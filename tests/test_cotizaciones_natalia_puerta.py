# -*- coding: utf-8 -*-
"""La puerta de Natalia para las FORMALES (Lucy 4 del diseño «Cotización formal que hace Natalia», 24-oct-2026).

Espejo de `test_facturas_natalia.py`: las mismas garantías, por las RUTAS reales de `/api/code` y la puerta REAL de `db/db.py`
sobre SQLite con las migraciones reales (`test_guardar_cotizacion._base`, con `proyectos` mínima y `cliente_noco_id`).

  1. LA PUERTA: seis rutas, cada una con su permiso propio (censo sacado de `rutas_registradas`); sin llave, 401; con llave
     sin ese permiso, 403; y la base no cambia.
  2. SOLO SOBRE EL PROYECTO ABIERTO: sin proyecto abierto (o con uno pausado o de IA) → 409 y nada escrito.
  3. NACE BORRADOR DE NATALIA: `hecha_por = natalia`, sin chat, con el número de la serie y su huella con actor `natalia`.
  4. PEDIR LA APROBACIÓN NO CAMBIA LA FORMAL: el envío nace `pendiente` y no se duplica.
  5. SIN APROBACIÓN NO HAY PDF; CON APROBACIÓN SÍ, Y SOLO SI LA FORMAL NO CAMBIÓ DESDE QUE SE PIDIÓ.
  6. «ENVIADA» SOLO SI NATALIA LO ANOTA; anotar «no salió» exige motivo y deja la formal en borrador.
  7. NATALIA NO VE FORMALES AJENAS (de la casa o de otro cliente): 404, y la lista trae solo las suyas.
  8. HERMANOS: las rutas de formales y las de facturas tienen las mismas formas (método, ruta y permiso).

FRONTERA, dicha una vez: SQLite, no Postgres; Telegram y la aprobación de Rosi son simulados (la tarjeta es la pieza 5). NINGÚN
nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_cotizaciones_natalia_puerta.py -q
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient  # primero: otros módulos de prueba cambian `httpx` al importarse

import test_guardar_cotizacion as g  # primero: fija el entorno de las pruebas antes de cargar `config`
import test_cotizaciones_tablas as t
import test_grupo_ia as gi
import config
import db.db as db
import web.api_code as api_code
import web.app as panel

K, K_SIN, K_SALA = "llave-natalia-formales-de-prueba", "llave-sin-formales", "llave-de-la-sala"
DUENO = config.CHAT_ID_DUENO
ROSI = DUENO + 5001
HTTP = TestClient(panel.app)
PERMISOS = sorted({perm for _, ruta, perm in api_code.rutas_registradas(panel.app) if "/cotizaciones" in ruta and "{noco_id}" in ruta})
BASE = "/api/code/clientes/7/cotizaciones"
CONTENIDO_BASE = {"opciones": [{"nombre": "Única", "renglones": [{"descripcion": "Sesión de prueba", "detalle": "", "total": "62500.00"}]}],
                  "condiciones": []}


# ── El mundo ────────────────────────────────────────────────────────────────────────────────────────

def _base():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = None
    contador = iter(range(1, 10**6))
    inicio = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    con.create_function("now", 0, lambda: g._a_texto(inicio + timedelta(microseconds=next(contador))))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, cliente_nombre TEXT, cliente_noco_id INTEGER, "
                "area TEXT, estado TEXT, borrado_en TEXT)")
    # Cliente 7: un proyecto abierto. Cliente 8: solo uno pausado. Cliente 9: solo uno de IA (no cuenta).
    con.execute("INSERT INTO proyectos VALUES (1, 'Proyecto de prueba uno', 'Cliente Inventado Uno', 7, 'CDS', 'activo', NULL)")
    con.execute("INSERT INTO proyectos VALUES (2, 'Proyecto de prueba dos', 'Cliente Inventado Dos', 8, 'CDS', 'pausado', NULL)")
    con.execute("INSERT INTO proyectos VALUES (3, 'Proyecto de prueba tres', 'Cliente Inventado Tres', 9, 'IA', 'activo', NULL)")
    con.execute("CREATE TABLE log_acciones (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT, "
                "tabla TEXT, registro_id INTEGER, antes TEXT, despues TEXT, motivo TEXT, bandeja_id INTEGER)")
    for texto in (t._texto_migracion(), t._texto_migracion_de_drive(), t._migracion_de_las_formales()):
        for s in gi._sentencias(texto):
            if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
                con.execute(t._a_sqlite(s))
    con.commit()
    return con


@pytest.fixture
def mundo(monkeypatch):
    con = _base()
    monkeypatch.setattr(db, "pool", g._Pool(con))
    monkeypatch.setattr(db, "hoy_rd", lambda: date(2026, 10, 14))
    monkeypatch.setattr(config, "CLAVES_API_CODE", {K: "natalia", K_SIN: "natalia_sin_formales", K_SALA: "sala_mac"})
    monkeypatch.setattr(config, "PERMISOS_API_CODE", {
        "natalia": frozenset(PERMISOS), "natalia_sin_formales": frozenset({"tareas:listar"}),
        "sala_mac": frozenset(PERMISOS)})
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {DUENO: "Dueno Inventado", ROSI: "Rosi Inventada"})
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, ROSI))
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Rosi Inventada")
    return con


def llamar(metodo, ruta, *, clave=K, json=None):
    api_code._pedidos_por_quien.clear()
    cabeceras = {"Authorization": f"Bearer {clave}"} if clave else {}
    return HTTP.request(metodo, ruta, json=json, headers=cabeceras)


def crear(contenido=None, validez=15, noco=7, clave=K):
    return llamar("POST", f"/api/code/clientes/{noco}/cotizaciones", clave=clave,
                  json={"contenido": contenido or CONTENIDO_BASE, "validez_dias": validez})


def pedir(cid, canal="whatsapp", noco=7, clave=K):
    return llamar("POST", f"/api/code/clientes/{noco}/cotizaciones/{cid}/envios", clave=clave, json={"canal": canal})


def pdf(cid, eid, noco=7, clave=K):
    return llamar("GET", f"/api/code/clientes/{noco}/cotizaciones/{cid}/envios/{eid}/pdf", clave=clave)


def anotar(cid, eid, salio, motivo=None, noco=7, clave=K):
    cuerpo = {"salio": salio}
    if motivo is not None:
        cuerpo["motivo"] = motivo
    return llamar("POST", f"/api/code/clientes/{noco}/cotizaciones/{cid}/envios/{eid}/resultado", clave=clave, json=cuerpo)


def _aprobar(con, envio_id):
    """Lo que hará la tarjeta de la pieza 5 cuando Rosi toque Aprobar: el envío queda `aprobado` con quién y cuándo."""
    con.execute("UPDATE cotizacion_envios SET estado = 'aprobado', aprobado_por_chat_id = ?, aprobado_en = ? WHERE id = ?",
                (ROSI, "2026-10-14T13:00:00+00:00", envio_id))
    con.commit()


def _cuenta(con, tabla):
    return con.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]


def _fila(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, cur.fetchone()))


def _formal_de_la_casa(con, noco_id=7):
    """Una formal que hizo el equipo (no Natalia): debe ser invisible para ella."""
    contenido = db.contenido_de_cotizacion_que_vale(CONTENIDO_BASE)
    con.execute("INSERT INTO cotizaciones (proyecto_id, anio, numero, estado, para_nombre, fecha, validez_dias, contenido, "
                "creado_por_chat_id, hecha_por) VALUES (?, 2026, 900, 'borrador', 'Cliente de la casa', '2026-10-14', 15, ?, ?, "
                "'equipo')", (1 if noco_id == 7 else 2, contenido, DUENO))
    con.commit()
    return con.execute("SELECT id FROM cotizaciones WHERE numero = 900").fetchone()[0]


# ── 1. La puerta ─────────────────────────────────────────────────────────────────────────────────────

def test_las_rutas_de_las_formales_son_seis_y_cada_una_exige_su_propio_permiso():
    rutas = [(m, p, perm) for m, p, perm in api_code.rutas_registradas(panel.app) if "{noco_id}/cotizaciones" in p]
    assert sorted((m, p) for m, p, _ in rutas) == [
        ("GET", "/api/code/clientes/{noco_id}/cotizaciones"),
        ("GET", "/api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}"),
        ("GET", "/api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}/envios/{envio_id}/pdf"),
        ("POST", "/api/code/clientes/{noco_id}/cotizaciones"),
        ("POST", "/api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}/envios"),
        ("POST", "/api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}/envios/{envio_id}/resultado")]
    permisos = [perm for _, _, perm in rutas]
    assert len(set(permisos)) == len(permisos) == 6, permisos
    assert set(permisos) == {"cotizaciones:crear", "cotizaciones:listar", "cotizaciones:leer", "cotizaciones:pedir_envio",
                             "cotizaciones:pdf", "cotizaciones:anotar_envio"}


def _llamadas_de_cada_ruta():
    """Un pedido válido por cada ruta de las formales, con la forma de su ruta (la que sale de `app.routes`)."""
    cuerpos = {"POST /api/code/clientes/{noco_id}/cotizaciones": {"contenido": CONTENIDO_BASE, "validez_dias": 15},
               "POST /api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}/envios": {"canal": "correo"},
               "POST /api/code/clientes/{noco_id}/cotizaciones/{cotizacion_id}/envios/{envio_id}/resultado": {"salio": True}}
    salida = []
    for m, p, perm in api_code.rutas_registradas(panel.app):
        if "{noco_id}/cotizaciones" not in p:
            continue
        ruta = p.replace("{noco_id}", "7").replace("{cotizacion_id}", "1").replace("{envio_id}", "1")
        salida.append((m, ruta, perm, cuerpos.get(f"{m} {p}")))
    return salida


def test_sin_llave_es_401_y_con_llave_sin_su_permiso_es_403_y_no_se_escribe(mundo):
    antes = (_cuenta(mundo, "cotizaciones"), _cuenta(mundo, "cotizacion_envios"), _cuenta(mundo, "log_acciones"))
    llamadas = _llamadas_de_cada_ruta()
    assert len(llamadas) == 6
    for m, ruta, _perm, cuerpo in llamadas:
        assert llamar(m, ruta, clave=None, json=cuerpo).status_code == 401, (m, ruta)
        assert llamar(m, ruta, clave=K_SIN, json=cuerpo).status_code == 403, (m, ruta)
    assert (_cuenta(mundo, "cotizaciones"), _cuenta(mundo, "cotizacion_envios"), _cuenta(mundo, "log_acciones")) == antes


# ── 2. Solo sobre el proyecto abierto ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("noco", [8, 9, 99])
def test_sin_proyecto_abierto_es_409_y_no_se_escribe_nada(mundo, noco):
    r = crear(noco=noco)
    assert r.status_code == 409, r.text
    assert "proyecto abierto" in r.json()["detail"]
    assert _cuenta(mundo, "cotizaciones") == 0


# ── 3. Nace borrador de Natalia ──────────────────────────────────────────────────────────────────────

def test_crear_sobre_el_proyecto_abierto_nace_borrador_de_natalia_sin_chat(mundo):
    r = crear()
    assert r.status_code == 200, r.text
    cot = r.json()["cotizacion"]
    assert cot["estado"] == "borrador" and cot["hecha_por"] == "natalia"
    assert cot["codigo"] == "COT-2026-001" and cot["para_nombre"] == "Cliente Inventado Uno"
    fila = _fila(mundo, "SELECT proyecto_id, estado, hecha_por, creado_por_chat_id, enviada_en FROM cotizaciones WHERE id = ?",
                 (cot["cotizacion_id"],))
    assert fila == {"proyecto_id": 1, "estado": "borrador", "hecha_por": "natalia", "creado_por_chat_id": None,
                    "enviada_en": None}
    huella = _fila(mundo, "SELECT actor, accion, tabla FROM log_acciones WHERE registro_id = ? ORDER BY id DESC LIMIT 1",
                   (cot["cotizacion_id"],))
    assert huella == {"actor": "natalia", "accion": "crear", "tabla": "cotizaciones"}


def test_contenido_o_validez_que_no_valen_son_422_y_no_escriben(mundo):
    malos = [({"opciones": []}, 15), (dict(CONTENIDO_BASE, extra=1), 15), (CONTENIDO_BASE, 10)]
    for contenido, validez in malos:
        assert crear(contenido, validez).status_code == 422, (contenido, validez)
    assert _cuenta(mundo, "cotizaciones") == 0


# ── 4 y 5. Pedir, PDF y candado de versión ───────────────────────────────────────────────────────────

def test_pedir_la_aprobacion_deja_un_pendiente_sin_cambiar_la_formal_y_no_duplica(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    r = pedir(cid, "whatsapp")
    assert r.status_code == 200, r.text
    envio = r.json()["envio"]
    assert r.json()["reusada"] is False and envio["estado"] == "pendiente" and envio["aprobada"] is False
    assert envio["pdf_listo"] is False
    assert _fila(mundo, "SELECT estado FROM cotizaciones WHERE id = ?", (cid,))["estado"] == "borrador"
    otra = pedir(cid, "correo")
    assert otra.json()["reusada"] is True and otra.json()["envio"]["envio_id"] == envio["envio_id"]
    assert _cuenta(mundo, "cotizacion_envios") == 1


def test_sin_aprobacion_el_pdf_no_baja(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    r = pdf(cid, eid)
    assert r.status_code == 409
    assert not r.content.startswith(b"%PDF")


def test_con_aprobacion_el_pdf_baja_y_la_formal_sigue_borrador(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    _aprobar(mundo, eid)
    r = pdf(cid, eid)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf" and "COT-2026-001.pdf" in r.headers["content-disposition"]
    assert _fila(mundo, "SELECT estado FROM cotizaciones WHERE id = ?", (cid,))["estado"] == "borrador"


def test_si_la_formal_cambio_despues_de_pedir_la_aprobacion_el_pdf_no_baja(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    mundo.execute("UPDATE cotizaciones SET cambiado_en = '2026-10-15T09:00:00+00:00' WHERE id = ?", (cid,))
    mundo.commit()
    _aprobar(mundo, eid)
    r = pdf(cid, eid)
    assert r.status_code == 409 and "pedirla otra vez" in r.json()["detail"]
    assert not r.content.startswith(b"%PDF")


def test_el_pdf_no_baja_si_la_formal_ya_salio_por_otro_envio(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid, "whatsapp").json()["envio"]["envio_id"]
    _aprobar(mundo, eid)
    mundo.execute("INSERT INTO cotizacion_envios (cotizacion_id, canal, estado, aprobado_por_chat_id, aprobado_en, enviado_en, "
                  "visto_cambiado_en) VALUES (?, 'correo', 'enviado', ?, ?, ?, (SELECT cambiado_en FROM cotizaciones WHERE id = ?))",
                  (cid, ROSI, "2026-10-14T13:00:00+00:00", "2026-10-14T14:00:00+00:00", cid))
    mundo.commit()
    r = pdf(cid, eid)
    assert r.status_code == 409 and "no se manda dos veces" in r.json()["detail"]


# ── 6. «Enviada» solo si Natalia lo anota ────────────────────────────────────────────────────────────

def test_anotar_que_salio_marca_la_formal_enviada_y_el_envio_enviado(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    _aprobar(mundo, eid)
    r = anotar(cid, eid, True)
    assert r.status_code == 200, r.text
    assert r.json()["envio"]["estado"] == "enviado"
    cot = _fila(mundo, "SELECT estado, enviada_en FROM cotizaciones WHERE id = ?", (cid,))
    assert cot["estado"] == "enviada" and cot["enviada_en"] is not None
    assert llamar("GET", f"{BASE}/{cid}").json()["cotizacion"]["estado"] == "enviada"


def test_anotar_que_no_salio_exige_motivo_y_deja_la_formal_en_borrador(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    _aprobar(mundo, eid)
    assert anotar(cid, eid, False).status_code == 422
    r = anotar(cid, eid, False, "No contestó el correo")
    assert r.status_code == 200 and r.json()["envio"]["estado"] == "no_se_pudo"
    assert _fila(mundo, "SELECT estado FROM cotizaciones WHERE id = ?", (cid,))["estado"] == "borrador"


def test_anotar_sin_aprobacion_es_409_y_no_cambia_la_formal(mundo):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    assert anotar(cid, eid, True).status_code == 409
    assert _fila(mundo, "SELECT estado FROM cotizaciones WHERE id = ?", (cid,))["estado"] == "borrador"
    assert _fila(mundo, "SELECT estado FROM cotizacion_envios WHERE id = ?", (eid,))["estado"] == "pendiente"


# ── 7. Natalia no ve formales ajenas ─────────────────────────────────────────────────────────────────

def test_natalia_solo_lee_las_formales_que_hizo_de_ese_cliente(mundo):
    propia = crear().json()["cotizacion"]["cotizacion_id"]
    ajena = _formal_de_la_casa(mundo)
    lista = llamar("GET", BASE).json()["cotizaciones"]
    assert [c["cotizacion_id"] for c in lista] == [propia]
    assert llamar("GET", f"{BASE}/{ajena}").status_code == 404
    assert llamar("GET", f"/api/code/clientes/8/cotizaciones/{propia}").status_code == 404
    assert pedir(ajena).status_code == 404
    assert pdf(propia, 1, noco=8).status_code == 404


# ── 8. Hermanos: las formales y las facturas de Natalia tienen las mismas formas ─────────────────────

def test_las_rutas_de_formales_son_las_mismas_formas_que_las_de_facturas():
    def forma(p, sustantivo):
        return (p.replace(sustantivo, "X").replace("{factura_id}", "{ID}").replace("{cotizacion_id}", "{ID}"))
    facturas = {(m, forma(p, "facturas"), perm.split(":", 1)[1])
                for m, p, perm in api_code.rutas_registradas(panel.app) if "{noco_id}/facturas" in p}
    formales = {(m, forma(p, "cotizaciones"), perm.split(":", 1)[1])
                for m, p, perm in api_code.rutas_registradas(panel.app) if "{noco_id}/cotizaciones" in p}
    assert facturas and facturas == formales, (facturas ^ formales)


def test_la_sala_con_todos_los_permisos_no_entra_porque_no_es_natalia(mundo):
    """`solo_de`: otra llave con TODOS los permisos de las formales (la de la sala) sale 403 en cada ruta, y nada se escribe."""
    antes = _cuenta(mundo, "cotizaciones")
    for m, ruta, _perm, cuerpo in _llamadas_de_cada_ruta():
        assert llamar(m, ruta, clave=K_SALA, json=cuerpo).status_code == 403, (m, ruta)
    assert _cuenta(mundo, "cotizaciones") == antes
