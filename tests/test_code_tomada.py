# -*- coding: utf-8 -*-
"""«Tomada»: la sala marca que EMPEZÓ una tarea técnica (26-sep-2026, diseño
aprobado por Tiziano: disenos/lucy-code/DISENO.md, §D — parte 3 del plan de
construcción).

Parte 3, y SOLO esa: `tareas.tomada_en`, `db.tomar_tarea_de_la_sala` con su
guarda embebida en el SQL, `POST /api/code/tareas/{id}/tomar` con su propio
permiso `tareas:tomar`, que `db.tareas_de_code_pendientes` diga si está
tomada, y el «🔧 en curso desde…» del panel humano (`db.tareas_por_grupo`,
`db.tarea_con_comentarios`). El aviso a Tiziano por no tomarla a tiempo es
de otra parte (B/alarma) y no se prueba acá.

NINGÚN chat_id ni clave de este archivo es real (regla del repo: es
PÚBLICO).

Herméticos: sin Postgres ni red — mismos stubs que el resto de la suite.

Correr:  python3 -m pytest tests/test_code_tomada.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sys
import textwrap
import types
from datetime import datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-tomada")
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

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.api_code as api_code  # noqa: E402
import web.auth as auth  # noqa: E402
import acciones.crud as crud  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

UTC = timezone.utc


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ═══════════════════════════════════════════════════════════════════════
# §D — La consulta REAL de `tomar_tarea_de_la_sala`, corrida contra SQLite
# (mismo patrón que `tests/test_code_responsable.py`, parte 1)
# ═══════════════════════════════════════════════════════════════════════

def _extraer_consulta_elegible() -> str:
    arbol = ast.parse(
        textwrap.dedent(inspect.getsource(db.tomar_tarea_de_la_sala)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Assign) and len(nodo.targets) == 1
                and isinstance(nodo.targets[0], ast.Name)
                and nodo.targets[0].id == "consulta_elegible"
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)):
            return nodo.value.value
    raise AssertionError("no se encontró 'consulta_elegible' en tomar_tarea_de_la_sala")


def _sqlite_con_tareas(filas: list[dict]):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, titulo TEXT, estado TEXT, vence_en TEXT,
          bandeja_id INTEGER, responsable_chat_id INTEGER, area TEXT,
          proyecto_id INTEGER, borrado_en TEXT, tomada_en TEXT
        )""")
    conn.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY, area TEXT)")
    conn.execute(
        "INSERT INTO proyectos (id, area) VALUES (901, ?)", (db.AREA_TECNICA,))
    for f in filas:
        conn.execute(
            "INSERT INTO tareas (id, titulo, estado, vence_en, bandeja_id, "
            "responsable_chat_id, area, proyecto_id, borrado_en, tomada_en) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (f["id"], f.get("titulo", f"tarea {f['id']}"), f["estado"],
             f.get("bandeja_id", 900 + f["id"]), f.get("responsable_chat_id"),
             f.get("area"), f.get("proyecto_id"), f.get("borrado_en"),
             f.get("tomada_en")))
    conn.commit()
    return conn


def _corre_consulta_elegible(conn, tarea_id: int) -> dict | None:
    sql = _extraer_consulta_elegible().replace("%s", "?")
    cur = conn.execute(
        sql, (tarea_id, db.ESTADO_PENDIENTE, db.CHAT_ID_CODE, db.AREA_TECNICA))
    fila = cur.fetchone()
    return dict(fila) if fila else None


def test_la_consulta_elegible_trae_una_tecnica_de_code_pendiente_no_tomada():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is not None


def test_la_consulta_elegible_NO_trae_una_tarea_que_no_es_de_code():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: tomar una tarea que no es de Code no
    toca nada -- corrida contra SQL real."""
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": None,
         "area": db.AREA_TECNICA, "tomada_en": None},
        {"id": 2, "estado": "pendiente", "responsable_chat_id": 700300002,
         "area": db.AREA_TECNICA, "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is None
    assert _corre_consulta_elegible(conn, 2) is None


def test_la_consulta_elegible_NO_trae_una_ya_tomada():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: tomar dos veces no pisa la hora de
    la primera -- la segunda ni siquiera ENCUENTRA la fila."""
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "tomada_en": "2026-09-26T10:00:00+00:00"}])
    assert _corre_consulta_elegible(conn, 1) is None


def test_la_consulta_elegible_NO_trae_una_ya_hecha_ni_pospuesta():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "hecha", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "tomada_en": None},
        {"id": 2, "estado": "pospuesta", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is None
    assert _corre_consulta_elegible(conn, 2) is None


def test_la_consulta_elegible_NO_trae_de_otra_area():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": "CDS", "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is None


def test_la_consulta_elegible_hereda_el_area_del_proyecto():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": None, "proyecto_id": 901, "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is not None


def test_la_consulta_elegible_NO_trae_una_borrada():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "borrado_en": "2026-09-01", "tomada_en": None}])
    assert _corre_consulta_elegible(conn, 1) is None


# ═══════════════════════════════════════════════════════════════════════
# `db.tomar_tarea_de_la_sala`: comportamiento completo (guarda + escritura +
# rastro + tolerancia a la columna ausente)
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


class _CurTomar:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if self._conn.sin_columna_tomada and "tomada_en" in s:
            raise _ErrorSQL("42703")
        if "SELECT t.id, t.titulo, t.estado" in s:
            self._filas = [self._conn.elegible] if self._conn.elegible else []
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnTomar:
    def __init__(self, elegible=None, sin_columna_tomada=False):
        self.elegible = elegible
        self.sin_columna_tomada = sin_columna_tomada
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CurTomar(self)

    def transaction(self):
        return _Transaccion(self)

    async def execute(self, sql, params=None):
        return await _CurTomar(self).execute(sql, params)


class _PoolTomar:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _con_pool(elegible=None, sin_columna_tomada=False):
    return _PoolTomar(_ConnTomar(elegible=elegible, sin_columna_tomada=sin_columna_tomada))


def test_tomar_no_toca_nada_si_no_es_elegible():
    guardado = db.pool
    db.pool = _con_pool(elegible=None)
    try:
        ok = _correr(db.tomar_tarea_de_la_sala(999))
    finally:
        db.pool = guardado
    assert ok is False


def test_tomar_marca_la_hora_y_deja_rastro_de_sala():
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE, "tomada_en": None,
            "area_efectiva": db.AREA_TECNICA}
    guardado = db.pool
    db.pool = _con_pool(elegible=fila)
    try:
        ok = _correr(db.tomar_tarea_de_la_sala(5))
    finally:
        conn = db.pool._conn
        db.pool = guardado
    assert ok is True
    updates = [(s, p) for s, p in conn.sql if s.upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert "TOMADA_EN = NOW()" in updates[0][0].upper()
    assert updates[0][1] == (5,)
    inserts = [(s, p) for s, p in conn.sql
              if s.upper().startswith("INSERT INTO LOG_ACCIONES")]
    assert len(inserts) == 1
    sql_log, _ = inserts[0]
    assert "'sala'" in sql_log
    assert "tomada" in sql_log.lower()


def test_sin_la_columna_tomar_no_revienta_devuelve_none_y_lo_registra(caplog):
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: sin la columna, nada revienta. Se
    comprueba que la función devuelve `None` (no lanza, no da 500) y que
    queda un registro claro."""
    import logging
    guardado = db.pool
    db.pool = _con_pool(elegible=None, sin_columna_tomada=True)
    try:
        with caplog.at_level(logging.WARNING, logger="lucy.db"):
            ok = _correr(db.tomar_tarea_de_la_sala(5))
    finally:
        db.pool = guardado
    assert ok is None
    assert any("tomada_en" in rec.message or "migracion" in rec.message.lower()
              for rec in caplog.records), (
        "no quedó ningún registro de que faltaba la migración")


# ═══════════════════════════════════════════════════════════════════════
# Hermanos: `tomadas_de` tolera la columna ausente, y quien la usa también
# ═══════════════════════════════════════════════════════════════════════

class _CurLote:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if self._conn.sin_columna_tomada and "tomada_en" in s:
            raise _ErrorSQL("42703")
        self._filas = list(self._conn.filas)
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _ConnLote:
    def __init__(self, filas=None, sin_columna_tomada=False):
        self.filas = filas or []
        self.sin_columna_tomada = sin_columna_tomada

    def cursor(self, row_factory=None):
        return _CurLote(self)


class _PoolLote:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def test_tomadas_de_vacia_no_pregunta_nada():
    guardado = db.pool
    db.pool = _PoolLote(_ConnLote())  # si esto se usara, reventaría
    try:
        salida = _correr(db.tomadas_de([]))
    finally:
        db.pool = guardado
    assert salida == {}


def test_tomadas_de_trae_lo_que_hay():
    guardado = db.pool
    db.pool = _PoolLote(_ConnLote(filas=[{"id": 7, "tomada_en": "2026-09-26T09:00:00"}]))
    try:
        salida = _correr(db.tomadas_de([7, 8]))
    finally:
        db.pool = guardado
    assert salida == {7: "2026-09-26T09:00:00"}


def test_tomadas_de_sin_la_columna_devuelve_vacio_no_revienta():
    """GARANTÍA: sin la columna, nada revienta -- ésta es la que usan
    `tareas_por_grupo` y `tarea_con_comentarios` (los HERMANOS que pintan
    el panel humano)."""
    guardado = db.pool
    db.pool = _PoolLote(_ConnLote(sin_columna_tomada=True))
    try:
        salida = _correr(db.tomadas_de([7]))
    finally:
        db.pool = guardado
    assert salida == {}


def test_tareas_de_code_pendientes_incluye_tomada_en():
    fuente = inspect.getsource(db.tareas_de_code_pendientes)
    assert "t.tomada_en" in fuente, (
        "la consulta con la columna nueva no está en tareas_de_code_pendientes")
    assert "42703" in fuente, (
        "tareas_de_code_pendientes no tolera la migración sin aplicar")


def test_tareas_por_grupo_y_tarea_con_comentarios_usan_tomadas_de():
    """Censo de hermanos: TODO sitio que pinta una lista o el detalle de
    tareas para humanos tiene que preguntar por `tomada_en` -- ninguno lo
    hace con SQL propio, todos vía `db.tomadas_de` (la única puerta, mismo
    principio que `puede_ser_responsable`)."""
    fuente_grupo = inspect.getsource(db.tareas_por_grupo)
    fuente_detalle = inspect.getsource(db.tarea_con_comentarios)
    assert "tomadas_de(" in fuente_grupo, (
        "tareas_por_grupo no pinta 'tomada_en' -- el panel no diría 'en curso'")
    assert "tomadas_de(" in fuente_detalle, (
        "tarea_con_comentarios no pinta 'tomada_en'")


# ═══════════════════════════════════════════════════════════════════════
# El panel humano: la etiqueta «🔧 en curso desde…»
# ═══════════════════════════════════════════════════════════════════════

DUENO = config.CHAT_ID_DUENO


def _pedir(ruta="/tareas"):
    from starlette.requests import Request
    token = auth.crear_token(DUENO, auth.VIDA_SESION)
    cabeceras = [(b"host", b"t"), (b"cookie", f"{panel.COOKIE}={token}".encode())]
    return Request({"type": "http", "http_version": "1.1", "method": "GET",
                    "scheme": "https", "server": ("t", 443), "path": ruta,
                    "root_path": "", "query_string": b"",
                    "headers": cabeceras, "app": panel.app})


class _CurPanel:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT t.id, t.titulo, t.estado, t.vence_en, t.creado_en"):
            self._filas = list(self._conn.tareas)
        elif s.startswith("SELECT id, tomada_en FROM tareas"):
            # Mismo filtro que la SQL real (26-sep-2026, hallazgo del
            # testigo sobre 3b1b6dc): solo cuenta si TODAVÍA es de Code --
            # una tarea reasignada a otra persona no sale acá aunque
            # `tomada_en` le haya quedado puesto.
            self._filas = [
                {"id": f["id"], "tomada_en": f["tomada_en"]}
                for f in self._conn.tareas
                if f.get("tomada_en")
                and f.get("responsable_chat_id") == config.CHAT_ID_CODE]
        else:
            self._filas = []
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _TransaccionPanel:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _ConnPanel:
    def __init__(self, tareas):
        self.tareas = tareas

    def cursor(self, row_factory=None):
        return _CurPanel(self)

    def transaction(self):
        return _TransaccionPanel(self)

    async def execute(self, sql, params=None):
        return await _CurPanel(self).execute(sql, params)


class _PoolPanel:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _fila_panel(id_, titulo, tomada_en=None, responsable=None):
    return {"id": id_, "titulo": titulo, "estado": "pendiente", "vence_en": None,
            "creado_en": datetime(2026, 9, 26, tzinfo=UTC), "bandeja_id": 900 + id_,
            "responsable_chat_id": responsable, "completado_en": None,
            "area": None, "proyecto_id": None, "proyecto_nombre": None,
            "primero_id": None, "primero_titulo": None, "primero_estado": None,
            "tomada_en": tomada_en}


class _CurDetalle:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT t.id, t.titulo, t.detalle, t.estado, t.vence_en"):
            self._filas = [dict(self._conn.tarea)]
        elif s.startswith("SELECT id, tarea_id, autor_chat_id"):
            self._filas = []
        elif s.startswith("SELECT id, texto, hecho, orden FROM micro_pasos"):
            self._filas = []
        elif s.startswith("SELECT id, tomada_en FROM tareas"):
            t = self._conn.tarea
            self._filas = (
                [{"id": t["id"], "tomada_en": t["tomada_en"]}]
                if t.get("tomada_en")
                and t.get("responsable_chat_id") == config.CHAT_ID_CODE
                else [])
        else:
            self._filas = []
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _ConnDetalle:
    def __init__(self, tarea):
        self.tarea = tarea

    def cursor(self, row_factory=None):
        return _CurDetalle(self)

    def transaction(self):
        return _TransaccionPanel(self)

    async def execute(self, sql, params=None):
        return await _CurDetalle(self).execute(sql, params)


class _PoolDetalle:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def test_el_panel_pinta_en_curso_cuando_hay_tomada_en():
    guardado = db.pool
    db.pool = _PoolPanel(_ConnPanel([
        _fila_panel(1, "Arreglar el canario",
                    tomada_en=datetime(2026, 9, 26, 9, 0, tzinfo=UTC),
                    responsable=config.CHAT_ID_CODE)]))
    try:
        r = _correr(panel.tareas(_pedir()))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    html = r.body.decode()
    assert "en curso desde" in html, "el panel no pintó la etiqueta de 'tomada'"


def test_el_panel_no_pinta_en_curso_sin_tomada_en():
    guardado = db.pool
    db.pool = _PoolPanel(_ConnPanel([
        _fila_panel(1, "Todavía sin empezar", tomada_en=None,
                    responsable=config.CHAT_ID_CODE)]))
    try:
        r = _correr(panel.tareas(_pedir()))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    assert "en curso desde" not in r.body.decode()


def test_el_panel_no_pinta_en_curso_en_una_tarea_ya_hecha_y_reciente():
    """Hallazgo del testigo sobre `3b1b6dc`: `cerrar_tarea_de_la_sala`
    nunca borra `tomada_en` -- queda escrito para siempre. Una tarea
    cerrada HOY cae en el grupo 'otros' (`db.grupo_de_tarea`, DIAS_
    HISTORIAL días antes de irse a 'historial'), que se pinta en /tareas
    con el MISMO bloque de fila que 'en curso'. Si el guardia
    `t.estado == pendiente` de `web/plantillas/tareas.html` se rompiera,
    esto se vería "en curso" estando ya hecha."""
    guardado = db.pool
    ahora = datetime.now(UTC)
    fila = _fila_panel(1, "Ya la cerré",
                       tomada_en=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
                       responsable=config.CHAT_ID_CODE)
    fila["estado"] = "hecha"
    fila["completado_en"] = ahora  # cerrada AHORA MISMO: cae en 'otros'
    db.pool = _PoolPanel(_ConnPanel([fila]))
    try:
        r = _correr(panel.tareas(_pedir()))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    html = r.body.decode()
    assert "Ya la cerré" in html, (
        "la tarea no aparece en /tareas -- revisar el fixture, no la garantía")
    assert "en curso desde" not in html, (
        "¡una tarea YA HECHA se pintó como 'en curso'!")


def test_tarea_detalle_no_pinta_en_curso_en_una_tarea_ya_hecha_y_reciente():
    """Misma garantía que arriba, para `/tareas/{id}` (`tarea_detalle.html`,
    que tiene su PROPIO guardia `tarea.estado == pendiente`)."""
    guardado = db.pool
    fila = _fila_panel(1, "Ya la cerré",
                       tomada_en=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
                       responsable=config.CHAT_ID_CODE)
    fila["estado"] = "hecha"
    db.pool = _PoolDetalle(_ConnDetalle(fila))
    try:
        r = _correr(panel.tarea_detalle(_pedir("/tareas/1"), 1))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    html = r.body.decode()
    assert "Ya la cerré" in html
    assert "en curso desde" not in html, (
        "¡el detalle de una tarea YA HECHA se pintó como 'en curso'!")


def test_el_panel_no_pinta_en_curso_si_la_tarea_se_reasigno():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (hallazgo 3 del testigo sobre
    `3b1b6dc`): `asignar_responsable` no borra `tomada_en` al reasignar --
    `db.tomadas_de` (§D) hace que el caso NO EXISTA filtrando por
    `responsable_chat_id = CHAT_ID_CODE`: una tarea reasignada a un humano
    no sale "en curso" aunque `tomada_en` le haya quedado puesto."""
    guardado = db.pool
    OTRA_PERSONA = 700400001
    db.pool = _PoolPanel(_ConnPanel([
        _fila_panel(1, "Reasignada a Rosi",
                    tomada_en=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
                    responsable=OTRA_PERSONA)]))
    try:
        r = _correr(panel.tareas(_pedir()))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    html = r.body.decode()
    assert "Reasignada a Rosi" in html
    assert "en curso desde" not in html, (
        "¡una tarea reasignada a otra persona se pintó como 'en curso'!")


# ═══════════════════════════════════════════════════════════════════════
# La ruta HTTP: `POST /api/code/tareas/{id}/tomar`
# ═══════════════════════════════════════════════════════════════════════

CLIENTE = TestClient(panel.app)
SALA_CLAVE = "clave-de-prueba-sala-tomada-no-es-real-11111"


def _con_clave_sala():
    claves_orig = config.CLAVES_API_CODE
    permisos_orig = config.PERMISOS_API_CODE
    config.CLAVES_API_CODE = {SALA_CLAVE: "sala_mac"}
    config.PERMISOS_API_CODE = {
        "sala_mac": frozenset({"tareas:listar", "tareas:cerrar", "tareas:tomar"})}

    def _restaurar():
        config.CLAVES_API_CODE = claves_orig
        config.PERMISOS_API_CODE = permisos_orig
    return _restaurar


def _limpiar_contadores():
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0


def test_ruta_tomar_pide_especificamente_tareas_tomar():
    """`tareas:listar` + `tareas:cerrar`, SIN `tareas:tomar`, tiene que
    recibir 403 en /tomar -- la pareja exacta que ya cubre, en general,
    `tests/test_api_code.py::test_cada_ruta_exige_especificamente_su_
    propio_permiso` (que deriva las rutas de `rutas_registradas` y ahora
    ve también ÉSTA sola). Acá se repite explícito para esta ruta."""
    claves_orig = config.CLAVES_API_CODE
    permisos_orig = config.PERMISOS_API_CODE
    clave = "clave-de-prueba-sin-tomar-22222"
    config.CLAVES_API_CODE = {clave: "quien"}
    config.PERMISOS_API_CODE = {
        "quien": frozenset({"tareas:listar", "tareas:cerrar"})}
    _limpiar_contadores()
    try:
        r = CLIENTE.post("/api/code/tareas/1/tomar",
                         headers={"Authorization": f"Bearer {clave}"})
        assert r.status_code == 403
    finally:
        config.CLAVES_API_CODE = claves_orig
        config.PERMISOS_API_CODE = permisos_orig


def test_ruta_tomar_ok():
    restaurar = _con_clave_sala()
    _limpiar_contadores()
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE, "tomada_en": None,
            "area_efectiva": db.AREA_TECNICA}
    guardado = db.pool
    db.pool = _con_pool(elegible=fila)
    try:
        r = CLIENTE.post("/api/code/tareas/5/tomar",
                         headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 200
        assert r.json() == {"tomada": True}
    finally:
        db.pool = guardado
        restaurar()


def test_ruta_tomar_una_que_no_es_de_code_da_409_y_no_toca_nada():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE, de punta a punta: tomar una tarea
    que no es de Code -> no se toca, por la puerta."""
    restaurar = _con_clave_sala()
    _limpiar_contadores()
    guardado = db.pool
    db.pool = _con_pool(elegible=None)
    try:
        r = CLIENTE.post("/api/code/tareas/999/tomar",
                         headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 409
        conn = db.pool._conn
        escribio = any(
            s.upper().startswith("UPDATE") or s.upper().startswith("INSERT")
            for s, _ in conn.sql)
        assert not escribio
    finally:
        db.pool = guardado
        restaurar()


def test_ruta_tomar_sin_la_columna_da_503_no_500():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: sin la columna, nada revienta -- de
    punta a punta, la ruta HTTP contesta un error CLARO (503), nunca un 500
    mudo."""
    restaurar = _con_clave_sala()
    _limpiar_contadores()
    guardado = db.pool
    db.pool = _con_pool(elegible=None, sin_columna_tomada=True)
    try:
        r = CLIENTE.post("/api/code/tareas/5/tomar",
                         headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 503
        assert "migra" in r.json()["detail"].lower()
    finally:
        db.pool = guardado
        restaurar()


def test_listar_sin_la_columna_no_revienta():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE, del lado de la LECTURA: sin la
    migración, `GET /tareas` sigue sirviendo -- con `tomada_en: None` en
    todas, no con un 500."""
    restaurar = _con_clave_sala()
    _limpiar_contadores()

    class _ConnListarSinTomada:
        def __init__(self):
            self.sql = []

        def cursor(self, row_factory=None):
            return self

        def transaction(self):
            return _Transaccion(self)

        async def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self.sql.append(s)
            if "tomada_en" in s:
                raise _ErrorSQL("42703")
            return self

        async def fetchall(self):
            return [{"id": 5, "titulo": "Arreglar el canario"}]

        async def fetchone(self):
            return None

    conn = _ConnListarSinTomada()
    guardado = db.pool
    db.pool = _PoolTomar(conn)
    try:
        r = CLIENTE.get("/api/code/tareas",
                        headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 200
        assert r.json() == {"tareas": [
            {"id": 5, "titulo": "Arreglar el canario", "tomada_en": None}]}
    finally:
        db.pool = guardado
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# GARANTÍA PEDIDA EXPLÍCITAMENTE (hallazgo 2 del testigo sobre `3b1b6dc`):
# `deshacer()` no revienta sobre una huella de `actor='sala'` de cerrar o de
# tomar -- el `antes` que esas dos funciones guardan tiene SOLO columnas
# reales de `tareas`, nunca un alias de JOIN (`area_efectiva`).
# ═══════════════════════════════════════════════════════════════════════

def _capturar_antes_guardado(coro_factory, elegible):
    """Corre la función REAL (`cerrar_tarea_de_la_sala`/`tomar_tarea_de_la_
    sala`) contra una conexión de mentira, y devuelve el `dict` exacto que
    esa función pasó a `json.dumps(...)` para `log_acciones.antes` -- no lo
    que la propia consulta trajo, sino lo que sobrevivió al filtro. Así la
    prueba de `deshacer()` corre sobre el dato REAL que hoy se escribe, no
    sobre uno armado a mano en la prueba."""
    conn = _ConnTomar(elegible=elegible)
    guardado = db.pool
    db.pool = _PoolTomar(conn)
    try:
        ok = _correr(coro_factory())
    finally:
        db.pool = guardado
    assert ok is True
    inserts = [(s, p) for s, p in conn.sql
              if s.upper().startswith("INSERT INTO LOG_ACCIONES")]
    assert len(inserts) == 1
    _, params = inserts[0]
    antes_json = params[1]
    return json.loads(antes_json)


def test_el_antes_que_guarda_cerrar_tiene_solo_columnas_reales_de_tareas():
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "completado_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE,
            "area_efectiva": db.AREA_TECNICA}
    antes = _capturar_antes_guardado(
        lambda: db.cerrar_tarea_de_la_sala(5), fila)
    columnas_reales = set(db.columnas_declaradas()["tareas"])
    sobrantes = set(antes) - columnas_reales
    assert not sobrantes, (
        f"log_acciones.antes de cerrar_tarea_de_la_sala trae columnas que "
        f"'tareas' no tiene: {sobrantes} -- deshacer() reventaría")
    assert "area_efectiva" not in antes


def test_el_antes_que_guarda_tomar_tiene_solo_columnas_reales_de_tareas():
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE, "tomada_en": None,
            "area_efectiva": db.AREA_TECNICA}
    antes = _capturar_antes_guardado(
        lambda: db.tomar_tarea_de_la_sala(5), fila)
    columnas_reales = set(db.columnas_declaradas()["tareas"])
    sobrantes = set(antes) - columnas_reales
    assert not sobrantes, (
        f"log_acciones.antes de tomar_tarea_de_la_sala trae columnas que "
        f"'tareas' no tiene: {sobrantes} -- deshacer() reventaría")
    assert "area_efectiva" not in antes


class _CurDeshacer:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if s.startswith("SELECT accion, tabla, registro_id, antes, despues"):
            self._filas = [self._conn.huella]
        elif s.upper().startswith("INSERT INTO LOG_ACCIONES"):
            self._filas = [(999,)]
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnDeshacer:
    def __init__(self, huella):
        self.huella = huella
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CurDeshacer(self)

    async def execute(self, sql, params=None):
        return await _CurDeshacer(self).execute(sql, params)


class _PoolDeshacer:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _deshacer_sobre_huella(antes: dict, despues: dict):
    """Corre `crud.deshacer()` DE VERDAD (no se llama a mano al SQL que
    generaría) sobre una huella `actor='sala', tabla='tareas', accion=
    'editar'` con el `antes`/`despues` dados. Devuelve el texto del UPDATE
    que `deshacer()` de verdad ejecutó."""
    huella = {"accion": "editar", "tabla": "tareas", "registro_id": 5,
             "antes": antes, "despues": despues}
    conn = _ConnDeshacer(huella)
    guardado = db.pool
    db.pool = _PoolDeshacer(conn)
    try:
        que = _correr(crud.deshacer(1))
    finally:
        db.pool = guardado
    updates = [s for s, _ in conn.sql if s.upper().startswith("UPDATE TAREAS")]
    assert len(updates) == 1, f"deshacer() no generó un UPDATE: {conn.sql}"
    return que, updates[0]


def test_deshacer_sobre_una_huella_de_cerrar_no_revienta():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: `deshacer()` real, sobre la huella
    real que `cerrar_tarea_de_la_sala` deja (ya filtrada), no intenta tocar
    `area_efectiva` -- que no es una columna de `tareas` y reventaría contra
    Postgres de verdad (`column "area_efectiva" does not exist`)."""
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "completado_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE,
            "area_efectiva": db.AREA_TECNICA}
    antes = _capturar_antes_guardado(lambda: db.cerrar_tarea_de_la_sala(5), fila)
    que, sql_update = _deshacer_sobre_huella(antes, {"estado": db.ESTADO_HECHA})
    assert "area_efectiva" not in sql_update.lower()
    assert que == "el cambio"


def test_deshacer_sobre_una_huella_de_tomar_no_revienta():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: misma garantía, para la huella de
    `tomar_tarea_de_la_sala`."""
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE, "tomada_en": None,
            "area_efectiva": db.AREA_TECNICA}
    antes = _capturar_antes_guardado(lambda: db.tomar_tarea_de_la_sala(5), fila)
    que, sql_update = _deshacer_sobre_huella(antes, {"tomada_en": "now"})
    assert "area_efectiva" not in sql_update.lower()
    assert que == "el cambio"


def test_solo_columnas_reales_filtra_por_tabla_de_verdad():
    """Unidad de `db._solo_columnas_reales`: usa `columnas_declaradas()`,
    no una lista tecleada -- se prueba con una tabla real (`tareas`) y una
    fila con una clave inventada."""
    fila = {"id": 1, "titulo": "x", "area_efectiva": "🛠️ Técnico",
            "un_alias_cualquiera": 42}
    filtrada = db._solo_columnas_reales("tareas", fila)
    assert filtrada == {"id": 1, "titulo": "x"}


# ═══════════════════════════════════════════════════════════════════════
# `db.tomadas_de`: el SQL REAL, corrido contra SQLite (no el doble que
# duplica el filtro en Python de las pruebas de arriba) -- para que una
# mutación del WHERE de verdad se note.
# ═══════════════════════════════════════════════════════════════════════

def _extraer_sql_tomadas_de() -> str:
    arbol = ast.parse(textwrap.dedent(inspect.getsource(db.tomadas_de)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "execute"
                and nodo.args
                and isinstance(nodo.args[0], ast.Constant)
                and isinstance(nodo.args[0].value, str)
                and "tomada_en" in nodo.args[0].value):
            return nodo.args[0].value
    raise AssertionError("no encontré el SELECT de tomadas_de en el código")


def _corre_tomadas_de_sqlite(filas: list[dict], id_pedido: int):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tareas (id INTEGER PRIMARY KEY, tomada_en TEXT, "
        "responsable_chat_id INTEGER)")
    for f in filas:
        conn.execute(
            "INSERT INTO tareas (id, tomada_en, responsable_chat_id) "
            "VALUES (?, ?, ?)",
            (f["id"], f.get("tomada_en"), f.get("responsable_chat_id")))
    conn.commit()

    # Traducción MÍNIMA para que corra en SQLite: `id = ANY(%s)` -> `id = ?`
    # (esta prueba pide un solo id a la vez) y `%s` -> `?`. El resto del
    # texto -- lo que de verdad importa, `AND responsable_chat_id = ?` -- es
    # EL MISMO que ejecuta producción, sin tocar.
    sql = _extraer_sql_tomadas_de()
    sql = sql.replace("id = ANY(%s)", "id = ?").replace("%s", "?")
    cur = conn.execute(sql, (id_pedido, db.CHAT_ID_CODE))
    salida = {row["id"]: row["tomada_en"] for row in cur.fetchall()}
    conn.close()
    return salida


def test_tomadas_de_sql_real_trae_la_que_sigue_siendo_de_code():
    salida = _corre_tomadas_de_sqlite(
        [{"id": 1, "tomada_en": "2026-09-20T09:00:00", "responsable_chat_id": db.CHAT_ID_CODE}],
        id_pedido=1)
    assert salida == {1: "2026-09-20T09:00:00"}


def test_tomadas_de_sql_real_NO_trae_una_reasignada():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (hallazgo 3), corrida contra SQL de
    verdad: una tarea con `tomada_en` puesto pero YA REASIGNADA a otra
    persona no sale."""
    OTRA_PERSONA = 700400002
    salida = _corre_tomadas_de_sqlite(
        [{"id": 1, "tomada_en": "2026-09-20T09:00:00",
          "responsable_chat_id": OTRA_PERSONA}],
        id_pedido=1)
    assert salida == {}
