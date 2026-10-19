# -*- coding: utf-8 -*-
"""Las consultas de Natalia al responsable (18-oct-2026, pieza L3 de `disenos/natalia-tareas-en-proyectos/DISENO.md`):
la tabla `consultas_de_natalia`, `POST /api/code/clientes/{noco_id}/consultas` (permiso `consultas:abrir`) y
`GET /api/code/clientes/{noco_id}/consultas?ids=…` (permiso `consultas:leer`).

CAMINO DE PRODUCCIÓN: la app FastAPI real, claves y permisos reales de `config`, las funciones REALES de `db/db.py`
(`abrir_consulta_de_natalia`, `consultas_de_natalia_de_cliente`, `pasar_consulta_de_natalia`,
`proyectos_de_cliente`, `situacion_de_proyecto`, `proyecto_admite_tareas`) ejecutando su SQL de verdad contra
SQLite en memoria (`%s` -> `?`), y las tablas armadas con el DDL REAL de `db/migrations/2026-10-18_consultas_de_natalia.sql`
(traducido: BIGSERIAL, TIMESTAMPTZ y `now()`), de modo que los CHECK y los índices que se prueban son los del archivo.
Lo único que se sustituye es el `pool` (la conexión), con DOS cosas dichas: (1) su cursor cede el control del bucle
en cada `execute` (`await asyncio.sleep(0)`) para que dos pedidos a la vez se crucen de verdad; (2) una tabla que no
existe se traduce al error que psycopg da (`UndefinedTable`, SQLSTATE `42P01`, comprobado contra psycopg 3.2.3).
En la comparación con el panel (G1) el escritor de la tarea (`db.crear_tarea_desde_el_panel`) es un registro de
llamadas: lo que se compara es lo que la ruta del panel valida ANTES de escribir.

FRONTERA: SQLite no es Postgres (no entiende `ANY`, los tipos son laxos, no hay FK); lo que se prueba es que el
texto del SQL y sus parámetros dicen lo que dicen. `tools/humo.py` corre estas consultas contra Postgres de verdad.

NINGÚN nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_api_code_consultas.py -q
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-consultas-de-natalia")
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

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.api_code as api_code  # noqa: E402
import web.auth as auth  # noqa: E402
from acciones import crud  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENTE = TestClient(panel.app)

CLAVE_NATALIA = "clave-de-prueba-natalia-consultas-no-es-real"
CLAVE_SOLO_ABRIR = "clave-de-prueba-natalia-solo-abrir-no-es-real"
CLAVE_SOLO_LEER = "clave-de-prueba-natalia-solo-leer-no-es-real"
CLAVE_SALA = "clave-de-prueba-sala-consultas-no-es-real"
ABRIR, LEER = "consultas:abrir", "consultas:leer"
RUTA = "/api/code/clientes/{}/consultas"

A, B, SIN_PROYECTOS = 5001, 5002, 5003            # fichas de clientes inventadas
ANA, BETO, FUERA = 7710001, 7710002, 7710003      # chats inventados (FUERA tiene nombre y no entra al panel)

AHORA = datetime.now(timezone.utc)
MIGRACION = _ROOT / "db" / "migrations" / "2026-10-18_consultas_de_natalia.sql"
SCHEMA = _ROOT / "db" / "schema.sql"


def en(dias: int) -> str:
    return (AHORA + timedelta(days=dias)).isoformat()


def dia(dias: int) -> str:
    """El día de Santo Domingo de `en(dias)`, escrito aparte de `db.dia_rd`."""
    return (AHORA + timedelta(days=dias)).astimezone(config.TZ).date().isoformat()


# ── Una base SQLite con las tablas que esto toca, la de consultas con el DDL REAL ─────────────

sqlite3.register_converter(
    "TSTZ", lambda b: datetime.fromisoformat(b.decode()).astimezone(timezone.utc))


class _SinTabla(Exception):
    """Lo que psycopg lanza cuando la tabla no existe: `psycopg.errors.UndefinedTable`, SQLSTATE 42P01."""
    sqlstate = "42P01"


class _Cur:
    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        await asyncio.sleep(0)       # cede el bucle: dos pedidos a la vez se cruzan aquí, como con la red
        pasos = tuple(p.isoformat() if isinstance(p, datetime) else p for p in (params or ()))
        try:
            cur = self._con.execute(sql.replace("%s", "?"), pasos)
        except sqlite3.OperationalError as e:
            if "no such table: consultas_de_natalia" in str(e):
                raise _SinTabla(str(e)) from None
            raise
        self._filas = [dict(f) for f in cur.fetchall()] if cur.description else []
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


def _sin_comentarios(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def ddl_de_la_migracion(ruta: pathlib.Path = MIGRACION) -> list[str]:
    """Las sentencias de la migración REAL, traducidas a SQLite (solo tipos y `now()`): sin BEGIN/COMMIT."""
    texto = _sin_comentarios(ruta.read_text(encoding="utf-8"))
    sentencias = []
    for s in texto.split(";"):
        s = " ".join(s.split())
        if not s or s.upper() in ("BEGIN", "COMMIT"):
            continue
        s = (s.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
              .replace("TIMESTAMPTZ", "TSTZ").replace("BIGINT", "INTEGER")
              .replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"))
        sentencias.append(s)
    return sentencias


ESQUEMA = """
CREATE TABLE proyectos (
  id INTEGER PRIMARY KEY, nombre TEXT, descripcion TEXT, estado TEXT DEFAULT 'activo',
  area TEXT, creado_en TSTZ, responsable_chat_id INTEGER, cliente_nombre TEXT,
  cliente_noco_id INTEGER, borrado_en TSTZ);
CREATE TABLE tareas (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, area TEXT, titulo TEXT, estado TEXT,
  vence_en TSTZ, completado_en TSTZ, creado_en TSTZ, responsable_chat_id INTEGER,
  borrado_en TSTZ, grave INTEGER NOT NULL DEFAULT 0);
CREATE TABLE participantes (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, tarea_id INTEGER, noco_id INTEGER,
  nombre TEXT, rol TEXT, borrado_en TSTZ);
CREATE TABLE log_acciones (id INTEGER PRIMARY KEY AUTOINCREMENT, accion TEXT);
"""

# (id, nombre, estado, responsable, cliente, borrado)
PROYECTOS = [
    (1, "Proyecto Alfa", "activo", ANA, A, None),
    (2, "Proyecto Beta", "pausado", config.CHAT_ID_CODE, A, None),
    (3, "Proyecto Cerrado", "cerrado", None, A, None),
    (4, "Proyecto en la papelera", "activo", ANA, A, en(-1)),
    (5, "Proyecto Gamma", "activo", BETO, B, None),
    (6, "Proyecto interno IA", "activo", ANA, None, None),
    (7, "Proyecto Delta", "activo", FUERA, A, None),
]
# (id, proyecto, titulo, estado, vence, completado, borrado, responsable)
TAREAS = [
    (10, 1, "Tarea hecha de Alfa", "hecha", None, en(-5), None, None),
    (11, 1, "Mezclar el tema", "pendiente", en(3), None, None, None),
    (12, 1, "Grabar las voces", "pendiente", en(-3), None, None, None),
    (13, 1, "Tarea sin fecha de Alfa", "pendiente", None, None, None, None),
    (14, 1, "Tarea borrada de Alfa", "pendiente", en(-10), None, en(-1), None),
    (20, 2, "Entregar el master", "pendiente", en(5), None, None, None),
    (30, 3, "Pendiente de un proyecto cerrado", "pendiente", en(-4), None, None, None),
    (40, 4, "Tarea de la papelera", "pendiente", en(-4), None, None, None),
    (50, 5, "SECRETO-DE-OTRO-CLIENTE", "pendiente", en(2), None, None, None),
    (51, 5, "SECRETO-VENCIDO-DE-OTRO-CLIENTE", "pendiente", en(-2), None, None, None),
    (60, 6, "Tarea interna de IA", "pendiente", en(-2), None, None, None),
    (70, 7, "Pendiente vencida de Delta", "pendiente", en(-2), None, None, None),
]


def _base(proyectos=PROYECTOS, tareas=TAREAS, con_tabla=True):
    con = sqlite3.connect(":memory:", check_same_thread=False,
                          detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    if con_tabla:
        for s in ddl_de_la_migracion():
            con.execute(s)
    for pid, nombre, estado, resp, cliente, borrado in proyectos:
        con.execute(
            "INSERT INTO proyectos (id, nombre, estado, responsable_chat_id, cliente_nombre, "
            "cliente_noco_id, creado_en, borrado_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, nombre, estado, resp, None if cliente is None else f"Cliente {cliente}",
             cliente, en(-30), borrado))
    for tid, pid, titulo, estado, vence, completado, borrado, resp in tareas:
        con.execute(
            "INSERT INTO tareas (id, proyecto_id, titulo, estado, vence_en, completado_en, "
            "creado_en, borrado_en, responsable_chat_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, pid, titulo, estado, vence, completado, en(-20), borrado, resp))
    con.commit()
    return con


@pytest.fixture
def natalia():
    """Claves (Natalia con los dos permisos nuevos, una con solo abrir, una con solo leer, la sala sin ellos),
    dos personas que entran al panel y la base armada con `poner_base`. Restaura todo al salir."""
    claves0, permisos0 = config.CLAVES_API_CODE, config.PERMISOS_API_CODE
    nombres0, permitidos0, pool0 = config.NOMBRES_POR_CHAT, config.CHAT_IDS_PERMITIDOS, db.pool
    config.CLAVES_API_CODE = {CLAVE_NATALIA: "natalia", CLAVE_SOLO_ABRIR: "natalia_abre",
                              CLAVE_SOLO_LEER: "natalia_lee", CLAVE_SALA: "sala_mac"}
    config.PERMISOS_API_CODE = {
        "natalia": frozenset({"alertas:crear", "proyectos:leer_de_cliente", ABRIR, LEER}),
        "natalia_abre": frozenset({ABRIR}),
        "natalia_lee": frozenset({LEER}),
        "sala_mac": frozenset({"tareas:listar", "tareas:cerrar", "tareas:tomar",
                               "comentarios:leer", "comentarios:escribir"})}
    config.NOMBRES_POR_CHAT = {ANA: "Ana Inventada", BETO: "Beto Inventado", FUERA: "Fuera Inventado"}
    config.CHAT_IDS_PERMITIDOS = (ANA, BETO)
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0

    def poner_base(**kw):
        con = _base(**kw)
        db.pool = _Pool(con)
        api_code._pedidos_por_quien.clear()
        return con
    poner_base()
    try:
        yield poner_base
    finally:
        config.CLAVES_API_CODE, config.PERMISOS_API_CODE = claves0, permisos0
        config.NOMBRES_POR_CHAT, config.CHAT_IDS_PERMITIDOS, db.pool = nombres0, permitidos0, pool0


def _h(clave=CLAVE_NATALIA):
    return {"Authorization": f"Bearer {clave}"}


def nueva(clave="k-1", proyecto_id=1, titulo="Subir el demo", pedido="El cliente quiere el demo el viernes",
          vence="", responsable="", **resto):
    return {"clave": clave, "tipo": "tarea_nueva", "proyecto_id": proyecto_id, "titulo": titulo,
            "pedido": pedido, "vence": vence, "responsable": responsable, **resto}


def fecha_nueva(clave="f-1", tarea_id=12):
    return {"clave": clave, "tipo": "nueva_fecha", "tarea_id": tarea_id}


def abrir(cuerpo, noco_id=A, clave=CLAVE_NATALIA):
    api_code._pedidos_por_quien.clear()
    return CLIENTE.post(RUTA.format(noco_id), json=cuerpo, headers=_h(clave))


def leer(ids, noco_id=A, clave=CLAVE_NATALIA):
    api_code._pedidos_por_quien.clear()
    ids = ",".join(str(i) for i in ids) if not isinstance(ids, str) else ids
    return CLIENTE.get(RUTA.format(noco_id), params={"ids": ids}, headers=_h(clave))


def filas(con, donde="1=1", *params):
    return [dict(f) for f in con.execute(f"SELECT * FROM consultas_de_natalia WHERE {donde} ORDER BY id", params)]


def _valores_de(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _valores_de(v)
    elif isinstance(x, list):
        for v in x:
            yield from _valores_de(v)
    else:
        yield x


def _sembrar_consulta(con, estado="abierta", tipo="tarea_nueva", clave="s-1", cliente=A, proyecto=1,
                      tarea_id=None, resultante=None, resp=ANA, titulo="Titulo sembrado",
                      pedido="Pedido sembrado"):
    """Una consulta puesta directo en la tabla (para probar la lectura en cada estado); el CHECK real manda."""
    if tipo == "nueva_fecha":
        titulo = pedido = None
    cur = con.execute(
        "INSERT INTO consultas_de_natalia (clave, tipo, estado, cliente_noco_id, proyecto_id, titulo, pedido, "
        "tarea_id, vence_vencida_en, responsable_chat_id, tarea_resultante_id, motivo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clave, tipo, estado, cliente, proyecto, titulo, pedido, tarea_id,
         en(-3) if tipo == "nueva_fecha" else None, resp, resultante, "MOTIVO-INTERNO"))
    con.commit()
    return cur.lastrowid


CAMPOS_CONSULTA = {"consulta_id", "clave", "tipo", "estado", "proyecto_id", "titulo", "tarea_id",
                   "tarea", "siguiente"}
CAMPOS_TAREA = {"tarea_id", "titulo", "fecha", "vencida", "pendiente"}
CAMPOS_SIGUIENTE = {"tarea_id", "titulo", "fecha", "vencida"}


# ═══════════════════════════════════════════════════════════════════════
# Lo básico: abrir una consulta de cada tipo
# ═══════════════════════════════════════════════════════════════════════

def test_abrir_una_tarea_nueva_guarda_una_fila_abierta_y_responde_lo_leido(natalia):
    con = natalia()
    r = abrir(nueva(vence="2027-01-15", responsable="Beto Inventado"))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == CAMPOS_CONSULTA | {"reusada"}
    assert (cuerpo["tipo"], cuerpo["estado"], cuerpo["proyecto_id"], cuerpo["titulo"], cuerpo["reusada"]) == (
        "tarea_nueva", "abierta", 1, "Subir el demo", False)
    [f] = filas(con)
    assert (f["clave"], f["cliente_noco_id"], f["proyecto_id"], f["pedido"], f["responsable_chat_id"]) == (
        "k-1", A, 1, "El cliente quiere el demo el viernes", BETO)
    assert f["id"] == cuerpo["consulta_id"] and f["estado"] == "abierta" and f["tarea_id"] is None
    # la fecha propuesta se guarda como instante (23:59 de Santo Domingo), igual que «Agregar tarea»
    assert db.dia_rd(f["vence_en"]).isoformat() == "2027-01-15"
    assert f["vence_en"].astimezone(config.TZ).hour == 23 and f["vence_en"].astimezone(config.TZ).minute == 59


def test_sin_responsable_dicho_se_le_pregunta_al_del_proyecto(natalia):
    con = natalia()
    assert abrir(nueva()).status_code == 200
    assert filas(con)[0]["responsable_chat_id"] == ANA          # el proyecto 1 es de Ana


def test_abrir_una_nueva_fecha_de_la_siguiente_vencida(natalia):
    con = natalia()
    r = abrir(fecha_nueva(tarea_id=12))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["tipo"], cuerpo["estado"], cuerpo["tarea_id"], cuerpo["proyecto_id"], cuerpo["titulo"]) == (
        "nueva_fecha", "abierta", 12, 1, None)
    [f] = filas(con)
    assert f["tarea_id"] == 12 and f["proyecto_id"] == 1 and f["cliente_noco_id"] == A
    assert f["titulo"] is None and f["pedido"] is None and f["vence_vencida_en"] is not None
    assert db.dia_rd(f["vence_vencida_en"]).isoformat() == dia(-3)
    assert f["responsable_chat_id"] == ANA


def test_nueva_fecha_le_pregunta_al_responsable_de_la_tarea_y_si_no_puede_al_del_proyecto(natalia):
    con = natalia()
    con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 12", (BETO,))
    con.commit()
    assert abrir(fecha_nueva("f-beto")).status_code == 200
    assert filas(con)[0]["responsable_chat_id"] == BETO
    # el de la tarea es Code (no tiene Telegram): pasa al del proyecto (Ana)
    con.execute("DELETE FROM consultas_de_natalia")
    con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 12", (config.CHAT_ID_CODE,))
    con.commit()
    assert abrir(fecha_nueva("f-ana")).status_code == 200
    assert filas(con)[0]["responsable_chat_id"] == ANA


# ═══════════════════════════════════════════════════════════════════════
# G1: la validación es la del panel. Un cruce de entradas da el mismo sí/no que `agregar_tarea_al_proyecto`
# ═══════════════════════════════════════════════════════════════════════

PROYECTOS_DEL_CRUCE = {1: True, 3: False, 4: False, 999: False}          # id -> ¿vale?
TITULOS_DEL_CRUCE = {"Mezclar las voces": True, "": False, "   ": False, "x" * 201: False, "x" * 200: True}
FECHAS_DEL_CRUCE = {"": True, "2027-01-15": True, "2026-13-45": False, "basura": False, "2019-05-01": False,
                    "15/01/2027": False}
RESPONSABLES_DEL_CRUCE = {"": True, "Ana Inventada": True, "ana inventada": True, "Beto Inventado": True,
                          "Nadie Conocido": False, "Fuera Inventado": False}


def _ruta_del_panel(monkeypatch):
    """La ruta REAL `POST /proyectos/{pid}/tareas` con sesión de Ana; solo el escritor de la tarea es un registro
    (lo que se compara es lo que la ruta valida ANTES de escribir)."""
    escritas = []

    async def escritor(chat, titulo, vence_en, area, responsable, **kw):
        escritas.append((titulo, vence_en, responsable, kw))
        return 900 + len(escritas)
    monkeypatch.setattr(db, "crear_tarea_desde_el_panel", escritor)
    c = TestClient(panel.app, follow_redirects=False)
    c.cookies.set(panel.COOKIE, auth.crear_token(ANA, auth.VIDA_SESION))

    def pedir(pid, titulo, vence, responsable):
        antes = len(escritas)
        r = c.post(f"/proyectos/{pid}/tareas", data={"titulo": titulo, "vence": vence, "responsable": responsable})
        assert r.status_code == 303, r.status_code
        return len(escritas) > antes
    return pedir


def test_G1_el_cruce_de_entradas_da_el_mismo_si_o_no_en_el_panel_y_en_la_consulta(natalia, monkeypatch):
    """TODAS las combinaciones de proyecto × título × fecha × responsable, por las DOS rutas reales. El esperado
    se escribe aparte (los diccionarios de arriba) y las dos deben coincidir con él y entre sí."""
    natalia()
    panel_dice = _ruta_del_panel(monkeypatch)
    n = 0
    for pid, p_ok in PROYECTOS_DEL_CRUCE.items():
        for titulo, t_ok in TITULOS_DEL_CRUCE.items():
            for vence, f_ok in FECHAS_DEL_CRUCE.items():
                for resp, r_ok in RESPONSABLES_DEL_CRUCE.items():
                    n += 1
                    esperado = p_ok and t_ok and f_ok and r_ok
                    en_el_panel = panel_dice(pid, titulo, vence, resp)
                    r = abrir(nueva(clave=f"cruce-{n}", proyecto_id=pid, titulo=titulo, vence=vence,
                                    responsable=resp))
                    en_la_consulta = r.status_code == 200
                    assert (en_el_panel, en_la_consulta) == (esperado, esperado), (
                        pid, titulo[:12], vence, resp, "panel", en_el_panel, "consulta", r.status_code, r.text)
    assert n == len(PROYECTOS_DEL_CRUCE) * len(TITULOS_DEL_CRUCE) * len(FECHAS_DEL_CRUCE) * len(
        RESPONSABLES_DEL_CRUCE) == 4 * 5 * 6 * 6


def test_G1_el_cruce_cuenta_las_filas_que_valian(natalia):
    """Las combinaciones válidas son exactamente las filas guardadas: lo que no valía no dejó nada."""
    con = natalia()
    validas = 0
    for n, (titulo, t_ok) in enumerate(TITULOS_DEL_CRUCE.items()):
        for m, (vence, f_ok) in enumerate(FECHAS_DEL_CRUCE.items()):
            for k, (resp, r_ok) in enumerate(RESPONSABLES_DEL_CRUCE.items()):
                r = abrir(nueva(clave=f"c-{n}-{m}-{k}", titulo=titulo, vence=vence, responsable=resp))
                validas += r.status_code == 200
    assert validas == sum(TITULOS_DEL_CRUCE.values()) * sum(FECHAS_DEL_CRUCE.values()) * sum(
        RESPONSABLES_DEL_CRUCE.values()) == 2 * 2 * 4
    assert len(filas(con)) == validas


@pytest.mark.parametrize("pedido, vale", [("Lo que pidió el cliente", True), ("", False), ("   ", False),
                                          ("x" * db.LARGO_PEDIDO_DE_CONSULTA, True),
                                          ("x" * (db.LARGO_PEDIDO_DE_CONSULTA + 1), False)])
def test_G1_el_pedido_no_puede_quedar_vacio_ni_pasar_del_tope_y_no_se_corta(natalia, pedido, vale):
    con = natalia()
    r = abrir(nueva(pedido=pedido))
    assert r.status_code == (200 if vale else 422), r.text
    assert len(filas(con)) == (1 if vale else 0)
    if vale:
        assert filas(con)[0]["pedido"] == pedido.strip()


def test_G1_el_tope_del_pedido_deja_sitio_en_el_comentario_de_la_tarea():
    """El pedido viaja luego como comentario (L5): el tope deja holgura bajo `LARGO_COMENTARIO`."""
    assert db.LARGO_PEDIDO_DE_CONSULTA + 200 <= db.LARGO_COMENTARIO


def test_G1_un_proyecto_cerrado_es_409_y_uno_que_no_existe_o_esta_en_la_papelera_es_404(natalia):
    con = natalia()
    assert abrir(nueva(proyecto_id=3)).status_code == 409
    assert abrir(nueva(proyecto_id=4)).status_code == 404
    assert abrir(nueva(proyecto_id=999)).status_code == 404
    assert filas(con) == []


@pytest.mark.parametrize("cuerpo", [
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": 1, "titulo": "t", "pedido": "p", "otro": 1},
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": "1", "titulo": "t", "pedido": "p"},
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": True, "titulo": "t", "pedido": "p"},
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": 1, "titulo": "t"},
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": 1, "titulo": "t", "pedido": "p", "tarea_id": 12},
    {"clave": "k", "tipo": "nueva_fecha", "tarea_id": 12, "titulo": "t"},
    {"clave": "k", "tipo": "nueva_fecha", "tarea_id": "12"},
    {"clave": "k", "tipo": "nueva_fecha"},
    {"clave": "k", "tipo": "otro", "tarea_id": 12},
    {"clave": "k"},
    {"clave": 5, "tipo": "nueva_fecha", "tarea_id": 12},
    {"clave": "", "tipo": "nueva_fecha", "tarea_id": 12},
    {"clave": "con espacio", "tipo": "nueva_fecha", "tarea_id": 12},
    {"clave": "x" * 121, "tipo": "nueva_fecha", "tarea_id": 12},
    {"clave": "k", "tipo": "tarea_nueva", "proyecto_id": 1, "titulo": "t\x00", "pedido": "p"},
])
def test_G1_un_pedido_mal_formado_es_422_y_no_toca_la_base(natalia, cuerpo):
    con = natalia()
    antes = con.total_changes
    assert abrir(cuerpo).status_code == 422, cuerpo
    assert con.total_changes == antes


# ═══════════════════════════════════════════════════════════════════════
# G2: un cliente solo abre y lee consultas de SU proyecto
# ═══════════════════════════════════════════════════════════════════════

def test_G2_abrir_una_tarea_nueva_en_un_proyecto_de_otra_ficha_es_404_y_cero_filas(natalia):
    con = natalia()
    for ficha, proyecto in ((A, 5), (B, 1), (A, 6), (SIN_PROYECTOS, 1)):
        r = abrir(nueva(clave=f"ajeno-{ficha}-{proyecto}", proyecto_id=proyecto), noco_id=ficha)
        assert r.status_code == 404, (ficha, proyecto, r.status_code)
        for secreto in ("Gamma", "Alfa", "interno", "SECRETO"):
            assert secreto not in r.text
    assert filas(con) == []


def test_G2_abrir_una_nueva_fecha_de_una_tarea_de_otra_ficha_es_404_y_cero_filas(natalia):
    """La 51 es la siguiente vencida del proyecto de B; la 60, de un proyecto sin cliente. Desde A no existen."""
    con = natalia()
    for tarea in (51, 60, 50, 999):
        r = abrir(fecha_nueva(clave=f"t-{tarea}", tarea_id=tarea), noco_id=A)
        assert r.status_code == 404, (tarea, r.status_code)
        assert "SECRETO" not in r.text
    assert filas(con) == []
    # y B sí puede con la suya
    assert abrir(fecha_nueva("de-b", 51), noco_id=B).status_code == 200


def test_G2_leer_solo_devuelve_las_de_esa_ficha_y_un_id_ajeno_da_lo_mismo_que_uno_inventado(natalia):
    con = natalia()
    mia = _sembrar_consulta(con, clave="m-1", cliente=A, proyecto=1)
    ajena = _sembrar_consulta(con, clave="a-1", cliente=B, proyecto=5)
    r = leer([mia, ajena, 987654])
    assert [c["consulta_id"] for c in r.json()["consultas"]] == [mia]
    assert "a-1" not in r.text and "Gamma" not in r.text
    # desde B se ve la suya y no la de A
    assert [c["consulta_id"] for c in leer([mia, ajena], noco_id=B).json()["consultas"]] == [ajena]
    # el de otra ficha y el inventado son indistinguibles
    assert leer([ajena]).json() == leer([987654]).json() == {"noco_id": A, "consultas": []}


def test_G2_la_misma_clave_de_otro_cliente_es_409_sin_decir_de_quien_y_no_cambia_nada(natalia):
    con = natalia()
    assert abrir(nueva("compartida", proyecto_id=1), noco_id=A).status_code == 200
    antes = filas(con)
    r = abrir(nueva("compartida", proyecto_id=5), noco_id=B)
    assert r.status_code == 409
    for dato in ("Alfa", "Subir el demo", str(A), "demo"):
        assert dato not in r.text, dato
    assert filas(con) == antes
    assert leer([antes[0]["id"]], noco_id=B).json()["consultas"] == []


def test_G2_otro_cliente_que_manda_el_mismo_pedido_con_la_misma_clave_no_recibe_la_consulta_ajena(natalia):
    """El ataque de verdad: B manda EXACTAMENTE el cuerpo de A (misma clave, mismo proyecto, mismo texto). La
    búsqueda por clave ocurre antes de validar el proyecto, así que sin el cerrojo de la ficha le devolvería la
    consulta de A, con su título y su estado."""
    con = natalia()
    cuerpo = nueva("identica", proyecto_id=1, titulo="Titulo-solo-de-A", pedido="Pedido-solo-de-A")
    assert abrir(cuerpo, noco_id=A).status_code == 200
    r = abrir(cuerpo, noco_id=B)
    assert r.status_code == 409, r.text
    assert "solo-de-A" not in r.text and "consulta_id" not in r.text
    assert len(filas(con)) == 1 and filas(con)[0]["cliente_noco_id"] == A


def test_G2_la_tarea_resultante_de_una_anotada_solo_sale_si_cuelga_de_un_proyecto_de_esa_ficha(natalia):
    """Una fila forjada: la consulta es de A pero su `tarea_resultante_id` es la tarea 50 del proyecto de B."""
    con = natalia()
    buena = _sembrar_consulta(con, "anotada", clave="b", resultante=11)
    mala = _sembrar_consulta(con, "anotada", clave="m", resultante=50)
    por_id = {c["consulta_id"]: c for c in leer([buena, mala]).json()["consultas"]}
    assert por_id[buena]["tarea"]["titulo"] == "Mezclar el tema"
    assert por_id[mala]["tarea"] is None
    assert "SECRETO" not in leer([buena, mala]).text


def test_G2_los_hermanos_las_rutas_con_ficha_son_estas_y_las_dos_de_consultas_filtran_por_ella():
    """HERMANOS, SACADOS DE LO REAL: toda ruta de `/api/code` con `{noco_id}`. El día que haya otra, esta prueba
    se rompe hasta que se le escriba su «otro cliente no la ve». Y cada función de `db` que la usa lleva
    `cliente_noco_id = %s` en su propio SQL."""
    con_ficha = sorted((m, p, perm) for m, p, perm in api_code.rutas_registradas(panel.app) if "{noco_id}" in p)
    assert [(m, p) for m, p, _ in con_ficha] == [
        ("GET", "/api/code/clientes/{noco_id}/consultas"), ("GET", "/api/code/clientes/{noco_id}/proyectos"),
        ("POST", "/api/code/clientes/{noco_id}/consultas")]
    arbol = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    for nombre in ("proyectos_de_cliente", "consultas_de_natalia_de_cliente"):
        fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == nombre)
        sqls = [c.value for c in ast.walk(fn) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                and re.search(r"\bSELECT\b", c.value)]
        assert sqls and all("cliente_noco_id = %s" in s for s in sqls), nombre


# ═══════════════════════════════════════════════════════════════════════
# G3: responsable sin Telegram (Code) o que no puede ser → 422
# ═══════════════════════════════════════════════════════════════════════

def test_G3_preguntarle_a_code_es_422_con_los_nombres_que_si_valen(natalia):
    con = natalia()
    r = abrir(nueva(responsable=config.NOMBRE_CODE))
    assert r.status_code == 422, r.text
    assert "Ana Inventada" in r.text and "Beto Inventado" in r.text and "Fuera Inventado" not in r.text
    assert filas(con) == []


def test_G3_si_el_responsable_del_proyecto_es_code_y_no_se_dijo_otro_es_422(natalia):
    con = natalia()
    r = abrir(nueva(proyecto_id=2))
    assert r.status_code == 422 and "Ana Inventada" in r.text
    assert filas(con) == []
    # diciendo uno que sí vale, se abre
    assert abrir(nueva(proyecto_id=2, responsable="Ana Inventada")).status_code == 200


def test_G3_si_el_responsable_del_proyecto_ya_no_puede_serlo_es_422(natalia):
    con = natalia()
    assert abrir(nueva(proyecto_id=7)).status_code == 422          # Delta es de FUERA, que no entra al panel
    assert filas(con) == []


def test_G3_un_nombre_que_no_es_de_nadie_o_no_entra_al_panel_es_422(natalia):
    for i, nombre in enumerate(("Nadie Conocido", "Fuera Inventado", "12345")):
        r = abrir(nueva(clave=f"n-{i}", responsable=nombre))
        assert r.status_code == 422, nombre
        assert "Ana Inventada" in r.text


def test_G3_nueva_fecha_sin_nadie_que_pueda_contestar_es_422(natalia):
    """La 70 es de Delta (proyecto de FUERA) y no tiene responsable: ni la tarea ni el proyecto pueden."""
    con = natalia()
    r = abrir(fecha_nueva("d", 70))
    assert r.status_code == 422 and "Ana Inventada" in r.text
    assert filas(con) == []


def test_G3_la_ruta_le_pregunta_a_las_dos_puertas(natalia, monkeypatch):
    """Cada puerta niega a alguien distinto: solo se le puede preguntar a quien pasa las DOS."""
    monkeypatch.setattr(config, "puede_recibir_telegram", lambda c: c != BETO)
    assert abrir(nueva("p1", responsable="Beto Inventado")).status_code == 422
    assert abrir(nueva("p2", responsable="Ana Inventada")).status_code == 200
    monkeypatch.setattr(db, "puede_ser_responsable", lambda c: c != ANA)
    assert abrir(nueva("p3", responsable="Ana Inventada")).status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# G4: nueva_fecha solo para la siguiente vencida; una viva por tarea
# ═══════════════════════════════════════════════════════════════════════

def test_G4_solo_la_siguiente_y_vencida_puede_abrir_nueva_fecha(natalia):
    con = natalia()
    # la 11 es pendiente de un proyecto de A pero NO es la siguiente (la 12 vencida pasa primero)
    assert abrir(fecha_nueva("a", 11)).status_code == 404
    # la 20 es la siguiente de Beta pero no está vencida
    r = abrir(fecha_nueva("b", 20))
    assert r.status_code == 409 and "vencida" in r.text
    # la 10 está hecha, la 14 borrada, la 30 es de un proyecto cerrado (no tiene siguiente), la 40 de la papelera
    for t in (10, 14, 30, 40):
        assert abrir(fecha_nueva(f"x-{t}", t)).status_code == 404, t
    assert filas(con) == []
    assert abrir(fecha_nueva("ok", 12)).status_code == 200


def test_G4_una_que_vence_hoy_no_esta_vencida_aunque_la_hora_ya_paso(natalia):
    con = natalia()
    hoy = datetime.now(config.TZ)
    con.execute("UPDATE tareas SET vence_en = ? WHERE id = 12",
                (datetime(hoy.year, hoy.month, hoy.day, 0, 1, tzinfo=config.TZ).isoformat(),))
    con.commit()
    assert abrir(fecha_nueva("hoy", 12)).status_code == 409


def test_G4_la_vencida_es_la_de_la_pagina_con_las_mismas_filas(natalia):
    """El oráculo es `armar_pagina` con las mismas filas: la siguiente y su «vencida» son las suyas."""
    con = natalia()
    proyectos = [dict(f) for f in con.execute("SELECT * FROM proyectos WHERE borrado_en IS NULL")]
    tareas = [dict(f) for f in con.execute("SELECT * FROM tareas WHERE borrado_en IS NULL")]
    for t in tareas:
        t["grave"] = False
    pagina = db.armar_pagina([], proyectos, tareas, [], [], config.nombres_con_code(), db.hoy_rd())["proyectos"]
    esperadas = {m["proxima"]["id"] for pid, m in pagina.items()
                 if m["proxima"] and m["proxima"]["vencida"] and pid in (1, 2, 3, 7)}
    abiertas = {t for t in (11, 12, 13, 20, 30, 70) if abrir(fecha_nueva(f"o-{t}", t)).status_code in (200, 422)}
    assert abiertas == esperadas == {12, 70}


def test_G4_dos_aperturas_con_distinta_clave_para_la_misma_tarea_dan_una_sola_fila(natalia):
    con = natalia()
    r1 = abrir(fecha_nueva("primera", 12))
    r2 = abrir(fecha_nueva("segunda", 12))
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert r1.json()["reusada"] is False and r2.json()["reusada"] is True
    assert r1.json()["consulta_id"] == r2.json()["consulta_id"]
    assert len(filas(con)) == 1


def test_G4_ya_preguntada_tambien_cuenta_como_viva(natalia):
    con = natalia()
    r1 = abrir(fecha_nueva("primera", 12))
    con.execute("UPDATE consultas_de_natalia SET estado = 'preguntada'")
    con.commit()
    r2 = abrir(fecha_nueva("segunda", 12))
    assert r2.status_code == 200 and r2.json()["reusada"] is True
    assert r2.json()["consulta_id"] == r1.json()["consulta_id"] and r2.json()["estado"] == "preguntada"
    assert len(filas(con)) == 1


def test_G4_resuelta_ya_no_esta_viva_y_deja_abrir_otra_para_la_misma_tarea(natalia):
    con = natalia()
    r1 = abrir(fecha_nueva("primera", 12))
    con.execute("UPDATE consultas_de_natalia SET estado = 'resuelta'")
    con.commit()
    r2 = abrir(fecha_nueva("segunda", 12))
    assert r2.status_code == 200 and r2.json()["reusada"] is False
    assert r2.json()["consulta_id"] != r1.json()["consulta_id"]
    assert len(filas(con)) == 2


def test_G4_dos_aperturas_A_LA_VEZ_de_la_misma_tarea_dejan_una_fila(natalia):
    """El cursor cede el bucle en cada `execute`: las dos pasan la comprobación y chocan en el índice único."""
    con = natalia()

    async def las_dos():
        return await asyncio.gather(
            db.abrir_consulta_de_natalia(A, "carrera-1", "nueva_fecha", tarea_id=12),
            db.abrir_consulta_de_natalia(A, "carrera-2", "nueva_fecha", tarea_id=12))
    (id1, re1), (id2, re2) = asyncio.run(las_dos())
    assert id1 == id2 and sorted([re1, re2]) == [False, True]
    assert len(filas(con)) == 1


# ═══════════════════════════════════════════════════════════════════════
# G5: la misma clave es la misma consulta
# ═══════════════════════════════════════════════════════════════════════

def test_G5_repetir_el_pedido_devuelve_la_misma_consulta_y_no_duplica(natalia):
    con = natalia()
    r1, r2 = abrir(nueva("reintento")), abrir(nueva("reintento"))
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert r1.json()["consulta_id"] == r2.json()["consulta_id"]
    assert (r1.json()["reusada"], r2.json()["reusada"]) == (False, True)
    assert len(filas(con)) == 1


def test_G5_el_reintento_devuelve_lo_abierto_aunque_el_mundo_cambie(natalia):
    """El proyecto se cerró entre el pedido y el reintento (por ejemplo, porque la respuesta se perdió): el
    reintento devuelve la consulta que ya existe, no un error nuevo."""
    con = natalia()
    r1 = abrir(nueva("se-perdio"))
    con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = 1")
    con.commit()
    r2 = abrir(nueva("se-perdio"))
    assert r2.status_code == 200 and r2.json()["consulta_id"] == r1.json()["consulta_id"]
    assert r2.json()["reusada"] is True
    assert abrir(nueva("otra-clave")).status_code == 409                  # una clave nueva sí ve el proyecto cerrado


def test_G5_la_misma_clave_con_otro_pedido_es_409_y_no_cambia_la_consulta(natalia):
    con = natalia()
    abrir(nueva("distinta"))
    antes = filas(con)
    for otro in (nueva("distinta", titulo="Otro título"), nueva("distinta", pedido="Otra cosa"),
                 nueva("distinta", vence="2027-02-02"), nueva("distinta", proyecto_id=2, responsable="Ana Inventada"),
                 nueva("distinta", responsable="Beto Inventado"), fecha_nueva("distinta", 12)):
        assert abrir(otro).status_code == 409, otro
    assert filas(con) == antes
    # decir lo mismo que ya está, o no decir responsable, es la misma consulta
    assert abrir(nueva("distinta", responsable="Ana Inventada")).json()["reusada"] is True
    assert abrir(nueva("distinta", titulo="  Subir el demo  ")).json()["reusada"] is True


def test_G5_dos_pedidos_A_LA_VEZ_con_la_misma_clave_dejan_una_fila(natalia):
    con = natalia()

    async def los_dos():
        return await asyncio.gather(
            db.abrir_consulta_de_natalia(A, "misma", "tarea_nueva", proyecto_id=1, titulo="T", pedido="P"),
            db.abrir_consulta_de_natalia(A, "misma", "tarea_nueva", proyecto_id=1, titulo="T", pedido="P"))
    (id1, re1), (id2, re2) = asyncio.run(los_dos())
    assert id1 == id2 and sorted([re1, re2]) == [False, True]
    assert len(filas(con)) == 1


def test_G5_la_clave_es_unica_entre_los_dos_tipos_tambien_A_LA_VEZ(natalia):
    """Una tarea_nueva y una nueva_fecha con la MISMA clave a la vez: una sola entra y la otra es 409. Si la unicidad
    fuera por (clave, tipo) entrarían las dos y «la misma clave es la misma consulta» sería mentira."""
    con = natalia()

    async def las_dos():
        return await asyncio.gather(
            db.abrir_consulta_de_natalia(A, "cruzada", "tarea_nueva", proyecto_id=1, titulo="T", pedido="P"),
            db.abrir_consulta_de_natalia(A, "cruzada", "nueva_fecha", tarea_id=12),
            return_exceptions=True)
    resultados = asyncio.run(las_dos())
    buenos = [r for r in resultados if isinstance(r, tuple)]
    malos = [r for r in resultados if isinstance(r, db.ConsultaNoVale)]
    assert len(buenos) == 1 and len(malos) == 1 and malos[0].clave == "clave_distinta", resultados
    assert len(filas(con)) == 1


# ═══════════════════════════════════════════════════════════════════════
# G6: lo que responde sale de una RELECTURA
# ═══════════════════════════════════════════════════════════════════════

def test_G6_el_estado_y_el_id_que_responde_el_post_son_los_de_la_base_no_los_enviados(natalia):
    """Un disparador (otro proceso: el despertador) mueve la consulta a `preguntada` justo después de abrirse.
    La respuesta tiene que decir `preguntada`, que es lo que hay en la base, no `abierta`, que es lo que se hizo."""
    con = natalia()
    con.execute("""CREATE TRIGGER adelanta AFTER INSERT ON consultas_de_natalia BEGIN
                     UPDATE consultas_de_natalia SET estado = 'preguntada', tarjeta_msg_id = 77 WHERE id = NEW.id;
                   END""")
    r = abrir(nueva("releida"))
    assert r.status_code == 200
    assert r.json()["estado"] == "preguntada"
    assert filas(con)[0]["estado"] == "preguntada"


def test_G6_la_tarea_de_una_anotada_se_relee_cada_vez(natalia):
    con = natalia()
    cid = _sembrar_consulta(con, "anotada", clave="x", resultante=11, titulo="Título de la consulta")
    t1 = leer([cid]).json()["consultas"][0]["tarea"]
    assert t1["titulo"] == "Mezclar el tema" and t1["fecha"] == dia(3) and t1["pendiente"] is True
    con.execute("UPDATE tareas SET titulo = 'Mezclar el tema (v2)', vence_en = ? WHERE id = 11", (en(10),))
    con.commit()
    t2 = leer([cid]).json()["consultas"][0]["tarea"]
    assert t2["titulo"] == "Mezclar el tema (v2)" and t2["fecha"] == dia(10)
    con.execute("UPDATE tareas SET estado = 'hecha', completado_en = ? WHERE id = 11", (en(0),))
    con.commit()
    assert leer([cid]).json()["consultas"][0]["tarea"]["pendiente"] is False
    con.execute("UPDATE tareas SET borrado_en = ? WHERE id = 11", (en(0),))
    con.commit()
    assert leer([cid]).json()["consultas"][0]["tarea"] is None


def test_G6_la_siguiente_de_una_resuelta_se_relee_y_sale_de_la_puerta_de_la_pagina(natalia):
    con = natalia()
    cid = _sembrar_consulta(con, "resuelta", "nueva_fecha", clave="r", tarea_id=12)
    s = leer([cid]).json()["consultas"][0]["siguiente"]
    assert set(s) == CAMPOS_SIGUIENTE
    assert (s["tarea_id"], s["fecha"], s["vencida"]) == (12, dia(-3), True)        # todavía no la movieron
    con.execute("UPDATE tareas SET vence_en = ? WHERE id = 12", (en(2),))          # alguien le puso fecha nueva
    con.commit()
    s = leer([cid]).json()["consultas"][0]["siguiente"]
    assert (s["tarea_id"], s["fecha"], s["vencida"]) == (12, dia(2), False)
    con.execute("UPDATE tareas SET vence_en = ? WHERE id = 12", (en(9),))          # ahora pasa primero la 11 (+3 d)
    con.commit()
    s = leer([cid]).json()["consultas"][0]["siguiente"]
    assert (s["tarea_id"], s["fecha"], s["vencida"]) == (11, dia(3), False)
    con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = 1")
    con.commit()
    assert leer([cid]).json()["consultas"][0]["siguiente"] is None


@pytest.mark.parametrize("estado, tipo", [("abierta", "tarea_nueva"), ("preguntada", "tarea_nueva"),
                                          ("rechazada", "tarea_nueva"), ("no_se_pudo", "tarea_nueva"),
                                          ("abierta", "nueva_fecha"), ("preguntada", "nueva_fecha")])
def test_G6_tarea_y_siguiente_solo_vienen_en_anotada_y_resuelta(natalia, estado, tipo):
    con = natalia()
    cid = _sembrar_consulta(con, estado, tipo, clave="e", tarea_id=12 if tipo == "nueva_fecha" else None,
                            resultante=11 if estado == "anotada" else None)
    c = leer([cid]).json()["consultas"][0]
    assert c["estado"] == estado and c["tarea"] is None and c["siguiente"] is None


# ═══════════════════════════════════════════════════════════════════════
# Los campos exactos: nada de chats, pedido ni motivo
# ═══════════════════════════════════════════════════════════════════════

def test_los_campos_de_las_dos_respuestas_son_exactamente_los_del_diseno_y_ningun_chat_sale(natalia):
    con = natalia()
    anotada = _sembrar_consulta(con, "anotada", clave="an", resultante=11)
    resuelta = _sembrar_consulta(con, "resuelta", "nueva_fecha", clave="re", tarea_id=12)
    post = abrir(nueva("campos"))
    get = leer([anotada, resuelta, post.json()["consulta_id"]])
    for r in (post, get):
        assert r.status_code == 200
        for numero in (ANA, BETO, FUERA, config.CHAT_ID_CODE, config.CHAT_ID_DUENO):
            assert numero not in list(_valores_de(r.json())) and str(numero) not in list(_valores_de(r.json()))
            assert not re.search(r"(?<![\d-])%s(?![\d-])" % re.escape(str(numero)), r.text), numero
        for palabra in ("MOTIVO-INTERNO", "Pedido sembrado", "El cliente quiere", "responsable", "motivo",
                        "pedido", "tarjeta", "contestada"):
            assert palabra not in r.text, palabra
    assert set(post.json()) == CAMPOS_CONSULTA | {"reusada"}
    por_id = {c["consulta_id"]: c for c in get.json()["consultas"]}
    assert len(por_id) == 3
    for c in por_id.values():
        assert set(c) == CAMPOS_CONSULTA
    assert set(por_id[anotada]["tarea"]) == CAMPOS_TAREA
    assert set(por_id[resuelta]["siguiente"]) == CAMPOS_SIGUIENTE
    assert set(get.json()) == {"noco_id", "consultas"}


@pytest.mark.parametrize("ids", ["", "abc", "0", "-1", "1,,2", "1,x", "1.5", " ", "9223372036854775808",
                                 ",".join(str(i) for i in range(1, 102))])
def test_ids_mal_formados_o_de_mas_son_422(natalia, ids):
    assert leer(ids).status_code == 422, ids


def test_el_tope_de_ids_es_cien_y_los_repetidos_no_duplican(natalia):
    con = natalia()
    cid = _sembrar_consulta(con, clave="uno")
    assert leer(list(range(1, 101))).status_code == 200
    assert [c["consulta_id"] for c in leer([cid, cid, cid]).json()["consultas"]] == [cid]


def test_sin_el_parametro_ids_es_422(natalia):
    api_code._pedidos_por_quien.clear()
    assert CLIENTE.get(RUTA.format(A), headers=_h()).status_code == 422


@pytest.mark.parametrize("malo", ["0", "-5", "abc", "1.5", "9223372036854775808"])
def test_un_noco_id_que_no_es_un_entero_positivo_es_422_en_las_dos_rutas(natalia, malo):
    con = natalia()
    antes = con.total_changes
    api_code._pedidos_por_quien.clear()
    assert CLIENTE.post(RUTA.format(malo), json=nueva(), headers=_h()).status_code == 422
    api_code._pedidos_por_quien.clear()
    assert CLIENTE.get(RUTA.format(malo), params={"ids": "1"}, headers=_h()).status_code == 422
    assert con.total_changes == antes


# ═══════════════════════════════════════════════════════════════════════
# G7: la tabla (CHECK, índices) y la única función que cambia el estado
# ═══════════════════════════════════════════════════════════════════════

def _insertar(con, **campos):
    base = dict(clave="c", tipo="tarea_nueva", estado="abierta", cliente_noco_id=A, proyecto_id=1, titulo="t",
                pedido="p", tarea_id=None, vence_vencida_en=None, responsable_chat_id=ANA)
    base.update(campos)
    columnas = ", ".join(base)
    con.execute(f"INSERT INTO consultas_de_natalia ({columnas}) VALUES ({', '.join('?' * len(base))})",
                tuple(base.values()))


def test_G7_la_base_acepta_las_filas_que_valen(natalia):
    con = natalia()
    _insertar(con, clave="uno")
    _insertar(con, clave="dos", tipo="nueva_fecha", estado="resuelta", titulo=None, pedido=None, tarea_id=12)
    for e in ("abierta", "preguntada", "anotada", "rechazada", "no_se_pudo"):
        _insertar(con, clave=f"e-{e}", estado=e)


@pytest.mark.parametrize("campos", [
    {"tipo": "otro"},                                            # tipo fuera del vocabulario
    {"estado": "inventado"},                                     # estado fuera del vocabulario
    {"estado": "resuelta"},                                      # `resuelta` es de nueva_fecha
    {"tipo": "nueva_fecha", "titulo": None, "pedido": None, "tarea_id": 12, "estado": "anotada"},
    {"tipo": "nueva_fecha", "titulo": None, "pedido": None, "tarea_id": 12, "estado": "rechazada"},
    {"tipo": "nueva_fecha", "titulo": None, "pedido": None, "tarea_id": 12, "estado": "no_se_pudo"},
    {"titulo": None},                                            # tarea_nueva sin título
    {"pedido": None},                                            # tarea_nueva sin pedido
    {"tarea_id": 12},                                            # tarea_nueva con tarea
    {"tipo": "nueva_fecha", "tarea_id": 12},                     # nueva_fecha con título y pedido
    {"tipo": "nueva_fecha", "titulo": None, "pedido": None, "tarea_id": None},   # nueva_fecha sin tarea
    {"clave": ""},                                               # clave vacía
    {"clave": "x" * 121},                                        # clave larguísima
    {"responsable_chat_id": None},                               # sin a quién preguntar
    {"cliente_noco_id": None},
    {"proyecto_id": None},
])
def test_G7_la_base_rechaza_lo_que_no_vale(natalia, campos):
    con = natalia()
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, **campos)


def test_G7_la_clave_es_unica_y_hay_una_nueva_fecha_viva_por_tarea(natalia):
    con = natalia()
    _insertar(con, clave="u")
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, clave="u")
    fecha = dict(tipo="nueva_fecha", titulo=None, pedido=None, tarea_id=12)
    _insertar(con, clave="f1", **fecha)
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, clave="f2", **fecha)                      # otra abierta para la misma tarea
    con.execute("UPDATE consultas_de_natalia SET estado = 'preguntada' WHERE clave = 'f1'")
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, clave="f3", **fecha)                      # tampoco con la otra preguntada
    con.execute("UPDATE consultas_de_natalia SET estado = 'resuelta' WHERE clave = 'f1'")
    _insertar(con, clave="f4", **fecha)                          # resuelta ya no ocupa el lugar
    _insertar(con, clave="f5", **{**fecha, "tarea_id": 13})      # y otra tarea es otra cosa


def test_G7_los_vocabularios_del_codigo_son_los_del_check_de_la_migracion():
    """Los estados y tipos del código salen de lo mismo que dice el CHECK, no de una lista aparte."""
    texto = _sin_comentarios(MIGRACION.read_text(encoding="utf-8"))
    del_check = set(re.findall(r"'(\w+)'", re.search(
        r"consultas_de_natalia_estado_valido\s+CHECK \((.*?)\)\s*,", " ".join(texto.split())).group(1)))
    assert del_check == {db.ESTADO_CONSULTA_ABIERTA, db.ESTADO_CONSULTA_PREGUNTADA, db.ESTADO_CONSULTA_ANOTADA,
                         db.ESTADO_CONSULTA_RECHAZADA, db.ESTADO_CONSULTA_NO_SE_PUDO, db.ESTADO_CONSULTA_RESUELTA}
    tipos = set(re.findall(r"'(\w+)'", re.search(
        r"consultas_de_natalia_tipo_valido\s+CHECK \((.*?)\)\s*,", " ".join(texto.split())).group(1)))
    assert tipos == {db.TIPO_CONSULTA_TAREA_NUEVA, db.TIPO_CONSULTA_NUEVA_FECHA}
    for de, a in db.TRANSICIONES_DE_CONSULTA:
        assert de in del_check and a in del_check


def test_G7_schema_sql_declara_lo_mismo_que_la_migracion():
    """El archivo que arma una base nueva y la migración que arma una vieja no se pueden separar."""
    def normal(t: str) -> list[str]:
        t = " ".join(_sin_comentarios(t).replace("IF NOT EXISTS", "").split())
        return sorted(s.strip() for s in t.split(";") if "consultas_de_natalia" in s
                      and not s.strip().upper().startswith(("COMMIT", "BEGIN")))
    de_migracion = normal(MIGRACION.read_text(encoding="utf-8"))
    de_schema = normal(SCHEMA.read_text(encoding="utf-8"))
    assert len(de_migracion) == 4          # la tabla y sus tres índices
    assert de_migracion == de_schema


def test_G7_la_migracion_es_idempotente_y_termina_en_un_solo_commit():
    texto = _sin_comentarios(MIGRACION.read_text(encoding="utf-8"))
    assert re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", texto) == ["consultas_de_natalia"]
    assert "CREATE TABLE IF NOT EXISTS consultas_de_natalia" in texto
    assert not re.search(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", texto)
    assert not re.search(r"\b(DROP|DELETE|UPDATE|TRUNCATE|ALTER)\b", texto)
    lineas = [l.strip() for l in MIGRACION.read_text(encoding="utf-8").splitlines()]
    assert lineas.count("BEGIN;") == 1 and lineas.count("COMMIT;") == 1 and [l for l in lineas if l][-1] == "COMMIT;"
    con = sqlite3.connect(":memory:")
    con.executescript(ESQUEMA)
    for _ in range(2):                                           # dos veces: la segunda no hace nada
        for s in ddl_de_la_migracion():
            con.execute(s)


def test_G7_el_ensayo_con_rollback_se_arma_con_el_guion_de_las_cotizaciones_sobre_esta_migracion():
    sys.path.insert(0, str(_ROOT / "tools"))
    try:
        import ensayo_cotizaciones as e
    finally:
        sys.path.pop(0)
    texto = MIGRACION.read_text(encoding="utf-8")
    assert e.tablas_de(texto) == ["consultas_de_natalia"]
    restricciones = e.restricciones_de(texto)
    assert len(set(restricciones)) == 6 and "consultas_de_natalia_clave_unica" in restricciones
    sql = e.sql_del_ensayo(texto)
    assert sql.count("COMMIT") == 0 and sql.count("ROLLBACK;") == 2 - 1
    assert "FROM consultas_de_natalia;" in sql and "to_regclass('public.consultas_de_natalia')" in sql
    assert "count(*) = 6" in sql


# ── La única función que cambia el estado ────────────────────────────────

async def _pasar(con, *a, **k):
    return await db.pasar_consulta_de_natalia(_Cur(con), *a, **k)


def test_G7_pasar_cambia_el_estado_y_solo_si_sigue_en_el_de_antes(natalia):
    con = natalia()
    cid = _sembrar_consulta(con)
    assert asyncio.run(_pasar(con, cid, "abierta", "preguntada", tarjeta_msg_id=55,
                              enviada_en=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))) is True
    f = filas(con)[0]
    assert (f["estado"], f["tarjeta_msg_id"]) == ("preguntada", 55) and f["enviada_en"] is not None
    # el primero gana: el segundo toque (con el mismo «de antes») no cambia nada
    assert asyncio.run(_pasar(con, cid, "abierta", "preguntada", tarjeta_msg_id=99)) is False
    assert filas(con)[0]["tarjeta_msg_id"] == 55
    assert asyncio.run(_pasar(con, cid, "preguntada", "anotada", contestada_por_chat_id=ANA,
                              tarea_resultante_id=11, motivo=None)) is True
    f = filas(con)[0]
    assert (f["estado"], f["contestada_por_chat_id"], f["tarea_resultante_id"], f["tarjeta_msg_id"]) == (
        "anotada", ANA, 11, 55)                                  # lo que no se pasa se queda como estaba
    # ya anotada: nadie más la mueve
    for a in ("rechazada", "no_se_pudo"):
        assert asyncio.run(_pasar(con, cid, "preguntada", a)) is False
    assert filas(con)[0]["estado"] == "anotada"


def test_G7_dos_toques_a_la_vez_solo_gana_uno(natalia):
    con = natalia()
    cid = _sembrar_consulta(con, "preguntada")

    async def los_dos():
        return await asyncio.gather(_pasar(con, cid, "preguntada", "anotada", tarea_resultante_id=11),
                                    _pasar(con, cid, "preguntada", "rechazada", motivo="no"))
    assert sorted(asyncio.run(los_dos())) == [False, True]
    assert filas(con)[0]["estado"] in ("anotada", "rechazada")


def test_G7_un_cambio_que_no_existe_es_ValueError_y_no_escribe(natalia):
    con = natalia()
    cid = _sembrar_consulta(con)
    for de, a in (("abierta", "anotada"), ("anotada", "abierta"), ("preguntada", "abierta"),
                  ("abierta", "abierta"), ("resuelta", "anotada"), ("abierta", "inventado")):
        with pytest.raises(ValueError):
            asyncio.run(_pasar(con, cid, de, a))
    assert filas(con)[0]["estado"] == "abierta"


def test_G7_el_cambio_que_existe_pero_no_es_del_tipo_lo_frena_el_check(natalia):
    con = natalia()
    cid = _sembrar_consulta(con, "preguntada", "nueva_fecha", clave="nf", tarea_id=12)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(_pasar(con, cid, "preguntada", "anotada"))
    assert filas(con)[0]["estado"] == "preguntada"


def _py_del_repo():
    """Los `.py` del repo fuera de `tests/`: se piden a LA puerta de los barridos (`_py_en_disco`)."""
    from test_buzon_que_no_se_ve import _py_en_disco   # LA puerta de los barridos del repo (importada aquí: ver otros tests)
    for ruta in sorted(_py_en_disco(_ROOT)):
        if ruta.relative_to(_ROOT).parts[0] != "tests":
            yield ruta


def _escritores_de_la_tabla(arbol) -> dict[str, set[str]]:
    """{función: {INSERT|UPDATE|DELETE}} de toda función con un SQL de escritura sobre la tabla (por el árbol)."""
    salida: dict[str, set[str]] = {}
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                for m in re.finditer(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+consultas_de_natalia\b", n.value, re.I):
                    salida.setdefault(fn.name, set()).add(m.group(1).split()[0].upper())
    return salida


def test_G7_censo_una_sola_funcion_cambia_el_estado_otra_inserta_y_nadie_borra():
    """Sacado del árbol sintáctico de TODOS los `.py` del repo fuera de `tests/`. Frontera: un SQL armado por
    partes o con un nombre de tabla en una variable no aparece (hoy ninguna función lo hace)."""
    vistos: dict[str, set[str]] = {}
    for ruta in _py_del_repo():
        for nombre, ops in _escritores_de_la_tabla(ast.parse(ruta.read_text(encoding="utf-8"))).items():
            vistos[f"{ruta.relative_to(_ROOT)}::{nombre}"] = ops
    assert vistos == {"db/db.py::abrir_consulta_de_natalia": {"INSERT"},
                      "db/db.py::pasar_consulta_de_natalia": {"UPDATE"}}


def test_G7_el_censo_ve_un_escritor_inventado():
    falso = ast.parse(
        "async def otro(c):\n    await c.execute('UPDATE consultas_de_natalia SET estado = %s')\n"
        "async def borra(c):\n    await c.execute('delete   from consultas_de_natalia')\n"
        "async def nada(c):\n    await c.execute('SELECT * FROM consultas_de_natalia')\n")
    assert _escritores_de_la_tabla(falso) == {"otro": {"UPDATE"}, "borra": {"DELETE"}}


# ═══════════════════════════════════════════════════════════════════════
# Las puertas: permisos, 401/403/429, 503 sin migrar, ni una palabra a Telegram
# ═══════════════════════════════════════════════════════════════════════

def test_el_censo_de_rutas_dice_los_permisos_y_solo_estas_rutas_los_usan():
    registradas = {(m, p): perm for m, p, perm in api_code.rutas_registradas(panel.app)}
    assert registradas[("POST", "/api/code/clientes/{noco_id}/consultas")] == ABRIR
    assert registradas[("GET", "/api/code/clientes/{noco_id}/consultas")] == LEER
    assert sorted(k for k, v in registradas.items() if v in (ABRIR, LEER)) == [
        ("GET", "/api/code/clientes/{noco_id}/consultas"), ("POST", "/api/code/clientes/{noco_id}/consultas")]
    assert not any(m in ("PUT", "PATCH", "DELETE") for (m, p) in registradas if "consultas" in p)


def test_sin_clave_o_con_clave_mala_es_401_en_las_dos(natalia):
    con = natalia()
    antes = con.total_changes
    assert CLIENTE.post(RUTA.format(A), json=nueva()).status_code == 401
    assert CLIENTE.get(RUTA.format(A), params={"ids": "1"}).status_code == 401
    assert abrir(nueva(), clave="clave-inventada").status_code == 401
    assert leer([1], clave="clave-inventada").status_code == 401
    assert con.total_changes == antes


def test_cada_permiso_abre_solo_su_ruta(natalia):
    con = natalia()
    assert abrir(nueva("p1"), clave=CLAVE_SOLO_LEER).status_code == 403
    assert abrir(nueva("p2"), clave=CLAVE_SALA).status_code == 403
    assert leer([1], clave=CLAVE_SOLO_ABRIR).status_code == 403
    assert leer([1], clave=CLAVE_SALA).status_code == 403
    assert filas(con) == []
    assert abrir(nueva("p3"), clave=CLAVE_SOLO_ABRIR).status_code == 200
    assert leer([1], clave=CLAVE_SOLO_LEER).status_code == 200
    # y los permisos nuevos no abren nada más
    for clave in (CLAVE_SOLO_ABRIR, CLAVE_SOLO_LEER):
        assert CLIENTE.get("/api/code/tareas", headers=_h(clave)).status_code == 403
        assert CLIENTE.get(f"/api/code/clientes/{A}/proyectos", headers=_h(clave)).status_code == 403


def test_leer_no_escribe_nada(natalia):
    con = natalia()
    _sembrar_consulta(con, clave="s")
    antes = con.total_changes
    assert leer([1, 2, 3]).status_code == 200
    assert con.total_changes == antes
    assert con.execute("SELECT COUNT(*) FROM log_acciones").fetchone()[0] == 0


def test_tiene_el_limite_de_pedidos_de_la_puerta(natalia):
    api_code._pedidos_por_quien.clear()
    codigos = [CLIENTE.get(RUTA.format(A), params={"ids": "1"}, headers=_h()).status_code for _ in range(61)]
    assert codigos[:60] == [200] * 60 and codigos[60] == 429


def test_sin_la_migracion_las_dos_rutas_dicen_503_y_no_guardan_nada(natalia):
    con = natalia(con_tabla=False)
    antes = con.total_changes
    r = abrir(nueva("sin-tabla"))
    assert r.status_code == 503 and "migración" in r.text
    r = leer([1])
    assert r.status_code == 503 and "migración" in r.text
    assert con.total_changes == antes
    # lo demás de la puerta sigue andando sin la tabla
    api_code._pedidos_por_quien.clear()
    assert CLIENTE.get(f"/api/code/clientes/{A}/proyectos", headers=_h()).status_code == 200


def test_otros_errores_de_la_base_no_se_disfrazan_de_falta_de_migracion(natalia, monkeypatch):
    natalia()

    async def revienta(*a, **k):
        raise RuntimeError("base caída")
    monkeypatch.setattr(db, "consultas_de_natalia_de_cliente", revienta)
    with pytest.raises(RuntimeError):
        leer([1])


def test_ni_abrir_ni_leer_ni_pasar_mandan_nada_a_telegram(natalia, monkeypatch):
    import telegram
    llamadas = []

    async def espia(self, *a, **k):
        llamadas.append((a, k))
    for nombre in ("send_message", "send_photo", "send_document", "edit_message_text"):
        monkeypatch.setattr(telegram.Bot, nombre, espia, raising=False)
    con = natalia()
    r = abrir(nueva("tg"))
    abrir(fecha_nueva("tg-f", 12))
    leer([r.json()["consulta_id"]])
    asyncio.run(_pasar(con, r.json()["consulta_id"], "abierta", "preguntada", tarjeta_msg_id=1))
    assert llamadas == []
    # y el código de esta pieza no nombra a Telegram ni al despertador
    fuente = (_ROOT / "web" / "api_code.py").read_text(encoding="utf-8")
    seccion = fuente[fuente.index("# ── Las consultas al responsable"):fuente.index("def avisar_si_alertas_mal_nombradas")]
    arbol = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    nombres = {n.name for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)
               and n.name in ("abrir_consulta_de_natalia", "consultas_de_natalia_de_cliente", "pasar_consulta_de_natalia")}
    assert len(nombres) == 3
    codigo = seccion + "\n".join(ast.unparse(n) for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)
                                 and n.name in nombres)
    for palabra in ("send_message", "import telegram", "telegram.", "Bot(", "_avisar", "despertador"):
        assert palabra not in codigo, palabra


def test_el_tolerar_la_base_sin_migrar_es_de_la_tabla_nueva_y_nada_mas(natalia):
    """El código viejo de la base no nombra la tabla: la única mención en SQL es de estas tres funciones y la
    migración."""
    menciones = set()
    for ruta in _py_del_repo():
        fuente = ruta.read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(fuente)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and re.search(r"\bconsultas_de_natalia\b", n.value)
                            and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", n.value)):
                        menciones.add(f"{ruta.relative_to(_ROOT)}::{fn.name}")
    assert menciones == {"db/db.py::abrir_consulta_de_natalia", "db/db.py::pasar_consulta_de_natalia",
                         "db/db.py::consultas_de_natalia_de_cliente", "db/db.py::_consulta_por_clave",
                         "tools/humo.py::_consultas_de_natalia_sin_tolerancia"}


def test_humo_corre_la_lectura_y_nombra_la_tabla_sin_tolerancia():
    texto = (_ROOT / "tools" / "humo.py").read_text(encoding="utf-8")
    assert "_consultas_de_natalia_sin_tolerancia" in texto and "db.consultas_de_natalia_de_cliente(" in texto
    assert texto.count('("consultas_de_natalia (sin tolerancia)", _consultas_de_natalia_sin_tolerancia)') == 1


def test_el_validador_de_la_fecha_es_uno_solo_y_el_panel_lo_importa_sin_cambiarlo():
    """`_vence_valido` del panel ES `db.vence_de_dia_valido` (la puerta de Natalia no puede importar `web.app`)."""
    assert panel._vence_valido is db.vence_de_dia_valido
    assert panel.PISO_FECHA == db.PISO_FECHA
    for texto, esperado in (("", (True, None)), ("2027-01-15", True), ("2019-12-31", (False, None)),
                            ("basura", (False, None))):
        ok, instante = db.vence_de_dia_valido(texto)
        if esperado is True:
            assert ok and instante.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M") == "2027-01-15 23:59"
        else:
            assert (ok, instante) == esperado
    # nadie en `web/` ni en `acciones/` define su propia copia
    for ruta in _py_del_repo():
        for fn in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
            if isinstance(fn, ast.FunctionDef) and fn.name in ("_vence_valido", "vence_de_dia_valido"):
                assert str(ruta.relative_to(_ROOT)) == "db/db.py", ruta
