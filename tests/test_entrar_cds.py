"""La puerta `/entrar-cds` (entrada a Proyectos desde la App de registro) y la
sesión de «solo ver»: garantías G2-3 a G2-9 del diseño aprobado el 1-oct-2026.
(G2-1, G2-2 y G2-10, las de rutas, están en `test_proyectos_solo_ver.py`.)

POR EL CAMINO DE PRODUCCIÓN: la ruta real, la plantilla real, y el canje lo hace
`httpx` de verdad contra un servidor HTTP local (`_app_de_registro.py`, escrito
desde el contrato que fija la prueba de la App). Nada reemplaza lo que estas
pruebas dicen vigilar: ni la ruta, ni la firma, ni el cliente HTTP.

LAS LISTAS DE ABAJO Y SU FRONTERA: los códigos de forma inválida, las fallas del
canje y los nombres de parámetro con que se intenta cambiar la dirección de la
App son listas escritas a mano. No prueban «todas las formas posibles»; prueban
las que se escribieron, y un código o un nombre que nadie pensó no está cubierto.

Correr:  python3 -m pytest tests/test_entrar_cds.py -q
"""
from __future__ import annotations

import logging
import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import _proyectos_de_prueba as pp  # noqa: E402
from _app_de_registro import AppDeRegistro  # noqa: E402
import config  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO
OTRA_DE_LA_CASA = 777001        # alguien que Lucy SÍ conoce (se agrega a la lista en la prueba)
AJENO = 555123                  # alguien que Lucy NO conoce


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
    """{nombre: valor} de todo `Set-Cookie` de la respuesta."""
    salida = {}
    for crudo in r.headers.get_list("set-cookie"):
        nombre, _, resto = crudo.partition("=")
        salida[nombre.strip()] = resto.split(";")[0]
    return salida


# ═══════════════════════════════════════════════════════════════════════
# G2-3 — qué sesión se da (el techo de la App, el mínimo con lo que Lucy permite)
# ═══════════════════════════════════════════════════════════════════════

CASOS_DE_SESION = [
    # (nivel que dice la App, chat que dice la App, cookie esperada, por qué)
    ("total", DUENO, panel.COOKIE, "total + chat de la lista: la sesión de siempre"),
    ("total", OTRA_DE_LA_CASA, panel.COOKIE, "total + otro chat de la lista"),
    ("total", AJENO, panel.COOKIE_VER, "total pero Lucy no conoce el chat: solo ver"),
    ("total", None, panel.COOKIE_VER, "total sin chat en la ficha: solo ver"),
    ("ver", DUENO, panel.COOKIE_VER, "el techo manda: ver aunque Lucy lo conozca"),
    ("ver", OTRA_DE_LA_CASA, panel.COOKIE_VER, "ver aunque esté en la lista"),
    ("ver", AJENO, panel.COOKIE_VER, "ver y desconocido"),
    ("ver", None, panel.COOKIE_VER, "ver sin chat"),
]


@pytest.mark.parametrize("nivel,chat,cookie,porque", CASOS_DE_SESION,
                         ids=[c[3] for c in CASOS_DE_SESION])
def test_G2_3_la_sesion_es_el_minimo_entre_el_techo_y_lo_que_lucy_permite(
        app_registro, nivel, chat, cookie, porque):
    c = cliente()
    r = entrar(c, app_registro.emitir(nivel, chat))
    assert r.status_code == 303, r.text[:200]
    puestas = cookies_puestas(r)
    assert list(puestas) == [cookie], (porque, puestas)
    token = puestas[cookie]
    if cookie == panel.COOKIE:
        # La de siempre: la firma el mismo `validar` y la deja pasar `puede_entrar`.
        assert auth.puede_entrar(auth.validar(token)) and auth.validar(token) == chat
        assert not auth.validar_ver(token)
    else:
        # La de solo ver: nunca es una sesión de la casa.
        assert auth.validar_ver(token)
        assert auth.validar(token) is None and not auth.puede_entrar(auth.validar(token))


def test_G2_3_un_chat_con_basura_no_da_la_sesion_de_la_casa(app_registro):
    """La App manda el chat como texto; lo que no es un número no vale."""
    for crudo in ("abc", "12 34", "-5", "1e3", "٣٤", "9" * 40):
        a = app_registro.emitir("total", crudo)
        r = entrar(cliente(), a)
        assert list(cookies_puestas(r)) == [panel.COOKIE_VER], (crudo, r.headers)


def test_G2_3_la_decision_vive_en_una_sola_funcion_y_mira_valores(monkeypatch):
    """`sesion_para` es la puerta: se le dan entradas que nadie escribió en los
    casos de arriba (un nivel inventado, un chat que no es número)."""
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO,))
    assert auth.sesion_para("total", DUENO) == auth.SESION_CASA
    for nivel in ("ver", "admin", "TOTAL", "", None, "total ", 1):
        assert auth.sesion_para(nivel, DUENO) == auth.SESION_VER, nivel
    for chat in (None, 0, -1, AJENO, "424242"):     # el texto no es un chat: ya viene convertido
        assert auth.sesion_para("total", chat) == auth.SESION_VER, chat


def test_G2_3_extremo_a_extremo_la_de_la_casa_ve_la_pagina_con_formularios_y_la_de_ver_sin_ellos(
        app_registro):
    with pp.pagina_sin_base():
        c = cliente()
        assert entrar(c, app_registro.emitir("total", DUENO)).status_code == 303
        completa = c.get("/proyectos", params={"p": 1})
        c2 = cliente()
        assert entrar(c2, app_registro.emitir("ver", DUENO)).status_code == 303
        de_ver = c2.get("/proyectos", params={"p": 1})
    assert completa.status_code == de_ver.status_code == 200
    assert 'method="post"' in completa.text
    assert 'method="post"' not in de_ver.text and "Volver al inicio" in de_ver.text


# ═══════════════════════════════════════════════════════════════════════
# G2-4 — la firma de la cookie de ver cubre el nivel y la vida
# ═══════════════════════════════════════════════════════════════════════

def test_G2_4_un_token_de_ver_valido_y_uno_vencido():
    assert auth.validar_ver(auth.crear_token_ver())
    assert not auth.validar_ver(auth.crear_token_ver(vida=-1))
    for nada in (None, "", "ver", "ver.1", "ver.1.2.3", "...", "ver..", "ver.abc.00"):
        assert not auth.validar_ver(nada), nada


def test_G2_4_la_firma_cubre_la_vida_y_la_palabra():
    marca, vence, firma = auth.crear_token_ver().split(".")
    assert not auth.validar_ver(f"{marca}.{int(vence) + 99999}.{firma}")      # vida alargada
    assert not auth.validar_ver(f"VER.{vence}.{firma}")                        # otra palabra
    assert not auth.validar_ver(f"{marca}.{vence}.{'0' * 32}")                 # firma inventada
    assert not auth.validar_ver(f"{marca}.{vence}.{firma[:-1]}")


def test_G2_4_un_token_de_ver_no_se_convierte_en_uno_de_la_casa_ni_al_reves():
    """Ni por el formato ni por la firma: se prueba con los dos sentidos y copiando
    la firma de uno en el otro con todos los chats que se le ocurren a la prueba."""
    ver = auth.crear_token_ver()
    marca, vence, firma = ver.split(".")
    casa = auth.crear_token(DUENO, auth.VIDA_SESION_CDS)
    chat, vence_c, firma_c = casa.split(".")
    assert auth.validar(ver) is None and not auth.puede_entrar(auth.validar(ver))
    assert not auth.validar_ver(casa)
    for quien in (DUENO, OTRA_DE_LA_CASA, 0, 1, AJENO):
        assert auth.validar(f"{quien}.{vence}.{firma}") is None, quien      # firma de ver puesta como casa
    assert not auth.validar_ver(f"{marca}.{vence_c}.{firma_c}")            # firma de casa puesta como ver
    # Y nada de lo firmado para ver sirve firmado «a pelo»: la firma lleva su prefijo.
    assert firma != auth._firmar(f"{marca}.{vence}")


def test_G2_4_la_cookie_de_ver_en_el_lugar_de_la_otra_no_abre_nada(app_registro):
    ver, casa = auth.crear_token_ver(), auth.crear_token(DUENO, auth.VIDA_SESION)
    with pp.pagina_sin_base():
        c = cliente()
        c.cookies.set(panel.COOKIE, ver)                  # ver puesta como casa
        assert c.get("/").status_code == 401
        assert c.get("/proyectos").status_code == 401
        c = cliente()
        c.cookies.set(panel.COOKIE_VER, casa)             # casa puesta como ver
        assert c.get("/proyectos").status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# G2-5 — si el canje falla, 401 y ninguna cookie
# ═══════════════════════════════════════════════════════════════════════

def _forzar(estado, cuerpo=b"", tipo="application/json", demora=0.0):
    return lambda a: setattr(a, "forzada", (estado, cuerpo, tipo, demora))


FALLAS = {
    "la App devuelve 500": _forzar(500, b'{"error": "boom"}'),
    "404 de la App": _forzar(404, b'{"error": "no"}'),
    "200 que no es JSON": _forzar(200, b"<html>hola</html>", "text/html"),
    "200 con una lista": _forzar(200, b'["total"]'),
    "200 con un nivel que no existe": _forzar(
        200, b'{"nivel": "admin", "chat": "424242", "tecnico_id": 1}'),
    "200 sin nivel": _forzar(200, b'{"chat": "424242", "tecnico_id": 1}'),
    "200 con el nivel en otro tipo": _forzar(
        200, b'{"nivel": ["total"], "chat": "424242", "tecnico_id": 1}'),
    "200 con cuerpo gigante": _forzar(
        200, b'{"nivel": "total", "chat": "424242", "x": "' + b"a" * 9000 + b'"}'),
    "redirección": _forzar(302, b"", "text/html"),
}


@pytest.mark.parametrize("falla", list(FALLAS), ids=list(FALLAS))
def test_G2_5_si_el_canje_falla_no_hay_cookie(app_registro, falla):
    FALLAS[falla](app_registro)
    c = cliente()
    r = entrar(c, app_registro.emitir("total", DUENO))
    _es_la_pagina_de_entrar_sin_cookie(r, c)
    assert len(app_registro.pedidos) == 1        # se le preguntó una vez, no se reintentó


def test_G2_5_boleto_que_la_App_no_conoce_o_ya_gasto(app_registro):
    c = cliente()
    _es_la_pagina_de_entrar_sin_cookie(entrar(c, "A" * 43), c)       # forma buena, no existe
    codigo = app_registro.emitir("ver", DUENO)
    assert entrar(c, codigo).status_code == 303
    c2 = cliente()
    _es_la_pagina_de_entrar_sin_cookie(entrar(c2, codigo), c2)       # ya se usó


def test_G2_5_la_App_caida_no_da_cookie(app_registro, monkeypatch):
    app_registro.cerrar()                         # el puerto queda sin nadie escuchando
    c = cliente()
    _es_la_pagina_de_entrar_sin_cookie(entrar(c, "B" * 43), c)


def test_G2_5_la_App_que_tarda_mas_del_tope_no_da_cookie(app_registro, monkeypatch):
    """Tope real, no una promesa: la App contesta BIEN pero tarde, y aun así no entra."""
    monkeypatch.setattr(panel, "TOPE_CANJE_S", 0.4)
    codigo = app_registro.emitir("total", DUENO)
    app_registro.forzada = (200, b'{"nivel": "total", "chat": "424242", "tecnico_id": 1}',
                            "application/json", 2.5)
    c = cliente()
    t0 = time.time()
    r = entrar(c, codigo)
    assert time.time() - t0 < 2.0, "esperó más que el tope"
    _es_la_pagina_de_entrar_sin_cookie(r, c)


@pytest.mark.parametrize("url", ["", "registro.example.com", "ftp://registro.example.com",
                                 "//registro.example.com", "/relativa"])
def test_G2_5_sin_una_direccion_valida_de_la_App_no_se_entra(app_registro, monkeypatch, url):
    monkeypatch.setattr(config, "REGISTRO_URL", url)
    c = cliente()
    _es_la_pagina_de_entrar_sin_cookie(entrar(c, app_registro.emitir("total", DUENO)), c)
    assert app_registro.pedidos == []


def _es_la_pagina_de_entrar_sin_cookie(r, c):
    assert r.status_code == 401, (r.status_code, r.text[:200])
    assert r.headers.get_list("set-cookie") == [], r.headers.get_list("set-cookie")
    assert len(c.cookies) == 0
    assert "location" not in r.headers
    # La página no dice que se guardó, entró ni se hizo nada que no se hizo.
    assert "No se pudo abrir Proyectos desde la App" in r.text
    for mentira in ("guardad", "entraste", "Bienvenid", "listo", "Se creó"):
        assert mentira not in r.text, mentira


# ═══════════════════════════════════════════════════════════════════════
# G2-6 — un código con forma inválida no llega a la App
# ═══════════════════════════════════════════════════════════════════════

BUENA = "k" * 43
CODIGOS_MALOS = {
    "vacío": "",
    "un carácter": "x",
    "42 caracteres": "k" * 42,
    "44 caracteres": "k" * 44,
    "43 buenos y un salto de línea": BUENA + "\n",
    "un salto de línea en el medio": "k" * 20 + "\n" + "k" * 22,
    "43 con un símbolo": "k" * 42 + "!",
    "con barra": "k" * 42 + "/",
    "con punto": "k" * 42 + ".",
    "con espacio": "k" * 42 + " ",
    "con barra de otra dirección": "../" * 14 + "kk",
    "unicode": "ñ" * 43,
    "larguísimo": "k" * 20000,
}


@pytest.mark.parametrize("codigo", list(CODIGOS_MALOS.values()), ids=list(CODIGOS_MALOS))
def test_G2_6_un_codigo_con_forma_invalida_no_llega_a_la_App(app_registro, codigo):
    c = cliente()
    r = entrar(c, codigo)
    assert r.status_code == 401 and r.headers.get_list("set-cookie") == []
    assert app_registro.pedidos == [], "se llamó a la App con un código que no tiene forma de boleto"


def test_G2_6_sin_parametro_no_llega_a_la_App(app_registro):
    r = cliente().get("/entrar-cds", follow_redirects=False)
    assert r.status_code == 401 and app_registro.pedidos == []


def test_G2_6_un_codigo_con_la_forma_buena_si_llega_y_es_el_mismo_que_se_mando(app_registro):
    codigo = app_registro.emitir("ver", DUENO)
    assert entrar(cliente(), codigo).status_code == 303
    (p,) = app_registro.pedidos
    assert p["ruta"] == "/api/pase/canjear"
    import json
    assert json.loads(p["cuerpo"]) == {"codigo": codigo}


# ═══════════════════════════════════════════════════════════════════════
# G2-7 — la dirección de la App sale SOLO de REGISTRO_URL
# ═══════════════════════════════════════════════════════════════════════

NOMBRES_DE_PARAMETRO = ["registro", "registro_url", "url", "u", "r", "app", "destino", "host",
                        "base", "next", "redirect", "to", "server", "origen", "cds", "lucy_url"]
CABECERAS = ["X-Forwarded-Host", "X-Registro-Url", "X-Forwarded-Server", "Origin", "Referer",
             "X-Original-Url", "Forwarded", "X-Host"]


def test_G2_7_nada_del_pedido_cambia_a_quien_se_le_canjea(app_registro):
    ajena = AppDeRegistro()
    try:
        codigo = app_registro.emitir("ver", DUENO)
        ajena.boletos[codigo] = {"nivel": "total", "chat": str(DUENO), "tecnico_id": 1}
        extra = {n: ajena.url for n in NOMBRES_DE_PARAMETRO}
        cab = {h: ajena.url for h in CABECERAS}
        r = cliente().get("/entrar-cds", params={"c": codigo, **extra}, headers=cab,
                          follow_redirects=False)
        assert r.status_code == 303
        assert list(cookies_puestas(r)) == [panel.COOKIE_VER]      # la dijo LA App buena (ver)
        assert ajena.pedidos == [], "Lucy le llevó el boleto a una dirección que vino en el pedido"
        assert len(app_registro.pedidos) == 1
        # Control positivo: la prueba SÍ ve cuando se le llama a esa otra dirección.
        config.REGISTRO_URL = ajena.url
        try:
            entrar(cliente(), codigo)
        finally:
            config.REGISTRO_URL = app_registro.url
        assert len(ajena.pedidos) == 1
    finally:
        ajena.cerrar()


# ═══════════════════════════════════════════════════════════════════════
# G2-8 — la cookie
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("nivel,chat,cookie", [("total", DUENO, panel.COOKIE),
                                               ("ver", DUENO, panel.COOKIE_VER)],
                         ids=["la de la casa", "la de solo ver"])
def test_G2_8_banderas_y_vida_de_la_cookie(app_registro, nivel, chat, cookie):
    r = entrar(cliente(), app_registro.emitir(nivel, chat))
    (crudo,) = r.headers.get_list("set-cookie")
    atributos = [a.strip().lower() for a in crudo.split(";")[1:]]
    nombres = {a.split("=")[0] for a in atributos}
    assert crudo.startswith(cookie + "=")
    assert "httponly" in nombres and "secure" in nombres
    assert "samesite=lax" in atributos
    assert "max-age" not in nombres and "expires" not in nombres, (
        "con `Max-Age` la cookie sobrevive al cierre del navegador; la vida va dentro del token")
    assert "domain" not in nombres
    assert "path=/" in atributos
    # La vida de 12 h está ESCRITA en el token.
    token = crudo.split(";")[0].split("=", 1)[1]
    vence = int(token.split(".")[1])
    assert 12 * 3600 - 30 <= vence - time.time() <= 12 * 3600 + 1
    assert auth.VIDA_SESION_CDS == 12 * 3600


# ═══════════════════════════════════════════════════════════════════════
# G2-9 — el código no se queda en la barra ni en el historial
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("nivel", ["total", "ver"])
def test_G2_9_el_303_va_a_proyectos_a_secas(app_registro, nivel):
    codigo = app_registro.emitir(nivel, DUENO)
    r = entrar(cliente(), codigo)
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos"
    # El boleto no aparece en ninguna parte de la respuesta.
    assert codigo not in r.text
    assert all(codigo not in v for v in r.headers.values())


# ═══════════════════════════════════════════════════════════════════════
# El registro no guarda el boleto ni el chat
# ═══════════════════════════════════════════════════════════════════════

def test_el_registro_de_lucy_no_lleva_el_boleto_ni_el_numero_de_chat(
        app_registro, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    # Números de chat propios de esta prueba, largos a propósito: `CHAT_ID_DUENO`
    # lo fija el primer módulo de prueba que importa `config` (puede ser un número
    # corto) y uno corto aparece por casualidad en cualquier registro (direcciones
    # de memoria, puertos). Medido: así falló una vez de cada ocho corridas.
    casa, ajeno, otra = 7364528190, 6152937408, 8405162739
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO, casa, otra))
    buenos = [app_registro.emitir("total", casa), app_registro.emitir("total", ajeno),
              app_registro.emitir("ver", otra)]
    for b in buenos:
        entrar(cliente(), b)
    entrar(cliente(), "Z" * 43)                       # un boleto que no existe
    app_registro.forzada = (500, b"x", "text/plain", 0)
    entrar(cliente(), "Y" * 43)
    # Lo que registra el servidor. Se quitan los renglones del cliente de PRUEBA
    # (`testserver`), que repite la dirección que él mismo pidió, con el código.
    texto = "\n".join(r.getMessage() for r in caplog.records
                      if "testserver" not in r.getMessage())
    assert "/entrar-cds" in texto, f"la prueba no vio el registro de la puerta: {texto[:600]!r}"
    for secreto in [*buenos, "Z" * 43, "Y" * 43, str(casa), str(ajeno), str(otra)]:
        i = texto.find(secreto)
        assert i < 0, ("el registro lleva un boleto o un número de chat, en: "
                       + repr(texto[max(0, i - 80): i + 80]))
