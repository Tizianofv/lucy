"""Cerebro de texto: DeepSeek. El ÚNICO lugar que decide qué significa un mensaje.

Reemplazó a Gemini el 2026-07-20. No fue por calidad —en las pruebas empatan,
y con "el jueves que viene no, el otro" DeepSeek fue más prudente: no inventó
una hora que nadie dijo— sino por cuota: la capa gratuita de Gemini daba 20
peticiones por día, y un asistente que deja de entender al mensaje 21 no sirve.

Modelo: deepseek-v4-flash. Directo a v4 y NO a deepseek-chat, que ya
desapareció del catálogo de DeepSeek. Construir sobre lo que se está muriendo
es el error que este proyecto ya pagó dos veces.

v4-flash razona antes de responder. Lo dejamos razonar: es lo que resuelve las
fechas ambiguas, cuesta fracciones de centavo, y el "reasoning leakage" que dio
problemas en Natalia acá no aplica porque solo leemos `content`, nunca
`reasoning_content`.
"""
from __future__ import annotations

import json
from datetime import datetime

from openai import AsyncOpenAI

from config import DEEPSEEK_API_KEY, TZ

cliente = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

MODELO = "deepseek-v4-flash"

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

CLASES = ("tarea", "cita", "nota", "idea", "gasto", "pregunta")

INSTRUCCIONES = """\
Sos el motor de comprensión de Lucy, la asistente personal de Tiziano.
Recibís un mensaje suyo, en español rioplatense/dominicano, muchas veces
informal o abreviado. Tu trabajo es ENTENDERLO, no responderle.

Ahora es {ahora} (zona {zona}, UTC-4, sin horario de verano).

Devolvés SOLO un objeto JSON con exactamente estas claves:
  clasificacion: uno de "tarea","cita","nota","idea","gasto","pregunta"
  titulo: string corto (en infinitivo si es tarea: "Llamar a Ana")
  detalle: string ("" si no aplica)
  cuando: ISO 8601 con offset, o "" si no hay ninguna referencia temporal
  duracion_min: entero (0 si no aplica)
  lugar: string ("" si no aplica)
  persona: string ("" si no aplica)
  proyecto: string ("" si no aplica)
  monto: número (0 si no aplica)
  moneda: SOLO si clasificacion es "gasto" (por defecto "DOP"); si no, ""
  supuestos: lista de strings — lo que dedujiste sin que te lo dijeran, en
             primera persona ("asumí que...")
  falta: lista de strings — solo datos CRÍTICOS ausentes que ameriten preguntar

CLASIFICACIÓN:
  tarea → algo que Tiziano tiene que hacer
  cita → algo que ocurre en un momento dado
  gasto → hay plata gastada
  nota → información para guardar, sin acción
  idea → algo que se le ocurrió y no quiere perder
  pregunta → le está preguntando algo a Lucy, no pidiéndole guardar algo

FECHAS — lo más delicado:
· Resolvé SIEMPRE lo relativo contra "ahora". "el jueves que viene no, el otro"
  = contá dos jueves. Si hoy es lunes y dice "el lunes" a una hora ya pasada,
  se refiere al lunes siguiente.
· Nunca inventes una hora exacta si el mensaje no la sugiere: dejá la fecha sin
  hora y anotá la hora en "falta".

SUPUESTOS Y FALTANTES — importan tanto como el resto:
· Si podés deducir algo razonablemente, deducilo y ponelo en "supuestos".
· "falta" es solo para lo crítico que amerite interrumpirlo.
· Molestar de más es peor que asumir de más, pero asumir en silencio es lo peor
  de todo: por eso todo supuesto va declarado.\
"""


def _ahora_txt() -> str:
    """'lunes 2026-07-20T20:24-04:00'.

    El día se arma a mano y no con strftime('%A'): strftime depende del locale
    del sistema y el contenedor de Railway corre en inglés. Sin esto, Lucy
    leería "Monday" adentro de un prompt en español.
    """
    ahora = datetime.now(TZ)
    return f"{DIAS[ahora.weekday()]} {ahora.isoformat(timespec='minutes')}"


def _validar(r: dict) -> dict:
    """Normaliza la respuesta. DeepSeek garantiza JSON, NO garantiza la forma.

    Gemini permitía forzar el esquema desde la API; acá no existe eso, así que
    el contrato se verifica de este lado. Confiar en que el modelo se porte
    bien es precisamente lo que este proyecto dejó de hacer.
    """
    if not isinstance(r, dict):
        raise ValueError(f"La respuesta no es un objeto JSON: {type(r).__name__}")

    clas = str(r.get("clasificacion", "")).strip().lower()
    if clas not in CLASES:
        # Preferimos guardar como nota antes que perder el mensaje: una
        # clasificación rara no justifica descartar lo que dijo Tiziano.
        r["clasificacion"] = "nota"
    else:
        r["clasificacion"] = clas

    if not str(r.get("titulo", "")).strip():
        raise ValueError("Vino sin título; el mensaje quedaría sin nombre.")

    # Las listas tienen que ser listas: el formateador las recorre sin preguntar.
    for campo in ("supuestos", "falta"):
        v = r.get(campo)
        r[campo] = [str(x) for x in v] if isinstance(v, list) else []

    return r


async def verificar_modelo() -> None:
    """Confirma al arrancar que la key sirve y el modelo se puede USAR.

    Hace una llamada real, no una consulta al catálogo. La versión anterior de
    este chequeo (contra Gemini) preguntaba si el modelo existía: existía, el
    log cantaba "OK", y cada llamada real moría con 404. Un chequeo que da
    tranquilidad falsa es peor que no tener chequeo.
    """
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY vacía: Lucy no podría interpretar nada.")

    await cliente.chat.completions.create(
        model=MODELO,
        messages=[{"role": "user", "content": "Respondé solo: ok"}],
        max_tokens=200,
        temperature=0,
    )


async def interpretar_texto(texto: str) -> dict:
    """Clasifica y extrae estructura. Devuelve el dict ya validado.

    El "ahora" se calcula en cada llamada, nunca se cachea: si el proceso lleva
    días levantado, un "ahora" del arranque haría que "mañana" apunte a un día
    que ya pasó.
    """
    respuesta = await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES.format(
                ahora=_ahora_txt(), zona=TZ.key)},
            {"role": "user", "content": texto},
        ],
        response_format={"type": "json_object"},
        temperature=0,  # extracción, no creatividad
    )
    return _validar(json.loads(respuesta.choices[0].message.content))
