"""Cliente único de Gemini — el ÚNICO lugar del código que habla con la IA.

Centralizado a propósito: si mañana cambiamos de proveedor o de modelo, se
toca solo este archivo. Texto, audio y visión pasan todos por acá.

Estado: andamiaje. Se implementa al empezar el Nivel 2.
"""
import google.generativeai as genai

from config import GEMINI_API_KEY

genai.configure(api_key=GEMINI_API_KEY)

MODELO = "gemini-1.5-flash"  # texto + audio + visión, capa gratuita generosa


async def interpretar_texto(texto: str) -> dict:
    """(Nivel 2) Clasifica y extrae estructura de un texto. Devuelve JSON."""
    raise NotImplementedError("Se implementa en Nivel 2.")


async def transcribir_audio(ruta_archivo: str) -> str:
    """(Nivel 1) Nota de voz → texto."""
    raise NotImplementedError("Se implementa al agregar audios.")


async def leer_imagen(ruta_archivo: str) -> dict:
    """(Nivel 1) Foto de ticket/tarjeta → datos estructurados."""
    raise NotImplementedError("Se implementa al agregar fotos.")
