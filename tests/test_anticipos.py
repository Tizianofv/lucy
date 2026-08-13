"""Tests de los recordatorios configurables (anticipos_min).

Cubren las tres piezas del cambio del 30-jul (default = un solo aviso a la hora;
anticipado opt-in por fila):
  · crud._anticipos: normalización (garantiza el 0, dedupe, orden, basura).
  · crud.crear_desde_interpretacion: qué anticipos_min termina en el INSERT.
  · crud.editar: el MISMO invariante por el otro camino (13-ago-2026). Editar
    era el agujero: "recordámelo 30 min antes" sobre algo que ya existe guardaba
    {30} sin el 0, y esa fila no volvía a sonar a la hora. Acá se ata, junto con
    su excepción: la lista VACÍA sigue significando silencio (los eventos
    espejados de Google), y la normalización NO puede convertirla en {0}.
  · despertador._campanadas: la decisión pura de qué campanada suena ahora,
    incluida la ventana por-fila (un {1440,0} dispara a tiempo, un {0} no se
    adelanta) y la fila vieja (que llega con {0} por el default de la columna).

Herméticos: sin Postgres ni red. Se stubean psycopg y openai antes de importar
el código real, igual que test_crud_dedup.py.

Correr:  python3 tests/test_anticipos.py
(o con pytest: pytest tests/test_anticipos.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime

# ---------------------------------------------------------------------------
# 1) Entorno + stubs ANTES de importar el código real.
# ---------------------------------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
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

# openai: despertador importa cerebro.deepseek, que CONSTRUYE un AsyncOpenAI al
# importarse. No lo usamos; solo tiene que existir y no reventar.
_openai = types.ModuleType("openai")


class _StubAsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _StubAsyncOpenAI
sys.modules.setdefault("openai", _openai)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db.db as db  # noqa: E402
from acciones import crud  # noqa: E402
from cerebro import despertador  # noqa: E402


# ---------------------------------------------------------------------------
# 2) FakeConn: modela solo lo que crear_desde_interpretacion emite para una
#    tarea/cita sin duplicado previo. Captura el anticipos_min del INSERT.
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self):
        self._id = 0
        self._logid = 1000
        self.anticipos_insertados: list | None = None

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()

        # Sin duplicado previo: dedup y su lookup de log devuelven None.
        if s.startswith("SELECT id FROM tareas WHERE borrado_en IS NULL"):
            return _Cur(None)
        if s.startswith("SELECT id FROM eventos WHERE borrado_en IS NULL"):
            return _Cur(None)
        if s.startswith("SELECT id FROM log_acciones WHERE tabla"):
            return _Cur(None)

        if s.startswith("INSERT INTO tareas"):
            self._id += 1
            self.anticipos_insertados = p[7]  # …, persona_id, anticipos_min
            return _Cur((self._id,))

        if s.startswith("INSERT INTO eventos"):
            self._id += 1
            self.anticipos_insertados = p[8]  # …, notas, anticipos_min
            return _Cur((self._id,))

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            return _Cur((self._logid,))

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
    db.pool = FakePool(conn)

    async def _cero(_):
        return None

    db.buscar_o_crear_persona = _cero
    db.buscar_o_crear_proyecto = _cero


# ---------------------------------------------------------------------------
# 3) Normalización (crud._anticipos)
# ---------------------------------------------------------------------------
def test_default_sin_campo_es_single_a_la_hora():
    # El default = un solo recordatorio a la hora exacta.
    assert crud._anticipos(None) == [0]
    assert crud._anticipos([]) == [0]


def test_treinta_min_antes():
    assert crud._anticipos([30]) == [30, 0]      # el 0 se agrega solo
    assert crud._anticipos([30, 0]) == [30, 0]   # ya venía completo


def test_normaliza_dedup_y_orden():
    # dedupe + orden descendente (anticipado primero, la hora al final).
    assert crud._anticipos([0, 60, 60, 30]) == [60, 30, 0]
    assert crud._anticipos([1440, 0]) == [1440, 0]


def test_basura_no_roba_la_campanada_a_la_hora():
    # Negativos y no-números se descartan; el 0 sobrevive siempre.
    assert crud._anticipos([-5, "x", None]) == [0]
    assert crud._anticipos(["30", 60]) == [60, 30, 0]  # strings numéricas valen


def test_escalar_suelto_es_un_anticipo_no_una_lista_de_digitos():
    # Un "30" sin corchetes es UN anticipo. Leído como iterable daría [3, 0]:
    # basura silenciosa justo donde el helper promete que no la hay.
    assert crud._anticipos(30) == [30, 0]
    assert crud._anticipos("30") == [30, 0]


def test_vacio_es_silencio_solo_cuando_se_pide():
    # Al CREAR, vacío = "no me dijeron nada" = el default, que suena.
    assert crud._anticipos([]) == [0]
    # Al EDITAR, vacío = una decisión: esta fila no avisa nunca. Es como están
    # los 48 eventos espejados de Google desde el 13-ago; volverlos a [0] sería
    # reencender exactamente lo que se apagó.
    assert crud._anticipos([], vacio_es_silencio=True) == []
    assert crud._anticipos((), vacio_es_silencio=True) == []
    # Un null no es una lista vacía: no es una decisión, es un dato que falta.
    # Gana el invariante (y la columna es NOT NULL, así que [] tampoco serviría
    # de excusa para escribir NULL ahí).
    assert crud._anticipos(None, vacio_es_silencio=True) == [0]
    # Y con contenido, la excepción no cambia nada: el 0 se garantiza igual.
    assert crud._anticipos([30], vacio_es_silencio=True) == [30, 0]


# ---------------------------------------------------------------------------
# 4) Qué anticipos_min llega al INSERT (crud.crear_desde_interpretacion)
# ---------------------------------------------------------------------------
async def test_crear_tarea_default_inserta_0():
    conn = FakeConn()
    _instalar(conn)
    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Comprar café",
            "cuando": "2026-08-01T10:00:00"})
    assert conn.anticipos_insertados == [0]


async def test_crear_tarea_con_anticipo_inserta_30_0():
    conn = FakeConn()
    _instalar(conn)
    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al banco",
            "cuando": "2026-08-01T10:00:00", "anticipos_min": [30]})
    assert conn.anticipos_insertados == [30, 0]


async def test_crear_cita_con_dia_antes_inserta_1440_0():
    conn = FakeConn()
    _instalar(conn)
    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Dentista",
            "cuando": "2026-08-02T15:00:00", "anticipos_min": [1440, 0]})
    assert conn.anticipos_insertados == [1440, 0]


# ---------------------------------------------------------------------------
# 4bis) El mismo invariante por el camino de EDITAR (crud.editar)
#
# El agujero del 13-ago: `editar` armaba el UPDATE con los cambios crudos, así
# que un {"anticipos_min": [30]} entraba sin pasar por _anticipos. Estos tests
# miran lo único que importa — qué valor llega a la columna en el UPDATE.
# ---------------------------------------------------------------------------
class _CurEditar:
    """Cursor de la fila: sirve los SELECT * (antes y después del UPDATE)."""

    def __init__(self, conn):
        self._conn = conn
        self._row = None

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if not s.startswith("SELECT * FROM"):
            raise AssertionError(f"SQL no modelado por _CurEditar: {s[:90]}")
        self._row = dict(self._conn.fila)
        return self

    async def fetchone(self):
        return self._row


class FakeConnEditar:
    """Modela lo justo para crud.editar sobre UNA fila viva.

    Guarda el UPDATE como dict columna→valor (`campos_guardados`): es la única
    pregunta de estos tests, y mirar el SQL crudo ataría el test a la forma de
    la cadena en vez de a la decisión.
    """

    def __init__(self, fila):
        self.fila = dict(fila)
        self.campos_guardados: dict | None = None
        self._logid = 2000

    def cursor(self, row_factory=None):
        return _CurEditar(self)

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()

        if s.startswith("UPDATE"):
            asignaciones = s.split(" SET ", 1)[1].split(" WHERE ")[0]
            columnas = [a.split("=")[0].strip() for a in asignaciones.split(", ")]
            self.campos_guardados = dict(zip(columnas, p))
            self.fila.update(self.campos_guardados)  # el SELECT de después lo ve
            return _Cur(None)

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            return _Cur((self._logid,))

        raise AssertionError(f"SQL no modelado por FakeConnEditar: {s[:90]}")


def _fila_tarea(**extra):
    fila = {"id": 69, "bandeja_id": 1, "titulo": "Cambiar el bombillo",
            "detalle": None, "vence_en": datetime(2026, 8, 20, 14, 0),
            "estado": "pendiente", "recurrencia": None, "pospuesta_veces": 0,
            "anticipos_min": [0], "avisos_enviados": [], "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, 9, 0)}
    fila.update(extra)
    return fila


def _fila_evento(**extra):
    fila = {"id": 12, "bandeja_id": None, "titulo": "Sesión Sala A",
            "inicia_en": datetime(2026, 8, 20, 9, 0), "termina_en": None,
            "lugar": None, "notas": None, "anticipos_min": [],
            "avisos_enviados": [], "gcal_id": "abc123", "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, 9, 0)}
    fila.update(extra)
    return fila


async def test_editar_30_min_antes_no_pierde_la_campanada_a_la_hora():
    # EL AGUJERO. "Recordámelo 30 minutos antes" sobre algo que ya existe es
    # una edición: antes guardaba [30] y esa tarea no volvía a sonar a la hora.
    conn = FakeConnEditar(_fila_tarea())
    _instalar(conn)
    await crud.editar("tareas", 69, {"anticipos_min": [30]}, motivo="test")
    assert conn.campos_guardados["anticipos_min"] == [30, 0]


async def test_editar_normaliza_dedup_y_orden():
    conn = FakeConnEditar(_fila_tarea())
    _instalar(conn)
    await crud.editar("tareas", 69, {"anticipos_min": [0, 60, 60, 30]}, motivo="t")
    assert conn.campos_guardados["anticipos_min"] == [60, 30, 0]


async def test_editar_basura_no_roba_la_campanada_a_la_hora():
    conn = FakeConnEditar(_fila_tarea())
    _instalar(conn)
    await crud.editar("tareas", 69, {"anticipos_min": [-5, "x"]}, motivo="t")
    assert conn.campos_guardados["anticipos_min"] == [0]


async def test_editar_no_convierte_el_silencio_de_google_en_campanada():
    # La contracara, y es la que puede doler: un evento espejado de Google vive
    # con anticipos_min = [] a propósito (13-ago). Apagarlo a mano tiene que
    # seguir siendo posible; normalizar a [0] reencendería los 48 avisos
    # duplicados que Tiziano pidió apagar.
    conn = FakeConnEditar(_fila_evento(anticipos_min=[30, 0]))
    _instalar(conn)
    await crud.editar("eventos", 12, {"anticipos_min": []}, motivo="t")
    assert conn.campos_guardados["anticipos_min"] == []


async def test_editar_enciende_una_cita_de_google_si_la_pide():
    # El camino inverso, el que promete el prompt del agente: sobre una cita de
    # Google muda, editar {"anticipos_min": [30]} la vuelve a encender — con su
    # 0 incluido, como cualquier otra fila.
    conn = FakeConnEditar(_fila_evento())
    _instalar(conn)
    await crud.editar("eventos", 12, {"anticipos_min": [30]}, motivo="t")
    assert conn.campos_guardados["anticipos_min"] == [30, 0]


async def test_editar_no_inventa_anticipos_en_otras_ediciones():
    # editar es genérico para ocho tablas: la normalización toca SOLO esa
    # columna y solo cuando viene. Un cambio de título no puede arrastrar un
    # anticipos_min al UPDATE.
    conn = FakeConnEditar(_fila_tarea(anticipos_min=[30]))
    _instalar(conn)
    await crud.editar("tareas", 69, {"titulo": "Otro título"}, motivo="t")
    assert conn.campos_guardados == {"titulo": "Otro título"}


# ---------------------------------------------------------------------------
# 5) Selección de campanada por-fila (despertador._campanadas)
# ---------------------------------------------------------------------------
def test_lookahead_por_fila_dispara_1440_a_tiempo():
    # Un {1440,0}: a 24h (faltan=1440) YA debe sonar la campanada del día antes.
    assert despertador._campanadas([1440, 0], set(), 1440) == {1440}


def test_default_0_no_se_adelanta():
    # Un {0}: a 24h no suena nada; solo cuando llega la hora (faltan<=0).
    assert despertador._campanadas([0], set(), 1440) == set()
    assert despertador._campanadas([0], set(), 0) == {0}


def test_fila_vieja_default_0_sigue_funcionando():
    # Una fila anterior a la columna llega con {0} (default de la migración):
    # se comporta como single a la hora, sin campanada anticipada.
    vieja = [0]
    assert despertador._campanadas(vieja, set(), 30) == set()   # 30' antes: nada
    assert despertador._campanadas(vieja, set(), 0) == {0}      # a la hora: suena


def test_campanada_ya_enviada_no_repite():
    # La de 1440 ya salió; a la hora solo queda la del 0.
    assert despertador._campanadas([1440, 0], {1440}, 0) == {0}
    assert despertador._campanadas([1440, 0], {1440, 0}, 0) == set()


def test_lucy_caida_toca_varias_de_una():
    # Estuvo caída y el evento pasó (faltan negativo): tocan ambas juntas; el
    # bucle manda un solo mensaje y las da todas por sonadas.
    assert despertador._campanadas([30, 0], set(), -10) == {30, 0}


_TESTS = [
    test_default_sin_campo_es_single_a_la_hora,
    test_treinta_min_antes,
    test_normaliza_dedup_y_orden,
    test_basura_no_roba_la_campanada_a_la_hora,
    test_escalar_suelto_es_un_anticipo_no_una_lista_de_digitos,
    test_vacio_es_silencio_solo_cuando_se_pide,
    test_crear_tarea_default_inserta_0,
    test_crear_tarea_con_anticipo_inserta_30_0,
    test_crear_cita_con_dia_antes_inserta_1440_0,
    test_editar_30_min_antes_no_pierde_la_campanada_a_la_hora,
    test_editar_normaliza_dedup_y_orden,
    test_editar_basura_no_roba_la_campanada_a_la_hora,
    test_editar_no_convierte_el_silencio_de_google_en_campanada,
    test_editar_enciende_una_cita_de_google_si_la_pide,
    test_editar_no_inventa_anticipos_en_otras_ediciones,
    test_lookahead_por_fila_dispara_1440_a_tiempo,
    test_default_0_no_se_adelanta,
    test_fila_vieja_default_0_sigue_funcionando,
    test_campanada_ya_enviada_no_repite,
    test_lucy_caida_toca_varias_de_una,
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
