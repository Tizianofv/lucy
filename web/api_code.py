"""La puerta HTTP de las tareas de Code (26-sep-2026, diseño aprobado por
Tiziano: disenos/lucy-code/DISENO.md, §C — parte 2 del plan de construcción).

Reemplaza que la sala lea/escriba la base de Lucy directo con
`railway run -s Postgres`. Vive en el MISMO proceso FastAPI que el panel
(`web/app.py` la monta con `app.include_router`), pero es una puerta
COMPLETAMENTE APARTE: nunca toca `web.auth` ni la cookie `lucy_panel`. El
panel es para una persona con un chat de Telegram abriendo un navegador; la
sala y Natalia son programas llamando una API, y no tienen ninguna de las
dos cosas.

RUTAS DE ESTA PARTE, y solo éstas: `GET /tareas` (listar) y
`POST /tareas/{id}/cerrar`. NO hay ruta de "tomar" ni de "alertas": esas
partes del diseño (§D, §B) todavía no se construyeron, y una ruta a medias
—que exista pero siempre falle, o que acepte un permiso que nada usa— es
peor que no tenerla: alguien podría configurarle una clave a Natalia con
"alertas:crear" y pensar que ya funciona.

EL REPO ES PÚBLICO. Ninguna clave real vive acá ni en ningún archivo del
repo — las variables `CLAVES_API_CODE`/`PERMISOS_API_CODE` las pone la sala
en Railway, con permiso de Tiziano (ver `config.py` para el formato exacto).
Este archivo no las pone, no las adivina, y nunca imprime una clave — ni
completa ni cortada — en ningún log.

CERRADO POR DEFECTO: si esas dos variables no existen o no tienen ninguna
pieza legible, `config.CLAVES_API_CODE`/`config.PERMISOS_API_CODE` quedan
vacíos y TODO pedido a esta puerta es 401, sin que este archivo tenga que
hacer nada especial para lograrlo — es la misma rama que una clave
inventada.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

import config
import db.db as db

log = logging.getLogger("lucy.api_code")

router = APIRouter(prefix="/api/code")

# ── Comparación en tiempo constante ───────────────────────────────────────
#
# Nunca `clave in config.CLAVES_API_CODE` ni `==` sobre el texto: un `==`
# normal falla más rápido cuanto antes difiere, y ese tiempo es información.
# Mismo motivo que `web/auth.py::validar` con la firma del enlace mágico —
# la única otra puerta de este repo que compara un secreto contra lo que
# llega de afuera.
def _resolver_quien(clave: str) -> str | None:
    encontrado = None
    for candidata, quien in config.CLAVES_API_CODE.items():
        if hmac.compare_digest(candidata, clave):
            encontrado = quien
    return encontrado


def _bearer(encabezado: str | None) -> str | None:
    if not encabezado or not encabezado.startswith("Bearer "):
        return None
    clave = encabezado[len("Bearer "):].strip()
    return clave or None


# ── Límite contra abuso: 60 pedidos/minuto por clave VÁLIDA ───────────────
#
# Decidido por Tiziano (26-sep-2026): protege contra un bug en la sala o en
# Natalia que llame en bucle, no contra un atacante -- a un atacante ya lo
# corta el 401 de una clave mala, más abajo. En memoria, un solo proceso:
# no hace falta más para este volumen (la sala revisa cada 3 horas; Natalia,
# ocasional).
_VENTANA_LIMITE_S = 60.0
_LIMITE_POR_CLAVE = 60
_pedidos_por_quien: dict[str, list[float]] = {}


def _dentro_del_limite(quien: str) -> bool:
    ahora = time.monotonic()
    marcas = [t for t in _pedidos_por_quien.get(quien, ()) if ahora - t < _VENTANA_LIMITE_S]
    marcas.append(ahora)
    _pedidos_por_quien[quien] = marcas
    return len(marcas) <= _LIMITE_POR_CLAVE


# ── Clave mala: 401, registrado, y aviso a Tiziano si se repite ───────────
#
# Decidido por Tiziano (26-sep-2026): 10 intentos con clave inválida en
# 10 minutos -> aviso DIRECTO a Tiziano (no a Code): es un posible ataque o
# una clave filtrada, y decidir si rotarla es suyo. El aviso se dedupea
# —una vez cada 10 minutos como máximo— para que un ataque sostenido no le
# mande cien mensajes.
_VENTANA_ABUSO_S = 600.0
_UMBRAL_ABUSO = 10
_intentos_malos: list[float] = []
_ultimo_aviso_abuso = 0.0


def _registrar_clave_mala() -> None:
    """Cuenta el intento (NUNCA la clave) y dispara el aviso si hace falta.

    Se registra en el log SOLO que hubo un intento -- ni la clave completa
    ni un pedazo de ella, ni la IP (regla del repo: nunca `srcIp` en un
    log). Lo único que se guarda es CUÁNDO, para la ventana de abuso.
    """
    global _ultimo_aviso_abuso
    ahora = time.monotonic()
    _intentos_malos[:] = [t for t in _intentos_malos if ahora - t < _VENTANA_ABUSO_S]
    _intentos_malos.append(ahora)
    log.warning(
        "puerta de Code: clave inválida (%d intento(s) en los últimos %d min)",
        len(_intentos_malos), int(_VENTANA_ABUSO_S // 60))
    if (len(_intentos_malos) >= _UMBRAL_ABUSO
            and ahora - _ultimo_aviso_abuso >= _VENTANA_ABUSO_S):
        _ultimo_aviso_abuso = ahora
        asyncio.create_task(_avisar_abuso(len(_intentos_malos)))


async def _avisar_abuso(n: int) -> None:
    """Un `telegram.Bot` propio, no el de `main.py` -- este proceso (el
    panel) no tiene ninguna referencia al bot que corre el long-polling, y
    un `Bot` nuevo es un cliente HTTP más, no una segunda instancia
    escuchando Telegram. Nunca puede tumbar la puerta: cualquier fallo se
    registra y se traga."""
    try:
        import telegram
        bot = telegram.Bot(token=config.TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=config.CHAT_ID_DUENO,
            text=(f"🔒 La puerta de Code recibió {n} intentos con clave "
                  "inválida en los últimos 10 minutos. Si no fuiste vos "
                  "configurando algo nuevo, puede ser una clave filtrada -- "
                  "considerá rotarla en Railway (CLAVES_API_CODE)."))
    except Exception:
        log.exception("puerta de Code: no pude avisar del abuso por Telegram")


# ── La dependencia: autentica, limita, autoriza ───────────────────────────

def requiere(permiso: str):
    """Fábrica de dependencias de FastAPI: cada ruta declara EL permiso que
    exige, y esta función arma la comprobación completa (clave -> quien ->
    límite -> permiso). Nunca revela POR QUÉ se rechazó un pedido en el
    cuerpo de la respuesta -- ni "esa clave no existe" ni "te falta este
    permiso" -- para no ayudar a alguien a tantear la puerta a ciegas.
    """
    async def _dependencia(request: Request) -> str:
        clave = _bearer(request.headers.get("authorization"))
        quien = _resolver_quien(clave) if clave else None
        if quien is None:
            _registrar_clave_mala()
            raise HTTPException(status_code=401, detail="clave inválida")
        if not _dentro_del_limite(quien):
            log.warning("puerta de Code: %s superó el límite de pedidos", quien)
            raise HTTPException(status_code=429, detail="demasiados pedidos")
        permisos = config.PERMISOS_API_CODE.get(quien, frozenset())
        if permiso not in permisos:
            log.warning(
                "puerta de Code: %s pidió %s sin tener ese permiso", quien, permiso)
            raise HTTPException(status_code=403, detail="clave inválida")
        return quien
    return _dependencia


# ── Las rutas de esta parte: listar y cerrar ──────────────────────────────

@router.get("/tareas")
async def listar_tareas(quien: str = Depends(requiere("tareas:listar"))) -> dict:
    filas = await db.tareas_de_code_pendientes()
    return {"tareas": filas}


@router.post("/tareas/{tid}/cerrar")
async def cerrar_tarea(
    tid: int, quien: str = Depends(requiere("tareas:cerrar"))
) -> dict:
    """Cierra la tarea `tid`. La guarda de valor (área Técnico + responsable
    Code) NO se repite acá: vive en el `WHERE` de `db.cerrar_tarea_de_la_
    sala` (parte 1), la misma para la sala local, la sala en la nube, o
    cualquier otro camino que llame a esa función. Esta ruta solo traduce
    su resultado a un código HTTP.
    """
    ok = await db.cerrar_tarea_de_la_sala(tid)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="no se cerró: no existe, ya está hecha, o no es una "
                   "tarea Técnica de Code")
    return {"cerrada": True}
