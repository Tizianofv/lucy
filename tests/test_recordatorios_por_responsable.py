# -*- coding: utf-8 -*-
"""Recordatorios por responsable (encargo 2, 22-sep-2026).

Diseño: `disenos/lucy-rosi-independiente/DISENO.md`, "Encargo 2": el
recordatorio de una tarea va a `responsable_chat_id`; sin responsable, al
dueño. Las citas (`eventos`) no tienen esa columna (medido contra
`db/schema.sql:313-336` el 22-sep-2026: el `CREATE TABLE eventos` no la
trae) -- siguen yendo SIEMPRE al dueño Y SIGUEN COPIÁNDOSE A ROSI, sin
cambio de ningún tipo, ni en a quién le llegan ni en si se copian.

CORREGIDO tras el NO PASA del testigo sobre `921abdd`: la primera versión
de este encargo apagaba la copia (`sin_copia=True`) para TODO recordatorio
que terminara yendo al dueño, tareas y citas por igual -- eso apagaba
también la copia de las citas, que el diseño deja "sin cambio". Tiziano no
tomó esa decisión: lo que sí decidió (22-sep-2026, textual: "Que las citas
tengan dueño") es que `eventos` va a tener su propio responsable, pero en
OTRO encargo, que todavía no existe. Hasta que exista, `sin_copia` se pasa
SOLO para recordatorios de TAREAS (`f["tabla"] == "tareas"`) -- nunca para
citas.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- Beta y Gamma, igual que
`tests/test_briefing_por_persona.py`, para probar que el ruteo sale de
`tareas.responsable_chat_id` y no de un nombre escrito en el código.

LA FRONTERA, dicha: este archivo prueba `despertador.revisar()` (los
recordatorios de tareas/citas) y `despertador._avisar()` (a quién manda y
si copia). NO vuelve a probar el briefing/plan semanal por persona -- eso
ya lo cubre `test_briefing_por_persona.py` -- ni la puerta de "Primero:" en
sí misma -- eso ya lo cubre `test_primero.py`; acá solo se comprueba que
seguir viva CON un responsable puesto (el caso nuevo de este encargo) no la
rompe.

Herméticos: sin Postgres ni red -- mismos stubs que el resto de la suite.

Correr:  python3 -m pytest tests/test_recordatorios_por_responsable.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1001")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

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

_openai = types.ModuleType("openai")


class _StubAsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _StubAsyncOpenAI
sys.modules.setdefault("openai", _openai)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import acciones.crud as crud  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402
from cerebro import despertador  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
GAMMA = 3003
AJENO = 999999   # sin acceso: no puede ser responsable


def _con_gente():
    """Mismo patrón que `tests/test_briefing_por_persona.py::_con_gente` --
    `config.puede_ser_responsable` lee estos dos módulo-globales directo, no
    del entorno, así que hay que fijarlos para que este archivo no dependa
    de qué otro archivo de la suite importó `config` primero."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = (DUENO, BETA, GAMMA)
    config.NOMBRES_POR_CHAT = {DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"}

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ---------------------------------------------------------------------------
# Andamios: una conexión falsa que sirve las filas de `revisar()` y registra
# los UPDATE. `_BotFalso` guarda cada `send_message` junto con si
# `copia_dueno._sin_copia` estaba activo EN ESE MOMENTO -- así se comprueba
# la supresión sin inventar un doble de `copia_dueno`.
# ---------------------------------------------------------------------------
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

    def _norm(self, sql):
        return " ".join(sql.split())

    async def execute(self, sql, params=None):
        self.sqls.append((self._norm(sql), params or ()))
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


class _BotFalso:
    def __init__(self):
        self.enviados: list[dict] = []

    async def send_message(self, **kw):
        self.enviados.append({**kw, "sin_copia": copia_dueno._sin_copia.get()})


def _instalar(conn):
    """Base falsa + callar briefing/semanal/recurrentes -- este archivo solo
    prueba `revisar()` en lo que hace a recordatorios (ver "LA FRONTERA" en
    la cabecera)."""
    db.pool = FakePool(conn)
    conn.avisos_registrados: list[tuple[int, str]] = []

    async def _registrar_aviso(chat_id, texto):
        conn.avisos_registrados.append((chat_id, texto))

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
    crud._registrar = _registrar


def _fila(tabla, id_, titulo, cuando, responsable=None):
    return {"tabla": tabla, "id": id_, "titulo": titulo, "cuando": cuando,
            "avisos_enviados": [], "anticipos_min": [0],
            "responsable_chat_id": responsable}


AHORA = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1) Ruteo: tarea con responsable real (hermanos Beta/Gamma), sin estar
#    tecleado en despertador.py.
# ---------------------------------------------------------------------------
def test_tarea_con_responsable_le_llega_a_ese_chat():
    conn = FakeConn([_fila("tareas", 1, "Pagar el estudio", AHORA, BETA)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        avisos = _correr(despertador.revisar(bot))
        assert avisos == 1
        assert len(bot.enviados) == 1
        assert bot.enviados[0]["chat_id"] == BETA
        assert conn.avisos_registrados == [(BETA, bot.enviados[0]["text"])]
    finally:
        restaurar_gente()


def test_otra_tarea_con_otro_responsable_le_llega_a_ese_otro():
    """Mismo mecanismo, otro hermano (Gamma) -- no es un caso especial de
    Beta escrito a mano."""
    conn = FakeConn([_fila("tareas", 2, "Comprar cinta", AHORA, GAMMA)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == GAMMA
    finally:
        restaurar_gente()


def test_tarea_sin_responsable_le_llega_al_dueno():
    conn = FakeConn([_fila("tareas", 3, "Renovar el dominio", AHORA, None)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == DUENO
    finally:
        restaurar_gente()


def test_responsable_sin_acceso_cae_al_dueno():
    """Un `responsable_chat_id` que quedó pegado en una fila vieja, de un
    chat que ya no puede entrar -- misma puerta que
    `despertador._destinatarios_de_tareas` (`config.puede_ser_responsable`),
    no una copia de la lógica."""
    conn = FakeConn([_fila("tareas", 4, "Vieja", AHORA, AJENO)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == DUENO
    finally:
        restaurar_gente()


def test_el_sql_de_eventos_nunca_declara_un_responsable_real():
    """Comprobación ESTRUCTURAL, sobre el texto de `revisar()`: las DOS
    ramas de `eventos` (con y sin `primero_id`) tienen que traer un
    `NULL::BIGINT` fijo para `responsable_chat_id` -- si algún día alguien
    intenta ponerle un valor real (por ejemplo, reusar `id`), esta prueba
    lo dice sin necesitar Postgres."""
    import inspect
    fuente = inspect.getsource(despertador.revisar)
    apariciones = fuente.count("NULL::BIGINT")
    assert apariciones == 2, (
        f"esperaba 2 apariciones de NULL::BIGINT (una por rama de eventos, "
        f"con y sin primero_id), hay {apariciones}")


def test_una_cita_siempre_le_llega_al_dueno_aunque_haya_responsables():
    """Los eventos no tienen `responsable_chat_id` -- la columna ni existe
    en `eventos` -- así que la fila llega con NULL sin importar quién más
    tenga tareas asignadas ese día."""
    conn = FakeConn([_fila("eventos", 5, "Dentista", AHORA, None)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == DUENO
    finally:
        restaurar_gente()


# ---------------------------------------------------------------------------
# 2) La copia: el recordatorio del dueño de una TAREA no se copia; el de una
#    CITA SÍ, sin cambio -- ésta es la pareja que faltaba (NO PASA del
#    testigo sobre `921abdd`): antes `sin_copia=True` salía para las dos por
#    igual, y una cita al dueño dejaba de copiarse a Rosi sin que el diseño
#    lo pidiera. Las citas todavía no tienen responsable propio (eso es
#    "Que las citas tengan dueño", un encargo aparte que Tiziano pidió el
#    22-sep-2026 y que todavía no existe): hasta que exista, su recordatorio
#    es indistinguible de como era ANTES de este encargo entero.
# ---------------------------------------------------------------------------
def test_el_recordatorio_de_una_tarea_del_dueno_no_se_copia():
    conn = FakeConn([_fila("tareas", 6, "Pagar la luz", AHORA, None)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == DUENO
        assert bot.enviados[0]["sin_copia"] is True
    finally:
        restaurar_gente()


def test_el_recordatorio_de_una_cita_SI_se_copia_sin_cambio():
    """La pareja exacta de la prueba de arriba: mismo destino (el dueño),
    misma forma de fila, pero `tabla == "eventos"` -- y acá `sin_copia`
    TIENE que ser False, porque una cita todavía no tiene responsable
    propio y el diseño no toca su copia."""
    conn = FakeConn([_fila("eventos", 9, "Dentista", AHORA, None)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == DUENO
        assert bot.enviados[0]["sin_copia"] is False, (
            "el recordatorio de una cita al dueño tiene que seguir "
            "copiándose a Rosi, sin cambio")
    finally:
        restaurar_gente()


def test_el_recordatorio_de_beta_no_pasa_por_la_puerta_de_copia():
    """`copia_dueno` solo copia lo que sale HACIA el chat del dueño -- el de
    Beta va a OTRO chat_id, así que la puerta ni se activa (el contextvar
    puede quedar True porque el `with sin_copiar()` se pide igual, pero eso
    no tiene efecto visible: `_send_message_con_copia` solo copia si
    `chat_id == config.CHAT_ID_DUENO`)."""
    conn = FakeConn([_fila("tareas", 7, "Entregar diseño", AHORA, BETA)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        _correr(despertador.revisar(bot))
        assert bot.enviados[0]["chat_id"] == BETA
    finally:
        restaurar_gente()


def test_el_aviso_de_respaldo_sigue_copiandose_no_es_de_este_encargo():
    """Hermano por función (`_avisar`) pero de OTRO camino: el aviso de
    respaldo no pasa `sin_copia`, así que sigue copiándose igual que antes
    -- tocarlo es de otro encargo (decisión de Tiziano, b: los avisos que no
    son de nadie se quedan solo con él, pero eso se aplica apagando la
    copia general, no acá)."""
    registrados = []

    async def _registrar_aviso(chat_id, texto):
        registrados.append((chat_id, texto))

    db.registrar_aviso = _registrar_aviso
    bot = _BotFalso()
    _correr(despertador._avisar(bot, "🚨 Sin respaldo de la base\n\n..."))
    assert bot.enviados[0]["chat_id"] == DUENO
    assert bot.enviados[0]["sin_copia"] is False
    assert registrados == [(DUENO, "🚨 Sin respaldo de la base\n\n...")]


# ---------------------------------------------------------------------------
# 3) "Primero:" sigue vivo con un responsable puesto (no se rompió al
#    agregar la columna a la consulta).
# ---------------------------------------------------------------------------
def test_primero_con_responsable_sigue_sin_sonar():
    """La tarea tiene responsable Y espera a otra que sigue pendiente: el
    SQL real (`NOT EXISTS`) la deja afuera -- acá se simula devolviendo CERO
    filas, que es lo que la consulta real haría en ese caso, y se comprueba
    que la CONSULTA sigue pidiendo `responsable_chat_id` (para el día en que
    sí entre alguna)."""
    conn = FakeConn([])   # la consulta real no trae nada: la tarea espera
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        avisos = _correr(despertador.revisar(bot))
        assert avisos == 0
        assert bot.enviados == []
        grandes = [s for s, _ in conn.sqls if s.startswith("SELECT 'tareas' AS tabla")]
        assert grandes, "no se corrió ninguna consulta grande"
        assert "responsable_chat_id" in grandes[0], (
            "la consulta con primero_id dejó de pedir responsable_chat_id")
    finally:
        restaurar_gente()


def test_la_consulta_de_respaldo_sin_primero_id_tambien_trae_responsable():
    """Si la columna `primero_id` todavía no existe (SQLSTATE 42703), cae a
    la consulta vieja -- que TAMBIÉN tiene que pedir `responsable_chat_id`,
    o el respaldo de compatibilidad perdería el ruteo por persona."""

    class _SinColumna(Exception):
        sqlstate = "42703"

    class _CursorQueFalla(_Cur):
        def __init__(self, conn, filas):
            super().__init__(filas)
            self._conn = conn

        async def execute(self, sql, params=None):
            self._conn.sqls.append((self._conn._norm(sql), params or ()))
            if not self._conn._ya_fallo:
                self._conn._ya_fallo = True
                raise _SinColumna()
            return self

    class _ConnFalla(FakeConn):
        def __init__(self, filas):
            super().__init__(filas)
            self._ya_fallo = False   # compartido entre los DOS cursores que pide revisar()

        def cursor(self, row_factory=None):
            # El PRIMER cursor (con primero_id) revienta y no sirve filas; el
            # SEGUNDO (la consulta de respaldo) sirve las de verdad.
            filas = self._filas if self._ya_fallo else []
            return _CursorQueFalla(self, filas)

    conn = _ConnFalla([_fila("tareas", 8, "Sin migrar", AHORA, BETA)])
    _instalar(conn)
    restaurar_gente = _con_gente()
    bot = _BotFalso()
    try:
        avisos = _correr(despertador.revisar(bot))
        assert avisos == 1
        assert bot.enviados[0]["chat_id"] == BETA
        chicas = [s for s, _ in conn.sqls
                  if s.startswith("SELECT 'tareas' AS tabla")
                  and "primero_id" not in s]
        assert chicas, "no cayó a la consulta de respaldo"
        assert "responsable_chat_id" in chicas[0], (
            "la consulta de respaldo (sin primero_id) no pide responsable_chat_id")
    finally:
        restaurar_gente()


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
