"""Configuración central de Lucy: zona horaria y secretos.

Los secretos se leen de variables de entorno — nunca se escriben en el código.
En Railway se cargan en la pestaña Variables; en local, desde un archivo .env.
"""
import os
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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DATABASE_URL   = os.environ["DATABASE_URL"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Candado de seguridad (pilar): Lucy SOLO le responde a este chat.
# Cualquier otro que le escriba es ignorado sin más.
CHAT_ID_DUENO = int(os.environ["CHAT_ID_DUENO"])
