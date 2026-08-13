"""Tests del corte del 13-ago-2026: el calendario se VE pero no AVISA.

Pedido de Tiziano: "está bien que Lucy VEA los calendarios, pero no necesito
recordatorios del calendario — ya el mismo calendario me da recordatorios".

Lo que hay que proteger acá son DOS cosas a la vez, y la segunda es la que
importa de verdad:
  1. Que un evento espejado de Google entre MUDO (anticipos_min = '{}').
  2. Que los recordatorios PROPIOS de Tiziano —sus tareas y las citas que le
     pide a Lucy por Telegram— sigan sonando exactamente igual. Perderlos
     sería mucho peor que el ruido que este cambio vino a quitar.

Y una tercera, silenciosa, que es la que más fácil se rompe sin querer: que el
sync NO pise `anticipos_min` al re-espejar. Si Tiziano pide "recordame 30 min
antes de esa reunión" sobre una cita de Google, esa elección tiene que
sobrevivir al próximo sync — que corre cada pocos minutos. Un DO UPDATE de más
le borraría el recordatorio en silencio.

Herméticos: sin Postgres ni red. Mismo patrón de stubs que test_anticipos.py.

Correr:  python3 tests/test_calendario_no_avisa.py
(o con pytest: pytest tests/test_calendario_no_avisa.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 1) Entorno + stubs ANTES de importar el código real.
# ---------------------------------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("GOOGLE_SA_KEY", "")

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

# httpx: cerebro.calendario lo importa a nivel de módulo. No lo usamos (nunca
# salimos a la red en estos tests), solo tiene que existir.
_httpx = types.ModuleType("httpx")


class _StubAsyncClient:
    def __init__(self, *a, **k):
        pass


_httpx.AsyncClient = _StubAsyncClient
sys.modules.setdefault("httpx", _httpx)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db.db as db  # noqa: E402
from cerebro import calendario, despertador  # noqa: E402


# ---------------------------------------------------------------------------
# 2) Andamios: una conexión falsa que guarda cada SQL con sus parámetros.
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


class FakeConn:
    """Registra los SQL emitidos. `filas` es lo que devuelve el SELECT grande.

    Las filas se sirven UNA sola vez, al primer cursor. `revisar` sigue con sus
    ramas laterales (salidas, briefing, semanal, recurrentes) y esas tienen que
    ver una base vacía: acá probamos los avisos, no ellas.
    """

    def __init__(self, filas=None):
        self.sqls: list[tuple[str, tuple]] = []
        self._filas = filas or []
        self._servidas = False

    def _norm(self, sql):
        return " ".join(sql.split())

    async def execute(self, sql, params=None):
        s = self._norm(sql)
        self.sqls.append((s, params or ()))
        if s.startswith("INSERT INTO log_acciones"):
            return _Cur([(1,)])
        return _Cur()

    def cursor(self, row_factory=None):
        filas, self._servidas = ([] if self._servidas else self._filas), True
        return _CursorConFilas(self, filas)


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
    """Pone la base falsa y calla las ramas laterales de `revisar`.

    guardar_en_bandeja se stubea para que el briefing y el plan semanal no
    dependan de a qué hora se corran los tests.
    """
    db.pool = FakePool(conn)

    async def _nada(*a, **k):
        return None

    db.registrar_aviso = _nada
    db.guardar_en_bandeja = _nada


class _BotFalso:
    """Junta lo que Lucy manda por Telegram, sin salir a la red."""

    def __init__(self):
        self.enviados: list[dict] = []

    async def send_message(self, **kw):
        self.enviados.append(kw)


def _sql_que_empieza(conn, prefijo):
    for s, p in conn.sqls:
        if s.startswith(prefijo):
            return s, p
    raise AssertionError(f"No se emitió ningún SQL que empiece con {prefijo!r}")


# ---------------------------------------------------------------------------
# 3) Un evento de Google entra MUDO
# ---------------------------------------------------------------------------
async def test_evento_de_google_entra_sin_campanadas():
    conn = FakeConn()
    _instalar(conn)
    await calendario._guardar(
        {"id": "cal-x", "nombre": "CDS Sala P"},
        {"id": "ev-1", "summary": "Ensayo Dony",
         "start": {"dateTime": "2026-08-20T21:00:00-04:00"},
         "end": {"dateTime": "2026-08-20T23:00:00-04:00"}},
    )
    s, _ = _sql_que_empieza(conn, "INSERT INTO eventos")
    # La lista vacía es "ninguna campanada": el despertador la saltea sola.
    assert "anticipos_min" in s, "el INSERT tiene que fijar anticipos_min"
    assert "'{}'" in s, f"el INSERT debería fijar anticipos_min = '{{}}': {s}"


async def test_el_resync_no_pisa_un_recordatorio_pedido_a_mano():
    """El DO UPDATE NO puede tocar anticipos_min. Es el error caro del cambio.

    Si Tiziano pidió "recordame 30 min antes de esa reunión", su elección vive
    en esa columna. El sync corre cada pocos minutos: incluirla en el DO UPDATE
    le borraría el recordatorio a los minutos y en silencio.
    """
    conn = FakeConn()
    _instalar(conn)
    await calendario._guardar(
        {"id": "cal-x", "nombre": "CDS Sala P"},
        {"id": "ev-1", "summary": "Ensayo Dony",
         "start": {"dateTime": "2026-08-20T21:00:00-04:00"}},
    )
    s, _ = _sql_que_empieza(conn, "INSERT INTO eventos")
    do_update = s.split("DO UPDATE SET", 1)[1]
    assert "anticipos_min" not in do_update, (
        "anticipos_min NO puede estar en el DO UPDATE: el resync le borraría "
        f"a Tiziano el recordatorio que pidió. SQL: {do_update}")


# ---------------------------------------------------------------------------
# 4) La query del despertador deja fuera lo mudo — en las DOS tablas
# ---------------------------------------------------------------------------
async def test_la_query_filtra_las_filas_sin_campanadas():
    conn = FakeConn(filas=[])
    _instalar(conn)
    await despertador.revisar(_BotFalso())
    s, _ = _sql_que_empieza(conn, "SELECT 'tareas'")
    # Una en cada rama del UNION: tareas y eventos.
    assert s.count("cardinality(anticipos_min) > 0") == 2, (
        "Las dos ramas del UNION necesitan la guarda; si falta la de eventos, "
        f"vuelve el aviso duplicado del calendario. SQL: {s}")


def test_lista_vacia_no_toca_ninguna_campanada():
    # Defensa en profundidad: aunque una fila muda se colara en el SELECT, la
    # decisión pura no le saca ninguna campanada.
    assert despertador._campanadas([], set(), 0) == set()
    assert despertador._campanadas([], set(), -60) == set()
    assert despertador._campanadas([], set(), 30) == set()


async def test_una_fila_muda_no_dispara_aviso_ni_marca_nada():
    """Si una fila con anticipos_min vacío llega igual, el bucle la saltea.

    Sin esto, un `or [0]` de más en el bucle convertiría el silencio deliberado
    en la campanada a la hora — o sea, el ruido de vuelta por la puerta de atrás.
    """
    conn = FakeConn(filas=[{
        "tabla": "eventos", "id": 7, "titulo": "Ensayo Dony",
        "cuando": datetime.now(timezone.utc),
        "avisos_enviados": [], "anticipos_min": [],
    }])
    _instalar(conn)
    bot = _BotFalso()
    n = await despertador.revisar(bot)
    assert bot.enviados == [], f"No debería haber avisado nada: {bot.enviados}"
    assert n == 0
    assert not any(s.startswith("UPDATE eventos SET avisos_enviados")
                   for s, _ in conn.sqls), "no debe marcar campanadas que no sonaron"


# ---------------------------------------------------------------------------
# 5) REGRESIÓN: los recordatorios PROPIOS de Tiziano siguen intactos
# ---------------------------------------------------------------------------
def test_los_recordatorios_propios_siguen_sonando():
    # Una tarea suya con el default {0}: a la hora, suena. Es la mitad del
    # encargo que NO se toca.
    assert despertador._campanadas([0], set(), 0) == {0}
    # Y con anticipo pedido a mano, también.
    assert despertador._campanadas([30, 0], set(), 30) == {30}
    assert despertador._campanadas([1440, 0], set(), 1440) == {1440}


async def test_una_tarea_propia_si_dispara_su_aviso():
    """El caso completo, de punta a punta: la tarea de Tiziano avisa igual."""
    conn = FakeConn(filas=[{
        "tabla": "tareas", "id": 3, "titulo": "Tomar la medicina",
        "cuando": datetime.now(timezone.utc),
        "avisos_enviados": [], "anticipos_min": [0],
    }])
    _instalar(conn)
    bot = _BotFalso()
    n = await despertador.revisar(bot)
    assert n == 1, "la tarea propia de Tiziano TIENE que avisar"
    assert len(bot.enviados) == 1
    assert "Tomar la medicina" in bot.enviados[0]["text"]
    assert any(s.startswith("UPDATE tareas SET avisos_enviados")
               for s, _ in conn.sqls), "y tiene que marcar la campanada"


async def test_una_cita_nativa_de_lucy_si_dispara_su_aviso():
    """Una cita que Tiziano le pidió a Lucy (gcal_id NULL) también avisa.

    Es la distinción del encargo: el silencio es por el ORIGEN del evento, no
    por la tabla. `eventos` guarda las dos cosas.
    """
    conn = FakeConn(filas=[{
        "tabla": "eventos", "id": 9, "titulo": "Dentista",
        "cuando": datetime.now(timezone.utc),
        "avisos_enviados": [], "anticipos_min": [0],
    }])
    _instalar(conn)
    bot = _BotFalso()
    n = await despertador.revisar(bot)
    assert n == 1, "una cita nativa de Lucy TIENE que seguir avisando"
    assert "Dentista" in bot.enviados[0]["text"]


async def test_un_anticipo_pedido_sobre_una_cita_de_google_vuelve_a_sonar():
    """La vuelta atrás por pedido explícito funciona.

    Es lo que hace que el corte sea por ORIGEN del RECORDATORIO y no una
    mordaza: si Tiziano dice "de esa sí avisame", el agente le edita
    anticipos_min y la cita de Google suena como cualquier otra.
    """
    conn = FakeConn(filas=[{
        "tabla": "eventos", "id": 11, "titulo": "Clases Itla",
        "cuando": datetime.now(timezone.utc) + timedelta(minutes=30),
        "avisos_enviados": [], "anticipos_min": [30, 0],
    }])
    _instalar(conn)
    bot = _BotFalso()
    n = await despertador.revisar(bot)
    assert n == 1, "con anticipo pedido a mano, la cita de Google SÍ avisa"
    assert "Clases Itla" in bot.enviados[0]["text"]


_TESTS = [
    test_evento_de_google_entra_sin_campanadas,
    test_el_resync_no_pisa_un_recordatorio_pedido_a_mano,
    test_la_query_filtra_las_filas_sin_campanadas,
    test_lista_vacia_no_toca_ninguna_campanada,
    test_una_fila_muda_no_dispara_aviso_ni_marca_nada,
    test_los_recordatorios_propios_siguen_sonando,
    test_una_tarea_propia_si_dispara_su_aviso,
    test_una_cita_nativa_de_lucy_si_dispara_su_aviso,
    test_un_anticipo_pedido_sobre_una_cita_de_google_vuelve_a_sonar,
]


async def _main():
    fallos = 0
    for t in _TESTS:
        try:
            r = t()
            if asyncio.iscoroutine(r):
                await r
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            fallos += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fallos += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - fallos}/{len(_TESTS)} en verde")
    return fallos


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
