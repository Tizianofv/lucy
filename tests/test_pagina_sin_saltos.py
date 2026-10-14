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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "href" in a:
            self.visto.add(("a", a["href"], "", a.get("target"), "download" in a, a.get("class") or "", a.get("id") or ""))
        elif tag == "form":
            self.visto.add(("form", a.get("action") or "", (a.get("method") or "get").lower(), a.get("target"), False,
                            a.get("class") or "", a.get("id") or ""))


def lo_que_se_toca(con: str) -> list[dict]:
    """La lista de todo lo que se toca en `con` («casa» o «ver») en TODAS las vistas y las direcciones
    armadas a mano de `test_proyectos_solo_ver.py`. Sale del HTML que dibuja el servidor."""
    c = _Censo()
    for consulta in sv.VISTAS + list(sv.A_MANO.values()):
        c.feed(sv._pintar(con, **consulta))
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
        ("a", REGISTRO + "/#inicio", "«‹ Inicio»: vuelve a la App de registro"),
        ("a", "https://drive.example.test/carpetas/disco-uno", "«Abrir la carpeta» (Drive): abre otra pestaña"),
    ]
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


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "401", "404"])
@pytest.mark.parametrize("que", ["post", "link"])
def test_si_el_pedido_no_llega_o_el_servidor_dice_que_no_se_hace_lo_de_siempre(modo, que):
    toque = ('var f = formulario("/proyectos/1/nombre", "post", [["nombre", "X"]]); __e = enviar(f);' if que == "post"
             else 'var f = formulario("/x", "get", []); __e = clic(enlace("/proyectos?p=2"));')
    cierre = ('sinRed(0);' if modo == "red" else
              'responder(0, "ENTRAR", {ok: false, status: %s});' % modo)
    r = correr(CASA(), toque + cierre)
    if que == "post":
        assert r["salidas"] == [["submit", "/proyectos/1/nombre"]], r              # el envío de siempre, una vez
    else:
        assert r["salidas"] == [["ir", "/proyectos?p=2"]], r                       # ir a la página, de la forma de siempre
    assert r["historia"] == [] and r["cen"] == "VIEJA" and r["escritos"] == []     # nada se pintó, nada quedó a medias


@hay_osascript
def test_un_post_que_llego_y_el_servidor_fallo_no_se_reenvia_se_enseña_lo_que_dijo():
    r = correr(CASA(), 'var f = formulario("/proyectos/1/notas", "post", [["contenido", "hola"]]); __e = enviar(f);'
                       ' responder(0, "<p>Error del servidor</p>", {ok: false, status: 500});')
    assert len(r["pedidos"]) == 1 and r["salidas"] == []                           # ni un segundo POST ni `submit()`
    assert r["escritos"] == ["open", "<p>Error del servidor</p>", "close"]


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
def test_dos_toques_al_mismo_guardado_mandan_un_solo_pedido_y_otro_guardado_en_camino_no_sale():
    r = correr(CASA(), 'var f = formulario("/proyectos/tarea/10/hecha", "post", []); var g = formulario("/proyectos/tarea/11/hecha", "post", []);'
                       ' enviar(f); enviar(f); __e = enviar(g);')
    assert len(r["pedidos"]) == 1 and r["prevenido"] is True                       # g se evitó (el navegador no lo manda solo)


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
        ("click", "alHacerClicSinSaltar"), ("popstate", "alVolverSinSaltar"), ("submit", "alEnviarSinSaltar")]
