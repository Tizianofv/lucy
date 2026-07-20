"""Arranque de Lucy: conecta con Telegram por long-polling y despacha mensajes.

Un solo proceso. No necesita webhook ni servidor web: Lucy le pregunta a
Telegram "¿algo nuevo?" en un bucle. Simple y robusto.
"""
import logging

from telegram.ext import ApplicationBuilder, MessageHandler, filters

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

    log.info("Lucy arrancando…")
    app.run_polling()


if __name__ == "__main__":
    main()
