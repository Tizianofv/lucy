"""Orquesta la comprensión: toma filas 'sin_procesar' de la bandeja, las pasa
por Gemini, y le cuenta a Tiziano qué entendió.

Corre en un bucle propio, desacoplado de la captura. Ese desacople es la razón
de ser del diseño: si Gemini se cae o tarda, la captura sigue respondiendo ✅ al
instante y los mensajes se apilan acá esperando. Nada se pierde, nada se traba.

Estado: Nivel 2, primera mitad. Interpreta y reporta; todavía no crea tareas
ni eventos — eso es el paso siguiente, con botones para corregir.
"""
import asyncio
import logging
from datetime import datetime

import cerebro.gemini as gemini
import db.db as db
from cerebro.gemini import DIAS

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


def _formatear(bandeja_id: int, r: dict) -> str:
    """Arma el mensaje que Lucy manda con lo que entendió.

    Los supuestos van SIEMPRE visibles, no escondidos: el req 9 pide que Lucy
    confiese lo que dedujo. Un asistente que asume en silencio es un asistente
    en el que no se puede confiar.
    """
    clas = r.get("clasificacion", "nota")
    lineas = [f"{ICONO.get(clas, '•')} <b>{clas.capitalize()}</b> · #{bandeja_id}",
              f"<b>{r.get('titulo', '')}</b>"]

    if r.get("detalle"):
        lineas.append(r["detalle"])

    datos = []
    if r.get("cuando"):
        datos.append(f"🕐 {_fecha_legible(r['cuando'])}")
    if r.get("duracion_min"):
        datos.append(f"⏱ {r['duracion_min']} min")
    if r.get("lugar"):
        datos.append(f"📍 {r['lugar']}")
    if r.get("persona"):
        datos.append(f"👤 {r['persona']}")
    if r.get("monto"):
        datos.append(f"💵 {r['monto']:g} {r.get('moneda') or 'DOP'}")
    if r.get("proyecto"):
        datos.append(f"🗂 {r['proyecto']}")
    if datos:
        lineas.append("")
        lineas.extend(datos)

    if r.get("supuestos"):
        lineas.append("")
        lineas.append("<i>Asumí:</i>")
        lineas.extend(f"· <i>{s}</i>" for s in r["supuestos"])

    if r.get("falta"):
        lineas.append("")
        lineas.append("<i>Me falta saber:</i>")
        lineas.extend(f"· <i>{f}</i>" for f in r["falta"])

    return "\n".join(lineas)


def _es_pasajero(e: Exception) -> bool:
    """¿Vale la pena reintentar, o el mensaje está roto de verdad?

    Pasajero: cuota agotada (429), caída del proveedor (5xx), timeouts de red.
    Definitivo: el modelo no existe, la key no sirve, el contenido es inválido.
    Reintentar lo definitivo es martillar la API sin sentido; NO reintentar lo
    pasajero es perder el mensaje. La distinción importa.
    """
    codigo = getattr(e, "code", None) or getattr(e, "status_code", None)
    if codigo in (429, 500, 502, 503, 504):
        return True
    return isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError))


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


async def _procesar(fila: dict, bot) -> None:
    """Interpreta una fila y reporta. Un fallo acá no puede tumbar el bucle."""
    bandeja_id = fila["id"]
    texto = fila.get("contenido_raw")

    if not texto:
        await db.marcar_error(bandeja_id, "Sin texto para interpretar.")
        return

    try:
        r = await gemini.interpretar_texto(texto)
    except Exception as e:
        await _fallo(fila, e, bot)
        return

    await db.guardar_interpretacion(bandeja_id, r.get("clasificacion", "nota"), r)
    await bot.send_message(
        chat_id=fila["chat_id"],
        text=_formatear(bandeja_id, r),
        parse_mode="HTML",
        reply_to_message_id=fila.get("telegram_msg_id"),
    )
    log.info("Interpretado #%s como %s", bandeja_id, r.get("clasificacion"))


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
