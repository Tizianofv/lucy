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

import asyncio
import contextvars
import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.templating import Jinja2Templates

import config
import db.db as db
import noco_lectura
import web.auth as auth
import web.menu as _menu
import web.avisos as avisos_mod
from web.avisos import Aviso, AvisoQueElige, Navegacion, PuertaDeAvisos
from acciones import crud
from cerebro.bancos.categorias import (CATEGORIAS, NO_SUMAN,
                                       categoria_permitida)
from web.api_code import router as api_code_router

log = logging.getLogger("lucy.panel")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# La puerta de las tareas de Code (26-sep-2026, §C): rutas aparte, con su
# PROPIA autenticación por clave -- nunca la cookie `lucy_panel` de abajo.
# Ver `web/api_code.py` para el porqué de vivir en el mismo proceso.
app.include_router(api_code_router)
# La puerta de los avisos (`web/avisos.py`): ningún aviso de «se guardó» o «no se
# pudo» sale de una dirección escrita a mano; solo de lo que un POST de verdad dejó.
app.add_middleware(PuertaDeAvisos)
plantillas = Jinja2Templates(directory="web/plantillas")

COOKIE = "lucy_panel"
# La sesión de «solo ver» (entrada desde la App de registro): cookie APARTE, que
# solo abre las rutas declaradas `PUERTA_VER`. `_sesion` no la lee (ver `web/auth.py`).
COOKIE_VER = "lucy_ver"


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
LARGO_TITULO = db.LARGO_TITULO_TAREA
LOGO_CDS = Path(__file__).resolve().parent / "imagenes" / "logo-cds.png"


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

_MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun",
                 "jul", "ago", "sep", "oct", "nov", "dic")


def _dia_corto(dia) -> str:
    """Un día → «5 oct» (como la maqueta de la página de proyectos)."""
    return f"{dia.day} {_MESES_CORTOS[dia.month - 1]}" if dia else "sin fecha"


def _dia_largo(dia) -> str:
    """Un día → «30 oct 2026» (las fechas de un proyecto, que sí necesitan el año)."""
    return f"{dia.day} {_MESES_CORTOS[dia.month - 1]} {dia.year}" if dia else "sin fecha"


def _hace_dias(n) -> str:
    """Días enteros → «hoy», «ayer» o «hace N días»."""
    if n is None:
        return ""
    return "hoy" if n <= 0 else "ayer" if n == 1 else f"hace {n} días"


def _iniciales(nombre) -> str:
    """«Ana Pérez» → «AP». Las dos primeras palabras; sin nombre, «?»."""
    partes = str(nombre or "").split()[:2]
    return "".join(p[0] for p in partes).upper() or "?"


plantillas.env.filters["dia_corto"] = _dia_corto
plantillas.env.filters["dia_largo"] = _dia_largo
# LA CARPETA DEL PROYECTO (parte 5, 8-oct-2026): la plantilla NO decide si la carpeta es un enlace ni
# recibe esa decisión ya tomada en el modelo: llama a este filtro con el texto guardado, y solo lo que
# devuelve (la dirección `http(s)` completa) llega a un `href`. Es `db.enlace_de_carpeta` y ninguna
# otra función.
plantillas.env.filters["enlace_de_carpeta"] = db.enlace_de_carpeta
plantillas.env.filters["hace_dias"] = _hace_dias
plantillas.env.filters["iniciales"] = _iniciales
# El buscador de la página de proyectos filtra EN VIVO en el navegador (1-oct-2026,
# como la maqueta): cada proyecto de la lista lleva su nombre y su cliente ya
# sin tildes ni mayúsculas (`data-n`, `data-c`), con la MISMA función con la que
# filtra el servidor (`_sin_tildes`), y el guion solo busca el texto en ellos.
plantillas.env.filters["sin_tildes"] = lambda t: _sin_tildes(t)


def _sesion(request: Request) -> int | None:
    return auth.validar(request.cookies.get(COOKIE))


def _fuera(request: Request) -> HTMLResponse:
    return plantillas.TemplateResponse(
        request, "entrar.html", {"chat": config.CHAT_ID_DUENO}, status_code=401)


def _poner_sesion_de_la_casa(r, chat: int) -> None:
    """LA sesión de la casa (`lucy_panel`): la misma vida (`auth.VIDA_SESION`) y la
    misma forma de cookie para quien entra con el enlace de Telegram (`/entrar`) y
    para quien entra desde la App con nivel `total` (`/entrar-cds`). Es UNA función
    a propósito, y una prueba exige que las dos puertas den cookies indistinguibles
    (Tiziano, 5-oct-2026: «igual que ahora»)."""
    r.set_cookie(COOKIE, auth.crear_token(chat, auth.VIDA_SESION),
                 max_age=auth.VIDA_SESION, httponly=True, samesite="lax",
                 secure=True)


@app.get("/entrar", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_ENTRADA)
async def entrar(request: Request, t: Navegacion[str] = ""):
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
    _poner_sesion_de_la_casa(r, chat)
    return r


# ── La puerta desde la App de registro (`/entrar-cds`) ────────────────────────
# Diseño aprobado por Tiziano el 1-oct-2026. La App le da a la persona un boleto
# de un solo uso (un código al azar de 256 bits, 60 s de vida) y la manda acá con
# `?c=<boleto>`. Lucy NO verifica una firma: le pregunta a la App, de servidor a
# servidor, «¿este boleto es bueno y de quién es?», y la App lo rompe al contestar.
#
# EL CONTRATO DEL OTRO LADO (lo fija `tests/test_pase_proyectos.py` de la App y
# lo implementa su `canjear_pase`): `POST <App>/api/pase/canjear` con
# `{"codigo": ...}` contesta 200 `{"nivel", "chat", "tecnico_id"}`, o 404
# `{"error": "no"}` por cualquier motivo sin decir cuál. `nivel` es `total` o
# `ver`; `chat` es texto con el número de Telegram de la ficha, o vacío.
#
# TRES REGLAS QUE ESTA PUERTA CUMPLE Y UNA PRUEBA POR CADA UNA:
#   · la dirección de la App sale de `config.REGISTRO_URL` y de nada más;
#   · un código que no tiene la forma de un boleto no llega a la App;
#   · si el canje falla de cualquier manera, no se pone ninguna cookie.
TOPE_CANJE_S = 5.0
# `secrets.token_urlsafe(32)`: 43 caracteres del alfabeto URL-seguro.
_FORMA_BOLETO = re.compile(r"[A-Za-z0-9_-]{43}")
_LARGO_MAX_RESPUESTA = 4096


def _chat_del_canje(crudo) -> int | None:
    """El número de chat de lo que contestó la App (texto de dígitos), o None."""
    if isinstance(crudo, bool) or not isinstance(crudo, (str, int)):
        return None
    texto = str(crudo).strip()
    return int(texto) if texto.isascii() and texto.isdigit() and len(texto) <= 18 else None


async def _canjear_boleto(codigo: str) -> tuple[str, int | None] | None:
    """`(nivel, chat)` si la App dice que el boleto es bueno; None si no sirve
    por cualquier motivo (sin dirección, App caída, tiempo, 404, respuesta
    rara). No devuelve la razón: nada de lo que pasó se le cuenta a quien llegó."""
    base = config.REGISTRO_URL
    if not base.startswith(("http://", "https://")):
        log.warning("/entrar-cds: REGISTRO_URL sin configurar")
        return None
    try:
        async with httpx.AsyncClient(timeout=TOPE_CANJE_S,
                                     follow_redirects=False) as cliente:
            r = await asyncio.wait_for(
                cliente.post(f"{base}/api/pase/canjear", json={"codigo": codigo}),
                timeout=TOPE_CANJE_S)
    except Exception as e:                                    # noqa: BLE001
        log.warning("/entrar-cds: el canje no se pudo hacer (%s)", type(e).__name__)
        return None
    if r.status_code != 200 or len(r.content) > _LARGO_MAX_RESPUESTA:
        log.info("/entrar-cds: la App no dio el boleto por bueno (HTTP %s)", r.status_code)
        return None
    try:
        datos = r.json()
    except ValueError:
        log.warning("/entrar-cds: la App contestó algo que no es JSON")
        return None
    nivel = datos.get("nivel") if isinstance(datos, dict) else None
    if nivel not in (auth.NIVEL_TOTAL, auth.NIVEL_VER):
        log.warning("/entrar-cds: la App contestó un nivel que no conozco")
        return None
    return nivel, _chat_del_canje(datos.get("chat"))


@app.get("/entrar-cds", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_ENTRADA)
async def entrar_cds(request: Request, c: Navegacion[str] = ""):
    """El boleto de la App se cambia por una cookie y se va a Proyectos. Ver el
    bloque de arriba."""
    def _no():
        return plantillas.TemplateResponse(
            request, "entrar.html",
            {"chat": config.CHAT_ID_DUENO, "desde_la_app": True}, status_code=401)

    if not _FORMA_BOLETO.fullmatch(c):
        return _no()
    canje = await _canjear_boleto(c)
    if canje is None:
        return _no()
    nivel, chat = canje
    sesion = auth.sesion_para(nivel, chat)
    if nivel == auth.NIVEL_TOTAL and sesion != auth.SESION_CASA:
        log.warning("/entrar-cds: la App dio acceso total pero Lucy no conoce a "
                    "esa persona: entra solo a ver")
    log.info("/entrar-cds: entra con la sesión %s", sesion)
    r = RedirectResponse("/proyectos", status_code=303)
    if sesion == auth.SESION_CASA:
        _poner_sesion_de_la_casa(r, chat)         # la misma que da `/entrar`
    else:
        # Solo ver: sin `max_age`, la cookie muere al cerrar el navegador (como la
        # sesión de la App) y la vida de 12 h va escrita dentro del token.
        r.set_cookie(COOKIE_VER, auth.crear_token_ver(auth.VIDA_SESION_CDS),
                     httponly=True, samesite="lax", secure=True)
    return r


@app.get("/", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
async def resumen(request: Request, mes: Navegacion[str] = ""):
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
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cola(request: Request, guardados: Aviso[int] = 0):
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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


def _volver_sin_avisos(request: Request) -> str:
    """La dirección de esta pantalla para el campo oculto `volver`, sin los avisos de ESTA visita (un
    «Guardado 1» no se hereda a la próxima acción). Si limpiarla falla, es solo la ruta: la página no se cae."""
    try:
        q = avisos_mod.consulta_sin_avisos(request.app, request.url.path, list(request.query_params.multi_items()))
        return str(request.url.path) + ("?" + urlencode(q) if q else "")
    except Exception:
        log.warning("Panel: no se pudo armar el volver de %s", request.url.path, exc_info=True)
        return str(request.url.path)


@app.get("/movimientos", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
async def movimientos(request: Request, desde: Navegacion[str] = "",
                      hasta: Navegacion[str] = "", tipo: Navegacion[str] = "",
                      categoria: Navegacion[str] = "", banco: Navegacion[str] = "",
                      codigo: Navegacion[str] = "", guardados: Aviso[int] = 0,
                      borrado: Aviso[str] = "", efectivo: Aviso[str] = "",
                      error: Aviso[str] = ""):
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
         # sin los avisos de ESTA visita: un «Guardado 1» no se hereda a la próxima acción
         "volver": _volver_sin_avisos(request)})


@app.post("/efectivo")
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
async def papelera(request: Request, restaurado: Aviso[int] = 0, hecho: Aviso[str] = "",
                   id: Aviso[int] = 0, error: Aviso[str] = ""):
    """Lo borrado, con los días que le quedan.

    Existe para que "borrar" no dé miedo: sale de las listas al instante y se
    puede traer de vuelta durante 30 días. Lo que no se ve no se recupera, así
    que la papelera se muestra con el plazo delante.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    lo_borrado = await db.papelera_de_proyectos_y_tareas()
    # Los avisos de restaurar dicen un ESTADO comprobado en la base al pintar, no la
    # acción (una dirección escrita a mano no puede forzarlos).
    de_vuelta = None
    if hecho == "proyecto_restaurado" and id:
        a = await db.aviso_de_proyecto_restaurado(id)
        de_vuelta = {"tipo": "proyecto", **a} if a else None
    elif hecho == "tarea_restaurada" and id:
        t = await db.aviso_de_tarea(id, borrada=False)
        de_vuelta = {"tipo": "tarea", "nombre": t} if t is not None else None
    elif hecho == "nota_restaurada" and id:
        n = await db.aviso_de_nota(id, borrada=False)
        de_vuelta = {"tipo": "nota", **n} if n is not None else None
    return plantillas.TemplateResponse(
        request, "papelera.html",
        {"movs": await db.papelera(), "dias": db.DIAS_EN_PAPELERA,
         "restaurado": restaurado, "proyectos_borrados": lo_borrado["proyectos"],
         "tareas_borradas": lo_borrado["tareas"], "notas_borradas": lo_borrado["notas"], "error": error, "de_vuelta": de_vuelta})


@app.post("/papelera/restaurar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def restaurar_proyecto_o_tarea(request: Request):
    """Restaurar un proyecto, una tarea o una nota desde la Papelera. NO es otra forma de
    des-borrar: `crud.deshacer_borrado` busca la huella `borrar` de esa fila y la
    pasa por `crud.deshacer`, lo mismo que hace Telegram con «deshaz». Las reglas
    (nombre repetido, proyecto en la papelera o cerrado) son las de `deshacer`."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    tabla = str(formulario.get("tabla", ""))
    crudo = str(formulario.get("id", ""))
    if tabla not in ("proyectos", "tareas", "notas") or not re.fullmatch(r"[0-9]{1,9}", crudo):
        return RedirectResponse("/papelera?error=restaurar_no_esta", status_code=303)
    rid = int(crudo)
    try:
        await crud.deshacer_borrado(tabla, rid)
    except crud.NadaQueRestaurar:
        return RedirectResponse("/papelera?error=restaurar_no_esta", status_code=303)
    except ValueError as e:
        log.warning("Papelera: no se restauró %s #%s: %s", tabla, rid, e)
        return RedirectResponse("/papelera?error=restaurar_no_se_pudo", status_code=303)
    hecho = {"proyectos": "proyecto_restaurado", "tareas": "tarea_restaurada", "notas": "nota_restaurada"}[tabla]
    return RedirectResponse(f"/papelera?hecho={hecho}&id={rid}", status_code=303)


@app.get("/tareas", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
async def tareas(request: Request, guardadas: Aviso[int] = 0, creada: Aviso[int] = 0,
                 asignadas: Aviso[int] = 0, movidas: Aviso[int] = 0,
                 derivadas: Aviso[int] = 0, derivadas_ids: Aviso[str] = "",
                 responsable: Navegacion[str] = "", sin_cerrar: Aviso[str] = ""):
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
    # EL FILTRO POR RESPONSABLE (tarea 145): se aplica DESPUÉS de repartir y de
    # descartar el historial, sobre las filas que se iban a pintar, y no toca
    # SQL. Sin `?responsable=` es exactamente la pantalla de siempre.
    tipo, chat_f, clave_f, aviso_filtro = _filtro_pedido(responsable)
    todas_las_filas = [f for g in grupos for f in g["filas"]]
    botones = _botones_de_responsable(todas_las_filas, clave_f)
    grupos = [dict(g, filas=[f for f in g["filas"]
                             if _coincide(f.get("responsable_chat_id"),
                                          tipo, chat_f)])
              for g in grupos]
    grupos = [g for g in grupos if g["filas"]]
    mostradas = sum(len(g["filas"]) for g in grupos)
    etiqueta_f = {"": "Todas", RESP_SIN: "Sin responsable",
                  RESP_OTROS: "Otros"}.get(clave_f, clave_f)
    areas = await db.areas()
    return plantillas.TemplateResponse(
        request, "tareas.html",
        {"grupos": grupos, "hay_mas": datos["hay_mas"],
         # Lo que dice la URL NO se afirma sin mirar la base: solo los ids que
         # existen, siguen pendientes y están en un proyecto cerrado. Solo
         # dígitos ASCII (`\d` aceptaría otros alfabetos).
         "sin_cerrar": await db.tareas_sin_cerrar_por_proyecto_cerrado(
             [int(i) for i in sin_cerrar.split(",")]
             if re.fullmatch(r"[0-9]{1,9}(,[0-9]{1,9})*", sin_cerrar) else []),
         "botones": botones, "filtro": clave_f, "filtro_etiqueta": etiqueta_f,
         "filtro_aviso": aviso_filtro, "filtro_pedido": (responsable or "").strip(),
         "total_visibles": len(todas_las_filas), "mostradas": mostradas,
         "guardadas": guardadas, "asignadas": asignadas, "movidas": movidas,
         "derivadas": derivadas, "derivadas_ids": derivadas_ids,
         "max_derivadas": MAX_DERIVADAS, "largo_titulo": LARGO_TITULO,
         "tope": db.TOPE_TAREAS, "hecha": db.ESTADO_HECHA, "creada": creada,
         # La fecha solo se puede mover en las PENDIENTES, y la regla vive en
         # `db.mover_vence`. Se le pasa el mismo valor a la plantilla para que
         # la pantalla no ofrezca el campo donde la escritura lo rechazaría.
         "pendiente": db.ESTADO_PENDIENTE, "piso_fecha": PISO_FECHA.isoformat(),
         "personas": config.personas_del_panel(),
         "opciones_responsable": config.opciones_de_responsable(),
         "asignables": [c for c, _ in config.personas_del_panel()]
                       + [config.CHAT_ID_CODE],
         "nombres": config.nombres_con_code(),
         # «Code» (26-sep-2026, diseño «Code como responsable», §2): NO se
         # mete en `personas` -- ese desplegable recorre
         # `config.personas_del_panel()`, que significa específicamente
         # «puede entrar al panel», y Code nunca entra (no tiene chat de
         # Telegram). Se ofrece como una opción FIJA aparte, en la plantilla.
         "chat_id_code": config.CHAT_ID_CODE,
         "nombre_code": config.NOMBRE_CODE,
         "sin_nombre": config.chats_sin_nombre(),
         "mal_escritos": config.NOMBRES_MAL_ESCRITOS,
         # Para el <select> de área del renglón «¿sale algo nuevo de ésta?»
         # (tarea derivada, 25-sep-2026) -- las mismas opciones que ofrece
         # `/tareas/nueva`, y NO se ofrece si la fila tiene proyecto (P3 del
         # diseño: el área de la hija sale del proyecto, no se elige).
         "areas": areas,
         # {clave: color}, para pintar la etiqueta de cada fila sin que la
         # plantilla tenga que adivinar un color por su cuenta.
         "colores_area": {a["clave"]: a["color"] for a in areas}})


@app.get("/tareas/historial", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
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


def _sin_tildes(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or ""))
                   if not unicodedata.combining(c)).casefold()


def _filtrar_por_busqueda(modelo: dict, q: str) -> dict:
    """La lista de la izquierda con solo lo que casa con `q` (el nombre del
    proyecto o el de su cliente, sin tildes ni mayúsculas, como la maqueta).
    Sin `q` no se toca nada. Con `q` un grupo sin coincidencias desaparece y
    los apartados de «Tareas sin proyecto» no se ofrecen: no son proyectos ni
    clientes."""
    clave = _sin_tildes(q.strip())
    if not clave:
        return modelo

    def casa(m):
        return clave in _sin_tildes(m["nombre"]) or clave in _sin_tildes(m["cliente"] or "")

    def recortar(g):
        return {**g, "abiertos": [m for m in g["abiertos"] if casa(m)],
                "cerrados": [m for m in g["cerrados"] if casa(m)], "sueltas": None}

    grupos = [recortar(g) for g in modelo["grupos"]]
    sin = recortar(modelo["sin_grupo"])
    return {**modelo,
            "grupos": [g for g in grupos if g["abiertos"] or g["cerrados"]],
            "sin_grupo": sin}


def _vista_de_proyectos(modelo: dict, visibles: dict, *, p: int, g: str,
                        sin_grupo: int, tarea_creada: int,
                        buscando: bool = False, nuevo: str = "") -> dict:
    """Qué se enseña a la derecha: un proyecto, las tareas sueltas de un grupo
    o las de «Sin grupo». `{"tipo", ...}`; `tipo` es None si no hay nada que
    enseñar. Cuando la URL no dice (o dice algo que ya no existe) se enseña el
    primer proyecto de la lista; sin proyectos, lo primero que haya suelto."""
    proyectos = modelo["proyectos"]
    if nuevo and not buscando:
        grupo = next((x for x in modelo["grupos"] if x["clave"] == nuevo), None)
        if grupo is not None:
            return {"tipo": "nuevo", "grupo": grupo}
    if not p and tarea_creada:
        p = next((m["id"] for m in proyectos.values()
                  if any(t["id"] == tarea_creada for t in m["pendientes"] + m["otras"])), 0)
    if p in proyectos:
        m = proyectos[p]
        grupo = next((x for x in modelo["grupos"] if x["clave"] == m["area"]), None)
        return {"tipo": "proyecto", "proyecto": m, "grupo": grupo}
    if g:
        grupo = next((x for x in modelo["grupos"] if x["clave"] == g), None)
        if grupo is not None and grupo["sueltas"]["n_total"]:
            return {"tipo": "grupo", "grupo": grupo}
    if sin_grupo and modelo["sin_grupo"]["sueltas"]["n_total"]:
        return {"tipo": "sin_grupo", "grupo": modelo["sin_grupo"]}
    for x in [*visibles["grupos"], visibles["sin_grupo"]]:
        for m in [*x["abiertos"], *x["cerrados"]]:
            return {"tipo": "proyecto", "proyecto": proyectos[m["id"]],
                    "grupo": next((y for y in modelo["grupos"] if y["clave"] == m["area"]), None)}
    if not buscando:
        for x in [*modelo["grupos"], modelo["sin_grupo"]]:
            if x["sueltas"]["n_total"]:
                return {"tipo": "sin_grupo" if x["clave"] is None else "grupo", "grupo": x}
    return {"tipo": None}


@app.get("/proyectos", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_VER)
async def proyectos(request: Request, area_guardada: AvisoQueElige[int] = 0,
                    creado: AvisoQueElige[int] = 0, error: Aviso[str] = "",
                    nombre_guardado: AvisoQueElige[int] = 0,
                    tarea_creada: Aviso[int] = 0, sala_no: Aviso[int] = 0,
                    p: Navegacion[int] = 0, g: Navegacion[str] = "",
                    sin_grupo: Navegacion[int] = 0, q: Navegacion[str] = "",
                    hecho: Aviso[str] = "", nuevo: Navegacion[str] = "",
                    confirmar: Navegacion[str] = "", editar: Navegacion[str] = "",
                    t: Navegacion[int] = 0, editar_tarea: Navegacion[int] = 0,
                    confirmar_borrar: Navegacion[int] = 0,
                    editar_comentario: Navegacion[int] = 0,
                    derivar: Navegacion[int] = 0, derivadas: Aviso[str] = "",
                    pq: Navegacion[str] = "", pdonde: Navegacion[str] = "",
                    nuevo_grupo: Navegacion[int] = 0, quitar_grupo: Navegacion[str] = "",
                    grupo: Aviso[str] = "", borrar_proyecto: Navegacion[int] = 0,
                    borrado: Aviso[int] = 0, borrada: Aviso[int] = 0,
                    filtro: Navegacion[str] = "", quien: Navegacion[str] = "",
                    editar_nota: Navegacion[int] = 0, borrar_nota: Navegacion[int] = 0):
    """La página de proyectos (Lucy 1.0): los grupos y sus proyectos a la
    izquierda; a la derecha UN proyecto (`?p=`), las tareas sueltas de un grupo
    (`?g=`), las de «Sin grupo» (`?sin_grupo=1`) o el formulario de un proyecto
    nuevo (`?nuevo=<grupo>`). `?q=` filtra la lista por el nombre del proyecto
    o del cliente.

    ESTA RUTA SOLO LEE. Las escrituras son POST aparte, cada una un
    `<form method="post">` de la plantilla: las del proyecto (`/proyectos/nuevo`,
    `/{pid}/nombre`, `/{pid}/area`, `/{pid}/responsable`, `/{pid}/estado`) y las
    de la tarea (`/{pid}/tareas`, y `/proyectos/tarea/{tid}/...`: hecha,
    reabrir, titulo, borrar, responsable, comentar y editar un comentario). Los
    parámetros `confirmar=cerrar`, `editar=nombre`, `t` (el detalle abierto de
    una tarea), `editar_tarea`, `confirmar_borrar`, `editar_comentario` y
    `derivar` (los renglones de «¿sale una tarea nueva de ésta?» abiertos en esa
    tarea) solo hacen que el SERVIDOR dibuje la confirmación o el formulario,
    para que todo funcione también sin JavaScript. `hecho`, `error` y
    `derivadas` (los ids de las tareas que salieron de la que se cerró) son los
    avisos con los que vuelven los POST, junto con `p` (el proyecto del que
    hablan). `pq` y `pdonde` (`cliente`, `proyecto` o `tarea-<id>`) son la
    búsqueda de una persona de Noco SIN JavaScript: el servidor pide a Noco las
    coincidencias (solo LEE) y las dibuja como botones que envían el formulario
    de ese sitio; con JavaScript las pide el navegador a `/personas/buscar`.
    """
    de_la_casa = auth.puede_entrar(_sesion(request))
    solo_ver = not de_la_casa
    if solo_ver and not auth.validar_ver(request.cookies.get(COOKIE_VER)):
        return _fuera(request)
    if solo_ver:
        # SOLO VER (entrada desde la App con nivel `ver`, o sin conocer a la
        # persona): todo lo que haría dibujar un formulario, una confirmación o
        # un aviso de «se guardó» se apaga ANTES de armar la página, para que ni
        # una dirección escrita a mano lo muestre. La plantilla no dibuja nada
        # que cambie algo con `solo_ver`, y las rutas que escriben le dan 401 a
        # esta sesión: esto es lo que se ve, aquello es lo que se impide.
        area_guardada = creado = nombre_guardado = tarea_creada = sala_no = 0
        editar_tarea = confirmar_borrar = editar_comentario = derivar = 0
        error = hecho = nuevo = confirmar = editar = derivadas = pq = pdonde = ""
        nuevo_grupo = 0
        quitar_grupo = grupo = ""
        borrar_proyecto = borrado = borrada = 0
        editar_nota = borrar_nota = 0
    modelo = await db.pagina_de_proyectos()
    visibles = _filtrar_por_busqueda(modelo, q)
    elegido = p or nombre_guardado or area_guardada or creado
    vista = _vista_de_proyectos(modelo, visibles, p=elegido, g=g,
                                sin_grupo=sin_grupo, tarea_creada=tarea_creada,
                                buscando=bool(q.strip()), nuevo=nuevo)
    # Los enlaces de la barra salen del menú de verdad (`web/menu.py`). Un menú
    # ilegible NO se traga acá: el único sitio que lo hace es el armado del
    # prompt, y `base.html`, que es ese menú, también tumba las demás pantallas.
    pantallas = _menu.pantallas()
    busqueda = await _buscar_personas_para_la_pagina(pq, pdonde)
    # GRUPOS (agregar y quitar): lo que se dibuja sale de la base. La cuenta de lo
    # que tiene un grupo se mide AL PINTAR (solo si se pidió la confirmación). Los
    # avisos de `hecho=grupo_creado|grupo_quitado` NO afirman la acción (una
    # dirección escrita a mano no puede probarla): dicen el ESTADO, que aquí se
    # comprueba: «está al final de la lista» (solo si es el último de la base) y
    # «ya no está en la lista» (solo si no está).
    todos_los_grupos = [a["clave"] for a in await db.areas()]
    claves_de_grupos = set(todos_los_grupos)
    contenido_quitar = await db.contenido_para_borrar_grupo(quitar_grupo) if quitar_grupo else None
    # BORRAR (7-oct-2026): lo que dice cada aviso sale de la base al pintar. La
    # pregunta de «¿borrar este proyecto?» lleva las cuentas de sus tareas medidas
    # ahora; los avisos de «borrado» solo salen si es verdad (el proyecto está en la
    # papelera, el grupo ya no está, la tarea está borrada).
    contenido_borrar_proyecto = await db.contenido_de_proyecto(borrar_proyecto) if borrar_proyecto else None
    aviso_proyecto_borrado = (await db.aviso_de_proyecto_borrado(borrado)
                              if hecho == "proyecto_borrado" and borrado else None)
    aviso_grupo_borrado = (await db.aviso_de_grupo_borrado(grupo)
                           if hecho == "grupo_borrado" and grupo else None)
    titulo_tarea_borrada = (await db.aviso_de_tarea(borrada, borrada=True)
                            if hecho == "tarea_borrada" and borrada else None)
    # «Nota borrada. Está en la Papelera»: solo si la nota de verdad está borrada (estado, no la dirección).
    nota_borrada_en_papelera = (await db.aviso_de_nota(borrada, borrada=True)
                                is not None if hecho == "nota_borrada" and borrada else False)
    # LOS FILTROS DE LAS TAREAS (parte 2, 8-oct-2026): solo en la vista de un proyecto. `?filtro=` y
    # `?quien=` se normalizan en `_filtro_vigente` (una sola puerta) y la lista sale de
    # `db.tareas_con_filtro`; cualquier otra vista los ignora.
    tareas_p, sufijo_filtro, personas_filtro, mi_nombre = None, "", [], None
    if vista["tipo"] == "proyecto":
        m = vista["proyecto"]
        personas_filtro = sorted({f["responsable"] for f in m["pendientes"] + m["otras"] if f["responsable"]})
        mi_nombre = None if solo_ver else config.nombres_con_code().get(_sesion(request))
        filtro, quien = _filtro_vigente(filtro, quien, personas=personas_filtro, mi_nombre=mi_nombre,
                                        solo_ver=solo_ver)
        tareas_p = db.tareas_con_filtro(m, filtro, quien, mi_nombre)
        sufijo_filtro = _sufijo_de_filtro(filtro, quien)
    else:
        filtro = quien = ""
    return plantillas.TemplateResponse(
        request, "proyectos.html",
        {"solo_ver": solo_ver,
         "filtro": filtro, "quien": quien, "tareas_p": tareas_p, "personas_filtro": personas_filtro,
         "ofrece_mias": bool(mi_nombre), "sufijo_filtro": sufijo_filtro,
         "consulta_filtro": ("?" + sufijo_filtro[1:]) if sufijo_filtro else "",
         "nuevo_grupo": nuevo_grupo, "quitar_grupo": quitar_grupo, "grupo": grupo,
         "grupo_existe": grupo in claves_de_grupos,
         "grupo_es_el_ultimo": bool(todos_los_grupos) and grupo == todos_los_grupos[-1],
         "contenido_quitar": contenido_quitar,
         "claves_de_contenido_de_grupo": db.CLAVES_DE_CONTENIDO_DE_GRUPO,
         "claves_de_contenido_de_proyecto": db.CLAVES_DE_CONTENIDO_DE_PROYECTO,
         "contenido_borrar_proyecto": contenido_borrar_proyecto,
         "aviso_proyecto_borrado": aviso_proyecto_borrado,
         "aviso_grupo_borrado": aviso_grupo_borrado,
         "titulo_tarea_borrada": titulo_tarea_borrada,
         "nota_borrada_en_papelera": nota_borrada_en_papelera,
         "largo_grupo": db.LARGO_NOMBRE_GRUPO,
         # `/#inicio` (exacto, en el hash): la App lo lee y abre su página de inicio en vez de
         # la última sesión; una App que no lo conozca lo ignora. Contrato: Levantamientos
         # INFO/registro/la-pagina-de-inicio-y-como-se-llega.md
         "volver_a_la_app": (config.REGISTRO_URL + "/#inicio") if config.REGISTRO_URL.startswith(
             ("http://", "https://")) else "",
         "busqueda": busqueda, "largo_rol": db.LARGO_ROL_PARTICIPANTE, "modelo": modelo, "visibles": visibles, "vista": vista, "q": q,
         "pantallas": pantallas, "areas": await db.areas(), "error": error,
         "area_guardada": area_guardada, "creado": creado,
         "nombre_guardado": nombre_guardado, "tarea_creada": tarea_creada,
         "sala_no": sala_no, "nombre_code": config.NOMBRE_CODE,
         "area_tecnica": db.AREA_TECNICA, "estado_cerrado": db.ESTADO_PROYECTO_CERRADO,
         "largo_nombre": db.LARGO_NOMBRE_PROYECTO,
         "largo_descripcion": db.LARGO_DESCRIPCION_PROYECTO,
         "largo_termina_cuando": db.LARGO_TERMINA_CUANDO,
         "largo_carpeta": db.LARGO_CARPETA_PROYECTO, "largo_nota": db.LARGO_NOTA_PROYECTO,
         "editar_nota": editar_nota, "borrar_nota": borrar_nota,
         "anio_menor": db.DIA_MAS_VIEJO_DE_PROYECTO.year, "anio_mayor": db.DIA_MAS_NUEVO_DE_PROYECTO.year,
         "dias_dormido": db.DIAS_DORMIDO, "hecho": hecho,
         "confirmar": confirmar, "editar": editar,
         # Qué tarea tiene el detalle abierto o un formulario dibujado por el
         # servidor, y la dirección base de lo que se está viendo (para que
         # cada enlace de la tarea vuelva al mismo sitio).
         "t_abierta": t, "editar_tarea": editar_tarea,
         "confirmar_borrar": confirmar_borrar,
         "editar_comentario": editar_comentario,
         # Los renglones de la tarea derivada: cuál los tiene abiertos
         # (`?derivar=`), cuántos se pintan y el piso de fecha, las MISMAS
         # piezas que usa /tareas (`MAX_DERIVADAS`, `PISO_FECHA`).
         "derivar": derivar, "max_derivadas": MAX_DERIVADAS,
         "piso_fecha": PISO_FECHA, "derivadas": derivadas,
         "vuelta": _direccion_de_la_vista(vista) + sufijo_filtro,
         "largo_comentario": db.LARGO_COMENTARIO,
         "largo_titulo": db.LARGO_TITULO_TAREA,
         # Solo los nombres, sin la opción «sin responsable»: no hay (Tiziano,
         # 1-oct-2026). Salen de `config.opciones_de_responsable()`, la misma
         # lista de los demás desplegables, y viajan por NOMBRE: el número de
         # chat no se escribe en la página.
         "nombres_responsable": [n for v, n in config.opciones_de_responsable() if v]})


async def _buscar_personas_para_la_pagina(pq: str, pdonde: str) -> dict | None:
    """La búsqueda de personas de Noco que se dibuja SIN JavaScript: `None` si no
    hay nada que buscar. Es solo LECTURA de Noco (`noco_lectura.buscar_personas`);
    si Noco no contesta se dice, y nunca se inventa una lista vacía que parezca
    «esa persona no está»."""
    texto = pq.strip()
    if not texto or not re.fullmatch(r"cliente|proyecto|tarea-\d+", pdonde):
        return None
    try:
        personas = await noco_lectura.buscar_personas(texto)
    except noco_lectura.NocoNoContesta as e:
        return {"donde": pdonde, "q": texto, "personas": [], "error": str(e)}
    return {"donde": pdonde, "q": texto, "personas": personas, "error": ""}


def _noco_id_de(formulario) -> tuple:
    """`(vale, noco_id)` del campo `noco_id` del formulario: vacío es «ninguno»
    (`None`, vale: es quitar), un entero es el Id, cualquier otra cosa no vale."""
    crudo = str(formulario.get("noco_id", "")).strip()
    if crudo == "":
        return True, None
    return (True, int(crudo)) if crudo.isdigit() else (False, None)


def _direccion_de_la_vista(vista: dict) -> str:
    """La dirección de lo que se está viendo (el proyecto, o las tareas sueltas
    de un grupo o de «Sin grupo»), sin avisos: a ella vuelven los enlaces de las
    tareas."""
    if vista["tipo"] == "proyecto":
        return f"/proyectos?p={vista['proyecto']['id']}"
    if vista["tipo"] == "grupo":
        return f"/proyectos?g={quote(vista['grupo']['clave'], safe='')}"
    if vista["tipo"] == "sin_grupo":
        return "/proyectos?sin_grupo=1"
    return "/proyectos?"


LARGO_QUIEN = 80          # lo más largo que se acepta de un nombre en `?quien=`


def _sufijo_de_filtro(filtro: str, quien: str) -> str:
    """`&filtro=…[&quien=…]` listo para pegar a una dirección de `/proyectos?p=N`, o "" si lo
    pedido no es un filtro. LA puerta del vocabulario en la dirección: solo `db.FILTROS_DE_TAREAS`;
    «persona» pide un nombre sin caracteres de control y de a lo más `LARGO_QUIEN`. El valor viaja
    codificado (`quote`): nunca cambia el destino, solo se anota en la consulta."""
    if filtro not in db.FILTROS_DE_TAREAS:
        return ""
    if filtro == "persona":
        if not quien or len(quien) > LARGO_QUIEN or not quien.isprintable():
            return ""
        return f"&filtro=persona&quien={quote(quien, safe='')}"
    return f"&filtro={filtro}"


# El filtro con el que se envió el formulario de una tarea (`/proyectos/tarea/N/...?filtro=…`): lo anota
# este middleware para que `_volver_a_la_tarea` lo devuelva en la dirección de vuelta. Va en la
# dirección de la acción y no en el cuerpo del formulario: el destino sigue saliendo de la base.
_FILTRO_DEL_POST = contextvars.ContextVar("filtro_del_post", default="")


class FiltroDeLosPostDeTarea:
    """Middleware ASGI: en un POST a `/proyectos/tarea/...` guarda `_sufijo_de_filtro(filtro, quien)`
    (de la consulta de la dirección) mientras dura el pedido."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"].startswith("/proyectos/tarea/"):
            consulta = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
            marca = _FILTRO_DEL_POST.set(_sufijo_de_filtro(consulta.get("filtro", ""), consulta.get("quien", "")))
            try:
                return await self.app(scope, receive, send)
            finally:
                _FILTRO_DEL_POST.reset(marca)
        return await self.app(scope, receive, send)


app.add_middleware(FiltroDeLosPostDeTarea)


def _filtro_vigente(filtro: str, quien: str, *, personas: list[str], mi_nombre: str | None,
                    solo_ver: bool) -> tuple[str, str]:
    """El filtro que de verdad se aplica a la lista de un proyecto, o ("", "") si no hay ninguno.
    Un filtro que nadie declaró no vale; «Las mías» solo vale en la sesión de la casa y con nombre
    conocido (la de solo ver no dice de quién es); «Por persona» solo vale con alguien que tiene
    tareas en ese proyecto. Todo lo que no vale se ignora y se enseña la lista entera."""
    if filtro not in db.FILTROS_DE_TAREAS:
        return "", ""
    if filtro == "mias":
        return ("mias", "") if (not solo_ver and mi_nombre) else ("", "")
    if filtro == "persona":
        return ("persona", quien) if quien in personas else ("", "")
    return filtro, ""


async def _volver_a_la_tarea(tid: int, **parametros) -> str:
    """Adónde vuelve un POST de tarea: al sitio donde ESTÁ la tarea (su proyecto,
    las sueltas de su grupo o «Sin grupo»), calculado en el servidor desde la
    base. NUNCA sale de un campo del formulario: no hay un destino elegible que
    defender de un `//evil.com`. Los `parametros` (`hecho=`, `error=`, `t=`...)
    van antes del ancla `#tarea-N`."""
    donde = await db.donde_esta_la_tarea(tid)
    if donde is None:
        return "/proyectos?error=tarea"
    if donde["proyecto_vivo"]:
        base = f"/proyectos?p={donde['proyecto_id']}{_FILTRO_DEL_POST.get()}"
    elif (donde["proyecto_id"] is None and donde["area"]
          and donde["area"] in {a["clave"] for a in await db.areas()}):
        base = f"/proyectos?g={quote(donde['area'], safe='')}"
    else:
        base = "/proyectos?sin_grupo=1"
    extra = "".join(f"&{k}={quote(str(v), safe='')}" for k, v in parametros.items())
    return f"{base}{extra}#tarea-{tid}"


@app.post("/proyectos/nuevo")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def crear_proyecto_nuevo(request: Request):
    """El formulario «+ Proyecto en X». Todo lo decide `db.crear_proyecto`
    (nombre, grupo y responsable); esta ruta solo traduce el formulario y el
    rechazo. El responsable llega por NOMBRE y se vuelve chat con la MISMA
    puerta de traducción que usan `crud.editar` y `deshacer`. Un rechazo vuelve
    al formulario con una CLAVE en la URL (nunca el nombre pedido)."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    nombre = str(formulario.get("nombre", ""))
    area = str(formulario.get("area", "")).strip()
    vuelta = f"/proyectos?nuevo={quote(area)}"
    # SOLO TRADUCE: el nombre elegido pasa a chat; si no se puede traducir, llega
    # `None` y la puerta (`db.crear_proyecto`) es la que lo rechaza.
    try:
        responsable = crud.PUERTAS["proyectos"]["responsable_chat_id"](
            str(formulario.get("responsable", "")))
    except ValueError:
        responsable = None
    try:
        nuevo = await db.crear_proyecto(nombre, area, responsable)
    except db.NombreDeProyectoNoVale as e:
        log.warning("Panel de proyectos: proyecto nuevo rechazado (%s)", e.clave)
        return RedirectResponse(f"{vuelta}&error=nombre_{e.clave}", status_code=303)
    except db.ProyectoNoSeCrea as e:
        log.warning("Panel de proyectos: proyecto nuevo rechazado (%s)", e.clave)
        return RedirectResponse(f"{vuelta}&error={e.clave}", status_code=303)
    # EL CLIENTE ES OPCIONAL (Tiziano, 1-oct-2026: «no todos los proyectos tienen
    # cliente»). Si la ventanita mandó uno, se pone DESPUÉS de crear el proyecto,
    # por la única puerta del cliente (`db.poner_cliente`, que vuelve a leer la
    # ficha de Noco): así el proyecto no depende de que Noco conteste, y si no
    # contesta el proyecto queda creado SIN cliente y el aviso lo dice.
    crudo = str(formulario.get("cliente", "")).strip()
    if crudo:
        try:
            if not crudo.isdigit():
                raise ValueError("cliente que no es un Id")
            await db.poner_cliente(nuevo["id"], int(crudo), leer_persona=noco_lectura.persona)
        except (ValueError, noco_lectura.NocoNoContesta) as e:
            log.warning("Panel de proyectos: el proyecto #%s se creó sin cliente: %s", nuevo["id"], e)
            return RedirectResponse(
                f"/proyectos?hecho=proyecto_nuevo&error=cliente_nuevo&p={nuevo['id']}", status_code=303)
    return RedirectResponse(
        f"/proyectos?hecho=proyecto_nuevo&p={nuevo['id']}", status_code=303)


@app.post("/proyectos/{pid}/cliente")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def poner_cliente_del_proyecto(request: Request, pid: int):
    """Poner, cambiar o quitar el cliente de un proyecto. TODO LO DECIDE
    `db.poner_cliente`: vuelve a pedirle la ficha a Noco (solo LEE) y guarda el
    nombre que Noco devuelve, nunca uno del formulario. `noco_id` vacío quita el
    cliente (es opcional). Esta ruta solo traduce y pide sesión."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    vale, noco_id = _noco_id_de(await request.form())
    if not vale:
        return RedirectResponse(f"/proyectos?error=cliente&p={pid}", status_code=303)
    try:
        cambio = await db.poner_cliente(pid, noco_id, leer_persona=noco_lectura.persona)
    except noco_lectura.NocoNoContesta as e:
        log.warning("Panel de proyectos: Noco no contestó al poner el cliente de #%s: %s", pid, e)
        return RedirectResponse(f"/proyectos?error=cliente_noco&p={pid}", status_code=303)
    except ValueError as e:
        log.warning("Panel de proyectos: cliente rechazado para #%s: %s", pid, e)
        return RedirectResponse(f"/proyectos?error=cliente&p={pid}", status_code=303)
    if not cambio:
        return RedirectResponse(f"/proyectos?error=cliente_igual&p={pid}", status_code=303)
    hecho = "cliente_quitado" if noco_id is None else "cliente"
    return RedirectResponse(f"/proyectos?hecho={hecho}&p={pid}", status_code=303)


def _error_de_persona(e: Exception) -> str:
    """La CLAVE del aviso con el que vuelve un rechazo de personas (nunca el texto
    escrito por la persona)."""
    if isinstance(e, noco_lectura.NocoNoContesta):
        return "persona_noco"
    if isinstance(e, db.ParticipanteNoVale):
        return f"persona_{e.clave}"
    return "persona_ficha"


@app.post("/proyectos/{pid}/personas")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def agregar_persona_al_proyecto(request: Request, pid: int):
    """Agregar una persona de Noco a un proyecto, con su «qué hace aquí». POR
    `db.agregar_participante`: quién escribe sale de la sesión, y la ficha se
    vuelve a leer de Noco. Agregar una persona NO es un movimiento del proyecto."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    vale, noco_id = _noco_id_de(formulario)
    if not vale or noco_id is None:
        # Sin persona escogida (el botón «Agregar» sin haber tocado un resultado) es
        # otro aviso que un Id que no vale.
        clave = "persona_ficha" if not vale else "persona_falta"
        return RedirectResponse(f"/proyectos?error={clave}&p={pid}", status_code=303)
    try:
        await db.agregar_participante(("proyecto", pid), noco_id, str(formulario.get("rol", "")),
                                      chat, leer_persona=noco_lectura.persona)
    except (db.ParticipanteNoVale, noco_lectura.NocoNoContesta, ValueError) as e:
        log.warning("Panel de proyectos: persona rechazada en el proyecto #%s: %s", pid, e)
        return RedirectResponse(f"/proyectos?error={_error_de_persona(e)}&p={pid}", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=persona&p={pid}", status_code=303)


@app.post("/proyectos/{pid}/personas/{xid}/quitar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def quitar_persona_del_proyecto(request: Request, pid: int, xid: int):
    """Quitar a una persona de un proyecto: `db.quitar_participante`, que exige
    que la persona sea DE ESE proyecto."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    try:
        await db.quitar_participante(xid, ("proyecto", pid), chat)
    except db.ParticipanteNoVale as e:
        log.warning("Panel de proyectos: persona #%s no se quitó del proyecto #%s: %s", xid, pid, e)
        return RedirectResponse(f"/proyectos?error={_error_de_persona(e)}&p={pid}", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=persona_quitada&p={pid}", status_code=303)


@app.post("/proyectos/tarea/{tid}/personas")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def agregar_persona_a_la_tarea(request: Request, tid: int):
    """Lo mismo, para una tarea (`db.agregar_participante` con `("tarea", id)`)."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    vale, noco_id = _noco_id_de(formulario)
    if not vale or noco_id is None:
        clave = "persona_ficha" if not vale else "persona_falta"
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error=clave, t=tid), status_code=303)
    try:
        await db.agregar_participante(("tarea", tid), noco_id, str(formulario.get("rol", "")),
                                      chat, leer_persona=noco_lectura.persona)
    except (db.ParticipanteNoVale, noco_lectura.NocoNoContesta, ValueError) as e:
        log.warning("Panel de proyectos: persona rechazada en la tarea #%s: %s", tid, e)
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error=_error_de_persona(e), t=tid), status_code=303)
    return RedirectResponse(await _volver_a_la_tarea(tid, t=tid, hecho="persona"), status_code=303)


@app.post("/proyectos/tarea/{tid}/personas/{xid}/quitar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def quitar_persona_de_la_tarea(request: Request, tid: int, xid: int):
    """Lo mismo, para una tarea (`db.quitar_participante` con `("tarea", id)`)."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    try:
        await db.quitar_participante(xid, ("tarea", tid), chat)
    except db.ParticipanteNoVale as e:
        log.warning("Panel de proyectos: persona #%s no se quitó de la tarea #%s: %s", xid, tid, e)
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error=_error_de_persona(e), t=tid), status_code=303)
    return RedirectResponse(await _volver_a_la_tarea(tid, t=tid, hecho="persona_quitada"), status_code=303)


@app.post("/proyectos/{pid}/responsable")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_responsable_de_proyecto(request: Request, pid: int):
    """Cambiar el responsable de un proyecto. POR LA MISMA PUERTA que Telegram:
    `crud.editar("proyectos", ...)`, que llama a
    `crud.PUERTAS["proyectos"]["responsable_chat_id"]` (la de las tareas)."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        despues, _log = await crud.editar(
            "proyectos", pid,
            {"responsable_chat_id": str(formulario.get("responsable", ""))},
            motivo="Responsable cambiado desde el panel", actor="panel")
    except ValueError as e:
        log.warning("Panel de proyectos: responsable rechazado para #%s: %s", pid, e)
        return RedirectResponse(f"/proyectos?error=responsable&p={pid}", status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=responsable&p={pid}", status_code=303)


@app.post("/proyectos/{pid}/estado")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_estado_de_proyecto(request: Request, pid: int):
    """Cerrar o reabrir un proyecto. POR LA MISMA PUERTA que Telegram:
    `crud.editar("proyectos", ...)` con `crud.PUERTAS["proyectos"]["estado"]`
    (vocabulario cerrado). La ruta no decide qué estado vale: lo que ofrece la
    plantilla es `cerrado` y `activo`, y lo que la puerta no deje no se guarda."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        despues, _log = await crud.editar(
            "proyectos", pid, {"estado": str(formulario.get("estado", ""))},
            motivo="Estado cambiado desde el panel", actor="panel")
    except ValueError as e:
        log.warning("Panel de proyectos: estado rechazado para #%s: %s", pid, e)
        return RedirectResponse(f"/proyectos?error=estado&p={pid}", status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    hecho = {db.ESTADO_PROYECTO_CERRADO: "cerrado", "activo": "reabierto"}.get(
        despues["estado"], "estado")
    return RedirectResponse(f"/proyectos?hecho={hecho}&p={pid}", status_code=303)


@app.post("/proyectos/grupos")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def crear_grupo_de_proyectos(request: Request):
    """«+ Nuevo grupo» (Tiziano, 6-oct-2026). Todo lo decide `db.crear_grupo`
    (nombre, repetido sin distinguir mayúsculas, tildes ni espacios, color y
    lugar al final): la ruta solo traduce el rechazo a una CLAVE en la URL, nunca
    el nombre pedido. La página NO afirma «creado» (una dirección puede escribirse
    a mano): dice que el grupo está al final de la lista, y solo si lo está."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        nuevo = await db.crear_grupo(str(formulario.get("nombre", "")))
    except db.GrupoNoVale as e:
        return RedirectResponse(f"/proyectos?nuevo_grupo=1&error=grupo_{e.clave}", status_code=303)
    return RedirectResponse(
        f"/proyectos?hecho=grupo_creado&grupo={quote(nuevo['clave'], safe='')}", status_code=303)


@app.post("/proyectos/grupos/quitar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def quitar_grupo_de_proyectos(request: Request):
    """Quitar un grupo VACÍO. La confirmación («¿Quitar el grupo X?») la dibuja
    el servidor con `?quitar_grupo=` (ver `proyectos`); esta ruta es lo que
    envía su botón. `db.quitar_grupo` decide: solo borra si nada apunta al grupo
    y nunca el fijo. Un grupo con cosas vuelve a la misma explicación, con las
    cuentas medidas al pintar. La página NO afirma «quitado»: dice que el grupo ya
    no está en la lista, y solo si es verdad."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    clave = str(formulario.get("clave", ""))
    try:
        await db.quitar_grupo(clave)
    except db.GrupoNoSeQuita as e:
        return RedirectResponse(
            f"/proyectos?error=grupo_{e.clave}&quitar_grupo={quote(clave, safe='')}", status_code=303)
    return RedirectResponse(
        f"/proyectos?hecho=grupo_quitado&grupo={quote(clave, safe='')}", status_code=303)


def _cuentas_del_formulario(formulario, claves) -> dict:
    """Las cuentas que la pregunta de borrar enseñó y que el «Sí» trae de vuelta, solo
    dígitos ASCII; lo que falte o no sea un número queda en `None` (y no coincide con
    nada: un «Sí» sin cuentas nunca borra)."""
    salida = {}
    for k in claves:
        crudo = str(formulario.get(k, ""))
        salida[k] = int(crudo) if re.fullmatch(r"[0-9]{1,9}", crudo) else None
    return salida


@app.post("/proyectos/grupos/borrar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def borrar_grupo_de_proyectos(request: Request):
    """Borrar un grupo CON lo que tenga (Tiziano, 7-oct-2026). La pregunta la dibuja el
    servidor (`?quitar_grupo=`) con las cuentas medidas al pintar, y este botón las
    trae de vuelta: `crud.borrar_grupo` las compara con lo medido AHORA y, si cambió
    algo, no borra y la página vuelve a preguntar. Todo lo decide `crud.borrar_grupo`;
    la ruta solo traduce el rechazo a una CLAVE en la URL. La página NO afirma
    «borrado»: dice un estado comprobado (el grupo ya no está, y lo que dice su huella)."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    clave = str(formulario.get("clave", ""))
    esperado = _cuentas_del_formulario(formulario, db.CLAVES_DE_CONTENIDO_DE_GRUPO)
    try:
        await crud.borrar_grupo(clave, esperado, actor="panel")
    except db.GrupoNoSeQuita as e:
        return RedirectResponse(
            f"/proyectos?error=grupo_{e.clave}&quitar_grupo={quote(clave, safe='')}", status_code=303)
    except crud.CambioAlBorrar:
        return RedirectResponse(
            f"/proyectos?error=grupo_cambio&quitar_grupo={quote(clave, safe='')}", status_code=303)
    return RedirectResponse(
        f"/proyectos?hecho=grupo_borrado&grupo={quote(clave, safe='')}", status_code=303)


@app.post("/proyectos/{pid}/borrar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def borrar_proyecto_desde_proyectos(request: Request, pid: int):
    """Borrar un proyecto, abierto o cerrado, con sus tareas (B2). POR LA MISMA PUERTA
    que Telegram: `crud.borrar("proyectos", ...)`, que lo manda a la papelera y se
    lleva sus tareas vivas. El «Sí» trae las cuentas de tareas de la pregunta: si lo
    medido ahora es otra cosa, no se borra y se vuelve a preguntar."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    esperado = _cuentas_del_formulario(formulario, db.CLAVES_DE_CONTENIDO_DE_PROYECTO)
    try:
        log_id = await crud.borrar(
            "proyectos", pid, "Proyecto borrado desde el panel", actor="panel", esperado=esperado)
    except crud.CambioAlBorrar:
        return RedirectResponse(
            f"/proyectos?p={pid}&borrar_proyecto={pid}&error=borrar_cambio", status_code=303)
    if log_id is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=proyecto_borrado&borrado={pid}", status_code=303)


@app.post("/proyectos/{pid}/tareas")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def agregar_tarea_al_proyecto(request: Request, pid: int):
    """Agregar una tarea DENTRO de un proyecto (título y fecha opcional). Nace
    con el responsable del proyecto (P10). Escribe por la MISMA puerta que el
    formulario de `/tareas/nueva`: `db.crear_tarea_desde_el_panel`, que a su vez
    vuelve a mirar que el proyecto admita tareas (un cerrado no las recibe). El
    título pasa por `db.titulo_de_tarea_que_vale`. Si el responsable del
    proyecto ya no puede serlo (salió del panel), la tarea nace sin responsable
    en vez de no poder crearse. Quién la anotó sale de la sesión."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    base = f"/proyectos?p={pid}"
    try:
        proyecto = await db.proyecto_para_tareas(pid)
    except db.ProyectoNoAdmiteTareas as e:
        clave = "proyecto_cerrado" if e.clave == "cerrado" else "proyecto"
        return RedirectResponse(f"{base}&error={clave}", status_code=303)
    try:
        titulo = db.titulo_de_tarea_que_vale(str(formulario.get("titulo", "")))
    except ValueError:
        return RedirectResponse(f"{base}&error=tarea_titulo", status_code=303)
    ok, vence_en = _vence_valido(str(formulario.get("vence", "")))
    if not ok:
        return RedirectResponse(f"{base}&error=tarea_fecha", status_code=303)
    responsable = await db.responsable_de_proyecto(pid)
    if responsable is not None and not config.puede_ser_responsable(responsable):
        responsable = None
    try:
        tid = await db.crear_tarea_desde_el_panel(
            chat, titulo, vence_en, None, responsable, proyecto_id=pid)
    except db.ProyectoNoAdmiteTareas as e:
        clave = "proyecto_cerrado" if e.clave == "cerrado" else "proyecto"
        return RedirectResponse(f"{base}&error={clave}", status_code=303)
    except ValueError as e:
        log.warning("Panel de proyectos: tarea rechazada por el escritor: %s", e)
        return RedirectResponse(f"{base}&error=tarea_rechazada", status_code=303)
    sala = ("&sala_no=1" if responsable == config.CHAT_ID_CODE
            and not db.sala_ve(responsable, proyecto["area"]) else "")
    return RedirectResponse(f"{base}&tarea_creada={tid}{sala}#tarea-{tid}", status_code=303)


@app.post("/proyectos/tarea/{tid}/hecha")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def marcar_tarea_hecha_desde_proyectos(request: Request, tid: int):
    """Marcar UNA tarea hecha desde la página de Proyectos — y, si la persona
    escribió la tarea que sale de ella, crearla en el MISMO gesto.

    POR LA MISMA PUERTA QUE `/tareas`: `db.cerrar_y_derivar`, que cierra la
    madre y crea las hijas en una sola transacción (Tiziano, 1-oct-2026: «Sí»
    a tener «crear la tarea que sigue» también en Proyectos). Antes esta ruta
    llamaba a `db.marcar_tarea_hecha`, que no deriva; el camino de cerrar una
    tarea es UNO para las dos pantallas, no dos que se parecen.

    Los renglones de la tarea nueva se leen y se validan con LAS MISMAS piezas
    que `/tareas` (`_derivadas_pedidas` y `_vence_con_hora_valido`), con el
    responsable por NOMBRE, que es como viaja en esta página. Un renglón que
    no vale NO cierra la madre (decisión D6 del diseño de la tarea derivada:
    «si la nueva no vale, la vieja tampoco se cierra»): se vuelve con los
    renglones abiertos y el aviso.

    Un proyecto cerrado tampoco recibe la derivada, y por la misma puerta
    (`db.ProyectoNoAdmiteTareas`, que `cerrar_y_derivar` levanta ANTES de
    cerrar nada): la madre se queda pendiente y se dice.
    """
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    claves_area_validas = {a["clave"] for a in await db.areas()}
    vale, derivadas = _derivadas_pedidas(
        formulario, tid, claves_area_validas, _responsable_por_nombre)
    if not vale:
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_derivada", derivar=tid), status_code=303)
    try:
        resultado = await db.cerrar_y_derivar(chat, tid, derivadas)
    except db.ProyectoNoAdmiteTareas as e:
        log.warning("Panel de proyectos: derivada rechazada, el proyecto no "
                    "admite tareas (%s)", e.clave)
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_derivada_cerrado", derivar=tid), status_code=303)
    except ValueError as e:
        # `cerrar_y_derivar` revalida el responsable contra
        # `config.puede_ser_responsable` antes de escribir nada: si dijera que
        # no, no entró ni la madre ni ninguna hija.
        log.warning("Panel de proyectos: tarea derivada rechazada: %s", e)
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_derivada", derivar=tid), status_code=303)
    if resultado is None:
        return RedirectResponse("/proyectos?error=tarea", status_code=303)
    cerrada, hijas = resultado
    if not cerrada and not hijas:
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_igual"), status_code=303)
    vuelta = {"hecho": "tarea_hecha"}
    if hijas:
        vuelta["derivadas"] = ",".join(str(i) for i in hijas)
    return RedirectResponse(await _volver_a_la_tarea(tid, **vuelta), status_code=303)


@app.post("/proyectos/tarea/{tid}/reabrir")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def reabrir_tarea_desde_proyectos(request: Request, tid: int):
    """Desmarcar una tarea hecha (P5): `db.reabrir_tarea`. Vuelve a pendiente con
    la misma fecha; si ya pasó, sale vencida."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    cambio = await db.reabrir_tarea(tid)
    return RedirectResponse(await _volver_a_la_tarea(
        tid, **({"hecho": "tarea_reabierta"} if cambio else {"error": "tarea_igual"})),
        status_code=303)


@app.post("/proyectos/tarea/{tid}/titulo")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_titulo_de_tarea(request: Request, tid: int):
    """Cambiar el título de una tarea (doble clic). POR LA MISMA PUERTA que
    Telegram: `crud.editar("tareas", ...)`, con `crud.PUERTAS["tareas"]["titulo"]`
    (no vacío, ni de más de `LARGO_TITULO`); la ruta no decide qué título vale."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        despues, _log = await crud.editar(
            "tareas", tid, {"titulo": str(formulario.get("titulo", ""))},
            motivo="Título cambiado desde el panel de proyectos", actor="panel")
    except ValueError as e:
        log.warning("Panel de proyectos: título rechazado para la tarea #%s: %s", tid, e)
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_titulo", editar_tarea=tid), status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=tarea", status_code=303)
    return RedirectResponse(await _volver_a_la_tarea(tid, hecho="tarea_titulo"), status_code=303)


@app.post("/proyectos/tarea/{tid}/borrar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def borrar_tarea_desde_proyectos(request: Request, tid: int):
    """La × de una tarea: soft-delete por `crud.borrar` con `actor='panel'` (va a
    la papelera, con huella, y se recupera con `deshacer` o desde la papelera)."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    donde = await _volver_a_la_tarea(tid, hecho="tarea_borrada", borrada=tid)
    log_id = await crud.borrar(
        "tareas", tid, "Tarea borrada desde el panel de proyectos", actor="panel")
    if log_id is None:
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error="tarea_igual"), status_code=303)
    return RedirectResponse(donde, status_code=303)


@app.post("/proyectos/tarea/{tid}/responsable")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_responsable_de_tarea_desde_proyectos(request: Request, tid: int):
    """El responsable de una tarea (en su detalle, P10). El nombre elegido pasa a
    chat con la puerta de traducción de `crud.PUERTAS` y escribe
    `db.asignar_responsable`, que decide si vale. «Sin responsable» (vacío)
    quita al responsable: para una tarea es un estado normal."""
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    try:
        chat = crud.PUERTAS["tareas"]["responsable_chat_id"](
            str(formulario.get("responsable", "")))
    except ValueError:
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_responsable", t=tid), status_code=303)
    if chat is not None and not config.puede_ser_responsable(chat):
        # La MISMA puerta que vuelve a preguntar `db.asignar_responsable`: la
        # ruta también la nombra (lo exige `tests/test_responsable.py`).
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_responsable", t=tid), status_code=303)
    cambio = await db.asignar_responsable(tid, chat)
    return RedirectResponse(await _volver_a_la_tarea(
        tid, t=tid, **({"hecho": "tarea_responsable"} if cambio else {"error": "tarea_igual"})),
        status_code=303)


@app.post("/proyectos/tarea/{tid}/comentar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def comentar_desde_proyectos(request: Request, tid: int):
    """Comentar una tarea: `db.comentar_tarea`, la de siempre. Quién escribe sale
    de la sesión, nunca de un campo del formulario."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    texto = db.texto_de_comentario(str(formulario.get("texto", "")))
    if texto is None:
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_comentario", t=tid), status_code=303)
    cid = await db.comentar_tarea(tid, chat, texto)
    if cid is None:
        return RedirectResponse(await _volver_a_la_tarea(tid, error="tarea_igual"), status_code=303)
    return RedirectResponse(await _volver_a_la_tarea(
        tid, t=tid, hecho="comentario"), status_code=303)


@app.post("/proyectos/tarea/{tid}/comentario/{cid}/editar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def editar_comentario_desde_proyectos(request: Request, tid: int, cid: int):
    """Editar un comentario (P6: cualquiera de los dos edita cualquiera; queda
    marcado «editado»). Lo decide `db.editar_comentario`, que exige que el
    comentario sea de ESA tarea."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    try:
        cambio = await db.editar_comentario(cid, tid, chat, str(formulario.get("texto", "")))
    except db.ComentarioNoVale:
        return RedirectResponse(await _volver_a_la_tarea(
            tid, error="tarea_comentario", t=tid, editar_comentario=cid), status_code=303)
    return RedirectResponse(await _volver_a_la_tarea(
        tid, t=tid, **({"hecho": "comentario_editado"} if cambio else {"error": "tarea_igual"})),
        status_code=303)


@app.post("/proyectos/tarea/{tid}/proyecto")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def meter_tarea_en_un_proyecto(request: Request, tid: int):
    """Meter en un proyecto una tarea que no tiene (o cuyo proyecto está en la
    papelera): el desplegable del detalle de las tareas sueltas.

    NO HAY UNA PUERTA NUEVA: escribe `crud.editar("tareas", tid, {"proyecto_id":
    N})`, la misma que usa Telegram para «pon esta tarea en el proyecto X». Ella
    decide que el proyecto exista, no esté en la papelera ni cerrado
    (`db.proyecto_admite_tareas`), limpia el grupo propio de la tarea (con
    proyecto, el grupo es el del proyecto, igual que al crear una tarea dentro
    de uno), deja la huella `editar` de `log_acciones` con `actor='panel'` y por
    eso `crud.deshacer` la revierte. Esta ruta solo traduce el formulario, mira
    que la tarea de verdad esté suelta y dice la verdad en el aviso.

    SOLO SUELTAS: una tarea que ya está en un proyecto vivo se rechaza
    (`tarea_con_proyecto`) y no escribe, también al segundo envío del mismo
    formulario. FRONTERA: esa mirada y la escritura son dos pasos; dos envíos
    exactamente a la vez podrían dejar dos huellas del mismo cambio (la tarea
    queda bien: en el proyecto pedido).

    El proyecto llega por su número, y NUNCA se confía en el destino de la
    vuelta: sale de `_volver_a_la_tarea`, desde la base."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    donde = await db.donde_esta_la_tarea(tid)
    if donde is None:
        return RedirectResponse("/proyectos?error=tarea", status_code=303)
    if donde["proyecto_vivo"]:
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error="tarea_con_proyecto", t=tid), status_code=303)
    formulario = await request.form()
    crudo = str(formulario.get("proyecto", "")).strip()
    if not crudo.isdigit():
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error="tarea_proyecto", t=tid), status_code=303)
    pid = int(crudo)
    try:
        proyecto = await db.proyecto_para_tareas(pid)
    except db.ProyectoNoAdmiteTareas as e:
        clave = "proyecto_cerrado" if e.clave == "cerrado" else "tarea_proyecto"
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error=clave, t=tid), status_code=303)
    try:
        despues, _log_id = await crud.editar(
            "tareas", tid, {"proyecto_id": pid},
            motivo="Tarea metida en un proyecto desde el panel de proyectos",
            actor="panel")
    except ValueError as e:
        log.warning("Panel de proyectos: tarea #%s no entró al proyecto #%s: %s", tid, pid, e)
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error="tarea_proyecto", t=tid), status_code=303)
    if despues is None or despues["proyecto_id"] != pid:
        return RedirectResponse(
            await _volver_a_la_tarea(tid, error="tarea_igual"), status_code=303)
    sala = {"sala_no": 1} if (despues["responsable_chat_id"] == config.CHAT_ID_CODE
                              and not db.sala_ve(config.CHAT_ID_CODE, proyecto["area"])) else {}
    return RedirectResponse(
        await _volver_a_la_tarea(tid, hecho="tarea_proyecto", **sala), status_code=303)


@app.get("/logo-cds.png")
@auth.puerta(auth.PUERTA_VER)
async def logo_cds(request: Request):
    """El logo de la barra de la página de proyectos, el mismo de la App. Va
    por una ruta propia: el panel no sirve archivos estáticos. La ven las dos
    sesiones (la de la casa y la de solo ver): es la imagen de la barra."""
    if not (auth.puede_entrar(_sesion(request))
            or auth.validar_ver(request.cookies.get(COOKIE_VER))):
        return _fuera(request)
    return FileResponse(LOGO_CDS, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/personas/buscar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def buscar_personas_en_noco(request: Request, q: Navegacion[str] = ""):
    """El buscador de personas de Noco, en JSON (Lucy 1.0, E3).

    Es la ÚNICA ruta del panel que contesta JSON, y existe para que el
    navegador pueda buscar sin recargar la página: el Noco de CDS tiene 733
    personas (medido por la sala el 1-oct-2026), así que elegir un cliente o
    agregar una persona a un proyecto es escribir dos letras y escoger de una
    lista corta, no un desplegable con todas.

    TODAVÍA NO LA USA NINGUNA PÁGINA. La pintan los encargos siguientes (el
    cliente del proyecto y las personas de proyectos y tareas); se construye
    ahora porque el diseño la pone acá y porque es la mitad que no se puede
    probar sin el lector de Noco.

    EXIGE SESIÓN, igual que todas las pantallas: no es una puerta de programa
    —para eso está `/api/code/*`, con su clave— sino una llamada del navegador
    de alguien que ya entró al panel. Sin cookie: 401 y ni una palabra a Noco.

    Y DICE CUANDO NO PUDO: si Noco no contesta (o falta configurarlo), la
    respuesta es un 503 con el motivo, nunca una lista vacía. Un buscador que
    devuelve «no hay nadie» cuando el que falló es el CRM es peor que un error:
    hace crear una ficha duplicada creyendo que la persona no estaba.
    """
    if not auth.puede_entrar(_sesion(request)):
        return JSONResponse({"error": "sin sesión"}, status_code=401)
    try:
        personas = await noco_lectura.buscar_personas(q)
    except noco_lectura.NocoNoContesta as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"personas": personas})


@app.post("/proyectos/{pid}/area")
@auth.puerta(auth.PUERTA_SIEMPRE)
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
        return RedirectResponse(f"/proyectos?error=area&p={pid}", status_code=303)
    if despues is None:
        return RedirectResponse(f"/proyectos?error=proyecto", status_code=303)
    return RedirectResponse(f"/proyectos?area_guardada={pid}&p={pid}", status_code=303)


@app.post("/proyectos/{pid}/nombre")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_nombre_de_proyecto(request: Request, pid: int):
    """Cambiar el NOMBRE de un proyecto (pedido de Tiziano, 29-sep-2026:
    «poder editar los titulos de los proyectos»).

    POR LA MISMA PUERTA que Telegram: `crud.editar("proyectos", ...)`, que llama
    a `crud.PUERTAS["proyectos"]["nombre"]` (no vacío, largo, espacios) y
    comprueba que no haya OTRO proyecto vivo con ese nombre con la misma
    consulta que usa Lucy para buscarlos. Esta ruta no decide qué nombre vale:
    solo traduce el formulario y el rechazo a algo que se pueda mostrar.

    Si el nombre no cambió, `editar` no escribe ni deja huella y esta ruta lo
    dice («sin cambios»), no dice «guardado». Un rechazo vuelve con una CLAVE
    en la URL (`?error=nombre_vacio|nombre_largo|nombre_repetido`), y ni la URL
    ni el log llevan el nombre pedido.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    nombre = str(formulario.get("nombre", ""))
    try:
        despues, log_id = await crud.editar(
            "proyectos", pid, {"nombre": nombre},
            motivo="Nombre cambiado desde el panel", actor="panel")
    except ValueError as e:
        causa = e.__cause__
        clave = (causa.clave if isinstance(causa, db.NombreDeProyectoNoVale)
                 else None)
        log.warning("Panel de proyectos: nombre rechazado para #%s (%s)",
                    pid, clave or "otro")
        return RedirectResponse(
            f"/proyectos?error=nombre_{clave or 'invalido'}&p={pid}"
            f"#proyecto-{pid}", status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    if log_id is None:
        return RedirectResponse(
            f"/proyectos?error=nombre_igual&p={pid}#proyecto-{pid}", status_code=303)
    return RedirectResponse(
        f"/proyectos?nombre_guardado={pid}&p={pid}#proyecto-{pid}", status_code=303)


@app.post("/proyectos/{pid}/descripcion")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_descripcion_de_proyecto(request: Request, pid: int):
    """Escribir o cambiar «De qué se trata» (`proyectos.descripcion`), parte 3 de la
    página completa del proyecto (8-oct-2026).

    POR LA MISMA PUERTA que Telegram: `crud.editar("proyectos", ...)`, que llama a
    `crud.PUERTAS["proyectos"]["descripcion"]` (texto limpio, a lo sumo
    `db.LARGO_DESCRIPCION_PROYECTO`); deja huella `actor='panel'` y se puede
    deshacer. Esta ruta no decide qué texto vale: traduce el formulario y el rechazo a
    una CLAVE en la URL (`?error=descripcion_largo|descripcion_caracteres|...`); ni la
    URL ni el log llevan el texto.

    El formulario trae `antes`: la huella de la descripción tal como estaba cuando se
    abrió. `crud.editar` la compara con lo que hay AHORA y, si cambió en el medio
    (Telegram agregó un renglón con `perfil`, otra sesión del panel), NO escribe y esta
    ruta vuelve al formulario abierto, con la descripción como está hoy. Sin `antes` (un
    POST que no salió de la página) se trata igual: no se escribe encima de algo que
    no se vio.

    Lo que se dice es lo que pasó: «guardada» solo si `editar` escribió (hay huella), «ya
    decía eso» si el texto limpio es el que había, «ya no está» si el proyecto no existe o
    está en la papelera.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    antes = formulario.get("antes", "")
    # El valor se pasa TAL CUAL (puede no ser texto si el POST es multipart): la
    # puerta de `crud.PUERTAS` decide si vale.
    try:
        despues, log_id = await crud.editar(
            "proyectos", pid, {"descripcion": formulario.get("descripcion", "")},
            motivo="Descripción cambiada desde el panel", actor="panel",
            si_sigue_igual={"descripcion": antes if isinstance(antes, str) else ""})
    except crud.CambioAlEditar:
        log.warning("Panel de proyectos: la descripción de #%s cambió mientras se escribía", pid)
        return RedirectResponse(
            f"/proyectos?error=descripcion_cambio&p={pid}&editar=descripcion#de-que-se-trata",
            status_code=303)
    except ValueError as e:
        causa = e.__cause__
        clave = (causa.clave if isinstance(causa, db.DescripcionDeProyectoNoVale)
                 else "invalida")
        log.warning("Panel de proyectos: descripción rechazada para #%s (%s)", pid, clave)
        return RedirectResponse(
            f"/proyectos?error=descripcion_{clave}&p={pid}&editar=descripcion#de-que-se-trata",
            status_code=303)
    except Exception:
        # La base falló en el guardado. No se sabe con certeza si llegó a confirmar, así que el aviso
        # no dice «guardada» ni «NO se guardó»: manda a mirar cómo quedó (el formulario vuelve abierto
        # con lo que HAY en la base).
        log.exception("Panel de proyectos: falló la base al guardar la descripción de #%s", pid)
        return RedirectResponse(
            f"/proyectos?error=descripcion_base&p={pid}&editar=descripcion#de-que-se-trata",
            status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    if log_id is None:
        return RedirectResponse(
            f"/proyectos?error=descripcion_igual&p={pid}#de-que-se-trata", status_code=303)
    hecho = "descripcion" if despues["descripcion"] else "descripcion_quitada"
    return RedirectResponse(
        f"/proyectos?hecho={hecho}&p={pid}#de-que-se-trata", status_code=303)


# Qué `error=` sale de cada rechazo de las fechas. CERRADO: lo que no esté acá es `fechas_invalida`.
# (`tipo` y `formato` dicen lo mismo a quien lee: eso que escribiste no es un día.)
_ERRORES_DE_FECHAS = {"tipo": "formato", "formato": "formato", "rango": "rango", "vacio": "vacio"}


@app.post("/proyectos/{pid}/fechas")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_fechas_de_proyecto(request: Request, pid: int):
    """Poner o corregir las fechas de un proyecto: cuándo empezó (`inicio`), cuándo se entrega
    (`entrega`) y «Termina cuando» (`termina_cuando`), parte 4 de la página completa del proyecto
    (8-oct-2026).

    POR LA MISMA PUERTA que Telegram: `crud.editar("proyectos", ...)`, que llama a
    `crud.PUERTAS["proyectos"]` por cada columna (un día `AAAA-MM-DD`, el inicio no se deja vacío,
    «Termina cuando» de a lo sumo `db.LARGO_TERMINA_CUANDO`) y rechaza una entrega anterior al inicio.
    Esta ruta no decide qué vale: traduce el formulario y el rechazo a una CLAVE en la URL
    (`?error=inicio_formato|entrega_rango|fechas_orden|termina_largo|...`); ni la URL ni el log llevan
    lo escrito. Se manda todo o no se escribe nada: un rechazo no deja la mitad guardada.

    Lo que se dice es lo que pasó: «guardadas» solo si `editar` escribió (hay huella), «no cambió nada»
    si todo ya era así, «ya no está» si el proyecto no existe o está en la papelera.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    # Los valores se pasan TAL CUAL (pueden no ser texto si el POST es multipart): las puertas deciden.
    cambios = {c: formulario.get(c) for c in ("inicio", "entrega", "termina_cuando") if c in formulario}
    try:
        despues, log_id = await crud.editar(
            "proyectos", pid, cambios, motivo="Fechas cambiadas desde el panel", actor="panel")
    except ValueError as e:
        causa = e.__cause__
        if isinstance(causa, db.TerminaCuandoNoVale):
            clave = f"termina_{causa.clave}"
        elif isinstance(causa, db.FechaDeProyectoNoVale) and causa.clave == "orden":
            clave = "fechas_orden"
        elif isinstance(causa, db.FechaDeProyectoNoVale):
            try:
                columna = causa.columna_con_puerta
            except AttributeError:
                columna = None
            clave = (f"{columna}_{_ERRORES_DE_FECHAS[causa.clave]}"
                     if columna in ("inicio", "entrega") and causa.clave in _ERRORES_DE_FECHAS
                     else "fechas_invalida")
        elif str(e).startswith("Esa tabla no tiene:"):
            clave = "fechas_sin_columnas"
        else:
            clave = "fechas_invalida"
        log.warning("Panel de proyectos: fechas rechazadas para #%s (%s)", pid, clave)
        return RedirectResponse(
            f"/proyectos?error={clave}&p={pid}&editar=fechas#de-que-se-trata", status_code=303)
    except Exception:
        # La base falló en el guardado. No se sabe con certeza si llegó a confirmar, así que el aviso
        # no dice «guardadas» ni «NO se guardó»: manda a mirar cómo quedó.
        log.exception("Panel de proyectos: falló la base al guardar las fechas de #%s", pid)
        return RedirectResponse(
            f"/proyectos?error=fechas_base&p={pid}&editar=fechas#de-que-se-trata", status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    if log_id is None:
        return RedirectResponse(
            f"/proyectos?error=fechas_igual&p={pid}#de-que-se-trata", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=fechas&p={pid}#de-que-se-trata", status_code=303)


@app.post("/proyectos/{pid}/carpeta")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def cambiar_carpeta_de_proyecto(request: Request, pid: int):
    """Poner, cambiar o quitar la carpeta de un proyecto (`proyectos.carpeta`): dónde vive el proyecto,
    una dirección de Drive o la ruta de una carpeta de una computadora. Parte 5 de la página completa
    del proyecto (8-oct-2026).

    POR LA MISMA PUERTA que Telegram: `crud.editar("proyectos", ...)`, que llama a
    `crud.PUERTAS["proyectos"]["carpeta"]` (texto sin caracteres de control `Cc`; U+2028 y U+2029 pasan; de a lo sumo
    `db.LARGO_CARPETA_PROYECTO`); deja huella `actor='panel'` y se puede deshacer. Esta ruta no decide
    qué vale ni si es un enlace (eso lo decide, al PINTAR, `db.enlace_de_carpeta`): traduce el
    formulario y el rechazo a una CLAVE en la URL (`?error=carpeta_largo|carpeta_caracteres|...`); ni
    la URL ni el log llevan lo escrito.

    Un campo vacío QUITA la carpeta. Un POST que ni trae el campo no quita nada: se rechaza.

    Lo que se dice es lo que pasó: «guardada» o «quitada» solo si `editar` escribió (hay huella), «ya
    estaba así» si el texto limpio es el que había, «ya no está» si el proyecto no existe o está en la
    papelera.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    formulario = await request.form()
    if "carpeta" not in formulario:
        log.warning("Panel de proyectos: carpeta de #%s sin el campo", pid)
        return RedirectResponse(
            f"/proyectos?error=carpeta_invalida&p={pid}&editar=carpeta#carpeta-del-proyecto", status_code=303)
    try:
        # El valor se pasa TAL CUAL (puede no ser texto si el POST es multipart): la puerta decide.
        despues, log_id = await crud.editar(
            "proyectos", pid, {"carpeta": formulario.get("carpeta")},
            motivo="Carpeta cambiada desde el panel", actor="panel")
    except ValueError as e:
        causa = e.__cause__
        if isinstance(causa, db.CarpetaDeProyectoNoVale):
            clave = f"carpeta_{causa.clave}"
        elif str(e).startswith("Esa tabla no tiene:"):
            clave = "carpeta_sin_columna"
        else:
            clave = "carpeta_invalida"
        log.warning("Panel de proyectos: carpeta rechazada para #%s (%s)", pid, clave)
        return RedirectResponse(
            f"/proyectos?error={clave}&p={pid}&editar=carpeta#carpeta-del-proyecto", status_code=303)
    except Exception:
        # La base falló en el guardado. No se sabe con certeza si llegó a confirmar, así que el aviso
        # no dice «guardada» ni «NO se guardó»: manda a mirar cómo quedó.
        log.exception("Panel de proyectos: falló la base al guardar la carpeta de #%s", pid)
        return RedirectResponse(
            f"/proyectos?error=carpeta_base&p={pid}&editar=carpeta#carpeta-del-proyecto", status_code=303)
    if despues is None:
        return RedirectResponse("/proyectos?error=proyecto", status_code=303)
    if log_id is None:
        return RedirectResponse(
            f"/proyectos?error=carpeta_igual&p={pid}#carpeta-del-proyecto", status_code=303)
    hecho = "carpeta" if despues["carpeta"] else "carpeta_quitada"
    return RedirectResponse(f"/proyectos?hecho={hecho}&p={pid}#carpeta-del-proyecto", status_code=303)


# LAS NOTAS Y DECISIONES DEL PROYECTO (parte 6, 8-oct-2026). Las tres rutas son sesión de la casa
# (`auth.puede_entrar`; solo ver y sin sesión: 401) y escriben por `db.crear_nota_de_proyecto`,
# `db.editar_nota_de_proyecto` y `db.borrar_nota_de_proyecto`, que son quienes deciden (la ruta traduce
# el formulario y la excepción a una CLAVE en la dirección). Quién escribe sale de la sesión, nunca de
# un campo del formulario; lo escrito nunca viaja en la dirección ni en el registro.

def _error_de_nota(e: Exception) -> str:
    """La CLAVE de `?error=` para lo que rechazó una escritura de nota. Una excepción que no es de las
    de `db` es un fallo de la base: no se sabe si llegó a confirmar."""
    if isinstance(e, db.NotaNoVale):
        return f"nota_{e.clave}"
    if isinstance(e, db.NotaNoEsta):
        return "nota_no_esta"
    if isinstance(e, db.NotaCambio):
        return "nota_cambio"
    if isinstance(e, db.NotasSinColumna):
        return "nota_sin_columna"
    return "nota_base"


@app.post("/proyectos/{pid}/notas")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def escribir_nota_de_proyecto(request: Request, pid: int):
    """Escribir una nota en un proyecto. El autor es la sesión. Un POST que ni trae el campo `texto` no
    escribe nada."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    if "texto" not in formulario:
        return RedirectResponse(f"/proyectos?error=nota_invalida&p={pid}#notas-del-proyecto", status_code=303)
    try:
        # El valor se pasa TAL CUAL (puede no ser texto si el POST es multipart): la puerta decide.
        await db.crear_nota_de_proyecto(pid, chat, formulario.get("texto"))
    except db.NotaSinSesion:
        return _fuera(request)
    except Exception as e:
        clave = _error_de_nota(e)
        if clave == "nota_base":
            log.exception("Panel de proyectos: falló la base al escribir una nota en #%s", pid)
        else:
            log.warning("Panel de proyectos: nota rechazada en #%s (%s)", pid, clave)
        return RedirectResponse(f"/proyectos?error={clave}&p={pid}#notas-del-proyecto", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=nota&p={pid}#notas-del-proyecto", status_code=303)


@app.post("/proyectos/{pid}/notas/{nid}/editar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def editar_nota_de_proyecto(request: Request, pid: int, nid: int):
    """Cambiar el texto de una nota de ESTE proyecto (cualquiera de la casa; el autor no cambia).
    «Ya decía eso» si el texto limpio es el que había: no se escribe ni se dice «guardado»."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    formulario = await request.form()
    if "texto" not in formulario:
        return RedirectResponse(
            f"/proyectos?error=nota_invalida&p={pid}&editar_nota={nid}#nota-{nid}", status_code=303)
    try:
        cambio = await db.editar_nota_de_proyecto(nid, pid, chat, formulario.get("texto"))
    except db.NotaSinSesion:
        return _fuera(request)
    except Exception as e:
        clave = _error_de_nota(e)
        if clave == "nota_base":
            log.exception("Panel de proyectos: falló la base al editar la nota #%s de #%s", nid, pid)
        else:
            log.warning("Panel de proyectos: edición de la nota #%s de #%s rechazada (%s)", nid, pid, clave)
        # El formulario se vuelve a abrir solo si la nota sigue ahí para editarse.
        reabre = f"&editar_nota={nid}" if clave in ("nota_vacio", "nota_largo", "nota_caracteres", "nota_tipo",
                                                      "nota_cambio", "nota_base") else ""
        return RedirectResponse(f"/proyectos?error={clave}&p={pid}{reabre}#nota-{nid}", status_code=303)
    if not cambio:
        return RedirectResponse(f"/proyectos?error=nota_igual&p={pid}#nota-{nid}", status_code=303)
    return RedirectResponse(f"/proyectos?hecho=nota_editada&p={pid}#nota-{nid}", status_code=303)


@app.post("/proyectos/{pid}/notas/{nid}/borrar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def borrar_nota_de_proyecto(request: Request, pid: int, nid: int):
    """Borrar una nota de ESTE proyecto (queda marcada, con su huella; no hay `DELETE`), solo quien la
    escribió. La pregunta «¿borrarla?» la dibuja el servidor (`?borrar_nota=`)."""
    chat = _sesion(request)
    if not auth.puede_entrar(chat):
        return _fuera(request)
    try:
        await db.borrar_nota_de_proyecto(nid, pid, chat)
    except db.NotaSinSesion:
        return _fuera(request)
    except Exception as e:
        clave = _error_de_nota(e)
        if clave == "nota_base":
            log.exception("Panel de proyectos: falló la base al borrar la nota #%s de #%s", nid, pid)
        else:
            log.warning("Panel de proyectos: borrar la nota #%s de #%s rechazado (%s)", nid, pid, clave)
        return RedirectResponse(f"/proyectos?error={clave}&p={pid}#notas-del-proyecto", status_code=303)
    return RedirectResponse(
        f"/proyectos?hecho=nota_borrada&borrada={nid}&p={pid}#notas-del-proyecto", status_code=303)


@app.post("/tareas/{tid}/area")
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
    except db.NombreDeProyectoNoVale as e:
        # El título de la tarea no puede ser el nombre de un proyecto: el motivo
        # REAL (repetido, vacío, largo) viaja como clave, no el «no califica»
        # de abajo, que sería una causa falsa.
        log.warning("Panel de tareas: no se convirtió #%s en proyecto (%s)",
                    tid, e.clave)
        return RedirectResponse(
            f"/tareas/{tid}?error=convertir_{e.clave}", status_code=303)
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


# Las claves de la URL que NO son un nombre: «sin responsable» y el cubo de
# los que ya no se pueden asignar y no tienen nombre. Son las MISMAS que va a
# usar el filtro de la lista (tarea 145); el alta solo las reconoce para no
# tomarlas por un nombre.
RESP_SIN = "_sin"
RESP_OTROS = "_otros"


def _responsable_por_nombre(crudo: str):
    """El responsable que manda la página de Proyectos: por NOMBRE.

    Es el hermano de `_responsable_pedido` para la otra convención de la casa.
    `/tareas` pinta los desplegables con el chat como valor (nadie lo LEE, pero
    viaja); la página de Proyectos los pinta con el NOMBRE, que es lo único que
    se escribe en el HTML (`tests/test_responsable.py::
    test_la_pantalla_no_enseña_ningun_numero_de_chat_como_texto`).

    La lectura del nombre NO se decide acá: la hace `crud.PUERTAS["tareas"]
    ["responsable_chat_id"]`, la MISMA puerta que usan los otros desplegables de
    la página y el `editar` de Telegram. Devuelve `(vale, chat | None)`, la
    misma forma de tres respuestas que `_responsable_pedido`: vacío es «sin
    responsable» (una respuesta de verdad, no un campo que falte), un nombre
    resuelve al chat, y cualquier otra cosa no vale y quien llama no escribe.
    """
    try:
        return True, crud.PUERTAS["tareas"]["responsable_chat_id"](crudo)
    except ValueError:
        return False, None


def _responsable_de_la_url(crudo: str):
    """El chat que dice `?responsable=<nombre>`, o None si no dice ninguno.

    Para PREESCOGER el desplegable del alta (tarea 146, punto 7 de Tiziano:
    desde una vista filtrada, «+ Agregar tarea» llega con ese responsable).
    Nunca el chat en la URL: el nombre, tal como está en `config` (las
    mayúsculas y las tildes no distinguen: la misma lectura que Telegram,
    `crud._chat_del_nombre`, que además se niega si el nombre es de dos
    personas). Solo PRESELECCIONA: quien crea puede cambiarlo, y lo que se
    guarda lo vuelve a decidir `_responsable_pedido` con el valor del
    desplegable. Y aun aquí se pasa por `config.puede_ser_responsable`: un
    nombre que resuelve a alguien que ya no puede serlo no se preselecciona.
    Todo lo demás —vacío, `_sin`, `_otros`, un nombre que no es de nadie o de
    varios— es «sin responsable», que es como se abre siempre.
    """
    crudo = (crudo or "").strip()
    if not crudo or crudo in (RESP_SIN, RESP_OTROS):
        return None
    chat, _motivo = crud._chat_del_nombre(crudo)
    if chat is None or not config.puede_ser_responsable(chat):
        return None
    return chat


def _filtro_pedido(crudo):
    """Lee `?responsable=` de /tareas (tarea 145). Devuelve
    `(tipo, chat, clave, aviso)`:

      · `tipo`: "todas", "sin", "otros" o "chat".
      · `chat`: solo con tipo "chat".
      · `clave`: cómo se escribe ese filtro en la URL y en el campo oculto del
        formulario, YA canónica: "" (todas), `RESP_SIN`, `RESP_OTROS` o el
        NOMBRE tal como está en `config.nombres_con_code()`. Nunca un chat.
      · `aviso`: True si se pidió un nombre que no es de nadie o es de varios.

    QUIÉN ES UN NOMBRE NO SE DECIDE ACÁ: `crud._chat_del_nombre`, la misma
    lectura que usa Telegram (ignora mayúsculas y tildes y se niega si el nombre
    es de más de una persona). Un nombre desconocido o ambiguo NO da una lista
    vacía: da «todas» y el aviso, para que un enlace viejo nunca deje la
    pantalla en blanco sin explicar por qué. Es la ÚNICA puerta del filtro: el
    campo oculto que viaja en el guardado la vuelve a pasar.
    """
    crudo = (crudo or "").strip()
    if not crudo:
        return "todas", None, "", False
    if crudo == RESP_SIN:
        return "sin", None, RESP_SIN, False
    if crudo == RESP_OTROS:
        return "otros", None, RESP_OTROS, False
    chat, _motivo = crud._chat_del_nombre(crudo)
    if chat is None:
        return "todas", None, "", True
    return "chat", chat, config.nombres_con_code()[chat], False


def _coincide(resp, tipo, chat) -> bool:
    """¿Una fila con este `responsable_chat_id` entra en ese filtro?

    LAS TRES CLASES SON UNA PARTICIÓN, y por eso ninguna fila queda sin botón que
    la muestre: sin responsable (`None`), con NOMBRE (persona, Code o alguien que
    ya no entra pero conserva nombre) y, lo que queda, sin nombre («otros»).
    """
    if tipo == "todas":
        return True
    if tipo == "sin":
        return resp is None
    if tipo == "otros":
        return resp is not None and resp not in config.nombres_con_code()
    return resp == chat


def _botones_de_responsable(filas, activa: str):
    """Los botones de la lista, DERIVADOS de lo real: `[{clave, etiqueta, n,
    activo}]`. Personas del panel y Code aunque hoy tengan 0, «Sin responsable»
    siempre, alguien que ya no entra pero tiene tareas y nombre (con su nombre) y
    «Otros» solo si hay alguna fila sin nombre. Ninguna persona está escrita
    acá. `n` sale de las MISMAS filas que se pintan y del mismo `_coincide`."""
    resp = [f.get("responsable_chat_id") for f in filas]
    nombres = config.nombres_con_code()

    def boton(clave, etiqueta, tipo, chat=None):
        return {"clave": clave, "etiqueta": etiqueta,
                "n": sum(1 for r in resp if _coincide(r, tipo, chat)),
                "activo": clave == activa}

    botones = [boton("", "Todas", "todas")]
    vistos = set()
    for chat, nombre in config.personas_del_panel():
        botones.append(boton(nombre, nombre, "chat", chat))
        vistos.add(chat)
    botones.append(boton(config.NOMBRE_CODE, config.NOMBRE_CODE, "chat",
                         config.CHAT_ID_CODE))
    vistos.add(config.CHAT_ID_CODE)
    botones.append(boton(RESP_SIN, "Sin responsable", "sin"))
    for r in resp:
        if r is not None and r not in vistos and r in nombres:
            vistos.add(r)
            botones.append(boton(
                nombres[r], f"{nombres[r]} — ya no se le puede asignar",
                "chat", r))
    if any(r is not None and r not in nombres for r in resp):
        botones.append(boton(RESP_OTROS, "Otros", "otros"))
    return botones


# Cuántas tareas nuevas se pueden escribir de una vez al marcar UNA sola
# hecha («tarea derivada», 25-sep-2026: Tiziano, en el panel /tareas, pidió
# que al marcar una tarea hecha se pueda escribir ahí mismo la que sale de
# ella -- y luego, revisando el diseño, que "pueden ser varias"). No dijo un
# número. Éste es un TECHO DE PANTALLA, igual que `TOPE_TAREAS` en `db.db`:
# existe para que el formulario no crezca sin límite, no es una regla de
# negocio. Si tres se queda corto, se pide y se sube -- un renglón de más no
# cuesta nada mientras esté vacío (ver `_derivadas_pedidas`).
MAX_DERIVADAS = 3


def _derivadas_pedidas(formulario, tid: int, claves_area_validas: set[str],
                       lee_responsable=_responsable_pedido):
    """Los renglones «¿sale algo nuevo de ésta?» de la tarea `tid`, leídos
    del MISMO envío que trae `hecha_<tid>`. Devuelve `(vale, [derivadas])`,
    con cada derivada ya lista para `db.cerrar_y_derivar`:
    `{"titulo", "vence_en", "area", "responsable_chat_id"}`.

    LA ÚNICA PIEZA PARA LAS DOS PANTALLAS QUE OFRECEN LA TAREA DERIVADA:
    `/tareas` (`guardar_tareas`) y la página de Proyectos
    (`marcar_tarea_hecha_desde_proyectos`), que la llaman con sus propios
    lectores de responsable porque las dos páginas escriben esa columna de
    forma distinta —una con el chat, otra con el nombre— pero TIENEN QUE
    decidir igual todo lo demás (título, fecha, área, y que un renglón que no
    vale no cierre la madre). `lee_responsable` es lo único que cambia entre
    las dos: `_responsable_pedido` (el chat, `/tareas`) o
    `_responsable_por_nombre` (el nombre, Proyectos).

    LOS ÍNDICES VAN DE 1 A `MAX_DERIVADAS`, SIN HUECOS QUE ADIVINAR: la
    plantilla pinta siempre los `MAX_DERIVADAS` renglones de cada fila
    pendiente (ocultos con `hidden`; el JS solo les quita el atributo), así
    que no hace falta un campo aparte que diga "cuántos renglones se
    usaron" -- un renglón vacío es sencillamente "acá no sale nada", igual
    que `deriva_titulo_<tid>_<n>` ausente del todo (formulario armado a
    mano, por ejemplo).

    SI ALGÚN RENGLÓN CON TÍTULO TIENE UN CAMPO QUE NO VALE, la función
    entera devuelve `(False, [])` -- ni un renglón se crea, aunque los otros
    tres estuvieran bien. Es la decisión D6 del diseño aprobado
    (disenos/lucy-tarea-derivada/DISENO.md): "si la nueva no vale, la vieja
    tampoco se cierra" -- cerrar tres de cuatro y callarse cuál falló sería
    peor que no cerrar ninguna, porque lo escrito en el renglón malo se
    pierde igual al recargar.

    LAS PIEZAS DE VALIDACIÓN SON LAS QUE YA EXISTEN, ninguna nueva: título
    1-`LARGO_TITULO` como `crear_tarea`; `_vence_con_hora_valido` como el
    resto de esta ruta -- CON hora, no como `/tareas/nueva` (decisión D9 del
    diseño: la hora decide cuándo suena el aviso, la misma que ya rige
    `vence_<id>` acá arriba); el área contra `claves_area_validas` -- lo que
    el `<select>` ofreció es lo único que se acepta, igual que `crear_tarea`;
    el responsable con `_responsable_pedido`, que ya pasa por
    `config.puede_ser_responsable`.
    """
    derivadas: list[dict] = []
    for n in range(1, MAX_DERIVADAS + 1):
        titulo = str(formulario.get(f"deriva_titulo_{tid}_{n}", "")).strip()
        if not titulo:
            # Renglón vacío: no sale nada nuevo en este puesto. No es un
            # error, es la respuesta normal para la enorme mayoría de las
            # tareas que se cierran sin dejar nada atrás.
            continue
        if len(titulo) > LARGO_TITULO:
            return False, []
        vale, vence_en = _vence_con_hora_valido(
            str(formulario.get(f"deriva_vence_{tid}_{n}", "")))
        if not vale:
            return False, []
        area = str(formulario.get(f"deriva_area_{tid}_{n}", "")).strip() or None
        if area is not None and area not in claves_area_validas:
            return False, []
        vale, resp = lee_responsable(
            str(formulario.get(f"deriva_resp_{tid}_{n}", "")))
        if not vale:
            return False, []
        derivadas.append({"titulo": titulo, "vence_en": vence_en,
                          "area": area, "responsable_chat_id": resp})
    return True, derivadas


@app.post("/tareas")
@auth.puerta(auth.PUERTA_SIEMPRE)
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

    LA TAREA DERIVADA (25-sep-2026) VIAJA EN EL MISMO ENVÍO TAMBIÉN, por el
    mismo motivo que el responsable y la fecha: Tiziano pidió «escribir la
    nueva y luego darle a guardar», y ES el mismo botón -- los campos
    `deriva_titulo_<tid>_<n>` (y sus hermanos `_vence_`, `_area_`, `_resp_`)
    son más entradas DENTRO del `<form>` único, igual que `vence_<id>` y
    `resp_<id>` ya lo son. `_derivadas_pedidas` los valida ANTES de escribir
    nada; si algo no vale, ni la madre se cierra ni ninguna hija se crea
    (D6). El servidor SOLO mira esos campos si en el MISMO envío viene
    `hecha_<tid>` -- no depende de que el JS haya escondido bien el
    renglón: una tarea con título derivado pero SIN la casilla marcada no
    crea nada, es exactamente el mismo criterio que ya sigue el resto de
    esta ruta con `prev_`.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)

    formulario = await request.form()
    hechas, asignadas, movidas, ignoradas = 0, 0, 0, 0
    hijas_creadas: list[int] = []
    sin_cerrar: list[int] = []
    # Se pide UNA sola vez, no una por tarea: `db.areas()` ya tolera la
    # migración del área sin aplicar (devuelve `[]`), y con `[]` ningún
    # renglón derivado puede llevar área -- el mismo estado en el que se ve
    # la pantalla hoy si esa migración no corrió.
    claves_area_validas = {a["clave"] for a in await db.areas()}

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

    chat = _sesion(request)
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
        vale, derivadas = _derivadas_pedidas(formulario, tid, claves_area_validas)
        if not vale:
            # D6 del diseño: la nueva no vale, así que la vieja TAMPOCO se
            # cierra. Lo escrito se pierde al recargar igual, y dejar la
            # vieja abierta es la señal visible de que hay que repetirlo.
            ignoradas += 1
            continue
        try:
            resultado = await db.cerrar_y_derivar(chat, tid, derivadas)
        except db.ProyectoNoAdmiteTareas as e:
            # El proyecto de esta tarea está cerrado y se pidió una derivada:
            # `cerrar_y_derivar` no cerró NADA (D6: si la nueva no vale, la
            # vieja tampoco). Se cuenta como cambio sin efecto, se deja rastro
            # y se le DICE a quien guardó cuál no se cerró (`sin_cerrar`).
            log.warning("Panel de tareas: derivada rechazada, el proyecto no "
                        "admite tareas (%s)", e.clave)
            ignoradas += 1
            sin_cerrar.append(tid)
            continue
        if resultado is None:
            ignoradas += 1
            continue
        cerrada, hijas = resultado
        if cerrada:
            hechas += 1
        hijas_creadas.extend(hijas)

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
        vale, chat_resp = _responsable_pedido(pedido)
        if not vale:
            ignoradas += 1
            continue
        if await db.asignar_responsable(tid, chat_resp):
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
    # `derivadas_ids` en la URL -- no solo la cuenta -- porque el aviso del
    # diseño aprobado ("Tareas nuevas que salen de otra: 1 (#512)") nombra
    # el número: sin esto, la persona tendría que buscarla en la lista.
    ids_derivadas = ",".join(str(i) for i in hijas_creadas)
    # EL FILTRO SOBREVIVE AL GUARDADO (tarea 145), pero VUELVE A PASAR por la
    # misma puerta que lo leyó al pintar (`_filtro_pedido`): lo que viaja en la
    # URL es la clave CANÓNICA que sale de ahí, nunca el texto crudo del campo.
    _, _, clave_filtro, _ = _filtro_pedido(str(formulario.get("filtro", "")))
    filtro_url = f"&responsable={quote(clave_filtro, safe='')}" if clave_filtro else ""
    return RedirectResponse(
        f"/tareas?guardadas={hechas}&asignadas={asignadas}&movidas={movidas}"
        f"&derivadas={len(hijas_creadas)}&derivadas_ids={ids_derivadas}"
        + (f"&sin_cerrar={','.join(str(i) for i in sin_cerrar)}" if sin_cerrar else "")
        + f"{filtro_url}",
        status_code=303)


@app.get("/tareas/nueva", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
async def tarea_nueva(request: Request, error: Aviso[str] = "",
                      responsable: Navegacion[str] = "", proyecto: Navegacion[str] = ""):
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

    EL ÁREA (encargo 4) se puede elegir en esta pantalla SALVO cuando se abre
    dentro de un proyecto (`?proyecto=<id>`, pieza 2 del diseño «proyectos»):
    ahí el selector no se pinta, porque la tarea hereda el área del proyecto.
    Sin proyecto —lo normal—, el caso que tendría que esconder el selector no
    existe. `db.areas()` ya tolera que la tabla no exista (devuelve `[]`), así
    que si la migración no se aplicó todavía el `<select>` sale vacío —solo
    queda «Sin área»— en vez de romper la pantalla.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    preescogido = _responsable_de_la_url(responsable)
    # DENTRO DE UN PROYECTO (pieza 2 del diseño «proyectos»): `?proyecto=<id>`,
    # validado por LA puerta (`db.proyecto_para_tareas`). Si no vale (no existe,
    # está en la papelera, está cerrado) la pantalla se abre SIN proyecto y lo
    # DICE: nunca escribe en un proyecto que no se puede.
    proyecto_fila, motivo_proyecto = None, ""
    if proyecto.strip():
        pid = _id_escrito(proyecto)
        try:
            if pid is None:
                raise db.ProyectoNoAdmiteTareas("no_existe", "")
            proyecto_fila = await db.proyecto_para_tareas(pid)
        except db.ProyectoNoAdmiteTareas as e:
            motivo_proyecto = e.clave
    con_proyecto = proyecto_fila is not None
    return plantillas.TemplateResponse(
        request, "tarea_nueva.html",
        {"proyecto": proyecto_fila, "motivo_proyecto": motivo_proyecto,
         "sala_no_la_ve": (con_proyecto and proyecto_fila["area"] != db.AREA_TECNICA),
         "primeros": await db.tareas_para_elegir_primero(0) if con_proyecto else [],
         "personas_lista": await db.personas_vivas() if con_proyecto else [],
         "largo_detalle": LARGO_DETALLE,
         "error": error, "piso_fecha": PISO_FECHA.isoformat(),
         "largo_titulo": LARGO_TITULO, "areas": await db.areas(),
         # El desplegable de responsable sale de `config.opciones_de_
         # responsable`, la MISMA lista que pinta la tabla de /tareas. El
         # preescogido es un chat (o None = «sin responsable») y se compara
         # como texto, que es lo que viaja en el formulario.
         "opciones_responsable": config.opciones_de_responsable(),
         "responsable_elegido": "" if preescogido is None else str(preescogido),
         "chat_id_code": config.CHAT_ID_CODE,
         "nombre_code": config.NOMBRE_CODE,
         "area_tecnica": db.AREA_TECNICA})


@app.post("/tareas/nueva")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def crear_tarea(request: Request):
    """Escribir una tarea a mano. La cuarta escritura del panel.

    CUATRO CAMPOS: título, cuándo vence, área (desde el encargo 4) y
    responsable (tarea 146, 28-sep-2026; vacío = sin responsable). DENTRO DE UN
    PROYECTO (campo oculto `proyecto`, pieza 2 del diseño «proyectos») suma
    «Primero:», de quién trata y detalle, y el área no se elige. La tabla
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

    # EL PROYECTO (pieza 2 del diseño «proyectos»): un campo oculto con el id,
    # validado por LA puerta (`db.proyecto_para_tareas`; el escritor la vuelve a
    # pasar). Sin el campo, la tarea es suelta, como siempre.
    proyecto_id = None
    proyecto_fila = None
    crudo_proyecto = str(formulario.get("proyecto", "")).strip()
    en_proyecto = f"?proyecto={crudo_proyecto}&" if crudo_proyecto else "?"

    def _vuelta(clave: str):
        log.warning("Panel de tareas: tarea a mano rechazada por %s", clave)
        destino = (f"/tareas/nueva?proyecto={proyecto_id}&error={clave}"
                   if proyecto_id is not None else f"/tareas/nueva?error={clave}")
        return RedirectResponse(destino, status_code=303)

    if crudo_proyecto:
        proyecto_id = _id_escrito(crudo_proyecto)
        try:
            if proyecto_id is None:
                raise db.ProyectoNoAdmiteTareas("no_existe", "")
            proyecto_fila = await db.proyecto_para_tareas(proyecto_id)
        except db.ProyectoNoAdmiteTareas as e:
            log.warning("Panel de tareas: tarea a mano rechazada por proyecto (%s)",
                        e.clave)
            return RedirectResponse(
                "/tareas/nueva?error=proyecto" + ("_cerrado" if e.clave == "cerrado"
                                                  else ""), status_code=303)

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
    if proyecto_id is not None:
        # Con proyecto el área es la del proyecto: si llega una en el POST se
        # IGNORA (la pantalla ni la ofrece) y el escritor guarda `area = NULL`.
        area = None
    elif area is not None:
        claves_validas = {a["clave"] for a in await db.areas()}
        if area not in claves_validas:
            return _vuelta("area")

    # EL RESPONSABLE (tarea 146): por la MISMA puerta que el desplegable de la
    # tabla. Vacío = sin responsable, válido. Si es Code, el área pasa a ser
    # `db.AREA_TECNICA` --lo hace `db.crear_tarea_desde_el_panel`, que es quien
    # escribe--, pero esa área tiene que existir en `db.areas()` o la FK
    # reventaría en un 500: se rechaza como cualquier otra área que no existe.
    vale, responsable = _responsable_pedido(
        str(formulario.get("responsable", "")))
    if not vale:
        return _vuelta("responsable")
    if responsable == config.CHAT_ID_CODE and proyecto_id is None:
        if db.AREA_TECNICA not in {a["clave"] for a in await db.areas()}:
            return _vuelta("area")

    # «DE QUIÉN TRATA» (`persona_id`): una persona que YA existe, elegida de la
    # lista. Esta pantalla no crea personas. Vacío = ninguna.
    persona_id = None
    crudo_persona = str(formulario.get("persona", "")).strip()
    if crudo_persona:
        persona_id = _id_escrito(crudo_persona)
        if persona_id is None or not await db.persona_viva(persona_id):
            return _vuelta("persona")

    # «PRIMERO:» por LA puerta de siempre (`crud._primero_que_vale`): existe,
    # no está en la papelera. Al crear no hay círculo posible.
    primero_id = None
    crudo_primero = str(formulario.get("primero_id", "")).strip()
    if crudo_primero:
        try:
            primero_id = await crud._primero_que_vale(crudo_primero)
        except ValueError:
            return _vuelta("primero")

    # EL DETALLE: texto opcional, con tope.
    detalle = _detalle_valido(str(formulario.get("detalle", "")))
    if detalle is False:
        return _vuelta("detalle")

    try:
        tid = await db.crear_tarea_desde_el_panel(
            chat, titulo, vence_en, area, responsable,
            proyecto_id=proyecto_id, primero_id=primero_id, detalle=detalle,
            persona_id=persona_id)
    except db.ProyectoNoAdmiteTareas as e:
        # El escritor volvió a mirar y el proyecto ya no vale (lo cerraron o lo
        # archivaron mientras se escribía).
        return RedirectResponse(
            "/tareas/nueva?error=proyecto" + ("_cerrado" if e.clave == "cerrado"
                                              else ""), status_code=303)
    except db.TareaNoValida as e:
        # El escritor rechazó el responsable, la persona o el «Primero:» que la
        # ruta acababa de dar por buenos (cambiaron en medio): mismo aviso que
        # si se hubiera rechazado antes, nunca un 500.
        return _vuelta(e.clave)
    except ValueError as e:
        # Cualquier otro «no» del escritor: no se guardó nada y se dice.
        log.warning("Panel de tareas: el escritor rechazó la tarea a mano: %s", e)
        return _vuelta("rechazada")
    if proyecto_id is not None:
        # Al guardar se vuelve AL PROYECTO, y el aviso dice lo que pasó. Si es
        # de Code y la sala no la va a ver (su proyecto no es del área
        # técnica), lo dice: ver `db.sala_ve`.
        sala = ("&sala_no=1" if responsable == config.CHAT_ID_CODE and not
                db.sala_ve(responsable, proyecto_fila["area"]) else "")
        return RedirectResponse(
            f"/proyectos?p={proyecto_id}&tarea_creada={tid}{sala}#proyecto-{proyecto_id}",
            status_code=303)
    # Se vuelve A LA LISTA y no al formulario: la tarea recién escrita tiene
    # que VERSE en su grupo. Un "guardado" que no muestra lo guardado obliga a
    # confiar, y este panel existe para no tener que confiar.
    return RedirectResponse(f"/tareas?creada={tid}", status_code=303)


# El largo máximo de un comentario y la limpieza de su texto viven en `db`
# (`db.LARGO_COMENTARIO`, `db.texto_de_comentario`): los usa también
# `db.editar_comentario`, que decide una vez. Acá solo se les da el nombre de
# siempre.
LARGO_COMENTARIO = db.LARGO_COMENTARIO

# El detalle de una tarea (`tareas.detalle`, texto libre que Lucy escribe por
# Telegram): mismo tope que un comentario, por la misma razón.
LARGO_DETALLE = LARGO_COMENTARIO


def _detalle_valido(crudo: str):
    """El detalle a guardar (`None` si vino vacío) o `False` si se pasa del tope."""
    limpio = (crudo or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not limpio:
        return None
    return False if len(limpio) > LARGO_DETALLE else limpio


def _id_escrito(crudo: str):
    """El id que dice ese texto, o None si no es exactamente un entero positivo
    escrito de una sola manera (`str(int(t)) == t`), igual que `chat_escrito`."""
    t = (crudo or "").strip()
    try:
        n = int(t)
    except ValueError:
        return None
    return n if n > 0 and str(n) == t else None


def _texto_de_comentario(crudo: str) -> str | None:
    """El cuadro de texto → el comentario a guardar, o None si no vale (lo decide
    `db.texto_de_comentario`)."""
    return db.texto_de_comentario(crudo)


@app.get("/tareas/{tid}", response_class=HTMLResponse)
@auth.puerta(auth.PUERTA_SIEMPRE)
async def tarea_detalle(request: Request, tid: int, error: Aviso[str] = "",
                        comentado: Aviso[int] = 0, borrado: Aviso[int] = 0,
                        area_guardada: Aviso[int] = 0, primero_guardado: Aviso[int] = 0,
                        paso_agregado: Aviso[int] = 0, paso_movido: Aviso[str] = ""):
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
                # `nombres_con_code()` y no `NOMBRES_POR_CHAT` a secas: esta
                # misma pantalla pinta el RESPONSABLE de la tarea (línea 15 de
                # la plantilla), que sí puede ser Code (§1/§2, 26-sep-2026);
                # los autores de comentario (línea 194) SÍ pueden ser Code desde
                # el 1-oct-2026 (la sala comenta sus tareas por la puerta HTTP),
                # y este merge es lo que los pinta como «Code».
                "nombres": config.nombres_con_code(), "error": error,
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
async def marcar_paso(request: Request, tid: int, pid: int):
    """Marcar (o desmarcar) UN micro-paso (encargo 7).

    LA PERTENENCIA SE COMPRUEBA ANTES DE ESCRIBIR (arreglo tras el NO PASA
    del testigo sobre `bebf6c9`): `db.pertenece_paso(tid, pid)`, la MISMA
    pieza que usa `quitar_paso` más abajo. Hasta este arreglo la
    comprobación era `despues.get("tarea_id") != tid` -- DESPUÉS de que el
    `UPDATE` ya había corrido: el paso de otra tarea quedaba marcado igual,
    aunque la respuesta dijera error.

    Reusa `crud.editar` ENTERO -- la misma huella, el mismo deshacer que
    cualquier otra edición genérica -- en vez de una escritura aparte. El
    valor que llega es el que el checkbox YA tiene DESPUÉS del clic (ver la
    plantilla: cada checkbox manda su propio formulario con el valor al
    que apunta, no el que tenía).
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    if not await db.pertenece_paso(tid, pid):
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    formulario = await request.form()
    hecho = str(formulario.get("hecho", "")).strip() == "1"
    despues, _log_id = await crud.editar(
        "micro_pasos", pid, {"hecho": hecho},
        motivo="Paso marcado desde el panel de tareas", actor="panel")
    if despues is None:
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    return RedirectResponse(f"/tareas/{tid}", status_code=303)


@app.post("/tareas/{tid}/pasos/{pid}/borrar")
@auth.puerta(auth.PUERTA_SIEMPRE)
async def quitar_paso(request: Request, tid: int, pid: int):
    """Quitar UN micro-paso (encargo 7). `crud.borrar` -- ya gratis, `micro_
    pasos` está en `crud.TABLAS`, así que esto es soft-delete + huella +
    deshacer, sin escribir nada nuevo.

    LA PERTENENCIA SE COMPRUEBA ANTES DE ESCRIBIR (arreglo tras el NO PASA
    del testigo sobre `bebf6c9`): `db.pertenece_paso(tid, pid)`, la MISMA
    pieza que usa `marcar_paso` arriba. Hasta este arreglo esta ruta NO
    comprobaba nada -- `crud.borrar` solo mira el `id` del paso, nunca de
    quién es -- así que bastaba con adivinar el id de un paso ajeno en la
    URL para borrarlo.
    """
    if not auth.puede_entrar(_sesion(request)):
        return _fuera(request)
    if not await db.pertenece_paso(tid, pid):
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    log_id = await crud.borrar(
        "micro_pasos", pid, motivo="Paso quitado desde el panel de tareas")
    if log_id is None:
        return RedirectResponse(f"/tareas/{tid}?error=pasos", status_code=303)
    return RedirectResponse(f"/tareas/{tid}", status_code=303)


@app.post("/tareas/{tid}/pasos/{pid}/mover")
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
@auth.puerta(auth.PUERTA_SIEMPRE)
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
