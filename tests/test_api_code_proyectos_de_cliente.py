# -*- coding: utf-8 -*-
"""Natalia lee los proyectos de un cliente (18-oct-2026, pieza L1 de
`disenos/natalia-tareas-en-proyectos/DISENO.md`): `GET /api/code/clientes/{noco_id}/proyectos`,
permiso `proyectos:leer_de_cliente`.

CAMINO DE PRODUCCIÓN: la app FastAPI real, claves y permisos reales de `config`, y las funciones
REALES de `db/db.py` (`proyectos_de_cliente`, `situacion_de_proyecto`, `proyecto_admite_tareas`,
`graves_de`) corriendo su SQL de verdad contra SQLite en memoria (`%s` -> `?`). Ningún doble
reemplaza lo que la prueba dice vigilar. Lo único que se sustituye es el `pool` (la conexión) y
las dos puertas de Telegram en UNA prueba (G4), que es justo para ver que la ruta las consulta.

NINGÚN nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_api_code_proyectos_de_cliente.py -q
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-proyectos-de-cliente")
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.api_code as api_code  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENTE = TestClient(panel.app)

CLAVE_NATALIA = "clave-de-prueba-natalia-proyectos-no-es-real"
CLAVE_SOLO_ALERTAS = "clave-de-prueba-natalia-solo-alertas-no-es-real"
CLAVE_SALA = "clave-de-prueba-sala-proyectos-no-es-real"
PERMISO = "proyectos:leer_de_cliente"
RUTA = "/api/code/clientes/{}/proyectos"

A, B, SIN_PROYECTOS = 5001, 5002, 5003            # fichas de clientes inventadas
ANA, BETO, FUERA = 7710001, 7710002, 7710003      # chats inventados (FUERA no entra al panel)

AHORA = datetime.now(timezone.utc)


def en(dias: int) -> str:
    return (AHORA + timedelta(days=dias)).isoformat()


def dia(dias: int) -> str:
    """El día de Santo Domingo de `en(dias)`, escrito aparte de `db.dia_rd`."""
    return (AHORA + timedelta(days=dias)).astimezone(config.TZ).date().isoformat()


# ── Una base SQLite con las tablas que esta lectura toca ──────────────────

sqlite3.register_converter(
    "TSTZ", lambda b: datetime.fromisoformat(b.decode()).astimezone(timezone.utc))


class _Cur:
    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        cur = self._con.execute(sql.replace("%s", "?"), tuple(params or ()))
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
]
# (id, proyecto, titulo, estado, vence, completado, borrado, grave)
TAREAS = [
    (10, 1, "Tarea hecha de Alfa", "hecha", None, en(-5), None, 0),
    (11, 1, "Mezclar el tema", "pendiente", en(3), None, None, 0),
    (12, 1, "Grabar las voces", "pendiente", en(-3), None, None, 0),
    (13, 1, "Tarea sin fecha de Alfa", "pendiente", None, None, None, 0),
    (14, 1, "Tarea borrada de Alfa", "pendiente", en(-10), None, en(-1), 0),
    (20, 2, "Entregar el master", "pendiente", en(5), None, None, 0),
    (30, 3, "Pendiente de un proyecto cerrado", "pendiente", en(1), None, None, 0),
    (31, 3, "Hecha de un proyecto cerrado", "hecha", None, en(-2), None, 0),
    (40, 4, "Tarea de la papelera", "pendiente", en(1), None, None, 0),
    (50, 5, "SECRETO-DE-OTRO-CLIENTE", "pendiente", en(2), None, None, 0),
    (60, 6, "Tarea interna de IA", "pendiente", en(2), None, None, 0),
]
# La ficha del cliente B figura como PERSONA del proyecto 1, y la del A como persona del proyecto 5.
PARTICIPANTES = [(1, 1, None, B, "Cliente B", "Invitado"), (2, 5, None, A, "Cliente A", "Invitado")]


def _base(proyectos=PROYECTOS, tareas=TAREAS, participantes=PARTICIPANTES):
    con = sqlite3.connect(":memory:", check_same_thread=False,
                          detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    for pid, nombre, estado, resp, cliente, borrado in proyectos:
        con.execute(
            "INSERT INTO proyectos (id, nombre, estado, responsable_chat_id, cliente_nombre, "
            "cliente_noco_id, creado_en, borrado_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, nombre, estado, resp, None if cliente is None else f"Cliente {cliente}",
             cliente, en(-30), borrado))
    for tid, pid, titulo, estado, vence, completado, borrado, grave in tareas:
        con.execute(
            "INSERT INTO tareas (id, proyecto_id, titulo, estado, vence_en, completado_en, "
            "creado_en, borrado_en, grave) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, pid, titulo, estado, vence, completado, en(-20), borrado, grave))
    for x in participantes:
        con.execute("INSERT INTO participantes (id, proyecto_id, tarea_id, noco_id, nombre, rol) "
                    "VALUES (?, ?, ?, ?, ?, ?)", x)
    con.commit()
    return con


@pytest.fixture
def natalia():
    """Claves (Natalia con el permiso nuevo, Natalia solo con alertas, la sala sin él), dos
    personas que entran al panel, y la base armada con `poner_base`. Restaura todo al salir."""
    claves0, permisos0 = config.CLAVES_API_CODE, config.PERMISOS_API_CODE
    nombres0, permitidos0, pool0 = config.NOMBRES_POR_CHAT, config.CHAT_IDS_PERMITIDOS, db.pool
    config.CLAVES_API_CODE = {CLAVE_NATALIA: "natalia", CLAVE_SOLO_ALERTAS: "natalia_vieja",
                              CLAVE_SALA: "sala_mac"}
    config.PERMISOS_API_CODE = {
        "natalia": frozenset({"alertas:crear", PERMISO}),
        "natalia_vieja": frozenset({"alertas:crear"}),
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


def _leer(noco_id, clave=CLAVE_NATALIA):
    return CLIENTE.get(RUTA.format(noco_id), headers=_h(clave))


def _valores_de(x):
    """Todos los valores hoja de un JSON, a cualquier profundidad (llaves incluidas)."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _valores_de(v)
    elif isinstance(x, list):
        for v in x:
            yield from _valores_de(v)
    else:
        yield x


def _numeros_de_chat() -> list[str]:
    return [str(n) for n in (ANA, BETO, FUERA, config.CHAT_ID_CODE, config.CHAT_ID_DUENO)]


# ═══════════════════════════════════════════════════════════════════════
# G1: solo los proyectos de ESA ficha. Un cliente nunca ve lo de otro.
# ═══════════════════════════════════════════════════════════════════════

def test_G1_el_cliente_A_recibe_sus_tres_proyectos_vivos_y_nada_mas(natalia):
    r = _leer(A)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["noco_id"] == A
    # 1, 2 y 3 (activo, pausado, cerrado); no el 4 (papelera), ni el 5 (de B), ni el 6 (sin cliente)
    assert [p["id"] for p in cuerpo["proyectos"]] == [1, 2, 3]
    assert [p["nombre"] for p in cuerpo["proyectos"]] == [
        "Proyecto Alfa", "Proyecto Beta", "Proyecto Cerrado"]
    for ajeno in ("SECRETO-DE-OTRO-CLIENTE", "Proyecto Gamma", "papelera", "interno IA",
                  "Tarea borrada", "Cliente B", "Cliente A"):
        assert ajeno not in r.text, ajeno


def test_G1_el_cliente_B_recibe_solo_lo_suyo_aunque_figure_como_persona_en_otro(natalia):
    """B es PERSONA del proyecto 1 (participantes) y ese proyecto es de A: no cuenta."""
    r = _leer(B)
    assert [p["id"] for p in r.json()["proyectos"]] == [5]
    assert "Proyecto Alfa" not in r.text and "Grabar las voces" not in r.text


def test_G1_ser_persona_de_un_proyecto_no_lo_hace_del_cliente(natalia):
    """A figura como persona del proyecto 5 (de B): A no lo recibe, y B lo sigue recibiendo."""
    assert 5 not in [p["id"] for p in _leer(A).json()["proyectos"]]
    assert 5 in [p["id"] for p in _leer(B).json()["proyectos"]]


@pytest.mark.parametrize("noco_id", [SIN_PROYECTOS, 987654321, 1])
def test_G1_sin_proyecto_o_con_un_id_inventado_la_respuesta_es_la_misma(natalia, noco_id):
    r = _leer(noco_id)
    assert r.status_code == 200
    assert r.json() == {"noco_id": noco_id, "proyectos": [], "responsables": []}


@pytest.mark.parametrize("malo", ["0", "-5", "abc", "1.5", "9223372036854775808"])
def test_G1_un_noco_id_que_no_es_un_entero_positivo_es_422_y_no_toca_la_base(natalia, malo):
    con = natalia()
    antes = con.total_changes
    r = CLIENTE.get(RUTA.format(malo), headers=_h())
    assert r.status_code == 422, (malo, r.status_code)
    assert con.total_changes == antes


def test_G1_un_proyecto_que_pasa_a_la_papelera_deja_de_salir(natalia):
    con = natalia()
    con.execute("UPDATE proyectos SET borrado_en = ? WHERE id = 2", (en(0),))
    con.commit()
    assert [p["id"] for p in _leer(A).json()["proyectos"]] == [1, 3]


def test_G1_el_orden_es_activos_primero_luego_pausados_y_al_final_cerrados(natalia):
    con = natalia()
    con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = 1")
    con.execute("UPDATE proyectos SET estado = 'activo' WHERE id = 3")
    con.commit()
    assert [(p["id"], p["estado"]) for p in _leer(A).json()["proyectos"]] == [
        (3, "En curso"), (2, "Pausado"), (1, "Cerrado")]


def test_G1_los_hermanos_con_noco_id_en_la_ruta_son_estos_y_todos_estan_cubiertos():
    """HERMANOS, SACADOS DE LO REAL (lo que FastAPI registró): toda ruta de `/api/code` que
    lleva la ficha de un cliente en la ruta. Son TRES: esta lectura y las dos de las consultas (pieza L3, cuyo
    «otro cliente no lo ve» vive en `tests/test_api_code_consultas.py`, G2 y su censo). El día que haya una
    cuarta esta prueba se rompe y obliga a escribirle su propia prueba antes de sumarla aquí."""
    con_ficha = sorted((m, p, perm) for m, p, perm in api_code.rutas_registradas(panel.app)
                       if "{noco_id}" in p)
    assert con_ficha == [
        ("GET", "/api/code/clientes/{noco_id}/consultas", "consultas:leer"),
        ("GET", "/api/code/clientes/{noco_id}/proyectos", PERMISO),
        ("POST", "/api/code/clientes/{noco_id}/consultas", "consultas:abrir")]


# ═══════════════════════════════════════════════════════════════════════
# G2: los campos exactos, y ningún número de chat
# ═══════════════════════════════════════════════════════════════════════

CAMPOS_RESPUESTA = {"noco_id", "proyectos", "responsables"}
CAMPOS_PROYECTO = {"id", "nombre", "estado", "siguiente", "admite_tareas", "responsable"}
CAMPOS_SIGUIENTE = {"tarea_id", "titulo", "fecha", "vencida"}


def test_G2_los_campos_de_la_respuesta_son_exactamente_los_del_diseno(natalia):
    cuerpo = _leer(A).json()
    assert set(cuerpo) == CAMPOS_RESPUESTA
    assert cuerpo["proyectos"], "sin proyectos la prueba no vigilaría nada"
    vistos_siguiente = 0
    for p in cuerpo["proyectos"]:
        assert set(p) == CAMPOS_PROYECTO, p
        if p["siguiente"] is not None:
            vistos_siguiente += 1
            assert set(p["siguiente"]) == CAMPOS_SIGUIENTE, p["siguiente"]
    assert vistos_siguiente == 2          # Alfa y Beta; el cerrado no tiene


def test_G2_ningun_numero_de_chat_sale_y_los_responsables_van_por_nombre(natalia):
    r = _leer(A)
    valores = list(_valores_de(r.json()))
    assert len(valores) > 20, "la prueba no estaría mirando nada"
    for numero in (ANA, BETO, FUERA, config.CHAT_ID_CODE, config.CHAT_ID_DUENO):
        assert numero not in valores and str(numero) not in valores, numero
    for numero in _numeros_de_chat():
        assert not re.search(r"(?<![\d-])%s(?![\d-])" % re.escape(numero), r.text), numero
    p = {x["id"]: x for x in r.json()["proyectos"]}
    assert p[1]["responsable"] == "Ana Inventada"
    assert p[2]["responsable"] == config.NOMBRE_CODE      # el de Code se llama por su nombre
    assert p[3]["responsable"] is None
    # y ninguna otra cosa de la tarea o del proyecto: ni detalle, ni grave, ni comentarios, ni dinero
    for palabra in ("grave", "detalle", "comentario", "dinero", "cotiz", "nota", "actividad",
                    "carpeta", "participante", "descripcion"):
        assert palabra not in r.text.lower(), palabra


def test_G2_una_tarea_grave_no_dice_que_es_grave(natalia):
    con = natalia()
    con.execute("UPDATE tareas SET grave = 1, responsable_chat_id = ? WHERE id = 11",
                (config.CHAT_ID_CODE,))
    con.commit()
    r = _leer(A)
    sig = {p["id"]: p["siguiente"] for p in r.json()["proyectos"]}[1]
    assert set(sig) == CAMPOS_SIGUIENTE
    assert sig["tarea_id"] == 11            # la grave pasa primero (misma regla que la página)
    assert "grave" not in r.text.lower()


# ═══════════════════════════════════════════════════════════════════════
# G3: estado y siguiente salen de la puerta única (con expectativas escritas a mano)
# ═══════════════════════════════════════════════════════════════════════

def test_G3_el_estado_y_la_siguiente_de_cada_proyecto_escritos_a_mano(natalia):
    p = {x["id"]: x for x in _leer(A).json()["proyectos"]}
    # Alfa: 1 hecha -> «En curso»; de las pendientes sale primero la VENCIDA (-3 d), no la de +3 d
    # ni la sin fecha, y la borrada (-10 d) no cuenta
    assert p[1]["estado"] == "En curso"
    assert p[1]["siguiente"] == {"tarea_id": 12, "titulo": "Grabar las voces",
                                 "fecha": dia(-3), "vencida": True}
    assert p[1]["admite_tareas"] is True
    # Beta: pausado dice cuál sigue
    assert p[2]["estado"] == "Pausado"
    assert p[2]["siguiente"] == {"tarea_id": 20, "titulo": "Entregar el master",
                                 "fecha": dia(5), "vencida": False}
    assert p[2]["admite_tareas"] is True
    # Cerrado: sin siguiente, aunque tenga pendientes, y no admite tareas
    assert p[3]["estado"] == "Cerrado"
    assert p[3]["siguiente"] is None
    assert p[3]["admite_tareas"] is False


def test_G3_sin_fecha_y_sin_pendientes(natalia):
    con = natalia()
    con.execute("UPDATE tareas SET estado = 'hecha', completado_en = ? WHERE id IN (11, 12)", (en(-1),))
    con.commit()
    p = {x["id"]: x for x in _leer(A).json()["proyectos"]}
    # queda la sin fecha: es la que sigue, y su fecha es None
    assert p[1]["siguiente"] == {"tarea_id": 13, "titulo": "Tarea sin fecha de Alfa",
                                 "fecha": None, "vencida": False}
    con.execute("UPDATE tareas SET estado = 'hecha', completado_en = ? WHERE id = 13", (en(-1),))
    con.commit()
    p = {x["id"]: x for x in _leer(A).json()["proyectos"]}
    assert p[1]["siguiente"] is None and p[1]["estado"] == "En curso"


def test_G3_proyecto_sin_ninguna_hecha_dice_sin_empezar(natalia):
    con = natalia()
    con.execute("UPDATE tareas SET estado = 'pendiente', completado_en = NULL WHERE id = 10")
    con.commit()
    assert {x["id"]: x for x in _leer(A).json()["proyectos"]}[1]["estado"] == "Sin empezar"


def test_G3_la_ruta_y_la_pagina_dicen_lo_mismo_con_las_mismas_filas(natalia):
    """El oráculo es `armar_pagina` (la página) corriendo con las MISMAS filas que salen de la
    misma base: para cada proyecto, el estado y la siguiente de la ruta son los de la página."""
    con = natalia()
    graves = {11}
    con.execute("UPDATE tareas SET grave = 1, responsable_chat_id = ? WHERE id = 11",
                (config.CHAT_ID_CODE,))
    con.commit()
    proyectos = [dict(f) for f in con.execute("SELECT * FROM proyectos WHERE borrado_en IS NULL")]
    tareas = [dict(f) for f in con.execute("SELECT * FROM tareas WHERE borrado_en IS NULL")]
    for t in tareas:
        t["grave"] = t["id"] in graves
    pagina = db.armar_pagina([], proyectos, tareas, [], [], config.nombres_con_code(),
                             db.hoy_rd())["proyectos"]
    for p in _leer(A).json()["proyectos"]:
        m = pagina[p["id"]]
        assert p["estado"] == m["estado_calculado"], p["id"]
        prox = m["proxima"]
        if prox is None:
            assert p["siguiente"] is None
        else:
            assert p["siguiente"] == {"tarea_id": prox["id"], "titulo": prox["titulo"],
                                      "fecha": prox["vence"].isoformat() if prox["vence"] else None,
                                      "vencida": prox["vencida"]}


def test_G3_vence_hoy_no_esta_vencida_aunque_la_hora_ya_paso(natalia):
    """Una tarea que vence HOY (día de Santo Domingo) a una hora que ya pasó no está vencida:
    es la regla de la página, no un `< now()`."""
    con = natalia()
    hoy = datetime.now(config.TZ)
    hace_un_rato = datetime(hoy.year, hoy.month, hoy.day, 0, 1, tzinfo=config.TZ)
    con.execute("UPDATE tareas SET vence_en = ? WHERE id = 12", (hace_un_rato.isoformat(),))
    con.commit()
    sig = {x["id"]: x for x in _leer(A).json()["proyectos"]}[1]["siguiente"]
    assert sig["tarea_id"] == 12
    assert sig["fecha"] == hoy.date().isoformat() and sig["vencida"] is False


# ═══════════════════════════════════════════════════════════════════════
# G4: responsables = puede_ser_responsable ∩ puede_recibir_telegram
# ═══════════════════════════════════════════════════════════════════════

def test_G4_responsables_son_las_personas_del_panel_con_telegram_y_sin_code(natalia):
    r = _leer(A)
    # ANA y BETO entran al panel; FUERA tiene nombre pero no entra; Code no es persona del panel
    assert r.json()["responsables"] == ["Ana Inventada", "Beto Inventado"]
    assert config.NOMBRE_CODE not in r.json()["responsables"]
    assert "Fuera Inventado" not in r.text


def test_G4_la_ruta_le_pregunta_a_las_dos_puertas(natalia, monkeypatch):
    """Cada puerta niega a alguien distinto: solo sale quien pasa las DOS."""
    gente = (ANA, BETO, 7710004, 7710005)
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {c: f"Persona {c}" for c in gente})
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", gente)
    monkeypatch.setattr(config, "puede_ser_responsable", lambda c: c != ANA)
    monkeypatch.setattr(config, "puede_recibir_telegram", lambda c: c != BETO)
    assert _leer(A).json()["responsables"] == ["Persona 7710004", "Persona 7710005"]


def test_G4_los_hermanos_las_dos_puertas_existen_y_code_no_pasa_la_segunda():
    """Lo que la ruta da por cierto de `config`, comprobado con lo real."""
    assert config.puede_ser_responsable(config.CHAT_ID_CODE) is True
    assert config.puede_recibir_telegram(config.CHAT_ID_CODE) is False


# ═══════════════════════════════════════════════════════════════════════
# G5: la ruta y su permiso; 401/403; solo lee
# ═══════════════════════════════════════════════════════════════════════

def test_G5_el_censo_de_rutas_dice_el_permiso_y_solo_esta_ruta_lo_usa():
    registradas = {(m, p): perm for m, p, perm in api_code.rutas_registradas(panel.app)}
    assert registradas[("GET", "/api/code/clientes/{noco_id}/proyectos")] == PERMISO
    assert [k for k, v in registradas.items() if v == PERMISO] == [
        ("GET", "/api/code/clientes/{noco_id}/proyectos")]
    # nada más que GET en esa ruta
    assert [m for (m, p) in registradas if p == "/api/code/clientes/{noco_id}/proyectos"] == ["GET"]


def test_G5_sin_clave_o_con_clave_mala_es_401(natalia):
    assert CLIENTE.get(RUTA.format(A)).status_code == 401
    assert _leer(A, "clave-inventada").status_code == 401
    assert CLIENTE.get(RUTA.format(A), headers={"Authorization": "Basic abc"}).status_code == 401


def test_G5_una_clave_sin_el_permiso_es_403_y_no_ve_nada(natalia):
    for clave in (CLAVE_SOLO_ALERTAS, CLAVE_SALA):
        r = _leer(A, clave)
        assert r.status_code == 403, clave
        assert "Proyecto" not in r.text


def test_G5_el_permiso_nuevo_no_abre_ninguna_otra_ruta(natalia):
    """Natalia con el permiso nuevo sigue sin poder listar, cerrar ni tomar tareas."""
    assert CLIENTE.get("/api/code/tareas", headers=_h()).status_code == 403
    assert CLIENTE.post("/api/code/tareas/1/cerrar", headers=_h()).status_code == 403
    assert CLIENTE.post("/api/code/tareas/1/tomar", headers=_h()).status_code == 403


def test_G5_leer_no_escribe_nada_en_la_base(natalia):
    con = natalia()
    antes = con.total_changes
    for noco_id in (A, B, SIN_PROYECTOS):
        assert _leer(noco_id).status_code == 200
    assert con.total_changes == antes
    assert con.execute("SELECT COUNT(*) FROM log_acciones").fetchone()[0] == 0


def test_G5_tiene_el_limite_de_pedidos_de_la_puerta(natalia):
    """La ruta pasa por el mismo límite por clave que las demás: el pedido 61 en un minuto es 429."""
    api_code._pedidos_por_quien.clear()
    codigos = [_leer(SIN_PROYECTOS).status_code for _ in range(api_code._LIMITE_POR_CLAVE + 1)]
    assert codigos[:-1] == [200] * api_code._LIMITE_POR_CLAVE and codigos[-1] == 429
