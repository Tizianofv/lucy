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

  /tareas           Lo que hay que hacer, para las DOS personas de la casa en
                    una sola lista. Es la primera pantalla que no habla de
                    plata: entró porque Rosi necesitaba ver y cerrar sus
                    pendientes sin pedírselo a Lucy por Telegram. Lo que la hace
                    distinta de una lista cualquiera está en db.grupo_de_tarea —
                    "atrasada" se dice ahí una sola vez, y es por DÍA.

SIN BUILD DE JAVASCRIPT. HTML renderizado en el servidor y CSS a mano. Meter un
toolchain de Node en un repo Python que despliega en Railway sería un segundo
proyecto de mantenimiento, y el tiempo es justo lo que este proyecto no tiene.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import db.db as db
import web.auth as auth
from acciones import crud
from cerebro.bancos.categorias import (CATEGORIAS, NO_SUMAN,
                                       categoria_permitida)

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


def _alfabetico(cs):
    """Las categorías ordenadas para el desplegable, sin que las tildes manden.

    Un sort() crudo pone "Educación" y "Teléfono/Internet" fuera de sitio,
    porque la Ó y la É van después de la Z en el orden de los códigos. Se
    ordena por el texto sin acentos para que salga como lo esperaría alguien
    buscando con el pulgar.

    CATEGORIAS mantiene su orden por frecuencia de uso —es lo que ve el agente
    de Telegram y lo que documenta el módulo—; acá se reordena solo para
    mostrar, que es donde Tiziano lo pidió.
    """
    import unicodedata

    def clave(c):
        return "".join(x for x in unicodedata.normalize("NFD", c)
                       if not unicodedata.combining(x)).upper()

    return sorted(cs, key=clave)


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


# El monto, en la única forma que se acepta: dígitos, y como mucho dos
# decimales. Se compila una vez.
#
# LOS CENTAVOS DE MÁS SE RECHAZAN, NO SE REDONDEAN. La columna es
# NUMERIC(12,2), así que Postgres guardaría 1.234 como 1.23 sin decir nada —
# y redondear dinero en silencio es exactamente lo que este proyecto no hace.
# Diez dígitos enteros es más de lo que cabe en NUMERIC(12,2) sin la parte
# decimal, así que un número absurdamente largo se frena acá y no en la base.
_MONTO = re.compile(r"^\d{1,10}(\.\d{1,2})?$")

# El suelo de la fecha. NO es una regla de negocio sobre hasta cuándo se puede
# cargar hacia atrás: es una malla contra un dígito del año que se resbaló.
#
# `date.fromisoformat` acepta tan contento "0026-09-04" y "1926-09-04", y una
# fila guardada en el año 26 no vuelve a aparecer en ninguna pantalla que
# alguien mire —el resumen va por mes, y ese mes nadie lo abre— así que el
# gasto se pierde EN SILENCIO, que es la familia de fallo que este panel
# combate. Los registros de Lucy empiezan en 2026; el suelo se deja seis años
# por debajo, deliberadamente flojo, para que ningún registro tardío legítimo
# lo toque y solo caiga el año mal tecleado.
PISO_FECHA = date(2020, 1, 1)


def _monto_valido(texto: str):
    """El texto del formulario → Decimal, o None si no es un monto.

    Vive en su propia función para poder PROBARLA sin levantar la app, igual
    que `_destino_seguro`. Un validador que solo se ejercita a través del
    endpoint se prueba a medias.
    """
    limpio = (texto or "").strip()
    if not _MONTO.fullmatch(limpio):
        return None
    valor = Decimal(limpio)
    # Cero no es un gasto, y negativo no puede llegar (el patrón no lo deja):
    # el monto se guarda SIEMPRE positivo y la dirección la da `tipo`.
    return valor if valor > 0 else None


def _fecha_valida(texto: str, hoy: date):
    """El texto del formulario → date, o None si no es una fecha que se pueda
    guardar. `hoy` se pasa para poder probar el borde sin depender del reloj.

    TRES COSAS SE RECHAZAN, y ninguna se arregla adivinando:

    1. Lo que no parsea —vacío, "abc", "2026-13-45", "2026-02-30"—. Acá NO se
       cae a la fecha de hoy: guardar una fila con una fecha que la persona no
       eligió es el error silencioso que se estaría tapando. Se rechaza y se
       avisa.
    2. EL FUTURO. Un gasto en efectivo es plata que YA salió; una fecha por
       venir no es un gasto, es un plan, y Lucy no tiene planes de gasto. Peor:
       ensucia el resumen de un mes que todavía no cerró.
    3. Lo anterior a PISO_FECHA (ver arriba).

    "Hoy" es hoy EN SANTO DOMINGO, del reloj del servidor, y el mismo valor va
    al `max` del campo en la pantalla. Que las dos puntas salgan de la misma
    fuente es lo que evita que un navegador en otra zona horaria ofrezca un día
    que el servidor considera futuro.
    """
    try:
        f = date.fromisoformat((texto or "").strip())
    except ValueError:
        return None
    if f > hoy or f < PISO_FECHA:
        return None
    return f


def _hoy() -> date:
    """Hoy en Santo Domingo. Una sola definición, usada por el validador y por
    el `max` del campo de fecha: si se separan, se contradicen."""
    return datetime.now(config.TZ).date()


# El largo máximo del título de una tarea escrita a mano. Es la misma vara que
# `concepto` en POST /efectivo, y por el mismo motivo: el campo de la base es
# TEXT —no tiene tope— así que sin esto un POST a mano puede guardar un título
# de megabytes que después hay que pintar en una tabla.
LARGO_TITULO = 200


def _vence_valido(texto: str, piso: date = PISO_FECHA):
    """El campo «vence» del formulario → (aceptado, instante).

    Devuelve `(True, None)` cuando viene VACÍO: la fecha es opcional y la tarea
    nace sin fecha, que en este panel no es un hueco sino un grupo con nombre
    propio —«Sin fecha»—. Ese grupo existe porque las 4 tareas sin `vence_en`
    de producción llevaban meses invisibles; mandarlas a hoy por defecto sería
    volver a inventarles una fecha que nadie eligió.

    Devuelve `(False, None)` si el texto no es una fecha, o si cae por debajo
    de PISO_FECHA. El piso es el mismo que usa /efectivo y por el mismo motivo:
    no es una regla de negocio, es la malla contra un dígito del año que se
    resbaló.

    EL FUTURO SE ACEPTA, al revés que en /efectivo. Un gasto en efectivo es
    plata que ya salió; una tarea que vence el mes que viene es el caso normal
    de una tarea. Son la misma forma de campo y preguntas opuestas.

    LA HORA ES 23:59 EN SANTO DOMINGO, y las dos mitades de esa frase importan:

      · «23:59» porque el formulario pide un DÍA. «Vence el jueves» quiere decir
        que el jueves entero todavía sirve, así que el instante que representa
        ese día es su final y no su comienzo.
      · «en Santo Domingo» porque `tareas.vence_en` es TIMESTAMPTZ, o sea un
        instante, y el panel decide el grupo con `db.dia_rd`, que lo lee EN
        SANTO DOMINGO. Armado en UTC, el 23:59 del jueves se guardaría como un
        instante que en Santo Domingo todavía es del jueves a las 19:59 — pero
        el 00:00 de cualquier día armado en UTC cae en el día ANTERIOR en
        Santo Domingo. Una tarea escrita de noche nacería en el día equivocado.

    La zona sale de `config.TZ`, la misma que usa `db.dia_rd`: dos copias de una
    zona horaria se desincronizan igual que dos copias de cualquier otra cosa.
    """
    limpio = (texto or "").strip()
    if not limpio:
        return True, None
    try:
        dia = date.fromisoformat(limpio)
    except ValueError:
        return False, None
    if dia < piso:
        return False, None
    return True, datetime(dia.year, dia.month, dia.day, 23, 59,
                          tzinfo=config.TZ)


# El campo de día y hora del navegador (`<input type="datetime-local">`) manda
# exactamente esta forma: 2026-09-20T15:30, a veces con segundos. Nada más se
# acepta: ni un día suelto, ni una zona horaria pegada. La zona la pone el
# servidor (ver `_vence_con_hora_valido`).
_FECHA_Y_HORA = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")


def _vence_con_hora_valido(texto: str, piso: date = PISO_FECHA):
    """El campo de fecha de una tarea de la lista → (aceptado, instante).

    Es el hermano de `_vence_valido`, que lee el campo de «Agregar tarea». La
    diferencia es la que decidió Tiziano para mover fechas desde la lista: acá
    se pide DÍA Y HORA, no solo el día. Por eso un día suelto («2026-09-20»)
    se RECHAZA: guardarle una hora inventada (23:59) sería elegir por la
    persona la hora a la que suena el aviso.

    Devuelve `(True, None)` cuando viene VACÍO: eso es QUITAR la fecha, y
    Tiziano decidió que desde el panel se puede. La tarea pasa a «Sin fecha».

    Devuelve `(False, None)` si no es la forma exacta del campo, si no es una
    fecha que exista, o si cae por debajo del piso (la misma malla contra un
    dígito del año mal tecleado que usan /efectivo y «Agregar tarea»).

    LA HORA ES LA DE SANTO DOMINGO. El navegador manda una hora sin zona, y
    `tareas.vence_en` es un instante. La zona sale de `config.TZ`, la misma que
    usa `db.dia_rd` para repartir la lista en grupos: con otra zona, la tarea
    caería en otro día del que se ve en la pantalla.
    """
    limpio = (texto or "").strip()
    if not limpio:
        return True, None
    if not _FECHA_Y_HORA.fullmatch(limpio):
        return False, None
    try:
        crudo = datetime.fromisoformat(limpio)
    except ValueError:
        return False, None
    if crudo.date() < piso:
        return False, None
    return True, crudo.replace(tzinfo=config.TZ)


def _para_el_campo(cuando) -> str:
    """Un instante → el valor del campo de día y hora, EN SANTO DOMINGO.

    Es la vuelta de `_vence_con_hora_valido`, y las dos usan la misma zona
    (`config.TZ`): lo que se pinta es lo que se vuelve a leer. Un instante sin
    zona se lee como UTC, igual que en `db.dia_rd`. `None` → "", que en el
    campo es «sin fecha».

    Se corta al minuto: el campo del navegador trabaja por minutos, y el valor
    pintado viaja también como `prev_vence_`. Si la fila no se tocó, el campo
    vuelve igual a `prev_vence_` y el servidor no escribe nada.
    """
    if not isinstance(cuando, datetime):
        return ""
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return cuando.astimezone(config.TZ).strftime("%Y-%m-%dT%H:%M")


plantillas.env.filters["para_el_campo"] = _para_el_campo


def _hora_rd(cuando) -> str:
    """Un instante → «20/09/2026 15:30», en hora de Santo Domingo. Para leer,
    no para un campo. Un instante sin zona se lee como UTC, igual que en
    `db.dia_rd`."""
    if not isinstance(cuando, datetime):
        return ""
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return cuando.astimezone(config.TZ).strftime("%d/%m/%Y %H:%M")


plantillas.env.filters["hora_rd"] = _hora_rd


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
        {"movs": await db.sin_clasificar(),
         "categorias": _alfabetico(CATEGORIAS), "guardados": guardados})


@app.post("/categorias")
async def categorias(request: Request):
    """Guardar las categorías corregidas. Queda en log_acciones como todo lo demás.

    (Era "la única escritura del panel" hasta que se sumó POST /efectivo, que
    carga un gasto en efectivo. Ahora son dos, y las dos dejan su huella.)

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
         "todas": _alfabetico(CATEGORIAS),
         # Para los ingresos y traspasos, solo las marcas — no los rubros.
         "no_suman": NO_SUMAN, "guardados": guardados,
         # Para el formulario de efectivo: la fecha viene con hoy puesta y
         # acotada entre el piso y hoy. Los mismos dos valores que valida el
         # servidor, para que la pantalla no ofrezca lo que la ruta rechaza.
         "hoy": _hoy().isoformat(), "piso_fecha": PISO_FECHA.isoformat(),
         "volver": str(request.url.path) + (
             "?" + str(request.url.query) if request.url.query else "")})


@app.post("/efectivo")
async def efectivo(request: Request):
    """Un gasto en efectivo, escrito a mano. La segunda escritura del panel.

    POR QUÉ ACÁ Y NO EN /sin-clasificar: esa pantalla es la cola de corrección
    y tiene un test que exige UN SOLO <form> en su plantilla; un segundo
    formulario ahí rompe la razón por la que ese test existe. /movimientos es
    el registro —es donde vas a mirar si quedó— y es donde vive el filtro por
    banco que hace útil la marca.

    EL FORMULARIO NO PREGUNTA EL MÉTODO DE PAGO. Elegir "efectivo" en un
    formulario que solo carga efectivo es una decisión que no existe: antes de
    manejar el caso, se borra.

    Nada de lo que se rechaza devuelve un 500: todo sale por un 303 de vuelta a
    la pantalla, con `?error=` para que se vea qué pasó. Y cada rechazo deja
    una línea en el log del servidor, como hace /categorias con las categorías
    que no pasan: un rechazo sin rastro es un fallo silencioso.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    formulario = await request.form()
    destino = _destino_seguro(str(formulario.get("volver", "")))

    def _vuelta(clave: str):
        log.warning("Panel: gasto en efectivo rechazado por %s", clave)
        sep = "&" if "?" in destino else "?"
        return RedirectResponse(f"{destino}{sep}error={clave}", status_code=303)

    concepto = str(formulario.get("concepto", "")).strip()
    if not concepto or len(concepto) > 200:
        return _vuelta("concepto")

    monto = _monto_valido(str(formulario.get("monto", "")))
    if monto is None:
        return _vuelta("monto")

    fecha = _fecha_valida(str(formulario.get("fecha", "")), _hoy())
    if fecha is None:
        return _vuelta("fecha")

    # La categoría se comprueba contra la lista cerrada AUNQUE el desplegable
    # ya solo ofrezca esas. Mismo motivo que en /categorias: basta un POST a
    # mano para meter "supermercado" en minúscula y partir el total en dos para
    # siempre. Vacía se acepta: la fila cae sola en /sin-clasificar, que es lo
    # que ya hace todo lo demás sin categoría.
    categoria = str(formulario.get("categoria", "")).strip()
    if categoria and (categoria not in CATEGORIAS
                      or not categoria_permitida("gasto", categoria)):
        return _vuelta("categoria")

    mid = await db.crear_gasto_en_efectivo(concepto, monto, categoria, fecha)
    sep = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{sep}efectivo={mid}", status_code=303)


@app.post("/borrar")
async def borrar(request: Request):
    """A la papelera, no al vacío. Sale de las listas y vuelve si hace falta."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    destino = _destino_seguro(str(formulario.get("volver", "")))
    try:
        mid = int(str(formulario.get("movimiento_id", "")))
    except ValueError:
        return RedirectResponse(destino, status_code=303)
    await db.a_la_papelera(mid)
    sep = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{sep}borrado=1", status_code=303)


@app.post("/restaurar")
async def restaurar(request: Request):
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        mid = int(str(formulario.get("movimiento_id", "")))
    except ValueError:
        return RedirectResponse("/papelera", status_code=303)
    await db.restaurar(mid)
    return RedirectResponse("/papelera?restaurado=1", status_code=303)


@app.get("/papelera", response_class=HTMLResponse)
async def papelera(request: Request, restaurado: int = 0):
    """Lo borrado, con los días que le quedan.

    Existe para que "borrar" no dé miedo: sale de las listas al instante y se
    puede traer de vuelta durante 30 días. Lo que no se ve no se recupera, así
    que la papelera se muestra con el plazo delante.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    return plantillas.TemplateResponse(
        request, "papelera.html",
        {"movs": await db.papelera(), "dias": db.DIAS_EN_PAPELERA,
         "restaurado": restaurado})


@app.get("/tareas", response_class=HTMLResponse)
async def tareas(request: Request, guardadas: int = 0, creada: int = 0,
                 asignadas: int = 0, movidas: int = 0):
    """El panel de tareas: lo que hay que hacer, para las dos personas.

    UNA SOLA LISTA PARA LOS DOS, por decisión de Tiziano —"está bien que Rosi
    vea todo"—. La columna RESPONSABLE dice quién tiene pendiente cada cosa;
    partir la lista en dos por persona sería inventarle al panel una frontera
    que el trabajo de esta casa no tiene.

    ESTA RUTA NO SABE QUÉ ES "ATRASADA". El criterio vive entero en
    `db.grupo_de_tarea` y esta función solo pinta lo que aquella devuelve. Es
    el defecto que este panel vino a arreglar: hasta hoy "atrasada" se
    reescribía en cada consulta y daba números distintos según quién la
    escribiera.

    DE DÓNDE SALEN LOS NOMBRES, que es la parte que no existía en este sistema.
    Ninguna tabla de esta base tiene a la vez un chat y un nombre —medido sobre
    las 16 tablas y las 150 columnas del esquema—, así que los nombres viven en
    la variable `NOMBRES_POR_CHAT` de Railway. Se le pasan a la plantilla dos
    cosas distintas y conviene no confundirlas:

      · `personas` — a quién SE LE PUEDE ASIGNAR hoy. Sale de
        `config.personas_del_panel()`: los que pueden entrar al panel Y tienen
        nombre. Es lo que llena el desplegable, y por eso una tercera persona
        aparece sola el día que Tiziano la agregue a las dos variables.
      · `nombres` — cómo se llama CADA chat que tenga nombre, se le pueda
        asignar hoy o no. Es para PINTAR lo que ya está guardado: si alguien
        deja de poder entrar al panel, las tareas que tenía siguen diciendo su
        nombre. Esconderlo o cambiarlo por otra cosa sería reescribir el
        pasado.

    EL ÁREA (encargo 4) se pinta en la MISMA lista, no en cuatro listas
    separadas por área — eso partiría la pantalla en algo que Tiziano no pidió
    y escondería una tarea del área equivocada detrás de una pestaña. Cada
    fila lleva su etiqueta de color al lado del título; sin área se ve gris y
    con el texto «sin área», nunca escondida. El color de cada clave sale de
    `db.areas()` —no está escrito acá—, así que una quinta área trae su color
    solo. Si la migración no se aplicó todavía, `db.areas()` da `[]` y todas
    las tareas se pintan «sin área»: el panel sigue andando.

    Y LO QUE FALTA SE DICE. `sin_nombre` son los que pueden entrar y no tienen
    nombre puesto: mientras eso no sea cero, hay alguien a quien no se le puede
    asignar nada. `mal_escritos` son las entradas de la variable que no se
    entendieron. Las dos son CUENTAS y nunca un chat — el número de Telegram de
    una persona no es material de pantalla, y Tiziano ya descartó enseñarlo.
    Callar cualquiera de las dos dejaría a alguien buscando en el desplegable
    un nombre que nunca va a aparecer, sin ninguna pista de por qué.

    LAS CERRADAS HACE `db.DIAS_HISTORIAL` DÍAS O MÁS NO SE PINTAN ACÁ. Esta
    ruta es la única que las saca a propósito: `db.tareas_por_grupo` sí las
    reparte, en su propia clave declarada 'historial' — igual de real que
    'otros' o 'sin_fecha' — y acá se descarta esa clave antes de pintar. No se
    dejan de PEDIR a la base ni se dejan de CONTAR para `hay_mas`: se dejan de
    MOSTRAR, que es lo único que "se archivan" quiere decir en este panel. La
    página completa está en `/tareas/historial`.
    """
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    datos = await db.tareas_por_grupo()
    grupos = [g for g in datos["grupos"] if g["clave"] != "historial"]
    return plantillas.TemplateResponse(
        request, "tareas.html",
        {"grupos": grupos, "hay_mas": datos["hay_mas"],
         "guardadas": guardadas, "asignadas": asignadas, "movidas": movidas,
         "tope": db.TOPE_TAREAS, "hecha": db.ESTADO_HECHA, "creada": creada,
         # La fecha solo se puede mover en las PENDIENTES, y la regla vive en
         # `db.mover_vence`. Se le pasa el mismo valor a la plantilla para que
         # la pantalla no ofrezca el campo donde la escritura lo rechazaría.
         "pendiente": db.ESTADO_PENDIENTE, "piso_fecha": PISO_FECHA.isoformat(),
         "personas": config.personas_del_panel(),
         "asignables": [c for c, _ in config.personas_del_panel()],
         "nombres": config.NOMBRES_POR_CHAT,
         "sin_nombre": config.chats_sin_nombre(),
         "mal_escritos": config.NOMBRES_MAL_ESCRITOS,
         # {clave: color}, para pintar la etiqueta de cada fila sin que la
         # plantilla tenga que adivinar un color por su cuenta.
         "colores_area": {a["clave"]: a["color"] for a in await db.areas()}})


@app.get("/tareas/historial", response_class=HTMLResponse)
async def tareas_historial(request: Request):
    """Las tareas cerradas hace `db.DIAS_HISTORIAL` días o más.

    Es el mismo cálculo que la ruta `/tareas` le esconde al panel —la clave
    'historial' de `db.tareas_por_grupo`— y nada más: esta ruta no vuelve a
    decidir qué cuenta como vieja, porque ese criterio vive UNA vez, en
    `db.grupo_de_tarea`, igual que "atrasada".

    MISMO ACCESO QUE EL PANEL, la misma cookie de sesión: quien puede ver las
    tareas de hoy puede ver las de antes. No hay una puerta nueva que abrir ni
    que rotar.

    No hay forma de cerrar ni de reabrir una tarea desde acá — es una
    consulta, no un formulario — así que no hace falta CSRF ni nada que
    escriba: mostrar el Historial no puede, por construcción, mover una fila.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    datos = await db.tareas_por_grupo()
    historial = next((g for g in datos["grupos"] if g["clave"] == "historial"),
                     None)
    return plantillas.TemplateResponse(
        request, "tareas_historial.html",
        {"filas": historial["filas"] if historial else [],
         "tope": db.TOPE_TAREAS, "hay_mas": datos["hay_mas"],
         "dias": db.DIAS_HISTORIAL})


@app.get("/proyectos", response_class=HTMLResponse)
async def proyectos(request: Request, area_guardada: int = 0, creado: int = 0,
                    error: str = ""):
    """Cada proyecto vivo, con su área, su estado y sus tareas en orden
    (encargo 5, requisito 1).

    SOLO LECTURA salvo por el <select> de área de cada proyecto, que postea a
    `/proyectos/{pid}/area` — un formulario POR PROYECTO, y está bien acá
    (esta pantalla no es `/tareas`: no tiene el formulario único de cerrar
    varias de una vez, así que nada impide que cada proyecto tenga el suyo,
    igual que ya hace `/tareas/{tid}` con sus comentarios).
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    return plantillas.TemplateResponse(
        request, "proyectos.html",
        {"proyectos": await db.proyectos_con_tareas(),
         "areas": await db.areas(), "error": error,
         "area_guardada": area_guardada, "creado": creado,
         "pendiente": db.ESTADO_PENDIENTE, "hecha": db.ESTADO_HECHA})


@app.post("/proyectos/{pid}/area")
async def cambiar_area_de_proyecto(request: Request, pid: int):
    """Cambiar el área de un proyecto (pedido de Tiziano: "no veo las
    posibilidades de cambiarlo", encargo 5).

    POR LA MISMA PUERTA que Telegram: `crud.editar("proyectos", ...)`, que ya
    valida con `crud._area_que_vale` — sin una segunda comprobación acá. El
    único trabajo de esta ruta es traducir el formulario y el error a algo
    que se pueda mostrar; la decisión de qué área vale la toma esa función,
    en un solo sitio para los dos caminos (panel y Telegram).

    `actor='panel'`, como toda escritura que dispara un botón del panel — la
    huella queda igual de clara sobre quién la disparó que
    `crear_tarea_desde_el_panel` o `convertir_tarea_en_proyecto`.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    area = str(formulario.get("area", "")).strip() or None
    try:
        despues, _log_id = await crud.editar(
            "proyectos", pid, {"area": area},
            motivo="Área cambiada desde el panel", actor="panel")
    except ValueError as e:
        log.warning("Panel de proyectos: área rechazada para #%s: %s", pid, e)
        return RedirectResponse("/proyectos?error=area", status_code=303)
    if despues is None:
        return RedirectResponse(f"/proyectos?error=proyecto", status_code=303)
    return RedirectResponse(f"/proyectos?area_guardada={pid}", status_code=303)


@app.post("/tareas/{tid}/area")
async def cambiar_area_de_tarea(request: Request, tid: int):
    """Cambiar el área de UNA tarea suelta (sin proyecto) desde el panel.

    MISMA PUERTA que Telegram: `crud.editar("tareas", ...)`, que llama a
    `crud._area_que_vale` — la única función que decide si un área vale, y la
    que YA sabe que una tarea con proyecto no tiene área propia (la hereda,
    silenciosamente, sin rechazar nada). Si esta ruta se llama sobre una
    tarea CON proyecto, `_area_que_vale` la ignora igual que por Telegram: no
    hay una comprobación aparte acá que diga "no se puede" antes de tiempo —
    la pantalla ya no ofrece el `<select>` en ese caso (ver
    `tarea_detalle.html`), pero si alguien postea de todos modos, el
    resultado es el mismo que pedirlo por chat.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    area = str(formulario.get("area", "")).strip() or None
    try:
        despues, _log_id = await crud.editar(
            "tareas", tid, {"area": area},
            motivo="Área cambiada desde el panel", actor="panel")
    except ValueError as e:
        log.warning("Panel de tareas: área rechazada para #%s: %s", tid, e)
        return RedirectResponse(f"/tareas/{tid}?error=area", status_code=303)
    if despues is None:
        return RedirectResponse(f"/tareas/{tid}?error=tarea", status_code=303)
    return RedirectResponse(f"/tareas/{tid}?area_guardada=1", status_code=303)


@app.post("/tareas/{tid}/primero")
async def cambiar_primero_de_tarea(request: Request, tid: int):
    """Elegir cuál tarea va «Primero:» (encargo 6). Vacío = no espera a nadie.

    MISMA PUERTA que Telegram: `crud.editar("tareas", ...)`, que llama a
    `crud._primero_que_vale` -- la única función que decide si un valor de
    «Primero:» vale, incluida la comprobación de que la tarea no se apunte a
    sí misma y de que la cadena no forme un CÍRCULO más largo (ver su
    docstring): si Tiziano elige algo que cerraría un círculo, esta ruta lo
    va a rechazar con el motivo que le llegue desde ahí, no con uno
    inventado acá.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    primero = str(formulario.get("primero_id", "")).strip() or None
    try:
        despues, _log_id = await crud.editar(
            "tareas", tid, {"primero_id": primero},
            motivo="«Primero:» cambiado desde el panel", actor="panel")
    except ValueError as e:
        log.warning("Panel de tareas: «Primero:» rechazado para #%s: %s", tid, e)
        return RedirectResponse(f"/tareas/{tid}?error=primero", status_code=303)
    if despues is None:
        return RedirectResponse(f"/tareas/{tid}?error=tarea", status_code=303)
    return RedirectResponse(f"/tareas/{tid}?primero_guardado=1", status_code=303)


@app.post("/tareas/{tid}/convertir-en-proyecto")
async def convertir_en_proyecto(request: Request, tid: int):
    """El botón «convertir en proyecto» (encargo 5, requisito 2).

    Sin formulario intermedio: un botón, un click. `db.convertir_tarea_en_proyecto`
    hace todo el trabajo —valida, crea el proyecto, archiva la tarea, dos
    huellas— dentro de una sola transacción; ver su docstring para qué pasa
    con el responsable, el área y los comentarios de la tarea original.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    try:
        resultado = await db.convertir_tarea_en_proyecto(tid)
    except ValueError as e:
        log.warning("Panel de tareas: no se convirtió #%s en proyecto: %s", tid, e)
        return RedirectResponse(f"/tareas/{tid}?error=convertir", status_code=303)
    return RedirectResponse(
        f"/proyectos?creado={resultado['proyecto_id']}"
        f"#proyecto-{resultado['proyecto_id']}",
        status_code=303)


def _responsable_pedido(crudo: str):
    """Lee un `resp_<id>` del formulario. Devuelve (vale, chat | None).

    Tres respuestas y no dos, porque son tres cosas distintas:

      · ("", …)  → (True, None): SIN RESPONSABLE. Es una petición legítima y
        de primera clase —así nacieron las 57 tareas que ya existían—, no un
        campo vacío que haya que rellenar.
      · un chat que puede serlo  → (True, chat).
      · cualquier otra cosa —texto, un chat que no entra al panel, un número
        inventado— → (False, None), y el que llama no escribe nada.

    QUIÉN PUEDE SER RESPONSABLE NO SE DECIDE ACÁ. Se le pregunta a
    `config.puede_ser_responsable`, que es la misma puerta que vuelve a mirar
    `db.asignar_responsable` antes de escribir. Que esté en los dos sitios no
    es duplicar el criterio: el criterio está UNA vez y los dos lo llaman.

    Y QUÉ TEXTO ES UN CHAT TAMPOCO SE DECIDE ACÁ: `config.chat_escrito`, la
    misma que usa el camino de Telegram. Con un `int()` propio este formulario
    leía `0<chat>` y `+<chat>` como el chat de una persona —el desplegable no
    los manda, pero un envío a mano sí— y entonces había DOS lecturas distintas
    de qué es un chat escrito. Que la lectura esté en un solo sitio es lo que
    impide que se separen, igual que con la puerta.
    """
    crudo = (crudo or "").strip()
    if not crudo:
        return True, None
    chat = config.chat_escrito(crudo)
    if chat is None:
        return False, None
    return (True, chat) if config.puede_ser_responsable(chat) else (False, None)


@app.post("/tareas")
async def guardar_tareas(request: Request):
    """Cerrar VARIAS tareas de una vez y cambiarles el responsable.

    UN SOLO BOTÓN PARA TODA LA TABLA, igual que /categorias y por el mismo
    motivo, que ahí está escrito con la cicatriz puesta: cuando cada fila se
    guardaba sola, el recargue se llevaba puesto lo que ya estaba marcado en
    las demás. Con cuarenta filas eso no lo hace nadie, y un panel que no se
    usa no le sirve a Rosi mañana.

    LAS FILAS QUE NADIE TOCÓ NO SE PISAN, y hay dos frenos para eso porque son
    dos preguntas distintas:

      · `prev_<id>` es lo que la fila tenía CUANDO SE PINTÓ. Un checkbox sin
        marcar no viaja en el formulario, así que la casilla sola no distingue
        "no la toqué" de "la desmarqué"; `prev_` es lo que hace la diferencia
        visible, igual que en POST /categorias.
      · y `db.marcar_tarea_hecha` vuelve a mirar la fila EN LA BASE antes de
        escribir. Eso cubre la pantalla vieja: si Rosi la cerró hace diez
        minutos, el envío de Tiziano no la vuelve a escribir ni duplica su
        huella en log_acciones.

    DESMARCAR NO DESHACE, y la pantalla lo dice: las que ya están hechas salen
    con la casilla marcada y DESHABILITADA. Qué tiene que pasar cuando alguien
    deshace un "hecha" —¿vuelve a pendiente?, ¿con qué fecha?— es una decisión
    de Tiziano que este encargo no tomó, y una casilla que parece deshacer y no
    deshace miente. Antes que manejar el caso, se borra.

    NO HAY REDIRECT A UN DESTINO ELEGIBLE. De acá se vuelve siempre a /tareas,
    así que no existe el parámetro `volver` que /categorias y /efectivo tienen
    que defender contra "//evil.com". El agujero que no existe no se tapa.

    EL RESPONSABLE VIAJA EN EL MISMO ENVÍO, y por eso es un desplegable dentro
    del formulario que ya había y no una pantalla aparte: cerrar dos y pasarle
    una tercera a la otra persona es UN gesto en la vida real, y partirlo en
    dos recargues es exactamente el defecto que este formulario único vino a
    arreglar. `/tareas/nueva` está aparte por otra razón —un <form> dentro de
    otro es HTML inválido—, que acá no aplica: el <select> vive DENTRO del
    mismo <form>.

    Y AQUÍ TAMBIÉN `prev_resp_` HACE FALTA, por un motivo distinto al de las
    casillas. Un <select> SIEMPRE viaja, incluso el que nadie tocó, así que sin
    el valor previo cada envío reescribiría el responsable de las cuarenta
    filas de la pantalla y llenaría `log_acciones` de ediciones que nadie
    pidió. Con él solo se escriben las que de verdad cambiaron.

    Y `db.asignar_responsable` vuelve a mirar la fila EN LA BASE antes de
    escribir, igual que `marcar_tarea_hecha`: la pantalla que lleva diez
    minutos abierta no puede pisar lo que el otro acaba de cambiar con un valor
    que ya coincidía.

    LA FECHA TAMBIÉN VIAJA EN EL MISMO ENVÍO (13-sep-2026): cada pendiente
    trae `vence_<id>` (día y hora) y `prev_vence_<id>` (lo que se pintó). Solo
    se escriben las filas cuyo campo cambió, por el mismo motivo que el
    responsable: el campo viaja siempre, tocado o no. Vacío QUITA la fecha.

    LAS FECHAS VAN ANTES QUE LAS CASILLAS DE «HECHA». Si en el mismo envío
    alguien mueve la fecha de una tarea y la marca hecha, las dos cosas se
    guardan. En el orden contrario, la tarea ya estaría hecha cuando llegara la
    fecha, `db.mover_vence` la rechazaría (solo mueve pendientes) y el cambio
    que la persona escribió se perdería.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    formulario = await request.form()
    hechas, asignadas, movidas, ignoradas = 0, 0, 0, 0

    for campo in formulario:
        if not campo.startswith("vence_"):
            continue
        try:
            tid = int(campo[len("vence_"):])
        except ValueError:
            ignoradas += 1
            continue
        pedido = str(formulario.get(campo, "")).strip()
        if pedido == str(formulario.get(f"prev_vence_{tid}", "")).strip():
            # Nadie tocó este campo: dice lo mismo que cuando se pintó.
            continue
        vale, vence_en = _vence_con_hora_valido(pedido)
        if not vale:
            ignoradas += 1
            continue
        if await db.mover_vence(tid, vence_en):
            movidas += 1
        else:
            ignoradas += 1

    for campo in formulario:
        if not campo.startswith("hecha_"):
            continue
        try:
            tid = int(campo[len("hecha_"):])
        except ValueError:
            ignoradas += 1
            continue
        previa = str(formulario.get(f"prev_{tid}", "")).strip()
        if previa == db.ESTADO_HECHA:
            # Ya estaba hecha cuando se pintó: no hay nada que cambiar.
            continue
        if await db.marcar_tarea_hecha(tid):
            hechas += 1
        else:
            ignoradas += 1

    for campo in formulario:
        if not campo.startswith("resp_"):
            continue
        try:
            tid = int(campo[len("resp_"):])
        except ValueError:
            ignoradas += 1
            continue
        pedido = str(formulario.get(campo, "")).strip()
        if pedido == str(formulario.get(f"prev_resp_{tid}", "")).strip():
            # Nadie tocó este desplegable: dice lo mismo que cuando se pintó.
            continue
        vale, chat = _responsable_pedido(pedido)
        if not vale:
            ignoradas += 1
            continue
        if await db.asignar_responsable(tid, chat):
            asignadas += 1
        else:
            ignoradas += 1

    if ignoradas:
        # Un rechazo que no deja rastro en ningún lado es un fallo silencioso,
        # y este panel paga por que todo sea auditable. Va acá y no en db.db
        # porque el logger de este módulo existe y el de aquél no (ver el
        # hallazgo sobre db/db.py:1648).
        #
        # No se registra QUÉ responsable se pidió: sería el número de Telegram
        # de una persona en el log de Railway, y ese número no hace falta para
        # entender qué pasó.
        log.warning("Panel de tareas: %s cambio(s) sin efecto —la tarea no "
                    "existe, está en la papelera, ya estaba así, no está "
                    "pendiente, la fecha no se entendió, o el responsable "
                    "pedido no entra al panel", ignoradas)
    return RedirectResponse(
        f"/tareas?guardadas={hechas}&asignadas={asignadas}&movidas={movidas}",
        status_code=303)


@app.get("/tareas/nueva", response_class=HTMLResponse)
async def tarea_nueva(request: Request, error: str = ""):
    """El formulario para escribir una tarea a mano.

    POR QUÉ ES UNA PANTALLA APARTE Y NO UN SEGUNDO FORMULARIO EN /tareas, que
    es lo primero que uno intentaría: `/tareas` tiene un test que exige UN SOLO
    <form> en su plantilla
    (`tests/test_panel_tareas.py::test_un_solo_formulario_para_toda_la_pantalla`),
    y ese test no es un capricho — la pantalla cierra VARIAS tareas con un solo
    botón justamente porque un formulario por fila recargaba la página y se
    llevaba puesto todo lo demás marcado sin enviar. Meterle un segundo <form>
    al lado rompe la razón por la que ese test existe, y además un <form>
    dentro de otro es HTML inválido: el navegador descarta el de adentro y el
    botón deja de hacer nada, EN SILENCIO.

    Es la misma decisión que ya tomó POST /efectivo, que por eso vive en
    /movimientos y no en /sin-clasificar. Acá el equivalente es una pantalla
    propia, y en /tareas queda un ENLACE —no un formulario— que se ve también
    cuando la lista está vacía, que es justo cuando hace falta escribir la
    primera.

    EL ÁREA (encargo 4) se puede elegir siempre en esta pantalla, sin
    excepción: `/tareas/nueva` no tiene selector de proyecto —no se pidió, ver
    el docstring de `crear_tarea`—, así que el caso que tendría que esconder
    el selector («ya tiene proyecto, el área es la del proyecto») no existe
    acá. `db.areas()` ya tolera que la tabla no exista (devuelve `[]`), así
    que si la migración no se aplicó todavía el `<select>` sale vacío —solo
    queda «Sin área»— en vez de romper la pantalla.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    return plantillas.TemplateResponse(
        request, "tarea_nueva.html",
        {"error": error, "piso_fecha": PISO_FECHA.isoformat(),
         "largo_titulo": LARGO_TITULO, "areas": await db.areas()})


@app.post("/tareas/nueva")
async def crear_tarea(request: Request):
    """Escribir una tarea a mano. La cuarta escritura del panel.

    TRES CAMPOS: título, cuándo vence, y —desde el encargo 4— área. La tabla
    tiene prioridad, recurrencia, proyecto, persona y anticipos, y ninguno de
    ésos entra acá — no se pidieron, y `prioridad` está vacía en las 91 filas
    de producción. Un campo que nadie llenó es una decisión inventada
    esperando a que alguien la crea. El área SÍ se pidió (requisito 3 del
    encargo 4): esta pantalla nunca elige proyecto, así que no hay excepción
    que aplicarle — se ofrece siempre.

    QUIÉN LA ANOTÓ SALE DE LA SESIÓN, no de un campo del formulario. Es el
    mismo chat que ya se comprobó para dejar entrar: un formulario que
    preguntara quién sos aceptaría la respuesta que le den.

    Nada de lo que se rechaza devuelve un 500: todo sale por un 303 de vuelta
    al formulario, con `?error=` para que se vea qué pasó, igual que
    /efectivo. Y cada rechazo deja una línea en el log del servidor: un rechazo
    sin rastro es un fallo silencioso.

    EL ÁREA SE VALIDA CONTRA `db.areas()`, no contra una lista escrita en este
    archivo: lo que el `<select>` ofreció es lo único que se acepta, y si
    mañana hay una quinta área, entra sola por los dos lados sin tocar esta
    ruta. Vacío = sin área, que es válido siempre. Si de todos modos llega una
    clave que no está en `db.areas()` —el formulario viejo en caché de un
    navegador, o alguien posteando a mano—, se rechaza igual que un título
    vacío: no se deja que la FK `tareas.area → areas.clave` lo convierta en un
    500.

    LO QUE SE ESCRIBIÓ NO VUELVE EN LA URL. Son pocos campos y volver a
    escribirlos cuesta poco; meter el título de una tarea en una query string
    lo deja en el historial del navegador y en el log de cualquier proxy, que
    es un precio bastante más alto.
    """
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)

    formulario = await request.form()

    def _vuelta(clave: str):
        log.warning("Panel de tareas: tarea a mano rechazada por %s", clave)
        return RedirectResponse(f"/tareas/nueva?error={clave}", status_code=303)

    titulo = str(formulario.get("titulo", "")).strip()
    if not titulo or len(titulo) > LARGO_TITULO:
        # Sin título no hay tarea: `tareas.titulo` es NOT NULL a propósito, y
        # una tarea que no dice qué hay que hacer no es una tarea. Se rechaza
        # y se dice; no se inventa un título por defecto.
        return _vuelta("titulo")

    ok, vence_en = _vence_valido(str(formulario.get("vence", "")))
    if not ok:
        return _vuelta("fecha")

    area = str(formulario.get("area", "")).strip() or None
    if area is not None:
        claves_validas = {a["clave"] for a in await db.areas()}
        if area not in claves_validas:
            return _vuelta("area")

    tid = await db.crear_tarea_desde_el_panel(chat, titulo, vence_en, area)
    # Se vuelve A LA LISTA y no al formulario: la tarea recién escrita tiene
    # que VERSE en su grupo. Un "guardado" que no muestra lo guardado obliga a
    # confiar, y este panel existe para no tener que confiar.
    return RedirectResponse(f"/tareas?creada={tid}", status_code=303)


# El largo máximo de un comentario. No es una regla de negocio: `texto` es TEXT
# y no tiene tope, así que sin esto un POST hecho a mano puede guardar megabytes
# que después hay que pintar y mandarle al modelo. Es la misma idea que
# LARGO_TITULO, con más holgura porque un comentario es para escribir más.
LARGO_COMENTARIO = 2000


def _texto_de_comentario(crudo: str) -> str | None:
    """El cuadro de texto → el comentario a guardar, o None si no vale.

    Los saltos de línea del navegador (\\r\\n) se guardan como \\n: así el mismo
    comentario es el mismo texto, lo haya escrito quien lo haya escrito. Y
    Lucy lo reconoce por su texto exacto (`cerebro.consultar`).
    """
    limpio = (crudo or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not limpio or len(limpio) > LARGO_COMENTARIO:
        return None
    return limpio


@app.get("/tareas/{tid}", response_class=HTMLResponse)
async def tarea_detalle(request: Request, tid: int, error: str = "",
                        comentado: int = 0, borrado: int = 0,
                        area_guardada: int = 0, primero_guardado: int = 0,
                        paso_agregado: int = 0, paso_movido: int = 0):
    """Una tarea con sus comentarios, y el cuadro para escribir uno.

    VA EN SU PROPIA PANTALLA, a la que se entra tocando el título en /tareas.
    Esa lista tiene UN SOLO formulario a propósito (ver `guardar_tareas`), y un
    cuadro de texto por fila en una lista de cincuenta no se usa en un celular.

    QUIÉN ESCRIBIÓ CADA COMENTARIO se pinta con su NOMBRE, sacado de
    `NOMBRES_POR_CHAT`. Si no tiene nombre se pinta «sin nombre», nunca el
    número de chat (Tiziano descartó enseñarlo en el panel).

    La ruta `/tareas/nueva` está registrada ANTES que ésta, así que «nueva»
    nunca llega acá.

    `areas` (encargo 5) es para el `<select>` de cambiar el área -- que la
    plantilla solo ofrece si `tarea.proyecto_id is none`, y para el botón
    «convertir en proyecto» -- que solo ofrece si además está pendiente.

    `candidatos_primero` (encargo 6) es para el `<select>` de «Primero:» --
    TODAS las tareas vivas menos ella misma. `db.tareas_para_elegir_primero`
    no filtra círculos (ver su docstring): elegir uno se rechaza al GUARDAR,
    con el mensaje de `crud._primero_que_vale`, que es quien recorre la
    cadena antes de escribir (ver su docstring para el porqué no lo hace la
    base).

    `pasos` (encargo 7) es la lista de chequeo -- `db.tarea_con_comentarios`
    ya la trae, tolerando la tabla ausente (ver `db.pasos_de_tarea`).
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    datos = await db.tarea_con_comentarios(tid)
    areas = await db.areas()
    contexto = {"tarea": None, "comentarios": [], "pasos": [],
                "nombres": config.NOMBRES_POR_CHAT, "error": error,
                "comentado": comentado, "borrado": borrado,
                "area_guardada": area_guardada,
                "primero_guardado": primero_guardado,
                "paso_agregado": paso_agregado, "paso_movido": paso_movido,
                "largo_comentario": LARGO_COMENTARIO,
                "areas": areas, "pendiente": db.ESTADO_PENDIENTE,
                "colores_area": {a["clave"]: a["color"] for a in areas},
                "candidatos_primero": []}
    if datos is None:
        return plantillas.TemplateResponse(
            request, "tarea_detalle.html", contexto, status_code=404)
    contexto.update(tarea=datos["tarea"], comentarios=datos["comentarios"],
                    pasos=datos["pasos"],
                    candidatos_primero=await db.tareas_para_elegir_primero(tid))
    return plantillas.TemplateResponse(request, "tarea_detalle.html", contexto)


@app.post("/tareas/{tid}/pasos")
async def agregar_pasos(request: Request, tid: int):
    """Agregar uno o varios micro-pasos a una tarea (encargo 7).

    UN TEXTO POR LÍNEA: el `<textarea>` del panel manda un solo campo
    `texto` con saltos de línea -- «divide en 3 pasos» escrito a mano es
    escribir tres líneas, no rellenar tres campos. `crud.crear_pasos` ya
    descarta las líneas vacías; acá solo se parte el texto.

    MISMA PUERTA que Telegram: `crud.crear_pasos`, que valida con
    `_tarea_viva_que_vale` -- sin comprobación aparte acá.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    lineas = str(formulario.get("texto", "")).splitlines()
    try:
        creados = await crud.crear_pasos(
            tid, lineas, motivo="Pasos agregados desde el panel de tareas",
            actor="panel")
    except (ValueError, crud.FaltanDatos) as e:
        log.warning("Panel de tareas: no se agregaron pasos a #%s: %s", tid, e)
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    return RedirectResponse(
        f"/tareas/{tid}?paso_agregado={len(creados)}", status_code=303)


@app.post("/tareas/{tid}/pasos/{pid}/hecho")
async def marcar_paso(request: Request, tid: int, pid: int):
    """Marcar (o desmarcar) UN micro-paso (encargo 7).

    Reusa `crud.editar` ENTERO -- la misma huella, el mismo deshacer que
    cualquier otra edición genérica -- en vez de una escritura aparte. El
    valor que llega es el que el checkbox YA tiene DESPUÉS del clic (ver la
    plantilla: cada checkbox manda su propio formulario con el valor al
    que apunta, no el que tenía).
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    hecho = str(formulario.get("hecho", "")).strip() == "1"
    despues, _log_id = await crud.editar(
        "micro_pasos", pid, {"hecho": hecho},
        motivo="Paso marcado desde el panel de tareas", actor="panel")
    if despues is None or despues.get("tarea_id") != tid:
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    return RedirectResponse(f"/tareas/{tid}", status_code=303)


@app.post("/tareas/{tid}/pasos/{pid}/borrar")
async def quitar_paso(request: Request, tid: int, pid: int):
    """Quitar UN micro-paso (encargo 7). `crud.borrar` -- ya gratis, `micro_
    pasos` está en `crud.TABLAS`, así que esto es soft-delete + huella +
    deshacer, sin escribir nada nuevo."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    log_id = await crud.borrar(
        "micro_pasos", pid, motivo="Paso quitado desde el panel de tareas")
    if log_id is None:
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    return RedirectResponse(f"/tareas/{tid}", status_code=303)


@app.post("/tareas/{tid}/pasos/{pid}/mover")
async def mover_paso_de_tarea(request: Request, tid: int, pid: int):
    """Subir o bajar UN micro-paso (encargo 7). `db.mover_paso` intercambia
    el `orden` con el vecino -- no hay `<select>` de orden libre, dos
    botones alcanzan para una lista corta."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    direccion = str(formulario.get("direccion", ""))
    movido = await db.mover_paso(tid, pid, direccion)
    return RedirectResponse(
        f"/tareas/{tid}?paso_movido={1 if movido else 0}", status_code=303)


@app.post("/tareas/{tid}/comentarios")
async def comentar(request: Request, tid: int):
    """Escribir un comentario en una tarea.

    EL AUTOR SALE DE LA SESIÓN, no del formulario: es el chat del token
    firmado de la cookie, el mismo que ya se comprobó para dejar entrar. Si el
    formulario trae un campo que diga otro autor, se ignora: la ruta no lo lee.

    Nada de lo que se rechaza devuelve un 500: todo vuelve a la pantalla de la
    tarea con `?error=`, y cada rechazo deja una línea en el log. El texto
    escrito NO vuelve en la URL, por lo mismo que en «Agregar tarea».
    """
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    texto = _texto_de_comentario(str(formulario.get("texto", "")))
    if texto is None:
        log.warning("Panel de tareas: comentario rechazado por el texto "
                    "(vacío o más largo de %s)", LARGO_COMENTARIO)
        return RedirectResponse(f"/tareas/{tid}?error=texto", status_code=303)
    cid = await db.comentar_tarea(tid, chat, texto)
    if cid is None:
        log.warning("Panel de tareas: comentario no guardado —la tarea no "
                    "existe o está en la papelera")
        return RedirectResponse(f"/tareas/{tid}?error=tarea", status_code=303)
    return RedirectResponse(f"/tareas/{tid}?comentado=1", status_code=303)


@app.post("/tareas/{tid}/comentarios/{cid}/borrar")
async def borrar_comentario_de_tarea(request: Request, tid: int, cid: int):
    """Borrar un comentario. Cualquiera de los dos puede borrar cualquiera, por
    decisión de Tiziano. Quién lo borró sale de la sesión y queda guardado."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    if not await db.borrar_comentario(cid, tid, chat):
        log.warning("Panel de tareas: comentario no borrado —no existe, ya "
                    "estaba borrado o es de otra tarea")
        return RedirectResponse(f"/tareas/{tid}?error=comentario",
                                status_code=303)
    return RedirectResponse(f"/tareas/{tid}?borrado=1", status_code=303)


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
    # Una línea por banco, sin umbral y sin aviso: dice desde cuándo no entra
    # nada de cada uno y deja que la lea quien sabe si eso es raro. Ponerle un
    # número inventado a "cada cuánto debería llegar algo del BHD" fabricaría
    # una alarma que grita en falso, y ya tuvimos una.
    por_banco = await db.silencio_por_banco()
    ahora = datetime.now(timezone.utc)
    for b in por_banco:
        b["dias"] = ((ahora - b["ultimo"]).days
                     if b.get("ultimo") is not None else None)
    return plantillas.TemplateResponse(
        request, "salud.html",
        {"s": s, "atraso": atraso, "umbral": timedelta(hours=2),
         "por_banco": por_banco,
         # Se muestran, no se fusionan: equivocarse borrando pierde un gasto
         # real en silencio, y lo silencioso es lo que este panel combate.
         "duplicados": await db.posibles_duplicados()})
