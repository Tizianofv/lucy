# -*- coding: utf-8 -*-
"""Las columnas de `tareas` que SOLO escriben otras puertas (`grave`,
`clave_tecnica`, `ultima_alarma_en`, `tomada_en`) no se escriben por `crud.editar`
ni por `crud.deshacer` (4-oct-2026).

El hallazgo del testigo: `NO_EDITABLES` era una lista negra, así que lo que no
estaba en ella era editable «por omisión», y el modelo de Lucy podía subir o
bajar `grave` o reescribir `clave_tecnica` por Telegram. Para `tareas` la regla
es ahora la contraria (`crud.es_editable`): solo se escribe lo clasificado como
editable, y una columna nueva queda NO editable hasta que alguien la clasifique.

Correr:  python3 -m pytest tests/test_columnas_de_sistema.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import textwrap

import os

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-columnas")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

from test_api_code_alertas import (CLIENTE, ESCRITURAS_DESHACER, RUTA, _Pool, _alerta, _base,  # noqa: E402,F401
                                   _h, puerta)  # (también pone los stubs)
import acciones.crud as crud                             # noqa: E402
import db.db as db                                       # noqa: E402
from test_fechas_del_panel import _Base, _Cursor, _correr as _correr_doble, _tarea

SISTEMA = ("grave", "clave_tecnica", "ultima_alarma_en", "tomada_en")


def _columnas_de_tareas() -> set[str]:
    return set(db.columnas_declaradas()["tareas"])


# ── LA LISTA SALE DE LO REAL ──────────────────────────────────────────────

def test_cada_columna_de_tareas_del_esquema_esta_clasificada_en_exactamente_un_sitio():
    reales = _columnas_de_tareas()
    cubos = {"no editables": crud.NO_EDITABLES & reales,
             "editables": set(crud.COLUMNAS_EDITABLES_DE_TAREAS),
             "de sistema": set(crud.COLUMNAS_DE_SISTEMA_DE_TAREAS)}
    sin_clasificar = reales - set().union(*cubos.values())
    assert not sin_clasificar, (
        f"columnas de `tareas` sin clasificar: {sorted(sin_clasificar)}. Decide si "
        "`editar`/`deshacer` pueden escribirlas (COLUMNAS_EDITABLES_DE_TAREAS) o si "
        "las escribe otra puerta (COLUMNAS_DE_SISTEMA_DE_TAREAS)")
    for a, b in (("no editables", "editables"), ("no editables", "de sistema"),
                 ("editables", "de sistema")):
        assert not cubos[a] & cubos[b], (a, b, cubos[a] & cubos[b])
    # y lo clasificado existe: una lista vieja no promete nada
    viejas = (set(crud.COLUMNAS_EDITABLES_DE_TAREAS) | set(crud.COLUMNAS_DE_SISTEMA_DE_TAREAS)) - reales
    assert not viejas, f"clasificadas pero no están en db/schema.sql: {sorted(viejas)}"


def test_las_cuatro_columnas_de_sistema_conocidas_estan_en_el_cubo_de_sistema():
    assert set(SISTEMA) <= set(crud.COLUMNAS_DE_SISTEMA_DE_TAREAS)
    assert not set(SISTEMA) & set(crud.COLUMNAS_EDITABLES_DE_TAREAS)


def test_una_columna_nueva_que_nadie_clasifico_cae_del_lado_estricto():
    """El cubo estricto es alcanzable: con una columna que nadie conoce."""
    assert crud.es_editable("tareas", "columna_inventada") is False
    assert crud.es_editable("tareas", "titulo") is True
    for c in crud.NO_EDITABLES:
        assert crud.es_editable("tareas", c) is False
    # las demás tablas no cambian
    assert crud.es_editable("notas", "columna_inventada") is True


def test_editar_y_deshacer_deciden_por_la_misma_puerta_es_editable():
    """`editar` la llama directo; `deshacer` por `deshacer_la_devuelve`, que la
    llama primero. Ninguno de los dos decide con `NO_EDITABLES` a mano."""
    for fn, puerta_ in ((crud.editar, "es_editable"), (crud.deshacer, "deshacer_la_devuelve")):
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        llamadas = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                    for n in ast.walk(arbol) if isinstance(n, ast.Call)}
        nombres = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
        assert puerta_ in llamadas, fn.__name__
        assert "NO_EDITABLES" not in nombres, (fn.__name__, "decide por su cuenta")
    cuerpo = ast.parse(textwrap.dedent(inspect.getsource(crud.deshacer_la_devuelve)))
    assert "es_editable" in {n.func.id for n in ast.walk(cuerpo)
                             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# ── SONDAS: el escritor genérico corrido de verdad ────────────────────────

def _tarea_de_alerta():
    con = _base()
    con.execute("INSERT INTO tareas (titulo, responsable_chat_id, area, clave_tecnica, grave, "
                "ultima_alarma_en, estado) VALUES ('t', -1, 'IA', 'backup', 1, '2026-10-04', 'pendiente')")
    con.commit()
    db.pool = _Pool(con)
    return con


def _leer(con):
    f = con.execute("SELECT titulo, grave, clave_tecnica, ultima_alarma_en, tomada_en FROM tareas").fetchone()
    return dict(f)


@pytest.fixture
def pool_restaurado():
    guardado = db.pool
    yield
    db.pool = guardado


def test_sonda_editar_no_escribe_ninguna_columna_de_sistema(pool_restaurado):
    """La sonda del testigo (`editar("tareas", 1, {"grave": True})` dejaba
    `grave = 1`), con las otras tres columnas y ahora en sentido contrario."""
    con = _tarea_de_alerta()
    antes = _leer(con)
    for cambios in ({"grave": False}, {"clave_tecnica": "natalia:x"},
                    {"ultima_alarma_en": "2000-01-01"}, {"tomada_en": "2026-10-04"},
                    {"grave": False, "clave_tecnica": "natalia:x"}):
        with pytest.raises(ValueError, match="No hay nada que cambiar"):
            asyncio.run(crud.editar("tareas", 1, cambios, "sonda"))
    assert _leer(con) == antes


def test_sonda_editar_con_lo_editable_y_lo_de_sistema_juntos_escribe_solo_lo_editable(pool_restaurado):
    con = _tarea_de_alerta()
    asyncio.run(crud.editar("tareas", 1, {"titulo": "nuevo", "grave": False,
                                          "clave_tecnica": "natalia:x"}, "sonda"))
    despues = _leer(con)
    assert despues["titulo"] == "nuevo"
    assert (despues["grave"], despues["clave_tecnica"]) == (1, "backup")


def test_sonda_deshacer_no_devuelve_columnas_de_sistema():
    """`crud.deshacer` corrido de verdad sobre una huella cuyo `antes` trae la
    fila entera: lo único que escribe es lo editable."""
    antes = {"titulo": "viejo", "grave": False, "clave_tecnica": "natalia:viejo",
             "ultima_alarma_en": "2026-10-01", "tomada_en": None, "id": 1}
    huella = {"accion": "editar", "tabla": "tareas", "registro_id": 1, "antes": antes,
              "despues": {"titulo": "nuevo"}}

    class _CursorDeshacer(_Cursor):
        async def execute(self, sql, params=None):
            s2 = " ".join(sql.split())
            if "FROM log_acciones" in s2 or s2.startswith("UPDATE tareas t SET"):
                self._base.sql.append((s2, params))
                self._filas = [huella] if "FROM log_acciones" in s2 else []
                return self
            return await super().execute(sql, params)

    class _BaseDeshacer(_Base):
        def cursor(self, row_factory=None):
            return _CursorDeshacer(self)

        async def execute(self, sql, params=None):
            return await _CursorDeshacer(self).execute(sql, params)

    fila = _tarea()
    fila["titulo"] = "nuevo"
    h = _BaseDeshacer(fila)
    _correr_doble(h, lambda: crud.deshacer(77))
    vuelta = [s for s, _ in h.sql if s.startswith("UPDATE tareas t SET")]
    assert vuelta, "deshacer no escribió nada"
    assert set(re.findall(r"(\w+) = r\.\w+", vuelta[0])) == {"titulo"}


# ═══════════════════════════════════════════════════════════════════════
# `deshacer` dice la verdad (vuelta del testigo sobre `d0fbb47`)
#
# Evidencia de `6eb45e7`: `deshacer` devolvía TODA columna del `antes` que no
# estuviera en `NO_EDITABLES`, así que deshacer un «tomar» de la sala dejaba
# `tomada_en` en NULL. Camino (a): lo que el propio sistema mueve con una acción
# que deja huella (`tomada_en`) vuelve, solo si ESA acción lo cambió y solo al
# valor de su huella; `grave`, `clave_tecnica` y `ultima_alarma_en` no vuelven
# nunca, y una huella que dice haberlas cambiado se rechaza sin escribir nada.
# ═══════════════════════════════════════════════════════════════════════

CODE = -1


def _tarea_de_code(con, **extra):
    cols = {"titulo": "t", "responsable_chat_id": CODE, "area": "IA", "estado": "pendiente", **extra}
    con.execute(f"INSERT INTO tareas ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(cols.values()))
    con.commit()


def _fila(con):
    return dict(con.execute("SELECT titulo, grave, clave_tecnica, tomada_en, ultima_alarma_en FROM tareas").fetchone())


def _huella_fabricada(con, antes, despues, accion="editar"):
    cur = con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues, motivo) "
        "VALUES ('lucy', ?, 'tareas', 1, ?, ?, 'fabricada por la prueba')",
        (accion, json.dumps(antes), json.dumps(despues)))
    con.commit()
    return cur.lastrowid


def test_tomar_y_deshacer_devuelve_tomada_en_y_no_toca_las_demas_columnas_de_sistema(puerta):
    """La sonda del testigo, por el camino real: `tomar_tarea_de_la_sala` deja su
    huella y `crud.deshacer` la corre."""
    con = puerta()
    _tarea_de_code(con, grave=1, clave_tecnica="natalia:k", ultima_alarma_en="2026-10-03")
    assert asyncio.run(db.tomar_tarea_de_la_sala(1)) is True
    assert _fila(con)["tomada_en"] is not None
    log_id = con.execute("SELECT id FROM log_acciones WHERE actor = 'sala'").fetchone()[0]
    asyncio.run(crud.deshacer(log_id))
    f = _fila(con)
    assert f["tomada_en"] is None, "deshacer dijo que deshizo y `tomada_en` no volvió"
    assert (f["grave"], f["clave_tecnica"], f["ultima_alarma_en"]) == (1, "natalia:k", "2026-10-03")
    tabla, columnas = ESCRITURAS_DESHACER[-1]
    assert tabla == "tareas" and "tomada_en" in columnas
    assert not set(columnas) & {"grave", "clave_tecnica", "ultima_alarma_en"}


def test_deshacer_otra_edicion_de_una_tarea_que_despues_se_hizo_grave_no_baja_la_marca(puerta):
    con = puerta()
    _tarea_de_code(con, grave=0, clave_tecnica="natalia:k")
    _, log_id = asyncio.run(crud.editar("tareas", 1, {"titulo": "otro título"}, "prueba"))
    r = CLIENTE.post(RUTA, headers=_h(), json=_alerta(clave="k", grave=True))
    assert r.status_code == 200 and r.json()["grave"] is True
    asyncio.run(crud.deshacer(log_id))
    f = _fila(con)
    assert f["titulo"] == "t"                      # la edición sí se deshizo
    assert (f["grave"], f["clave_tecnica"]) == (1, "natalia:k")
    assert not set(ESCRITURAS_DESHACER[-1][1]) & {"grave", "clave_tecnica", "ultima_alarma_en"}


@pytest.mark.parametrize("antes,despues", [
    ({"id": 1, "titulo": "x", "grave": False}, {"titulo": "y", "grave": True}),      # sube
    ({"id": 1, "titulo": "x", "grave": True}, {"titulo": "y", "grave": False}),      # baja
    ({"id": 1, "titulo": "x", "clave_tecnica": "natalia:a"}, {"clave_tecnica": "natalia:b"}),
    ({"id": 1, "titulo": "x", "ultima_alarma_en": "2026-01-01"}, {"ultima_alarma_en": "2026-02-02"}),
    ({"id": 1, "tomada_en": None, "grave": False}, {"tomada_en": "now", "grave": True}),
])
def test_una_huella_fabricada_que_dice_haber_cambiado_una_columna_de_sistema_se_rechaza_sin_escribir(puerta, antes, despues):
    con = puerta()
    _tarea_de_code(con, grave=0, clave_tecnica="natalia:real")
    log_id = _huella_fabricada(con, antes, despues)
    antes_fila = _fila(con)
    with pytest.raises(ValueError, match="No lo deshice"):
        asyncio.run(crud.deshacer(log_id))
    assert _fila(con) == antes_fila
    assert not ESCRITURAS_DESHACER


def test_una_huella_fabricada_con_grave_en_el_antes_que_su_accion_no_cambio_no_lo_escribe(puerta):
    con = puerta()
    _tarea_de_code(con, grave=0, clave_tecnica="natalia:real")
    log_id = _huella_fabricada(
        con, {"id": 1, "titulo": "viejo", "grave": True, "clave_tecnica": "natalia:colada"},
        {"titulo": "nuevo"})
    asyncio.run(crud.deshacer(log_id))
    f = _fila(con)
    assert (f["titulo"], f["grave"], f["clave_tecnica"]) == ("viejo", 0, "natalia:real")
    assert not set(ESCRITURAS_DESHACER[-1][1]) & {"grave", "clave_tecnica", "ultima_alarma_en"}


def test_tomada_en_solo_vuelve_si_esa_accion_la_cambio():
    assert crud.deshacer_la_devuelve("tareas", "tomada_en", {"tomada_en": None}, {"tomada_en": "now"})
    assert not crud.deshacer_la_devuelve("tareas", "tomada_en", {"tomada_en": "a"}, {"tomada_en": "a"})
    assert not crud.deshacer_la_devuelve("tareas", "tomada_en", {"tomada_en": "a"}, {})


def test_lo_de_sistema_que_deshacer_devuelve_esta_declarado_y_es_de_sistema():
    assert set(crud.DESHACER_DEVUELVE_DE_SISTEMA["tareas"]) <= set(crud.COLUMNAS_DE_SISTEMA_DE_TAREAS)
    assert crud.DESHACER_DEVUELVE_DE_SISTEMA["tareas"] == {"tomada_en"}


def test_el_cubo_estricto_de_deshacer_es_alcanzable_con_entradas_inventadas():
    c = crud.columnas_de_sistema_que_no_vuelven
    assert c("tareas", {"grave": 0}, {"grave": 1}) == ["grave"]
    assert c("tareas", {"clave_tecnica": "a", "grave": 0}, {"clave_tecnica": "b", "grave": 1}) == ["clave_tecnica", "grave"]
    assert c("tareas", {"tomada_en": None}, {"tomada_en": "now"}) == []
    assert c("tareas", {"grave": 1}, {"grave": 1}) == []
    assert c("notas", {"grave": 0}, {"grave": 1}) == []
    assert crud.deshacer_la_devuelve("tareas", "columna_inventada", {"columna_inventada": 1},
                                     {"columna_inventada": 2}) is False


def test_deshacer_decide_por_las_funciones_de_crud_y_no_por_su_cuenta():
    arbol = ast.parse(textwrap.dedent(inspect.getsource(crud.deshacer)))
    llamadas = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                for n in ast.walk(arbol) if isinstance(n, ast.Call)}
    assert {"deshacer_la_devuelve", "columnas_de_sistema_que_no_vuelven"} <= llamadas
