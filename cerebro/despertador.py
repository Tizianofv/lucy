"""El despertador: la primera vez que Lucy habla sin que le hablen.

Nació de la bandeja #50. Tiziano preguntó "¿me puedes enviar un mensaje antes
de la hora y usar eso como recordatorio?" y Lucy le contestó la verdad de su
cuerpo de entonces: "no puedo enviarte mensajes por mi cuenta, solo respondo
cuando me hablas". Era cierto — tenía oídos pero no reloj. Técnicamente era
mentira que no se pudiera: Telegram deja que el bot escriba primero. Nadie le
había construido esa parte.

Esta es esa parte. Mira el reloj cada media vuelta del bucle y avisa de cada
tarea con hora y cada cita, en los momentos que fija la columna `anticipos_min`
de CADA fila (default: {0} = una sola campanada a la hora exacta; el anticipado
es opt-in por recordatorio). `avisos_enviados` guarda qué campanadas ya
sonaron, para que cada una suene UNA sola vez: el pilar de silencio inteligente
aplica más que nunca cuando Lucy es la que inicia — cada interrupción tiene que
ganarse el derecho a existir, y una alarma repetida es la forma más rápida de
que las alarmas se ignoren.

Cada aviso queda en log_acciones: cuando llegue el Nivel 7 y Tiziano pregunte
"¿por qué me escribiste a las 2:30?", la respuesta ya va a estar escrita.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

import acciones.crud as crud
import config
import db.db as db
from cerebro.deepseek import DIAS
from config import TZ

log = logging.getLogger("lucy.despertador")

# En qué momentos se avisa, en minutos ANTES de la hora, es una decisión POR
# FILA: cada tarea/evento lleva su propia lista en la columna `anticipos_min`.
# El default de una fila nueva es {0} —una sola campanada, a la hora exacta—
# porque Tiziano lo pidió así (30-jul): el doble aviso por defecto molestaba.
# El anticipado es opt-in por recordatorio: "recordámelo 30 min antes" guarda
# {30,0}; "el día antes", {1440,0}. La maquinaria recorre la lista de CADA
# fila, así que sumar o quitar un anticipo NO toca este archivo.
#   avisos_enviados (en tareas/eventos) guarda cuáles de esos minutos-antes ya
#   salieron, así cada campanada suena una sola vez — el pilar de silencio
#   inteligente pesa doble cuando Lucy habla sin que le hablen.

# Hasta cuánto DESPUÉS de la hora sigue teniendo sentido avisar. Si Lucy estuvo
# caída y algo venció hace un rato, "ojo, esto venció a las 3" ayuda; "venció
# hace dos días" es ruido — eso ya lo sabés. Sin este tope, agregar el aviso a
# la hora haría que, tras un reinicio, cada evento viejo suelte su campanada
# retroactiva de golpe. La avalancha es justo lo que el despertador vino a evitar.
GRACIA_MIN = 120

# Cuánto antes se PREPARA una cita con lugar: la pregunta de salida ("¿desde
# dónde vas a salir?") sale con este margen, para que dé tiempo a calcular la
# hora de arrancar y crear el recordatorio de salida. Pedido de Tiziano,
# 21-jul: "si tengo una reunión a las 3, dos horas antes debería preguntarme
# dónde estoy".
PREAVISO_MIN = 120

# Piso de la ventana de preparación de salida: no tiene sentido preparar la
# salida de una cita que arranca en cinco minutos. 35' da aire para calcular la
# hora de arranque y crear el recordatorio de salida. (Antes se derivaba de
# ANTICIPO_MAX+5, que desapareció al volverse los anticipos una lista por-fila.)
SALIDA_LEAD_MIN = 35

# La ventana del briefing matinal (Nivel 5, req 24): a partir de qué hora se
# arma, y hasta cuándo tiene sentido mandarlo. Si Lucy estuvo caída toda la
# mañana, un "buenos días" a las 4 de la tarde no informa: molesta. Mejor
# saltar el día y que el de mañana salga a su hora.
BRIEFING_DESDE = 7   # 7:00 AM
BRIEFING_HASTA = 12  # mediodía

# El plan semanal (req 25, con la vuelta que le dio Tiziano): domingo a la
# tarde, mirando SOLO hacia adelante. Lucy revisa el pasado ella sola y lo
# que quedó colgado lo reubica en la semana que arranca — sin informes de
# lo que no se hizo. "No quiero que me lo informe: si faltó algo, que lo
# ponga en la nueva semana."
#
# ⚠️ HORARIO MOVIDO (1-ago-2026): era 21:00–24:00, que cae ENTERO adentro de la
# franja de tarifa doble de DeepSeek (21:00–00:00). Es el único trabajo
# automático de Lucy que gastaba IA ahí. Ahora sale a las 20:00 y la ventana
# cierra a las 21:00, justo cuando abre lo caro. Se corrió hacia ATRÁS y no
# hacia adelante a propósito: el plan es de la noche del domingo, y empujarlo
# al lunes lo volvería un informe de una semana ya empezada.
SEMANAL_DIA = 6      # domingo (weekday() de Python: lunes=0 … domingo=6)
SEMANAL_DESDE = 20   # 8:00 PM
SEMANAL_HASTA = 21   # cierra JUSTO cuando abre la tarifa doble

# El rescate: una ventana de una hora es angosta, y si Lucy está caída o
# redesplegando justo ahí el plan de la semana se perdería entero hasta el
# domingo siguiente. Diferir no puede significar perder. Así que lo que no
# salió el domingo sale el lunes temprano, ya en horario barato (las 06:00 son
# el primer minuto sin recargo). El encargo se adapta: el lunes la semana no
# "arranca mañana", arranca hoy.
SEMANAL_RESCATE_DIA = 0      # lunes
SEMANAL_RESCATE_DESDE = 6    # 6:00 AM
SEMANAL_RESCATE_HASTA = 9    # 9:00 AM: más tarde ya pisa el briefing matinal


async def _avisar(bot, texto: str) -> None:
    """Manda el aviso Y lo deja en la bandeja como parte de la conversación.

    Sin el registro, el próximo mensaje de Tiziano ("salgo del estudio")
    llegaría al agente sin la pregunta que lo causó: proactividad que rompe
    el hilo en vez de empezarlo.
    """
    await bot.send_message(chat_id=config.CHAT_ID_DUENO, text=texto[:4000])
    await db.registrar_aviso(config.CHAT_ID_DUENO, texto)


def _campanadas(anticipos, enviados, faltan: int) -> set[int]:
    """Cuáles de los minutos-antes de una fila deben sonar AHORA.

    Suena una campanada `m` si su momento ya llegó (faltan <= m) y todavía no
    salió (m not in enviados). Es la decisión pura —sin base ni Telegram—, por
    eso se puede probar sola. Si Lucy estuvo caída y el evento ya pasó, pueden
    devolverse varias de una: el bucle manda UN solo mensaje —el de la situación
    real— y da por sonadas todas las vencidas, para no soltar el "en 30 min"
    retroactivo al lado del "ya es la hora".
    """
    return {m for m in anticipos if m not in enviados and faltan <= m}


async def revisar(bot) -> int:
    """Busca lo que está por vencer, avisa, y marca. Devuelve cuántos avisos.

    El orden dentro de cada aviso es el de siempre: primero MANDAR, después
    agregar la campanada a avisos_enviados. Si el envío falla, la fila queda
    sin marcar y el próximo ciclo lo reintenta solo. Al revés, un fallo de
    envío se comería el recordatorio en silencio — que es exactamente la clase
    de mudez que este módulo vino a matar.

    La ventana de lookahead es POR FILA: cada tarea/evento se mira hasta su
    anticipo más lejano (max(anticipos_min)), no un techo global. Así un
    {1440,0} (avisar el día antes) entra a tiempo y un {0} (solo a la hora) no
    se adelanta.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            # Traemos cada fila cuando entra en la ventana de SU campanada más
            # lejana y todavía le falta al menos una por sonar. `@>` es
            # "contiene": NOT (avisos_enviados @> anticipos_min) = aún no
            # salieron todas las de esa fila.
            """
            SELECT 'tareas' AS tabla, id, titulo, vence_en AS cuando,
                   avisos_enviados, anticipos_min
              FROM tareas
             WHERE estado = 'pendiente' AND borrado_en IS NULL
               AND vence_en IS NOT NULL
               AND vence_en <= now() + make_interval(
                     mins => (SELECT COALESCE(max(m), 0) FROM unnest(anticipos_min) m))
               AND vence_en >= now() - make_interval(mins => %s)
               AND NOT (avisos_enviados @> anticipos_min)
            UNION ALL
            SELECT 'eventos', id, titulo, inicia_en,
                   avisos_enviados, anticipos_min
              FROM eventos
             WHERE borrado_en IS NULL
               AND inicia_en <= now() + make_interval(
                     mins => (SELECT COALESCE(max(m), 0) FROM unnest(anticipos_min) m))
               AND inicia_en >= now() - make_interval(mins => %s)
               AND NOT (avisos_enviados @> anticipos_min)
             ORDER BY cuando
            """,
            (GRACIA_MIN, GRACIA_MIN),
        )
        filas = await cur.fetchall()

    avisos = 0
    for f in filas:
        faltan = round(
            (f["cuando"] - datetime.now(timezone.utc)).total_seconds() / 60)
        enviados = set(f["avisos_enviados"] or ())
        anticipos = f["anticipos_min"] or [0]

        pendientes = _campanadas(anticipos, enviados, faltan)
        if not pendientes:
            continue

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

        nuevos = sorted(enviados | {m for m in anticipos if faltan <= m})
        async with db.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {f['tabla']} SET avisos_enviados = %s WHERE id = %s",
                (nuevos, f["id"]),
            )
            await crud._registrar(
                conn, accion="avisar", tabla=f["tabla"], registro_id=f["id"],
                motivo=f"Recordatorio enviado: {f['titulo']} ({hora}, "
                       f"faltaban {faltan} min)",
            )
        avisos += 1
        log.info("Aviso enviado: %s#%s (%s, faltan %s)",
                 f["tabla"], f["id"], f["titulo"], faltan)

    avisos += await _preparar_salidas(bot)
    avisos += await _briefing()
    avisos += await _semanal()
    await _reprogramar_recurrentes()
    return avisos


def _toca_semanal(ahora: datetime) -> bool:
    """¿Este momento es hora de dejar el encargo del plan semanal?

    Decisión pura (sin base ni Telegram), por eso se puede probar sola. Tres
    condiciones, en este orden:

    1. NUNCA dentro de una franja de tarifa doble de DeepSeek. Es una guarda
       dura y va primero a propósito: si mañana alguien mueve las constantes de
       arriba a un horario caro, esto lo frena igual. Una regla que solo vive
       en dos números sueltos es una regla que se pierde en el próximo ajuste.
    2. Domingo en la ventana normal (20:00–21:00), que es el caso de siempre.
    3. Lunes temprano en la ventana de rescate, para lo que no salió el domingo
       porque Lucy estuvo caída.
    """
    if config.es_horario_caro_deepseek(ahora):
        return False
    if ahora.weekday() == SEMANAL_DIA:
        return SEMANAL_DESDE <= ahora.hour < SEMANAL_HASTA
    if ahora.weekday() == SEMANAL_RESCATE_DIA:
        return SEMANAL_RESCATE_DESDE <= ahora.hour < SEMANAL_RESCATE_HASTA
    return False


# Cuánto hacia atrás se mira para saber si el plan de ESTA semana ya salió.
# Antes se miraba "desde las 00:00 de hoy", que alcanzaba cuando la única
# ventana era la del domingo. Con el rescate del lunes ya no: el encargo del
# domingo 20:00 y el rescate del lunes 06:00 son días distintos, y un corte por
# medianoche los dejaría mandar el plan dos veces. 24 horas cubre esas 10 horas
# de distancia de sobra y sigue lejísimos del plan de la semana pasada.
SEMANAL_DEDUPE_H = 24


async def _semanal() -> int:
    """Deja el encargo del plan semanal, el domingo por la noche.

    Mismo patrón que el briefing: este módulo mira el reloj, el agente
    piensa. La diferencia está en el encargo: acá Lucy primero ORDENA la
    casa ella sola (reubicar lo colgado) y recién después habla — y habla
    solo del futuro.
    """
    ahora = datetime.now(TZ)
    if not _toca_semanal(ahora):
        return 0

    desde = ahora - timedelta(hours=SEMANAL_DEDUPE_H)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT 1 FROM bandeja
             WHERE origen = 'despertador' AND tipo_entrada = 'sistema'
               AND contenido_raw LIKE 'Prepará la semana%%'
               AND creado_en >= %s
             LIMIT 1
            """,
            (desde,),
        )
        if await cur.fetchone() is not None:
            return 0

    rescate = ahora.weekday() == SEMANAL_RESCATE_DIA
    await db.guardar_en_bandeja(
        tipo_entrada="sistema",
        contenido_raw=_encargo_semanal(rescate),
        chat_id=config.CHAT_ID_DUENO,
        origen="despertador",
    )
    log.info("Encargo del plan semanal dejado en la bandeja%s.",
             " (rescate del lunes)" if rescate else "")
    return 1


def _encargo_semanal(rescate: bool = False) -> str:
    """El encargo del plan semanal. En el rescate del lunes, la semana ya arrancó.

    Es un detalle de una línea, pero de los que delatan a un robot: mandar el
    lunes a las 6 AM un "prepará la semana que arranca mañana lunes" hace que
    todo lo que siga se lea como escrito por un cron.

    Va por `{cuando}` y no por un reemplazo de texto a propósito: si alguien
    reescribe el encargo y se lleva puesto el hueco, esto revienta al toque en
    vez de mandar callado el día equivocado.
    """
    return ENCARGO_SEMANAL.format(
        cuando="arranca hoy lunes" if rescate else "arranca mañana lunes")


ENCARGO_SEMANAL = (
    "Prepará la semana que {cuando}. En DOS tiempos:\n"
    "PRIMERO, ordená la casa vos sola, sin contarle nada: consultá las "
    "tareas vencidas sin hacer y las estancadas, y a cada una que siga "
    "teniendo sentido ponele con editar una fecha nueva dentro de la semana "
    "entrante (que sume posposición está bien, para eso existe). Si alguna "
    "te parece que ya murió, no la borres callada: preguntale solo por esa.\n"
    "SEGUNDO, mandale UN mensaje mirando SOLO hacia adelante: las citas de "
    "la semana con hora y lugar (si dos chocan, decilo), lo que vence cada "
    "día, y lo que reubicaste YA INTEGRADO como parte del plan — sin decir "
    "'esto quedó de la semana pasada' ni rendirle cuentas del pasado. Tono "
    "de plan, no de informe. Si la semana está vacía, decíselo en una línea "
    "y listo."
)


# ── Recurrencia (22-jul, pedido de Tiziano) ─────────────────────────────
#
# Una tarea recurrente es UNA fila que avanza, no N filas futuras. Nació de
# ver la bandeja #86: "medicina cada 8 horas" terminó como 11 tareas
# individuales porque el esquema no tenía dónde decir "esto se repite" — y
# once filas se acaban; una regla, no.
#
# El formato de `recurrencia` es español acotado, no texto libre: lo que el
# agente escribe tiene que ser lo que este parser lee, y un formato libre
# es una promesa de desincronización.

_DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes",
                "sábado", "domingo")

_PATRONES = (
    (re.compile(r"^cada\s+(\d+)\s*horas?$"), lambda n: timedelta(hours=n)),
    (re.compile(r"^(cada\s+hora|horaria)$"), lambda n: timedelta(hours=1)),
    (re.compile(r"^(diaria|diario|cada\s+d[ií]a|todos\s+los\s+d[ií]as)$"),
     lambda n: timedelta(days=1)),
    (re.compile(r"^cada\s+(\d+)\s*d[ií]as?$"), lambda n: timedelta(days=n)),
    (re.compile(r"^(semanal|cada\s+semana)$"), lambda n: timedelta(weeks=1)),
    (re.compile(r"^cada\s+(\d+)\s*semanas?$"), lambda n: timedelta(weeks=n)),
    # "cada lunes" = semanal; el día lo ancla vence_en, no hace falta más.
    (re.compile(r"^cada\s+(?:%s)$" % "|".join(_DIAS_SEMANA)),
     lambda n: timedelta(weeks=1)),
    # Mensual como 30 días: para recordatorios personales la deriva de 1-2
    # días es aceptable y evita la aritmética de calendarios (¿31 de feb?).
    (re.compile(r"^(mensual|cada\s+mes)$"), lambda n: timedelta(days=30)),
    (re.compile(r"^cada\s+(\d+)\s*meses$"), lambda n: timedelta(days=30 * n)),
)


def _intervalo(recurrencia: str) -> timedelta | None:
    """'cada 8 horas' → timedelta(hours=8). None si no se entiende.

    Un valor ilegible NO rompe nada: la tarea simplemente no se reprograma
    y queda como tarea normal. Degradar a "una sola vez" es mejor que
    adivinar un intervalo.
    """
    texto = (recurrencia or "").strip().lower()
    for patron, fabrica in _PATRONES:
        m = patron.match(texto)
        if m:
            n = int(m.group(1)) if m.groups() and m.group(1) and m.group(1).isdigit() else 1
            return fabrica(max(1, n))
    return None


def _proxima(vence_en: datetime, intervalo: timedelta, ahora: datetime) -> datetime:
    """La próxima ocurrencia FUTURA, anclada al horario original.

    Se avanza desde vence_en y no desde 'ahora' a propósito: la medicina de
    las 23:00 marcada hecha a las 23:40 tiene que caer a las 7:00, no a las
    7:40. El ancla es el horario de la regla, no el momento del clic.
    """
    proxima = vence_en + intervalo
    while proxima <= ahora:
        proxima += intervalo
    return proxima


async def _reprogramar_recurrentes() -> int:
    """Avanza las tareas recurrentes que ya cumplieron su ocurrencia.

    Dos casos, mismo destino:
    · hecha → se reprograma al toque: el "listo" de hoy arma el aviso de
      mañana. completado_en queda en el log, no en la fila: la fila es la
      REGLA viva, la historia vive en log_acciones (como todo lo demás).
    · pendiente y vencida hace más de medio intervalo → se saltó una toma.
      Se avanza igual (con su huella diciendo que se saltó): una cadena de
      recordatorios que se corta porque un día no marcaste "hecha" es
      exactamente el tipo de silencio que el despertador vino a matar.
      El medio intervalo es la gracia para marcarla tarde sin perderla.
    """
    from psycopg.rows import dict_row as _dict_row

    ahora = datetime.now(timezone.utc)
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=_dict_row)
        await cur.execute(
            """
            SELECT * FROM tareas
             WHERE recurrencia IS NOT NULL AND borrado_en IS NULL
               AND vence_en IS NOT NULL
               AND (estado = 'hecha'
                    OR (estado = 'pendiente' AND vence_en < now()))
            """
        )
        filas = await cur.fetchall()

    avanzadas = 0
    for f in filas:
        intervalo = _intervalo(f["recurrencia"])
        if intervalo is None:
            continue  # recurrencia ilegible: queda como tarea normal

        saltada = f["estado"] == "pendiente"
        if saltada and ahora < f["vence_en"] + intervalo / 2:
            continue  # todavía en gracia: puede marcarla hecha tarde

        proxima = _proxima(f["vence_en"], intervalo, ahora)
        hora = proxima.astimezone(TZ).strftime("%d/%m %I:%M %p").lstrip("0")
        motivo = (
            f"Recurrente ({f['recurrencia']}): ocurrencia "
            + ("saltada sin marcar" if saltada else "hecha")
            + f", reprogramada para el {hora}"
        )

        async with db.pool.connection() as conn:
            cur = conn.cursor(row_factory=_dict_row)
            await cur.execute(
                """
                UPDATE tareas
                   SET estado = 'pendiente', vence_en = %s,
                       avisos_enviados = '{}', completado_en = NULL
                 WHERE id = %s
                RETURNING *
                """,
                (proxima, f["id"]),
            )
            despues = await cur.fetchone()
            await crud._registrar(
                conn, accion="editar", tabla="tareas", registro_id=f["id"],
                antes=f, despues=despues, motivo=motivo,
                bandeja_id=f.get("bandeja_id"),
            )
        avanzadas += 1
        log.info("Recurrente avanzada: tareas#%s → %s (%s)",
                 f["id"], hora, "saltada" if saltada else "hecha")

    return avanzadas


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
        "citas de HOY, con hora y lugar — y si dos se pisan en el tiempo, "
        "decilo con todas las letras; 2) las tareas que vencen hoy; 3) las "
        "atrasadas (vencieron antes de hoy y siguen pendientes); 4) las bolas "
        "que se están cayendo: tareas pospuestas 2 o más veces "
        "(pospuesta_veces) o pendientes sin fecha desde hace más de 7 días — "
        "decile cuánto llevan rodando, sin regañar. "
        "Y AL CIERRE, lo más útil de todo: POR DÓNDE EMPEZAR. Elegí las 2 o 3 "
        "cosas del día que de verdad importan y decí en qué orden encararlas, "
        "con una razón corta cada una. Cruzá urgencia (vence hoy, atrasado, "
        "choque de agenda) con importancia (prioridad alta, proyecto activo, "
        "alguien esperando). No es repetir la lista: es tu recomendación de qué "
        "mover primero y por qué. Respetá lo que Tiziano ya te dijo sobre cómo "
        "priorizar. Omití las secciones vacías sin mencionarlas. Si no hay nada "
        "de nada, un buenos días de una línea y listo. No le preguntes nada."
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

    EXENTO de la regla de tarifa doble de DeepSeek, a propósito: la hora no la
    elige Lucy, la elige la cita. Un encargo de salida aplazado hasta que baje
    la tarifa llega cuando Tiziano ya debería estar manejando — no ahorra, no
    sirve. Lo mismo vale para los recordatorios de `revisar`, que además no
    gastan IA: son texto armado acá.
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
            (SALIDA_LEAD_MIN, PREAVISO_MIN),
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
