"""Orquesta la comprensión: toma filas 'sin_procesar' de la bandeja, las pasa
por Gemini, y propone una interpretación para que Tiziano confirme.

Estado: andamiaje. Se implementa en Nivel 2.
"""

# El flujo previsto (Nivel 2):
#   1. Leer de bandeja las filas con estado='sin_procesar'.
#   2. Según tipo_entrada: transcribir audio / leer imagen / usar texto directo.
#   3. gemini.interpretar_texto() → clasificación + extracción estructurada.
#   4. Guardar en bandeja (clasificacion, interpretacion) y marcar
#      estado='esperando_confirmacion'.
#   5. Enviar a Telegram la tarjeta con botones inline [✅] [✏️] [🗑].
