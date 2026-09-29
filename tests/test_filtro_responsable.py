# -*- coding: utf-8 -*-
"""Clasificar las tareas por responsable en /tareas (tarea 145 de Tiziano,
aprobada el 28-sep-2026).

QUÉ SE VIGILA, siempre por la ruta real (`panel.tareas`, `panel.guardar_tareas`)
sobre la base de mentira de `test_tarea_a_mano.py`/`test_alta_con_responsable.py`:

  · Sin parámetro, la pantalla es la de siempre (se entra en «Todas»).
  · Cada botón muestra exactamente las filas de su clase, y los botones, todos
    juntos, cubren CADA fila una sola vez (sin responsable, cada persona, Code,
    alguien que ya no entra pero tiene nombre y «Otros»). Los botones se leen de
    la pantalla y salen de `config`; una tercera persona inventada aparece sola.
  · Un nombre desconocido o ambiguo da «todas» con aviso, nunca una pantalla
    vacía; el filtro se lee por NOMBRE y jamás por chat.
  · Guardar con filtro conserva el filtro (revalidado) y NO toca lo que no se ve
    ni borra responsables: pinta filtrado → manda el formulario tal cual → lee
    la base.
  · «+ Agregar tarea» llega con el responsable escogido; el Historial no lleva
    botones; los botones son enlaces, no un segundo formulario.

Límite dicho: la base es de mentira (no ve nada de lo que Postgres haría con la
consulta); esta función no toca SQL, filtra en Python las filas que trae
`db.tareas_por_grupo`.

Correr:  python3 -m pytest tests/test_filtro_responsable.py
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_alta_con_responsable as A  # noqa: E402
from test_alta_con_responsable import base  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402

DUENO, OTRA = A.DUENO, A.OTRA
CODE = config.CHAT_ID_CODE


def _pintar(conn, responsable=""):
    return base._con_base(conn, lambda: panel.tareas(
        base._get("/tareas"), responsable=responsable)).body.decode()


def _ids(html):
    """Las tareas que la pantalla pinta: las que traen su `prev_resp_<id>`."""
    return sorted(int(i) for i in re.findall(r'name="prev_resp_(\d+)"', html))


def _botones(html):
    """[(clave, etiqueta, n, activo)] leídos de la pantalla, tal cual. El botón
    ACTIVO no es un enlace, así que su clave sale vacía: se lo distingue por
    `activo` y por su etiqueta."""
    m = re.search(r'id="por-responsable">(.*?)</p>', html, re.S)
    assert m, "no hay fila de botones"
    out = []
    for activo, href, etiqueta, n in re.findall(
            r'(<strong aria-current="page">)?(?:<a href="/tareas(?:\?responsable=([^"]*))?">)?'
            r'([^<(]+?) \((\d+)\)', m.group(1)):
        out.append((unquote(href or ""), etiqueta.strip(), int(n), bool(activo)))
    return out


# La clase de cada tarea de `A._mundo()`: 1 Zutana, 2 Mengano, 3 Code, 4 sin
# responsable, 5 alguien sin nombre que ya no entra, 6 alguien CON nombre que
# ya no entra, 7 una hecha de Mengano.
ESPERADO = {"": [1, 2, 3, 4, 5, 6, 7], "Zutana": [1], "Mengano": [2, 7],
            "Code": [3], "_sin": [4], "_otros": [5], "Jubilada": [6]}


def test_sin_parametro_es_la_pantalla_de_siempre_y_entra_en_todas():
    conn = A._mundo()
    html = _pintar(conn)
    assert _ids(html) == ESPERADO[""]
    activos = [b for b in _botones(html) if b[3]]
    assert [b[1] for b in activos] == ["Todas"], f"no se entra en «Todas»: {activos}"
    assert "Viendo:" not in html, "avisa de un filtro que no hay"


def test_cada_boton_muestra_exactamente_sus_filas():
    conn = A._mundo()
    for clave, ids in ESPERADO.items():
        assert _ids(_pintar(conn, clave)) == ids, f"filtro {clave!r}"


def test_los_botones_cubren_cada_fila_una_sola_vez_y_salen_de_lo_real():
    conn = A._mundo()
    html = _pintar(conn)
    botones = [b for b in _botones(html) if b[1] != "Todas"]
    vistas = []
    for clave, _, n, _ in botones:
        ids = _ids(_pintar(conn, clave))
        assert len(ids) == n, f"el botón {clave!r} dice {n} y pinta {len(ids)}"
        vistas += ids
    assert sorted(vistas) == ESPERADO[""], (
        f"la unión de los botones no cubre cada fila una vez: {sorted(vistas)}")
    assert [b[2] for b in _botones(html) if b[1] == "Todas"] == [7], (
        "«Todas» no cuenta lo que hay en pantalla")


def test_los_botones_salen_de_config_y_una_tercera_persona_aparece_sola():
    A._casa({DUENO: "Zutana", OTRA: "Mengano", A.TERCERA: "Fulanita"})
    conn = base._Conn(areas=A.AREAS)
    claves = [b[0] for b in _botones(_pintar(conn))]
    assert claves == ["", "Zutana", "Mengano", "Fulanita", "Code", "_sin"], claves
    # Sin nadie en la variable siguen Code y «Sin responsable».
    A._casa({}, permitidos=(DUENO,))
    claves = [b[0] for b in _botones(_pintar(conn))]
    assert claves == ["", "Code", "_sin"], claves


def test_otros_solo_sale_si_hay_alguien_sin_nombre():
    A._casa()
    claves = [b[0] for b in _botones(_pintar(base._Conn(areas=A.AREAS)))]
    assert "_otros" not in claves
    assert "_otros" in [b[0] for b in _botones(_pintar(A._mundo()))]


def test_el_filtro_ignora_mayusculas_y_tildes_como_telegram():
    conn = A._mundo()
    assert _ids(_pintar(conn, " mengano ")) == [2, 7]
    assert _ids(_pintar(conn, "CODE")) == [3]
    # Y el botón que queda activo lleva el nombre canónico.
    assert [b[1] for b in _botones(_pintar(conn, "mengano")) if b[3]] == ["Mengano"]


def test_un_nombre_desconocido_o_ambiguo_da_todas_con_aviso_nunca_vacio():
    conn = A._mundo()
    for crudo in ("Nadie", "Men", str(OTRA), '"><script>alert(1)</script>'):
        html = _pintar(conn, crudo)
        assert _ids(html) == ESPERADO[""], f"{crudo!r}: no muestra todas"
        assert "te\nmuestro todas" in html or "te muestro todas" in html
        assert "<script>alert(1)" not in html, "reflejó el texto sin escapar"
        assert 'name="filtro" value=""' in html, "coló el texto en el campo oculto"
    A._casa({DUENO: "Igual", OTRA: "igual"})
    conn = base._Conn(areas=A.AREAS)
    html = _pintar(conn, "igual")
    assert "te muestro todas" in html.replace("\n", " ")


def test_la_vista_filtrada_dice_cuantas_ve_de_cuantas_y_las_cuentas_cuadran():
    conn = A._mundo()
    html = _pintar(conn, "Mengano")
    m = re.search(r"Viendo: <strong>([^<]*)</strong>\s*—\s*(\d+) de (\d+)", html)
    assert m, "falta la línea «Viendo»"
    assert m.group(1) == "Mengano"
    assert int(m.group(2)) == len(_ids(html)) == 2
    assert int(m.group(3)) == 7


def test_una_vista_vacia_no_dice_que_no_hay_ninguna_tarea():
    A._casa()
    conn = base._Conn(tareas=[A._fila(1, DUENO)], areas=A.AREAS)
    html = _pintar(conn, "Mengano")
    assert "No hay tareas en esta vista" in html
    assert "No hay ninguna tarea" not in html
    assert "No hay ninguna tarea" in _pintar(base._Conn(areas=A.AREAS), "Mengano")


# ── Lo que nunca debe verse ni cambiar ───────────────────────────────────

def test_ningun_chat_sale_como_texto_ni_en_los_enlaces():
    conn = A._mundo()
    chats = [DUENO, OTRA, A.EXPULSADO, A.JUBILADA]   # el de Code (-1) no se busca como texto
    for crudo in ("", "Mengano", "_otros", "Jubilada"):
        html = _pintar(conn, crudo)
        sin_valores = re.sub(r'value="[^"]*"', 'value=""', html)
        for chat in chats:
            assert str(chat) not in sin_valores, f"{crudo!r}: se ve {chat}"
        assert f"responsable={CODE}" not in html
        for href in re.findall(r'href="([^"]*)"', html):
            for chat in chats:
                assert str(chat) not in href, f"un enlace lleva un chat: {href}"


def test_un_solo_formulario_y_los_botones_son_enlaces():
    ruta = os.path.join(base.RAIZ, "web", "plantillas", "tareas.html")
    plantilla = open(ruta, encoding="utf-8").read()
    assert plantilla.count("<form") == 1
    html = _pintar(A._mundo(), "Mengano")
    fila = re.search(r'id="por-responsable">(.*?)</p>', html, re.S).group(1)
    assert "<a href=" in fila and "<form" not in fila and "<button" not in fila


def test_agregar_tarea_lleva_el_responsable_y_el_alta_lo_preescoge():
    conn = A._mundo()
    assert 'href="/tareas/nueva"' in _pintar(conn)
    html = _pintar(conn, "mengano")
    assert 'href="/tareas/nueva?responsable=Mengano"' in html
    assert 'href="/tareas/nueva?responsable=_sin"' in _pintar(conn, "_sin")
    # De punta a punta: el enlace que se pintó, seguido, marca a esa persona.
    destino = re.search(r'href="/tareas/nueva\?responsable=([^"]*)"', html).group(1)
    marcadas = A._leer(A._pintar_alta(responsable=unquote(destino))).marcadas
    assert marcadas["responsable"] == [str(OTRA)]


def test_el_historial_no_lleva_botones():
    A._casa()
    conn = base._Conn(areas=A.AREAS)
    html = base._con_base(conn, lambda: panel.tareas_historial(
        base._get("/tareas/historial"))).body.decode()
    assert "por-responsable" not in html and "responsable=" not in html


def test_el_campo_oculto_lleva_el_filtro_canonico():
    conn = A._mundo()
    assert 'name="filtro" value="Mengano"' in _pintar(conn, "mengano")
    assert 'name="filtro" value=""' in _pintar(conn)
    assert 'name="filtro" value="_sin"' in _pintar(conn, "_sin")


# ── Guardar con un filtro puesto ─────────────────────────────────────────

def _guardar(conn, campos):
    return base._con_base(conn, lambda: panel.guardar_tareas(base._post(campos)))


def test_guardar_conserva_el_filtro_ya_validado_y_descarta_lo_hostil():
    conn = A._mundo()
    campos = dict(A._leer(_pintar(conn, "Mengano")).campos)
    loc = _guardar(conn, campos).headers["location"]
    assert loc.endswith("&responsable=Mengano"), loc
    for hostil in ("mengano", "MENGANO"):          # se vuelve canónico
        loc = _guardar(conn, dict(campos, filtro=hostil)).headers["location"]
        assert loc.endswith("&responsable=Mengano"), (hostil, loc)
    for hostil in ("Nadie", "//evil.com", "x&y=1", '"><b>', str(OTRA), "Men"):
        loc = _guardar(conn, dict(campos, filtro=hostil)).headers["location"]
        assert "responsable=" not in loc and "evil" not in loc, (hostil, loc)
    loc = _guardar(conn, dict(campos, filtro="_sin")).headers["location"]
    assert loc.endswith("&responsable=_sin")
    assert quote("Mengano", safe="") == "Mengano"


def test_guardar_con_filtro_sin_tocar_nada_no_toca_lo_que_no_se_ve_ni_borra_nada():
    """Pinta filtrado → manda el formulario TAL CUAL lo mandaría un navegador →
    lee la base. Para cada vista posible, incluidos «Otros» y el que ya no se
    puede asignar."""
    for clave in ESPERADO:
        conn = A._mundo()
        antes = {t["id"]: dict(t) for t in conn.tareas}
        f = A._leer(_pintar(conn, clave))
        enviados = sorted(int(i) for i in
                          re.findall(r"prev_resp_(\d+)", " ".join(f.campos)))
        assert enviados == ESPERADO[clave], (
            f"el formulario de {clave!r} trae filas que no se ven: {enviados}")
        r = _guardar(conn, dict(f.campos))
        assert r.status_code == 303
        assert "asignadas=0" in r.headers["location"], (clave, r.headers["location"])
        assert {t["id"]: dict(t) for t in conn.tareas} == antes, (
            f"guardar sin tocar nada cambió la base en la vista {clave!r}")
        assert conn.log == [] and not [
            q for q, _ in conn.sql if q.startswith("UPDATE")], (
            f"vista {clave!r}: escribió sin que nadie tocara nada")


def test_guardar_con_filtro_cambia_solo_lo_que_se_toco():
    conn = A._mundo()
    f = A._leer(_pintar(conn, "Mengano"))
    campos = dict(f.campos)
    campos["resp_2"] = str(DUENO)      # una de las dos que se ven, reasignada
    antes = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    r = _guardar(conn, campos)
    assert "asignadas=1" in r.headers["location"]
    assert r.headers["location"].endswith("&responsable=Mengano")
    despues = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    assert despues == {**antes, 2: DUENO}, despues


# ── Los botones no cambian con el filtro; solo cambia cuál está activo ───

def test_con_cualquier_filtro_los_botones_y_sus_conteos_son_los_de_todas():
    """Hallazgo del testigo sobre ed0fb79: si los botones se calcularan sobre las
    filas YA filtradas, en la vista de una persona las demás dirían (0), «Todas»
    contaría solo las suyas y «Otros» o el que ya no entra desaparecerían. Para
    cada vista, mismos botones, mismas etiquetas, mismos `n`, mismas claves, y
    solo cambia cuál está activo."""
    conn = A._mundo()
    todas = _botones(_pintar(conn))
    assert [b[1] for b in todas if b[3]] == ["Todas"]
    etiquetas = [b[1] for b in todas]
    assert "Otros" in etiquetas and any("ya no se le puede asignar" in e
                                        for e in etiquetas), (
        "el mundo de la prueba no trae los botones difíciles")
    for clave, ids in ESPERADO.items():
        vista = _botones(_pintar(conn, clave))
        assert [b[1] for b in vista] == etiquetas, f"{clave!r}: otras etiquetas"
        assert [b[2] for b in vista] == [b[2] for b in todas], (
            f"{clave!r}: los conteos cambiaron con el filtro: "
            f"{[(b[1], b[2]) for b in vista]}")
        activos = [i for i, b in enumerate(vista) if b[3]]
        assert len(activos) == 1, f"{clave!r}: activos {activos}"
        # Las claves de los que son enlaces no cambian (el activo no es enlace).
        assert [b[0] for i, b in enumerate(vista) if i != activos[0]] == [
            b[0] for i, b in enumerate(todas) if i != activos[0]], (
            f"{clave!r}: cambiaron las claves de los enlaces")
        if clave:
            assert vista[activos[0]][2] == len(ids), (
                f"{clave!r}: el botón activo no cuenta lo que se ve")


# ── Un nombre con caracteres que rompen una URL ──────────────────────────

NOMBRE_RARO = "Ána Ñ&Co+#1 x"


def _consulta(href):
    """El valor de `responsable` tal como lo decodifica el servidor al llegar
    la petición (Starlette usa `parse_qs`: `+` sería un espacio, `%2B` un +)."""
    from urllib.parse import parse_qs
    q = href.split("?", 1)[1]
    return parse_qs(q)["responsable"][0]


def test_un_nombre_con_caracteres_especiales_llega_a_su_vista_por_los_tres_caminos():
    A._casa({DUENO: "Zutana", OTRA: NOMBRE_RARO})
    conn = base._Conn(tareas=[A._fila(1, OTRA), A._fila(2, DUENO)], areas=A.AREAS)
    html = _pintar(conn)
    # 1. El botón.
    href = next(h for h in re.findall(r'href="(/tareas\?responsable=[^"]*)"', html)
                if _consulta(h).replace(" ", "") != "" and "Co" in unquote(h))
    assert _consulta(href) == NOMBRE_RARO, f"el botón dice otro nombre: {href}"
    vista = _pintar(conn, _consulta(href))
    assert _ids(vista) == [1], "el botón no lleva a la vista de esa persona"
    from html import unescape
    assert [unescape(b[1]) for b in _botones(vista) if b[3]] == [NOMBRE_RARO]
    assert "Ñ&Co" not in vista, "el nombre salió sin escapar en el HTML"
    # 2. «+ Agregar tarea» desde esa vista.
    agregar = re.search(r'href="(/tareas/nueva\?responsable=[^"]*)"', vista).group(1)
    assert _consulta(agregar) == NOMBRE_RARO
    assert A._leer(A._pintar_alta(responsable=_consulta(agregar))).marcadas[
        "responsable"] == [str(OTRA)]
    # 3. El redirect de guardar.
    campos = dict(A._leer(vista).campos)
    loc = _guardar(conn, campos).headers["location"]
    assert "responsable=" in loc
    assert _consulta(loc) == NOMBRE_RARO, f"el redirect deformó el nombre: {loc}"
    assert _ids(_pintar(conn, _consulta(loc))) == [1]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
