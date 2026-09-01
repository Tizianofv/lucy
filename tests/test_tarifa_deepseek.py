"""Tests de la regla de tarifa doble de DeepSeek (1-ago-2026).

Regla de Tiziano: ningún proceso AUTOMÁTICO que gaste DeepSeek puede correr en
las franjas de tarifa doble — 21:00–00:00 y 02:00–06:00, hora de Santo Domingo.
Lo conversacional no se toca.

Cubren:
  · config.es_horario_caro_deepseek: los ocho bordes exactos, y la trampa de
    recibir el momento en UTC en vez de en hora local.
  · despertador._toca_semanal: que el plan semanal quedó a las 20:00, que ya no
    dispara a las 21:00, y que la guarda dura gana aunque las constantes digan
    otra cosa.
  · Que diferir NO pierde trabajo: el domingo perdido se rescata el lunes
    temprano, y el dedupe impide que salga dos veces.
  · Las exenciones: recordatorios y 911 no dependen del horario.

Herméticos: sin Postgres ni red. Se stubean psycopg y openai antes de importar
el código real, igual que test_anticipos.py.

Correr:  python3 tests/test_tarifa_deepseek.py
(o con pytest: pytest tests/test_tarifa_deepseek.py)
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

import config  # noqa: E402
import db.db as db  # noqa: E402
from cerebro import despertador  # noqa: E402
from config import TZ  # noqa: E402

caro = config.es_horario_caro_deepseek


def _local(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Un momento en hora de Santo Domingo. `dia` = 1..7 de agosto de 2026.

    Agosto 2026: el 2 es domingo y el 3 es lunes (weekday 6 y 0).
    """
    return datetime(2026, 8, dia, hora, minuto, tzinfo=TZ)


# ---------------------------------------------------------------------------
# 2) Los bordes exactos del helper (hora LOCAL)
# ---------------------------------------------------------------------------
def test_borde_2059_es_barato():
    assert caro(_local(1, 20, 59)) is False


def test_borde_2100_es_caro():
    assert caro(_local(1, 21, 0)) is True


def test_borde_2359_es_caro():
    assert caro(_local(1, 23, 59)) is True


def test_borde_medianoche_es_barato():
    # La franja cierra a las 00:00: la medianoche ya no paga doble.
    assert caro(_local(2, 0, 0)) is False


def test_borde_0159_es_barato():
    assert caro(_local(2, 1, 59)) is False


def test_borde_0200_es_caro():
    assert caro(_local(2, 2, 0)) is True


def test_borde_0559_es_caro():
    assert caro(_local(2, 5, 59)) is True


def test_borde_0600_es_barato():
    assert caro(_local(2, 6, 0)) is False


def test_mediodia_y_tarde_baratos():
    # El grueso del día: nada de tarifa doble entre las 6 AM y las 9 PM.
    for h in range(6, 21):
        assert caro(_local(1, h, 30)) is False, f"las {h}:30 no deberían ser caras"


# ---------------------------------------------------------------------------
# 3) La trampa UTC-4: el mismo instante, contado en UTC
# ---------------------------------------------------------------------------
def test_utc_se_convierte_no_se_compara_crudo():
    # 21:00 en Santo Domingo = 01:00 UTC del día siguiente. Comparar la hora
    # UTC (1) contra las franjas daría False — barato — justo cuando empieza
    # lo caro. El helper tiene que convertir primero.
    en_utc = datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)
    assert en_utc.astimezone(TZ).hour == 21   # sanidad del supuesto
    assert caro(en_utc) is True


def test_utc_borde_inverso():
    # 06:00 UTC = 02:00 local → caro, aunque la hora UTC (6) sea barata local.
    assert caro(datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)) is True
    # 00:00 UTC = 20:00 local del día anterior → barato, aunque 0 UTC "parezca"
    # medianoche (que también es barata, pero por otro motivo).
    assert caro(datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)) is False


def test_naive_se_asume_local():
    # Sin zona se toma tal cual: es lo que devuelve datetime.now(TZ) sin tz.
    assert caro(datetime(2026, 8, 1, 22, 0)) is True
    assert caro(datetime(2026, 8, 1, 20, 0)) is False


def test_sin_argumento_no_revienta():
    # El uso real es sin argumento (usa el reloj). Solo verificamos el contrato.
    assert isinstance(caro(), bool)


# ---------------------------------------------------------------------------
# 4) El plan semanal quedó a las 20:00
# ---------------------------------------------------------------------------
def test_domingo_agosto_2026_es_el_dia_correcto():
    # Sanidad de las fechas que usan los tests de abajo.
    assert _local(2, 12).weekday() == despertador.SEMANAL_DIA          # domingo
    assert _local(3, 12).weekday() == despertador.SEMANAL_RESCATE_DIA  # lunes


def test_constantes_del_semanal():
    assert despertador.SEMANAL_DESDE == 20
    assert despertador.SEMANAL_HASTA == 21


def test_el_encargo_tiene_su_hueco_y_lo_llena():
    # Si alguien reescribe el encargo y se lleva puesto el {cuando}, format()
    # no revienta (deja el texto igual) pero el domingo diría "arranca hoy".
    assert "{cuando}" in despertador.ENCARGO_SEMANAL
    assert "arranca mañana lunes" in despertador._encargo_semanal(False)
    assert "arranca hoy lunes" in despertador._encargo_semanal(True)


def test_semanal_dispara_a_las_20():
    assert despertador._toca_semanal(_local(2, 20, 0)) is True
    assert despertador._toca_semanal(_local(2, 20, 59)) is True


def test_semanal_ya_no_dispara_a_las_21():
    # El horario viejo. Es el cambio entero del pedido.
    assert despertador._toca_semanal(_local(2, 21, 0)) is False
    assert despertador._toca_semanal(_local(2, 23, 30)) is False


def test_semanal_no_se_adelanta_a_la_tarde():
    assert despertador._toca_semanal(_local(2, 19, 59)) is False


def test_semanal_ninguna_ventana_del_semanal_es_cara():
    # La propiedad que importa, dicha directamente: no hay un solo minuto en el
    # que el plan semanal pueda salir pagando doble.
    for dia in (2, 3):           # domingo y lunes
        for h in range(24):
            for m in (0, 30, 59):
                t = _local(dia, h, m)
                if despertador._toca_semanal(t):
                    assert not caro(t), f"el semanal saldría caro el {dia} a las {h}:{m}"


def test_guarda_dura_gana_a_las_constantes():
    # Si mañana alguien devuelve las constantes al horario caro, la guarda
    # tiene que frenarlo igual. Esa es su razón de ser.
    original = despertador.SEMANAL_DESDE, despertador.SEMANAL_HASTA
    despertador.SEMANAL_DESDE, despertador.SEMANAL_HASTA = 21, 24
    try:
        assert despertador._toca_semanal(_local(2, 22, 0)) is False
    finally:
        despertador.SEMANAL_DESDE, despertador.SEMANAL_HASTA = original


# ---------------------------------------------------------------------------
# 5) Diferir NO es perder: el rescate del lunes
# ---------------------------------------------------------------------------
def test_rescate_del_lunes_temprano():
    assert despertador._toca_semanal(_local(3, 6, 0)) is True
    assert despertador._toca_semanal(_local(3, 8, 59)) is True


def test_rescate_no_arranca_antes_de_las_6():
    # 05:59 del lunes todavía es tarifa doble.
    assert despertador._toca_semanal(_local(3, 5, 59)) is False


def test_rescate_no_pisa_el_briefing():
    assert despertador._toca_semanal(_local(3, 9, 0)) is False


def test_otros_dias_no_disparan():
    for dia in (4, 5, 6, 7):  # martes a viernes
        for h in (7, 12, 20):
            assert despertador._toca_semanal(_local(dia, h)) is False


def test_dedupe_cubre_domingo_20_y_lunes_6():
    # El rescate es un día calendario DISTINTO al del envío normal. Si el
    # dedupe siguiera cortando por medianoche, el plan saldría dos veces.
    distancia_h = (_local(3, 6) - _local(2, 20)).total_seconds() / 3600
    assert distancia_h == 10
    assert despertador.SEMANAL_DEDUPE_H >= distancia_h


def test_dedupe_no_alcanza_a_la_semana_pasada():
    # Y no tanto como para tapar el plan del domingo anterior (168 h).
    assert despertador.SEMANAL_DEDUPE_H < 168


# ---------------------------------------------------------------------------
# 6) El ciclo diferido no pierde trabajo — end to end sobre _semanal()
# ---------------------------------------------------------------------------
class FakeDB:
    """Modela la bandeja para _semanal: qué encargos hay y cuándo se guardaron."""

    def __init__(self):
        self.encargos: list[tuple[datetime, str]] = []
        self.ahora = _local(2, 20)

    # — lo que _semanal consulta —
    def connection(self):
        fake = self

        class _Cur:
            async def fetchone(self):
                return fake._hay_reciente()

        class _Conn:
            async def execute(self, sql, params=None):
                s = " ".join(sql.split())
                assert "FROM bandeja" in s, f"SQL inesperado: {s[:80]}"
                fake._desde = params[0]
                return _Cur()

        class _CM:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _CM()

    def _hay_reciente(self):
        return 1 if any(t >= self._desde for t, _ in self.encargos) else None

    async def guardar(self, *, tipo_entrada, contenido_raw, chat_id, origen):
        self.encargos.append((self.ahora, contenido_raw))
        return len(self.encargos)


def _montar(fake: FakeDB):
    """Enchufa el FakeDB y hace que datetime.now(TZ) devuelva fake.ahora."""
    db.pool = fake
    db.guardar_en_bandeja = fake.guardar

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.ahora

    despertador.datetime = _Reloj
    return lambda: setattr(despertador, "datetime", datetime)


async def test_domingo_normal_deja_un_encargo():
    fake = FakeDB()
    restaurar = _montar(fake)
    try:
        fake.ahora = _local(2, 20, 5)
        assert await despertador._semanal() == 1
        # Y no repite en la misma ventana.
        fake.ahora = _local(2, 20, 40)
        assert await despertador._semanal() == 0
        assert len(fake.encargos) == 1
        assert "mañana lunes" in fake.encargos[0][1]
    finally:
        restaurar()


async def test_lo_diferido_no_se_pierde():
    """Lucy caída toda la ventana del domingo: el plan sale el lunes, una vez."""
    fake = FakeDB()
    restaurar = _montar(fake)
    try:
        # Domingo 21:00 a 23:30 (tarifa doble): no sale nada. Ese es el diferimiento.
        for h, m in ((21, 0), (22, 30), (23, 30)):
            fake.ahora = _local(2, h, m)
            assert await despertador._semanal() == 0
        # Madrugada del lunes, todavía cara o fuera de ventana: sigue sin salir.
        for h in (0, 3, 5):
            fake.ahora = _local(3, h, 0)
            assert await despertador._semanal() == 0
        assert fake.encargos == []

        # 06:00 del lunes: primera ventana barata → el trabajo aparece, no se perdió.
        fake.ahora = _local(3, 6, 0)
        assert await despertador._semanal() == 1
        assert len(fake.encargos) == 1
        # Y el texto se adaptó: el lunes la semana ya arrancó.
        assert "hoy lunes" in fake.encargos[0][1]
        assert "mañana lunes" not in fake.encargos[0][1]

        # No vuelve a salir en el resto de la ventana de rescate.
        fake.ahora = _local(3, 8, 30)
        assert await despertador._semanal() == 0
        assert len(fake.encargos) == 1
    finally:
        restaurar()


async def test_el_domingo_que_salio_no_se_duplica_el_lunes():
    """El caso que rompía el dedupe viejo: salió el domingo, ¿sale otra vez?"""
    fake = FakeDB()
    restaurar = _montar(fake)
    try:
        fake.ahora = _local(2, 20, 10)
        assert await despertador._semanal() == 1
        fake.ahora = _local(3, 6, 0)          # ventana de rescate, día distinto
        assert await despertador._semanal() == 0
        assert len(fake.encargos) == 1
    finally:
        restaurar()


async def test_a_la_semana_siguiente_vuelve_a_salir():
    """El dedupe no puede volverse un silencio permanente."""
    fake = FakeDB()
    restaurar = _montar(fake)
    try:
        fake.ahora = _local(2, 20, 10)
        assert await despertador._semanal() == 1
        fake.ahora = _local(2, 20, 10) + timedelta(days=7)   # domingo siguiente
        assert await despertador._semanal() == 1
        assert len(fake.encargos) == 2
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 7) Las exenciones: lo que NO se difiere
# ---------------------------------------------------------------------------
def test_recordatorios_no_dependen_del_horario():
    # _campanadas es la decisión de los recordatorios y no mira el reloj de
    # tarifa: no gasta IA (el texto se arma en Python) y aplazar una alarma la
    # vacía de sentido. Si alguien le mete la guarda, esto se pone rojo.
    assert despertador._campanadas([0], set(), 0) == {0}
    assert despertador._campanadas([30, 0], set(), 30) == {30}


def test_el_helper_no_se_coló_en_el_camino_conversacional():
    # Interpretar un mensaje de Tiziano NUNCA puede quedar esperando a que baje
    # la tarifa. Este test cuida la frontera leyendo el código: si mañana
    # aparece la guarda en el agente o en el bucle de interpretación, avisa.
    import pathlib
    raiz = pathlib.Path(_ROOT)
    for archivo in ("cerebro/agente.py", "cerebro/interpretar.py",
                    "acciones/botones.py", "cerebro/whisper.py",
                    "cerebro/vision.py"):
        texto = (raiz / archivo).read_text(encoding="utf-8")
        assert "es_horario_caro_deepseek" not in texto, (
            f"{archivo} está en el camino conversacional: no puede diferir por tarifa")


_TESTS_BASE = [
    test_borde_2059_es_barato,
    test_borde_2100_es_caro,
    test_borde_2359_es_caro,
    test_borde_medianoche_es_barato,
    test_borde_0159_es_barato,
    test_borde_0200_es_caro,
    test_borde_0559_es_caro,
    test_borde_0600_es_barato,
    test_mediodia_y_tarde_baratos,
    test_utc_se_convierte_no_se_compara_crudo,
    test_utc_borde_inverso,
    test_naive_se_asume_local,
    test_sin_argumento_no_revienta,
    test_domingo_agosto_2026_es_el_dia_correcto,
    test_constantes_del_semanal,
    test_el_encargo_tiene_su_hueco_y_lo_llena,
    test_semanal_dispara_a_las_20,
    test_semanal_ya_no_dispara_a_las_21,
    test_semanal_no_se_adelanta_a_la_tarde,
    test_semanal_ninguna_ventana_del_semanal_es_cara,
    test_guarda_dura_gana_a_las_constantes,
    test_rescate_del_lunes_temprano,
    test_rescate_no_arranca_antes_de_las_6,
    test_rescate_no_pisa_el_briefing,
    test_otros_dias_no_disparan,
    test_dedupe_cubre_domingo_20_y_lunes_6,
    test_dedupe_no_alcanza_a_la_semana_pasada,
    test_domingo_normal_deja_un_encargo,
    test_lo_diferido_no_se_pierde,
    test_el_domingo_que_salio_no_se_duplica_el_lunes,
    test_a_la_semana_siguiente_vuelve_a_salir,
    test_recordatorios_no_dependen_del_horario,
    test_el_helper_no_se_coló_en_el_camino_conversacional,
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


def test_ningun_cliente_de_ia_puede_quedarse_esperando_para_siempre():
    """El bucle de interpretación procesa los mensajes DE UNO EN UNO, así que
    una sola llamada colgada congela a Lucy entera: no contesta a nadie, y no
    hay error ni log que lo diga porque está esperando, no fallando.

    Pasó el 1-sep con el "Dame el panel" de Rosi. El turno estuvo siete minutos
    sin producir un solo paso, con el proceso vivo y el panel respondiendo — que
    desde fuera se ve idéntico a "Lucy está rota".
    """
    import os
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for archivo in ("deepseek.py", "vision.py", "whisper.py", "memoria.py"):
        fuente = open(os.path.join(raiz, "cerebro", archivo),
                      encoding="utf-8").read()
        i = fuente.find("AsyncOpenAI(")
        assert i > 0, f"{archivo}: no encuentro el cliente"
        argumentos = fuente[i:fuente.index(")", i)]
        assert "timeout=" in argumentos, (
            f"{archivo}: el cliente no tiene timeout — una llamada colgada "
            "congelaría el bucle entero")


_TESTS = _TESTS_BASE + [test_ningun_cliente_de_ia_puede_quedarse_esperando_para_siempre]


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
