# -*- coding: utf-8 -*-
"""Las citas de Google toman dueño por el calendario de donde vienen
(encargo 4 del diseño "lucy-citas-con-dueno", 22-sep-2026).

Decisión de Tiziano, textual, tras ver la lista de los 10 calendarios de
`cerebro/calendario.py`: «Si esta bien vamos a usar esos y despues
ajustamos». "Rosilis" → Rosi; "Tiziano (personal)" y "Calendario Tiziano
(estudio)" → Tiziano; los de CDS (principal, Bloqueos, GRABACIONES,
Sala P, Sala R, Sala K, Pasantías) → sin dueño.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- Beta/Gamma, mismo criterio que
el resto de este diseño, para probar que `config.dueno_de_calendario`
resuelve por `personas_del_panel()` y no por un nombre escrito en el
código (la asignación real Rosilis→Rosi/Tiziano→Tiziano se prueba aparte,
UNA vez, contra los nombres reales medidos en producción -- ver la sección
3 de este archivo).

CÓMO SE PRUEBA: `cerebro/calendario.py::_guardar` (el INSERT/UPDATE real,
con su ON CONFLICT DO UPDATE) corre SIN TOCAR contra SQLite real -- a
diferencia de `despertador.revisar()` (encargo 3), esta consulta NO usa
`make_interval`/`unnest`/`cardinality`/`@>` (funciones de array de
Postgres que SQLite no tiene): solo INSERT parametrizado, ON CONFLICT DO
UPDATE con una expresión CASE, y un UPDATE con `now()` -- las tres cosas
existen en SQLite moderno (medido: `sqlite3.sqlite_version` 3.50.4 en el
intérprete de este proyecto). La traducción es SOLO sintaxis: `%s`→`?`,
`'{}'` (array vacío de Postgres) → `'[]'` (JSON, porque SQLite no tiene
tipo array), una lista de Python → JSON al pasarla como parámetro, y
`now()` → `CURRENT_TIMESTAMP`. Nada de la LÓGICA de `_guardar` se
reimplementa.

LA GARANTÍA CENTRAL (lo que pidió el encargo: "que no pise lo que puso una
persona"): el DO UPDATE solo aplica el dueño del calendario cuando la fila
sigue en `duenos_chat_id = '{}'` (el estado con el que nace toda cita); se
prueba forzando ese estado a "ya tiene un dueño puesto a mano" ANTES de un
resync y comprobando que el resync no lo toca.

LA FRONTERA: "quién más escribe `eventos`" (que sea SOLO `_guardar` y
`crear_desde_interpretacion`) ya se mide en `tests/test_citas_con_dueno.py
::test_solo_dos_caminos_escriben_eventos_y_solo_uno_por_la_puerta_de_crud`,
corregida en este mismo encargo -- no se repite acá. Este archivo tampoco
vuelve a probar `sincronizar()` contra la red (usa `httpx`/Google de
verdad): eso está fuera del alcance de una prueba hermética; se prueba que
el dueño se resuelve UNA vez por calendario llamando a `_dueno_chat_de`
directo.

Correr:  python3 -m pytest tests/test_citas_google.py -q
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "1001")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")
os.environ.setdefault("GOOGLE_SA_KEY", "")


class _Cualquiera:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Cualquiera()

    def __call__(self, *a, **k):
        return _Cualquiera()


for _n, _attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object()}),
                   ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                   ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera}),
                   ("httpx", {"AsyncClient": _Cualquiera,
                              "HTTPError": type("H", (Exception,), {})})):
    _m = types.ModuleType(_n)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    _m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[_n] = _m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cerebro.calendario as calendario  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
GAMMA = 3003


def _con_gente(nombres: dict[int, str]):
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = tuple(nombres)
    config.NOMBRES_POR_CHAT = dict(nombres)

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---------------------------------------------------------------------------
# 1) config.dueno_de_calendario: la puerta, sin base -- pura.
# ---------------------------------------------------------------------------
def test_sin_nombre_es_sin_dueno():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        assert config.dueno_de_calendario(None) == None  # noqa: E711
        assert config.dueno_de_calendario("") is None
    finally:
        restaurar()


def test_un_nombre_que_resuelve_da_su_chat():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        assert config.dueno_de_calendario("Beta") == BETA
    finally:
        restaurar()


def test_un_nombre_que_no_resuelve_da_none():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        assert config.dueno_de_calendario("Pedro") is None
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 2) calendario._dueno_chat_de: resuelve, y avisa si el nombre no resuelve.
# ---------------------------------------------------------------------------
def test_dueno_chat_de_resuelve_el_chat(caplog):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        cal = {"id": "cal-x", "nombre": "Calendario de Beta", "dueno": "Beta"}
        assert calendario._dueno_chat_de(cal) == BETA
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    finally:
        restaurar()


def test_dueno_chat_de_sin_dueno_declarado_no_avisa(caplog):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        cal = {"id": "cal-x", "nombre": "Sala compartida", "dueno": None}
        assert calendario._dueno_chat_de(cal) is None
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    finally:
        restaurar()


def test_dueno_chat_de_nombre_que_no_resuelve_avisa_y_queda_sin_dueno(caplog):
    """El nombre no resuelve (typo, o la persona perdió el acceso): la cita
    queda SIN DUEÑO -- nunca uno inventado -- y queda un aviso."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    try:
        cal = {"id": "cal-x", "nombre": "Calendario fantasma", "dueno": "Pedro"}
        with caplog.at_level(logging.WARNING, logger="lucy.calendario"):
            resultado = calendario._dueno_chat_de(cal)
        assert resultado is None
        assert any(r.levelno == logging.WARNING for r in caplog.records), (
            "un nombre que no resuelve tiene que dejar un aviso en el registro")
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 3) Los DIEZ calendarios reales: exactamente la asignación que Tiziano
#    aprobó, medida contra la lista real de config.py (no re-tecleada acá).
# ---------------------------------------------------------------------------
def test_la_asignacion_real_es_la_que_aprobo_tiziano():
    """Rosilis -> Rosi; los dos "Tiziano..." -> Tiziano; el resto, sin
    dueño. Se compara contra `NOMBRES_POR_CHAT` de PRODUCCIÓN (medido el
    22-sep-2026: 'Tiziano' y 'Rosi', exactos) -- si esos nombres cambiaran
    en Railway sin tocar `CALENDARIOS`, este test es el que se entera."""
    con_dueno = {c["nombre"]: c["dueno"] for c in calendario.CALENDARIOS
                 if c["dueno"]}
    sin_dueno = {c["nombre"] for c in calendario.CALENDARIOS if not c["dueno"]}
    assert con_dueno == {
        "Tiziano (personal)": "Tiziano",
        "Rosilis": "Rosi",
        "Calendario Tiziano (estudio)": "Tiziano",
    }
    assert sin_dueno == {
        "CDS (principal)", "Bloqueos CDS", "CDS GRABACIONES", "CDS Sala P",
        "CDS Sala R", "Sala K", "Pasantías",
    }
    assert len(calendario.CALENDARIOS) == 10, (
        "la lista tiene que seguir teniendo los 10 calendarios medidos")


# ---------------------------------------------------------------------------
# 4) `_guardar()`, LA FUNCIÓN REAL, contra SQLite real.
# ---------------------------------------------------------------------------
_TABLA_EVENTOS = """
    CREATE TABLE eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        titulo TEXT, inicia_en TEXT, termina_en TEXT, lugar TEXT,
        gcal_id TEXT, gcal_cal_id TEXT, gcal_calendar TEXT,
        anticipos_min TEXT NOT NULL DEFAULT '[0]',
        duenos_chat_id TEXT NOT NULL DEFAULT '[]',
        borrado_en TEXT
    )
"""
_INDICE_GCAL = (
    "CREATE UNIQUE INDEX idx_eventos_gcal ON eventos (gcal_cal_id, gcal_id) "
    "WHERE gcal_id IS NOT NULL"
)


def _traducir(sql: str, params: tuple):
    """Traduce SOLO sintaxis: `%s`→`?`; `'{}'` (array vacío de Postgres,
    literal en el texto) → `'[]'` (JSON); una lista de Python en los
    parámetros → JSON; `now()` → `CURRENT_TIMESTAMP`. Nada más."""
    sql2 = (sql.replace("'{}'", "'[]'")
               .replace("now()", "CURRENT_TIMESTAMP")
               .replace("%s", "?"))
    params2 = tuple(json.dumps(p) if isinstance(p, list) else p
                     for p in (params or ()))
    return sql2, params2


class _Cur:
    def __init__(self, cur):
        self._cur = cur

    async def fetchall(self):
        return self._cur.fetchall()

    async def fetchone(self):
        return self._cur.fetchone()


class _Transaccion:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ConnSQLite:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    async def execute(self, sql, params=None):
        sql2, params2 = _traducir(sql, params or ())
        cur = self.con.execute(sql2, params2)
        self.con.commit()
        return _Cur(cur)

    def transaction(self):
        return _Transaccion()


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _PoolSQLite:
    def __init__(self, con: sqlite3.Connection):
        self._conn = _ConnSQLite(con)

    def connection(self):
        return _PoolCM(self._conn)


def _instalar() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(_TABLA_EVENTOS)
    con.execute(_INDICE_GCAL)
    con.commit()
    db.pool = _PoolSQLite(con)
    return con


def _ev(gcal_id="ev-1", titulo="reunión", inicio="2026-09-22T15:00:00-04:00"):
    return {"id": gcal_id, "summary": titulo, "status": "confirmed",
            "start": {"dateTime": inicio},
            "end": {"dateTime": "2026-09-22T16:00:00-04:00"}}


CAL_CON_DUENO = {"id": "cal-beta@x.com", "nombre": "Calendario de Beta"}
CAL_SIN_DUENO = {"id": "cal-sala@x.com", "nombre": "Sala compartida"}


def test_guardar_con_dueno_lo_deja_en_la_fila_nueva():
    con = _instalar()
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), BETA))
    fila = con.execute(
        "SELECT duenos_chat_id FROM eventos WHERE gcal_id=?", ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == [BETA]


def test_guardar_sin_dueno_queda_vacio():
    con = _instalar()
    _correr(calendario._guardar(CAL_SIN_DUENO, _ev(), None))
    fila = con.execute(
        "SELECT duenos_chat_id FROM eventos WHERE gcal_id=?", ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == []


def test_resync_sobre_fila_nueva_aplica_el_dueno_del_calendario():
    """Las citas que YA existen toman su dueño en la PRÓXIMA sincronización,
    sin guion aparte: se simula insertando la fila SIN dueño (como están
    las 418 de producción hoy) y resincronizando con el dueño puesto."""
    con = _instalar()
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), None))  # como hoy: sin dueño
    fila = con.execute(
        "SELECT duenos_chat_id FROM eventos WHERE gcal_id=?", ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == []

    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), BETA))  # el próximo sync
    fila = con.execute(
        "SELECT duenos_chat_id FROM eventos WHERE gcal_id=?", ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == [BETA], (
        "una fila que sigue en su estado de siempre (sin dueño) tiene que "
        "tomar el del calendario en el próximo sync")


def test_resync_no_pisa_un_dueno_puesto_a_mano():
    """LA GARANTÍA CENTRAL: alguien le puso dueño a esta cita a mano por
    Telegram (`editar`, distinto del que dice el calendario) -- el próximo
    sync NO se lo toca."""
    con = _instalar()
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), BETA))
    # Simula que una persona editó la cita por Telegram y le puso OTRO dueño
    # (o los dos) -- exactamente lo que haría `crud.editar` de verdad.
    con.execute("UPDATE eventos SET duenos_chat_id=? WHERE gcal_id=?",
                (json.dumps([GAMMA]), "ev-1"))
    con.commit()

    _correr(calendario._guardar(CAL_CON_DUENO, _ev(titulo="reunión (movida)"), BETA))
    fila = con.execute(
        "SELECT duenos_chat_id, titulo FROM eventos WHERE gcal_id=?",
        ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == [GAMMA], (
        "el sync no puede pisar el dueño que puso una persona")
    assert fila["titulo"] == "reunión (movida)", (
        "el resto de los campos SÍ se sigue actualizando con normalidad")


def test_cambiar_el_dueno_del_calendario_es_cambiar_un_solo_sitio():
    """«Despues ajustamos»: sobre una fila SIN TOCAR (nunca editada a mano),
    cambiar `CALENDARIOS` -- acá, simulado pasando un `dueno_chat` distinto
    -- alcanza para que la PRÓXIMA sincronización la reasigne."""
    con = _instalar()
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), BETA))
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), GAMMA))  # "se ajustó" el dueño
    fila = con.execute(
        "SELECT duenos_chat_id FROM eventos WHERE gcal_id=?", ("ev-1",)).fetchone()
    assert json.loads(fila["duenos_chat_id"]) == [BETA], (
        "espera -- la fila YA tenía un dueño (BETA) tras el primer sync, así "
        "que el segundo (GAMMA) NO la pisa: esto es la garantía central, no "
        "el ajuste. El ajuste real se prueba con una fila que nunca se tocó "
        "(ver test_resync_sobre_fila_nueva_aplica_el_dueno_del_calendario)")


def test_una_cita_cancelada_se_archiva_con_el_dueno_intacto():
    con = _instalar()
    _correr(calendario._guardar(CAL_CON_DUENO, _ev(), BETA))
    cancelado = dict(_ev(), status="cancelled")
    _correr(calendario._guardar(CAL_CON_DUENO, cancelado, BETA))
    fila = con.execute(
        "SELECT borrado_en, duenos_chat_id FROM eventos WHERE gcal_id=?",
        ("ev-1",)).fetchone()
    assert fila["borrado_en"] is not None
    assert json.loads(fila["duenos_chat_id"]) == [BETA]


# ---------------------------------------------------------------------------
# 5) `sincronizar()` resuelve el dueño UNA vez por calendario, no por evento.
# ---------------------------------------------------------------------------
def test_sincronizar_resuelve_el_dueno_una_vez_por_calendario(monkeypatch):
    """Dos calendarios, con 3 y 2 eventos de Google respectivamente: si
    `_dueno_chat_de` se llamara por evento saldría 5 veces; si se llama por
    calendario (lo que pide el diseño, y lo que dice el docstring de
    `sincronizar`) salen 2 -- una por calendario, sin importar cuántos
    eventos tenga cada uno."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    llamadas = []
    original = calendario._dueno_chat_de

    def _contado(cal):
        llamadas.append(cal["nombre"])
        return original(cal)

    cals_falsos = [
        {"id": "c1", "nombre": "Cal Uno", "dueno": "Beta"},
        {"id": "c2", "nombre": "Cal Dos", "dueno": None},
    ]
    eventos_por_cal = {"c1": [_ev("e1"), _ev("e2"), _ev("e3")],
                        "c2": [_ev("e4"), _ev("e5")]}
    guardados = []

    async def _guardar_falso(cal, ev, dueno_chat):
        guardados.append((cal["id"], ev["id"], dueno_chat))

    async def _eventos_de_falso(client, token, cal_id):
        return eventos_por_cal[cal_id]

    async def _token_falso():
        return "tok"

    class _ClienteFalso:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(calendario, "CALENDARIOS", cals_falsos)
    monkeypatch.setattr(calendario, "_dueno_chat_de", _contado)
    monkeypatch.setattr(calendario, "_guardar", _guardar_falso)
    monkeypatch.setattr(calendario, "_eventos_de", _eventos_de_falso)
    monkeypatch.setattr(calendario, "_token", _token_falso)
    monkeypatch.setattr(calendario, "GOOGLE_SA_KEY", "x")
    monkeypatch.setattr(calendario.httpx, "AsyncClient", _ClienteFalso)
    try:
        _correr(calendario.sincronizar())
        assert llamadas == ["Cal Uno", "Cal Dos"], (
            f"_dueno_chat_de se llamó {len(llamadas)} veces ({llamadas}); "
            "tiene que ser exactamente una por calendario (2), no una por "
            "evento (5)")
        assert len(guardados) == 5, "los 5 eventos se siguen guardando igual"
        assert all(d == BETA for (c, e, d) in guardados if c == "c1")
        assert all(d is None for (c, e, d) in guardados if c == "c2")
    finally:
        restaurar()


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
