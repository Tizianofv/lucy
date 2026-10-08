"""Parte 1 de «la página de un proyecto, completa» (diseño de Tiziano aprobado el 8-oct-2026): «Cómo va» y
«Cerrar el proyecto» pasan a la columna derecha, junto a «Personas». No escribe nada en la base.

Lo que se vigila, todo sobre el HTML que de verdad sale de `GET /proyectos` (ruta real, plantilla real):
  1. «Cómo va» está en la columna derecha y NO en la cabecera ni en el centro.
  2. Sus cifras son las de `db._resumen`: no hay un segundo cálculo en la plantilla.
  3. «Cerrar proyecto», su pregunta y «Reabrir» están en la columna derecha y no quedó ninguno en el centro.
  4. «Personas» sale idéntica a como salía antes (foto del HTML de `853e8fa`).
  5. En solo ver, ni «Cómo va» ni «Cerrar el proyecto» traen un control, en cada consulta de lectura que ya
     usa `test_proyectos_solo_ver.py` (la lista de controles sale de lo real: `controles_que_sobran`/`_CAMPOS`).

FRONTERA: no es un navegador; se lee el HTML, no cómo se pinta. Que cerrar y reabrir "sigan andando" lo vigilan los
recorridos que ya existen (`test_escrituras_proyecto.py`: cada formulario de la página se envía como lo enviaría un
navegador); acá solo se exige que esos formularios estén en la columna nueva.

Correr:  python3 -m pytest tests/test_proyecto_como_va_y_cerrar.py -q
"""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import config
import db.db as db
from test_pagina_proyectos import _dia, gente, mundo, ver  # noqa: F401
from test_pagina_proyectos_maqueta import arbol
import test_proyectos_solo_ver as sv

# El HTML de «Personas del proyecto» que salía en `main` 853e8fa (antes de esta parte) para un proyecto con
# una persona, sacado corriendo la página de ese commit. Es una foto: si se cambia a propósito, se cambia aquí.
PERSONAS_DE_ANTES = '<section class="bloque personas-p">\n      <div class="encabezado"><h2>Personas del proyecto</h2><span class="nota">Quién está metido y qué hace aquí</span></div>\n      <div class="personas"><div class="persona"><span class="ini" title="Ana Pérez">AP</span>\n  <span class="quien"><b>Ana Pérez</b><span>Mezcla</span></span>\n  <form class="en-linea" method="post" action="/proyectos/1/personas/5/quitar"><button class="quitar" aria-label="Quitar a Ana Pérez" title="Quitar a Ana Pérez">×</button></form></div></div>\n      \n<div class="persona-campo">\n  <form class="buscar-persona" method="get" action="/proyectos" role="search">\n    <input type="text" name="pq" placeholder="Escoge de las personas de Noco…" aria-label="Escoge a una persona de Noco" data-buscar-persona="elegir" autocomplete="off">\n    <input type="hidden" name="p" value="1"><input type="hidden" name="pdonde" value="proyecto">\n    <button class="btn-linea">Buscar</button>\n  </form><form class="agregar" method="post" action="/proyectos/1/personas" data-pide-persona>\n    <div class="sugerencias"></div>\n    <input name="rol" type="text" required maxlength="80" placeholder="¿Qué hace aquí?" aria-label="Qué hace esta persona">\n    <button class="guardar">Agregar</button>\n  </form>\n</div>\n    </section>'


def _con_de_todo(mundo, gente):
    mundo.proyecto(1, "Uno", area="CDS", responsable=config.CHAT_ID_DUENO)
    mundo.proyecto(2, "Sin tareas", area="CDS")
    mundo.proyecto(3, "Cerrado", area="CDS", estado="cerrado")
    mundo.tarea(10, "vencida", proyecto=1, vence=_dia(-2))
    mundo.tarea(11, "pendiente", proyecto=1)
    mundo.tarea(12, "hecha", proyecto=1, estado="hecha", completado=_dia(-1))
    mundo.tarea(13, "hecha 2", proyecto=1, estado="hecha", completado=_dia(-1))
    mundo.tarea(30, "del cerrado", proyecto=3)
    mundo.con.execute("INSERT INTO participantes (id, proyecto_id, noco_id, nombre, rol, creado_por_chat_id) "
                      "VALUES (5, 1, 101, 'Ana Pérez', 'Mezcla', ?)", (gente.rosi,))


def _columnas(html):
    cuerpo, = arbol(html).buscar("main")[0].buscar("div", "cuerpo")
    centro, lado = cuerpo.hijos[0], cuerpo.hijos[1]
    assert centro.clases == ["centro"] and lado.clases == ["lado-der"]
    return arbol(html).buscar("main")[0].buscar("div", "cabeza")[0], centro, lado


def _bloque(lado, clase):
    b = [s for s in lado.buscar("section", "bloque") if clase in s.clases]
    assert len(b) == 1, (clase, len(b))
    return b[0]


# ═══ 1. Dónde está «Cómo va» ═══════════════════════════════════════════

def test_como_va_esta_en_la_columna_derecha_y_no_en_la_cabecera_ni_en_el_centro(mundo, gente):
    _con_de_todo(mundo, gente)
    cabeza, centro, lado = _columnas(ver(mundo, p=1))
    assert [h.todo_el_texto() for h in lado.buscar("h2")] == ["Cómo va", "Personas del proyecto", "Cerrar el proyecto"]
    como = _bloque(lado, "como-va")
    assert "2 de 4 tareas hechas" in como.todo_el_texto() and "1 vencida" in como.todo_el_texto()
    # En la cabecera quedan el estado, el último movimiento y los pendientes; ni la barra ni las vencidas.
    assert [p.todo_el_texto() for p in cabeza.buscar("span", "pastilla")][2:] == ["2 pendientes"]
    assert not cabeza.buscar(None, "avance") and not cabeza.buscar(None, "riel")
    assert "vencida" not in cabeza.todo_el_texto() and "tareas hechas" not in cabeza.todo_el_texto()
    assert not centro.buscar(None, "avance") and "tareas hechas" not in centro.todo_el_texto()


def test_sin_tareas_el_bloque_lo_dice_con_la_frase_de_la_lista_y_sin_barra(mundo, gente):
    _con_de_todo(mundo, gente)
    _, _, lado = _columnas(ver(mundo, p=2))
    como = _bloque(lado, "como-va")
    assert "Sin tareas todavía." in como.todo_el_texto() and not como.buscar(None, "riel")


# ═══ 2. Las cifras son las de `_resumen` ═══════════════════════════════

def test_las_cifras_del_bloque_son_las_de_resumen_y_no_otro_calculo(mundo, gente, monkeypatch):
    _con_de_todo(mundo, gente)
    real = db._resumen

    def inventado(pendientes, otras):
        # Cifras que ningún conteo de las tareas del proyecto puede dar (el proyecto 1 tiene 4), y un porcentaje
        # que no sale de 11 entre 17 (65): así un segundo cálculo del porcentaje en la plantilla se nota.
        return {**real(pendientes, otras), "n_total": 17, "n_hechas": 11, "n_vencidas": 6, "pct": 40}
    monkeypatch.setattr(db, "_resumen", inventado)
    html = ver(mundo, p=1)
    _, _, lado = _columnas(html)
    como = _bloque(lado, "como-va")
    assert "11 de 17 tareas hechas" in como.todo_el_texto() and "6 vencidas" in como.todo_el_texto()
    assert 'style="width:40%"' in html.split('<section class="bloque como-va">', 1)[1].split("</section>", 1)[0]


def test_con_cifras_reales_el_bloque_dice_lo_que_dice_la_lista_de_la_izquierda(mundo, gente):
    _con_de_todo(mundo, gente)
    html = ver(mundo, p=1)
    _, _, lado = _columnas(html)
    assert "2 de 4 tareas hechas" in lado.todo_el_texto() and 'style="width:50%"' in html
    assert "<b>1 vencida</b> · 2/4" in html


# ═══ 3. Cerrar y reabrir ═══════════════════════════════════════════════

def test_cerrar_proyecto_esta_en_la_columna_derecha_y_en_el_centro_no_queda_nada_de_estado(mundo, gente):
    _con_de_todo(mundo, gente)
    _, centro, lado = _columnas(ver(mundo, p=1))
    cerrar = _bloque(lado, "cerrar-p")
    enlaces = [a for a in cerrar.buscar("a") if a.todo_el_texto() == "Cerrar proyecto"]
    assert len(enlaces) == 1 and enlaces[0].attrs["href"] == "/proyectos?p=1&confirmar=cerrar"
    assert "Cerrar proyecto" not in centro.todo_el_texto()
    assert not [f for f in centro.buscar("form") if f.attrs["action"].endswith("/estado")]


def test_la_pregunta_de_cerrar_y_el_reabrir_estan_en_la_columna_derecha(mundo, gente):
    _con_de_todo(mundo, gente)
    _, centro, lado = _columnas(ver(mundo, p=1, confirmar="cerrar"))
    cerrar = _bloque(lado, "cerrar-p")
    assert "¿Cerrar este proyecto?" in cerrar.todo_el_texto() and "Quedan 2 pendientes" in cerrar.todo_el_texto()
    sí, = cerrar.buscar("form")
    assert sí.attrs["action"] == "/proyectos/1/estado"
    assert [i.attrs["value"] for i in sí.buscar("input")] == ["cerrado"]
    assert "¿Cerrar este proyecto?" not in centro.todo_el_texto()
    # Cerrado: el bloque trae «Reabrir» y ya no ofrece cerrar.
    _, centro3, lado3 = _columnas(ver(mundo, p=3))
    cerrar3 = _bloque(lado3, "cerrar-p")
    abrir, = cerrar3.buscar("form")
    assert abrir.attrs["action"] == "/proyectos/3/estado" and abrir.buscar("input")[0].attrs["value"] == "activo"
    assert "Reabrir" in abrir.todo_el_texto() and "Cerrar proyecto" not in cerrar3.todo_el_texto()
    assert "Proyecto cerrado: no se le agregan tareas." in cerrar3.todo_el_texto()
    assert not [f for f in centro3.buscar("form") if f.attrs["action"].endswith("/estado")]


# ═══ 4. «Personas» queda idéntica ══════════════════════════════════════

def test_personas_sale_identica_a_como_salia(mundo, gente):
    _con_de_todo(mundo, gente)
    html = ver(mundo, p=1)
    ahora = re.search(r'<section class="bloque personas-p">.*?</section>', html, re.S).group(0)
    assert ahora == PERSONAS_DE_ANTES


# ═══ 5. En solo ver no hay controles ═══════════════════════════════════

CONSULTAS = sv.VISTAS + list(sv.A_MANO.values())
_CONTROLES = set(sv._CAMPOS) | {"a"}


@pytest.mark.parametrize("consulta", CONSULTAS, ids=[str(c) for c in CONSULTAS])
def test_en_solo_ver_ni_como_va_ni_cerrar_traen_un_control(consulta, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", "https://registro.example.test")
    html = sv._pintar("ver", **consulta)
    raiz = arbol(html)
    for bloque in [s for s in raiz.buscar("section") if {"como-va", "cerrar-p"} & set(s.clases)]:
        for n in bloque.elementos():
            assert n.tag not in _CONTROLES, (consulta, bloque.clases, n.tag)
            assert not [k for k in n.attrs if k.startswith("on") or k.startswith("data-")], (consulta, n.tag, n.attrs)
    assert sv.controles_que_sobran(html, "https://registro.example.test/#inicio") == []


def _ver_solo(mundo, **consulta):
    """`GET /proyectos` con la cookie de solo ver, por la ruta real y con la base de prueba real (`mundo`)."""
    r = sv.cliente("ver").get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def test_en_solo_ver_con_base_de_verdad_las_mismas_consultas_no_traen_controles(mundo, gente):
    _con_de_todo(mundo, gente)
    for consulta in ({"p": 1}, {"p": 2}, {"p": 3}, {"p": 1, "confirmar": "cerrar"}, {"p": 3, "confirmar": "cerrar"}):
        html = _ver_solo(mundo, **consulta)
        for bloque in [s for s in arbol(html).buscar("section") if {"como-va", "cerrar-p"} & set(s.clases)]:
            assert not [n.tag for n in bloque.elementos() if n.tag in _CONTROLES], (consulta, bloque.clases)
        assert "¿Cerrar este proyecto?" not in html and "Cerrar proyecto" not in html
        assert sv.controles_que_sobran(html) == [], consulta


def test_en_solo_ver_un_proyecto_abierto_no_trae_el_bloque_de_cerrar_y_uno_cerrado_solo_lo_dice(mundo, gente):
    _con_de_todo(mundo, gente)
    abierto = arbol(_ver_solo(mundo, p=1))
    assert not [s for s in abierto.buscar("section") if "cerrar-p" in s.clases]
    cerrado = [s for s in arbol(_ver_solo(mundo, p=3)).buscar("section") if "cerrar-p" in s.clases]
    assert len(cerrado) == 1 and "Proyecto cerrado: no se le agregan tareas." in cerrado[0].todo_el_texto()
    assert "Reabrir" not in cerrado[0].todo_el_texto()
    # Y «Cómo va» sí sale en solo ver: es información, no un control.
    assert [s for s in abierto.buscar("section") if "como-va" in s.clases]


def test_la_sesion_de_la_casa_si_ve_los_controles_del_bloque_de_cerrar(mundo, gente):
    """La prueba anterior solo vale si el detector ve lo que debe: en la sesión de la casa los controles existen."""
    _con_de_todo(mundo, gente)
    _, _, lado = _columnas(ver(mundo, p=1))
    assert [a for a in _bloque(lado, "cerrar-p").elementos() if a.tag in _CONTROLES]
