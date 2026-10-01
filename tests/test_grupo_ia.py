"""El grupo «IA» (Lucy 1.0, encargo E1, 1-oct-2026): G1, G2 y G3.

«🛠️ Técnico» pasó a llamarse «IA» y «🏠 Personal» desapareció. La base lo
cambia con dos migraciones (`2026-10-01_area_ia.sql` y
`2026-10-01b_area_ia_mover_y_borrar.sql`) y el código con `db.AREA_TECNICA`.
Lo que esta prueba impide:

  G1  La puerta de Code (`tareas_de_code_pendientes`, `cerrar_tarea_de_la_sala`,
      `tomar_tarea_de_la_sala`) sigue viendo las tareas de Code DESPUÉS de las
      migraciones. Se corren las tres funciones REALES de `db/db.py`, con su
      SQL, contra una base donde se aplicaron las migraciones en orden.
  G2  Al aplicar todas las migraciones en orden quedan exactamente CDS, ACD e
      IA, y `db.AREA_TECNICA` es una de ellas. Se prueba también qué hace M1b
      (papelera, mover, borrar), que es idempotente y que falla fuerte si falta
      el área «IA».
  G3  El nombre viejo no queda escrito en el código ni en los textos.

LA FRONTERA, dicha una vez y sin adjetivos:

  · NO hay Postgres en esta máquina (no existe el binario `postgres`; solo las
    herramientas cliente de libpq) y la suite entera es hermética. Por eso las
    migraciones se aplican sobre SQLite, y de cada archivo solo se EJECUTAN las
    sentencias DML (`INSERT`/`UPDATE`/`DELETE` sobre `areas`, `proyectos`,
    `tareas` o `log_acciones`) que nombran `area` o `areas`. El DDL
    (`CREATE TABLE`, `ALTER TABLE`, `COMMENT`, ...) no se ejecuta: las tablas se
    crean con las columnas que declara `db.columnas_declaradas()` (sale de
    `db/schema.sql` y las migraciones, no se teclean) más la FK de `area` y el
    `CHECK tareas_area_no_con_proyecto`, que sí se escriben acá a mano.
  · SQLite no trae `now()` ni `jsonb_build_object`; se registran dos funciones
    con ese nombre. Es lo único que se imita del dialecto de Postgres.
  · Lo que no se ve acá: que las migraciones corran en Postgres de verdad (eso
    lo ve `tools/humo.py` y el conteo previo del guion de la sala).

Correr:  python3 -m pytest tests/test_grupo_ia.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-grupo-ia")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object()
_psycopg.rows = _rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
_pool_mod = types.ModuleType("psycopg_pool")


class _StubPool:
    def __init__(self, *a, **k):
        pass


_pool_mod.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _pool_mod)

_openai = types.ModuleType("openai")


class _StubAsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _StubAsyncOpenAI
sys.modules.setdefault("openai", _openai)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
import db.db as db  # noqa: E402
import cerebro.agente as agente  # noqa: E402

_MIGRACIONES = _ROOT / "db" / "migrations"

# Los tres colores de la maqueta aprobada de Proyectos (diseño de Lucy 1.0,
# §3.1): es una decisión de Tiziano, por eso se escribe acá.
_COLORES_PEDIDOS = {"CDS": "#0f7c74", "ACD": "#b5611a", "IA": "#8a4a8f"}


# ═══════════════════════════════════════════════════════════════════════
# Aplicar las migraciones sobre SQLite
# ═══════════════════════════════════════════════════════════════════════

def _sentencias(texto: str) -> list[str]:
    """Parte un archivo SQL en sentencias: quita los comentarios `--` y corta
    en `;`, sin mirar dentro de las comillas simples."""
    sentencias, actual = [], []
    en_texto = False
    i = 0
    while i < len(texto):
        c = texto[i]
        if en_texto:
            actual.append(c)
            if c == "'":
                if i + 1 < len(texto) and texto[i + 1] == "'":
                    actual.append("'")
                    i += 1
                else:
                    en_texto = False
        elif c == "'":
            en_texto = True
            actual.append(c)
        elif c == "-" and texto[i:i + 2] == "--":
            while i < len(texto) and texto[i] != "\n":
                i += 1
            continue
        elif c == ";":
            s = "".join(actual).strip()
            if s:
                sentencias.append(s)
            actual = []
        else:
            actual.append(c)
        i += 1
    s = "".join(actual).strip()
    if s:
        sentencias.append(s)
    return sentencias


_DML = re.compile(
    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(areas|proyectos|tareas|log_acciones)\b", re.I)
_MENCIONA_AREA = re.compile(r"\bareas?\b", re.I)


def _las_que_se_ejecutan(archivo: pathlib.Path) -> list[str]:
    return [s for s in _sentencias(archivo.read_text(encoding="utf-8"))
            if _DML.match(s) and _MENCIONA_AREA.search(s)]


def _migraciones() -> list[pathlib.Path]:
    return sorted(_MIGRACIONES.glob("*.sql"), key=lambda p: p.name)


def _borra_areas(archivo: pathlib.Path) -> bool:
    return any(re.match(r"\s*DELETE\s+FROM\s+areas\b", s, re.I)
               for s in _las_que_se_ejecutan(archivo))


def _hacia_sqlite(sql: str) -> str:
    return sql.replace("%s", "?")


def _base_vacia() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.create_function("now", 0, lambda: "2026-10-01T12:00:00+00:00")
    con.create_function(
        "jsonb_build_object", -1,
        lambda *a: json.dumps(dict(zip(a[::2], a[1::2])), ensure_ascii=False))
    declaradas = db.columnas_declaradas()
    con.execute("CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, "
                "orden INTEGER)")
    for tabla in ("proyectos", "tareas", "log_acciones"):
        columnas = []
        for c in declaradas[tabla]:
            if c == "id":
                columnas.append("id INTEGER PRIMARY KEY")
            elif c == "area":
                columnas.append("area TEXT REFERENCES areas(clave)")
            else:
                columnas.append(c)
        if tabla == "tareas":
            columnas.append(
                "CONSTRAINT tareas_area_no_con_proyecto "
                "CHECK (proyecto_id IS NULL OR area IS NULL)")
        con.execute(f"CREATE TABLE {tabla} ({', '.join(columnas)})")
    return con


def _aplicar(con: sqlite3.Connection, archivo: pathlib.Path) -> set[str]:
    """Ejecuta las sentencias DML de `archivo` en una transacción. Devuelve
    todas las claves de `areas` que hubo en algún momento."""
    vistas: set[str] = set()
    for s in _las_que_se_ejecutan(archivo):
        con.execute(s)
        vistas |= {f["clave"] for f in con.execute("SELECT clave FROM areas")}
    con.commit()
    return vistas


def _areas(con) -> dict[str, str]:
    return {f["clave"]: f["color"]
            for f in con.execute("SELECT clave, color FROM areas")}


def _todo_en_orden() -> tuple[sqlite3.Connection, set[str]]:
    con = _base_vacia()
    vistas: set[str] = set()
    for archivo in _migraciones():
        vistas |= _aplicar(con, archivo)
    return con, vistas


def _nombres_viejos() -> set[str]:
    """Las claves que alguna migración sembró y que después de aplicarlas todas
    ya no existen. Salen de reproducir las migraciones, no de una lista."""
    con, vistas = _todo_en_orden()
    return vistas - set(_areas(con))


def _con_estado_previo(sembrar) -> sqlite3.Connection:
    """Reproduce lo que hay en producción antes de M1b: todas las migraciones
    hasta la que borra las áreas viejas (sin incluirla), se siembra, y se
    aplican esa y las que siguen."""
    con = _base_vacia()
    archivos = _migraciones()
    corte = next((i for i, a in enumerate(archivos) if _borra_areas(a)),
                 len(archivos))
    for archivo in archivos[:corte]:
        _aplicar(con, archivo)
    sembrar(con)
    con.commit()
    for archivo in archivos[corte:]:
        _aplicar(con, archivo)
    return con


def _viejas() -> tuple[str, str]:
    viejas = _nombres_viejos()
    tecnico = [n for n in viejas if "Técnico" in n]
    personal = [n for n in viejas if "Personal" in n]
    assert len(viejas) == 2 and len(tecnico) == 1 and len(personal) == 1, (
        f"las migraciones deberían dejar sin existir exactamente las dos áreas "
        f"viejas (la técnica y la personal); dejaron sin existir: "
        f"{sorted(viejas)!r}")
    return tecnico[0], personal[0]


# ═══════════════════════════════════════════════════════════════════════
# El escenario de antes de M1b (inventado, sin datos reales)
# ═══════════════════════════════════════════════════════════════════════

CODE = config.CHAT_ID_CODE
HUMANO = config.CHAT_ID_DUENO


def _insertar(con, tabla, **campos):
    cols = ", ".join(campos)
    marcas = ", ".join("?" for _ in campos)
    con.execute(f"INSERT INTO {tabla} ({cols}) VALUES ({marcas})",
                tuple(campos.values()))


def _sembrar(tecnico: str, personal: str):
    def hacer(con):
        pr = lambda id, nombre, area, borrado_en=None: _insertar(   # noqa: E731
            con, "proyectos", id=id, nombre=nombre, estado="activo",
            area=area, borrado_en=borrado_en)
        pr(1, "Proyecto técnico con tareas", tecnico)
        pr(2, "Proyecto personal vacío", personal)
        pr(3, "Proyecto de CDS", "CDS")
        pr(4, "Proyecto personal con una tarea viva", personal)
        pr(5, "Proyecto personal ya en la papelera", personal,
           "2026-01-01T00:00:00+00:00")

        def ta(id, estado="pendiente", proyecto_id=None, area=None,
               responsable=CODE, borrado_en=None, tomada_en=None):
            _insertar(con, "tareas", id=id, titulo=f"tarea {id}", estado=estado,
                      proyecto_id=proyecto_id, area=area,
                      responsable_chat_id=responsable, borrado_en=borrado_en,
                      tomada_en=tomada_en, bandeja_id=9000 + id)

        ta(10, proyecto_id=1)                        # de Code, en el proyecto técnico
        ta(11, proyecto_id=2, borrado_en="2026-01-01T00:00:00+00:00")  # la tarea vieja del proyecto vacío
        ta(12, area=tecnico)                         # de Code, área propia técnica
        ta(13, estado="hecha", area=tecnico)         # hecha: no se ve
        ta(14, area=personal, responsable=None)      # personal, sin responsable
        ta(15, area=personal)                        # de Code, área propia personal
        ta(16, proyecto_id=4)                        # de Code, en el proyecto personal
        ta(17, area="CDS")                           # de Code pero de CDS: nunca
        ta(18, area=tecnico, responsable=HUMANO)     # técnica pero de una persona: nunca
        ta(19)                                       # de Code, sin área ni proyecto: nunca
    return hacer


# Lo que la puerta de Code tiene que ver tras las migraciones: pendientes, de
# Code, con área efectiva IA, no borradas. Escrito a mano desde el escenario.
_VISIBLES_PARA_CODE = [10, 12, 15, 16]


# ═══════════════════════════════════════════════════════════════════════
# G2: las migraciones en orden dejan CDS, ACD e IA
# ═══════════════════════════════════════════════════════════════════════

def test_G2_todas_las_migraciones_en_orden_dejan_cds_acd_e_ia():
    con, _ = _todo_en_orden()
    assert set(_areas(con)) == {"CDS", "ACD", "IA"}, (
        f"quedaron estas áreas: {sorted(_areas(con))!r}")


def test_G2_el_area_tecnica_del_codigo_es_una_de_las_que_quedan():
    con, _ = _todo_en_orden()
    assert db.AREA_TECNICA in _areas(con), (
        f"db.AREA_TECNICA={db.AREA_TECNICA!r} no existe en la base que dejan "
        f"las migraciones: {sorted(_areas(con))!r}. Una tarea de Code con "
        f"esa área reventaría la FK al crearse.")


def test_G2_los_colores_son_los_de_la_maqueta():
    con, _ = _todo_en_orden()
    assert _areas(con) == _COLORES_PEDIDOS


def test_G2_el_archivo_de_M1b_corre_despues_del_que_siembra_las_viejas():
    """La trampa del diseño: `2026-09-22_areas.sql` resembraría las viejas si
    corriera después. El orden de nombre es el orden en que se aplican."""
    siembran = [a.name for a in _migraciones()
                if any(re.match(r"\s*INSERT\s+INTO\s+areas", s, re.I)
                       and ("Técnico" in s or "Personal" in s)
                       for s in _las_que_se_ejecutan(a))]
    borran = [a.name for a in _migraciones() if _borra_areas(a)]
    assert siembran and borran
    assert max(siembran) < min(borran), (
        f"una migración que borra las áreas viejas ({borran}) corre ANTES "
        f"que una que las siembra ({siembran}): quedarían resembradas")


def test_G2_M1b_con_el_escenario_de_antes_mueve_borra_y_manda_a_la_papelera():
    tecnico, personal = _viejas()
    con = _con_estado_previo(_sembrar(tecnico, personal))

    assert set(_areas(con)) == {"CDS", "ACD", "IA"}
    proyectos = {f["id"]: dict(f) for f in con.execute("SELECT * FROM proyectos")}
    # Ningún proyecto ni tarea apunta a un área que ya no existe, y los de CDS
    # no se tocaron.
    assert {p["area"] for p in proyectos.values()} == {"IA", "CDS"}
    assert proyectos[3]["area"] == "CDS" and proyectos[3]["borrado_en"] is None
    for pid in (1, 2, 4, 5):
        assert proyectos[pid]["area"] == "IA", proyectos[pid]
    # Papelera: solo el personal VACÍO y vivo (el 2). El que tiene una tarea
    # viva (4) sigue vivo, y el que ya estaba en la papelera (5) conserva su fecha.
    assert proyectos[2]["borrado_en"] is not None
    assert proyectos[1]["borrado_en"] is None
    assert proyectos[4]["borrado_en"] is None
    assert proyectos[5]["borrado_en"] == "2026-01-01T00:00:00+00:00"
    tareas = {f["id"]: dict(f) for f in con.execute("SELECT * FROM tareas")}
    assert {t["area"] for t in tareas.values()} <= {"IA", "CDS", None}
    assert [tareas[i]["area"] for i in (12, 13, 14, 15, 18)] == ["IA"] * 5
    assert tareas[17]["area"] == "CDS" and tareas[19]["area"] is None
    # Las tareas con proyecto no guardan área propia (el CHECK de la base).
    assert all(tareas[i]["area"] is None for i in (10, 11, 16))
    # La huella para poder deshacer: una, del proyecto 2, 'borrar', con su antes.
    huellas = [dict(f) for f in con.execute("SELECT * FROM log_acciones")]
    assert len(huellas) == 1, huellas
    h = huellas[0]
    assert (h["accion"], h["tabla"], h["registro_id"]) == ("borrar", "proyectos", 2)
    antes = json.loads(h["antes"])
    assert antes["nombre"] == "Proyecto personal vacío"
    assert antes["borrado_en"] is None and antes["area"] == personal


def test_G2_M1b_se_puede_correr_dos_veces():
    tecnico, personal = _viejas()
    con = _con_estado_previo(_sembrar(tecnico, personal))
    antes = ([tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")],
             [tuple(f) for f in con.execute("SELECT * FROM tareas ORDER BY id")],
             [tuple(f) for f in con.execute("SELECT * FROM areas ORDER BY clave")],
             con.execute("SELECT count(*) FROM log_acciones").fetchone()[0])
    for archivo in _migraciones():
        if _borra_areas(archivo):
            _aplicar(con, archivo)
    despues = ([tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")],
               [tuple(f) for f in con.execute("SELECT * FROM tareas ORDER BY id")],
               [tuple(f) for f in con.execute("SELECT * FROM areas ORDER BY clave")],
               con.execute("SELECT count(*) FROM log_acciones").fetchone()[0])
    assert despues == antes


def test_G2_M1b_sin_el_area_IA_falla_fuerte_y_no_aplica_nada():
    """Si alguien corre M1b sin haber corrido M1a, la FK revienta: mejor eso
    que dejar tareas apuntando a un área que no existe."""
    tecnico, personal = _viejas()
    con = _base_vacia()
    archivos = _migraciones()
    solo_la_que_siembra = [a for a in archivos
                           if any(re.match(r"\s*INSERT\s+INTO\s+areas", s, re.I)
                                  and tecnico in s
                                  for s in _las_que_se_ejecutan(a))]
    assert len(solo_la_que_siembra) == 1
    _aplicar(con, solo_la_que_siembra[0])
    _sembrar(tecnico, personal)(con)
    con.commit()
    borra = [a for a in archivos if _borra_areas(a)]
    assert len(borra) == 1
    with pytest.raises(sqlite3.IntegrityError):
        _aplicar(con, borra[0])


# ═══════════════════════════════════════════════════════════════════════
# G1: la puerta de Code, con las funciones reales de db/db.py
# ═══════════════════════════════════════════════════════════════════════

def _fila(f, como_dict: bool):
    """Una fila como la entrega psycopg 3.2.3 (`psycopg/rows.py`, leído el
    2-oct-2026): con `dict_row` es un `dict` común (sin índice por posición:
    `fila[0]` es KeyError), y SIN `row_factory` —el caso de `conn.execute(...)`
    en una conexión del pool— es `tuple_row`, el valor por omisión, una tupla
    (sin acceso por nombre). Las columnas JSON (`antes`, `despues`) llegan ya
    leídas, como las entrega psycopg con `jsonb` (SQLite las guarda como texto:
    eso sí es una imitación declarada)."""
    if f is None:
        return None
    d = dict(f)
    for k in ("antes", "despues"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    # Las fechas `timestamptz` llegan de psycopg como `datetime` con zona;
    # SQLite las guarda como texto ISO (imitación declarada). Un texto sin zona
    # se lee como UTC, igual que `db.dia_rd`.
    for k in ("creado_en", "vence_en", "completado_en", "borrado_en",
              "editado_en", "ts"):
        if isinstance(d.get(k), str):
            try:
                visto = datetime.fromisoformat(d[k])
            except ValueError:
                continue
            d[k] = visto if visto.tzinfo else visto.replace(tzinfo=timezone.utc)
    return d if como_dict else tuple(d.values())


class _Cur:
    def __init__(self, con, como_dict: bool):
        self.con = con
        self.como_dict = como_dict
        self._cur = None

    async def execute(self, sql, params=()):
        # Un `int[]` de Postgres (los `anticipos_min` de una tarea nueva) viaja
        # como lista; SQLite no la entiende y se guarda como texto JSON
        # (imitación declarada, igual que las columnas JSON de más abajo).
        params = tuple(json.dumps(x) if isinstance(x, (list, tuple)) else x
                       for x in (params or ()))
        self._cur = self.con.execute(_hacia_sqlite(sql), params)
        return self

    async def fetchone(self):
        return _fila(self._cur.fetchone(), self.como_dict)

    async def fetchall(self):
        return [_fila(f, self.como_dict) for f in self._cur.fetchall()]


class _Conn:
    def __init__(self, con):
        self.con = con

    def cursor(self, row_factory=None):
        return _Cur(self.con, como_dict=row_factory is not None)

    async def execute(self, sql, params=()):
        return await _Cur(self.con, como_dict=False).execute(sql, params)

    @asynccontextmanager
    async def transaction(self):
        yield self


class _Pool:
    def __init__(self, con):
        self.con = con

    @asynccontextmanager
    async def connection(self):
        yield _Conn(self.con)


@pytest.fixture
def base_migrada(monkeypatch):
    tecnico, personal = _viejas()
    con = _con_estado_previo(_sembrar(tecnico, personal))
    monkeypatch.setattr(db, "pool", _Pool(con))
    return con


async def test_G1_tareas_de_code_pendientes_ve_las_de_code_en_IA(base_migrada):
    filas = await db.tareas_de_code_pendientes()
    assert sorted(f["id"] for f in filas) == _VISIBLES_PARA_CODE


async def test_G1_cerrar_encuentra_las_de_IA_y_rechaza_el_resto(base_migrada):
    for tid in _VISIBLES_PARA_CODE:
        assert await db.cerrar_tarea_de_la_sala(tid) is True, tid
    for tid in (11, 13, 14, 17, 18, 19):
        assert await db.cerrar_tarea_de_la_sala(tid) is False, tid
    hechas = {f["id"] for f in base_migrada.execute(
        "SELECT id FROM tareas WHERE estado = 'hecha'")}
    assert hechas == {13, *_VISIBLES_PARA_CODE}


async def test_G1_tomar_encuentra_las_de_IA_una_sola_vez(base_migrada):
    for tid in _VISIBLES_PARA_CODE:
        assert await db.tomar_tarea_de_la_sala(tid) is True, tid
        assert await db.tomar_tarea_de_la_sala(tid) is False, tid
    for tid in (11, 13, 14, 17, 18, 19):
        assert await db.tomar_tarea_de_la_sala(tid) is False, tid


# ═══════════════════════════════════════════════════════════════════════
# G3: el nombre viejo no queda escrito
# ═══════════════════════════════════════════════════════════════════════
#
# LA REGLA, en una línea: fuera de `tests/` y de las migraciones que tienen que
# nombrarlas (las que corren hasta la que borra las áreas viejas, inclusive), en
# ningún archivo de texto del repo aparece una de las áreas viejas, ni por su
# clave completa (con o sin el selector de emoji) ni por su palabra suelta
# («Técnico», «Personal»; con mayúscula inicial y como palabra entera).
#
# Las áreas viejas SALEN DE LAS MIGRACIONES (`_nombres_viejos`), no se teclean.
# QUÉ NO VE: el nombre viejo escrito con otras letras (`Tecnico` sin tilde,
# `técnico` en minúscula, `TÉCNICO`), armado por partes, o dentro de `tests/`.
# La palabra femenina «técnica/TÉCNICAS» no es el nombre del grupo y no entra.
# EXENCIÓN DECLARADA, una sola, por archivo y texto de la línea (no por número):
# `captura/correo.py`, el encabezado «# Personal» de las categorías de correo,
# que habla de la vida personal de Tiziano y no del grupo.

_EXENTAS = {("captura/correo.py", "# Personal")}


def _sin_selector(s: str) -> str:
    return s.replace("️", "")


def _palabras_sueltas(viejas) -> set[str]:
    palabras = set()
    for n in viejas:
        for p in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]+", n):
            if p[0].isupper():
                palabras.add(p)
    return palabras


def _hallazgos_en(texto: str, ruta: str, viejas) -> list[tuple[str, int, str]]:
    claves = {_sin_selector(n) for n in viejas}
    patrones = [re.compile(r"(?<!\w)" + re.escape(p) + r"(?!\w)")
                for p in _palabras_sueltas(viejas)]
    hallados = []
    for numero, linea in enumerate(texto.splitlines(), 1):
        if (ruta, linea.strip()) in _EXENTAS:
            continue
        limpia = _sin_selector(linea)
        if any(c in limpia for c in claves) or any(p.search(limpia) for p in patrones):
            hallados.append((ruta, numero, linea.strip()[:120]))
    return hallados


def _es_entorno_o_cache(carpeta: pathlib.Path) -> bool:
    return ((carpeta / "pyvenv.cfg").exists()
            or (carpeta / "CACHEDIR.TAG").exists()
            or carpeta.name in {".git", "__pycache__", "node_modules"})


def _archivos_de_texto():
    """Todo archivo de texto del repo (derivado del disco, no de una lista),
    salvo `tests/` y los entornos virtuales y cachés (se les pregunta qué son:
    `pyvenv.cfg`, `CACHEDIR.TAG`; `.git`, `__pycache__` y `node_modules` por
    nombre, como en pytest.ini)."""
    pendientes = [_ROOT]
    while pendientes:
        carpeta = pendientes.pop()
        for hijo in sorted(carpeta.iterdir()):
            if hijo.is_dir():
                if _es_entorno_o_cache(hijo):
                    continue
                if carpeta == _ROOT and hijo.name == "tests":
                    continue
                pendientes.append(hijo)
            else:
                yield hijo


def test_G3_el_nombre_viejo_no_queda_escrito_en_el_repo():
    viejas = _nombres_viejos()
    assert viejas, "no salió ninguna área vieja de las migraciones"
    archivos = _migraciones()
    corte = next(i for i, a in enumerate(archivos) if _borra_areas(a))
    que_las_nombran = {a.resolve() for a in archivos[:corte + 1]}
    leidos, hallados = 0, []
    for archivo in _archivos_de_texto():
        if archivo.resolve() in que_las_nombran:
            continue
        try:
            texto = archivo.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        leidos += 1
        hallados += _hallazgos_en(
            texto, archivo.relative_to(_ROOT).as_posix(), viejas)
    assert leidos > 50, f"el barrido leyó {leidos} archivos: ¿se perdió el repo?"
    assert not hallados, (
        "el nombre de un área que ya no existe sigue escrito:\n  "
        + "\n  ".join(f"{r}:{n}: {t}" for r, n, t in hallados))


def test_G3_la_guarda_ve_lo_que_dice_ver_con_textos_inventados():
    """Una guarda que solo se probó con lo que hay hoy promete de más: se le
    dan entradas que no existen."""
    viejas = {"🛠️ Técnico", "🏠 Personal"}
    ve = lambda texto, ruta="x.py": bool(_hallazgos_en(texto, ruta, viejas))   # noqa: E731
    assert ve("AREA = '🛠️ Técnico'")
    assert ve("AREA = '🛠 Técnico'")            # sin el selector de emoji
    assert ve("# 'CDS' | 'ACD' | '🏠 Personal'")
    assert ve("solo Técnico, nada más")          # la palabra suelta
    assert ve('"Personal-- que puede llevar"')   # pegada a signos
    assert not ve("una tarea TÉCNICA de Code")   # no es el nombre del grupo
    assert not ve("alarmas_tecnicas y clave_tecnica")
    assert not ve("el buzón personal de Tiziano")
    assert not ve("# Personal", "captura/correo.py")      # la exención declarada
    assert ve("# Personal", "cerebro/otra_cosa.py")       # solo en su archivo


# La pista del prompt de Lucy: «IA» entra, lo personal ya no es un grupo.

def _pista_de_area() -> str:
    con, _ = _todo_en_orden()
    claves = list(_areas(con))
    prompt = agente.herramientas_del_prompt(
        areas=[{"clave": c, "color": "#0"} for c in claves])
    ini = prompt.index("ÁREA (tareas, opcional)")
    fin = prompt.index('Si la tarea lleva "proyecto"', ini)
    texto = prompt[ini:fin]
    # La lista inyectada trae las tres claves; la pista habla aparte.
    lista = ", ".join(f'"{c}"' for c in claves)
    assert lista in texto
    return texto.replace(lista, "")


def test_la_pista_del_prompt_nombra_IA_y_manda_lo_personal_sin_area():
    pista = _pista_de_area()
    assert re.search(r"(?<!\w)IA(?!\w)", pista), pista
    for m in re.finditer(r"personal", pista, re.I):
        resto = pista[m.start():].split(". ")[0]
        assert "sin área" in resto, (
            "la pista menciona «personal» sin decir que va sin área: " + pista)
