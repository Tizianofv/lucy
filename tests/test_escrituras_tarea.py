"""Las escrituras de la tarea en la página de proyectos (Lucy 1.0, E6).

Agregar una tarea dentro del proyecto (nace con el responsable del proyecto),
marcarla hecha y desmarcarla (P5), cambiarle el título con doble clic, borrarla
con la × (va a la papelera), cambiarle el responsable en su detalle (P10), y
comentar y editar comentarios (P6, G8). Las personas de la tarea (E7), Telegram
(E8) y el cliente (E3) NO están acá.

Cada escritura pasa por la puerta que ya existía (o por la nueva, probada
llamándola directo): `db.crear_tarea_desde_el_panel`, `db.marcar_tarea_hecha`,
`db.reabrir_tarea`, `crud.editar` (título, con `crud.PUERTAS`), `crud.borrar`,
`db.asignar_responsable`, `db.comentar_tarea` y `db.editar_comentario`.

La frontera es la de `tests/test_escrituras_proyecto.py` (que de ahí toma el
lector de formularios y su regla de HTML simple): NO hay Postgres (SQLite con el
SQL real; `crud.deshacer` solo por el camino de `borrar`); el lector NO es un
navegador, no ve las hojas de estilo ni los ancestros fuera de su lista; el
JavaScript (un solo script que muestra el formulario de edición) se prueba con
`osascript -l JavaScript` y un `document` de mentira.

Correr:  python3 -m pytest tests/test_escrituras_tarea.py -q
"""
from __future__ import annotations

import ast
import json
import re
from datetime import timedelta

import pytest

from test_escrituras_proyecto import noco  # noqa: F401  (el Noco de mentira)
from test_escrituras_proyecto import (FECHA_ESCRITA, FECHA_Y_HORA_ESCRITA, _LectorDeFormularios,  # noqa: F401
                                      _escrito, _formularios_de, _la_marcada,
                                      _lo_que_manda_el_navegador, _otra_opcion,
                                      _primera_habilitada, _problemas_de_html_simple, _valor)
from test_grupo_ia import _archivos_de_texto, _ROOT
import test_pagina_proyectos as _pagina
from test_pagina_proyectos import _cliente, _dia, gente, mundo, tareas_en, titulo_de, ver  # noqa: F401
import acciones.crud as crud
import config
import db.db as db


# ═══════════════════════════════════════════════════════════════════════
# El mundo de prueba
# ═══════════════════════════════════════════════════════════════════════

def _con_bandeja(m):
    """`crear_tarea_desde_el_panel` escribe una fila de `bandeja`: se crea con
    las columnas que declara el esquema."""
    columnas = ["id INTEGER PRIMARY KEY" if c == "id" else c
                for c in db.columnas_declaradas()["bandeja"]]
    # (Desde la parte 6 de la página del proyecto el mundo base ya trae `bandeja`, con el `CREATE TABLE`
    # del esquema, porque la página lee de ella el autor de las notas de Telegram: no se vuelve a crear.)
    if m.con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bandeja'").fetchone() is None:
        m.con.execute(f"CREATE TABLE bandeja ({', '.join(columnas)})")


def _mundo(monkeypatch, gente):
    m = _pagina.Mundo()
    _con_bandeja(m)
    monkeypatch.setattr(db, "pool", _pagina.g._Pool(m.con))
    monkeypatch.setattr(db, "hoy_rd", lambda: _pagina.HOY)
    m.proyecto(1, "Otro uno", area="CDS", responsable=gente.rosi)
    m.proyecto(2, "El de la página", area="CDS", responsable=gente.dueno)
    m.proyecto(3, "Cerrado", area="CDS", estado="cerrado", responsable=gente.dueno)
    m.proyecto(4, "Sin responsable", area="ACD")
    m.tarea(10, "tarea diez", proyecto=2, vence=_dia(4))
    m.tarea(11, "tarea once, hecha", proyecto=2, estado="hecha", vence=_dia(-5), completado=_dia(-4))
    m.tarea(12, "tarea doce", proyecto=2)
    m.tarea(20, "de otro proyecto", proyecto=1)
    m.tarea(30, "suelta de CDS", area="CDS")
    m.tarea(31, "suelta sin grupo")
    m.tarea(40, "de un proyecto cerrado", proyecto=3, estado="hecha", completado=_dia(-9))
    m.comentario(50, 10, gente.rosi, "primer comentario", cuando=_dia(-1, 15))
    m.comentario(51, 12, gente.dueno, "comentario de la doce", cuando=_dia(-1, 16))
    m.con.commit()
    return m


@pytest.fixture
def mt(monkeypatch, gente):
    return _mundo(monkeypatch, gente)


def post(ruta: str, campos: dict | None = None, *, chat="dueno"):
    sesion = config.CHAT_ID_DUENO if chat == "dueno" else chat
    return _cliente(sesion).post(ruta, data=campos or {}, follow_redirects=False)


def tarea(m, tid: int) -> dict:
    f = m.con.execute("SELECT * FROM tareas WHERE id = ?", (tid,)).fetchone()
    return dict(f) if f is not None else None


def comentario(m, cid: int) -> dict:
    f = m.con.execute("SELECT * FROM comentarios_tarea WHERE id = ?", (cid,)).fetchone()
    return dict(f) if f is not None else None


def huellas(m) -> list[dict]:
    return [dict(f) for f in m.con.execute("SELECT * FROM log_acciones ORDER BY id")]


def foto(m) -> dict:
    """Todo lo que estas escrituras pueden tocar, para comprobar que NADA MÁS cambió."""
    return {t: [dict(f) for f in m.con.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("tareas", "comentarios_tarea", "proyectos", "log_acciones")}


def igual_salvo(antes: dict, despues: dict, **permitido) -> bool:
    """`despues == antes` salvo lo que `permitido` declare: `tareas={10}` son
    los ids de esa tabla que SÍ pueden haber cambiado o aparecido."""
    for tabla in antes:
        libres = permitido.get(tabla, set())
        a = {f["id"]: f for f in antes[tabla]}
        d = {f["id"]: f for f in despues[tabla]}
        if any(a.get(i) != d.get(i) for i in (set(a) | set(d)) - libres):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════
# db.reabrir_tarea (P5), llamada directo
# ═══════════════════════════════════════════════════════════════════════

async def test_reabrir_vuelve_a_pendiente_con_la_misma_fecha_y_deja_su_huella(mt):
    antes = tarea(mt, 11)
    assert await db.reabrir_tarea(11) is True
    despues = tarea(mt, 11)
    assert (despues["estado"], despues["completado_en"]) == ("pendiente", None)
    assert despues["vence_en"] == antes["vence_en"] and despues["titulo"] == antes["titulo"]
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "tareas", 11)
    assert json.loads(h["antes"])["estado"] == "hecha"
    assert json.loads(h["despues"]) == {"estado": "pendiente", "completado_en": None}


@pytest.mark.parametrize("tid,estado", [(10, "pendiente"), (999, None)])
async def test_reabrir_no_toca_lo_que_no_esta_hecho_ni_lo_que_no_existe(mt, tid, estado):
    foto0 = foto(mt)
    assert await db.reabrir_tarea(tid) is False
    assert foto(mt) == foto0


async def test_reabrir_no_toca_una_descartada_ni_una_de_la_papelera(mt):
    mt.tarea(60, "descartada", proyecto=2, estado="descartado")
    mt.tarea(61, "hecha y borrada", proyecto=2, estado="hecha", borrada=True)
    foto0 = foto(mt)
    assert await db.reabrir_tarea(60) is False and await db.reabrir_tarea(61) is False
    assert foto(mt) == foto0


async def test_reabrir_una_tarea_de_un_proyecto_cerrado_si_se_puede(mt):
    assert await db.reabrir_tarea(40) is True
    assert tarea(mt, 40)["estado"] == "pendiente"


async def test_reabrir_dos_veces_no_escribe_la_segunda(mt):
    await db.reabrir_tarea(11)
    assert await db.reabrir_tarea(11) is False and len(huellas(mt)) == 1


def test_una_reabierta_con_la_fecha_pasada_sale_vencida_y_con_la_futura_no(mt):
    mt.tarea(62, "pasada", proyecto=2, estado="hecha", vence=_dia(-3), completado=_dia(-2))
    mt.tarea(63, "futura", proyecto=2, estado="hecha", vence=_dia(6), completado=_dia(-2))
    for tid in (62, 63, 11):
        post(f"/proyectos/tarea/{tid}/reabrir")
    html = ver(mt, p=2)
    rojas = [int(x) for x in re.findall(r'class="tarea [^"]*vencida[^"]*" id="tarea-(\d+)"', html)]
    assert sorted(rojas) == [11, 62]          # la 11 venció hace 5 días; la 63 vence en 6


# ═══════════════════════════════════════════════════════════════════════
# db.editar_comentario (G8, P6), llamada directo
# ═══════════════════════════════════════════════════════════════════════

async def test_G8_se_edita_un_comentario_de_esa_tarea_y_queda_marcado(mt, gente):
    antes = comentario(mt, 50)
    assert await db.editar_comentario(50, 10, gente.dueno, "  texto nuevo \r\n con salto ") is True
    c = comentario(mt, 50)
    assert c["texto"] == "texto nuevo \n con salto"
    assert c["editado_en"] is not None and antes["editado_en"] is None
    assert (c["autor_chat_id"], c["creado_en"], c["tarea_id"]) == (
        antes["autor_chat_id"], antes["creado_en"], antes["tarea_id"])


async def test_G8_la_huella_lleva_el_antes_y_el_despues(mt, gente):
    await db.editar_comentario(50, 10, gente.dueno, "texto nuevo")
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "comentarios_tarea", 50)
    assert json.loads(h["antes"])["texto"] == "primer comentario" and json.loads(h["antes"])["editado_en"] is None
    assert json.loads(h["despues"])["texto"] == "texto nuevo" and json.loads(h["despues"])["editado_en"] is not None


async def test_G8_con_el_id_de_un_comentario_de_otra_tarea_no_se_escribe_nada(mt, gente):
    foto0 = foto(mt)
    assert await db.editar_comentario(51, 10, gente.dueno, "intruso") is False      # el 51 es de la tarea 12
    assert await db.editar_comentario(50, 12, gente.dueno, "intruso") is False      # el 50 es de la 10
    assert await db.editar_comentario(50, 999, gente.dueno, "intruso") is False
    assert foto(mt) == foto0


async def test_G8_no_se_edita_uno_borrado_ni_uno_de_una_tarea_en_la_papelera(mt, gente):
    mt.comentario(52, 10, gente.rosi, "borrado", borrado=True)
    mt.tarea(64, "borrada", proyecto=2, borrada=True)
    mt.comentario(53, 64, gente.rosi, "de tarea borrada")
    foto0 = foto(mt)
    assert await db.editar_comentario(52, 10, gente.dueno, "x") is False
    assert await db.editar_comentario(53, 64, gente.dueno, "x") is False
    assert foto(mt) == foto0


async def test_P6_cualquiera_de_los_dos_edita_el_comentario_del_otro(mt, gente):
    assert await db.editar_comentario(50, 10, gente.dueno, "lo editó Tiziano") is True    # era de Rosi
    assert await db.editar_comentario(51, 12, gente.rosi, "lo editó Rosi") is True        # era de Tiziano
    assert comentario(mt, 50)["autor_chat_id"] == gente.rosi                              # el autor no cambia


async def test_quien_no_entra_al_panel_no_edita(mt, gente):
    foto0 = foto(mt)
    assert await db.editar_comentario(50, 10, 700100999, "x") is False
    assert foto(mt) == foto0


@pytest.mark.parametrize("texto", ["", "   ", "\n\n", None, 5, "x" * (db.LARGO_COMENTARIO + 1)])
async def test_un_texto_que_no_vale_lanza_y_no_escribe(mt, gente, texto):
    foto0 = foto(mt)
    with pytest.raises(db.ComentarioNoVale):
        await db.editar_comentario(50, 10, gente.dueno, texto)
    assert foto(mt) == foto0


async def test_el_texto_igual_al_que_ya_tenia_no_escribe_ni_deja_huella(mt, gente):
    assert await db.editar_comentario(50, 10, gente.dueno, "  primer comentario ") is False
    assert comentario(mt, 50)["editado_en"] is None and huellas(mt) == []


async def test_el_tope_es_2000_y_el_largo_se_mide_despues_de_limpiar(mt, gente):
    assert await db.editar_comentario(50, 10, gente.dueno, "  " + "x" * db.LARGO_COMENTARIO + "  ") is True
    assert len(comentario(mt, 50)["texto"]) == db.LARGO_COMENTARIO


# ═══════════════════════════════════════════════════════════════════════
# Las rutas, de punta a punta
# ═══════════════════════════════════════════════════════════════════════

def _adonde(r) -> str:
    return r.headers["location"]


def test_marcar_hecha_escribe_con_huella_de_panel_y_vuelve_a_la_tarea(mt):
    antes = foto(mt)
    r = post("/proyectos/tarea/10/hecha")
    assert r.status_code == 303 and _adonde(r) == "/proyectos?p=2&hecho=tarea_hecha#tarea-10"
    t = tarea(mt, 10)
    assert t["estado"] == "hecha" and t["completado_en"] is not None
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["registro_id"]) == ("panel", "editar", 10)
    assert igual_salvo(antes, foto(mt), tareas={10}, log_acciones={h["id"]})
    html = ver(mt, p=2, hecho="tarea_hecha")
    assert "Tarea marcada como hecha." in html


def test_marcar_hecha_lo_ya_hecho_no_escribe_y_lo_dice(mt):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/11/hecha")
    assert _adonde(r) == "/proyectos?p=2&error=tarea_igual#tarea-11" and foto(mt) == foto0
    assert "No cambió nada" in ver(mt, p=2, error="tarea_igual")


def test_desmarcar_vuelve_a_pendiente_y_lo_cuenta_la_pagina(mt):
    r = post("/proyectos/tarea/11/reabrir")
    assert _adonde(r) == "/proyectos?p=2&hecho=tarea_reabierta#tarea-11"
    assert tarea(mt, 11)["estado"] == "pendiente"
    assert "Tarea desmarcada: vuelve a pendiente, con la misma fecha." in ver(mt, p=2, hecho="tarea_reabierta")
    assert 11 in tareas_en(ver(mt, p=2).split('<details class="plegar">')[0])    # ya no está plegada


def test_las_tareas_sueltas_vuelven_a_su_grupo_o_a_sin_grupo(mt):
    assert _adonde(post("/proyectos/tarea/30/hecha")).startswith("/proyectos?g=CDS&hecho=tarea_hecha#tarea-30")
    assert _adonde(post("/proyectos/tarea/31/hecha")).startswith("/proyectos?sin_grupo=1&hecho=tarea_hecha#tarea-31")


def test_una_tarea_de_un_proyecto_en_la_papelera_vuelve_a_sin_grupo(mt):
    mt.proyecto(9, "En la papelera", area="CDS", borrado=True)
    mt.tarea(65, "huérfana", proyecto=9)
    assert _adonde(post("/proyectos/tarea/65/hecha")).startswith("/proyectos?sin_grupo=1")


def test_lo_que_no_existe_da_error_tarea_y_no_escribe(mt):
    foto0 = foto(mt)
    for ruta, campos in (("hecha", {}), ("reabrir", {}), ("titulo", {"titulo": "x"}), ("borrar", {}),
                         ("responsable", {"responsable": "Code"}), ("comentar", {"texto": "x"})):
        r = post(f"/proyectos/tarea/999/{ruta}", campos)
        assert r.status_code == 303 and _adonde(r).startswith("/proyectos?error="), ruta
    assert foto(mt) == foto0


# ── Título (G12 por el panel) ────────────────────────────────────────────

def test_el_titulo_se_guarda_limpio_con_huella_de_panel(mt):
    r = post("/proyectos/tarea/10/titulo", {"titulo": "  Nuevo título  "})
    assert _adonde(r) == "/proyectos?p=2&hecho=tarea_titulo#tarea-10"
    assert tarea(mt, 10)["titulo"] == "Nuevo título"
    h, = huellas(mt)
    assert (h["actor"], h["accion"]) == ("panel", "editar")
    assert titulo_de(ver(mt, p=2, hecho="tarea_titulo")) == "El de la página"
    assert "Nuevo título" in ver(mt, p=2)


@pytest.mark.parametrize("titulo", ["", "   ", "x" * (db.LARGO_TITULO_TAREA + 1)])
def test_G12_un_titulo_vacio_o_largo_se_rechaza_y_vuelve_al_formulario_abierto(mt, titulo):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/10/titulo", {"titulo": titulo})
    assert _adonde(r) == "/proyectos?p=2&error=tarea_titulo&editar_tarea=10#tarea-10"
    assert foto(mt) == foto0
    html = ver(mt, p=2, error="tarea_titulo", editar_tarea=10)
    assert "El título no puede quedar vacío ni pasar de 200 caracteres." in html
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/tarea/10/titulo"[^>]*>', html)


def test_el_tope_del_titulo_son_200(mt):
    post("/proyectos/tarea/10/titulo", {"titulo": "x" * db.LARGO_TITULO_TAREA})
    assert len(tarea(mt, 10)["titulo"]) == db.LARGO_TITULO_TAREA


# ── Borrar con la × (G15 por el panel) ───────────────────────────────────

def test_la_x_pide_confirmacion_y_cancelar_no_borra(mt):
    html = ver(mt, p=2, confirmar_borrar=10)
    assert "¿Borrarla?" in html and 'action="/proyectos/tarea/10/borrar"' in html
    assert 'href="/proyectos?p=2&amp;confirmar_borrar=10#tarea-10"' not in html
    assert 'href="/proyectos?p=2&amp;confirmar_borrar=12#tarea-12"' in html           # las otras siguen con su ×
    assert tarea(mt, 10)["borrado_en"] is None


def test_borrar_va_a_la_papelera_con_huella_de_panel_y_se_deshace(mt):
    import asyncio
    antes = foto(mt)
    r = post("/proyectos/tarea/10/borrar")
    assert _adonde(r) == "/proyectos?p=2&hecho=tarea_borrada&borrada=10#tarea-10"
    t = tarea(mt, 10)
    assert t is not None and t["borrado_en"] is not None            # soft-delete: la fila sigue
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "borrar", "tareas", 10)
    assert 10 not in tareas_en(ver(mt, p=2))
    # El aviso dice un ESTADO comprobado en la base (7-oct-2026): la tarea con ese id
    # está en la papelera. Una dirección escrita a mano, sin id o con el de una tarea
    # viva, no lo fuerza.
    assert "está en la <a href=\"/papelera\">Papelera</a>" in ver(mt, p=2, hecho="tarea_borrada", borrada=10)
    assert "está en la <a href=\"/papelera\">Papelera</a>" not in ver(mt, p=2, hecho="tarea_borrada")
    assert "está en la <a href=\"/papelera\">Papelera</a>" not in ver(mt, p=2, hecho="tarea_borrada", borrada=11)
    assert igual_salvo(antes, foto(mt), tareas={10}, log_acciones={h["id"]})
    asyncio.new_event_loop().run_until_complete(crud.deshacer(h["id"]))
    assert tarea(mt, 10)["borrado_en"] is None and 10 in tareas_en(ver(mt, p=2))


def test_borrar_dos_veces_no_escribe_la_segunda(mt):
    post("/proyectos/tarea/10/borrar")
    r = post("/proyectos/tarea/10/borrar")
    assert "error=tarea_igual" in _adonde(r) and len(huellas(mt)) == 1


# ── Responsable de la tarea (P10) ────────────────────────────────────────

def test_el_responsable_se_cambia_por_nombre_con_huella_de_panel(mt, gente):
    r = post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"})
    assert _adonde(r) == "/proyectos?p=2&t=10&hecho=tarea_responsable#tarea-10"
    assert tarea(mt, 10)["responsable_chat_id"] == gente.rosi
    h, = huellas(mt)
    assert (h["actor"], h["accion"]) == ("panel", "editar")
    html = ver(mt, p=2, t=10)
    # La página ya no trae el desplegable de la tarea: el responsable sale como
    # la persona «Responsable» del detalle, por nombre.
    assert "<b>Persona Dos</b><span>Responsable</span>" in html.split('<div class="detalle">', 1)[1].split("<h4>Comentarios</h4>")[0]
    assert str(gente.rosi) not in html


def test_se_puede_quitar_el_responsable_de_una_tarea_y_asignarle_a_code(mt, gente):
    post("/proyectos/tarea/10/responsable", {"responsable": "Code"})
    assert tarea(mt, 10)["responsable_chat_id"] == config.CHAT_ID_CODE
    post("/proyectos/tarea/10/responsable", {"responsable": ""})
    assert tarea(mt, 10)["responsable_chat_id"] is None


@pytest.mark.parametrize("quien", ["Nadie", "Ajeno", "700100999", "x" * 300])
def test_un_responsable_que_no_vale_no_se_guarda(mt, gente, quien):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/10/responsable", {"responsable": quien})
    assert _adonde(r) == "/proyectos?p=2&error=tarea_responsable&t=10#tarea-10" and foto(mt) == foto0


def test_el_mismo_responsable_no_deja_otra_huella(mt, gente):
    post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"})
    r = post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"})
    assert "error=tarea_igual" in _adonde(r) and len(huellas(mt)) == 1


# ── Comentarios ──────────────────────────────────────────────────────────

def test_comentar_guarda_con_el_chat_de_la_sesion_y_nunca_con_el_del_formulario(mt, gente):
    r = post("/proyectos/tarea/10/comentar", {"texto": "  un comentario nuevo ", "autor_chat_id": "700100001"})
    assert _adonde(r) == "/proyectos?p=2&t=10&hecho=comentario#tarea-10"
    nuevo = max(f["id"] for f in map(dict, mt.con.execute("SELECT id FROM comentarios_tarea")))
    c = comentario(mt, nuevo)
    assert (c["texto"], c["tarea_id"], c["autor_chat_id"]) == ("un comentario nuevo", 10, gente.dueno)
    h = huellas(mt)[-1]
    assert (h["actor"], h["accion"], h["tabla"]) == ("panel", "crear", "comentarios_tarea")
    html = ver(mt, p=2, t=10)
    assert "un comentario nuevo" in html and "Comentario guardado." in ver(mt, p=2, t=10, hecho="comentario")


@pytest.mark.parametrize("texto", ["", "   ", "x" * (db.LARGO_COMENTARIO + 1)])
def test_un_comentario_vacio_o_largo_no_se_guarda(mt, texto):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/10/comentar", {"texto": texto})
    assert _adonde(r) == "/proyectos?p=2&error=tarea_comentario&t=10#tarea-10" and foto(mt) == foto0


def test_editar_un_comentario_por_la_ruta_lo_marca_editado_y_la_pagina_lo_dice(mt, gente):
    r = post("/proyectos/tarea/10/comentario/50/editar", {"texto": "ya corregido"})
    assert _adonde(r) == "/proyectos?p=2&t=10&hecho=comentario_editado#tarea-10"
    assert comentario(mt, 50)["texto"] == "ya corregido" and comentario(mt, 50)["editado_en"] is not None
    html = ver(mt, p=2, t=10)
    assert "ya corregido" in html and html.count(" · editado") == 1
    assert "Comentario editado." in ver(mt, p=2, t=10, hecho="comentario_editado")


def test_G8_por_la_ruta_el_comentario_de_otra_tarea_no_se_edita(mt):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/10/comentario/51/editar", {"texto": "intruso"})
    assert "error=tarea_igual" in _adonde(r) and foto(mt) == foto0


@pytest.mark.parametrize("texto", ["", "x" * (db.LARGO_COMENTARIO + 1)])
def test_un_texto_malo_vuelve_al_comentario_abierto(mt, texto):
    foto0 = foto(mt)
    r = post("/proyectos/tarea/10/comentario/50/editar", {"texto": texto})
    assert _adonde(r) == "/proyectos?p=2&error=tarea_comentario&t=10&editar_comentario=50#tarea-10"
    assert foto(mt) == foto0
    html = ver(mt, p=2, t=10, editar_comentario=50, error="tarea_comentario")
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/tarea/10/comentario/50/editar"[^>]*>', html)


# ── Agregar una tarea dentro del proyecto (P10) ──────────────────────────

def _nueva(titulo="Tarea nueva", vence="", pid=2, **resto):
    return post(f"/proyectos/{pid}/tareas", {"titulo": titulo, "vence": vence, **resto})


def test_la_tarea_nueva_nace_en_el_proyecto_con_el_responsable_del_proyecto(mt, gente):
    r = _nueva("  Mezclar el bajo  ", "2026-10-20")
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert _adonde(r) == f"/proyectos?p=2&tarea_creada={nueva}#tarea-{nueva}"
    t = tarea(mt, nueva)
    assert (t["titulo"], t["proyecto_id"], t["responsable_chat_id"], t["area"]) == (
        "Mezclar el bajo", 2, gente.dueno, None)
    assert t["vence_en"] is not None and t["estado"] == "pendiente"
    h = huellas(mt)[-1]
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "crear", "tareas", nueva)
    html = ver(mt, p=2, tarea_creada=nueva)
    assert "Tarea creada (#%d)" % nueva in html and "Mezclar el bajo" in html and nueva in tareas_en(html)


def test_la_fecha_es_opcional_y_la_tarea_nueva_sale_sin_fecha(mt):
    _nueva("Sin fecha")
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert tarea(mt, nueva)["vence_en"] is None


def test_si_el_proyecto_no_tiene_responsable_la_tarea_nace_sin_responsable(mt):
    _nueva("De nadie", pid=4)
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert tarea(mt, nueva)["responsable_chat_id"] is None and tarea(mt, nueva)["proyecto_id"] == 4


def test_si_el_responsable_del_proyecto_ya_no_puede_serlo_la_tarea_se_crea_sin_responsable(mt, gente):
    mt.con.execute("UPDATE proyectos SET responsable_chat_id = 700100999 WHERE id = 2")
    r = _nueva("Se crea igual")
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert "tarea_creada=" in _adonde(r) and tarea(mt, nueva)["responsable_chat_id"] is None


def test_si_el_responsable_es_code_y_el_proyecto_no_es_de_ia_avisa_que_la_sala_no_la_ve(mt):
    mt.con.execute("UPDATE proyectos SET responsable_chat_id = ? WHERE id = 2", (config.CHAT_ID_CODE,))
    assert "&sala_no=1" in _adonde(_nueva("Para Code"))


@pytest.mark.parametrize("titulo,clave", [("", "tarea_titulo"), ("   ", "tarea_titulo"),
                                           ("x" * (db.LARGO_TITULO_TAREA + 1), "tarea_titulo")])
def test_un_titulo_que_no_vale_no_crea_nada(mt, titulo, clave):
    foto0 = foto(mt)
    assert _adonde(_nueva(titulo)) == f"/proyectos?p=2&error={clave}" and foto(mt) == foto0


def test_una_fecha_que_no_se_entiende_no_crea_nada(mt):
    foto0 = foto(mt)
    assert _adonde(_nueva("x", "no es fecha")) == "/proyectos?p=2&error=tarea_fecha" and foto(mt) == foto0


def test_un_proyecto_cerrado_o_que_no_existe_no_recibe_tareas(mt):
    foto0 = foto(mt)
    assert _adonde(_nueva("x", pid=3)) == "/proyectos?p=3&error=proyecto_cerrado"
    assert _adonde(_nueva("x", pid=999)).startswith("/proyectos?p=999&error=proyecto")
    assert foto(mt) == foto0
    assert "no recibe tareas nuevas" in ver(mt, p=3, error="proyecto_cerrado")


def test_el_formulario_de_tarea_nueva_no_sale_en_un_proyecto_cerrado(mt):
    assert 'action="/proyectos/3/tareas"' not in ver(mt, p=3)
    assert 'action="/proyectos/2/tareas"' in ver(mt, p=2)


def test_sin_sesion_ninguna_escritura_de_tarea_escribe(mt):
    foto0 = foto(mt)
    for ruta, campos in (("/proyectos/2/tareas", {"titulo": "x"}), ("/proyectos/tarea/10/hecha", {}),
                         ("/proyectos/tarea/11/reabrir", {}), ("/proyectos/tarea/10/titulo", {"titulo": "x"}),
                         ("/proyectos/tarea/10/borrar", {}), ("/proyectos/tarea/10/responsable", {"responsable": "Code"}),
                         ("/proyectos/tarea/10/comentar", {"texto": "x"}),
                         ("/proyectos/tarea/10/comentario/50/editar", {"texto": "x"})):
        assert post(ruta, campos, chat=None).status_code == 401, ruta
    assert foto(mt) == foto0


# ── Las puertas que ya existían se prueban llamándolas directo también ───

async def test_la_puerta_de_crear_tarea_directo_rechaza_un_proyecto_cerrado_y_un_responsable_ajeno(mt, gente):
    foto0 = foto(mt)
    with pytest.raises(db.ProyectoNoAdmiteTareas):
        await db.crear_tarea_desde_el_panel(gente.dueno, "x", None, None, None, proyecto_id=3)
    with pytest.raises(db.TareaNoValida):
        await db.crear_tarea_desde_el_panel(gente.dueno, "x", None, None, 700100999, proyecto_id=2)
    assert foto(mt) == foto0
    assert tarea(mt, 10) is not None


async def test_la_puerta_de_titulo_directo_por_crud_editar(mt):
    for malo in ("", "   ", "x" * 201):
        with pytest.raises(ValueError):
            await crud.editar("tareas", 10, {"titulo": malo}, motivo="p", actor="panel")
    assert tarea(mt, 10)["titulo"] == "tarea diez"


async def test_la_puerta_de_borrar_directo_deja_el_actor_que_se_le_pide(mt):
    log_id = await crud.borrar("tareas", 10, "prueba", actor="panel")
    assert [h for h in huellas(mt) if h["id"] == log_id][0]["actor"] == "panel"
    assert tarea(mt, 10)["borrado_en"] is not None


# ═══════════════════════════════════════════════════════════════════════
# Los formularios del HTML, enviados como el navegador a la ruta real
# ═══════════════════════════════════════════════════════════════════════
#
# Misma idea que `tests/test_escrituras_proyecto.py` (y el mismo lector, con su
# regla de HTML simple y su FRONTERA: no es un navegador): la lista de
# formularios sale del HTML de cada vista, de cada uno se toma su `action` y los
# valores que mandaría el navegador, se envía a la ruta de verdad y se comprueba
# que cambió LA TAREA DE LA PÁGINA y nada más (ni otra tarea, ni otro
# comentario, ni otro proyecto).

# Sin `/proyectos/N/area`: el «Mover a» del detalle se quitó de la página (1-oct-2026,
# decisión de Tiziano: la maqueta no lo tiene); la ruta sigue y se prueba directo.
# El «Responsable» de la tarea VOLVIÓ al detalle el 7-oct-2026 (ver `_DETALLE_10`).
_A = ["/proyectos/2/nombre", "/proyectos/2/responsable",
      # E7 (1-oct-2026): el cliente y las personas del proyecto son formularios.
      "/proyectos/2/cliente", "/proyectos/2/personas",
      # Parte 6 (8-oct-2026): el formulario de «Nueva nota» está siempre que la base tenga la columna.
      "/proyectos/2/notas"]
_TAREAS_DE_2 = ["/proyectos/tarea/10/hecha", "/proyectos/tarea/10/titulo",
                "/proyectos/tarea/11/reabrir", "/proyectos/tarea/11/titulo",
                "/proyectos/tarea/12/hecha", "/proyectos/tarea/12/titulo", "/proyectos/2/tareas"]
_DETALLE_10 = ["/proyectos/tarea/10/comentar", "/proyectos/tarea/10/comentario/50/editar",
               "/proyectos/tarea/10/personas", "/proyectos/tarea/10/responsable"]

# La ventanita de «+ Proyecto en X» (1-oct-2026) la escribe el servidor en CADA
# vista, una por grupo de la lista de la izquierda: un formulario
# `/proyectos/nuevo` más por grupo del mundo de prueba (`_pagina.AREAS`).
_V = ["/proyectos/nuevo"] * len(_pagina.AREAS)

# vista -> (consulta, TODAS las acciones POST que tiene que tener, exactas)
_VISTAS_DE_TAREAS = {
    "abierto": ({"p": 2}, _A + _TAREAS_DE_2 + _V),
    "detalle": ({"p": 2, "t": 10}, _A + _TAREAS_DE_2 + _DETALLE_10 + _V),
    "editar_titulo": ({"p": 2, "editar_tarea": 10}, _A + _TAREAS_DE_2 + _V),
    "confirmar_borrar": ({"p": 2, "confirmar_borrar": 10}, _A + _TAREAS_DE_2 + ["/proyectos/tarea/10/borrar"] + _V),
    "editar_comentario": ({"p": 2, "t": 10, "editar_comentario": 50}, _A + _TAREAS_DE_2 + _DETALLE_10 + _V),
    "confirmar_cerrar": ({"p": 2, "confirmar": "cerrar"}, _A + _TAREAS_DE_2 + ["/proyectos/2/estado"] + _V),
    "editar_nombre": ({"p": 2, "editar": "nombre"}, _A + _TAREAS_DE_2 + _V),
    "cerrado": ({"p": 3}, ["/proyectos/3/estado", "/proyectos/3/nombre",
                           "/proyectos/3/responsable", "/proyectos/3/cliente",
                           "/proyectos/3/personas", "/proyectos/3/notas", "/proyectos/tarea/40/reabrir",
                           "/proyectos/tarea/40/titulo"] + _V),
    "sueltas": ({"g": "CDS"}, ["/proyectos/tarea/30/hecha", "/proyectos/tarea/30/titulo"] + _V),
    # El detalle de una tarea suelta: además de lo de siempre, «Meter en un proyecto».
    "detalle_suelta": ({"g": "CDS", "t": 30}, ["/proyectos/tarea/30/hecha", "/proyectos/tarea/30/titulo",
                                               "/proyectos/tarea/30/comentar", "/proyectos/tarea/30/personas",
                                               "/proyectos/tarea/30/proyecto",
                                               "/proyectos/tarea/30/responsable"] + _V),
    "sin_grupo": ({"sin_grupo": 1}, ["/proyectos/tarea/31/hecha", "/proyectos/tarea/31/titulo"] + _V),
    "nuevo": ({"nuevo": "CDS"}, ["/proyectos/nuevo"] + _V),
    # (Las vistas `derivada` y `derivada_suelta`, que abrían los renglones de «¿sale
    # una tarea nueva de ésta?» con `?derivar=`, se fueron: la página ya no los
    # ofrece —la maqueta no tiene el «＋»—. `/tareas` y la ruta de marcar hecha
    # siguen creándola, y se prueban directo más abajo.)
}

_ES_DE_TAREA = re.compile(r"/proyectos/(tarea/\d+/.*|\d+/tareas)")


def _lleva_derivada(form: dict) -> bool:
    """¿Ese formulario trae los renglones de «¿sale una tarea nueva de ésta?»?"""
    return any((c["name"] or "").startswith("deriva_titulo_") for c in form["campos"])


@pytest.mark.parametrize("vista", sorted(_VISTAS_DE_TAREAS))
def test_cada_vista_tiene_exactamente_estos_formularios_de_escritura(mt, vista):
    consulta, esperadas = _VISTAS_DE_TAREAS[vista]
    html = ver(mt, **consulta)
    assert sorted(f["accion"] for f in _formularios_de(html)) == sorted(esperadas)
    assert _problemas_de_html_simple(html) == []


def _nombres_a_chat() -> dict:
    return {"": None, **{n: c for c, n in config.nombres_con_code().items()}}


def _nuevos(antes: dict, despues: dict, tabla: str) -> set:
    return {f["id"] for f in despues[tabla]} - {f["id"] for f in antes[tabla]}


@pytest.mark.parametrize("vista", sorted(_VISTAS_DE_TAREAS))
def test_cada_formulario_de_tarea_enviado_como_el_navegador_escribe_en_la_tarea_de_la_pagina(
        monkeypatch, gente, vista, noco):
    consulta, esperadas = _VISTAS_DE_TAREAS[vista]
    chat_de = _nombres_a_chat()
    probados = 0
    for i in range(len(esperadas)):
        m = _mundo(monkeypatch, gente)
        forms = sorted(_formularios_de(ver(m, **consulta)), key=lambda f: f["accion"])
        form = forms[i]
        if not _ES_DE_TAREA.fullmatch(form["accion"]):
            continue                          # los del proyecto: `tests/test_escrituras_proyecto.py`
        escoger = _otra_opcion if form["accion"].endswith(("/responsable", "/proyecto")) else _la_marcada
        if form["accion"].endswith("/hecha") and _lleva_derivada(form):
            # El responsable de la tarea nueva lo CAMBIA la persona: así, un
            # `name` mal escrito (la hija nacería sin responsable) se nota.
            def escoger(c):
                return _otra_opcion(c) if c["name"].startswith("deriva_resp_") else _la_marcada(c)
        datos = _lo_que_manda_el_navegador(form, _escrito, escoger)
        if form["accion"].endswith("/personas"):          # la persona se elige con el clic en su botón
            datos["noco_id"] = "102"
        antes = foto(m)
        r = _cliente(config.CHAT_ID_DUENO).post(form["accion"], data=datos, follow_redirects=False)
        despues = foto(m)
        donde = (vista, form["accion"], datos, r.headers.get("location"))
        assert r.status_code == 303 and "error=" not in r.headers["location"], donde
        nuevas = _nuevos(antes, despues, "log_acciones")
        coincide = re.fullmatch(r"/proyectos/tarea/(\d+)/(.*)", form["accion"])
        if coincide is None:                                              # agregar una tarea
            pid = int(re.fullmatch(r"/proyectos/(\d+)/tareas", form["accion"]).group(1))
            ids = _nuevos(antes, despues, "tareas")
            assert len(ids) == 1 and igual_salvo(antes, despues, tareas=ids, log_acciones=nuevas), donde
            t = tarea(m, ids.pop())
            assert (t["titulo"], t["proyecto_id"]) == ("Escrito en titulo", pid), donde
            # La fecha que escribió la persona en SU campo llega a la tarea.
            assert str(t["vence_en"]).startswith(FECHA_ESCRITA), donde
            assert t["responsable_chat_id"] == gente.dueno, donde         # el del proyecto 2
            probados += 1
            continue
        tid, accion = int(coincide.group(1)), coincide.group(2)
        if accion.startswith("comentario/"):
            cid = int(re.fullmatch(r"comentario/(\d+)/editar", accion).group(1))
            assert igual_salvo(antes, despues, comentarios_tarea={cid}, log_acciones=nuevas, **{}), donde
            c = comentario(m, cid)
            assert c["texto"] == "Escrito en texto" and c["editado_en"] is not None, donde
        elif accion == "comentar":
            ids = _nuevos(antes, despues, "comentarios_tarea")
            assert len(ids) == 1 and igual_salvo(antes, despues, comentarios_tarea=ids, log_acciones=nuevas), donde
            c = comentario(m, ids.pop())
            assert (c["texto"], c["tarea_id"], c["autor_chat_id"]) == ("Escrito en texto", tid, gente.dueno), donde
        elif accion == "personas":
            assert igual_salvo(antes, despues, log_acciones=nuevas), (donde, "cambió OTRA cosa")
            filas = [dict(f) for f in m.con.execute("SELECT * FROM participantes")]
            assert len(filas) == 1 and (filas[0]["tarea_id"], filas[0]["proyecto_id"], filas[0]["noco_id"],
                                        filas[0]["nombre"], filas[0]["rol"], filas[0]["creado_por_chat_id"]) == (
                tid, None, 102, "Persona Dos de Noco", "Escrito en rol", gente.dueno), (donde, filas)
        else:
            # La vista `derivada` trae los renglones de «¿sale una tarea nueva
            # de ésta?» DENTRO del formulario de marcar hecha: el mismo envío
            # que cierra la tarea crea la hija, así que la única fila de más
            # que puede aparecer es ésa (y solo cuando la vista los tiene
            # abiertos).
            hijas = _nuevos(antes, despues, "tareas") if _lleva_derivada(form) else set()
            if accion == "hecha" and consulta.get("derivar") == tid:
                # Esa tarea es la que la vista abre para ofrecer la que sigue: si el
                # formulario de marcar hecha no trae sus campos (o el `name`
                # del título no llega a la ruta), la hija no nace y la madre
                # se cierra sola. Se exige la hija.
                cuantos = len([c for c in form["campos"] if (c["name"] or "").startswith("deriva_titulo_")])
                assert cuantos >= 1 and len(hijas) == cuantos, (donde, "la tarea que sigue no nació")
            assert igual_salvo(antes, despues, tareas={tid} | hijas, log_acciones=nuevas), (
                donde, "cambió OTRA cosa")
            t = tarea(m, tid)
            if accion == "hecha":
                assert t["estado"] == "hecha" and t["completado_en"] is not None, donde
                if hijas:
                    por_titulo = {h["titulo"]: h for h in (tarea(m, i) for i in hijas)}
                    nombres_n = sorted(c["name"] for c in form["campos"]
                                       if c["name"].startswith("deriva_titulo_"))
                    assert len(por_titulo) == len(hijas) == len(nombres_n), donde
                    for nombre in nombres_n:
                        n = nombre.rsplit("_", 1)[1]
                        hija = por_titulo[f"Escrito en deriva_titulo_{tid}_{n}"]
                        assert hija["deriva_de_id"] == tid, donde
                        elegido = _otra_opcion(next(c for c in form["campos"]
                                                    if c["name"] == f"deriva_resp_{tid}_{n}"))
                        assert hija["responsable_chat_id"] == chat_de[_valor(elegido)] is not None, donde
                        # Con proyecto, la nueva va al MISMO proyecto y sin grupo
                        # propio; suelta, se queda con el grupo que se eligió (el
                        # de la tarea que se cierra).
                        if t["proyecto_id"]:
                            assert (hija["proyecto_id"], hija["area"]) == (t["proyecto_id"], None), donde
                        else:
                            assert (hija["proyecto_id"], hija["area"]) == (None, t["area"]), donde
                        assert str(hija["vence_en"]).startswith(FECHA_Y_HORA_ESCRITA[:10]), donde
            elif accion == "reabrir":
                assert t["estado"] == "pendiente" and t["completado_en"] is None, donde
            elif accion == "titulo":
                assert t["titulo"] == "Escrito en titulo", donde
            elif accion == "borrar":
                assert t["borrado_en"] is not None, donde
            elif accion == "proyecto":
                elegido = _otra_opcion(next(c for c in form["campos"] if c["tipo"] == "select"))
                assert (t["proyecto_id"], t["area"]) == (int(_valor(elegido)), None), donde
            elif accion == "responsable":
                elegido = _otra_opcion(next(c for c in form["campos"] if c["tipo"] == "select"))
                assert t["responsable_chat_id"] == chat_de[_valor(elegido)], donde
            else:
                raise AssertionError(f"formulario sin intención declarada: {form['accion']}")
        probados += 1
    assert probados == len([a for a in esperadas if _ES_DE_TAREA.fullmatch(a)])


def test_ninguna_vista_ofrece_ya_la_tarea_que_sigue(monkeypatch, gente):
    """Hermanos: en TODAS las vistas (las de este archivo, más las de derivar que
    ya no existen y algunas que sí) ningún formulario trae campos `deriva_*` y no
    hay «＋». La maqueta no los tiene."""
    m = _mundo(monkeypatch, gente)
    for consulta in [c for c, _ in _VISTAS_DE_TAREAS.values()] + [
            {"p": 2, "derivar": 10}, {"g": "CDS", "derivar": 30}]:
        html = ver(m, **consulta)
        assert "deriva_" not in html and "sale-otra" not in html and "＋" not in html, consulta
        assert not any(_lleva_derivada(f) for f in _formularios_de(html)), consulta


def test_el_envio_escribe_algo_en_todo_control_que_la_persona_llena_en_todas_las_vistas(monkeypatch, gente):
    """La lista de controles sale del PROPIO LECTOR, de los formularios de cada
    vista renderizada: ninguno de los que la persona llena (texto, fecha, texto
    largo) se envía vacío, porque un campo vacío no prueba que su `name` esté
    atado a la ruta. Un tipo de control nuevo que el envío no sepa llenar rompe
    `_lo_que_manda_el_navegador`."""
    m = _mundo(monkeypatch, gente)
    vistos = set()
    for consulta, _ in _VISTAS_DE_TAREAS.values():
        for form in _formularios_de(ver(m, **consulta)):
            for c in form["campos"]:
                if c["tipo"] in ("select", "hidden"):
                    continue
                vistos.add(c["tipo"])
                assert _escrito(c) != "", (consulta, form["accion"], c)
    # (Ya no entra `datetime-local`: era el del renglón de la tarea derivada, que
    # la página no ofrece desde el 1-oct-2026.)
    assert vistos == {"text", "date", "textarea"}, vistos


# ── Cada elemento editable en el sitio tiene su formulario, y el servidor lo dibuja sin JS ──

class _Sitios(_LectorDeFormularios.__mro__[1]):          # html.parser.HTMLParser
    """Para cada contenedor `data-edita`: cuántos textos `data-dbl` y cuántos
    `form.renombrar` lleva dentro."""

    def __init__(self):
        super().__init__()
        self.sitios, self._pila = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-edita" in a:
            self.sitios.append({"dbl": 0, "formularios": 0, "nivel": len(self._pila)})
            self._pila.append(("sitio", tag))
            return
        if tag not in ("input", "br", "hr", "img", "meta", "link"):
            self._pila.append(("otro", tag))
        if self.sitios and any(k == "sitio" for k, _ in self._pila):
            actual = self.sitios[-1]
            if "data-dbl" in a:
                actual["dbl"] += 1
            if tag == "form" and a.get("class") == "renombrar":
                actual["formularios"] += 1

    def handle_endtag(self, tag):
        for i in range(len(self._pila) - 1, -1, -1):
            if self._pila[i][1] == tag:
                del self._pila[i:]
                break


def test_todo_texto_editable_con_doble_clic_tiene_su_formulario_en_su_sitio(mt):
    html = ver(mt, p=2, t=10)
    lector = _Sitios()
    lector.feed(html)
    assert len(lector.sitios) >= 1 + 3 + 1          # el proyecto, 3 tareas y el comentario de la 10
    assert all(s["dbl"] == s["formularios"] == 1 for s in lector.sitios), lector.sitios
    for dbl in ('data-dbl="nombre"', 'data-dbl="titulo"', 'data-dbl="texto"'):
        assert dbl in html or dbl.replace("nombre", "texto") in html
    assert html.count('data-dbl="titulo"') == 3 and html.count('data-dbl="texto"') == 1


def test_sin_javascript_cada_edicion_tiene_su_enlace_y_el_servidor_la_dibuja(mt):
    html = ver(mt, p=2, t=10)
    assert '<noscript><a class="nota" href="/proyectos?p=2&amp;t=10&amp;editar_comentario=50#tarea-10">' in html
    assert html.count("<noscript>") == 1 + 3 + 1
    abierto = ver(mt, p=2, editar_tarea=12)
    assert re.search(r'<form class="renombrar" method="post" action="/proyectos/tarea/12/titulo"[^>]*>', abierto)
    assert re.search(r'<a class="titulo" data-dbl="titulo"[^>]*hidden>tarea doce</a>', abierto)
    assert re.search(r'action="/proyectos/tarea/10/titulo"[^>]*hidden>', abierto)   # las otras siguen escondidas


# ── «Fecha límite» una sola vez; nunca un número de chat ─────────────────

def test_la_cabecera_fecha_limite_sale_una_sola_vez_por_lista(mt):
    html = ver(mt, p=2)
    assert html.count('<div class="col-fecha"><span>Fecha límite</span></div>') == 1
    assert ver(mt, g="CDS").count("Fecha límite</span>") == 1


def test_ninguna_vista_de_tareas_escribe_un_numero_de_chat(mt, gente):
    mt.con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 10", (gente.rosi,))
    mt.con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 12", (config.CHAT_ID_CODE,))
    for consulta, _ in _VISTAS_DE_TAREAS.values():
        html = ver(mt, **consulta)
        for numero in (str(gente.rosi), str(gente.dueno), "700100999"):
            assert numero not in html, (consulta, numero)
        assert re.search(r'value="-?\d{4,}"', html) is None, consulta


def test_el_detalle_trae_el_selector_de_responsable_y_sigue_sin_la_pantalla_de_la_tarea(mt):
    """Dos decisiones de Tiziano, una encima de la otra.

    1-oct-2026: mismas funciones que la maqueta. El detalle de la maqueta no tiene
    el enlace «Abrir la pantalla de la tarea»: eso SIGUE así y se vigila aquí.

    7-oct-2026: «En las tareas, tambien quiero poder cambiar el resposable.» y,
    preguntado si era en Proyectos, al abrir una tarea: «Si, exacto» (y que Lucy NO
    le avise por Telegram a nadie: «No»). Eso CONTRADICE lo que esta prueba exigía
    del desplegable «Responsable» (que NO estuviera), así que esa parte se cambió:
    ahora el detalle abierto lo trae, y solo el detalle abierto."""
    html = ver(mt, p=2, t=10)
    assert 'id="rt-10"' in html and 'action="/proyectos/tarea/10/responsable"' in html
    assert "resp-tarea" not in html            # la clase vieja, la de antes del 1-oct
    assert "Abrir la pantalla" not in html and 'href="/tareas/10"' not in html
    # Cerrado el detalle, ninguna tarea trae el desplegable.
    assert "/responsable" not in ver(mt, p=2).replace('action="/proyectos/2/responsable"', "")


# ── La × y el botón de marcar son lo que dice la maqueta ─────────────────

def test_el_titulo_es_el_enlace_que_abre_el_detalle_y_ya_no_hay_enlace_chiquitito(mt):
    """Como en la maqueta: un clic en la tarea enseña sus opciones. El enlace
    «▸ N» se fue —el número de comentarios queda como TEXTO— y sin JavaScript el
    título es un enlace normal: al detalle, y de vuelta si ya está abierto."""
    cerrado = ver(mt, p=2)
    assert ('<a class="titulo" data-dbl="titulo" href="/proyectos?p=2&amp;t=10#tarea-10"'
            in cerrado)
    assert 'class="detalle-enlace"' not in cerrado
    sin_css = re.sub(r"<style>.*?</style>", "", cerrado, flags=re.S)
    assert "▸" not in sin_css and "▾" not in sin_css
    assert cerrado.count("1 coment.<") == 2          # la 10 y la 12: texto, no enlace
    assert "primer comentario" not in cerrado           # el detalle está cerrado
    abierto = ver(mt, p=2, t=10)
    assert '<a class="titulo" data-dbl="titulo" href="/proyectos?p=2#tarea-10"' in abierto
    assert "primer comentario" in abierto


def test_la_x_va_al_final_a_la_derecha_de_cada_fila(mt):
    html = ver(mt, p=2)
    for tid in (10, 11, 12):
        fila = re.search(rf'id="tarea-{tid}".*?<div class="fila" data-edita>(.*?)\n  </div>', html, re.S).group(1)
        assert fila.rstrip().endswith("×</a>"), (tid, fila[-120:])
    assert "justify-content" in html and ".borrar-x{" in html


def test_marcar_y_desmarcar_son_botones_con_nombre_para_quien_no_ve(mt):
    html = ver(mt, p=2)
    assert html.count('aria-label="Marcar la tarea como hecha"') == 2          # la 10 y la 12
    assert html.count('aria-label="Desmarcar la tarea: vuelve a pendiente"') == 1     # la 11, hecha
    assert 'type="checkbox"' not in html


# ═══════════════════════════════════════════════════════════════════════
# Los hermanos: quién más escribe título, estado, área, responsable y comentarios
# ═══════════════════════════════════════════════════════════════════════

def _rutas_post_de_tareas():
    """Cada `@app.post` de `/proyectos/tarea/...` y de `/proyectos/{pid}/tareas`
    en `web/app.py`, con lo que escribe. SALE DEL ÁRBOL SINTÁCTICO."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    salida = {}
    for f in arbol.body:
        if not isinstance(f, ast.AsyncFunctionDef):
            continue
        for d in f.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
                    and d.args and isinstance(d.args[0], ast.Constant)):
                continue
            ruta = d.args[0].value
            if not (ruta.startswith("/proyectos/tarea/") or ruta.endswith("/tareas") and ruta.startswith("/proyectos/")):
                continue
            escribe, sesion = [], False
            for n in ast.walk(f):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                if n.func.attr in ("editar", "borrar") and n.args and isinstance(n.args[0], ast.Constant):
                    actor = tuple(k.value.value for k in n.keywords
                                  if k.arg == "actor" and isinstance(k.value, ast.Constant))
                    escribe.append((f"crud.{n.func.attr}({n.args[0].value})", actor))
                elif n.func.attr in ("marcar_tarea_hecha", "cerrar_y_derivar", "reabrir_tarea",
                                     "asignar_responsable", "comentar_tarea", "editar_comentario",
                                     "crear_tarea_desde_el_panel", "agregar_participante",
                                     "quitar_participante"):
                    escribe.append((f"db.{n.func.attr}", ()))
                elif n.func.attr == "puede_entrar":
                    sesion = True
            salida[f.name] = {"ruta": ruta, "escribe": escribe, "sesion": sesion}
    return salida


def test_toda_ruta_post_de_tarea_pide_sesion_y_escribe_por_una_puerta_con_actor_panel():
    rutas = _rutas_post_de_tareas()
    assert {r["ruta"] for r in rutas.values()} == {
        "/proyectos/{pid}/tareas", "/proyectos/tarea/{tid}/hecha", "/proyectos/tarea/{tid}/reabrir",
        "/proyectos/tarea/{tid}/titulo", "/proyectos/tarea/{tid}/borrar",
        "/proyectos/tarea/{tid}/responsable", "/proyectos/tarea/{tid}/comentar",
        "/proyectos/tarea/{tid}/comentario/{cid}/editar",
        # E7 (1-oct-2026): las personas de la tarea.
        "/proyectos/tarea/{tid}/personas", "/proyectos/tarea/{tid}/personas/{xid}/quitar",
        # Meter una tarea suelta en un proyecto (5-oct-2026): por `crud.editar`.
        "/proyectos/tarea/{tid}/proyecto"}
    for nombre, r in rutas.items():
        assert r["sesion"], f"{nombre} no pide sesión"
        assert len(r["escribe"]) == 1, f"{nombre} escribe por {r['escribe']}"
        puerta, actor = r["escribe"][0]
        if puerta.startswith("crud."):
            assert actor == ("panel",), f"{nombre}: actor {actor}"
    # «Marcar hecha» escribe por `db.cerrar_y_derivar`, LA MISMA puerta que usa
    # /tareas: cerrar una tarea —con la que sale de ella o sin ella— se decide
    # en un solo sitio (Tiziano, 1-oct-2026).
    assert {r["escribe"][0][0] for r in rutas.values()} == {
        "db.crear_tarea_desde_el_panel", "db.cerrar_y_derivar", "db.reabrir_tarea", "crud.editar(tareas)",
        "crud.borrar(tareas)", "db.asignar_responsable", "db.comentar_tarea", "db.editar_comentario",
        "db.agregar_participante", "db.quitar_participante"}


def _escritores_sql() -> dict:
    """Por columna de `tareas` (y por la tabla de comentarios): las funciones del
    código (fuera de `tests/`) cuyo SQL legible la escribe. SALE DE RECORRER LOS
    `.py`. FRONTERA: no ve SQL armado al vuelo (los genéricos `crud.editar`,
    `deshacer` y `borrar` no nombran la columna: los cubren las pruebas de las
    puertas)."""
    columnas = ("titulo", "estado", "area", "responsable_chat_id")
    salida = {c: set() for c in columnas}
    salida["comentarios_tarea"] = set()
    for archivo in _archivos_de_texto():
        if archivo.suffix != ".py":
            continue
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                    continue
                for m in re.finditer(r"INSERT\s+INTO\s+tareas\s*\(([^)]*)\)", n.value, re.I):
                    for c in columnas:
                        if re.search(rf"\b{c}\b", m.group(1)):
                            salida[c].add(fn.name)
                for m in re.finditer(r"UPDATE\s+tareas\s+(?:\w+\s+)?SET\s+(.*?)(?:WHERE|FROM|$)", n.value, re.I | re.S):
                    for c in columnas:
                        if re.search(rf"\b{c}\s*=", m.group(1)):
                            salida[c].add(fn.name)
                if re.search(r"(INSERT\s+INTO|UPDATE)\s+comentarios_tarea", n.value, re.I):
                    salida["comentarios_tarea"].add(fn.name)
    return salida


def test_quien_escribe_cada_columna_de_la_tarea_y_cada_comentario():
    e = _escritores_sql()
    # EL TRINQUETE: un escritor nuevo se pone rojo hasta que alguien lo mire y lo
    # declare acá (con la puerta que le toca).
    assert e["titulo"] == {"cerrar_y_derivar", "crear_desde_interpretacion", "crear_o_reusar_alerta_tecnica",
                           "crear_tarea_desde_el_panel"}                       # los que CREAN; el cambio es `crud.editar`
    assert e["estado"] == {"_reprogramar_recurrentes", "cerrar_tarea_de_la_sala", "cerrar_y_derivar",
                           "marcar_tarea_hecha", "reabrir_tarea"}
    # `borrar_grupo` (7-oct-2026) solo PONE el área en NULL en lo que nombraba el grupo
    # que se borra: no escribe un grupo que venga de afuera (ver su prueba).
    assert e["area"] == {"borrar_grupo", "cerrar_y_derivar", "crear_desde_interpretacion",
                         "crear_o_reusar_alerta_tecnica", "crear_tarea_desde_el_panel"}
    assert e["responsable_chat_id"] == {"asignar_responsable", "cerrar_y_derivar", "crear_desde_interpretacion",
                                        "crear_o_reusar_alerta_tecnica", "crear_tarea_desde_el_panel"}
    assert e["comentarios_tarea"] == {"borrar_comentario", "comentar_tarea", "editar_comentario"}


def test_las_puertas_nuevas_estan_donde_las_dicen_sus_hermanas():
    """`reabrir_tarea` es la inversa de `marcar_tarea_hecha` (misma huella) y
    `editar_comentario` la hermana de `borrar_comentario` (mismo permiso)."""
    fuente = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    fns = {f.name: f for f in ast.walk(fuente) if isinstance(f, ast.AsyncFunctionDef)}

    def llama(nombre, objetivo):
        return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == objetivo
                   for n in ast.walk(fns[nombre]))
    assert llama("editar_comentario", "puede_entrar") and llama("borrar_comentario", "puede_entrar")
    assert llama("editar_comentario", "texto_de_comentario")
    cuerpo = ast.get_source_segment((_ROOT / "db" / "db.py").read_text(encoding="utf-8"), fns["editar_comentario"])
    assert "AND tarea_id = %s" in cuerpo and "t.borrado_en IS NULL" in cuerpo      # G8


# ── La tarea que sale de otra: /tareas y Proyectos deciden IGUAL ─────────

def test_hermanos_las_dos_pantallas_arman_y_escriben_la_derivada_con_lo_mismo():
    """`/tareas` (`guardar_tareas`) y la página de Proyectos
    (`marcar_tarea_hecha_desde_proyectos`) ofrecen la tarea derivada con LAS
    MISMAS piezas: se leen y se validan con `_derivadas_pedidas` y se escriben
    por `db.cerrar_y_derivar`. SALE DEL ÁRBOL SINTÁCTICO de `web/app.py`: no de
    una lista de nombres escrita a mano."""
    fuente = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    fns = {f.name: f for f in ast.walk(fuente) if isinstance(f, ast.AsyncFunctionDef)}

    def usa(fn, nombre):
        """Lo NOMBRA, llamándolo (`db.cerrar_y_derivar(...)`) o pasándolo como
        lector (`_responsable_por_nombre`): las dos formas cuentan."""
        return any(nombre in (getattr(n, "id", None), getattr(n, "attr", None))
                   for n in ast.walk(fns[fn]))

    cierran = {n for n in fns if usa(n, "cerrar_y_derivar")}
    assert cierran == {"guardar_tareas", "marcar_tarea_hecha_desde_proyectos"}, cierran
    for n in sorted(cierran):
        assert usa(n, "_derivadas_pedidas"), f"{n} no valida la derivada por la pieza común"
    # Lo ÚNICO que cambia entre las dos es cómo se lee el responsable, porque
    # las dos páginas lo escriben distinto (chat / nombre), no qué se decide.
    assert usa("marcar_tarea_hecha_desde_proyectos", "_responsable_por_nombre")
    assert not usa("guardar_tareas", "_responsable_por_nombre")


def test_la_tarea_que_sigue_nace_con_lo_que_se_escribio_por_la_puerta_de_siempre(mt, gente):
    """Título, fecha, grupo, responsable y de qué tarea sale, y la huella de
    creación: todo por `db.cerrar_y_derivar`, en el mismo gesto que cierra la
    madre. El responsable va y vuelve por NOMBRE: el número de chat no se
    escribe en la página."""
    antes = foto(mt)
    r = post("/proyectos/tarea/10/hecha", {
        "deriva_titulo_10_1": "  La que sigue  ", "deriva_vence_10_1": FECHA_Y_HORA_ESCRITA,
        "deriva_resp_10_1": "Persona Dos"})
    assert _adonde(r).startswith("/proyectos?p=2&hecho=tarea_hecha&derivadas=")
    hija_id = int(re.search(r"derivadas=(\d+)", _adonde(r)).group(1))
    hija = tarea(mt, hija_id)
    assert (hija["titulo"], hija["proyecto_id"], hija["area"], hija["responsable_chat_id"],
            hija["deriva_de_id"]) == ("La que sigue", 2, None, gente.rosi, 10)
    assert str(hija["vence_en"]).startswith(FECHA_Y_HORA_ESCRITA[:10])
    assert tarea(mt, 10)["estado"] == "hecha"
    h = huellas(mt)[-1]
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "crear", "tareas", hija_id)
    assert igual_salvo(antes, foto(mt), tareas={10, hija_id}, log_acciones={x["id"] for x in huellas(mt)})
    html = ver(mt, p=2, hecho="tarea_hecha", derivadas=str(hija_id))
    assert f"Salió la tarea que sigue (#{hija_id})." in html
    # El desplegable del responsable de la tarea nueva ofrece NOMBRES: el
    # número de chat no se escribe en la página.
    # La página ya no ofrece el renglón (ni siquiera con `?derivar=`): sin «＋».
    fila = ver(mt, p=2, derivar=10)
    assert "deriva_titulo" not in fila and "sale-otra" not in fila and str(gente.rosi) not in fila


def test_en_un_proyecto_cerrado_la_tarea_que_sigue_no_se_crea_y_la_madre_no_se_cierra(mt):
    """El proyecto cerrado no recibe la tarea nueva, y por eso la vieja NO se
    cierra (D6). Sin renglón, la misma tarea sí se cierra: lo que el proyecto
    cerrado impide es la tarea nueva, no cerrar lo que ya estaba."""
    mt.tarea(70, "pendiente de un cerrado", proyecto=3)
    foto0 = foto(mt)
    r = post("/proyectos/tarea/70/hecha", {"deriva_titulo_70_1": "La que sigue"})
    assert "error=tarea_derivada_cerrado" in _adonde(r)
    assert foto(mt) == foto0
    assert "no recibe la tarea nueva" in ver(mt, p=3, error="tarea_derivada_cerrado")
    assert _adonde(post("/proyectos/tarea/70/hecha")).startswith("/proyectos?p=3&hecho=tarea_hecha")
    assert tarea(mt, 70)["estado"] == "hecha"


@pytest.mark.parametrize("pagina", ["tareas", "proyectos"])
def test_hermanos_un_renglon_que_no_vale_no_cierra_la_madre_en_ninguna(mt, pagina):
    """El mismo renglón malo (un título de más de `LARGO_TITULO`) deja la tarea
    SIN cerrar en las dos pantallas, y sin escribir nada."""
    foto0 = foto(mt)
    largo = "x" * (db.LARGO_TITULO_TAREA + 1)
    if pagina == "tareas":
        r = post("/tareas", {"filtro": "", "prev_10": "pendiente", "hecha_10": "1",
                             "deriva_titulo_10_1": largo})
        assert _adonde(r).startswith("/tareas?")
    else:
        r = post("/proyectos/tarea/10/hecha", {"deriva_titulo_10_1": largo})
        assert "error=tarea_derivada" in _adonde(r)
    assert tarea(mt, 10)["estado"] == "pendiente", pagina
    assert foto(mt) == foto0, pagina


def test_el_censo_de_escritores_ve_un_escritor_inventado():
    fuente = "async def inventado(c):\n    await c.execute('UPDATE tareas SET titulo = 1, estado = 2 WHERE id = 3')\n"
    n = [n for n in ast.walk(ast.parse(fuente)) if isinstance(n, ast.Constant)][0]
    m = re.search(r"UPDATE\s+tareas\s+(?:\w+\s+)?SET\s+(.*?)(?:WHERE|FROM|$)", n.value, re.I | re.S)
    assert re.search(r"\btitulo\s*=", m.group(1)) and re.search(r"\bestado\s*=", m.group(1))


def test_la_nota_del_menu_cuenta_lo_que_se_hace_con_una_tarea():
    import web.menu as menu
    nota = next(p.nota for p in menu.pantallas() if p.ruta == "/proyectos")
    for dicho in ("se marca hecha o se desmarca", "el título", "el responsable", "se comenta",
                  "se edita un comentario", "se borra con la ×", "nace con el responsable del proyecto",
                  "se guarda solo", "la tarea que sale de ella"):
        assert dicho in nota, dicho
