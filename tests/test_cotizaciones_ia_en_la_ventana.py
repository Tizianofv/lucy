"""Pieza 3 de «IA para los textos de las cotizaciones»: los botones de la ventana y el guion `guion-cotizaciones-ia`
(`web/plantillas/proyectos.html`, `web/plantillas/_cotizaciones_del_proyecto.html`).

Qué se vigila (diseño §4, pieza 3):
  1. el guion solo pide a las dos rutas de la IA, por POST, con UNA sola función (censo de nombres con intrusos
     inventados + las direcciones que de verdad pide el guion corrido, comparadas con las rutas registradas);
  2. nunca escribe en una casilla de precio (aunque la respuesta traiga `total`): quedan vacías y marcadas;
  3. no cambia nada sin el clic: el detalle sigue igual hasta «Usar este texto»; «Dejar el mío» no lo cambia;
  4. «Armar borrador» solo con las opciones vacías (con una casilla escrita no pide nada, ni con un clic directo);
  5. la vista previa se rehace (se dispara `input`, que es lo que escucha el guion de la ventana);
  6. solo la casa los ve; el guion de la ventana conserva su garantía de «no pide nada».

El guion se corre en JavaScriptCore (`osascript -l JavaScript`) sobre el HTML que la página PINTA DE VERDAD (se parsea la
ventana pintada y se arma un documento de mentira con ese árbol). FRONTERA, dicha: el documento de mentira NO es un
navegador. Entiende el subconjunto de selectores que usa el guion (si el guion usa otro, revienta y la prueba se pone
roja), no hace diseño ni mide nada, y sus tres dobles son: (a) `fetch` y su promesa, que se resuelven a mano (las promesas de
JavaScriptCore no avanzan dentro de `osascript`); (b) los «+ Agregar» y «✕» del guion de la ventana, que aquí clonan la
`<template>` pintada por el servidor y quitan el bloque (el real se midió en un navegador, no aquí); (c) el guion de la
ventana en sí (la vista previa): aquí solo se cuenta que le llegue el evento `input` que él escucha. La medida de pantalla
(1300 y 412 px) y la combinación real de los dos guiones se hicieron en Chrome sobre la página pintada (ver el reporte del
trabajo), no en esta suite.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from test_cotizaciones_en_la_pagina import (_casa, _guardar, _pagina, _ver, lo_que_el_guion_toca, m,  # noqa: F401
                                            tabla_ausente_como_postgres)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)
import cotizaciones_ia as ia
import web.app as panel

hay_osascript = pytest.mark.skipif(shutil.which("osascript") is None, reason="no hay osascript (JavaScriptCore de macOS)")

PROYECTOS = "web/plantillas/proyectos.html"
PLANTILLA = "web/plantillas/_cotizaciones_del_proyecto.html"
RUTA_DE_LA_IA = "/proyectos/1/cotizaciones/ia"


def _guion_ia() -> str:
    fuente = open(PROYECTOS, encoding="utf-8").read()
    assert fuente.count('<script id="guion-cotizaciones-ia">') == 1
    return fuente.split('<script id="guion-cotizaciones-ia">', 1)[1].split("</script>", 1)[0]


def _guion_de_la_ventana() -> str:
    fuente = open(PROYECTOS, encoding="utf-8").read()
    return fuente.split('<script id="guion-cotizaciones">', 1)[1].split("</script>", 1)[0]


def _ventana(html: str) -> str:
    desde = html.index('<div class="velo"')
    return html[desde:html.index("</section>", desde)]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Lo que la página pinta (HTML de verdad)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class _Elementos(HTMLParser):
    """Todos los elementos del HTML pintado, con su atributo y si están dentro de un `<form class="cz-form">`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elementos: list[dict] = []
        self.en_plantilla = 0
        self.form = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "template":
            self.en_plantilla += 1
        if tag == "form" and "cz-form" in a.get("class", "").split():
            self.form += 1
        self.elementos.append({"tag": tag, "a": a, "plantilla": self.en_plantilla > 0, "form": self.form > 0})

    def handle_endtag(self, tag):
        if tag == "template":
            self.en_plantilla -= 1
        if tag == "form":
            self.form = 0


def _elementos(html: str) -> list[dict]:
    p = _Elementos()
    p.feed(html)
    return p.elementos


def _de_la_ia(e: dict) -> bool:
    return any(k.startswith("data-cz-ia") for k in e["a"])


def test_la_casa_ve_la_caja_de_armar_el_boton_de_cada_detalle_y_el_guion(m):
    html = _pagina(_casa(), p=1, cotizacion="nueva")
    els = _elementos(html)
    assert sum(1 for e in els if "data-cz-ia-caja" in e["a"]) == 1
    assert sum(1 for e in els if e["a"].get("data-cz-ia") == "borrador") == 1
    assert '<script id="guion-cotizaciones-ia">' in html
    # un botón «Mejorar redacción» por cada «Detalle»: los pintados Y los de las dos plantillas de «+ Agregar»
    detalles = [e for e in els if e["tag"] == "textarea" and e["a"].get("name", "").endswith("-detalle")]
    botones = [e for e in els if e["a"].get("data-cz-ia") == "redaccion"]
    assert len(detalles) >= 4 and len(botones) == len(detalles), (len(detalles), len(botones))
    assert sum(1 for e in detalles if e["plantilla"]) == 2 and sum(1 for e in botones if e["plantilla"]) == 2


def test_solo_ver_y_sin_sesion_no_tienen_ni_botones_ni_caja_ni_guion(m):
    _guardar("enviar")
    for quien in (_ver(),):
        for consulta in ({"p": 1}, {"p": 1, "cotizacion": 1}, {"p": 1, "cotizacion": "nueva"}):
            html = _pagina(quien, **consulta)
            assert not [e for e in _elementos(html) if _de_la_ia(e)], consulta
            assert 'id="guion-cotizaciones-ia"' not in html and 'class="ia-caja' not in html and "Mejorar redacción" not in html
    # la casa, con la hoja de una enviada (no es formulario), tampoco los trae: no hay nada que mejorar
    assert not [e for e in _elementos(_pagina(_casa(), p=1, cotizacion=1)) if _de_la_ia(e) and e["tag"] != "script"
                and e["a"].get("data-cz-ia") in ("redaccion", "borrador")]


def test_nada_de_la_ia_viaja_en_el_post_de_guardar(m):
    """Cada control de la IA es un `<button type="button">` o una casilla SIN `name`: el POST de «Guardar» no lo lleva.
    La lista sale del HTML pintado (cualquier elemento con un atributo `data-cz-ia…`)."""
    els = _elementos(_pagina(_casa(), p=1, cotizacion="nueva"))
    de_la_ia = [e for e in els if _de_la_ia(e) and e["tag"] in ("button", "input", "textarea", "select")]
    assert len(de_la_ia) >= 6, "la prueba no ve los controles de la IA"
    for e in de_la_ia:
        assert "name" not in e["a"] and "form" not in e["a"], e
        if e["tag"] == "button":
            assert e["a"].get("type") == "button", e


def test_sin_javascript_no_sale_nada_de_la_ia():
    """Todo lo que pinta la IA lleva `cz-ia` (o va dentro de lo que lo lleva) y la regla lo esconde; solo `.con-js` lo muestra."""
    fuente = open(PROYECTOS, encoding="utf-8").read()
    estilos = fuente.split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"(?m)^\.cz-ia\{display:none\}", estilos)
    assert re.search(r"\.con-js \.ia-caja\.cz-ia", estilos) and re.search(r"\.con-js \.ia-fila\.cz-ia", estilos)
    plantilla = open(PLANTILLA, encoding="utf-8").read()
    assert len(re.findall(r'class="ia-caja cz-ia"', plantilla)) == 1
    assert len(re.findall(r'class="ia-fila cz-ia"><button type="button" class="btn-ia chico" data-cz-ia="redaccion"', plantilla)) == 1
    # el macro del botón se llama una vez por cada «Detalle» de la plantilla (pintado + 2 plantillas)
    assert plantilla.count("{{ ia_mejorar() }}") == len(re.findall(r'<textarea name="op-[^"]*-detalle"', plantilla)) == 3


def test_las_frases_que_pinta_la_ventana_son_las_del_servidor(m):
    """`data-cz-ia-textos` es la fuente de las frases del guion. Las que el servidor también dice son LAS MISMAS
    (`CAUSAS`), y toda clave que el guion lee (`t.<clave>`) existe."""
    f = next(e for e in _elementos(_pagina(_casa(), p=1, cotizacion="nueva")) if e["tag"] == "form" and "cz-form" in e["a"].get("class", ""))
    textos = json.loads(f["a"]["data-cz-ia-textos"])
    assert textos["sin_respuesta"] == ia.CAUSAS["no_contesto"]
    assert textos["detalle_vacio"] == ia.CAUSAS["detalle_vacio"]
    assert textos["pedido_vacio"] == ia.CAUSAS["pedido_vacio"]
    usadas = set(re.findall(r"\bt\.([a-z_]+)\b", _guion_ia()))
    assert usadas and usadas <= set(textos), usadas - set(textos)
    assert "Mejorar redacción" in textos["solo_vacias"] and "Todavía no se guardó nada" in textos["aviso"]


def test_la_ruta_que_pinta_la_ventana_es_la_de_las_rutas_registradas_y_el_guion_solo_le_pega_dos_nombres(m):
    f = next(e for e in _elementos(_pagina(_casa(), p=1, cotizacion="nueva")) if e["tag"] == "form" and "cz-form" in e["a"].get("class", ""))
    base = f["a"]["data-cz-ia-ruta"]
    assert base == RUTA_DE_LA_IA
    registradas = {r.path.replace("{pid}", "1") for r in panel.app.routes if "POST" in (getattr(r, "methods", None) or ())}
    assert {base + "/redaccion", base + "/borrador"} <= registradas
    assert sorted(re.findall(r'pedir\(form, "(\w+)"', _guion_ia())) == ["borrador", "redaccion"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Lo que el guion puede tocar del navegador (decide por NOMBRES, como el de la ventana)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

# LO ÚNICO DEL NAVEGADOR QUE ESTE GUION PUEDE USAR. A diferencia del de la ventana, NO tiene `innerHTML` ni
# `insertAdjacentHTML`: el texto que pone en pantalla (la propuesta de la IA) entra solo por `textContent`.
# («instanceof» es un operador de JavaScript que el limpiador de nombres lee como un nombre libre.)
GLOBALES_DE_LA_IA = {"document", "Array", "JSON", "Event", "fetch", "instanceof"}
PROPIEDADES_DE_LA_IA = {
    "add", "addEventListener", "appendChild", "createElement", "bubbles", "call", "classList", "className", "click", "closest", "concat", "contains",
    "dispatchEvent", "every", "filter", "firstChild", "forEach", "getAttribute", "ok", "parse", "preventDefault", "prototype",
    "querySelector", "querySelectorAll", "readyState", "remove", "removeAttribute", "removeChild", "setAttribute", "slice",
    "stringify", "target", "textContent", "then", "title", "trim", "type", "value", "disabled", "json", "length", "datos",
    "renglones", "nombre", "descripcion", "detalle", "opciones", "error", "causa", "opcion", "pedido", "ok"}
# Nombres de las claves que el guion lee de SU propia información (el cuerpo de la respuesta y las frases del formulario).
PROPIEDADES_DE_DATOS = {"sin_respuesta", "detalle_vacio", "pedido_vacio", "escribiendo", "solo_vacias", "aviso", "pon_precio"}


def lo_que_toca_la_ia(codigo: str) -> tuple[set, set]:
    return lo_que_el_guion_toca(codigo)


def _veredicto_de_los_nombres(codigo: str) -> list[str]:
    """Lo que está mal en `codigo` (vacío = bien): un nombre del navegador fuera de las listas, otro `fetch(`, un
    `[\"nombre\"+\"armado\"]`, o HTML por `innerHTML`."""
    libres, propiedades = lo_que_toca_la_ia(codigo)
    mal = [f"global {n}" for n in sorted(libres - GLOBALES_DE_LA_IA)]
    mal += [f"propiedad {n}" for n in sorted(propiedades - PROPIEDADES_DE_LA_IA - PROPIEDADES_DE_DATOS)]
    if re.search(r"\[\s*[\"'][^\]]*[\"']\s*\]", codigo.replace('[name$=', "").replace("[data-", "").replace("[name", "")):
        mal.append("objeto[\"nombre\"]")
    sin_texto = re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)
    if len(re.findall(r"\bfetch\(", sin_texto)) != 1:
        mal.append("no hay exactamente UN fetch(")
    if sorted(re.findall(r'\bpedir\(form, "(\w+)"', sin_texto)) != ["borrador", "redaccion"] \
            or len(re.findall(r"\bpedir\(", sin_texto)) != 3:
        mal.append("pedir( no se llama exactamente dos veces con redaccion y borrador")
    return mal


def test_el_guion_de_la_ia_solo_usa_los_nombres_permitidos_y_un_solo_fetch():
    assert _veredicto_de_los_nombres(_guion_ia()) == []


@pytest.mark.parametrize("intruso", [
    'fetch("/x");', 'navigator.sendBeacon("/x", d);', 'new Image().src = "/x";', 'window.open("/x");', 'form.submit();',
    'var r = new XMLHttpRequest(); r.open("GET", "/x");', 'location.href = "/x";', 'document.defaultView.fetch("/x");',
    'var f = window["fe" + "tch"]; f("/x");', 'eval("fetch(1)");', 'new WebSocket("ws://x");', 'document.forms[0].submit();',
    'x.innerHTML = "<img src=/x>";', 'x.insertAdjacentHTML("beforeend", y);', 'document.write(y);',
    'pedir(form, "otra", {});', 'pedir(form, "redaccion", {});', 'document.cookie = "a=b";', 'localStorage.setItem("a", b);',
    'e.target.ownerDocument.location = "/x";'])
def test_la_prueba_de_los_nombres_ve_a_un_intruso(intruso):
    assert _veredicto_de_los_nombres(_guion_ia() + "\n" + intruso), intruso


def test_el_guion_de_la_ia_de_hoy_no_se_come_la_prueba_de_los_nombres():
    libres, propiedades = lo_que_toca_la_ia(_guion_ia())
    assert {"document", "fetch", "Event"} <= libres and {"querySelectorAll", "closest", "textContent", "setAttribute"} <= propiedades
    assert len(propiedades) > 20


def test_el_guion_de_la_ventana_sigue_sin_pedir_nada_ni_nombrar_a_la_ia():
    """Su garantía de «no pide nada a ningún servidor» no cambia: no tiene `fetch` y la IA vive en el otro guion."""
    ventana = _guion_de_la_ventana()
    assert "fetch" not in ventana and "data-cz-ia" not in ventana and "cotizaciones/ia" not in ventana


def test_los_otros_guiones_de_la_pagina_no_ven_el_de_la_ia(m):
    """El guion de la IA no cuenta como «el script de la página» para los censos de `fetch` de esa página: su id contiene
    `guion-cotizaciones` (las pruebas que cuentan scripts excluyen todo id así). Si cambia el id, esto avisa."""
    assert 'id="guion-cotizaciones-ia"' in open(PROYECTOS, encoding="utf-8").read()
    html = _pagina(_casa(), p=1, cotizacion="nueva")
    ids = re.findall(r'<script id="([^"]+)"', html)
    assert [i for i in ids if "guion-cotizaciones" in i] == ["guion-cotizaciones", "guion-cotizaciones-ia"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3. El guion corrido en JavaScriptCore sobre la ventana pintada
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

_VACIOS = {"input", "br", "hr", "img", "meta", "link", "source", "col", "area", "base", "embed", "wbr"}


class _Arbol(HTMLParser):
    """La ventana pintada como árbol `{t, a, c}` (texto como `str`), para armar el documento de mentira."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = {"t": "#raiz", "a": {}, "c": []}
        self.pila = [self.raiz]

    def handle_starttag(self, tag, attrs):
        n = {"t": tag, "a": {k: (v if v is not None else "") for k, v in attrs}, "c": []}
        self.pila[-1]["c"].append(n)
        if tag not in _VACIOS:
            self.pila.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VACIOS:
            self.pila.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.pila) - 1, 0, -1):
            if self.pila[i]["t"] == tag:
                del self.pila[i:]
                return

    def handle_data(self, data):
        self.pila[-1]["c"].append(data)


def _arbol_de(html: str) -> dict:
    p = _Arbol()
    p.feed(html)
    return p.raiz


_DOCUMENTO_DE_MENTIRA = r"""
// ── Promesas que se resuelven a mano (las de JavaScriptCore no avanzan dentro de osascript) ──
function SP() { this.estado = "pendiente"; this.valor = undefined; this.cola = []; }
SP.prototype.resolver = function (v) {
  if (this.estado !== "pendiente") return;
  if (v && typeof v.then === "function") { var yo = this; v.then(function (x) { yo.resolver(x); }, function (e) { yo.rechazar(e); }); return; }
  this.estado = "cumplida"; this.valor = v; this.vaciar();
};
SP.prototype.rechazar = function (e) { if (this.estado !== "pendiente") return; this.estado = "rechazada"; this.valor = e; this.vaciar(); };
SP.prototype.vaciar = function () { var c = this.cola; this.cola = []; c.forEach(function (f) { f(); }); };
SP.prototype.then = function (ok, no) {
  var hija = new SP(), yo = this;
  function correr() {
    var f = yo.estado === "cumplida" ? ok : no;
    if (typeof f !== "function") { if (yo.estado === "cumplida") hija.resolver(yo.valor); else hija.rechazar(yo.valor); return; }
    try { hija.resolver(f(yo.valor)); } catch (e) { hija.rechazar(e); }
  }
  if (this.estado === "pendiente") this.cola.push(correr); else correr();
  return hija;
};

// ── fetch de mentira: anota todo pedido y espera a que la prueba lo conteste ──
var pedidos = [];
function fetch(url, init) {
  var p = new SP();
  pedidos.push({url: url, init: init, p: p, metodo: init && init.method, cuerpo: init && init.body ? JSON.parse(init.body) : null,
                cabeceras: init && init.headers, credenciales: init && init.credentials});
  return p;
}
function responder(i, estado, cuerpo) {
  pedidos[i].p.resolver({ok: estado >= 200 && estado < 300, status: estado, json: function () { var q = new SP(); q.resolver(cuerpo); return q; }});
}
function noEsJson(i, estado) {
  pedidos[i].p.resolver({ok: estado >= 200 && estado < 300, status: estado, json: function () { var q = new SP(); q.rechazar(new Error("no es json")); return q; }});
}
function sinRed(i) { pedidos[i].p.rechazar(new Error("sin red")); }

// ── el documento de mentira: el árbol de la ventana pintada, con el subconjunto de selectores que usa el guion ──
var escuchas = {};
function Event(tipo, init) { this.type = tipo; this.bubbles = !!(init && init.bubbles); this.target = null; this.prevenido = false; }
Event.prototype.preventDefault = function () { this.prevenido = true; };
function Nodo(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.parent = null; this._valor = ""; this._sucio = false; this.contenido = null; }
Nodo.prototype.appendChild = function (n) { if (n.parent) n.parent.removeChild(n); n.parent = this; this.children.push(n); return n; };
Nodo.prototype.removeChild = function (n) { var i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); n.parent = null; return n; };
Nodo.prototype.remove = function () { if (this.parent) this.parent.removeChild(this); };
Nodo.prototype.insertarAntes = function (n, ref) { n.parent = this; this.children.splice(this.children.indexOf(ref), 0, n); };
Nodo.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
Nodo.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Nodo.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Nodo.prototype.hasAttribute = function (k) { return k in this.attrs; };
Nodo.prototype.addEventListener = function () { throw new Error("el guion escucha en el documento, no en un elemento"); };
Nodo.prototype.click = function () { if (this.disabled) return; this.dispatchEvent(new Event("click", {bubbles: true})); };
Nodo.prototype.dispatchEvent = function (ev) {
  ev.target = this;
  (escuchas[ev.type] || []).slice().forEach(function (f) { f(ev); });
  return true;
};
Object.defineProperty(Nodo.prototype, "firstChild", {get: function () { return this.children.length ? this.children[0] : null; }});
Object.defineProperty(Nodo.prototype, "value", {
  // Como un navegador: un <input> que nadie ha tocado (ni por `.value =` ni escribiendo) muestra su atributo `value`
  // (`setAttribute("value", x)` SÍ cambia lo que se ve); desde el primer `.value =` manda lo asignado.
  get: function () {
    if (this.tag === "input") return this._sucio ? this._valor : (this.attrs.value || "");
    return this.tag === "textarea" ? this._valor : undefined;
  },
  set: function (v) { this._valor = String(v); this._sucio = true; }});
Object.defineProperty(Nodo.prototype, "textContent", {
  get: function () { return this.children.map(function (c) { return c.tag === "#texto" ? c.texto : c.textContent; }).join(""); },
  set: function (v) { var self = this; this.children.slice().forEach(function (c) { self.removeChild(c); });
                      if (v !== "") { var t = new Nodo("#texto"); t.texto = String(v); this.appendChild(t); } }});
Object.defineProperty(Nodo.prototype, "disabled", {get: function () { return "disabled" in this.attrs; },
  set: function (v) { if (v) this.attrs.disabled = ""; else delete this.attrs.disabled; }});
Object.defineProperty(Nodo.prototype, "title", {get: function () { return this.attrs.title || ""; }, set: function (v) { this.attrs.title = String(v); }});
Object.defineProperty(Nodo.prototype, "type", {get: function () { return this.attrs.type || ""; }, set: function (v) { this.attrs.type = String(v); }});
Object.defineProperty(Nodo.prototype, "className", {get: function () { return this.attrs["class"] || ""; }, set: function (v) { this.attrs["class"] = String(v); }});
Object.defineProperty(Nodo.prototype, "classList", {get: function () {
  var yo = this;
  function lista() { return (yo.attrs["class"] || "").split(/\s+/).filter(function (x) { return x; }); }
  return {add: function (c) { var l = lista(); if (l.indexOf(c) < 0) l.push(c); yo.attrs["class"] = l.join(" "); },
          remove: function (c) { yo.attrs["class"] = lista().filter(function (x) { return x !== c; }).join(" "); },
          contains: function (c) { return lista().indexOf(c) >= 0; }};
}});
// Selectores: UNO compuesto (etiqueta, .clases y [atributo], [atributo=valor], [atributo$=valor]). Otro, revienta.
function compuesto(sel) {
  var m = /^([a-z0-9]*)((?:\.[\w-]+|\[[^\]]+\])*)$/.exec(sel);
  if (!m) throw new Error("selector que el documento de mentira no entiende: " + sel);
  var reglas = {tag: m[1], clases: [], attrs: []}, resto = m[2], r;
  var re = /\.([\w-]+)|\[([\w-]+)(?:(\$?=)(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g;
  while ((r = re.exec(resto))) {
    if (r[1]) reglas.clases.push(r[1]);
    else reglas.attrs.push({k: r[2], op: r[3] || "", v: r[4] !== undefined ? r[4] : (r[5] !== undefined ? r[5] : r[6])});
  }
  return reglas;
}
Nodo.prototype.coincide = function (sel) {
  if (this.tag === "#texto") return false;
  var s = compuesto(sel), yo = this;
  if (s.tag && s.tag !== this.tag) return false;
  var cl = (this.attrs["class"] || "").split(/\s+/);
  if (!s.clases.every(function (c) { return cl.indexOf(c) >= 0; })) return false;
  return s.attrs.every(function (a) {
    if (!(a.k in yo.attrs)) return false;
    if (a.op === "=") return yo.attrs[a.k] === a.v;
    if (a.op === "$=") return yo.attrs[a.k].slice(-a.v.length) === a.v && a.v.length > 0;
    return true;
  });
};
Nodo.prototype.closest = function (sel) { for (var n = this; n; n = n.parent) if (n.coincide(sel)) return n; return null; };
Nodo.prototype.querySelectorAll = function (sel) {
  var fuera = [];
  (function ir(n) { n.children.forEach(function (c) { if (c.coincide(sel)) fuera.push(c); ir(c); }); })(this);   // el contenido de <template> no está en `children`
  return fuera;
};
Nodo.prototype.querySelector = function (sel) { var l = this.querySelectorAll(sel); return l.length ? l[0] : null; };

function construir(n) {
  if (typeof n === "string") { var t = new Nodo("#texto"); t.texto = n; return t; }
  var x = new Nodo(n.t);
  Object.keys(n.a).forEach(function (k) { x.attrs[k] = n.a[k]; });
  if (n.t === "template") { x.contenido = n.c.map(construir); }
  else n.c.forEach(function (c) { x.appendChild(construir(c)); });
  if (n.t === "textarea") { x._valor = x.textContent; }
  return x;
}
function clonar(n, sustituir) {
  var x = new Nodo(n.tag);
  Object.keys(n.attrs).forEach(function (k) { x.attrs[k] = sustituir(n.attrs[k]); });
  if (n.tag === "#texto") x.texto = n.texto;
  x._valor = n._valor; x._sucio = n._sucio;
  n.children.forEach(function (c) { x.appendChild(clonar(c, sustituir)); });
  if (n.tag === "textarea") x._valor = "";
  return x;
}
var raiz = construir(ARBOL);
var document = {readyState: "complete", body: raiz, documentElement: raiz,
  addEventListener: function (t, f) { (escuchas[t] = escuchas[t] || []).push(f); },
  querySelector: function (s) { return raiz.querySelector(s); }, querySelectorAll: function (s) { return raiz.querySelectorAll(s); },
  createElement: function (t) { return new Nodo(t); }};

// ── los dobles del guion de la ventana (declarados): «+ Agregar», «✕» y la vista previa ──
var rehechas = [];          // cada `input` que le llegó al documento (lo que escucha `rehacer`)
var agregados = [];         // cada «+ Agregar» pulsado
function siguiente(nombres, regla) { var m = -1; nombres.forEach(function (n) { var r = regla.exec(n); if (r && Number(r[1]) > m) m = Number(r[1]); }); return m + 1; }
function nombresDe(en) { return en.querySelectorAll("[name]").map(function (c) { return c.attrs.name; }); }
function plantillaDe(que) {
  var t = null; (function ir(n) { n.children.forEach(function (c) { if (c.tag === "template" && c.attrs["data-cz-plantilla"] === que) t = c; ir(c); }); })(raiz);
  return t;
}
function insertarClon(que, o, r, antes) {
  var t = plantillaDe(que), padre = antes.parent;
  t.contenido.forEach(function (c) {
    var copia = clonar(construir_desde_contenido(c), function (v) { return v.replace(/__O__/g, o).replace(/__R__/g, r); });
    padre.insertarAntes(copia, antes);
  });
}
function construir_desde_contenido(c) { return c; }
if (!SIN_AGREGAR) {
  document.addEventListener("click", function (e) {
    var mas = e.target.closest("[data-cz-agregar]"), quitar = e.target.closest("[data-cz-quitar]");
    var form = (mas || quitar) && (mas || quitar).closest("form.cz-form");
    if (!form) return;
    e.preventDefault();
    if (mas) {
      agregados.push(mas.attrs["data-cz-agregar"]);
      if (mas.attrs["data-cz-agregar"] === "opcion") insertarClon("opcion", String(siguiente(nombresDe(form), /^op-([0-9]+)-nombre$/)), "0", mas);
      else if (mas.attrs["data-cz-agregar"] === "renglon") {
        var op = mas.closest(".cz-op"), nombre = op.querySelector("[name$='-nombre']").attrs.name;
        insertarClon("renglon", /^op-([0-9]+)-nombre$/.exec(nombre)[1], String(siguiente(nombresDe(op), /-r-([0-9]+)-descripcion$/)), mas);
      }
    } else quitar.closest(".cz-op").remove();
  });
}
document.addEventListener("input", function (e) { rehechas.push(e.target.attrs.name || e.target.tag); });

// ── ayudas para escribir los escenarios ──
function forma() { return raiz.querySelector("form.cz-form"); }
function ops() { return forma().querySelectorAll(".cz-op").filter(function (o) { return !o.classList.contains("cz-sobra"); }); }
function bloques(op) { return op.querySelectorAll(".renglon-bloque").filter(function (b) { return !b.classList.contains("cz-sobra"); }); }
function campo(b, sufijo) { return b.querySelector("[name$='" + sufijo + "']"); }
function escribir(c, v) { c.value = v; c.dispatchEvent(new Event("input", {bubbles: true})); }
function boton(b, que) { return b.querySelector('[data-cz-ia="' + que + '"]'); }
function pedido() { return raiz.querySelector("[data-cz-ia-pedido]"); }
function caja() { return raiz.querySelector("[data-cz-ia-caja]"); }
function estadoDeArmar() { var b = boton(caja(), "borrador"); return {desactivado: b.disabled, frase: caja().querySelector("[data-cz-ia-estado]").textContent}; }
function totales() { return forma().querySelectorAll("[name$='-total']").map(function (t) { return {nombre: t.attrs.name, valor: t.value, atributo: t.getAttribute("value") || "", marcado: !!(t.closest(".total") && t.closest(".total").classList.contains("cz-falta-precio"))}; }); }
function lleno() {
  return ops().map(function (o) {
    return {nombre: campo(o, "-nombre").value, renglones: bloques(o).map(function (b) {
      return {descripcion: campo(b, "-descripcion").value, detalle: campo(b, "-detalle").value, total: campo(b, "-total").value}; })};
  });
}
"""


def _correr(html: str, escenario: str, sin_agregar: bool = False, tmp_path=None) -> dict:
    """Corre el guion REAL (sacado de `proyectos.html`) sobre la ventana pintada en `html`, y después `escenario`
    (una expresión JavaScript que termina en un valor JSON-able)."""
    if shutil.which("osascript") is None:
        pytest.skip("no hay osascript")
    arbol = _arbol_de(_ventana(html))
    programa = ("var ARBOL = " + json.dumps(arbol) + ";\nvar SIN_AGREGAR = " + ("true" if sin_agregar else "false") + ";\n"
                + _DOCUMENTO_DE_MENTIRA + "\n" + _guion_ia() + "\n"
                + "var __salida = (function () {\n" + escenario + "\n})();\n"
                + "JSON.stringify({salida: __salida, pedidos: pedidos.map(function (p) { return {url: p.url, metodo: p.metodo, cuerpo: p.cuerpo,"
                  " cabeceras: p.cabeceras, credenciales: p.credenciales}; }), rehechas: rehechas, agregados: agregados})")
    ruta = tmp_path / "escenario.js"
    ruta.write_text(programa, encoding="utf-8")
    r = subprocess.run(["osascript", "-l", "JavaScript", str(ruta)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads((r.stdout or r.stderr).strip())


@pytest.fixture
def nueva(m, tmp_path):
    html = _pagina(_casa(), p=1, cotizacion="nueva")

    def correr(escenario, **kw):
        return _correr(html, escenario, tmp_path=tmp_path, **kw)
    return correr


@pytest.fixture
def borrador_escrito(m, tmp_path):
    """La ventana de un borrador ya guardado (con textos y precios)."""
    _guardar("borrador")
    html = _pagina(_casa(), p=1, cotizacion=1, corregir=1)

    def correr(escenario, **kw):
        return _correr(html, escenario, tmp_path=tmp_path, **kw)
    return correr


PRIMER_BLOQUE = "var b0 = bloques(ops()[0])[0];\n"
FRASES = {"escribiendo": "La IA está escribiendo…"}


@hay_osascript
def test_al_cargar_no_pide_nada_y_los_botones_empiezan_como_toca(nueva):
    v = nueva(PRIMER_BLOQUE + "return {mejorar: boton(b0, 'redaccion').disabled, armar: estadoDeArmar()};")
    assert v["pedidos"] == [] and v["rehechas"] == []
    assert v["salida"]["mejorar"] is True
    assert v["salida"]["armar"] == {"desactivado": True, "frase": ia.CAUSAS["pedido_vacio"]}


@hay_osascript
def test_mejorar_redaccion_pide_a_su_ruta_y_la_propuesta_sale_al_lado_sin_tocar_el_detalle(nueva):
    escenario = PRIMER_BLOQUE + """
    escribir(campo(ops()[0], '-nombre'), 'Opción uno'); escribir(campo(b0, '-descripcion'), 'Grabación de bajo');
    var b = boton(b0, 'redaccion'), antes = b.disabled;
    escribir(campo(b0, '-detalle'), 'bajo, conga y guira');
    var activo = !b.disabled;
    b.click();
    var esperando = {desactivado: b.disabled, frase: b0.querySelector('[data-cz-ia-estado]').textContent, pedidos: pedidos.length};
    b.click(); b.dispatchEvent(new Event('click', {bubbles: true}));        // doble clic: ni deshabilitado ni directo piden otra vez
    responder(0, 200, {detalle: 'Grabación de bajo, conga y güira.', total: '99', precio: '5'});
    var lugar = b0.querySelector('[data-cz-ia-propuesta]');
    return {antes: antes, activo: activo, esperando: esperando, pedidosTrasDobleClic: pedidos.length,
            detalle: campo(b0, '-detalle').value, propuesta: lugar.querySelector('.texto').textContent,
            rotulo: lugar.querySelector('.rot-ia').textContent, usar: !!boton(lugar, 'usar'), dejar: !!boton(lugar, 'dejar'),
            estadoDespues: b0.querySelector('[data-cz-ia-estado]').textContent, reactivado: !b.disabled, totales: totales()};
    """
    v = nueva(escenario)
    s = v["salida"]
    assert s["antes"] is True and s["activo"] is True
    assert s["esperando"] == {"desactivado": True, "frase": FRASES["escribiendo"], "pedidos": 1}
    assert s["pedidosTrasDobleClic"] == 1
    assert v["pedidos"] == [{"url": RUTA_DE_LA_IA + "/redaccion", "metodo": "POST", "cuerpo": {
        "opcion": "Opción uno", "descripcion": "Grabación de bajo", "detalle": "bajo, conga y guira"},
        "cabeceras": {"Content-Type": "application/json"}, "credenciales": "same-origin"}]
    # 3. la propuesta sale al lado y el detalle no cambió
    assert s["detalle"] == "bajo, conga y guira" and s["propuesta"] == "Grabación de bajo, conga y güira."
    assert s["rotulo"] == "La IA propone" and s["usar"] and s["dejar"] and s["estadoDespues"] == "" and s["reactivado"]
    # 2. la respuesta traía `total` y `precio`: ninguna casilla de precio cambió
    assert all(t["valor"] == "" and t["atributo"] == "" for t in s["totales"]), s["totales"]


@hay_osascript
def test_usar_este_texto_cambia_el_detalle_y_rehace_la_vista_previa_y_dejar_el_mio_no_lo_cambia(nueva):
    escenario = PRIMER_BLOQUE + """
    var d = campo(b0, '-detalle'), lugar = b0.querySelector('[data-cz-ia-propuesta]');
    escribir(d, 'bajo y conga'); var base = rehechas.length;
    boton(b0, 'redaccion').click(); responder(0, 200, {detalle: 'Texto de la IA'});
    boton(lugar, 'dejar').click();
    var trasDejar = {detalle: d.value, hijos: lugar.children.length, rehechas: rehechas.length - base};
    boton(b0, 'redaccion').click(); responder(1, 200, {detalle: 'Otro texto de la IA'});
    boton(lugar, 'usar').click();
    return {trasDejar: trasDejar, trasUsar: {detalle: d.value, hijos: lugar.children.length, rehechas: rehechas.slice(base), totales: totales()}};
    """
    s = nueva(escenario)["salida"]
    assert s["trasDejar"] == {"detalle": "bajo y conga", "hijos": 0, "rehechas": 0}
    assert s["trasUsar"]["detalle"] == "Otro texto de la IA" and s["trasUsar"]["hijos"] == 0
    assert s["trasUsar"]["rehechas"] == ["op-0-r-0-detalle"], "«Usar» dispara `input` sobre el detalle: la vista previa se rehace"
    assert all(t["valor"] == "" and t["atributo"] == "" for t in s["trasUsar"]["totales"])


@hay_osascript
@pytest.mark.parametrize("final", [
    ("503", 503, {"error": ia.CAUSAS["no_contesto"], "causa": "no_contesto"}, ia.CAUSAS["no_contesto"]),
    ("422_cifra", 422, {"error": "La IA escribió una cifra que tú no diste (45000). No te la enseño: vuelve a intentarlo o escríbelo tú.",
                        "causa": "cifra"}, "La IA escribió una cifra que tú no diste (45000). No te la enseño: vuelve a intentarlo o escríbelo tú."),
    ("401", 401, {"error": "sin sesión"}, ia.CAUSAS["no_contesto"]),
    ("200_sin_detalle", 200, {"detalle": ""}, ia.CAUSAS["no_contesto"]),
    ("200_otra_forma", 200, {"opciones": []}, ia.CAUSAS["no_contesto"]),
], ids=lambda f: f[0] if isinstance(f, tuple) else None)
def test_si_la_ia_falla_se_dice_con_la_frase_y_lo_escrito_sigue_igual(nueva, final):
    _, estado, cuerpo, frase = final
    escenario = PRIMER_BLOQUE + f"""
    var d = campo(b0, '-detalle'); escribir(d, 'bajo y conga');
    boton(b0, 'redaccion').click(); responder(0, {estado}, {json.dumps(cuerpo)});
    var lugar = b0.querySelector('[data-cz-ia-propuesta]');
    return {{detalle: d.value, error: lugar.querySelector('.ia-error').textContent, propuesta: !!lugar.querySelector('.ia-propuesta'),
            reactivado: !boton(b0, 'redaccion').disabled}};
    """
    s = nueva(escenario)["salida"]
    assert s == {"detalle": "bajo y conga", "error": frase, "propuesta": False, "reactivado": True}


@hay_osascript
@pytest.mark.parametrize("como", ["sin_red", "no_es_json"])
def test_sin_red_o_sin_json_es_la_frase_de_no_contesto(nueva, como):
    cae = "sinRed(0)" if como == "sin_red" else "noEsJson(0, 502)"
    s = nueva(PRIMER_BLOQUE + f"""
    escribir(campo(b0, '-detalle'), 'bajo'); boton(b0, 'redaccion').click(); {cae};
    return {{error: b0.querySelector('.ia-error').textContent, detalle: campo(b0, '-detalle').value}};""")["salida"]
    assert s == {"error": ia.CAUSAS["no_contesto"], "detalle": "bajo"}


@hay_osascript
def test_mejorar_sin_detalle_no_pide_nada_ni_con_un_clic_directo(nueva):
    v = nueva(PRIMER_BLOQUE + """
    var b = boton(b0, 'redaccion'); b.click(); b.dispatchEvent(new Event('click', {bubbles: true}));
    escribir(campo(b0, '-detalle'), '   ');
    b.dispatchEvent(new Event('click', {bubbles: true}));
    return {desactivado: b.disabled, titulo: b.title};""")
    assert v["pedidos"] == [] and v["salida"] == {"desactivado": True, "titulo": ia.CAUSAS["detalle_vacio"]}


@hay_osascript
def test_un_renglon_agregado_con_el_mas_trae_su_boton_y_funciona(nueva):
    v = nueva("""
    var op = ops()[0], mas = op.querySelector('[data-cz-agregar="renglon"]'); mas.click();
    var b1 = bloques(op)[1], boton1 = boton(b1, 'redaccion'), antes = boton1.disabled;
    escribir(campo(b1, '-detalle'), 'mezcla'); var despues = boton1.disabled;
    boton1.click(); responder(0, 200, {detalle: 'Mezcla y máster.'});
    return {bloques: bloques(op).length, antes: antes, despues: despues, propuesta: b1.querySelector('.texto').textContent,
            nombre: campo(b1, '-detalle').attrs.name};""")
    s = v["salida"]
    assert s == {"bloques": 2, "antes": True, "despues": False, "propuesta": "Mezcla y máster.", "nombre": "op-0-r-2-detalle"}      # el r-1 es el renglón de sobra (escondido) del servidor
    assert v["pedidos"][0]["cuerpo"]["detalle"] == "mezcla"


# ── Armar borrador ──────────────────────────────────────────────────────────────────────────────────────

PROPUESTA = {"opciones": [
    {"nombre": "Músicos de calle", "total": "9", "renglones": [
        {"descripcion": "Grabación de bajo y güira", "detalle": "Incluye horas de estudio.", "total": "45000", "precio": "1"},
        {"descripcion": "Mezcla y máster", "detalle": "Canción lista.", "total": "5000"}]},
    {"nombre": "Músicos de conservatorio", "renglones": [
        {"descripcion": "Grabación de bajo y güira", "detalle": "Con formación académica.", "total": "RD$ 99,000"}]},
    {"nombre": "Músicos graduados afuera", "renglones": [
        {"descripcion": "Grabación completa", "detalle": "Formación internacional."}]}], "total": "1"}


@hay_osascript
def test_armar_borrador_llena_los_textos_deja_el_precio_vacio_y_marcado_y_rehace_la_vista_previa(nueva):
    escenario = f"""
    escribir(pedido(), 'quiere grabar un merengue con tres tipos de músicos');
    var activo = !boton(caja(), 'borrador').disabled;
    boton(caja(), 'borrador').click();
    var esperando = estadoDeArmar(), pedidosEsperando = pedidos.length;
    var base = rehechas.length;
    responder(0, 200, {json.dumps(PROPUESTA)});
    return {{activo: activo, esperando: esperando, pedidosEsperando: pedidosEsperando, lleno: lleno(), totales: totales(),
            aviso: raiz.querySelector('[data-cz-ia-aviso]').textContent, error: raiz.querySelector('[data-cz-ia-error]').children.length,
            marcas: forma().querySelectorAll('[name$="-total"]').map(function (t) {{ return t.getAttribute('placeholder'); }}),
            rehechas: rehechas.length - base, opcionesVisibles: ops().length, agregados: agregados,
            despues: estadoDeArmar()}};
    """
    v = nueva(escenario)
    s = v["salida"]
    assert s["activo"] is True
    assert s["esperando"] == {"desactivado": True, "frase": FRASES["escribiendo"]} and s["pedidosEsperando"] == 1
    assert v["pedidos"] == [{"url": RUTA_DE_LA_IA + "/borrador", "metodo": "POST", "cabeceras": {"Content-Type": "application/json"},
                             "cuerpo": {"pedido": "quiere grabar un merengue con tres tipos de músicos"}, "credenciales": "same-origin"}]
    # los textos, tal cual, en el orden de la propuesta
    assert [o["nombre"] for o in s["lleno"]] == ["Músicos de calle", "Músicos de conservatorio", "Músicos graduados afuera"]
    assert [[r["descripcion"] for r in o["renglones"]] for o in s["lleno"]] == [
        ["Grabación de bajo y güira", "Mezcla y máster"], ["Grabación de bajo y güira"], ["Grabación completa"]]
    assert [[r["detalle"] for r in o["renglones"]] for o in s["lleno"]] == [
        ["Incluye horas de estudio.", "Canción lista."], ["Con formación académica."], ["Formación internacional."]]
    # 2. NINGUNA casilla de precio recibe nada de la respuesta (traía `total`, `precio` y «RD$ 99,000»), y todas quedan marcadas
    assert [r["total"] for o in s["lleno"] for r in o["renglones"]] == [""] * 4
    assert len(s["totales"]) >= 4 and all(t["valor"] == "" and t["atributo"] == "" for t in s["totales"])
    assert sum(1 for t in s["totales"] if t["marcado"]) == 4, "una marca por cada renglón que llenó la IA"
    assert s["marcas"].count("pon el precio") == 4
    assert s["aviso"] == "Esto lo propuso la IA. Revisa cada texto y pon los precios (en amarillo). Todavía no se guardó nada."
    assert s["error"] == 0 and s["rehechas"] >= 1, "5. la vista previa se rehace (llega un `input` al documento)"
    assert s["opcionesVisibles"] == 3 and s["agregados"].count("opcion") == 2 and s["agregados"].count("renglon") == 1
    assert s["despues"]["desactivado"] is True, "ya hay texto: no se puede armar otra vez encima"


@hay_osascript
def test_al_escribir_el_precio_se_quita_solo_la_marca_de_esa_casilla(nueva):
    s = nueva(f"""
    escribir(pedido(), 'algo'); boton(caja(), 'borrador').click(); responder(0, 200, {json.dumps(PROPUESTA)});
    var primeras = forma().querySelectorAll('[name$="-total"]').filter(function (t) {{ return t.closest('.total').classList.contains('cz-falta-precio'); }});
    escribir(primeras[0], '45000'); var tras = totales().filter(function (t) {{ return t.marcado; }}).length;
    escribir(primeras[1], '  '); var trasEspacios = totales().filter(function (t) {{ return t.marcado; }}).length;
    return {{marcadasAntes: primeras.length, tras: tras, trasEspacios: trasEspacios, valor: primeras[0].value}};""")["salida"]
    assert s == {"marcadasAntes": 4, "tras": 3, "trasEspacios": 3, "valor": "45000"}


@hay_osascript
@pytest.mark.parametrize("sufijo", ["-nombre", "-descripcion", "-detalle", "-total"])
def test_armar_borrador_con_una_casilla_escrita_no_pide_nada_ni_con_un_clic_directo(nueva, sufijo):
    v = nueva(f"""
    var op = ops()[0], b = bloques(op)[0];
    escribir(pedido(), 'quiere grabar');
    var libre = !boton(caja(), 'borrador').disabled;
    escribir(campo({'op' if sufijo == '-nombre' else 'b'}, '{sufijo}'), 'algo');
    var desactivado = estadoDeArmar();
    boton(caja(), 'borrador').click();
    boton(caja(), 'borrador').dispatchEvent(new Event('click', {{bubbles: true}}));
    return {{libre: libre, desactivado: desactivado, lleno: lleno()}};""")
    s = v["salida"]
    assert v["pedidos"] == []
    assert s["libre"] is True
    assert s["desactivado"] == {"desactivado": True, "frase": "Solo con las opciones vacías: usa «Mejorar redacción» en cada renglón."}
    assert sum(1 for o in s["lleno"] for x in [o["nombre"]] if x) + sum(
        1 for o in s["lleno"] for r in o["renglones"] for x in (r["descripcion"], r["detalle"], r["total"]) if x) == 1, "nada más se escribió"


@hay_osascript
def test_armar_borrador_sin_pedido_no_pide_nada_ni_con_un_clic_directo(nueva):
    v = nueva("""
    escribir(pedido(), '   ');
    boton(caja(), 'borrador').dispatchEvent(new Event('click', {bubbles: true}));
    return estadoDeArmar();""")
    assert v["pedidos"] == [] and v["salida"] == {"desactivado": True, "frase": ia.CAUSAS["pedido_vacio"]}


@hay_osascript
def test_si_la_ventana_ya_traia_texto_armar_borrador_sale_desactivado_con_su_frase(borrador_escrito):
    v = borrador_escrito("""
    escribir(pedido(), 'quiere grabar');
    boton(caja(), 'borrador').dispatchEvent(new Event('click', {bubbles: true}));
    return {estado: estadoDeArmar(), mejorar: boton(bloques(ops()[0])[0], 'redaccion').disabled, detalleVacio: campo(bloques(ops()[0])[0], '-detalle').value === ''};""")
    assert v["pedidos"] == []
    assert v["salida"]["estado"]["desactivado"] is True and "Solo con las opciones vacías" in v["salida"]["estado"]["frase"]


@hay_osascript
def test_si_la_persona_escribe_mientras_la_ia_piensa_no_se_pisa_nada(nueva):
    s = nueva(f"""
    escribir(pedido(), 'quiere grabar'); boton(caja(), 'borrador').click();
    escribir(campo(ops()[0], '-nombre'), 'Lo mío');
    responder(0, 200, {json.dumps(PROPUESTA)});
    return {{lleno: lleno(), error: raiz.querySelector('[data-cz-ia-error]').textContent, aviso: raiz.querySelector('[data-cz-ia-aviso]').children.length,
            totalesMarcados: totales().filter(function (t) {{ return t.marcado; }}).length}};""")["salida"]
    assert s["lleno"] == [{"nombre": "Lo mío", "renglones": [{"descripcion": "", "detalle": "", "total": ""}]}]
    assert "Solo con las opciones vacías" in s["error"] and s["aviso"] == 0 and s["totalesMarcados"] == 0


@hay_osascript
@pytest.mark.parametrize("respuesta", [
    (503, {"error": ia.CAUSAS["no_contesto"], "causa": "no_contesto"}, ia.CAUSAS["no_contesto"]),
    (422, {"error": "La IA contestó algo que no se puede usar. Vuelve a intentarlo.", "causa": "no_sirve"},
     "La IA contestó algo que no se puede usar. Vuelve a intentarlo."),
    (200, {"opciones": []}, ia.CAUSAS["no_contesto"]),
    (200, {"opciones": [{"nombre": "A", "renglones": []}]}, ia.CAUSAS["no_contesto"]),
    (200, {"opciones": "texto"}, ia.CAUSAS["no_contesto"]),
    (200, {"opciones": [{"nombre": "A"}]}, ia.CAUSAS["no_contesto"]),
    (200, {"opciones": [None]}, ia.CAUSAS["no_contesto"]),
    (200, {}, ia.CAUSAS["no_contesto"]),
])
def test_si_la_ia_falla_al_armar_el_borrador_no_se_llena_nada_y_se_dice(nueva, respuesta):
    estado, cuerpo, frase = respuesta
    s = nueva(f"""
    escribir(pedido(), 'quiere grabar'); boton(caja(), 'borrador').click(); responder(0, {estado}, {json.dumps(cuerpo)});
    return {{error: raiz.querySelector('[data-cz-ia-error]').querySelector('.ia-error').textContent, lleno: lleno(), aviso: raiz.querySelector('[data-cz-ia-aviso]').children.length,
            armar: estadoDeArmar(), pedido: pedido().value}};""")["salida"]
    assert s["error"] == frase and s["aviso"] == 0 and s["pedido"] == "quiere grabar"
    assert s["lleno"] == [{"nombre": "", "renglones": [{"descripcion": "", "detalle": "", "total": ""}]}]
    assert s["armar"] == {"desactivado": False, "frase": ""}, "puede volver a pulsar"


@hay_osascript
def test_si_no_hay_como_hacer_sitio_no_se_llena_nada(nueva):
    """Sin los «+ Agregar» (el guion de la ventana no cargó), la propuesta de 3 opciones no cabe: no se llena NADA a medias."""
    s = nueva(f"""
    escribir(pedido(), 'quiere grabar'); boton(caja(), 'borrador').click(); responder(0, 200, {json.dumps(PROPUESTA)});
    return {{lleno: lleno(), error: raiz.querySelector('[data-cz-ia-error]').textContent}};""", sin_agregar=True)["salida"]
    assert s["lleno"] == [{"nombre": "", "renglones": [{"descripcion": "", "detalle": "", "total": ""}]}]
    assert s["error"] == ia.CAUSAS["no_contesto"]


@hay_osascript
def test_sin_sitio_para_otra_opcion_aunque_cada_una_traiga_un_solo_renglon_no_se_llena_nada(nueva):
    """Lo mismo, con una propuesta de dos opciones de un renglón: falta sitio solo para la OPCIÓN (los renglones cabrían)."""
    dos = {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "D1", "detalle": "T1"}]},
                        {"nombre": "B", "renglones": [{"descripcion": "D2", "detalle": "T2"}]}]}
    s = nueva(f"""
    escribir(pedido(), 'quiere grabar'); boton(caja(), 'borrador').click(); responder(0, 200, {json.dumps(dos)});
    return {{lleno: lleno(), error: raiz.querySelector('[data-cz-ia-error]').textContent, aviso: raiz.querySelector('[data-cz-ia-aviso]').children.length}};""",
              sin_agregar=True)["salida"]
    assert s["lleno"] == [{"nombre": "", "renglones": [{"descripcion": "", "detalle": "", "total": ""}]}]
    assert s["error"] == ia.CAUSAS["no_contesto"] and s["aviso"] == 0


@hay_osascript
def test_una_opcion_en_blanco_de_mas_se_quita_y_las_que_la_persona_agrego_vacias_no_estorban(nueva):
    """La ventana trae 1 opción visible; la persona agrega otra en blanco y pide un borrador de 1 sola: la de más se quita."""
    s = nueva("""
    forma().querySelector('[data-cz-agregar="opcion"]').click();
    var antes = ops().length;
    escribir(pedido(), 'quiere grabar'); boton(caja(), 'borrador').click();
    responder(0, 200, {opciones: [{nombre: 'Única', renglones: [{descripcion: 'D', detalle: 'T'}]}]});
    return {antes: antes, despues: ops().length, lleno: lleno()};""")["salida"]
    assert s["antes"] == 2 and s["despues"] == 1
    assert s["lleno"] == [{"nombre": "Única", "renglones": [{"descripcion": "D", "detalle": "T", "total": ""}]}]


# ── Las direcciones que de verdad pide el guion ─────────────────────────────────────────────────────────

@hay_osascript
def test_todo_lo_que_pide_el_guion_va_a_una_ruta_registrada_por_post(nueva):
    """Con TODO lo que el guion sabe pedir (los dos botones, éxito y fallo), cada dirección pedida es una ruta POST
    registrada en la aplicación (se compara con `app.routes`, no con un texto escrito aquí), con JSON y la cookie de la casa."""
    v = nueva(PRIMER_BLOQUE + f"""
    escribir(campo(b0, '-detalle'), 'bajo'); boton(b0, 'redaccion').click(); responder(0, 200, {{detalle: 'X'}});
    boton(b0, 'redaccion').click(); sinRed(1);
    escribir(campo(b0, '-detalle'), ''); campo(b0, '-detalle').value = '';
    forma().querySelectorAll('input').concat(forma().querySelectorAll('textarea')).forEach(function (c) {{ if (c.attrs.name) c.value = ''; }});
    escribir(pedido(), 'algo'); boton(caja(), 'borrador').click(); responder(2, 503, {{error: 'x', causa: 'no_contesto'}});
    return pedidos.length;""")
    assert v["salida"] == 3 and len(v["pedidos"]) == 3
    registradas = {r.path.replace("{pid}", "1") for r in panel.app.routes if "POST" in (getattr(r, "methods", None) or ())}
    for p in v["pedidos"]:
        assert p["url"] in registradas and re.fullmatch(r"/proyectos/1/cotizaciones/ia/(redaccion|borrador)", p["url"]), p
        assert p["metodo"] == "POST" and p["cabeceras"] == {"Content-Type": "application/json"} and p["credenciales"] == "same-origin"
    assert sorted(p["url"].rsplit("/", 1)[1] for p in v["pedidos"]) == ["borrador", "redaccion", "redaccion"]
