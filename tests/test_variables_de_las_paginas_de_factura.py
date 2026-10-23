"""Cada `var(--x)` que usa el CSS de una página de factura tiene que tener su `--x:` definido en esa misma página
(defecto de producción, 23-oct-2026: el botón «Guardar como borrador» no se veía). Se renderizan las cuatro páginas
de factura que usan la ventana y se mira el texto de sus `<style>`."""
from __future__ import annotations

import re

import test_guardar_factura as _tgf   # primero: variables de entorno
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import pantalla, registro  # noqa: F401  (fixtures)
from test_estilo_de_la_ventana_dentro_de_style import separar
import test_pantalla_de_facturas as tp
import test_guardar_factura as tgf


def _variables_que_faltan(html: str) -> list[str]:
    estilos, _ = separar(html)
    usadas = set(re.findall(r"var\(--([A-Za-z][\w-]*)", estilos))
    definidas = set(re.findall(r"(?<![\w-])--([A-Za-z][\w-]*)\s*:", estilos))
    return sorted(usadas - definidas)


def test_las_paginas_de_factura_definen_las_variables_que_usa_su_ventana(pantalla):
    c = tp.cliente()
    b = tgf.crear()
    paginas = {
        "/facturas/nueva (cliente)": c.get("/facturas/nueva").text,
        "/facturas/nueva?proyecto=1": c.get("/facturas/nueva", params={"proyecto": 1}).text,
        "/facturas/nueva (escribir)": c.get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text,
        "/facturas/{id}": c.get(f"/facturas/{b['id']}").text,
        "/facturas/{id}?corregir=1": c.get(f"/facturas/{b['id']}", params={"corregir": 1}).text,
    }
    for donde, html in paginas.items():
        faltan = _variables_que_faltan(html)
        assert not faltan, f"{donde}: usa var(--…) sin definir en la página: {faltan}"


