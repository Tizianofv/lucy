"""Piezas compartidas por las pruebas de la entrada desde la App: el modelo de
la página de Proyectos (con tareas pendientes, hechas, comentarios y un
proyecto cerrado) y la base de mentira para pintarla por la ruta real."""
from __future__ import annotations

import contextlib

import db.db as db
import test_pagina_proyectos as tp
import web.app as panel


def modelo() -> dict:
    pr = [dict(id=1, nombre="Disco Uno", area="CDS", estado="activo", cliente_nombre="Cliente X"),
          dict(id=2, nombre="Cerrado Dos", area="CDS", estado="cerrado"),
          dict(id=3, nombre="Sin area", area=None)]
    ta = [dict(id=10, titulo="Pendiente A", proyecto_id=1, vence_en=tp._dia(1)),
          dict(id=11, titulo="Hecha B", proyecto_id=1, estado="hecha", completado_en=tp._dia(-1)),
          dict(id=12, titulo="Suelta C", area="CDS"),
          dict(id=13, titulo="Suelta sin grupo")]
    co = [dict(id=100, tarea_id=10, autor_chat_id=424242, creado_en=tp.CREADO,
               texto="hola comentario")]
    return tp.modelo_de_filas(pr, ta, comentarios=co, nombres={424242: "Dueño"})


class BaseQueNoSeToca:
    """Reemplaza `web.app.db`: lo que la página lee sale del modelo; CUALQUIER otra
    cosa que se le pida a la base es un fallo de la prueba (una ruta que debía
    cortar con 401 y llegó a la base)."""

    def __getattr__(self, nombre):
        if nombre == "pagina_de_proyectos":
            async def _pagina(hoy=None):
                return modelo()
            return _pagina
        if nombre == "areas":
            async def _areas():
                return list(tp.AREAS)
            return _areas
        if nombre in {n for n in dir(db) if n.isupper()}:    # constantes (largos, estados…)
            return getattr(db, nombre)
        raise AssertionError(f"la ruta tocó la base: db.{nombre}")


@contextlib.contextmanager
def pagina_sin_base():
    """`GET /proyectos` por la ruta real y la plantilla real, con la base y Noco
    de mentira."""
    guardado = panel.db, panel._buscar_personas_para_la_pagina

    async def _sin_busqueda(pq, pdonde):
        return None
    panel.db = BaseQueNoSeToca()
    panel._buscar_personas_para_la_pagina = _sin_busqueda
    try:
        yield
    finally:
        panel.db, panel._buscar_personas_para_la_pagina = guardado
