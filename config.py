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

# Candado de seguridad (pilar): Lucy SOLO le responde a este chat.
# Cualquier otro que le escriba es ignorado sin más.
CHAT_ID_DUENO = int(os.environ["CHAT_ID_DUENO"])
