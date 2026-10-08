"""El enlace «‹ Inicio» / «Volver al inicio» de la página de Proyectos lleva al INICIO de la App
de registro, no a su última sesión.

Contrato (Levantamientos INFO/registro/la-pagina-de-inicio-y-como-se-llega.md): la dirección de la
App + `/#inicio`, exacto, en el hash, misma pestaña (enlace normal, sin target). Una App que no lo
conozca lo ignora, así que publicar esto antes que ella no rompe nada.

Todo corre por la ruta real `GET /proyectos` y la plantilla real, en las dos sesiones que la ven
(la de la casa y la de «solo ver») y en las mismas consultas que mide `test_proyectos_solo_ver.py`.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
import os
from pathlib import Path

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import config  # noqa: E402
from test_proyectos_solo_ver import (  # noqa: E402  # noqa: F401  (el autouse apaga el conteo de claves malas)
    A_MANO, VISTAS, _pintar, _sin_contar_claves_malas_de_code)

APP = "https://registro.example.test"
RAIZ = Path(__file__).resolve().parent.parent
CONSULTAS = VISTAS + list(A_MANO.values())
IDS = [str(v) for v in VISTAS] + list(A_MANO)


class _Enlaces(HTMLParser):
    def __init__(self):
        super().__init__()
        self.todos = []          # (atributos, texto)
        self._abierto = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._abierto = [dict(attrs), ""]
            self.todos.append(self._abierto)

    def handle_data(self, data):
        if self._abierto is not None:
            self._abierto[1] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._abierto = None


def _enlaces_a_la_app(html: str, app: str) -> list[tuple[dict, str]]:
    p = _Enlaces()
    p.feed(html)
    return [(a, t) for a, t in p.todos if (a.get("href") or "").startswith(app)]


@pytest.mark.parametrize("con", ["casa", "ver"])
@pytest.mark.parametrize("consulta", CONSULTAS, ids=IDS)
def test_todo_enlace_hacia_la_App_es_exactamente_barra_almohadilla_inicio(con, consulta, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", APP)
    enlaces = _enlaces_a_la_app(_pintar(con, **consulta), APP)
    esperados = {"casa": 1, "ver": 2}[con]      # casa: «‹ Inicio»; ver: «‹ Inicio» y «Volver al inicio»
    assert len(enlaces) == esperados, enlaces
    for atributos, _ in enlaces:
        assert atributos["href"] == APP + "/#inicio", atributos
        assert "target" not in atributos, "misma pestaña: el token de sesión vive por pestaña"


@pytest.mark.parametrize("con", ["casa", "ver"])
def test_los_textos_y_el_lugar_no_cambian(con, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", APP)
    html = _pintar(con, p=1)
    assert re.search(r'<a id="btn-inicio" class="btn-fantasma" href="' + re.escape(APP) +
                     r'/#inicio" title="Volver a la página de inicio">‹ Inicio</a>\s*<img class="logo"', html)
    textos = [t for _, t in _enlaces_a_la_app(html, APP)]
    assert textos == (["‹ Inicio"] if con == "casa" else ["‹ Inicio", "Volver al inicio"]), textos


@pytest.mark.parametrize("con", ["casa", "ver"])
@pytest.mark.parametrize("url", ["", "registro.example.test", "ftp://registro.example.test", "//registro.example.test"])
def test_sin_direccion_valida_no_sale_el_enlace(con, url, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", url)
    html = _pintar(con, p=1)
    assert "btn-inicio" not in html and "Volver al inicio" not in html and "#inicio" not in html


@pytest.mark.parametrize("url", ["http://registro.local:8080", "https://registro.example.test/sub"])
def test_la_direccion_de_la_App_es_la_configurada_y_nada_mas(url, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", url)
    assert _enlaces_a_la_app(_pintar("casa", p=1), url)[0][0]["href"] == url + "/#inicio"


def test_censo_quien_pinta_el_enlace_sale_de_lo_real():
    """Las plantillas y rutas que usan `volver_a_la_app`, sacadas del disco: si aparece otra
    página que mande «al inicio de la App», esta prueba se pone roja hasta que se le exija lo mismo."""
    plantillas = {p.name for p in (RAIZ / "web" / "plantillas").glob("*.html")
                  if "volver_a_la_app" in p.read_text(encoding="utf-8")}
    assert plantillas == {"proyectos.html"}
    fuentes = {p.name: p.read_text(encoding="utf-8").count("volver_a_la_app")
               for p in (RAIZ / "web").glob("*.py") if "volver_a_la_app" in p.read_text(encoding="utf-8")}
    assert fuentes == {"app.py": 1}, fuentes
    # y nadie más arma una dirección hacia la App con REGISTRO_URL para un enlace de página
    usos = {p.name for p in (RAIZ / "web").glob("*.py") if "config.REGISTRO_URL" in p.read_text(encoding="utf-8")}
    assert usos == {"app.py"}, usos
