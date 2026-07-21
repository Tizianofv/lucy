"""El despachador: toma filas 'sin_procesar', vuelve texto lo que haga falta
(voz, foto) y le entrega el mensaje al agente.

Corre en un bucle propio, desacoplado de la captura. Ese desacople es la razón
de ser del diseño: si la IA se cae o tarda, la captura sigue respondiendo ✅
al instante y los mensajes se apilan acá esperando. Nada se pierde.

Este módulo ya no entiende nada por sí mismo. Antes era un pasillo de salones
—clasificar, y según la puerta un prompt distinto—; ahora la comprensión
entera vive en cerebro/agente.py, que trabaja con herramientas y puede
preguntarle a Tiziano por Telegram cuando no sabe (la ventana). Acá queda lo
que no es pensar: la cola, los reintentos con espera creciente, y la
distinción entre un tropiezo pasajero y un fallo real.
"""
from __future__ import annotations

import asyncio
import logging

import openai
import telegram.error

import cerebro.agente as agente
import cerebro.preguntar as preguntar
import cerebro.vision as vision
import cerebro.whisper as whisper
import db.db as db

log = logging.getLogger("lucy.interpretar")

# Cada cuánto mira si hay algo nuevo. 5s es imperceptible para vos y no le
# hace cosquillas a la base: es una query indexada por estado.
INTERVALO_S = 5

# Reintentos ante fallos pasajeros (cuota de la IA, red). 30s, 60s, 120s…
MAX_INTENTOS = 5
ESPERA_BASE_S = 30


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

    log.exception("Fallo definitivo en #%s", bandeja_id)
    await db.marcar_error(bandeja_id, f"{type(e).__name__}: {e}")

    # Preguntar en vez de informar el error: una pregunta concreta deja la
    # conversación viva; un "no pude" la mata.
    dicho = fila.get("transcripcion") or fila.get("contenido_raw") or ""
    pregunta = await preguntar.repreguntar(dicho, f"{type(e).__name__}: {e}")
    await agente._enviar(
        bot,
        text=f"{pregunta}\n\n(Tu mensaje quedó guardado como #{bandeja_id}, "
             f"no se perdió nada.)",
        chat_id=fila["chat_id"],
        reply_to_message_id=fila.get("telegram_msg_id"),
    )


async def _obtener_texto(fila: dict, bot) -> str | None:
    """Devuelve el texto a interpretar, leyendo la voz o la foto si hace falta.

    Voz y foto siguen exactamente el mismo camino porque son el mismo problema:
    un archivo que hay que volver texto antes de poder entenderlo. Whisper y
    gpt-4o-mini son intercambiables acá — cambia el traductor, no el recorrido.

    Lo leído se guarda ANTES de interpretar: si el agente falla después, el
    reintento no vuelve a pagar —ni a esperar— la lectura del archivo.
    """
    tipo = fila["tipo_entrada"]
    if tipo not in ("audio", "foto"):
        return fila.get("contenido_raw")

    texto = fila.get("transcripcion")
    if not texto:  # si ya se leyó en un intento previo, no se vuelve a pagar
        archivo = await bot.get_file(fila["archivo_id"])
        datos = bytes(await archivo.download_as_bytearray())
        texto = (
            await whisper.transcribir(datos) if tipo == "audio"
            else await vision.leer(datos)
        )
        await db.guardar_transcripcion(fila["id"], texto)
        log.info("Leído #%s (%s, %s caracteres)", fila["id"], tipo, len(texto))

    # El pie de foto suma contexto a lo leído ("esto es del almuerzo de ayer").
    if fila.get("contenido_raw"):
        return f"{texto}\n\n({fila['contenido_raw']})"
    return texto


async def _procesar(fila: dict, bot) -> None:
    """Un mensaje → texto → agente. Un fallo acá no puede tumbar el bucle."""
    try:
        texto = await _obtener_texto(fila, bot)
        if not texto or not texto.strip():
            await db.marcar_error(fila["id"], "Sin contenido que interpretar.")
            return
        await agente.atender(fila, texto, bot)
    except Exception as e:
        await _fallo(fila, e, bot)


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
