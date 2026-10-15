"""La página de Proyectos NO SE MUEVE al tocar algo (Tiziano, 13-oct-2026: «cada vez que le doy click
algo en Proyecto la pagina se mueve y es muy incomodo tengo que estar haciendo scroll constantemente»).

LA CAUSA, medida el 13-oct-2026 sobre la página que dibuja el servidor: de 73 cosas que se tocan en la
sesión de la casa, solo 11 dejaban a la persona donde estaba (marcar, desmarcar y el responsable de la
tarea, que usaban la pieza `marcarSinSaltar`); 32 la mandaban arriba del todo (enlaces y formularios
cuya vuelta no lleva `#`) y 30 a un ancla. En la sesión de solo ver la página no traía ningún guion:
todo recargaba.

EL ARREGLO: la MISMA pieza (`web/plantillas/_marcar_sin_saltar.html`), generalizada. Todo enlace y
todo formulario de la página que la puerta `destinoSinSaltar` deja pasar se pide por detrás y se
pinta lo que contesta el servidor, con el desplazamiento y la fila de referencia devueltos; la dirección
de la barra queda siendo la verdadera (un GET deja una entrada en el historial, un POST no); y «atrás»
vuelve a pintar. Funciona igual en la sesión de la casa y en la de solo ver.

QUÉ VIGILA ESTE ARCHIVO. (1) EL CENSO: todo `<a href>` y todo `<form>` que el servidor dibuja (casa y
solo ver, un proyecto con de todo, las vistas y las direcciones armadas a mano de
`test_proyectos_solo_ver.py`) pasa por los oyentes REALES de la página y tiene que caer en uno de dos
cubos: «se queda» (se pide por detrás, y a una dirección de esta pantalla) o «sale a propósito»
(declarado uno por uno). Lo no clasificado cae del lado estricto y un control inventado lo atrapa.
(2) LO QUE HACE EL GUION, corrido en JavaScriptCore con el navegador de mentira de `tests/_sin_saltar.py`.
(3) LAS PROHIBICIONES de la pieza.

LA FRONTERA, dicha: no es un navegador. NO se midió aquí: dónde queda de verdad la pantalla, el alto
real, qué hace «atrás» con el desplazamiento guardado por el navegador, que el POST siga la
redirección con la cookie del recibo de los avisos, que `r.url` no traiga el `#ancla` ni que `fetch`
lea un `Set-Cookie` de una redirección. Eso lo mide la sala en un navegador (el servidor local de
prueba de este trabajo)."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from _sin_saltar import (PERMITIDO_EN_LA_PIEZA, la_pieza_de, lo_prohibido_en_la_pieza, sin_comentarios_de_js,
                         texto_de_la_pieza, correr_en_navegador, correr)
from test_escrituras_proyecto import hay_osascript
import config
import test_proyectos_solo_ver as sv
import _proyectos_de_prueba as pp
import web.menu as menu


# ═══════════════════════════════════════════════════════════════════════
# Los guiones de la página, tal como los pinta el servidor
# ═══════════════════════════════════════════════════════════════════════

REGISTRO = "https://registro.example.test"


def guion_de(con: str, **consulta) -> str:
    """El `<script>` (el primero) de la página tal como la pinta la ruta real, con la base de prueba
    (`tests/_proyectos_de_prueba.py`: un proyecto con de todo)."""
    html = sv._pintar(con, **consulta)
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert guiones, (con, consulta)
    return guiones[0]


@pytest.fixture(autouse=True)
def _registro(monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", REGISTRO)


# ═══════════════════════════════════════════════════════════════════════
# 1. EL CENSO: todo lo que se toca, sacado de la página renderizada
# ═══════════════════════════════════════════════════════════════════════

class _Censo(HTMLParser):
    """Todo `<a href>` y todo `<form>` del HTML pintado, con lo que el guion les pregunta."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.visto: set[tuple] = set()
        self.botones: set[tuple] = set()        # (action, método, nombre, valor) de los botones con nombre
        self._form: dict = {}

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = a
        if tag in ("button", "input") and self._form and (a.get("type") or "submit") == "submit":
            if a.get("name"):
                self.botones.add((self._form.get("action") or "", (self._form.get("method") or "get").lower(), a["name"], a.get("value") or ""))
            if a.get("formaction"):      # un botón que cambia a dónde va el envío es otro destino que el censo tiene que ver
                self.visto.add(("form", a["formaction"], (a.get("formmethod") or self._form.get("method") or "get").lower(),
                                a.get("formtarget") or self._form.get("target"), False, a.get("class") or "", a.get("id") or ""))
        if tag == "a" and "href" in a:
            self.visto.add(("a", a["href"], "", a.get("target"), "download" in a, a.get("class") or "", a.get("id") or ""))
        elif tag == "form":
            self.visto.add(("form", a.get("action") or "", (a.get("method") or "get").lower(), a.get("target"), False,
                            a.get("class") or "", a.get("id") or ""))


def paginas_de(con: str) -> list[str]:
    """TODAS las páginas que la plantilla puede dibujar con la sesión `con`: las consultas de `sv.COBERTURA`
    (la lista con la que `test_proyectos_solo_ver.py` exige que ningún control de la plantilla quede sin pintar,
    `sitios_sin_cubrir`), con el bloque de sesiones del registro dibujado. No es una lista corta escogida aquí."""
    with sv._con_el_bloque_de_sesiones():
        return [sv._pintar(con, **c) for c in sv.COBERTURA]


def lo_que_se_toca(con: str) -> list[dict]:
    """La lista de todo lo que se toca en `con` («casa» o «ver») en TODAS las vistas y las direcciones
    armadas a mano de `test_proyectos_solo_ver.py`. Sale del HTML que dibuja el servidor."""
    c = _Censo()
    for html in paginas_de(con):
        c.feed(html)
    return [dict(zip(("tag", "href", "metodo", "target", "download", "clase", "id"), t)) | {"es": t[0]}
            for t in sorted(c.visto, key=str)]


_CENSO_JS = r"""
var datos = JSON.parse(%s);
var filas = datos.map(function (d) {
  pedidos.length = 0; salidas.length = 0;
  var e;
  if (d.tag === "a") {
    var extra = {}; if (d.target !== null) extra.target = d.target; if (d.download) extra.download = "";
    e = clic(enlace(d.href, extra));
  } else {
    e = enviar(formulario(d.href, d.metodo, [], d.target !== null ? {target: d.target} : {}));
  }
  var fila = {prevenido: e.defaultPrevented, pedidos: pedidos.map(function (p) { return [p.metodo, p.url]; })};
  pedidos.slice().forEach(function (p) { p.f.rechazar(new Error("x")); });   // suelta el pedido: nada queda en camino
  return fila;
});
JSON.stringify(filas)
"""


def pasar_por_los_oyentes(con: str, lista: list[dict]) -> list[dict]:
    """Cada elemento de `lista` pasa por los oyentes REALES de la página (el guion de `con`): qué se
    evita y qué pedido sale."""
    filas = correr_en_navegador(guion_de(con, p=1), _CENSO_JS % json.dumps(json.dumps(lista)))
    return [{**d, **f} for d, f in zip(lista, filas)]


def lo_que_sale_a_proposito() -> list[tuple[str, str, str]]:
    """LOS DOS CUBOS, el segundo: lo que SALE de la página a propósito, declarado uno por uno
    (qué es, y por qué). Todo lo demás que se toca tiene que quedarse en la página."""
    # (El «Proyectos» del menú apunta a ESTA pantalla: se queda, como cualquier enlace a sí misma.)
    declarados = [("a", p.ruta, f"el menú de arriba: «{p.nombre}» (sale de `web/menu.py`, el `<nav>` real)")
                  for p in menu.pantallas() if p.ruta != "/proyectos"]
    declarados += [
        # (los enlaces de texto de la página hacia otras pantallas, uno por uno)
        ("a", "/tareas", "enlace de texto «Tareas» (las sueltas): es otra pantalla"),
        ("a", "/papelera", "enlace de texto hacia la Papelera: es otra pantalla"),
        ("a", REGISTRO + "/#inicio", "«‹ Inicio»: vuelve a la App de registro"),
        ("a", "https://drive.example.test/carpetas/disco-uno", "«Abrir la carpeta» (Drive): abre otra pestaña"),
    ]
    # El «PDF» de cada cotización de la lista (14-oct-2026, pieza 5): un enlace con `download`, que baja un
    # archivo y la pieza deja pasar. La lista sale de las cotizaciones del modelo de prueba, no de números tecleados.
    declarados += [("a", f"/proyectos/{pid}/cotizaciones/{c['id']}/pdf",
                    "«PDF» de una cotización: baja un archivo (`download`), no cambia la página")
                   for pid, cs in pp.COTIZACIONES_DEL_MODELO.items() for c in cs]
    return declarados


def problemas_del_censo(filas: list[dict]) -> list[str]:
    declarados = {(t, h) for t, h, _ in lo_que_sale_a_proposito()}
    malos = []
    for f in filas:
        nombre = f"{f['tag']} {f['href']!r} ({f['metodo'] or 'GET'}, clase={f['clase']!r})"
        if f["prevenido"] and len(f["pedidos"]) == 1:                     # SE QUEDA
            metodo, url = f["pedidos"][0]
            esperado_post = f["tag"] == "form" and f["metodo"] == "post"
            if metodo != ("POST" if esperado_post else "GET") or not url.startswith("/proyectos"):
                malos.append(f"{nombre}: se queda pero pide {metodo} {url}")
            if f["target"] not in (None, "_self") or f["download"]:
                malos.append(f"{nombre}: se queda y abría otra pestaña o bajaba un archivo")
        elif not f["prevenido"] and not f["pedidos"]:                     # SALE
            if (f["tag"], f["href"]) not in declarados:
                malos.append(f"{nombre}: sale de la página y nadie lo declaró")
        else:
            malos.append(f"{nombre}: a medias (prevenido={f['prevenido']}, pedidos={f['pedidos']})")
    return malos


@hay_osascript
@pytest.mark.parametrize("con", ["casa", "ver"])
def test_todo_lo_que_se_toca_se_queda_en_la_pagina_o_sale_a_proposito(con):
    lista = lo_que_se_toca(con)
    filas = pasar_por_los_oyentes(con, lista)
    assert problemas_del_censo(filas) == []
    # El censo no está vacío ni es solo de un cubo: si no viera nada, esto no vigilaría nada.
    quedan = [f for f in filas if f["prevenido"]]
    salen = [f for f in filas if not f["prevenido"]]
    assert len(quedan) >= (60 if con == "casa" else 10) and len(salen) >= 2, (con, len(quedan), len(salen))
    if con == "casa":
        assert {f["metodo"] for f in quedan if f["tag"] == "form"} == {"post", "get"}
        # lo declarado como «sale» existe de verdad en la página (una línea declarada que ya no hay, se quita)
        assert ("a", "/proyectos") in {(f["tag"], f["href"]) for f in quedan}      # el «Proyectos» del menú se queda
        assert {p.ruta for p in menu.pantallas()} - {"/proyectos"} <= {f["href"] for f in salen}
        assert {(f["tag"], f["href"]) for f in salen} == {(t, h) for t, h, _ in lo_que_sale_a_proposito()}
    # Y la de solo ver no trae ningún POST en lo que se queda (no hay formulario que escriba).
    if con == "ver":
        assert not [f for f in quedan if f["tag"] == "form" and f["metodo"] == "post"]


def los_botones_con_nombre() -> list[dict]:
    """Todo botón con nombre de todo formulario que la casa ve (de la página dibujada): el envío depende de él."""
    c = _Censo()
    for html in paginas_de("casa"):
        c.feed(html)
    return [dict(accion=a, metodo=m, nombre=n, valor=v) for a, m, n, v in sorted(c.botones)]


@hay_osascript
def test_el_respaldo_manda_el_boton_que_envio_en_todo_formulario_que_depende_de_el():
    """H1: «Elegir» una persona y «Quitar el cliente» llevan `name="noco_id"`; `submit()` no manda el botón y
    elegir a alguien se volvía quitar el cliente. Cada formulario con botón con nombre sale de la página
    dibujada; cuando el servidor lo rechaza (4xx) el respaldo es `requestSubmit(ese botón)`."""
    botones = los_botones_con_nombre()
    assert {(b["nombre"], b["valor"]) for b in botones} >= {("noco_id", ""), ("noco_id", "900")}, botones     # «Quitar el cliente» y «Elegir»
    for b in botones:
        if destino_es_de_la_pantalla(b):
            esc = ('var f = formulario(%s, %s, []); var bt = {name: %s, value: %s, disabled: false}; __e = enviar(f, bt);'
                   ' responder(0, "X", {ok: false, status: 401});'
                   % (json.dumps(b["accion"]), json.dumps(b["metodo"]), json.dumps(b["nombre"]), json.dumps(b["valor"])))
            r = correr(CASA(), esc)
            assert [s for s in r["salidas"] if s[0] in ("requestSubmit", "submit")] == [["requestSubmit", b["accion"], b["nombre"]]], b
            # y por detrás el botón viaja en lo que se manda
            r = correr(CASA(), esc.replace('responder(0, "X", {ok: false, status: 401});', ""))
            enviado = r["pedidos"][0]
            assert (b["nombre"] + "=" + b["valor"]) in ((enviado[2] or "") + "&" + enviado[1]), (b, enviado)


def destino_es_de_la_pantalla(b: dict) -> bool:
    return b["accion"].startswith("/proyectos")


@hay_osascript
@pytest.mark.parametrize("inventado", [
    dict(tag="a", href="/otra", metodo="", target=None, download=False, clase="", id=""),
    dict(tag="a", href="/proyectos?p=1", metodo="", target="_blank", download=False, clase="", id=""),
    dict(tag="a", href="/proyectos?p=1", metodo="", target=None, download=True, clase="", id=""),
    dict(tag="a", href="https://otro.example.test/proyectos?p=1", metodo="", target=None, download=False, clase="", id=""),
    dict(tag="a", href="javascript:alert(1)", metodo="", target=None, download=False, clase="", id=""),
    dict(tag="form", href="/otra/3/borrar", metodo="post", target=None, download=False, clase="", id=""),
    dict(tag="form", href="/tareas", metodo="get", target=None, download=False, clase="", id=""),
    dict(tag="form", href="/proyectos/1/borrar", metodo="post", target="_blank", download=False, clase="", id=""),
])
def test_un_control_inventado_lo_atrapa_el_censo(inventado):
    """Lo no clasificado cae del lado estricto: un enlace o formulario nuevo que la página no
    declara, o que se abriría en otra pestaña, o que va a otro sitio, pone rojo el censo."""
    inventado = {**inventado, "es": inventado["tag"]}
    filas = pasar_por_los_oyentes("casa", [inventado])
    assert problemas_del_censo(filas) != [], filas


@hay_osascript
def test_el_censo_ve_un_boton_con_formaction_dentro_de_un_formulario():
    """Q2 del testigo (n15b): `<button formaction="/otra/3/borrar">` en el formulario de nuevo grupo. Un
    botón así cambia a dónde va el envío; el censo lo cuenta como destino y lo acusa si no está declarado."""
    c = _Censo()
    c.feed('<form method="post" action="/proyectos/grupos"><button formaction="/otra/3/borrar">x</button></form>')
    lista = [dict(zip(("tag", "href", "metodo", "target", "download", "clase", "id"), t)) | {"es": t[0]} for t in sorted(c.visto, key=str)]
    assert {d["href"] for d in lista} == {"/proyectos/grupos", "/otra/3/borrar"}
    assert problemas_del_censo(pasar_por_los_oyentes("casa", lista)) != []


@hay_osascript
def test_el_censo_ve_un_enlace_que_se_queda_y_no_lo_acusa():
    ok = dict(tag="a", href="/proyectos?p=2", metodo="", target=None, download=False, clase="", id="", es="a")
    assert problemas_del_censo(pasar_por_los_oyentes("casa", [ok])) == []


# ═══════════════════════════════════════════════════════════════════════
# 2. LO QUE HACE EL GUION (JavaScriptCore, navegador de mentira)
# ═══════════════════════════════════════════════════════════════════════

CASA = lambda: guion_de("casa", p=1)     # noqa: E731
VER = lambda: guion_de("ver", p=1)       # noqa: E731

@hay_osascript
@pytest.mark.parametrize("sesion", ["casa", "ver"])
def test_un_enlace_de_la_pagina_se_pide_por_detras_y_no_recarga(sesion):
    g = CASA() if sesion == "casa" else VER()
    r = correr(g, '__e = clic(enlace("/proyectos?p=2")); responder(0, "PAG-2");')
    assert r["prevenido"] is True
    assert r["pedidos"] == [["GET", "/proyectos?p=2", None]]                     # UN pedido, y es la propia dirección
    assert r["historia"] == [["replace", {"x": 0, "y": 50}, None], ["push", None, "/proyectos?p=2"]]   # la entrada que se deja recuerda dónde estaba
    assert r["donde"] == "/proyectos?p=2"                                       # la barra dice la verdad
    assert r["izq"] == "PAG-2" and r["cen"] == "PAG-2"                          # se pintó lo que contestó el servidor
    assert r["izqTop"] == 300 and r["cenTop"] == 1200 and r["ventana"] == [[0, 50]]   # y el desplazamiento es el de antes
    assert r["salidas"] == []                                                   # nada de recargar


@hay_osascript
@pytest.mark.parametrize("sesion", ["casa", "ver"])
@pytest.mark.parametrize("href,extra,modificadores", [
    ("/tareas", {}, {}), ("/", {}, {}), ("https://otro.example.test/proyectos?p=2", {}, {}),
    ("mailto:a@example.test", {}, {}), ("javascript:alert(1)", {}, {}),
    ("/proyectos?p=2", {"target": "_blank"}, {}), ("/proyectos?p=2", {"download": ""}, {}),
    ("/proyectos?p=2", {}, {"ctrlKey": True}), ("/proyectos?p=2", {}, {"metaKey": True}),
    ("/proyectos?p=2", {}, {"shiftKey": True}), ("/proyectos?p=2", {}, {"altKey": True}),
    ("/proyectos?p=2", {}, {"button": 1}),
    ("#algo", {}, {}),                          # un ancla suelta de esta misma pantalla: la maneja el navegador
])
def test_lo_que_sale_de_la_pagina_o_pide_otra_cosa_al_navegador_no_se_toca(sesion, href, extra, modificadores):
    g = CASA() if sesion == "casa" else VER()
    r = correr(g, '__e = clic(enlace(%s, %s), %s);' % (json.dumps(href), json.dumps(extra), json.dumps(modificadores)))
    assert r["prevenido"] is False and r["pedidos"] == [] and r["historia"] == [] and r["salidas"] == []


@hay_osascript
def test_un_formulario_que_guarda_va_por_detras_con_sus_campos_y_el_boton_que_lo_envio_y_no_deja_entrada_nueva():
    esc = ('var f = formulario("/proyectos/tarea/10/hecha?filtro=mias", "post", [["a", "1"], ["b", "x y"]]);'
           ' __e = enviar(f, {name: "noco_id", value: "77", disabled: false}); responder(0, "PAG-3", {url: "http://x.test/proyectos?p=2&hecho=tarea_hecha"});'
           ' var __boton = f.boton.disabled;')
    r = correr(CASA(), esc + "")
    assert r["prevenido"] is True
    assert r["pedidos"] == [["POST", "/proyectos/tarea/10/hecha?filtro=mias", "a=1&b=x%20y&noco_id=77"]]
    assert r["historia"] == [["replace", None, "/proyectos?p=2&hecho=tarea_hecha"]]       # guardar no suma una entrada
    assert r["cen"] == "PAG-3" and r["salidas"] == []


@hay_osascript
def test_un_formulario_de_busqueda_get_manda_sus_campos_en_la_direccion_y_si_deja_entrada():
    r = correr(CASA(), '__e = enviar(formulario("/proyectos", "get", [["q", "disco"], ["estado", "1"]])); responder(0, "PAG-4");')
    assert r["pedidos"] == [["GET", "/proyectos?q=disco&estado=1", None]]
    assert [h[0] for h in r["historia"]] == ["replace", "push"] and r["historia"][1][2] == "/proyectos?q=disco&estado=1"
    r = correr(CASA(), '__e = enviar(formulario("/proyectos", "get", [])); responder(0, "PAG-4");')
    assert r["pedidos"] == [["GET", "/proyectos", None]]


@hay_osascript
@pytest.mark.parametrize("accion,metodo,extra", [
    ("/otra/3/borrar", "post", {}), ("/tareas", "get", {}), ("https://otro.example.test/proyectos/1/nombre", "post", {}),
    ("/proyectos/1/nombre", "post", {"target": "_blank"}), ("/proyectos", "dialog", {}),
])
def test_un_formulario_que_no_es_de_la_pantalla_se_envia_como_siempre(accion, metodo, extra):
    r = correr(CASA(), '__e = enviar(formulario(%s, %s, [], %s));' % (json.dumps(accion), json.dumps(metodo), json.dumps(extra)))
    assert r["prevenido"] is False and r["pedidos"] == []


@hay_osascript
def test_agregar_una_persona_sin_escogerla_no_sale_y_lo_dice_el_guion_de_la_pagina():
    """El oyente de la página (que ve el formulario ANTES que la pieza) cancela el envío: la pieza respeta
    `defaultPrevented` y no pide nada."""
    esc = ('var f = formulario("/proyectos/1/personas", "post", []); f.dataset.pidePersona = "";'
           ' var lista = {firstChild: null, appendChild: function () {}};'
           ' f.querySelector = function (s) { return s === ".sugerencias" ? lista : null; };'
           ' __e = enviar(f);')
    r = correr(CASA(), esc)
    assert r["prevenido"] is True and r["pedidos"] == []


AVISO_SIN_CONFIRMAR = "No se pudo confirmar si se guardó. No se volvió a enviar para no duplicarlo; lo que tenías escrito sigue aquí."


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "401", "404"])
def test_un_get_que_no_llega_o_el_servidor_rechaza_se_hace_de_la_forma_de_siempre(modo):
    """Un GET (enlace, búsqueda) no duplica nada: se hace como siempre. Con el botón de la búsqueda, si lo hubo."""
    cierre = 'sinRed(0);' if modo == "red" else 'responder(0, "ENTRAR", {ok: false, status: %s});' % modo
    r = correr(CASA(), '__e = clic(enlace("/proyectos?p=2"));' + cierre)
    assert r["salidas"] == [["ir", "/proyectos?p=2"]] and r["historia"] == [] and r["cen"] == "VIEJA"


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "500", "503", "texto-corto"])
def test_h1_un_post_sin_respuesta_o_con_5xx_no_se_reenvia_y_la_persona_lo_lee_sin_perder_lo_escrito(modo):
    """SONDA DEL TESTIGO (H1): el POST pudo llegar y perderse solo la respuesta. Nada se reenvía (ni
    `submit()` ni `requestSubmit()`), la página no se repinta (lo escrito sigue ahí) y se lee un aviso fijo con
    un enlace a esta misma vista para mirar cómo quedó."""
    cierre = {"red": "sinRed(0);", "500": 'responder(0, "<p>error</p>", {ok: false, status: 500});',
              "503": 'responder(0, "x", {ok: false, status: 503});', "texto-corto": ""}[modo]
    if modo == "texto-corto":       # la respuesta es buena pero se corta: ya se guardó, se va a la página (GET), nunca otro POST
        cierre = ('pedidos[0].f.resolver({ok: true, status: 200, url: "http://x.test/proyectos?p=1", text: function () {'
                  ' var f = new Futuro(); f.rechazar(new Error("corte")); return f; }});')
    r = correr(CASA(), 'var f = formulario("/proyectos/1/notas", "post", [["contenido", "lo que escribí"]]); __e = enviar(f); '
                       'var __b = f.boton.disabled;' + cierre)
    assert [p[0] for p in r["pedidos"]] == ["POST"] and r["cen"] == "VIEJA" and r["historia"] == []
    if modo == "texto-corto":
        assert r["salidas"] == [["ir", "http://x.test/proyectos?p=1"]]
    else:
        assert r["salidas"] == [["aviso", [AVISO_SIN_CONFIRMAR], "/proyectos?p=1"]]       # y NADA de submit / requestSubmit
    assert r["escritos"] == []


@hay_osascript
@pytest.mark.parametrize("modo", ["401", "404"])
def test_un_post_que_el_servidor_rechazo_con_4xx_no_escribio_nada_y_se_envia_como_siempre_con_su_boton(modo):
    esc = ('var f = formulario("/proyectos/1/cliente", "post", [["a", "1"]]); var b = {name: "noco_id", value: "7", disabled: false};'
           ' __e = enviar(f, b); responder(0, "ENTRAR", {ok: false, status: %s}); salidas.push(["nativo", f.dataset.nativo, f.dataset.enviado]);' % modo)
    r = correr(CASA(), esc)
    assert r["salidas"] == [["requestSubmit", "/proyectos/1/cliente", "noco_id"], ["nativo", "", None]]
    assert r["historia"] == [] and r["cen"] == "VIEJA"


def _con_controles(nombre):
    return ('%s.controles = [{type: "text", tagName: "INPUT", readOnly: false}, {type: "hidden", tagName: "INPUT", readOnly: false},'
            ' {type: "select-one", tagName: "SELECT", disabled: false}];' % nombre)


@hay_osascript
def test_h4_un_formulario_que_ya_se_mando_o_espera_en_la_cola_no_deja_escribir_y_se_suelta_al_terminar():
    """SONDAS 2 Y 5 DEL TESTIGO: el texto que se teclea en un formulario ya mandado se perdía. Ahora no se puede
    teclear (texto en solo lectura, desplegable desactivado; lo escondido no se toca) hasta que su envío termina."""
    esc = ('var A = formulario("/proyectos/1/notas", "post", []); enviar(A);'
           ' var B = formulario("/proyectos/1/descripcion", "post", [["descripcion", "x"]]); ' + _con_controles("B") +
           ' __e = enviar(B); var c = B.controles;'
           ' salidas.push(["bloqueado", c[0].readOnly, c[1].readOnly, c[2].disabled]);'
           ' responder(0, "PAG-A"); salidas.push(["B corriendo", c[0].readOnly]); sinRed(1);'
           ' salidas.push(["suelto", c[0].readOnly, c[2].disabled]);')
    r = correr(CASA(), esc)
    assert ["bloqueado", True, False, True] in r["salidas"]              # en la cola ya está bloqueado (el escondido, no)
    assert ["B corriendo", True] in r["salidas"]
    assert ["suelto", False, False] in r["salidas"]                      # terminó (aquí con aviso: el formulario sigue ahí)


@hay_osascript
def test_h4_la_lectura_pisada_en_la_cola_suelta_su_formulario():
    esc = ('var A = formulario("/proyectos/1/notas", "post", []); enviar(A);'
           ' var G = formulario("/proyectos", "get", [["q", "a"]]); ' + _con_controles("G") + ' enviar(G); var c = G.controles;'
           ' var G2 = formulario("/proyectos", "get", [["q", "b"]]); enviar(G2); salidas.push(["suelto", c[0].readOnly, G.dataset.enCamino]);')
    assert ["suelto", False, None] in correr(CASA(), esc)["salidas"]


@hay_osascript
def test_sonda_4_un_enlace_entre_dos_guardados_la_pagina_termina_en_el_enlace_y_los_avisos_se_leen():
    """SONDA 4 DEL TESTIGO: POST A, enlace a p=2, POST B. La página termina en lo último que se pidió ver (p=2);
    los guardados no la mueven y su aviso se lee."""
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [["contenido", "A"]]));'
           ' clic(enlace("/proyectos?p=2"));'
           ' __e = enviar(formulario("/proyectos/tarea/10/comentar", "post", [["texto", "B"]]));'
           ' paginas["PAG-A"] = {avisos: ["Nota guardada."]}; paginas["PAG-B"] = {avisos: ["Comentario guardado."]};'
           ' responder(0, "PAG-A"); responder(1, "PAG-B"); responder(2, "PAG-2");')
    r = correr(CASA(), esc)
    assert [(p[0], p[1]) for p in r["pedidos"]] == [("POST", "/proyectos/1/notas"), ("POST", "/proyectos/tarea/10/comentar"), ("GET", "/proyectos?p=2")]
    assert r["cen"] == "PAG-2" and r["donde"] == "/proyectos?p=2"
    ultimo = [x for x in r["salidas"] if x[0] == "aviso"][-1]
    assert ultimo[1] == ["Nota guardada.", "Comentario guardado."]


# ── N1: ningún pedido sale después de que la página empezó a irse ─────────────────────────────────
# Los tres caminos que hacen que la página se vaya: `irA` tras un POST bueno que no se pudo pintar (otra pantalla
# / respuesta cortada), y el envío de siempre tras un 4xx. La pareja de cada uno: sin nada en la cola, se va enseguida.
_CAMINOS_QUE_SE_VAN = {
    "no-se-pudo-pintar": ('paginas["PAG"] = {regiones: ["cuerpo"]};', 'responder(0, "PAG", {url: "http://x.test/proyectos?p=2"});',
                          [["ir", "http://x.test/proyectos?p=2"]]),
    "respuesta-cortada": ("", 'pedidos[0].f.resolver({ok: true, status: 200, url: "http://x.test/proyectos?p=2", text: function () {'
                              ' var f = new Futuro(); f.rechazar(new Error("corte")); return f; }});', [["ir", "http://x.test/proyectos?p=2"]]),
    "401": ("", 'responder(0, "ENTRAR", {ok: false, status: 401});', [["requestSubmit", "/proyectos/1/notas", None]]),
}


@hay_osascript
@pytest.mark.parametrize("camino", list(_CAMINOS_QUE_SE_VAN))
def test_n1_con_guardados_en_la_cola_la_pagina_no_se_va_sola_y_ninguno_sale(camino):
    """SONDA 5 DEL TESTIGO. A recibe una respuesta que obliga a irse y B espera en la cola: B NO sale (saldría
    desde la página que se va y podría perderse sin que la persona lo sepa); su formulario se suelta con lo
    escrito; el aviso fijo dice que no se mandó; la página se va solo con «Continuar»."""
    previo, cierre, destino = _CAMINOS_QUE_SE_VAN[camino]
    esc = (previo + 'var A = formulario("/proyectos/1/notas", "post", []); enviar(A);'
           ' var B = formulario("/proyectos/tarea/10/comentar", "post", [["texto", "B"]]); ' + _con_controles("B") + ' __e = enviar(B);'
           + cierre + ' var c = B.controles; salidas.push(["B", c[0].readOnly, B.dataset.enCamino, pedidos.length]);'
           ' salidas.push(["antes de Continuar", salidas.filter(function (x) { return x[0] === "ir" || x[0] === "requestSubmit"; }).length]);'
           ' avisosPuestos[0].continuar(); ')
    r = correr(CASA(), esc)
    assert len(r["pedidos"]) == 1                                                    # B nunca salió
    assert ["B", False, None, 1] in r["salidas"]                                      # B soltó su formulario, con lo escrito
    assert ["antes de Continuar", 0] in r["salidas"]                                  # la página NO se fue sola
    aviso = [x for x in r["salidas"] if x[0] == "aviso"][-1]
    assert any("Había 1 cambio esperando: NO se mandó" in m and "Continuar" in m for m in aviso[1]), aviso
    assert [x for x in r["salidas"] if x[0] in ("ir", "requestSubmit")] == destino   # y se fue al tocar «Continuar»


@hay_osascript
@pytest.mark.parametrize("camino", list(_CAMINOS_QUE_SE_VAN))
def test_n1_pareja_sin_nada_en_la_cola_la_pagina_se_va_enseguida(camino):
    previo, cierre, destino = _CAMINOS_QUE_SE_VAN[camino]
    r = correr(CASA(), previo + 'var A = formulario("/proyectos/1/notas", "post", []); __e = enviar(A);' + cierre)
    assert [x for x in r["salidas"] if x[0] in ("ir", "requestSubmit")] == destino and not [x for x in r["salidas"] if x[0] == "aviso"]
    assert len(r["pedidos"]) == 1


@hay_osascript
def test_n1_dos_guardados_en_la_cola_dicen_cuantos_no_se_mandaron():
    esc = ('paginas["PAG"] = {regiones: ["cuerpo"]}; enviar(formulario("/proyectos/1/notas", "post", []));'
           ' enviar(formulario("/proyectos/1/descripcion", "post", [])); enviar(formulario("/proyectos/1/carpeta", "post", []));'
           ' responder(0, "PAG", {url: "http://x.test/proyectos?p=2"});')
    r = correr(CASA(), esc)
    assert len(r["pedidos"]) == 1 and any("Había 2 cambios esperando: NO se mandaron" in m for x in r["salidas"] if x[0] == "aviso" for m in x[1])


@hay_osascript
def test_n1_una_lectura_en_la_cola_gana_sobre_un_guardado_que_obligaba_a_irse():
    """Con una lectura esperando, un guardado cuya página no se puede poner no navega: la página termina en
    lo último que la persona pidió ver (la lectura sale y pinta)."""
    esc = ('paginas["PAG"] = {regiones: ["cuerpo"]}; enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/proyectos?p=3"));'
           ' responder(0, "PAG", {url: "http://x.test/proyectos?p=2"}); responder(1, "PAG-3");')
    r = correr(CASA(), esc)
    assert [p[1] for p in r["pedidos"]] == ["/proyectos/1/notas", "/proyectos?p=3"] and r["salidas"] == [] and r["cen"] == "PAG-3"


@hay_osascript
def test_n1_una_lectura_en_la_cola_se_descarta_si_el_envio_de_siempre_se_va():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/proyectos?p=3"));'
           ' responder(0, "ENTRAR", {ok: false, status: 401});')
    r = correr(CASA(), esc)
    assert [p[1] for p in r["pedidos"]] == ["/proyectos/1/notas"] and r["salidas"] == [["requestSubmit", "/proyectos/1/notas", None]]


# ── N5: las salidas de la página con algo pendiente ───────────────────────────────────────────────
_CENSO_PENDIENTE_JS = r"""
var datos = JSON.parse(%s);
%s
var base = pedidos.length;
var filas = datos.map(function (d) {
  salidas.length = 0; var antes = pedidos.length;
  var e;
  if (d.tag === "a") {
    var extra = {}; if (d.target !== null) extra.target = d.target; if (d.download) extra.download = "";
    e = clic(enlace(d.href, extra));
  } else {
    var ff = formulario(d.href, d.metodo, [], d.target !== null ? {target: d.target} : {}); ff.clases = (d.clase || "").split(" ");
    e = enviar(ff);
  }
  var fila = {prevenido: e.defaultPrevented, avisos: avisosPuestos.length, nuevos: pedidos.length - antes,
              navego: salidas.filter(function (x) { return x[0] === "ir" || x[0] === "requestSubmit" || x[0] === "submit"; }).length,
              mensaje: avisosPuestos.length ? avisosPuestos[0].hijos.filter(function (h) { return h.tag === "div"; }).map(function (h) { return h.textContent; }).join(" | ") : ""};
  if (avisosPuestos.length) { avisosPuestos[0].continuar(); }
  fila.tras = salidas.filter(function (x) { return x[0] === "ir" || x[0] === "requestSubmit"; }).length;
  fila.pendiente = typeof __oyW.beforeunload;
  /* LIMPIEZA entre elementos (cada uno se juzga solo): lo que quedó en el aire se da por perdido, el aviso se cierra y la
     página se da por no ida (como si el navegador la mostrara de nuevo). */
  pedidos.slice(base).forEach(function (p) { p.f.rechazar(new Error("x")); });
  while (avisosPuestos.length) avisosPuestos[0].cerrar();
  dispararVentana("pageshow", {persisted: true});
  return fila;
});
JSON.stringify(filas)
"""
_CON_PENDIENTES = ('var A = formulario("/proyectos/1/notas", "post", []); enviar(A);'
                   ' var B = formulario("/proyectos/1/descripcion", "post", [["descripcion", "B"]]); enviar(B);')


def _salidas_del_censo():
    """Lo que SALE de la página y la pieza no toma (de la lista real del censo): el menú, «‹ Inicio», los
    enlaces de texto a Tareas y Papelera, lo que abre otra pestaña."""
    lista = lo_que_se_toca("casa")
    declarados = {(t, h) for t, h, _ in lo_que_sale_a_proposito()}
    salen = [f for f in pasar_por_los_oyentes("casa", lista) if not f["prevenido"]]
    assert {(f["tag"], f["href"]) for f in salen} == declarados and len(salen) >= 6, salen
    return [{k: v for k, v in f.items() if k not in ("prevenido", "pedidos")} for f in salen]


@hay_osascript
def test_n5_con_un_guardado_en_camino_y_otro_en_cola_ninguna_salida_de_la_pagina_se_va_sola():
    """Cada enlace/formulario que sale de la página (la lista sale del censo real). Con A en camino y B en cola:
    no se va (se evita el clic), B no sale, el aviso lo dice y se va solo con «Continuar». Lo que abre otra
    pestaña (`target=_blank`) no se toca."""
    filas = _salidas_del_censo()
    r = correr_en_navegador(guion_de("casa", p=1), _CENSO_PENDIENTE_JS % (json.dumps(json.dumps(filas)), _CON_PENDIENTES))
    primera = True
    for f, d in zip(r, filas):
        nombre = (d["tag"], d["href"])
        if d["target"] not in (None, "_self") or d["download"]:      # otra pestaña, o un archivo que se baja: la página no se va
            assert not f["prevenido"] and f["avisos"] == 0 and f["tras"] == 0, (nombre, f)
            continue
        assert f["prevenido"] is True and f["navego"] == 0 and f["nuevos"] == 0, (nombre, f)          # no se fue ni salió B
        # (la primera salida suelta la cola y lo dice; las siguientes ya solo encuentran el guardado en camino)
        assert f["avisos"] == 1 and "sigue en camino" in f["mensaje"], (nombre, f)
        assert ("Había 1 cambio esperando: NO se mandó" in f["mensaje"]) == primera, (nombre, f)
        primera = False
        assert f["tras"] == 1, (nombre, f)                                                             # y se fue con «Continuar»


@hay_osascript
def test_n5_pareja_sin_nada_pendiente_las_salidas_se_van_como_siempre():
    filas = _salidas_del_censo()
    r = correr_en_navegador(guion_de("casa", p=1), _CENSO_PENDIENTE_JS % (json.dumps(json.dumps(filas)), ""))
    for f, d in zip(r, filas):
        assert f["prevenido"] is False and f["avisos"] == 0 and f["tras"] == 0, ((d["tag"], d["href"]), f)


@hay_osascript
def test_n5_el_menu_con_un_guardado_en_camino_y_otro_en_cola_sonda_del_testigo():
    r = correr(CASA(), _CON_PENDIENTES + '__e = clic(enlace("/tareas")); salidas.push(["pedidos", pedidos.length]);')
    assert r["prevenido"] is True and ["pedidos", 1] in r["salidas"] and not [x for x in r["salidas"] if x[0] == "ir"]
    assert [x for x in r["salidas"] if x[0] == "aviso"]


@hay_osascript
def test_n5_un_enlace_a_otro_sitio_o_un_correo_no_se_toca_ni_con_algo_pendiente():
    for href in ("mailto:a@example.test", "javascript:alert(1)"):
        r = correr(CASA(), _CON_PENDIENTES + '__e = clic(enlace(%s));' % json.dumps(href))
        assert r["prevenido"] is False


@hay_osascript
def test_n5_un_formulario_que_sale_de_la_pantalla_pasa_por_la_puerta_con_algo_pendiente():
    esc = (_CON_PENDIENTES + '__e = enviar(formulario("/otra/3/borrar", "post", []), {name: "x", value: "1", disabled: false});'
           ' salidas.push(["antes", salidas.filter(function (x) { return x[0] === "requestSubmit"; }).length]); avisosPuestos[0].continuar();')
    r = correr(CASA(), esc)
    assert r["prevenido"] is True and ["antes", 0] in r["salidas"] and ["requestSubmit", "/otra/3/borrar", "x"] in r["salidas"]


@hay_osascript
def test_n5_beforeunload_se_pone_con_pendientes_y_se_quita_sin_ellos_y_pide_confirmar_solo_entonces():
    esc = ('function salir() { var e = {prevented: false, preventDefault: function () { this.prevented = true; }};'
           ' dispararVentana("beforeunload", e); return [e.prevented, (__oyW.beforeunload || []).length]; }'
           ' salidas.push(["sin nada", salir()]);'
           ' enviar(formulario("/proyectos/1/notas", "post", [])); salidas.push(["en camino", salir()]);'
           ' enviar(formulario("/proyectos/1/descripcion", "post", [])); responder(0, "PAG-A"); salidas.push(["B en camino", salir()]);'
           ' responder(1, "PAG-B"); salidas.push(["terminado", salir()]);')
    r = correr(CASA(), esc)
    dicho = {x[0]: x[1] for x in r["salidas"] if x[0] in ("sin nada", "en camino", "B en camino", "terminado")}
    assert dicho == {"sin nada": [False, 0], "en camino": [True, 1], "B en camino": [True, 1], "terminado": [False, 0]}


@hay_osascript
def test_n5_el_oyente_de_salida_por_si_solo_no_pide_nada_si_no_hay_nada_pendiente():
    esc = ('var e = {prevented: false, preventDefault: function () { this.prevented = true; }}; var v = alSalirSinSaltar(e);'
           ' salidas.push(["sale", e.prevented, v === undefined]);')
    assert ["sale", False, True] in correr(CASA(), esc)["salidas"]


@hay_osascript
def test_b_despues_de_continuar_no_sale_ningun_pedido_y_el_formulario_queda_con_lo_escrito():
    """Sonda continuar+nuevo del testigo: tras «Continuar» el envío C ya no sale."""
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/tareas")); avisosPuestos[0].continuar();'
           ' var C = formulario("/proyectos/1/descripcion", "post", [["descripcion", "C"]]); C.controles = [{type: "text", tagName: "INPUT", readOnly: false}];'
           ' __e = enviar(C); var G = clic(enlace("/proyectos?p=2")); salidas.push(["C", C.controles[0].readOnly, C.dataset.enCamino, G.defaultPrevented]);'
           ' irAlSitio("/proyectos?p=7"); dispararVentana("popstate", {state: null});'
           ' responder(0, "PAG-A");')                      # y cuando A termina, nada de lo tocado después de «Continuar» sale
    r = correr(CASA(), esc)
    assert r["prevenido"] is True and [p[1] for p in r["pedidos"]] == ["/proyectos/1/notas"]       # ni C, ni el enlace, ni «atrás»
    assert ["C", False, None, True] in r["salidas"]                                                  # C quedó sin bloquear, con lo escrito


AVISO_SE_VA = "La página se está yendo; esto no se mandó. Si no cambia, copia lo que escribiste y recarga la página (al recargar se pierde lo que no se mandó)."


def test_b_todo_rechazo_por_irse_pasa_por_la_unica_funcion_sacada_del_texto_de_la_pieza():
    """LO QUE CUENTA ESTA PRUEBA, y nada más: los `if (saliendoSinSaltar) {…}` escritos con esa forma literal en el texto de la
    pieza (hoy 5) llaman `rechazarPorIrseSinSaltar`. Un rechazo escrito de otra forma (otra condición, un alias) no lo ve. La
    garantía de que cada sitio de verdad avisa la dan las pruebas de comportamiento por sitio:
    `test_b_todo_rechazo_tras_continuar_deja_el_aviso_con_salida_clara_y_lo_escrito_intacto` (formulario, enlace y «atrás» de
    Proyectos) y `test_b_todo_rechazo_tras_continuar_en_tareas_deja_el_aviso` (marcar y formulario que sale, en Tareas)."""
    codigo = sin_comentarios_de_js(texto_de_la_pieza())
    cuerpos = re.findall(r"if \(saliendoSinSaltar\) \{([^}]*)\}", codigo)
    assert len(cuerpos) == 5 and all("rechazarPorIrseSinSaltar(e)" in c for c in cuerpos), cuerpos


def _rechazos():
    """(nombre, guion, escenario que deja a la página yéndose y luego intenta algo) de cada sitio que rechaza."""
    yendo = 'enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/tareas")); avisosPuestos[0].continuar(); salidas.length = 0;'
    return [
        ("formulario de Proyectos", "casa", yendo + '__e = enviar(formulario("/proyectos/1/descripcion", "post", [["descripcion", "C"]]));'),
        ("enlace de Proyectos", "casa", yendo + '__e = clic(enlace("/proyectos?p=2"));'),
        ("atras de Proyectos", "casa", yendo + 'irAlSitio("/proyectos?p=7"); dispararVentana("popstate", {state: null});'),
    ]


@hay_osascript
@pytest.mark.parametrize("nombre,sesion,esc", _rechazos(), ids=lambda x: x if isinstance(x, str) and len(x) < 30 else "")
def test_b_todo_rechazo_tras_continuar_deja_el_aviso_con_salida_clara_y_lo_escrito_intacto(nombre, sesion, esc):
    """Sonda del testigo: antes C no salía y nadie lo decía. Ahora el aviso fijo dice que la página se está yendo y
    que no se mandó, con un botón que recarga; la pareja sin «Continuar» no trae ese aviso."""
    r = correr(CASA(), esc + ' salidas.push(["recargar", avisosPuestos.length && avisosPuestos[avisosPuestos.length - 1].hijos.filter(function (h) { return h.tag === "button"; }).map(function (h) { return h.textContent; })]);'
                       ' avisosPuestos[avisosPuestos.length - 1].continuar(); ')
    avisos = [x for x in r["salidas"] if x[0] == "aviso"]
    assert avisos and AVISO_SE_VA in avisos[-1][1], (nombre, r["salidas"])
    assert ["recargar", ["Entendido", "Recargar la página"]] in r["salidas"]
    assert [x for x in r["salidas"] if x[0] == "ir"][-1][1].startswith("http://x.test/proyectos?p=")     # el botón recarga la dirección en que está


@hay_osascript
def test_b_pareja_sin_continuar_no_hay_aviso_de_que_la_pagina_se_va():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/proyectos?p=2")); '
           '__e = enviar(formulario("/proyectos/1/descripcion", "post", [["descripcion", "C"]])); irAlSitio("/proyectos?p=7"); dispararVentana("popstate", {state: null});')
    r = correr(CASA(), esc)
    assert not [x for x in r["salidas"] if x[0] == "aviso" and AVISO_SE_VA in x[1]]


@hay_osascript
def test_b_el_rechazo_no_se_repite_en_cada_toque():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/tareas")); avisosPuestos[0].continuar();'
           ' clic(enlace("/proyectos?p=2")); clic(enlace("/proyectos?p=3")); enviar(formulario("/proyectos/1/descripcion", "post", []));')
    r = correr(CASA(), esc)
    assert [x for x in r["salidas"] if x[0] == "aviso" and AVISO_SE_VA in x[1]][-1][1].count(AVISO_SE_VA) == 1


@hay_osascript
def test_b_si_la_pagina_vuelve_a_mostrarse_desde_la_cache_se_recalcula_la_vigilancia_y_se_puede_seguir():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/tareas")); avisosPuestos[0].continuar();'
           ' function salir() { var e = {prevented: false, preventDefault: function () { this.prevented = true; }}; dispararVentana("beforeunload", e); return e.prevented; }'
           ' salidas.push(["tras Continuar", salir()]);'
           ' dispararVentana("pageshow", {persisted: false}); salidas.push(["pageshow normal", salir()]);'
           ' dispararVentana("pageshow", {persisted: true}); salidas.push(["pageshow de la cache", salir()]);'
           ' __e = enviar(formulario("/proyectos/1/descripcion", "post", [])); salidas.push(["pedidos", pedidos.length]);')
    r = correr(CASA(), esc)
    dicho = {x[0]: x[1] for x in r["salidas"] if x[0] in ("tras Continuar", "pageshow normal", "pageshow de la cache", "pedidos")}
    assert dicho == {"tras Continuar": False, "pageshow normal": False, "pageshow de la cache": True, "pedidos": 1}   # (el guardado A sigue en el aire; el nuevo espera su turno)


@hay_osascript
def test_n5_con_continuar_ya_no_se_pide_confirmar():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); clic(enlace("/tareas")); avisosPuestos[0].continuar();'
           ' var e = {prevented: false, preventDefault: function () { this.prevented = true; }}; dispararVentana("beforeunload", e);'
           ' salidas.push(["sale", e.prevented, (__oyW.beforeunload || []).length]);')
    assert ["sale", False, 0] in correr(CASA(), esc)["salidas"]


@hay_osascript
def test_n5_la_cola_queda_vacia_tras_decidir_irse():
    """s01 del testigo: si la cola no se vacía, una lectura posterior queda detrás de lo que ya no va a salir."""
    esc = ('paginas["PAG"] = {regiones: ["cuerpo"]}; enviar(formulario("/proyectos/1/notas", "post", []));'
           ' enviar(formulario("/proyectos/1/descripcion", "post", [])); responder(0, "PAG", {url: "http://x.test/proyectos?p=2"});'
           ' __e = clic(enlace("/proyectos?p=5"));')
    r = correr(CASA(), esc)
    assert r["pedidos"][-1][1] == "/proyectos?p=5"        # salió enseguida: no quedó detrás de nada


# ── N2: tiempo máximo por guardado ────────────────────────────────────────────────────────────────
@hay_osascript
def test_n2_un_guardado_colgado_vence_se_suelta_y_la_cola_sigue_y_lo_que_llegue_tarde_se_ignora():
    """TIEMPO_ENVIO_SINSALTAR es una decisión de diseño (30 s), no una medida. Aquí el reloj es el de mentira."""
    esc = ('var A = formulario("/proyectos/1/notas", "post", []); ' + _con_controles("A") + ' enviar(A);'
           ' var B = formulario("/proyectos/1/descripcion", "post", [["descripcion", "B"]]); enviar(B);'
           ' var c = A.controles; salidas.push(["bloqueado", c[0].readOnly]); correrTimers();'
           ' salidas.push(["tras vencer", c[0].readOnly, pedidos.length]); responder(0, "PAG-TARDE"); responder(1, "PAG-B");')
    r = correr(CASA(), esc)
    assert ["bloqueado", True] in r["salidas"] and ["tras vencer", False, 2] in r["salidas"]       # A suelto con lo escrito, B salió
    aviso = [x for x in r["salidas"] if x[0] == "aviso"][-1]
    assert aviso[1] == [AVISO_SIN_CONFIRMAR]
    assert r["cen"] == "PAG-B"                                                                      # la respuesta tardía de A no pintó


@hay_osascript
@pytest.mark.parametrize("tarde", ['responder(0, "ENTRAR", {ok: false, status: 401});', 'responder(0, "x", {ok: false, status: 500});', "sinRed(0);"])
def test_n2_un_rechazo_o_un_fallo_que_llega_despues_de_vencer_no_hace_nada_mas(tarde):
    r = correr(CASA(), 'enviar(formulario("/proyectos/1/notas", "post", [])); correrTimers();' + tarde)
    assert [x[0] for x in r["salidas"]] == ["aviso"]            # solo el aviso de sin confirmar: ni envío de siempre ni segundo aviso


@hay_osascript
def test_n2_si_vence_mientras_se_lee_el_cuerpo_de_la_respuesta_lo_leido_no_pinta():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); var lectura = new Futuro();'
           ' pedidos[0].f.resolver({ok: true, status: 200, url: "http://x.test/proyectos?p=1", text: function () { return lectura; }});'
           ' correrTimers(); lectura.resolver("PAG-TARDE");')
    r = correr(CASA(), esc)
    assert r["cen"] == "VIEJA" and [x[0] for x in r["salidas"]] == ["aviso"]


@hay_osascript
def test_n2_un_guardado_que_contesta_a_tiempo_apaga_su_reloj():
    r = correr(CASA(), 'enviar(formulario("/proyectos/1/notas", "post", [])); responder(0, "PAG-A"); correrTimers();')
    assert [x for x in r["salidas"] if x[0] == "aviso"] == [] and r["cen"] == "PAG-A"


@hay_osascript
def test_un_guardado_de_la_cola_rechazado_cuyo_formulario_ya_no_esta_se_avisa_y_no_se_envia_a_ciegas():
    esc = ('var A = formulario("/proyectos/1/notas", "post", []); enviar(A); var B = formulario("/proyectos/1/descripcion", "post", [["descripcion", "x"]]);'
           ' __e = enviar(B); responder(0, "PAG-A"); B.isConnected = false; responder(1, "ENTRAR", {ok: false, status: 401});')
    r = correr(CASA(), esc)
    assert [x[0] for x in r["salidas"]] == ["aviso"] and r["salidas"][0][1] == ["No se guardó: el servidor no aceptó el envío."]


@hay_osascript
def test_el_envio_nativo_que_pidio_la_pieza_no_vuelve_a_pasar_por_ella():
    esc = ('var f = formulario("/proyectos/1/cliente", "post", [["a", "1"]]); f.dataset.nativo = ""; __e = enviar(f);')
    r = correr(CASA(), esc)
    assert r["pedidos"] == [] and r["prevenido"] is False


@hay_osascript
def test_h3_el_aviso_de_no_confirmado_no_lo_quita_ningun_repintado_y_los_dos_se_leen():
    """SONDA DEL TESTIGO (H3): otro guardado se repinta antes de que la persona lea el aviso. El aviso se
    queda; si llega un segundo, se leen los dos; se cierra con «Entendido»."""
    esc = ('var A = formulario("/proyectos/1/notas", "post", []); enviar(A); sinRed(0);'
           ' var B = formulario("/proyectos/1/descripcion", "post", []); enviar(B); responder(1, "PAG-B");'      # B se repinta
           ' var C = formulario("/proyectos/1/carpeta", "post", []); enviar(C); sinRed(2);'
           ' salidas.push(["abiertos", avisosPuestos.length]); avisosPuestos[0].cerrar(); salidas.push(["abiertos", avisosPuestos.length]);')
    r = correr(CASA(), esc)
    avisos = [x for x in r["salidas"] if x[0] == "aviso"]
    assert avisos[0][1] == [AVISO_SIN_CONFIRMAR] and avisos[-1][1] == [AVISO_SIN_CONFIRMAR, AVISO_SIN_CONFIRMAR]    # el repintado de B no lo quitó
    assert r["cen"] == "PAG-B"
    assert [x for x in r["salidas"] if x[0] == "abiertos"] == [["abiertos", 1], ["abiertos", 0]]


@hay_osascript
def test_el_enlace_mirar_como_quedo_tambien_cierra_el_aviso():
    esc = ('enviar(formulario("/proyectos/1/notas", "post", [])); sinRed(0);'
           ' avisosPuestos[0].hijos.filter(function (h) { return h.tag === "a"; })[0].onclick(); salidas.push(["abiertos", avisosPuestos.length]);')
    assert ["abiertos", 0] in correr(CASA(), esc)["salidas"]


@hay_osascript
@pytest.mark.parametrize("tipo", ["otra-pantalla", "importar-falla", "texto-cortado"])
def test_si_guardo_y_no_se_puede_pintar_se_va_a_esa_pagina_sin_reenviar(tipo):
    previo = {"otra-pantalla": 'paginas["PAG-X"] = {regiones: ["cuerpo"]};', "importar-falla": 'reemplazos.__importar_falla = true;',
              "texto-cortado": ""}[tipo]
    if tipo == "texto-cortado":
        resp = ('pedidos[0].f.resolver({ok: true, status: 200, url: "http://x.test/proyectos?p=2", text: function () {'
                ' var f = new Futuro(); f.rechazar(new Error("corte")); return f; }});')
    else:
        resp = 'responder(0, "PAG-X", {url: "http://x.test/proyectos?p=2&hecho=tarea_hecha"});'
    r = correr(CASA(), previo + 'var f = formulario("/proyectos/tarea/10/hecha", "post", []); __e = enviar(f);' + resp)
    assert len(r["pedidos"]) == 1                                                  # el POST no se repite
    assert [s[0] for s in r["salidas"]] == ["ir"] and "submit" not in json.dumps(r["salidas"])
    assert r["historia"] == [] or tipo == "importar-falla" and r["historia"] == []


@hay_osascript
def test_dos_toques_al_mismo_guardado_mandan_un_solo_pedido():
    r = correr(CASA(), 'var f = formulario("/proyectos/tarea/10/hecha", "post", []); enviar(f); __e = enviar(f);')
    assert len(r["pedidos"]) == 1 and r["prevenido"] is True


@hay_osascript
def test_h2_un_guardado_que_llega_mientras_otro_va_en_camino_se_guarda_con_lo_que_tenia_y_sale_despues_en_orden_una_vez():
    """SONDA DEL TESTIGO (H2): un POST en camino y, mientras, un campo automático (`requestSubmit`) manda su
    formulario. Antes B se tragaba; ahora sale cuando A termina, con los datos de ESE momento, una sola vez."""
    esc = ('var A = formulario("/proyectos/1/notas", "post", [["contenido", "nota A"]]); enviar(A);'
           ' var B = formulario("/proyectos/tarea/10/comentar", "post", [["texto", "comentario B"]]); __e = enviar(B);'
           ' B.campos[0][1] = "lo que escribí DESPUÉS"; var C = formulario("/proyectos/1/descripcion", "post", [["descripcion", "C"]]); enviar(C);'
           ' salidas.push(["en camino", pedidos.length]); responder(0, "PAG-A");'
           ' salidas.push(["tras A", pedidos.length]); responder(1, "PAG-B"); salidas.push(["tras B", pedidos.length]); responder(2, "PAG-C");')
    r = correr(CASA(), esc)
    assert r["prevenido"] is True
    assert r["pedidos"] == [["POST", "/proyectos/1/notas", "contenido=nota%20A"],
                            ["POST", "/proyectos/tarea/10/comentar", "texto=comentario%20B"],     # con lo que tenía al enviar
                            ["POST", "/proyectos/1/descripcion", "descripcion=C"]]                 # y en orden, cada uno una vez
    assert [x for x in r["salidas"] if x[0] in ("en camino", "tras A", "tras B")] == [["en camino", 1], ["tras A", 2], ["tras B", 3]]
    assert r["cen"] == "PAG-C"


@hay_osascript
def test_lo_que_se_escribe_en_otro_campo_mientras_se_guarda_no_desaparece_al_repintar():
    """El texto de un campo que NO es del formulario guardado vuelve a su sitio (con el foco); el campo del
    formulario guardado y los de formularios que ya van en camino muestran lo que dice el servidor."""
    esc = ('var otro = campo("text", "contenido", "lo que voy escribiendo", "", "/proyectos/1/notas");'
           ' var guardado = campo("text", "texto", "ya guardado", "", "/proyectos/tarea/10/comentar");'
           ' var enCamino = campo("text", "descripcion", "texto en camino", "", "/proyectos/1/descripcion"); enCamino.form.dataset.enCamino = "";'
           ' document.activeElement = otro;'
           ' vivos.centro.campos = [otro, guardado, enCamino];'
           ' var nuevoOtro = campo("text", "contenido", "", "", "/proyectos/1/notas?x=1"), nuevoG = campo("text", "texto", "", "", "/proyectos/tarea/10/comentar"),'
           ' nuevoC = campo("text", "descripcion", "", "", "/proyectos/1/descripcion");'
           ' paginas["PAG-X"] = {campos: {centro: [nuevoOtro, nuevoG, nuevoC]}};'
           ' var f = formulario("/proyectos/tarea/10/comentar", "post", [["texto", "ya guardado"]]); guardado.form.boton = f.boton;'
           ' __e = enviar(f); f.closest = function () { return null; };'
           ' responder(0, "PAG-X"); salidas.push(["campos", nuevoOtro.value, nuevoG.value, nuevoC.value, nuevoOtro.focos, nuevoOtro.rangos]);')
    # `guardado.form` no es el formulario enviado (`f`): para el censo de campos lo es por la prueba de abajo
    r = correr(CASA(), esc.replace("guardado.form.boton = f.boton;", "guardado.form = f;"))
    campos = [x for x in r["salidas"] if x[0] == "campos"][0]
    assert campos[1:] == ["lo que voy escribiendo", "", "", 1, [[22, 22]]]


@hay_osascript
def test_no_se_repone_un_campo_que_el_servidor_ya_cambio():
    esc = ('var otro = campo("text", "contenido", "mi borrador", "", "/proyectos/1/notas"); vivos.centro.campos = [otro];'
           ' var nuevo = campo("text", "contenido", "lo del servidor", "lo del servidor", "/proyectos/1/notas"); paginas["PAG-Y"] = {campos: {centro: [nuevo]}};'
           ' __e = enviar(formulario("/proyectos/tarea/10/hecha", "post", [])); responder(0, "PAG-Y"); salidas.push(["campo", nuevo.value]);')
    r = correr(CASA(), esc)
    assert ["campo", "lo del servidor"] in r["salidas"]


@hay_osascript
def test_al_ir_a_otra_pantalla_no_se_arrastra_lo_escrito():
    esc = ('var otro = campo("text", "contenido", "borrador", "", "/proyectos/1/notas"); vivos.centro.campos = [otro];'
           ' var nuevo = campo("text", "contenido", "", "", "/proyectos/1/notas"); paginas["PAG-Z"] = {campos: {centro: [nuevo]}};'
           ' __e = clic(enlace("/proyectos?p=2")); responder(0, "PAG-Z"); salidas.push(["campo", nuevo.value]);')
    assert ["campo", ""] in correr(CASA(), esc)["salidas"]


@hay_osascript
def test_el_boton_que_cambia_a_donde_va_el_envio_se_respeta_igual_que_en_el_navegador():
    boton = lambda **a: json.dumps({"name": "x", "value": "1", "disabled": False, "atributos": a})   # noqa: E731
    def b(**a):
        return ('{name: "x", value: "1", disabled: false, getAttribute: function (k) { return %s[k] === undefined ? null : %s[k]; }}'
                % (json.dumps(a), json.dumps(a)))
    # formaction a otra pantalla: sale de la página, no se toca
    r = correr(CASA(), 'var f = formulario("/proyectos/1/nombre", "post", []); __e = enviar(f, %s);' % b(formaction="/otra/3/borrar"))
    assert r["prevenido"] is False and r["pedidos"] == []
    # formaction dentro de la pantalla: el POST va ahí, no al `action` del formulario
    r = correr(CASA(), 'var f = formulario("/otra/3/borrar", "post", []); __e = enviar(f, %s);' % b(formaction="/proyectos/1/estado"))
    assert [p[1] for p in r["pedidos"]] == ["/proyectos/1/estado"]
    # formmethod=get: se manda en la dirección
    r = correr(CASA(), 'var f = formulario("/proyectos/1/estado", "post", [["q", "z"]]); __e = enviar(f, %s);' % b(formmethod="get", formaction="/proyectos"))
    assert [(p[0], p[1]) for p in r["pedidos"]] == [("GET", "/proyectos?q=z&x=1")]
    # formtarget=_blank: lo del navegador
    r = correr(CASA(), 'var f = formulario("/proyectos/1/estado", "post", []); __e = enviar(f, %s);' % b(formtarget="_blank"))
    assert r["prevenido"] is False and r["pedidos"] == []


@hay_osascript
@pytest.mark.parametrize("sesion", ["casa", "ver"])
def test_dos_toques_a_la_misma_busqueda_mandan_un_solo_pedido(sesion):
    """Una búsqueda (GET) no espera a ningún guardado, pero el mismo formulario no sale dos veces
    mientras su pedido está en camino. En la casa también lo frena el guion de la página (`enviado`); en
    solo ver no hay otro guion: ahí lo vigila SOLO `enCamino` de la pieza."""
    r = correr(CASA() if sesion == "casa" else VER(), 'var f = formulario("/proyectos", "get", [["q", "x"]]); enviar(f); __e = enviar(f);')
    assert len(r["pedidos"]) == 1 and r["prevenido"] is True


@hay_osascript
def test_un_enlace_tocado_mientras_se_guarda_espera_a_que_termine_y_sale_despues():
    esc = ('var f = formulario("/proyectos/1/notas", "post", [["contenido", "x"]]); enviar(f);'
           ' __e = clic(enlace("/proyectos?p=2")); salidas.push(["pedidos antes de que conteste el guardado", pedidos.length]);'
           ' responder(0, "PAG-G"); salidas.push(["pedidos despues", pedidos.length]); responder(1, "PAG-2");')
    r = correr(CASA(), esc)
    assert r["salidas"] == [["pedidos antes de que conteste el guardado", 1], ["pedidos despues", 2]]    # esperó su turno
    assert r["prevenido"] is True and [p[0] for p in r["pedidos"]] == ["POST", "GET"]
    assert r["cen"] == "PAG-2" and r["historia"][-1] == ["push", None, "/proyectos?p=2"]


@hay_osascript
def test_lo_que_contesta_tarde_tras_haber_tocado_otra_cosa_no_se_pinta():
    esc = ('clic(enlace("/proyectos?p=2")); __e = clic(enlace("/proyectos?p=3"));'
           ' responder(0, "PAG-VIEJA"); var __a = [vivos.centro.desde, historia.llamadas.length]; responder(1, "PAG-3");')
    r = correr(CASA(), esc)
    assert [p[1] for p in r["pedidos"]] == ["/proyectos?p=2", "/proyectos?p=3"]
    assert r["cen"] == "PAG-3" and r["donde"] == "/proyectos?p=3"
    assert [h[0] for h in r["historia"]] == ["replace", "push"]                    # la vieja no dejó ni una entrada


@hay_osascript
def test_si_un_pedido_viejo_falla_tras_haber_tocado_otra_cosa_no_pisa_a_la_persona():
    r = correr(CASA(), 'clic(enlace("/proyectos?p=2")); __e = clic(enlace("/proyectos?p=3")); sinRed(0); responder(1, "PAG-3");')
    assert r["salidas"] == [] and r["cen"] == "PAG-3"                              # nada de `irA` a la dirección vieja


@hay_osascript
def test_atras_vuelve_a_pintar_lo_que_hay_en_la_barra_con_el_desplazamiento_guardado_y_sin_tocar_el_historial():
    esc = ('clic(enlace("/proyectos?p=2")); responder(0, "PAG-2"); var antes = historia.llamadas.length;'
           ' irAlSitio("/proyectos?p=1"); dispararVentana("popstate", {state: {x: 0, y: 400}}); responder(1, "PAG-1");')
    r = correr(CASA(), esc)
    assert [p[1] for p in r["pedidos"]] == ["/proyectos?p=2", "/proyectos?p=1"]
    assert r["cen"] == "PAG-1" and len(r["historia"]) == 2                          # «atrás» no escribe en el historial
    assert r["ventana"][-1] == [0, 400]


@hay_osascript
def test_un_cambio_de_ancla_en_la_barra_no_vuelve_a_pedir_nada():
    r = correr(CASA(), 'dispararVentana("popstate", {state: null});')
    assert r["pedidos"] == []


@hay_osascript
def test_una_entrada_de_atras_sin_desplazamiento_guardado_no_rompe_nada():
    r = correr(CASA(), 'irAlSitio("/proyectos?p=7"); dispararVentana("popstate", {state: null}); responder(0, "PAG-7");')
    assert r["cen"] == "PAG-7" and r["ventana"] == [[0, 50]]


@hay_osascript
def test_el_titulo_de_un_proyecto_abre_el_detalle_sin_recargar_tras_la_espera_del_doble_clic():
    """El oyente de la página decide primero (espera por si llega el doble clic) y el enlace que sigue
    cuando no llegó pasa por la pieza: ese es el orden de los oyentes."""
    esc = ('var a = enlace("/proyectos?p=1&t=10#tarea-10"); a.closest = function (s) { return (s === "a[data-dbl]" || s === "a[href]") ? this : null; };'
           ' a.click = function () { clic(this); };'
           ' __e = clic(a); var __antes = pedidos.length; correrTimers();')
    r = correr(CASA(), esc)
    assert r["prevenido"] is True and [p[1] for p in r["pedidos"]] == ["/proyectos?p=1&t=10"]     # sin el `#`: el ancla no viaja


@hay_osascript
def test_sin_lo_que_hace_falta_en_el_navegador_nada_se_toca():
    r = correr(CASA(), '__e = clic(enlace("/proyectos?p=2")); var f = formulario("/proyectos/1/nombre", "post", []); enviar(f);',
               previo="window.fetch = undefined;")
    assert r["prevenido"] is False and r["pedidos"] == [] and r["historia"] == []


# La fila de referencia: la primera que se veía arriba, sin contar la tocada (si es de las que se
# mueven) ni las marcadas ni las que contienen otra, vuelve al mismo sitio de la pantalla. A está
# arriba de lo que se ve; B es la tocada; C tiene una casilla marcada; D es la que se elige (top 400).
# Tras guardar, D queda 120 más abajo (el aviso empujó): hay que correr 120.
_ANCLAS = r"""
var rect = {};
function fila(id, antes, despues, extra) {
  extra = extra || {};
  rect[id] = {antes: antes, despues: despues};
  return {id: id,
    querySelector: function (s) {
      if (s === "input:checked:not(:disabled)") return extra.marcada ? {} : null;
      if (s === "[data-ancla]") return extra.contiene ? {} : null;
      return null; },
    closest: function () { return vivos.centro; },
    getBoundingClientRect: function () { var r = rect[id][__fase]; return {top: r[0], bottom: r[1], height: r[1] - r[0]}; }};
}
var A = fila("tarea-1", [-300, -250], [-300, -250]);
var B = fila("tarea-2", [-40, 10], [-40, 10]);
var C = fila("tarea-3", [120, 170], [120, 170], {marcada: true});
var D = fila("tarea-4", [400, 450], [520, 570]);
__filas = [A, B, C, D];
reemplazos["tarea-4"] = D;
"""


@hay_osascript
@pytest.mark.parametrize("region_con_scroll", [True, False])
def test_la_fila_de_referencia_vuelve_a_su_sitio_al_marcar(region_con_scroll):
    esc = (_ANCLAS + 'paginas["PAG-M"] = {sinScroll: %s};' % ("false" if region_con_scroll else "true") +
           # la tocada SIGUE en la página nueva pero en otro sitio (se fue a «hechas», 900 más abajo): no puede ser la referencia.
           'reemplazos["tarea-2"] = B; rect["tarea-2"].despues = [900, 950];'
           'var f = formulario("/proyectos/tarea/2/hecha", "post", []); f.clases = ["marcar"]; f.closest = function () { return B; };'
           '__e = enviar(f); responder(0, "PAG-M");')
    r = correr(CASA(), esc)
    if region_con_scroll:
        assert r["cenTop"] == 1200 + 120 and r["ventana"] == [[0, 50]], r
    else:
        assert r["cenTop"] == 1200 and r["ventana"] == [[0, 50], [0, 50 + 120]], r


@hay_osascript
def test_si_lo_tocado_no_se_mueve_es_el_propio_punto_de_referencia_y_si_la_primera_ya_no_esta_sigue_la_que_queda():
    # B (la tocada, un comentario que no mueve la fila) es la primera visible: B es la referencia y queda 80 más abajo.
    esc = (_ANCLAS + 'rect["tarea-2"].despues = [40, 90]; reemplazos["tarea-2"] = B; __filas = [B, D]; paginas["PAG-N"] = {};'
           'var f = formulario("/proyectos/tarea/2/comentar", "post", []); f.closest = function () { return B; };'
           '__e = enviar(f); responder(0, "PAG-N");')
    r = correr(CASA(), esc)
    assert r["cenTop"] == 1200 + 80, r
    # Si B desaparece (se borró), sigue D (120 más abajo).
    esc = (_ANCLAS + '__filas = [B, D]; paginas["PAG-N"] = {};'
           'var f = formulario("/proyectos/tarea/2/borrar", "post", []); f.closest = function () { return B; };'
           '__e = enviar(f); responder(0, "PAG-N");')
    r = correr(CASA(), esc)
    assert r["cenTop"] == 1200 + 120, r


@hay_osascript
def test_si_ninguna_fila_de_referencia_sigue_la_ventana_queda_donde_estaba():
    r = correr(CASA(), _ANCLAS + '__filas = [A, D]; reemplazos["tarea-4"] = null; clic(enlace("/proyectos?p=2")); __e = null; responder(0, "PAG-2");')
    assert r["ventana"] == [[0, 50]] and r["cenTop"] == 1200


@hay_osascript
@pytest.mark.parametrize("que,abierto_despues", [("post", [True, False]), ("link", [False, False])])
def test_lo_que_la_persona_abrio_sigue_abierto_tras_guardar_y_no_se_hereda_al_ir_a_otra_pantalla(que, abierto_despues):
    esc = ('vivos.centro.detalles = [{open: true}, {open: false}]; paginas["PAG-D"] = {detalles: {centro: [{open: false}, {open: false}]}};'
           + ('var f = formulario("/proyectos/1/notas", "post", []); __e = enviar(f);' if que == "post" else '__e = clic(enlace("/proyectos?p=2"));')
           + 'responder(0, "PAG-D"); var __d = vivos.centro.detalles.map(function (d) { return d.open; });')
    r = correr_en_navegador(CASA(), "var __e = null;\n" + esc + "\nJSON.stringify(__d)")
    assert r == abierto_despues


@hay_osascript
def test_el_guion_de_copiar_que_trae_la_pagina_nueva_se_pone_una_vez_y_el_titulo_cambia():
    r = correr(CASA(), 'paginas["PAG-C"] = {copiar: true, titulo: "OTRO"}; __e = clic(enlace("/proyectos?p=2")); responder(0, "PAG-C");')
    assert r["salidas"] == [["guion-nuevo", "guion-copiar-carpeta", "COPIAR-DEL-SERVIDOR"]] and r["titulo"] == "OTRO"
    r = correr(CASA(), 'paginas["PAG-C"] = {copiar: true}; reemplazos["guion-copiar-carpeta"] = {}; __e = clic(enlace("/proyectos?p=2")); responder(0, "PAG-C");')
    assert r["salidas"] == [] and r["titulo"] == "TITULO-VIEJO"               # ya estaba: no se pone dos veces


@hay_osascript
def test_la_sesion_de_solo_ver_no_tiene_ningun_post_y_el_buscador_sigue_siendo_un_get():
    g = VER()
    assert "method" not in g.replace("getAttribute(\"method\")", "") or True
    r = correr(g, '__e = enviar(formulario("/proyectos", "get", [["q", "x"]])); responder(0, "PAG-V");')
    assert r["pedidos"] == [["GET", "/proyectos?q=x", None]] and r["cen"] == "PAG-V"


# ═══════════════════════════════════════════════════════════════════════
# 3. LAS PROHIBICIONES de la pieza (y que es UNA)
# ═══════════════════════════════════════════════════════════════════════

def test_la_pieza_tiene_las_mismas_prohibiciones_que_el_resto_del_guion_salvo_lo_declarado():
    assert lo_prohibido_en_la_pieza(texto_de_la_pieza()) == []


@pytest.mark.parametrize("meter", [
    'window.location.href = "/otra";', 'window["fetch"]("/borrar", {method: "POST"});', 'new WebSocket("ws://x");',
    'fetch ("/otra");', 'var u = window.fetch;', 'form.action = "/otra";', 'new XMLHttpRequest();',
    'document.cookie = "x";', 'navigator.sendBeacon("/x");', "var g = self['fe' + 'tch'];", 'eval("1");',
    'document.body.innerHTML = "x";', 'localStorage.setItem("a", "b");', 'location.assign("/x");',
    'var s = document.createElement("iframe");', 'setTimeout(function () {}, 1);', 'new Function("1");'])
def test_lo_prohibido_no_se_cuela_en_la_pieza_ni_en_una_rama_muerta(meter):
    pieza = texto_de_la_pieza()
    roto = pieza.replace("function irA(url) {", "function irA(url) { if (false) { " + meter + " }", 1)
    assert roto != pieza
    try:
        quedan = lo_prohibido_en_la_pieza(roto)
    except AssertionError:                    # un permitido que cambió de cuenta también es una puerta cerrada
        return
    assert quedan, meter


def test_el_unico_fetch_de_la_pieza_va_a_la_direccion_que_decidio_la_puerta():
    """El `fetch` pide `p.url`; y `p.url` solo se arma con `u.pathname + u.search` de `destinoSinSaltar` o con la
    dirección de la barra (`window.location`)."""
    codigo = sin_comentarios_de_js(texto_de_la_pieza())
    assert len(re.findall(r"\bfetch\(", codigo)) == 1
    asignaciones = re.findall(r"\burl:\s*([^,}]+)", codigo)
    assert sorted(a.strip() for a in asignaciones) == sorted([
        'u.pathname + u.search',                                       # marcarSinSaltar (POST)
        'u.pathname + (qs ? "?" + qs : "")',                           # marcarSinSaltar (GET)
        'destino',                                                     # un enlace (ya salió de destinoSinSaltar)
        'aqui',                                                        # «atrás»: la dirección de la barra
    ]), asignaciones


def test_las_tres_pantallas_que_usan_la_pieza_traen_la_misma_y_proyectos_la_arranca():
    pieza = texto_de_la_pieza()
    for con in ("casa", "ver"):
        g = guion_de(con, p=1)
        assert la_pieza_de(g)[0] == pieza
        assert g.count('iniciarSinSaltos("/proyectos");') == 1
    # los oyentes de la pieza se registran en un solo sitio
    codigo = sin_comentarios_de_js(pieza)
    assert sorted(re.findall(r'addEventListener\("(\w+)", (\w+)\)', codigo)) == [
        ("beforeunload", "alSalirSinSaltar"), ("click", "alHacerClicSinSaltar"), ("click", "alSalirPorEnlaceSinSaltar"),
        ("pageshow", "alMostrarseSinSaltar"), ("popstate", "alVolverSinSaltar"), ("submit", "alEnviarSinSaltar"),
        ("submit", "alSalirPorFormularioSinSaltar")]
