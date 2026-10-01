# -*- coding: utf-8 -*-
"""La lista de herramientas que se le ofrece al modelo cuando pide una que no
existe (`cerebro/agente.py::HERRAMIENTAS_QUE_HAY`).

Es una LISTA TECLEADA, y ésa es exactamente la clase de cosa que se separa de
la realidad: hasta el 1-oct-2026 el mensaje decía catorce nombres y `pasos`
—que existía y funcionaba— no estaba entre ellos, así que un modelo que se
equivocara de nombre leía una lista donde faltaba una herramienta de verdad.

Lo que la sostiene es la guarda de abajo: la lista no se compara contra otra
lista escrita a mano, se compara contra EL DESPACHADOR. Los nombres salen del
árbol sintáctico de `_ejecutar_herramienta`, buscando cada `if nombre ==
"<literal>"`, que es como se atiende cada herramienta. Una herramienta nueva
—o una que se borre— se pone roja hasta que las dos cosas digan lo mismo.

Correr:  <intérprete> -m pytest tests/test_herramientas.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tarea_a_mano as base  # noqa: E402  (deja `psycopg` falseado)

import cerebro.agente as agente  # noqa: E402


# Las DOS funciones que atienden herramientas, y no una: `atender` se queda con
# tres —`panel`, `preguntar` y `responder`, que terminan el turno— y le pasa el
# resto a `_ejecutar_herramienta`. Mirar solo la segunda dejaría esas tres fuera
# de la comparación, y el mensaje al modelo sí las nombra.
_DESPACHADORES = ("atender", "_ejecutar_herramienta")


def _las_que_atiende_el_despachador() -> set:
    """Los nombres que el agente atiende, por el árbol sintáctico: cada
    `if nombre == "<literal>"` de las dos funciones que despachan.

    FRONTERA DICHA: ve la forma `if nombre == "<literal>"` (y el `elif`, que
    el AST trata igual). NO ve una herramienta despachada por un diccionario de
    funciones, un `match`, ni un nombre que llegue en una variable. En este
    archivo hoy no hay ninguna de esas formas.
    """
    arbol = ast.parse(pathlib.Path(agente.__file__).read_text(encoding="utf-8"))
    salida = set()
    for fn in ast.walk(arbol):
        if not (isinstance(fn, ast.AsyncFunctionDef)
                and fn.name in _DESPACHADORES):
            continue
        salida |= {
            n.test.comparators[0].value
            for n in ast.walk(fn)
            if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Name) and n.test.left.id == "nombre"
            and isinstance(n.test.comparators[0], ast.Constant)
            and isinstance(n.test.comparators[0].value, str)}
    return salida


def test_la_lista_de_herramientas_es_exactamente_la_que_el_despachador_atiende():
    atendidas = _las_que_atiende_el_despachador()
    assert len(atendidas) > 10, (
        f"el AST no encontró las herramientas: dejó de ver ({sorted(atendidas)})")
    assert set(agente.HERRAMIENTAS_QUE_HAY) == atendidas, (
        "la lista que se le muestra al modelo y las herramientas que el "
        "despachador atiende se separaron:\n"
        f"  sólo en la lista: {sorted(set(agente.HERRAMIENTAS_QUE_HAY) - atendidas)}\n"
        f"  sólo en el despachador: {sorted(atendidas - set(agente.HERRAMIENTAS_QUE_HAY))}")


def test_no_hay_nombres_repetidos_en_la_lista():
    assert len(agente.HERRAMIENTAS_QUE_HAY) == len(set(agente.HERRAMIENTAS_QUE_HAY))


def test_el_mensaje_de_herramienta_inexistente_nombra_las_que_hay():
    """El mensaje se arma de la lista, no de otro texto tecleado aparte."""
    acciones: list = []

    import asyncio

    import db.db as db
    guardado = db.pool
    db.pool = None
    try:
        bucle = asyncio.new_event_loop()
        try:
            r = bucle.run_until_complete(agente._ejecutar_herramienta(
                "inventada", {}, 1, acciones))
        finally:
            bucle.close()
    finally:
        db.pool = guardado

    assert "no existe la herramienta 'inventada'" in r, r
    for nombre in agente.HERRAMIENTAS_QUE_HAY:
        assert nombre in r, (nombre, r)
