"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone

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
from config import (CHAT_ID_CODE, CHAT_ID_DUENO, DATABASE_URL, TZ,
                    puede_ser_responsable)

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


async def _buscar_o_crear(tabla: str, nombre: str, *,
                          bandeja_id: int | None = None) -> int | None:
    """Devuelve el id de la persona/proyecto con ese nombre; la crea si no está.

    Sin esto, "Ana", "ana" y "Ana García" serían tres personas distintas y la
    consulta "¿cuándo vi a Ana por última vez?" del req 10 devolvería un tercio
    de la verdad. Por eso la búsqueda es insensible a mayúsculas y acentos
    (unaccent no está garantizado, así que comparamos en minúsculas) y mira
    también los alias.

    LA HUELLA, SOLO PARA `proyectos` (encargo 5, hallazgo del propio diseño:
    "cuando Lucy crea una tarea nombrando un proyecto que no existe, lo crea
    SIN DEJAR RASTRO en el registro -- 3 de los 4 proyectos de producción no
    tienen rastro"). Cuando la fila YA existía, no hay nada que registrar: no
    se creó nada. Cuando se crea de verdad, se deja la misma huella que
    cualquier otra creación -- `accion='crear'`, con lo que quedó guardado en
    `despues` -- así que `deshacer()` la revierte gratis, sin código nuevo:
    "proyectos" ya está en `acciones.crud.TABLAS` y la rama 'crear' de
    `deshacer()` es genérica para cualquier tabla ahí.

    `personas` NO lleva huella -- no se pidió, y agregarla sería un segundo
    problema resuelto en el mismo cambio (Regla 15 de la sala: uno a la vez).

    No se puede usar `acciones.crud._registrar` acá: ese vive en `crud.py`, que
    IMPORTA este módulo (`import db.db as db`), así que importarlo de vuelta
    sería un ciclo. Se escribe la fila de `log_acciones` a mano, con la MISMA
    forma que ya usan las demás escrituras del panel en este archivo (ver
    `crear_tarea_desde_el_panel`, `gasto_en_efectivo`), actor `'lucy'` porque
    esto lo dispara SIEMPRE una interpretación de Telegram, nunca el panel.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        return None

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"""
            SELECT id FROM {tabla}
             WHERE borrado_en IS NULL
               AND (lower(nombre) = lower(%s)
                    {"OR lower(%s) = ANY(SELECT lower(a) FROM unnest(alias) a)"
                     if tabla == "personas" else ""})
             LIMIT 1
            """,
            (nombre, nombre) if tabla == "personas" else (nombre,),
        )
        fila = await cur.fetchone()
        if fila:
            return fila["id"]

        await cur.execute(
            f"INSERT INTO {tabla} (nombre) VALUES (%s) RETURNING *", (nombre,)
        )
        nueva = await cur.fetchone()
        if tabla == "proyectos":
            await conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo,
                   bandeja_id)
                VALUES ('lucy', 'crear', 'proyectos', %s, NULL, %s,
                        'Proyecto nuevo, nombrado al crear una tarea', %s)
                """,
                (nueva["id"],
                 json.dumps(nueva, default=str, ensure_ascii=False),
                 bandeja_id))
        return nueva["id"]


async def buscar_o_crear_persona(nombre: str) -> int | None:
    return await _buscar_o_crear("personas", nombre)


async def buscar_o_crear_proyecto(nombre: str, *,
                                  bandeja_id: int | None = None) -> int | None:
    return await _buscar_o_crear("proyectos", nombre, bandeja_id=bandeja_id)


async def convertir_tarea_en_proyecto(tarea_id: int) -> dict:
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
      · TÍTULO Y DETALLE → `proyectos.nombre` y `proyectos.descripcion`. Es
        la traducción obvia: son los mismos dos campos que ya tiene una
        tarea, con otro nombre.
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
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
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

        await cur.execute(
            """
            INSERT INTO proyectos (nombre, descripcion, area)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (tarea["titulo"], tarea.get("detalle"), tarea.get("area")))
        proyecto = await cur.fetchone()

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
AREA_TECNICA = "🛠️ Técnico"

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

    LA CLAVE ES EL NOMBRE QUE SE VE ('CDS', 'ACD', '🛠️ Técnico',
    '🏠 Personal') — no hay traducción por medio, ver
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


async def proyectos_con_tareas() -> list[dict]:
    """Cada proyecto vivo, con su área, su estado y sus tareas en orden
    (encargo 5, requisito 1). `[{id, nombre, descripcion, estado, area,
    color, tareas: [...]}, ...]`.

    LOS PROYECTOS se ordenan por `estado` (activo, pausado, cerrado -- en ese
    orden, el que un humano leería primero) y dentro de cada estado por
    nombre. Ninguna parte del encargo dijo un orden para los proyectos entre
    sí (solo para SUS TAREAS): ésta es una decisión de presentación, técnica,
    no de negocio.

    LAS TAREAS de cada proyecto, en el MISMO criterio que ya usa
    `tareas_por_grupo`: `vence_en ASC NULLS LAST, creado_en ASC`. Van TODAS
    las vivas de ese proyecto -- pendientes y hechas -- porque esta pantalla
    contesta "qué hay en este proyecto", no "qué falta hacer": eso ya lo
    tiene `/tareas`.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        # El ORDER BY va LITERAL, sin armarlo con un f-string: una consulta
        # que un censo (`tests/test_responsable.py::_censo`) no pueda resolver
        # como texto fijo cae en el cubo de "escritor genérico" aunque sea un
        # SELECT puro -- ese censo busca escrituras dinámicas de
        # `responsable_chat_id` en TODO el repo, y una plantilla con `{...}`
        # se ve igual de "no legible" le escriba algo o no.
        await cur.execute(
            """
            SELECT p.id, p.nombre, p.descripcion, p.estado, p.area, a.color
              FROM proyectos p
              LEFT JOIN areas a ON a.clave = p.area
             WHERE p.borrado_en IS NULL
             ORDER BY CASE p.estado WHEN 'activo' THEN 0
                                     WHEN 'pausado' THEN 1 ELSE 2 END,
                      p.nombre
            """)
        proyectos = list(await cur.fetchall())
        if not proyectos:
            return []

        await cur.execute(
            """
            SELECT id, proyecto_id, titulo, estado, vence_en, completado_en
              FROM tareas
             WHERE borrado_en IS NULL AND proyecto_id = ANY(%s)
             ORDER BY vence_en ASC NULLS LAST, creado_en ASC, id ASC
            """,
            ([p["id"] for p in proyectos],))
        tareas = list(await cur.fetchall())

    por_proyecto: dict[int, list] = {p["id"]: [] for p in proyectos}
    for t in tareas:
        por_proyecto[t["proyecto_id"]].append(t)
    for p in proyectos:
        p["tareas"] = por_proyecto[p["id"]]
    return proyectos


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
            (tarea_id, json.dumps(antes, default=str, ensure_ascii=False),
             json.dumps({"estado": ESTADO_HECHA}, ensure_ascii=False),
             antes.get("bandeja_id")))
        return True


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
        return list(await cur.fetchall())


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


async def comentar_tarea(tarea_id: int, autor_chat_id: int,
                         texto: str) -> int | None:
    """Guarda UN comentario. Devuelve su id, o None si no se guardó.

    `autor_chat_id` es el chat de la SESIÓN del panel, el mismo que ya se
    comprobó para dejar entrar. Nunca sale de un campo del formulario: un
    formulario que preguntara «quién sos» aceptaría la respuesta que le den.

    No se guarda si quien escribe no puede entrar al panel, si el texto está
    vacío, o si la tarea no existe o está en la papelera.

    Deja su huella en log_acciones con actor 'panel', como toda escritura del
    panel. La fila y la huella van en la misma transacción.
    """
    from web.auth import puede_entrar

    limpio = (texto or "").strip()
    if not limpio or not puede_entrar(autor_chat_id):
        return None
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
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
        await conn.execute(
            """
            INSERT INTO log_acciones
              (actor, accion, tabla, registro_id, antes, despues, motivo,
               bandeja_id)
            VALUES ('panel', 'crear', 'comentarios_tarea', %s, NULL, %s,
                    'comentario escrito desde el panel de tareas', %s)
            """,
            (fila["id"], json.dumps(fila, default=str, ensure_ascii=False),
             tarea.get("bandeja_id")))
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


async def crear_tarea_desde_el_panel(chat_id: int, titulo: str,
                                     vence_en: datetime | None,
                                     area: str | None = None) -> int:
    """Una tarea escrita a mano en el panel. La cuarta escritura del panel.

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
    escrita a mano nace SIN responsable, como todas.

    O sea que lo que esta fila de `bandeja` sostiene ya no es una columna de la
    pantalla: es la trazabilidad de la fila —de dónde salió— y el `bandeja_id`
    que viaja en cada huella de `log_acciones`. Sigue haciendo falta; solo dejó
    de mirarse desde el panel.

    NO ES UNA COLUMNA NUEVA NI UNA MIGRACIÓN. `bandeja` es la columna vertebral
    del proyecto y ya recibe filas que no son mensajes de Telegram:
    `registrar_aviso` mete las del despertador con `origen='despertador'`. Ésta
    es la misma idea con `origen='panel'`. Cero DDL de esta función: la única
    columna que este panel tuvo que agregarle a `tareas` es
    `responsable_chat_id`, y no la escribe acá — una tarea nace sin responsable.

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
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
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
              (bandeja_id, titulo, vence_en, anticipos_min, area)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """
        sin_area = """
            INSERT INTO tareas
              (bandeja_id, titulo, vence_en, anticipos_min)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """
        try:
            async with conn.transaction():
                await cur.execute(
                    con_area, (bandeja_id, titulo, vence_en, SIN_ANTICIPOS, area))
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
                sin_area, (bandeja_id, titulo, vence_en, SIN_ANTICIPOS))
            fila = await cur.fetchone()

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
    chat_id: int, madre_id: int, derivadas: list[dict],
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
