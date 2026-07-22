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
import cerebro.viaje as viaje
import db.db as db

log = logging.getLogger("lucy.agente")

# Techo de pasos ÚTILES por mensaje (herramientas ejecutadas de verdad). No es
# un muro: es el equivalente de "si diste doce vueltas y seguís perdido, pará y
# preguntá". Una consulta legítima —buscar sucursales + ubicación + comparar
# rutas— encadena varias herramientas, así que el techo tiene que dar aire.
MAX_PASOS = 12

# Tope aparte para los TROPIEZOS: turnos en que el modelo devuelve vacío o un
# JSON inválido. DeepSeek razona antes de responder y a veces sale con la
# respuesta en blanco; eso no es un paso de trabajo, así que no gasta del
# presupuesto de arriba — pero igual tiene tope para no colgarse en un bucle.
MAX_TROPIEZOS = 6

# Archivar/borrar: habilitado por Tiziano el 22-jul ("Habilitalo"). Estuvo
# apagado desde el 21-jul ("ahora no quiero que borre nada pero es seguro que
# mañana sí") — y efectivamente fue mañana. Apagarlo de nuevo = False; el
# resto del circuito (soft-delete + antes en log_acciones + deshacer) no
# cambia con el flag.
ARCHIVAR_HABILITADO = True

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
          "recurrencia": "", "detalle": "", "duracion_min": 0, "lugar": "",
          "persona": "", "proyecto": "", "monto": 0, "moneda": "DOP",
          "referencia": "", "contraparte": ""}
  Crea la fila real. Personas y proyectos se enlazan solos por nombre.
  RECURRENCIA (solo tareas): si algo se repite ("la medicina cada 8 horas",
  "sacar la basura los lunes"), es UNA tarea con "recurrencia" — NUNCA
  varias copias a futuro. Formatos que entiende la maquinaria (usá estos,
  literal): "cada N horas", "diaria", "cada N días", "semanal",
  "cada N semanas", "cada lunes"…"cada domingo", "mensual", "cada N meses".
  Necesita "cuando" (la primera ocurrencia, con hora); si falta, pedila.
  Al marcarse hecha se reprograma sola a la próxima — y si una ocurrencia
  pasa sin marcarse, también avanza sola: la cadena no se corta.
  "Ya no tomo más esa medicina" = editar {"recurrencia": null,
  "estado": "hecha"}. Cambiar el horario = editar vence_en (la regla se
  ancla ahí).

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

· ubicacion  {}
  Dónde está Tiziano según su última ubicación COMPARTIDA: coordenadas, hace
  cuántos minutos, y el lugar con nombre en el que cae. OJO — no es un GPS en
  vivo: es la última vez que él compartió su posición por Telegram.
   · Fresca (menos de ~30 min): usála sin preguntar.
   · Vieja o inexistente: NO la des como su posición actual. Decile desde
     cuándo es y pedile que comparta su ubicación. Y si necesita que lo sepas
     mientras se mueve (camino a algún lado), explicale que puede mandar
     "Ubicación en tiempo real": 📎 Adjuntar → Ubicación → "Compartir mi
     ubicación en tiempo real" → elegí el tiempo. Así te llega sola y no te
     la vuelve a preguntar mientras dure.

· lugar  {"nombre": "el estudio", "lat": 0, "lon": 0, "radio_m": 300}
  Nombra un lugar de su mundo. Sin lat/lon usa su última ubicación: cuando
  diga "estoy en el estudio" (y haya pin reciente) o "guardá este lugar
  como X", esta es la herramienta. Los lugares con nombre son lo que vuelve
  útil la ubicación.

· buscar_lugar  {"texto": "la sirena"}
  Busca un lugar por nombre en Google Maps y devuelve varios candidatos, cada
  uno con nombre, dirección y coordenadas (lat/lon). Es tu forma de ubicar un
  sitio que Tiziano no tiene guardado. Cómo usar el resultado:
   · UN candidato claro → seguí con él (viaje con sus lat/lon).
   · VARIOS que podrían ser → NO adivines: mostrale los nombres+direcciones y
     preguntale cuál. "La Sirena" tiene cuatro sucursales; elegir por él es la
     forma más silenciosa de mandarlo al lugar equivocado.
   · NINGUNO → pedile más detalle (sector, avenida).
  Cuando confirme cuál es, ofrecé guardarlo con `lugar` (pasando su lat/lon)
  para no volver a preguntar.
  "¿CUÁL ME QUEDA MÁS CERCA?" NO es una pregunta para devolverle: es trabajo
  tuyo. buscar_lugar te da las sucursales con coordenadas y `ubicacion` te da
  dónde está él; compará y respondé cuál es la más cercana con su tiempo. Y
  NUNCA listes sucursales de memoria: las de verdad salen de buscar_lugar,
  las inventadas lo mandan al lugar equivocado.

· viaje  {"destino": "", "desde": "", "dest_lat": 0, "dest_lon": 0}
  Cuánto se tarda AHORA, con el tráfico real. Sin "desde", parte de su última
  ubicación. Preferila a las rutas guardadas: el tráfico de hoy le gana a la
  memoria de la semana pasada.
  El destino, en orden de preferencia:
   1. dest_lat/dest_lon → coordenadas exactas (de buscar_lugar o de un lugar
      guardado). SIN AMBIGÜEDAD posible: es la mejor opción.
   2. destino = nombre de un lugar GUARDADO (consultá `lugares`): resuelve por
      sus coordenadas.
   3. destino = texto libre: ÚLTIMO recurso; Google geocodifica y puede
      elegir mal. Si vas a caer acá para un lugar conocido, mejor buscar_lugar
      primero.

· recordar  {"texto": "lo que acordamos del depósito", "n": 5}
  Busca por SIGNIFICADO en todo lo que se han dicho (tus respuestas
  incluidas). Para "¿qué te dije de...?", "¿cuándo hablamos de...?" y todo
  lo que no se pueda nombrar con palabras exactas — ahí SQL no llega y esto
  sí. Si hace falta precisión de fechas o montos, combiná con consultar.

· archivar  {"tabla": "...", "id": N}
  Saca algo de la vista cuando él pida borrar/archivar/descartar algo que ya
  existe. Es reversible (soft-delete; deshacer lo revive), así que no pidas
  permiso si la orden es clara — pero consultá antes para dar con el id
  correcto, y si hay varios candidatos preguntá cuál, como con editar.

· deshacer  {"accion": N}
  Revierte una acción del log. El resultado de crear/editar te da el número.

· preguntar  {"texto": "..."}
  TU VENTANA. Le mandás eso a Tiziano por Telegram y la conversación queda
  abierta esperando su respuesta; cuando conteste, seguís con todo el
  contexto. Usála cuando falte un dato, cuando haya varios candidatos y no
  sea obvio cuál, cuando el mensaje sea ambiguo de verdad. Preguntar bien es
  mejor que adivinar rápido — pero preguntar lo obvio es ruido.
  Tiziano SIEMPRE está del otro lado (lo dijo él, con esas palabras): ante
  cualquier duda real, preferí preguntarle antes que trabarte o adivinar.

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
· DIRECCIONES: cuando te dé la dirección de un lugar suyo ("mi casa es
  Capitán Eugenio de Marchena #5"), guardala al instante como nota con
  etiquetas ["direccion"] (ej: "Casa: Capitán Eugenio de Marchena #5,
  Santo Domingo") — sin que te lo pida. La próxima vez que necesites esa
  dirección para viaje, consultala en vez de volver a preguntar. Preguntar
  dos veces la misma dirección es no haber escuchado la primera.
· TENÉS DESPERTADOR: una parte automática tuya le manda un aviso por
  Telegram ~30 minutos antes de cada tarea con hora y de cada cita, una sola
  vez. Así que si te pregunta "¿me lo vas a recordar?": SÍ, siempre que la
  tarea o la cita tenga su hora puesta. Si no la tiene, pedísela y editála.
  Con las recurrentes el aviso se rearma solo en cada ocurrencia: "¿me lo
  vas a recordar siempre?" también es SÍ.
· TENÉS BRIEFING MATINAL: cada mañana (~7:00) tu maquinaria te deja el
  encargo de armarle el resumen del día en UN solo mensaje. Si te pregunta
  "¿me podés dar un resumen cada mañana?": SÍ, ya lo hacés solo.
· Los mensajes [sistema] son ENCARGOS DE TU PROPIA MAQUINARIA (el
  despertador), no de Tiziano. Hacé el trabajo con tus herramientas y usá
  responder para decirle a él SOLO el resultado útil — o preguntar si de
  verdad falta algo.
· SALIDAS (el encargo [sistema] típico): ~2h antes de una cita CON LUGAR te
  llega "prepará la salida". La regla de oro: RESOLVÉ CON LO QUE YA SABÉS y
  preguntá únicamente lo que falte de verdad:
   1. ¿Desde dónde sale? → ubicacion. Fresca y con lugar con nombre: listo.
      Vieja, sin lugar o sin datos: preguntale.
   2. ¿Cuánto tarda? → viaje (tráfico real de ahora). Si viaje da ERROR:
      consultar notas con etiquetas @> ARRAY['ruta']; y si tampoco hay,
      preguntáselo UNA vez y guardalo con crear (nota, etiquetas ["ruta"]).
   3. Creá la tarea "Salir para {la cita}" con vence_en = hora de la cita
      menos el viaje menos 10 min de colchón, y avisale en una línea:
      "Salí 2:05 desde CDS para llegar a las 3". El despertador la recuerda.
  Con todo a mano, CERO preguntas: ese es el estándar.
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


async def _avisar_choques(evento_id: int) -> str:
    """Texto informativo si el evento se pisa con otro, o "" si está limpio.

    Es una ventana, no un muro: no bloquea nada ni decide nada. Le acerca el
    dato a Lucy en el mismo turno y ella elige el movimiento — avisar,
    proponer mover una, o preguntarle a Tiziano. La casa le alcanza la
    información donde la necesita; qué hacer con ella es asunto suyo.
    """
    choques = await db.choques_de_evento(evento_id)
    if not choques:
        return ""
    partes = []
    for c in choques[:3]:
        hora = c["inicia_rd"].strftime("%d/%m %I:%M %p").lstrip("0")
        lugar = f" en {c['lugar']}" if c.get("lugar") else ""
        partes.append(f"«{c['titulo']}»{lugar} ({hora})")
    return (" OJO — CHOQUE DE AGENDA: se pisa con " + "; ".join(partes) +
            ". Avisale a Tiziano en tu respuesta y, si él quiere, movés una.")


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
            resultado = f"OK: {tabla}#{rid} creado (acción #{log_id}, reversible)."
            if tabla == "eventos":
                resultado += await _avisar_choques(rid)
            return resultado

        if nombre == "editar":
            tabla = str(args.get("tabla") or "")
            cambios = dict(args.get("cambios") or {})
            despues, log_id = await crud.editar(
                tabla, int(args.get("id") or 0), cambios,
                motivo=f"Orden de Tiziano (bandeja #{bandeja_id})")
            if despues is None:
                return "ERROR: ese registro no existe o está archivado."
            acciones.append(log_id)
            resultado = f"OK: editado (acción #{log_id}, reversible)."
            # Mover una cita puede crear un choque que antes no existía: la
            # casa le acerca el dato acá, en el momento en que aparece.
            if tabla == "eventos" and ("inicia_en" in cambios or "termina_en" in cambios):
                resultado += await _avisar_choques(int(args.get("id") or 0))
            return resultado

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

        if nombre == "ubicacion":
            u = await db.ultima_ubicacion()
            if u is None:
                return ("No hay ninguna ubicación compartida todavía. "
                        "Habría que pedirle un pin.")
            donde = f"dentro de '{u['lugar']}'" if u["lugar"] else \
                "en un punto sin lugar con nombre"
            return (f"Hace {u['hace_min']} min estaba {donde} "
                    f"(lat {u['lat']:.5f}, lon {u['lon']:.5f}"
                    f"{', ubicación en vivo' if u['en_vivo'] else ''}).")

        if nombre == "lugar":
            resultado, log_id = await crud.guardar_lugar(
                str(args.get("nombre") or ""),
                lat=args.get("lat") or None,
                lon=args.get("lon") or None,
                radio_m=args.get("radio_m") or None,
            )
            if log_id:
                acciones.append(log_id)
            return resultado

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

        if nombre == "buscar_lugar":
            cands = await viaje.buscar_lugares(str(args.get("texto") or ""))
            if not cands:
                return ("No encontré ese lugar. Pedile más detalle (sector, "
                        "avenida) o que comparta la ubicación.")
            return json.dumps(cands, ensure_ascii=False)

        if nombre == "viaje":
            return await viaje.calcular(
                destino=str(args.get("destino") or "") or None,
                desde=str(args.get("desde") or "") or None,
                dest_lat=args.get("dest_lat") or None,
                dest_lon=args.get("dest_lon") or None,
            )

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
                "ubicacion, lugar, buscar_lugar, viaje, recordar, preguntar, "
                "responder.")

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
        # Una fila puede ser solo de Lucy (un aviso del despertador: sin
        # dicho). Entra igual: sus palabras proactivas son parte del hilo.
        if h["dicho"]:
            etiqueta = {"audio": "[voz] ", "foto": "[foto] ",
                        "sistema": "[sistema] "}.get(h["tipo_entrada"], "")
            mensajes.append({"role": "user", "content": etiqueta + h["dicho"]})
        if h["respuesta_lucy"]:
            mensajes.append({"role": "assistant", "content": h["respuesta_lucy"]})
    mensajes.extend(dialogo_previo)

    etiqueta = {"audio": "[voz] ", "foto": "[foto] ",
                "sistema": "[sistema] "}.get(fila["tipo_entrada"], "")
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

    pasos = 0       # herramientas ejecutadas de verdad
    tropiezos = 0   # turnos vacíos o mal formados: no cuentan como paso
    while pasos < MAX_PASOS and tropiezos < MAX_TROPIEZOS:
        crudo = (await motor.cliente.chat.completions.create(
            model=motor.MODELO,
            messages=mensajes,
            response_format={"type": "json_object"},
            temperature=0,
        )).choices[0].message.content or ""

        # Vacío: DeepSeek razonó y no escribió nada. NO lo metemos al contexto
        # —verse a sí mismo en blanco lo confunde y encadena más vacíos— y lo
        # empujamos a elegir una herramienta. Tropiezo, no paso.
        if not crudo.strip():
            tropiezos += 1
            aviso = {"role": "user", "content":
                     '[resultado] Devolviste vacío. Elegí UNA herramienta y '
                     'respondé SOLO el JSON {"herramienta":"...","argumentos":{...}}.'}
            mensajes.append(aviso)
            dialogo.append(aviso)
            continue

        turno = {"role": "assistant", "content": crudo}
        mensajes.append(turno)
        dialogo.append(turno)

        try:
            j = json.loads(crudo)
            nombre = str(j.get("herramienta") or "").strip().lower()
            # Tolerancia: el modelo a veces APLANA el JSON —pone los argumentos
            # al nivel de arriba en vez de dentro de "argumentos"— y así una
            # pregunta suya quedaba con texto vacío y se perdía (pasó con la
            # #70). Si "argumentos" no vino como dict con contenido, tomamos el
            # resto de las claves como argumentos.
            args = j.get("argumentos")
            if not isinstance(args, dict) or not args:
                args = {k: v for k, v in j.items()
                        if k not in ("herramienta", "argumentos")}
        except (json.JSONDecodeError, AttributeError):
            # Vacío o mal formado: tropiezo, no paso. Se le pide de nuevo sin
            # cobrarle del presupuesto de trabajo.
            tropiezos += 1
            resultado = ("ERROR: devolviste vacío o inválido. Respondé SOLO el "
                         'JSON {"herramienta": "...", "argumentos": {...}}.')
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
                     bandeja_id, pasos, len(acciones))
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
                     bandeja_id, pasos)
            return

        # ── cualquier otra herramienta: ejecutar y seguir ────────────────
        resultado = await _ejecutar_herramienta(nombre, args, bandeja_id, acciones)
        pasos += 1
        log.info("#%s paso %s: %s -> %s",
                 bandeja_id, pasos, nombre, resultado[:120])
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
