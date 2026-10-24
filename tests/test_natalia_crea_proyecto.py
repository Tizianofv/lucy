# -*- coding: utf-8 -*-
"""Natalia crea el proyecto de un cliente sin proyecto abierto (Lucy 2, diseño «Cotización formal que hace Natalia»,
aprobado 24-oct-2026): `POST /api/code/clientes/{noco_id}/proyecto`, permiso `proyectos:crear_de_cliente`, solo `natalia`.

CAMINO DE PRODUCCIÓN: la app FastAPI real con su puerta (`requiere`), la puerta única REAL `db.crear_proyecto(desde="natalia")`
corriendo su SQL de verdad contra SQLite (el mundo de `test_pagina_proyectos`), la lectura de «un solo activo»
(`proyecto_puede_quedar_activo`) y el aviso (`db.registrar_aviso`) escribiendo en la bandeja. Lo que se sustituye:
Noco (red externa, `noco_lectura.persona`), Drive (`cotizaciones_drive.preparar_la_carpeta`, red externa) y las claves y
permisos de `config` (son datos de prueba inventados). NINGÚN nombre, clave ni número es real (el repo es público).

Correr:  python3 -m pytest tests/test_natalia_crea_proyecto.py -q
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-natalia-crea-proyecto")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import cotizaciones_drive
import db.db as db
import noco_lectura
import web.api_code as api_code
import web.app as panel
from test_pagina_proyectos import Mundo, gente, mundo, HOY  # noqa: F401  (el mundo de SQLite con su esquema real)

pytestmark = pytest.mark.usefixtures("drive_aparte")      # ver conftest.py: Drive no se toca desde las pruebas

CLIENTE = TestClient(panel.app)
CLAVE_NATALIA = "clave-de-prueba-natalia-lucy2-no-es-real"
CLAVE_SOLO_LEER = "clave-de-prueba-natalia-solo-leer-no-es-real"
CLAVE_SALA = "clave-de-prueba-sala-lucy2-no-es-real"
PERMISO = "proyectos:crear_de_cliente"
RUTA = "/api/code/clientes/{}/proyecto"
RESPONSABLE_DE_PRUEBA = "Persona Dos"                # el nombre que `gente` le pone al chat de Rosi en el mundo de prueba


class Noco:
    """El doble de Noco: `persona(id)`. `cae=True` es Noco sin contestar."""

    def __init__(self, fichas):
        self.fichas = dict(fichas)
        self.cae = False

    async def persona(self, noco_id):
        if self.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        return self.fichas.get(noco_id)


@pytest.fixture
def noco(monkeypatch):
    n = Noco({101: {"id": 101, "nombre": "Cliente 101"}, 102: {"id": 102, "nombre": "Cliente 102"}})
    monkeypatch.setattr(noco_lectura, "persona", n.persona)
    return n


@pytest.fixture
def drive(monkeypatch):
    """Drive como doble: `lista` por omisión. El test puede cambiar lo que devuelve o hacerla reventar."""
    spy = {"llamadas": [], "devuelve": {"resultado": "lista", "causa": None, "carpeta": "https://drive/x"}, "revienta": False}

    async def preparar(proyecto_id, **kw):
        spy["llamadas"].append(proyecto_id)
        if spy["revienta"]:
            raise RuntimeError("Drive de mentira: se cayó")
        return spy["devuelve"]
    monkeypatch.setattr(cotizaciones_drive, "preparar_la_carpeta", preparar)
    return spy


@pytest.fixture
def api(monkeypatch, gente, mundo, noco, drive):
    """Claves de prueba: Natalia con el permiso nuevo (y uno de lectura), y la sala con el permiso nuevo pero sin ser Natalia.
    El responsable de los proyectos que cree Natalia es `RESPONSABLE_DE_PRUEBA` (el chat de Rosi del mundo de prueba)."""
    monkeypatch.setattr(config, "CLAVES_API_CODE", {CLAVE_NATALIA: "natalia", CLAVE_SOLO_LEER: "natalia_vieja",
                                                    CLAVE_SALA: "sala_mac"})
    monkeypatch.setattr(config, "PERMISOS_API_CODE", {
        "natalia": frozenset({PERMISO, "proyectos:leer_de_cliente"}),
        "natalia_vieja": frozenset({"proyectos:leer_de_cliente"}),
        "sala_mac": frozenset({PERMISO})})
    monkeypatch.setattr(config, "NOMBRE_DEL_RESPONSABLE_DE_PROYECTO_DE_NATALIA", RESPONSABLE_DE_PRUEBA)
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0
    return mundo


def _h(clave=CLAVE_NATALIA):
    return {"Authorization": f"Bearer {clave}"}


def _crear(noco_id, clave=CLAVE_NATALIA, cuerpo=None):
    if cuerpo is None:
        return CLIENTE.post(RUTA.format(noco_id), headers=_h(clave))
    return CLIENTE.post(RUTA.format(noco_id), headers=_h(clave), json=cuerpo)


def _foto(m) -> dict:
    """Todo lo que la ruta podría escribir: filas completas de proyectos, la huella y la bandeja."""
    return {t: [tuple(f) for f in m.con.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("proyectos", "log_acciones", "bandeja")}


def _proyectos_de(m, noco_id):
    return [dict(f) for f in m.con.execute(
        "SELECT * FROM proyectos WHERE cliente_noco_id = ? AND borrado_en IS NULL ORDER BY id", (noco_id,))]


# ═══════════════════════════════════════════════════════════════════════
# G1: la puerta. Sin clave, sin permiso o con la clave de la sala: no se escribe nada.
# ═══════════════════════════════════════════════════════════════════════

def test_G1_sin_clave_es_401_y_no_escribe_nada(api, noco, drive):
    antes = _foto(api)
    r = CLIENTE.post(RUTA.format(101))
    assert r.status_code == 401
    assert _foto(api) == antes and drive["llamadas"] == []


def test_G1_clave_de_natalia_sin_el_permiso_nuevo_es_403(api, noco, drive):
    antes = _foto(api)
    r = _crear(101, CLAVE_SOLO_LEER)
    assert r.status_code == 403
    assert _foto(api) == antes and drive["llamadas"] == []


def test_G1_la_sala_con_el_permiso_nuevo_no_puede_porque_la_ruta_es_solo_de_natalia(api, noco, drive):
    antes = _foto(api)
    r = _crear(101, CLAVE_SALA)
    assert r.status_code == 403
    assert _foto(api) == antes and drive["llamadas"] == []


# ═══════════════════════════════════════════════════════════════════════
# G2: el alta. La puerta única decide todo: grupo CDS, nombre del cliente, responsable Rosi, origen natalia.
# ═══════════════════════════════════════════════════════════════════════

def test_G2_cliente_sin_proyecto_crea_uno_con_lo_que_decide_lucy(api, noco, drive, gente):
    r = _crear(101)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["creado"] is True
    assert cuerpo["carpeta"] == "ok" and cuerpo["aviso"] == "ok"
    p = cuerpo["proyecto"]
    assert set(p) == {"id", "nombre", "estado", "area", "responsable"}
    assert (p["nombre"], p["estado"], p["area"], p["responsable"]) == ("Cliente 101", "activo", "CDS", RESPONSABLE_DE_PRUEBA)
    fila = api.con.execute("SELECT * FROM proyectos WHERE id = ?", (p["id"],)).fetchone()
    assert fila["cliente_noco_id"] == 101 and fila["cliente_nombre"] == "Cliente 101"
    assert fila["responsable_chat_id"] == gente.rosi
    assert drive["llamadas"] == [p["id"]]


def test_G2_la_huella_dice_natalia_y_el_aviso_va_a_quien_revisa(api, noco, drive, gente):
    p = _crear(101).json()["proyecto"]
    huellas = api.con.execute("SELECT actor, accion, tabla, registro_id, motivo FROM log_acciones "
                              "WHERE tabla = 'proyectos'").fetchall()
    assert [tuple(h) for h in huellas] == [("natalia", "crear", "proyectos", p["id"],
                                            "Proyecto nuevo, creado por Natalia")]
    avisos = api.con.execute("SELECT chat_id, origen, respuesta_lucy FROM bandeja").fetchall()
    assert len(avisos) == 1 and avisos[0]["chat_id"] == gente.rosi
    assert "Cliente 101" in avisos[0]["respuesta_lucy"]


def test_G2_el_cuerpo_con_campos_que_Lucy_decide_es_422(api, noco, drive):
    antes = _foto(api)
    r = _crear(101, cuerpo={"nombre": "Lo que sea"})
    assert r.status_code == 422
    assert _foto(api) == antes and drive["llamadas"] == []


# ═══════════════════════════════════════════════════════════════════════
# G3: un solo proyecto abierto por cliente. Si ya tiene uno, la puerta lo rechaza ANTES de escribir.
# ═══════════════════════════════════════════════════════════════════════

def test_G3_cliente_con_proyecto_abierto_devuelve_ese_y_no_escribe(api, noco, drive):
    api.con.execute("INSERT INTO proyectos (id, nombre, estado, area, creado_en, cliente_noco_id, cliente_nombre, "
                    "responsable_chat_id) VALUES (7, 'Cliente 101', 'activo', 'CDS', '2026-10-01T00:00:00+00:00', "
                    "101, 'Cliente 101', ?)", (config.CHAT_ID_DUENO,))
    api.con.commit()
    antes = _foto(api)
    r = _crear(101)
    assert r.status_code == 200, r.text
    assert r.json() == {"proyecto": {"id": 7, "nombre": "Cliente 101", "estado": "activo", "area": "CDS",
                                     "responsable": config.nombres_con_code().get(config.CHAT_ID_DUENO)},
                        "creado": False}
    assert _foto(api) == antes, "con proyecto abierto no se escribe nada"
    assert drive["llamadas"] == []


def test_G3_un_proyecto_pausado_no_cuenta_y_deja_crear_otro(api, noco, drive):
    api.con.execute("INSERT INTO proyectos (id, nombre, estado, area, creado_en, cliente_noco_id, cliente_nombre, "
                    "responsable_chat_id) VALUES (7, 'Cliente 101', 'pausado', 'CDS', '2026-10-01T00:00:00+00:00', "
                    "101, 'Cliente 101', ?)", (config.CHAT_ID_DUENO,))
    api.con.commit()
    r = _crear(101)
    assert r.status_code == 201, r.text
    assert r.json()["creado"] is True


def test_G3_un_proyecto_de_otro_cliente_no_estorba(api, noco, drive):
    api.con.execute("INSERT INTO proyectos (id, nombre, estado, area, creado_en, cliente_noco_id, cliente_nombre, "
                    "responsable_chat_id) VALUES (7, 'Cliente 102', 'activo', 'CDS', '2026-10-01T00:00:00+00:00', "
                    "102, 'Cliente 102', ?)", (config.CHAT_ID_DUENO,))
    api.con.commit()
    r = _crear(101)
    assert r.status_code == 201, r.text


# ═══════════════════════════════════════════════════════════════════════
# G4: si algo no contesta o no existe, no se escribe nada (503 y 404).
# ═══════════════════════════════════════════════════════════════════════

def test_G4_noco_sin_contestar_es_503_y_no_escribe(api, noco, drive):
    noco.cae = True
    antes = _foto(api)
    r = _crear(101)
    assert r.status_code == 503
    assert _foto(api) == antes and drive["llamadas"] == []


def test_G4_ficha_inexistente_es_404_y_no_escribe(api, noco, drive):
    antes = _foto(api)
    r = _crear(999)
    assert r.status_code == 404
    assert _foto(api) == antes and drive["llamadas"] == []


def test_G4_si_el_responsable_no_resuelve_es_503_y_no_escribe(api, noco, drive, monkeypatch):
    monkeypatch.setattr(config, "NOMBRE_DEL_RESPONSABLE_DE_PROYECTO_DE_NATALIA", "Nadie de la casa")
    antes = _foto(api)
    r = _crear(101)
    assert r.status_code == 503
    assert _foto(api) == antes and drive["llamadas"] == []


# ═══════════════════════════════════════════════════════════════════════
# G5: el sistema no miente. Drive y el aviso pueden fallar, y la respuesta lo dice; el alta ya quedó escrita.
# ═══════════════════════════════════════════════════════════════════════

def test_G5_si_drive_falla_el_proyecto_queda_y_la_respuesta_dice_pendiente(api, noco, drive):
    drive["devuelve"] = {"resultado": "fallo", "causa": "red", "carpeta": None}
    r = _crear(101)
    assert r.status_code == 201, r.text
    assert r.json()["carpeta"] == "pendiente" and r.json()["creado"] is True
    assert len(_proyectos_de(api, 101)) == 1


def test_G5_si_drive_revienta_el_proyecto_queda_y_la_respuesta_dice_pendiente(api, noco, drive):
    drive["revienta"] = True
    r = _crear(101)
    assert r.status_code == 201, r.text
    assert r.json()["carpeta"] == "pendiente"
    assert len(_proyectos_de(api, 101)) == 1


def test_G5_si_el_aviso_falla_el_proyecto_queda_y_la_respuesta_dice_pendiente(api, noco, drive, monkeypatch):
    async def se_cae(chat, texto, origen="despertador"):
        raise RuntimeError("bandeja de mentira: no escribe")
    monkeypatch.setattr(db, "registrar_aviso", se_cae)
    r = _crear(101)
    assert r.status_code == 201, r.text
    assert r.json()["aviso"] == "pendiente" and r.json()["creado"] is True
    assert len(_proyectos_de(api, 101)) == 1


# ═══════════════════════════════════════════════════════════════════════
# G6: nombre repetido con otro proyecto activo: 409, no se crea, y Rosi tiene un aviso para resolverlo.
# ═══════════════════════════════════════════════════════════════════════

def test_G6_nombre_repetido_es_409_no_escribe_proyecto_y_deja_aviso(api, noco, drive, gente):
    api.con.execute("INSERT INTO proyectos (id, nombre, estado, area, creado_en, cliente_noco_id, cliente_nombre, "
                    "responsable_chat_id) VALUES (7, 'Cliente 101', 'activo', 'CDS', '2026-10-01T00:00:00+00:00', "
                    "102, 'Cliente 102', ?)", (config.CHAT_ID_DUENO,))
    api.con.commit()
    antes_proyectos = [tuple(f) for f in api.con.execute("SELECT * FROM proyectos ORDER BY id")]
    r = _crear(101)
    assert r.status_code == 409, r.text
    assert [tuple(f) for f in api.con.execute("SELECT * FROM proyectos ORDER BY id")] == antes_proyectos
    avisos = api.con.execute("SELECT chat_id FROM bandeja").fetchall()
    assert [a["chat_id"] for a in avisos] == [gente.rosi]
    assert drive["llamadas"] == []
