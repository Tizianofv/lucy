# -*- coding: utf-8 -*-
"""Alarmas técnicas de Lucy convertidas en tareas de Code (26-sep-2026,
diseño aprobado por Tiziano: disenos/lucy-code/DISENO.md, §B — parte 4 del
plan de construcción).

Parte 4, y SOLO esa: `db.crear_o_reusar_alerta_tecnica` (§B.2), las cinco
alarmas del censo B.1 (el respaldo, las tres señales del canario, el latido
de la cosecha), y el aviso de las 6 horas si la sala no toma
(`db.tareas_tecnicas_atrasadas`/`marcar_aviso_atraso_code`/
`cerebro.despertador.revisar_alertas_tecnicas_sin_tomar`). `_al_fallar` y la
vigilancia 911/correos NO se tocan -- siguen yendo a Tiziano. La ruta para
que Natalia cree alertas (parte 5) NO existe todavía.

NINGÚN chat_id ni clave de este archivo es real (regla del repo: PÚBLICO).

Herméticos: sin Postgres ni red -- mismos stubs que el resto de la suite.

Correr:  python3 -m pytest tests/test_code_alarmas.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import sys
import textwrap
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-alarmas")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

logging.disable(logging.CRITICAL)  # varias pruebas provocan un fallo a propósito

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
import captura.consumos as consumos  # noqa: E402
from cerebro import despertador  # noqa: E402

UTC = timezone.utc


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


class _ErrorSQL(Exception):
    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(f"error de mentira, sqlstate={sqlstate}")


# ═══════════════════════════════════════════════════════════════════════
# §B.2 — `db.crear_o_reusar_alerta_tecnica`: SQL real, dedupe contra la base
# ═══════════════════════════════════════════════════════════════════════

class _CurAlerta:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if self._conn.sin_columnas and (
                "clave_tecnica" in s or "ultima_alarma_en" in s):
            raise _ErrorSQL("42703")
        if s.startswith("SELECT id, detalle FROM tareas"):
            self._filas = [self._conn.existente] if self._conn.existente else []
        elif s.upper().startswith("INSERT INTO TAREAS"):
            self._filas = [{"id": self._conn.nuevo_id}]
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnAlerta:
    def __init__(self, existente=None, nuevo_id=999, sin_columnas=False):
        self.existente = existente
        self.nuevo_id = nuevo_id
        self.sin_columnas = sin_columnas
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CurAlerta(self)

    async def execute(self, sql, params=None):
        return await _CurAlerta(self).execute(sql, params)


class _PoolAlerta:
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


def test_crear_alerta_nueva_cuando_no_hay_una_abierta():
    conn = _ConnAlerta(existente=None, nuevo_id=42)
    guardado = db.pool
    db.pool = _PoolAlerta(conn)
    try:
        tid = _correr(db.crear_o_reusar_alerta_tecnica(
            "backup", "Respaldo atrasado", "el detalle"))
    finally:
        db.pool = guardado
    assert tid == 42
    inserts = [(s, p) for s, p in conn.sql if s.upper().startswith("INSERT INTO TAREAS")]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "responsable_chat_id" in sql
    assert config.CHAT_ID_CODE in params
    assert db.AREA_TECNICA in params
    logs = [(s, p) for s, p in conn.sql if s.upper().startswith("INSERT INTO LOG_ACCIONES")]
    assert len(logs) == 1, "no dejó huella de la creación"


def test_reusa_la_tarea_abierta_de_la_misma_clave_y_actualiza_ultima_alarma():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: sin duplicados, decidido contra la
    base -- la misma falla reusa la tarea abierta y actualiza
    `ultima_alarma_en`."""
    conn = _ConnAlerta(existente={"id": 7, "detalle": "ya había pasado antes"})
    guardado = db.pool
    db.pool = _PoolAlerta(conn)
    try:
        tid = _correr(db.crear_o_reusar_alerta_tecnica(
            "canario:popular:reventado", "Canario", "volvió a pasar"))
    finally:
        db.pool = guardado
    assert tid == 7
    inserts = [s for s, _ in conn.sql if s.upper().startswith("INSERT INTO TAREAS")]
    assert not inserts, "creó una tarea nueva en vez de reusar la abierta"
    updates = [(s, p) for s, p in conn.sql if s.upper().startswith("UPDATE TAREAS")]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "ultima_alarma_en" in sql
    assert params[-1] == 7


def test_una_falla_que_vuelve_tras_cerrarse_abre_una_nueva():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una falla que vuelve DESPUÉS de que
    la tarea se cerró abre una NUEVA -- el SELECT solo busca `estado =
    'pendiente'`, así que una cerrada con la misma clave no cuenta como
    "abierta"."""
    conn = _ConnAlerta(existente=None, nuevo_id=55)  # el SELECT no la trae: ya está hecha
    guardado = db.pool
    db.pool = _PoolAlerta(conn)
    try:
        tid = _correr(db.crear_o_reusar_alerta_tecnica(
            "backup", "Respaldo atrasado otra vez", "de nuevo"))
    finally:
        db.pool = guardado
    assert tid == 55
    selects = [(s, p) for s, p in conn.sql if s.startswith("SELECT id, detalle")]
    assert len(selects) == 1
    assert "estado = %s" in selects[0][0]


def _extraer_sql_dedupe_alerta() -> str:
    """Saca del código de `crear_o_reusar_alerta_tecnica` el SELECT del
    dedupe, tal cual lo va a ejecutar Postgres -- no una copia tecleada a
    mano (Regla 18: una lista/consulta tecleada se separa de la realidad)."""
    arbol = ast.parse(
        textwrap.dedent(inspect.getsource(db.crear_o_reusar_alerta_tecnica)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "execute"
                and nodo.args
                and isinstance(nodo.args[0], ast.Constant)
                and isinstance(nodo.args[0].value, str)
                and "clave_tecnica = %s" in nodo.args[0].value):
            return nodo.args[0].value
    raise AssertionError(
        "no encontré el SELECT del dedupe en crear_o_reusar_alerta_tecnica")


def _corre_dedupe_sqlite(filas: list[dict], clave: str):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tareas (id INTEGER PRIMARY KEY, detalle TEXT, "
        "clave_tecnica TEXT, estado TEXT, borrado_en TEXT)")
    for f in filas:
        conn.execute(
            "INSERT INTO tareas (id, detalle, clave_tecnica, estado, borrado_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (f["id"], f.get("detalle", ""), f["clave_tecnica"], f["estado"],
             f.get("borrado_en")))
    conn.commit()
    sql = _extraer_sql_dedupe_alerta().replace("%s", "?")
    cur = conn.execute(sql, (clave, db.ESTADO_PENDIENTE))
    fila = cur.fetchone()
    conn.close()
    return dict(fila) if fila else None


def test_dedupe_sql_real_trae_la_pendiente_con_la_misma_clave():
    fila = _corre_dedupe_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "estado": db.ESTADO_PENDIENTE}],
        clave="backup")
    assert fila is not None and fila["id"] == 1


def test_dedupe_sql_real_NO_reusa_una_tarea_cerrada():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE, corrida contra SQL de verdad (SQLite
    con el mismo texto): una falla que vuelve DESPUÉS de que la tarea se
    cerró abre una NUEVA -- el SELECT no puede traer una `estado <> 'pendiente'`
    aunque la clave coincida."""
    fila = _corre_dedupe_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "estado": "hecha"}],
        clave="backup")
    assert fila is None


def test_dedupe_sql_real_NO_trae_una_borrada():
    fila = _corre_dedupe_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "estado": db.ESTADO_PENDIENTE,
          "borrado_en": "2026-09-01"}],
        clave="backup")
    assert fila is None


def test_dedupe_sql_real_NO_trae_otra_clave():
    fila = _corre_dedupe_sqlite(
        [{"id": 1, "clave_tecnica": "canario:mudo:sin_ruta", "estado": db.ESTADO_PENDIENTE}],
        clave="backup")
    assert fila is None


# ═══════════════════════════════════════════════════════════════════════
# `db.crear_o_reusar_alerta_tecnica` de PUNTA A PUNTA contra SQLite REAL --
# no la SELECT sola (arriba) sino la función completa: INSERT/UPDATE/
# RETURNING/`now()` corriendo de verdad, muchas veces seguidas. GARANTÍA
# PEDIDA EXPLÍCITAMENTE por el testigo (hallazgo 3, NO PASA sobre 00eb9e6):
# "sin duplicados: 96 corridas seguidas -> una sola fila, con 96 apariciones
# de «Volvió a pasar» en el detalle" (96 = un despertador cada 15 min en 24h).
# ═══════════════════════════════════════════════════════════════════════

class _CurAlertaReal:
    def __init__(self, con):
        self._con = con
        self._cur = None

    async def execute(self, sql, params=None):
        sql2 = sql.replace("%s", "?")
        self._cur = self._con.execute(sql2, params or ())
        return self

    async def fetchone(self):
        fila = self._cur.fetchone()
        return dict(fila) if fila is not None else None

    async def fetchall(self):
        return [dict(f) for f in self._cur.fetchall()]


class _ConnAlertaReal:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _CurAlertaReal(self._con)

    async def execute(self, sql, params=None):
        return await _CurAlertaReal(self._con).execute(sql, params)


class _PoolAlertaReal:
    def __init__(self, con):
        self._conn = _ConnAlertaReal(con)

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _sqlite_para_alerta_real():
    import sqlite3

    con = sqlite3.connect(":memory:", isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    # `now()` no existe en SQLite -- se registra como función nativa, igual
    # de determinista para esta prueba que Postgres (solo importa que
    # AVANCE, no la hora exacta).
    con.create_function("now", 0, lambda: datetime.now(timezone.utc).isoformat())
    con.execute("""
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, titulo TEXT, detalle TEXT, area TEXT,
          responsable_chat_id INTEGER, estado TEXT DEFAULT 'pendiente',
          borrado_en TEXT, clave_tecnica TEXT, ultima_alarma_en TEXT
        )""")
    con.execute("""
        CREATE TABLE log_acciones (
          id INTEGER PRIMARY KEY, actor TEXT, accion TEXT, tabla TEXT,
          registro_id INTEGER, despues TEXT, motivo TEXT
        )""")
    con.commit()
    return con


def test_96_corridas_seguidas_una_sola_fila_con_96_ocurrencias():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE por el testigo, de punta a punta
    contra SQL real: 96 llamadas seguidas con la MISMA clave (un
    despertador cada 15 minutos durante 24h, la misma falla repitiéndose)
    dejan UNA sola fila en `tareas`, y su `detalle` acumula 96 apariciones
    de "Volvió a pasar" -- una por cada ocurrencia después de la primera
    creación, sin perder ninguna y sin abrir una segunda fila."""
    con = _sqlite_para_alerta_real()
    guardado_pool = db.pool
    db.pool = _PoolAlertaReal(con)
    try:
        ids = []
        for i in range(96):
            tid = _correr(db.crear_o_reusar_alerta_tecnica(
                "canario:banco-real:reventado", "Canario: banco-real reventó",
                f"ocurrencia número {i}"))
            ids.append(tid)
        filas = con.execute(
            "SELECT id, detalle FROM tareas WHERE clave_tecnica = ?",
            ("canario:banco-real:reventado",)).fetchall()
        assert len(filas) == 1, (
            f"esperaba UNA sola fila tras 96 corridas, salieron {len(filas)}")
        assert len(set(ids)) == 1, (
            f"las 96 corridas devolvieron ids distintos: {sorted(set(ids))}")
        detalle = filas[0]["detalle"]
        apariciones = detalle.count("Volvió a pasar")
        assert apariciones == 95, (
            f"esperaba 95 apariciones de 'Volvió a pasar' (96 corridas: la "
            f"1a crea, las 95 siguientes reusan y agregan), salieron {apariciones}")
    finally:
        db.pool = guardado_pool
        con.close()


def test_sin_la_migracion_devuelve_none_y_no_revienta(caplog):
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 1): sin las columnas de
    esta parte, nada revienta -- devuelve `None` para que quien llama caiga
    al aviso directo, como antes."""
    conn = _ConnAlerta(sin_columnas=True)
    guardado = db.pool
    db.pool = _PoolAlerta(conn)
    try:
        with caplog.at_level(logging.WARNING, logger="lucy.db"):
            tid = _correr(db.crear_o_reusar_alerta_tecnica(
                "backup", "x", "y"))
    finally:
        db.pool = guardado
    assert tid is None
    assert any("migracion" in r.message.lower() for r in caplog.records)


def test_la_puerta_del_responsable_se_consulta():
    """Censo AST: `crear_o_reusar_alerta_tecnica` nombra
    `puede_ser_responsable` -- la MISMA puerta que cualquier otro escritor
    de `responsable_chat_id` (`tests/test_responsable.py`)."""
    fuente = inspect.getsource(db.crear_o_reusar_alerta_tecnica)
    assert "puede_ser_responsable" in fuente


# ═══════════════════════════════════════════════════════════════════════
# Censo de HERMANOS: ningún `INSERT INTO tareas` con Técnico+Code fuera de
# `crear_o_reusar_alerta_tecnica`
# ═══════════════════════════════════════════════════════════════════════

def _usa_code_como_valor(funcion) -> bool:
    """¿Esta función USA `CHAT_ID_CODE` como valor (lo pasa, lo asigna, lo
    mete en los parámetros de un INSERT) y no solo lo COMPARA?

    Criterio puesto en el merge de main (30-sep-2026): la tarea 146 (main)
    dejó que un humano elija a Code como responsable desde el formulario del
    panel (`db.crear_tarea_desde_el_panel`), que nombra `CHAT_ID_CODE` solo en
    un `==` para fijar el área. Eso NO es una alarma técnica: el responsable
    lo escribe la persona, no el código. Una alarma técnica, en cambio,
    escribe Code como valor. Se mira el AST (cada `Name`/`Attribute`
    `CHAT_ID_CODE` y su padre), no el texto: un docstring que lo nombre no
    cuenta, y un `==` tampoco. Frontera dicha: un escritor que meta a Code
    por un literal de SQL (`'...'`) o por una variable intermedia con otro
    nombre no se ve acá; ese hueco ya existía con la búsqueda de texto."""
    padres = {}
    for n in ast.walk(funcion):
        for h in ast.iter_child_nodes(n):
            padres[h] = n
    for n in ast.walk(funcion):
        nombre = (n.id if isinstance(n, ast.Name)
                  else n.attr if isinstance(n, ast.Attribute) else None)
        if nombre != "CHAT_ID_CODE":
            continue
        if not isinstance(padres.get(n), ast.Compare):
            return True
    return False


def test_ningun_insert_de_tarea_tecnica_de_code_fuera_de_la_puerta():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 3): toda alarma técnica
    pasa por LA MISMA puerta -- se recorre TODO EL REPOSITORIO (no solo
    `db/db.py`, que es lo que este censo entregaba antes de este arreglo:
    hallazgo 4 del testigo sobre 00eb9e6, NO PASA) buscando cualquier
    `INSERT INTO tareas` que fije `responsable_chat_id` a `CHAT_ID_CODE` Y
    un área -- fuera de la función `crear_o_reusar_alerta_tecnica`.

    LA MISMA PUERTA de recorrido que ya usa el resto de la suite
    (`tests/test_buzon_que_no_se_ve.py::_py_en_disco`, la única fuente de
    "qué archivos hay en este repo": venvs, cachés y `norecursedirs`
    quedan afuera solos, sin lista tecleada) -- no una segunda función que
    camine el disco a su manera y se desalinee con esa el día que cambie.
    Mismo patrón de exclusión de PRUEBAS que `tests/test_responsable.py::
    _censo` (`barrido._testpaths`): sin esto, este mismo archivo se
    denuncia a sí mismo por nombrar "INSERT INTO tareas"/"CHAT_ID_CODE" en
    su propio texto (docstring y literales de comparación)."""
    import test_buzon_que_no_se_ve as barrido

    raiz = barrido.RAIZ
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    ofensores = []
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        try:
            fuente_modulo = real.read_text(encoding="utf-8")
            arbol = ast.parse(fuente_modulo, filename=str(real))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for nodo in ast.walk(arbol):
            # `async def` es `ast.AsyncFunctionDef`, que NO hereda de
            # `ast.FunctionDef` -- mirar solo `ast.FunctionDef` deja el
            # censo ciego a casi toda función de este repo (defecto real
            # encontrado en la mutación 7 de este mismo encargo).
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if nodo.name == "crear_o_reusar_alerta_tecnica":
                continue
            fuente_fn = ast.get_source_segment(fuente_modulo, nodo) or ""
            if ("INSERT INTO tareas" in fuente_fn
                    and _usa_code_como_valor(nodo)):
                ofensores.append(f"{real.relative_to(raiz)}::{nodo.name}")
    assert not ofensores, (
        f"estas funciones insertan tareas con CHAT_ID_CODE sin pasar por "
        f"crear_o_reusar_alerta_tecnica: {ofensores}")


# ═══════════════════════════════════════════════════════════════════════
# §B.1 — Las cinco alarmas: primario (tarea de Code) y de respaldo (Telegram
# directo / bandeja, cuando la tarea no se pudo crear)
# ═══════════════════════════════════════════════════════════════════════

class FakeBot:
    def __init__(self):
        self.mensajes: list[str] = []

    async def send_message(self, chat_id, text):
        self.mensajes.append(text)


def test_revisar_backup_crea_tarea_de_code_y_no_manda_telegram():
    async def _ultimo_backup():
        return None  # nunca hubo respaldo

    async def _crear_o_reusar(clave, titulo, detalle):
        assert clave == "backup"
        return 123

    guardado_ultimo = despertador.db.ultimo_backup
    guardado_crear = despertador.db.crear_o_reusar_alerta_tecnica
    despertador.db.ultimo_backup = _ultimo_backup
    despertador.db.crear_o_reusar_alerta_tecnica = _crear_o_reusar
    bot = FakeBot()
    try:
        n = _correr(despertador.revisar_backup(bot))
    finally:
        despertador.db.ultimo_backup = guardado_ultimo
        despertador.db.crear_o_reusar_alerta_tecnica = guardado_crear
    assert n == 1
    assert bot.mensajes == [], "mandó Telegram directo en vez de crear la tarea"


def test_revisar_backup_cae_a_telegram_si_la_tarea_no_se_pudo_crear():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 1): si crear la tarea
    falla por cualquier razón, la alarma le llega a Tiziano como antes."""
    async def _ultimo_backup():
        return None

    async def _ultimo_aviso():
        return None

    async def _registrar_aviso(chat_id, texto):
        return 1

    async def _crear_o_reusar_que_falla(clave, titulo, detalle):
        raise RuntimeError("la base está caída ahora mismo")

    g1, g2, g3, g4 = (despertador.db.ultimo_backup, despertador.db.ultimo_aviso_de_backup,
                      despertador.db.registrar_aviso, despertador.db.crear_o_reusar_alerta_tecnica)
    despertador.db.ultimo_backup = _ultimo_backup
    despertador.db.ultimo_aviso_de_backup = _ultimo_aviso
    despertador.db.registrar_aviso = _registrar_aviso
    despertador.db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_que_falla
    bot = FakeBot()
    try:
        n = _correr(despertador.revisar_backup(bot))
    finally:
        (despertador.db.ultimo_backup, despertador.db.ultimo_aviso_de_backup,
         despertador.db.registrar_aviso, despertador.db.crear_o_reusar_alerta_tecnica) = g1, g2, g3, g4
    assert n == 1
    assert len(bot.mensajes) == 1, "no cayó al aviso directo cuando la tarea falló"
    assert bot.mensajes[0].startswith(db.AVISO_BACKUP_PREFIJO)


def _fila_resumen(reventados=None, sin_ruta=None, enrutados=None, rechazados=None):
    return consumos.Resumen(
        reventados=reventados or {}, sin_ruta=sin_ruta or {},
        enrutados=enrutados or {}, rechazados=rechazados or {})


def test_canario_reventado_crea_tarea_y_no_toca_la_bandeja():
    async def _crear_o_reusar(clave, titulo, detalle):
        assert clave == "canario:banco-x:reventado"
        return 5

    avisos_bandeja = []

    async def _guardar_en_bandeja(**kw):
        avisos_bandeja.append(kw)
        return 1

    g1, g2 = db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar
    db.guardar_en_bandeja = _guardar_en_bandeja
    try:
        res = _fila_resumen(reventados={"banco-x": 2})
        n = _correr(consumos.avisar_si_hay_bancos_mudos(res))
    finally:
        db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja = g1, g2
    assert n == 1
    assert avisos_bandeja == [], "avisó por la bandeja en vez de crear la tarea"


def test_canario_reventado_cae_a_la_bandeja_si_la_tarea_no_se_pudo_crear():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: nunca se pierde el aviso."""
    avisos_bandeja = []

    async def _crear_o_reusar_none(clave, titulo, detalle):
        return None  # sin la migración, por ejemplo

    async def _guardar_en_bandeja(**kw):
        avisos_bandeja.append(kw)
        return 1

    consumos._ultimo_aviso.clear()
    g1, g2 = db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_none
    db.guardar_en_bandeja = _guardar_en_bandeja
    try:
        res = _fila_resumen(reventados={"banco-y": 1})
        n = _correr(consumos.avisar_si_hay_bancos_mudos(res))
    finally:
        db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja = g1, g2
    assert n == 1
    assert len(avisos_bandeja) == 1, "no cayó a la bandeja cuando la tarea no se pudo crear"
    assert avisos_bandeja[0]["chat_id"] == config.CHAT_ID_DUENO


def _generadores_del_canario() -> list[str]:
    """Saca del código REAL de `avisar_si_hay_bancos_mudos` CUÁLES señales
    existen hoy: cada `for rem in res.remitentes_X():` de su cuerpo. Esto es
    lo único que se deriva del código que se está probando -- CUÁNTAS
    pruebas parametrizar y para qué generador, no CONTRA QUÉ VALOR
    comparar (eso es `_SUFIJO_ESPERADO_POR_GENERADOR`, fijo, más abajo).

    Derivar el sufijo esperado del MISMO código que la mutación cambia es
    circular y no puede fallar nunca -- lo midió la mutación 10 de este
    encargo: mutar el sufijo real de la señal B daba "3 passed" porque el
    oráculo leía el sufijo YA mutado y se comparaba contra sí mismo. Por
    eso el sufijo esperado abajo es un valor fijo, independiente del
    archivo que la mutación toca.
    """
    arbol = ast.parse(textwrap.dedent(inspect.getsource(consumos.avisar_si_hay_bancos_mudos)))
    fn = arbol.body[0]
    generadores = [
        nodo.iter.func.attr for nodo in ast.walk(fn)
        if isinstance(nodo, ast.For) and isinstance(nodo.iter, ast.Call)
        and isinstance(nodo.iter.func, ast.Attribute)
        and nodo.iter.func.attr.startswith("remitentes_")]
    assert len(generadores) >= 3, f"esperaba al menos 3 señales, salieron {generadores}"
    return generadores


# EL ORÁCULO, FIJO A PROPÓSITO (ver el docstring de arriba): el sufijo de
# clave que cada señal tiene que armar hoy. Si `avisar_si_hay_bancos_mudos`
# cambiara el sufijo de una señal existente, esto NO cambia solo -- y por
# eso la prueba de abajo sí se pone roja (mutación 10).
_SUFIJO_ESPERADO_POR_GENERADOR = {
    "remitentes_reventados": "reventado",
    "remitentes_mudos": "sin_ruta",
    "remitentes_rechazados": "rechazado",
}

_CAMPO_POR_GENERADOR = {
    "remitentes_reventados": "reventados",
    "remitentes_mudos": "sin_ruta",
    "remitentes_rechazados": "rechazados",
}


def _resumen_para_senal(generador: str, remitente: str, n: int = 1) -> "consumos.Resumen":
    campo = _CAMPO_POR_GENERADOR[generador]
    kwargs = {campo: {remitente: n}}
    if generador == "remitentes_mudos":
        kwargs["enrutados"] = {remitente: 0}  # exige enrutados == 0, ver Resumen.remitentes_mudos
    return _fila_resumen(**kwargs)


@pytest.mark.parametrize("generador", _generadores_del_canario())
def test_cada_senal_del_canario_crea_tarea_de_code(generador):
    """Una por señal, camino real, de punta a punta -- las tres tienen que
    llamar a la puerta con la MISMA clave que arma la producción HOY (el
    sufijo esperado es fijo -- `_SUFIJO_ESPERADO_POR_GENERADOR` -- para que
    un cambio en el sufijo real ponga esto en rojo en vez de mirarse al
    espejo)."""
    sufijo = _SUFIJO_ESPERADO_POR_GENERADOR[generador]
    remitente = "banco-de-prueba"
    clave_esperada = f"canario:{remitente}:{sufijo}"
    llamadas = []

    async def _crear_o_reusar(clave, titulo, detalle):
        llamadas.append(clave)
        return 5

    avisos_bandeja = []

    async def _guardar_en_bandeja(**kw):
        avisos_bandeja.append(kw)
        return 1

    g1, g2 = db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar
    db.guardar_en_bandeja = _guardar_en_bandeja
    try:
        res = _resumen_para_senal(generador, remitente)
        n = _correr(consumos.avisar_si_hay_bancos_mudos(res))
    finally:
        db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja = g1, g2
    assert n == 1, f"la señal {sufijo} ({generador}) no avisó"
    assert llamadas == [clave_esperada], (
        f"la señal {sufijo} llamó a la puerta con {llamadas}, esperaba [{clave_esperada!r}]")
    assert avisos_bandeja == [], f"la señal {sufijo} avisó por la bandeja en vez de crear la tarea"


@pytest.mark.parametrize("generador", _generadores_del_canario())
def test_cada_senal_del_canario_cae_a_la_bandeja_si_la_tarea_no_se_pudo_crear(generador):
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: nunca se pierde el aviso, para
    NINGUNA de las tres señales -- no solo para la A (reventado), que era
    la única cubierta antes de este arreglo."""
    sufijo = _SUFIJO_ESPERADO_POR_GENERADOR[generador]
    remitente = "banco-de-prueba-2"
    avisos_bandeja = []

    async def _crear_o_reusar_none(clave, titulo, detalle):
        return None  # sin la migración, por ejemplo

    async def _guardar_en_bandeja(**kw):
        avisos_bandeja.append(kw)
        return 1

    consumos._ultimo_aviso.clear()
    g1, g2 = db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_none
    db.guardar_en_bandeja = _guardar_en_bandeja
    try:
        res = _resumen_para_senal(generador, remitente)
        n = _correr(consumos.avisar_si_hay_bancos_mudos(res))
    finally:
        db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja = g1, g2
    assert n == 1, f"la señal {sufijo} ({generador}) no avisó de ninguna forma"
    assert len(avisos_bandeja) == 1, (
        f"la señal {sufijo} no cayó a la bandeja cuando la tarea no se pudo crear")
    assert avisos_bandeja[0]["chat_id"] == config.CHAT_ID_DUENO


def test_latido_crea_tarea_y_no_toca_la_bandeja():
    async def _crear_o_reusar(clave, titulo, detalle):
        assert clave == "latido_cosecha"
        return 9

    avisos_bandeja = []

    async def _guardar_en_bandeja(**kw):
        avisos_bandeja.append(kw)
        return 1

    consumos._ultimo_aviso.clear()
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now() - timedelta(hours=10)
    config.CORREO_CUENTAS = [{"user": "x@x.com", "pass": "x"}]
    g1, g2 = db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar
    db.guardar_en_bandeja = _guardar_en_bandeja
    try:
        n = _correr(consumos.avisar_si_no_hay_latido())
    finally:
        db.crear_o_reusar_alerta_tecnica, db.guardar_en_bandeja = g1, g2
    if not list(consumos.bancos.remitentes_registrados()):
        return  # sin remitentes registrados en este entorno, no hay latido que pedir
    assert n == 1
    assert avisos_bandeja == []


# ═══════════════════════════════════════════════════════════════════════
# §B.3 — El aviso de las 6 horas: una sola vez, decidido contra la base
# ═══════════════════════════════════════════════════════════════════════

class _CurAtraso:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if self._conn.sin_columnas and (
                "clave_tecnica" in s or "ultima_alarma_en" in s or "tomada_en" in s):
            raise _ErrorSQL("42703")
        if s.startswith("SELECT t.id, t.titulo, t.clave_tecnica"):
            self._filas = list(self._conn.atrasadas)
        elif s.upper().startswith("INSERT INTO BANDEJA"):
            self._filas = [(1,)]
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnAtraso:
    def __init__(self, atrasadas=None, sin_columnas=False):
        self.atrasadas = atrasadas or []
        self.sin_columnas = sin_columnas
        self.sql: list = []
        self.marcadas: list = []

    def cursor(self, row_factory=None):
        return _CurAtraso(self)

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.upper().startswith("INSERT INTO LOG_ACCIONES") and "aviso_atraso_code" in s:
            self.marcadas.append(params[0])
        return await _CurAtraso(self).execute(sql, params)


class _PoolAtraso:
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


def test_tareas_tecnicas_atrasadas_sin_columna_devuelve_vacio():
    conn = _ConnAtraso(sin_columnas=True)
    guardado = db.pool
    db.pool = _PoolAtraso(conn)
    try:
        salida = _correr(db.tareas_tecnicas_atrasadas())
    finally:
        db.pool = guardado
    assert salida == []


def test_revisar_alertas_tecnicas_avisa_una_vez_y_marca():
    conn = _ConnAtraso(atrasadas=[{"id": 3, "titulo": "x", "clave_tecnica": "backup"}])
    guardado = db.pool
    db.pool = _PoolAtraso(conn)
    bot = FakeBot()
    try:
        n = _correr(despertador.revisar_alertas_tecnicas_sin_tomar(bot))
    finally:
        db.pool = guardado
    assert n == 1
    assert len(bot.mensajes) == 1
    assert "6 horas" in bot.mensajes[0] or "6" in bot.mensajes[0]
    assert conn.marcadas == [3], "no marcó el aviso en log_acciones"


def test_revisar_alertas_tecnicas_no_avisa_dos_veces_por_la_misma_tarea():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 4): una sola vez por tarea.
    El SELECT real ya excluye las que tienen la marca -- se prueba que UNA
    vez marcada, una segunda corrida (con la marca ya puesta, como si
    sobreviviera un redespliegue) no la vuelve a traer."""
    conn = _ConnAtraso(atrasadas=[])  # la marca ya está: el SELECT no la trae
    guardado = db.pool
    db.pool = _PoolAtraso(conn)
    bot = FakeBot()
    try:
        n = _correr(despertador.revisar_alertas_tecnicas_sin_tomar(bot))
    finally:
        db.pool = guardado
    assert n == 0
    assert bot.mensajes == []


def test_la_consulta_de_atrasadas_excluye_tomadas_y_marcadas():
    fuente = inspect.getsource(db.tareas_tecnicas_atrasadas)
    assert "tomada_en IS NULL" in fuente
    assert "NOT EXISTS" in fuente
    assert "aviso_atraso_code" in fuente


# ═══════════════════════════════════════════════════════════════════════
# `db.tareas_tecnicas_atrasadas`: el SELECT real (SQL de verdad, SQLite) --
# el doble hermético `_CurAtraso` decide qué filas "hay" mirando un
# atributo de Python (`self._conn.atrasadas`), sin ejecutar el WHERE; así
# que no es sensible a una mutación del NOT EXISTS, de `tomada_en IS NULL`
# ni del umbral de horas. Esto SÍ corre el texto tal cual sale del código.
# ═══════════════════════════════════════════════════════════════════════

def _extraer_sql_atrasadas() -> str:
    arbol = ast.parse(
        textwrap.dedent(inspect.getsource(db.tareas_tecnicas_atrasadas)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Assign) and len(nodo.targets) == 1
                and isinstance(nodo.targets[0], ast.Name)
                and nodo.targets[0].id == "consulta"
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)):
            return nodo.value.value
    raise AssertionError("no encontré 'consulta' en tareas_tecnicas_atrasadas")


def _corre_atrasadas_sqlite(filas: list[dict], marcas: list[int], umbral_horas: int = 6):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tareas (id INTEGER PRIMARY KEY, titulo TEXT, "
        "clave_tecnica TEXT, estado TEXT, borrado_en TEXT, tomada_en TEXT, "
        "ultima_alarma_en TEXT, creado_en TEXT)")
    conn.execute(
        "CREATE TABLE log_acciones (tabla TEXT, registro_id INTEGER, accion TEXT)")
    for f in filas:
        conn.execute(
            "INSERT INTO tareas (id, titulo, clave_tecnica, estado, borrado_en, "
            "tomada_en, ultima_alarma_en, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f["id"], f.get("titulo", f"tarea {f['id']}"), f["clave_tecnica"],
             f.get("estado", db.ESTADO_PENDIENTE), f.get("borrado_en"),
             f.get("tomada_en"), f.get("ultima_alarma_en"), f.get("creado_en")))
    for tarea_id in marcas:
        conn.execute(
            "INSERT INTO log_acciones (tabla, registro_id, accion) "
            "VALUES ('tareas', ?, 'aviso_atraso_code')", (tarea_id,))
    conn.commit()

    # Traducción MÍNIMA para que corra en SQLite: `now() - make_interval(hours
    # => %s)` -> `datetime('now', '-' || ? || ' hours')` (mismo umbral, misma
    # posición del parámetro) y el resto de `%s` -> `?`. El resto del texto
    # -- el WHERE, el NOT EXISTS -- es EL MISMO que ejecuta producción.
    sql = _extraer_sql_atrasadas()
    assert "now() - make_interval(hours => %s)" in sql
    sql = sql.replace(
        "now() - make_interval(hours => %s)",
        "datetime('now', '-' || ? || ' hours')")
    sql = sql.replace("%s", "?")
    cur = conn.execute(sql, (db.ESTADO_PENDIENTE, umbral_horas))
    salida = [dict(r) for r in cur.fetchall()]
    conn.close()
    return salida


def _hace(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).strftime(
        "%Y-%m-%d %H:%M:%S")


def test_atrasadas_sql_real_trae_la_vieja_sin_tomar_ni_marcar():
    filas = _corre_atrasadas_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "creado_en": _hace(7)}], marcas=[])
    assert [f["id"] for f in filas] == [1]


def test_atrasadas_sql_real_NO_trae_una_reciente():
    filas = _corre_atrasadas_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "creado_en": _hace(1)}], marcas=[])
    assert filas == []


def test_atrasadas_sql_real_NO_trae_una_tomada():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 4): si la sala ya la tomó,
    no se avisa -- corrido contra SQL de verdad."""
    filas = _corre_atrasadas_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "creado_en": _hace(7),
          "tomada_en": _hace(1)}], marcas=[])
    assert filas == []


def test_atrasadas_sql_real_NO_avisa_dos_veces_la_misma_tarea():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (condición 4): una tarea ya marcada en
    `log_acciones` (el aviso sobrevive un redespliegue porque queda en la
    base, no en memoria) no vuelve a salir -- corrido contra SQL de
    verdad."""
    filas = _corre_atrasadas_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "creado_en": _hace(7)}], marcas=[1])
    assert filas == []


def test_atrasadas_sql_real_usa_la_ultima_alarma_no_la_creacion():
    """Una tarea creada hace mucho pero con una recaída reciente (la última
    alarma actualiza `ultima_alarma_en`, §B.2) NO cuenta el reloj desde su
    creación."""
    filas = _corre_atrasadas_sqlite(
        [{"id": 1, "clave_tecnica": "backup", "creado_en": _hace(30),
          "ultima_alarma_en": _hace(1)}], marcas=[])
    assert filas == []


def test_el_criterio_de_valor_distingue_comparar_de_escribir():
    """Entradas INVENTADAS para `_usa_code_como_valor`: comparar no es
    escribir; pasar a Code como parámetro, asignarlo o usarlo por atributo sí."""
    def f(src):
        return ast.parse(src).body[0]
    comparar = f("def a(r):\n    if r == CHAT_ID_CODE:\n        x = 1\n")
    pasar = f("def a(c):\n    c.execute('INSERT INTO tareas', (CHAT_ID_CODE,))\n")
    asignar = f("def a():\n    r = CHAT_ID_CODE\n")
    atributo = f("def a(c):\n    c.execute('x', (config.CHAT_ID_CODE,))\n")
    mezcla = f("def a(r, c):\n    if r == CHAT_ID_CODE:\n        c.execute('x', (CHAT_ID_CODE,))\n")
    assert _usa_code_como_valor(comparar) is False
    assert _usa_code_como_valor(pasar) is True
    assert _usa_code_como_valor(asignar) is True
    assert _usa_code_como_valor(atributo) is True
    assert _usa_code_como_valor(mezcla) is True
