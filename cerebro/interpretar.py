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

import captura.consumos as consumos
import captura.correo as correo
import cerebro.agente as agente
import cerebro.calendario as calendario
import cerebro.despertador as despertador
import cerebro.memoria as memoria
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
    vuelta = 0
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

        # Las ramas laterales: el despertador (cada ~30s) y el indexado de la
        # memoria (cada ~1min). Si fallan, la comprensión ni se entera —
        # misma filosofía que los logs de Natalia. Nada de lo que pase acá
        # puede tocar el camino del mensaje.
        vuelta += 1
        if vuelta % 6 == 0:
            try:
                await despertador.revisar(bot)
            except Exception:
                log.warning("El despertador tropezó; reintento en la próxima.",
                            exc_info=True)
        if vuelta % 12 == 0:
            try:
                await memoria.indexar_pendientes()
            except Exception:
                log.warning("No pude indexar memoria; reintento en la próxima.",
                            exc_info=True)
        if vuelta % 36 == 0:  # chequeo barato cada ~3 min; solo abre Gmail 1x/día
            # PENDIENTE, Y NO ES DE ESTE ARCHIVO NI DE ESTE ARREGLO (5-sep-2026).
            #
            # Este `await` es directo: no hay `create_task`. Mientras
            # `reporte_diario()` trabaja, el bucle NO vuelve al `for` de arriba,
            # así que Lucy deja de contestarle a Tiziano por Telegram hasta que
            # termine. Nadie decidió eso; salió de que todo cuelga del mismo
            # `await`.
            #
            # Medido con un arnés de IMAP falso el 5-sep-2026, un buzón con
            # 2.000 sin leer en la ventana disparaba 2.000 descargas de cuerpo
            # completo y 2.000 llamadas a DeepSeek, secuenciales. Hoy son 2.000
            # cabeceras, 60 cuerpos y 60 llamadas (ver `MAX_CLASIFICA_POR_VUELTA`
            # en captura/correo.py): el bloqueo bajó mucho, pero NO desapareció
            # — quedan las 2.000 cabeceras, una por fetch IMAP y en serie.
            #
            # Por qué no se arregla acá: no es un `create_task` y ya.
            #   · `reporte_diario()` y `confirmar_leidos()` están acoplados POR
            #     ORDEN a propósito ("mandar primero, marcar después"), y el
            #     candado de una-vez-al-día se apoya en que estas llamadas no se
            #     solapen: dos tareas a la vez pueden pasar el candado juntas y
            #     dejar dos encargos.
            #   · Y no es solo el correo. En este mismo bucle hay seis ramas más
            #     con `await` directo — despertador, memoria, 911, rescate de
            #     huérfanos, ingesta bancaria, calendario. Sacar una sola del
            #     camino deja las otras seis igual.
            # O sea: qué ramas se desacoplan del camino del mensaje, y con qué
            # garantías de orden, es un diseño de este bucle — no un renglón
            # dentro del arreglo del reporte de correo.
            try:
                await correo.reporte_diario()
            except Exception:
                log.warning("El reporte de correo tropezó; reintento en la próxima.",
                            exc_info=True)
            # Marcar leído lo ya informado va pegado al reporte y DESPUÉS: solo
            # marca lo que de verdad llegó (leído = "ya te informé").
            try:
                await correo.confirmar_leidos()
            except Exception:
                log.warning("No pude marcar leídos; reintento en la próxima.",
                            exc_info=True)
        # Vigilancia 911 cada ~10 min, las 24 horas: lo ÚNICO que interrumpe es
        # que se rompa la infraestructura donde viven Natalia y Lucy. Es barato
        # —mirar remitente y asunto— y no gasta IA salvo que encuentre algo.
        if vuelta % 120 == 0:
            try:
                await correo.vigilar_911(bot)
            except Exception:
                log.warning("La vigilancia 911 tropezó; sigo igual.",
                            exc_info=True)
        # La ingesta bancaria cada ~15 min (180 vueltas). No corre más seguido
        # porque una alerta de consumo no es urgente —el gasto ya ocurrió— y
        # abrir Gmail cada rato no gana nada. El canario va pegado y DESPUÉS:
        # solo puede avisar sobre una revisión que de verdad ocurrió.
        # El rescate de huérfanos también acá, cada ~10 min (120 vueltas), no
        # solo al arrancar. Un mensaje reclamado 3 minutos antes de un
        # redespliegue queda huérfano, y el rescate del arranque lo SALTA con
        # razón —su margen de 10 minutos existe para no pisar un turno vivo—;
        # como después no hay otro reinicio, se quedaba atascado para siempre.
        # Le pasó al "Dame el panel" de Rosi.
        if vuelta % 120 == 0:
            try:
                n = await db.rescatar_procesando()
                if n:
                    log.warning("Rescatados %s mensajes huérfanos en marcha.", n)
            except Exception:
                log.warning("El rescate de huérfanos tropezó; sigo.",
                            exc_info=True)

        if vuelta % 180 == 0:
            try:
                res = await consumos.revisar()
                await consumos.avisar_si_hay_bancos_mudos(res)
            except Exception:
                log.warning("La ingesta bancaria tropezó; reintento en la "
                            "próxima.", exc_info=True)
            # El latido va APARTE del try de arriba y después: es el aviso de
            # que la cosecha no está corriendo, así que tiene que sobrevivir
            # justo al caso en que la cosecha revienta. Dentro del mismo try,
            # la excepción de `revisar()` se lo saltaría — y ese es exactamente
            # el silencio que este aviso existe para romper.
            try:
                await consumos.avisar_si_no_hay_latido()
            except Exception:
                log.warning("El latido de la cosecha tropezó; sigo igual.",
                            exc_info=True)
        # El respaldo se chequea cada ~10 min (120 vueltas). Son dos SELECT
        # chicos, y quien decide cuándo sale el MENSAJE es el despertador
        # (48h sin backup, y después un recordatorio por día). Chequear seguido
        # y avisar poco es a propósito: lo que no puede pasar es que Lucy tarde
        # en enterarse, y lo que tampoco puede pasar es que lo repita hasta que
        # se lo ignore. Los 25 días de agosto sin respaldo entraron justo por
        # ese hueco — nada adentro del sistema estaba mirando.
        if vuelta % 120 == 0:
            try:
                await despertador.revisar_backup(bot)
            except Exception:
                log.warning("No pude revisar el estado del respaldo; sigo igual.",
                            exc_info=True)
        # El calendario se jala cada ~5 min (60 vueltas): las sesiones del
        # estudio no cambian cada segundo, y consultar 10 calendarios más
        # seguido gastaría cuota sin ganar frescura útil.
        if vuelta % 60 == 0:
            try:
                await calendario.sincronizar()
            except Exception:
                log.warning("No pude sincronizar el calendario; sigo igual.",
                            exc_info=True)

        await asyncio.sleep(INTERVALO_S)
