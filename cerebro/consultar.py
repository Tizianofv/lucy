"""Consultas en lenguaje natural sobre los datos de Tiziano (req 10).

FILOSOFÍA — decidida con Tiziano el 2026-07-21, vale la pena dejarla escrita:

Lucy es suya y tiene que tener libertad. No hay una lista blanca de preguntas
permitidas ni un catálogo de consultas prefabricadas: se le enseña el esquema
completo y escribe el SQL que se le ocurra. Restringir QUÉ puede preguntar la
dejaría corta en un mes, y después habría que desmontar la restricción y
enseñarle igual. Mejor enseñarle desde el principio.

Lo único que no puede hacer es ESCRIBIR, y eso no le quita capacidad: no
existe una pregunta sobre estos datos que necesite un DELETE para responderse.
Es al revés — es esa garantía la que permite dejarla intentar consultas raras
sin pedirle permiso a Tiziano cada vez. Escribir tiene su propio camino, con
botones y confirmación; una pregunta no puede saltárselo.

El candado lo aplica Postgres (transacción READ ONLY), no un filtro de strings
de este lado. Un filtro se le escapa algo; el servidor no.

Y ante la duda, PREGUNTA. Una respuesta segura y equivocada sobre cuánto
gastaste es peor que un "¿te referís a este mes o a los últimos 30 días?".
"""
from __future__ import annotations

import json
import logging

from psycopg.rows import dict_row

import db.db as db
from cerebro.deepseek import MODELO, TZ, _ahora_txt, cliente

log = logging.getLogger("lucy.consultar")

# Techos de sensatez, no de capacidad: evitan que una consulta mal armada
# cuelgue el bucle o traiga media base a la memoria.
LIMITE_FILAS = 200
TIMEOUT_SQL = "10s"

ESQUEMA = """\
TABLAS (PostgreSQL). Todas las fechas son timestamptz salvo aviso.

bandeja — todo lo que Tiziano le mandó a Lucy, crudo. Es el historial completo.
  id, creado_en, origen, tipo_entrada ('texto'|'audio'|'foto'),
  contenido_raw (lo que escribió), transcripcion (lo que Whisper oyó o lo que
  se leyó en la foto), estado, clasificacion, interpretacion (jsonb),
  procesado_en
  · estado: 'sin_procesar'|'procesando'|'esperando_confirmacion'|'procesado'|
            'descartado'|'error'

tareas — cosas por hacer.
  id, bandeja_id, creado_en, titulo, detalle, vence_en, prioridad,
  proyecto_id, persona_id, estado ('pendiente'|'hecha'|'pospuesta'),
  pospuesta_veces, completado_en, borrado_en

eventos — citas y compromisos con hora.
  id, bandeja_id, creado_en, titulo, inicia_en, termina_en, lugar,
  persona_id, proyecto_id, notas, borrado_en

notas — información guardada sin acción asociada.
  id, bandeja_id, creado_en, contenido, etiquetas (text[]; 'idea' marca las
  ideas), proyecto_id, persona_id, borrado_en

movimientos — TODA la plata, entre o salga.
  id, bandeja_id, creado_en, tipo ('gasto'|'ingreso'|'transferencia'),
  fecha (DATE), monto (numeric), moneda (normalmente 'DOP'), contraparte
  (el comercio si salió, quién pagó si entró), categoria, referencia
  (No. de comprobante), persona_id, proyecto_id, notas, borrado_en

personas — gente de su vida.
  id, creado_en, nombre, alias (text[]), relacion, notas, borrado_en

lugares — los lugares con nombre de su vida ("CDS", "el estudio", "casa").
  id, creado_en, nombre, lat, lon, radio_m, borrado_en

ubicaciones — cada pin o latido de ubicación que compartió por Telegram.
  id, ts, lat, lon, en_vivo. La última fila es dónde está (preferí la
  herramienta `ubicacion`, que ya calcula la edad y el lugar con nombre).

proyectos — id, creado_en, nombre, descripcion, estado, borrado_en

log_acciones — todo lo que Lucy hizo, con el antes y el después.
  id, ts, actor, accion, tabla, registro_id, antes (jsonb), despues (jsonb),
  motivo, bandeja_id

MODISMOS DE LA CASA — respetarlos o las respuestas van a ser falsas:

1. BORRADO SUAVE. Nada se borra de verdad. tareas, eventos, notas,
   movimientos, personas y proyectos tienen borrado_en: si NO es NULL, esa
   fila está borrada y NO debe contarse. Filtrá SIEMPRE con
   "borrado_en IS NULL", salvo que te pregunten explícitamente por lo borrado.

2. EL MONTO SIEMPRE ES POSITIVO. La dirección la da `tipo`. Para un balance:
   sum(monto) FILTER (WHERE tipo='ingreso') - sum(monto) FILTER (WHERE tipo='gasto')
   Nunca asumas que un gasto viene con signo negativo, porque no viene.

3. PERSONAS POR NOMBRE O ALIAS. Para encontrar a alguien mencionado por su
   nombre, mirá también los alias:
   WHERE lower(nombre)=lower('Ana') OR lower('Ana')=ANY(SELECT lower(a) FROM unnest(alias) a)

4. HORA LOCAL. Las columnas timestamptz se guardan en UTC. Para razonar sobre
   "hoy", "esta semana" o la hora del día, convertí primero:
   (vence_en AT TIME ZONE 'America/Santo_Domingo')
   `movimientos.fecha` ya es DATE local, esa no se convierte.

5. UNA TAREA PENDIENTE es estado='pendiente' AND borrado_en IS NULL.\
"""

INSTRUCCIONES_SQL = """\
Sos la parte de Lucy que consulta la base de datos de Tiziano para responderle.

Ahora es {ahora} (zona {zona}, UTC-4, sin horario de verano).

{esquema}

Recibís su pregunta y devolvés SOLO un objeto JSON con estas claves:
  sql: una ÚNICA sentencia SELECT (puede empezar con WITH) que responda la
       pregunta. Sin punto y coma al final. "" si vas a pedir una aclaración.
  aclaracion: si la pregunta es genuinamente ambigua y una interpretación
       equivocada daría un número falso, escribí acá la repregunta corta que
       le harías. "" si no hace falta.
  explicacion: en una frase y en criollo, qué es lo que fuiste a buscar.

REGLAS:
· Solo lectura. Nada de INSERT, UPDATE, DELETE, DDL ni funciones que escriban.
· Tenés libertad total para el SELECT: uniones, CTEs, ventanas, agregados,
  generate_series, lo que haga falta. Si la pregunta es rara, armá la consulta
  rara. Nadie te limitó a un catálogo.
· Preferí devolver pocas filas y ya resumidas (contar, sumar, agrupar) antes
  que traer todo y que se resuma después.
· Poné alias en español a las columnas: se las va a leer una persona.
· Si la pregunta NO se puede responder con estos datos, dejá sql en "" y
  explicá en `aclaracion` qué es lo que Lucy todavía no guarda.
· ANTE LA DUDA, PREGUNTÁ. Una respuesta segura y equivocada sobre su plata o
  su agenda es peor que una repregunta. Pero no preguntes por deporte: si el
  sentido común alcanza, resolvé y aclaralo después en la explicación.\
"""

INSTRUCCIONES_RESPUESTA = """\
Sos Lucy. Le preguntaste algo a la base y volvió este resultado. Contestale a
Tiziano en español rioplatense/dominicano, breve y natural, como una asistente
que ya miró y le cuenta — no como un informe.

Ahora es {ahora}.

Reglas:
· Respondé la pregunta de una. Nada de "según los datos consultados".
· Los montos en DOP se escriben así: RD$ 2,300.00
· Si no vino ninguna fila, decilo simple ("no tenés nada anotado para mañana"),
  sin disculpas largas.
· Si el resultado es una lista, usá viñetas cortas con "·".
· NO inventes ni un dato que no esté en el resultado. Si el resultado parece
  incompleto, decilo.
· TEXTO PLANO. Ni markdown ni HTML: se manda tal cual por Telegram, y un "<"
  suelto en un nombre haría que el mensaje entero se rechace.\
"""


def _validar(sql: str) -> str:
    """Red de contención liviana. La barrera real es la transacción READ ONLY.

    Esto no está para atajar a un atacante —no hay atacante, el SQL lo escribe
    el propio cerebro de Lucy— sino para cortar temprano un error obvio y dar
    un mensaje claro en vez de un fallo raro de Postgres.

    Y es liviana a propósito. En las pruebas, un "WITH x AS (...) INSERT ..."
    pasó estas comprobaciones sin despeinarse: empieza con WITH y es una sola
    sentencia. Postgres lo rechazó igual. Esa es la lección — un filtro de
    strings siempre tiene el agujero que no se te ocurrió.

    Por eso NO hay lista negra de palabras (insert, update, delete...): daría
    falsos positivos en preguntas legítimas —buscar "delete" en una nota,
    consultar log_acciones donde accion='borrar'— y cada falso positivo es una
    pregunta que Lucy deja de saber responder. Sería un candado sobre su
    capacidad para cubrir algo que ya está cubierto sin costo.
    """
    limpio = sql.strip().rstrip(";").strip()
    if not limpio:
        raise ValueError("Vino sin consulta.")
    if ";" in limpio:
        raise ValueError("Más de una sentencia en la misma consulta.")
    if not limpio.lower().startswith(("select", "with")):
        raise ValueError("La consulta no empieza con SELECT ni WITH.")
    return limpio


async def _ejecutar(sql: str) -> list[dict]:
    """Corre el SELECT dentro de una transacción de SOLO LECTURA.

    El READ ONLY lo hace cumplir Postgres: si algo se colara e intentara
    escribir, el servidor lo rechaza. No dependemos de haber sabido prever
    todas las formas de escribir que existen.
    """
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(f"SET LOCAL statement_timeout = '{TIMEOUT_SQL}'")
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql)
            return await cur.fetchmany(LIMITE_FILAS)


async def _corregir(pregunta: str, sql: str, error: str) -> str:
    """Segunda oportunidad: se le muestra el error de Postgres y lo arregla.

    Se le devuelve la conversación completa —su propia consulta y el rechazo—
    porque el modelo corrige mucho mejor viendo qué escribió que recibiendo el
    pedido de cero.
    """
    r = json.loads((await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES_SQL.format(
                ahora=_ahora_txt(), zona=TZ.key, esquema=ESQUEMA)},
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": json.dumps({"sql": sql})},
            {"role": "user", "content":
                f"Postgres rechazó esa consulta con este error:\n{error}\n\n"
                f"Corregila y devolvé el mismo JSON con el sql arreglado."},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )).choices[0].message.content)
    return str(r.get("sql") or "")


def _crudo(filas: list[dict]) -> str:
    """El resultado tal cual, sin redactar. Fea pero verdadera."""
    if not filas:
        return "No encontré nada."
    if len(filas) == 1 and len(filas[0]) == 1:
        valor = next(iter(filas[0].values()))
        # Un booleano suelto es la respuesta a un "¿tengo...?". Devolver
        # "False" sería contestar en jerga de base de datos.
        if isinstance(valor, bool):
            return "Sí." if valor else "No."
        return str(valor)
    return "\n".join(
        "· " + " · ".join(f"{k}: {v}" for k, v in f.items() if v is not None)
        for f in filas[:20]
    )


def _redactar_o_crudo(respuesta, filas: list[dict]) -> str:
    """Devuelve la redacción del modelo, o el resultado crudo si vino vacía.

    Esto no es paranoia: pasó de verdad. DeepSeek v4-flash razona antes de
    responder, y esta es la única llamada sin modo JSON. Volvió con `content`
    vacío, Telegram rechazó el mensaje vacío, y como la fila ya estaba marcada
    como procesada, la pregunta de Tiziano murió en un silencio perfecto.

    Una respuesta fea siempre es mejor que ninguna.
    """
    contenido = (respuesta.choices[0].message.content or "").strip()
    if contenido:
        return contenido
    log.warning("El modelo no redactó nada; devuelvo el resultado crudo.")
    return _crudo(filas)


async def responder(pregunta: str) -> dict:
    """Pregunta en criollo → respuesta en criollo.

    Devuelve {'texto': str, 'sql': str|None, 'explicacion': str|None}. El sql
    se devuelve para poder mostrarlo si Tiziano pregunta por qué contestó eso
    (req 36): una respuesta que no se puede auditar no es una respuesta.
    """
    plan = json.loads((await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES_SQL.format(
                ahora=_ahora_txt(), zona=TZ.key, esquema=ESQUEMA)},
            {"role": "user", "content": pregunta},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )).choices[0].message.content)

    sql = str(plan.get("sql") or "").strip()
    aclaracion = str(plan.get("aclaracion") or "").strip()
    explicacion = str(plan.get("explicacion") or "").strip()

    # Prefirió repreguntar (o no le alcanzan los datos): se le pasa tal cual.
    if not sql:
        return {"texto": aclaracion or "No sé cómo responder eso todavía.",
                "sql": None, "explicacion": explicacion}

    sql = _validar(sql)
    try:
        filas = await _ejecutar(sql)
    except Exception as e:
        # Un SELECT que Postgres rechaza no es el final del camino: el mensaje
        # de error dice exactamente qué está mal, y con esa pista el modelo
        # suele arreglarlo solo. Rendirse en el primer intento sería tirar la
        # mejor información disponible — enseñarle sale más barato.
        log.warning("SQL rechazado, intento corregirlo una vez: %s", e)
        sql = _validar(await _corregir(pregunta, sql, str(e)))
        filas = await _ejecutar(sql)
        log.info("La corrección funcionó.")

    log.info("Consulta (%s filas): %s", len(filas), sql.replace("\n", " ")[:160])

    texto = _redactar_o_crudo(await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES_RESPUESTA.format(
                ahora=_ahora_txt())},
            {"role": "user", "content":
                f"Pregunta: {pregunta}\n\n"
                f"Resultado ({len(filas)} filas):\n"
                f"{json.dumps(filas, default=str, ensure_ascii=False, indent=1)}"},
        ],
        temperature=0.3,  # un poco de soltura para que suene humana
    ), filas)

    return {"texto": texto, "sql": sql, "explicacion": explicacion}
