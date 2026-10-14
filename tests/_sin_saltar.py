"""La pieza única de «tocar algo sin que la página se mueva»
(`web/plantillas/_marcar_sin_saltar.html`) vista desde las pruebas: su texto de JavaScript, y cómo
sacarla de un guion ya pintado para vigilar el RESTO del guion aparte.

No ejecuta nada ni decide nada: solo lee la plantilla (la fuente real, no una copia) y la separa."""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_PIEZA = RAIZ / "web" / "plantillas" / "_marcar_sin_saltar.html"


def sin_comentarios_de_js(codigo: str) -> str:
    return re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)


def texto_de_la_pieza() -> str:
    """El JavaScript de la pieza tal como Jinja lo escribe en la página: la plantilla sin su
    comentario de cabecera (`{# ... #}`)."""
    fuente = RUTA_PIEZA.read_text(encoding="utf-8")
    sin_cabecera = re.sub(r"\A\{#.*?#\}", "", fuente, count=1, flags=re.S)
    assert sin_cabecera != fuente and "{#" not in sin_cabecera, "la pieza ya no empieza con su comentario de Jinja"
    return sin_cabecera.rstrip("\n")


def la_pieza_de(guion: str) -> tuple[str, str]:
    """(la pieza, el guion SIN ella). La pieza tiene que estar entera, una sola vez; si al guion ya
    se le quitaron los comentarios, se busca la pieza sin los suyos."""
    pieza = texto_de_la_pieza()
    if guion.count(pieza) != 1:
        pieza = sin_comentarios_de_js(pieza)
    assert guion.count(pieza) == 1, "el guion no trae la pieza única de sin saltar, entera y una vez"
    return pieza, guion.replace(pieza, "")


# LO QUE LA PIEZA TIENE PERMITIDO, uno por uno y con su cuenta. Todo lo demás de la lista de
# prohibiciones (las mismas de siempre del guion de la página) sobre el texto de la pieza sin
# comentarios y sin esto, tiene que dar vacío. Cada línea: (texto, veces, para qué).
PERMITIDO_EN_LA_PIEZA = (
    # el único pedido de red: a la dirección que decidió la puerta (`destinoSinSaltar`); con cuerpo
    # (un POST) solo para un formulario, sin él es un GET.
    ("fetch(p.url, opciones)", 1),
    # el envío de siempre de un formulario cuando el navegador no sabe `requestSubmit` (con el botón que
    # lo envió si lo sabe: `form.requestSubmit(envia || undefined)`).
    ("form.submit()", 1),
    # el formulario leído: sus campos y su dirección (solo lectura).
    ("new FormData(form)", 1), ("form.action", 1),
    # leer dónde está la página (nunca escribirlo: ir a otro sitio se hace con un enlace suelto).
    ("window.location.href", 2), ("window.location.origin", 1), ("window.location.pathname", 4),
    ("window.location.search", 3),
    # preguntar si el navegador sabe hacerlo (nunca llamarlo fuera de `fetch(p.url, opciones)`).
    ("window.fetch", 2), ("window.FormData", 1),
    # elementos nuevos: el enlace suelto (ir sin reenviar), el aviso fijo de «no se pudo confirmar» con su
    # enlace para mirar cómo quedó, y el guion de «Copiar» que la página nueva trae y ésta no.
    ('document.createElement("a")', 2), ('document.createElement("div")', 2), ('document.createElement("button")', 1),
    ('document.createElement("script")', 1),
)
PROHIBIDO_EN_LA_PIEZA = re.compile(
    r"fetch|XMLHttpRequest|sendBeacon|WebSocket|EventSource|localStorage|sessionStorage|\.submit\(|"
    r"\blocation\b|\.action|FormData|innerHTML|outerHTML|insertAdjacentHTML|document\.write|document\.open|"
    r"document\.close|eval|Function\b|import\(|(?<![.\w])(self|globalThis|top|parent|frames)\b|"
    r"\bwindow\s*\[|\bthis\b|Reflect|Object\.|\[\s*[\"'`]|setTimeout|setInterval|\.open\(|postMessage|"
    r"navigator|\.cookie|createElement")


def lo_prohibido_en_la_pieza(pieza: str) -> list[str]:
    """Quita los comentarios y lo PERMITIDO (comprobando cuántas veces está) y devuelve lo prohibido
    que quede. Un permitido que no aparece la cuenta exacta es un `AssertionError`."""
    codigo = re.sub(r"\s+", " ", sin_comentarios_de_js(pieza))
    for permitido, veces in PERMITIDO_EN_LA_PIEZA:
        assert codigo.count(permitido) == veces, (permitido, codigo.count(permitido))
        codigo = codigo.replace(permitido, "")
    return [m.group(0) for m in PROHIBIDO_EN_LA_PIEZA.finditer(codigo)]


# ═══════════════════════════════════════════════════════════════════════
# EL NAVEGADOR DE MENTIRA (JavaScriptCore, `osascript -l JavaScript`)
# ═══════════════════════════════════════════════════════════════════════
# FRONTERA, dicha: esto NO es un navegador. Imita, con lo mínimo, lo que la pieza le pide a uno:
#   · `fetch` devuelve un futuro que el ESCENARIO resuelve cuando quiere (así se prueban las carreras:
#     «contestó tarde», «dos toques seguidos»). Lo que afirma sobre el servidor (que el POST sigue la
#     redirección y `r.url` es la final SIN el `#ancla`; que `r.ok` es falso en 4xx/5xx; que una cookie
#     puesta por el POST viaja en el GET que sigue) sale de la especificación de Fetch, no de esta
#     mentira: se comprobó en un navegador por la sala, no aquí.
#   · `history.pushState/replaceState` solo anotan la llamada y mueven `location` (el navegador además
#     restaura el desplazamiento por su cuenta; aquí no).
#   · `URL` resuelve direcciones `http(s)` absolutas y `/ruta?consulta#ancla`, `?consulta`, `#ancla`; otro
#     esquema (`javascript:`, `mailto:`) tiene origen «null», como en un navegador.
#   · el DOM son nodos de mano: `closest`, `querySelectorAll`, `getBoundingClientRect` salen de lo que
#     cada prueba arma. Un `click()` sobre un enlace suelto (`irA`) solo anota a dónde iría.
# Los eventos van a TODOS los oyentes en el orden en que se registraron, con `defaultPrevented`, como un
# navegador (el arnés de `test_escrituras_proyecto.py` guarda uno solo por tipo y no sirve aquí).
NAVEGADOR = r"""
var __oy = {}, __oyW = {};
function disparar(tipo, e) {
  e.defaultPrevented = false;
  e.preventDefault = function () { this.defaultPrevented = true; };
  (__oy[tipo] || []).forEach(function (f) { f(e); });
  return e;
}
function dispararVentana(tipo, e) { (__oyW[tipo] || []).forEach(function (f) { f(e || {}); }); }
function Futuro() { this.cb = []; this.est = 0; this.v = undefined; }
Futuro.prototype.resolver = function (v) { if (this.est) return; this.est = 1; this.v = v; this._vaciar(); };
Futuro.prototype.rechazar = function (e) { if (this.est) return; this.est = 2; this.v = e; this._vaciar(); };
Futuro.prototype._vaciar = function () { var c = this.cb; this.cb = []; c.forEach(function (f) { f(); }); };
Futuro.prototype.then = function (f, g) {
  var sig = new Futuro(), self = this;
  var correr = function () {
    var h = self.est === 1 ? f : g;
    if (!h) { if (self.est === 1) sig.resolver(self.v); else sig.rechazar(self.v); return; }
    try {
      var r = h(self.v);
      if (r && r.then) r.then(function (x) { sig.resolver(x); }, function (e) { sig.rechazar(e); });
      else sig.resolver(r);
    } catch (e) { sig.rechazar(e); }
  };
  if (self.est) correr(); else self.cb.push(correr);
  return sig;
};
Futuro.prototype.catch = function (g) { return this.then(undefined, g); };
function URL(u, base) {
  var m = /^(https?:)\/\/([^\/?#]+)([^?#]*)(\?[^#]*)?(#.*)?$/.exec(u);
  if (!m) {
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u)) { this.origin = "null"; this.pathname = u; this.search = ""; this.hash = ""; return; }
    var b = new URL(base), r = /^([^?#]*)(\?[^#]*)?(#.*)?$/.exec(u);
    this.origin = b.origin;
    this.pathname = r[1] === "" ? b.pathname : r[1];
    this.search = r[1] === "" && r[2] === undefined && r[3] === undefined ? b.search : (r[2] || "");
    this.hash = r[3] || "";
    return;
  }
  this.origin = m[1] + "//" + m[2]; this.pathname = m[3] || "/"; this.search = m[4] || ""; this.hash = m[5] || "";
}
function URLSearchParams(x) { this.pares = []; var s = this; if (x && x.pares) x.pares.forEach(function (p) { s.pares.push(p.slice()); }); }
URLSearchParams.prototype.append = function (k, v) { this.pares.push([k, v]); };
URLSearchParams.prototype.toString = function () {
  return this.pares.map(function (p) { return encodeURIComponent(p[0]) + "=" + encodeURIComponent(p[1]); }).join("&");
};
function FormData(form) { this.pares = (form.campos || []).map(function (p) { return p.slice(); }); }
var location = {href: "http://x.test/proyectos?p=1", origin: "http://x.test", pathname: "/proyectos", search: "?p=1"};
function irAlSitio(u) { var d = new URL(u, location.href); location.href = d.origin + d.pathname + d.search; location.pathname = d.pathname; location.search = d.search; }
var historia = {llamadas: [], pushState: function (s, t, u) { this.llamadas.push(["push", s, u]); irAlSitio(u); },
                replaceState: function (s, t, u) { this.llamadas.push(["replace", s, u === undefined ? null : u]); if (u !== undefined) irAlSitio(u); }};
var history = historia;
var pedidos = [], sinFetch = false;
var fetch = function (url, op) {
  var f = new Futuro();
  pedidos.push({url: url, metodo: (op && op.method) || "GET", cuerpo: op && op.body ? op.body.toString() : null, f: f});
  return f;
};
function responder(i, texto, extra) {
  var r = {ok: true, status: 200, url: "http://x.test" + pedidos[i].url, text: function () { var f = new Futuro(); f.resolver(texto); return f; }};
  for (var k in (extra || {})) r[k] = extra[k];
  pedidos[i].f.resolver(r);
}
function sinRed(i) { pedidos[i].f.rechazar(new Error("sin red")); }
var salidas = [], reemplazos = {}, vivos = {}, ventanaScroll = [], escritos = [], timerCola = [];
function campo(tipo, nombre, valor, defecto, accion) {
  /* un campo de texto de un formulario de mentira (su `form` sabe su `action`) */
  var f = {dataset: {}, getAttribute: function (k) { return k === "action" ? accion : null; }};
  return {type: tipo, name: nombre, value: valor, defaultValue: defecto, form: f, focos: 0, rangos: [],
    focus: function () { this.focos++; }, setSelectionRange: function (a, b) { this.rangos.push([a, b]); }};
}
function nodo(rol, y, desde, detalles, campos) {
  var n = {rol: rol, desde: desde, scrollTop: y, scrollHeight: 3000, clientHeight: 700, detalles: detalles || [], campos: campos || [],
    getAttribute: function (a) { return a === "data-region" ? rol : null; },
    querySelector: function () { return null; },
    querySelectorAll: function (s) { return s === "details" ? this.detalles : (s === "input, textarea" ? this.campos : []); },
    closest: function () { return null; },
    getBoundingClientRect: function () { return {top: 0, bottom: 0, height: 0}; },
    replaceWith: function (n2) { vivos[rol] = n2; __fase = "despues"; }};
  return n;
}
var paginas = {};   // texto -> {detalles: {izquierda: [...], centro: [...]}, copiar: bool, titulo: str, regiones: [..]}
function DOMParser() {}
DOMParser.prototype.parseFromString = function (texto) {
  var p = paginas[texto] || {};
  var roles = p.regiones || ["izquierda", "centro"];
  var regs = roles.map(function (r) { var n = nodo(r, 0, texto, (p.detalles || {})[r], (p.campos || {})[r]); if (p.sinScroll) n.scrollHeight = 700; return n; });
  var avisos = (p.avisos || []).map(function (t) { return {textContent: " " + t + " "}; });
  return {title: p.titulo || "", querySelectorAll: function (s) { return s === "[data-region]" ? regs : (s === "p.aviso" ? avisos : []); },
          querySelector: function (s) { return s === "#guion-copiar-carpeta" && p.copiar ? {textContent: "COPIAR-DEL-SERVIDOR"} : null; }};
};
var __filas = null, __fase = "antes";
var document = {
  documentElement: {className: "", classList: {add: function () {}}},
  title: "TITULO-VIEJO",
  activeElement: null,
  body: {firstChild: {}, parentNode: null,
    appendChild: function (n) { salidas.push(["guion-nuevo", n.id, n.textContent]); },
    insertBefore: function (n, ref) { n.parentNode = document.body; avisosPuestos.push(n); salidas.push(["aviso", n.hijos.filter(function (h) { return h.tag === "div"; }).map(function (h) { return h.textContent; }),
      n.hijos.filter(function (h) { return h.tag === "a"; })[0].atributos.href]); n.cerrar = function () { n.hijos.filter(function (h) { return h.tag === "button"; })[0].onclick(); }; },
    removeChild: function (n) { avisosPuestos.splice(avisosPuestos.indexOf(n), 1); salidas.push(["aviso quitado"]); }},
  addEventListener: function (t, f) { (__oy[t] = __oy[t] || []).push(f); },
  querySelector: function () { return null; },
  querySelectorAll: function (s) {
    if (s === "[data-region]") return roles_vivos();
    if (s === "[data-ancla][id]") return __fase === "antes" && __filas ? __filas : [];
    return []; },
  getElementById: function (id) { return reemplazos[id] || null; },
  importNode: function (n) { if (reemplazos.__importar_falla) throw new Error("x"); return n; },
  createElement: function (t) {
    var n = {tag: t, atributos: {}, hijos: [], parentNode: null, textContent: "",
      click: function () { salidas.push(["ir", this.href]); },
      setAttribute: function (k, v) { this.atributos[k] = v; }, getAttribute: function (k) { return this.atributos[k] === undefined ? null : this.atributos[k]; },
      appendChild: function (h) { this.hijos.push(h); h.parentNode = this; }};
    return n; }
};
var avisosPuestos = [];
function roles_vivos() { return [vivos.izquierda, vivos.centro]; }
vivos.izquierda = nodo("izquierda", 300, "VIEJA"); vivos.centro = nodo("centro", 1200, "VIEJA");
var window = {fetch: fetch, URLSearchParams: URLSearchParams, DOMParser: DOMParser, FormData: FormData, history: historia,
  location: location, pageYOffset: 50, pageXOffset: 0,
  scrollTo: function (x, y) { ventanaScroll.push([x, y]); },
  addEventListener: function (t, f) { (__oyW[t] = __oyW[t] || []).push(f); }};
/* Un enlace y un formulario de mentira, con lo que el guion les pregunta. */
function enlace(href, extra) {
  var a = {href: href, atributos: {href: href}, tag: "a",
    getAttribute: function (k) { return this.atributos[k] === undefined ? null : this.atributos[k]; },
    hasAttribute: function (k) { return this.atributos[k] !== undefined; },
    closest: function (s) { return s === "a[href]" ? this : null; }};
  for (var k in (extra || {})) a.atributos[k] = extra[k];
  return a;
}
function formulario(accion, metodo, campos, extra) {
  var boton = {disabled: false};
  var f = {action: accion, tag: "form", campos: campos || [], dataset: {}, boton: boton, enviados: 0,
    atributos: {action: accion, method: metodo},
    getAttribute: function (k) { return this.atributos[k] === undefined ? null : this.atributos[k]; },
    classList: {contains: function (c) { return (f.clases || []).indexOf(c) >= 0; }},
    querySelector: function (s) { return s === "button" ? boton : null; },
    querySelectorAll: function () { return this.controles || []; },
    closest: function () { return null; },
    isConnected: true,
    submit: function () { salidas.push(["submit", this.action]); },
    requestSubmit: function (b) { salidas.push(["requestSubmit", this.action, b && b.name !== undefined ? b.name : null]); }};
  for (var k in (extra || {})) f.atributos[k] = extra[k];
  return f;
}
function clic(destino, extra) { var e = {target: destino}; for (var k in (extra || {})) e[k] = extra[k]; return disparar("click", e); }
function enviar(form, submitter) { return disparar("submit", {target: form, submitter: submitter}); }
"""
NAVEGADOR += r"""
var __timers = [];
function setTimeout(f, ms) { __timers.push(f); return __timers.length; }
function clearTimeout(id) { if (id) __timers[id - 1] = null; }
function correrTimers() { var t = __timers; __timers = []; t.forEach(function (f) { if (f) f(); }); }
"""


def correr_en_navegador(script: str, escenario: str, previo: str = "") -> dict:
    """Corre `script` (el de la página) en el navegador de mentira y el `escenario`, que termina en una
    expresión JSON. `previo` va antes del guion (para quitarle algo al navegador antes de que cargue)."""
    import json
    import subprocess
    programa = NAVEGADOR + previo + "\n" + script + "\n" + escenario
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", programa], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


FOTO = ("JSON.stringify({prevenido: __e && __e.defaultPrevented, pedidos: pedidos.map(function (p) { return [p.metodo, p.url, p.cuerpo]; }),"
        " historia: historia.llamadas, ventana: ventanaScroll, izq: vivos.izquierda.desde, cen: vivos.centro.desde,"
        " izqTop: vivos.izquierda.scrollTop, cenTop: vivos.centro.scrollTop, salidas: salidas, escritos: escritos,"
        " titulo: document.title, donde: location.pathname + location.search})")


def correr(script: str, escenario: str, previo: str = "") -> dict:
    """`correr_en_navegador` y, al final, la foto de todo lo que el guion hizo (`FOTO`)."""
    return correr_en_navegador(script, "var __e = null;\n" + escenario + "\n" + FOTO, previo)
