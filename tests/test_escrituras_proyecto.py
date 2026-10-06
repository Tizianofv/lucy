"""Las escrituras del proyecto en la página de proyectos (Lucy 1.0, E5).

Crear un proyecto, cambiarle el nombre (doble clic), el responsable y el grupo,
y cerrarlo o reabrirlo. Cada escritura es un `<form method="post">` a una ruta
del panel, y cada ruta escribe POR LA PUERTA que ya existe (`crud.editar`, con
huella y deshacer) o por `db.crear_proyecto`. El cliente NO se pone acá (lo hace
E3, con el lector de Noco).

Se recorre la ruta REAL con la plantilla REAL, con `crud` y `db` reales sobre
SQLite. La frontera es la de `tests/test_pagina_proyectos.py` y
`tests/test_base_m2.py`, más dos cosas:

  · NO hay Postgres: el éxito de `crud.deshacer` sobre una EDICIÓN usa
    `jsonb_populate_record` y no corre en SQLite; se prueba el de `crear`.
  · El JavaScript (doble clic para mostrar el formulario del nombre) se prueba
    con `osascript -l JavaScript` (JavaScriptCore de macOS) y un `document` de
    mentira que solo imita lo que el script toca: `addEventListener`,
    `querySelector`, `closest`, `hidden`, `focus`, `select`. NO prueba cómo lo
    ejecuta un navegador de verdad. Sin `osascript`, esas pruebas se SALTAN con
    su motivo.

Correr:  python3 -m pytest tests/test_escrituras_proyecto.py -q
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import types

import pytest

from test_grupo_ia import _archivos_de_texto, _ROOT       # (pone el entorno de prueba)
from test_pagina_proyectos import (_cliente, _dia, gente, mundo, titulo_de,  # noqa: F401
                                   tareas_en, ver)
import acciones.crud as crud
import config
import db.db as db
import noco_lectura


FICHAS_DE_NOCO = {101: "Cliente Uno", 102: "Persona Dos de Noco", 103: "Persona Tres de Noco"}


@pytest.fixture
def noco(monkeypatch):
    """Un Noco de mentira para las pruebas de la página, con la FORMA de lo que
    devuelve `noco_lectura` (`persona(id) -> {id, nombre} | None`,
    `buscar_personas(texto) -> [{id, nombre}]`, hasta 20): esa forma sale del propio
    módulo y se comprueba en `test_el_noco_de_mentira_tiene_la_forma_del_real`.
    Registra cada pedido (`pedidos`) y puede «caerse» (`noco.cae = True`: lanza
    `NocoNoContesta`, como el real)."""
    estado = types.SimpleNamespace(pedidos=[], cae=False, fichas=dict(FICHAS_DE_NOCO))

    async def persona(noco_id):
        estado.pedidos.append(("persona", noco_id))
        if estado.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        n = estado.fichas.get(noco_id)
        return {"id": noco_id, "nombre": n} if n else None

    async def buscar_personas(texto):
        estado.pedidos.append(("buscar", texto))
        if estado.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        return [{"id": i, "nombre": n} for i, n in estado.fichas.items()
                if texto.casefold() in n.casefold()][:20]

    monkeypatch.setattr(noco_lectura, "persona", persona)
    monkeypatch.setattr(noco_lectura, "buscar_personas", buscar_personas)
    return estado


def test_el_noco_de_mentira_tiene_la_forma_del_real(noco):
    """El doble contesta lo que documenta el módulo real: la ficha que construye
    `noco_lectura._ficha` a partir de una fila de Noco es `{id, nombre}` (y nada
    más: ni teléfono ni correo, G16), y las funciones públicas son las que el
    doble reemplaza."""
    ficha = noco_lectura._ficha({"Id": 5, "nombre": "Alguien", "telefono": "809", "correo": "x"})
    assert ficha == {"id": 5, "nombre": "Alguien"}
    assert sorted(f for f in dir(noco_lectura) if not f.startswith("_")
                  and callable(getattr(noco_lectura, f)) and f in ("persona", "buscar_personas")) == [
        "buscar_personas", "persona"]
    import asyncio
    for i, n in FICHAS_DE_NOCO.items():
        assert asyncio.new_event_loop().run_until_complete(noco_lectura.persona(i)) == {"id": i, "nombre": n}


def mandar(ruta: str, campos: dict | None = None, *, chat="dueno"):
    """Un POST al panel con la sesión del dueño (o sin sesión si `chat=None`)."""
    sesion = config.CHAT_ID_DUENO if chat == "dueno" else chat
    return _cliente(sesion).post(ruta, data=campos or {}, follow_redirects=False)


def _fila(mundo, pid: int) -> dict:
    f = mundo.con.execute("SELECT * FROM proyectos WHERE id = ?", (pid,)).fetchone()
    return dict(f) if f is not None else None


def _huellas(mundo) -> list[dict]:
    return [dict(f) for f in mundo.con.execute("SELECT * FROM log_acciones ORDER BY id")]


def _nuevo(mundo, **campos):
    datos = {"nombre": "Grabación del sencillo", "area": "CDS", "responsable": "Persona Dos"}
    datos.update(campos)
    return mandar("/proyectos/nuevo", datos)


# ═══════════════════════════════════════════════════════════════════════
# Crear un proyecto
# ═══════════════════════════════════════════════════════════════════════

def test_crear_deja_el_proyecto_con_su_grupo_responsable_y_sin_cliente(mundo, gente):
    r = _nuevo(mundo, nombre="  Grabación del sencillo  ")
    assert r.status_code == 303
    pid = mundo.con.execute("SELECT id FROM proyectos").fetchone()[0]
    assert r.headers["location"] == f"/proyectos?hecho=proyecto_nuevo&p={pid}"
    fila = _fila(mundo, pid)
    assert (fila["nombre"], fila["area"], fila["estado"]) == ("Grabación del sencillo", "CDS", "activo")
    assert fila["responsable_chat_id"] == gente.rosi
    assert fila["cliente_noco_id"] is None and fila["cliente_nombre"] is None
    assert fila["borrado_en"] is None


def test_crear_deja_su_huella_de_panel_y_se_puede_deshacer(mundo, gente):
    import asyncio
    _nuevo(mundo)
    huellas = _huellas(mundo)
    assert len(huellas) == 1
    h = huellas[0]
    assert (h["actor"], h["accion"], h["tabla"]) == ("panel", "crear", "proyectos")
    despues = json.loads(h["despues"])
    assert despues["nombre"] == "Grabación del sencillo" and despues["area"] == "CDS"
    assert h["antes"] is None
    # Deshacer (la rama genérica de «crear») lo manda a la papelera.
    asyncio.new_event_loop().run_until_complete(crud.deshacer(h["id"]))
    assert _fila(mundo, h["registro_id"])["borrado_en"] is not None
    # (sin el ejemplo del campo «Nombre» de la ventanita, que dice lo mismo)
    assert "Grabación del sencillo" not in re.sub(r'placeholder="[^"]*"', "", ver(mundo))


def test_el_proyecto_recien_creado_se_ve_con_su_aviso_y_su_responsable(mundo, gente):
    _nuevo(mundo)
    pid = mundo.con.execute("SELECT id FROM proyectos").fetchone()[0]
    html = ver(mundo, hecho="proyecto_nuevo", p=pid)
    assert "Proyecto creado." in html and titulo_de(html) == "Grabación del sencillo"
    assert '<option value="Persona Dos" selected>' in html


@pytest.mark.parametrize("nombre,clave", [("", "nombre_vacio"), ("   ", "nombre_vacio"),
                                           ("x" * (db.LARGO_NOMBRE_PROYECTO + 1), "nombre_largo")])
def test_un_nombre_que_no_vale_no_crea_nada_y_vuelve_al_formulario(mundo, nombre, clave):
    r = _nuevo(mundo, nombre=nombre)
    assert r.headers["location"] == f"/proyectos?nuevo=CDS&error={clave}"
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0
    assert _huellas(mundo) == []


def test_un_nombre_repetido_no_crea_otro_pero_uno_de_la_papelera_no_cuenta(mundo):
    mundo.proyecto(1, "Ya existe", area="CDS")
    mundo.proyecto(2, "De la papelera", area="CDS", borrado=True)
    r = _nuevo(mundo, nombre="YA EXISTE")
    assert r.headers["location"] == "/proyectos?nuevo=CDS&error=nombre_repetido"
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 2
    assert _nuevo(mundo, nombre="de la papelera").headers["location"].startswith(
        "/proyectos?hecho=proyecto_nuevo")


def test_el_rechazo_no_lleva_el_nombre_pedido_en_la_url(mundo):
    r = _nuevo(mundo, nombre="x" * 500)
    assert "xxxx" not in r.headers["location"]


@pytest.mark.parametrize("quien", ["", "Nadie", "Ajeno", "700100999", "-5", "Persona Dos; DROP", "x" * 300])
def test_el_responsable_tiene_que_ser_de_los_que_valen(mundo, gente, quien):
    r = _nuevo(mundo, responsable=quien)
    assert r.headers["location"] == "/proyectos?nuevo=CDS&error=responsable"
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0


@pytest.mark.parametrize("quien", ["Persona Uno", "Persona Dos", "Code", " persona dos "])
def test_se_puede_escoger_rosi_tiziano_o_code(mundo, gente, quien):
    assert _nuevo(mundo, responsable=quien).headers["location"].startswith("/proyectos?hecho=proyecto_nuevo")


@pytest.mark.parametrize("area", ["", "Inventada", "cds", "CDS' OR 1=1 --"])
def test_el_grupo_tiene_que_existir(mundo, area):
    r = _nuevo(mundo, area=area)
    assert "error=grupo" in r.headers["location"]
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0


def test_sin_sesion_no_se_crea_nada(mundo):
    assert mandar("/proyectos/nuevo", {"nombre": "x", "area": "CDS", "responsable": "Code"},
                  chat=None).status_code == 401
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0


def test_el_formulario_de_proyecto_nuevo_pide_nombre_y_responsable_y_el_cliente_va_en_la_ventanita(mundo, gente):
    """La PÁGINA aparte (la de sin JavaScript) pide nombre y responsable: el cliente
    es opcional y sin JavaScript se pone después, desde la cabecera. La
    VENTANITA (que solo existe con JavaScript) trae además el campo del cliente."""
    html = ver(mundo, nuevo="ACD")
    assert titulo_de(html) == "Proyecto nuevo en ACD"
    aparte = html.split("<h1>Proyecto nuevo en ACD</h1>", 1)[1].split("</section>", 1)[0]
    assert 'action="/proyectos/nuevo"' in aparte and '<input type="hidden" name="area" value="ACD">' in aparte
    for opcion in ("Persona Uno", "Persona Dos", "Code"):
        assert f'<option value="{opcion}">' in aparte
    assert "sin responsable" not in html.lower()
    assert 'name="cliente' not in aparte and "Cliente" not in aparte
    assert html.count('<input type="hidden" name="cliente"') == len(_pagina.AREAS)   # una por ventanita


def test_cada_grupo_ofrece_su_boton_de_proyecto_nuevo_y_sin_grupo_no(mundo):
    mundo.proyecto(1, "Sin grupo", area=None)
    html = ver(mundo)
    lista = html.split("<aside>", 1)[1].split("</aside>", 1)[0]
    for grupo in ("CDS", "ACD", "IA"):
        assert (f'<a class="nuevo-proy mas" href="/proyectos?nuevo={grupo}" aria-label="Proyecto nuevo en {grupo}" '
                f'title="Proyecto nuevo en {grupo}">+</a></h3>') in lista
    assert lista.count('class="nuevo-proy mas"') == 3
    assert 'class="nuevo-proy' not in ver(mundo, q="sin").split("<aside>", 1)[1].split("</aside>", 1)[0]


def test_un_grupo_que_no_existe_no_abre_el_formulario(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    assert titulo_de(ver(mundo, nuevo="Inventada")) == "Uno"


# ═══════════════════════════════════════════════════════════════════════
# Renombrar con doble clic
# ═══════════════════════════════════════════════════════════════════════

def test_el_titulo_se_edita_con_doble_clic_y_sin_javascript_hay_un_enlace(mundo):
    mundo.proyecto(1, "Mi proyecto", area="CDS")
    html = ver(mundo, p=1)
    assert '<h1 data-dbl="nombre" title="Doble clic para cambiar el nombre" >Mi proyecto</h1>' in html
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/1/nombre"[^>]* hidden>', html)
    assert '<noscript><a class="nota" href="/proyectos?p=1&amp;editar=nombre">Cambiar el nombre</a></noscript>' in html
    # Sin JavaScript el servidor dibuja el formulario abierto y esconde el título.
    abierto = ver(mundo, p=1, editar="nombre")
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/1/nombre"[^>]*>', abierto)
    assert " hidden" not in re.search(
        r'<form class="renombrar" method="post" action="/proyectos/1/nombre"[^>]*>', abierto).group(0)
    assert re.search(r'<h1 data-dbl="nombre"[^>]*hidden>', abierto)


def test_renombrar_guarda_y_la_pagina_lo_enseña(mundo):
    mundo.proyecto(1, "Viejo", area="CDS")
    r = mandar("/proyectos/1/nombre", {"nombre": "  Nuevo nombre "})
    assert r.headers["location"] == "/proyectos?nombre_guardado=1&p=1#proyecto-1"
    assert _fila(mundo, 1)["nombre"] == "Nuevo nombre"
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"]) == ("panel", "editar")
    assert titulo_de(ver(mundo, nombre_guardado=1, p=1)) == "Nuevo nombre"


def test_renombrar_a_uno_repetido_o_vacio_se_rechaza(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.proyecto(2, "Dos", area="CDS")
    assert mandar("/proyectos/1/nombre", {"nombre": "dos"}).headers["location"].startswith(
        "/proyectos?error=nombre_repetido&p=1")
    assert mandar("/proyectos/1/nombre", {"nombre": ""}).headers["location"].startswith(
        "/proyectos?error=nombre_vacio&p=1")
    assert _fila(mundo, 1)["nombre"] == "Uno" and _huellas(mundo) == []


# ── El script de la página, con JavaScriptCore de macOS ───────────────────
#
# El `document` de mentira que ya existía, más un constructor de «sitios» que
# arma cada formulario con LOS CAMPOS DE VERDAD del HTML renderizado: su
# `action`, cada `name` y el valor con el que nace el control. Así lo que se
# prueba es el script contra la página que se sirve, no contra un ejemplo
# escrito a mano que puede dejar de parecerse.
#
# FRONTERA: NO es un navegador. No hay validación de `required`, ni envío de
# verdad, ni foco, ni orden de eventos (un navegador dispara `focusout` Y
# `change`; acá se dispara uno por vez). Lo que se prueba es qué formulario
# manda el script y cuándo.

def _script_de_la_pagina(mundo) -> str:
    mundo.proyecto(1, "P", area="CDS")
    html = ver(mundo, p=1)
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert len(guiones) == 1
    return guiones[0]


_ARNES = """
var oyentes = {};
var evitado = 0;
function ev(destino, extra) {
  var e = {target: destino, preventDefault: function () { evitado++; }};
  for (var k in extra) e[k] = extra[k];
  return e;
}
/* Un sitio de mentira con los datos de UN formulario de verdad. */
function sitioDeMentira(datos) {
  var sitio = {};
  var campos = [];
  /* Cerrado, el formulario está escondido y el TEXTO se ve; abierto, al revés.
     El texto es el enlace del título: `navego` cuenta las veces que el clic
     siguió el `href` (lo que en un navegador sería irse a la otra página). */
  var texto = {hidden: !datos.oculto, navego: 0,
    /* Como un navegador: `click()` sobre el enlace VUELVE a disparar el evento
       `click` en el documento (con el enlace de destino). Un guion que no se
       proteja de eso se espera a sí mismo para siempre. */
    click: function () { this.navego++; if (oyentes.click) oyentes.click(ev(this)); },
    closest: function (s) {
      if (s === "[data-dbl]" || s === "a[data-dbl]") return this;
      return (s === "[data-edita]" && datos.edita) ? sitio : null; }};
  var form = {
    hidden: !!datos.oculto, action: datos.accion, enviados: 0,
    dataset: datos.auto ? {auto: ""} : {},
    requestSubmit: function () { enviarFormulario(this); },
    closest: function (s) { return (s === "[data-edita]" && datos.edita) ? sitio : null; },
    querySelector: function (s) {
      return s === "[data-dbl]" ? texto : (s === "input, textarea" ? campos[0] : null); },
    querySelectorAll: function (s) { return campos; }
  };
  (datos.campos || []).forEach(function (c) {
    campos.push({
      tagName: c.tag.toUpperCase(), name: c.name, value: c.valor, defaultValue: c.valor,
      form: form, focos: 0, seleccionado: 0,
      focus: function () { this.focos++; }, select: function () { this.seleccionado++; },
      closest: function (s) { return (s === "form[data-auto]" && datos.auto) ? form : null; }
    });
  });
  sitio.querySelector = function (s) {
    return s === "form.renombrar" ? form : (s === "[data-dbl]" ? texto : null); };
  return {sitio: sitio, form: form, campos: campos, texto: texto};
}
/* Un envío como lo hace un navegador (`requestSubmit`, o el clic en el botón):
   dispara `submit` en el documento y solo cuenta si nadie lo cancela. */
function enviarFormulario(form) {
  var e = {target: form, prevenido: false, preventDefault: function () { this.prevenido = true; }};
  if (oyentes.submit) oyentes.submit(e);
  if (!e.prevenido) form.enviados++;
}
var otro = {closest: function () { return null; }};
var document = {addEventListener: function (tipo, f) { oyentes[tipo] = f; }};
/* Los temporizadores, de mentira: el escenario decide cuándo pasa el tiempo
   (`correrTemporizadores()`), así que la prueba no espera de verdad. */
var temporizadores = {}, siguienteT = 0;
function setTimeout(f, ms) { siguienteT++; temporizadores[siguienteT] = f; return siguienteT; }
function clearTimeout(id) { delete temporizadores[id]; }
function cuantosTemporizadores() { return Object.keys(temporizadores).length; }
function correrTemporizadores() {
  var pendientes = [];
  for (var id in temporizadores) pendientes.push({id: id, f: temporizadores[id]});
  temporizadores = {};
  pendientes.forEach(function (p) { p.f(); });
}
"""


def _correr_en_jxa(script: str, escenario: str) -> dict:
    """Corre `script` con un `document` de mentira y el `escenario` (JS que lo
    ejercita y termina en una expresión JSON)."""
    arnes = _ARNES + script + "\n" + escenario
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", arnes], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


hay_osascript = pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="no hay osascript (JavaScriptCore de macOS): el script de la página no se prueba aquí")

# Un formulario abierto con un texto editable, como el del nombre del proyecto.
_SITIO = ('var S = sitioDeMentira({accion: "/proyectos/1/nombre", auto: true, edita: true,'
          ' oculto: true, campos: [{tag: "input", name: "nombre", valor: "P"}]});')
_FOTO = "JSON.stringify({texto: S.texto.hidden, form: S.form.hidden, focos: S.campos[0].focos, sel: S.campos[0].seleccionado, enviados: S.form.enviados})"


@hay_osascript
def test_js_el_doble_clic_en_el_titulo_muestra_el_formulario_y_enfoca_el_campo(mundo):
    r = _correr_en_jxa(_script_de_la_pagina(mundo), _SITIO + 'oyentes.dblclick(ev(S.texto));' + _FOTO)
    assert r == {"texto": True, "form": False, "focos": 1, "sel": 1, "enviados": 0}


@hay_osascript
def test_js_un_clic_en_el_titulo_abre_el_detalle(mundo):
    """El clic no se va derecho: espera al segundo. Cuando el tiempo pasa sin
    que llegue nadie más, sigue el enlace del título —el `href` que escribió el
    servidor— y NO abre el renombrar."""
    guion = _script_de_la_pagina(mundo)
    clic = _SITIO + "oyentes.click(ev(S.texto));"
    antes = _correr_en_jxa(guion, clic + "JSON.stringify({navego: S.texto.navego,"
                                          " esperando: cuantosTemporizadores(), evitado: evitado})")
    assert antes == {"navego": 0, "esperando": 1, "evitado": 1}
    # `enlace.click()` en un navegador VUELVE a disparar el clic; el guion no
    # puede esperarse a sí mismo (otro temporizador) ni cancelar ese segundo
    # clic (`evitado` sigue en 1: el segundo clic tiene que dejar seguir el enlace).
    despues = _correr_en_jxa(guion, clic + "correrTemporizadores();"
                                           "JSON.stringify({navego: S.texto.navego, form: S.form.hidden,"
                                           " texto: S.texto.hidden, esperando: cuantosTemporizadores(),"
                                           " evitado: evitado})")
    assert despues == {"navego": 1, "form": True, "texto": False, "esperando": 0, "evitado": 1}


@hay_osascript
def test_js_dos_clics_separados_abren_el_detalle_las_dos_veces(mundo):
    """El seguro contra el clic que el propio guion provoca se suelta al
    terminar: si se quedara puesto, el segundo clic de la persona (otra tarea,
    o la misma después) no abriría nada."""
    escenario = (_SITIO + "oyentes.click(ev(S.texto));correrTemporizadores();"
                 "oyentes.click(ev(S.texto));correrTemporizadores();"
                 "JSON.stringify({navego: S.texto.navego, esperando: cuantosTemporizadores()})")
    assert _correr_en_jxa(_script_de_la_pagina(mundo), escenario) == {"navego": 2, "esperando": 0}


@hay_osascript
def test_js_un_doble_clic_renombra_y_no_abre_el_detalle(mundo):
    """Los dos clics y el doble clic, en el orden en que los manda un navegador:
    el detalle NO se abre y el renombrar sí."""
    guion = _script_de_la_pagina(mundo)
    escenario = (_SITIO + "oyentes.click(ev(S.texto));oyentes.click(ev(S.texto));"
                 "oyentes.dblclick(ev(S.texto));correrTemporizadores();"
                 "JSON.stringify({navego: S.texto.navego, form: S.form.hidden,"
                 " texto: S.texto.hidden, focos: S.campos[0].focos})")
    assert _correr_en_jxa(guion, escenario) == {
        "navego": 0, "form": False, "texto": True, "focos": 1}


@hay_osascript
def test_js_un_clic_fuera_del_titulo_no_abre_nada(mundo):
    guion = _script_de_la_pagina(mundo)
    r = _correr_en_jxa(guion, "oyentes.click(ev(otro));correrTemporizadores();"
                              "JSON.stringify({esperando: cuantosTemporizadores(), evitado: evitado})")
    assert r == {"esperando": 0, "evitado": 0}


@hay_osascript
def test_js_el_doble_clic_en_otra_parte_no_hace_nada(mundo):
    r = _correr_en_jxa(_script_de_la_pagina(mundo), _SITIO + 'oyentes.dblclick(ev(otro));' + _FOTO)
    assert r == {"texto": False, "form": True, "focos": 0, "sel": 0, "enviados": 0}


@hay_osascript
def test_js_escape_dentro_del_formulario_lo_esconde_sin_guardar_y_otra_tecla_no(mundo):
    """Escape devuelve el campo a lo que decía, esconde el formulario y NO
    envía; Enter envía; y Escape en otra parte no toca nada."""
    guion = _script_de_la_pagina(mundo)
    abrir = _SITIO + "oyentes.dblclick(ev(S.texto));"
    escape = (abrir + 'S.campos[0].value = "otro";'
              'oyentes.keydown(ev(S.campos[0], {key: "Escape"}));' + _FOTO)
    assert _correr_en_jxa(guion, escape) == {
        "texto": False, "form": True, "focos": 1, "sel": 1, "enviados": 0}
    # Y el campo quedó como estaba: lo que se escribió no viaja.
    assert _correr_en_jxa(guion, abrir + 'S.campos[0].value = "otro";'
                          'oyentes.keydown(ev(S.campos[0], {key: "Escape"}));'
                          "JSON.stringify({v: S.campos[0].value, d: S.campos[0].defaultValue})") == {
        "v": "P", "d": "P"}
    fuera = abrir + 'oyentes.keydown(ev(otro, {key: "Escape"}));' + _FOTO
    assert _correr_en_jxa(guion, fuera)["form"] is False
    enter = abrir + 'oyentes.keydown(ev(S.campos[0], {key: "Enter"}));' + _FOTO
    assert _correr_en_jxa(guion, enter)["enviados"] == 1        # Enter sí guarda


@hay_osascript
def test_js_un_texto_se_guarda_al_salir_del_campo_y_solo_si_cambio(mundo):
    guion = _script_de_la_pagina(mundo)
    abrir = _SITIO + "oyentes.dblclick(ev(S.texto));"
    sin_cambio = abrir + "oyentes.focusout(ev(S.campos[0]));" + _FOTO
    assert _correr_en_jxa(guion, sin_cambio)["enviados"] == 0
    con_cambio = abrir + 'S.campos[0].value = "otro";oyentes.focusout(ev(S.campos[0]));' + _FOTO
    assert _correr_en_jxa(guion, con_cambio)["enviados"] == 1


@hay_osascript
def test_js_un_cambio_en_el_select_envia_su_formulario(mundo):
    sitio = ('var S = sitioDeMentira({accion: "/proyectos/1/responsable", auto: true,'
             ' campos: [{tag: "select", name: "responsable", valor: "Code"}]});')
    r = _correr_en_jxa(_script_de_la_pagina(mundo),
                       sitio + 'S.campos[0].value = "IA";oyentes.change(ev(S.campos[0]));' + _FOTO)
    assert r["enviados"] == 1 and r["form"] is False


@hay_osascript
def test_js_un_comentario_vacio_no_envia_y_uno_escrito_si(mundo):
    """Los DOS cuadros de verdad que sirve la página: el de comentar (nace
    vacío) y el de editar (nace con el texto del comentario). Vacío no manda
    nada en ninguno de los dos —también el que la persona VACÍA, que es el
    único caso que la guarda de «no cambió» no cubre—; con texto, sí."""
    guion = _script_de_la_pagina(mundo)          # ya deja el proyecto 1
    mundo.tarea(10, "una", proyecto=1)
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "primer comentario")
    cerrado = ver(mundo, p=1, t=10)
    crear = next(f for f in _formularios_de(cerrado) if f["accion"] == "/proyectos/tarea/10/comentar")
    assert crear["clase"] == "comentar"
    editar = next(f for f in _formularios_de(ver(mundo, p=1, t=10, editar_comentario=50))
                  if f["accion"] == "/proyectos/tarea/10/comentario/50/editar")

    vacio = _sitio_de_mentira(crear, edita=False) + "oyentes.focusout(ev(S.campos[0]));" + _FOTO
    assert _correr_en_jxa(guion, vacio)["enviados"] == 0
    escrito = (_sitio_de_mentira(crear, edita=False)
               + 'S.campos[0].value = "un comentario";oyentes.focusout(ev(S.campos[0]));' + _FOTO)
    r = _correr_en_jxa(guion, escrito)
    assert r["enviados"] == 1 and r["form"] is False
    # El de editar nace CON el texto del comentario: si lo borra, no se guarda.
    borrado = (_sitio_de_mentira(editar, edita=True)
               + 'S.campos[0].value = "";oyentes.focusout(ev(S.campos[0]));' + _FOTO)
    assert _correr_en_jxa(guion, borrado)["enviados"] == 0
    tocado = (_sitio_de_mentira(editar, edita=True)
              + 'S.campos[0].value = "ya corregido";oyentes.focusout(ev(S.campos[0]));' + _FOTO)
    assert _correr_en_jxa(guion, tocado)["enviados"] == 1


def _formulario_de_comentar_de_verdad(mundo) -> dict:
    mundo.tarea(10, "una", proyecto=1)
    html = ver(mundo, p=1, t=10)
    return next(f for f in _formularios_de(html) if f["accion"] == "/proyectos/tarea/10/comentar")


@hay_osascript
def test_js_enter_en_el_cuadro_de_comentarios_es_un_salto_de_linea_y_en_un_texto_envia(mundo):
    """Enter en un `textarea` (el comentario) escribe un salto de línea: ni
    envía ni se le cancela la tecla. Enter en un campo de una línea sí envía.
    Los dos formularios son los del HTML de verdad."""
    guion = _script_de_la_pagina(mundo)          # ya deja el proyecto 1
    comentar = _formulario_de_comentar_de_verdad(mundo)
    assert [c["tipo"] for c in comentar["campos"] if c["name"]] == ["textarea"]
    foto = "JSON.stringify({enviados: S.form.enviados, evitado: evitado})"
    en_textarea = (_sitio_de_mentira(comentar, edita=False)
                   + 'S.campos[0].value = "linea uno";oyentes.keydown(ev(S.campos[0], {key: "Enter"}));' + foto)
    assert _correr_en_jxa(guion, en_textarea) == {"enviados": 0, "evitado": 0}
    # (el doble clic que abre el campo ya cancela su propio evento: 1; el Enter: 1 más)
    en_linea = _SITIO + 'oyentes.dblclick(ev(S.texto));oyentes.keydown(ev(S.campos[0], {key: "Enter"}));' + foto
    assert _correr_en_jxa(guion, en_linea) == {"enviados": 1, "evitado": 2}


@hay_osascript
def test_js_al_comentar_el_foco_y_el_clic_del_boton_no_envian_dos_veces(mundo):
    """Lo que pasa de verdad al hacer clic en «Comentar» con el cuadro lleno:
    primero el cuadro pierde el foco (se envía solo) y luego el clic del botón
    envía el MISMO formulario. Llega un solo envío. Y pasado el par de segundos
    (la página no se fue), el formulario se puede enviar otra vez."""
    guion = _script_de_la_pagina(mundo)          # ya deja el proyecto 1
    comentar = _formulario_de_comentar_de_verdad(mundo)
    sitio = (_sitio_de_mentira(comentar, edita=False)
             + 'S.campos[0].value = "un comentario";oyentes.focusout(ev(S.campos[0]));')
    doble = sitio + "enviarFormulario(S.form);JSON.stringify({enviados: S.form.enviados})"
    assert _correr_en_jxa(guion, doble) == {"enviados": 1}
    pasado = sitio + "correrTemporizadores();enviarFormulario(S.form);JSON.stringify({enviados: S.form.enviados})"
    assert _correr_en_jxa(guion, pasado) == {"enviados": 2}


@hay_osascript
def test_js_el_seguro_de_doble_envio_vale_para_todos_los_formularios_de_la_pagina(mundo, gente, monkeypatch):
    """Hermanos: la lista de formularios sale del HTML renderizado de todas las
    vistas (los del proyecto y los de la tarea); a cada uno se le manda un
    envío doble y a todos les llega uno solo."""
    import test_escrituras_tarea as _t
    formas = {}
    for m, consultas in ((_mundo_de_formularios(monkeypatch, gente), [c for c, _ in _VISTAS.values()]),
                         (_t._mundo(monkeypatch, gente), [c for c, _ in _t._VISTAS_DE_TAREAS.values()])):
        for consulta in consultas:
            for f in _formularios_de(ver(m, **consulta)):
                formas[f["accion"]] = "data-auto" in f["atributos"]
    assert len(formas) >= 8, formas
    escenario = (
        "var acciones = " + json.dumps(sorted(formas)) + ";\n"
        "JSON.stringify(acciones.map(function (a) {\n"
        "  var S = sitioDeMentira({accion: a, auto: false, edita: false, oculto: false, campos: []});\n"
        "  enviarFormulario(S.form); enviarFormulario(S.form);\n"
        "  return {accion: a, enviados: S.form.enviados};\n"
        "}))")
    for dicho in _correr_en_jxa(_script_de_la_pagina(mundo), escenario):
        assert dicho["enviados"] == 1, dicho


@hay_osascript
def test_js_el_comentario_que_se_vuelve_a_editar_va_a_la_ruta_de_editar(mundo):
    """Con el formulario de verdad del comentario que se está editando: al
    salir del cuadro, el script envía ESE formulario —el de editar— y no el de
    crear. La ruta sale del HTML renderizado."""
    guion = _script_de_la_pagina(mundo)          # ya deja el proyecto 1
    mundo.tarea(10, "una", proyecto=1)
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "primer comentario")
    html = ver(mundo, p=1, t=10, editar_comentario=50)
    form = next(f for f in _formularios_de(html)
                if f["accion"] == "/proyectos/tarea/10/comentario/50/editar")
    sitio = _sitio_de_mentira(form, edita=True, oculto=False)
    escenario = (sitio + 'S.campos[0].value = "ya corregido";'
                 'oyentes.focusout(ev(S.campos[0]));'
                 "JSON.stringify({enviados: S.form.enviados, accion: S.form.action})")
    assert _correr_en_jxa(guion, escenario) == {
        "enviados": 1, "accion": "/proyectos/tarea/10/comentario/50/editar"}
    # Y el cuadro de crear NO es el que se envía: son dos formularios distintos.
    crear = next(f for f in _formularios_de(html) if f["accion"] == "/proyectos/tarea/10/comentar")
    assert crear["accion"] != form["accion"]


def la_funcion_de_marcar(codigo: str) -> tuple[str, str]:
    """(el texto de `function marcarSinSaltar(form) {...}`, el código SIN ella).
    Es el SEGUNDO pedido de red que el script puede hacer (6-oct-2026, Tiziano:
    marcar una tarea no puede mover la página): se envía el mismo formulario
    `form.marcar` y se vuelve a pedir la misma página. Se saca entera, con las llaves
    emparejadas, y se vigila aparte (`tests/test_marcar_sin_saltar.py`)."""
    m = re.search(r"function marcarSinSaltar\(form\) \{", codigo)
    assert m, "no está la función que marca sin recargar"
    nivel, k = 0, m.end() - 1
    while True:
        nivel += {"{": 1, "}": -1}.get(codigo[k], 0)
        if nivel == 0:
            break
        k += 1
    return codigo[m.start():k + 1], codigo[:m.start()] + codigo[k + 1:]


def sin_el_unico_pedido_permitido(codigo: str) -> str:
    """Fuera de la función de marcar (que se vigila aparte), el script puede hacer
    UN pedido de red, y ninguno más: el GET que busca personas de Noco para
    ofrecer coincidencias (`/personas/buscar?q=...`, sin método, sin cuerpo y sin
    opciones: un `fetch` con un solo argumento es un GET). Lo comprueba y devuelve
    el código SIN ese pedido NI esa función, para que las demás prohibiciones
    (otro `fetch`, `XMLHttpRequest`, `.submit(`...) se apliquen al resto.
    (E7, 1-oct-2026: Lucy solo LEE de Noco.)"""
    _, codigo = la_funcion_de_marcar(codigo)
    permitido = 'fetch("/personas/buscar?q=" + encodeURIComponent(q))'
    assert codigo.count("fetch(") == 1 and codigo.count(permitido) == 1, "el único fetch tiene que ser el de buscar personas"
    return codigo.replace(permitido, "")


def test_el_script_no_decide_nada_de_negocio(mundo):
    guion = _script_de_la_pagina(mundo)
    codigo = sin_el_unico_pedido_permitido(re.sub(r"/\*.*?\*/", "", guion, flags=re.S))
    assert re.search(r"fetch\(|XMLHttpRequest|\.submit\(|FormData|localStorage|eval\(", codigo) is None
    # Un oyente por evento y nada más: el clic (abrir el detalle, esperando al
    # segundo), el doble clic, el teclado, el cambio de un desplegable, la
    # salida de un campo y el envío (para no mandar dos veces el mismo).
    assert sorted(re.findall(r'addEventListener\("(\w+)"', codigo)) == [
        "change", "click", "dblclick", "focusout", "input", "keydown", "load", "submit"]


def _valor_del_campo(c: dict) -> str:
    """Con lo que nace un control en el HTML: el `value` del input, la opción
    marcada del select o el texto que trae el textarea."""
    if c["tipo"] == "select":
        marcada = _la_marcada(c) or _primera_habilitada(c)
        return _valor(marcada) if marcada is not None else ""
    return c["value"] or ""


def _campos_en_js(form: dict) -> str:
    """Los controles CON NOMBRE de ese formulario del HTML, como literal de
    JavaScript, con el valor con el que nacen. Los `hidden` no se miran: un
    navegador no los enfoca ni los cambia."""
    return ", ".join(
        "{{tag: {!r}, name: {!r}, valor: {!r}}}".format(
            "textarea" if c["tipo"] == "textarea" else c["tipo"], c["name"],
            _valor_del_campo(c))
        for c in form["campos"] if c["name"] and c["tipo"] != "hidden")


def _sitio_de_mentira(form: dict, *, edita: bool, oculto: bool = False) -> str:
    """El JavaScript que arma el sitio de mentira de ESE formulario del HTML
    renderizado: su `action`, si lleva `data-auto`, y sus controles."""
    return ("var S = sitioDeMentira({{accion: {!r}, auto: {}, edita: {}, oculto: {}, "
            "campos: [{}]}});").format(
        form["accion"], "true" if "data-auto" in form["atributos"] else "false",
        "true" if edita else "false", "true" if oculto else "false", _campos_en_js(form))


# ═══════════════════════════════════════════════════════════════════════
# El responsable del proyecto
# ═══════════════════════════════════════════════════════════════════════

def test_cambiar_el_responsable_por_nombre_guarda_el_chat_y_deja_huella(mundo, gente):
    mundo.proyecto(1, "P", area="CDS", responsable=gente.dueno)
    r = mandar("/proyectos/1/responsable", {"responsable": "Persona Dos"})
    assert r.headers["location"] == "/proyectos?hecho=responsable&p=1"
    assert _fila(mundo, 1)["responsable_chat_id"] == gente.rosi
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"], h["tabla"]) == ("panel", "editar", "proyectos")
    assert json.loads(h["antes"])["responsable_chat_id"] == gente.dueno
    assert json.loads(h["despues"])["responsable_chat_id"] == gente.rosi
    html = ver(mundo, hecho="responsable", p=1)
    assert "Responsable guardado." in html and '<option value="Persona Dos" selected>' in html


def test_code_tambien_puede_ser_responsable_de_un_proyecto(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mandar("/proyectos/1/responsable", {"responsable": "Code"})
    assert _fila(mundo, 1)["responsable_chat_id"] == config.CHAT_ID_CODE


@pytest.mark.parametrize("quien", ["Nadie", "Ajeno", "700100999", "x" * 300])
def test_un_responsable_que_no_vale_no_se_guarda(mundo, gente, quien):
    mundo.proyecto(1, "P", area="CDS", responsable=gente.dueno)
    r = mandar("/proyectos/1/responsable", {"responsable": quien})
    assert r.headers["location"] == "/proyectos?error=responsable&p=1"
    assert _fila(mundo, 1)["responsable_chat_id"] == gente.dueno and _huellas(mundo) == []


def test_el_responsable_de_un_proyecto_que_no_existe_o_esta_en_la_papelera_da_error(mundo):
    mundo.proyecto(2, "Borrado", area="CDS", borrado=True)
    for pid in (999, 2):
        assert mandar(f"/proyectos/{pid}/responsable", {"responsable": "Code"}).headers[
            "location"] == "/proyectos?error=proyecto"


def test_sin_sesion_no_se_cambia_el_responsable_ni_el_estado_ni_el_nombre(mundo):
    mundo.proyecto(1, "P", area="CDS")
    for ruta, campos in (("/proyectos/1/responsable", {"responsable": "Code"}),
                         ("/proyectos/1/estado", {"estado": "cerrado"}),
                         ("/proyectos/1/nombre", {"nombre": "Otro"}),
                         ("/proyectos/1/area", {"area": "IA"})):
        assert mandar(ruta, campos, chat=None).status_code == 401, ruta
    assert _huellas(mundo) == []


def test_el_selector_del_responsable_no_escribe_ningun_numero_de_chat(mundo, gente):
    mundo.proyecto(1, "P", area="CDS", responsable=gente.rosi)
    html = ver(mundo, p=1)
    for numero in (str(gente.rosi), str(gente.dueno)):
        assert numero not in html, numero
    # (Solo las OPCIONES: la página también lleva ids de proyecto en campos escondidos.)
    assert re.search(r'<option value="-?\d+"', html) is None, "una opción viaja con un número de chat"
    # Ya tiene responsable: el selector del proyecto no lleva el marcador de «falta»
    # (la ventanita de «+ Proyecto» sí tiene el suyo, y no cuenta).
    quienes = html.split('<div class="quienes">', 1)[1].split("</form>", 1)[0]
    assert "Escoge" not in quienes


# ═══════════════════════════════════════════════════════════════════════
# Cerrar y reabrir
# ═══════════════════════════════════════════════════════════════════════

def test_el_boton_de_cerrar_va_debajo_al_final_a_la_derecha(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "una", proyecto=1)
    html = ver(mundo, p=1)
    boton = '<a class="btn-linea" href="/proyectos?p=1&amp;confirmar=cerrar">Cerrar proyecto</a>'
    assert html.count(boton) == 1
    # (El «+ Agregar tarea con más opciones» se quitó de esta página: la maqueta no lo tiene.)
    assert "más opciones" not in html and html.index("Agregar tarea</button>") < html.index(boton)
    assert '<div class="acciones abajo">\n      ' + boton in html
    assert "justify-content:flex-end" in html.split(".acciones.abajo{", 1)[1].split("}", 1)[0]


@pytest.mark.parametrize("pendientes,frase", [
    (0, None),
    (1, "Queda 1 pendiente y sigue abierta en <a href=\"/tareas\">Tareas</a>."),
    (3, "Quedan 3 pendientes y siguen abiertas en <a href=\"/tareas\">Tareas</a>.")])
def test_la_confirmacion_dice_cuantas_pendientes_siguen_abiertas(mundo, pendientes, frase):
    mundo.proyecto(1, "P", area="CDS")
    for i in range(pendientes):
        mundo.tarea(10 + i, f"pendiente {i}", proyecto=1)
    mundo.tarea(50, "hecha", proyecto=1, estado="hecha", completado=_dia(-1))
    html = ver(mundo, p=1, confirmar="cerrar")
    assert "¿Cerrar este proyecto?" in html
    if frase:
        assert frase in html
    else:
        assert "Quedan" not in html and "Queda " not in html
    assert 'action="/proyectos/1/estado"' in html and '<input type="hidden" name="estado" value="cerrado">' in html
    assert ">Cancelar</a>" in html and "Cerrar proyecto</a>" not in html


def test_cerrar_deja_las_tareas_abiertas_y_el_proyecto_cerrado(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "sigue pendiente", proyecto=1)
    r = mandar("/proyectos/1/estado", {"estado": "cerrado"})
    assert r.headers["location"] == "/proyectos?hecho=cerrado&p=1"
    assert _fila(mundo, 1)["estado"] == "cerrado"
    estado_tarea = mundo.con.execute("SELECT estado, borrado_en FROM tareas WHERE id = 10").fetchone()
    assert tuple(estado_tarea) == ("pendiente", None)
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"]) == ("panel", "editar")
    assert json.loads(h["despues"])["estado"] == "cerrado"
    html = ver(mundo, hecho="cerrado", p=1)
    assert "Proyecto cerrado. Está en «cerrados»" in html and "Reabrir" in html
    assert "/tareas/nueva?proyecto=1" not in html and "Cerrar proyecto</a>" not in html
    assert 'data-tarea="10"' in html                      # la tarea sigue en la lista


def test_un_cerrado_no_recibe_tareas_y_reabrirlo_lo_arregla(mundo):
    import asyncio
    mundo.proyecto(1, "P", area="CDS")
    mandar("/proyectos/1/estado", {"estado": "cerrado"})
    cur = __import__("test_grupo_ia")._Conn(mundo.con).cursor(row_factory=dict)
    with pytest.raises(db.ProyectoNoAdmiteTareas):
        asyncio.new_event_loop().run_until_complete(db.proyecto_admite_tareas(cur, 1))
    r = mandar("/proyectos/1/estado", {"estado": "activo"})
    assert r.headers["location"] == "/proyectos?hecho=reabierto&p=1"
    assert _fila(mundo, 1)["estado"] == "activo"
    assert "Proyecto reabierto." in ver(mundo, hecho="reabierto", p=1)


@pytest.mark.parametrize("pedido", ["terminado", "", "cerrado y listo", "archivado"])
def test_un_estado_fuera_del_vocabulario_no_se_guarda(mundo, pedido):
    mundo.proyecto(1, "P", area="CDS")
    r = mandar("/proyectos/1/estado", {"estado": pedido})
    assert r.headers["location"] == "/proyectos?error=estado&p=1"
    assert _fila(mundo, 1)["estado"] == "activo" and _huellas(mundo) == []


def test_el_estado_de_un_proyecto_que_no_existe_da_error(mundo):
    assert mandar("/proyectos/999/estado", {"estado": "cerrado"}).headers["location"] == "/proyectos?error=proyecto"


def test_un_estado_libre_que_dejo_telegram_se_ve_y_se_corrige_al_cerrar_o_reabrir(mundo):
    mundo.proyecto(1, "Viejo", area="CDS", estado="terminado")
    html = ver(mundo, p=1)
    assert "tiene un estado que no es de la lista («terminado»)" in html
    assert "Cerrar proyecto</a>" in html                 # no está cerrado: se puede cerrar
    mandar("/proyectos/1/estado", {"estado": "cerrado"})
    assert _fila(mundo, 1)["estado"] == "cerrado"
    assert "no es de la lista" not in ver(mundo, p=1)


def test_el_esquema_no_impide_un_estado_libre_y_por_eso_la_pagina_lo_tolera():
    """Qué tan común es un estado libre en producción NO se puede medir con una
    prueba (la base no se toca): lo que se prueba es el ESQUEMA. `estado` es
    texto con DEFAULT y sin ningún CHECK, ni en `db/schema.sql` ni en una
    migración, así que Telegram pudo guardar cualquier cosa antes de la puerta
    de E2. Medirlo es un `SELECT estado, count(*) FROM proyectos GROUP BY 1` de
    la sala."""
    esquema = db._sin_comentarios((_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    tabla = re.search(r"CREATE TABLE proyectos\s*\((.*?)\n\);", esquema, re.S).group(1)
    assert re.search(r"estado\s+TEXT NOT NULL DEFAULT 'activo'", tabla)
    assert not re.search(r"CHECK\s*\(\s*estado", tabla, re.I)
    for migracion in (_ROOT / "db" / "migrations").glob("*.sql"):
        assert not re.search(r"proyectos[^;]*CHECK\s*\(\s*estado", migracion.read_text(encoding="utf-8"), re.I | re.S)


# ═══════════════════════════════════════════════════════════════════════
# Cambiar de grupo (P4)
# ═══════════════════════════════════════════════════════════════════════

def test_la_pagina_ya_no_ofrece_cambiar_de_grupo_porque_la_maqueta_no_lo_tiene(mundo):
    """Decisión de Tiziano (1-oct-2026): la página tiene las mismas funciones que
    la maqueta, y la maqueta no tiene «Mover a». Se quitó de ESTA página; la ruta
    `/proyectos/{pid}/area` y su efecto siguen (las pruebas de abajo le mandan el
    formulario directo)."""
    mundo.proyecto(1, "P", area="ACD")
    html = ver(mundo, p=1)
    migas = html.split('<div class="migas">', 1)[1].split("</div>", 1)[0]
    assert "<b>ACD</b> / Proyecto" in migas
    assert "Mover a" not in html and "/proyectos/1/area" not in html and "cambiar-grupo" not in html


def test_cambiar_de_grupo_lo_mueve_con_sus_tareas_y_deja_huella_de_panel(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "va con el", proyecto=1)
    r = mandar("/proyectos/1/area", {"area": "IA"})
    assert r.headers["location"] == "/proyectos?area_guardada=1&p=1"
    assert _fila(mundo, 1)["area"] == "IA"
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"]) == ("panel", "editar")
    html = ver(mundo, area_guardada=1, p=1)
    assert "Grupo guardado." in html and "<b>IA</b> / Proyecto" in html
    assert tareas_en(html) == [10]


def test_un_grupo_que_no_existe_no_se_guarda(mundo):
    mundo.proyecto(1, "P", area="CDS")
    assert mandar("/proyectos/1/area", {"area": "Inventada"}).headers["location"] == "/proyectos?error=area&p=1"
    assert _fila(mundo, 1)["area"] == "CDS"


# ═══════════════════════════════════════════════════════════════════════
# El cliente NO está en E5
# ═══════════════════════════════════════════════════════════════════════

def test_el_cliente_se_lee_de_la_base_y_se_escribe_solo_por_poner_cliente_con_la_ficha_de_noco(mundo):
    """El cliente de la cabecera sale de `proyectos.cliente_nombre`; se cambia con
    el formulario de `/proyectos/{pid}/cliente`; y la ÚNICA puerta que lo escribe
    es `db.poner_cliente`, llamada desde dos rutas (la del cliente y la de
    «proyecto nuevo») SIEMPRE con `leer_persona=noco_lectura.persona`, para que el
    nombre que se guarda sea el que Noco devuelve. SALE DEL ÁRBOL de `web/`."""
    mundo.proyecto(1, "P", area="CDS", cliente="Colegio")
    html = ver(mundo, p=1)
    assert '<label>Cliente <input class="campo-quien" type="text" name="pq" value="Colegio"' in html
    assert 'action="/proyectos/1/cliente"' in html and "Quitar el cliente" in html
    llamadas = {}
    for archivo in _archivos_de_texto():
        if archivo.suffix == ".py" and archivo.relative_to(_ROOT).parts[0] == "web":
            for f in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
                if isinstance(f, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    for n in ast.walk(f):
                        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                and n.func.attr == "poner_cliente"):
                            llamadas[f.name] = {k.arg: ast.unparse(k.value) for k in n.keywords}
    assert sorted(llamadas) == ["crear_proyecto_nuevo", "poner_cliente_del_proyecto"], llamadas
    for nombre, kw in llamadas.items():
        assert kw == {"leer_persona": "noco_lectura.persona"}, (nombre, kw)


# ═══════════════════════════════════════════════════════════════════════
# Los hermanos: quién más escribe un proyecto
# ═══════════════════════════════════════════════════════════════════════

def _rutas_post_de_proyectos(con_tareas: bool = False):
    """Cada función `@app.post("/proyectos...")` de `web/app.py`, con lo que
    escribe: las llamadas a `crud.editar("proyectos", ...)` (con su `actor`) y a
    `db.crear_proyecto`. LA LISTA SALE DEL ÁRBOL SINTÁCTICO, no de nombres. Sin
    `con_tareas` deja fuera las de la tarea (`/proyectos/tarea/...` y
    `/proyectos/{pid}/tareas`), que son de `tests/test_escrituras_tarea.py`."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    salida = {}
    for f in arbol.body:
        if not isinstance(f, ast.AsyncFunctionDef):
            continue
        for d in f.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
                    and d.args and isinstance(d.args[0], ast.Constant)
                    and d.args[0].value.startswith("/proyectos")
                    and (con_tareas or not (d.args[0].value.startswith("/proyectos/tarea/")
                                            or d.args[0].value.endswith("/tareas")))):
                escribe, pide_sesion = [], False
                for n in ast.walk(f):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "editar" and n.args
                            and isinstance(n.args[0], ast.Constant) and n.args[0].value == "proyectos"):
                        actor = [k.value.value for k in n.keywords
                                 if k.arg == "actor" and isinstance(k.value, ast.Constant)]
                        escribe.append(("crud.editar", tuple(actor)))
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr in ("crear_proyecto", "poner_cliente",
                                                "agregar_participante", "quitar_participante")):
                        escribe.append((f"db.{n.func.attr}", ()))
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "puede_entrar"):
                        pide_sesion = True
                salida[f.name] = {"ruta": d.args[0].value, "escribe": escribe, "sesion": pide_sesion}
    return salida


def test_toda_ruta_post_de_proyectos_pide_sesion_y_escribe_por_una_puerta_con_actor_panel():
    rutas = _rutas_post_de_proyectos()
    # Cada ruta y las puertas por las que escribe (E7, 1-oct-2026: el cliente y las
    # personas). «Proyecto nuevo» tiene dos: crea, y después pone el cliente si la
    # ventanita mandó uno.
    esperadas = {
        "/proyectos/nuevo": {"db.crear_proyecto", "db.poner_cliente"},
        "/proyectos/{pid}/nombre": {"crud.editar"},
        "/proyectos/{pid}/area": {"crud.editar"},
        "/proyectos/{pid}/responsable": {"crud.editar"},
        "/proyectos/{pid}/estado": {"crud.editar"},
        "/proyectos/{pid}/cliente": {"db.poner_cliente"},
        "/proyectos/{pid}/personas": {"db.agregar_participante"},
        "/proyectos/{pid}/personas/{xid}/quitar": {"db.quitar_participante"},
    }
    assert {r["ruta"] for r in rutas.values()} == set(esperadas)
    for nombre, r in rutas.items():
        assert r["sesion"], f"{nombre} no pide sesión"
        assert {puerta for puerta, _ in r["escribe"]} == esperadas[r["ruta"]], (
            f"{nombre} escribe por {r['escribe']}")
        assert len(r["escribe"]) == len(esperadas[r["ruta"]]), f"{nombre}: una llamada por puerta"
        for puerta, actor in r["escribe"]:
            if puerta == "crud.editar":
                assert actor == ("panel",), f"{nombre}: actor {actor}"


def test_el_censo_de_rutas_ve_una_ruta_inventada(tmp_path, monkeypatch):
    falso = tmp_path / "web"
    falso.mkdir()
    (falso / "app.py").write_text(
        "@app.post('/proyectos/{pid}/otra')\n"
        "async def otra(request, pid):\n"
        "    await crud.editar('proyectos', pid, {}, motivo='x')\n")
    monkeypatch.setattr(__import__("test_escrituras_proyecto"), "_ROOT", tmp_path)
    r = _rutas_post_de_proyectos()
    assert r["otra"]["sesion"] is False and r["otra"]["escribe"] == [("crud.editar", ())]


def _escritores_sql_de_proyectos() -> dict:
    """Por columna (`area`, `estado`, `responsable_chat_id`, `nombre`): las
    funciones del código (fuera de `tests/`) cuyo texto SQL escribe esa columna
    con un `INSERT INTO proyectos (...)` o `UPDATE proyectos SET ...`. SALE DE
    RECORRER LOS `.py`. FRONTERA: un SQL armado al vuelo (los escritores
    genéricos `crud.editar`, `deshacer`) no se ve; esos los cubren las pruebas
    de las puertas (`tests/test_base_m2.py`)."""
    columnas = ("nombre", "area", "estado", "responsable_chat_id")
    salida = {c: set() for c in columnas}
    for archivo in _archivos_de_texto():
        if archivo.suffix != ".py":
            continue
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                    continue
                for m in re.finditer(r"INSERT\s+INTO\s+proyectos\s*\(([^)]*)\)", n.value, re.I):
                    for c in columnas:
                        if re.search(rf"\b{c}\b", m.group(1)):
                            salida[c].add(fn.name)
                for m in re.finditer(r"UPDATE\s+proyectos\s+SET\s+(.*?)(?:WHERE|$)", n.value, re.I | re.S):
                    for c in columnas:
                        if re.search(rf"\b{c}\s*=", m.group(1)):
                            salida[c].add(fn.name)
    return salida


def test_quien_escribe_cada_columna_del_proyecto_y_si_pasa_por_su_puerta():
    escritores = _escritores_sql_de_proyectos()
    # EL TRINQUETE: un escritor nuevo de estas columnas se pone rojo hasta que
    # alguien lo mire y lo declare acá.
    assert escritores["responsable_chat_id"] == {"crear_proyecto"}
    assert escritores["estado"] == set()            # nacen con el DEFAULT; el resto, por `crud.editar`
    assert escritores["area"] == {"convertir_tarea_en_proyecto", "crear_proyecto"}
    # (`_buscar_o_crear` arma su `INSERT INTO {tabla}` al vuelo y este censo no lo
    # ve; el de `tests/test_nombre_de_proyecto.py` sí, y exige la puerta del nombre.)
    # `perfil` SALIÓ de acá con E8 (1-oct-2026): su `INSERT INTO proyectos
    # (nombre, descripcion)` se borró, porque un proyecto ya no nace por el
    # perfil — nace con grupo y responsable, por `crear_proyecto`. El trinquete
    # hizo lo suyo: al desaparecer ese escritor, esta línea se puso roja hasta
    # que alguien vino y lo declaró.
    assert escritores["nombre"] == {"convertir_tarea_en_proyecto", "crear_proyecto"}
    # `crear_proyecto` llama a las puertas de lo que escribe.
    fuente = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    crear = next(f for f in ast.walk(fuente) if isinstance(f, ast.AsyncFunctionDef) and f.name == "crear_proyecto")
    llamadas = {n.func.id for n in ast.walk(crear) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"nombre_de_proyecto_que_vale", "puede_ser_responsable", "areas",
            "proyecto_vivo_con_nombre"} <= llamadas


def test_el_censo_de_escritores_ve_un_escritor_inventado(tmp_path):
    texto = "async def inventado(c):\n    await c.execute('UPDATE proyectos SET estado = 1, area = 2 WHERE id = 3')\n"
    n = [n for n in ast.walk(ast.parse(texto)) if isinstance(n, ast.Constant)][0]
    m = re.search(r"UPDATE\s+proyectos\s+SET\s+(.*?)(?:WHERE|$)", n.value, re.I | re.S)
    assert re.search(r"\bestado\s*=", m.group(1)) and re.search(r"\barea\s*=", m.group(1))


def test_la_nota_del_menu_ya_no_dice_que_la_pagina_es_solo_para_mirar():
    import web.menu as menu
    nota = next(p.nota for p in menu.pantallas() if p.ruta == "/proyectos")
    assert "solo para mirar" not in nota
    for dicho in ("se crea un proyecto nuevo", "el nombre", "el responsable", "el grupo",
                  "se cierra o se reabre", "el cliente todavía no se pone"):
        assert dicho in nota, dicho


# ═══════════════════════════════════════════════════════════════════════
# La puerta `db.crear_proyecto`, llamada DIRECTO (sin la ruta)
# ═══════════════════════════════════════════════════════════════════════
#
# El docstring de la puerta dice que decide ella el nombre, el grupo y el
# responsable, y que la ruta solo traduce. Quien la llame sin la ruta (el
# Telegram de E8) tiene que encontrar la misma puerta: se prueba llamándola.

def _filas(mundo) -> int:
    return mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0]


@pytest.mark.parametrize("responsable", [None, True, False, "Persona Dos", "700100001", 12345,
                                          700100999, 3.5, [700100001]])
async def test_la_puerta_rechaza_un_responsable_que_no_es_un_chat_que_valga(mundo, gente, responsable):
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        await db.crear_proyecto("Un proyecto", "CDS", responsable)
    assert e.value.clave == "responsable"
    assert _filas(mundo) == 0 and _huellas(mundo) == []


@pytest.mark.parametrize("area", ["Inventada", "", None, "cds", 5, "CDS' OR 1=1 --"])
async def test_la_puerta_rechaza_un_grupo_que_no_existe(mundo, gente, area):
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        await db.crear_proyecto("Un proyecto", area, gente.rosi)
    assert e.value.clave == "grupo"
    assert _filas(mundo) == 0 and _huellas(mundo) == []


@pytest.mark.parametrize("nombre,clave", [("", "vacio"), ("   ", "vacio"), (None, "vacio"), (5, "vacio"),
                                           ("x" * (db.LARGO_NOMBRE_PROYECTO + 1), "largo")])
async def test_la_puerta_rechaza_un_nombre_malo(mundo, gente, nombre, clave):
    with pytest.raises(db.NombreDeProyectoNoVale) as e:
        await db.crear_proyecto(nombre, "CDS", gente.rosi)
    assert e.value.clave == clave
    assert _filas(mundo) == 0 and _huellas(mundo) == []


async def test_la_puerta_rechaza_un_nombre_repetido_pero_no_el_de_uno_en_la_papelera(mundo, gente):
    mundo.proyecto(1, "Ya existe", area="CDS")
    mundo.proyecto(2, "En la papelera", area="CDS", borrado=True)
    with pytest.raises(db.NombreDeProyectoNoVale) as e:
        await db.crear_proyecto(" YA EXISTE ", "CDS", gente.rosi)
    assert e.value.clave == "repetido" and _filas(mundo) == 2
    nuevo = await db.crear_proyecto("en la papelera", "CDS", gente.rosi)
    assert nuevo["nombre"] == "en la papelera" and _filas(mundo) == 3


async def test_la_puerta_decide_en_orden_nombre_grupo_responsable(mundo):
    with pytest.raises(db.NombreDeProyectoNoVale):
        await db.crear_proyecto("", "Inventada", None)
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        await db.crear_proyecto("Bien", "Inventada", None)
    assert e.value.clave == "grupo"


@pytest.mark.parametrize("quien", ["dueno", "rosi", "code"])
async def test_la_puerta_deja_pasar_a_rosi_a_tiziano_y_a_code(mundo, gente, quien):
    chat = {"dueno": gente.dueno, "rosi": gente.rosi, "code": config.CHAT_ID_CODE}[quien]
    nuevo = await db.crear_proyecto("  Con espacios  ", "ACD", chat)
    assert (nuevo["nombre"], nuevo["area"], nuevo["responsable_chat_id"]) == ("Con espacios", "ACD", chat)
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"], h["registro_id"]) == ("panel", "crear", nuevo["id"])


# ═══════════════════════════════════════════════════════════════════════
# Los formularios del HTML, enviados a la ruta real como lo haría el navegador
# ═══════════════════════════════════════════════════════════════════════
#
# Antes las pruebas llamaban a la ruta con campos escritos a mano y nunca leían
# el formulario de la plantilla: un `name` cambiado o un `action` apuntando a
# otro proyecto no lo veía nadie. Acá la lista de formularios SALE DEL HTML
# renderizado de cada vista; de cada uno se toma su `action`, sus controles y los
# valores que el navegador mandaría (hidden con su `value`, select con la opción
# marcada o la que la persona escoge, texto con lo que se escriba), se envía a
# la ruta de verdad y se mira el efecto sobre el proyecto DE LA PÁGINA y que nada
# más cambió.
#
# FRONTERA, dicha una vez: ESTO NO ES UN NAVEGADOR (es `html.parser`). No ejecuta
# el script, no aplica `required`, y no sabe qué hace un navegador con `disabled`,
# `hidden`, `style`, `onsubmit`, `enctype`, `formaction`... Lo que vigila es una
# REGLA de HTML simple (ver `_ATRIBUTOS`): los formularios de escritura usan solo
# esos elementos y atributos, y todo lo que el lector no sepa clasificar hace
# fallar la prueba. No se persigue cada comportamiento del navegador: se les pone
# fondo prohibiéndolos.
#
# LO QUE LA REGLA CUMPLE DE VERDAD, y nada más: el HTML del propio formulario y
# el de los ANCESTROS QUE NOMBRA LA LISTA (`noscript`, `template`, `dialog`,
# `details`, otro `form`, y los atributos `hidden`, `inert`, `on*` y `style` de
# cualquier ancestro). Lo que queda FUERA, y esta prueba NO ve:
#   · las HOJAS DE ESTILO: un `form.resp{display:none}`, un `pointer-events:none`
#     o un `visibility:hidden` en el `<style>` de la plantilla (o en cualquier
#     CSS) esconden o apagan un formulario o un botón sin tocar su HTML;
#   · los ANCESTROS FUERA DE LA LISTA: un `<fieldset disabled>` o un
#     `<div popover>` alrededor del formulario lo apagan o lo esconden y el lector
#     no lo mira.
# Por eso esta prueba NO garantiza que lo que se envía aquí sea lo que haría un
# navegador de verdad.

from html.parser import HTMLParser  # noqa: E402

import test_pagina_proyectos as _pagina  # noqa: E402


_VACIOS = {"input", "br", "hr", "img", "meta", "link"}

# LA REGLA, EN UNA LÍNEA: los formularios de escritura de la página usan HTML
# SIMPLE —los elementos y atributos de esta tabla, ninguno más—, y lo que el
# lector no sepa clasificar HACE FALLAR la prueba (el cubo estricto). Un
# navegador de verdad hace mil cosas con `disabled`, `hidden`, `style`,
# `onsubmit`, `enctype`, `formaction`, `form=`, `type="button"`, un `<noscript>`...
# y esta prueba no las modela: las PROHÍBE en el formulario y en los ancestros
# que nombra la lista. Mientras pase, el HTML de esos elementos es el simple de la
# tabla; NO que el navegador haga lo mismo que el lector (las hojas de estilo y
# los ancestros fuera de la lista quedan sin vigilar: ver la FRONTERA de arriba).
_ATRIBUTOS = {
    # `data-auto` (el encargo de la página, 1-oct-2026): marca los formularios
    # que se guardan solos —los textos al salir del campo y los desplegables al
    # escoger—, para que el script no tenga que llevar una lista de clases
    # tecleada. Los que CREAN una fila con más de un campo (proyecto nuevo,
    # tarea nueva) NO lo llevan: guardarían a medias.
    # `data-pide-persona` (1-oct-2026): el formulario de agregar una persona; le dice
    # al script que NO lo envíe sin persona escogida. No cambia lo que se envía.
    "form": {"method", "action", "class", "data-auto", "data-pide-persona"},
    # `min` (1-oct-2026): el piso de fecha del campo de día y hora de la tarea
    # nueva. Es una ayuda del navegador, no una guarda — lo que vale lo decide
    # `_vence_con_hora_valido` en el servidor.
    # `data-buscar-persona` y `autocomplete` (1-oct-2026, personas de Noco): la caja
    # donde se escribe para buscar a una persona. `data-buscar-persona` le dice al
    # script qué hacer con las coincidencias («enviar» el formulario o «elegir»
    # copiando el Id a un campo escondido) y no cambia lo que se envía;
    # `autocomplete="off"` solo apaga la ayuda del navegador.
    "input": {"type", "name", "value", "required", "maxlength", "placeholder", "aria-label", "min",
              "data-buscar-persona", "autocomplete",
              # `data-elegida` (1-oct-2026): el campo escondido donde queda el Id de la
              # persona o del cliente ESCOGIDOS en la ventanita; solo marca cuál es.
              "data-elegida"},
    "select": {"id", "name", "required"},
    "option": {"value", "selected", "disabled"},
    # `title` y `aria-label` (E6): el botón redondo de marcar hecha (○ o ✓) no
    # tiene texto; su nombre para quien lo lee en voz alta viaja en `aria-label`,
    # y `title` es lo que dice al pasar el cursor. No cambian lo que envía.
    # `name` y `value` (1-oct-2026, personas de Noco): cada coincidencia de una
    # búsqueda es un botón que envía su formulario con el Id de Noco
    # (`noco_id`), y «Quitar el cliente» envía `noco_id` vacío. Un botón con
    # nombre manda su par SOLO si es el que se pulsó: `_lo_que_manda_el_navegador`
    # lo recibe por `boton=`.
    "button": {"class", "type", "title", "aria-label", "name", "value"},
    "label": {"for"},
    # `title` y `aria-label` en el enlace (1-oct-2026): el `＋` que abre los
    # renglones de «¿sale una tarea nueva de ésta?» no dice nada por su texto,
    # y así lo dice al pasar el cursor y al leerlo en voz alta. No cambia a
    # dónde lleva el enlace.
    "a": {"class", "href", "title", "aria-label"},
    "div": {"class"},
    # Los mensajes de la lista de coincidencias («Nadie con … en Noco», el error
    # de Noco) son un `<p class="vacio">` dentro del formulario: no envían nada.
    "p": {"class"},
    # E6 (Lucy 1.0), cada uno con su porqué: el cuadro de comentario y el de
    # editarlo son de varias líneas (`textarea`), y la fecha límite opcional de
    # una tarea nueva es un `<input type="date">` (lo manda el navegador como
    # texto `AAAA-MM-DD`, o vacío).
    "textarea": {"name", "required", "maxlength", "placeholder", "aria-label"},
}
# `datetime-local` (1-oct-2026): la fecha de la tarea que sale de otra es la
# MISMA que ofrece /tareas en su renglón derivado, día y hora (`deriva_vence_`),
# y la lee el mismo `_vence_con_hora_valido`.
_TIPOS_DE_INPUT = {"hidden", "text", "date", "datetime-local"}
# La ÚNICA excepción, por nombre: `form.renombrar` nace `hidden` y lo muestra el
# JavaScript (doble clic) o el servidor (`?editar=nombre`); hay pruebas de las dos.
_NACE_ESCONDIDO = {"renombrar"}
# El color de cada grupo viaja en dos variables (1-oct-2026): `--claro`, el de
# `areas.color`, y `--oscuro`, el mismo tono aclarado para el modo oscuro. El
# envoltorio de la vista lleva además `display:contents`.
_ESTILO_DEL_GRUPO = re.compile(
    r"(display:contents;)?--claro:#[0-9a-fA-F]{3,8};--oscuro:#[0-9a-fA-F]{3,8}")


class _LectorDeFormularios(HTMLParser):
    """Lee los formularios y, a la vez, junta `problemas`: todo lo que no sea el
    HTML simple de la regla de arriba. FRONTERA: NO es un navegador. Lo que
    vigila es esa regla sobre el HTML del formulario y de los ancestros que
    nombra la lista; no ve las hojas de estilo (CSS que esconda o apague un
    formulario o un botón) ni los ancestros fuera de la lista (`<fieldset
    disabled>`, `<div popover>`), y no es el comportamiento completo de un
    navegador."""

    def __init__(self):
        super().__init__()
        self.formularios = []
        self.problemas = []
        self._pila = []
        self._f = self._sel = self._op = self._boton = self._ta = None

    def _mal(self, texto):
        self.problemas.append(texto)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        nombres = [k for k, _ in attrs]
        if tag == "form":
            metodo = (a.get("method") or "get").lower()
            f = {"metodo": metodo, "accion": a.get("action"), "clase": a.get("class") or "",
                 "atributos": set(nombres), "campos": [], "botones": [], "post": metodo == "post"}
            self.formularios.append(f)
            if f["post"]:
                self._revisar_formulario(f, nombres)
            self._f = f
        elif self._f is not None and self._f["post"]:
            self._dentro_de_un_formulario(tag, a, nombres)
        if tag not in _VACIOS:
            self._pila.append((tag, a))

    def _revisar_formulario(self, f, nombres):
        donde = f"form {f['accion']}"
        permitidos = set(_ATRIBUTOS["form"])
        if f["clase"] in _NACE_ESCONDIDO:
            permitidos.add("hidden")
        for n in nombres:
            if n not in permitidos:
                self._mal(f"{donde}: atributo no permitido {n!r}")
        for tag, atr in self._pila:
            # `details` solo si es el que pliega las tareas hechas (E6): su
            # `<summary>` lo abre con un clic, sin JavaScript, y adentro están
            # las mismas filas con sus formularios. Cualquier otro `details`
            # esconde el formulario y falla.
            # `dialog` solo si es la ventanita de «+ Proyecto en X» (1-oct-2026):
            # el servidor la escribe cerrada, el enlace de al lado la abre con
            # JavaScript y sin JavaScript no se ve nunca (el enlace lleva a la
            # página aparte). Su formulario es el mismo de esa página. Cualquier
            # otro `dialog` esconde el formulario y falla.
            if tag in ("noscript", "template", "form") or (
                    tag == "details" and atr.get("class") != "plegar") or (
                    tag == "dialog" and atr.get("class") != "ventana"):
                self._mal(f"{donde}: dentro de <{tag}>")
            for n, v in atr.items():
                if n in ("hidden", "inert") or n.startswith("on"):
                    self._mal(f"{donde}: un ancestro <{tag}> tiene {n!r}")
                if n == "style" and not _ESTILO_DEL_GRUPO.fullmatch(v or ""):
                    self._mal(f"{donde}: un ancestro <{tag}> tiene un style que no es el del grupo")

    def _dentro_de_un_formulario(self, tag, a, nombres):
        donde = f"form {self._f['accion']}"
        if tag not in _ATRIBUTOS:
            self._mal(f"{donde}: elemento no permitido <{tag}>")
            return
        for n in nombres:
            if n not in _ATRIBUTOS[tag]:
                self._mal(f"{donde}: <{tag}> con atributo no permitido {n!r}")
        if tag == "input" and a.get("type", "text") not in _TIPOS_DE_INPUT:
            self._mal(f"{donde}: <input> con type {a.get('type')!r}")
        if tag == "button" and a.get("type") not in (None, "submit"):
            self._mal(f"{donde}: <button> con type {a.get('type')!r}")
        f = self._f
        if tag == "input":
            f["campos"].append({"tipo": a.get("type", "text"), "name": a.get("name"),
                                "value": a.get("value"), "disabled": "disabled" in a})
        elif tag == "textarea":
            self._ta = {"tipo": "textarea", "name": a.get("name"), "value": "", "disabled": "disabled" in a}
            f["campos"].append(self._ta)
        elif tag == "select":
            self._sel = {"tipo": "select", "name": a.get("name"), "disabled": "disabled" in a, "opciones": []}
            f["campos"].append(self._sel)
        elif tag == "option" and self._sel is not None:
            self._op = {"value": a.get("value"), "selected": "selected" in a,
                        "disabled": "disabled" in a, "texto": ""}
            self._sel["opciones"].append(self._op)
        elif tag == "button":
            self._boton = {"texto": "", "tipo": a.get("type"), "name": a.get("name"), "value": a.get("value")}
            f["botones"].append(self._boton)
        elif tag == "div" and "sugerencias" in (a.get("class") or "").split():
            f["sugerencias"] = True

    def handle_data(self, dato):
        if self._boton is not None:
            self._boton["texto"] += dato
        if self._op is not None:
            self._op["texto"] += dato
        if self._ta is not None:
            self._ta["value"] += dato

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._ta = None
        if tag == "form":
            f = self._f
            if f is not None and f["post"]:
                donde = f"form {f['accion']}"
                # Un formulario de personas de Noco (cliente, agregar una persona) no
                # trae su botón de envío escrito: lo traen las COINCIDENCIAS de la
                # búsqueda, que el servidor (sin JavaScript) o el navegador (con él)
                # dibujan dentro de su `<div class="sugerencias">`. Ese contenedor
                # cuenta como el lugar del botón; sin botón ni contenedor, falla.
                if not f["botones"] and not f.get("sugerencias"):
                    self._mal(f"{donde}: no tiene un botón que envíe")
                nombres = [c["name"] for c in f["campos"] if c["name"]]
                repetidos = sorted({n for n in nombres if nombres.count(n) > 1})
                if repetidos:
                    self._mal(f"{donde}: nombre repetido {repetidos}")
            self._f = self._sel = self._op = self._boton = self._ta = None
        elif tag == "select":
            self._sel = self._op = None
        elif tag == "option":
            self._op = None
        elif tag == "button":
            self._boton = None
        if tag not in _VACIOS:
            for i in range(len(self._pila) - 1, -1, -1):
                if self._pila[i][0] == tag:
                    del self._pila[i:]
                    break


def _formularios_de(html: str, solo_post=True) -> list[dict]:
    lector = _LectorDeFormularios()
    lector.feed(html)
    return [f for f in lector.formularios if not solo_post or f["metodo"] == "post"]


def _problemas_de_html_simple(html: str) -> list[str]:
    """Todo lo que, en los formularios `post` de `html`, se sale del HTML simple
    (vacío = cumple la regla)."""
    lector = _LectorDeFormularios()
    lector.feed(html)
    lector.close()
    return lector.problemas


def _valor(op: dict) -> str:
    return op["value"] if op["value"] is not None else op["texto"].strip()


def _lo_que_manda_el_navegador(form: dict, escribir, escoger, boton: dict | None = None) -> dict:
    """Los pares nombre=valor que mandaría el navegador: hidden con su `value`,
    texto con lo que `escribir(campo)` devuelva, select con `escoger(campo)` (la
    opción que la persona deja o cambia); un control sin nombre, deshabilitado
    o una opción deshabilitada no viajan."""
    datos = {}
    if boton is not None:                   # el botón PULSADO manda su par (los demás no)
        assert boton in form["botones"] and boton["name"], boton
        datos[boton["name"]] = boton["value"] or ""
    vistos = [c["name"] for c in form["campos"] if c["name"] and not c.get("disabled")]
    assert len(vistos) == len(set(vistos)), f"nombre repetido en {form['accion']}: un dict lo colapsaría"
    for c in form["campos"]:
        if not c["name"] or c.get("disabled"):
            continue
        if c["tipo"] == "select":
            op = escoger(c)
            if op is not None and not op["disabled"]:
                datos[c["name"]] = _valor(op)
        elif c["tipo"] == "hidden":
            datos[c["name"]] = c["value"] or ""
        elif c["tipo"] in ("text", "search", "date", "datetime-local", "textarea"):
            datos[c["name"]] = escribir(c)
        else:
            # Un tipo de control que el lector conoce y este envío no sabe llenar
            # no se salta en silencio: se rompe.
            raise AssertionError(f"el envío no sabe llenar un control {c['tipo']!r} ({c['name']})")
    return datos


def _mundo_de_formularios(monkeypatch, gente):
    m = _pagina.Mundo()
    monkeypatch.setattr(db, "pool", _pagina.g._Pool(m.con))
    monkeypatch.setattr(db, "hoy_rd", lambda: _pagina.HOY)
    m.proyecto(1, "Otro uno", area="CDS", responsable=gente.rosi)
    m.proyecto(2, "El de la página", area="CDS", responsable=gente.dueno)
    m.proyecto(3, "Otro tres", area="ACD", responsable=gente.dueno)
    m.proyecto(4, "Cerrado de la página", area="CDS", estado="cerrado", responsable=gente.dueno)
    m.proyecto(5, "Otro cinco", area="IA")
    m.tarea(10, "pendiente", proyecto=2)
    return m


def _todos(m) -> dict:
    return {f["id"]: dict(f) for f in m.con.execute("SELECT * FROM proyectos")}


FECHA_ESCRITA = "2026-10-20"
# Lo que manda un `<input type="datetime-local">`: día Y hora, sin zona.
FECHA_Y_HORA_ESCRITA = "2026-10-20T09:30"


def _escrito(c):
    """Lo que la persona escribe en un campo: texto con el nombre del campo (si
    el nombre está mal, el efecto no aparece) y, en un campo de fecha, una fecha
    REAL (`2026-10-20`, o `2026-10-20T09:30` si pide hora, como las manda el
    navegador): una fecha opcional que se envía vacía no prueba que el campo
    esté atado a la ruta."""
    if c["tipo"] == "date":
        return FECHA_ESCRITA
    if c["tipo"] == "datetime-local":
        return FECHA_Y_HORA_ESCRITA
    return f"Escrito en {c['name']}"


def _otra_opcion(c):
    """La persona CAMBIA la selección: la última opción habilitada que no es la marcada."""
    libres = [o for o in c["opciones"] if not o["disabled"] and not o["selected"]]
    return libres[-1] if libres else None


def _la_marcada(c):
    return next((o for o in c["opciones"] if o["selected"]), None)


def _primera_habilitada(c):
    return next((o for o in c["opciones"] if not o["disabled"]), None)


# vista -> (consulta, acciones POST exactas que tiene que tener, página = proyecto)
# Las escrituras de la tarea (`/proyectos/tarea/...`, `/proyectos/{pid}/tareas`)
# son de `tests/test_escrituras_tarea.py`; acá, solo las del proyecto.
_SOLO_PROYECTO = re.compile(r"/proyectos/(nuevo|\d+/(nombre|area|responsable|estado|cliente|personas))")


def _del_proyecto(formularios):
    return [f for f in formularios if _SOLO_PROYECTO.fullmatch(f["accion"] or "")]


# La ventanita de «+ Proyecto en X» (1-oct-2026): el servidor escribe UNA por
# grupo de la lista de la izquierda en TODA vista, y cada una es un formulario
# `/proyectos/nuevo`. Cuántas: tantas como grupos tiene el mundo de prueba.
_V = ["/proyectos/nuevo"] * len(_pagina.AREAS)

_VISTAS = {
    # Sin `/proyectos/N/area`: el «Mover a» se quitó de esta página (la maqueta no
    # lo tiene; la ruta sigue y se prueba aparte, enviándole el formulario directo).
    # Con `/cliente` y `/personas` (1-oct-2026, E7): el cliente de la cabecera y el
    # renglón de agregar una persona al proyecto son formularios de verdad.
    "abierto": ({"p": 2}, ["/proyectos/2/nombre", "/proyectos/2/responsable",
                           "/proyectos/2/cliente", "/proyectos/2/personas"] + _V),
    "cerrado": ({"p": 4}, ["/proyectos/4/estado", "/proyectos/4/nombre",
                           "/proyectos/4/responsable", "/proyectos/4/cliente",
                           "/proyectos/4/personas"] + _V),
    "confirmar": ({"p": 2, "confirmar": "cerrar"},
                  ["/proyectos/2/estado", "/proyectos/2/nombre",
                   "/proyectos/2/responsable", "/proyectos/2/cliente",
                   "/proyectos/2/personas"] + _V),
    "editar_nombre": ({"p": 2, "editar": "nombre"},
                      ["/proyectos/2/nombre", "/proyectos/2/responsable",
                       "/proyectos/2/cliente", "/proyectos/2/personas"] + _V),
    # La página aparte (`?nuevo=`) es un formulario MÁS, el de siempre.
    "nuevo": ({"nuevo": "CDS"}, ["/proyectos/nuevo"] + _V),
}


@pytest.mark.parametrize("vista", sorted(_VISTAS))
def test_cada_vista_tiene_exactamente_estos_formularios_de_escritura(mundo, gente, monkeypatch, vista):
    consulta, esperadas = _VISTAS[vista]
    m = _mundo_de_formularios(monkeypatch, gente)
    html = ver(m, **consulta)
    assert sorted(f["accion"] for f in _del_proyecto(_formularios_de(html))) == sorted(esperadas)


@pytest.mark.parametrize("vista", sorted(_VISTAS))
def test_cada_formulario_enviado_como_el_navegador_escribe_en_el_proyecto_de_la_pagina(
        mundo, gente, monkeypatch, vista, noco):
    consulta, esperadas = _VISTAS[vista]
    nombres = {n: c for c, n in config.nombres_con_code().items()}
    casos = 0
    for i in range(len(esperadas)):
        m = _mundo_de_formularios(monkeypatch, gente)
        html = ver(m, **consulta)
        assert _problemas_de_html_simple(html) == [], (vista, _problemas_de_html_simple(html))
        form = _del_proyecto(_formularios_de(html))[i]
        clase = form["clase"]
        escoger = _la_marcada
        if clase == "resp":
            escoger = _otra_opcion
        elif clase == "nuevo":
            escoger = _primera_habilitada
        datos = _lo_que_manda_el_navegador(form, _escrito, escoger)
        # Los formularios de Noco se envían con el botón de la coincidencia que se
        # eligió (`noco_id`): la persona hace clic en «Cliente Uno» / «Persona Dos
        # de Noco», que son botones que la búsqueda dibuja.
        if clase == "elegir-persona":
            datos["noco_id"] = "101"
        elif clase == "agregar":
            datos["noco_id"] = "102"
        antes = _todos(m)
        pid = consulta.get("p")
        r = _cliente(config.CHAT_ID_DUENO).post(form["accion"], data=datos, follow_redirects=False)
        despues = _todos(m)
        assert r.status_code == 303, (vista, form["accion"], datos)
        assert r.headers["location"].startswith("/proyectos?") and "error=" not in r.headers["location"], (
            vista, form["accion"], datos, r.headers["location"])
        textos = " ".join(b["texto"] for b in form["botones"])

        if clase == "nuevo":
            nuevos = set(despues) - set(antes)
            assert len(nuevos) == 1 and all(despues[k] == antes[k] for k in antes), (vista, nuevos)
            fila = despues[nuevos.pop()]
            # El grupo es el que ESE formulario trae escondido: cada ventanita lleva
            # el de su grupo, y la página aparte el de `?nuevo=`.
            suyo = next(c["value"] for c in form["campos"] if c["name"] == "area")
            assert (fila["nombre"], fila["area"]) == ("Escrito en nombre", suyo), (vista, fila)
            assert fila["responsable_chat_id"] == nombres["Persona Uno"], (vista, fila)
            casos += 1
            continue

        assert set(despues) == set(antes)
        otros = {k for k in antes if k != pid}
        assert all(despues[k] == antes[k] for k in otros), (
            vista, form["accion"], "cambió OTRO proyecto",
            [k for k in otros if despues[k] != antes[k]])
        fila = despues[pid]
        if clase == "renombrar":
            assert fila["nombre"] == "Escrito en nombre", (vista, fila)
        elif clase == "resp":
            elegido = _otra_opcion(next(c for c in form["campos"] if c["tipo"] == "select"))
            assert fila["responsable_chat_id"] == nombres[_valor(elegido)], (vista, fila)
        elif clase == "elegir-persona":
            assert (fila["cliente_noco_id"], fila["cliente_nombre"]) == (101, "Cliente Uno"), (vista, fila)
        elif clase == "agregar":
            assert fila == antes[pid], (vista, "agregar una persona NO cambia el proyecto")
            filas = [dict(f) for f in m.con.execute("SELECT * FROM participantes")]
            assert len(filas) == 1 and (filas[0]["proyecto_id"], filas[0]["tarea_id"], filas[0]["noco_id"],
                                        filas[0]["nombre"], filas[0]["rol"]) == (
                pid, None, 102, "Persona Dos de Noco", "Escrito en rol"), (vista, filas)
        elif clase == "en-linea" and "Reabrir" in textos:
            assert fila["estado"] == "activo" and antes[pid]["estado"] == "cerrado", (vista, fila)
        elif clase == "en-linea" and "Sí, cerrar" in textos:
            assert fila["estado"] == "cerrado" and antes[pid]["estado"] == "activo", (vista, fila)
        else:
            raise AssertionError(f"formulario sin intención declarada: {clase!r} {form['accion']} {textos!r}")
        casos += 1
    assert casos == len(esperadas)


def test_el_lector_de_formularios_ve_lo_que_ve_un_navegador():
    """Con HTML inventado: hidden, opción marcada, placeholder deshabilitado, un
    control sin nombre y uno deshabilitado."""
    html = ('<form method="post" action="/x/1" class="a"><input type="hidden" name="h" value="v">'
            '<select name="s"><option value="" selected disabled>Escoge</option>'
            '<option value="u">U</option><option>Texto</option></select>'
            '<input type="text" name="t"><input type="text"><input type="hidden" name="d" value="z" disabled>'
            '<button>Ir</button></form><form method="get" action="/y"></form>')
    forms = _formularios_de(html)
    assert len(forms) == 1 and forms[0]["accion"] == "/x/1"
    datos = _lo_que_manda_el_navegador(forms[0], lambda c: "escrito", _la_marcada)
    assert datos == {"h": "v", "t": "escrito"}                # el placeholder deshabilitado no viaja
    datos = _lo_que_manda_el_navegador(forms[0], lambda c: "", _otra_opcion)
    assert datos["s"] == "Texto"                               # sin `value`, viaja el texto de la opción
    assert len(_formularios_de(html, solo_post=False)) == 2


# ── La regla de HTML simple: cada vista la cumple y el cubo estricto se alcanza ──

@pytest.mark.parametrize("vista", sorted(_VISTAS))
def test_los_formularios_de_cada_vista_cumplen_la_regla_de_html_simple(mundo, gente, monkeypatch, vista):
    m = _mundo_de_formularios(monkeypatch, gente)
    html = ver(m, **_VISTAS[vista][0])
    assert _problemas_de_html_simple(html) == []
    # Y la regla se aplica a TODO formulario post que haya, sin excepción de vista.
    assert len(_del_proyecto(_formularios_de(html))) == len(_VISTAS[vista][1])


_BUENO = ('<div style="display:contents;--claro:#0f7c74;--oscuro:#3cc0b4"><form class="resp" method="post" '
          'action="/proyectos/2/responsable"><select id="r" name="responsable">'
          '<option value="" selected disabled>Escoge</option><option value="Code">Code</option></select>'
          '<button class="btn-linea">Guardar</button></form></div>')


def test_el_html_simple_bueno_no_da_problemas():
    assert _problemas_de_html_simple(_BUENO) == []
    assert _problemas_de_html_simple(_BUENO.replace("<button class", '<button type="submit" class')) == []


# ═══════════════════════════════════════════════════════════════════════
# El guardado automático: el mismo criterio para TODOS los controles
# ═══════════════════════════════════════════════════════════════════════
#
# LA LISTA SALE DEL HTML: se recorren las vistas de la página (las de este
# archivo y las de `test_escrituras_tarea.py`) y de cada formulario POST se
# sacan sus controles con nombre. Para cada uno se corre el script de verdad
# —con su `action` y su valor de verdad— y se mira si envía su formulario.
#
# LA REGLA, EN UNA LÍNEA: un formulario con UN SOLO campo que la persona llena
# se guarda solo (`data-auto`); con NINGUNO es de un clic (marcar hecha, la ×,
# cerrar), y con VARIOS crea una fila y guardarla a medias sería peor que no
# guardarla (proyecto nuevo, tarea nueva). La regla se COMPRUEBA contra el HTML
# (el conteo de campos), no contra una lista escrita a mano.

def _controles_de_la_pagina(monkeypatch, gente) -> list[dict]:
    """Todos los controles que la persona puede tocar, en todas las vistas:
    `[{vista, accion, auto, tag, name, valor, cuantos}]`, con `cuantos` = los
    campos que la persona llena en ESE formulario."""
    import test_escrituras_tarea as _t
    casos = []

    def recoger(m, consultas):
        for consulta in consultas:
            html = ver(m, **consulta)
            for form in _formularios_de(html):
                de_la_persona = [c for c in form["campos"]
                                 if c["name"] and not c.get("disabled") and c["tipo"] != "hidden"]
                for c in de_la_persona:
                    casos.append({"vista": consulta, "accion": form["accion"],
                                  "auto": "data-auto" in form["atributos"],
                                  "tag": "textarea" if c["tipo"] == "textarea" else c["tipo"],
                                  "name": c["name"], "valor": _valor_del_campo(c),
                                  "cuantos": len(de_la_persona),
                                  "con_sugerencias": bool(form.get("sugerencias"))})

    # Cada mundo se recorre ANTES de armar el siguiente: el doble de la base se
    # instala en `db.pool` y el segundo taparía al primero.
    recoger(_mundo_de_formularios(monkeypatch, gente), [c for c, _ in _VISTAS.values()])
    recoger(_t._mundo(monkeypatch, gente), [c for c, _ in _t._VISTAS_DE_TAREAS.values()])
    return casos


@hay_osascript
def test_hermanos_todos_los_controles_de_la_pagina_siguen_el_mismo_criterio(mundo, gente, monkeypatch):
    casos = _controles_de_la_pagina(monkeypatch, gente)
    assert len(casos) >= 12 and len({c["accion"] for c in casos}) >= 8, casos
    # El conteo de campos por formulario y el `data-auto` que trae el HTML: la
    # regla de arriba, comprobada contra lo que se sirve.
    por_accion: dict[str, set] = {}
    for c in casos:
        por_accion.setdefault(c["accion"], set()).add((c["cuantos"], c["auto"], c["con_sugerencias"]))
    for accion, formas in por_accion.items():
        for cuantos, auto, con_sugerencias in formas:
            # Un formulario con UN solo campo se guarda solo, SALVO el que se envía
            # con el botón de una coincidencia de Noco (agregar una persona: el
            # campo es el «qué hace aquí», y sin elegir a quién guardarlo a medias
            # no tendría sentido).
            assert auto == (cuantos == 1 and not con_sugerencias), (accion, cuantos, auto)
    escenario = (
        "var casos = " + json.dumps(casos) + ";\n"
        "JSON.stringify(casos.map(function (caso) {\n"
        "  var S = sitioDeMentira({accion: caso.accion, auto: caso.auto, edita: false, oculto: false,\n"
        "    campos: [{tag: caso.tag, name: caso.name, valor: caso.valor}]});\n"
        "  var c = S.campos[0];\n"
        "  if (c.tagName === 'SELECT') { c.value = 'otro'; oyentes.change(ev(c)); }\n"
        "  else { c.value = c.defaultValue + 'x'; oyentes.focusout(ev(c)); }\n"
        "  return {accion: caso.accion, tag: c.tagName, auto: caso.auto, enviados: S.form.enviados};\n"
        "}))")
    salida = _correr_en_jxa(_script_de_la_pagina(mundo), escenario)
    for dicho in salida:
        assert dicho["enviados"] == (1 if dicho["auto"] else 0), dicho


def _con(cambio_de, cambio_a, base=_BUENO):
    assert cambio_de in base
    return base.replace(cambio_de, cambio_a, 1)


# Los ataques del testigo, ESCRITOS A MANO: cada uno tiene que dar al menos un problema.
_ATAQUES = {
    "a1_boton_type_button": _con("<button class", '<button type="button" class'),
    "a2_boton_disabled": _con("<button class", "<button disabled class"),
    "a3_form_dentro_de_noscript": "<noscript>" + _BUENO + "</noscript>",
    "a4_form_hidden": _con('<form class="resp"', '<form class="resp" hidden'),
    "a4b_form_con_style": _con('<form class="resp"', '<form class="resp" style="display:none"'),
    "a5_hidden_duplicado_antes": _con('<select id="r"', '<input type="hidden" name="responsable" value="Code"><select id="r"'),
    "a5b_hidden_duplicado_despues": _con("<button class", '<input type="hidden" name="responsable" value="Code"><button class'),
    "a6_enctype": _con('<form class="resp"', '<form class="resp" enctype="text/plain"'),
    "a7_sin_boton": _con('<button class="btn-linea">Guardar</button>', ""),
    "a8_onsubmit": _con('<form class="resp"', '<form class="resp" onsubmit="return false"'),
    "a9_form_que_nace_hidden_dentro_de_noscript": (
        '<noscript><form class="renombrar" hidden method="post" action="/proyectos/2/nombre">'
        '<input type="text" name="nombre" value="x"><button>Guardar</button></form></noscript>'),
    # Lo que el lector no sabe clasificar cae en el cubo estricto.
    "x1_atributo_desconocido": _con("<button class", '<button data-x="1" class'),
    "x2_formaction": _con("<button class", '<button formaction="/otra" class'),
    "x3_form_que_apunta_a_otro": _con('<select id="r"', '<select form="otro" id="r"'),
    "x4_elemento_desconocido": _con("<button class", '<fieldset></fieldset><button class'),
    "x5_input_checkbox": _con("<button class", '<input type="checkbox" name="c"><button class'),
    "x6_input_submit": _con("<button class", '<input type="submit" name="c"><button class'),
    "x7_ancestro_escondido": "<div hidden>" + _BUENO + "</div>",
    "x8_ancestro_con_style": '<div style="display:none">' + _BUENO + "</div>",
    "x9_ancestro_con_onclick": '<div onclick="x()">' + _BUENO + "</div>",
    "x10_form_dentro_de_details": "<details>" + _BUENO + "</details>",
    "x10b_details_con_otra_clase": '<details class="otra">' + _BUENO + "</details>",
    "x13_textarea_con_atributo_raro": _con("<button class", '<textarea name="t" readonly></textarea><button class'),
    "x14_input_date_con_atributo_raro": _con("<button class", '<input type="date" name="d" disabled><button class'),
    "x11_form_dentro_de_form": '<form method="post" action="/a"><button>x</button>' + _BUENO + "</form>",
    # La ventanita de «+ Proyecto» es el ÚNICO `dialog` que se acepta; cualquier
    # otro, o ésta escondida o con un manejador, esconde o apaga el formulario.
    "d1_dialog_con_otra_clase": '<dialog class="otra">' + _BUENO + "</dialog>",
    "d2_dialog_sin_clase": "<dialog>" + _BUENO + "</dialog>",
    "d3_ventanita_hidden": '<dialog class="ventana" hidden>' + _BUENO + "</dialog>",
    "d4_ventanita_con_style": '<dialog class="ventana" style="display:none">' + _BUENO + "</dialog>",
    "d5_ventanita_con_manejador": '<dialog class="ventana" onclose="x()">' + _BUENO + "</dialog>",
    "d6_ventanita_inerte": '<dialog class="ventana" inert>' + _BUENO + "</dialog>",
    "x12_method_distinto_de_post_con_atributos": _con('method="post"', 'method="POST" target="_blank"'),
}


@pytest.mark.parametrize("ataque", sorted(_ATAQUES))
def test_cada_ataque_escrito_a_mano_hace_fallar_la_regla(ataque):
    assert _problemas_de_html_simple(_ATAQUES[ataque]) != [], ataque


def test_la_ventanita_de_proyecto_nuevo_se_acepta():
    assert _problemas_de_html_simple('<dialog class="ventana" aria-label="x">' + _BUENO + "</dialog>") == []


def test_el_details_que_pliega_las_hechas_se_acepta_y_el_textarea_y_la_fecha_tambien():
    plegado = '<details class="plegar">' + _BUENO + "</details>"
    assert _problemas_de_html_simple(plegado) == []
    con_texto = _con("<button class", '<textarea name="t" required maxlength="9" aria-label="x"></textarea>'
                                       '<input type="date" name="d" aria-label="f"><button class')
    assert _problemas_de_html_simple(con_texto) == []


def test_un_ancestro_con_el_style_del_grupo_si_se_acepta_y_otro_no():
    assert _problemas_de_html_simple(_BUENO) == []                       # el de la página
    assert _problemas_de_html_simple(_con("--claro:#0f7c74;--oscuro:#3cc0b4", "--claro:#0f7c74;--oscuro:red")) != []
    assert _problemas_de_html_simple(_con("--claro:#0f7c74;--oscuro:#3cc0b4", "--claro:red;--oscuro:#3cc0b4")) != []
    # El `.grupo` de la lista (sin `display:contents`) también lo lleva, porque la
    # ventanita vive dentro de él.
    assert _problemas_de_html_simple(_con("display:contents;", "")) == []


def test_solo_renombrar_puede_nacer_escondido_y_las_dos_formas_de_mostrarla_existen(mundo, gente, monkeypatch):
    m = _mundo_de_formularios(monkeypatch, gente)
    oculto = re.compile(r"<form\b[^>]*\bhidden\b[^>]*>")
    for vista in sorted(_VISTAS):
        html = ver(m, **_VISTAS[vista][0])
        clases = {re.search(r'class="([^"]*)"', f).group(1) for f in oculto.findall(html)}
        assert clases <= {"renombrar"}, (vista, clases)
    # Sin JavaScript, el servidor dibuja visible el formulario del NOMBRE del
    # proyecto; con JavaScript, el doble clic le quita `hidden`
    # (`test_js_el_doble_clic_en_el_titulo_muestra_el_formulario...`).
    del_nombre = re.compile(r'<form\b[^>]*action="/proyectos/2/nombre"[^>]*>')
    assert " hidden" in del_nombre.search(ver(m, p=2)).group(0)
    assert " hidden" not in del_nombre.search(ver(m, p=2, editar="nombre")).group(0)
