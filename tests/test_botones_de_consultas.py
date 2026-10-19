# -*- coding: utf-8 -*-
"""Los botones Sí / No de las consultas de Natalia (18-oct-2026, pieza L5 de `disenos/natalia-tareas-en-proyectos/DISENO.md`).

Un cliente le pide algo a Natalia, ella abre una CONSULTA en Lucy (L3), el despertador le manda la tarjeta con dos botones al
responsable (L4 + L5) y, cuando él toca uno, `acciones.botones.al_pulsar` llama a `db.contestar_consulta_de_natalia`, que
(con el «Sí») crea la tarea, deja el comentario de Natalia, pone al cliente como persona de la tarea y marca la consulta
`anotada`: todo en UNA transacción.

CAMINO DE PRODUCCIÓN: la tarjeta la manda `despertador.revisar_consultas_de_natalia` REAL por un `telegram.Bot` real (con
`copia_dueno` instalada); los botones que se tocan son LOS QUE LA TARJETA LLEVÓ; el toque pasa por `botones.al_pulsar`
REAL; la consulta se abre con `db.abrir_consulta_de_natalia` REAL; la tarea la escribe `db.crear_tarea_desde_el_panel`
REAL y el comentario `db.comentar_tarea` REAL. Todo el SQL se ejecuta de verdad contra SQLite (`%s` -> `?`), con la tabla de
consultas armada con el DDL REAL de la migración y con TRANSACCIONES de verdad (`BEGIN`/`COMMIT`/`ROLLBACK`, y el
`SAVEPOINT` de `conn.transaction()`), que es lo que garantiza que «todo o nada» sea verdad en estas pruebas. Lo ÚNICO de
mentira es la red de Telegram (`Bot.send_message` y `Bot.edit_message_text`, a nivel de clase) y el objeto
`CallbackQuery`.

LAS FALLAS SE INYECTAN EN LA BASE (un TRIGGER que aborta el `INSERT` o el `UPDATE` que se quiere romper), no en Python: es
la base la que falla en el punto exacto, como lo haría Postgres.

LA CARRERA («el primero gana») se arma con un gancho: justo antes de que el segundo toque abra el `SAVEPOINT` de su tarea
—después de haber leído la consulta como `preguntada`— se ejecuta COMPLETO el primer toque. Es lo que pasaría con dos
toques casi juntos (Telegram reenvía el callback si tarda): el segundo leyó `preguntada` y cuando va a reclamarla ya es
otra.

FRONTERA: SQLite no es Postgres (no entiende `ANY`, no hay FK, los tipos son laxos; la carrera de verdad entre dos
conexiones la resuelve el bloqueo de fila del `UPDATE … WHERE estado`, que aquí se emula con el gancho). Nada se corrió
contra Postgres ni contra Telegram: `tools/humo.py` corre las lecturas contra Postgres.

NINGÚN nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_botones_de_consultas.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import itertools
import json
import os
import pathlib
import sqlite3
import sys
import types
from datetime import date, datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-botones-consultas")
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

import acciones.botones as botones  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.despertador as despertador  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

UTC = timezone.utc
MIGRACION = _ROOT / "db" / "migrations" / "2026-10-18_consultas_de_natalia.sql"

A, B = 5001, 5002                                  # fichas de clientes inventadas
ANA, BETO, ROSI = 7710001, 7710002, 7710003        # chats inventados
AJENO = 7799999                                    # alguien de fuera de la casa
HOY = date(2026, 3, 10)
AHORA = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def sd(dia: str, hora: str = "12:00") -> datetime:
    return datetime.fromisoformat(f"{dia}T{hora}").replace(tzinfo=config.TZ)


# ── La base: SQLite con TRANSACCIONES de verdad ──────────────────────────────────────────────────────────

sqlite3.register_converter(
    "TSTZ", lambda b: datetime.fromisoformat(b.decode()).astimezone(UTC))


class _SinTabla(Exception):
    sqlstate = "42P01"


class _Fila(dict):
    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else super().__getitem__(k)


def _adaptar(p):
    if isinstance(p, datetime):
        return p.isoformat()
    if isinstance(p, (list, tuple, dict)):
        return json.dumps(p)
    return p


class _Cur:
    def __init__(self, base):
        self._base = base
        self._filas: list[dict] = []
        self.rowcount = 0

    async def execute(self, sql, params=None):
        await asyncio.sleep(0)
        pasos = tuple(_adaptar(p) for p in (params or ()))
        sql = sql.replace("%s", "?").replace("IS NOT DISTINCT FROM", "IS").replace("now()", "CURRENT_TIMESTAMP")
        try:
            cur = self._base.con.execute(sql, pasos)
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


class _Tx:
    """`conn.transaction()` de psycopg: un SAVEPOINT. Sale limpio -> RELEASE; sale con error -> ROLLBACK TO y RELEASE."""

    def __init__(self, base):
        self._base = base

    async def __aenter__(self):
        gancho, self._base.gancho = self._base.gancho, None
        if gancho is not None:
            await gancho()
        self._nombre = f"sp{next(self._base.ids)}"
        self._base.con.execute(f"SAVEPOINT {self._nombre}")
        return self

    async def __aexit__(self, tipo, valor, tb):
        if tipo is not None:
            self._base.con.execute(f"ROLLBACK TO {self._nombre}")
        self._base.con.execute(f"RELEASE {self._nombre}")
        return False


class _Conn:
    def __init__(self, base):
        self._base = base

    def cursor(self, row_factory=None):
        return _Cur(self._base)

    async def execute(self, sql, params=None):
        return await _Cur(self._base).execute(sql, params)

    def transaction(self):
        return _Tx(self._base)


class _Base:
    """La base y su pool. `pool.connection()` es la transacción de la conexión de psycopg: `BEGIN` al entrar, `COMMIT` si
    sale limpio, `ROLLBACK` si sale con un error. Una conexión abierta DENTRO de otra (solo el gancho de la carrera lo
    hace) es un `SAVEPOINT`."""

    def __init__(self, con):
        self.con = con
        self.profundidad = 0
        self.ids = itertools.count(1)
        self.gancho = None
        self.commits = 0
        self.max_profundidad = 0          # cuántas conexiones hubo abiertas a la vez, como mucho

    def connection(self):
        base = self

        class _CM:
            async def __aenter__(self_):
                base.profundidad += 1
                base.max_profundidad = max(base.max_profundidad, base.profundidad)
                if base.profundidad == 1:
                    base.con.execute("BEGIN")
                else:
                    self_.nombre = f"cx{next(base.ids)}"
                    base.con.execute(f"SAVEPOINT {self_.nombre}")
                return _Conn(base)

            async def __aexit__(self_, tipo, valor, tb):
                try:
                    if base.profundidad == 1:
                        base.con.execute("ROLLBACK" if tipo is not None else "COMMIT")
                        base.commits += tipo is None
                    else:
                        if tipo is not None:
                            base.con.execute(f"ROLLBACK TO {self_.nombre}")
                        base.con.execute(f"RELEASE {self_.nombre}")
                finally:
                    base.profundidad -= 1
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
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, area TEXT, titulo TEXT, estado TEXT DEFAULT 'pendiente',
  vence_en TSTZ, completado_en TSTZ, creado_en TSTZ DEFAULT CURRENT_TIMESTAMP, responsable_chat_id INTEGER,
  borrado_en TSTZ, grave INTEGER NOT NULL DEFAULT 0, pospuesta_veces INTEGER DEFAULT 0, bandeja_id INTEGER,
  anticipos_min TEXT, primero_id INTEGER, detalle TEXT, persona_id INTEGER);
CREATE TABLE participantes (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, tarea_id INTEGER, noco_id INTEGER,
  nombre TEXT, rol TEXT, creado_en TSTZ DEFAULT CURRENT_TIMESTAMP, creado_por_chat_id INTEGER,
  borrado_en TSTZ, borrado_por_chat_id INTEGER);
CREATE TABLE comentarios_tarea (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tarea_id INTEGER, autor_chat_id INTEGER,
  creado_en TSTZ DEFAULT CURRENT_TIMESTAMP, texto TEXT, borrado_en TSTZ, borrado_por_chat_id INTEGER,
  editado_en TSTZ);
CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER);
CREATE TABLE log_acciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT, tabla TEXT, registro_id INTEGER,
  antes TEXT, despues TEXT, motivo TEXT, bandeja_id INTEGER);
CREATE TABLE bandeja (
  id INTEGER PRIMARY KEY AUTOINCREMENT, creado_en TSTZ DEFAULT CURRENT_TIMESTAMP, origen TEXT, tipo_entrada TEXT,
  contenido_raw TEXT, chat_id INTEGER, estado TEXT, transcripcion TEXT, respuesta_lucy TEXT, procesado_en TSTZ);
"""

NOMBRES_DE_CLIENTE = {A: "Cliente Alfa", B: "Cliente Beta"}
PROYECTOS = [          # (id, nombre, estado, responsable, cliente)
    (1, "Proyecto Alfa", "activo", ANA, A),
    (2, "Proyecto Gamma", "activo", BETO, B),
]
TAREAS = [             # (id, proyecto, titulo, estado, vence)
    (11, 1, "Mezclar el tema", "pendiente", sd("2026-03-20")),
    (12, 1, "Grabar las voces", "pendiente", sd("2026-03-05")),
    (50, 2, "Tarea de otro cliente", "pendiente", sd("2026-03-04")),
]


def _base(con_tabla=True):
    con = sqlite3.connect(":memory:", check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES,
                          isolation_level=None)             # sin transacciones implícitas: las manda _Base
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
    return con


# ── La red de mentira ────────────────────────────────────────────────────────────────────────────────────

class Red:
    def __init__(self):
        self.enviados: list[dict] = []
        self.base = None

    def a(self, chat):
        return [e for e in self.enviados if e["chat_id"] == chat]


@pytest.fixture
def mundo():
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
        red.enviados.append({"chat_id": chat_id, "text": text, **kwargs})
        return types.SimpleNamespace(message_id=1000 + len(red.enviados))

    async def _edicion(self, chat_id=None, message_id=None, text=None, **kwargs):
        return True

    telegram.Bot.send_message = _envio
    telegram.Bot.edit_message_text = _edicion
    copia_dueno.instalar()
    red.bot = telegram.Bot(token="123456:token-de-prueba")

    def poner_base(**kw):
        con = _base(**kw)
        red.base = _Base(con)
        db.pool = red.base
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
                      vence="2026-03-13", responsable=None, noco=A):
    return correr(db.abrir_consulta_de_natalia(noco, clave, "tarea_nueva", proyecto_id=proyecto, titulo=titulo,
                                               pedido=pedido, vence=vence, responsable_chat_id=responsable, hoy=HOY))


def abrir_nueva_fecha(clave="f-1", tarea=12, noco=A):
    return correr(db.abrir_consulta_de_natalia(noco, clave, "nueva_fecha", tarea_id=tarea, hoy=HOY))


def consulta(con, cid=1):
    return dict(con.execute("SELECT * FROM consultas_de_natalia WHERE id = ?", (cid,)).fetchone())


def con_tarjeta(mundo, **kw):
    """Abre una consulta `tarea_nueva`, deja que el despertador REAL mande su tarjeta y devuelve (con, los `callback_data`
    de SUS botones: (sí, no))."""
    con = mundo.poner_base()
    abrir_tarea_nueva(**kw)
    vuelta(mundo)
    e = mundo.enviados[-1]
    datos = [b.callback_data for fila in e["reply_markup"].inline_keyboard for b in fila]
    assert len(datos) == 2
    return con, datos[0], datos[1]


# ── El toque ─────────────────────────────────────────────────────────────────────────────────────────────

class Toque:
    """Un `CallbackQuery` de mentira: lo justo que `al_pulsar` toca."""

    def __init__(self, data, chat_id, texto="La tarjeta."):
        self.data = data
        self.avisos: list[str] = []
        self.editados: list[dict] = []
        self.respuestas: list = []
        self.message = types.SimpleNamespace(chat_id=chat_id, text_html=texto, reply_text=self._reply)

    async def answer(self, *a, **k):
        self.respuestas.append((a, k))

    async def edit_message_text(self, text, **kw):
        self.editados.append({"text": text, **kw})

    async def _reply(self, texto, **kw):
        self.avisos.append(texto)
        return types.SimpleNamespace(message_id=1)

    @property
    def texto_final(self):
        return self.editados[-1]["text"] if self.editados else None


def tocar(data, chat_id):
    q = Toque(data, chat_id)
    correr(botones.al_pulsar(types.SimpleNamespace(callback_query=q), None))
    return q


def foto(con):
    """Todo lo que L5 puede escribir, contado."""
    n = lambda sql: con.execute(sql).fetchone()[0]          # noqa: E731
    return {
        "tareas": n("SELECT count(*) FROM tareas"),
        "bandeja_natalia": n("SELECT count(*) FROM bandeja WHERE origen = 'natalia'"),
        "bandeja": n("SELECT count(*) FROM bandeja"),
        "comentarios": n("SELECT count(*) FROM comentarios_tarea"),
        "participantes": n("SELECT count(*) FROM participantes"),
        "huellas": n("SELECT count(*) FROM log_acciones"),
    }


# ═══════════════════════════════════════════════════════════════════════
# El camino feliz, de punta a punta, y G5: lo que escribe el «Sí»
# ═══════════════════════════════════════════════════════════════════════

def test_la_tarjeta_real_lleva_los_botones_y_el_si_anota_la_tarea_con_todo(mundo):
    con, si, no = con_tarjeta(mundo)
    assert (si, no) == ("cns:1", "cnn:1")
    assert all(len(d.encode()) <= 64 for d in (si, no)), "callback_data pasa del tope de Telegram"
    antes = foto(con)

    q = tocar(si, ANA)

    c = consulta(con)
    assert c["estado"] == "anotada" and c["contestada_por_chat_id"] == ANA and c["contestada_en"] is not None
    tid = c["tarea_resultante_id"]
    assert tid is not None and foto(con)["tareas"] == antes["tareas"] + 1
    t = dict(con.execute("SELECT * FROM tareas WHERE id = ?", (tid,)).fetchone())
    assert (t["titulo"], t["proyecto_id"], t["responsable_chat_id"], t["estado"], t["borrado_en"]) == (
        "Subir el demo", 1, ANA, "pendiente", None)
    assert t["vence_en"] == c["vence_en"] and db.dia_rd(t["vence_en"]) == date(2026, 3, 13)   # la fecha propuesta, tal cual
    # G5: el escritor real, con el origen, la huella y los avisos que dice el diseño
    assert json.loads(t["anticipos_min"]) == []
    b = dict(con.execute("SELECT * FROM bandeja WHERE id = ?", (t["bandeja_id"],)).fetchone())
    assert (b["origen"], b["tipo_entrada"], b["chat_id"], b["estado"]) == (
        "natalia", "natalia", config.CHAT_ID_NATALIA, "procesado")
    assert (b["contenido_raw"], b["transcripcion"], b["respuesta_lucy"]) == (None, None, None), "la fila no es muda"
    h = dict(con.execute("SELECT * FROM log_acciones WHERE tabla = 'tareas' AND registro_id = ?", (tid,)).fetchone())
    assert (h["actor"], h["accion"], h["bandeja_id"]) == ("natalia", "crear", t["bandeja_id"])
    assert h["motivo"] == f"confirmada por Ana Inventada (consulta #{c['id']})"
    # el comentario de Natalia y el cliente como persona de la tarea
    com = dict(con.execute("SELECT * FROM comentarios_tarea WHERE tarea_id = ?", (tid,)).fetchone())
    assert com["autor_chat_id"] == config.CHAT_ID_NATALIA
    assert com["texto"] == "Pedido del cliente: «El cliente quiere el demo el viernes». Confirmó Ana Inventada."
    p = dict(con.execute("SELECT * FROM participantes WHERE tarea_id = ?", (tid,)).fetchone())
    assert (p["noco_id"], p["nombre"], p["rol"], p["creado_por_chat_id"]) == (
        A, "Cliente Alfa", "Cliente", config.CHAT_ID_NATALIA)
    huellas = {(f["tabla"], f["actor"]) for f in con.execute("SELECT tabla, actor FROM log_acciones WHERE id > ?",
                                                             (antes["huellas"],)).fetchall()}
    assert huellas == {("tareas", "natalia"), ("comentarios_tarea", "natalia"), ("participantes", "natalia")}
    # lo que se le dice al responsable sale de la tarea RELEÍDA
    assert q.editados[-1]["reply_markup"] is None, "el botón usado tiene que desaparecer"
    assert f"Anotada como tarea #{tid}" in q.texto_final and "Proyecto Alfa" in q.texto_final


def test_el_no_deja_la_consulta_rechazada_y_no_escribe_ninguna_tarea(mundo):
    con, si, no = con_tarjeta(mundo)
    antes = foto(con)
    q = tocar(no, ANA)
    c = consulta(con)
    assert (c["estado"], c["tarea_resultante_id"], c["contestada_por_chat_id"]) == ("rechazada", None, ANA)
    assert foto(con) == antes
    assert "Dijiste que no" in q.texto_final and "Anotada" not in q.texto_final


@pytest.mark.parametrize("vacio", [None, "", "   "])
def test_un_proyecto_sin_nombre_de_cliente_no_anota_nada_y_el_boton_sigue_sirviendo(mundo, vacio):
    """Lado estricto (DISENO §5.3: la ficha del cliente es persona de la tarea): sin nombre el Sí falla como cualquier
    otro error, deshace todo y la consulta sigue `preguntada`. Hoy ese caso no existe (medido por la sala, 18-oct-2026)."""
    con, si, no = con_tarjeta(mundo)
    antes, c0 = foto(con), consulta(con)
    con.execute("UPDATE proyectos SET cliente_nombre = ? WHERE id = 1", (vacio,))
    q = tocar(si, ANA)
    assert foto(con) == antes and consulta(con) == c0 and c0["estado"] == "preguntada"
    assert q.editados == [] and q.avisos and "No pude guardar" in q.avisos[0]


def test_un_nombre_de_proyecto_con_html_sale_escapado_en_la_tarjeta_cerrada(mundo):
    con, si, no = con_tarjeta(mundo)
    con.execute("UPDATE proyectos SET nombre = 'A<b>&' WHERE id = 1")
    q = tocar(si, ANA)
    assert "A&lt;b&gt;&amp;" in q.texto_final and "A<b>&" not in q.texto_final


# ═══════════════════════════════════════════════════════════════════════
# G1 · pasan por la puerta única de `al_pulsar`
# ═══════════════════════════════════════════════════════════════════════

def test_G1_los_botones_de_la_tarjeta_son_acciones_que_al_pulsar_reconoce(mundo):
    con, si, no = con_tarjeta(mundo)
    from test_botones_rosi import _acciones_reales          # LA lista que sale del árbol sintáctico de `al_pulsar`
    reales = set(_acciones_reales())
    assert {d.split(":")[0] for d in (si, no)} <= reales


@pytest.mark.parametrize("lado", [0, 1])
def test_G1_un_chat_ajeno_a_la_casa_toca_el_boton_y_no_pasa_nada(mundo, lado):
    con, si, no = con_tarjeta(mundo)
    antes, c0 = foto(con), consulta(con)
    q = tocar((si, no)[lado], AJENO)
    assert q.editados == [] and q.avisos == []
    assert foto(con) == antes and consulta(con) == c0


# ═══════════════════════════════════════════════════════════════════════
# G2 · solo el responsable de la consulta decide
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("lado", [0, 1])
def test_G2_nadie_mas_de_la_casa_decide_y_se_le_dice_a_quien_le_toca(mundo, lado):
    con, si, no = con_tarjeta(mundo)
    antes, c0 = foto(con), consulta(con)
    otros = [c for c in config.CHAT_IDS_PERMITIDOS if c != ANA]       # TODOS los demás de la casa, sacados de la config
    assert len(otros) >= 3
    for chat in otros:
        q = tocar((si, no)[lado], chat)
        assert q.editados == [], "no se toca la tarjeta del responsable"
        assert q.avisos == ["Esto le toca a Ana Inventada."]
    assert foto(con) == antes and consulta(con) == c0
    # y el responsable sí puede, después de que otros lo intentaron
    tocar((si, no)[lado], ANA)
    assert consulta(con)["estado"] == ("anotada", "rechazada")[lado]


def test_G2_la_funcion_de_la_base_tambien_se_niega_sola(mundo):
    """La comparación vive en `db.contestar_consulta_de_natalia`, no solo en el botón: cualquier camino que llegue ahí
    pasa por ella."""
    con, si, no = con_tarjeta(mundo)
    r = correr(db.contestar_consulta_de_natalia(1, BETO, True))
    assert r["resultado"] == "no_es_tuya" and not r["gano"]
    assert consulta(con)["estado"] == "preguntada"


# ═══════════════════════════════════════════════════════════════════════
# G3 · el primero gana
# ═══════════════════════════════════════════════════════════════════════

def test_G3_dos_si_seguidos_anotan_una_sola_tarea(mundo):
    con, si, no = con_tarjeta(mundo)
    q1 = tocar(si, ANA)
    despues_del_primero = foto(con)
    q2 = tocar(si, ANA)
    assert foto(con) == despues_del_primero and despues_del_primero["tareas"] == len(TAREAS) + 1
    assert "Ya estaba contestada" in q2.texto_final and "Anotada como tarea" in q2.texto_final
    assert "Ya estaba contestada" not in q1.texto_final


def test_G3_si_y_despues_no_deja_la_tarea_y_no_la_borra(mundo):
    con, si, no = con_tarjeta(mundo)
    tocar(si, ANA)
    despues = foto(con)
    q = tocar(no, ANA)
    assert consulta(con)["estado"] == "anotada" and foto(con) == despues
    assert "Ya estaba contestada" in q.texto_final and "Anotada como tarea" in q.texto_final


def test_G3_no_y_despues_si_no_crea_nada(mundo):
    con, si, no = con_tarjeta(mundo)
    tocar(no, ANA)
    despues = foto(con)
    q = tocar(si, ANA)
    assert consulta(con)["estado"] == "rechazada" and foto(con) == despues
    assert "Ya estaba contestada" in q.texto_final and "Anotada" not in q.texto_final


def test_G3_la_carrera_el_segundo_toque_leyo_preguntada_y_aun_asi_pierde(mundo):
    """El segundo toque lee la consulta como `preguntada`; ANTES de que reclame, el primero termina completo. El
    `UPDATE … WHERE estado = 'preguntada'` del segundo ya no encuentra nada: pierde y SU tarea se deshace."""
    con, si, no = con_tarjeta(mundo)
    ganador = {}

    async def el_primero():
        ganador["r"] = await db.contestar_consulta_de_natalia(1, ANA, True)

    mundo.base.gancho = el_primero
    perdedor = correr(db.contestar_consulta_de_natalia(1, ANA, True))
    assert ganador["r"]["gano"] is True and ganador["r"]["resultado"] == "anotada"
    assert perdedor["gano"] is False and perdedor["resultado"] == "anotada"
    assert perdedor["tarea_id"] == ganador["r"]["tarea_id"], "dice la tarea del que ganó, no la suya"
    f = foto(con)
    assert (f["tareas"], f["bandeja_natalia"], f["comentarios"], f["participantes"]) == (len(TAREAS) + 1, 1, 1, 1)
    assert consulta(con)["tarea_resultante_id"] == ganador["r"]["tarea_id"]


def test_G3_la_carrera_entre_un_si_y_un_no(mundo):
    con, si, no = con_tarjeta(mundo)
    ganador = {}

    async def el_no():
        ganador["r"] = await db.contestar_consulta_de_natalia(1, ANA, False)

    mundo.base.gancho = el_no
    perdedor = correr(db.contestar_consulta_de_natalia(1, ANA, True))
    assert ganador["r"]["resultado"] == "rechazada" and perdedor["resultado"] == "rechazada" and not perdedor["gano"]
    f = foto(con)
    assert (f["tareas"], f["bandeja_natalia"], f["comentarios"], f["participantes"]) == (len(TAREAS), 0, 0, 0)


# ═══════════════════════════════════════════════════════════════════════
# G4 · «Sí» = consulta anotada Y tarea, en una transacción
# ═══════════════════════════════════════════════════════════════════════

FALLAS = {
    "la tarea": "CREATE TRIGGER falla BEFORE INSERT ON tareas BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
    "la fila de bandeja": "CREATE TRIGGER falla BEFORE INSERT ON bandeja BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
    "la huella de la tarea": "CREATE TRIGGER falla BEFORE INSERT ON log_acciones WHEN NEW.tabla = 'tareas' "
                             "BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
    "el comentario": "CREATE TRIGGER falla BEFORE INSERT ON comentarios_tarea BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
    "el cliente como persona": "CREATE TRIGGER falla BEFORE INSERT ON participantes BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
    "el cambio de estado de la consulta": "CREATE TRIGGER falla BEFORE UPDATE ON consultas_de_natalia "
                                          "WHEN NEW.estado = 'anotada' BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END",
}


@pytest.mark.parametrize("donde", sorted(FALLAS))
def test_G4_si_algo_falla_a_mitad_no_queda_nada_escrito_y_el_boton_sigue_sirviendo(mundo, donde):
    con, si, no = con_tarjeta(mundo)
    antes, c0 = foto(con), consulta(con)
    con.execute(FALLAS[donde])
    q = tocar(si, ANA)
    assert foto(con) == antes, f"falló {donde} y quedó algo escrito"
    assert consulta(con) == c0 and c0["estado"] == "preguntada" and c0["tarea_resultante_id"] is None
    assert q.editados == [], "la tarjeta no se cierra: no se contestó"
    assert q.avisos and "tarjeta" not in q.avisos[0].lower() and "No pude guardar" in q.avisos[0]
    # arreglada la falla, el mismo botón funciona
    con.execute("DROP TRIGGER falla")
    q = tocar(si, ANA)
    c = consulta(con)
    assert c["estado"] == "anotada" and c["tarea_resultante_id"] is not None
    assert foto(con)["tareas"] == antes["tareas"] + 1


def test_G4_el_no_tampoco_queda_a_medias(mundo):
    con, si, no = con_tarjeta(mundo)
    antes, c0 = foto(con), consulta(con)
    con.execute("CREATE TRIGGER falla BEFORE UPDATE ON consultas_de_natalia BEGIN SELECT RAISE(ABORT, 'falla inyectada'); END")
    q = tocar(no, ANA)
    assert consulta(con) == c0 and foto(con) == antes and q.editados == []


@pytest.mark.parametrize("lado", [0, 1])
def test_G4_durante_un_toque_nunca_hay_dos_conexiones_abiertas_a_la_vez(mundo, lado):
    """Dos conexiones abiertas a la vez son dos transacciones: lo que una confirma, la otra no lo deshace. El «Sí» (la
    tarea, su fila de bandeja, el comentario, la persona y el cambio de estado) y el «No» usan UNA mientras escriben (la
    relectura de después abre otra, pero cuando la primera ya cerró)."""
    con, si, no = con_tarjeta(mundo)
    mundo.base.max_profundidad = 0
    tocar((si, no)[lado], ANA)
    assert consulta(con)["estado"] == ("anotada", "rechazada")[lado]
    assert mundo.base.max_profundidad == 1


def test_G4_una_sola_transaccion_confirmada_por_cada_toque(mundo):
    """El «Sí» hace COMMIT una vez para escribir (más la relectura, que no escribe): no hay un commit intermedio
    donde la consulta ya esté `anotada` y la tarea todavía no."""
    con, si, no = con_tarjeta(mundo)
    vistos = []

    # cada COMMIT de la base: lo que un lector VE en ese instante
    original = mundo.base.connection

    def conexion():
        cm = original()

        class _CM:
            async def __aenter__(self_):
                return await cm.__aenter__()

            async def __aexit__(self_, tipo, valor, tb):
                if tipo is None and mundo.base.profundidad == 1:
                    c = consulta(con)
                    vistos.append((c["estado"], con.execute("SELECT count(*) FROM tareas WHERE id > 50").fetchone()[0]))
                return await cm.__aexit__(tipo, valor, tb)
        return _CM()
    mundo.base.connection = conexion
    tocar(si, ANA)
    # lo que se vio justo antes de confirmar: nunca `anotada` sin tarea ni tarea sin `anotada`
    assert ("anotada", 1) in vistos
    assert all((estado == "anotada") == (n == 1) for estado, n in vistos), vistos


# ═══════════════════════════════════════════════════════════════════════
# G5 · el escritor es el real, con sus tres datos
# ═══════════════════════════════════════════════════════════════════════

def test_G5_el_escritor_solo_acepta_a_natalia_o_al_panel_y_cada_uno_con_su_chat(mundo):
    con = mundo.poner_base()
    with pytest.raises(ValueError):
        correr(db.crear_tarea_desde_el_panel(ANA, "x", None, None, None, proyecto_id=1, desde="natalia"))
    with pytest.raises(ValueError):
        correr(db.crear_tarea_desde_el_panel(config.CHAT_ID_NATALIA, "x", None, None, None, proyecto_id=1))
    with pytest.raises(ValueError):
        correr(db.crear_tarea_desde_el_panel(ANA, "x", None, None, None, proyecto_id=1, desde="otro"))
    assert foto(con)["tareas"] == len(TAREAS) and foto(con)["bandeja"] == 0


def test_G5_la_tarea_de_natalia_sin_la_transaccion_del_si_no_se_escribe_nada(mundo):
    """Sin `conexion` el escritor abriría su propia conexión y haría commit: tarea de Natalia sin cambiar la consulta."""
    con = mundo.poner_base()
    with pytest.raises(ValueError):
        correr(db.crear_tarea_desde_el_panel(config.CHAT_ID_NATALIA, "x", None, None, None, proyecto_id=1, desde="natalia"))
    assert foto(con)["tareas"] == len(TAREAS) and foto(con)["bandeja"] == 0 and mundo.base.commits == 0


def test_G5_el_panel_sigue_escribiendo_como_panel(mundo):
    con = mundo.poner_base()
    tid = correr(db.crear_tarea_desde_el_panel(ANA, "Una a mano", None, None, None, proyecto_id=1))
    t = dict(con.execute("SELECT * FROM tareas WHERE id = ?", (tid,)).fetchone())
    b = dict(con.execute("SELECT * FROM bandeja WHERE id = ?", (t["bandeja_id"],)).fetchone())
    assert (b["origen"], b["tipo_entrada"], b["chat_id"]) == ("panel", "panel", ANA)
    h = dict(con.execute("SELECT * FROM log_acciones WHERE tabla = 'tareas'").fetchone())
    assert (h["actor"], h["motivo"]) == ("panel", "tarea escrita a mano desde el panel de tareas")
    assert json.loads(t["anticipos_min"]) == []


def test_G5_natalia_no_comenta_fuera_de_la_transaccion_del_si(mundo):
    con = mundo.poner_base()
    assert correr(db.comentar_tarea(11, config.CHAT_ID_NATALIA, "hola")) is None
    assert foto(con)["comentarios"] == 0


def test_G5_el_responsable_de_la_tarea_es_el_de_la_consulta(mundo):
    con, si, no = con_tarjeta(mundo, responsable=BETO)           # el responsable lo dice quien abre; vacío = el del proyecto
    assert mundo.a(BETO) and not mundo.a(ANA)
    tocar(si, BETO)
    tid = consulta(con)["tarea_resultante_id"]
    assert con.execute("SELECT responsable_chat_id FROM tareas WHERE id = ?", (tid,)).fetchone()[0] == BETO
    assert "Confirmó Beto Inventado." in con.execute("SELECT texto FROM comentarios_tarea").fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════
# G6 · si el escritor rechaza, `no_se_pudo` con motivo y cero tareas
# ═══════════════════════════════════════════════════════════════════════

def test_G6_el_proyecto_se_cerro_antes_del_si(mundo):
    con, si, no = con_tarjeta(mundo)
    antes = foto(con)
    con.execute("UPDATE proyectos SET estado = ? WHERE id = 1", (db.ESTADO_PROYECTO_CERRADO,))
    q = tocar(si, ANA)
    c = consulta(con)
    assert c["estado"] == "no_se_pudo" and c["tarea_resultante_id"] is None and "cerrado" in c["motivo"]
    assert c["contestada_por_chat_id"] == ANA and c["contestada_en"] is not None
    assert foto(con) == antes, "rechazada por el escritor y quedó algo escrito"
    assert "No se pudo anotar" in q.texto_final and "cerrado" in q.texto_final and "Anotada" not in q.texto_final
    assert q.editados[-1]["reply_markup"] is None
    # y el otro botón ya no cambia nada
    tocar(no, ANA)
    assert consulta(con) == c and foto(con) == antes


def test_G6_el_proyecto_ya_no_existe(mundo):
    con, si, no = con_tarjeta(mundo)
    antes = foto(con)
    con.execute("UPDATE proyectos SET borrado_en = CURRENT_TIMESTAMP WHERE id = 1")
    q = tocar(si, ANA)
    c = consulta(con)
    assert c["estado"] == "no_se_pudo" and c["tarea_resultante_id"] is None and c["motivo"]
    assert foto(con) == antes and "No se pudo anotar" in q.texto_final


def test_G6_el_responsable_dejo_de_poder_serlo(mundo):
    con, si, no = con_tarjeta(mundo)
    antes = foto(con)
    guardada, db.puede_ser_responsable = db.puede_ser_responsable, lambda chat: False     # la puerta, cerrada
    try:
        q = tocar(si, ANA)
    finally:
        db.puede_ser_responsable = guardada
    c = consulta(con)
    assert c["estado"] == "no_se_pudo" and c["tarea_resultante_id"] is None and c["motivo"]
    assert foto(con) == antes and "No se pudo anotar" in q.texto_final


# ═══════════════════════════════════════════════════════════════════════
# El sistema no miente
# ═══════════════════════════════════════════════════════════════════════

def test_la_tarjeta_solo_dice_anotada_si_la_tarea_esta_releida(mundo):
    """Si justo al confirmarse la tarea desaparece (la mandan a la papelera), la tarjeta NO dice «Anotada»: dice que no
    pudo confirmarlo."""
    con, si, no = con_tarjeta(mundo)
    con.execute("CREATE TRIGGER se_va AFTER UPDATE ON consultas_de_natalia WHEN NEW.estado = 'anotada' BEGIN "
                "UPDATE tareas SET borrado_en = CURRENT_TIMESTAMP WHERE id = NEW.tarea_resultante_id; END")
    q = tocar(si, ANA)
    assert consulta(con)["estado"] == "anotada"
    assert "Anotada" not in q.texto_final and "No pude confirmar" in q.texto_final


def test_los_motivos_y_los_nombres_con_html_se_escapan(mundo):
    con, si, no = con_tarjeta(mundo)
    con.execute("UPDATE proyectos SET estado = ? WHERE id = 1", (db.ESTADO_PROYECTO_CERRADO,))
    con.execute("CREATE TRIGGER x AFTER UPDATE ON consultas_de_natalia BEGIN "
                "UPDATE consultas_de_natalia SET motivo = '<i>raro</i>' WHERE id = NEW.id AND estado = 'no_se_pudo'; END")
    q = tocar(si, ANA)
    assert "&lt;i&gt;raro&lt;/i&gt;" in q.texto_final and "<i>raro</i>" not in q.texto_final


# ═══════════════════════════════════════════════════════════════════════
# Lo que NO se contesta con un botón
# ═══════════════════════════════════════════════════════════════════════

def test_una_consulta_que_no_existe_o_es_de_fecha_o_aun_no_tiene_tarjeta_no_cambia_nada(mundo):
    con = mundo.poner_base()
    abrir_nueva_fecha()                               # consulta 1: nueva_fecha (no tiene Sí/No)
    abrir_tarea_nueva(clave="k-2")                    # consulta 2: abierta, su tarjeta aún no salió
    antes = foto(con)
    c1, c2 = consulta(con, 1), consulta(con, 2)
    for data, aviso in (("cns:99", "ya no existe"), ("cns:1", "no se contesta"), ("cnn:2", "todavía se está preparando")):
        q = tocar(data, ANA)
        assert q.editados == [] and any(aviso in a for a in q.avisos), (data, q.avisos)
    assert consulta(con, 1) == c1 and consulta(con, 2) == c2 and foto(con) == antes


def test_un_boton_mal_formado_no_toca_nada(mundo):
    con, si, no = con_tarjeta(mundo)
    antes = foto(con)
    for data in ("cns:", "cns:abc", "cns", "cnn:1.5"):
        q = tocar(data, ANA)
        assert q.editados == []
    assert foto(con) == antes and consulta(con)["estado"] == "preguntada"


def test_sin_la_migracion_se_dice_y_no_se_rompe(mundo):
    mundo.poner_base(con_tabla=False)
    q = tocar("cns:1", ANA)
    assert q.editados == [] and any("todavía no están disponibles" in a for a in q.avisos)


def test_el_recordatorio_no_repite_los_botones_y_la_nueva_fecha_no_lleva(mundo):
    con, si, no = con_tarjeta(mundo)
    abrir_nueva_fecha()
    vuelta(mundo)
    sin = [e for e in mundo.enviados if "reply_markup" not in e]
    assert any("Grabar las voces" in e["text"] for e in sin)           # la tarjeta de nueva_fecha: sin botones
    vuelta(mundo, ahora=datetime(2026, 3, 10, 16, 0, tzinfo=UTC))        # una hora después: el recordatorio
    recordatorios = [e for e in mundo.enviados if e["text"].startswith("⏰")]
    assert recordatorios and all("reply_markup" not in e for e in recordatorios)


# ═══════════════════════════════════════════════════════════════════════
# Los hermanos: nada que salga de una lista tecleada
# ═══════════════════════════════════════════════════════════════════════

def test_los_callbacks_de_las_consultas_los_arma_botones_y_los_lee_al_pulsar_con_los_mismos_prefijos():
    teclado = botones.teclado_de_consulta(77)
    datos = [b.callback_data for fila in teclado.inline_keyboard for b in fila]
    fuente = ast.parse(inspect.getsource(botones.al_pulsar))
    comparados = {c.value for n in ast.walk(fuente) if isinstance(n, ast.Compare)
                  and isinstance(n.left, ast.Name) and n.left.id == "accion"
                  for c in n.comparators if isinstance(c, ast.Constant)}
    assert {d.split(":")[0] for d in datos} <= comparados
    assert [d.split(":")[1] for d in datos] == ["77", "77"]


def test_la_unica_funcion_que_escribe_con_natalia_de_autora_es_la_del_si():
    """Quién puede llamar a `contestar_consulta_de_natalia`: solo `botones._contestar_consulta`. Sacado del árbol
    sintáctico de todo `.py` fuera de `tests/`. FRONTERA: no ve un llamado por `getattr` ni por alias."""
    from test_buzon_que_no_se_ve import _py_en_disco
    llamadores = set()
    for ruta in _py_en_disco(_ROOT):
        rel = pathlib.Path(ruta).resolve().relative_to(_ROOT).as_posix()
        if rel.startswith("tests/"):
            continue
        for fn in ast.walk(ast.parse(pathlib.Path(ruta).read_text(encoding="utf-8"))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call):
                        nombre = (n.func.attr if isinstance(n.func, ast.Attribute)
                                  else n.func.id if isinstance(n.func, ast.Name) else None)
                        if nombre == "contestar_consulta_de_natalia":
                            llamadores.add((rel, fn.name))
    assert llamadores == {("acciones/botones.py", "_contestar_consulta")}


def test_quien_pasa_desde_natalia_al_escritor_de_tareas_es_solo_el_si():
    from test_buzon_que_no_se_ve import _py_en_disco
    llamadores = set()
    for ruta in _py_en_disco(_ROOT):
        rel = pathlib.Path(ruta).resolve().relative_to(_ROOT).as_posix()
        if rel.startswith("tests/"):
            continue
        for fn in ast.walk(ast.parse(pathlib.Path(ruta).read_text(encoding="utf-8"))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None))
                            == "crear_tarea_desde_el_panel" and any(k.arg == "desde" for k in n.keywords)):
                        llamadores.add((rel, fn.name))
    assert llamadores == {("db/db.py", "_anotar_la_tarea_de_la_consulta")}


def test_el_humo_corre_la_lectura_del_boton_y_esa_lectura_devuelve_none_para_un_id_que_no_existe(mundo):
    texto = (_ROOT / "tools" / "humo.py").read_text(encoding="utf-8")
    assert texto.count('("consulta_de_natalia_leida", lambda: db.consulta_de_natalia_leida(0))') == 1
    mundo.poner_base()
    assert correr(db.consulta_de_natalia_leida(0)) is None
    abrir_tarea_nueva()
    assert correr(db.consulta_de_natalia_leida(1))["estado"] == "abierta"
