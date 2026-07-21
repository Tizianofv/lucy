"""El cerebro de Lucy como agente: un bucle con herramientas y una ventana.

Tiziano, 21-jul-2026, corrigiendo la filosofía entera del diseño:

  "Construye ventanas, no muros. Ella puede usar Telegram para preguntarme
   cosas y yo le respondo. Las paredes son necesarias para construir armarios,
   tramos, cajones — para guardar las cosas — pero no para que no pueda pasar
   de un salón a otro. Necesito que tenga esa libertad."

Lo que había antes era un pasillo de salones cerrados: cada mensaje se
clasificaba hacia una puerta (charla/tarea/pregunta/orden) y detrás de cada
puerta había UNA llamada al modelo con UN prompt fijo. El modelo no podía
mirar los datos antes de decidir, ni preguntar a mitad de una acción y seguir,
ni hacer dos cosas con un mensaje. Y la repregunta era una salida de
emergencia: solo se abría cuando algo ya se había roto.

Ahora hay un solo cerebro con herramientas: mira, consulta, pregunta, actúa,
en el orden que la situación pida. La ventana (`preguntar`) es un movimiento
más, disponible siempre — manda la pregunta por Telegram, la conversación
queda esperando, y cuando Tiziano contesta se retoma con todo el contexto.
Y los últimos intercambios viajan en el contexto de cada mensaje, así que
"movelo a las 6" por fin sabe qué es "lo" (req 11).

Los muros que quedan son los de los armarios: la bandeja que captura antes
que nada, el esquema, y log_acciones registrando cada cambio con su
antes/después. Estructura para guardar, no rejas para moverse.
"""
from __future__ import annotations

import json
import logging

import acciones.botones as botones
import acciones.crud as crud
import cerebro.consultar as consultar
import cerebro.deepseek as motor
import cerebro.memoria as memoria
import db.db as db

log = logging.getLogger("lucy.agente")

# Techo de pasos por mensaje. No es un muro: es el equivalente de "si diste
# ocho vueltas y seguís perdido, pará y preguntá" — y eso es exactamente lo
# que hace al llegar acá.
MAX_PASOS = 8

# Archivar/borrar existe y está probado, pero apagado a pedido de Tiziano
# (21-jul: "ahora no quiero que borre nada pero es seguro que mañana sí").
# Encenderlo = True. La herramienta se lo dice con todas las letras en vez de
# fingir que no existe: es una decisión suya, no un límite técnico.
ARCHIVAR_HABILITADO = False

# Cuántas filas de un SELECT se le muestran al modelo. Más que esto no ayuda
# a razonar y ensancha el contexto al pedo; si necesita agregados, que agregue
# en SQL, que para eso tiene libertad total de SELECT.
MAX_FILAS_CONTEXTO = 50

HERRAMIENTAS = """\
En cada turno devolvés SOLO un objeto JSON:
  {"herramienta": "<nombre>", "argumentos": {...}}

HERRAMIENTAS DISPONIBLES:

· consultar  {"sql": "SELECT ..."}
  Mirá los datos cuando los necesites: para responder algo, para encontrar el
  registro que hay que cambiar, para verificar antes de crear. Solo lectura
  (lo garantiza la base, no un filtro). Libertad total de SELECT: CTEs,
  ventanas, agregados, lo que haga falta.

· crear  {"clasificacion": "tarea|cita|nota|idea|gasto|ingreso",
          "titulo": "...", "cuando": "ISO 8601 con offset o \\"\\"",
          "detalle": "", "duracion_min": 0, "lugar": "", "persona": "",
          "proyecto": "", "monto": 0, "moneda": "DOP", "referencia": "",
          "contraparte": ""}
  Crea la fila real. Personas y proyectos se enlazan solos por nombre.

· editar  {"tabla": "tareas|eventos|notas|movimientos|personas|proyectos",
           "id": N, "cambios": {"columna": "valor", ...}}
  Cambia algo que ya existe. Marcar hecha una tarea =
  cambios {"estado": "hecha", "completado_en": "<ahora en ISO>"}.
  Consultá antes para encontrar el id correcto: editar a ciegas es adivinar.

· perfil  {"tipo": "persona|proyecto", "nombre": "Rosi",
           "alias": ["la flaca"], "relacion": "hermana",
           "nota": "no llamarla antes de las 10", "descripcion": ""}
  Lo que sabés de la gente y los proyectos de Tiziano. Cuando él cuente algo
  de alguien ("Rosi es mi hermana", "Pedro es el contador") anotalo SIN que
  te lo pida, y confirmalo en una palabra. Es acumulativo: los alias se
  suman, las notas se agregan con fecha, nada se pisa. Mandá solo los campos
  que aprendiste ahora.

· recordar  {"texto": "lo que acordamos del depósito", "n": 5}
  Busca por SIGNIFICADO en todo lo que se han dicho (tus respuestas
  incluidas). Para "¿qué te dije de...?", "¿cuándo hablamos de...?" y todo
  lo que no se pueda nombrar con palabras exactas — ahí SQL no llega y esto
  sí. Si hace falta precisión de fechas o montos, combiná con consultar.

· archivar  {"tabla": "...", "id": N}
  Saca algo de la vista (reversible). Hoy está deshabilitada por Tiziano.

· deshacer  {"accion": N}
  Revierte una acción del log. El resultado de crear/editar te da el número.

· preguntar  {"texto": "..."}
  TU VENTANA. Le mandás eso a Tiziano por Telegram y la conversación queda
  abierta esperando su respuesta; cuando conteste, seguís con todo el
  contexto. Usála cuando falte un dato, cuando haya varios candidatos y no
  sea obvio cuál, cuando el mensaje sea ambiguo de verdad. Preguntar bien es
  mejor que adivinar rápido — pero preguntar lo obvio es ruido.

· responder  {"texto": "...",
              "clasificacion": "tarea|cita|nota|idea|gasto|ingreso|pregunta|orden|charla"}
  Tu último movimiento: le contás el resultado, o le seguís la charla. La
  clasificación es solo estadística de qué fue el mensaje.

CÓMO TRABAJÁS:
· Si estás segura, hacé y avisá. No pidas permiso: todo queda registrado y se
  puede deshacer, y Tiziano prefiere corregirte a confirmarte cada paso.
· Si dudás DE VERDAD, preguntá. El cinturón es la pregunta, no el freno.
· Un mensaje puede pedir varias cosas ("ya llamé a Ana y anotame comprar
  café"): hacelas todas antes de responder.
· Si una herramienta devuelve ERROR, leé el motivo: casi siempre dice cómo
  arreglarlo o qué preguntar. No repitas la misma llamada idéntica.
· Nunca inventes un dato que no hayas visto en un resultado.
· Respuestas breves, en su registro (español dominicano informal), texto
  plano sin markdown ni HTML. Montos: RD$ 2,300.00.
· Los mensajes que empiezan con [foto] son texto leído de una imagen que él
  te mostró — quien habla ahí NO es Tiziano (mirá el DESTINO en los
  comprobantes: si es él, la plata ENTRÓ). Los [voz] son su nota de voz
  transcripta.
· Terminá SIEMPRE con preguntar o con responder.\
"""


def _sistema() -> str:
    """El prompt de sistema se arma en cada llamada: el 'ahora' no se cachea."""
    return (
        "Sos Lucy, la asistente personal de Tiziano. Trabajás en pasos: en "
        "cada turno elegís UNA herramienta y esperás su resultado.\n\n"
        f"Ahora es {motor._ahora_txt()} (zona {motor.TZ.key}, UTC-4, sin "
        "horario de verano).\n\n"
        f"{consultar.ESQUEMA}\n\n{HERRAMIENTAS}"
    )


async def _ejecutar_herramienta(
    nombre: str, args: dict, bandeja_id: int, acciones: list[int]
) -> str:
    """Corre una herramienta y devuelve el resultado COMO TEXTO para el modelo.

    Los errores no se lanzan: se devuelven como "ERROR: ..." y el modelo decide
    qué hacer — corregir, preguntar, desistir. Un error acá es información,
    no una excepción; convertirlo en excepción sería volver a soldar la puerta
    que acabamos de abrir. (Las caídas de la API del propio modelo sí se
    propagan: esas las maneja la cola de reintentos, no el agente.)
    """
    try:
        if nombre == "consultar":
            sql = consultar._validar(str(args.get("sql") or ""))
            filas = await consultar._ejecutar(sql)
            if not filas:
                return "0 filas."
            return json.dumps(
                filas[:MAX_FILAS_CONTEXTO], default=str, ensure_ascii=False)

        if nombre == "crear":
            tabla, rid, log_id = await crud.crear_desde_interpretacion(
                bandeja_id, dict(args),
                motivo=f"Creado por Lucy desde la bandeja #{bandeja_id}")
            acciones.append(log_id)
            return f"OK: {tabla}#{rid} creado (acción #{log_id}, reversible)."

        if nombre == "editar":
            despues, log_id = await crud.editar(
                str(args.get("tabla") or ""), int(args.get("id") or 0),
                dict(args.get("cambios") or {}),
                motivo=f"Orden de Tiziano (bandeja #{bandeja_id})")
            if despues is None:
                return "ERROR: ese registro no existe o está archivado."
            acciones.append(log_id)
            return f"OK: editado (acción #{log_id}, reversible)."

        if nombre == "archivar":
            if not ARCHIVAR_HABILITADO:
                return ("ERROR: Tiziano todavía no habilitó archivar/borrar. "
                        "Decíselo: si él quiere, se enciende con una línea.")
            log_id = await crud.borrar(
                str(args.get("tabla") or ""), int(args.get("id") or 0),
                motivo=f"Orden de Tiziano (bandeja #{bandeja_id})")
            if log_id is None:
                return "ERROR: ese registro no existe o ya estaba archivado."
            acciones.append(log_id)
            return f"OK: archivado (acción #{log_id}, reversible)."

        if nombre == "deshacer":
            que = await crud.deshacer(int(args.get("accion") or 0))
            return f"OK: revertí {que}."

        if nombre == "perfil":
            resultado, log_id = await crud.perfil(
                str(args.get("tipo") or ""),
                str(args.get("nombre") or ""),
                alias=list(args.get("alias") or []),
                relacion=str(args.get("relacion") or "") or None,
                nota=str(args.get("nota") or "") or None,
                descripcion=str(args.get("descripcion") or "") or None,
                bandeja_id=bandeja_id,
            )
            if log_id:
                acciones.append(log_id)
            return resultado

        if nombre == "recordar":
            filas = await memoria.buscar(
                str(args.get("texto") or ""),
                max(1, min(int(args.get("n") or 5), 10)),
            )
            if not filas:
                return ("No encontré nada parecido en la memoria (¿quizás fue "
                        "antes de que yo existiera, o todavía no se indexó?).")
            return json.dumps(filas, default=str, ensure_ascii=False)

        return (f"ERROR: no existe la herramienta '{nombre}'. Las que hay: "
                "consultar, crear, editar, archivar, deshacer, perfil, "
                "recordar, preguntar, responder.")

    except crud.FaltanDatos as e:
        return f"ERROR: me falta {e}. Preguntáselo a Tiziano."
    except (ValueError, KeyError, TypeError) as e:
        return f"ERROR: {e}"
    except Exception as e:
        # Errores de la base (SQL malo, timeout de consulta...) también son
        # información: el mensaje de Postgres dice exactamente qué corregir.
        return f"ERROR: {type(e).__name__}: {e}"


async def _enviar(bot, text: str, **kw):
    """Envío blindado: nunca vacío, nunca más largo de lo que Telegram acepta.

    Si un envío vacío se rechazara después de dar la fila por atendida, el
    resultado sería silencio permanente (pasó con la pregunta #26). Un texto
    feo es mejor que ninguno.
    """
    limpio = (text or "").strip() or "Me quedé sin palabras — algo salió mal de mi lado."
    return await bot.send_message(text=limpio[:4000], **kw)


async def atender(fila: dict, texto: str, bot) -> None:
    """Un mensaje entra, el agente trabaja, y termina preguntando o respondiendo.

    Las excepciones de la API del modelo se propagan a propósito: la cola de
    reintentos del bucle sabe distinguir un 429 de un fallo real, y ese
    trabajo no se duplica acá.
    """
    bandeja_id = fila["id"]
    chat_id = fila["chat_id"]

    # ── Contexto: la ventana abierta (si la hay) + la memoria corta ──────
    pendiente = await db.buscar_esperando_respuesta(chat_id, bandeja_id)
    dialogo_previo = list(
        ((pendiente or {}).get("interpretacion") or {}).get("dialogo") or [])

    excluir = [bandeja_id] + ([pendiente["id"]] if pendiente else [])
    historial = await db.ultimos_intercambios(chat_id, excluir)

    mensajes: list[dict] = [{"role": "system", "content": _sistema()}]
    for h in historial:
        etiqueta = {"audio": "[voz] ", "foto": "[foto] "}.get(h["tipo_entrada"], "")
        mensajes.append({"role": "user", "content": etiqueta + h["dicho"]})
        if h["respuesta_lucy"]:
            mensajes.append({"role": "assistant", "content": h["respuesta_lucy"]})
    mensajes.extend(dialogo_previo)

    etiqueta = {"audio": "[voz] ", "foto": "[foto] "}.get(fila["tipo_entrada"], "")
    actual = {"role": "user", "content": etiqueta + texto}
    mensajes.append(actual)

    # El diálogo que se guardará si esta conversación queda esperando una
    # respuesta: arrastra lo previo para que la ventana no pierda memoria.
    dialogo = dialogo_previo + [actual]
    acciones: list[int] = []  # log_ids de lo hecho en este mensaje

    responder_kw = dict(chat_id=chat_id,
                        reply_to_message_id=fila.get("telegram_msg_id"))

    async def _cerrar_pendiente() -> None:
        if pendiente:
            await db.cambiar_estado(
                pendiente["id"], "procesado", desde="esperando_respuesta")

    for paso in range(MAX_PASOS):
        crudo = (await motor.cliente.chat.completions.create(
            model=motor.MODELO,
            messages=mensajes,
            response_format={"type": "json_object"},
            temperature=0,
        )).choices[0].message.content or ""

        turno = {"role": "assistant", "content": crudo}
        mensajes.append(turno)
        dialogo.append(turno)

        try:
            j = json.loads(crudo)
            nombre = str(j.get("herramienta") or "").strip().lower()
            args = j.get("argumentos") or {}
        except (json.JSONDecodeError, AttributeError):
            resultado = ("ERROR: eso no fue un JSON válido. Devolvé "
                         '{"herramienta": "...", "argumentos": {...}}.')
            aviso = {"role": "user", "content": f"[resultado] {resultado}"}
            mensajes.append(aviso)
            dialogo.append(aviso)
            continue

        # ── responder: el final feliz ────────────────────────────────────
        if nombre == "responder":
            salida = str(args.get("texto") or "")
            markup = botones.teclado_deshacer(acciones[-1]) if acciones else None
            await _enviar(bot, salida, reply_markup=markup, **responder_kw)
            await db.guardar_respuesta(bandeja_id, salida)
            await db.guardar_interpretacion(
                bandeja_id, str(args.get("clasificacion") or "") or None,
                {"dialogo": dialogo[-30:]}, estado="procesado")
            await _cerrar_pendiente()
            log.info("#%s resuelto en %s paso(s), %s acción(es)",
                     bandeja_id, paso + 1, len(acciones))
            return

        # ── preguntar: la ventana se abre y el turno termina ─────────────
        if nombre == "preguntar":
            salida = str(args.get("texto") or "")
            await _enviar(bot, salida, **responder_kw)
            await db.guardar_respuesta(bandeja_id, salida)
            # Mandar primero, marcar después: si el envío falla, la fila
            # vuelve a la cola en vez de quedarse esperando una respuesta a
            # una pregunta que nunca salió.
            await db.guardar_interpretacion(
                bandeja_id, None, {"dialogo": dialogo[-30:]},
                estado="esperando_respuesta")
            await _cerrar_pendiente()  # la ventana vieja la reemplaza esta
            log.info("#%s preguntó y espera respuesta (paso %s)",
                     bandeja_id, paso + 1)
            return

        # ── cualquier otra herramienta: ejecutar y seguir ────────────────
        resultado = await _ejecutar_herramienta(nombre, args, bandeja_id, acciones)
        log.info("#%s paso %s: %s -> %s",
                 bandeja_id, paso + 1, nombre, resultado[:120])
        aviso = {"role": "user", "content": f"[resultado] {resultado}"}
        mensajes.append(aviso)
        dialogo.append(aviso)

    # ── Se quedó sin pasos: eso también es "no sé" — y no sabe = pregunta ─
    salida = ("Me enredé tratando de resolver esto y prefiero no adivinar. "
              "¿Me lo decís de otra forma, o en partes?")
    await _enviar(bot, salida, **responder_kw)
    await db.guardar_respuesta(bandeja_id, salida)
    await db.guardar_interpretacion(
        bandeja_id, None, {"dialogo": dialogo[-30:]}, estado="procesado")
    await _cerrar_pendiente()
    log.warning("#%s agotó los %s pasos sin terminar", bandeja_id, MAX_PASOS)
