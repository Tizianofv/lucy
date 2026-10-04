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
import re
import textwrap

import os

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-columnas")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

from test_api_code_alertas import _Pool, _base          # noqa: E402  (también pone los stubs)
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
    for fn in (crud.editar, crud.deshacer):
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        llamadas = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                    for n in ast.walk(arbol) if isinstance(n, ast.Call)}
        nombres = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
        assert "es_editable" in llamadas, fn.__name__
        assert "NO_EDITABLES" not in nombres, (fn.__name__, "decide por su cuenta")


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
