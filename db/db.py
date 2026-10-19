"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
from __future__ import annotations

import colorsys
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Import por el EFECTO, no por lo que exporta: al cargarse, este módulo se
# aplica a sí mismo (ver el final de db/sin_preparadas.py) y deja
# `prepare_threshold=None` puesto para el proceso entero. Ningún nombre de
# acá se usa más abajo — por eso sin alias.
import db.sin_preparadas  # noqa: F401

# TZ es la zona de Santo Domingo, y viene de config para que haya UNA sola en
# todo Lucy: la usa el panel de tareas para decidir a qué DÍA pertenece un
# `vence_en`, que es un instante y no una fecha (ver `dia_rd`).
#
# `puede_ser_responsable` viene de config por lo mismo: es LA puerta de quién
# puede quedar con una tarea pendiente, y tiene que ser la misma para la ruta
# del panel y para la escritura de acá. Dos copias del criterio se separan.
import config as _config
from config import (CHAT_ID_CODE, CHAT_ID_DUENO, CHAT_ID_NATALIA, DATABASE_URL, QUIEN_DE_NATALIA, TZ,
                    nombres_con_code, puede_ser_responsable)

# HALLAZGO LATERAL (25-sep-2026, mientras se escribía `cerrar_y_derivar`):
# este módulo ya llamaba `log.warning(...)` en dos sitios de
# `poner_categoria` (líneas de más abajo) sin que `log` estuviera definido en
# ningún lado del archivo -- un NameError agazapado en un `except`/rama de
# validación que ninguna prueba de este repo ejercita hasta el final. Se
# arregla acá, con el mismo patrón que ya usan `web/app.py`
# (`logging.getLogger("lucy.panel")`) y `cerebro/agente.py`
# (`logging.getLogger("lucy.agente")`), porque `cerrar_y_derivar` (nueva,
# más abajo) también necesita loguear su propia tolerancia de columna
# ausente y no tiene sentido repetir el mismo bug a propósito.
log = logging.getLogger("lucy.db")


class MovimientoRechazado(Exception):
    """La base rechazó ESTA fila. El problema es el dato, no la conexión.

    Existe para poder distinguir dos cosas que hoy llegaban idénticas a quien
    llama y que piden lo contrario:

      · la base no responde  → hay que PARAR, no avanzar el cursor de la
        ingesta y reintentar en la próxima pasada.
      · esta fila está mal   → hay que SEGUIR con las demás, y dejar rastro de
        la que no entró.

    Sin la distinción, una fila mala mataba `revisar()` entera: se saltaba el
    canario, no se guardaba el cursor de UID de ese buzón, y el único registro
    era un `log.warning` en Railway. O sea que la ingesta de esa cuenta quedaba
    atascada para siempre y en silencio.
    """


# Las excepciones de psycopg que significan "la fila está mal", no "la base se
# cayó": violación de restricción (CHECK, NOT NULL, FK, UNIQUE) y dato fuera de
# rango o de tipo. Se resuelven con getattr porque varias suites herméticas
# stubean el módulo `psycopg` entero, y ahí estos nombres no existen; con la
# tupla vacía el `except` no atrapa nada y el comportamiento es el de antes.
_ERRORES_DE_FILA = tuple(
    c for c in (getattr(psycopg, n, None)
                for n in ("IntegrityError", "DataError"))
    if isinstance(c, type) and issubclass(c, BaseException))

# SIN CONSULTAS PREPARADAS (encargo 3, 22-sep-2026 — segunda vuelta tras un
# NO PASA del testigo, ver db/sin_preparadas.py). psycopg prepara una
# consulta repetida a partir de la quinta vez que la ve IDÉNTICA en la misma
# conexión (`prepare_threshold`, default 5) y le guarda a Postgres un PLAN.
# Si a `tareas` se le agrega una columna MIENTRAS ese plan sigue vivo, la
# siguiente ejecución revienta con `cached plan must not change result
# type` — pasó de verdad el 10-sep-2026 publicando el Responsable de Lucy,
# contra el `SELECT * FROM tareas` de `cerebro/despertador.py:519`.
#
# Esto NO se le pasa al pool por `kwargs`, y esta línea de acá arriba
# —`import db.sin_preparadas`— YA BASTA: el propio módulo se aplica a sí
# mismo al importarse (ver el final de `db/sin_preparadas.py`, agregado
# después de que un testigo encontrara que llamar a `.aplicar()` a mano en
# cada sitio dejaba huecos si alguna de esas llamadas se perdía o
# duplicaba). Con eso, `psycopg.AsyncConnection.connect` (la que el pool de
# abajo llama por dentro, en `psycopg_pool/pool_async.py:650`) ya tiene
# `prepare_threshold=None` como su PROPIO valor por omisión antes de que se
# construya el pool. El porqué completo —de un solo sitio para todo el
# proceso, en vez de un keyword por cada llamada que abre una conexión—
# está en `db/sin_preparadas.py`.

# Pool de conexiones reutilizables. Se abre al arrancar el bot (ver main.py).
pool = AsyncConnectionPool(DATABASE_URL, open=False)

# La firma del aviso de respaldo en la bandeja. Es a la vez lo primero que
# Tiziano lee y la clave con la que se busca el aviso anterior, así que vive en
# un solo lugar: dos copias de este texto se desincronizan y el aviso pasa a
# repetirse en cada vuelta del bucle.
AVISO_BACKUP_PREFIJO = "🚨 Sin respaldo de la base"


async def abrir() -> None:
    # wait=True es deliberado: si la base no responde, queremos reventar ACÁ,
    # al arrancar, y que el deploy falle a los gritos. Sin esto el pool abre
    # "en diferido" y el log canta "Pool abierto" aunque no haya conexión:
    # Lucy estuvo 3 horas respondiendo silencio con cara de que todo iba bien.
    await pool.open(wait=True, timeout=30)


RUTA_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "schema.sql")


def columnas_declaradas(ruta: str = RUTA_SCHEMA) -> dict[str, list[str]]:
    """{tabla: [columnas]} tal como lo declara db/schema.sql y sus migraciones.

    Una sola fuente para "qué columnas tiene esta tabla". La usan dos cosas
    que antes lo sabían por separado:

      · columnas_que_faltan(), acá abajo, para comparar contra Postgres.
      · cerebro/consultar.py, para ARMAR el esquema que ve el modelo en vez de
        tenerlo copiado a mano. Esa copia se había separado de la tabla: le
        faltaban 17 columnas, tres de ellas de `movimientos` —estado, banco,
        hash_contenido—, y sin `estado` Lucy no podía excluir las compras
        declinadas que el panel sí excluye.

    Las migraciones se leen también: una columna puede vivir un tiempo solo
    ahí antes de que alguien la baje a schema.sql. Así llegó
    `movimientos.hash_contenido`.
    """
    sin_comentarios = _sin_comentarios  # definido más abajo; se resuelve al llamar

    with open(ruta, encoding="utf-8") as f:
        crudo = f.read()

    tablas: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);",
                         crudo, re.S | re.I):
        # Cortar por comas de PRIMER NIVEL: un CHECK (x IN ('a','b')) trae
        # comas propias que no separan columnas.
        piezas: list[str] = []
        pieza, profundidad = "", 0
        for ch in sin_comentarios(m.group(2)):
            if ch == "(":
                profundidad += 1
            elif ch == ")":
                profundidad -= 1
            if ch == "," and profundidad == 0:
                piezas.append(pieza)
                pieza = ""
            else:
                pieza += ch
        piezas.append(pieza)

        columnas: list[str] = []
        for p in piezas:
            palabras = p.split()
            if not palabras:
                continue
            primera = palabras[0].lower()
            # Las restricciones de tabla no son columnas.
            if primera in ("primary", "unique", "foreign", "check",
                           "constraint", "exclude", "like"):
                continue
            columnas.append(primera)
        tablas[m.group(1).lower()] = columnas

    migraciones = os.path.join(os.path.dirname(ruta), "migrations")
    if os.path.isdir(migraciones):
        for nombre in sorted(os.listdir(migraciones)):
            if not nombre.endswith(".sql"):
                continue
            with open(os.path.join(migraciones, nombre), encoding="utf-8") as f:
                txt = sin_comentarios(f.read())
            for m in re.finditer(
                    r"ALTER TABLE\s+(\w+)\s+ADD COLUMN"
                    r"(?:\s+IF NOT EXISTS)?\s+(\w+)", txt, re.I | re.S):
                tabla, col = m.group(1).lower(), m.group(2).lower()
                if tabla in tablas and col not in tablas[tabla]:
                    tablas[tabla].append(col)
    return tablas


def _sin_comentarios(txt: str) -> str:
    return "\n".join(l.split("--")[0] for l in txt.splitlines())


def _archivos_del_esquema(ruta: str, con_migraciones: bool = True) -> list[str]:
    """db/schema.sql primero y las migraciones después, en orden de nombre.

    El orden importa: una migración puede tirar una restricción y volver a
    ponerla (lo hace 2026-08-31b con `movimientos_estado_valido`), y leerlas
    desordenadas daría un objeto de menos o de más.
    """
    archivos = [ruta]
    migraciones = os.path.join(os.path.dirname(ruta), "migrations")
    if con_migraciones and os.path.isdir(migraciones):
        archivos += [os.path.join(migraciones, n)
                     for n in sorted(os.listdir(migraciones))
                     if n.endswith(".sql")]
    return archivos


# Un solo barrido, en orden, sobre las cinco formas en que este repo nombra un
# índice o una restricción. Van juntas en una alternación para que el orden de
# aparición se conserve: si un DROP se leyera antes que su ADD, el objeto
# quedaría fuera de la lista.
_RE_OBJETOS = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<idx>\w+)\s+ON\s+(?P<idx_tabla>\w+)"
    r"|DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?(?P<drop_idx>\w+)"
    r"|ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<add_tabla>\w+)\s+ADD\s+CONSTRAINT"
    r"\s+(?P<add_con>\w+)"
    r"|ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<drop_tabla>\w+)\s+DROP\s+CONSTRAINT"
    r"\s+(?:IF\s+EXISTS\s+)?(?P<drop_con>\w+)",
    re.I | re.S)

_RE_TABLA = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);", re.S | re.I)


def objetos_declarados(ruta: str = RUTA_SCHEMA,
                       con_migraciones: bool = True) -> dict[str, dict[str, str]]:
    """{'indices': {nombre: tabla}, 'restricciones': {nombre: tabla}}.

    `con_migraciones=False` lee SOLO db/schema.sql. Es lo que hace falta para
    preguntar si el archivo y las migraciones dicen lo mismo, que es la
    pregunta del candado hermético (tests/test_esquema_reproduce_la_base.py).

    La otra mitad de `columnas_declaradas()`. Aquella responde "¿qué columnas
    tiene esta tabla?"; ésta responde "¿qué índices y qué restricciones CON
    NOMBRE declara el repo?", que hasta el 5-sep-2026 no lo respondía nadie —
    y por ahí se fueron las tres derivas que se midieron ese día:

      · `idx_movimientos_hash` vivía solo en una migración. Sin él, el
        `ON CONFLICT (hash_contenido)` de guardar_movimiento revienta y no
        entra un solo movimiento bancario.
      · `idx_movimientos_banco` e `idx_correo_reportado_fecha` estaban en la
        base real y en ningún archivo.
      · `cuentas_propias_patron_unico` estaba en la migración, pero el
        `CREATE TABLE IF NOT EXISTS` de schema.sql corría antes y la saltaba.

    SOLO los objetos con nombre propio. Las PRIMARY KEY y las FOREIGN KEY se
    declaran sin nombre en este repo, y Postgres se los inventa: producción
    llama `gastos_pkey` a la clave de `movimientos` porque la tabla se llamó
    `gastos`, mientras una base nueva la llamaría `movimientos_pkey`. Comparar
    eso mediría cómo bautiza Postgres, no qué esquema hay. La tabla y la
    columna que sostienen esa PK sí se comparan, en tablas_que_faltan() y
    columnas_que_faltan().
    """
    indices: dict[str, str] = {}
    restricciones: dict[str, str] = {}
    for archivo in _archivos_del_esquema(ruta, con_migraciones):
        try:
            with open(archivo, encoding="utf-8") as f:
                txt = _sin_comentarios(f.read())
        except OSError:
            continue
        # Las restricciones con nombre escritas DENTRO del CREATE TABLE.
        for m in _RE_TABLA.finditer(txt):
            tabla = m.group(1).lower()
            for c in re.finditer(r"CONSTRAINT\s+(\w+)", m.group(2), re.I):
                restricciones[c.group(1).lower()] = tabla
        # Y todo lo que se declara fuera, en orden de aparición.
        for m in _RE_OBJETOS.finditer(txt):
            if m.group("idx"):
                indices[m.group("idx").lower()] = m.group("idx_tabla").lower()
            elif m.group("drop_idx"):
                indices.pop(m.group("drop_idx").lower(), None)
            elif m.group("add_con"):
                restricciones[m.group("add_con").lower()] = \
                    m.group("add_tabla").lower()
            elif m.group("drop_con"):
                restricciones.pop(m.group("drop_con").lower(), None)
    return {"indices": indices, "restricciones": restricciones}


# Los índices de la base real, dejando fuera los que Postgres crea SOLO para
# sostener una restricción (PK, UNIQUE): ésos ya se comparan como restricción y
# contarlos dos veces daría un descuadre inventado.
_SQL_INDICES_REALES = """
SELECT c.relname, i.relname
  FROM pg_index x
  JOIN pg_class c ON c.oid = x.indrelid
  JOIN pg_class i ON i.oid = x.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
   AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.oid)
"""

# Solo UNIQUE ('u') y CHECK ('c'): son las que este repo escribe con nombre
# propio. 'p' y 'f' van sin nombre (ver objetos_declarados), y 'n' —el NOT NULL
# con nombre en el catálogo— solo existe desde Postgres 17, así que compararlo
# mediría la versión del motor. El NOT NULL de verdad viaja en la columna.
_SQL_RESTRICCIONES_REALES = """
SELECT rel.relname, con.conname
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname = 'public' AND con.contype IN ('u', 'c')
"""


async def objetos_que_faltan() -> list[str]:
    """Los índices y restricciones donde el repo y la base real no coinciden.

    Mismo par de direcciones que columnas_que_faltan(), y duelen igual de
    distinto:

      · declarado y no en la base → la consulta que lo necesita revienta. Es
        el caso de `idx_movimientos_hash`: sin él no entra un movimiento.
      · en la base y no declarado → nadie sabe que existe, así que el día que
        se levante la base de cero no está. Silencioso, y el peligroso.

    Ningún test hermético puede ver esto: hay que preguntarle a la base.
    """
    declarados = objetos_declarados()
    async with pool.connection() as conn:
        cur = await conn.execute(_SQL_INDICES_REALES)
        indices_reales = {i.lower(): t.lower() for t, i in await cur.fetchall()}
        cur = await conn.execute(_SQL_RESTRICCIONES_REALES)
        restr_reales = {c.lower(): t.lower() for t, c in await cur.fetchall()}

    problemas: list[str] = []
    for etiqueta, decl, reales in (
            ("índice", declarados["indices"], indices_reales),
            ("restricción", declarados["restricciones"], restr_reales)):
        for nombre in sorted(set(decl) - set(reales)):
            problemas.append(
                f"{etiqueta} {decl[nombre]}.{nombre}: en db/schema.sql, no en "
                "la base (lo que dependa de él revienta)")
        for nombre in sorted(set(reales) - set(decl)):
            problemas.append(
                f"{etiqueta} {reales[nombre]}.{nombre}: en la base, no en "
                "db/schema.sql (una base nueva no lo tendría)")
    return sorted(problemas)


async def columnas_que_faltan() -> list[str]:
    """Dónde NO coinciden db/schema.sql y las columnas de la base real.

    Existe por el mismo fallo que tablas_que_faltan() pero un nivel más abajo,
    y ese ya ocurrió: `movimientos.banco` existía en Postgres y NO en
    db/schema.sql. Nadie se enteró hasta que algo reventó.

    Ahora eso cuesta más caro, y por eso este chequeo: el esquema que ve el
    modelo al escribir SQL se ARMA desde db/schema.sql. Si el archivo se atrasa
    respecto de la base, Lucy deja de saber que una columna existe — y una
    columna que no conoce es una columna por la que no puede filtrar. Fue
    exactamente lo que pasó con `estado`: sumaba compras que el banco había
    rechazado, mientras el panel las excluía.

    Se miran las DOS direcciones porque duelen distinto:
      · en el archivo y no en la base → la consulta revienta, ruidoso.
      · en la base y no en el archivo → el modelo no la ve, silencioso. Éste
        es el peligroso, y el que no tenía detector.

    Ningún test hermético puede ver esto: hay que preguntarle a la base.
    """
    declaradas = columnas_declaradas()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'")
        reales: dict[str, set[str]] = {}
        for tabla, col in await cur.fetchall():
            reales.setdefault(tabla.lower(), set()).add(col.lower())

    problemas: list[str] = []
    for tabla, columnas in declaradas.items():
        if tabla not in reales:
            continue  # eso lo reporta tablas_que_faltan(), no se dice dos veces
        for col in sorted(set(columnas) - reales[tabla]):
            problemas.append(f"{tabla}.{col}: en db/schema.sql, no en la base")
        for col in sorted(reales[tabla] - set(columnas)):
            problemas.append(
                f"{tabla}.{col}: en la base, no en db/schema.sql "
                "(Lucy no sabe que existe)")
    return sorted(problemas)


def tablas_declaradas(ruta: str = RUTA_SCHEMA) -> set[str]:
    """Los nombres de tabla que declara db/schema.sql. Vacío si no se puede leer.

    Está separada de tablas_que_faltan() para poder probarla sin base: es puro
    parseo de un archivo, y el parseo es donde se equivocó (ver abajo).
    """
    try:
        with open(ruta, encoding="utf-8") as f:
            # Sin quitar los comentarios primero, cualquier frase en prosa que
            # diga "CREATE TABLE" inventa una tabla que falta. Pasó el
            # 5-sep-2026: un comentario que explicaba por qué el CREATE TABLE
            # de una migración se salta hizo que esto reportara una tabla
            # llamada `se`. Una alarma que grita en falso es peor que ninguna:
            # enseña a ignorarla. Los otros dos lectores de este archivo
            # —columnas_declaradas() y backup._tablas_del_repo()— ya lo hacían.
            return {t.lower() for t in re.findall(
                r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)",
                _sin_comentarios(f.read()), re.I)}
    except OSError:
        return set()


async def tablas_que_faltan() -> list[str]:
    """Las tablas que db/schema.sql declara y la base real NO tiene.

    Existe por un fallo concreto: `backups` estaba en el archivo del repo y no
    en la base de Railway, así que el chequeo de respaldo reventaba cada diez
    minutos con UndefinedTable — y como el bucle atrapa la excepción y sigue,
    reventaba EN SILENCIO. Lucy pasó semanas sin poder avisar que no había
    respaldo, que es justo lo que ese aviso vino a arreglar.

    Ningún test hermético puede ver esto: los dobles de conexión responden lo
    que uno quiera. Solo se sabe preguntándole a la base de verdad, y el momento
    de preguntar es al arrancar, cuando el log todavía lo lee alguien.
    """
    declaradas = tablas_declaradas()
    if not declaradas:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'")
        reales = {r[0].lower() for r in await cur.fetchall()}
    return sorted(declaradas - reales)


async def cerrar() -> None:
    await pool.close()


async def guardar_en_bandeja(
    *,
    tipo_entrada: str,
    contenido_raw: str | None = None,
    archivo_id: str | None = None,
    chat_id: int | None = None,
    telegram_msg_id: int | None = None,
    origen: str = "telegram",
) -> int:
    """Guarda un mensaje crudo en la bandeja y devuelve su id.

    No interpreta nada: solo captura. La comprensión viene después,
    en un paso aparte, leyendo de esta tabla.
    """
    hash_contenido = (
        hashlib.sha256(contenido_raw.encode("utf-8")).hexdigest()
        if contenido_raw
        else None
    )
    async with pool.connection() as conn:
        # ON CONFLICT = idempotencia. Telegram reentrega el mismo mensaje si no
        # le confirmamos a tiempo (un deploy, un timeout, la base lenta). Sin
        # esto, una reentrega crea una fila duplicada y mañana Lucy te recuerda
        # dos veces la misma tarea. El DO UPDATE es un no-op: existe solo para
        # que RETURNING devuelva el id de la fila que YA estaba.
        cur = await conn.execute(
            """
            INSERT INTO bandeja
              (tipo_entrada, contenido_raw, archivo_id, chat_id,
               telegram_msg_id, hash_contenido, origen)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, telegram_msg_id) DO UPDATE
              SET contenido_raw = EXCLUDED.contenido_raw
            RETURNING id
            """,
            (tipo_entrada, contenido_raw, archivo_id, chat_id,
             telegram_msg_id, hash_contenido, origen),
        )
        row = await cur.fetchone()
        return row[0]


async def rescatar_procesando(minutos: int = 10, max_intentos: int = 3) -> int:
    """Devuelve a la cola los mensajes que quedaron en 'procesando'.

    `tomar_pendientes` marca 'procesando' al reclamar una fila. Si el proceso
    muere entre eso y el final del turno —un redespliegue, que en este proyecto
    pasa varias veces al día— NADIE la devuelve: se queda en 'procesando' para
    siempre y Tiziano escribe algo y Lucy nunca contesta. Pasó con tres
    mensajes suyos, uno del 30 de agosto.

    Se llama al ARRANCAR, que es justo después del redespliegue que las dejó
    huérfanas. El margen de minutos evita pisar un turno que de verdad está
    corriendo en otro contenedor: durante un despliegue conviven dos unos
    segundos, y un turno normal no llega a diez minutos.

    Los intentos suben. Si un mensaje concreto es el que MATA el proceso, sin
    esto volvería a reclamarse eternamente y tumbaría a Lucy en cada arranque;
    al tercer intento pasa a 'error' y se queda quieto, visible, sin bloquear
    a los demás.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE bandeja
               SET estado = CASE WHEN intentos + 1 >= %s THEN 'error'
                                 ELSE 'sin_procesar' END,
                   intentos = intentos + 1,
                   error_detalle = CASE WHEN intentos + 1 >= %s
                       THEN 'Se quedó en procesando tras varios arranques: puede '
                            'ser el mensaje que mata el proceso.' END
             WHERE estado = 'procesando'
               AND creado_en < now() - make_interval(mins => %s)
            RETURNING id
            """, (max_intentos, max_intentos, minutos))
        return len(await cur.fetchall())


async def devolver_reclamadas(ids: list[int]) -> int:
    """Devuelve a 'sin_procesar' las filas que esta instancia reclamó y no
    terminó, al apagarse. Devuelve cuántas.

    NO suma `intentos` ni pone `reintentar_despues`: no fallaron, se cortaron
    (a diferencia de `rescatar_procesando` y `devolver_a_cola`, que sí cuentan).
    Solo toca filas todavía en 'procesando': una que alcanzó a cerrarse
    ('procesado', 'error', 'esperando_confirmacion') no se revive.
    """
    if not ids:
        return 0
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE bandeja SET estado = 'sin_procesar'
             WHERE id = ANY(%s) AND estado = 'procesando'
            RETURNING id
            """, (list(ids),))
        return len(await cur.fetchall())


async def tomar_pendientes(
    tipos: tuple[str, ...] = ("texto", "audio", "foto", "sistema", "email"),
    limite: int = 5,
) -> list[dict]:
    """Reclama filas sin procesar y las marca 'procesando' en un solo paso.

    FOR UPDATE SKIP LOCKED no es adorno: durante cada redespliegue conviven dos
    contenedores unos segundos (lo vemos en los logs como 409 Conflict de
    Telegram). Sin esto, los dos tomarían la misma fila y Lucy interpretaría el
    mismo mensaje dos veces. Con esto, el segundo simplemente saltea lo tomado.

    `tipos` acota a lo que Lucy sabe interpretar hoy. Desde que tiene vista,
    entran los tres; lo que aparezca mañana (un PDF, un reenvío) se queda en
    'sin_procesar' esperando su turno, sin perderse ni trabar la cola.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            UPDATE bandeja SET estado = 'procesando'
            WHERE id IN (
                SELECT id FROM bandeja
                WHERE estado = 'sin_procesar'
                  AND tipo_entrada = ANY(%s)
                  AND (reintentar_despues IS NULL OR reintentar_despues <= now())
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, tipo_entrada, contenido_raw, archivo_id, chat_id,
                      telegram_msg_id, intentos, transcripcion
            """,
            (list(tipos), limite),
        )
        return await cur.fetchall()


async def guardar_interpretacion(
    bandeja_id: int,
    clasificacion: str,
    interpretacion: dict,
    estado: str = "esperando_confirmacion",
) -> None:
    """Guarda lo que el cerebro entendió. Por defecto queda esperando el ✅.

    No crea todavía la tarea/evento/gasto: eso es un paso aparte y deliberado.
    Primero que Tiziano vea qué entendió Lucy; recién después se escribe.

    `estado` se fuerza a 'procesado' para lo que no va a crear nada: la charla
    y las preguntas se responden y se archivan ahí mismo. Poner un botón de
    confirmación bajo un "buenos días" sería pedirle a Tiziano que apruebe la
    existencia de un saludo.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE bandeja
               SET clasificacion  = %s,
                   interpretacion = %s,
                   estado         = %s,
                   procesado_en   = now(),
                   error_detalle  = NULL
             WHERE id = %s
            """,
            (clasificacion, json.dumps(interpretacion), estado, bandeja_id),
        )


async def guardar_transcripcion(bandeja_id: int, texto: str) -> None:
    """Guarda lo que Whisper oyó, antes de interpretarlo.

    Se escribe en un paso aparte a propósito: si DeepSeek falla después, la
    transcripción ya está a salvo y el reintento no vuelve a pagar el audio.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE bandeja SET transcripcion = %s WHERE id = %s",
            (texto, bandeja_id),
        )


async def devolver_a_cola(bandeja_id: int, espera_s: int) -> int:
    """Devuelve la fila a la cola tras un fallo pasajero. Devuelve los intentos.

    Un 429 de la IA o un timeout de red duran segundos; condenar el mensaje por
    eso sería perderlo, que es lo único que Lucy no puede hacer. Vuelve a
    'sin_procesar' con una espera, y el bucle la retoma sola.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE bandeja
               SET estado             = 'sin_procesar',
                   intentos           = intentos + 1,
                   error_detalle      = NULL,
                   reintentar_despues = now() + make_interval(secs => %s)
             WHERE id = %s
            RETURNING intentos
            """,
            (espera_s, bandeja_id),
        )
        row = await cur.fetchone()
        return row[0]


async def marcar_error(bandeja_id: int, detalle: str) -> None:
    """Deja la fila en 'error' con el motivo, para poder reintentar a mano.

    Nunca se borra ni se pierde: el mensaje crudo sigue intacto en la bandeja.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE bandeja SET estado = 'error', error_detalle = %s WHERE id = %s",
            (detalle[:2000], bandeja_id),
        )


async def guardar_respuesta(bandeja_id: int, texto: str) -> None:
    """Guarda lo que Lucy contestó. Es SU mitad de la conversación.

    Sin esto no hay memoria conversacional posible: "movelo a las 6" solo se
    entiende si se recuerda qué se dijo justo antes — de los dos lados.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE bandeja SET respuesta_lucy = %s WHERE id = %s",
            (texto[:4000], bandeja_id),
        )


async def destinos_con_encargo_hoy(origen: str, prefijo: str, desde) -> set[int]:
    """A qué chats ya se les dejó HOY un encargo que empieza con `prefijo`.

    Es el candado de "esto ya salió hoy" de los procesos que hablan una vez al
    día: la marca es la PROPIA fila que el proceso deja, no una tabla aparte.
    Un candado que lee lo mismo que escribe no puede quedarse abierto porque a
    nadie se le ocurrió inicializar una fila — que es exactamente como el
    reporte de correo llegó a salir ~100 veces por mañana.

    Devuelve un conjunto de `chat_id` y no un booleano porque el candado es POR
    DESTINATARIO. Un booleano global se cierra en cuanto ALGUIEN recibió lo
    suyo, y el resto de los destinos se quedan sin su encargo del día sin que
    nada lo registre: el reporte de correo tiene un destino por buzón
    (`reporte_a`), así que "ya salió hoy" solo tiene sentido preguntado con
    nombre y apellido.

    El precio, y hay que decirlo: depende del TEXTO del encargo. Quien cambie
    esa primera frase abre el candado. Por eso el prefijo vive en una constante
    del módulo que lo escribe y hay un test que ata las dos puntas.
    """
    patron = prefijo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT DISTINCT chat_id FROM bandeja
             WHERE origen = %s AND tipo_entrada = 'sistema'
               AND contenido_raw LIKE %s
               AND creado_en >= %s
               AND chat_id IS NOT NULL
            """,
            (origen, patron + "%", desde),
        )
        return {f[0] for f in await cur.fetchall()}


async def leer_estado_correo(cuenta: str) -> dict | None:
    """Estado de lectura de una cuenta. None si es la primera vez.

    Trae ultimo_reporte: la fecha del último reporte matinal, para no repetirlo
    el mismo día aunque el proceso se reinicie.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT uidvalidity, ultimo_uid, ultimo_reporte "
            "FROM correo_estado WHERE cuenta = %s",
            (cuenta,),
        )
        return await cur.fetchone()


async def correos_ya_reportados(cuenta: str, uids: list[int],
                                 destino: int = CHAT_ID_DUENO) -> set[int]:
    """De esos uids, cuáles ya se le informaron a ESE destino.

    Es la memoria que hace posible mirar los SIN LEER en vez de un puntero que
    se consume: sin ella, un correo que él no marque leído volvería a aparecer
    cada mañana hasta el fin de los tiempos. Informado una vez, informado.

    POR DESTINO desde el encargo 3 ("Rosi independiente", 22-sep-2026): que un
    correo del buzón del estudio ya se le haya informado a Tiziano no puede
    borrarle a Rosi la oportunidad de que se lo informen a ella, y al revés.
    Una fila con `destino_chat_id IS NULL` es de ANTES de este encargo, cuando
    solo existía un destino por buzón (el dueño) -- se cuenta como "informada
    al dueño" únicamente cuando `destino` ES el dueño; para cualquier otro
    destino (Rosi, o quien sea mañana) esas filas viejas no dicen nada, así
    que no la excluyen.

    SI LA COLUMNA TODAVÍA NO EXISTE (la migración de este encargo no se
    aplicó: SQLSTATE 42703), cae a la consulta de ANTES -- sin filtrar por
    destino -- mismo patrón que `cerebro/despertador.py::revisar` con
    `primero_id`. Con eso, hasta que la migración corra, TODO destino ve como
    "ya informado" lo que ya se le informó a cualquiera -- sub-informa, nunca
    sobre-informa, que es el lado seguro mientras la migración no está.
    """
    if not uids:
        return set()
    es_dueno = destino == CHAT_ID_DUENO
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT uid FROM correo_reportado
                     WHERE cuenta = %s AND uid = ANY(%s)
                       AND (destino_chat_id = %s
                            OR (destino_chat_id IS NULL AND %s))
                    """,
                    (cuenta, [int(u) for u in uids], destino, es_dueno),
                )
                return {r[0] for r in await cur.fetchall()}
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            cur = await conn.execute(
                "SELECT uid FROM correo_reportado WHERE cuenta = %s AND uid = ANY(%s)",
                (cuenta, [int(u) for u in uids]),
            )
            return {r[0] for r in await cur.fetchall()}


async def marcar_correo_reportado(cuenta: str, uid: int, *,
                                  destino: int = CHAT_ID_DUENO,
                                  nivel: str = "", ambito: str = "",
                                  area: str = "", asunto: str = "",
                                  bandeja_id: int | None = None) -> None:
    """Deja constancia de que ese correo ya se le informó a ESE destino, con
    su clasificación.

    Guardar CÓMO se clasificó no es adorno: es lo que después permite contestar
    "¿por qué no me avisaste de esto?" con datos en la mano, y afinar las
    reglas con hechos en vez de impresiones.

    Una fila por (cuenta, uid, destino) -- el mismo correo puede tener una
    fila para Tiziano y otra para Rosi, cada una con su propio `bandeja_id`
    (el encargo que se lo contó a ESA persona).

    SI LA COLUMNA TODAVÍA NO EXISTE (SQLSTATE 42703), cae al INSERT de antes
    -- sin destino -- para no romper el reporte mientras la migración no
    corrió. Ver `correos_ya_reportados` para el porqué completo.
    """
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO correo_reportado
                      (cuenta, uid, destino_chat_id, nivel, ambito, area,
                       asunto, bandeja_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cuenta, uid, destino_chat_id) DO NOTHING
                    """,
                    (cuenta, int(uid), destino, nivel or None, ambito or None,
                     area or None, (asunto or "")[:300] or None, bandeja_id),
                )
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            # `ON CONFLICT DO NOTHING` A SECAS -- sin columnas -- a propósito:
            # antes de la migración, la restricción única en producción sigue
            # siendo la PK vieja (cuenta, uid); después, es el índice nuevo
            # (cuenta, uid, destino_chat_id). Nombrar columnas ataría este
            # camino de compatibilidad a UNA de las dos formas, y dejaría de
            # ser compatible con la otra. Sin lista, Postgres lo aplica
            # contra CUALQUIER restricción única que choque, sea cual sea.
            await conn.execute(
                """
                INSERT INTO correo_reportado
                  (cuenta, uid, nivel, ambito, area, asunto, bandeja_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (cuenta, int(uid), nivel or None, ambito or None, area or None,
                 (asunto or "")[:300] or None, bandeja_id),
            )


async def correos_por_marcar_leidos() -> list[dict]:
    """Correos ya informados cuyo reporte SÍ llegó y todavía no están marcados.

    El filtro es la clave de que "leído" no mienta: solo entran los que
    pertenecen a un encargo que el agente ya procesó y contestó (estado
    'procesado' + respuesta enviada). Si el reporte se rompió a mitad de
    camino, esos correos siguen sin marcar y sin contar como informados, así
    que vuelven a aparecer mañana.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT r.cuenta, r.uid
              FROM correo_reportado r
              JOIN bandeja b ON b.id = r.bandeja_id
             WHERE r.leido_en IS NULL
               AND b.estado = 'procesado'
               AND b.respuesta_lucy IS NOT NULL
             LIMIT 200
            """
        )
        return await cur.fetchall()


async def confirmar_leido(cuenta: str, uid: int) -> None:
    """Deja constancia de que ese correo ya quedó marcado como leído en Gmail."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE correo_reportado SET leido_en = now() "
            "WHERE cuenta = %s AND uid = %s",
            (cuenta, int(uid)),
        )


async def olvidar_reportados_fallidos() -> int:
    """Suelta los correos cuyo reporte NUNCA llegó, para que vuelvan mañana.

    Sin esto habría un agujero silencioso: un correo anotado como "reportado"
    cuyo encargo murió con error quedaría marcado para siempre y no se
    volvería a mencionar — justo el silencio que esta política prohíbe.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            DELETE FROM correo_reportado r
             USING bandeja b
             WHERE b.id = r.bandeja_id
               AND r.leido_en IS NULL
               AND b.estado = 'error'
            """
        )
        return cur.rowcount


async def guardar_estado_correo(
    cuenta: str, uidvalidity: int, ultimo_uid: int, ultimo_reporte=None
) -> None:
    """Avanza el puntero de lectura de una cuenta. Upsert.

    El puntero es lo que hace que revisar sea mirar hacia adelante y no releer
    44.000 correos. ultimo_reporte, cuando se pasa, marca que el reporte de hoy
    ya salió: sobrevive reinicios, así el reporte matinal no se duplica.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO correo_estado
              (cuenta, uidvalidity, ultimo_uid, ultimo_reporte, actualizado_en)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (cuenta) DO UPDATE
              SET uidvalidity = EXCLUDED.uidvalidity,
                  ultimo_uid  = EXCLUDED.ultimo_uid,
                  ultimo_reporte = COALESCE(EXCLUDED.ultimo_reporte,
                                            correo_estado.ultimo_reporte),
                  actualizado_en = now()
            """,
            (cuenta, uidvalidity, ultimo_uid, ultimo_reporte),
        )


async def ultimo_backup() -> dict | None:
    """El último respaldo que terminó bien. None = nunca hubo ninguno.

    Es la única forma que tiene Lucy de saber si todavía tiene copia. Antes esa
    verdad vivía solo en los nombres de archivo de una carpeta de Google Drive
    que el contenedor de Railway no puede ver — por eso los backups se pudieron
    caer el 29-jul-2026 y pasar 25 días sin que nadie se enterara.

    El None NO es un caso raro que haya que suavizar: significa "no hay
    respaldo", y se tiene que leer exactamente así.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT hecho_en, archivo, bytes, tablas, filas, esquema, origen "
            "FROM backups ORDER BY hecho_en DESC LIMIT 1"
        )
        return await cur.fetchone()


async def ultimo_aviso_de_backup() -> datetime | None:
    """Cuándo salió el último aviso de backup atrasado (para no repetirlo cada vuelta).

    Se busca en la bandeja, que es donde `despertador._avisar` deja todo lo que
    Lucy dice por su cuenta: el registro del aviso ES la memoria de que el aviso
    salió. Mismo patrón que el dedupe del encargo semanal — no hace falta una
    tabla nueva para acordarse de algo que ya quedó escrito.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT creado_en FROM bandeja
             WHERE origen = 'despertador' AND tipo_entrada = 'aviso'
               AND respuesta_lucy LIKE %s
             ORDER BY creado_en DESC LIMIT 1
            """,
            (AVISO_BACKUP_PREFIJO + "%",),
        )
        fila = await cur.fetchone()
        return fila[0] if fila else None


async def listar_preferencias() -> list[dict]:
    """Las reglas activas que Lucy aprendió (req 35), las más nuevas primero.

    Se leen en cada mensaje para inyectarlas en el prompt del agente: son el
    'dentro de los límites que vos fijás' de la autonomía. Baratas de traer —
    son pocas y la tabla es chica — y siempre frescas.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, texto, contexto FROM preferencias "
            "WHERE borrado_en IS NULL ORDER BY creado_en DESC"
        )
        return await cur.fetchall()


async def ultimos_intercambios(
    chat_id: int, excluir: list[int], n: int = 6
) -> list[dict]:
    """Los últimos n intercambios (lo que dijo Tiziano, lo que contestó Lucy).

    Es la memoria corta del agente (req 11). Se excluyen las filas que ya
    viajan aparte en el contexto (la actual y la pendiente) para no duplicar.

    Entran también las filas donde solo habló Lucy (los avisos del
    despertador: dicho NULL, respuesta_lucy con texto). Sin ellas, si Lucy
    pregunta "¿desde dónde salís?" y Tiziano contesta "del estudio", el
    agente vería la respuesta sin la pregunta — proactividad que rompe la
    conversación en vez de empezarla.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, tipo_entrada,
                   coalesce(transcripcion, contenido_raw) AS dicho,
                   respuesta_lucy
              FROM bandeja
             WHERE chat_id = %s
               AND NOT (id = ANY(%s))
               AND (coalesce(transcripcion, contenido_raw) IS NOT NULL
                    OR respuesta_lucy IS NOT NULL)
             ORDER BY id DESC
             LIMIT %s
            """,
            (chat_id, excluir or [0], n),
        )
        filas = await cur.fetchall()
    return list(reversed(filas))


async def lugar_por_nombre(nombre: str) -> dict | None:
    """Un lugar con nombre de Tiziano, o None si no existe con ese nombre."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, nombre, lat, lon, radio_m FROM lugares "
            "WHERE borrado_en IS NULL AND lower(nombre) = lower(%s) LIMIT 1",
            ((nombre or "").strip(),),
        )
        return await cur.fetchone()


async def registrar_aviso(chat_id: int, texto: str, origen: str = "despertador") -> int:
    """Deja constancia en la bandeja de algo que Lucy dijo POR SU CUENTA.

    Los avisos del despertador entran a la conversación como una fila más
    (origen 'despertador', sin dicho, con respuesta_lucy): así la memoria
    corta y la de largo plazo los ven igual que a cualquier otro intercambio.
    Lo que Lucy dice proactivamente también es parte de la historia.

    `origen` es libre (la columna no tiene vocabulario cerrado; ver
    `db/schema.sql`) — `cerebro/copia_dueno.py` lo usa con 'copia_dueno' para
    poder distinguir, si hiciera falta mirar la bandeja, una copia de un
    aviso real del despertador. Al modelo no le llega esta columna: lo que ve
    es solo `respuesta_lucy` (`cerebro/agente.py::atender`), así que la marca
    que de verdad importa para que Lucy sepa que es una copia va en el TEXTO.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO bandeja
              (origen, tipo_entrada, chat_id, estado, respuesta_lucy, procesado_en)
            VALUES (%s, 'aviso', %s, 'procesado', %s, now())
            RETURNING id
            """,
            (origen, chat_id, texto[:4000]),
        )
        return (await cur.fetchone())[0]


async def buscar_esperando_respuesta(chat_id: int, excluir_id: int) -> dict | None:
    """La conversación que quedó abierta cuando Lucy preguntó algo (si hay).

    NO la marca como cerrada: eso se hace recién cuando el mensaje nuevo se
    procesa hasta el final. Si esto la cerrara al leerla y el procesamiento
    fallara a mitad de camino, el reintento arrancaría sin el contexto — la
    ventana se habría cerrado sola con la pregunta adentro.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, interpretacion
              FROM bandeja
             WHERE chat_id = %s AND estado = 'esperando_respuesta' AND id <> %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (chat_id, excluir_id),
        )
        return await cur.fetchone()


async def obtener(bandeja_id: int) -> dict | None:
    """Trae una fila completa de la bandeja. La usa el manejador de botones."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, tipo_entrada, contenido_raw, transcripcion, chat_id,
                   telegram_msg_id, estado, clasificacion, interpretacion
              FROM bandeja WHERE id = %s
            """,
            (bandeja_id,),
        )
        return await cur.fetchone()


async def cambiar_estado(bandeja_id: int, estado: str, desde: str | None = None) -> bool:
    """Cambia el estado y dice si realmente cambió algo.

    `desde` convierte la operación en un candado: solo pasa si la fila todavía
    está en el estado esperado. Sin eso, dos toques rápidos al botón ✅ crearían
    la misma tarea dos veces — Telegram reenvía el callback si tarda en
    responder, así que no es una hipótesis rebuscada.
    """
    sql = "UPDATE bandeja SET estado = %s WHERE id = %s"
    args: tuple = (estado, bandeja_id)
    if desde is not None:
        sql += " AND estado = %s"
        args += (desde,)

    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return cur.rowcount > 0


# ── EL NOMBRE DE UN PROYECTO (28/29-sep-2026, pieza 1 del diseño «proyectos») ──
#
# UNA SOLA PUERTA para «qué nombre vale», y UNA SOLA función para «¿ya hay uno
# con ese nombre?», llamadas por TODO lo que escribe `proyectos.nombre`:
# `crud.editar` (Telegram y panel, por `crud.PUERTAS`), `crud.deshacer`,
# `crud.perfil` (que busca), `crear_proyecto`, `poner_cliente` (en CDS/ACD el nombre es el del cliente) y
# `convertir_tarea_en_proyecto`. La lista de escritores no está tecleada acá: la saca de lo real
# `tests/test_nombre_de_proyecto.py`. (`_buscar_o_crear` ya no crea proyectos: pieza 1 de «Un proyecto no se crea sin cliente».)
#
# Sin candado en la base (decisión de la sala: sin migración): un índice único
# sería DDL en producción. La regla es de aplicación, y por eso el duplicado se
# compara con LA MISMA consulta con la que Lucy busca un proyecto por su nombre.
LARGO_NOMBRE_PROYECTO = 200


class NombreDeProyectoNoVale(ValueError):
    """Un nombre de proyecto que no puede quedar. `clave` dice por qué
    (`vacio`, `largo`, `repetido`, y `cliente`: en CDS y ACD el nombre es el del cliente) para que una pantalla lo traduzca sin
    adivinar por el texto del mensaje. El mensaje NUNCA repite el nombre pedido."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


# EL TÍTULO DE UNA TAREA (Lucy 1.0, E2, G12): no vacío y de a lo sumo
# `LARGO_TITULO_TAREA` caracteres. El panel ya lo exigía al crear (`web/app.py`
# usa esta misma constante); por Telegram, `crud.editar` dejaba un título
# vacío o larguísimo. La puerta es `crud.PUERTAS["tareas"]["titulo"]`.
LARGO_TITULO_TAREA = 200


def titulo_de_tarea_que_vale(valor) -> str:
    """El título limpio que va a quedar, o ValueError diciendo por qué no."""
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("el título de una tarea no puede quedar vacío")
    limpio = valor.strip()
    if len(limpio) > LARGO_TITULO_TAREA:
        raise ValueError(
            f"el título de una tarea no puede pasar de {LARGO_TITULO_TAREA} "
            "caracteres")
    return limpio


# El suelo de la fecha. NO es una regla de negocio sobre hasta cuándo se puede
# cargar hacia atrás: es una malla contra un dígito del año que se resbaló.
#
# `date.fromisoformat` acepta tan contento "0026-09-04" y "1926-09-04", y una
# fila guardada en el año 26 no vuelve a aparecer en ninguna pantalla que
# alguien mire —el resumen va por mes, y ese mes nadie lo abre— así que el
# gasto se pierde EN SILENCIO, que es la familia de fallo que este panel
# combate. Los registros de Lucy empiezan en 2026; el suelo se deja seis años
# por debajo, deliberadamente flojo, para que ningún registro tardío legítimo
# lo toque y solo caiga el año mal tecleado.
PISO_FECHA = date(2020, 1, 1)


def vence_de_dia_valido(texto: str, piso: date = PISO_FECHA):
    """El campo «vence» del formulario → (aceptado, instante).

    Devuelve `(True, None)` cuando viene VACÍO: la fecha es opcional y la tarea
    nace sin fecha, que en este panel no es un hueco sino un grupo con nombre
    propio —«Sin fecha»—. Ese grupo existe porque las 4 tareas sin `vence_en`
    de producción llevaban meses invisibles; mandarlas a hoy por defecto sería
    volver a inventarles una fecha que nadie eligió.

    Devuelve `(False, None)` si el texto no es una fecha, o si cae por debajo
    de PISO_FECHA. El piso es el mismo que usa /efectivo y por el mismo motivo:
    no es una regla de negocio, es la malla contra un dígito del año que se
    resbaló.

    EL FUTURO SE ACEPTA, al revés que en /efectivo. Un gasto en efectivo es
    plata que ya salió; una tarea que vence el mes que viene es el caso normal
    de una tarea. Son la misma forma de campo y preguntas opuestas.

    LA HORA ES 23:59 EN SANTO DOMINGO, y las dos mitades de esa frase importan:

      · «23:59» porque el formulario pide un DÍA. «Vence el jueves» quiere decir
        que el jueves entero todavía sirve, así que el instante que representa
        ese día es su final y no su comienzo.
      · «en Santo Domingo» porque `tareas.vence_en` es TIMESTAMPTZ, o sea un
        instante, y el panel decide el grupo con `db.dia_rd`, que lo lee EN
        SANTO DOMINGO. Armado en UTC, el 23:59 del jueves se guardaría como un
        instante que en Santo Domingo todavía es del jueves a las 19:59 — pero
        el 00:00 de cualquier día armado en UTC cae en el día ANTERIOR en
        Santo Domingo. Una tarea escrita de noche nacería en el día equivocado.

    La zona sale de `config.TZ`, la misma que usa `db.dia_rd`: dos copias de una
    zona horaria se desincronizan igual que dos copias de cualquier otra cosa.
    """
    limpio = (texto or "").strip()
    if not limpio:
        return True, None
    try:
        dia = date.fromisoformat(limpio)
    except ValueError:
        return False, None
    if dia < piso:
        return False, None
    return True, datetime(dia.year, dia.month, dia.day, 23, 59,
                          tzinfo=_config.TZ)


def nombre_de_proyecto_que_vale(valor) -> str:
    """El nombre limpio que va a quedar, o `NombreDeProyectoNoVale`.

    LA PARTE QUE NO NECESITA LA BASE (por eso es la que entra en
    `crud.PUERTAS`, que es síncrona): sin los espacios de alrededor, no vacío y
    de a lo sumo `LARGO_NOMBRE_PROYECTO` caracteres. Que no haya OTRO proyecto
    vivo con ese nombre se mira aparte, con `proyecto_vivo_con_nombre`, porque
    pide la base.
    """
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        raise NombreDeProyectoNoVale(
            "vacio", "el nombre del proyecto no puede quedar vacío")
    if not isinstance(valor, str):
        raise NombreDeProyectoNoVale(
            "vacio", "el nombre del proyecto tiene que ser un texto")
    limpio = valor.strip()
    if len(limpio) > LARGO_NOMBRE_PROYECTO:
        raise NombreDeProyectoNoVale(
            "largo",
            f"el nombre del proyecto no puede pasar de {LARGO_NOMBRE_PROYECTO} caracteres")
    return limpio


# ── «DE QUÉ SE TRATA» (`proyectos.descripcion`, parte 3 del diseño de la página
# completa del proyecto, 8-oct-2026) ──────────────────────────────────────────
#
# UNA SOLA PUERTA para «qué texto vale como descripción» y UNA SOLA forma de
# dejarlo limpio, llamadas por TODO lo que escribe la columna por `crud.editar`
# (el panel `POST /proyectos/{pid}/descripcion`, Telegram `editar` y `perfil`, que
# agrega renglones con fecha) y por `crud.deshacer`. La puerta es
# `crud.PUERTAS["proyectos"]["descripcion"]`. `convertir_tarea_en_proyecto`
# copia el `detalle` de la tarea con su propio INSERT y NO pasa por acá (ver
# `tests/test_descripcion_de_proyecto.py` para por qué y con qué frontera).
#
# El tope es el MISMO que ya tienen el detalle de una tarea y un comentario
# (`LARGO_COMENTARIO`, `web/app.py::LARGO_DETALLE`): 2000. No sale de una medida de
# producción de descripciones (no se midió); sale de que es el texto libre más largo
# que la casa ya acepta en otra columna, y de la misma razón de ese tope: `TEXT` no
# tiene límite, y sin uno un POST a mano puede guardar megabytes que después hay que
# pintar y mandarle al modelo.
LARGO_DESCRIPCION_PROYECTO = 2000


class DescripcionDeProyectoNoVale(ValueError):
    """Una descripción que no puede quedar. `clave` dice por qué (`tipo`,
    `caracteres`, `largo`) para que una pantalla lo traduzca sin adivinar por el
    texto. El mensaje NUNCA repite el texto pedido."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def texto_de_descripcion(valor) -> str | None:
    """La descripción en su forma de siempre: saltos de línea `\\n` (el navegador
    manda `\\r\\n` desde un `textarea`), sin los espacios de alrededor, y `None`
    si queda vacía. NO mira el largo (`descripcion_de_proyecto_que_vale` lo
    hace): sirve también para leer lo que YA está guardado, sea cual sea su
    largo, y compararlo con lo que llega. `None` es «sin descripción»; algo que
    no es texto, o que lleva un NUL o un carácter que no se puede codificar
    (Postgres no guarda un NUL en un `TEXT`), no vale."""
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise DescripcionDeProyectoNoVale(
            "tipo", "la descripción del proyecto tiene que ser un texto")
    limpio = valor.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        limpio.encode("utf-8")
    except UnicodeEncodeError:
        raise DescripcionDeProyectoNoVale(
            "caracteres",
            "la descripción del proyecto lleva caracteres que no se pueden guardar") from None
    if "\x00" in limpio:
        raise DescripcionDeProyectoNoVale(
            "caracteres",
            "la descripción del proyecto lleva caracteres que no se pueden guardar")
    return limpio or None


def descripcion_de_proyecto_que_vale(valor) -> str | None:
    """La descripción limpia que va a quedar (`None` = sin descripción), o
    `DescripcionDeProyectoNoVale`. Es la parte que no necesita la base: por eso
    entra en `crud.PUERTAS`, que es síncrona."""
    limpio = texto_de_descripcion(valor)
    if limpio is not None and len(limpio) > LARGO_DESCRIPCION_PROYECTO:
        raise DescripcionDeProyectoNoVale(
            "largo",
            f"la descripción del proyecto no puede pasar de {LARGO_DESCRIPCION_PROYECTO} caracteres")
    return limpio


def huella_de_descripcion(valor) -> str:
    """Una huella de lo que dice AHORA la descripción (la de `texto_de_descripcion`,
    así que `\\r\\n` y los espacios de alrededor no la cambian). El formulario del
    panel la lleva escondida: al guardar, `crud.editar` la compara con lo que hay
    en ese momento, y si otro (Telegram agregó un renglón con `perfil`, otra
    sesión del panel) la cambió mientras se escribía, NO se guarda encima."""
    return hashlib.sha256((texto_de_descripcion(valor) or "").encode("utf-8")).hexdigest()


# ── LAS FECHAS DEL PROYECTO (`proyectos.inicio`, `entrega`, `termina_cuando`, parte 4 del
# diseño de la página completa del proyecto, 8-oct-2026) ──────────────────────────────
#
# UNA SOLA PUERTA para «qué vale como día» y UNA para «qué vale como "Termina cuando"»,
# llamadas por TODO lo que escribe esas columnas: `crud.editar` (el panel
# `POST /proyectos/{pid}/fechas` y Telegram `editar`, por `crud.PUERTAS`) y `crud.deshacer`.
# Al CREAR un proyecto escribe `inicio` solo `_con_su_inicio` (el día de hoy en Santo Domingo).
#
# Los días viajan como `date` (Postgres `DATE`) o como el texto `AAAA-MM-DD`; cualquier otra
# cosa no vale, y un `datetime` tampoco (un día no lleva hora: aceptarlo en silencio sería
# cortarle la hora a alguien que creía haberla dado).
DIA_MAS_VIEJO_DE_PROYECTO = date(2000, 1, 1)
DIA_MAS_NUEVO_DE_PROYECTO = date(2100, 12, 31)
# Cuánto puede medir «Termina cuando». Tope puesto por la construcción, SIN medir nada de
# producción (hoy ningún proyecto lo tiene): es una frase, así que mucho menos que la
# descripción (`LARGO_DESCRIPCION_PROYECTO`). Decide Tiziano si es otro.
LARGO_TERMINA_CUANDO = 300


class FechaDeProyectoNoVale(ValueError):
    """Un día de proyecto que no puede quedar. `clave`: `tipo` (no es un día), `formato` (texto
    que no es `AAAA-MM-DD` o un día que no existe), `rango` (fuera de `DIA_MAS_VIEJO_DE_PROYECTO`
    .. `DIA_MAS_NUEVO_DE_PROYECTO`), `vacio` (el inicio no se deja vacío) y `orden` (la entrega
    antes del inicio). El mensaje NUNCA repite lo que se pidió."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


class TerminaCuandoNoVale(ValueError):
    """Un «Termina cuando» que no puede quedar. `clave`: `tipo`, `caracteres`, `largo`. El
    mensaje NUNCA repite el texto pedido."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def dia_de_proyecto_que_vale(valor) -> date | None:
    """El día que va a quedar (`None` = sin fecha: `None` o un texto vacío), o
    `FechaDeProyectoNoVale`. Vale un `date` (no un `datetime`) o el texto `AAAA-MM-DD`
    (cifras ASCII; sin espacios de alrededor), entre `DIA_MAS_VIEJO_DE_PROYECTO` y
    `DIA_MAS_NUEVO_DE_PROYECTO`."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        raise FechaDeProyectoNoVale("tipo", "la fecha del proyecto tiene que ser un día, sin hora")
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return None
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", texto):
            raise FechaDeProyectoNoVale("formato", "la fecha del proyecto no se entiende")
        try:
            dia = date.fromisoformat(texto)
        except ValueError:
            raise FechaDeProyectoNoVale("formato", "la fecha del proyecto no se entiende") from None
    elif isinstance(valor, date):
        dia = valor
    else:
        raise FechaDeProyectoNoVale("tipo", "la fecha del proyecto tiene que ser un día")
    if not DIA_MAS_VIEJO_DE_PROYECTO <= dia <= DIA_MAS_NUEVO_DE_PROYECTO:
        raise FechaDeProyectoNoVale(
            "rango", f"la fecha del proyecto tiene que estar entre {DIA_MAS_VIEJO_DE_PROYECTO.year} "
                     f"y {DIA_MAS_NUEVO_DE_PROYECTO.year}")
    return dia


def inicio_de_proyecto_que_vale(valor) -> date:
    """`dia_de_proyecto_que_vale`, pero el inicio no se deja vacío (todo proyecto tiene el día
    en que arrancó: la migración se lo pone a los que ya existían y `_con_su_inicio` a los nuevos)."""
    dia = dia_de_proyecto_que_vale(valor)
    if dia is None:
        raise FechaDeProyectoNoVale("vacio", "el inicio del proyecto no puede quedar vacío")
    return dia


def termina_cuando_guardado(valor) -> str | None:
    """«Termina cuando» en su forma de siempre (saltos `\\n`, sin espacios de alrededor, `None` si
    queda vacío), SIN mirar el largo: sirve también para leer lo que YA está guardado y compararlo con
    lo que llega. Lo que no es texto, o lleva un NUL o un carácter que no se puede codificar, no vale
    (`TerminaCuandoNoVale`)."""
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise TerminaCuandoNoVale("tipo", "lo que dice «Termina cuando» tiene que ser un texto")
    limpio = valor.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        limpio.encode("utf-8")
    except UnicodeEncodeError:
        raise TerminaCuandoNoVale(
            "caracteres", "lo que dice «Termina cuando» lleva caracteres que no se pueden guardar") from None
    if "\x00" in limpio:
        raise TerminaCuandoNoVale(
            "caracteres", "lo que dice «Termina cuando» lleva caracteres que no se pueden guardar")
    return limpio or None


def termina_cuando_que_vale(valor) -> str | None:
    """El «Termina cuando» que va a quedar (`None` = sin escribir), o `TerminaCuandoNoVale`: el de
    `termina_cuando_guardado` y de a lo sumo `LARGO_TERMINA_CUANDO` caracteres."""
    limpio = termina_cuando_guardado(valor)
    if limpio is not None and len(limpio) > LARGO_TERMINA_CUANDO:
        raise TerminaCuandoNoVale(
            "largo", f"«Termina cuando» no puede pasar de {LARGO_TERMINA_CUANDO} caracteres")
    return limpio


def entrega_cuadra_con_inicio(inicio, entrega) -> bool:
    """¿La entrega NO es anterior al inicio? Sin uno de los dos días, no hay con qué comparar y
    cuadra. Los dos ya limpios (`date` o `None`). Es la regla de `crud.editar` y de
    `crud.deshacer`; la base la repite en `proyectos_entrega_despues_del_inicio`."""
    return inicio is None or entrega is None or entrega >= inicio


def frase_de_entrega(entrega, hoy: date) -> tuple[str, bool] | None:
    """Lo que dice «Cómo va» de la entrega, y si va en rojo: `(frase, vencida)`, o `None` sin
    fecha de entrega. Los días se cuentan en Santo Domingo (`hoy` sale de `hoy_rd`): el día
    de la entrega es «hoy» hasta que acaba, y pasado ese día dice «venció», nunca un número
    negativo. Quien llama decide además si el proyecto está cerrado (`armar_pagina`)."""
    if not isinstance(entrega, date) or isinstance(entrega, datetime):
        return None
    faltan = (entrega - hoy).days
    if faltan < 0:
        return "La entrega venció", True
    if faltan == 0:
        return "La entrega es hoy", False
    return (f"{faltan} día para la entrega" if faltan == 1 else f"{faltan} días para la entrega"), False


async def fechas_de_proyectos() -> dict[int, dict] | None:
    """`{proyecto_id: {inicio, entrega, termina_cuando}}` de los proyectos VIVOS, o `None` si las
    columnas todavía no existen (la migración `2026-10-08_proyectos_fechas.sql` sin aplicar,
    SQLSTATE 42703): entonces la página no dibuja fechas ni el enlace para cambiarlas. UNA LECTURA
    APARTE, con su propia conexión (un error 42703 deja abortada la transacción de la conexión
    donde ocurre), igual que `graves_de`; no es un cuarto nivel de la consulta de la página."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT id, inicio, entrega, termina_cuando FROM proyectos WHERE borrado_en IS NULL")
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return None
            raise
    return {f["id"]: {"inicio": f["inicio"], "entrega": f["entrega"],
                      "termina_cuando": f["termina_cuando"]} for f in filas}


def motivo_de_deshacer(log_id: int, accion: str) -> str:
    """El `motivo` de la huella `deshacer` (la escribe `crud.deshacer`): dice QUÉ huella se deshizo.
    `id_que_deshizo` lo lee de vuelta; una sola fuente para las dos."""
    return f"Tiziano deshizo la acción #{log_id} ({accion})"


def id_que_deshizo(motivo) -> int | None:
    """El id de la huella que deshizo una huella `deshacer`, o `None` si el motivo no tiene esa forma."""
    visto = re.match(r"Tiziano deshizo la acción #(\d+) \(", motivo or "")
    return int(visto.group(1)) if visto else None


def cierres_desde_las_huellas(filas) -> dict[int, datetime]:
    """LA REGLA ÚNICA de `cierres_de_proyectos`, sobre filas ya leídas: para cada proyecto se recorren
    sus huellas `editar` y `deshacer` en orden (`ts`, luego `id`) llevando el ESTADO en que quedó:
    * `editar`: el estado pasa a `despues_estado`. Es cierre si antes NO era `cerrado` (el `antes_estado`
      de la propia huella; si no trae estado cuenta como «no lo era») y ahora SÍ.
    * `deshacer` de un `editar` de ESE proyecto: el estado vuelve a lo que ese `editar` tenía en
      `antes_estado` (si el `editar` no cambió el estado, queda como estaba). Es cierre si antes no era
      `cerrado` y ahora sí. Un `deshacer` de un `borrar` o un `crear` no toca el estado.
    * Cualquier huella que no deja saber el estado (un `deshacer` cuyo motivo no apunta a un `editar` de
      este proyecto, o cuyo `editar` no trae estado, o un `editar` sin `despues.estado`) deja el estado
      DESCONOCIDO y borra la fecha: la página dice «Cerrado» sin día antes que inventar uno. Un cierre
      posterior, que sí se sabe, la vuelve a dar.
    Cada fila: `{id, pid, accion, ts, motivo, antes_estado, despues_estado}`. La fecha es la del ÚLTIMO
    cierre. Un proyecto sin cierre conocido, o que hoy no está cerrado, no sale."""
    cerrado = ESTADO_PROYECTO_CERRADO
    desconocido = object()
    por_id = {f["id"]: f for f in filas}
    por_proyecto: dict[int, list] = {}
    for f in filas:
        por_proyecto.setdefault(f["pid"], []).append(f)
    cierres: dict[int, datetime] = {}
    for pid, huellas in por_proyecto.items():
        estado, fecha = desconocido, None
        for f in sorted(huellas, key=lambda h: (h["ts"], h["id"])):
            if f["accion"] == "editar":
                previo = f["antes_estado"]          # sin estado en el `antes`: no estaba cerrado
                estado = f["despues_estado"] if f["despues_estado"] is not None else desconocido
            elif f["accion"] == "deshacer":
                previo = estado
                u = por_id.get(id_que_deshizo(f["motivo"]))
                if u is None or u["pid"] != pid:
                    estado = desconocido
                elif u["accion"] == "editar":
                    if u["antes_estado"] is None:
                        estado = desconocido
                    elif u["antes_estado"] != u["despues_estado"]:
                        estado = u["antes_estado"]
                elif u["accion"] not in ("borrar", "crear"):
                    estado = desconocido
            else:
                continue                             # `borrar` y `crear` solo sirven de blanco de un `deshacer`
            if estado is desconocido:
                fecha = None
            elif estado == cerrado and previo is not desconocido and previo != cerrado:
                fecha = f["ts"]
            elif estado != cerrado:
                fecha = None                         # reabierto: ya no hay fecha que enseñar
        if fecha is not None:
            cierres[pid] = fecha
    return cierres


async def cierres_de_proyectos() -> dict[int, datetime]:
    """`{proyecto_id: instante}` del ÚLTIMO MOMENTO EN QUE CADA PROYECTO PASÓ A CERRADO (Archivar y
    borrar, pieza 2, 18-oct-2026). SOLO LEE; lee las huellas `editar` y `deshacer` de `proyectos`
    en `log_acciones` y decide con `cierres_desde_las_huellas` (la regla, dicha ahí). «Archivado» es
    «cerrado» (no hay estado nuevo) y no hay columna con la fecha. Solo hay DOS escritores del
    `estado` de un proyecto (los dos genéricos de `acciones/crud.py`; el panel, Telegram y los botones
    pasan por `crud.editar`). `crud.editar` guarda la fila entera en `antes` y `despues`; `crud.deshacer`
    deja `antes` y `despues` vacíos y en su `motivo` dice qué huella deshizo (`id_que_deshizo`).
    Un proyecto sin huella (nació cerrado) o de estado no deducible no sale: la página no inventa fecha."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, registro_id AS pid, accion, ts, motivo,
                   antes->>'estado' AS antes_estado, despues->>'estado' AS despues_estado
              FROM log_acciones
             WHERE tabla = 'proyectos' AND accion IN ('editar', 'deshacer', 'borrar', 'crear')
            """)
        filas = await cur.fetchall()
    return cierres_desde_las_huellas(filas)


async def _con_su_inicio(conn, cur, fila: dict) -> dict:
    """Un proyecto recién insertado (`fila`, con `RETURNING *`) con su `inicio` puesto a HOY en
    Santo Domingo (`hoy_rd`), y la fila releída para que la huella `crear` lo diga. LA ÚNICA que
    escribe `inicio` al crear: la llaman los DOS sitios que insertan un proyecto
    (`crear_proyecto` y `convertir_tarea_en_proyecto`), dentro de la misma
    transacción que el `INSERT`. Con la columna sin migrar (SQLSTATE 42703) no escribe nada y
    devuelve la fila como vino (la migración le pone el día de creación): crear no se rompe.
    Va en un SAVEPOINT para que ese error no deje abortada la transacción del `INSERT`."""
    try:
        async with conn.transaction():
            await cur.execute(
                "UPDATE proyectos SET inicio = %s WHERE id = %s", (hoy_rd(), fila["id"]))
    except Exception as e:
        try:
            sqlstate = e.sqlstate
        except AttributeError:
            raise e from None
        if sqlstate == "42703":
            return fila
        raise
    await cur.execute("SELECT * FROM proyectos WHERE id = %s", (fila["id"],))
    return await cur.fetchone()


# ── LA CARPETA DEL PROYECTO (`proyectos.carpeta`, parte 5 del diseño de la página completa del
# proyecto, 8-oct-2026) ─────────────────────────────────────────────────────────────────
#
# UN SOLO DATO por proyecto: dónde vive el proyecto, escrito por quien lo lleva. Puede ser la
# dirección de una carpeta de Drive (`https://…`) o la ruta de una carpeta de una computadora
# (`/Users/…`, `C:\…`, `smb://…`): la columna guarda texto y no sabe cuál es.
#
# DOS PUERTAS, y no se mezclan:
#  · ESCRIBIR: `carpeta_de_proyecto_que_vale` (la llama `crud.PUERTAS["proyectos"]["carpeta"]`, o sea
#    el panel, Telegram `editar` y `deshacer`): texto sin caracteres de control (categoría Unicode `Cc`:
#    NUL, `\n`, `\r`, tabulador…; los separadores U+2028 y U+2029 SÍ pasan), sin espacios de alrededor, a lo
#    sumo `LARGO_CARPETA_PROYECTO`. NO decide si es un enlace: guarda cualquier texto que sea
#    una ruta o una dirección.
#  · PINTAR UN ENLACE: `enlace_de_carpeta` (la plantilla la llama como filtro, ver `web/app.py`). Es
#    LA ÚNICA que decide si el valor llega a un `href`, MIRANDO EL VALOR y no cómo se escribió: solo
#    una dirección `http://` o `https://` completa, sin espacios ni caracteres de control ni
#    comillas, con un servidor. Lo que no se pueda clasificar con certeza cae del lado estricto: sale
#    como texto escapado, nunca como enlace.
# Cuánto puede medir. Tope puesto por la construcción, SIN medir nada de producción (hoy ningún
# proyecto tiene carpeta). Decide Tiziano si es otro.
LARGO_CARPETA_PROYECTO = 500


class CarpetaDeProyectoNoVale(ValueError):
    """Una carpeta que no puede quedar. `clave`: `tipo` (no es un texto), `caracteres` (lleva un NUL,
    un salto de línea u otro carácter de control, o uno que no se puede guardar) y `largo`. El
    mensaje NUNCA repite lo que se pidió."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def carpeta_guardada(valor) -> str | None:
    """La carpeta en su forma de siempre (sin espacios de alrededor, `None` si queda vacía), SIN mirar
    el largo: sirve también para leer lo que YA está guardado y compararlo con lo que llega. Lo que no
    es texto, o lleva un carácter de control (también un salto de línea en medio: es una ruta o una
    dirección) o uno que no se puede codificar, no vale (`CarpetaDeProyectoNoVale`). «Carácter de control» es
    la categoría Unicode `Cc` (NUL, `\\n`, `\\r`, tabulador…): los separadores de línea U+2028 y U+2029 (categorías
    `Zl` y `Zp`) NO lo son y pasan esta puerta."""
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise CarpetaDeProyectoNoVale("tipo", "la carpeta del proyecto tiene que ser un texto")
    limpio = valor.strip()
    try:
        limpio.encode("utf-8")
    except UnicodeEncodeError:
        raise CarpetaDeProyectoNoVale(
            "caracteres", "la carpeta del proyecto lleva caracteres que no se pueden guardar") from None
    if any(unicodedata.category(c) == "Cc" for c in limpio):
        raise CarpetaDeProyectoNoVale(
            "caracteres", "la carpeta del proyecto lleva caracteres que no se pueden guardar")
    return limpio or None


def carpeta_de_proyecto_que_vale(valor) -> str | None:
    """La carpeta que va a quedar (`None` = sin carpeta), o `CarpetaDeProyectoNoVale`: la de
    `carpeta_guardada` y de a lo sumo `LARGO_CARPETA_PROYECTO` caracteres."""
    limpio = carpeta_guardada(valor)
    if limpio is not None and len(limpio) > LARGO_CARPETA_PROYECTO:
        raise CarpetaDeProyectoNoVale(
            "largo", f"la carpeta del proyecto no puede pasar de {LARGO_CARPETA_PROYECTO} caracteres")
    return limpio


# Lo que no se deja pasar a un `href` aunque el escape de la plantilla lo cubriría: comillas, ángulos,
# acento grave y barra invertida (un navegador la lee como `/` en una dirección `http`).
_NO_EN_UN_ENLACE = frozenset("\"'<>`\\")


def enlace_de_carpeta(valor) -> str | None:
    """LA PUERTA DEL `href`: el valor tal cual si es una dirección que un navegador abre como `http` o
    `https`, y `None` en todo otro caso (entonces se pinta como texto). Decide mirando el valor:

      · tiene que empezar, sin nada delante (ni espacio ni carácter de control ni invisible), por
        `http://` o `https://` (mayúsculas o minúsculas);
      · ni un carácter de las categorías C* (control, formato, sin asignar…) o Z* (espacios) de
        Unicode, ni comillas, `<`, `>`, acento grave o barra invertida, en ninguna parte;
      · `urlsplit` tiene que entenderlo como `http`/`https` con un servidor y un puerto que valga;
      · y no pasa de `LARGO_CARPETA_PROYECTO` (lo que la puerta de escribir no habría dejado).

    Lo que no es texto, o no cumple cualquiera de esas cosas, o hace fallar al analizador: `None`.
    No arregla nada ni recorta: un valor que se arregla solo es un valor que nadie vio."""
    if not isinstance(valor, str) or not valor or len(valor) > LARGO_CARPETA_PROYECTO:
        return None
    if re.match(r"https?://", valor, re.IGNORECASE) is None:
        return None
    for c in valor:
        if c in _NO_EN_UN_ENLACE or unicodedata.category(c)[0] in ("C", "Z"):
            return None
    try:
        partes = urlsplit(valor)
        servidor = partes.hostname
        partes.port                                              # noqa: B018 -- lanza si no vale
    except ValueError:
        return None
    if partes.scheme not in ("http", "https") or not partes.netloc or not servidor:
        return None
    return valor


async def carpetas_de_proyectos() -> dict[int, str | None] | None:
    """`{proyecto_id: carpeta}` de los proyectos VIVOS, o `None` si la columna todavía no existe (la
    migración `2026-10-08_proyectos_carpeta.sql` sin aplicar, SQLSTATE 42703): entonces la página no
    dibuja el bloque de la carpeta. UNA LECTURA APARTE con su propia conexión, igual que
    `fechas_de_proyectos` (y por la misma razón: un 42703 deja abortada la transacción de la conexión
    donde ocurre; y aparte de las fechas para que una base con la migración de las fechas y sin esta
    siga enseñando las fechas)."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute("SELECT id, carpeta FROM proyectos WHERE borrado_en IS NULL")
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return None
            raise
    return {f["id"]: f["carpeta"] for f in filas}


async def carpetas_de_audio() -> dict[int, str | None] | None:
    """`{proyecto_id: carpeta_audio}` de los proyectos VIVOS, o `None` si la columna todavía no existe (la
    migración `2026-10-18_proyectos_carpeta_audio.sql` sin aplicar, SQLSTATE 42703): entonces la página no
    dibuja la línea «Audio (disco Sesiones)». UNA LECTURA APARTE con su propia conexión y NO dentro de
    `carpetas_de_proyectos`: una base con la columna `carpeta` y sin `carpeta_audio` tiene que seguir
    enseñando la carpeta de Drive (y un 42703 deja abortada la transacción de la conexión donde ocurre).
    Lo que devuelve es lo que alguien escribió: Lucy no ve el disco Sesiones y aquí no se comprueba nada de él."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute("SELECT id, carpeta_audio FROM proyectos WHERE borrado_en IS NULL")
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return None
            raise
    return {f["id"]: f["carpeta_audio"] for f in filas}


# ── LAS NOTAS Y DECISIONES DEL PROYECTO (`notas.proyecto_id`, parte 6 del diseño de la página completa
# del proyecto, 8-oct-2026) ─────────────────────────────────────────────────────────────────
#
# SE REUSA `notas`, no una tabla nueva: lo que se le dicte a Lucy por Telegram para un proyecto
# (`crud.crear_desde_interpretacion`, que llena `notas.proyecto_id` cuando el mensaje nombra un
# proyecto vivo) sale en la página sin hacer nada más, y viceversa.
#
# QUIÉN ESCRIBE `notas` (sonda sobre el código, 8-oct-2026): `crud.crear_desde_interpretacion` (Telegram,
# el único `INSERT INTO notas` escrito a mano que había), los escritores genéricos `crud.editar`,
# `crud.borrar` y `crud.deshacer` (arman el SQL con la tabla al vuelo) y, desde esta parte,
# `crear_nota_de_proyecto`, `editar_nota_de_proyecto` y `borrar_nota_de_proyecto`. QUIÉN LA LEE: la página
# (`notas_de_proyectos`), Lucy por Telegram (`cerebro/consultar.py`, SQL que arma el modelo), la
# lectura de lo de Tiziano para Code (`db/lectura_dueno.py`, solo las que tienen `bandeja_id` suyo) y
# el respaldo (`db/backup.py`). Ninguno de esos lectores cambia con esta parte.
#
# EL AUTOR sale de la sesión del panel (`autor_chat_id`), nunca de un campo del formulario. Una nota que
# no escribió el panel (Telegram) dice quién la mandó por su `bandeja` (`autor_de_nota`), y una sin
# ninguna de las dos cosas no tiene autor conocido: la página lo dice, no inventa un nombre.
#
# QUIÉN LA TOCA (editar, borrar): cualquiera que entre al panel, igual que los comentarios de las tareas
# (`borrar_comentario`, `editar_comentario`). La nota dice quién la escribió y eso no se cambia; no hay una
# regla de «solo el autor» (por Telegram, `crud.editar` tampoco la tendría).
#
# EL TOPE de una nota es el mismo de los demás textos libres de la casa (`LARGO_COMENTARIO`,
# `LARGO_DESCRIPCION_PROYECTO`): 2000. No sale de medir notas de producción (no se midió): sale de que
# `TEXT` no tiene límite y sin uno un POST a mano puede guardar megabytes que después hay que pintar.
# Una nota de Telegram no tiene tope (`crud.crear_desde_interpretacion`): una más larga se ve entera
# y editarla desde el panel se rechaza por el tope hasta recortarla.
LARGO_NOTA_PROYECTO = 2000


class NotaNoVale(ValueError):
    """Un texto de nota que no puede quedar. `clave`: `tipo` (no es texto), `vacio`, `largo`, `caracteres`
    (un NUL, o algo que no se puede codificar). El mensaje NUNCA repite lo que se pidió."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


class NotaNoEsta(LookupError):
    """La nota (o el proyecto) no existe, está borrada, o es de OTRO proyecto que el de la ruta."""


class NotaSinSesion(PermissionError):
    """Quien lo pide no entra al panel: no se escribe nada. Las rutas ya cortan con 401 antes; esto es la
    puerta de las funciones de `db` cuando alguien las llama directo."""


class NotaCambio(RuntimeError):
    """La nota cambió entre que se leyó y se escribió: NO se escribió nada."""


class NotasSinColumna(RuntimeError):
    """La base todavía no tiene `notas.autor_chat_id` (migración sin aplicar): NO se escribió nada."""


def texto_de_nota_que_vale(crudo) -> str:
    """El cuadro de texto → la nota a guardar, o `NotaNoVale`. Los saltos de línea del navegador
    (`\\r\\n`) se guardan como `\\n`; sin espacios de alrededor; no vacía, de a lo sumo
    `LARGO_NOTA_PROYECTO`, sin NUL ni caracteres que no se puedan codificar."""
    if not isinstance(crudo, str):
        raise NotaNoVale("tipo", "la nota tiene que ser un texto")
    limpio = crudo.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        limpio.encode("utf-8")
    except UnicodeEncodeError:
        raise NotaNoVale("caracteres", "la nota lleva caracteres que no se pueden guardar") from None
    if "\x00" in limpio:
        raise NotaNoVale("caracteres", "la nota lleva caracteres que no se pueden guardar")
    if not limpio:
        raise NotaNoVale("vacio", "la nota no puede quedar vacía")
    if len(limpio) > LARGO_NOTA_PROYECTO:
        raise NotaNoVale("largo", f"la nota no puede pasar de {LARGO_NOTA_PROYECTO} caracteres")
    return limpio


def autor_de_nota(fila: dict) -> int | None:
    """El chat de quien escribió la nota, o `None` si no se sabe. `fila` trae `autor_chat_id` (el panel) y,
    de su `bandeja`, `bandeja_chat_id` y `bandeja_origen`. Una nota del panel dice su autor; una de
    Telegram, el chat que mandó el mensaje (solo si la bandeja es de origen `telegram`: en otro origen el
    `chat_id` es a quién iba dirigido el aviso o el dueño por omisión, no quién escribió el texto)."""
    propio = fila.get("autor_chat_id")
    if propio is not None:
        return propio
    if fila.get("bandeja_origen") == "telegram":
        return fila.get("bandeja_chat_id")
    return None


def _columna_ausente(e: Exception) -> bool:
    try:
        return e.sqlstate == "42703"
    except AttributeError:
        return False


async def notas_de_proyectos() -> dict[int, list[dict]] | None:
    """`{proyecto_id: [fila, ...]}` de las notas VIVAS de proyectos VIVOS, la más nueva primero, o `None`
    si la columna `notas.autor_chat_id` todavía no existe (la migración `2026-10-08_notas_autor.sql` sin
    aplicar, SQLSTATE 42703): entonces la página no dibuja el bloque de notas. UNA LECTURA APARTE con su
    propia conexión, igual que `fechas_de_proyectos` y `carpetas_de_proyectos` (un 42703 deja abortada la
    transacción de la conexión donde ocurre). Cada fila trae `autor_chat_id` y lo de su bandeja para
    `autor_de_nota`; un proyecto sin notas no sale en el diccionario."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT n.id, n.proyecto_id, n.creado_en, n.contenido, n.autor_chat_id, "
                "       b.chat_id AS bandeja_chat_id, b.origen AS bandeja_origen "
                "  FROM notas n "
                "  JOIN proyectos p ON p.id = n.proyecto_id "
                "  LEFT JOIN bandeja b ON b.id = n.bandeja_id "
                " WHERE n.borrado_en IS NULL AND p.borrado_en IS NULL "
                " ORDER BY n.creado_en DESC, n.id DESC")
            filas = await cur.fetchall()
        except Exception as e:
            if _columna_ausente(e):
                return None
            raise
    salida: dict[int, list[dict]] = {}
    for f in filas:
        salida.setdefault(f["proyecto_id"], []).append(dict(f))
    return salida


async def _nota_viva_de(cur, nota_id: int, proyecto_id: int) -> dict:
    """La fila entera (lo que va a la huella) de la nota VIVA `nota_id` que es de ESE proyecto VIVO, o
    `NotaNoEsta`. Sin la columna `autor_chat_id`: `NotasSinColumna`."""
    await cur.execute(
        "SELECT n.* FROM notas n JOIN proyectos p ON p.id = n.proyecto_id "
        "WHERE n.id = %s AND n.proyecto_id = %s AND n.borrado_en IS NULL AND p.borrado_en IS NULL",
        (nota_id, proyecto_id))
    fila = await cur.fetchone()
    if fila is None:
        raise NotaNoEsta("esa nota no está en ese proyecto")
    fila = dict(fila)
    if "autor_chat_id" not in fila:
        raise NotasSinColumna("notas.autor_chat_id no existe todavía")
    return fila


async def _huella_de_nota(conn, accion: str, registro_id: int, antes, despues, motivo: str, bandeja_id) -> None:
    await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES ('panel', %s, 'notas', %s, %s, %s, %s, %s)
        """,
        (accion, registro_id,
         json.dumps(antes, default=str, ensure_ascii=False) if antes else None,
         json.dumps(despues, default=str, ensure_ascii=False) if despues else None,
         motivo, bandeja_id))


async def crear_nota_de_proyecto(proyecto_id: int, autor_chat_id: int, texto) -> int:
    """Escribe UNA nota en un proyecto VIVO y devuelve su id. `autor_chat_id` es el chat de la SESIÓN del
    panel (la ruta lo saca de la cookie, nunca de un campo). `NotaNoVale` si el texto no vale,
    `NotaSinSesion` si ese chat no entra al panel, `NotaNoEsta` si el proyecto no existe o está en la
    papelera, `NotasSinColumna` si falta la migración. La fila y su huella `crear` (actor `panel`) van en
    la misma transacción. Un proyecto cerrado SÍ recibe notas (Telegram ya las pone en cualquier
    proyecto vivo)."""
    from web.auth import puede_entrar

    limpio = texto_de_nota_que_vale(texto)
    if autor_chat_id is None or not puede_entrar(autor_chat_id):
        raise NotaSinSesion("quien escribe no entra al panel")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        if await cur.fetchone() is None:
            raise NotaNoEsta("ese proyecto no está")
        try:
            await cur.execute(
                "INSERT INTO notas (contenido, proyecto_id, autor_chat_id) VALUES (%s, %s, %s) RETURNING *",
                (limpio, proyecto_id, autor_chat_id))
        except Exception as e:
            if _columna_ausente(e):
                raise NotasSinColumna("notas.autor_chat_id no existe todavía") from e
            raise
        fila = dict(await cur.fetchone())
        await _huella_de_nota(conn, "crear", fila["id"], None, fila,
                              "nota escrita desde el panel de proyectos", None)
        return fila["id"]


async def editar_nota_de_proyecto(nota_id: int, proyecto_id: int, chat_id: int, texto) -> bool:
    """Cambia el texto de UNA nota de ESE proyecto. `True` si cambió, `False` si el texto limpio ya era
    el que había (sin escribir ni dejar huella). `NotaNoVale`, `NotaNoEsta` (la nota no está, está
    borrada o es de OTRO proyecto, o de ninguno), `NotaSinSesion` (no entra al panel), `NotaCambio` (otro la
    cambió en el medio: el `UPDATE` solo escribe si el texto sigue siendo el leído, para que el `antes`
    de la huella sea verdad) y `NotasSinColumna`. Huella `editar` con la fila de antes y la de después en
    la misma transacción: `crud.deshacer` la revierte."""
    from web.auth import puede_entrar

    nuevo = texto_de_nota_que_vale(texto)
    if chat_id is None or not puede_entrar(chat_id):
        raise NotaSinSesion("quien escribe no entra al panel")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        antes = await _nota_viva_de(cur, nota_id, proyecto_id)
        if antes["contenido"] == nuevo:
            return False
        escrita = await conn.execute(
            "UPDATE notas SET contenido = %s "
            "WHERE id = %s AND proyecto_id = %s AND borrado_en IS NULL AND contenido = %s",
            (nuevo, nota_id, proyecto_id, antes["contenido"]))
        if escrita.rowcount == 0:
            raise NotaCambio("la nota cambió mientras se editaba")
        await cur.execute("SELECT * FROM notas WHERE id = %s", (nota_id,))
        despues = dict(await cur.fetchone())
        await _huella_de_nota(conn, "editar", nota_id, antes, despues,
                              "nota editada desde el panel de proyectos", antes.get("bandeja_id"))
        return True


async def borrar_nota_de_proyecto(nota_id: int, proyecto_id: int, chat_id: int) -> None:
    """Borra (marca `borrado_en`) UNA nota de ESE proyecto; nunca un `DELETE`. Las mismas negativas que
    `editar_nota_de_proyecto` (`NotaNoEsta`, `NotaSinSesion`, `NotasSinColumna`). Huella `borrar` con la fila
    entera de antes, como `crud.borrar`: `crud.deshacer` la devuelve."""
    from web.auth import puede_entrar

    if chat_id is None or not puede_entrar(chat_id):
        raise NotaSinSesion("quien escribe no entra al panel")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        antes = await _nota_viva_de(cur, nota_id, proyecto_id)
        escrita = await conn.execute(
            "UPDATE notas SET borrado_en = now() "
            "WHERE id = %s AND proyecto_id = %s AND borrado_en IS NULL", (nota_id, proyecto_id))
        if escrita.rowcount == 0:
            raise NotaNoEsta("la nota ya no está")
        await _huella_de_nota(conn, "borrar", nota_id, antes, None,
                              "nota borrada desde el panel de proyectos", antes.get("bandeja_id"))


# ── LAS SESIONES QUE SE QUITAN DE UN PROYECTO, SE AGREGAN Y SE DEVUELVEN (`sesiones_de_proyecto`,
# partes 11 y 13 del diseño de la página completa del proyecto, 9-oct-2026) ──────────────────
#
# LA DECISIÓN DE LA CASA, no el dato de la sesión. Lucy le pregunta al registro las sesiones del
# cliente de un proyecto (`registro_lectura.sesiones_de_cliente`, parte 9) y lo único que guarda es
# lo que la casa decidió a mano sobre esa lista: `quitada` (parte 11) o `agregada` (parte 13). El
# dato de la sesión —fecha, sala, horas, concepto y el dinero— no se copia: lo sigue diciendo la
# App en cada lectura, y por eso lo que cambia allá se ve acá la próxima vez que se abra el
# proyecto. Tampoco se le manda nada: `registro_lectura` solo sabe GET.
#
# EL IDENTIFICADOR QUE SE GUARDA es el `ref` que la App devolvió para esa sesión, pasado a texto
# (`ref_de_sesion_que_vale`), nunca el código de 4 letras (según el mapa de la App, no se sabe que
# no se repita) ni nada que venga de un formulario: las rutas de quitar y de agregar le vuelven a
# preguntar a la App y guardan lo que esa lectura devolvió para esa sesión, así que una que la App
# no devuelve no se puede quitar ni agregar. Sacar (devolver una quitada, o sacar una agregada) sí
# usa el `ref` guardado, porque deshace una decisión que la casa ya tomó.
#
# Una decisión viva por proyecto y sesión (el índice único parcial de la tabla): quitar dos veces
# lo mismo no escribe ni deja huella la segunda, y sacar una decisión le pone `borrado_en` sin
# borrar la fila, así que se puede volver a decidir sobre esa sesión después. Por eso una sesión no
# puede quedar a la vez quitada y agregada.
LARGO_REF_SESION = 200
# El código que la sesión tenía al decidir: uno que no quepa acá no es un código, así que se guarda
# sin él (no se recorta ni se inventa). Se pinta en el aviso de una agregada que la App ya no
# devuelve o devolvió cancelada (parte 13).
LARGO_CODIGO_SESION = 40


class RefDeSesionNoVale(ValueError):
    """Un identificador de sesión que no puede quedar. `clave`: `tipo` (no es un texto ni un
    número), `vacio`, `caracteres` (un carácter de control) o `largo`. El mensaje NUNCA repite lo
    que se pidió."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


class SesionNoEsta(LookupError):
    """Esa sesión no está quitada de ese proyecto (o ese proyecto no existe o está en la papelera)."""


class SesionSinSesion(PermissionError):
    """Quien lo pide no entra al panel: no se escribe nada."""


class SesionesSinTabla(RuntimeError):
    """La base todavía no tiene `sesiones_de_proyecto` (migración sin aplicar): NO se escribió nada."""


def ref_de_sesion_que_vale(valor) -> str:
    """El identificador de una sesión del registro en texto, o `RefDeSesionNoVale`. Un número
    entero vale (la App manda el Id como número); `True` no (es un número de mentira). Sin espacios
    de alrededor, no vacío, sin caracteres de control y de a lo sumo `LARGO_REF_SESION`."""
    if isinstance(valor, bool):
        raise RefDeSesionNoVale("tipo", "ese identificador de sesión no vale")
    if isinstance(valor, int):
        return str(valor)
    if not isinstance(valor, str):
        raise RefDeSesionNoVale("tipo", "ese identificador de sesión no vale")
    limpio = valor.strip()
    if not limpio:
        raise RefDeSesionNoVale("vacio", "ese identificador de sesión no vale")
    if any(unicodedata.category(c) == "Cc" for c in limpio):
        raise RefDeSesionNoVale("caracteres", "ese identificador de sesión no vale")
    if len(limpio) > LARGO_REF_SESION:
        raise RefDeSesionNoVale(
            "largo", f"ese identificador de sesión no puede pasar de {LARGO_REF_SESION} caracteres")
    return limpio


def _codigo_de_sesion_guardable(codigo) -> str | None:
    """El código que la sesión tenía al decidir, o `None`. Lo que no sea un texto que quepa en
    `LARGO_CODIGO_SESION` se guarda sin código. Se pinta en el aviso de una agregada que la App ya
    no devuelve o está cancelada."""
    if not isinstance(codigo, str):
        return None
    limpio = codigo.strip()
    return limpio if limpio and len(limpio) <= LARGO_CODIGO_SESION else None


def _tabla_ausente(e: Exception) -> bool:
    try:
        return e.sqlstate == "42P01"
    except AttributeError:
        return False


async def _mapa_de_decisiones_de_sesiones(modo: str) -> dict[int, dict[str, str | None]] | None:
    """`{proyecto_id: {sesion_ref: codigo}}` de las decisiones vivas de ese `modo` sobre los
    proyectos vivos, la más nueva primero, o `None` si la tabla todavía no existe (SQLSTATE 42P01):
    entonces la página carga como hoy y sin ningún control. Lectura aparte con su propia conexión,
    igual que `notas_de_proyectos` (un error deja abortada la transacción donde ocurre). El código
    puede ser `None` (la App no lo mandó al decidir)."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT s.proyecto_id, s.sesion_ref, s.codigo "
                "  FROM sesiones_de_proyecto s JOIN proyectos p ON p.id = s.proyecto_id "
                " WHERE s.modo = %s AND s.borrado_en IS NULL AND p.borrado_en IS NULL "
                " ORDER BY s.creado_en DESC, s.id DESC", (modo,))
            filas = await cur.fetchall()
        except Exception as e:
            if _tabla_ausente(e):
                return None
            raise
    salida: dict[int, dict[str, str | None]] = {}
    for f in filas:
        salida.setdefault(f["proyecto_id"], {})[f["sesion_ref"]] = f["codigo"]
    return salida


async def sesiones_quitadas_de_proyectos() -> dict[int, dict[str, str | None]] | None:
    """Los `ref` de las sesiones quitadas de cada proyecto, con su código, o `None` si la tabla
    todavía no existe (ver `_mapa_de_decisiones_de_sesiones`)."""
    return await _mapa_de_decisiones_de_sesiones("quitada")


async def sesiones_agregadas_de_proyectos() -> dict[int, dict[str, str | None]] | None:
    """Los `ref` de las sesiones que la casa agregó a mano a cada proyecto, con su código (parte
    13), o `None` si la tabla todavía no existe. Con esos `ref` la página le pide al registro, en
    la misma llamada de las del cliente, las que devuelve."""
    return await _mapa_de_decisiones_de_sesiones("agregada")


async def ficha_de_cliente_de_proyecto(proyecto_id: int) -> int | None:
    """El Id de la ficha de Noco del cliente de un proyecto VIVO, o `None` si no tiene cliente.
    `SesionNoEsta` si el proyecto no existe o está en la papelera. Es con lo que la ruta de quitar
    le vuelve a preguntar al registro por las sesiones de ese cliente."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT cliente_noco_id FROM proyectos WHERE id = %s AND borrado_en IS NULL",
            (proyecto_id,))
        fila = await cur.fetchone()
    if fila is None:
        raise SesionNoEsta("ese proyecto no está")
    return fila["cliente_noco_id"]


async def _huella_de_sesion(conn, accion: str, registro_id: int, antes, despues, motivo: str) -> None:
    await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES ('panel', %s, 'sesiones_de_proyecto', %s, %s, %s, %s, NULL)
        """,
        (accion, registro_id,
         json.dumps(antes, default=str, ensure_ascii=False) if antes else None,
         json.dumps(despues, default=str, ensure_ascii=False) if despues else None,
         motivo))


async def quitar_sesion_de_proyecto(proyecto_id: int, sesion_ref, codigo, chat_id: int) -> bool:
    """Quita una sesión de la lista de ESE proyecto VIVO: guarda la decisión (`modo='quitada'`).
    `True` si se guardó, `False` si ya estaba quitada (no escribe ni deja huella). `SesionSinSesion`
    (no entra al panel), `RefDeSesionNoVale`, `SesionNoEsta` (el proyecto no está) y
    `SesionesSinTabla` (falta la migración).

    `sesion_ref` es el `ref` que DEVOLVIÓ la App: la ruta se lo pasa después de volver a
    preguntarle al registro por las sesiones de ese cliente. `codigo` es el código que la sesión
    tenía al decidir: se guarda, y se pinta en el aviso de una agregada que la App ya no devuelve o
    está cancelada. La fila y su huella `crear` (actor `panel`) van en la misma transacción."""
    from web.auth import puede_entrar

    ref = ref_de_sesion_que_vale(sesion_ref)
    if chat_id is None or not puede_entrar(chat_id):
        raise SesionSinSesion("quien lo pide no entra al panel")
    guardable = _codigo_de_sesion_guardable(codigo)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        if await cur.fetchone() is None:
            raise SesionNoEsta("ese proyecto no está")
        try:
            await cur.execute(
                "SELECT id FROM sesiones_de_proyecto "
                " WHERE proyecto_id = %s AND sesion_ref = %s AND modo = 'quitada' AND borrado_en IS NULL",
                (proyecto_id, ref))
            ya = await cur.fetchone()
            if ya is not None:
                return False
            await cur.execute(
                "INSERT INTO sesiones_de_proyecto "
                "  (proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                "VALUES (%s, %s, %s, 'quitada', %s) RETURNING *",
                (proyecto_id, ref, guardable, chat_id))
            fila = dict(await cur.fetchone())
        except Exception as e:
            if _tabla_ausente(e):
                raise SesionesSinTabla("sesiones_de_proyecto no existe todavía") from e
            raise
        await _huella_de_sesion(conn, "crear", fila["id"], None, fila,
                                "sesión quitada de la lista del proyecto desde el panel")
        return True


async def _deshacer_la_decision_viva(proyecto_id: int, sesion_ref, modo: str, chat_id: int,
                                     motivo: str) -> bool:
    """Saca la decisión viva de ese `modo` de ese proyecto (le pone `borrado_en`, no borra la fila)
    y deja su huella `borrar` con la fila entera de antes, actor `panel`, en la misma transacción.
    `True` si la sacó. `SesionSinSesion`, `RefDeSesionNoVale`, `SesionNoEsta` (no estaba) y
    `SesionesSinTabla`. Al registro no le pregunta nada: el identificador ya está guardado."""
    from web.auth import puede_entrar

    ref = ref_de_sesion_que_vale(sesion_ref)
    if chat_id is None or not puede_entrar(chat_id):
        raise SesionSinSesion("quien lo pide no entra al panel")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT * FROM sesiones_de_proyecto "
                " WHERE proyecto_id = %s AND sesion_ref = %s AND modo = %s AND borrado_en IS NULL",
                (proyecto_id, ref, modo))
            antes = await cur.fetchone()
        except Exception as e:
            if _tabla_ausente(e):
                raise SesionesSinTabla("sesiones_de_proyecto no existe todavía") from e
            raise
        if antes is None:
            raise SesionNoEsta("esa sesión no estaba en la lista de ese proyecto")
        antes = dict(antes)
        escrita = await conn.execute(
            "UPDATE sesiones_de_proyecto SET borrado_en = now() "
            " WHERE id = %s AND borrado_en IS NULL", (antes["id"],))
        if escrita.rowcount == 0:
            raise SesionNoEsta("esa sesión ya no estaba en la lista de ese proyecto")
        await _huella_de_sesion(conn, "borrar", antes["id"], antes, None, motivo)
        return True


async def devolver_sesion_de_proyecto(proyecto_id: int, sesion_ref, chat_id: int) -> bool:
    """Devuelve a la lista una sesión QUITADA de ESE proyecto: deshace la decisión. `True` si la
    devolvió. `SesionSinSesion`, `RefDeSesionNoVale`, `SesionNoEsta` (esa sesión no estaba quitada de
    ese proyecto) y `SesionesSinTabla` (ver `_deshacer_la_decision_viva`)."""
    return await _deshacer_la_decision_viva(
        proyecto_id, sesion_ref, "quitada", chat_id,
        "sesión devuelta a la lista del proyecto desde el panel")


async def sacar_sesion_agregada_de_proyecto(proyecto_id: int, sesion_ref, chat_id: int) -> bool:
    """Saca de la lista una sesión que la casa había agregado a mano a ESE proyecto: deshace esa
    decisión. `True` si la sacó. Las mismas excepciones que `devolver_sesion_de_proyecto`, con
    `SesionNoEsta` si esa sesión no estaba agregada a ese proyecto. Lo usa el botón «Sacar» del
    aviso de una agregada que el registro ya no devuelve o devolvió cancelada."""
    return await _deshacer_la_decision_viva(
        proyecto_id, sesion_ref, "agregada", chat_id,
        "sesión agregada sacada de la lista del proyecto desde el panel")


async def agregar_sesion_a_proyecto(proyecto_id: int, sesion_ref, codigo, chat_id: int) -> bool:
    """Agrega a mano una sesión del registro a la lista de ESE proyecto vivo: guarda la decisión
    (`modo='agregada'`). `True` si se guardó, `False` si ya había una decisión viva para esa sesión
    en ese proyecto (no escribe ni deja huella). `SesionSinSesion` (no entra al panel),
    `RefDeSesionNoVale`, `SesionNoEsta` (el proyecto no está) y `SesionesSinTabla`.

    `sesion_ref` es el `ref` que devolvió la App al volver a pedirle la sesión, y `codigo` el que
    traía, para poder nombrarla si un día la App ya no la devuelve. Agregar una que estaba quitada
    es devolverla, y no pasa por acá (la ruta la desvía). La fila y su huella `crear` (actor
    `panel`) van en la misma transacción."""
    from web.auth import puede_entrar

    ref = ref_de_sesion_que_vale(sesion_ref)
    if chat_id is None or not puede_entrar(chat_id):
        raise SesionSinSesion("quien lo pide no entra al panel")
    guardable = _codigo_de_sesion_guardable(codigo)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        if await cur.fetchone() is None:
            raise SesionNoEsta("ese proyecto no está")
        try:
            await cur.execute(
                "SELECT id FROM sesiones_de_proyecto "
                " WHERE proyecto_id = %s AND sesion_ref = %s AND borrado_en IS NULL",
                (proyecto_id, ref))
            if await cur.fetchone() is not None:
                return False
            await cur.execute(
                "INSERT INTO sesiones_de_proyecto "
                "  (proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                "VALUES (%s, %s, %s, 'agregada', %s) RETURNING *",
                (proyecto_id, ref, guardable, chat_id))
            fila = dict(await cur.fetchone())
        except Exception as e:
            if _tabla_ausente(e):
                raise SesionesSinTabla("sesiones_de_proyecto no existe todavía") from e
            raise
        await _huella_de_sesion(conn, "crear", fila["id"], None, fila,
                                "sesión agregada a la lista del proyecto desde el panel")
        return True


async def proyecto_vivo_con_nombre(cur, nombre: str, excluir_id: int | None = None):
    """El id del proyecto ACTIVO que se llama así, o None. LA comparación de
    nombres de proyecto: todo chequeo de duplicados la usa, para que no puedan
    separarse.

    «ACTIVO» es `cuenta_como_activo`: ni la papelera, ni un pausado, ni un
    cerrado cuentan (Tiziano, 18-oct-2026: un cliente vuelve con algo distinto,
    se abre un proyecto nuevo y el viejo sigue pausado o cerrado CON EL MISMO
    NOMBRE). El nombre de la función se queda por no mover sus referencias.
    El SQL pasa los dos estados que no cuentan como parámetros, sacados de las
    mismas constantes que `cuenta_como_activo`: no hay una segunda lista.

    `lower(nombre) = lower(...)` y nada más (ignora mayúsculas, NO tildes: es lo
    que hacía la búsqueda antes, y un chequeo con otro criterio dejaría pasar
    nombres que la búsqueda ve iguales). `excluir_id` es la propia fila, para
    poder cambiar solo las mayúsculas de sí mismo. `ORDER BY id`: si algún día
    hay dos, siempre gana el mismo (antes, `LIMIT 1` sin orden dejaba la
    elección a Postgres).

    `cur` es un cursor con `row_factory=dict_row`.
    """
    await cur.execute(
        """
        SELECT id FROM proyectos
         WHERE borrado_en IS NULL
           AND lower(nombre) = lower(%s)
           AND id <> %s
           AND estado NOT IN (%s, %s)
         ORDER BY id
         LIMIT 1
        """,
        (nombre, excluir_id or 0, ESTADO_PROYECTO_PAUSADO, ESTADO_PROYECTO_CERRADO))
    fila = await cur.fetchone()
    return fila["id"] if fila else None


# ── UN PROYECTO NO SE CREA SIN CLIENTE (Tiziano, 18-oct-2026; pieza 1, «la puerta») ───────────────────────────────
#
# LA REGLA: en CDS, ACD y sin grupo el proyecto lleva cliente y su nombre es el nombre del cliente; en IA
# (`AREA_TECNICA`) no hace falta. Y UN SOLO PROYECTO ACTIVO POR CLIENTE (pausados, cerrados y la papelera no cuentan).
# Dos preguntas, dos funciones, y todos los caminos preguntan a ESTAS:
#   · `cliente_es_obligatorio(area)`: ¿este grupo exige cliente?
#   · `proyecto_vivo_del_cliente(cur, cliente)`: ¿ese cliente ya tiene un proyecto activo?
# El cierre en la base (CHECK e índice) es de la pieza 6, y es el último recurso, no la defensa principal.
def cliente_es_obligatorio(area) -> bool:
    """¿Un proyecto de este grupo exige cliente? Todo menos IA. `None` (sin grupo) y cualquier grupo desconocido
    o que no sea un texto dan True: lo que la función no sabe clasificar cae del lado estricto. Es LA ÚNICA que
    lo decide; nadie compara con `'IA'` por su cuenta."""
    return area != AREA_TECNICA


class ClienteConProyecto(ValueError):
    """El cliente ya tiene un proyecto activo y no se pueden activar 2. `proyecto_id`, `nombre` y `estado` son del
    que ya está (para que la pantalla lo nombre y lo enlace). El MENSAJE no lleva el nombre: el nombre de un
    proyecto de CDS/ACD es el de un cliente y los avisos van a los registros."""

    def __init__(self, proyecto_id: int, nombre: str, estado: str):
        super().__init__(f"el cliente tiene un proyecto activo (#{proyecto_id}) y no se pueden activar 2")
        self.proyecto_id = proyecto_id
        self.nombre = nombre
        self.estado = estado


class ClienteDelProyectoNoVale(ValueError):
    """El cliente (o la falta de él) no puede quedar así en un proyecto que ya existe. `clave`: `obligatorio`
    (un proyecto de CDS/ACD/sin grupo no se queda sin cliente), `nombre` (en CDS/ACD el nombre es el del cliente),
    `ficha` (la ficha de Noco no existe o no vale: `poner_cliente`) y `proyecto` (el proyecto no existe o está en la
    papelera: `poner_cliente`). Cada una es una CAUSA distinta, para que la pantalla diga la que fue."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


async def proyecto_vivo_del_cliente(cur, cliente_noco_id, excluir_id: int | None = None):
    """El proyecto ACTIVO de ese cliente, como `{id, nombre, estado}`, o None. LA ÚNICA que decide «un solo
    proyecto activo por cliente». Cuenta por `cliente_noco_id` (no por nombre) y entre CDS, ACD y sin grupo
    juntos: IA no cuenta ni es contada (`cliente_es_obligatorio`, escrito en SQL como `COALESCE(area,'') <> IA`).
    Un pausado, un cerrado y la papelera tampoco cuentan (`cuenta_como_activo`, en SQL `estado NOT IN (pausado,
    cerrado)` con las mismas constantes). `excluir_id` es la propia fila. `ORDER BY id LIMIT 1`.

    `cur` es un cursor con `row_factory=dict_row`."""
    await cur.execute(
        """
        SELECT id, nombre, estado FROM proyectos
         WHERE borrado_en IS NULL
           AND cliente_noco_id = %s
           AND COALESCE(area, '') <> %s
           AND estado NOT IN (%s, %s)
           AND id <> %s
         ORDER BY id
         LIMIT 1
        """,
        (cliente_noco_id, AREA_TECNICA, ESTADO_PROYECTO_PAUSADO, ESTADO_PROYECTO_CERRADO, excluir_id or 0))
    fila = await cur.fetchone()
    return dict(fila) if fila else None


def _cliente_que_cuenta(fila) -> int | None:
    """El cliente por el que ESTA fila ocupa un proyecto activo, o None si no ocupa ninguno: tiene que estar
    fuera de la papelera, con cliente, activa (`cuenta_como_activo`) y ser de un grupo que lo exige."""
    if fila is None or fila.get("borrado_en") is not None or fila.get("cliente_noco_id") is None:
        return None
    if not cuenta_como_activo(fila.get("estado")) or not cliente_es_obligatorio(fila.get("area")):
        return None
    return fila["cliente_noco_id"]


async def proyecto_puede_quedar_activo(cur, proyecto_id, antes, despues) -> None:
    """Comprueba, ANTES de escribir, que un proyecto que va a quedar como `despues` no rompe «un solo proyecto
    activo por cliente» ni el nombre de otro activo. `antes` y `despues` son dicts con `estado`, `area`,
    `cliente_noco_id`, `nombre` y `borrado_en` (`antes=None` es un proyecto que todavía no existe: la creación).
    LA LLAMAN todos los caminos por los que un proyecto PASA a ocupar a su cliente: crear, convertir una tarea,
    `poner_cliente`, `crud.editar` (estado o grupo: reabrir un cerrado, reactivar un pausado, pasar de IA a
    CDS/ACD) y `crud.deshacer` (deshacer un cierre o una pausa, restaurar de la papelera).

    Se consulta SOLO cuando pasa a ocupar a un cliente que antes no ocupaba (`_cliente_que_cuenta`): activo→pausado,
    activo→cerrado, cerrado→pausado y activo→activo no consultan; volver a escoger al mismo cliente tampoco.
    Si el cliente ya tiene OTRO activo: `ClienteConProyecto`. Aparte, un proyecto que vuelve a ser activo
    (cuenta_como_activo, fuera de la papelera) no puede tomar el nombre de otro activo: `NombreDeProyectoNoVale`
    (`repetido`). Los cambios de nombre de uno que ya era activo los revisan quienes lo renombran."""
    clave = _cliente_que_cuenta(despues)
    if clave is not None and clave != _cliente_que_cuenta(antes):
        otro = await proyecto_vivo_del_cliente(cur, clave, excluir_id=proyecto_id)
        if otro is not None:
            raise ClienteConProyecto(otro["id"], otro["nombre"], otro["estado"])
    if antes is not None and despues.get("borrado_en") is None and cuenta_como_activo(despues.get("estado")):
        era_activo = antes.get("borrado_en") is None and cuenta_como_activo(antes.get("estado"))
        if not era_activo and await proyecto_vivo_con_nombre(
                cur, despues["nombre"], excluir_id=proyecto_id) is not None:
            raise NombreDeProyectoNoVale("repetido", "ya hay otro proyecto con ese nombre")


def nombre_que_toca(area, cliente_nombre, nombre_dado) -> str:
    """El nombre que va a quedar en un proyecto NUEVO (o al poner su cliente). En CDS, ACD y sin grupo
    (`cliente_es_obligatorio`) es el nombre del cliente, ya limpio por `nombre_de_proyecto_que_vale`; un
    `nombre_dado` distinto del cliente es `ProyectoNoSeCrea('nombre')` (no se ignora en silencio) y `None` o
    el mismo nombre valen. En IA es el `nombre_dado`, limpio como siempre."""
    if not cliente_es_obligatorio(area):
        return nombre_de_proyecto_que_vale(nombre_dado)
    del_cliente = nombre_de_proyecto_que_vale(cliente_nombre)
    if nombre_dado is not None and (not isinstance(nombre_dado, str) or nombre_dado.strip() != del_cliente):
        raise ProyectoNoSeCrea("nombre", "en CDS y ACD el nombre del proyecto es el del cliente")
    return del_cliente


# ── LAS COTIZACIONES DE UN PROYECTO: LA PUERTA DEL CONTENIDO (pieza 1 del diseño «Cotizaciones en
# Proyectos», 14-oct-2026) ──────────────────────────────────────────────────────────────────────────
#
# `cotizaciones.contenido` es un JSON en TEXT (opciones, renglones y condiciones). UNA SOLA PUERTA,
# `contenido_de_cotizacion_que_vale`, decide qué contenido vale y devuelve el texto que se guarda;
# nada escribe `contenido` sin pasar por ella (las escrituras llegan en la pieza 3).
#
# Los vocabularios cerrados (estados, validez, claves de condición) se declaran UNA vez, acá, y la
# prueba los compara con los CHECK de `db/schema.sql` y de la migración. Los topes de largo y de
# cantidad son de diseño: no salen de medir cotizaciones reales (no hay ninguna en Lucy).
ESTADOS_DE_COTIZACION = ("borrador", "enviada")
VALIDEZ_DE_COTIZACION = (7, 15, 30)
CLAVES_DE_CONDICION_DE_COTIZACION = ("validez", "alternativas", "deposito", "balance", "impuestos")
MAX_OPCIONES_DE_COTIZACION = 6
MAX_RENGLONES_DE_OPCION = 10
MAX_CONDICIONES_DE_COTIZACION = 20
LARGO_NOMBRE_DE_OPCION = 120
LARGO_DESCRIPCION_DE_RENGLON = 200
LARGO_DETALLE_DE_RENGLON = 1000
LARGO_TEXTO_DE_CONDICION = 300
TOPE_TOTAL_DE_RENGLON = Decimal("99999999.99")

# Dinero escrito a la dominicana: «62500», «62,500», «62,500.50», «62500.5». Solo dígitos ASCII (un
# dígito de otro alfabeto también lo entiende `Decimal`), coma solo como separador de miles en grupos
# de tres, punto para los centavos. Nada de signo, exponente, NaN ni Infinity: no encajan.
_RE_DINERO_DE_COTIZACION = re.compile(r"^(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.([0-9]+))?$")


class CotizacionNoVale(ValueError):
    """Un contenido de cotización que no puede quedar. `clave` dice por qué (`forma`, `sin_opciones`,
    `total_cero`, …) para que una pantalla lo traduzca sin adivinar por el texto. El mensaje NUNCA
    repite lo que se pidió."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def _forma_simple_de_cotizacion(c: str) -> str:
    """El PDF escribe con una letra de Latin-1 (los acentos y la «ñ» sí; «—», «€», «“» y los emoji no). Un
    carácter fuera de Latin-1 se cambia por su forma simple cuando la REGLA la da: cualquier raya (categoría
    `Pd`) → «-»; cualquier comilla tipográfica (su nombre Unicode dice QUOTATION MARK) → «"» si es doble o
    «'» si es simple; y lo que Unicode descompone (NFKD) en puros signos de puntuación ASCII, como «…» → «...» (letras como «™» → «TM» no: se rechaza). Lo demás se deja
    como está y la puerta lo rechaza."""
    if ord(c) <= 0xFF:
        return c
    if unicodedata.category(c) == "Pd":
        return "-"
    nombre = unicodedata.name(c, "")
    if "QUOTATION MARK" in nombre and "ANGLE" not in nombre:
        return '"' if "DOUBLE" in nombre else "'"
    compat = unicodedata.normalize("NFKD", c)
    solo_signos = compat.isascii() and all(unicodedata.category(x).startswith("P") for x in compat)
    return compat if solo_signos else c


def _texto_de_cotizacion(crudo, campo: str, largo: int, *, minimo: int = 1, varias_lineas: bool = False) -> str:
    """Un texto del contenido: `str`, `\\r\\n` y `\\r` → `\\n`, sin espacios de alrededor, de `minimo` a
    `largo` caracteres, sin NUL ni lo que no se codifique, con comillas tipográficas, rayas y «…» cambiadas por su forma simple (`_forma_simple_de_cotizacion`), sin nada fuera de Latin-1 (`fuera_de_latin1`), ni ningún carácter de control (`Cc`) ni
    separador de línea o de párrafo (`Zl`/`Zp`); con `varias_lineas` el `\\n` sí se deja."""
    if crudo is None:
        crudo = ""
    if not isinstance(crudo, str):
        raise CotizacionNoVale("tipo", f"{campo}: tiene que ser un texto")
    limpio = crudo.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        limpio.encode("utf-8")
    except UnicodeEncodeError:
        raise CotizacionNoVale("caracteres", f"{campo}: lleva caracteres que no se pueden guardar") from None
    limpio = "".join(_forma_simple_de_cotizacion(c) for c in limpio)
    for c in limpio:
        if varias_lineas and c == "\n":
            continue
        if unicodedata.category(c) in ("Cc", "Zl", "Zp"):
            raise CotizacionNoVale("caracteres", f"{campo}: lleva caracteres que no se pueden guardar")
        if ord(c) > 0xFF:
            raise CotizacionNoVale("fuera_de_latin1", f"{campo}: lleva caracteres que el PDF no puede escribir")
    if len(limpio) < minimo:
        raise CotizacionNoVale(f"{campo}_vacio", f"{campo}: no puede quedar vacío")
    if len(limpio) > largo:
        raise CotizacionNoVale(f"{campo}_largo", f"{campo}: no puede pasar de {largo} caracteres")
    return limpio


def total_de_cotizacion_que_vale(crudo) -> str:
    """Lo escrito en «Total» → el texto del `Decimal` con dos decimales («62500.00»), o `CotizacionNoVale`.
    Acepta `str`, `int` (no `bool`) y `Decimal`; un `float` NO (llega ya redondeado por la máquina y
    el repo no pasa dinero por `float`). Mayor que cero, a lo sumo `TOPE_TOTAL_DE_RENGLON`, a lo sumo
    dos decimales."""
    if crudo is None:
        raise CotizacionNoVale("total_vacio", "el total no puede quedar vacío")
    if isinstance(crudo, bool) or not isinstance(crudo, (str, int, Decimal)):
        raise CotizacionNoVale("total_tipo", "el total tiene que ser un texto o un número exacto")
    if isinstance(crudo, (int, Decimal)):
        valor = Decimal(crudo)
        if not valor.is_finite():
            raise CotizacionNoVale("total_formato", "el total no se entiende como dinero")
    else:
        escrito = str(crudo).strip()
        if not escrito:
            raise CotizacionNoVale("total_vacio", "el total no puede quedar vacío")
        m = _RE_DINERO_DE_COTIZACION.match(escrito)
        if not m:
            raise CotizacionNoVale("total_formato", "el total no se entiende como dinero")
        # Se mira el TEXTO, no el valor: «62.500» vale lo mismo que «62.50» como número, pero quien lo
        # escribió pudo querer sesenta y dos mil quinientos (así lo lee otro validador de Lucy).
        if m.group(1) is not None and len(m.group(1)) > 2:
            raise CotizacionNoVale("total_decimales", "el total no puede llevar más de dos decimales")
        valor = Decimal(escrito.replace(",", ""))
    if valor <= 0:
        raise CotizacionNoVale("total_cero", "el total tiene que ser mayor que cero")
    if valor > TOPE_TOTAL_DE_RENGLON:
        raise CotizacionNoVale("total_alto", "el total es demasiado grande")
    centavos = Decimal("0.01")
    if valor.as_tuple().exponent < -2:      # un Decimal ya traído: se mira cómo viene escrito (62.500 no)
        raise CotizacionNoVale("total_decimales", "el total no puede llevar más de dos decimales")
    if valor != valor.quantize(centavos):
        raise CotizacionNoVale("total_decimales", "el total no puede llevar más de dos decimales")
    return str(valor.quantize(centavos))


def _vacio_de_cotizacion(valor) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def contenido_de_cotizacion_que_vale(crudo) -> str:
    """El contenido que llega (de la ventana de la cotización) → el JSON en texto que se guarda, o
    `CotizacionNoVale`. Forma de lo que llega y de lo que sale:

        {"opciones": [{"nombre": "...", "renglones": [{"descripcion": "...", "detalle": "...",
                                                        "total": "62500.00"}]}],
         "condiciones": [{"clave": "validez"|"alternativas"|"deposito"|"balance"|"impuestos"|None,
                          "texto": "...", "marcada": True}]}

    Antes de validar se descartan lo vacío de sobra que dibuja el formulario sin JavaScript: el renglón
    con descripción, detalle y total vacíos, la opción sin nombre y sin renglones con algo, la
    condición sin clave y sin texto. Después: de 1 a `MAX_OPCIONES_DE_COTIZACION` opciones, cada una con
    nombre y de 1 a `MAX_RENGLONES_DE_OPCION` renglones (descripción obligatoria, detalle opcional,
    total por `total_de_cotizacion_que_vale`); condiciones con `marcada` booleana, la clave de la lista
    cerrada `CLAVES_DE_CONDICION_DE_COTIZACION` o `None`, sin repetir clave y con texto. Ningún `float`
    llega al JSON: el total sale como texto del `Decimal`. Los textos de las condiciones `validez` y
    `alternativas` NO se rehacen acá: esta puerta no conoce la validez ni el número de opciones."""
    if not isinstance(crudo, dict):
        raise CotizacionNoVale("forma", "el contenido tiene que ser un diccionario con opciones y condiciones")
    if set(crudo) - {"opciones", "condiciones"}:
        raise CotizacionNoVale("forma", "el contenido trae campos que no existen")
    opciones_crudas = crudo.get("opciones")
    condiciones_crudas = crudo.get("condiciones")
    if condiciones_crudas is None:
        condiciones_crudas = []
    if not isinstance(opciones_crudas, list) or not isinstance(condiciones_crudas, list):
        raise CotizacionNoVale("forma", "las opciones y las condiciones tienen que ser listas")

    opciones = []
    for op in opciones_crudas:
        if not isinstance(op, dict) or set(op) - {"nombre", "renglones"}:
            raise CotizacionNoVale("forma", "una opción no tiene la forma de una opción")
        renglones_crudos = op.get("renglones")
        if renglones_crudos is None:
            renglones_crudos = []
        if not isinstance(renglones_crudos, list):
            raise CotizacionNoVale("forma", "los renglones de una opción tienen que ser una lista")
        con_algo = []
        for r in renglones_crudos:
            if not isinstance(r, dict) or set(r) - {"descripcion", "detalle", "total"}:
                raise CotizacionNoVale("forma", "un renglón no tiene la forma de un renglón")
            if all(_vacio_de_cotizacion(r.get(k)) for k in ("descripcion", "detalle", "total")):
                continue
            con_algo.append(r)
        if _vacio_de_cotizacion(op.get("nombre")) and not con_algo:
            continue
        if len(con_algo) > MAX_RENGLONES_DE_OPCION:
            raise CotizacionNoVale("renglones_de_mas", f"una opción no puede tener más de {MAX_RENGLONES_DE_OPCION} renglones")
        if not con_algo:
            raise CotizacionNoVale("sin_renglones", "cada opción necesita al menos un renglón")
        renglones = []
        for r in con_algo:
            renglones.append({
                "descripcion": _texto_de_cotizacion(r.get("descripcion"), "descripcion", LARGO_DESCRIPCION_DE_RENGLON),
                "detalle": _texto_de_cotizacion(r.get("detalle"), "detalle", LARGO_DETALLE_DE_RENGLON,
                                                minimo=0, varias_lineas=True),
                "total": total_de_cotizacion_que_vale(r.get("total")),
            })
        opciones.append({"nombre": _texto_de_cotizacion(op.get("nombre"), "nombre", LARGO_NOMBRE_DE_OPCION),
                         "renglones": renglones})
    if not opciones:
        raise CotizacionNoVale("sin_opciones", "la cotización necesita al menos una opción")
    if len(opciones) > MAX_OPCIONES_DE_COTIZACION:
        raise CotizacionNoVale("opciones_de_mas", f"no puede haber más de {MAX_OPCIONES_DE_COTIZACION} opciones")

    condiciones, claves = [], set()
    for c in condiciones_crudas:
        if not isinstance(c, dict) or set(c) - {"clave", "texto", "marcada"}:
            raise CotizacionNoVale("forma", "una condición no tiene la forma de una condición")
        clave = c.get("clave")
        if clave is not None and (not isinstance(clave, str) or clave not in CLAVES_DE_CONDICION_DE_COTIZACION):
            raise CotizacionNoVale("condicion_clave", "una condición trae una clave que no existe")
        if clave is None and _vacio_de_cotizacion(c.get("texto")):
            continue
        marcada = c.get("marcada")
        if not isinstance(marcada, bool):
            raise CotizacionNoVale("condicion_marcada", "una condición tiene que decir si va marcada (sí o no)")
        if clave is not None:
            if clave in claves:
                raise CotizacionNoVale("condicion_repetida", "una condición está repetida")
            claves.add(clave)
        condiciones.append({"clave": clave,
                            "texto": _texto_de_cotizacion(c.get("texto"), "condicion", LARGO_TEXTO_DE_CONDICION,
                                                          varias_lineas=True),
                            "marcada": marcada})
    if len(condiciones) > MAX_CONDICIONES_DE_COTIZACION:
        raise CotizacionNoVale("condiciones_de_mas", f"no puede haber más de {MAX_CONDICIONES_DE_COTIZACION} condiciones")
    return json.dumps({"opciones": opciones, "condiciones": condiciones}, ensure_ascii=False)


# ── EL NÚMERO DE UNA COTIZACIÓN (pieza 2 del diseño «Cotizaciones en Proyectos», 14-oct-2026) ─────────
#
# UNA SOLA PUERTA para sacar un número: `siguiente_numero_de_cotizacion`. Un contador por año
# (`serie_cotizaciones`) que se sube en la MISMA sentencia que lo lee: Postgres bloquea la fila hasta el
# `COMMIT` del que la subió, así que dos guardados a la vez salen seguidos. `UNIQUE (anio, numero)` de
# `cotizaciones` es la última puerta. La serie nace vacía (respuesta 1 de Tiziano): la primera de 2026 es
# la 001. Quien la llama (pieza 3) lo hace en la misma transacción que el `INSERT` de la cotización.
# Un borrador que no se usa gasta su número: no se reutilizan.
SENTENCIA_DEL_NUMERO_DE_COTIZACION = (
    "INSERT INTO serie_cotizaciones (anio, ultimo) VALUES (%s, 1) "
    "ON CONFLICT (anio) DO UPDATE SET ultimo = serie_cotizaciones.ultimo + 1 "
    "RETURNING ultimo")


def _anio_de_cotizacion_que_vale(anio) -> bool:
    return isinstance(anio, int) and not isinstance(anio, bool) and 2000 <= anio <= 9999


async def siguiente_numero_de_cotizacion(cur, anio: int) -> int:
    """El número que sigue en la serie de `anio` (1 si es la primera del año). Escribe en
    `serie_cotizaciones`: llámala dentro de la transacción que guarda la cotización. `cur` es un cursor
    con `row_factory=dict_row`. El año es el del día en que se guarda por primera vez, no el de `fecha`."""
    if not _anio_de_cotizacion_que_vale(anio):
        raise ValueError("el año de la serie no es un año")
    await cur.execute(SENTENCIA_DEL_NUMERO_DE_COTIZACION, (anio,))
    fila = await cur.fetchone()
    return int(fila["ultimo"])


def codigo_de_cotizacion(anio: int, numero: int) -> str:
    """`COT-AAAA-NNN` (tres cifras como mínimo: la 1000 es `COT-2026-1000`)."""
    if isinstance(numero, bool) or not isinstance(numero, int) or numero < 1:
        raise ValueError("el número de la cotización no es un entero positivo")
    if not _anio_de_cotizacion_que_vale(anio):
        raise ValueError("el año de la cotización no es un año")
    return f"COT-{anio}-{numero:03d}"


# ── GUARDAR Y LEER LAS COTIZACIONES (pieza 3 del diseño «Cotizaciones en Proyectos», 14-oct-2026) ─────
#
# UNA SOLA PUERTA para escribir `cotizaciones`: `guardar_cotizacion` (crear, editar un borrador, enviar,
# corregir una enviada). Y UNA LECTURA APARTE, `cotizaciones_de_proyectos` (con su propia conexión: un
# 42P01 deja abortada la transacción donde ocurre).
#
# Lo que decidió Tiziano (14-oct-2026, «1) Comienza hoy con el codigo 2) SI 3) Si»): la serie nace vacía;
# una ENVIADA SÍ se puede corregir y conserva su número (no se congela: lo que queda es la huella `corregir`
# en `log_acciones`, con lo de antes y lo de después); quien entra solo a ver LA VE y no escribe (acá:
# `puede_entrar`, la sesión de la casa; la cookie de solo ver no llega a esta función).
#
# El año del número es el del día en que se guarda por primera vez (`hoy_rd().year`), no el de `fecha`:
# corregir la fecha no cambia el número. Nombre y teléfono del cliente se COPIAN a la cotización al
# guardar y se leen de la copia: si la ficha del cliente cambia, la cotización ya hecha no cambia.
# El candado de edición es `cambiado_en`: se escribe solo si sigue siendo el que el formulario leyó.
ACCIONES_DE_COTIZACION = ("borrador", "enviar")
_NUMERO_EN_PALABRAS = {2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis"}
# Los dos textos que SE REHACEN en cada guardado (con la validez y con el número de opciones). Los usa
# `condiciones_con_sus_textos_al_dia` y también la ventana de la cotización (`cotizaciones.py`): una sola fuente.
TEXTO_DE_VALIDEZ_DE_COTIZACION = "Esta cotización tiene validez de {dias} días a partir de la fecha de emisión."
TEXTO_DE_ALTERNATIVAS_DE_COTIZACION = "Las {palabra} opciones son alternativas: se selecciona una sola para el proyecto."
LARGO_PARA_NOMBRE_DE_COTIZACION = 200
LARGO_PARA_TELEFONO_DE_COTIZACION = 40


class CotizacionSinSesion(Exception):
    """Quien guarda no entra al panel (la cookie de solo ver tampoco)."""


class CotizacionNoEsta(Exception):
    """El proyecto no existe o está en la papelera, o la cotización no es de ese proyecto."""


class CotizacionCambio(Exception):
    """El candado: la cotización cambió desde que se leyó. NO se guardó nada."""


class CotizacionEnviadaSoloSeEnvia(Exception):
    """Una enviada no vuelve a borrador: solo se corrige con `accion='enviar'`."""


class CotizacionesSinTabla(Exception):
    """Falta la migración de las cotizaciones (SQLSTATE 42P01)."""


def condiciones_con_sus_textos_al_dia(contenido_json: str, validez_dias: int) -> str:
    """Rehace los textos de las condiciones `validez` y `alternativas` con la validez y el número de
    opciones de ESTE guardado (así nunca dicen «15 días» con la validez en 30). Con una sola opción
    `alternativas` no se ofrece y se quita. `contenido_json` ya pasó por la puerta."""
    contenido = json.loads(contenido_json)
    n = len(contenido["opciones"])
    hechas = []
    for c in contenido["condiciones"]:
        if c["clave"] == "validez":
            c = {**c, "texto": TEXTO_DE_VALIDEZ_DE_COTIZACION.format(dias=validez_dias)}
        elif c["clave"] == "alternativas":
            if n < 2:
                continue
            c = {**c, "texto": TEXTO_DE_ALTERNATIVAS_DE_COTIZACION.format(palabra=_NUMERO_EN_PALABRAS[n])}
        hechas.append(c)
    contenido["condiciones"] = hechas
    return json.dumps(contenido, ensure_ascii=False)


def _instante_de_cotizacion(valor) -> datetime:
    """El `cambiado_en` que el formulario leyó: un `datetime` o su texto ISO."""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor)
        except ValueError:
            raise CotizacionNoVale("candado", "el candado de la cotización no se entiende") from None
    if not isinstance(valor, datetime):
        raise CotizacionNoVale("candado", "el candado de la cotización no se entiende")
    return valor


def _fecha_de_cotizacion(valor) -> date:
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor)
        except ValueError:
            raise CotizacionNoVale("fecha", "la fecha no se entiende") from None
    if isinstance(valor, date) and not isinstance(valor, datetime):
        return valor
    raise CotizacionNoVale("fecha", "la fecha no se entiende")


def _validez_de_cotizacion(valor) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor not in VALIDEZ_DE_COTIZACION:
        raise CotizacionNoVale("validez", "la validez tiene que ser de 7, 15 o 30 días")
    return valor


async def _huella_de_cotizacion(conn, accion: str, registro_id: int, antes, despues, motivo: str) -> None:
    await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES ('panel', %s, 'cotizaciones', %s, %s, %s, %s, NULL)
        """,
        (accion, registro_id,
         json.dumps(antes, default=str, ensure_ascii=False) if antes else None,
         json.dumps(despues, default=str, ensure_ascii=False) if despues else None,
         motivo))


# Las dos sentencias que cambian una cotización YA hecha. Ninguna toca `proyecto_id`, `anio`, `numero` ni
# `creado_*`; las dos exigen el candado. La de borrador exige además que SIGA siendo borrador: una enviada
# no vuelve atrás. La de enviar guarda `enviada_en` de la primera vez (`coalesce`), también al corregir.
SENTENCIA_DE_GUARDAR_BORRADOR = (
    "UPDATE cotizaciones SET para_nombre = %s, para_telefono = %s, fecha = %s, validez_dias = %s, "
    "contenido = %s, cambiado_en = now() "
    "WHERE id = %s AND proyecto_id = %s AND cambiado_en = %s AND borrado_en IS NULL AND estado = 'borrador' "
    "RETURNING id, proyecto_id, anio, numero, estado, para_nombre, para_telefono, fecha, validez_dias, contenido, creado_en, creado_por_chat_id, cambiado_en, enviada_en")
SENTENCIA_DE_ENVIAR = (
    "UPDATE cotizaciones SET estado = 'enviada', enviada_en = coalesce(enviada_en, now()), "
    "para_nombre = %s, para_telefono = %s, fecha = %s, validez_dias = %s, contenido = %s, cambiado_en = now() "
    "WHERE id = %s AND proyecto_id = %s AND cambiado_en = %s AND borrado_en IS NULL "
    "RETURNING id, proyecto_id, anio, numero, estado, para_nombre, para_telefono, fecha, validez_dias, contenido, creado_en, creado_por_chat_id, cambiado_en, enviada_en")


async def guardar_cotizacion(proyecto_id: int, chat_id: int, accion: str, contenido, validez_dias,
                             *, cotizacion_id: int | None = None, cambiado_en=None,
                             para_nombre: str | None = None, para_telefono: str | None = None,
                             fecha=None) -> dict:
    """Guarda UNA cotización de un proyecto VIVO y devuelve la fila (más `codigo` y `creada`).

    `cotizacion_id=None` es una NUEVA: saca su número (`siguiente_numero_de_cotizacion`, año de
    `hoy_rd()`) en la MISMA transacción que la fila. Con `cotizacion_id` es una existente y hace falta
    `cambiado_en`, el candado que el formulario leyó: si ya no es ese, `CotizacionCambio` y no se escribe
    nada. `accion='borrador'` deja borrador (sobre una enviada: `CotizacionEnviadaSoloSeEnvia`);
    `accion='enviar'` deja enviada, también al CORREGIR una enviada, que conserva `anio`, `numero` y
    `enviada_en`. `contenido` pasa por `contenido_de_cotizacion_que_vale` y los textos de `validez` y
    `alternativas` se rehacen. `para_nombre`/`para_telefono`: lo que el formulario trae (se copian);
    `None` en una nueva = el `cliente_nombre` del proyecto / sin teléfono, y en una existente = no tocar.
    `fecha=None` = hoy en una nueva y no tocar en una existente. Errores: `CotizacionSinSesion`,
    `CotizacionNoVale`, `CotizacionNoEsta`, `CotizacionCambio`, `CotizacionesSinTabla`. La fila y su huella
    (`crear`/`editar`/`enviar`/`corregir`, actor `panel`) van en la misma transacción."""
    from web.auth import puede_entrar

    if chat_id is None or not puede_entrar(chat_id):
        raise CotizacionSinSesion("quien guarda no entra al panel")
    if accion not in ACCIONES_DE_COTIZACION:
        raise CotizacionNoVale("accion", "la acción tiene que ser guardar borrador o enviar")
    validez = _validez_de_cotizacion(validez_dias)
    guardable = condiciones_con_sus_textos_al_dia(contenido_de_cotizacion_que_vale(contenido), validez)
    nombre = None if para_nombre is None else _texto_de_cotizacion(
        para_nombre, "para_nombre", LARGO_PARA_NOMBRE_DE_COTIZACION)
    # `None` = no hay / no tocar; `""` = dejarlo sin teléfono (se guarda NULL)
    telefono = None if para_telefono is None else _texto_de_cotizacion(
        para_telefono, "para_telefono", LARGO_PARA_TELEFONO_DE_COTIZACION, minimo=0)
    el_dia = None if fecha is None else _fecha_de_cotizacion(fecha)
    if cotizacion_id is not None:
        if cambiado_en is None:
            raise CotizacionNoVale("candado", "falta el candado de la cotización")
        candado = _instante_de_cotizacion(cambiado_en)

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        if await cur.fetchone() is None:
            raise CotizacionNoEsta("ese proyecto no está")
        try:
            if cotizacion_id is None:
                return await _crear_cotizacion(conn, cur, proyecto_id, chat_id, accion, guardable, validez,
                                               nombre, telefono, el_dia)
            return await _cambiar_cotizacion(conn, cur, proyecto_id, cotizacion_id, accion, guardable, validez,
                                             nombre, telefono, el_dia, candado)
        except Exception as e:
            if _tabla_ausente(e):
                raise CotizacionesSinTabla("cotizaciones no existe todavía") from e
            raise


async def _crear_cotizacion(conn, cur, proyecto_id, chat_id, accion, guardable, validez, nombre, telefono, el_dia):
    if nombre is None:
        await cur.execute("SELECT cliente_nombre FROM proyectos WHERE id = %s", (proyecto_id,))
        de_la_ficha = (await cur.fetchone())["cliente_nombre"]
        nombre = _texto_de_cotizacion(de_la_ficha, "para_nombre", LARGO_PARA_NOMBRE_DE_COTIZACION)
    anio = hoy_rd().year
    numero = await siguiente_numero_de_cotizacion(cur, anio)
    enviada = accion == "enviar"
    # Una sola línea a propósito: `tests/test_backup_alerta.py` lee las columnas de cada INSERT del código.
    await cur.execute(
        "INSERT INTO cotizaciones (proyecto_id, anio, numero, estado, para_nombre, para_telefono, fecha, validez_dias, contenido, creado_por_chat_id, enviada_en) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id, proyecto_id, anio, numero, estado, para_nombre, para_telefono, fecha, validez_dias, contenido, creado_en, creado_por_chat_id, cambiado_en, enviada_en",
        (proyecto_id, anio, numero, "enviada" if enviada else "borrador", nombre, telefono or None,
         el_dia or hoy_rd(), validez, guardable, chat_id, enviada))
    fila = dict(await cur.fetchone())
    await _huella_de_cotizacion(conn, "crear", fila["id"], None, fila,
                                "cotización creada desde el panel de proyectos" + (" y enviada" if enviada else ""))
    return {**fila, "codigo": codigo_de_cotizacion(fila["anio"], fila["numero"]), "creada": True}


async def _cambiar_cotizacion(conn, cur, proyecto_id, cotizacion_id, accion, guardable, validez,
                              nombre, telefono, el_dia, candado):
    await cur.execute(
        "SELECT id, proyecto_id, anio, numero, estado, para_nombre, para_telefono, fecha, validez_dias, contenido, creado_en, creado_por_chat_id, cambiado_en, enviada_en FROM cotizaciones WHERE id = %s AND proyecto_id = %s AND borrado_en IS NULL",
        (cotizacion_id, proyecto_id))
    antes = await cur.fetchone()
    if antes is None:
        raise CotizacionNoEsta("esa cotización no está en ese proyecto")
    antes = dict(antes)
    parametros = (
        antes["para_nombre"] if nombre is None else nombre,
        antes["para_telefono"] if telefono is None else (telefono or None),
        el_dia or antes["fecha"], validez, guardable, cotizacion_id, proyecto_id, candado)
    if accion == "enviar":
        await cur.execute(SENTENCIA_DE_ENVIAR, parametros)
    else:
        await cur.execute(SENTENCIA_DE_GUARDAR_BORRADOR, parametros)
    fila = await cur.fetchone()
    if fila is None:
        if antes["estado"] == "enviada" and accion == "borrador":
            raise CotizacionEnviadaSoloSeEnvia("una enviada no vuelve a borrador")
        raise CotizacionCambio("la cotización cambió mientras se editaba")
    fila = dict(fila)
    if antes["estado"] == "enviada":
        huella, motivo = "corregir", "cotización enviada corregida desde el panel (conserva su número)"
    elif accion == "enviar":
        huella, motivo = "enviar", "cotización enviada desde el panel de proyectos"
    else:
        huella, motivo = "editar", "borrador guardado desde el panel de proyectos"
    await _huella_de_cotizacion(conn, huella, fila["id"], antes, fila, motivo)
    return {**fila, "codigo": codigo_de_cotizacion(fila["anio"], fila["numero"]), "creada": False}


# La lista, el PDF y la subida a Drive leen las mismas columnas de una cotización. Las tres últimas son las de
# Drive (pieza 8, migración `2026-10-16_cotizaciones_drive.sql`): si esa migración falta, SQLSTATE 42703. Cada
# sentencia lleva su lista escrita entera (sin armar el texto), para que los censos de «quién arma SQL al vuelo» no las confundan.
def _cotizaciones_sin_migrar(e: Exception) -> bool:
    """La base no tiene la tabla de las cotizaciones (42P01) o alguna columna que la lectura nombra (42703:
    la migración de Drive sin aplicar). En los dos casos la lectura devuelve `None` y la página carga sin el
    bloque; `tools/humo.py` es quien dice cuál de las dos falta."""
    try:
        return e.sqlstate in ("42P01", "42703")
    except AttributeError:
        return False


def _cotizacion_leida(fila) -> dict:
    """La fila tal como sale de la base, con `codigo`, `contenido` ya leído (dict) y `totales_por_opcion`."""
    f = dict(fila)
    contenido = json.loads(f["contenido"])
    f["contenido"] = contenido
    f["codigo"] = codigo_de_cotizacion(f["anio"], f["numero"])
    f["totales_por_opcion"] = [sum((Decimal(r["total"]) for r in op["renglones"]), Decimal("0"))
                               for op in contenido["opciones"]]
    return f


async def cotizaciones_de_proyectos() -> dict[int, list[dict]] | None:
    """`{proyecto_id: [cotización, ...]}` de las cotizaciones VIVAS de proyectos VIVOS, la más nueva
    primero, o `None` si la tabla todavía no existe (SQLSTATE 42P01) o le faltan las columnas de Drive
    (42703): entonces la página carga como hoy y el bloque no sale. UNA LECTURA APARTE con su propia
    conexión. Nombre y teléfono salen de la COPIA que se guardó, nunca de la ficha del cliente. Cada una
    trae `codigo`, `contenido` ya leído (dict), `totales_por_opcion` (`Decimal`, la suma de los renglones de
    cada opción), `cambiado_en` tal como está en la base (el candado que la pantalla devuelve al guardar) y
    las tres de Drive (`drive_archivo_id`, `drive_version`, `drive_error`). Lo lee cualquier sesión que vea
    Proyectos, también la de solo ver: la función no pide sesión."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT c.id, c.proyecto_id, c.anio, c.numero, c.estado, c.para_nombre, c.para_telefono, "
                "c.fecha, c.validez_dias, c.contenido, c.creado_en, c.creado_por_chat_id, c.cambiado_en, "
                "c.enviada_en, c.drive_archivo_id, c.drive_version, c.drive_error "
                "FROM cotizaciones c JOIN proyectos p ON p.id = c.proyecto_id "
                "WHERE c.borrado_en IS NULL AND p.borrado_en IS NULL "
                "ORDER BY c.creado_en DESC, c.id DESC")
            filas = await cur.fetchall()
        except Exception as e:
            if _cotizaciones_sin_migrar(e):
                return None
            raise
    salida: dict[int, list[dict]] = {}
    for f in filas:
        f = _cotizacion_leida(f)
        salida.setdefault(f["proyecto_id"], []).append(f)
    return salida


# ── EL DRIVE DE LAS COTIZACIONES (pieza 8 del diseño «Cotizaciones en Proyectos», 16-oct-2026) ─────────────
#
# Aquí solo hay SQL. Quién sube, en qué orden y con qué frases lo decide `cotizaciones_drive.py`; quién habla
# con Drive es `drive.py`. «En Drive» quiere decir `drive_version = cambiado_en` y SOLO lo escribe
# `marcar_cotizacion_en_drive`, con el candado de `cambiado_en` en la sentencia: si alguien corrigió la
# cotización mientras se subía, la versión de Drive ya es vieja y la marca no se pone.
SENTENCIA_DE_MARCAR_EN_DRIVE = (
    "UPDATE cotizaciones SET drive_archivo_id = %s, drive_version = %s, drive_error = NULL "
    "WHERE id = %s AND proyecto_id = %s AND cambiado_en = %s AND estado = 'enviada' AND borrado_en IS NULL "
    "RETURNING id")
SENTENCIA_DE_ERROR_DE_DRIVE = (
    "UPDATE cotizaciones SET drive_error = %s "
    "WHERE id = %s AND proyecto_id = %s AND cambiado_en = %s AND estado = 'enviada' AND borrado_en IS NULL "
    "RETURNING id")


async def cotizacion_para_drive(proyecto_id: int, cotizacion_id: int) -> dict | None:
    """UNA cotización viva de un proyecto vivo, tal como la lee la lista (más `proyecto_nombre` y
    `proyecto_carpeta`), o `None` si no existe, no es de ese proyecto o falta la tabla o alguna columna.
    Una lectura: no pide sesión."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT c.id, c.proyecto_id, c.anio, c.numero, c.estado, c.para_nombre, c.para_telefono, "
                "c.fecha, c.validez_dias, c.contenido, c.creado_en, c.creado_por_chat_id, c.cambiado_en, "
                "c.enviada_en, c.drive_archivo_id, c.drive_version, c.drive_error, "
                "p.nombre AS proyecto_nombre, p.carpeta AS proyecto_carpeta "
                "FROM cotizaciones c JOIN proyectos p ON p.id = c.proyecto_id "
                "WHERE c.id = %s AND c.proyecto_id = %s AND c.borrado_en IS NULL AND p.borrado_en IS NULL",
                (cotizacion_id, proyecto_id))
            fila = await cur.fetchone()
        except Exception as e:
            if _cotizaciones_sin_migrar(e):
                return None
            raise
    return None if fila is None else _cotizacion_leida(fila)


async def marcar_cotizacion_en_drive(proyecto_id: int, cotizacion_id: int, archivo_id: str, version) -> bool:
    """Pone `drive_archivo_id`, `drive_version = version` y borra `drive_error`, SOLO si `cambiado_en` sigue
    siendo `version` (la que se subió). `True` si escribió; `False` si la cotización cambió mientras se subía
    (o ya no es una enviada viva): entonces no se marca nada."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(SENTENCIA_DE_MARCAR_EN_DRIVE, (archivo_id, version, cotizacion_id, proyecto_id, version))
        return await cur.fetchone() is not None


async def marcar_error_de_drive(proyecto_id: int, cotizacion_id: int, causa: str, version) -> bool:
    """Deja la `causa` (una frase fija) del intento que falló, SOLO si `cambiado_en` sigue siendo `version`: una
    versión más nueva tiene su propio intento. No toca `drive_archivo_id` ni `drive_version`."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(SENTENCIA_DE_ERROR_DE_DRIVE, (causa, cotizacion_id, proyecto_id, version))
        return await cur.fetchone() is not None


# Al rehacer la carpeta del proyecto, los archivos de sus cotizaciones (que estaban en la carpeta muerta) dejan de valer:
# se sueltan una por una, cada una con su candado `cambiado_en` (una que alguien corrigió en medio no se toca).
SENTENCIA_DE_SOLTAR_DRIVE = (
    "UPDATE cotizaciones SET drive_archivo_id = NULL, drive_version = NULL, drive_error = NULL "
    "WHERE id = %s AND proyecto_id = %s AND cambiado_en = %s AND estado = 'enviada' AND borrado_en IS NULL "
    "RETURNING id")


async def rehacer_carpetas_de_drive(proyecto_id: int, carpeta_cotizaciones_vieja: str, carpeta_proyecto_id: str,
                                    carpeta_cotizaciones_id: str, nombre_puesto: str,
                                    cotizaciones: list[tuple]) -> bool:
    """En UNA transacción: cambia las carpetas anotadas del proyecto por las nuevas, solo si la de cotizaciones anotada
    sigue siendo `carpeta_cotizaciones_vieja` (nadie las rehízo en medio), y suelta (`drive_archivo_id`, `drive_version`
    y `drive_error` en NULL) cada cotización de `cotizaciones` (pares `(id, cambiado_en)`), que queda «No está en
    Drive». `False` si las anotadas ya no eran las viejas: no se escribe nada."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "UPDATE drive_de_proyectos SET carpeta_proyecto_id = %s, carpeta_cotizaciones_id = %s, nombre_puesto = %s "
            "WHERE proyecto_id = %s AND carpeta_cotizaciones_id = %s RETURNING proyecto_id",
            (carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto, proyecto_id, carpeta_cotizaciones_vieja))
        if await cur.fetchone() is None:
            return False
        for cid, version in cotizaciones:
            await cur.execute(SENTENCIA_DE_SOLTAR_DRIVE, (cid, proyecto_id, version))
        return True


async def proyecto_para_la_carpeta(proyecto_id: int) -> dict | None:
    """El proyecto VIVO (`id`, `nombre`), o `None` si no existe o está en la papelera. La lectura mínima de
    `cotizaciones_drive.preparar_la_carpeta`: lo único que hace falta para crear su carpeta en Drive. Una lectura."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id, nombre FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        fila = await cur.fetchone()
    return None if fila is None else dict(fila)


async def proyecto_para_el_aviso(proyecto_id: int) -> dict | None:
    """El proyecto VIVO como `{id, nombre, estado, area}`, o `None` si no existe o está en la papelera. La lectura mínima
    de los avisos que nombran a OTRO proyecto («el cliente tiene un proyecto activo»): la pantalla decide con ella si
    el nombre y el enlace «Abrir» todavía dicen la verdad. Una lectura."""
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT id, nombre, estado, area FROM proyectos WHERE id = %s AND borrado_en IS NULL",
                          (proyecto_id,))
        fila = await cur.fetchone()
    return None if fila is None else dict(fila)


async def carpetas_de_drive(proyecto_id: int) -> dict | None:
    """Las carpetas de Drive del proyecto (`carpeta_proyecto_id`, `carpeta_cotizaciones_id`, `nombre_puesto`)
    o `None` si todavía no tiene. Si falta la tabla SUBE la excepción: quien sube no puede crear carpetas sin
    poder anotarlas. Una lectura."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto "
            "FROM drive_de_proyectos WHERE proyecto_id = %s", (proyecto_id,))
        fila = await cur.fetchone()
    return None if fila is None else dict(fila)


async def guardar_carpetas_de_drive(proyecto_id: int, carpeta_proyecto_id: str, carpeta_cotizaciones_id: str,
                                    nombre_puesto: str) -> dict:
    """Anota las dos carpetas que se acaban de crear y devuelve las que QUEDARON: si otro intento se adelantó
    (una fila por proyecto) gana el primero y se devuelven las suyas (`ON CONFLICT DO NOTHING`)."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "INSERT INTO drive_de_proyectos (proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto) VALUES (%s, %s, %s, %s) ON CONFLICT (proyecto_id) DO NOTHING",
            (proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto))
        await cur.execute(
            "SELECT proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto "
            "FROM drive_de_proyectos WHERE proyecto_id = %s", (proyecto_id,))
        return dict(await cur.fetchone())


async def poner_nombre_de_carpeta_de_drive(proyecto_id: int, nombre_puesto: str) -> None:
    """Anota el nombre con que quedó puesta la carpeta del proyecto en Drive, después de renombrarla allá."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("UPDATE drive_de_proyectos SET nombre_puesto = %s WHERE proyecto_id = %s",
                          (nombre_puesto, proyecto_id))


SENTENCIA_DE_LA_QUE_TOCA_DE_COTIZACION = "SELECT ultimo FROM serie_cotizaciones WHERE anio = %s"


async def numero_que_sigue_de_cotizacion(anio: int | None = None) -> int | None:
    """El número que se le pondría a la próxima cotización del año (`ultimo + 1`, o 1 si la serie no ha
    empezado), o `None` si falta la tabla. SOLO LEE: la ventana de «Nueva cotización» lo enseña como
    «número automático»; el de verdad se saca al guardar (`siguiente_numero_de_cotizacion`) y puede ser
    otro si alguien guarda antes."""
    anio = hoy_rd().year if anio is None else anio
    if not _anio_de_cotizacion_que_vale(anio):
        raise ValueError("el año de la serie no es un año")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(SENTENCIA_DE_LA_QUE_TOCA_DE_COTIZACION, (anio,))
            fila = await cur.fetchone()
        except Exception as e:
            if _tabla_ausente(e):
                return None
            raise
    return (int(fila["ultimo"]) if fila else 0) + 1


# ── AGREGAR TAREAS A UN PROYECTO (pieza 2 del diseño «proyectos», 29-sep-2026) ─
#
# UNA SOLA PUERTA para «¿a este proyecto se le pueden agregar tareas?»:
# `proyecto_admite_tareas`. La llaman TODOS los que crean una tarea con proyecto
# (`crear_tarea_desde_el_panel`, `cerrar_y_derivar` para las derivadas y
# `crud.crear_desde_interpretacion` por Telegram); la lista no está tecleada
# acá: la saca de lo real `tests/test_tarea_en_proyecto.py`.
#
# Decisión de la sala (delegada por Tiziano, 29-sep): un proyecto CERRADO no
# recibe tareas; uno PAUSADO sí.
ESTADO_PROYECTO_CERRADO = "cerrado"
ESTADO_PROYECTO_PAUSADO = "pausado"

# EL VOCABULARIO CERRADO DE `proyectos.estado` (Lucy 1.0, E2, G9). Hasta aquí
# `estado` era texto libre: `crud.editar` guardaba lo que dijera el modelo
# («terminado», «Cerrado », ...) y un proyecto con un estado que nadie reconoce
# no estaba ni cerrado ni abierto. La puerta es `estado_de_proyecto_que_vale`
# y entra en `crud.PUERTAS["proyectos"]["estado"]`: la pasan `crud.editar`
# (Telegram y panel) y `crud.deshacer`. Los demás escritores del estado no lo
# nombran: los INSERT de `proyectos` (`crear_proyecto` y
# `convertir_tarea_en_proyecto`) lo dejan al DEFAULT 'activo' de la base.
ESTADOS_PROYECTO = ("activo", ESTADO_PROYECTO_PAUSADO, ESTADO_PROYECTO_CERRADO)


def cuenta_como_activo(estado) -> bool:
    """¿Este estado de proyecto cuenta como ACTIVO? (Diseño «Un proyecto no se
    crea sin cliente», §2.1-bis.) UNA sola pregunta, y la hacen todos: «un solo
    proyecto activo por cliente», la comparación de nombres y la búsqueda de
    Telegram. Un pausado y un cerrado NO cuentan (Tiziano, 18-oct-2026: «un
    proyecto en pausa deja abrir otro»). Un estado FUERA del vocabulario (la
    columna es texto libre y una escritura vieja pudo dejar otro) SÍ cuenta:
    lo no clasificado cae del lado estricto. Por eso se pregunta por lo que NO
    cuenta, y no por `estado == 'activo'`."""
    return estado not in (ESTADO_PROYECTO_PAUSADO, ESTADO_PROYECTO_CERRADO)


def estado_de_proyecto_que_vale(valor) -> str:
    """El estado limpio que va a quedar, o ValueError diciendo cuáles hay.
    Sin los espacios de alrededor y en minúscula; fuera del vocabulario (o
    vacío, o no texto) se rechaza. El mensaje no repite lo pedido."""
    if isinstance(valor, str) and valor.strip().lower() in ESTADOS_PROYECTO:
        return valor.strip().lower()
    raise ValueError(
        "el estado de un proyecto es uno de: " + ", ".join(ESTADOS_PROYECTO))


class ProyectoNoAdmiteTareas(ValueError):
    """`clave` dice por qué (`no_existe` o `cerrado`) para que una pantalla lo
    traduzca sin adivinar por el texto. El mensaje no repite nada de lo pedido."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


class TareaNoValida(ValueError):
    """`crear_tarea_desde_el_panel` rechazó un dato del alta (`clave`:
    `responsable`, `persona` o `primero`, las mismas de la pantalla). La ruta ya
    los miró; esto llega si cambiaron entre su chequeo y la escritura."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


async def proyecto_admite_tareas(cur, proyecto_id) -> dict:
    """La fila `{id, nombre, area, estado}` del proyecto si se le pueden agregar
    tareas, o `ProyectoNoAdmiteTareas`. `cur` es un cursor con `dict_row`.

    Un id que no es un número, que no existe o que está en la papelera es
    `no_existe`; uno cerrado es `cerrado` (`estado` es texto libre en la base:
    se compara contra `ESTADO_PROYECTO_CERRADO`, lo único que se prohíbe).
    """
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        raise ProyectoNoAdmiteTareas("no_existe", "ese proyecto no existe o ya no está")
    await cur.execute(
        "SELECT id, nombre, area, estado FROM proyectos "
        "WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
    fila = await cur.fetchone()
    if fila is None:
        raise ProyectoNoAdmiteTareas("no_existe", "ese proyecto no existe o ya no está")
    if fila["estado"] == ESTADO_PROYECTO_CERRADO:
        raise ProyectoNoAdmiteTareas(
            "cerrado", "ese proyecto está cerrado: no se le agregan tareas")
    return fila


async def proyecto_para_tareas(proyecto_id) -> dict:
    """`proyecto_admite_tareas` con su propia conexión, para la ruta y para
    Telegram (que no tienen cursor)."""
    async with pool.connection() as conn:
        return await proyecto_admite_tareas(
            conn.cursor(row_factory=dict_row), proyecto_id)


def sala_ve(responsable_chat_id, area_efectiva) -> bool:
    """¿La sala de control ve esta tarea? Solo si es de Code Y su área EFECTIVA
    (la propia o la de su proyecto) es la técnica: la misma condición que pone
    `tareas_de_code_pendientes` en su SQL (`tests/test_tarea_en_proyecto.py` la
    contrasta ejecutando esa consulta)."""
    return responsable_chat_id == CHAT_ID_CODE and area_efectiva == AREA_TECNICA


# Cuántos ids de `?sin_cerrar=` mira la pantalla como máximo.
TOPE_SIN_CERRAR = 20


async def tareas_sin_cerrar_por_proyecto_cerrado(ids) -> list[int]:
    """De los `ids` que dice la URL de /tareas (`?sin_cerrar=`), los que de
    verdad SIGUEN pendientes, viven y están en un proyecto CERRADO: los únicos de
    los que la pantalla puede decir «no se cerró porque su proyecto está
    cerrado». Sin repetidos, en orden, y como mucho `TOPE_SIN_CERRAR`. Una
    consulta por id (SQL literal, sin armar el texto al vuelo): son pocos."""
    limpios = list(dict.fromkeys(
        i for i in ids if isinstance(i, int) and not isinstance(i, bool) and i > 0
    ))[:TOPE_SIN_CERRAR]
    salida = []
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        for i in limpios:
            await cur.execute(
                """
                SELECT t.id FROM tareas t
                  JOIN proyectos p ON p.id = t.proyecto_id
                 WHERE t.id = %s AND t.borrado_en IS NULL AND t.estado = %s
                   AND p.borrado_en IS NULL AND p.estado = %s
                """,
                (i, ESTADO_PENDIENTE, ESTADO_PROYECTO_CERRADO))
            if await cur.fetchone() is not None:
                salida.append(i)
    return salida


async def personas_vivas() -> list[dict]:
    """`[{id, nombre}]` de las personas no archivadas, para elegir «de quién
    trata» una tarea."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, nombre FROM personas WHERE borrado_en IS NULL "
            "ORDER BY nombre, id")
        return list(await cur.fetchall())


async def _persona_viva(cur, persona_id) -> bool:
    if isinstance(persona_id, bool) or not isinstance(persona_id, int):
        return False
    await cur.execute(
        "SELECT id FROM personas WHERE id = %s AND borrado_en IS NULL",
        (persona_id,))
    return await cur.fetchone() is not None


async def persona_viva(persona_id) -> bool:
    """¿Existe esa persona y no está archivada? La puerta de `persona_id`."""
    async with pool.connection() as conn:
        return await _persona_viva(conn.cursor(row_factory=dict_row), persona_id)


async def _buscar_o_crear(tabla: str, nombre: str, *,
                          bandeja_id: int | None = None) -> int | None:
    """Devuelve el id de la persona con ese nombre; la crea si no está.

    Sin esto, "Ana", "ana" y "Ana García" serían tres personas distintas y la
    consulta "¿cuándo vi a Ana por última vez?" del req 10 devolvería un tercio
    de la verdad. Por eso la búsqueda es insensible a mayúsculas y acentos
    (unaccent no está garantizado, así que comparamos en minúsculas) y mira
    también los alias.

    SOLO PARA `personas`. Hasta la pieza 1 de «Un proyecto no se crea sin
    cliente» (18-oct-2026) también creaba proyectos con un INSERT genérico, de
    tabla armada al vuelo, que no podía traer cliente ni nombre del cliente; esa rama se
    borró junto con `buscar_o_crear_proyecto`, y cualquier otra `tabla` es un
    error de programación. UN PROYECTO NACE SOLO por `crear_proyecto` y
    `convertir_tarea_en_proyecto`.

    `personas` NO lleva huella -- no se pidió, y agregarla sería un segundo
    problema resuelto en el mismo cambio (Regla 15 de la sala: uno a la vez).
    """
    if tabla != "personas":
        raise ValueError(f"_buscar_o_crear solo crea personas, no {tabla!r}")
    nombre = (nombre or "").strip()
    if not nombre:
        return None

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id FROM personas
             WHERE borrado_en IS NULL
               AND (lower(nombre) = lower(%s)
                    OR lower(%s) = ANY(SELECT lower(a) FROM unnest(alias) a))
             LIMIT 1
            """,
            (nombre, nombre),
        )
        fila = await cur.fetchone()
        if fila:
            return fila["id"]

        # `bandeja_id` (§E, 27-sep-2026): la puerta de dueño de la parte
        # A decide "esto es de Tiziano" por la bandeja que originó la
        # fila -- INSERT literal, para que el censo de `tests/test_duenos.py`
        # la encuentre por el AST sin tener que reconstruir un f-string.
        #
        # CON UN SAVEPOINT (`async with conn.transaction():`), igual que
        # `tareas_de_code_pendientes`/`tomar_tarea_de_la_sala`: SIN esto,
        # el primer INSERT que revienta con SQLSTATE 42703 deja la
        # transacción ABORTADA en Postgres real (autocommit=False, sin
        # savepoint de por medio) -- el segundo INSERT del "except"
        # correría en la MISMA transacción envenenada y reventaría con
        # 25P02 ("current transaction is aborted"), no con la persona
        # creada. Hallazgo del testigo sobre `cbc2726`, NO PASA: la
        # primera versión de este arreglo pasaba las pruebas SQLite
        # (que no modelan el aborto de la conexión) pero reventaba
        # contra el comportamiento real de Postgres.
        try:
            async with conn.transaction():
                await cur.execute(
                    "INSERT INTO personas (nombre, bandeja_id) VALUES (%s, %s) "
                    "RETURNING *",
                    (nombre, bandeja_id),
                )
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            # SIN LA MIGRACIÓN (§E sin aplicar): cae al INSERT de antes,
            # sin bandeja_id -- no revienta, la persona se crea igual.
            # El SAVEPOINT de arriba ya revirtió: esta conexión sigue
            # sana.
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "INSERT INTO personas (nombre) VALUES (%s) RETURNING *",
                (nombre,),
            )
        nueva = await cur.fetchone()
        return nueva["id"]


async def buscar_o_crear_persona(nombre: str, *,
                                 bandeja_id: int | None = None) -> int | None:
    return await _buscar_o_crear("personas", nombre, bandeja_id=bandeja_id)


class NombreEnVarios(ValueError):
    """Dos o más proyectos PAUSADOS se llaman igual y ninguno está activo: recibirían tareas los dos, así que
    no se elige uno por su cuenta. `ids` son los candidatos (ordenados). Quien pregunta (Telegram) le pide a
    Tiziano que diga cuál. El mensaje no lleva nombres."""

    def __init__(self, ids):
        super().__init__("hay varios proyectos pausados con ese nombre y ninguno activo")
        self.ids = tuple(ids)


async def proyecto_vivo_por_nombre(nombre: str) -> int | None:
    """El id del proyecto que se llama así (fuera de la papelera), o `None`. **NO CREA NADA.**

    Es la búsqueda que usa Telegram desde E8 (1-oct-2026) y `crud.perfil`. Un proyecto nace SOLO por
    `crear_proyecto` y `convertir_tarea_en_proyecto`: un proyecto que aparece porque alguien lo nombró al vuelo
    nace sin grupo, sin responsable y sin cliente.

    DESDE «UN PROYECTO NO SE CREA SIN CLIENTE» (pieza 1): en CDS y ACD el nombre es el del cliente y puede
    REPETIRSE entre un activo y varios pausados o cerrados, así que ELIGE POR PRIORIDAD (la comparación de
    nombres es la misma, `lower(nombre) = lower(...)`):
      1. el ACTIVO con ese nombre (`cuenta_como_activo`; como mucho uno, el de menor `id` si hubiera más);
      2. si no hay, el PAUSADO (recibe tareas);
      3. si no, el CERRADO de menor `id`, para que el rechazo «ese proyecto está cerrado» siga siendo verdadero y
         no «no existe».
    Si no hay activo y hay DOS O MÁS pausados: `NombreEnVarios(ids)`; nunca elige uno por su cuenta.

    Abre su propia conexión: quien pregunta desde `acciones/crud.py` no está dentro de una transacción en ese
    momento, y meterlo adentro alargaría la transacción de la entidad sin ganar nada.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, estado FROM proyectos
             WHERE borrado_en IS NULL
               AND lower(nombre) = lower(%s)
             ORDER BY id
            """,
            (nombre,))
        filas = [dict(f) for f in await cur.fetchall()]
    if not filas:
        return None
    activos = [f["id"] for f in filas if cuenta_como_activo(f["estado"])]
    if activos:
        return activos[0]
    pausados = [f["id"] for f in filas if f["estado"] == ESTADO_PROYECTO_PAUSADO]
    if len(pausados) > 1:
        raise NombreEnVarios(pausados)
    if pausados:
        return pausados[0]
    return filas[0]["id"]


class ProyectoNoSeCrea(ValueError):
    """Un proyecto nuevo que no vale por algo que no es el nombre. `clave` dice
    qué (`grupo` o `responsable`) para que una pantalla lo traduzca sin adivinar
    por el texto. (El nombre tiene la suya: `NombreDeProyectoNoVale`.)"""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


async def crear_proyecto(nombre: str | None, area: str, responsable_chat_id: int, *,
                         cliente_noco_id: int | None, leer_persona, desde: str = "panel") -> dict:
    """El diálogo «+ Proyecto en X» de la página de proyectos (Lucy 1.0, E5) y el alta por Telegram.
    Crea un proyecto y devuelve su fila. Si no vale, `NombreDeProyectoNoVale` (por el nombre),
    `ProyectoNoSeCrea` (por el grupo, el responsable, el cliente o el nombre) o `ClienteConProyecto`
    (el cliente ya tiene un proyecto activo), todas con ValueError; si Noco no contesta sube la excepción de
    `leer_persona` (`noco_lectura.NocoNoContesta`) y NO se escribe nada.

    TODO LO QUE DECIDE ESTÁ AQUÍ, UNA VEZ, Y NO EN LA RUTA:
      · el cliente (pieza 1 de «Un proyecto no se crea sin cliente», 18-oct-2026): `cliente_noco_id` y
        `leer_persona` van SIN valor por omisión, para que ningún camino pueda olvidarlos. Si el grupo lo exige
        (`cliente_es_obligatorio`: todo menos IA) y no vino, `ProyectoNoSeCrea('cliente')`. Si vino, la ficha se
        VUELVE A LEER de Noco con `leer_persona` ANTES de abrir la conexión (`_ficha_de_noco_que_vale`, la misma
        de `poner_cliente`): Noco sin contestar, una ficha que no existe o un Id que no es un número positivo
        no dejan ninguna fila ni huella. El nombre del cliente que se guarda es el que Noco devuelve.
      · el nombre, por `nombre_que_toca`: en CDS/ACD es el nombre del cliente (y `nombre` puede venir `None`; uno
        distinto es `ProyectoNoSeCrea('nombre')`); en IA es el pedido, por `nombre_de_proyecto_que_vale`.
        Que no haya otro proyecto ACTIVO con ese nombre, con `proyecto_vivo_con_nombre`.
      · un solo proyecto ACTIVO por cliente: `proyecto_puede_quedar_activo` (todo proyecto nuevo nace activo).
      · el grupo, que tiene que ser uno de `areas()`.
      · el responsable, por `puede_ser_responsable` (Rosi, Tiziano o Code): no hay «sin responsable» al crear
        (Tiziano, 1-oct-2026: «somos siempre Rosi, Yo o Code»). Lo que llega tiene que SER un chat.
    La ruta solo TRADUCE; quien la llame sin ella (el Telegram de E8) tiene la misma puerta.

    `desde` dice QUIÉN lo pide y queda en la huella: `"panel"` (el botón de la página, por omisión) o `"lucy"`
    (Telegram, vía `crud.crear_proyecto`). Es un ValueError si es otra cosa. Cada origen tiene su `INSERT` con el
    actor como LITERAL, no como variable, para no romper el guarda de `tests/test_proyectos_panel.py` (el actor
    variable solo viaja por `crud._registrar`).

    UNA SOLA TRANSACCIÓN: el INSERT (con `cliente_noco_id` y `cliente_nombre` en el MISMO insert) y su huella
    `crear` (con lo que quedó guardado en `despues`, así que `crud.deshacer` la revierte con la rama genérica de
    'crear'). No se puede usar `crud._registrar` (`crud` importa este módulo): la fila de `log_acciones` se
    escribe a mano con la misma forma que las otras escrituras de este archivo.

    DEVUELVE LA FILA CON SU `log_id` — el asa para deshacer, que es lo que `cerebro/agente.py::_anotar` necesita.
    """
    if desde not in ("panel", "lucy"):
        raise ValueError(f"origen de proyecto desconocido: {desde!r}")
    obligatorio = cliente_es_obligatorio(area)
    pedido = nombre
    if not obligatorio:
        nombre = nombre_de_proyecto_que_vale(nombre)
    if area not in {a["clave"] for a in await areas()}:
        raise ProyectoNoSeCrea("grupo", "ese grupo no existe")
    if (isinstance(responsable_chat_id, bool) or not isinstance(responsable_chat_id, int)
            or not puede_ser_responsable(responsable_chat_id)):
        raise ProyectoNoSeCrea("responsable", "ese chat no puede ser responsable")
    cliente_id, cliente_nombre = None, None
    if cliente_noco_id is None:
        if obligatorio:
            raise ProyectoNoSeCrea("cliente", "un proyecto de este grupo necesita cliente")
    else:
        # ANTES de abrir la conexión: si Noco no contesta, no hay nada que deshacer.
        try:
            cliente_id, cliente_nombre = await _ficha_de_noco_que_vale(
                cliente_noco_id, leer_persona, "el cliente")
        except ValueError as e:
            raise ProyectoNoSeCrea("cliente", str(e)) from e
    nombre = nombre_que_toca(area, cliente_nombre, pedido if obligatorio else nombre)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await proyecto_puede_quedar_activo(
            cur, None, None,
            {"estado": "activo", "area": area, "cliente_noco_id": cliente_id, "nombre": nombre,
             "borrado_en": None})
        if await proyecto_vivo_con_nombre(cur, nombre) is not None:
            raise NombreDeProyectoNoVale(
                "repetido", "ya hay otro proyecto con ese nombre")
        await cur.execute(
            """
            INSERT INTO proyectos (nombre, area, responsable_chat_id, cliente_noco_id, cliente_nombre)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (nombre, area, responsable_chat_id, cliente_id, cliente_nombre))
        # Nace con `inicio` = hoy (parte 4): `_con_su_inicio` es la única que lo escribe al crear.
        nuevo = await _con_su_inicio(conn, cur, await cur.fetchone())
        # EL ASA, EN LA MISMA FILA QUE SE DEVUELVE. La huella ya se escribía;
        # lo que faltaba era PODER NOMBRARLA. Quien crea un proyecto desde
        # Telegram (E8) tiene que poder decir «acción #N, reversible» y dejar
        # el botón de deshacer — `cerebro/agente.py::_anotar` no anota nada sin
        # un id—, y para eso necesita el id. Va como una clave más de la fila, y
        # no como un segundo valor de retorno, para no cambiarle la firma a
        # quien ya la llama: la ruta del panel la ignora.
        #
        # Es el mismo `RETURNING id` que ya usa `convertir_tarea_en_proyecto`
        # unas líneas más abajo, sobre el MISMO INSERT: ni una escritura más ni
        # una transacción distinta. Y va con `cur` (el cursor con
        # `row_factory=dict_row`) y no con `conn.execute`, porque el id se lee
        # POR NOMBRE y `conn.execute` devuelve un cursor con la factoría de la
        # conexión, que en SQLite son tuplas — el mismo motivo por el que
        # `convertir_tarea_en_proyecto` lee su `RETURNING id` con `cur`.
        huella = (nuevo["id"], json.dumps(nuevo, default=str, ensure_ascii=False))
        if desde == "lucy":
            await cur.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo)
                VALUES ('lucy', 'crear', 'proyectos', %s, NULL, %s,
                        'Proyecto nuevo, creado por Telegram')
                RETURNING id
                """, huella)
        else:
            await cur.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo)
                VALUES ('panel', 'crear', 'proyectos', %s, NULL, %s,
                        'Proyecto nuevo, creado desde el panel de proyectos')
                RETURNING id
                """, huella)
        nuevo["log_id"] = (await cur.fetchone())["id"]
        return nuevo


async def convertir_tarea_en_proyecto(tarea_id: int, *, cliente_noco_id: int | None, leer_persona) -> dict:
    """El botón «convertir en proyecto» (encargo 5). Una tarea SUELTA se
    convierte en un proyecto propio. Devuelve
    `{proyecto_id, proyecto_nombre, log_id_proyecto, log_id_tarea}`.

    SOLO CALIFICA una tarea pendiente y sin proyecto -- ValueError con el
    motivo si no. Pendiente, porque las 12 que motivan esto lo son, y
    convertir algo ya cerrado no tiene con qué llenarse (¿un proyecto
    "hecho" desde el minuto cero?). Sin proyecto, porque una tarea CON
    proyecto ya es parte de uno -- convertirla otra vez no dice nada nuevo y
    dejaría un proyecto con una sola tarea adentro que a su vez es "de" otro
    proyecto, sin que nadie lo haya pedido.

    NO SE RESTRINGE POR FECHA. Las 12 tareas que motivan el encargo no tienen
    fecha, pero nada en el pedido dice que haga falta no tenerla -- si mañana
    hace falta convertir una CON fecha, la fecha simplemente se pierde (ver
    abajo), igual que se pierde hoy sin este botón si alguien la reescribe a
    mano. Queda anotado acá por si la decisión correcta es otra: se restringe
    agregando `AND vence_en IS NULL` al `WHERE` de abajo.

    QUÉ PASA CON CADA COSA DE LA TAREA ORIGINAL (decisión de este encargo,
    ninguna le pertenecía a Tiziano -- son todas preguntas con una respuesta
    técnica, no de negocio):
      · TÍTULO Y DETALLE → en IA, `proyectos.nombre` y `proyectos.descripcion`
        (son los mismos dos campos que ya tiene una tarea, con otro nombre).
        EN CDS, ACD Y SIN ÁREA (pieza 1 de «Un proyecto no se crea sin
        cliente», 18-oct-2026) EL NOMBRE ES EL DEL CLIENTE, así que el título ya
        no puede ser el nombre: pasa a la PRIMERA LÍNEA de la descripción y el
        detalle sigue debajo (la tarea archivada sigue recuperable).
      · CLIENTE: `cliente_noco_id` y `leer_persona` van sin valor por omisión,
        como en `crear_proyecto`. Si el área de la tarea lo exige
        (`cliente_es_obligatorio`; sin área también) y no vino, `ProyectoNoSeCrea
        ('cliente')`. La ficha se vuelve a leer de Noco ANTES de la transacción
        que escribe: Noco sin contestar no deja proyecto, ni huella, ni tarea
        archivada. Un solo proyecto ACTIVO por cliente: `ClienteConProyecto`.
      · ÁREA → `proyectos.area`, tal cual la tenía la tarea (una tarea sin
        proyecto puede tener la suya propia, encargo 4). Con esto el
        proyecto nace con la misma área que ya se le había puesto a la
        tarea -- no hay ninguna pregunta nueva que hacerle a `_area_que_vale`
        ni a nada: se copia el valor, y punto.
      · LA TAREA ORIGINAL se ARCHIVA (soft-delete, `borrado_en = now()`) —
        NO se muda adentro del proyecto nuevo como su primera tarea. Una
        tarea titulada "Álbum nuevo" viviendo DENTRO de un proyecto también
        llamado "Álbum nuevo" sería un duplicado que no dice nada; archivarla
        dice "esto dejó de ser una tarea suelta, ahora es el proyecto
        mismo", y sigue estando -- en la papelera, recuperable.
      · RESPONSABLE (`responsable_chat_id`), COMENTARIOS (`comentarios_tarea`)
        Y MICRO-PASOS (`micro_pasos`, encargo 7, por `tarea_id`) → SE QUEDAN
        EN LA TAREA ARCHIVADA, tal cual estaban. `proyectos` no tiene
        ninguna de las tres columnas/tablas -- no hay a dónde migrarlos --
        así que no se pierden (siguen en la fila, que sigue existiendo,
        solo archivada) pero tampoco aparecen en ningún lado nuevo. Si el
        proyecto necesita su propio responsable, su propia conversación o
        su propia lista de chequeo algún día, eso es una pregunta nueva,
        para Tiziano, no una consecuencia de este botón.

    DOS HUELLAS, NO UNA, y las dos con la forma que YA existe en
    `log_acciones` -- nada nuevo que enseñarle a `deshacer()`:
      · `accion='crear', tabla='proyectos'` — deshacerla archiva el proyecto
        (la rama 'crear' de `deshacer()` ya es genérica para cualquier tabla
        de `acciones.crud.TABLAS`, y 'proyectos' ya está ahí).
      · `accion='borrar', tabla='tareas'`, con el `antes` completo — deshacerla
        le quita `borrado_en` a la tarea (la rama 'borrar' de `deshacer()`,
        igual de genérica).
      Las dos en la MISMA transacción que las escrituras que registran, y
      cada motivo nombra al otro lado (el id del proyecto en la huella de la
      tarea, y viceversa) para que se puedan leer juntas en el registro. Un
      "deshacer" completo de esta conversión hoy pide deshacer las DOS
      huellas por separado -- no existe un tercer tipo de acción combinada
      en `log_acciones`, y esta función no lo inventa: usa el vocabulario que
      ya hay.

    `actor='panel'`, como toda escritura que dispara este botón -- no lo
    dispara Lucy. El `bandeja_id` de las dos huellas es el que YA tenía la
    tarea: no hace falta una fila de `bandeja` nueva, la trazabilidad sigue
    siendo la de la tarea original.
    """
    async def _la_tarea(cur) -> dict:
        await cur.execute(
            "SELECT * FROM tareas WHERE id = %s AND borrado_en IS NULL",
            (tarea_id,))
        tarea = await cur.fetchone()
        if tarea is None:
            raise ValueError("Esa tarea no existe o ya está en la papelera.")
        if tarea["estado"] != ESTADO_PENDIENTE:
            raise ValueError(
                "Solo se puede convertir una tarea pendiente en proyecto.")
        if tarea.get("proyecto_id") is not None:
            raise ValueError("Esa tarea ya tiene proyecto: no se convierte.")
        return tarea

    # PRIMERA PASADA, solo lee: qué área tiene la tarea decide si hace falta cliente, y la ficha se pide a Noco
    # ANTES de la transacción que escribe.
    async with pool.connection() as conn:
        tarea = await _la_tarea(conn.cursor(row_factory=dict_row))
    area = tarea.get("area")
    obligatorio = cliente_es_obligatorio(area)
    cliente_id, cliente_nombre = None, None
    if cliente_noco_id is None:
        if obligatorio:
            raise ProyectoNoSeCrea("cliente", "un proyecto de este grupo necesita cliente")
    else:
        try:
            cliente_id, cliente_nombre = await _ficha_de_noco_que_vale(
                cliente_noco_id, leer_persona, "el cliente")
        except ValueError as e:
            raise ProyectoNoSeCrea("cliente", str(e)) from e

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        tarea = await _la_tarea(cur)           # otra vez, ya dentro de la transacción: pudo cambiar en el medio
        if cliente_es_obligatorio(tarea.get("area")) != obligatorio:
            raise ValueError("Esa tarea cambió de grupo mientras se convertía: inténtalo otra vez.")

        # En IA el título de la tarea es el NOMBRE del proyecto: por la misma puerta que un renombre (no vacío,
        # largo, y que no haya ya otro ACTIVO con ese nombre: convertir dos tareas con el mismo título dejaba dos
        # proyectos iguales). En los demás grupos el nombre es el del cliente (`nombre_que_toca`) y el título
        # pasa a la descripción.
        nombre_proyecto = nombre_que_toca(area, cliente_nombre, tarea["titulo"] if not obligatorio else None)
        descripcion = tarea.get("detalle")
        if obligatorio:
            titulo = (tarea.get("titulo") or "").strip()
            descripcion = "\n".join(x for x in (titulo, descripcion) if x and str(x).strip()) or None
        await proyecto_puede_quedar_activo(
            cur, None, None,
            {"estado": "activo", "area": area, "cliente_noco_id": cliente_id, "nombre": nombre_proyecto,
             "borrado_en": None})
        if await proyecto_vivo_con_nombre(cur, nombre_proyecto) is not None:
            raise NombreDeProyectoNoVale(
                "repetido", "Ya hay un proyecto con ese nombre: no se convierte.")

        await cur.execute(
            """
            INSERT INTO proyectos (nombre, descripcion, area, cliente_noco_id, cliente_nombre)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (nombre_proyecto, descripcion, area, cliente_id, cliente_nombre))
        # Nace con `inicio` = hoy (parte 4), por la misma función que los otros dos sitios.
        proyecto = await _con_su_inicio(conn, cur, await cur.fetchone())

        await cur.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'crear', 'proyectos', %s, NULL, %s, %s, %s)
            RETURNING id
            """,
            (proyecto["id"],
             json.dumps(proyecto, default=str, ensure_ascii=False),
             f"Convertida desde la tarea #{tarea_id} (botón del panel)",
             tarea.get("bandeja_id")))
        log_id_proyecto = (await cur.fetchone())["id"]

        await conn.execute(
            "UPDATE tareas SET borrado_en = now() WHERE id = %s", (tarea_id,))

        await cur.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'borrar', 'tareas', %s, %s, NULL, %s, %s)
            RETURNING id
            """,
            (tarea_id,
             json.dumps(tarea, default=str, ensure_ascii=False),
             f"Convertida en el proyecto #{proyecto['id']} "
             f"'{proyecto['nombre']}' (botón del panel)",
             tarea.get("bandeja_id")))
        log_id_tarea = (await cur.fetchone())["id"]

    return {"proyecto_id": proyecto["id"], "proyecto_nombre": proyecto["nombre"],
            "log_id_proyecto": log_id_proyecto, "log_id_tarea": log_id_tarea}


async def choques_de_evento(evento_id: int) -> list[dict]:
    """Los eventos que se pisan en el tiempo con este (req 26: conflictos).

    Corrección de Tiziano (22-jul) sobre el espíritu de esto: Lucy no es
    Natalia — acá la regla es ENSEÑARLE al modelo, no restringirlo. Este
    chequeo no es una reja: es un mueble de la casa. Le acerca el dato del
    choque a Lucy en el momento justo, y ELLA decide qué hacer con él —
    avisarlo, proponer mover una, o preguntarle a Tiziano por Telegram
    (que siempre está).

    Un evento sin termina_en se asume de 1 hora: mejor un choque de más que
    dos citas pisadas en silencio.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT o.id, o.titulo, o.lugar,
                   o.inicia_en AT TIME ZONE 'America/Santo_Domingo' AS inicia_rd
              FROM eventos e
              JOIN eventos o
                ON o.id <> e.id AND o.borrado_en IS NULL
               AND tstzrange(e.inicia_en,
                             coalesce(e.termina_en, e.inicia_en + interval '1 hour'))
                && tstzrange(o.inicia_en,
                             coalesce(o.termina_en, o.inicia_en + interval '1 hour'))
             WHERE e.id = %s AND e.borrado_en IS NULL
             ORDER BY o.inicia_en
            """,
            (evento_id,),
        )
        return await cur.fetchall()


async def guardar_movimiento(mov, bandeja_id: int | None = None,
                             categoria: str | None = None) -> int | None:
    """Guarda un Movimiento parseado de un correo. Devuelve el id, o None si ya estaba.

    None NO es un error: significa que este movimiento ya se había guardado. Pasa
    de verdad — Banco Popular manda la misma transacción dos veces con segundos
    de diferencia — y también cada vez que la ingesta reprocesa un correo tras un
    fallo. Quien llama debe contarlo como "ya visto", no como fallo.

    La huella es `Movimiento.clave_dedupe()` y el que decide es el índice único
    parcial de la migración 001, no una consulta previa: con un SELECT antes del
    INSERT, dos ejecuciones simultáneas de la ingesta se colarían las dos.

    `monto` viaja como str a propósito. La columna es NUMERIC(12,2) y el
    movimiento trae Decimal; pasar por float perdería centavos justo en la
    conversión, que es el único sitio donde este sistema podría perderlos.
    """
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                """
                INSERT INTO movimientos
                  (bandeja_id, tipo, fecha, monto, moneda, contraparte,
                   categoria, referencia, hash_contenido, banco, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (hash_contenido) WHERE hash_contenido IS NOT NULL
                  DO NOTHING
                RETURNING id
                """,
                (bandeja_id, mov.tipo, mov.fecha.date(), str(mov.monto),
                 mov.moneda, mov.contraparte, categoria, mov.referencia,
                 mov.clave_dedupe(), mov.banco, mov.estado),
            )
        except _ERRORES_DE_FILA as e:
            # UN MOVIMIENTO QUE NO SE PUEDE GUARDAR NO DESAPARECE EN SILENCIO.
            #
            # Antes esto subía crudo hasta el `except Exception` del bucle de
            # cerebro/interpretar.py, que lo dejaba en un `log.warning` de
            # Railway y nada más: sin aviso, sin bandeja, y sin llegar a
            # `guardar_estado_consumos` — o sea con el cursor de UID de ese
            # buzón sin avanzar, releyendo el mismo correo malo cada 15 minutos
            # para siempre. La cuenta entera dejaba de ingerir y nadie se
            # enteraba.
            #
            # El mensaje lleva banco/tipo/estado porque es lo que hace falta
            # para saber QUÉ rechazó la base sin ir a buscar la fila (que no
            # existe: no se insertó). No lleva monto ni contraparte: esto
            # termina en un aviso que Lucy lee en voz alta.
            raise MovimientoRechazado(
                f"la base rechazó el movimiento de {mov.banco} "
                f"(tipo={mov.tipo}, estado={mov.estado}): "
                f"{type(e).__name__}: {e}") from e
        fila = await cur.fetchone()
        return fila[0] if fila else None


# ── Ingesta de movimientos bancarios (captura/consumos.py) ───────────────

async def leer_estado_consumos(cuenta: str) -> dict | None:
    """Cursor de la ingesta para una cuenta. None si nunca se ha corrido."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT uidvalidity, ultimo_uid, desde_fecha FROM consumos_estado "
            "WHERE cuenta = %s", (cuenta,))
        return await cur.fetchone()


async def guardar_estado_consumos(cuenta: str, uidvalidity: int,
                                  ultimo_uid: int, desde_fecha,
                                  reiniciar: bool = False) -> None:
    """Avanza el cursor. Nunca retrocede, SALVO que el buzón se haya renumerado.

    El GREATEST está para que dos pasadas solapadas no se pisen: la lenta no
    puede hacer que la rápida vuelva a mirar lo que ya miró. Pero cuando cambia
    el UIDVALIDITY los UID viejos dejan de significar nada, y un GREATEST
    incondicional dejaría el puntero clavado en un número del buzón anterior —
    la cuenta ciega para siempre, en silencio. Por eso `reiniciar` lo reemplaza.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO consumos_estado
              (cuenta, uidvalidity, ultimo_uid, desde_fecha, actualizado_en)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (cuenta) DO UPDATE SET
              uidvalidity    = EXCLUDED.uidvalidity,
              ultimo_uid     = CASE WHEN %s THEN EXCLUDED.ultimo_uid
                                    ELSE GREATEST(consumos_estado.ultimo_uid,
                                                  EXCLUDED.ultimo_uid) END,
              desde_fecha    = EXCLUDED.desde_fecha,
              actualizado_en = now()
            """,
            (cuenta, uidvalidity, ultimo_uid, desde_fecha, reiniciar),
        )


async def listar_cuentas_propias() -> list[dict]:
    """Los patrones que identifican a la casa. Lista vacía si no hay tabla."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT patron FROM cuentas_propias WHERE borrado_en IS NULL")
        return await cur.fetchall()


# ── Consultas del panel web (web/app.py) ─────────────────────────────────
#
# Todas excluyen `borrado_en IS NOT NULL` y, cuando suman dinero, filtran
# `tipo <> 'transferencia'`: un traspaso entre cuentas propias no es gasto ni
# ingreso, y sumarlo fue el error que costaba RD$657,400 al año.
#
# Y filtran `estado <> 'declinada'`, que es plata que el banco rechazó. Este
# comentario decía "`estado` no aplica (la tabla no lo guarda)" — quedó viejo
# el 31-ago-2026, cuando la migración agregó la columna y las cuatro consultas
# de abajo empezaron a usarla. Se corrige acá porque un comentario que niega
# una columna es la forma barata de que el próximo la pase por alto: es
# exactamente lo que le pasó al esquema que ve Lucy en cerebro/consultar.py.
#
# EL MISMO CRITERIO VALE PARA EL SQL QUE ESCRIBE LUCY. Estas consultas y el
# prompt de cerebro/consultar.py contestan LA MISMA pregunta por dos caminos:
# si filtran distinto, Tiziano recibe dos números. El candado que lo sujeta es
# tests/test_esquema_del_modelo.py::test_lucy_y_el_panel_filtran_lo_mismo, que
# lee la fuente de estas cuatro funciones. Cambiar acá el criterio sin cambiar
# el prompt pone ese test en rojo, a propósito.

async def resumen_por_mes(meses: int = 12) -> list[dict]:
    """Gasto e ingreso por mes y moneda.

    Fuera quedan los traspasos —no son gasto, es dinero cambiando de bolsillo—
    y las categorías que NO SUMAN: el dinero de terceros que solo pasa por la
    cuenta. Contarlo inflaba agosto en RD$43,312 de ingreso y RD$41,500 de
    gasto a la vez, así que el neto salía casi bien por casualidad mientras
    cada cifra por separado era falsa.
    """
    from cerebro.bancos.categorias import NO_SUMAN
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT to_char(fecha, 'YYYY-MM') AS mes, moneda, tipo,
                   sum(monto) AS total, count(*) AS n
              FROM movimientos
             WHERE borrado_en IS NULL
               AND tipo <> 'transferencia'
               AND estado <> 'declinada'
               AND coalesce(categoria, '') <> ALL(%s)
               AND fecha >= date_trunc('month', now()) - (%s || ' months')::interval
             GROUP BY 1, 2, 3
             ORDER BY 1 DESC, 2, 3
            """, (list(NO_SUMAN), meses))
        return await cur.fetchall()


async def gasto_por_categoria(mes: str | None = None) -> list[dict]:
    """Gasto por categoría, con la MONEDA como parte de la agrupación.

    La moneda va en el GROUP BY y no se convierte: sumar DOP con USD da un
    número que no significa nada, y este panel existe justo para no cometer ese
    error. Un total de "175,000" que en realidad son 154,000 pesos más 228
    dólares no es un total, es una confusión con formato de número.

    Los traspasos quedan fuera —no son gasto, son dinero cambiando de bolsillo—
    y los ingresos también: no se clasifican.

    Las categorías que NO SUMAN salen igual, con la bandera `no_suma` puesta:
    esconderlas sería otra forma de mentir. Se muestran aparte y no entran en
    el total, que es distinto de no mostrarlas.

    `mes` en formato 'YYYY-MM'; sin él, todo lo que haya.
    """
    from cerebro.bancos.categorias import NO_SUMAN
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT coalesce(nullif(categoria, ''), '— sin clasificar —') AS categoria,
                   moneda, sum(monto) AS total, count(*) AS n,
                   coalesce(categoria, '') = ANY(%s) AS no_suma
              FROM movimientos
             WHERE borrado_en IS NULL AND tipo = 'gasto'
               AND estado <> 'declinada'
               -- El ::text NO es adorno: sin él Postgres no puede deducir el
               -- tipo del parámetro cuando llega NULL y tira
               -- IndeterminateDatatype, que en el panel se ve como un
               -- Internal Server Error en la portada.
               AND (%s::text IS NULL OR to_char(fecha, 'YYYY-MM') = %s)
             -- El 5 es la bandera `no_suma`: se calcula desde `categoria`, y
             -- Postgres exige agruparla igual que a la propia categoría.
             GROUP BY 1, 2, 5
             ORDER BY 2, 3 DESC
            """, (list(NO_SUMAN), mes, mes))
        return await cur.fetchall()


async def crear_gasto_en_efectivo(concepto: str, monto, categoria: str | None,
                                  fecha) -> int:
    """Un gasto pagado en efectivo, cargado a mano desde el panel.

    NO HAY COLUMNA DE MÉTODO DE PAGO, y no hace falta una: `banco` es la
    columna que dice DE DÓNDE SALIÓ LA PLATA, y "efectivo" es una respuesta
    legítima a esa pregunta. Se llama `banco` porque hasta hoy la única fuente
    eran correos de bancos, no porque el concepto sea "banco". Con eso,
    "¿cuánto gasté en efectivo?" se contesta con el filtro que ya existe
    (`movimientos_filtrados(banco="efectivo")`) y la opción aparece sola en el
    desplegable, porque `bancos_usados()` lo arma desde la base.

    `hash_contenido` va NULO y ES LO MÁS IMPORTANTE DE ESTA FILA. Es lo que
    distingue "vino de un correo" de "lo escribió una persona", y por eso deja
    la fila fuera de los contadores de ingesta de /salud
    (`salud_ingesta`, `silencio_por_banco`) y fuera de `posibles_duplicados`,
    las tres consultas que filtran por `hash_contenido IS NOT NULL`. Un gasto
    en efectivo no dice nada sobre si la lectura de correos funciona.

    Sin guarda de duplicados, al revés que el alta por Telegram
    (`acciones/crud.py`): ahí el riesgo es chocar con un movimiento que el
    correo del banco ya trajo, y acá eso no puede pasar porque NINGÚN BANCO
    MANDA UN CORREO POR UN PAGO EN EFECTIVO. Dos cafés de RD$150 el mismo día
    son normales, y el 303 del endpoint ya evita el duplicado por refrescar.

    `monto` viaja como str por el mismo motivo que en `guardar_movimiento()`:
    la columna es NUMERIC(12,2) y pasar por float perdería centavos justo en
    la conversión.

    La fila y su huella en log_acciones se escriben en el MISMO bloque de
    conexión, o sea en la misma transacción, igual que `a_la_papelera()`: o
    entran las dos o no entra ninguna. Un movimiento sin su línea de log es un
    movimiento que no se puede deshacer, y el deshacer de este proyecto ES
    log_acciones.

    La acción se registra como 'crear' y no como otra palabra porque es lo que
    `deshacer()` sabe revertir: su rama de 'crear' hace
    `SET borrado_en = now()`, o sea lo manda a la papelera.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            INSERT INTO movimientos
              (bandeja_id, tipo, fecha, monto, moneda, contraparte,
               categoria, referencia, hash_contenido, banco)
            VALUES (NULL, 'gasto', %s, %s, 'DOP', %s, %s, NULL, NULL,
                    'efectivo')
            RETURNING *
            """,
            (fecha, str(monto), concepto, categoria or None))
        # RETURNING * y no una fila reconstruida a mano: `despues` tiene que
        # ser lo que de verdad quedó guardado —con el id, el creado_en y los
        # defaults que puso Postgres—, no lo que creíamos estar mandando.
        fila = await cur.fetchone()
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'crear', 'movimientos', %s, NULL, %s,
                    'gasto en efectivo cargado desde el panel')
            """,
            (fila["id"],
             json.dumps(fila, default=str, ensure_ascii=False)))
        return fila["id"]


DIAS_EN_PAPELERA = 30


async def papelera() -> list[dict]:
    """Lo borrado que todavía se puede recuperar, con los días que le quedan."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, fecha, banco, tipo, monto, moneda, contraparte,
                   categoria, borrado_en,
                   %s - EXTRACT(DAY FROM now() - borrado_en)::int AS dias
              FROM movimientos
             WHERE borrado_en IS NOT NULL
             ORDER BY borrado_en DESC
            """, (DIAS_EN_PAPELERA,))
        return await cur.fetchall()


async def a_la_papelera(movimiento_id: int, motivo: str = "borrado desde el panel") -> bool:
    """Saca un movimiento de las listas sin destruirlo. Devuelve si tocó algo."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT * FROM movimientos WHERE id = %s AND borrado_en IS NULL",
            (movimiento_id,))
        antes = await cur.fetchone()
        if antes is None:
            return False
        await conn.execute(
            "UPDATE movimientos SET borrado_en = now() WHERE id = %s",
            (movimiento_id,))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'borrar', 'movimientos', %s, %s, %s, %s)
            """,
            (movimiento_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"borrado_en": "ahora"}), motivo))
        return True


async def restaurar(movimiento_id: int) -> bool:
    """Lo saca de la papelera y vuelve a las listas."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE movimientos SET borrado_en = NULL "
            "WHERE id = %s AND borrado_en IS NOT NULL RETURNING id",
            (movimiento_id,))
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'movimientos', %s, %s, %s,
                    'restaurado desde la papelera')
            """,
            (movimiento_id, json.dumps({"borrado_en": "tenía fecha"}),
             json.dumps({"borrado_en": None})))
        return True


async def vaciar_papelera(dias: int = DIAS_EN_PAPELERA) -> int:
    """Borra DE VERDAD lo que lleva más de `dias` en la papelera.

    Esto rompe a propósito el pilar "nunca DELETE real" que este módulo
    declara, y por eso hay dos condiciones que no se negocian:

    1. Corre DESPUÉS del respaldo nocturno y solo si el respaldo se verificó.
       Así lo que se destruye está siempre dentro de al menos una copia buena.
       Borrar antes de respaldar sería la única forma de perder algo de verdad.
    2. La fila de log_acciones NO se toca. Ahí queda el `antes` completo en
       JSON, así que incluso después del DELETE hay rastro de qué había y quién
       lo borró — lo que se pierde es la fila viva, no la memoria.

    Treinta días es la promesa que se le hizo a Tiziano: sale de la lista al
    instante, se puede recuperar un mes, y después se va.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM movimientos "
            " WHERE borrado_en IS NOT NULL "
            "   AND borrado_en < now() - make_interval(days => %s) "
            "RETURNING id", (dias,))
        return len(await cur.fetchall())


async def posibles_duplicados() -> list[dict]:
    """Pares que huelen a la misma operación contada dos veces.

    Mismo banco, mismo MINUTO, mismo monto y misma moneda, con la contraparte
    escrita distinta. Pasa cuando un banco manda dos correos por una sola
    operación: BHD avisa del "Pago de Servicio" Y lo repite en su alerta
    genérica de transacciones, escribiendo el comercio de dos formas
    ("ALTICE HOGAR" y "Tricom - IB BHDLeon"). El dedupe no los junta porque la
    huella incluye la contraparte.

    SE MUESTRA, NO SE FUSIONA. Medido sobre los 466 movimientos del corpus
    entero: UN caso. Con n=1 no hay evidencia para una regla automática, y
    equivocarse borrando pierde un gasto real EN SILENCIO — que es peor que
    contarlo dos veces, porque contarlo dos veces se ve. Que decida una persona.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT split_part(hash_contenido, '|', 1) AS banco,
                   split_part(hash_contenido, '|', 2) AS cuando,
                   monto, moneda,
                   array_agg(id ORDER BY id)          AS ids,
                   array_agg(contraparte ORDER BY id) AS contrapartes
              FROM movimientos
             WHERE borrado_en IS NULL AND hash_contenido IS NOT NULL
             GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT contraparte) > 1
             ORDER BY 3 DESC
            """)
        return await cur.fetchall()


async def gastos_de_cada_categoria(mes: str | None = None) -> list[dict]:
    """Los gastos uno por uno, para poder abrir una categoría y ver qué hay.

    Se traen TODOS los del mes en una sola consulta y se agrupan en memoria, en
    vez de una consulta por categoría cuando alguien despliega: son ~130 filas y
    la alternativa es abrir la base cada vez que se hace clic.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, fecha, banco, contraparte, monto, moneda,
                   coalesce(nullif(categoria, ''), '— sin clasificar —') AS categoria
              FROM movimientos
             WHERE borrado_en IS NULL AND tipo = 'gasto'
               AND estado <> 'declinada'
               AND (%s::text IS NULL OR to_char(fecha, 'YYYY-MM') = %s)
             ORDER BY monto DESC
            """, (mes, mes))
        return await cur.fetchall()


async def meses_con_movimientos() -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT to_char(fecha, 'YYYY-MM') FROM movimientos "
            "WHERE borrado_en IS NULL ORDER BY 1 DESC")
        return [r[0] for r in await cur.fetchall()]


async def sin_clasificar(limite: int = 100) -> list[dict]:
    """La cola de corrección: los GASTOS que entraron sin categoría.

    Es la pantalla que paga el panel — cada corrección acá es una regla que el
    sistema aprende. Se ordena por monto: si solo se van a corregir diez, que
    sean los diez que más pesan.

    Solo gastos, por decisión de Tiziano: el dinero que entra no hace falta
    clasificarlo. Meter los ingresos acá no aportaba nada y sí quitaba — cada
    ingreso sin categoría empujaba hacia abajo un gasto que sí hay que mirar, y
    la cola vale exactamente por lo que uno alcanza a corregir antes de
    aburrirse. Los traspasos quedan fuera por lo mismo y desde antes: no son
    gasto, son dinero cambiando de bolsillo.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, fecha, tipo, monto, moneda, contraparte, referencia, banco
              FROM movimientos
             WHERE borrado_en IS NULL
               AND (categoria IS NULL OR categoria = '')
               AND tipo = 'gasto'
               -- Una compra rechazada no hay que clasificarla: no pasó. Una
               -- retención SÍ: para las tarjetas en dólares es el único aviso
               -- que manda el banco, así que ese gasto es real.
               AND estado <> 'declinada'
             ORDER BY monto DESC
             LIMIT %s
            """, (limite,))
        return await cur.fetchall()


async def bancos_usados() -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT banco FROM movimientos WHERE borrado_en IS NULL "
            "AND banco IS NOT NULL ORDER BY 1")
        return [r[0] for r in await cur.fetchall()]


async def movimientos_filtrados(desde=None, hasta=None, tipo: str | None = None,
                                categoria: str | None = None,
                                banco: str | None = None,
                                codigo: int | None = None,
                                limite: int = 300) -> list[dict]:
    """El detalle, filtrable. `codigo` es el id del movimiento —el M-0086 que
    muestra el panel— y cuando viene, manda sobre todo lo demás: buscar uno
    concreto y que los otros filtros lo escondan sería la forma más rápida de
    hacer creer que no existe."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, fecha, tipo, monto, moneda, contraparte, categoria,
                   referencia, banco, estado
              FROM movimientos
             WHERE borrado_en IS NULL
               AND (%s::date IS NULL OR fecha >= %s::date)
               AND (%s::date IS NULL OR fecha <= %s::date)
               AND (%s::text IS NULL OR tipo = %s::text)
               AND (%s::text IS NULL OR categoria = %s::text)
               AND (%s::text IS NULL OR banco = %s::text)
               AND (%s::bigint IS NULL OR id = %s::bigint)
             ORDER BY fecha DESC, id DESC
             LIMIT %s
            """, (desde, desde, hasta, hasta, tipo, tipo, categoria, categoria,
                  banco, banco, codigo, codigo, limite))
        return await cur.fetchall()


async def categorias_usadas() -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT categoria FROM movimientos "
            "WHERE borrado_en IS NULL AND categoria IS NOT NULL "
            "AND categoria <> '' ORDER BY 1")
        return [r[0] for r in await cur.fetchall()]


async def salud_ingesta() -> dict:
    """Lo que hace falta para creerle al panel: cuándo miró por última vez y
    si hay algo entrando. Un panel que no dice desde cuándo no sabe nada es un
    panel que miente por omisión."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT cuenta, ultimo_uid, actualizado_en FROM consumos_estado "
            "ORDER BY cuenta")
        cuentas = await cur.fetchall()
        cur2 = await conn.execute(
            "SELECT count(*), max(creado_en) FROM movimientos "
            "WHERE borrado_en IS NULL AND hash_contenido IS NOT NULL")
        n, ultimo = await cur2.fetchone()
        cur3 = await conn.execute(
            "SELECT count(*) FROM cuentas_propias WHERE borrado_en IS NULL")
        propios = (await cur3.fetchone())[0]
        return {"cuentas": cuentas, "automaticos": n, "ultimo": ultimo,
                "patrones_propios": propios}


async def silencio_por_banco() -> list[dict]:
    """Cuántos movimientos automáticos lleva cada banco y cuándo entró el último.

    Es PASIVA a propósito: se muestra en /salud y no empuja ningún aviso. Sin
    una expectativa declarada por Tiziano —cada cuánto "debería" llegar algo de
    cada banco—, ponerle un umbral sería inventarse la segunda alarma que grita
    en falso. Lo que sí puede hacer es que, cuando él mire, la respuesta a
    "¿hace cuánto que no entra nada del BHD?" esté escrita.

    Solo los automáticos (`hash_contenido IS NOT NULL`): un movimiento cargado a
    mano no dice nada sobre si la ingesta de ese banco funciona.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT banco, count(*) AS n, max(creado_en) AS ultimo
              FROM movimientos
             WHERE borrado_en IS NULL AND hash_contenido IS NOT NULL
               AND banco IS NOT NULL AND banco <> ''
             GROUP BY banco
             ORDER BY banco
            """)
        return await cur.fetchall()


async def categorias_aprendidas() -> dict:
    """{comercio_normalizado: categoria} — lo que el sistema ya sabe."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT comercio, categoria FROM categorias_aprendidas "
            "WHERE borrado_en IS NULL")
        return {r[0]: r[1] for r in await cur.fetchall()}


async def aprender_categoria(comercio_norm: str, categoria: str) -> None:
    """Guarda la corrección. Un comercio tiene UNA categoría: la última gana,
    porque una corrección nueva sobre el mismo sitio es un cambio de opinión,
    no un conflicto."""
    if not comercio_norm or not categoria:
        return
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO categorias_aprendidas (comercio, categoria)
            VALUES (%s, %s)
            ON CONFLICT (comercio) DO UPDATE
              SET categoria = EXCLUDED.categoria, borrado_en = NULL,
                  creado_en = now()
            """, (comercio_norm, categoria))


async def olvidar_categoria(movimiento_id: int) -> None:
    """Borra lo aprendido del comercio de ese movimiento.

    Va junto con vaciar la categoría a mano: sin esto, quitar una categoría
    equivocada duraba hasta la próxima compra en el mismo sitio, porque la regla
    aprendida seguía viva y volvía a ponerla — y esa vez sin pasar por ninguna
    cola, o sea sin que nadie se enterara. Corregir tiene que corregir de
    verdad; si no, el panel enseña a desconfiar de él.

    Es borrado suave, como todo acá: queda la fila con borrado_en.
    """
    from cerebro.bancos.categorias import normalizar_comercio
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT contraparte FROM movimientos WHERE id = %s",
                          (movimiento_id,))
        fila = await cur.fetchone()
        if not fila or not fila.get("contraparte"):
            return
        await conn.execute(
            "UPDATE categorias_aprendidas SET borrado_en = now() "
            "WHERE comercio = %s AND borrado_en IS NULL",
            (normalizar_comercio(fila["contraparte"]),))


async def poner_categoria(movimiento_id: int, categoria: str) -> None:
    """La escritura de la cola de corrección. Pasa por log_acciones como todo
    lo demás (la otra escritura del panel es `crear_gasto_en_efectivo`):
    una corrección hecha desde la web tiene que ser tan auditable y tan
    reversible como una hecha por Telegram.

    Y ADEMÁS ENSEÑA. Corregir un movimiento y no aprender del comercio deja el
    trabajo a medias: la próxima compra en el mismo sitio vuelve a caer en la
    cola, y a la tercera vez que uno corrige "SM NACIONAL" deja de corregir. La
    promesa del panel —una corrección vale para siempre— se cumple acá o no se
    cumple en ningún lado.
    """
    from cerebro.bancos.categorias import (
        categoria_permitida, normalizar_comercio, se_aprende)

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT categoria, contraparte, tipo FROM movimientos WHERE id = %s",
            (movimiento_id,))
        antes = await cur.fetchone()
        if antes is None:
            # El movimiento no existe. Sin esto se escribía igual una fila de
            # log_acciones con antes='{}' —auditoría de una edición que nunca
            # pasó, y que `deshacer` rechaza después con "no guardó con qué
            # volver atrás"—. Basura permanente en la tabla que ES el deshacer.
            log.warning("poner_categoria sobre movimiento inexistente: %s",
                        movimiento_id)
            return
        if not categoria_permitida(antes.get("tipo"), categoria):
            log.warning("Categoría %r no corresponde a un movimiento de tipo "
                        "%r (id %s)", categoria, antes.get("tipo"), movimiento_id)
            return
        await conn.execute("UPDATE movimientos SET categoria = %s WHERE id = %s",
                           (categoria or None, movimiento_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'movimientos', %s, %s, %s,
                    'corrección desde el panel web')
            """,
            (movimiento_id,
             json.dumps(antes or {}, default=str, ensure_ascii=False),
             json.dumps({"categoria": categoria}, ensure_ascii=False)))

    # Y se APRENDE: sin esto, corregir el mismo comercio la semana que viene
    # volvería a ser trabajo manual, que es como muere este tipo de sistema.
    # (Va fuera del `async with` porque aprender_categoria pide su propia
    # conexión al pool. Ya estaba así; acá solo se le quitó un SELECT de más.)
    if antes and antes.get("contraparte") and se_aprende(categoria):
        await aprender_categoria(normalizar_comercio(antes["contraparte"]),
                                 categoria)


# ═══════════════════════════════════════════════════════════════════════════
# EL PANEL DE TAREAS
#
# "Atrasada" NO EXISTÍA en el sistema hasta acá. No había función, ni columna,
# ni consulta guardada: vivía dentro de dos textos de prompt, y el modelo
# reescribía el SQL cada vez. Medido el 8-sep-2026 contra producción, las dos
# redacciones daban NÚMEROS DISTINTOS —9 por instante y 8 por día— y ninguna de
# las dos veía las 4 tareas pendientes SIN FECHA, que llevaban meses invisibles.
#
# Por eso lo que sigue se dice UNA vez y en UN sitio. Quien necesite
# "atrasadas" llama a `grupo_de_tarea`; no vuelve a escribir el criterio.
# ═══════════════════════════════════════════════════════════════════════════

# El ÚNICO estado que este módulo nombra para decidir un grupo, y el único que
# se escribe. NO hay una lista de estados válidos acá a propósito.
#
# `tareas.estado` no tiene restricción CHECK y ya se separó de lo que declara
# su propio comentario en db/schema.sql (`pendiente | hecha | pospuesta`):
# contra producción, el 8-sep-2026, había 46 pendiente, 44 hecha, 1 descartado
# y CERO pospuesta. O sea que existe un estado que nadie declaró y uno
# declarado que nadie usó nunca.
#
# Una lista escrita a mano acá habría hecho desaparecer la fila en 'descartado'
# del panel sin que nadie se entere. Así que se nombra lo que se necesita
# nombrar y TODO LO DEMÁS cae en un grupo VISIBLE (ver `grupo_de_tarea`).
ESTADO_PENDIENTE = "pendiente"
ESTADO_HECHA = "hecha"

# La clave de `areas` que identifica una tarea TÉCNICA (`db/migrations/
# 2026-09-22_areas.sql`). No hay ninguna otra constante de Python para esto
# en todo el repo -- las áreas se comparan como el string que son, igual que
# `areas.clave` en la base -- así que ésta es la primera, y nace acá porque
# `cerrar_tarea_de_la_sala` (26-sep-2026, §4) es el primer sitio que necesita
# nombrarla en vez de solo pintarla.
#
# Desde el 1-oct-2026 el grupo se llama «IA» (Lucy 1.0, E1) y antes tenía otro
# nombre, que ya no se escribe en ningún sitio del código (lo vigila
# `tests/test_grupo_ia.py`). La base lo cambia en dos pasos para que no haya
# ventana de error: `2026-10-01_area_ia.sql` crea el área y
# `2026-10-01b_area_ia_mover_y_borrar.sql` mueve lo que había y borra las
# viejas. Entre el despliegue y el segundo paso, `tareas_de_code_pendientes`
# devuelve lista vacía.
AREA_TECNICA = "IA"

# El orden en que se pintan los grupos, y su título. Es una decisión de
# presentación y vive acá, pegada al criterio que produce las claves, para que
# no se puedan separar.
#
# NO es la lista de la que salen los grupos: los grupos salen de lo que
# devuelve la consulta. Una clave que no esté acá se pinta igual, al final, con
# su clave cruda por título — ver `tareas_por_grupo`. Que un grupo pueda
# aparecer sin permiso es deliberado: lo contrario es cómo se pierde una fila.
#
# "historial" SÍ está declarado, y es la única excepción a "todo lo que se
# reparte se pinta en el panel": `web/app.py::tareas` lo saca a propósito antes
# de pintar, porque para eso existe — que las cerradas hace 3 días o más dejen
# de verse ahí. `web/app.py::tareas_historial` hace lo contrario: se queda solo
# con esa clave. Que esté declarada (y no un accidente sin nombre) es lo que
# permite que las dos rutas la traten como lo que es, no como una fila
# perdida.
GRUPOS_DE_TAREAS = (
    ("atrasadas", "Atrasadas"),
    ("hoy", "Hoy"),
    ("sin_fecha", "Sin fecha"),
    ("proximas", "Próximas"),
    ("otros", "Otros estados"),
    ("historial", "Historial"),
)

# Techo de la consulta. Hoy la tabla tiene 91 filas vivas, así que no muerde;
# existe para que el día que muerda se NOTE. Se piden `limite + 1` filas y, si
# vuelve una de más, `tareas_por_grupo` lo dice con `hay_mas`. Un LIMIT que
# recorta callado es la forma más barata de perder una tarea.
TOPE_TAREAS = 500

# Cuántos días de gracia tiene una tarea CERRADA antes de irse del panel al
# Historial. Sale de la sección 5 del documento de alcance: "se archivan
# automáticamente 3 días después de completarse [...] dejan de aparecer en la
# vista principal, y quedan accesibles en un historial". Va acá, pegado al
# criterio que lo usa (`grupo_de_tarea`), por el mismo motivo que TOPE_TAREAS:
# un número que decide qué se ve vive junto a quien lo usa, no repetido.
#
# OJO CON LA PALABRA: esto NO es "archivar". En este código "archivar" ya
# significa mandar a la papelera (`cerebro/agente.py:255`). Acá no se mueve ni
# se borra nada — se calcula, cada vez que se pinta, a qué grupo va la fila.
DIAS_HISTORIAL = 3


def dia_rd(cuando) -> date | None:
    """El día calendario de un instante, EN SANTO DOMINGO.

    `tareas.vence_en` es TIMESTAMPTZ, o sea que llega como instante. Un
    instante no tiene día hasta que se le elige una zona: las 8 de la noche del
    lunes en Santo Domingo son las 0:00 del MARTES en UTC. Comparar en UTC hace
    que las tareas de la noche cambien de día, y entonces el panel dice
    "atrasada" sobre algo que vence hoy.

    La zona sale de `config.TZ`, que es la misma que usa el resto de Lucy. No
    se escribe 'America/Santo_Domingo' acá: dos copias de una zona horaria se
    desincronizan igual que dos copias de cualquier otra cosa.

    Un instante SIN zona se lee como UTC y no como la hora de la máquina que
    corra esto: adivinar la zona del servidor haría que el mismo dato diera
    días distintos en Railway y en una laptop.
    """
    if cuando is None:
        return None
    if isinstance(cuando, datetime):
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=timezone.utc)
        return cuando.astimezone(TZ).date()
    if isinstance(cuando, date):
        return cuando
    return None


def hoy_rd() -> date:
    """Hoy en Santo Domingo. Se pasa como argumento a `grupo_de_tarea` para
    poder probar los bordes sin depender del reloj de quien corra las pruebas."""
    return datetime.now(TZ).date()


def grupo_de_tarea(estado, vence_en, hoy: date, completado_en=None) -> str:
    """A qué grupo del panel pertenece una tarea. LA definición de "atrasada"
    Y de "cuándo se va al Historial".

    ATRASADA ES POR DÍA, NO POR INSTANTE, y esa es la decisión central. Una
    tarea que vence hoy a las 9 de la mañana NO está atrasada a las 10: lo
    estará mañana. En el ritmo de un estudio de grabación nadie trabaja al
    minuto, y las dos personas que miran este panel piensan en días. Medido
    contra producción el 8-sep-2026, las dos definiciones que andaban sueltas
    en los prompts daban 9 (por instante) y 8 (por día).

    LO QUE NO SE PUEDE CONFIRMAR QUE ES 'pendiente' CAE EN 'otros', que es un
    grupo VISIBLE. Se usa `!=` contra el único estado nombrado y no una lista
    de estados conocidos: así, el estado que alguien invente mañana —o el que
    ya existe y nadie declaró, 'descartado'— aparece en el panel en vez de
    desaparecer de él. Al grupo indulgente no se llega por olvido; acá el
    olvido lleva al grupo que SE VE.

    Y DENTRO DE ESE 'otros', LAS QUE YA SE CERRARON HACE `DIAS_HISTORIAL` DÍAS
    O MÁS SE VAN A 'historial' — por DÍA, con el mismo `dia_rd` que decide
    "atrasada", así que el corte cae a medianoche de Santo Domingo y no a
    cualquier hora. NO se mira el estado ('hecha', 'descartado', el que sea):
    se mira si HAY `completado_en`. Mirar el estado sería teclear de nuevo la
    lista que este archivo ya evita más arriba. Sin `completado_en` —hoy, el
    'descartado' de producción— no hay desde cuándo contar, así que se queda
    en 'otros': perderla sería el mismo defecto que perder las sin fecha.
    """
    if estado != ESTADO_PENDIENTE:
        if completado_en is not None:
            dia_cierre = dia_rd(completado_en)
            if dia_cierre is not None and (hoy - dia_cierre).days >= DIAS_HISTORIAL:
                return "historial"
        return "otros"
    dia = dia_rd(vence_en)
    if dia is None:
        # SIN FECHA VA VISIBLE Y CON SU PROPIO TÍTULO. Las 4 que hay en
        # producción no las ve ninguna definición de "atrasada" —ni la de
        # instante ni la de día—, y por eso llevaban meses perdidas. Meterlas
        # dentro de otro grupo, u ocultarlas, es exactamente cómo se perdieron.
        return "sin_fecha"
    if dia < hoy:
        return "atrasadas"
    if dia == hoy:
        return "hoy"
    return "proximas"


async def areas() -> list[dict]:
    """Las áreas declaradas, en su orden: `[{clave, color}, ...]`.

    LA CLAVE ES EL NOMBRE QUE SE VE ('CDS', 'ACD', 'IA') — no hay
    traducción por medio, ver
    `db/migrations/2026-09-22_areas.sql`. Panel y Lucy leen esta función; no
    hay ninguna copia de la lista escrita a mano en ningún otro sitio del
    código, así que un área nueva aparece sola en los dos.

    SI LA TABLA TODAVÍA NO EXISTE (la migración no se aplicó: SQLSTATE
    42P01, undefined_table), devuelve `[]` en vez de reventar — mismo patrón
    que `cerebro/consultar.py::_comentarios_presentes` para el mismo caso.
    Con `[]`, el panel simplemente no ofrece ningún área para elegir (las
    tareas existentes se siguen viendo, sin etiqueta) y Lucy no la menciona
    en el prompt: ninguno de los dos inventa una lista.

    CUALQUIER OTRO FALLO SE PROPAGA: no hay una lista "segura" que inventar
    si la pregunta de verdad no se pudo contestar.
    """
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    "SELECT clave, color FROM areas ORDER BY orden, clave")
                return list(await cur.fetchall())
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42P01":
                return []
            raise


# ═══ Agregar y quitar grupos (Tiziano, 6-oct-2026: «quiero tambien poder agregar
# y quitar grupos») ═══════════════════════════════════════════════════════════
#
# UN GRUPO ES UNA FILA DE `areas` (clave = el nombre que se ve, color, orden), y
# lo que apunta a él son DOS columnas con llave foránea SIN `ON DELETE`
# (`proyectos.area`, `tareas.area`; medido en producción el 6-oct-2026:
# `proyectos_area_fkey` y `tareas_area_fkey`, ambas «NO ACTION»). Por nombre, no
# por id. Eso hace que «vacío» tenga una sola definición posible: NINGUNA fila de
# esas dos tablas apunta al grupo, de cualquier estado y también las de la
# papelera, porque la base no deja borrar un grupo mientras alguna lo nombre.
#
# `AREA_TECNICA` es el único nombre que el código da por fijo (las tareas de Code
# nacen ahí): ese grupo no se quita aunque esté vacío.
#
# QUÉ GARANTIZA QUÉ. Un bloqueo de transacción de Postgres
# (`pg_advisory_xact_lock`) pone EN FILA a quien crea o quita un grupo: la segunda
# petición espera a la primera y ve lo que ésta dejó. La comparación de nombres
# «sin distinguir mayúsculas, acentos ni espacios» se hace en Python sobre las
# claves que hay y por eso NECESITA ese bloqueo; la clave exacta repetida la
# frena además la llave primaria. Quitar usa UNA sentencia `DELETE ... WHERE NOT
# EXISTS (...)` y, por si un pedido le mete un proyecto o una tarea al grupo entre
# la lectura y el borrado, la llave foránea: o falla la inserción o falla el
# borrado (SQLSTATE 23503), nunca queda una fila apuntando a un grupo que no está.
# FRONTERA, completa. NO se ejercita contra un Postgres de verdad (no lo hay en
# esta Mac; las pruebas usan SQLite y dobles), y por eso NADIE ha comprobado que
# Postgres: (1) acepte `SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))` con el
# texto «grupos» como parámetro (la misma forma ya corre en producción en
# `crear_o_reusar_alerta_tecnica`); (2) ponga EN FILA a dos pedidos con ese bloqueo;
# (3) rechace el `DELETE` con SQLSTATE «23503» (llave foránea) y el `INSERT` con
# «23505» (llave primaria) tal como aquí se reconocen; (4) deje la transacción de
# afuera usable después de un rechazo gracias al punto de guardado
# (`conn.transaction()` anidado) de `quitar_grupo`. Lo que SÍ se vigila sin Postgres
# (`tests/test_grupos.py`): que el código reconozca esos errores por su SQLSTATE
# (clases reales de psycopg, en un subproceso), que el bloqueo sea lo primero que
# corre dentro de la transacción y que el `DELETE` corra dentro del punto de
# guardado, con dobles que lanzan lo mismo que lanza psycopg.

LARGO_NOMBRE_GRUPO = 30
NOMBRE_RESERVADO_DE_GRUPO = "Sin grupo"      # el cubo de lo que no tiene grupo
DATA_G_RESERVADO = "sin-grupo"              # el `data-g` que la plantilla le da al cubo («Sin grupo»)

# Colores para un grupo nuevo, en este orden, saltando los que ya están. Uno
# solo pide decidirse en la pantalla (Tiziano no escoge color): lo decide el
# sistema. Cuando se acaban, `color_para_un_grupo_nuevo` calcula uno por el
# ángulo áureo, que no se repite.
PALETA_DE_GRUPOS = ("#c0392b", "#2f8f4e", "#8c6d1f", "#5a5fb0", "#b83a8a", "#2a7f9e",
                    "#7a5230", "#6f8f1f")


class GrupoNoVale(ValueError):
    """Un nombre de grupo que no puede quedar. `clave`: `vacio`, `largo`,
    `caracteres`, `reservado` o `repetido`. El mensaje no repite el nombre."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


class GrupoNoSeQuita(ValueError):
    """Un grupo que no se puede quitar. `clave`: `fijo` (el que el código da por
    fijo), `no_existe` o `con_cosas` (en ese caso `contenido` trae las cuentas
    medidas en ese momento)."""

    def __init__(self, clave: str, mensaje: str, contenido: dict | None = None):
        super().__init__(mensaje)
        self.clave = clave
        self.contenido = contenido


def nombre_de_grupo_que_vale(valor) -> str:
    """El nombre limpio (sin espacios de alrededor, con uno solo entre palabras)
    o `GrupoNoVale`. Sin letras de control ni saltos de línea."""
    if not isinstance(valor, str) or not valor.strip():
        raise GrupoNoVale("vacio", "el nombre del grupo no puede quedar vacío")
    if any(ord(c) < 32 or ord(c) == 127 for c in valor):
        raise GrupoNoVale("caracteres", "el nombre del grupo lleva caracteres que no se pueden guardar")
    limpio = " ".join(valor.split())
    if len(limpio) > LARGO_NOMBRE_GRUPO:
        raise GrupoNoVale("largo", f"el nombre del grupo no puede pasar de {LARGO_NOMBRE_GRUPO} caracteres")
    if (clave_comparable_de_grupo(limpio) == clave_comparable_de_grupo(NOMBRE_RESERVADO_DE_GRUPO)
            or limpio.lower() == DATA_G_RESERVADO):
        raise GrupoNoVale("reservado", "ese nombre ya lo usa «Sin grupo»")
    return limpio


def clave_comparable_de_grupo(nombre: str) -> str:
    """LA comparación de nombres de grupo: sin tildes, sin mayúsculas y con los
    espacios de sobra colapsados. Dos nombres que den lo mismo acá son el mismo
    grupo."""
    sin_tildes = "".join(c for c in unicodedata.normalize("NFD", nombre)
                         if not unicodedata.combining(c))
    return " ".join(sin_tildes.casefold().split())


def color_para_un_grupo_nuevo(usados) -> str:
    """El primer color de `PALETA_DE_GRUPOS` que ningún grupo tiene; si ya se
    usaron todos, uno calculado por el ángulo áureo según cuántos hay (siempre
    distinto de la paleta y de los anteriores). Siempre un hex de 6 dígitos."""
    en_uso = {str(c).lower() for c in usados}
    for c in PALETA_DE_GRUPOS:
        if c not in en_uso:
            return c
    n = len(en_uso)
    while True:
        h = (n * 137.508) % 360
        c = colorsys.hls_to_rgb(h / 360, 0.38, 0.55)
        hexa = "#" + "".join(f"{round(x * 255):02x}" for x in c)
        if hexa not in en_uso:
            return hexa
        n += 1


async def crear_grupo(nombre) -> dict:
    """Crea un grupo al final de la lista, con color que asigna el sistema, y
    devuelve `{clave, color, orden}` LEÍDO de la base. `GrupoNoVale` si el nombre
    no vale o ya hay un grupo así (sin distinguir mayúsculas, tildes ni espacios).
    Todo en UNA transacción y bajo el bloqueo de grupos (ver arriba)."""
    limpio = nombre_de_grupo_que_vale(nombre)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("grupos", ))
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT clave, color, orden FROM areas")
            hay = await cur.fetchall()
            mio = clave_comparable_de_grupo(limpio)
            if any(clave_comparable_de_grupo(a["clave"]) == mio for a in hay):
                raise GrupoNoVale("repetido", "ya hay un grupo con ese nombre")
            color = color_para_un_grupo_nuevo([a["color"] for a in hay])
            orden = max([a["orden"] for a in hay] or [0]) + 1
            try:
                await cur.execute(
                    "INSERT INTO areas (clave, color, orden) VALUES (%s, %s, %s) "
                    "RETURNING clave, color, orden", (limpio, color, orden))
            except Exception as e:
                if _es_violacion(e, "23505"):      # la llave primaria: el mismo nombre, exacto
                    raise GrupoNoVale("repetido", "ya hay un grupo con ese nombre") from None
                raise
            return await cur.fetchone()


def _es_violacion(e: Exception, sqlstate: str) -> bool:
    """¿Es este error de la base una violación de ese SQLSTATE? (SQLite, que usan
    las pruebas, no trae SQLSTATE: se reconoce por su clase.)"""
    try:
        return e.sqlstate == sqlstate
    except AttributeError:
        return type(e).__name__ == "IntegrityError"


async def _contenido_de_grupo(cur, clave: str) -> dict:
    """Cuántas filas apuntan a ese grupo, por clase, medidas AHORA. `total` es la
    suma: cero es el único «vacío» (cualquier fila, también la de la papelera,
    impide borrar el grupo)."""
    await cur.execute(
        "SELECT count(*) FILTER (WHERE borrado_en IS NULL AND estado <> %s) AS proyectos_abiertos, "
        "count(*) FILTER (WHERE borrado_en IS NULL AND estado = %s) AS proyectos_cerrados, "
        "count(*) FILTER (WHERE borrado_en IS NOT NULL) AS proyectos_papelera "
        "FROM proyectos WHERE area = %s",
        (ESTADO_PROYECTO_CERRADO, ESTADO_PROYECTO_CERRADO, clave))
    cuentas = dict(await cur.fetchone())
    await cur.execute(
        "SELECT count(*) FILTER (WHERE borrado_en IS NULL AND estado = %s) AS tareas_pendientes, "
        "count(*) FILTER (WHERE borrado_en IS NULL AND estado = %s) AS tareas_hechas, "
        "count(*) FILTER (WHERE borrado_en IS NULL AND estado NOT IN (%s, %s)) AS tareas_otras, "
        "count(*) FILTER (WHERE borrado_en IS NOT NULL) AS tareas_papelera "
        "FROM tareas WHERE area = %s",
        (ESTADO_PENDIENTE, ESTADO_HECHA, ESTADO_PENDIENTE, ESTADO_HECHA, clave))
    cuentas.update(dict(await cur.fetchone()))
    cuentas["total"] = sum(cuentas.values())
    return cuentas


async def contenido_de_grupo(clave: str) -> dict | None:
    """Lo que tiene un grupo hoy (ver `_contenido_de_grupo`), o `None` si no hay
    un grupo con esa clave."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT 1 AS x FROM areas WHERE clave = %s", (clave,))
        if await cur.fetchone() is None:
            return None
        return await _contenido_de_grupo(cur, clave)


async def quitar_grupo(clave: str) -> None:
    """Quita un grupo VACÍO. `GrupoNoSeQuita` si es el fijo (`AREA_TECNICA`), si
    no existe o si tiene cosas (con las cuentas medidas en la misma transacción).
    Una sola sentencia borra solo si nada lo nombra; la llave foránea cubre a quien
    le meta algo entre medias (ver arriba)."""
    if clave == AREA_TECNICA:
        raise GrupoNoSeQuita("fijo", "ese grupo no se puede quitar: Lucy y Code lo usan")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("grupos", ))
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT 1 AS x FROM areas WHERE clave = %s", (clave,))
            if await cur.fetchone() is None:
                raise GrupoNoSeQuita("no_existe", "ese grupo ya no está")
            try:
                # Un punto de guardado propio: si la llave foránea rechaza el
                # borrado, la transacción de afuera sigue sirviendo para contar.
                async with conn.transaction():
                    await cur.execute(
                        "DELETE FROM areas WHERE clave = %s "
                        "AND NOT EXISTS (SELECT 1 FROM proyectos WHERE proyectos.area = areas.clave) "
                        "AND NOT EXISTS (SELECT 1 FROM tareas WHERE tareas.area = areas.clave) "
                        "RETURNING clave", (clave,))
                    borrado = await cur.fetchone()
            except Exception as e:
                if not _es_violacion(e, "23503"):    # alguien le metió algo justo ahora
                    raise
                borrado = None
            if borrado is None:
                contenido = await _contenido_de_grupo(cur, clave)
                raise GrupoNoSeQuita("con_cosas", "ese grupo tiene cosas: no se quita", contenido)


# ── BORRAR UN GRUPO CON COSAS, Y BORRAR UN PROYECTO CON SUS TAREAS (7-oct-2026) ──
#
# Tiziano: «poder borrar cualquier proyecto o grupo, con confirmación». Lo que se
# decidió (B2 y C): borrar un proyecto se lleva sus tareas; borrar un grupo borra
# sus proyectos y sus tareas sueltas «igual que si se hubieran borrado a mano»
# (papelera, recuperable) y el grupo desaparece; lo que se restaure vuelve a
# «Sin grupo». La ESCRITURA vive en `acciones/crud.py` (`borrar`, `borrar_grupo`,
# `deshacer`): acá solo lo que se LEE para preguntar (la confirmación dice con
# números qué se va) y para dibujar la papelera. Los números de la pregunta son
# los mismos con los que después se compara el «Sí» (ver `crud.borrar`).

CLAVES_DE_CONTENIDO_DE_PROYECTO = ("tareas_pendientes", "tareas_hechas", "tareas_otras")
CLAVES_DE_CONTENIDO_DE_GRUPO = (
    "proyectos_abiertos", "proyectos_cerrados", "proyectos_papelera",
    "tareas_pendientes", "tareas_hechas", "tareas_otras", "tareas_papelera",
    "tareas_en_proyectos_pendientes", "tareas_en_proyectos_hechas",
    "tareas_en_proyectos_otras")


async def _contenido_de_proyecto(cur, proyecto_id: int) -> dict:
    """Las tareas VIVAS del proyecto por clase, medidas ahora (las que se van con
    él si se borra). Mismas clases que las del grupo: pendientes, hechas, otras."""
    await cur.execute(
        "SELECT count(*) FILTER (WHERE estado = %s) AS tareas_pendientes, "
        "count(*) FILTER (WHERE estado = %s) AS tareas_hechas, "
        "count(*) FILTER (WHERE estado NOT IN (%s, %s)) AS tareas_otras "
        "FROM tareas WHERE proyecto_id = %s AND borrado_en IS NULL",
        (ESTADO_PENDIENTE, ESTADO_HECHA, ESTADO_PENDIENTE, ESTADO_HECHA, proyecto_id))
    return dict(await cur.fetchone())


async def contenido_de_proyecto(proyecto_id: int) -> dict | None:
    """`{id, nombre, estado, area, tareas_pendientes, tareas_hechas, tareas_otras}`
    de un proyecto VIVO, o `None` si no existe o ya está en la papelera."""
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, nombre, estado, area FROM proyectos "
            "WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        fila = await cur.fetchone()
        if fila is None:
            return None
        return {**dict(fila), **await _contenido_de_proyecto(cur, proyecto_id)}


async def _contenido_para_borrar_grupo(cur, clave: str) -> dict:
    """Lo que tiene un grupo y se iría al borrarlo: lo de `_contenido_de_grupo`
    más las tareas VIVAS que están dentro de sus proyectos vivos (se van con
    ellos). `total` es la suma de todo."""
    c = await _contenido_de_grupo(cur, clave)
    await cur.execute(
        "SELECT count(*) FILTER (WHERE t.estado = %s) AS tareas_en_proyectos_pendientes, "
        "count(*) FILTER (WHERE t.estado = %s) AS tareas_en_proyectos_hechas, "
        "count(*) FILTER (WHERE t.estado NOT IN (%s, %s)) AS tareas_en_proyectos_otras "
        "FROM tareas t JOIN proyectos p ON p.id = t.proyecto_id "
        "WHERE t.borrado_en IS NULL AND p.borrado_en IS NULL AND p.area = %s",
        (ESTADO_PENDIENTE, ESTADO_HECHA, ESTADO_PENDIENTE, ESTADO_HECHA, clave))
    c.update(dict(await cur.fetchone()))
    c["total"] = sum(v for k, v in c.items() if k != "total")
    return c


async def contenido_para_borrar_grupo(clave: str) -> dict | None:
    """Lo que se iría con el grupo (ver `_contenido_para_borrar_grupo`), o `None`
    si no hay un grupo con esa clave."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT 1 AS x FROM areas WHERE clave = %s", (clave,))
        if await cur.fetchone() is None:
            return None
        return await _contenido_para_borrar_grupo(cur, clave)


def _json_de(valor):
    """El JSON de una columna `jsonb` (psycopg lo da ya convertido; SQLite, como
    texto), o `{}`."""
    if isinstance(valor, dict):
        return valor
    try:
        r = json.loads(valor) if valor else {}
    except (TypeError, ValueError):
        return {}
    return r if isinstance(r, dict) else {}


def _ids_que_se_fueron_con_de(despues) -> list[int]:
    """Los ids de `despues.tareas_con_el` de la huella `borrar` de un proyecto
    (solo enteros). UNA lectura de esa lista: la usan la papelera, los avisos, el
    bot y `crud.deshacer` para devolver las MISMAS tareas."""
    ids = _json_de(despues).get("tareas_con_el") or []
    return [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]


async def _ids_que_se_fueron_con(cur, huella_id) -> list[int]:
    """Los ids de las tareas que se fueron con un proyecto en SU acto de borrar
    (lo que guardó la huella `borrar` del proyecto en `despues`)."""
    if huella_id is None:
        return []
    await cur.execute("SELECT despues FROM log_acciones WHERE id = %s", (huella_id,))
    fila = await cur.fetchone()
    return _ids_que_se_fueron_con_de(fila["despues"] if fila else None)


async def aviso_de_proyecto_borrado(proyecto_id) -> dict | None:
    """Para el aviso de «borrado»: `{nombre, tareas}` SOLO si el proyecto está de
    verdad en la papelera (`tareas` = las que se fueron con él según su huella);
    `None` si no existe o sigue vivo. Una dirección escrita a mano no lo cambia."""
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT nombre, "
            "(SELECT max(l.id) FROM log_acciones l WHERE l.tabla = 'proyectos' "
            "   AND l.registro_id = proyectos.id AND l.accion = 'borrar') AS huella_id "
            "FROM proyectos WHERE id = %s AND borrado_en IS NOT NULL", (proyecto_id,))
        fila = await cur.fetchone()
        if fila is None:
            return None
        return {"nombre": fila["nombre"],
                "tareas": len(await _ids_que_se_fueron_con(cur, fila["huella_id"]))}


async def aviso_de_proyecto_restaurado(proyecto_id) -> dict | None:
    """Para el aviso de «restaurado»: `{nombre, tareas}` SOLO si el proyecto está
    VIVO (`tareas` = cuántas de las que se fueron con él en su último borrado
    están vivas ahora); `None` si no existe o sigue en la papelera."""
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT nombre, "
            "(SELECT max(l.id) FROM log_acciones l WHERE l.tabla = 'proyectos' "
            "   AND l.registro_id = proyectos.id AND l.accion = 'borrar') AS huella_id "
            "FROM proyectos WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        fila = await cur.fetchone()
        if fila is None:
            return None
        vivas = 0
        for tid in await _ids_que_se_fueron_con(cur, fila["huella_id"]):
            await cur.execute(
                "SELECT 1 AS x FROM tareas WHERE id = %s AND borrado_en IS NULL", (tid,))
            vivas += 1 if await cur.fetchone() else 0
        return {"nombre": fila["nombre"], "tareas": vivas}


async def aviso_de_tarea(tarea_id, *, borrada: bool) -> str | None:
    """El título de la tarea SOLO si su estado es el que el aviso dice: en la
    papelera (`borrada=True`) o viva (`borrada=False`). `None` si no."""
    if isinstance(tarea_id, bool) or not isinstance(tarea_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT titulo, (borrado_en IS NOT NULL) AS borrada FROM tareas WHERE id = %s",
            (tarea_id,))
        fila = await cur.fetchone()
    if fila is None or bool(fila["borrada"]) != borrada:
        return None
    return fila["titulo"]


async def aviso_de_nota(nota_id, *, borrada: bool) -> dict | None:
    """`{proyecto: nombre}` de la nota SOLO si su estado es el que el aviso dice: en la papelera
    (`borrada=True`) o viva (`borrada=False`). `None` si no, o si la nota no es de ningún proyecto."""
    if isinstance(nota_id, bool) or not isinstance(nota_id, int):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT p.nombre, (n.borrado_en IS NOT NULL) AS borrada FROM notas n "
            "JOIN proyectos p ON p.id = n.proyecto_id WHERE n.id = %s", (nota_id,))
        fila = await cur.fetchone()
    if fila is None or bool(fila["borrada"]) != borrada:
        return None
    return {"proyecto": fila["nombre"]}


async def aviso_de_grupo_borrado(clave: str) -> dict | None:
    """Para el aviso de «grupo borrado»: lo que dice la huella del último borrado
    de un grupo con ese nombre (`{proyectos, tareas}`), SOLO si el grupo ya no
    existe; `None` si existe o no hay huella. La huella (`tabla='areas'`) la deja
    `crud.borrar_grupo`."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT 1 AS x FROM areas WHERE clave = %s", (clave,))
        if await cur.fetchone() is not None:
            return None
        await cur.execute(
            "SELECT antes, despues FROM log_acciones WHERE tabla = 'areas' AND accion = 'borrar' "
            "ORDER BY id DESC")
        for fila in await cur.fetchall():
            if _json_de(fila["antes"]).get("clave") == clave:
                d = _json_de(fila["despues"])
                return {"proyectos": len(d.get("proyectos") or []),
                        "tareas": len(d.get("tareas") or [])}
    return None


async def papelera_de_proyectos_y_tareas() -> dict:
    """Lo borrado de Proyectos para la pantalla Papelera: `{proyectos, tareas, notas}`.

    · Cada proyecto: `id, nombre, area, borrado_en, huella_id` (su último `borrar`;
      sin él no hay con qué restaurar), `tareas` (las que se fueron con él en ese
      acto, según la huella) y `choca` (hay otro proyecto VIVO con ese nombre: no se
      puede restaurar mientras; un pausado o un cerrado no chocan por nombre).
    · Cada tarea: `id, titulo, estado, proyecto_id, proyecto_nombre, borrado_en,
      huella_id` y `por_que_no` (`None` si se puede restaurar; `sin_huella`,
      `proyecto_borrado` o `proyecto_cerrado`). Las que se fueron CON su proyecto
      no se listan aparte: van contadas en la fila de su proyecto, y vuelven con él.
    No toca nada. Es la MISMA decisión que toma `crud.deshacer` (ver la prueba de
    hermanos de `tests/test_papelera_de_proyectos.py`)."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT p.id, p.nombre, p.area, p.estado, p.borrado_en, "
            "(SELECT max(l.id) FROM log_acciones l WHERE l.tabla = 'proyectos' "
            "   AND l.registro_id = p.id AND l.accion = 'borrar') AS huella_id "
            "FROM proyectos p WHERE p.borrado_en IS NOT NULL ORDER BY p.borrado_en DESC, p.id DESC")
        proyectos = [dict(f) for f in await cur.fetchall()]
        fueron: dict[int, int] = {}              # tarea_id -> proyecto_id con el que se fue
        for p in proyectos:
            ids = await _ids_que_se_fueron_con(cur, p["huella_id"])
            p["tareas"] = len(ids)
            # Solo choca por nombre si al volver CUENTA como activo: un pausado o un cerrado pueden repetir el
            # nombre de uno activo (misma decisión que `proyecto_puede_quedar_activo`, que toma `crud.deshacer`).
            p["choca"] = cuenta_como_activo(p["estado"]) and (
                await proyecto_vivo_con_nombre(cur, p["nombre"], excluir_id=p["id"])) is not None
            for tid in ids:
                fueron[tid] = p["id"]
        await cur.execute(
            "SELECT t.id, t.titulo, t.estado, t.proyecto_id, t.borrado_en, "
            "(SELECT max(l.id) FROM log_acciones l WHERE l.tabla = 'tareas' "
            "   AND l.registro_id = t.id AND l.accion = 'borrar') AS huella_id, "
            "p.nombre AS proyecto_nombre, p.borrado_en AS proyecto_borrado_en, "
            "p.estado AS proyecto_estado "
            "FROM tareas t LEFT JOIN proyectos p ON p.id = t.proyecto_id "
            "WHERE t.borrado_en IS NOT NULL ORDER BY t.borrado_en DESC, t.id DESC")
        tareas = []
        for f in await cur.fetchall():
            t = dict(f)
            if t["proyecto_id"] is not None and fueron.get(t["id"]) == t["proyecto_id"] \
                    and t["proyecto_borrado_en"] is not None:
                continue                         # se fue con su proyecto: vuelve con él
            if t["huella_id"] is None:
                t["por_que_no"] = "sin_huella"
            elif t["proyecto_id"] is not None and t["proyecto_borrado_en"] is not None:
                t["por_que_no"] = "proyecto_borrado"
            elif t["proyecto_id"] is not None and t["proyecto_estado"] == ESTADO_PROYECTO_CERRADO:
                t["por_que_no"] = "proyecto_cerrado"
            else:
                t["por_que_no"] = None
            tareas.append(t)
        # LAS NOTAS BORRADAS DE UN PROYECTO (parte 6): sin proyecto no salen (no hay a dónde volver).
        # Una cuyo proyecto está en la papelera SALE, pero no se devuelve sola (`por_que_no`).
        await cur.execute(
            "SELECT n.id, n.contenido, n.proyecto_id, n.borrado_en, "
            "(SELECT max(l.id) FROM log_acciones l WHERE l.tabla = 'notas' "
            "   AND l.registro_id = n.id AND l.accion = 'borrar') AS huella_id, "
            "p.nombre AS proyecto_nombre, p.borrado_en AS proyecto_borrado_en "
            "FROM notas n JOIN proyectos p ON p.id = n.proyecto_id "
            "WHERE n.borrado_en IS NOT NULL ORDER BY n.borrado_en DESC, n.id DESC")
        notas = []
        for f in await cur.fetchall():
            n = dict(f)
            if n["huella_id"] is None:
                n["por_que_no"] = "sin_huella"
            elif n["proyecto_borrado_en"] is not None:
                n["por_que_no"] = "proyecto_borrado"
            else:
                n["por_que_no"] = None
            notas.append(n)
        return {"proyectos": proyectos, "tareas": tareas, "notas": notas}


async def proyectos_vivos() -> list[dict]:
    """Los proyectos no archivados, para que Lucy los reciba como información
    (encargo 5, requisito 3) y entienda "dentro del proyecto X" sin tener que
    consultar primero. `[{id, nombre, area, estado}, ...]`, por nombre.

    "Vivos" = no borrados (`borrado_en IS NULL`), SIN filtrar por `estado`
    ('activo'|'pausado'|'cerrado'): son dos preguntas distintas -- un
    proyecto pausado sigue siendo un proyecto real, y Lucy tiene que poder
    reconocerlo si Tiziano lo nombra. El `estado` viaja en la fila para que
    ella lo vea y decida (o pregunte) si hace falta, no para que esta
    función decida por ella.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, nombre, area, estado FROM proyectos "
            " WHERE borrado_en IS NULL ORDER BY nombre")
        return list(await cur.fetchall())


# ═══════════════════════════════════════════════════════════════════════════
# LA PÁGINA DE PROYECTOS (Lucy 1.0, E4: solo lectura)
# ═══════════════════════════════════════════════════════════════════════════
#
# El SQL solo TRAE filas (`pagina_de_proyectos`); todo criterio vive en
# `armar_pagina` y sus funciones, que son Python puro y se prueban con un `hoy`
# fijo: la misma separación que `tareas_por_grupo`/`grupo_de_tarea`.

# Cuántos días sin movimiento hacen que un proyecto se marque «dormido». Lo
# puso la sala (diseño de Lucy 1.0, §5.6). Se cuenta por día de Santo Domingo.
DIAS_DORMIDO = 5

# QUÉ ES «MOVERSE» (§5.6): cada huella de `log_acciones` sobre el proyecto, sus
# tareas, los comentarios de sus tareas o sus notas es un movimiento, MENOS las que Lucy
# escribe sola cuando AVISA (no es que alguien se moviera). TODA `accion` que
# el código escribe tiene que estar en una de las dos listas: lo comprueba
# `tests/test_pagina_proyectos.py` recorriendo el código. Una `accion` que no
# esté en ninguna cuenta como movimiento (el lado que no despierta falsas
# alarmas) hasta que alguien la clasifique.
ACCIONES_AUTOMATICAS = ("avisar", "aviso_atraso_code")
ACCIONES_QUE_MUEVEN = ("crear", "editar", "borrar", "deshacer")

# LA ACTIVIDAD DEL PROYECTO (parte 7 del diseño «la página de un proyecto, completa», 8-oct-2026).
#
# Lo que ha pasado en un proyecto, SIN decir quién lo hizo, y sin nombrar a la persona que entra o sale
# de un papel. La huella de `log_acciones` no guarda el actor de verdad (`actor` es una palabra fija,
# 'panel', 'lucy' o 'sala'), y Tiziano lo dejó para después (8-oct-2026: «Que no diga quien lo hizo, eso
# se hace despues»). Por eso lo que se pinta sale de la HUELLA —tabla, acción, qué cambió y el título
# que la tarea tenía EN ESE MOMENTO (`_titulo_de_la_huella`)— y nunca de un dato de persona: ni el
# `actor`, ni un `responsable_chat_id`, ni el nombre de un cliente, de una persona agregada, de quien
# escribió un comentario o una nota. El cliente y el responsable salen por su PAPEL, sin nombre.
#
# FRONTERA, dicha una vez: el único texto libre de la casa que sale en una frase es el TÍTULO de la
# tarea, tal cual (el diseño lo pide así: «Se marcó hecha “…”»). Si alguien escribe un nombre de persona
# en el título de una tarea, ese nombre sale en la Actividad. **El NOMBRE DEL PROYECTO no sale en
# ninguna frase**: una edición suya dice «Se le cambió el nombre al proyecto.». Lo que la garantía
# cubre es que la frase no saque el nombre de ningún CAMPO de persona, no que filtre lo que una persona
# escribió.
#
# LAS TABLAS QUE LLEGAN A UN PROYECTO son las cuatro que ya juntaba la consulta de «Último movimiento»
# (`pagina_de_proyectos`): el proyecto, sus tareas, los comentarios de sus tareas y sus notas. La
# Actividad y «Último movimiento» salen de LA MISMA consulta, así que cuentan lo mismo. Las huellas de
# `participantes` NO entran a propósito: agregar o quitar una persona no cuenta como movimiento del
# proyecto (la nota de `proyectos.html` y su prueba `tests/test_personas_y_cliente.py`, de la decisión
# del diseño de Lucy 1.0 §5.4 que está escrita en `db.agregar_participante`).
# Los gastos (`movimientos`), las citas (`eventos`) y los micro-pasos no se ven en esta página, así que
# tampoco salen aquí.
#
# CADA COMBINACIÓN DE TABLA Y ACCIÓN que el código escribe tiene su frase. La lista sale de recorrer el
# código, no de una lista tecleada: `tests/test_actividad_de_proyecto.py` saca las tablas del `UNION`
# de la consulta y las acciones de `ACCIONES_QUE_MUEVEN`, y exige que cada par tenga su frase. Una
# combinación que nadie declaró NO se esconde: sale con la frase general (`ACTIVIDAD_OTRA`).

# Cuántos renglones se pintan por proyecto. Veinte: la maqueta muestra tres y un historial no puede
# crecer sin fin; es el orden de magnitud de un proyecto (sus tareas son un puñado). El tope se aplica
# EN LA CONSULTA (`TOPE_ACTIVIDAD`), así que lo que la página TRAE está acotado a 20 por proyecto; el
# SQL, eso sí, sigue recorriendo y ordenando todas las huellas de esas cuatro tablas en cada carga (es
# lo que ya hacía para «Último movimiento»). Las 43 tareas vivas en 20 proyectos vivos las midió la
# sala el 8-oct-2026 con un guion de solo conteos; yo no las medí.
TOPE_ACTIVIDAD = 20

# La frase de una combinación que nadie declaró. Dice la verdad sin prometer qué pasó.
ACTIVIDAD_OTRA = "Hubo un cambio en el proyecto."


def _con_titulo(titulo, con: str, sin: str) -> str:
    """La frase con el título de la tarea entre comillas angulares, o su versión sin título cuando la
    huella no lo trae (un `deshacer`, un comentario, un renglón viejo)."""
    t = (titulo or "").strip()
    return con.format(t=f"«{t}»") if t else sin


def _titulo_de_la_huella(tabla, accion, antes, despues):
    """El título que la tarea tenía EN ESE MOMENTO, sacado de la PROPIA huella — nunca del JOIN a
    `tareas`, que da el de HOY (una tarea creada como «A» y hoy llamada «B» no se agregó como «B»).

    De qué lado de la huella sale, por acción: al CREAR, del `despues` (la fila que quedó); al EDITAR y
    al BORRAR, del `antes` (la que había). Las huellas de un COMENTARIO no traen el título de su tarea
    (traen el comentario) y la de un `deshacer` no trae ni `antes` ni `despues`: en esos casos se
    devuelve `None` y la frase va SIN título, en vez de decir el de hoy. Y un `editar` cuyo `antes` no
    traiga `titulo` también sale sin título: no se inventa ni se busca el de ahora.
    """
    if tabla != "tareas" or accion not in ("crear", "editar", "borrar"):
        return None
    fila = (despues if accion == "crear" else antes) or {}
    return fila.get("titulo") if isinstance(fila, dict) else None


def _cambiadas(antes, despues) -> set:
    """Las columnas que esa edición CAMBIÓ de verdad.

    Hace falta comparar: la huella de `crud.editar` trae la fila ENTERA de después (`SELECT *` tras el
    `UPDATE`), y la de las funciones de `db` (marcar hecha, reabrir, cerrar…) trae SOLO lo que cambió.
    Mirar qué claves hay en el `despues` diría, por ejemplo, que un cambio de nombre «cambió el
    cliente» —porque el cliente está en la fila— y eso sería una frase falsa. Sin `antes` con qué
    comparar no se afirma nada: no se devuelve ninguna columna y sale la frase general."""
    if not isinstance(antes, dict) or not isinstance(despues, dict):
        return set()
    return {c for c, v in despues.items() if antes.get(c) != v}


def _proyecto_cambiado(titulo, antes, despues) -> str:
    """Qué se dice de una edición de un proyecto: lo que la huella dice que cambió, sin nombres. El
    cliente y el responsable se nombran por su PAPEL, nunca por su nombre ni por su número de chat."""
    d = despues if isinstance(despues, dict) else {}
    cambiadas = _cambiadas(antes, despues)
    if "estado" in cambiadas:
        if d["estado"] == ESTADO_PROYECTO_CERRADO:
            return "Se cerró el proyecto."
        return ("Se reabrió el proyecto." if d["estado"] == "activo"
                else "Se cambió el estado del proyecto.")
    if {"cliente_noco_id", "cliente_nombre"} & cambiadas:
        return "Se cambió el cliente del proyecto."
    if "responsable_chat_id" in cambiadas:
        return "Se cambió el responsable del proyecto."
    if "nombre" in cambiadas:
        return "Se le cambió el nombre al proyecto."
    if "descripcion" in cambiadas:
        return "Se escribió de qué se trata el proyecto."
    if "carpeta" in cambiadas:
        return "Se cambió la carpeta del proyecto."
    if "area" in cambiadas:
        return "Se cambió el proyecto de grupo."
    if {"inicio", "entrega", "termina_cuando"} & cambiadas:
        return "Se cambiaron las fechas del proyecto."
    return "Se cambió el proyecto."


def _tarea_cambiada(titulo, antes, despues) -> str:
    """Qué se dice de una edición de una tarea. El responsable se nombra por su papel, y un cambio de
    título no repite el título: lo escribió una persona y de él se dice solo que cambió."""
    d = despues if isinstance(despues, dict) else {}
    cambiadas = _cambiadas(antes, despues)
    if "estado" in cambiadas:
        if d["estado"] == ESTADO_HECHA:
            return _con_titulo(titulo, "Se marcó hecha {t}.", "Se marcó hecha una tarea.")
        return (_con_titulo(titulo, "Se reabrió {t}.", "Se reabrió una tarea.")
                if d["estado"] == ESTADO_PENDIENTE
                else _con_titulo(titulo, "Se le cambió el estado a {t}.",
                                 "Se le cambió el estado a una tarea."))
    if "responsable_chat_id" in cambiadas:
        return _con_titulo(titulo, "Se cambió el responsable de {t}.",
                           "Se cambió el responsable de una tarea.")
    if "titulo" in cambiadas:
        return "Se le cambió el nombre a una tarea."
    if "vence_en" in cambiadas:
        return _con_titulo(titulo, "Se le cambió la fecha a {t}.",
                           "Se le cambió la fecha a una tarea.")
    if "proyecto_id" in cambiadas:
        return _con_titulo(titulo, "Se movió {t} a otro proyecto.",
                           "Se movió una tarea a otro proyecto.")
    if "area" in cambiadas:
        return _con_titulo(titulo, "Se cambió {t} de grupo.", "Se cambió una tarea de grupo.")
    return _con_titulo(titulo, "Se cambió {t}.", "Se cambió una tarea.")


# (tabla, acción) → la frase. UNA entrada por cada combinación que el código puede escribir en
# `log_acciones` sobre algo que cuelga de un proyecto. La clave y la frase viven juntas a propósito:
# así la prueba que saca las combinaciones del código compara contra esto, y no contra una lista aparte
# que se puede separar de la realidad.
FRASES_DE_ACTIVIDAD = {
    ("proyectos", "crear"): lambda t, a, d: "Se creó el proyecto.",
    ("proyectos", "editar"): _proyecto_cambiado,
    ("proyectos", "borrar"): lambda t, a, d: "Se mandó el proyecto a la papelera.",
    ("proyectos", "deshacer"): lambda t, a, d: "Se deshizo un cambio del proyecto.",
    ("tareas", "crear"): lambda t, a, d: _con_titulo(t, "Se agregó la tarea {t}.", "Se agregó una tarea."),
    ("tareas", "editar"): _tarea_cambiada,
    ("tareas", "borrar"): lambda t, a, d: _con_titulo(t, "Se borró la tarea {t}.", "Se borró una tarea."),
    # DESHACER SIRVE PARA LAS DOS COSAS: devolver algo borrado o deshacer una creación (que la manda a
    # la papelera). La huella de `deshacer` NO dice cuál de las dos fue (no guarda `antes`/`despues`), así
    # que la frase no puede prometer que algo «volvió».
    ("tareas", "deshacer"): lambda t, a, d: _con_titulo(t, "Se deshizo un cambio en la tarea {t}.", "Se deshizo un cambio en una tarea."),
    ("comentarios_tarea", "crear"): lambda t, a, d: _con_titulo(t, "Nuevo comentario en {t}.", "Nuevo comentario en una tarea."),
    ("comentarios_tarea", "editar"): lambda t, a, d: _con_titulo(t, "Se editó un comentario en {t}.", "Se editó un comentario en una tarea."),
    ("comentarios_tarea", "borrar"): lambda t, a, d: _con_titulo(t, "Se borró un comentario en {t}.", "Se borró un comentario en una tarea."),
    ("comentarios_tarea", "deshacer"): lambda t, a, d: _con_titulo(t, "Se deshizo un cambio en un comentario de {t}.", "Se deshizo un cambio en un comentario."),
    ("notas", "crear"): lambda t, a, d: "Nueva nota en el proyecto.",
    ("notas", "editar"): lambda t, a, d: "Se editó una nota.",
    ("notas", "borrar"): lambda t, a, d: "Se borró una nota.",
    ("notas", "deshacer"): lambda t, a, d: "Se deshizo un cambio en una nota.",
}

COMBINACIONES_DE_ACTIVIDAD = tuple(sorted(FRASES_DE_ACTIVIDAD))


def frase_de_actividad(tabla, accion, antes=None, despues=None) -> str:
    """La frase de UNA huella de `log_acciones`: una sola puerta. Nunca vacía: la combinación que
    nadie declaró sale con la frase general, no se esconde. Ni la frase ni lo que mira para armarla
    tocan un dato de persona, y el título que nombra es el que la tarea tenía EN ESE MOMENTO
    (`_titulo_de_la_huella`), no el de hoy."""
    como = FRASES_DE_ACTIVIDAD.get((tabla, accion)) \
        if isinstance(tabla, str) and isinstance(accion, str) else None
    if como is None:
        return ACTIVIDAD_OTRA
    return como(_titulo_de_la_huella(tabla, accion, antes, despues), antes, despues)


def actividad_de_renglones(renglones) -> list[dict]:
    """Las huellas de UN proyecto (las que trae `pagina_de_proyectos`, ya la más nueva primero) → lo
    que pinta el bloque: la frase y su instante.

    El `actor` y la bandeja no viajan en la consulta, así que no se pueden escribir por descuido. Los
    avisos automáticos (`ACCIONES_AUTOMATICAS`) tampoco llegan: los deja fuera el `NOT IN` del SQL —
    UNA sola puerta para la Actividad —, y el filtro que `armar_pagina` hace sobre `huellas` es otra
    decisión (qué despierta un proyecto), no una segunda regla de lo que se pinta."""
    return [{"frase": frase_de_actividad(r["tabla"], r["accion"], r.get("antes"), r.get("despues")),
             "cuando": r["ts"]} for r in renglones]

# El color de un grupo sale de `areas.color` y se escribe en un `style=`: solo
# pasa un hex; cualquier otra cosa se pinta con este gris.
COLOR_SIN_GRUPO = "#6b6b6b"
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}")


def color_de_grupo(color) -> str:
    return color if isinstance(color, str) and _HEX.fullmatch(color) else COLOR_SIN_GRUPO


# El tono de un grupo para el modo oscuro SALE DEL MISMO `areas.color`, no de una
# lista escrita grupo por grupo: la regla es una línea. Se pasa el color a OKLCH
# (claridad, color y tono), se conservan el TONO y el COLOR, y la claridad sube
# `_OSCURO_HACIA_BLANCO` del camino que le falta hasta el blanco, sin quedar por
# debajo de `_OSCURO_PISO` (para que un color muy oscuro también se lea sobre el
# fondo oscuro). Medido el 1-oct-2026 contra los tres tonos oscuros de la
# maqueta aprobada, versión 19 (#c8102e -> #ff6b6b, #ef6c00 -> #ffa04a,
# #8a4a8f -> #c98ccf): la diferencia mayor en un canal es 6 de 255
# (`tests/test_pagina_proyectos_maqueta.py` la vuelve a medir). Un color que al
# aclararlo se sale de la gama de la pantalla se recorta y su tono puede
# correrse; el contraste del resultado contra el fondo oscuro de la página lo
# exige la misma prueba en una rejilla de colores (4.5 o más).
_OSCURO_HACIA_BLANCO = 0.44
_OSCURO_PISO = 0.70


def _oklch_de(rgb: tuple) -> tuple:
    def lineal(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lineal(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    claridad = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return claridad, math.hypot(a, bb), math.atan2(bb, a)


def _rgb_de(claridad: float, croma: float, tono: float) -> tuple:
    a, bb = croma * math.cos(tono), croma * math.sin(tono)
    l = (claridad + 0.3963377774 * a + 0.2158037573 * bb) ** 3
    m = (claridad - 0.1055613458 * a - 0.0638541728 * bb) ** 3
    s = (claridad - 0.0894841775 * a - 1.2914855480 * bb) ** 3
    lineales = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    def gamma(c):
        c = min(1.0, max(0.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(gamma(c) for c in lineales)


def color_oscuro_de_grupo(color) -> str:
    """El tono del grupo para el fondo oscuro (ver arriba). Pasa por
    `color_de_grupo`: lo que no es un hex se pinta con el gris, aclarado. Un hex
    de largo raro (5 o 7 dígitos) no se puede leer y vuelve igual."""
    color = color_de_grupo(color)
    digitos = color[1:]
    if len(digitos) in (3, 4):
        digitos = "".join(c * 2 for c in digitos[:3])
    elif len(digitos) in (6, 8):
        digitos = digitos[:6]
    else:
        return color
    claridad, croma, tono = _oklch_de(tuple(int(digitos[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    nueva = max(claridad + _OSCURO_HACIA_BLANCO * (1 - claridad), _OSCURO_PISO)
    rgb = _rgb_de(nueva, croma, tono)
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def ultimo_movimiento(creado_en, huellas) -> datetime | None:
    """El más reciente entre la creación del proyecto y cada huella (ya sin las
    automáticas). `None` solo si no hay ni creación."""
    candidatos = [c for c in [creado_en, *huellas] if isinstance(c, datetime)]
    return max(candidatos, key=lambda c: c if c.tzinfo else c.replace(tzinfo=timezone.utc)) \
        if candidatos else None


def dias_sin_movimiento(ultimo, hoy: date) -> int | None:
    dia = dia_rd(ultimo)
    return None if dia is None else max((hoy - dia).days, 0)


def esta_dormido(estado, dias: int | None) -> bool:
    """Un proyecto cerrado nunca está dormido."""
    return estado != ESTADO_PROYECTO_CERRADO and dias is not None and dias >= DIAS_DORMIDO


def estado_calculado(estado, hechas: int) -> str:
    """Lo que se lee del proyecto, calculado (P7: nadie lo elige a mano):
    cerrado y pausado son el `estado` guardado; si no, 0 tareas hechas es «Sin
    empezar» y 1 o más, «En curso»."""
    if estado == ESTADO_PROYECTO_CERRADO:
        return "Cerrado"
    if estado == "pausado":
        return "Pausado"
    return "Sin empezar" if hechas == 0 else "En curso"


def _orden_de_pendientes(t: dict):
    """Las graves primero (alertas técnicas que Natalia declaró graves, 4-oct-2026),
    luego las vencidas, después las de fecha más cercana y al final las sin
    fecha (el `ordenar` de la maqueta). Empata por id."""
    futuro = datetime.max.replace(tzinfo=timezone.utc)
    cuando = t["vence_en"] if isinstance(t["vence_en"], datetime) else futuro
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return (not t["grave"], not t["vencida"], t["vence_en"] is None, cuando, t["id"])


def _fila_de_tarea(t: dict, hoy: date, nombres: dict, comentarios: dict) -> dict:
    estado = t["estado"]
    return {
        "id": t["id"], "titulo": t["titulo"], "estado": estado,
        "es_hecha": estado == ESTADO_HECHA,
        "es_pendiente": estado == ESTADO_PENDIENTE,
        "vence_en": t["vence_en"], "dia": dia_rd(t["vence_en"]),
        "completado_en": t["completado_en"],
        # Por día de Santo Domingo, con LA definición (`grupo_de_tarea`): una
        # que vence hoy a las 9 no está vencida a las 10.
        "vencida": grupo_de_tarea(estado, t["vence_en"], hoy) == "atrasadas",
        # El NOMBRE, nunca el número de chat; sin nombre conocido no se pinta.
        "responsable": nombres.get(t["responsable_chat_id"]),
        "comentarios": comentarios.get(t["id"], []),
        "de_proyecto_en_papelera": False,
        # De dónde cuelga la tarea: lo necesita el renglón «¿sale una tarea
        # nueva de ésta?» de la página (1-oct-2026) para decidir lo MISMO que
        # decide /tareas — con proyecto, la nueva va al mismo proyecto y el
        # grupo no se elige; suelta, se ofrece el grupo que ya tiene.
        "proyecto_id": t["proyecto_id"],
        "area": t["area"],
        # Grave y todavía pendiente: la misma regla que /tareas. Con la
        # columna sin migrar nadie la trae y es False.
        "grave": bool(t.get("grave")) and estado == ESTADO_PENDIENTE,
        # «Sigue de» (14-oct-2026): la tarea de la que nació esta, si nació de otra.
        "deriva_de_id": t.get("deriva_de_id"),
        "deriva_de_titulo": t.get("deriva_de_titulo"),
    }


def con_la_recien_hecha_en_su_sitio(m: dict, tareas_p: dict, tid: int, hoy: date | None = None) -> dict:
    """Las listas de la página (`tareas_con_filtro`) con la tarea `tid`, recién marcada hecha, EN
    EL SITIO DONDE ESTABA entre las pendientes, para que el recuadro de «la que sigue» salga encima
    de ella y la lista no se mueva al marcar (14-oct-2026). Sin esto la tarea caería al instante en
    «N hechas», plegado, y el recuadro quedaría escondido. Se busca en el proyecto ENTERO (`m`), no
    en lo filtrado: un filtro que esconde las hechas no puede esconder el recuadro.

    El sitio es el que le daría `_orden_de_pendientes` si siguiera pendiente (la fecha cuenta como
    vencida con la regla de `grupo_de_tarea` de HOY; «grave» no se recupera, porque solo vale
    mientras está pendiente). Devuelve las mismas listas, sin tocar `m`; si `tid` no es una tarea
    HECHA de `m`, las devuelve tal cual."""
    fila = next((f for f in m["otras"] if f["id"] == tid and f["es_hecha"]), None)
    if fila is None:
        return tareas_p
    hoy = hoy or hoy_rd()
    como_pendiente = dict(fila, vencida=grupo_de_tarea(ESTADO_PENDIENTE, fila["vence_en"], hoy) == "atrasadas")
    pendientes = sorted([*tareas_p["pendientes"], como_pendiente], key=_orden_de_pendientes)
    otras = [f for f in tareas_p["otras"] if f["id"] != tid]
    hechas = sum(1 for f in otras if f["es_hecha"])
    return {"pendientes": [fila if f["id"] == tid else f for f in pendientes], "otras": otras,
            "n_hechas": hechas, "n_otras": len(otras) - hechas}


# Los filtros de las tareas de un proyecto (parte 2 de la página completa, 8-oct-2026). El
# vocabulario es CERRADO: lo que no esté aquí no es un filtro y no esconde nada.
FILTROS_DE_TAREAS = ("pendientes", "vencidas", "mias", "persona")


def tarea_cumple_filtro(f: dict, filtro: str, quien: str = "", mi_nombre: str | None = None) -> bool:
    """¿Esta fila (`_fila_de_tarea`) se enseña con ese filtro? Mira SOLO lo que la fila ya dice:
    `es_pendiente`, `vencida` y `responsable` (el nombre). «Las mías» y «Por persona» comparan el
    NOMBRE del responsable, el mismo con el que la página lo pinta; una tarea sin responsable no es de
    nadie. Un filtro que no está en `FILTROS_DE_TAREAS` deja pasar todo (nunca esconde)."""
    if filtro == "pendientes":
        return bool(f["es_pendiente"])
    if filtro == "vencidas":
        return bool(f["vencida"])
    if filtro == "mias":
        return bool(mi_nombre) and f["responsable"] == mi_nombre
    if filtro == "persona":
        return bool(quien) and f["responsable"] == quien
    return True


def tareas_con_filtro(m: dict, filtro: str, quien: str = "", mi_nombre: str | None = None) -> dict:
    """Las tareas de un proyecto (`armar_pagina`) con el filtro puesto: las mismas listas y el mismo
    orden, sin las que no lo cumplen, y las cuentas del «N hechas» de la lista PLEGADA recalculadas
    sobre lo que queda. Las cifras de «Cómo va» siguen siendo las de `_resumen` (todo el proyecto)."""
    pendientes = [f for f in m["pendientes"] if tarea_cumple_filtro(f, filtro, quien, mi_nombre)]
    otras = [f for f in m["otras"] if tarea_cumple_filtro(f, filtro, quien, mi_nombre)]
    hechas = sum(1 for f in otras if f["es_hecha"])
    return {"pendientes": pendientes, "otras": otras, "n_hechas": hechas, "n_otras": len(otras) - hechas}


def _repartir(filas: list[dict]) -> tuple[list[dict], list[dict]]:
    """(pendientes ordenadas, no pendientes). Lo que no es `pendiente` —hecha,
    descartada o un estado que nadie declaró— va al segundo grupo, que también
    se ve: no se pierde nada por no estar en una lista."""
    pendientes = sorted((f for f in filas if f["es_pendiente"]), key=_orden_de_pendientes)
    otras = [f for f in filas if not f["es_pendiente"]]
    otras.sort(key=lambda f: (f["completado_en"] is None,
                              -(f["completado_en"].timestamp()
                                if isinstance(f["completado_en"], datetime) else 0),
                              f["id"]))
    return pendientes, otras


def _resumen(pendientes: list[dict], otras: list[dict]) -> dict:
    hechas = sum(1 for f in otras if f["es_hecha"])
    total = len(pendientes) + len(otras)
    return {
        "n_total": total, "n_hechas": hechas,
        "n_otras": len(otras) - hechas, "n_pendientes": len(pendientes),
        "n_vencidas": sum(1 for f in pendientes if f["vencida"]),
        "pct": round(hechas * 100 / total) if total else 0,
    }


def situacion_de_proyecto(estado_guardado, filas: list[dict]) -> dict:
    """LA PUERTA ÚNICA de la situación de un proyecto: su estado y su próxima tarea. Quien necesite
    decir «cómo está este proyecto» o «qué sigue» llama aquí; ningún otro sitio lo decide
    (`armar_pagina` la usa para la página, y de ella saca también el reparto y las cifras).

    `estado_guardado` es `proyectos.estado` tal cual está en la base (puede ser un texto fuera del
    vocabulario: `estado_calculado` lo trata como abierto). `filas` son las tareas VIVAS del proyecto
    ya hechas por `_fila_de_tarea` (una por tarea; las que no son `pendiente` cuentan como «otras»).

    Devuelve un diccionario con:
      `estado`        una de las cuatro palabras de `estado_calculado`: «Sin empezar», «En curso»,
                      «Pausado» o «Cerrado»;
      `pendientes`    las tareas pendientes, en el orden de `_orden_de_pendientes`; `otras`, el resto
                      (`_repartir`);
      `n_total`, `n_hechas`, `n_otras`, `n_pendientes`, `n_vencidas`, `pct`   las cifras de `_resumen`;
      `proxima`       la PRIMERA de `pendientes` si el proyecto NO está cerrado, y `None` si está
                      cerrado o no tiene ninguna pendiente (un proyecto en pausa sí dice cuál sigue).
                      Es `{id, titulo, vence, vencida, grave}`: `vence` es el día de Santo Domingo
                      (`date`) o `None`, `vencida` y `grave` son booleanos.

    DEFECTOS CONOCIDOS de las cuatro palabras, que se quedan así por decisión de Tiziano
    (13-oct-2026): un proyecto con todas sus tareas hechas y sin cerrar dice «En curso» (no hay
    palabra para «por cerrar»), y uno con tareas pendientes —también vencidas— y ninguna hecha dice
    «Sin empezar». «Pausado» y «Cerrado» son lo que dice el estado guardado, no lo que hagan las
    tareas. No es una frase escrita a mano por nadie."""
    pendientes, otras = _repartir(filas)
    resumen = _resumen(pendientes, otras)
    estado = estado_calculado(estado_guardado, resumen["n_hechas"])
    proxima = None
    if estado_guardado != ESTADO_PROYECTO_CERRADO and pendientes:
        f = pendientes[0]
        proxima = {"id": f["id"], "titulo": f["titulo"], "vence": f["dia"],
                   "vencida": bool(f["vencida"]), "grave": bool(f["grave"])}
    return {"estado": estado, "pendientes": pendientes, "otras": otras,
            "proxima": proxima, **resumen}


def _nota_del_modelo(f: dict, nombres: dict) -> dict:
    """Una fila de `notas_de_proyectos()` → lo que sabe la plantilla. `autor` es el NOMBRE, o «Autor sin
    nombre» (se sabe el chat pero no tiene nombre en `NOMBRES_POR_CHAT`), o «Autor desconocido»: la página
    nunca escribe un número de chat."""
    de = autor_de_nota(f)
    autor = (nombres.get(de) or "Autor sin nombre") if de is not None else "Autor desconocido"
    return {"id": f["id"], "autor": autor, "creado_en": f["creado_en"], "texto": f["contenido"]}


def orden_en_la_lista(m: dict) -> tuple:
    """LA PUERTA ÚNICA del orden de los proyectos dentro de un grupo (`armar_pagina`), la lista de la
    izquierda y la vista «Estado de los proyectos» (13-oct-2026, Tiziano: «ordenar los proyectos en
    cantidad de tareas pendientes, que sea automatico»). `m` es el modelo de un proyecto.

    Los ABIERTOS (también los pausados): el que más tareas pendientes tiene, primero (`n_pendientes`,
    la cifra de `situacion_de_proyecto`); a igual cantidad, por nombre sin mayúsculas, y a igual
    nombre, por `id`: el orden es el mismo en cada carga. Los CERRADOS no se ordenan por pendientes
    (siguen por nombre, como siempre) y van en su propia lista. Un proyecto sin tareas cuenta 0."""
    return (0 if m["cerrado"] else -m["n_pendientes"], m["nombre"].casefold(), m["id"])


def armar_pagina(areas, proyectos, tareas, huellas, comentarios, nombres,
                 hoy: date, participantes=(), fechas=None, carpetas=None, notas=None,
                 actividad=None, audios=None, cierres=None) -> dict:
    """El modelo de la página de proyectos, con todas las decisiones.

    `cierres` es lo que devuelve `cierres_de_proyectos()` (`None` o sin el proyecto = no se sabe
    cuándo se cerró: `cerrado_el` sale `None` y la página dice «Cerrado» sin fecha). Solo un proyecto
    CERRADO ahora lleva `cerrado_el` (el día en Santo Domingo del último cierre): uno reabierto no.

    `fechas` es lo que devuelve `fechas_de_proyectos()` (`None` = las columnas no existen: el
    proyecto sale con `fechas_disponibles` falso y nada de fechas). Los días para la entrega
    se cuentan contra `hoy` (Santo Domingo) y no se enseñan en un proyecto cerrado.
    `carpetas` es lo que devuelve `carpetas_de_proyectos()` (`None` = la columna no existe: el
    proyecto sale con `carpeta_disponible` falso y el bloque de la carpeta no se dibuja).
    `audios` es lo que devuelve `carpetas_de_audio()` (`None` = la columna no existe: el proyecto sale con
    `carpeta_audio_disponible` falso y la línea de audio no se dibuja; la carpeta de Drive no depende de esto).
    `notas` es lo que devuelve `notas_de_proyectos()` (`None` = la columna `autor_chat_id` no existe:
    el proyecto sale con `notas_disponibles` falso y el bloque de notas no se dibuja). Cada nota lleva
    el NOMBRE de su autor (`_nota_del_modelo`), nunca el número de chat.
    `actividad` es `{proyecto_id: [renglones de log_acciones]}` (la más nueva primero), sacado de la
    MISMA consulta que `huellas`; cada renglón se pasa a frase sin nombres (`actividad_de_renglones`).
    `huellas` es `[(proyecto_id, accion, ts)]`; `comentarios`, las filas de
    `comentarios_tarea` vivas; `nombres`, `{chat: nombre}`. EL REPARTO (diseño
    §5.2): cada tarea viva cae en EXACTAMENTE un sitio —dentro de su proyecto si
    el proyecto está vivo; si no, suelta en el grupo de su área; y lo que no
    tiene grupo (o su proyecto está en la papelera) va a «Sin grupo»—. Nada se
    pierde por no encajar: el cubo que se ve es el último.
    """
    # Las personas de cada proyecto y de cada tarea (`participantes`, ya sin las
    # quitadas): por NOMBRE (el de Noco al agregarlas), nunca un número de chat.
    personas_de_proyecto: dict[int, list] = {}
    personas_de_tarea: dict[int, list] = {}
    for x in participantes:
        ficha = {"id": x["id"], "nombre": x["nombre"], "rol": x["rol"]}
        if x["proyecto_id"] is not None:
            personas_de_proyecto.setdefault(x["proyecto_id"], []).append(ficha)
        else:
            personas_de_tarea.setdefault(x["tarea_id"], []).append(ficha)

    por_tarea: dict[int, list] = {}
    for c in comentarios:
        por_tarea.setdefault(c["tarea_id"], []).append({
            "id": c["id"], "autor": (nombres.get(c["autor_chat_id"])
                      or _config.autores_reservados().get(c["autor_chat_id"])
                      or "Alguien"),
            "creado_en": c["creado_en"], "texto": c["texto"],
            "editado": c.get("editado_en") is not None})

    ultimas: dict[int, list] = {}
    for pid, accion, ts in huellas:
        if accion not in ACCIONES_AUTOMATICAS:
            ultimas.setdefault(pid, []).append(ts)

    vivos = {p["id"]: p for p in proyectos}
    del_proyecto: dict[int, list] = {pid: [] for pid in vivos}
    sueltas_por_area: dict = {}
    for t in tareas:
        fila = _fila_de_tarea(t, hoy, nombres, por_tarea)
        fila["personas"] = personas_de_tarea.get(t["id"], [])
        pid = t["proyecto_id"]
        if pid in vivos:
            del_proyecto[pid].append(fila)
        else:
            if pid is not None:
                fila["de_proyecto_en_papelera"] = True
            sueltas_por_area.setdefault(None if pid is not None else t["area"], []).append(fila)

    claves = {a["clave"] for a in areas}
    modelo_proyectos: dict[int, dict] = {}
    for pid, p in vivos.items():
        # El estado, el reparto, las cifras y la próxima tarea: UNA sola decisión.
        situacion = situacion_de_proyecto(p["estado"], del_proyecto[pid])
        ultimo = ultimo_movimiento(p["creado_en"], ultimas.get(pid, []))
        dias = dias_sin_movimiento(ultimo, hoy)
        propias = (fechas or {}).get(pid)
        entrega_dicha = (frase_de_entrega(propias["entrega"], hoy)
                         if propias and p["estado"] != ESTADO_PROYECTO_CERRADO else None)
        modelo_proyectos[pid] = {
            "id": pid, "nombre": p["nombre"], "area": p["area"] if p["area"] in claves else None,
            "descripcion": p["descripcion"], "estado": p["estado"],
            # Con qué huella se abre el formulario de «De qué se trata»: `crud.editar` la
            # compara al guardar (`si_sigue_igual`) para no pisar lo que llegó en el medio.
            "descripcion_huella": huella_de_descripcion(p["descripcion"]),
            "cerrado": p["estado"] == ESTADO_PROYECTO_CERRADO,
            # Archivado = cerrado. El día del último cierre (de la huella `editar`); solo si ESTÁ
            # cerrado, y `None` si la huella no existe: la página no inventa fecha.
            "cerrado_el": (dia_rd((cierres or {}).get(pid))
                           if p["estado"] == ESTADO_PROYECTO_CERRADO else None),
            # Telegram pudo guardar un estado libre antes de que existiera la
            # puerta (`crud.PUERTAS["proyectos"]["estado"]`): se ve, no se oculta.
            "estado_conocido": p["estado"] in ESTADOS_PROYECTO,
            "estado_calculado": situacion["estado"],
            "proxima": situacion["proxima"],
            "cliente": (p["cliente_nombre"] or None),
            # La ficha de Noco del cliente (parte 9): con ella se le piden al registro las sesiones
            # del proyecto, y sin ella (o sin ficha) la página no le pregunta nada.
            "cliente_noco_id": p.get("cliente_noco_id"),
            "personas": personas_de_proyecto.get(pid, []),
            "responsable": nombres.get(p["responsable_chat_id"]),
            "pendientes": situacion["pendientes"], "otras": situacion["otras"],
            **{k: situacion[k] for k in ("n_total", "n_hechas", "n_otras", "n_pendientes",
                                         "n_vencidas", "pct")},
            "ultimo": ultimo, "dias_sin_movimiento": dias,
            "dormido": esta_dormido(p["estado"], dias),
            # Las fechas (parte 4). Sin las columnas (`fechas` es None, o el proyecto no salió en la
            # lectura) no se dibuja nada de esto. `entrega_frase` es None si no hay entrega o el
            # proyecto está cerrado; `entrega_vencida` pinta la frase en rojo.
            "fechas_disponibles": propias is not None,
            "inicio": propias["inicio"] if propias else None,
            "entrega": propias["entrega"] if propias else None,
            "termina_cuando": propias["termina_cuando"] if propias else None,
            "entrega_frase": entrega_dicha[0] if entrega_dicha else None,
            "entrega_vencida": bool(entrega_dicha and entrega_dicha[1]),
            # La carpeta (parte 5): el texto tal como está guardado. Si es un enlace lo decide la
            # plantilla con `enlace_de_carpeta` (una sola puerta), no este modelo.
            "carpeta_disponible": carpetas is not None and pid in carpetas,
            "carpeta": carpetas.get(pid) if carpetas else None,
            # La carpeta de audio (pieza 4 de «Carpetas del proyecto»): la ruta que alguien escribió, tal cual. Lucy no
            # ve el disco Sesiones; la plantilla la pinta siempre como texto, nunca como enlace.
            "carpeta_audio_disponible": audios is not None and pid in audios,
            "carpeta_audio": audios.get(pid) if audios else None,
            # Las notas (parte 6): las vivas de este proyecto, la más nueva primero. Sin la columna
            # `autor_chat_id` en la base (`notas` es `None`) no se dibuja nada de esto.
            "notas_disponibles": notas is not None,
            "notas": [_nota_del_modelo(f, nombres) for f in (notas or {}).get(pid, [])],
            # LA ACTIVIDAD (parte 7): las últimas huellas de este proyecto, ya en frases sin nombres.
            # Solo lo que la consulta trajo (su tope): no se inventa un renglón de más.
            "actividad": actividad_de_renglones((actividad or {}).get(pid, [])),
        }

    def _grupo(clave, color):
        mios = sorted((m for m in modelo_proyectos.values() if m["area"] == clave),
                      key=orden_en_la_lista)
        pend, otras = _repartir(sueltas_por_area.get(clave, []))
        return {"clave": clave, "color": color_de_grupo(color),
                "color_oscuro": color_oscuro_de_grupo(color),
                "abiertos": [m for m in mios if not m["cerrado"]],
                "cerrados": [m for m in mios if m["cerrado"]],
                "sueltas": {"pendientes": pend, "otras": otras,
                            **_resumen(pend, otras)}}

    grupos = [_grupo(a["clave"], a["color"]) for a in areas]
    # Lo que cae en el cubo que se ve: sin área, o con un área que ya no está.
    huerfanas = [f for k, fs in sueltas_por_area.items() if k not in claves for f in fs]
    sin_grupo = _grupo(None, None)
    pend, otras = _repartir(huerfanas)
    sin_grupo["sueltas"] = {"pendientes": pend, "otras": otras, **_resumen(pend, otras)}
    return {"grupos": grupos, "sin_grupo": sin_grupo, "proyectos": modelo_proyectos}


async def pagina_de_proyectos(hoy: date | None = None) -> dict:
    """Trae lo que hace falta de la base y lo entrega a `armar_pagina`. Cuatro
    lecturas (cinco con las personas), ninguna escribe. (Necesita las columnas de la migración
    `2026-10-02_proyectos_responsable_cliente_participantes.sql`, aplicada en
    producción el 1-oct-2026.)"""
    hoy = hoy or hoy_rd()
    grupos = await areas()
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, nombre, descripcion, estado, area, creado_en,
                   responsable_chat_id, cliente_nombre, cliente_noco_id
              FROM proyectos
             WHERE borrado_en IS NULL
            """)
        proyectos = list(await cur.fetchall())
        await cur.execute(
            """
            SELECT id, proyecto_id, area, titulo, estado, vence_en,
                   completado_en, creado_en, responsable_chat_id
              FROM tareas
             WHERE borrado_en IS NULL
            """)
        tareas = list(await cur.fetchall())
        # LAS HUELLAS DEL PROYECTO (parte 7): UNA SOLA FUENTE para «Último movimiento» y para la
        # Actividad. Antes traía una fila por (proyecto, acción) con la fecha más nueva; ahora trae
        # CADA huella con lo que hace falta para pasarla a frase —la tabla, la acción y `antes`/
        # `despues`, de donde sale el título que la tarea tenía ENTONCES (`_titulo_de_la_huella`; el
        # título de HOY no se trae a propósito, que sería decir algo falso)— y de aquí salen las dos
        # cosas: el renglón que decide el movimiento es el mismo que se pinta. Lo que la consulta
        # DEVUELVE está acotado a `TOPE_ACTIVIDAD` por proyecto; el SQL recorre y ordena todas las
        # huellas de esas cuatro tablas en cada carga, igual que ya hacía para «Último movimiento».
        # Las huellas automáticas (`ACCIONES_AUTOMATICAS`) se dejan fuera AQUÍ (el `NOT IN` de abajo):
        # es la ÚNICA puerta de lo que se pinta, y así tampoco ocupan sitio del tope. El `actor` y la
        # bandeja NO se traen: lo que no viaja no se puede escribir por descuido.
        # Las cuatro tablas de este `UNION` son las que «tocan» a un proyecto; la prueba de la
        # actividad saca la lista de aquí (`l.tabla = '…'`) y exige una frase por cada combinación.
        # El SQL va como TEXTO FIJO (sin f-string) para que el censo de escritores genéricos lo pueda
        # leer entero; los `%s` del `NOT IN` y los valores que van en ellos salen de `ACCIONES_QUE_
        # MUEVEN`/`ACCIONES_AUTOMATICAS` (el número de huecos lo vigila la prueba de la actividad).
        await cur.execute(
            """
            SELECT z.pid, z.tabla, z.accion, z.registro_id, z.ts, z.antes, z.despues
              FROM (SELECT y.*,
                           row_number() OVER (PARTITION BY y.pid
                                              ORDER BY y.ts DESC, y.id DESC) AS puesto
                      FROM (
                        SELECT l.registro_id AS pid, l.tabla, l.accion, l.registro_id, l.ts,
                               l.antes, l.despues, l.id
                          FROM log_acciones l
                         WHERE l.tabla = 'proyectos'
                        UNION ALL
                        SELECT t.proyecto_id, l.tabla, l.accion, l.registro_id, l.ts,
                               l.antes, l.despues, l.id
                          FROM log_acciones l JOIN tareas t ON t.id = l.registro_id
                         WHERE l.tabla = 'tareas' AND t.proyecto_id IS NOT NULL
                        UNION ALL
                        SELECT t.proyecto_id, l.tabla, l.accion, l.registro_id, l.ts,
                               l.antes, l.despues, l.id
                          FROM log_acciones l
                          JOIN comentarios_tarea c ON c.id = l.registro_id
                          JOIN tareas t ON t.id = c.tarea_id
                         WHERE l.tabla = 'comentarios_tarea' AND t.proyecto_id IS NOT NULL
                        UNION ALL
                        SELECT n.proyecto_id, l.tabla, l.accion, l.registro_id, l.ts,
                               l.antes, l.despues, l.id
                          FROM log_acciones l JOIN notas n ON n.id = l.registro_id
                         WHERE l.tabla = 'notas' AND n.proyecto_id IS NOT NULL
                      ) y
                     WHERE y.accion NOT IN (%s, %s)) z
             WHERE z.puesto <= %s
             ORDER BY z.pid, z.puesto
            """, (*ACCIONES_AUTOMATICAS, TOPE_ACTIVIDAD))
        renglones = list(await cur.fetchall())
        huellas = [(f["pid"], f["accion"], f["ts"]) for f in renglones]
        actividad: dict[int, list] = {}
        for f in renglones:
            actividad.setdefault(f["pid"], []).append(f)
        await cur.execute(
            """
            SELECT c.id, c.tarea_id, c.autor_chat_id, c.creado_en, c.texto,
                   c.editado_en
              FROM comentarios_tarea c JOIN tareas t ON t.id = c.tarea_id
             WHERE c.borrado_en IS NULL AND t.borrado_en IS NULL
             ORDER BY c.creado_en, c.id
            """)
        comentarios = list(await cur.fetchall())
        await cur.execute(
            """
            SELECT id, proyecto_id, tarea_id, noco_id, nombre, rol
              FROM participantes
             WHERE borrado_en IS NULL
             ORDER BY creado_en, id
            """)
        participantes = list(await cur.fetchall())
    # «GRAVE» (4-oct-2026): lectura aparte y en lote, tolerante a la columna
    # sin migrar (`graves_de`), igual que /tareas.
    graves = await graves_de([t["id"] for t in tareas])
    for t in tareas:
        t["grave"] = t["id"] in graves
    # «SIGUE DE» (14-oct-2026): de qué tarea nace cada una, de la misma lectura aparte que usa
    # /tareas (`derivaciones`, tolerante a la columna sin migrar: `{}`). El título es el de la tarea
    # madre si sigue viva; si no, queda en None y la página lo dice sin inventarlo.
    hijas_de = await derivaciones()
    titulo_de = {t["id"]: t["titulo"] for t in tareas}
    for t in tareas:
        t["deriva_de_id"] = hijas_de.get(t["id"])
        t["deriva_de_titulo"] = titulo_de.get(t["deriva_de_id"])
    # LAS FECHAS DEL PROYECTO (parte 4): lectura aparte, tolerante a las columnas sin migrar
    # (`fechas_de_proyectos` devuelve `None` y la página no dibuja fechas).
    fechas = await fechas_de_proyectos()
    # LA CARPETA (parte 5): otra lectura aparte, tolerante a su columna sin migrar.
    carpetas = await carpetas_de_proyectos()
    # LA CARPETA DE AUDIO (pieza 4 de «Carpetas del proyecto»): su propia lectura, tolerante a su columna sin migrar.
    audios = await carpetas_de_audio()
    # LAS NOTAS (parte 6): otra lectura aparte, tolerante a su columna sin migrar.
    notas = await notas_de_proyectos()
    # CUÁNDO SE CERRÓ cada archivado (pieza 2 de «Archivar y borrar»): lectura aparte y solo de lectura.
    cierres = await cierres_de_proyectos()
    return armar_pagina(grupos, proyectos, tareas, huellas, comentarios,
                        nombres_con_code(), hoy, participantes, fechas, carpetas, notas,
                        actividad, audios, cierres)


async def proyectos_de_cliente(noco_id: int, hoy: date | None = None) -> list[dict]:
    """Los proyectos de UN cliente (la ficha de NocoDB `noco_id`), para que Natalia le cuente
    cómo va lo suyo (diseño `natalia-tareas-en-proyectos`, pieza L1, 18-oct-2026).

    QUÉ PROYECTOS: los que NO están en la papelera y cuya `cliente_noco_id` ES esa ficha, sea cual
    sea su estado (activo, pausado o cerrado: «vivo» es lo mismo que en `armar_pagina`, que no esté
    borrado). El filtro por cliente va EN la consulta (`WHERE cliente_noco_id = %s`), no después:
    lo de otro cliente ni sale de la base. Las personas agregadas a un proyecto (`participantes`)
    NO cuentan: solo la ficha del cliente del proyecto. Un id sin proyectos da `[]`.

    QUÉ DICE CADA UNO: `estado` y `proxima` salen de `situacion_de_proyecto` (la misma puerta
    única que usa la página, con las mismas filas de `_fila_de_tarea` y las graves de `graves_de`),
    y `admite_tareas` de `proyecto_admite_tareas` (la misma que usa agregar una tarea). Aquí NO se
    recorta nada: devuelve `{id, nombre, estado, estado_guardado, proxima, admite_tareas,
    responsable_chat_id}` y quien lo manda afuera (`web/api_code.py`) decide qué campos salen.

    ORDEN: los que cuentan como activos (`cuenta_como_activo`), luego los pausados, luego los
    cerrados; dentro de cada grupo, por id."""
    hoy = hoy or hoy_rd()
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, nombre, estado, responsable_chat_id
              FROM proyectos
             WHERE cliente_noco_id = %s AND borrado_en IS NULL
             ORDER BY id
            """, (noco_id,))
        proyectos = list(await cur.fetchall())
        if not proyectos:
            return []
        await cur.execute(
            """
            SELECT t.id, t.proyecto_id, t.area, t.titulo, t.estado, t.vence_en,
                   t.completado_en, t.responsable_chat_id
              FROM tareas t JOIN proyectos p ON p.id = t.proyecto_id
             WHERE p.cliente_noco_id = %s AND p.borrado_en IS NULL
               AND t.borrado_en IS NULL
            """, (noco_id,))
        tareas = list(await cur.fetchall())
        admiten: dict[int, bool] = {}
        for p in proyectos:
            try:
                await proyecto_admite_tareas(cur, p["id"])
                admiten[p["id"]] = True
            except ProyectoNoAdmiteTareas:
                admiten[p["id"]] = False
    graves = await graves_de([t["id"] for t in tareas])
    por_proyecto: dict[int, list] = {p["id"]: [] for p in proyectos}
    for t in tareas:
        t["grave"] = t["id"] in graves
        por_proyecto[t["proyecto_id"]].append(_fila_de_tarea(t, hoy, {}, {}))
    salida = []
    for p in proyectos:
        situacion = situacion_de_proyecto(p["estado"], por_proyecto[p["id"]])
        salida.append({
            "id": p["id"], "nombre": p["nombre"], "estado": situacion["estado"],
            "estado_guardado": p["estado"], "proxima": situacion["proxima"],
            "admite_tareas": admiten[p["id"]],
            "responsable_chat_id": p["responsable_chat_id"]})
    salida.sort(key=lambda m: (
        0 if cuenta_como_activo(m["estado_guardado"]) else
        (1 if m["estado_guardado"] == ESTADO_PROYECTO_PAUSADO else 2), m["id"]))
    return salida


# ── LAS CONSULTAS DE NATALIA (pieza L3 del diseño «Natalia y los Proyectos», 18-oct-2026) ─────────────
#
# Cuando un cliente necesita algo, Natalia NO escribe una tarea: abre una consulta y Lucy le pregunta al
# responsable (DISENO.md §5). Esta pieza es la tabla (`consultas_de_natalia`, migración
# `2026-10-18_consultas_de_natalia.sql`), abrirla (`abrir_consulta_de_natalia`), leerla
# (`consultas_de_natalia_de_cliente`) y la UNA función que cambia su estado (`pasar_consulta_de_natalia`).
# La tarjeta al responsable (L4) y los botones Sí/No (L5, `contestar_consulta_de_natalia`) usan esa función.
#
# EL CERROJO DE «UN CLIENTE SOLO ABRE CONSULTAS DE SU PROYECTO» es UNO: `proyectos_de_cliente`, que filtra
# por la ficha EN su consulta SQL. Abrir una consulta busca el proyecto (o la tarea) ENTRE los de esa
# ficha; lo que no esté ahí no existe para esta función. Y la lectura filtra por `cliente_noco_id` en su
# propia consulta. Ninguna de las dos mira el proyecto de otro y después lo descarta.
TIPO_CONSULTA_TAREA_NUEVA = "tarea_nueva"
TIPO_CONSULTA_NUEVA_FECHA = "nueva_fecha"
ESTADO_CONSULTA_ABIERTA = "abierta"
ESTADO_CONSULTA_PREGUNTADA = "preguntada"
ESTADO_CONSULTA_ANOTADA = "anotada"
ESTADO_CONSULTA_RECHAZADA = "rechazada"
ESTADO_CONSULTA_NO_SE_PUDO = "no_se_pudo"
ESTADO_CONSULTA_RESUELTA = "resuelta"
# Lo que dijo el cliente viaja luego como comentario de la tarea («Pedido del cliente: «…». Confirmó X.»,
# L5) y un comentario tiene tope `LARGO_COMENTARIO` (2000): se deja holgura para ese envoltorio. Un pedido
# más largo se RECHAZA (no se corta: una consulta cortada le pregunta al responsable otra cosa).
LARGO_PEDIDO_DE_CONSULTA = 1500
# Cuántas consultas se leen de una vez (la lectura de Natalia pide las suyas por número).
TOPE_DE_CONSULTAS_POR_LECTURA = 100

# Los cambios de estado que existen. Una sola función (`pasar_consulta_de_natalia`) los escribe y solo
# acepta estos pares; el CHECK de la tabla ata además cada tipo a SUS estados (el par `abierta → resuelta`
# es de `nueva_fecha`: la tarea se arregló antes de que saliera la tarjeta).
TRANSICIONES_DE_CONSULTA = frozenset({
    (ESTADO_CONSULTA_ABIERTA, ESTADO_CONSULTA_PREGUNTADA),
    (ESTADO_CONSULTA_ABIERTA, ESTADO_CONSULTA_RESUELTA),
    (ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_ANOTADA),
    (ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_RECHAZADA),
    (ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_NO_SE_PUDO),
    (ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_RESUELTA),
})


class ConsultasSinTabla(Exception):
    """Falta la migración de las consultas (SQLSTATE 42P01). No se guardó ni se leyó nada."""


class ConsultaNoVale(Exception):
    """Una consulta que no se puede abrir. `clave` dice por qué, para que quien llama la traduzca sin
    adivinar por el texto: `tipo`, `titulo`, `pedido`, `fecha`, `proyecto` (no existe, no es de ese
    cliente o está en la papelera), `proyecto_cerrado`, `tarea` (no es la que sigue en un proyecto de ese
    cliente), `tarea_no_vencida`, `responsable` (nadie a quien preguntarle), `clave_ajena` (esa clave ya es
    de la consulta de otro cliente) y `clave_distinta` (esa clave ya se usó con otra consulta). El mensaje
    NUNCA repite lo pedido ni lleva un número de chat."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def _misma_peticion(fila: dict, quiere: dict) -> bool:
    """¿La consulta guardada es la que se está pidiendo otra vez? Mismo tipo y mismo contenido. El
    responsable solo se compara si quien pide dijo uno (vacío = «el que corresponda», que hoy pudo haber
    cambiado de persona sin que sea otra consulta)."""
    if fila["tipo"] != quiere["tipo"]:
        return False
    if quiere["tipo"] == TIPO_CONSULTA_TAREA_NUEVA:
        if (fila["proyecto_id"], fila["titulo"], fila["pedido"], fila["vence_en"]) != (
                quiere["proyecto_id"], quiere["titulo"], quiere["pedido"], quiere["vence_en"]):
            return False
    elif fila["tarea_id"] != quiere["tarea_id"]:
        return False
    return (quiere["responsable_chat_id"] is None
            or fila["responsable_chat_id"] == quiere["responsable_chat_id"])


async def _consulta_por_clave(cur, clave: str):
    """La fila (con lo que hace falta para comparar) de la consulta que tiene esa clave, o `None`."""
    await cur.execute(
        "SELECT id, clave, tipo, estado, cliente_noco_id, proyecto_id, titulo, pedido, vence_en, "
        "tarea_id, responsable_chat_id FROM consultas_de_natalia WHERE clave = %s", (clave,))
    return await cur.fetchone()


async def abrir_consulta_de_natalia(noco_id: int, clave: str, tipo: str, *, proyecto_id=None, titulo=None,
                                    pedido=None, vence="", tarea_id=None, responsable_chat_id=None,
                                    hoy: date | None = None) -> tuple[int, bool]:
    """Abre la consulta de Natalia para la ficha `noco_id` y devuelve `(consulta_id, reusada)`; o lanza
    `ConsultaNoVale` (nada guardado) o `ConsultasSinTabla`. Quien contesta al HTTP vuelve a LEER la
    consulta por ese id (`consultas_de_natalia_de_cliente`): lo que se responde sale de la base, no de lo
    que se mandó.

    LA MISMA CLAVE ES LA MISMA CONSULTA. Si la clave ya existe se devuelve esa consulta con `reusada=True`,
    ANTES de validar nada de la base: un reintento tras cambiar el mundo (el proyecto se cerró, el
    responsable salió) tiene que devolver lo que ya se abrió, no un error nuevo. Si es de otro cliente, o
    se pidió algo distinto con la misma clave, es `ConsultaNoVale`. Y UNA `nueva_fecha` viva por tarea: la
    segunda apertura (con otra clave) devuelve la viva, `reusada=True`; la última puerta es el índice único
    parcial de la tabla, no una comprobación previa (dos aperturas a la vez no pasan las dos).

    `tarea_nueva` (proyecto, título, pedido, fecha propuesta y, opcional, responsable): se valida con las
    MISMAS piezas que «Agregar tarea» del panel (`titulo_de_tarea_que_vale`, `vence_de_dia_valido`,
    `proyecto_admite_tareas` dentro de `proyectos_de_cliente`), más que el proyecto sea de la ficha y que
    el responsable (el dicho, o el del proyecto si no se dijo) cumpla `_responsable_de_consulta_que_vale`.
    `nueva_fecha` (tarea): la tarea tiene que ser LA SIGUIENTE (`proxima`) de un proyecto de la ficha y
    estar vencida; se le pregunta a su responsable, o al del proyecto si ese no puede."""
    hoy = hoy or hoy_rd()

    def _a_quien_se_puede_preguntar(chat) -> bool:
        # LAS DOS puertas de siempre: quién puede ser responsable de una tarea y a quién Lucy le puede
        # escribir por Telegram (Code cumple la primera y no la segunda).
        return chat is not None and puede_ser_responsable(chat) and _config.puede_recibir_telegram(chat)

    if tipo == TIPO_CONSULTA_TAREA_NUEVA:
        if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
            raise ConsultaNoVale("proyecto", "ese proyecto no es de este cliente o no existe")
        try:
            titulo = titulo_de_tarea_que_vale(titulo)
        except ValueError as e:
            raise ConsultaNoVale("titulo", str(e)) from None
        pedido = pedido.strip() if isinstance(pedido, str) else ""
        if not pedido:
            raise ConsultaNoVale("pedido", "lo que pidió el cliente no puede quedar vacío")
        if len(pedido) > LARGO_PEDIDO_DE_CONSULTA:
            raise ConsultaNoVale(
                "pedido", f"lo que pidió el cliente no puede pasar de {LARGO_PEDIDO_DE_CONSULTA} caracteres")
        ok, vence_en = vence_de_dia_valido(vence if isinstance(vence, str) else "x")
        if not ok:
            raise ConsultaNoVale("fecha", "la fecha propuesta no vale: va como AAAA-MM-DD, o vacía")
    elif tipo == TIPO_CONSULTA_NUEVA_FECHA:
        if isinstance(tarea_id, bool) or not isinstance(tarea_id, int):
            raise ConsultaNoVale("tarea", "esa tarea no es la que sigue en un proyecto de este cliente")
        titulo = pedido = vence_en = None
    else:
        raise ConsultaNoVale("tipo", "el tipo de consulta no existe")
    quiere = {"tipo": tipo, "proyecto_id": proyecto_id, "titulo": titulo, "pedido": pedido,
              "vence_en": vence_en, "tarea_id": tarea_id, "responsable_chat_id": responsable_chat_id}

    def _la_ya_abierta(fila):
        if fila["cliente_noco_id"] != noco_id:
            raise ConsultaNoVale("clave_ajena", "esa clave ya está en uso")
        if not _misma_peticion(fila, quiere):
            raise ConsultaNoVale("clave_distinta", "esa clave ya se usó con otra consulta distinta")
        return fila["id"], True

    # 1. ¿La clave ya existe? Si sí, esa es la consulta.
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            existente = await _consulta_por_clave(cur, clave)
    except Exception as e:
        if _tabla_ausente(e):
            raise ConsultasSinTabla("falta la migración de las consultas de Natalia") from None
        raise
    if existente is not None:
        return _la_ya_abierta(existente)

    # 2. El proyecto (o la tarea) ENTRE los de esa ficha, y a quién se le pregunta.
    modelos = await proyectos_de_cliente(noco_id, hoy)
    vence_vencida_en = None
    if tipo == TIPO_CONSULTA_TAREA_NUEVA:
        m = next((m for m in modelos if m["id"] == proyecto_id), None)
        if m is None:
            raise ConsultaNoVale("proyecto", "ese proyecto no es de este cliente o no existe")
        if not m["admite_tareas"]:
            raise ConsultaNoVale("proyecto_cerrado", "ese proyecto está cerrado: no se le agregan tareas")
        a_quien = responsable_chat_id if responsable_chat_id is not None else m["responsable_chat_id"]
        if not _a_quien_se_puede_preguntar(a_quien):
            raise ConsultaNoVale("responsable", "no hay a quién preguntarle: ese responsable no puede ser")
    else:
        m = next((m for m in modelos if m["proxima"] is not None and m["proxima"]["id"] == tarea_id), None)
        if m is None:
            raise ConsultaNoVale("tarea", "esa tarea no es la que sigue en un proyecto de este cliente")
        if not m["proxima"]["vencida"]:
            raise ConsultaNoVale("tarea_no_vencida", "esa tarea no está vencida: no hay fecha nueva que pedir")
        proyecto_id = m["id"]
        a_quien = None          # se decide al leer la tarea, abajo

    # 3. Escribir. `ON CONFLICT DO NOTHING` cubre las DOS llaves únicas (la clave y la nueva_fecha viva).
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if tipo == TIPO_CONSULTA_NUEVA_FECHA:
                await cur.execute(
                    "SELECT vence_en, responsable_chat_id FROM tareas WHERE id = %s AND borrado_en IS NULL",
                    (tarea_id,))
                t = await cur.fetchone()
                if t is None:
                    raise ConsultaNoVale("tarea", "esa tarea no es la que sigue en un proyecto de este cliente")
                vence_vencida_en = t["vence_en"]
                a_quien = (t["responsable_chat_id"] if _a_quien_se_puede_preguntar(t["responsable_chat_id"])
                           else m["responsable_chat_id"])
                if not _a_quien_se_puede_preguntar(a_quien):
                    raise ConsultaNoVale(
                        "responsable", "no hay a quién preguntarle: ni la tarea ni el proyecto tienen quién pueda ser")
            await cur.execute(
                """
                INSERT INTO consultas_de_natalia
                  (clave, tipo, cliente_noco_id, proyecto_id, titulo, pedido, vence_en, tarea_id,
                   vence_vencida_en, responsable_chat_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (clave, tipo, noco_id, proyecto_id, titulo, pedido, vence_en, tarea_id,
                 vence_vencida_en, a_quien))
            nueva = await cur.fetchone()
            if nueva is not None:
                return nueva["id"], False
            # Chocó con una llave única: o la misma clave (otro pedido la abrió justo ahora) o ya hay una
            # nueva_fecha viva para esa tarea.
            existente = await _consulta_por_clave(cur, clave)
            if existente is not None:
                return _la_ya_abierta(existente)
            await cur.execute(
                "SELECT id FROM consultas_de_natalia WHERE tipo = %s AND tarea_id = %s "
                "AND estado IN (%s, %s) ORDER BY id LIMIT 1",
                (TIPO_CONSULTA_NUEVA_FECHA, tarea_id, ESTADO_CONSULTA_ABIERTA, ESTADO_CONSULTA_PREGUNTADA))
            viva = await cur.fetchone()
            if viva is None:
                raise RuntimeError("la consulta chocó con una llave única y no se encontró con quién")
            return viva["id"], True
    except ConsultaNoVale:
        raise
    except Exception as e:
        if _tabla_ausente(e):
            raise ConsultasSinTabla("falta la migración de las consultas de Natalia") from None
        raise


async def consultas_de_natalia_de_cliente(noco_id: int, ids, hoy: date | None = None) -> list[dict]:
    """Las consultas de la ficha `noco_id` que están entre `ids`, LEÍDAS de la base ahora, por id. Un id
    que no existe y uno de otra ficha dan lo mismo: no salen. Solo lee.

    Cada una: `{id, clave, tipo, estado, proyecto_id, titulo, tarea_id, tarea, siguiente}`. Nada de
    chats, `pedido`, `motivo` ni fechas internas. `tarea` solo si está `anotada`: la tarea que nació, RELEÍDA
    (`{id, titulo, vence, vencida, pendiente}`; `None` si ya no existe o no cuelga de un proyecto de esa ficha).
    `siguiente` solo si está `resuelta`: la `proxima` de su proyecto en este momento (la de
    `situacion_de_proyecto`; `None` si el proyecto ya no tiene o se cerró). Lanza `ConsultasSinTabla`."""
    hoy = hoy or hoy_rd()
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, int) and not isinstance(i, bool)))
    ids = ids[:TOPE_DE_CONSULTAS_POR_LECTURA]
    if not ids:
        return []
    filas: list[dict] = []
    tareas: dict[int, dict] = {}
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            # Una consulta SQL por id (son pocas, tope `TOPE_DE_CONSULTAS_POR_LECTURA`): ningún SQL se arma
            # al vuelo, y la ficha va EN cada consulta.
            for consulta_id in sorted(ids):
                await cur.execute(
                    "SELECT id, clave, tipo, estado, proyecto_id, titulo, tarea_id, tarea_resultante_id "
                    "FROM consultas_de_natalia WHERE cliente_noco_id = %s AND id = %s", (noco_id, consulta_id))
                fila = await cur.fetchone()
                if fila is not None:
                    filas.append(fila)
            for f in filas:
                nacida = f["tarea_resultante_id"]
                if f["estado"] != ESTADO_CONSULTA_ANOTADA or nacida is None:
                    continue
                await cur.execute(
                    "SELECT t.id, t.titulo, t.estado, t.vence_en, t.completado_en "
                    "FROM tareas t JOIN proyectos p ON p.id = t.proyecto_id "
                    "WHERE p.cliente_noco_id = %s AND p.borrado_en IS NULL AND t.borrado_en IS NULL "
                    "AND t.id = %s", (noco_id, nacida))
                t = await cur.fetchone()
                if t is not None:
                    tareas[t["id"]] = t
    except Exception as e:
        if _tabla_ausente(e):
            raise ConsultasSinTabla("falta la migración de las consultas de Natalia") from None
        raise
    siguientes: dict[int, dict | None] = {}
    if any(f["estado"] == ESTADO_CONSULTA_RESUELTA for f in filas):
        siguientes = {m["id"]: m["proxima"] for m in await proyectos_de_cliente(noco_id, hoy)}
    salida = []
    for f in filas:
        tarea = siguiente = None
        if f["estado"] == ESTADO_CONSULTA_ANOTADA and f["tarea_resultante_id"] in tareas:
            t = tareas[f["tarea_resultante_id"]]
            tarea = {"id": t["id"], "titulo": t["titulo"], "vence": dia_rd(t["vence_en"]),
                     "vencida": grupo_de_tarea(t["estado"], t["vence_en"], hoy, t["completado_en"]) == "atrasadas",
                     "pendiente": t["estado"] == ESTADO_PENDIENTE}
        if f["estado"] == ESTADO_CONSULTA_RESUELTA:
            siguiente = siguientes.get(f["proyecto_id"])
        salida.append({"id": f["id"], "clave": f["clave"], "tipo": f["tipo"], "estado": f["estado"],
                       "proyecto_id": f["proyecto_id"], "titulo": f["titulo"], "tarea_id": f["tarea_id"],
                       "tarea": tarea, "siguiente": siguiente})
    return salida


async def pasar_consulta_de_natalia(cur, consulta_id: int, de: str, a: str, *, tarjeta_msg_id=None,
                                    enviada_en=None, contestada_por_chat_id=None, contestada_en=None,
                                    tarea_resultante_id=None, motivo=None) -> bool:
    """LA ÚNICA FUNCIÓN QUE CAMBIA EL ESTADO de una consulta de Natalia. `cur` es un cursor con `dict_row`
    de la conexión de quien llama (L5, `contestar_consulta_de_natalia`, la mete en la MISMA transacción que crea la tarea). Solo pasa los
    pares de `TRANSICIONES_DE_CONSULTA` (otro es `ValueError`, nada escrito), y el `UPDATE` lleva
    `WHERE estado = <el de antes>`: si otro ya la movió, no cambia nada y devuelve `False` (el primero
    gana). Los campos que no se pasan (`None`) se quedan como estaban. El CHECK de la tabla impide un
    estado que no es de su tipo. Devuelve si cambió."""
    if (de, a) not in TRANSICIONES_DE_CONSULTA:
        raise ValueError("ese cambio de estado de una consulta no existe")
    await cur.execute(
        """
        UPDATE consultas_de_natalia
           SET estado = %s,
               tarjeta_msg_id = COALESCE(%s, tarjeta_msg_id),
               enviada_en = COALESCE(%s, enviada_en),
               contestada_por_chat_id = COALESCE(%s, contestada_por_chat_id),
               contestada_en = COALESCE(%s, contestada_en),
               tarea_resultante_id = COALESCE(%s, tarea_resultante_id),
               motivo = COALESCE(%s, motivo)
         WHERE id = %s AND estado = %s
        RETURNING id
        """,
        (a, tarjeta_msg_id, enviada_en, contestada_por_chat_id, contestada_en, tarea_resultante_id,
         motivo, consulta_id, de))
    return (await cur.fetchone()) is not None


# ── EL DESPERTADOR DE LAS CONSULTAS (pieza L4 del diseño «Natalia y los Proyectos», 18-oct-2026) ───────────
#
# Lo que el despertador (`cerebro/despertador.py::revisar_consultas_de_natalia`) necesita de la base: qué
# consultas esperan su tarjeta, cuáles piden su recordatorio, cuáles son `nueva_fecha` vivas, y cómo se
# cambia el estado SIN que nadie más lo escriba (`pasar_consulta_de_natalia_sola` solo abre la conexión y
# llama a la única función que lo hace). Las lecturas traen lo que hace falta para escribir el texto de la
# tarjeta (proyecto, cliente, tarea); ninguna sale de la casa: el texto va al Telegram del responsable.
def _sin_tabla_de_consultas(e: Exception) -> Exception:
    """Lo que hay que lanzar ante `e` al leer las consultas: `ConsultasSinTabla` si falta la migración (42P01), el
    mismo error si es otra cosa (no se disfraza)."""
    return ConsultasSinTabla("falta la migración de las consultas de Natalia") if _tabla_ausente(e) else e


async def consultas_de_natalia_sin_tarjeta() -> list[dict]:
    """Las consultas `abierta` (la tarjeta al responsable todavía no salió), de la más vieja a la más nueva."""
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT c.id, c.tipo, c.estado, c.cliente_noco_id, c.proyecto_id, c.titulo, c.pedido, c.vence_en, "
                "c.tarea_id, c.responsable_chat_id, c.tarjeta_msg_id, c.enviada_en, c.recordada_en, "
                "p.nombre AS proyecto_nombre, p.cliente_nombre, "
                "t.titulo AS tarea_titulo, t.estado AS tarea_estado, t.vence_en AS tarea_vence_en, "
                "t.borrado_en AS tarea_borrada_en "
                "FROM consultas_de_natalia c "
                "LEFT JOIN proyectos p ON p.id = c.proyecto_id "
                "LEFT JOIN tareas t ON t.id = c.tarea_id "
                "WHERE c.estado = %s ORDER BY c.id",
                (ESTADO_CONSULTA_ABIERTA,))
            return [dict(f) for f in await cur.fetchall()]
    except Exception as e:
        raise _sin_tabla_de_consultas(e) from None


async def consultas_de_natalia_para_recordar(enviada_antes_de: datetime) -> list[dict]:
    """Las `preguntada` cuya tarjeta salió en `enviada_antes_de` o antes y a las que todavía no se les
    recordó (`recordada_en` vacío). NO hay tope por arriba: una consulta nunca caduca (respuesta 2A de
    Tiziano, DISENO.md §5.3)."""
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT c.id, c.tipo, c.estado, c.cliente_noco_id, c.proyecto_id, c.titulo, c.pedido, c.vence_en, "
                "c.tarea_id, c.responsable_chat_id, c.tarjeta_msg_id, c.enviada_en, c.recordada_en, "
                "p.nombre AS proyecto_nombre, p.cliente_nombre, "
                "t.titulo AS tarea_titulo, t.estado AS tarea_estado, t.vence_en AS tarea_vence_en, "
                "t.borrado_en AS tarea_borrada_en "
                "FROM consultas_de_natalia c "
                "LEFT JOIN proyectos p ON p.id = c.proyecto_id "
                "LEFT JOIN tareas t ON t.id = c.tarea_id "
                "WHERE c.estado = %s AND c.recordada_en IS NULL "
                "AND c.enviada_en IS NOT NULL AND c.enviada_en <= %s ORDER BY c.id",
                (ESTADO_CONSULTA_PREGUNTADA, enviada_antes_de))
            return [dict(f) for f in await cur.fetchall()]
    except Exception as e:
        raise _sin_tabla_de_consultas(e) from None


async def consultas_de_natalia_de_fecha_vivas() -> list[dict]:
    """Las `nueva_fecha` que siguen vivas (`abierta` o `preguntada`): las que el despertador vuelve a
    mirar en cada vuelta para saber si la fecha ya se arregló."""
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT c.id, c.tipo, c.estado, c.cliente_noco_id, c.proyecto_id, c.titulo, c.pedido, c.vence_en, "
                "c.tarea_id, c.responsable_chat_id, c.tarjeta_msg_id, c.enviada_en, c.recordada_en, "
                "p.nombre AS proyecto_nombre, p.cliente_nombre, "
                "t.titulo AS tarea_titulo, t.estado AS tarea_estado, t.vence_en AS tarea_vence_en, "
                "t.borrado_en AS tarea_borrada_en "
                "FROM consultas_de_natalia c "
                "LEFT JOIN proyectos p ON p.id = c.proyecto_id "
                "LEFT JOIN tareas t ON t.id = c.tarea_id "
                "WHERE c.tipo = %s AND c.estado IN (%s, %s) ORDER BY c.id",
                (TIPO_CONSULTA_NUEVA_FECHA, ESTADO_CONSULTA_ABIERTA, ESTADO_CONSULTA_PREGUNTADA))
            return [dict(f) for f in await cur.fetchall()]
    except Exception as e:
        raise _sin_tabla_de_consultas(e) from None


def consulta_de_fecha_sigue_viva(proxima: dict | None, tarea_id: int) -> bool:
    """LA PUERTA ÚNICA de «¿esta `nueva_fecha` sigue pendiente?». `proxima` es la `proxima` de
    `situacion_de_proyecto` (la de la página y la de `proyectos_de_cliente`) del proyecto de la consulta.
    Sigue viva solo si la tarea de la consulta SIGUE siendo la que sigue y esa tarea SIGUE vencida, en el
    sentido de `grupo_de_tarea` (por día de Santo Domingo, no por instante). Que la tarea se haya hecho, se
    haya borrado, ya no sea la siguiente, tenga fecha de hoy o de mañana, o se haya quedado sin fecha, la
    resuelve: no se mira QUIÉN movió la fecha ni por dónde."""
    return proxima is not None and proxima["id"] == tarea_id and bool(proxima["vencida"])


async def proxima_de_proyecto_de_cliente(noco_id: int, proyecto_id: int, hoy: date | None = None) -> dict | None:
    """La `proxima` (la de `situacion_de_proyecto`) del proyecto `proyecto_id` DE ESA FICHA, o `None` (no
    tiene ninguna, está cerrado, o ya no es de esa ficha o está en la papelera)."""
    for m in await proyectos_de_cliente(noco_id, hoy):
        if m["id"] == proyecto_id:
            return m["proxima"]
    return None


async def marcar_consulta_de_natalia_recordada(consulta_id: int, cuando: datetime) -> bool:
    """Deja `recordada_en = cuando` en una consulta `preguntada` que todavía no la tenía. Cada vuelta del despertador
    manda el recordatorio una vez y lo marca DESPUÉS de enviar; si dos contenedores coinciden en un redespliegue,
    el recordatorio puede salir dos veces y la base guarda la marca del primero. Devuelve si cambió. NO toca el estado (eso es solo de `pasar_consulta_de_natalia`)."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "UPDATE consultas_de_natalia SET recordada_en = %s "
            "WHERE id = %s AND estado = %s AND recordada_en IS NULL RETURNING id",
            (cuando, consulta_id, ESTADO_CONSULTA_PREGUNTADA))
        return (await cur.fetchone()) is not None


async def pasar_consulta_de_natalia_sola(consulta_id: int, de: str, a: str, **campos) -> bool:
    """`pasar_consulta_de_natalia` en su propia conexión, para quien (el despertador) no tiene transacción
    que compartir. No decide nada: los pares, el `WHERE estado = <el de antes>` y los campos son los de ella."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        return await pasar_consulta_de_natalia(cur, consulta_id, de, a, **campos)


# ── LOS BOTONES SÍ/NO DE LAS CONSULTAS (pieza L5 del diseño «Natalia y los Proyectos», 18-oct-2026) ──────────
#
# El responsable toca «Sí, se puede» o «No» en la tarjeta que mandó L4 (`acciones/botones.py::al_pulsar`, ramas `cns`
# y `cnn`), y ESTA es la función que decide, `contestar_consulta_de_natalia`. Todo lo que cambia —la consulta, y con el
# «Sí» la tarea, su comentario y el cliente como persona de la tarea— pasa en UNA transacción: o entra todo o no entra
# nada. Quedar `anotada` sin tarea (o con tarea y la consulta sin contestar) es exactamente lo que esa transacción impide.
#
#   · quién decide: SOLO el `responsable_chat_id` de la consulta (se compara aquí, no en el botón: cualquier camino
#     que llegue a esta función pasa por la misma comparación);
#   · el primero gana: el cambio de estado es `pasar_consulta_de_natalia` (`UPDATE … WHERE estado = <el de antes>`); si
#     devuelve que no cambió nada, esta llamada PERDIÓ y lo que se escribió con ella se deshace;
#   · lo que se le dice a quien tocó sale de RELEER la consulta (y su tarea) después del commit, no de lo que esta
#     función creía estar escribiendo: «anotada» solo si la tarea está ahí.
RESULTADO_NO_EXISTE = "no_existe"          # esa consulta no existe
RESULTADO_NO_APLICA = "no_aplica"          # es una `nueva_fecha`: no tiene Sí/No
RESULTADO_NO_ES_TUYA = "no_es_tuya"        # la decide otra persona
RESULTADO_TODAVIA = "todavia"              # su tarjeta aún no está marcada como enviada (`abierta`)
RESULTADO_ANOTADA = "anotada"
RESULTADO_RECHAZADA = "rechazada"
RESULTADO_NO_SE_PUDO = "no_se_pudo"
RESULTADO_SIN_CONFIRMAR = "sin_confirmar"  # la consulta dice anotada y la tarea no se encuentra
RESULTADO_PENDIENTE = "pendiente"          # sigue sin contestarse (no debería verse tras contestar)
ROL_DEL_CLIENTE_EN_LA_TAREA = "Cliente"
MOTIVO_PROYECTO_YA_NO_ES_DEL_CLIENTE = "el proyecto ya no es de ese cliente"
LARGO_NOMBRE_EN_COMENTARIO = 100


class _PerdioLaCarrera(Exception):
    """Otro ya contestó esa consulta (el `UPDATE … WHERE estado` no cambió nada): lo escrito en esta llamada se deshace."""


async def _leer_consulta_para_contestar(cur, consulta_id: int):
    """La consulta con lo que hace falta para contestarla y para decir cómo quedó: su proyecto, el cliente y la tarea que
    nació de ella (solo si sigue viva). UNA lectura para las dos cosas, así lo que se decide y lo que se cuenta salen
    del mismo SQL. `None` si no existe."""
    try:
        await cur.execute(
            "SELECT c.id, c.tipo, c.estado, c.cliente_noco_id, c.proyecto_id, c.titulo, c.pedido, c.vence_en, "
            "c.responsable_chat_id, c.tarea_resultante_id, c.motivo, "
            "p.nombre AS proyecto_nombre, p.cliente_nombre, p.cliente_noco_id AS proyecto_cliente_noco_id, "
            "t.id AS tarea_id, t.titulo AS tarea_titulo "
            "FROM consultas_de_natalia c "
            "LEFT JOIN proyectos p ON p.id = c.proyecto_id "
            "LEFT JOIN tareas t ON t.id = c.tarea_resultante_id AND t.borrado_en IS NULL "
            "WHERE c.id = %s", (consulta_id,))
        fila = await cur.fetchone()
    except Exception as e:
        raise _sin_tabla_de_consultas(e) from None
    return dict(fila) if fila is not None else None


async def consulta_de_natalia_leida(consulta_id: int):
    """`_leer_consulta_para_contestar` en su propia conexión (la relectura de después del commit, y la que corre
    `tools/humo.py`)."""
    async with pool.connection() as conn:
        return await _leer_consulta_para_contestar(conn.cursor(row_factory=dict_row), consulta_id)


def _comentario_del_pedido(c: dict, quien: str) -> str:
    """El comentario que Natalia deja en la tarea: lo que pidió el cliente y quién lo confirmó. `pedido` tiene tope
    (`LARGO_PEDIDO_DE_CONSULTA`) y el nombre se corta para que el comentario quepa siempre en `LARGO_COMENTARIO`."""
    quien = (quien or "").strip()[:LARGO_NOMBRE_EN_COMENTARIO]
    return f"Pedido del cliente: «{c['pedido']}». Confirmó {quien}."


async def _poner_al_cliente_en_la_tarea(cur, tarea_id: int, c: dict) -> None:
    """El cliente como persona de la tarea (rol «Cliente», DISENO.md §5.3). Su nombre es el que Lucy ya guarda en el
    proyecto (`proyectos.cliente_nombre`, el de Noco al asignarlo).

    EL CLIENTE DE LA CONSULTA Y EL DEL PROYECTO SON EL MISMO aquí: `_anotar_la_tarea_de_la_consulta` rechaza (`no_se_pudo`,
    «el proyecto ya no es de ese cliente») antes de llegar, si `proyectos.cliente_noco_id` es distinto del de la consulta
    (incluido NULL). Por eso `cliente_noco_id` (de la consulta) y `cliente_nombre` (del proyecto) son de la misma ficha.

    UN PROYECTO CON CLIENTE SIN NOMBRE NO EXISTE HOY: las tres puertas que ponen el cliente (`poner_cliente`,
    `crear_proyecto`, `convertir_tarea_en_proyecto`) escriben `cliente_noco_id` y `cliente_nombre` juntos, y el nombre
    sale de `_ficha_de_noco_que_vale`, que rechaza uno vacío. Medido por la sala el 18-oct-2026 en producción: 10
    proyectos vivos con `cliente_noco_id`, 0 sin `cliente_nombre`. Si algún día pasara, el lado estricto: se lanza
    `RuntimeError`, se deshace TODO el «Sí» y la consulta queda `preguntada` (nunca nace una tarea anotada sin la
    persona del cliente, ni se inventa un nombre)."""
    nombre = (c.get("cliente_nombre") or "").strip()
    if not nombre:
        raise RuntimeError("el proyecto tiene cliente pero no el nombre de su ficha: no se anota la tarea sin la persona del cliente")
    await cur.execute(
        "INSERT INTO participantes (tarea_id, noco_id, nombre, rol, creado_por_chat_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (tarea_id, c["cliente_noco_id"], nombre, ROL_DEL_CLIENTE_EN_LA_TAREA, CHAT_ID_NATALIA))
    nueva = await cur.fetchone()
    await cur.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues, motivo) "
        "VALUES ('natalia', 'crear', 'participantes', %s, NULL, %s, "
        "'el cliente, como persona de la tarea que Natalia anotó')",
        (nueva["id"], json.dumps(dict(nueva), default=str, ensure_ascii=False)))


async def _anotar_la_tarea_de_la_consulta(conn, cur, c: dict, chat_id: int, quien: str, ahora) -> bool:
    """El «Sí»: la tarea, su comentario, el cliente como persona y la consulta `preguntada → anotada`, en la transacción
    de `conn`. Devuelve si esta llamada ganó. Si el escritor de la tarea la rechaza (proyecto cerrado, responsable que
    ya no vale) NO queda nada de la tarea y la consulta pasa a `no_se_pudo` con el motivo. Cualquier otro fallo sube y
    lo deshace todo: la consulta sigue `preguntada` y el botón sigue sirviendo.

    ANTES de escribir nada: si el proyecto YA NO ES DE LA FICHA de la consulta (`proyectos.cliente_noco_id` distinto de
    `consultas_de_natalia.cliente_noco_id`, incluido que el proyecto haya quedado sin cliente) es un rechazo como los del
    escritor: la consulta pasa a `no_se_pudo` con ese motivo y no se crea nada. Lo de otro cliente no se toca (G2 de L3), y
    así nunca se mezcla el id de la ficha de la consulta con el nombre de otro cliente en el participante."""
    if c["proyecto_cliente_noco_id"] != c["cliente_noco_id"]:
        return await pasar_consulta_de_natalia(
            cur, c["id"], ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_NO_SE_PUDO,
            contestada_por_chat_id=chat_id, contestada_en=ahora, motivo=MOTIVO_PROYECTO_YA_NO_ES_DEL_CLIENTE)
    try:
        # El SAVEPOINT es lo que deja la tarea (y su fila de bandeja) en NADA cuando el escritor la rechaza o
        # cuando otro ganó la carrera, sin tirar la transacción entera.
        async with conn.transaction():
            tarea_id = await crear_tarea_desde_el_panel(
                CHAT_ID_NATALIA, c["titulo"], c["vence_en"], None, c["responsable_chat_id"],
                proyecto_id=c["proyecto_id"], conexion=conn, desde=DESDE_DE_NATALIA,
                motivo=f"confirmada por {quien} (consulta #{c['id']})")
            await _poner_al_cliente_en_la_tarea(cur, tarea_id, c)
            if await comentar_tarea(tarea_id, CHAT_ID_NATALIA, _comentario_del_pedido(c, quien), conexion=conn) is None:
                raise RuntimeError("no se guardó el comentario de la tarea que Natalia anotó")
            if not await pasar_consulta_de_natalia(
                    cur, c["id"], ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_ANOTADA,
                    contestada_por_chat_id=chat_id, contestada_en=ahora, tarea_resultante_id=tarea_id):
                raise _PerdioLaCarrera()
    except _PerdioLaCarrera:
        return False
    except (TareaNoValida, ProyectoNoAdmiteTareas) as e:
        return await pasar_consulta_de_natalia(
            cur, c["id"], ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_NO_SE_PUDO,
            contestada_por_chat_id=chat_id, contestada_en=ahora, motivo=str(e))
    return True


async def contestar_consulta_de_natalia(consulta_id: int, chat_id: int, acepta: bool, *, ahora=None) -> dict:
    """LA PUERTA ÚNICA de «el responsable contestó Sí o No». `chat_id` es el chat que tocó el botón; `acepta`, cuál
    botón. Devuelve `{"resultado", "gano", "tarea_id", "tarea_titulo", "motivo", "responsable_chat_id",
    "proyecto_nombre"}`; `resultado` es una de las constantes `RESULTADO_*`. Sin la migración lanza
    `ConsultasSinTabla`.

    · No existe / no es de Sí-No / la decide otro chat / su tarjeta aún no está marcada: no escribe nada.
    · `acepta=False`: `preguntada → rechazada`.
    · `acepta=True`: ver `_anotar_la_tarea_de_la_consulta`.
    · Una consulta que ya no está `preguntada` (otro toque ganó antes) no se toca: se cuenta cómo quedó.
    `gano` dice si ESTA llamada fue la que la contestó. Todo lo que dice `resultado` sale de releer la consulta
    después del commit."""
    ahora = ahora or datetime.now(timezone.utc)
    quien = _config.NOMBRES_POR_CHAT.get(chat_id) or "alguien de la casa"
    gano = False
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        c = await _leer_consulta_para_contestar(cur, consulta_id)
        if c is None:
            return {"resultado": RESULTADO_NO_EXISTE, "gano": False}
        if c["tipo"] != TIPO_CONSULTA_TAREA_NUEVA:
            return {"resultado": RESULTADO_NO_APLICA, "gano": False}
        if c["responsable_chat_id"] != chat_id:
            return {"resultado": RESULTADO_NO_ES_TUYA, "gano": False, "responsable_chat_id": c["responsable_chat_id"]}
        if c["estado"] == ESTADO_CONSULTA_ABIERTA:
            return {"resultado": RESULTADO_TODAVIA, "gano": False}
        if c["estado"] == ESTADO_CONSULTA_PREGUNTADA:
            if not acepta:
                gano = await pasar_consulta_de_natalia(
                    cur, c["id"], ESTADO_CONSULTA_PREGUNTADA, ESTADO_CONSULTA_RECHAZADA,
                    contestada_por_chat_id=chat_id, contestada_en=ahora)
            else:
                gano = await _anotar_la_tarea_de_la_consulta(conn, cur, c, chat_id, quien, ahora)
    leida = await consulta_de_natalia_leida(consulta_id)
    return _lo_que_quedo(leida, gano)


def _lo_que_quedo(leida, gano: bool) -> dict:
    """Cómo quedó una consulta contestada, dicho a partir de lo RELEÍDO: «anotada» solo si la tarea está viva."""
    if leida is None:
        return {"resultado": RESULTADO_NO_EXISTE, "gano": gano}
    estado = leida["estado"]
    if estado == ESTADO_CONSULTA_ANOTADA:
        resultado = RESULTADO_ANOTADA if leida["tarea_id"] is not None else RESULTADO_SIN_CONFIRMAR
    elif estado == ESTADO_CONSULTA_RECHAZADA:
        resultado = RESULTADO_RECHAZADA
    elif estado == ESTADO_CONSULTA_NO_SE_PUDO:
        resultado = RESULTADO_NO_SE_PUDO
    else:
        resultado = RESULTADO_PENDIENTE
    return {"resultado": resultado, "gano": gano, "tarea_id": leida["tarea_id"], "tarea_titulo": leida["tarea_titulo"],
            "motivo": leida["motivo"], "responsable_chat_id": leida["responsable_chat_id"],
            "proyecto_nombre": leida["proyecto_nombre"]}


async def derivaciones() -> dict[int, int]:
    """`{hija_id: madre_id}` de las tareas VIVAS que salen de otra.

    UNA LECTURA APARTE, no un cuarto nivel en la cascada de tolerancia de
    `tareas_por_grupo` (que ya tiene tres, por área y por «Primero:»): sumar
    una columna más ahí habría significado una cascada de OCHO consultas
    combinadas en vez de tres (documento de diseño, disenos/lucy-tarea-
    derivada/DISENO.md §3.4). Acá alcanza con un SELECT tonto y una sola
    tolerancia — mismo patrón que `conteo_pasos` con la tabla `micro_pasos`
    ausente, pero por COLUMNA ausente (42703, undefined_column) en vez de
    TABLA ausente (42P01, undefined_table): si la migración de este encargo
    no se aplicó todavía, `tareas.deriva_de_id` no existe y esto devuelve
    `{}` — ninguna tarea "sale de" otra, que es como se veían todas hasta
    ayer.

    `borrado_en IS NULL`: una tarea derivada que se mandó a la papelera no
    "sale de" nada a los ojos de la pantalla — no hay nada que enlazar con
    una fila que ya no se pinta. Y al revés, si la MADRE se borra, la hija
    sigue mostrando de dónde salió (`tareas_por_grupo` no filtra por eso al
    resolver el título: si la madre no está entre las filas traídas, el
    enlace sale sin título en vez de romper la pantalla).

    `tareas_por_grupo` la llama UNA vez y le cuelga a cada fila
    `deriva_de_titulo` (para la hija: «↳ Sale de: …») y `siguio` (para la
    madre: «→ Siguió: …», puede ser más de una — Tiziano pidió que se
    pudieran crear varias por cada tarea que se cierra).
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT id, deriva_de_id FROM tareas "
                "WHERE deriva_de_id IS NOT NULL AND borrado_en IS NULL")
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return {}
            raise
    return {f["id"]: f["deriva_de_id"] for f in filas}


async def tomadas_de(ids: list[int]) -> dict[int, object]:
    """`{tarea_id: tomada_en}` de las que la sala ya empezó a trabajar Y
    SIGUE SIENDO SU RESPONSABLE, entre las `ids` que se le pasan
    (26-sep-2026, §D, parte 3).

    `AND responsable_chat_id = CHAT_ID_CODE`, hallazgo del testigo sobre la
    vuelta anterior: `asignar_responsable` no borra `tomada_en` cuando la
    tarea se REASIGNA a otra persona (ni tiene por qué -- es la puerta
    genérica de CUALQUIER responsable, tocarla para que sepa de "Code" y
    "tomada" acoplaría una función compartida a este encargo). Filtrar acá,
    en la ÚNICA función que decide qué sale "en curso", hace que el caso NO
    EXISTA en vez de manejarlo: una tarea reasignada a un humano simplemente
    no tiene fila en el resultado, así que ninguna plantilla necesita
    preguntarse "¿pero sigue siendo de Code?" -- ya viene resuelto.

    UNA LECTURA APARTE, no un cuarto nivel en la cascada de tolerancia de
    `tareas_por_grupo` (que ya tiene tres, por área y por «Primero:») --
    mismo motivo, mismo patrón, que `derivaciones()` con `deriva_de_id`: un
    SELECT tonto y una sola tolerancia por columna ausente (42703). Si la
    migración `2026-09-26_tomada_en.sql` no se aplicó todavía, esto
    devuelve `{}` y ninguna tarea sale "en curso" -- que es como se veían
    todas hasta ahora.

    Con `ids` vacía no hace falta ni preguntarle a la base.
    """
    if not ids:
        return {}
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                "SELECT id, tomada_en FROM tareas "
                "WHERE id = ANY(%s) AND tomada_en IS NOT NULL "
                "AND responsable_chat_id = %s", (ids, CHAT_ID_CODE))
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return {}
            raise
    return {f["id"]: f["tomada_en"] for f in filas}


async def tareas_por_grupo(limite: int = TOPE_TAREAS, hoy: date | None = None) -> dict:
    """Las tareas vivas, repartidas en los grupos del panel.

    El SQL solo TRAE filas; el criterio lo pone `grupo_de_tarea`. Es a
    propósito: un criterio escrito en SQL solo se puede comprobar con una base
    delante, y esta suite es hermética por diseño. Acá el reparto es código
    Python que se prueba con un `hoy` fijo, y la consulta queda tan tonta que
    no tiene dónde esconder una regla.

    `responsable_chat_id` es QUIÉN TIENE PENDIENTE la tarea, y sale de la
    propia fila: es un chat de Telegram, el mismo con el que esa persona entra
    al panel. Su NOMBRE no está en ninguna tabla —no existe en este sistema una
    que tenga a la vez un chat y un nombre— y lo pone quien pinta, con
    `config.NOMBRES_POR_CHAT`. NULL es lo normal: una tarea sin responsable es
    una tarea que nadie tomó todavía, no un error.

    ESTA CONSULTA YA NO SE UNE CON `bandeja`. Hasta el 10-sep-2026 traía
    `b.chat_id AS quien` para la columna «Quién la anotó», que Tiziano sacó del
    panel —«nno es relevante quien la anoto»—. `tareas.bandeja_id` sigue en el
    SELECT y sigue escribiéndose: es la trazabilidad de la fila y la que va en
    `log_acciones`. Lo que se fue es la unión, que solo servía para pintar un
    número. La regla de que todo JOIN de acá tenga que ser LEFT sigue viva en
    `tests/test_panel_tareas.py`, esperando al que alguien escriba mañana.

    `completado_en` viaja por lo mismo que `responsable_chat_id`: es lo que
    `grupo_de_tarea` necesita para decidir si una cerrada ya se fue al
    Historial, y `web/app.py::tareas_historial` la usa para pintar CUÁNDO se
    cerró. No se calcula acá si "ya toca": eso es al criterio, no a la
    consulta — la misma separación que ya tiene "atrasada".

    EL ÁREA (encargo 4) sale de `COALESCE(p.area, t.area)`: si la tarea tiene
    proyecto, la restricción `tareas_area_no_con_proyecto` ya garantiza que
    `t.area` está en NULL, así que el `COALESCE` termina siendo el área del
    proyecto sin que este código tenga que preguntar "¿tiene proyecto?" — la
    base vuelve irrepresentable el caso que habría que manejar acá. El
    `LEFT JOIN` es obligatorio por la misma regla de siempre: una tarea sin
    proyecto (la mayoría) no puede desaparecer porque el JOIN la exija.

    «PRIMERO:» (encargo 6) sale de un segundo LEFT JOIN, de `tareas` contra sí
    misma por `t.primero_id = ant.id`. Cada fila trae `primero_id` (para
    saber SI espera a alguien), `primero_titulo` (para pintar «→ Primero:
    <título>») y `primero_estado` (para decidir si la de antes SIGUE
    pendiente). "Espera" no se guarda en ningún lado: se calcula acá abajo,
    fila por fila, con el mismo espíritu que "atrasada" — ver
    `grupo_de_tarea`. El JOIN ya descarta la tarea "Primero:" si está
    borrada, así que una tarea que esperaba a algo que se borró sale con
    `primero_titulo=None` y se pinta normal, sin "→ Primero:" — la decisión
    de Tiziano es que borrar la de antes también "enciende" a la que
    esperaba.

    SI LA COLUMNA `area` O LA COLUMNA `primero_id` TODAVÍA NO EXISTEN (las
    migraciones `2026-09-22_areas.sql` o `2026-09-22_primero.sql` no se
    aplicaron: SQLSTATE 42703, undefined_column), esto cae en cascada —
    primero a la consulta CON área pero SIN «Primero:» (de antes de este
    encargo), y si esa TAMBIÉN falla, a la de antes del encargo 4, sin
    ninguna de las dos — mismo patrón que `cerebro/consultar.py::
    _comentarios_presentes` usa para una tabla que todavía no existe (42P01),
    con un SAVEPOINT (`conn.transaction()` anidado) en cada intento para que
    el fallo no deje abortada la conexión antes de reintentar. Así Lucy puede
    arrancar (y el panel puede pintarse) ANTES de que la sala aplique
    NINGUNA de las dos migraciones, y da igual el orden en que las aplique —
    las tareas simplemente salen sin área y/o sin «Primero:», que es como se
    veían hasta ayer.

    CUALQUIER OTRO FALLO SE PROPAGA: si no se pudo traer las tareas por un
    motivo distinto, no hay nada seguro que devolver.
    """
    # TRES FORMAS DE LA CONSULTA (encargo 6 suma la tercera a las dos que ya
    # traía el encargo 4): `con_area_y_primero` trae, además del área, la
    # tarea "Primero:" -- su id, su título (para pintar "→ Primero: <título>")
    # y su estado (para decidir si "espera": ver el reparto en Python, más
    # abajo). `t.primero_id` viaja SUELTO, no solo lo del JOIN, porque
    # `ant.*` sale NULL tanto si `primero_id` es NULL como si la tarea
    # "Primero:" está borrada -- y esos dos casos necesitan distinguirse en
    # la plantilla del panel (ver el requisito "si la anterior se borra").
    #
    # `ant.borrado_en IS NULL` en el JOIN es la decisión de Tiziano puesta en
    # SQL: "si la anterior se borra ... se enciende sola" -- una tarea
    # "Primero:" borrada no cuenta como que sigue esperando, así que ni
    # siquiera hace falta mirar su estado, el JOIN ya no la trae.
    con_area_y_primero = """
            SELECT t.id, t.titulo, t.estado, t.vence_en, t.creado_en,
                   t.bandeja_id, t.responsable_chat_id, t.completado_en,
                   COALESCE(p.area, t.area) AS area, t.proyecto_id,
                   p.nombre AS proyecto_nombre,
                   t.primero_id, ant.titulo AS primero_titulo,
                   ant.estado AS primero_estado
              FROM tareas t
              LEFT JOIN proyectos p ON p.id = t.proyecto_id
              LEFT JOIN tareas ant ON ant.id = t.primero_id
                                   AND ant.borrado_en IS NULL
             WHERE t.borrado_en IS NULL
             ORDER BY t.vence_en ASC NULLS LAST, t.creado_en ASC, t.id ASC
             LIMIT %s
            """
    con_area_sin_primero = """
            SELECT t.id, t.titulo, t.estado, t.vence_en, t.creado_en,
                   t.bandeja_id, t.responsable_chat_id, t.completado_en,
                   COALESCE(p.area, t.area) AS area, t.proyecto_id,
                   p.nombre AS proyecto_nombre
              FROM tareas t
              LEFT JOIN proyectos p ON p.id = t.proyecto_id
             WHERE t.borrado_en IS NULL
             ORDER BY t.vence_en ASC NULLS LAST, t.creado_en ASC, t.id ASC
             LIMIT %s
            """
    sin_area_ni_primero = """
            SELECT t.id, t.titulo, t.estado, t.vence_en, t.creado_en,
                   t.bandeja_id, t.responsable_chat_id, t.completado_en
              FROM tareas t
             WHERE t.borrado_en IS NULL
             ORDER BY t.vence_en ASC NULLS LAST, t.creado_en ASC, t.id ASC
             LIMIT %s
            """

    def _columna_ausente(e: Exception) -> bool:
        try:
            return e.sqlstate == "42703"
        except AttributeError:
            return False

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            async with conn.transaction():
                await cur.execute(con_area_y_primero, (limite + 1,))
                filas = list(await cur.fetchall())
        except Exception as e:
            if not _columna_ausente(e):
                raise
            cur = conn.cursor(row_factory=dict_row)
            try:
                async with conn.transaction():
                    await cur.execute(con_area_sin_primero, (limite + 1,))
                    filas = list(await cur.fetchall())
            except Exception as e2:
                if not _columna_ausente(e2):
                    raise
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(sin_area_ni_primero, (limite + 1,))
                filas = list(await cur.fetchall())

    hay_mas = len(filas) > limite
    if hay_mas:
        filas = filas[:limite]

    # EL «2 DE 5» (encargo 7): una consulta APARTE, en LOTE, después de
    # tener la lista final de filas -- no un tercer intercambio con JOIN
    # dentro de la consulta de arriba. Las dos consultas grandes de este
    # archivo (`con_area_y_primero` y sus caídas) ya tienen dos JOINs y una
    # cascada de tolerancia de tres niveles por columnas que pueden no
    # existir todavía; sumarle un cuarto JOIN a causa de una tabla ENTERA
    # que puede no existir (no una columna: la migración de este encargo
    # puede no estar aplicada) habría significado una cascada de SEIS
    # combinaciones en vez
    # de tres. `conteo_pasos` ya tolera la tabla ausente por su cuenta
    # (devuelve `{}`), así que acá no hace falta ningún SAVEPOINT más.
    conteo = await conteo_pasos([f["id"] for f in filas])

    # «SALE DE» / «SIGUIÓ» (tarea derivada, 25-sep-2026): igual que el «2 de
    # 5» de arriba, una consulta APARTE en vez de sumar un cuarto JOIN a una
    # cascada que ya tiene tres niveles -- ver el docstring de
    # `derivaciones()`. Los TÍTULOS salen de las propias `filas` que esta
    # función ya trajo (hasta TOPE_TAREAS, sin filtrar por fecha ni estado:
    # una madre cerrada hace tiempo sigue viniendo en el grupo "Historial"),
    # así que no hace falta una tercera consulta para resolverlos -- el mismo
    # motivo por el que esta función no se une con `bandeja` desde el
    # 10-sep-2026. Si la madre o la hija quedaron FUERA de esas
    # TOPE_TAREAS=500 filas (la misma frontera que ya declara `hay_mas`), el
    # enlace se pinta sin título en vez de romper la pantalla.
    hijas_de = await derivaciones()  # {hija_id: madre_id}
    titulo_de = {f["id"]: f["titulo"] for f in filas}

    # «TOMADA» (26-sep-2026, §D, parte 3): mismo patrón que `hijas_de` arriba
    # -- una consulta aparte, en lote, después de tener la lista final de
    # filas. Cada fila que la sala ya tomó lleva su `tomada_en`; el resto,
    # `None`.
    tomada_en_de = await tomadas_de([f["id"] for f in filas])
    # «GRAVE» (4-oct-2026): igual, una consulta aparte y en lote (ver
    # `graves_de`); las graves se ordenan primero dentro de su grupo.
    graves = await graves_de([f["id"] for f in filas])
    for f in filas:
        f["tomada_en"] = tomada_en_de.get(f["id"])
        # Solo cuenta PENDIENTE: una grave ya cerrada no sube en su grupo.
        f["grave"] = f["id"] in graves and f.get("estado") == ESTADO_PENDIENTE
    siguio_de: dict[int, list[dict]] = {}
    for hija_id, madre_id in hijas_de.items():
        siguio_de.setdefault(madre_id, []).append(
            {"id": hija_id, "titulo": titulo_de.get(hija_id)})

    hoy = hoy or hoy_rd()
    por_clave: dict = {}
    for f in filas:
        c = conteo.get(f["id"])
        f["pasos_hechos"] = c["hechos"] if c else 0
        f["pasos_total"] = c["total"] if c else 0
        f["vence_dia"] = dia_rd(f.get("vence_en"))
        f["completado_dia"] = dia_rd(f.get("completado_en"))
        # `setdefault` y no `f["area"] = f.get("area")`: con la consulta SIN
        # área (la columna todavía no existe), la clave ni siquiera está en
        # la fila, y de esta forma queda `None` explícito en los dos casos
        # en vez de que la plantilla tenga que adivinar si falta la clave o
        # si de verdad no hay área.
        f.setdefault("area", None)
        # Mismo motivo que "area": el NOMBRE del proyecto (encargo 5) sale del
        # mismo LEFT JOIN, y con la consulta sin área tampoco viaja -- pero
        # una tarea sin proyecto YA da `None` de por sí (el JOIN no encuentra
        # fila), así que este `setdefault` es solo para el caso "la consulta
        # con área falló entera" (columna ausente), no para "sin proyecto".
        f.setdefault("proyecto_nombre", None)
        f.setdefault("proyecto_id", None)
        # «PRIMERO:» (encargo 6). Mismo motivo que "area" arriba: si la
        # consulta con `primero_id` falló entera (la columna no existe
        # todavía), la clave ni siquiera está en la fila.
        f.setdefault("primero_id", None)
        f.setdefault("primero_titulo", None)
        f.setdefault("primero_estado", None)
        # "ESPERA" es la MISMA decisión que toma `grupo_de_tarea` para
        # "atrasada": se CALCULA acá, cada vez que se pinta, nunca se guarda.
        # Una tarea "espera" si tiene `primero_id` Y la tarea que apunta
        # sigue viva y PENDIENTE -- el JOIN de arriba ya excluyó las
        # borradas, así que alcanza con mirar el estado. En cuanto la de
        # antes deja de estar pendiente (hecha, descartada, cualquier otro
        # estado que no sea 'pendiente', o borrada), esto da False SOLO -- la
        # base nunca guardó "esperando" en ningún lado, así que no hay nada
        # que "encender": el próximo pintado ya la ve normal.
        f["primero_esperando"] = (
            f.get("primero_id") is not None
            and f.get("primero_estado") == ESTADO_PENDIENTE)
        # «Sale de»: esta fila es una HIJA. `deriva_de_titulo` puede ser
        # `None` con `deriva_de_id` puesto -- la madre existe pero quedó
        # fuera de las `filas` traídas (frontera de arriba); la plantilla
        # decide qué pintar en ese caso.
        madre_id = hijas_de.get(f["id"])
        f["deriva_de_id"] = madre_id
        f["deriva_de_titulo"] = titulo_de.get(madre_id) if madre_id else None
        # «Siguió»: esta fila es una MADRE, y puede tener VARIAS hijas
        # (Tiziano: "pueden ser varias"). Solo las que sí resolvieron
        # título -- una hija fuera de la frontera no se pinta muda.
        f["siguio"] = [s for s in siguio_de.get(f["id"], [])
                       if s["titulo"] is not None]
        clave = grupo_de_tarea(f.get("estado"), f.get("vence_en"), hoy,
                               f.get("completado_en"))
        por_clave.setdefault(clave, []).append(f)

    # Primero los grupos declarados, en su orden. Después, CUALQUIER clave que
    # haya salido y que nadie previó: se pinta al final con su clave cruda en
    # vez de desaparecer. Hoy `grupo_de_tarea` no puede devolver otra cosa —
    # pero "hoy no puede" es exactamente lo que se creía de la lista de estados
    # de db/schema.sql antes de que apareciera 'descartado' en producción.
    for fs in por_clave.values():
        fs.sort(key=lambda f: not f["grave"])      # estable; `grave` ya es «grave Y pendiente»
    grupos = [{"clave": c, "titulo": t, "filas": por_clave.pop(c)}
              for c, t in GRUPOS_DE_TAREAS if por_clave.get(c)]
    grupos += [{"clave": c, "titulo": c, "filas": fs}
               for c, fs in por_clave.items()]
    return {"grupos": grupos, "hay_mas": hay_mas}


async def marcar_tarea_hecha(tarea_id: int) -> bool:
    """Marca UNA tarea como hecha. Devuelve si de verdad cambió algo.

    Deja su huella en log_acciones como toda escritura del panel, con
    actor='panel': una tarea cerrada desde la web tiene que ser tan auditable y
    tan reversible como una cerrada por Telegram. El actor no es 'lucy' porque
    no fue Lucy — `acciones.crud._registrar` firma 'lucy' y por eso no se
    reutiliza acá, igual que no lo reutilizan `poner_categoria` ni
    `a_la_papelera`.

    `completado_en` se llena en el mismo UPDATE. La otra puerta —el modelo por
    Telegram— manda `{"estado": "hecha", "completado_en": "<ahora>"}` junto
    (`cerebro/agente.py:117`), así que las dos puertas dejan la fila igual. Dos
    caminos que dan resultados distintos para la misma acción es cómo se pierde
    la confianza en los dos.

    LO QUE YA ESTABA HECHO NO SE VUELVE A ESCRIBIR: devuelve False sin tocar
    nada. Eso cubre la carrera real —Rosi la marca, la pantalla de Tiziano
    lleva diez minutos abierta y la marca otra vez— sin duplicar la huella ni
    mover `completado_en` a la hora equivocada.

    LAS RECURRENTES NO NECESITAN NADA ESPECIAL ACÁ, y conviene saber por qué
    antes de "arreglarlo": `cerebro/despertador.py:519` barre las filas con
    recurrencia y estado 'hecha' y las reprograma solas. Barre por ESTADO, no
    por quién lo escribió, así que una tarea recurrente cerrada desde el panel
    se reprograma igual que una cerrada por Telegram.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, titulo, estado, vence_en, completado_en, bandeja_id "
            "FROM tareas WHERE id = %s AND borrado_en IS NULL", (tarea_id,))
        antes = await cur.fetchone()
        if antes is None or antes.get("estado") == ESTADO_HECHA:
            # No existe, está en la papelera, o ya estaba hecha. En los tres
            # casos no se escribe: una huella de una edición que no pasó es
            # basura permanente en la tabla que ES el deshacer.
            return False
        await conn.execute(
            "UPDATE tareas SET estado = %s, completado_en = now() WHERE id = %s",
            (ESTADO_HECHA, tarea_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'editar', 'tareas', %s, %s, %s,
                    'marcada hecha desde el panel de tareas', %s)
            """,
            (tarea_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"estado": ESTADO_HECHA}, ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


async def reabrir_tarea(tarea_id: int) -> bool:
    """Desmarca UNA tarea hecha: vuelve a `pendiente`. Devuelve si cambió algo.
    (Lucy 1.0, E6; P5 aprobada por Tiziano el 1-oct-2026: «sí; vuelve a
    pendiente con la misma fecha, y si ya pasó, sale roja».)

    `vence_en` no se toca (la misma fecha), y `completado_en` vuelve a NULL: ya
    no está completada. Por eso, si la fecha ya pasó, la tarea sale vencida sin
    que esta función haga nada: «vencida» la decide `grupo_de_tarea`.

    SOLO REABRE LO HECHO. Una tarea pendiente, descartada o de otro estado, una
    que no existe o una que está en la papelera: devuelve False sin escribir (una
    huella de una edición que no pasó es basura en la tabla que ES el deshacer).
    Una tarea de un proyecto CERRADO sí se puede reabrir: igual que seguir
    editándola, no es «recibir» una tarea nueva.

    Es la inversa de `marcar_tarea_hecha`, con la misma huella (`editar`, actor
    'panel', la fila de antes entera para que `crud.deshacer` sepa volver) y en
    la misma transacción que el UPDATE.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, titulo, estado, vence_en, completado_en, bandeja_id "
            "FROM tareas WHERE id = %s AND borrado_en IS NULL", (tarea_id,))
        antes = await cur.fetchone()
        if antes is None or antes.get("estado") != ESTADO_HECHA:
            return False
        await conn.execute(
            "UPDATE tareas SET estado = %s, completado_en = NULL WHERE id = %s",
            (ESTADO_PENDIENTE, tarea_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'editar', 'tareas', %s, %s, %s,
                    'desmarcada (vuelve a pendiente) desde el panel de proyectos', %s)
            """,
            (tarea_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"estado": ESTADO_PENDIENTE, "completado_en": None},
                        ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


async def donde_esta_la_tarea(tarea_id: int) -> dict | None:
    """`{proyecto_id, area, proyecto_vivo}` de una tarea, EN LA PAPELERA O NO, o
    `None` si no existe. Lo usan las rutas de la página de proyectos para volver
    al sitio de la tarea después de escribir (también después de borrarla).
    `proyecto_vivo` es verdadero si tiene proyecto y ese proyecto no está en la
    papelera. Solo lee."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT t.proyecto_id, t.area, "
            "(p.id IS NOT NULL AND p.borrado_en IS NULL) AS proyecto_vivo "
            "FROM tareas t LEFT JOIN proyectos p ON p.id = t.proyecto_id "
            "WHERE t.id = %s", (tarea_id,))
        fila = await cur.fetchone()
        return None if fila is None else {**fila, "proyecto_vivo": bool(fila["proyecto_vivo"])}


async def estado_y_fecha_de_tarea(tarea_id: int) -> dict | None:
    """`{estado, vence_en, borrada}` de una tarea (también en la papelera), o `None` si no existe.
    Solo lee. Lo usa la ruta de la fecha de Proyectos para decir POR QUÉ `mover_vence` no escribió."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT estado, vence_en, (borrado_en IS NOT NULL) AS borrada FROM tareas WHERE id = %s",
            (tarea_id,))
        fila = await cur.fetchone()
        return None if fila is None else {**fila, "borrada": bool(fila["borrada"])}


async def responsable_de_proyecto(proyecto_id: int) -> int | None:
    """El chat responsable de un proyecto vivo, o `None` (sin responsable, o el
    proyecto no existe). Una tarea nueva dentro del proyecto nace con él (P10).
    Solo lee."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT responsable_chat_id FROM proyectos "
            "WHERE id = %s AND borrado_en IS NULL", (proyecto_id,))
        fila = await cur.fetchone()
        return fila["responsable_chat_id"] if fila else None


async def tareas_de_code_pendientes() -> list[dict]:
    """Las tareas TÉCNICAS pendientes de Code. Es la consulta de solo
    lectura que expone `GET /api/code/tareas` (§C.4/§3 del diseño «Code
    como responsable», 26-sep-2026, parte 2 del plan de construcción).

    LA GUARDA DE VALOR VIVE EN EL `WHERE`, igual que `cerrar_tarea_de_la_
    sala` (parte 1): `responsable_chat_id = CHAT_ID_CODE` y área EFECTIVA
    (`COALESCE(t.area, p.area)`, mismo criterio que ya usa el panel) igual
    a `AREA_TECNICA`. Ninguna tarea que no cumpla las dos condiciones sale
    de acá, así que la ruta HTTP no tiene que volver a filtrar nada.

    `primero_id` viaja tal cual -- decidir si una tarea que todavía espera a
    otra debería ofrecérsele a la sala es un refinamiento que el diseño deja
    fuera a propósito (§6 del documento).

    ORDEN (4-oct-2026): las GRAVES primero (`t.grave DESC`; alertas que Natalia
    declaró graves), y dentro de cada grupo el orden de siempre. Cada fila
    trae `grave` (bool). Sin la columna, cae a la consulta de antes y todas
    salen `grave: false`.

    `tomada_en` (26-sep-2026, §D, parte 3): cuándo la sala EMPEZÓ esta tarea
    (`db.tomar_tarea_de_la_sala`), para que la lista -- y el panel -- puedan
    decir «en curso» en vez de solo «pendiente». SI LA COLUMNA TODAVÍA NO
    EXISTE (SQLSTATE 42703, mismo patrón que `tarea_con_comentarios` con
    `primero_id`), esto cae -- con un SAVEPOINT -- a la consulta de ANTES de
    esta parte: la lista sigue sirviendo, cada fila con `tomada_en: None`.
    """
    # LAS GRAVES PRIMERO (4-oct-2026): `con_grave` es `con_tomada` más
    # `t.grave` y el orden `grave DESC`. Si la columna `grave` no existe
    # todavía (42703) se cae a `con_tomada`, y de ahí a `sin_tomada`: tres
    # peldaños, cada uno la consulta de antes.
    con_grave = """
        SELECT t.id, t.titulo, t.detalle, t.vence_en, t.creado_en,
               t.primero_id, t.tomada_en, t.grave, p.nombre AS proyecto
          FROM tareas t
          LEFT JOIN proyectos p ON p.id = t.proyecto_id
         WHERE t.borrado_en IS NULL
           AND t.estado = %s
           AND t.responsable_chat_id = %s
           AND COALESCE(t.area, p.area) = %s
         ORDER BY t.grave DESC, t.vence_en NULLS LAST, t.id
        """
    con_tomada = """
        SELECT t.id, t.titulo, t.detalle, t.vence_en, t.creado_en,
               t.primero_id, t.tomada_en, p.nombre AS proyecto
          FROM tareas t
          LEFT JOIN proyectos p ON p.id = t.proyecto_id
         WHERE t.borrado_en IS NULL
           AND t.estado = %s
           AND t.responsable_chat_id = %s
           AND COALESCE(t.area, p.area) = %s
         ORDER BY t.vence_en NULLS LAST, t.id
        """
    sin_tomada = """
        SELECT t.id, t.titulo, t.detalle, t.vence_en, t.creado_en,
               t.primero_id, p.nombre AS proyecto
          FROM tareas t
          LEFT JOIN proyectos p ON p.id = t.proyecto_id
         WHERE t.borrado_en IS NULL
           AND t.estado = %s
           AND t.responsable_chat_id = %s
           AND COALESCE(t.area, p.area) = %s
         ORDER BY t.vence_en NULLS LAST, t.id
        """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            async with conn.transaction():
                await cur.execute(
                    con_grave, (ESTADO_PENDIENTE, CHAT_ID_CODE, AREA_TECNICA))
                filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            cur = conn.cursor(row_factory=dict_row)
            try:
                async with conn.transaction():
                    await cur.execute(
                        con_tomada, (ESTADO_PENDIENTE, CHAT_ID_CODE, AREA_TECNICA))
                    filas = await cur.fetchall()
            except Exception as e2:
                try:
                    sqlstate = e2.sqlstate
                except AttributeError:
                    raise e2 from None
                if sqlstate != "42703":
                    raise
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    sin_tomada, (ESTADO_PENDIENTE, CHAT_ID_CODE, AREA_TECNICA))
                filas = await cur.fetchall()
    filas = list(filas)
    for f in filas:
        f.setdefault("tomada_en", None)
        f["grave"] = bool(f.get("grave", False))
    return filas


def _solo_columnas_reales(tabla: str, fila: dict) -> dict:
    """`fila` recortada a SOLO las claves que `tabla` declara de verdad en
    `db/schema.sql` (vía `columnas_declaradas()`, la MISMA fuente que ya usa
    `columnas_que_faltan()`/`cerebro/consultar.py` -- nunca una lista
    tecleada de "lo que sobra").

    EXISTE PORQUE `log_acciones.antes` alimenta `acciones.crud.deshacer()`,
    que arma `columnas = [c for c in antes if c not in NO_EDITABLES y no
    tiene puerta que lo impida]` y después
    `UPDATE {tabla} SET {c} = r.{c} FROM jsonb_populate_record(...)` con
    CADA clave que sobrevive ese filtro (`acciones/crud.py:1636-1643`). Una
    clave de `antes` que no sea una columna real de la tabla -- un alias de
    JOIN, un cálculo -- revienta ese `UPDATE` con
    `column "..." does not exist`, porque `NO_EDITABLES`/`PUERTAS` no la
    conocen y no tienen por qué: no son "la lista de columnas seguras", son
    "las columnas con una regla especial".

    HALLAZGO DEL TESTIGO sobre `cerrar_tarea_de_la_sala`/`tomar_tarea_de_
    la_sala` (26-sep-2026): las dos arman su `antes` con
    `COALESCE(t.area, p.area) AS area_efectiva`, que no es una columna de
    `tareas` -- `deshacer()` sobre esa huella intentaba
    `UPDATE tareas SET area_efectiva = r.area_efectiva ...` y reventaba.
    `deshacer()` es alcanzable desde Telegram sin restricción de `actor`
    (`cerebro/agente.py`), así que una huella de `actor='sala'` no está a
    salvo solo porque la sala no la vaya a deshacer ella misma.

    AUDITORÍA DE HERMANOS (26-sep-2026): de las 20 escrituras a
    `log_acciones` que hay en `db/db.py`/`acciones/crud.py`, las ÚNICAS DOS
    que arman `antes` a partir de un SELECT con una unión de tablas son
    `cerrar_tarea_de_la_sala` y `tomar_tarea_de_la_sala` -- las demás usan
    `SELECT *` (todas columnas reales por definición) o listan columnas
    reales a mano, sin alias (`marcar_tarea_hecha`, `asignar_responsable`,
    `mover_vence`, `cerrar_y_derivar`, `convertir_tarea_en_proyecto`, entre
    otras). Los demás sitios de este archivo que unen tablas
    (`tareas_por_grupo`, `tarea_con_comentarios`, `tareas_de_code_pendientes`,
    `choques_de_evento`, `correos_por_marcar_leidos`,
    `pagina_de_proyectos`) son de SOLO LECTURA: ninguno escribe
    `log_acciones`.
    """
    columnas = set(columnas_declaradas().get(tabla, ()))
    return {k: v for k, v in fila.items() if k in columnas}


async def cerrar_tarea_de_la_sala(tarea_id: int) -> bool:
    """Cierra UNA tarea TÉCNICA de Code. Devuelve si de verdad cerró algo.

    Diseño aprobado por Tiziano, 26-sep-2026 (§4, «Code como responsable»):
    la sala de control puede marcar como hecha una tarea suya, por esta MISMA
    puerta -- nunca un `UPDATE` suelto desde afuera. La puerta HTTP que la
    sala usa de verdad (§C del diseño) todavía no existe -- es otra parte del
    plan de construcción -- así que hoy esto se llama directo, en proceso,
    con el mismo patrón que ya prueba el resto de este archivo.

    LA GUARDA VIVE EN EL `WHERE` DE LA CONSULTA, NO EN UN `if` DESPUÉS: el
    `SELECT` de abajo (`consulta_elegible`, nombrada así para que
    `tests/test_code_responsable.py` la extraiga del árbol de sintaxis y la
    corra tal cual contra datos reales) solo trae la fila si SU área EFECTIVA
    -- la propia si la tarea está suelta, la del proyecto si tiene uno,
    `COALESCE(t.area, p.area)`, mismo criterio que ya usa el panel para
    pintar la etiqueta -- es `AREA_TECNICA` Y su `responsable_chat_id` es
    `CHAT_ID_CODE` Y no está ya `hecha` Y no está borrada. Si esa consulta no
    trae nada -- no existe, está en la papelera, ya estaba hecha, no es de
    Code, o no es Técnica -- esta función no escribe nada y devuelve `False`,
    sea cual sea la intención de quien la llamó: el `id` nunca decide solo,
    decide la fila que la base tiene HOY.

    EL RASTRO ES DISTINGUIBLE: `actor='sala'` (no 'panel' ni 'lucy' -- los
    dos que ya existen) y un `motivo` que nombra a Code explícitamente. Nadie
    más en este archivo escribe `actor='sala'` hoy: es la primera vez que
    alguien que no es una persona con sesión de panel ni el propio modelo de
    Telegram cierra una tarea.

    QUÉ NO HACE, a propósito: no revisa `recurrencia` -- eso lo resuelve
    `cerebro/despertador.py::_reprogramar_recurrentes`, que barre por ESTADO
    y no por quién cerró, igual que documenta `marcar_tarea_hecha` arriba --
    y no crea tareas derivadas: eso es `cerrar_y_derivar`, una pantalla del
    panel con campos que acá no existen. «La sala SOLO cierra» es la
    instrucción de Tiziano, textual, y esta función no hace nada más que
    eso.
    """
    consulta_elegible = """
            SELECT t.id, t.titulo, t.estado, t.vence_en, t.completado_en,
                   t.bandeja_id, t.responsable_chat_id,
                   COALESCE(t.area, p.area) AS area_efectiva
              FROM tareas t LEFT JOIN proyectos p ON p.id = t.proyecto_id
             WHERE t.id = %s
               AND t.borrado_en IS NULL
               AND t.estado <> %s
               AND t.responsable_chat_id = %s
               AND COALESCE(t.area, p.area) = %s
            """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            consulta_elegible,
            (tarea_id, ESTADO_HECHA, CHAT_ID_CODE, AREA_TECNICA))
        antes = await cur.fetchone()
        if antes is None:
            # No existe, está en la papelera, ya estaba hecha, no es de Code,
            # o no es Técnica -- la consulta de arriba ya descartó los cinco
            # casos, así que acá no hace falta volver a preguntar nada.
            return False
        await conn.execute(
            "UPDATE tareas SET estado = %s, completado_en = now() WHERE id = %s",
            (ESTADO_HECHA, tarea_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('sala', 'editar', 'tareas', %s, %s, %s,
                    'tarea técnica cerrada por la sala de control (Code)', %s)
            """,
            (tarea_id,
             json.dumps(_solo_columnas_reales("tareas", antes), default=str,
                        ensure_ascii=False),
             json.dumps({"estado": ESTADO_HECHA}, ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


async def tomar_tarea_de_la_sala(tarea_id: int) -> bool | None:
    """La sala marca que EMPEZÓ a trabajar la tarea `tarea_id` (§D, parte 3
    del plan de construcción, 26-sep-2026). Devuelve:

      · `True`  — la tomó de verdad recién ahora.
      · `False` — no se tocó nada: no existe, está borrada, no está
        `pendiente`, no es de Code, no es Técnica, o YA ESTABA tomada.
      · `None`  — la columna `tareas.tomada_en` TODAVÍA NO EXISTE
        (`db/migrations/2026-09-26_tomada_en.sql` sin aplicar). Es un
        tercer valor A PROPÓSITO, distinto de `False`: acá no hay un
        "tomar sin poder guardar cuándo" que tenga sentido -- a diferencia
        de una tarea derivada sin `deriva_de_id`, no existe una escritura
        parcial razonable -- así que se distingue de un rechazo por valor
        para que quien llama (la ruta HTTP) pueda contestar un error CLARO
        y registrado en vez de un 500 mudo o, peor, un 409 que mienta
        diciendo "no es de Code" cuando en realidad SÍ lo es.

    LA GUARDA VIVE EN EL `WHERE`, MISMO PATRÓN que `cerrar_tarea_de_la_
    sala`: `consulta_elegible` solo trae la fila si su área EFECTIVA es
    `AREA_TECNICA`, su `responsable_chat_id` es `CHAT_ID_CODE`, su `estado`
    es `pendiente`, Y `tomada_en` sigue en `NULL`. TOMARLA DOS VECES es
    un no-op seguro: la segunda llamada no encuentra la fila (ya no cumple
    `tomada_en IS NULL`) y devuelve `False` SIN pisar la hora que puso la
    primera -- la condición está en el `WHERE`, no en un `if` después de
    leer el valor viejo.

    EL RASTRO: `actor='sala'`, igual que al cerrar, con su propio motivo.
    """
    consulta_elegible = """
            SELECT t.id, t.titulo, t.estado, t.vence_en, t.bandeja_id,
                   t.responsable_chat_id, t.tomada_en,
                   COALESCE(t.area, p.area) AS area_efectiva
              FROM tareas t LEFT JOIN proyectos p ON p.id = t.proyecto_id
             WHERE t.id = %s
               AND t.borrado_en IS NULL
               AND t.estado = %s
               AND t.responsable_chat_id = %s
               AND COALESCE(t.area, p.area) = %s
               AND t.tomada_en IS NULL
            """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            async with conn.transaction():
                await cur.execute(
                    consulta_elegible,
                    (tarea_id, ESTADO_PENDIENTE, CHAT_ID_CODE, AREA_TECNICA))
                antes = await cur.fetchone()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            log.warning(
                "tomar_tarea_de_la_sala: falta tareas.tomada_en (falta la "
                "migracion db/migrations/2026-09-26_tomada_en.sql) -- no se "
                "puede tomar la tarea #%s", tarea_id)
            return None
        if antes is None:
            # No existe, borrada, no pendiente, no es de Code, no es
            # Técnica, o ya estaba tomada -- la consulta ya descartó los
            # seis casos.
            return False
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "UPDATE tareas SET tomada_en = now() WHERE id = %s", (tarea_id,))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('sala', 'editar', 'tareas', %s, %s, %s,
                    'tarea técnica tomada por la sala de control (Code)', %s)
            """,
            (tarea_id,
             json.dumps(_solo_columnas_reales("tareas", antes), default=str,
                        ensure_ascii=False),
             json.dumps({"tomada_en": "now"}, ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


MARCA_RECORTE = "… [recortado]"


async def _es_grave(conn, tarea_id: int) -> bool:
    """¿La tarea está marcada grave? Con la columna sin migrar, False."""
    try:
        async with conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT grave FROM tareas WHERE id = %s", (tarea_id,))
            fila = await cur.fetchone()
    except Exception as e:
        try:
            sqlstate = e.sqlstate
        except AttributeError:
            raise e from None
        if sqlstate == "42703":
            return False
        raise
    return bool(fila and fila["grave"])


async def graves_de(ids: list[int]) -> set[int]:
    """Los `ids` (de los que se le pasan) que son tareas GRAVES de Code
    (`tareas.grave`, 4-oct-2026). Misma forma que `tomadas_de`: una lectura
    aparte, en lote, que con la columna sin migrar devuelve el conjunto vacío
    (nada se pinta grave) en vez de reventar. Solo cuenta las que SIGUEN
    siendo de Code, por lo mismo que `tomadas_de`."""
    if not ids:
        return set()
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            # Todas las graves vivas de Code (son pocas: alertas técnicas) y se
            # cruzan con `ids` acá: sin `= ANY(%s)`, así la misma lectura sirve
            # para una lista de 500 filas y para la página entera de Proyectos.
            await cur.execute(
                "SELECT id FROM tareas WHERE grave AND borrado_en IS NULL "
                "AND responsable_chat_id = %s", (CHAT_ID_CODE,))
            filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42703":
                return set()
            raise
    return {f["id"] for f in filas} & set(ids)


async def crear_o_reusar_alerta_tecnica(
        clave: str, titulo: str, detalle: str, grave: bool = False,
        informe: dict | None = None,
        tope_detalle: int | None = None) -> int | None:
    """Convierte una alarma técnica de Lucy en una tarea de Code -- o, si ya
    hay una abierta para el MISMO problema, le agrega esta ocurrencia en vez
    de crear otra (§B, parte 4 del plan de construcción, 26-sep-2026).

    Devuelve el `id` de la tarea (nueva o reusada), o `None` si las columnas
    `clave_tecnica`/`ultima_alarma_en` todavía no existen (la migración
    `2026-09-26_alertas_tecnicas.sql` sin aplicar, SQLSTATE 42703). `None`
    es un tercer valor A PROPÓSITO, igual que en `tomar_tarea_de_la_sala`:
    no hay una "alerta a medias" razonable sin poder guardar la clave del
    dedupe, así que quien llama tiene que enterarse y avisar a Tiziano como
    antes -- no ignorar el problema.

    SIN DUPLICADOS, DECIDIDO CONTRA LA BASE: se busca una tarea PENDIENTE
    con la MISMA `clave_tecnica` (no un diccionario en memoria, que se
    pierde en cada redespliegue -- el defecto que tenía el canario hasta
    ahora). Si existe, se le AGREGA esta ocurrencia al `detalle` (con
    `||`, nunca reescribiendo lo que ya había) y se actualiza
    `ultima_alarma_en`; no se toca `log_acciones` de nuevo -- la tarea ya
    tiene su huella de creación, y esto es la MISMA tarea, no una edición
    que alguien pidió. Si NO existe -- porque es la primera vez, o porque
    la anterior con esa clave ya se CERRÓ (`estado <> 'pendiente'`, así que
    el SELECT no la encuentra) -- nace una tarea nueva: Técnica, de Code,
    con la clave puesta, y su propia huella en `log_acciones` (`actor=
    'lucy'`, porque es Lucy quien la crea sola, sin que nadie se lo pida
    en ese momento).

    PARÁMETROS DE LA PUERTA DE NATALIA (4-oct-2026; las cinco alarmas de
    Lucy no pasan ninguno y se comportan exactamente como antes):
      · `grave=True` marca la tarea (`tareas.grave`) -- al crearla o, si ya
        había una abierta, al reusarla. NUNCA baja una marca: `grave=False`
        sobre una tarea grave la deja grave. Con `grave=True` y la columna
        sin migrar devuelve `None` (no guarda una alerta grave sin poder
        decir que lo es). Con `grave=False` ni nombra la columna.
      · `informe`, un dict que esta función llena con lo que pasó, para quien
        necesita decirlo con verdad: `reusada` (bool) y `grave` (cómo quedó
        la tarea). Si devuelve `None`, no lo llena.
      · `tope_detalle`: el `detalle` de una tarea reusada NUNCA queda por
        encima de este largo: la repetición entra entera, cortada o no entra
        (ver `informe`). Es un valor que pone quien llama, no medido.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        # UN BLOQUEO POR CLAVE, ANTES DE LEER: dos alertas con la misma clave
        # que llegan casi a la vez se turnan (la segunda espera a que la
        # primera termine) y la segunda ve la tarea que la primera creó. Es un
        # bloqueo de transacción de Postgres: se suelta solo al terminar,
        # también si algo falla. Sin esto, leer-y-luego-escribir podía crear
        # dos tareas para la misma clave.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (clave,))
        try:
            await cur.execute(
                "SELECT id, detalle FROM tareas "
                "WHERE clave_tecnica = %s AND estado = %s AND borrado_en IS NULL",
                (clave, ESTADO_PENDIENTE))
            existente = await cur.fetchone()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            log.warning(
                "crear_o_reusar_alerta_tecnica: faltan tareas.clave_tecnica/"
                "ultima_alarma_en (falta la migracion db/migrations/"
                "2026-09-26_alertas_tecnicas.sql) -- clave=%s", clave)
            return None

        if existente is not None:
            ahora = f"\n\n— Volvió a pasar ({datetime.now(timezone.utc):%d/%m %H:%M} UTC): {detalle}"
            omitida = recortada = False
            if tope_detalle is not None:
                # EL TOPE ES DEL RESULTADO, no de lo que había antes: la
                # repetición solo entra en el espacio que queda. Si cabe, entra
                # entera; si cabe a medias, entra cortada (con la marca); si
                # no cabe nada, no entra. Nunca deja el `detalle` por encima.
                espacio = tope_detalle - len(existente["detalle"] or "")
                if espacio <= len(MARCA_RECORTE):
                    ahora, omitida = "", True
                elif len(ahora) > espacio:
                    ahora = ahora[: espacio - len(MARCA_RECORTE)] + MARCA_RECORTE
                    recortada = True
            # TODO O NADA: la marca de grave y el detalle van en UNA
            # transacción. Si cualquiera falla, no queda ni la una ni el otro.
            try:
                async with conn.transaction():
                    if grave:
                        await conn.execute(
                            "UPDATE tareas SET grave = true WHERE id = %s",
                            (existente["id"],))
                    await conn.execute(
                        "UPDATE tareas SET detalle = COALESCE(detalle, '') || %s, "
                        "ultima_alarma_en = now() WHERE id = %s",
                        (ahora, existente["id"]))
            except Exception as e:
                try:
                    sqlstate = e.sqlstate
                except AttributeError:
                    raise e from None
                if sqlstate != "42703" or not grave:
                    raise
                log.warning(
                    "crear_o_reusar_alerta_tecnica: falta tareas.grave "
                    "(falta la migracion db/migrations/2026-10-04_tarea_grave.sql)"
                    " -- clave=%s", clave)
                return None
            if informe is not None:
                informe["reusada"] = True
                informe["omitida"] = omitida
                informe["recortada"] = recortada
                informe["grave"] = bool(grave) or await _es_grave(conn, existente["id"])
            return existente["id"]

        # LA MISMA PUERTA que cualquier otro escritor de `responsable_chat_id`
        # (`tests/test_responsable.py::
        # test_toda_escritura_de_la_columna_pasa_por_la_misma_puerta`, que
        # censa TODO `execute` del repo que nombre la columna): acá el valor
        # es SIEMPRE `CHAT_ID_CODE`, nunca algo que alguien pidió, pero la
        # regla es "toda escritura pasa por la puerta", no "toda escritura
        # que reciba un valor de afuera" -- así que si algún día
        # `puede_ser_responsable` dejara de aceptar a Code, esto avisa en
        # vez de seguir escribiendo un responsable que la puerta ya rechaza.
        if not puede_ser_responsable(CHAT_ID_CODE):
            raise ValueError(
                "CHAT_ID_CODE ya no pasa por puede_ser_responsable -- no "
                "debería pasar nunca, revisar config.puede_ser_responsable")
        if grave:
            try:
                async with conn.transaction():
                    await cur.execute(
                        """
                        INSERT INTO tareas
                          (titulo, detalle, area, responsable_chat_id,
                           clave_tecnica, ultima_alarma_en, grave)
                        VALUES (%s, %s, %s, %s, %s, now(), true)
                        RETURNING id
                        """,
                        (titulo, detalle, AREA_TECNICA, CHAT_ID_CODE, clave))
                    fila = await cur.fetchone()
            except Exception as e:
                try:
                    sqlstate = e.sqlstate
                except AttributeError:
                    raise e from None
                if sqlstate != "42703":
                    raise
                log.warning(
                    "crear_o_reusar_alerta_tecnica: falta tareas.grave "
                    "(falta la migracion db/migrations/2026-10-04_tarea_grave.sql)"
                    " -- clave=%s", clave)
                return None
        else:
            await cur.execute(
                """
                INSERT INTO tareas
                  (titulo, detalle, area, responsable_chat_id, clave_tecnica,
                   ultima_alarma_en)
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING id
                """,
                (titulo, detalle, AREA_TECNICA, CHAT_ID_CODE, clave))
            fila = await cur.fetchone()
        if informe is not None:
            informe["reusada"] = False
            informe["omitida"] = False
            informe["recortada"] = False
            informe["grave"] = bool(grave)
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, despues, motivo)
            VALUES ('lucy', 'crear', 'tareas', %s, %s,
                    'alarma técnica convertida en tarea de Code')
            """,
            (fila["id"],
             json.dumps({"clave_tecnica": clave, "titulo": titulo},
                       ensure_ascii=False)))
        return fila["id"]


# De dónde vino una alerta se lee del DATO ESCRITO, no del texto: la puerta HTTP
# (`web/api_code.py::crear_alerta`) guarda `clave_tecnica` como
# `f"{quien}:{clave}"`, y `quien` lo resuelve la autenticación de la clave de
# Bearer (`config.CLAVES_API_CODE`), nunca el cuerpo del pedido. Las cinco
# alarmas de Lucy escriben claves fijas (`backup`, `latido_cosecha`,
# `canario:<remitente>:<señal>`) y nunca empiezan por este prefijo. Las de
# Natalia no disparan el aviso de Telegram de `tareas_tecnicas_atrasadas`:
# los graves ya le llegan a Tiziano por Telegram desde la propia Natalia, y
# lo técnico va a Code, no a Telegram (Tiziano, 4-oct-2026).
PREFIJO_ALERTAS_DE_NATALIA = f"{QUIEN_DE_NATALIA}:"


async def tareas_tecnicas_atrasadas(umbral_horas: int = 6) -> list[dict]:
    """Las tareas técnicas de Code que llevan más de `umbral_horas` sin que
    la sala las TOME (`tomada_en`, §D) desde la última vez que la falla
    ocurrió de verdad -- y a las que TODAVÍA no se les avisó a Tiziano de
    ese atraso (§B.3, parte 4).

    EL AVISO YA MANDADO SE MARCA EN `log_acciones`
    (`marcar_aviso_atraso_code`), no en memoria: sobrevive un redespliegue
    sin duplicar -- la MISMA fila de `log_acciones` sigue estando después de
    reiniciar el proceso -- y sin perderse -- no depende de que el proceso
    seguido sea el mismo que detectó el atraso la primera vez.

    LAS ALERTAS DE NATALIA NO ENTRAN (4-oct-2026): las tareas cuya
    `clave_tecnica` empieza por `PREFIJO_ALERTAS_DE_NATALIA` se excluyen en el
    `WHERE`, graves o no. Las cinco alarmas de Lucy siguen como estaban.

    SIN LA MIGRACIÓN (`clave_tecnica`/`ultima_alarma_en`/`tomada_en`
    ausentes, SQLSTATE 42703), devuelve `[]`: nada que revisar todavía,
    nunca revienta.
    """
    consulta = """
        SELECT t.id, t.titulo, t.clave_tecnica
          FROM tareas t
         WHERE t.clave_tecnica IS NOT NULL
           AND t.borrado_en IS NULL
           AND t.estado = %s
           AND t.tomada_en IS NULL
           AND substr(t.clave_tecnica, 1, %s) <> %s
           AND COALESCE(t.ultima_alarma_en, t.creado_en)
                 < now() - make_interval(hours => %s)
           AND NOT EXISTS (
                 SELECT 1 FROM log_acciones la
                  WHERE la.tabla = 'tareas' AND la.registro_id = t.id
                    AND la.accion = 'aviso_atraso_code')
         ORDER BY t.id
        """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                consulta, (ESTADO_PENDIENTE, len(PREFIJO_ALERTAS_DE_NATALIA),
                           PREFIJO_ALERTAS_DE_NATALIA, int(umbral_horas)))
            return list(await cur.fetchall())
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            return []


async def marcar_aviso_atraso_code(tarea_id: int) -> None:
    """Deja constancia en `log_acciones` de que YA se le avisó a Tiziano
    que esta tarea técnica de Code lleva atraso (§B.3). Es la marca que lee
    `tareas_tecnicas_atrasadas` para no volver a avisar -- persistida en la
    base, no en memoria, para que sobreviva un redespliegue.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO log_acciones (actor, accion, tabla, registro_id, motivo)
            VALUES ('lucy', 'aviso_atraso_code', 'tareas', %s,
                    'avisado a Tiziano: la sala no tomó esta tarea técnica a tiempo')
            """,
            (tarea_id,))


async def _ficha_de_noco_que_vale(noco_id, leer_persona, quien: str) -> tuple:
    """`(id, nombre)` de la ficha de Noco con ese Id, VUELTA A LEER de Noco con
    `leer_persona` (`noco_lectura.persona`): el nombre que se guarda es el que Noco
    devuelve, nunca uno que haya mandado el navegador (diseño §4, G6). Es la pieza
    común de `poner_cliente` y de `agregar_participante`. Una ficha que no existe,
    o que vuelve con otro Id o sin nombre, es un ValueError y no se escribe nada;
    si Noco no contesta, la excepción de `leer_persona` sube tal cual."""
    if isinstance(noco_id, bool) or not isinstance(noco_id, int) or noco_id <= 0:
        raise ValueError(f"{quien} tiene que ser el Id de una ficha de Noco")
    ficha = await leer_persona(noco_id)
    if not ficha:
        raise ValueError("esa ficha no existe en Noco")
    nombre = ficha.get("nombre")
    if ficha.get("id") != noco_id or not isinstance(nombre, str) or not nombre.strip():
        raise ValueError("Noco no devolvió una ficha que valga para ese Id")
    return noco_id, nombre.strip()


async def poner_cliente(proyecto_id: int, noco_id: int | None, *,
                        leer_persona) -> bool:
    """Pone, cambia o quita el cliente de UN proyecto que ya existe. Devuelve si cambió algo.
    (Lucy 1.0, E2, G6; reglas de «Un proyecto no se crea sin cliente», pieza 1.)

    EL NOMBRE QUE SE GUARDA ES EL QUE NOCO DEVUELVE PARA ESE Id, nunca uno que
    haya mandado quien llama: `leer_persona(noco_id)` es la función que vuelve a
    pedirle la ficha a Noco (`noco_lectura.persona`, de E3: `{id, nombre}` o
    `None`) y se pasa por parámetro, sin valor por omisión, para que no haya un
    camino que guarde sin haberla leído. Una ficha que no existe, o que vuelve
    con otro Id o sin nombre, se rechaza con ValueError y no se escribe nada. Si
    Noco no contesta, la excepción de `leer_persona` sube tal cual, antes de
    abrir ninguna conexión.

    EN CDS, ACD Y SIN GRUPO (`cliente_es_obligatorio`):
      · `noco_id=None` (quitar el cliente) se RECHAZA: `ClienteDelProyectoNoVale('obligatorio')`. Solo en IA se quita.
      · el NOMBRE DEL PROYECTO es el del cliente: `nombre` se escribe en la MISMA sentencia que las dos columnas del
        cliente (volver a escoger al mismo cliente cuando la ficha cambió de nombre en Noco actualiza los dos, y
        arregla un nombre viejo). La huella `editar` muestra las tres.
      · un solo proyecto ACTIVO por cliente: si el proyecto cuenta como activo y el cliente nuevo ya tiene OTRO,
        `ClienteConProyecto` y no cambia nada (volver a escoger al mismo cliente no es choque; un pausado o un
        cerrado pueden tomar cualquier cliente).
    En IA el cliente es OPCIONAL (las dos columnas NULL, que es como nacen) y el nombre no se toca.

    Lo que ya decía lo mismo no se reescribe ni deja huella. Las dos columnas se escriben JUNTAS en un solo UPDATE
    (el CHECK `proyectos_cliente_entero` lo exige) y con su huella `editar` de actor 'panel' en la misma
    transacción. Es el ÚNICO escritor de `cliente_noco_id` y `cliente_nombre` de un proyecto que existe:
    `crud.editar` y `crud.deshacer` las rechazan por `crud.PUERTAS`, así que Telegram no las toca.
    """
    if isinstance(proyecto_id, bool) or not isinstance(proyecto_id, int):
        raise ClienteDelProyectoNoVale("proyecto", "ese proyecto no existe o ya no está")
    nuevo_id, nuevo_nombre = None, None
    if noco_id is not None:
        # La causa viaja en la CLAVE (`ficha`): la pantalla dice «esa ficha no está en Noco» y no adivina por el texto.
        try:
            nuevo_id, nuevo_nombre = await _ficha_de_noco_que_vale(
                noco_id, leer_persona, "el cliente")
        except ValueError as e:
            raise ClienteDelProyectoNoVale("ficha", str(e)) from e

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT * FROM proyectos WHERE id = %s AND borrado_en IS NULL",
            (proyecto_id,))
        antes = await cur.fetchone()
        if antes is None:
            raise ClienteDelProyectoNoVale("proyecto", "ese proyecto no existe o ya no está")
        obligatorio = cliente_es_obligatorio(antes.get("area"))
        if obligatorio and noco_id is None:
            raise ClienteDelProyectoNoVale(
                "obligatorio", "un proyecto de CDS o ACD necesita cliente: no se quita")
        nombre_nuevo = nombre_que_toca(antes.get("area"), nuevo_nombre, None) if obligatorio else None
        if (antes.get("cliente_noco_id") == nuevo_id
                and antes.get("cliente_nombre") == nuevo_nombre
                and (not obligatorio or antes.get("nombre") == nombre_nuevo)):
            return False
        despues_fila = {**antes, "cliente_noco_id": nuevo_id, "cliente_nombre": nuevo_nombre}
        if obligatorio:
            despues_fila["nombre"] = nombre_nuevo
        # Primero «un solo activo por cliente» (es la causa que se le dice a la persona)…
        await proyecto_puede_quedar_activo(cur, proyecto_id, antes, despues_fila)
        # …y después el nombre: el del cliente puede ser el de otro proyecto ACTIVO (dos fichas con el mismo nombre).
        if (obligatorio and nombre_nuevo != antes.get("nombre") and cuenta_como_activo(antes.get("estado"))
                and await proyecto_vivo_con_nombre(cur, nombre_nuevo, excluir_id=proyecto_id) is not None):
            raise NombreDeProyectoNoVale("repetido", "ya hay otro proyecto con ese nombre")
        if obligatorio:
            await conn.execute(
                "UPDATE proyectos SET cliente_noco_id = %s, cliente_nombre = %s, nombre = %s "
                "WHERE id = %s", (nuevo_id, nuevo_nombre, nombre_nuevo, proyecto_id))
            cambio = {"cliente_noco_id": nuevo_id, "cliente_nombre": nuevo_nombre, "nombre": nombre_nuevo}
        else:
            await conn.execute(
                "UPDATE proyectos SET cliente_noco_id = %s, cliente_nombre = %s "
                "WHERE id = %s", (nuevo_id, nuevo_nombre, proyecto_id))
            cambio = {"cliente_noco_id": nuevo_id, "cliente_nombre": nuevo_nombre}
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'proyectos', %s, %s, %s,
                    'cliente cambiado desde el panel de proyectos')
            """,
            (proyecto_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps(cambio, ensure_ascii=False)))
        return True


# LAS PERSONAS DE UN PROYECTO O DE UNA TAREA (Lucy 1.0, E7; diseño §5.4). Una sola
# tabla (`participantes`) y DOS puertas, las únicas que la escriben:
# `agregar_participante` y `quitar_participante`. El rol («qué hace aquí») es
# texto libre de 1 a `LARGO_ROL_PARTICIPANTE` caracteres (el `CHECK` de la tabla
# dice lo mismo; acá se rechaza antes con un motivo legible).
LARGO_ROL_PARTICIPANTE = 80


class ParticipanteNoVale(ValueError):
    """Una persona que no se puede agregar o quitar. `clave` dice por qué
    (`sitio`, `rol`, `repetida`, `no_esta`, `quien`) para que la pantalla lo
    traduzca sin adivinar por el texto. Nunca repite lo que escribió la persona."""

    def __init__(self, clave: str, mensaje: str):
        super().__init__(mensaje)
        self.clave = clave


def _sitio_de_participante(donde) -> tuple:
    """`("proyecto"|"tarea", id)` o ParticipanteNoVale("sitio")."""
    try:
        sitio, sitio_id = donde
    except (TypeError, ValueError):
        raise ParticipanteNoVale("sitio", "esa persona va en un proyecto o en una tarea") from None
    if (sitio not in ("proyecto", "tarea") or isinstance(sitio_id, bool)
            or not isinstance(sitio_id, int)):
        raise ParticipanteNoVale("sitio", "esa persona va en un proyecto o en una tarea")
    return sitio, sitio_id


def _quien_escribe(chat_id) -> int:
    if (isinstance(chat_id, bool) or not isinstance(chat_id, int)
            or chat_id not in _config.CHAT_IDS_PERMITIDOS):
        raise ParticipanteNoVale("quien", "solo quien entra al panel puede cambiar las personas")
    return chat_id


async def agregar_participante(donde, noco_id: int, rol: str, chat_id: int, *,
                               leer_persona) -> dict:
    """Agrega UNA persona de Noco a un proyecto o a una tarea. Devuelve su fila.
    (Lucy 1.0, E7, G7.)

    `donde` es `("proyecto", id)` o `("tarea", id)`. TODO LO QUE DECIDE ESTÁ
    AQUÍ, una vez:
      · quién escribe: `chat_id` es de quien entra al panel (viene de la sesión,
        nunca de un campo del formulario) y queda en `creado_por_chat_id`;
      · la persona: el nombre se VUELVE A LEER de Noco con `leer_persona(noco_id)`
        (sin valor por omisión, para que no haya camino que guarde sin leerla) y
        se guarda el que Noco devuelve; Lucy solo LEE de Noco;
      · el rol: no vacío y de a lo sumo `LARGO_ROL_PARTICIPANTE` caracteres;
      · el sitio: el proyecto o la tarea tienen que estar VIVOS;
      · la misma persona no entra dos veces en el mismo sitio (los índices únicos
        de la tabla dicen lo mismo; acá se comprueba antes, con su motivo).
    UNA TRANSACCIÓN: el `INSERT` y su huella `crear participantes`. Agregar una
    persona NO cuenta como movimiento del proyecto (diseño §5.4): la huella es
    de la tabla `participantes`, y el «último movimiento» solo mira proyectos,
    tareas y comentarios.
    """
    sitio, sitio_id = _sitio_de_participante(donde)
    chat_id = _quien_escribe(chat_id)
    if not isinstance(rol, str) or not 1 <= len(rol.strip()) <= LARGO_ROL_PARTICIPANTE:
        raise ParticipanteNoVale(
            "rol", f"«qué hace aquí» va de 1 a {LARGO_ROL_PARTICIPANTE} caracteres")
    rol = rol.strip()
    noco_id, nombre = await _ficha_de_noco_que_vale(noco_id, leer_persona, "la persona")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if sitio == "proyecto":
            await cur.execute("SELECT id FROM proyectos WHERE id = %s AND borrado_en IS NULL",
                              (sitio_id,))
        else:
            await cur.execute("SELECT id FROM tareas WHERE id = %s AND borrado_en IS NULL",
                              (sitio_id,))
        if await cur.fetchone() is None:
            raise ParticipanteNoVale("sitio", "ese proyecto o esa tarea ya no está")
        if sitio == "proyecto":
            await cur.execute(
                "SELECT id FROM participantes WHERE proyecto_id = %s AND noco_id = %s "
                "AND borrado_en IS NULL", (sitio_id, noco_id))
        else:
            await cur.execute(
                "SELECT id FROM participantes WHERE tarea_id = %s AND noco_id = %s "
                "AND borrado_en IS NULL", (sitio_id, noco_id))
        if await cur.fetchone() is not None:
            raise ParticipanteNoVale("repetida", "esa persona ya está ahí")
        if sitio == "proyecto":
            await cur.execute(
                """
                INSERT INTO participantes (proyecto_id, noco_id, nombre, rol, creado_por_chat_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """, (sitio_id, noco_id, nombre, rol, chat_id))
        else:
            await cur.execute(
                """
                INSERT INTO participantes (tarea_id, noco_id, nombre, rol, creado_por_chat_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """, (sitio_id, noco_id, nombre, rol, chat_id))
        nueva = await cur.fetchone()
        await cur.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'crear', 'participantes', %s, NULL, %s,
                    'persona agregada desde el panel de proyectos')
            """,
            (nueva["id"], json.dumps(nueva, default=str, ensure_ascii=False)))
        return dict(nueva)


async def persona_ya_esta_ahi(cur, antes: dict) -> bool:
    """¿Hay otra fila VIVA de `participantes` con la misma persona de Noco en el
    mismo sitio que la fila `antes`? (`crud.deshacer` la mira antes de devolver a
    una persona quitada: los índices únicos de la tabla no dejan dos.)"""
    if antes.get("proyecto_id") is not None:
        await cur.execute(
            "SELECT id FROM participantes WHERE noco_id = %s AND proyecto_id = %s "
            "AND borrado_en IS NULL", (antes.get("noco_id"), antes["proyecto_id"]))
    else:
        await cur.execute(
            "SELECT id FROM participantes WHERE noco_id = %s AND tarea_id = %s "
            "AND borrado_en IS NULL", (antes.get("noco_id"), antes.get("tarea_id")))
    return await cur.fetchone() is not None


async def quitar_participante(participante_id: int, donde, chat_id: int) -> dict:
    """Quita (soft-delete) a UNA persona de un proyecto o de una tarea y devuelve
    su fila de antes. (Lucy 1.0, E7.) La PERTENENCIA la decide esta puerta: la
    persona tiene que ser de ESE sitio (`donde`), así que el id de una persona de
    otro proyecto, o de una tarea, no se puede quitar desde éste. Una ya quitada
    es `ParticipanteNoVale("no_esta")`. Nunca hay `DELETE`: `borrado_en` y
    `borrado_por_chat_id`, más la huella `borrar participantes` con el antes, en
    la misma transacción. Tampoco cuenta como movimiento."""
    sitio, sitio_id = _sitio_de_participante(donde)
    chat_id = _quien_escribe(chat_id)
    if isinstance(participante_id, bool) or not isinstance(participante_id, int):
        raise ParticipanteNoVale("no_esta", "esa persona ya no está ahí")
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if sitio == "proyecto":
            await cur.execute(
                "SELECT * FROM participantes WHERE id = %s AND proyecto_id = %s "
                "AND borrado_en IS NULL", (participante_id, sitio_id))
        else:
            await cur.execute(
                "SELECT * FROM participantes WHERE id = %s AND tarea_id = %s "
                "AND borrado_en IS NULL", (participante_id, sitio_id))
        antes = await cur.fetchone()
        if antes is None:
            raise ParticipanteNoVale("no_esta", "esa persona ya no está ahí")
        await cur.execute(
            "UPDATE participantes SET borrado_en = now(), borrado_por_chat_id = %s "
            "WHERE id = %s", (chat_id, participante_id))
        await cur.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'borrar', 'participantes', %s, %s, NULL,
                    'persona quitada desde el panel de proyectos')
            """,
            (participante_id, json.dumps(antes, default=str, ensure_ascii=False)))
        return dict(antes)


async def asignar_responsable(tarea_id: int, chat_id: int | None) -> bool:
    """Pone (o quita) quién tiene pendiente UNA tarea. Devuelve si cambió algo.

    QUITAR ES UNA OPERACIÓN DE PRIMERA, no un caso raro: `chat_id=None` deja la
    tarea sin responsable, que es el estado en el que nacieron las 57 que ya
    existían. Una tarea que nadie tomó todavía no es un error y no hay nada que
    validar; por eso `None` no pasa por la puerta de abajo.

    LA PUERTA ES `config.puede_ser_responsable`, Y ES LA MISMA QUE USA LA RUTA.
    Está acá además de en el panel a propósito: la validación de un formulario
    protege al formulario, no a la tabla. Cualquier camino que aparezca mañana
    —otra pantalla, el agente, un script— llega a la columna por esta función y
    se encuentra la misma puerta. Y no es una lista tecleada: sale de quién
    puede ENTRAR al panel cruzado con quién tiene nombre, o sea de las dos
    variables de Railway. La tercera persona que Tiziano dé de alta pasa sola.

    LO QUE YA ESTABA NO SE REESCRIBE: si la tarea ya tenía a esa misma persona
    —o ya estaba sin nadie y se manda vaciarla otra vez— devuelve False sin
    tocar nada. Es la misma decisión que `marcar_tarea_hecha` y por el mismo
    motivo: una huella en `log_acciones` de una edición que no pasó es basura
    permanente en la tabla que ES el deshacer de este proyecto.

    LA HUELLA GUARDA LA FILA ENTERA en `antes`, y no solo la columna que
    cambia, porque así es como `acciones.crud.deshacer` sabe volver atrás: su
    rama de 'editar' arma la escritura de vuelta con las columnas que
    encuentra ahí. El
    actor es 'panel' —no 'lucy'— porque no fue Lucy, igual que en
    `marcar_tarea_hecha`, `poner_categoria` y `a_la_papelera`.

    El UPDATE y el INSERT van en el MISMO bloque de conexión, o sea en la misma
    transacción: o entran los dos o no entra ninguno.
    """
    if chat_id is not None and not puede_ser_responsable(chat_id):
        # Ni se abre conexión. Un chat que no puede entrar al panel no puede
        # quedar con una tarea: sería dejarle un pendiente donde nunca lo va a
        # ver, y el panel no tendría con qué nombrarlo.
        return False

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, titulo, estado, vence_en, responsable_chat_id, "
            "bandeja_id FROM tareas WHERE id = %s AND borrado_en IS NULL",
            (tarea_id,))
        antes = await cur.fetchone()
        if antes is None or antes.get("responsable_chat_id") == chat_id:
            # No existe, está en la papelera, o ya decía eso mismo.
            return False
        await conn.execute(
            "UPDATE tareas SET responsable_chat_id = %s WHERE id = %s",
            (chat_id, tarea_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'editar', 'tareas', %s, %s, %s,
                    'responsable cambiado desde el panel de tareas', %s)
            """,
            (tarea_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"responsable_chat_id": chat_id}, ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


def cuenta_como_posposicion(estado_antes, vence_antes,
                            estado_despues, vence_despues) -> bool:
    """¿Este cambio de fecha es «posponer»? LA definición, para los dos caminos.

    Posponer es mover para MÁS TARDE una tarea que estaba pendiente y sigue
    pendiente. El resumen de la mañana usa la cuenta (`tareas.pospuesta_veces`)
    para nombrar las tareas que se están quedando.

    LA LLAMAN LOS DOS ESCRITORES DE LA FECHA: `acciones.crud.editar` (el chat) y
    `mover_vence` (el panel). Hasta el 13-sep-2026 el criterio vivía escrito
    dentro de `crud.editar`, y el panel no contaba. Tiziano decidió que mover
    para más tarde desde el panel SÍ cuenta, igual que por el chat. Si cada
    camino tuviera su copia del criterio, las dos copias se separarían. Por eso
    hay una sola función, y una prueba la cambia y exige que los dos caminos
    reaccionen (`tests/test_fechas_del_panel.py`).

    NO CUENTA:
      · poner fecha a una tarea que no tenía (no había fecha que posponer);
      · quitarle la fecha (no es «más tarde», es «sin fecha»);
      · adelantarla;
      · una tarea que no estaba pendiente, o que deja de estarlo en el mismo
        cambio.

    Si la fecha nueva no se puede comparar con la vieja (una con zona horaria
    y otra sin zona da TypeError), no cuenta. Es la misma decisión que ya tenía
    `crud.editar`: perder la edición entera por no poder contar una posposición
    sería desproporcionado.
    """
    if estado_antes != ESTADO_PENDIENTE or estado_despues != ESTADO_PENDIENTE:
        return False
    if vence_antes is None or not isinstance(vence_despues, datetime):
        return False
    try:
        return vence_despues > vence_antes
    except TypeError:
        return False


async def mover_vence(tarea_id: int, vence_en: datetime | None) -> bool:
    """Cambia (o quita) la fecha de UNA tarea pendiente desde el panel.

    Devuelve si de verdad cambió algo. `vence_en=None` QUITA la fecha: la tarea
    pasa al grupo «Sin fecha». Tiziano decidió que eso se puede hacer desde el
    panel.

    EL AVISO POR TELEGRAM NO SE VUELVE A ARMAR, por decisión de Tiziano: igual
    que cuando la fecha se mueve por el chat. Por eso este UPDATE NO toca
    `avisos_enviados` ni `anticipos_min`. Eso quiere decir dos cosas, y las dos
    son iguales a lo que ya hace el chat:
      · una tarea que YA avisó guarda esa campanada, y en la fecha nueva no
        vuelve a sonar;
      · una tarea que todavía NO había avisado sigue teniendo su campanada
        pendiente, y suena a la hora nueva.

    MOVER PARA MÁS TARDE CUENTA COMO POSPONER, también por decisión de Tiziano.
    El criterio no se escribe acá: se le pregunta a `cuenta_como_posposicion`,
    la misma función que usa el chat.

    SOLO PENDIENTES. Una tarea hecha, descartada o en cualquier otro estado no
    se mueve: devuelve False sin escribir. La pantalla solo ofrece el campo en
    las pendientes, pero la regla vive acá para que un envío hecho a mano
    tampoco la salte.

    LA HUELLA DICE 'panel', no 'lucy', igual que `marcar_tarea_hecha` y
    `asignar_responsable`: no fue Lucy. `acciones.crud._registrar` firma 'lucy'
    y por eso no se usa acá.

    `antes` guarda SOLO las columnas que este cambio escribe (más el id y el
    bandeja_id, que `deshacer` no toca). La rama 'editar' de
    `acciones.crud.deshacer` devuelve todas las columnas que encuentra en
    `antes`. Con la fila entera, deshacer un cambio de fecha podía devolverle
    también el estado o el título que la tarea tenía en ese momento, pisando
    lo que alguien cambió después.

    LO QUE YA DECÍA ESO NO SE REESCRIBE: devuelve False sin huella. Una huella
    de una edición que no pasó es basura permanente en la tabla que ES el
    deshacer. El UPDATE y el INSERT van en el mismo bloque de conexión, o sea
    en la misma transacción.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, estado, vence_en, pospuesta_veces, bandeja_id "
            "FROM tareas WHERE id = %s AND borrado_en IS NULL", (tarea_id,))
        fila = await cur.fetchone()
        if fila is None or fila.get("estado") != ESTADO_PENDIENTE:
            # No existe, está en la papelera, o no está pendiente.
            return False
        if fila.get("vence_en") == vence_en:
            return False

        veces = fila.get("pospuesta_veces") or 0
        if cuenta_como_posposicion(fila.get("estado"), fila.get("vence_en"),
                                   ESTADO_PENDIENTE, vence_en):
            veces += 1

        await conn.execute(
            "UPDATE tareas SET vence_en = %s, pospuesta_veces = %s "
            "WHERE id = %s",
            (vence_en, veces, tarea_id))
        antes = {"id": fila["id"], "vence_en": fila.get("vence_en"),
                 "pospuesta_veces": fila.get("pospuesta_veces") or 0,
                 "bandeja_id": fila.get("bandeja_id")}
        despues = {"vence_en": vence_en, "pospuesta_veces": veces}
        motivo = ("fecha movida desde el panel de tareas" if vence_en is not None
                  else "fecha quitada desde el panel de tareas")
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'editar', 'tareas', %s, %s, %s, %s, %s)
            """,
            (tarea_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps(despues, default=str, ensure_ascii=False), motivo,
             fila.get("bandeja_id")))
        return True


# ═══════════════════════════════════════════════════════════════════════════
# LOS COMENTARIOS DE UNA TAREA (13-sep-2026)
#
# Decisiones de Tiziano: se guardan APARTE (tabla `comentarios_tarea`), cada uno
# con quién y cuándo, y nadie —ni Lucy— pisa el de otro; comentan los dos que
# entran al panel; Lucy los lee; cualquiera de los dos puede borrar cualquiera.
#
# QUIÉN ESCRIBE O BORRA NO SE DECIDE ACÁ: se le pregunta a
# `web.auth.puede_entrar`, la misma puerta que usa el panel para dejar entrar.
# Está en la escritura además de en la ruta porque la validación de un
# formulario protege al formulario, no a la tabla (la misma razón que en
# `asignar_responsable`). Se importa dentro de cada función porque `web.app`
# importa este módulo al arrancar.
#
# EL TEXTO DE UN COMENTARIO: qué está comprobado y qué no.
#   · `acciones.crud` —los escritores genéricos que usa Lucy— no puede escribir
#     esta tabla porque no está en `crud.TABLAS`. Se comprueba CORRIENDO
#     `crud.editar`, `crud.borrar` y `crud.deshacer` contra ella.
#   · En este archivo la única escritura que actualiza la tabla es
#     `borrar_comentario`, y solo llena `borrado_en` y `borrado_por_chat_id`.
#   · LA BASE NO LO IMPIDE, y el código de mañana tampoco está vigilado de
#     verdad. La prueba que barre los .py solo ve esto, dicho en una línea:
#     DENTRO DE UNA FUNCIÓN, sus textos literales traen seguido
#     «update comentarios_tarea set columna = … where» (en mayúsculas o no),
#     con el nombre pelado de la tabla justo después del verbo. Lo escrito de
#     otra manera no lo ve, y eso incluye SQL de todos los días: la tabla en una
#     variable (como arma sus sentencias `acciones.crud.deshacer`), el verbo
#     partido, una constante de módulo o de clase, un alias, `public.` o
#     `only` delante del nombre, el nombre entre comillas dobles, sin `where`.
#     Esas formas están medidas como escapes en
#     `test_hasta_donde_ve_el_barrido_del_texto`, y la lista no es completa.
#   · Lo que lo cerraría sin depender de cómo se escriba el código: que la base
#     lo rechace, con un disparador que antes de cada actualización falle si el
#     texto nuevo es distinto del viejo. No se escribió: va en una migración de
#     producción y donde se hizo este cambio no hubo un Postgres en el que
#     probarlo.
# Todo lo de arriba lo comprueba `tests/test_comentarios_de_tareas.py`.
# ═══════════════════════════════════════════════════════════════════════════

async def comentarios_de_tarea(tarea_id: int) -> list[dict]:
    """Los comentarios vivos de una tarea, del más viejo al más nuevo."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, tarea_id, autor_chat_id, creado_en, texto "
            "FROM comentarios_tarea "
            "WHERE tarea_id = %s AND borrado_en IS NULL "
            "ORDER BY creado_en ASC, id ASC", (tarea_id,))
        filas = list(await cur.fetchall())
    # El NOMBRE de quien escribió, para pintarlo (`tarea_detalle.html`): sale de
    # `config.nombre_de_autor`, que también conoce a Natalia (autora reservada,
    # 18-oct-2026). None = sin nombre conocido.
    for f in filas:
        f["autor"] = _config.nombre_de_autor(f["autor_chat_id"])
    return filas


async def tarea_con_comentarios(tarea_id: int) -> dict | None:
    """La tarea y sus comentarios, para la pantalla de una tarea. None si la
    tarea no existe o está en la papelera.

    `proyecto_id`/`proyecto_nombre` y `area` (encargo 5) viajan igual que en
    `tareas_por_grupo`: el nombre del proyecto para pintarlo, y el área ya
    resuelta con `COALESCE(p.area, t.area)` -- si la tarea tiene proyecto, la
    hereda; si no, es la suya. `proyecto_id is None` es también la condición
    que usa `/tareas/{id}` para decidir si ofrece el botón «convertir en
    proyecto»: solo tiene sentido sobre una tarea que todavía no es parte de
    ningún proyecto.

    `primero_id`/`primero_titulo`/`primero_estado` (encargo 6) son para el
    `<select>` de «Primero:» de esta pantalla y para que, si la tarea espera
    a otra, se lo diga. SI LA COLUMNA `primero_id` TODAVÍA NO EXISTE
    (SQLSTATE 42703), esto cae -- con un SAVEPOINT, mismo patrón que
    `tareas_por_grupo` -- a la consulta de ANTES de este encargo: la pantalla
    de la tarea sigue funcionando, solo que sin el selector de «Primero:».
    """
    con_primero = """
            SELECT t.id, t.titulo, t.detalle, t.estado, t.vence_en,
                   t.responsable_chat_id, t.proyecto_id, p.nombre AS proyecto_nombre,
                   COALESCE(p.area, t.area) AS area,
                   t.primero_id, ant.titulo AS primero_titulo,
                   ant.estado AS primero_estado
              FROM tareas t
              LEFT JOIN proyectos p ON p.id = t.proyecto_id
              LEFT JOIN tareas ant ON ant.id = t.primero_id
                                   AND ant.borrado_en IS NULL
             WHERE t.id = %s AND t.borrado_en IS NULL
            """
    sin_primero = """
            SELECT t.id, t.titulo, t.detalle, t.estado, t.vence_en,
                   t.responsable_chat_id, t.proyecto_id, p.nombre AS proyecto_nombre,
                   COALESCE(p.area, t.area) AS area
              FROM tareas t
              LEFT JOIN proyectos p ON p.id = t.proyecto_id
             WHERE t.id = %s AND t.borrado_en IS NULL
            """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            async with conn.transaction():
                await cur.execute(con_primero, (tarea_id,))
                tarea = await cur.fetchone()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sin_primero, (tarea_id,))
            tarea = await cur.fetchone()
    if tarea is None:
        return None
    tarea.setdefault("primero_id", None)
    tarea.setdefault("primero_titulo", None)
    tarea.setdefault("primero_estado", None)
    tarea["primero_esperando"] = (
        tarea.get("primero_id") is not None
        and tarea.get("primero_estado") == ESTADO_PENDIENTE)
    # «TOMADA» (26-sep-2026, §D, parte 3): mismo patrón que `derivaciones`/
    # `tomadas_de` en `tareas_por_grupo` -- una consulta aparte, tolerante a
    # la columna ausente, en vez de un cuarto nivel de cascada acá.
    tarea["tomada_en"] = (await tomadas_de([tarea_id])).get(tarea_id)
    tarea["grave"] = (tarea_id in await graves_de([tarea_id])
                      and tarea.get("estado") == ESTADO_PENDIENTE)
    # LOS MICRO-PASOS (encargo 7): `pasos_de_tarea` ya tolera la tabla
    # ausente por su cuenta (devuelve `[]`), así que no hace falta otro
    # SAVEPOINT acá -- es una consulta APARTE, no un tercer JOIN.
    return {"tarea": tarea, "comentarios": await comentarios_de_tarea(tarea_id),
            "pasos": await pasos_de_tarea(tarea_id)}


async def tareas_para_elegir_primero(excluir_id: int) -> list[dict]:
    """Las tareas vivas que se le pueden ofrecer como «Primero:» a
    `excluir_id`, para el `<select>` de la pantalla de una tarea (encargo 6).

    VIVAS Y NO ELLA MISMA -- `id <> excluir_id` es la MISMA comprobación de
    autorreferencia que hace `_primero_que_vale`, acá para no ofrecer en el
    desplegable una opción que la puerta va a rechazar al guardar. Van
    TODOS los estados, no solo 'pendiente': una tarea que ya está hecha
    también puede ser la «Primero:» de otra (esperar a algo que ya se hizo
    es un caso raro pero no un error, y desde el panel se puede elegir tal
    cual como quedó).

    NO SE FILTRAN LOS CÍRCULOS ACÁ. Ofrecer solo las que "seguro no formarían
    un círculo" exigiría recorrer la cadena de cada candidata en cada
    pintado de esta pantalla -- el mismo cálculo que `acciones/crud.py::
    _primero_que_vale` ya hace UNA vez, al guardar (no en la base: ver su
    docstring para el porqué). Elegir una que formaría un círculo se
    rechaza ahí, con su mensaje; duplicar el cálculo acá sería la misma
    pregunta respondida dos veces por dos caminos que se pueden separar.

    NO NECESITA TOLERAR `primero_id` AUSENTE: a diferencia de
    `tareas_por_grupo`, esta consulta no lee esa columna -- solo `id`,
    `titulo` y `estado`, que existen desde siempre. Si la migración de este
    encargo no se aplicó todavía, el `<select>` se puede llenar igual; lo
    que fallaría es GUARDAR una elección, y eso ya lo cubre `editar()` con
    su propio mensaje (ver el docstring de `_primero_que_vale`).
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, titulo, estado FROM tareas "
            " WHERE borrado_en IS NULL AND id <> %s "
            " ORDER BY titulo", (excluir_id,))
        return list(await cur.fetchall())


# «¿ES ESTA UNA TAREA DE CODE?», para los comentarios de la sala (1-oct-2026).
# UNA SOLA PUERTA: leer y escribir comentarios pasan por `tarea_de_code`, y
# `tests/test_api_code_comentarios.py` corre los CINCO caminos de la sala
# (listar -- por sus dos ramas, con y sin `tomada_en` --, tomar, cerrar, leer
# comentarios y comentar) contra la misma base y exige que digan
# lo mismo de cada tarea. `tomar`/`cerrar`/`tareas_de_code_pendientes` siguen
# con su propio `WHERE` (las pruebas de la parte 1 y 3 extraen su texto del
# árbol de sintaxis): lo que las ata a esta puerta es esa prueba, no el
# texto compartido. Comentar NO mira el estado (una tarea hecha se puede
# comentar: el cierre se cuenta después de cerrar); tomar y cerrar sí, además
# de esta condición.
async def tarea_de_code(cur, tarea_id: int) -> dict | None:
    """La tarea `tarea_id` si es de Code: viva, con `responsable_chat_id =
    CHAT_ID_CODE` y área EFECTIVA `AREA_TECNICA` (`COALESCE(t.area, p.area)`,
    la misma que usan `tomar_tarea_de_la_sala` y `cerrar_tarea_de_la_sala`).
    Si no, None. Solo lee."""
    await cur.execute(
        "SELECT t.id, t.bandeja_id FROM tareas t "
        "LEFT JOIN proyectos p ON p.id = t.proyecto_id "
        "WHERE t.id = %s AND t.borrado_en IS NULL "
        "AND t.responsable_chat_id = %s "
        "AND COALESCE(t.area, p.area) = %s",
        (tarea_id, CHAT_ID_CODE, AREA_TECNICA))
    return await cur.fetchone()


async def comentarios_de_tarea_de_code(tarea_id: int) -> list[dict] | None:
    """Los comentarios vivos de una tarea DE CODE, del más viejo al más nuevo,
    con `editado_en`. None si la tarea no es de Code (no existe, está en la
    papelera, otro responsable u otra área): de esas no se lee nada."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if await tarea_de_code(cur, tarea_id) is None:
            return None
        await cur.execute(
            "SELECT id, autor_chat_id, creado_en, texto, editado_en "
            "FROM comentarios_tarea "
            "WHERE tarea_id = %s AND borrado_en IS NULL "
            "ORDER BY creado_en ASC, id ASC", (tarea_id,))
        return list(await cur.fetchall())


async def comentar_tarea(tarea_id: int, autor_chat_id: int,
                         texto: str, *, conexion=None) -> int | None:
    """Guarda UN comentario. Devuelve su id, o None si no se guardó.

    `autor_chat_id` es el chat de la SESIÓN del panel, el mismo que ya se
    comprobó para dejar entrar. Nunca sale de un campo del formulario: un
    formulario que preguntara «quién sos» aceptaría la respuesta que le den.

    No se guarda si quien escribe no puede entrar al panel, si el texto está
    vacío, o si la tarea no existe o está en la papelera.

    UNA EXCEPCIÓN, la sala (1-oct-2026): `CHAT_ID_CODE` no entra al panel, pero
    puede comentar -- SOLO una tarea de Code (`tarea_de_code`) -- y su huella
    es de actor 'sala', como la de tomar y cerrar. La regla vive acá, no en la
    ruta: cualquiera que llame con ese autor pasa por ella.

    OTRA EXCEPCIÓN, Natalia (L5, 18-oct-2026): `CHAT_ID_NATALIA` solo comenta
    CON `conexion`, o sea dentro de la transacción de quien la llama (el «Sí»
    del responsable a una consulta suya, `contestar_consulta_de_natalia`, que
    escribe la tarea, este comentario y el cambio de estado de la consulta
    como UNA sola cosa). Sin `conexion` Natalia no comenta (`None`): no hay una
    ruta, ni un botón, ni un llamado suelto desde el que pueda. Su huella es de
    actor 'natalia'.

    Deja su huella en log_acciones con actor 'panel', como toda escritura del
    panel. La fila y la huella van en la misma transacción.
    """
    from web.auth import puede_entrar

    limpio = (texto or "").strip()
    de_la_sala = autor_chat_id == CHAT_ID_CODE
    de_natalia = _es_de_natalia(autor_chat_id) and conexion is not None
    if not limpio or not (de_la_sala or de_natalia or puede_entrar(autor_chat_id)):
        return None
    async with _conexion_de(conexion) as conn:
        cur = conn.cursor(row_factory=dict_row)
        if de_la_sala:
            tarea = await tarea_de_code(cur, tarea_id)
        else:
            await cur.execute(
                "SELECT id, bandeja_id FROM tareas "
                "WHERE id = %s AND borrado_en IS NULL", (tarea_id,))
            tarea = await cur.fetchone()
        if tarea is None:
            return None
        await cur.execute(
            """
            INSERT INTO comentarios_tarea (tarea_id, autor_chat_id, texto)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (tarea_id, autor_chat_id, limpio))
        fila = await cur.fetchone()
        huella = json.dumps(fila, default=str, ensure_ascii=False)
        if de_la_sala:
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('sala', 'crear', 'comentarios_tarea', %s, NULL, %s,
                        'comentario escrito por la sala de control (Code)', %s)
                """,
                (fila["id"], huella, tarea.get("bandeja_id")))
        elif de_natalia:
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('natalia', 'crear', 'comentarios_tarea', %s, NULL, %s,
                        'comentario de Natalia al anotar la tarea que confirmó el responsable', %s)
                """,
                (fila["id"], huella, tarea.get("bandeja_id")))
        else:
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('panel', 'crear', 'comentarios_tarea', %s, NULL, %s,
                        'comentario escrito desde el panel de tareas', %s)
                """,
                (fila["id"], huella, tarea.get("bandeja_id")))
        return fila["id"]


async def borrar_comentario(comentario_id: int, tarea_id: int,
                            chat_id: int) -> bool:
    """Borra UN comentario (lo marca). Devuelve si de verdad cambió algo.

    CUALQUIERA DE LOS DOS PUEDE BORRAR CUALQUIER COMENTARIO, por decisión de
    Tiziano: no se compara `chat_id` con el autor. Sí se exige que `chat_id`
    pueda entrar al panel, y se guarda quién lo borró.

    El texto NO se toca: se llenan `borrado_en` y `borrado_por_chat_id`, y el
    comentario deja de verse. El comentario tiene que ser de ESA tarea: con otro
    `tarea_id`, no se borra nada.
    """
    from web.auth import puede_entrar

    if not puede_entrar(chat_id):
        return False
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, tarea_id, autor_chat_id, creado_en, texto "
            "FROM comentarios_tarea "
            "WHERE id = %s AND tarea_id = %s AND borrado_en IS NULL",
            (comentario_id, tarea_id))
        antes = await cur.fetchone()
        if antes is None:
            return False
        await conn.execute(
            "UPDATE comentarios_tarea "
            "SET borrado_en = now(), borrado_por_chat_id = %s "
            "WHERE id = %s AND borrado_en IS NULL",
            (chat_id, comentario_id))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'borrar', 'comentarios_tarea', %s, %s, %s,
                    'comentario borrado desde el panel de tareas')
            """,
            (comentario_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"borrado_por_chat_id": chat_id})))
        return True


# El largo máximo de un comentario. No es una regla de negocio: `texto` es TEXT
# y no tiene tope, así que sin esto un POST hecho a mano puede guardar megabytes
# que después hay que pintar y mandarle al modelo. Vive acá (y no en el panel)
# para que lo decida UNA vez quien escribe: `comentar_tarea` desde la ruta y
# `editar_comentario`.
LARGO_COMENTARIO = 2000


class ComentarioNoVale(ValueError):
    """El texto de un comentario que no puede quedar: vacío o demasiado largo."""


def texto_de_comentario(crudo) -> str | None:
    """El cuadro de texto → el comentario a guardar, o None si no vale.

    Los saltos de línea del navegador (\\r\\n) se guardan como \\n: así el mismo
    comentario es el mismo texto, lo haya escrito quien lo haya escrito. Y
    Lucy lo reconoce por su texto exacto (`cerebro.consultar`).
    """
    if not isinstance(crudo, str):
        return None
    limpio = crudo.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not limpio or len(limpio) > LARGO_COMENTARIO:
        return None
    return limpio


async def editar_comentario(comentario_id: int, tarea_id: int, chat_id: int,
                            texto: str) -> bool:
    """Cambia el texto de UN comentario y lo marca editado. Devuelve si de
    verdad cambió algo. (Lucy 1.0, E6; G8.)

    P6, APROBADA por Tiziano el 1-oct-2026: CUALQUIERA DE LOS DOS (Tiziano o
    Rosi) PUEDE EDITAR CUALQUIER COMENTARIO, igual que ya pasa al borrarlo: no se
    compara `chat_id` con el autor. Sí se exige que `chat_id` pueda entrar al
    panel (como `borrar_comentario`), y el texto pasa por `texto_de_comentario`:
    vacío o de más de `LARGO_COMENTARIO` lanza `ComentarioNoVale` SIN escribir.
    El autor y la fecha de creación no cambian; se llena `editado_en`.

    EL COMENTARIO TIENE QUE SER DE ESA TAREA (G8): el `WHERE` lleva `tarea_id`,
    y la tarea tiene que estar viva. Con el id de un comentario de OTRA tarea,
    de uno borrado o de una tarea en la papelera, no se escribe nada (False).
    Si el texto nuevo es igual al que ya tenía, tampoco (False, sin huella).

    Deja su huella `editar` de actor 'panel' con la fila de ANTES y la de
    DESPUÉS, en la misma transacción que el UPDATE. `comentarios_tarea` no está
    en `crud.TABLAS` (nadie —ni Lucy— edita el comentario de otro por el chat):
    esta es la única puerta.
    """
    from web.auth import puede_entrar

    nuevo = texto_de_comentario(texto)
    if nuevo is None:
        raise ComentarioNoVale(
            f"el comentario no puede quedar vacío ni pasar de {LARGO_COMENTARIO} caracteres")
    if not puede_entrar(chat_id):
        return False
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT c.id, c.tarea_id, c.autor_chat_id, c.creado_en, c.texto, "
            "c.editado_en FROM comentarios_tarea c JOIN tareas t ON t.id = c.tarea_id "
            "WHERE c.id = %s AND c.tarea_id = %s AND c.borrado_en IS NULL "
            "AND t.borrado_en IS NULL", (comentario_id, tarea_id))
        antes = await cur.fetchone()
        if antes is None or antes["texto"] == nuevo:
            return False
        await cur.execute(
            "UPDATE comentarios_tarea SET texto = %s, editado_en = now() "
            "WHERE id = %s AND tarea_id = %s AND borrado_en IS NULL "
            "RETURNING id, tarea_id, autor_chat_id, creado_en, texto, editado_en",
            (nuevo, comentario_id, tarea_id))
        despues = await cur.fetchone()
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'comentarios_tarea', %s, %s, %s,
                    'comentario editado desde el panel de proyectos')
            """,
            (comentario_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps(despues, default=str, ensure_ascii=False)))
        return True


# ═══════════════════════════════════════════════════════════════════════════
# LOS MICRO-PASOS (encargo 7, 22-sep-2026)
#
# «No, es una lista de chequeo» (Tiziano): sin fecha, sin responsable, sin
# aviso propio. Crear y borrar un paso individual ya lo sirve `acciones.
# crud` GRATIS -- `crear_pasos` (bulk) y `crud.editar`/`crud.borrar`, porque
# `micro_pasos` está en `crud.TABLAS`. Lo que sigue acá es SOLO lectura
# (`pasos_de_tarea`, `conteo_pasos`) y la única escritura que no encaja en
# el molde genérico de `editar()` -- `mover_paso`, que cambia DOS filas a
# la vez (intercambia el `orden` con el vecino) y por eso dos huellas, no
# una, igual que `db.convertir_tarea_en_proyecto`.
#
# QUÉ PASA CON LOS PASOS DE UNA TAREA SEGÚN LO QUE LE PASE A ELLA: está
# dicho, completo, al lado de `CREATE TABLE micro_pasos` en db/schema.sql
# -- no se repite acá para que las dos explicaciones no se separen.
# ═══════════════════════════════════════════════════════════════════════════

async def pasos_de_tarea(tarea_id: int) -> list[dict]:
    """Los micro-pasos vivos de una tarea, en su orden. `[{id, texto, hecho,
    orden}, ...]`.

    SI LA TABLA TODAVÍA NO EXISTE (la migración de este encargo no se
    aplicó: SQLSTATE 42P01, undefined_table), devuelve `[]` en vez de
    reventar -- mismo patrón que `db.areas()` para el mismo caso. La página
    de la tarea sigue funcionando, solo que sin lista de chequeo que
    mostrar.
    """
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    "SELECT id, texto, hecho, orden FROM micro_pasos "
                    " WHERE tarea_id = %s AND borrado_en IS NULL "
                    " ORDER BY orden, id", (tarea_id,))
                return list(await cur.fetchall())
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42P01":
                return []
            raise


async def pertenece_paso(tarea_id: int, paso_id: int) -> bool:
    """¿Ese micro-paso es de VERDAD de esa tarea, y sigue vivo? (encargo 7,
    arreglo tras el NO PASA del testigo sobre `bebf6c9`.)

    LA MISMA PIEZA para las dos rutas del panel que tocan un paso por su id
    -- `web/app.py::marcar_paso` y `quitar_paso` -- y las dos la llaman
    ANTES de escribir, no después. Hasta este arreglo, `quitar_paso` no
    comprobaba nada (`crud.borrar("micro_pasos", pid, ...)` sin mirar
    `tid`: se podía borrar el paso de OTRA tarea con solo adivinar su id en
    la URL) y `marcar_paso` sí comprobaba, pero DESPUÉS de haber escrito
    (`despues.get("tarea_id") != tid`, una vez que el UPDATE ya había
    corrido) -- la escritura quedaba hecha igual, aunque la respuesta dijera
    error.

    Vivo: `borrado_en IS NULL`, igual que `pasos_de_tarea`. Un paso ya
    quitado no "pertenece" para estos efectos -- no hay nada que marcar ni
    que volver a quitar.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT 1 FROM micro_pasos "
            " WHERE id = %s AND tarea_id = %s AND borrado_en IS NULL",
            (paso_id, tarea_id))
        return await cur.fetchone() is not None


async def conteo_pasos(tarea_ids: list[int]) -> dict[int, dict]:
    """`{tarea_id: {"hechos": N, "total": M}}` para las tareas de la lista,
    SOLO las que de verdad tienen al menos un paso vivo -- una tarea sin
    pasos no aparece en el dict, y el llamador la trata como "sin pasos"
    (ver `tareas_por_grupo`, que hace exactamente eso con `.get`).

    Es la consulta detrás del «2 de 5» de la lista principal. Se pide en
    LOTE -- un solo `= ANY(%s)`, no una consulta por fila -- por lo mismo
    que ya evita `tareas_por_grupo` con `bandeja`: una fila por tarea
    visible sería una consulta más por cada tarea del panel.

    `tarea_ids` vacía devuelve `{}` sin tocar la base: no hay nada que
    contar, y `= ANY('{}')` sobre una lista vacía es una consulta de más.

    SI LA TABLA TODAVÍA NO EXISTE, devuelve `{}` -- mismo patrón que
    `pasos_de_tarea`: sin pasos que contar, el panel se pinta igual que
    antes de este encargo.
    """
    if not tarea_ids:
        return {}
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    """
                    SELECT tarea_id,
                           count(*) AS total,
                           count(*) FILTER (WHERE hecho) AS hechos
                      FROM micro_pasos
                     WHERE borrado_en IS NULL AND tarea_id = ANY(%s)
                     GROUP BY tarea_id
                    """,
                    (list(tarea_ids),))
                filas = await cur.fetchall()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate == "42P01":
                return {}
            raise
    return {f["tarea_id"]: {"hechos": f["hechos"], "total": f["total"]}
            for f in filas}


async def mover_paso(tarea_id: int, paso_id: int, direccion: str) -> bool:
    """Sube o baja UN micro-paso, intercambiando su `orden` con el del
    vecino vivo más cercano en esa dirección. Devuelve si de verdad cambió
    algo.

    `direccion` es `'arriba'` o `'abajo'`, literal -- la pantalla no ofrece
    otra cosa. Cualquier otro valor no mueve nada (`False`), sin reventar:
    un botón mal formado no tiene por qué tumbar la página.

    NO ES UN `editar()` GENÉRICO porque toca DOS filas a la vez -- el paso
    y su vecino -- y `editar()` está pensado para UNA. Mismo patrón que
    `db.convertir_tarea_en_proyecto`: dos huellas, no una, en la MISMA
    transacción, cada una nombrando a la otra en el motivo.

    `actor='panel'`: este botón solo existe en la pantalla de la tarea, no
    hay forma de moverlo por Telegram (el encargo no lo pidió -- «divide X
    en pasos» y «ya hice el paso 2» son las dos únicas frases que Lucy
    entiende).
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, orden FROM micro_pasos "
            " WHERE id = %s AND tarea_id = %s AND borrado_en IS NULL",
            (paso_id, tarea_id))
        actual = await cur.fetchone()
        if actual is None:
            return False

        if direccion == "arriba":
            await cur.execute(
                "SELECT id, orden FROM micro_pasos "
                " WHERE tarea_id = %s AND borrado_en IS NULL AND orden < %s "
                " ORDER BY orden DESC, id DESC LIMIT 1",
                (tarea_id, actual["orden"]))
        elif direccion == "abajo":
            await cur.execute(
                "SELECT id, orden FROM micro_pasos "
                " WHERE tarea_id = %s AND borrado_en IS NULL AND orden > %s "
                " ORDER BY orden ASC, id ASC LIMIT 1",
                (tarea_id, actual["orden"]))
        else:
            return False
        vecino = await cur.fetchone()
        if vecino is None:
            return False  # ya está en la punta: no hay con quién cambiar

        await conn.execute(
            "UPDATE micro_pasos SET orden = %s WHERE id = %s",
            (vecino["orden"], actual["id"]))
        await conn.execute(
            "UPDATE micro_pasos SET orden = %s WHERE id = %s",
            (actual["orden"], vecino["id"]))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'micro_pasos', %s, %s, %s, %s)
            """,
            (actual["id"],
             json.dumps({"orden": actual["orden"]}, ensure_ascii=False),
             json.dumps({"orden": vecino["orden"]}, ensure_ascii=False),
             f"movido {direccion}, intercambiado con el paso #{vecino['id']}"))
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo)
            VALUES ('panel', 'editar', 'micro_pasos', %s, %s, %s, %s)
            """,
            (vecino["id"],
             json.dumps({"orden": vecino["orden"]}, ensure_ascii=False),
             json.dumps({"orden": actual["orden"]}, ensure_ascii=False),
             f"intercambiado con el paso #{actual['id']} al moverlo "
             f"{direccion}"))
        return True


# Los minutos-antes de una tarea escrita desde el panel: NINGUNO.
#
# El formulario pide un DÍA, no una hora. `anticipos_min` con su default de la
# tabla ('{0}') haría que `cerebro/despertador.py:242` mandara un Telegram al
# instante que guardemos en `vence_en` — un instante que nadie eligió, porque
# la persona escribió una fecha y no una hora. Avisar a una hora inventada es
# inventarle a Tiziano una decisión que no tomó.
#
# No es un caso nuevo: las citas que entran de Google Calendar ya entran mudas
# por el mismo mecanismo (`cerebro/despertador.py`, cabecera), y la consulta que
# reparte campanadas exige `cardinality(anticipos_min) > 0`, así que una lista
# vacía no avisa nunca. La tarea SÍ aparece en el panel, en su grupo, que es
# donde se la fue a buscar.
#
# Poner una hora de aviso es una decisión de Tiziano y este código no la toma.
SIN_ANTICIPOS: list[int] = []

# QUIÉN ESCRIBE UNA TAREA POR `crear_tarea_desde_el_panel` (`desde`): el panel (lo de siempre) o Natalia, cuando el
# responsable le dice «sí» a una consulta suya (L5 del diseño «Natalia y los Proyectos», 18-oct-2026). Es el origen
# de la fila de `bandeja` Y el actor de la huella: no se pueden separar (una tarea de Natalia con huella del panel
# mentiría quién la puso). Cerrado a propósito: otro valor es `ValueError`.
DESDE_DEL_PANEL = "panel"
DESDE_DE_NATALIA = "natalia"
ESCRITORES_DE_TAREA = (DESDE_DEL_PANEL, DESDE_DE_NATALIA)


def _es_de_natalia(chat_id) -> bool:
    """¿Este chat es el de Natalia COMO AUTORA (el valor reservado `config.CHAT_ID_NATALIA`)? Es la única pregunta que
    las escrituras de L5 le hacen a ese valor. No dice quién entra al panel ni quién puede ser responsable: Natalia no es
    ni una cosa ni la otra."""
    return chat_id == CHAT_ID_NATALIA


@contextlib.asynccontextmanager
async def _conexion_de(conexion):
    """La conexión en la que se escribe: la que le pasan (una transacción que NO es de esta función, ella ni la abre
    ni la cierra ni hace commit) o, sin ninguna, una del pool (el bloque ES la transacción)."""
    if conexion is not None:
        yield conexion
        return
    async with pool.connection() as propia:
        yield propia


async def crear_tarea_desde_el_panel(chat_id: int, titulo: str,
                                     vence_en: datetime | None,
                                     area: str | None = None,
                                     responsable_chat_id: int | None = None,
                                     *, proyecto_id: int | None = None,
                                     primero_id: int | None = None,
                                     detalle: str | None = None,
                                     persona_id: int | None = None,
                                     conexion=None,
                                     desde: str = DESDE_DEL_PANEL,
                                     motivo: str | None = None) -> int:
    """Una tarea escrita a mano en el panel. La cuarta escritura del panel.

    LA TAREA QUE NATALIA ANOTA (L5, 18-oct-2026): `desde='natalia'`, `conexion` y `motivo`. La escribe el botón «Sí»
    del responsable (`contestar_consulta_de_natalia`) DENTRO de la transacción que también cambia la consulta, por
    eso recibe la `conexion` de quien la llama (la usa, no hace commit: lo hace quien la abrió; el área con
    SAVEPOINT de más abajo sigue siendo un savepoint dentro de ella). Con `desde='natalia'` la fila de `bandeja`
    lleva `origen='natalia'` y la huella `actor='natalia'` con ese `motivo`, y `chat_id` TIENE que ser
    `CHAT_ID_NATALIA` (una fila de Natalia con el chat de una persona de la casa mentiría quién la puso). Lo demás
    (las validaciones, `anticipos_min` vacío, la fila muda) es lo mismo que para el panel.

    QUIÉN LA ANOTÓ NO SE PUEDE MENTIR, y por eso esta función escribe DOS filas
    y no una. La fila de `bandeja` se crea de verdad, con el chat de la SESIÓN
    —el mismo dato que ya se comprobó para dejar entrar, no un nombre escrito a
    mano en ninguna parte— y la tarea cuelga de ella. Una tarea con `bandeja_id`
    NULO no deja constancia de nadie, y el encargo que creó esta función pedía
    que la dejara.

    OJO CON LO QUE ESTO YA NO ES, desde el 10-sep-2026: el panel tenía una
    columna «Quién la anotó» que salía de `tareas.bandeja_id → bandeja.chat_id`,
    y Tiziano la sacó —«nno es relevante quien la anoto»—. Lo que se ve hoy en
    esa columna es el RESPONSABLE (`tareas.responsable_chat_id`), que es otra
    pregunta: quién la tiene pendiente, no quién la escribió. Una tarea recién
    escrita a mano nace SIN responsable salvo que quien la escribe elija uno en
    el formulario (`responsable_chat_id`, tarea 146 de Tiziano, 28-sep-2026).

    O sea que lo que esta fila de `bandeja` sostiene ya no es una columna de la
    pantalla: es la trazabilidad de la fila —de dónde salió— y el `bandeja_id`
    que viaja en cada huella de `log_acciones`. Sigue haciendo falta; solo dejó
    de mirarse desde el panel.

    NO ES UNA COLUMNA NUEVA NI UNA MIGRACIÓN. `bandeja` es la columna vertebral
    del proyecto y ya recibe filas que no son mensajes de Telegram:
    `registrar_aviso` mete las del despertador con `origen='despertador'`. Ésta
    es la misma idea con `origen='panel'`. Cero DDL de esta función: la única
    columna que este panel tuvo que agregarle a `tareas` es
    `responsable_chat_id`, que esta función escribe solo si se le pasa uno.

    EL RESPONSABLE (tarea 146): `responsable_chat_id=None` es lo normal y no
    pasa por ninguna puerta. Si viene un valor, se revalida ACÁ con
    `config.puede_ser_responsable` --la MISMA puerta que ya pasó la ruta--, antes
    de abrir la conexión y lanzando `ValueError` (mismo trato que
    `cerrar_y_derivar`): la validación de un formulario protege al formulario,
    no a la tabla, y `tests/test_responsable.py` censa que todo sitio que
    inserte la columna nombre la puerta.

    Y SI EL RESPONSABLE ES CODE, EL ÁREA SE PONE SOLA EN `AREA_TECNICA`, sin mirar
    la que se haya pedido (decisión de Tiziano, 28-sep-2026): la API de la sala
    solo ve lo que es de Code Y del área técnica (`tareas_de_code_pendientes`),
    así que una tarea de Code con otra área nacería invisible para ella. Vive
    acá, en el escritor, y no en la ruta: cualquier camino que llegue a esta
    función recibe la misma regla. Quien llama tiene que haber comprobado que
    esa área existe en `areas()` (la ruta lo hace), porque la FK lo exige.

    LA FILA DE BANDEJA VA MUDA, y eso es deliberado. `contenido_raw`,
    `transcripcion` y `respuesta_lucy` quedan NULOS, así que las dos consultas
    que arman la memoria del agente —`ultimos_intercambios` acá y
    `cerebro/memoria.py:78`— la descartan por su propio filtro
    (`coalesce(transcripcion, contenido_raw) IS NOT NULL OR respuesta_lucy IS
    NOT NULL`). Guardar el título como si Tiziano lo hubiera dicho por Telegram
    metería en la conversación una frase que nadie dijo: el sistema recordaría
    una voz que no existió. La fila dice lo único que de verdad pasó —de dónde
    vino y de quién— y nada más.

    Y va con `estado='procesado'` para que `tomar_pendientes` no la levante:
    no hay nada que interpretar, la tarea ya está creada.

    EL ÁREA (encargo 4) es el único campo nuevo, y esta pantalla NUNCA elige
    proyecto —`/tareas/nueva` no tiene ese selector (ver su docstring: «SOLO
    DOS CAMPOS», ahora tres)—, así que `area` siempre puede grabarse sin
    chocar con `tareas_area_no_con_proyecto`: esa tarea nace con
    `proyecto_id IS NULL` siempre, y la restricción de la base ya lo permite
    sin que este código tenga que comprobarlo dos veces. Quien llama decide
    si `area` es `None` («sin área») o una de las claves de `areas`; esta
    función no valida el valor -- lo mismo que `crear_tarea_desde_el_panel`
    no valida `titulo` contra ningún vocabulario: la ruta HTTP
    (`web/app.py::crear_tarea`) ya lo hizo, comparando contra `db.areas()` en
    código —el mismo patrón de vocabulario cerrado que ya usan las
    categorías de gastos (`acciones/crud.py::editar`, comparando contra
    `CATEGORIAS`), NO la FK—. La FK `tareas.area REFERENCES areas(clave)`
    sigue ahí, pero como red de más atrás: por Telegram, la misma
    comprobación la hace `acciones/crud.py::_area_que_vale` (hallazgo del
    testigo sobre `e94b37a`: antes de eso, el camino de `editar()` no
    validaba nada y llegaba derecho a la FK/CHECK, con el texto crudo de
    psycopg).

    SIN GUARDA DE DUPLICADOS, al revés que el alta por Telegram
    (`acciones/crud.py:_duplicado_pendiente`). Los motivos, por orden de peso:

      · Aquella guarda existe porque EL AGENTE re-crea lo que acaba de crear
        —lo dice su propio docstring—. Una persona escribiendo en un formulario
        sabe lo que está escribiendo; no es el mismo problema.
      · Su coincidencia es `titulo = %s AND vence_en IS NOT DISTINCT FROM %s`, y
        acá la fecha es OPCIONAL: dos tareas sin fecha con el mismo título
        chocarían SIEMPRE. «Llamar al banco» de la semana pasada se comería a
        «Llamar al banco» de hoy.
      · Y peor que no crearla: aquella función devuelve el id de la vieja, así
        que el panel diría «creada» sobre algo que no creó. Un mensaje de éxito
        que no corresponde a una escritura es la familia de fallo callado que
        este panel existe para combatir.
      · Es la misma decisión, con la misma forma, que ya tomó
        `crear_gasto_en_efectivo`: sin guarda, porque el 303 del endpoint ya
        tapa el duplicado por refrescar.

    La fila y su huella en log_acciones se escriben en el MISMO bloque de
    conexión, o sea en la misma transacción, igual que `crear_gasto_en_efectivo`
    y `a_la_papelera`: o entran las tres o no entra ninguna. Una tarea sin su
    línea de log es una tarea que no se puede deshacer, y el deshacer de este
    proyecto ES log_acciones.

    La acción se registra como 'crear' porque es lo que `deshacer()` sabe
    revertir: su rama de 'crear' hace `SET borrado_en = now()`.

    DENTRO DE UN PROYECTO (pieza 2 del diseño «proyectos», 29-sep-2026):
    `proyecto_id`, `primero_id`, `detalle` y `persona_id` (de quién trata) son
    opcionales y `None` es lo normal. Cada uno se revalida ACÁ, antes de abrir
    ninguna escritura, aunque la ruta ya lo haya mirado (la validación de un
    formulario protege al formulario, no a la tabla): el proyecto por
    `proyecto_admite_tareas` (existe, no está en la papelera, no está cerrado),
    la persona por `_persona_viva` y el «Primero:» con que exista y no esté en
    la papelera (el chequeo de círculos vive en `crud._primero_que_vale`, que la
    ruta llama; al CREAR no hay círculo posible). Con proyecto, `area` se pone
    en `None` SIEMPRE, sin mirar lo pedido: el CHECK `tareas_area_no_con_proyecto`
    hace irrepresentable «proyecto + área», y el área de la tarea sale del
    proyecto. Por lo mismo la regla «Code → área técnica» de arriba NO aplica
    con proyecto: quien llama dice, con `sala_ve`, si la sala no la va a ver.
    """
    if desde not in ESCRITORES_DE_TAREA:
        raise ValueError("una tarea la escribe el panel o Natalia, no otro")
    if (desde == DESDE_DE_NATALIA) != _es_de_natalia(chat_id):
        raise ValueError("el chat de la fila tiene que ser el de quien la escribe")
    if desde == DESDE_DE_NATALIA and conexion is None:
        # Sin la transacción del «Sí» esto abriría la suya y haría commit: una tarea de Natalia sin que la consulta cambie.
        raise ValueError("la tarea de Natalia se escribe dentro de la transacción de su consulta")
    if responsable_chat_id is not None:
        if not puede_ser_responsable(responsable_chat_id):
            raise TareaNoValida(
                "responsable", "ese chat no puede ser responsable de una tarea")
        if responsable_chat_id == CHAT_ID_CODE and proyecto_id is None:
            area = AREA_TECNICA
    if proyecto_id is not None:
        area = None

    async with _conexion_de(conexion) as conn:
        cur = conn.cursor(row_factory=dict_row)
        if proyecto_id is not None:
            await proyecto_admite_tareas(cur, proyecto_id)
        if persona_id is not None and not await _persona_viva(cur, persona_id):
            raise TareaNoValida("persona", "esa persona no existe o está archivada")
        if primero_id is not None:
            await cur.execute(
                "SELECT id FROM tareas WHERE id = %s AND borrado_en IS NULL",
                (primero_id,))
            if await cur.fetchone() is None:
                raise TareaNoValida(
                    "primero", "esa tarea de «Primero:» no existe o está archivada")
        if desde == DESDE_DE_NATALIA:
            await cur.execute(
                """
                INSERT INTO bandeja
                  (origen, tipo_entrada, chat_id, estado, procesado_en)
                VALUES ('natalia', 'natalia', %s, 'procesado', now())
                RETURNING id
                """,
                (chat_id,))
        else:
            await cur.execute(
                """
                INSERT INTO bandeja
                  (origen, tipo_entrada, chat_id, estado, procesado_en)
                VALUES ('panel', 'panel', %s, 'procesado', now())
                RETURNING id
                """,
                (chat_id,))
        bandeja_id = (await cur.fetchone())["id"]

        # EL ÁREA TOLERA LA MIGRACIÓN SIN APLICAR, igual que el lado de lectura
        # (`tareas_por_grupo`, `areas()`): si `tareas.area` todavía no existe
        # (SQLSTATE 42703), la tarea se crea igual, sin área -- perder el área
        # pedida es mejor que no crear la tarea. El SAVEPOINT
        # (`conn.transaction()` anidado) es necesario porque `bandeja` YA se
        # insertó arriba, en la MISMA conexión: sin savepoint, un error acá
        # abortaría toda la transacción y la fila de bandeja se perdería con
        # ella al reintentar.
        con_area = """
            INSERT INTO tareas
              (bandeja_id, titulo, vence_en, anticipos_min, area,
               responsable_chat_id, proyecto_id, primero_id, detalle,
               persona_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """
        sin_area = """
            INSERT INTO tareas
              (bandeja_id, titulo, vence_en, anticipos_min,
               responsable_chat_id, proyecto_id, primero_id, detalle,
               persona_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """
        try:
            async with conn.transaction():
                await cur.execute(
                    con_area, (bandeja_id, titulo, vence_en, SIN_ANTICIPOS, area,
                               responsable_chat_id, proyecto_id, primero_id,
                               detalle, persona_id))
                # RETURNING * y no una fila reconstruida a mano: `despues` tiene
                # que ser lo que de verdad quedó guardado —con el id, el
                # creado_en y los defaults que puso Postgres—, no lo que
                # creíamos estar mandando.
                fila = await cur.fetchone()
        except Exception as e:
            try:
                sqlstate = e.sqlstate
            except AttributeError:
                raise e from None
            if sqlstate != "42703":
                raise
            await cur.execute(
                sin_area, (bandeja_id, titulo, vence_en, SIN_ANTICIPOS,
                           responsable_chat_id, proyecto_id, primero_id,
                           detalle, persona_id))
            fila = await cur.fetchone()

        if desde == DESDE_DE_NATALIA:
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('natalia', 'crear', 'tareas', %s, NULL, %s, %s, %s)
                """,
                (fila["id"],
                 json.dumps(fila, default=str, ensure_ascii=False),
                 motivo or "tarea anotada por Natalia", bandeja_id))
        else:
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('panel', 'crear', 'tareas', %s, NULL, %s,
                        'tarea escrita a mano desde el panel de tareas', %s)
                """,
                (fila["id"],
                 json.dumps(fila, default=str, ensure_ascii=False), bandeja_id))
        return fila["id"]


async def cerrar_y_derivar(
    chat_id: int, madre_id: int, derivadas: list[dict], *,
    exige_hecha: bool = False,
) -> tuple[bool, list[int]] | None:
    """Cierra UNA tarea y, en la MISMA transacción, crea las que salen de
    ella al marcarla hecha desde el panel. Devuelve `(se_cerro, [ids de las
    hijas])`, o `None` si la madre no existe o está en la papelera -- ahí no
    hay de dónde derivar nada.

    Pedido de Tiziano, textual: «cuando yo seleccione el recuadro para
    marcar como hecha una tarea aparezca un cuadro donde pueda crear una
    tarea nueva, porque muchas veces una tarea hecha da como resultado una
    tarea nueva que se deriva de la completada». Diseño aprobado en
    disenos/lucy-tarea-derivada/DISENO.md.

    `derivadas` es una lista (Tiziano: pueden ser VARIAS por cada tarea que
    se cierra), cada una `{"titulo": str, "vence_en": datetime | None,
    "area": str | None, "responsable_chat_id": int | None}`. El título, la
    fecha y el área YA LLEGAN VALIDADOS por quien llama
    (`web/app.py::guardar_tareas`, con las mismas piezas que
    `crear_tarea_desde_el_panel`: título 1-200, `_vence_con_hora_valido`,
    área contra `db.areas()`) y esta función no los vuelve a mirar, mismo
    motivo por el que `crear_tarea_desde_el_panel` no revalida `titulo`.

    EL RESPONSABLE ES LA EXCEPCIÓN, Y SE REVALIDA ACÁ CONTRA
    `config.puede_ser_responsable` -- LA MISMA PUERTA que ya pasa
    `_responsable_pedido` en la ruta, pero repetida a propósito: es la regla
    de `tests/test_responsable.py::
    test_toda_escritura_de_la_columna_pasa_por_la_misma_puerta`, que censa
    TODO sitio del repo cuyo SQL nombra `responsable_chat_id` y exige que
    ESE MISMO sitio nombre la puerta -- no que algo, en algún lugar antes en
    la cadena de llamadas, ya la haya nombrado. La razón de fondo es la
    misma que la de `asignar_responsable`: la validación de un formulario
    protege al formulario, no a la tabla, y un tercer camino que aparezca
    mañana (otra pantalla, un script) que llame a `cerrar_y_derivar` sin
    pasar por `guardar_tareas` se encuentra la puerta acá también, no un
    agujero.

    UNA SOLA TRANSACCIÓN: el bloque de conexión ES la transacción (mismo
    patrón que `crear_tarea_desde_el_panel` y `a_la_papelera`) -- cerrar la
    madre y crear las hijas son "un solo gesto" (Tiziano: "escribir la nueva
    tarea y luego darle a guardar"), y tienen que ser un solo gesto en la
    base: o entran todas, o ninguna. Si algo revienta a mitad de la lista
    de `derivadas`, la excepción sube y NADA de esta llamada queda escrito
    -- ni la madre cerrada ni las hijas que sí habían entrado antes del
    error, porque no hay commit hasta que el bloque `async with` termina
    limpio.

    SI LA MADRE YA ESTABA HECHA (otra persona la cerró antes de que este
    envío llegara), NO SE REESCRIBE -- mismo criterio que `marcar_tarea_
    hecha`, con el mismo motivo: una huella de una edición que no pasó es
    basura permanente en la tabla que ES el deshacer. Pero las hijas SÍ se
    crean igual (decisión D7 del diseño): la vieja ya está como él quería,
    y tirar lo que escribió no gana nada.

    CADA HIJA SIGUE EL PROYECTO DE LA MADRE (decisión aprobada por Tiziano,
    P3 del diseño: "si la hecha es de un proyecto, la nueva va al mismo
    proyecto"). Si la madre tiene `proyecto_id`, la hija nace con el MISMO
    `proyecto_id` y su `area` se pone en `None` sin mirar lo que traiga
    `derivadas[i]["area"]` -- no porque se desconfíe de quien llama, sino
    porque así el caso "proyecto + área propia" queda IRREPRESENTABLE acá
    también, la misma garantía que ya impone `tareas_area_no_con_proyecto`
    sobre cualquier otra escritura de esta tabla.

    `deriva_de_id` TOLERA LA MIGRACIÓN SIN APLICAR, mismo patrón que el área
    en `crear_tarea_desde_el_panel`: si la columna no existe todavía
    (SQLSTATE 42703, undefined_column), la hija se crea IGUAL, sin el
    enlace -- perder el enlace es mejor que perder la tarea que alguien
    acaba de escribir. Cada INSERT va en su propio SAVEPOINT
    (`conn.transaction()` anidado) para que el error de una hija no aborte
    las que ya entraron antes en la misma transacción.

    `exige_hecha=True` (14-oct-2026, «la tarea que sigue» de la página de
    Proyectos, que se ofrece DESPUÉS de marcar hecha): la madre TIENE que estar
    hecha en la base ahora mismo. Si no lo está (alguien la reabrió entre el
    clic y el envío) no se escribe NADA -- ni se cierra la madre otra vez ni
    se crea la hija -- y se devuelve `(False, [])`. Sin esto, esta puerta
    cerraría una tarea que la persona ya había visto reabrir.

    QUIÉN LAS ANOTÓ es `chat_id` -- la sesión de quien guardó el formulario
    (`web/app.py::guardar_tareas`), no la madre ni su `bandeja_id`: es la
    misma persona escribiendo un renglón más, igual que
    `crear_tarea_desde_el_panel`.

    SIN GUARDA DE DUPLICADOS, mismos motivos que `crear_tarea_desde_el_panel`
    (decisión D10 del diseño): es una persona escribiendo a mano, no el
    agente repitiéndose.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, titulo, estado, vence_en, completado_en, "
            "bandeja_id, proyecto_id FROM tareas "
            "WHERE id = %s AND borrado_en IS NULL",
            (madre_id,))
        madre = await cur.fetchone()
        if madre is None:
            # No existe, o está en la papelera: no hay de dónde derivar.
            return None

        if exige_hecha and madre.get("estado") != ESTADO_HECHA:
            return False, []

        # UN PROYECTO CERRADO NO RECIBE TAREAS, tampoco las derivadas (pieza 2
        # del diseño «proyectos»): por la MISMA puerta que el alta, y ANTES de
        # cerrar la madre: «si la nueva no vale, la vieja tampoco se cierra»
        # (D6). Sin derivadas, cerrar una tarea de un proyecto cerrado sigue
        # siendo posible.
        if derivadas and madre.get("proyecto_id") is not None:
            await proyecto_admite_tareas(cur, madre["proyecto_id"])

        cerrada = False
        if madre.get("estado") != ESTADO_HECHA:
            await conn.execute(
                "UPDATE tareas SET estado = %s, completado_en = now() "
                "WHERE id = %s", (ESTADO_HECHA, madre_id))
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('panel', 'editar', 'tareas', %s, %s, %s,
                        'marcada hecha desde el panel de tareas', %s)
                """,
                (madre_id,
                 json.dumps(madre, default=str, ensure_ascii=False),
                 json.dumps({"estado": ESTADO_HECHA}, ensure_ascii=False),
                 madre.get("bandeja_id")))
            cerrada = True

        proyecto_id = madre.get("proyecto_id")

        con_deriva = """
            INSERT INTO tareas
              (bandeja_id, titulo, vence_en, anticipos_min, area,
               proyecto_id, responsable_chat_id, deriva_de_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """
        sin_deriva = """
            INSERT INTO tareas
              (bandeja_id, titulo, vence_en, anticipos_min, area,
               proyecto_id, responsable_chat_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """

        hijas: list[int] = []
        for d in derivadas:
            resp = d.get("responsable_chat_id")
            if resp is not None and not puede_ser_responsable(resp):
                # No debería pasar nunca -- la ruta ya lo filtró con la
                # misma puerta -- pero si pasa, no se escribe NADA de este
                # renglón ni de los que vengan después: la excepción sube y
                # aborta la transacción entera (ver el docstring: "o entran
                # todas, o ninguna").
                raise ValueError(
                    f"responsable inválido en una tarea derivada: {resp}")
            area = None if proyecto_id is not None else d.get("area")

            await cur.execute(
                """
                INSERT INTO bandeja
                  (origen, tipo_entrada, chat_id, estado, procesado_en)
                VALUES ('panel', 'panel', %s, 'procesado', now())
                RETURNING id
                """,
                (chat_id,))
            bandeja_id = (await cur.fetchone())["id"]

            try:
                async with conn.transaction():
                    await cur.execute(
                        con_deriva,
                        (bandeja_id, d["titulo"], d.get("vence_en"),
                         SIN_ANTICIPOS, area, proyecto_id,
                         d.get("responsable_chat_id"), madre_id))
                    fila = await cur.fetchone()
            except Exception as e:
                try:
                    sqlstate = e.sqlstate
                except AttributeError:
                    raise e from None
                if sqlstate != "42703":
                    raise
                log.warning(
                    "cerrar_y_derivar: falta tareas.deriva_de_id (falta la "
                    "migracion) -- se creo la tarea derivada de #%s SIN el "
                    "enlace", madre_id)
                await cur.execute(
                    sin_deriva,
                    (bandeja_id, d["titulo"], d.get("vence_en"),
                     SIN_ANTICIPOS, area, proyecto_id,
                     d.get("responsable_chat_id")))
                fila = await cur.fetchone()

            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('panel', 'crear', 'tareas', %s, NULL, %s,
                        'tarea derivada desde el panel de tareas', %s)
                """,
                (fila["id"],
                 json.dumps(fila, default=str, ensure_ascii=False),
                 bandeja_id))
            hijas.append(fila["id"])

    return cerrada, hijas
