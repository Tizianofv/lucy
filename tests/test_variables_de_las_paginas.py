"""Cada `var(--x)` que usa el CSS de una página tiene que tener su `--x:` definido en esa misma página (defecto de
producción, 23-oct-2026: «Guardar como borrador» no se veía porque `--color` solo estaba definido en la página de
proyectos; el botón salía con texto blanco sobre fondo transparente).

Se renderizan las páginas que usan la ventana (las cuatro de factura y /proyectos) y se mira el texto de sus `<style>`.
"""
from __future__ import annotations

import re

import test_cotizaciones_en_la_pagina as tc                 # trae `_casa()`
from test_pagina_proyectos import gente, mundo               # noqa: F401  (fixtures de /proyectos)
from test_estilo_de_la_ventana_dentro_de_style import separar


def _variables_que_faltan(html: str) -> list[str]:
    estilos, _ = separar(html)
    usadas = set(re.findall(r"var\(--([A-Za-z][\w-]*)", estilos))
    definidas = set(re.findall(r"(?<![\w-])--([A-Za-z][\w-]*)\s*:", estilos))
    # `--claro` y `--oscuro` no se definen en una hoja: el servidor las escribe en el atributo `style` de cada grupo
    # (proyectos.html, `style="--claro:…;--oscuro:…"`). Son las únicas que se exceptúan, y por esa razón.
    return sorted((usadas - definidas) - {"claro", "oscuro"})


def test_la_pagina_de_proyectos_define_las_variables_que_usa(mundo):
    r = tc._casa().get("/proyectos")
    assert r.status_code == 200
    faltan = _variables_que_faltan(r.text)
    assert not faltan, f"/proyectos: usa var(--…) sin definir: {faltan}"
