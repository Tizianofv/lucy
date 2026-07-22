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
# personas y proyectos entraron con el perfil vivo (req 12): antes el agente
# no podía editarlos y el "perfil" era una tabla que nadie alimentaba.
TABLAS = ("tareas", "eventos", "notas", "movimientos", "personas", "proyectos",
          "lugares")


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
                  (bandeja_id, titulo, detalle, vence_en, recurrencia,
                   proyecto_id, persona_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, titulo, detalle, cuando,
                 str(r.get("recurrencia") or "").strip() or None,
                 proyecto_id, persona_id),
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

        # Posponer se cuenta solo (req 28): una tarea pendiente que se mueve
        # para MÁS TARDE es una posposición, lo diga Tiziano con esa palabra o
        # no. Esto es contabilidad del armario, no vigilancia del modelo: como
        # actualizado_en o el log, es la casa llevando sus propias cuentas
        # para que Lucy tenga el dato cuando lo quiera mirar. El try tapa un
        # caso real: si la fecha vino sin zona horaria, comparar aware con
        # naive lanza TypeError, y perder la edición entera por no poder
        # contar una posposición sería castigo desproporcionado.
        if (tabla == "tareas" and "pospuesta_veces" not in campos
                and isinstance(campos.get("vence_en"), datetime)
                and antes.get("vence_en") is not None
                and antes.get("estado") == "pendiente"
                and campos.get("estado", "pendiente") == "pendiente"):
            try:
                if campos["vence_en"] > antes["vence_en"]:
                    campos["pospuesta_veces"] = (antes.get("pospuesta_veces") or 0) + 1
            except TypeError:
                pass

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


async def perfil(
    tipo: str,
    nombre: str,
    *,
    alias: list[str] | None = None,
    relacion: str | None = None,
    nota: str | None = None,
    descripcion: str | None = None,
    bandeja_id: int | None = None,
) -> tuple[str, int | None]:
    """El perfil vivo (req 12): lo que Lucy sabe de la gente y los proyectos.

    Devuelve (resultado_para_el_agente, log_id|None).

    Es ACUMULATIVO a propósito: los alias se suman, las notas se agregan con
    fecha, nada se pisa. "Rosi es mi hermana" en enero y "a Rosi no llamarla
    antes de las 10" en marzo tienen que convivir — un perfil que se
    sobreescribe es un perfil que olvida, y olvidar es lo único que un
    asistente no se puede permitir. Lo único que se reemplaza es `relacion`,
    porque es un dato de estado, no una historia.
    """
    tipo = (tipo or "").strip().lower()
    nombre = (nombre or "").strip()
    if tipo not in ("persona", "proyecto"):
        raise ValueError(f"'{tipo}' no es persona ni proyecto.")
    if not nombre:
        raise ValueError("Sin nombre no hay perfil.")

    tabla = "personas" if tipo == "persona" else "proyectos"
    hoy = datetime.now(TZ).strftime("%d/%m/%Y")
    linea = f"· [{hoy}] {nota.strip()}" if nota and nota.strip() else None

    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if tabla == "personas":
            await cur.execute(
                """
                SELECT * FROM personas
                 WHERE borrado_en IS NULL
                   AND (lower(nombre) = lower(%s)
                        OR lower(%s) = ANY(SELECT lower(a) FROM unnest(alias) a))
                 LIMIT 1
                """,
                (nombre, nombre),
            )
        else:
            await cur.execute(
                "SELECT * FROM proyectos "
                "WHERE borrado_en IS NULL AND lower(nombre) = lower(%s) LIMIT 1",
                (nombre,),
            )
        fila = await cur.fetchone()

        # ── No existía: nace con lo que se sepa hoy ──────────────────────
        if fila is None:
            if tabla == "personas":
                cur = await conn.execute(
                    """INSERT INTO personas (nombre, alias, relacion, notas)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (nombre, [a.strip() for a in (alias or []) if a.strip()],
                     (relacion or "").strip() or None, linea),
                )
            else:
                cur = await conn.execute(
                    """INSERT INTO proyectos (nombre, descripcion)
                       VALUES (%s, %s) RETURNING id""",
                    (nombre, (descripcion or "").strip() or linea),
                )
            rid = (await cur.fetchone())[0]
            log_id = await _registrar(
                conn, accion="crear", tabla=tabla, registro_id=rid,
                despues={"nombre": nombre, "alias": alias, "relacion": relacion,
                         "nota": nota, "descripcion": descripcion},
                motivo=f"Perfil: Tiziano contó algo de {nombre}",
                bandeja_id=bandeja_id,
            )
            return f"OK: {tipo} '{nombre}' creado en el perfil (#{rid}).", log_id

    # ── Existía: se acumula (editar() registra antes/después y es reversible) ─
    cambios: dict = {}
    if alias:
        nuevos = [a.strip() for a in alias if a.strip()]
        viejos = fila.get("alias") or []
        union = viejos + [a for a in nuevos
                          if a.lower() not in {v.lower() for v in viejos}]
        if union != viejos:
            cambios["alias"] = union
    if relacion and relacion.strip():
        if (fila.get("relacion") or "").strip().lower() != relacion.strip().lower():
            cambios["relacion"] = relacion.strip()
    if descripcion and descripcion.strip() and tabla == "proyectos":
        cambios["descripcion"] = descripcion.strip()
    if linea:
        campo = "notas" if tabla == "personas" else "descripcion"
        previo = fila.get(campo)
        if campo not in cambios:
            cambios[campo] = f"{previo}\n{linea}" if previo else linea
        else:
            cambios[campo] = f"{cambios[campo]}\n{linea}"

    if not cambios:
        return f"OK: eso ya lo sabía de '{fila['nombre']}'.", None

    _, log_id = await editar(
        tabla, fila["id"], cambios,
        motivo=f"Perfil: Tiziano contó algo de {fila['nombre']}",
    )
    return (f"OK: perfil de '{fila['nombre']}' actualizado "
            f"({', '.join(cambios)}).", log_id)


async def guardar_lugar(
    nombre: str,
    lat: float | None = None,
    lon: float | None = None,
    radio_m: int | None = None,
) -> tuple[str, int | None]:
    """Nombra un lugar del mundo de Tiziano ("CDS", "el estudio", "casa").

    Sin coordenadas usa la última ubicación compartida: el gesto natural es
    "estoy en el estudio" + un pin, o el pin primero y el nombre después.
    Si el lugar ya existía, actualiza sus coordenadas (se mudó, o el pin
    viejo era malo) — el log guarda el antes, como siempre.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("Sin nombre no hay lugar.")

    if lat is None or lon is None:
        u = await db.ultima_ubicacion()
        if u is None:
            raise ValueError(
                "no tengo ninguna ubicación suya; pedile que comparta un pin.")
        lat, lon = u["lat"], u["lon"]

    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT * FROM lugares WHERE borrado_en IS NULL "
            "AND lower(nombre) = lower(%s) LIMIT 1", (nombre,))
        fila = await cur.fetchone()

    if fila is not None:
        cambios: dict = {"lat": lat, "lon": lon}
        if radio_m:
            cambios["radio_m"] = int(radio_m)
        _, log_id = await editar(
            "lugares", fila["id"], cambios,
            motivo=f"Lugar '{fila['nombre']}' reubicado")
        return f"OK: lugar '{fila['nombre']}' actualizado.", log_id

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO lugares (nombre, lat, lon, radio_m)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (nombre, lat, lon, int(radio_m or 300)))
        rid = (await cur.fetchone())[0]
        log_id = await _registrar(
            conn, accion="crear", tabla="lugares", registro_id=rid,
            despues={"nombre": nombre, "lat": lat, "lon": lon},
            motivo=f"Lugar nuevo: {nombre}")
    return f"OK: lugar '{nombre}' guardado (#{rid}).", log_id


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
