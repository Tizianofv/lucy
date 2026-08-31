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

  /movimientos      El detalle, filtrable — y donde se CAMBIA una categoría ya
                    puesta. Sin esa segunda parte, un error quedaba fijo para
                    siempre: la cola solo trae lo que no tiene categoría, así
                    que una mal puesta no volvía nunca y encima seguía
                    enseñándole lo mismo al sistema en cada compra siguiente.

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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import db.db as db
import web.auth as auth
from cerebro.bancos.categorias import CATEGORIAS, NO_SUMAN

log = logging.getLogger("lucy.panel")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
plantillas = Jinja2Templates(directory="web/plantillas")

COOKIE = "lucy_panel"


def _pesos(v) -> str:
    """Formato dominicano: 1,234.56. Sin símbolo — la moneda va aparte, porque
    mezclarlas en la misma columna es justo lo que este panel no hace."""
    return f"{Decimal(v):,.2f}" if v is not None else "—"


def _codigo(mid) -> str:
    """El id del movimiento, en la forma que se dice en voz alta: M-0086.

    NO es una columna nueva. El id de Postgres ya es único y estable, así que
    guardar además un código sería guardar dos veces el mismo hecho — y dos
    copias del mismo hecho se desincronizan, siempre. Esto es presentación.

    Solo dígitos a propósito: nada de letras que se confundan al dictarlo (0/O,
    1/l). Y el prefijo M- para que se reconozca como código de movimiento
    cuando aparezca suelto en un mensaje.
    """
    return f"M-{int(mid):04d}" if mid is not None else "—"


def _leer_codigo(texto: str) -> int | None:
    """'M-0086', 'm86', '  86  ' → 86. Cualquier otra cosa → None.

    Acepta las formas en que una persona lo escribe de verdad: con prefijo o
    sin él, con ceros o sin ellos, en mayúscula o minúscula. Un buscador que
    exige el formato exacto es un buscador que no se usa.
    """
    import re as _re
    m = _re.fullmatch(r"\s*[mM]?[-\s]*0*(\d{1,18})\s*", texto or "")
    return int(m.group(1)) if m else None


plantillas.env.filters["pesos"] = _pesos
plantillas.env.filters["codigo"] = _codigo


def _destino_seguro(volver: str) -> str:
    """A dónde se puede volver después de guardar. Solo rutas propias.

    Vive en su propia función para poder PROBARLA. Antes la decisión estaba
    suelta dentro del endpoint y su test se limitaba a comprobar que el código
    fuente contuviera cierto texto —daba tranquilidad sin dar garantía, y no
    habría detectado ninguna regresión de comportamiento—.

    EL startswith NO ES DECORATIVO. Comprobado contra Starlette el 31-ago:

      "//evil.com"        Location: //evil.com — redirect ABIERTO: es una URL
                          protocolo-relativa y se va del sitio. Lo frena esto
                          y nada más.
      "https://evil.com"  igual de abierto. Lo mismo.
      con CR/LF           Starlette lo percent-codifica sola, así que la
                          inyección de cabeceras está tapada río abajo. No
                          dependemos de eso igual.
      "/movimientos/../x" pasa, y es inofensivo: sigue siendo ruta de este
                          mismo sitio, y todas piden sesión.
    """
    limpio = (volver or "").strip()
    return limpio if limpio.startswith("/movimientos") else "/sin-clasificar"


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
async def resumen(request: Request, mes: str = ""):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    filas = await db.resumen_por_mes()
    # Se agrupa acá y no en SQL para que la plantilla no tenga que pensar:
    # {mes: {moneda: {tipo: total}}}
    meses: dict = {}
    for f in filas:
        meses.setdefault(f["mes"], {}).setdefault(f["moneda"], {})[f["tipo"]] = f["total"]
    salud = await db.salud_ingesta()

    # El desglose por categoría, con la moneda SEPARADA. Se arma acá y no en la
    # plantilla: {moneda: [filas ordenadas de mayor a menor]}. Que cada moneda
    # tenga su propia tabla no es un detalle de presentación — es la única forma
    # de que nadie lea "45,000" y crea que incluye los dólares.
    disponibles = await db.meses_con_movimientos()
    elegido = mes if mes in disponibles else (disponibles[0] if disponibles else None)
    por_moneda: dict = {}
    for f in await db.gasto_por_categoria(elegido):
        por_moneda.setdefault(f["moneda"], []).append(f)

    # El detalle de cada categoría, para poder desplegarla. Se pide una vez y
    # se agrupa acá: son ~130 filas, y la alternativa —una consulta por clic—
    # abriría la base cada vez que alguien tiene curiosidad.
    detalle: dict = {}
    for m in await db.gastos_de_cada_categoria(elegido):
        detalle.setdefault((m["moneda"], m["categoria"]), []).append(m)
    # El total EXCLUYE las que no suman. Que la consulta las marque y la
    # plantilla las pinte debajo no alcanzaba: este sum() las recorría todas, y
    # el "TOTAL DOP" incluía el dinero de terceros. La marca servía para
    # mirarlas aparte y no para lo único que su nombre promete.
    totales = {mo: sum(f["total"] for f in fs if not f["no_suma"])
               for mo, fs in por_moneda.items()}

    return plantillas.TemplateResponse(
        request, "resumen.html",
        {"meses": meses, "salud": salud, "por_moneda": por_moneda,
         "totales": totales, "mes_elegido": elegido,
         "detalle": detalle, "meses_disponibles": disponibles})


@app.get("/sin-clasificar", response_class=HTMLResponse)
async def cola(request: Request, guardados: int = 0):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    # El desplegable ofrece el VOCABULARIO COMPLETO, no las categorías ya
    # usadas. Con base vacía "las ya usadas" son cero, y una cola de corrección
    # cuyo desplegable está vacío no se puede usar: no hay forma de empezar.
    #
    # Y ofrece EXACTAMENTE lo que POST /categorias acepta, ni una más. Sumarle
    # las categorías heredadas de la base parecía generoso y era una trampa:
    # ponía en el desplegable opciones que la validación rechaza siempre, o sea
    # opciones garantizadas a fallar en silencio.
    return plantillas.TemplateResponse(
        request, "sin_clasificar.html",
        {"movs": await db.sin_clasificar(), "categorias": CATEGORIAS,
         "guardados": guardados})


@app.post("/categorias")
async def categorias(request: Request):
    """La única escritura del panel. Queda en log_acciones como todo lo demás.

    Guarda TODA la tabla de una vez. Antes era una fila por envío, y como cada
    guardado recargaba la página, se llevaba puesto lo que ya estaba elegido en
    las demás filas: había que marcar y guardar de uno en uno. Con cuarenta
    movimientos eso no lo hace nadie, y una cola que no se corrige no le enseña
    nada al sistema — o sea que el defecto de usabilidad se comía la función.

    Los campos vienen como cat_<id>. La categoría se comprueba contra la lista
    cerrada: el desplegable ya solo ofrece esas, pero un vocabulario que solo se
    respeta si el formulario se porta bien no es un vocabulario cerrado — basta
    un POST a mano para meter "supermercado" en minúscula y partir el total en
    dos para siempre.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    formulario = await request.form()
    guardados, rechazados = 0, 0
    for campo, valor in formulario.items():
        if not campo.startswith("cat_"):
            continue
        try:
            mid = int(campo[4:])
        except ValueError:
            rechazados += 1
            continue

        limpia = str(valor).strip()
        # `prev_<id>` es lo que la fila tenía cuando se pintó la pantalla. Sin
        # eso no se puede distinguir "no toqué esta fila" de "la vacié a
        # propósito": en la cola todo llega vacío y saltarse los vacíos está
        # bien, pero en /movimientos vaciar una es justamente cómo se deshace
        # una categoría equivocada.
        previa = str(formulario.get(f"prev_{mid}", "")).strip()
        if limpia == previa:
            continue
        if limpia and limpia not in CATEGORIAS:
            rechazados += 1
            continue

        await db.poner_categoria(mid, limpia)
        if not limpia and previa:
            # Vaciarla también DESAPRENDE el comercio. Si no, la corrección
            # duraba hasta la próxima compra en el mismo sitio: la regla vieja
            # seguía viva y volvía a ponerle la categoría que se acababa de
            # quitar, sin pasar por ninguna cola.
            await db.olvidar_categoria(mid)
        guardados += 1

    if rechazados:
        # Un rechazo que no deja rastro en ningún lado es un fallo silencioso, y
        # este proyecto paga por que todo sea auditable.
        log.warning("Panel: %s categoría(s) rechazadas por no estar en la "
                    "lista cerrada", rechazados)
    # Se vuelve a la pantalla de donde vino, con sus filtros puestos. Mandarlo
    # siempre a la cola le haría perder el filtro que estaba mirando, que en
    # /movimientos es la mitad del trabajo.
    #
    # EL startswith NO ES DECORATIVO, y conviene saber qué frena antes de
    # "simplificarlo". Comprobado a mano contra Starlette el 31-ago:
    #
    #   "//evil.com"        Location: //evil.com — redirect ABIERTO, es una URL
    #                       protocolo-relativa y se va del sitio. Lo frena ESTA
    #                       línea y nada más.
    #   "https://evil.com"  igual de abierto. La misma línea.
    #   "/movimientos" + CR/LF   Starlette lo percent-codifica solo, así que la
    #                       inyección de cabeceras ya está tapada río abajo.
    #                       No dependemos de eso igual.
    #   "/movimientos/../x" pasa, y es inofensivo: sigue siendo una ruta de este
    #                       mismo sitio, y todas piden sesión.
    destino = _destino_seguro(str(formulario.get("volver", "")))
    sep = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{sep}guardados={guardados}",
                            status_code=303)


@app.get("/movimientos", response_class=HTMLResponse)
async def movimientos(request: Request, desde: str = "", hasta: str = "",
                      tipo: str = "", categoria: str = "", banco: str = "",
                      codigo: str = "", guardados: int = 0):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    def _f(s):
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    buscado = _leer_codigo(codigo)
    movs = await db.movimientos_filtrados(
        _f(desde), _f(hasta), tipo or None, categoria or None, banco or None,
        buscado)
    return plantillas.TemplateResponse(
        request, "movimientos.html",
        {"movs": movs, "categorias": await db.categorias_usadas(),
         "bancos": await db.bancos_usados(), "desde": desde, "hasta": hasta,
         "tipo": tipo, "categoria": categoria, "banco": banco,
         "codigo": codigo, "codigo_ilegible": bool(codigo.strip()) and buscado is None,
         # `categorias` (las usadas) es para el FILTRO: filtrar por una que
         # nadie usó no devuelve nada. `todas` es para EDITAR, y tiene que ser
         # el vocabulario completo o no se podría corregir hacia una categoría
         # que todavía no usa nadie.
         "todas": CATEGORIAS,
         # Para los ingresos y traspasos, solo las marcas — no los rubros.
         "no_suman": NO_SUMAN, "guardados": guardados,
         "volver": str(request.url.path) + (
             "?" + str(request.url.query) if request.url.query else "")})


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
