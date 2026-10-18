"""Los ARCHIVADOS de la página de Proyectos (Archivar y borrar, pieza 2, 18-oct-2026).

«Archivado» es «cerrado»: no hay estado nuevo. Lo que cambia es la palabra del desplegable («N archivados») y
que cada archivado diga «Cerrado el <día>». Qué se vigila, por la ruta REAL, la plantilla REAL y SQL que se
ejecuta de verdad (SQLite con el `CREATE TABLE` de `db/schema.sql` y huellas con la forma de las que deja
`crud.editar`: la FILA ENTERA en `antes` y `despues`):

  G5  Hermanos: la lista de la casa, la de solo ver, la vista de estados y el aviso de «Proyecto cerrado» dicen
      «archivado(s)»; ningún `<summary>` de ninguna vista dice «N cerrado(s)».
  G6  La fecha sale del ÚLTIMO CIERRE (la huella `editar` donde `antes.estado` no era cerrado y `despues.estado`
      sí): una edición posterior de un cerrado (trae `despues.estado = 'cerrado'` pero `antes` también) no la
      mueve; cerrar, reabrir y cerrar da el segundo; un reabierto no muestra fecha. El día es el de Santo Domingo.
  G6b Los DOS escritores del `estado` (`crud.editar` y `crud.deshacer`) cuentan: deshacer una reapertura vuelve
      a cerrar el proyecto y la fecha es la del deshacer; deshacer lo que no lo cerró, o de otro proyecto, no.
  G7  Sin huella de cierre: «Cerrado», sin fecha (no se inventa una).
  G8  Solo ver ve la fecha, en la lista y en la ficha, y ningún formulario del cierre.

FRONTERA, dicha una vez: no hay Postgres en esta máquina (`->>` e `IS DISTINCT FROM` corren en SQLite 3.50);
un cierre que vuelve por `deshacer` (otra acción) no cuenta como cierre; no es un navegador.

Correr:  python3 -m pytest tests/test_archivados.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import test_pagina_proyectos as tp  # noqa: E402
from test_pagina_proyectos import _dia, gente, mundo, ver, ver_r  # noqa: F401
from _navegador import Navegador  # noqa: E402
import config
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402


def _cierre(m, pid, cuando, antes="activo", despues="cerrado", accion="editar"):
    """Una huella con la forma de `crud.editar`: la fila entera, con su `estado`."""
    m.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, ts, antes, despues) "
        "VALUES ('lucy', ?, 'proyectos', ?, ?, ?, ?)",
        (accion, pid, cuando.isoformat(),
         json.dumps({"id": pid, "estado": antes}), json.dumps({"id": pid, "estado": despues})))


def _ver_como_ver(**consulta) -> str:
    c = Navegador(panel.app)
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    r = c.get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _lineas_cerrado_el(html: str) -> list[str]:
    return re.findall(r'<span class="sub cerrado-el">(.*?)</span>', html, re.S)


def _resumenes(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", s).strip() for s in re.findall(r"<summary[^>]*>(.*?)</summary>", html, re.S)]


def _dos_cerrados(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado", cliente="Cliente A")
    mundo.proyecto(2, "Dos", area="CDS", estado="cerrado", cliente="Cliente A")
    mundo.proyecto(3, "Vivo", area="CDS", cliente="Cliente B")


# ═══════════════════════════════════════════════════════════════════════
# G5: las palabras, en todos los sitios
# ═══════════════════════════════════════════════════════════════════════

def test_G5_el_desplegable_dice_archivados_en_la_casa_en_solo_ver_y_en_la_vista_de_estados(mundo):
    _dos_cerrados(mundo)
    vistas = {"casa": ver(mundo, p=3), "solo ver": _ver_como_ver(p=3),
              "estados": ver(mundo, estado=1), "estados (solo ver)": _ver_como_ver(estado=1)}
    for nombre, html in vistas.items():
        res = _resumenes(html)
        assert "2 archivados" in res, (nombre, res)
        # Hermanos: ningún <summary> de ninguna vista dice «N cerrado(s)».
        assert not [r for r in res if re.fullmatch(r"\d+ cerrados?", r)], (nombre, res)
    assert "Archivado: sin próxima tarea" in vistas["estados"]
    assert "Cerrado: sin próxima tarea" not in vistas["estados"]


def test_G5_uno_solo_dice_archivado_en_singular(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado", cliente="Cliente A")
    assert "1 archivado" in _resumenes(ver(mundo, p=1))


def test_G5_el_aviso_de_recien_cerrado_dice_archivados(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    html = ver_r(mundo, p=1, hecho="cerrado")
    assert "Proyecto cerrado. Está en «archivados» en la lista de la izquierda." in html
    assert "«cerrados»" not in html


def test_G5_el_boton_de_cerrar_y_el_de_reabrir_no_cambian(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.proyecto(2, "Dos", area="CDS")
    assert ">Reabrir<" in ver(mundo, p=1)
    assert ">Cerrar proyecto<" in ver(mundo, p=2)


# ═══════════════════════════════════════════════════════════════════════
# G6: la fecha sale del último cierre
# ═══════════════════════════════════════════════════════════════════════

def test_G6_un_cierre_da_su_dia_en_la_lista_y_en_la_ficha(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado", cliente="Cliente A")
    _cierre(mundo, 1, _dia(-5))
    esperado = f"Cerrado el {tp._dia(-5).day} oct 2026"
    html = ver(mundo, p=1)
    assert _lineas_cerrado_el(html) == [esperado]
    assert f"Proyecto cerrado el {tp._dia(-5).day} oct 2026: no se le agregan tareas." in html


def test_G6_una_edicion_posterior_del_cerrado_no_mueve_la_fecha(mundo):
    """`crud.editar` guarda la fila entera: editar la descripción de un cerrado deja `despues.estado =
    'cerrado'`. Eso no es un cierre."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierre(mundo, 1, _dia(-8))
    _cierre(mundo, 1, _dia(-2), antes="cerrado", despues="cerrado")
    assert _lineas_cerrado_el(ver(mundo, p=1)) == [f"Cerrado el {tp._dia(-8).day} oct 2026"]


def test_G6_cerrar_reabrir_y_cerrar_da_el_ultimo_cierre(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierre(mundo, 1, _dia(-9))
    _cierre(mundo, 1, _dia(-6), antes="cerrado", despues="activo")
    _cierre(mundo, 1, _dia(-3))
    assert _lineas_cerrado_el(ver(mundo, p=1)) == [f"Cerrado el {tp._dia(-3).day} oct 2026"]


def test_G6_la_fecha_de_un_proyecto_no_sale_de_la_huella_de_otro(mundo):
    _dos_cerrados(mundo)
    _cierre(mundo, 1, _dia(-7))
    _cierre(mundo, 2, _dia(-4))
    # La lista va por nombre: «Dos» (cerrado hace 4 días) y luego «Uno» (hace 7).
    assert _lineas_cerrado_el(ver(mundo, p=1)) == [f"Cerrado el {tp._dia(-4).day} oct 2026",
                                                   f"Cerrado el {tp._dia(-7).day} oct 2026"]


def test_G6_un_reabierto_no_muestra_fecha(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="activo")
    _cierre(mundo, 1, _dia(-6))
    _cierre(mundo, 1, _dia(-3), antes="cerrado", despues="activo")
    html = ver(mundo, p=1)
    assert _lineas_cerrado_el(html) == [] and "Cerrado el" not in html


def test_G6_el_modelo_solo_da_fecha_a_los_cerrados_aunque_la_huella_exista():
    """Se sostiene en el modelo y no solo en la plantilla: un reabierto trae `cerrado_el` en None."""
    cuando = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    pr = [dict(id=1, nombre="A", area="CDS", estado="cerrado"), dict(id=2, nombre="B", area="CDS", estado="activo")]
    base = {"creado_en": tp.CREADO, "descripcion": None, "responsable_chat_id": None, "cliente_nombre": None}
    m = db.armar_pagina(list(tp.AREAS), [{**base, **p} for p in pr], [], [], [], {}, tp.HOY,
                        cierres={1: cuando, 2: cuando})
    assert m["proyectos"][1]["cerrado_el"] == cuando.date() and m["proyectos"][2]["cerrado_el"] is None


def test_G6_el_dia_es_el_de_santo_domingo_no_el_de_utc(mundo):
    """02:00 UTC del 6 de oct son las 22:00 del 5 de oct en Santo Domingo."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, ts, antes, despues) VALUES "
        "('lucy', 'editar', 'proyectos', 1, '2026-10-06T02:00:00+00:00', ?, ?)",
        (json.dumps({"estado": "activo"}), json.dumps({"estado": "cerrado"})))
    assert _lineas_cerrado_el(ver(mundo, p=1)) == ["Cerrado el 5 oct 2026"]


def test_G6_la_lectura_solo_cuenta_proyectos_y_editar(mundo):
    """Una tarea con el mismo id, una acción que no es `editar` y un estado distinto no son cierres."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, ts, antes, despues) VALUES "
        "('lucy', 'editar', 'tareas', 1, ?, ?, ?)",
        (_dia(-1).isoformat(), json.dumps({"estado": "pendiente"}), json.dumps({"estado": "cerrado"})))
    _cierre(mundo, 1, _dia(-1), accion="borrar")
    _cierre(mundo, 1, _dia(-1), antes="activo", despues="pausado")
    assert asyncio.run(db.cierres_de_proyectos()) == {}


def test_G6_la_lectura_devuelve_el_instante_del_ultimo_cierre(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierre(mundo, 1, _dia(-9))
    _cierre(mundo, 1, _dia(-3))
    r = asyncio.run(db.cierres_de_proyectos())
    assert list(r) == [1] and r[1] == _dia(-3).astimezone(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════
# G7: sin huella no se inventa fecha
# ═══════════════════════════════════════════════════════════════════════

def test_G7_sin_huella_dice_cerrado_sin_fecha(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado", cliente="Cliente A")
    html = ver(mundo, p=1)
    assert _lineas_cerrado_el(html) == ["Cerrado"]
    assert "Cerrado el" not in html and "Proyecto cerrado: no se le agregan tareas." in html


def test_G7_una_huella_que_no_es_cierre_tampoco_da_fecha(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.huella("editar", "proyectos", 1, _dia(-2))        # sin antes ni despues
    assert _lineas_cerrado_el(ver(mundo, p=1)) == ["Cerrado"]


def test_G7_el_modelo_sin_cierres_no_se_rompe():
    """`armar_pagina` sin `cierres` (la forma de todas las pruebas viejas) deja `cerrado_el` en None."""
    import _proyectos_de_prueba as pp
    m = pp.modelo()
    assert m["proyectos"][2]["cerrado"] and m["proyectos"][2]["cerrado_el"] is None


# ═══════════════════════════════════════════════════════════════════════
# G8: solo ver ve la fecha y ningún control
# ═══════════════════════════════════════════════════════════════════════

def test_G8_solo_ver_ve_la_fecha_y_ningun_control_del_cierre(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado", cliente="Cliente A")
    _cierre(mundo, 1, _dia(-5))
    html = _ver_como_ver(p=1)
    esperado = f"{tp._dia(-5).day} oct 2026"
    assert _lineas_cerrado_el(html) == [f"Cerrado el {esperado}"]
    assert f"Proyecto cerrado el {esperado}: no se le agregan tareas." in html
    assert "Reabrir" not in html and "/estado" not in html


# ═══════════════════════════════════════════════════════════════════════
# G6b: `deshacer` también cierra (hallazgo H1 del testigo)
# ═══════════════════════════════════════════════════════════════════════

_RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _huella(m, hid, pid, accion, cuando, antes=None, despues=None, motivo=None, tabla="proyectos"):
    """Una huella con id fijo. `deshacer` va con `antes` y `despues` VACÍOS, como la escribe `crud.deshacer`."""
    m.con.execute(
        "INSERT INTO log_acciones (id, actor, accion, tabla, registro_id, ts, antes, despues, motivo) "
        "VALUES (?, 'lucy', ?, ?, ?, ?, ?, ?, ?)",
        (hid, accion, tabla, pid, cuando.isoformat(),
         json.dumps(antes) if antes is not None else None,
         json.dumps(despues) if despues is not None else None, motivo))


def _deshacer(m, hid, pid, cuando, deshizo, accion="editar"):
    _huella(m, hid, pid, "deshacer", cuando, motivo=db.motivo_de_deshacer(deshizo, accion))


def _cierra_reabre(m, pid=1):
    """Huellas 10 (cierra, hace 9 días) y 11 (reabre, hace 6)."""
    _huella(m, 10, pid, "editar", _dia(-9), {"estado": "activo"}, {"estado": "cerrado"})
    _huella(m, 11, pid, "editar", _dia(-6), {"estado": "cerrado"}, {"estado": "activo"})


def test_G6b_el_caso_del_testigo_cerrar_reabrir_y_deshacer_la_reapertura_da_la_fecha_del_deshacer(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierra_reabre(mundo)
    _deshacer(mundo, 12, 1, _dia(-2), deshizo=11)
    html = ver(mundo, p=1)
    assert _lineas_cerrado_el(html) == [f"Cerrado el {tp._dia(-2).day} oct 2026"]
    assert f"Proyecto cerrado el {tp._dia(-2).day} oct 2026: no se le agregan tareas." in html


def test_G6b_deshacer_el_cierre_lo_reabre_y_no_es_un_cierre(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _huella(mundo, 10, 1, "editar", _dia(-9), {"estado": "activo"}, {"estado": "cerrado"})
    _deshacer(mundo, 11, 1, _dia(-4), deshizo=10)
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


def test_G6b_deshacer_una_edicion_que_no_toco_el_estado_no_mueve_la_fecha(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _huella(mundo, 10, 1, "editar", _dia(-9), {"estado": "activo"}, {"estado": "cerrado"})
    _huella(mundo, 11, 1, "editar", _dia(-6), {"estado": "cerrado", "x": 1}, {"estado": "cerrado", "x": 2})
    _deshacer(mundo, 12, 1, _dia(-2), deshizo=11)
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


def test_G6b_deshacer_un_cambio_de_estado_que_no_salio_de_cerrado_no_cierra(mundo):
    """Deshacer pausado → activo devuelve `pausado`, nunca `cerrado`: no es un cierre aunque el estado cambiara."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _huella(mundo, 10, 1, "editar", _dia(-9), {"estado": "activo"}, {"estado": "cerrado"})
    _huella(mundo, 11, 1, "editar", _dia(-6), {"estado": "pausado"}, {"estado": "activo"})
    _deshacer(mundo, 12, 1, _dia(-2), deshizo=11)
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


@pytest.mark.parametrize("motivo", [None, "", "Volvió con el proyecto #1 (acción #11)",
                                    "Tiziano deshizo la acción #999 (editar)"])
def test_G6b_un_deshacer_que_no_apunta_a_una_reapertura_conocida_no_cierra(mundo, motivo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierra_reabre(mundo)
    _huella(mundo, 12, 1, "deshacer", _dia(-2), motivo=motivo)
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


def test_G6b_el_deshacer_de_otro_proyecto_no_cierra_este(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.proyecto(2, "Dos", area="CDS", estado="activo")
    _cierra_reabre(mundo, 1)
    _deshacer(mundo, 12, 2, _dia(-2), deshizo=11)       # apunta a la huella del proyecto 1, pero es del 2
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


def test_G6b_un_deshacer_de_una_tarea_con_el_mismo_numero_no_cierra(mundo):
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    _cierra_reabre(mundo)
    _huella(mundo, 12, 1, "deshacer", _dia(-2), tabla="tareas", motivo=db.motivo_de_deshacer(11, "editar"))
    assert asyncio.run(db.cierres_de_proyectos()) == {1: _dia(-9).astimezone(timezone.utc)}


def test_G6b_el_motivo_se_escribe_y_se_lee_con_las_mismas_dos_funciones():
    assert db.id_que_deshizo(db.motivo_de_deshacer(4242, "editar")) == 4242
    assert db.id_que_deshizo("otra cosa") is None and db.id_que_deshizo(None) is None
    codigo = (_RAIZ / "acciones" / "crud.py").read_text(encoding="utf-8")
    # `crud.deshacer` lo escribe por la función compartida, no con su propia copia del texto.
    assert "deshizo la acción" not in codigo and "db.motivo_de_deshacer(log_id" in codigo


def test_G6b_los_escritores_del_estado_de_un_proyecto_siguen_siendo_los_dos_genericos_de_crud():
    """La lista de «quién cierra» sale de lo real: cada `UPDATE {tabla} SET {asignaciones}` de `crud.py` (editar y
    deshacer) y ningún `UPDATE proyectos` fuera de `tests/` que toque `estado`. Un tercero pone roja esta prueba:
    hay que decidir cómo deja su huella antes de que la fecha pueda leerla."""
    genericos = re.findall(r'f"UPDATE \{tabla\}(?: t)? SET \{asignaciones\}',
                           (_RAIZ / "acciones" / "crud.py").read_text(encoding="utf-8"))
    assert len(genericos) == 2
    for ruta in _py_en_disco(_RAIZ):       # la única puerta del repo: no entra en entornos virtuales
        if ruta.relative_to(_RAIZ).parts[0] == "tests":
            continue
        for sql in re.findall(r"UPDATE\s+proyectos\s+SET\b[^\"]*", ruta.read_text(encoding="utf-8")):
            assert not re.search(r"\bestado\s*=", sql), (str(ruta), sql)


# ═══════════════════════════════════════════════════════════════════════
# H2: un `antes` sin estado
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("antes", [None, {}, {"id": 1}])
def test_G6_un_cierre_cuyo_antes_no_trae_estado_cuenta_como_cierre(mundo, antes):
    """El proyecto no estaba cerrado antes si el `antes` no lo dice (NULL ≠ 'cerrado')."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.con.execute(
        "INSERT INTO log_acciones (id, actor, accion, tabla, registro_id, ts, antes, despues) "
        "VALUES (20, 'lucy', 'editar', 'proyectos', 1, ?, ?, ?)",
        (_dia(-3).isoformat(), json.dumps(antes) if antes is not None else None,
         json.dumps({"estado": "cerrado"})))
    assert list(asyncio.run(db.cierres_de_proyectos())) == [1]
