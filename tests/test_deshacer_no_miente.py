# -*- coding: utf-8 -*-
"""`crud.deshacer` deja de mentir (pieza 1 de «Archivar y borrar», 18-oct-2026).

EL DEFECTO: contra una fila que ya no existe (la purga de la papelera la borrará de verdad), Postgres no da error
en un `UPDATE ... WHERE id = x`: `deshacer` no miraba cuántas filas tocó, escribía otra huella `deshacer` y
contestaba «revertí …» sin haber hecho nada. Lo que se exige ahora: si el `UPDATE` no tocó ninguna fila,
`ValueError("No lo deshice: eso ya no existe (se borró de verdad).")`, SIN huella `deshacer`.

QUÉ SE VIGILA, y con qué camino:
  · LAS RAMAS SALEN DE LO REAL: del árbol sintáctico de `crud.deshacer` (las acciones con que compara la huella),
    por todas las tablas que `crud.TABLAS` nombra, más una tabla inventada que se le agrega a `TABLAS`.
  · Se ejecuta el SQL del repositorio contra sqlite (la misma base que `test_nombre_de_proyecto.py`). LO FINGIDO,
    dicho: sqlite no tiene `jsonb_populate_record`, así que el `UPDATE` de la rama `editar` se EMULA con Python
    (`_Cur._emular_deshacer`); se prueba la lógica y el texto, NO lo que Postgres hace con el `UPDATE ... FROM
    jsonb_populate_record` (que `rowcount` de psycopg cuenta las filas tocadas es comportamiento de la biblioteca,
    no comprobado acá contra un Postgres).
  · El mismo mensaje por los cuatro caminos que llaman a `deshacer`: el botón de Telegram, la herramienta del
    agente («deshaz»), `deshacer_varias` y la papelera de la página (`deshacer_borrado`).

Correr:  python3 -m pytest tests/test_deshacer_no_miente.py
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_nombre_de_proyecto as tnp  # noqa: E402  (deja `psycopg` falseado y trae `Base`)
import test_botones_rosi as tbr  # noqa: E402
from test_buzon_que_no_se_ve import _py_en_disco  # noqa: E402

import acciones.botones as botones  # noqa: E402
import acciones.crud as crud  # noqa: E402
import cerebro.agente as agente  # noqa: E402
import db.db as db  # noqa: E402

_RAIZ = Path(__file__).resolve().parent.parent
YA_NO_EXISTE = "No lo deshice: eso ya no existe (se borró de verdad)."
YA_RECUPERADO = "No lo deshice: eso ya estaba recuperado."
# La columna que cada tabla edita en estas pruebas (proyectos: la descripción, con puerta y con candado, que es la
# que más reglas tiene; el resto, una sin puerta). La fila de ahora trae el valor de `despues` ('nuevo').
_COLUMNA = {"tareas": "detalle", "proyectos": "descripcion"}
_POR_OMISION = "nota"


def _ramas_de_deshacer() -> list[str]:
    """Las acciones de huella que `crud.deshacer` distingue, SACADAS de su árbol sintáctico: toda comparación
    `huella["accion"] == "<texto>"`. Si mañana se agrega una rama, entra sola."""
    arbol = ast.parse(textwrap.dedent(inspect.getsource(crud.deshacer)))
    salida = []
    for n in ast.walk(arbol):
        if (isinstance(n, ast.Compare) and isinstance(n.ops[0], ast.Eq)
                and isinstance(n.left, ast.Subscript) and isinstance(n.left.value, ast.Name)
                and n.left.value.id == "huella" and isinstance(n.left.slice, ast.Constant)
                and n.left.slice.value == "accion"
                and isinstance(n.comparators[0], ast.Constant)):
            salida.append(n.comparators[0].value)
    return sorted(set(salida))


RAMAS = _ramas_de_deshacer()
TABLAS = list(crud.TABLAS)


class Mundo(tnp.Base):
    """`Base` (proyectos, tareas, log_acciones) + una tabla genérica por cada otra tabla de `crud.TABLAS`."""

    def __init__(self, extra=()):
        super().__init__()
        for t in dict.fromkeys([*crud.TABLAS, *extra]):
            if t not in ("proyectos", "tareas"):
                self.con.execute(
                    f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, borrado_en, nota, proyecto_id, tarea_id, noco_id)")

    def fila(self, tabla, rid=1, *, borrada=False):
        borrado = "2026-09-01" if borrada else None
        if tabla == "proyectos":
            self.con.execute("INSERT INTO proyectos (id, nombre, area, descripcion, borrado_en) VALUES (?, 'Casa', 'IA', 'nuevo', ?)",
                             (rid, borrado))
        elif tabla == "tareas":
            self.con.execute("INSERT INTO tareas (id, titulo, detalle, borrado_en) VALUES (?, 'T', 'nuevo', ?)",
                             (rid, borrado))
        else:
            self.con.execute(f"INSERT INTO {tabla} (id, nota, borrado_en) VALUES (?, 'nuevo', ?)", (rid, borrado))

    def huella(self, accion, tabla, rid):
        col = _COLUMNA.get(tabla, _POR_OMISION)
        antes = {"id": rid, col: "viejo"}
        despues = {col: "nuevo"}
        c = self.con.execute(
            "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) VALUES ('lucy', ?, ?, ?, ?, ?)",
            (accion, tabla, rid, json.dumps(antes), json.dumps(despues)))
        return c.lastrowid

    def foto_del_log(self):
        return self.con.execute("SELECT id, accion, tabla, registro_id FROM log_acciones ORDER BY id").fetchall()

    def borrado(self, tabla, rid=1):
        f = self.con.execute(f"SELECT borrado_en FROM {tabla} WHERE id = ?", (rid,)).fetchone()
        return None if f is None else f[0]

    def valor(self, tabla, rid=1):
        col = _COLUMNA.get(tabla, _POR_OMISION)
        f = self.con.execute(f"SELECT {col} FROM {tabla} WHERE id = ?", (rid,)).fetchone()
        return None if f is None else f[0]


def _deshacer(m, log_id):
    return tnp._correr(m, lambda: crud.deshacer(log_id))


def _rechazo(m, log_id):
    return tnp._rechazo(m, lambda: crud.deshacer(log_id))


def test_las_ramas_salen_del_codigo_y_no_estan_vacias():
    assert RAMAS and all(isinstance(r, str) for r in RAMAS), RAMAS
    # una acción que `deshacer` no distingue tampoco miente: dice que no sabe
    m = Mundo()
    m.fila("tareas")
    e = _rechazo(m, m.huella("inventada", "tareas", 1))
    assert isinstance(e, ValueError) and "No sé deshacer una acción de tipo 'inventada'" in str(e)
    assert [f[1] for f in m.foto_del_log()] == ["inventada"]


# ── G1: la fila ya no existe → ValueError, y el registro queda igual ─────────────────────────────────────
@pytest.mark.parametrize("tabla", TABLAS)
@pytest.mark.parametrize("accion", RAMAS)
def test_G1_deshacer_de_una_fila_que_ya_no_existe_no_afirma_nada_y_no_escribe(accion, tabla):
    m = Mundo()
    log_id = m.huella(accion, tabla, 999)          # el registro 999 nunca existió (o la purga lo borró)
    antes = m.foto_del_log()
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError), f"{accion}/{tabla}: contestó en vez de rechazar"
    assert str(e) == YA_NO_EXISTE, f"{accion}/{tabla}: {e}"
    assert m.foto_del_log() == antes, f"{accion}/{tabla}: dejó una huella de un deshacer que no hizo nada"


# ── G2: la fila que sí existe sigue funcionando igual ────────────────────────────────────────────────────
_QUE = {"crear": "lo que había creado", "borrar": "lo que había archivado", "editar": "el cambio"}


@pytest.mark.parametrize("tabla", TABLAS)
@pytest.mark.parametrize("accion", RAMAS)
def test_G2_una_fila_que_existe_se_deshace_como_siempre(accion, tabla):
    m = Mundo()
    m.fila(tabla, 1, borrada=(accion == "borrar"))
    antes = len(m.foto_del_log())
    log_id = m.huella(accion, tabla, 1)
    r = _deshacer(m, log_id)
    assert r == _QUE[accion], f"{accion}/{tabla}: {r!r}"
    if accion == "crear":
        assert m.borrado(tabla) is not None, "no la mandó a la papelera"
    elif accion == "borrar":
        assert m.borrado(tabla) is None, "no la sacó de la papelera"
    else:
        assert m.valor(tabla) == "viejo", "no devolvió el valor de antes"
    nuevas = m.foto_del_log()[antes + 1:]
    assert [(f[1], f[2], f[3]) for f in nuevas] == [("deshacer", tabla, 1)], nuevas


@pytest.mark.parametrize("tabla", TABLAS)
def test_G2b_deshacer_un_borrar_de_una_fila_que_ya_estaba_viva_no_afirma_nada_y_no_escribe(tabla):
    """El caso del testigo: repetir un deshacer viejo. La fila existe y no está en la papelera: el `UPDATE ... SET
    borrado_en = NULL` la tocaría NULL a NULL y se diría «revertí». Ahora: la verdad, sin huella."""
    m = Mundo()
    m.fila(tabla, 1, borrada=False)
    log_id = m.huella("borrar", tabla, 1)
    antes = m.foto_del_log()
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError) and str(e) == YA_RECUPERADO, f"{tabla}: {e}"
    assert m.foto_del_log() == antes, f"{tabla}: dejó una huella de un deshacer que no hizo nada"
    assert m.borrado(tabla) is None


def test_G4b_los_cuatro_caminos_dicen_lo_mismo_de_una_fila_que_ya_estaba_recuperada():
    m = Mundo()
    m.fila("tareas", 1)
    log_id = m.huella("borrar", "tareas", 1)
    antes = m.foto_del_log()
    r = tnp._correr(m, lambda: agente._ejecutar_herramienta("deshacer", {"accion": log_id}, 1, []))
    assert r == f"ERROR: {YA_RECUPERADO}", r
    restaurar = tbr._con_gente({tbr.DUENO: "Tiziano"})
    guardado = db.pool
    db.pool = m.pool()
    try:
        q = tbr._Q(f"und:{log_id}", tbr.DUENO)
        tnp._correr(m, lambda: botones.al_pulsar(tbr._update(q), None))
    finally:
        db.pool = guardado
        restaurar()
    assert [a[0] for a, k in q.respuestas if a] == [f"No pude deshacerlo: {YA_RECUPERADO}"]
    assert q.editado == {}
    assert tnp._correr(m, lambda: crud.deshacer_varias([log_id])) == (0, [f"#{log_id}: {YA_RECUPERADO}"])
    # la Papelera de la página: su propia puerta (`NadaQueRestaurar`), con la MISMA frase
    e = tnp._rechazo(m, lambda: crud.deshacer_borrado("tareas", 1))
    assert isinstance(e, crud.NadaQueRestaurar) and e.clave == "no_esta_borrado" and str(e) == YA_RECUPERADO, e
    e = tnp._rechazo(m, lambda: crud.deshacer_borrado("tareas", 999))
    assert isinstance(e, crud.NadaQueRestaurar) and str(e) == YA_NO_EXISTE, e
    assert m.foto_del_log() == antes


def test_deshacer_de_un_crear_sobre_algo_que_ya_estaba_en_la_papelera_no_dice_que_revirtio():
    m = Mundo()
    m.fila("tareas", 1, borrada=True)
    log_id = m.huella("crear", "tareas", 1)
    antes = m.foto_del_log()
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError) and str(e) == "No lo deshice: eso ya estaba en la papelera."
    assert m.foto_del_log() == antes


# ── La fila desaparece ENTRE la lectura y el UPDATE de la rama `editar` (lo que solo ve el rowcount) ────
def _que_el_update_no_toque(monkeypatch, *, borrar_la_fila):
    original = tnp._Cur._emular_deshacer

    def _emular(self, sql, params):
        r = original(self, sql, params)
        if r is None:
            return None
        if borrar_la_fila:
            self._b.con.execute("DELETE FROM tareas WHERE id = 1")
        # el UPDATE emulado ya corrió; lo que se le devuelve a `deshacer` es uno que tocó 0 filas
        return self._b.con.execute("UPDATE tareas SET detalle = 'x' WHERE id = -1")
    monkeypatch.setattr(tnp._Cur, "_emular_deshacer", _emular)


def test_G1b_si_el_update_de_editar_no_toca_ninguna_fila_porque_se_borro_en_medio_no_afirma_nada(monkeypatch):
    m = Mundo()
    m.fila("tareas")
    log_id = m.huella("editar", "tareas", 1)
    antes = m.foto_del_log()
    _que_el_update_no_toque(monkeypatch, borrar_la_fila=True)
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError) and str(e) == YA_NO_EXISTE, e
    assert m.foto_del_log() == antes


def test_G1c_si_el_update_de_editar_no_toca_nada_y_la_fila_sigue_ahi_tampoco_afirma_nada(monkeypatch):
    m = Mundo()
    m.fila("tareas")
    log_id = m.huella("editar", "tareas", 1)
    antes = m.foto_del_log()
    _que_el_update_no_toque(monkeypatch, borrar_la_fila=False)
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError) and str(e) == "No lo deshice: la fila no cambió.", e
    assert m.foto_del_log() == antes


def test_G1d_la_descripcion_con_candado_dice_lo_suyo_si_sigue_ahi_y_lo_de_la_fila_si_no(monkeypatch):
    # candado: el UPDATE escribe solo si la descripción sigue valiendo lo mirado
    m = Mundo()
    m.con.execute("INSERT INTO proyectos (id, nombre, area, descripcion) VALUES (1, 'Casa', 'IA', 'nueva')")
    c = m.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) "
        "VALUES ('lucy', 'editar', 'proyectos', 1, ?, ?)",
        (json.dumps({"id": 1, "descripcion": "vieja"}), json.dumps({"descripcion": "nueva"})))
    original = tnp._Cur._emular_deshacer

    def _emular(self, sql, params):
        if original(self, sql, params) is None:
            return None
        return self._b.con.execute("UPDATE proyectos SET descripcion = descripcion WHERE id = -1")
    monkeypatch.setattr(tnp._Cur, "_emular_deshacer", _emular)
    antes = m.foto_del_log()
    e = _rechazo(m, c.lastrowid)
    assert isinstance(e, ValueError) and str(e) == crud.DESHACER_PISARIA_LA_DESCRIPCION, e
    assert m.foto_del_log() == antes


# ── G3: lo que `TABLAS` nombra se saca de lo real; una tabla inventada que se agregue también cae ──────
@pytest.mark.parametrize("accion", RAMAS)
def test_G3_una_tabla_inventada_agregada_a_TABLAS_tambien_responde_la_verdad(monkeypatch, accion):
    monkeypatch.setattr(crud, "TABLAS", (*crud.TABLAS, "cosas"))
    m = Mundo(extra=("cosas",))
    log_id = m.huella(accion, "cosas", 999)
    antes = m.foto_del_log()
    e = _rechazo(m, log_id)
    assert isinstance(e, ValueError) and str(e) == YA_NO_EXISTE, (accion, e)
    assert m.foto_del_log() == antes


# ── G4: los cuatro caminos que llaman a `deshacer` dicen lo mismo ─────────────────────────────────────────
def test_G4_el_agente_el_boton_las_varias_y_la_papelera_dicen_lo_mismo():
    m = Mundo()
    m.fila("proyectos", 1)
    log_id = m.huella("crear", "proyectos", 999)
    antes = m.foto_del_log()

    # herramienta «deshacer» del agente («deshaz»)
    r = tnp._correr(m, lambda: agente._ejecutar_herramienta("deshacer", {"accion": log_id}, 1, []))
    assert r == f"ERROR: {YA_NO_EXISTE}", r
    assert "revertí" not in r

    # botón de Telegram («und:<huella>»)
    restaurar = tbr._con_gente({tbr.DUENO: "Tiziano"})
    guardado = db.pool
    db.pool = m.pool()
    try:
        q = tbr._Q(f"und:{log_id}", tbr.DUENO)
        tnp._correr(m, lambda: botones.al_pulsar(tbr._update(q), None))
    finally:
        db.pool = guardado
        restaurar()
    alertas = [a[0] for a, k in q.respuestas if a]
    assert alertas == [f"No pude deshacerlo: {YA_NO_EXISTE}"], alertas
    assert q.editado == {}, "tachó la tarjeta con un «Deshecho» que no pasó"

    # deshacer varias (el botón «deshacer todo»)
    revertidas, fallos = tnp._correr(m, lambda: crud.deshacer_varias([log_id]))
    assert revertidas == 0 and fallos == [f"#{log_id}: {YA_NO_EXISTE}"], (revertidas, fallos)

    # la papelera de la página: la fila no está borrada → su propia puerta; borrada y con huella → la misma frase
    m.fila("tareas", 5, borrada=True)
    h = m.huella("borrar", "tareas", 5)
    m.con.execute("DELETE FROM tareas WHERE id = 5")       # la purga la borró entre mirar y restaurar
    antes2 = m.foto_del_log()
    assert h
    e = tnp._rechazo(m, lambda: crud.deshacer(h))
    assert str(e) == YA_NO_EXISTE
    assert m.foto_del_log() == antes2 == antes + [(h, "borrar", "tareas", 5)]


# ── Ninguna frase de «revertí …» / «recuperé …» sale de un sitio que no pasó por `deshacer` ───────────────
def test_toda_frase_de_reversion_sale_de_una_funcion_que_llama_a_deshacer():
    frases = ("revertí", "Revertí", "recuperé", "Recuperé", "Deshecho")
    culpables = []
    vistos = 0
    for ruta in sorted(_py_en_disco(_RAIZ)):              # LA puerta de los barridos del repo (no un rglob)
        rel = ruta.relative_to(_RAIZ)
        if rel.parts[0] == "tests":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # solo la función más cercana a cada texto: se compara el texto de las constantes DIRECTAS
            textos = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and any(f in n.value for f in frases)]
            if not textos:
                continue
            llama = any(isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "deshacer")
                or (isinstance(n.func, ast.Name) and n.func.id == "deshacer")) for n in ast.walk(fn))
            vistos += 1
            if not llama:
                culpables.append((str(rel), fn.name, textos[:1]))
    assert vistos >= 2, "el barrido no encontró ni el botón ni el agente: ya no mira lo real"
    # Las funciones que SOLO tienen el texto en un docstring o en un comentario de prosa no cuentan como culpables:
    # se descartan las que lo traen únicamente en su docstring.
    reales = []
    for rel, nombre, textos in culpables:
        arbol = ast.parse((_RAIZ / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == nombre)
        doc = ast.get_docstring(fn, clean=False) or ""
        resto = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value != doc and any(f in n.value for f in frases)]
        if resto:
            reales.append((rel, nombre, resto[:1]))
    assert not reales, f"dicen que revirtieron algo sin pasar por `deshacer`: {reales}"
