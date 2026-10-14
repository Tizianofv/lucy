"""«La tarea que sigue» en la página de Proyectos (Tiziano, 14-oct-2026).

Al marcar una tarea hecha, ENCIMA de ella sale un recuadro para agregar la que sigue (título, fecha,
responsable —el de la tarea terminada, escogido—, «Agregar tarea siguiente» y «No, nada más»); la
nueva nace en el mismo proyecto, queda anotada de cuál viene (`tareas.deriva_de_id`, la columna de
la tarea derivada de /tareas) y se ve como «↳ sigue de «…»». «De qué se trata» ocupa menos espacio.

Todo corre por las rutas reales (`POST /proyectos/tarea/N/hecha` y `/siguiente`, `GET /proyectos`) y el
SQL de verdad en SQLite (el mismo mundo de `test_escrituras_tarea.py`). NO es un navegador: lo que se
mueve en pantalla, o no, se midió aparte (ver el mapa `la-pagina-de-proyectos.md`).

Correr:  python3 -m pytest tests/test_tarea_siguiente.py -q
"""
from __future__ import annotations

import ast
import re
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_escrituras_proyecto import noco  # noqa: F401  (primero: pone el entorno de prueba)
import config
import db.db as db
from test_escrituras_tarea import (_adonde, foto, huellas, igual_salvo, mt, post, tarea)  # noqa: F401
from test_grupo_ia import _ROOT
from test_pagina_proyectos import _cliente, _dia, gente, mundo, ver, ver_r  # noqa: F401
from test_pagina_proyectos_v19 import _regla  # noqa: F401


# ── Ayudas ─────────────────────────────────────────────────────────────

def _consulta_de(r) -> dict:
    """Los parámetros de la dirección a la que el POST mandó."""
    return dict(parse_qsl(urlsplit(_adonde(r)).query))


def _pagina_a_la_que_manda(mt, r) -> str:
    """La página que se ve al seguir la redirección del POST (con el recibo que el POST dejó)."""
    return ver(mt, **_consulta_de(r))


def _recuadro(html: str) -> str | None:
    m = re.search(r'<div class="sigue".*?</div>\s*(?=<div class="tarea )', html, re.S)
    return m.group(0) if m else None


def _ids_en_orden(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'<div class="tarea [^"]*" id="tarea-(\d+)"', html)]


def _mundo_con(mt, gente):
    """Tres pendientes del proyecto 2, una con responsable, otra sin."""
    mt.tarea(70, "Revisar la mezcla", proyecto=2, vence=_dia(-2), responsable=gente.rosi)
    mt.tarea(71, "Sin dueño", proyecto=2, vence=_dia(3))
    mt.tarea(72, "Llamar al cliente", proyecto=2, vence=_dia(9), responsable=gente.dueno)
    mt.con.commit()


def _nombre_de(chat: int) -> str:
    return next(n for v, n in config.opciones_de_responsable() if v == str(chat) or v == chat)


# ── 1. El recuadro sale al marcar, encima de la tarea, y la lista no se mueve ─────────────────────

def test_marcar_hecha_deja_el_recuadro_encima_de_esa_tarea_y_la_lista_no_se_mueve(mt, gente):
    _mundo_con(mt, gente)
    antes = _ids_en_orden(ver(mt, p=2))
    r = post("/proyectos/tarea/70/hecha")
    assert _consulta_de(r).get("sigue") == "70"
    html = _pagina_a_la_que_manda(mt, r)
    caja = _recuadro(html)
    assert caja is not None, "no salió el recuadro"
    # El recuadro está justo antes de LA tarea 70 (no de otra) ...
    assert re.findall(r'data-tarea="(\d+)"', html[html.index('class="sigue"'):])[0] == "70"
    # ... con su texto, su fecha, su responsable, y sus dos botones.
    assert "Marcaste hecha la de abajo." in caja and "¿Terminar eso deja otra cosa por hacer?" in caja
    assert 'name="titulo"' in caja and 'type="date"' in caja and 'name="responsable"' in caja
    assert "Agregar tarea siguiente" in caja and "No, nada más" in caja
    assert "queda anotado de cuál viene" in caja
    assert 'action="/proyectos/tarea/70/siguiente"' in caja
    # La 70 sigue en su lugar entre las pendientes: el orden de TODA la página es el de antes.
    assert _ids_en_orden(html) == antes
    assert html.count('id="tarea-70"') == 1


def test_el_responsable_de_la_tarea_terminada_viene_escogido(mt, gente):
    _mundo_con(mt, gente)
    html = _pagina_a_la_que_manda(mt, post("/proyectos/tarea/70/hecha"))
    caja = _recuadro(html)
    escogidas = re.findall(r'<option value="([^"]*)"\s*selected', caja)
    assert escogidas == [_nombre_de(gente.rosi)], escogidas
    # Sin responsable: sale escogido «sin responsable» (no el del proyecto, no el primero).
    html = _pagina_a_la_que_manda(mt, post("/proyectos/tarea/71/hecha"))
    assert re.findall(r'<option value="([^"]*)"\s*selected', _recuadro(html)) == [""]


def test_sin_el_recibo_del_post_o_con_otra_tarea_no_hay_recuadro(mt, gente):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    assert _recuadro(ver(mt, p=2, sigue=70)) is None                 # escrito a mano, sin recibo
    assert _recuadro(ver_r(mt, p=2, sigue=72)) is None               # 72 sigue pendiente: no hay "hecha"
    assert _recuadro(ver_r(mt, p=2, sigue=20)) is None               # la 20 es de OTRO proyecto
    assert _recuadro(ver_r(mt, p=2, sigue=999)) is None              # no existe
    assert _recuadro(ver_r(mt, p=2, sigue=70)) is not None           # con recibo y hecha: sí


def test_en_un_proyecto_cerrado_no_hay_recuadro(mt):
    assert _recuadro(ver_r(mt, p=3, sigue=40)) is None


def test_en_solo_ver_no_hay_recuadro_ni_aunque_la_direccion_lo_pida(mt, gente):
    from test_proyectos_solo_ver import cliente
    from _navegador import dar_recibo
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    dar_recibo("/proyectos", p=2, sigue=70)
    r = cliente("ver").get("/proyectos", params={"p": 2, "sigue": 70})
    assert r.status_code == 200
    assert 'class="sigue"' not in r.text and "/siguiente" not in r.text and "Agregar tarea siguiente" not in r.text


def test_las_tareas_sueltas_no_ofrecen_el_recuadro(mt):
    r = post("/proyectos/tarea/30/hecha")                              # suelta de CDS
    assert "sigue" not in _consulta_de(r)
    assert _recuadro(ver_r(mt, g="CDS", sigue=30)) is None


# ── 2. Agregar la que sigue: una sola puerta, el mismo proyecto, de cuál viene ───────────────────

def test_agregar_la_siguiente_nace_en_el_mismo_proyecto_con_lo_escrito_y_anotada_de_cual_viene(mt, gente):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    antes = foto(mt)
    nombre = _nombre_de(gente.dueno)
    r = post("/proyectos/tarea/70/siguiente", {"titulo": "  Mandar la mezcla  ", "vence": "2030-05-17",
                                               "responsable": nombre})
    assert r.status_code == 303
    nueva = int(_consulta_de(r)["tarea_creada"])
    hija = tarea(mt, nueva)
    assert (hija["titulo"], hija["proyecto_id"], hija["area"], hija["responsable_chat_id"],
            hija["deriva_de_id"], hija["estado"]) == ("Mandar la mezcla", 2, None, gente.dueno, 70, "pendiente")
    assert str(hija["vence_en"]).startswith("2030-05-17")
    h = huellas(mt)[-1]
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "crear", "tareas", nueva)
    # Nada más cambió: la madre sigue hecha y tal cual, y no hay otra tarea nueva.
    assert igual_salvo(antes, foto(mt), tareas={nueva}, log_acciones={x["id"] for x in huellas(mt)})
    assert tarea(mt, 70)["estado"] == "hecha"
    # El aviso de siempre de «Tarea creada» y la tarea nueva se ve en la lista.
    html = _pagina_a_la_que_manda(mt, r)
    assert f"Tarea creada (#{nueva})" in html and f'id="tarea-{nueva}"' in html


def test_la_nueva_dice_de_cual_viene_y_es_la_madre_correcta(mt, gente):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    post("/proyectos/tarea/72/hecha")
    r = post("/proyectos/tarea/72/siguiente", {"titulo": "Cobrar", "responsable": ""})
    nueva = int(_consulta_de(r)["tarea_creada"])
    html = ver(mt, p=2)
    fila = html.split(f'id="tarea-{nueva}"', 1)[1].split('id="tarea-', 1)[0]
    assert "↳ sigue de «Llamar al cliente»" in fila and "Revisar la mezcla" not in fila
    # Una tarea que no viene de otra no lleva la línea.
    assert "sigue de" not in html.split('id="tarea-71"', 1)[1].split('id="tarea-', 1)[0]
    # Si la madre ya no está (papelera), se dice sin inventar su título.
    mt.con.execute("UPDATE tareas SET borrado_en = '2026-09-02T00:00:00+00:00' WHERE id = 72")
    mt.con.commit()
    fila = ver(mt, p=2).split(f'id="tarea-{nueva}"', 1)[1].split('id="tarea-', 1)[0]
    assert "sigue de una tarea que ya no está a la vista" in fila and "Llamar al cliente" not in fila


def test_se_puede_agregar_sin_fecha_ni_responsable_y_el_vacio_es_de_verdad_vacio(mt, gente):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    r = post("/proyectos/tarea/70/siguiente", {"titulo": "Solo el título", "vence": "", "responsable": ""})
    hija = tarea(mt, int(_consulta_de(r)["tarea_creada"]))
    assert hija["vence_en"] is None and hija["responsable_chat_id"] is None and hija["deriva_de_id"] == 70


@pytest.mark.parametrize("campos,clave", [
    ({"titulo": "   "}, "tarea_titulo"),
    ({"titulo": "x" * (db.LARGO_TITULO_TAREA + 1)}, "tarea_titulo"),
    ({"titulo": "Vale", "vence": "ayer"}, "tarea_fecha"),
    ({"titulo": "Vale", "vence": "1999-01-01"}, "tarea_fecha"),
    ({"titulo": "Vale", "responsable": "Nadie Conocido"}, "tarea_nueva_responsable"),
])
def test_lo_que_no_vale_no_escribe_nada_y_vuelve_con_el_recuadro_abierto(mt, gente, campos, clave):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    antes = foto(mt)
    r = post("/proyectos/tarea/70/siguiente", campos)
    consulta = _consulta_de(r)
    assert consulta.get("error") == clave and consulta.get("sigue") == "70" and "tarea_creada" not in consulta
    assert foto(mt) == antes
    assert _recuadro(_pagina_a_la_que_manda(mt, r)) is not None      # la persona puede corregir y reintentar


def test_si_la_madre_ya_no_esta_hecha_no_se_escribe_nada_y_no_se_vuelve_a_cerrar(mt, gente):
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    post("/proyectos/tarea/70/reabrir")                                # otra pestaña la reabrió
    antes = foto(mt)
    r = post("/proyectos/tarea/70/siguiente", {"titulo": "Una que sigue"})
    assert _consulta_de(r).get("error") == "tarea_siguiente_no_hecha"
    assert foto(mt) == antes and tarea(mt, 70)["estado"] == "pendiente"
    assert "ya no está hecha" in _pagina_a_la_que_manda(mt, r)


def test_en_un_proyecto_cerrado_no_se_crea_y_se_dice_sin_mentir_sobre_la_marcada(mt):
    antes = foto(mt)
    r = post("/proyectos/tarea/40/siguiente", {"titulo": "No debería entrar"})
    assert _consulta_de(r).get("error") == "tarea_siguiente_cerrado"
    assert foto(mt) == antes
    html = _pagina_a_la_que_manda(mt, r)
    assert "no recibe la tarea que sigue" in html and "NO se marcó hecha" not in html


def test_una_tarea_que_no_existe_no_escribe_nada(mt):
    antes = foto(mt)
    r = post("/proyectos/tarea/999/siguiente", {"titulo": "Huérfana"})
    assert _adonde(r) == "/proyectos?error=tarea" and foto(mt) == antes


def test_sin_sesion_o_en_solo_ver_da_401_y_no_escribe(mt, gente):
    from test_proyectos_solo_ver import cliente
    _mundo_con(mt, gente)
    post("/proyectos/tarea/70/hecha")
    antes = foto(mt)
    for con in ("ver", None):
        r = cliente(con).post("/proyectos/tarea/70/siguiente", data={"titulo": "Colada"}, follow_redirects=False)
        assert r.status_code == 401, con
    assert foto(mt) == antes


# ── 3. Una sola puerta, y los hermanos que marcan ────────────────────────────────────────────────

def _funciones_de_app() -> dict:
    fuente = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    return {f.name: f for f in ast.walk(fuente) if isinstance(f, ast.AsyncFunctionDef)}


def _nombra(fn, nombre: str) -> bool:
    return any(nombre in (getattr(n, "id", None), getattr(n, "attr", None)) for n in ast.walk(fn))


def test_la_siguiente_se_crea_por_la_puerta_de_la_derivada_y_se_lee_como_nueva_tarea():
    """Sale del árbol sintáctico: la ruta escribe SOLO por `cerrar_y_derivar` (la que anota
    `deriva_de_id`) y lee título, fecha y responsable con las mismas piezas que «Nueva tarea»
    (`agregar_tarea_al_proyecto`). Otra puerta que cree tareas no anotaría de cuál viene."""
    fns = _funciones_de_app()
    siguiente, nueva = fns["agregar_tarea_siguiente"], fns["agregar_tarea_al_proyecto"]
    assert _nombra(siguiente, "cerrar_y_derivar") and not _nombra(siguiente, "crear_tarea_desde_el_panel")
    for pieza in ("titulo_de_tarea_que_vale", "_vence_valido", "_responsable_por_nombre", "puede_ser_responsable"):
        assert _nombra(siguiente, pieza) and _nombra(nueva, pieza), pieza
    # y exige que la madre siga hecha (no cierra nada)
    llamada = next(n for n in ast.walk(siguiente) if isinstance(n, ast.Call) and _nombra(n.func, "cerrar_y_derivar"))
    assert any(k.arg == "exige_hecha" and getattr(k.value, "value", None) is True for k in llamada.keywords)


async def test_cerrar_y_derivar_con_exige_hecha_no_cierra_ni_crea_si_la_madre_no_esta_hecha(mt):
    antes = foto(mt)
    asi = await db.cerrar_y_derivar(config.CHAT_ID_DUENO, 10, [{"titulo": "x", "vence_en": None, "area": None,
                                                                "responsable_chat_id": None}], exige_hecha=True)
    assert asi == (False, []) and foto(mt) == antes
    # Sin la marca, el comportamiento de siempre: cierra la madre y crea la hija.
    cerrada, hijas = await db.cerrar_y_derivar(config.CHAT_ID_DUENO, 10, [{
        "titulo": "x", "vence_en": None, "area": None, "responsable_chat_id": None}])
    assert cerrada is True and len(hijas) == 1


def test_todas_las_vias_de_marcar_de_la_pagina_piden_el_recuadro_por_la_misma_ruta(mt, gente):
    """Cada formulario de la página que marca una tarea hecha (sacados de la página PINTADA, en la
    vista del proyecto y en la de tareas sueltas y la de «Sin grupo») va a la ruta `/hecha` y esa
    ruta pide el recuadro SOLO si la tarea es de un proyecto vivo."""
    _mundo_con(mt, gente)
    marcas = []
    for consulta in ({"p": 2}, {"p": 1}, {"g": "CDS"}, {"sin_grupo": 1}, {"p": 2, "t": 72}, {"p": 2, "filtro": "pendientes"}):
        marcas += re.findall(r'<form class="marcar" method="post" action="(/proyectos/tarea/(\d+)/hecha[^"]*)"',
                             ver(mt, **consulta))
    assert len(marcas) >= 5
    vistos = set()
    for accion, tid in marcas:
        if tid in vistos:
            continue
        vistos.add(tid)
        con_proyecto = tarea(mt, int(tid))["proyecto_id"] is not None
        r = _cliente(config.CHAT_ID_DUENO).post(accion.replace("&amp;", "&"), follow_redirects=False)
        assert r.status_code == 303
        assert ("sigue" in _consulta_de(r)) == con_proyecto, (accion, _adonde(r))
        assert _consulta_de(r).get("sigue", tid) == tid


# ── 4. «De qué se trata» más compacto (solo apariencia: la hoja de estilos de la maqueta aprobada) ──

def test_de_que_se_trata_ocupa_menos_descripcion_en_una_linea_y_las_tres_fechas_en_una_fila(mt):
    html = ver(mt, p=2)
    fechas = _regla(html, ".de-que-se-trata .fechas-p")
    assert fechas["display"] == "flex" and fechas["flex-direction"] == "row" and fechas["margin-top"] == "0"
    datos = _regla(html, ".de-que-se-trata .fechas-p dl.datos")
    assert datos["display"] == "flex" and datos["flex-wrap"] == "wrap"
    assert _regla(html, ".de-que-se-trata .fechas-p .botones")["margin-left"] == "auto"      # «Cambiar las fechas» a la derecha
    assert _regla(html, ".de-que-se-trata .fechas-p .btn-linea")["font-size"] == ".76rem"     # pequeño
    assert _regla(html, "section.bloque.de-que-se-trata")["padding"] == ".7rem 1rem"
    assert _regla(html, ".de-que-se-trata .descripcion")["font-size"] == ".92rem"


def test_el_recuadro_y_la_linea_de_sigue_de_usan_los_estilos_de_la_maqueta(mt):
    html = ver(mt, p=2)
    assert _regla(html, ".sigue")["border-left-width"] == "4px"
    assert _regla(html, ".viene-de")["font-size"] == ".74rem"
