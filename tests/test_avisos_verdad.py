"""Los avisos de las pantallas dicen la verdad (`web/avisos.py`, 7-oct-2026).

EL DEFECTO. Un aviso («Proyecto cerrado», «No se guardó: el monto…», «Restaurado») salía de
un parámetro de la dirección. Escrito a mano, guardado en favoritos o abierto con «atrás» o
recargando, la pantalla decía que algo había pasado cuando no había pasado nada. Medido el
7-oct-2026 sobre `5948720`, con la página real servida por la app y datos inventados:
106 de 123 combinaciones `parámetro=valor` probadas pintaban un aviso sin que nada pasara.

LA PUERTA. Una sola, sin mirar qué dice cada aviso: lo que un POST de verdad deja (un recibo
firmado, de un solo uso, que no viaja en la dirección) es lo único que deja pasar los
parámetros de aviso de un GET; sin él la pantalla sale como si no se hubieran escrito. Cuál
parámetro es de aviso se declara en la firma de cada ruta (`Aviso[...]`, `Navegacion[...]`).

QUÉ VIGILA ESTE ARCHIVO, y de dónde saca cada lista:
  1. CENSO: todo parámetro de toda ruta GET está marcado (se lee de las rutas registradas), y
     las plantillas no leen parámetros que ninguna ruta declare.
  2. DIRECCIÓN A MANO: para cada pantalla con avisos, cada parámetro de aviso con cada valor
     que su plantilla conoce (sacado del texto de la plantilla) más valores genéricos y
     acompañantes, la página es BYTE POR BYTE la que sale sin parámetros.
  3. CON RECIBO SÍ SALE: con el recibo de la propia puerta, cada aviso conocido pinta algo
     distinto (para que el «no sale» de arriba no sea un vacío); los que dependen de un estado
     de la base se quedan fuera por su `and` en la plantilla (sacado del texto) y tienen su
     prueba de estado en `test_borrar_segunda_vuelta.py` y `test_grupos.py`.
  4. LA ACCIÓN DE VERDAD: por cada pantalla, un POST real → el aviso de siempre sale una vez;
     recargar, abrir la dirección otra vez o escribirla a mano en otro navegador, no.
  5. NAVEGACIÓN QUE NO HABLA: ningún parámetro marcado `Navegacion` pinta un aviso de
     acción (una marca equivocada en un aviso nuevo se vería aquí).
  6. LA PUERTA SOLA, con una aplicación mínima: recibo gastado, vencido, de otra ruta, de otro
     valor, falsificado, redirección a otro sitio, banderas de la cookie, alias de navegación,
     tope de recibos, y un aviso inventado (rojo si no se declara; sin salir si se declara).

FRONTERA (lo que esto NO ve): rutas que no sean GET con parámetros; un aviso que viaje por
otro canal que la dirección; que una ruta nueva con avisos tenga mundo de prueba aquí (si no
lo tiene, `test_cada_pantalla_con_avisos_tiene_su_mundo` se pone roja); que un aviso dependa de
un estado que el mundo de prueba no reproduce (SQLite y dobles, no Postgres); un parámetro
marcado `Navegacion` que SÍ es un aviso y cuyo texto no sale en un `<p class="aviso|vacio">`.
"""
import inspect
import re
import time
import types
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient

import test_grupos as tg  # noqa: F401  (pone el entorno antes de importar `config`)
from test_grupos import base  # noqa: F401
from test_pagina_proyectos import _cliente, gente, mundo  # noqa: F401
from test_papelera_de_proyectos import pap  # noqa: F401
from test_borrar_proyecto_y_grupo import _corre, _sembrar
from test_grupo_ia import _ROOT
from _navegador import Navegador, dar_recibo
import _tareas_de_prueba as TP
import config
import db.db as db
import web.app as panel
import web.avisos as avisos
from acciones import crud
from web.avisos import Aviso, AvisoQueElige, Navegacion, PuertaDeAvisos

PLANTILLAS = _ROOT / "web" / "plantillas"
GENERICOS = ("1", "0", "7", "zzz", "Hogar", "5,6")      # valores que ninguna plantilla nombra
# Parámetros de NAVEGACIÓN que a propósito pintan un `<p class="aviso|vacio">`: hablan de la
# PETICIÓN o de lo que hay, no de una acción que pasó (medido el 7-oct-2026 con GENERICOS y los
# valores de las plantillas): `p` («No queda nada pendiente» al mirar un proyecto), `q`
# («Ningún proyecto ni cliente con «x»»), `codigo` («No entendí «x» como código»), `responsable`
# («no es de nadie»), `proyecto` de `/tareas/nueva` («Ese proyecto no existe o ya no está»). Una marca equivocada en un aviso nuevo NO está aquí y pone roja la prueba.
NAVEGACION_QUE_HABLA = {("/proyectos", "p"), ("/proyectos", "q"), ("/movimientos", "codigo"),
                        ("/tareas", "responsable"), ("/tareas/nueva", "proyecto")}
# Parámetros de aviso que SOLO acompañan a otro y no pintan nada por sí solos en un mundo de
# prueba (su texto sale con `hecho=…`, o depende de un estado que el mundo no reproduce); su
# prueba de estado es `test_borrar_segunda_vuelta.py` / `test_grupos.py` / `test_tarea_derivada.py`.
SIN_TEXTO_POR_SI_SOLOS = {
    ("/proyectos", "sala_no"): "agrega una frase a `tarea_creada`",
    ("/proyectos", "grupo"): "nombra el grupo de `hecho=grupo_*`",
    ("/proyectos", "borrado"): "nombra el proyecto de `hecho=proyecto_borrado` (estado de la base)",
    ("/proyectos", "borrada"): "nombra la tarea de `hecho=tarea_borrada` (estado de la base)",
    ("/proyectos", "derivadas"): "agrega ids a `hecho=tarea_hecha`",
    ("/papelera", "id"): "nombra la fila de `hecho=*_restaurad*` (estado de la base)",
    ("/papelera", "hecho"): "`proyecto_restaurado|tarea_restaurada` con su `id`: lo confirma la base (`de_vuelta`)",
    ("/tareas", "derivadas_ids"): "agrega ids a `derivadas`",
    ("/tareas", "sin_cerrar"): "lo confirma la base (`tareas_sin_cerrar_por_proyecto_cerrado`)",
}


# ═══════════════════════════════════════════════════════════════════════
# Lo que se saca de lo real
# ═══════════════════════════════════════════════════════════════════════

def _rutas_get(app):
    return [r for r in app.router.routes
            if "GET" in (getattr(r, "methods", None) or ()) and hasattr(r, "dependant")]


def _ruta(app, plantilla_de_ruta):
    return next(r for r in _rutas_get(app) if r.path == plantilla_de_ruta)


def _plantilla_de(ruta) -> str | None:
    m = re.search(r'"(\w+\.html)"', inspect.getsource(ruta.endpoint))
    return m.group(1) if m else None


def _valores_de(plantilla: str, parametro: str) -> list[str]:
    """Los valores con los que la plantilla compara el parámetro (`error == 'x'`), también si
    primero lo guarda en otra variable (`{% set err = request.query_params.get('error') %}`)."""
    t = (PLANTILLAS / plantilla).read_text(encoding="utf-8")
    nombres = {parametro} | {m.group(1) for m in re.finditer(
        rf"set (\w+) = request\.query_params\.get\('{parametro}'\)", t)}
    vs = {v for n in nombres for v in re.findall(rf"\b{n}\s*==\s*'(\w+)'", t)}
    return sorted(vs)


def _condicionados(plantilla: str, parametro: str) -> set[str]:
    """Los valores cuya condición en la plantilla lleva además un estado (`… == 'x' and …`)."""
    t = (PLANTILLAS / plantilla).read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(rf"\b{parametro}\s*==\s*'(\w+)'\s*and\b", t)}


def textos(html: str) -> list[str]:
    """Los textos de los avisos de la página: cada `<p class="aviso…|vacio…">`."""
    cuerpo = html.split("<main", 1)[-1]
    return sorted({re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m)).strip()
                   for m in re.findall(r'<p class="(?:aviso|vacio)[^"]*"[^>]*>(.*?)</p>', cuerpo, re.S)})


def parametros(ruta) -> dict[str, dict]:
    return avisos.marcas_de(ruta)


def de_aviso(ruta) -> list[str]:
    return sorted(avisos.avisos_de(ruta))


# ═══════════════════════════════════════════════════════════════════════
# Los mundos: una pantalla se prueba con SU base de mentira
# ═══════════════════════════════════════════════════════════════════════

class Escenario:
    def __init__(self, nombre, cliente, rutas, algo=None):
        self.nombre, self.cliente, self.rutas, self.algo = nombre, cliente, rutas, algo


def _mundo_casa(pap):
    _sembrar(pap)
    return Escenario("casa", _cliente(config.CHAT_ID_DUENO),
                     {"/proyectos": "/proyectos", "/papelera": "/papelera"}, pap)


def _mundo_tareas(monkeypatch):
    TP.Mundo().instalar(monkeypatch)
    monkeypatch.setattr(panel.crud, "_chat_del_nombre", crud._chat_del_nombre, raising=False)
    c = Navegador(panel.app)
    c.cookies.set(panel.COOKIE, TP.galleta())
    return Escenario("tareas", c, {"/tareas": "/tareas", "/tareas/1": "/tareas/{tid}",
                                   "/tareas/nueva": "/tareas/nueva"})


class _DineroDeMentira:
    """Lo que `/movimientos`, `/sin-clasificar` y los POST de dinero necesitan de la base."""

    def __init__(self, monkeypatch):
        self.puestas, self.efectivo, self.papelera = [], [], []
        fila = {"id": 7, "fecha": "2026-08-04", "tipo": "gasto", "monto": Decimal("100"),
                "moneda": "DOP", "contraparte": "X", "categoria": "Seguros",
                "referencia": "x", "banco": "bhd"}

        async def movs(*a, **k):
            return [dict(fila)]

        async def lista(*a, **k):
            return ["Seguros"]

        async def poner(mid, cat):
            self.puestas.append((mid, cat))

        async def olvidar(mid):
            return None

        async def efectivo(concepto, monto, categoria, fecha):
            self.efectivo.append(concepto)
            return 31

        async def a_la_papelera(mid, motivo="x"):
            self.papelera.append(mid)
            return True

        for n, f in (("movimientos_filtrados", movs), ("sin_clasificar", movs),
                     ("categorias_usadas", lista), ("bancos_usados", lista),
                     ("poner_categoria", poner), ("olvidar_categoria", olvidar),
                     ("crear_gasto_en_efectivo", efectivo), ("a_la_papelera", a_la_papelera)):
            monkeypatch.setattr(db, n, f)


def _mundo_dinero(monkeypatch):
    m = _DineroDeMentira(monkeypatch)
    c = Navegador(panel.app)
    c.cookies.set(panel.COOKIE, TP.galleta())
    return Escenario("dinero", c, {"/movimientos": "/movimientos",
                                   "/sin-clasificar": "/sin-clasificar"}, m)


@pytest.fixture(params=["casa", "tareas", "dinero"])
def esc(request, monkeypatch):
    if request.param == "casa":
        return _mundo_casa(request.getfixturevalue("pap"))
    return {"tareas": _mundo_tareas, "dinero": _mundo_dinero}[request.param](monkeypatch)


def _todas_las_pantallas_con_avisos():
    return {r.path for r in _rutas_get(panel.app) if de_aviso(r)}


# ═══════════════════════════════════════════════════════════════════════
# 1. El censo: todo parámetro de toda ruta GET está marcado
# ═══════════════════════════════════════════════════════════════════════

def test_todo_parametro_de_toda_ruta_get_esta_marcado_aviso_o_navegacion():
    """Se lee de las rutas registradas de verdad (`route.dependant.query_params`), no de una lista:
    un parámetro nuevo sin `Aviso[...]` ni `Navegacion[...]` pone roja esta prueba."""
    assert avisos.parametros_sin_marca(panel.app) == []
    # y la lectura ve de verdad las rutas con parámetros (no es un vacío)
    rutas = {r.path for r in _rutas_get(panel.app) if parametros(r)}
    assert {"/proyectos", "/tareas", "/tareas/{tid}", "/tareas/nueva", "/papelera", "/movimientos",
            "/sin-clasificar"} <= rutas, rutas


def test_el_censo_ve_un_parametro_sin_marca_y_uno_marcado_no_se_reporta():
    app = FastAPI()

    @app.get("/y")
    async def y(suelto: str = "", bien: Aviso[str] = "", nav: Navegacion[int] = 0):
        return ""

    @app.get("/y/{n}")
    async def yn(n: int, otro: str = ""):
        return ""
    assert avisos.parametros_sin_marca(app) == [("/y", "suelto"), ("/y/{n}", "otro")]


def test_las_plantillas_solo_leen_parametros_que_alguna_ruta_declara():
    """Una plantilla que lee `request.query_params.get('x')` sin que ninguna ruta lo declare es un
    canal de aviso que la puerta no vería."""
    declarados = {n for r in _rutas_get(panel.app) for n in parametros(r)}
    leidos = {}
    for f in PLANTILLAS.glob("*.html"):
        for m in re.finditer(r"query_params(?:\.get\(|\[)['\"](\w+)['\"]", f.read_text(encoding="utf-8")):
            leidos[(f.name, m.group(1))] = True
    assert leidos, "la búsqueda no vio ninguna lectura: ya no sirve"
    assert {k for k in leidos if k[1] not in declarados} == set(), leidos
    # y sin ninguna otra forma de leer la dirección en las plantillas
    for f in PLANTILLAS.glob("*.html"):
        t = f.read_text(encoding="utf-8")
        assert "request.url" not in t and "request.args" not in t, f.name


def test_cada_pantalla_con_avisos_tiene_su_mundo():
    """Si mañana una pantalla nueva declara avisos y esta prueba no sabe armarle una base, no se
    queda sin vigilar en silencio: se pone roja."""
    cubiertas = {tpl for nombre, f in (("casa", _mundo_casa), ("tareas", _mundo_tareas), ("dinero", _mundo_dinero))
                 for tpl in {"casa": {"/proyectos", "/papelera"},
                             "tareas": {"/tareas", "/tareas/{tid}", "/tareas/nueva"},
                             "dinero": {"/movimientos", "/sin-clasificar"}}[nombre]}
    assert _todas_las_pantallas_con_avisos() == cubiertas


# ═══════════════════════════════════════════════════════════════════════
# 2. Dirección escrita a mano: la página es la de siempre
# ═══════════════════════════════════════════════════════════════════════

def _variantes(ruta, p, v):
    """`{p: v}` solo, y con cada otro parámetro de aviso de la ruta puesto a cada valor
    genérico (los avisos que necesitan un acompañante: `hecho` + `grupo`…)."""
    otros = [q for q in de_aviso(ruta) if q != p]
    yield {p: v}
    for g in GENERICOS:
        yield {p: v, **{q: g for q in otros}}


def _esperada_con(esc, url, ruta, consulta, p, elige, limpia):
    """La página que debe salir con `consulta` sin recibo: los parámetros de aviso desaparecen y los que
    además escogen se leen como su navegación (`creado=5` → `p=5`, si `p` no viene)."""
    nav = {}
    for q, v in consulta.items():
        if elige.get(q) and v.isdigit() and elige[q] not in nav:
            nav[elige[q]] = v
    return esc.cliente.get(url, params=nav) if nav else limpia


def sonda_a_mano(esc: Escenario) -> tuple[int, list]:
    """Pide cada (pantalla, parámetro de aviso, valor, acompañantes) SIN recibo y devuelve
    (cuántas combinaciones probó, las que pintaron algo distinto de la página sin parámetros)."""
    probadas, mal = 0, []
    for url, tpl in esc.rutas.items():
        ruta = _ruta(panel.app, tpl)
        plantilla = _plantilla_de(ruta)
        limpia = esc.cliente.get(url)
        assert limpia.status_code == 200, (url, limpia.status_code)
        elige = avisos.avisos_de(ruta)
        for p in de_aviso(ruta):
            valores = sorted(set(_valores_de(plantilla, p)) | set(GENERICOS))
            for v in valores:
                # un parámetro que además ESCOGE (`creado=5` → proyecto 5) deja la página que saldría
                # con ese parámetro de navegación solo; los demás, la página sin parámetros
                esperada = (esc.cliente.get(url, params={elige[p]: v}) if elige[p] and v.isdigit() else limpia)
                for consulta in _variantes(ruta, p, v):
                    r = esc.cliente.get(url, params=consulta)
                    probadas += 1
                    otras = {q: g for q, g in consulta.items() if q != p}
                    # con acompañantes la página esperada es la de los acompañantes que sí eligen
                    esperada_c = esperada if not otras else _esperada_con(esc, url, ruta, consulta, p, elige, limpia)
                    if r.status_code != 200 or r.text != esperada_c.text:
                        mal.append((url, consulta, r.status_code,
                                    [t for t in textos(r.text) if t not in textos(esperada_c.text)]))
    return probadas, mal


def test_una_direccion_escrita_a_mano_no_pinta_ningun_aviso(esc):
    probadas, mal = sonda_a_mano(esc)
    assert probadas >= 15, (esc.nombre, probadas)
    assert mal == [], mal[:5]


# ═══════════════════════════════════════════════════════════════════════
# 3. Con el recibo de la puerta, cada aviso conocido SÍ sale
# ═══════════════════════════════════════════════════════════════════════

def sonda_con_recibo(esc: Escenario) -> dict:
    """{(pantalla, parámetro): [valores que, con recibo, pintan algo distinto de la página limpia]}"""
    vivos = {}
    for url, tpl in esc.rutas.items():
        ruta = _ruta(panel.app, tpl)
        plantilla = _plantilla_de(ruta)
        limpia = esc.cliente.get(url).text
        for p in de_aviso(ruta):
            condicionados = _condicionados(plantilla, p)
            valores = sorted((set(_valores_de(plantilla, p)) - condicionados) | set(GENERICOS))
            vivos[(tpl, p)] = []
            for v in valores:
                dar_recibo(url, **{p: v})
                if esc.cliente.get(url, params={p: v}).text != limpia:
                    vivos[(tpl, p)].append(v)
    return vivos


def test_con_el_recibo_cada_aviso_que_la_plantilla_conoce_se_pinta(esc):
    vivos = sonda_con_recibo(esc)
    muertos = {k for k, v in vivos.items() if not v}
    assert muertos <= set(SIN_TEXTO_POR_SI_SOLOS), (
        f"avisos declarados que ni con recibo pintan nada solos: {muertos - set(SIN_TEXTO_POR_SI_SOLOS)}")
    # y cada valor con nombre en la plantilla (sin estado) se pinta: ninguno quedó fuera
    for url, tpl in esc.rutas.items():
        ruta = _ruta(panel.app, tpl)
        plantilla = _plantilla_de(ruta)
        for p in de_aviso(ruta):
            esperados = set(_valores_de(plantilla, p)) - _condicionados(plantilla, p)
            if (tpl, p) in SIN_TEXTO_POR_SI_SOLOS:
                continue
            assert esperados <= set(vivos[(tpl, p)]), (tpl, p, esperados - set(vivos[(tpl, p)]))


# ═══════════════════════════════════════════════════════════════════════
# 4. La acción de verdad: sale una vez, y no de otra forma
# ═══════════════════════════════════════════════════════════════════════

def _flujo(c, accion, quien="ver el aviso"):
    """Hace el POST sin seguir la redirección, abre su dirección y devuelve
    (dirección, avisos que salieron, avisos al recargar, avisos de un navegador sin recibo)."""
    r = accion()
    assert r.status_code == 303, (r.status_code, r.text[:200])
    destino = r.headers["location"]
    # un navegador distinto (otra cookie, sin recibo) abre la misma dirección
    ajeno = TestClient(panel.app)
    ajeno.cookies.set(panel.COOKIE, TP.galleta())
    a_mano = textos(ajeno.get(destino).text)
    primera = textos(c.get(destino).text)
    recarga = textos(c.get(destino).text)
    return destino, primera, recarga, a_mano


def _sale_una_vez(c, accion, esperado: str, limpia: list[str]):
    destino, primera, recarga, a_mano = _flujo(c, accion)
    nuevos = [t for t in primera if t not in limpia]
    assert any(esperado in t for t in nuevos), (destino, nuevos)
    assert [t for t in recarga if t not in limpia] == [], ("recargar lo repite", destino, recarga)
    assert [t for t in a_mano if t not in limpia] == [], ("sin recibo sale", destino, a_mano)


def test_el_aviso_de_una_accion_hecha_sale_una_vez_en_proyectos_y_papelera(pap):
    esc = _mundo_casa(pap)
    c = esc.cliente
    limpia = textos(c.get("/proyectos?p=1").text)
    # un «hecho»
    _sale_una_vez(c, lambda: c.post("/proyectos/1/estado", data={"estado": "cerrado"}, follow_redirects=False),
                  "Proyecto cerrado", limpia)
    assert pap.con.execute("SELECT estado FROM proyectos WHERE id = 1").fetchone()[0] == "cerrado"
    # un rechazo
    _sale_una_vez(c, lambda: c.post("/proyectos/1/nombre", data={"nombre": ""}, follow_redirects=False),
                  "El nombre no puede quedar vacío", limpia)
    # la Papelera: borrar de verdad y restaurar por la ruta
    _corre(crud.borrar("proyectos", 2, "x", actor="panel"))
    limpia_p = textos(c.get("/papelera").text)
    _sale_una_vez(c, lambda: c.post("/papelera/restaurar", data={"tabla": "proyectos", "id": 2}, follow_redirects=False),
                  "ya está de vuelta", limpia_p)
    _sale_una_vez(c, lambda: c.post("/papelera/restaurar", data={"tabla": "tareas", "id": 9999}, follow_redirects=False),
                  "no se restauró nada", limpia_p)


def test_el_aviso_de_una_accion_hecha_sale_una_vez_en_tareas(monkeypatch):
    esc = _mundo_tareas(monkeypatch)
    c = esc.cliente
    _sale_una_vez(c, lambda: c.post("/tareas", data={"filtro": "", "hecha_4": "1", "prev_4": "pendiente"},
                                    follow_redirects=False), "Cerrada 1.", textos(c.get("/tareas").text))
    _sale_una_vez(c, lambda: c.post("/tareas/1/pasos/900/hecho", data={"hecho": "1"}, follow_redirects=False),
                  "No se guardó: el paso ya no estaba", textos(c.get("/tareas/1").text))
    _sale_una_vez(c, lambda: c.post("/tareas/nueva", data={"titulo": ""}, follow_redirects=False),
                  "Falta el título", textos(c.get("/tareas/nueva").text))


def test_el_aviso_de_una_accion_hecha_sale_una_vez_en_dinero(monkeypatch):
    esc = _mundo_dinero(monkeypatch)
    c, m = esc.cliente, esc.algo
    limpia = textos(c.get("/movimientos").text)
    _sale_una_vez(c, lambda: c.post("/efectivo", data={"concepto": "café", "monto": "-5", "fecha": "2026-08-04",
                                                       "volver": "/movimientos"}, follow_redirects=False),
                  "No se guardó: el monto", limpia)
    _sale_una_vez(c, lambda: c.post("/efectivo", data={"concepto": "café", "monto": "5", "fecha": "2026-08-04",
                                                       "volver": "/movimientos"}, follow_redirects=False),
                  "Gasto en efectivo guardado", limpia)
    _sale_una_vez(c, lambda: c.post("/borrar", data={"movimiento_id": "7", "volver": "/movimientos"},
                                    follow_redirects=False), "A la papelera", limpia)
    assert m.efectivo == ["café"] and m.papelera == [7]
    _sale_una_vez(c, lambda: c.post("/categorias", data={"cat_7": "Seguros", "prev_7": "", "volver": "/sin-clasificar"},
                                    follow_redirects=False), "Guardado 1.", textos(c.get("/sin-clasificar").text))
    assert m.puestas == [(7, "Seguros")]


def test_proyectos_el_aviso_dice_el_estado_de_la_base_que_el_recibo_no_puede_cambiar(pap):
    """Con el recibo bueno puesto, un aviso de estado sigue sin salir si la base lo desmiente (el
    recibo prueba que pasó ALGO, no que el estado que el aviso dice siga siendo cierto)."""
    _sembrar(pap)
    c = _cliente(config.CHAT_ID_DUENO)
    dar_recibo("/proyectos", hecho="proyecto_borrado", borrado=1)
    assert not any("Papelera" in t for t in textos(c.get("/proyectos?hecho=proyecto_borrado&borrado=1").text))


# ═══════════════════════════════════════════════════════════════════════
# 5. La navegación no habla
# ═══════════════════════════════════════════════════════════════════════

def sonda_navegacion(esc: Escenario) -> set:
    """{(pantalla, parámetro de navegación)} que, con algún valor de las plantillas o genérico,
    pintan un aviso (`<p class="aviso|vacio">`) que la página limpia no tiene."""
    habla = set()
    for url, tpl in esc.rutas.items():
        ruta = _ruta(panel.app, tpl)
        plantilla = _plantilla_de(ruta)
        limpia = set(textos(esc.cliente.get(url).text))
        for p, marca in parametros(ruta).items():
            if marca.get("aviso") is not False:
                continue
            for v in sorted(set(_valores_de(plantilla, p)) | set(GENERICOS) | {"cerrar", "nombre", "tarea-10", "cliente"}):
                try:
                    r = esc.cliente.get(url, params={p: v})
                except Exception:       # el mundo de prueba no reproduce ese pedido (pide algo a la base real)
                    continue
                if r.status_code == 200 and set(textos(r.text)) - limpia:
                    habla.add((tpl, p))
    return habla


def test_ningun_parametro_de_navegacion_pinta_un_aviso_de_accion(esc):
    habla = sonda_navegacion(esc)
    assert habla <= NAVEGACION_QUE_HABLA, habla - NAVEGACION_QUE_HABLA


def test_la_sonda_de_navegacion_ve_un_aviso_marcado_por_error_como_navegacion():
    app = _app_minima(marca_hecho=Navegacion)
    assert sonda_navegacion_minima(app) == {("/x/{n}", "hecho")}


# ═══════════════════════════════════════════════════════════════════════
# 6. La puerta sola, con una aplicación mínima
# ═══════════════════════════════════════════════════════════════════════

def _app_minima(marca_hecho=Aviso):
    app = FastAPI()
    app.add_middleware(PuertaDeAvisos)

    @app.get("/x/{n}", response_class=HTMLResponse)
    async def x(n: int, hecho: marca_hecho[str] = "", cuenta: Aviso[int] = 0, p: Navegacion[int] = 0,
                creado: AvisoQueElige[int] = 0):
        return (f'<main><p class="aviso">hecho={hecho} cuenta={cuenta}</p>'
                f'<p>p={p} creado={creado}</p></main>')

    @app.post("/x/{n}/hacer")
    async def hacer(n: int, q: str = "listo"):
        return RedirectResponse(f"/x/{n}?hecho={q}&cuenta=2&p=3", status_code=303)

    @app.post("/afuera")
    async def afuera():
        return RedirectResponse("https://otro.example/x/1?hecho=listo&cuenta=2", status_code=303)

    @app.post("/sin-aviso")
    async def sin_aviso():
        return RedirectResponse("/x/1?p=3", status_code=303)

    @app.post("/nueva-ruta")
    async def nueva_ruta():
        return RedirectResponse("/z?novedad=hola", status_code=303)
    return app


def sonda_navegacion_minima(app) -> set:
    c = TestClient(app)
    limpia = set(textos(c.get("/x/1").text))
    habla = set()
    for r in _rutas_get(app):
        for p, marca in parametros(r).items():
            if marca.get("aviso") is False:
                for v in ("listo", "1", "zzz"):
                    if set(textos(c.get("/x/1", params={p: v}).text)) - limpia:
                        habla.add((r.path, p))
    return habla


def _dice(r) -> str:
    return re.search(r"<p class=\"aviso\">(.*?)</p>", r.text).group(1)


def test_sin_recibo_la_pagina_sale_sin_el_aviso_y_con_su_navegacion():
    c = TestClient(_app_minima())
    r = c.get("/x/1?hecho=listo&cuenta=2&p=3")
    assert _dice(r) == "hecho= cuenta=0" and "p=3" in r.text


def test_el_recibo_de_un_post_hace_salir_el_aviso_una_sola_vez():
    c = TestClient(_app_minima())
    r = c.post("/x/1/hacer", follow_redirects=False)
    assert r.status_code == 303 and "lucy_aviso=" in r.headers["set-cookie"]
    assert _dice(c.get(r.headers["location"])) == "hecho=listo cuenta=2"
    assert _dice(c.get(r.headers["location"])) == "hecho= cuenta=0"                  # recargar


def test_la_pagina_con_un_aviso_gastado_no_se_guarda_en_el_cache_del_navegador():
    """«Atrás» no debe resucitar un aviso: la página que pintó uno sale `no-store`; la normal, no."""
    c = TestClient(_app_minima())
    destino = c.post("/x/1/hacer", follow_redirects=False).headers["location"]
    con_aviso = c.get(destino)
    assert con_aviso.headers.get("cache-control") == "no-store" and "hecho=listo" in con_aviso.text
    assert c.get(destino).headers.get("cache-control") != "no-store"          # ya sin recibo
    assert c.get("/x/1?p=3").headers.get("cache-control") != "no-store"       # una página sin avisos, intacta


def test_el_recibo_vale_para_ese_aviso_en_esa_ruta_y_nada_mas():
    c = TestClient(_app_minima())
    c.post("/x/1/hacer", follow_redirects=False)
    # otra ruta (otro `n`), otro valor, otro parámetro: nada sale, y el recibo SIGUE vivo
    assert _dice(c.get("/x/2?hecho=listo&cuenta=2")) == "hecho= cuenta=0"
    assert _dice(c.get("/x/1?hecho=otro&cuenta=2")) == "hecho= cuenta=0"
    assert _dice(c.get("/x/1?hecho=listo&cuenta=3")) == "hecho= cuenta=0"
    assert _dice(c.get("/x/1?hecho=listo")) == "hecho= cuenta=0"
    assert _dice(c.get("/x/1?hecho=listo&cuenta=2&p=99")) == "hecho=listo cuenta=2"   # la navegación no entra en la firma


def _con_reloj(monkeypatch, segundos):
    """Adelanta SOLO el reloj de la puerta (no el global: el cliente tiraría su propia cookie vencida)."""
    ahora = time.time()
    monkeypatch.setattr(avisos, "time", types.SimpleNamespace(time=lambda: ahora + segundos))


def test_un_recibo_vencido_no_vale(monkeypatch):
    app = _app_minima()
    c = TestClient(app)
    c.post("/x/1/hacer", follow_redirects=False)
    c2 = TestClient(app)
    c2.cookies.set("lucy_aviso", c.cookies.get("lucy_aviso"))          # sin vencimiento propio en el cliente
    _con_reloj(monkeypatch, avisos.VIDA_RECIBO + 1)
    assert _dice(c2.get("/x/1?hecho=listo&cuenta=2")) == "hecho= cuenta=0"


def test_un_recibo_a_punto_de_vencer_todavia_vale(monkeypatch):
    app = _app_minima()
    c = TestClient(app)
    c.post("/x/1/hacer", follow_redirects=False)
    c2 = TestClient(app)
    c2.cookies.set("lucy_aviso", c.cookies.get("lucy_aviso"))
    _con_reloj(monkeypatch, avisos.VIDA_RECIBO - 5)
    assert _dice(c2.get("/x/1?hecho=listo&cuenta=2")) == "hecho=listo cuenta=2"


def test_un_recibo_falsificado_o_firmado_con_otro_secreto_no_vale(monkeypatch):
    app = _app_minima()
    c = TestClient(app)
    c.post("/x/1/hacer", follow_redirects=False)
    bueno = c.cookies.get("lucy_aviso")
    vence, firma = bueno.split(".")
    for malo in (f"{vence}.{'0' * len(firma)}", f"{int(vence) + 50}.{firma}", f"{vence}.", f".{firma}",
                 "basura", f"{vence}.{firma}x"):
        c2 = TestClient(app)
        c2.cookies.set("lucy_aviso", malo)
        assert _dice(c2.get("/x/1?hecho=listo&cuenta=2")) == "hecho= cuenta=0", malo
    # firmado a mano con otro secreto: misma forma, firma distinta
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "otro-secreto")
    c3 = TestClient(app)
    c3.cookies.set("lucy_aviso", bueno)
    assert _dice(c3.get("/x/1?hecho=listo&cuenta=2")) == "hecho= cuenta=0"


def test_dos_pestanas_guardando_a_la_vez_conservan_los_dos_recibos():
    c = TestClient(_app_minima())
    a = c.post("/x/1/hacer?q=uno", follow_redirects=False).headers["location"]
    b = c.post("/x/2/hacer?q=dos", follow_redirects=False).headers["location"]
    assert _dice(c.get(b)) == "hecho=dos cuenta=2"
    assert _dice(c.get(a)) == "hecho=uno cuenta=2"


def test_la_cookie_no_guarda_mas_recibos_que_el_tope():
    c = TestClient(_app_minima())
    for i in range(avisos.MAXIMO_DE_RECIBOS + 4):
        c.post(f"/x/1/hacer?q=v{i}", follow_redirects=False)
    assert len(c.cookies.get("lucy_aviso").split("~")) == avisos.MAXIMO_DE_RECIBOS


def test_una_redireccion_a_otro_sitio_o_sin_aviso_no_deja_recibo():
    c = TestClient(_app_minima())
    for ruta in ("/afuera", "/sin-aviso"):
        r = c.post(ruta, follow_redirects=False)
        assert r.status_code == 303 and "set-cookie" not in r.headers, ruta


def test_la_cookie_es_http_only_de_la_misma_ruta_y_secure_solo_con_https():
    for base_url, secure in (("http://testserver", False), ("https://testserver", True)):
        r = TestClient(_app_minima(), base_url=base_url).post("/x/1/hacer", follow_redirects=False)
        c = r.headers["set-cookie"]
        assert "HttpOnly" in c and "SameSite=lax" in c.replace("Lax", "lax") and "Path=/" in c
        assert ("Secure" in c) is secure, (base_url, c)


def test_un_parametro_que_tambien_escoge_conserva_esa_parte_sin_recibo():
    c = TestClient(_app_minima())
    sin = c.get("/x/1?creado=5")
    assert "p=5 creado=0" in sin.text                       # escoge, no afirma
    assert "p=0 creado=0" in c.get("/x/1").text
    assert "p=7 creado=0" in c.get("/x/1?creado=5&p=7").text    # lo que dice `p` manda


def test_un_aviso_inventado_hoy_sin_marca_se_ve_en_el_censo_y_marcado_no_sale_a_mano():
    app = _app_minima()

    @app.get("/z", response_class=HTMLResponse)
    async def z(novedad: str = ""):                         # un aviso nuevo, sin declarar
        return f'<main><p class="aviso">{novedad}</p></main>'
    assert avisos.parametros_sin_marca(app) == [("/z", "novedad")]

    app2 = _app_minima()

    @app2.get("/z", response_class=HTMLResponse)
    async def z2(novedad: Aviso[str] = ""):                 # el mismo, declarado
        return f'<main><p class="aviso">novedad={novedad}</p></main>'
    assert avisos.parametros_sin_marca(app2) == []
    c = TestClient(app2)
    assert "novedad=hola" not in c.get("/z?novedad=hola").text           # a mano, no
    r = c.post("/nueva-ruta", follow_redirects=False)                    # por la acción, sí
    assert "novedad=hola" in c.get(r.headers["location"]).text
    assert "novedad=hola" not in c.get(r.headers["location"]).text       # y una vez


def test_la_sonda_a_mano_ve_un_aviso_nuevo_que_la_puerta_no_cubre():
    """La sonda de la sección 2, contra una aplicación SIN la puerta: ve el aviso forzado (así que no
    es un vacío)."""
    def armar(con_puerta):
        app = FastAPI()
        if con_puerta:
            app.add_middleware(PuertaDeAvisos)

        @app.get("/x/{n}", response_class=HTMLResponse)
        async def x(n: int, hecho: Aviso[str] = ""):
            return f'<main><p class="aviso">hecho={hecho}</p></main>'
        return TestClient(app)
    c = armar(False)
    assert textos(c.get("/x/1?hecho=listo").text) != textos(c.get("/x/1").text)
    c = armar(True)
    assert textos(c.get("/x/1?hecho=listo").text) == textos(c.get("/x/1").text)


# ═══════════════════════════════════════════════════════════════════════
# Un bug que esta medición encontró: un aviso que salía SIN parámetros
# ═══════════════════════════════════════════════════════════════════════

def test_el_detalle_de_una_tarea_no_dice_que_un_paso_ya_estaba_en_la_punta_si_nadie_lo_movio(monkeypatch):
    """Medido sobre `5948720`: `paso_movido` valía 0 por omisión y la plantilla comparaba `== 0`, así
    que TODA página de tarea decía «Ese paso ya estaba en la punta.» sin que nadie moviera nada. Ahora
    solo sale tras un `mover` de verdad que no cambió nada."""
    esc = _mundo_tareas(monkeypatch)
    assert not any("punta" in t for t in textos(esc.cliente.get("/tareas/1").text))
    dar_recibo("/tareas/1", paso_movido="0")
    assert any("punta" in t for t in textos(esc.cliente.get("/tareas/1?paso_movido=0").text))
    dar_recibo("/tareas/1", paso_movido="1")
    assert not any("punta" in t for t in textos(esc.cliente.get("/tareas/1?paso_movido=1").text))
