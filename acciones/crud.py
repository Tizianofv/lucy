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
import re
from datetime import datetime, timedelta

from psycopg.rows import dict_row

import db.db as db
from config import TZ

# Lista blanca. Los nombres de tabla se interpolan en el SQL (no se pueden
# parametrizar), así que nunca pueden venir de afuera sin pasar por acá.
TABLAS = ("tareas", "eventos", "notas", "movimientos")


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
) -> int:
    """Escribe la huella en log_acciones. Siempre dentro de la transacción.

    Devuelve el id de la huella: es el asa por la que después se agarra el
    deshacer. Sin ese número, "deshacé lo último" tendría que adivinar qué
    fue lo último.
    """
    cur = await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES ('lucy', %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
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
    return (await cur.fetchone())[0]


async def crear_desde_interpretacion(
    bandeja_id: int, r: dict, motivo: str | None = None
) -> tuple[str, int, int]:
    """Convierte una interpretación en una fila real.

    Devuelve (tabla, id, log_id). El log_id es lo que permite deshacerlo.
    Lanza FaltanDatos si el mensaje no alcanza para crear la entidad — pasa
    con una cita sin fecha o un gasto sin monto, que son columnas NOT NULL a
    propósito: una cita sin cuándo no es una cita.
    """
    clas = r.get("clasificacion")
    cuando = _fecha(r.get("cuando"))
    titulo = str(r.get("titulo") or "").strip()
    detalle = str(r.get("detalle") or "").strip() or None

    # Validar ANTES de abrir la conexión: si falta un dato no tiene sentido
    # ocupar una conexión del pool para terminar cancelando.
    if clas == "cita" and cuando is None:
        raise FaltanDatos("la fecha y la hora")
    if clas in ("gasto", "ingreso") and not r.get("monto"):
        raise FaltanDatos("el monto")
    if clas not in ("tarea", "cita", "nota", "idea", "gasto", "ingreso"):
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

        else:  # gasto | ingreso — misma tabla, lo distingue `tipo`
            tabla = "movimientos"
            # abs() a propósito: el monto se guarda siempre positivo y la
            # dirección la da `tipo`. Si el modelo devolviera -2300 para un
            # gasto, un monto negativo con tipo='gasto' sumaría al revés en
            # cualquier balance.
            cur = await conn.execute(
                """
                INSERT INTO movimientos
                  (bandeja_id, tipo, fecha, monto, moneda, contraparte,
                   referencia, persona_id, proyecto_id, notas)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, clas, (cuando or datetime.now(TZ)).date(),
                 abs(float(r["monto"])), str(r.get("moneda") or "DOP"),
                 str(r.get("contraparte") or r.get("lugar")
                     or r.get("persona") or "") or None,
                 str(r.get("referencia") or "") or None,
                 persona_id, proyecto_id, detalle),
            )

        registro_id = (await cur.fetchone())[0]
        log_id = await _registrar(
            conn,
            accion="crear",
            tabla=tabla,
            registro_id=registro_id,
            despues=r,
            motivo=motivo or f"Creado desde la bandeja #{bandeja_id}",
            bandeja_id=bandeja_id,
        )

    return tabla, registro_id, log_id


# Lo único que no se edita. Cambiar esto no habilitaría nada: rompería la
# trazabilidad (bandeja_id, creado_en) o la identidad de la fila (id). Es lista
# NEGRA y no blanca a propósito — todo lo demás es editable sin que haya que
# venir a autorizarlo campo por campo cada vez que Lucy aprenda algo nuevo.
NO_EDITABLES = {"id", "bandeja_id", "creado_en", "borrado_en"}

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")


def _adaptar(v):
    """Las fechas viajan como texto ISO en el JSON del modelo; Postgres las
    quiere como datetime para una columna timestamptz."""
    if isinstance(v, str) and _ISO.match(v):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


async def editar(
    tabla: str, registro_id: int, cambios: dict, motivo: str
) -> tuple[dict | None, int | None]:
    """Aplica cambios a una fila existente. Devuelve (después, log_id).

    Guarda el antes Y el después en el log: con eso, deshacer una edición es
    volver a escribir el 'antes', igual que con el borrado.
    """
    if tabla not in TABLAS:
        raise ValueError(f"Tabla no permitida: {tabla}")

    campos = {k: _adaptar(v) for k, v in cambios.items() if k not in NO_EDITABLES}
    if not campos:
        raise ValueError("No hay nada que cambiar.")

    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT * FROM {tabla} WHERE id = %s AND borrado_en IS NULL",
            (registro_id,),
        )
        antes = await cur.fetchone()
        if antes is None:
            return None, None

        # Los nombres de columna se interpolan (no se pueden parametrizar), así
        # que se validan contra las columnas reales de la fila que acabamos de
        # leer. Nada que no exista en la tabla llega al UPDATE.
        desconocidas = set(campos) - set(antes)
        if desconocidas:
            raise ValueError(f"Esa tabla no tiene: {', '.join(sorted(desconocidas))}")

        asignaciones = ", ".join(f"{c} = %s" for c in campos)
        await conn.execute(
            f"UPDATE {tabla} SET {asignaciones} WHERE id = %s",
            (*campos.values(), registro_id),
        )
        await cur.execute(f"SELECT * FROM {tabla} WHERE id = %s", (registro_id,))
        despues = await cur.fetchone()

        log_id = await _registrar(
            conn, accion="editar", tabla=tabla, registro_id=registro_id,
            antes=antes, despues=despues, motivo=motivo,
            bandeja_id=antes.get("bandeja_id"),
        )
    return despues, log_id


async def borrar(tabla: str, registro_id: int, motivo: str) -> int | None:
    """Soft-delete: marca borrado_en y guarda el 'antes' completo en el log.

    Devuelve el log_id, o None si no había nada que borrar. Ese 'antes' ES el
    deshacer: restaurar la fila es volver a escribir lo que quedó guardado
    ahí. Por eso nunca hay DELETE de verdad.
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
            return None  # no existe o ya estaba borrada

        await conn.execute(
            f"UPDATE {tabla} SET borrado_en = now() WHERE id = %s", (registro_id,)
        )
        return await _registrar(
            conn,
            accion="borrar",
            tabla=tabla,
            registro_id=registro_id,
            antes=antes,
            motivo=motivo,
            bandeja_id=antes.get("bandeja_id"),
        )


async def deshacer(log_id: int) -> str:
    """Revierte una acción registrada. Devuelve una frase de qué se revirtió.

    Es lo que permite que Lucy actúe sin preguntar: equivocarse deja de ser
    caro. Preguntar antes cuesta un toque SIEMPRE; deshacer cuesta un toque
    solo cuando se equivocó — y se equivoca poco.

    Para revertir una edición se usa jsonb_populate_record, que le deja a
    Postgres la conversión de tipos. Reescribir a mano un timestamptz o un
    numeric desde el JSON del log sería reinventar —mal— algo que la base ya
    hace bien.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT accion, tabla, registro_id, antes FROM log_acciones WHERE id = %s",
            (log_id,),
        )
        huella = await cur.fetchone()
        if huella is None:
            raise ValueError("No encuentro esa acción en el registro.")

        tabla, registro_id = huella["tabla"], huella["registro_id"]
        if tabla not in TABLAS:
            raise ValueError(f"No sé deshacer cambios en {tabla}.")

        if huella["accion"] == "crear":
            await conn.execute(
                f"UPDATE {tabla} SET borrado_en = now() "
                f"WHERE id = %s AND borrado_en IS NULL", (registro_id,))
            que = "lo que había creado"

        elif huella["accion"] == "borrar":
            await conn.execute(
                f"UPDATE {tabla} SET borrado_en = NULL WHERE id = %s", (registro_id,))
            que = "lo que había archivado"

        elif huella["accion"] == "editar":
            antes = huella["antes"] or {}
            columnas = [c for c in antes if c not in NO_EDITABLES]
            if not columnas:
                raise ValueError("Esa edición no guardó con qué volver atrás.")
            asignaciones = ", ".join(f"{c} = r.{c}" for c in columnas)
            await conn.execute(
                f"UPDATE {tabla} t SET {asignaciones} "
                f"FROM jsonb_populate_record(null::{tabla}, %s) r WHERE t.id = %s",
                (json.dumps(antes, default=str, ensure_ascii=False), registro_id))
            que = "el cambio"

        else:
            raise ValueError(f"No sé deshacer una acción de tipo '{huella['accion']}'.")

        # El deshacer también se registra: la historia no se reescribe, se
        # agrega. Si no, el log mentiría diciendo que aquello nunca pasó.
        await _registrar(
            conn, accion="deshacer", tabla=tabla, registro_id=registro_id,
            motivo=f"Tiziano deshizo la acción #{log_id} ({huella['accion']})",
        )
    return que
