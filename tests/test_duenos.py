# -*- coding: utf-8 -*-
"""Marca de dueño en `personas` y `preferencias` (§E, parte E del plan de
construcción "Code como responsable de tareas técnicas", 27-sep-2026,
migración `db/migrations/2026-09-27_dueno_personas_preferencias.sql`).

Parte E, y SOLO esa: `personas.bandeja_id`/`preferencias.bandeja_id` en
los tres escritores reales (`db._buscar_o_crear`/`db.buscar_o_crear_
persona`, `acciones/crud.py::perfil`, `acciones/crud.py::
guardar_preferencia`), con su censo de hermanos, y las dos lecturas nuevas
de `db/lectura_dueno.py` (`leer_personas_de_dueno`/`leer_preferencias_de_
dueno`, ya probadas del lado de LECTURA en `tests/test_lectura_dueno.py`
-- acá se prueba el lado de ESCRITURA: que los tres escritores de verdad
guarden `bandeja_id`, y que sin la migración no revienten).

NINGÚN chat_id ni clave de este archivo es real (regla del repo: es
PÚBLICO).

CÓMO SE PRUEBA: SQL de verdad contra SQLite (mismo patrón que `tests/
test_lectura_dueno.py`), corriendo el código real de `db.py`/`crud.py`,
no una reimplementación.

Correr:  python3 -m pytest tests/test_duenos.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sqlite3
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-duenos")
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
import acciones.crud as crud  # noqa: E402
import db.lectura_dueno as lectura  # noqa: E402
import _doble_postgres as doble  # noqa: E402

DUENO = config.CHAT_ID_DUENO


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ═══════════════════════════════════════════════════════════════════════
# El arnés SQLite: %s -> ?, el `ANY(SELECT ... FROM unnest(alias) a)` de
# la búsqueda de personas se traduce a un literal que NUNCA calza (no es
# lo que esta prueba mide -- mide que el ESCRITOR ponga bandeja_id, no la
# búsqueda por alias, que ya cubre otra suite).
# ═══════════════════════════════════════════════════════════════════════

class _ErrorSQL(Exception):
    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(f"columna ausente, sqlstate={sqlstate}")


def _traducir(sql: str) -> str:
    sql2 = sql.replace(
        "OR lower(%s) = ANY(SELECT lower(a) FROM unnest(alias) a)",
        "OR lower(%s) = 'nunca-calza-un-alias-de-mentira'")
    return sql2.replace("%s", "?")


class _CurDuenos:
    def __init__(self, con, sin_columnas):
        self._con = con
        self._cur = None
        self._sin_columnas = sin_columnas

    async def execute(self, sql, params=None):
        normal = " ".join(sql.split()).upper()
        objetivo = ("INSERT INTO PERSONAS" in normal
                    or "INSERT INTO PREFERENCIAS" in normal)
        if self._sin_columnas and objetivo and "BANDEJA_ID" in normal:
            raise _ErrorSQL("42703")
        # SQLite no liga listas (los `alias` de personas viajan como
        # `TEXT[]` en Postgres) -- se serializan a JSON solo para esta
        # copia de prueba, nunca para la comparación de qué guardó.
        params2 = tuple(json.dumps(p) if isinstance(p, list) else p
                        for p in (params or ()))
        self._cur = self._con.execute(_traducir(sql), params2)
        return self

    async def fetchone(self):
        # `sqlite3.Row` sirve como tupla (`fila[0]`) Y como mapa
        # (`fila["id"]`) a la vez -- justo los dos estilos que usa el
        # código real según haya pedido `dict_row` o no. Se devuelve tal
        # cual, sin envolver en `dict`.
        return self._cur.fetchone()

    async def fetchall(self):
        return list(self._cur.fetchall())


class _ConnDuenos:
    def __init__(self, con, sin_columnas):
        self._con = con
        self._sin_columnas = sin_columnas

    def cursor(self, row_factory=None):
        return _CurDuenos(self._con, self._sin_columnas)

    async def execute(self, sql, params=None):
        return await _CurDuenos(self._con, self._sin_columnas).execute(sql, params)

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False
        return _Tx()


class _PoolDuenos:
    def __init__(self, con, sin_columnas=False):
        self._conn = _ConnDuenos(con, sin_columnas)

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _sqlite_duenos(con_bandeja_id_en_personas=True):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE bandeja (
          id INTEGER PRIMARY KEY, chat_id INTEGER, origen TEXT
        )""")
    if con_bandeja_id_en_personas:
        con.execute("""
            CREATE TABLE personas (
              id INTEGER PRIMARY KEY, creado_en TEXT, nombre TEXT,
              alias TEXT DEFAULT '', relacion TEXT, notas TEXT,
              borrado_en TEXT, bandeja_id INTEGER
            )""")
        con.execute("""
            CREATE TABLE preferencias (
              id INTEGER PRIMARY KEY, creado_en TEXT, texto TEXT,
              contexto TEXT, borrado_en TEXT, bandeja_id INTEGER
            )""")
    else:
        con.execute("""
            CREATE TABLE personas (
              id INTEGER PRIMARY KEY, creado_en TEXT, nombre TEXT,
              alias TEXT DEFAULT '', relacion TEXT, notas TEXT,
              borrado_en TEXT
            )""")
        con.execute("""
            CREATE TABLE preferencias (
              id INTEGER PRIMARY KEY, creado_en TEXT, texto TEXT,
              contexto TEXT, borrado_en TEXT
            )""")
    con.execute("""
        CREATE TABLE log_acciones (
          id INTEGER PRIMARY KEY, actor TEXT, accion TEXT, tabla TEXT,
          registro_id INTEGER, antes TEXT, despues TEXT, motivo TEXT,
          bandeja_id INTEGER
        )""")
    return con


def _instalar_db(con, sin_columnas=False):
    guardado = db.pool
    db.pool = _PoolDuenos(con, sin_columnas)
    return guardado


def _restaurar_db(guardado):
    db.pool = guardado


# ═══════════════════════════════════════════════════════════════════════
# `db.buscar_o_crear_persona`: guarda bandeja_id, y sin la migración no
# revienta.
# ═══════════════════════════════════════════════════════════════════════

def test_buscar_o_crear_persona_guarda_bandeja_id():
    con = _sqlite_duenos()
    guardado = _instalar_db(con)
    try:
        pid = _correr(db.buscar_o_crear_persona("Ana", bandeja_id=77))
    finally:
        _restaurar_db(guardado)
    fila = con.execute("SELECT bandeja_id FROM personas WHERE id = ?", (pid,)).fetchone()
    con.close()
    assert fila["bandeja_id"] == 77


def test_buscar_o_crear_persona_sin_bandeja_id_queda_sin_dueno():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: sin evidencia de quién la creó, la
    fila queda SIN dueño -- nunca se adivina."""
    con = _sqlite_duenos()
    guardado = _instalar_db(con)
    try:
        pid = _correr(db.buscar_o_crear_persona("Beto"))
    finally:
        _restaurar_db(guardado)
    fila = con.execute("SELECT bandeja_id FROM personas WHERE id = ?", (pid,)).fetchone()
    con.close()
    assert fila["bandeja_id"] is None


def test_buscar_o_crear_persona_sin_la_migracion_no_revienta():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 5), CONTRA EL DOBLE FIEL
    de Postgres (hallazgo del testigo sobre `cbc2726`, NO PASA: el doble
    SQLite de antes no modelaba que la conexión queda ABORTADA tras el
    primer error, así que no podía ver que el segundo INSERT reventaba con
    25P02 contra Postgres real): publicado sin la migración de §E, la
    columna no existe -- cae al INSERT de antes, la persona se crea
    igual, y la conexión queda SANA (con el SAVEPOINT)."""
    conexion = doble.ConexionPostgresFiel([
        ("SELECT ID FROM PERSONAS", None),
        ("INSERT INTO PERSONAS (NOMBRE, BANDEJA_ID)", "FALLA_42703"),
        ("INSERT INTO PERSONAS (NOMBRE) VALUES", {"id": 42}),
    ])
    guardado = db.pool
    db.pool = doble.PoolPostgresFiel(conexion)
    try:
        pid = _correr(db.buscar_o_crear_persona("Carla", bandeja_id=77))
    finally:
        db.pool = guardado
    assert pid == 42
    assert conexion.abortada is False, (
        "la conexión quedó abortada -- el SAVEPOINT no la recuperó")


# ═══════════════════════════════════════════════════════════════════════
# `acciones.crud.perfil` (tipo="persona"): guarda bandeja_id.
# ═══════════════════════════════════════════════════════════════════════

def test_crud_perfil_persona_nueva_guarda_bandeja_id():
    con = _sqlite_duenos()
    guardado = _instalar_db(con)
    try:
        _resultado, _log_id = _correr(crud.perfil(
            "persona", "Dana", bandeja_id=99))
    finally:
        _restaurar_db(guardado)
    fila = con.execute(
        "SELECT bandeja_id FROM personas WHERE nombre = 'Dana'").fetchone()
    con.close()
    assert fila["bandeja_id"] == 99


def test_crud_perfil_persona_sin_la_migracion_no_revienta():
    """CONTRA EL DOBLE FIEL de Postgres -- ver el docstring de
    `test_buscar_o_crear_persona_sin_la_migracion_no_revienta`."""
    conexion = doble.ConexionPostgresFiel([
        ("SELECT * FROM PERSONAS", None),
        ("INSERT INTO PERSONAS (NOMBRE, ALIAS, RELACION, NOTAS, BANDEJA_ID)",
         "FALLA_42703"),
        ("INSERT INTO PERSONAS (NOMBRE, ALIAS, RELACION, NOTAS) VALUES", (42,)),
    ])
    guardado_pool = db.pool
    guardado_registrar = crud._registrar

    async def _registrar_falso(*a, **k):
        return 99

    db.pool = doble.PoolPostgresFiel(conexion)
    crud._registrar = _registrar_falso
    try:
        resultado, _log_id = _correr(crud.perfil(
            "persona", "Elsa", bandeja_id=99))
    finally:
        db.pool = guardado_pool
        crud._registrar = guardado_registrar
    assert "Elsa" in resultado
    assert conexion.abortada is False, (
        "la conexión quedó abortada -- el SAVEPOINT no la recuperó")


# ═══════════════════════════════════════════════════════════════════════
# `acciones.crud.guardar_preferencia`: guarda bandeja_id.
# ═══════════════════════════════════════════════════════════════════════

def test_guardar_preferencia_guarda_bandeja_id():
    con = _sqlite_duenos()
    guardado = _instalar_db(con)
    try:
        pid, _log_id = _correr(crud.guardar_preferencia(
            55, "no me recuerdes trabajo los domingos"))
    finally:
        _restaurar_db(guardado)
    fila = con.execute(
        "SELECT bandeja_id FROM preferencias WHERE id = ?", (pid,)).fetchone()
    con.close()
    assert fila["bandeja_id"] == 55


def test_guardar_preferencia_sin_la_migracion_no_revienta():
    """CONTRA EL DOBLE FIEL de Postgres -- ver el docstring de
    `test_buscar_o_crear_persona_sin_la_migracion_no_revienta`."""
    conexion = doble.ConexionPostgresFiel([
        ("INSERT INTO PREFERENCIAS (TEXTO, CONTEXTO, BANDEJA_ID)", "FALLA_42703"),
        ("INSERT INTO PREFERENCIAS (TEXTO, CONTEXTO) VALUES", (77,)),
    ])
    guardado_pool = db.pool
    guardado_registrar = crud._registrar

    async def _registrar_falso(*a, **k):
        return 99

    db.pool = doble.PoolPostgresFiel(conexion)
    crud._registrar = _registrar_falso
    try:
        pid, _log_id = _correr(crud.guardar_preferencia(55, "x"))
    finally:
        db.pool = guardado_pool
        crud._registrar = guardado_registrar
    assert pid == 77
    assert conexion.abortada is False, (
        "la conexión quedó abortada -- el SAVEPOINT no la recuperó")


# ═══════════════════════════════════════════════════════════════════════
# CENSO DE HERMANOS: TODO `INSERT INTO personas`/`INSERT INTO preferencias`
# real del repo (excepto pruebas) tiene que poner `bandeja_id` en AL MENOS
# una de sus ramas (la sana; la de "sin la migración" cae al INSERT viejo
# a propósito, y eso está bien -- lo que no puede pasar es que NINGUNA
# rama de una función lo ponga).
# ═══════════════════════════════════════════════════════════════════════

def _texto_de_sql(nodo_sql):
    """El texto de un argumento SQL de `.execute(...)`: un `ast.Constant`
    string se devuelve tal cual (Python ya funde literales adyacentes en
    un solo Constant al parsear); un f-string junta sus piezas literales y
    dice `{VAR}` donde haya una expresión, para que un `{tabla}` genérico
    no rompa la búsqueda de texto."""
    if isinstance(nodo_sql, ast.Constant) and isinstance(nodo_sql.value, str):
        return nodo_sql.value
    if isinstance(nodo_sql, ast.JoinedStr):
        return "".join(
            p.value if isinstance(p, ast.Constant) else "{VAR}"
            for p in nodo_sql.values)
    return None


def _censo_de_inserts(tabla: str) -> dict[str, list[bool]]:
    """(archivo::función) -> [tiene_bandeja_id por cada INSERT INTO
    `tabla` que esa función ejecuta], recorriendo el AST del repositorio
    ENTERO (menos pruebas)."""
    import test_buzon_que_no_se_ve as barrido

    raiz = barrido.RAIZ
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    resultado: dict[str, list[bool]] = {}
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        try:
            fuente = real.read_text(encoding="utf-8")
            arbol = ast.parse(fuente, filename=str(real))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = real.relative_to(raiz).as_posix()
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for nodo in ast.walk(funcion):
                if not (isinstance(nodo, ast.Call)
                        and isinstance(nodo.func, ast.Attribute)
                        and nodo.func.attr in ("execute", "executemany")):
                    continue
                sql = nodo.args[0] if nodo.args else None
                texto = _texto_de_sql(sql)
                if texto is None:
                    continue
                normal = " ".join(texto.split()).upper()
                if f"INSERT INTO {tabla.upper()}" not in normal:
                    continue
                clave = f"{rel}::{funcion.name}"
                resultado.setdefault(clave, []).append("BANDEJA_ID" in normal)
    return resultado


def test_todo_insert_de_personas_pone_bandeja_id_en_alguna_rama():
    censo = _censo_de_inserts("personas")
    assert censo, "el censo no encontró ni un INSERT INTO personas: se quedó ciego"
    sin_bandeja_id = sorted(f for f, ramas in censo.items() if not any(ramas))
    assert not sin_bandeja_id, (
        f"estas funciones escriben personas sin poner bandeja_id en NINGUNA "
        f"rama: {sin_bandeja_id}")


def test_todo_insert_de_preferencias_pone_bandeja_id_en_alguna_rama():
    censo = _censo_de_inserts("preferencias")
    assert censo, "el censo no encontró ni un INSERT INTO preferencias: se quedó ciego"
    sin_bandeja_id = sorted(f for f, ramas in censo.items() if not any(ramas))
    assert not sin_bandeja_id, (
        f"estas funciones escriben preferencias sin poner bandeja_id en "
        f"NINGUNA rama: {sin_bandeja_id}")


# ═══════════════════════════════════════════════════════════════════════
# CENSO ESTRUCTURAL (hallazgo del testigo sobre `cbc2726`, NO PASA,
# 27-sep-2026): TODO reintento de una sentencia distinta tras capturar
# SQLSTATE 42703, en la MISMA función, tiene que envolver el intento que
# puede fallar en un SAVEPOINT (`async with conn.transaction():`) -- sin
# eso, Postgres real deja la transacción ABORTADA y el reintento revienta
# con 25P02. Esto recorre el AST del repositorio ENTERO (menos pruebas):
# no hace falta una lista tecleada de "qué funciones mirar", ni tampoco
# mantener a mano cuáles pruebas cubren cuáles sitios -- si el patrón
# aparece sin savepoint en CUALQUIER archivo, esto se pone rojo solo.
# ═══════════════════════════════════════════════════════════════════════

def _tiene_reintento_en_handler(handler: ast.ExceptHandler) -> bool:
    """¿El manejador de la excepción ejecuta OTRA sentencia (no solo
    relanza, loguea o hace `return`)?"""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("execute", "executemany")
        for n in ast.walk(handler))


def _atrapa_42703(nodo_try: ast.Try) -> bool:
    return "42703" in ast.dump(nodo_try)


def _primer_intento_tiene_savepoint(nodo_try: ast.Try) -> bool:
    """¿El CUERPO del `try` (el intento que puede fallar) está envuelto en
    un `with .transaction():`, sea `async with` (psycopg async, la mayoría
    del repo) o `with` liso (psycopg SÍNCRONO, como los guiones sueltos de
    `tools/`)? Mira los nodos de primer nivel del cuerpo del try -- que es
    donde vive ese `with` en todos los sitios reales del repo."""
    for n in nodo_try.body:
        if isinstance(n, (ast.AsyncWith, ast.With)) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "transaction"
                for item in n.items):
            return True
    return False


def _censo_de_reintentos_tras_42703() -> dict[str, bool]:
    """(archivo::función) -> tiene_savepoint, para cada función del
    repositorio (menos pruebas) que reintenta otra sentencia tras
    capturar SQLSTATE 42703."""
    import test_buzon_que_no_se_ve as barrido

    raiz = barrido.RAIZ
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    resultado: dict[str, bool] = {}
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        try:
            fuente = real.read_text(encoding="utf-8")
            arbol = ast.parse(fuente, filename=str(real))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = real.relative_to(raiz).as_posix()
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for nodo in ast.walk(funcion):
                if not isinstance(nodo, ast.Try):
                    continue
                if not _atrapa_42703(nodo):
                    continue
                if not any(_tiene_reintento_en_handler(h) for h in nodo.handlers):
                    continue
                clave = f"{rel}::{funcion.name}:{nodo.lineno}"
                resultado[clave] = _primer_intento_tiene_savepoint(nodo)
    return resultado


def test_todo_reintento_tras_42703_tiene_savepoint():
    censo = _censo_de_reintentos_tras_42703()
    assert censo, (
        "el censo no encontró ningún reintento tras SQLSTATE 42703 en todo "
        "el repositorio: se quedó ciego (hay varios, empezando por "
        "db._buscar_o_crear y crud.guardar_preferencia)")
    sin_savepoint = sorted(sitio for sitio, tiene in censo.items() if not tiene)
    assert not sin_savepoint, (
        f"estos sitios reintentan otra sentencia tras SQLSTATE 42703 SIN "
        f"envolver el intento en un SAVEPOINT (`async with conn."
        f"transaction():`) -- revientan con 25P02 contra Postgres real, "
        f"aunque una prueba con SQLite no lo vea: {sin_savepoint}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
