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


# ── La sesión de «solo ver» (entrada desde la App de registro) ────────────────
#
# Quien entra a Proyectos desde la App con nivel `ver` no es alguien que Lucy
# conozca: no se le abre el panel. Se le da OTRA cookie, aparte, que solo abre
# `GET /proyectos` y el logo. La separación está en tres sitios y cada uno se
# prueba por su cuenta:
#   1. otro NOMBRE de cookie (`web.app.COOKIE_VER`): `_sesion` no la lee;
#   2. otro FORMATO (`ver.<vence>.<firma>`: lleva la palabra, no un chat), así
#      que `validar` no la acepta como sesión de la casa;
#   3. otra FIRMA: lo firmado lleva un prefijo propio (`_PREFIJO_VER`), así que
#      ni copiando la firma de un token a otro se convierte uno en el otro.
# `puede_entrar` no mira esta cookie nunca: la lista de quién ve las finanzas
# sigue siendo la misma de siempre.

# 12 horas, y se escribe DENTRO del token (no en `Max-Age`): la cookie muere al
# cerrar el navegador y, aunque no muriera, el token vence solo.
VIDA_SESION_CDS = 12 * 3600

_PREFIJO_VER = "solo-ver|"
_MARCA_VER = "ver"


def crear_token_ver(vida: int = VIDA_SESION_CDS) -> str:
    """Token de solo ver. Formato: ver.vence.firma. No dice de quién es."""
    vence = int(time.time()) + vida
    payload = f"{_MARCA_VER}.{vence}"
    return f"{payload}.{_firmar(_PREFIJO_VER + payload)}"


def validar_ver(token: str | None) -> bool:
    """True si es un token de solo ver, bien firmado y sin vencer.

    Un token de la sesión de la casa (`chat.vence.firma`) NO pasa: ni por el
    formato ni por la firma, que está hecha con otro prefijo."""
    if not token or token.count(".") != 2:
        return False
    marca, vence, firma = token.split(".")
    if marca != _MARCA_VER:
        return False
    if not hmac.compare_digest(firma, _firmar(_PREFIJO_VER + f"{marca}.{vence}")):
        return False
    try:
        return int(vence) >= time.time()
    except ValueError:
        return False


# ── Qué puerta tiene cada ruta ────────────────────────────────────────────────
# Cada ruta del panel DECLARA su puerta con `@puerta(...)`, y una prueba recorre
# `app.routes` (lo registrado, no una lista tecleada) y falla con cualquier
# ruta sin declarar o que se porte distinto de lo que declara.
PUERTA_SIEMPRE = "siempre"      # la sesión de la casa (`puede_entrar`) y nada más
PUERTA_VER = "ver"              # la de la casa o la de solo ver; solo lecturas
PUERTA_ENTRADA = "entrada"      # entra con un enlace o un boleto, no con cookie
PUERTA_CODE = "code"            # `/api/code/*`: su propia clave, nunca cookie
PUERTAS = (PUERTA_SIEMPRE, PUERTA_VER, PUERTA_ENTRADA, PUERTA_CODE)


def puerta(nombre: str):
    """Marca el endpoint con su puerta. No cambia la función."""
    if nombre not in (PUERTA_SIEMPRE, PUERTA_VER, PUERTA_ENTRADA):
        raise ValueError(f"puerta desconocida: {nombre!r}")

    def marcar(fn):
        fn.puerta = nombre
        return fn
    return marcar


# ── Qué sesión se da a quien llega con un boleto de la App ────────────────────
# La App dice EL TECHO (`total` o `ver`); Lucy da el MÍNIMO entre ese techo y lo
# que ella misma ya permite. `total` solo vale si Lucy conoce a esa persona
# (`puede_entrar`): para escribir en Proyectos hace falta estar en su lista, y
# además la base lo vuelve a exigir al comentar.
NIVEL_TOTAL = "total"
NIVEL_VER = "ver"
SESION_CASA = "casa"
SESION_VER = "ver"


def sesion_para(nivel: str, chat_id: int | None) -> str:
    """`SESION_CASA` solo con techo `total` Y un chat que `puede_entrar` conoce;
    todo lo demás es `SESION_VER`. Es la única puerta de esta decisión."""
    if nivel == NIVEL_TOTAL and puede_entrar(chat_id):
        return SESION_CASA
    return SESION_VER
