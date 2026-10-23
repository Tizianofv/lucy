"""Emitir desde la ventana de «Nueva factura» y de «Corregir» (23-oct-2026, Tiziano: «Me gustaria que se pueda hacer desde
ahi mismo»).

Lo que se prueba, por la ruta real (`TestClient`, la misma base de prueba que la pantalla de facturas):
  · «Guardar y emitir…» primero PIDE la confirmación dentro de la página y no guarda nada;
  · «Sí, guardar y emitir» guarda y emite en un solo paso, con la misma puerta de «Emitir» (`db.emitir_factura`);
  · si esa puerta NO deja emitir, lo escrito queda guardado como borrador y la ventana lo dice con su razón: nunca
    dice «emitida» si no lo está.
La puerta se prueba con sus propias reglas (la base real de la prueba); en el caso de «no deja» se cambia la puerta por un
doble que se niega, para ver qué dice la ventana cuando eso pasa.
"""
from __future__ import annotations

import re

import pytest

import test_guardar_factura as tgf          # primero: deja puestas las variables de entorno que pide `config`
import test_pantalla_de_facturas as tp
from test_guardar_factura import mundo      # noqa: F401  (fixture)
from test_pantalla_de_facturas import pantalla, registro   # noqa: F401  (fixtures de la pantalla)
import db.db as db
from test_pantalla_de_facturas import unica


def _datos_de_corregir(form: str, **extra) -> dict:
    """Lo que la página de corregir manda, con el candado que la página leyó."""
    datos = {"cambiado_en": re.search(r'name="cambiado_en" value="([^"]+)"', form).group(1),
             "para_nombre": tp.NOMBRE, "fecha": "2026-10-21", "itbis": "18", "ncf": "", "ncf_tipo": "", "rnc_cedula": "",
             "r-1-descripcion": "Grabación de voces", "r-1-detalle": "dos horas", "r-1-cantidad": "1", "r-1-precio": "62,500",
             "r-2-descripcion": "", "r-2-cantidad": "", "r-2-precio": ""}
    datos.update(extra)
    return datos


def test_pedir_emision_no_guarda_nada_y_abre_la_confirmacion_dentro_de_la_ventana(pantalla):
    r = tp.cliente().post("/facturas/nueva", data=tp.cuerpo(accion="pedir_emision"), follow_redirects=False)
    assert r.status_code == 200
    assert "¿Emitir esta factura?" in r.text and 'data-confirmar-emision' in r.text
    assert 'name="accion" value="emitir"' in r.text and 'name="accion" value="volver"' in r.text
    assert tgf.filas(pantalla, "facturas") == []


def test_la_confirmacion_va_despues_del_guardar_para_que_enter_no_emita(pantalla):
    """El botón por omisión del formulario es el primero en el DOM: tiene que ser «Guardar como borrador»."""
    r = tp.cliente().post("/facturas/nueva", data=tp.cuerpo(accion="pedir_emision"), follow_redirects=False)
    pie = r.text[r.text.index('class="ventana-pie"'):]
    assert pie.index("Guardar como borrador") < pie.index('value="emitir"')


def test_sin_pedir_no_aparece_la_confirmacion(pantalla):
    html = tp.cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert "¿Emitir esta factura?" not in html and 'value="emitir"' not in html


def test_guardar_y_emitir_en_un_paso_emite_con_la_puerta_de_siempre(pantalla):
    r = tp.cliente().post("/facturas/nueva", data=tp.cuerpo(accion="emitir"), follow_redirects=False)
    assert r.status_code == 303
    f = unica(pantalla)
    assert f["estado"] == "emitida" and f["numero"] == 1
    assert r.headers["location"] == f"/facturas/{f['id']}"


def test_si_la_puerta_no_deja_emitir_queda_el_borrador_y_la_ventana_lo_dice(pantalla, monkeypatch):
    def la_puerta_se_niega(*_a, **_k):
        raise db.FacturaNoVale("ncf_sin_itbis", "prueba de la puerta")
    monkeypatch.setattr(db, "emitir_factura", la_puerta_se_niega)
    r = tp.cliente().post("/facturas/nueva", data=tp.cuerpo(accion="emitir"), follow_redirects=False)
    assert r.status_code == 400
    assert "No se emitió: una factura con comprobante (NCF) lleva ITBIS." in r.text
    assert "quedó guardado como borrador" in r.text
    assert unica(pantalla)["estado"] == "borrador"
    assert 'data-confirmar-emision' not in r.text


def test_corregir_pedir_emision_no_cambia_el_borrador(pantalla):
    b = tgf.crear()
    antes = unica(pantalla)["para_nombre"]
    form = tp._pagina(b["id"], corregir=1)
    r = tp.cliente().post(f"/facturas/{b['id']}/corregir", data=_datos_de_corregir(form, accion="pedir_emision",
                                                                                   para_nombre="Nombre Nuevo"))
    assert r.status_code == 200 and "¿Emitir esta factura?" in r.text and 'data-confirmar-emision' in r.text
    assert unica(pantalla)["para_nombre"] == antes


def test_corregir_guardar_y_emitir_corrige_y_emite_en_un_paso(pantalla):
    b = tgf.crear()
    form = tp._pagina(b["id"], corregir=1)
    r = tp.cliente().post(f"/facturas/{b['id']}/corregir", data=_datos_de_corregir(form, accion="emitir",
                                                                                   para_nombre="Nombre Nuevo"),
                          follow_redirects=False)
    assert r.status_code == 303
    f = unica(pantalla)
    assert (f["estado"], f["numero"], f["para_nombre"]) == ("emitida", 1, "Nombre Nuevo")


def test_corregir_con_la_puerta_negada_deja_el_borrador_corregido_y_lo_dice(pantalla, monkeypatch):
    b = tgf.crear()
    form = tp._pagina(b["id"], corregir=1)

    def la_puerta_se_niega(*_a, **_k):
        raise db.FacturaNoVale("ncf_sin_itbis", "prueba de la puerta")
    monkeypatch.setattr(db, "emitir_factura", la_puerta_se_niega)
    r = tp.cliente().post(f"/facturas/{b['id']}/corregir", data=_datos_de_corregir(form, accion="emitir",
                                                                                   para_nombre="Nombre Nuevo"),
                          follow_redirects=False)
    assert r.status_code == 400 and "No se emitió: una factura con comprobante (NCF) lleva ITBIS." in r.text
    f = unica(pantalla)
    assert (f["estado"], f["para_nombre"]) == ("borrador", "Nombre Nuevo")


def test_las_dos_ventanas_de_factura_traen_el_estilo_de_la_ventana(pantalla):
    nueva = tp.cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert ".cz-ventana{" in nueva and ".velo{" in nueva
    b = tgf.crear()
    corregir = tp._pagina(b["id"], corregir=1)
    assert ".cz-ventana{" in corregir and ".velo{" in corregir
