"""Facturas, pieza 7 (21-oct-2026): el pago leído de la App.

Qué se vigila, por las RUTAS reales, las plantillas reales y la puerta REAL de `db/db.py` en SQLite, con la App de
registro doblada EN LA RED (`tests/_app_de_registro.py`, que afirma el contrato de la App):

  1. LO PAGADO SALE DE LA APP, EN EL MOMENTO: es la suma del `abonado` de las sesiones y trabajos ligados vivos; la
     factura no guarda ningún pago y lo que cambia en la App cambia lo que la factura dice, sin tocar la base de Lucy.
  2. «PAGADA» ES QUE LAS SESIONES NO DEBAN NADA: la suma del `saldo` que da la App por sesión (sin contar las
     canceladas) es cero o menos. Lo que se muestra como pagado es la suma del `abonado`, y el «de RD$Y» es la suma de
     los `total` de esas sesiones. El «no cuadra con la App: revísala» —el subtotal de la factura contra la suma de los
     `total` de las sesiones— es un aviso y NO cambia el estado. El ITBIS no cuenta y la pantalla dice que se cobra
     aparte.
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
import registro_lectura

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


# ── 2. «Pagada» es que las sesiones no deban nada, y el «no cuadra» solo avisa ─────────────────────

def test_el_itbis_no_cuenta_para_pagada_y_la_pantalla_dice_que_se_cobra_aparte(pantalla):
    """4,500 pagados sobre un renglón de 4,500 con ITBIS, y la sesión sin saldo: pagada, sin «no cuadra» (el subtotal
    sin ITBIS es el que se compara) y con la frase del ITBIS."""
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


def test_una_sesion_que_debe_no_se_dice_pagada_aunque_la_factura_cobre_solo_lo_abonado(pantalla):
    """EL CASO 1 DEL TESTIGO: la sesión cuesta 1,000, se han abonado 500 y la factura se escribió por 500. La sesión debe
    500 en el Registro, así que la factura NO está pagada: dice «Pagado RD$500 de RD$1,000» y avisa que no cuadra."""
    pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION_B, ref=14, codigo="s014", total=1000.0, abonado=500.0, saldo=500.0)]}
    f = una_factura(ref="14", monto="500.00")
    estado, texto, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{f['id']}").text)
    assert (estado, texto) == ("con_abono", "Pagado RD$500 de RD$1,000")
    assert detalle == ("No cuadra con la App: revísala (la factura suma RD$500 y el registro dice RD$1,000 de sus "
                       "sesiones).")


def test_una_factura_que_cobra_mas_no_deja_de_estar_pagada_si_la_sesion_no_debe_nada(pantalla):
    """EL CASO 2 DEL TESTIGO: la sesión de 9,000 está pagada entera (saldo 0) y la factura se escribió por 10,000. Las
    sesiones no deben nada: está pagada, y el aviso de que no cuadra va aparte, sin cambiar el estado."""
    pantalla.registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION, total=9000.0, abonado=9000.0, saldo=0.0)]}
    f = una_factura(ref=str(SESION["ref"]), monto="10000.00")
    estado, texto, detalle = tarjeta_de_pago(cliente().get(f"/facturas/{f['id']}").text)
    assert (estado, texto) == ("pagada", "Pagada")
    assert detalle == ("No cuadra con la App: revísala (la factura suma RD$10,000 y el registro dice RD$9,000 de sus "
                       "sesiones).")


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


# ── 6. La lista: el pago de cada factura, leído de la App en tandas ───────────────────────────────

def test_la_lista_lee_el_pago_de_cada_factura_y_pide_todas_las_referencias_sin_ficha(pantalla, registro):
    """Dos facturas con dos sesiones: una pagada y otra que debe. Las dos referencias viajan en la MISMA llamada (caben en
    una tanda de 100) y sin `persona` (no hace falta la ficha entera de nadie para saber cuánto se ha pagado)."""
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


def test_la_lectura_de_la_lista_va_en_tandas_de_cien(registro):
    """Con más referencias que una tanda, la App se pide varias veces (no es «una sola llamada»): 150 referencias son dos
    llamadas, de 100 y de 50, y lo que contesta cada una se junta en una lectura sola."""
    import asyncio
    refs = [str(n) for n in range(1, 151)]
    registro.sesiones = {"puede_ligar": False, "motivo_sin_ligar": "sin_persona", "sesiones": []}
    registro.otras = [dict(SESION_B, ref=int(n), codigo=f"s{n:03d}") for n in range(1, 151)]
    registro.pedidos.clear()
    lectura = asyncio.run(registro_lectura.sesiones_por_refs(refs))
    tandas = [[v for k, v in c if k == "sesion"] for c in pedidas_a_la_app(registro)]
    assert [len(t) for t in tandas] == [registro_lectura.REFS_POR_LLAMADA, 50]
    assert len(lectura["sesiones"]) == 150 and lectura["estado"] == "sin_cliente"


def test_la_lista_de_una_factura_con_una_sesion_que_ya_no_esta_lo_dice(pantalla):
    tgf.crear(cliente_noco_id=7, liga={"tipo": "sesion", "ref": "404"},
              renglones=[{"descripcion": "Ensayo", "monto": "1000.00"}])
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_se_pudo", SIN_LEER)]


# ── 7. La cuenta, con entradas inventadas ─────────────────────────────────────────────────────────

def _factura(subtotal="4500.00", itbis=None, estado="emitida"):
    return {"estado": estado, "subtotal": Decimal(subtotal),
            "itbis": Decimal(itbis or "0.00"), "itbis_pct": Decimal("18") if itbis else None}


def _leida(ref, total=None, abonado=None, saldo=None, leida="ok"):
    fila = {"ref": ref, "total": total, "abonado": abonado, "saldo": saldo} if leida == "ok" else None
    return {"ref": ref, "tipo": "sesion", "codigo": f"s{ref}", "leida": leida, "fila": fila}


def test_la_cuenta_del_pago_es_pura_y_cae_del_lado_estricto():
    de, algo, cero = Decimal("4500.00"), Decimal("500.00"), Decimal("0")
    # las sesiones no deben nada (saldo cero o menos): pagada, aunque la factura cobre otra cosa
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de, cero)])["estado"] == "pagada"
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de + algo, -algo)])["estado"] == "pagada"
    # deben: «Pagado X de Y» con lo abonado y el total de las sesiones
    pago = fp.el_pago_de_la_factura(_factura(), [_leida("1", de, algo, de - algo)])
    assert (pago["estado"], pago["texto"]) == ("con_abono", "Pagado RD$500 de RD$4,500")
    assert pago["detalle"] == ""
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, cero, de)])["texto"] == "Sin pagos"
    # la que decide es la SUMA: una sesión saldada y otra que debe → todavía debe
    dos = [_leida("1", de, de, cero), _leida("2", de, cero, de)]
    assert fp.el_pago_de_la_factura(_factura(subtotal="9000.00"), dos)["estado"] == "con_abono"
    # lo que no se sabe, se dice: nunca «sin pagos»
    for rotas in ([_leida("1", de, None, de)], [_leida("1", de, algo, None)], [_leida("1", None, algo, de)],
                  [_leida("1", de, Decimal("-5"), de)], [_leida("1", Decimal("-1"), cero, de)],
                  [_leida("1", de, cero, de, leida="no_esta")],
                  [_leida("1", None, None, None, leida="cancelada"), _leida("2", de, cero, de, leida="no_se_pudo")],
                  None):
        pago = fp.el_pago_de_la_factura(_factura(), rotas)
        assert pago["estado"] == "no_se_pudo" and pago["texto"] == SIN_LEER, rotas
    # sin ninguna liga que contar no hay nada pagado
    assert fp.el_pago_de_la_factura(_factura(), [])["estado"] == "sin_pagos"
    # la anulada no se pinta nunca
    assert fp.el_pago_de_la_factura(_factura(estado="anulada"), [_leida("1", de, de, cero)]) == fp.PAGO_QUE_NO_SE_PINTA
    # una cancelada no entra en ninguna de las cifras ni inventa un «no cuadra»
    pago = fp.el_pago_de_la_factura(_factura(), [_leida("1", None, None, None, leida="cancelada")])
    assert (pago["estado"], pago["detalle"]) == ("sin_pagos", "")
    # el ITBIS se nombra con su cifra solo cuando la factura lo lleva
    assert fp.el_pago_de_la_factura(_factura(itbis="810.00"), [_leida("1", de, de, cero)])["itbis"] == \
        "El ITBIS (RD$810) se cobra aparte; no se lleva en la App."
    assert fp.el_pago_de_la_factura(_factura(), [_leida("1", de, de, cero)])["itbis"] == ""


def test_el_aviso_de_no_cuadra_es_un_aviso_y_no_cambia_el_estado():
    """La factura suma 4,000 y la App dice 4,500: el aviso va con las dos cifras y el estado lo deciden las sesiones."""
    de = Decimal("4500.00")
    pago = fp.el_pago_de_la_factura(_factura(subtotal="4000.00"), [_leida("1", de, de, Decimal("0"))])
    assert pago["estado"] == "pagada"
    assert pago["detalle"] == ("No cuadra con la App: revísala (la factura suma RD$4,000 y el registro dice RD$4,500 "
                               "de sus sesiones).")
    # con una cancelada la comparación no está completa: no se afirma que no cuadre
    pago = fp.el_pago_de_la_factura(_factura(subtotal="4000.00"),
                                    [_leida("1", de, de, Decimal("0")), _leida("2", None, None, None, leida="cancelada")])
    assert (pago["estado"], pago["detalle"]) == ("pagada", "")


def test_una_cifra_que_la_app_no_trae_es_no_se_pudo_y_no_un_estado_inventado():
    """Si la App no trae el total, el saldo o lo abonado de una sesión, no se puede decir si debe ni cuánto: se dice."""
    de = Decimal("4500.00")
    for fila in (_leida("1", None, de, de), _leida("1", de, de, None), _leida("1", de, None, de)):
        pago = fp.el_pago_de_la_factura(_factura(), [fila])
        assert (pago["estado"], pago["texto"]) == ("no_se_pudo", SIN_LEER), fila
        assert "no dijo" in pago["detalle"]


# ── 8. La lectura de la lista, con la App caída ───────────────────────────────────────────────────

def test_si_la_app_no_contesta_la_lista_entera_lo_dice(pantalla):
    una_factura(ref=str(SESION["ref"]), monto="9000.00")
    una_factura(ref=str(SESION_B["ref"]), monto="4500.00")
    pantalla.registro.forzada = (502, b'{"error": "no_se_pudo_leer"}', "application/json", 0)
    assert pago_en_la_lista(cliente().get("/facturas").text) == [("no_se_pudo", SIN_LEER)] * 2
