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
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from psycopg.rows import dict_row

import config
import db.db as db
from config import TZ

# Lista blanca. Los nombres de tabla se interpolan en el SQL (no se pueden
# parametrizar), así que nunca pueden venir de afuera sin pasar por acá.
# personas y proyectos entraron con el perfil vivo (req 12): antes el agente
# no podía editarlos y el "perfil" era una tabla que nadie alimentaba.
TABLAS = ("tareas", "eventos", "notas", "movimientos", "personas", "proyectos",
          "lugares", "preferencias")


class FaltanDatos(Exception):
    """No se puede crear la entidad porque falta un dato obligatorio.

    No es un fallo de Lucy: es que el mensaje no traía la información. Se le
    dice a Tiziano qué falta, en vez de inventarlo o de tragarse el mensaje.
    """


def _monto_exacto(valor) -> Decimal:
    """El monto, en Decimal y siempre positivo. Nunca float.

    Este es el ÚNICO sitio del sistema donde el dinero pasaba por float: el
    camino automático (los correos del banco) ya usa Decimal de punta a punta, y
    este es el camino manual — cuando Tiziano le dice a Lucy "gasté 500".

    float no representa exactamente los decimales de base 10: 0.10 + 0.10 + 0.10
    da 0.30000000000000004. Postgres redondea al guardar en NUMERIC(12,2), así
    que hoy no se ve nada raro; el problema es que el error entra ANTES de
    guardar, y un día con la cifra equivocada nadie va a saber de dónde salió.
    Con Decimal no hay que confiar en que el redondeo tape nada.

    Positivo SIEMPRE: el signo lo da `tipo` (gasto | ingreso), como dice el
    esquema. Guardar el signo dos veces es cómo se termina restando un ingreso.
    """
    try:
        return abs(Decimal(str(valor)))
    except (InvalidOperation, ValueError, TypeError):
        # Que llegue basura acá es un fallo del clasificador, no del usuario, y
        # tragárselo como 0.00 sería anotar un gasto de cero pesos que nadie
        # entendería después.
        raise ValueError(f"monto no numérico: {valor!r}")


def _fecha(iso: str | None) -> datetime | None:
    """ISO 8601 → datetime. None si viene vacío o ilegible."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _anticipos(v, *, vacio_es_silencio: bool = False) -> list[int]:
    """Normaliza los minutos-antes de aviso: garantiza el 0, dedupe, ordena.

    El 0 (la campanada a la hora exacta) SIEMPRE está: es el default y el ancla
    del recordatorio. Ausente, vacío o ilegible → [0], que es un solo aviso a
    la hora. Se ordena de mayor a menor —el anticipado primero, la hora al
    final: [30, 0]— por legibilidad; el despertador no depende del orden (usa
    `@>`, contención de conjuntos). Negativos y basura se descartan en silencio:
    un anticipo mal formado no puede robarle a la fila su aviso a la hora.

    `vacio_es_silencio=True` (lo usa `editar`) es la ÚNICA excepción al 0: una
    lista vacía explícita se respeta como "esta fila no avisa nunca". Eso es lo
    que significa '{}' desde el 13-ago-2026 —así entran los eventos espejados
    de Google Calendar, que Google ya recuerda por su cuenta— y también es la
    forma de apagar un aviso a mano. Al CREAR no aplica y no debe aplicar: ahí
    "vacío" es "no me dijeron nada", que es el default, y el default suena.
    """
    if vacio_es_silencio and isinstance(v, (list, tuple, set, frozenset)) and not v:
        return []
    # Un escalar suelto es UN anticipo, no una lista. Sin esto, un "30" que
    # viniera sin corchetes se leería carácter por carácter y daría [3, 0]:
    # basura silenciosa en vez del anticipo que pidieron.
    if isinstance(v, (int, float, str)):
        v = [v]
    nums: set[int] = set()
    for x in (v or []):
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            nums.add(n)
    nums.add(0)
    return sorted(nums, reverse=True)


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
    actor: str = "lucy",
) -> int:
    """Escribe la huella en log_acciones. Siempre dentro de la transacción.

    Devuelve el id de la huella: es el asa por la que después se agarra el
    deshacer. Sin ese número, "deshacé lo último" tendría que adivinar qué
    fue lo último.

    `actor` es 'lucy' por omisión -- todo lo que ya llamaba a esta función es
    Telegram/`crud`, y eso no cambia. `web/app.py` (encargo 5, cambiar el área
    de una tarea o un proyecto desde el panel) es el único llamador que manda
    `actor='panel'` explícito, reusando `editar()` entero -- validación por
    `_area_que_vale` incluida -- en vez de tener una segunda escritura.
    """
    cur = await conn.execute(
        """
        INSERT INTO log_acciones
          (actor, accion, tabla, registro_id, antes, despues, motivo, bandeja_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            actor,
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


async def _duplicado_pendiente(
    conn, tabla: str, titulo: str, cuando: datetime | None
) -> tuple[int, int | None] | None:
    """Busca una fila viva y pendiente igual a la que se va a crear.

    Devuelve (id, log_id_de_creación) si ya existe una, o None si no hay.
    Es el corazón de la deduplicación: el agente a veces re-crea lo que
    acaba de crear (misma tarea, misma cita) y sin este freno llegan
    recordatorios repetidos —era exactamente el pendiente de los avisos
    dobles—.

    La coincidencia se acota a título + fecha a propósito. Dos tareas
    homónimas en fechas distintas —"pagar la luz" este mes y el que viene,
    una recurrencia— son dos tareas legítimas, no un duplicado: recortar por
    la sola coincidencia de título las fusionaría y perderíamos una.

    Se devuelve el log de la creación original (no uno nuevo): así el asa de
    "deshacer" sigue apuntando a la fila real, y no se ensucia el log con una
    huella de algo que en verdad no se creó.
    """
    if tabla == "tareas":
        # IS NOT DISTINCT FROM: una tarea sin fecha (vence_en NULL) coincide
        # con otra sin fecha. Con `=`, NULL nunca iguala a NULL y se colarían
        # duplicados de tareas sin cuándo, que son las más fáciles de repetir.
        cur = await conn.execute(
            """
            SELECT id FROM tareas
            WHERE borrado_en IS NULL
              AND estado = 'pendiente'
              AND titulo = %s
              AND vence_en IS NOT DISTINCT FROM %s
            ORDER BY id DESC LIMIT 1
            """,
            (titulo, cuando),
        )
    else:  # eventos — no tienen `estado`; "pendiente" = vivo (no borrado)
        cur = await conn.execute(
            """
            SELECT id FROM eventos
            WHERE borrado_en IS NULL
              AND titulo = %s
              AND inicia_en = %s
            ORDER BY id DESC LIMIT 1
            """,
            (titulo, cuando),
        )
    row = await cur.fetchone()
    if row is None:
        return None
    registro_id = row[0]

    cur = await conn.execute(
        """
        SELECT id FROM log_acciones
        WHERE tabla = %s AND registro_id = %s AND accion = 'crear'
        ORDER BY id DESC LIMIT 1
        """,
        (tabla, registro_id),
    )
    log_row = await cur.fetchone()
    return registro_id, (log_row[0] if log_row else None)


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

    # EL RESPONSABLE, SOLO PARA TAREAS, por la MISMA puerta que usa `editar()`
    # para cambiarlo — `_por_las_puertas` con `PUERTAS["tareas"]`, y no una
    # copia del criterio. Poner el responsable al crear es lo que pidió
    # Tiziano en el encargo 2 del diseño: "crea X para Rosi" en un solo paso,
    # en vez de crear y después editar.
    #
    # Sin dato (el caso normal) `_responsable_que_vale` devuelve None sin
    # validar nada: "cuando no se dice responsable, todo queda igual que
    # hoy" es la propia lógica de la puerta, no algo que se decida acá.
    #
    # Si lo pedido no vale, esto CORTA LA CREACIÓN ENTERA — no crea la tarea
    # sin responsable como si no se hubiera pedido nada, que sería peor:
    # "para Rosi" se perdería en silencio y nadie más que Rosi lo notaría.
    #
    # Solo se mira si `clas == "tarea"`: en cualquier otra clasificación
    # "responsable_chat_id" no es una columna que se vaya a escribir, y
    # validarlo igual rechazaría una cita o un gasto por un dato que ni
    # siquiera se va a usar.
    responsable_chat_id = None
    if clas == "tarea":
        try:
            responsable_chat_id = _por_las_puertas(
                "tareas", {"responsable_chat_id": r.get("responsable_chat_id")}
            )["responsable_chat_id"]
        except ValueError as e:
            raise ValueError(f"No creé la tarea: {e}.") from e

    # Personas y proyectos se resuelven fuera de la transacción a propósito:
    # crear una persona de más es inofensivo y reutilizable, mientras que
    # meterlo adentro alargaría la transacción de la entidad sin ganar nada.
    persona_id = await db.buscar_o_crear_persona(str(r.get("persona") or ""))
    proyecto_id = await db.buscar_o_crear_proyecto(
        str(r.get("proyecto") or ""), bandeja_id=bandeja_id)

    # EL ÁREA, SOLO PARA TAREAS (encargo 4), por la MISMA puerta que usa
    # `editar()` para cambiarla — `_area_que_vale`, y no una copia del
    # criterio (hallazgo del testigo sobre `e94b37a`: la primera versión de
    # esto validaba distinto que `editar`, que no validaba nada). Valida
    # contra `db.areas()` con el mismo tipo de mensaje que ya usa la
    # categoría de un movimiento — vocabulario cerrado, comparado en
    # código, como las categorías (`db/schema.sql:273` no tiene ni FK ni
    # CHECK en `categoria`; acá SÍ hay FK/CHECK, pero se valida IGUAL en
    # código para dar el mismo tipo de mensaje explicando cuáles hay, en
    # vez del texto crudo de psycopg). Y si la tarea tiene proyecto, el área
    # pedida se IGNORA en silencio -- ni se mira, ni se rechaza: hereda la
    # del proyecto, que es la decisión de Tiziano (ver el docstring de
    # `_area_que_vale`, corregido en la segunda vuelta tras el NO PASA sobre
    # `2d8451c`).
    if clas == "tarea":
        try:
            area_tarea = await _area_que_vale(
                r.get("area"), tabla="tareas", proyecto_id=proyecto_id)
        except ValueError as e:
            raise ValueError(f"No creé la tarea: {e}.") from e
    else:
        area_tarea = None

    async with db.pool.connection() as conn:
        if clas == "tarea":
            tabla = "tareas"
            ya = await _duplicado_pendiente(conn, tabla, titulo, cuando)
            if ya is not None:
                # El agente la re-pidió; ya existía. No se crea otra — pero
                # el responsable de ESTE mensaje no se puede perder callado
                # solo porque la tarea ya estaba. Hallazgo del testigo sobre
                # eeb07dd: `responsable_chat_id` se validaba (líneas de
                # arriba) y se tiraba entero acá, sin error, sin log, sin
                # aviso — Tiziano recibía "OK" creyendo que había quedado
                # asignada.
                #
                # Tres casos, y los tres se deciden con la fila de VERDAD
                # (no con lo que se pidió, que puede estar desactualizado):
                log_id = ya[1]
                if responsable_chat_id is not None:
                    # UNA sola columna, no `SELECT *`: es lo único que hace
                    # falta para decidir, y `antes`/`despues` de la huella no
                    # necesitan más — `deshacer()` (más abajo, rama 'editar')
                    # arma el UPDATE con `jsonb_populate_record` y solo aplica
                    # las columnas que la huella trae, así que una huella
                    # PARCIAL con únicamente `responsable_chat_id` deshace
                    # exactamente esto y nada más, ni de más ni de menos.
                    cur_actual = await conn.execute(
                        "SELECT responsable_chat_id FROM tareas WHERE id = %s",
                        (ya[0],))
                    fila_actual = await cur_actual.fetchone()
                    actual = fila_actual[0] if fila_actual else None
                    if actual == responsable_chat_id:
                        pass  # ya tiene el mismo: nada que hacer, nada que avisar.
                    elif actual is None:
                        # No tenía: se le pone, por la MISMA puerta que ya
                        # validó `responsable_chat_id` arriba, y con una
                        # huella accion='editar' — la misma forma que dejaría
                        # un `editar()` de verdad — para que el deshacer y la
                        # auditoría no distingan un camino del otro.
                        await conn.execute(
                            "UPDATE tareas SET responsable_chat_id = %s "
                            "WHERE id = %s",
                            (responsable_chat_id, ya[0]))
                        # EL ASA DEL DESHACER PASA A SER ESTA EDICIÓN, no la
                        # creación original. Si se quedara con `ya[1]`,
                        # "deshacé eso" después de "ya le puse Rosi" borraría
                        # (archivaría) la tarea entera en vez de solo quitarle
                        # el responsable que se le acaba de poner — mucho más
                        # destructivo que lo que se pidió deshacer.
                        log_id = await _registrar(
                            conn, accion="editar", tabla="tareas",
                            registro_id=ya[0],
                            antes={"responsable_chat_id": actual},
                            despues={"responsable_chat_id": responsable_chat_id},
                            motivo=motivo or (
                                "Responsable puesto al re-crear desde la "
                                f"bandeja #{bandeja_id} (la tarea ya "
                                "existía)"),
                            bandeja_id=bandeja_id,
                        )
                    else:
                        # Ya tenía OTRO: no se pisa sin decirlo. Se corta acá
                        # con el motivo — "ERROR: ..." es información para el
                        # modelo, no un fallo (mismo trato que un responsable
                        # que no vale, más arriba) — y por nombre, nunca por
                        # número: Tiziano descartó enseñar el chat.
                        nombres = config.NOMBRES_POR_CHAT
                        quien = nombres.get(
                            actual, "alguien que no tiene nombre puesto")
                        raise ValueError(
                            f"No creé la tarea: ya existía («{titulo}», "
                            f"#{ya[0]}) y la tiene {quien}. No la "
                            "reasigné — si hay que cambiarla, decímelo "
                            "explícito.")
                return tabla, ya[0], log_id
            # DOS FORMAS DEL INSERT (encargo 4), igual que
            # `db.crear_tarea_desde_el_panel`: `con_area` trae la columna
            # `area`, y si `tareas.area` todavía no existe (la migración no
            # se aplicó: SQLSTATE 42703) se cae a `sin_area` -- la de
            # siempre -- dentro de un SAVEPOINT para que el fallo no deje
            # abortada la transacción completa (que también tiene que
            # escribir `log_acciones` después). Lucy puede seguir creando
            # tareas ANTES de que la sala aplique la migración.
            params_base = (
                bandeja_id, titulo, detalle, cuando,
                str(r.get("recurrencia") or "").strip() or None,
                proyecto_id, persona_id, _anticipos(r.get("anticipos_min")),
                responsable_chat_id)
            con_area = """
                INSERT INTO tareas
                  (bandeja_id, titulo, detalle, vence_en, recurrencia,
                   proyecto_id, persona_id, anticipos_min,
                   responsable_chat_id, area)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """
            sin_area = """
                INSERT INTO tareas
                  (bandeja_id, titulo, detalle, vence_en, recurrencia,
                   proyecto_id, persona_id, anticipos_min, responsable_chat_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """
            try:
                async with conn.transaction():
                    cur = await conn.execute(
                        con_area, params_base + (area_tarea,))
            except Exception as e:
                try:
                    sqlstate = e.sqlstate
                except AttributeError:
                    raise e from None
                if sqlstate != "42703":
                    raise
                cur = await conn.execute(sin_area, params_base)

        elif clas == "cita":
            tabla = "eventos"
            ya = await _duplicado_pendiente(conn, tabla, titulo, cuando)
            if ya is not None:
                return tabla, ya[0], ya[1]
            dur = int(r.get("duracion_min") or 0)
            termina = cuando + timedelta(minutes=dur) if dur > 0 else None
            cur = await conn.execute(
                """
                INSERT INTO eventos
                  (bandeja_id, titulo, inicia_en, termina_en, lugar,
                   persona_id, proyecto_id, notas, anticipos_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, titulo, cuando, termina, str(r.get("lugar") or "") or None,
                 persona_id, proyecto_id, detalle,
                 _anticipos(r.get("anticipos_min"))),
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

            # ¿Ya lo trajo el correo del banco? El camino automático calcula
            # una huella y ON CONFLICT lo frena; este camino no tiene huella,
            # así que sin esta comprobación el agente puede anotar de nuevo un
            # movimiento que la ingesta ya registró.
            #
            # Pasó el 1-sep: procesando "dame todas las tareas pendientes",
            # Lucy anotó una transferencia de RD$18,280 que el correo de
            # Banreservas ya había guardado. Los dos con el mismo número de
            # referencia y el mismo día, escritos distinto — "WENDY MARISOL
            # CANELA CRUZ" contra "ROSILIS ... → WENDY MARISOL CANELA CRUZ" —
            # así que ninguna comparación de texto los hubiera juntado.
            #
            # Se compara por fecha, monto y moneda, que es lo que ninguna de
            # las dos versiones puede escribir distinto. NO se crea nada: se
            # devuelve el que ya está, para que el agente se lo diga en vez de
            # duplicar en silencio. Dos gastos iguales el mismo día existen,
            # pero es mucho más raro que este caso, y equivocarse acá cuesta
            # una pregunta — mientras que duplicar cuesta un total falso.
            gemelo = await conn.execute(
                """
                SELECT id FROM movimientos
                 WHERE borrado_en IS NULL
                   AND fecha = %s AND monto = %s AND moneda = %s
                   AND hash_contenido IS NOT NULL
                 ORDER BY id DESC LIMIT 1
                """,
                ((cuando or datetime.now(TZ)).date(), _monto_exacto(r["monto"]),
                 str(r.get("moneda") or "DOP")),
            )
            fila_gemela = await gemelo.fetchone()
            if fila_gemela:
                raise FaltanDatos(
                    f"Ese movimiento ya está: el correo del banco lo registró "
                    f"como M-{fila_gemela[0]:04d} (mismo día, mismo monto). No "
                    "lo anoté otra vez. Si de verdad son dos gastos distintos, "
                    "decímelo y lo agrego.")
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
                 _monto_exacto(r["monto"]), str(r.get("moneda") or "DOP"),
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


async def guardar_preferencia(
    bandeja_id: int, texto: str, contexto: str | None = None
) -> tuple[int, int]:
    """Guarda una regla de comportamiento que Lucy aprendió. Devuelve (id, log_id).

    Es 'crear' a los ojos del log, así que el deshacer genérico la revierte
    igual que a una tarea: soft-delete por borrado_en. Sin trato especial.
    """
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO preferencias (texto, contexto) VALUES (%s, %s) RETURNING id",
            (texto.strip(), (contexto or "").strip() or None),
        )
        pid = (await cur.fetchone())[0]
        log_id = await _registrar(
            conn, accion="crear", tabla="preferencias", registro_id=pid,
            despues={"texto": texto, "contexto": contexto},
            motivo=f"Preferencia aprendida: {texto}", bandeja_id=bandeja_id,
        )
    return pid, log_id


async def olvidar_preferencia(bandeja_id: int, pref_id: int) -> int | None:
    """Da de baja una preferencia (soft-delete). Devuelve el log_id, o None si no estaba.

    Acción 'borrar' a los ojos del log: el deshacer la revive poniendo
    borrado_en = NULL. Reversibilidad sin escribir nada nuevo.
    """
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE preferencias SET borrado_en = now() "
            "WHERE id = %s AND borrado_en IS NULL RETURNING texto",
            (pref_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return await _registrar(
            conn, accion="borrar", tabla="preferencias", registro_id=pref_id,
            antes={"texto": row[0]},
            motivo=f"Preferencia olvidada: {row[0]}", bandeja_id=bandeja_id,
        )


# Lo único que no se edita. Cambiar esto no habilitaría nada: rompería la
# trazabilidad (bandeja_id, creado_en) o la identidad de la fila (id). Es lista
# NEGRA y no blanca a propósito — todo lo demás es editable sin que haya que
# venir a autorizarlo campo por campo cada vez que Lucy aprenda algo nuevo.
NO_EDITABLES = {"id", "bandeja_id", "creado_en", "borrado_en"}

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")


# ── LAS COLUMNAS CON PUERTA ──────────────────────────────────────────────
#
# `editar` y `deshacer` son escritores GENÉRICOS: el nombre de la columna que
# escriben no está en ningún texto de este archivo. Sale de los datos —los
# `cambios` que manda el modelo, o el `antes` de una huella— y se arma al vuelo
# en el SQL. Por eso ninguno de los dos puede «acordarse» de la puerta de una
# columna concreta: no la nombran nunca.
#
# Así que la decisión vive UNA vez, acá, y los dos pasan por `_por_las_puertas`
# con los valores que están a punto de escribir. Se mira el VALOR que va a
# quedar, no cómo se escribió la llamada.
#
# Hoy hay una sola columna con puerta: quién tiene pendiente una tarea. Lo
# decidió Tiziano el 10-sep-2026: Lucy puede asignar responsable por Telegram,
# pero solo a quien entra al panel, igual que el panel, y un chat que no vale se
# rechaza. La puerta es la misma que usa el panel, `config.puede_ser_responsable`.
#
# Por Telegram se pide con el NOMBRE («la tarea 40 es de Rosi»), no con el
# número: el modelo no tiene los números y no debe tenerlos. El nombre se
# convierte en chat ACÁ, antes de la puerta, y el chat que sale pasa por la
# misma puerta que un número escrito a mano. Nombre y número son dos formas de
# decir QUIÉN; la puerta es la única que decide SI puede.

# Qué texto es un chat NO se decide acá: `config.chat_escrito`, la misma que
# lee el desplegable del panel. Una sola lectura para los dos caminos, por el
# mismo motivo que una sola puerta — ver su docstring, que trae la regla y la
# cicatriz del 11-sep-2026.


def _clave_de_nombre(texto: str) -> str:
    """El nombre sin lo que no distingue a una persona de otra.

    Se quitan las mayúsculas, las tildes y los espacios de más, y nada más.
    «rosi», «ROSI», « Rosí » y «Rosi» son la misma clave: por Telegram nadie
    escribe con tilde ni con mayúscula, y ninguna de esas diferencias puede
    señalar a otra persona. Si dos entradas de la variable dan la misma clave,
    no se desempata con esto: ver `_chat_del_nombre`.
    """
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto)
                         if not unicodedata.combining(c))
    return " ".join(sin_tildes.casefold().split())


def _chat_del_nombre(texto: str):
    """(chat, None) si ese nombre es de UNA persona; (None, motivo) si no.

    La lista sale de `config.NOMBRES_POR_CHAT` en cada llamada, no de nada
    tecleado: la tercera persona que Tiziano agregue a la variable se encuentra
    sin tocar código. Se busca entre TODOS los que tienen nombre, también los
    que no entran al panel, a propósito: a esos no los rechaza esta función sino
    la puerta, y así hay una sola que decide.

    LO QUE NO SE ADIVINA, y por qué:
      · Un apodo o un pedazo de nombre («la flaca», «Ros»). La variable no
        tiene apodos, y la tabla `personas` sí los tiene pero no está atada a
        ningún chat: tomar un alias de ahí sería suponer que la Rosi del perfil
        es la del chat. Se rechaza, y el rechazo dice los nombres que sí hay
        para que el modelo pregunte.
      · Dos entradas con la misma clave («Ana» y «ána» en dos chats). Elegir
        una por orden sería adivinar, y el orden de la variable no significa
        nada (ver `config._leer_nombres`). Tampoco se desempata por quién entra
        al panel: el nombre tiene que decir QUIÉN antes de preguntar si PUEDE,
        o se le asigna la tarea a la otra persona sin que nadie lo note.
    """
    clave = _clave_de_nombre(texto)
    chats = [chat for chat, nombre in config.NOMBRES_POR_CHAT.items()
             if _clave_de_nombre(nombre) == clave]
    if len(chats) == 1:
        return chats[0], None
    if chats:
        return None, "ese nombre es de más de una persona y no elijo"
    return None, "ese nombre no es de nadie de la casa"


def _responsable_que_vale(valor):
    """El chat que va a quedar como responsable, o ValueError explicando por qué no.

    Sin responsable es lo normal y no pasa por la puerta: no hay a quién
    validar. Llega como None o como texto vacío.

    QUIÉN, de tres formas, y las tres terminan en la MISMA puerta:
      · un número —el JSON del modelo no promete si viene como número o como
        texto con cifras—, y se guarda como número. De texto lo lee
        `config.chat_escrito`, que acepta UNA sola escritura por persona y por
        eso lo pedido y lo escrito no pueden decir cifras distintas;
      · un nombre, que se busca en la variable (ver `_chat_del_nombre`);
      · cualquier otra cosa no dice quién. `True` no es un chat aunque Python
        lo compare igual a 1. Un número escrito de cualquier otra manera
        —`0700…`, `+700…`— tampoco es un chat: se lee como nombre, no es de
        nadie, y el rechazo dice a quién sí se le puede asignar.

    El mensaje dice quién SÍ puede, por nombre y nunca por número: lo lee el
    modelo y puede terminar en un aviso de Telegram. Y NO repite lo que se
    pidió: el que pidió ya lo sabe, y lo pedido puede traer un número.
    """
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    chat, motivo = None, "eso no dice quién"
    if isinstance(valor, int) and not isinstance(valor, bool):
        chat, motivo = valor, "ese chat no puede ser responsable"
    elif isinstance(valor, str):
        # Se le pregunta UNA vez y con eso se decide por cuál de las dos ramas
        # va: si el texto es un chat, es un chat; si no, es un nombre. No hay
        # una tercera lectura en ningún lado.
        chat = config.chat_escrito(valor)
        if chat is not None:
            motivo = "ese chat no puede ser responsable"
        else:
            chat, motivo = _chat_del_nombre(valor)
            motivo = motivo or "esa persona no puede ser responsable"
    if chat is not None and config.puede_ser_responsable(chat):
        return chat
    nombres = [nombre for _, nombre in config.personas_del_panel()]
    quienes = (f"hoy: {', '.join(nombres)}" if nombres
               else "hoy nadie: falta NOMBRES_POR_CHAT")
    # Corto a propósito: el botón de Telegram recorta el aviso, y lo que se
    # pierde primero es el final. Por eso la razón va delante y los nombres
    # detrás.
    raise ValueError(
        f"{motivo}: solo quien entra al panel y tiene nombre ({quienes})")


async def _area_que_vale(valor, *, tabla: str, proyecto_id=None):
    """El área que va a quedar en `tareas` o `proyectos`, o ValueError
    explicando por qué no. LA ÚNICA función que decide esto: la usan
    `crear_desde_interpretacion` (al crear una tarea) y `editar` (tareas Y
    proyectos, por Telegram) — dos criterios que hoy coincidieran habrían
    empezado a separarse el primer día que alguien tocara uno solo. NO es
    la puerta de `PUERTAS`/`_por_las_puertas` (esa es sync y de un solo
    valor); acá hace falta `db.areas()` (async) y, para una tarea, el
    proyecto que va a quedar — dos cosas que esa forma no puede cargar.

    `proyecto_id` es el que la TAREA tiene o va a tener DESPUÉS de esta
    escritura — None para un proyecto (ahí no aplica) o para una tarea sin
    proyecto.

    SEGUNDA VUELTA (NO PASA del testigo sobre `2d8451c`): con proyecto
    puesto, el área queda SIEMPRE en `None` — SIN MIRAR `valor` y SIN
    RECHAZAR NADA. Es la decisión de Tiziano tal cual la dijo: "la tarea
    con proyecto HEREDA el área del proyecto. Cuando una tarea pasa a tener
    proyecto, su área propia se limpia sola, no se rechaza el pedido ni se
    le pide a nadie que lo haga en dos pasos." Así que "poné esta tarea en
    el proyecto X" nunca falla por un área vieja que quedó pisada, y un
    `editar` que mande `area` Y `proyecto_id` juntos tampoco — el área
    pedida se descarta en silencio, porque de todos modos es irrelevante:
    la que cuenta sale del proyecto (`COALESCE(p.area, t.area)` en
    `db.tareas_por_grupo`). La primera versión de esta función RECHAZABA
    este caso (commit `726f967`) — Tiziano lo corrigió: eso obligaba a un
    "editar en dos pasos" que él no pidió.

    Sin proyecto (`proyecto_id is None`): sin dato (`valor` es `None` o
    vacío) no pasa por la puerta — no hay nada que validar, y es el estado
    normal de una tarea a la que le sacaron el proyecto: se queda sin área
    hasta que alguien le ponga una, no hereda ninguna vieja. Con dato, se
    valida contra `db.areas()` — vocabulario cerrado, el MISMO patrón que
    ya usa la categoría de un movimiento (`editar`, más abajo): se compara
    un valor contra la tabla, no se juzga cómo está redactada una frase, así
    que esto NO es una guarda sobre lo que escribe el modelo. Si no está en
    la lista, se rechaza con el mismo tipo de mensaje —"no es un área,
    son: ..."—, igual que un responsable que no vale corta la escritura
    entera: acá SÍ importa que no se ignore en silencio, porque no hay
    ningún otro sitio (como el proyecto) de donde el área pudiera salir.
    """
    if tabla == "tareas" and proyecto_id is not None:
        return None
    valor = (valor or "").strip() or None
    if valor is None:
        return None
    validas = {a["clave"] for a in await db.areas()}
    if valor not in validas:
        lista = (", ".join(f'"{c}"' for c in sorted(validas)) if validas
                 else "(ninguna declarada todavía)")
        raise ValueError(f"'{valor}' no es un área. Son: {lista}")
    return valor


PUERTAS = {"tareas": {"responsable_chat_id": _responsable_que_vale}}


def _por_las_puertas(tabla: str, valores: dict) -> dict:
    """Los `valores` que van a escribirse, después de pasar por su puerta.

    Devuelve una copia con los valores de las columnas con puerta ya
    normalizados, o lanza ValueError si alguno no vale. Las columnas sin puerta
    salen tal cual: para ellas esto no cambia nada.
    """
    salida = dict(valores)
    for columna, puerta in PUERTAS.get(tabla, {}).items():
        if columna in salida:
            salida[columna] = puerta(salida[columna])
    return salida


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
    tabla: str, registro_id: int, cambios: dict, motivo: str,
    *, actor: str = "lucy",
) -> tuple[dict | None, int | None]:
    """Aplica cambios a una fila existente. Devuelve (después, log_id).

    Guarda el antes Y el después en el log: con eso, deshacer una edición es
    volver a escribir el 'antes', igual que con el borrado.

    `actor` es 'lucy' por omisión -- todos los llamadores de siempre son
    Telegram. `web/app.py` (encargo 5) es el único que manda `actor='panel'`,
    para cambiar el área de una tarea o un proyecto DESDE EL PANEL
    reutilizando esta misma función entera -- la MISMA puerta
    (`_area_que_vale`), la misma huella, el mismo deshacer -- en vez de una
    segunda escritura con su propio criterio.
    """
    if tabla not in TABLAS:
        raise ValueError(f"Tabla no permitida: {tabla}")

    campos = {k: _adaptar(v) for k, v in cambios.items() if k not in NO_EDITABLES}
    if not campos:
        raise ValueError("No hay nada que cambiar.")

    # El invariante del 0 vale por ACÁ TAMBIÉN (13-ago-2026). `crear` normaliza
    # con _anticipos desde el principio; `editar` no lo hacía, y era el camino
    # más transitado: "recordámelo 30 minutos antes" sobre algo que YA existe
    # es una edición, no una creación. Llegaba {"anticipos_min": [30]} y se
    # guardaba tal cual — esa fila avisaba 30' antes y NUNCA a la hora, que es
    # justo la campanada que el helper existe para garantizar. Se encontró así
    # en producción (tarea #69, con avisos_enviados={30}: la anticipada sonó, la
    # de la hora no sonó nunca).
    #   Se toca SOLO esta columna, por nombre: `editar` es genérico para ocho
    # tablas y no tiene por qué saber nada del resto. Y con
    # vacio_es_silencio=True, porque acá una lista VACÍA es una decisión, no un
    # olvido: convertirla en [0] volvería a encender los eventos espejados de
    # Google que se apagaron esta misma mañana.
    if "anticipos_min" in campos:
        campos["anticipos_min"] = _anticipos(
            campos["anticipos_min"], vacio_es_silencio=True)

    # La categoría es vocabulario CERRADO, y tiene que serlo por los dos
    # caminos. El panel ya la valida; sin esto, corregir por Telegram podía
    # meter "supermercado" en minúscula o "Súper" y partir el total en dos para
    # siempre. Un vocabulario que solo se respeta en una de las dos puertas no
    # es un vocabulario cerrado.
    if tabla == "movimientos" and "categoria" in campos:
        from cerebro.bancos.categorias import CATEGORIAS
        valor = (campos["categoria"] or "").strip() or None
        if valor is not None and valor not in CATEGORIAS:
            raise ValueError(
                f"'{valor}' no es una categoría. Son: {', '.join(CATEGORIAS)}")
        campos["categoria"] = valor
        # Si además NO le corresponde a este tipo de movimiento, se rechaza más
        # abajo, cuando ya se leyó la fila y se sabe si es gasto o ingreso.

    # Las columnas con puerta, ANTES de abrir la conexión y con la misma forma
    # que la categoría de arriba: si lo pedido no vale, se rechaza la edición
    # entera con el motivo, en vez de escribir la mitad. Ver `PUERTAS`.
    try:
        campos = _por_las_puertas(tabla, campos)
    except ValueError as e:
        raise ValueError(f"No cambié nada: {e}.") from e

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
        # (Si la migración del encargo 4 no se aplicó, `tareas`/`proyectos` no
        # tienen la columna `area` todavía, así que un `{"area": ...}` cae en
        # el `raise` de arriba, con un motivo claro -- no hace falta ningún
        # SAVEPOINT de tolerancia en `editar`: el `SELECT *` de más arriba ya
        # dice qué columnas existen de verdad.)

        # EL ÁREA (encargo 4), en TAREAS y en PROYECTOS, por la MISMA puerta
        # que usa `crear_desde_interpretacion` — `_area_que_vale`, ni una
        # validación aparte ni ninguna. Hallazgo del testigo sobre `e94b37a`:
        # esta función no comprobaba nada, así que "área inventada" y "tarea
        # con proyecto + área" pasaban derecho hasta la FK/CHECK de Postgres,
        # que revienta con su texto crudo (ver el `except Exception` de
        # `cerebro/agente.py::_ejecutar_herramienta`).
        #
        # SEGUNDA VUELTA (NO PASA sobre `2d8451c`): la primera versión solo
        # entraba acá si `"area" in campos`. `editar("tareas", id,
        # {"proyecto_id": 5}, ...)` — "poné esta tarea en el proyecto X",
        # SIN tocar el área — no pasaba por ningún lado: la fila quedaba con
        # `proyecto_id` puesto Y el área vieja intacta al mismo tiempo, un
        # estado que solo el CHECK de la base atajaba, con su texto crudo.
        # Ahora entra si se toca CUALQUIERA de las dos columnas, porque las
        # dos deciden la misma pregunta: "¿con qué área y con qué proyecto
        # va a quedar esta tarea DESPUÉS de este UPDATE?" -- un solo cálculo,
        # no una rama nueva en paralelo para "solo cambió el proyecto".
        #
        # EL PROYECTO QUE CUENTA es el que la tarea va a tener DESPUÉS de
        # este UPDATE: lo pedido en estos MISMOS `cambios` si está, si no el
        # que ya tenía. Con proyecto puesto, `_area_que_vale` devuelve
        # `None` SIEMPRE -- ni mira lo que se pidió en "area", ni rechaza
        # nada: la tarea hereda el área del proyecto (decisión de Tiziano).
        # Sin proyecto (se lo quitaron, o nunca lo tuvo), el área pedida se
        # valida contra `db.areas()` como siempre, y si no viene ninguna se
        # queda con la que ya tenía.
        #
        # SOLO SE ESCRIBE SI DE VERDAD CAMBIA (`area_final != antes.get`)
        # salvo que "area" se haya pedido EXPLÍCITAMENTE -- ahí se escribe
        # siempre, aunque el valor final sea el mismo que ya tenía, porque
        # `desconocidas` (más arriba) ya garantizó que la columna existe si
        # alguien la nombró a propósito. Sin eso, un `editar` que solo toca
        # `proyecto_id` en una base donde la columna `area` todavía no
        # existe intentaría escribir una columna que no está, y reventaría
        # con 42703 por una tarea que ni siquiera tenía área que limpiar.
        if tabla == "tareas" and ("area" in campos or "proyecto_id" in campos):
            area_pedida_explicita = "area" in campos
            proyecto_final = campos.get("proyecto_id", antes.get("proyecto_id"))
            area_pedida = campos.get("area", antes.get("area"))
            try:
                area_final = await _area_que_vale(
                    area_pedida, tabla="tareas", proyecto_id=proyecto_final)
            except ValueError as e:
                raise ValueError(f"No cambié nada: {e}.") from e
            if area_pedida_explicita or area_final != antes.get("area"):
                campos["area"] = area_final
        elif tabla == "proyectos" and "area" in campos:
            try:
                campos["area"] = await _area_que_vale(
                    campos["area"], tabla="proyectos")
            except ValueError as e:
                raise ValueError(f"No cambié nada: {e}.") from e

        # Un ingreso no lleva categoría: los rubros dicen EN QUÉ se gastó. La
        # única excepción es la marca "No suma".
        #
        # SE COMPRUEBA EL PAR QUE VA A QUEDAR, no solo lo que se está tocando.
        # La primera versión miraba esto solo si "categoria" venía en los
        # cambios, así que editar únicamente `tipo` —"el M-86 en realidad es un
        # ingreso"— se saltaba las dos validaciones y dejaba un ingreso con
        # categoría "Restaurantes". Lo encontró el testigo; es exactamente el
        # estado que esta regla existe para impedir.
        if tabla == "movimientos" and ("categoria" in campos or "tipo" in campos):
            from cerebro.bancos.categorias import categoria_permitida
            tipo_final = campos.get("tipo", antes.get("tipo"))
            cat_final = campos.get("categoria", antes.get("categoria"))
            if not categoria_permitida(tipo_final, cat_final):
                if "categoria" in campos:
                    # La pidió explícitamente y no corresponde: es un error, no
                    # una consecuencia. Se rechaza en vez de arreglarlo a medias.
                    raise ValueError(
                        f"Un movimiento de tipo '{tipo_final}' no lleva "
                        f"categoría '{cat_final}'. Los ingresos solo se pueden "
                        "marcar como 'No suma'.")
                # Cambió el TIPO y el rubro dejó de tener sentido. No es pérdida
                # de dato: ese rubro solo significaba algo mientras era gasto, y
                # el valor viejo queda en log_acciones, así que se puede deshacer.
                # Reventar acá obligaría a Tiziano a dar dos órdenes para decir
                # una sola cosa.
                campos["categoria"] = None

        # Posponer se cuenta solo (req 28): una tarea pendiente que se mueve
        # para MÁS TARDE es una posposición, lo diga Tiziano con esa palabra o
        # no. Esto es contabilidad del armario, no vigilancia del modelo: como
        # actualizado_en o el log, es la casa llevando sus propias cuentas
        # para que Lucy tenga el dato cuando lo quiera mirar. El try tapa un
        # caso real: si la fecha vino sin zona horaria, comparar aware con
        # naive lanza TypeError, y perder la edición entera por no poder
        # contar una posposición sería castigo desproporcionado.
        #   QUÉ ES POSPONER NO SE DECIDE ACÁ desde el 13-sep-2026: lo decide
        # `db.cuenta_como_posposicion`, la misma función que usa el panel
        # (`db.mover_vence`). Tiziano pidió que mover para más tarde desde el
        # panel cuente igual que por el chat, y dos copias del criterio se
        # separan.
        if (tabla == "tareas" and "pospuesta_veces" not in campos
                and "vence_en" in campos
                and db.cuenta_como_posposicion(
                    antes.get("estado"), antes.get("vence_en"),
                    campos.get("estado", antes.get("estado")),
                    campos["vence_en"])):
            campos["pospuesta_veces"] = (antes.get("pospuesta_veces") or 0) + 1

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
            bandeja_id=antes.get("bandeja_id"), actor=actor,
        )

    # Y APRENDE, igual que el panel. Sin esto había dos puertas que hacían
    # cosas distintas: corregir "SM NACIONAL" en la pantalla enseñaba para
    # siempre, y corregir el mismo comercio por Telegram arreglaba una fila y
    # nada más — la próxima compra volvía a la cola. Dos caminos que dan
    # resultados distintos para la misma corrección es cómo se pierde la
    # confianza en los dos.
    from cerebro.bancos.categorias import se_aprende as _se_aprende
    if (tabla == "movimientos" and "categoria" in campos
            and _se_aprende(campos["categoria"]) and antes.get("contraparte")):
        from cerebro.bancos.categorias import normalizar_comercio
        await db.aprender_categoria(
            normalizar_comercio(antes["contraparte"]), campos["categoria"])

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

    Las coordenadas son obligatorias y salen de buscar_lugar. Antes se podían
    omitir y se tomaba la última ubicación compartida por Telegram, pero ese
    rastreo se eliminó el 30-ago: sin él, adivinar el punto sería inventarlo.
    Si el lugar ya existía, actualiza sus coordenadas (se mudó, o las viejas
    estaban mal) — el log guarda el antes, como siempre.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("Sin nombre no hay lugar.")

    if lat is None or lon is None:
        raise ValueError(
            "las coordenadas del lugar; buscalas con buscar_lugar y pasá "
            "lat/lon.")

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
            "SELECT accion, tabla, registro_id, antes, despues "
            "FROM log_acciones WHERE id = %s",
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
            despues = huella.get("despues") or {}
            con_puerta = PUERTAS.get(tabla, {})
            # Una columna CON PUERTA vuelve atrás solo si ESTA edición la
            # cambió, o sea si el `despues` de la huella la trae con otro valor.
            # Sin esto, deshacer un cambio de título le devolvía a la tarea el
            # responsable que tenía en ese momento, pisando el que alguien le
            # puso después y sin preguntarle a la puerta. Si la huella no dice
            # qué quedó, no se sabe si la cambió, y entonces no se toca.
            #   Las columnas SIN puerta siguen como siempre: vuelven todas las
            # del `antes`, también las que esta edición no tocó. Eso vale para
            # todas las tablas y no se decidió en este cambio.
            columnas = [c for c in antes if c not in NO_EDITABLES
                        and (c not in con_puerta
                             or (c in despues and despues[c] != antes[c]))]
            if not columnas:
                raise ValueError("Esa edición no guardó con qué volver atrás.")
            try:
                _por_las_puertas(tabla, {c: antes[c] for c in columnas})
            except ValueError as e:
                raise ValueError(
                    f"No lo deshice: la tarea volvería a quien la tenía, y {e}."
                ) from e
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


async def deshacer_varias(log_ids) -> tuple[int, list[str]]:
    """Revierte VARIAS acciones de un mismo mensaje. Devuelve (cuántas, fallos).

    Existe porque un mensaje puede escribir muchas cosas —"ya hice todo" cierra
    once tareas— y hasta hoy el botón de vuelta apuntaba solo a la última: el
    `antes` de las otras diez seguía en `log_acciones` y no había por dónde
    pedirlo.

    DE ATRÁS PARA ADELANTE, y eso no es un detalle de estilo. Dos ediciones
    sobre la misma fila guardan `antes` encadenados: la primera guarda A (y deja
    B), la segunda guarda B (y deja C). Deshaciendo en orden queda B; deshaciendo
    al revés queda A, que es el estado del que salimos.

    No se corta en el primer fallo: si una huella ya no se puede revertir, las
    demás sí, y quedarse a medias en silencio sería lo peor de las dos cosas. Lo
    que no se pudo se devuelve como texto para que quien llame lo cuente.
    """
    revertidas, fallos = 0, []
    for log_id in sorted({int(i) for i in log_ids}, reverse=True):
        try:
            await deshacer(log_id)
            revertidas += 1
        except Exception as e:  # noqa: BLE001 — el motivo se le cuenta a Tiziano
            fallos.append(f"#{log_id}: {e}")
    return revertidas, fallos
