# -*- coding: utf-8 -*-
"""El briefing matinal y el plan semanal, por persona (encargo 1, 22-sep-2026).

Diseño: `disenos/lucy-rosi-independiente/DISENO.md`. Decisión de Tiziano ahí:
"las tareas sin responsable le salen a Tiziano mientras nadie las asigne" —
así que el dueño SIEMPRE es destinatario, y además cada quien tenga al menos
una tarea pendiente con `responsable_chat_id` puesto.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI. Los destinatarios de este archivo
se llaman "Beta" y "Gamma" -- nombres inventados, sin relación con los reales
-- para probar que `_destinatarios_de_tareas` los recoge por DERIVARLOS de
`tareas.responsable_chat_id` y `config.NOMBRES_POR_CHAT`, no porque el código
tenga escrito "Rosi" en algún lado. Si mañana entra una tercera persona a la
casa, este archivo prueba que la maquinaria la recoge igual sin tocar una
línea de producción.

LA FRONTERA, dicha: `_destinatarios_de_tareas` solo mira `tareas`. Las citas
(`eventos`) no tienen ninguna columna de responsable (medido contra
`db/schema.sql:313-336` el 22-sep-2026: el `CREATE TABLE eventos` no trae
nada parecido) -- así que el punto 1 del briefing (las citas de HOY) sigue
siendo el mismo para todos los destinatarios, sin filtrar. Eso no es un
descuido de este encargo: es lo que hay hasta que exista esa columna.

Herméticos: sin Postgres ni red — se stubean psycopg/psycopg_pool/openai
antes de importar el código real, igual que el resto de la suite.

Correr:  python3 -m pytest tests/test_briefing_por_persona.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime

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

import config  # noqa: E402
import db.db as db  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
from cerebro import despertador  # noqa: E402
from config import TZ  # noqa: E402

DUENO = config.CHAT_ID_DUENO      # lo que haya quedado fijado por el proceso;
                                   # NO se asume un valor, porque `config` es un
                                   # singleton y otro archivo de la suite puede
                                   # haberlo importado primero con OTRO CHAT_ID_DUENO
                                   # (mismo patrón que ya usa el resto de la suite).
BETA = 2002
GAMMA = 3003
AJENO = 999999                    # no está en la lista de la casa: no puede ser responsable


def _con_gente():
    """`config.CHAT_IDS_PERMITIDOS` y `NOMBRES_POR_CHAT` de mentira, para esta
    prueba -- MISMO patrón que ya usa `tests/test_botones_rosi.py::_con_gente`
    y `tests/test_responsable.py`, porque `personas_del_panel()` (de la que
    salen `config.puede_ser_responsable` y los nombres) lee estos DOS módulo-
    globales directo, no del entorno en cada llamada. Sin esto, el resultado
    de este archivo dependería de qué otro archivo de la suite importó
    `config` primero y con qué `CHAT_IDS_CASA`/`NOMBRES_POR_CHAT` -- se midió
    corriendo la suite entera: sin este parche, pasa solo, y falla adentro de
    ella.

    Devuelve una función que restaura los originales -- se llama SIEMPRE en
    un `finally`.
    """
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = (DUENO, BETA, GAMMA)
    config.NOMBRES_POR_CHAT = {DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"}

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


def _local(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Un momento en hora de Santo Domingo. `dia` 1..7 de agosto de 2026:
    el 2 es domingo y el 3 es lunes (weekday 6 y 0) -- mismas fechas que usa
    `test_tarifa_deepseek.py`, para no inventar otro calendario."""
    return datetime(2026, 8, dia, hora, minuto, tzinfo=TZ)


# ---------------------------------------------------------------------------
# FakeDB: modela `tareas` y `bandeja`, las DOS tablas que `_destinatarios_de_
# tareas` y `db.destinos_con_encargo_hoy` consultan.
# ---------------------------------------------------------------------------
class FakeDB:
    def __init__(self):
        self.asignados: set[int] = set()          # responsable_chat_id "reales" hoy
        self.encargos: list[tuple[datetime, int, str]] = []  # (cuándo, chat, texto)
        self.ahora = _local(2, 20)

    def connection(self):
        fake = self

        class _Cur:
            def __init__(self, filas):
                self._filas = filas

            async def fetchall(self):
                return self._filas

        class _Conn:
            async def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if "FROM tareas" in s:
                    return _Cur([(c,) for c in fake.asignados])
                assert "FROM bandeja" in s, f"SQL inesperado: {s[:80]}"
                desde = params[2]
                chats = {c for t, c, _ in fake.encargos if t >= desde}
                return _Cur([(c,) for c in chats])

        class _CM:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _CM()

    async def guardar(self, *, tipo_entrada, contenido_raw, chat_id, origen):
        self.encargos.append((self.ahora, chat_id, contenido_raw))
        return len(self.encargos)


def _montar(fake: FakeDB):
    db.pool = fake
    db.guardar_en_bandeja = fake.guardar

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.ahora

    despertador.datetime = _Reloj
    return lambda: setattr(despertador, "datetime", datetime)


def _correr(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1) _destinatarios_de_tareas: derivado de lo real, con dos hermanos
# ---------------------------------------------------------------------------
def test_el_dueno_siempre_esta_aunque_nadie_tenga_tareas():
    fake = FakeDB()
    restaurar = _montar(fake)
    try:
        assert _correr(despertador._destinatarios_de_tareas()) == (DUENO,)
    finally:
        restaurar()


def test_cada_hermano_con_tarea_asignada_entra_sin_estar_tecleado():
    """Beta y Gamma no están escritos en ningún lado de despertador.py: entran
    porque `tareas.responsable_chat_id` los tiene puestos."""
    fake = FakeDB()
    fake.asignados = {BETA, GAMMA}
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        destinatarios = _correr(despertador._destinatarios_de_tareas())
        assert destinatarios == (DUENO, BETA, GAMMA)
    finally:
        restaurar_gente()
        restaurar_db()


def test_un_chat_sin_acceso_no_entra_aunque_tenga_una_tarea():
    """Filtrado por `config.puede_ser_responsable` -- la MISMA puerta que
    decide quién puede QUEDAR asignado. Un chat_id viejo, sin acceso, que
    quedó pegado en una fila vieja no recibe nada."""
    fake = FakeDB()
    fake.asignados = {BETA, AJENO}
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        destinatarios = _correr(despertador._destinatarios_de_tareas())
        assert destinatarios == (DUENO, BETA)
        assert AJENO not in destinatarios
    finally:
        restaurar_gente()
        restaurar_db()


def test_el_dueno_no_se_duplica_si_aparece_como_responsable():
    fake = FakeDB()
    fake.asignados = {DUENO, BETA}
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        destinatarios = _correr(despertador._destinatarios_de_tareas())
        assert destinatarios == (DUENO, BETA)
        assert destinatarios.count(DUENO) == 1
    finally:
        restaurar_gente()
        restaurar_db()


# ---------------------------------------------------------------------------
# 2) _briefing / _semanal: un encargo por destinatario, cada uno con SU filtro
# ---------------------------------------------------------------------------
def test_briefing_deja_un_encargo_por_destinatario_con_su_propio_filtro():
    fake = FakeDB()
    fake.asignados = {BETA}
    fake.ahora = _local(2, 8, 0)   # 8 AM, dentro de la ventana del briefing
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        avisos = _correr(despertador._briefing())
        assert avisos == 2, "el dueño y Beta, ninguno más"
        destinos = {c for _, c, _ in fake.encargos}
        assert destinos == {DUENO, BETA}

        texto_dueno = next(t for _, c, t in fake.encargos if c == DUENO)
        texto_beta = next(t for _, c, t in fake.encargos if c == BETA)

        assert "TIZIANO" not in texto_beta, (
            "el briefing de Beta no puede hablar de Tiziano")
        assert "Beta" in texto_beta
        assert f"EXACTAMENTE {BETA}" in texto_beta, (
            "el filtro tiene que nombrar el chat_id exacto de Beta")
        assert f"NULL (sin asignar) o {DUENO}" not in texto_beta, (
            "el filtro de Beta no puede ser el del dueño (NULL + su chat)")

        assert "TIZIANO" in texto_dueno
        assert f"NULL (sin asignar) o {DUENO}" in texto_dueno, (
            "el dueño tiene que ver también las tareas sin asignar")
        assert "Beta" not in texto_dueno and "Gamma" not in texto_dueno
    finally:
        restaurar_gente()
        restaurar_db()


def test_briefing_candado_es_por_destinatario():
    """Si el dueño YA tiene su briefing de hoy pero Beta no, solo Beta recibe
    uno nuevo -- el candado viejo (un solo `LIMIT 1` global) habría dejado a
    Beta sin nada hasta mañana."""
    fake = FakeDB()
    fake.asignados = {BETA}
    fake.ahora = _local(2, 8, 0)
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        fake.encargos.append(
            (fake.ahora, DUENO, despertador.MARCA_BRIEFING + " Alfa, hoy ..."))
        avisos = _correr(despertador._briefing())
        assert avisos == 1
        assert {c for _, c, _ in fake.encargos} == {DUENO, BETA}
        # Y no vuelve a salir en la misma ventana si se llama de nuevo.
        assert _correr(despertador._briefing()) == 0
    finally:
        restaurar_gente()
        restaurar_db()


def test_semanal_un_encargo_por_destinatario_y_filtro_correcto():
    fake = FakeDB()
    fake.asignados = {GAMMA}
    fake.ahora = _local(2, 20, 5)   # domingo, ventana normal
    restaurar_db = _montar(fake)
    restaurar_gente = _con_gente()
    try:
        avisos = _correr(despertador._semanal())
        assert avisos == 2
        texto_gamma = next(t for _, c, t in fake.encargos if c == GAMMA)
        texto_dueno = next(t for _, c, t in fake.encargos if c == DUENO)
        assert "Gamma" in texto_gamma and f"EXACTAMENTE {GAMMA}" in texto_gamma
        assert "TIZIANO" in texto_dueno
        assert f"NULL (sin asignar) o {DUENO}" in texto_dueno
        assert texto_gamma.startswith(despertador.MARCA_SEMANAL)
        assert texto_dueno.startswith(despertador.MARCA_SEMANAL)
    finally:
        restaurar_gente()
        restaurar_db()


# ---------------------------------------------------------------------------
# 3) interpretar._es_briefing_o_semanal_del_dueno: distingue hermanos
# ---------------------------------------------------------------------------
def test_reconoce_el_briefing_del_dueno():
    fila = {"origen": "despertador", "chat_id": DUENO,
            "contenido_raw": despertador.MARCA_BRIEFING + " Alfa, hoy ..."}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila) is True


def test_reconoce_el_semanal_del_dueno():
    fila = {"origen": "despertador", "chat_id": DUENO,
            "contenido_raw": despertador.MARCA_SEMANAL + " Alfa, que arranca..."}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila) is True


def test_el_briefing_de_beta_no_es_el_del_dueno():
    """Hermano por tipo (mismo origen, mismo prefijo) pero de OTRO chat: no
    tiene que activar la supresión de copia -- copiarle A ÉL su propio
    briefing no tiene sentido, pero la función tiene que decir que esta fila
    no es "la del dueño"."""
    fila = {"origen": "despertador", "chat_id": BETA,
            "contenido_raw": despertador.MARCA_BRIEFING + " Beta, hoy ..."}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila) is False


def test_el_recordatorio_del_dueno_no_se_confunde_con_el_briefing():
    """Hermano por chat (mismo origen, mismo chat_id=dueño) pero OTRO tipo de
    aviso del despertador (un recordatorio de tarea/cita, o el aviso de
    respaldo) -- ninguno de los dos tiene camino propio todavía (este
    encargo es solo briefing y plan semanal), así que tiene que seguir
    copiándose como hasta ahora."""
    fila = {"origen": "despertador", "chat_id": DUENO,
            "contenido_raw": "⏰ Ya es la hora: pagar la luz (03:00 PM)"}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila) is False

    fila_backup = {"origen": "despertador", "chat_id": DUENO,
                   "contenido_raw": db.AVISO_BACKUP_PREFIJO + "\n\n..."}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila_backup) is False


def test_un_encargo_de_otro_origen_con_texto_parecido_no_cuela():
    """Aunque el texto empezara igual, si el ORIGEN no es 'despertador' no es
    uno de estos dos (por ejemplo, algo escrito a mano en el panel)."""
    fila = {"origen": "panel", "chat_id": DUENO,
            "contenido_raw": despertador.MARCA_BRIEFING + " Alfa, hoy ..."}
    assert interpretar._es_briefing_o_semanal_del_dueno(fila) is False


# ---------------------------------------------------------------------------
# 4) End to end: el briefing del dueño no se copia; el de Beta tampoco (va
#    directo a su propio chat, nunca pasa por la puerta de copia_dueno).
# ---------------------------------------------------------------------------
def test_procesar_el_briefing_del_dueno_corre_sin_copiar():
    """`_procesar` envuelve el turno del briefing/semanal del dueño en
    `copia_dueno.sin_copiar()`. Se comprueba mirando el ESTADO del
    contextvar DENTRO de un `agente.atender` de mentira -- no hace falta
    Telegram real."""
    fila = {"id": 1, "chat_id": DUENO, "tipo_entrada": "sistema",
            "origen": "despertador",
            "contenido_raw": despertador.MARCA_BRIEFING + " Alfa, hoy ..."}

    visto = {}

    async def _atender_falso(fila, texto, bot):
        visto["sin_copia"] = copia_dueno._sin_copia.get()

    interpretar.agente.atender = _atender_falso
    try:
        _correr(interpretar._procesar(fila, bot=None))
    finally:
        pass
    assert visto["sin_copia"] is True


def test_procesar_un_recordatorio_del_dueno_sigue_copiando():
    """Un aviso del despertador que NO es briefing/semanal (acá, uno con la
    forma de un recordatorio) tiene que dejar el contextvar en su default
    (False) -- sigue yendo por la copia general hasta que tenga su propio
    camino."""
    fila = {"id": 2, "chat_id": DUENO, "tipo_entrada": "sistema",
            "origen": "despertador",
            "contenido_raw": "⏰ Ya es la hora: pagar la luz (03:00 PM)"}

    visto = {}

    async def _atender_falso(fila, texto, bot):
        visto["sin_copia"] = copia_dueno._sin_copia.get()

    interpretar.agente.atender = _atender_falso
    try:
        _correr(interpretar._procesar(fila, bot=None))
    finally:
        pass
    assert visto["sin_copia"] is False


def test_procesar_el_briefing_de_beta_tampoco_se_marca_sin_copia():
    """El propio briefing de Beta va directo a SU chat (`chat_id=BETA`), que
    no es `config.CHAT_ID_DUENO` -- `copia_dueno` ni se dispara para él, así
    que no hace falta (ni corresponde) envolverlo en `sin_copiar()`."""
    fila = {"id": 3, "chat_id": BETA, "tipo_entrada": "sistema",
            "origen": "despertador",
            "contenido_raw": despertador.MARCA_BRIEFING + " Beta, hoy ..."}

    visto = {}

    async def _atender_falso(fila, texto, bot):
        visto["sin_copia"] = copia_dueno._sin_copia.get()

    interpretar.agente.atender = _atender_falso
    try:
        _correr(interpretar._procesar(fila, bot=None))
    finally:
        pass
    assert visto["sin_copia"] is False


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
