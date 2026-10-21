# -*- coding: utf-8 -*-
"""La puerta de Natalia para las facturas (pieza 9 del diseño «Facturas», 21-oct-2026).

El camino (Tiziano, 20-oct-2026): el cliente le pide la factura a Natalia; NATALIA LA HACE con las sesiones o el trabajo de
ese cliente; ROSI LA REVISA Y LA APRUEBA en Lucy (Telegram, sin el panel); Natalia la manda por WhatsApp o correo; Natalia
anota si salió. Lo que se vigila, por las RUTAS reales de `/api/code`, la puerta REAL de `db/db.py` (SQLite sobre las
migraciones reales), el despertador REAL (`revisar_envios_de_facturas`) y el manejador de botones REAL
(`acciones.botones.al_pulsar`):

  1. LA PUERTA: sin llave, con llave mala, con llave sin el permiso de ESA ruta, o con el permiso pero de otro `quien`:
     401 o 403, y la base no cambia (cada ruta sacada de `rutas_registradas`, no de una lista tecleada);
  2. NATALIA NO CREA CON UNA SESIÓN AJENA: la comprobación es la de la pantalla (la ficha del cliente en el registro), antes
     de escribir, y nada queda escrito si falla;
  3. SIN APROBACIÓN DE ROSI NO HAY PDF NI EMISIÓN: el borrador no toma número hasta que ella aprueba, y el PDF solo se baja con
     el envío aprobado;
  4. LA TARJETA VA AL CHAT DE QUIEN REVISA (sale de la configuración) y a nadie más, con el PDF y los botones;
  5. RECHAZAR NO EMITE;
  6. «ENVIADA» SOLO SI NATALIA LO ANOTÓ (o la casa, a mano): el estado que Natalia lee y el que la pantalla dice;
  7. NATALIA NO VE FACTURAS AJENAS: solo las que ella hizo, de la ficha de la ruta; ni las de la casa ni las de otra ficha;
  8. LA PANTALLA: quién la hizo, cómo van los envíos, «Marcar como mandada a mano».

NADA DICE «aprobada», «emitida» ni «enviada» SI NO PASÓ: cada respuesta se compara con lo que la base tiene.

FRONTERA, dicha una vez: SQLite, no Postgres (no hay `ANY`, ni bloqueo de fila: el «primero gana» se vigila por el `WHERE
estado` de cada `UPDATE`); la App, Noco y Telegram son dobles (la red de Telegram es `Bot.send_document` a nivel de clase;
su firma se compara con la de verdad); no se miró el aspecto del PDF ni de la pantalla. NINGÚN nombre, clave ni número de este
archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_facturas_natalia.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import io
import re
import types
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # primero: otros módulos de prueba cambian `httpx` al importarse

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudantes de la pantalla)
    NOMBRE, OTRA, OTRO_TRABAJO, SESION, SESION_B, SU_CANCELADA, TRABAJO, cliente, pantalla, registro)
import test_facturas_tablas as ft

import telegram
from pypdf import PdfReader

import acciones.botones as botones
import cerebro.despertador as despertador
import config
import db.db as db
import facturas_drive
import noco_lectura
import web.api_code as api_code
import web.app as panel

RAIZ = Path(__file__).resolve().parent.parent
K, K_SIN, K_SALA = "llave-natalia-de-prueba", "llave-sin-facturas", "llave-de-la-sala"
DUENO = config.CHAT_ID_DUENO
ROSI, BETO = DUENO + 5001, DUENO + 5002
HTTP = TestClient(panel.app)
PERMISOS = sorted({perm for _, ruta, perm in api_code.rutas_registradas(panel.app) if "/facturas" in ruta})


# ── El mundo ────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(pantalla, monkeypatch):  # noqa: F811
    """La base y la App de las pruebas de la pantalla, las llaves (Natalia con los permisos de las facturas; otra sin ellos;
    la sala con TODOS pero con otro `quien`) y la gente de la casa: Rosi revisa, el dueño y Beto también entran al panel."""
    assert PERMISOS and len(PERMISOS) == 6, PERMISOS
    monkeypatch.setattr(config, "CLAVES_API_CODE", {K: "natalia", K_SIN: "natalia_sin_facturas", K_SALA: "sala_mac"})
    monkeypatch.setattr(config, "PERMISOS_API_CODE", {
        "natalia": frozenset(PERMISOS), "natalia_sin_facturas": frozenset({"tareas:listar"}),
        "sala_mac": frozenset(PERMISOS)})
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {DUENO: "Dueno Inventado", ROSI: "Rosi Inventada", BETO: "Beto Inventado"})
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, ROSI, BETO))
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Rosi Inventada")

    async def persona(noco_id):                       # tres fichas existen en Noco (7 es la de siempre); la 99 no
        return {"id": noco_id, "nombre": NOMBRE if noco_id == 7 else "Otro Cliente"} if noco_id in (7, 8, 9) else None
    monkeypatch.setattr(noco_lectura, "persona", persona)
    return pantalla


def llamar(metodo, ruta, *, clave=K, json=None):
    api_code._pedidos_por_quien.clear()
    cabeceras = {"Authorization": f"Bearer {clave}"} if clave else {}
    return HTTP.request(metodo, ruta, json=json, headers=cabeceras)


def crear_api(refs=("11",), noco=7, clave=K, **kw):
    return llamar("POST", f"/api/code/clientes/{noco}/facturas", clave=clave, json={"refs": list(refs), **kw})


def pedir(fid, canal="whatsapp", noco=7, clave=K):
    return llamar("POST", f"/api/code/clientes/{noco}/facturas/{fid}/envios", clave=clave, json={"canal": canal})


def ver(fid, noco=7, clave=K):
    return llamar("GET", f"/api/code/clientes/{noco}/facturas/{fid}", clave=clave)


def lista(noco=7, clave=K):
    return llamar("GET", f"/api/code/clientes/{noco}/facturas", clave=clave)


def pdf(fid, eid, noco=7, clave=K):
    return llamar("GET", f"/api/code/clientes/{noco}/facturas/{fid}/envios/{eid}/pdf", clave=clave)


def anotar(fid, eid, salio, motivo=None, noco=7, clave=K):
    cuerpo = {"salio": salio}
    if motivo is not None:
        cuerpo["motivo"] = motivo
    return llamar("POST", f"/api/code/clientes/{noco}/facturas/{fid}/envios/{eid}/resultado", clave=clave, json=cuerpo)


def correr(coro):
    return asyncio.run(coro)


def envios(m, fid=None):
    return [e for e in tgf.filas(m, "factura_envios") if fid is None or e["factura_id"] == fid]


def factura_de(m, fid):
    return next(f for f in tgf.filas(m, "facturas") if f["id"] == fid)


def acciones_de_la_huella(m):
    return [(h["actor"], h["accion"]) for h in tgf.filas(m, "log_acciones")]


def de_natalia(m, noco=8, ref="ses-ajena"):
    """Una factura que hizo Natalia para OTRA ficha, directo por la puerta de `db`."""
    return correr(db.crear_borrador_de_factura_de_natalia(
        noco, para_nombre="Otro Cliente", renglones=[{"descripcion": "Algo", "monto": "100"}],
        ligas=[{"tipo": "sesion", "ref": ref}]))


# ── Telegram de mentira: la red a nivel de CLASE, como las pruebas de las consultas ───────────────────

class Red:
    def __init__(self):
        self.documentos: list[dict] = []
        self.modo = "bien"                      # bien | excepcion | sin_numero | sin_mensaje


@pytest.fixture
def tg(api, monkeypatch):  # noqa: F811
    red = Red()

    async def _documento(self, chat_id, document, caption=None, reply_markup=None, filename=None, **kw):
        if red.modo == "excepcion":
            raise RuntimeError("Telegram caído")
        red.documentos.append({"chat_id": chat_id, "document": document, "caption": caption,
                               "reply_markup": reply_markup, "filename": filename, **kw})
        if red.modo == "sin_mensaje":
            return None
        if red.modo == "sin_numero":
            return types.SimpleNamespace()
        return types.SimpleNamespace(message_id=9000 + len(red.documentos))

    monkeypatch.setattr(telegram.Bot, "send_document", _documento, raising=False)
    red.bot = telegram.Bot(token="123456:token-de-prueba-facturas")
    red.subidas = []
    red.avisos = []

    async def _aviso(chat, texto):
        red.avisos.append((chat, texto))
    monkeypatch.setattr(db, "registrar_aviso", _aviso)

    async def _subir(fid):
        red.subidas.append(fid)
        return "subida"
    monkeypatch.setattr(facturas_drive, "subir", _subir)
    api.red = red
    return red


def vuelta(red):
    return correr(despertador.revisar_envios_de_facturas(red.bot))


def botones_de(documento):
    return [b.callback_data for fila in documento["reply_markup"].inline_keyboard for b in fila]


class Toque:
    """Un `CallbackQuery` de mentira de una tarjeta que es un DOCUMENTO con pie."""

    def __init__(self, data, chat_id, pie="El pie de la tarjeta."):
        self.data = data
        self.avisos: list[str] = []
        self.editados: list[dict] = []
        self.message = types.SimpleNamespace(chat_id=chat_id, caption_html=pie, reply_text=self._reply)

    async def answer(self, *a, **k):
        pass

    async def edit_message_caption(self, caption=None, **kw):
        self.editados.append({"caption": caption, **kw})

    async def _reply(self, texto, **kw):
        self.avisos.append(texto)
        return types.SimpleNamespace(message_id=1)

    @property
    def pie_final(self):
        return self.editados[-1]["caption"] if self.editados else None


def tocar(data, chat_id):
    q = Toque(data, chat_id)
    correr(botones.al_pulsar(types.SimpleNamespace(callback_query=q), None))
    return q


def hasta_la_tarjeta(tg_, refs=("11", "13"), canal="whatsapp", **kw):
    """Natalia hace la factura y pide el envío; el despertador REAL manda la tarjeta. Devuelve `(fid, eid)`."""
    r = crear_api(refs, **kw)
    assert r.status_code == 200, r.text
    fid = r.json()["factura"]["factura_id"]
    r = pedir(fid, canal)
    assert r.status_code == 200, r.text
    eid = r.json()["envio"]["envio_id"]
    assert vuelta(tg_) == 1
    return fid, eid


def aprobada(tg_, **kw):
    """Todo el camino hasta que Rosi aprueba. Devuelve `(fid, eid)`."""
    fid, eid = hasta_la_tarjeta(tg_, **kw)
    q = tocar(f"fas:{eid}", ROSI)
    assert "Aprobada" in q.pie_final, q.pie_final
    return fid, eid


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. LA PUERTA
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def _rutas():
    return [(m, p, perm) for m, p, perm in api_code.rutas_registradas(panel.app) if "/facturas" in p]


def _llenar(ruta):
    return ruta.format(noco_id=7, factura_id=1, envio_id=1)


def _json_de(metodo, ruta):
    if metodo != "POST":
        return None
    if ruta.endswith("/resultado"):
        return {"salio": True}
    if ruta.endswith("/envios"):
        return {"canal": "whatsapp"}
    return {"refs": ["11"]}


def test_las_rutas_de_las_facturas_son_seis_y_cada_una_exige_su_propio_permiso():
    """Salen de lo que FastAPI registró. Seis rutas, seis permisos distintos (el censo de `tests/test_api_code.py` exige uno
    por ruta) y todas con el `quien` de Natalia (`solo_de`)."""
    rutas = _rutas()
    assert len(rutas) == 6 and len({perm for _, _, perm in rutas}) == 6, rutas
    assert sorted(PERMISOS) == sorted(perm for _, _, perm in rutas)
    assert {"facturas:crear", "facturas:pedir_envio", "facturas:leer", "facturas:listar", "facturas:pdf",
            "facturas:anotar_envio"} == set(PERMISOS)


@pytest.mark.parametrize("metodo,ruta,permiso", _rutas())
def test_sin_llave_o_con_llave_mala_es_401_y_con_llave_sin_permiso_o_de_otro_quien_es_403_y_no_se_escribe(
        api, metodo, ruta, permiso):
    antes = tgf.foto(api)
    url = _llenar(ruta)
    cuerpo = _json_de(metodo, ruta)
    assert llamar(metodo, url, clave=None, json=cuerpo).status_code == 401
    assert llamar(metodo, url, clave="llave-inventada", json=cuerpo).status_code == 401
    assert llamar(metodo, url, clave=K_SIN, json=cuerpo).status_code == 403                     # sin el permiso
    assert llamar(metodo, url, clave=K_SALA, json=cuerpo).status_code == 403                    # tiene el permiso, no es Natalia
    # y con TODOS los permisos MENOS el de esta ruta: 403
    config.PERMISOS_API_CODE["natalia"] = frozenset(set(PERMISOS) - {permiso})
    try:
        assert llamar(metodo, url, json=cuerpo).status_code == 403
    finally:
        config.PERMISOS_API_CODE["natalia"] = frozenset(PERMISOS)
    assert tgf.foto(api) == antes


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. NATALIA HACE LA FACTURA
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_natalia_crea_un_borrador_con_las_sesiones_del_cliente_y_la_respuesta_sale_de_la_base(api):
    r = crear_api(("11", "12", "13"))
    assert r.status_code == 200, r.text
    f = r.json()["factura"]
    assert (f["estado"], f["codigo"], f["para_nombre"], f["envios"]) == ("borrador", None, NOMBRE, [])
    assert r.json()["reusada"] is False
    # un renglón por sesión con el total que el registro tiene; el trabajo se liga como trabajo
    assert [(x["descripcion"], x["monto"]) for x in f["renglones"]] == [
        ("Grabación", "9000.00"), ("Mezcla", "6000.00"), ("Ensayo", "4500.00")]
    assert [(l["tipo"], l["ref"]) for l in f["ligas"]] == [("sesion", "11"), ("trabajo", "12"), ("sesion", "13")]
    assert (f["subtotal"], f["itbis"], f["total"]) == ("19500.00", "0.00", "19500.00")
    fila = factura_de(api, f["factura_id"])
    assert (fila["hecha_por"], fila["hecha_por_chat_id"], fila["estado"], fila["numero"], fila["cliente_noco_id"]) == (
        "natalia", None, "borrador", None, 7)
    assert fila["proyecto_id"] is None and fila["ncf"] is None and fila["cotizacion_id"] is None
    assert tgf.filas(api, "serie_facturas") == []                    # sin número: se numera al aprobarse
    assert acciones_de_la_huella(api) == [("natalia", "crear")]
    assert envios(api) == []


def test_con_itbis_con_renglones_propios_con_rnc_y_con_fecha(api):
    r = crear_api(("11",), itbis=True, rnc_cedula="123456789", fecha="2026-10-20",
                  renglones=[{"descripcion": "Grabación de voces", "detalle": "dos horas", "monto": "62500.00"}])
    assert r.status_code == 200, r.text
    f = r.json()["factura"]
    assert (f["subtotal"], f["itbis"], f["total"], f["fecha"]) == ("62500.00", "11250.00", "73750.00", "2026-10-20")
    assert f["renglones"] == [{"descripcion": "Grabación de voces", "detalle": "dos horas", "monto": "62500.00"}]
    fila = factura_de(api, f["factura_id"])
    assert (Decimal(str(fila["itbis_pct"])), fila["rnc_cedula"]) == (Decimal("18"), "123456789")


@pytest.mark.parametrize("refs,noco", [
    (("21",), 7), (("23",), 7), (("31",), 7), (("32",), 7), (("999",), 7), (("11", "21"), 7), (("21", "11"), 7),
    (("11",), 8)], ids=["sesion-de-otra-persona", "trabajo-de-otra-persona", "cancelada-de-otra-persona",
                        "su-propia-cancelada", "no-existe", "buena-y-ajena", "ajena-y-buena", "ficha-de-otro-cliente"])
def test_natalia_no_crea_con_una_sesion_que_no_es_del_cliente_y_no_queda_nada(api, registro, refs, noco):
    """HERMANO de la pieza 6: la misma comprobación de la pantalla (la ficha del cliente en el registro), antes de escribir."""
    registro.por_persona = {"8": [dict(OTRA)]}
    antes = tgf.foto(api)
    r = crear_api(refs, noco=noco)
    assert r.status_code in (404, 422), (r.status_code, r.text)
    assert tgf.foto(api) == antes


def test_a_la_app_se_le_pide_solo_la_ficha_del_cliente_de_la_ruta(api, registro):
    from test_facturas_sesiones_del_registro import pedidas_a_la_app
    registro.pedidos.clear()
    assert crear_api(("11",)).status_code == 200
    assert pedidas_a_la_app(registro) == [[("persona", "7")]]


def test_si_la_app_no_contesta_o_no_puede_ligar_no_se_guarda_nada(api, registro):
    antes = tgf.foto(api)
    registro.sesiones = {"puede_ligar": False, "motivo_sin_ligar": "sin_telefono", "sesiones": []}
    r = crear_api(("11",))
    assert r.status_code == 422 and "no se pudo comprobar" in r.text and tgf.foto(api) == antes
    registro.cerrar()                                     # la App deja de contestar
    r = crear_api(("11",))
    assert r.status_code == 503 and "no se guardó nada" in r.text and tgf.foto(api) == antes


def test_ficha_que_no_existe_o_noco_caido(api, monkeypatch):
    antes = tgf.foto(api)
    r = crear_api(("11",), noco=99)
    assert r.status_code == 404 and tgf.foto(api) == antes

    async def caido(noco_id):
        raise noco_lectura.NocoNoContesta("caído")
    monkeypatch.setattr(noco_lectura, "persona", caido)
    r = crear_api(("11",))
    assert r.status_code == 503 and "no se guardó nada" in r.text and tgf.foto(api) == antes


def test_una_sesion_que_ya_esta_en_otra_factura_viva_es_409(api):
    assert crear_api(("11",)).status_code == 200
    antes = tgf.foto(api)
    r = crear_api(("11", "13"))
    assert r.status_code == 409, r.text
    assert tgf.foto(api) == antes                                # ni la 13 quedó: nada a medias
    tgf.crear(liga={"tipo": "sesion", "ref": "13"}, cliente_noco_id=7)      # la casa la factura
    assert crear_api(("13",)).status_code == 409


def test_sin_total_en_el_registro_pide_los_renglones_y_con_ellos_se_crea(api, registro):
    registro.sesiones["sesiones"][0] = dict(SESION, total=None)
    antes = tgf.foto(api)
    r = crear_api(("11",))
    assert r.status_code == 422 and "s011" in r.text and "Manda los renglones" in r.text and tgf.foto(api) == antes
    assert crear_api(("11",), renglones=[{"descripcion": "Grabación", "monto": "9000"}]).status_code == 200


@pytest.mark.parametrize("cuerpo", [
    {"refs": []}, {"refs": ["11"] * 51}, {"refs": ["11"], "ncf": "B0100000001"}, {"refs": ["11"], "proyecto_id": 1},
    {"refs": ["11"], "hecha_por": "casa"}, {"refs": ["11"], "estado": "emitida"}, {"refs": ["11"], "itbis": "si"},
    {"refs": ["11"], "renglones": [{"descripcion": "a", "monto": 1500}]},
    {"refs": ["11"], "renglones": [{"descripcion": "a", "monto": "0"}]},
    {"refs": ["11"], "renglones": [{"descripcion": "a", "monto": "10.505"}]},
    {"refs": ["11"], "renglones": [{"descripcion": "a", "monto": "5", "extra": 1}]},
    {"refs": ["11"], "renglones": []}, {"refs": ["11"], "rnc_cedula": "123"}, {"refs": ["11"], "fecha": "ayer"},
    {"refs": ["11\u0000"]}, {"refs": [11]}, {"refs": ["11", "11"]}, {}])
def test_un_pedido_que_no_vale_no_escribe_y_no_deja_poner_ncf_ni_quien_la_hizo(api, cuerpo):
    antes = tgf.foto(api)
    r = llamar("POST", "/api/code/clientes/7/facturas", json=cuerpo)
    if cuerpo == {"refs": ["11", "11"]}:                 # repetida: se ve como una sola
        assert r.status_code == 200 and len(r.json()["factura"]["ligas"]) == 1
        return
    assert r.status_code == 422, (r.status_code, r.text)
    assert tgf.foto(api) == antes


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 3. EL ENVÍO, LA TARJETA Y LA APROBACIÓN
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_pedir_el_envio_deja_un_pendiente_sin_numero_sin_tarjeta_y_no_dice_aprobada(api):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    r = pedir(fid, "correo")
    assert r.status_code == 200, r.text
    e = r.json()["envio"]
    assert (e["canal"], e["estado"], e["aprobada"], e["pdf_listo"], e["motivo"], r.json()["reusada"]) == (
        "correo", "pendiente", False, False, None, False)
    fila = envios(api, fid)
    assert len(fila) == 1 and (fila[0]["estado"], fila[0]["tarjeta_msg_id"], fila[0]["aprobado_en"],
                                fila[0]["enviado_en"]) == ("pendiente", None, None, None)
    f = factura_de(api, fid)
    assert (f["estado"], f["numero"]) == ("borrador", None) and tgf.filas(api, "serie_facturas") == []
    assert acciones_de_la_huella(api) == [("natalia", "crear"), ("natalia", "pedir_envio")]


def test_pedir_dos_veces_lo_mismo_devuelve_el_vivo_y_otro_canal_es_otro_envio(api):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    primero = pedir(fid, "whatsapp").json()
    segundo = pedir(fid, "whatsapp").json()
    assert segundo["reusada"] is True and segundo["envio"]["envio_id"] == primero["envio"]["envio_id"]
    assert len(envios(api, fid)) == 1
    otro = pedir(fid, "correo").json()
    assert otro["reusada"] is False and otro["envio"]["envio_id"] != primero["envio"]["envio_id"]
    assert len(envios(api, fid)) == 2


@pytest.mark.parametrize("canal", ["mano", "sms", "", "WhatsApp"])
def test_el_canal_es_whatsapp_o_correo_y_nada_mas(api, canal):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    antes = tgf.foto(api)
    assert pedir(fid, canal).status_code == 422
    assert tgf.foto(api) == antes


def test_el_canal_tambien_se_comprueba_en_la_puerta_de_db_sin_pasar_por_la_ruta(api):
    """Segunda llave: la ruta ya frena el canal con su `Literal`, pero la puerta de `db` no confía en quien la llame."""
    f = de_natalia(api, 7, "r-canal")
    antes = tgf.foto(api)
    for canal in ("mano", "sms", None, 5):
        with pytest.raises(db.FacturaNoVale) as e:
            correr(db.pedir_envio_de_factura_de_natalia(7, f["id"], canal))
        assert e.value.clave == "canal"
    assert tgf.foto(api) == antes


def test_la_lista_de_envios_sin_tarjeta_solo_trae_los_pendientes_por_whatsapp_o_correo(api):
    f = de_natalia(api, 7, "r-sin-tarjeta")
    e1 = correr(db.pedir_envio_de_factura_de_natalia(7, f["id"], "whatsapp"))["envio"]["id"]
    e2 = correr(db.pedir_envio_de_factura_de_natalia(7, f["id"], "correo"))["envio"]["id"]
    assert [x["id"] for x in correr(db.envios_de_factura_sin_tarjeta())] == [e1, e2]
    api.con.execute("UPDATE factura_envios SET estado = 'no_se_pudo', motivo = 'la factura se anuló' WHERE id = ?", (e1,))
    api.con.execute("UPDATE factura_envios SET estado = 'rechazado', motivo = 'x' WHERE id = ?", (e2,))
    api.con.execute("INSERT INTO factura_envios (factura_id, canal) VALUES (?, 'mano')", (f["id"],))
    api.con.commit()
    assert correr(db.envios_de_factura_sin_tarjeta()) == []


def test_anular_una_emitida_con_un_envio_esperando_tarjeta_no_deja_salir_la_tarjeta(api, tg):
    fid, eid = aprobada(tg)
    pedir(fid, "correo")
    correr(db.anular_factura(fid, DUENO, "se hizo por error"))
    assert vuelta(tg) == 0 and len(tg.documentos) == 1               # solo la del primer envío; la del segundo no sale


def test_si_no_hay_a_quien_pedirle_la_aprobacion_es_503_y_no_se_pide_nada(api, monkeypatch):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    antes = tgf.foto(api)
    for nombre in ("Nadie Conocido", ""):
        monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", nombre)
        r = pedir(fid)
        assert r.status_code == 503 and "no se pidió nada" in r.text, nombre
    assert tgf.foto(api) == antes
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Rosi Inventada")
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, BETO))             # Rosi ya no entra al panel
    assert pedir(fid).status_code == 503


def test_sin_las_columnas_de_la_tarjeta_es_503_y_no_se_escribe_nada(api, monkeypatch):
    """Código nuevo con la migración sin aplicar: se dice y no queda un envío que nunca va a tener tarjeta."""
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    antes = tgf.foto(api)

    class _SinColumna(Exception):
        sqlstate = "42703"
    original = tgf._Cur.execute

    async def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO factura_envios"):
            raise _SinColumna("column does not exist")
        return await original(self, sql, params)
    monkeypatch.setattr(tgf._Cur, "execute", execute)
    r = pedir(fid)
    assert r.status_code == 503 and "migración" in r.text
    monkeypatch.setattr(tgf._Cur, "execute", original)
    assert tgf.foto(api) == antes


def test_una_anulada_no_se_manda_y_una_emitida_si(api, tg):
    fid, eid = aprobada(tg)
    assert pedir(fid, "correo").status_code == 200                       # emitida: otro canal
    correr(db.anular_factura(fid, DUENO, "se hizo por error"))
    assert [e["estado"] for e in envios(api, fid)] == ["no_se_pudo", "no_se_pudo"]       # los vivos se soltaron al anular
    antes = tgf.foto(api)
    r = pedir(fid, "whatsapp")
    assert r.status_code == 409 and tgf.foto(api) == antes


# ── La tarjeta ──────────────────────────────────────────────────────────────────────────────────────

def test_la_tarjeta_va_al_chat_de_quien_revisa_con_el_pdf_y_los_botones_y_a_nadie_mas(api, tg):
    fid, eid = hasta_la_tarjeta(tg, canal="whatsapp")
    assert len(tg.documentos) == 1
    d = tg.documentos[0]
    assert d["chat_id"] == ROSI and d["chat_id"] != DUENO != BETO               # a la revisora (de la configuración), a nadie más
    assert botones_de(d) == [f"fas:{eid}", f"fan:{eid}"]
    assert NOMBRE in d["caption"] and "WhatsApp" in d["caption"] and "RD$" in d["caption"] and "borrador" in d["caption"]
    assert d["filename"] == f"Factura-borrador-{fid}.pdf"
    lector = PdfReader(io.BytesIO(d["document"]))
    assert "BORRADOR" in "".join(p.extract_text() for p in lector.pages)       # lo que se aprueba es lo que se ve: el borrador
    e = envios(api, fid)[0]
    assert (e["tarjeta_msg_id"], e["estado"]) == (9001, "pendiente")
    assert tgf.filas(api, "factura_envios")[0]["visto_cambiado_en"] == factura_de(api, fid)["cambiado_en"]
    assert [c for c, _ in tg.avisos] == [ROSI] and "Natalia hizo una factura" in tg.avisos[0][1]     # y queda en su bandeja
    assert vuelta(tg) == 0 and len(tg.documentos) == 1                          # no se repite


def test_la_tarjeta_dice_el_itbis_y_el_total_de_la_base(api, tg):
    hasta_la_tarjeta(tg, refs=("11",), itbis=True)
    assert "total RD$10,620" in tg.documentos[0]["caption"] and "(con ITBIS 18%)" in tg.documentos[0]["caption"]


def test_si_cambia_quien_revisa_la_tarjeta_va_a_la_nueva_persona(api, tg, monkeypatch):
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Beto Inventado")
    hasta_la_tarjeta(tg)
    assert [d["chat_id"] for d in tg.documentos] == [BETO]


@pytest.mark.parametrize("modo", ["excepcion", "sin_mensaje", "sin_numero"])
def test_una_tarjeta_que_no_salio_no_se_anota_y_se_reintenta(api, tg, modo):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    pedir(fid)
    tg.modo = modo
    assert vuelta(tg) == 0
    assert envios(api, fid)[0]["tarjeta_msg_id"] is None and envios(api, fid)[0]["estado"] == "pendiente"
    tg.modo = "bien"
    assert vuelta(tg) == 1 and envios(api, fid)[0]["tarjeta_msg_id"] is not None


def test_sin_a_quien_mandarla_el_despertador_no_manda_nada_y_sin_migrar_tampoco(api, tg, monkeypatch):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    pedir(fid)
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Nadie Conocido")
    assert vuelta(tg) == 0 and tg.documentos == []
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Rosi Inventada")

    async def sin_migrar():
        raise db.EnviosSinMigrar("faltan")
    monkeypatch.setattr(db, "envios_de_factura_sin_tarjeta", sin_migrar)
    assert vuelta(tg) == 0 and tg.documentos == []


def test_lo_que_el_despertador_le_pasa_a_telegram_es_lo_que_la_firma_de_verdad_acepta():
    """El doble de la red afirma cómo se porta la biblioteca: se compara con `telegram.Bot` y con `ExtBot` (el que arma `main.py`)."""
    from telegram.ext import ExtBot
    for clase in (telegram.Bot, ExtBot):
        firma = inspect.signature(clase.send_document)
        firma.bind(object(), chat_id=1, document=b"%PDF", filename="a.pdf", caption="c", reply_markup=None)
    assert "caption" in inspect.signature(telegram.CallbackQuery.edit_message_caption).parameters
    assert {"caption", "parse_mode", "reply_markup"} <= set(inspect.signature(telegram.CallbackQuery.edit_message_caption).parameters)


# ── Aprobar ─────────────────────────────────────────────────────────────────────────────────────────

def test_rosi_aprueba_y_el_borrador_se_emite_con_su_numero_y_el_envio_queda_aprobado(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    assert factura_de(api, fid)["estado"] == "borrador"
    q = tocar(f"fas:{eid}", ROSI)
    f = factura_de(api, fid)
    assert (f["estado"], f["anio"], f["numero"]) == ("emitida", 2026, 1)
    e = envios(api, fid)[0]
    assert (e["estado"], e["aprobado_por_chat_id"], e["canal"]) == ("aprobado", ROSI, "whatsapp") and e["aprobado_en"]
    assert e["enviado_en"] is None                                      # aprobar NO es enviar
    assert "Aprobada" in q.pie_final and "FAC-2026-001" in q.pie_final and "Se emitió" in q.pie_final
    assert q.editados[-1]["reply_markup"] is None                       # los botones se van
    assert "ya puede mandarla" in q.pie_final and "ya la mandó" not in q.pie_final.lower()
    assert acciones_de_la_huella(api) == [("natalia", "crear"), ("natalia", "pedir_envio"), ("panel", "emitir"),
                                          ("panel", "aprobar_envio")]
    assert tg.subidas == [fid]                                          # emitida: sube a Drive, como en la pantalla
    leida = ver(fid).json()["factura"]
    assert (leida["estado"], leida["codigo"]) == ("emitida", "FAC-2026-001")
    assert [(x["estado"], x["aprobada"], x["pdf_listo"]) for x in leida["envios"]] == [("aprobado", True, True)]


def test_aprobar_dos_veces_no_vuelve_a_numerar(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    tocar(f"fas:{eid}", ROSI)
    antes = tgf.foto(api)
    q = tocar(f"fas:{eid}", ROSI)
    assert "Ya estaba contestada" in q.pie_final
    assert tgf.foto(api) == antes and [r["ultimo"] for r in tgf.filas(api, "serie_facturas")] == [1]
    tocar(f"fan:{eid}", ROSI)                                           # y rechazar lo ya aprobado tampoco cambia nada
    assert tgf.foto(api) == antes


def test_solo_quien_revisa_aprueba_otra_persona_de_la_casa_no_y_alguien_de_fuera_tampoco(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    antes = tgf.foto(api)
    q = tocar(f"fas:{eid}", BETO)                                       # entra al panel, pero no es quien revisa
    assert q.avisos == ["Esto le toca a Rosi Inventada."] and q.editados == []
    q = tocar(f"fas:{eid}", DUENO)                                      # ni siquiera el dueño
    assert q.avisos == ["Esto le toca a Rosi Inventada."]
    q = tocar(f"fas:{eid}", tgf.EXTRANO)                                # de fuera: la puerta de `al_pulsar` lo corta
    assert q.avisos == [] and q.editados == []
    assert tgf.foto(api) == antes
    assert factura_de(api, fid)["estado"] == "borrador"


def test_rechazar_no_emite_el_envio_queda_rechazado_con_motivo_y_natalia_lo_lee(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    q = tocar(f"fan:{eid}", ROSI)
    f = factura_de(api, fid)
    assert (f["estado"], f["numero"], f["emitida_en"]) == ("borrador", None, None)          # NO se emitió
    assert tgf.filas(api, "serie_facturas") == []
    e = envios(api, fid)[0]
    assert (e["estado"], e["motivo"], e["aprobado_en"]) == ("rechazado", "Rechazada por Rosi Inventada", None)
    assert "Rechazada" in q.pie_final and "No se manda nada" in q.pie_final
    assert tg.subidas == []
    leida = ver(fid).json()["factura"]["envios"][0]
    assert (leida["estado"], leida["aprobada"], leida["pdf_listo"], leida["motivo"]) == (
        "rechazado", False, False, "Rechazada por Rosi Inventada")
    assert pdf(fid, eid).status_code == 409                              # y sin aprobación no hay PDF
    assert acciones_de_la_huella(api)[-1] == ("panel", "rechazar_envio")


def test_un_borrador_rechazado_se_puede_descartar_y_sus_sesiones_quedan_libres(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    tocar(f"fan:{eid}", ROSI)
    correr(db.descartar_borrador_de_factura(fid, DUENO))
    assert tgf.filas(api, "facturas") == [] and envios(api) == []
    assert crear_api(("11", "13")).status_code == 200                    # las mismas sesiones, otra vez
    h = [h for h in tgf.filas(api, "log_acciones") if h["accion"] == "descartar"][0]
    assert '"estado": "rechazado"' in h["antes"]                         # lo borrado queda en la huella


def test_un_borrador_con_un_envio_esperando_no_se_descarta(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    antes = tgf.foto(api)
    with pytest.raises(db.FacturaNoVale) as e:
        correr(db.descartar_borrador_de_factura(fid, DUENO))
    assert e.value.clave == "tiene_envios" and tgf.foto(api) == antes


def test_tocar_antes_de_que_exista_la_tarjeta_no_escribe_nada(api, tg):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    eid = pedir(fid).json()["envio"]["envio_id"]
    antes = tgf.foto(api)
    q = tocar(f"fas:{eid}", ROSI)
    assert "todavía se está preparando" in q.avisos[0] and tgf.foto(api) == antes
    q = tocar("fas:99999", ROSI)
    assert "ya no existe" in q.avisos[0] and tgf.foto(api) == antes


def test_si_la_factura_cambio_despues_de_la_tarjeta_no_se_emite_otra_distinta(api, tg):
    """La aprobación emite con el candado de lo que Rosi VIO: una corrección posterior la vuelve «no se pudo»."""
    fid, eid = hasta_la_tarjeta(tg)
    f = correr(db.factura_por_id(fid))
    correr(db.corregir_borrador_de_factura(fid, DUENO, f["cambiado_en"], {"para_nombre": "Nombre Cambiado"}))
    q = tocar(f"fas:{eid}", ROSI)
    assert factura_de(api, fid)["estado"] == "borrador" and factura_de(api, fid)["numero"] is None
    e = envios(api, fid)[0]
    assert e["estado"] == "no_se_pudo" and "cambió" in e["motivo"] and e["aprobado_en"] is None
    assert "No se pudo aprobar" in q.pie_final and "otra vez" in q.pie_final
    assert tgf.filas(api, "serie_facturas") == []
    leida = ver(fid).json()["factura"]["envios"][0]
    assert (leida["estado"], leida["aprobada"]) == ("no_se_pudo", False)


def test_una_emitida_se_aprueba_sin_numerar_de_nuevo(api, tg):
    fid, eid = aprobada(tg)                                              # FAC-2026-001
    eid2 = pedir(fid, "correo").json()["envio"]["envio_id"]
    assert vuelta(tg) == 1
    assert botones_de(tg.documentos[-1]) == [f"fas:{eid2}", f"fan:{eid2}"]
    assert "Ya está emitida como FAC-2026-001" in tg.documentos[-1]["caption"]
    q = tocar(f"fas:{eid2}", ROSI)
    assert "Ya estaba emitida" in q.pie_final and "correo" in q.pie_final
    assert [r["ultimo"] for r in tgf.filas(api, "serie_facturas")] == [1]
    assert tg.subidas == [fid]                                           # no vuelve a subir: no se emitió ahora


def test_si_la_factura_se_anulo_antes_de_aprobar_no_se_aprueba(api, tg):
    """Defensa: aunque un envío quedara pendiente sobre una anulada (anular los suelta), aprobar no lo aprueba."""
    fid, eid = aprobada(tg)
    eid2 = pedir(fid, "correo").json()["envio"]["envio_id"]
    vuelta(tg)
    api.con.execute("UPDATE factura_envios SET estado = 'pendiente', motivo = NULL WHERE id = ?", (eid2,))
    api.con.execute("UPDATE facturas SET estado = 'anulada', anulada_en = '2026-10-21', motivo_anulacion = 'x', "
                    "anulada_por_chat_id = 1 WHERE id = ?", (fid,))
    api.con.commit()
    q = tocar(f"fas:{eid2}", ROSI)
    e = next(x for x in envios(api, fid) if x["id"] == eid2)
    assert e["estado"] == "no_se_pudo" and "anuló" in e["motivo"] and "No se pudo aprobar" in q.pie_final


def test_si_drive_falla_al_emitir_con_la_aprobacion_la_tarjeta_igual_se_cierra(api, tg, monkeypatch):
    async def cae(fid):
        raise RuntimeError("Drive caído")
    monkeypatch.setattr(facturas_drive, "subir", cae)
    fid, eid = hasta_la_tarjeta(tg)
    q = tocar(f"fas:{eid}", ROSI)
    assert factura_de(api, fid)["estado"] == "emitida" and "Aprobada" in q.pie_final


def test_la_base_que_falla_a_la_mitad_no_deja_ni_el_numero_ni_el_envio_aprobado(api, tg, monkeypatch):
    """Todo o nada: si no se puede dejar la huella del envío, ni el número ni la aprobación quedan."""
    fid, eid = hasta_la_tarjeta(tg)
    original = db._huella_de_factura

    async def falla(conn, accion, *a, **k):
        if accion == "aprobar_envio":
            raise RuntimeError("base caída")
        return await original(conn, accion, *a, **k)
    monkeypatch.setattr(db, "_huella_de_factura", falla)
    q = tocar(f"fas:{eid}", ROSI)
    assert "No pude guardar tu respuesta" in q.avisos[0]
    f = factura_de(api, fid)
    assert (f["estado"], f["numero"]) == ("borrador", None)
    assert envios(api, fid)[0]["estado"] == "pendiente" and tgf.filas(api, "serie_facturas") == []


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 4. EL PDF
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_el_pdf_se_baja_solo_con_el_envio_aprobado_y_es_el_de_la_factura_emitida(api, tg):
    fid = crear_api(("11", "13")).json()["factura"]["factura_id"]
    eid = pedir(fid).json()["envio"]["envio_id"]
    antes = tgf.foto(api)
    r = pdf(fid, eid)                                                    # pendiente: todavía no
    assert r.status_code == 409 and "pendiente" in r.text and not r.content.startswith(b"%PDF")
    assert tgf.foto(api) == antes
    vuelta(tg)
    tocar(f"fas:{eid}", ROSI)
    r = pdf(fid, eid)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert 'filename="FAC-2026-001.pdf"' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"
    texto = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "FAC-2026-001" in texto and "BORRADOR" not in texto and "13,500" in texto


def test_el_pdf_no_se_baja_de_otro_envio_de_un_borrador_ni_de_lo_que_no_es_de_natalia(api, tg):
    fid, eid = aprobada(tg, refs=("11",))
    otra = crear_api(("13",)).json()["factura"]["factura_id"]
    otro_envio = pedir(otra).json()["envio"]["envio_id"]
    assert pdf(otra, eid).status_code == 404                # el envío es de otra factura
    assert pdf(otra, otro_envio).status_code == 409         # pendiente
    assert pdf(fid, 99999).status_code == 404
    # un envío «aprobado» sobre un BORRADOR (no pasa por esta puerta: se arma a mano) tampoco baja un borrador
    api.con.execute("UPDATE factura_envios SET estado = 'aprobado', aprobado_por_chat_id = 1, aprobado_en = '2026-10-21' "
                    "WHERE id = ?", (otro_envio,))
    api.con.commit()
    r = pdf(otra, otro_envio)
    assert r.status_code == 409 and "borrador" in r.text and not r.content.startswith(b"%PDF")
    # una factura de la casa con un envío aprobado: para Natalia no existe
    casa = tgf.emitir(tgf.crear(liga={"tipo": "sesion", "ref": "casa-1"}, cliente_noco_id=7))
    api.con.execute("INSERT INTO factura_envios (factura_id, canal, estado, aprobado_por_chat_id, aprobado_en) "
                    "VALUES (?, 'whatsapp', 'aprobado', 1, '2026-10-21')", (casa["id"],))
    api.con.commit()
    assert pdf(casa["id"], envios(api, casa["id"])[0]["id"]).status_code == 404


def test_el_pdf_se_acaba_cuando_natalia_anota_como_termino_o_la_factura_se_anula(api, tg):
    fid, eid = aprobada(tg, refs=("11",))
    assert pdf(fid, eid).status_code == 200 and pdf(fid, eid).status_code == 200      # puede reintentar mientras no anote
    assert anotar(fid, eid, True).status_code == 200
    assert pdf(fid, eid).status_code == 409                                           # la aprobación se gastó
    fid2, eid2 = aprobada(tg, refs=("13",))
    correr(db.anular_factura(fid2, DUENO, "se hizo por error"))
    assert pdf(fid2, eid2).status_code == 409


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 5. «ENVIADA» SOLO SI NATALIA LO ANOTÓ
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_nada_dice_enviado_hasta_que_natalia_lo_anota(api, tg):
    fid, eid = aprobada(tg)
    assert [e["estado"] for e in envios(api, fid)] == ["aprobado"] and envios(api, fid)[0]["enviado_en"] is None
    assert [e["estado"] for e in ver(fid).json()["factura"]["envios"]] == ["aprobado"]
    assert "enviado" not in lista().text.lower().replace("pdf_listo", "")
    html = cliente().get(f"/facturas/{fid}").text
    assert "Natalia todavía no anotó que salió" in html and "Enviada" not in html and "Mandada a mano" not in html
    r = anotar(fid, eid, True)
    assert r.status_code == 200 and r.json()["envio"]["estado"] == "enviado"
    e = envios(api, fid)[0]
    assert e["estado"] == "enviado" and e["enviado_en"] and e["aprobado_en"]
    assert acciones_de_la_huella(api)[-1] == ("natalia", "anotar_envio")
    html = cliente().get(f"/facturas/{fid}").text
    assert "Natalia anotó que la mandó por WhatsApp el" in html


def test_natalia_anota_que_no_salio_con_su_motivo_y_sin_motivo_no_se_anota(api, tg):
    fid, eid = aprobada(tg)
    for cuerpo in ({"salio": False}, {"salio": False, "motivo": "   "}, {"salio": False, "motivo": "x" * 501}):
        antes = tgf.foto(api)
        r = llamar("POST", f"/api/code/clientes/7/facturas/{fid}/envios/{eid}/resultado", json=cuerpo)
        assert r.status_code == 422 and tgf.foto(api) == antes, cuerpo
    r = anotar(fid, eid, False, "El número del cliente no tiene WhatsApp")
    assert r.status_code == 200 and r.json()["envio"]["estado"] == "no_se_pudo"
    e = envios(api, fid)[0]
    assert (e["estado"], e["motivo"], e["enviado_en"]) == ("no_se_pudo", "El número del cliente no tiene WhatsApp", None)
    assert "No salió por WhatsApp: El número del cliente no tiene WhatsApp" in cliente().get(f"/facturas/{fid}").text


def test_solo_se_anota_un_envio_aprobado_y_anotar_lo_mismo_dos_veces_es_lo_mismo(api, tg):
    fid = crear_api(("11",)).json()["factura"]["factura_id"]
    eid = pedir(fid).json()["envio"]["envio_id"]
    antes = tgf.foto(api)
    assert anotar(fid, eid, True).status_code == 409                     # pendiente: no
    vuelta(tg)
    tocar(f"fan:{eid}", ROSI)
    despues_de_rechazar = tgf.foto(api)
    assert anotar(fid, eid, True).status_code == 409 and tgf.foto(api) == despues_de_rechazar     # rechazado: no
    fid2, eid2 = aprobada(tg, refs=("13",))
    assert anotar(fid2, eid2, True).status_code == 200
    quieto = tgf.foto(api)
    assert anotar(fid2, eid2, True).status_code == 200 and tgf.foto(api) == quieto      # igual: no cambia nada
    assert anotar(fid2, eid2, False, "ahora no").status_code == 409 and tgf.foto(api) == quieto    # distinto: no se pisa


def test_la_unica_forma_de_que_un_envio_figure_enviado_es_anotarlo(api, tg):
    """Por todos los caminos que existen — pedir, tarjeta, aprobar, rechazar, reintentar la tarjeta, el PDF, leer — ningún
    envío queda `enviado` sin que Natalia (o la casa, a mano) lo anote."""
    fid, eid = hasta_la_tarjeta(tg)
    for paso in (lambda: pedir(fid), lambda: vuelta(tg), lambda: ver(fid), lambda: lista(), lambda: pdf(fid, eid),
                 lambda: tocar(f"fas:{eid}", BETO), lambda: tocar(f"fas:{eid}", ROSI), lambda: pdf(fid, eid),
                 lambda: pedir(fid), lambda: tocar(f"fas:{eid}", ROSI), lambda: ver(fid)):
        paso()
        assert [e["estado"] for e in envios(api)] != ["enviado"]
        assert all(e["estado"] != "enviado" and e["enviado_en"] is None for e in envios(api))


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 6. NATALIA NO VE FACTURAS AJENAS
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_natalia_solo_ve_las_que_ella_hizo_de_esa_ficha(api, tg):
    suya = crear_api(("11",)).json()["factura"]["factura_id"]
    de_la_casa = tgf.crear(liga={"tipo": "sesion", "ref": "casa-1"}, cliente_noco_id=7)["id"]
    de_otra_ficha = de_natalia(api, 8, "ses-8")["id"]
    assert [f["factura_id"] for f in lista(7).json()["facturas"]] == [suya]
    assert [f["factura_id"] for f in lista(8).json()["facturas"]] == [de_otra_ficha]
    assert lista(9).json()["facturas"] == []
    assert ver(suya).status_code == 200
    for fid, noco in ((de_la_casa, 7), (de_otra_ficha, 7), (suya, 8), (99999, 7)):
        r = ver(fid, noco)
        assert r.status_code == 404 and r.json() == {"detail": "esa factura no está"}, (fid, noco)


def test_ni_pedir_ni_bajar_ni_anotar_sirven_con_una_factura_ajena(api, tg):
    """HERMANOS: las cinco rutas de una factura reciben la de la casa, la de otra ficha y la propia con la ficha equivocada."""
    ajenas = [(tgf.crear(liga={"tipo": "sesion", "ref": "casa-1"}, cliente_noco_id=7)["id"], 7),
              (de_natalia(api, 8, "ses-8")["id"], 7), (crear_api(("11",)).json()["factura"]["factura_id"], 8)]
    for fid, noco in ajenas:
        api.con.execute("INSERT INTO factura_envios (factura_id, canal, estado, aprobado_por_chat_id, aprobado_en) "
                        "VALUES (?, 'whatsapp', 'aprobado', 1, '2026-10-21')", (fid,))
    api.con.commit()
    antes = tgf.foto(api)
    for fid, noco in ajenas:
        eid = envios(api, fid)[0]["id"]
        assert pedir(fid, noco=noco).status_code == 404
        assert pdf(fid, eid, noco=noco).status_code == 404
        assert anotar(fid, eid, True, noco=noco).status_code == 404
        assert ver(fid, noco).status_code == 404
    assert tgf.foto(api) == antes


def test_cada_ruta_de_las_facturas_con_ficha_esta_cubierta_arriba():
    """El censo de las rutas sale de lo registrado: si aparece una ruta más con `{noco_id}` y `/facturas`, esto se rompe hasta que
    alguien le escriba su «otra ficha no la ve»."""
    assert sorted((m, p) for m, p, _ in _rutas()) == [
        ("GET", "/api/code/clientes/{noco_id}/facturas"),
        ("GET", "/api/code/clientes/{noco_id}/facturas/{factura_id}"),
        ("GET", "/api/code/clientes/{noco_id}/facturas/{factura_id}/envios/{envio_id}/pdf"),
        ("POST", "/api/code/clientes/{noco_id}/facturas"),
        ("POST", "/api/code/clientes/{noco_id}/facturas/{factura_id}/envios"),
        ("POST", "/api/code/clientes/{noco_id}/facturas/{factura_id}/envios/{envio_id}/resultado")]


def _funciones_de_db_de_natalia():
    """Las funciones públicas de `db` que reciben la ficha (`noco_id`): sacadas del módulo."""
    return {n: f for n, f in vars(db).items()
            if inspect.iscoroutinefunction(f) and not n.startswith("_") and f.__module__ == db.__name__
            and "noco_id" in inspect.signature(f).parameters and ("factura" in n or "natalia" in n)
            and "consulta" not in n}


def test_toda_funcion_de_db_que_recibe_la_ficha_de_una_factura_la_filtra_en_su_sql():
    fns = _funciones_de_db_de_natalia()
    assert sorted(fns) == sorted([
        "anotar_envio_de_natalia", "crear_borrador_de_factura_de_natalia", "factura_y_envio_para_el_pdf_de_natalia",
        "facturas_de_natalia", "pedir_envio_de_factura_de_natalia"])
    for nombre, f in fns.items():
        if nombre == "crear_borrador_de_factura_de_natalia":
            assert 'hecha_por="natalia"' in inspect.getsource(inspect.unwrap(f)).replace(" ", "")       # nace de ella
            continue
        fuente = inspect.getsource(inspect.unwrap(f))
        assert ("_factura_de_natalia(cur" in fuente or "SENTENCIA_DE_LAS_FACTURAS_DE_NATALIA" in fuente
                or "SENTENCIA_DE_LA_FACTURA_DE_NATALIA" in fuente), nombre
    for sql in (db.SENTENCIA_DE_LA_FACTURA_DE_NATALIA, db.SENTENCIA_DE_LAS_FACTURAS_DE_NATALIA):
        assert "cliente_noco_id = %s" in sql and "hecha_por = 'natalia'" in sql


def test_ninguna_respuesta_deja_salir_un_numero_de_chat_ni_datos_internos(api, tg):
    fid, eid = aprobada(tg)
    anotar(fid, eid, False, "No salió")
    cuerpos = [lista().text, ver(fid).text, crear_api(("12",)).text, pedir(fid, "correo").text]
    for c in cuerpos:
        for chat in (DUENO, ROSI, BETO):
            assert str(chat) not in c
        for prohibido in ("chat", "ncf", "drive", "hecha_por", "aprobado_por", "tarjeta"):
            assert prohibido not in c.lower(), prohibido


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 7. LA PANTALLA
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_la_pagina_dice_quien_hizo_la_factura(api, tg):
    suya = crear_api(("11",)).json()["factura"]["factura_id"]
    de_la_casa = tgf.crear(liga={"tipo": "sesion", "ref": "casa-1"}, cliente_noco_id=7, chat=ROSI)["id"]
    sin_nombre = tgf.crear(liga={"tipo": "sesion", "ref": "casa-2"}, cliente_noco_id=7, chat=BETO)["id"]
    config.NOMBRES_POR_CHAT.pop(BETO)
    c = cliente()
    assert "La hizo Natalia." in c.get(f"/facturas/{suya}").text
    assert "La hizo Rosi Inventada." in c.get(f"/facturas/{de_la_casa}").text
    html = c.get(f"/facturas/{sin_nombre}").text
    assert "La hizo Alguien de la casa." in html and str(BETO) not in html


def test_la_pagina_dice_cada_estado_del_envio_con_lo_que_la_base_tiene(api, tg):
    fid, eid = hasta_la_tarjeta(tg)
    c = cliente()
    html = c.get(f"/facturas/{fid}").text
    assert 'data-envio="pendiente"' in html and "esperando que Rosi Inventada la revise en Telegram" in html
    assert "Marcar como mandada a mano" not in html                      # un borrador no se marca mandada
    tocar(f"fas:{eid}", ROSI)
    html = c.get(f"/facturas/{fid}").text
    assert 'data-envio="aprobado"' in html and "Natalia todavía no anotó que salió" in html
    assert "Marcar como mandada a mano" in html
    anotar(fid, eid, True)
    assert 'data-envio="enviado"' in c.get(f"/facturas/{fid}").text
    otra = crear_api(("12",)).json()["factura"]["factura_id"]
    e2 = pedir(otra).json()["envio"]["envio_id"]
    vuelta(tg)
    tocar(f"fan:{e2}", ROSI)
    html = c.get(f"/facturas/{otra}").text
    assert 'data-envio="rechazado"' in html and "Rechazada: Rechazada por Rosi Inventada. No se mandó." in html
    assert "Todavía no se ha mandado ni se ha pedido mandarla" not in html


def test_una_factura_sin_envios_lo_dice_y_si_no_se_pueden_leer_no_inventa_que_no_hay(api, monkeypatch):
    f = tgf.emitir(tgf.crear())
    html = cliente().get(f"/facturas/{f['id']}").text
    assert "Todavía no se ha mandado ni se ha pedido mandarla" in html

    async def cae(fid):
        raise RuntimeError("base caída")
    monkeypatch.setattr(db, "envios_de_factura", cae)
    r = cliente().get(f"/facturas/{f['id']}")
    assert r.status_code == 200 and "No se pudo leer cómo van los envíos" in r.text
    assert "Todavía no se ha mandado ni se ha pedido mandarla" not in r.text


def test_marcar_como_mandada_a_mano_crea_un_envio_a_mano_ya_enviado_sin_pedir_aprobacion(api):
    e = tgf.emitir(tgf.crear())
    r = cliente().post(f"/facturas/{e['id']}/mandada-a-mano", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/facturas/{e['id']}"
    fila = envios(api, e["id"])
    assert len(fila) == 1 and (fila[0]["canal"], fila[0]["estado"], fila[0]["aprobado_en"]) == ("mano", "enviado", None)
    assert fila[0]["enviado_en"]
    assert acciones_de_la_huella(api)[-1] == ("panel", "mandada_a_mano")
    html = cliente().get(f"/facturas/{e['id']}").text
    assert 'data-envio="enviado"' in html and "Mandada a mano" in html
    assert lista().json()["facturas"] == []                              # es de la casa: para Natalia no existe


def test_marcar_a_mano_solo_vale_para_una_emitida_y_solo_la_casa_lo_hace(api):
    borrador = tgf.crear()
    anulada = correr(db.anular_factura(tgf.emitir(tgf.crear(liga={"tipo": "sesion", "ref": "z"}))["id"], tgf.CASA, "error"))
    antes = tgf.foto(api)
    for fid in (borrador["id"], anulada["id"]):
        r = cliente().post(f"/facturas/{fid}/mandada-a-mano", follow_redirects=False)
        assert r.status_code == 400, r.status_code
    e = tgf.emitir(tgf.crear(liga={"tipo": "sesion", "ref": "y"}))
    antes = tgf.foto(api)
    for sesion in ("ver", None, "falsa", "ajena"):
        assert cliente(sesion).post(f"/facturas/{e['id']}/mandada-a-mano", follow_redirects=False).status_code == 401, sesion
    assert tgf.foto(api) == antes
    assert cliente().post("/facturas/99999/mandada-a-mano", follow_redirects=False).status_code in (400, 404)


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# 8. LA CONFIGURACIÓN, LA MIGRACIÓN Y LAS HERRAMIENTAS
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_quien_revisa_sale_del_nombre_y_de_quien_entra_al_panel_y_nunca_de_un_chat_tecleado(monkeypatch):
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {DUENO: "Dueno Inventado", ROSI: "Rosi Inventada", BETO: "Beto Inventado"})
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, ROSI))
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Rosi Inventada")
    assert config.chat_de_quien_revisa_las_facturas() == ROSI
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "Beto Inventado")
    assert config.chat_de_quien_revisa_las_facturas() is None             # tiene nombre, pero no entra al panel
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", config.NOMBRE_CODE)
    assert config.chat_de_quien_revisa_las_facturas() is None             # Code no tiene Telegram
    monkeypatch.setattr(config, "NOMBRE_DE_QUIEN_REVISA_LAS_FACTURAS", "")
    assert config.chat_de_quien_revisa_las_facturas() is None
    fuente = (RAIZ / "config.py").read_text(encoding="utf-8")
    cuerpo = fuente[fuente.index("def chat_de_quien_revisa_las_facturas"):fuente.index("def chat_escrito")]
    assert not re.search(r"\d{5,}", cuerpo)                               # ningún chat tecleado


def test_la_migracion_agrega_dos_columnas_nulas_y_el_esquema_dice_lo_mismo():
    texto = ft._texto_migracion_de_natalia()
    cols = re.findall(r"ALTER\s+TABLE\s+factura_envios\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)\s*;", texto, re.I)
    assert cols == [("tarjeta_msg_id", "BIGINT"), ("visto_cambiado_en", "TIMESTAMPTZ")]
    esquema = (RAIZ / "db" / "schema.sql").read_text(encoding="utf-8")
    bloque = esquema[esquema.index("CREATE TABLE factura_envios"):esquema.index("CREATE INDEX idx_factura_envios_factura")]
    assert re.search(r"tarjeta_msg_id\s+BIGINT,", bloque) and re.search(r"visto_cambiado_en\s+TIMESTAMPTZ,", bloque)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE)\b", re.sub(r"--.*", "", texto), re.I)


def test_la_migracion_corre_sobre_las_anteriores_y_una_segunda_vez_no_hace_nada(mundo):  # noqa: F811
    antes = [c[1] for c in mundo.con.execute("PRAGMA table_info(factura_envios)")]
    assert antes[-2:] == ["tarjeta_msg_id", "visto_cambiado_en"]
    f = tgf.crear()
    mundo.con.execute("INSERT INTO factura_envios (factura_id, canal) VALUES (?, 'mano')", (f["id"],))
    e = mundo.con.execute("SELECT tarjeta_msg_id, visto_cambiado_en FROM factura_envios").fetchone()
    assert e == (None, None)                                               # lo de hoy queda sin tarjeta


def test_el_guion_de_ensayo_saca_el_commit_cierra_con_rollback_y_solo_lee_con_cuenta(capsys):
    from tools import ensayo_facturas_envios as g
    assert g.main(["--cuenta"]) == 0
    cuenta = capsys.readouterr().out
    assert cuenta.startswith("BEGIN READ ONLY;") and cuenta.rstrip().endswith("ROLLBACK;")
    assert not re.search(r"\b(ALTER|INSERT|UPDATE|DELETE|CREATE|DROP)\b", cuenta)
    assert g.main(["--ensayo"]) == 0
    ensayo = capsys.readouterr().out
    assert "ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS tarjeta_msg_id BIGINT" in ensayo
    sin_comentarios = re.sub(r"--[^\n]*", "", ensayo)
    assert "COMMIT" not in sin_comentarios and ensayo.count("ROLLBACK;") >= 2
    assert "timestamp with time zone" in ensayo and "bigint" in ensayo
    assert g.main(["--otra-cosa"]) == 2


def test_el_humo_lee_las_columnas_de_la_tarjeta_sin_tolerancia_y_las_lecturas_nuevas():
    humo = (RAIZ / "tools" / "humo.py").read_text(encoding="utf-8")
    i = humo.index("async def _envios_de_las_facturas")
    cuerpo = humo[i:humo.index("\nasync def ", i + 10)]
    assert "tarjeta_msg_id" in cuerpo and "visto_cambiado_en" in cuerpo
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", cuerpo)
    pruebas = humo[humo.index("pruebas = ["):]
    for nombre in ("_envios_de_las_facturas", "db.envios_de_factura(0)", "db.facturas_de_natalia(0)",
                   "db.envios_de_factura_sin_tarjeta()"):
        assert nombre in pruebas, nombre


def test_las_lecturas_de_los_envios_corren_su_sql_en_sqlite(mundo):  # noqa: F811
    assert correr(db.envios_de_factura(0)) == []
    assert correr(db.facturas_de_natalia(0)) == []
    assert correr(db.envios_de_factura_sin_tarjeta()) == []
    f = de_natalia(mundo, 7, "r-1")
    correr(db.pedir_envio_de_factura_de_natalia(7, f["id"], "correo"))
    assert [(e["factura_id"], e["canal"]) for e in correr(db.envios_de_factura_sin_tarjeta())] == [(f["id"], "correo")]


def test_los_escritores_nuevos_de_la_puerta_de_natalia_no_piden_sesion_y_los_de_la_casa_si(mundo):  # noqa: F811
    """`contestar_envio_de_factura` y `marcar_factura_mandada_a_mano` (reciben un chat) piden la sesión de la casa ANTES de abrir
    la base, igual que sus hermanos (`tests/test_guardar_factura.py` los suma a su censo); los de Natalia no tienen chat."""
    for f in (db.contestar_envio_de_factura, db.marcar_factura_mandada_a_mano):
        assert "chat_id" in inspect.signature(f).parameters
    for nombre, f in _funciones_de_db_de_natalia().items():
        assert "chat_id" not in inspect.signature(f).parameters, nombre


def test_ninguna_cifra_sin_medir_en_los_comentarios_de_la_pieza():
    """Los dos únicos números que la pieza decide (el tope de sesiones por factura y el de facturas por lista) están dichos como
    decisión de la puerta, no como medición."""
    for ruta, nombre in (("web/api_code.py", "LIMITE_DE_SESIONES_POR_FACTURA_DE_NATALIA"),
                         ("db/db.py", "TOPE_DE_FACTURAS_DE_NATALIA")):
        lineas = (RAIZ / ruta).read_text(encoding="utf-8").splitlines()
        i = next(n for n, l in enumerate(lineas) if l.startswith(nombre + " ="))
        antes = " ".join(lineas[max(0, i - 3):i]).lower()
        assert "decisión de la puerta, no una medición" in antes, nombre
