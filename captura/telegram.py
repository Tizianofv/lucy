"""Captura desde Telegram.

Principio arquitectónico rey: este módulo NO importa nada de `cerebro/`.
Su único trabajo es guardar el mensaje crudo y confirmar recepción.
Que la IA funcione o no, es problema de otra capa — acá nada se pierde.
"""
from telegram import Update
from telegram.ext import ContextTypes

import db.db as db


async def recibir_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message

    # 1. Guardar CRUDO primero. Sin IA. Esto es lo que hace que nada se pierda.
    bandeja_id = await db.guardar_en_bandeja(
        tipo_entrada="texto",
        contenido_raw=msg.text,
        chat_id=msg.chat_id,
        telegram_msg_id=msg.message_id,
    )

    # 2. Confirmar. Nunca depende de que Gemini esté vivo.
    await msg.reply_text(f"✅ Recibí (#{bandeja_id})")

    # 3. (Nivel 2) Acá enganchará el disparo de la interpretación con Gemini,
    #    en segundo plano, leyendo de la bandeja. Todavía no.


# TODO (Nivel 1): recibir_audio() → descarga el archivo, lo guarda en bandeja
#   como tipo_entrada='audio', responde ✅, y luego Gemini lo transcribe.
# TODO (Nivel 1): recibir_foto()  → idem con tipo_entrada='foto'.
