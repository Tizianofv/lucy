"""Facturas, pieza 7 (21-oct-2026): el pago leído de la App.

Qué se vigila, por las RUTAS reales, las plantillas reales y la puerta REAL de `db/db.py` en SQLite, con la App de
registro doblada EN LA RED (`tests/_app_de_registro.py`, que afirma el contrato de la App):

  1. LO PAGADO SALE DE LA APP, EN EL MOMENTO: es la suma del `abonado` de las sesiones y trabajos ligados vivos; la
     factura no guarda ningún pago y lo que cambia en la App cambia lo que la factura dice, sin tocar la base de Lucy.
  2. LA BASE DE «PAGADA» ES EL SUBTOTAL DE LA FACTURA, SIN ITBIS: si un renglón se cambió a mano manda el renglón
     escrito, y cuando la suma de la App no da lo mismo la factura dice «no cuadra con la App: revísala» con las dos
     cifras. El ITBIS no cuenta y la pantalla dice que se cobra aparte.
  3. CON LA APP CAÍDA NO SE INVENTA: dice «No se pudo leer lo pagado» y nunca «Sin pagos» ni «Pagada».
  4. UNA ANULADA NO MUESTRA ESTADO DE PAGO, ni en la página ni en la lista, aunque la App diga que está todo pagado.
  5. UNA SESIÓN CANCELADA (o que el registro ya no devuelve) SE DICE, y una cancelada no cuenta como pagada.

FRONTERA, dicha una vez: SQLite y no Postgres; la App es un doble; se miran el HTML y las respuestas, no el aspecto. La
pagina de un proyecto no se toca en esta pieza (sigue sin estado de pago). El enlace «Anotar el pago en la App» del
diseño §7 no se pinta: el mapa de la App dice que no hay dirección que abra una sesión (ver REPORTE.md).

Correr:  python3 -m pytest tests/test_facturas_el_pago.py -q
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudantes de la pantalla)
    CASA, SESION, SESION_B, SU_CANCELADA, TRABAJO, cliente, pantalla, registro)

import db.db as db
import facturas_panel as fp

REF = "13"                      # la sesión de la ficha con total 4,500 y abonado 1,000 (pieza 4)
SU_TOTAL = Decimal("4500.00")
SIN_LEER = "No se pudo leer lo pagado."


def una_factura(*, ref=REF, monto="4500.00", **kw):
    """Una factura de una sesión, con el renglón escrito a mano (es lo que la persona puede cambiar)."""
    return tgf.crear(cliente_noco_id=7, liga={"tipo": "sesion", "ref": ref},
                     renglones=[{"descripcion": "Ensayo", "monto": monto}], **kw)


def tarjeta_de_pago(html: str) -> tuple[str, str, str] | None:
    """`(estado, texto, detalle)` de la tarjeta del pago de la página, o `None` si no sale."""
    m = re.search(r'<div class="tarjeta" data-pago="([^"]+)">(.*?)</div>', html, re.S)
    if m is None:
        return None
    texto = re.search(r"<p data-pago-texto>(.*?)</p>", m.group(2), re.S)
    detalle = re.search(r'<p class="suave" data-pago-detalle>(.*?)</p>', m.group(2), re.S)
    return (m.group(1), (texto.group(1).strip() if texto else ""),
            re.sub(r"\s+", " ", detalle.group(1)).strip() if detalle else "")


def pago_en_la_lista(html: str) -> list[tuple[str, str]]:
    """`[(estado, texto)]` de la columna del pago de cada fila de la lista, en el orden en que salen."""
    return [(e, t.strip()) for e, t in re.findall(r'<td data-pago="([^"]*)">([^<]*)</td>', html)]


def pedidas_a_la_app(registro):
    """Las consultas `GET` que le llegaron a la App a la ruta de las sesiones, como lista de `[(clave, valor)]`."""
    return [parse_qsl(urlsplit(p["ruta"]).query) for p in registro.pedidos
            if p["metodo"] == "GET" and urlsplit(p["ruta"]).path == "/api/lucy/sesiones"]


# ── 1. Lo pagado sale de la App, en el momento ─────────────────────────────────────────────────────

def test_la_pagina_dice_lo_pagado_segun_lo_que_la_app_tiene_ahora(pantalla):
    """El mismo renglón de 4,500 y la misma factura: lo que cambia es lo que la App dice que se ha pagado."""
    f = una_factura()
    casos = [(1000.0, 3500.0, "con_abono", "Pagado RD$1,000 de RD$4,500"),
             (4500.0, 0.0, "pagada", "Pagada"),
             (0.0, 4500.0, "sin_pagos", "Sin pagos")]
    for abonado, saldo, estado, frase in casos:
        pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
            dict(SESION_B, total=4500.0, abonado=abonado, saldo=saldo)]}
        html = cliente().get(f"/facturas/{f['id']}").text
        assert tarjeta_de_pago(html)[:2] == (estado, frase), (abonado, html)


def test_lo_pagado_no_se_guarda_en_ningun_sitio_de_la_factura(pantalla):
    """Ver la página y la lista lee la App y no escribe nada: ni un pago, ni una huella, ni un candado."""
    f = una_factura()
    tgf.correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"]))
    antes = tgf.foto(pantalla)
    cliente().get(f"/facturas/{f['id']}")
    cliente().get("/facturas")
    assert tgf.foto(pantalla) == antes


# ── 2. La base es el subtotal de la factura, sin ITBIS ────────────────────────────────────────────

def test_el_itbis_no_cuenta_para_pagada_y_la_pantalla_dice_que_se_cobra_aparte(pantalla):
    """4,500 pagados sobre un renglón de 4,500 con ITBIS: la factura está pagada; el ITBIS (810) no pasa por la App."""
    f = una_factura(itbis_pct=18)
    assert (f["subtotal"], f["itbis"], f["total"]) == (Decimal("4500.00"), Decimal("810.00"), Decimal("5310.00"))
    pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION_B, total=4500.0, abonado=4500.0, saldo=0.0)]}
    estado, texto, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{f['id']}").text)
    assert (estado, texto, detalle) == ("pagada", "Pagada", "")
    html = cliente().get(f"/facturas/{f['id']}").text
    assert "El ITBIS (RD$810) se cobra aparte; no se lleva en la App." in html
    # y la lista dice lo mismo de esa factura
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("pagada", "Pagada")]


def test_sin_itbis_no_hay_frase_de_itbis(pantalla):
    f = una_factura()
    assert "data-pago-itbis" not in cliente().get(f"/facturas/{f['id']}").text


def test_si_los_renglones_se_cambiaron_a_mano_manda_el_renglon_escrito_y_si_no_cuadra_se_dice(pantalla):
    """La App dice 4,500 y la factura cobra 4,000: manda lo que se le facturó al cliente (pagada), y se avisa que la
    App y la factura no dicen lo mismo. Al revés, la factura cobra 5,000: faltan 500 y también se avisa."""
    pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION_B, total=4500.0, abonado=4500.0, saldo=0.0), dict(SESION, total=9000.0, abonado=9000.0, saldo=0.0)]}
    barata = una_factura(monto="4000.00")
    estado, texto, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{barata['id']}").text)
    assert (estado, texto) == ("pagada", "Pagada")
    assert detalle == ("No cuadra con la App: revísala (la factura suma RD$4,000 y el registro dice RD$4,500 de sus "
                       "sesiones).")
    cara = una_factura(ref=str(SESION["ref"]), monto="10000.00")
    estado, texto, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{cara['id']}").text)
    assert (estado, texto) == ("con_abono", "Pagado RD$9,000 de RD$10,000")
    assert "No cuadra con la App: revísala" in detalle and "RD$10,000" in detalle and "RD$9,000" in detalle


def test_cuando_la_app_y_la_factura_cuadran_no_se_avisa_de_nada(pantalla):
    f = una_factura()
    estado, _, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{f['id']}").text)
    assert estado == "con_abono" and detalle == ""


# ── 3. Con la App caída no se inventa ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("falla", [
    (500, b"{}", "application/json", 0),
    (502, b'{"error": "no_se_pudo_leer"}', "application/json", 0),
    (200, b"no es json", "text/plain", 0),
    (200, b'["no", "es", "un", "objeto"]', "application/json", 0),
    (401, b'{"error": "no"}', "application/json", 0),
], ids=["500", "502", "basura", "otra-forma", "401"])
def test_si_la_app_no_contesta_la_factura_dice_que_no_se_pudo_leer_y_no_inventa(pantalla, falla):
    f = una_factura()
    pantalla.registro.forzada = falla
    html = cliente().get(f"/facturas/{f['id']}").text
    estado, texto, detalle = tarjeta_de_pago(html)
    assert (estado, texto) == ("no_se_pudo", SIN_LEER)
    assert detalle == "El registro no contestó."
    assert "Sin pagos" not in html and "Pagado RD$" not in html
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_se_pudo", SIN_LEER)]


def test_si_la_app_no_contesta_una_anulada_sigue_sin_estado_de_pago(pantalla):
    """La anulada no muestra pago ni con la App caída: no es que no se pudo leer, es que no se pinta."""
    a = tgf.emitir(una_factura())
    tgf.correr(db.anular_factura(a["id"], CASA, "se hizo dos veces"))
    pantalla.registro.forzada = (500, b"{}", "application/json", 0)
    assert "data-pago" not in cliente().get(f"/facturas/{a['id']}").text


# ── 4. Una anulada no muestra estado de pago ──────────────────────────────────────────────────────

def test_una_anulada_no_se_muestra_pagada_aunque_la_app_diga_que_esta_todo_pagado(pantalla):
    a = tgf.emitir(una_factura())
    tgf.correr(db.anular_factura(a["id"], CASA, "se hizo dos veces"))
    pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION_B, total=4500.0, abonado=4500.0, saldo=0.0)]}
    html = cliente().get(f"/facturas/{a['id']}").text
    assert "data-pago" not in html and "Pagada" not in html
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_aplica", "")]


# ── 5. Una cancelada o una que ya no está se dicen ────────────────────────────────────────────────

def test_una_sesion_cancelada_se_dice_y_no_cuenta_como_pagada(pantalla):
    f = una_factura(ref=str(SU_CANCELADA["ref"]), monto="1000.00")
    html = cliente().get(f"/facturas/{f['id']}").text
    assert "Cancelada o pospuesta en el registro" in html
    estado, texto, _ = tarjeta_de_pago(html)
    assert (estado, texto) == ("sin_pagos", "Sin pagos")


def test_una_sesion_que_la_app_ya_no_devuelve_se_dice_y_el_pago_no_se_inventa(pantalla):
    f = tgf.crear(cliente_noco_id=7, liga={"tipo": "sesion", "ref": "404"},
                  renglones=[{"descripcion": "Ensayo", "monto": "1000.00"}])
    html = cliente().get(f"/facturas/{f['id']}").text
    assert "El registro ya no la devuelve" in html
    estado, texto, detalle = tarjeta_de_pago(html)
    assert (estado, texto) == ("no_se_pudo", SIN_LEER)
    assert detalle == "El registro ya no devuelve una de sus sesiones o trabajos."


# ── 6. La lista: el pago de cada factura, en una sola lectura ─────────────────────────────────────

def test_la_lista_lee_el_pago_de_cada_factura_de_la_app_en_una_sola_llamada_y_sin_ficha(pantalla, registro):
    """Dos facturas con dos sesiones: una pagada y otra con abono. La App se lee UNA vez, con las dos referencias y sin
    `persona` (no hace falta la ficha entera de nadie para saber cuánto se ha pagado)."""
    pagada = una_factura(ref=str(SESION["ref"]), monto="9000.00")            # la 11: 9,000
    con_abono = una_factura(ref=str(SESION_B["ref"]), monto="4500.00")       # la 13: 4,500 con 1,000 abonados
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION, total=9000.0, abonado=9000.0, saldo=0.0),
        dict(SESION_B, total=4500.0, abonado=1000.0, saldo=3500.0)]}
    registro.pedidos.clear()
    html = cliente().get("/facturas").text
    assert pago_en_la_lista(html) == [("con_abono", "Pagado RD$1,000 de RD$4,500"), ("pagada", "Pagada")]
    consultas = pedidas_a_la_app(registro)
    assert len(consultas) == 1, consultas
    assert [v for k, v in consultas[0] if k == "sesion"] == sorted([str(SESION["ref"]), str(SESION_B["ref"])])
    assert all(k != "persona" for k, _ in consultas[0])
    assert pagada["id"] != con_abono["id"]


def test_la_lista_de_una_factura_con_una_sesion_que_ya_no_esta_lo_dice(pantalla):
    tgf.crear(cliente_noco_id=7, liga={"tipo": "sesion", "ref": "404"},
              renglones=[{"descripcion": "Ensayo", "monto": "1000.00"}])
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_se_pudo", SIN_LEER)]


# ── 7. La cuenta, con entradas inventadas ─────────────────────────────────────────────────────────

def _factura(subtotal="4500.00", itbis=None, estado="emitida"):
    return {"estado": estado, "subtotal": Decimal(subtotal),
            "itbis": Decimal(itbis or "0.00"), "itbis_pct": Decimal("18") if itbis else None}


def _leida(ref, total=None, abonado=None, leida="ok", cancelada=False):
    fila = {"ref": ref, "total": total, "abonado": abonado, "saldo": None} if leida == "ok" else None
    return {"ref": ref, "tipo": "sesion", "codigo": f"s{ref}", "leida": leida, "fila": fila}


def test_la_cuenta_del_pago_es_pura_y_cae_del_lado_estricto():
    de = Decimal("4500.00")
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, Decimal("0"))])["estado"] == "sin_pagos"
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, Decimal("1.00"))])["texto"] == "Pagado RD$1 de RD$4,500"
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de)])["estado"] == "pagada"
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de + 1)])["estado"] == "pagada"
    # lo que no se sabe, se dice: nunca «sin pagos»
    for rotas in ([_leida("1", de, None)], [_leida("1", None, None)], [_leida("1", de, Decimal("-5"))],
                  [_leida("1", None, Decimal("0"), leida="no_esta")],
                  [_leida("1", None, None, leida="cancelada"), _leida("2", de, None, leida="no_se_pudo")],
                  None, []):
        pago = fp.el_pago_de_la_factura(_factura(), rotas)
        if rotas == []:
            assert pago["estado"] == "sin_pagos"
        else:
            assert pago["estado"] == "no_se_pudo" and pago["texto"] == SIN_LEER, rotas
    # la anulada no se pinta nunca
    assert fp.el_pago_de_la_factura(_factura(estado="anulada"), [_leida("1", de, de)]) == fp.PAGO_QUE_NO_SE_PINTA
    # una cancelada no entra en ninguna de las dos cifras y no inventa un «no cuadra»
    pago = fp.el_pago_de_la_factura(_factura(), [_leida("1", None, Decimal("0"), leida="cancelada")])
    assert (pago["estado"], pago["detalle"]) == ("sin_pagos", "")
    # el ITBIS se nombra con su cifra solo cuando la factura lo lleva
    assert fp.el_pago_de_la_factura(_factura(itbis="810.00"), [_leida("1", de, de)])["itbis"] == \
        "El ITBIS (RD$810) se cobra aparte; no se lleva en la App."
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de)])["itbis"] == ""


def test_un_renglon_sin_total_en_la_app_no_inventa_un_no_cuadra():
    """Si la App no trae el total, no se puede comparar: se dice lo pagado y no se afirma que no cuadre."""
    de = Decimal("4500.00")
    pago = fp.el_pago_de_la_factura(_factura(), [_leida("1", None, de)])
    assert (pago["estado"], pago["detalle"]) == ("pagada", "")


# ── 8. La lectura de la lista, con la App caída ───────────────────────────────────────────────────

def test_si_la_app_no_contesta_la_lista_entera_lo_dice(pantalla):
    una_factura(ref=str(SESION["ref"]), monto="9000.00")
    una_factura(ref=str(SESION_B["ref"]), monto="4500.00")
    pantalla.registro.forzada = (502, b'{"error": "no_se_pudo_leer"}', "application/json", 0)
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_se_pudo", SIN_LEER)] * 2
