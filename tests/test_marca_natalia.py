# -*- coding: utf-8 -*-
"""La marca de Natalia en la página de Proyectos (19-oct-2026, pedido de Tiziano: «podemos usar esto como un
pequeñito icono al lado de lo que Natalia lee?»).

Qué vigila:
  M1  La lista de qué lee Natalia (`web/marca_natalia.py`: `LEE` y `NO_LEE`) cubre EXACTAMENTE los campos que
      manda la ruta REAL `GET /api/code/clientes/{noco_id}/proyectos` (se llama la ruta y se aplanan sus llaves):
      un campo nuevo en la ruta, o uno que sale de ella, pone esta prueba en rojo hasta decidir si se marca.
      Y lo que se marca es lo dicho por el mapa de Natalia: nombre, estado y la tarea siguiente (título y fecha).
  M2  La página marca, en cada sitio donde pinta uno de esos datos, y SOLO ahí: cada marca lleva en
      `data-natalia` un campo de `LEE`, no hay marca sin ese atributo, y por proyecto las marcas son las que la
      ruta dice (los proyectos que la ruta devuelve, con sus campos; los que no devuelve —sin cliente— ninguna).
  M3  La tarea siguiente: solo la fila de la tarea que la ruta llama `siguiente` lleva marca (título y fecha); ni
      otra pendiente, ni una hecha, ni una suelta. Al terminarla, la marca pasa a la nueva siguiente, la que la
      ruta dice. Con un filtro que esconde esa fila, ninguna fila lleva marca.
  M4  El icono va dentro de la hoja (sin pedir nada afuera), pesa poco, es el PNG de 64x64 y el texto es el pedido.

CAMINO DE PRODUCCIÓN: la ruta real del panel y la ruta real de Natalia, una sola base (SQLite, con el SQL real de
`db.proyectos_de_cliente` y `db.pagina_de_proyectos`). Lo único que no está en la base es la lista de qué lee
Natalia: viene del mapa de Natalia (sin comprobar desde Lucy) y por eso M1 la ata a la ruta real campo por campo.

Correr:  python3 -m pytest tests/test_marca_natalia.py -q
"""
from __future__ import annotations

import base64
import os
import re
import struct

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import pytest

import config
import db.db as db
import test_pagina_proyectos as tp
from test_pagina_proyectos import _dia, gente, mundo, ver  # noqa: F401
import web.api_code as api_code
import web.marca_natalia as marca
import web.app as panel

CLAVE = "clave-de-prueba-marca-natalia-no-es-real"
FICHA = 7                       # la que `Mundo.proyecto(cliente=…)` le pone a los proyectos con cliente

# Lo que dijo el mapa de Natalia que le llega al cliente (sin comprobar desde Lucy). Está escrito aparte de
# `marca.LEE` a propósito: M1 exige que las dos digan lo mismo.
LO_DICHO_POR_EL_MAPA = {"nombre", "estado", "siguiente.titulo", "siguiente.fecha"}


@pytest.fixture(autouse=True)
def _la_casa_y_la_clave(monkeypatch):
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {config.CHAT_ID_DUENO: "Dueño"})
    monkeypatch.setattr(config, "CLAVES_API_CODE", {CLAVE: "natalia"})
    monkeypatch.setattr(config, "PERMISOS_API_CODE", {"natalia": frozenset({"proyectos:leer_de_cliente"})})
    monkeypatch.setattr(api_code, "_registrar_clave_mala", lambda: None)
    api_code._pedidos_por_quien.clear()


def sembrar(m):
    """Un proyecto con cliente y varias tareas, uno sin cliente, uno cerrado con cliente, y suelta."""
    m.proyecto(1, "Disco Uno", area="CDS", cliente="Cliente X")
    m.proyecto(2, "Sin cliente", area="CDS")
    m.proyecto(3, "Cerrado Tres", area="CDS", cliente="Cliente X", estado="cerrado")
    m.proyecto(4, "Otro con cliente", area="ACD", cliente="Cliente X")
    m.tarea(10, "Futura", proyecto=1, vence=_dia(5))
    m.tarea(11, "Vencida", proyecto=1, vence=_dia(-2))
    m.tarea(12, "Sin fecha", proyecto=1, responsable=config.CHAT_ID_DUENO)
    m.tarea(13, "Ya hecha", proyecto=1, estado="hecha", completado=_dia(-1))
    m.tarea(20, "Pendiente del sin cliente", proyecto=2, vence=_dia(2))
    m.tarea(40, "Unica de Otro", proyecto=4, vence=_dia(1))
    m.tarea(50, "Suelta de CDS", area="CDS", vence=_dia(-1))
    m.tarea(51, "Suelta sin grupo", vence=_dia(1))


def de_la_ruta():
    """La respuesta REAL de la ruta de Natalia para la ficha."""
    r = tp.TestClient(panel.app).get(f"/api/code/clientes/{FICHA}/proyectos",
                                     headers={"Authorization": f"Bearer {CLAVE}"})
    assert r.status_code == 200, r.text
    return r.json()


def llaves(cuerpo: dict) -> set[str]:
    """Todos los caminos de llaves de la respuesta (`siguiente.titulo`), de la ruta tal cual. Un campo que en algún
    proyecto trae un objeto se cuenta por sus llaves de dentro (en los que trae `null` no se sabe más)."""
    salida = set()
    for k, v in cuerpo.items():
        if k == "noco_id":
            continue
        if k == "proyectos":
            con_objeto = {kk for p in v for kk, vv in p.items() if isinstance(vv, dict)}
            for p in v:
                for kk, vv in p.items():
                    if isinstance(vv, dict):
                        salida |= {f"{kk}.{x}" for x in vv}
                    elif kk not in con_objeto:
                        salida.add(kk)
        else:
            salida.add(k)
    return salida


MARCA = re.compile(r'<[a-z]+\b[^>]*class="marca-natalia"[^>]*>')


def marcas(html: str) -> list[str]:
    """Los campos (`data-natalia`) de cada marca; '' si una marca no lo trae."""
    return [(re.search(r'data-natalia="([^"]*)"', t) or [None, ""])[1] for t in MARCA.findall(html)]


def centro(html: str) -> str:
    return html.split('<main data-region="centro">', 1)[1].split("</main>", 1)[0]


def filas_de_tarea(html: str) -> dict[int, str]:
    """{id de la tarea: el texto de su bloque}, desde su `id="tarea-N"` hasta la siguiente."""
    trozos = re.split(r'(?=<div class="tarea [^"]*" id="tarea-\d+")', centro(html))
    return {int(re.search(r'id="tarea-(\d+)"', t).group(1)): t for t in trozos if 'id="tarea-' in t[:200]}


# ═══════════════════════════════════════════════════════════════════════
# M1: la lista está atada a la ruta real
# ═══════════════════════════════════════════════════════════════════════

def test_M1_LEE_y_NO_LEE_son_exactamente_los_campos_de_la_ruta_real(mundo):
    sembrar(mundo)
    cuerpo = de_la_ruta()
    assert any(p["siguiente"] for p in cuerpo["proyectos"]), "la prueba necesita un proyecto con tarea siguiente"
    assert llaves(cuerpo) == set(marca.LEE) | set(marca.NO_LEE)
    assert not (set(marca.LEE) & set(marca.NO_LEE))


def test_M1_lo_que_se_marca_es_lo_que_dijo_el_mapa_de_Natalia():
    assert set(marca.LEE) == LO_DICHO_POR_EL_MAPA


def test_M1_la_ruta_inventa_un_campo_y_la_prueba_lo_ve(mundo, monkeypatch):
    """Frontera de M1: un campo nuevo que la ruta empiece a mandar sale en las llaves y rompe la igualdad."""
    sembrar(mundo)
    cuerpo = de_la_ruta()
    cuerpo["proyectos"][0]["campo_nuevo"] = "x"
    assert llaves(cuerpo) != set(marca.LEE) | set(marca.NO_LEE)


# ═══════════════════════════════════════════════════════════════════════
# M2: la página marca lo que la ruta manda y lo que Natalia lee, y nada más
# ═══════════════════════════════════════════════════════════════════════

def test_M2_toda_marca_trae_un_campo_que_natalia_lee(mundo):
    sembrar(mundo)
    for consulta in ({"p": 1}, {"p": 3}, {"estado": 1}, {"g": "CDS"}, {"sin_grupo": 1}, {}):
        html = ver(mundo, **consulta)
        assert "" not in marcas(html), consulta
        assert set(marcas(html)) <= set(marca.LEE), (consulta, marcas(html))


def test_M2_un_proyecto_sin_cliente_no_lleva_ninguna_marca(mundo):
    sembrar(mundo)
    assert 2 not in [p["id"] for p in de_la_ruta()["proyectos"]]
    html = ver(mundo, p=2)
    assert "Sin cliente" in centro(html)
    # el centro es SU proyecto: ninguna marca; la lista de la izquierda marca a los otros, no a este
    assert marcas(centro(html)) == []
    izquierda = html.split('<nav class="grupos"', 1)[1].split("</nav>", 1)[0]
    renglon = re.search(r'<a class="proy[^"]*"[^>]*href="/proyectos\?p=2"[^>]*>(.*?)</a>', izquierda, re.S).group(1)
    assert marcas(renglon) == []


def test_M2_la_vista_de_estado_marca_por_proyecto_lo_que_la_ruta_manda(mundo):
    sembrar(mundo)
    cuerpo = de_la_ruta()
    por_ruta = {p["id"]: p for p in cuerpo["proyectos"]}
    html = ver(mundo, estado=1)
    renglones = re.findall(r'<li>\s*<a class="nombre" href="/proyectos\?p=(\d+)">(.*?)</li>', centro(html), re.S)
    assert {int(i) for i, _ in renglones} == {1, 2, 3, 4}
    for pid, texto in renglones:
        pid = int(pid)
        esperado = []
        if pid in por_ruta:
            esperado = ["nombre", "estado"]
            sig = por_ruta[pid]["siguiente"]
            if sig:
                esperado += ["siguiente.titulo"]
                if sig["fecha"] and not sig["vencida"]:        # la fecha vencida no llega a Natalia (su mapa, N1)
                    esperado += ["siguiente.fecha"]
        assert sorted(marcas(texto)) == sorted(esperado), (pid, marcas(texto), esperado)


def test_M2_en_el_proyecto_marca_nombre_y_estado_una_vez_en_la_cabecera(mundo):
    sembrar(mundo)
    html = ver(mundo, p=1)
    cabeza = centro(html).split('<div class="cuerpo">', 1)[0]
    assert sorted(marcas(cabeza)) == ["estado", "nombre"]
    # el nombre queda junto al título y el estado junto a su pastilla
    assert re.search(r"Disco Uno<span class=\"marca-natalia\"[^>]*data-natalia=\"nombre\"", cabeza)
    assert re.search(r'<span class="pastilla">En curso</span><span class="marca-natalia"[^>]*data-natalia="estado"', cabeza)


def test_M2_la_lista_de_la_izquierda_marca_el_nombre_de_los_proyectos_que_la_ruta_manda(mundo):
    sembrar(mundo)
    ids = {p["id"] for p in de_la_ruta()["proyectos"]}
    html = ver(mundo, p=1)
    izquierda = html.split('<nav class="grupos"', 1)[1].split("</nav>", 1)[0]
    for pid in (1, 2, 3, 4):
        r = re.search(rf'<a class="proy[^"]*"[^>]*href="/proyectos\?p={pid}"[^>]*>(.*?)</a>', izquierda, re.S)
        assert r, pid
        assert marcas(r.group(1)) == (["nombre"] if pid in ids else []), pid


# ═══════════════════════════════════════════════════════════════════════
# M3: la tarea que sigue
# ═══════════════════════════════════════════════════════════════════════

def test_M3_solo_la_fila_de_la_tarea_siguiente_de_la_ruta_lleva_marca(mundo):
    sembrar(mundo)
    siguiente = {p["id"]: p["siguiente"] for p in de_la_ruta()["proyectos"]}[1]["tarea_id"]
    assert siguiente == 11                      # la vencida: la misma regla que la página
    filas = filas_de_tarea(ver(mundo, p=1))
    assert {10, 11, 12, 13} <= set(filas)
    # vencida: lleva la marca del título y NO la de la fecha (la fecha vieja no llega a Natalia)
    for tid, fila in filas.items():
        assert sorted(marcas(fila)) == (["siguiente.titulo"] if tid == siguiente else []), tid


def test_M3_la_fecha_lleva_marca_solo_si_la_siguiente_no_esta_vencida(mundo):
    sembrar(mundo)
    cuerpo = {p["id"]: p["siguiente"] for p in de_la_ruta()["proyectos"]}
    assert cuerpo[1]["vencida"] is True and cuerpo[4]["vencida"] is False and cuerpo[4]["fecha"]
    # fila: vencida (proyecto 1) sin marca de fecha; no vencida y con fecha (proyecto 4) con ella
    assert "siguiente.fecha" not in marcas(filas_de_tarea(ver(mundo, p=1))[11])
    assert sorted(marcas(filas_de_tarea(ver(mundo, p=4))[40])) == ["siguiente.fecha", "siguiente.titulo"]
    # vista de estado: lo mismo en el «Próxima: …» de cada uno
    renglones = dict(re.findall(r'<li>\s*<a class="nombre" href="/proyectos\?p=(\d+)">(.*?)</li>',
                                centro(ver(mundo, estado=1)), re.S))
    assert "siguiente.fecha" not in marcas(renglones["1"]) and "siguiente.titulo" in marcas(renglones["1"])
    assert "siguiente.fecha" in marcas(renglones["4"])
    # una siguiente sin fecha: el título sí, la fecha no
    mundo.proyecto(5, "Solo sin fecha", area="CDS", cliente="Cliente X")
    mundo.tarea(60, "Sin fecha sola", proyecto=5)
    assert {p["id"]: p["siguiente"] for p in de_la_ruta()["proyectos"]}[5]["fecha"] is None
    assert sorted(marcas(filas_de_tarea(ver(mundo, p=5))[60])) == ["siguiente.titulo"]


def test_M3_las_sueltas_no_llevan_marca_en_sus_filas(mundo):
    sembrar(mundo)
    for consulta in ({"g": "CDS"}, {"sin_grupo": 1}):
        filas = filas_de_tarea(ver(mundo, **consulta))
        assert all(marcas(f) == [] for f in filas.values()), consulta
    assert 50 in filas_de_tarea(ver(mundo, g="CDS")) and 51 in filas_de_tarea(ver(mundo, sin_grupo=1))


def test_M3_al_terminar_la_siguiente_la_marca_pasa_a_la_que_dice_la_ruta(mundo):
    sembrar(mundo)
    mundo.con.execute("UPDATE tareas SET estado = 'hecha', completado_en = ? WHERE id = 11",
                      (_dia(0).isoformat(),))
    siguiente = {p["id"]: p["siguiente"] for p in de_la_ruta()["proyectos"]}[1]["tarea_id"]
    assert siguiente == 10
    filas = filas_de_tarea(ver(mundo, p=1))
    assert [tid for tid, f in filas.items() if marcas(f)] == [10]


def test_M3_con_un_filtro_que_esconde_la_siguiente_ninguna_fila_lleva_marca(mundo):
    sembrar(mundo)
    html = ver(mundo, p=1, filtro="mias")             # solo la 12 (del dueño); la siguiente es la 11
    filas = filas_de_tarea(html)
    assert 12 in filas and 11 not in filas
    assert all(marcas(f) == [] for f in filas.values())


def test_M3_el_proyecto_cerrado_marca_su_siguiente_solo_si_la_ruta_la_manda(mundo):
    sembrar(mundo)
    mundo.tarea(30, "Pendiente en el cerrado", proyecto=3, vence=_dia(3))
    por_ruta = {p["id"]: p for p in de_la_ruta()["proyectos"]}
    tiene = bool(por_ruta[3]["siguiente"])
    filas = filas_de_tarea(ver(mundo, p=3))
    assert (30 in filas) and (bool(marcas(filas[30])) == tiene)


# ═══════════════════════════════════════════════════════════════════════
# M4: el icono
# ═══════════════════════════════════════════════════════════════════════

def test_M4_el_icono_va_dentro_de_la_hoja_pesa_poco_y_es_el_png_de_64():
    crudo = base64.b64decode(marca.ICONO.split(",", 1)[1])
    assert marca.ICONO.startswith("data:image/png;base64,")
    assert crudo[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", crudo[16:24]) == (64, 64)
    assert len(crudo) < 5000


def test_M4_la_pagina_lleva_el_icono_una_vez_y_el_texto_pedido(mundo):
    sembrar(mundo)
    html = ver(mundo, estado=1)
    assert html.count(marca.ICONO) == 1                       # en la hoja de estilos, no en cada marca
    assert "Natalia se lo puede contar al cliente" == marca.TEXTO
    assert all(f'title="{marca.TEXTO}"' in t and f'aria-label="{marca.TEXTO}"' in t for t in MARCA.findall(html))
    regla = re.search(r"\.marca-natalia\{[^}]*\}", html).group(0)
    assert "16px" in regla and "border-radius" in regla
    assert not re.search(r"https?://", regla)                 # nada de afuera


def test_M4_el_archivo_del_repo_no_lleva_nada_de_clientes_ni_claves():
    fuente = open(marca.__file__, encoding="utf-8").read()
    assert not re.search(r"(?i)bearer|api[_-]?key|secret|token", fuente)
