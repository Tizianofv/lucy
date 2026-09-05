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
import textwrap

from psycopg.rows import dict_row

import db.db as db
from cerebro.bancos.categorias import NO_SUMAN
from cerebro.deepseek import MODELO, TZ, _ahora_txt, cliente

log = logging.getLogger("lucy.consultar")

# Techos de sensatez, no de capacidad: evitan que una consulta mal armada
# cuelgue el bucle o traiga media base a la memoria.
LIMITE_FILAS = 200
TIMEOUT_SQL = "10s"


# ── El esquema que ve el modelo ──────────────────────────────────────────
#
# LA LISTA DE COLUMNAS NO SE ESCRIBE A MANO. Se arma leyendo db/schema.sql.
#
# Por qué. Hasta el 4-sep-2026 este texto era una copia a mano de la tabla, y
# se había separado de ella sin que nadie se enterara: le faltaban 17 columnas
# de las 9 tablas que describe. Tres eran de `movimientos` —estado, banco,
# hash_contenido—, así que Lucy no sabía que `estado` existe y no podía
# excluir las compras declinadas. El panel sí las excluye, en sus cuatro
# consultas: db.resumen_por_mes, db.gasto_por_categoria,
# db.gastos_de_cada_categoria y db.sin_clasificar. (Se nombran por función y no
# por número de línea a propósito: las referencias que había acá —822, 858,
# 1076, 1117— ya apuntaban a otro sitio, porque db/db.py creció 120 líneas.)
# Los dos caminos contestaban números distintos a la misma pregunta, y el de
# Telegram contaba dinero que nunca salió.
#
# Agregar las tres columnas a mano habría arreglado el síntoma de hoy y dejado
# el mecanismo intacto: la copia se vuelve a separar la próxima vez que alguien
# agregue una columna. Así que la lista sale ahora de db/schema.sql, que es la
# misma fuente que ya usa db.tablas_que_faltan(). Lo único escrito a mano son
# los SIGNIFICADOS, que no se pueden deducir de un CREATE TABLE.
#
# Lo que esto NO cubre: que db/schema.sql se separe de la base REAL de Railway.
# Ya pasó una vez (`movimientos.banco` existía en Postgres y no en el archivo).
# Eso se pone rojo por otro lado: db.columnas_que_faltan(), que corre al
# arrancar (main.py) y en tools/humo.py.

# Las tablas que Lucy VE cuando escribe SQL. Es una decisión, no un descarte
# automático: son los datos de Tiziano. El orden es el del texto.
TABLAS_DE_TIZIANO = ("bandeja", "tareas", "eventos", "notas", "movimientos",
                     "personas", "lugares", "proyectos", "log_acciones")

# Las que NO ve, con el motivo. Están acá y no simplemente ausentes para que
# una tabla NUEVA no entre en silencio por ninguno de los dos lados: el test
# `test_el_esquema_sale_de_la_tabla` exige que toda tabla de schema.sql esté
# en una de las dos listas.
TABLAS_DE_MAQUINARIA = {
    "preferencias": "ajustes de Lucy, no datos de Tiziano",
    "correo_estado": "cursor de la ingesta de correo",
    "correo_reportado": "control de a qué correo ya se le avisó",
    "consumos_estado": "cursor de la ingesta bancaria",
    "backups": "latido del respaldo",
    "categorias_aprendidas": "memoria del clasificador, no un dato consultable",
    "cuentas_propias": "patrones de cuentas para detectar traspasos",
}

# Una línea por tabla, para que el modelo sepa qué es antes de mirar columnas.
TITULOS = {
    "bandeja": "todo lo que Tiziano le mandó a Lucy, crudo. Es el historial "
               "completo.",
    "tareas": "cosas por hacer.",
    "eventos": "citas y compromisos con hora. Agenda UNIFICADA: las que creó "
               "Lucy y las que vienen de Google Calendar (personal + estudio) "
               "viven juntas acá.",
    "notas": "información guardada sin acción asociada.",
    "movimientos": "TODA la plata, entre o salga.",
    "personas": "gente de su vida.",
    "lugares": 'los lugares con nombre de su vida ("CDS", "el estudio", '
               '"casa").',
    "proyectos": "los proyectos de su vida y de CDS.",
    "log_acciones": "todo lo que Lucy hizo, con el antes y el después.",
}

# El significado de una columna, cuando el nombre no alcanza. Es lo ÚNICO
# escrito a mano de esta parte. Una clave que ya no exista en schema.sql pone
# el test en rojo: una nota huérfana es la señal de que la tabla cambió.
NOTAS_DE_COLUMNA = {
    ("bandeja", "tipo_entrada"): "'texto'|'audio'|'foto'",
    ("bandeja", "contenido_raw"): "lo que escribió",
    ("bandeja", "transcripcion"): "lo que Whisper oyó o lo que se leyó en la foto",
    ("bandeja", "respuesta_lucy"): "lo que Lucy le contestó: su mitad de la charla",
    ("bandeja", "interpretacion"): "jsonb",
    ("bandeja", "embedding"): "vector; no sirve para contar ni sumar",
    ("bandeja", "archivo_id"): "maquinaria de Telegram",
    ("bandeja", "chat_id"): "maquinaria de Telegram",
    ("bandeja", "telegram_msg_id"): "maquinaria de Telegram",
    ("bandeja", "hash_contenido"): "maquinaria: idempotencia de la captura",
    ("bandeja", "intentos"): "maquinaria: cola de reintentos",
    ("bandeja", "reintentar_despues"): "maquinaria: cola de reintentos",
    ("tareas", "estado"): "'pendiente'|'hecha'|'pospuesta'",
    ("tareas", "avisos_enviados"): "int[]: minutos-antes que ya se avisaron",
    ("tareas", "anticipos_min"): "int[]: minutos-antes a los que hay que avisar",
    ("eventos", "avisos_enviados"): "int[]: minutos-antes que ya se avisaron",
    ("eventos", "anticipos_min"): "int[]: minutos-antes a los que hay que avisar",
    ("eventos", "preaviso_en"): "HUÉRFANA desde el 13-ago-2026: ya no se lee "
                                "ni se escribe, no la uses",
    ("eventos", "gcal_cal_id"): "id del calendario en Google",
    ("notas", "etiquetas"): "text[]; 'idea' marca las ideas",
    ("movimientos", "tipo"): "'gasto'|'ingreso'|'transferencia'",
    ("movimientos", "fecha"): "DATE, ya en hora local",
    ("movimientos", "monto"): "numeric, SIEMPRE positivo",
    ("movimientos", "moneda"): "normalmente 'DOP'",
    ("movimientos", "contraparte"): "el comercio si salió, quién pagó si entró",
    ("movimientos", "referencia"): "No. de comprobante",
    ("movimientos", "estado"): "'aprobada'|'declinada'|'pendiente' — ver "
                               "MODISMO 6, es el que más engaña",
    ("movimientos", "banco"): "de dónde salió la plata: 'BHD', 'Banreservas', "
                              "'efectivo'… NULL si no se dijo",
    ("movimientos", "hash_contenido"): "huella del correo del banco; NULL = "
                                       "cargado a mano desde el panel",
    ("personas", "alias"): "text[]",
    ("log_acciones", "antes"): "jsonb",
    ("log_acciones", "despues"): "jsonb",
}

# Lo que no cabe al lado del nombre de una columna. Va debajo de cada tabla.
NOTAS_DE_TABLA = {
    "bandeja": [
        "estado: 'sin_procesar'|'procesando'|'esperando_confirmacion'|"
        "'esperando_respuesta'|'procesado'|'descartado'|'error'",
    ],
    "tareas": [
        "recurrencia: NULL = una sola vez. Con texto ('cada 8 horas', "
        "'diaria', 'semanal'...) la tarea se reprograma sola al marcarse "
        "hecha: hay UNA fila por tarea recurrente, no una por ocurrencia.",
    ],
    "eventos": [
        "gcal_calendar: de qué calendario de Google vino ('Tiziano Fajardo "
        "Vargas' = su personal; 'CDS Sala P', 'CDS GRABACIONES', etc. = el "
        "estudio). NULL = cita que creó Lucy por Telegram.",
        "gcal_id: NULL = nativa de Lucy; con valor = espejo de un evento de "
        "Google.",
    ],
}

MODISMOS = """\
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
   `AT TIME ZONE` va SOLO sobre timestamptz (vence_en, inicia_en, creado_en,
   ts…). Aplicárselo a un DATE o a current_date revienta con "invalid input
   syntax for type date" — `movimientos.fecha` YA es fecha local, y para el
   día de hoy usá (now() AT TIME ZONE 'America/Santo_Domingo')::date.

5. UNA TAREA PENDIENTE es estado='pendiente' AND borrado_en IS NULL.

6. LAS DECLINADAS NO CUENTAN, Y NO HACE FALTA QUE TE LO PIDAN.
   movimientos.estado vale 'aprobada', 'declinada' o 'pendiente'.
   · 'declinada' = el banco rechazó la compra. ESA PLATA NUNCA SALIÓ.
   · 'pendiente' = una retención. SÍ es gasto real: para las tarjetas en
     dólares el aviso de retención es el único registro que manda el banco
     (Railway, Amazon Prime, Anthropic llegan así todos los meses).
   Entonces, en TODA suma, conteo, promedio o ranking de movimientos, poné
   siempre "AND estado <> 'declinada'", aunque la pregunta no lo mencione.
   NUNCA uses "estado = 'aprobada'": eso borra las retenciones, que sí
   ocurrieron. El panel de Tiziano filtra exactamente así, y si vos filtrás
   distinto le vas a dar dos números diferentes a la misma pregunta.
   Solo mostrá las declinadas cuando la pregunta sea sobre ellas ("¿qué
   compras me rechazaron?") o pida el detalle sin totalizar.

7. EL DINERO DE TERCEROS NO ENTRA EN NINGÚN TOTAL.
   La categoría {no_suman} marca la plata que solo PASA por la cuenta (el
   circuito del papá de Rosi: intereses que entran, la luz de su casa que
   sale). No es ingreso ni gasto de esta casa, y contarla infló un mes en
   RD$43,312 de ingreso y RD$41,500 de gasto a la vez.
   En cualquier TOTAL de gasto o de ingreso agregá
   "AND coalesce(categoria, '') <> ALL(ARRAY[{no_suman}])".
   En un DETALLE (la lista de movimientos de un mes) mostralos, pero decí que
   no cuentan. Es lo mismo que hace el panel.\
"""


def _armar_bloques() -> dict[str, str]:
    """{tabla: su trozo del prompt}, con la estructura sacada de schema.sql.

    Se devuelve partido por tabla y no como un solo texto para que se pueda
    comprobar POR TABLA que cada columna llegó. Mirar el prompt entero no
    alcanza: `estado` existe en bandeja, tareas, proyectos y movimientos, así
    que un test que busque "estado" en todo el texto pasa aunque el bloque de
    `movimientos` lo haya perdido — que es exactamente el fallo que se está
    arreglando.
    """
    declaradas = db.columnas_declaradas()
    # Que esto reviente al importar es DELIBERADO. La alternativa —seguir con
    # una lista vacía— es un prompt sin columnas, y un prompt sin columnas no
    # deja a Lucy muda: la deja inventando SQL contra tablas que no conoce y
    # contestando números falsos. Un arranque roto se ve en el primer log; un
    # número falso no se ve nunca. Si schema.sql no está o no se puede leer,
    # eso es un defecto de despliegue y tiene que gritar.
    vacias = [t for t in TABLAS_DE_TIZIANO if not declaradas.get(t)]
    if vacias:
        raise ValueError(
            f"db/schema.sql no declara columnas para {vacias}. Sin eso el "
            "esquema que ve Lucy sale incompleto y sus respuestas dejan de "
            "cuadrar con el panel.")

    bloques: dict[str, str] = {}
    for tabla in TABLAS_DE_TIZIANO:
        piezas = []
        for col in declaradas.get(tabla, []):
            nota = NOTAS_DE_COLUMNA.get((tabla, col))
            piezas.append(f"{col} ({nota})" if nota else col)
        # break_long_words=False: sin esto textwrap parte por la mitad un
        # 'esperando_respuesta' o un nombre de columna largo, y el modelo
        # termina leyendo un identificador que no existe.
        lineas = [
            textwrap.fill(f"{tabla} — {TITULOS[tabla]}", width=76,
                          subsequent_indent="  ", break_long_words=False),
            textwrap.fill(", ".join(piezas), width=76, initial_indent="  ",
                          subsequent_indent="  ", break_long_words=False),
        ]
        for nota in NOTAS_DE_TABLA.get(tabla, []):
            lineas.append(textwrap.fill(nota, width=76, initial_indent="  · ",
                                        subsequent_indent="    ",
                                        break_long_words=False))
        bloques[tabla] = "\n".join(lineas)
    return bloques


def _armar_esquema() -> str:
    """El texto completo que ve el modelo: las tablas y los modismos."""
    cabecera = (
        "TABLAS (PostgreSQL). Todas las fechas son timestamptz salvo aviso.\n"
        "Esta lista se arma sola desde el esquema real: son TODAS las "
        "columnas\nque la tabla tiene, no un resumen.\n"
    )
    cuerpo = "\n\n".join(BLOQUES[t] for t in TABLAS_DE_TIZIANO)
    modismos = MODISMOS.format(
        no_suman=", ".join(f"'{c}'" for c in NO_SUMAN))
    return f"{cabecera}\n{cuerpo}\n\n{modismos}"


BLOQUES = _armar_bloques()
ESQUEMA = _armar_esquema()

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
