"""La pieza M1 de «Facturas como módulo» (21-oct-2026): el pase de entrada puede caer en
Facturas, y las páginas de Facturas vuelven al inicio de la App con el MISMO «‹ Inicio» que
Proyectos (una sola función arma esa dirección).

Diseño: `IA CDS/disenos/lucy-facturas/DISENO.md`, apartado «Facturas como módulo».

POR EL CAMINO DE PRODUCCIÓN:
  · el pase por la ruta real `/entrar-cds`, con el canje hecho por `httpx` de verdad contra un
    servidor HTTP local (`tests/_app_de_registro.py`, el doble que usan las pruebas del pase);
  · las páginas de Facturas por sus rutas reales y sus plantillas reales, sobre la base real de
    las pruebas en SQLite (`test_guardar_factura.mundo`) y la App y Noco doblados
    (`test_pantalla_de_facturas.pantalla`).
  · las rutas GET de Facturas se sacan de `app.routes`, no de una lista tecleada; la única que
    no pinta página (el PDF) está declarada con su razón y la prueba exige que toda ruta GET
    esté o vigilada o declarada.

FRONTERA, dicha una vez: se miran el HTML y las redirecciones del servidor, no el navegador. La
lista de valores de `?a=` que van a Proyectos es escrita (incluye valores que hoy nadie manda):
un valor nuevo que nadie pensó no está cubierto, pero cualquier valor que no sea el literal
`facturas` cae del lado estricto por construcción (`DESTINOS_DEL_PASE` es una lista cerrada).
"""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

import test_guardar_factura as tgf          # primero: deja puestas las variables de entorno de `config`
from test_guardar_factura import mundo  # noqa: F401  (fixture que usa `pantalla`)
from test_pantalla_de_facturas import cliente as cliente_de_pantalla, pantalla, registro  # noqa: F401
from _app_de_registro import AppDeRegistro

import config
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO
OTRA_DE_LA_CASA = 777001        # alguien que Lucy SÍ conoce (se agrega a la lista en la prueba)
AJENO = 555123                  # alguien que Lucy NO conoce
APP = "https://registro.example.test"


# ── El pase: adónde cae ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app_registro(monkeypatch):
    a = AppDeRegistro()
    monkeypatch.setattr(config, "REGISTRO_URL", a.url)
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, OTRA_DE_LA_CASA))
    yield a
    a.cerrar()


def cliente() -> TestClient:
    # https: la cookie es `Secure` y el cliente solo la reenvía por https, como un navegador.
    return TestClient(panel.app, base_url="https://testserver")


def entrar(c: TestClient, codigo, **extra):
    return c.get("/entrar-cds", params={"c": codigo, **extra}, follow_redirects=False)


def cookies_puestas(r) -> dict:
    salida = {}
    for crudo in r.headers.get_list("set-cookie"):
        nombre, _, resto = crudo.partition("=")
        salida[nombre.strip()] = resto.split(";")[0]
    return salida


@pytest.mark.parametrize("chat", [DUENO, OTRA_DE_LA_CASA], ids=["dueño", "otra de la casa"])
def test_con_la_sesion_de_la_casa_el_pase_cae_en_facturas(app_registro, chat):
    r = entrar(cliente(), app_registro.emitir("total", chat), a="facturas")
    assert r.status_code == 303, r.text[:200]
    assert r.headers["location"] == "/facturas"
    assert list(cookies_puestas(r)) == [panel.COOKIE]          # la misma sesión de siempre


@pytest.mark.parametrize("valor", ["Facturas", "FACTURAS", " facturas", "facturas ", "facturas/",
                                   "/facturas", "https://x", "proyectos", "factura", ""])
def test_solo_el_valor_exacto_cambia_el_destino(app_registro, valor):
    r = entrar(cliente(), app_registro.emitir("total", DUENO), a=valor)
    assert r.status_code == 303, r.text[:200]
    assert r.headers["location"] == "/proyectos", valor


def test_sin_pedir_nada_el_pase_cae_en_proyectos(app_registro):
    r = entrar(cliente(), app_registro.emitir("total", DUENO))
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos"


@pytest.mark.parametrize("nivel,chat", [("ver", DUENO), ("ver", AJENO), ("total", AJENO), ("total", None)],
                         ids=["ver con dueño", "ver ajeno", "total ajeno", "total sin chat"])
def test_quien_entra_solo_a_ver_va_a_proyectos_aunque_pida_facturas(app_registro, nivel, chat):
    """Facturas es solo de la casa: a quien entra a ver no se le deja en una pantalla que le
    contesta 401. La cookie es la de solo ver, como hoy."""
    r = entrar(cliente(), app_registro.emitir(nivel, chat), a="facturas")
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos"
    assert list(cookies_puestas(r)) == [panel.COOKIE_VER]


def test_pedir_facturas_no_cambia_el_rechazo(app_registro):
    """Un boleto que no sirve se rechaza igual con `?a=facturas`: 401, sin cookie y sin destino."""
    c = cliente()
    r = entrar(c, "B" * 43, a="facturas")
    assert r.status_code == 401
    assert r.headers.get_list("set-cookie") == []
    assert "location" not in r.headers and len(c.cookies) == 0


def test_el_destino_sale_de_una_lista_cerrada_y_no_del_texto(app_registro):
    """La dirección de la redirección nunca es lo que llegó: un valor con forma de dirección
    termina en `/proyectos`, y el pedido no se convierte en `Location`."""
    for valor in ("//evil.example", "https://evil.example/facturas", "/\\evil.example"):
        r = entrar(cliente(), app_registro.emitir("total", DUENO), a=valor)
        assert r.headers["location"] == "/proyectos", valor


# ── El «‹ Inicio» de Facturas ──────────────────────────────────────────────────────────────────

# Rutas GET de Facturas que NO pintan una página con barra. La prueba exige que toda ruta GET
# esté vigilada o declarada aquí, con su razón.
RUTAS_QUE_NO_PINTAN_PAGINA = {
    "/facturas/{fid}/pdf": "contesta el PDF de la factura, no una página con barra",
}


def _rutas_get_de_facturas() -> list[str]:
    """Las rutas GET de Facturas, sacadas de lo registrado (no de una lista tecleada)."""
    return sorted({r.path for r in panel.app.routes
                   if getattr(r, "path", "").startswith("/facturas")
                   and "GET" in getattr(r, "methods", set())})


def _href_del_inicio(html: str) -> str | None:
    m = re.search(r'<a id="btn-inicio"[^>]*\bhref="([^"]*)"', html)
    return m.group(1) if m else None


def test_el_censo_de_rutas_get_de_facturas_esta_completo():
    """Para que lo de abajo no sea verde por no mirar nada: toda ruta GET o se vigila o se
    declara, y una declaración que ya no es una ruta se ve (no tapa un cambio)."""
    todas = set(_rutas_get_de_facturas())
    assert "/facturas" in todas, todas                    # la casa del módulo
    declaradas = set(RUTAS_QUE_NO_PINTAN_PAGINA)
    assert declaradas <= todas, declaradas - todas
    assert all(motivo.strip() for motivo in RUTAS_QUE_NO_PINTAN_PAGINA.values())


def test_toda_pagina_de_facturas_lleva_el_inicio_de_la_app(pantalla):
    """Se mira la dirección que la pantalla pinta contra la que dice la configuración VIVA (la
    del doble de la App que puso el fixture): la misma regla que Proyectos."""
    b = tgf.crear()
    casa = cliente_de_pantalla("casa")
    esperado = config.REGISTRO_URL + "/#inicio"
    assert config.REGISTRO_URL.startswith("http://")            # el fixture apunta al doble
    vigiladas = [p for p in _rutas_get_de_facturas() if p not in RUTAS_QUE_NO_PINTAN_PAGINA]
    assert set(vigiladas) | set(RUTAS_QUE_NO_PINTAN_PAGINA) == set(_rutas_get_de_facturas())
    for ruta in vigiladas:
        url = ruta.replace("{fid}", str(b["id"]))
        r = casa.get(url)
        assert r.status_code == 200, (ruta, r.status_code)
        assert _href_del_inicio(r.text) == esperado, ruta
        assert "‹ Inicio" in r.text, ruta
    # «La de corregir» no es una ruta aparte: es la de la factura con `?corregir=1` (el mismo
    # contexto y la misma barra), y también tiene que llevar el Inicio.
    html = casa.get(f"/facturas/{b['id']}?corregir=1").text
    assert _href_del_inicio(html) == esperado


def test_el_inicio_de_facturas_sale_de_la_misma_funcion_que_proyectos(pantalla, monkeypatch):
    """Se reemplaza la función compartida por una que contesta otra cosa: si Facturas armara la
    dirección por su cuenta (copiando la expresión), el enlace no cambiaría."""
    def otra():
        return "https://otro.example/ini"

    monkeypatch.setattr(panel, "enlace_al_inicio_de_la_app", otra)
    b = tgf.crear()
    casa = cliente_de_pantalla("casa")
    for url in ("/facturas", "/facturas/nueva", f"/facturas/{b['id']}"):
        assert _href_del_inicio(casa.get(url).text) == "https://otro.example/ini", url


@pytest.mark.parametrize("url", ["", "registro.example.test", "ftp://registro.example.test",
                                 "//registro.example.test", "/relativa"])
def test_la_regla_de_la_direccion_es_la_de_siempre(url, monkeypatch):
    """La MISMA regla que Proyectos: `REGISTRO_URL` que no empiece por http(s) no da dirección."""
    monkeypatch.setattr(config, "REGISTRO_URL", url)
    assert panel.enlace_al_inicio_de_la_app() == ""


def test_la_direccion_es_la_configurada_y_nada_mas(monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", APP)
    assert panel.enlace_al_inicio_de_la_app() == APP + "/#inicio"


def test_sin_direccion_facturas_no_pinta_el_inicio(pantalla, monkeypatch):
    """La plantilla no dibuja nada cuando la función contesta vacío (con el doble de la App
    intacto: lo que se cambia es el enlace, no la dirección del canje)."""
    monkeypatch.setattr(panel, "enlace_al_inicio_de_la_app", lambda: "")
    b = tgf.crear()
    casa = cliente_de_pantalla("casa")
    for ruta in ("/facturas", "/facturas/nueva", f"/facturas/{b['id']}"):
        html = casa.get(ruta).text
        assert "btn-inicio" not in html and "#inicio" not in html, ruta

