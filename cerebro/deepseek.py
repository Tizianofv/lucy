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

# El "sin-key" no es cosmético: el SDK revienta al CONSTRUIR el cliente si la
# key viene vacía, y eso ocurre al importar el módulo. Sin el placeholder, una
# key ausente no degradaría a Lucy: la mataría entera al arrancar, incluida la
# captura — exactamente lo contrario de lo que promete config.py. Con él, el
# cliente existe, verificar_modelo() falla a los gritos en el log, y el Nivel 1
# sigue vivo recibiendo mensajes.
cliente = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY or "sin-key", base_url="https://api.deepseek.com"
)

MODELO = "deepseek-v4-flash"

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

CLASES = ("tarea", "cita", "nota", "idea", "gasto", "ingreso",
          "pregunta", "orden", "charla")

# Las que se convierten en una fila de verdad. El resto (pregunta, charla) se
# responde y se archiva: no todo lo que Tiziano dice es algo para guardar.
CLASES_ENTIDAD = ("tarea", "cita", "nota", "idea", "gasto", "ingreso")

ICONO = {
    "tarea": "📌",
    "cita": "📅",
    "nota": "📝",
    "idea": "💡",
    "gasto": "💸",
    "ingreso": "💰",
    "pregunta": "❓",
    "orden": "🛠",
    "charla": "💬",
}

# Lo que se le dice al cerebro sobre DE DÓNDE vino el texto.
#
# Con texto y voz, las palabras son de Tiziano. Con una foto no: el texto es
# algo que él te MUESTRA. Sin esta aclaración, la captura de un cliente
# preguntando por su depósito se leía como si Tiziano se lo preguntara a Lucy
# — el canal cambia el significado, y el cerebro tiene que saberlo.
CONTEXTO_ORIGEN = {
    "texto": "",
    "audio": "",
    "foto": (
        "\nORIGEN — IMPORTANTE: lo que sigue NO son palabras de Tiziano. Es lo "
        "que se ve en una FOTO que él sacó y te muestra: un ticket, una captura "
        "de pantalla, un cartel, una tarjeta. Preguntate qué significa que te la "
        "muestre, en vez de leerlo como si él lo estuviera diciendo. Si es la "
        "captura de alguien escribiéndole, quien habla ahí es esa persona y no "
        "él; lo más probable entonces es que haya algo que hacer al respecto.\n"
    ),
}

INSTRUCCIONES = """\
Sos el motor de comprensión de Lucy, la asistente personal de Tiziano.
Recibís un mensaje suyo, en español rioplatense/dominicano, muchas veces
informal o abreviado. Tu trabajo es ENTENDERLO, no responderle.

Ahora es {ahora} (zona {zona}, UTC-4, sin horario de verano).
{origen}

Devolvés SOLO un objeto JSON con exactamente estas claves:
  clasificacion: uno de "tarea","cita","nota","idea","gasto","pregunta","charla"
  titulo: string corto (en infinitivo si es tarea: "Llamar a Ana")
  detalle: string ("" si no aplica)
  cuando: ISO 8601 con offset, o "" si no hay ninguna referencia temporal
  duracion_min: entero (0 si no aplica)
  lugar: string ("" si no aplica)
  persona: string ("" si no aplica)
  proyecto: string ("" si no aplica)
  monto: número SIEMPRE POSITIVO (0 si no aplica). Que la plata entre o salga
         lo dice la clasificación, no el signo.
  moneda: SOLO si es "gasto" o "ingreso" (por defecto "DOP"); si no, ""
  referencia: No. de confirmación, comprobante o factura si aparece; si no, ""
  contraparte: SOLO en "gasto" o "ingreso" — el comercio donde gastó, o quién
               le pagó/transfirió (en un comprobante, el ORIGEN cuando la
               plata entra). "" si no aplica.
  supuestos: lista de strings — lo que dedujiste sin que te lo dijeran, en
             primera persona ("asumí que...")
  falta: lista de strings — solo datos CRÍTICOS ausentes que ameriten preguntar
  respuesta: SOLO si clasificacion es "charla" — lo que Lucy le contesta, en su
             mismo registro y tono, breve y cálida. "" en todo lo demás.
  alternativa: otra clasificación que también sería razonable, o "" si estás
               seguro. Solo cuando dudás DE VERDAD entre dos: sirve para
               ofrecerle a Tiziano un botón y que elija él. Poner una
               alternativa por las dudas, cuando la primera es clara, le
               agrega una decisión inútil a cada mensaje.

CLASIFICACIÓN:
  tarea → algo que Tiziano tiene que hacer
  cita → algo que ocurre en un momento dado
  gasto → plata que SALE: una compra, un pago que hizo, un ticket
  ingreso → plata que ENTRA: le transfirieron, le pagaron, cobró algo.
            En un comprobante, mirá quién es el DESTINO: si el destino es
            Tiziano (Fajardo Vargas), la plata entró y es "ingreso", aunque el
            papel diga "transferencia". Confundir esto le invierte el balance.
  nota → información para guardar, sin acción
  idea → algo que se le ocurrió y no quiere perder
  pregunta → quiere CONSULTAR sus datos (agenda, pendientes, gastos)
  orden → quiere CAMBIAR algo que ya existe: marcarlo como hecho, moverlo de
          hora, corregir un dato, archivarlo.
          Lo que separa "tarea" de "orden" es si la cosa ya existe:
            "llamar al contador"          → tarea (nace algo nuevo)
            "ya llamé al contador"        → orden (completar la que existe)
            "movelo a las 6"              → orden (cambiar una que existe)
            "esa reunión era el jueves"   → orden (corregir una que existe)
  charla → saludos, cortesías, bromas, "¿estás ahí?", "gracias": conversación
           que no pide guardar NADA ni consultar NADA

NO fuerces la charla dentro de otra categoría. "Buenos días" no es una nota y
"klk viejita" no es una pregunta. Si Tiziano solo está saludando o tirando un
chiste, es "charla" y punto: inventarle una categoría le llena la base de
basura que después tiene que borrar a mano.

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

    # La charla no genera ninguna fila, así que no necesita título: exigirlo
    # obligaría al modelo a inventar un "Saludo matutino" para algo que no se
    # guarda en ningún lado. Lo que sí necesita es la respuesta.
    if r["clasificacion"] == "charla":
        r["titulo"] = str(r.get("titulo") or "").strip()
        r["respuesta"] = str(r.get("respuesta") or "").strip() or "👋"
    elif not str(r.get("titulo", "")).strip():
        raise ValueError("Vino sin título; el mensaje quedaría sin nombre.")

    # Las listas tienen que ser listas: el formateador las recorre sin preguntar.
    for campo in ("supuestos", "falta"):
        v = r.get(campo)
        r[campo] = [str(x) for x in v] if isinstance(v, list) else []

    # La alternativa solo vale si crea algo y si de verdad es OTRA cosa. Un
    # botón que ofrece lo mismo que ya dice la tarjeta es ruido con forma de
    # opción, y el pilar de silencio inteligente aplica también a los botones.
    alt = str(r.get("alternativa", "")).strip().lower()
    r["alternativa"] = (
        alt if alt in CLASES_ENTIDAD and alt != r["clasificacion"] else ""
    )

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


async def interpretar_texto(texto: str, origen: str = "texto") -> dict:
    """Clasifica y extrae estructura. Devuelve el dict ya validado.

    `origen` es el tipo de entrada (texto|audio|foto) y NO es un detalle: lo
    que llega de una foto no son palabras de Tiziano sino algo que él muestra,
    y leerlo como si lo dijera cambia por completo la interpretación.

    El "ahora" se calcula en cada llamada, nunca se cachea: si el proceso lleva
    días levantado, un "ahora" del arranque haría que "mañana" apunte a un día
    que ya pasó.
    """
    respuesta = await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES.format(
                ahora=_ahora_txt(), zona=TZ.key,
                origen=CONTEXTO_ORIGEN.get(origen, ""))},
            {"role": "user", "content": texto},
        ],
        response_format={"type": "json_object"},
        temperature=0,  # extracción, no creatividad
    )
    return _validar(json.loads(respuesta.choices[0].message.content))
