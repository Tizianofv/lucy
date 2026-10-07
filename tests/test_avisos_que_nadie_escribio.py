"""Segunda vuelta de la puerta de los avisos (`web/avisos.py`): el testigo dio NO PASA sobre `e8ff32f`.

Tres cosas, todas reproducidas con peticiones reales:
  1. UN AVISO VERDADERO SE REPETÍA EN OTRA ACCIÓN. `/movimientos` armaba el `volver` de sus formularios con
     la dirección de la visita, aviso incluido; el siguiente POST lo copiaba a su redirección y se firmaba.
  2. SE FIRMABA UN TEXTO ESCRITO POR LA PERSONA. `/borrar` con un `volver` a mano y un id que no vale
     redirigía a ese `volver` con su aviso y le daba recibo.
  3. UNA COOKIE HOSTIL TUMBABA LA PÁGINA Y EL GUARDADO (`int()` de miles de dígitos).

LA PREMISA CORREGIDA: una redirección puede traer parámetros de aviso que la ruta NO decidió (el destino que
llega en un campo del formulario, una cabecera, la dirección del POST). Ahora la puerta, en un solo sitio, quita
del `Location` lo que venía DENTRO de un valor de la petición y firma solo el resto; y nada que pase dentro de la
puerta llega a la persona como error. Estas pruebas recorren TODAS las rutas que redirigen (sacadas de la app).

FRONTERA: las rutas se recorren con los mundos de prueba de `test_avisos_verdad.py` (SQLite y dobles); una ruta
que sin un mundo adecuado se rechaza antes de redirigir cuenta como «no redirigió» (el conteo mínimo de
redirecciones por mundo lo dice); un aviso copiado de `Referer` NO lo quita la puerta (el navegador lo manda solo)
y por eso el recorrido le pone un `Referer` hostil a cada ruta y exige que ninguna lo copie.
"""
import random
import re
import time
import types
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

import test_grupos as tg  # noqa: F401  (pone el entorno antes de importar `config`)
from test_grupos import base  # noqa: F401
from test_pagina_proyectos import gente, mundo  # noqa: F401
from test_papelera_de_proyectos import pap  # noqa: F401
from test_avisos_verdad import (_app_minima, _con_reloj, _dice, _mundo_casa, _mundo_dinero, _mundo_tareas,
                                _rutas_get, de_aviso, parametros, textos)
import _tareas_de_prueba as TP
import web.app as panel
import web.avisos as avisos
from web.avisos import Aviso, Navegacion, PuertaDeAvisos

SENTINELA = "/movimientos?guardados=987654&hecho=zzcentinela&error=zzcentinela&efectivo=987655&borrado=987656"
HUELLAS = ("987654", "987655", "987656", "zzcentinela")


def _cliente_dinero(m):
    c = TestClient(panel.app, follow_redirects=False, raise_server_exceptions=False)
    c.cookies.set(panel.COOKIE, TP.galleta())
    return c


# ═══════════════════════════════════════════════════════════════════════
# 1 y 2. El aviso heredado no viaja; el que escribió la persona no se firma
# ═══════════════════════════════════════════════════════════════════════

def test_guardar_una_categoria_y_luego_borrar_no_repite_el_guardado(monkeypatch):
    esc = _mundo_dinero(monkeypatch)
    c, m = _cliente_dinero(esc), esc.algo
    r = c.post("/categorias", data={"cat_7": "Seguros", "prev_7": "", "volver": "/movimientos"})
    assert r.headers["location"] == "/movimientos?guardados=1" and "lucy_aviso=" in r.headers["set-cookie"]
    pagina = c.get(r.headers["location"])
    assert any(t.startswith("Guardado 1") for t in textos(pagina.text)) and pagina.headers["cache-control"] == "no-store"
    # el campo oculto de la página no carga el aviso de esta visita
    volver = re.findall(r'name="volver" value="([^"]*)"', pagina.text)
    assert volver and all("guardados" not in v for v in volver), volver
    # el POST siguiente (con el `volver` que la página ofrecía) solo dice lo suyo
    r2 = c.post("/borrar", data={"movimiento_id": "7", "volver": volver[0]})
    assert r2.headers["location"].endswith("borrado=1") and "guardados" not in r2.headers["location"]
    dicho = textos(c.get(r2.headers["location"]).text)
    assert any("A la papelera" in t for t in dicho) and not any("Guardado" in t for t in dicho), dicho
    assert m.papelera == [7]


def test_aun_con_un_volver_sucio_el_aviso_heredado_no_viaja(monkeypatch):
    """La puerta lo resuelve para todas las rutas: aunque el campo `volver` traiga un aviso (de otra página o
    escrito a mano), el destino sale sin él y la página no lo dice."""
    esc = _mundo_dinero(monkeypatch)
    c = _cliente_dinero(esc)
    r = c.post("/borrar", data={"movimiento_id": "7", "volver": "/movimientos?guardados=1&desde=2026-08-01"})
    assert r.headers["location"] == "/movimientos?desde=2026-08-01&borrado=1"
    assert "Guardado" not in " ".join(textos(c.get(r.headers["location"]).text))


def test_un_post_con_un_id_que_no_vale_y_un_volver_con_aviso_no_firma_el_aviso_de_la_persona(monkeypatch):
    esc = _mundo_dinero(monkeypatch)
    c = _cliente_dinero(esc)
    r = c.post("/borrar", data={"movimiento_id": "abc", "volver": "/movimientos?guardados=99"})
    assert r.status_code == 303 and r.headers["location"] == "/movimientos"
    assert "set-cookie" not in r.headers
    assert not any("Guardado" in t for t in textos(c.get("/movimientos").text))
    # el mismo ataque por la dirección del POST o por otro campo, en `/efectivo` y `/categorias`
    for ruta, datos in (("/efectivo", {"concepto": "", "monto": "1", "fecha": "2026-08-04"}),
                        ("/categorias", {"cat_7": "Seguros", "prev_7": ""})):
        r = c.post(ruta, data={**datos, "volver": "/movimientos?efectivo=5&guardados=99"})
        assert "efectivo=5" not in r.headers["location"] and "guardados=99" not in r.headers["location"], ruta


def test_un_campo_que_se_llama_como_un_aviso_es_el_argumento_de_la_accion_y_su_aviso_se_conserva(pap):
    """Restaurar manda `id=1` en el formulario y la ruta lo repite como aviso (`hecho=…&id=1`): el recibo debe
    amparar ese aviso, que la acción sí produjo."""
    esc = _mundo_casa(pap)
    from acciones import crud
    from test_borrar_proyecto_y_grupo import _corre
    _corre(crud.borrar("proyectos", 2, "x", actor="panel"))
    r = esc.cliente.post("/papelera/restaurar", data={"tabla": "proyectos", "id": 2}, follow_redirects=False)
    assert "id=2" in r.headers["location"] and "lucy_aviso=" in r.headers["set-cookie"]


def test_la_referencia_del_navegador_no_tapa_un_aviso_de_verdad(monkeypatch):
    """El navegador manda `Referer` con la dirección de la página anterior (con su aviso gastado): si contara
    como «lo que trajo la petición», el aviso de la acción siguiente, idéntico, se perdería."""
    esc = _mundo_dinero(monkeypatch)
    c = _cliente_dinero(esc)
    r = c.post("/categorias", data={"cat_7": "Seguros", "prev_7": "", "volver": "/sin-clasificar"},
               headers={"referer": "http://x.test/sin-clasificar?guardados=1"})
    assert r.headers["location"] == "/sin-clasificar?guardados=1" and "lucy_aviso=" in r.headers["set-cookie"]
    assert any(t.startswith("Guardado 1") for t in textos(c.get(r.headers["location"]).text))


def _aplicacion_con_destinos():
    """Una aplicación mínima con las tres formas en que algo llega a una redirección sin que la ruta lo decida."""
    app = _app_minima()

    @app.post("/copia-formulario")
    async def copia_formulario(request: Request):
        f = await request.form()
        return RedirectResponse(str(f.get("siguiente", "/x/1")), status_code=303)

    @app.post("/copia-direccion")
    async def copia_direccion(siguiente: str = "/x/1"):
        return RedirectResponse(siguiente, status_code=303)

    @app.post("/copia-cabecera")
    async def copia_cabecera(request: Request):
        return RedirectResponse(request.headers.get("x-destino", "/x/1"), status_code=303)

    @app.post("/repite-su-argumento")
    async def repite_su_argumento(request: Request):
        f = await request.form()
        return RedirectResponse(f"/x/1?cuenta={f.get('cuenta')}&hecho=listo", status_code=303)
    return app


def test_la_puerta_quita_de_cualquier_destino_dado_por_fuera_el_aviso_y_no_lo_firma():
    c = TestClient(_aplicacion_con_destinos(), follow_redirects=False)
    sucio = "/x/1?p=3&hecho=viejo&cuenta=9"
    for nombre, r in (
            ("formulario", c.post("/copia-formulario", data={"siguiente": sucio})),
            ("dirección del POST", c.post("/copia-direccion", params={"siguiente": sucio})),
            ("cabecera", c.post("/copia-cabecera", headers={"x-destino": sucio}))):
        assert r.status_code == 303, nombre
        assert r.headers["location"] == "/x/1?p=3", (nombre, r.headers["location"])
        assert "set-cookie" not in r.headers, nombre
    # la navegación del destino sí viaja; el aviso de la acción de verdad sigue con su recibo
    r = c.post("/repite-su-argumento", data={"cuenta": "2"})
    assert "cuenta=2" in r.headers["location"] and "lucy_aviso=" in r.headers["set-cookie"]


def test_un_cuerpo_demasiado_grande_no_se_revisa_y_no_deja_recibo(monkeypatch):
    monkeypatch.setattr(avisos, "LIMITE_DE_CUERPO", 50)
    c = TestClient(_aplicacion_con_destinos(), follow_redirects=False)
    r = c.post("/repite-su-argumento", data={"cuenta": "2", "relleno": "x" * 200})
    assert r.status_code == 303 and "set-cookie" not in r.headers and "cuenta" not in r.headers["location"]


# ═══════════════════════════════════════════════════════════════════════
# 1 y 2, el recorrido de TODAS las rutas que redirigen
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(params=["casa", "tareas", "dinero"])
def mundo_de_rutas(request, monkeypatch):
    if request.param == "casa":
        esc = _mundo_casa(request.getfixturevalue("pap"))
    else:
        esc = {"tareas": _mundo_tareas, "dinero": _mundo_dinero}[request.param](monkeypatch)
    return esc


def _rutas_que_escriben():
    return [r for r in panel.app.router.routes
            if getattr(r, "methods", None) and "GET" not in r.methods and hasattr(r, "endpoint")]


def _nombres_de_campo(ruta) -> set[str]:
    import inspect
    fuente = inspect.getsource(ruta.endpoint)
    return set(re.findall(r"""["']([a-z][a-z_0-9]{1,30})["']""", fuente)) | {"volver", "siguiente", "destino"}


def recorrido(esc, solo_referencia=False):
    """POST a toda ruta que no es GET de la aplicación, con un destino con avisos metido en CADA campo que su
    código nombra, en la dirección, en `Referer` y como campos que se llaman como avisos. Devuelve (cuántas
    redirigieron, [(ruta, Location, cookie)] de las que dejaron huella de lo metido)."""
    nombres_de_aviso = {n for r in _rutas_get(panel.app) for n in de_aviso(r)}
    c = TestClient(panel.app, follow_redirects=False, raise_server_exceptions=False)
    c.cookies.set(panel.COOKIE, TP.galleta())
    redirigieron, fugas = 0, []
    for ruta in _rutas_que_escriben():
        if ruta.path.startswith("/api/code"):
            continue
        camino = re.sub(r"\{\w+\}", "1", ruta.path)
        campos = {n: SENTINELA for n in _nombres_de_campo(ruta)}
        campos.update({n: "987654" for n in nombres_de_aviso})
        if solo_referencia:                  # la frontera: lo único que la puerta no quita
            r = c.post(camino, headers={"referer": "http://x.test" + SENTINELA})
        else:
            r = c.post(camino, params={"siguiente": SENTINELA, **{n: "987654" for n in nombres_de_aviso}}, data=campos,
                       headers={"referer": "http://x.test" + SENTINELA, "x-destino": SENTINELA})
        if 300 <= r.status_code < 400:
            redirigieron += 1
            donde = r.headers.get("location", "")
            destino = urlsplit(donde)
            de_aviso_ahi = avisos.avisos_de(avisos._ruta_get(panel.app, destino.path))
            if any(k in de_aviso_ahi and v in HUELLAS for k, v in parse_qsl(destino.query, keep_blank_values=True)):
                fugas.append((ruta.path, donde, r.headers.get("set-cookie")))
    return redirigieron, fugas


def test_ninguna_ruta_que_redirige_repite_ni_firma_un_aviso_que_le_dieron(mundo_de_rutas):
    redirigieron, fugas = recorrido(mundo_de_rutas)
    assert fugas == [], fugas
    assert redirigieron >= 5, (mundo_de_rutas.nombre, redirigieron)         # el recorrido no es un vacío


def test_el_recorrido_ve_una_ruta_que_copia_el_aviso_de_la_referencia(monkeypatch):
    """La frontera dicha en el docstring: la cabecera `Referer` la copia una ruta y la puerta no la quita; el
    recorrido SÍ lo ve (así una ruta nueva que lo haga pone roja la suite)."""
    esc = _mundo_dinero(monkeypatch)

    @panel.app.post("/zz-copia-referer")
    async def copia_referer(request: Request):
        ref = urlsplit(request.headers.get("referer", "/movimientos"))
        return RedirectResponse(ref.path + "?" + ref.query, status_code=303)
    try:
        _, fugas = recorrido(esc, solo_referencia=True)
    finally:
        panel.app.router.routes[:] = [r for r in panel.app.router.routes if getattr(r, "path", "") != "/zz-copia-referer"]
    assert [f[0] for f in fugas] == ["/zz-copia-referer"]


# ═══════════════════════════════════════════════════════════════════════
# 3. La puerta nunca tumba una página ni un guardado
# ═══════════════════════════════════════════════════════════════════════

def _cookies_hostiles() -> dict[str, str]:
    casos = {"vacia": "", "tildes": "~~~~", "sin_punto": "abc", "digitos_unicode": "²9.abc", "sup3": "³.abc",
             "arabe": "٣.abc", "enorme_digitos": "9" * 5000 + ".abc", "cuatro_mil_digitos": "9" * 4000 + ".abc", "gigante": "x" * 15000, "negativa": "-5.abc",
             "mixta": "1.a~²9.b~99999999999999999999.c", "solo_puntos": "....", "nulos": "1\x00.a\x00",
             "flotante": "1e5.abc", "muchos": "~".join(f"{10**11 + i}.{'a' * 32}" for i in range(400)),
             "no_ascii": "9999999999.é", "cabeza_larga": "1" * 13 + "." + "a" * 32, "firma_larga": "9999999999." + "a" * 5000}
    azar = random.Random(7)
    alfabeto = "0123456789.~abcdef-+ ²٣é\x01\"';="
    for i in range(40):                                              # inventadas, que nadie clasificó
        casos[f"azar{i}"] = "".join(azar.choice(alfabeto) for _ in range(azar.randint(1, 300)))
    return casos


def _pedir(c, metodo, ruta, galleta, **kw):
    cabecera = f"{panel.COOKIE}={TP.galleta()}; lucy_aviso={galleta}".encode("latin-1", "replace")
    return c.request(metodo, ruta, headers={"cookie": cabecera}, **kw)


@pytest.mark.parametrize("nombre", sorted(_cookies_hostiles()))
def test_una_cookie_hostil_no_tumba_ni_la_pagina_ni_el_guardado(monkeypatch, nombre):
    """Y se descarta ANTES de usarse: ni siquiera hace falta que la puerta contenga una excepción (dos
    capas: la cookie hostil no llega a `int()`, y si algo llegara a fallar, ver la prueba de roturas)."""
    esc = _mundo_dinero(monkeypatch)
    contenidas = []                                   # las excepciones que la puerta tuvo que contener
    monkeypatch.setattr(avisos.log, "warning", lambda *a, **k: contenidas.append(a))
    c = TestClient(panel.app, follow_redirects=False, raise_server_exceptions=False)
    galleta = _cookies_hostiles()[nombre]
    r = _pedir(c, "GET", "/movimientos?guardados=3", galleta)
    assert r.status_code == 200 and "Guardados 3" not in r.text, (nombre, r.status_code)
    antes = list(esc.algo.papelera)
    r = _pedir(c, "POST", "/borrar", galleta, data={"movimiento_id": "7", "volver": "/movimientos"})
    assert r.status_code == 303 and esc.algo.papelera == antes + [7], (nombre, r.status_code)
    assert contenidas == [], (nombre, contenidas)


ROTURAS = ["_recibos_de", "_recibo", "avisos_de", "_ruta_get", "_cookie_de", "_poner_cookie", "clave_de",
           "_valores_que_trajo_la_peticion", "_viene_de_fuera", "consulta_sin_avisos"]


@pytest.mark.parametrize("pieza", ROTURAS)
def test_si_algo_dentro_de_la_puerta_falla_no_se_ve_un_error_y_lo_guardado_sigue_guardado(monkeypatch, pieza):
    esc = _mundo_dinero(monkeypatch)
    c = TestClient(panel.app, follow_redirects=False, raise_server_exceptions=False)
    c.cookies.set(panel.COOKIE, TP.galleta())
    # un recibo bueno primero, para que la entrada tenga algo que gastar
    r = c.post("/categorias", data={"cat_7": "Seguros", "prev_7": "", "volver": "/movimientos"})
    destino = r.headers["location"]
    assert pieza in vars(avisos), pieza

    def roto(*a, **k):
        raise RuntimeError("falla inventada dentro de la puerta")
    monkeypatch.setattr(avisos, pieza, roto)
    pagina = c.get(destino)
    assert pagina.status_code == 200, (pieza, pagina.status_code)
    antes = list(esc.algo.papelera)
    r2 = c.post("/borrar", data={"movimiento_id": "7", "volver": "/movimientos"})
    assert r2.status_code == 303 and esc.algo.papelera == antes + [7], (pieza, r2.status_code)


def test_si_la_puerta_falla_al_entrar_no_se_dice_nada_que_no_se_pueda_probar(monkeypatch):
    """Falla hacia «sin avisos», nunca hacia «con avisos»: con la lectura de recibos rota, una dirección que
    traía aviso (con o sin recibo) sale limpia."""
    esc = _mundo_dinero(monkeypatch)
    c = TestClient(panel.app, follow_redirects=False, raise_server_exceptions=False)
    c.cookies.set(panel.COOKIE, TP.galleta())
    destino = c.post("/categorias", data={"cat_7": "Seguros", "prev_7": "", "volver": "/movimientos"}).headers["location"]
    monkeypatch.setattr(avisos, "_recibos_de", lambda *a, **k: 1 / 0)
    assert not any("Guardado" in t for t in textos(c.get(destino).text))
    assert not any("Guardado" in t for t in textos(c.get("/movimientos?guardados=5").text))


# ═══════════════════════════════════════════════════════════════════════
# Lo que quedó en la cola del testigo
# ═══════════════════════════════════════════════════════════════════════

def test_el_recibo_vive_ciento_veinte_segundos_en_la_cookie_y_en_la_firma():
    """El 120 s lo dicen el docstring y el mapa: si alguien lo cambia, que cambie también lo que dicen."""
    assert avisos.VIDA_RECIBO == 120
    c = TestClient(_app_minima(), follow_redirects=False)
    antes = int(time.time())
    r = c.post("/x/1/hacer")
    assert "Max-Age=120" in r.headers["set-cookie"]
    vence = int(r.headers["set-cookie"].split("lucy_aviso=")[1].split(".")[0])
    assert antes + 119 <= vence <= antes + 121


def test_la_cookie_vacia_se_borra_con_max_age_cero():
    c = TestClient(_app_minima())
    destino = c.post("/x/1/hacer", follow_redirects=False).headers["location"]
    r = c.get(destino)
    assert "Max-Age=0" in r.headers["set-cookie"]


def test_las_paginas_sin_aviso_salen_con_las_mismas_cabeceras_de_cache_que_antes(mundo_de_rutas):
    """Antes de la puerta ninguna pantalla mandaba `Cache-Control` ni cookies: sin avisos, tampoco ahora (el
    `no-store` es solo de la página que pintó un aviso gastado)."""
    esc = mundo_de_rutas
    for url in esc.rutas:
        for consulta in ({}, {"desde": "2026-08-01"} if "movimientos" in url else {}):
            r = esc.cliente.get(url, params=consulta)
            assert r.status_code == 200
            assert "cache-control" not in r.headers and "set-cookie" not in r.headers, (url, dict(r.headers))
        # una dirección con aviso a mano (sin recibo): tampoco
        for ruta in _rutas_get(panel.app):
            if ruta.path == esc.rutas[url] and de_aviso(ruta):
                forzada = esc.cliente.get(url, params={de_aviso(ruta)[0]: "1"})
                assert "cache-control" not in forzada.headers and "set-cookie" not in forzada.headers, url


def test_una_ruta_sin_parametros_de_aviso_sale_con_sus_cabeceras_de_siempre(monkeypatch):
    """Las rutas que no declaran avisos (la pantalla del historial, y una mínima) no reciben `no-store` ni cookie."""
    app = _app_minima()

    @app.get("/plano", response_class=HTMLResponse)
    async def plano(p: Navegacion[int] = 0):
        return "<main>plano</main>"
    c = TestClient(app)
    for url in ("/plano", "/plano?p=3"):
        r = c.get(url)
        assert r.status_code == 200 and "cache-control" not in r.headers and "set-cookie" not in r.headers, url
    esc = _mundo_tareas(monkeypatch)
    r = esc.cliente.get("/tareas/historial")
    assert r.status_code == 200 and "cache-control" not in r.headers and "set-cookie" not in r.headers


def test_la_pagina_que_pinta_un_aviso_gastado_si_sale_no_store_y_una_redireccion_no():
    c = TestClient(_app_minima(), follow_redirects=False)
    r = c.post("/x/1/hacer")
    assert "cache-control" not in r.headers
    assert c.get(r.headers["location"]).headers["cache-control"] == "no-store"


def test_dos_avisos_iguales_en_el_mismo_segundo_se_gastan_uno_a_la_vez(monkeypatch):
    """Dos pestañas haciendo el mismo aviso en el mismo segundo dan dos recibos iguales: cada una debe ver el
    suyo (antes, gastar uno se llevaba los dos y la segunda se quedaba sin el aviso de algo que sí pasó)."""
    ahora = time.time()
    monkeypatch.setattr(avisos, "time", types.SimpleNamespace(time=lambda: ahora))
    c = TestClient(_app_minima(), follow_redirects=False)
    d1 = c.post("/x/1/hacer").headers["location"]
    d2 = c.post("/x/1/hacer").headers["location"]
    assert d1 == d2 and len(c.cookies.get("lucy_aviso").split("~")) == 2
    assert _dice(c.get(d1)) == "hecho=listo cuenta=2"
    assert _dice(c.get(d2)) == "hecho=listo cuenta=2"
    assert _dice(c.get(d2)) == "hecho= cuenta=0"


def test_paso_movido_que_no_es_un_numero_ya_no_da_422_y_no_deja_ver_nada_falso(monkeypatch):
    """Antes `paso_movido=abc` daba 422 (era un entero); ahora es texto y la página sale sin el aviso: solo
    el valor exacto `0`, con recibo, pinta algo."""
    esc = _mundo_tareas(monkeypatch)
    for valor in ("abc", "", "01", "0 ", "cero"):
        r = esc.cliente.get("/tareas/1", params={"paso_movido": valor})
        assert r.status_code == 200 and not any("punta" in t for t in textos(r.text)), valor
    from _navegador import dar_recibo
    dar_recibo("/tareas/1", paso_movido="abc")
    assert not any("punta" in t for t in textos(esc.cliente.get("/tareas/1?paso_movido=abc").text))
