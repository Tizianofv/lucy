"""La hoja de la factura en el estilo del modelo de Tiziano (23-oct-2026).

Lo que se prueba, y por qué:
  · el nombre y el RNC de quien factura NO están escritos en el código: salen de `FACTURA_EMISOR_NOMBRE` y
    `FACTURA_EMISOR_RNC`. Sin ellos no sale ninguna de las dos líneas (no se inventa nada); con ellos, arriba y en
    ese orden, en el PDF y en la vista previa;
  · la factura NO lleva logo: ni la vista previa ni el PDF traen ninguna imagen;
  · el NCF va en rojo debajo del número, no en el bloque Para;
  · las retenciones salen en negativo y el neto a pagar va al final; el total va en la franja negra y el ITBIS y las
    retenciones en gris: el color y la franja se comprueban en la macro (las clases que la hoja pinta) y en el PDF
    (la franja se mide en el dibujo, no en el texto).

Las cifras NO cambian: las de estas pruebas salen de `db.totales_de_factura`, la única cuenta, igual que antes.
"""
from __future__ import annotations

import io
import re

import pytest
from pypdf import PdfReader

import test_hoja_y_pdf_de_factura as thf     # primero: deja puestas las variables de entorno que pide `config`
import cotizaciones as cz
import facturas as fa

NOMBRE = "Estudio de Prueba SRL"
RNC = "101-00000-1"


@pytest.fixture
def sin_emisor(monkeypatch):
    monkeypatch.delenv("FACTURA_EMISOR_NOMBRE", raising=False)
    monkeypatch.delenv("FACTURA_EMISOR_RNC", raising=False)


@pytest.fixture
def con_emisor(monkeypatch):
    monkeypatch.setenv("FACTURA_EMISOR_NOMBRE", NOMBRE)
    monkeypatch.setenv("FACTURA_EMISOR_RNC", RNC)


def _sin_blancos(texto: str) -> str:
    return re.sub(r"\s+", "", texto)


def test_sin_variables_no_sale_ningun_nombre_ni_rnc(sin_emisor):
    h = fa.hoja_de_factura(thf._f())
    assert h["emisor"] == ("", "")
    assert "Estudio" not in " ".join(fa.textos_de_la_factura(h))
    # La primera línea del PDF es el título: no sale ni nombre ni RNC antes.
    assert fa.textos_de_la_factura(h)[0] == "FACTURA DE SERVICIOS"
    assert _sin_blancos(thf._texto_del_pdf(thf._pdf(thf._f()))).startswith("FACTURADESERVICIOS")


def test_con_variables_el_nombre_y_el_rnc_van_arriba_en_el_pdf_y_en_la_vista_previa(con_emisor):
    h = fa.hoja_de_factura(thf._f())
    assert h["emisor"] == (NOMBRE, f"RNC {RNC}")
    texto_del_pdf = _sin_blancos(thf._texto_del_pdf(thf._pdf(thf._f())))
    assert texto_del_pdf.startswith(_sin_blancos(NOMBRE + f"RNC {RNC}" + "FACTURA DE SERVICIOS"))
    assert f'<b>{NOMBRE}</b>' in thf._html(h) and f"<span>RNC {RNC}</span>" in thf._html(h)


def test_la_factura_no_lleva_logo_ni_en_la_vista_previa_ni_en_el_pdf(sin_emisor):
    assert '<img' not in thf._html(fa.hoja_de_factura(thf._f()))
    pagina = PdfReader(io.BytesIO(thf._pdf(thf._f()))).pages[0]
    assert len(pagina.images) == 0


def test_el_ncf_va_debajo_del_numero_y_no_en_el_bloque_para(sin_emisor):
    h = fa.hoja_de_factura(thf._f())
    assert h["ncf"] == "NCF: B0100000001"
    assert not any(etiqueta == "NCF" for etiqueta, _ in h["para"])
    textos = fa.textos_de_la_factura(h)
    assert textos.index(h["ncf"]) == textos.index(h["codigo"]) + 1
    html = thf._html(h)
    assert html.index('class="cod"') < html.index('class="ncf-f"') < html.index('class="para-f"')


def test_sin_ncf_no_hay_linea_de_ncf_en_la_hoja(sin_emisor):
    h = fa.hoja_de_factura(thf._f(ncf=False))
    assert h["ncf"] is None
    assert 'class="ncf-f"' not in thf._html(h)


def test_las_retenciones_van_en_negativo_y_el_neto_va_al_final(sin_emisor):
    h = fa.hoja_de_factura(thf._f())
    etiquetas = [t["etiqueta"] for t in h["totales"]]
    assert etiquetas[-3:] == ["Retención ISR (15%) sobre el servicio", "Retención ITBIS (18%)", "Neto a pagar"]
    assert all(t["total"].startswith("- RD$") for t in h["totales"][-3:-1])
    assert h["totales"][-1]["total"].startswith("RD$")


def test_el_total_va_en_franja_negra_el_itbis_y_las_retenciones_en_gris(sin_emisor):
    html = thf._html(fa.hoja_de_factura(thf._f()))
    assert html.count('class="tot-total"') == 1
    assert html.index('class="tot-gris"') < html.index('class="tot-total"') < html.index('class="tot-neto"')
    # Gris: el subtotal, el ITBIS y las dos retenciones (sin descuento).
    assert html.count('class="tot-gris"') == 4
    assert html.count('class="tot-neto"') == 1


def test_el_pdf_pinta_la_franja_negra_del_total(sin_emisor):
    """La franja se dibuja como un rectángulo relleno de negro: se busca en el contenido de la página."""
    pagina = PdfReader(io.BytesIO(thf._pdf(thf._f()))).pages[0]
    contenido = pagina.get_contents().get_data().decode("latin-1")
    # fpdf escribe los colores con tres decimales: 26/255 sale como 0.102.
    assert re.search(r"0\.10[0-3] 0\.10[0-3] 0\.10[0-3] rg", contenido), \
        "no hay ningún relleno negro (26,26,26) en la página"


def test_el_titulo_y_la_seccion_del_modelo(sin_emisor):
    h = fa.hoja_de_factura(thf._f())
    assert h["titulo"] == "FACTURA DE SERVICIOS" and h["seccion"] == "SERVICIOS"
    assert h["encabezados"] == ("SERVICIO", "CANTIDAD", "PRECIO UNITARIO", "TOTAL")


# ── La ventana (23-oct-2026): «Nueva factura» y «Corregir» como la ventana de las cotizaciones ─────────────────────
#
# Las pruebas de la pantalla (los pasos, las rutas, las validaciones) siguen en sus archivos; acá solo se mira la forma:
# que las dos páginas son la ventana (el velo, el marco, el cuerpo de dos columnas y el pie con sus botones) y que el marco
# que las dibuja es el de `_ventana.html`, el mismo que usa la ventana de cotizaciones.

import test_guardar_factura as tgf                    # noqa: E402
import test_pantalla_de_facturas as tp                # noqa: E402
from test_pantalla_de_facturas import pantalla        # noqa: E402,F401  (la fixture de la pantalla)
from test_guardar_factura import mundo             # noqa: E402,F401  (la base de la pantalla)
from test_pantalla_de_facturas import registro       # noqa: E402,F401  (la App doblada)


def _partes_de_la_ventana(html: str) -> None:
    assert 'class="velo"' in html and 'class="cz-ventana"' in html
    orden = [html.index(f'class="{c}"') for c in ("ventana-enc", "ventana-cuerpo", "form-c", "previa", "ventana-pie")]
    assert orden == sorted(orden), "la ventana va en orden: cabecera, cuerpo (formulario, hoja) y pie"


def test_nueva_factura_es_la_ventana_con_su_pie_y_el_marco_de_las_cotizaciones(pantalla):
    html = tp.cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text
    _partes_de_la_ventana(html)
    pie = html[html.index('class="ventana-pie"'):]
    assert "Guardar como borrador" in pie and ">Cancelar<" in pie
    assert "Así va a salir" in html and 'data-factura-nueva' in html and 'data-hoja' in html
    assert ".cz-ventana{" in html, "el marco viene de _ventana.html, el de las cotizaciones"


def test_corregir_es_la_ventana_con_su_pie_y_la_hoja_al_lado(pantalla):
    b = tgf.crear()
    html = tp.cliente().get(f"/facturas/{b['id']}?corregir=1").text
    _partes_de_la_ventana(html)
    pie = html[html.index('class="ventana-pie"'):]
    assert "Guardar los cambios" in pie and ">Cancelar<" in pie
    assert html.count('<div class="hoja-caja" data-hoja>') == 1      # una sola hoja (el guion la busca aparte)
