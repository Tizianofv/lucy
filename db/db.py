"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
from __future__ import annotations

import hashlib
import json

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import DATABASE_URL

# Pool de conexiones reutilizables. Se abre al arrancar el bot (ver main.py).
pool = AsyncConnectionPool(DATABASE_URL, open=False)


async def abrir() -> None:
    # wait=True es deliberado: si la base no responde, queremos reventar ACÁ,
    # al arrancar, y que el deploy falle a los gritos. Sin esto el pool abre
    # "en diferido" y el log canta "Pool abierto" aunque no haya conexión:
    # Lucy estuvo 3 horas respondiendo silencio con cara de que todo iba bien.
    await pool.open(wait=True, timeout=30)


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


async def guardar_ubicacion(lat: float, lon: float, en_vivo: bool) -> None:
    """Cada pin o latido de ubicación en vivo cae acá. Captura pura, sin IA."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ubicaciones (lat, lon, en_vivo) VALUES (%s, %s, %s)",
            (lat, lon, en_vivo),
        )


async def ultima_ubicacion() -> dict | None:
    """La última posición conocida, con su edad en minutos y el lugar con
    nombre en el que cae (si cae en alguno).

    La distancia usa la aproximación equirectangular, que a escala de una
    ciudad erra por centímetros: Haversine acá sería precisión de avión para
    un problema de motoconcho.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT u.lat, u.lon, u.en_vivo,
                   round(extract(epoch FROM (now() - u.ts)) / 60)::int AS hace_min,
                   (SELECT l.nombre FROM lugares l
                     WHERE l.borrado_en IS NULL
                       AND 111320 * sqrt(
                             pow(l.lat - u.lat, 2) +
                             pow((l.lon - u.lon) * cos(radians(u.lat)), 2)
                           ) <= l.radio_m
                     ORDER BY 111320 * sqrt(
                             pow(l.lat - u.lat, 2) +
                             pow((l.lon - u.lon) * cos(radians(u.lat)), 2))
                     LIMIT 1) AS lugar
              FROM ubicaciones u
             ORDER BY u.ts DESC
             LIMIT 1
            """
        )
        return await cur.fetchone()


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
