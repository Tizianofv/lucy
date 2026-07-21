"""El despertador: la primera vez que Lucy habla sin que le hablen.

Nació de la bandeja #50. Tiziano preguntó "¿me puedes enviar un mensaje antes
de la hora y usar eso como recordatorio?" y Lucy le contestó la verdad de su
cuerpo de entonces: "no puedo enviarte mensajes por mi cuenta, solo respondo
cuando me hablas". Era cierto — tenía oídos pero no reloj. Técnicamente era
mentira que no se pudiera: Telegram deja que el bot escriba primero. Nadie le
había construido esa parte.

Esta es esa parte. Mira el reloj cada medía vuelta del bucle y avisa una sola
vez, un rato antes, de cada tarea con hora y cada cita. `avisado_en` es lo que
garantiza el "una sola vez": el pilar de silencio inteligente aplica más que
nunca cuando Lucy es la que inicia — cada interrupción tiene que ganarse el
derecho a existir, y una alarma repetida es la forma más rápida de que las
alarmas se ignoren.

Cada aviso queda en log_acciones: cuando llegue el Nivel 7 y Tiziano pregunte
"¿por qué me escribiste a las 2:30?", la respuesta ya va a estar escrita.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg.rows import dict_row

import acciones.crud as crud
import config
import db.db as db
from config import TZ

log = logging.getLogger("lucy.despertador")

# Cuánto antes se avisa. Si la tarea se creó con menos margen que esto, el
# aviso sale igual apenas el despertador la vea: tarde es mejor que nunca,
# pero nunca después de la hora sin decir nada.
ANTICIPO_MIN = 30


async def revisar(bot) -> int:
    """Busca lo que está por vencer, avisa, y marca. Devuelve cuántos avisos.

    El orden dentro de cada aviso es el de siempre: primero MANDAR, después
    marcar avisado_en. Si el envío falla, la fila queda sin marcar y el
    próximo ciclo lo reintenta solo. Al revés, un fallo de envío se comería
    el recordatorio en silencio — que es exactamente la clase de mudez que
    este módulo vino a matar.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT 'tareas' AS tabla, id, titulo, vence_en AS cuando
              FROM tareas
             WHERE estado = 'pendiente' AND borrado_en IS NULL
               AND avisado_en IS NULL AND vence_en IS NOT NULL
               AND vence_en <= now() + make_interval(mins => %s)
            UNION ALL
            SELECT 'eventos', id, titulo, inicia_en
              FROM eventos
             WHERE borrado_en IS NULL AND avisado_en IS NULL
               AND inicia_en <= now() + make_interval(mins => %s)
             ORDER BY cuando
            """,
            (ANTICIPO_MIN, ANTICIPO_MIN),
        )
        filas = await cur.fetchall()

    avisos = 0
    for f in filas:
        faltan = round(
            (f["cuando"] - datetime.now(timezone.utc)).total_seconds() / 60)
        hora = f["cuando"].astimezone(TZ).strftime("%I:%M %p").lstrip("0")

        if faltan > 1:
            texto = f"⏰ En {faltan} min: {f['titulo']} ({hora})"
        elif faltan >= -5:
            texto = f"⏰ Ya es la hora: {f['titulo']} ({hora})"
        else:
            # Se venció mientras Lucy estaba caída o recién se enteró. Avisar
            # "en -40 min" sería ridículo; la honestidad suena distinto:
            texto = f"⏰ Ojo, esto venció a las {hora}: {f['titulo']}"

        await bot.send_message(chat_id=config.CHAT_ID_DUENO, text=texto)

        async with db.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {f['tabla']} SET avisado_en = now() WHERE id = %s",
                (f["id"],),
            )
            await crud._registrar(
                conn, accion="avisar", tabla=f["tabla"], registro_id=f["id"],
                motivo=f"Recordatorio enviado: {f['titulo']} ({hora}, "
                       f"faltaban {faltan} min)",
            )
        avisos += 1
        log.info("Aviso enviado: %s#%s (%s)", f["tabla"], f["id"], f["titulo"])

    return avisos
