"""El CSS de la ventana tiene que ir DENTRO de una etiqueta `<style>` (defecto de producción, 23-oct-2026: «cuando le doy
a + para hacer una factura en proyecto lo que me sale son un reguero de letras»). `estilo_de_la_ventana()` devuelve CSS
crudo: si una página lo pone fuera de `<style>`, el navegador lo pinta como texto.

Lo que se exige, en cada página que usa la ventana (`/facturas/nueva` en sus pasos, la de una factura y la de corregir,
y `/proyectos`): que las marcas del CSS de la ventana estén dentro de `<style>` y que NO salgan como texto fuera de él
(el texto de fuera incluye todo el `<body>`, sin `<style>` ni `<script>`).
"""
from __future__ import annotations

from html.parser import HTMLParser

import test_guardar_factura as _tgf   # primero: deja puestas las variables de entorno que pide `config`
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import pantalla, registro  # noqa: F401  (fixtures)

MARCAS_DE_LA_VENTANA = (".velo{", ".cz-ventana{", ".ventana-enc{", ".ventana-cuerpo{", ".form-c{", ".previa{",
                        ".ventana-pie{")


class _Partes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.en_style = 0
        self.en_script = 0
        self.dentro: list[str] = []
        self.fuera: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self.en_style += 1
        elif tag == "script":
            self.en_script += 1

    def handle_endtag(self, tag):
        if tag == "style":
            self.en_style -= 1
        elif tag == "script":
            self.en_script -= 1

    def handle_data(self, data):
        if self.en_style:
            self.dentro.append(data)
        elif not self.en_script:
            self.fuera.append(data)


def separar(html: str) -> tuple[str, str]:
    """(texto dentro de `<style>`, texto que se pinta: todo lo que no es `<style>` ni `<script>`)."""
    partes = _Partes()
    partes.feed(html)
    return "".join(partes.dentro), "".join(partes.fuera)


def assert_ventana_con_su_estilo(html: str, donde: str) -> None:
    dentro, fuera = separar(html)
    for marca in MARCAS_DE_LA_VENTANA:
        assert marca in dentro, f"{donde}: falta «{marca}» dentro de un <style>"
        assert marca not in fuera, f"{donde}: «{marca}» sale como texto fuera de <style> (se pinta como letras)"


def test_el_estilo_de_la_ventana_va_dentro_de_style_en_las_paginas_de_factura(pantalla):
    import test_pantalla_de_facturas as tp
    import test_guardar_factura as tgf
    c = tp.cliente()
    b = tgf.crear()
    paginas = {
        "/facturas/nueva (cliente)": c.get("/facturas/nueva").text,
        "/facturas/nueva?proyecto=1 (cliente, desde un proyecto)": c.get("/facturas/nueva", params={"proyecto": 1}).text,
        "/facturas/nueva (escribir)": c.get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text,
        "/facturas/{id} (una factura)": c.get(f"/facturas/{b['id']}").text,
        "/facturas/{id}?corregir=1": c.get(f"/facturas/{b['id']}", params={"corregir": 1}).text,
    }
    for donde, html in paginas.items():
        assert_ventana_con_su_estilo(html, donde)
