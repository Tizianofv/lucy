# -*- coding: utf-8 -*-
"""Micro-pasos (encargo 7, 22-sep-2026): una lista de chequeo dentro de una
tarea. «No, es una lista de chequeo» (Tiziano) -- sin fecha, sin
responsable, sin aviso propio.

Herméticos: se stubea `psycopg` antes de importar, igual que el resto de la
suite. Una base de mentira en memoria (`_BaseDePasos`) modela lo justo de
`tareas` y `micro_pasos` para que `acciones/crud.py::crear_pasos`,
`crud.editar`, `crud.borrar` y `crud.deshacer` corran DE VERDAD contra
ella -- no un doble que decida por su cuenta lo que esas funciones dicen
vigilar.

Correr:  python3 -m pytest tests/test_micro_pasos.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import date, datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

_psycopg = types.ModuleType("psycopg")
_psycopg_rows = types.ModuleType("psycopg.rows")
_psycopg_rows.dict_row = object()
_psycopg.rows = _psycopg_rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _psycopg_rows)

_psycopg_pool = types.ModuleType("psycopg_pool")


class _StubPool:
    def __init__(self, *a, **k):
        pass


_psycopg_pool.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _psycopg_pool)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import db.db as db  # noqa: E402
from acciones import crud  # noqa: E402

UTC = timezone.utc


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ═══════════════════════════════════════════════════════════════════════
# Una base de mentira que entiende `tareas` (solo lo que hace falta:
# existencia y borrado_en) y `micro_pasos` de verdad, con el SQL real de
# `acciones/crud.py` (crear_pasos, editar, borrar, deshacer) corriendo
# contra ella.
# ═══════════════════════════════════════════════════════════════════════

class _ErrorSQL(Exception):
    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(f"error de mentira, sqlstate={sqlstate}")


class _Transaccion:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _Cur:
    def __init__(self, row=None, filas=None):
        self._row = row
        self._filas = filas if filas is not None else ([row] if row is not None else [])

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._filas


class _CursorGenerico:
    def __init__(self, conn):
        self._conn = conn
        self._cur = None

    async def execute(self, sql, params=None):
        self._cur = await self._conn.execute(sql, params)
        return self._cur

    async def fetchone(self):
        return await self._cur.fetchone()

    async def fetchall(self):
        return await self._cur.fetchall()


class _BaseDePasos:
    def __init__(self, tareas: dict[int, dict], sin_tabla_pasos: bool = False):
        self.tareas = {tid: dict(f) for tid, f in tareas.items()}
        self.pasos: dict[int, dict] = {}
        self._siguiente_id = 0
        self._logid = 6000
        self.log: list[dict] = []
        self.sql: list[tuple[str, tuple]] = []
        self.sin_tabla_pasos = sin_tabla_pasos

    def cursor(self, row_factory=None):
        return _CursorGenerico(self)

    def transaction(self):
        return _Transaccion(self)

    # -- helpers de siembra --------------------------------------------
    def seed_paso(self, tarea_id, texto, orden, *, hecho=False, borrado_en=None):
        self._siguiente_id += 1
        pid = self._siguiente_id
        self.pasos[pid] = {"id": pid, "tarea_id": tarea_id, "texto": texto,
                           "hecho": hecho, "orden": orden,
                           "creado_en": datetime(2026, 9, 1, tzinfo=UTC),
                           "borrado_en": borrado_en}
        return pid

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self.sql.append((s, p))

        if self.sin_tabla_pasos and "micro_pasos" in s:
            raise _ErrorSQL("42P01")

        if s.startswith("SELECT id FROM tareas WHERE id"):
            (tid,) = p
            f = self.tareas.get(tid)
            if f is None or f.get("borrado_en") is not None:
                return _Cur(None)
            return _Cur((f["id"],))

        if s.startswith("SELECT id, texto, hecho, orden FROM micro_pasos"):
            (tid,) = p
            vivos = sorted(
                (f for f in self.pasos.values()
                 if f["tarea_id"] == tid and f["borrado_en"] is None),
                key=lambda f: (f["orden"], f["id"]))
            return _Cur(filas=[
                {"id": f["id"], "texto": f["texto"], "hecho": f["hecho"],
                 "orden": f["orden"]} for f in vivos])

        if s.startswith("SELECT COALESCE(MAX(orden), 0) AS siguiente_desde"):
            tid, = p
            vivos = [x["orden"] for x in self.pasos.values()
                    if x["tarea_id"] == tid and x["borrado_en"] is None]
            return _Cur({"siguiente_desde": max(vivos) if vivos else 0})

        if s.startswith("INSERT INTO micro_pasos"):
            tid, texto, orden = p
            self._siguiente_id += 1
            pid = self._siguiente_id
            fila = {"id": pid, "tarea_id": tid, "texto": texto,
                    "hecho": False, "orden": orden,
                    "creado_en": datetime(2026, 9, 22, tzinfo=UTC),
                    "borrado_en": None}
            self.pasos[pid] = fila
            return _Cur(dict(fila))

        if s.startswith("SELECT * FROM micro_pasos WHERE id") and "borrado_en IS NULL" in s:
            (pid,) = p
            f = self.pasos.get(pid)
            if f is None or f["borrado_en"] is not None:
                return _Cur(None)
            return _Cur(dict(f))

        if s.startswith("SELECT * FROM micro_pasos WHERE id"):
            (pid,) = p
            f = self.pasos.get(pid)
            return _Cur(dict(f) if f is not None else None)

        if s == "UPDATE micro_pasos SET borrado_en = now() WHERE id = %s":
            (pid,) = p
            self.pasos[pid]["borrado_en"] = datetime(2026, 9, 22, tzinfo=UTC)
            return _Cur(None)

        if s == ("UPDATE micro_pasos SET borrado_en = now() WHERE id = %s "
                "AND borrado_en IS NULL"):
            (pid,) = p
            if self.pasos[pid]["borrado_en"] is None:
                self.pasos[pid]["borrado_en"] = datetime(2026, 9, 22, tzinfo=UTC)
            return _Cur(None)

        if s == "UPDATE micro_pasos SET borrado_en = NULL WHERE id = %s":
            (pid,) = p
            self.pasos[pid]["borrado_en"] = None
            return _Cur(None)

        if s.startswith("UPDATE micro_pasos SET"):
            # El resto -- `editar()`, que arma `col = %s, ...` con un
            # placeholder POR columna -- sí tiene un valor por columna en
            # `p`, a diferencia de los tres casos de arriba (`now()`/`NULL`
            # van escritos en el SQL, no como parámetro).
            asignaciones = s.split(" SET ", 1)[1].split(" WHERE ")[0]
            columnas = [a.split("=")[0].strip() for a in asignaciones.split(",")]
            pid = p[-1]
            for c, v in zip(columnas, p[:-1]):
                self.pasos[pid][c] = v
            return _Cur(None)

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            import re as _re
            m = _re.search(r"log_acciones\s*\(([^)]*)\)", s)
            columnas = [c.strip() for c in m.group(1).split(",")]
            fila = dict(zip(columnas, p))
            fila["id"] = self._logid
            self.log.append(fila)
            return _Cur((self._logid,))

        if s.startswith("SELECT accion, tabla, registro_id, antes, despues"):
            (log_id,) = p
            hit = next((x for x in self.log if x["id"] == log_id), None)
            if hit is None:
                return _Cur(None)
            return _Cur({"accion": hit.get("accion"), "tabla": hit.get("tabla"),
                        "registro_id": hit.get("registro_id"),
                        "antes": hit.get("antes"), "despues": hit.get("despues")})

        raise AssertionError(f"SQL no modelado por _BaseDePasos: {s[:100]}")


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _PoolCM(self._conn)


def _tarea(id, *, borrado_en=None):
    return {"id": id, "borrado_en": borrado_en}


def _instalar(conn):
    guardado = db.pool
    db.pool = _FakePool(conn)
    return guardado


def _restaurar(guardado):
    db.pool = guardado


# ═══════════════════════════════════════════════════════════════════════
# 1) `_tarea_viva_que_vale`: la puerta.
# ═══════════════════════════════════════════════════════════════════════

def test_una_tarea_que_no_existe_se_rechaza():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._tarea_viva_que_vale(999))
            assert False, "tenía que rechazar una tarea que no existe"
        except ValueError as e:
            assert "999" in str(e)
    finally:
        _restaurar(guardado)


def test_una_tarea_borrada_no_vale():
    conn = _BaseDePasos({1: _tarea(1, borrado_en=datetime(2026, 9, 1))})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._tarea_viva_que_vale(1))
            assert False, "una tarea en la papelera no puede recibir pasos"
        except ValueError as e:
            assert "1" in str(e)
    finally:
        _restaurar(guardado)


def test_un_valor_que_no_es_numero_se_rechaza():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._tarea_viva_que_vale("la tarea grande"))
            assert False, "tenía que rechazar un texto que no es un número"
        except ValueError as e:
            assert "no es el número" in str(e)
    finally:
        _restaurar(guardado)


def test_una_tarea_viva_vale():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        assert _correr(crud._tarea_viva_que_vale(1)) == 1
        assert _correr(crud._tarea_viva_que_vale(" 1 ")) == 1
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 2) `crud.crear_pasos`
# ═══════════════════════════════════════════════════════════════════════

def test_crear_pasos_crea_uno_por_texto_en_orden():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        creados = _correr(crud.crear_pasos(1, ["primero", "segundo", "tercero"]))
    finally:
        _restaurar(guardado)
    assert len(creados) == 3
    vivos = sorted(conn.pasos.values(), key=lambda f: f["orden"])
    assert [f["texto"] for f in vivos] == ["primero", "segundo", "tercero"]
    assert [f["orden"] for f in vivos] == [1, 2, 3]
    # Un log_id POR paso -- cada uno se puede deshacer solo.
    assert len({log_id for _, log_id in creados}) == 3


def test_crear_pasos_agrega_despues_de_los_que_ya_habia():
    conn = _BaseDePasos({1: _tarea(1)})
    conn.seed_paso(1, "ya existía", 5)
    guardado = _instalar(conn)
    try:
        _correr(crud.crear_pasos(1, ["nuevo"]))
    finally:
        _restaurar(guardado)
    nuevo = next(f for f in conn.pasos.values() if f["texto"] == "nuevo")
    assert nuevo["orden"] == 6, "tiene que ir DESPUÉS del que ya había (orden 5)"


def test_crear_pasos_descarta_textos_vacios_y_falla_si_no_queda_ninguno():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        creados = _correr(crud.crear_pasos(1, ["  ", "de verdad", ""]))
        assert len(creados) == 1
        assert list(conn.pasos.values())[0]["texto"] == "de verdad"

        try:
            _correr(crud.crear_pasos(1, ["", "   "]))
            assert False, "sin ningún texto de verdad, tiene que fallar"
        except crud.FaltanDatos:
            pass
    finally:
        _restaurar(guardado)


def test_crear_pasos_tolera_la_tabla_ausente():
    conn = _BaseDePasos({1: _tarea(1)}, sin_tabla_pasos=True)
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud.crear_pasos(1, ["algo"]))
            assert False, "sin la tabla, tiene que avisar y no reventar feo"
        except ValueError as e:
            assert "migración" in str(e).lower() or "no está" in str(e).lower() \
                or "disponibles" in str(e).lower()
    finally:
        _restaurar(guardado)


def test_crear_pasos_rechaza_una_tarea_que_no_existe():
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud.crear_pasos(999, ["algo"]))
            assert False, "tenía que rechazar una tarea inexistente"
        except ValueError as e:
            assert "999" in str(e)
        assert conn.pasos == {}, "no se creó nada"
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 3) La puerta única -- prueba de hermanos, sacada del código real.
# ═══════════════════════════════════════════════════════════════════════

def test_todo_lo_que_toca_tarea_id_para_micro_pasos_pasa_por_la_misma_puerta():
    """Recorre CADA función de `acciones/crud.py` que mencione `tarea_id`
    COMO CLAVE DE DICCIONARIO ENTRECOMILLADA en su cuerpo -- `campos[
    "tarea_id"]`, `{"tarea_id": ...}` -- y exige que TODAS llamen a
    `_tarea_viva_que_vale`. Mismo patrón (y misma frontera) que las
    pruebas gemelas sobre `area` y `primero_id`.

    SOLO `editar()` ENTRA EN ESTE BARRIDO, y no es un descuido: `crear_
    pasos` recibe `tarea_id` como un PARÁMETRO normal de la función --
    `crear_pasos(tarea_id, textos, ...)` -- nunca como clave de un dict
    (`campos["tarea_id"]`), así que `ast.unparse` nunca deja la forma
    entrecomillada que esta prueba persigue (medido: ni `"tarea_id"` ni
    `'tarea_id'` aparecen en su código fuente). Que SÍ llama a la puerta se
    prueba aparte, corriendo la función de verdad -- ver
    `test_crear_pasos_rechaza_una_tarea_que_no_existe`, más arriba.

    LA FRONTERA de lo que ESTA prueba puntual no ve: un sitio que escriba
    `tarea_id` como texto suelto dentro de un literal de SQL crudo
    (`"INSERT INTO micro_pasos (tarea_id, ...)"`, que sí existe en
    `crear_pasos`) -- ahí las comillas que deja `ast.unparse` son las de la
    CADENA ENTERA, no las de la palabra `tarea_id`. Mismo escape que ya
    declaran las pruebas gemelas de `area` y `primero_id`.
    """
    import ast
    import inspect

    fuente = inspect.getsource(crud)
    arbol = ast.parse(fuente)
    culpables = []
    vistas = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if nodo.name == "_tarea_viva_que_vale":
            continue
        texto = ast.unparse(nodo)
        menciona = '"tarea_id"' in texto or "'tarea_id'" in texto
        if not menciona:
            continue
        vistas.append(nodo.name)
        if "_tarea_viva_que_vale(" not in texto:
            culpables.append(nodo.name)
    assert vistas, "no se encontró ninguna función que mencione 'tarea_id'"
    assert set(vistas) == {"editar"}, (
        f"aparecieron funciones nuevas que tocan 'tarea_id' como clave de "
        f"dict: {set(vistas) - {'editar'}}. Revisá si pasan por "
        f"_tarea_viva_que_vale y agregalas a la lista esperada.")
    assert not culpables, (
        f"estas funciones mencionan 'tarea_id' sin llamar a "
        f"_tarea_viva_que_vale: {culpables}")


def test_editar_rechaza_reparentar_a_una_tarea_que_no_existe():
    """`editar("micro_pasos", id, {"tarea_id": 999})` -- nadie lo pide hoy,
    pero `editar()` es genérico y sin la puerta lo escribiría sin
    comprobar nada."""
    conn = _BaseDePasos({1: _tarea(1)})
    conn.seed_paso(1, "un paso", 1)
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud.editar("micro_pasos", 1, {"tarea_id": 999}, motivo="t"))
            assert False, "tenía que rechazar reparentar a una tarea inexistente"
        except ValueError as e:
            assert "999" in str(e)
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 4) `micro_pasos` YA ESTÁ EN `crud.TABLAS`: editar/borrar/deshacer
#    genéricos la sirven gratis. Se corre DE VERDAD, no se asume.
# ═══════════════════════════════════════════════════════════════════════

def test_micro_pasos_esta_en_tablas():
    assert "micro_pasos" in crud.TABLAS


def test_editar_marca_un_paso_hecho():
    conn = _BaseDePasos({1: _tarea(1)})
    pid = conn.seed_paso(1, "lavar los platos", 1)
    guardado = _instalar(conn)
    try:
        despues, log_id = _correr(crud.editar(
            "micro_pasos", pid, {"hecho": True}, motivo="test", actor="panel"))
        assert despues["hecho"] is True
        assert log_id is not None
        huella = conn.log[-1]
        assert huella["accion"] == "editar" and huella["tabla"] == "micro_pasos"
        assert huella["actor"] == "panel"
    finally:
        _restaurar(guardado)


def test_borrar_quita_un_paso_y_deja_huella():
    conn = _BaseDePasos({1: _tarea(1)})
    pid = conn.seed_paso(1, "sacar la basura", 1)
    guardado = _instalar(conn)
    try:
        log_id = _correr(crud.borrar("micro_pasos", pid, motivo="quitado a mano"))
        assert log_id is not None
        assert conn.pasos[pid]["borrado_en"] is not None
        huella = conn.log[-1]
        assert huella["accion"] == "borrar" and huella["tabla"] == "micro_pasos"
    finally:
        _restaurar(guardado)


def test_deshacer_un_crear_de_micro_pasos_lo_archiva():
    """`crear_pasos` deja una huella `accion='crear'`. Deshacerla tiene que
    archivar ESE paso -- la rama genérica de `crud.deshacer()`, sin que
    `micro_pasos` necesite ningún caso especial."""
    conn = _BaseDePasos({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        creados = _correr(crud.crear_pasos(1, ["un paso nuevo"]))
        (pid, log_id), = creados
        assert conn.pasos[pid]["borrado_en"] is None

        que = _correr(crud.deshacer(log_id))
        assert conn.pasos[pid]["borrado_en"] is not None
        assert "creado" in que or "creé" in que or que
    finally:
        _restaurar(guardado)


def test_deshacer_un_borrar_de_micro_pasos_lo_restaura():
    conn = _BaseDePasos({1: _tarea(1)})
    pid = conn.seed_paso(1, "paso", 1)
    guardado = _instalar(conn)
    try:
        log_id = _correr(crud.borrar("micro_pasos", pid, motivo="t"))
        assert conn.pasos[pid]["borrado_en"] is not None
        _correr(crud.deshacer(log_id))
        assert conn.pasos[pid]["borrado_en"] is None
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 5) `db.pasos_de_tarea`, `db.conteo_pasos`: lectura, con SQL real en
#    sqlite para el orden y el filtro de vivos.
# ═══════════════════════════════════════════════════════════════════════

def _extraer_sql(fn, nombre_variable: str) -> str:
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Assign) and len(nodo.targets) == 1
                and isinstance(nodo.targets[0], ast.Name)
                and nodo.targets[0].id == nombre_variable
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)):
            return nodo.value.value
    raise AssertionError(f"no se encontró {nombre_variable!r} en {fn.__name__}")


def _extraer_sql_de_execute(fn, contiene: str) -> str:
    """El texto (ya concatenado -- Python junta literales adyacentes al
    parsear) del primer argumento de un `cur.execute(...)` dentro de `fn`
    que contenga la subcadena `contiene`. Por AST, no por regex sobre el
    texto crudo: `pasos_de_tarea` y `conteo_pasos` arman su SQL con dos
    literales de cadena pegados (`"SELECT ..." " WHERE ..."`), no con
    triples comillas, así que una regex que busque `\"\"\"..\"\"\"` no lo
    encuentra -- medido rompiendo la primera versión de esta prueba."""
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "execute" and nodo.args):
            continue
        primero = nodo.args[0]
        if isinstance(primero, ast.Constant) and isinstance(primero.value, str) \
                and contiene in primero.value:
            return primero.value
    raise AssertionError(f"no se encontró un execute(...) con {contiene!r} "
                         f"en {fn.__name__}")


def test_pasos_de_tarea_en_sqlite_respeta_orden_y_vivos():
    """El SQL REAL de `pasos_de_tarea`, extraído del código, corrido en
    sqlite (solo %s -> ?) contra filas de verdad: un paso borrado no debe
    salir, y el orden manda sobre el id."""
    import sqlite3

    sql = _extraer_sql_de_execute(db.pasos_de_tarea, "FROM micro_pasos")
    assert "ORDER BY orden, id" in sql, (
        "el SQL real no ordena por 'orden, id'")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE micro_pasos (id INTEGER PRIMARY KEY, "
                 "tarea_id INTEGER, texto TEXT, hecho INTEGER, orden INTEGER, "
                 "borrado_en TEXT)")
    filas = [
        (10, 1, "segundo", 0, 2, None),
        (11, 1, "primero", 0, 1, None),
        (12, 1, "borrado, no debe salir", 0, 3, "2026-09-01"),
        (13, 2, "de otra tarea", 0, 1, None),
    ]
    conn.executemany(
        "INSERT INTO micro_pasos VALUES (?, ?, ?, ?, ?, ?)", filas)
    conn.commit()
    sql_sqlite = sql.replace("%s", "?")
    cur = conn.execute(sql_sqlite, (1,))
    vistos = [dict(r) for r in cur.fetchall()]
    conn.close()

    assert [f["texto"] for f in vistos] == ["primero", "segundo"], (
        f"orden o filtro de vivos equivocado: {vistos}")


def test_pasos_de_tarea_tolera_la_tabla_ausente():
    class _ConnSinTabla:
        def cursor(self, row_factory=None):
            return self

        def transaction(self):
            return _Transaccion(self)

        async def execute(self, sql, params=None):
            raise _ErrorSQL("42P01")

    guardado = _instalar(_ConnSinTabla())
    try:
        assert _correr(db.pasos_de_tarea(1)) == []
    finally:
        _restaurar(guardado)


def test_conteo_pasos_vacio_no_toca_la_base():
    class _NuncaLlamar:
        def cursor(self, row_factory=None):
            raise AssertionError("no debería abrir cursor con lista vacía")

    guardado = _instalar(_NuncaLlamar())
    try:
        assert _correr(db.conteo_pasos([])) == {}
    finally:
        _restaurar(guardado)


def test_conteo_pasos_tolera_la_tabla_ausente():
    class _ConnSinTabla:
        def cursor(self, row_factory=None):
            return self

        def transaction(self):
            return _Transaccion(self)

        async def execute(self, sql, params=None):
            raise _ErrorSQL("42P01")

    guardado = _instalar(_ConnSinTabla())
    try:
        assert _correr(db.conteo_pasos([1, 2])) == {}
    finally:
        _restaurar(guardado)


def test_conteo_pasos_cuenta_hechos_y_total_en_sqlite():
    """SQL real de `conteo_pasos`, corrido en sqlite con filas reales.

    DOS TRADUCCIONES, ninguna cambia lo que se mide: `%s` -> `?` (de
    siempre) y `= ANY(%s)` -> `IN (?, ?)` -- sqlite no tiene `ANY` sobre un
    array de Postgres, así que el operador de "está en esta lista" se
    escribe distinto en cada motor; el WHERE que decide sigue siendo el
    mismo. `count(*) FILTER (WHERE hecho)` -> `sum(hecho)` es la misma
    cuenta (sqlite no tiene FILTER): un booleano 1/0 sumado es cuántos
    salieron verdaderos.
    """
    import sqlite3

    sql = _extraer_sql_de_execute(db.conteo_pasos, "FROM micro_pasos")
    assert "= ANY(%s)" in sql, "el SQL real no filtra con ANY(%s)"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE micro_pasos (id INTEGER PRIMARY KEY, "
                 "tarea_id INTEGER, hecho INTEGER, borrado_en TEXT)")
    conn.executemany(
        "INSERT INTO micro_pasos (tarea_id, hecho, borrado_en) VALUES (?, ?, ?)",
        [(1, 1, None), (1, 0, None), (1, 1, "2026-09-01"),  # 2 vivos, 1 hecho, 1 borrado
         (2, 0, None)])
    conn.commit()
    ids = [1, 2]
    sql_sqlite = (
        sql.replace("count(*) FILTER (WHERE hecho)", "sum(hecho)")
           .replace("tarea_id = ANY(%s)",
                    f"tarea_id IN ({','.join('?' * len(ids))})"))
    cur = conn.execute(sql_sqlite, ids)
    filas = {r["tarea_id"]: dict(r) for r in cur.fetchall()}
    conn.close()

    assert filas[1]["total"] == 2
    assert filas[1]["hechos"] == 1
    assert filas[2]["total"] == 1
    assert filas[2]["hechos"] == 0


# ═══════════════════════════════════════════════════════════════════════
# 6) `db.mover_paso`
# ═══════════════════════════════════════════════════════════════════════

class _CurMover:
    def __init__(self, conn):
        self._conn = conn
        self._filas = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._conn.sql.append((s, p))

        if s.startswith("SELECT id, orden FROM micro_pasos") and "orden < %s" not in s \
                and "orden > %s" not in s:
            pid, tid = p
            f = self._conn.pasos.get(pid)
            if f is None or f["tarea_id"] != tid or f["borrado_en"] is not None:
                self._filas = []
            else:
                self._filas = [dict(f)]
            return self

        if "orden < %s" in s:
            tid, orden = p
            candidatos = [f for f in self._conn.pasos.values()
                         if f["tarea_id"] == tid and f["borrado_en"] is None
                         and f["orden"] < orden]
            candidatos.sort(key=lambda f: (-f["orden"], -f["id"]))
            self._filas = [dict(candidatos[0])] if candidatos else []
            return self

        if "orden > %s" in s:
            tid, orden = p
            candidatos = [f for f in self._conn.pasos.values()
                         if f["tarea_id"] == tid and f["borrado_en"] is None
                         and f["orden"] > orden]
            candidatos.sort(key=lambda f: (f["orden"], f["id"]))
            self._filas = [dict(candidatos[0])] if candidatos else []
            return self

        if s.startswith("UPDATE micro_pasos SET orden"):
            orden, pid = p
            self._conn.pasos[pid]["orden"] = orden
            return self

        if s.startswith("INSERT INTO log_acciones"):
            self._conn._logid += 1
            self._conn.log.append({"id": self._conn._logid})
            return self

        raise AssertionError(f"SQL no modelado por _CurMover: {s[:90]}")

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnMover(_BaseDePasos):
    def cursor(self, row_factory=None):
        return _CurMover(self)


def test_mover_arriba_intercambia_con_el_vecino():
    conn = _ConnMover({1: _tarea(1)})
    a = conn.seed_paso(1, "a", 1)
    b = conn.seed_paso(1, "b", 2)
    guardado = _instalar(conn)
    try:
        movido = _correr(db.mover_paso(1, b, "arriba"))
        assert movido is True
        assert conn.pasos[b]["orden"] == 1
        assert conn.pasos[a]["orden"] == 2
        assert len(conn.log) == 2, "dos huellas, una por fila tocada"
    finally:
        _restaurar(guardado)


def test_mover_en_la_punta_no_hace_nada():
    conn = _ConnMover({1: _tarea(1)})
    a = conn.seed_paso(1, "a", 1)
    guardado = _instalar(conn)
    try:
        assert _correr(db.mover_paso(1, a, "arriba")) is False
        assert conn.pasos[a]["orden"] == 1
        assert conn.log == []
    finally:
        _restaurar(guardado)


def test_mover_una_direccion_invalida_no_hace_nada():
    conn = _ConnMover({1: _tarea(1)})
    a = conn.seed_paso(1, "a", 1)
    conn.seed_paso(1, "b", 2)
    guardado = _instalar(conn)
    try:
        assert _correr(db.mover_paso(1, a, "izquierda")) is False
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 7) Qué pasa con los pasos de una tarea borrada: SE QUEDAN, no se tocan
#    -- comprobado corriendo el código, no leído del comentario.
# ═══════════════════════════════════════════════════════════════════════

def test_los_pasos_de_una_tarea_borrada_se_quedan_colgados():
    """`pasos_de_tarea` NO filtra por el `borrado_en` de la TAREA -- solo
    por el de los PASOS. Una tarea archivada sigue enseñando su checklist
    si se le pregunta directo (la ruta HTTP de una tarea borrada ya
    devuelve 404 desde `db.tarea_con_comentarios`, pero eso es una
    decisión de OTRA función -- ésta, que es la de más abajo, no vuelve a
    decidir lo mismo)."""
    conn = _BaseDePasos({1: _tarea(1, borrado_en=datetime(2026, 9, 20, tzinfo=UTC))})
    conn.seed_paso(1, "seguía sin marcar", 1)
    guardado = _instalar(conn)
    try:
        vivos = _correr(db.pasos_de_tarea(1))
    finally:
        _restaurar(guardado)
    assert len(vivos) == 1
    assert vivos[0]["texto"] == "seguía sin marcar"


# ═══════════════════════════════════════════════════════════════════════
# 8) El panel: la plantilla de verdad trae el «2 de 5» y la lista de
#    chequeo.
# ═══════════════════════════════════════════════════════════════════════

def test_la_lista_pinta_el_conteo_de_pasos():
    from pathlib import Path
    html = Path(_ROOT, "web", "plantillas", "tareas.html").read_text(encoding="utf-8")
    assert "pasos_total" in html and "pasos_hechos" in html


def test_la_pagina_de_la_tarea_tiene_la_lista_de_chequeo():
    from pathlib import Path
    html = Path(_ROOT, "web", "plantillas", "tarea_detalle.html").read_text(
        encoding="utf-8")
    assert '/tareas/{{ tarea.id }}/pasos"' in html, "no hay form para agregar pasos"
    assert '/hecho"' in html, "no hay form para marcar un paso hecho"
    assert '/mover"' in html, "no hay form para mover un paso"
    assert 'name="texto"' in html


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
