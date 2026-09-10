# -*- coding: utf-8 -*-
"""El panel de tareas: los grupos, el día de Santo Domingo, y cerrar varias.

QUÉ SE PRUEBA CON MÁS SAÑA, y por qué son ésas y no otras:

  · QUE NADA DESAPAREZCA. Las 4 tareas pendientes SIN FECHA de producción no
    las veía ninguna de las dos definiciones de "atrasada" que andaban sueltas
    en los prompts, y llevaban meses invisibles. Y `tareas.estado` no tiene
    restricción CHECK: en producción hay un 'descartado' que el comentario del
    esquema no declara. Una lista de estados escrita a mano en el panel lo
    habría borrado de la pantalla sin que nadie se entere.

  · QUE "ATRASADA" SEA POR DÍA. Medido contra producción el 8-sep-2026, las dos
    redacciones que había daban 9 (por instante) y 8 (por día). Ahora hay una
    sola definición y esto la fija.

  · QUE EL DÍA SEA EL DE SANTO DOMINGO. `vence_en` es TIMESTAMPTZ, o sea un
    instante. Las 11 de la noche de un lunes en Santo Domingo son las 3 de la
    mañana del MARTES en UTC: comparado en UTC, el panel diría que una tarea de
    esta noche es de mañana.

LO QUE ESTOS TESTS NO PUEDEN VER, medido y no supuesto: `psycopg` está
reemplazado por un módulo falso, así que nada de acá habla con Postgres y LA
CONEXIÓN DEVUELVE LAS FILAS QUE SE LE PREPARAN. O sea que ninguna prueba de
comportamiento de este archivo puede ver lo que el SQL hace con las filas antes
de devolverlas.

Eso no es teoría: cuando esta consulta se unía con `bandeja`, cambiar
`LEFT JOIN` por `JOIN` en una copia dejó las 484 pruebas VERDES —y
`tools/humo.py` también habría salido verde, porque un INNER JOIN corre
perfecto y solo devuelve 90 filas en vez de 91—. Por eso
`test_ningun_join_de_esta_consulta_puede_descartar_una_tarea` mira el TEXTO del
SQL, que es lo que el resto de este archivo evita: ahí es lo único que hay, y
está dicho con su límite al lado. Desde el 10-sep-2026 esa consulta no tiene
ningún JOIN —la columna que lo justificaba salió del panel—, y esa guarda queda
esperando al que alguien escriba mañana; lo que sí comprueba hoy es que
`tareas` siga mandando en el FROM.

Y el texto del SQL se saca con `_sql_de`, del árbol de sintaxis, no con
`inspect.getsource`. Los docstrings de este repo son largos y están en español,
y una frase como «todo JOIN de esta consulta» se lee como un JOIN que no es
LEFT: la guarda terminaba midiendo la prosa que EXPLICA el SQL en vez del SQL.

Que las columnas existan de verdad en la base lo cubre `tools/humo.py`, que
necesita DATABASE_URL. El test de columnas de abajo compara contra
db/schema.sql, que describe la base pero no ES la base.

Correr:  python3 -m pytest tests/test_panel_tareas.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO
UTC = timezone.utc


# ── Una base de mentira ──────────────────────────────────────────────────

class _Cursor:
    """Devuelve las filas preparadas y anota todo el SQL que le llega."""

    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if s.startswith("SELECT t.id, t.titulo"):
            self._filas = list(self._conn.tareas)
        elif s.startswith("SELECT id, titulo, estado"):
            tid = params[0]
            self._filas = [f for f in self._conn.tareas if f["id"] == tid]
        else:
            self._filas = []
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _Conn:
    def __init__(self, filas):
        self.tareas = filas
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def execute(self, sql, params=None):
        return await _Cursor(self).execute(sql, params)


class _CM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _CM(self._conn)


def _fila(id, estado="pendiente", vence_en=None, titulo=None,
          responsable=None):
    """Una fila como la devuelve la consulta del panel.

    `responsable` es None por defecto porque ES el estado normal: las 57 tareas
    vivas de producción nacieron sin responsable y se asignan desde el panel.
    Ponerle un valor por defecto haría que ninguna prueba de este archivo
    pintara nunca el caso más común.
    """
    return {"id": id, "titulo": titulo or f"tarea {id}", "estado": estado,
            "vence_en": vence_en, "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "bandeja_id": 900 + id, "responsable_chat_id": responsable,
            "completado_en": None}


def _con_base(filas, fn):
    """Corre una corutina con la base falseada. Devuelve (resultado, conn)."""
    conn = _Conn(filas)
    guardado = db.pool
    db.pool = _Pool(conn)
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(fn()), conn
        finally:
            bucle.close()
    finally:
        db.pool = guardado


def _grupos(filas, hoy):
    datos, _ = _con_base(filas, lambda: db.tareas_por_grupo(hoy=hoy))
    return {g["clave"]: g["filas"] for g in datos["grupos"]}


def _sql_de(fn) -> str:
    """El SQL que ESCRIBE una función, sin su docstring ni sus comentarios.

    POR QUÉ NO `inspect.getsource` A SECAS, que es lo que había acá y costó dos
    rojos falsos el 10-sep-2026. Las guardas de abajo buscan `LEFT JOIN` y
    `b.columna` con expresiones regulares, y el texto que les llegaba incluía
    la prosa del docstring — que en este repo es larga y está en español. Una
    frase tan normal como «todo JOIN de esta consulta» se leía como un
    `JOIN de` que no era LEFT, y «traía `b.chat_id`» se leía como una columna
    que la consulta usa. O sea: la guarda medía lo que alguien había escrito
    EXPLICANDO el SQL en vez de medir el SQL.

    Acá el texto sale del árbol de sintaxis: se parsea la función, se descarta
    el docstring y se juntan los literales de cadena que quedan, que es de
    donde salen las consultas de este módulo. Los comentarios `#` no son nodos
    del árbol y desaparecen solos. Lo que queda es lo que de verdad viaja a
    Postgres, y ninguna cantidad de prosa lo puede mover.
    """
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    cuerpo = arbol.body[0].body
    if (cuerpo and isinstance(cuerpo[0], ast.Expr)
            and isinstance(cuerpo[0].value, ast.Constant)
            and isinstance(cuerpo[0].value.value, str)):
        cuerpo = cuerpo[1:]
    trozos = [n.value for nodo in cuerpo for n in ast.walk(nodo)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return " ".join(" ".join(t.split()) for t in trozos)


# ── El criterio, sin base de por medio ───────────────────────────────────

def test_atrasada_es_por_dia_y_no_por_instante():
    """LA decisión central. Una tarea que vencía HOY a las 9 de la mañana no
    está atrasada a las 10: lo estará mañana. En el ritmo de un estudio de
    grabación nadie trabaja al minuto, y las dos personas que miran este panel
    piensan en días. Medido contra producción el 8-sep-2026, las dos
    definiciones sueltas que había daban 9 (por instante) y 8 (por día).
    """
    hoy = date(2026, 9, 8)
    # 9:00 de la mañana de HOY en Santo Domingo (13:00 UTC): ya pasó la hora y
    # sigue siendo de hoy. Con un criterio por instante, esto saldría atrasado.
    esta_manana = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
    assert db.grupo_de_tarea("pendiente", esta_manana, hoy) == "hoy"

    # Un minuto después de medianoche de hoy, tampoco.
    assert db.grupo_de_tarea(
        "pendiente", datetime(2026, 9, 8, 4, 1, tzinfo=UTC), hoy) == "hoy"

    # Y lo de AYER sí está atrasado, aunque sea por un minuto.
    ayer_tarde = datetime(2026, 9, 8, 3, 59, tzinfo=UTC)   # 7-sep 23:59 RD
    assert db.grupo_de_tarea("pendiente", ayer_tarde, hoy) == "atrasadas"

    # Y lo de mañana es próximo, no de hoy.
    assert db.grupo_de_tarea(
        "pendiente", datetime(2026, 9, 9, 4, 1, tzinfo=UTC), hoy) == "proximas"


def test_el_dia_se_cuenta_en_santo_domingo_y_no_en_utc():
    """`vence_en` es TIMESTAMPTZ: un INSTANTE, no una fecha. Las 11 de la noche
    del 7 de septiembre en Santo Domingo son las 3 de la mañana del 8 en UTC.

    Contado en UTC, esa tarea sería "de mañana" y desaparecería del grupo de
    hoy — que es justo la hora a la que alguien mira el panel para ver qué le
    falta hoy. Esta prueba es la que se pone roja si algún día se compara en
    UTC, y no habría forma de verlo mirando la pantalla hasta las 8 de la noche.
    """
    de_noche = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)      # 7-sep 23:00 RD
    assert db.dia_rd(de_noche) == date(2026, 9, 7), (
        "el día se está sacando en UTC y no en Santo Domingo")
    assert db.grupo_de_tarea("pendiente", de_noche, date(2026, 9, 7)) == "hoy"
    # Y al día siguiente esa misma tarea sí está atrasada.
    assert db.grupo_de_tarea(
        "pendiente", de_noche, date(2026, 9, 8)) == "atrasadas"

    # La zona sale de config.TZ y no de un texto escrito en db/db.py: si un día
    # se separan, el panel y el resto de Lucy contarían días distintos.
    assert db.TZ is config.TZ


def test_la_zona_no_depende_del_reloj_de_la_maquina():
    """Un instante sin zona se lee como UTC, no como la hora de quien corra
    esto. Adivinar la zona del servidor haría que el MISMO dato diera días
    distintos en Railway y en una laptop."""
    sin_zona = datetime(2026, 9, 8, 3, 0)
    assert db.dia_rd(sin_zona) == date(2026, 9, 7)
    assert db.dia_rd(None) is None


# ── Que nada desaparezca ─────────────────────────────────────────────────

def test_las_tareas_sin_fecha_aparecen_y_con_su_propio_titulo():
    """ES EL CASO QUE HOY SE PIERDE. En producción hay 4 pendientes sin
    `vence_en`, y ninguna definición de "atrasada" —ni la de instante ni la de
    día— las ve: están perdidas. Esconderlas es exactamente cómo se perdieron.
    """
    filas = [_fila(1), _fila(2), _fila(3, vence_en=datetime(2026, 9, 8, 13,
                                                            tzinfo=UTC))]
    datos, _ = _con_base(filas, lambda: db.tareas_por_grupo(
        hoy=date(2026, 9, 8)))
    grupos = {g["clave"]: g for g in datos["grupos"]}
    assert "sin_fecha" in grupos, "las tareas sin fecha desaparecieron"
    assert len(grupos["sin_fecha"]["filas"]) == 2
    assert grupos["sin_fecha"]["titulo"] == "Sin fecha", (
        "el grupo tiene que tener su propio título, no colgarse de otro")


def test_un_estado_que_nadie_declaro_sale_en_el_panel():
    """LA PRUEBA QUE IMPIDE QUE VUELVA EL DEFECTO DE LA LISTA TECLEADA.

    `tareas.estado` no tiene restricción CHECK. El comentario de db/schema.sql
    declara `pendiente | hecha | pospuesta`, y en producción hay 46 pendiente,
    44 hecha, 1 DESCARTADO y cero pospuesta: un estado que nadie declaró, y uno
    declarado que nadie usó nunca.

    Si el panel filtrara por una lista de estados conocidos, la fila en
    'descartado' desaparecería y nadie se enteraría. Acá se le mete un estado
    inventado en el momento —uno que no está escrito en NINGUNA parte del
    repositorio— y tiene que salir igual.
    """
    inventado = "guardado_en_la_nevera"
    assert inventado not in open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "db", "db.py"), encoding="utf-8").read(), (
        "el estado de esta prueba tiene que ser uno que el código no conozca")

    filas = [_fila(1, estado="descartado"), _fila(2, estado=inventado),
             _fila(3, estado="hecha"), _fila(4)]
    grupos = _grupos(filas, date(2026, 9, 8))
    estados = {f["estado"] for f in grupos.get("otros", [])}
    assert estados == {"descartado", inventado, "hecha"}, (
        f"un estado se perdió del panel: {estados}")


def test_ninguna_fila_se_pierde_por_el_camino():
    """La suma de los grupos tiene que dar la cantidad de filas que entraron.

    No se comprueba contra una lista de grupos escrita acá —eso sería el mismo
    defecto que esta prueba persigue—: se cuenta lo que entró y lo que salió.
    """
    filas = ([_fila(i) for i in range(1, 5)]
             + [_fila(10 + i, estado="hecha") for i in range(4)]
             + [_fila(20 + i, vence_en=datetime(2026, 9, 1 + i, 13, tzinfo=UTC))
                for i in range(6)]
             + [_fila(30, estado="lo_que_sea")])
    datos, _ = _con_base(filas, lambda: db.tareas_por_grupo(
        hoy=date(2026, 9, 8)))
    salieron = sum(len(g["filas"]) for g in datos["grupos"])
    assert salieron == len(filas), (
        f"entraron {len(filas)} tareas y salieron {salieron}")
    assert not datos["hay_mas"]


def test_una_clave_de_grupo_que_nadie_previo_se_pinta_igual():
    """`GRUPOS_DE_TAREAS` da el ORDEN y los títulos, no la lista de grupos que
    pueden existir. Si mañana el criterio devuelve una clave que no está ahí,
    esas tareas tienen que salir al final con su clave cruda en vez de
    desaparecer — que es la forma en que una lista escrita a mano borra cosas.
    """
    declaradas = {c for c, _ in db.GRUPOS_DE_TAREAS}
    guardado = db.grupo_de_tarea
    db.grupo_de_tarea = lambda estado, vence, hoy: "grupo_del_futuro"
    try:
        datos, _ = _con_base([_fila(1), _fila(2)],
                             lambda: db.tareas_por_grupo(hoy=date(2026, 9, 8)))
    finally:
        db.grupo_de_tarea = guardado
    claves = [g["clave"] for g in datos["grupos"]]
    assert "grupo_del_futuro" in claves, (
        "una clave no declarada hizo desaparecer sus tareas")
    assert "grupo_del_futuro" not in declaradas
    assert sum(len(g["filas"]) for g in datos["grupos"]) == 2


def test_la_tarea_sin_bandeja_no_se_esconde_al_repartirla():
    """Una tarea a la que le falta un dato accesorio sale IGUAL. Es la misma
    familia de fallo que las 4 sin fecha, que llevaban meses invisibles.

    En producción, `bandeja_id` estaba en 90 de las 91 filas. Esa columna hoy
    ya no se pinta —era la de «Quién la anotó», que Tiziano sacó del panel el
    10-sep-2026— pero sigue viajando en la consulta y sigue siendo el
    `bandeja_id` de cada huella de `log_acciones`, así que la pregunta de si
    una fila sin ella sobrevive al reparto sigue en pie. Y con ella la de la
    columna que la reemplazó: una tarea SIN RESPONSABLE es lo normal —así
    nacieron las 57 vivas— y no puede desaparecer por eso.

    ESTA MITAD ES DE COMPORTAMIENTO, y solo cubre el reparto en Python. La otra
    mitad —que el SQL no descarte la fila antes de llegar acá— NO se puede ver
    desde una suite hermética, y va en la prueba de abajo con su límite dicho.
    """
    filas = [_fila(1), _fila(2, responsable=DUENO)]
    filas[0]["bandeja_id"] = None
    grupos = _grupos(filas, date(2026, 9, 8))
    ids = {f["id"] for fs in grupos.values() for f in fs}
    assert ids == {1, 2}, "se perdió una tarea a la que le falta un accesorio"


def test_ningun_join_de_esta_consulta_puede_descartar_una_tarea():
    """Todo JOIN de esta consulta tiene que ser LEFT JOIN. La regla, en una
    línea: `tareas` manda, y ninguna tabla accesoria puede quitar una fila.

    POR QUÉ ESTO MIRA EL TEXTO DEL SQL, que es lo que el resto de esta suite
    evita a propósito. Lo descubrí mutando: cambié `LEFT JOIN` por `JOIN` en la
    copia y las 484 pruebas siguieron VERDES, incluida la de arriba, que
    parecía cubrirlo. No es culpa de esa prueba: la conexión de estas suites es
    de mentira y devuelve las filas que se le preparan, así que el tipo de JOIN
    NO ES OBSERVABLE desde acá. Ninguna cantidad de pruebas de comportamiento
    en esta suite puede verlo.

    Y `tools/humo.py` TAMPOCO lo vería: con la base real, un INNER JOIN corre
    perfecto y solo devuelve 90 filas en vez de 91. Corre en verde y miente.

    Así que esto es lo más fuerte que se puede hacer sin una base delante, y se
    dice como lo que es. Lo que lo salva de ser una lista tecleada es que la
    lista sale del SQL —se cuentan los JOIN que HAY, no los que me acuerdo—:
    el JOIN que alguien escriba mañana tiene que cumplirlo igual, o esto se
    pone rojo solo.

    HOY LA CONSULTA NO TIENE NINGUNO, y por eso la comprobación no exige que
    haya alguno. Hasta el 10-sep-2026 traía una unión con `bandeja` para pintar
    la columna «Quién la anotó», que Tiziano sacó del panel. Un test que
    exigiera un JOIN estaría exigiendo que se pinte una columna que ya no
    existe. Lo que se comprueba, y sigue mordiendo, son dos cosas que no
    dependen de cuántos haya:

      · el que HAYA tiene que ser LEFT;
      · y `tareas` tiene que ser la tabla que manda en el FROM. Si mañana
        alguien la mueve a un JOIN y pone otra en el FROM, la regla «ninguna
        tabla accesoria puede quitar una fila de tareas» se invierte sin que
        una sola letra diga LEFT.
    """
    import re
    sql = _sql_de(db.tareas_por_grupo)
    joins = re.findall(r"(\w+)\s+JOIN\s+(\w+)", sql)
    malos = [f"{previo} JOIN {tabla}" for previo, tabla in joins
             if previo.upper() != "LEFT"]
    assert not malos, (
        f"estos JOIN pueden descartar tareas enteras: {malos}. Una tarea sin "
        "la fila accesoria desaparecería del panel y nadie se enteraría")
    de_donde = re.findall(r"\bFROM\s+(\w+)", sql)
    assert de_donde == ["tareas"], (
        f"esta consulta ya no sale de `tareas`, sale de {de_donde}. `tareas` "
        "tiene que mandar en el FROM: desde cualquier otra tabla, una tarea "
        "sin su fila accesoria desaparece del panel aunque todos los JOIN "
        "digan LEFT")


def test_el_tope_no_recorta_callado():
    """Un LIMIT que recorta en silencio es la forma más barata de perder una
    tarea. Se piden `limite + 1` filas y, si vuelve una de más, se dice."""
    filas = [_fila(i) for i in range(1, 8)]
    datos, conn = _con_base(filas, lambda: db.tareas_por_grupo(
        limite=5, hoy=date(2026, 9, 8)))
    assert datos["hay_mas"] is True, "recortó sin avisar"
    assert sum(len(g["filas"]) for g in datos["grupos"]) == 5
    sql, params = conn.sql[0]
    assert params == (6,), "hay que pedir una fila de más para saber si sobra"


# ── Los grupos no salen de una lista de estados ──────────────────────────

def test_el_panel_no_tiene_escrita_una_lista_de_estados():
    """La forma en que este panel se rompería en silencio es que alguien
    escriba `('pendiente', 'hecha', 'pospuesta')` en algún lado: ese día la
    fila en 'descartado' de producción sale de la pantalla y nadie se entera.

    El estado 'pospuesta' está DECLARADO en el comentario de db/schema.sql y no
    lo usa ninguna fila, así que es la pista de que alguien copió la lista del
    esquema en vez de derivar los grupos de lo que devuelve la consulta.
    """
    import inspect
    fuente = (inspect.getsource(db.tareas_por_grupo)
              + inspect.getsource(db.grupo_de_tarea)
              + inspect.getsource(db.marcar_tarea_hecha))
    assert "pospuesta" not in fuente, (
        "hay una lista de estados copiada del esquema en el panel de tareas")
    # Y el SQL no filtra por estado: si filtrara, el estado nuevo no llegaría
    # nunca a `grupo_de_tarea` y la prueba del estado inventado sería mentira.
    sql = inspect.getsource(db.tareas_por_grupo)
    assert "estado =" not in sql and "estado IN" not in sql, (
        "la consulta filtra por estado: lo que no esté en el filtro desaparece")


# ── Las columnas salen del esquema, no de la memoria de nadie ────────────

def test_las_columnas_de_la_consulta_existen_en_el_esquema():
    """Las columnas que nombra el SELECT se sacan del código y se comprueban
    contra db/schema.sql. Ninguna de las dos listas se teclea acá.

    NI SIQUIERA LA DE ALIAS. Acá decía `(("t", "tareas"), ("b", "bandeja"))`, y
    eso era exactamente la lista escrita a mano que este archivo persigue en
    todo lo demás: el día que la consulta dejó de unirse con `bandeja`, el par
    `("b", "bandeja")` seguía ahí exigiendo columnas de una tabla que ya no se
    consulta. Ahora los alias salen del propio SQL —de sus FROM y sus JOIN—,
    así que una tabla que entre o salga se refleja sola.

    Y SE COMPRUEBA QUE NO QUEDE NINGÚN PREFIJO SUELTO: si el SELECT nombra
    `x.algo` y ningún FROM ni JOIN declara `x`, es un alias mal escrito, que es
    justo el error que esta prueba existe para agarrar sin DATABASE_URL. Sin
    esa vuelta, un typo en el alias se saltaba la comprobación entera en vez de
    ponerse rojo.

    Esto NO reemplaza a tools/humo.py: schema.sql describe la base, no ES la
    base. Lo que agarra es el typo, sin necesidad de DATABASE_URL.
    """
    import re
    sql = _sql_de(db.tareas_por_grupo)
    declaradas = db.columnas_declaradas()

    # `FROM tareas t` y `LEFT JOIN bandeja b`: la tabla y el alias que le pone
    # esta consulta, leídos de la consulta.
    por_alias = {alias: tabla for tabla, alias in
                 re.findall(r"(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql)}
    assert por_alias, "no se encontró ninguna tabla con alias en el SQL"

    usados = set(re.findall(r"\b([a-z_]+)\.[a-z_]+\b", sql))
    huerfanos = usados - set(por_alias)
    assert not huerfanos, (
        f"el SQL nombra columnas de {sorted(huerfanos)} y ningún FROM ni JOIN "
        "declara ese alias: está mal escrito, y las columnas que cuelgan de él "
        "no las comprueba nadie")

    for alias, tabla in por_alias.items():
        nombres = set(re.findall(rf"\b{alias}\.([a-z_]+)\b", sql))
        assert nombres, f"no se encontró ninguna columna de {tabla} en el SQL"
        faltan = nombres - set(declaradas[tabla])
        assert not faltan, f"{tabla}: columnas que el esquema no declara: {faltan}"


def test_la_escritura_solo_toca_columnas_declaradas():
    import inspect
    import re
    fuente = inspect.getsource(db.marcar_tarea_hecha)
    declaradas = set(db.columnas_declaradas()["tareas"])
    escritas = set(re.findall(r"SET ([a-z_]+) =", fuente))
    escritas |= set(re.findall(r"([a-z_]+) = now\(\)", fuente))
    assert escritas, "no se encontró ninguna columna escrita"
    assert escritas <= declaradas, (
        f"escribe columnas que el esquema no declara: {escritas - declaradas}")


# ── La escritura ─────────────────────────────────────────────────────────

def _post(campos: dict, con_sesion: bool = True):
    from urllib.parse import urlencode

    from starlette.requests import Request
    import web.app as panel

    cuerpo = urlencode(campos).encode()
    cabeceras = [(b"host", b"t"),
                 (b"content-type", b"application/x-www-form-urlencoded"),
                 (b"content-length", str(len(cuerpo)).encode())]
    if con_sesion:
        galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
        cabeceras.append((b"cookie", galleta.encode()))

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "https", "server": ("t", 443), "path": "/tareas",
                    "root_path": "", "query_string": b"",
                    "headers": cabeceras, "app": panel.app}, recibir)


def _guardar(campos: dict, con_sesion: bool = True):
    """Manda el formulario con la escritura ESPIADA. Devuelve (respuesta, ids)."""
    import web.app as panel

    marcadas: list = []

    async def _espia(tid):
        marcadas.append(tid)
        return True

    guardado = db.marcar_tarea_hecha
    db.marcar_tarea_hecha = _espia
    try:
        bucle = asyncio.new_event_loop()
        try:
            r = bucle.run_until_complete(
                panel.guardar_tareas(_post(campos, con_sesion)))
        finally:
            bucle.close()
    finally:
        db.marcar_tarea_hecha = guardado
    return r, marcadas


def test_se_cierran_varias_de_una_vez_y_las_demas_no_se_pisan():
    """Marcar diez y guardar UNA vez. Con un guardado por fila, cada envío
    recargaba y se llevaba puesto lo demás marcado: es el defecto que ya está
    documentado en POST /categorias y que se comió aquella función entera.

    Y lo que no se marcó NO SE TOCA. Un panel que reescribe filas que nadie
    pidió es peor que uno que no guarda.
    """
    campos = {"prev_1": "pendiente", "prev_2": "pendiente",
              "prev_3": "pendiente", "prev_4": "pendiente",
              "hecha_1": "1", "hecha_3": "1"}
    r, marcadas = _guardar(campos)
    assert r.status_code == 303
    assert marcadas == [1, 3], f"se escribieron filas que nadie marcó: {marcadas}"
    assert "guardadas=2" in r.headers["location"]


def test_lo_que_ya_estaba_hecho_no_se_vuelve_a_escribir():
    """`prev_` es lo que la fila tenía cuando se pintó la pantalla. Si ya
    estaba hecha, no hay nada que cambiar: reescribirla movería `completado_en`
    a la hora equivocada y dejaría una huella de una edición que no pasó."""
    r, marcadas = _guardar({"prev_5": "hecha", "hecha_5": "1"})
    assert marcadas == [], "reescribió una tarea que ya estaba hecha"
    assert "guardadas=0" in r.headers["location"]


def test_un_campo_con_basura_no_revienta_la_pantalla():
    campos = {"hecha_abc": "1", "hecha_": "1", "hecha_1": "1",
              "prev_1": "pendiente", "volver": "//evil.com"}
    r, marcadas = _guardar(campos)
    assert r.status_code == 303
    assert marcadas == [1]
    # No hay destino elegible: de acá se vuelve siempre a /tareas, así que el
    # agujero de "//evil.com" no existe en vez de estar tapado.
    assert r.headers["location"].startswith("/tareas?")


def test_la_ruta_que_escribe_exige_sesion():
    """Cada ruta nueva es una puerta nueva, y ésta ESCRIBE: sin sesión, un POST
    de cualquiera cerraría las tareas de esta casa."""
    r, marcadas = _guardar({"prev_1": "pendiente", "hecha_1": "1"},
                           con_sesion=False)
    assert r.status_code == 401, f"entró sin cookie: {r.status_code}"
    assert marcadas == [], "escribió sin sesión"


def test_la_pantalla_exige_sesion():
    import web.app as panel
    from starlette.requests import Request

    def _pedir(cabeceras):
        return Request({"type": "http", "http_version": "1.1", "method": "GET",
                        "scheme": "https", "server": ("t", 443),
                        "path": "/tareas", "root_path": "", "query_string": b"",
                        "headers": cabeceras, "app": panel.app})

    bucle = asyncio.new_event_loop()
    try:
        # Sin cookie.
        r = bucle.run_until_complete(panel.tareas(_pedir([(b"host", b"t")])))
        assert r.status_code == 401, f"entró sin cookie: {r.status_code}"
        # Y con un token bien firmado PARA OTRO CHAT: es válido como token y aun
        # así no puede entrar. Son dos preguntas distintas.
        ajeno = auth.crear_token(DUENO + 1, auth.VIDA_SESION)
        r = bucle.run_until_complete(panel.tareas(_pedir(
            [(b"host", b"t"),
             (b"cookie", f"{panel.COOKIE}={ajeno}".encode())])))
        assert r.status_code == 401, f"entró un chat ajeno: {r.status_code}"
    finally:
        bucle.close()


def test_la_escritura_deja_huella_y_no_pisa_lo_ya_hecho():
    """`marcar_tarea_hecha` de verdad, contra la base falsa: tiene que hacer el
    UPDATE Y la fila de log_acciones, y no escribir nada cuando ya estaba
    hecha. Toda escritura del panel es auditable y reversible."""
    filas = [_fila(1), _fila(2, estado="hecha")]

    ok, conn = _con_base(filas, lambda: db.marcar_tarea_hecha(1))
    assert ok is True
    escrito = " | ".join(s for s, _ in conn.sql)
    assert "UPDATE tareas SET estado" in escrito
    assert "completado_en = now()" in escrito, (
        "la otra puerta (Telegram) sí llena completado_en: dos caminos que "
        "dejan la fila distinta es cómo se pierde la confianza en los dos")
    assert "INSERT INTO log_acciones" in escrito, "escribió sin dejar huella"
    assert "'panel'" in escrito, "la huella tiene que decir que fue el panel"

    ya, conn2 = _con_base(filas, lambda: db.marcar_tarea_hecha(2))
    assert ya is False
    assert not any("UPDATE" in s for s, _ in conn2.sql), (
        "reescribió una tarea que ya estaba hecha")
    assert not any("log_acciones" in s for s, _ in conn2.sql), (
        "dejó una huella de una edición que no pasó")

    no_esta, conn3 = _con_base(filas, lambda: db.marcar_tarea_hecha(999))
    assert no_esta is False
    assert not any("UPDATE" in s for s, _ in conn3.sql)


# ── La pantalla se pinta de verdad ───────────────────────────────────────

def test_la_pantalla_se_pinta_con_todos_los_grupos():
    """No se mira el HTML con grep desde afuera: se llama a la ruta y se exige
    que la plantilla se RENDERICE. Una variable que la plantilla usa y la ruta
    no manda se cae acá — que es exactamente el fallo que dejó la portada en
    Internal Server Error con los tests en verde.
    """
    from starlette.requests import Request
    import web.app as panel

    hoy_rd = datetime.now(config.TZ).date()
    def _en(dias, hora=13):
        d = hoy_rd + timedelta(days=dias)
        return datetime(d.year, d.month, d.day, hora, tzinfo=UTC)

    filas = [_fila(1, vence_en=_en(-3)),                    # atrasada
             _fila(2, vence_en=_en(0)),                     # hoy
             _fila(3),                                      # sin fecha
             _fila(4, vence_en=_en(5)),                     # próxima
             _fila(5, estado="descartado"),                 # otro estado
             _fila(6, estado="hecha", responsable=DUENO),   # con responsable
             _fila(7)]                                      # sin responsable

    galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
    peticion = Request({"type": "http", "http_version": "1.1", "method": "GET",
                        "scheme": "https", "server": ("t", 443),
                        "path": "/tareas", "root_path": "", "query_string": b"",
                        "headers": [(b"host", b"t"),
                                    (b"cookie", galleta.encode())],
                        "app": panel.app})

    r, _ = _con_base(filas, lambda: panel.tareas(peticion))
    assert r.status_code == 200, f"/tareas devolvió {r.status_code}"
    html = r.body.decode()

    for titulo in ("Atrasadas", "Hoy", "Sin fecha", "Próximas",
                   "Otros estados"):
        assert titulo in html, f"no se pintó el grupo «{titulo}»"
    for t in range(1, 8):
        assert f"tarea {t}" in html, f"la tarea {t} no se pintó"

    assert 'action="/tareas"' in html, "no se puede guardar nada"
    assert 'name="hecha_1"' in html, "no hay casilla para cerrar la tarea"
    assert 'name="prev_1"' in html, (
        "sin el valor previo no se distingue 'no la toqué' de 'la desmarqué'")
    assert "descartado" in html, "el estado que nadie declaró no se ve"
    # La columna RESPONSABLE reemplazó a «Quién la anotó», y no se conservan
    # las dos: la vieja tiene que haberse ido de la cabecera de la tabla.
    assert "Responsable" in html, "no se pintó la columna del responsable"
    assert "Quién la anotó" not in html, (
        "la columna vieja sigue en la pantalla; Tiziano pidió cambiarla, no "
        "tener las dos")
    # Y se puede CAMBIAR desde acá, con su valor previo al lado por el mismo
    # motivo que `prev_`: un <select> siempre viaja, tocado o no.
    assert 'name="resp_1"' in html, "no se puede cambiar el responsable"
    assert 'name="prev_resp_1"' in html, (
        "sin el valor previo, cada envío reescribiría el responsable de toda "
        "la pantalla")
    # Lo ya hecho se ve hecho y no se puede volver a mandar.
    assert "checked disabled" in html


def test_un_solo_formulario_para_toda_la_pantalla():
    """Con un <form> por fila, guardar una recarga y se lleva puesto lo demás
    marcado. Y un <form> dentro de otro es HTML inválido: el navegador descarta
    el de adentro y el botón deja de hacer nada, EN SILENCIO."""
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "web", "plantillas", "tareas.html")
    html = open(ruta, encoding="utf-8").read()
    assert html.count("<form") == 1, "hay más de un formulario en la pantalla"
    assert html.count("</form>") == 1


def test_la_pantalla_esta_en_el_menu():
    """Una pantalla a la que no se llega no existe. Rosi la necesita mañana y
    nadie le va a dictar la URL."""
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "web", "plantillas", "base.html")
    assert 'href="/tareas"' in open(ruta, encoding="utf-8").read()


def test_el_humo_prueba_la_consulta_nueva():
    """Las suites son herméticas y por construcción NO ven los errores de
    acople con la base: una columna que no existe sale verde acá y revienta en
    producción. `tareas` es una tabla que hasta hoy ninguna función de db/db.py
    consultaba, así que su consulta tiene que estar en la prueba de humo."""
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "tools", "humo.py")
    assert "tareas_por_grupo" in open(ruta, encoding="utf-8").read(), (
        "tools/humo.py no corre la consulta del panel de tareas contra la base")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
