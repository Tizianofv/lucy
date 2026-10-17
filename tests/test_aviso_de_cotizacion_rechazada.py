"""Un guardado de cotización RECHAZADO nunca es silencioso (17-oct-2026).

Lo que pasó: dos veces alguien armó un borrador con la IA, dejó los precios vacíos (así lo manda el diseño) y tocó «Guardar y
descargar PDF». El servidor rechazó con razón (`cotizacion_total_vacio`) y la persona no vio por qué: «no funcionó». Qué se vigila:

  1. El aviso de un rechazo sale DENTRO de la ventana, en el pie, junto a los botones de guardar; no arriba. El pie se queda
     a la vista (CSS `position:sticky`; que de verdad quede dentro de lo visible se midió en Chrome, ver la FRONTERA).
  2. Las casillas de precio que la puerta de `db` rechazaría salen marcadas (`aria-invalid="true"`): la misma función que decide
     al guardar, sin una regla escrita aparte.
  3. Lo escrito no se pierde (también el borrador de la IA): el servidor lo guarda unos minutos en memoria
     (`web/cotizacion_rechazada.py`) y la dirección lleva SOLO una contraseña; ningún dato escrito viaja en la dirección ni en el
     registro. Solo la misma sesión, el mismo proyecto y la misma cotización lo reciben; un guardado corregido se acepta.
  4. Si ya se sabe antes de mandar (un renglón con algo y el precio vacío), el guion de la ventana avisa SIN mandar. Eso no
     reemplaza al servidor: un POST que no pasó por la página se rechaza igual.
  5. Todos los motivos de rechazo de ese POST, sacados de LO REAL (los `raise CotizacionNoVale(...)` de `db/db.py` por AST y las
     clases `Cotizacion*` de `db`), salen en el pie y ninguno arriba.

FRONTERA, dicha una vez: el navegador de mentira de JavaScriptCore (4) no pinta ni sabe de `submit` de verdad: el orden con el guion de
«sin saltar» (que respeta `defaultPrevented`) y que el aviso quede dentro de lo visible a 412 y a 1300 px se midió en Chrome con el
servidor local, no aquí. Los `raise` con una clave armada en una variable (no un texto ni un f-string de `{campo}`) no los ve el AST:
la prueba se pone roja si aparece uno, no lo salta.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_cotizaciones_en_la_pagina as t
from test_cotizaciones_en_la_pagina import m, _casa, _filas, _formulario, _pagina, _post_de_la_pagina  # noqa: F401  (m: fixture)
from test_pagina_proyectos import gente, mundo  # noqa: F401
from _navegador import Navegador
import config
import db.db as db
import web.app as panel
import web.auth as auth
import web.cotizacion_rechazada as rechazada

hay_osascript = pytest.mark.skipif(shutil.which("osascript") is None, reason="no hay osascript (JavaScriptCore de macOS)")

ESCRITO = {"opcion": "Opción de la persona ñandú", "descripcion": "Grabación de voces", "detalle": "Detalle largo con tilde: canción",
           "cliente": "Cliente corregido a mano"}


# ── Lo que la ventana pinta, sacado del HTML ──────────────────────────────────────────────────────────

class _Ventana(HTMLParser):
    """De la página: el texto del aviso del PIE (`[data-cz-aviso-pie]` dentro de `.ventana-pie`), cuántos avisos hay en la ventana
    FUERA del pie, las casillas (nombre, valor, `aria-invalid`, marcada) y el texto de los `<textarea>`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pie = ""
        self.pie_dentro_del_pie = False
        self.pie_vacio = None
        self.avisos_arriba = 0
        self.casillas: dict[str, dict] = {}
        self._div = []                  # pila de divs: ¿es el aviso del pie?
        self._pie_prof = 0
        self._ventana = False
        self._textarea = None
        self._plantilla = 0
        self._pie_de_ventana = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        clases = a.get("class", "").split()
        if tag == "template":
            self._plantilla += 1
        if tag == "div" and "cz-ventana" in clases:
            self._ventana = True
        if tag == "div":
            es_pie = "data-cz-aviso-pie" in a
            self._div.append(es_pie)
            if es_pie:
                self._pie_prof += 1
                self.pie_vacio = "cz-aviso-vacio" in clases
                self.pie_dentro_del_pie = self._pie_de_ventana > 0
            if "ventana-pie" in clases:
                self._pie_de_ventana = len(self._div)
        if tag == "p" and "aviso" in clases and self._ventana and not self._pie_prof and not self._plantilla:
            self.avisos_arriba += 1
        if self._plantilla:
            return
        if tag == "input" and a.get("name"):
            self.casillas[a["name"]] = {"valor": a.get("value", ""), "invalida": a.get("aria-invalid"), "marcada": "checked" in a}
        if tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.casillas[a["name"]] = {"valor": "", "invalida": None, "marcada": False}
        if tag == "select" and a.get("name"):
            self._select = a["name"]
            self.casillas[a["name"]] = {"valor": "", "invalida": None, "marcada": False}
        if tag == "option" and "selected" in a and getattr(self, "_select", None):
            self.casillas[self._select]["valor"] = a.get("value", "")

    def handle_data(self, data):
        if self._textarea:
            self.casillas[self._textarea]["valor"] += data
        if self._pie_prof:
            self.pie += data

    def handle_endtag(self, tag):
        if tag == "template":
            self._plantilla -= 1
        if tag == "textarea":
            self._textarea = None
        if tag == "select":
            self._select = None
        if tag == "div" and self._div:
            if self._div.pop():
                self._pie_prof -= 1
            if self._pie_de_ventana and len(self._div) < self._pie_de_ventana:
                self._pie_de_ventana = 0


def _ventana(html: str) -> _Ventana:
    v = _Ventana()
    v.feed(html)
    return v


def _con_lo_escrito(datos: dict, vacios=("op-0-r-0-total",)) -> dict:
    """El POST de la página con lo que la persona escribió; los totales de `vacios` quedan vacíos."""
    datos = dict(datos)
    datos["op-0-nombre"] = ESCRITO["opcion"]
    datos["op-0-r-0-descripcion"] = ESCRITO["descripcion"]
    datos["op-0-r-0-detalle"] = ESCRITO["detalle"]
    datos["para-nombre"] = ESCRITO["cliente"]
    datos["validez"] = "30"
    datos.pop("cond-deposito", None)                         # desmarcó una condición
    for k in vacios:
        datos[k] = ""
    return datos


def _rechazo(m, vacios=("op-0-r-0-total",), cotizacion="nueva", accion="enviar"):
    """POST real de una ventana con el precio vacío; devuelve (navegador, respuesta del POST)."""
    c = _casa()
    ruta = "/proyectos/1/cotizaciones" + ("" if cotizacion == "nueva" else f"/{cotizacion}")
    consulta = {"cotizacion": cotizacion} if cotizacion == "nueva" else {"cotizacion": cotizacion, "corregir": 1}
    datos = _con_lo_escrito(_post_de_la_pagina(_formulario(_pagina(c, p=1, **consulta)), accion), vacios)
    return c, c.post(ruta, data=datos, follow_redirects=False)


# ══════════════════════════════════════════════════════════════════
# 1, 2, 3. El rechazo de verdad: aviso en el pie, casillas marcadas, lo escrito sigue
# ══════════════════════════════════════════════════════════════════

def test_un_precio_vacio_rechazado_sale_en_el_pie_con_la_casilla_marcada_y_lo_escrito_sigue(m, caplog):
    caplog.set_level("INFO")
    c, r = _rechazo(m)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert "error=cotizacion_total_vacio" in destino and re.search(r"&rechazo=[\w-]{10,}#cotizaciones$", destino), destino
    # lo escrito NO viaja en la dirección ni queda en el registro
    for texto in ESCRITO.values():
        assert texto not in destino and texto.encode("latin-1").hex() not in destino
    assert not [x for x in caplog.records if any(v in x.getMessage() for v in ESCRITO.values())]
    html = c.get(destino.split("#")[0]).text
    v = _ventana(html)
    # (1) el aviso está en el pie, dentro del formulario, y no hay otro arriba
    assert v.pie_dentro_del_pie and v.pie_vacio is False
    assert "Cada renglón necesita un total mayor que cero" in v.pie and "NO se guardó nada" in v.pie
    assert v.avisos_arriba == 0
    # (2) la casilla vacía, marcada; las demás no
    marcadas = sorted(k for k, c_ in v.casillas.items() if c_["invalida"] == "true")
    assert marcadas == ["op-0-r-0-total"], marcadas
    # (3) lo escrito, tal cual (nombres con tilde, el detalle, el cliente cambiado, la validez, la condición desmarcada)
    assert v.casillas["op-0-nombre"]["valor"] == ESCRITO["opcion"]
    assert v.casillas["op-0-r-0-descripcion"]["valor"] == ESCRITO["descripcion"]
    assert v.casillas["op-0-r-0-detalle"]["valor"] == ESCRITO["detalle"]
    assert v.casillas["para-nombre"]["valor"] == ESCRITO["cliente"]
    assert v.casillas["validez"]["valor"] == "30"
    assert v.casillas["cond-deposito"]["marcada"] is False and v.casillas["cond-validez"]["marcada"] is True
    # y no se escribió nada ni se gastó el número
    assert _filas(m) == [] and _filas(m, "SELECT * FROM serie_cotizaciones") == []


def test_el_pie_trae_el_aviso_antes_de_los_botones_de_guardar(m):
    """Junto a los botones: en el HTML, el aviso va dentro del mismo `.ventana-pie`, antes de «Guardar y descargar PDF»."""
    c, r = _rechazo(m)
    html = c.get(r.headers["location"].split("#")[0]).text
    pie = html[html.index('<div class="ventana-pie">'):]
    assert pie.index("data-cz-aviso-pie") < pie.index("NO se guardó nada") < pie.index('name="accion" value="enviar"')


def test_varios_precios_vacios_se_marcan_todos_y_el_que_vale_no(m):
    c = _casa()
    f = _formulario(_pagina(c, p=1, cotizacion="nueva"))
    datos = _con_lo_escrito(_post_de_la_pagina(f, "enviar"))
    totales = sorted(k for k in datos if k.endswith("-total") and k.startswith("op-0") or k.startswith("op-1") and k.endswith("-total"))
    assert len(totales) >= 2
    datos[totales[0]] = ""                                    # vacío
    datos[totales[1]] = "abc"                                 # escrito pero que no se entiende
    for k in totales[2:]:
        datos[k] = "1,500"                                    # bueno
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    v = _ventana(c.get(r.headers["location"].split("#")[0]).text)
    marcadas = sorted(k for k, x in v.casillas.items() if x["invalida"] == "true")
    esperadas = sorted(k for k in totales[:2])
    assert marcadas == esperadas, (marcadas, esperadas)
    assert v.casillas[totales[1]]["valor"] == "abc"          # el precio mal escrito vuelve como se escribió
    # la marca sale de la misma puerta que decide: ninguna casilla marcada tiene un total que `db` acepte, ninguna sin marca uno que no
    for k, x in v.casillas.items():
        if k.endswith("-total") and re.match(r"op-[01]-r-", k):
            try:
                db.total_de_cotizacion_que_vale(x["valor"])
                acepta = True
            except db.CotizacionNoVale:
                acepta = False
            if x["valor"].strip() or acepta is False and x["invalida"] == "true":
                assert (x["invalida"] == "true") == (not acepta), (k, x)


def test_lo_escrito_y_rechazado_se_puede_corregir_y_guardar_desde_la_ventana_que_volvio(m):
    """De punta a punta: se rechaza, vuelve la ventana con lo escrito, se llena el precio que faltaba y se guarda."""
    c, r = _rechazo(m)
    pagina = c.get(r.headers["location"].split("#")[0]).text
    f = _formulario(pagina)
    datos = _post_de_la_pagina(f, "borrador", texto=lambda n: "")
    datos["validez"] = _ventana(pagina).casillas["validez"]["valor"]       # lo que el navegador manda del <select> pintado
    datos["op-0-r-0-total"] = "62,500"
    suyos = ("op-0-nombre", "op-0-r-0-descripcion", "op-0-r-0-detalle", "op-0-r-0-total")
    for k in [k for k in datos if k.startswith("op-") and k not in suyos]:
        datos[k] = ""                                          # el resto de lo que dibuja la página (y la prueba llenó) se vacía
    ok = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert "hecho=cotizacion_borrador" in ok.headers["location"], ok.headers["location"]
    filas = _filas(m)
    assert len(filas) == 1 and filas[0]["para_nombre"] == ESCRITO["cliente"] and filas[0]["validez_dias"] == 30
    contenido = json.loads(filas[0]["contenido"]) if isinstance(filas[0]["contenido"], str) else filas[0]["contenido"]
    assert contenido["opciones"][0]["nombre"] == ESCRITO["opcion"]
    assert contenido["opciones"][0]["renglones"][0]["total"] == "62500.00"
    assert not any(x["clave"] == "deposito" and x["marcada"] for x in contenido["condiciones"])


def test_un_rechazo_de_una_cotizacion_que_ya_existe_vuelve_con_lo_escrito_y_el_candado_que_la_persona_leyo(m):
    borrador = t._guardar("borrador", contenido=t._contenido(1))
    pagina = _pagina(_casa(), p=1, cotizacion=borrador["id"])
    candado = _formulario(pagina)
    candado = next(v for _, n, _, v, _ in candado.casillas if n == "cambiado_en")
    c, r = _rechazo(m, cotizacion=borrador["id"], accion="borrador")
    assert f"cotizacion={borrador['id']}" in r.headers["location"] and "error=cotizacion_total_vacio" in r.headers["location"]
    v = _ventana(c.get(r.headers["location"].split("#")[0]).text)
    assert v.pie_dentro_del_pie and "NO se guardó nada" in v.pie
    assert v.casillas["op-0-nombre"]["valor"] == ESCRITO["opcion"]                  # lo escrito, no lo guardado
    assert v.casillas["cambiado_en"]["valor"] == candado                            # el candado que ella leyó
    assert [k for k, x in v.casillas.items() if x["invalida"] == "true"] == ["op-0-r-0-total"]
    assert _filas(m)[0]["cambiado_en"] is not None


def test_si_la_cotizacion_cambio_la_ventana_abre_como_quedo_lo_guardado_y_sin_contrasena(m):
    """Única excepción, dicha: con el candado viejo, lo escrito no se pone sobre lo guardado (pisaría el cambio ajeno)."""
    borrador = t._guardar("borrador", contenido=t._contenido(1))
    c = _casa()
    datos = _post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion=borrador["id"])), "borrador")
    otro = _casa().post(f"/proyectos/1/cotizaciones/{borrador['id']}", data=datos, follow_redirects=False)
    assert "hecho=cotizacion_borrador" in otro.headers["location"]                  # la otra pestaña guardó
    datos["para-nombre"] = "Lo de la segunda pestaña"
    r = c.post(f"/proyectos/1/cotizaciones/{borrador['id']}", data=datos, follow_redirects=False)
    assert "error=cotizacion_cambio" in r.headers["location"] and "rechazo=" not in r.headers["location"]
    v = _ventana(c.get(r.headers["location"].split("#")[0]).text)
    assert v.pie_dentro_del_pie and "La cotización cambió" in v.pie and v.avisos_arriba == 0
    assert v.casillas["para-nombre"]["valor"] != "Lo de la segunda pestaña"


# ── La contraseña de lo escrito: de quién es, de qué proyecto y de qué cotización ──────────────────────────────

def test_la_contrasena_solo_sirve_a_su_sesion_su_proyecto_su_cotizacion_y_mientras_no_venza(m, gente, monkeypatch):
    c, r = _rechazo(m)
    destino = r.headers["location"].split("#")[0]
    contrasena = re.search(r"rechazo=([\w-]+)", destino).group(1)
    assert ESCRITO["cliente"] in c.get(destino).text                                # la suya: sí
    # otra sesión (la otra persona de la casa), mismo proyecto
    otra = Navegador(panel.app, base_url="https://testserver")
    otra.cookies.set(panel.COOKIE, auth.crear_token(gente.rosi, auth.VIDA_SESION))
    assert ESCRITO["cliente"] not in otra.get(destino).text
    # otro proyecto
    assert ESCRITO["cliente"] not in c.get(destino.replace("p=1", "p=3")).text
    # otra cotización: la contraseña de la nueva no abre la 1 (que ni existe), ni una contraseña inventada
    assert rechazada.leer(config.CHAT_ID_DUENO, 1, 7, contrasena) is None
    for falsa in ("", "x", contrasena[:-1] + ("A" if contrasena[-1] != "A" else "B"), None, 5):
        assert rechazada.leer(config.CHAT_ID_DUENO, 1, None, falsa) is None
    assert rechazada.leer(config.CHAT_ID_DUENO, 1, None, contrasena) is not None
    # vence
    real = rechazada.time.monotonic
    monkeypatch.setattr(rechazada.time, "monotonic", lambda: real() + rechazada.VIDA + 1)
    assert rechazada.leer(config.CHAT_ID_DUENO, 1, None, contrasena) is None
    assert ESCRITO["cliente"] not in c.get(destino).text


def test_la_memoria_tiene_tope_y_no_guarda_lo_enorme():
    rechazada._guardado.clear()
    campos = {"contenido": {"opciones": [], "condiciones": []}, "validez": 15}
    claves = [rechazada.guardar(1, 1, None, campos) for _ in range(rechazada.MAXIMO + 5)]
    assert len(rechazada._guardado) == rechazada.MAXIMO
    assert rechazada.leer(1, 1, None, claves[0]) is None and rechazada.leer(1, 1, None, claves[-1]) is not None
    assert rechazada.guardar(1, 1, None, {"x": "a" * (rechazada.LARGO_MAXIMO + 1)}) is None
    rechazada._guardado.clear()


# ══════════════════════════════════════════════════════════════════
# 4. El servidor sigue siendo la puerta aunque la página no avise
# ══════════════════════════════════════════════════════════════════

def test_un_post_que_no_paso_por_la_pagina_con_el_precio_vacio_se_rechaza_igual_y_no_escribe(m):
    """Sin el guion (otro cliente, un navegador sin JavaScript, un curl): el servidor decide solo."""
    c = _casa()
    datos = _con_lo_escrito(_post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion="nueva")), "enviar"), vacios=(
        "op-0-r-0-total", "op-1-r-0-total"))
    for accion in ("borrador", "enviar"):
        r = c.post("/proyectos/1/cotizaciones", data=dict(datos, accion=accion), follow_redirects=False)
        assert r.status_code == 303 and "error=cotizacion_total_vacio" in r.headers["location"], accion
    assert _filas(m) == [] and _filas(m, "SELECT * FROM serie_cotizaciones") == []


# ══════════════════════════════════════════════════════════════════
# 5. Todos los motivos de rechazo de ese POST, sacados de lo real
# ══════════════════════════════════════════════════════════════════

def _claves_de_no_vale_en_db() -> set[str]:
    """Las claves de los `raise CotizacionNoVale(...)` de `db/db.py`, por AST. Un f-string `f"{campo}_vacio"` se abre con los
    `campo` que de verdad se le pasan a `_texto_de_cotizacion` (su segundo argumento, un texto). Una clave que no sea ni un texto
    ni de esa forma pone roja la prueba."""
    arbol = ast.parse(open("db/db.py", encoding="utf-8").read())
    campos = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_texto_de_cotizacion":
            campo = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "campo"), None)
            assert isinstance(campo, ast.Constant) and isinstance(campo.value, str), ast.dump(n)[:200]
            campos.add(campo.value)
    claves = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and getattr(n.exc.func, "id", None) == "CotizacionNoVale":
            arg = n.exc.args[0]
            if isinstance(arg, ast.Constant):
                claves.add(arg.value)
            else:
                assert isinstance(arg, ast.JoinedStr) and len(arg.values) == 2 and isinstance(arg.values[0], ast.FormattedValue) \
                    and ast.unparse(arg.values[0].value) == "campo" and isinstance(arg.values[1], ast.Constant), ast.unparse(arg)
                claves |= {f"{c}{arg.values[1].value}" for c in campos}
    return claves


def _errores_de_rechazo_de_lo_real() -> list[str]:
    """Cada `?error=` que `_error_de_cotizacion` puede dar: una por clave de `CotizacionNoVale` y una por cada clase `Cotizacion*` de
    `db` que sea una excepción (menos la de sesión, que no redirige: da 401), más la de «la base falló»."""
    errores = {panel._error_de_cotizacion(db.CotizacionNoVale(c, "x")) for c in _claves_de_no_vale_en_db()}
    clases = [c for n, c in inspect.getmembers(db, inspect.isclass) if n.startswith("Cotizacion") and issubclass(c, Exception)
              and n not in ("CotizacionNoVale", "CotizacionSinSesion")]
    for c in clases:
        errores.add(panel._error_de_cotizacion(c("x")))
    errores.add(panel._error_de_cotizacion(RuntimeError("la base falló")))
    return sorted(errores)


def test_la_lista_de_motivos_sale_de_lo_real_y_no_esta_vacia():
    claves = _claves_de_no_vale_en_db()
    assert len(claves) >= 25 and {"total_vacio", "sin_opciones", "candado", "descripcion_vacio", "nombre_largo"} <= claves, sorted(claves)
    errores = _errores_de_rechazo_de_lo_real()
    assert {"cotizacion_cambio", "cotizacion_no_esta", "cotizacion_base", "cotizacion_sin_tabla",
            "cotizacion_enviada_no_vuelve", "cotizacion_total_vacio"} <= set(errores) and len(errores) >= 25


def test_cada_motivo_de_rechazo_sale_en_el_pie_de_la_ventana_y_ninguno_arriba(m):
    c = _casa()
    for error in _errores_de_rechazo_de_lo_real():
        v = _ventana(_pagina(c, con_recibo=True, p=1, cotizacion="nueva", error=error))
        assert v.pie_dentro_del_pie and v.pie_vacio is False, error
        assert "NO se guardó" in v.pie or "no se pudo confirmar" in v.pie, (error, v.pie)
        assert v.avisos_arriba == 0, error
    # y con una ventana que ya existe (borrador) igual
    borrador = t._guardar("borrador", contenido=t._contenido(1))
    for error in ("cotizacion_cambio", "cotizacion_total_vacio"):
        v = _ventana(_pagina(c, con_recibo=True, p=1, cotizacion=borrador["id"], corregir=1, error=error))
        assert v.pie_dentro_del_pie and v.pie_vacio is False and v.avisos_arriba == 0, error


def test_sin_rechazo_el_pie_no_trae_aviso_y_la_lista_y_la_hoja_lo_dicen_arriba(m):
    c = _casa()
    v = _ventana(_pagina(c, p=1, cotizacion="nueva"))
    assert v.pie_vacio is True and v.pie.strip() == ""
    enviada = t._guardar("enviar")
    arriba = _pagina(c, con_recibo=True, p=1, cotizacion=enviada["id"], error="cotizacion_enviada_no_vuelve")      # la hoja: sin botones de guardar
    assert "NO se guardó nada" in arriba and 'class="cz-aviso-pie' not in arriba
    lista = _pagina(c, con_recibo=True, p=1, error="cotizacion_sin_tabla")
    assert "falta actualizar la base de datos" in lista and 'class="cz-aviso-pie' not in lista


# ── El estilo: el pie se queda a la vista (que quede dentro de lo visible se midió en Chrome) ─────────────────

def test_el_pie_de_la_ventana_se_queda_pegado_abajo_y_la_casilla_marcada_se_ve():
    css = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    assert re.search(r"\.cz-ventana \.ventana-pie\{[^}]*position:sticky;bottom:0", css)
    assert re.search(r"\.cz-ventana input\[aria-invalid=\"true\"\]\{[^}]*border:2px solid var\(--vencida\)", css)
    assert re.search(r"\.ventana-pie \.cz-aviso-vacio\{display:none\}", css)


# ══════════════════════════════════════════════════════════════════
# 4. El guion de la ventana: avisa antes de mandar (JavaScriptCore con una ventana de mentira)
# ══════════════════════════════════════════════════════════════════

@hay_osascript
def test_la_cuenta_de_precio_vacio_del_guion_es_la_que_el_servidor_aplica():
    """`faltaElPrecio` contra la puerta: para cada combinación de vacío / en blanco / escrito en los tres campos, el guion dice
    «falta el precio» exactamente cuando el servidor NO descartaría el renglón por vacío y el total está vacío; y en esos casos, con una
    descripción buena, la puerta rechaza con `total_vacio`."""
    valores = ["", "   ", "x"]
    for d in valores:
        for det in valores:
            for tot in valores + ["1,000"]:
                esperado = not all(db._vacio_de_cotizacion(x) for x in (d, det, tot)) and db._vacio_de_cotizacion(tot)
                dicho = t._en_jxa(f"faltaElPrecio({json.dumps(d)}, {json.dumps(det)}, {json.dumps(tot)})")
                assert dicho is esperado, (d, det, tot, dicho)
                if dicho:
                    contenido = {"opciones": [{"nombre": "Op", "renglones": [{"descripcion": "Algo", "detalle": det, "total": tot}]}],
                                 "condiciones": []}
                    with pytest.raises(db.CotizacionNoVale) as e:
                        db.contenido_de_cotizacion_que_vale(contenido)
                    assert e.value.clave == "total_vacio"


_VENTANA_DE_MENTIRA = r"""
var marcas = [], enfocados = [], quitadas = [], prevenidos = [], oyentes = {};
function campo(valor) {
  return {value: valor, atributos: {}, setAttribute: function (k, v) { this.atributos[k] = v; },
          getAttribute: function (k) { return k in this.atributos ? this.atributos[k] : null; },
          removeAttribute: function (k) { delete this.atributos[k]; }, classList: {add: function () {}, remove: function () {}},
          focus: function () { enfocados.push(this); }};
}
function bloque(d, det, t) {
  var c = {"[name$='-descripcion']": campo(d), "[name$='-detalle']": campo(det), "[name$='-total']": campo(t)};
  return {campos: c, querySelector: function (s) { return c[s] || null; }};
}
var aviso = {hijos: [], firstChild: null, clases: {}, appendChild: function (n) { this.hijos.push(n); this.firstChild = n; },
             removeChild: function () { this.hijos = []; this.firstChild = null; },
             classList: {remove: function (c) { quitadas.push(c); }}};
var bloques = [];
var forma = {querySelectorAll: function (s) { return s === ".renglon-bloque" ? bloques : []; },
             querySelector: function (s) { return s === "[data-cz-aviso-pie]" ? aviso : null; },
             getAttribute: function (n) { return n === "data-cz-sin-precio" ? "AVISO-DE-LA-PAGINA" : null; }};
var document = {readyState: "complete", body: {}, documentElement: {classList: {add: function () {}}},
  addEventListener: function (t, f, captura) { (oyentes[t] = oyentes[t] || []).push({f: f, captura: !!captura}); },
  querySelectorAll: function (s) { return s === "form.cz-form" ? [] : []; }, querySelector: function () { return null; },
  createElement: function (e) { return {etiqueta: e, className: "", textContent: "", appendChild: function () {}}; },
  createTextNode: function (x) { return {texto: x}; }};
function MutationObserver() { this.observe = function () {}; }
function enviar() {
  var e = {target: {closest: function (s) { return s === "form.cz-form" ? forma : null; }}, preventDefault: function () { prevenidos.push(1); }};
  oyentes.submit.forEach(function (o) { o.f(e); });
}
"""


def _enviar_con(renglones: list[tuple], despues: str = "") -> dict:
    """Corre el guion REAL con una ventana de mentira que tiene esos renglones `(descripción, detalle, total)` y «envía» el formulario."""
    antes = "bloques = [" + ", ".join(f"bloque({json.dumps(d)}, {json.dumps(x)}, {json.dumps(tt)})" for d, x, tt in renglones) + "];\n"
    programa = (_VENTANA_DE_MENTIRA + t._guion_de_las_cotizaciones() + "\n" + antes + "enviar();\n" + despues +
                "\nJSON.stringify({prevenidos: prevenidos.length, marcados: bloques.map(function (b) { return b.campos[\"[name$='-total']\"]"
                ".atributos['aria-invalid'] || null; }), enfocado: enfocados.map(function (c) { return bloques.map(function (b) { return "
                "b.campos[\"[name$='-total']\"] === c; }).indexOf(true); }), texto: aviso.hijos.length ? aviso.hijos[0].textContent : null, "
                "clase: aviso.hijos.length ? aviso.hijos[0].className : null, quitadas: quitadas, "
                "captura: oyentes.submit.map(function (o) { return o.captura; })})")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", programa], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


@hay_osascript
def test_el_guion_no_manda_un_renglon_con_algo_y_sin_precio_y_avisa_en_el_pie_y_marca_la_casilla():
    v = _enviar_con([("Grabación", "", ""), ("Mezcla", "", "5,000"), ("", "", "")])
    assert v["prevenidos"] == 1
    assert v["marcados"] == ["true", None, None]                     # solo la que falta
    assert v["texto"] == "AVISO-DE-LA-PAGINA" and v["clase"] == "aviso" and v["quitadas"] == ["cz-aviso-vacio"]
    assert v["enfocado"] == [0]                                      # y deja el cursor en la primera que falta
    assert v["captura"] == [True]                                    # fase de captura: corre antes que «sin saltar»


@hay_osascript
def test_el_guion_deja_pasar_lo_completo_y_lo_vacio_de_sobra():
    for renglones in ([("Grabación", "", "5,000")], [("", "", ""), ("", "", "")], [("Mezcla", "d", "1"), ("", "", "")]):
        v = _enviar_con(renglones)
        assert v["prevenidos"] == 0 and set(v["marcados"]) == {None} and v["texto"] is None, renglones


@hay_osascript
def test_el_guion_solo_avisa_de_precios_vacios_y_nunca_decide_otra_cosa():
    """Un precio mal escrito («abc») NO lo detiene el guion: lo decide el servidor (formato, tope, decimales no se duplican aquí)."""
    v = _enviar_con([("Grabación", "", "abc"), ("Mezcla", "", "99999999999")])
    assert v["prevenidos"] == 0 and v["marcados"] == [None, None]
