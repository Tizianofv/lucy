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
               telegram_msg_id, hash_contenido)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, telegram_msg_id) DO UPDATE
              SET contenido_raw = EXCLUDED.contenido_raw
            RETURNING id
            """,
            (tipo_entrada, contenido_raw, archivo_id, chat_id,
             telegram_msg_id, hash_contenido),
        )
        row = await cur.fetchone()
        return row[0]


async def tomar_pendientes(
    tipos: tuple[str, ...] = ("texto", "audio"), limite: int = 5
) -> list[dict]:
    """Reclama filas sin procesar y las marca 'procesando' en un solo paso.

    FOR UPDATE SKIP LOCKED no es adorno: durante cada redespliegue conviven dos
    contenedores unos segundos (lo vemos en los logs como 409 Conflict de
    Telegram). Sin esto, los dos tomarían la misma fila y Lucy interpretaría el
    mismo mensaje dos veces. Con esto, el segundo simplemente saltea lo tomado.

    `tipos` acota a lo que Lucy sabe interpretar hoy. Los audios y fotos se
    quedan en 'sin_procesar' esperando su turno: no se pierden ni se traban.
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
