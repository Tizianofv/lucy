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
from cerebro.deepseek import DIAS
from config import TZ

log = logging.getLogger("lucy.despertador")

# Cuánto antes se avisa. Si la tarea se creó con menos margen que esto, el
# aviso sale igual apenas el despertador la vea: tarde es mejor que nunca,
# pero nunca después de la hora sin decir nada.
ANTICIPO_MIN = 30

# Cuánto antes se PREPARA una cita con lugar: la pregunta de salida ("¿desde
# dónde vas a salir?") sale con este margen, para que dé tiempo a calcular la
# hora de arrancar y crear el recordatorio de salida. Pedido de Tiziano,
# 21-jul: "si tengo una reunión a las 3, dos horas antes debería preguntarme
# dónde estoy".
PREAVISO_MIN = 120

# La ventana del briefing matinal (Nivel 5, req 24): a partir de qué hora se
# arma, y hasta cuándo tiene sentido mandarlo. Si Lucy estuvo caída toda la
# mañana, un "buenos días" a las 4 de la tarde no informa: molesta. Mejor
# saltar el día y que el de mañana salga a su hora.
BRIEFING_DESDE = 7   # 7:00 AM
BRIEFING_HASTA = 12  # mediodía


async def _avisar(bot, texto: str) -> None:
    """Manda el aviso Y lo deja en la bandeja como parte de la conversación.

    Sin el registro, el próximo mensaje de Tiziano ("salgo del estudio")
    llegaría al agente sin la pregunta que lo causó: proactividad que rompe
    el hilo en vez de empezarlo.
    """
    await bot.send_message(chat_id=config.CHAT_ID_DUENO, text=texto[:4000])
    await db.registrar_aviso(config.CHAT_ID_DUENO, texto)


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

        await _avisar(bot, texto)

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

    avisos += await _preparar_salidas(bot)
    avisos += await _briefing()
    return avisos


async def _briefing() -> int:
    """Deja el encargo del briefing una vez por día, en la ventana matinal.

    Mismo patrón que las salidas: este módulo solo mira el reloj, el agente
    piensa. El encargo cae en la bandeja como [sistema], y el agente consulta
    la agenda REAL y redacta el resumen — acá no se arma ningún texto de
    briefing, porque armarlo sin mirar los datos sería opinar sin consultar.

    La marca de "hoy ya salió" es la propia fila de la bandeja: no hace falta
    una tabla nueva para recordar un hecho que la bandeja ya registra.
    """
    ahora = datetime.now(TZ)
    if not (BRIEFING_DESDE <= ahora.hour < BRIEFING_HASTA):
        return 0

    hoy_arranca = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT 1 FROM bandeja
             WHERE origen = 'despertador' AND tipo_entrada = 'sistema'
               AND contenido_raw LIKE 'Prepará el briefing%%'
               AND creado_en >= %s
             LIMIT 1
            """,
            (hoy_arranca,),
        )
        if await cur.fetchone() is not None:
            return 0

    fecha = f"{DIAS[ahora.weekday()]} {ahora.strftime('%d/%m/%Y')}"
    encargo = (
        f"Prepará el briefing matinal de hoy, {fecha}. Consultá la base y "
        "armá UN solo mensaje breve y ordenado con lo que aplique: 1) las "
        "citas de HOY, con hora y lugar; 2) las tareas que vencen hoy; 3) las "
        "atrasadas (vencieron antes de hoy y siguen pendientes); 4) las "
        "pospuestas más de una vez, si las hay. Omití las secciones vacías "
        "sin mencionarlas. Si no hay nada de nada, un buenos días de una "
        "línea y listo. No le preguntes nada."
    )
    await db.guardar_en_bandeja(
        tipo_entrada="sistema",
        contenido_raw=encargo,
        chat_id=config.CHAT_ID_DUENO,
        origen="despertador",
    )
    log.info("Encargo del briefing matinal dejado en la bandeja.")
    return 1


async def _preparar_salidas(bot) -> int:
    """Citas con lugar se preparan ~2h antes (reqs 22/26) — SIN preguntar.

    El despertador no le habla a Tiziano: le deja un ENCARGO al agente en la
    bandeja (tipo 'sistema'). El agente lo toma como cualquier mensaje, junta
    lo que ya sabe —última ubicación, lugares con nombre, rutas guardadas—
    y le habla a Tiziano una sola vez, con la respuesta: "salí 2:05 desde
    CDS". Solo pregunta si de verdad le falta algo. Pedido explícito de
    Tiziano (21-jul): "debería decírmelo por su cuenta, sin que yo tenga que
    responder ninguna pregunta; si le falta algo, que pregunte como siempre".

    Cada pieza en su oficio: este módulo mira el reloj, el agente piensa.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT id, titulo, lugar, inicia_en
              FROM eventos
             WHERE borrado_en IS NULL AND preaviso_en IS NULL
               AND lugar IS NOT NULL AND lugar <> ''
               AND inicia_en >  now() + make_interval(mins => %s)
               AND inicia_en <= now() + make_interval(mins => %s)
             ORDER BY inicia_en
            """,
            (ANTICIPO_MIN + 5, PREAVISO_MIN),
        )
        filas = await cur.fetchall()

    for f in filas:
        hora = f["inicia_en"].astimezone(TZ).strftime("%I:%M %p").lstrip("0")
        encargo = (
            f"Prepará la salida para la cita #{f['id']}: «{f['titulo']}» en "
            f"{f['lugar']} a las {hora}. Averiguá desde dónde sale (ubicacion "
            f"/ lugares) y cuánto tarda (notas con etiqueta 'ruta'); creá la "
            f"tarea «Salir para {f['titulo']}» a la hora correcta y avisale "
            f"en una línea. Si tenés todo, no le preguntes nada."
        )
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=encargo,
            chat_id=config.CHAT_ID_DUENO,
            origen="despertador",
        )

        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE eventos SET preaviso_en = now() WHERE id = %s", (f["id"],))
            await crud._registrar(
                conn, accion="avisar", tabla="eventos", registro_id=f["id"],
                motivo=f"Encargo de salida al agente: {f['titulo']} en "
                       f"{f['lugar']} ({hora})",
            )
        log.info("Encargo de salida: eventos#%s (%s)", f["id"], f["titulo"])

    return len(filas)
