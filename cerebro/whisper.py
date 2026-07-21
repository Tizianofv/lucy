"""Oído de Lucy: transcripción de notas de voz con Whisper (OpenAI).

Separado del cerebro de texto a propósito. Son dos proveedores distintos y dos
fallas distintas: si Whisper se cae, los textos se siguen entendiendo; si
DeepSeek se cae, las voces se siguen transcribiendo y esperan en la bandeja.
Acoplarlos habría hecho que una caída se llevara puestas las dos capacidades.

El audio NO se guarda en disco ni en la base: viaja en memoria de Telegram a
Whisper y lo que queda guardado es la transcripción. El archivo original vive
en los servidores de Telegram, referenciado por su file_id, que no caduca.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from config import OPENAI_API_KEY

log = logging.getLogger("lucy.whisper")

# Placeholder por el mismo motivo que en deepseek.py: con la key vacía el SDK
# falla al construir el cliente y se lleva puesto el import del módulo entero.
cliente = AsyncOpenAI(api_key=OPENAI_API_KEY or "sin-key")

MODELO = "whisper-1"

# Las notas de voz de Telegram son Ogg/Opus. Whisper detecta el formato por la
# extensión del nombre que le mandamos, así que el nombre no es decorativo.
NOMBRE_ARCHIVO = "nota.ogg"


async def verificar() -> None:
    """Confirma que la key existe y tiene permiso de TRANSCRIBIR.

    Ojo con las keys sk-proj-: pueden estar restringidas por servicio. Una key
    válida para texto-a-voz puede rechazar voz-a-texto, y el error recién
    aparecería con tu primera nota de voz. Acá solo comprobamos que la key
    exista; el permiso real se probó al configurarla.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY vacía: Lucy no podría oír las notas de voz.")


async def transcribir(audio: bytes) -> str:
    """Bytes de audio → texto. Devuelve la transcripción ya limpia."""
    r = await cliente.audio.transcriptions.create(
        model=MODELO,
        file=(NOMBRE_ARCHIVO, audio),
        # Sin este idioma, Whisper a veces "traduce" un audio en español con
        # acento dominicano a un inglés inventado.
        language="es",
    )
    return (r.text or "").strip()
