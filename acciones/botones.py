"""Botones de confirmación: el momento en que lo entendido se vuelve real.

Es la implementación del pilar "confirmación proporcional al riesgo". Crear
una fila en la agenda de Tiziano no puede pasar a sus espaldas, pero tampoco
puede costarle una conversación: cuesta un toque.

Estados que maneja la bandeja acá:
  esperando_confirmacion → procesado   (tocó ✅ y se creó la entidad)
  esperando_confirmacion → descartado  (tocó 🗑; el mensaje crudo NO se borra)
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import acciones.crud as crud
import config
import db.db as db

log = logging.getLogger("lucy.botones")

# Nombre humano de cada tabla, para el mensaje de confirmación.
COMO_SE_LLAMA = {
    "tareas": "tarea",
    "eventos": "cita",
    "notas": "nota",
    "gastos": "gasto",
}


def teclado(bandeja_id: int) -> InlineKeyboardMarkup:
    """Los botones que van debajo de la tarjeta de interpretación.

    El bandeja_id viaja en el callback_data porque el callback llega suelto,
    sin contexto: es el único hilo que ata el botón a la fila que representa.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Dale", callback_data=f"ok:{bandeja_id}"),
        InlineKeyboardButton("🗑 Descartar", callback_data=f"no:{bandeja_id}"),
    ]])


async def _cerrar_tarjeta(q, remate: str) -> None:
    """Saca los botones y deja escrito en qué terminó la tarjeta.

    Editar en vez de mandar un mensaje nuevo es deliberado: la respuesta queda
    pegada a lo que se decidió, y un botón ya usado desaparece en lugar de
    quedar ahí invitando a tocarlo otra vez.
    """
    try:
        await q.edit_message_text(
            text=f"{q.message.text_html}\n\n{remate}",
            parse_mode="HTML",
            reply_markup=None,
        )
    except BadRequest as e:
        # "Message is not modified" y similares no son un problema real: lo que
        # importaba (la escritura en la base) ya pasó.
        log.warning("No pude editar la tarjeta: %s", e)


async def al_pulsar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query

    # Candado, igual que en los mensajes: el bot es de Tiziano y de nadie más.
    if q.message.chat_id != config.CHAT_ID_DUENO:
        await q.answer()
        return

    try:
        accion, sid = q.data.split(":", 1)
        bandeja_id = int(sid)
    except (ValueError, AttributeError):
        await q.answer("Botón ilegible.", show_alert=True)
        return

    # Cortar el relojito de Telegram cuanto antes; si no, parece que se colgó.
    await q.answer()

    if accion == "no":
        if not await db.cambiar_estado(bandeja_id, "descartado", desde="esperando_confirmacion"):
            await _cerrar_tarjeta(q, "<i>(ya estaba resuelto)</i>")
            return
        # El mensaje crudo sigue intacto en la bandeja: descartar es decidir no
        # crear la entidad, no borrar lo que dijo Tiziano.
        await _cerrar_tarjeta(q, "🗑 <b>Descartado</b> · sigue guardado en la bandeja")
        log.info("Descartado #%s", bandeja_id)
        return

    if accion != "ok":
        return

    # Reclamamos la fila ANTES de crear nada. Si dos toques llegan casi juntos
    # (Telegram reenvía el callback si tardamos en responder), solo el primero
    # encuentra la fila en 'esperando_confirmacion' y el segundo no crea un
    # duplicado.
    if not await db.cambiar_estado(bandeja_id, "procesado", desde="esperando_confirmacion"):
        await _cerrar_tarjeta(q, "<i>(ya estaba resuelto)</i>")
        return

    fila = await db.obtener(bandeja_id)
    if fila is None or not fila.get("interpretacion"):
        await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
        await _cerrar_tarjeta(q, "⚠️ <b>No encuentro la interpretación</b>")
        return

    try:
        tabla, registro_id = await crud.crear_desde_interpretacion(
            bandeja_id, fila["interpretacion"]
        )
    except crud.FaltanDatos as e:
        # Devolvemos la fila a su estado para que el botón siga sirviendo
        # cuando Tiziano complete el dato.
        await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
        await q.answer(
            f"Me falta {e} para poder guardarlo. Mandámelo y lo completo.",
            show_alert=True,
        )
        return
    except Exception:
        await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
        log.exception("Fallo creando la entidad de #%s", bandeja_id)
        await q.answer(
            "No pude guardarlo. Tu mensaje sigue a salvo; probá de nuevo.",
            show_alert=True,
        )
        return

    nombre = COMO_SE_LLAMA.get(tabla, tabla)
    await _cerrar_tarjeta(q, f"✅ <b>Guardado</b> · {nombre} #{registro_id}")
    log.info("Creada %s #%s desde bandeja #%s", tabla, registro_id, bandeja_id)
