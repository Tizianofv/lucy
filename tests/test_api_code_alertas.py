# -*- coding: utf-8 -*-
"""`POST /api/code/alertas`: la puerta de Natalia (4-oct-2026) y la marca de
GRAVE (`tareas.grave`) que ordena `GET /api/code/tareas` y se pinta en /tareas.

CAMINO DE PRODUCCIÓN: la app FastAPI real, claves reales de `config`, y las
funciones REALES de `db/db.py` corriendo su SQL contra SQLite en memoria
(`%s` -> `?`, `now()` registrada). Un error de columna que no existe sube como
el de Postgres (SQLSTATE 42703) para ejercer las ramas de «migración sin
aplicar». Ningún nombre, teléfono ni clave de este archivo es real.

Correr:  python3 -m pytest tests/test_api_code_alertas.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-alertas")
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

CLAVE_NATALIA = "clave-de-prueba-alertas-natalia-no-es-real"
CLAVE_SALA = "clave-de-prueba-alertas-sala-no-es-real"
PERMISOS_SALA = frozenset({"tareas:listar", "tareas:cerrar", "tareas:tomar",
                           "comentarios:leer", "comentarios:escribir"})
RUTA = "/api/code/alertas"
CLIENTE_INVENTADO = "Marta Inventada 8095550000"   # dato de cliente de mentira


# ── SQLite con el esquema que estas rutas tocan ───────────────────────────

class _ErrorSQL(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Cur:
    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        # `= ANY(%s)` es de Postgres: con una lista se emula con json_each.
        sql = sql.replace("= ANY(%s)", "IN (SELECT value FROM json_each(%s))")
        params = tuple(json.dumps(p) if isinstance(p, list) else p for p in (params or ()))
        try:
            cur = self._con.execute(sql.replace("%s", "?"), params)
        except sqlite3.OperationalError as e:
            if "no such column" in str(e) or "no column named" in str(e):
                raise _ErrorSQL("42703") from None
            raise
        self._filas = [dict(f) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Tx:
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
        return _Tx()

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


def _base(sin_grave=False, sin_clave=False):
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("now", 0, lambda: "2026-10-04 10:00:00")
    columnas_nuevas = ""
    if not sin_clave:
        columnas_nuevas += ", clave_tecnica TEXT, ultima_alarma_en TEXT"
    if not sin_grave:
        columnas_nuevas += ", grave INTEGER NOT NULL DEFAULT 0"
    con.executescript(f"""
        CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, area TEXT);
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT, detalle TEXT,
          estado TEXT NOT NULL DEFAULT 'pendiente', vence_en TEXT,
          creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, primero_id INTEGER,
          tomada_en TEXT, completado_en TEXT, bandeja_id INTEGER,
          responsable_chat_id INTEGER, area TEXT, proyecto_id INTEGER,
          borrado_en TEXT{columnas_nuevas});
        CREATE TABLE log_acciones (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT,
          tabla TEXT, registro_id INTEGER, antes TEXT, despues TEXT,
          motivo TEXT, bandeja_id INTEGER);
    """)
    return con


@pytest.fixture
def puerta():
    """Claves de Natalia (solo `alertas:crear`) y de la sala (sin él),
    contadores limpios; devuelve `poner_base(**opciones) -> sqlite3 conexión`."""
    claves0, permisos0, pool0 = config.CLAVES_API_CODE, config.PERMISOS_API_CODE, db.pool
    config.CLAVES_API_CODE = {CLAVE_NATALIA: "natalia", CLAVE_SALA: "sala_mac"}
    config.PERMISOS_API_CODE = {"natalia": frozenset({"alertas:crear"}),
                                "sala_mac": PERMISOS_SALA}
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0

    def poner_base(**opciones):
        con = _base(**opciones)
        db.pool = _Pool(con)
        api_code._pedidos_por_quien.clear()
        return con
    try:
        yield poner_base
    finally:
        config.CLAVES_API_CODE, config.PERMISOS_API_CODE, db.pool = claves0, permisos0, pool0


def _h(clave=CLAVE_NATALIA):
    return {"Authorization": f"Bearer {clave}"}


def _alerta(clave="whatsapp_caido", titulo="WhatsApp no entrega", detalle="detalle de prueba",
            **extra):
    return {"clave": clave, "titulo": titulo, "detalle": detalle, **extra}


def _n(con, tabla="tareas"):
    return con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════
# Crear, reusar, no duplicar
# ═══════════════════════════════════════════════════════════════════════

def test_crea_una_tarea_de_code_y_dice_que_la_creo(puerta):
    con = puerta()
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle=f"falló con {CLIENTE_INVENTADO}"))
    assert r.status_code == 200, r.text
    assert r.json() == {"tarea_id": 1, "reusada": False, "grave": False,
                        "repeticion_omitida": False, "recortado": False}
    t = dict(con.execute("SELECT * FROM tareas").fetchone())
    assert (t["titulo"], t["area"], t["responsable_chat_id"], t["estado"]) == (
        "WhatsApp no entrega", db.AREA_TECNICA, config.CHAT_ID_CODE, "pendiente")
    assert t["clave_tecnica"] == "natalia:whatsapp_caido"
    assert t["grave"] == 0 and CLIENTE_INVENTADO in t["detalle"]


def test_la_huella_lleva_clave_y_titulo_pero_nunca_el_detalle(puerta):
    con = puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle=f"datos de {CLIENTE_INVENTADO}"))
    log = con.execute("SELECT * FROM log_acciones").fetchall()
    assert len(log) == 1
    assert (log[0]["actor"], log[0]["accion"], log[0]["tabla"]) == ("lucy", "crear", "tareas")
    assert json.loads(log[0]["despues"]) == {
        "clave_tecnica": "natalia:whatsapp_caido", "titulo": "WhatsApp no entrega"}
    assert CLIENTE_INVENTADO not in json.dumps(dict(log[0]))


def test_la_misma_clave_repetida_reusa_la_misma_tarea(puerta):
    con = puerta()
    primera = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="uno")).json()
    for i in range(2, 6):
        r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle=f"vez {i}"))
        assert r.status_code == 200
        assert r.json()["reusada"] is True and r.json()["tarea_id"] == primera["tarea_id"]
    assert _n(con) == 1
    assert con.execute("SELECT detalle FROM tareas").fetchone()[0].count("Volvió a pasar") == 4
    assert _n(con, "log_acciones") == 1          # una sola huella: la de nacer


def test_una_clave_distinta_es_otra_tarea(puerta):
    con = puerta()
    a = CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave="uno")).json()
    b = CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave="dos")).json()
    assert a["tarea_id"] != b["tarea_id"] and b["reusada"] is False and _n(con) == 2


def test_si_ya_se_cerro_y_vuelve_a_sonar_nace_una_tarea_nueva(puerta):
    con = puerta()
    a = CLIENTE.post(RUTA, headers=_h(), json=_alerta()).json()
    con.execute("UPDATE tareas SET estado = 'hecha' WHERE id = ?", (a["tarea_id"],))
    b = CLIENTE.post(RUTA, headers=_h(), json=_alerta()).json()
    assert b["reusada"] is False and b["tarea_id"] != a["tarea_id"]
    estados = [r[0] for r in con.execute("SELECT estado FROM tareas ORDER BY id")]
    assert estados == ["hecha", "pendiente"]


def test_la_clave_la_prefija_la_autenticacion_y_no_pisa_a_las_alarmas_de_lucy(puerta):
    con = puerta()
    # una alarma propia de Lucy, ya abierta, con la clave «backup» a secas
    asyncio.run(db.crear_o_reusar_alerta_tecnica("backup", "Respaldo viejo", "d"))
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave="backup")).json()
    assert r["reusada"] is False
    claves = sorted(x[0] for x in con.execute("SELECT clave_tecnica FROM tareas"))
    assert claves == ["backup", "natalia:backup"]


def test_una_tarea_tomada_o_comentada_se_reusa_igual(puerta):
    """Reusar mira estado y clave, no si la sala ya la tomó: sigue abierta."""
    con = puerta()
    a = CLIENTE.post(RUTA, headers=_h(), json=_alerta()).json()
    con.execute("UPDATE tareas SET tomada_en = '2026-10-04 09:00:00'")
    assert CLIENTE.post(RUTA, headers=_h(), json=_alerta()).json()["tarea_id"] == a["tarea_id"]


# ═══════════════════════════════════════════════════════════════════════
# Grave
# ═══════════════════════════════════════════════════════════════════════

def test_una_alerta_grave_queda_marcada_y_lo_dice(puerta):
    con = puerta()
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True)).json()
    assert r["grave"] is True and r["reusada"] is False
    assert con.execute("SELECT grave FROM tareas").fetchone()[0] == 1


def test_leve_primero_y_grave_despues_sube_la_misma_tarea_a_grave(puerta):
    con = puerta()
    a = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=False)).json()
    assert a["grave"] is False
    b = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True)).json()
    assert (b["reusada"], b["grave"], b["tarea_id"]) == (True, True, a["tarea_id"])
    assert _n(con) == 1 and con.execute("SELECT grave FROM tareas").fetchone()[0] == 1


def test_una_grave_no_baja_aunque_la_repeticion_venga_leve(puerta):
    con = puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True))
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=False)).json()
    assert r["reusada"] is True and r["grave"] is True
    assert con.execute("SELECT grave FROM tareas").fetchone()[0] == 1


def test_la_grave_ya_cerrada_no_contagia_a_la_nueva(puerta):
    con = puerta()
    a = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True)).json()
    con.execute("UPDATE tareas SET estado = 'hecha' WHERE id = ?", (a["tarea_id"],))
    b = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=False)).json()
    assert b["grave"] is False


def test_la_sala_lee_primero_las_graves_y_ve_la_marca(puerta):
    puerta()
    for clave, grave in (("a", False), ("b", False), ("c", True), ("d", True)):
        CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave=clave, titulo=f"t-{clave}", grave=grave))
    r = CLIENTE.get("/api/code/tareas", headers=_h(CLAVE_SALA))
    assert r.status_code == 200
    filas = r.json()["tareas"]
    assert [(f["titulo"], f["grave"]) for f in filas] == [
        ("t-c", True), ("t-d", True), ("t-a", False), ("t-b", False)]


def test_el_orden_de_las_graves_respeta_el_de_antes_entre_ellas(puerta):
    con = puerta()
    for clave in ("a", "b", "c"):
        CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave=clave, titulo=f"t-{clave}", grave=True))
    con.execute("UPDATE tareas SET vence_en = '2026-10-09' WHERE titulo = 't-a'")
    con.execute("UPDATE tareas SET vence_en = '2026-10-05' WHERE titulo = 't-c'")
    titulos = [f["titulo"] for f in
               CLIENTE.get("/api/code/tareas", headers=_h(CLAVE_SALA)).json()["tareas"]]
    assert titulos == ["t-c", "t-a", "t-b"]       # con fecha primero, sin fecha al final


# ═══════════════════════════════════════════════════════════════════════
# Lo que se rechaza, y que NO se guarda nada
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("cuerpo", [
    {"clave": "", "titulo": "t"},
    {"clave": "   ", "titulo": "t"},
    {"clave": "con espacio", "titulo": "t"},
    {"clave": "con/barra", "titulo": "t"},
    {"clave": "ñandú", "titulo": "t"},
    {"clave": "x" * (api_code.LARGO_CLAVE_ALERTA + 1), "titulo": "t"},
    {"clave": "k", "titulo": ""},
    {"clave": "k", "titulo": "  \n "},
    {"clave": "k"},
    {"titulo": "t"},
    {"clave": "k", "titulo": "t", "grave": "si"},
    {"clave": "k", "titulo": "t", "grave": 1},
    {"clave": "k", "titulo": "t", "detalle": 123},
    {"clave": 5, "titulo": "t"},
    {"clave": "k", "titulo": "t", "urgente": True},
])
def test_pedido_mal_formado_es_422_y_no_escribe_nada(puerta, cuerpo):
    con = puerta()
    r = CLIENTE.post(RUTA, headers=_h(), json=cuerpo)
    assert r.status_code == 422
    assert _n(con) == 0 and _n(con, "log_acciones") == 0


def test_cuerpo_que_no_es_json_es_422(puerta):
    con = puerta()
    r = CLIENTE.post(RUTA, headers={**_h(), "Content-Type": "application/json"}, content=b"no es json")
    assert r.status_code == 422 and _n(con) == 0


def test_el_limite_exacto_pasa_sin_recortar_y_uno_mas_se_recorta_y_se_dice(puerta):
    con = puerta()
    ok = CLIENTE.post(RUTA, headers=_h(), json=_alerta(
        clave="a", titulo="t" * api_code.LARGO_TITULO_ALERTA,
        detalle="d" * api_code.LARGO_DETALLE_ALERTA)).json()
    assert ok["recortado"] is False
    largo = CLIENTE.post(RUTA, headers=_h(), json=_alerta(
        clave="b", titulo="t" * (api_code.LARGO_TITULO_ALERTA + 1),
        detalle="d" * (api_code.LARGO_DETALLE_ALERTA + 1))).json()
    assert largo["recortado"] is True
    t = con.execute("SELECT titulo, detalle FROM tareas WHERE id = ?", (largo["tarea_id"],)).fetchone()
    assert len(t["titulo"]) == api_code.LARGO_TITULO_ALERTA and t["titulo"].endswith("[recortado]")
    assert len(t["detalle"]) == api_code.LARGO_DETALLE_ALERTA and t["detalle"].endswith("[recortado]")
    solo_detalle = CLIENTE.post(RUTA, headers=_h(), json=_alerta(
        clave="c", detalle="d" * (api_code.LARGO_DETALLE_ALERTA + 1))).json()
    assert solo_detalle["recortado"] is True


def test_el_titulo_pierde_saltos_de_linea(puerta):
    con = puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(titulo="uno\ndos\r\n  tres"))
    assert con.execute("SELECT titulo FROM tareas").fetchone()[0] == "uno dos tres"


def test_el_detalle_de_una_tarea_reusada_deja_de_crecer_en_el_tope(puerta):
    con = puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="d" * api_code.LARGO_DETALLE_ALERTA))
    antes = con.execute("SELECT detalle FROM tareas").fetchone()[0]
    for _ in range(3):
        r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="otra repetición")).json()
        assert r["reusada"] is True and r["repeticion_omitida"] is True
    assert con.execute("SELECT detalle FROM tareas").fetchone()[0] == antes
    assert con.execute("SELECT ultima_alarma_en FROM tareas").fetchone()[0] == "2026-10-04 10:00:00"


def test_bajo_el_tope_la_repeticion_si_se_agrega_y_no_se_dice_omitida(puerta):
    con = puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="uno"))
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="dos")).json()
    assert r["repeticion_omitida"] is False
    assert "dos" in con.execute("SELECT detalle FROM tareas").fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════
# La clave y los permisos
# ═══════════════════════════════════════════════════════════════════════

def test_sin_clave_o_con_clave_mala_401_y_nada_se_guarda(puerta):
    con = puerta()
    assert CLIENTE.post(RUTA, json=_alerta()).status_code == 401
    assert CLIENTE.post(RUTA, headers=_h("inventada"), json=_alerta()).status_code == 401
    assert CLIENTE.post(RUTA, json={}).status_code == 401      # la clave va antes que el cuerpo
    assert _n(con) == 0


def test_la_clave_de_la_sala_no_crea_alertas(puerta):
    con = puerta()
    assert CLIENTE.post(RUTA, headers=_h(CLAVE_SALA), json=_alerta()).status_code == 403
    assert _n(con) == 0


def test_natalia_solo_puede_crear_alertas_en_todas_las_demas_rutas_es_403(puerta):
    """Las rutas salen de lo que FastAPI registró (`rutas_registradas`)."""
    con = puerta()
    con.execute("INSERT INTO tareas (titulo, responsable_chat_id, area, estado) "
                "VALUES ('de code', ?, ?, 'pendiente')", (config.CHAT_ID_CODE, db.AREA_TECNICA))
    con.commit()
    rutas = api_code.rutas_registradas(panel.app)
    otras = [(m, p) for m, p, perm in rutas if perm != "alertas:crear"]
    assert len(otras) >= 5 and any(perm == "alertas:crear" for _, _, perm in rutas)
    for metodo, path in otras:
        api_code._pedidos_por_quien.clear()
        r = CLIENTE.request(metodo, path.replace("{tid}", "1"), headers=_h(),
                            json={"texto": "x"} if metodo == "POST" else None)
        assert r.status_code == 403, (metodo, path, r.status_code)
    assert con.execute("SELECT COUNT(*) FROM tareas WHERE estado <> 'pendiente'").fetchone()[0] == 0


def test_la_ruta_sale_en_el_censo_con_su_permiso_y_es_la_unica_con_ese_permiso():
    registradas = {(m, p): perm for m, p, perm in api_code.rutas_registradas(panel.app)}
    assert registradas[("POST", "/api/code/alertas")] == "alertas:crear"
    assert [k for k, v in registradas.items() if v == "alertas:crear"] == [("POST", "/api/code/alertas")]


# ═══════════════════════════════════════════════════════════════════════
# Migraciones sin aplicar: el sistema dice 503, no miente
# ═══════════════════════════════════════════════════════════════════════

def test_sin_las_columnas_de_la_alerta_es_503_y_no_escribe(puerta):
    con = puerta(sin_clave=True, sin_grave=True)
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta())
    assert r.status_code == 503 and "migración" in r.json()["detail"]
    assert _n(con) == 0 and _n(con, "log_acciones") == 0


def test_sin_la_columna_grave_una_alerta_leve_se_crea_igual(puerta):
    con = puerta(sin_grave=True)
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=False))
    assert r.status_code == 200 and r.json()["grave"] is False
    assert CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=False)).json()["reusada"] is True
    assert _n(con) == 1


def test_sin_la_columna_grave_una_alerta_grave_es_503_y_no_escribe_nada(puerta):
    con = puerta(sin_grave=True)
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True))
    assert r.status_code == 503 and _n(con) == 0 and _n(con, "log_acciones") == 0


def test_sin_la_columna_grave_una_grave_sobre_una_tarea_abierta_no_toca_el_detalle(puerta):
    con = puerta(sin_grave=True)
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="original"))
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(detalle="repetida", grave=True))
    assert r.status_code == 503
    assert con.execute("SELECT detalle FROM tareas").fetchone()[0] == "original"


def test_sin_la_columna_grave_la_sala_sigue_leyendo_y_todas_salen_leves(puerta):
    puerta(sin_grave=True)
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave="a"))
    r = CLIENTE.get("/api/code/tareas", headers=_h(CLAVE_SALA))
    assert r.status_code == 200
    assert [f["grave"] for f in r.json()["tareas"]] == [False]


def test_las_cinco_alarmas_de_lucy_siguen_igual_sin_pasar_grave(puerta):
    """Quien llama a la puerta sin `grave` (las alarmas de Lucy) no nombra la
    columna: funciona con la migración sin aplicar y deja `grave` en false."""
    con = puerta(sin_grave=True)
    tid = asyncio.run(db.crear_o_reusar_alerta_tecnica("backup", "Respaldo", "d"))
    assert tid == 1
    assert asyncio.run(db.crear_o_reusar_alerta_tecnica("backup", "Respaldo", "d")) == 1
    assert _n(con) == 1


# ═══════════════════════════════════════════════════════════════════════
# Nada por Telegram
# ═══════════════════════════════════════════════════════════════════════

def test_crear_leer_y_reusar_no_crean_ningun_bot_de_telegram(puerta, monkeypatch):
    import telegram
    creados = []

    class _BotQueRecuerda:
        def __init__(self, *a, **k):
            creados.append("creado")

        async def send_message(self, *a, **k):
            creados.append("enviado")
    monkeypatch.setattr(telegram, "Bot", _BotQueRecuerda)
    puerta()
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True))
    CLIENTE.post(RUTA, headers=_h(), json=_alerta(grave=True))
    CLIENTE.get("/api/code/tareas", headers=_h(CLAVE_SALA))
    assert creados == []


def test_el_bot_que_recuerda_si_ve_un_aviso_real(monkeypatch):
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


def test_nada_de_la_alerta_llega_a_un_log(puerta, caplog):
    import logging
    puerta()
    with caplog.at_level(logging.DEBUG):
        CLIENTE.post(RUTA, headers=_h(), json=_alerta(
            clave="secreta-clave-1", titulo="Titulo-unico-9", detalle=CLIENTE_INVENTADO, grave=True))
        CLIENTE.post(RUTA, headers=_h(), json=_alerta(
            clave="secreta-clave-1", titulo="Titulo-unico-9", detalle=CLIENTE_INVENTADO))
    visto = " ".join(r.getMessage() for r in caplog.records)
    for secreto in (CLIENTE_INVENTADO, "Titulo-unico-9", "secreta-clave-1", CLAVE_NATALIA):
        assert secreto not in visto


# ═══════════════════════════════════════════════════════════════════════
# La página de Tiziano: /tareas y /tareas/{id}
# ═══════════════════════════════════════════════════════════════════════

def test_graves_de_cuenta_solo_las_graves_que_siguen_siendo_de_code(puerta):
    con = puerta()
    for resp, grave in ((config.CHAT_ID_CODE, 1), (config.CHAT_ID_CODE, 0), (424242, 1)):
        con.execute("INSERT INTO tareas (titulo, responsable_chat_id, grave) VALUES ('x', ?, ?)",
                    (resp, grave))
    assert asyncio.run(db.graves_de([1, 2, 3])) == {1}
    assert asyncio.run(db.graves_de([])) == set()


def test_graves_de_sin_la_columna_es_vacio_y_no_revienta(puerta):
    con = puerta(sin_grave=True)
    con.execute("INSERT INTO tareas (titulo, responsable_chat_id) VALUES ('x', ?)",
                (config.CHAT_ID_CODE,))
    assert asyncio.run(db.graves_de([1])) == set()


def _fila(id_, titulo, grave, estado="pendiente"):
    from datetime import datetime, timezone
    return {"id": id_, "titulo": titulo, "estado": estado, "vence_en": None,
            "creado_en": datetime(2026, 10, 4, tzinfo=timezone.utc),
            "bandeja_id": 900 + id_, "responsable_chat_id": config.CHAT_ID_CODE,
            "completado_en": None, "area": None, "proyecto_id": None,
            "proyecto_nombre": None, "primero_id": None, "primero_titulo": None,
            "primero_estado": None, "tomada_en": None, "grave": grave}


def _pagina(filas):
    """La página real `/tareas` (y `db.tareas_por_grupo` real) sobre filas
    fabricadas: el mismo estilo de `tests/test_code_tomada.py`."""
    from test_code_tomada import _ConnPanel, _CurPanel, _PoolPanel, _correr, _pedir

    class _CurG(_CurPanel):
        async def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT id FROM tareas WHERE grave AND borrado_en IS NULL"):
                self._filas = [{"id": f["id"]} for f in self._conn.tareas
                               if f.get("grave")
                               and f.get("responsable_chat_id") == config.CHAT_ID_CODE]
                return self
            return await super().execute(sql, params)

    class _ConnG(_ConnPanel):
        def cursor(self, row_factory=None):
            return _CurG(self)

        async def execute(self, sql, params=None):
            return await _CurG(self).execute(sql, params)

    guardado = db.pool
    db.pool = _PoolPanel(_ConnG(filas))
    try:
        r = _correr(panel.tareas(_pedir()))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    return r.body.decode()


def test_la_pagina_marca_la_grave_y_no_la_leve():
    html = _pagina([_fila(1, "Tarea leve unica", False), _fila(2, "Tarea grave unica", True)])
    assert html.count("🚨 GRAVE") == 1
    assert html.index("🚨 GRAVE") > html.index("Tarea grave unica")


def test_la_pagina_pone_las_graves_primero_dentro_de_su_grupo():
    html = _pagina([_fila(1, "Primera leve", False), _fila(2, "Segunda leve", False),
                    _fila(3, "Tercera grave", True)])
    assert html.index("Tercera grave") < html.index("Primera leve") < html.index("Segunda leve")


def test_la_pagina_no_marca_una_grave_ya_hecha():
    from datetime import datetime, timezone
    f = _fila(1, "Grave ya cerrada", True, estado="hecha")
    f["completado_en"] = datetime.now(timezone.utc)
    html = _pagina([f])
    assert "Grave ya cerrada" in html and "🚨 GRAVE" not in html


def test_la_pagina_sin_graves_no_cambia():
    html = _pagina([_fila(1, "Solo una leve", False)])
    assert "Solo una leve" in html and "GRAVE" not in html


def _detalle(estado, grave):
    from datetime import datetime, timezone
    from test_code_tomada import (_ConnDetalle, _CurDetalle, _PoolDetalle, _correr,
                                  _fila_panel, _pedir)

    class _CurD(_CurDetalle):
        async def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT id FROM tareas WHERE grave AND borrado_en IS NULL"):
                t = self._conn.tarea
                self._filas = [{"id": t["id"]}] if t.get("grave") else []
                return self
            return await super().execute(sql, params)

    class _ConnD(_ConnDetalle):
        def cursor(self, row_factory=None):
            return _CurD(self)

        async def execute(self, sql, params=None):
            return await _CurD(self).execute(sql, params)

    fila = _fila_panel(1, "Detalle de alerta", responsable=config.CHAT_ID_CODE)
    fila.update(estado=estado, grave=grave, detalle="d")
    guardado = db.pool
    db.pool = _PoolDetalle(_ConnD(fila))
    try:
        r = _correr(panel.tarea_detalle(_pedir("/tareas/1"), 1))
    finally:
        db.pool = guardado
    assert r.status_code == 200
    return r.body.decode()


def test_el_detalle_marca_la_grave_pendiente_y_no_la_leve_ni_la_hecha():
    assert "🚨 GRAVE" in _detalle("pendiente", True)
    assert "GRAVE" not in _detalle("pendiente", False)
    assert "GRAVE" not in _detalle("hecha", True)


# ═══════════════════════════════════════════════════════════════════════
# La página de PROYECTOS (la que lee Tiziano): `/proyectos` real, sobre la
# base de SQLite con el esquema de verdad (`tests/test_pagina_proyectos.py`)
# ═══════════════════════════════════════════════════════════════════════

from test_pagina_proyectos import _dia, gente, mundo, tareas_en, ver  # noqa: E402,F401

CODE = config.CHAT_ID_CODE


def _grave(mundo, *ids):
    for i in ids:
        mundo.con.execute("UPDATE tareas SET grave = 1 WHERE id = ?", (i,))


def test_proyectos_pone_la_grave_primero_y_la_marca(mundo):
    mundo.proyecto(1, "Sala", area="IA")
    mundo.tarea(10, "Leve uno", proyecto=1, responsable=CODE)
    mundo.tarea(11, "Leve dos", proyecto=1, responsable=CODE)
    mundo.tarea(12, "Grave tres", proyecto=1, responsable=CODE)
    _grave(mundo, 12)
    html = ver(mundo, p=1)
    assert tareas_en(html)[:3] == [12, 10, 11]
    assert html.count("🚨 grave") == 1
    bloque = html.split('data-tarea="12"')[1].split('data-tarea="10"')[0]
    assert "🚨 grave" in bloque


def test_proyectos_la_grave_va_antes_que_la_vencida(mundo):
    mundo.proyecto(1, "Sala", area="IA")
    mundo.tarea(10, "Vencida leve", proyecto=1, responsable=CODE, vence=_dia(-3))
    mundo.tarea(11, "Grave sin fecha", proyecto=1, responsable=CODE)
    _grave(mundo, 11)
    assert tareas_en(ver(mundo, p=1))[:2] == [11, 10]


def test_proyectos_no_marca_una_grave_ya_hecha_ni_una_leve(mundo):
    mundo.proyecto(1, "Sala", area="IA")
    mundo.tarea(10, "Grave hecha", proyecto=1, responsable=CODE, estado="hecha",
                completado=_dia(-1))
    mundo.tarea(11, "Leve", proyecto=1, responsable=CODE)
    _grave(mundo, 10)
    html = ver(mundo, p=1)
    assert "Grave hecha" in html and "🚨 grave" not in html


def test_proyectos_no_marca_una_grave_que_se_reasigno_a_una_persona(mundo):
    mundo.proyecto(1, "Sala", area="IA")
    mundo.tarea(10, "Era grave de Code", proyecto=1, responsable=config.CHAT_ID_DUENO)
    _grave(mundo, 10)
    assert "🚨 grave" not in ver(mundo, p=1)


def test_proyectos_tambien_las_sueltas_del_grupo_van_con_su_marca_y_primero(mundo):
    mundo.proyecto(1, "Otro", area="IA")
    mundo.tarea(10, "Suelta leve", area="IA", responsable=CODE)
    mundo.tarea(11, "Suelta grave", area="IA", responsable=CODE)
    _grave(mundo, 11)
    html = ver(mundo, g="IA")
    assert html.count("🚨 grave") == 1
    assert tareas_en(html).index(11) < tareas_en(html).index(10)


def test_proyectos_sin_graves_se_ve_igual_que_antes(mundo):
    mundo.proyecto(1, "Sala", area="IA")
    mundo.tarea(10, "A", proyecto=1, responsable=CODE, vence=_dia(2))
    mundo.tarea(11, "B", proyecto=1, responsable=CODE, vence=_dia(-1))
    mundo.tarea(12, "C", proyecto=1, responsable=CODE)
    html = ver(mundo, p=1)
    assert tareas_en(html)[:3] == [11, 10, 12] and "🚨" not in html
