"""Configuración central de Lucy: zona horaria y secretos.

Los secretos se leen de variables de entorno — nunca se escriben en el código.
En Railway se cargan en la pestaña Variables; en local, desde un archivo .env.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Zona horaria de Tiziano — República Dominicana, UTC-4, SIN horario de verano.
# Toda interpretación de fechas ("mañana a las 10") se ancla acá.
TZ = ZoneInfo("America/Santo_Domingo")

# En local: cargar .env si existe. En Railway las variables ya están en el entorno.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass  # dotenv es solo comodidad local; en producción no hace falta.

# .strip() defensivo: un espacio invisible pegado al copiar no vuelve a romper nada.
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
DATABASE_URL   = os.environ["DATABASE_URL"].strip()

# Cerebro (texto) y oído (voz). Las de IA van con .get(): si faltan, Lucy
# arranca igual y sigue capturando — degradada, pero sin perder nada. Capturar
# no puede depender de que la IA esté viva.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()

# Tránsito real (Routes API). Si falta, Lucy degrada con gracia: usa las
# rutas que aprendió preguntando, como antes de tener Maps.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

# Vigilancia de correo (Nivel 4, req 17). JSON: [{"user":..., "pass":...}, ...]
# — la "pass" es una contraseña de aplicación de Google (IMAP), no la real.
# Si falta o viene mal, queda vacía y Lucy arranca sin correo: como toda IA,
# su ausencia degrada con gracia, no tumba la captura.
try:
    CORREO_CUENTAS = json.loads(os.environ.get("CORREO_CUENTAS", "[]"))
    if not isinstance(CORREO_CUENTAS, list):
        CORREO_CUENTAS = []
except ValueError:
    CORREO_CUENTAS = []

# Cuenta de servicio de Google Calendar (JSON completo como string). Si falta,
# Lucy sigue con su agenda propia; solo no ve los calendarios de Google. NO se
# hace .strip() del JSON entero — se lee tal cual viene de la variable.
GOOGLE_SA_KEY = os.environ.get("GOOGLE_SA_KEY", "")

# Dónde vive el panel de finanzas. Vacío = no hay panel, y Lucy lo dice en vez
# de mandar un enlace roto.
PANEL_URL = os.environ.get("PANEL_URL", "").rstrip("/")

# Candado de seguridad (pilar): Lucy SOLO le responde a este chat.
# Cualquier otro que le escriba es ignorado sin más.
CHAT_ID_DUENO = int(os.environ["CHAT_ID_DUENO"])

# Quién más puede usar a Lucy y ver el panel. Lista de chat_id separados por
# coma en la variable CHAT_IDS_CASA — vacía por defecto, así que si nadie la
# pone, todo sigue exactamente como estaba.
#
# Es UNA lista y no un booleano a propósito: "quién ve las finanzas de la casa"
# tiene que ser una enumeración explícita que alguien escribió a mano, no una
# condición que se pueda volver verdadera por accidente.
CHAT_IDS_CASA = tuple(
    int(x) for x in os.environ.get("CHAT_IDS_CASA", "").replace(";", ",").split(",")
    if x.strip()
)

# Todos los que pueden entrar, con el dueño siempre dentro.
CHAT_IDS_PERMITIDOS = (CHAT_ID_DUENO,) + tuple(
    c for c in CHAT_IDS_CASA if c != CHAT_ID_DUENO)


# ── CÓMO SE LLAMA CADA UNO ───────────────────────────────────────────────────
#
# El panel de tareas necesita decir QUIÉN tiene pendiente cada tarea, y para
# eso hace falta un nombre. En este sistema no había ninguno: medido sobre el
# esquema (16 tablas, 150 columnas) no hay una sola tabla que tenga a la vez un
# chat y un nombre, y en los 71 `.py` del repo no hay ningún mapa de número a
# nombre ni ninguna llamada a Telegram para pedir un perfil.
#
# POR QUÉ EN UNA VARIABLE DE ENTORNO Y NO EN EL CÓDIGO. Decisión de la sala:
# así Tiziano cambia un nombre o da de alta a una tercera persona él solo,
# desde Railway, sin que nadie despliegue nada. En el código, cada nombre nuevo
# costaría un despliegue a producción.
#
# LA FORMA: pares `chat:nombre` separados por coma (o por punto y coma, igual
# que CHAT_IDS_CASA).
#
#     NOMBRES_POR_CHAT="111111:Tiziano, 222222:Rosi"
#
# Es una lista de PARES y no dos listas paralelas a propósito: con dos listas,
# reordenar una le pone a una persona el nombre de la otra y no hay forma de
# notarlo. Acá cada nombre viaja pegado a su chat y el orden no significa nada.
#
# Y es una variable APARTE de CHAT_IDS_CASA, no un reemplazo: aquélla decide
# quién ENTRA —es la única línea que dice quién ve las finanzas de esta casa— y
# ésta solo dice cómo se llama. Un error de dedo escribiendo nombres no puede
# darle acceso a nadie.
#
# QUÉ PASA CON LO QUE NO SE ENTIENDE: se descarta y SE CUENTA. Un par sin `:`,
# con un chat que no es número o con el nombre vacío no se adivina; entra en
# `NOMBRES_MAL_ESCRITOS` y el panel lo dice. Descartarlo callado es cómo una
# persona desaparece de la lista sin que nadie sepa por qué.
def _leer_nombres(crudo: str) -> tuple[dict[int, str], int]:
    """{chat: nombre} y cuántas entradas no se entendieron.

    Se parte por el PRIMER `:` para que un nombre pueda llevar dos puntos sin
    romper nada. Si el mismo chat aparece dos veces, manda el último — que es
    lo que hace cualquiera al corregir una línea sin borrar la anterior.
    """
    nombres: dict[int, str] = {}
    malos = 0
    for pieza in crudo.replace(";", ",").split(","):
        pieza = pieza.strip()
        if not pieza:
            continue
        chat, sep, nombre = pieza.partition(":")
        nombre = nombre.strip()
        if not sep or not nombre:
            malos += 1
            continue
        try:
            nombres[int(chat.strip())] = nombre
        except ValueError:
            malos += 1
    return nombres, malos


NOMBRES_POR_CHAT, NOMBRES_MAL_ESCRITOS = _leer_nombres(
    os.environ.get("NOMBRES_POR_CHAT", ""))


def personas_del_panel() -> tuple[tuple[int, str], ...]:
    """A quién se le puede asignar una tarea: [(chat, nombre), …].

    LAS DOS CONDICIONES SALEN DE LO REAL, ninguna está tecleada acá:

      · que pueda ENTRAR al panel — `CHAT_IDS_PERMITIDOS`, la misma tupla que
        usa `web.auth.puede_entrar`. Asignarle una tarea a alguien que no puede
        abrir la pantalla es dejarle un pendiente donde nunca lo va a ver.
      · que TENGA NOMBRE en `NOMBRES_POR_CHAT`. Sin nombre no hay nada honesto
        que pintar: inventarlo sería mentir y poner el número de chat lo
        descartó Tiziano.

    El día que entre una tercera persona, aparece acá sola: se la agrega a las
    dos variables y nadie toca una línea de código. Se lee en cada llamada —y
    no se calcula una vez al importar— justamente para eso.

    El orden es el de `CHAT_IDS_PERMITIDOS`, o sea el dueño primero.
    """
    return tuple((c, NOMBRES_POR_CHAT[c]) for c in CHAT_IDS_PERMITIDOS
                 if c in NOMBRES_POR_CHAT)


# ── «CODE», LA SALA DE CONTROL ───────────────────────────────────────────────
#
# Diseño aprobado por Tiziano, 26-sep-2026 (disenos/lucy-code/DISENO.md, §1):
# la sala de control (Claude Code, en la Mac de Tiziano) puede quedar como
# responsable de una tarea, igual que Rosi, para las tareas TÉCNICAS que se
# trabajan con ella. «Code» NO es un chat de Telegram: no tiene con quién
# hablarle, y NO puede entrar al panel (no hay enlace mágico que mandarle).
#
# `CHAT_ID_CODE` es un valor RESERVADO, no un chat real. Negativo a propósito:
# un chat privado de Telegram (una persona, nunca un grupo) es SIEMPRE
# positivo, así que ningún chat_id real de una persona de la casa puede
# chocar con esto, hoy ni en el futuro. No hace falta ninguna tabla ni FK
# para reservarlo: `tareas.responsable_chat_id` ya es un `BIGINT` sin FK
# (`db/schema.sql:181-192`), y esto es un valor de aplicación, no de esquema
# — por eso esta parte no trae ninguna migración.
CHAT_ID_CODE = -1

# El único lugar donde el texto «Code» se teclea en todo el repo. Todo lo
# demás lee ESTA constante — nunca compara contra el string "Code" por su
# cuenta (la misma razón por la que los nombres de personas salen de
# `NOMBRES_POR_CHAT` y no de una copia: dos sitios que dicen lo mismo de dos
# formas son dos sitios que un día van a decir cosas distintas).
NOMBRE_CODE = "Code"


def nombres_con_code() -> dict[int, str]:
    """`NOMBRES_POR_CHAT` más «Code» — para pintar o para resolver un nombre,
    en cualquier sitio que necesite las dos cosas a la vez.

    NO se mete a Code en `NOMBRES_POR_CHAT` ni en `personas_del_panel()`:
    esas dos siguen significando exactamente lo que significaban (quién
    ENTRA al panel y cómo se llama), y Code no entra. Este merge es la única
    forma de que el panel pinte «Code» en vez de «sin nombre» y de que
    Telegram resuelva «Code» como nombre sin tocar el significado de las
    otras dos.
    """
    return {**NOMBRES_POR_CHAT, CHAT_ID_CODE: NOMBRE_CODE}


# ── LA PUERTA HTTP DE LAS TAREAS DE CODE ──────────────────────────────────
#
# Diseño aprobado por Tiziano, 26-sep-2026 (disenos/lucy-code/DISENO.md, §C):
# reemplaza que la sala lea/escriba la base de Lucy directo con
# `railway run -s Postgres`. La sala (desde esta Mac o desde la nube) y
# Natalia hablan con Lucy por HTTP, con una clave — nunca con la base.
#
# EL REPO ES PÚBLICO. Las claves NUNCA viven acá: solo en las variables de
# Railway `CLAVES_API_CODE` y `PERMISOS_API_CODE`, que la SALA pone después,
# con permiso de Tiziano — este archivo no las pone ni las adivina.
#
# `CLAVES_API_CODE`, mismo patrón que `NOMBRES_POR_CHAT` (pares separados
# por coma o punto y coma), pero con los dos lados AL REVÉS: acá lo que
# LLEGA en cada pedido es la clave, así que se busca por clave.
#
#     CLAVES_API_CODE="sala_mac:<clave-larga-1>, sala_nube:<clave-larga-2>, natalia:<clave-larga-3>"
#
# `PERMISOS_API_CODE`, un `quien` por pieza, con sus permisos unidos por `+`
# — un `:` DENTRO del permiso (`tareas:listar`) es parte del nombre, así que
# el separador entre `quien` y su lista es `=`, no `:`:
#
#     PERMISOS_API_CODE="sala_mac=tareas:listar+tareas:cerrar, sala_nube=tareas:listar+tareas:cerrar, natalia=alertas:crear"
#
# Los permisos que YA EXISTEN en el código, hoy (parte 2 del plan): sólo
# `tareas:listar` y `tareas:cerrar` tienen una ruta que los pida
# (`web/api_code.py`). `alertas:crear` y `tareas:tomar` son NOMBRES ya
# reservados para partes futuras del mismo diseño (§D, crear/reusar
# alerta técnica) — declararlos acá no abre ninguna ruta que no exista.
#
# CERRADO POR DEFECTO, en dos niveles independientes:
#   · Si `CLAVES_API_CODE` no está puesta (o no tiene ninguna pieza legible),
#     `CLAVES_API_CODE` queda `{}`: NINGUNA clave resuelve a nadie, así que
#     TODO pedido es 401. No hace falta ningún caso especial en la puerta
#     para esto — es la misma rama que un pedido con una clave inventada.
#   · Si un `quien` SÍ tiene una clave válida pero no aparece en
#     `PERMISOS_API_CODE` (o `PERMISOS_API_CODE` tampoco está puesta), sus
#     permisos son el conjunto vacío: pasa la autenticación y falla en la
#     autorización (403), nunca al revés.
#
# UNA CLAVE QUE DOS `quien` RECLAMAN es AMBIGUA Y PELIGROSA -- no se
# adivina cuál es la buena (elegir "la primera" dejaría a quien tenga la
# clave real actuando como el otro sin que nadie lo note): las dos entradas
# se DESCARTAN, no se deja pasar ninguna. Es la misma decisión que ya toma
# `acciones.crud._chat_del_nombre` con dos personas del mismo nombre — un
# error de configuración se vuelve "nadie entra con esa clave", nunca
# "adivino cuál".
def _leer_claves_api_code(crudo: str) -> tuple[dict[str, str], int]:
    """{clave: quien} y cuántas entradas no se entendieron o quedaron
    descartadas por ambiguas."""
    piezas: list[tuple[str, str]] = []
    malos = 0
    for pieza in crudo.replace(";", ",").split(","):
        pieza = pieza.strip()
        if not pieza:
            continue
        quien, sep, clave = pieza.partition(":")
        quien, clave = quien.strip(), clave.strip()
        if not sep or not quien or not clave:
            malos += 1
            continue
        piezas.append((quien, clave))
    por_clave: dict[str, set[str]] = {}
    for quien, clave in piezas:
        por_clave.setdefault(clave, set()).add(quien)
    claves: dict[str, str] = {}
    for clave, quienes in por_clave.items():
        if len(quienes) > 1:
            malos += len(quienes)
            continue
        claves[clave] = next(iter(quienes))
    return claves, malos


def _leer_permisos_api_code(crudo: str) -> tuple[dict[str, frozenset[str]], int]:
    """{quien: {permisos}} y cuántas entradas no se entendieron.

    Si el mismo `quien` aparece en dos piezas, sus permisos se UNEN — no es
    el caso ambiguo de `_leer_claves_api_code` (ahí dos DUEÑOS distintos de
    la misma clave es un choque; acá es la misma persona con más de una
    línea, y sumar es la lectura obvia)."""
    permisos: dict[str, frozenset[str]] = {}
    malos = 0
    for pieza in crudo.replace(";", ",").split(","):
        pieza = pieza.strip()
        if not pieza:
            continue
        quien, sep, lista = pieza.partition("=")
        quien, lista = quien.strip(), lista.strip()
        items = tuple(p.strip() for p in lista.split("+") if p.strip())
        if not sep or not quien or not items:
            malos += 1
            continue
        permisos[quien] = permisos.get(quien, frozenset()) | frozenset(items)
    return permisos, malos


CLAVES_API_CODE, CLAVES_API_CODE_MAL_ESCRITAS = _leer_claves_api_code(
    os.environ.get("CLAVES_API_CODE", ""))
PERMISOS_API_CODE, PERMISOS_API_CODE_MAL_ESCRITOS = _leer_permisos_api_code(
    os.environ.get("PERMISOS_API_CODE", ""))


# ── COPIA AL DUEÑO ────────────────────────────────────────────────────────
#
# Decisión de Tiziano, 21-sep-2026: «lo que me llega a mí también le llega a
# Rosi, nada cambiado, simplemente lo mismo» / «no importa si me llega algo
# más quiero que le lleguen a ella también» / «exacto, todo».
#
# LA FORMA: nombres separados por coma (o por punto y coma), buscados contra
# `personas_del_panel()` — la MISMA lista de quién puede entrar y cómo se
# llama que ya existe, no una lista propia. Por eso solo se le puede copiar a
# alguien que YA tiene acceso a Lucy: escribir acá un nombre no le abre la
# puerta a nadie, y si mañana alguien deja de poder entrar, deja de recibir
# copia el mismo día sin tocar esta variable.
#
#     COPIAS_DEL_DUENO="Rosi"
#
# QUÉ PASA SI UN NOMBRE NO RESUELVE: no se copia a NADIE —ni a los nombres
# que sí resolvieron— y queda un aviso en el registro. Es la misma regla que
# `cuentas_de_correo`: ante lo que no se entiende, la salida segura es «como
# si la variable no existiera», nunca «copiar a medias sin que se sepa qué
# falta».
#
# Vacía (el default): nadie se copia. Todo queda exactamente como hoy.
_NOMBRES_DE_COPIA = tuple(
    n.strip() for n in os.environ.get("COPIAS_DEL_DUENO", "").replace(";", ",").split(",")
    if n.strip()
)


def chats_de_copia() -> tuple[int, ...]:
    """A qué chats se copia cada mensaje que Lucy le manda al dueño.

    Se deriva de `personas_del_panel()` en cada llamada, igual que
    `puede_ser_responsable`: no hay una lista propia que se pueda separar de
    quién puede entrar de verdad.
    """
    if not _NOMBRES_DE_COPIA:
        return ()
    por_nombre = {nombre: chat for chat, nombre in personas_del_panel()}
    resueltos = tuple(por_nombre[n] for n in _NOMBRES_DE_COPIA if n in por_nombre)
    if len(resueltos) != len(_NOMBRES_DE_COPIA):
        faltan = [n for n in _NOMBRES_DE_COPIA if n not in por_nombre]
        log.warning(
            "COPIAS_DEL_DUENO tiene nombre(s) que no resuelven contra "
            "personas_del_panel() (%s): no se copia a nadie hasta "
            "corregirlo.", ", ".join(faltan))
        return ()
    return resueltos


def dueno_de_calendario(nombre: str | None) -> int | None:
    """El chat de la persona que ese NOMBRE identifica, buscado en
    `personas_del_panel()` -- la MISMA fuente que ya usa `chats_de_copia()`,
    no una lista aparte. `None` si `nombre` es falsy (sin dueño puesto) o si
    no resuelve contra quién puede entrar al panel.

    Vive acá, junto a `chats_de_copia`, por lo mismo que aquella: quien la
    llama (`cerebro/calendario.py`, encargo "Google → dueño por calendario",
    22-sep-2026) declara el dueño de un calendario por NOMBRE, nunca por
    chat_id -- ese archivo es público. Es una función aparte y no una
    reutilización de `chats_de_copia` porque resuelve UN nombre, no una
    lista: `chats_de_copia` decide "COPIAS_DEL_DUENO" y este encargo no lo
    toca.
    """
    if not nombre:
        return None
    por_nombre = {n: c for c, n in personas_del_panel()}
    return por_nombre.get(nombre)


def chat_escrito(texto):
    """El chat que dice ese TEXTO, o None si ese texto no es un chat.

    LA ÚNICA QUE DECIDE QUÉ TEXTO ES UN CHAT. La llaman los dos caminos por los
    que alguien puede pedir un responsable escribiéndolo: el panel
    (`web.app._responsable_pedido`) y Telegram (`acciones.crud`). Tener una
    sola es lo que impide que los dos se separen, igual que con
    `puede_ser_responsable`.

    LA REGLA, ENTERA, EN UNA LÍNEA: vale si el texto —sin los espacios de
    alrededor— es EXACTAMENTE cómo se escribe ese número, o sea
    `str(int(t)) == t`. Nada más. `int()` por su cuenta se traga además un cero
    delante, un `+`, un `1_000` y las cifras de otros alfabetos; ninguna de esas
    cuatro cosas es cómo alguien escribe un chat, y las cuatro caen por la misma
    regla sin que ninguna esté nombrada acá.

    POR QUÉ TAN ESTRECHO, y es la cicatriz del 11-sep-2026. El número se guarda
    UNA vez y se vuelve a decir con el nombre de la persona
    (`cerebro.agente._con_nombres`), que compara la cifra entera. Mientras la
    entrada acepte `0<chat>` y la salida solo sepa deshacer `<chat>`, el mismo
    QUIÉN existe escrito de dos maneras y una de ellas no se puede traducir:
    cualquier frase que repita lo PEDIDO en vez de lo que QUEDÓ saca a la calle
    el número de una persona. Se midió el turno entero y salía por Telegram.

    Y no se arregla ensanchando la traducción: las escrituras de un mismo
    número son infinitas —un cero delante, dos, tres— y no se terminan de
    enumerar nunca. Se le pone fondo a la entrada, que sí lo tiene: UNA por
    persona.

    El negativo se lee como lo que es: un chat de grupo de Telegram empieza por
    `-` y `str(-100) == "-100"`, así que la misma regla lo deja pasar sin
    ninguna excepción escrita.
    """
    if not isinstance(texto, str):
        return None
    t = texto.strip()
    try:
        chat = int(t)
    except ValueError:
        return None
    return chat if str(chat) == t else None


def puede_ser_responsable(chat_id) -> bool:
    """LA PUERTA ÚNICA de quién puede quedar como responsable de una tarea.

    La llaman directo la ruta del panel y `db.asignar_responsable`. Los dos
    escritores GENÉRICOS —`acciones.crud.editar` y `acciones.crud.deshacer`,
    que no nombran la columna porque la sacan de los datos— llegan acá por
    `crud._por_las_puertas`. Quién escribe la columna no se da por sabido: lo
    cuenta `tests/test_responsable.py` recorriendo cada `execute` del
    repositorio, y hasta dónde llega ese recorrido está dicho ahí, en LA
    FRONTERA, y en ningún otro sitio.

    Que la respuesta se derive de `personas_del_panel()` y no de una lista
    propia es lo que impide que las dos se separen: si mañana alguien deja de
    poder entrar al panel, deja de poder ser responsable el mismo día.

    LA ÚNICA EXCEPCIÓN, y por qué es una comparación y no una lista: `Code`
    (`CHAT_ID_CODE`) puede quedar como responsable sin estar en
    `personas_del_panel()`, porque nunca «entra al panel» — no tiene chat de
    Telegram con el que abrir un enlace. Agregarlo a `personas_del_panel()`
    para que pasara esta puerta habría mentido sobre lo que esa función
    contesta (`web.auth.puede_entrar` la usa para eso mismo). Esta puerta
    sigue siendo LA única que decide «puede ser responsable»: lo que NO
    decide es «se le puede escribir por Telegram» — para eso está
    `puede_recibir_telegram`, más abajo, que es DISTINTA a propósito.
    """
    return chat_id == CHAT_ID_CODE or any(c == chat_id for c, _ in personas_del_panel())


def puede_recibir_telegram(chat_id) -> bool:
    """¿Se le puede mandar un mensaje de Telegram a este chat_id?

    NO es lo mismo que `puede_ser_responsable`. Esa puerta decide si un valor
    puede QUEDAR ASIGNADO a una tarea; ésta decide si, dado que ya quedó
    asignado, Lucy le puede escribir. Para toda persona real de la casa las
    dos preguntas tienen la misma respuesta, y por eso hasta hoy había una
    sola función — pero `CHAT_ID_CODE` puede ser responsable (arriba) y NO
    tiene Telegram: intentar `bot.send_message(CHAT_ID_CODE, ...)` le
    respondería a Lucy que ese chat no existe.

    LA LLAMAN los sitios de `cerebro/despertador.py` que arman a QUIÉN
    mandarle un aviso a partir de un `responsable_chat_id` guardado
    (`revisar`, para el recordatorio de una tarea puntual, y
    `_destinatarios_de_tareas`, para a quién armarle su propio briefing o
    plan semanal) — NUNCA `acciones/crud.py` ni `db.asignar_responsable`, que
    siguen preguntándole a `puede_ser_responsable`: ésos deciden si algo
    puede QUEDAR, no si se le puede escribir.
    """
    return chat_id != CHAT_ID_CODE and puede_ser_responsable(chat_id)


def chats_sin_nombre() -> int:
    """Cuántos de los que ENTRAN al panel no tienen nombre en la variable.

    Devuelve una cuenta y nunca un chat: el número de Telegram de una persona
    no es material de pantalla. Mientras esto sea mayor que cero hay alguien
    que puede abrir el panel y a quien no se le puede asignar nada, y el panel
    tiene que decirlo — callarlo deja a Tiziano buscando en el desplegable un
    nombre que nunca va a estar.
    """
    return sum(1 for c in CHAT_IDS_PERMITIDOS if c not in NOMBRES_POR_CHAT)


# ── LA PUERTA ÚNICA DE LOS BUZONES ────────────────────────────────────────────
#
# `CORREO_CUENTAS` de arriba es la lista CRUDA, con las credenciales. Nadie la
# lee directamente: se pide por `cuentas_de_correo(para=...)`, y ese `para` es
# el punto donde se decide qué buzones existen para cada cosa.
#
# POR QUÉ ACÁ Y NO EN CADA CAMINO. Hasta el 5-sep-2026 cada función que tocaba
# el correo iteraba `config.CORREO_CUENTAS` por su cuenta y se acordaba (o no)
# de mirar `reporte_a`. `reporte_diario` se acordaba; `revisar_ahora`,
# `buscar`, `leer` y `vigilar_911` no. O sea: el reporte automático respetaba
# el campo y todo lo que Tiziano pedía a mano se lo saltaba, enseñándole el
# correo de un buzón marcado con `reporte_a: 0` — justo la fuga que ese campo
# existe para impedir.
#
# Filtrar la salida de esas cuatro no arregla el problema, lo posterga: el
# quinto camino que alguien escriba mañana vuelve a olvidarse. Así que el
# filtro no está en la salida de cada camino, sino en la ENTRADA de todos: para
# tocar un buzón hace falta su usuario y su contraseña, y el único sitio del
# que salen es esta función. Un camino nuevo no puede "olvidarse" de filtrar
# porque no puede conseguir un buzón sin decir para qué lo quiere — y no hay
# valor por defecto que lo decida por él.
#
# Lo guarda `tests/test_buzon_que_no_se_ve.py::test_nadie_lee_la_lista_cruda`,
# que recorre los .py que hay EN DISCO (no una lista escrita a mano) y los lee
# con `ast.parse`: compara nodos del árbol de sintaxis, nunca texto. Da igual
# si el camino nuevo escribe `config.CORREO_CUENTAS`, la importa con alias, la
# pide por `getattr` con el nombre partido en dos, por `vars(config)` o por
# `importlib` — el único uso permitido del módulo es leerle un atributo suyo
# que no sea esta lista, y todo lo demás cae del lado rojo por no poder
# clasificarse. La lista de atributos permitidos sale de `vars(config)`, o sea
# de lo que este archivo de verdad define, no de algo tecleado en la prueba.
#
# BARRER NO ES MOSTRAR, y ésa es toda la distinción:
#
#   · "barrer"  → TODOS los buzones. Abrir el IMAP, leer, sacar movimientos
#     bancarios, marcar leído lo que ya se informó. Nada de esto le cuenta
#     nada a nadie: es máquina hablando con máquina. El buzón de Rosi se
#     sigue barriendo entero, igual que antes.
#
#   · "mostrar" → solo los que tienen a quién informar. Todo lo que termina
#     en texto que Tiziano lee: el reporte de la mañana, "revisá el correo",
#     una búsqueda, leer un correo suelto, una alerta 911.
#
# Es la línea entre "Lucy lee el correo de Rosi para sacar sus movimientos" y
# "Lucy le cuenta a Tiziano lo que le escriben a Rosi". El sistema tiene que
# poder hacer lo primero sin lo segundo.

def destinos_del_reporte(cuenta: dict) -> tuple[int, ...]:
    """A qué chats va el reporte de ESTE buzón. () = a nadie.

    Sin el campo `reporte_a`, va al dueño — que es como se comportaba antes y
    por eso no rompe nada existente. Con él, el buzón se puede escanear para
    bancos sin que su correspondencia aparezca en el briefing de otra persona.

    `reporte_a: 0` (o false) = este buzón NO se le enseña a nadie.

    VARIOS DESTINOS (encargo 3, "Rosi independiente", 22-sep-2026):
    `reporte_a` acepta ahora también una LISTA de chats — es la MISMA puerta,
    extendida, no un campo nuevo aparte. Un buzón que hoy solo informa al
    dueño puede pasar a informarle también a otra persona sin que el código
    invente de quién es cada buzón: eso lo sigue diciendo esta variable, no
    una lista tecleada en otro archivo. `reporte_a: [111, 222]` = a los dos
    chats, cada uno con su propio reporte independiente.

    Vive en config y no en `captura/correo.py` porque es política de
    configuración —qué dice la variable de entorno sobre cada buzón— y porque
    tiene que estar donde está la lista cruda: es lo que la convierte en las
    dos vistas de abajo. `captura.correo.destinos_del_reporte` sigue
    existiendo como alias.
    """
    v = cuenta.get("reporte_a", cuenta.get("reporte", True))
    if v is True:
        return (CHAT_ID_DUENO,)
    if v is False or v == 0:
        return ()
    if isinstance(v, (list, tuple)):
        try:
            return tuple(int(x) for x in v)
        except (TypeError, ValueError):
            log.warning("reporte_a inválido en %s (%r): mando al dueño.",
                        cuenta.get("user"), v)
            return (CHAT_ID_DUENO,)
    try:
        return (int(v),)
    except (TypeError, ValueError):
        log.warning("reporte_a inválido en %s (%r): mando al dueño.",
                    cuenta.get("user"), v)
        return (CHAT_ID_DUENO,)


def destino_del_reporte(cuenta: dict) -> int:
    """Compatibilidad con el código y las pruebas de antes del encargo 3: el
    PRIMER destino de `destinos_del_reporte`, o 0 si no hay ninguno.

    Sigue sirviendo para todo lo que solo necesita saber SI hay a quién
    informar (`cuentas_de_correo("mostrar")`). Lo que necesita TODOS los
    destinos —el reporte de correo en sí— usa `destinos_del_reporte`.
    """
    d = destinos_del_reporte(cuenta)
    return d[0] if d else 0


def cuentas_de_correo(para: str) -> list[dict]:
    """Los buzones sobre los que se puede trabajar, según PARA QUÉ.

    `para` es obligatorio y su vocabulario es CERRADO: un valor que no esté
    acá revienta, no elige un valor por defecto. Es la regla del proyecto (los
    vocabularios son cerrados) y acá además es la que sostiene todo: si
    hubiera un valor por defecto, el camino nuevo que se olvide de pensar
    heredaría "todos" y la fuga volvería sin que nadie la escribiera.

        cuentas_de_correo("barrer")  → todos los buzones
        cuentas_de_correo("mostrar") → solo los que tienen a quién informar
    """
    if para == "barrer":
        return list(CORREO_CUENTAS)
    if para == "mostrar":
        return [c for c in CORREO_CUENTAS if destino_del_reporte(c)]
    raise ValueError(
        f"cuentas_de_correo(para={para!r}): los únicos valores son 'barrer' "
        "(leer/procesar todos los buzones) y 'mostrar' (lo que va a ojos de "
        "Tiziano). Elegí a conciencia: 'mostrar' respeta reporte_a, 'barrer' no.")


# ── Tarifa doble de DeepSeek (regla de Tiziano, 1-ago-2026) ───────────────────
#
# DeepSeek cobra el DOBLE en dos franjas del día. En hora de Santo Domingo
# (UTC-4, sin horario de verano) caen así:
#
#     21:00 → 00:00     y     02:00 → 06:00
#
# LA REGLA: ningún proceso AUTOMÁTICO/programado que gaste DeepSeek puede
# arrancar adentro de esas franjas. Se mueve al horario barato más cercano que
# preserve su propósito, o se difiere hasta que la franja pase.
#
# LO QUE NO SE TOCA: todo lo conversacional —interpretar un mensaje de Tiziano,
# el agente respondiéndole, un botón que él pulsa— corre siempre, a la hora que
# sea. Hacerlo esperar para ahorrar centavos sería cobrarle el ahorro en su
# tiempo, que es justo lo que Lucy existe para no hacer. Tampoco se difiere una
# emergencia (la vigilancia 911 del correo) ni nada atado a un compromiso con
# hora fija: diferirlos no ahorra, los rompe. (El ejemplo de esto último era el
# preaviso de salida de una cita, que murió el 13-ago-2026; el principio sigue
# valiendo para lo próximo que se ate a una hora que Lucy no elige.)
#
# Formato: (hora_desde, hora_hasta) en HORAS LOCALES, con el `hasta` EXCLUSIVO.
VENTANAS_CARAS_DEEPSEEK = ((21, 24), (2, 6))


def es_horario_caro_deepseek(ahora: datetime | None = None) -> bool:
    """¿Estamos dentro de una franja de tarifa doble de DeepSeek?

    `ahora` se interpreta SIEMPRE en hora de Santo Domingo. Si viene con zona
    (típico: UTC, que es lo que devuelve Postgres) se convierte primero; si
    viene sin zona se asume que ya es local.

    Esa conversión es la trampa entera del asunto: las 21:00 de Santo Domingo
    son la 01:00 UTC del día siguiente, así que comparar las horas de un
    datetime en UTC contra estas franjas da la respuesta CONTRARIA justo en los
    bordes — que es donde importa. Por eso el `astimezone` no es defensivo: es
    la función.
    """
    ahora = ahora or datetime.now(TZ)
    if ahora.tzinfo is not None:
        ahora = ahora.astimezone(TZ)
    return any(desde <= ahora.hour < hasta
               for desde, hasta in VENTANAS_CARAS_DEEPSEEK)
