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

import pytest

from test_grupo_ia import _archivos_de_texto, _ROOT       # (pone el entorno de prueba)
from test_pagina_proyectos import (_cliente, _dia, gente, mundo, titulo_de,  # noqa: F401
                                   tareas_en, ver)
import acciones.crud as crud
import config
import db.db as db


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
    assert "Grabación del sencillo" not in ver(mundo)


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


def test_el_formulario_de_proyecto_nuevo_pide_nombre_y_responsable_y_no_el_cliente(mundo, gente):
    html = ver(mundo, nuevo="ACD")
    assert titulo_de(html) == "Proyecto nuevo en ACD"
    assert 'action="/proyectos/nuevo"' in html and '<input type="hidden" name="area" value="ACD">' in html
    for opcion in ("Persona Uno", "Persona Dos", "Code"):
        assert f'<option value="{opcion}">' in html
    assert "sin responsable" not in html.lower()
    assert 'name="cliente' not in html and "Cliente" not in html


def test_cada_grupo_ofrece_su_boton_de_proyecto_nuevo_y_sin_grupo_no(mundo):
    mundo.proyecto(1, "Sin grupo", area=None)
    html = ver(mundo)
    lista = html.split("<aside>", 1)[1].split("</aside>", 1)[0]
    for grupo in ("CDS", "ACD", "IA"):
        assert f'href="/proyectos?nuevo={grupo}">+ Proyecto en {grupo}</a>' in lista
    assert lista.count("+ Proyecto en") == 3
    assert "+ Proyecto en" not in ver(mundo, q="sin").split("<aside>", 1)[1].split("</aside>", 1)[0]


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
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/1/nombre" hidden>', html)
    assert '<noscript><a class="nota" href="/proyectos?p=1&amp;editar=nombre">Cambiar el nombre</a></noscript>' in html
    # Sin JavaScript el servidor dibuja el formulario abierto y esconde el título.
    abierto = ver(mundo, p=1, editar="nombre")
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/1/nombre" >', abierto)
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


# ── El script del doble clic, con JavaScriptCore de macOS ────────────────

def _script_de_la_pagina(mundo) -> str:
    mundo.proyecto(1, "P", area="CDS")
    html = ver(mundo, p=1)
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert len(guiones) == 1
    return guiones[0]


def _correr_en_jxa(script: str, escenario: str) -> dict:
    """Corre `script` con un `document` de mentira y el `escenario` (JS que lo
    ejercita y termina en una expresión JSON)."""
    arnes = """
var oyentes = {};
var campo = {focos: 0, seleccionado: 0, focus: function () { this.focos++; }, select: function () { this.seleccionado++; }};
var form = {hidden: true, querySelector: function () { return campo; }, closest: function (s) { return s === "form.renombrar" ? this : null; }};
var titulo = {hidden: false, closest: function (s) { return s === "h1[data-dbl]" ? this : null; }};
var otro = {closest: function () { return null; }};
var document = {
  addEventListener: function (tipo, f) { oyentes[tipo] = f; },
  querySelector: function (s) { return s === "form.renombrar" ? form : s === "h1[data-dbl]" ? titulo : null; }
};
var evitado = 0;
function ev(destino, extra) { var e = {target: destino, preventDefault: function () { evitado++; }}; for (var k in extra) e[k] = extra[k]; return e; }
""" + script + "\n" + escenario
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", arnes], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


hay_osascript = pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="no hay osascript (JavaScriptCore de macOS): el script del doble clic no se prueba aquí")

_FOTO = "JSON.stringify({titulo: titulo.hidden, form: form.hidden, focos: campo.focos, sel: campo.seleccionado, evitado: evitado})"


@hay_osascript
def test_js_el_doble_clic_en_el_titulo_muestra_el_formulario_y_enfoca_el_campo(mundo):
    r = _correr_en_jxa(_script_de_la_pagina(mundo), 'oyentes.dblclick(ev(titulo));' + _FOTO)
    assert r == {"titulo": True, "form": False, "focos": 1, "sel": 1, "evitado": 1}


@hay_osascript
def test_js_el_doble_clic_en_otra_parte_no_hace_nada(mundo):
    r = _correr_en_jxa(_script_de_la_pagina(mundo), 'oyentes.dblclick(ev(otro));' + _FOTO)
    assert r == {"titulo": False, "form": True, "focos": 0, "sel": 0, "evitado": 0}


@hay_osascript
def test_js_escape_dentro_del_formulario_lo_esconde_y_otra_tecla_no(mundo):
    guion = _script_de_la_pagina(mundo)
    abrir = "oyentes.dblclick(ev(titulo));"
    escape = abrir + 'oyentes.keydown(ev(form, {key: "Escape"}));' + _FOTO
    assert _correr_en_jxa(guion, escape) == {"titulo": False, "form": True, "focos": 1, "sel": 1, "evitado": 1}
    enter = abrir + 'oyentes.keydown(ev(form, {key: "Enter"}));' + _FOTO
    assert _correr_en_jxa(guion, enter)["form"] is False        # sigue abierto: Enter es el envío normal
    fuera = abrir + 'oyentes.keydown(ev(otro, {key: "Escape"}));' + _FOTO
    assert _correr_en_jxa(guion, fuera)["form"] is False


def test_el_script_no_decide_nada_de_negocio(mundo):
    guion = _script_de_la_pagina(mundo)
    codigo = re.sub(r"/\*.*?\*/", "", guion, flags=re.S)
    assert re.search(r"fetch\(|XMLHttpRequest|\.submit\(|FormData|localStorage|eval\(", codigo) is None
    assert codigo.count("addEventListener") == 2


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
    assert re.search(r'value="-?\d+"', html) is None, "una opción viaja con un número de chat"
    assert "Escoge" not in html           # ya tiene responsable: no hay marcador de «falta»


# ═══════════════════════════════════════════════════════════════════════
# Cerrar y reabrir
# ═══════════════════════════════════════════════════════════════════════

def test_el_boton_de_cerrar_va_debajo_al_final_a_la_derecha(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "una", proyecto=1)
    html = ver(mundo, p=1)
    boton = '<a class="btn-linea" href="/proyectos?p=1&amp;confirmar=cerrar">Cerrar proyecto</a>'
    assert html.count(boton) == 1
    assert html.index("+ Agregar tarea a este proyecto") < html.index(boton)
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

def test_el_selector_de_grupo_esta_arriba_junto_al_grupo_con_el_actual_marcado(mundo):
    mundo.proyecto(1, "P", area="ACD")
    html = ver(mundo, p=1)
    migas = html.split('<div class="migas">', 1)[1].split("</div>", 1)[0]
    assert "<b>ACD</b> / Proyecto" in migas and 'action="/proyectos/1/area"' in migas
    assert '<option value="ACD" selected>' in migas and migas.count("<option") == 3


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

def test_el_cliente_no_se_escribe_desde_la_pagina_todavia(mundo):
    mundo.proyecto(1, "P", area="CDS", cliente="Colegio")
    html = ver(mundo, p=1)
    assert "Cliente: <b>Colegio</b>" in html                  # se LEE si lo hay
    assert 'name="cliente' not in html
    llamadas = []
    for archivo in _archivos_de_texto():
        if archivo.suffix == ".py" and archivo.relative_to(_ROOT).parts[0] == "web":
            for n in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Attribute) and n.attr == "poner_cliente":
                    llamadas.append(archivo.name)
    assert llamadas == [], "una ruta del panel llama a poner_cliente: es de E3"


# ═══════════════════════════════════════════════════════════════════════
# Los hermanos: quién más escribe un proyecto
# ═══════════════════════════════════════════════════════════════════════

def _rutas_post_de_proyectos():
    """Cada función `@app.post("/proyectos...")` de `web/app.py`, con lo que
    escribe: las llamadas a `crud.editar("proyectos", ...)` (con su `actor`) y a
    `db.crear_proyecto`. LA LISTA SALE DEL ÁRBOL SINTÁCTICO, no de nombres."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    salida = {}
    for f in arbol.body:
        if not isinstance(f, ast.AsyncFunctionDef):
            continue
        for d in f.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
                    and d.args and isinstance(d.args[0], ast.Constant)
                    and d.args[0].value.startswith("/proyectos")):
                escribe, pide_sesion = [], False
                for n in ast.walk(f):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "editar" and n.args
                            and isinstance(n.args[0], ast.Constant) and n.args[0].value == "proyectos"):
                        actor = [k.value.value for k in n.keywords
                                 if k.arg == "actor" and isinstance(k.value, ast.Constant)]
                        escribe.append(("crud.editar", tuple(actor)))
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "crear_proyecto"):
                        escribe.append(("db.crear_proyecto", ()))
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "puede_entrar"):
                        pide_sesion = True
                salida[f.name] = {"ruta": d.args[0].value, "escribe": escribe, "sesion": pide_sesion}
    return salida


def test_toda_ruta_post_de_proyectos_pide_sesion_y_escribe_por_una_puerta_con_actor_panel():
    rutas = _rutas_post_de_proyectos()
    assert {r["ruta"] for r in rutas.values()} == {
        "/proyectos/nuevo", "/proyectos/{pid}/nombre", "/proyectos/{pid}/area",
        "/proyectos/{pid}/responsable", "/proyectos/{pid}/estado"}
    for nombre, r in rutas.items():
        assert r["sesion"], f"{nombre} no pide sesión"
        assert len(r["escribe"]) == 1, f"{nombre} escribe por {r['escribe']}"
        puerta, actor = r["escribe"][0]
        assert puerta in ("crud.editar", "db.crear_proyecto")
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
    assert escritores["nombre"] == {"convertir_tarea_en_proyecto", "perfil", "crear_proyecto"}
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
