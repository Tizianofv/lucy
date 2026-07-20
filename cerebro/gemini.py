"""Cliente único de Gemini — el ÚNICO lugar del código que habla con la IA.

Centralizado a propósito: si mañana cambiamos de proveedor o de modelo, se
toca solo este archivo. Texto, audio y visión pasan todos por acá.

Estado: andamiaje. Se implementa al empezar el Nivel 2.
"""
from google import genai

from config import GEMINI_API_KEY

# google-generativeai (el SDK que usaba este archivo) fue reemplazado por
# google-genai. La forma de llamar cambió: ahora se instancia un cliente.
cliente = genai.Client(api_key=GEMINI_API_KEY)

# Modelo fijado a propósito, no un alias tipo "flash-latest": queremos que el
# comportamiento de Lucy cambie cuando NOSOTROS lo decidamos, no cuando Google
# mueva el alias por debajo. El precio de fijarlo es que algún día lo jubilan
# —a gemini-1.5-flash, que estaba acá antes, ya lo jubilaron— y por eso existe
# verificar_modelo(): que ese día falle al arrancar y no en silencio.
MODELO = "gemini-3.5-flash"


async def verificar_modelo() -> None:
    """Confirma al arrancar que la key sirve y el modelo existe.

    Misma filosofía que pool.open(wait=True): más vale reventar en el deploy
    que descubrir tres semanas después que Lucy dejó de entender lo que le
    mandan. Un modelo jubilado es la falla silenciosa perfecta.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY vacía: Lucy no podría interpretar nada.")
    await cliente.aio.models.get(model=MODELO)


async def interpretar_texto(texto: str) -> dict:
    """(Nivel 2) Clasifica y extrae estructura de un texto. Devuelve JSON."""
    raise NotImplementedError("Se implementa en Nivel 2.")


async def transcribir_audio(ruta_archivo: str) -> str:
    """(Nivel 2) Nota de voz → texto."""
    raise NotImplementedError("Se implementa en Nivel 2.")


async def leer_imagen(ruta_archivo: str) -> dict:
    """(Nivel 2) Foto de ticket/tarjeta → datos estructurados."""
    raise NotImplementedError("Se implementa en Nivel 2.")
