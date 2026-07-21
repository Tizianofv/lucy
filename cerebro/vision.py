"""Vista de Lucy: lectura de fotos con gpt-4o-mini (OpenAI).

Gemela de whisper.py, y por el mismo motivo: esto es un TRANSCRIPTOR, no un
cerebro. Whisper convierte voz en texto; esto convierte una foto en texto. En
los dos casos, quien decide qué significa lo leído sigue siendo DeepSeek, que
ya sabe clasificar, resolver fechas ambiguas y extraer estructura. Si la vista
opinara sobre el contenido tendríamos dos cerebros discrepando sobre el mismo
mensaje, y ninguna forma de saber cuál mandó.

Ese reparto también es lo que hace que agregar ojos NO toque nada aguas abajo:
una foto termina siendo texto en `transcripcion`, igual que un audio, y el
resto del camino ya estaba escrito.

Comparte la OPENAI_API_KEY con Whisper a propósito: mismo proveedor, mismo SDK
y mismos errores, que _es_pasajero() ya sabe reintentar.

La imagen viaja en memoria y nunca toca el disco: llega de Telegram, va a
OpenAI, y lo que queda guardado es el texto. El original sigue viviendo en los
servidores de Telegram, referenciado por su file_id.
"""
from __future__ import annotations

import base64
import logging

from openai import AsyncOpenAI

from config import OPENAI_API_KEY

log = logging.getLogger("lucy.vision")

# Placeholder por el mismo motivo que en deepseek.py: con la key vacía el SDK
# falla al construir el cliente y se lleva puesto el import del módulo entero.
cliente = AsyncOpenAI(api_key=OPENAI_API_KEY or "sin-key")

MODELO = "gpt-4o-mini"

# "high" y no "low": de un ticket queremos la letra chica. La diferencia de
# costo es de centavos al mes al volumen real (1-2 fotos por día), y leer mal
# un monto sale mucho más caro que eso.
DETALLE = "high"

INSTRUCCIONES = """\
Sos los ojos de Lucy. Recibís una foto que sacó Tiziano y la convertís en
texto. NO opines, NO clasifiques, NO saques conclusiones: solo contá con
precisión qué se ve. Otro sistema decidirá después qué hacer con esto.

Escribí en español, en texto plano, sin markdown.

Según lo que sea la foto:
· TICKET o FACTURA → comercio, fecha, moneda, TOTAL, y los ítems si se leen.
  El total es lo más importante: si está borroso, decilo en vez de adivinar.
· TARJETA DE VISITA → nombre, cargo, empresa, teléfonos, correo, web.
· CARTEL, AFICHE o PANTALLA → transcribí el texto tal cual, y agregá una línea
  sobre qué anuncia (fecha, lugar, horario si los hay).
· PIZARRA o NOTA MANUSCRITA → transcribí lo escrito, respetando las listas.
· CUALQUIER OTRA COSA → describí en una o dos frases qué se ve.

Si algo no se lee con seguridad, escribí "(ilegible)" en ese punto. Inventar un
número en un ticket es peor que admitir que no se ve.\
"""


async def verificar() -> None:
    """Confirma que la key existe. Comparte credencial con Whisper."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY vacía: Lucy no podría leer las fotos.")


async def leer(imagen: bytes) -> str:
    """Bytes de una imagen → texto de lo que se ve."""
    b64 = base64.b64encode(imagen).decode("ascii")
    r = await cliente.chat.completions.create(
        model=MODELO,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": INSTRUCCIONES},
                {
                    "type": "image_url",
                    "image_url": {
                        # Telegram entrega JPEG; el prefijo solo le dice al
                        # modelo cómo decodificar los bytes.
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": DETALLE,
                    },
                },
            ],
        }],
        temperature=0,  # lectura, no creatividad
    )
    return (r.choices[0].message.content or "").strip()
