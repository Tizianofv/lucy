# -*- coding: utf-8 -*-
"""E8 — proyectos por Telegram (diseño «Lucy 1.0», §7 y §9; respuesta P11).

QUÉ SE VIGILA, y con qué camino:

  · Un proyecto NACE SOLO por la herramienta `crear_proyecto`, con nombre,
    grupo y responsable, y por la puerta `db.crear_proyecto` — la MISMA que usa
    el panel. Se corre la herramienta REAL, `cerebro/agente.py::
    _ejecutar_herramienta`, contra una base sqlite que EJECUTA DE VERDAD el SQL
    del repositorio (el mismo texto, `%s` → `?`), y se mira la fila que queda y
    la huella que deja.
  · Nombrar un proyecto que no existe NO lo crea: la tarea se rechaza entera y
    el motivo es la PREGUNTA, con las dos listas que hacen falta sacadas de
    `db.areas()` y `config.opciones_de_responsable()` (no tecleadas).
  · El prompt que ve el modelo nombra la herramienta nueva y dejó de prometer
    el alta silenciosa.
  · LOS HERMANOS SALEN DE LO REAL: se recorre el árbol sintáctico de todo el
    repo, se arma el árbol de llamadas y se pregunta qué funciones que INSERTAN
    en `proyectos` alcanza Telegram desde sus dos puertas de entrada —
    `cerebro/agente.py::_ejecutar_herramienta` (las herramientas) y
    `acciones/botones.py::al_pulsar` (el botón de la tarjeta)—.

LA FRONTERA, DICHA (medida, no supuesta):

  · SQLite no es Postgres: se compara el TEXTO y la lógica del SQL, no lo que
    Postgres haría con él. Que las columnas existan de verdad lo cubre
    `tools/humo.py`, que necesita `DATABASE_URL`.
  · El árbol de llamadas es ESTÁTICO y por nombre: ve `f(...)` y `x.f(...)`
    donde `x` es un alias de import resoluble, y las funciones del mismo
    archivo llamadas por su nombre. NO ve una llamada armada con `getattr`,
    `eval`, un diccionario de funciones ni una referencia pasada como
    argumento. En el camino de proyectos no hay ninguna: los tres creadores del
    censo salen con sus llamadores reales.
  · Los archivos de `tests/` quedan fuera del censo (son siembra, no caminos).

Correr:  <intérprete> -m pytest tests/test_crear_proyecto_telegram.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tarea_a_mano as base  # noqa: E402  (deja `psycopg` falseado)
import test_buzon_que_no_se_ve as barrido  # noqa: E402  (la puerta del barrido)

import config  # noqa: E402
import db.db as db  # noqa: E402
import acciones.crud as crud  # noqa: E402
import cerebro.agente as agente  # noqa: E402

_ROA = pathlib.Path(base.RAIZ).resolve()
DUENO = config.CHAT_ID_DUENO
ROSI = DUENO + 7


# ═══════════════════════════════════════════════════════════════════════════
# El mundo: sqlite que ejecuta el SQL del repo de verdad
# ═══════════════════════════════════════════════════════════════════════════

def _para_sqlite(valor):
    """Un parámetro tal como lo acepta sqlite.

    Postgres tiene ARRAY y JSONB, y el SQL del repo manda listas ahí
    (`tareas.anticipos_min`, `notas.etiquetas`). sqlite no sabe atarlas, así que
    acá van como su texto JSON. Es una adaptación del MUNDO, no del código: el
    SQL que se ejecuta es el mismo del repositorio.
    """
    if isinstance(valor, (list, tuple, dict)):
        return json.dumps(valor, ensure_ascii=False)
    return valor


class _Cur:
    """Cursor que traduce `%s` a `?` y devuelve dicts cuando se le pide."""

    def __init__(self, m, como_dict: bool):
        self._m = m
        self._dict = como_dict
        self._cur = None

    async def execute(self, sql, params=None):
        self._m.sql.append((" ".join(str(sql).split()), params))
        self._cur = self._m.con.execute(
            str(sql).replace("%s", "?"),
            tuple(_para_sqlite(v) for v in (params or ())))
        return self

    def _fila(self, f):
        if f is None or not self._dict:
            return f
        return dict(zip([d[0] for d in self._cur.description], f))

    async def fetchone(self):
        return self._fila(self._cur.fetchone())

    async def fetchall(self):
        return [self._fila(f) for f in self._cur.fetchall()]


class _Conn:
    def __init__(self, m):
        self._m = m

    def cursor(self, row_factory=None):
        return _Cur(self._m, row_factory is not None)

    async def execute(self, sql, params=None):
        return await _Cur(self._m, False).execute(sql, params)

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *e):
                return False
        return _T()


class Mundo:
    """La casa de mentira: proyectos, áreas, tareas, notas y el registro.

    Las columnas son las de `db/schema.sql` para las cinco tablas que toca este
    camino. `personas` no hace falta: las interpretaciones de estas pruebas no
    nombran a nadie, así que `buscar_o_crear_persona("")` sale antes de tocar
    la base. `proyectos` NO tiene columnas de cliente: el diseño dice que el
    cliente no se pide por Telegram, y una columna acá dejaría pasar un
    `cliente_*` escrito sin que nadie lo note.
    """

    def __init__(self, areas=("CDS", "ACD", "IA")):
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.sql: list = []
        self.con.executescript("""
            CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER);
            CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL,
              descripcion, estado TEXT DEFAULT 'activo', area,
              responsable_chat_id INTEGER, borrado_en);
            CREATE TABLE tareas (id INTEGER PRIMARY KEY, bandeja_id, titulo,
              detalle, vence_en, recurrencia, estado TEXT DEFAULT 'pendiente',
              proyecto_id, persona_id, anticipos_min, responsable_chat_id,
              area, primero_id, borrado_en);
            CREATE TABLE notas (id INTEGER PRIMARY KEY, bandeja_id, contenido,
              etiquetas, proyecto_id, persona_id, borrado_en);
            CREATE TABLE log_acciones (id INTEGER PRIMARY KEY, actor, accion,
              tabla, registro_id, antes, despues, motivo, bandeja_id);
        """)
        for i, clave in enumerate(areas):
            self.con.execute(
                "INSERT INTO areas (clave, color, orden) VALUES (?, ?, ?)",
                (clave, "#2b6cb0", i))

    def proyecto(self, nombre, area=None, responsable=None, borrado=False):
        c = self.con.execute(
            "INSERT INTO proyectos (nombre, area, responsable_chat_id, borrado_en)"
            " VALUES (?, ?, ?, ?)",
            (nombre, area, responsable, "2026-09-01" if borrado else None))
        return c.lastrowid

    def proyectos(self):
        return [dict(zip(("id", "nombre", "area", "responsable_chat_id"), f))
                for f in self.con.execute(
                    "SELECT id, nombre, area, responsable_chat_id FROM proyectos"
                    " ORDER BY id")]

    def tareas(self):
        return [dict(zip(("id", "titulo", "proyecto_id"), f))
                for f in self.con.execute(
                    "SELECT id, titulo, proyecto_id FROM tareas ORDER BY id")]

    def notas(self):
        return [dict(zip(("id", "contenido", "proyecto_id"), f))
                for f in self.con.execute(
                    "SELECT id, contenido, proyecto_id FROM notas ORDER BY id")]

    def huellas(self):
        return [dict(zip(("id", "actor", "accion", "tabla", "registro_id",
                          "motivo"), f))
                for f in self.con.execute(
                    "SELECT id, actor, accion, tabla, registro_id, motivo"
                    " FROM log_acciones ORDER BY id")]

    def pool(self):
        m = self

        class _CM:
            async def __aenter__(s):
                return _Conn(m)

            async def __aexit__(s, *e):
                return False

        class _P:
            def connection(s):
                return _CM()
        return _P()


def _casa(nombres=None):
    """Quién puede ser responsable y cómo se llama, en la variable de config.

    `config.NOMBRES_POR_CHAT` y `CHAT_IDS_PERMITIDOS` se leen EN CADA LLAMADA,
    así que alcanza con cambiar el atributo del módulo; el fixture de
    `conftest.py` los devuelve a su sitio después de cada prueba.
    """
    nombres = nombres or {DUENO: "Tiziano", ROSI: "Rosi"}
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = tuple(nombres)


def _correr(m, fn):
    guardado = db.pool
    db.pool = m.pool()
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(fn())
    finally:
        bucle.close()
        db.pool = guardado


def _herramienta(m, nombre, args, bandeja_id=1):
    """Corre una herramienta REAL y devuelve (resultado, parte de lo escrito)."""
    acciones: list = []
    r = _correr(m, lambda: agente._ejecutar_herramienta(
        nombre, args, bandeja_id, acciones))
    return r, acciones


# ═══════════════════════════════════════════════════════════════════════════
# 1) Nombrar un proyecto que no existe NO lo crea: se pregunta
# ═══════════════════════════════════════════════════════════════════════════

def test_una_tarea_que_nombra_un_proyecto_inexistente_no_lo_crea_y_pregunta():
    """El defecto que E8 viene a cerrar: hasta el 1-oct-2026, nombrar un
    proyecto que no existía lo creaba al vuelo SIN GRUPO, SIN CLIENTE Y SIN
    RESPONSABLE, y en la página nueva ese proyecto caía en «Sin grupo»."""
    _casa()
    m = Mundo()
    r, acciones = _herramienta(m, "crear", {
        "clasificacion": "tarea", "titulo": "Grabar la intro",
        "proyecto": "Proyecto Que No Existe"})

    assert r.startswith("ERROR"), r
    assert m.proyectos() == [], (
        f"creó el proyecto que Tiziano no pidió crear: {m.proyectos()}")
    assert m.tareas() == [], (
        f"creó la tarea sin proyecto, como si no hubiera dicho nada: {m.tareas()}")
    assert m.huellas() == [], m.huellas()
    assert acciones == [], "anotó una escritura que no ocurrió"


def test_la_pregunta_trae_los_grupos_y_los_responsables_de_verdad():
    """El motivo ES la pregunta, con las dos listas puestas: si el modelo se
    limita a repetirlo, Tiziano igual lee qué opciones hay. Las listas salen de
    `db.areas()` y de `config.opciones_de_responsable()`, no tecleadas."""
    _casa()
    m = Mundo(areas=("CDS", "ACD", "IA"))
    r, _ = _herramienta(m, "crear", {
        "clasificacion": "tarea", "titulo": "x", "proyecto": "Nada"})
    for grupo in ("CDS", "ACD", "IA"):
        assert grupo in r, (grupo, r)
    for quien in ("Tiziano", "Rosi", "Code"):
        assert quien in r, (quien, r)
    # La lista OFRECIDA es lo que va entre paréntesis: ahí no puede estar «sin
    # responsable», que al crear no existe (Tiziano, 1-oct-2026: «somos
    # siempre Rosi, Yo o Code»). Se mira esa lista y no el texto entero, que
    # dice «nacería sin grupo y sin responsable» al explicar el porqué.
    ofrecidos = r[r.index("quién es el responsable (") + 25:]
    assert "sin responsable" not in ofrecidos[:ofrecidos.index(")")], ofrecidos
    assert "crear_proyecto" in r, f"no dice por dónde sí se crea: {r}"


def test_la_pregunta_cambia_sola_si_cambian_los_grupos():
    """La prueba que le da a la guarda una entrada que no está en el repo: con
    otras áreas declaradas, la pregunta ofrece ÉSAS. Si alguien tecleara
    «CDS, ACD o IA» en el texto, esto se pone rojo."""
    _casa()
    m = Mundo(areas=("Banda", "Estudio"))
    r, _ = _herramienta(m, "crear", {
        "clasificacion": "tarea", "titulo": "x", "proyecto": "Nada"})
    assert "Banda" in r and "Estudio" in r, r
    assert "ACD" not in r and "IA" not in r, (
        f"ofreció grupos que no están declarados: {r}")


def test_con_el_proyecto_existiendo_la_tarea_si_se_crea_y_se_enlaza():
    """El control que hace que la de arriba signifique algo: si el proyecto
    existe, el camino de siempre funciona igual. Sin esto, una guarda que
    rechazara TODO pasaría por buena."""
    _casa()
    m = Mundo()
    pid = m.proyecto("Álbum nuevo", area="CDS")

    r, _ = _herramienta(m, "crear", {
        "clasificacion": "tarea", "titulo": "Grabar la intro",
        "proyecto": "Álbum nuevo"})

    assert r.startswith("OK"), r
    assert len(m.tareas()) == 1, m.tareas()
    assert m.tareas()[0]["proyecto_id"] == pid, m.tareas()
    assert len(m.proyectos()) == 1, "creó un segundo proyecto parecido"


def test_el_proyecto_se_busca_sin_importar_mayusculas_como_siempre():
    _casa()
    m = Mundo()
    m.proyecto("Álbum nuevo", area="CDS")
    r, _ = _herramienta(m, "crear", {
        "clasificacion": "tarea", "titulo": "x", "proyecto": "  ÁLBUM NUEVO "})
    assert r.startswith("OK"), r
    assert len(m.proyectos()) == 1, m.proyectos()


def test_una_nota_con_un_proyecto_inexistente_se_guarda_sin_enlazar_y_sin_crearlo():
    """FRONTERA DICHA: el corte entero es solo para TAREAS — donde el proyecto
    es de lo que trata el pedido. En una nota (y en una cita o un movimiento)
    el proyecto es un enlace secundario: se guarda la nota sin proyecto antes
    que perder el registro por un nombre, y el proyecto NO se crea."""
    _casa()
    m = Mundo()
    r, _ = _herramienta(m, "crear", {
        "clasificacion": "nota", "titulo": "una idea", "detalle": "una idea",
        "proyecto": "Proyecto Que No Existe"})
    assert r.startswith("OK"), r
    assert m.proyectos() == [], m.proyectos()
    n, = m.notas()
    assert n["proyecto_id"] is None, n


# ═══════════════════════════════════════════════════════════════════════════
# 2) La herramienta nueva: crea por la puerta, y rechaza lo que no vale
# ═══════════════════════════════════════════════════════════════════════════

def test_crear_proyecto_con_datos_buenos_crea_por_la_puerta_con_su_huella():
    _casa()
    m = Mundo()
    r, acciones = _herramienta(m, "crear_proyecto", {
        "nombre": "  Documental  ", "grupo": "ACD", "responsable": "Rosi"})

    filas = m.proyectos()
    assert len(filas) == 1, filas
    f = filas[0]
    assert (f["nombre"], f["area"], f["responsable_chat_id"]) == (
        "Documental", "ACD", ROSI), f
    h, = m.huellas()
    assert (h["accion"], h["tabla"], h["registro_id"]) == (
        "crear", "proyectos", f["id"]), h
    assert json.loads(m.con.execute(
        "SELECT despues FROM log_acciones").fetchone()[0])["nombre"] == (
        "Documental"), "la huella no guarda la fila que quedó"
    # El asa: el parte y el «acción #N» que ve el modelo.
    assert re.search(r"acción #\d+, reversible", r), r
    assert "#None" not in r, r
    assert [a["log_id"] for a in acciones] == [h["id"]], acciones
    assert "Documental" in acciones[0]["que"], acciones


def test_crear_proyecto_sin_cliente_y_el_cliente_no_se_pide_por_telegram():
    """Tiziano (1-oct-2026): «no todo necesita cliente». La fila nace sin
    cliente y el texto no promete uno que no hay."""
    _casa()
    m = Mundo()
    r, _ = _herramienta(m, "crear_proyecto", {
        "nombre": "Interno", "grupo": "IA", "responsable": "Code"})
    assert config.CHAT_ID_CODE == m.proyectos()[0]["responsable_chat_id"]
    assert "cliente" not in r.lower(), f"prometió un cliente: {r}"


def test_crear_proyecto_rechaza_un_grupo_que_no_existe():
    _casa()
    m = Mundo(areas=("CDS", "ACD", "IA"))
    r, acciones = _herramienta(m, "crear_proyecto", {
        "nombre": "Uno", "grupo": "Inventado", "responsable": "Rosi"})
    assert r.startswith("ERROR"), r
    assert m.proyectos() == [] and m.huellas() == [], m.proyectos()
    assert acciones == []
    assert "CDS" in r and "ACD" in r and "IA" in r, (
        f"el rechazo no dice qué grupos hay: {r}")


def test_crear_proyecto_rechaza_un_responsable_que_no_es_de_la_casa():
    _casa()
    m = Mundo()
    for quien in ("nadie", "", None, "Pedro el de la esquina", 12345):
        r, _ = _herramienta(m, "crear_proyecto", {
            "nombre": "Uno", "grupo": "CDS", "responsable": quien})
        assert r.startswith("ERROR"), (quien, r)
        assert m.proyectos() == [], (quien, m.proyectos())
        assert m.huellas() == [], (quien, m.huellas())
        assert "Tiziano" in r and "Rosi" in r, (
            f"el rechazo no dice a quién sí ({quien!r}): {r}")


def test_crear_proyecto_rechaza_un_nombre_vacio_o_repetido():
    _casa()
    m = Mundo()
    m.proyecto("Ya existe", area="CDS")
    for nombre in ("", "   ", "Ya existe", "  ya EXISTE "):
        r, _ = _herramienta(m, "crear_proyecto", {
            "nombre": nombre, "grupo": "CDS", "responsable": "Rosi"})
        assert r.startswith("ERROR"), (nombre, r)
    assert len(m.proyectos()) == 1, m.proyectos()


def test_un_proyecto_en_la_papelera_no_estorba_para_crear_otro_con_su_nombre():
    """La puerta compara contra los VIVOS: un proyecto borrado no reserva su
    nombre (`proyecto_vivo_con_nombre`)."""
    _casa()
    m = Mundo()
    m.proyecto("En la papelera", area="CDS", borrado=True)
    r, _ = _herramienta(m, "crear_proyecto", {
        "nombre": "En la papelera", "grupo": "CDS", "responsable": "Rosi"})
    assert r.startswith("OK"), r
    assert len(m.proyectos()) == 2, m.proyectos()


def test_el_perfil_ya_no_crea_proyectos_pero_si_anota_los_que_existen():
    _casa()
    m = Mundo()
    r, _ = _herramienta(m, "perfil", {
        "tipo": "proyecto", "nombre": "Nuevo proyecto", "nota": "es de ACD"})
    assert r.startswith("ERROR"), r
    assert m.proyectos() == [], m.proyectos()
    assert m.huellas() == [], "anotó una escritura que no ocurrió"
    assert "crear_proyecto" in r, f"no dice por dónde sí: {r}"

    pid = m.proyecto("Viejo", area="CDS")
    r, _ = _herramienta(m, "perfil", {
        "tipo": "proyecto", "nombre": "Viejo", "descripcion": "del estudio"})
    assert r.startswith("OK"), r
    assert m.con.execute("SELECT descripcion FROM proyectos WHERE id = ?",
                         (pid,)).fetchone()[0] == "del estudio"


# ═══════════════════════════════════════════════════════════════════════════
# 3) El prompt que ve el modelo
# ═══════════════════════════════════════════════════════════════════════════

def _bloque_de(herramienta: str, texto: str | None = None) -> str:
    """El trozo de `HERRAMIENTAS` que describe esa herramienta: desde su
    renglón `· <nombre>  ` hasta el siguiente `· `."""
    texto = agente.HERRAMIENTAS if texto is None else texto
    inicio = texto.index(f"\n· {herramienta}  ")
    resto = texto[inicio + 3:]
    fin = resto.find("\n· ")
    return resto if fin < 0 else resto[:fin]


def test_el_prompt_describe_la_herramienta_nueva_con_sus_tres_campos():
    bloque = _bloque_de("crear_proyecto")
    for campo in ('"nombre"', '"grupo"', '"responsable"'):
        assert campo in bloque, (campo, bloque)
    assert "{AREAS}" in bloque and "{PERSONAS_Y_CODE}" in bloque, (
        "el grupo y el responsable tienen que salir de su lista real, no de "
        "un texto tecleado")
    assert "cliente" in bloque.lower(), (
        "el prompt tiene que decir que el cliente NO se pide por acá")


def test_el_prompt_ya_no_promete_que_nombrar_un_proyecto_lo_crea():
    """El defecto, en el texto que lee el modelo: `crear` decía «Si de verdad
    no existe, se crea solo, con ese nombre». Eso ya no es verdad."""
    bloque = _bloque_de("crear")
    assert "se crea solo" not in bloque, (
        "sigue prometiendo el alta silenciosa que E8 vino a cerrar")
    assert "crear_proyecto" in bloque, (
        "tiene que decir por dónde sí se crea")


def test_el_prompt_del_perfil_avisa_que_por_ahi_no_nace_ningun_proyecto():
    assert "crear_proyecto" in _bloque_de("perfil")


def test_las_listas_del_prompt_salen_de_su_fuente_y_no_estan_tecleadas():
    """El prompt armado de verdad ofrece los grupos y los responsables que hay.
    `herramientas_del_prompt` es SÍNCRONA y recibe las áreas por parámetro
    (quien llama ya las trajo de la base); los nombres salen de la variable."""
    config.NOMBRES_POR_CHAT = {DUENO: "Zutana"}
    config.CHAT_IDS_PERMITIDOS = (DUENO,)

    texto = agente.herramientas_del_prompt([{"clave": "Banda", "color": "#1"}])
    bloque = _bloque_de("crear_proyecto", texto)
    assert "Banda" in bloque, bloque
    assert "ACD" not in bloque and "IA" not in bloque, bloque
    assert "Zutana" in bloque, bloque


def test_la_herramienta_nueva_esta_en_la_lista_que_ve_el_modelo():
    assert "crear_proyecto" in agente.HERRAMIENTAS_QUE_HAY


def test_la_herramienta_esta_registrada_en_el_despachador():
    """La lista de arriba sola no basta: tiene que haber una rama que la
    atienda, o el modelo pediría una herramienta que no existe."""
    arbol = ast.parse(pathlib.Path(
        agente.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(arbol)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_ejecutar_herramienta")
    atendidas = {
        n.test.comparators[0].value
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name) and n.test.left.id == "nombre"
        and isinstance(n.test.comparators[0], ast.Constant)}
    assert "crear_proyecto" in atendidas, sorted(atendidas)


# ═══════════════════════════════════════════════════════════════════════════
# 4) Los hermanos: quién más puede crear un proyecto desde Telegram
# ═══════════════════════════════════════════════════════════════════════════
#
# LA LISTA SALE DE LO REAL, y por los dos lados:
#   · los CREADORES, del árbol sintáctico: toda función cuyo texto SQL hace un
#     `INSERT INTO proyectos (...)`. Y también el ESCRITOR GENÉRICO —aquel cuyo
#     SQL arma la tabla al vuelo, `INSERT INTO {tabla} (...)`, que es como
#     escribe `db._buscar_o_crear`—, junto con quien le pasa el literal
#     «proyectos»: ese envoltorio crea proyectos aunque su SQL no nombre la
#     tabla.
#   · lo que ALCANZA TELEGRAM, del árbol de llamadas: desde las dos puertas de
#     entrada, todo lo que se puede llamar siguiendo `f(...)` y `x.f(...)`.
#
# Un f-string NO es un `ast.Constant`: un censo que solo mirara `ast.Constant`
# no vería al escritor genérico, que es justo el que se pierde en un `grep`.

_INSERT = re.compile(r"INSERT\s+INTO\s+([\w{}\[…\]]+)\s*\(", re.I)


def _sql_de_una_funcion(fn) -> list[str]:
    """Todo el texto SQL que hay dentro de una función: las cadenas literales
    y los f-strings rearmados, con `{…}` donde interpolan."""
    salida = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            salida.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            salida.append("".join(
                p.value if isinstance(p, ast.Constant) else "{…}"
                for p in n.values))
    return salida


def _modulos(raiz):
    """[(ruta_relativa, árbol)] de todo `.py` del repo, menos `tests/`.

    La lista sale de `test_buzon_que_no_se_ve._py_en_disco`, que es la puerta
    del barrido: le pregunta a cada carpeta si es un entorno virtual en vez de
    tener una lista de nombres.
    """
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    salida = []
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        salida.append((real.relative_to(raiz).as_posix(),
                       ast.parse(real.read_text(encoding="utf-8"), str(real))))
    return salida


def _funciones(arbol):
    return [n for n in ast.walk(arbol)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _creadores_de_proyectos(raiz):
    """`(creadores, genericos)`: quién puede INSERTAR una fila en `proyectos`."""
    modulos = _modulos(raiz)
    directos, genericos = set(), set()
    for rel, arbol in modulos:
        for fn in _funciones(arbol):
            for texto in _sql_de_una_funcion(fn):
                for m in _INSERT.finditer(texto):
                    tabla = m.group(1)
                    if tabla.lower() == "proyectos":
                        directos.add((rel, fn.name))
                    elif "…" in tabla:          # la tabla se arma al vuelo
                        genericos.add((rel, fn.name))

    nombres_genericos = {n for _, n in genericos}
    por_generico = set()
    for rel, arbol in modulos:
        for fn in _funciones(arbol):
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and n.args):
                    continue
                nombre = (n.func.attr if isinstance(n.func, ast.Attribute)
                          else n.func.id if isinstance(n.func, ast.Name) else None)
                # El literal «proyectos» puede ir en cualquier posición
                # (`_buscar_o_crear("proyectos", …)` lo lleva primero; un
                # envoltorio que recibe la conexión, segundo), así que se mira
                # la llamada entera y no un argumento concreto.
                if nombre in nombres_genericos and any(
                        isinstance(a, ast.Constant) and a.value == "proyectos"
                        for a in n.args):
                    por_generico.add((rel, fn.name))
    return directos | por_generico, genericos


def _alias_del_modulo(arbol) -> dict:
    """{alias: ruta_relativa} de los imports de un módulo.

    FRONTERA DICHA: `import a.b.c as x` —la forma que usa este repo— resuelve
    `x` a `a/b/c.py`; `from a.b import c` resuelve `c` a `a/b/c.py`. Un
    `import a.b` a secas ata el nombre `a` y se resuelve al mismo archivo: es
    una aproximación, y si algún día un módulo importara así dos paquetes
    distintos, el grafo se quedaría con uno.
    """
    salida = {}
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            for a in n.names:
                salida[a.asname or a.name.split(".")[0]] = (
                    a.name.replace(".", "/") + ".py")
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                salida[a.asname or a.name] = (
                    n.module.replace(".", "/") + "/" + a.name + ".py")
    return salida


def _llamadas(fn) -> set:
    """Los nombres que una función LLAMA: `(None, f)` para `f(...)` y
    `(x, f)` para `x.f(...)`."""
    salida = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name):
            salida.add((None, n.func.id))
        elif (isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)):
            salida.add((n.func.value.id, n.func.attr))
    return salida


def _alcanzables(raiz, entradas) -> set:
    """Lo que se puede llamar desde esas puertas de entrada, por el árbol de
    llamadas del repo. `entradas` son `(ruta, función)`."""
    modulos = _modulos(raiz)
    alias = {rel: _alias_del_modulo(arbol) for rel, arbol in modulos}
    por_modulo = {rel: {fn.name for fn in _funciones(arbol)}
                  for rel, arbol in modulos}
    cuerpos = {(rel, fn.name): fn for rel, arbol in modulos
               for fn in _funciones(arbol)}
    rutas = {rel for rel, _ in modulos}

    vistos, pila = set(), list(entradas)
    while pila:
        nodo = pila.pop()
        if nodo in vistos or nodo not in cuerpos:
            continue
        vistos.add(nodo)
        rel = nodo[0]
        for cual, nombre in _llamadas(cuerpos[nodo]):
            if cual is None:
                if nombre in por_modulo.get(rel, ()):
                    pila.append((rel, nombre))
                continue
            destino = alias.get(rel, {}).get(cual)
            if destino in rutas and nombre in por_modulo.get(destino, ()):
                pila.append((destino, nombre))
    return vistos


ENTRADAS_DE_TELEGRAM = (
    ("cerebro/agente.py", "_ejecutar_herramienta"),   # las herramientas
    ("acciones/botones.py", "al_pulsar"),             # el botón de la tarjeta
)


def test_ningun_otro_camino_de_telegram_crea_un_proyecto():
    creadores, genericos = _creadores_de_proyectos(_ROA)
    assert genericos, (
        "el censo no vio al escritor genérico (`INSERT INTO {tabla}`): sin él "
        "estaría verde sin mirar el camino que se pierde en un grep")

    alcanzables = _alcanzables(_ROA, ENTRADAS_DE_TELEGRAM)
    assert ("cerebro/agente.py", "_ejecutar_herramienta") in alcanzables, (
        "el árbol de llamadas no ve ni su propia puerta de entrada: dejó de "
        "medir")
    # Y LLEGA HONDO, hasta los DOS que creaban proyectos hasta E8: `crear` por
    # `crear_desde_interpretacion` (que creaba al nombrar) y el perfil. Si el
    # recorrido se quedara corto, la prueba de abajo pasaría por no ver nada.
    for antes_creaba in (("acciones/crud.py", "crear_desde_interpretacion"),
                         ("acciones/crud.py", "perfil")):
        assert antes_creaba in alcanzables, (antes_creaba, sorted(alcanzables))
    # Y el escritor GENÉRICO sigue siendo alcanzable —lo usa `buscar_o_crear_
    # persona`, que Telegram sí llama—: lo que lo salva no es que no se llegue,
    # es que nadie le pasa «proyectos» (`buscar_o_crear_proyecto` no está en
    # `alcanzables`, y eso lo dice el `assert not culpables` de abajo).
    assert ("db/db.py", "_buscar_o_crear") in alcanzables, sorted(alcanzables)

    # La ÚNICA excepción, y no es un perdón: es la herramienta que E8 agrega.
    unico = ("db/db.py", "crear_proyecto")
    assert unico in alcanzables, (
        "Telegram ya no llega a la puerta de crear un proyecto: la excepción "
        "de abajo estaría tapando un camino que no existe")
    culpables = sorted(creadores & alcanzables - {unico})
    assert not culpables, (
        f"estos caminos alcanzables desde Telegram crean un proyecto sin grupo "
        f"ni responsable: {culpables}")


def test_el_censo_de_creadores_ve_a_los_de_hoy():
    """EL TRINQUETE, para que el de arriba no se quede verde por no ver nada:
    hoy pueden crear un proyecto estos tres. Uno nuevo se pone rojo acá hasta
    que alguien lo mire."""
    creadores, genericos = _creadores_de_proyectos(_ROA)
    assert sorted(n for _, n in creadores) == [
        "buscar_o_crear_proyecto", "convertir_tarea_en_proyecto",
        "crear_proyecto"], sorted(creadores)
    assert sorted(n for _, n in genericos) == ["_buscar_o_crear"], sorted(genericos)


def test_el_censo_ve_un_creador_inventado(tmp_path):
    """La prueba que le da al censo una entrada que no está en el repo."""
    (tmp_path / "falso.py").write_text(
        "async def inventado(c):\n"
        "    await c.execute('INSERT INTO proyectos (nombre) VALUES (1)')\n",
        encoding="utf-8")
    creadores, _ = _creadores_de_proyectos(tmp_path)
    assert ("falso.py", "inventado") in creadores, creadores


def test_el_censo_ve_un_escritor_generico_inventado(tmp_path):
    """El que un `grep` de `INSERT INTO proyectos` no encuentra."""
    (tmp_path / "falso.py").write_text(
        "async def generico(c, tabla):\n"
        "    await c.execute(f'INSERT INTO {tabla} (nombre) VALUES (1)')\n"
        "async def envoltorio(c):\n"
        "    await generico(c, 'proyectos')\n",
        encoding="utf-8")
    creadores, genericos = _creadores_de_proyectos(tmp_path)
    assert ("falso.py", "generico") in genericos, genericos
    assert ("falso.py", "envoltorio") in creadores, creadores


def test_el_grafo_de_llamadas_ve_una_arista_inventada(tmp_path):
    (tmp_path / "a.py").write_text(
        "import b as b\n\nasync def entrada():\n    await b.hondo()\n",
        encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "async def hondo():\n    return 1\n", encoding="utf-8")
    alcanzables = _alcanzables(tmp_path, [("a.py", "entrada")])
    assert ("b.py", "hondo") in alcanzables, alcanzables
