"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import DATABASE_URL

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
    import os
    import re
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    try:
        with open(ruta, encoding="utf-8") as f:
            declaradas = {t.lower() for t in re.findall(
                r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", f.read(), re.I)}
    except OSError:
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


async def correos_ya_reportados(cuenta: str, uids: list[int]) -> set[int]:
    """De esos uids, cuáles ya se le informaron a Tiziano.

    Es la memoria que hace posible mirar los SIN LEER en vez de un puntero que
    se consume: sin ella, un correo que él no marque leído volvería a aparecer
    cada mañana hasta el fin de los tiempos. Informado una vez, informado.
    """
    if not uids:
        return set()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT uid FROM correo_reportado WHERE cuenta = %s AND uid = ANY(%s)",
            (cuenta, [int(u) for u in uids]),
        )
        return {r[0] for r in await cur.fetchall()}


async def marcar_correo_reportado(cuenta: str, uid: int, *, nivel: str = "",
                                  ambito: str = "", area: str = "",
                                  asunto: str = "", bandeja_id: int | None = None
                                  ) -> None:
    """Deja constancia de que ese correo ya se informó, con su clasificación.

    Guardar CÓMO se clasificó no es adorno: es lo que después permite contestar
    "¿por qué no me avisaste de esto?" con datos en la mano, y afinar las
    reglas con hechos en vez de impresiones.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO correo_reportado
              (cuenta, uid, nivel, ambito, area, asunto, bandeja_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cuenta, uid) DO NOTHING
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


async def registrar_aviso(chat_id: int, texto: str) -> int:
    """Deja constancia en la bandeja de algo que Lucy dijo POR SU CUENTA.

    Los avisos del despertador entran a la conversación como una fila más
    (origen 'despertador', sin dicho, con respuesta_lucy): así la memoria
    corta y la de largo plazo los ven igual que a cualquier otro intercambio.
    Lo que Lucy dice proactivamente también es parte de la historia.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO bandeja
              (origen, tipo_entrada, chat_id, estado, respuesta_lucy, procesado_en)
            VALUES ('despertador', 'aviso', %s, 'procesado', %s, now())
            RETURNING id
            """,
            (chat_id, texto[:4000]),
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


async def _buscar_o_crear(tabla: str, nombre: str) -> int | None:
    """Devuelve el id de la persona/proyecto con ese nombre; la crea si no está.

    Sin esto, "Ana", "ana" y "Ana García" serían tres personas distintas y la
    consulta "¿cuándo vi a Ana por última vez?" del req 10 devolvería un tercio
    de la verdad. Por eso la búsqueda es insensible a mayúsculas y acentos
    (unaccent no está garantizado, así que comparamos en minúsculas) y mira
    también los alias.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        return None

    async with pool.connection() as conn:
        cur = await conn.execute(
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
            return fila[0]

        cur = await conn.execute(
            f"INSERT INTO {tabla} (nombre) VALUES (%s) RETURNING id", (nombre,)
        )
        return (await cur.fetchone())[0]


async def buscar_o_crear_persona(nombre: str) -> int | None:
    return await _buscar_o_crear("personas", nombre)


async def buscar_o_crear_proyecto(nombre: str) -> int | None:
    return await _buscar_o_crear("proyectos", nombre)


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
        cur = await conn.execute(
            """
            INSERT INTO movimientos
              (bandeja_id, tipo, fecha, monto, moneda, contraparte,
               categoria, referencia, hash_contenido, banco)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (hash_contenido) WHERE hash_contenido IS NOT NULL
              DO NOTHING
            RETURNING id
            """,
            (bandeja_id, mov.tipo, mov.fecha.date(), str(mov.monto), mov.moneda,
             mov.contraparte, categoria, mov.referencia, mov.clave_dedupe(),
             mov.banco),
        )
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
# `tipo <> 'transferencia'` y `estado` no aplica (la tabla no lo guarda):
# un traspaso entre cuentas propias no es gasto ni ingreso, y sumarlo fue el
# error que costaba RD$657,400 al año.

async def resumen_por_mes(meses: int = 12) -> list[dict]:
    """Gasto e ingreso por mes y moneda. Los traspasos quedan fuera."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT to_char(fecha, 'YYYY-MM') AS mes, moneda, tipo,
                   sum(monto) AS total, count(*) AS n
              FROM movimientos
             WHERE borrado_en IS NULL
               AND tipo <> 'transferencia'
               AND fecha >= date_trunc('month', now()) - (%s || ' months')::interval
             GROUP BY 1, 2, 3
             ORDER BY 1 DESC, 2, 3
            """, (meses,))
        return await cur.fetchall()


async def gasto_por_categoria(mes: str | None = None) -> list[dict]:
    """Gasto por categoría, con la MONEDA como parte de la agrupación.

    La moneda va en el GROUP BY y no se convierte: sumar DOP con USD da un
    número que no significa nada, y este panel existe justo para no cometer ese
    error. Un total de "175,000" que en realidad son 154,000 pesos más 228
    dólares no es un total, es una confusión con formato de número.

    Los traspasos quedan fuera —no son gasto, son dinero cambiando de bolsillo—
    y los ingresos también: no se clasifican.

    `mes` en formato 'YYYY-MM'; sin él, todo lo que haya.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT coalesce(nullif(categoria, ''), '— sin clasificar —') AS categoria,
                   moneda, sum(monto) AS total, count(*) AS n
              FROM movimientos
             WHERE borrado_en IS NULL AND tipo = 'gasto'
               AND (%s IS NULL OR to_char(fecha, 'YYYY-MM') = %s)
             GROUP BY 1, 2
             ORDER BY 2, 3 DESC
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
                                limite: int = 300) -> list[dict]:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, fecha, tipo, monto, moneda, contraparte, categoria,
                   referencia, banco
              FROM movimientos
             WHERE borrado_en IS NULL
               AND (%s::date IS NULL OR fecha >= %s::date)
               AND (%s::date IS NULL OR fecha <= %s::date)
               AND (%s::text IS NULL OR tipo = %s::text)
               AND (%s::text IS NULL OR categoria = %s::text)
               AND (%s::text IS NULL OR banco = %s::text)
             ORDER BY fecha DESC, id DESC
             LIMIT %s
            """, (desde, desde, hasta, hasta, tipo, tipo, categoria, categoria,
                  banco, banco, limite))
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
    """La única escritura del panel. Pasa por log_acciones como todo lo demás:
    una corrección hecha desde la web tiene que ser tan auditable y tan
    reversible como una hecha por Telegram.

    Y ADEMÁS ENSEÑA. Corregir un movimiento y no aprender del comercio deja el
    trabajo a medias: la próxima compra en el mismo sitio vuelve a caer en la
    cola, y a la tercera vez que uno corrige "SM NACIONAL" deja de corregir. La
    promesa del panel —una corrección vale para siempre— se cumple acá o no se
    cumple en ningún lado.
    """
    from cerebro.bancos.categorias import normalizar_comercio

    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT categoria, contraparte FROM movimientos WHERE id = %s",
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
    if antes and antes.get("contraparte") and categoria:
        await aprender_categoria(normalizar_comercio(antes["contraparte"]),
                                 categoria)
