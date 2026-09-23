# -*- coding: utf-8 -*-
"""La alerta 911 de correo, directo a los dos (encargo "alerta 911 directo a
los dos", 22-sep-2026).

Decisión de Tiziano, textual, a «cuando apaguemos la copia general, ¿a
quién le llega?»: «A los dos». Hoy (antes de este encargo) el aviso iba
SOLO al dueño (`captura/correo.py:1135`, `chat_id=config.CHAT_ID_DUENO`) y
le llegaba a Rosi nada más que por la copia general (`COPIAS_DEL_DUENO`,
`cerebro/copia_dueno.py`), que la sala va a apagar después de este trabajo.

QUÉ TIENE QUE CUMPLIR, medido acá:
  1. La alerta le llega DIRECTO a cada persona de `config.personas_del_panel()`
     -- no una lista tecleada -- con el mismo resguardo del dueño-solo que ya
     usa `despertador._destinatarios_de_tareas` cuando esa lista viene vacía.
  2. No le llega dos veces a nadie MIENTRAS LA COPIA GENERAL SIGUE ENCENDIDA:
     el encargo del dueño lleva `correo.MARCA_911`, que
     `cerebro.interpretar._es_encargo_propio_del_dueno` reconoce para apagar
     la copia en su turno (`cerebro/copia_dueno.sin_copiar`).
  3. El candado de "ya se avisó" es POR DESTINO: avisarle a Tiziano no le
     puede robar el aviso a Rosi, y al revés -- se mide con las funciones
     REALES `db.correos_ya_reportados`/`db.marcar_correo_reportado` contra
     SQLite (el mismo patrón, ya validado, de
     `tests/test_correo_directo_a_los_dos.py`), no una reimplementación.

CÓMO SE PRUEBA EL CAMINO DE PRODUCCIÓN, de punta a punta:
  · `correo.vigilar_911` corre TAL CUAL contra un IMAP de mentira (mismo
    arnés que `tests/test_correo_no_descarta_callado.py`) y contra
    `db.correos_ya_reportados`/`marcar_correo_reportado` REALES en SQLite.
    `db.guardar_en_bandeja` se dobla (solo anota qué fila se creó: no hay
    tabla `bandeja` en este arnés SQLite, y no hace falta para lo que se
    mide acá).
  · Las filas de bandeja que deja `vigilar_911` se procesan con
    `cerebro.interpretar._procesar`, LA FUNCIÓN REAL que decide si apaga la
    copia (`_es_encargo_propio_del_dueno`, sin doblar) y que instala el
    parche REAL de `cerebro.copia_dueno` sobre `telegram.Bot.send_message`
    (mismo arnés que `tests/test_copia_a_rosi.py::puerta`: un
    `telegram.Bot(...)` REAL, con `send_message` doblado ANTES de instalar
    el parche, así que "la función real" que el parche envuelve es el doble
    y no sale nada a la red). Solo se dobla `agente.atender` -- llamar al
    LLM de verdad no es parte de esta garantía -- por un stand-in que hace
    lo mínimo que el agente real termina haciendo con un encargo de sistema:
    mandarle su texto al chat que le tocó.

HERMANOS: `config.personas_del_panel()` es LA MISMA fuente que ya usan
`despertador._destinatarios_de_tareas` (briefing/plan semanal),
`captura.correo.reporte_diario`/`_encargo` (el reporte diario) y
`captura.correo.vigilar_911` (acá) -- no una copia con criterio propio.
`_con_gente`/`_con_acceso_separado` de abajo distinguen `NOMBRES_POR_CHAT`
(tener nombre) de `CHAT_IDS_PERMITIDOS` (tener acceso), igual que exige el
patrón ya establecido en `tests/test_citas_google.py` para no repetir el
NO PASA del 22-sep-2026 (una prueba que arme las dos tuplas siempre iguales
no puede ver a alguien con nombre pero sin acceso).

DOS COSAS QUE ESTE ENCARGO NO RESUELVE Y QUE VAN EN EL REPORTE, NO ACÁ
(archivo y línea, ver el reporte): que `cuentas_de_correo("mostrar")` no
filtra por `destinos_del_reporte(cuenta)` como sí hace `reporte_diario`, y
que el texto del encargo no personaliza "él" por destinatario. Ninguna
prueba de este archivo depende de esas dos cosas ni las tapa: se miden
tal como están hoy.

Correr:  python3 -m pytest tests/test_alerta_911_a_los_dos.py -q
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-911")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "111")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")


class _Cualquiera:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Cualquiera()

    def __call__(self, *a, **k):
        return _Cualquiera()


for _n, _attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object}),
                   ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                   ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera})):
    _m = types.ModuleType(_n)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    _m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[_n] = _m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import telegram  # noqa: E402

import captura.correo as correo  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO       # 111
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


def _con_acceso_separado(nombres: dict[int, str], permitidos: tuple[int, ...]):
    """Deja EXISTIR a alguien con nombre pero sin acceso -- la frontera que
    `tests/test_citas_google.py::_con_acceso_separado` ya defendió contra
    exactamente este NO PASA el 22-sep-2026."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = tuple(permitidos)
    config.NOMBRES_POR_CHAT = dict(nombres)

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---------------------------------------------------------------------------
# El arnés IMAP -- mismo patrón que tests/test_correo_no_descarta_callado.py
# ---------------------------------------------------------------------------
def _eml(de: str, asunto: str, cuerpo: str = "se cayó algo") -> bytes:
    return (f"From: {de}\r\n"
            f"Subject: {asunto}\r\n"
            f"Date: Fri, 05 Sep 2026 08:00:00 -0400\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{cuerpo}\r\n").encode()


class _IMAPFalso:
    def __init__(self, buzon):
        self.buzon = list(buzon)

    def login(self, usuario, clave):
        return ("OK", [b"logueado"])

    def select(self, carpeta, readonly=False):
        return ("OK", [str(len(self.buzon)).encode()])

    def uid(self, orden, *args):
        if orden == "search":
            return ("OK", [b" ".join(str(u).encode() for u, _ in self.buzon)])
        if orden == "fetch":
            uid, pieza = int(args[0]), args[1]
            for u, crudo in self.buzon:
                if u != uid:
                    continue
                if "HEADER" in pieza:
                    crudo = crudo.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                return ("OK", [(b"1 (UID x {n})", crudo)])
            return ("OK", [None])
        raise AssertionError(f"orden IMAP inesperada: {orden}")

    def logout(self):
        return ("BYE", [b"chao"])


CUENTA = {"user": "railway-avisos@ejemplo.com", "pass": "x"}
ALERTA = _eml("Railway <team@railway.app>", "Build failed for lucy")


def _montar_imap(buzon):
    correo.imaplib = types.SimpleNamespace(
        IMAP4_SSL=lambda servidor, puerto: _IMAPFalso(buzon))
    config.CORREO_CUENTAS = [CUENTA]


# ---------------------------------------------------------------------------
# `db.correos_ya_reportados`/`db.marcar_correo_reportado` REALES, contra
# SQLite -- copiado del arnés ya validado de
# tests/test_correo_directo_a_los_dos.py (columnas sacadas de db/schema.sql).
# ---------------------------------------------------------------------------
_TABLA = """
    CREATE TABLE correo_reportado (
        cuenta TEXT NOT NULL, uid INTEGER NOT NULL, reportado_en TEXT,
        nivel TEXT, ambito TEXT, area TEXT, asunto TEXT,
        bandeja_id INTEGER, leido_en TEXT, destino_chat_id INTEGER,
        UNIQUE(cuenta, uid, destino_chat_id)
    )
"""


def _traducir(sql: str, params: tuple):
    import re
    m = re.search(r"=\s*ANY\(%s\)", sql)
    if m:
        indice = sql[:m.start()].count("%s")
        valores = params[indice]
        marcas = ", ".join("?" * len(valores))
        sql = sql[:m.start()] + f"IN ({marcas})" + sql[m.end():]
        params = params[:indice] + tuple(valores) + params[indice + 1:]
    return sql.replace("%s", "?"), params


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
    def __init__(self, con):
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
    def __init__(self, con):
        self._conn = _ConnSQLite(con)

    def connection(self):
        return _PoolCM(self._conn)


def _instalar_sqlite():
    con = sqlite3.connect(":memory:")
    con.execute(_TABLA)
    db.pool = _PoolSQLite(con)
    return con


class _BandejaFalsa:
    """Solo lo que `vigilar_911` necesita de la bandeja: no hay tabla
    `bandeja` en este arnés (el candado que se mide es el de
    `correo_reportado`, real, en SQLite -- ver arriba)."""

    def __init__(self):
        self.filas: list[dict] = []

    async def guardar_en_bandeja(self, **kw):
        fila = dict(kw)
        fila["id"] = len(self.filas) + 1
        self.filas.append(fila)
        return fila["id"]

    async def listar_preferencias(self):
        return []


def _clasificar_no_usado(c, r=""):
    raise AssertionError("vigilar_911 no debería clasificar nada con DeepSeek")


# ---------------------------------------------------------------------------
# 1) A quién le llega: personas_del_panel(), con su resguardo de dueño-solo.
# ---------------------------------------------------------------------------
def test_le_llega_a_cada_uno_de_personas_del_panel():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"})
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        avisados = _correr(correo.vigilar_911(None))
        destinos = sorted(f["chat_id"] for f in bandeja.filas)
        assert destinos == sorted([DUENO, BETA, GAMMA]), (
            f"esperaba un encargo para cada uno de personas_del_panel() "
            f"({sorted([DUENO, BETA, GAMMA])}), salió para {destinos}")
        assert avisados == 3
        assert all(f["origen"] == "correo" for f in bandeja.filas)
    finally:
        restaurar()
        con.close()


def test_sin_nombres_puestos_el_resguardo_es_el_dueno_solo():
    """`NOMBRES_POR_CHAT` vacía (como en producción hasta que se cargue):
    `personas_del_panel()` da (), y el último resguardo -- el MISMO idioma
    que `despertador._destinatarios_de_tareas` -- es el dueño solo. Una 911
    NUNCA se queda sin avisarle a nadie."""
    restaurar = _con_gente({})
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        avisados = _correr(correo.vigilar_911(None))
        assert avisados == 1
        assert bandeja.filas[0]["chat_id"] == DUENO
    finally:
        restaurar()
        con.close()


def test_alguien_con_nombre_pero_sin_acceso_no_recibe_la_911():
    """LA FRONTERA: "Beta" tiene nombre pero no acceso -- no puede ser un
    destino de la alerta, porque `personas_del_panel()` exige las dos
    cosas. Sin esta prueba, nada distingue resolver por
    `personas_del_panel()` (correcto) de leer `NOMBRES_POR_CHAT` directo."""
    restaurar = _con_acceso_separado({DUENO: "Alfa", BETA: "Beta"},
                                      permitidos=(DUENO,))  # Beta NO entra
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        avisados = _correr(correo.vigilar_911(None))
        assert avisados == 1
        assert [f["chat_id"] for f in bandeja.filas] == [DUENO]
    finally:
        restaurar()
        con.close()


# ---------------------------------------------------------------------------
# 2) El candado es POR DESTINO -- funciones REALES contra SQLite.
# ---------------------------------------------------------------------------
def test_avisarle_a_uno_no_le_roba_el_aviso_al_otro():
    """Al dueño ya se le había avisado (una vuelta anterior); a Beta,
    todavía no. La vuelta de ahora tiene que avisarle a Beta y NO
    duplicarle el aviso al dueño."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    con = _instalar_sqlite()
    _correr(db.marcar_correo_reportado(
        CUENTA["user"], 1, destino=DUENO, nivel="911", asunto="x"))
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        avisados = _correr(correo.vigilar_911(None))
        assert avisados == 1
        assert [f["chat_id"] for f in bandeja.filas] == [BETA]
        filas_reportado = con.execute(
            "SELECT destino_chat_id FROM correo_reportado "
            "WHERE cuenta=? AND uid=1", (CUENTA["user"],)).fetchall()
        assert sorted(r[0] for r in filas_reportado) == sorted([DUENO, BETA])
    finally:
        restaurar()
        con.close()


def test_una_vez_avisados_los_dos_la_proxima_vuelta_no_avisa_a_nadie():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        primera = _correr(correo.vigilar_911(None))
        assert primera == 2
        bandeja.filas.clear()
        segunda = _correr(correo.vigilar_911(None))
        assert segunda == 0
        assert bandeja.filas == []
    finally:
        restaurar()
        con.close()


# ---------------------------------------------------------------------------
# 3) Marca correcta, y de punta a punta: no llega dos veces con la copia
#    general encendida -- `_procesar`/`_es_encargo_propio_del_dueno`/
#    `copia_dueno` REALES, `telegram.Bot.send_message` doblado.
# ---------------------------------------------------------------------------
def test_el_encargo_del_dueno_lleva_la_marca_911():
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    try:
        _correr(correo.vigilar_911(None))
        del_dueno = [f for f in bandeja.filas if f["chat_id"] == DUENO]
        assert len(del_dueno) == 1
        assert del_dueno[0]["contenido_raw"].startswith(correo.MARCA_911)
    finally:
        restaurar()
        con.close()


@pytest.fixture
def puerta():
    """Mismo arnés que tests/test_copia_a_rosi.py::puerta: `telegram.Bot.
    send_message` doblado ANTES de instalar el parche real de
    `copia_dueno`, así que ninguna llamada sale a la red y el parche
    envuelve al doble."""
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


async def _atender_falso(fila, texto, bot):
    """Stand-in de `cerebro.agente.atender`: lo único que le importa a esta
    garantía es a qué chat termina llegando el mensaje, no qué escribe el
    LLM -- así que manda el propio texto del encargo, tal cual, al chat de
    la fila (que es, en sustancia, lo que hace el agente real con un
    encargo de sistema: contárselo a quien se lo dejaron)."""
    await bot.send_message(chat_id=fila["chat_id"], text=texto)


def test_no_llega_dos_veces_con_la_copia_general_encendida(puerta, monkeypatch):
    """LA GARANTÍA CENTRAL. Con COPIAS_DEL_DUENO="Beta" (la copia general
    sigue encendida, como hoy hasta que la sala la apague): el correo 911
    genera DOS encargos (dueño y Beta, directo); se procesan los DOS con
    `interpretar._procesar` real. Beta tiene que aparecer en `enviados`
    UNA sola vez -- ni la copia general duplicó lo que ya le había llegado
    directo, ni el envío directo se perdió."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ("Beta",)  # la copia general, todavía encendida
    con = _instalar_sqlite()
    bandeja = _BandejaFalsa()
    db.guardar_en_bandeja = bandeja.guardar_en_bandeja
    db.listar_preferencias = bandeja.listar_preferencias
    correo.clasificar = _clasificar_no_usado
    _montar_imap([(1, ALERTA)])
    monkeypatch.setattr(interpretar.agente, "atender", _atender_falso)
    try:
        avisados = _correr(correo.vigilar_911(None))
        assert avisados == 2
        bot = telegram.Bot(token="123456:token-de-prueba")
        for fila in bandeja.filas:
            _correr(interpretar._procesar(fila, bot))
        a_dueno = [e for e in puerta if e["chat_id"] == DUENO]
        a_beta = [e for e in puerta if e["chat_id"] == BETA]
        assert len(a_dueno) == 1, f"el dueño recibió {len(a_dueno)} veces"
        assert len(a_beta) == 1, (
            f"Beta recibió {len(a_beta)} veces (directo + copia general "
            "duplicaría esto a 2)")
    finally:
        restaurar()
        config._NOMBRES_DE_COPIA = ()
        con.close()


def test_sin_la_marca_911_la_copia_general_si_duplicaria(puerta, monkeypatch):
    """LA PAREJA del test anterior, mismo ataque al revés: si el encargo del
    dueño NO llevara `MARCA_911` (comportamiento de ANTES de este encargo),
    `_es_encargo_propio_del_dueno` no lo reconoce, la copia general sigue
    encendida y Beta SÍ lo recibe dos veces -- prueba que el arnés de arriba
    sabe detectar la duplicación cuando de verdad ocurre, no solo cuando no
    ocurre."""
    restaurar = _con_gente({DUENO: "Alfa", BETA: "Beta"})
    config._NOMBRES_DE_COPIA = ("Beta",)
    monkeypatch.setattr(interpretar.agente, "atender", _atender_falso)
    try:
        fila_dueno = {"id": 1, "tipo_entrada": "sistema",
                      "contenido_raw": "Sin marca, como antes del encargo.",
                      "chat_id": DUENO, "origen": "correo"}
        fila_beta = {"id": 2, "tipo_entrada": "sistema",
                     "contenido_raw": "Sin marca, como antes del encargo.",
                     "chat_id": BETA, "origen": "correo"}
        bot = telegram.Bot(token="123456:token-de-prueba")
        _correr(interpretar._procesar(fila_dueno, bot))
        _correr(interpretar._procesar(fila_beta, bot))
        a_beta = [e for e in puerta if e["chat_id"] == BETA]
        assert len(a_beta) == 2, (
            "sin MARCA_911 la copia general tenía que duplicarle el aviso "
            f"a Beta (directo + copia); recibió {len(a_beta)}")
    finally:
        restaurar()
        config._NOMBRES_DE_COPIA = ()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
