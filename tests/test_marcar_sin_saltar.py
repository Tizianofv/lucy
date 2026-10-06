"""Marcar o desmarcar una tarea en Proyectos no mueve lo que la persona está viendo
(Tiziano, 6-oct-2026: «cuando seleccione una tarea como realizada la pagina se me
mueve»).

LA CAUSA, medida en el navegador: el formulario del círculo hacía POST, el servidor
contestaba 303 a la página entera con `#tarea-N`, y el navegador la cargaba de cero:
el centro (`.envoltura > main`, con su propio desplazamiento) y la ventana quedaban
donde los dejara el ancla, no donde estaba la persona.

EL ARREGLO: el guion de la página (`marcarSinSaltar` en `proyectos.html`) envía EL
MISMO formulario (mismo POST, misma ruta, misma redirección del servidor), vuelve a
pedir LA MISMA página que se está viendo y cambia la columna de la izquierda y el
centro por lo que devolvió el servidor, devolviendo el desplazamiento. Si algo falla
envía el formulario de la forma de siempre.

QUÉ VIGILA ESTE ARCHIVO Y QUÉ NO. Vigila: (1) la ruta real marca y la página que se
pide después trae lo que se ve (aviso, tarea, cuenta); (2) todos los formularios que
marcan o desmarcan, sacados del HTML real, son los que el guion intercepta; (3) las
claves de aviso que el guion copia son las que la ruta usa; (4) el guion, corrido en
JavaScriptCore con un DOM de mentira, impide el envío normal, hace UN POST, restaura
el desplazamiento de las dos columnas y de la ventana y cae al envío normal si algo
falla. NO vigila el alto ni el desplazamiento reales: eso es de un navegador (se midió
a mano; los números van en el reporte del trabajo)."""
from __future__ import annotations

import ast
import re
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_escrituras_proyecto import _correr_en_jxa, _script_de_la_pagina, hay_osascript, la_funcion_de_marcar
from test_escrituras_tarea import mt, post, tarea  # noqa: F401
from test_grupo_ia import _ROOT
from test_pagina_proyectos import _cliente, gente, mundo  # noqa: F401
import config

CLAVES_DEL_AVISO = ("hecho", "error", "derivadas", "derivar")
MARCAR = re.compile(r'<form class="marcar" method="post" action="(/proyectos/tarea/(\d+)/(hecha|reabrir))"')


def _pagina(**consulta) -> str:
    r = _cliente(config.CHAT_ID_DUENO).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:200]
    return r.text


def _lo_que_pide_el_guion(donde: dict, location: str) -> dict:
    """La misma cuenta que hace `marcarSinSaltar`: la página que se está viendo,
    sin su aviso viejo y con el que trajo la redirección del servidor."""
    nuevo = dict(parse_qsl(urlsplit(location).query))
    return {**{k: v for k, v in donde.items() if k not in CLAVES_DEL_AVISO},
            **{k: v for k, v in nuevo.items() if k in CLAVES_DEL_AVISO}}


def _clase_de(html: str, tid: int) -> list[str]:
    return re.search(r'<div class="(tarea[^"]*)" id="tarea-%d"' % tid, html).group(1).split()


# (consulta que la persona está viendo, la tarea que toca): la lista de un
# proyecto, el mismo con un detalle abierto, las sueltas de un grupo y las de
# «sin grupo».
SITIOS = [({"p": 2}, 10), ({"p": 2, "t": 12}, 10), ({"g": "CDS"}, 30), ({"sin_grupo": 1}, 31)]


@pytest.mark.parametrize("donde,tid", SITIOS, ids=lambda x: str(x))
def test_la_ruta_marca_y_desmarca_y_la_pagina_que_pide_el_guion_lo_muestra(mt, donde, tid):
    """Camino de producción de punta a punta: el POST que envía el formulario, la
    redirección del servidor, y la MISMA página que se estaba viendo pedida con el
    aviso. Lo que cambia en pantalla al marcar (la tarea hecha, el aviso y la
    cuenta del avance) tiene que salir de ahí."""
    antes = _pagina(**donde)
    assert "hecha" not in _clase_de(antes, tid)
    r = post(f"/proyectos/tarea/{tid}/hecha")
    assert r.status_code == 303 and "hecho=tarea_hecha" in r.headers["location"]
    pedida = _lo_que_pide_el_guion(donde, r.headers["location"])
    assert pedida == {**donde, "hecho": "tarea_hecha"}
    despues = _pagina(**pedida)
    assert "hecha" in _clase_de(despues, tid)
    assert "Tarea marcada como hecha." in despues and "Tarea marcada como hecha." not in antes
    if "p" in donde:     # la cuenta del avance sube en uno, como con la recarga
        def n(h):
            return int(re.search(r"(\d+) de \d+ tareas hechas", h).group(1))
        assert n(despues) == n(antes) + 1
    # Y desmarcar: vuelve a pendiente, con SU aviso (el viejo ya no está).
    r = post(f"/proyectos/tarea/{tid}/reabrir")
    assert "hecho=tarea_reabierta" in r.headers["location"]
    otra = _pagina(**_lo_que_pide_el_guion(pedida, r.headers["location"]))
    assert "hecha" not in _clase_de(otra, tid)
    assert "Tarea desmarcada" in otra and "Tarea marcada como hecha." not in otra
    assert tarea(mt, tid)["estado"] == "pendiente"


def test_todos_los_formularios_que_marcan_son_los_que_intercepta_el_guion(mt):
    """Hermanos: cada formulario de la página que marca o desmarca una tarea (de
    TODAS las vistas, sacados del HTML real) es un `form.marcar`, que es lo que
    engancha el guion; y la plantilla no los escribe de otra forma en otro sitio."""
    vistas = [{"p": 1}, {"p": 2}, {"p": 2, "t": 12}, {"p": 3}, {"p": 4}, {"g": "CDS"}, {"g": "ACD"},
              {"sin_grupo": 1}, {}]
    vistos = set()
    for v in vistas:
        html = _pagina(**v)
        marcan = re.findall(r'<form\b[^>]*action="(/proyectos/tarea/\d+/(?:hecha|reabrir))"[^>]*>', html)
        enganchables = [m.group(1) for m in MARCAR.finditer(html)]
        assert sorted(marcan) == sorted(enganchables), (v, marcan, enganchables)
        vistos |= set(marcan)
    assert any(x.endswith("/hecha") for x in vistos) and any(x.endswith("/reabrir") for x in vistos), vistos
    fuente = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    assert len(re.findall(r'action="/proyectos/tarea/\{\{ t\.id \}\}/(?:hecha|reabrir)"', fuente)) == 2
    assert len(re.findall(r'<form class="marcar" method="post" action="/proyectos/tarea/\{\{ t\.id \}\}/(?:hecha|reabrir)"', fuente)) == 2


def test_las_claves_de_aviso_que_copia_el_guion_son_las_que_usa_la_ruta():
    """El guion copia de la redirección solo estas claves a la página que vuelve a
    pedir. Se sacan de las dos rutas (los nombres que le pasan a
    `_volver_a_la_tarea`), no de memoria: una clave nueva en la ruta que el guion no
    copie perdería su aviso."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    usadas = set()
    for f in ast.walk(arbol):
        if isinstance(f, ast.AsyncFunctionDef) and f.name in {
                "marcar_tarea_hecha_desde_proyectos", "reabrir_tarea_desde_proyectos"}:
            for n in ast.walk(f):
                if isinstance(n, ast.keyword) and n.arg:
                    usadas.add(n.arg)
                if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "vuelta"
                        and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
                    usadas.add(n.slice.value)        # `vuelta["derivadas"] = ...`
                if isinstance(n, ast.Dict):
                    usadas |= {k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    usadas -= {"status_code"}
    guion = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    copiadas = set(re.search(r"var clavesDelAviso = \[(.*?)\];", guion).group(1).replace('"', "").replace(" ", "").split(","))
    assert {"hecho", "error", "derivadas", "derivar"} <= usadas, usadas
    assert usadas <= copiadas, (usadas, copiadas)
    assert copiadas == set(CLAVES_DEL_AVISO)


def test_la_funcion_de_marcar_hace_dos_pedidos_y_nada_mas(mundo):
    """Frontera de lo que el guion puede hacer en la red con esta función: un POST
    al `action` del formulario y un GET de la página que se ve; ni XHR, ni
    almacenamiento, ni `innerHTML`; y si algo falla, el envío normal."""
    f, _ = la_funcion_de_marcar(_script_de_la_pagina(mundo))
    sin_comentarios = re.sub(r"/\*.*?\*/", "", f, flags=re.S)
    assert sin_comentarios.count("fetch(") == 2
    assert re.search(r'fetch\(form\.action, \{method: "POST"', sin_comentarios)
    assert "fetch(aqui.href," in sin_comentarios
    assert re.search(r"XMLHttpRequest|sendBeacon|localStorage|sessionStorage|innerHTML|eval\(", sin_comentarios) is None
    assert ".catch(normal)" in sin_comentarios and "form.submit()" in sin_comentarios


# ── El guion corrido de verdad (JavaScriptCore) con un DOM de mentira ──────────
# FRONTERA: no es un navegador; no mide pixeles ni alto. Comprueba lo que el guion
# HACE con lo que ve: impedir el envío normal, un POST, devolver el desplazamiento
# y caer al envío normal si algo falla. Las «promesas» son sincrónicas (`ok`/`bad`).

_MENTIRAS = r"""
function ok(v) { return {then: function (f) { if (!f) return this;
  try { var r = f(v); return (r && r.then) ? r : ok(r); } catch (e) { return bad(e); } },
  catch: function () { return this; }}; }
function bad(e) { return {then: function () { return this; },
  catch: function (g) { try { var r = g(e); return (r && r.then) ? r : ok(r); } catch (x) { return bad(x); } }}; }
function URL(u) {
  var a = u.split("#")[0].split("?"), q = {}, orden = [];
  this.pathname = a[0].replace(/^https?:\/\/[^\/]+/, "");
  (a[1] || "").split("&").forEach(function (p) { if (p) { var kv = p.split("="); q[kv[0]] = kv[1]; orden.push(kv[0]); } });
  var self = this;
  this.searchParams = {has: function (k) { return k in q; }, get: function (k) { return q[k]; },
    set: function (k, v) { if (orden.indexOf(k) < 0) orden.push(k); q[k] = v; },
    delete: function (k) { delete q[k]; orden = orden.filter(function (x) { return x !== k; }); }};
  Object.defineProperty(this, "search", {get: function () {
    return "?" + orden.filter(function (k) { return k in q; }).map(function (k) { return k + "=" + q[k]; }).join("&"); }});
  Object.defineProperty(this, "href", {get: function () { return "http://x.test" + self.pathname + self.search + self.hash; }});
  this.hash = u.indexOf("#") >= 0 ? u.slice(u.indexOf("#")) : "";
}
function FormData(f) {}
function URLSearchParams(x) {}
var location = {href: "http://x.test/proyectos?p=2&hecho=tarea_creada#tarea-10"};   // con ancla, como tras una recarga normal
var llamadas = {posts: [], gets: [], submit: 0, replace: [], scrollTo: [], evitado: 0};
var modo = "ok";
var vivos = {};
function nodo(rol, y) {
  return {rol: rol, scrollTop: y, scrollHeight: 3000, clientHeight: 700,
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    getBoundingClientRect: function () { return {top: 0, bottom: 0, height: 0}; },
    replaceWith: function (n) { vivos[rol] = n; }};
}
vivos.aside = nodo("aside", 300); vivos.main = nodo("main", 1200);
document.querySelector = function (s) { return s.indexOf("aside") >= 0 ? vivos.aside : (s.indexOf("main") >= 0 ? vivos.main : null); };
document.querySelectorAll = function () { return []; };
document.importNode = function (n) { return n; };
function DOMParser() {}
DOMParser.prototype.parseFromString = function () {
  return {querySelector: function (s) {
    if (modo === "no-es-proyectos") return null;
    return s.indexOf("aside") >= 0 ? nodo("aside", 0) : nodo("main", 0); }}; };
var history = {replaceState: function (a, b, u) { llamadas.replace.push(u); }};
var fetch = function (url, op) {
  if (op && op.method === "POST") {
    llamadas.posts.push(url);
    if (modo === "red") return bad(new Error("sin red"));
    return ok({ok: modo !== "500", status: modo === "500" ? 500 : 200,
      url: "http://x.test/proyectos?p=2&hecho=tarea_hecha"});
  }
  llamadas.gets.push(url);
  return ok({ok: true, text: function () { return ok("<html>"); }});
};
var window = {fetch: fetch, URLSearchParams: URLSearchParams, DOMParser: DOMParser, pageYOffset: 50, pageXOffset: 0,
  scrollTo: function (x, y) { llamadas.scrollTo.push([x, y]); }};
function formularioDeMarcar() {
  var boton = {disabled: false};
  return {classList: {contains: function (c) { return c === "marcar"; }}, dataset: {},
    action: "/proyectos/tarea/10/hecha", boton: boton,
    querySelector: function (s) { return s === "button" ? boton : null; },
    closest: function () { return null; },
    submit: function () { llamadas.submit++; }};
}
function enviar(form) {
  var e = {target: form, preventDefault: function () { llamadas.evitado++; }};
  oyentes.submit(e);
}
"""


def _jxa(mundo, escenario: str) -> dict:
    return _correr_en_jxa(_script_de_la_pagina(mundo), _MENTIRAS + escenario)


@hay_osascript
def test_js_marcar_envia_un_post_y_restaura_el_desplazamiento_y_no_recarga(mundo):
    r = _jxa(mundo, 'var f = formularioDeMarcar(); enviar(f);'
             'JSON.stringify({posts: llamadas.posts, gets: llamadas.gets, evitado: llamadas.evitado,'
             ' recarga: llamadas.submit, aside: vivos.aside.scrollTop, main: vivos.main.scrollTop,'
             ' ventana: llamadas.scrollTo, url: llamadas.replace, boton: f.boton.disabled})')
    assert r == {"posts": ["/proyectos/tarea/10/hecha"],
                 "gets": ["http://x.test/proyectos?p=2&hecho=tarea_hecha"],
                 "evitado": 1, "recarga": 0, "aside": 300, "main": 1200,
                 "ventana": [[0, 50]], "url": ["/proyectos?p=2&hecho=tarea_hecha"], "boton": True}


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "500", "no-es-proyectos"])
def test_js_si_algo_falla_se_envia_el_formulario_de_siempre(mundo, modo):
    r = _jxa(mundo, 'modo = %r; var f = formularioDeMarcar(); enviar(f);'
             'JSON.stringify({posts: llamadas.posts.length, recarga: llamadas.submit,'
             ' evitado: llamadas.evitado, escrito: llamadas.replace.length, aside: vivos.aside.scrollTop,'
             ' boton: f.boton.disabled})' % modo)
    assert r["posts"] == 1 and r["recarga"] == 1 and r["evitado"] == 1 and r["escrito"] == 0
    assert r["aside"] == 300 and r["boton"] is False      # nada se cambió y el botón queda usable


@hay_osascript
def test_js_un_segundo_envio_del_mismo_formulario_no_manda_otro_post(mundo):
    r = _jxa(mundo, 'var f = formularioDeMarcar(); modo = "red"; enviar(f); enviar(f);'
             'JSON.stringify({posts: llamadas.posts.length, evitado: llamadas.evitado})')
    assert r == {"posts": 1, "evitado": 2}
