"""Copia al dueño: todo lo que Lucy le manda a Tiziano también le llega,
con el mismo texto, a quien diga `config.chats_de_copia()`.

Pedido de Tiziano, 21-sep-2026: «lo que me llega a mí también le llega a
Rosi, nada cambiado, simplemente lo mismo» / «no importa si me llega algo
más quiero que le lleguen a ella también» / «exacto, todo».

POR QUÉ ACÁ Y NO EN CADA LLAMADA. Medido el 21-sep-2026: hay varios caminos
que le escriben al dueño sin pasar por `cerebro.agente._enviar`
(`cerebro/despertador.py:109`; y el «✅ Recibí» de `captura/telegram.py:34`,
que sale por `Message.reply_text`, no por `bot.send_message`). Copiar en
cada llamada obliga a que la próxima que alguien escriba mañana se acuerde
de hacerlo. Y hay algo peor: `tests/test_cerrar_varias.py::
test_ninguna_salida_de_atender_esquiva_la_puerta` ya deja medido que un
`telegram.Bot(token=...)` armado a mano —un cliente propio, no el que arma
`main.py`— esquiva CUALQUIER vigilancia atada a un nombre de función o a una
instancia concreta.

LA PUERTA DE VERDAD, medida sobre `telegram` 21.9
(`site-packages/telegram/_message.py:1771` y
`site-packages/telegram/ext/_extbot.py:2939,2963`): `ExtBot.send_message`
termina en `super().send_message(...)`, y `Message.reply_text` termina en
`self.get_bot().send_message(...)` — el mismo `get_bot()` que Telegram le
puso a cada `Message` al parsear el `Update`. Los dos caminos, y cualquier
`telegram.Bot(...)` que alguien arme, convergen en UNA sola función:
`telegram.Bot.send_message`. Ésa es la puerta, y por eso se parcha la CLASE,
no una instancia: `Bot` usa `__slots__` y ni siquiera lo permitiría
(comprobado: `bot.send_message = lo_que_sea` levanta
`AttributeError: Attribute 'send_message' of class 'Bot' can't be set!`).

LA FRONTERA, dicha en vez de prometida: esto cubre todo lo que hable con
Telegram A TRAVÉS de la clase `telegram.Bot` (directo o vía `reply_text`).
Lo que le hablara a la API de Telegram SIN pasar por esa clase —un
`httpx.post` a la brava contra `api.telegram.org`— se escaparía. Medido el
21-sep-2026: no hay ningún camino así en el repo
(`grep -rn "api.telegram.org" --include="*.py"` fuera de `site-packages` no
devuelve nada). Si alguna vez aparece uno, esta puerta no lo va a ver.

Y el alcance es SOLO `send_message` —mensajes de texto—, a propósito: es lo
único que Lucy manda hoy (`git grep` de `send_` en todo el código no
encuentra `send_photo` ni `send_document` fuera de esta librería). El día
que mande una foto o un archivo, eso no se copia hasta que alguien lo agregue
acá.
"""
from __future__ import annotations

import contextvars
import logging

import telegram

import config
import db.db as db

log = logging.getLogger("lucy.copia_dueno")

# LA SALIDA, y por qué es un contextvar y no un kwarg de `send_message`.
# Se probó pasar `sin_copia=True` como kwarg del `bot.send_message(...)` de
# `_enviar` y se midió que revienta: el objeto real que arma `main.py` es un
# `ExtBot`, y `ExtBot.send_message` (`telegram/ext/_extbot.py:2939`) tiene su
# propia firma, SIN `**kwargs` de sobra — cualquier nombre que no reconozca
# lanza `TypeError` ANTES de llegar a esta puerta, porque `ExtBot.send_message`
# se resuelve primero que `Bot.send_message` (es la que Python encuentra por
# herencia) y recién ADENTRO llama a `super().send_message(...)`, que es donde
# vive el parche. Un contextvar no toca esa firma: viaja con la corrutina
# (mismo `await`, mismo Task) sin pasar por ningún parámetro de Telegram.
_sin_copia = contextvars.ContextVar("copia_dueno_sin_copia", default=False)


class sin_copiar:
    """Gestor de contexto: los envíos al dueño hechos DENTRO del bloque no
    se copian.

    Uso — `cerebro/agente.py`, herramienta "panel":

        with copia_dueno.sin_copiar():
            await _enviar(bot, texto, ...)

    Sirve para lo que un `Bot.send_message` no puede decidir por sí solo
    (el enlace del panel entra COMO quien lo pidió; copiarlo sería mandarle
    a otra persona una llave a nombre de otro) sin ensuciar la firma de
    ningún método de la librería de Telegram.
    """

    def __enter__(self):
        self._token = _sin_copia.set(True)
        return self

    def __exit__(self, *exc_info):
        _sin_copia.reset(self._token)
        return False

# El texto que se le manda al dueño NO cambia (Tiziano: «nada cambiado,
# simplemente lo mismo»). Este marcador NO viaja por Telegram: solo se
# guarda en la memoria de la conversación de la persona copiada, para que
# Lucy sepa que es un mensaje que le mandó a Tiziano y no una charla propia
# de ella. Ver `_copiar`.
_MARCADOR_MEMORIA = "[Esto se lo mandé a Tiziano; te lo copio a vos también]"

# Kwargs que tienen sentido para un mensaje NUEVO al dueño pero no para su
# copia: el botón «Deshacer» no hace nada en un chat ajeno
# (`acciones/botones.py`, el callback vuelve con el `bandeja_id` del dueño),
# y citar un mensaje que Rosi nunca vio no es una respuesta, es confuso.
_KWARGS_QUE_NO_SE_COPIAN = ("reply_markup", "reply_parameters", "reply_to_message_id")

_instalado = False
_original_send_message = None  # se fija en instalar(); la función SIN parchar.


def instalar() -> None:
    """Pone el parche en `telegram.Bot.send_message`. Idempotente.

    Se llama UNA vez, al arrancar (`main.py`), antes de que Lucy pueda
    mandar el primer mensaje. Como el parche vive en la clase y no en una
    instancia, el ORDEN respecto de `ApplicationBuilder().build()` no
    importa: lo único que importa es que esto corra antes del primer envío.
    """
    global _instalado, _original_send_message
    if _instalado:
        return
    _original_send_message = telegram.Bot.send_message
    telegram.Bot.send_message = _send_message_con_copia
    _instalado = True
    log.info("Copia al dueño instalada sobre telegram.Bot.send_message.")


def desinstalar() -> None:
    """Solo para pruebas: deja `telegram.Bot.send_message` como estaba."""
    global _instalado
    if not _instalado or _original_send_message is None:
        return
    telegram.Bot.send_message = _original_send_message
    _instalado = False


async def _send_message_con_copia(self, chat_id=None, text=None, **kwargs):
    """El reemplazo de `telegram.Bot.send_message`. Manda, y si iba al
    dueño, copia.

    Si el envío al dueño falla (excepción de `_original_send_message`), esa
    excepción se propaga tal cual y NUNCA se llega a copiar — no hay nada
    que copiar todavía. `cerebro.agente._enviar` ya sabe reintentar en
    texto plano si Telegram rechaza el HTML; ese reintento vuelve a pasar
    por acá y copia en el segundo intento si el primero falló.
    """
    resultado = await _original_send_message(self, chat_id, text, **kwargs)
    if chat_id == config.CHAT_ID_DUENO and not _sin_copia.get():
        await _copiar(self, text, kwargs)
    return resultado


async def _copiar(bot, text, kwargs) -> None:
    """Manda `text` a cada chat de `config.chats_de_copia()`, y lo anota en
    su memoria.

    Cada destino es independiente: si uno falla, el registro lo dice y se
    sigue con el siguiente — nunca se reintenta (decisión del diseño: al
    dueño ya le llegó el suyo, y una copia vieja reintentada tarde es peor
    que una copia perdida y anotada).
    """
    destinos = config.chats_de_copia()
    if not destinos:
        return
    copia_kwargs = {k: v for k, v in kwargs.items()
                     if k not in _KWARGS_QUE_NO_SE_COPIAN}
    for destino in destinos:
        try:
            await _original_send_message(bot, destino, text, **copia_kwargs)
        except Exception:
            log.error("No pude copiarle al chat %s el mensaje que le mandé "
                      "al dueño; no se reintenta.", destino, exc_info=True)
            continue
        try:
            await db.registrar_aviso(
                destino, f"{_MARCADOR_MEMORIA}\n\n{text}", origen="copia_dueno")
        except Exception:
            log.error("Copié al chat %s pero no pude anotarlo en su "
                      "memoria.", destino, exc_info=True)
