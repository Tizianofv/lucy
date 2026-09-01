"""Quién puede entrar al panel.

El panel muestra las finanzas completas de la casa. Una URL pública con eso es
un desastre esperando, así que la pregunta de quién entra se resuelve ANTES de
escribir una sola pantalla.

CÓMO: enlace mágico por Telegram. Tiziano le pide el panel a Lucy, Lucy le manda
un enlace firmado que vence en 10 minutos, y al abrirlo queda una cookie de
sesión. Cero contraseñas nuevas, cero servicio de autenticación, cero
credenciales que rotar.

POR QUÉ ASÍ y no con usuario y clave: la frontera de confianza YA EXISTE en este
proyecto —`config.CHAT_ID_DUENO`, "Lucy SOLO le responde a este chat"— y está
probada. Montar un login encima sería inventar una segunda puerta para la misma
casa, con su propia forma de estar mal cerrada. Quien puede pedirle el enlace a
Lucy es exactamente quien ya podía preguntarle cuánto gastó.

EL SECRETO no se inventa acá: sale de TELEGRAM_TOKEN, que ya existe, ya es
secreto y ya vive en las variables de Railway. Un secreto más sería una cosa más
que se puede filtrar, y no compraría nada.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import config

# Un enlace vive 10 minutos. Es de un solo uso en la práctica: el tiempo justo
# para abrirlo desde Telegram, y no tanto como para que quede útil en el
# historial del navegador de nadie.
VIDA_ENLACE = 600

# La sesión dura una semana. Más sería cómodo y peor: una cookie de finanzas
# olvidada en un navegador prestado es exactamente lo que esto evita.
VIDA_SESION = 7 * 24 * 3600


def _firmar(payload: str) -> str:
    return hmac.new(config.TELEGRAM_TOKEN.encode(), payload.encode(),
                    hashlib.sha256).hexdigest()[:32]


def crear_token(chat_id: int, vida: int = VIDA_ENLACE) -> str:
    """Token firmado para un chat concreto. Formato: chat.vence.firma."""
    vence = int(time.time()) + vida
    payload = f"{chat_id}.{vence}"
    return f"{payload}.{_firmar(payload)}"


def validar(token: str | None) -> int | None:
    """chat_id si el token es válido y no venció; None si no.

    Compara la firma con `compare_digest` a propósito: un `==` sobre firmas
    filtra información por el tiempo que tarda en fallar. Es barato hacerlo bien.
    """
    if not token or token.count(".") != 2:
        return None
    chat, vence, firma = token.split(".")
    if not hmac.compare_digest(firma, _firmar(f"{chat}.{vence}")):
        return None
    try:
        if int(vence) < time.time():
            return None
        return int(chat)
    except ValueError:
        return None


def puede_entrar(chat_id: int | None) -> bool:
    """Quién ve las finanzas de la casa. La lista sale de CHAT_IDS_PERMITIDOS,
    que es el dueño más lo que haya en la variable CHAT_IDS_CASA de Railway.

    Vacía por defecto: si nadie la escribe a mano, solo entra el dueño. Que sea
    una enumeración explícita y no una condición es deliberado — esta es la
    única línea que decide quién ve cuánto gasta esta casa, y no puede volverse
    permisiva por accidente."""
    return chat_id is not None and chat_id in config.CHAT_IDS_PERMITIDOS
