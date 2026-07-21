"""CRUD sobre las entidades (tareas, eventos, notas, gastos).

Dos reglas que TODA operación respeta — son pilares, no opcionales:
  · Borrar = marcar borrado_en (soft-delete). Nunca DELETE real. → reversibilidad
  · Toda operación escribe una fila en log_acciones con antes/después.
    → auditoría + autoexplicación + el "deshacer" sale gratis de ahí.

La entidad y su registro en log_acciones se escriben en la MISMA transacción.
Una fila creada sin rastro en el log sería exactamente el agujero que el log
existe para tapar: si se separaran, un fallo entre medio dejaría a Lucy sin
poder explicar de dónde salió algo que ella misma creó.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from psycopg.rows import dict_row

import db.db as db
from config import TZ

# Lista blanca. Los nombres de tabla se interpolan en el SQL (no se pueden
# parametrizar), así que nunca pueden venir de afuera sin pasar por acá.
TABLAS = ("tareas", "eventos", "notas", "gastos")


class FaltanDatos(Exception):
    """No se puede crear la entidad porque falta un dato obligatorio.

    No es un fallo de Lucy: es que el mensaje no traía la información. Se le
    dice a Tiziano qué falta, en vez de inventarlo o de tragarse el mensaje.
    """


def _fecha(iso: str | None) -> datetime | None:
    """ISO 8601 → datetime. None si viene vacío o ilegible."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


async def _registrar(
    conn,
    *,
    accion: str,
    tabla: str,
    registro_id: int,
    antes: dict | None = None,
    despues: dict | None = None,
    motivo: str | None = None,
    bandeja_id: int | None = None,
) -> None:
    """Escribe la huella en log_acciones. Siempre dentro de la transacción."""
    await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES ('lucy', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            accion,
            tabla,
            registro_id,
            json.dumps(antes, default=str, ensure_ascii=False) if antes else None,
            json.dumps(despues, default=str, ensure_ascii=False) if despues else None,
            motivo,
            bandeja_id,
        ),
    )


async def crear_desde_interpretacion(bandeja_id: int, r: dict) -> tuple[str, int]:
    """Convierte una interpretación confirmada en una fila real.

    Devuelve (tabla, id). Lanza FaltanDatos si el mensaje no alcanza para
    crear la entidad — pasa con una cita sin fecha o un gasto sin monto, que
    son columnas NOT NULL a propósito: una cita sin cuándo no es una cita.
    """
    clas = r.get("clasificacion")
    cuando = _fecha(r.get("cuando"))
    titulo = str(r.get("titulo") or "").strip()
    detalle = str(r.get("detalle") or "").strip() or None

    # Validar ANTES de abrir la conexión: si falta un dato no tiene sentido
    # ocupar una conexión del pool para terminar cancelando.
    if clas == "cita" and cuando is None:
        raise FaltanDatos("la fecha y la hora")
    if clas == "gasto" and not r.get("monto"):
        raise FaltanDatos("el monto")
    if clas not in ("tarea", "cita", "nota", "idea", "gasto"):
        raise ValueError(f"'{clas}' no crea ninguna entidad.")

    # Personas y proyectos se resuelven fuera de la transacción a propósito:
    # crear una persona de más es inofensivo y reutilizable, mientras que
    # meterlo adentro alargaría la transacción de la entidad sin ganar nada.
    persona_id = await db.buscar_o_crear_persona(str(r.get("persona") or ""))
    proyecto_id = await db.buscar_o_crear_proyecto(str(r.get("proyecto") or ""))

    async with db.pool.connection() as conn:
        if clas == "tarea":
            tabla = "tareas"
            cur = await conn.execute(
                """
                INSERT INTO tareas
                  (bandeja_id, titulo, detalle, vence_en, proyecto_id, persona_id)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, titulo, detalle, cuando, proyecto_id, persona_id),
            )

        elif clas == "cita":
            tabla = "eventos"
            dur = int(r.get("duracion_min") or 0)
            termina = cuando + timedelta(minutes=dur) if dur > 0 else None
            cur = await conn.execute(
                """
                INSERT INTO eventos
                  (bandeja_id, titulo, inicia_en, termina_en, lugar,
                   persona_id, proyecto_id, notas)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, titulo, cuando, termina, str(r.get("lugar") or "") or None,
                 persona_id, proyecto_id, detalle),
            )

        elif clas in ("nota", "idea"):
            tabla = "notas"
            # La idea vive en `notas` con etiqueta: es una nota con intención,
            # no una entidad distinta. Una tabla más solo para ideas sería
            # duplicar estructura para ganar una palabra.
            contenido = f"{titulo}\n\n{detalle}" if detalle else titulo
            cur = await conn.execute(
                """
                INSERT INTO notas
                  (bandeja_id, contenido, etiquetas, proyecto_id, persona_id)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, contenido, ["idea"] if clas == "idea" else [],
                 proyecto_id, persona_id),
            )

        else:  # gasto
            tabla = "gastos"
            cur = await conn.execute(
                """
                INSERT INTO gastos
                  (bandeja_id, fecha, monto, moneda, comercio, notas)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, (cuando or datetime.now(TZ)).date(), r["monto"],
                 str(r.get("moneda") or "DOP"), str(r.get("lugar") or "") or None,
                 detalle),
            )

        registro_id = (await cur.fetchone())[0]
        await _registrar(
            conn,
            accion="crear",
            tabla=tabla,
            registro_id=registro_id,
            despues=r,
            motivo=f"Confirmado por Tiziano desde la bandeja #{bandeja_id}",
            bandeja_id=bandeja_id,
        )

    return tabla, registro_id


async def borrar(tabla: str, registro_id: int, motivo: str) -> bool:
    """Soft-delete: marca borrado_en y guarda el 'antes' completo en el log.

    Ese 'antes' ES el deshacer: restaurar la fila es volver a escribir lo que
    quedó guardado ahí. Por eso nunca hay DELETE de verdad.
    """
    if tabla not in TABLAS:
        raise ValueError(f"Tabla no permitida: {tabla}")

    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT * FROM {tabla} WHERE id = %s AND borrado_en IS NULL",
            (registro_id,),
        )
        antes = await cur.fetchone()
        if antes is None:
            return False  # no existe o ya estaba borrada

        await conn.execute(
            f"UPDATE {tabla} SET borrado_en = now() WHERE id = %s", (registro_id,)
        )
        await _registrar(
            conn,
            accion="borrar",
            tabla=tabla,
            registro_id=registro_id,
            antes=antes,
            motivo=motivo,
            bandeja_id=antes.get("bandeja_id"),
        )
    return True
