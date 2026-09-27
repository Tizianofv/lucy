# -*- coding: utf-8 -*-
"""Lectura de SOLO lo de Tiziano en `bandeja`, `notas`, `movimientos` y
`eventos` (§A.1-A.3, parte A del plan de construcción "Code como
responsable de tareas técnicas", 27-sep-2026, `db/lectura_dueno.py`).

Parte A, y SOLO esa: cuatro funciones de lectura, de solo lectura de
verdad (`SET TRANSACTION READ ONLY`, dentro de una
transacción), que deciden "de Tiziano" por LA MISMA puerta
(`_condicion_de_dueno`). `personas`/`preferencias` NO están acá (van en la
parte E). Sin ruta HTTP: eso no está aprobado.

NINGÚN chat_id ni clave de este archivo es real (regla del repo: es
PÚBLICO).

CÓMO SE PRUEBA: SQL de verdad. Las cuatro funciones corren tal cual contra
un `db.pool` que es SQLite de verdad (mismo patrón de
`tests/test_code_alarmas.py::_sqlite_para_alerta_real` -- `%s` -> `?`, y
`%s = ANY(duenos_chat_id)` traducido a una función SQLite registrada,
porque SQLite no tiene arrays). Ninguna prueba reimplementa el filtro en
Python: si `_condicion_de_dueno` cambiara su SQL, estas pruebas lo
ejecutarían tal cual sale, no una copia.

Correr:  python3 -m pytest tests/test_lectura_dueno.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import textwrap
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-lectura")
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import db.lectura_dueno as lectura  # noqa: E402

DUENO = config.CHAT_ID_DUENO   # 424242
ROSI = 700300777
DESCONOCIDO = 999999999


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ═══════════════════════════════════════════════════════════════════════
# El arnés SQLite: %s -> ?, "%s = ANY(duenos_chat_id)" -> una función
# registrada (SQLite no tiene arrays; duenos_chat_id se guarda como JSON).
# ═══════════════════════════════════════════════════════════════════════

def _traducir(sql: str) -> str:
    sql2 = sql.replace("%s = ANY(duenos_chat_id)", "es_miembro(duenos_chat_id, ?)")
    return sql2.replace("%s", "?")


def _es_miembro(arreglo_json, valor):
    if not arreglo_json:
        return 0
    return 1 if valor in json.loads(arreglo_json) else 0


class _CurLectura:
    def __init__(self, con):
        self._con = con
        self._cur = None

    async def execute(self, sql, params=None):
        self._cur = self._con.execute(_traducir(sql), params or ())
        return self

    async def fetchall(self):
        return [dict(f) for f in self._cur.fetchall()]

    async def fetchone(self):
        f = self._cur.fetchone()
        return dict(f) if f is not None else None


class _ConnLectura:
    def __init__(self, con, comandos):
        self._con = con
        self.comandos = comandos

    def cursor(self, row_factory=None):
        return _CurLectura(self._con)

    async def execute(self, sql, params=None):
        self.comandos.append(sql.strip())
        if sql.strip().upper() == "SET TRANSACTION READ ONLY":
            return None
        return await _CurLectura(self._con).execute(sql, params)

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False
        return _Tx()


class _PoolLectura:
    def __init__(self, con):
        self.comandos: list[str] = []
        self._conn = _ConnLectura(con, self.comandos)

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _sqlite_de_dueno():
    con = sqlite3.connect(":memory:", isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    con.create_function("es_miembro", 2, _es_miembro)
    con.execute("""
        CREATE TABLE bandeja (
          id INTEGER PRIMARY KEY, creado_en TEXT, origen TEXT,
          tipo_entrada TEXT, contenido_raw TEXT, clasificacion TEXT,
          estado TEXT, chat_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE notas (
          id INTEGER PRIMARY KEY, creado_en TEXT, contenido TEXT,
          etiquetas TEXT, proyecto_id INTEGER, persona_id INTEGER,
          borrado_en TEXT, bandeja_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE movimientos (
          id INTEGER PRIMARY KEY, fecha TEXT, tipo TEXT, monto TEXT,
          moneda TEXT, contraparte TEXT, categoria TEXT, referencia TEXT,
          estado TEXT, borrado_en TEXT, bandeja_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE eventos (
          id INTEGER PRIMARY KEY, titulo TEXT, inicia_en TEXT,
          termina_en TEXT, lugar TEXT, persona_id INTEGER,
          proyecto_id INTEGER, notas TEXT, borrado_en TEXT,
          duenos_chat_id TEXT
        )""")
    return con


def _instalar(con):
    guardado = lectura.pool
    lectura.pool = _PoolLectura(con)
    return guardado


def _restaurar(guardado):
    lectura.pool = guardado


# ═══════════════════════════════════════════════════════════════════════
# §A.1 -- bandeja: el chat_id vive EN LA FILA.
# ═══════════════════════════════════════════════════════════════════════

def test_bandeja_trae_solo_lo_de_tiziano():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una fila de Rosi, de un chat
    desconocido, o sin chat_id (origen de sistema) nunca sale."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id, contenido_raw) VALUES "
                "(1, ?, 'de tiziano')", (DUENO,))
    con.execute("INSERT INTO bandeja (id, chat_id, contenido_raw) VALUES "
                "(2, ?, 'de rosi')", (ROSI,))
    con.execute("INSERT INTO bandeja (id, chat_id, contenido_raw) VALUES "
                "(3, ?, 'de un desconocido')", (DESCONOCIDO,))
    con.execute("INSERT INTO bandeja (id, chat_id, contenido_raw) VALUES "
                "(4, NULL, 'sin chat_id, origen de sistema')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert [f["id"] for f in filas] == [1]


def test_bandeja_usa_transaccion_de_solo_lectura():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: `SET TRANSACTION READ ONLY`, DENTRO
    de una transacción -- el mismo literal exacto que ya reconoce
    `tests/test_responsable.py::_ejecuta_solo_lectura`, para que el censo
    de escritores genéricos no le pida una sonda a una función que nunca
    escribe nada."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id) VALUES (1, ?)", (DUENO,))
    guardado = _instalar(con)
    pool_falso = lectura.pool
    try:
        _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert any(c.strip().upper() == "SET TRANSACTION READ ONLY"
               for c in pool_falso.comandos), (
        f"no se puso la transacción de solo lectura: {pool_falso.comandos}")


# ═══════════════════════════════════════════════════════════════════════
# §A.2 -- notas y movimientos: SIN chat_id propio, se decide por la
# `bandeja` que los originó.
# ═══════════════════════════════════════════════════════════════════════

def _con_bandejas(con):
    con.execute("INSERT INTO bandeja (id, chat_id) VALUES (10, ?)", (DUENO,))
    con.execute("INSERT INTO bandeja (id, chat_id) VALUES (20, ?)", (ROSI,))
    con.execute("INSERT INTO bandeja (id, chat_id) VALUES (30, ?)", (DESCONOCIDO,))


def test_notas_trae_solo_lo_de_tiziano():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una nota de Rosi, de un chat
    desconocido, o SIN `bandeja_id` (no hay forma de probar de quién es)
    nunca sale."""
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(1, 10, 'de tiziano')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(2, 20, 'de rosi')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(3, 30, 'de un desconocido')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(4, NULL, 'sin bandeja_id')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_notas_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert [f["id"] for f in filas] == [1]


def test_notas_no_trae_borradas():
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO notas (id, bandeja_id, contenido, borrado_en) "
                "VALUES (1, 10, 'borrada', '2026-01-01')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_notas_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


def test_movimientos_trae_solo_lo_de_tiziano():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: mismo patrón que notas -- un
    movimiento de Rosi (mismo pipeline bancario, otra bandeja) no sale."""
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO movimientos (id, bandeja_id, fecha, monto) "
                "VALUES (1, 10, '2026-09-01', '100.00')")
    con.execute("INSERT INTO movimientos (id, bandeja_id, fecha, monto) "
                "VALUES (2, 20, '2026-09-02', '200.00')")
    con.execute("INSERT INTO movimientos (id, bandeja_id, fecha, monto) "
                "VALUES (3, 30, '2026-09-03', '300.00')")
    con.execute("INSERT INTO movimientos (id, bandeja_id, fecha, monto) "
                "VALUES (4, NULL, '2026-09-04', '400.00')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_movimientos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert [f["id"] for f in filas] == [1]


def test_movimientos_no_trae_borrados():
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO movimientos (id, bandeja_id, fecha, monto, "
                "borrado_en) VALUES (1, 10, '2026-09-01', '5.00', '2026-09-05')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_movimientos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


# ═══════════════════════════════════════════════════════════════════════
# §A.3 -- eventos: `duenos_chat_id` es un ARRAY; vacío = "sin dueño" y NO
# cuenta como de Tiziano (al revés que `despertador._destinatarios_de_
# tareas`, que trata el array vacío como "de todos" para avisar).
# ═══════════════════════════════════════════════════════════════════════

def test_eventos_trae_las_suyas_y_las_compartidas_con_rosi():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una cita SOLO de Rosi, o SIN dueño
    (`duenos_chat_id = []`, el estado normal de casi toda cita sincronizada
    de Google), nunca sale. Una compartida (Tiziano Y Rosi) SÍ sale."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (1, 'de tiziano', '2026-09-01', ?)",
                (json.dumps([DUENO]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (2, 'de rosi', '2026-09-02', ?)",
                (json.dumps([ROSI]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (3, 'compartida', '2026-09-03', ?)",
                (json.dumps([DUENO, ROSI]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (4, 'sin dueno', '2026-09-04', ?)",
                (json.dumps([]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (5, 'sin dueno de verdad', '2026-09-05', NULL)")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_eventos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert sorted(f["id"] for f in filas) == [1, 3]


def test_eventos_no_trae_borrados():
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id, "
                "borrado_en) VALUES (1, 'x', '2026-09-01', ?, '2026-09-10')",
                (json.dumps([DUENO]),))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_eventos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


# ═══════════════════════════════════════════════════════════════════════
# HERMANOS: las cuatro lecturas usan LA MISMA puerta -- se saca de lo real
# recorriendo el AST del módulo, no de memoria.
# ═══════════════════════════════════════════════════════════════════════

def _llama_a(funcion, nombre: str) -> bool:
    codigo = textwrap.dedent(inspect.getsource(funcion))
    arbol = ast.parse(codigo)
    return any(isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == nombre)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre))
               for n in ast.walk(arbol))


def test_las_cuatro_lecturas_pasan_por_leer_de_dueno():
    for fn in (lectura.leer_bandeja_de_dueno, lectura.leer_notas_de_dueno,
               lectura.leer_movimientos_de_dueno, lectura.leer_eventos_de_dueno):
        assert _llama_a(fn, "_leer_de_dueno"), (
            f"{fn.__name__} no pasa por _leer_de_dueno -- arma su propio "
            "criterio de dueño")


def test_leer_de_dueno_pasa_por_la_puerta_de_condicion():
    assert _llama_a(lectura._leer_de_dueno, "_condicion_de_dueno"), (
        "_leer_de_dueno ya no llama a _condicion_de_dueno -- LA puerta "
        "dejó de ser la única fuente del filtro de dueño")


def test_condicion_de_dueno_cubre_las_cuatro_tablas():
    for tabla in ("bandeja", "notas", "movimientos", "eventos"):
        assert lectura._condicion_de_dueno(tabla)  # no revienta, y no es ""


def test_condicion_de_dueno_rechaza_tabla_desconocida():
    with pytest.raises(ValueError):
        lectura._condicion_de_dueno("personas")  # personas: parte E, no acá


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
