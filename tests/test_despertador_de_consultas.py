# -*- coding: utf-8 -*-
"""El despertador de las consultas de Natalia (18-oct-2026, pieza L4 de `disenos/natalia-tareas-en-proyectos/DISENO.md`):
la tarjeta al responsable, el recordatorio a los 30 minutos y el cierre de una `nueva_fecha` cuando la fecha se arregla.

CAMINO DE PRODUCCIÓN: `despertador.revisar_consultas_de_natalia` REAL, las funciones REALES de `db/db.py` ejecutando su
SQL de verdad contra SQLite en memoria (`%s` -> `?`), con la tabla armada con el DDL REAL de
`db/migrations/2026-10-18_consultas_de_natalia.sql`; el panel REAL (`POST /proyectos/tarea/{tid}/fecha` -> `db.mover_vence`)
y Telegram REAL (`crud.editar`, la puerta del chat) para mover la fecha; y un `telegram.Bot` REAL, con `copia_dueno`
instalada encima. Lo ÚNICO que se sustituye es la red: `Bot.send_message` y `Bot.edit_message_text` a nivel de CLASE (como
`tests/test_copia_a_rosi.py`), con un doble que puede fallar, devolver nada o devolver un mensaje sin número, y que mira
en qué estado está la consulta EN EL MOMENTO de enviar. El reloj (`hoy`, `ahora`) se pasa a la función.

FRONTERA: SQLite no es Postgres (no entiende `ANY`, no hay FK; los tipos son laxos); lo que se prueba es que el texto del SQL y
sus parámetros dicen lo que dicen. `tools/humo.py` corre estas lecturas contra Postgres de verdad. `telegram.Bot` no es el
`ExtBot` que arma `main.py` (otra firma de `send_message`): la puerta de la copia es la misma (`Bot.send_message`).

NINGÚN nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_despertador_de_consultas.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import pathlib
import re
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-despertador-consultas")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object()
_psycopg.rows = _rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
_pool_mod = types.ModuleType("psycopg_pool")
_pool_mod.AsyncConnectionPool = type("_StubPool", (), {"__init__": lambda *a, **k: None})
sys.modules.setdefault("psycopg_pool", _pool_mod)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
import telegram  # noqa: E402

import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.despertador as despertador  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
from acciones import crud  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

UTC = timezone.utc
CLIENTE_HTTP = TestClient(panel.app)
MIGRACION = _ROOT / "db" / "migrations" / "2026-10-18_consultas_de_natalia.sql"

A, B = 5001, 5002                                  # fichas de clientes inventadas
ANA, BETO, ROSI = 7710001, 7710002, 7710003        # chats inventados
HOY = date(2026, 3, 10)                            # el "hoy" de Santo Domingo de estas pruebas (fijo, en el pasado)
AHORA = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)   # 11:00 en Santo Domingo
SIN_PANEL = "(sin enlace)"


def sd(dia: str, hora: str = "12:00") -> datetime:
    """Un instante dicho en hora de Santo Domingo: `sd("2026-03-05", "18:30")`."""
    return datetime.fromisoformat(f"{dia}T{hora}").replace(tzinfo=config.TZ)


# ── La base: SQLite con las tablas que esto toca; la de consultas con el DDL REAL ───────────────────────────

sqlite3.register_converter(
    "TSTZ", lambda b: datetime.fromisoformat(b.decode()).astimezone(UTC))


class _SinTabla(Exception):
    """Lo que psycopg lanza si la tabla no existe: `UndefinedTable`, SQLSTATE 42P01 (comprobado contra psycopg 3.2.3)."""
    sqlstate = "42P01"


class _Fila(dict):
    """Una fila como la de `dict_row`, que además se puede indexar por posición (`fila[0]`, como una tupla)."""

    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else super().__getitem__(k)


class _Cur:
    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []
        self.rowcount = 0

    async def execute(self, sql, params=None):
        await asyncio.sleep(0)
        pasos = tuple(p.isoformat() if isinstance(p, datetime) else p for p in (params or ()))
        sql = sql.replace("%s", "?").replace("IS NOT DISTINCT FROM", "IS").replace("now()", "CURRENT_TIMESTAMP")
        try:
            cur = self._con.execute(sql, pasos)
        except sqlite3.OperationalError as e:
            if "no such table: consultas_de_natalia" in str(e):
                raise _SinTabla(str(e)) from None
            raise
        self.rowcount = cur.rowcount
        self._filas = [_Fila(f) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Conn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _Cur(self._con)

    async def execute(self, sql, params=None):
        return await _Cur(self._con).execute(sql, params)

    def transaction(self):
        class _T:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *e):
                return False
        return _T()


class _Pool:
    def __init__(self, con):
        self.con = con

    def connection(self):
        conn = _Conn(self.con)

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _ddl_de_la_migracion() -> list[str]:
    texto = "\n".join(l.split("--")[0] for l in MIGRACION.read_text(encoding="utf-8").splitlines())
    sentencias = []
    for s in texto.split(";"):
        s = " ".join(s.split())
        if not s or s.upper() in ("BEGIN", "COMMIT"):
            continue
        sentencias.append(s.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                           .replace("TIMESTAMPTZ", "TSTZ").replace("BIGINT", "INTEGER")
                           .replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"))
    return sentencias


ESQUEMA = """
CREATE TABLE proyectos (
  id INTEGER PRIMARY KEY, nombre TEXT, descripcion TEXT, estado TEXT DEFAULT 'activo',
  area TEXT, creado_en TSTZ, responsable_chat_id INTEGER, cliente_nombre TEXT,
  cliente_noco_id INTEGER, borrado_en TSTZ);
CREATE TABLE tareas (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, area TEXT, titulo TEXT, estado TEXT,
  vence_en TSTZ, completado_en TSTZ, creado_en TSTZ, responsable_chat_id INTEGER,
  borrado_en TSTZ, grave INTEGER NOT NULL DEFAULT 0, pospuesta_veces INTEGER DEFAULT 0, bandeja_id INTEGER);
CREATE TABLE participantes (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, tarea_id INTEGER, noco_id INTEGER,
  nombre TEXT, rol TEXT, borrado_en TSTZ);
CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER);
CREATE TABLE log_acciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT, tabla TEXT, registro_id INTEGER,
  antes TEXT, despues TEXT, motivo TEXT, bandeja_id INTEGER);
CREATE TABLE bandeja (
  id INTEGER PRIMARY KEY AUTOINCREMENT, origen TEXT, tipo_entrada TEXT, chat_id INTEGER, estado TEXT,
  respuesta_lucy TEXT, procesado_en TSTZ);
"""

NOMBRES_DE_CLIENTE = {A: "Cliente Alfa", B: "Cliente Beta"}
# (id, nombre, estado, responsable, cliente)
PROYECTOS = [
    (1, "Proyecto Alfa", "activo", ANA, A),
    (2, "Proyecto Gamma", "activo", BETO, B),
]
# (id, proyecto, titulo, estado, vence)
TAREAS = [
    (11, 1, "Mezclar el tema", "pendiente", sd("2026-03-20")),
    (12, 1, "Grabar las voces", "pendiente", sd("2026-03-05")),       # vencida: la que sigue
    (13, 1, "Tarea sin fecha", "pendiente", None),
    (50, 2, "Tarea de otro cliente", "pendiente", sd("2026-03-04")),
]


def _base(con_tabla=True):
    con = sqlite3.connect(":memory:", check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    if con_tabla:
        for s in _ddl_de_la_migracion():
            con.execute(s)
    for pid, nombre, estado, resp, cliente in PROYECTOS:
        con.execute(
            "INSERT INTO proyectos (id, nombre, estado, responsable_chat_id, cliente_nombre, cliente_noco_id, "
            "creado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, nombre, estado, resp, NOMBRES_DE_CLIENTE[cliente], cliente, AHORA.isoformat()))
    for tid, pid, titulo, estado, vence in TAREAS:
        con.execute(
            "INSERT INTO tareas (id, proyecto_id, titulo, estado, vence_en, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, pid, titulo, estado, vence.isoformat() if vence else None, AHORA.isoformat()))
    con.commit()
    return con


# ── La red de mentira: `Bot.send_message` y `Bot.edit_message_text` de la CLASE ─────────────────────────────

class Red:
    """Lo que Telegram "recibió", y cómo se porta. `modo`: "ok" (devuelve un mensaje con número), "falla" (lanza),
    "nada" (devuelve `None`), "sin_numero" (devuelve un mensaje sin `message_id`)."""

    def __init__(self):
        self.modo = "ok"
        self.modo_edicion = "ok"
        self.enviados: list[dict] = []
        self.editados: list[dict] = []
        self.con = None
        self.estado_al_enviar: list[str | None] = []

    def a(self, chat):
        return [e for e in self.enviados if e["chat_id"] == chat]


@pytest.fixture
def mundo():
    """Claves, personas, la base armada, el bot real con la red doblada y la copia al dueño instalada. Restaura todo."""
    guardado = {k: getattr(config, k) for k in ("NOMBRES_POR_CHAT", "CHAT_IDS_PERMITIDOS", "_NOMBRES_DE_COPIA", "PANEL_URL")}
    pool0 = db.pool
    envio0, edicion0 = telegram.Bot.send_message, telegram.Bot.edit_message_text
    dueno = config.CHAT_ID_DUENO
    config.NOMBRES_POR_CHAT = {ANA: "Ana Inventada", BETO: "Beto Inventado", ROSI: "Rosi Inventada",
                               dueno: "Dueno Inventado"}
    config.CHAT_IDS_PERMITIDOS = (ANA, BETO, ROSI, dueno)
    config._NOMBRES_DE_COPIA = ()
    config.PANEL_URL = ""
    red = Red()

    async def _envio(self, chat_id, text, **kwargs):
        if red.con is not None:
            fila = red.con.execute("SELECT estado FROM consultas_de_natalia ORDER BY id DESC LIMIT 1").fetchone()
            red.estado_al_enviar.append(fila["estado"] if fila else None)
        red.enviados.append({"chat_id": chat_id, "text": text, **kwargs})
        if red.modo == "falla":
            raise telegram.error.NetworkError("sin red de mentira")
        if red.modo == "nada":
            return None
        if red.modo == "sin_numero":
            return types.SimpleNamespace()
        return types.SimpleNamespace(message_id=1000 + len(red.enviados))

    async def _edicion(self, chat_id=None, message_id=None, text=None, **kwargs):
        red.editados.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        if red.modo_edicion == "falla":
            raise telegram.error.NetworkError("sin red de mentira")
        return True

    telegram.Bot.send_message = _envio
    telegram.Bot.edit_message_text = _edicion
    copia_dueno.instalar()
    red.bot = telegram.Bot(token="123456:token-de-prueba")

    def poner_base(**kw):
        con = _base(**kw)
        db.pool = _Pool(con)
        red.con = con
        return con
    red.poner_base = poner_base
    red.dueno = dueno
    try:
        yield red
    finally:
        copia_dueno.desinstalar()
        telegram.Bot.send_message, telegram.Bot.edit_message_text = envio0, edicion0
        db.pool = pool0
        for k, v in guardado.items():
            setattr(config, k, v)


def correr(coro):
    return asyncio.run(coro)


def vuelta(red, hoy=HOY, ahora=AHORA):
    return correr(despertador.revisar_consultas_de_natalia(red.bot, hoy=hoy, ahora=ahora))


def abrir_tarea_nueva(clave="k-1", proyecto=1, titulo="Subir el demo", pedido="El cliente quiere el demo el viernes",
                      vence="", responsable=None, noco=A):
    return correr(db.abrir_consulta_de_natalia(noco, clave, "tarea_nueva", proyecto_id=proyecto, titulo=titulo,
                                               pedido=pedido, vence=vence, responsable_chat_id=responsable, hoy=HOY))


def abrir_nueva_fecha(clave="f-1", tarea=12, noco=A):
    return correr(db.abrir_consulta_de_natalia(noco, clave, "nueva_fecha", tarea_id=tarea, hoy=HOY))


def vence_de(con, tid):
    """El `vence_en` de una tarea tal como lo guardó el escritor (la base lo devuelve ya como instante)."""
    return con.execute("SELECT vence_en FROM tareas WHERE id = ?", (tid,)).fetchone()[0]


def consulta(con, cid=1):
    return dict(con.execute("SELECT * FROM consultas_de_natalia WHERE id = ?", (cid,)).fetchone())


def sembrar(con, estado, *, tipo="tarea_nueva", clave="s-1", resp=ANA, enviada_en=None, recordada_en=None,
            tarjeta=555, tarea=None, proyecto=1, cliente=A):
    if tipo == "nueva_fecha":
        titulo = pedido = None
    else:
        titulo, pedido = "Titulo sembrado", "Pedido sembrado"
    cur = con.execute(
        "INSERT INTO consultas_de_natalia (clave, tipo, estado, cliente_noco_id, proyecto_id, titulo, pedido, "
        "tarea_id, vence_vencida_en, responsable_chat_id, tarjeta_msg_id, enviada_en, recordada_en) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clave, tipo, estado, cliente, proyecto, titulo, pedido, tarea,
         sd("2026-03-05").isoformat() if tipo == "nueva_fecha" else None, resp,
         tarjeta if estado != "abierta" else None,
         enviada_en.isoformat() if enviada_en else None, recordada_en.isoformat() if recordada_en else None))
    con.commit()
    return cur.lastrowid


# ═══════════════════════════════════════════════════════════════════════
# G1 · «preguntada» solo si Telegram devolvió el mensaje
# ═══════════════════════════════════════════════════════════════════════

def test_G1_con_telegram_sano_la_tarjeta_sale_y_la_consulta_queda_preguntada(mundo):
    con = mundo.poner_base()
    abrir_tarea_nueva()
    assert vuelta(mundo) == 1
    c = consulta(con)
    assert (c["estado"], c["tarjeta_msg_id"]) == ("preguntada", 1001)
    assert c["enviada_en"] == AHORA
    assert [e["chat_id"] for e in mundo.enviados] == [ANA]


@pytest.mark.parametrize("modo", ["falla", "nada", "sin_numero"])
def test_G1_si_telegram_no_devuelve_el_mensaje_la_consulta_sigue_abierta_y_se_reintenta(mundo, modo):
    con = mundo.poner_base()
    abrir_tarea_nueva()
    mundo.modo = modo
    assert vuelta(mundo) == 0
    c = consulta(con)
    assert (c["estado"], c["tarjeta_msg_id"], c["enviada_en"]) == ("abierta", None, None)
    assert len(mundo.enviados) == 1                      # lo intentó
    # la vuelta siguiente, con Telegram sano, la manda: nada se perdió por el fallo
    mundo.modo = "ok"
    assert vuelta(mundo) == 1
    assert consulta(con)["estado"] == "preguntada"
    assert len(mundo.enviados) == 2


def test_G1_en_el_momento_de_enviar_la_consulta_todavia_esta_abierta(mundo):
    """Aunque Telegram conteste bien, marcar ANTES de enviar es el defecto: se mira el estado DENTRO del envío."""
    con = mundo.poner_base()
    abrir_tarea_nueva()
    vuelta(mundo)
    assert mundo.estado_al_enviar == ["abierta"]
    assert consulta(con)["estado"] == "preguntada"


def test_G1_una_falla_no_frena_a_las_demas_consultas(mundo):
    con = mundo.poner_base()
    abrir_tarea_nueva("k-1", titulo="Primera")
    abrir_tarea_nueva("k-2", titulo="Segunda", proyecto=2, noco=B)
    mundo.modo = "falla"
    vuelta(mundo)
    assert len(mundo.enviados) == 2                      # intentó las dos
    assert [consulta(con, i)["estado"] for i in (1, 2)] == ["abierta", "abierta"]


def test_G1_lo_que_sale_no_esta_marcado_ni_dicho_si_no_salio_nada(mundo):
    """Sin mensaje no hay constancia en la bandeja del responsable (`registrar_aviso`): no se dice «avisé»."""
    con = mundo.poner_base()
    abrir_tarea_nueva()
    mundo.modo = "falla"
    vuelta(mundo)
    assert con.execute("SELECT COUNT(*) FROM bandeja").fetchone()[0] == 0
    mundo.modo = "ok"
    vuelta(mundo)
    fila = con.execute("SELECT chat_id, origen, respuesta_lucy FROM bandeja").fetchall()
    assert [(f["chat_id"], f["origen"]) for f in fila] == [(ANA, "despertador")]
    assert "Subir el demo" in fila[0]["respuesta_lucy"]


def test_G1_si_la_constancia_falla_la_consulta_queda_preguntada_igual(mundo, monkeypatch):
    con = mundo.poner_base()
    abrir_tarea_nueva()

    async def _revienta(*a, **k):
        raise RuntimeError("bandeja caída")
    monkeypatch.setattr(db, "registrar_aviso", _revienta)
    assert vuelta(mundo) == 1
    assert consulta(con)["estado"] == "preguntada"       # la tarjeta ya salió: no se reenvía
    assert vuelta(mundo) == 0 and len(mundo.enviados) == 1


# ═══════════════════════════════════════════════════════════════════════
# G2 · la tarjeta va solo al responsable, sin copia
# ═══════════════════════════════════════════════════════════════════════

def test_G2_la_tarjeta_va_solo_al_responsable_y_no_se_copia_a_nadie(mundo):
    con = mundo.poner_base()
    config._NOMBRES_DE_COPIA = ("Rosi Inventada",)
    assert config.chats_de_copia() == (ROSI,)            # la copia está encendida de verdad
    abrir_tarea_nueva()                                  # el responsable del proyecto: ANA
    vuelta(mundo)
    assert [e["chat_id"] for e in mundo.enviados] == [ANA]


def test_G2_si_el_responsable_ES_el_dueno_la_copia_a_Rosi_tampoco_sale(mundo):
    """El caso que `copia_dueno` copiaría: el responsable es el dueño. La tarjeta tiene lo que pidió un cliente."""
    con = mundo.poner_base()
    config._NOMBRES_DE_COPIA = ("Rosi Inventada",)
    abrir_tarea_nueva(responsable=mundo.dueno)
    vuelta(mundo)
    assert [e["chat_id"] for e in mundo.enviados] == [mundo.dueno]
    assert mundo.a(ROSI) == []


def test_G2_el_recordatorio_tampoco_se_copia(mundo):
    con = mundo.poner_base()
    config._NOMBRES_DE_COPIA = ("Rosi Inventada",)
    sembrar(con, "preguntada", resp=mundo.dueno, enviada_en=AHORA - timedelta(minutes=40))
    vuelta(mundo)
    assert [e["chat_id"] for e in mundo.enviados] == [mundo.dueno]


def test_G2_un_responsable_sin_telegram_no_recibe_nada_y_la_consulta_sigue_abierta(mundo):
    """Code puede quedar de responsable de una tarea y no tiene Telegram: la misma puerta que los demás avisos."""
    con = mundo.poner_base()
    sembrar(con, "abierta", resp=config.CHAT_ID_CODE)
    assert vuelta(mundo) == 0
    assert mundo.enviados == []
    assert consulta(con)["estado"] == "abierta"


def test_G2_la_tarjeta_no_lleva_numeros_de_chat_ni_formato(mundo):
    con = mundo.poner_base()
    abrir_tarea_nueva(vence="2026-03-25")
    vuelta(mundo)
    e = mundo.enviados[0]
    assert "parse_mode" not in e                         # texto plano: lo del cliente no se interpreta
    for chat in (ANA, BETO, ROSI, mundo.dueno, A):
        assert str(chat) not in e["text"]
    for dato in ("Proyecto Alfa", "Cliente Alfa", "El cliente quiere el demo el viernes", "Subir el demo", "25/03"):
        assert dato in e["text"], dato


# ═══════════════════════════════════════════════════════════════════════
# G3 · recordatorio UNA vez a los 30 min; nunca caduca
# ═══════════════════════════════════════════════════════════════════════

def test_G3_a_los_29_59_no_hay_recordatorio_a_los_30_00_si_y_despues_nunca_mas(mundo):
    con = mundo.poner_base()
    sembrar(con, "preguntada", enviada_en=AHORA)
    assert vuelta(mundo, ahora=AHORA + timedelta(minutes=29, seconds=59)) == 0
    assert mundo.enviados == []
    assert vuelta(mundo, ahora=AHORA + timedelta(minutes=30)) == 1
    r = mundo.enviados[0]
    assert r["chat_id"] == ANA and "Sigue pendiente" in r["text"]
    assert r["reply_parameters"].message_id == 555      # responde a la tarjeta
    assert consulta(con)["recordada_en"] == AHORA + timedelta(minutes=30)
    # y no se repite: ni un minuto después, ni un día, ni un mes
    for despues in (timedelta(minutes=31), timedelta(hours=24), timedelta(days=30)):
        assert vuelta(mundo, ahora=AHORA + despues) == 0
    assert len(mundo.enviados) == 1
    assert consulta(con)["estado"] == "preguntada"


def test_G3_una_consulta_vieja_se_recuerda_igual_nunca_caduca(mundo):
    con = mundo.poner_base()
    sembrar(con, "preguntada", enviada_en=AHORA - timedelta(days=90))
    assert vuelta(mundo) == 1
    assert "Sigue pendiente" in mundo.enviados[0]["text"]
    assert consulta(con)["estado"] == "preguntada"       # y sigue esperando: nada la cierra por vieja


def test_G3_si_el_recordatorio_no_sale_no_se_marca_y_se_reintenta(mundo):
    con = mundo.poner_base()
    sembrar(con, "preguntada", enviada_en=AHORA - timedelta(minutes=40))
    mundo.modo = "falla"
    assert vuelta(mundo) == 0
    assert consulta(con)["recordada_en"] is None
    mundo.modo = "ok"
    assert vuelta(mundo) == 1
    assert consulta(con)["recordada_en"] is not None


def test_G3_solo_las_preguntadas_se_recuerdan(mundo):
    con = mundo.poner_base()
    vieja = AHORA - timedelta(hours=5)
    sembrar(con, "abierta", clave="a")
    sembrar(con, "anotada", clave="b", enviada_en=vieja)
    sembrar(con, "rechazada", clave="c", enviada_en=vieja)
    sembrar(con, "no_se_pudo", clave="d", enviada_en=vieja)
    sembrar(con, "resuelta", tipo="nueva_fecha", clave="e", tarea=12, enviada_en=vieja)
    # la `abierta` manda su tarjeta (otro mensaje); recordatorios: ninguno
    mundo.modo = "ok"
    vuelta(mundo)
    assert [e for e in mundo.enviados if "Sigue pendiente" in e["text"]] == []


def test_G3_el_recordatorio_dice_lo_que_se_pregunta(mundo):
    con = mundo.poner_base()
    sembrar(con, "preguntada", clave="t", enviada_en=AHORA - timedelta(minutes=31))
    vuelta(mundo)
    assert "¿Se puede?" in mundo.enviados[0]["text"] and "Titulo sembrado" in mundo.enviados[0]["text"]


# ═══════════════════════════════════════════════════════════════════════
# G4 · la nueva_fecha se resuelve cuando la tarea deja de estar vencida, pendiente o siguiente — por CUALQUIER escritor
# ═══════════════════════════════════════════════════════════════════════

def _preguntar_nueva_fecha(mundo):
    con = mundo.poner_base()
    abrir_nueva_fecha()
    vuelta(mundo)                                         # sale la tarjeta
    assert consulta(con)["estado"] == "preguntada"
    mundo.enviados.clear()
    return con


def _por_el_panel(tid, vence):
    config.CHAT_IDS_PERMITIDOS = tuple(set(config.CHAT_IDS_PERMITIDOS) | {ANA})
    CLIENTE_HTTP.cookies.set(panel.COOKIE, auth.crear_token(ANA, auth.VIDA_SESION))
    r = CLIENTE_HTTP.post(f"/proyectos/tarea/{tid}/fecha", data={"vence": vence, "prev_vence": "x"},
                          follow_redirects=False)
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]


def _por_telegram(tid, vence):
    despues, log_id = correr(crud.editar("tareas", tid, {"vence_en": vence}, "la fecha nueva, por Telegram"))
    assert despues is not None and log_id is not None


def _a_mano(con, tid, vence):
    con.execute("UPDATE tareas SET vence_en = ? WHERE id = ?", (vence.isoformat(), tid))
    con.commit()


CAMINOS_DE_LA_FECHA = {
    "panel": lambda mundo, con, tid, dia: _por_el_panel(tid, f"{dia}T10:00"),
    "telegram": lambda mundo, con, tid, dia: _por_telegram(tid, sd(dia, "10:00").isoformat()),
    "otro_escritor_directo": lambda mundo, con, tid, dia: _a_mano(con, tid, sd(dia, "10:00")),
}


@pytest.mark.parametrize("camino", sorted(CAMINOS_DE_LA_FECHA))
def test_G4_mover_la_fecha_resuelve_la_consulta_por_cada_camino_y_edita_la_tarjeta(mundo, camino):
    con = _preguntar_nueva_fecha(mundo)
    assert vuelta(mundo) == 0 and consulta(con)["estado"] == "preguntada"     # mientras sigue vencida, espera
    CAMINOS_DE_LA_FECHA[camino](mundo, con, 12, "2026-03-25")
    # el escritor de verdad dejó la fecha nueva en la base
    assert db.dia_rd(vence_de(con, 12)) == date(2026, 3, 25)
    vuelta(mundo)
    c = consulta(con)
    assert c["estado"] == "resuelta" and c["contestada_en"] is not None and c["contestada_por_chat_id"] is None
    assert len(mundo.editados) == 1
    e = mundo.editados[0]
    assert (e["chat_id"], e["message_id"]) == (ANA, c["tarjeta_msg_id"])
    assert "Nueva fecha estimada" in e["text"] and "25/03" in e["text"] and "Grabar las voces" in e["text"]
    # una vez resuelta no se vuelve a tocar
    mundo.editados.clear()
    vuelta(mundo)
    assert mundo.editados == [] and mundo.enviados == []


def test_G4_quitarle_la_fecha_tambien_la_resuelve(mundo):
    con = _preguntar_nueva_fecha(mundo)
    _por_el_panel(12, "")
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert "quedó sin fecha" in mundo.editados[0]["text"]


def test_G4_si_la_tarea_se_hace_ya_no_hace_falta(mundo):
    con = _preguntar_nueva_fecha(mundo)
    correr(crud.editar("tareas", 12, {"estado": "hecha"}, "se hizo"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert "ya no está pendiente" in mundo.editados[0]["text"]


def test_G4_si_la_tarea_se_borra_ya_no_hace_falta(mundo):
    con = _preguntar_nueva_fecha(mundo)
    con.execute("UPDATE tareas SET borrado_en = ? WHERE id = 12", (AHORA.isoformat(),))
    con.commit()
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert "ya no está pendiente" in mundo.editados[0]["text"]


def test_G4_si_ya_no_es_la_siguiente_ya_no_hace_falta(mundo):
    """Otra tarea del mismo proyecto pasa por delante (más vencida): la consulta era de la que SEGUÍA."""
    con = _preguntar_nueva_fecha(mundo)
    # la 13 (sin fecha) se vence antes que la 12: ahora la que sigue es otra
    _a_mano(con, 13, sd("2026-03-01"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert "ya no es la tarea que sigue" in mundo.editados[0]["text"]


def test_G4_mientras_siga_siendo_la_siguiente_vencida_no_se_resuelve(mundo):
    con = _preguntar_nueva_fecha(mundo)
    _por_el_panel(11, "2026-03-30T10:00")                 # se mueve OTRA tarea del proyecto
    _por_el_panel(12, "2026-03-04T10:00")                 # y a la 12 se le pone otra fecha, también vencida
    for _ in range(3):
        vuelta(mundo)
    assert consulta(con)["estado"] == "preguntada" and mundo.editados == []


def test_G4_una_abierta_que_se_arregla_antes_de_la_tarjeta_pasa_a_resuelta_sin_mandar_nada(mundo):
    con = mundo.poner_base()
    abrir_nueva_fecha()
    _a_mano(con, 12, sd("2026-03-25"))
    assert vuelta(mundo) == 0
    assert consulta(con)["estado"] == "resuelta"
    assert mundo.enviados == [] and mundo.editados == []


def test_G4_si_no_se_pudo_editar_la_tarjeta_la_consulta_queda_resuelta_igual(mundo):
    con = _preguntar_nueva_fecha(mundo)
    mundo.modo_edicion = "falla"
    _a_mano(con, 12, sd("2026-03-25"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert len(mundo.editados) == 1


def test_G4_el_recordatorio_de_una_resuelta_no_sale(mundo):
    con = _preguntar_nueva_fecha(mundo)
    _a_mano(con, 12, sd("2026-03-25"))
    vuelta(mundo, ahora=AHORA + timedelta(hours=5))       # la fecha se arregló Y pasó el tiempo del recordatorio
    assert consulta(con)["estado"] == "resuelta"
    assert [e for e in mundo.enviados if "Sigue pendiente" in e["text"]] == []


def test_G4_la_tarjeta_de_la_nueva_fecha_dice_la_tarea_y_lleva_el_enlace_si_hay_panel(mundo):
    con = mundo.poner_base()
    config.PANEL_URL = "https://panel.ejemplo.invalido"
    abrir_nueva_fecha()
    vuelta(mundo)
    e = mundo.enviados[0]
    assert "Grabar las voces" in e["text"] and "05/03" in e["text"] and "¿Nueva fecha estimada?" in e["text"]
    boton = e["reply_markup"].inline_keyboard[0][0]
    assert boton.url == "https://panel.ejemplo.invalido/proyectos?p=1&t=12#tarea-12"


def test_G4_sin_panel_https_la_tarjeta_va_sin_boton_y_la_de_tarea_nueva_espera_a_L5(mundo):
    con = mundo.poner_base()
    config.PANEL_URL = "http://localhost:8000"            # Telegram solo acepta https en un botón
    abrir_nueva_fecha()
    abrir_tarea_nueva()
    config.PANEL_URL = "https://panel.ejemplo.invalido"
    vuelta(mundo)
    por_titulo = {("nueva_fecha" if "venció" in e["text"] else "tarea_nueva"): e for e in mundo.enviados}
    assert "reply_markup" in por_titulo["nueva_fecha"]
    assert "reply_markup" not in por_titulo["tarea_nueva"]  # los botones Sí/No son de la pieza L5


# ═══════════════════════════════════════════════════════════════════════
# G4 · la lista de escritores de `vence_en` sale del código, y cada uno está contado
# ═══════════════════════════════════════════════════════════════════════

def _py_del_repo():
    from test_buzon_que_no_se_ve import _py_en_disco   # LA puerta de los barridos del repo
    for ruta in sorted(_py_en_disco(_ROOT)):
        if ruta.relative_to(_ROOT).parts[0] != "tests":
            yield ruta


def _texto_de(nodo) -> str | None:
    """El texto de una cadena o f-string con cada pedazo interpolado como `{}`."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in nodo.values)
    return None


_ESCRIBE_TAREAS = re.compile(r"\bUPDATE\s+(tareas|\{\})\s+SET\b", re.I)


def _escritores_de_fechas(arbol) -> set[str]:
    """Funciones con un `UPDATE tareas SET …` que nombra `vence_en`, y funciones con un `UPDATE {tabla} SET …` GENÉRICO
    (la tabla interpolada, como `crud.editar`/`deshacer`/`borrar` y `despertador.revisar`): estas últimas pueden mover una
    fecha sin nombrarla. Las otras (`UPDATE tareas` que no nombran `vence_en`) no la escriben."""
    salida = set()
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            t = _texto_de(n)
            if t is None or not _ESCRIBE_TAREAS.search(t):
                continue
            if "vence_en" in t or re.search(r"\bUPDATE\s+\{\}\s+SET\b", t, re.I):
                salida.add(fn.name)
    return salida


# Cada escritor que el barrido encuentra, y por dónde lo cubre una prueba de ESTE archivo. La lista se compara con el código:
# un escritor nuevo hace fallar la prueba hasta que alguien decida cómo se cubre.
ESCRITORES_DE_FECHAS = {
    "db/db.py::mover_vence": "el panel (`CAMINOS_DE_LA_FECHA['panel']`)",
    "acciones/crud.py::editar": "Telegram (`CAMINOS_DE_LA_FECHA['telegram']`)",
    "acciones/crud.py::deshacer": "genérico: devuelve el `antes` de una edición; lo cubre `otro_escritor_directo`",
    "acciones/crud.py::borrar": "genérico: solo llena `borrado_en`; la tarea borrada sale de la siguiente (test de borrar)",
    "cerebro/despertador.py::revisar": "escribe `avisos_enviados` (no la fecha); no mueve nada",
    "cerebro/despertador.py::_reprogramar_recurrentes": "mueve una recurrente: lo cubre `otro_escritor_directo`",
    "tools/rellenar_duenos.py::main": "guion de una vez: escribe `bandeja_id` (no la fecha)",
}


def test_G4_los_escritores_de_la_fecha_salen_del_codigo_y_cada_uno_esta_cubierto():
    vistos = set()
    for ruta in _py_del_repo():
        for nombre in _escritores_de_fechas(ast.parse(ruta.read_text(encoding="utf-8"))):
            vistos.add(f"{ruta.relative_to(_ROOT)}::{nombre}")
    assert vistos == set(ESCRITORES_DE_FECHAS), (
        f"escritores nuevos: {sorted(vistos - set(ESCRITORES_DE_FECHAS))}; "
        f"ya no están: {sorted(set(ESCRITORES_DE_FECHAS) - vistos)}")
    # y los dos caminos que la prueba recorre de verdad son los de la lista
    assert {"panel", "telegram"} <= set(CAMINOS_DE_LA_FECHA)


def test_G4_el_barrido_ve_un_escritor_inventado():
    falso = ast.parse(
        "async def mueve(c):\n    await c.execute('UPDATE tareas SET vence_en = %s WHERE id = %s')\n"
        "async def generico(c, t):\n    await c.execute(f'UPDATE {t} SET {a} WHERE id = %s')\n"
        "async def no_la_toca(c):\n    await c.execute('UPDATE tareas SET titulo = %s WHERE id = %s')\n")
    assert _escritores_de_fechas(falso) == {"mueve", "generico"}


def test_G4_la_decision_no_mira_quien_escribe_solo_lo_que_la_base_dice_ahora():
    """`consulta_de_fecha_sigue_viva` recibe SOLO la `proxima` de la página y la tarea de la consulta: ni la huella de
    `log_acciones`, ni el actor, ni la ruta."""
    firma = inspect.signature(db.consulta_de_fecha_sigue_viva)
    assert list(firma.parameters) == ["proxima", "tarea_id"]
    assert db.consulta_de_fecha_sigue_viva({"id": 12, "vencida": True}, 12) is True
    assert db.consulta_de_fecha_sigue_viva({"id": 12, "vencida": False}, 12) is False
    assert db.consulta_de_fecha_sigue_viva({"id": 99, "vencida": True}, 12) is False
    assert db.consulta_de_fecha_sigue_viva(None, 12) is False


# ═══════════════════════════════════════════════════════════════════════
# G5 · «vencida» es la de `proxima` (por día de Santo Domingo), no una cuenta aparte
# ═══════════════════════════════════════════════════════════════════════

def test_G5_una_fecha_de_hoy_que_ya_paso_por_instante_NO_esta_vencida(mundo):
    """`vence_en` hoy a las 00:30 de Santo Domingo: por instante ya pasó (la prueba corre con el reloj real años
    después del `HOY` fijo); por día es de hoy. La página dice «hoy», no «atrasada»: la consulta se resuelve."""
    con = _preguntar_nueva_fecha(mundo)
    _a_mano(con, 12, sd("2026-03-10", "00:30"))
    assert vence_de(con, 12) < datetime.now(UTC)
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"
    assert "Nueva fecha estimada" in mundo.editados[0]["text"] and "10/03" in mundo.editados[0]["text"]


def test_G5_ayer_a_las_23_59_si_esta_vencida_y_hoy_a_las_23_59_no(mundo):
    con = _preguntar_nueva_fecha(mundo)
    _a_mano(con, 12, sd("2026-03-09", "23:59"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "preguntada"
    _a_mano(con, 12, sd("2026-03-10", "23:59"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "resuelta"


def test_G5_el_borde_lo_marca_el_dia_de_Santo_Domingo_no_el_de_UTC(mundo):
    """Las 22:00 de Santo Domingo del día 9 son las 02:00 UTC del día 10: por UTC ya es «hoy», por Santo Domingo es ayer."""
    con = _preguntar_nueva_fecha(mundo)
    _a_mano(con, 12, sd("2026-03-09", "22:00"))
    vuelta(mundo)
    assert consulta(con)["estado"] == "preguntada"


# ═══════════════════════════════════════════════════════════════════════
# Las puertas: sin tabla, el bucle, el censo de quién escribe la tabla
# ═══════════════════════════════════════════════════════════════════════

def test_sin_la_migracion_el_despertador_no_hace_nada_y_no_revienta(mundo):
    mundo.poner_base(con_tabla=False)
    assert vuelta(mundo) == 0
    assert mundo.enviados == [] and mundo.editados == []


def test_otros_errores_de_la_base_no_se_disfrazan_de_falta_de_migracion(mundo, monkeypatch):
    mundo.poner_base()

    async def _revienta(*a, **k):
        raise RuntimeError("la base se cayó")
    monkeypatch.setattr(db, "consultas_de_natalia_de_fecha_vivas", _revienta)
    with pytest.raises(RuntimeError):
        vuelta(mundo)


def test_el_bucle_llama_a_las_consultas_dentro_de_la_vuelta_del_despertador_con_su_propio_try():
    arbol = ast.parse(inspect.getsource(interpretar.bucle))
    llamadas = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Try):
            dentro = {c.func.attr for t in n.body for c in ast.walk(t)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
            if "revisar_consultas_de_natalia" in dentro:
                llamadas.append(dentro)
    assert len(llamadas) == 1
    # el try de las consultas NO es el de `revisar`: una falla de una no apaga la otra
    assert "revisar" not in llamadas[0]


def test_el_despertador_manda_a_telegram_solo_por_las_dos_puertas_de_los_responsables():
    """Hermanos: todo `send_message` de `cerebro/despertador.py` está en una función que llama a
    `puede_recibir_telegram` (directo o por `_avisar`, cuyos llamadores lo hacen) — se mira el árbol, no el texto."""
    arbol = ast.parse(inspect.getsource(despertador))
    envian = {fn.name for fn in ast.walk(arbol) if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
              for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
              and c.func.attr == "send_message"}
    assert envian == {"_avisar", "_mandar_al_responsable"}
    mandar = next(fn for fn in ast.walk(arbol) if isinstance(fn, ast.AsyncFunctionDef)
                  and fn.name == "_mandar_al_responsable")
    llama = {c.func.attr for c in ast.walk(mandar) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert {"puede_recibir_telegram", "sin_copiar", "send_message"} <= llama


def test_nada_de_L4_toca_el_estado_salvo_por_la_unica_funcion():
    """La marca del recordatorio es `recordada_en`; el estado solo lo escribe `pasar_consulta_de_natalia`."""
    arbol = ast.parse(inspect.getsource(db.marcar_consulta_de_natalia_recordada).lstrip())
    sets = [re.search(r"SET\s+(.*?)\s+WHERE", " ".join(n.value.split()), re.I).group(1)
            for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.search(r"\bUPDATE\s+consultas_de_natalia\b", n.value, re.I)]
    assert sets == ["recordada_en = %s"]
    assert "pasar_consulta_de_natalia(" in inspect.getsource(db.pasar_consulta_de_natalia_sola)
