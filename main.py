"""Arranque de Lucy: conecta con Telegram por long-polling y despacha mensajes.

Un solo proceso. No necesita webhook ni servidor web: Lucy le pregunta a
Telegram "¿algo nuevo?" en un bucle. Simple y robusto.
"""
import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import config
import db.db as db
from captura.telegram import recibir_texto

logging.basicConfig(
    format="%(asctime)s · %(levelname)s · %(name)s · %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("lucy")


async def _al_arrancar(app) -> None:
    await db.abrir()
    log.info("Pool de Postgres abierto. Lucy escuchando solo a %s.", config.CHAT_ID_DUENO)


async def _al_apagar(app) -> None:
    await db.cerrar()


async def _al_fallar(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Última red: ningún fallo puede terminar en silencio (pilar #39).

    Sin este manejador, una excepción en el handler muere en el log y Tiziano
    se queda esperando una respuesta que no llega. Un aviso honesto siempre es
    mejor que un silencio educado: el silencio parece que Lucy te ignoró.
    """
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
    app.add_error_handler(_al_fallar)

    log.info("Lucy arrancando…")
    app.run_polling()


if __name__ == "__main__":
    main()
