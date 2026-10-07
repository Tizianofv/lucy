"""Meter en un proyecto una tarea que no tiene (pedido de Tiziano, 5-oct-2026:
«poder agregar a un proyecto una Tarea sin proyecto»).

La ruta es `POST /proyectos/tarea/{tid}/proyecto` y escribe por la puerta que ya
existía, `crud.editar("tareas", tid, {"proyecto_id": N})`. Estas pruebas corren
el camino de producción: la ruta real, la plantilla real y el SQL de verdad
sobre SQLite (el mundo de `tests/test_escrituras_tarea.py`, el mismo de sus
hermanas `titulo`, `borrar` y `responsable`).

FRONTERA: SQLite no es Postgres. Por eso `crud.deshacer` (que usa
`jsonb_populate_record`) no se ejecuta acá: se comprueba que la huella lleva lo
que ese camino lee (la fila entera de `antes`), igual que hace
`crud.editar` para Telegram. Y dos envíos EXACTAMENTE simultáneos no se
prueban: los dos envíos seguidos sí.

Correr:  python3 -m pytest tests/test_tarea_a_proyecto.py -q
"""
from __future__ import annotations

import json
import os

import re

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import config
import db.db as db
from test_escrituras_tarea import (_adonde, foto, huellas, igual_salvo, mt,  # noqa: F401
                                   post, tarea)
from test_pagina_proyectos import _cliente, gente, ver  # noqa: F401
from test_escrituras_proyecto import noco  # noqa: F401

RUTA = "/proyectos/tarea/{}/proyecto"


def _mundo_con_sueltas(mt):
    mt.proyecto(5, "En la papelera", area="CDS", borrado=True)
    mt.proyecto(6, "Tecnico", area=db.AREA_TECNICA)
    mt.tarea(32, "suelta hecha", area="CDS", estado="hecha")
    mt.tarea(33, "huérfana", proyecto=5)
    mt.tarea(34, "de Code", area=db.AREA_TECNICA, responsable=config.CHAT_ID_CODE)
    mt.tarea(35, "borrada suelta", borrada=True)
    mt.con.commit()
    return mt


@pytest.fixture
def ms(mt):
    return _mundo_con_sueltas(mt)


# ── Se mueve, con su huella ──────────────────────────────────────────────

def test_una_tarea_suelta_entra_al_proyecto_con_huella_de_panel_y_pierde_su_grupo_propio(ms):
    antes = foto(ms)
    r = post(RUTA.format(30), {"proyecto": "2"})
    assert r.status_code == 303 and _adonde(r) == "/proyectos?p=2&hecho=tarea_proyecto#tarea-30"
    t = tarea(ms, 30)
    assert (t["proyecto_id"], t["area"]) == (2, None)          # el grupo es el del proyecto
    h, = huellas(ms)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "tareas", 30)
    # La huella lleva la fila entera de antes: de ahí vuelve `crud.deshacer`.
    assert json.loads(h["antes"])["proyecto_id"] is None and json.loads(h["antes"])["area"] == "CDS"
    assert json.loads(h["despues"])["proyecto_id"] == 2
    assert igual_salvo(antes, foto(ms), tareas={30}, log_acciones={h["id"]})
    html = ver(ms, p=2, hecho="tarea_proyecto")
    assert "Tarea metida en el proyecto." in html and 'data-tarea="30"' in html


def test_la_huella_es_la_misma_clase_que_la_de_las_hermanas(ms):
    post(RUTA.format(30), {"proyecto": "2"})
    post("/proyectos/tarea/31/titulo", {"titulo": "otro"})
    a, b = huellas(ms)
    assert (a["actor"], a["accion"], a["tabla"]) == (b["actor"], b["accion"], b["tabla"])


def test_una_tarea_sin_grupo_entra(ms):
    r = post(RUTA.format(31), {"proyecto": "1"})
    assert _adonde(r) == "/proyectos?p=1&hecho=tarea_proyecto#tarea-31"
    assert tarea(ms, 31)["proyecto_id"] == 1


def test_una_tarea_de_un_proyecto_en_la_papelera_se_puede_meter_en_uno_vivo(ms):
    r = post(RUTA.format(33), {"proyecto": "2"})
    assert _adonde(r) == "/proyectos?p=2&hecho=tarea_proyecto#tarea-33"
    assert tarea(ms, 33)["proyecto_id"] == 2


def test_una_tarea_hecha_suelta_tambien_entra_como_en_las_hermanas(ms):
    r = post(RUTA.format(32), {"proyecto": "2"})
    assert "hecho=tarea_proyecto" in _adonde(r)
    t = tarea(ms, 32)
    assert (t["proyecto_id"], t["estado"]) == (2, "hecha")


# ── Lo que NO se mueve, y la página lo dice ──────────────────────────────

@pytest.mark.parametrize("valor,clave", [
    ("999", "tarea_proyecto"),            # no existe
    ("5", "tarea_proyecto"),              # en la papelera
    ("3", "proyecto_cerrado"),            # cerrado
    ("abc", "tarea_proyecto"), ("", "tarea_proyecto"), ("-1", "tarea_proyecto"), ("2.5", "tarea_proyecto"),
])
def test_un_proyecto_que_no_vale_no_mueve_nada_y_la_pagina_lo_dice(ms, valor, clave):
    antes = foto(ms)
    r = post(RUTA.format(30), {"proyecto": valor})
    assert r.status_code == 303 and _adonde(r) == f"/proyectos?g=CDS&error={clave}&t=30#tarea-30"
    assert foto(ms) == antes
    html = ver(ms, g="CDS", error=clave)
    if clave == "tarea_proyecto":
        assert "la tarea NO se movió" in html
    assert "metida en el proyecto" not in html


def test_sin_el_campo_del_proyecto_no_escribe(ms):
    antes = foto(ms)
    assert "error=tarea_proyecto" in _adonde(post(RUTA.format(30), {}))
    assert foto(ms) == antes


def test_una_tarea_que_ya_tiene_proyecto_no_se_mueve(ms):
    antes = foto(ms)
    r = post(RUTA.format(10), {"proyecto": "1"})
    assert _adonde(r) == "/proyectos?p=2&error=tarea_con_proyecto&t=10#tarea-10"
    assert foto(ms) == antes
    assert "ya está en un proyecto" in ver(ms, p=2, error="tarea_con_proyecto")


def test_una_tarea_de_un_proyecto_cerrado_tampoco_se_mueve(ms):
    antes = foto(ms)
    assert "error=tarea_con_proyecto" in _adonde(post(RUTA.format(40), {"proyecto": "2"}))
    assert foto(ms) == antes


def test_una_tarea_borrada_no_se_mueve(ms):
    antes = foto(ms)
    r = post(RUTA.format(35), {"proyecto": "2"})
    assert "error=tarea_igual" in _adonde(r)
    assert foto(ms) == antes and tarea(ms, 35)["proyecto_id"] is None


def test_una_tarea_que_no_existe_da_error_tarea_y_no_escribe(ms):
    antes = foto(ms)
    r = post(RUTA.format(999), {"proyecto": "2"})
    assert _adonde(r) == "/proyectos?error=tarea" and foto(ms) == antes


def test_el_doble_envio_deja_una_sola_huella(ms):
    assert "hecho=tarea_proyecto" in _adonde(post(RUTA.format(30), {"proyecto": "2"}))
    despues = foto(ms)
    r = post(RUTA.format(30), {"proyecto": "2"})
    assert "error=tarea_con_proyecto" in _adonde(r)
    assert foto(ms) == despues and len(huellas(ms)) == 1


def test_el_segundo_envio_a_otro_proyecto_tampoco_la_vuelve_a_mover(ms):
    post(RUTA.format(30), {"proyecto": "2"})
    r = post(RUTA.format(30), {"proyecto": "1"})
    assert "error=tarea_con_proyecto" in _adonde(r) and tarea(ms, 30)["proyecto_id"] == 2


# ── La sala de control (la misma advertencia que al crear una tarea) ─────

def test_si_es_de_code_y_el_proyecto_no_es_tecnico_avisa_que_la_sala_no_la_ve(ms):
    r = post(RUTA.format(34), {"proyecto": "2"})
    assert _adonde(r) == "/proyectos?p=2&hecho=tarea_proyecto&sala_no=1#tarea-34"
    html = ver(ms, p=2, hecho="tarea_proyecto", sala_no=1)
    assert "la sala de control NO la verá" in html


def test_si_es_de_code_y_el_proyecto_es_tecnico_no_avisa(ms):
    r = post(RUTA.format(34), {"proyecto": "6"})
    assert "sala_no" not in _adonde(r)


# ── Quién puede ──────────────────────────────────────────────────────────

def test_sin_sesion_no_escribe(ms):
    antes = foto(ms)
    assert post(RUTA.format(30), {"proyecto": "2"}, chat=None).status_code == 401
    assert foto(ms) == antes


def test_con_la_cookie_de_solo_ver_la_ruta_rechaza_y_no_escribe(ms):
    import web.app as panel
    import web.auth as auth
    antes = foto(ms)
    c = _cliente()
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    r = c.post(RUTA.format(30), data={"proyecto": "2"}, follow_redirects=False)
    assert r.status_code == 401 and foto(ms) == antes


# ── El control en la página ──────────────────────────────────────────────

def _formulario(html: str, tid: int) -> str:
    return html.split(f'action="/proyectos/tarea/{tid}/proyecto"', 1)[1].split("</form>", 1)[0]


def test_el_detalle_de_una_tarea_suelta_ofrece_solo_proyectos_vivos_y_abiertos(ms):
    f = _formulario(ver(ms, g="CDS", t=30), 30)
    assert 'name="proyecto"' in f and ">Meter</button>" in f
    assert 'value="2"' in f and 'value="1"' in f and 'value="4"' in f and 'value="6"' in f
    assert 'value="3"' not in f          # cerrado
    assert 'value="5"' not in f          # en la papelera
    assert "CDS · El de la página" in f and "El de la página · CDS" not in f      # el grupo primero (6-oct-2026)


def test_el_selector_va_por_grupo_en_el_orden_de_la_izquierda_y_por_nombre_dentro_de_cada_uno(ms):
    """«grupo · proyecto», agrupado como la columna de la izquierda (el orden sale de la
    PÁGINA, no se teclea) y por nombre dentro de cada grupo; sin grupo, al final y sin
    separador. Los `value` son los ids de siempre."""
    ms.proyecto(7, "Zeta suelto", area=None)
    ms.proyecto(8, "aaa suelto", area=None)
    ms.proyecto(9, "Beta", area="CDS")
    ms.con.commit()
    html = ver(ms, g="CDS", t=30)
    f = _formulario(html, 30)
    opciones = re.findall(r'<option value="(\d+)">([^<]*)</option>', f)
    izquierda = [x for x in re.findall(r'<div class="grupo gc" data-g="([^"]*)"',
                                       html.split('<aside data-region="izquierda">', 1)[1].split("</aside>", 1)[0]) if x != "sin-grupo"]
    nombres = {r[0].lower(): r[0] for r in ms.con.execute("SELECT clave FROM areas")}
    orden_de_grupos = [nombres[x] for x in izquierda]
    textos = [t for _, t in opciones]
    con_grupo = [t.split(" · ", 1) for t in textos if " · " in t]
    sin_grupo = [t for t in textos if " · " not in t]
    assert [g for g, _ in con_grupo] == sorted([g for g, _ in con_grupo], key=orden_de_grupos.index)
    for g in set(g for g, _ in con_grupo):
        nombres_del_grupo = [n for gg, n in con_grupo if gg == g]
        assert nombres_del_grupo == sorted(nombres_del_grupo, key=str.lower), g
    assert textos[:len(con_grupo)] == [" · ".join(x) for x in con_grupo]      # los sin grupo, DESPUÉS de todos
    assert sin_grupo == ["aaa suelto", "Zeta suelto"]
    assert [i for i, t in opciones if t == "CDS · Beta"] == ["9"]


def test_sin_grupo_tambien_lo_ofrece(ms):
    assert 'action="/proyectos/tarea/31/proyecto"' in ver(ms, sin_grupo=1, t=31)


def test_dentro_de_un_proyecto_no_se_ofrece_ni_cerrado_el_detalle(ms):
    assert "/proyecto\"" not in ver(ms, p=2, t=10)
    assert "/proyecto\"" not in ver(ms, g="CDS")           # sin abrir el detalle


def test_la_pagina_de_ver_no_lo_ofrece(ms):
    import web.app as panel
    import web.auth as auth
    c = _cliente()
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    r = c.get("/proyectos", params={"g": "CDS", "t": 30})
    assert r.status_code == 200 and "Meter en un proyecto" not in r.text and "/proyecto\"" not in r.text
