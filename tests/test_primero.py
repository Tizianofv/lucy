# -*- coding: utf-8 -*-
"""«Primero:» (encargo 6, 22-sep-2026): una tarea puede esperar a otra.

Herméticos: se stubea `psycopg` antes de importar, igual que el resto de la
suite. Nada de esto toca una base real -- de producción, menos. Una base de
mentira en memoria (`_BaseDeTareas`) modela lo justo de `tareas` para que
`acciones/crud.py::_primero_que_vale`, `crud.editar` y `db.tareas_por_grupo`
corran DE VERDAD contra ella (no un doble que decida por su cuenta lo que
esas funciones dicen vigilar).

QUÉ NO PUEDE VER ESTA SUITE, dicho con la verdad y no tapado: que la base
real rechace una tarea que se apunta a sí misma
(`tareas_primero_no_a_si_misma`, un CHECK de Postgres) -- eso no hay cómo
ejercitarlo sin un Postgres, y en esta Mac no hay uno. Lo que SÍ se prueba de
punta a punta es la comprobación GEMELA que hace `_primero_que_vale` en
Python, con el mismo criterio, ANTES de llegar a esa base.

Correr:  python3 -m pytest tests/test_primero.py -q
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
# Una base de mentira que SÍ entiende la cadena de `primero_id`: un dict
# {id: fila}, y el SQL de verdad que emite `_primero_que_vale` y `editar`
# contra ella. No es un doble que "decida" el veredicto -- es el motor
# mínimo que hace que ESAS funciones corran su propio código.
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


class _BaseDeTareas:
    """`{id: fila}` de `tareas`, y el SQL real de `_primero_que_vale` /
    `crud.editar` contra ella. `sin_columna_primero=True` hace que CUALQUIER
    SQL que nombre `primero_id` reviente con 42703, para probar la cascada de
    tolerancia."""

    def __init__(self, tareas: dict[int, dict], *, sin_columna_primero=False):
        self.tareas = {tid: dict(f) for tid, f in tareas.items()}
        self.sql: list[tuple[str, tuple]] = []
        self.sin_columna_primero = sin_columna_primero
        self._logid = 5000

    def cursor(self, row_factory=None):
        return _CursorGenerico(self)

    def transaction(self):
        return _Transaccion(self)

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self.sql.append((s, p))

        if self.sin_columna_primero and "primero_id" in s:
            raise _ErrorSQL("42703")

        if s.startswith("SELECT id, primero_id FROM tareas WHERE id"):
            (tid,) = p
            f = self.tareas.get(tid)
            if f is None or f.get("borrado_en") is not None:
                return _Cur(None)
            return _Cur((f["id"], f.get("primero_id")))

        if s.startswith("SELECT primero_id FROM tareas WHERE id"):
            (tid,) = p
            f = self.tareas.get(tid)
            return _Cur((f.get("primero_id"),) if f is not None else None)

        if s.startswith("SELECT * FROM tareas WHERE id") and "borrado_en IS NULL" in s:
            (tid,) = p
            f = self.tareas.get(tid)
            if f is None or f.get("borrado_en") is not None:
                return _Cur(None)
            return _Cur(dict(f))

        if s.startswith("SELECT * FROM tareas WHERE id"):
            (tid,) = p
            f = self.tareas.get(tid)
            return _Cur(dict(f) if f is not None else None)

        if s.startswith("UPDATE tareas SET"):
            asignaciones = s.split(" SET ", 1)[1].split(" WHERE ")[0]
            columnas = [a.split("=")[0].strip() for a in asignaciones.split(",")]
            tid = p[-1]
            for c, v in zip(columnas, p[:-1]):
                self.tareas[tid][c] = v
            return _Cur(None)

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            return _Cur((self._logid,))

        raise AssertionError(f"SQL no modelado por _BaseDeTareas: {s[:90]}")


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


def _tarea(id, *, primero_id=None, estado="pendiente", borrado_en=None):
    return {"id": id, "bandeja_id": None, "titulo": f"tarea {id}",
            "detalle": None, "vence_en": None, "estado": estado,
            "recurrencia": None, "pospuesta_veces": 0, "anticipos_min": [0],
            "avisos_enviados": [], "area": None, "proyecto_id": None,
            "responsable_chat_id": None, "borrado_en": borrado_en,
            "primero_id": primero_id,
            "creado_en": datetime(2026, 9, 1, 9, 0, tzinfo=UTC)}


def _instalar(conn):
    guardado = db.pool
    db.pool = _FakePool(conn)
    return guardado


def _restaurar(guardado):
    db.pool = guardado


# ═══════════════════════════════════════════════════════════════════════
# 1) `_primero_que_vale`: vacío, número inválido, autorreferencia, tarea
#    que no existe, y la cadena sin círculos.
# ═══════════════════════════════════════════════════════════════════════

def test_vacio_o_none_no_pasa_por_la_puerta():
    conn = _BaseDeTareas({})
    guardado = _instalar(conn)
    try:
        assert _correr(crud._primero_que_vale(None)) is None
        assert _correr(crud._primero_que_vale("")) is None
        assert _correr(crud._primero_que_vale("   ")) is None
    finally:
        _restaurar(guardado)
    assert conn.sql == [], "sin valor, no debería tocar la base para nada"


def test_un_valor_que_no_es_numero_se_rechaza():
    conn = _BaseDeTareas({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale("la tarea de mañana"))
            assert False, "tenía que rechazar un texto que no es un número"
        except ValueError as e:
            assert "no es el número" in str(e)
    finally:
        _restaurar(guardado)


def test_una_tarea_no_puede_ser_su_propia_primero():
    conn = _BaseDeTareas({7: _tarea(7)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale(7, tarea_id=7))
            assert False, "tenía que rechazar que la tarea se apunte a sí misma"
        except ValueError as e:
            assert "propia" in str(e).lower()
    finally:
        _restaurar(guardado)


def test_al_crear_no_hay_tarea_id_asi_que_no_hay_autorreferencia_posible():
    """Al CREAR (`tarea_id=None`, el default) la tarea todavía no tiene id,
    así que pedir un `primero_id` cualquiera que exista se acepta sin que
    la puerta intente comparar contra "sí misma" -- no hay nada con qué
    comparar."""
    conn = _BaseDeTareas({7: _tarea(7)})
    guardado = _instalar(conn)
    try:
        assert _correr(crud._primero_que_vale(7)) == 7
    finally:
        _restaurar(guardado)


def test_una_tarea_que_no_existe_se_rechaza_con_motivo():
    conn = _BaseDeTareas({1: _tarea(1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale(999))
            assert False, "tenía que rechazar una tarea que no existe"
        except ValueError as e:
            assert "999" in str(e)
    finally:
        _restaurar(guardado)


def test_una_tarea_borrada_no_vale_como_primero():
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, borrado_en=datetime(2026, 9, 1))})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale(2))
            assert False, "una tarea en la papelera no puede ser Primero:"
        except ValueError as e:
            assert "2" in str(e)
    finally:
        _restaurar(guardado)


def test_un_circulo_directo_se_rechaza():
    """A (id=1) editada para esperar a B (id=2), que YA espera a A. Cerrar
    esto formaría un círculo A→B→A."""
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, primero_id=1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale(2, tarea_id=1))
            assert False, "tenía que rechazar el círculo A→B→A"
        except ValueError as e:
            assert "círculo" in str(e).lower()
    finally:
        _restaurar(guardado)


def test_un_circulo_indirecto_de_tres_tambien_se_rechaza():
    """A(1) editada para esperar a C(3); C ya espera a B(2); B ya espera a
    A(1). A→C→B→A: un círculo de tres, no de dos."""
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, primero_id=1),
                          3: _tarea(3, primero_id=2)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud._primero_que_vale(3, tarea_id=1))
            assert False, "tenía que rechazar el círculo A→C→B→A"
        except ValueError as e:
            assert "círculo" in str(e).lower()
    finally:
        _restaurar(guardado)


def test_una_cadena_sin_circulo_se_acepta():
    """A(1) editada para esperar a B(2); B espera a C(3); C no espera a
    nadie. A→B→C es una cadena válida, no un círculo."""
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, primero_id=3), 3: _tarea(3)})
    guardado = _instalar(conn)
    try:
        assert _correr(crud._primero_que_vale(2, tarea_id=1)) == 2
    finally:
        _restaurar(guardado)


def test_dos_tareas_apuntando_a_la_misma_tercera_no_es_un_circulo():
    """B(2) y C(3) esperan las DOS a D(4) -- una forma de árbol, no de
    círculo. Editar A(1) para esperar a B tiene que aceptarse igual."""
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, primero_id=4),
                          3: _tarea(3, primero_id=4), 4: _tarea(4)})
    guardado = _instalar(conn)
    try:
        assert _correr(crud._primero_que_vale(2, tarea_id=1)) == 2
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 2) `crud.editar("tareas", ..., {"primero_id": ...})` de punta a punta.
# ═══════════════════════════════════════════════════════════════════════

def test_editar_guarda_el_primero_id():
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2)})
    guardado = _instalar(conn)
    try:
        despues, log_id = _correr(crud.editar(
            "tareas", 1, {"primero_id": 2}, motivo="test"))
        assert despues["primero_id"] == 2
        assert log_id is not None
        assert conn.tareas[1]["primero_id"] == 2
    finally:
        _restaurar(guardado)


def test_editar_quita_el_primero_id_con_null():
    conn = _BaseDeTareas({1: _tarea(1, primero_id=2), 2: _tarea(2)})
    guardado = _instalar(conn)
    try:
        despues, _log_id = _correr(crud.editar(
            "tareas", 1, {"primero_id": None}, motivo="test"))
        assert despues["primero_id"] is None
    finally:
        _restaurar(guardado)


def test_editar_rechaza_el_circulo_y_no_escribe_nada():
    conn = _BaseDeTareas({1: _tarea(1), 2: _tarea(2, primero_id=1)})
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud.editar("tareas", 1, {"primero_id": 2}, motivo="t"))
            assert False, "tenía que rechazar el círculo"
        except ValueError as e:
            assert "círculo" in str(e).lower()
        assert conn.tareas[1]["primero_id"] is None, (
            "no se escribió nada: el rechazo fue ANTES del UPDATE")
    finally:
        _restaurar(guardado)


def test_editar_sobre_columna_ausente_da_un_motivo_claro():
    """Si `tareas.primero_id` todavía no existe (la migración de este
    encargo no se aplicó), el `SELECT *` que lee la fila ANTES de escribir
    no trae esa clave, y `editar()` la rechaza con un mensaje legible --
    sin SAVEPOINT ni tolerancia especial, mismo patrón que `area` (ver
    docstring de `crud.editar`)."""
    conn = _BaseDeTareas({1: _tarea(1)})
    del conn.tareas[1]["primero_id"]  # como sería una fila SIN esa columna
    guardado = _instalar(conn)
    try:
        try:
            _correr(crud.editar("tareas", 1, {"primero_id": 2}, motivo="t"))
            assert False, "tenía que rechazar: la columna no está en la fila"
        except ValueError as e:
            assert "primero_id" in str(e)
    finally:
        _restaurar(guardado)


# ═══════════════════════════════════════════════════════════════════════
# 3) La puerta única: toda función de crud.py que mencione "primero_id"
#    como clave de diccionario pasa por `_primero_que_vale`. Mismo patrón
#    (y misma frontera declarada) que
#    test_crud_dedup.py::test_todo_lo_que_toca_area_o_proyecto_id_en_crud_
#    pasa_por_la_misma_puerta.
# ═══════════════════════════════════════════════════════════════════════

def test_todo_lo_que_toca_primero_id_en_crud_pasa_por_la_misma_puerta():
    """Recorre CADA función de `acciones/crud.py` que mencione `primero_id`
    COMO CLAVE DE DICCIONARIO ENTRECOMILLADA en su cuerpo -- `campos[
    "primero_id"]`, `{"primero_id": ...}` -- y exige que TODAS llamen a
    `_primero_que_vale`.

    LA FRONTERA, la MISMA que ya declara la prueba gemela sobre `area`: esto
    ve un sitio que escribe `primero_id` como clave de dict -- así es como
    `ast.unparse` deja el nombre, entre comillas propias. NO VE un sitio
    que escriba la columna como texto suelto dentro de una cadena de SQL
    crudo (`"UPDATE tareas SET primero_id = %s WHERE id = %s"`), porque ahí
    las comillas que `ast.unparse` pone son las de la CADENA ENTERA. Ese
    escape ya existe en este archivo, para otra columna
    (`responsable_chat_id`, `acciones/crud.py` dentro de
    `crear_desde_interpretacion`) y no se cierra acá tampoco.
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
        if nodo.name == "_primero_que_vale":
            continue  # la puerta misma, obviamente menciona la clave
        texto = ast.unparse(nodo)
        menciona = '"primero_id"' in texto or "'primero_id'" in texto
        if not menciona:
            continue
        vistas.append(nodo.name)
        if "_primero_que_vale(" not in texto:
            culpables.append(nodo.name)
    assert vistas, ("no se encontró ninguna función que mencione "
                    "'primero_id' -- la prueba dejó de medir algo")
    assert set(vistas) == {"crear_desde_interpretacion", "editar"}, (
        f"aparecieron funciones nuevas que tocan 'primero_id': "
        f"{set(vistas) - {'crear_desde_interpretacion', 'editar'}}. Revisá si "
        f"pasan por _primero_que_vale y agregalas a la lista esperada.")
    assert not culpables, (
        f"estas funciones de crud.py mencionan 'primero_id' sin llamar a "
        f"_primero_que_vale: {culpables}")


# ═══════════════════════════════════════════════════════════════════════
# 4) `db.tareas_por_grupo`: la tarea que espera se marca, y se "enciende"
#    sola cuando la de antes deja de estar pendiente.
# ═══════════════════════════════════════════════════════════════════════

class _CursorPanel:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if self._conn.sin_columna_primero and "primero_id" in s:
            raise _ErrorSQL("42703")
        if s.startswith("SELECT t.id, t.titulo"):
            self._filas = list(self._conn.filas)
        else:
            self._filas = []
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _ConnPanel:
    def __init__(self, filas, sin_columna_primero=False):
        self.filas = filas
        self.sin_columna_primero = sin_columna_primero
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CursorPanel(self)

    async def execute(self, sql, params=None):
        return await _CursorPanel(self).execute(sql, params)

    def transaction(self):
        return _Transaccion(self)


def _fila_panel(id, *, estado="pendiente", primero_id=None,
                primero_titulo=None, primero_estado=None):
    return {"id": id, "titulo": f"tarea {id}", "estado": estado,
            "vence_en": None, "creado_en": datetime(2026, 9, 1, tzinfo=UTC),
            "bandeja_id": None, "responsable_chat_id": None,
            "completado_en": None, "area": None, "proyecto_id": None,
            "proyecto_nombre": None, "primero_id": primero_id,
            "primero_titulo": primero_titulo, "primero_estado": primero_estado}


def _grupos(filas):
    conn = _ConnPanel(filas)
    guardado = _instalar(conn)
    try:
        return _correr(db.tareas_por_grupo(hoy=date(2026, 9, 22)))
    finally:
        _restaurar(guardado)


def _todas(datos):
    return {t["id"]: t for g in datos["grupos"] for t in g["filas"]}


def test_una_tarea_que_espera_se_marca_esperando():
    filas = [_fila_panel(1, primero_id=2, primero_titulo="la de antes",
                         primero_estado="pendiente")]
    por_id = _todas(_grupos(filas))
    assert por_id[1]["primero_esperando"] is True
    assert por_id[1]["primero_titulo"] == "la de antes"


def test_una_tarea_sin_primero_no_espera():
    por_id = _todas(_grupos([_fila_panel(1)]))
    assert por_id[1]["primero_esperando"] is False


def test_cuando_la_de_antes_se_hace_la_que_esperaba_se_enciende_sola():
    """MISMA fila, el único cambio es el estado de la tarea "Primero:" --
    nada se reescribió en la fila que esperaba: se enciende SOLA porque
    "esperando" nunca se guardó, se calcula al pintar."""
    esperando = _fila_panel(1, primero_id=2, primero_titulo="la de antes",
                            primero_estado="pendiente")
    ya_hecha = _fila_panel(1, primero_id=2, primero_titulo="la de antes",
                           primero_estado="hecha")
    assert _todas(_grupos([esperando]))[1]["primero_esperando"] is True
    assert _todas(_grupos([ya_hecha]))[1]["primero_esperando"] is False


def test_si_la_de_antes_esta_descartada_tambien_se_enciende():
    fila = _fila_panel(1, primero_id=2, primero_titulo="la de antes",
                       primero_estado="descartado")
    assert _todas(_grupos([fila]))[1]["primero_esperando"] is False


def test_si_la_de_antes_se_borra_se_enciende_y_no_hay_titulo_que_mostrar():
    """El JOIN de `tareas_por_grupo` excluye la tarea "Primero:" borrada, así
    que `primero_titulo`/`primero_estado` llegan en None -- la fila se ve
    normal, sin "→ Primero:"."""
    fila = _fila_panel(1, primero_id=2, primero_titulo=None, primero_estado=None)
    resultado = _todas(_grupos([fila]))[1]
    assert resultado["primero_esperando"] is False
    assert resultado["primero_titulo"] is None


def test_columna_primero_id_ausente_cae_en_cascada_sin_romper_el_panel():
    conn = _ConnPanel([{"id": 1, "titulo": "vieja", "estado": "pendiente",
                        "vence_en": None,
                        "creado_en": datetime(2026, 9, 1, tzinfo=UTC),
                        "bandeja_id": None, "responsable_chat_id": None,
                        "completado_en": None}],
                      sin_columna_primero=True)
    guardado = _instalar(conn)
    try:
        datos = _correr(db.tareas_por_grupo(hoy=date(2026, 9, 22)))
    finally:
        _restaurar(guardado)
    fila = _todas(datos)[1]
    assert fila["primero_esperando"] is False
    assert fila["primero_id"] is None
    assert fila["primero_titulo"] is None


# ═══════════════════════════════════════════════════════════════════════
# 5) El despertador: una tarea que espera no suena. Corre
#    `despertador.revisar(bot)` DE VERDAD (no un doble que decida el
#    resultado), con una conexión de mentira que solo captura el SQL y
#    sirve filas vacías -- lo que se mide es el TEXTO de la consulta que de
#    verdad se ejecuta, mismo espíritu que `tests/test_panel_tareas.py`.
# ═══════════════════════════════════════════════════════════════════════

class _CurDespertador:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append(s)
        if self._conn.sin_columna_primero and "primero_id" in s:
            raise _ErrorSQL("42703")
        if s.startswith("INSERT INTO log_acciones"):
            return _Cur((1,))
        return self

    async def fetchall(self):
        return []

    async def fetchone(self):
        return None


class _ConnDespertador:
    def __init__(self, sin_columna_primero=False):
        self.sql: list[str] = []
        self.sin_columna_primero = sin_columna_primero

    def cursor(self, row_factory=None):
        return _CurDespertador(self)

    async def execute(self, sql, params=None):
        return await _CurDespertador(self).execute(sql, params)

    def transaction(self):
        return _Transaccion(self)


class _BotMudo:
    async def send_message(self, *a, **k):
        raise AssertionError("no debería mandar ningún aviso: la consulta "
                             "no devuelve filas en esta prueba")


def test_el_despertador_excluye_lo_que_espera_en_su_propia_consulta():
    from cerebro import despertador

    conn = _ConnDespertador()
    guardado = _instalar(conn)
    try:
        _correr(despertador.revisar(_BotMudo()))
    finally:
        _restaurar(guardado)
    grandes = [s for s in conn.sql if s.startswith("SELECT 'tareas' AS tabla")]
    assert grandes, "no se ejecutó la consulta grande de avisos"
    sql = grandes[0]
    assert "NOT EXISTS" in sql and "t.primero_id" in sql, (
        "la consulta del despertador tiene que excluir explícitamente lo "
        "que sigue esperando a otra tarea")
    assert "ant.estado = 'pendiente'" in sql
    assert "ant.borrado_en IS NULL" in sql


def test_el_despertador_tolera_la_columna_primero_id_ausente():
    """Si `primero_id` no existe todavía (SQLSTATE 42703), `revisar` cae a
    la consulta de ANTES de este encargo -- sin el NOT EXISTS -- y sigue
    avisando en vez de reventar."""
    from cerebro import despertador

    conn = _ConnDespertador(sin_columna_primero=True)
    guardado = _instalar(conn)
    try:
        avisos = _correr(despertador.revisar(_BotMudo()))
    finally:
        _restaurar(guardado)
    assert avisos == 0, "sin filas que avisar, tiene que devolver 0 y no reventar"
    grandes = [s for s in conn.sql if s.startswith("SELECT 'tareas' AS tabla")]
    # DOS intentos: el que trae `t.primero_id` (falla) y el de antes (entra).
    assert len(grandes) == 2, (
        f"tenía que intentar con primero_id (y fallar) y caer a la consulta "
        f"vieja una vez: {grandes}")
    assert "primero_id" not in grandes[1]


def test_el_briefing_le_dice_a_lucy_que_no_proponga_lo_que_espera():
    """El encargo del briefing matinal (texto que Lucy recibe como [sistema]
    y desarrolla ella misma) tiene que mencionar `primero_id` explícitamente
    -- «dale la información, no le pongas una guarda» es la decisión del
    diseño para Telegram: la única forma de que Lucy no proponga una tarea
    que espera es que sepa que existe ese concepto."""
    import inspect

    from cerebro import despertador
    fuente = inspect.getsource(despertador._briefing)
    assert "primero_id" in fuente, (
        "el encargo del briefing no menciona primero_id: Lucy no tiene "
        "cómo saber que no debe proponer una tarea que todavía espera")


# ═══════════════════════════════════════════════════════════════════════
# 6) El panel: la plantilla de verdad trae la fila gris y el selector.
#    Se lee el ARCHIVO, como ya hace `tests/test_panel_tareas.py::
#    test_un_solo_formulario_para_toda_la_pantalla` -- no hay un motor de
#    plantillas Jinja instalado para renderizar de verdad en este entorno
#    hermético, así que lo que se puede comprobar sin un servidor es que el
#    marcado que la lógica necesita está en el archivo que Jinja va a leer.
# ═══════════════════════════════════════════════════════════════════════

def test_la_lista_pinta_gris_y_la_flecha_hacia_la_tarea_que_espera():
    """Ojo con medir la PALABRA `fila-espera` suelta: aparece también en un
    comentario de la plantilla, así que una mutación que saque la clase del
    `<td>` de verdad y deje el comentario intacto seguiría en verde con esa
    comprobación (medido mutando -- ver `m7_panel.py` en el scratchpad del
    testigo/constructor). Por eso se exige la forma EXACTA en que Jinja la
    aplica -- `class="fila-espera"`, entre comillas, como queda escrita en
    el `<td>` -- no la palabra sola."""
    from pathlib import Path
    html = Path(_ROOT, "web", "plantillas", "tareas.html").read_text(encoding="utf-8")
    assert "primero_esperando" in html, (
        "la plantilla no consulta primero_esperando: no hay cómo decidir "
        "si pinta gris")
    assert 'class="fila-espera"' in html, (
        "no se aplica la clase que la pinta gris -- se buscó la forma "
        "exacta, no la palabra suelta, que también aparece en un comentario")
    assert "→ Primero:" in html, "no se pinta la flecha con el texto"
    css = Path(_ROOT, "web", "plantillas", "base.html").read_text(encoding="utf-8")
    assert ".fila-espera" in css, "la clase no tiene estilo definido"


def test_la_pagina_de_la_tarea_tiene_el_selector_de_primero():
    from pathlib import Path
    html = Path(_ROOT, "web", "plantillas", "tarea_detalle.html").read_text(
        encoding="utf-8")
    assert 'name="primero_id"' in html, "no hay <select> para elegir «Primero:»"
    assert '/tareas/{{ tarea.id }}/primero' in html, (
        "el <select> no postea a la ruta que lo guarda")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
