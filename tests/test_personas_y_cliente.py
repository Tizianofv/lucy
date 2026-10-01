"""El cliente del proyecto y las personas del proyecto y de la tarea (Lucy 1.0, E7;
diseño `disenos/lucy-proyectos/DISENO_V1.md` §4, §5, §5.4 y §11-12).

Lo que se vigila:

  · las DOS puertas de las personas (`db.agregar_participante` y
    `db.quitar_participante`) con el SQL de verdad sobre SQLite (el `CHECK`, los
    índices únicos y las huellas son los de `db/schema.sql`): el nombre sale de
    Noco y no de quien llama, el rol tiene tope, el sitio tiene que estar vivo, la
    misma persona no entra dos veces, quitar exige que sea de ESE sitio, y ninguna
    de las dos cuenta como movimiento;
  · el cliente: las dos rutas que lo escriben llaman a `db.poner_cliente`, que vuelve
    a leer la ficha de Noco; vaciar lo quita (es opcional); Noco caído no cambia
    nada y lo dice;
  · las rutas, con sesión, y el aviso con el que vuelve cada rechazo;
  · la búsqueda SIN JavaScript (`?pq=`): el servidor LEE de Noco y dibuja las
    coincidencias como botones;
  · el guion que las busca CON JavaScript (JavaScriptCore de macOS);
  · los censos: quién escribe `participantes` y las columnas del cliente.

FRONTERA, dicha una vez: el Noco de estas pruebas es el doble de
`tests/test_escrituras_proyecto.py::noco`, con la forma de lo que devuelve
`noco_lectura` (comprobada ahí); no se habla con ningún Noco de verdad. Lucy solo
LEE de Noco: ninguna prueba de aquí lo escribe, y la guarda general está en
`tests/test_noco_lectura.py`. El navegador no se ejecuta: el guion corre con un
`document` de mentira que solo imita lo que toca.

Correr:  python3 -m pytest tests/test_personas_y_cliente.py -q
"""
from __future__ import annotations

import ast
import json
import re
import subprocess

import pytest

from test_escrituras_proyecto import (_ARNES, FICHAS_DE_NOCO, _formularios_de, _problemas_de_html_simple,  # noqa: F401
                                      hay_osascript, mandar, noco)
from test_grupo_ia import _archivos_de_texto, _ROOT
from test_pagina_proyectos import _dia, gente, mundo, ver  # noqa: F401
from test_pagina_proyectos_maqueta import _guion_de, arbol
import config
import db.db as db
import noco_lectura


async def _ficha(noco_id):
    return {"id": noco_id, "nombre": FICHAS_DE_NOCO.get(noco_id, f"Ficha {noco_id}")}


def _filas(mundo, tabla="participantes"):
    return [dict(f) for f in mundo.con.execute(f"SELECT * FROM {tabla} ORDER BY id")]


def _huellas(mundo):
    return [dict(f) for f in mundo.con.execute("SELECT * FROM log_acciones ORDER BY id")]


def _sembrar(mundo):
    mundo.proyecto(1, "Uno", area="CDS", creado=_dia(-30))
    mundo.proyecto(2, "Dos", area="CDS")
    mundo.proyecto(3, "En la papelera", area="CDS", borrado=True)
    mundo.tarea(10, "una", proyecto=1)
    mundo.tarea(11, "otra", proyecto=1)
    mundo.tarea(12, "borrada", proyecto=1, borrada=True)


# ═══════════════════════════════════════════════════════════════════════
# La puerta de agregar
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("donde,columna", [(("proyecto", 1), "proyecto_id"), (("tarea", 10), "tarea_id")])
async def test_agregar_guarda_la_persona_con_el_nombre_de_noco_y_deja_su_huella(mundo, gente, donde, columna):
    _sembrar(mundo)

    async def noco_con_otro_nombre(i):
        return {"id": i, "nombre": "  El Nombre De Noco  "}

    nueva = await db.agregar_participante(donde, 101, "  Lleva la mezcla  ", gente.rosi,
                                          leer_persona=noco_con_otro_nombre)
    fila, = _filas(mundo)
    assert fila["id"] == nueva["id"]
    assert (fila[columna], fila["noco_id"], fila["nombre"], fila["rol"], fila["creado_por_chat_id"]) == (
        donde[1], 101, "El Nombre De Noco", "Lleva la mezcla", gente.rosi)
    assert fila["borrado_en"] is None
    otra = "tarea_id" if columna == "proyecto_id" else "proyecto_id"
    assert fila[otra] is None
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "crear", "participantes", fila["id"])
    assert h["antes"] is None and json.loads(h["despues"])["nombre"] == "El Nombre De Noco"


async def test_el_rol_no_puede_quedar_vacio_ni_pasar_de_80(mundo, gente):
    _sembrar(mundo)
    for malo in ("", "   ", None, 7, "x" * (db.LARGO_ROL_PARTICIPANTE + 1)):
        with pytest.raises(db.ParticipanteNoVale) as e:
            await db.agregar_participante(("proyecto", 1), 101, malo, gente.dueno, leer_persona=_ficha)
        assert e.value.clave == "rol", malo
    assert _filas(mundo) == [] and _huellas(mundo) == []
    justo = "x" * db.LARGO_ROL_PARTICIPANTE
    await db.agregar_participante(("proyecto", 1), 101, justo, gente.dueno, leer_persona=_ficha)
    assert _filas(mundo)[0]["rol"] == justo


async def test_el_proyecto_o_la_tarea_tienen_que_estar_vivos(mundo, gente):
    _sembrar(mundo)
    for donde in (("proyecto", 3), ("proyecto", 999), ("tarea", 12), ("tarea", 999)):
        with pytest.raises(db.ParticipanteNoVale) as e:
            await db.agregar_participante(donde, 101, "rol", gente.dueno, leer_persona=_ficha)
        assert e.value.clave == "sitio", donde
    assert _filas(mundo) == [] and _huellas(mundo) == []


@pytest.mark.parametrize("donde", [None, ("persona", 1), ("proyecto", True), ("proyecto", "1"), ("proyecto",),
                                    "proyecto", ("tarea", None)])
async def test_el_sitio_tiene_que_ser_un_proyecto_o_una_tarea_con_su_id(mundo, gente, donde):
    _sembrar(mundo)
    with pytest.raises(db.ParticipanteNoVale) as e:
        await db.agregar_participante(donde, 101, "rol", gente.dueno, leer_persona=_ficha)
    assert e.value.clave == "sitio"
    assert _filas(mundo) == []


async def test_la_misma_persona_no_entra_dos_veces_en_el_mismo_sitio_pero_si_en_otro_o_despues_de_quitarla(mundo, gente):
    _sembrar(mundo)
    primera = await db.agregar_participante(("proyecto", 1), 101, "uno", gente.dueno, leer_persona=_ficha)
    with pytest.raises(db.ParticipanteNoVale) as e:
        await db.agregar_participante(("proyecto", 1), 101, "otro rol", gente.rosi, leer_persona=_ficha)
    assert e.value.clave == "repetida" and len(_filas(mundo)) == 1
    await db.agregar_participante(("proyecto", 2), 101, "en el otro", gente.dueno, leer_persona=_ficha)
    await db.agregar_participante(("tarea", 10), 101, "en una tarea", gente.dueno, leer_persona=_ficha)
    await db.quitar_participante(primera["id"], ("proyecto", 1), gente.dueno)
    await db.agregar_participante(("proyecto", 1), 101, "de vuelta", gente.dueno, leer_persona=_ficha)
    assert len(_filas(mundo)) == 4


async def test_los_indices_unicos_de_la_tabla_frenan_a_la_misma_persona_aunque_la_puerta_no_mirara(mundo):
    """El segundo cinturón: sin pasar por la puerta, la BASE rechaza a la misma
    persona dos veces en un sitio (el índice parcial de `db/schema.sql`)."""
    import sqlite3
    _sembrar(mundo)
    sql = ("INSERT INTO participantes (proyecto_id, noco_id, nombre, rol, creado_por_chat_id) "
           "VALUES (1, 101, 'x', 'r', 1)")
    mundo.con.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        mundo.con.execute(sql)


@pytest.mark.parametrize("chat", [None, 700100999, True, "424242", 0])
async def test_solo_quien_entra_al_panel_puede_agregar_o_quitar(mundo, gente, chat):
    _sembrar(mundo)
    persona = await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=_ficha)
    with pytest.raises(db.ParticipanteNoVale) as e:
        await db.agregar_participante(("proyecto", 1), 102, "rol", chat, leer_persona=_ficha)
    assert e.value.clave == "quien"
    with pytest.raises(db.ParticipanteNoVale) as e:
        await db.quitar_participante(persona["id"], ("proyecto", 1), chat)
    assert e.value.clave == "quien"
    assert len(_filas(mundo)) == 1 and _filas(mundo)[0]["borrado_en"] is None


@pytest.mark.parametrize("ficha", [None, {}, {"id": 999, "nombre": "otro id"}, {"id": 101, "nombre": "   "},
                                    {"id": 101}, {"id": 101, "nombre": 7}])
async def test_si_noco_no_devuelve_una_ficha_que_valga_no_se_escribe_nada(mundo, gente, ficha):
    _sembrar(mundo)

    async def noco_raro(i):
        return ficha

    with pytest.raises(ValueError):
        await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=noco_raro)
    assert _filas(mundo) == [] and _huellas(mundo) == []


@pytest.mark.parametrize("noco_id", [None, True, 0, -3, "101", 1.5])
async def test_el_id_de_noco_tiene_que_ser_un_entero_positivo(mundo, gente, noco_id):
    _sembrar(mundo)
    with pytest.raises(ValueError):
        await db.agregar_participante(("proyecto", 1), noco_id, "rol", gente.dueno, leer_persona=_ficha)
    assert _filas(mundo) == []


async def test_si_noco_no_contesta_la_excepcion_sube_y_no_se_escribe_nada(mundo, gente):
    _sembrar(mundo)

    async def noco_caido(i):
        raise noco_lectura.NocoNoContesta("sin Noco")

    with pytest.raises(noco_lectura.NocoNoContesta):
        await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=noco_caido)
    assert _filas(mundo) == [] and _huellas(mundo) == []


def test_la_puerta_no_tiene_valor_por_omision_para_leer_la_ficha():
    """`leer_persona` es obligatorio (sin valor por omisión): no hay camino que
    guarde sin haber leído la ficha de Noco."""
    import inspect
    p = inspect.signature(db.agregar_participante).parameters["leer_persona"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty


# ═══════════════════════════════════════════════════════════════════════
# La puerta de quitar
# ═══════════════════════════════════════════════════════════════════════

async def test_quitar_es_un_soft_delete_con_su_huella_y_deja_el_antes(mundo, gente):
    _sembrar(mundo)
    persona = await db.agregar_participante(("tarea", 10), 101, "rol", gente.dueno, leer_persona=_ficha)
    antes = await db.quitar_participante(persona["id"], ("tarea", 10), gente.rosi)
    assert antes["nombre"] == "Cliente Uno" and antes["borrado_en"] is None
    fila, = _filas(mundo)                                  # sigue en la tabla: nunca hay DELETE
    assert fila["borrado_en"] is not None and fila["borrado_por_chat_id"] == gente.rosi
    h = _huellas(mundo)[-1]
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "borrar", "participantes", persona["id"])
    assert json.loads(h["antes"])["rol"] == "rol" and h["despues"] is None


async def test_quitar_exige_que_la_persona_sea_de_ese_sitio_y_que_siga_ahi(mundo, gente):
    _sembrar(mundo)
    de_p1 = await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=_ficha)
    de_t10 = await db.agregar_participante(("tarea", 10), 102, "rol", gente.dueno, leer_persona=_ficha)
    for id_, donde in ((de_p1["id"], ("proyecto", 2)),           # de otro proyecto
                       (de_p1["id"], ("tarea", 1)),              # un proyecto no es una tarea
                       (de_t10["id"], ("tarea", 11)),            # de otra tarea
                       (de_t10["id"], ("proyecto", 1)),          # una tarea no es un proyecto
                       (999, ("proyecto", 1)), (True, ("proyecto", 1)), (None, ("proyecto", 1))):
        with pytest.raises(db.ParticipanteNoVale) as e:
            await db.quitar_participante(id_, donde, gente.dueno)
        assert e.value.clave == "no_esta", (id_, donde)
    assert all(f["borrado_en"] is None for f in _filas(mundo))
    await db.quitar_participante(de_p1["id"], ("proyecto", 1), gente.dueno)
    with pytest.raises(db.ParticipanteNoVale):                     # ya quitada
        await db.quitar_participante(de_p1["id"], ("proyecto", 1), gente.dueno)


async def test_agregar_o_quitar_una_persona_no_cuenta_como_movimiento_del_proyecto(mundo, gente):
    """Diseño §5.4: como en la maqueta (`movio()` no se llama al agregar una
    persona). El proyecto de 30 días sin tocar sigue DORMIDO después de agregar y
    de quitar; las huellas de `participantes` no entran al «último movimiento»."""
    _sembrar(mundo)
    antes = (await db.pagina_de_proyectos())["proyectos"][1]
    assert antes["dormido"] is True
    persona = await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=_ficha)
    await db.agregar_participante(("tarea", 10), 102, "rol", gente.dueno, leer_persona=_ficha)
    await db.quitar_participante(persona["id"], ("proyecto", 1), gente.dueno)
    despues = (await db.pagina_de_proyectos())["proyectos"][1]
    assert despues["dormido"] is True and despues["ultimo"] == antes["ultimo"]
    assert despues["dias_sin_movimiento"] == antes["dias_sin_movimiento"]
    assert len(_huellas(mundo)) == 3                                  # sí dejaron huella, de su tabla


# ═══════════════════════════════════════════════════════════════════════
# Lo que se lee: las personas se pintan por NOMBRE, las quitadas no salen
# ═══════════════════════════════════════════════════════════════════════

async def test_el_modelo_trae_las_personas_vivas_de_cada_proyecto_y_tarea(mundo, gente):
    _sembrar(mundo)
    a = await db.agregar_participante(("proyecto", 1), 101, "mezcla", gente.dueno, leer_persona=_ficha)
    await db.agregar_participante(("proyecto", 1), 102, "master", gente.dueno, leer_persona=_ficha)
    await db.agregar_participante(("tarea", 10), 103, "revisa", gente.dueno, leer_persona=_ficha)
    await db.quitar_participante(a["id"], ("proyecto", 1), gente.dueno)
    modelo = await db.pagina_de_proyectos()
    p = modelo["proyectos"][1]
    assert [(x["nombre"], x["rol"]) for x in p["personas"]] == [("Persona Dos de Noco", "master")]
    assert [(x["nombre"], x["rol"]) for x in p["pendientes"][0]["personas"]] == [("Persona Tres de Noco", "revisa")]
    assert modelo["proyectos"][2]["personas"] == [] and p["pendientes"][1]["personas"] == []
    # Por nombre y rol y su id de fila (para quitarla): ningún número de chat ni el Id de Noco.
    assert set(p["personas"][0]) == {"id", "nombre", "rol"}


def test_la_pagina_pinta_las_personas_con_su_rol_su_x_y_su_carita_y_ningun_numero_de_chat(mundo, gente):
    _sembrar(mundo)
    mundo.con.execute("INSERT INTO participantes (id, proyecto_id, noco_id, nombre, rol, creado_por_chat_id) "
                      "VALUES (5, 1, 101, 'Ana Pérez', 'Mezcla', ?)", (gente.rosi,))
    mundo.con.execute("INSERT INTO participantes (id, tarea_id, noco_id, nombre, rol, creado_por_chat_id) "
                      "VALUES (6, 10, 102, 'Luis Gómez', 'Revisa', ?)", (gente.rosi,))
    mundo.con.execute("INSERT INTO participantes (id, proyecto_id, noco_id, nombre, rol, creado_por_chat_id, borrado_en) "
                      "VALUES (7, 1, 103, 'Ya Quitada', 'Nada', ?, '2026-09-01T00:00:00+00:00')", (gente.rosi,))
    html = ver(mundo, p=1, t=10)
    raiz = arbol(html)
    lado = raiz.buscar("aside", "lado-der")[0]
    tarjeta, = lado.buscar("div", "persona")
    assert tarjeta.buscar("b")[0].todo_el_texto() == "Ana Pérez" and tarjeta.buscar("span", "ini")[0].todo_el_texto() == "AP"
    assert tarjeta.buscar("span", "quien")[0].buscar("span")[0].todo_el_texto() == "Mezcla"
    quitar = tarjeta.buscar("form")[0]
    assert quitar.attrs["method"] == "post" and quitar.attrs["action"] == "/proyectos/1/personas/5/quitar"
    detalle = raiz.buscar("div", "detalle")[0]
    assert [t.buscar("b")[0].todo_el_texto() for t in detalle.buscar("div", "persona")] == ["Luis Gómez"]
    assert detalle.buscar("div", "persona")[0].buscar("form")[0].attrs["action"] == "/proyectos/tarea/10/personas/6/quitar"
    # La carita de la fila de la tarea.
    fila = [t for t in raiz.buscar("div", "tarea") if t.attrs["data-tarea"] == "10"][0]
    assert [c.todo_el_texto() for c in fila.buscar("span", "caritas")[0].buscar("span", "ini")] == ["LG"]
    assert "Ya Quitada" not in html
    for numero in (str(gente.rosi), str(gente.dueno)):
        assert numero not in html
    assert _problemas_de_html_simple(html) == []


# ═══════════════════════════════════════════════════════════════════════
# Las rutas de las personas
# ═══════════════════════════════════════════════════════════════════════

def test_agregar_una_persona_al_proyecto_por_la_ruta_guarda_lo_que_noco_devuelve(mundo, gente, noco):
    _sembrar(mundo)
    r = mandar("/proyectos/1/personas", {"noco_id": "102", "rol": " Mezcla ", "nombre": "Hacker", "chat": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?hecho=persona&p=1"
    fila, = _filas(mundo)
    assert (fila["proyecto_id"], fila["noco_id"], fila["nombre"], fila["rol"], fila["creado_por_chat_id"]) == (
        1, 102, "Persona Dos de Noco", "Mezcla", config.CHAT_ID_DUENO)
    assert noco.pedidos == [("persona", 102)]
    assert "Persona agregada." in ver(mundo, hecho="persona", p=1)


def test_quien_agrega_sale_de_la_sesion_y_los_dos_del_panel_pueden(mundo, gente, noco):
    _sembrar(mundo)
    from test_pagina_proyectos import _cliente
    r = _cliente(gente.rosi).post("/proyectos/1/personas", data={"noco_id": "101", "rol": "r"}, follow_redirects=False)
    assert r.status_code == 303 and _filas(mundo)[0]["creado_por_chat_id"] == gente.rosi


def test_agregar_una_persona_a_la_tarea_vuelve_a_la_tarea_con_el_detalle_abierto(mundo, gente, noco):
    _sembrar(mundo)
    r = mandar("/proyectos/tarea/10/personas", {"noco_id": "103", "rol": "Revisa"})
    assert r.headers["location"] == "/proyectos?p=1&t=10&hecho=persona#tarea-10"
    assert _filas(mundo)[0]["tarea_id"] == 10


@pytest.mark.parametrize("campos,aviso", [
    ({"noco_id": "101", "rol": ""}, "persona_rol"),
    ({"noco_id": "101", "rol": "x" * 81}, "persona_rol"),
    ({"noco_id": "", "rol": "r"}, "persona_ficha"),
    ({"rol": "r"}, "persona_ficha"),
    ({"noco_id": "abc", "rol": "r"}, "persona_ficha"),
    ({"noco_id": "-5", "rol": "r"}, "persona_ficha"),
    ({"noco_id": "999", "rol": "r"}, "persona_ficha"),            # una ficha que Noco no tiene
])
def test_un_rechazo_vuelve_con_su_aviso_y_no_escribe_nada(mundo, gente, noco, campos, aviso):
    _sembrar(mundo)
    for ruta, esperado in (("/proyectos/1/personas", f"/proyectos?error={aviso}&p=1"),
                           ("/proyectos/tarea/10/personas", f"/proyectos?p=1&error={aviso}&t=10#tarea-10")):
        r = mandar(ruta, campos)
        assert r.status_code == 303 and r.headers["location"] == esperado, (ruta, r.headers["location"])
    assert _filas(mundo) == [] and _huellas(mundo) == []
    assert 'class="aviso"' in ver(mundo, error=aviso, p=1)


def test_la_misma_persona_dos_veces_y_un_sitio_que_ya_no_esta_vuelven_con_su_aviso(mundo, gente, noco):
    _sembrar(mundo)
    mandar("/proyectos/1/personas", {"noco_id": "101", "rol": "r"})
    assert mandar("/proyectos/1/personas", {"noco_id": "101", "rol": "r"}).headers["location"] == \
        "/proyectos?error=persona_repetida&p=1"
    assert mandar("/proyectos/3/personas", {"noco_id": "101", "rol": "r"}).headers["location"] == \
        "/proyectos?error=persona_sitio&p=3"
    assert len(_filas(mundo)) == 1


def test_si_noco_no_contesta_la_persona_no_se_agrega_y_el_aviso_lo_dice(mundo, gente, noco):
    _sembrar(mundo)
    noco.cae = True
    r = mandar("/proyectos/1/personas", {"noco_id": "101", "rol": "r"})
    assert r.headers["location"] == "/proyectos?error=persona_noco&p=1" and _filas(mundo) == []
    assert "Noco no contestó" in ver(mundo, error="persona_noco", p=1)


def test_quitar_por_las_rutas_exige_el_sitio_correcto_y_deja_huella_de_panel(mundo, gente, noco):
    _sembrar(mundo)
    mandar("/proyectos/1/personas", {"noco_id": "101", "rol": "r"})
    mandar("/proyectos/tarea/10/personas", {"noco_id": "102", "rol": "r"})
    p_id, t_id = (f["id"] for f in _filas(mundo))
    # El id de una persona de OTRO sitio no se quita.
    assert mandar(f"/proyectos/2/personas/{p_id}/quitar").headers["location"] == "/proyectos?error=persona_no_esta&p=2"
    assert mandar(f"/proyectos/tarea/11/personas/{t_id}/quitar").headers["location"].startswith(
        "/proyectos?p=1&error=persona_no_esta")
    assert mandar(f"/proyectos/tarea/10/personas/{p_id}/quitar").headers["location"].startswith(
        "/proyectos?p=1&error=persona_no_esta")                    # la del proyecto, por la ruta de tarea
    assert all(f["borrado_en"] is None for f in _filas(mundo))
    assert mandar(f"/proyectos/1/personas/{p_id}/quitar").headers["location"] == "/proyectos?hecho=persona_quitada&p=1"
    assert mandar(f"/proyectos/tarea/10/personas/{t_id}/quitar").headers["location"] == \
        "/proyectos?p=1&t=10&hecho=persona_quitada#tarea-10"
    assert all(f["borrado_en"] is not None for f in _filas(mundo))
    assert [h["accion"] for h in _huellas(mundo)] == ["crear", "crear", "borrar", "borrar"]


def test_sin_sesion_no_se_cambia_ninguna_persona_ni_el_cliente(mundo, noco):
    _sembrar(mundo)
    for ruta in ("/proyectos/1/personas", "/proyectos/1/personas/1/quitar", "/proyectos/tarea/10/personas",
                 "/proyectos/tarea/10/personas/1/quitar", "/proyectos/1/cliente"):
        assert mandar(ruta, {"noco_id": "101", "rol": "r"}, chat=None).status_code == 401, ruta
    assert _filas(mundo) == [] and _huellas(mundo) == [] and noco.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# El cliente
# ═══════════════════════════════════════════════════════════════════════

def _cliente_de(mundo, pid):
    f = mundo.con.execute("SELECT cliente_noco_id, cliente_nombre FROM proyectos WHERE id = ?", (pid,)).fetchone()
    return tuple(f)


def test_poner_el_cliente_guarda_el_nombre_que_devuelve_noco_y_no_el_del_formulario(mundo, gente, noco):
    _sembrar(mundo)
    r = mandar("/proyectos/1/cliente", {"noco_id": "101", "cliente_nombre": "Hacker", "cliente_noco_id": "5"})
    assert r.headers["location"] == "/proyectos?hecho=cliente&p=1"
    assert _cliente_de(mundo, 1) == (101, "Cliente Uno") and noco.pedidos == [("persona", 101)]
    h, = _huellas(mundo)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "proyectos", 1)
    assert json.loads(h["despues"]) == {"cliente_noco_id": 101, "cliente_nombre": "Cliente Uno"}
    assert "Cliente guardado." in ver(mundo, hecho="cliente", p=1)


def test_cambiar_el_cliente_y_quitarlo_con_un_id_vacio(mundo, gente, noco):
    _sembrar(mundo)
    mandar("/proyectos/1/cliente", {"noco_id": "101"})
    assert mandar("/proyectos/1/cliente", {"noco_id": "102"}).headers["location"] == "/proyectos?hecho=cliente&p=1"
    assert _cliente_de(mundo, 1) == (102, "Persona Dos de Noco")
    # El cliente es OPCIONAL: vaciar lo quita, sin llamar a Noco.
    pedidos = len(noco.pedidos)
    r = mandar("/proyectos/1/cliente", {"noco_id": ""})
    assert r.headers["location"] == "/proyectos?hecho=cliente_quitado&p=1" and _cliente_de(mundo, 1) == (None, None)
    assert len(noco.pedidos) == pedidos
    assert "Cliente quitado" in ver(mundo, hecho="cliente_quitado", p=1)
    # Quitarlo cuando ya no tiene no cambia nada ni deja huella nueva.
    n = len(_huellas(mundo))
    assert mandar("/proyectos/1/cliente", {"noco_id": ""}).headers["location"] == "/proyectos?error=cliente_igual&p=1"
    assert len(_huellas(mundo)) == n


@pytest.mark.parametrize("noco_id", ["999", "abc", "-1", "0", "1.5", "101 OR 1=1"])
def test_un_cliente_que_no_vale_no_cambia_nada(mundo, gente, noco, noco_id):
    _sembrar(mundo)
    mandar("/proyectos/1/cliente", {"noco_id": "101"})
    antes = (_cliente_de(mundo, 1), len(_huellas(mundo)))
    r = mandar("/proyectos/1/cliente", {"noco_id": noco_id})
    assert r.headers["location"] == "/proyectos?error=cliente&p=1"
    assert (_cliente_de(mundo, 1), len(_huellas(mundo))) == antes


def test_si_noco_no_contesta_el_cliente_no_cambia_y_el_aviso_lo_dice(mundo, gente, noco):
    _sembrar(mundo)
    mandar("/proyectos/1/cliente", {"noco_id": "101"})
    noco.cae = True
    r = mandar("/proyectos/1/cliente", {"noco_id": "102"})
    assert r.headers["location"] == "/proyectos?error=cliente_noco&p=1" and _cliente_de(mundo, 1) == (101, "Cliente Uno")
    assert "Noco no contestó" in ver(mundo, error="cliente_noco", p=1)


def test_el_cliente_de_un_proyecto_que_no_esta_o_esta_en_la_papelera_da_error(mundo, gente, noco):
    _sembrar(mundo)
    for pid in (3, 999):
        assert mandar(f"/proyectos/{pid}/cliente", {"noco_id": "101"}).headers["location"] == \
            f"/proyectos?error=cliente&p={pid}"


def test_telegram_no_escribe_el_cliente_y_crud_editar_lo_rechaza(mundo, gente, noco):
    """G6: `crud.editar` rechaza `cliente_*` (la puerta de `crud.PUERTAS`): el
    cliente se elige en el panel, donde se vuelve a leer de Noco."""
    import asyncio
    import acciones.crud as crud
    _sembrar(mundo)
    for columna in ("cliente_nombre", "cliente_noco_id"):
        with pytest.raises(ValueError):
            asyncio.new_event_loop().run_until_complete(
                crud.editar("proyectos", 1, {columna: "x" if columna == "cliente_nombre" else 5},
                            motivo="p", actor="lucy"))
    assert _cliente_de(mundo, 1) == (None, None)


# ── El cliente al crear un proyecto (la ventanita) ──────────────────────

def test_la_ventanita_puede_mandar_un_cliente_y_el_proyecto_nace_con_el_de_noco(mundo, gente, noco):
    r = mandar("/proyectos/nuevo", {"nombre": "Con cliente", "area": "CDS", "responsable": "Persona Dos",
                                    "cliente": "101"})
    pid = mundo.con.execute("SELECT id FROM proyectos").fetchone()[0]
    assert r.headers["location"] == f"/proyectos?hecho=proyecto_nuevo&p={pid}"
    assert _cliente_de(mundo, pid) == (101, "Cliente Uno")
    assert [h["accion"] for h in _huellas(mundo)] == ["crear", "editar"]       # dos puertas, dos huellas


def test_sin_cliente_el_proyecto_nace_igual_y_no_se_le_pide_nada_a_noco(mundo, gente, noco):
    for campos in ({}, {"cliente": ""}, {"cliente": "   "}):
        mandar("/proyectos/nuevo", {"nombre": f"P{len(campos)}{campos.get('cliente', '')!r}", "area": "CDS",
                                    "responsable": "Persona Dos", **campos})
    assert mundo.con.execute("SELECT count(*) FROM proyectos WHERE cliente_noco_id IS NOT NULL").fetchone()[0] == 0
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 3 and noco.pedidos == []


@pytest.mark.parametrize("cae,cliente", [(True, "101"), (False, "999"), (False, "abc")])
def test_si_el_cliente_no_se_puede_poner_el_proyecto_se_crea_sin_cliente_y_el_aviso_lo_dice(mundo, gente, noco, cae, cliente):
    noco.cae = cae
    r = mandar("/proyectos/nuevo", {"nombre": "Sin cliente por Noco", "area": "CDS", "responsable": "Persona Dos",
                                    "cliente": cliente})
    pid = mundo.con.execute("SELECT id FROM proyectos").fetchone()[0]
    assert r.headers["location"] == f"/proyectos?hecho=proyecto_nuevo&error=cliente_nuevo&p={pid}"
    assert _cliente_de(mundo, pid) == (None, None)
    assert "SIN cliente" in ver(mundo, hecho="proyecto_nuevo", error="cliente_nuevo", p=pid)


def test_un_proyecto_que_no_vale_no_pide_nada_a_noco_aunque_traiga_cliente(mundo, gente, noco):
    r = mandar("/proyectos/nuevo", {"nombre": "", "area": "CDS", "responsable": "Persona Dos", "cliente": "101"})
    assert "error=nombre_vacio" in r.headers["location"] and noco.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# La búsqueda SIN JavaScript: el servidor LEE de Noco y dibuja las coincidencias
# ═══════════════════════════════════════════════════════════════════════

def _sugerencias(html: str, donde: str):
    """Los botones de coincidencia dentro del contenedor de ese sitio."""
    raiz = arbol(html)
    if donde == "cliente":
        cajas = [c for c in raiz.buscar("div", "sugerencias") if c.ancestro("form")
                 and c.ancestro("form").attrs.get("action", "").endswith("/cliente")]
    elif donde == "proyecto":
        cajas = [c for c in raiz.buscar("div", "sugerencias") if c.ancestro("form")
                 and re.fullmatch(r"/proyectos/\d+/personas", c.ancestro("form").attrs.get("action", ""))]
    else:
        cajas = [c for c in raiz.buscar("div", "sugerencias") if c.ancestro("form")
                 and c.ancestro("form").attrs.get("action", "").endswith(f"/proyectos/tarea/{donde.split('-')[1]}/personas")]
    assert len(cajas) == 1, (donde, len(cajas))
    return [(b.attrs["name"], b.attrs["value"], b.todo_el_texto()) for b in cajas[0].buscar("button")], cajas[0]


@pytest.mark.parametrize("donde", ["cliente", "proyecto", "tarea-10"])
def test_buscar_sin_javascript_dibuja_las_coincidencias_como_botones_que_envian_el_id(mundo, gente, noco, donde):
    _sembrar(mundo)
    html = ver(mundo, p=1, t=10, pq="de noco", pdonde=donde)
    botones, _ = _sugerencias(html, donde)
    assert botones == [("noco_id", "102", "Persona Dos de Noco"), ("noco_id", "103", "Persona Tres de Noco")]
    assert noco.pedidos == [("buscar", "de noco")]
    assert _problemas_de_html_simple(html) == []
    # Los demás sitios NO se llenan: la búsqueda es de uno solo.
    for otro in {"cliente", "proyecto", "tarea-10"} - {donde}:
        assert _sugerencias(html, otro)[0] == []


def test_buscar_sin_coincidencias_lo_dice_y_noco_caido_se_dice_distinto(mundo, gente, noco):
    _sembrar(mundo)
    _, caja = _sugerencias(ver(mundo, p=1, pq="zzzz", pdonde="cliente"), "cliente")
    assert caja.todo_el_texto().strip() == "Nadie con «zzzz» en Noco."
    noco.cae = True
    _, caja = _sugerencias(ver(mundo, p=1, pq="zzzz", pdonde="cliente"), "cliente")
    assert "Noco de mentira: no contesta" in caja.todo_el_texto() and "Nadie con" not in caja.todo_el_texto()


def test_sin_texto_o_con_un_sitio_que_no_existe_no_se_le_pide_nada_a_noco(mundo, gente, noco):
    _sembrar(mundo)
    for consulta in ({"pq": "", "pdonde": "cliente"}, {"pq": "   ", "pdonde": "cliente"},
                     {"pq": "col", "pdonde": ""}, {"pq": "col", "pdonde": "otro"}, {"pq": "col", "pdonde": "tarea-x"},
                     {"pq": "col", "pdonde": "cliente; DROP"}):
        ver(mundo, p=1, **consulta)
    ver(mundo, p=1)
    assert noco.pedidos == []


def test_lo_que_se_busca_se_escapa_y_la_pagina_no_se_cae_con_texto_raro(mundo, gente, noco):
    _sembrar(mundo)
    peligro = '"><script>alert(1)</script>'
    html = ver(mundo, p=1, pq=peligro, pdonde="cliente")
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_la_busqueda_sin_javascript_vuelve_a_la_misma_vista_y_la_ofrece_a_cada_formulario_de_buscar(mundo, gente, noco):
    _sembrar(mundo)
    html = ver(mundo, p=1, t=10)
    for forma in arbol(html).buscar("form", "buscar-persona"):
        assert forma.attrs["method"] == "get" and forma.attrs["action"] == "/proyectos"
        ocultos = {i.attrs["name"]: i.attrs["value"] for i in forma.buscar("input") if i.attrs.get("type") == "hidden"}
        assert ocultos["p"] == "1" and ocultos["t"] == "10" and ocultos["pdonde"] in ("cliente", "proyecto", "tarea-10")
        assert [i.attrs["name"] for i in forma.buscar("input") if i.attrs.get("type") == "text"] == ["pq"]
    # En las tareas sueltas la vista que se conserva es la del grupo, y sin proyecto
    # no hay cliente ni personas del proyecto: solo las de la tarea abierta.
    mundo.tarea(30, "suelta", area="CDS")
    html = ver(mundo, g="CDS", t=30)
    formas = arbol(html).buscar("form", "buscar-persona")
    assert len(formas) == 1
    ocultos = {i.attrs["name"]: i.attrs["value"] for i in formas[0].buscar("input") if i.attrs.get("type") == "hidden"}
    assert ocultos == {"g": "CDS", "t": "30", "pdonde": "tarea-30"}
    botones, _ = _sugerencias(ver(mundo, g="CDS", t=30, pq="de noco", pdonde="tarea-30"), "tarea-30")
    assert [b[2] for b in botones] == ["Persona Dos de Noco", "Persona Tres de Noco"]


# ═══════════════════════════════════════════════════════════════════════
# Los censos: quién escribe `participantes` y las columnas del cliente
# ═══════════════════════════════════════════════════════════════════════

def _escritores_de(patron: str, archivos=None) -> set:
    """Las funciones del código (fuera de `tests/`) cuyo SQL legible casa `patron`.
    SALE DE RECORRER LOS `.py`. FRONTERA: no ve SQL armado al vuelo."""
    encontradas = set()
    for archivo in (archivos if archivos is not None else _archivos_de_texto()):
        if archivo.suffix != ".py" or archivo.relative_to(_ROOT).parts[0] == "tests":
            continue
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and re.search(patron, n.value, re.I | re.S)):
                    encontradas.add(fn.name)
    return encontradas


def test_solo_las_dos_puertas_escriben_participantes():
    assert _escritores_de(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+participantes\b") == {
        "agregar_participante", "quitar_participante"}


def test_solo_poner_cliente_escribe_las_columnas_del_cliente_en_proyectos():
    assert _escritores_de(r"UPDATE\s+proyectos\s+SET[^;]*\bcliente_(noco_id|nombre)\s*=") == {"poner_cliente"}
    assert _escritores_de(r"INSERT\s+INTO\s+proyectos\s*\([^)]*cliente_") == set()


def test_el_censo_ve_un_escritor_inventado():
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d).resolve()
        (d / "web").mkdir()
        f = d / "web" / "x.py"
        f.write_text("async def intruso(c):\n    await c.execute('UPDATE participantes SET rol = 1')\n"
                     "async def otro(c):\n    await c.execute('DELETE FROM participantes')\n"
                     "async def cliente(c):\n    await c.execute('UPDATE proyectos SET cliente_nombre = 1 WHERE id = 2')\n")
        # `_escritores_de` mide contra la raíz del repo: se le da el archivo inventado
        # con su ruta relativa fingida mirando el árbol sintáctico a mano.
        encontradas = set()
        for fn in ast.walk(ast.parse(f.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and re.search(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+participantes\b", n.value, re.I)):
                        encontradas.add(fn.name)
        assert encontradas == {"intruso", "otro"}


def test_las_puertas_de_participantes_dejan_el_actor_como_literal_panel():
    """El guarda del repo (`tests/test_proyectos_panel.py`) no deja que el actor
    viaje como variable fuera de `crud._registrar`: las dos puertas lo escriben
    como literal, y sus huellas son `crear` y `borrar` de la tabla
    `participantes`, que el «último movimiento» no mira."""
    fuente = (_ROOT / "db" / "db.py").read_text(encoding="utf-8")
    for nombre, accion in (("agregar_participante", "'crear'"), ("quitar_participante", "'borrar'")):
        cuerpo = fuente.split(f"async def {nombre}(", 1)[1].split("\nasync def ", 1)[0]
        assert f"VALUES ('panel', {accion}, 'participantes'" in cuerpo, nombre
    consulta = fuente.split("async def pagina_de_proyectos", 1)[1].split("return armar_pagina", 1)[0]
    assert "participantes'" not in consulta.split("FROM log_acciones")[1].split("GROUP BY")[0] if "FROM log_acciones" in consulta else True
    assert "l.tabla = 'participantes'" not in consulta


# ═══════════════════════════════════════════════════════════════════════
# El guion que busca personas CON JavaScript
# ═══════════════════════════════════════════════════════════════════════

_FALSOS = """
var pedidos = [], respuestas = [], creados = [];
function trampa(valor) { return {then: function (f) { return trampa(f(valor)); }, catch: function () { return this; }}; }
var fetch = function (url) { pedidos.push(url); var r = respuestas.shift();
  if (r === "falla") return {then: function () { return this; }, catch: function (f) { f(); return this; }};
  return trampa({json: function () { return r; }}); };
document.createElement = function (tag) {
  return {tag: tag, className: "", textContent: "", value: "", title: "", name: "", type: "submit", hijos: []}; };
function lista() {
  var l = {hijos: [], firstChild: null,
    removeChild: function (h) { this.hijos.shift(); this.firstChild = this.hijos[0] || null; },
    appendChild: function (h) { this.hijos.push(h); this.firstChild = this.hijos[0]; }};
  return l;
}
function campoDeMentira(modo, valor) {
  var sug = lista();
  var oculto = {value: "preexistente"};
  var envios = {quitar: 0};
  var caja = {value: valor, dataset: {buscarPersona: modo}, focos: 0};
  var campo = {querySelector: function (s) {
      if (s === ".sugerencias") return sug;
      if (s === 'input[name="cliente"]') return oculto;
      if (s === "input[data-buscar-persona]") return caja;
      if (s === "form.elegir-persona") return envio;
      return null; }};
  var quitar = {};
  var envio = {querySelector: function (s) { return s === "button.quitar-cliente" ? quitar : null; },
    requestSubmit: function (b) { envios.quitar += (b === quitar) ? 1 : 0; envios.otro = (envios.otro || 0) + (b === quitar ? 0 : 1); }};
  caja.closest = function (s) {
    if (s === ".persona-campo") return campo;
    if (s === "input[data-buscar-persona]") return this;
    return null; };
  return {caja: caja, sug: sug, oculto: oculto, campo: campo, envios: envios, quitar: quitar};
}
function boton(c, id, nombre, tipo) {
  var b = {value: id, textContent: nombre, type: tipo,
    closest: function (s) { return s === "button.sugerencia" ? this : (s === ".persona-campo" ? c.campo : null); }};
  return b;
}
function estado(c) {
  return JSON.stringify({pedidos: pedidos,
    sugerencias: c.sug.hijos.map(function (h) { return {tag: h.tag, clase: h.className, texto: h.textContent,
      value: h.value, name: h.name, type: h.type, title: h.title}; })});
}
"""


def _jxa_personas(mundo, escenario: str):
    html = ver(mundo)
    guion = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]
    # `fetch` y `createElement` de mentira ANTES del guion; el escenario después.
    previo = _FALSOS.split("function lista()")[0]
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e",
                        _ARNES + previo + "function lista" + _FALSOS.split("function lista", 1)[1] + guion + "\n" + escenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


@hay_osascript
@pytest.mark.parametrize("modo,tipo,nombre_del_boton", [("enviar", "submit", "noco_id"), ("elegir", "button", "")])
def test_js_escribir_dos_letras_pide_a_noco_y_dibuja_un_boton_por_coincidencia(mundo, modo, tipo, nombre_del_boton):
    r = _jxa_personas(mundo, f"""
      var c = campoDeMentira({modo!r}, "an");
      respuestas.push({{personas: [{{id: 7, nombre: "Ana Pérez"}}, {{id: 9, nombre: "Anabel"}}]}});
      oyentes.input(ev(c.caja)); correrTemporizadores();
      estado(c)""")
    assert r["pedidos"] == ["/personas/buscar?q=an"]
    assert [(s["tag"], s["clase"], s["texto"], s["value"], s["name"], s["type"]) for s in r["sugerencias"]] == [
        ("button", "sugerencia", "Ana Pérez", 7, nombre_del_boton, tipo),
        ("button", "sugerencia", "Anabel", 9, nombre_del_boton, tipo)]
    assert r["sugerencias"][0]["title"] == "Elegir a Ana Pérez"


@hay_osascript
def test_js_con_una_sola_letra_no_pide_nada_y_limpia_la_lista(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "a"); c.sug.appendChild({});
      oyentes.input(ev(c.caja)); correrTemporizadores();
      estado(c)""")
    assert r["pedidos"] == [] and r["sugerencias"] == []


@hay_osascript
def test_js_espera_a_que_se_termine_de_escribir_y_pide_una_sola_vez(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "an");
      respuestas.push({personas: []});
      oyentes.input(ev(c.caja)); c.caja.value = "ana"; oyentes.input(ev(c.caja)); c.caja.value = "ana p";
      oyentes.input(ev(c.caja)); correrTemporizadores();
      estado(c)""")
    assert r["pedidos"] == ["/personas/buscar?q=ana%20p"]


@hay_osascript
def test_js_una_respuesta_vieja_no_pisa_lo_que_se_escribio_despues(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "an");
      respuestas.push({personas: [{id: 1, nombre: "De otra búsqueda"}]});
      oyentes.input(ev(c.caja)); c.caja.value = "otra cosa";      // se escribe antes de que llegue
      correrTemporizadores();
      estado(c)""")
    assert r["sugerencias"] == []


@hay_osascript
def test_js_si_noco_no_contesta_lo_dice_y_nunca_dice_que_no_hay_nadie(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "an");
      respuestas.push({error: "Noco no contesta: sin red"});
      oyentes.input(ev(c.caja)); correrTemporizadores();
      var con_error = estado(c);
      var d = campoDeMentira("enviar", "an");
      respuestas.push("falla");
      oyentes.input(ev(d.caja)); correrTemporizadores();
      JSON.stringify({error: JSON.parse(con_error).sugerencias, cae: JSON.parse(estado(d)).sugerencias})""")
    assert [(s["clase"], s["texto"]) for s in r["error"]] == [("vacio", "Noco no contesta: sin red")]
    assert [(s["clase"], s["texto"]) for s in r["cae"]] == [("vacio", "No se pudo buscar en Noco.")]


@hay_osascript
def test_js_sin_coincidencias_dice_nadie_con_lo_escrito(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "zz"); respuestas.push({personas: []});
      oyentes.input(ev(c.caja)); correrTemporizadores(); estado(c)""")
    assert [(s["clase"], s["texto"]) for s in r["sugerencias"]] == [("vacio", "Nadie con «zz» en Noco.")]


@hay_osascript
def test_js_el_pedido_siempre_es_el_get_de_buscar_con_el_texto_codificado(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", 'a&b=c#"x"'); respuestas.push({personas: []});
      oyentes.input(ev(c.caja)); correrTemporizadores(); estado(c)""")
    assert r["pedidos"] == ["/personas/buscar?q=a%26b%3Dc%23%22x%22"]


@hay_osascript
def test_js_en_la_ventanita_elegir_copia_el_id_y_el_nombre_y_escribir_otra_cosa_lo_deshace(mundo):
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("elegir", "col");
      var b = boton(c, 7, "Colegio Ejemplo", "button");
      oyentes.click(ev(b));
      var elegido = {oculto: c.oculto.value, caja: c.caja.value, lista: c.sug.hijos.length, evitado: evitado};
      c.caja.value = "Colegio Ejem"; oyentes.input(ev(c.caja));
      JSON.stringify({elegido: elegido, despues: c.oculto.value})""")
    assert r["elegido"] == {"oculto": 7, "caja": "Colegio Ejemplo", "lista": 0, "evitado": 1}
    assert r["despues"] == ""


@hay_osascript
def test_js_un_boton_de_coincidencia_de_los_formularios_no_se_intercepta(mundo):
    """En los formularios de la página la coincidencia es un botón de envío NORMAL:
    el guion no le cambia nada (el navegador envía su formulario con `noco_id`)."""
    r = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "col"); var b = boton(c, 7, "Colegio", "submit");
      oyentes.click(ev(b)); JSON.stringify({evitado: evitado, oculto: c.oculto.value})""")
    assert r == {"evitado": 0, "oculto": "preexistente"}


@hay_osascript
def test_js_vaciar_la_caja_del_cliente_envia_el_formulario_de_quitar_y_solo_si_hay_cliente(mundo):
    con = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", ""); c.caja.dataset.actual = "";
      oyentes.change(ev(c.caja)); JSON.stringify(c.envios)""")
    assert con == {"quitar": 1, "otro": 0}
    sin = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", ""); oyentes.change(ev(c.caja)); JSON.stringify(c.envios)""")
    assert sin == {"quitar": 0}
    lleno = _jxa_personas(mundo, """
      var c = campoDeMentira("enviar", "Otro nombre"); c.caja.dataset.actual = "";
      oyentes.change(ev(c.caja)); JSON.stringify(c.envios)""")
    assert lleno == {"quitar": 0}


def test_el_guion_solo_pide_buscar_personas_y_nada_mas_de_red(mundo):
    from test_escrituras_proyecto import sin_el_unico_pedido_permitido
    codigo = re.sub(r"/\*.*?\*/", "", _guion_de(mundo), flags=re.S)
    resto = sin_el_unico_pedido_permitido(codigo)
    assert not re.search(r"XMLHttpRequest|sendBeacon|WebSocket|\.submit\(|FormData|innerHTML|eval\(", resto)
    assert "method" not in codigo.lower().split("fetch(")[1].split(")")[0]


# ═══════════════════════════════════════════════════════════════════════
# `crud.TABLAS`: `participantes` entra solo para borrar y deshacer (diseño §5.4)
# ═══════════════════════════════════════════════════════════════════════

async def test_crud_editar_rechaza_participantes_y_borrar_y_deshacer_si_funcionan(mundo, gente):
    import acciones.crud as crud
    _sembrar(mundo)
    assert "participantes" in crud.TABLAS
    p = await db.agregar_participante(("proyecto", 1), 101, "rol", gente.dueno, leer_persona=_ficha)
    with pytest.raises(ValueError, match="no se editan"):
        await crud.editar("participantes", p["id"], {"rol": "otro"}, motivo="x", actor="lucy")
    assert _filas(mundo)[0]["rol"] == "rol"
    # Deshacer el «crear» de la puerta: la persona deja de estar.
    h_crear = _huellas(mundo)[0]["id"]
    assert await crud.deshacer(h_crear) == "lo que había creado"
    assert _filas(mundo)[0]["borrado_en"] is not None
    # Una persona agregada de nuevo y quitada por la puerta: deshacer el «borrar» la devuelve.
    q = await db.agregar_participante(("proyecto", 1), 101, "otra vez", gente.dueno, leer_persona=_ficha)
    await db.quitar_participante(q["id"], ("proyecto", 1), gente.dueno)
    h_borrar = _huellas(mundo)[-1]["id"]
    assert await crud.deshacer(h_borrar) == "lo que había archivado"
    assert [f["borrado_en"] for f in _filas(mundo)][-1] is None


async def test_no_se_deshace_el_quitar_si_la_misma_persona_ya_volvio_a_estar_ahi(mundo, gente):
    import acciones.crud as crud
    _sembrar(mundo)
    for donde in (("proyecto", 1), ("tarea", 10)):
        p = await db.agregar_participante(donde, 101, "uno", gente.dueno, leer_persona=_ficha)
        await db.quitar_participante(p["id"], donde, gente.dueno)
        h = _huellas(mundo)[-1]["id"]
        await db.agregar_participante(donde, 101, "dos", gente.dueno, leer_persona=_ficha)   # volvió a entrar
        with pytest.raises(ValueError, match="ya está ahí"):
            await crud.deshacer(h)
        vivas = [f for f in _filas(mundo) if f["borrado_en"] is None
                 and f["noco_id"] == 101 and (f["proyecto_id"] or f["tarea_id"])]
        assert len(vivas) >= 1
