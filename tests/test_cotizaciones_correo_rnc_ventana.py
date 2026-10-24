"""La ventana de la cotización con el correo y el RNC o cédula (24-oct-2026): las dos casillas nuevas, el valor que se escribió
vuelve escapado tras un rechazo, y un RNC que no vale se rechaza sin escribir. La base, la API y la hoja están en
`test_cotizaciones_correo_rnc.py`. Va en su propio módulo porque el fixture `m` de la página es otro que el de Natalia."""
from __future__ import annotations

import pytest  # noqa: F401

from test_guardar_cotizacion import con  # noqa: F401  (fija el entorno antes de cargar `config`)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures que pide `m`)
from test_cotizaciones_en_la_pagina import m, _casa, _pagina, _formulario, _post_de_la_pagina, _filas  # noqa: F401


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
