"""Captura desde Telegram.

Principio arquitectónico rey: este módulo NO importa nada de `cerebro/`.
Su único trabajo es guardar el mensaje crudo y confirmar recepción.
Que la IA funcione o no, es problema de otra capa — acá nada se pierde.
"""
from __future__ import annotations

from telegram import Message, Update
from telegram.ext import ContextTypes

import db.db as db


async def _capturar(
    msg: Message, *, tipo_entrada: str, archivo_id: str | None = None
) -> None:
    """Camino único de entrada para los tres tipos: guardar crudo y confirmar.

    El orden importa y no es negociable: primero la base, después el ✅. Si se
    invirtiera, Lucy podría confirmar algo que no quedó guardado — y la promesa
    entera del Nivel 1 es que un ✅ significa "esto ya está a salvo".
    """
    # 1. Guardar CRUDO primero. Sin IA. Esto es lo que hace que nada se pierda.
    bandeja_id = await db.guardar_en_bandeja(
        tipo_entrada=tipo_entrada,
        contenido_raw=msg.text or msg.caption,
        archivo_id=archivo_id,
        chat_id=msg.chat_id,
        telegram_msg_id=msg.message_id,
    )

    # 2. Confirmar. Nunca depende de que la IA esté viva.
    await msg.reply_text(f"✅ Recibí (#{bandeja_id})")

    # 3. La interpretación NO se dispara acá: la hace el bucle de
    #    cerebro/interpretar.py leyendo de la bandeja. Ese desacople es lo que
    #    hace que tu ✅ no espere a que la IA piense.


async def recibir_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _capturar(update.message, tipo_entrada="texto")


async def recibir_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notas de voz (grabadas en el momento) y archivos de audio reenviados.

    No descargamos el archivo: el file_id de Telegram no caduca y sirve para
    bajarlo cuando el Nivel 2 vaya a transcribirlo. Guardar los bytes acá sería
    pagar almacenamiento por algo que Telegram ya guarda gratis, y además
    metería una descarga —que puede fallar— entre vos y el ✅.
    """
    msg = update.message
    archivo = msg.voice or msg.audio
    await _capturar(msg, tipo_entrada="audio", archivo_id=archivo.file_id)


async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fotos: tickets, carteles, tarjetas de visita.

    msg.photo trae el mismo archivo en varias resoluciones, de menor a mayor.
    Nos quedamos con la última —la más grande— porque de un ticket vamos a
    querer leer la letra chica.
    """
    msg = update.message
    await _capturar(msg, tipo_entrada="foto", archivo_id=msg.photo[-1].file_id)
