"""Facturas, pieza D4 (21-oct-2026): las retenciones cuando la factura lleva comprobante (NCF).

La regla la decidió Tiziano (`regla-de-tiziano.md`, 21-oct-2026, textual). Para una factura CON NCF:

    bruto            = subtotal − descuento
    retención ISR    = redondeo a centavos de (bruto ÷ 0.85 − bruto)
    base del ITBIS   = bruto + retención
    ITBIS            = 18 % de la base (solo si la factura lleva ITBIS)
    total            = base + ITBIS
    se retienen la retención y TODO el ITBIS
    total a pagar    = bruto

Ejemplo aprobado: bruto 10,000 → retención 1,764.71 → base 11,764.71 → ITBIS 2,117.65 → total 13,882.36 →
total a pagar 10,000.00. SIN NCF la factura queda como la pieza D3, céntimo por céntimo.

Qué se vigila, con la puerta REAL de `db/db.py` en SQLite, las rutas y plantillas REALES y la App de registro doblada
EN LA RED:

  1. LA CUENTA, solo en `db.totales_de_factura`: la retención es bruto ÷ 0.85 − bruto (redondeada a centavos, mitad
     hacia arriba), el ITBIS va sobre el bruto MÁS la retención, y el total a pagar es el bruto. Sin NCF, lo de la D3
     céntimo por centímetro y las tres cifras nuevas en cero.
  2. LA HOJA Y EL PDF: debajo del TOTAL, «Impuesto a retener (ISR 15%)», «ITBIS retenido» y «Total a pagar», en ese
     orden y solo con NCF.
  3. LA LISTA Y LA PÁGINA dicen el total y el total a pagar cuando hay NCF.
  4. LA PUERTA DE NATALIA trae las tres cifras nuevas, y si la casa le pone el NCF al borrador, lo que Natalia lee ya
     viene con la retención.
  5. EL PAGO (pieza 7) sigue midiendo «pagada» con las sesiones del Registro y no se rompe; con NCF la frase del ITBIS
     dice que se retiene completo (no que se cobra aparte, que mandaría a cobrarlo).
  6. LAS CIFRAS NO SE GUARDAN: no hay columnas de retención en `facturas`; salen de los renglones cada vez.

FRONTERA, dicha una vez: SQLite y no Postgres (la pieza no trae migración: no hay nada nuevo que guardar); la App es un
doble; se miran el texto del PDF y el HTML, no el aspecto. La pregunta abierta —una factura con NCF pero SIN ITBIS
lleva la retención y paga el bruto— está construida como dice el encargo y probada aquí; la sala se la confirma a
Tiziano.

Correr:  python3 -m pytest tests/test_facturas_las_retenciones.py -q
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos cambian `httpx` al importarse)

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_hoja_y_pdf_de_factura import _f, _pdf, _sin_blancos, _texto_del_pdf
from test_pantalla_de_facturas import cliente, pantalla, registro  # noqa: F401  (fixtures)
from test_facturas_natalia import api, crear_api, ver  # noqa: F401  (fixtures y ayudantes)

import cotizaciones as cz
import db.db as db
import facturas as fa
import facturas_panel as fp

CENTAVOS = Decimal("0.01")
RETENCION = "Impuesto a retener (ISR 15%)"


def _centavos(valor) -> Decimal:
    return Decimal(valor)


# ── 1. La cuenta: la retención, el ITBIS sobre la base y el total a pagar ──────────────────────────

def test_el_ejemplo_aprobado_por_tiziano_da_exactamente_esas_cifras():
    """subtotal 12,000 · descuento 2,000 · bruto 10,000 · retención 1,764.71 · base 11,764.71 · ITBIS 2,117.65 ·
    total 13,882.36 · total a pagar 10,000.00 (el ejemplo que él aprobó)."""
    t = db.totales_de_factura([Decimal("12000.00")], db.ITBIS_DE_FACTURA, Decimal("2000.00"), con_ncf=True)
    assert (t["subtotal"], t["descuento"], t["neto"]) == (Decimal("12000.00"), Decimal("2000.00"), Decimal("10000.00"))
    assert (t["retencion_isr"], t["itbis"], t["total"]) == (
        Decimal("1764.71"), Decimal("2117.65"), Decimal("13882.36"))
    assert (t["itbis_retenido"], t["total_a_pagar"]) == (Decimal("2117.65"), Decimal("10000.00"))
    # la base del ITBIS es el bruto más la retención, y el total es la base más su ITBIS
    assert t["neto"] + t["retencion_isr"] == Decimal("11764.71") and t["total"] - t["itbis"] == Decimal("11764.71")


@pytest.mark.parametrize("bruto,retencion", [
    ("10000.00", "1764.71"),     # 10,000 ÷ 0.85 = 11,764.705… → 11,764.71 − 10,000
    ("850.00", "150.00"),        # 850 ÷ 0.85 = 1,000 exacto
    ("100.00", "17.65"),         # 117.647… → 117.65 − 100: el redondeo sube
    ("12345.67", "2178.65"),     # 14,524.3176… → 14,524.32 − 12,345.67
    ("0.01", "0.00"),            # 0.0117… → 0.01 − 0.01
])
def test_la_retencion_es_el_bruto_entre_cero_ochenta_y_cinco_menos_el_bruto(bruto, retencion):
    t = db.totales_de_factura([_centavos(bruto)], None, con_ncf=True)
    assert t["retencion_isr"] == _centavos(retencion), t


def test_el_itbis_va_sobre_el_bruto_mas_la_retencion_y_no_sobre_el_bruto():
    """18 % de 11,764.71 = 2,117.65; sobre el bruto sería 1,800."""
    t = db.totales_de_factura([Decimal("10000.00")], db.ITBIS_DE_FACTURA, con_ncf=True)
    assert t["itbis"] == Decimal("2117.65") != Decimal("1800.00")


@pytest.mark.parametrize("montos,descuento", [
    (["10000.00"], None), (["10000.00"], "2000.00"), (["1234.56", "0.44"], None),
    (["0.01"], None), (["99999999.99", "99999999.99"], "1.00")])
@pytest.mark.parametrize("pct", [None, "18"])
def test_lo_que_se_retiene_es_la_retencion_mas_todo_el_itbis_y_asi_queda_el_bruto(montos, descuento, pct):
    """La resta que la hoja promete: total − retención − ITBIS retenido = total a pagar = bruto. Se comprueba en las
    cifras de la cuenta, no en una frase."""
    t = db.totales_de_factura([_centavos(m) for m in montos], None if pct is None else _centavos(pct),
                              None if descuento is None else _centavos(descuento), con_ncf=True)
    assert t["itbis_retenido"] == t["itbis"]
    assert t["total"] - t["retencion_isr"] - t["itbis_retenido"] == t["total_a_pagar"] == t["neto"]


def test_sin_ncf_la_cuenta_es_la_de_la_d3_y_las_tres_cifras_nuevas_son_cero():
    """La cuenta vieja, escrita aquí aparte, contra la de ahora sin NCF: todo igual céntimo por céntimo."""
    for montos in (["10.25"], ["0.03", "0.02"], ["1257.60"], ["99999999.99", "99999999.99"]):
        for pct in (None, db.ITBIS_DE_FACTURA):
            for descuento in (None, Decimal("0"), Decimal("1.00")):
                subtotal = sum((_centavos(m) for m in montos), Decimal("0")).quantize(CENTAVOS)
                neto = subtotal - (Decimal("0.00") if descuento is None else descuento)
                itbis = (Decimal("0") if pct is None
                         else (neto * pct / 100).quantize(CENTAVOS, rounding=ROUND_HALF_UP))
                antes = {"subtotal": subtotal, "neto": neto, "itbis": itbis, "total": neto + itbis}
                ahora = db.totales_de_factura([_centavos(m) for m in montos], pct, descuento)
                assert {k: ahora[k] for k in antes} == antes, (montos, pct, descuento)
                assert ahora["retencion_isr"] == Decimal("0.00") == ahora["itbis_retenido"]
                assert ahora["total_a_pagar"] == ahora["total"]


def test_con_ncf_y_sin_itbis_lleva_la_retencion_del_isr_y_paga_el_bruto():
    """LA PREGUNTA ABIERTA del encargo: con NCF y sin ITBIS, la retención entra igual, no hay ITBIS que retener y el
    total a pagar es el bruto. Construido así; la sala se lo confirma a Tiziano."""
    t = db.totales_de_factura([Decimal("10000.00")], None, con_ncf=True)
    assert (t["itbis"], t["itbis_retenido"]) == (Decimal("0"), Decimal("0"))
    assert (t["retencion_isr"], t["total"], t["total_a_pagar"]) == (
        Decimal("1764.71"), Decimal("11764.71"), Decimal("10000.00"))


# ── 2. La hoja y el PDF ────────────────────────────────────────────────────────────────────────────

def test_la_hoja_pinta_las_tres_lineas_debajo_del_total_y_solo_con_ncf():
    con = fa.hoja_de_factura(_f())                                      # lleva NCF
    assert [t["etiqueta"] for t in con["totales"]] == [
        "Subtotal", "ITBIS 18%", "TOTAL", RETENCION, "ITBIS retenido", "Total a pagar"]
    sin = fa.hoja_de_factura(_f(ncf=False))
    assert [t["etiqueta"] for t in sin["totales"]] == ["Subtotal", "ITBIS 18%", "TOTAL"]


def test_la_hoja_del_ejemplo_aprobado_escribe_las_cifras_del_ejemplo():
    """La hoja no calcula: escribe las de `db.totales_de_factura`. Con el ejemplo de Tiziano, línea por línea."""
    renglones = [{"orden": 1, "descripcion": "Servicio inventado", "detalle": "", "monto": Decimal("12000.00")}]
    f = {"estado": "emitida", "codigo": "FAC-2026-001", "para_nombre": "Cliente inventado", "rnc_cedula": None,
         "fecha": "2026-10-21", "ncf": "B0100000001", "ncf_tipo": "Crédito fiscal", "itbis_pct": Decimal("18.00"),
         "descuento": Decimal("2000.00"), "motivo_anulacion": None, "renglones": renglones}
    f.update(db.totales_de_factura([Decimal("12000.00")], Decimal("18.00"), Decimal("2000.00"), con_ncf=True))
    h = fa.hoja_de_factura(f)
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [
        ("Subtotal", "RD$12,000"), ("Descuento", "- RD$2,000"), ("ITBIS 18%", "RD$2,117.65"),
        ("TOTAL", "RD$13,882.36"), (RETENCION, "RD$1,764.71"), ("ITBIS retenido", "RD$2,117.65"),
        ("Total a pagar", "RD$10,000")]


def test_el_pdf_trae_las_tres_lineas_solo_con_ncf():
    con = _sin_blancos(_texto_del_pdf(_pdf(_f())))
    for texto in ("Impuestoaretener(ISR15%)", "ITBISretenido", "Totalapagar"):
        assert texto in con, texto
    sin = _sin_blancos(_texto_del_pdf(_pdf(_f(ncf=False))))
    for texto in ("Impresoaretener", "ITBISretenido", "Totalapagar"):
        assert texto not in sin, texto


# ── 3. La lista y la página ────────────────────────────────────────────────────────────────────────

def test_la_lista_y_la_pagina_dicen_el_total_a_pagar_solo_con_ncf(pantalla):  # noqa: F811
    con_ncf = tgf.crear(ncf="B0100000001", ncf_tipo="Crédito fiscal", itbis_pct=18, descuento="2000.00",
                        renglones=[{"descripcion": "Servicio inventado", "monto": "12000.00"}])
    sin_ncf = tgf.otra(itbis_pct=18, renglones=[{"descripcion": "Otro servicio", "monto": "500.00"}])
    html = cliente().get("/facturas").text
    assert cz.dinero_en_pesos(Decimal("13882.36")) in html          # el total de la factura
    assert f"a pagar {cz.dinero_en_pesos(Decimal('10000.00'))}" in html
    # una sola fila lleva el «a pagar»: la que tiene NCF
    assert html.count("data-total-a-pagar") == 1
    pagina = cliente().get(f"/facturas/{con_ncf['id']}").text
    assert "Total a pagar" in pagina and cz.dinero_en_pesos(Decimal("10000.00")) in pagina
    assert "Total a pagar" not in cliente().get(f"/facturas/{sin_ncf['id']}").text


def test_las_cifras_de_la_retencion_no_se_guardan_en_la_base(mundo):  # noqa: F811
    """Salen de los renglones cada vez, como el subtotal y el ITBIS: la tabla no gana columnas."""
    columnas = {c[1] for c in mundo.con.execute("PRAGMA table_info(facturas)")}
    assert not columnas & {"retencion_isr", "itbis_retenido", "total_a_pagar", "bruto"}


# ── 4. La puerta de Natalia ───────────────────────────────────────────────────────────────────────

def test_la_puerta_de_natalia_trae_las_tres_cifras_nuevas(api):  # noqa: F811
    r = crear_api(("11",), itbis=True)
    assert r.status_code == 200, r.text
    f = r.json()["factura"]
    assert {"retencion_isr", "itbis_retenido", "total_a_pagar"} <= set(f)
    # una factura que hace Natalia nace SIN NCF: sin retención y con el total como total a pagar
    assert (f["retencion_isr"], f["itbis_retenido"], f["total_a_pagar"]) == ("0.00", "0.00", f["total"])


def test_la_puerta_de_natalia_lee_la_retencion_cuando_la_casa_le_pone_el_ncf(api):  # noqa: F811
    """El borrador lo corrige la casa; Natalia lo relee por su ruta y lo que recibe ya trae las cuentas del comprobante.
    Es el ejemplo aprobado, por las rutas de verdad."""
    r = crear_api(("11",), itbis=True, renglones=[{"descripcion": "Servicio inventado", "monto": "12000.00"}],
                  descuento="2000.00")
    assert r.status_code == 200, r.text
    fid = r.json()["factura"]["factura_id"]
    candado = tgf.leer(fid)["cambiado_en"]
    tgf.correr(db.corregir_borrador_de_factura(fid, tgf.CASA, candado,
                                              {"ncf": "B0100000001", "ncf_tipo": "Crédito fiscal"}))
    leida = ver(fid).json()["factura"]
    assert (leida["subtotal"], leida["descuento"], leida["itbis"], leida["total"]) == (
        "12000.00", "2000.00", "2117.65", "13882.36")
    assert (leida["retencion_isr"], leida["itbis_retenido"], leida["total_a_pagar"]) == (
        "1764.71", "2117.65", "10000.00")


# ── 5. El pago (pieza 7) no se rompe ─────────────────────────────────────────────────────────────

def test_el_pago_con_ncf_sigue_midiendose_con_las_sesiones_y_la_frase_del_itbis_dice_que_se_retiene():
    f = {"estado": "emitida", "ncf": "B0100000001", "itbis_pct": Decimal("18.00"), "subtotal": Decimal("12000.00"),
         "descuento": Decimal("2000.00"), "neto": Decimal("10000.00"), "itbis": Decimal("2117.65"),
         "total": Decimal("13882.36"), "retencion_isr": Decimal("1764.71"), "itbis_retenido": Decimal("2117.65"),
         "total_a_pagar": Decimal("10000.00")}
    ligas = [{"leida": "ok", "fila": {"total": Decimal("12000.00"), "abonado": Decimal("0"),
                                      "saldo": Decimal("12000.00")}}]
    p = fp.el_pago_de_la_factura(f, ligas)
    # «pagada» se mide con las sesiones, igual que sin NCF: el total de la factura no entra en la comparación
    assert p["estado"] == "sin_pagos" and "No cuadra" not in p["detalle"]
    assert "se retiene completo" in p["itbis"] and "se cobra aparte" not in p["itbis"]
    assert cz.dinero_en_pesos(Decimal("10000.00")) in p["itbis"]
    ligas[0]["fila"]["saldo"] = Decimal("0")
    assert fp.el_pago_de_la_factura(f, ligas)["estado"] == "pagada"
