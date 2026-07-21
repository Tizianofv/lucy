"""Memoria de largo plazo: búsqueda por significado sobre todo lo hablado.

Es el req 13 ("¿qué acordamos con el cliente X en marzo?"). SQL encuentra lo
que se puede nombrar con exactitud; esto encuentra lo que se recuerda a
medias. "Lo del depósito aquel" no matchea ningún LIKE, pero su embedding
queda cerca del comprobante que mandó en julio.

Se indexa la bandeja entera —lo que Tiziano dijo Y lo que Lucy respondió—
porque la conversación completa es el recuerdo, no solo la mitad de él.

Los embeddings son de OpenAI (text-embedding-3-small), la misma credencial de
Whisper y la visión: cero piezas nuevas. A volumen personal cuesta centavos
al año. Y viven en Postgres vía pgvector: la memoria está en el mismo lugar
que los datos, se respalda con ellos, y buscar es un ORDER BY.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI
from psycopg.rows import dict_row

import db.db as db
from config import OPENAI_API_KEY

log = logging.getLogger("lucy.memoria")

# Placeholder por el mismo motivo que en whisper/vision: con la key vacía el
# SDK revienta al construir el cliente y se lleva puesto el import.
cliente = AsyncOpenAI(api_key=OPENAI_API_KEY or "sin-key")

MODELO = "text-embedding-3-small"
DIMENSIONES = 1536

# Cuántas filas se indexan por tanda. El bucle pasa seguido: no hace falta
# tragarse todo de una, y una tanda corta no bloquea la comprensión.
TANDA = 20


def _a_vector(v: list[float]) -> str:
    """pgvector recibe el vector como texto '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


async def vectores(textos: list[str]) -> list[list[float]]:
    """Embeddings en tanda: una llamada para N textos, no N llamadas."""
    r = await cliente.embeddings.create(model=MODELO, input=textos)
    # La API garantiza el orden, pero lo reafirmamos por índice igual:
    # confiar en garantías ajenas gratis está bien, verificarlas cuesta nada.
    ordenado = sorted(r.data, key=lambda d: d.index)
    return [d.embedding for d in ordenado]


async def indexar_pendientes() -> int:
    """Indexa filas ya atendidas que aún no tienen embedding. Devuelve cuántas.

    Corre como rama lateral del bucle: si falla, la comprensión no se entera.
    Solo toma filas en estados finales — indexar algo a medio procesar sería
    fotografiar una conversación por la mitad.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, coalesce(transcripcion, contenido_raw) AS dicho,
                   respuesta_lucy
              FROM bandeja
             WHERE embedding IS NULL
               AND coalesce(transcripcion, contenido_raw) IS NOT NULL
               AND estado IN ('procesado', 'descartado', 'error',
                              'esperando_respuesta', 'esperando_confirmacion')
             ORDER BY id
             LIMIT %s
            """,
            (TANDA,),
        )
        filas = await cur.fetchall()

    if not filas:
        return 0

    textos = [
        f["dicho"] + (f"\nLucy: {f['respuesta_lucy']}" if f["respuesta_lucy"] else "")
        for f in filas
    ]
    vs = await vectores(textos)

    async with db.pool.connection() as conn:
        for f, v in zip(filas, vs):
            await conn.execute(
                "UPDATE bandeja SET embedding = %s::vector WHERE id = %s",
                (_a_vector(v), f["id"]),
            )
    log.info("Indexadas %s filas en la memoria de largo plazo.", len(filas))
    return len(filas)


async def buscar(texto: str, n: int = 5) -> list[dict]:
    """Los n recuerdos más cercanos en significado a `texto`.

    Devuelve fecha, lo dicho, lo respondido y la afinidad (1 = idéntico).
    La distancia es coseno (<=>), que es para lo que está el índice HNSW.
    """
    if not texto.strip():
        return []
    v = _a_vector((await vectores([texto]))[0])

    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id,
                   to_char(creado_en AT TIME ZONE 'America/Santo_Domingo',
                           'DD/MM/YYYY HH24:MI')            AS fecha,
                   tipo_entrada,
                   coalesce(transcripcion, contenido_raw)   AS dicho,
                   respuesta_lucy,
                   round((1 - (embedding <=> %s::vector))::numeric, 3) AS afinidad
              FROM bandeja
             WHERE embedding IS NOT NULL
             ORDER BY embedding <=> %s::vector
             LIMIT %s
            """,
            (v, v, n),
        )
        return await cur.fetchall()
