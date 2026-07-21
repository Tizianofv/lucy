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
from cerebro.deepseek import ICONO

log = logging.getLogger("lucy.botones")

# Nombre humano de cada tabla, para el mensaje de confirmación.
COMO_SE_LLAMA = {
    "tareas": "tarea",
    "eventos": "cita",
    "notas": "nota",
    "movimientos": "movimiento",
}


def teclado(bandeja_id: int, alternativa: str = "") -> InlineKeyboardMarkup:
    """Los botones que van debajo de la tarjeta de interpretación.

    El bandeja_id viaja en el callback_data porque el callback llega suelto,
    sin contexto: es el único hilo que ata el botón a la fila que representa.

    Si el cerebro dudó entre dos clasificaciones, la duda aparece como un
    botón más. Es la forma barata de "preguntar": Tiziano decide con un toque
    en vez de escribir una corrección, y su elección queda en log_acciones —
    que es la materia prima con la que Lucy va a aprender sus hábitos (req 35)
    en vez de que nosotros los adivinemos hoy.
    """
    ok = InlineKeyboardButton("✅ Dale", callback_data=f"ok:{bandeja_id}")
    no = InlineKeyboardButton("🗑 Descartar", callback_data=f"no:{bandeja_id}")

    if not alternativa:
        return InlineKeyboardMarkup([[ok, no]])

    alt = InlineKeyboardButton(
        f"{ICONO.get(alternativa, '')} Mejor {alternativa}".strip(),
        callback_data=f"alt:{bandeja_id}",
    )
    return InlineKeyboardMarkup([[ok, alt], [no]])


def teclado_orden(
    bandeja_id: int, candidatos: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """Botones para confirmar un cambio sobre algo que ya existe.

    Con un solo candidato es un simple "dale". Con varios, cada uno es un
    botón: ahí la pregunta ES el cinturón. Lucy no elige por su cuenta cuál
    de tres reuniones mover — eso sería adivinar sobre datos reales, que es la
    forma más silenciosa de equivocarse.
    """
    if len(candidatos) == 1:
        filas = [[InlineKeyboardButton(
            "✅ Dale", callback_data=f"acc:{bandeja_id}:{candidatos[0][0]}")]]
    else:
        filas = [
            [InlineKeyboardButton(f"👉 {etiqueta}"[:60],
                                  callback_data=f"acc:{bandeja_id}:{rid}")]
            for rid, etiqueta in candidatos
        ]
    filas.append([InlineKeyboardButton(
        "🗑 Cancelar", callback_data=f"no:{bandeja_id}")])
    return InlineKeyboardMarkup(filas)


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
        partes = (q.data or "").split(":")
        accion = partes[0]
        bandeja_id = int(partes[1])
        # Las órdenes llevan un tercer campo: sobre qué fila hay que actuar.
        registro_id = int(partes[2]) if len(partes) > 2 else None
    except (ValueError, IndexError, AttributeError):
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

    # ── Orden: aplicar un cambio sobre algo que ya existe ───────────────
    if accion == "acc":
        if not await db.cambiar_estado(bandeja_id, "procesado",
                                       desde="esperando_confirmacion"):
            await _cerrar_tarjeta(q, "<i>(ya estaba resuelto)</i>")
            return

        fila = await db.obtener(bandeja_id)
        plan = ((fila or {}).get("interpretacion") or {}).get("plan")
        if not plan or registro_id is None:
            await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
            await _cerrar_tarjeta(q, "⚠️ <b>Perdí el plan de esa orden</b>")
            return

        motivo = (f"Orden de Tiziano (bandeja #{bandeja_id}): "
                  f"{plan.get('resumen') or plan.get('accion')}")
        try:
            if plan.get("accion") == "borrar":
                hecho = await crud.borrar(plan["tabla"], registro_id, motivo)
                remate = ("🗑 <b>Archivado</b> · se puede deshacer"
                          if hecho else "⚠️ Ya no estaba ahí")
            else:
                despues = await crud.editar(
                    plan["tabla"], registro_id, plan.get("cambios") or {}, motivo)
                remate = ("✅ <b>Hecho</b>" if despues else "⚠️ Ya no estaba ahí")
        except Exception as e:
            await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
            log.exception("Fallo aplicando la orden de #%s", bandeja_id)
            await q.answer(f"No pude aplicarlo: {e}"[:190], show_alert=True)
            return

        await _cerrar_tarjeta(q, remate)
        log.info("Orden aplicada: %s %s#%s", plan.get("accion"),
                 plan.get("tabla"), registro_id)
        return

    if accion not in ("ok", "alt"):
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

    interpretacion = dict(fila["interpretacion"])
    motivo = f"Confirmado por Tiziano desde la bandeja #{bandeja_id}"

    if accion == "alt":
        # Tiziano prefirió la segunda opción. Se deja escrito cuál se descartó:
        # ese par (lo que Lucy propuso, lo que él eligió) es exactamente lo que
        # después permite detectar el patrón y dejar de preguntar.
        elegida = interpretacion.get("alternativa")
        if not elegida:
            await db.cambiar_estado(bandeja_id, "esperando_confirmacion")
            await q.answer("Esa tarjeta no tenía alternativa.", show_alert=True)
            return
        motivo = (
            f"Tiziano eligió '{elegida}' en vez de "
            f"'{interpretacion.get('clasificacion')}' (bandeja #{bandeja_id})"
        )
        interpretacion["clasificacion"] = elegida

    try:
        tabla, registro_id = await crud.crear_desde_interpretacion(
            bandeja_id, interpretacion, motivo=motivo
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
