"""El aviso fijo de «tocar algo sin que la página se mueva» NO TAPA la barra de arriba (14-oct-2026).

Antes el aviso nacía en `inset:0 0 auto 0` con `z-index:9999`: se pintaba ENCIMA del menú hasta que la persona
lo cerraba. Ahora nace justo debajo de la barra (`bajoLaBarraSinSaltar`, en `_marcar_sin_saltar.html`).
Lo que se vigila:
  1. las pantallas reales (Proyectos de la casa, lista de Tareas, detalle de una tarea) tienen
     su `<header>` como PRIMER `<header>` y esa barra queda pegada arriba (`position:sticky`), que es lo que hace
     que "debajo de la barra" sea siempre la misma altura;
  2. corrido el guion REAL de la página (JavaScriptCore, navegador de mentira de `tests/_sin_saltar.py`), el aviso
     que sale de un guardado sin confirmar empieza donde termina la barra, y sin barra empieza arriba.
FRONTERA: el navegador de mentira no pinta; la altura de la barra la da el `getBoundingClientRect` que se le pone
aquí. Que de verdad se vea debajo se mira en un navegador."""
from __future__ import annotations

import re

from _sin_saltar import correr_en_navegador
from test_escrituras_proyecto import _script_de_la_pagina, hay_osascript
from test_marcar_sin_saltar import _marcar, _pagina
from test_tareas_sin_saltar import _scripts_de_tareas, tareas  # noqa: F401
from test_pagina_proyectos import gente, mundo  # noqa: F401
from test_tareas_sin_saltar import galleta  # noqa: F401

BARRA = ('document.querySelector = function (s) { return s === "header" ? '
         '{getBoundingClientRect: function () { return {bottom: 64.4}; }} : null; };')
SIN_BARRA = 'document.querySelector = function () { return null; };'
ESTILO = "JSON.stringify({estilo: avisosPuestos.length ? avisosPuestos[0].atributos.style : null})"


def _estilo_del_aviso(script: str, preparacion: str) -> str:
    r = correr_en_navegador(script, "var __e = null;\n" + preparacion + _marcar("sinRed(0);") + "\n" + ESTILO)
    assert r["estilo"], "no salió el aviso"
    return r["estilo"]


def _pegado_arriba(html: str) -> bool:
    """El PRIMER `<header>` de la página, con el CSS que de verdad le toca (su clase o el selector `header`)."""
    h = re.search(r"<header\b([^>]*)>", html)
    assert h, "la pantalla no tiene <header>"
    clases = re.search(r'class="([^"]*)"', h.group(1))
    selectores = ["header"] + ["." + c for c in (clases.group(1).split() if clases else [])]
    css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    for sel in selectores:
        for regla in re.finditer(r"(?:^|[}\s])" + re.escape(sel) + r"\s*\{([^}]*)\}", css):
            if re.search(r"position:\s*sticky", regla.group(1)):
                return True
    return False


@hay_osascript
def test_el_aviso_empieza_debajo_de_la_barra_en_proyectos(mundo):
    estilo = _estilo_del_aviso(_script_de_la_pagina(mundo), BARRA)
    assert "inset:64px 0 auto 0" in estilo and "position:fixed" in estilo, estilo
    assert "inset:0 0 auto 0" not in estilo


@hay_osascript
def test_sin_barra_el_aviso_sigue_arriba_del_todo(mundo):
    assert "inset:0px 0 auto 0" in _estilo_del_aviso(_script_de_la_pagina(mundo), SIN_BARRA)


@hay_osascript
def test_el_aviso_empieza_debajo_de_la_barra_en_las_dos_pantallas_de_tareas(tareas, mundo):
    for nombre, guion in _scripts_de_tareas(tareas).items():
        assert "inset:64px 0 auto 0" in _estilo_del_aviso(guion, BARRA), nombre


def test_las_pantallas_reales_tienen_su_barra_pegada_arriba(tareas, mundo):
    paginas = {"proyectos": _pagina(p=2), "tareas": tareas.cliente.get("/tareas").text,
               "detalle": tareas.cliente.get("/tareas/1").text}
    for nombre, html in paginas.items():
        assert _pegado_arriba(html), nombre
        assert "bajoLaBarraSinSaltar" in html, nombre            # la pieza única llegó a esa pantalla
