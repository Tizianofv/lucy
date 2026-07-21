"""Cliente único de Gemini — el ÚNICO lugar del código que habla con la IA.

Centralizado a propósito: si mañana cambiamos de proveedor o de modelo, se
toca solo este archivo. Texto, audio y visión pasan todos por acá.

Estado: andamiaje. Se implementa al empezar el Nivel 2.
"""
import json
from datetime import datetime

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, TZ

# google-generativeai (el SDK que usaba este archivo) fue reemplazado por
# google-genai. La forma de llamar cambió: ahora se instancia un cliente.
cliente = genai.Client(api_key=GEMINI_API_KEY)

# Modelo fijado a propósito, no un alias tipo "flash-latest": queremos que el
# comportamiento de Lucy cambie cuando NOSOTROS lo decidamos, no cuando Google
# mueva el alias por debajo. El precio de fijarlo es que algún día lo jubilan
# —a gemini-1.5-flash, que estaba acá antes, ya lo jubilaron— y por eso existe
# verificar_modelo(): que ese día falle al arrancar y no en silencio.
# gemini-3.5-flash quedó descartado por cuota, no por calidad: su capa gratuita
# son 20 peticiones por día, verificado a los golpes (429 repetidos con uso de
# prueba). Un asistente que deja de entender al mensaje 21 no es un asistente.
# La cuota es POR MODELO, así que bajar de versión da un presupuesto nuevo.
MODELO = "gemini-2.5-flash"


async def verificar_modelo() -> None:
    """Confirma al arrancar que la key sirve y el modelo existe.

    Misma filosofía que pool.open(wait=True): más vale reventar en el deploy
    que descubrir tres semanas después que Lucy dejó de entender lo que le
    mandan. Un modelo jubilado es la falla silenciosa perfecta.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY vacía: Lucy no podría interpretar nada.")
    await cliente.aio.models.get(model=MODELO)


# Esquema de salida forzado. No le pedimos a Gemini que "devuelva JSON" y
# después parseamos con los dedos cruzados: el modelo está obligado por la API
# a responder con esta forma exacta. Sin esto, un día contesta con ```json
# alrededor y otro día con una explicación amable, y el parser se rompe.
ESQUEMA = {
    "type": "OBJECT",
    "properties": {
        "clasificacion": {
            "type": "STRING",
            "enum": ["tarea", "cita", "nota", "idea", "gasto", "pregunta"],
        },
        "titulo": {"type": "STRING"},
        "detalle": {"type": "STRING"},
        "cuando": {"type": "STRING"},        # ISO 8601 con offset, o "" si no aplica
        "duracion_min": {"type": "INTEGER"},
        "lugar": {"type": "STRING"},
        "persona": {"type": "STRING"},
        "proyecto": {"type": "STRING"},
        "monto": {"type": "NUMBER"},
        "moneda": {"type": "STRING"},
        # Req 9: lo que Lucy dedujo por su cuenta y te tiene que confesar.
        "supuestos": {"type": "ARRAY", "items": {"type": "STRING"}},
        # Req 9: datos críticos que faltan y que sí ameritan preguntarte.
        "falta": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["clasificacion", "titulo", "supuestos", "falta"],
}

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

INSTRUCCIONES = """\
Sos el motor de comprensión de Lucy, la asistente personal de Tiziano.
Recibís un mensaje suyo, en español rioplatense/dominicano y muchas veces
informal o abreviado. Tu trabajo es entenderlo, no responderle.

FECHAS — lo más importante:
· Ahora es {ahora} (zona {zona}, UTC-4, sin horario de verano).
· Resolvé SIEMPRE las fechas relativas a ese momento y devolvé ISO 8601 con
  offset. "mañana a las 10" → fecha concreta. "el jueves que viene no, el otro"
  → contá dos jueves. Si no hay ninguna referencia temporal, dejá "cuando" vacío.
· Nunca inventes una hora exacta si el mensaje no la sugiere: es preferible
  dejar la fecha sin hora y anotarlo en "falta".

CLASIFICACIÓN:
· tarea → algo que Tiziano tiene que hacer.
· cita → algo que ocurre en un momento dado, con o sin otras personas.
· gasto → hay plata gastada. Moneda por defecto DOP (peso dominicano).
· nota → información para guardar, sin acción.
· idea → algo que se le ocurrió y quiere no perder.
· pregunta → le está preguntando algo a Lucy, no pidiéndole guardar algo.

SUPUESTOS Y FALTANTES (importa tanto como el resto):
· "supuestos": todo lo que dedujiste sin que te lo dijeran, en primera persona
  y en lenguaje natural. Ej: "asumí que era esta semana", "asumí pesos".
· "falta": solo datos CRÍTICOS ausentes que ameriten molestarlo con una
  pregunta. Si podés deducirlo razonablemente, deducilo y ponelo en supuestos
  en vez de en falta. Molestar de más es peor que asumir de más.

"titulo" va corto y en infinitivo si es tarea ("Llamar a Ana").
Los campos que no apliquen, dejalos vacíos o en 0.\
"""


def _json_de(respuesta) -> dict:
    """Extrae el JSON de la respuesta tomando SOLO las partes de texto.

    gemini-3.5-flash razona antes de contestar, así que la respuesta trae
    además partes de "pensamiento". El atajo `respuesta.text` las mezcla y
    emite un warning en cada llamada — ruido constante en el log, que es
    justo lo que nos entrena a ignorar los errores de verdad.
    """
    partes = respuesta.candidates[0].content.parts
    crudo = "".join(p.text for p in partes if getattr(p, "text", None))
    return json.loads(crudo)


async def interpretar_texto(texto: str) -> dict:
    """Clasifica y extrae estructura de un texto. Devuelve el dict del esquema.

    El "ahora" se inyecta en cada llamada, no se cachea: si el proceso lleva
    días levantado, una fecha anclada al arranque haría que "mañana" apunte a
    un día que ya pasó.
    """
    ahora = datetime.now(TZ)
    # El día de la semana se arma a mano y no con strftime("%A"): strftime
    # depende del locale del sistema, y el contenedor de Railway corre en
    # inglés. Sin esto, Lucy leería "Monday" adentro de un prompt en español.
    ahora_txt = f"{DIAS[ahora.weekday()]} {ahora.isoformat(timespec='minutes')}"

    respuesta = await cliente.aio.models.generate_content(
        model=MODELO,
        contents=texto,
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCCIONES.format(ahora=ahora_txt, zona=TZ.key),
            response_mime_type="application/json",
            response_schema=ESQUEMA,
            temperature=0,  # extracción, no creatividad: queremos reproducibilidad
        ),
    )
    return _json_de(respuesta)


async def transcribir_audio(ruta_archivo: str) -> str:
    """(Nivel 2) Nota de voz → texto."""
    raise NotImplementedError("Se implementa en Nivel 2.")


async def leer_imagen(ruta_archivo: str) -> dict:
    """(Nivel 2) Foto de ticket/tarjeta → datos estructurados."""
    raise NotImplementedError("Se implementa en Nivel 2.")
