"""Tests de la deduplicación de crear_desde_interpretacion (acciones/crud.py).

Por qué son herméticos (sin Postgres, sin red): el bug era que el agente
re-crea lo que acaba de crear y llegaban recordatorios dobles. Lo que hay que
probar es la DECISIÓN de crear o no crear otra fila, no Postgres. Así que se
stubea psycopg y se corre `crear_desde_interpretacion` contra una conexión de
mentira en memoria (FakeConn) que modela las mismas consultas que emite el
código. No toca ninguna base — de producción, menos.

Correr:  python3 tests/test_crud_dedup.py
(o con pytest si está instalado: pytest tests/test_crud_dedup.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime

# ---------------------------------------------------------------------------
# 1) Entorno mínimo + stubs de psycopg ANTES de importar el código real.
#    config.py exige estas variables; db.db crea el pool al importarse. Nada
#    de esto necesita una base viva para lo que probamos.
# ---------------------------------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

_psycopg = types.ModuleType("psycopg")
_psycopg_rows = types.ModuleType("psycopg.rows")
_psycopg_rows.dict_row = object()  # solo tiene que existir como atributo
_psycopg.rows = _psycopg_rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _psycopg_rows)

_psycopg_pool = types.ModuleType("psycopg_pool")


class _StubPool:  # AsyncConnectionPool(DATABASE_URL, open=False)
    def __init__(self, *a, **k):
        pass


_psycopg_pool.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _psycopg_pool)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db.db as db  # noqa: E402
from acciones import crud  # noqa: E402


# ---------------------------------------------------------------------------
# 2) Conexión de mentira: un Postgres en memoria que entiende exactamente las
#    consultas que emite crud.py (dedup, insert, log). Reimplementa la
#    semántica pretendida (IS NOT DISTINCT FROM para fechas NULL incluido).
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self):
        self.tareas: list[dict] = []
        self.eventos: list[dict] = []
        self.logs: list[dict] = []
        self._ids = {"tareas": 0, "eventos": 0}
        self._logid = 1000

    # -- helpers de siembra (una fila que "ya existía") ---------------------
    def seed_tarea(self, titulo, vence, *, estado="pendiente", borrado_en=None):
        self._ids["tareas"] += 1
        rid = self._ids["tareas"]
        self.tareas.append({"id": rid, "titulo": titulo, "vence_en": vence,
                            "estado": estado, "borrado_en": borrado_en})
        self._logid += 1
        self.logs.append({"id": self._logid, "tabla": "tareas",
                          "registro_id": rid, "accion": "crear"})
        return rid, self._logid

    def seed_evento(self, titulo, inicia, *, borrado_en=None):
        self._ids["eventos"] += 1
        rid = self._ids["eventos"]
        self.eventos.append({"id": rid, "titulo": titulo, "inicia_en": inicia,
                             "borrado_en": borrado_en})
        self._logid += 1
        self.logs.append({"id": self._logid, "tabla": "eventos",
                          "registro_id": rid, "accion": "crear"})
        return rid, self._logid

    # -- el "motor SQL" de mentira -----------------------------------------
    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()

        if s.startswith("SELECT id FROM tareas WHERE borrado_en IS NULL AND estado"):
            titulo, cuando = p
            hits = [t for t in self.tareas
                    if t["borrado_en"] is None and t["estado"] == "pendiente"
                    and t["titulo"] == titulo and t["vence_en"] == cuando]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("SELECT id FROM eventos WHERE borrado_en IS NULL"):
            titulo, inicia = p
            hits = [e for e in self.eventos
                    if e["borrado_en"] is None
                    and e["titulo"] == titulo and e["inicia_en"] == inicia]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("SELECT id FROM log_acciones WHERE tabla"):
            tabla, rid = p
            hits = [l for l in self.logs if l["tabla"] == tabla
                    and l["registro_id"] == rid and l["accion"] == "crear"]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("INSERT INTO tareas"):
            self._ids["tareas"] += 1
            rid = self._ids["tareas"]
            _bandeja, titulo, _detalle, vence = p[0], p[1], p[2], p[3]
            self.tareas.append({"id": rid, "titulo": titulo, "vence_en": vence,
                                "estado": "pendiente", "borrado_en": None})
            return _Cur((rid,))

        if s.startswith("INSERT INTO eventos"):
            self._ids["eventos"] += 1
            rid = self._ids["eventos"]
            titulo, inicia = p[1], p[2]
            self.eventos.append({"id": rid, "titulo": titulo, "inicia_en": inicia,
                                 "borrado_en": None})
            return _Cur((rid,))

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            lid = self._logid
            accion, tabla, registro_id = p[0], p[1], p[2]
            self.logs.append({"id": lid, "tabla": tabla,
                              "registro_id": registro_id, "accion": accion})
            return _Cur((lid,))

        raise AssertionError(f"SQL no modelado por FakeConn: {s[:90]}")


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
    """Deja a crud.py hablando con la conexión de mentira."""
    db.pool = FakePool(conn)

    async def _cero_persona(_):
        return None

    async def _cero_proyecto(_):
        return None

    db.buscar_o_crear_persona = _cero_persona
    db.buscar_o_crear_proyecto = _cero_proyecto


# ---------------------------------------------------------------------------
# 3) Tests
# ---------------------------------------------------------------------------
async def test_tarea_mismo_titulo_misma_fecha_no_crea_segunda():
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    rid0, log0 = conn.seed_tarea("Comprar café", vence)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Comprar café",
            "cuando": "2026-08-01T10:00:00"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 1, "no debió crear una segunda fila"
    assert rid == rid0, "debió devolver la tarea que ya existía"
    assert log_id == log0, "debió devolver el log de la creación original"


async def test_tarea_mismo_titulo_fecha_distinta_si_crea():
    conn = FakeConn()
    _instalar(conn)
    conn.seed_tarea("Pagar la luz",
                    datetime.fromisoformat("2026-08-01T09:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Pagar la luz",
            "cuando": "2026-09-01T09:00:00"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 2, "misma etiqueta, otra fecha = otra tarea"
    assert rid != conn.tareas[0]["id"]
    assert log_id > 1000, "una creación real escribe un log nuevo"


async def test_tarea_sin_fecha_dedup_por_null():
    # IS NOT DISTINCT FROM: dos tareas sin fecha son la misma; no se duplican.
    conn = FakeConn()
    _instalar(conn)
    rid0, log0 = conn.seed_tarea("Llamar al banco", None)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al banco"})

    assert len(conn.tareas) == 1
    assert (rid, log_id) == (rid0, log0)


async def test_tarea_borrada_no_bloquea():
    # Una fila soft-deleteada no cuenta como duplicado: se crea de nuevo.
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    conn.seed_tarea("Renovar dominio", vence,
                    borrado_en=datetime.fromisoformat("2026-07-01T00:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Renovar dominio",
            "cuando": "2026-08-01T10:00:00"})

    assert len(conn.tareas) == 2, "la borrada no debe frenar una nueva"


async def test_tarea_hecha_no_bloquea():
    # Solo 'pendiente' dedup: una tarea ya 'hecha' no impide recrearla.
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    conn.seed_tarea("Sacar la basura", vence, estado="hecha")

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Sacar la basura",
            "cuando": "2026-08-01T10:00:00"})

    assert len(conn.tareas) == 2


async def test_cita_mismo_titulo_mismo_inicio_no_crea_segunda():
    conn = FakeConn()
    _instalar(conn)
    inicia = datetime.fromisoformat("2026-08-02T15:00:00")
    rid0, log0 = conn.seed_evento("Dentista", inicia)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Dentista",
            "cuando": "2026-08-02T15:00:00"})

    assert tabla == "eventos"
    assert len(conn.eventos) == 1, "no debió crear una segunda cita"
    assert (rid, log_id) == (rid0, log0)


async def test_cita_mismo_titulo_inicio_distinto_si_crea():
    conn = FakeConn()
    _instalar(conn)
    conn.seed_evento("Reunión de equipo",
                     datetime.fromisoformat("2026-08-02T15:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Reunión de equipo",
            "cuando": "2026-08-09T15:00:00"})

    assert tabla == "eventos"
    assert len(conn.eventos) == 2, "otra hora de inicio = otra cita"


def _sincrono(fn):
    """Envuelve un test síncrono para el runner, que espera corrutinas."""
    async def envuelto():
        return fn()
    envuelto.__name__ = fn.__name__
    return envuelto


def test_el_monto_manual_no_pasa_por_float():
    """El camino manual —cuando Tiziano le dice a Lucy "gasté 500"— era el ÚNICO
    sitio del sistema donde el dinero pasaba por float. El automático ya usaba
    Decimal de punta a punta.

    Postgres redondea al guardar en NUMERIC(12,2), así que hoy no se ve nada
    raro. El punto es que el error entra ANTES de guardar, y una cifra torcida
    que nace de un redondeo no deja rastro de dónde salió.
    """
    from decimal import Decimal
    from acciones.crud import _monto_exacto
    assert _monto_exacto("1234.56") == Decimal("1234.56")
    assert isinstance(_monto_exacto(500), Decimal)
    # Positivo SIEMPRE: el signo lo da `tipo`, y guardarlo dos veces es cómo se
    # termina restando un ingreso.
    assert _monto_exacto(-500) == Decimal("500")
    assert _monto_exacto("0.10") * 3 == Decimal("0.30")


def test_un_monto_ilegible_no_se_guarda_como_cero():
    """Tragarse la basura como 0.00 anotaría un gasto de cero pesos que nadie
    entendería después. Si el clasificador manda algo que no es un número, eso
    es un fallo suyo y tiene que verse."""
    from acciones.crud import _monto_exacto
    for basura in ("quinientos", "", None, "12,50,30"):
        try:
            _monto_exacto(basura)
            assert False, f"{basura!r} no debería haber pasado"
        except ValueError:
            pass


_TESTS = [
    test_tarea_mismo_titulo_misma_fecha_no_crea_segunda,
    test_tarea_mismo_titulo_fecha_distinta_si_crea,
    test_tarea_sin_fecha_dedup_por_null,
    test_tarea_borrada_no_bloquea,
    test_tarea_hecha_no_bloquea,
    test_cita_mismo_titulo_mismo_inicio_no_crea_segunda,
    test_cita_mismo_titulo_inicio_distinto_si_crea,
    # Los dos de abajo son síncronos; el runner hace `await t()`, así que se
    # envuelven acá en vez de volverlos async por una razón de plomería.
    _sincrono(test_el_monto_manual_no_pasa_por_float),
    _sincrono(test_un_monto_ilegible_no_se_guarda_como_cero),
]


async def _main():
    fallos = 0
    for t in _TESTS:
        try:
            await t()
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
