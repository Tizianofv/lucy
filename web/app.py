"""El panel: las finanzas de la casa en una página que se abre desde el celular.

Vive en el MISMO servicio de Railway que Lucy — una ruta más, no un servicio
nuevo. Esa decisión es de costo: un servicio aparte movería la factura, y una
ruta añadida a un proceso que ya corre no cuesta nada medible.

CUATRO PANTALLAS, y el orden no es casual:

  /                 Resumen por mes, con las monedas SEPARADAS y los traspasos
                    fuera. Sumar DOP con USD da un número que no significa nada,
                    y contar un traspaso entre cuentas propias como gasto fue el
                    error de RD$657,400 al año que este proyecto vino a arreglar.

  /sin-clasificar   La cola de corrección, ordenada por monto. ES LA PANTALLA
                    QUE PAGA EL PANEL: cada corrección acá es una regla que el
                    sistema aprende, y si solo se corrigen diez, que sean los
                    diez que más pesan.

  /movimientos      El detalle, filtrable.

  /salud            Desde cuándo el sistema no sabe nada. Un panel que no dice
                    cuándo miró por última vez miente por omisión: un cero puede
                    ser "no gastaste" o "dejé de mirar", y son cosas opuestas.

SIN BUILD DE JAVASCRIPT. HTML renderizado en el servidor y CSS a mano. Meter un
toolchain de Node en un repo Python que despliega en Railway sería un segundo
proyecto de mantenimiento, y el tiempo es justo lo que este proyecto no tiene.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import db.db as db
import web.auth as auth
from cerebro.bancos.categorias import CATEGORIAS

log = logging.getLogger("lucy.panel")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
plantillas = Jinja2Templates(directory="web/plantillas")

COOKIE = "lucy_panel"


def _pesos(v) -> str:
    """Formato dominicano: 1,234.56. Sin símbolo — la moneda va aparte, porque
    mezclarlas en la misma columna es justo lo que este panel no hace."""
    return f"{Decimal(v):,.2f}" if v is not None else "—"


plantillas.env.filters["pesos"] = _pesos


def _sesion(request: Request) -> int | None:
    return auth.validar(request.cookies.get(COOKIE))


def _fuera(request: Request) -> HTMLResponse:
    return plantillas.TemplateResponse(
        request, "entrar.html", {"chat": config.CHAT_ID_DUENO}, status_code=401)


@app.get("/entrar", response_class=HTMLResponse)
async def entrar(request: Request, t: str = ""):
    """La puerta. El token del enlace mágico se cambia por una cookie de sesión.

    El token viaja en la URL y por eso vive 10 minutos; la cookie vive una
    semana y nunca aparece en un historial ni en un log de servidor.
    """
    chat = auth.validar(t)
    if not auth.puede_entrar(chat):
        return plantillas.TemplateResponse(
            request, "entrar.html",
            {"chat": config.CHAT_ID_DUENO, "error": bool(t)}, status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(COOKIE, auth.crear_token(chat, auth.VIDA_SESION),
                 max_age=auth.VIDA_SESION, httponly=True, samesite="lax",
                 secure=True)
    return r


@app.get("/", response_class=HTMLResponse)
async def resumen(request: Request):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    filas = await db.resumen_por_mes()
    # Se agrupa acá y no en SQL para que la plantilla no tenga que pensar:
    # {mes: {moneda: {tipo: total}}}
    meses: dict = {}
    for f in filas:
        meses.setdefault(f["mes"], {}).setdefault(f["moneda"], {})[f["tipo"]] = f["total"]
    salud = await db.salud_ingesta()
    return plantillas.TemplateResponse(
        request, "resumen.html", {"meses": meses, "salud": salud})


@app.get("/sin-clasificar", response_class=HTMLResponse)
async def cola(request: Request):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    # El desplegable ofrece el VOCABULARIO COMPLETO, no las categorías ya
    # usadas. Con base vacía "las ya usadas" son cero, y una cola de corrección
    # cuyo desplegable está vacío no se puede usar: no hay forma de empezar.
    # Se le suma lo que haya en la base por si alguna vez entró algo a mano.
    usadas = await db.categorias_usadas()
    categorias = CATEGORIAS + [c for c in usadas if c not in CATEGORIAS]
    return plantillas.TemplateResponse(
        request, "sin_clasificar.html",
        {"movs": await db.sin_clasificar(), "categorias": categorias})


@app.post("/categoria")
async def categoria(request: Request, movimiento_id: int = Form(...),
                    categoria: str = Form("")):
    """La única escritura del panel. Queda en log_acciones como todo lo demás.

    La categoría se comprueba contra la lista cerrada. El desplegable ya solo
    ofrece esas, pero un vocabulario que solo se respeta si el formulario se
    porta bien no es un vocabulario cerrado: basta un POST a mano para meter
    "supermercado" en minúscula y partir el total en dos para siempre.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    limpia = categoria.strip()
    # "" es legítimo: es sacarle la categoría a un movimiento mal corregido.
    if limpia and limpia not in CATEGORIAS:
        return RedirectResponse("/sin-clasificar", status_code=303)
    await db.poner_categoria(movimiento_id, limpia)
    return RedirectResponse("/sin-clasificar", status_code=303)


@app.get("/movimientos", response_class=HTMLResponse)
async def movimientos(request: Request, desde: str = "", hasta: str = "",
                      tipo: str = "", categoria: str = "", banco: str = ""):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    def _f(s):
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    movs = await db.movimientos_filtrados(
        _f(desde), _f(hasta), tipo or None, categoria or None, banco or None)
    return plantillas.TemplateResponse(
        request, "movimientos.html",
        {"movs": movs, "categorias": await db.categorias_usadas(),
         "bancos": await db.bancos_usados(), "desde": desde, "hasta": hasta,
         "tipo": tipo, "categoria": categoria, "banco": banco})


@app.get("/salud", response_class=HTMLResponse)
async def salud(request: Request):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    s = await db.salud_ingesta()
    # "Hace cuánto" es la única cifra que importa acá: dice si el cero de la
    # portada significa "no gastaste" o "dejé de mirar".
    atraso = None
    if s["cuentas"]:
        ultimo = max(c["actualizado_en"] for c in s["cuentas"])
        atraso = datetime.now(ultimo.tzinfo) - ultimo
    return plantillas.TemplateResponse(
        request, "salud.html",
        {"s": s, "atraso": atraso, "umbral": timedelta(hours=2)})
