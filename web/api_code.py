"""La puerta HTTP de las tareas de Code (26-sep-2026, diseño aprobado por
Tiziano: disenos/lucy-code/DISENO.md, §C — parte 2 del plan de construcción).

Reemplaza que la sala lea/escriba la base de Lucy directo con
`railway run -s Postgres`. Vive en el MISMO proceso FastAPI que el panel
(`web/app.py` la monta con `app.include_router`), pero es una puerta
COMPLETAMENTE APARTE: nunca toca `web.auth` ni la cookie `lucy_panel`. El
panel es para una persona con un chat de Telegram abriendo un navegador; la
sala y Natalia son programas llamando una API, y no tienen ninguna de las
dos cosas.

RUTAS DE ESTA PARTE, y solo éstas: `GET /tareas` (listar),
`POST /tareas/{id}/cerrar`, `POST /tareas/{id}/tomar`, (1-oct-2026) los
comentarios de una tarea de Code: `GET`/`POST /tareas/{id}/comentarios`, y
(4-oct-2026) `POST /alertas`, la ÚNICA ruta de Natalia (permiso
`alertas:crear`). Una ruta a medias —que exista pero siempre falle, o que
acepte un permiso que nada usa— es peor que no tenerla: por eso `alertas:
crear` no tuvo ruta hasta que esta estuvo completa.

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
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

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
    # Etiqueta la dependencia con el permiso que exige, en un atributo
    # REAL del objeto -- no en el texto fuente. Es lo que le permite a
    # `tests/test_panel.py::test_todas_las_rutas_estan_protegidas` (y a
    # `tests/test_api_code.py`) preguntarle a `app.routes` qué exige CADA
    # ruta de verdad, en vez de buscar el literal `"Depends(requiere("` en
    # el código -- un comentario con ese mismo texto, sin el `Depends` real,
    # pasaba esa prueba igual (hallazgo del testigo sobre `b07de3f`).
    _dependencia.permiso = permiso
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
    """Cierra la tarea `tid`. La guarda de valor (área IA + responsable
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
                   "tarea del grupo IA asignada a Code")
    return {"cerrada": True}


@router.post("/tareas/{tid}/tomar")
async def tomar_tarea(
    tid: int, quien: str = Depends(requiere("tareas:tomar"))
) -> dict:
    """Marca que la sala EMPEZÓ a trabajar la tarea `tid` (§D, parte 3). La
    guarda de valor (IA + Code + pendiente + no tomada todavía) NO se
    repite acá: vive en el `WHERE` de `db.tomar_tarea_de_la_sala`.

    TRES RESPUESTAS, porque `db.tomar_tarea_de_la_sala` distingue TRES
    casos (ver su docstring): `True` -> 200; `False` -> 409 (no es
    elegible, o ya estaba tomada); `None` -> 503, un error CLARO y
    registrado ("falta la migración"), nunca un 500 mudo ni un 409 que
    mienta diciendo que la tarea no es de Code cuando sí lo es.
    """
    ok = await db.tomar_tarea_de_la_sala(tid)
    if ok is None:
        log.error(
            "puerta de Code: /tareas/%s/tomar sin tareas.tomada_en -- "
            "falta aplicar db/migrations/2026-09-26_tomada_en.sql", tid)
        raise HTTPException(
            status_code=503,
            detail="no se puede tomar todavía: falta una migración de la "
                   "base (tareas.tomada_en). Avisale a la sala/Tiziano.")
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="no se tomó: no existe, no está pendiente, no es una "
                   "tarea del grupo IA asignada a Code, o ya estaba tomada")
    return {"tomada": True}


# ── Los comentarios de una tarea de Code (1-oct-2026) ─────────────────────
#
# La sala lee y escribe SUS comentarios; Tiziano los lee en el panel de
# Proyectos. Dos permisos propios (`comentarios:leer`, `comentarios:escribir`).
# Quién decide que la tarea es de Code es `db.tarea_de_code`, la misma puerta
# para leer y para escribir. NADA sale por Telegram: escribir un comentario no
# avisa a nadie (ni en la base ni acá). El autor sale por NOMBRE, nunca un
# número de chat.

class _ComentarioNuevo(BaseModel):
    texto: str


def _fecha(valor) -> str | None:
    return valor.isoformat() if isinstance(valor, datetime) else (
        None if valor is None else str(valor))


@router.get("/tareas/{tid}/comentarios")
async def leer_comentarios(
    tid: int, quien: str = Depends(requiere("comentarios:leer"))
) -> dict:
    filas = await db.comentarios_de_tarea_de_code(tid)
    if filas is None:
        raise HTTPException(
            status_code=404,
            detail="no es una tarea del grupo IA asignada a Code, o no existe")
    nombres = config.nombres_con_code()
    return {"comentarios": [
        {"id": f["id"],
         "autor": nombres.get(f["autor_chat_id"], "sin nombre"),
         "creado_en": _fecha(f["creado_en"]),
         "texto": f["texto"],
         "editado": f["editado_en"] is not None}
        for f in filas]}


@router.post("/tareas/{tid}/comentarios")
async def comentar_tarea_de_code(
    tid: int, cuerpo: _ComentarioNuevo,
    quien: str = Depends(requiere("comentarios:escribir"))
) -> dict:
    """Agrega un comentario de Code a la tarea `tid`. Mismo texto válido que
    el panel (`db.texto_de_comentario`); el autor es siempre
    `config.CHAT_ID_CODE`, nunca algo que mande quien llama."""
    texto = db.texto_de_comentario(cuerpo.texto)
    if texto is None:
        raise HTTPException(
            status_code=422,
            detail=f"el texto no puede quedar vacío ni pasar de "
                   f"{db.LARGO_COMENTARIO} caracteres")
    cid = await db.comentar_tarea(tid, config.CHAT_ID_CODE, texto)
    if cid is None:
        raise HTTPException(
            status_code=404,
            detail="no se comentó: no es una tarea del grupo IA asignada a "
                   "Code, o no existe")
    return {"comentado": True, "id": cid}


# ── La alerta técnica de Natalia (4-oct-2026) ─────────────────────────────
#
# Tiziano: los letreros técnicos de Natalia van a Code, no a Telegram; los
# graves, a Telegram Y a Code «con un aviso de grave para que lo atienda
# primero». Esta ruta es la mitad de Lucy: deja (o reusa) una tarea de Code.
# De Telegram se ocupa Natalia por su lado; esta ruta NO manda nada por
# Telegram.
#
# LA CLAVE LA PREFIJA LA AUTENTICACIÓN: lo que llega en `clave` se guarda como
# `f"{quien}:{clave}"`. Así el dedupe de Natalia no puede pisar el de otro
# llamador ni el de las alarmas de Lucy (`backup`, `latido_cosecha`,
# `canario:...`, que nunca llevan `natalia:` delante).
#
# EL DETALLE PUEDE TRAER DATOS DE CLIENTES (nombres, teléfonos): queda en
# `tareas.detalle`, en la base de Lucy, y lo ven quien entra al panel (Tiziano
# y Rosi), la sala por `GET /api/code/tareas` y el modelo de Lucy cuando
# consulta sus tablas. NUNCA se escribe en un log de este código (ni el
# título, ni el detalle, ni la clave), y la huella de `log_acciones` lleva
# solo la clave y el título.
LARGO_TITULO_ALERTA = db.LARGO_TITULO_TAREA
# Elegido, no medido: lo que la tarea guarda de UNA alerta, y lo que puede
# crecer el `detalle` de una tarea reusada antes de dejar de agregar
# repeticiones.
LARGO_DETALLE_ALERTA = 4000
LARGO_CLAVE_ALERTA = 120
_CLAVE_VALIDA = re.compile(r"[A-Za-z0-9._:-]{1,%d}" % LARGO_CLAVE_ALERTA)
_MARCA_RECORTE = "… [recortado]"


class _AlertaNueva(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: StrictStr
    titulo: StrictStr
    detalle: StrictStr = ""
    grave: StrictBool = False


def _recortar(texto: str, tope: int) -> tuple[str, bool]:
    if len(texto) <= tope:
        return texto, False
    return texto[: tope - len(_MARCA_RECORTE)] + _MARCA_RECORTE, True


@router.post("/alertas")
async def crear_alerta(
    cuerpo: _AlertaNueva, quien: str = Depends(requiere("alertas:crear"))
) -> dict:
    """Crea una alerta técnica como tarea de Code, o la reusa si ya hay una
    abierta con la misma clave (`db.crear_o_reusar_alerta_tecnica`, la misma
    puerta de las alarmas de Lucy).

    RESPUESTAS, todas dicen la verdad:
      · 200 `{"tarea_id", "reusada", "grave", "repeticion_omitida",
        "recortado"}`. `reusada: false` = nació una tarea; `true` = ya había
        una abierta y se le agregó esta repetición. `grave` = cómo quedó la
        tarea (una grave no baja). `recortado: true` = el título o el detalle
        pasaron el tope y se guardaron cortados. `repeticion_omitida: true` =
        la tarea ya llegó al tope de detalle y esta repetición no se escribió
        (solo se anotó que la falla sigue sonando).
      · 422: clave/título vacíos o mal formados. NO se guardó nada.
      · 503: falta una migración de la base. NO se guardó nada.
      · 401/403/429: la puerta de siempre. NO se guardó nada.
      · Cualquier otro fallo es un 500: tampoco se guardó nada que se pueda
        asegurar. Natalia decide qué hace; esta ruta no avisa a nadie.
    """
    clave = cuerpo.clave.strip()
    titulo = " ".join(cuerpo.titulo.split())
    if not _CLAVE_VALIDA.fullmatch(clave):
        raise HTTPException(
            status_code=422,
            detail=f"la clave debe tener de 1 a {LARGO_CLAVE_ALERTA} caracteres "
                   "entre letras, números y . _ : -")
    if not titulo:
        raise HTTPException(status_code=422, detail="el título no puede quedar vacío")
    titulo, titulo_recortado = _recortar(titulo, LARGO_TITULO_ALERTA)
    detalle, detalle_recortado = _recortar(cuerpo.detalle.strip(), LARGO_DETALLE_ALERTA)
    informe: dict = {}
    tid = await db.crear_o_reusar_alerta_tecnica(
        f"{quien}:{clave}", titulo, detalle, grave=cuerpo.grave,
        informe=informe, tope_detalle=LARGO_DETALLE_ALERTA)
    if tid is None:
        log.error("puerta de Code: /alertas sin las columnas que necesita -- "
                  "falta aplicar db/migrations/2026-09-26_alertas_tecnicas.sql "
                  "(y 2026-10-04_tarea_grave.sql si la alerta es grave)")
        raise HTTPException(
            status_code=503,
            detail="no se guardó: falta una migración de la base de Lucy. "
                   "Avisale a la sala/Tiziano.")
    return {"tarea_id": tid, "reusada": informe["reusada"],
            "grave": informe["grave"],
            "repeticion_omitida": informe["omitida"],
            "recortado": titulo_recortado or detalle_recortado}


def rutas_registradas(app) -> list[tuple[str, str, str]]:
    """(método, ruta completa, permiso que exige) de cada ruta de esta
    puerta, sacado de lo que FastAPI REALMENTE registró en `app.routes` --
    nunca una lista tecleada aparte, que se desincroniza el día que se
    agregue o se saque una ruta y nadie se acuerde de venir a actualizarla.

    Solo cuenta una ruta si alguna de sus dependencias tiene el atributo
    `permiso` (lo pone `requiere()`, arriba) -- así una ruta futura de este
    mismo router que por algún motivo NO pase por `requiere()` sale con
    permiso `None` y una prueba que la exija puede notarlo, en vez de que
    esta función la calle.
    """
    salida: list[tuple[str, str, str | None]] = []
    for ruta in app.routes:
        if not getattr(ruta, "path", "").startswith(router.prefix):
            continue
        dependant = getattr(ruta, "dependant", None)
        permiso = None
        if dependant is not None:
            for dep in dependant.dependencies:
                if hasattr(dep.call, "permiso"):
                    permiso = dep.call.permiso
        metodos = sorted((ruta.methods or set()) - {"HEAD"})
        for metodo in metodos:
            salida.append((metodo, ruta.path, permiso))
    return salida
