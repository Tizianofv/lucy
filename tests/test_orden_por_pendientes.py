"""El orden de los proyectos dentro de cada grupo: por cuántas tareas PENDIENTES tienen, el que más
tiene arriba (13-oct-2026, Tiziano: «ordenar los proyectos en cantidad de tareas pendientes, que sea
automatico»). Se decide en UN sitio: `db.orden_en_la_lista`, que usa `armar_pagina`.

Qué vigila (todo por la ruta real, la plantilla real y el SQL real sobre SQLite):
  O1  Los abiertos salen del que más pendientes tiene al que menos; los pausados cuentan como abiertos;
      uno sin tareas cuenta 0 (va al final de los abiertos).
  O2  Empates: por nombre sin mayúsculas y, a igual nombre, por `id`; el mismo orden se siembre como se
      siembre (estable entre una carga y la siguiente).
  O3  Los cerrados no cambian: siguen por nombre, aunque tengan pendientes, y van aparte de los abiertos.
  O4  Hermanos: la lista de la izquierda, la vista «Estado de los proyectos» y la sesión de solo ver
      dicen el mismo orden (se leen de la página pintada, grupo por grupo).
  O5  Lo que se ve: el «N pendientes» de la vista de estado baja de renglón en renglón en los abiertos.
  O6  Una sola puerta: si `db.orden_en_la_lista` cambia, la página cambia; nadie más ordena los
      proyectos de un grupo (se mira `armar_pagina`, sacado del código).
  O7  Buscar con `?q=` acorta la lista sin cambiarle el orden.
  O8  Lo que NO se reordena: el selector «Meter en un proyecto» sigue alfabético.
  O9  El proyecto que se enseña cuando la dirección no dice cuál es el primero de la lista (ahora el
      que más pendientes tiene del primer grupo).

Frontera: no hay navegador; el orden se lee del HTML que sirve la ruta.
Correr:  python3 -m pytest tests/test_orden_por_pendientes.py -q
"""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import ast
import re
from pathlib import Path

import pytest

import config
import db.db as db
from test_pagina_proyectos import _dia, gente, mundo, ver, titulo_de, modelo_de_filas  # noqa: F401
from test_buzon_que_no_se_ve import _py_en_disco   # DESPUÉS de la línea de arriba: importado antes, rompe starlette
import test_proyectos_solo_ver as sv
import web.avisos  # noqa: F401

RAIZ = Path(__file__).resolve().parent.parent


def _izquierda(html: str) -> dict:
    """{grupo: {"abiertos": [ids], "cerrados": [ids]}} de la columna de la izquierda."""
    nav = html.split('<nav class="grupos"', 1)[1].split("</nav>", 1)[0]
    salida = {}
    for bloque in nav.split('<div class="grupo gc" data-g="')[1:]:
        grupo = bloque.split('"', 1)[0]
        abiertos, cerrados = [], []
        for m in re.finditer(r'<a class="proy ([^"]*)"[^>]*href="/proyectos\?p=(\d+)', bloque):
            (cerrados if "cerrado" in m.group(1).split() else abiertos).append(int(m.group(2)))
        salida[grupo] = {"abiertos": abiertos, "cerrados": cerrados}
    return salida


def _estado(html: str) -> dict:
    """{grupo: [(id, n pendientes)]} de la vista «Estado de los proyectos», en el orden pintado."""
    centro = html.split('<main data-region="centro">', 1)[1].split("</main>", 1)[0]
    salida = {}
    for sec in centro.split('<section class="bloque"')[1:]:
        grupo = re.search(r"<h2>(.*?)</h2>", sec).group(1)
        salida[grupo] = [(int(i), int(n)) for i, n in re.findall(
            r'<a class="nombre" href="/proyectos\?p=(\d+)".*?<span class="cuenta-p">(\d+) pendiente', sec, re.S)]
    return salida


def _sembrar(mundo):
    """CDS: cinco abiertos con 0, 1, 2, 2 y 3 pendientes (dos empatados, uno pausado), más dos cerrados
    con pendientes (el de nombre «Zeta» con 9 y el de nombre «Alfa cerrado» con 0). ACD: tres con 1."""
    mundo.proyecto(1, "Cero", area="CDS")
    mundo.proyecto(2, "Uno", area="CDS")
    mundo.proyecto(3, "Dos b", area="CDS")
    mundo.proyecto(4, "dos a", area="CDS", estado="pausado")
    mundo.proyecto(5, "Tres", area="CDS")
    mundo.proyecto(6, "Zeta", area="CDS", estado="cerrado")
    mundo.proyecto(7, "Alfa cerrado", area="CDS", estado="cerrado")
    n = 100
    for pid, cuantas in ((2, 1), (3, 2), (4, 2), (5, 3), (6, 9)):
        for _ in range(cuantas):
            n += 1
            mundo.tarea(n, f"t{n}", proyecto=pid)
    n += 1
    mundo.tarea(n, "hecha de Cero", proyecto=1, estado="hecha", completado=_dia(-1))
    n += 1
    mundo.tarea(n, "descartada de Tres", proyecto=5, estado="descartada")
    mundo.proyecto(10, "Beta", area="ACD")
    mundo.proyecto(11, "Alfa", area="ACD")
    mundo.proyecto(12, "Gamma", area="ACD")
    for pid in (10, 11, 12):
        n += 1
        mundo.tarea(n, f"t{n}", proyecto=pid)
    mundo.proyecto(20, "Suelto b", area=None)
    mundo.proyecto(21, "Suelto a", area=None)
    n += 1
    mundo.tarea(n, f"t{n}", proyecto=20)
    n += 1
    mundo.tarea(n, f"t{n}", proyecto=20)


def test_O1_los_abiertos_salen_del_que_mas_pendientes_tiene_al_que_menos(mundo):
    _sembrar(mundo)
    izq = _izquierda(ver(mundo))
    # 5 (3 pend.) · 4 «dos a» (2, pausado) y 3 «Dos b» (2) · 2 (1) · 1 (0 pend., solo una hecha)
    assert izq["cds"]["abiertos"] == [5, 4, 3, 2, 1]
    assert izq["acd"]["abiertos"] == [11, 10, 12]            # los tres tienen 1: por nombre
    assert izq["sin-grupo"]["abiertos"] == [20, 21]          # 2 pendientes antes que 0


def test_O2_el_empate_es_por_nombre_y_luego_por_id_y_no_depende_de_como_se_sembro(mundo):
    mundo.proyecto(9, "Igual", area="CDS")
    mundo.proyecto(3, "igual", area="CDS")
    mundo.proyecto(5, "IGUAL", area="CDS")
    mundo.proyecto(7, "Mas", area="CDS")
    mundo.tarea(101, "a", proyecto=7)
    primera = _izquierda(ver(mundo))["cds"]["abiertos"]
    assert primera == [7, 3, 5, 9]                           # Mas (1); luego «igual» ×3 por id
    assert _izquierda(ver(mundo))["cds"]["abiertos"] == primera


@pytest.mark.parametrize("llegan", [(9, 3, 5), (5, 9, 3), (3, 5, 9)])
def test_O2_con_el_mismo_nombre_gana_el_id_llegue_la_lista_como_llegue(llegan):
    """Postgres no promete el orden de `SELECT ... FROM proyectos` (no lleva ORDER BY): el desempate final
    es el `id`, no el sitio de la fila. (SQLite devuelve por id y no lo ve: se arma el modelo a mano.)"""
    m = modelo_de_filas(proyectos=[dict(id=i, nombre="Igual", area="CDS") for i in llegan])
    cds = next(g for g in m["grupos"] if g["clave"] == "CDS")
    assert [p["id"] for p in cds["abiertos"]] == [3, 5, 9]


def test_O3_los_cerrados_siguen_por_nombre_y_aparte(mundo):
    _sembrar(mundo)
    izq = _izquierda(ver(mundo))
    assert izq["cds"]["cerrados"] == [7, 6]                  # «Alfa cerrado» antes que «Zeta» (que tiene 9)
    assert not set(izq["cds"]["cerrados"]) & set(izq["cds"]["abiertos"])


def test_O4_la_izquierda_la_vista_de_estado_y_solo_ver_dicen_el_mismo_orden(mundo):
    _sembrar(mundo)
    izq = _izquierda(ver(mundo))
    quiere = {"CDS": izq["cds"]["abiertos"] + izq["cds"]["cerrados"],
              "ACD": izq["acd"]["abiertos"] + izq["acd"]["cerrados"],
              "Sin grupo": izq["sin-grupo"]["abiertos"] + izq["sin-grupo"]["cerrados"]}
    estado = _estado(ver(mundo, estado=1))
    assert {g: [i for i, _ in ps] for g, ps in estado.items()} == quiere
    ver_solo = sv.cliente("ver").get("/proyectos")
    assert ver_solo.status_code == 200
    assert _izquierda(ver_solo.text) == izq


def test_O5_el_N_pendientes_que_se_ve_baja_de_renglon_en_renglon_en_los_abiertos(mundo):
    _sembrar(mundo)
    estado = _estado(ver(mundo, estado=1))
    cds = estado["CDS"]
    assert [n for _, n in cds[:5]] == [3, 2, 2, 1, 0]
    for grupo, filas in estado.items():
        cuentas = [n for _, n in filas]
        abiertos = cuentas[: len(_izquierda(ver(mundo))[{"CDS": "cds", "ACD": "acd", "Sin grupo": "sin-grupo"}[grupo]]["abiertos"])]
        assert abiertos == sorted(abiertos, reverse=True), (grupo, abiertos)


def test_O6_la_pagina_toma_el_orden_de_la_puerta_y_de_ninguna_otra_parte(mundo, monkeypatch):
    _sembrar(mundo)
    real = db.orden_en_la_lista
    monkeypatch.setattr(db, "orden_en_la_lista", lambda m: tuple(-x if isinstance(x, int) else x
                                                                 for x in real(m)))
    cambiada = _izquierda(ver(mundo))
    assert cambiada["cds"]["abiertos"] != [5, 4, 3, 2, 1]
    assert _estado(ver(mundo, estado=1))["CDS"][0][0] == cambiada["cds"]["abiertos"][0]


def test_O6_nadie_mas_ordena_los_proyectos_de_un_grupo():
    """`armar_pagina` es quien pinta el grupo: dentro, el ÚNICO `sorted` que toca proyectos usa la puerta."""
    fuente = (RAIZ / "db" / "db.py").read_text(encoding="utf-8")
    armar = next(n for n in ast.walk(ast.parse(fuente))
                 if isinstance(n, ast.FunctionDef) and n.name == "armar_pagina")
    sorteos = [n for n in ast.walk(armar)
               if isinstance(n, ast.Call) and getattr(n.func, "id", None) in ("sorted", "sort")]
    con_proyectos = [ast.get_source_segment(fuente, n) for n in sorteos
                     if "modelo_proyectos" in (ast.get_source_segment(fuente, n) or "")]
    assert len(con_proyectos) == 1 and "orden_en_la_lista" in con_proyectos[0], con_proyectos
    # y la puerta se usa como `key=`, no se llama a mano en ningún otro sitio de producción
    for p in _py_en_disco(RAIZ):           # LA puerta de los barridos del repositorio
        if p.relative_to(RAIZ).parts[0] == "tests":
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call):
                f = n.func
                usado = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                assert usado != "orden_en_la_lista", (p, n.lineno)


def test_O7_buscar_acorta_la_lista_sin_cambiarle_el_orden(mundo):
    _sembrar(mundo)
    mundo.proyecto(30, "Dos c", area="CDS")
    mundo.tarea(300, "x", proyecto=30)
    mundo.tarea(301, "y", proyecto=30)
    todo = _izquierda(ver(mundo))["cds"]["abiertos"]
    con_q = _izquierda(ver(mundo, q="dos"))["cds"]["abiertos"]
    assert con_q == [i for i in todo if i in {3, 4, 30}] and len(con_q) == 3


def test_O8_el_selector_de_meter_en_un_proyecto_sigue_alfabetico(mundo):
    _sembrar(mundo)
    mundo.tarea(500, "suelta", area="CDS")
    html = ver(mundo, g="CDS", t=500)
    sel = html.split('name="proyecto" required>', 1)[1].split("</select>", 1)[0]
    nombres = re.findall(r"CDS · ([^<]*)</option>", sel)
    assert nombres == sorted(nombres, key=str.casefold) and nombres[0] == "Cero" and "Zeta" not in nombres


def test_O9_sin_decir_cual_se_enseña_el_primero_de_la_lista(mundo):
    mundo.proyecto(1, "Alfa", area="CDS")
    mundo.proyecto(2, "Zeta", area="CDS")
    mundo.tarea(20, "t", proyecto=2)
    assert titulo_de(ver(mundo)) == "Zeta"                   # el que tiene pendientes, no el primero por nombre
    assert titulo_de(ver(mundo, p=999)) == "Zeta"


def test_la_puerta_con_entradas_inventadas():
    base = {"cerrado": False, "n_pendientes": 0, "nombre": "A", "id": 1}
    assert db.orden_en_la_lista({**base, "n_pendientes": 4}) < db.orden_en_la_lista(base)
    assert db.orden_en_la_lista({**base, "n_pendientes": 4, "cerrado": True}) == db.orden_en_la_lista(
        {**base, "cerrado": True})
    assert db.orden_en_la_lista({**base, "nombre": "b"}) < db.orden_en_la_lista({**base, "nombre": "C"})
