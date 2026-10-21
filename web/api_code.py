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
(4-oct-2026) `POST /alertas` (permiso `alertas:crear`) y, desde el 18-oct-2026, la lectura
`GET /clientes/{noco_id}/proyectos` (permiso `proyectos:leer_de_cliente`) y, de la
pieza L3, las consultas al responsable: `POST`/`GET /clientes/{noco_id}/consultas` (permisos
`consultas:abrir` y `consultas:leer`) y, de la pieza 9 de las facturas (21-oct-2026), las facturas de Natalia:
`GET`/`POST /clientes/{noco_id}/facturas`, `GET /clientes/{noco_id}/facturas/{id}`, `POST …/envios`, `GET …/envios/{id}/pdf`
y `POST …/envios/{id}/resultado` (permisos `facturas:listar`, `facturas:leer`, `facturas:crear`, `facturas:pedir_envio`,
`facturas:pdf` y `facturas:anotar_envio`): las rutas de Natalia. Una ruta a medias —que exista pero siempre falle, o que
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
from datetime import date, datetime
from decimal import Decimal

from typing import Annotated, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

import config
import db.db as db
import facturas
import facturas_panel as fp
import noco_lectura
import registro_lectura
from acciones import crud
from web.avisos import Navegacion

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

def requiere(permiso: str, solo_de: str | None = None):
    """Fábrica de dependencias de FastAPI: cada ruta declara EL permiso que
    exige, y esta función arma la comprobación completa (clave -> quien ->
    límite -> permiso). Nunca revela POR QUÉ se rechazó un pedido en el
    cuerpo de la respuesta -- ni "esa clave no existe" ni "te falta este
    permiso" -- para no ayudar a alguien a tantear la puerta a ciegas.

    `solo_de` (21-oct-2026, las facturas de Natalia): además del permiso, el
    `quien` de la clave tiene que ser ESE (hoy `config.QUIEN_DE_NATALIA`). Las
    rutas de las facturas se escriben pensando en que quien llama es Natalia
    (solo ve y toca lo que ella hizo): que otra clave con el permiso entre
    como ella sería un descuido de configuración, no un diseño.
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
        if solo_de is not None and quien != solo_de:
            log.warning(
                "puerta de Code: %s pidió %s y esa ruta es solo de %s", quien, permiso, solo_de)
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
    return {"comentarios": [
        {"id": f["id"],
         "autor": config.nombre_de_autor(f["autor_chat_id"]) or "sin nombre",
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
# `canario:...`, que nunca llevan delante un prefijo de
# `config.PREFIJOS_DE_ALERTAS_DE_FUERA`).
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
_MARCA_RECORTE = db.MARCA_RECORTE


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
    if quien not in config.QUIENES_DE_ALERTAS_DE_FUERA:
        # La exclusión del aviso por Telegram se lee del prefijo `<quien>:`
        # (`config.PREFIJOS_DE_ALERTAS_DE_FUERA`); una clave con `alertas:crear`
        # que se llame de otra forma guardaría otro prefijo y volvería a
        # disparar el aviso. No se guarda nada.
        log.error("puerta de Code: %s tiene alertas:crear pero la puerta de "
                  "alertas solo acepta a %r", quien,
                  config.QUIENES_DE_ALERTAS_DE_FUERA)
        raise HTTPException(
            status_code=503,
            detail="no se guardó: la clave con alertas:crear no se llama "
                   f"{' ni '.join(map(repr, config.QUIENES_DE_ALERTAS_DE_FUERA))} "
                   "en la configuración de Lucy. Avisale a la sala/Tiziano.")
    for crudo in (cuerpo.clave, cuerpo.titulo, cuerpo.detalle):
        if "\x00" in crudo:
            raise HTTPException(status_code=422,
                                detail="el texto no puede llevar caracteres nulos")
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
            "recortado": (titulo_recortado or detalle_recortado
                          or informe["recortada"])}


# ── Los proyectos de un cliente, para Natalia (18-oct-2026, pieza L1) ─────
#
# Diseño aprobado: disenos/natalia-tareas-en-proyectos/DISENO.md §4. Natalia le
# cuenta al cliente SOLO el estado de su proyecto, la tarea que sigue y su
# fecha estimada. Tres cerrojos, los tres de ESTA ruta y de `db`:
#   1. Lucy filtra en la consulta por la ficha del cliente
#      (`db.proyectos_de_cliente`): lo de otro cliente ni sale de la base.
#   2. Lucy recorta los campos AQUÍ, armando la respuesta campo por campo
#      (no copiando el modelo de `db`): lo que no se nombra no sale.
#   3. NINGÚN número de chat sale: los responsables van por NOMBRE.
# Es de solo lectura: no escribe nada ni avisa a nadie.
_ID_MAXIMO_BIGINT = 9223372036854775807


def _siguiente_para_natalia(proxima: dict | None) -> dict | None:
    """La tarea que sigue, recortada: id, título, fecha (día de Santo Domingo,
    `AAAA-MM-DD`, o `None` si no tiene) y si está vencida. `grave` no sale."""
    if proxima is None:
        return None
    return {"tarea_id": proxima["id"], "titulo": proxima["titulo"],
            "fecha": proxima["vence"].isoformat() if proxima["vence"] else None,
            "vencida": bool(proxima["vencida"])}


def responsables_para_natalia() -> list[str]:
    """Los NOMBRES de quienes pueden ser responsables de una tarea
    (`config.puede_ser_responsable`) Y a quienes Lucy les puede escribir por
    Telegram (`config.puede_recibir_telegram`): Code no sale. Sale de
    `personas_del_panel()`, nunca de una lista tecleada."""
    return [nombre for chat, nombre in config.personas_del_panel()
            if config.puede_ser_responsable(chat)
            and config.puede_recibir_telegram(chat)]


@router.get("/clientes/{noco_id}/proyectos")
async def proyectos_del_cliente(
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("proyectos:leer_de_cliente")),
) -> dict:
    """Los proyectos (no borrados, de cualquier estado) del cliente cuya ficha
    de NocoDB es `noco_id`. 200 `{"noco_id", "proyectos": [{"id", "nombre",
    "estado", "siguiente": {"tarea_id", "titulo", "fecha", "vencida"} | null,
    "admite_tareas", "responsable"}], "responsables": [nombre, …]}`.
    Un cliente sin proyecto (o un id inventado) da `proyectos: []` y
    `responsables: []`, igual. 401/403/429 son los de siempre; un `noco_id` que
    no es un entero positivo es 422. No escribe nada.
    """
    modelos = await db.proyectos_de_cliente(noco_id)
    if not modelos:
        return {"noco_id": noco_id, "proyectos": [], "responsables": []}
    nombres = config.nombres_con_code()
    proyectos = [{
        "id": m["id"], "nombre": m["nombre"], "estado": m["estado"],
        "siguiente": _siguiente_para_natalia(m["proxima"]),
        "admite_tareas": bool(m["admite_tareas"]),
        "responsable": nombres.get(m["responsable_chat_id"]),
    } for m in modelos]
    return {"noco_id": noco_id, "proyectos": proyectos,
            "responsables": responsables_para_natalia()}


# ── Las consultas al responsable, para Natalia (18-oct-2026, pieza L3) ────
#
# Diseño aprobado: disenos/natalia-tareas-en-proyectos/DISENO.md §5. Natalia NO escribe tareas en Lucy: abre
# una CONSULTA y Lucy le pregunta al responsable (la tarjeta es de la pieza L4, los botones de la L5). Estas dos
# rutas solo abren y leen consultas; ninguna manda nada a Telegram.
#   · Abrir: `POST /clientes/{noco_id}/consultas` (`consultas:abrir`). Toda la validación y la escritura
#     viven en `db.abrir_consulta_de_natalia`; aquí solo se traduce el pedido y el resultado.
#   · Leer: `GET /clientes/{noco_id}/consultas?ids=…` (`consultas:leer`), SOLO las de esa ficha.
# Lo que responden las dos sale de una RELECTURA de la base (`db.consultas_de_natalia_de_cliente`), nunca de lo
# que se mandó. Ningún número de chat sale.

class _ConsultaTareaNueva(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: StrictStr
    tipo: Literal["tarea_nueva"]
    proyecto_id: StrictInt
    titulo: StrictStr
    pedido: StrictStr
    vence: StrictStr = ""
    responsable: StrictStr = ""


class _ConsultaNuevaFecha(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: StrictStr
    tipo: Literal["nueva_fecha"]
    tarea_id: StrictInt


_ConsultaNueva = Annotated[Union[_ConsultaTareaNueva, _ConsultaNuevaFecha], Field(discriminator="tipo")]

# Qué estado HTTP le toca a cada `ConsultaNoVale.clave`; lo que no esté aquí es 422 (el pedido está mal formado).
# 404: no existe para ese cliente (también lo de otro cliente: no se distingue). 409: existe pero choca.
_HTTP_DE_CONSULTA = {"proyecto": 404, "tarea": 404, "proyecto_cerrado": 409, "tarea_no_vencida": 409,
                     "clave_ajena": 409, "clave_distinta": 409}


def _sin_a_quien_preguntar(motivo: str) -> str:
    """El 422 de «no hay a quién preguntarle»: el motivo y los NOMBRES que sí valen (nunca un chat)."""
    return f"{motivo} Se puede preguntar a: {', '.join(responsables_para_natalia()) or 'nadie hoy'}."


def _consulta_para_natalia(c: dict) -> dict:
    """Una consulta releída → lo que sale: campo por campo, nada copiado del modelo de `db`."""
    tarea, siguiente = c["tarea"], c["siguiente"]
    return {
        "consulta_id": c["id"], "clave": c["clave"], "tipo": c["tipo"], "estado": c["estado"],
        "proyecto_id": c["proyecto_id"], "titulo": c["titulo"], "tarea_id": c["tarea_id"],
        "tarea": None if tarea is None else {
            "tarea_id": tarea["id"], "titulo": tarea["titulo"],
            "fecha": tarea["vence"].isoformat() if tarea["vence"] else None,
            "vencida": bool(tarea["vencida"]), "pendiente": bool(tarea["pendiente"])},
        "siguiente": _siguiente_para_natalia(siguiente),
    }


@router.post("/clientes/{noco_id}/consultas")
async def abrir_consulta(
    cuerpo: _ConsultaNueva,
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("consultas:abrir")),
) -> dict:
    """Abre la consulta de Natalia para la ficha `noco_id`, o devuelve la que ya tiene esa `clave`.

    PEDIDO `tarea_nueva`: `{clave, tipo, proyecto_id, titulo, pedido, vence: "AAAA-MM-DD" | "", responsable:
    "<nombre>" | ""}`. `nueva_fecha`: `{clave, tipo, tarea_id}`. Cualquier otro campo es 422.

    RESPUESTAS, todas dicen la verdad:
      · 200 `{"consulta_id", "clave", "tipo", "estado", "proyecto_id", "titulo", "tarea_id", "tarea",
        "siguiente", "reusada"}`, releído de la base. `reusada: true` = ya existía (la misma clave, o ya había
        una `nueva_fecha` viva para esa tarea): no se abrió otra.
      · 404: el proyecto o la tarea no es de ese cliente (o no existe). 409: el proyecto está cerrado; la tarea
        no está vencida; la clave es de otra consulta. 422: pedido mal formado, o no hay a quién preguntarle
        (el detalle dice los nombres que sí valen).
      · 503: falta la migración de la tabla. NO se guardó nada. 401/403/429: la puerta de siempre.
    """
    clave = cuerpo.clave.strip()
    textos = [clave]
    if isinstance(cuerpo, _ConsultaTareaNueva):
        textos += [cuerpo.titulo, cuerpo.pedido, cuerpo.vence, cuerpo.responsable]
    if any("\x00" in t for t in textos):
        raise HTTPException(status_code=422, detail="el texto no puede llevar caracteres nulos")
    if not _CLAVE_VALIDA.fullmatch(clave):
        raise HTTPException(
            status_code=422,
            detail=f"la clave debe tener de 1 a {LARGO_CLAVE_ALERTA} caracteres "
                   "entre letras, números y . _ : -")
    responsable = None
    if isinstance(cuerpo, _ConsultaTareaNueva) and cuerpo.responsable.strip():
        # Por NOMBRE, con la MISMA pieza que los desplegables del panel (`crud.PUERTAS`), y después las
        # puertas de la consulta (poder ser responsable y poder recibir Telegram) dentro de `db`.
        try:
            responsable = crud.PUERTAS["tareas"]["responsable_chat_id"](cuerpo.responsable)
        except ValueError:
            raise HTTPException(status_code=422, detail=_sin_a_quien_preguntar(
                "ese nombre no es de nadie que pueda ser responsable.")) from None
    try:
        if isinstance(cuerpo, _ConsultaTareaNueva):
            consulta_id, reusada = await db.abrir_consulta_de_natalia(
                noco_id, clave, cuerpo.tipo, proyecto_id=cuerpo.proyecto_id, titulo=cuerpo.titulo,
                pedido=cuerpo.pedido, vence=cuerpo.vence, responsable_chat_id=responsable)
        else:
            consulta_id, reusada = await db.abrir_consulta_de_natalia(
                noco_id, clave, cuerpo.tipo, tarea_id=cuerpo.tarea_id)
        relei = await db.consultas_de_natalia_de_cliente(noco_id, [consulta_id])
    except db.ConsultasSinTabla:
        log.error("puerta de Code: /consultas sin la tabla -- falta aplicar "
                  "db/migrations/2026-10-18_consultas_de_natalia.sql")
        raise HTTPException(
            status_code=503,
            detail="no se guardó: falta una migración de la base de Lucy. Avisale a la sala/Tiziano.") from None
    except db.ConsultaNoVale as e:
        if e.clave == "responsable":
            raise HTTPException(status_code=422, detail=_sin_a_quien_preguntar(str(e))) from None
        raise HTTPException(status_code=_HTTP_DE_CONSULTA.get(e.clave, 422), detail=str(e)) from None
    if not relei:
        # Se abrió y no se vuelve a ver: no se responde nada que no se pueda leer.
        log.error("puerta de Code: la consulta %s se abrió y la relectura no la encontró", consulta_id)
        raise HTTPException(status_code=500, detail="la consulta se abrió pero no se pudo releer")
    return {**_consulta_para_natalia(relei[0]), "reusada": reusada}


@router.get("/clientes/{noco_id}/consultas")
async def leer_consultas(
    ids: Navegacion[str],
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("consultas:leer")),
) -> dict:
    """Las consultas de la ficha `noco_id` con esos `ids` (`?ids=3,4,9`, hasta
    `db.TOPE_DE_CONSULTAS_POR_LECTURA`). 200 `{"noco_id", "consultas": [{"consulta_id", "clave", "tipo",
    "estado", "proyecto_id", "titulo", "tarea_id", "tarea", "siguiente"}]}`. `tarea` solo viene si está
    `anotada` (la tarea releída) y `siguiente` solo si está `resuelta` (la que sigue en su proyecto, releída).
    Una de otra ficha y una que no existe dan lo mismo: no salen. `ids` mal formado o con demasiados es 422;
    503 si falta la migración. Solo lee."""
    try:
        if len(ids) > 2000:
            raise ValueError
        pedidas = [int(x) for x in ids.split(",")]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids va como números separados por coma") from None
    if (not pedidas or len(pedidas) > db.TOPE_DE_CONSULTAS_POR_LECTURA
            or any(i < 1 or i > _ID_MAXIMO_BIGINT for i in pedidas)):
        raise HTTPException(
            status_code=422,
            detail=f"ids son de 1 a {db.TOPE_DE_CONSULTAS_POR_LECTURA} números enteros positivos")
    try:
        consultas = await db.consultas_de_natalia_de_cliente(noco_id, pedidas)
    except db.ConsultasSinTabla:
        raise HTTPException(
            status_code=503,
            detail="no se pudo leer: falta una migración de la base de Lucy. Avisale a la sala/Tiziano.") from None
    return {"noco_id": noco_id, "consultas": [_consulta_para_natalia(c) for c in consultas]}


# ── Las facturas de Natalia (21-oct-2026, pieza 9 del diseño «Facturas») ──────────────────────────────
#
# Diseño aprobado por Tiziano el 20-oct-2026 (disenos/lucy-facturas/DISENO.md). El camino: el cliente le pide la
# factura a Natalia → NATALIA LA HACE (`POST …/facturas`) → pide que se apruebe mandarla (`POST …/envios`) → ROSI LA
# REVISA en Telegram (la tarjeta con el PDF y los botones es del despertador y de `acciones/botones.py`) → Natalia lee
# cómo quedó (`GET …/facturas`) → si quedó aprobada baja el PDF (`GET …/pdf`) y la manda por WhatsApp o por correo →
# anota si salió (`POST …/resultado`). Lucy NUNCA dice «aprobada», «emitida» ni «enviada» si no pasó: todo lo que
# responde sale de una RELECTURA de la base.
#
# QUÉ FACTURAS VE Y TOCA NATALIA: solo las que ELLA hizo, de la ficha que dice la ruta (la regla está en `db`:
# `SENTENCIA_DE_LA_FACTURA_DE_NATALIA`). Una de la casa o de otra ficha le da 404, igual que una que no existe. Natalia
# no anula, no descarta, no corrige, no emite y no escribe el NCF: eso es de la casa. Ningún número de chat sale.
#
# LA SESIÓN TIENE QUE SER DEL CLIENTE: se comprueba con LA MISMA lectura y LA MISMA regla que la pantalla
# (`registro_lectura.lo_que_dice_el_registro_del_cliente` + `facturas_panel.ligas_de_las_refs`), ANTES de escribir.
#
# LÍMITE DE SESIONES POR FACTURA: decisión de la puerta, no una medición.
LIMITE_DE_SESIONES_POR_FACTURA_DE_NATALIA = 50


class _RenglonDeNatalia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    descripcion: StrictStr
    detalle: StrictStr = ""
    monto: StrictStr           # texto ("62500.00"): un número JSON se leería como float, que la puerta rechaza


class _FacturaNueva(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refs: list[StrictStr] = Field(min_length=1, max_length=LIMITE_DE_SESIONES_POR_FACTURA_DE_NATALIA)
    renglones: list[_RenglonDeNatalia] | None = None
    itbis: StrictBool = False
    rnc_cedula: StrictStr = ""
    fecha: StrictStr = ""


class _EnvioPedido(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canal: Literal["whatsapp", "correo"]


class _EnvioResultado(BaseModel):
    model_config = ConfigDict(extra="forbid")
    salio: StrictBool
    motivo: StrictStr = ""


def _dinero_para_natalia(valor) -> str:
    return str(Decimal(valor).quantize(Decimal("0.01")))


def _envio_para_natalia(e: dict) -> dict:
    """Un envío releído → lo que sale: `estado` es el de la base (`pendiente` = esperando que la revisen, `aprobado` =
    aprobado y todavía sin anotar cómo terminó, `rechazado`, `enviado`, `no_se_pudo`); `pdf_listo` solo es verdadero con
    el envío aprobado (la única vez que se puede bajar el PDF)."""
    return {"envio_id": e["id"], "canal": e["canal"], "estado": e["estado"], "aprobada": bool(e["aprobada"]),
            "pdf_listo": e["estado"] == "aprobado", "motivo": e["motivo"]}


def _factura_para_natalia(f: dict) -> dict:
    """Una factura releída (`db.facturas_de_natalia`) → lo que sale, campo por campo. `codigo` es `None` mientras
    sea borrador (no tiene número hasta que se aprueba). Sin chats, sin NCF, sin Drive."""
    return {
        "factura_id": f["id"], "codigo": f["codigo"], "estado": f["estado"], "para_nombre": f["para_nombre"],
        "fecha": f["fecha"].isoformat() if isinstance(f["fecha"], date) else str(f["fecha"]),
        "subtotal": _dinero_para_natalia(f["subtotal"]), "itbis": _dinero_para_natalia(f["itbis"]),
        "total": _dinero_para_natalia(f["total"]),
        "renglones": [{"descripcion": r["descripcion"], "detalle": r["detalle"],
                       "monto": _dinero_para_natalia(r["monto"])} for r in f["renglones"]],
        "ligas": [{"tipo": l["tipo"], "ref": l["ref"], "codigo": l["codigo"]} for l in f["ligas"] if l["viva"]],
        "envios": [_envio_para_natalia(e) for e in f["envios"]],
    }


def _sin_migrar_las_facturas() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="no se pudo: falta una migración de la base de Lucy. Avisale a la sala/Tiziano.")


def _por_que_la_puerta_dijo_que_no(e: Exception) -> HTTPException:
    """El `HTTPException` de lo que la puerta de `db` rechazó, con su razón en palabras (la misma que lee la persona
    en la pantalla: `facturas_panel.texto_del_error`). 404: no es de Natalia / no existe. 409: existe pero choca
    (estado que no deja, o una sesión que ya está en otra factura). 422: el pedido no vale."""
    if isinstance(e, db.FacturaNoEsta):
        return HTTPException(status_code=404, detail="esa factura no está")
    if isinstance(e, db.FacturaYaNoSeCorrige):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, db.FacturaCambio):
        return HTTPException(status_code=409, detail="la factura cambió; vuelve a leerla")
    if isinstance(e, db.FacturaNoVale):
        razon = fp.texto_del_error(e)["razon"]
        return HTTPException(status_code=409 if e.clave == "sesion_ya_facturada" else 422, detail=razon)
    if isinstance(e, (db.FacturasSinTabla, db.EnviosSinMigrar)):
        return _sin_migrar_las_facturas()
    raise e


def _renglones_de_las_sesiones_de_natalia(ligas: list[dict], lectura: dict) -> tuple[list[dict], list[str]]:
    """Los renglones que salen de las sesiones o trabajos escogidos (el mismo `facturas_panel.renglon_de_una_sesion`
    de la pantalla: el total que la App tiene, sin multiplicar nada), y los códigos de los que la App no trae total."""
    por_ref = {r["ref"]: r for r in lectura.get("sesiones", []) + lectura.get("trabajos", [])}
    renglones, sin_total = [], []
    for liga in ligas:
        r = fp.renglon_de_una_sesion(por_ref[liga["ref"]])
        if r["sin_total"]:
            sin_total.append(r["codigo"] or r["ref"])
            continue
        renglones.append({"descripcion": r["descripcion"], "detalle": r["detalle"], "monto": r["monto"]})
    return renglones, sin_total


@router.post("/clientes/{noco_id}/facturas")
async def crear_factura_de_natalia(
    cuerpo: _FacturaNueva,
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:crear", solo_de=config.QUIEN_DE_NATALIA)),
) -> dict:
    """Natalia HACE una factura (borrador, sin número, `hecha_por = natalia`) para la ficha `noco_id` con las sesiones o
    trabajos `refs` de esa ficha en el registro.

    PEDIDO: `{refs: ["<ref>", …] (1 a 50), renglones?: [{descripcion, detalle?, monto: "62500.00"}], itbis?: bool
    (18 %; falso por omisión), rnc_cedula?: "9 u 11 cifras", fecha?: "AAAA-MM-DD" (hoy por omisión)}`. Sin `renglones`
    sale un renglón por sesión con el total que el registro tiene (si alguna no trae total, 422 y nada se guarda).
    El nombre lo pone Lucy: es el de la ficha en NocoDB. NCF, proyecto y cotización no se piden: son de la casa.

    RESPUESTAS: 200 `{"factura": {factura_id, codigo (null), estado: "borrador", para_nombre, fecha, subtotal, itbis, total,
    renglones, ligas, envios: []}, "reusada": false}`, releído de la base. 404: la ficha no existe. 409: alguna sesión ya
    está en otra factura viva. 422: pedido que no vale (sesión de otro cliente, cancelada, ficha sin teléfono en el
    registro, monto mal escrito…): el detalle dice cuál. 503: Noco, el registro o la base no contestaron, o falta una
    migración. EN TODOS LOS ERRORES NO SE GUARDÓ NADA. 401/403/429: la puerta de siempre."""
    textos = [cuerpo.rnc_cedula, cuerpo.fecha, *cuerpo.refs]
    if cuerpo.renglones:
        textos += [x for r in cuerpo.renglones for x in (r.descripcion, r.detalle, r.monto)]
    if any("\x00" in x for x in textos):
        raise HTTPException(status_code=422, detail="el texto no puede llevar caracteres nulos")
    try:
        ficha = await noco_lectura.persona(noco_id)
    except noco_lectura.NocoNoContesta:
        raise HTTPException(status_code=503, detail="no se pudo leer la ficha del cliente en NocoDB: no se guardó nada") from None
    if ficha is None:
        raise HTTPException(status_code=404, detail="esa ficha de cliente no existe")
    # LA COMPROBACIÓN DE QUE LAS SESIONES SON DEL CLIENTE: la misma de la pantalla, antes de escribir.
    lectura = await registro_lectura.lo_que_dice_el_registro_del_cliente(noco_id)
    if lectura is None:
        raise HTTPException(
            status_code=503,
            detail="no se pudo consultar el registro para comprobar que esas sesiones son del cliente: no se guardó nada")
    ligas, problemas = fp.ligas_de_las_refs(cuerpo.refs, lectura)
    if problemas:
        raise HTTPException(status_code=422, detail=" ".join(fp.frase_del_problema_de_liga(p) for p in problemas)
                            + " No se guardó nada.")
    if cuerpo.renglones is None:
        renglones, sin_total = _renglones_de_las_sesiones_de_natalia(ligas, lectura)
        if sin_total:
            raise HTTPException(
                status_code=422,
                detail="el registro no trae el total de: " + ", ".join(sin_total)
                       + ". Manda los renglones. No se guardó nada.")
    else:
        renglones = [{"descripcion": r.descripcion, "detalle": r.detalle or None, "monto": r.monto}
                     for r in cuerpo.renglones]
    try:
        creada = await db.crear_borrador_de_factura_de_natalia(
            noco_id, para_nombre=ficha["nombre"], renglones=renglones, ligas=ligas, fecha=cuerpo.fecha or None,
            itbis_pct=db.ITBIS_DE_FACTURA if cuerpo.itbis else None, rnc_cedula=cuerpo.rnc_cedula or None)
        relei = await db.facturas_de_natalia(noco_id, creada["id"])
    except (db.FacturaNoVale, db.FacturaNoEsta, db.FacturasSinTabla) as e:
        raise _por_que_la_puerta_dijo_que_no(e) from None
    if not relei:
        log.error("puerta de Code: la factura %s se creó y la relectura no la encontró", creada["id"])
        raise HTTPException(status_code=500, detail="la factura se creó pero no se pudo releer")
    return {"factura": _factura_para_natalia(relei[0]), "reusada": False}


@router.get("/clientes/{noco_id}/facturas")
async def leer_facturas_de_natalia(
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:listar", solo_de=config.QUIEN_DE_NATALIA)),
) -> dict:
    """Las facturas que HIZO Natalia para esa ficha (la más nueva primero, hasta `db.TOPE_DE_FACTURAS_DE_NATALIA`), cada
    una con sus envíos y cómo van. 200 `{"noco_id", "facturas": [ … la forma de `POST …/facturas` … ]}`. Una ficha sin
    facturas de Natalia da `[]`. 503 si falta la migración. Solo lee."""
    try:
        facturas_de_ella = await db.facturas_de_natalia(noco_id)
    except db.FacturasSinTabla:
        raise _sin_migrar_las_facturas() from None
    return {"noco_id": noco_id, "facturas": [_factura_para_natalia(f) for f in facturas_de_ella]}


@router.get("/clientes/{noco_id}/facturas/{factura_id}")
async def leer_una_factura_de_natalia(
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    factura_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:leer", solo_de=config.QUIEN_DE_NATALIA)),
) -> dict:
    """UNA factura de Natalia, releída de la base: 200 `{"factura": {…}}`, o 404 si no es suya / no es de esa ficha / no
    existe (no se distingue). Es lo que Natalia lee para saber si Rosi la aprobó (`envios[].estado`)."""
    try:
        relei = await db.facturas_de_natalia(noco_id, factura_id)
    except db.FacturasSinTabla:
        raise _sin_migrar_las_facturas() from None
    if not relei:
        raise HTTPException(status_code=404, detail="esa factura no está")
    return {"factura": _factura_para_natalia(relei[0])}


@router.post("/clientes/{noco_id}/facturas/{factura_id}/envios")
async def pedir_envio_de_factura(
    cuerpo: _EnvioPedido,
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    factura_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:pedir_envio", solo_de=config.QUIEN_DE_NATALIA)),
) -> dict:
    """Natalia pide que se APRUEBE mandar su factura por `canal` (`whatsapp` o `correo`). Lucy le manda la tarjeta (con el
    PDF y los botones Aprobar / Rechazar) a quien revisa las facturas (`config.chat_de_quien_revisa_las_facturas`) y
    NADA sale al cliente hasta que esa persona apruebe. Al aprobar, Lucy EMITE la factura (se numera ahí).

    PEDIDO `{canal}`. 200 `{"envio": {envio_id, canal, estado, aprobada, pdf_listo, motivo}, "reusada": bool}`. El
    `estado` es SIEMPRE el de la base, y hay tres desenlaces:
      · `pendiente` — hay que esperar a que quien revisa conteste (`reusada`: ya había uno vivo de ese canal).
      · `aprobado` — el envío ya se puede mandar (`pdf_listo` verdadero). Pasa cuando la factura está EMITIDA y ya tuvo
        una aprobación en el OTRO canal que no salió: no se le pide a la revisora que apruebe dos veces la misma
        factura.
      · `enviado` — la factura YA salió al cliente: no hay nada que mandar (`reusada` verdadero; es el envío que salió).
    404: la factura no es suya / no existe. 409: la factura está anulada. 422: la factura está incompleta. 503: no hay a
    quién pedirle la aprobación (el nombre de quien revisa no resuelve a un chat al que Lucy le pueda escribir) o falta
    una migración: NO SE PIDIÓ NADA. «pendiente» quiere decir que la persona todavía no contestó, no que la tarjeta ya
    llegó."""
    if config.chat_de_quien_revisa_las_facturas() is None:
        log.error("puerta de Code: no hay a quién mandarle la tarjeta de las facturas (REVISA_FACTURAS no resuelve)")
        raise HTTPException(
            status_code=503,
            detail="Lucy no sabe a quién pedirle la aprobación de la factura: no se pidió nada. Avisale a la sala/Tiziano.")
    try:
        r = await db.pedir_envio_de_factura_de_natalia(noco_id, factura_id, cuerpo.canal)
    except (db.FacturaNoVale, db.FacturaNoEsta, db.FacturaYaNoSeCorrige, db.FacturasSinTabla, db.EnviosSinMigrar) as e:
        raise _por_que_la_puerta_dijo_que_no(e) from None
    return {"envio": _envio_para_natalia(r["envio"]), "reusada": r["reusada"]}


@router.get("/clientes/{noco_id}/facturas/{factura_id}/envios/{envio_id}/pdf")
async def pdf_de_factura_aprobada(
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    factura_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    envio_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:pdf", solo_de=config.QUIEN_DE_NATALIA)),
) -> Response:
    """El PDF de la factura, SOLO si ese envío está APROBADO: la factura es de Natalia y de esa ficha, está emitida y
    alguien aprobó mandarla por ese envío (y Natalia todavía no anotó cómo terminó). 200 `application/pdf` (el nombre es
    el número: `FAC-2026-001.pdf`). 404: no es suya / no existe / ese envío no es de esa factura. 409: el envío todavía
    no se aprobó, se rechazó, ya terminó, la factura no está emitida, o **la factura ya salió al cliente por otro envío
    (por el canal que sea, `mano` incluido)** — es la puerta que evita el segundo documento. 503: falta una migración.
    Un borrador NUNCA se baja por aquí."""
    try:
        f = await db.factura_y_envio_para_el_pdf_de_natalia(noco_id, factura_id, envio_id)
    except (db.FacturaNoEsta, db.FacturaYaNoSeCorrige, db.FacturasSinTabla) as e:
        raise _por_que_la_puerta_dijo_que_no(e) from None
    pdf = facturas.pdf_de_factura(facturas.hoja_de_factura(f))
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{facturas.nombre_del_archivo(f)}"',
        "Cache-Control": "private, no-store"})


@router.post("/clientes/{noco_id}/facturas/{factura_id}/envios/{envio_id}/resultado")
async def anotar_resultado_del_envio(
    cuerpo: _EnvioResultado,
    noco_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    factura_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    envio_id: int = Path(ge=1, le=_ID_MAXIMO_BIGINT),
    quien: str = Depends(requiere("facturas:anotar_envio", solo_de=config.QUIEN_DE_NATALIA)),
) -> dict:
    """Natalia anota cómo terminó un envío APROBADO: `{salio: true}` → `enviado`; `{salio: false, motivo: "…"}` →
    `no_se_pudo` (el motivo, de 1 a 500 caracteres, es obligatorio). Es lo ÚNICO que hace que una factura figure como
    enviada: Lucy no lo supone. Anotar dos veces lo mismo es lo mismo. 200 `{"envio": {…}}` releído. 404: no es suya.
    409: el envío no está aprobado (pendiente, rechazado) o terminó de otra forma. 422: falta el motivo."""
    if "\x00" in cuerpo.motivo:
        raise HTTPException(status_code=422, detail="el texto no puede llevar caracteres nulos")
    try:
        envio = await db.anotar_envio_de_natalia(noco_id, factura_id, envio_id, cuerpo.salio, cuerpo.motivo)
    except (db.FacturaNoVale, db.FacturaNoEsta, db.FacturaYaNoSeCorrige, db.FacturasSinTabla) as e:
        raise _por_que_la_puerta_dijo_que_no(e) from None
    return {"envio": _envio_para_natalia(envio)}


def avisar_si_alertas_mal_nombradas() -> list[str]:
    """Al arrancar: si la configuración trae un `quien` con `alertas:crear` que no
    está en `config.QUIENES_DE_ALERTAS_DE_FUERA`, lo dice en voz alta (log de
    error) y devuelve cuáles son. La ruta además se niega a atenderlos (503)."""
    mal = config.quienes_con_alertas_fuera_de_nombre()
    if mal:
        log.error("CONFIGURACIÓN DE LA PUERTA DE CODE: %s tiene alertas:crear y "
                  "no es ninguno de %r: la puerta de alertas no los atiende",
                  ", ".join(mal), config.QUIENES_DE_ALERTAS_DE_FUERA)
    return mal


avisar_si_alertas_mal_nombradas()


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
