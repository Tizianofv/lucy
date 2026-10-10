"""Las sesiones y trabajos que el registro del estudio tiene a nombre de un cliente.

Hermano de `noco_lectura.py`: este archivo le PIDE al registro las sesiones de
una ficha, y solo lee (el otro camino de Lucy hacia la App —el POST que canjea
el pase de `/entrar-cds`— vive en `web/app.py`: `_canjear_boleto`). La dirección
sale de `config.REGISTRO_URL` y la llave de `config.LUCY_LLAVE_SERVICIO` (la
misma variable en los dos servicios); la llave viaja en la cabecera
`X-Lucy-Llave`, nunca en la URL ni en un mensaje. `_get` es la única función de
este archivo que toca la red, y solo sabe hacer GET.

Lo que devuelve `sesiones_de_cliente` es un diccionario con su `estado`: `ok`
(con las dos listas y los totales), `sin_cliente` (el proyecto no tiene ficha a
la que preguntar), `sin_ligar` (la ficha no se puede ligar, con su motivo) o
`no_se_pudo` (no hay llave, la App no contestó o contestó algo que no se
entiende). Los cuatro se dicen distinto en la página; ninguno se disfraza de
«no tiene sesiones».

El dinero lo calcula la App; acá solo se decide cómo se pinta (`dinero_de_sesion`,
la tabla del diseño 3.8) y cuánto suman los renglones pintados
(`totales_de_sesiones`). Las dos son funciones puras, sin red ni base.

Desde la parte 11, `con_las_quitadas` es la otra regla pura del bloque: saca de
las listas y de los totales las sesiones que la casa quitó de ESTE proyecto, y
arma la lista «Quitadas» para devolverlas (solo con las que la App devuelve en
esa lectura). `sesion_por_ref` busca una sesión entre lo que la App devolvió; es
lo único para lo que sirve el identificador que llega del formulario.
"""
from __future__ import annotations

import logging
import math
from datetime import date
from decimal import Decimal, InvalidOperation

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


def _ref(valor) -> str | None:
    """El identificador de la sesión en texto, o None si no sirve.

    Es el `ref` que manda la App (el Id de su fila), que llega como número: se
    pasa a texto para poder compararlo con lo guardado (`sesion_ref` es TEXT) y
    para que el formulario de quitar lo lleve tal cual. Un texto se guarda sin
    espacios de alrededor; lo que no sea un número entero ni un texto, None.
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, str):
        return valor.strip() or None
    return None


def _dinero(valor) -> Decimal | None:
    """Una cifra de dinero como `Decimal`, sin pasar por `float` al hacer
    cuentas; lo que no es un número —o no es finito—, None («no vino»).

    `Decimal(str(valor))` y no `Decimal(valor)`: el segundo arrastra lo que el
    `float` guarda de verdad (2000.1 no es 2000.1), y esas sobras hacen que dos
    cifras que la App manda cuadradas acá no cuadren.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def dinero_de_sesion(total, abonado, saldo) -> dict:
    """Qué se pinta en la columna de dinero de una sesión (diseño 3.8), y qué
    aporta a los totales del bloque.

    Verde con el total cuando no debe nada; rojo con lo que debe cuando debe, y
    con «pagó X de TOTAL» debajo si ya pagó una parte; «—» cuando todavía no hay
    total. Las tres cifras tienen que venir, no ser negativas y cuadrar
    (`abonado + saldo == total`): lo que no encaja sale con sus cifras sin color
    y con «revísala en el registro», sin adivinar ninguno.
    """
    if total is None or total == 0:
        return _lo_que_se_pinta("sin_total")
    cuadra = (abonado is not None and saldo is not None and total > 0
              and abonado >= 0 and saldo >= 0 and abonado + saldo == total)
    if not cuadra:
        return _lo_que_se_pinta("revisar", total, abonado, saldo)
    if saldo == 0:
        return _lo_que_se_pinta("pago", total, abonado, saldo)
    if abonado == 0:
        return _lo_que_se_pinta("debe", total, abonado, saldo)
    return _lo_que_se_pinta("debe_con_abono", total, abonado, saldo)


def _lo_que_se_pinta(caso, total=None, abonado=None, saldo=None) -> dict:
    """La forma que la página pinta para un caso, con lo que el renglón aporta a
    los totales (`aporta`: facturado, cobrado y por cobrar, en ese orden).

    Un renglón sin total no aporta nada: no pintó ninguna cifra.
    """
    if caso == "sin_total":
        return {"caso": caso, "clase": "", "monto": None,
                "total": None, "abonado": None, "cifras": [], "aporta": (None, None, None)}
    if caso == "revisar":
        cifras = [c for c in (total, abonado, saldo) if c is not None]
        return {"caso": caso, "clase": "", "monto": None,
                "total": total, "abonado": abonado, "cifras": cifras,
                "aporta": (total, abonado, saldo)}
    return {"caso": caso, "clase": "pago" if caso == "pago" else "debe",
            "monto": total if caso == "pago" else saldo,
            "total": total, "abonado": abonado, "cifras": [],
            "aporta": (total, abonado, saldo)}


def totales_de_sesiones(renglones) -> dict:
    """Los tres totales del bloque (facturado, cobrado, por cobrar): la suma de
    lo que aporta cada renglón pintado. Las quitadas y las canceladas no llegan
    hasta acá, así que no cuentan."""
    facturado, cobrado, por_cobrar = Decimal(0), Decimal(0), Decimal(0)
    for renglon in renglones:
        aporta = renglon["dinero"]["aporta"]
        facturado += aporta[0] or 0
        cobrado += aporta[1] or 0
        por_cobrar += aporta[2] or 0
    return {"facturado": facturado, "cobrado": cobrado, "por_cobrar": por_cobrar}


def _renglon(fila) -> dict | None:
    """Un renglón con SOLO lo que la página pinta, o None si no sirve.

    El `ref` entra desde la parte 11: es con lo que se guarda la decisión de
    quitarla del proyecto, y viaja al formulario de quitar. Lo que no entra acá
    no puede pintarse por descuido desde la plantilla: un renglón sin `ref` se
    pinta igual, solo que no se puede quitar. Una sesión cancelada no sale.
    """
    if not isinstance(fila, dict) or fila.get("cancelada"):
        return None
    total = _dinero(fila.get("total"))
    abonado = _dinero(fila.get("abonado"))
    saldo = _dinero(fila.get("saldo"))
    return {
        "ref": _ref(fila.get("ref")),
        "fecha": _dia(fila.get("fecha")),
        "sala": _texto(fila.get("sala_mostrar")) or _texto(fila.get("sala")),
        "horas": _horas(fila.get("horas")),
        "concepto": _texto(fila.get("servicio")),
        "codigo": _texto(fila.get("codigo")),
        "atendio": _texto(fila.get("atendio")),
        "asignado": _texto(fila.get("asignado_a")),
        "estado": _texto(fila.get("estado")),
        "es_trabajo": fila.get("es_trabajo") is True,
        "total": total, "abonado": abonado, "saldo": saldo,
        "dinero": dinero_de_sesion(total, abonado, saldo),
    }


def _vacio(estado: str, motivo: str = "") -> dict:
    """La respuesta sin renglones, con su estado y su motivo."""
    return {"estado": estado, "motivo": motivo, "sesiones": [], "trabajos": [],
            "totales": totales_de_sesiones([])}


async def sesiones_de_cliente(noco_id) -> dict:
    """Las sesiones y los trabajos de esa ficha del cliente, ya partidos en dos
    listas por `es_trabajo`, con los tres totales del bloque.

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
            "trabajos": [r for r in renglones if r["es_trabajo"]],
            "totales": totales_de_sesiones(renglones)}


def sesion_por_ref(respuesta, ref) -> dict | None:
    """El renglón de la sesión con ESE identificador, de las dos listas, o None.

    Es la puerta con la que la página vuelve a encontrar la sesión que se pide
    quitar: el identificador que llega del formulario solo sirve para BUSCAR
    entre lo que la App devolvió, nunca para guardarse tal cual.
    """
    buscado = _ref(ref)
    if buscado is None:
        return None
    for renglon in respuesta["sesiones"] + respuesta["trabajos"]:
        if renglon["ref"] == buscado:
            return renglon
    return None


def con_las_quitadas(respuesta, quitadas) -> dict:
    """La respuesta del bloque con las sesiones que la casa quitó, fuera de las
    listas Y de los totales, y con la lista «Quitadas» para devolverlas.

    `quitadas` es `{ref: codigo}` de las decisiones vivas de ESTE proyecto, o
    `None` si la tabla todavía no existe (la migración sin aplicar): entonces no
    se filtra nada, no sale ninguna lista de quitadas y `quitadas_disponibles`
    queda falso, así que la página no dibuja ningún control. Un diccionario
    vacío es otra cosa: la tabla está y este proyecto no tiene ninguna quitada.

    En «Quitadas» sale SOLO lo que la App devolvió en ESTA lectura (diseño 3.4:
    si la App ya no la devuelve, la fila de la quitada no hace nada y no se ve),
    y con la App sin contestar no sale ninguna. El código guardado al quitarla no
    se pinta: queda en la tabla para lo que lo necesite después.
    """
    if quitadas is None:
        return {**respuesta, "quitadas": [], "quitadas_disponibles": False}
    salida = {**respuesta, "quitadas": [], "quitadas_disponibles": True}
    if respuesta["estado"] != "ok":
        # Sin la respuesta de la App no hay renglones que filtrar ni nada que
        # nombrar: no se afirma qué se quitó.
        return salida
    todas = respuesta["sesiones"] + respuesta["trabajos"]
    pintadas = [r for r in todas if r["ref"] not in quitadas]
    de_la_app = {r["ref"]: r for r in todas if r["ref"] is not None}
    salida["sesiones"] = [r for r in pintadas if not r["es_trabajo"]]
    salida["trabajos"] = [r for r in pintadas if r["es_trabajo"]]
    salida["totales"] = totales_de_sesiones(pintadas)
    salida["quitadas"] = [{"ref": ref, "renglon": de_la_app[ref]}
                          for ref in quitadas if ref in de_la_app]
    return salida
