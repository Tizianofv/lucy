"""Orquesta la comprensión: toma filas 'sin_procesar' de la bandeja, transcribe
la voz si hace falta, la pasa por el cerebro y le cuenta a Tiziano qué entendió.

Corre en un bucle propio, desacoplado de la captura. Ese desacople es la razón
de ser del diseño: si la IA se cae o tarda, la captura sigue respondiendo ✅ al
instante y los mensajes se apilan acá esperando. Nada se pierde, nada se traba.

Estado: Nivel 2, primera mitad. Interpreta y reporta; todavía no crea tareas
ni eventos — eso es el paso siguiente, con botones para corregir.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import escape

import openai
import telegram.error

import cerebro.deepseek as motor
import cerebro.whisper as whisper
import db.db as db
from cerebro.deepseek import DIAS

log = logging.getLogger("lucy.interpretar")

# Cada cuánto mira si hay algo nuevo. 5s es imperceptible para vos y no le
# hace cosquillas a la base: es una query indexada por estado.
INTERVALO_S = 5

# Reintentos ante fallos pasajeros (cuota de la IA, red). 30s, 60s, 120s…
MAX_INTENTOS = 5
ESPERA_BASE_S = 30

ICONO = {
    "tarea": "📌",
    "cita": "📅",
    "nota": "📝",
    "idea": "💡",
    "gasto": "💸",
    "pregunta": "❓",
}


def _fecha_legible(iso: str) -> str:
    """ISO 8601 → 'martes 21/07 10:00'. Devuelve el crudo si no parsea."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    hora = "" if (d.hour == 0 and d.minute == 0) else d.strftime(" %H:%M")
    return f"{DIAS[d.weekday()]} {d.strftime('%d/%m')}{hora}"


def _formatear(bandeja_id: int, r: dict, oido: str | None = None) -> str:
    """Arma el mensaje que Lucy manda con lo que entendió.

    Los supuestos van SIEMPRE visibles, no escondidos: el req 9 pide que Lucy
    confiese lo que dedujo. Un asistente que asume en silencio es un asistente
    en el que no se puede confiar.

    `oido` es la transcripción, cuando el mensaje fue una nota de voz. Se
    muestra por la misma razón: si Whisper entendió mal, tenés que poder verlo
    en el momento y no descubrirlo por una tarea absurda tres días después.
    """
    # esc() es obligatorio en TODO lo que venga de vos o del modelo: el mensaje
    # va con parse_mode HTML, así que un "<" en "gasté <1200" o un "&" en un
    # título haría que Telegram rechace el mensaje entero y la tarjeta se pierda.
    esc = escape

    clas = r.get("clasificacion", "nota")
    lineas = [f"{ICONO.get(clas, '•')} <b>{clas.capitalize()}</b> · #{bandeja_id}",
              f"<b>{esc(str(r.get('titulo', '')))}</b>"]

    if oido:
        lineas.append(f"🎙 <i>«{esc(oido)}»</i>")

    if r.get("detalle"):
        lineas.append(esc(str(r["detalle"])))

    datos = []
    if r.get("cuando"):
        datos.append(f"🕐 {esc(_fecha_legible(r['cuando']))}")
    if r.get("duracion_min"):
        datos.append(f"⏱ {r['duracion_min']} min")
    if r.get("lugar"):
        datos.append(f"📍 {esc(str(r['lugar']))}")
    if r.get("persona"):
        datos.append(f"👤 {esc(str(r['persona']))}")
    if r.get("monto"):
        datos.append(f"💵 {r['monto']:g} {esc(str(r.get('moneda') or 'DOP'))}")
    if r.get("proyecto"):
        datos.append(f"🗂 {esc(str(r['proyecto']))}")
    if datos:
        lineas.append("")
        lineas.extend(datos)

    if r.get("supuestos"):
        lineas.append("")
        lineas.append("<i>Asumí:</i>")
        lineas.extend(f"· <i>{esc(s)}</i>" for s in r["supuestos"])

    if r.get("falta"):
        lineas.append("")
        lineas.append("<i>Me falta saber:</i>")
        lineas.extend(f"· <i>{esc(f)}</i>" for f in r["falta"])

    return "\n".join(lineas)


def _es_pasajero(e: Exception) -> bool:
    """¿Vale la pena reintentar, o el mensaje está roto de verdad?

    Pasajero: cuota agotada (429), caída del proveedor (5xx), timeouts de red.
    Definitivo: el modelo no existe, la key no sirve, el contenido es inválido.
    Reintentar lo definitivo es martillar la API sin sentido; NO reintentar lo
    pasajero es perder el mensaje. La distinción importa.
    """
    # OJO con el orden: el SDK de OpenAI/DeepSeek guarda el número HTTP en
    # .status_code, mientras que .code trae un string ('rate_limit_exceeded').
    # Mirar .code primero con un `or` haría que ese string —verdadero— tapara
    # al 429, y todos los cortes de cuota pasarían por definitivos. Es
    # exactamente el fallo que esta función existe para evitar.
    codigo = getattr(e, "status_code", None)
    if not isinstance(codigo, int):
        c = getattr(e, "code", None)
        codigo = c if isinstance(c, int) else None

    if codigo in (408, 409, 429, 500, 502, 503, 504):
        return True

    return isinstance(e, (
        openai.APIConnectionError,   # no llegamos al proveedor
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.InternalServerError,
        telegram.error.NetworkError,  # bajando el audio de Telegram
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    ))


async def _fallo(fila: dict, e: Exception, bot) -> None:
    """Decide si la fila vuelve a la cola o se da por perdida."""
    bandeja_id = fila["id"]

    if _es_pasajero(e) and fila.get("intentos", 0) < MAX_INTENTOS:
        # Espera que se duplica: 30s, 60s, 120s… así un corte largo no se
        # convierte en un martilleo contra la API.
        espera = ESPERA_BASE_S * (2 ** fila.get("intentos", 0))
        n = await db.devolver_a_cola(bandeja_id, espera)
        log.warning(
            "Fallo pasajero en #%s (%s). Reintento %s/%s en %ss.",
            bandeja_id, type(e).__name__, n, MAX_INTENTOS, espera,
        )
        # A propósito NO le avisamos a Tiziano: en 30 segundos lo más probable
        # es que funcione. Avisar de algo que se arregla solo es ruido, y el
        # pilar de silencio inteligente dice que hay que ganarse la interrupción.
        return

    log.exception("Fallo definitivo interpretando #%s", bandeja_id)
    await db.marcar_error(bandeja_id, f"{type(e).__name__}: {e}")
    await bot.send_message(
        chat_id=fila["chat_id"],
        text=f"⚠️ Guardé tu mensaje (#{bandeja_id}) pero no logro interpretarlo.\n"
             "Está a salvo en la bandeja: no se perdió nada.",
        reply_to_message_id=fila.get("telegram_msg_id"),
    )


async def _obtener_texto(fila: dict, bot) -> str | None:
    """Devuelve el texto a interpretar, transcribiendo la voz si hace falta.

    La transcripción se guarda ANTES de interpretar: si DeepSeek falla después,
    el reintento no vuelve a pagar —ni a esperar— la transcripción del audio.
    """
    if fila["tipo_entrada"] == "audio":
        if fila.get("transcripcion"):
            return fila["transcripcion"]  # ya transcripto en un intento previo

        archivo = await bot.get_file(fila["archivo_id"])
        datos = await archivo.download_as_bytearray()
        texto = await whisper.transcribir(bytes(datos))
        await db.guardar_transcripcion(fila["id"], texto)
        log.info("Transcripto #%s (%s caracteres)", fila["id"], len(texto))

        # El pie de foto del audio, si lo hubiera, suma contexto a lo dicho.
        if fila.get("contenido_raw"):
            return f"{texto}\n\n({fila['contenido_raw']})"
        return texto

    return fila.get("contenido_raw")


async def _procesar(fila: dict, bot) -> None:
    """Interpreta una fila y reporta. Un fallo acá no puede tumbar el bucle."""
    bandeja_id = fila["id"]

    try:
        texto = await _obtener_texto(fila, bot)
    except Exception as e:
        await _fallo(fila, e, bot)
        return

    if not texto or not texto.strip():
        await db.marcar_error(bandeja_id, "Sin contenido que interpretar.")
        return

    try:
        r = await motor.interpretar_texto(texto)
    except Exception as e:
        await _fallo(fila, e, bot)
        return

    await db.guardar_interpretacion(bandeja_id, r["clasificacion"], r)
    await bot.send_message(
        chat_id=fila["chat_id"],
        text=_formatear(bandeja_id, r, oido=fila["tipo_entrada"] == "audio" and texto),
        parse_mode="HTML",
        reply_to_message_id=fila.get("telegram_msg_id"),
    )
    log.info("Interpretado #%s como %s", bandeja_id, r["clasificacion"])


async def bucle(bot) -> None:
    """Bucle infinito de comprensión. Se lanza al arrancar (ver main.py)."""
    log.info("Bucle de interpretación en marcha (cada %ss).", INTERVALO_S)
    while True:
        try:
            for fila in await db.tomar_pendientes():
                await _procesar(fila, bot)
        except asyncio.CancelledError:
            raise  # apagado ordenado: no lo tratamos como error
        except Exception:
            # Que una vuelta falle no puede matar el bucle: si se muere en
            # silencio, Lucy vuelve a "solo bandeja" sin que nadie se entere.
            log.exception("Error en el bucle de interpretación; sigo igual.")
        await asyncio.sleep(INTERVALO_S)
