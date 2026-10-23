"""El estilo de la ventana (`web/plantillas/_ventana.html`) tiene que llegar a la página que la usa (hallazgo H3 del
testigo, 23-oct-2026: «si alguien quita `{{ estilo_de_la_ventana() }}` de `proyectos.html`, la suite sigue verde»)."""
from __future__ import annotations

import test_cotizaciones_en_la_pagina as tc      # trae `_casa()` (la sesión de la casa)
from test_pagina_proyectos import gente, mundo   # noqa: F401  (fixtures: la base de la página de proyectos)


def test_la_pagina_de_proyectos_trae_el_estilo_de_la_ventana(mundo):
    r = tc._casa().get("/proyectos")
    assert r.status_code == 200
    assert ".cz-ventana{" in r.text and ".velo{" in r.text, "la ventana de cotizaciones perdió su estilo"
