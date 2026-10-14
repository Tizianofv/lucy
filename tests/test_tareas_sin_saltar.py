# -*- coding: utf-8 -*-
"""Marcar en las pantallas de TAREAS no mueve lo que la persona está viendo
(Tiziano, 7-oct-2026, sobre «Tareas»: «Si»; el 6-oct había pedido lo mismo para
Proyectos: «cuando seleccione una tarea como realizada la pagina se me mueve»).

DÓNDE SE MARCA ALGO COMO HECHO (sacado de las plantillas y las rutas, no de
memoria): (1) Proyectos, el círculo de cada tarea (`proyectos.html`, ya arreglado el
6-oct); (2) la lista `/tareas`, las casillas `hecha_<id>` con UN solo «Guardar»
(`guardar_tareas`: solo se CIERRA, las ya hechas salen deshabilitadas; no hay
desmarcar); (3) el detalle `/tareas/{id}`, el círculo de cada paso (marca y
desmarca, `marcar_paso`). Ninguna otra pantalla de tareas marca nada.

LA CAUSA, medida en un navegador (servidor local, datos inventados): cada una
mandaba un POST, el servidor contestaba 303 a la página SIN ancla, y el navegador la
cargaba de cero: la ventana quedaba arriba del todo (y 1164 → 0 en el detalle, 1904
→ 0 en la lista).

EL ARREGLO: la MISMA pieza que ya usa Proyectos (`web/plantillas/_marcar_sin_saltar.html`,
con `marcarSinSaltar` y su oyente `alEnviarMarcar`), incluida por las tres pantallas.
El mismo POST, la misma redirección, y con la página que contesta el servidor se
cambian las regiones (`data-region`) conservando el desplazamiento. Una sola copia: lo
vigila el apartado «hermanos» de este archivo.

QUÉ VIGILA ESTE ARCHIVO Y QUÉ NO. Vigila: (1) las rutas de verdad (`POST /tareas`,
`POST /tareas/{id}/pasos/{pid}/hecho`) y la página a la que redirigen: lo que cambia
al marcar (aviso, cuenta, a dónde va la tarea, el paso marcado) y que un guardado que
falla NO se vea como hecho; (2) hermanos: todo lo que marca, sacado de las plantillas
reales, usa la pieza única y es un `form.marcar`, y ninguna pantalla escribe su copia;
(3) las MISMAS prohibiciones del guion en las tres pantallas; (4) el guion de cada
pantalla, corrido en JavaScriptCore con un DOM de mentira: un POST, nada más, pinta
lo que trajo el servidor, cae al envío normal, no reenvía, y la fila de referencia
vuelve a su sitio. NO vigila el desplazamiento real: eso es de un navegador (se
midió a mano; los números van en el reporte del trabajo). No ejercita Postgres."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from starlette.testclient import TestClient

from _tareas_de_prueba import Mundo, galleta, panel
from _sin_saltar import correr, la_pieza_de, lo_prohibido_en_la_pieza, sin_comentarios_de_js, texto_de_la_pieza
from test_escrituras_proyecto import _script_de_la_pagina, hay_osascript
from test_pagina_proyectos import gente, mundo  # noqa: F401

_ROOT = Path(__file__).resolve().parent.parent
PLANTILLAS = _ROOT / "web" / "plantillas"
PIEZA = "_marcar_sin_saltar.html"


@pytest.fixture
def tareas(monkeypatch):
    m = Mundo().instalar(monkeypatch)
    m.cliente = TestClient(panel.app, follow_redirects=False)
    m.cliente.cookies.set(panel.COOKIE, galleta())
    return m


def _avisos(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", a).strip() for a in re.findall(r'<p class="(?:aviso|vacio)[^"]*">(.*?)</p>', html, re.S)]


def _pagina_de(m, r) -> str:
    """La página a la que redirige el servidor: la que el guion pinta tal cual."""
    assert r.status_code == 303, r.text[:200]
    g = m.cliente.get(r.headers["location"])
    assert g.status_code == 200
    return g.text


# ═══════════════════════════════════════════════════════════════════════
# 1. Las rutas de verdad y la página que se ve después
# ═══════════════════════════════════════════════════════════════════════

def test_lista_guardar_cierra_y_la_pagina_que_se_pinta_tiene_un_solo_aviso_y_la_tarea_hecha(tareas):
    """Lo que cambia hoy en `/tareas` al marcar sigue cambiando igual: el aviso
    «Cerrada 1.», la tarea pasa de «pendiente» a «hecha» (deshabilitada y marcada, en
    «Otros estados»), y la cuenta del encabezado del grupo baja en uno. La dirección
    no trae nada que no sea del guardado."""
    antes = tareas.cliente.get("/tareas").text
    n_pendientes = int(re.search(r"Sin fecha · (\d+)", antes).group(1))
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_3": "1", "prev_3": "pendiente"})
    destino = urlsplit(r.headers["location"])
    assert destino.path == "/tareas"
    assert set(dict(parse_qsl(destino.query))) <= {"guardadas", "asignadas", "movidas", "derivadas", "derivadas_ids",
                                                   "sin_cerrar", "responsable"}
    despues = _pagina_de(tareas, r)
    assert [a for a in _avisos(despues) if a.startswith("Cerrada")] == ["Cerrada 1."]
    assert tareas.cerradas == [3]
    assert int(re.search(r"Sin fecha · (\d+)", despues).group(1)) == n_pendientes - 1
    assert re.search(r'name="hecha_3" value="1"\s+checked disabled', despues), "la hecha sale marcada y deshabilitada"
    assert re.search(r"Otros estados · 1", despues)


def test_lista_un_guardado_que_no_escribio_nada_no_se_ve_como_hecho(tareas):
    """Una casilla de una tarea que ya estaba hecha (o que no existe) no escribe y la
    página NO dice «Cerrada»: dice lo que pasó (cero), nunca un éxito."""
    tareas.cliente.post("/tareas", data={"filtro": "", "hecha_3": "1", "prev_3": "pendiente"})
    escritos = tareas.escritos
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_3": "1", "prev_3": "pendiente"})
    assert "guardadas=0" in r.headers["location"]
    assert not [a for a in _avisos(_pagina_de(tareas, r)) if a.startswith("Cerrada")]
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_9999": "1", "prev_9999": "pendiente"})
    assert "guardadas=0" in r.headers["location"]
    assert tareas.escritos == escritos and tareas.cerradas == [3]


def test_lista_el_aviso_de_antes_no_se_pega(tareas):
    """La persona viene de `/tareas?guardadas=3` (guardó tres de verdad) y guarda otra: el
    aviso de la página que se pinta es el del guardado nuevo, no el viejo (la página que
    contesta el servidor es la de la redirección, no la dirección que tenía)."""
    primero = tareas.cliente.post("/tareas", data={"filtro": "", **{f"{k}_{i}": v for i in (1, 2, 3) for k, v in (
        ("hecha", "1"), ("prev", "pendiente"))}})
    vieja = _pagina_de(tareas, primero)
    assert "Cerradas 3." in vieja
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_4": "1", "prev_4": "pendiente"})
    nueva = _pagina_de(tareas, r)
    assert [a for a in _avisos(nueva) if a.startswith("Cerrada")] == ["Cerrada 1."]


def test_lista_el_filtro_sobrevive_al_guardado(tareas):
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_5": "1", "prev_5": "pendiente"})
    assert "responsable=" not in r.headers["location"]


def test_detalle_marcar_y_desmarcar_un_paso_cambia_solo_el_paso_y_no_deja_aviso(tareas):
    antes = tareas.cliente.get("/tareas/1").text
    assert "☑" not in antes and "Pasos · 0 de 12" in antes
    r = tareas.cliente.post("/tareas/1/pasos/101/hecho", data={"hecho": "1"})
    assert r.headers["location"] == "/tareas/1"
    despues = _pagina_de(tareas, r)
    assert despues.count("☑") == 1 and "Pasos · 1 de 12" in despues
    assert tareas.pasos_escritos == [(101, True)]
    # el círculo marcado ofrece el contrario: desmarcar
    assert re.search(r'/pasos/101/hecho"[^>]*>\s*<input type="hidden" name="hecho" value="0"', despues)
    r = tareas.cliente.post("/tareas/1/pasos/101/hecho", data={"hecho": "0"})
    otra = _pagina_de(tareas, r)
    assert "☑" not in otra and "Pasos · 0 de 12" in otra
    assert tareas.pasos_escritos == [(101, True), (101, False)]
    assert not [a for a in _avisos(despues) if "paso" in a.lower() and "agregad" in a.lower()]


def test_detalle_un_paso_que_no_es_de_esa_tarea_no_se_marca_y_la_pagina_lo_dice(tareas):
    tareas.pasos[2] = [{"id": 900, "texto": "ajeno", "hecho": False, "orden": 1}]
    r = tareas.cliente.post("/tareas/1/pasos/900/hecho", data={"hecho": "1"})
    assert r.headers["location"] == "/tareas/1?error=pasos"
    pagina = _pagina_de(tareas, r)
    assert any(a.startswith("No se guardó") for a in _avisos(pagina)), _avisos(pagina)
    assert "☑" not in pagina and tareas.pasos_escritos == [] and tareas.pasos[2][0]["hecho"] is False


# ═══════════════════════════════════════════════════════════════════════
# 2. Hermanos: todo lo que marca usa la pieza única
# ═══════════════════════════════════════════════════════════════════════

# Lo que MARCA algo como hecho en una plantilla: un formulario cuya ruta termina en
# hecha/reabrir/hecho, o que lleva una casilla `hecha_<id>`. Se saca del TEXTO de cada
# plantilla de `web/plantillas/`; lo que no marca no entra.
DECLARADAS_SIN_LA_PIEZA: dict[str, str] = {}      # plantilla -> por qué marca sin la pieza (hoy ninguna)


def _formularios(texto: str):
    for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", texto, re.S):
        yield m.group(1), m.group(2)


def formularios_que_marcan(texto: str) -> list[str]:
    """Las etiquetas de apertura de los formularios de `texto` que marcan algo como
    hecho. Es un análisis de TEXTO de la plantilla (frontera: no ve un formulario
    que se arme en tiempo de ejecución con JavaScript)."""
    return [etiqueta for etiqueta, cuerpo in _formularios(texto)
            if re.search(r'action="[^"]*/(?:hecha|reabrir|hecho)[^"/]*"', etiqueta)
            or re.search(r'name="hecha_', cuerpo)]


def lo_que_incumple(plantillas: dict[str, str]) -> list[str]:
    """Qué plantillas marcan y no usan la pieza única: sin la inclusión, o con un
    formulario que no es `form.marcar` (el guion no lo engancha), o con su propia
    `function marcarSinSaltar`. Sale de los textos que se le dan."""
    malas = []
    for nombre, texto in plantillas.items():
        if nombre == PIEZA or nombre in DECLARADAS_SIN_LA_PIEZA:
            continue
        marcan = formularios_que_marcan(texto)
        if "function marcarSinSaltar" in texto:
            malas.append(f"{nombre}: lleva su propia copia de la función")
        if not marcan:
            continue
        if f'{{% include "{PIEZA}" %}}' not in texto:
            malas.append(f"{nombre}: marca y no incluye la pieza")
        for etiqueta in marcan:
            if not re.search(r'\bclass="marcar"', etiqueta):
                malas.append(f"{nombre}: un formulario que marca no es form.marcar ({etiqueta.strip()[:60]})")
    return malas


def _plantillas_reales() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(PLANTILLAS.glob("*.html"))}


def test_hermanos_todo_lo_que_marca_usa_la_pieza_unica():
    reales = _plantillas_reales()
    assert lo_que_incumple(reales) == []
    # Y de verdad hay quien marca: si el análisis no viera ninguna, esto no vigilaría nada.
    marcan = sorted(n for n, t in reales.items() if n != PIEZA and formularios_que_marcan(t))
    assert marcan == ["proyectos.html", "tarea_detalle.html", "tareas.html"], marcan
    # la función vive en UN sitio
    donde = [n for n, t in reales.items() if "function marcarSinSaltar" in t]
    assert donde == [PIEZA], donde


@pytest.mark.parametrize("texto,esperado", [
    # una pantalla nueva que marca y no usa la pieza
    ('<form method="post" action="/otra/3/hecha"><button>x</button></form>', 2),
    # incluye la pieza pero el formulario no es form.marcar
    ('{% include "_marcar_sin_saltar.html" %}<form method="post" action="/otra/3/reabrir"><button>x</button></form>', 1),
    # una casilla `hecha_` en un formulario sin la clase
    ('{% include "_marcar_sin_saltar.html" %}<form method="post" action="/x"><input type="checkbox" name="hecha_4"></form>', 1),
    # su propia copia de la función
    ('{% include "_marcar_sin_saltar.html" %}<script>function marcarSinSaltar(form) {}</script>'
     '<form class="marcar" method="post" action="/y/hecho"><button>x</button></form>', 1),
    # bien hecha: no se queja
    ('{% include "_marcar_sin_saltar.html" %}<form class="marcar" method="post" action="/y/hecho"><button>x</button></form>', 0),
    # algo que no marca (comentar): no entra
    ('<form method="post" action="/z/comentar"><button>x</button></form>', 0),
])
def test_hermanos_la_guarda_ve_entradas_inventadas(texto, esperado):
    assert len(lo_que_incumple({"inventada.html": texto})) == esperado


def _scripts_de_tareas(tareas) -> dict[str, str]:
    lista = tareas.cliente.get("/tareas").text
    detalle = tareas.cliente.get("/tareas/1").text
    out = {}
    for nombre, html in (("tareas", lista), ("detalle", detalle)):
        guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        assert len(guiones) == 1, (nombre, len(guiones))
        out[nombre] = guiones[0]
    return out


def test_hermanos_las_tres_paginas_traen_la_misma_funcion_de_la_pieza(tareas, mundo):
    """La pieza que llega a cada navegador es EXACTAMENTE la de la plantilla única, tres veces el
    mismo texto (si mañana alguien la copia y la cambia en una, esto se pone rojo)."""
    guiones = _scripts_de_tareas(tareas)
    proyectos = _script_de_la_pagina(mundo)
    pieza = texto_de_la_pieza()
    assert "function alEnviarMarcar(e)" in pieza
    for nombre, g in {"tareas": guiones["tareas"], "detalle": guiones["detalle"], "proyectos": proyectos}.items():
        assert la_pieza_de(g)[0] == pieza, nombre


def test_cada_pagina_que_marca_pinta_sus_regiones(tareas, mundo):
    """El guion cambia lo que tiene `data-region`: cada pantalla que marca tiene al
    menos una, y las que contesta el servidor tras guardar traen las MISMAS."""
    def regiones(html):
        return re.findall(r'<(?:main|aside)\b[^>]*data-region="([^"]+)"', html)
    lista = tareas.cliente.get("/tareas").text
    detalle = tareas.cliente.get("/tareas/1").text
    assert regiones(lista) == ["cuerpo"] and regiones(detalle) == ["cuerpo"]
    r = tareas.cliente.post("/tareas", data={"filtro": "", "hecha_3": "1", "prev_3": "pendiente"})
    assert regiones(_pagina_de(tareas, r)) == ["cuerpo"]
    r = tareas.cliente.post("/tareas/1/pasos/101/hecho", data={"hecho": "1"})
    assert regiones(_pagina_de(tareas, r)) == ["cuerpo"]


def test_hermanos_las_filas_de_referencia_tienen_id(tareas):
    """El guion devuelve a su sitio una fila `data-ancla` con `id`: las que marcan
    (lista y detalle) las pintan."""
    lista = tareas.cliente.get("/tareas").text
    assert len(re.findall(r'<tr id="tarea-\d+" data-ancla>', lista)) == lista.count('name="hecha_') == 40
    detalle = tareas.cliente.get("/tareas/1").text
    assert len(re.findall(r'<div class="barra" id="paso-\d+" data-ancla', detalle)) == 12


# ═══════════════════════════════════════════════════════════════════════
# 3. Las mismas prohibiciones en las tres pantallas
# ═══════════════════════════════════════════════════════════════════════

def _sin_la_pieza(guion: str) -> str:
    _, resto = la_pieza_de(guion)
    return sin_comentarios_de_js(resto)


# (Las prohibiciones de los guiones propios de la pantalla: lo que no es la pieza no escribe ni pide nada.)
_PROHIBIDO_EN_EL_RESTO = re.compile(      # la misma lista que tenía `lo_prohibido_en` de 46bf55b (con `setTimeout`, `this`, `self/top/...`)
    r"fetch|XMLHttpRequest|sendBeacon|WebSocket|EventSource|localStorage|sessionStorage|\.submit\(|\blocation\b|\.action|"
    r"FormData|innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval|Function\b|import\(|"
    r"(?<![.\w])(self|globalThis|top|parent|frames)\b|\bwindow\s*\[|\bthis\b|Reflect|"
    r"Object\.|\[\s*[\"'`]|setTimeout|setInterval|\.open\(|postMessage|navigator|\.cookie")


def test_las_pantallas_de_tareas_no_hacen_pedidos_fuera_de_la_pieza(tareas):
    """Lo que no es la pieza no escribe ni pide nada por JavaScript: ni `fetch`, ni XMLHttpRequest, ni
    `.submit(`, ni FormData, ni nada de la lista de prohibidos de Proyectos. Solo contadores y
    mostrar/esconder, y el oyente de «enviar» de la pieza."""
    for nombre, g in _scripts_de_tareas(tareas).items():
        resto = _sin_la_pieza(g)
        assert _PROHIBIDO_EN_EL_RESTO.findall(resto) == [], (nombre, _PROHIBIDO_EN_EL_RESTO.findall(resto))


def test_la_pieza_en_las_pantallas_de_tareas_tiene_las_mismas_prohibiciones(tareas):
    for nombre, g in _scripts_de_tareas(tareas).items():
        pieza, _ = la_pieza_de(g)
        assert lo_prohibido_en_la_pieza(pieza) == [], nombre


@pytest.mark.parametrize("meter", [
    'window.location.href = "/otra";', 'window["fetch"]("/borrar", {method: "POST"});', 'fetch ("/otra");',
    'form.action = "/otra";', 'new XMLHttpRequest();', 'form.submit();', 'document.write("x");'])
def test_lo_prohibido_no_se_cuela_en_la_pieza_de_tareas(tareas, meter):
    pieza, _ = la_pieza_de(_scripts_de_tareas(tareas)["tareas"])
    roto = pieza.replace("function irA(url) {", "function irA(url) { if (false) { " + meter + " }", 1)
    assert roto != pieza
    try:
        quedan = lo_prohibido_en_la_pieza(roto)
    except AssertionError:
        return
    assert quedan, meter


def test_el_oyente_de_las_pantallas_de_tareas_es_el_de_la_pieza(tareas):
    """Cada pantalla de Tareas registra UN oyente de «enviar» y es `alEnviarMarcar`, sin lógica propia, y
    NO arranca los oyentes de Proyectos (`iniciarSinSaltos`): ahí solo se marca."""
    for nombre, g in _scripts_de_tareas(tareas).items():
        resto = _sin_la_pieza(g)
        assert re.findall(r'addEventListener\("submit", ([^)]*)\)', resto) == ["alEnviarMarcar"], nombre
        assert "iniciarSinSaltos" not in resto, nombre


# ═══════════════════════════════════════════════════════════════════════
# 4. El guion de cada pantalla, corrido (JavaScriptCore, DOM de mentira)
# ═══════════════════════════════════════════════════════════════════════
# FRONTERA: no es un navegador; no mide pixeles. Comprueba lo que el guion HACE.

def _jxa(guion: str, escenario: str, previo: str = "") -> dict:
    # El navegador de mentira va ANTES del guion: el de la lista corre cosas al cargar (el contador).
    return correr(guion, escenario, previo)


def _marca(accion: str) -> str:
    return 'var f = formulario(%r, "post", []); f.clases = ["marcar"]; __e = enviar(f);' % accion


URL_FINAL = "http://x.test/tareas?hecho=1"


@hay_osascript
@pytest.mark.parametrize("pantalla,ruta", [("tareas", "/tareas"), ("detalle", "/tareas/1/pasos/101/hecho")])
def test_js_un_envio_hace_un_post_y_pinta_lo_que_trajo_el_servidor(tareas, pantalla, ruta):
    g = _scripts_de_tareas(tareas)[pantalla]
    r = _jxa(g, _marca(ruta) + 'responder(0, "PAGINA-DEL-SERVIDOR", {url: %r});' % URL_FINAL)
    assert r["pedidos"] == [["POST", ruta, ""]] and r["prevenido"] is True and r["salidas"] == []
    assert r["izq"] == "PAGINA-DEL-SERVIDOR" and r["cen"] == "PAGINA-DEL-SERVIDOR"
    assert r["izqTop"] == 300 and r["cenTop"] == 1200 and r["ventana"] == [[0, 50]]
    assert r["historia"] == [["replace", None, "/tareas?hecho=1"]]


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
@pytest.mark.parametrize("modo", ["red", "500", "401"])
def test_js_si_el_post_falla_no_se_reenvia_lo_que_pudo_llegar(tareas, pantalla, modo):
    """CAMBIÓ PARA TAREAS el 13-oct-2026 (pedido de la sala: «un guardado se manda UNA vez»): `/tareas` crea
    tareas derivadas, así que un reenvío duplicaría. Sin respuesta o con 5xx se lee un aviso; con 4xx se envía
    el formulario de siempre."""
    g = _scripts_de_tareas(tareas)[pantalla]
    cierre = {"red": "sinRed(0);", "500": 'responder(0, "ERROR", {ok: false, status: 500});',
              "401": 'responder(0, "ENTRAR", {ok: false, status: 401});'}[modo]
    r = _jxa(g, _marca("/tareas") + cierre)
    assert len(r["pedidos"]) == 1 and r["izq"] == "VIEJA" and r["historia"] == [] and r["escritos"] == []
    if modo == "401":
        assert r["salidas"] == [["requestSubmit", "/tareas", None]]
    else:
        assert [x[0] for x in r["salidas"]] == ["aviso"]


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
def test_js_las_pantallas_de_tareas_no_cambian_titulo_ni_details_ni_campos(tareas, pantalla):
    """Q5: lo que la pieza hace solo en Proyectos (título, `<details>` abiertos, campos a medias) NO ocurre en
    Tareas ni en el detalle de una tarea."""
    g = _scripts_de_tareas(tareas)[pantalla]
    esc = ('vivos.centro.detalles = [{open: true}]; paginas["PAG"] = {titulo: "OTRO", detalles: {centro: [{open: false}]}};'
           'var o = campo("text", "x", "a medias", "", "/tareas"); vivos.centro.campos = [o];'
           'var n = campo("text", "x", "", "", "/tareas"); paginas["PAG"].campos = {centro: [n]};'
           + _marca("/tareas") + 'responder(0, "PAG"); salidas.push(["despues", document.title, vivos.centro.detalles[0].open, n.value]);')
    r = _jxa(g, esc)
    assert ["despues", "TITULO-VIEJO", False, ""] in r["salidas"]


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
@pytest.mark.parametrize("modo", ["no-es-proyectos", "importar-falla"])
def test_js_si_guardo_y_no_se_puede_pintar_va_a_la_pagina_sin_reenviar(tareas, pantalla, modo):
    g = _scripts_de_tareas(tareas)[pantalla]
    previo = 'paginas["PAG"] = {regiones: ["cuerpo"]};' if modo == "no-es-proyectos" else "reemplazos.__importar_falla = true;"
    r = _jxa(g, _marca("/tareas") + 'responder(0, "PAG", {url: %r});' % URL_FINAL, previo)
    assert len(r["pedidos"]) == 1 and r["salidas"] == [["ir", URL_FINAL]]


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
def test_js_un_segundo_envio_del_mismo_formulario_no_manda_otro_post(tareas, pantalla):
    g = _scripts_de_tareas(tareas)[pantalla]
    r = _jxa(g, 'var f = formulario("/tareas", "post", []); f.clases = ["marcar"]; enviar(f); __e = enviar(f);')
    assert len(r["pedidos"]) == 1 and r["prevenido"] is True


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
def test_js_un_formulario_que_no_marca_se_envia_como_siempre(tareas, pantalla):
    """El comentario, agregar un paso, subir/bajar/quitar: no son `form.marcar`, el
    guion no los toca (el navegador los envía solo y la página se recarga como hoy)."""
    g = _scripts_de_tareas(tareas)[pantalla]
    r = _jxa(g, 'var f = formulario("/tareas/1/comentar", "post", []); __e = enviar(f);')
    assert r["pedidos"] == [] and r["prevenido"] is False and r["salidas"] == [] and r["izq"] == "VIEJA"


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
def test_js_sin_fetch_en_el_navegador_el_envio_es_el_de_siempre(tareas, pantalla):
    g = _scripts_de_tareas(tareas)[pantalla]
    r = _jxa(g, _marca("/tareas"), previo="window.fetch = undefined;")
    assert r["pedidos"] == [] and r["prevenido"] is False


# La fila de referencia: la primera que se veía arriba, sin contar la tocada ni las
# marcadas, vuelve al mismo sitio de la pantalla. Escenario: A está arriba de lo que
# se ve; B es la tocada; C tiene una casilla marcada para guardar; D es la que se
# elige (top 400). Tras guardar, D queda 120 más abajo (el aviso empujó): hay que
# correr 120.
_ESCENARIO_ANCLA = r"""
var rect = {};   // id -> {antes: [top, bottom], despues: [top, bottom]}
function fila(id, antes, despues, marcada) {
  rect[id] = {antes: antes, despues: despues};
  return {id: id, querySelector: function (s) { return marcada && s === "input:checked:not(:disabled)" ? {} : null; },
    closest: function () { return vivos.centro; },
    getBoundingClientRect: function () { var r = rect[id][__fase]; return {top: r[0], bottom: r[1], height: r[1] - r[0]}; }};
}
var A = fila("tarea-1", [-300, -250], [-300, -250]);
var B = fila("tarea-2", [-40, 10], [-40, 10]);
var C = fila("tarea-3", [120, 170], [120, 170], true);
var D = fila("tarea-4", [400, 450], [520, 570]);
__filas = [A, B, C, D];
reemplazos["tarea-4"] = D;
"""


@hay_osascript
@pytest.mark.parametrize("pantalla", ["tareas", "detalle"])
@pytest.mark.parametrize("region_con_scroll", [True, False])
def test_js_la_fila_de_referencia_vuelve_a_su_sitio(tareas, pantalla, region_con_scroll):
    g = _scripts_de_tareas(tareas)[pantalla]
    esc = (_ESCENARIO_ANCLA + 'paginas["PAG"] = {sinScroll: %s};' % ("false" if region_con_scroll else "true") +
           'var f = formulario("/tareas", "post", []); f.clases = ["marcar"];'
           ' f.closest = function () { return B; }; __e = enviar(f); responder(0, "PAG");')
    r = _jxa(g, esc)
    assert r["pedidos"] and r["salidas"] == []
    if region_con_scroll:
        assert r["cenTop"] == 1200 + 120 and r["ventana"] == [[0, 50]], r
    else:
        assert r["cenTop"] == 1200 and r["ventana"] == [[0, 50], [0, 50 + 120]], r


@hay_osascript
def test_js_los_oyentes_de_la_lista_van_en_el_documento_y_siguen_vivos_tras_cambiar_el_cuerpo(tareas):
    """Al guardar, el guion cambia el cuerpo de la página por el que contesta el
    servidor, y un guion que viva DENTRO del cuerpo cambiado no se vuelve a correr:
    por eso el de la lista vive fuera de `<main>` y sus oyentes de «cambiar» y de
    clic están en el documento, no en el formulario de antes. Aquí el formulario NO
    existe al cargar el guion (`document.querySelector` da nada) y marcar la casilla
    igual abre el renglón de la tarea derivada."""
    g = _scripts_de_tareas(tareas)["tareas"]
    assert "<script" not in re.search(r'<main data-region="cuerpo">(.*?)</main>',
                                      tareas.cliente.get("/tareas").text, re.S).group(1), "el guion quedó dentro del cuerpo"
    from _sin_saltar import correr_en_navegador
    r = correr_en_navegador(g, 'var fila = {hidden: true, querySelector: function () { return null; }};'
                'reemplazos["derivada-3"] = fila;'
                'var casilla = {checked: true, dataset: {abre: "derivada-3"},'
                ' closest: function () { return {}; }};'
                'disparar("change", {target: casilla});'
                'JSON.stringify({abierta: fila.hidden === false, cambio: typeof __oy.change[0], clic: typeof __oy.click[0]})')
    assert r == {"abierta": True, "cambio": "function", "clic": "function"}
