"""La página de proyectos contra la maqueta aprobada, VERSIÓN 19 (1-oct-2026):
el menú, la lista de grupos, la cabecera, las tres columnas y el desplazamiento.

Pedido de Tiziano: «quiero que la versión real sea exactamente igual al
mockup». La maqueta es `disenos/lucy-proyectos/maqueta-v19.html`; lo que acá se
vigila está copiado de ella con su número de línea.

FRONTERA, dicha una vez: NO es un navegador. Se lee el HTML y el CSS que se
sirven, no cómo se pintan; que dos reglas CSS sean las de la maqueta no prueba
que los píxeles lo sean. El script corre con el `document` de mentira de
`tests/test_escrituras_proyecto.py` (JavaScriptCore de macOS), al que acá se le
agregan `documentElement` y `querySelector` solo para el desplazamiento.

LO QUE LA PÁGINA TODAVÍA NO GUARDA y se ve apagado, no como formulario (hace
falta una escritura que no está diseñada): el cliente de la cabecera, las
personas del proyecto y las personas de una tarea. Las pruebas de abajo exigen
que sigan siendo eso —de solo lectura o apagadas, sin formulario ni `name`—:
una promesa de guardar que no se cumple sería el defecto.

Correr:  python3 -m pytest tests/test_pagina_proyectos_v19.py -q
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest  # noqa: F401

from test_escrituras_proyecto import _ARNES, _formularios_de, _problemas_de_html_simple, hay_osascript
from test_pagina_proyectos import AREAS, _dia, gente, mundo, ver  # noqa: F401
import db.db as db
from test_pagina_proyectos_maqueta import _css, _guion_de, _vistas, arbol
import config
import web.menu as menu

def _regla(html: str, selector: str) -> dict:
    """Las declaraciones de TODAS las reglas `selector{...}` de la hoja juntas (la
    última gana, como en la cascada con la misma especificidad)."""
    hallazgos = re.findall(r"(?:^|[};])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", _css(html), re.M)
    assert hallazgos, selector
    junto = {}
    for cuerpo in hallazgos:
        junto.update({k.strip(): v.strip() for k, v in (d.split(":", 1) for d in cuerpo.split(";") if ":" in d)})
    return junto


# Las siete pantallas del menú de la maqueta, en su orden (maqueta-v19.html:180).
MENU_DE_LA_MAQUETA = ["Resumen", "Tareas", "Proyectos", "Sin clasificar", "Movimientos", "Papelera", "Salud"]


def _con_de_todo(mundo):
    mundo.proyecto(1, "Uno", area="CDS", cliente="Colegio", responsable=config.CHAT_ID_DUENO,
                   creado=_dia(-30))
    mundo.proyecto(2, "Sin cliente", area="CDS")
    mundo.proyecto(3, "Cerrado", area="CDS", estado="cerrado", cliente="Otro")
    mundo.proyecto(4, "Del otro", area="ACD")
    mundo.tarea(10, "pendiente", proyecto=1, vence=_dia(-2), responsable=config.CHAT_ID_DUENO)
    mundo.tarea(11, "hecha", proyecto=1, estado="hecha", completado=_dia(-1))
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "un comentario")
    mundo.tarea(30, "suelta", area="CDS")


# ═══════════════════════════════════════════════════════════════════════
# El menú de arriba a la derecha
# ═══════════════════════════════════════════════════════════════════════

def test_el_menu_es_el_de_la_maqueta_con_la_pagina_actual_subrayada(mundo):
    html = ver(mundo)
    raiz = arbol(html)
    barra = raiz.buscar("header", "barra-marca")
    assert len(barra) == 1
    navs = barra[0].buscar("nav", "menu")
    assert len(navs) == 1 and navs[0].attrs["aria-label"] == "Lucy"
    enlaces = navs[0].buscar("a")
    assert [a.todo_el_texto() for a in enlaces] == MENU_DE_LA_MAQUETA
    # Y salen del menú REAL del panel (`base.html`), no de una lista escrita acá.
    assert [(a.attrs["href"], a.todo_el_texto()) for a in enlaces] == [
        (p.ruta, p.nombre) for p in menu.pantallas()]
    actuales = [a.todo_el_texto() for a in enlaces if a.attrs.get("aria-current") == "page"]
    assert actuales == ["Proyectos"]
    assert not raiz.buscar("nav", "panel")                 # el menú viejo se fue
    regla = _regla(html, ".menu")
    assert regla["margin-left"] == "auto" and regla["flex-wrap"] == "wrap" and regla["font-size"] == ".88rem"
    sub = _regla(html, '.menu a[aria-current="page"]')
    assert sub["font-weight"] == "600" and sub["border-bottom-color"] == "var(--marca,#eb2129)"
    assert _regla(html, ".menu a")["border-bottom"] == "2px solid transparent"


# ═══════════════════════════════════════════════════════════════════════
# La lista de grupos
# ═══════════════════════════════════════════════════════════════════════

def test_la_lista_separa_los_proyectos_con_una_raya_y_oscurece_los_no_seleccionados(mundo):
    html = ver(mundo)
    assert _regla(html, ".grupo a.proy")["border-bottom"] == "1px solid var(--linea)"
    assert _regla(html, ".grupo a.proy:last-of-type")["border-bottom"] == "0"
    assert (_regla(html, '.grupo a.proy:not([aria-current="true"])')["background"]
            == "color-mix(in srgb, var(--tinta) 7%, var(--papel))")
    assert (_regla(html, '.grupo a.proy:not([aria-current="true"]):hover')["background"]
            == "color-mix(in srgb, var(--tinta) 3%, var(--papel))")


def test_el_seleccionado_lleva_el_fondo_de_su_color_que_se_degrada_y_acd_en_tres_colores(mundo):
    html = ver(mundo)
    css = _css(html)
    # Todos los grupos: del 30 % de su color al 10 %, hacia la derecha (CDS en rojo, IA en morado).
    degradado = ("linear-gradient(90deg,color-mix(in srgb,var(--color) 30%,var(--papel)),"
                 "color-mix(in srgb,var(--color) 10%,var(--papel)))")
    assert degradado in css.replace("\n", "")
    base = _regla(html, '.grupo a.proy[aria-current="true"]') if css.count('.grupo a.proy[aria-current="true"]{') == 1 else None
    assert base is None or "inset 4px 0 0 var(--color)" in base["box-shadow"]
    assert "box-shadow:inset 4px 0 0 var(--color);font-weight:600" in css
    # ACD: rojo, naranja y amarillo, escrito por su clave (el único con degradado propio).
    acd = _regla(html, '.grupo[data-g="acd"] a.proy[aria-current="true"]')
    assert acd["background"].startswith("linear-gradient(90deg,color-mix(in srgb,#d62828 18%,var(--papel)),"
                                        "color-mix(in srgb,#f77f00 16%,var(--papel)),"
                                        "color-mix(in srgb,#fcbf49 18%,var(--papel)))")
    assert acd["border-image"] == "linear-gradient(180deg,#d62828,#f77f00,#fcbf49) 1"
    assert "linear-gradient(135deg,#d62828,#f77f00,#fcbf49)" in _regla(html, '.grupo[data-g="acd"] h3 i')["background"]


def test_cada_grupo_lleva_su_clave_en_data_g_para_esas_reglas(mundo):
    _con_de_todo(mundo)
    grupos = arbol(ver(mundo)).buscar("div", "grupo")
    assert [g.attrs["data-g"] for g in grupos] == [a["clave"].lower() for a in AREAS] + ["sin-grupo"][:len(grupos) - len(AREAS)]
    for g in grupos:
        assert "gc" in g.clases


def test_el_mas_del_grupo_se_ve_a_la_derecha_del_nombre_con_el_color_del_grupo(mundo):
    html = ver(mundo)
    mas = _regla(html, ".nuevo-proy.mas")
    assert mas["margin-left"] == "auto" and mas["font-size"] == "1.15rem" and mas["font-weight"] == "600"
    assert mas["color"] == "var(--color)" and mas["border-radius"] == "6px"
    assert (_regla(html, ".nuevo-proy.mas:hover")["background"]
            == "color-mix(in srgb, var(--color) 14%, transparent)")


def test_un_proyecto_abierto_sin_cliente_dice_sin_cliente_y_uno_cerrado_no(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=3)
    abiertos = [a for a in arbol(html).buscar("a", "proy") if "cerrado" not in a.clases and "sueltas" not in a.clases]
    subs = {a.attrs["href"]: [s.todo_el_texto() for s in a.buscar("span", "sub")] for a in abiertos}
    assert subs["/proyectos?p=1"][0] == "Colegio" and subs["/proyectos?p=2"][0] == "Sin cliente"
    cerrado = [a for a in arbol(html).buscar("a", "cerrado")][0]
    assert [s.todo_el_texto() for s in cerrado.buscar("span", "sub")] == ["Otro"]
    sin = ver(mundo, p=4)
    cerrados_sin = [a for a in arbol(sin).buscar("a", "cerrado")]
    assert all("Sin cliente" not in a.todo_el_texto() for a in cerrados_sin)


def test_tareas_sin_proyecto_ya_no_va_en_cursiva_y_se_ve_como_un_proyecto_mas(mundo):
    _con_de_todo(mundo)
    html = ver(mundo)
    assert "font-style:italic" not in _css(html)
    sueltas = arbol(html).buscar("a", "sueltas")
    assert sueltas and all(a.clases == ["proy", "sueltas"] for a in sueltas)


# ═══════════════════════════════════════════════════════════════════════
# La cabecera del proyecto
# ═══════════════════════════════════════════════════════════════════════

def test_la_cabecera_pone_el_aviso_de_sin_movimiento_debajo_del_titulo_con_el_texto_de_la_maqueta(mundo):
    mundo.proyecto(1, "Dormido", area="CDS", creado=_dia(-30))
    mundo.proyecto(2, "Activo", area="CDS", creado=_dia(0))
    html = ver(mundo, p=1)
    cabeza = arbol(html).buscar("div", "cabeza")[0]
    orden = [(n.tag, n.clases[0] if n.clases else "") for n in cabeza.hijos if n.tag in ("div", "h1", "form")]
    nombres = [c for _, c in orden]
    # migas, el título (con su formulario de renombrar), el aviso, quiénes, pastillas, avance.
    assert nombres.index("aviso-dormido") > [t for t, _ in orden].index("h1")
    assert nombres.index("aviso-dormido") < nombres.index("quienes") < nombres.index("meta")
    aviso = cabeza.buscar("div", "aviso-dormido")[0].todo_el_texto()
    assert aviso == "Este proyecto lleva 30 días sin movimiento: ni tareas hechas ni comentarios."
    assert not arbol(ver(mundo, p=2)).buscar("div", "aviso-dormido")


def test_cliente_y_responsable_van_en_la_cabecera_como_campos_con_su_etiqueta(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=1)
    quienes = arbol(html).buscar("div", "quienes")[0]
    etiquetas = [l.todo_el_texto().strip() for l in quienes.buscar("label")]
    assert etiquetas[0].startswith("Cliente") and etiquetas[1] == "Responsable"
    cliente = quienes.buscar("input")[0]
    assert cliente.clases == ["campo-quien"] and cliente.attrs["value"] == "Colegio"
    assert cliente.attrs["placeholder"] == "Buscar en Noco… (sin cliente)"
    # E7: escribir en la caja BUSCA en Noco (con JavaScript, ofrece coincidencias; sin él, el
    # formulario GET con su botón «Buscar»), y elegir una coincidencia envía el
    # formulario del cliente. La caja misma NO se envía (el nombre no viaja).
    assert cliente.attrs["name"] == "pq" and cliente.attrs["data-buscar-persona"] == "enviar"
    assert "data-actual" in cliente.attrs and "readonly" not in cliente.attrs
    assert cliente.ancestro("form").attrs["method"] == "get"
    elegir = quienes.buscar("form", "elegir-persona")[0]
    assert elegir.attrs["method"] == "post" and elegir.attrs["action"] == "/proyectos/1/cliente"
    assert [b.todo_el_texto() for b in elegir.buscar("button")] == ["Quitar el cliente"]
    assert elegir.buscar("button")[0].attrs["value"] == "" and elegir.buscar("div", "sugerencias")
    resp = quienes.buscar("select")[0]
    assert resp.attrs["name"] == "responsable" and resp.ancestro("form").attrs["action"] == "/proyectos/1/responsable"
    css = _regla(html, "form.resp select")
    assert css["min-width"] == "12rem" and css["border-radius"] == "8px" and css["background"] == "var(--papel)"
    assert _regla(html, ".campo-quien")["min-width"] == "12rem"
    # Sin cliente no hay «Quitar el cliente».
    sin = arbol(ver(mundo, p=2)).buscar("div", "quienes")[0]
    assert not sin.buscar("form", "elegir-persona")[0].buscar("button")
    assert "data-actual" not in sin.buscar("input")[0].attrs


# ═══════════════════════════════════════════════════════════════════════
# Las tres columnas y el desplazamiento
# ═══════════════════════════════════════════════════════════════════════

def test_la_hoja_tiene_el_reparto_de_la_maqueta(mundo):
    html = ver(mundo)
    env = _regla(html, ".envoltura")
    assert env["grid-template-columns"] == "17rem minmax(0,1fr)" and env["gap"] == "3.5rem"
    assert env["max-width"] == "96rem" and env["padding-block"] == "1.6rem 4rem"
    cuerpo = _regla(html, ".cuerpo")
    assert cuerpo["grid-template-columns"] == "minmax(0,1fr) 17rem" and cuerpo["gap"] == "2rem"
    primera = re.search(r"(?:^|[};])\s*\.lado-der\s*\{([^}]*)\}", _css(html), re.M).group(1)
    assert primera == "position:static"
    css = _css(html).replace("\n", "")
    assert "@media (max-width:1100px){.cuerpo{grid-template-columns:minmax(0,1fr)}}" in css
    tarjeta = _regla(html, ".envoltura > aside")                # la primera regla: la tarjeta
    assert tarjeta["border-radius"] == "16px" and tarjeta["padding"] == "1.2rem 1.1rem"
    # 6-oct-2026 (Tiziano: «la columna se corta en un punto y da la impresion que
    # mas abajo no hay nada»): las columnas NO tienen desplazamiento propio; la
    # página entera baja. Se mira TODA regla de la hoja (también dentro de un
    # `@media`) cuyo selector sea una de las columnas o el panel de personas.
    for regla in re.finditer(r"([^{}]+)\{([^{}]*)\}", _css(html)):
        selector, cuerpo = regla.group(1), regla.group(2)
        if any(c in selector for c in (".envoltura > aside", ".envoltura > main", ".lado-der")):
            for prohibido in ("position:sticky", "max-height", "overflow-y", "overflow:auto", "overflow:scroll",
                              "height:calc(100vh", "overscroll-behavior"):
                assert prohibido not in cuerpo.replace(" ", "") or prohibido == "max-height" and "max-height:none" in cuerpo, (selector, cuerpo)
    assert ("@media (max-width:760px){.envoltura{grid-template-columns:minmax(0,1fr);gap:1.6rem}}") in css
    # Y la barra de arriba (fija, 58 px; hasta ~94 px en dos líneas, medido en el
    # navegador) no tapa el sitio al que llevan los enlaces con ancla (`#tarea-N`).
    pad = re.search(r"(?:^|[};])\s*html\s*\{scroll-padding-top:([\d.]+)rem\}", _css(html), re.M)
    assert pad and float(pad.group(1)) * 16 >= 94, pad


def test_el_proyecto_va_en_tres_columnas_las_tareas_al_centro_y_las_personas_a_la_derecha(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=1)
    main = arbol(html).buscar("main")[0]
    cuerpos = main.buscar("div", "cuerpo")
    assert len(cuerpos) == 1
    centro, lado = cuerpos[0].hijos[0], cuerpos[0].hijos[1]
    assert centro.tag == "div" and centro.clases == ["centro"]
    assert lado.tag == "aside" and lado.clases == ["lado-der"]
    # En el centro: el bloque de tareas y debajo «Cerrar proyecto».
    assert [h2.todo_el_texto() for h2 in centro.buscar("h2")] == ["Tareas"]
    assert [n.clases[:2] for n in centro.buscar("div", "acciones")][-1] == ["acciones", "abajo"]
    assert "Cerrar proyecto" in centro.todo_el_texto()
    # A la derecha: las personas del proyecto, y nada más.
    assert [h2.todo_el_texto() for h2 in lado.buscar("h2")] == ["Personas del proyecto"]
    assert lado.buscar("section", "bloque")[0].buscar("span", "nota")[0].todo_el_texto() == "Quién está metido y qué hace aquí"
    assert "Nadie más todavía." in lado.todo_el_texto()
    assert not lado.buscar("section") or all("Tareas" not in s.todo_el_texto() for s in lado.buscar("section"))
    # Las tareas del centro y las personas: dos columnas del MISMO nivel dentro del cuerpo.
    assert centro.padre is lado.padre


def test_las_personas_del_proyecto_y_las_de_la_tarea_son_formularios_que_se_envian_con_la_coincidencia(mundo):
    """E7: agregar una persona es BUSCARLA en Noco y elegirla. El formulario de
    agregar trae el «qué hace aquí» (obligatorio, con tope) y el lugar de las
    coincidencias; el Id de Noco viaja en el botón de la coincidencia; nada
    manda el nombre ni un número de chat."""
    _con_de_todo(mundo)
    for consulta, accion, donde in (({"p": 1}, "/proyectos/1/personas", "proyecto"),
                                    ({"p": 1, "t": 10}, "/proyectos/tarea/10/personas", "tarea-10")):
        html = ver(mundo, **consulta)
        raiz = arbol(html)
        formas = [f for f in raiz.buscar("form", "agregar") if f.attrs["action"] == accion]
        assert len(formas) == 1, (consulta, accion)
        forma = formas[0]
        rol = [i for i in forma.buscar("input") if i.attrs.get("name") == "rol"]
        assert len(rol) == 1 and "required" in rol[0].attrs and rol[0].attrs["maxlength"] == str(db.LARGO_ROL_PARTICIPANTE)
        assert forma.buscar("div", "sugerencias") and "data-auto" not in forma.attrs
        # La búsqueda sin JavaScript: un GET con su botón, hacia la misma página, y el sitio dicho.
        busca = forma.padre.buscar("form", "buscar-persona")[0]
        assert busca.attrs["method"] == "get" and busca.attrs["action"] == "/proyectos"
        ocultos = {i.attrs["name"]: i.attrs["value"] for i in busca.buscar("input") if i.attrs.get("type") == "hidden"}
        assert ocultos["pdonde"] == donde and ocultos["p"] == "1"
        assert busca.buscar("input")[0].attrs["name"] == "pq" and busca.buscar("button")
        assert _problemas_de_html_simple(html) == []


def test_el_detalle_de_la_tarea_trae_las_personas_el_separador_y_los_comentarios_en_ese_orden(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=1, t=10)
    detalle = arbol(html).buscar("div", "detalle")[0]
    orden = [n.todo_el_texto() for n in detalle.hijos if n.tag == "h4"]
    assert orden == ["Personas de esta tarea", "Comentarios"]
    separador = [n for n in detalle.hijos if n.clases == ["separa"]]
    assert len(separador) == 1
    hijos = [n for n in detalle.hijos if n.tag == "h4" or n.clases == ["separa"]]
    assert [n.todo_el_texto() if n.tag == "h4" else "|" for n in hijos] == ["Personas de esta tarea", "|", "Comentarios"]
    # La única persona que Lucy guarda en una tarea es su responsable, con ese rol.
    personas = detalle.buscar("div", "personas")[0]
    tarjeta = personas.buscar("div", "persona")
    assert len(tarjeta) == 1 and tarjeta[0].buscar("b")[0].todo_el_texto() == "Persona Uno"
    assert tarjeta[0].buscar("span", "quien")[0].buscar("span")[0].todo_el_texto() == "Responsable"
    # Sin responsable: «Nadie asignado.»
    mundo.tarea(12, "sin responsable", proyecto=1)
    vacio = arbol(ver(mundo, p=1, t=12)).buscar("div", "detalle")[0]
    assert "Nadie asignado." in vacio.buscar("div", "personas")[0].todo_el_texto()


def test_la_fila_de_la_tarea_tiene_las_caritas_y_el_coment_de_la_maqueta(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=1)
    fila = [t for t in arbol(html).buscar("div", "tarea") if t.attrs["data-tarea"] == "10"][0]
    assert [c.todo_el_texto() for c in fila.buscar("span", "caritas")[0].buscar("span", "ini")] == ["PU"]
    assert "1 coment." in fila.todo_el_texto()
    # Sin responsable, las caritas siguen ahí (vacías), como en la maqueta.
    mundo.tarea(12, "sin responsable", proyecto=1)
    otra = [t for t in arbol(ver(mundo, p=1)).buscar("div", "tarea") if t.attrs["data-tarea"] == "12"][0]
    assert len(otra.buscar("span", "caritas")) == 1 and not otra.buscar("span", "caritas")[0].buscar("span")
    css = _css(html)
    caritas = _regla(html, ".caritas .ini")
    assert caritas["border"] == "2px solid var(--papel)" and caritas["margin-left"] == "-.35rem"
    assert "text-decoration:underline" not in css                     # el título no se subraya al pasar el ratón


def test_la_marca_de_hecha_se_dibuja_como_la_casilla_de_la_maqueta(mundo):
    _con_de_todo(mundo)
    html = ver(mundo, p=1)
    marca = _regla(html, "form.marcar button.marca")
    assert (marca["width"], marca["height"]) == ("1.1rem", "1.1rem")
    assert marca["border-radius"] == "3px" and marca["border"].startswith("2px solid")
    hecha = _regla(html, "form.marcar button.marca.hecha-b")
    assert hecha["background"] == "var(--color)" and hecha["color"] == "#fff"
    assert marca["color"] == "transparent"                        # el ○ no se ve: es una casilla vacía
    # Sigue siendo un botón que envía su formulario (nunca un checkbox).
    assert 'type="checkbox"' not in html
    assert "button class=\"marca hecha-b\"" in html


# ═══════════════════════════════════════════════════════════════════════
# Con JavaScript: sin botones de más, y el proyecto escogido queda en el centro
# ═══════════════════════════════════════════════════════════════════════

def _selectores_que_esconden(html: str) -> list[str]:
    reglas = re.findall(r"(?:^|[};])\s*((?:\.js [^{]+))\{([^}]*)\}", _css(html), re.M)
    assert len(reglas) == 1 and "display:none" in reglas[0][1]
    return [x.strip() for x in reglas[0][0].split(",")]


def _lo_esconde(selectores: list[str], clase_form: str, clase_boton: str) -> bool:
    for sel in selectores:
        m = re.fullmatch(r"\.js form\.([\w-]+) button(?:\.([\w-]+))?", sel)
        if m and m.group(1) == clase_form and (m.group(2) is None or m.group(2) == clase_boton):
            return True
    return False


def test_con_javascript_cada_campo_que_se_guarda_solo_pierde_su_boton_salvo_comentar(mundo, monkeypatch, gente):
    """Hermanos: los formularios `data-auto` salen del HTML de todas las vistas y
    de cada uno se mira su botón. Solo «Comentar» (el botón de la maqueta) se
    queda; los demás los esconde una regla `.js ...`, que sin JavaScript no
    aplica (el botón sigue ahí y es lo que envía)."""
    import test_escrituras_tarea as _t
    html0 = ver(mundo)
    selectores = _selectores_que_esconden(html0)
    vistos = {}
    mt = _t._mundo(monkeypatch, gente)
    for consulta in [c for c, _ in _t._VISTAS_DE_TAREAS.values()]:
        for f in _formularios_de(ver(mt, **consulta)):
            if "data-auto" in f["atributos"]:
                vistos[f["clase"]] = {b["tipo"] for b in f["botones"]}
    # (Sin `cambiar-grupo` ni `resp-tarea`: se quitaron de la página el 1-oct-2026.)
    # `resp marcar` es el responsable de la tarea (7-oct-2026): guarda sin mover la
    # página (`form.marcar`) y su botón lo esconde la misma regla `form.resp`.
    assert set(vistos) == {"resp", "resp marcar", "renombrar", "comentar"}, vistos
    for clase in sorted(vistos):
        esconde = any(_lo_esconde(selectores, c, "guardar") for c in clase.split())
        assert esconde == (clase != "comentar"), (clase, selectores)
    assert ".js .buscador button" in selectores


@hay_osascript
def test_js_marca_la_pagina_como_con_javascript(mundo):
    r = _correr_con(mundo, "document.documentElement = {className: 'a'};", "JSON.stringify({c: document.documentElement.className})")
    assert r == {"c": "a js"}


def _correr_con(mundo, preparar: str, escenario: str) -> dict:
    """Corre el script de la página con el `document` de mentira MÁS lo que
    `preparar` le agregue antes de que el script arranque."""
    guion = _guion_de(mundo)
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + preparar + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


_LADO = """
var movidos = [];
var window = {oyentes: {}, addEventListener: function (t, f) { this.oyentes[t] = f; }};
var seleccionado = {getBoundingClientRect: function () { return {top: TOPE, height: 40}; }};
var lado = {scrollHeight: ALTO, clientHeight: 400, scrollTop: 100,
  getBoundingClientRect: function () { return {top: 20}; },
  querySelector: function (s) { return s === 'a.proy[aria-current="true"]' ? SELECCIONADO : null; },
  scrollTo: function (o) { movidos.push(o); }};
document.documentElement = {className: ''};
document.querySelector = function (s) { return s === '.envoltura > aside' ? lado : null; };
"""


def _lado(alto: int, tope: int, seleccionado: bool = True) -> str:
    return (_LADO.replace("ALTO", str(alto)).replace("TOPE", str(tope))
            .replace("SELECCIONADO", "seleccionado" if seleccionado else "null"))


@hay_osascript
def test_js_el_proyecto_escogido_se_centra_en_la_columna_de_la_izquierda(mundo):
    """Con la fórmula de la maqueta: arriba de la columna + lo que falta para que
    el escogido quede a media altura."""
    r = _correr_con(mundo, _lado(1000, 520), "JSON.stringify({m: movidos})")
    # 100 + (520 - 20) - (400 - 40) / 2 = 420. SIN `behavior: "smooth"`: medido el
    # 1-oct-2026 en el navegador del panel, un desplazamiento suave pedido mientras la
    # página carga se pierde y la lista se queda arriba (scrollTop 0 con `?p=10`).
    assert r == {"m": [{"top": 420}]}


@hay_osascript
def test_js_al_terminar_de_cargar_vuelve_a_centrar_porque_las_alturas_se_mueven(mundo):
    """Las tipografías llegan después y cambian las alturas: el evento `load` de la
    ventana centra otra vez con las medidas de entonces."""
    r = _correr_con(mundo, _lado(1000, 520),
                    "seleccionado.getBoundingClientRect = function () { return {top: 700, height: 40}; };"
                    "lado.scrollTop = 420; window.oyentes.load();"
                    "JSON.stringify({m: movidos, tipos: Object.keys(window.oyentes)})")
    # 420 + (700 - 20) - 180 = 920
    assert r == {"m": [{"top": 420}, {"top": 920}], "tipos": ["load"]}


@hay_osascript
def test_js_si_no_hay_nada_que_desplazar_o_no_hay_escogido_no_mueve_nada(mundo):
    assert _correr_con(mundo, _lado(300, 520), "JSON.stringify({m: movidos})") == {"m": []}      # cabe entera
    assert _correr_con(mundo, _lado(1000, 520, seleccionado=False), "JSON.stringify({m: movidos})") == {"m": []}


# ═══════════════════════════════════════════════════════════════════════
# El buscador filtra en vivo, como la maqueta (maqueta-v19.html:457, 244-262)
# ═══════════════════════════════════════════════════════════════════════

def _buscable(mundo):
    mundo.proyecto(1, "Cotización de bachatas", area="CDS", cliente="Academia Ñandú")
    mundo.proyecto(2, "Mezcla del EP", area="CDS")
    mundo.proyecto(3, "Cerrado viejo", area="CDS", estado="cerrado", cliente="Academia Ñandú")
    mundo.proyecto(4, "Curso de producción", area="ACD", cliente="Colegio")
    mundo.proyecto(5, "Panel", area="IA")
    mundo.proyecto(6, "Sin grupo ni cliente", area=None)
    mundo.tarea(30, "suelta", area="CDS")
    mundo.tarea(31, "suelta de ACD", area="ACD")


def test_cada_proyecto_de_la_lista_lleva_su_nombre_y_su_cliente_sin_tildes_para_el_buscador(mundo):
    _buscable(mundo)
    anchors = {a.attrs["href"].split("&")[0]: a for a in arbol(ver(mundo)).buscar("a", "proy") if "data-n" in a.attrs}
    assert anchors["/proyectos?p=1"].attrs["data-n"] == "cotizacion de bachatas"
    assert anchors["/proyectos?p=1"].attrs["data-c"] == "academia nandu"
    assert anchors["/proyectos?p=2"].attrs["data-c"] == ""
    assert anchors["/proyectos?p=3"].attrs["data-c"] == "academia nandu"          # los cerrados también
    assert len(anchors) == 6
    # Las «sin proyecto» no llevan esos datos: el buscador no las cuenta como proyectos.
    assert all("data-n" not in a.attrs for a in arbol(ver(mundo)).buscar("a", "sueltas"))


def _dom_de_mentira(html: str) -> str:
    """El JavaScript que arma, con los grupos, los proyectos y los avisos DEL HTML
    servido, los objetos que el guion toca (`querySelectorAll`, `hidden`,
    `dataset`, `getAttribute`...). Los selectores que contestan son los del guion."""
    raiz = arbol(html)
    grupos = []
    for g in raiz.buscar("nav", "grupos")[0].buscar("div", "grupo"):
        def dato(a):
            return {"sueltas": "sueltas" in a.clases, "n": a.attrs.get("data-n"), "c": a.attrs.get("data-c"),
                    "href": a.attrs["href"], "g": g.attrs["data-g"]}
        detalles = [[dato(a) for a in d.buscar("a", "proy")] for d in g.buscar("details", "cerrados")]
        grupos.append({"g": g.attrs["data-g"], "anchors": [dato(a) for a in g.buscar("a", "proy")],
                       "detalles": detalles, "mas": len(g.buscar("a", "nuevo-proy")),
                       "vacios": len(g.buscar("p", "vacio-grupo"))})
    aviso = [p for p in raiz.buscar("p") if "data-sin-resultados" in p.attrs]
    assert len(aviso) == 1
    return ("var DATOS = " + json.dumps(grupos) + "; var AVISO_OCULTO = " + json.dumps("hidden" in aviso[0].attrs) + ";\n"
            """
function nodo(extra) { var n = {hidden: false, _atrs: {}}; for (var k in extra) n[k] = extra[k]; return n; }
var porHref = {};
var grupos = DATOS.map(function (g) {
  var anchors = g.anchors.map(function (a) {
    var n = nodo({dataset: {n: a.n === null ? undefined : a.n, c: a.c === null ? undefined : a.c}, sueltas: a.sueltas,
      classList: {contains: function (c) { return c === "sueltas" ? a.sueltas : c === "proy"; }},
      getAttribute: function (k) { return this._atrs[k]; },
      setAttribute: function (k, v) { this._atrs[k] = v; }});
    n._atrs.href = a.href;
    return n;
  });
  var mas = []; for (var i = 0; i < g.mas; i++) mas.push(nodo({}));
  var vacios = []; for (var j = 0; j < g.vacios; j++) vacios.push(nodo({}));
  var nodos = g.detalles.map(function (d) {
    var primero = g.anchors.findIndex(function (x) { return d.length && x.href === d[0].href; });
    return nodo({querySelectorAll: function (s) { return s === "a.proy" ? anchors.slice(primero, primero + d.length) : []; }});
  });
  return nodo({data: g.g, anchors: anchors, mas: mas, vacios: vacios, detalles: nodos,
    querySelectorAll: function (s) {
      return s === "a.proy" ? anchors : s === "details.cerrados" ? nodos : s === ".nuevo-proy" ? mas
           : s === "p.vacio-grupo" ? vacios : []; }});
});
var aviso = nodo({hidden: AVISO_OCULTO, textContent: ""});
document.querySelectorAll = function (s) { return s === "nav.grupos .grupo" ? grupos : []; };
document.querySelector = function (s) { return s === "nav.grupos [data-sin-resultados]" ? aviso : null; };
var caja = {value: "", closest: function (s) { return s.indexOf("input.buscar") === 0 ? this : null; }};
function estado() {
  return JSON.stringify({
    grupos: grupos.map(function (g) { return {g: g.data, oculto: g.hidden, mas: g.mas.map(function (x) { return x.hidden; }),
      vacios: g.vacios.map(function (x) { return x.hidden; }), detalles: g.detalles.map(function (x) { return x.hidden; }),
      proyectos: g.anchors.map(function (a) { return {href: a._atrs.href, oculto: a.hidden}; })}; }),
    aviso: {oculto: aviso.hidden, texto: aviso.textContent}});
}
""")


def _filtrar_con_el_guion(mundo, escrito: str) -> dict:
    """La lista tal como la deja el guion de la página (de verdad, en JavaScriptCore)
    después de escribir `escrito` en la caja."""
    html = ver(mundo)
    guion = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]
    escenario = (_dom_de_mentira(html) + "caja.value = " + json.dumps(escrito)
                 + "; oyentes.input(ev(caja)); estado()")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


def _lo_que_ve_el_servidor(mundo, escrito: str) -> dict:
    raiz = arbol(ver(mundo, q=escrito))
    nav = raiz.buscar("nav", "grupos")[0]
    avisos = [p for p in nav.buscar("p") if "data-sin-resultados" in p.attrs]
    return {"grupos": [g.attrs["data-g"] for g in nav.buscar("div", "grupo")],
            "proyectos": {a.attrs["href"] for a in nav.buscar("a", "proy")},
            "sin_resultados": not any("hidden" in p.attrs for p in avisos)}


@hay_osascript
@pytest.mark.parametrize("escrito", ["cotiz", "COTIZACION", "ñandú", "nandu", "academia", "col", "zzzz", "p", "mezcla ep"])
def test_js_el_filtro_en_vivo_deja_a_la_vista_lo_mismo_que_el_servidor_con_q(mundo, escrito):
    """Hermanos: el filtro del navegador y el del servidor (`?q=`, el de sin JavaScript)
    tienen que dar la MISMA lista: los mismos proyectos, con los mismos enlaces, los
    mismos grupos y el mismo aviso de «nada»."""
    _buscable(mundo)
    js = _filtrar_con_el_guion(mundo, escrito)
    sv = _lo_que_ve_el_servidor(mundo, escrito)
    visibles = {p["href"] for g in js["grupos"] if not g["oculto"] for p in g["proyectos"] if not p["oculto"]}
    assert visibles == sv["proyectos"], (escrito, visibles, sv["proyectos"])
    assert [g["g"] for g in js["grupos"] if not g["oculto"]] == sv["grupos"], escrito
    assert (not js["aviso"]["oculto"]) == sv["sin_resultados"], escrito
    if sv["sin_resultados"]:
        assert js["aviso"]["texto"] == f"Ningún proyecto ni cliente con «{escrito}»."
    # Mientras se busca no hay «+», ni «sin proyecto», ni «Sin proyectos abiertos».
    for g in js["grupos"]:
        assert all(g["mas"]) and all(g["vacios"]), escrito
        assert all(p["oculto"] for p in g["proyectos"] if "g=" in p["href"] or "sin_grupo=1" in p["href"]), escrito


@hay_osascript
def test_js_con_la_caja_vacia_vuelve_todo_y_sin_el_q_en_los_enlaces(mundo):
    _buscable(mundo)
    html = ver(mundo)
    originales = [a.attrs["href"] for a in arbol(html).buscar("a", "proy")]
    escenario = (_dom_de_mentira(html) + 'caja.value = "cotiz"; oyentes.input(ev(caja)); caja.value = "  ";'
                 " oyentes.input(ev(caja)); estado()")
    guion = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    estado = json.loads(r.stdout.strip())
    assert all(not g["oculto"] and not any(g["mas"]) and not any(g["vacios"]) and not any(g["detalles"])
               for g in estado["grupos"])
    assert [p["href"] for g in estado["grupos"] for p in g["proyectos"]] == originales
    assert all(not p["oculto"] for g in estado["grupos"] for p in g["proyectos"])
    assert estado["aviso"]["oculto"] is True


@hay_osascript
def test_js_enter_en_la_caja_del_buscador_no_recarga_y_en_otro_campo_sigue_igual(mundo):
    _buscable(mundo)
    html = ver(mundo)
    guion = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]
    escenario = (_dom_de_mentira(html) + 'oyentes.keydown(ev(caja, {key: "Enter"})); var a = evitado;'
                 ' oyentes.keydown(ev({closest: function () { return null; }}, {key: "Enter"}));'
                 " JSON.stringify({caja: a, otro: evitado - a, enviados: 0})")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == {"caja": 1, "otro": 0, "enviados": 0}


def test_sin_javascript_el_buscador_sigue_siendo_un_formulario_que_busca_en_el_servidor(mundo):
    _buscable(mundo)
    html = ver(mundo)
    forma = arbol(html).buscar("form", "buscador")[0]
    assert forma.attrs["method"] == "get" and forma.attrs["action"] == "/proyectos"
    assert forma.buscar("input")[0].attrs["name"] == "q" and forma.buscar("button")        # el botón sigue en el HTML
    assert ".js .buscador button" in _selectores_que_esconden(html)                         # y solo con JS se esconde


def test_la_hoja_deja_que_hidden_esconda_de_verdad_lo_que_filtra_el_buscador(mundo):
    """Medido a mano en el navegador del panel: `hidden` pierde contra el `display`
    de `a.proy` si la hoja no lo refuerza. Cada cosa que el guion esconde tiene su
    selector en la regla con `display:none!important`."""
    _buscable(mundo)
    css = _css(ver(mundo)).replace("\n", "")
    m = re.search(r"((?:\.grupo|nav\.grupos)[^{]*\[hidden\][^{]*)\{display:none!important\}", css)
    assert m, "no hay regla que esconda lo `hidden` de la lista"
    selectores = [x.strip() for x in m.group(1).split(",")]
    for pieza in (".grupo[hidden]", ".grupo a[hidden]", ".grupo details[hidden]", ".grupo p[hidden]",
                  ".grupo .nuevo-proy[hidden]", "nav.grupos > p[hidden]"):
        assert pieza in selectores, pieza
