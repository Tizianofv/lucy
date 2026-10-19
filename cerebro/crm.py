"""`consultar_crm`: lo que el equipo le pregunta a Lucy sobre el registro de clientes.

Lucy no contesta estas preguntas: se las pasa a un módulo aparte (su dirección y su
llave vienen de `config.CRM_MODULO_URL` y `config.CRM_MODULO_LLAVE`) y lleva lo que el
módulo dice, tal cual, al chat que preguntó. Este archivo es el único sitio de Lucy que
habla con ese módulo, y no sabe nada de él más que el contrato de la red:

    POST {CRM_MODULO_URL}/lucy/consulta   cabecera con la llave
    {"chat_id": <número>, "texto": "<lo que escribió la persona>"}
    200 {"texto": "...", "propuesta": null | {...}}

Lo que se promete, y qué lo vigila (`tests/test_consultar_crm.py`):

  · La respuesta NO pasa por el modelo de Lucy ni se guarda en ningún sitio de Lucy.
    Sale de `preguntar` y entra a `enviar` (Telegram); en la base solo se escribe una
    MARCA («[contestó el CRM]»), nunca el texto. El modelo ve la pregunta y la marca.
  · Va solo al chat que preguntó, y sin copia a nadie (`copia_dueno.sin_copiar`): cada
    uno ve lo que pregunta.
  · La espera NO frena el bucle de Lucy: la llamada vive en una tarea aparte (`lanzar`),
    que el turno no espera.
  · Si el módulo no contesta (tope vencido, 401, 403, 5xx, red caída, algo que no es lo
    del contrato) la persona recibe una frase fija. Nunca una respuesta inventada.
  · La llave va en la cabecera y no se sigue ninguna redirección, así que no puede
    terminar en otro servidor. Ni la llave ni el texto de la pregunta o de la respuesta
    se escriben en el registro de Lucy: solo el TIPO del fallo y el número de estado.

La pieza es de SOLO LECTURA. El campo `propuesta` de la respuesta (escrituras que vendrán
con otra pieza) se ignora.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

import cerebro.copia_dueno as copia_dueno
import config
import db.db as db

log = logging.getLogger("lucy.crm")

# Lo que ve la persona mientras el módulo trabaja.
FRASE_BUSCANDO = "🔎 Buscando en el registro…"
# Lo que ve cuando el módulo no contesta, sea por lo que sea.
FRASE_NO_CONTESTO = "El registro no me contestó ahora; intenta en un rato"

# Lo que queda en `bandeja.respuesta_lucy` (y por eso lo único que el modelo de Lucy
# y la memoria de largo plazo pueden ver de esta consulta).
MARCA_CONSULTANDO = "[consultando el CRM]"
MARCA_CONTESTO = "[contestó el CRM]"
MARCA_SIN_ENTREGAR = "[el CRM contestó y no pude entregar la respuesta]"

# Cuánto espera Lucy al módulo. El módulo tiene el suyo (120 s) y el de Lucy es mayor, para
# que Lucy oiga la causa que el módulo da antes de rendirse. De diseño, no medido.
TOPE_S = 150.0

CABECERA_LLAVE = "X-Lucy-Llave"
RUTA = "/lucy/consulta"

# Cuántos caracteres caben en un mensaje de Telegram, contados en unidades UTF-16 y con
# margen (el tope del Bot API es 4096).
LARGO_MAX = 4000

# Los canales por los que una PERSONA le pregunta algo a Lucy. Una foto es el texto que
# otro modelo leyó; un correo o un encargo de la maquinaria no son preguntas de nadie: por
# ahí no se abre el registro de clientes.
CANALES_QUE_PREGUNTAN = ("texto", "audio")


class ModuloNoContesta(RuntimeError):
    """El módulo no contestó, contestó algo que no es lo del contrato, o falta configurarlo."""


def se_puede(chat_id, tipo_entrada: str) -> bool:
    """¿Este turno puede preguntarle al registro? Solo una persona de la casa, por texto o voz.

    `config.CHAT_IDS_PERMITIDOS` se lee AHORA, no al importar.
    """
    return chat_id in config.CHAT_IDS_PERMITIDOS and tipo_entrada in CANALES_QUE_PREGUNTAN


async def preguntar(chat_id: int, texto: str) -> str:
    """Le pregunta al módulo y devuelve su `texto`, o lanza `ModuloNoContesta`.

    La pregunta viaja en el cuerpo, nunca en la dirección. Una redirección no se sigue.
    El mensaje de la excepción nombra la variable que falta o el estado recibido, nunca
    la llave ni lo que dijo el módulo.
    """
    base, llave = config.CRM_MODULO_URL, config.CRM_MODULO_LLAVE
    if not base.startswith(("http://", "https://")):
        raise ModuloNoContesta("falta CRM_MODULO_URL")
    if not llave:
        raise ModuloNoContesta("falta CRM_MODULO_LLAVE")
    try:
        async with httpx.AsyncClient(timeout=TOPE_S, follow_redirects=False) as cliente:
            # El tope es de la llamada ENTERA: el de httpx es por fase y no basta.
            respuesta = await asyncio.wait_for(
                cliente.post(f"{base}{RUTA}",
                             json={"chat_id": chat_id, "texto": texto},
                             headers={CABECERA_LLAVE: llave, "Accept": "application/json"}),
                TOPE_S)
    except Exception as e:                                    # noqa: BLE001
        log.warning("el módulo del CRM no contestó (%s)", type(e).__name__)
        raise ModuloNoContesta("no pude hablar con el módulo") from e
    if respuesta.status_code != 200:
        log.warning("el módulo del CRM contestó %s", respuesta.status_code)
        raise ModuloNoContesta(f"el módulo contestó {respuesta.status_code}")
    try:
        datos = respuesta.json()
    except ValueError as e:
        log.warning("el módulo del CRM contestó algo que no es JSON")
        raise ModuloNoContesta("el módulo contestó algo que no entiendo") from e
    texto_de_vuelta = datos.get("texto") if isinstance(datos, dict) else None
    if not isinstance(texto_de_vuelta, str) or not texto_de_vuelta.strip():
        log.warning("el módulo del CRM contestó sin texto")
        raise ModuloNoContesta("el módulo contestó algo que no entiendo")
    return texto_de_vuelta


def _u16(texto: str) -> int:
    return len(texto.encode("utf-16-le")) // 2


def pedazos(texto: str, tope: int = LARGO_MAX) -> list[str]:
    """`texto` en pedazos de a lo sumo `tope` unidades UTF-16, que pegados dan el texto.

    Corta en el último salto de línea de la ventana si lo hay y, si no, donde llega el tope.
    Un pedazo que quedaría solo con blancos no se devuelve (Telegram no lo acepta).
    """
    salida: list[str] = []
    resto = texto
    while resto:
        if _u16(resto) <= tope:
            salida.append(resto)
            break
        fin = tope
        while _u16(resto[:fin]) > tope:
            fin -= 1
        corte = resto.rfind("\n", 0, fin)
        corte = corte + 1 if corte > 0 else fin
        salida.append(resto[:corte])
        resto = resto[corte:]
    return [p for p in salida if p.strip()]


_en_vuelo: set[asyncio.Task] = set()


def lanzar(enviar, chat_id: int, texto: str, bandeja_id: int) -> asyncio.Task:
    """Arranca la consulta en una tarea APARTE y vuelve ya: el bucle de Lucy no la espera.

    `enviar(texto, **kw)` es la puerta de Telegram del turno (`agente._enviar` con el bot).
    La tarea se guarda en `_en_vuelo` para que nadie la recoja antes de que termine.
    """
    tarea = asyncio.create_task(_atender(enviar, chat_id, texto, bandeja_id))
    _en_vuelo.add(tarea)
    tarea.add_done_callback(_en_vuelo.discard)
    return tarea


async def _atender(enviar, chat_id: int, texto: str, bandeja_id: int) -> None:
    """Pregunta, entrega la respuesta al chat que preguntó y deja solo la marca."""
    try:
        respuesta = await preguntar(chat_id, texto)
        marca = MARCA_CONTESTO
    except ModuloNoContesta:
        respuesta = FRASE_NO_CONTESTO
        marca = FRASE_NO_CONTESTO
    try:
        # Sin copia a nadie: cada uno ve lo que pregunta. El bloque está DENTRO de la tarea
        # porque el contexto de `sin_copiar` no viaja de una tarea a otra.
        with copia_dueno.sin_copiar():
            for pedazo in pedazos(respuesta):
                await enviar(pedazo, chat_id=chat_id)
    except Exception as e:                                    # noqa: BLE001
        log.warning("no pude entregar la respuesta del CRM (%s)", type(e).__name__)
        marca = MARCA_SIN_ENTREGAR
    try:
        await db.guardar_respuesta(bandeja_id, marca)
    except Exception as e:                                    # noqa: BLE001
        log.warning("no pude dejar la marca de la consulta #%s (%s)",
                    bandeja_id, type(e).__name__)
