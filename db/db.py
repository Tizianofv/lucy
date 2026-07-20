"""Capa de acceso a Postgres: pool de conexiones + escritura en la bandeja.

Regla de oro del Nivel 1: guardar_en_bandeja() es lo primero que corre con
cada mensaje, ANTES de tocar la IA. Si todo lo demás falla, el mensaje ya está
a salvo aquí.
"""
import hashlib
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
