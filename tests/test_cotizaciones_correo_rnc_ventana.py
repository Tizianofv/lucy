"""La ventana de la cotización con el correo y el RNC o cédula (24-oct-2026): las dos casillas nuevas, el valor que se escribió
vuelve escapado tras un rechazo, y un RNC que no vale se rechaza sin escribir. La base, la API y la hoja están en
`test_cotizaciones_correo_rnc.py`. Va en su propio módulo porque el fixture `m` de la página es otro que el de Natalia."""
from __future__ import annotations

import re

import pytest  # noqa: F401

from test_guardar_cotizacion import con  # noqa: F401  (fija el entorno antes de cargar `config`)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures que pide `m`)
from test_cotizaciones_en_la_pagina import m, _casa, _pagina, _formulario, _post_de_la_pagina, _filas  # noqa: F401
from test_cotizaciones_en_la_pagina import _guardar  # noqa: F401

import asyncio

import cotizaciones
import db.db as db


# ── La ventana ───────────────────────────────────────────────────────────────────────────────────

def test_la_ventana_nueva_trae_las_dos_casillas_vacias(m):
    html = _pagina(_casa(), p=1, cotizacion="nueva")
    nombres = [n for _, n, _, _, _ in _formulario(html).casillas]
    assert "para-correo" in nombres and "para-rnc-cedula" in nombres


def test_lo_escrito_con_etiquetas_vuelve_escapado_tras_un_rechazo(m):
    c = _casa()
    datos = _post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion="nueva")), "enviar")
    datos["para-correo"] = "<b>x</b>@ejemplo.do"
    datos["op-0-r-0-total"] = "no es dinero"
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "error=cotizacion_" in r.headers["location"], r.headers["location"]
    html = c.get(r.headers["location"].split("#")[0]).text
    assert "&lt;b&gt;x&lt;/b&gt;@ejemplo.do" in html and "<b>x</b>@ejemplo.do" not in html
    assert _filas(m) == []


def test_un_rnc_que_no_vale_en_la_ventana_se_rechaza_y_no_se_guarda(m):
    c = _casa()
    datos = _post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion="nueva")), "enviar")
    datos["para-rnc-cedula"] = "123"
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "error=cotizacion_para_rnc_cedula_formato" in r.headers["location"], r.headers["location"]
    assert _filas(m) == []


def test_guardar_desde_la_ventana_escribe_el_correo_y_el_rnc(m):
    c = _casa()
    datos = _post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion="nueva")), "enviar")
    datos["para-correo"] = "ana@ejemplo.do"
    datos["para-rnc-cedula"] = "131-23456-7"
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "hecho=cotizacion_enviada" in r.headers["location"], r.headers["location"]
    fila = _filas(m)[0]
    assert (fila["para_correo"], fila["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567")


def test_tras_un_rechazo_la_casilla_del_rnc_y_la_del_correo_quedan_marcadas(m):
    c = _casa()
    datos = _post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion="nueva")), "enviar")
    datos["para-rnc-cedula"] = "123"
    datos["para-correo"] = "a" * 300
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "error=cotizacion_para_correo_largo" in r.headers["location"], r.headers["location"]
    html = c.get(r.headers["location"].split("#")[0]).text
    assert re.search(r'name="para-correo"[^>]*aria-invalid="true"', html), "el correo no quedó marcado"
    # el RNC se valida después del correo en la puerta: con los dos malos sale el primero; se prueba el RNC solo
    datos["para-correo"] = "ana@ejemplo.do"
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert "error=cotizacion_para_rnc_cedula_formato" in r.headers["location"], r.headers["location"]
    html = c.get(r.headers["location"].split("#")[0]).text
    assert re.search(r'name="para-rnc-cedula"[^>]*aria-invalid="true"', html), "el RNC no quedó marcado"


def test_el_correo_y_el_rnc_llegan_por_cada_lectura_del_codigo_y_la_edicion_los_cambia(m):
    """Una formal con correo y RNC: la lista del panel, la lectura de Drive (y su hoja), la ventana para corregir y la
    hoja de la página lo traen; editar cambia el correo y un RNC vacío lo borra. Si alguien quita una columna de un
    SELECT, esta prueba se pone roja en ese camino (la de hermanos lo mira en el SQL)."""
    _guardar(para_correo="ana@ejemplo.do", para_rnc_cedula="131-23456-7")
    cot = asyncio.run(db.cotizaciones_de_proyectos())[1][0]
    assert (cot["para_correo"], cot["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567"), "lista del panel"
    dr = asyncio.run(db.cotizacion_para_drive(1, 1))
    assert (dr["para_correo"], dr["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567"), "lectura de Drive"
    textos = cotizaciones.textos_de_la_hoja(cotizaciones.hoja_de_cotizacion(dr))
    assert "CORREO" in textos and "ana@ejemplo.do" in textos and "131234567" in textos, "hoja de Drive"
    c = _casa()
    html = _pagina(c, p=1, cotizacion="1", corregir=1)
    assert 'value="ana@ejemplo.do"' in html and 'value="131234567"' in html, "ventana para corregir"
    assert "ana@ejemplo.do" in _pagina(c, p=1, cotizacion="1"), "hoja de la página"
    # edición: cambia el correo y borra el RNC
    datos = _post_de_la_pagina(_formulario(html), "borrador")
    datos["para-correo"] = "nuevo@ejemplo.do"
    datos["para-rnc-cedula"] = ""
    r = c.post("/proyectos/1/cotizaciones/1", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "hecho=" in r.headers["location"], r.headers["location"]
    fila = _filas(m)[0]
    assert (fila["para_correo"], fila["para_rnc_cedula"]) == ("nuevo@ejemplo.do", None), "edición"
    assert "nuevo@ejemplo.do" in _pagina(c, p=1, cotizacion="1"), "la página muestra lo editado"
