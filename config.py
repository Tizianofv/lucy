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
# que recorre los .py que hay EN DISCO (no una lista escrita a mano) y se pone
# rojo si algún archivo vuelve a nombrar `config.CORREO_CUENTAS`.
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

def destino_del_reporte(cuenta: dict) -> int:
    """A qué chat va el reporte de ESTE buzón. 0 = a nadie.

    Sin el campo `reporte_a`, va al dueño — que es como se comportaba antes y
    por eso no rompe nada existente. Con él, el buzón se puede escanear para
    bancos sin que su correspondencia aparezca en el briefing de otra persona.

    `reporte_a: 0` (o false) = este buzón NO se le enseña a nadie.

    Vive en config y no en `captura/correo.py` porque es política de
    configuración —qué dice la variable de entorno sobre cada buzón— y porque
    tiene que estar donde está la lista cruda: es lo que la convierte en las
    dos vistas de abajo. `captura.correo.destino_del_reporte` sigue existiendo
    como alias.
    """
    v = cuenta.get("reporte_a", cuenta.get("reporte", True))
    if v is True:
        return CHAT_ID_DUENO
    if v is False or v == 0:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        log.warning("reporte_a inválido en %s (%r): mando al dueño.",
                    cuenta.get("user"), v)
        return CHAT_ID_DUENO


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
