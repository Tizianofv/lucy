"""Facturas, pieza D3 (21-oct-2026): el descuento al final.

Tiziano escogió la opción B: una línea de descuento al final, junto al subtotal. La sala decidió (cosa técnica) que el
ITBIS se calcula sobre el subtotal YA DESCONTADO (el neto).

Qué se vigila, con la puerta REAL de `db/db.py` en SQLite, las rutas y plantillas REALES y la App de registro doblada
EN LA RED:

  1. LOS TOTALES, en `db.totales_de_factura` y en ningún otro sitio: neto = subtotal − descuento; ITBIS = 18 % del
     neto (mitad hacia arriba); total = neto + ITBIS. Sin descuento, todo igual que antes, céntimo por céntimo.
  2. EL DESCUENTO ES UNA CIFRA POR FACTURA: ≥ 0, dos decimales, y NO PUEDE PASAR DEL SUBTOTAL. La base cierra el lado
     negativo con un CHECK; el tope contra el subtotal lo exige la puerta al crear y al corregir (con los renglones
     que van a quedar) y lo cuenta el humo contra la base real.
  3. DESDE LAS SESIONES se llena solo: la suma de los `descuento` de las sesiones escogidas, en la pantalla y en la
     puerta de Natalia cuando ella no lo manda. Se puede cambiar, y lo escrito manda.
  4. LA HOJA Y EL PDF: debajo del subtotal, «Descuento» y «- RD$X», solo si hay descuento; después el ITBIS sobre el
     neto y el total. La cotización no cambia.
  5. LA PANTALLA: crear y corregir aceptan el descuento; una emitida no se corrige. Lo escrito vuelve en un rechazo.
  6. EL PAGO (pieza 7) sigue comparando el subtotal SIN descuento con la suma de los totales de las sesiones.
  7. Q1 del testigo de la D2: un renglón de Natalia con `monto` y `cantidad` o `unidad` (sin precio) es 422, no se
     descarta nada en silencio.
  8. LA MIGRACIÓN, el esquema, el ensayo y el humo.

FRONTERA, dicha una vez: SQLite y no Postgres (el CHECK corre en los dos; `NUMERIC(14,2)` no impone decimales en SQLite:
los rechaza la puerta); la App es un doble; se miran el texto del PDF y el HTML, no el aspecto. El signo del descuento
es el guion ASCII porque la letra de fábrica del PDF (Helvetica) no tiene el menos tipográfico y `fpdf2` revienta con él.

Correr:  python3 -m pytest tests/test_facturas_el_descuento.py -q
"""
from __future__ import annotations

import ast
import io
import re
import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos cambian `httpx` al importarse)
from pypdf import PdfReader

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
import test_facturas_tablas as ft
from test_facturas_el_detalle import DETALLADA, _con_cuenta
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudantes de la pantalla)
    SESION, SESION_B, cliente, crear_por_la_pantalla, pantalla, registro)
from test_facturas_natalia import api, crear_api  # noqa: F401  (fixture de la puerta de Natalia y su ayudante)

import cotizaciones as cz
import db.db as db
import facturas as fa
import facturas_panel as fp


# ── 1. Los totales: el ITBIS sobre el neto ──────────────────────────────────────────────────────────

def test_los_totales_restan_el_descuento_y_el_itbis_va_sobre_el_neto():
    t = db.totales_de_factura([Decimal("1000.00")], db.ITBIS_DE_FACTURA, Decimal("100.00"))
    assert (t["subtotal"], t["descuento"], t["neto"], t["itbis"], t["total"]) == (
        Decimal("1000.00"), Decimal("100.00"), Decimal("900.00"), Decimal("162.00"), Decimal("1062.00"))


def test_el_itbis_del_neto_redondea_a_centavos_con_la_mitad_hacia_arriba():
    # 900.05 × 18 % = 162.009 → 162.01 (no 162.00)
    t = db.totales_de_factura([Decimal("1000.05")], db.ITBIS_DE_FACTURA, Decimal("100.00"))
    assert (t["neto"], t["itbis"]) == (Decimal("900.05"), Decimal("162.01"))


def test_sin_descuento_todo_queda_igual_que_antes_centimo_por_centimo():
    """La cuenta de antes (ITBIS sobre el subtotal, mitad hacia arriba) contra la de ahora con el descuento en cero o
    sin mandarlo."""
    from decimal import ROUND_HALF_UP
    centavos = Decimal("0.01")
    for montos in ([Decimal("10.25")], [Decimal("0.03"), Decimal("0.02")], [Decimal("1257.60")],
                   [Decimal("0.05")], [Decimal("99999999.99"), Decimal("99999999.99")]):
        for pct in (None, db.ITBIS_DE_FACTURA):
            subtotal = sum(montos, Decimal("0")).quantize(centavos)
            itbis = (Decimal("0") if pct is None
                     else (subtotal * pct / 100).quantize(centavos, rounding=ROUND_HALF_UP))
            antes = {"subtotal": subtotal, "itbis": itbis, "total": subtotal + itbis}
            for descuento in (None, Decimal("0"), "0", "0.00"):
                ahora = db.totales_de_factura(montos, pct, descuento)
                assert {k: ahora[k] for k in antes} == antes, (montos, pct, descuento)


def test_el_descuento_puede_dejar_la_factura_en_cero_pero_no_en_negativo():
    t = db.totales_de_factura([Decimal("500.00")], db.ITBIS_DE_FACTURA, Decimal("500.00"))
    assert (t["neto"], t["itbis"], t["total"]) == (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))


# ── 2. La puerta: crear y corregir con descuento ────────────────────────────────────────────────────

def test_crear_acepta_el_descuento_y_lo_guarda(mundo):
    f = tgf.crear(descuento="500.00", itbis_pct=18, renglones=[{"descripcion": "Mezcla", "monto": "1000.00"}])
    assert (f["descuento"], f["neto"], f["itbis"], f["total"]) == (
        Decimal("500.00"), Decimal("500.00"), Decimal("90.00"), Decimal("590.00"))
    fila = tgf.filas(mundo, "facturas")[0]
    assert Decimal(str(fila["descuento"])) == Decimal("500.00")
    # y lo que se lee de la base es lo mismo que devolvió el crear
    leido = tgf.leer(f)
    assert (leido["descuento"], leido["neto"], leido["total"]) == (Decimal("500.00"), Decimal("500.00"), Decimal("590.00"))


def test_sin_mandarlo_la_factura_nace_sin_descuento(mundo):
    f = tgf.crear()
    assert (f["descuento"], f["neto"], f["total"]) == (Decimal("0.00"), Decimal("3500.50"), Decimal("3500.50"))


def test_corregir_acepta_el_descuento_y_lo_puede_quitar(mundo):
    f = tgf.crear()
    con = tgf.correr(db.corregir_borrador_de_factura(f["id"], tgf.CASA, f["cambiado_en"], {"descuento": "300"}))
    assert con["descuento"] == Decimal("300.00")
    assert tgf.leer(f)["descuento"] == Decimal("300.00")
    sin = tgf.correr(db.corregir_borrador_de_factura(f["id"], tgf.CASA, con["cambiado_en"], {"descuento": ""}))
    assert sin["descuento"] == Decimal("0.00") and sin["total"] == sin["subtotal"]


def test_la_lista_de_facturas_cuenta_el_descuento(mundo):
    tgf.crear(descuento="500", itbis_pct=18)
    (fila,) = tgf.correr(db.facturas_para_la_lista())
    assert (fila["subtotal"], fila["descuento"], fila["neto"], fila["itbis"], fila["total"]) == (
        Decimal("3500.50"), Decimal("500.00"), Decimal("3000.50"), Decimal("540.09"), Decimal("3540.59"))


@pytest.mark.parametrize("descuento,clave", [
    ("-1", "descuento_formato"), (Decimal("-1"), "descuento_negativo"), ("-0.01", "descuento_formato"),
    ("3500.51", "descuento_mayor"), ("1.234", "descuento_decimales"), ("abc", "descuento_formato"),
    (True, "descuento_tipo"), (1.5, "descuento_tipo"), ("100000000", "descuento_alto"),
])
def test_un_descuento_que_no_vale_no_toca_la_base(mundo, descuento, clave):
    antes = tgf.foto(mundo)
    tgf.no_vale(clave, lambda: tgf.otra(descuento=descuento))
    assert tgf.foto(mundo) == antes


def test_un_descuento_igual_al_subtotal_vale_y_uno_mas_ya_no(mundo):
    f = tgf.crear(descuento="3500.50", itbis_pct=None)
    assert (f["neto"], f["itbis"], f["total"]) == (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
    tgf.no_vale("descuento_mayor", lambda: tgf.otra(descuento="3500.51"))


def test_corregir_unos_renglones_que_dejen_el_descuento_por_encima_no_escribe_nada(mundo):
    f = tgf.crear(descuento="1000")
    antes = tgf.foto(mundo)
    tgf.no_vale("descuento_mayor", lambda: tgf.correr(db.corregir_borrador_de_factura(
        f["id"], tgf.CASA, f["cambiado_en"], {"renglones": [{"descripcion": "Solo uno", "monto": "500.00"}]})))
    assert tgf.foto(mundo) == antes
    assert (tgf.leer(f)["descuento"], len(tgf.leer(f)["renglones"])) == (Decimal("1000.00"), 2)


def test_la_base_no_deja_un_descuento_negativo(mundo):
    f = tgf.crear()
    with pytest.raises(sqlite3.IntegrityError):
        mundo.con.execute("UPDATE facturas SET descuento = -1 WHERE id = ?", (f["id"],))
    mundo.con.rollback()
    assert tgf.leer(f)["descuento"] == Decimal("0.00")


def test_una_factura_emitida_no_se_corrige_ni_para_cambiarle_el_descuento(mundo):
    f = tgf.emitir(tgf.crear())
    with pytest.raises(db.FacturaYaNoSeCorrige):
        tgf.correr(db.corregir_borrador_de_factura(f["id"], tgf.CASA, f["cambiado_en"], {"descuento": "10"}))
    assert tgf.leer(f)["descuento"] == Decimal("0.00")


# ── 3. Desde las sesiones se llena solo ────────────────────────────────────────────────────────────

def test_la_casilla_del_descuento_trae_la_suma_de_los_de_las_sesiones(pantalla, registro):
    _con_cuenta(registro, dict(SESION, descuento=200.0), dict(SESION_B, descuento=300.0))
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "13"], "paso": "escribir"}).text
    assert 'name="descuento" value="500"' in html
    # una sesión sin descuento no estorba
    _con_cuenta(registro, dict(SESION, descuento=None), dict(SESION_B, descuento=300.0))
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "13"], "paso": "escribir"}).text
    assert 'name="descuento" value="300"' in html


def test_sin_descuento_en_las_sesiones_la_casilla_va_vacia(pantalla, registro):
    _con_cuenta(registro, dict(SESION, descuento=0.0))
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert 'name="descuento" value=""' in html


def test_la_factura_guarda_el_descuento_que_trae_la_casilla_y_se_puede_cambiar(pantalla, registro):
    _con_cuenta(registro, dict(DETALLADA, descuento=500.0))
    assert crear_por_la_pantalla(cliente="7", refs=("11",), descuento="500").status_code == 303
    creada = tgf.leer(tgf.filas(pantalla, "facturas")[0]["id"])
    assert (creada["descuento"], creada["subtotal"]) == (Decimal("500.00"), Decimal("62500.00"))
    # la persona lo cambia: manda lo escrito, no lo que traían las sesiones
    f = tgf.crear(descuento="500")
    html = cliente().get(f"/facturas/{f['id']}?corregir=1").text
    assert 'name="descuento" value="500"' in html
    r = cliente().post(f"/facturas/{f['id']}/corregir", data={
        "cambiado_en": fp.candado(tgf.leer(f)["cambiado_en"]), "para_nombre": "Cliente de prueba",
        "fecha": "2026-10-21", "descuento": "125.50",
        "r-1-descripcion": "Ensayo 1", "r-1-cantidad": "1", "r-1-precio": "1000.00",
        "r-2-descripcion": "Ensayo 2", "r-2-cantidad": "1", "r-2-precio": "2500.50"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert tgf.leer(f)["descuento"] == Decimal("125.50")


def test_un_descuento_rechazado_vuelve_escrito_en_las_dos_pantallas(pantalla, registro):
    _con_cuenta(registro, dict(DETALLADA, descuento=500.0))
    r = crear_por_la_pantalla(cliente="7", refs=("11",), descuento="99,999,999,999")
    assert r.status_code == 400 and 'name="descuento" value="99,999,999,999"' in r.text
    assert "data-error-de-guardado" in r.text


def test_natalia_arma_el_descuento_de_las_sesiones_cuando_no_lo_manda(api, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                         "sesiones": [dict(DETALLADA, descuento=500.0), dict(SESION_B, descuento=0.0)]}
    r = crear_api(("11",))
    assert r.status_code == 200, r.text
    f = r.json()["factura"]
    assert (f["subtotal"], f["descuento"], f["itbis"], f["total"]) == (
        "44000.00", "500.00", "0.00", "43500.00")


def test_natalia_respeta_el_descuento_que_manda_y_puede_mandarlo_en_cero(api, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                         "sesiones": [dict(DETALLADA, descuento=500.0), dict(SESION_B, descuento=500.0)]}
    r = crear_api(("11",), descuento="100.00")
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["descuento"] == "100.00"
    r = crear_api(("13",), descuento="0")
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["descuento"] == "0.00"


def test_natalia_con_un_descuento_que_pasa_del_subtotal_no_escribe(api, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                         "sesiones": [dict(DETALLADA, descuento=0.0)]}
    antes = tgf.foto(api)
    r = crear_api(("11",), descuento="44000.01")
    assert r.status_code == 422, (r.status_code, r.text)
    assert tgf.foto(api) == antes


# ── 4. La hoja y el PDF ────────────────────────────────────────────────────────────────────────────

def _factura(descuento=Decimal("500.00"), itbis_pct=Decimal("18.00"), **kw):
    base = {"id": 7, "anio": 2026, "numero": 1, "estado": "emitida", "codigo": "FAC-2026-001",
            "para_nombre": "Cliente de prueba", "rnc_cedula": None, "fecha": "2026-10-14", "ncf": None,
            "ncf_tipo": None, "itbis_pct": itbis_pct, "descuento": descuento, "motivo_anulacion": None,
            "renglones": [
                {"orden": 1, "descripcion": "Mezcla", "detalle": "", "cantidad": Decimal("1"),
                 "unidad": "", "precio_unitario": Decimal("2000.00"), "monto": Decimal("2000.00")}]}
    base.update(kw)
    base.update(db.totales_de_factura([r["monto"] for r in base["renglones"]], base["itbis_pct"], base["descuento"]))
    return base


def test_la_hoja_pone_el_descuento_debajo_del_subtotal_y_el_itbis_sobre_el_neto():
    h = fa.hoja_de_factura(_factura())
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [
        ("Subtotal", "RD$2,000"), ("Descuento", "- RD$500"), ("ITBIS 18%", "RD$270"), ("TOTAL", "RD$1,770")]


def test_sin_descuento_la_hoja_no_tiene_linea_de_descuento():
    h = fa.hoja_de_factura(_factura(descuento=Decimal("0.00")))
    assert [t["etiqueta"] for t in h["totales"]] == ["Subtotal", "ITBIS 18%", "TOTAL"]
    assert h["totales"][2]["total"] == "RD$2,360"


def test_sin_itbis_pero_con_descuento_la_hoja_dice_el_subtotal_el_descuento_y_el_total():
    h = fa.hoja_de_factura(_factura(itbis_pct=None))
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [
        ("Subtotal", "RD$2,000"), ("Descuento", "- RD$500"), ("TOTAL", "RD$1,500")]


def test_sin_descuento_ni_itbis_la_hoja_dice_solo_el_total_como_siempre():
    h = fa.hoja_de_factura(_factura(descuento=Decimal("0"), itbis_pct=None))
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [("TOTAL", "RD$2,000")]


def test_el_pdf_pinta_el_descuento_y_el_itbis_del_neto_en_orden():
    pdf = fa.pdf_de_factura(fa.hoja_de_factura(_factura()))
    leido = re.sub(r"\s+", "", "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages))
    for texto in ("Subtotal", "Descuento", "-RD$500", "ITBIS18%", "RD$270", "TOTAL", "RD$1,770"):
        assert texto in leido, texto
    # «TOTAL» también es el encabezado de la última columna: el de los totales es el ÚLTIMO de la hoja
    assert leido.index("Subtotal") < leido.index("Descuento") < leido.index("ITBIS18%") < leido.rindex("TOTAL")


def test_la_vista_previa_pinta_la_misma_linea_de_descuento():
    h = fa.hoja_de_factura(_factura())
    html = str(__import__("web.app", fromlist=["x"]).plantillas.env.get_template(
        "_hoja_de_factura.html").module.hoja_de_factura(h))
    assert "Descuento" in html and "- RD$500" in html and "ITBIS 18%" in html and "RD$1,770" in html
    # «TOTAL» también es el encabezado de la tabla: el de los totales es el último
    assert html.index("Subtotal") < html.index("Descuento") < html.index("ITBIS 18%") < html.rindex("TOTAL")


def test_la_cotizacion_no_cambia(monkeypatch):
    """Hermana: la cotización sigue con sus dos columnas y su hoja sin descuento."""
    import test_hoja_y_pdf_de_cotizacion as thc
    hoja = cz.hoja_de_cotizacion(thc._cot())
    leido = re.sub(r"\s+", "", "\n".join(p.extract_text() for p in
                                         PdfReader(io.BytesIO(cz.pdf_de_cotizacion(hoja))).pages))
    assert "Descuento" not in leido and "Subtotal" not in leido


# ── 5. El pago (pieza 7) no se rompe: compara el subtotal sin descuento ────────────────────────────

def test_el_no_cuadra_compara_el_subtotal_sin_descuento_con_las_sesiones():
    f = {"estado": "emitida", "itbis_pct": Decimal("18.00"), "subtotal": Decimal("9000.00"),
         "descuento": Decimal("500.00"), "neto": Decimal("8500.00"), "itbis": Decimal("1530.00"),
         "total": Decimal("10030.00")}
    ligas = [{"leida": "ok", "fila": {"total": Decimal("9000.00"), "abonado": Decimal("0"),
                                      "saldo": Decimal("9000.00")}}]
    p = fp.el_pago_de_la_factura(f, ligas)
    assert p["estado"] == "sin_pagos" and "No cuadra" not in p["detalle"]      # el descuento no descuadra nada
    assert "RD$1,530" in p["itbis"]                                            # el ITBIS es el del neto y se dice aparte
    ligas[0]["fila"]["total"] = Decimal("8000.00")                             # la App dice otra cosa: sí avisa
    assert "No cuadra" in fp.el_pago_de_la_factura(f, ligas)["detalle"]


# ── 6. Q1 del testigo de la D2: las dos formas del renglón son 422 ────────────────────────────────

@pytest.mark.parametrize("renglon", [
    {"descripcion": "A", "monto": "10", "cantidad": "2"},              # monto con cantidad, sin precio: se caía callado
    {"descripcion": "A", "monto": "10", "unidad": "horas"},            # monto con unidad, sin precio: se caía callado
    {"descripcion": "A", "monto": "10", "cantidad": "2", "unidad": "horas"},
])
def test_natalia_con_monto_y_cantidad_o_unidad_sin_precio_es_422(api, renglon):
    antes = tgf.foto(api)
    r = crear_api(("11",), renglones=[renglon])
    assert r.status_code == 422, (r.status_code, r.text)
    assert "dos formas" in r.json()["detail"]
    assert tgf.foto(api) == antes


# ── 7. La migración, el esquema, el ensayo y el humo ──────────────────────────────────────────────

def test_la_migracion_agrega_la_columna_con_su_check_y_el_esquema_dice_lo_mismo():
    import test_base_m2 as b2
    import test_cotizaciones_tablas as ct
    texto = ft._texto_migracion_del_descuento()
    sin_comentarios = db._sin_comentarios(texto)
    assert re.search(r"ALTER\s+TABLE\s+facturas\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+descuento\s+NUMERIC\(14,2\)\s+"
                     r"NOT\s+NULL\s+DEFAULT\s+0", sin_comentarios, re.I)
    assert re.search(r"CHECK\s*\(\s*descuento\s*>=\s*0\s*\)", sin_comentarios, re.I)
    assert re.search(r"^\s+descuento\s+NUMERIC\(14,2\)\s+NOT\s+NULL\s+DEFAULT\s+0\s+CHECK\s*\(\s*descuento\s*>=\s*0\s*\)",
                     b2._ddl(ft._ESQUEMA, "facturas")[0], re.M | re.I)
    assert "descuento" in ct._columnas_que_agregan_otras_migraciones_a("facturas", ft._migracion())
    # aditiva e idempotente: solo ALTER … ADD COLUMN IF NOT EXISTS, el CHECK viaja con su columna, y nada más
    for s in ft._sentencias_que_corren(texto):
        if re.match(r"ALTER\s+TABLE", s, re.I):
            assert re.search(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", s, re.I), s
        else:
            assert re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I), s


def test_la_migracion_corre_sobre_lo_de_hoy_y_deja_todo_en_cero():
    con = ft._base()
    f = ft._factura(con)
    info = {c[1]: c for c in con.execute("PRAGMA table_info(facturas)")}
    assert info["descuento"][3] == 1                      # NOT NULL
    assert str(info["descuento"][4]) == "0"               # por omisión 0
    assert con.execute("SELECT descuento FROM facturas WHERE id = ?", (f,)).fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facturas SET descuento = -1 WHERE id = ?", (f,))


def test_el_ensayo_arma_el_sql_sin_conectarse_y_cierra_con_rollback():
    import importlib.util
    from test_grupo_ia import _ROOT
    spec = importlib.util.spec_from_file_location("ensayo_facturas_descuento",
                                                  _ROOT / "tools" / "ensayo_facturas_descuento.py")
    guion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guion)
    texto = ft._texto_migracion_del_descuento()
    ensayo = guion.sql_del_ensayo(texto)
    assert "ROLLBACK;" in ensayo and ensayo.count("COMMIT") == 0
    assert "facturas.descuento" in ensayo and "descuento >= 0" in ensayo
    assert "todas las facturas de hoy quedan en 0" in ensayo
    cuenta = guion.sql_de_la_cuenta(texto)
    assert cuenta.strip().startswith("BEGIN READ ONLY;") and "count(*)" in cuenta


def test_el_humo_lee_la_columna_y_cuenta_los_descuentos_fuera_de_sitio():
    from test_grupo_ia import _ROOT
    fuente = (_ROOT / "tools" / "humo.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_descuento_de_las_facturas")
    cuerpo = ast.unparse(funcion)
    assert "descuento" in cuerpo and "factura_renglones" in cuerpo
    assert "descuento < 0" in cuerpo and "descuento > " in cuerpo
    assert "_descuento_de_las_facturas" in fuente.split("pruebas = [", 1)[1]
