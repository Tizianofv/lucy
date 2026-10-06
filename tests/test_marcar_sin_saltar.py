"""Marcar o desmarcar una tarea en Proyectos no mueve lo que la persona está viendo
(Tiziano, 6-oct-2026: «cuando seleccione una tarea como realizada la pagina se me
mueve»).

LA CAUSA, medida en el navegador: el formulario del círculo hacía POST, el servidor
contestaba 303 a la página entera con `#tarea-N`, y el navegador la cargaba de cero:
el centro (`.envoltura > main`, con su propio desplazamiento) y la ventana quedaban
donde los dejara el ancla, no donde estaba la persona.

EL ARREGLO: el guion de la página (`marcarSinSaltar` en `proyectos.html`) envía EL
MISMO formulario (mismo POST, misma ruta). El navegador sigue la redirección del
servidor y baja la página que el servidor decidió mostrar (la de antes, con sus
avisos y sin los estados viejos de la URL); con ESA página, sin pedir nada más, se
cambian la columna de la izquierda y el centro y se devuelve el desplazamiento. Si el
POST no llega o el servidor contesta un error, envía el formulario de la forma de
siempre; si el POST contestó bien pero la página no se puede poner (ya se guardó),
va a esa página con un enlace, sin reenviar el POST.

QUÉ VIGILA ESTE ARCHIVO Y QUÉ NO. Vigila: (1) la ruta real marca y la página que se
redirige a una página con SOLO el aviso nuevo y lo que se ve (tarea, cuenta); (2) todos los formularios que
marcan o desmarcan, sacados del HTML real, son los que el guion intercepta; (3) la
función del guion tiene las MISMAS prohibiciones que el resto del guion, salvo un
POST declarado; (4) el guion, corrido en JavaScriptCore con un DOM de mentira,
impide el envío normal, hace UN POST y ningún otro pedido, pinta lo que trajo el
servidor, restaura el desplazamiento de las dos columnas y de la ventana, cae al
envío normal si el POST falla y va a la página (sin reenviar) si guardó y no pudo
pintarla. NO vigila el alto ni el desplazamiento reales: eso es de un navegador (se midió
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


def _clase_de(html: str, tid: int) -> list[str]:
    return re.search(r'<div class="(tarea[^"]*)" id="tarea-%d"' % tid, html).group(1).split()


# (consulta que la persona está viendo, la tarea que toca): la lista de un
# proyecto, el mismo con un detalle abierto, las sueltas de un grupo y las de
# «sin grupo».
SITIOS = [({"p": 2}, 10), ({"p": 2, "t": 12}, 10), ({"g": "CDS"}, 30), ({"sin_grupo": 1}, 31)]


ESTADOS_VIEJOS = ("tarea_creada", "sala_no", "confirmar_borrar", "editar_tarea", "editar_comentario", "t",
                  "creado", "area_guardada", "nombre_guardado", "confirmar")


def _avisos(html: str) -> list[str]:
    return re.findall(r'<p class="aviso[^"]*">(.*?)</p>', html)


@pytest.mark.parametrize("donde,tid", SITIOS, ids=lambda x: str(x))
def test_la_ruta_marca_y_desmarca_y_la_pagina_a_la_que_redirige_es_la_que_se_pinta(mt, donde, tid):
    """Camino de producción de punta a punta: el POST que envía el formulario y la
    página a la que redirige el servidor (la que el guion pinta tal cual, sin pedir
    otra). Lo que cambia en pantalla al marcar (la tarea hecha, UN aviso y la cuenta
    del avance) sale de ahí, y la dirección no trae ningún estado viejo."""
    antes = _pagina(**donde)
    assert "hecha" not in _clase_de(antes, tid)
    r = post(f"/proyectos/tarea/{tid}/hecha")
    assert r.status_code == 303
    destino = urlsplit(r.headers["location"])
    claves = dict(parse_qsl(destino.query))
    assert claves.pop("hecho") == "tarea_hecha" and not (set(claves) & set(ESTADOS_VIEJOS)), claves
    despues = _pagina(**dict(parse_qsl(destino.query)))
    assert "hecha" in _clase_de(despues, tid)
    assert _avisos(despues) == ["Tarea marcada como hecha."]
    if "p" in donde:     # la cuenta del avance sube en uno, como con la recarga
        def n(h):
            return int(re.search(r"(\d+) de \d+ tareas hechas", h).group(1))
        assert n(despues) == n(antes) + 1
    # Y desmarcar: vuelve a pendiente, con SU aviso (y solo ese).
    r = post(f"/proyectos/tarea/{tid}/reabrir")
    otra = _pagina(**dict(parse_qsl(urlsplit(r.headers["location"]).query)))
    assert "hecha" not in _clase_de(otra, tid)
    assert len(_avisos(otra)) == 1 and "Tarea desmarcada" in _avisos(otra)[0]
    assert tarea(mt, tid)["estado"] == "pendiente"


def test_el_servidor_descarta_los_estados_viejos_al_marcar(mt):
    """El caso del testigo: la persona está en `?p=2&tarea_creada=..&confirmar_borrar=..`
    y marca otra tarea. Lo que el guion pinta es la página de la redirección: no
    hereda ese aviso ni el «¿Borrar?»; la dirección vieja SÍ los pintaría (por eso
    el guion no la vuelve a pedir)."""
    vieja = _pagina(p=2, tarea_creada=12, confirmar_borrar=10)
    assert any("Tarea creada" in a for a in _avisos(vieja))
    r = post("/proyectos/tarea/10/hecha")
    nueva = _pagina(**dict(parse_qsl(urlsplit(r.headers["location"]).query)))
    assert _avisos(nueva) == ["Tarea marcada como hecha."]


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


# LAS MISMAS PROHIBICIONES que el resto del guion (la unión de las listas de
# `test_pagina_proyectos._JS_QUE_DECIDIRIA`, `test_escrituras_proyecto`,
# `test_pagina_proyectos_maqueta` y `test_personas_y_cliente`) más las formas de
# escribir lo mismo de otro modo. La función tiene PERMITIDO solo esto, uno por uno:
PERMITIDO_EN_LA_FUNCION = (
    # el único pedido de red: POST al `action` del propio formulario, con SUS campos.
    ('fetch(form.action, {method: "POST", body: new URLSearchParams(new FormData(form)), credentials: "same-origin"})', 1),
    # la caída al envío de siempre.
    ("form.submit()", 1),
)
PROHIBIDO = re.compile(
    r"fetch|XMLHttpRequest|sendBeacon|WebSocket|EventSource|localStorage|sessionStorage|\.submit\(|"
    r"\blocation\b|\.action|FormData|innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval|Function\b|"
    r"import\(|(?<![.\w])(self|globalThis|top|parent|frames)\b|\bwindow\s*\[|\bthis\b|Reflect|Object\.|"
    r"\[\s*[\"'`]|setTimeout|setInterval|\.open\(|postMessage|navigator|\.cookie")


def lo_prohibido_en(funcion: str) -> list[str]:
    """Quita los comentarios y los pedidos PERMITIDOS (comprobando cuántas veces
    están) y devuelve lo prohibido que quede en el texto."""
    codigo = re.sub(r"\s+", " ", re.sub(r"/\*.*?\*/", "", funcion, flags=re.S))
    for permitido, veces in PERMITIDO_EN_LA_FUNCION:
        assert codigo.count(permitido) == veces, (permitido, codigo.count(permitido))
        codigo = codigo.replace(permitido, "")
    return [m.group(0) for m in PROHIBIDO.finditer(codigo)]


def test_la_funcion_de_marcar_tiene_las_mismas_prohibiciones_que_el_resto_del_guion(mundo):
    """FRONTERA, dicha: es un análisis de TEXTO. Ve el nombre `fetch` (o `location`,
    `.action`, `XMLHttpRequest`...) escrito de cualquier forma con ese nombre, con
    espacios, en una rama muerta o como `window.fetch`, y el acceso por corchetes con
    texto. NO ve un nombre armado en tiempo de ejecución sin ninguna de esas
    palabras (p. ej. una cadena que se junta y se evalúa: `eval`, `Function`,
    `import(`, `this`, `Reflect` y `Object.` también están prohibidos para cerrar
    esa puerta). Que el guion haga en un navegador solo lo que dice se comprueba
    con el guion corrido (abajo), no con este texto."""
    f, _ = la_funcion_de_marcar(_script_de_la_pagina(mundo))
    assert lo_prohibido_en(f) == []


@pytest.mark.parametrize("meter", [
    'location.href = "/otra";', 'window["fetch"]("/borrar", {method: "POST"});', 'new WebSocket("ws://x");',
    'fetch ("/otra");', 'var u = window.fetch;', 'form.action = "/otra";', 'new XMLHttpRequest();',
    'document.cookie = "x";', 'if (false) { navigator.sendBeacon("/x"); }', "var g = self['fe' + 'tch'];",
    'new FormData(form);', 'form.submit();'])
def test_lo_prohibido_no_se_cuela_en_la_funcion_ni_en_una_rama_muerta(mundo, meter):
    f, _ = la_funcion_de_marcar(_script_de_la_pagina(mundo))
    roto = f.replace("var boton =", "if (false) { " + meter + " }\n  var boton =", 1)
    assert roto != f
    try:
        quedan = lo_prohibido_en(roto)
    except AssertionError:        # un permitido repetido también es una puerta cerrada
        return
    assert quedan, meter


# ── El guion corrido de verdad (JavaScriptCore) con un DOM de mentira ──────────
# FRONTERA: no es un navegador; no mide pixeles ni alto. Comprueba lo que el guion
# HACE con lo que ve: impedir el envío normal, UN POST y ningún otro pedido, pintar
# lo que trajo el servidor (cada nodo nuevo nace de LA página que contestó),
# devolver el desplazamiento y qué hace si algo falla. Las «promesas» son
# sincrónicas (`ok`/`bad`).

_MENTIRAS = r"""
function ok(v) { return {then: function (f) { if (!f) return this;
  try { var r = f(v); return (r && r.then) ? r : ok(r); } catch (e) { return bad(e); } },
  catch: function () { return this; }}; }
function bad(e) { return {then: function () { return this; },
  catch: function (g) { try { var r = g(e); return (r && r.then) ? r : ok(r); } catch (x) { return bad(x); } }}; }
function URL(u) {
  var a = u.split("#")[0].split("?"), q = {}, orden = [];
  this.pathname = a[0].replace(/^https?:\/\/[^\/]+/, "");
  this.search = a[1] ? "?" + a[1] : "";
}
function FormData(f) {}
function URLSearchParams(x) {}
var llamadas = {posts: [], otros: [], submit: 0, replace: [], scrollTo: [], evitado: 0, ir: []};
var modo = "ok";
var vivos = {};
function nodo(rol, y, desde) {
  return {rol: rol, desde: desde, scrollTop: y, scrollHeight: 3000, clientHeight: 700,
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    getBoundingClientRect: function () { return {top: 0, bottom: 0, height: 0}; },
    replaceWith: function (n) { vivos[rol] = n; }};
}
vivos.aside = nodo("aside", 300, "VIEJA"); vivos.main = nodo("main", 1200, "VIEJA");
document.querySelector = function (s) { return s.indexOf("aside") >= 0 ? vivos.aside : (s.indexOf("main") >= 0 ? vivos.main : null); };
document.querySelectorAll = function () { return []; };
document.importNode = function (n) { if (modo === "importar-falla") throw new Error("x"); return n; };
document.createElement = function (t) { return {tag: t, click: function () { llamadas.ir.push(this.href); }}; };
function DOMParser() {}
DOMParser.prototype.parseFromString = function (texto) {
  return {querySelector: function (s) {
    if (modo === "no-es-proyectos") return null;
    return s.indexOf("aside") >= 0 ? nodo("aside", 0, texto) : nodo("main", 0, texto); }}; };
var history = {replaceState: function (a, b, u) { llamadas.replace.push(u); }};
var fetch = function (url, op) {
  if (op && op.method === "POST") {
    llamadas.posts.push(url);
    if (modo === "red") return bad(new Error("sin red"));
    return ok({ok: modo !== "500", status: modo === "500" ? 500 : 200,
      url: "http://x.test/proyectos?p=2&hecho=tarea_hecha",
      text: function () { return ok("PAGINA-DEL-SERVIDOR"); }});
  }
  llamadas.otros.push(url);
  return bad(new Error("un segundo pedido"));
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

_FOTO = ('JSON.stringify({posts: llamadas.posts, otros: llamadas.otros, evitado: llamadas.evitado,'
         ' recarga: llamadas.submit, ir: llamadas.ir, aside: vivos.aside.scrollTop, main: vivos.main.scrollTop,'
         ' asideDe: vivos.aside.desde, mainDe: vivos.main.desde, ventana: llamadas.scrollTo,'
         ' url: llamadas.replace, boton: f.boton.disabled})')


def _jxa(mundo, escenario: str) -> dict:
    return _correr_en_jxa(_script_de_la_pagina(mundo), _MENTIRAS + escenario)


@hay_osascript
def test_js_marcar_pinta_lo_que_trajo_el_servidor_sin_un_segundo_pedido(mundo):
    r = _jxa(mundo, 'var f = formularioDeMarcar(); enviar(f);' + _FOTO)
    assert r == {"posts": ["/proyectos/tarea/10/hecha"], "otros": [], "evitado": 1, "recarga": 0, "ir": [],
                 # lo pintado nació de LA página que contestó el servidor (no de la vieja)…
                 "asideDe": "PAGINA-DEL-SERVIDOR", "mainDe": "PAGINA-DEL-SERVIDOR",
                 # …y el desplazamiento (de las dos columnas y de la ventana) es el de antes.
                 "aside": 300, "main": 1200, "ventana": [[0, 50]],
                 "url": ["/proyectos?p=2&hecho=tarea_hecha"], "boton": True}


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "500"])
def test_js_si_el_post_no_llega_o_falla_se_envia_el_formulario_de_siempre(mundo, modo):
    r = _jxa(mundo, 'modo = %r; var f = formularioDeMarcar(); enviar(f);' % modo + _FOTO)
    assert r["posts"] == ["/proyectos/tarea/10/hecha"] and r["otros"] == []
    assert r["recarga"] == 1 and r["evitado"] == 1 and r["ir"] == []
    assert r["asideDe"] == "VIEJA" and r["mainDe"] == "VIEJA" and r["url"] == []    # nada se pintó
    assert r["boton"] is False                                                      # y el botón queda usable


@hay_osascript
@pytest.mark.parametrize("modo", ["no-es-proyectos", "importar-falla"])
def test_js_si_guardo_y_no_se_puede_pintar_va_a_la_pagina_sin_reenviar_el_post(mundo, modo):
    """El POST contestó bien (ya se guardó): no se reenvía (daría «ya estaba»), se
    va a la MISMA página que contestó el servidor, con un enlace."""
    r = _jxa(mundo, 'modo = %r; var f = formularioDeMarcar(); enviar(f);' % modo + _FOTO)
    assert r["posts"] == ["/proyectos/tarea/10/hecha"] and r["otros"] == []
    assert r["recarga"] == 0 and r["ir"] == ["http://x.test/proyectos?p=2&hecho=tarea_hecha"]


@hay_osascript
def test_js_un_segundo_envio_del_mismo_formulario_no_manda_otro_post(mundo):
    r = _jxa(mundo, 'var f = formularioDeMarcar(); modo = "red"; enviar(f); enviar(f);' + _FOTO)
    assert len(r["posts"]) == 1 and r["evitado"] == 2
