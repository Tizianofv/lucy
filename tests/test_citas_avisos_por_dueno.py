# -*- coding: utf-8 -*-
"""Recordatorios de citas por dueño (encargo 3 del diseño
"lucy-citas-con-dueno", 22-sep-2026).

Decisiones de Tiziano, en la sección final de
`disenos/lucy-citas-con-dueno/DISENO.md`:
  · cita CON dueño(s): el aviso a la hora le llega DIRECTO a cada dueño, y
    a NADIE más -- tampoco por la copia general.
  · cita SIN dueño: le llega A LOS DOS, directo, sin depender de
    `COPIAS_DEL_DUENO` -- porque esa copia se apaga después (encargo
    aparte) y nadie puede quedarse sin el aviso.
  · «Los dos» sale de `config.personas_del_panel()`
    (`config.puede_ser_responsable`), no de una lista tecleada.
  · Tareas: sin cambio -- eso lo prueba `tests/test_recordatorios_por_
    responsable.py`, no este archivo (ver LA FRONTERA).

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- Alfa/Beta/Gamma, mismo criterio
que el resto de este diseño: "los dos" y "cada dueño" se prueban con
nombres inventados para que quede claro que el reparto sale de
`personas_del_panel()`/`puede_ser_responsable`, no de un nombre escrito en
el código.

CÓMO SE PRUEBA:
  · `despertador.revisar()` corre ENTERO, sin tocar: lo único que se
    reemplaza es `db.pool`, por una conexión que SIRVE filas (como si
    fueran la respuesta de Postgres) y registra cada SQL que el código real
    ejecuta -- mismo patrón, ya aceptado por el testigo, que usan `tests/
    test_calendario_no_avisa.py` y la versión del encargo 2 de `tests/
    test_recordatorios_por_responsable.py`. La diferencia con lo que el
    testigo marcó NO PASA en el encargo de correo: ahí una función CUYO
    ÚNICO TRABAJO era una consulta SQL (`db.correos_ya_reportados`) se
    reemplazaba entera por una reimplementación en Python. Acá no hay
    ninguna función así: la lógica de "a quién avisar" y "se copia o no"
    vive DENTRO de `revisar()`, que corre completa; lo que se dobla es la
    RESPUESTA de la fila (lo que Postgres devolvería), no la lógica.
  · El TEXTO del SQL (`duenos_chat_id` seleccionado, `NULL::BIGINT[]`/
    `NULL::BIGINT` en el lugar correcto) se comprueba aparte, de forma
    ESTRUCTURAL, con `inspect.getsource` -- mismo criterio que ya usa `test_
    recordatorios_por_responsable.py` para lo mismo. No se traduce a SQLite
    porque la consulta real usa `make_interval`, `unnest`, `cardinality` y
    `@>` (arreglos de Postgres) que SQLite no tiene: traducirlos sería
    escribir una segunda implementación de esa consulta, exactamente el
    riesgo que el NO PASA de correo vino a evitar.
  · La copia SÍ se prueba con el parche REAL: `copia_dueno.instalar()`
    sobre `telegram.Bot.send_message` (doblado un nivel más abajo, sin
    red), igual que `tests/test_copia_a_rosi.py`. Así "no se copia" se mide
    contando cuántos envíos salieron de la puerta de verdad, no leyendo un
    contextvar.

LA FRONTERA: este archivo prueba el REPARTO de `despertador.revisar()` para
`eventos` (a quién, y si se copia) y la dedupe de `avisos_enviados` para
una fila con VARIOS destinatarios. NO vuelve a probar tareas (sin cambio,
ver `test_recordatorios_por_responsable.py`), ni el briefing/plan semanal,
ni "Primero:" -- esos ya tienen sus propios archivos.

Correr:  python3 -m pytest tests/test_citas_avisos_por_dueno.py -q
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1001")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import telegram  # noqa: E402

import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.despertador as despertador  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
GAMMA = 3003
AJENO = 999999   # sin acceso: no puede ser dueño ni "uno de los dos"

AHORA = datetime.now(timezone.utc)


def _con_gente(nombres: dict[int, str]):
    """Mismo patrón que el resto del diseño: `config.puede_ser_responsable`
    y `personas_del_panel()` leen estos dos módulo-globales directo."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = tuple(nombres)
    config.NOMBRES_POR_CHAT = dict(nombres)

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


# ---------------------------------------------------------------------------
# Andamios: fila servida a `revisar()`, y el resto de la maquinaria callada
# (briefing/semanal/recurrentes no son parte de este encargo).
# ---------------------------------------------------------------------------
def _fila(tabla, id_, titulo, cuando, *, responsable=None, duenos=None):
    return {"tabla": tabla, "id": id_, "titulo": titulo, "cuando": cuando,
            "avisos_enviados": [], "anticipos_min": [0],
            "responsable_chat_id": responsable,
            "duenos_chat_id": list(duenos) if duenos is not None else []}


class _Cur:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows

    async def execute(self, sql, params=None):
        return self


class _Transaccion:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class FakeConn:
    def __init__(self, filas):
        self.sqls: list[tuple[str, tuple]] = []
        self._filas = filas
        self._servidas = False
        self.updates: list[tuple[str, tuple]] = []

    def _norm(self, sql):
        return " ".join(sql.split())

    async def execute(self, sql, params=None):
        s = self._norm(sql)
        self.sqls.append((s, params or ()))
        if s.startswith("UPDATE"):
            self.updates.append((s, params or ()))
        return _Cur()

    def cursor(self, row_factory=None):
        filas, self._servidas = ([] if self._servidas else self._filas), True
        return _CursorConFilas(self, filas)

    def transaction(self):
        return _Transaccion(self)


class _CursorConFilas(_Cur):
    def __init__(self, conn, filas):
        super().__init__(filas)
        self._conn = conn

    async def execute(self, sql, params=None):
        self._conn.sqls.append((self._conn._norm(sql), params or ()))
        return self


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _PoolCM(self._conn)


def _instalar(conn):
    """Base falsa (filas de `eventos`/`tareas`) + `log_acciones`/`registrar_
    aviso`/briefing/semanal/recurrentes callados -- fuera de la frontera de
    este archivo."""
    db.pool = FakePool(conn)

    async def _registrar_aviso(chat_id, texto):
        return 1

    async def _cero():
        return 0

    async def _nada():
        return 0

    async def _registrar(*a, **k):
        return None

    db.registrar_aviso = _registrar_aviso
    despertador._briefing = _cero
    despertador._semanal = _cero
    despertador._reprogramar_recurrentes = _nada
    despertador.crud._registrar = _registrar


# ---------------------------------------------------------------------------
# El parche REAL de copia_dueno, sobre telegram.Bot.send_message doblado un
# nivel más abajo -- mismo arnés que tests/test_copia_a_rosi.py.
# ---------------------------------------------------------------------------
@pytest.fixture
def puerta():
    pristino = telegram.Bot.send_message
    enviados: list[dict] = []

    async def _doble(self, chat_id, text, **kwargs):
        enviados.append({"chat_id": chat_id, "text": text})
        return types.SimpleNamespace(message_id=len(enviados))

    telegram.Bot.send_message = _doble
    copia_dueno.instalar()
    try:
        yield enviados
    finally:
        copia_dueno.desinstalar()
        telegram.Bot.send_message = pristino


@pytest.fixture
def bot():
    return telegram.Bot(token="123456:token-de-prueba")


# ---------------------------------------------------------------------------
# 1) Cita CON dueño(s): directo, y a nadie más -- ni siquiera por la copia.
# ---------------------------------------------------------------------------
async def test_cita_con_un_dueno_le_llega_solo_a_el_y_no_se_copia(puerta, bot):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ("Beta",)   # COPIAS_DEL_DUENO="Beta"
    try:
        conn = FakeConn([_fila("eventos", 1, "reunión", AHORA, duenos=[BETA])])
        _instalar(conn)
        n = await despertador.revisar(bot)
        assert n == 1
        assert len(puerta) == 1, (
            f"tenía que salir UN solo envío (a Beta, sin copia): {puerta}")
        assert puerta[0]["chat_id"] == BETA
    finally:
        restaurar()


async def test_cita_con_el_dueno_mismo_no_se_copia_a_la_persona_de_copia(puerta, bot):
    """El caso que de verdad importa: si el dueño de la cita ES quien
    normalmente recibiría la copia (`COPIAS_DEL_DUENO`), esa copia NO tiene
    que salir -- sin esto, Beta vería la cita dos veces (como dueño Y como
    copia) o, peor, una cita que solo es de Alfa le llegaría a Beta igual
    por la copia general."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ("Beta",)
    try:
        conn = FakeConn([_fila("eventos", 2, "reunión de Alfa", AHORA, duenos=[DUENO])])
        _instalar(conn)
        n = await despertador.revisar(bot)
        assert n == 1
        assert len(puerta) == 1, (
            f"el envío al dueño se copió a Beta y no tenía que: {puerta}")
        assert puerta[0]["chat_id"] == DUENO
    finally:
        restaurar()


async def test_cita_con_los_dos_duenos_manda_dos_envios_directos(puerta, bot):
    """«Puede ser de los dos» (encargo 1+2): dos dueños, dos envíos, cada
    uno directo -- y ninguno se copia a un tercero."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"})
    config._NOMBRES_DE_COPIA = ("Gamma",)
    try:
        conn = FakeConn([_fila("eventos", 3, "sesión", AHORA, duenos=[DUENO, BETA])])
        _instalar(conn)
        n = await despertador.revisar(bot)
        assert n == 1  # una campanada, aunque tenga dos destinatarios
        assert len(puerta) == 2, f"tenían que ser 2 envíos: {puerta}"
        assert {e["chat_id"] for e in puerta} == {DUENO, BETA}
        assert GAMMA not in {e["chat_id"] for e in puerta}, (
            "Gamma no es dueño de esta cita: no le corresponde")
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 2) Cita SIN dueño: «A los dos» -- derivado de personas_del_panel().
# ---------------------------------------------------------------------------
async def test_cita_sin_dueno_le_llega_a_todos_los_de_personas_del_panel(puerta, bot):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"})
    config._NOMBRES_DE_COPIA = ()
    try:
        conn = FakeConn([_fila("eventos", 4, "sin dueño", AHORA, duenos=[])])
        _instalar(conn)
        n = await despertador.revisar(bot)
        assert n == 1
        assert {e["chat_id"] for e in puerta} == {DUENO, BETA, GAMMA}, (
            f"«a los dos» (acá, a los tres) tiene que salir de personas_del_"
            f"panel(), no de una lista tecleada: {puerta}")
    finally:
        restaurar()


async def test_un_cuarto_en_personas_del_panel_tambien_recibe_la_sin_dueno(puerta, bot):
    """La lista NO está tecleada en `despertador.py`: si mañana entra una
    cuarta persona a la casa, una cita sin dueño le llega a ella también sin
    tocar el código -- se prueba agregando un cuarto hermano."""
    DELTA = 4004
    restaurar = _con_gente(
        {DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma", DELTA: "Delta"})
    config._NOMBRES_DE_COPIA = ()
    try:
        conn = FakeConn([_fila("eventos", 5, "sin dueño", AHORA, duenos=[])])
        _instalar(conn)
        await despertador.revisar(bot)
        assert {e["chat_id"] for e in puerta} == {DUENO, BETA, GAMMA, DELTA}
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 3) Un dueño sin acceso no recibe nada; si TODOS los dueños perdieron
#    acceso, cae a "los dos".
# ---------------------------------------------------------------------------
async def test_un_dueno_sin_acceso_se_filtra_y_el_resto_igual_recibe(puerta, bot):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ()
    try:
        conn = FakeConn([_fila("eventos", 6, "cita", AHORA, duenos=[BETA, AJENO])])
        _instalar(conn)
        await despertador.revisar(bot)
        assert {e["chat_id"] for e in puerta} == {BETA}, (
            "AJENO no tiene acceso: no se le puede avisar, pero Beta sí")
    finally:
        restaurar()


async def test_todos_los_duenos_sin_acceso_cae_a_los_dos(puerta, bot):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ()
    try:
        conn = FakeConn([_fila("eventos", 7, "cita vieja", AHORA, duenos=[AJENO])])
        _instalar(conn)
        await despertador.revisar(bot)
        assert {e["chat_id"] for e in puerta} == {DUENO, BETA}, (
            "sin ningún dueño con acceso, cae a personas_del_panel() entera")
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 4) La dedupe: avisarle a uno no le roba el aviso a otro -- porque TODOS
#    los destinatarios de la fila se mandan antes de marcar la campanada.
# ---------------------------------------------------------------------------
async def test_los_dos_duenos_quedan_avisados_antes_de_marcar_la_campanada(puerta, bot):
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ()
    try:
        conn = FakeConn([_fila("eventos", 8, "sesión", AHORA, duenos=[DUENO, BETA])])
        _instalar(conn)
        await despertador.revisar(bot)
        assert {e["chat_id"] for e in puerta} == {DUENO, BETA}
        # Y el UPDATE de avisos_enviados salió UNA sola vez para la fila --
        # no una vez por destinatario, que dejaría el candado en un estado
        # a medio marcar si algo fallara entre el primero y el segundo.
        updates_de_la_fila = [u for u in conn.updates if u[1] == ([0], 8)]
        assert len(updates_de_la_fila) == 1, (
            f"esperaba 1 UPDATE para la fila #8, hubo {len(updates_de_la_fila)}")
    finally:
        restaurar()


async def test_tareas_siguen_siendo_un_solo_destino_sin_cambio(puerta, bot):
    """LA FRONTERA: tareas no cambia con este encargo. Regresión corta --
    la cobertura completa vive en test_recordatorios_por_responsable.py."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ("Beta",)
    try:
        conn = FakeConn([_fila("tareas", 9, "pagar", AHORA, responsable=BETA)])
        _instalar(conn)
        await despertador.revisar(bot)
        assert len(puerta) == 1
        assert puerta[0]["chat_id"] == BETA
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 5) El TEXTO del SQL: duenos_chat_id viaja en las dos ramas, en el lugar
#    correcto -- comprobación estructural, sin traducir Postgres a SQLite
#    (ver LA FRONTERA en la cabecera del archivo).
# ---------------------------------------------------------------------------
def test_el_sql_de_eventos_trae_duenos_chat_id_en_las_dos_ramas():
    import inspect
    fuente = inspect.getsource(despertador.revisar)
    assert fuente.count("duenos_chat_id") >= 4, (
        "duenos_chat_id tiene que aparecer en la rama con primero_id y en "
        "la de respaldo, cada una tanto en el SELECT de eventos como en el "
        "hueco NULL::BIGINT[] del lado de tareas")


def test_el_sql_de_tareas_trae_un_hueco_array_para_duenos():
    import inspect
    fuente = inspect.getsource(despertador.revisar)
    # La forma EXACTA que escribe el código (con el " AS duenos_chat_id"
    # detrás) para no contar, de paso, la mención suelta en un comentario.
    apariciones = fuente.count("NULL::BIGINT[] AS duenos_chat_id")
    assert apariciones == 2, (
        f"cada rama (con y sin primero_id) tiene que dejar un hueco "
        f"NULL::BIGINT[] del lado de tareas para que el UNION ALL tenga la "
        f"misma forma de columnas que el lado de eventos, hay {apariciones}")
