"""Parte 2 de «la página de un proyecto, completa» (diseño de Tiziano aprobado el 8-oct-2026): los filtros de las
tareas de un proyecto (Todas / Pendientes / Vencidas / Las mías / Por persona). No escribe en la base.

Todo sobre lo que de verdad sale de `GET /proyectos` (ruta real, plantilla real, base de prueba real `mundo`):
  1. Cada filtro enseña EXACTAMENTE las tareas que cumplen, contra lo que dice `_fila_de_tarea` de cada una
     (`db.pagina_de_proyectos`), con un oráculo escrito aparte en esta prueba.
  2. Un filtro que nadie declaró (o una persona que no existe, o valores con formas raras) no esconde nada y no
     viaja en ningún enlace. Las capas se prueban de a una: `_sufijo_de_filtro`, `_filtro_vigente` y
     `db.tarea_cumple_filtro`.
  3. «Las mías» no se ofrece en solo ver (y escrita a mano allí no filtra); en la casa son las de quien entró.
  4. Marcar (y todo lo que escribe una tarea) con un filtro puesto no lo pierde: los formularios y los enlaces
     hermanos salen de la página pintada, no de una lista tecleada.
  5. Las cifras de «Cómo va» no cambian con el filtro; las otras vistas ignoran el filtro.

FRONTERA, dicha una vez: no es un navegador; se lee el HTML que sale. Los formularios de PROYECTO (nombre, cliente,
responsable, estado, personas del proyecto, nueva tarea) y los enlaces a OTRO proyecto de la lista de la izquierda
NO conservan el filtro a propósito: no son «una tarea», y otro proyecto empieza sin filtro.

Correr:  python3 -m pytest tests/test_filtros_de_tareas.py -q
"""
from __future__ import annotations

import asyncio
import os
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import config
import db.db as db
import web.app as panel
from test_escrituras_proyecto import (noco, _formularios_de, _lo_que_manda_el_navegador,  # noqa: F401
                                      _escrito, _la_marcada, _otra_opcion)
from test_pagina_proyectos import _cliente, _dia, gente, mundo, tareas_en, ver  # noqa: F401
from test_pagina_proyectos_maqueta import arbol
import test_proyectos_solo_ver as sv

SIN_NOMBRE = 777001                      # un chat que nadie nombró: su tarea sale sin responsable


def _sembrar(mundo, gente):
    mundo.proyecto(1, "Uno", area="CDS", responsable=gente.dueno)
    mundo.proyecto(2, "Otro", area="CDS")
    t = mundo.tarea
    t(10, "vencida del dueño", proyecto=1, vence=_dia(-3), responsable=gente.dueno)
    t(11, "futura del dueño", proyecto=1, vence=_dia(5), responsable=gente.dueno)
    t(12, "vencida de rosi", proyecto=1, vence=_dia(-1), responsable=gente.rosi)
    t(13, "sin fecha de rosi", proyecto=1, responsable=gente.rosi)
    t(14, "sin responsable", proyecto=1)
    t(15, "de Code", proyecto=1, vence=_dia(2), responsable=config.CHAT_ID_CODE)
    t(16, "de un chat sin nombre", proyecto=1, vence=_dia(-2), responsable=SIN_NOMBRE)
    t(17, "hecha del dueño", proyecto=1, estado="hecha", completado=_dia(-1), responsable=gente.dueno)
    t(18, "hecha de rosi", proyecto=1, estado="hecha", completado=_dia(-2), responsable=gente.rosi)
    t(19, "descartada de rosi", proyecto=1, estado="descartado", responsable=gente.rosi)
    t(20, "de otro proyecto", proyecto=2, vence=_dia(-1), responsable=gente.dueno)
    t(21, "suelta", area="CDS", responsable=gente.dueno)
    t(22, "suelta sin grupo")
    mundo.con.execute("INSERT INTO comentarios_tarea (id, tarea_id, autor_chat_id, creado_en, texto) "
                      "VALUES (50, 10, ?, '2026-09-01T00:00:00+00:00', 'un comentario')", (gente.dueno,))


def _filas(proyecto=1) -> list[dict]:
    """Las filas de `_fila_de_tarea` del proyecto, tal como las arma `db.pagina_de_proyectos` (lo que la página
    de verdad lee), sin pasar por la plantilla."""
    modelo = asyncio.new_event_loop().run_until_complete(db.pagina_de_proyectos())
    m = modelo["proyectos"][proyecto]
    return m["pendientes"] + m["otras"]


def _ids(html) -> set[int]:
    return set(tareas_en(html))


def _solo_ver(mundo, **consulta):
    r = sv.cliente("ver").get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _como_chat(mundo, chat, **consulta):
    r = _cliente(chat).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


# Lo que cada filtro debe enseñar, escrito AQUÍ y mirando solo los campos de la fila (no llama a lo que prueba).
def _esperado(filas, filtro, quien=None, mi_nombre=None):
    if filtro == "pendientes":
        return {f["id"] for f in filas if f["estado"] == "pendiente"}
    if filtro == "vencidas":
        return {f["id"] for f in filas if f["vencida"]}
    if filtro == "mias":
        return {f["id"] for f in filas if f["responsable"] is not None and f["responsable"] == mi_nombre}
    if filtro == "persona":
        return {f["id"] for f in filas if f["responsable"] is not None and f["responsable"] == quien}
    return {f["id"] for f in filas}


# ═══ 1. Cada filtro enseña exactamente lo que cumple ═══════════════════

def test_el_mundo_de_prueba_tiene_de_todo(mundo, gente):
    """Para que lo de abajo signifique algo: hay vencidas y no, pendientes y no, con y sin responsable, de cada
    persona y de un chat sin nombre."""
    _sembrar(mundo, gente)
    filas = _filas()
    assert len(filas) == 10
    assert {f["responsable"] for f in filas} == {None, "Persona Uno", "Persona Dos", "Code"}
    assert {f["estado"] for f in filas} == {"pendiente", "hecha", "descartado"}
    assert sum(f["vencida"] for f in filas) == 3 and sum(not f["vencida"] for f in filas) == 7


def test_los_filtros_declarados_son_estos_y_cada_uno_se_prueba_aqui():
    assert set(db.FILTROS_DE_TAREAS) == {"pendientes", "vencidas", "mias", "persona"}


@pytest.mark.parametrize("filtro", ["pendientes", "vencidas"])
def test_pendientes_y_vencidas_enseñan_exactamente_las_que_cumplen(mundo, gente, filtro):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro=filtro)
    assert _ids(html) == _esperado(_filas(), filtro) and _ids(html)
    assert _ids(html) != _ids(ver(mundo, p=1))                      # y de verdad esconde algo


@pytest.mark.parametrize("quien", ["Persona Uno", "Persona Dos", "Code"])
def test_por_persona_enseña_exactamente_las_de_esa_persona(mundo, gente, quien):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro="persona", quien=quien)
    assert _ids(html) == _esperado(_filas(), "persona", quien=quien) and _ids(html)


@pytest.mark.parametrize("chat,nombre", [("dueno", "Persona Uno"), ("rosi", "Persona Dos")])
def test_las_mias_son_las_de_quien_entro_y_cada_quien_ve_las_suyas(mundo, gente, chat, nombre):
    _sembrar(mundo, gente)
    html = _como_chat(mundo, getattr(gente, chat), p=1, filtro="mias")
    assert _ids(html) == _esperado(_filas(), "mias", mi_nombre=nombre) and _ids(html)
    assert "Las mías" in html and 'aria-current="true">Las mías' in html


def test_por_persona_compara_el_nombre_entero_no_un_pedazo(mundo, gente, monkeypatch):
    """Dos personas cuyos nombres se contienen («Ana» y «Ana María»): filtrar por una no trae las de la otra, ni
    por mayúsculas distintas."""
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {gente.dueno: "Ana", gente.rosi: "Ana María"})
    _sembrar(mundo, gente)
    for quien, chat in (("Ana", gente.dueno), ("Ana María", gente.rosi)):
        html = ver(mundo, p=1, filtro="persona", quien=quien)
        propias = {f["id"] for f in _filas() if f["responsable"] == quien}
        assert propias and _ids(html) == propias, quien
    assert _ids(ver(mundo, p=1, filtro="persona", quien="ana")) == {f["id"] for f in _filas()}      # no existe: no filtra


def test_la_lista_de_una_persona_incluye_sus_hechas_y_el_resumen_cuenta_lo_que_queda(mundo, gente):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro="persona", quien="Persona Dos")
    # Rosi: 12 y 13 pendientes; 18 hecha; 19 descartada.
    assert _ids(html) == {12, 13, 18, 19}
    assert re.search(r"<summary>1 hecha y 1 con otro estado</summary>", html)


def test_el_filtro_que_no_deja_pendientes_lo_dice_y_no_inventa(mundo, gente):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro="persona", quien="Code")
    assert _ids(html) == {15}
    mundo.con.execute("UPDATE tareas SET estado = 'hecha' WHERE id = 15")
    html = ver(mundo, p=1, filtro="persona", quien="Code")
    assert "Ninguna tarea pendiente con este filtro." in html and "No queda nada pendiente." not in html
    assert "Ninguna tarea pendiente con este filtro." not in ver(mundo, p=1)


def test_la_navegacion_ofrece_todas_pendientes_vencidas_mias_y_cada_persona_que_tiene_tareas(mundo, gente):
    _sembrar(mundo, gente)
    nav = arbol(ver(mundo, p=1)).buscar("nav", "filtros")
    assert len(nav) == 1
    assert [a.todo_el_texto() for a in nav[0].buscar("a")] == [
        "Todas", "Pendientes", "Vencidas", "Las mías", "Code", "Persona Dos", "Persona Uno"]
    actuales = [a.todo_el_texto() for a in nav[0].buscar("a") if a.attrs.get("aria-current") == "true"]
    assert actuales == ["Todas"]
    # Cada enlace lleva a una lista cuyo filtro es el suyo (se sigue el enlace de verdad).
    for a in nav[0].buscar("a")[1:]:
        consulta = dict(parse_qsl(urlsplit(a.attrs["href"]).query))
        html = ver(mundo, **consulta)
        assert [x.todo_el_texto() for x in arbol(html).buscar("nav", "filtros")[0].buscar("a")
                if x.attrs.get("aria-current") == "true"] == [a.todo_el_texto()]


# ═══ 2. Un filtro desconocido no esconde nada ══════════════════════════

INVENTADOS = [
    {}, {"filtro": ""}, {"filtro": "todas"}, {"filtro": "x"}, {"filtro": "PENDIENTES"}, {"filtro": "pendientes "},
    {"filtro": " pendientes"}, {"filtro": "pendientes,vencidas"}, {"filtro": "mias;drop"}, {"filtro": "%"},
    {"filtro": "\x00"}, {"filtro": "persona"}, {"filtro": "persona", "quien": ""},
    {"filtro": "persona", "quien": "Nadie"}, {"filtro": "persona", "quien": "persona uno"},
    {"filtro": "persona", "quien": "Persona Uno "}, {"filtro": "persona", "quien": "Persona Uno\x00"},
    {"filtro": "persona", "quien": "Persona Uno'; DROP TABLE tareas;--"}, {"filtro": "persona", "quien": "ñ" * 5000},
    {"quien": "Persona Uno"}, {"filtro": "ñ"}, {"filtro": "../../etc/passwd"}, {"filtro": "//evil.example"},
    {"filtro": "pendientes\r\nSet-Cookie: x=1"}, {"filtro": "‮pedientes"}, {"filtro": "0"}, {"filtro": "-1"},
]


@pytest.mark.parametrize("consulta", INVENTADOS, ids=[repr(c)[:60] for c in INVENTADOS])
def test_un_filtro_que_nadie_declaro_no_esconde_nada_ni_viaja_en_ningun_enlace(mundo, gente, consulta):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, **consulta)
    assert _ids(html) == _ids(ver(mundo, p=1)) == {f["id"] for f in _filas()}
    assert "filtro=" not in html.split('<nav class="filtros"', 1)[1].split("</nav>", 1)[1]
    assert [a.todo_el_texto() for a in arbol(html).buscar("nav", "filtros")[0].buscar("a")
            if a.attrs.get("aria-current") == "true"] == ["Todas"]


def test_dos_filtros_a_la_vez_no_son_un_filtro_raro_se_toma_uno_solo(mundo, gente):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro="pendientes")
    raw = _cliente(config.CHAT_ID_DUENO).get("/proyectos?p=1&filtro=pendientes&filtro=vencidas").text
    assert _ids(raw) in (_esperado(_filas(), "pendientes"), _esperado(_filas(), "vencidas")) and _ids(html)


# Las tres capas, de a una (cada una tiene que cumplir por sí sola):
_NO_FILTROS = ["", "x", "todas", "PENDIENTES", "pendientes ", "mias;", "%", "\x00", "ñ", "persona_", "../x",
               "persona\n", "pendiente", "vencida", "mía"]


@pytest.mark.parametrize("filtro", _NO_FILTROS)
def test_capa_sufijo_lo_que_no_es_filtro_no_viaja(filtro):
    assert panel._sufijo_de_filtro(filtro, "Persona Uno") == ""


@pytest.mark.parametrize("filtro", _NO_FILTROS)
def test_capa_vigente_lo_que_no_es_filtro_no_vale(filtro):
    assert panel._filtro_vigente(filtro, "Persona Uno", personas=["Persona Uno"], mi_nombre="Persona Uno",
                                 solo_ver=False) == ("", "")


@pytest.mark.parametrize("filtro", _NO_FILTROS)
def test_capa_predicado_lo_que_no_es_filtro_deja_pasar_todo(mundo, gente, filtro):
    _sembrar(mundo, gente)
    assert all(db.tarea_cumple_filtro(f, filtro, "Persona Uno", "Persona Uno") for f in _filas())


@pytest.mark.parametrize("quien", ["", "x" * (panel.LARGO_QUIEN + 1), "a\nb", "a\x00b", "a\rb", "\t"])
def test_capa_sufijo_una_persona_sin_nombre_o_con_forma_rara_no_viaja(quien):
    assert panel._sufijo_de_filtro("persona", quien) == ""


def test_capa_sufijo_lo_valido_viaja_codificado():
    assert panel._sufijo_de_filtro("vencidas", "") == "&filtro=vencidas"
    assert panel._sufijo_de_filtro("persona", "Ana Núñez&x=1") == "&filtro=persona&quien=Ana%20N%C3%BA%C3%B1ez%26x%3D1"


def test_capa_vigente_mias_y_persona_piden_sus_condiciones():
    f = panel._filtro_vigente
    assert f("mias", "", personas=[], mi_nombre="Ana", solo_ver=False) == ("mias", "")
    assert f("mias", "", personas=[], mi_nombre="Ana", solo_ver=True) == ("", "")
    assert f("mias", "", personas=[], mi_nombre=None, solo_ver=False) == ("", "")
    assert f("persona", "Ana", personas=["Ana"], mi_nombre=None, solo_ver=True) == ("persona", "Ana")
    assert f("persona", "Luis", personas=["Ana"], mi_nombre=None, solo_ver=False) == ("", "")


def test_capa_predicado_sin_nombre_no_hay_nadie_que_cumpla_mias_ni_persona(mundo, gente):
    """Una tarea sin responsable (`None`) no es «mía» de quien no tiene nombre, ni de la persona «vacía»."""
    _sembrar(mundo, gente)
    sin_dueno = next(f for f in _filas() if f["responsable"] is None)
    assert not db.tarea_cumple_filtro(sin_dueno, "mias", "", None)
    assert not db.tarea_cumple_filtro(sin_dueno, "mias", "", "")
    assert not db.tarea_cumple_filtro(sin_dueno, "persona", "", None)
    assert not db.tarea_cumple_filtro(sin_dueno, "persona", None, None)


# ═══ 3. «Las mías» y solo ver ══════════════════════════════════════════

def test_en_solo_ver_no_se_ofrece_las_mias_y_escrita_a_mano_no_filtra(mundo, gente):
    _sembrar(mundo, gente)
    html = _solo_ver(mundo, p=1)
    textos = [a.todo_el_texto() for a in arbol(html).buscar("nav", "filtros")[0].buscar("a")]
    assert "Las mías" not in textos and "Todas" in textos and "Pendientes" in textos and "Persona Uno" in textos
    assert "filtro=mias" not in html
    a_mano = _solo_ver(mundo, p=1, filtro="mias")
    assert _ids(a_mano) == _ids(html) == {f["id"] for f in _filas()}
    assert "filtro=mias" not in a_mano


def test_en_solo_ver_los_demas_filtros_funcionan_y_no_traen_controles(mundo, gente):
    _sembrar(mundo, gente)
    for consulta, filtro, quien in (({"filtro": "pendientes"}, "pendientes", None), ({"filtro": "vencidas"}, "vencidas", None),
                                    ({"filtro": "persona", "quien": "Persona Dos"}, "persona", "Persona Dos")):
        html = _solo_ver(mundo, p=1, **consulta)
        assert _ids(html) == _esperado(_filas(), filtro, quien=quien), consulta
        assert sv.controles_que_sobran(html) == [], consulta
        nav = arbol(html).buscar("nav", "filtros")[0]
        assert not [n for n in nav.elementos() if n.tag in sv._CAMPOS]


def test_la_sesion_de_la_casa_sin_nombre_conocido_no_ofrece_las_mias(mundo, gente, monkeypatch):
    _sembrar(mundo, gente)
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {gente.rosi: "Persona Dos"})     # el dueño sin nombre
    html = ver(mundo, p=1, filtro="mias")
    assert "Las mías" not in html and _ids(html) == {f["id"] for f in _filas()}


# ═══ 4. Marcar (y todo lo de una tarea) no pierde el filtro ════════════

FILTROS_PUESTOS = [("pendientes", None), ("vencidas", None), ("mias", None), ("persona", "Persona Dos")]


def _consulta(filtro, quien):
    return {"filtro": filtro, **({"quien": quien} if quien else {})}


def _tiene_el_filtro(href, filtro, quien):
    q = dict(parse_qsl(urlsplit(href).query))
    return q.get("filtro") == filtro and q.get("quien") == quien


@pytest.mark.parametrize("filtro,quien", FILTROS_PUESTOS)
def test_marcar_hecha_y_desmarcar_vuelven_con_el_filtro_puesto(mundo, gente, filtro, quien):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, **_consulta(filtro, quien))
    marcan = [f for f in _formularios_de(html) if re.fullmatch(r"/proyectos/tarea/\d+/(hecha|reabrir)\?.*", f["accion"] or "")]
    assert marcan, "ningún formulario de marcar en la página con filtro"
    for f in marcan:
        assert _tiene_el_filtro(f["accion"], filtro, quien), f["accion"]
    uno = next(f for f in marcan if f["accion"].split("?")[0].endswith("/hecha"))
    r = _cliente(config.CHAT_ID_DUENO).post(uno["accion"], data={}, follow_redirects=False)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert destino.startswith("/proyectos?p=1&filtro=") and "error=" not in destino and _tiene_el_filtro(destino, filtro, quien), destino
    # La página a la que vuelve tiene el filtro puesto de verdad.
    tareas = ver(mundo, **{k: v for k, v in parse_qsl(urlsplit(destino).query) if k in ("p", "filtro", "quien")})
    assert _ids(tareas) <= {f["id"] for f in _filas()}
    assert [a.todo_el_texto() for a in arbol(tareas).buscar("nav", "filtros")[0].buscar("a")
            if a.attrs.get("aria-current") == "true"] != ["Todas"]


def test_marcar_sin_filtro_vuelve_como_siempre(mundo, gente):
    _sembrar(mundo, gente)
    r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/13/hecha", data={}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?p=1&hecho=tarea_hecha#tarea-13"


@pytest.mark.parametrize("filtro,quien", FILTROS_PUESTOS)
def test_todo_formulario_de_tarea_de_la_pagina_con_filtro_lo_lleva_y_vuelve_con_el_mismo_filtro(mundo, gente, monkeypatch, noco, filtro, quien):
    """Los hermanos de «marcar», sacados de la página pintada: TODO formulario `post` cuya acción empieza por
    `/proyectos/tarea/` (en la lista, el detalle abierto, el título en edición, la pregunta de borrar, el
    comentario en edición, las personas de la tarea) lleva el filtro en su dirección y, enviado como lo envía un
    navegador, vuelve a una dirección con el MISMO filtro."""
    _sembrar(mundo, gente)
    mundo.comentario(51, 12, gente.rosi, "de rosi")
    vistas = [{"t": 10}, {"t": 12}, {"t": 10, "editar_tarea": 10}, {"t": 10, "confirmar_borrar": 10},
              {"t": 12, "editar_comentario": 51}]
    enviados = 0
    for extra in vistas:
        html = ver(mundo, p=1, **_consulta(filtro, quien), **extra)
        formas = [f for f in _formularios_de(html) if (f["accion"] or "").startswith("/proyectos/tarea/")]
        assert formas, extra
        for f in formas:
            assert _tiene_el_filtro(f["accion"], filtro, quien), (extra, f["accion"])
    # Y enviados de verdad (cada clase de formulario de tarea, una vez por vista).
    for extra in vistas:
        vistos = set()
        for i in range(40):
            html = ver(mundo, p=1, **_consulta(filtro, quien), **extra)
            formas = [f for f in _formularios_de(html) if (f["accion"] or "").startswith("/proyectos/tarea/")]
            if i >= len(formas):
                break
            f = formas[i]
            ruta = f["accion"].split("?")[0]
            marca = re.sub(r"\d+", "N", ruta)
            if marca in vistos or ruta.endswith("/hecha") or ruta.endswith("/reabrir"):
                continue
            vistos.add(marca)
            escoger = _otra_opcion if f["clase"] == "resp" else _la_marcada
            datos = _lo_que_manda_el_navegador(f, _escrito, escoger)
            if f["clase"] == "agregar":
                datos["noco_id"] = "102"
            r = _cliente(config.CHAT_ID_DUENO).post(f["accion"], data=datos, follow_redirects=False)
            assert r.status_code == 303, (extra, f["accion"], r.status_code)
            destino = r.headers["location"]
            if destino.startswith("/proyectos?p="):
                assert _tiene_el_filtro(destino, filtro, quien), (extra, f["accion"], destino)
                enviados += 1
    assert enviados >= 5, enviados


@pytest.mark.parametrize("filtro,quien", FILTROS_PUESTOS)
def test_los_enlaces_y_busquedas_que_se_quedan_en_el_proyecto_conservan_el_filtro(mundo, gente, filtro, quien):
    """Hermanos de lectura: en el centro y la derecha, todo enlace al MISMO proyecto (menos los del propio filtro)
    y todo formulario de búsqueda (GET) lleva el filtro puesto; los enlaces de la izquierda a OTROS proyectos no."""
    _sembrar(mundo, gente)
    consulta = _consulta(filtro, quien)
    paginas = [ver(mundo, p=1, **consulta, **extra) for extra in
               ({}, {"t": 10}, {"t": 10, "editar_tarea": 10}, {"t": 10, "confirmar_borrar": 10},
                {"confirmar": "cerrar"}, {"editar": "nombre"}, {"borrar_proyecto": 1}, {"pq": "ab", "pdonde": "proyecto"},
                {"t": 10, "pq": "ab", "pdonde": "tarea-10"})]
    revisados = 0
    for html in paginas:
        main = arbol(html).buscar("main")[0]
        filtros = main.buscar("nav", "filtros")[0]
        propios = {id(a) for a in filtros.buscar("a")}
        for a in main.buscar("a"):
            href = a.attrs.get("href", "")
            if id(a) in propios or not href.startswith("/proyectos?p=1"):
                continue
            assert _tiene_el_filtro(href, filtro, quien), href
            revisados += 1
        for f in _formularios_de(html, solo_post=False):
            if f["metodo"] == "get" and f["accion"] == "/proyectos" and any(c["name"] == "pdonde" for c in f["campos"]) \
                    and not any(c["name"] == "q" for c in f["campos"]):
                ocultos = {c["name"]: c["value"] for c in f["campos"] if c["tipo"] == "hidden"}
                assert ocultos.get("filtro") == filtro and ocultos.get("quien") == quien, ocultos
                revisados += 1
    assert revisados >= 12, revisados
    # La izquierda: otro proyecto empieza sin filtro.
    izq = [a.attrs["href"] for a in arbol(paginas[0]).buscar("nav", "grupos")[0].buscar("a")
           if a.attrs.get("href", "").startswith("/proyectos?p=")]
    assert izq and not [h for h in izq if "filtro=" in h]


@pytest.mark.parametrize("sucio", ["//evil.example", "x", "persona", "pendientes\r\nSet-Cookie: x=1", "../..", "\x00"])
def test_un_filtro_malo_en_la_accion_de_un_post_no_cambia_el_destino(mundo, gente, sucio):
    _sembrar(mundo, gente)
    sucio = quote(sucio, safe="")      # lo que de verdad viaja por la red: los controles van codificados
    for accion in (f"/proyectos/tarea/13/hecha?filtro={sucio}", f"/proyectos/tarea/13/hecha?filtro=persona&quien={sucio}"):
        r = _cliente(config.CHAT_ID_DUENO).post(accion, data={}, follow_redirects=False)
        assert r.status_code == 303
        destino = r.headers["location"]
        # (un nombre imprimible, como «//evil.example», sí puede ir: viaja codificado dentro de `quien=` y nunca es el destino)
        assert destino.startswith("/proyectos?p=1&") and "//evil" not in destino and "\r" not in destino and "\n" not in destino
        assert "%0D" not in destino.upper() and "%00" not in destino
        mundo.con.execute("UPDATE tareas SET estado = 'pendiente', completado_en = NULL WHERE id = 13")
        if "filtro=persona" not in destino:
            assert "filtro=" not in destino, destino


def test_un_nombre_enorme_en_la_accion_de_un_post_no_viaja(mundo, gente):
    _sembrar(mundo, gente)
    r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/13/hecha?filtro=persona&quien=" + "a" * 500, data={}, follow_redirects=False)
    assert "quien=" not in r.headers["location"] and "filtro=" not in r.headers["location"]


def test_el_filtro_de_un_pedido_no_se_filtra_al_siguiente(mundo, gente):
    """El middleware guarda el filtro solo mientras dura el pedido: un POST sin filtro justo después de uno con filtro
    vuelve limpio."""
    _sembrar(mundo, gente)
    c = _cliente(config.CHAT_ID_DUENO)
    c.post("/proyectos/tarea/13/hecha?filtro=pendientes", data={}, follow_redirects=False)
    r = c.post("/proyectos/tarea/13/reabrir", data={}, follow_redirects=False)
    assert "filtro" not in r.headers["location"]


# ═══ 5. Lo demás no cambia ═════════════════════════════════════════════

def test_como_va_dice_lo_mismo_con_cualquier_filtro(mundo, gente):
    _sembrar(mundo, gente)

    def como_va(html):
        return arbol(html).buscar("section", "como-va")[0].todo_el_texto()
    base = como_va(ver(mundo, p=1))
    for filtro, quien in FILTROS_PUESTOS:
        assert como_va(ver(mundo, p=1, **_consulta(filtro, quien))) == base
    assert "de 10 tareas hechas" in base                       # (10 filas: no depende del filtro)


def test_las_otras_vistas_ignoran_el_filtro(mundo, gente):
    _sembrar(mundo, gente)
    for consulta in ({"g": "CDS"}, {"sin_grupo": 1}, {"g": "CDS", "t": 21}):
        limpio = ver(mundo, **consulta)
        assert not arbol(limpio).buscar("nav", "filtros") and _ids(limpio)
        for filtro, quien in FILTROS_PUESTOS:
            filtrado = ver(mundo, **consulta, **_consulta(filtro, quien))
            assert _ids(filtrado) == _ids(limpio), (consulta, filtro)
            assert "filtro=" not in filtrado and 'name="filtro"' not in filtrado and 'name="quien"' not in filtrado


def test_un_proyecto_sin_tareas_no_ofrece_filtros(mundo, gente):
    mundo.proyecto(1, "Vacío", area="CDS")
    html = ver(mundo, p=1)
    assert not arbol(html).buscar("nav", "filtros") and "Sin tareas todavía." in html


def test_ningun_numero_de_chat_en_la_navegacion_de_filtros(mundo, gente):
    _sembrar(mundo, gente)
    html = ver(mundo, p=1, filtro="mias")
    nav = html.split('<nav class="filtros"', 1)[1].split("</nav>", 1)[0]
    for n in (str(gente.dueno), str(gente.rosi), str(config.CHAT_ID_CODE), str(SIN_NOMBRE)):
        assert n not in nav
