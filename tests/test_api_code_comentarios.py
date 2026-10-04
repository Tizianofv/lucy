# -*- coding: utf-8 -*-
"""Los comentarios de la sala en SUS tareas (1-oct-2026): `GET` y `POST`
`/api/code/tareas/{id}/comentarios`.

CAMINO DE PRODUCCIÓN: la app FastAPI real, con claves reales de `config`, y
las funciones REALES de `db/db.py` corriendo su SQL de verdad contra SQLite en
memoria (`%s` -> `?`, `now()` registrada). Ningún doble reemplaza lo que la
prueba dice vigilar: ni el criterio de «es de Code», ni la validación del
texto, ni la huella.

NINGÚN nombre ni clave de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_api_code_comentarios.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sqlite3
import sys
import textwrap
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-comentarios")
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

CLAVE_SALA = "clave-de-prueba-comentarios-sala-no-es-real"
TODOS = frozenset({"tareas:listar", "tareas:cerrar", "tareas:tomar",
                   "comentarios:leer", "comentarios:escribir"})
PERSONA = 777001          # una persona inventada
OTRO_AUTOR = 777002       # un autor que ya no tiene nombre en el panel
RUTA_COMENTARIOS = "/api/code/tareas/{}/comentarios"


# ── Una base SQLite con el esquema que estas funciones tocan ──────────────

class _ErrorSQL(Exception):
    """Como el de psycopg: trae `sqlstate` (42703 = columna que no existe)."""
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Cur:
    sin_tomada_en = False      # lo prende `_sin_la_columna_tomada_en`

    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        if _Cur.sin_tomada_en and "tomada_en" in sql:
            raise _ErrorSQL("42703")
        cur = self._con.execute(sql.replace("%s", "?"), tuple(params or ()))
        self._filas = [dict(f) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Transaccion:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _Conn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _Cur(self._con)

    def transaction(self):
        return _Transaccion()

    async def execute(self, sql, params=None):
        return await _Cur(self._con).execute(sql, params)


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


def _base(tareas: list[dict], comentarios: list[dict] = ()):
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("now", 0, lambda: "2026-10-01 10:00:00")
    con.executescript("""
        CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, area TEXT);
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, titulo TEXT, detalle TEXT, estado TEXT,
          vence_en TEXT, creado_en TEXT, primero_id INTEGER, tomada_en TEXT,
          completado_en TEXT, bandeja_id INTEGER, responsable_chat_id INTEGER,
          area TEXT, proyecto_id INTEGER, borrado_en TEXT,
          grave INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE comentarios_tarea (
          id INTEGER PRIMARY KEY AUTOINCREMENT, tarea_id INTEGER NOT NULL,
          autor_chat_id INTEGER NOT NULL,
          creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, texto TEXT NOT NULL,
          borrado_en TEXT, borrado_por_chat_id INTEGER, editado_en TEXT);
        CREATE TABLE log_acciones (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT,
          tabla TEXT, registro_id INTEGER, antes TEXT, despues TEXT,
          motivo TEXT, bandeja_id INTEGER);
        INSERT INTO proyectos VALUES (901, 'Proyecto técnico inventado', 'IA');
        INSERT INTO proyectos VALUES (902, 'Proyecto de casa inventado', 'Casa');
    """)
    for t in tareas:
        con.execute(
            "INSERT INTO tareas (id, titulo, estado, bandeja_id, "
            "responsable_chat_id, area, proyecto_id, borrado_en) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], f"tarea {t['id']}", t.get("estado", "pendiente"),
             900 + t["id"], t.get("resp"), t.get("area"), t.get("proy"),
             t.get("borrada")))
    for c in comentarios:
        con.execute(
            "INSERT INTO comentarios_tarea (tarea_id, autor_chat_id, texto, "
            "borrado_en, editado_en) VALUES (?, ?, ?, ?, ?)",
            (c["tarea"], c["autor"], c["texto"], c.get("borrado"), c.get("editado")))
    con.commit()
    return con


@pytest.fixture
def sala():
    """Clave de la sala con todos los permisos, contadores limpios, y la base
    que cada prueba arma con `poner_base`. Restaura todo al salir."""
    claves0, permisos0 = config.CLAVES_API_CODE, config.PERMISOS_API_CODE
    nombres0, pool0 = config.NOMBRES_POR_CHAT, db.pool
    config.CLAVES_API_CODE = {CLAVE_SALA: "sala_mac"}
    config.PERMISOS_API_CODE = {"sala_mac": TODOS}
    config.NOMBRES_POR_CHAT = {PERSONA: "Marta Inventada"}
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0

    def poner_base(tareas, comentarios=()):
        con = _base(tareas, comentarios)
        db.pool = _Pool(con)
        api_code._pedidos_por_quien.clear()
        return con
    try:
        yield poner_base
    finally:
        config.CLAVES_API_CODE, config.PERMISOS_API_CODE = claves0, permisos0
        config.NOMBRES_POR_CHAT, db.pool = nombres0, pool0


def _h(clave=CLAVE_SALA):
    return {"Authorization": f"Bearer {clave}"}


def _n(con, tabla):
    return con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


DE_CODE = {"resp": config.CHAT_ID_CODE, "area": db.AREA_TECNICA}

# ═══════════════════════════════════════════════════════════════════════
# Leer y escribir sobre una tarea de Code
# ═══════════════════════════════════════════════════════════════════════

def test_escribir_y_leer_sobre_una_tarea_de_code(sala):
    con = sala([{"id": 1, **DE_CODE}])
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(),
                     json={"texto": "  Avance: la prueba ya corre.  "})
    assert r.status_code == 200, r.text
    assert r.json()["comentado"] is True
    r = CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h())
    assert r.status_code == 200
    comentarios = r.json()["comentarios"]
    assert len(comentarios) == 1
    assert comentarios[0]["texto"] == "Avance: la prueba ya corre."
    assert comentarios[0]["editado"] is False
    assert comentarios[0]["creado_en"]
    fila = con.execute("SELECT * FROM comentarios_tarea").fetchone()
    assert fila["autor_chat_id"] == config.CHAT_ID_CODE


def test_la_huella_es_de_la_sala_en_la_misma_base(sala):
    con = sala([{"id": 1, **DE_CODE}])
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "hola"})
    assert r.status_code == 200
    log = con.execute("SELECT * FROM log_acciones").fetchall()
    assert len(log) == 1
    assert (log[0]["actor"], log[0]["accion"], log[0]["tabla"]) == (
        "sala", "crear", "comentarios_tarea")
    assert log[0]["registro_id"] == r.json()["id"]
    assert "Code" in log[0]["motivo"]


def test_una_tarea_hecha_de_code_tambien_se_comenta(sala):
    con = sala([{"id": 1, "estado": "hecha", **DE_CODE}])
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "cierre"})
    assert r.status_code == 200
    assert _n(con, "comentarios_tarea") == 1


def test_el_autor_sale_por_nombre_nunca_por_numero(sala):
    sala([{"id": 1, **DE_CODE}],
         [{"tarea": 1, "autor": PERSONA, "texto": "de la persona"},
          {"tarea": 1, "autor": OTRO_AUTOR, "texto": "de alguien sin nombre"}])
    CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "de Code"})
    r = CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h())
    cs = r.json()["comentarios"]
    assert [c["autor"] for c in cs] == ["Marta Inventada", "sin nombre", config.NOMBRE_CODE]
    for c in cs:
        assert set(c) == {"id", "autor", "creado_en", "texto", "editado"}
    for numero in (str(PERSONA), str(OTRO_AUTOR), str(config.CHAT_ID_CODE)):
        assert f'"{numero}"' not in r.text and f": {numero}" not in r.text


def test_la_marca_de_editado_y_los_borrados_no_salen(sala):
    sala([{"id": 1, **DE_CODE}],
         [{"tarea": 1, "autor": PERSONA, "texto": "editado", "editado": "2026-10-01"},
          {"tarea": 1, "autor": PERSONA, "texto": "normal"},
          {"tarea": 1, "autor": PERSONA, "texto": "borrado", "borrado": "2026-10-01"}])
    cs = CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h()).json()["comentarios"]
    assert [(c["texto"], c["editado"]) for c in cs] == [("editado", True), ("normal", False)]


def test_solo_salen_los_comentarios_de_esa_tarea(sala):
    sala([{"id": 1, **DE_CODE}, {"id": 2, **DE_CODE}],
         [{"tarea": 1, "autor": PERSONA, "texto": "uno"},
          {"tarea": 2, "autor": PERSONA, "texto": "dos"}])
    cs = CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h()).json()["comentarios"]
    assert [c["texto"] for c in cs] == ["uno"]


# ═══════════════════════════════════════════════════════════════════════
# Rechazos por la tarea
# ═══════════════════════════════════════════════════════════════════════

NO_SON_DE_CODE = [
    ("de otra persona", {"resp": PERSONA, "area": db.AREA_TECNICA}),
    ("sin responsable", {"resp": None, "area": db.AREA_TECNICA}),
    ("de Code pero de otra área", {"resp": config.CHAT_ID_CODE, "area": "Casa"}),
    ("de Code, área del proyecto de casa", {"resp": config.CHAT_ID_CODE, "proy": 902}),
    ("de Code en la papelera", {**DE_CODE, "borrada": "2026-09-30"}),
]


@pytest.mark.parametrize("nombre,datos", NO_SON_DE_CODE, ids=[n for n, _ in NO_SON_DE_CODE])
def test_sobre_una_tarea_que_no_es_de_code_no_se_lee_ni_se_escribe(sala, nombre, datos):
    con = sala([{"id": 1, **datos}],
               [{"tarea": 1, "autor": PERSONA, "texto": "secreto de la casa"}])
    r = CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h())
    assert r.status_code == 404 and "secreto" not in r.text
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "hola"})
    assert r.status_code == 404
    assert _n(con, "comentarios_tarea") == 1 and _n(con, "log_acciones") == 0


def test_una_tarea_que_no_existe_se_rechaza(sala):
    con = sala([])
    assert CLIENTE.get(RUTA_COMENTARIOS.format(99), headers=_h()).status_code == 404
    assert CLIENTE.post(RUTA_COMENTARIOS.format(99), headers=_h(),
                        json={"texto": "x"}).status_code == 404
    assert _n(con, "comentarios_tarea") == 0


def test_db_comentar_tarea_con_autor_code_se_niega_sola_sin_la_ruta(sala):
    """La regla vive en `db.comentar_tarea`, no en la ruta."""
    con = sala([{"id": 1, "resp": PERSONA, "area": db.AREA_TECNICA}])
    cid = asyncio.run(db.comentar_tarea(1, config.CHAT_ID_CODE, "hola"))
    assert cid is None and _n(con, "comentarios_tarea") == 0


def test_el_panel_sigue_igual_una_persona_de_la_casa_comenta_cualquier_tarea(sala):
    con = sala([{"id": 1, "resp": PERSONA, "area": "Casa"}])
    cid = asyncio.run(db.comentar_tarea(1, config.CHAT_ID_DUENO, "del panel"))
    assert cid is not None
    assert con.execute("SELECT actor FROM log_acciones").fetchone()["actor"] == "panel"
    assert asyncio.run(db.comentar_tarea(1, 555000111, "de un desconocido")) is None


def test_un_desconocido_ni_con_un_negativo_comenta_ni_en_una_tarea_de_code(sala):
    """Solo `CHAT_ID_CODE` exacto es la sala: otro chat, incluso negativo (un
    grupo de Telegram) o positivo, no comenta ni en una tarea de Code."""
    con = sala([{"id": 1, **DE_CODE}])
    for chat in (555000111, -555000111, config.CHAT_ID_CODE - 1, 0):
        assert asyncio.run(db.comentar_tarea(1, chat, "intruso")) is None, chat
    assert _n(con, "comentarios_tarea") == 0


# ═══════════════════════════════════════════════════════════════════════
# Rechazos por la clave
# ═══════════════════════════════════════════════════════════════════════

def test_sin_clave_o_con_clave_mala_es_401_y_no_se_toca_nada(sala):
    con = sala([{"id": 1, **DE_CODE}])
    for headers in ({}, _h("clave-inventada-que-no-existe")):
        assert CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=headers).status_code == 401
        assert CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=headers,
                            json={"texto": "x"}).status_code == 401
    # incluso con un cuerpo roto: la clave se mira ANTES que el cuerpo
    assert CLIENTE.post(RUTA_COMENTARIOS.format(1), json={}).status_code == 401
    assert _n(con, "comentarios_tarea") == 0 and _n(con, "log_acciones") == 0


@pytest.mark.parametrize("permisos,lee,escribe", [
    (frozenset({"tareas:listar", "tareas:cerrar", "tareas:tomar"}), 403, 403),
    (frozenset({"comentarios:leer"}), 200, 403),
    (frozenset({"comentarios:escribir"}), 403, 200),
    (frozenset({"alertas:crear"}), 403, 403),
])
def test_cada_permiso_abre_solo_su_ruta(sala, permisos, lee, escribe):
    con = sala([{"id": 1, **DE_CODE}])
    config.PERMISOS_API_CODE = {"sala_mac": permisos}
    assert CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h()).status_code == lee
    n0 = _n(con, "comentarios_tarea")
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "x"})
    assert r.status_code == escribe
    assert _n(con, "comentarios_tarea") == n0 + (1 if escribe == 200 else 0)


def test_las_rutas_nuevas_salen_de_lo_que_fastapi_registro_con_su_permiso():
    registradas = {(m, p): perm for m, p, perm in api_code.rutas_registradas(panel.app)}
    assert registradas[("GET", "/api/code/tareas/{tid}/comentarios")] == "comentarios:leer"
    assert registradas[("POST", "/api/code/tareas/{tid}/comentarios")] == "comentarios:escribir"


# ═══════════════════════════════════════════════════════════════════════
# El texto
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("cuerpo", [
    {"texto": ""}, {"texto": "   \n\t "}, {"texto": "x" * (db.LARGO_COMENTARIO + 1)},
    {"texto": 123}, {"texto": None}, {}, {"otro": "campo"}])
def test_texto_vacio_largo_o_que_no_es_texto_se_rechaza(sala, cuerpo):
    con = sala([{"id": 1, **DE_CODE}])
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json=cuerpo)
    assert r.status_code == 422
    assert _n(con, "comentarios_tarea") == 0 and _n(con, "log_acciones") == 0


def test_el_largo_exacto_pasa_y_los_saltos_de_linea_se_normalizan(sala):
    con = sala([{"id": 1, **DE_CODE}])
    ok = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(),
                      json={"texto": "x" * db.LARGO_COMENTARIO})
    assert ok.status_code == 200
    CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(), json={"texto": "a\r\nb"})
    textos = [f["texto"] for f in con.execute("SELECT texto FROM comentarios_tarea ORDER BY id")]
    assert textos[1] == "a\nb"


def test_el_autor_no_lo_decide_quien_llama(sala):
    con = sala([{"id": 1, **DE_CODE}])
    r = CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(),
                     json={"texto": "hola", "autor_chat_id": PERSONA, "autor": "Marta"})
    assert r.status_code == 200
    assert con.execute("SELECT autor_chat_id FROM comentarios_tarea").fetchone()[0] \
        == config.CHAT_ID_CODE


def test_la_ruta_valida_con_la_misma_regla_que_el_panel():
    fuente = inspect.getsource(api_code.comentar_tarea_de_code)
    assert "db.texto_de_comentario" in fuente


# ═══════════════════════════════════════════════════════════════════════
# Nada por Telegram
# ═══════════════════════════════════════════════════════════════════════

def test_escribir_y_leer_no_crean_ningun_bot_de_telegram(sala, monkeypatch):
    """El `Bot` de mentira RECUERDA que lo crearon (no lanza: `_avisar_abuso`
    se traga cualquier excepción, y una prueba que dependiera de eso no
    vería nada)."""
    import telegram
    creados = []

    class _BotQueRecuerda:
        def __init__(self, *a, **k):
            creados.append((a, k))

        async def send_message(self, *a, **k):
            creados.append(("send_message", a, k))
    monkeypatch.setattr(telegram, "Bot", _BotQueRecuerda)
    con = sala([{"id": 1, **DE_CODE}])
    assert CLIENTE.post(RUTA_COMENTARIOS.format(1), headers=_h(),
                        json={"texto": "x"}).status_code == 200
    assert CLIENTE.get(RUTA_COMENTARIOS.format(1), headers=_h()).status_code == 200
    assert _n(con, "comentarios_tarea") == 1
    assert creados == [], f"un comentario de Code tocó Telegram: {creados}"


def test_el_bot_que_recuerda_si_ve_un_aviso_real(sala, monkeypatch):
    """Control de la prueba de arriba: el mismo `Bot` de mentira SÍ registra
    el aviso de abuso, que es el único mensaje que esta puerta sabe mandar."""
    import telegram
    creados = []

    class _BotQueRecuerda:
        def __init__(self, *a, **k):
            creados.append("creado")

        async def send_message(self, *a, **k):
            creados.append("enviado")
    monkeypatch.setattr(telegram, "Bot", _BotQueRecuerda)
    asyncio.run(api_code._avisar_abuso(10))
    assert creados == ["creado", "enviado"]


def test_comentar_tarea_no_nombra_telegram():
    """Descripción del hallazgo: escribir un comentario no dispara aviso
    ninguno. Esta prueba mira el texto; la de arriba, el comportamiento."""
    for fn in (db.comentar_tarea, db.comentarios_de_tarea_de_code, db.tarea_de_code,
               api_code.comentar_tarea_de_code, api_code.leer_comentarios):
        fuente = inspect.getsource(fn).lower()
        assert "send_message" not in fuente and "telegram.bot" not in fuente


# ═══════════════════════════════════════════════════════════════════════
# HERMANOS: tomar, cerrar, listar, leer y comentar dicen lo mismo
# ═══════════════════════════════════════════════════════════════════════

# (nombre, datos, ¿es de Code?). Todas pendientes y sin tomar: así tomar y
# cerrar (que además miran el estado) no se distinguen por otra cosa.
MATRIZ = [
    (1, {**DE_CODE}, True),
    (2, {"resp": config.CHAT_ID_CODE, "proy": 901}, True),                 # área del proyecto
    (3, {"resp": config.CHAT_ID_CODE, "area": "Casa"}, False),
    (4, {"resp": config.CHAT_ID_CODE, "proy": 902}, False),
    (5, {"resp": PERSONA, "area": db.AREA_TECNICA}, False),
    (6, {**DE_CODE, "borrada": "2026-09-30"}, False),
    (7, {"resp": None, "area": db.AREA_TECNICA}, False),
    (8, {"resp": config.CHAT_ID_CODE, "area": "Casa", "proy": 901}, False),  # gana la de la tarea
    (9, {"resp": config.CHAT_ID_CODE, "area": db.AREA_TECNICA, "proy": 902}, True),
]
IDS = [i for i, _, _ in MATRIZ] + [999]   # 999: no existe
ESPERADO = {i for i, _, es in MATRIZ if es}


def _elegidas_por(consulta) -> set[int]:
    return {i for i in IDS if consulta(i)}


@pytest.fixture
def _sin_la_columna_tomada_en():
    """Base sin `tareas.tomada_en`: toda consulta que la nombre revienta con
    SQLSTATE 42703, y `listar` cae a su SELECT de respaldo (`sin_tomada`)."""
    _Cur.sin_tomada_en = True
    try:
        yield
    finally:
        _Cur.sin_tomada_en = False


def test_listar_por_su_rama_de_respaldo_sin_tomada_en_dice_lo_mismo(
        sala, _sin_la_columna_tomada_en):
    """La otra rama de `tareas_de_code_pendientes`. Se comprueba que de verdad
    cayó al respaldo (cada fila sale con `tomada_en: None`) y que acepta las
    mismas tareas que el resto."""
    _con_matriz(sala)
    r = CLIENTE.get("/api/code/tareas", headers=_h())
    assert r.status_code == 200
    assert {t["id"] for t in r.json()["tareas"]} == ESPERADO
    assert all(t["tomada_en"] is None for t in r.json()["tareas"])


def _con_matriz(sala):
    return sala([{"id": i, **d} for i, d, _ in MATRIZ])


def _http_ok(metodo, ruta, **kw) -> bool:
    return CLIENTE.request(metodo, ruta, headers=_h(), **kw).status_code == 200


def test_los_cinco_caminos_aceptan_exactamente_las_mismas_tareas(sala):
    caminos = {
        "tomar": lambda i: _http_ok("POST", f"/api/code/tareas/{i}/tomar"),
        "cerrar": lambda i: _http_ok("POST", f"/api/code/tareas/{i}/cerrar"),
        "leer comentarios": lambda i: _http_ok("GET", RUTA_COMENTARIOS.format(i)),
        "comentar": lambda i: _http_ok("POST", RUTA_COMENTARIOS.format(i), json={"texto": "x"}),
    }
    vistos = {}
    for nombre, camino in caminos.items():
        _con_matriz(sala)                     # base nueva por camino
        vistos[nombre] = _elegidas_por(camino)
    _con_matriz(sala)
    r = CLIENTE.get("/api/code/tareas", headers=_h())
    vistos["listar"] = {t["id"] for t in r.json()["tareas"]}
    assert vistos == {n: ESPERADO for n in (*caminos, "listar")}, vistos


def test_la_matriz_sale_de_las_tareas_que_el_criterio_distingue():
    """Si el criterio cambia, la matriz tiene que seguir teniendo de las dos
    clases (de Code / no de Code) y de las cinco razones de rechazo."""
    assert ESPERADO and set(IDS) - ESPERADO
    assert len({tuple(sorted(d)) for _, d, es in MATRIZ if not es}) >= 4


def test_leer_y_comentar_pasan_por_la_misma_puerta_tarea_de_code():
    """Las dos funciones de comentarios llaman a `db.tarea_de_code` (mirando
    el árbol de sintaxis, no el texto) -- y ninguna trae su propio criterio
    de responsable/área."""
    for fn in (db.comentarios_de_tarea_de_code, db.comentar_tarea):
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        llamadas = {n.func.id for n in ast.walk(arbol)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "tarea_de_code" in llamadas, fn.__name__
        textos = " ".join(n.value for n in ast.walk(arbol)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str))
        assert "responsable_chat_id" not in textos and "COALESCE" not in textos, fn.__name__
