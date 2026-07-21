"""Arranque de Lucy: conecta con Telegram por long-polling y despacha mensajes.

Un solo proceso. No necesita webhook ni servidor web: Lucy le pregunta a
Telegram "¿algo nuevo?" en un bucle. Simple y robusto.
"""
import asyncio
import logging

from telegram import Update
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import cerebro.gemini as gemini
import cerebro.interpretar as interpretar
import config
import db.db as db
from captura.telegram import recibir_audio, recibir_foto, recibir_texto

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(name)s · %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("lucy")

# gemini-3.5-flash razona antes de responder, y el SDK avisa por consola en
# CADA llamada que la respuesta trae partes de pensamiento además del texto.
# Es informativo, no un problema — pero repetido en cada mensaje convierte el
# log en ruido, y el ruido es lo que nos entrena a ignorar los errores reales.
logging.getLogger("google_genai.types").setLevel(logging.ERROR)

_tarea_interpretacion: asyncio.Task | None = None


async def _al_arrancar(app) -> None:
    await db.abrir()
    log.info("Pool de Postgres abierto. Lucy escuchando solo a %s.", config.CHAT_ID_DUENO)

    # La IA se chequea, pero su falla NO tumba a Lucy: capturar no puede
    # depender de que Gemini esté vivo (es el principio del Nivel 1). Un
    # modelo jubilado o una key vencida degradan a Lucy a "solo bandeja",
    # que es exactamente lo que queremos: ruidoso en el log, intacto para vos.
    try:
        await gemini.verificar_modelo()
        log.info("Gemini OK: modelo %s disponible.", gemini.MODELO)
    except Exception:
        log.exception(
            "GEMINI NO DISPONIBLE — la captura sigue funcionando, "
            "pero no habrá interpretación hasta arreglarlo."
        )

    # El bucle de comprensión vive aparte del de captura, a propósito: si
    # interpretar tarda 4 segundos, tu ✅ no espera esos 4 segundos.
    global _tarea_interpretacion
    _tarea_interpretacion = asyncio.create_task(interpretar.bucle(app.bot))


async def _al_apagar(app) -> None:
    if _tarea_interpretacion is not None:
        _tarea_interpretacion.cancel()
        # Esperamos a que muera de verdad: si el proceso se apaga con una
        # fila en 'procesando', esa fila queda huérfana hasta que alguien la
        # rescate a mano.
        await asyncio.gather(_tarea_interpretacion, return_exceptions=True)
    await db.cerrar()


async def _al_fallar(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Última red: ningún fallo puede terminar en silencio (pilar #39).

    Sin este manejador, una excepción en el handler muere en el log y Tiziano
    se queda esperando una respuesta que no llega. Un aviso honesto siempre es
    mejor que un silencio educado: el silencio parece que Lucy te ignoró.
    """
    # El Conflict es esperable: en cada redespliegue el contenedor viejo y el
    # nuevo se pisan unos segundos peleando por el long-polling. Registrarlo
    # como ERROR sería crear un error benigno recurrente — y un log que grita
    # todos los días es un log que se deja de leer.
    if isinstance(context.error, Conflict):
        log.warning("Conflict de Telegram (dos instancias); típico de un redespliegue.")
        return

    log.error("Fallo procesando un update", exc_info=context.error)

    msg = getattr(update, "effective_message", None)
    if msg is None:
        return  # Fallo sin mensaje asociado (p. ej. de red): solo queda en el log.

    try:
        await msg.reply_text(
            "⚠️ Te leí, pero no pude guardarlo — la base no está respondiendo.\n"
            "NO quedó registrado. Volvé a mandármelo cuando te avise."
        )
    except Exception:
        # Si ni siquiera podemos avisar, que al menos quede el rastro.
        log.exception("Tampoco pude avisar del fallo por Telegram")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_al_arrancar)
        .post_shutdown(_al_apagar)
        .build()
    )

    # Candado de seguridad: solo procesamos mensajes del chat de Tiziano.
    solo_dueno = filters.Chat(config.CHAT_ID_DUENO)

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & solo_dueno, recibir_texto)
    )
    # VOICE = nota de voz grabada en el momento; AUDIO = archivo de audio reenviado.
    app.add_handler(
        MessageHandler((filters.VOICE | filters.AUDIO) & solo_dueno, recibir_audio)
    )
    app.add_handler(MessageHandler(filters.PHOTO & solo_dueno, recibir_foto))

    app.add_error_handler(_al_fallar)

    log.info("Lucy arrancando…")
    app.run_polling()


if __name__ == "__main__":
    main()
