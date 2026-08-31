"""Configuración central de Lucy: zona horaria y secretos.

Los secretos se leen de variables de entorno — nunca se escriben en el código.
En Railway se cargan en la pestaña Variables; en local, desde un archivo .env.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

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
