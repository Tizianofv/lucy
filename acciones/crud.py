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
from decimal import Decimal, InvalidOperation

from psycopg.rows import dict_row

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

    # Personas y proyectos se resuelven fuera de la transacción a propósito:
    # crear una persona de más es inofensivo y reutilizable, mientras que
    # meterlo adentro alargaría la transacción de la entidad sin ganar nada.
    persona_id = await db.buscar_o_crear_persona(str(r.get("persona") or ""))
    proyecto_id = await db.buscar_o_crear_proyecto(str(r.get("proyecto") or ""))

    async with db.pool.connection() as conn:
        if clas == "tarea":
            tabla = "tareas"
            ya = await _duplicado_pendiente(conn, tabla, titulo, cuando)
            if ya is not None:
                # El agente la re-pidió; ya existía. Se devuelve la de antes
                # sin crear otra ni escribir un log nuevo.
                return tabla, ya[0], ya[1]
            cur = await conn.execute(
                """
                INSERT INTO tareas
                  (bandeja_id, titulo, detalle, vence_en, recurrencia,
                   proyecto_id, persona_id, anticipos_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (bandeja_id, titulo, detalle, cuando,
                 str(r.get("recurrencia") or "").strip() or None,
                 proyecto_id, persona_id, _anticipos(r.get("anticipos_min"))),
            )

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
