"""Las pantallas del panel, tal como las lista el menú de verdad.

POR QUÉ EXISTE, medido el 9-sep-2026 sobre 399fe2d:

Se publicó `/tareas` —el panel dejó de ser solo de plata— y la descripción de
la herramienta `panel` que lee Lucy siguió diciendo «gastos por mes, lo que
falta clasificar y el detalle». Nada se puso rojo. Consecuencia: quien pedía
«los pendientes» o «las tareas» no tenía por qué recibir el enlace, porque para
el modelo ese panel era de plata.

Arreglarlo escribiendo «Tareas» en el prompt tapa el caso de hoy y deja
plantado el de mañana: una lista tecleada y el menú empiezan a separarse el
mismo día que se escriben. Así que acá NO se teclea ninguna lista. Se lee del
único sitio donde las pantallas existen de verdad —el `<nav>` de
`web/plantillas/base.html`, que es exactamente lo que ve quien abre el panel—.
Una pantalla nueva en el menú llega sola al prompt, sin que nadie se acuerde
de nada, y una que se quita deja de mencionarse igual de sola.

Es el mismo principio que las categorías (`CATEGORIAS` inyectadas en
`cerebro/agente.py`, no copiadas al texto): una sola fuente, y el prompt se
arma desde ella.

Lo que NO se hace acá: adivinar. Si el menú no se puede leer o no tiene
enlaces, esto revienta con el motivo. Un fallback silencioso devolvería una
lista vacía, el prompt quedaría sin pantallas y todo seguiría en verde —que es
justo la forma de fallar que este archivo viene a cerrar.
"""
from __future__ import annotations

import re
from pathlib import Path

# Independiente del directorio de trabajo a propósito: `web/app.py` monta las
# plantillas con una ruta relativa ("web/plantillas") y eso solo funciona si el
# proceso arranca en la raíz. El prompt se arma también desde las pruebas y
# desde tools/, así que acá se ancla al propio archivo.
BASE = Path(__file__).resolve().parent / "plantillas" / "base.html"

_NAV = re.compile(r"<nav\b[^>]*>(.*?)</nav>", re.S | re.I)
_ENLACE = re.compile(r'<a\b[^>]*?\bhref="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_MARCAS = re.compile(r"<[^>]+>")


def pantallas(fuente: str | None = None) -> list[tuple[str, str]]:
    """`[(nombre, ruta), ...]` en el mismo orden en que salen en el menú.

    `fuente` es el HTML ya leído; sirve para probar con un menú fabricado sin
    tocar la plantilla real. Sin argumento lee `web/plantillas/base.html`.
    """
    html = BASE.read_text(encoding="utf-8") if fuente is None else fuente

    bloques = _NAV.findall(html)
    if not bloques:
        raise RuntimeError(
            f"{BASE}: no encontré ningún <nav>. El menú es la lista de "
            "pantallas del panel; sin él no sé qué tiene el panel y no me "
            "invento una lista.")

    salida: list[tuple[str, str]] = []
    for bloque in bloques:
        for ruta, texto in _ENLACE.findall(bloque):
            nombre = " ".join(_MARCAS.sub(" ", texto).split())
            if nombre and ruta.startswith("/"):
                salida.append((nombre, ruta))

    if not salida:
        raise RuntimeError(
            f"{BASE}: el <nav> no tiene ningún enlace con href propio. "
            "Devolver una lista vacía dejaría el prompt sin pantallas y todo "
            "en verde.")
    return salida


def bloque_para_el_prompt(fuente: str | None = None) -> str:
    """Las pantallas como se las decimos al modelo, una por línea.

    El sangrado (tres espacios y `·`) es el de las demás sublistas de
    `HERRAMIENTAS` en `cerebro/agente.py`.
    """
    return "\n".join(f"   · {nombre}  ({ruta})"
                     for nombre, ruta in pantallas(fuente))
