"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
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

    No interpreta nada: solo captura. La comprensión (Gemini) viene después,
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
    tipos: tuple[str, ...] = ("texto",), limite: int = 5
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
                      telegram_msg_id, intentos
            """,
            (list(tipos), limite),
        )
        return await cur.fetchall()


async def guardar_interpretacion(
    bandeja_id: int, clasificacion: str, interpretacion: dict
) -> None:
    """Guarda lo que Gemini entendió. La fila queda esperando confirmación.

    No crea todavía la tarea/evento/gasto: eso es un paso aparte y deliberado.
    Primero que Tiziano vea qué entendió Lucy; recién después se escribe.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE bandeja
               SET clasificacion  = %s,
                   interpretacion = %s,
                   estado         = 'esperando_confirmacion',
                   procesado_en   = now(),
                   error_detalle  = NULL
             WHERE id = %s
            """,
            (clasificacion, json.dumps(interpretacion), bandeja_id),
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
