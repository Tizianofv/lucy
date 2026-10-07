"""Segunda vuelta de «borrar proyecto o grupo» (el testigo dio NO PASA sobre `018034e`, 7-oct-2026).

Cuatro cosas que faltaban, con el SQL ejecutándose de verdad en SQLite como en
`tests/test_borrar_proyecto_y_grupo.py`:
  1. los avisos que este trabajo agregó o cambió: CADA UNO, sacado de las plantillas, tiene su prueba de
     «dirección escrita a mano con el estado contrario en la base → no sale» y, en el mismo caso, de que sí
     sale cuando el estado es el bueno (para que el «no sale» no sea un vacío); el que faltaba era el del
     grupo borrado que se volvió a crear con el mismo nombre;
  2. la guarda de `crud.borrar` que rechaza `esperado` fuera de proyectos;
  3. lo que de verdad hace el bot al deshacer (corrido, no razonado);
  4. tres recorridos largos de pasos, corridos de verdad, que al final comparan las tablas contra lo que debe
     quedar. ESCOGIDOS por traicioneros: (A) borrar un grupo, volver a crearlo con el mismo nombre y restaurar
     (que lo restaurado NO se cuelgue del grupo nuevo, y que el grupo nuevo pueda borrarse otra vez); (B) borrar
     un proyecto, restaurarlo, borrar una tarea aparte, borrarlo otra vez, restaurarlo y deshacer varias
     huellas viejas (que lo borrado aparte no vuelva con un deshacer viejo); (C) borrar un grupo con un proyecto
     CERRADO, intentar restaurar una tarea suelta de él y después el proyecto (que el rechazo no escriba nada y
     que lo cerrado vuelva cerrado, con su tarea).
FRONTERA: la de `test_borrar_proyecto_y_grupo.py` (SQLite, no Postgres)."""
from __future__ import annotations

import re

import pytest

import test_grupos as tg  # pone el entorno antes de importar `config`
from test_grupos import base  # noqa: F401
from test_pagina_proyectos import _dia, gente, mundo, ver, ver_r  # noqa: F401
from test_borrar_proyecto_y_grupo import (ESPERADO_HOGAR, _avisos, _borrada, _casa, _corre, _filas, _huellas,
                                          _sembrar, _texto)
from test_papelera_de_proyectos import _papelera, pap  # noqa: F401
from test_grupo_ia import _ROOT
import config
import db.db as db
from acciones import crud

PLANTILLAS = _ROOT / "web" / "plantillas"


# ═══════════════════════════════════════════════════════════════════════
# 1. Los avisos: cada uno, con su prueba de «a mano, estado contrario»
# ═══════════════════════════════════════════════════════════════════════

# Lo que este trabajo AGREGÓ o CAMBIÓ en las plantillas: cada clave de `hecho` o `error` que habla de borrar o
# de restaurar. Los demás `hecho` de `proyectos.html` son de antes (no cambiaron: la lista de abajo es un
# TRINQUETE, una clave nueva sin declarar pone roja la prueba hasta que alguien decida a cuál lista va).
HECHOS_DE_ESTE_TRABAJO = {"proyectos.html": {"grupo_borrado", "proyecto_borrado", "tarea_borrada"}}
ERRORES_DE_ESTE_TRABAJO = {"proyectos.html": {"grupo_cambio", "borrar_cambio"},
                           "papelera.html": {"restaurar_no_esta", "restaurar_no_se_pudo"}}
HECHOS_DE_ANTES = {"proyecto_nuevo", "grupo_creado", "grupo_quitado", "responsable", "cerrado", "cliente", "cliente_quitado",
                   "persona", "persona_quitada", "reabierto", "tarea_hecha", "tarea_reabierta", "tarea_titulo",
                   "tarea_proyecto", "tarea_responsable", "comentario", "comentario_editado"}


def _claves(texto: str, campo: str) -> dict[str, bool]:
    """{clave: ¿lleva una condición extra con `and`?} de cada `campo == 'clave'` de la plantilla."""
    return {m.group(1): m.group(2).strip().startswith("and")
            for m in re.finditer(rf"{campo} == '(\w+)'(\s*and\b)?", texto)} | \
           {m.group(1): False for m in re.finditer(rf"{campo} == '(\w+)'\s*%\}}", texto)}


def test_censo_cada_aviso_de_este_trabajo_esta_en_la_plantilla_y_con_su_condicion_de_estado():
    p = (PLANTILLAS / "proyectos.html").read_text(encoding="utf-8")
    hechos = {m.group(1): bool(m.group(2)) for m in re.finditer(r"hecho == '(\w+)'( and )?", p)}
    # una clave de `hecho` que no es de antes ni de este trabajo: alguien agregó un aviso sin decidir
    nuevas = set(hechos) - HECHOS_DE_ANTES - HECHOS_DE_ESTE_TRABAJO["proyectos.html"]
    assert nuevas == set(), nuevas
    for k in HECHOS_DE_ESTE_TRABAJO["proyectos.html"]:
        assert hechos.get(k) is True, f"el aviso «{k}» no lleva una condición de estado (`and …`)"
    for archivo, claves in ERRORES_DE_ESTE_TRABAJO.items():
        t = (PLANTILLAS / archivo).read_text(encoding="utf-8")
        for k in claves:
            assert f"error == '{k}'" in t, (archivo, k)
    # los de «bueno» de la Papelera salen del estado de la base (`de_vuelta`), no de la dirección
    pap_t = (PLANTILLAS / "papelera.html").read_text(encoding="utf-8")
    assert "de_vuelta and de_vuelta.tipo" in pap_t and "hecho ==" not in pap_t
    app = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
    assert "db.aviso_de_proyecto_restaurado(id)" in app and "db.aviso_de_tarea(id, borrada=False)" in app


def test_el_censo_ve_un_aviso_inventado():
    inventado = "{% elif hecho == 'algo_nuevo' %}<p>ok</p>{% elif hecho == 'otro' and cosa %}x{% endif %}"
    hechos = {m.group(1): bool(m.group(2)) for m in re.finditer(r"hecho == '(\w+)'( and )?", inventado)}
    assert hechos == {"algo_nuevo": False, "otro": True}


def _aviso_en(html: str, trozo: str) -> bool:
    return any(trozo in a for a in _avisos(html))


def test_el_aviso_de_grupo_borrado_no_sale_si_el_grupo_se_volvio_a_crear_con_ese_nombre(base):
    """El hueco que vio el testigo: se borra «Hogar», se vuelve a crear, y la dirección escrita a mano
    NO puede decir «ya no está»; sin esa guarda, nada se ponía rojo. Aquí con el recibo puesto (como si
    un POST hubiera mandado a esa dirección): la guarda de estado sigue sola, sin depender de él."""
    _sembrar(base)
    _corre(crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel"))
    q = dict(hecho="grupo_borrado", grupo="Hogar")
    assert _aviso_en(ver_r(base, **q), "ya no está en la lista de la izquierda")          # estado bueno: no existe, y hay huella
    _corre(db.crear_grupo("Hogar"))                                                      # …y ahora existe otra vez
    assert not _aviso_en(ver_r(base, **q), "ya no está"), _avisos(ver_r(base, **q))
    assert not _aviso_en(ver_r(base, **q), "Papelera")
    # y un grupo que existe y nunca tuvo huella, ni uno que nunca existió
    assert not _aviso_en(ver_r(base, hecho="grupo_borrado", grupo="CDS"), "ya no está")
    assert not _aviso_en(ver_r(base, hecho="grupo_borrado", grupo="Nunca existió"), "Papelera")


def _caso_proyecto_borrado(base):
    _sembrar(base)
    q = dict(hecho="proyecto_borrado", borrado=1)
    malo = ver_r(base, **q)                                                                # el proyecto 1 está vivo
    _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
    return malo, ver_r(base, **q), "está en la Papelera"


def _caso_tarea_borrada(base):
    _sembrar(base)
    q = dict(p=4, hecho="tarea_borrada", borrada=20)
    malo = ver_r(base, **q)
    _corre(crud.borrar("tareas", 20, "x", actor="panel"))
    return malo, ver_r(base, **q), "está en la Papelera"


def _caso_grupo_borrado(base):
    _sembrar(base)
    _corre(crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel"))
    _corre(db.crear_grupo("Hogar"))
    q = dict(hecho="grupo_borrado", grupo="Hogar")
    malo = ver_r(base, **q)                                                                # existe de nuevo
    _corre(db.quitar_grupo("Hogar"))
    return malo, ver_r(base, **q), "ya no está en la lista"


def _caso_borrar_cambio(base):
    _sembrar(base)
    q = dict(p=1, borrar_proyecto=1, error="borrar_cambio")
    _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
    malo = ver_r(base, **q)                                                                # ya no está: no hay «sigue aquí»
    _corre(crud.deshacer_borrado("proyectos", 1))
    return malo, ver_r(base, **q), "sigue aquí: NO se borró nada"


def _caso_grupo_cambio(base):
    _sembrar(base)
    q = dict(error="grupo_cambio", quitar_grupo="Hogar")
    _corre(crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel"))
    malo = ver_r(base, **q)                                                                # el grupo ya no existe
    _corre(db.crear_grupo("Hogar"))
    base.proyecto(40, "Nuevo en Hogar", area="Hogar")
    base.con.commit()
    return malo, ver_r(base, **q), "sigue aquí: NO se borró nada"


CASOS_DE_PROYECTOS = {"proyecto_borrado": _caso_proyecto_borrado, "tarea_borrada": _caso_tarea_borrada,
                      "grupo_borrado": _caso_grupo_borrado, "borrar_cambio": _caso_borrar_cambio,
                      "grupo_cambio": _caso_grupo_cambio}


def test_hay_un_caso_por_cada_aviso_declarado_de_proyectos():
    assert set(CASOS_DE_PROYECTOS) == (HECHOS_DE_ESTE_TRABAJO["proyectos.html"]
                                       | ERRORES_DE_ESTE_TRABAJO["proyectos.html"])


@pytest.mark.parametrize("clave", sorted(CASOS_DE_PROYECTOS))
def test_cada_aviso_de_proyectos_no_sale_con_el_estado_contrario_y_si_con_el_bueno(base, clave):
    malo, bueno, texto = CASOS_DE_PROYECTOS[clave](base)
    assert not _aviso_en(malo, texto), (clave, _avisos(malo))
    assert _aviso_en(bueno, texto), (clave, _avisos(bueno))


def test_cada_aviso_de_la_papelera_no_sale_con_el_estado_contrario_y_si_con_el_bueno(pap):
    _sembrar(pap)
    _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
    _corre(crud.borrar("tareas", 15, "x", actor="panel"))
    for hecho, rid, texto in (("proyecto_restaurado", 1, "ya está de vuelta"), ("tarea_restaurada", 15, "ya está de vuelta")):
        assert not _aviso_en(_papelera(pap, hecho=hecho, id=rid), texto), hecho          # sigue borrado
    _corre(crud.deshacer_borrado("proyectos", 1))
    _corre(crud.deshacer_borrado("tareas", 15))
    for hecho, rid in (("proyecto_restaurado", 1), ("tarea_restaurada", 15)):
        assert _aviso_en(_papelera(pap, hecho=hecho, id=rid), "ya está de vuelta"), hecho
    # y el de otra cosa que no se restauró
    assert not _aviso_en(_papelera(pap, hecho="proyecto_restaurado", id=3), "ya está de vuelta")
    # los de error de la Papelera no afirman nada que no sea cierto: no dicen «sigue en la papelera»
    for e in ("restaurar_no_esta", "restaurar_no_se_pudo"):
        assert not _aviso_en(_papelera(pap, error=e), "sigue en la papelera"), e


# ═══════════════════════════════════════════════════════════════════════
# 2. `esperado` solo vale para proyectos
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tabla,rid", [("tareas", 10), ("movimientos", 1), ("personas", 1)])
async def test_esperado_fuera_de_proyectos_se_rechaza_y_no_escribe(base, tabla, rid):
    _sembrar(base)
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base))
    with pytest.raises(ValueError) as e:
        await crud.borrar(tabla, rid, "x", actor="panel", esperado={"tareas_pendientes": 0})
    assert "`esperado` solo vale para proyectos" in str(e.value)
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base)) == antes


# ═══════════════════════════════════════════════════════════════════════
# 3. Lo que hace el bot de verdad
# ═══════════════════════════════════════════════════════════════════════

def _bot(escenario):
    import test_nombre_de_proyecto as t
    b = t.Base()
    p = b.proyecto("Casa")
    b.con.execute("INSERT INTO tareas (id, titulo, proyecto_id) VALUES (1, 'a', ?), (2, 'b', ?)", (p, p))
    r, _ = t._herramienta(b, "archivar", {"tabla": "proyectos", "id": p})
    log = int(re.search(r"acción #(\d+)", r).group(1))
    escenario(b, p)
    d, _ = t._herramienta(b, "deshacer", {"accion": log})
    vivo = lambda tabla: [tuple(f) for f in b.con.execute(f"SELECT id, borrado_en IS NULL FROM {tabla} ORDER BY id")]  # noqa: E731
    return r, d, vivo("tareas"), vivo("proyectos")


def test_el_bot_al_deshacer_con_el_nombre_repetido_se_niega_dice_por_que_y_no_escribe():
    archivado, deshecho, tareas, proyectos = _bot(lambda b, p: b.proyecto("casa"))
    assert deshecho == "ERROR: No lo deshice: ya hay otro proyecto vivo con ese nombre."
    assert tareas == [(1, 0), (2, 0)] and proyectos == [(1, 0), (2, 1)]
    # y lo que Lucy le dijo ANTES no prometió más que eso
    assert "Deshacer intenta traerlo de vuelta con ellas y puede negarse" in archivado
    assert "por ejemplo, si ya hay otro proyecto vivo con ese nombre" in archivado and "trae de vuelta con ellas." not in archivado


def test_el_bot_al_deshacer_en_el_caso_normal_trae_el_proyecto_con_sus_tareas():
    _, deshecho, tareas, proyectos = _bot(lambda b, p: None)
    assert deshecho == "OK: revertí lo que había archivado y 2 tareas que se fueron con él."
    assert tareas == [(1, 1), (2, 1)] and proyectos == [(1, 1)]


# ═══════════════════════════════════════════════════════════════════════
# 4. Recorridos largos, corridos de verdad
# ═══════════════════════════════════════════════════════════════════════

def _estado(m) -> dict:
    """Lo que importa de las tablas: (borrada, grupo) de cada proyecto y de cada tarea."""
    return {"proyectos": {i: (f["borrado_en"] is not None, f["area"], f["estado"]) for i, f in _filas(m, "proyectos").items()},
            "tareas": {i: (f["borrado_en"] is not None, f["area"], f["proyecto_id"]) for i, f in _filas(m, "tareas").items()},
            "grupos": sorted(r[0] for r in m.con.execute("SELECT clave FROM areas"))}


async def test_recorrido_A_borrar_el_grupo_recrearlo_con_el_mismo_nombre_y_restaurar(base):
    _sembrar(base)
    await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    nuevo = await db.crear_grupo("Hogar")                                   # mismo nombre, otro color y otro orden
    assert nuevo["color"] != "#2f6fb3"
    await crud.deshacer_borrado("proyectos", 1)
    await crud.deshacer_borrado("tareas", 15)
    e = _estado(base)
    # lo restaurado NO se cuelga del grupo nuevo: vuelve a «Sin grupo»; el grupo nuevo está vacío
    assert e["proyectos"][1] == (False, None, "activo") and e["tareas"][15] == (False, None, None)
    assert [i for i, (b, a, _) in e["proyectos"].items() if a == "Hogar"] == []
    assert [i for i, (b, a, _) in e["tareas"].items() if a == "Hogar"] == []
    assert {t: e["tareas"][t][0] for t in (10, 11, 12, 13)} == {10: False, 11: False, 12: False, 13: True}
    assert e["proyectos"][2][0] and e["proyectos"][3][0] and e["tareas"][16][0] and e["tareas"][14][0]
    assert e["grupos"] == ["ACD", "CDS", "Hogar", "IA"]
    # el grupo nuevo, vacío, se quita con la ruta de los vacíos; lo restaurado sigue en «Sin grupo»
    await db.quitar_grupo("Hogar")
    assert _estado(base)["grupos"] == ["ACD", "CDS", "IA"] and _estado(base)["proyectos"][1] == (False, None, "activo")
    assert [h["registro_id"] for h in _huellas(base, tabla="areas")] == [0]


async def test_recorrido_B_proyecto_borrado_restaurado_tarea_aparte_borrado_otra_vez_y_deshacer_viejo(base):
    _sembrar(base)
    l1 = await crud.borrar("proyectos", 1, "primera", actor="panel")
    await crud.deshacer_borrado("proyectos", 1)
    assert all(not _borrada(base, "tareas", t) for t in (10, 11, 12))
    await crud.borrar("tareas", 10, "aparte", actor="panel")
    l2 = await crud.borrar("proyectos", 1, "segunda", actor="panel")
    assert _json_tareas(base, l2) == [11, 12]                              # la 10 ya estaba borrada aparte: no va en la lista
    await crud.deshacer_borrado("proyectos", 1)
    e = _estado(base)
    assert e["proyectos"][1][0] is False
    assert {t: e["tareas"][t][0] for t in (10, 11, 12, 13)} == {10: True, 11: False, 12: False, 13: True}
    # deshacer VARIAS huellas, las viejas incluidas, de atrás para adelante: lo borrado aparte NO vuelve
    cuantas, fallos = await crud.deshacer_varias([l1, l2])
    assert (cuantas, fallos) == (2, [])
    e = _estado(base)
    assert {t: e["tareas"][t][0] for t in (10, 11, 12, 13)} == {10: True, 11: False, 12: False, 13: True}
    assert e["proyectos"][1][0] is False and e["proyectos"][4][0] is False and e["tareas"][20][0] is False


def _json_tareas(m, log_id):
    return sorted(__import__("db.db", fromlist=["x"])._json_de(_huellas(m, id=log_id)[0]["despues"])["tareas_con_el"])


async def test_recorrido_C_grupo_con_un_proyecto_cerrado_tarea_sola_rechazada_y_despues_el_proyecto(base):
    _sembrar(base)
    await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    antes = _estado(base)
    with pytest.raises(ValueError) as e:
        await crud.deshacer_borrado("tareas", 14)                          # la tarea del proyecto CERRADO, sola
    assert "su proyecto está en la papelera" in str(e.value)
    assert _estado(base) == antes                                           # el rechazo no escribió nada
    await crud.deshacer_borrado("proyectos", 2)
    d = _estado(base)
    assert d["proyectos"][2] == (False, None, "cerrado"), "lo cerrado vuelve cerrado, y sin grupo"
    assert d["tareas"][14] == (False, None, 2)
    assert d["proyectos"][1][0] and d["tareas"][10][0] and d["tareas"][15][0]       # lo demás sigue borrado
    # y ahora sí: una tarea de un proyecto CERRADO que se borra aparte no se restaura sola (el proyecto no recibe)
    await crud.borrar("tareas", 14, "aparte", actor="panel")
    with pytest.raises(ValueError) as e2:
        await crud.deshacer_borrado("tareas", 14)
    assert "cerrado" in str(e2.value)
