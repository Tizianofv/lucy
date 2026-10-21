"""La vista previa de una factura mientras se escribe (pieza 4b, 21-oct-2026): la hoja al lado del formulario, en
«Nueva factura» (el paso de escribir) y en «Corregir el borrador».

Qué se vigila, por las rutas y las plantillas REALES, sobre la puerta REAL de `db/db.py` en SQLite (el mundo de
`tests/test_guardar_factura.py`) y la App de registro doblada:

  1. LA MISMA CUENTA: la hoja de la vista previa es, con los mismos datos, el MISMO diccionario que la hoja de una
     factura guardada (`facturas.hoja_de_factura`), que sale de `db.totales_de_factura`; con el ejemplo D4 (bruto
     10,000 con NCF e ITBIS) las cifras son 13,882.36 de total y 10,000.00 a pagar.
  2. NO GUARDA: la ruta `POST /facturas/vista-previa` no escribe una sola fila —la foto de la base antes y después—.
  3. DICE «BORRADOR» y no inventa un número.
  4. CON DATOS A MEDIAS lo dice suave, sin cifras y sin reventar (200 con la nota).
  5. LAS DOS PANTALLAS la traen al lado del formulario y el guion solo aparece cuando hay formulario.
  6. EL GUION (el de la página, corrido en JavaScriptCore con un navegador de mentira): pide la hoja al servidor al
     escribir, espera a que pares, no pinta si el servidor no contesta y NUNCA envía el formulario.
  7. LA COTIZACIÓN SIGUE IGUAL: la huella de su vista previa y del texto de su PDF, medidas antes de esta pieza.

FRONTERA: no es un navegador (ver el arnés); la base es SQLite y la App es un doble, como en
`tests/test_pantalla_de_facturas.py`. El aspecto no se mira: se miran el HTML y las respuestas.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)
from pypdf import PdfReader
from starlette.datastructures import FormData

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
import test_hoja_y_pdf_de_cotizacion as thc
from test_pantalla_de_facturas import NOMBRE, cliente, cuerpo, pantalla, registro  # noqa: F401  (fixtures)

import cotizaciones as cz
import db.db as db
import facturas as fa
import facturas_panel as fp
import web.app as panel

HOY = "2026-10-21"


def _formulario(**kw) -> dict:
    """Lo que manda el formulario de una factura (los nombres que lee `fp.campos_del_formulario`)."""
    datos = {"cliente": "7", "para_nombre": NOMBRE, "fecha": HOY, "r-1-descripcion": "Servicio",
             "r-1-detalle": "", "r-1-cantidad": "1", "r-1-unidad": "", "r-1-precio": "10,000"}
    datos.update(kw)
    return {k: v for k, v in datos.items() if v is not None}      # `itbis=None` es «la casilla no va»


def _campos(**kw) -> dict:
    """El formulario ya leído, como lo que recibe `fp.vista_previa_de_factura`."""
    return fp.campos_del_formulario(FormData(list(_formulario(**kw).items())))


def _fragmento(campos: dict) -> str:
    """El HTML que pinta la ruta: la macro de la hoja con lo que devuelve la vista previa."""
    return str(panel.plantillas.env.get_template("_vista_previa_de_factura.html").module.la_hoja(
        fp.vista_previa_de_factura(campos)))


def _html_del_guion(texto: str) -> str:
    m = re.search(r'<script id="guion-vista-previa">(.*?)</script>', texto, re.S)
    assert m, "la página no trae el guion de la vista previa"
    return m.group(1)


# ── 1. La misma cuenta ──────────────────────────────────────────────────────────────────────────────

CASOS = {
    # nombre: (renglón, itbis, descuento, ncf, tipo, rnc)
    "el ejemplo D4": ({"r-1-precio": "10,000"}, "18", "", "B0100000011", "Crédito fiscal", "101010101"),
    "con descuento y sin NCF": ({"r-1-precio": "10,000"}, "18", "1,500", "", "", ""),
    "con descuento y con NCF": ({"r-1-precio": "10,000"}, "18", "1,500", "B0100000012", "Crédito fiscal", ""),
    "sin NCF y sin ITBIS": ({"r-1-precio": "10,000"}, None, "", "", "", ""),
    "dos renglones": ({"r-1-cantidad": "2", "r-1-precio": "5,000", "r-2-descripcion": "Otro",
                       "r-2-cantidad": "1.5", "r-2-unidad": "horas", "r-2-precio": "1,200.50"}, "18", "",
                      "B0100000013", "Consumo", ""),
}


def _renglones_del_caso(extra: dict) -> list[dict]:
    """Los renglones como los manda el formulario: uno, o dos si el caso escribe el segundo."""
    hechos = [{"descripcion": extra.get("r-1-descripcion", "Servicio"), "detalle": extra.get("r-1-detalle", ""),
               "cantidad": extra.get("r-1-cantidad", "1"), "unidad": extra.get("r-1-unidad", ""),
               "precio_unitario": extra.get("r-1-precio", "10,000").replace(",", "")}]
    if any(k.startswith("r-2-") for k in extra):
        hechos.append({"descripcion": extra.get("r-2-descripcion", "Servicio"),
                       "detalle": extra.get("r-2-detalle", ""), "cantidad": extra.get("r-2-cantidad", "1"),
                       "unidad": extra.get("r-2-unidad", ""),
                       "precio_unitario": extra.get("r-2-precio", "1").replace(",", "")})
    return hechos


def _crear_igual(m, caso: str) -> dict:
    """La factura guardada con los mismos datos que el formulario del caso, por la puerta real."""
    extra, itbis, descuento, ncf, tipo, rnc = CASOS[caso]
    f = tgf.correr(db.crear_borrador_de_factura(
        tgf.CASA, cliente_noco_id=1001, para_nombre=NOMBRE, renglones=_renglones_del_caso(extra),
        liga={"tipo": "sesion", "ref": f"ses-previa-{caso}"}, fecha=HOY,
        itbis_pct=itbis, descuento=descuento or None, ncf=ncf or None, ncf_tipo=tipo or None,
        rnc_cedula=rnc or None))
    return tgf.leer(f)


@pytest.mark.parametrize("caso", sorted(CASOS))
def test_la_vista_previa_es_la_misma_hoja_que_la_de_la_factura_guardada(pantalla, caso):
    """Con lo mismo escrito, la hoja de la vista previa y la de la factura que se guardaría son el MISMO
    diccionario: no hay una segunda cuenta ni un segundo escritor de los textos."""
    extra, itbis, descuento, ncf, tipo, rnc = CASOS[caso]
    f = _crear_igual(pantalla, caso)
    campos = _campos(itbis="18" if itbis else None, descuento=descuento, ncf=ncf, ncf_tipo=tipo,
                     rnc_cedula=rnc, **extra)
    vista = fp.vista_previa_de_factura(campos)
    assert vista["notas"] == []
    assert vista["hoja"] == fa.hoja_de_factura(f)


def test_el_ejemplo_d4_sale_con_sus_cifras_en_la_vista_previa(pantalla):
    """Bruto 10,000 con NCF e ITBIS (la regla de Tiziano, 21-oct-2026): total 13,882.36 y 10,000.00 a pagar."""
    f = _crear_igual(pantalla, "el ejemplo D4")
    assert fa.renglon_de_la_hoja(f["renglones"][0])["monto"] == "RD$10,000"
    html = _fragmento(_campos(itbis="18", ncf="B0100000011", ncf_tipo="Crédito fiscal"))
    for cifra in ("RD$13,882.36", "RD$1,764.71", "RD$2,117.65", "RD$10,000"):
        assert cifra in html, cifra
    assert "Impuesto a retener (ISR 15%)" in html and "Total a pagar" in html


def test_con_descuento_y_sin_ncf_la_vista_previa_da_el_neto_mas_el_itbis(pantalla):
    f = _crear_igual(pantalla, "con descuento y sin NCF")
    html = _fragmento(_campos(itbis="18", descuento="1,500"))
    # 10,000 − 1,500 de descuento; el ITBIS es el 18 % del neto (8,500 → 1,530) y el total 10,030
    assert "RD$10,000" in html and "- RD$1,500" in html
    assert "RD$1,530" in html and "RD$10,030" in html
    assert "Total a pagar" not in html                       # sin NCF no hay retenciones
    assert fp.vista_previa_de_factura(_campos(itbis="18", descuento="1,500"))["hoja"] == fa.hoja_de_factura(f)


# ── 2. No guarda ───────────────────────────────────────────────────────────────────────────────────

def test_la_vista_previa_no_escribe_ni_una_fila(pantalla):
    """La ruta de la vista previa es un POST que LEE: la foto de todo lo que la puerta escribe es la misma antes y
    después, tanto con lo que vale como con lo que no."""
    antes = tgf.foto(pantalla)
    c = cliente()
    for datos in (_formulario(), _formulario(**{"r-1-cantidad": ""}),
                  _formulario(**{"r-1-descripcion": ""}), {"cliente": "7"}):
        r = c.post("/facturas/vista-previa", data=datos, follow_redirects=False)
        assert r.status_code == 200, r.text
    assert tgf.foto(pantalla) == antes


def test_la_ruta_de_la_vista_previa_no_llama_a_ningun_escritor_de_la_puerta():
    """La lista de escritores sale del código de `db` (las que piden sesión de la casa, igual que en
    `test_pantalla_de_facturas._escritores_de_la_puerta`), no de una lista tecleada: si mañana la vista previa
    llama a una que escribe, esta prueba se pone roja aunque la foto de la base no lo note."""
    import ast
    import inspect

    escritores = {nombre for nombre, f in inspect.getmembers(db, inspect.iscoroutinefunction)
                  if f.__module__ == db.__name__ and not nombre.startswith("_")
                  and "_pedir_sesion_de_la_casa(" in inspect.getsource(f)}
    assert {"crear_borrador_de_factura", "corregir_borrador_de_factura"} <= escritores
    funciones = {n.name: n for n in ast.parse(open(panel.__file__, encoding="utf-8").read()).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    llamadas = {c.func.attr for c in ast.walk(funciones["vista_previa_de_una_factura"])
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "db"}
    assert not (llamadas & escritores), llamadas & escritores


def test_sin_sesion_de_la_casa_la_vista_previa_es_401_y_no_devuelve_la_hoja(pantalla):
    for sesion in ("ver", "ajena", "falsa"):
        r = cliente(sesion).post("/facturas/vista-previa", data=_formulario(), follow_redirects=False)
        assert r.status_code == 401, (sesion, r.status_code)
        assert "BORRADOR" not in r.text


# ── 3. Dice BORRADOR ──────────────────────────────────────────────────────────────────────────────

def test_la_vista_previa_dice_borrador_y_no_tiene_numero(pantalla):
    html = _fragmento(_campos())
    assert "BORRADOR" in html and 'class="marca-f"' in html and "FAC-" not in html


# ── 4. Con datos a medias ─────────────────────────────────────────────────────────────────────────

MEDIOS = {
    "cantidad vacía": ({"r-1-cantidad": ""}, "Renglón 1"),
    "precio que no se entiende": ({"r-1-precio": "abc"}, "Renglón 1"),
    "precio con tres decimales": ({"r-1-precio": "10.005"}, "Renglón 1"),
    "sin descripción": ({"r-1-descripcion": ""}, "Renglón 1"),
    "descuento que no se entiende": ({"descuento": "mucho"}, "El descuento"),
    "descuento mayor que el subtotal": ({"descuento": "20,000"}, "El descuento"),
    "NCF sin su tipo": ({"ncf": "B0100000099"}, "El NCF"),
    "tipo de NCF sin NCF": ({"ncf_tipo": "Consumo"}, "El NCF"),
    "fecha que no se entiende": ({"fecha": "2026-13-45"}, "La fecha"),
    "RNC con ocho cifras": ({"rnc_cedula": "10101010"}, "El RNC o la cédula"),
    "sin nombre": ({"para_nombre": ""}, "Falta a nombre"),
}


@pytest.mark.parametrize("nombre", sorted(MEDIOS))
def test_con_datos_a_medias_lo_dice_suave_y_no_inventa_cifras(pantalla, nombre):
    cambios, donde = MEDIOS[nombre]
    r = cliente().post("/facturas/vista-previa", data=_formulario(**cambios), follow_redirects=False)
    assert r.status_code == 200
    assert 'data-vista-previa-nota' in r.text and donde in r.text, r.text
    assert 'class="tot-f"' not in r.text, "pintó totales con algo que todavía no vale"
    assert "BORRADOR" in r.text


def test_sin_ningun_renglon_lo_dice_y_no_hay_totales(pantalla):
    r = cliente().post("/facturas/vista-previa", data={"cliente": "7", "para_nombre": NOMBRE, "fecha": HOY},
                       follow_redirects=False)
    assert r.status_code == 200
    assert "Todavía no hay ningún renglón." in r.text
    assert 'class="tot-f"' not in r.text and "RD$" not in r.text


def test_lo_que_se_escribe_sale_escapado(pantalla):
    malo = "<script>alert(1)</script>"
    r = cliente().post("/facturas/vista-previa", data=_formulario(para_nombre=malo, **{"r-1-descripcion": malo}),
                       follow_redirects=False)
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


# ── 5. Las dos pantallas ──────────────────────────────────────────────────────────────────────────

def test_la_pantalla_de_escribir_trae_la_vista_previa_al_lado_del_formulario(pantalla):
    html = cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text
    assert 'data-factura-nueva' in html and 'data-hoja' in html and 'id="guion-vista-previa"' in html
    assert html.index('data-factura-nueva') < html.index('data-hoja')       # la hoja va después del formulario
    assert 'class="dos"' in html
    assert "BORRADOR" in html and "RD$9,000" in html                        # el renglón de la sesión, ya sumado


def test_la_pantalla_de_corregir_trae_la_vista_previa_al_lado_y_una_sola_hoja(pantalla):
    b = tgf.crear()
    html = cliente().get(f"/facturas/{b['id']}?corregir=1").text
    assert 'data-corregir' in html and 'data-hoja' in html and 'id="guion-vista-previa"' in html
    assert html.index('data-corregir') < html.index('data-hoja')
    # una sola hoja con el atributo (la del formulario): la de la página de abajo no se pinta mientras se corrige
    assert len(re.findall(r'<div class="hoja-caja" data-hoja>', html)) == 1
    assert "BORRADOR" in html


def test_sin_el_formulario_abierto_no_se_pinta_el_guion(pantalla):
    b = tgf.crear()
    html = cliente().get(f"/facturas/{b['id']}").text
    assert 'data-hoja' in html and 'id="guion-vista-previa"' not in html
    html = cliente().get("/facturas/nueva?cliente=7&ref=11").text      # el paso de escoger, no el de escribir
    assert 'data-hoja' not in html and 'id="guion-vista-previa"' not in html


# ── 6. El guion, en JavaScriptCore ───────────────────────────────────────────────────────────────

_ARNES = r"""
var pedidos = [];
var cuerpos = [];
var oyentesDelFormulario = {};
var FORM = {
  enviados: 0,
  addEventListener: function (tipo, f) { oyentesDelFormulario[tipo] = f; },
  requestSubmit: function () { this.enviados++; },
  submit: function () { this.enviados++; }
};
var CAJA = {innerHTML: "HOJA-VIEJA"};
var document = {querySelector: function (s) {
  if (s === "form[data-factura-nueva], form[data-corregir]") return FORM;
  if (s === "[data-hoja]") return CAJA;
  return null;
}};
function FormData(form) { this.form = form; cuerpos.push(form); }
/* Promesas de mentira que resuelven en el acto: osascript termina antes de correr las microtareas. */
function resolver(valor, cb) {
  if (valor && typeof valor.then === "function") { valor.then(function (x) { resolver(x, cb); }); }
  else cb(valor);
}
function Promesa(valor) {
  this.then = function (cb) {
    var salida = null;
    resolver(valor, function (x) { salida = new Promesa(cb(x)); });
    return salida;
  };
  this.catch = function () { return this; };
}
function Rechazo() { this.then = function () { return this; }; this.catch = function (cb) { cb(); return this; }; }
var GUION_DE_FETCH = null;
function fetch(url, opciones) { pedidos.push({url: url, opciones: opciones}); return GUION_DE_FETCH(); }
function responde(ok, texto) {
  return new Promesa({ok: ok, text: function () { return new Promesa(texto); }});
}
var temporizadores = {}, siguienteT = 0;
function setTimeout(f, ms) { siguienteT++; temporizadores[siguienteT] = f; return siguienteT; }
function clearTimeout(id) { delete temporizadores[id]; }
function correrTemporizadores() {
  var pendientes = [];
  for (var id in temporizadores) pendientes.push(temporizadores[id]);
  temporizadores = {};
  pendientes.forEach(function (f) { f(); });
}
function escribir() { oyentesDelFormulario.input({target: null}); }
function cambiar() { oyentesDelFormulario.change({target: null}); }
"""

hay_osascript = pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="no hay osascript (JavaScriptCore de macOS): el guion de la vista previa no se prueba aquí")


def _correr_en_jxa(guion: str, escenario: str) -> dict:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


@hay_osascript
def test_el_guion_pide_la_vista_previa_al_escribir_y_la_pinta(pantalla):
    guion = _html_del_guion(cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text)
    r = _correr_en_jxa(guion, 'GUION_DE_FETCH = function () { return responde(true, "HOJA-NUEVA"); };\n'
                              'escribir();\n'
                              'var antes = Object.keys(temporizadores).length;\n'
                              'correrTemporizadores();\n'
                              'JSON.stringify({antes: antes, pedidos: pedidos.length, url: pedidos[0].url,\n'
                              '  metodo: pedidos[0].opciones.method, cuerpos: cuerpos.length,\n'
                              '  caja: CAJA.innerHTML, enviados: FORM.enviados});')
    assert r == {"antes": 1, "pedidos": 1, "url": "/facturas/vista-previa", "metodo": "POST", "cuerpos": 1,
                 "caja": "HOJA-NUEVA", "enviados": 0}


@hay_osascript
def test_el_guion_espera_a_que_pares_de_escribir_y_tambien_atiende_el_cambio(pantalla):
    guion = _html_del_guion(cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text)
    r = _correr_en_jxa(guion, 'var n = 0; GUION_DE_FETCH = function () { n++; return responde(true, "H" + n); };\n'
                              'escribir(); escribir(); escribir();\n'
                              'correrTemporizadores();\n'
                              'var trasEscribir = pedidos.length;\n'
                              'cambiar(); correrTemporizadores();\n'
                              'JSON.stringify({trasEscribir: trasEscribir, total: pedidos.length,\n'
                              '  caja: CAJA.innerHTML, enviados: FORM.enviados});')
    assert r == {"trasEscribir": 1, "total": 2, "caja": "H2", "enviados": 0}


@hay_osascript
@pytest.mark.parametrize("fallo", ["no_ok", "revienta"])
def test_el_guion_no_pinta_ni_revienta_si_el_servidor_no_contesta(pantalla, fallo):
    guion = _html_del_guion(cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text)
    cual = ('GUION_DE_FETCH = function () { return responde(false, ""); };' if fallo == "no_ok"
            else 'GUION_DE_FETCH = function () { return new Rechazo(); };')
    r = _correr_en_jxa(guion, cual + '\nescribir();\ncorrerTemporizadores();\n'
                                    'JSON.stringify({caja: CAJA.innerHTML, pedidos: pedidos.length,\n'
                                    '  enviados: FORM.enviados});')
    assert r == {"caja": "HOJA-VIEJA", "pedidos": 1, "enviados": 0}


@hay_osascript
def test_el_guion_nunca_manda_el_formulario_y_solo_habla_con_la_vista_previa(pantalla):
    guion = _html_del_guion(cliente().get("/facturas/nueva?cliente=7&ref=11&paso=escribir").text)
    r = _correr_en_jxa(guion, 'GUION_DE_FETCH = function () { return responde(true, "H"); };\n'
                              'escribir(); correrTemporizadores(); escribir(); correrTemporizadores();\n'
                              'JSON.stringify({enviados: FORM.enviados, urls: pedidos.map(function (p) { return p.url; })});')
    assert r == {"enviados": 0, "urls": ["/facturas/vista-previa", "/facturas/vista-previa"]}


# ── 7. La cotización sigue igual ────────────────────────────────────────────────────────────────

# Medidas el 21-oct-2026, ANTES de esta pieza, con `thc._cot()` y la plantilla real (ver `medir_cotizacion.py` del
# scratchpad del trabajo): si cambian, la pieza tocó la cotización sin decir.
HUELLA_DE_LA_VISTA_PREVIA_DE_LA_COTIZACION = "348cb10aeb40d12d4ff637918ad49db13124f1dc624e41d290b5af3c192645b4"
HUELLA_DEL_TEXTO_DEL_PDF_DE_LA_COTIZACION = "e7a9cfb462f2d62f7d5875c916e48f060677e20ebf8db222c9661e8802a11858"


def test_la_vista_previa_de_la_cotizacion_sigue_igual():
    hoja = cz.hoja_de_cotizacion(thc._cot())
    html = str(panel.plantillas.env.get_template("_hoja_de_cotizacion.html").module.hoja_de_cotizacion(hoja))
    assert hashlib.sha256(html.encode("utf-8")).hexdigest() == HUELLA_DE_LA_VISTA_PREVIA_DE_LA_COTIZACION


def test_el_pdf_de_la_cotizacion_sigue_igual():
    hoja = cz.hoja_de_cotizacion(thc._cot())
    pdf = cz.pdf_de_cotizacion(hoja, borrador=True)
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert hashlib.sha256(re.sub(r"\s+", "", texto).encode("utf-8")).hexdigest() == \
        HUELLA_DEL_TEXTO_DEL_PDF_DE_LA_COTIZACION
