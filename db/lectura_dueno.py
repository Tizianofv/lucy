"""Lectura de SOLO lo de Tiziano en `bandeja`, `notas` y `eventos`
(§A.1-A.3 del plan de construcción "Code como responsable de tareas
técnicas", parte A, 27-sep-2026). `movimientos` queda FUERA por ahora --
ver más abajo por qué.

QUÉ ES ESTO Y QUÉ NO ES: tres funciones de lectura para que Code (el
agente de la sala de control) pueda consultar los datos de Tiziano sin
tocar los de Rosi ni los de nadie más. NO expone ruta HTTP -- eso no está
aprobado (ver el reporte de esta parte). `personas` y `preferencias`
quedan FUERA a propósito: van en la parte E.

UNA SOLA TRANSACCIÓN DE SOLO LECTURA por llamada: `SET TRANSACTION READ
ONLY`, dentro de una transacción explícita. Ese literal exacto es a
propósito el MISMO que ya reconoce `tests/test_responsable.py::
_ejecuta_solo_lectura` (el censo de escritores de `tareas.responsable_
chat_id`): sin él, el censo clasifica a `_leer_de_dueno` como "escritor
genérico" -- arma su SELECT con SQL dinámico -- y pide una sonda para una
función que nunca escribe nada. `SET TRANSACTION` (a diferencia de `SET
default_transaction_read_only`) es transaccional por definición del
estándar SQL: revierte solo al terminar la transacción, así que la
conexión FÍSICA del pool nunca se queda en solo lectura para la próxima
función que la tome prestada.

"DE TIZIANO" SE DECIDE POR UN VALOR EN LA FILA, SIEMPRE CONTRA
`config.CHAT_ID_DUENO`, SIEMPRE POR LA MISMA PUERTA (`_condicion_de_dueno`,
abajo) -- las lecturas la llaman, ninguna arma su propio criterio:

  · `bandeja.chat_id` -- la columna existe en la fila misma, PERO no
    alcanza sola: `captura/consumos.py` deja `chat_id=CHAT_ID_DUENO` en
    CUALQUIER correo bancario, sea del buzón que sea (incluido el de
    Rosi) -- medido contra producción, 27-sep-2026 (hallazgo de la sala).
    Por eso se exige TAMBIÉN `origen` en la lista de orígenes CONFIABLES
    (`_ORIGENES_CONFIABLES_DE_BANDEJA`, censada abajo desde el código
    real): un `chat_id` que dice la verdad es el que puso quien de verdad
    mandó o recibió esa fila (Telegram, un aviso del despertador/correo/
    panel/copia al dueño), no el que alguien puso por default sobre
    contenido que no escribió esa persona.
  · `notas` no tiene chat_id propio: se decide por la `bandeja` que la
    originó (`bandeja_id`), con el MISMO filtro de `chat_id` + `origen`
    confiable. Una fila sin `bandeja_id` NO tiene forma de probar de
    quién es, así que NUNCA sale -- ver el docstring de
    `_condicion_de_dueno`.
  · `eventos.duenos_chat_id` es un ARRAY (puede tener a Tiziano, a Rosi, a
    los dos, o estar vacío -- "sin dueño", el estado de casi toda cita
    sincronizada de Google, ver `db/schema.sql`). SEGÚN EL DISEÑO APROBADO
    (`DISENO.md` §A.1/§A.2), un array VACÍO sí sale: es "de la casa",
    compartida por defecto -- MISMO criterio que
    `despertador._destinatarios_de_tareas` (que trata el array vacío como
    "de todos"). Un array CON dueños puestos sale solo si incluye a
    `CHAT_ID_DUENO`: una cita puesta explícitamente SOLO a nombre de Rosi
    no es de Tiziano, aunque viva en la misma base.
    (27-sep-2026: la primera versión de esta lectura excluía el array
    vacío -- medido contra producción con `tools/contar_lectura_de_dueno.py`:
    125 eventos totales, 11 "de Tiziano", 114 "de otro" -- que no era lo
    que el diseño aprobado pedía. Corregido para que el array vacío
    cuente, como dice `DISENO.md`.)

UNA NOTA O MOVIMIENTO SIN `bandeja_id` NUNCA SALE: no hay forma de probar
de quién es, y ausencia de prueba se trata como "no es de Tiziano", nunca
como "sí lo es". Un EVENTO es la excepción declarada arriba: su "sin
dueño" (array vacío) SÍ cuenta, porque el diseño lo define así, no porque
se relaje la regla general.

`movimientos` NO tiene lectura en esta parte (ver `_condicion_de_dueno`
más abajo): medido contra producción con `tools/contar_lectura_de_dueno.py`
antes de este arreglo, el filtro por `bandeja.chat_id` no distinguía nada
-- 223 movimientos totales, 220 "de Tiziano", 3 "de otro" -- porque
`captura/consumos.py:455` guarda TODO correo bancario (de cualquier
buzón, incluido el de Rosi, ver `config.py` sobre "barrer") con
`chat_id=config.CHAT_ID_DUENO` en la `bandeja` que lo origina. La fila no
tiene ningún dato que diga de qué buzón salió, así que no hay manera
honesta de separar los movimientos de Rosi de los de Tiziano con lo que
existe hoy.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from config import CHAT_ID_DUENO
from db.db import pool

LIMITE_POR_OMISION = 50
LIMITE_MAXIMO = 500

# CENSO DE ESCRITORES DE `bandeja`, hecho a mano contra el código real
# (27-sep-2026, hallazgo de la sala: `bandeja.chat_id` solo no alcanza).
# Cada escritor de `bandeja` del repo (`db.guardar_en_bandeja`, `db.
# registrar_aviso`, y los dos INSERT literales de `crear_tarea_desde_el_
# panel`/`cerrar_y_derivar`) tiene que estar clasificado en UNO de los dos
# conjuntos de abajo. `tests/test_lectura_dueno.py::
# test_el_censo_de_origenes_no_tiene_un_escritor_nuevo_sin_clasificar`
# recorre el AST del repo entero y exige que el conjunto de orígenes que
# encuentra sea EXACTAMENTE la unión de los dos -- ni uno de más (un
# escritor nuevo sin clasificar) ni uno de menos (uno de estos ya no
# existe). Tabla completa, con archivo:línea, en el reporte de esta parte.
#
#   CONFIABLES (el chat_id de la fila es de verdad quien la originó, o el
#   destinatario real al que Lucy se la dirigió):
#     "telegram"     -- captura/telegram.py:25 (default de guardar_en_
#                        bandeja): chat_id = msg.chat_id, el remitente real.
#     "despertador"  -- cerebro/despertador.py:128,594,854 (llamadas a
#                        registrar_aviso/guardar_en_bandeja): chat_id =
#                        destino, el destinatario real del aviso.
#     "correo"       -- captura/correo.py:1203,1370: chat_id = destino,
#                        el destinatario real del aviso de correo/911.
#     "panel"        -- db/db.py:3657 (crear_tarea_desde_el_panel),
#                        db/db.py:3861 (cerrar_y_derivar): chat_id = la
#                        sesión autenticada del panel (`web/app.py`), no
#                        un valor por default.
#     "copia_dueno"  -- cerebro/copia_dueno.py:174: chat_id = destino, el
#                        chat al que Lucy copió el mensaje de verdad.
#
#   NO CONFIABLES (el chat_id NO dice quién originó el contenido):
#     "banco"        -- captura/consumos.py:455,588,615,641,688: SIEMPRE
#                        chat_id=config.CHAT_ID_DUENO, sea cual sea el buzón
#                        de origen (incluido el de Rosi) -- medido contra
#                        producción, hallazgo de la sala 27-sep-2026.
#
#   Tabla exacta, sacada corriendo `tests/test_lectura_dueno.py::
#   _origenes_reales_de_bandeja()` sobre este mismo commit -- no tecleada
#   de memoria.
_ORIGENES_CONFIABLES_DE_BANDEJA = (
    "telegram", "despertador", "correo", "panel", "copia_dueno")
_ORIGENES_NO_CONFIABLES_DE_BANDEJA = ("banco",)


def _condicion_de_dueno(tabla: str) -> str:
    """LA ÚNICA PUERTA: el fragmento SQL que decide "esta fila es de
    Tiziano" para `tabla`. Toma UN parámetro posicional (`CHAT_ID_DUENO`).

    Las lecturas de este archivo llaman a ESTA función -- ninguna escribe
    su propio `WHERE chat_id = ...` a mano. Una prueba (`tests/
    test_lectura_dueno.py::test_las_lecturas_pasan_por_leer_de_dueno`) lo
    exige recorriendo el AST de las funciones reales.

    `movimientos` NO tiene puerta A PROPÓSITO -- no hay lectura de
    movimientos en esta parte, ver el docstring del módulo. Pedirla acá
    revienta con `ValueError`, para que quien la necesite se entere del
    motivo en vez de recibir un WHERE que promete más de lo que cumple.
    """
    if tabla == "bandeja":
        origenes = ", ".join(f"'{o}'" for o in _ORIGENES_CONFIABLES_DE_BANDEJA)
        return f"chat_id = %s AND origen IN ({origenes})"
    if tabla == "notas":
        origenes = ", ".join(f"'{o}'" for o in _ORIGENES_CONFIABLES_DE_BANDEJA)
        return (f"bandeja_id IN (SELECT id FROM bandeja WHERE chat_id = %s "
                f"AND origen IN ({origenes}))")
    if tabla == "eventos":
        # DISEÑO APROBADO (`DISENO.md` §A.2): array vacío = de la casa,
        # sale; con dueños puestos, sale solo si incluye a CHAT_ID_DUENO.
        return "(duenos_chat_id = '{}' OR %s = ANY(duenos_chat_id))"
    if tabla == "movimientos":
        raise ValueError(
            "movimientos no tiene lectura en la parte A: bandeja.chat_id "
            "no distingue el buzón de origen (captura/consumos.py guarda "
            "TODO correo bancario con chat_id=CHAT_ID_DUENO, incluido el "
            "de Rosi) -- no hay dato confiable para decidir de quién es "
            "un movimiento. Hace falta guardar el buzón de origen en la "
            "fila antes de poder ofrecer esta lectura.")
    raise ValueError(f"tabla sin puerta de dueño definida: {tabla!r}")


def _limite(limite: int) -> int:
    """Nunca 0, nunca negativo, nunca más de `LIMITE_MAXIMO` -- una lectura
    para Code no puede convertirse por accidente (o por un valor mal
    puesto) en un volcado completo de la tabla."""
    return max(1, min(int(limite), LIMITE_MAXIMO))


async def _leer_de_dueno(tabla: str, columnas: str, extra_where: str,
                         orden: str, limite: int) -> list[dict]:
    """Lo que las cuatro funciones públicas de abajo tienen en común: abrir
    una transacción de SOLO LECTURA, armar el WHERE con `_condicion_de_dueno`
    y devolver filas como diccionarios. Cada función pública sigue siendo
    su propia función nombrada (no una sola genérica parametrizada por
    fuera) para que el censo de "las cuatro llaman a la puerta" recorra
    código de verdad, no una fábrica.
    """
    condicion = _condicion_de_dueno(tabla)
    where = condicion if not extra_where else f"{extra_where} AND {condicion}"
    sql = f"SELECT {columnas} FROM {tabla} WHERE {where} ORDER BY {orden} LIMIT %s"
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(sql, (CHAT_ID_DUENO, _limite(limite)))
            return list(await cur.fetchall())


async def leer_bandeja_de_dueno(limite: int = LIMITE_POR_OMISION) -> list[dict]:
    """Los últimos mensajes de Tiziano en la bandeja (§A.1). Sin `embedding`
    (vector, no sirve fuera de Postgres) ni `interpretacion` completa (JSON
    grande); lo justo para que Code sepa qué pasó y cuándo."""
    return await _leer_de_dueno(
        "bandeja",
        "id, creado_en, origen, tipo_entrada, contenido_raw, "
        "clasificacion, estado",
        extra_where="",
        orden="creado_en DESC",
        limite=limite)


async def leer_notas_de_dueno(limite: int = LIMITE_POR_OMISION) -> list[dict]:
    """Las notas/ideas de Tiziano, no borradas (§A.2)."""
    return await _leer_de_dueno(
        "notas",
        "id, creado_en, contenido, etiquetas, proyecto_id, persona_id",
        extra_where="borrado_en IS NULL",
        orden="creado_en DESC",
        limite=limite)


async def leer_eventos_de_dueno(limite: int = LIMITE_POR_OMISION) -> list[dict]:
    """Las citas de Tiziano, no borradas (§A.3): las suyas, las que
    comparte con Rosi, y las de la casa (`duenos_chat_id` vacío, el estado
    normal de casi toda cita sincronizada de Google). Una cita puesta
    EXPLÍCITAMENTE solo a nombre de Rosi no sale -- ver el docstring del
    módulo y `DISENO.md` §A.1/§A.2."""
    return await _leer_de_dueno(
        "eventos",
        "id, titulo, inicia_en, termina_en, lugar, persona_id, "
        "proyecto_id, notas",
        extra_where="borrado_en IS NULL",
        orden="inicia_en DESC",
        limite=limite)
