"""Cambiar el responsable de una tarea desde el detalle de Proyectos (Tiziano, 7-oct-2026).

«En las tareas, tambien quiero poder cambiar el resposable.» Preguntado si era en
Proyectos, al abrir una tarea: «Si, exacto». Preguntado si Lucy debía avisarle por
Telegram a la persona: «No».

QUÉ VIGILA ESTE ARCHIVO (todo por la ruta real y el SQL real en SQLite, sin dobles
sobre lo que dice vigilar, salvo el espía de Telegram, que solo cuenta llamadas):

  1. el desplegable está en el detalle abierto de TODA tarea de TODA vista (la lista
     de tareas y de vistas sale de la tabla del mundo de prueba, no de una lista
     escrita aquí) y en ningún otro sitio;
  2. sus opciones son las de `config.opciones_de_responsable` (las de `/tareas`),
     por nombre, nunca un número de chat; y cada una se puede guardar;
  3. cambiarlo guarda, deja huella `panel`/`editar`, el aviso de la página que
     contesta es el verdadero, y NO sale ningún mensaje de Telegram;
  4. si no cambió (mismo valor, nombre que no vale, tarea que ya no está) ninguna
     pantalla dice «guardado»;
  5. la sesión de solo ver no ve el control y su POST no escribe; sin sesión, igual;
  6. el formulario es `form.marcar` + `data-auto`: guarda al escoger y sin mover la
     página (la pieza única de `_marcar_sin_saltar.html`);
  7. una dirección escrita a mano no pinta el aviso.

FRONTERA: no es Postgres ni un navegador. Lo que el navegador hace con el formulario
(escoger y no saltar) se midió a mano, y el guion de marcar sin saltar lo prueba
`tests/test_marcar_sin_saltar.py`. `crud.deshacer` de la huella que deja el panel
lo prueba `tests/test_responsable.py::test_la_huella_que_deja_el_panel_se_deshace_por_la_misma_puerta`
con dobles, porque SQLite no tiene `jsonb_populate_record`.
"""
from __future__ import annotations

import re

import pytest

from test_escrituras_proyecto import _formularios_de
from test_escrituras_tarea import foto, huellas, igual_salvo, mt, post, tarea  # noqa: F401
from test_pagina_proyectos import _cliente, gente, mundo, ver  # noqa: F401
from test_proyectos_solo_ver import cliente as _cliente_de
import config
import db.db as db

AVISOS = re.compile(r'<p class="aviso[^"]*">(.*?)</p>', re.S)
GUARDADO = "Responsable de la tarea guardado."


def _avisos(html: str) -> list[str]:
    return [a.strip() for a in AVISOS.findall(html)]


def _formulario(html: str, tid: int):
    """El formulario de responsable de la tarea `tid`, o None. Sale del lector de
    formularios de las pruebas, no de una regex sobre el texto."""
    hallados = [f for f in _formularios_de(html) if f["accion"] == f"/proyectos/tarea/{tid}/responsable"]
    assert len(hallados) <= 1, hallados
    return hallados[0] if hallados else None


def _opciones(html: str, tid: int) -> list[tuple[str, str, bool]]:
    """`(valor, etiqueta, escogida)` del desplegable de la tarea, del HTML real."""
    trozo = re.search(r'<select id="rt-%d" name="responsable">(.*?)</select>' % tid, html, re.S).group(1)
    return [(m.group(1), m.group(3).strip(), "selected" in m.group(2))
            for m in re.finditer(r'<option value="([^"]*)"([^>]*)>(.*?)</option>', trozo, re.S)]


def _vista_de(m, tid: int) -> dict:
    """La vista de `/proyectos` donde cae la tarea con su detalle abierto, calculada
    de la fila (proyecto vivo → `p`; si no, su grupo; si no, «Sin grupo»)."""
    t = tarea(m, tid)
    vivo = t["proyecto_id"] and m.con.execute(
        "SELECT 1 FROM proyectos WHERE id = ? AND borrado_en IS NULL", (t["proyecto_id"],)).fetchone()
    if vivo:
        return {"p": t["proyecto_id"], "t": tid}
    if t["area"]:
        return {"g": t["area"], "t": tid}
    return {"sin_grupo": 1, "t": tid}


# ── 1. Dónde está el control ─────────────────────────────────────────────

def test_todo_detalle_de_toda_tarea_trae_el_desplegable_y_solo_el_suyo(mt):
    ids = [r[0] for r in mt.con.execute("SELECT id FROM tareas WHERE borrado_en IS NULL ORDER BY id")]
    assert len(ids) >= 7, ids          # hechas, pendientes, sueltas, sin grupo y de proyecto cerrado
    for tid in ids:
        vista = _vista_de(mt, tid)
        html = ver(mt, **vista)
        assert _formulario(html, tid) is not None, (tid, vista)
        for otro in ids:
            if otro != tid:
                assert _formulario(html, otro) is None, (tid, otro, vista)
        # y sin abrir el detalle, ninguna tarea lo trae
        sin_t = {k: v for k, v in vista.items() if k != "t"}
        assert not any(re.search(r"/proyectos/tarea/\d+/responsable", f["accion"])
                       for f in _formularios_de(ver(mt, **sin_t))), (tid, sin_t)


def test_el_formulario_es_form_marcar_y_se_guarda_solo_al_escoger(mt):
    f = _formulario(ver(mt, p=2, t=10), 10)
    clases = f["clase"].split()
    assert "marcar" in clases and "resp" in clases and "data-auto" in f["atributos"], f


# ── 2. Las opciones son las de /tareas ───────────────────────────────────

def test_las_opciones_son_las_de_tareas_por_nombre_y_nunca_un_numero(mt, gente):
    esperadas = [etiqueta for _, etiqueta in config.opciones_de_responsable()]
    assert esperadas[0] == "sin responsable" and config.NOMBRE_CODE in esperadas and len(esperadas) >= 4
    for vista in ({"p": 2, "t": 10}, {"g": "CDS", "t": 30}, {"sin_grupo": 1, "t": 31}):
        tid = vista["t"]
        html = ver(mt, **vista)
        ops = [o for o in _opciones(html, tid) if o[1] and not (o[0] == "" and o[1] not in esperadas)]
        assert [e for _, e, _ in ops] == esperadas, vista
        # el valor de cada una es el nombre (la de «sin responsable», vacío)
        assert [v for v, _, _ in ops] == ["" if e == "sin responsable" else e for e in esperadas], vista
        assert re.search(r'value="-?\d{4,}"', html) is None, vista


def test_cada_opcion_del_desplegable_se_puede_guardar_y_queda_esa_persona(mt, gente):
    nombre_a_chat = {n: c for c, n in config.nombres_con_code().items()}
    for _, etiqueta in config.opciones_de_responsable():
        valor = "" if etiqueta == "sin responsable" else etiqueta
        # se parte de otra persona, para que SIEMPRE haya un cambio
        mt.con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 12",
                       (gente.rosi if valor != config.nombres_con_code()[gente.rosi] else gente.dueno,))
        mt.con.commit()
        r = post("/proyectos/tarea/12/responsable", {"responsable": valor})
        assert "hecho=tarea_responsable" in r.headers["location"], (etiqueta, r.headers["location"])
        assert tarea(mt, 12)["responsable_chat_id"] == (nombre_a_chat[valor] if valor else None), etiqueta


# ── 3. Cambiarlo: queda, se ve, el aviso es el verdadero y no hay Telegram ──

@pytest.fixture
def telegram_espia(monkeypatch):
    import telegram
    llamadas = []

    async def espia(self, *a, **k):
        llamadas.append((a, k))
    for nombre in ("send_message", "send_photo", "send_document"):
        monkeypatch.setattr(telegram.Bot, nombre, espia, raising=False)
    return llamadas


def test_cambiarlo_guarda_deja_huella_y_la_pagina_que_contesta_lo_dice_y_lo_enseña(mt, gente, telegram_espia):
    antes = foto(mt)
    c = _cliente(config.CHAT_ID_DUENO)
    r = c.post("/proyectos/tarea/10/responsable", data={"responsable": "Persona Dos"}, follow_redirects=True)
    assert r.status_code == 200 and str(r.url).endswith("#tarea-10") or "t=10" in str(r.url), str(r.url)
    html = r.text
    assert _avisos(html).count(GUARDADO) == 1, _avisos(html)
    assert tarea(mt, 10)["responsable_chat_id"] == gente.rosi
    # el detalle (abierto en la misma página) ya dice la persona nueva, en las dos partes
    detalle = html.split('<div class="detalle">', 1)[1].split("<h4>Comentarios</h4>")[0]
    assert "<b>Persona Dos</b><span>Responsable</span>" in detalle
    assert [e for _, e, esc in _opciones(html, 10) if esc] == ["Persona Dos"]
    # una sola huella, la del panel; nada más cambió
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "tareas", 10)
    assert igual_salvo(antes, foto(mt), tareas={10}, log_acciones={h["id"]})
    # y NADIE recibió nada por Telegram (Tiziano, 7-oct-2026: «No»)
    assert telegram_espia == []


def test_quitarlo_deja_la_tarea_sin_responsable_y_la_pagina_lo_dice(mt, gente):
    post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"})
    r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/10/responsable", data={"responsable": ""},
                                             follow_redirects=True)
    assert tarea(mt, 10)["responsable_chat_id"] is None
    assert _avisos(r.text).count(GUARDADO) == 1
    assert "Nadie asignado." in r.text.split('<div class="detalle">', 1)[1].split("<h4>Comentarios</h4>")[0]
    assert [e for _, e, esc in _opciones(r.text, 10) if esc] == ["sin responsable"]


# ── 4. Si no cambió, ninguna pantalla dice «guardado» ────────────────────

def _sin_exito(html: str) -> None:
    assert not any("guardado" in a for a in _avisos(html)), _avisos(html)


def test_el_mismo_responsable_dice_que_no_cambio_nada_y_no_deja_huella(mt, gente):
    post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"})
    antes = foto(mt)
    r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/10/responsable",
                                             data={"responsable": "Persona Dos"}, follow_redirects=True)
    _sin_exito(r.text)
    assert "No cambió nada: ya estaba así, o ya no está." in _avisos(r.text)
    assert foto(mt) == antes


@pytest.mark.parametrize("quien", ["Nadie", "Ajeno", "700100999", "x" * 300, " Persona Dos x"])
def test_un_responsable_que_no_vale_dice_que_no_vale_y_no_escribe(mt, gente, quien):
    antes = foto(mt)
    r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/10/responsable",
                                             data={"responsable": quien}, follow_redirects=True)
    _sin_exito(r.text)
    assert any(a.startswith("Ese responsable no vale: se escoge entre ") for a in _avisos(r.text)), _avisos(r.text)
    assert foto(mt) == antes


def test_una_tarea_que_ya_no_esta_no_dice_guardado_ni_escribe(mt, gente):
    mt.con.execute("UPDATE tareas SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id = 10")
    mt.con.commit()
    antes = foto(mt)
    for tid in (10, 99999):
        r = _cliente(config.CHAT_ID_DUENO).post(f"/proyectos/tarea/{tid}/responsable",
                                                 data={"responsable": "Persona Dos"}, follow_redirects=True)
        assert r.status_code == 200
        _sin_exito(r.text)
        assert foto(mt) == antes
        assert _avisos(r.text), "dijo… nada: tiene que decir que la tarea no está"


def test_una_direccion_escrita_a_mano_no_pinta_el_aviso_de_guardado(mt):
    for consulta in ({"p": 2, "t": 10, "hecho": "tarea_responsable"}, {"p": 2, "hecho": "tarea_responsable"}):
        assert GUARDADO not in _avisos(ver(mt, **consulta)), consulta


# ── 5. Quién puede ──────────────────────────────────────────────────────

def test_solo_ver_ni_ve_el_control_ni_puede_cambiarlo_y_sin_sesion_tampoco(mt, gente):
    for vista in ({"p": 2, "t": 10}, {"g": "CDS", "t": 30}):
        r = _cliente_de("ver").get("/proyectos", params=vista)
        assert r.status_code == 200
        assert "/responsable" not in r.text.replace('/proyectos/2/responsable', ''), vista
        assert 'id="rt-' not in r.text, vista
    antes = foto(mt)
    for con in ("ver", None):
        r = _cliente_de(con).post("/proyectos/tarea/10/responsable", data={"responsable": "Persona Dos"},
                                  follow_redirects=False)
        assert r.status_code == 401, (con, r.status_code)
        assert foto(mt) == antes


def test_alguien_que_entra_al_panel_pero_no_es_el_dueno_tambien_puede(mt, gente):
    r = post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"}, chat=gente.rosi)
    assert "hecho=tarea_responsable" in r.headers["location"]
    assert tarea(mt, 10)["responsable_chat_id"] == gente.rosi


def test_el_chat_de_alguien_que_no_entra_no_puede_escribir_aunque_firme_su_sesion(mt, gente):
    antes = foto(mt)
    r = post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"}, chat=700100999)
    assert r.status_code == 401 and foto(mt) == antes


# ── el responsable que ya no se puede asignar no se pierde ni se ofrece ───

def test_un_responsable_que_ya_no_se_puede_asignar_se_ve_pero_no_se_ofrece(mt, gente, monkeypatch):
    mt.con.execute("UPDATE tareas SET responsable_chat_id = ? WHERE id = 10", (gente.rosi,))
    mt.con.commit()
    # Rosi conserva su nombre pero deja de entrar al panel: ya no es una opción.
    monkeypatch.setattr(config, "personas_del_panel",
                        lambda: tuple(p for p in config.NOMBRES_POR_CHAT.items()
                                      if p[0] != gente.rosi and p[0] in config.CHAT_IDS_PERMITIDOS))
    ops = _opciones(ver(mt, p=2, t=10), 10)
    assert ("", "Persona Dos", True) in ops                 # se ve, escogida y sin poder re-escogerse
    assert [v for v, e, _ in ops if e == "Persona Dos" and v] == []      # y no se ofrece como valor
