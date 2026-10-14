"""Escoger el responsable al crear una tarea y cambiarle la fecha después, en Proyectos
(Tiziano, 13-oct-2026): «al momento de crear una tarea quiero que se pueda seleccionar el
responsable y que luego de creada una tarea se le pueda modificar la fecha.»

QUÉ VIGILA ESTE ARCHIVO (todo por la ruta real, la plantilla real y el SQL real en SQLite; el
único doble es un espía que cuenta llamadas a Telegram):

  1. «Nueva tarea» trae un desplegable de responsable con las opciones de `/tareas` (por nombre,
     nunca un número), con el responsable del proyecto escogido de entrada;
  2. cada opción se puede crear y la tarea nace con ESA persona (incluida «sin responsable»,
     aunque el proyecto tenga uno); un nombre que no vale no crea nada y lo dice;
  3. el criterio de «quién puede ser responsable» es el mismo que el de `/tareas/nueva`
     (hermanos): misma entrada, misma respuesta;
  4. el detalle de TODA tarea pendiente trae el campo de fecha, y ninguna otra (hechas, vista
     sin detalle, solo ver);
  5. cambiar la fecha escribe lo mismo que `/tareas` (`db.mover_vence`) para la misma entrada:
     fila y huella iguales; quitarla; no cambiar nada; fecha que no se entiende; sin Telegram,
     sin tocar los avisos;
  6. tarea hecha, borrada, que no existe o de otro proyecto: no se mueve por una dirección a mano;
  7. solo ver y sin sesión no ven el campo ni escriben; el filtro de la página viaja;
  8. el aviso que se pinta dice lo que quedó guardado.

FRONTERA: no es Postgres ni un navegador. SQLite no tiene `jsonb_populate_record`, así que
`crud.deshacer` de la huella que deja `db.mover_vence` NO se ejerció aquí (la huella es la misma
que escribe `/tareas`; lo que se comprueba es que las dos rutas la escriben igual). El
desplazamiento de la página al guardar (`form.marcar`) lo prueba `tests/test_marcar_sin_saltar.py`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from test_escrituras_proyecto import _formularios_de
from test_escrituras_tarea import foto, huellas, igual_salvo, mt, post, tarea  # noqa: F401
from test_pagina_proyectos import _cliente, gente, ver, ver_r  # noqa: F401
from test_proyectos_solo_ver import cliente as _cliente_de
import config
import db.db as db

AVISOS = re.compile(r'<p class="aviso[^"]*">(.*?)</p>', re.S)


def _avisos(html: str) -> list[str]:
    return [a.strip() for a in AVISOS.findall(html)]


@pytest.fixture
def telegram_espia(monkeypatch):
    import telegram
    llamadas = []

    async def espia(self, *a, **k):
        llamadas.append((a, k))
    for nombre in ("send_message", "send_photo", "send_document"):
        monkeypatch.setattr(telegram.Bot, nombre, espia, raising=False)
    return llamadas


def _nuevas(antes, despues, tabla="tareas"):
    return {f["id"] for f in despues[tabla]} - {f["id"] for f in antes[tabla]}


# ═══════════════════════════════════════════════════════════════════════
# 1 y 2. El responsable al crear
# ═══════════════════════════════════════════════════════════════════════

def _select_de_nueva(html: str, pid: int):
    """`(valor, etiqueta, escogida)` del desplegable de «Nueva tarea» del proyecto `pid`,
    leído del formulario real (el lector de formularios de las pruebas)."""
    hallados = [f for f in _formularios_de(html) if f["accion"] == f"/proyectos/{pid}/tareas"]
    assert len(hallados) == 1, hallados
    campos = [c for c in hallados[0]["campos"] if c["name"] == "responsable"]
    assert len(campos) == 1, hallados[0]["campos"]
    return [(o["value"] if o["value"] is not None else o["texto"].strip(), o["texto"].strip(), o["selected"])
            for o in campos[0]["opciones"]]


def test_nueva_tarea_trae_el_desplegable_con_las_opciones_de_tareas_por_nombre(mt, gente):
    esperadas = [e for _, e in config.opciones_de_responsable()]
    assert esperadas[0] == "sin responsable" and config.NOMBRE_CODE in esperadas and len(esperadas) >= 4
    html = ver(mt, p=2)
    ops = _select_de_nueva(html, 2)
    assert [e for _, e, _ in ops] == esperadas
    assert [v for v, _, _ in ops] == ["" if e == "sin responsable" else e for e in esperadas]
    nuevo = html.split('class="nueva-tarea"', 1)[1].split("</form>", 1)[0]
    assert re.search(r"-?\d{6,}", nuevo) is None            # nunca un número de chat


def test_sale_escogido_el_responsable_del_proyecto_y_si_no_hay_o_no_puede_serlo_sin_responsable(mt, gente):
    escogidas = lambda pid: [e for _, e, esc in _select_de_nueva(ver(mt, p=pid), pid) if esc]   # noqa: E731
    assert escogidas(2) == ["Persona Uno"]                  # el del proyecto 2 (el dueño)
    assert escogidas(1) == ["Persona Dos"]                  # el del proyecto 1
    mt.proyecto(5, "Sin responsable", area="CDS")
    mt.con.commit()
    assert escogidas(5) == ["sin responsable"]
    mt.con.execute("UPDATE proyectos SET responsable_chat_id = 700100999 WHERE id = 2")   # ya no puede serlo
    mt.con.commit()
    assert escogidas(2) == ["sin responsable"]


def test_el_desplegable_no_sale_en_un_proyecto_cerrado_ni_en_solo_ver(mt):
    assert 'action="/proyectos/3/tareas"' not in ver(mt, p=3)
    r = _cliente_de("ver").get("/proyectos", params={"p": 2})
    assert r.status_code == 200 and 'action="/proyectos/2/tareas"' not in r.text


def test_cada_opcion_crea_la_tarea_con_esa_persona_y_sin_responsable_es_sin_responsable(mt, gente):
    nombre_a_chat = {n: c for c, n in config.nombres_con_code().items()}
    for _, etiqueta in config.opciones_de_responsable():
        valor = "" if etiqueta == "sin responsable" else etiqueta
        antes = foto(mt)
        r = post("/proyectos/2/tareas", {"titulo": f"para {etiqueta}", "vence": "", "responsable": valor})
        despues = foto(mt)
        nueva, = _nuevas(antes, despues)
        assert f"tarea_creada={nueva}" in r.headers["location"] and "error=" not in r.headers["location"], etiqueta
        # el proyecto 2 tiene responsable (el dueño): «sin responsable» tiene que ganarle
        assert tarea(mt, nueva)["responsable_chat_id"] == (nombre_a_chat[valor] if valor else None), etiqueta
        assert tarea(mt, nueva)["proyecto_id"] == 2


def test_la_pagina_dice_quien_es_el_responsable_de_la_tarea_creada(mt, gente):
    post("/proyectos/2/tareas", {"titulo": "Mezclar", "responsable": "Persona Dos"})
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    html = ver_r(mt, p=2, tarea_creada=nueva)
    fila = re.search(rf'id="tarea-{nueva}".*?<div class="fila" data-edita>(.*?)\n  </div>', html, re.S).group(1)
    assert 'title="Responsable: Persona Dos"' in fila
    assert any(a.startswith(f"Tarea creada (#{nueva})") for a in _avisos(html))


@pytest.mark.parametrize("quien", ["Nadie", "Ajeno", "700100999", "x" * 300, " Persona Dos x", "7001000010"])
def test_un_responsable_que_no_vale_no_crea_la_tarea_y_la_pagina_lo_dice(mt, gente, quien):
    antes = foto(mt)
    r = post("/proyectos/2/tareas", {"titulo": "no entra", "responsable": quien})
    assert r.headers["location"] == "/proyectos?p=2&error=tarea_nueva_responsable"
    assert foto(mt) == antes
    avisos = _avisos(ver_r(mt, p=2, error="tarea_nueva_responsable"))
    assert any(a.startswith("Ese responsable no vale: se escoge entre ") and a.endswith("La tarea NO se creó.")
               for a in avisos), avisos
    assert not any(a.startswith("Tarea creada") for a in avisos)


def test_hermanos_la_nueva_tarea_y_el_detalle_leen_igual_lo_que_llega_en_el_campo(mt, gente):
    """Misma entrada en `/proyectos/N/tareas` y en `/proyectos/tarea/N/responsable`: misma respuesta
    (las dos pasan por `crud.PUERTAS["tareas"]["responsable_chat_id"]` y `config.puede_ser_responsable`).
    Incluye lo que esa puerta acepta además del nombre (el número de chat de una persona de la casa,
    como en Telegram) y lo que rechaza."""
    entradas = ["", "Persona Dos", "persona dos", " Persona Dos ", config.NOMBRE_CODE, str(gente.rosi),
                "Nadie", "700100999", "x" * 300, "Persona Dos x"]
    for quien in entradas:
        n = post("/proyectos/2/tareas", {"titulo": "h", "responsable": quien})
        d = post("/proyectos/tarea/12/responsable", {"responsable": quien})
        rechaza_nueva = "error=tarea_nueva_responsable" in n.headers["location"]
        rechaza_detalle = "error=tarea_responsable" in d.headers["location"]
        assert rechaza_nueva == rechaza_detalle, (quien[:20], n.headers["location"], d.headers["location"])
        if not rechaza_nueva:
            nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
            assert tarea(mt, nueva)["responsable_chat_id"] == tarea(mt, 12)["responsable_chat_id"] or \
                "tarea_igual" in d.headers["location"], quien[:20]


def test_un_responsable_que_ya_no_puede_serlo_no_crea_la_tarea_aunque_su_nombre_siga_(mt, gente, monkeypatch):
    # Rosi conserva su nombre pero deja de entrar al panel: lo decide `config.puede_ser_responsable`.
    monkeypatch.setattr(config, "personas_del_panel",
                        lambda: tuple(p for p in config.NOMBRES_POR_CHAT.items() if p[0] != gente.rosi))
    antes = foto(mt)
    r = post("/proyectos/2/tareas", {"titulo": "no entra", "responsable": "Persona Dos"})
    assert "error=tarea_nueva_responsable" in r.headers["location"] and foto(mt) == antes


def test_sin_el_campo_la_tarea_nace_con_el_responsable_del_proyecto_como_siempre(mt, gente):
    post("/proyectos/2/tareas", {"titulo": "formulario viejo"})
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert tarea(mt, nueva)["responsable_chat_id"] == gente.dueno


def test_si_se_escoge_a_code_y_el_proyecto_no_es_de_ia_avisa_que_la_sala_no_la_ve(mt):
    r = post("/proyectos/2/tareas", {"titulo": "Para Code", "responsable": config.NOMBRE_CODE})
    assert "&sala_no=1" in r.headers["location"]
    nueva = max(f[0] for f in mt.con.execute("SELECT id FROM tareas"))
    assert tarea(mt, nueva)["responsable_chat_id"] == config.CHAT_ID_CODE


def test_un_proyecto_cerrado_o_que_no_existe_no_recibe_la_tarea_aunque_se_escoja_responsable(mt, gente):
    antes = foto(mt)
    assert post("/proyectos/3/tareas", {"titulo": "x", "responsable": "Persona Dos"}).headers["location"] \
        == "/proyectos?p=3&error=proyecto_cerrado"
    assert post("/proyectos/999/tareas", {"titulo": "x", "responsable": "Persona Dos"}).headers["location"] \
        .startswith("/proyectos?p=999&error=proyecto")
    assert foto(mt) == antes


def test_hermanos_misma_entrada_misma_respuesta_que_tareas_nueva(mt, gente):
    """`/tareas/nueva` lee el responsable como chat y `/proyectos/N/tareas` como nombre, pero QUIÉN
    puede serlo lo decide la misma puerta: cada persona que `/tareas/nueva` acepta, Proyectos la
    acepta, y lo que una rechaza la otra también. La lista de entradas sale de `config`, más tres
    inventadas."""
    nombres = config.nombres_con_code()               # {chat: nombre}, incluida Code
    entradas = [(chat, nombre) for chat, nombre in nombres.items()]
    entradas += [(700100999, "Ajeno"), (0, "x"), (-5, "Nadie")]       # no entran ni con nombre
    for chat, nombre in entradas:
        a = post("/tareas/nueva", {"titulo": f"hermana {nombre}", "responsable": str(chat)})
        b = post("/proyectos/2/tareas", {"titulo": f"hermana {nombre}", "responsable": nombre})
        acepta_tareas = "error=" not in a.headers["location"]
        acepta_proyectos = "error=" not in b.headers["location"]
        assert acepta_tareas == acepta_proyectos == config.puede_ser_responsable(chat) is True or (
            not acepta_tareas and not acepta_proyectos and not config.puede_ser_responsable(chat)), (
            chat is not None and nombre, a.headers["location"], b.headers["location"])
    filas = {f["titulo"]: f["responsable_chat_id"] for f in map(dict, mt.con.execute(
        "SELECT titulo, responsable_chat_id FROM tareas WHERE titulo LIKE 'hermana %'"))}
    for chat, nombre in nombres.items():
        # las dos rutas dejaron a la misma persona (dos filas con el mismo título)
        assert sum(1 for f in mt.con.execute("SELECT 1 FROM tareas WHERE titulo = ? AND responsable_chat_id = ?",
                                              (f"hermana {nombre}", chat))) == 2, nombre
    assert all(t not in filas for t in ("hermana Ajeno", "hermana x", "hermana Nadie"))


def test_solo_ver_y_sin_sesion_no_crean_tareas(mt, gente):
    antes = foto(mt)
    for con in ("ver", None):
        r = _cliente_de(con).post("/proyectos/2/tareas", data={"titulo": "x", "responsable": "Persona Dos"},
                                  follow_redirects=False)
        assert r.status_code == 401 and foto(mt) == antes, con


# ═══════════════════════════════════════════════════════════════════════
# 4. Dónde está el campo de fecha
# ═══════════════════════════════════════════════════════════════════════

def _form_fecha(html: str, tid: int):
    hallados = [f for f in _formularios_de(html) if f["accion"].startswith(f"/proyectos/tarea/{tid}/fecha")]
    assert len(hallados) <= 1, hallados
    return hallados[0] if hallados else None


def _vista_de(m, tid: int) -> dict:
    t = tarea(m, tid)
    vivo = t["proyecto_id"] and m.con.execute(
        "SELECT 1 FROM proyectos WHERE id = ? AND borrado_en IS NULL", (t["proyecto_id"],)).fetchone()
    if vivo:
        return {"p": t["proyecto_id"], "t": tid}
    if t["area"]:
        return {"g": t["area"], "t": tid}
    return {"sin_grupo": 1, "t": tid}


def test_el_detalle_de_toda_tarea_pendiente_trae_la_fecha_y_ninguna_otra_la_trae(mt):
    filas = [dict(f) for f in mt.con.execute("SELECT id, estado FROM tareas WHERE borrado_en IS NULL ORDER BY id")]
    assert {f["estado"] for f in filas} == {"pendiente", "hecha"} and len(filas) >= 7
    for f in filas:
        vista = _vista_de(mt, f["id"])
        html = ver(mt, **vista)
        esta = _form_fecha(html, f["id"]) is not None
        assert esta == (f["estado"] == "pendiente"), (f, vista)
        for otra in filas:
            if otra["id"] != f["id"]:
                assert _form_fecha(html, otra["id"]) is None, (f, otra)
        sin_t = {k: v for k, v in vista.items() if k != "t"}
        assert not any("/fecha" in x["accion"] for x in _formularios_de(ver(mt, **sin_t))), sin_t


def test_el_campo_trae_la_fecha_y_hora_de_santo_domingo_y_el_piso_y_se_guarda_sin_mover_la_pagina(mt):
    f = _form_fecha(ver(mt, p=2, t=10), 10)
    campos = {c["name"]: c for c in f["campos"]}
    assert set(campos) == {"prev_vence", "vence"} and campos["vence"]["tipo"] == "datetime-local"
    esperado = db.dia_rd  # noqa: F841  (la hora se compara con la fila, abajo)
    fila = tarea(mt, 10)["vence_en"]
    assert campos["vence"]["value"] == campos["prev_vence"]["value"] == "2026-10-14T12:00", (fila, campos)
    assert {"marcar", "fecha-t"} <= set(f["clase"].split()) and "data-auto" in f["atributos"]
    assert f["accion"] == "/proyectos/tarea/10/fecha"


def test_una_tarea_sin_fecha_trae_el_campo_vacio(mt):
    campos = {c["name"]: c for c in _form_fecha(ver(mt, p=2, t=12), 12)["campos"]}
    assert campos["vence"]["value"] in (None, "") and campos["prev_vence"]["value"] in (None, "")


def test_solo_ver_no_ve_el_campo_y_su_post_no_escribe_y_sin_sesion_tampoco(mt):
    for vista in ({"p": 2, "t": 10}, {"g": "CDS", "t": 30}):
        r = _cliente_de("ver").get("/proyectos", params=vista)
        assert r.status_code == 200 and "/fecha" not in r.text and 'id="ft-' not in r.text, vista
    antes = foto(mt)
    for con in ("ver", None):
        r = _cliente_de(con).post("/proyectos/tarea/10/fecha", data={"vence": "2026-10-20T09:30"},
                                  follow_redirects=False)
        assert r.status_code == 401 and foto(mt) == antes, con


def test_alguien_de_la_casa_que_no_es_el_dueno_tambien_puede(mt, gente):
    r = post("/proyectos/tarea/10/fecha", {"vence": "2026-10-20T09:30"}, chat=gente.rosi)
    assert "hecho=tarea_fecha" in r.headers["location"]


def test_el_chat_de_alguien_que_no_entra_no_puede_aunque_firme_su_sesion(mt):
    antes = foto(mt)
    r = post("/proyectos/tarea/10/fecha", {"vence": "2026-10-20T09:30"}, chat=700100999)
    assert r.status_code == 401 and foto(mt) == antes


# ═══════════════════════════════════════════════════════════════════════
# 5. Cambiar, quitar, no cambiar, no entender
# ═══════════════════════════════════════════════════════════════════════

def test_cambiar_la_fecha_guarda_hora_de_santo_domingo_deja_huella_y_la_pagina_lo_dice(mt, telegram_espia):
    antes = foto(mt)
    r = _cliente(config.CHAT_ID_DUENO).post(
        "/proyectos/tarea/10/fecha", data={"vence": "2026-10-20T09:30", "prev_vence": "2026-10-14T12:00"},
        follow_redirects=True)
    assert r.status_code == 200
    assert _avisos(r.text).count("Fecha de la tarea guardada.") == 1, _avisos(r.text)
    t = tarea(mt, 10)
    assert datetime.fromisoformat(str(t["vence_en"])).astimezone(config.TZ).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-20 09:30"
    campos = {c["name"]: c for c in _form_fecha(r.text, 10)["campos"]}
    assert campos["vence"]["value"] == "2026-10-20T09:30"           # el detalle sigue abierto y ya dice la nueva
    h, = huellas(mt)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "tareas", 10)
    assert h["motivo"] == "fecha movida desde el panel de tareas"
    assert igual_salvo(antes, foto(mt), tareas={10}, log_acciones={h["id"]})
    assert telegram_espia == []


def test_mover_para_mas_tarde_cuenta_como_posponer_y_no_toca_los_avisos(mt):
    mt.con.execute("UPDATE tareas SET avisos_enviados = '{0}', anticipos_min = '{0}' WHERE id = 10")
    mt.con.commit()
    antes = tarea(mt, 10)
    post("/proyectos/tarea/10/fecha", {"vence": "2026-10-30T09:30", "prev_vence": "2026-10-14T12:00"})
    despues = tarea(mt, 10)
    assert despues["pospuesta_veces"] == (antes["pospuesta_veces"] or 0) + 1
    assert (despues["avisos_enviados"], despues["anticipos_min"]) == (antes["avisos_enviados"], antes["anticipos_min"])
    post("/proyectos/tarea/10/fecha", {"vence": "2026-10-12T09:30", "prev_vence": "2026-10-30T09:30"})
    assert tarea(mt, 10)["pospuesta_veces"] == despues["pospuesta_veces"]          # para antes no posponer


def test_a_una_tarea_sin_fecha_se_le_puede_poner_una(mt):
    r = post("/proyectos/tarea/12/fecha", {"vence": "2026-10-20T09:30", "prev_vence": ""})
    assert "hecho=tarea_fecha" in r.headers["location"] and tarea(mt, 12)["vence_en"] is not None


def test_vaciar_el_campo_quita_la_fecha_y_la_pagina_dice_que_quedo_sin_fecha(mt):
    r = _cliente(config.CHAT_ID_DUENO).post(
        "/proyectos/tarea/10/fecha", data={"vence": "", "prev_vence": "2026-10-14T12:00"}, follow_redirects=True)
    assert tarea(mt, 10)["vence_en"] is None
    assert _avisos(r.text).count("Fecha de la tarea quitada: queda sin fecha.") == 1, _avisos(r.text)
    assert not any("guardada" in a for a in _avisos(r.text))
    h, = huellas(mt)
    assert h["motivo"] == "fecha quitada desde el panel de tareas"


def test_dejar_la_misma_fecha_dice_que_no_cambio_nada_y_no_escribe(mt):
    antes = foto(mt)
    r = _cliente(config.CHAT_ID_DUENO).post(
        "/proyectos/tarea/10/fecha", data={"vence": "2026-10-14T12:00", "prev_vence": "2026-10-14T12:00"},
        follow_redirects=True)
    assert foto(mt) == antes
    avisos = _avisos(r.text)
    assert "No cambió nada: la tarea ya tenía esa fecha, o ya no está pendiente." in avisos, avisos
    assert not any("guardada" in a or "quitada" in a for a in avisos)


def test_guardar_sin_tocar_el_campo_no_recorta_los_segundos_de_una_fecha_que_los_tiene(mt):
    """El campo del navegador enseña minutos: una tarea con `09:30:45` (la puso el chat) se pinta `09:30`.
    Guardar sin tocarlo NO la mueve a `09:30:00`: lo evita `prev_vence`, lo que se pintó."""
    mt.con.execute("UPDATE tareas SET vence_en = '2026-10-14T09:30:45-04:00' WHERE id = 10")
    mt.con.commit()
    campos = {c["name"]: c for c in _form_fecha(ver(mt, p=2, t=10), 10)["campos"]}
    assert campos["vence"]["value"] == campos["prev_vence"]["value"] == "2026-10-14T09:30"
    antes = foto(mt)
    r = post("/proyectos/tarea/10/fecha", {"vence": "2026-10-14T09:30", "prev_vence": "2026-10-14T09:30"})
    assert "error=tarea_fecha_igual" in r.headers["location"] and foto(mt) == antes


@pytest.mark.parametrize("malo", ["mañana", "2026-10-20", "2026-10-20T25:99", "2019-12-31T10:00",
                                  "2026-10-20T09:30+05:00", "2026-10-20 09:30", "x" * 300, "0000-01-01T00:00"])
def test_una_fecha_que_no_se_entiende_no_cambia_nada_y_la_pagina_lo_dice(mt, malo):
    antes = foto(mt)
    r = _cliente(config.CHAT_ID_DUENO).post(
        "/proyectos/tarea/10/fecha", data={"vence": malo, "prev_vence": "2026-10-14T12:00"}, follow_redirects=True)
    assert foto(mt) == antes
    avisos = _avisos(r.text)
    assert "La fecha no se entendió: la tarea sigue con la fecha que tenía." in avisos, avisos
    assert not any("guardada" in a or "quitada" in a for a in avisos)


def test_un_envio_sin_el_campo_vence_no_quita_la_fecha_ni_dice_que_la_quito(mt):
    """La sonda del testigo: el campo AUSENTE no es «vacío = quitar»."""
    for datos in ({}, {"prev_vence": "2026-10-14T12:00"}):
        antes = foto(mt)
        r = _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/10/fecha", data=datos, follow_redirects=True)
        assert foto(mt) == antes and tarea(mt, 10)["vence_en"] is not None, datos
        avisos = _avisos(r.text)
        assert avisos and not any("quitada" in a or "guardada" in a for a in avisos), avisos


def test_sin_el_valor_previo_igual_se_mueve_y_vaciar_quita(mt):
    """`prev_vence` solo evita escribir lo que nadie tocó; un envío sin él no se queda sin responder."""
    post("/proyectos/tarea/10/fecha", {"vence": "2026-10-20T09:30"})
    assert "2026-10-20" in str(tarea(mt, 10)["vence_en"])
    post("/proyectos/tarea/10/fecha", {"vence": ""})
    assert tarea(mt, 10)["vence_en"] is None


# ── 6. Lo que no es pendiente, no existe o es de otro proyecto ───────────

def test_hecha_borrada_o_inexistente_no_se_mueve_y_ninguna_pantalla_dice_guardada(mt):
    mt.con.execute("UPDATE tareas SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id = 12")
    mt.con.commit()
    antes = foto(mt)
    for tid in (11, 40, 12, 99999):           # hecha, hecha de proyecto cerrado, borrada, no existe
        r = _cliente(config.CHAT_ID_DUENO).post(
            f"/proyectos/tarea/{tid}/fecha", data={"vence": "2026-10-20T09:30"}, follow_redirects=True)
        assert r.status_code == 200 and foto(mt) == antes, tid
        avisos = _avisos(r.text)
        assert avisos and not any("guardada" in a or "quitada" in a for a in avisos), (tid, avisos)


def test_la_tarea_de_otro_proyecto_vuelve_a_su_propio_proyecto_y_solo_cambia_ella(mt):
    antes = foto(mt)
    r = post("/proyectos/tarea/20/fecha", {"vence": "2026-10-20T09:30"})
    assert r.headers["location"] == "/proyectos?p=1&t=20&hecho=tarea_fecha#tarea-20"
    h, = [x for x in huellas(mt)]
    assert igual_salvo(antes, foto(mt), tareas={20}, log_acciones={h["id"]})


def test_una_direccion_escrita_a_mano_no_pinta_el_aviso(mt):
    for clave in ("tarea_fecha", "tarea_fecha_quitada"):
        assert not any("Fecha de la tarea" in a for a in _avisos(ver(mt, p=2, t=10, hecho=clave))), clave
    for clave in ("tarea_fecha_igual", "tarea_fecha_cambio", "tarea_nueva_responsable"):
        assert _avisos(ver(mt, p=2, t=10, error=clave)) == [], clave


# ── 3 (hermanos). Lo mismo que `/tareas` para la misma entrada ───────────

ENTRADAS = [("2026-10-20T09:30", "2026-10-14T12:00"), ("", "2026-10-14T12:00"), ("2026-10-20", "2026-10-14T12:00"),
            ("basura", "2026-10-14T12:00"), ("2019-12-31T10:00", "2026-10-14T12:00"),
            ("2026-10-20T09:30:45", "2026-10-14T12:00"), ("2026-10-12T09:30", "2026-10-14T12:00"),
            ("2026-10-14T12:00", "2026-10-14T12:00"), ("2026-10-30T09:30", ""),
            # Sin el campo `vence` (con y sin `prev_vence`): una entrada más, y el hermano no toca nada.
            (None, "2026-10-14T12:00"), (None, None)]


def _lo_que_quedo(m, tid: int) -> dict:
    t = tarea(m, tid)
    hs = [{k: json.loads(h[k]) if h[k] else None for k in ("antes", "despues")} | {
        "actor": h["actor"], "accion": h["accion"], "motivo": h["motivo"]} for h in huellas(m)]
    return {"vence_en": t["vence_en"], "pospuesta_veces": t["pospuesta_veces"],
            "estado": t["estado"], "avisos_enviados": t["avisos_enviados"],
            "anticipos_min": t["anticipos_min"], "huellas": hs}


@pytest.mark.parametrize("pedido,previo", ENTRADAS)
def test_misma_entrada_misma_fila_y_misma_huella_que_el_panel_de_tareas(monkeypatch, gente, pedido, previo):
    from test_escrituras_tarea import _mundo
    resultados = []
    for ruta in ("tareas", "proyectos"):
        m = _mundo(monkeypatch, gente)
        m.con.execute("UPDATE tareas SET avisos_enviados = '{0}', anticipos_min = '{0}' WHERE id = 10")
        if previo == "":
            m.con.execute("UPDATE tareas SET vence_en = NULL WHERE id = 10")
        m.con.commit()
        campos = {k: v for k, v in (("vence", pedido), ("prev_vence", previo)) if v is not None}
        if ruta == "tareas":
            _cliente(config.CHAT_ID_DUENO).post(
                "/tareas", data={f"{k}_10": v for k, v in campos.items()}, follow_redirects=False)
        else:
            _cliente(config.CHAT_ID_DUENO).post("/proyectos/tarea/10/fecha", data=campos, follow_redirects=False)
        resultados.append(_lo_que_quedo(m, 10))
    assert resultados[0] == resultados[1], (pedido, previo)
    if pedido is None:
        assert resultados[0]["huellas"] == [] and resultados[0]["vence_en"] is not None, previo
    # y que la comparación no es vacía: las entradas buenas SÍ cambiaron algo
    if pedido in ("2026-10-20T09:30", "", "2026-10-20T09:30:45", "2026-10-12T09:30", "2026-10-30T09:30"):
        assert resultados[0]["huellas"], pedido


# ── 7. El filtro viaja ───────────────────────────────────────────────────

def test_el_filtro_de_la_pagina_viaja_en_el_formulario_y_vuelve(mt):
    html = ver(mt, p=2, t=10, filtro="pendientes")
    f = _form_fecha(html, 10)
    assert f["accion"] == "/proyectos/tarea/10/fecha?filtro=pendientes"
    r = post(f["accion"], {"vence": "2026-10-20T09:30", "prev_vence": "2026-10-14T12:00"})
    assert r.headers["location"] == "/proyectos?p=2&filtro=pendientes&t=10&hecho=tarea_fecha#tarea-10"
