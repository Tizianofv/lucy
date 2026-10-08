"""Piezas compartidas por las pruebas de la entrada desde la App: el modelo de
la página de Proyectos (con tareas pendientes, hechas, comentarios y un
proyecto cerrado) y la base de mentira para pintarla por la ruta real."""
from __future__ import annotations

import contextlib
import re

import config
import db.db as db
import test_pagina_proyectos as tp
import web.app as panel


def modelo(con_personas: bool = True) -> dict:
    pr = [dict(id=1, nombre="Disco Uno", area="CDS", estado="activo", cliente_nombre="Cliente X"),
          dict(id=2, nombre="Cerrado Dos", area="CDS", estado="cerrado"),
          dict(id=3, nombre="Sin area", area=None),
          dict(id=4, nombre="Estado raro", area="CDS", estado="estado-que-nadie-conoce")]
    ta = [dict(id=10, titulo="Pendiente A", proyecto_id=1, vence_en=tp._dia(1), responsable_chat_id=424242),
          dict(id=11, titulo="Hecha B", proyecto_id=1, estado="hecha", completado_en=tp._dia(-1)),
          dict(id=12, titulo="Suelta C", area="CDS"),
          dict(id=13, titulo="Suelta sin grupo")]
    co = [dict(id=100, tarea_id=10, autor_chat_id=424242, creado_en=tp.CREADO,
               texto="hola comentario")]
    # Personas (de Noco) en un proyecto y en una tarea: sin ellas las macros
    # `ficha_persona` y `agregar_persona` de la plantilla no corren nunca y una
    # prueba sobre «lo que pinta» no vería la ✕ de quitar ni «Agregar».
    personas = [dict(id=500, nombre="Persona Uno", rol="productor", proyecto_id=1, tarea_id=None),
                dict(id=501, nombre="Persona Dos", rol="mezcla", proyecto_id=None, tarea_id=10),
                dict(id=502, nombre="Persona Tres", rol="edicion", proyecto_id=None, tarea_id=11)]
    # Las fechas (parte 4): el 1 las tiene todas, el 2 (cerrado) inicio y entrega, el resto no salió en la
    # lectura (`fechas_disponibles` falso). Sin esto la plantilla no dibuja el formulario de las fechas.
    fechas = {1: dict(inicio=tp.HOY.replace(day=1), entrega=tp.HOY.replace(day=28),
                      termina_cuando="Los másters entregados"),
              2: dict(inicio=tp.HOY.replace(day=1), entrega=tp.HOY.replace(day=20), termina_cuando=None)}
    # La carpeta (parte 5): el 1 es una dirección de Drive (se pinta como enlace), el 2 la ruta de una
    # computadora (texto con «Copiar»), el resto no salió en la lectura (`carpeta_disponible` falso).
    # Sin esto la plantilla no dibuja ni el enlace, ni el texto, ni el formulario de la carpeta.
    carpetas = {1: "https://drive.example.test/carpetas/disco-uno", 2: "/Users/estudio/Proyectos/Disco Dos"}
    # Las notas (parte 6): el 1 tiene tres (una del panel con autor conocido, una de Telegram que dice su
    # autor por la bandeja y una que no dice quién), el 2 (cerrado) una; el resto no salió en la lectura
    # (`notas_disponibles` falso en cada uno). Sin esto la plantilla no dibuja el bloque, ni los controles.
    notas = {1: [dict(id=700, proyecto_id=1, creado_en=tp.CREADO, contenido="Nota del panel del dueño",
                      autor_chat_id=config.CHAT_ID_DUENO, bandeja_chat_id=None, bandeja_origen=None),
                 dict(id=701, proyecto_id=1, creado_en=tp.CREADO, contenido="Nota de Telegram",
                      autor_chat_id=None, bandeja_chat_id=config.CHAT_ID_DUENO, bandeja_origen="telegram"),
                 dict(id=702, proyecto_id=1, creado_en=tp.CREADO, contenido="Nota sin autor conocido",
                      autor_chat_id=None, bandeja_chat_id=None, bandeja_origen=None)],
             2: [dict(id=703, proyecto_id=2, creado_en=tp.CREADO, contenido="Nota del cerrado",
                      autor_chat_id=config.CHAT_ID_DUENO, bandeja_chat_id=None, bandeja_origen=None)]}
    return _armar(pr, ta, co, personas if con_personas else [], fechas, carpetas, notas)


def _armar(pr, ta, co, personas, fechas=None, carpetas=None, notas=None):
    """`tp.modelo_de_filas` con participantes (esa función no los recibe): los mismos
    rellenos, y `db.armar_pagina` de verdad."""
    pr = [{"creado_en": tp.CREADO, "descripcion": None, "estado": "activo", "area": None,
           "responsable_chat_id": None, "cliente_nombre": None, **p} for p in pr]
    ta = [{"proyecto_id": None, "area": None, "vence_en": None, "completado_en": None,
           "creado_en": tp.CREADO, "responsable_chat_id": None, "estado": "pendiente", **t}
          for t in ta]
    return db.armar_pagina(list(tp.AREAS), pr, ta, [], list(co),
                           {424242: "Dueño", config.CHAT_ID_DUENO: "Dueño"}, tp.HOY,
                           participantes=personas, fechas=fechas, carpetas=carpetas, notas=notas)


class BaseQueNoSeToca:
    """Reemplaza `web.app.db`: lo que la página lee sale del modelo; CUALQUIER otra
    cosa que se le pida a la base es un fallo de la prueba (una ruta que debía
    cortar con 401 y llegó a la base)."""

    def __getattr__(self, nombre):
        if nombre == "pagina_de_proyectos":
            async def _pagina(hoy=None):
                return modelo()
            return _pagina
        if nombre == "contenido_para_borrar_grupo":
            # FINGIDO, declarado: lo que `db.contenido_para_borrar_grupo` mediría. Un grupo que
            # no está en `AREAS` da `None`; «CDS» (el del modelo, con proyectos y
            # tareas) da cuentas distintas de cero; los demás, vacíos.
            async def _contenido(clave):
                if clave not in {a["clave"] for a in tp.AREAS}:
                    return None
                ceros = dict.fromkeys(db.CLAVES_DE_CONTENIDO_DE_GRUPO, 0)
                if clave == "CDS":
                    ceros.update(proyectos_abiertos=2, proyectos_cerrados=1, tareas_pendientes=1)
                return {**ceros, "total": sum(ceros.values())}
            return _contenido
        if nombre == "contenido_de_proyecto":
            # FINGIDO, declarado: lo que `db.contenido_de_proyecto` mediría para el
            # proyecto 1 del modelo («Disco Uno», con una pendiente y una hecha).
            async def _de_proyecto(pid):
                if pid != 1:
                    return None
                return dict(id=1, nombre="Disco Uno", estado="activo", area="CDS",
                            tareas_pendientes=1, tareas_hechas=1, tareas_otras=0)
            return _de_proyecto
        if nombre == "aviso_de_proyecto_borrado":
            async def _aviso_proyecto(pid):         # FINGIDO: «está de verdad en la papelera»
                return {"nombre": "Disco Uno", "tareas": 2} if pid == 1 else None
            return _aviso_proyecto
        if nombre == "aviso_de_grupo_borrado":
            async def _aviso_grupo(clave):          # FINGIDO: «el grupo ya no está y su huella dice…»
                return {"proyectos": 1, "tareas": 2}
            return _aviso_grupo
        if nombre == "aviso_de_tarea":
            async def _aviso_tarea(tid, *, borrada):  # FINGIDO: «la tarea está en el estado que el aviso dice»
                return "Pendiente A" if tid == 10 else None
            return _aviso_tarea
        if nombre == "areas":
            async def _areas():
                return list(tp.AREAS)
            return _areas
        if nombre == "aviso_de_nota":
            async def _aviso_nota(nid, *, borrada):  # FINGIDO: «la nota 700 está en el estado que el aviso dice»
                return {"proyecto": "Disco Uno"} if (nid == 700 and borrada) else None
            return _aviso_nota
        if nombre == "tareas_con_filtro":
            # NO es un doble: es la función REAL y pura de `db` (no toca la base; solo recorre las filas
            # del modelo que ya se entregaron). Se deja pasar para que la prueba corra el filtro de verdad.
            return db.tareas_con_filtro
        if nombre in {n for n in dir(db) if n.isupper()}:    # constantes (largos, estados…)
            return getattr(db, nombre)
        raise AssertionError(f"la ruta tocó la base: db.{nombre}")


@contextlib.contextmanager
def pagina_sin_base():
    """`GET /proyectos` por la ruta real y la plantilla real, con la base y Noco
    de mentira."""
    guardado = panel.db, panel._buscar_personas_para_la_pagina

    async def _sin_busqueda(pq, pdonde):
        # Como la real: sin texto o con un «dónde» que no vale, nada; con ellos, las
        # coincidencias (de mentira) para que se dibujen los botones de elegir.
        if not pq.strip() or not re.fullmatch(r"cliente|proyecto|tarea-\d+", pdonde):
            return None
        return {"donde": pdonde, "q": pq.strip(), "error": "",
                "personas": [{"id": 900, "nombre": "Coincidencia Uno"}]}
    panel.db = BaseQueNoSeToca()
    panel._buscar_personas_para_la_pagina = _sin_busqueda
    try:
        yield
    finally:
        panel.db, panel._buscar_personas_para_la_pagina = guardado
