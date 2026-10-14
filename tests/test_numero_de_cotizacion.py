"""El número de una cotización (pieza 2 del diseño «Cotizaciones en Proyectos», 14-oct-2026).

Qué se vigila, con la sentencia REAL de `db.siguiente_numero_de_cotizacion` EJECUTADA en SQLite (>= 3.35
entiende `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING`) sobre las tablas que crea la migración real:

  1. la primera del año es 1 (la serie nace vacía: respuesta 1 de Tiziano); dos seguidas dan N y N+1;
  2. cada año lleva su cuenta: el año nuevo empieza en 1 y no toca la del anterior;
  3. dos conexiones INTERCALADAS no sacan el mismo número (archivo real, dos conexiones);
  4. `UNIQUE (anio, numero)` frena un repetido aunque el contador falle;
  5. el formato `COT-AAAA-NNN`;
  6. una sola puerta: ningún `.py` fuera de `tests/` escribe `serie_cotizaciones` salvo la sentencia de `db`;
  7. un año que no es año no llega a la base.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. SQLite bloquea la base ENTERA mientras una conexión escribe; Postgres
    bloquea solo la fila del año y el segundo ESPERA al `COMMIT` del primero. Aquí el segundo recibe
    «database is locked» (timeout 0) en vez de esperar: lo que se prueba es que NO sale el mismo número
    mientras el primero no ha cerrado, y que sale el siguiente cuando cierra. La espera real de Postgres
    no se ejerce; la del `ON CONFLICT DO UPDATE` sobre la misma fila la cubre el ensayo de la sala.
  · `psycopg` no se ejerce: el cursor es un doble mínimo (`%s` → `?`, filas como dict) que NO cambia la
    sentencia que le llega; la prueba compara el texto recibido con `db.SENTENCIA_DEL_NUMERO_DE_COTIZACION`.

Correr:  python3 -m pytest tests/test_numero_de_cotizacion.py -q
"""
from __future__ import annotations

import ast
import asyncio
import re
import sqlite3

import pytest

import test_buzon_que_no_se_ve as buzon
import test_cotizaciones_tablas as t
import test_grupo_ia as g
from test_grupo_ia import _ROOT

import db.db as db


class _Cursor:
    """Lo mínimo de un cursor `dict_row` de psycopg sobre una conexión de SQLite."""

    def __init__(self, con: sqlite3.Connection):
        self._con = con
        self._filas: list[dict] = []
        self.recibidas: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.recibidas.append((sql, tuple(params or ())))
        cur = self._con.execute(sql.replace("%s", "?"), tuple(params or ()))
        self._filas = [dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None


def _base_en_memoria() -> sqlite3.Connection:
    con = t._base_con_migracion()
    return con


def _numero(con, anio):
    return asyncio.run(db.siguiente_numero_de_cotizacion(_Cursor(con), anio))


def _cotizacion(con, anio, numero):
    f = t._fila(anio=anio, numero=numero)
    con.execute(f"INSERT INTO cotizaciones ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", tuple(f.values()))


# ── 1. La primera es 1 y las siguientes suben de uno en uno ─────────────────────────────────────────

def test_la_primera_del_anio_es_1_sobre_la_serie_vacia():
    con = _base_en_memoria()
    assert con.execute("SELECT count(*) FROM serie_cotizaciones").fetchone()[0] == 0
    assert _numero(con, 2026) == 1
    assert con.execute("SELECT anio, ultimo FROM serie_cotizaciones").fetchall() == [(2026, 1)]


def test_dos_seguidas_dan_n_y_n_mas_1_y_siguen_subiendo():
    con = _base_en_memoria()
    sacados = [_numero(con, 2026) for _ in range(5)]
    assert sacados == [1, 2, 3, 4, 5]
    assert con.execute("SELECT ultimo FROM serie_cotizaciones WHERE anio = 2026").fetchone()[0] == 5


def test_el_contador_escribe_solo_su_fila_y_no_toca_cotizaciones():
    con = _base_en_memoria()
    _numero(con, 2026)
    assert con.execute("SELECT count(*) FROM cotizaciones").fetchone()[0] == 0


def test_a_la_base_llega_exactamente_la_sentencia_del_diseno_con_el_anio_como_parametro():
    con = _base_en_memoria()
    cur = _Cursor(con)
    asyncio.run(db.siguiente_numero_de_cotizacion(cur, 2026))
    assert cur.recibidas == [(db.SENTENCIA_DEL_NUMERO_DE_COTIZACION, (2026,))]
    plana = " ".join(db.SENTENCIA_DEL_NUMERO_DE_COTIZACION.split())
    assert plana == ("INSERT INTO serie_cotizaciones (anio, ultimo) VALUES (%s, 1) ON CONFLICT (anio) "
                     "DO UPDATE SET ultimo = serie_cotizaciones.ultimo + 1 RETURNING ultimo")


# ── 2. Cada año lleva su cuenta ─────────────────────────────────────────────────────────────────────

def test_el_anio_nuevo_empieza_en_1_y_no_toca_la_cuenta_del_anterior():
    con = _base_en_memoria()
    assert [_numero(con, 2026) for _ in range(3)] == [1, 2, 3]
    assert _numero(con, 2027) == 1
    assert _numero(con, 2026) == 4
    assert _numero(con, 2027) == 2
    assert con.execute("SELECT anio, ultimo FROM serie_cotizaciones ORDER BY anio").fetchall() == [(2026, 4), (2027, 2)]


# ── 3. Dos conexiones intercaladas no sacan el mismo número ─────────────────────────────────────────

def test_dos_conexiones_intercaladas_no_sacan_el_mismo_numero(tmp_path):
    """A saca su número y NO ha cerrado; B pide el suyo en medio. B no puede recibir el de A. Cuando A
    guarda su cotización y cierra, B recibe el siguiente. (Una sentencia `max(numero)+1` dejaría a los
    dos con el 1: ninguno ha insertado todavía.)"""
    ruta = str(tmp_path / "dos_conexiones.db")
    a = sqlite3.connect(ruta, timeout=0, isolation_level=None)
    b = sqlite3.connect(ruta, timeout=0, isolation_level=None)
    for con in (a, b):
        con.create_function("now", 0, lambda: "2026-10-14T12:00:00+00:00")
        con.execute("PRAGMA foreign_keys = ON")
    _crear_tablas_en_autocommit(a)

    a.execute("BEGIN")
    n_a = asyncio.run(db.siguiente_numero_de_cotizacion(_Cursor(a), 2026))
    assert n_a == 1

    b.execute("BEGIN")
    try:
        n_b = asyncio.run(db.siguiente_numero_de_cotizacion(_Cursor(b), 2026))
    except sqlite3.OperationalError as e:
        assert "locked" in str(e)
        n_b = None
    assert n_b != n_a, "las dos conexiones sacaron el mismo número"
    if n_b is None:
        b.execute("ROLLBACK")

    _cotizacion(a, 2026, n_a)
    a.execute("COMMIT")

    if n_b is None:
        b.execute("BEGIN")
        n_b = asyncio.run(db.siguiente_numero_de_cotizacion(_Cursor(b), 2026))
    assert n_b == n_a + 1
    _cotizacion(b, 2026, n_b)
    b.execute("COMMIT")
    assert sorted(r[0] for r in a.execute("SELECT numero FROM cotizaciones")) == [1, 2]
    a.close()
    b.close()


def _crear_tablas_en_autocommit(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS proyectos (id INTEGER PRIMARY KEY)")
    con.execute("INSERT OR IGNORE INTO proyectos (id) VALUES (1)")
    for s in g._sentencias(t._texto_migracion()):
        if re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
            continue
        con.execute(t._a_sqlite(s))


# ── 4. El UNIQUE es la última puerta ────────────────────────────────────────────────────────────────

def test_el_unique_frena_un_repetido_aunque_el_contador_falle():
    con = _base_en_memoria()
    n = _numero(con, 2026)
    _cotizacion(con, 2026, n)
    # El contador "falla": alguien lo devuelve a cero y vuelve a dar el mismo número.
    con.execute("UPDATE serie_cotizaciones SET ultimo = 0 WHERE anio = 2026")
    repetido = _numero(con, 2026)
    assert repetido == n
    with pytest.raises(sqlite3.IntegrityError):
        _cotizacion(con, 2026, repetido)
    # En otro año el mismo número es otra cotización, y vale.
    _cotizacion(con, 2027, 1)


# ── 5. El formato ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("anio,numero,esperado", [
    (2026, 1, "COT-2026-001"), (2026, 42, "COT-2026-042"), (2026, 999, "COT-2026-999"),
    (2026, 1000, "COT-2026-1000"), (2027, 1, "COT-2027-001"),
])
def test_el_codigo_es_cot_anio_tres_cifras(anio, numero, esperado):
    assert db.codigo_de_cotizacion(anio, numero) == esperado


def test_la_primera_que_haga_lucy_es_la_cot_2026_001():
    con = _base_en_memoria()
    assert db.codigo_de_cotizacion(2026, _numero(con, 2026)) == "COT-2026-001"


@pytest.mark.parametrize("anio,numero", [(2026, 0), (2026, -1), (2026, True), (2026, "1"), (2026, 1.0), (2026, None),
                                         (1999, 1), (10000, 1), ("2026", 1), (True, 1), (None, 1)])
def test_el_codigo_rechaza_lo_que_no_es_un_numero_o_un_anio(anio, numero):
    with pytest.raises(ValueError):
        db.codigo_de_cotizacion(anio, numero)


# ── 7. Un año que no es año no llega a la base ──────────────────────────────────────────────────────

@pytest.mark.parametrize("anio", [None, "2026", 2026.0, True, 1999, 10000, 0, -2026])
def test_un_anio_que_no_es_anio_no_toca_la_base(anio):
    con = _base_en_memoria()
    cur = _Cursor(con)
    with pytest.raises(ValueError):
        asyncio.run(db.siguiente_numero_de_cotizacion(cur, anio))
    assert cur.recibidas == []
    assert con.execute("SELECT count(*) FROM serie_cotizaciones").fetchone()[0] == 0


# ── 6. Una sola puerta ──────────────────────────────────────────────────────────────────────────────

def _textos_que_escriben_la_serie(archivos, raiz) -> list[tuple[str, int, str]]:
    """(archivo, línea, texto) de toda constante de texto de un `.py` fuera de `tests/` que nombra
    `serie_cotizaciones` y trae INSERT, UPDATE, DELETE o REPLACE. Las constantes se juntan con `ast`, así
    que un texto partido en pedazos pegados con `+` o entre paréntesis sigue siendo UNA constante."""
    hallados = []
    for ruta in archivos:
        rel = ruta.relative_to(raiz).as_posix()
        if rel.startswith("tests/") or ruta.name == "conftest.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        # Los docstrings explican, no se ejecutan: se descartan por su lugar en el árbol, no por su texto.
        docstrings = {id(n.body[0].value) for n in ast.walk(arbol)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docstrings:
                v = nodo.value
                if re.search(r"serie_cotizaciones", v, re.I) and re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", v, re.I):
                    hallados.append((rel, nodo.lineno, v))
    return hallados


def test_solo_la_sentencia_del_contador_escribe_la_serie():
    # Los archivos salen de lo real: la puerta única de los barridos (`buzon._py_en_disco`).
    hallados = _textos_que_escriben_la_serie(buzon._py_en_disco(_ROOT), _ROOT)
    assert [(r, v) for r, _, v in hallados] == [("db/db.py", db.SENTENCIA_DEL_NUMERO_DE_COTIZACION)], hallados


def test_la_busqueda_de_la_puerta_unica_ve_un_escritor_inventado(tmp_path):
    """Entradas inventadas para la guarda: un archivo nuevo que escribe la serie, con el texto partido."""
    (tmp_path / "otro.py").write_text(
        'SQL = ("UPDATE serie_cotizaciones "\n       "SET ultimo = 99")\nLEE = "SELECT * FROM serie_cotizaciones"\n',
        encoding="utf-8")
    hallados = _textos_que_escriben_la_serie([tmp_path / "otro.py"], tmp_path)
    assert [v for _, _, v in hallados] == ["UPDATE serie_cotizaciones SET ultimo = 99"]
