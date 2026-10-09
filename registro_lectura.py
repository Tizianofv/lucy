"""Las sesiones y trabajos que el registro del estudio tiene a nombre de un cliente.

Hermano de `noco_lectura.py`: este archivo le PIDE al registro las sesiones de
una ficha, y solo lee (el otro camino de Lucy hacia la App —el POST que canjea
el pase de `/entrar-cds`— vive en `web/app.py`: `_canjear_boleto`). La dirección
sale de `config.REGISTRO_URL` y la llave de `config.LUCY_LLAVE_SERVICIO` (la
misma variable en los dos servicios); la llave viaja en la cabecera
`X-Lucy-Llave`, nunca en la URL ni en un mensaje. `_get` es la única función de
este archivo que toca la red, y solo sabe hacer GET.

Lo que devuelve `sesiones_de_cliente` es un diccionario con su `estado`: `ok`
(con las dos listas), `sin_cliente` (el proyecto no tiene ficha a la que
preguntar), `sin_ligar` (la ficha no se puede ligar, con su motivo) o
`no_se_pudo` (no hay llave, la App no contestó o contestó algo que no se
entiende). Los cuatro se dicen distinto en la página; ninguno se disfraza de
«no tiene sesiones».
"""
from __future__ import annotations

import logging
from datetime import date

import httpx

import config

log = logging.getLogger("lucy.registro")

# Cuánto se espera al registro, el mismo tope que el lector de Noco: es una
# persona con la página de un proyecto abierta. Elegido, no medido.
TIEMPO_LIMITE = 8.0


class RegistroNoContesta(RuntimeError):
    """El registro no contestó, contestó algo que no se entiende, o falta configurarlo."""


def _configurado() -> tuple[str, str]:
    """(dirección, llave) del registro, leídas AHORA. El mensaje nombra la
    variable que falta, nunca su valor."""
    base = config.REGISTRO_URL
    llave = config.LUCY_LLAVE_SERVICIO
    if not base.startswith(("http://", "https://")):
        raise RegistroNoContesta("falta REGISTRO_URL")
    if not llave:
        raise RegistroNoContesta("falta LUCY_LLAVE_SERVICIO")
    return base, llave


async def _get(parametros: dict[str, str]) -> dict:
    """La única puerta de salida: un GET a `/api/lucy/sesiones` del registro.

    La llave va en la cabecera y en ningún otro sitio, y `follow_redirects=False`
    va escrito: una redirección no se sigue, así que la llave no puede terminar
    en otro servidor. Un fallo al pedir —el de la red o el de una dirección mal
    escrita— sale como `RegistroNoContesta`, y en el log de Lucy solo queda el
    TIPO del fallo: ni la llave ni el texto crudo del error se escriben.
    """
    base, llave = _configurado()
    try:
        async with httpx.AsyncClient(timeout=TIEMPO_LIMITE,
                                     follow_redirects=False) as cliente:
            respuesta = await cliente.get(
                f"{base}/api/lucy/sesiones", params=parametros,
                headers={"X-Lucy-Llave": llave, "Accept": "application/json"})
    except Exception as e:                                    # noqa: BLE001
        log.warning("el registro no contestó (%s)", type(e).__name__)
        raise RegistroNoContesta("no pude hablar con el registro") from e
    if respuesta.status_code != 200:
        log.warning("el registro contestó %s", respuesta.status_code)
        raise RegistroNoContesta(f"el registro contestó {respuesta.status_code}")
    try:
        datos = respuesta.json()
    except ValueError as e:
        log.warning("el registro contestó algo que no es JSON")
        raise RegistroNoContesta("el registro contestó algo que no entiendo") from e
    if not isinstance(datos, dict):
        log.warning("el registro contestó un JSON con otra forma")
        raise RegistroNoContesta("el registro contestó algo que no entiendo")
    return datos


def _texto(valor) -> str | None:
    """El texto de un campo, sin espacios de alrededor; lo demás, None."""
    if not isinstance(valor, str):
        return None
    return valor.strip() or None


def _dia(valor) -> date | None:
    """Un día «AAAA-MM-DD» como `date`; lo que no se lea como un día, None."""
    if not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor.strip())
    except ValueError:
        return None


def _horas(valor) -> str | None:
    """Las horas de un renglón, como texto; lo que no sea un número, None."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return f"{valor:g}"


def _renglon(fila) -> dict | None:
    """Un renglón con SOLO lo que la página pinta, o None si no sirve.

    Deja caer a propósito el dinero (`total`, `abonado`, `saldo`) y el `ref`:
    esta parte no los pinta, y lo que no entra acá no puede pintarse por
    descuido desde la plantilla. Una sesión cancelada no sale.
    """
    if not isinstance(fila, dict) or fila.get("cancelada"):
        return None
    return {
        "fecha": _dia(fila.get("fecha")),
        "sala": _texto(fila.get("sala_mostrar")) or _texto(fila.get("sala")),
        "horas": _horas(fila.get("horas")),
        "concepto": _texto(fila.get("servicio")),
        "codigo": _texto(fila.get("codigo")),
        "atendio": _texto(fila.get("atendio")),
        "asignado": _texto(fila.get("asignado_a")),
        "estado": _texto(fila.get("estado")),
        "es_trabajo": fila.get("es_trabajo") is True,
    }


def _vacio(estado: str, motivo: str = "") -> dict:
    """La respuesta sin renglones, con su estado y su motivo."""
    return {"estado": estado, "motivo": motivo, "sesiones": [], "trabajos": []}


async def sesiones_de_cliente(noco_id) -> dict:
    """Las sesiones y los trabajos de esa ficha del cliente, ya partidos en dos
    listas por `es_trabajo`.

    Sin ficha (`None` o algo que no es un Id) no le pregunta a nadie: un
    proyecto sin cliente no tiene de quién traerlas.
    """
    if isinstance(noco_id, bool) or not isinstance(noco_id, int) or noco_id <= 0:
        return _vacio("sin_cliente")
    try:
        datos = await _get({"persona": str(noco_id)})
    except RegistroNoContesta:
        return _vacio("no_se_pudo")
    puede_ligar = datos.get("puede_ligar")
    filas = datos.get("sesiones")
    if not isinstance(puede_ligar, bool) or not isinstance(filas, list):
        log.warning("el registro contestó una forma que no conozco")
        return _vacio("no_se_pudo")
    if not puede_ligar:
        return _vacio("sin_ligar", _texto(datos.get("motivo_sin_ligar")) or "")
    renglones = [r for r in (_renglon(fila) for fila in filas) if r]
    if len(renglones) != len(filas):
        log.info("el registro mandó %d renglón(es) que no se pintan de %d",
                 len(filas) - len(renglones), len(filas))
    return {"estado": "ok", "motivo": "",
            "sesiones": [r for r in renglones if not r["es_trabajo"]],
            "trabajos": [r for r in renglones if r["es_trabajo"]]}
