"""La puerta de los avisos: «se guardó», «no se pudo», «ya no está»…

EL DEFECTO QUE ESTO CIERRA. Cada pantalla de Lucy se entera de lo que acaba de
pasar por la dirección a la que la manda el POST (`/proyectos?hecho=cerrado&p=3`).
Una dirección se puede escribir a mano, guardar en favoritos, volver a abrir con
«atrás» o recargar: la pantalla decía que algo había pasado cuando en ese
momento no había pasado nada. La regla de la casa: el sistema no le dice a quien
lo usa que pasó algo que no pasó.

LA FORMA. Una sola puerta, que no mira qué dice cada aviso:

  1. Todo POST que contesta con una redirección a una dirección que lleva un
     aviso deja, en la misma respuesta, un RECIBO en una cookie: la firma de ESE
     aviso (la ruta y sus parámetros de aviso, no los de navegación), con
     vencimiento. Solo
     existe porque la ruta de verdad llegó a ese punto.
  2. Un GET a una pantalla con avisos solo los deja pasar si trae el recibo de ese
     aviso; el recibo se gasta al usarse (una recarga ya no lo
     tiene) y la página que lo pintó sale con `Cache-Control: no-store` (el botón
     «atrás» la vuelve a pedir en vez de resucitarla del caché). Sin recibo, los parámetros de aviso se quitan de la petición ANTES
     de que la ruta la lea: la pantalla sale como si no se hubieran escrito.

QUÉ ES UN AVISO Y QUÉ ES NAVEGACIÓN (la regla, en una línea): un parámetro es de
AVISO si lo único que hace es CONTAR algo que pasó, o que se rechazó, en la
acción anterior; es de NAVEGACIÓN si decide qué se dibuja aunque no haya pasado
nada (qué proyecto, qué filtro, qué formulario abierto, qué pregunta de
confirmar). Los rechazos van por la misma puerta que los «hechos»: «Ya hay otro
proyecto con ese nombre» o «No se guardó» dichos sin que pasara nada también
son falsos (miden algo que no ocurrió) y asustan sin motivo.

DÓNDE SE DECLARA. En la firma de la propia ruta, junto al parámetro:
`hecho: Aviso[str] = ""` o `p: Navegacion[int] = 0`. La puerta lee esa marca de
las rutas de verdad (`route.dependant.query_params`); no hay otra lista. Un
parámetro de una ruta GET sin marca pone roja `tests/test_avisos_verdad.py`.

UN PARÁMETRO QUE ES LAS DOS COSAS (`AvisoQueElige`): `creado=5` dice «se creó» y
además escoge el proyecto 5 en pantalla. Sin recibo se quita el aviso pero su
valor se lee como el parámetro de navegación que declara (`p`), para que una
recarga siga en el mismo proyecto (solo si el valor es un id: dígitos).

LO QUE LA RUTA NO DECIDIÓ NO SE FIRMA NI VIAJA. Una redirección puede traer parámetros
de aviso que la ruta no puso: el destino que llega en un campo del formulario
(`volver`, en `/categorias`, `/efectivo` y `/borrar`), una cabecera, la propia
dirección del POST. Firmar «lo que haya en el `Location`» amparaba un aviso heredado de
otra página o escrito por la persona. Por eso, al salir, la puerta QUITA del `Location`
todo parámetro de aviso que aparezca DENTRO de un valor que la petición trajo (un
campo del formulario, la dirección, una cabecera; menos `Referer` y `Cookie`) y firma solo
lo que queda. Un campo que se llama como un aviso (`id=1` al restaurar) no cuenta: es el
argumento de la acción. Que una ruta lo repita como aviso lo vigila la prueba que recorre
todas las rutas que redirigen. Es la
misma puerta para todas las rutas, hoy y mañana. Además `/movimientos` ya no pinta los
avisos en el `volver` de sus formularios (`consulta_sin_avisos`).

LA PUERTA NUNCA TUMBA NADA. Dentro de la puerta ninguna excepción llega a la persona:
al entrar (GET), si algo falla se quitan TODOS los avisos de la petición (falla hacia
«no se dice nada», nunca hacia «se dice algo falso»); al salir, si algo falla la
respuesta sale tal cual la dio la ruta, sin recibo (lo que ya se guardó sigue guardado
y sin error). Una cookie hostil (gigante, con dígitos que no son de ASCII, con números
que `int()` no acepta) se descarta antes de usarse.

FRONTERA (lo que esta puerta NO ve): una ruta que llame a su función a mano en
vez de pasar por la aplicación (las pruebas lo hacen) no pasa por la puerta; solo
las respuestas de un método que no sea GET dejan recibo (una ruta GET que redirija
a una dirección con avisos no lo deja; hoy ninguna lo hace); un aviso que viaje por
otro canal que un parámetro del GET (una cabecera, una cookie propia, el cuerpo de
un POST que se pinta sin redirigir) no se declara aquí; una ruta que copie a su
redirección un aviso de la cabecera `Referer` (el navegador la manda sola, así que no
puede contarse como «lo que trajo la petición»: taparía los avisos de verdad) le daría
recibo a un aviso viejo (hoy ninguna lo hace; `tests/test_avisos_verdad.py` recorre todas
las rutas que redirigen con un `Referer` hostil); un aviso de verdad idéntico, letra por
letra, a uno que venía dentro de un valor de la petición se quita también (lado seguro: se
pierde un aviso verdadero, nunca se dice uno falso); cuerpos de más de `LIMITE_DE_CUERPO` bytes no se
revisan y por eso ninguna de sus redirecciones deja recibo. Un destino escrito codificado (`guardad%6Fs=99`, `%2099`, doble codificación) se compara
después de una decodificación más, que es la que el GET le hará al `Location`: lo vigila
`test_un_destino_con_el_aviso_escrito_codificado…` con formas a mano y generadas con semilla;
una codificación que el GET no decodifica tampoco forma un aviso, así que no hay una más que cubrir. Un parámetro mal marcado
como navegación sale tal cual.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from typing import Annotated, TypeVar
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit

from fastapi import Query
from starlette.requests import cookie_parser
from starlette.routing import Match

import config

T = TypeVar("T")
log = logging.getLogger("lucy.avisos")

# Las tres marcas. `json_schema_extra` es lo que FastAPI deja en
# `route.dependant.query_params[i].field_info`; no cambia cómo la ruta recibe el
# valor ni su valor por omisión (las pruebas llaman a las rutas a mano).
Aviso = Annotated[T, Query(json_schema_extra={"aviso": True})]
Navegacion = Annotated[T, Query(json_schema_extra={"aviso": False})]
AvisoQueElige = Annotated[T, Query(json_schema_extra={"aviso": True, "elige": "p"})]

COOKIE = "lucy_aviso"
# Lo que dura un recibo: el viaje del POST a su redirección. Más tiempo lo
# dejaría útil en un favorito; menos lo vencería en un celular lento.
VIDA_RECIBO = 120
# Cuántos recibos conviven en la cookie (varias pestañas guardando a la vez).
MAXIMO_DE_RECIBOS = 6
# Lo más largo que se mira de una cookie de recibos y del cuerpo de un POST.
LARGO_DE_COOKIE = 4096          # menor que los 4300 dígitos que `int()` acepta
LIMITE_DE_CUERPO = 1_000_000


def _firmar(texto: str) -> str:
    return hmac.new(config.TELEGRAM_TOKEN.encode(), ("aviso|" + texto).encode(),
                    hashlib.sha256).hexdigest()[:32]


def clave_de(ruta: str, pares, avisos) -> str:
    """Lo que dice el aviso, en su forma canónica: la ruta y SOLO los parámetros de
    aviso (decodificados y ordenados). El recibo vale aunque el navegador reescriba
    el orden o la forma de escapar, y solo para ese aviso en esa pantalla; los
    parámetros de navegación (qué proyecto se mira) no entran: no dicen nada de lo
    que pasó."""
    return ruta + "?" + urlencode(sorted((k, v) for k, v in pares if k in avisos))


def _recibo(clave: str, vence: int) -> str:
    return f"{vence}.{_firmar(f'{clave}|{vence}')}"


def _recibos_de(valor: str | None, ahora: float) -> list[str]:
    """Los recibos de la cookie que todavía no vencieron (el resto se descarta)."""
    vivos = []
    if not valor or len(valor) > LARGO_DE_COOKIE or not valor.isascii():
        return vivos                      # una cookie hostil no se usa: sin recibos
    for r in valor.split("~"):
        vence, _, firma = r.partition(".")
        # solo dígitos ASCII (el largo total ya está acotado arriba: `int()` revienta con más de 4300
        # dígitos y `LARGO_DE_COOKIE` es menor)
        if vence.isdigit() and firma and int(vence) >= ahora:
            vivos.append(r)
    return vivos[-MAXIMO_DE_RECIBOS:]


def _ruta_get(app, ruta: str):
    """La ruta GET de la aplicación que contestaría a `ruta` (la primera que
    encaja, como el enrutador), o None."""
    scope = {"type": "http", "method": "GET", "path": ruta, "root_path": ""}
    for r in app.router.routes:
        if Match.FULL == r.matches(scope)[0]:
            return r
    return None


def marcas_de(ruta) -> dict[str, dict]:
    """{parámetro: marca} de una ruta GET, leído de su firma real. La marca es
    `{"aviso": True|False, "elige": …}`; un parámetro sin marca da `{}`. Una ruta
    que no existe (`None`) o que no declara parámetros (un `Mount`) da `{}`."""
    dependiente = getattr(ruta, "dependant", None)
    if dependiente is None:
        return {}
    return {p.name: dict(getattr(p.field_info, "json_schema_extra", None) or {})
            for p in dependiente.query_params}


def parametros_sin_marca(app) -> list[tuple[str, str]]:
    """[(ruta, parámetro)] de toda ruta GET registrada cuyo parámetro no declara ser de aviso ni
    de navegación. Lo usa la prueba de censo: un parámetro nuevo sin marca la pone roja."""
    return [(r.path, n) for r in app.router.routes
            if "GET" in (getattr(r, "methods", None) or ())
            for n, m in marcas_de(r).items() if not isinstance(m.get("aviso"), bool)]


def avisos_de(ruta) -> dict[str, str | None]:
    """{parámetro de aviso: parámetro de navegación que además escoge, o None}."""
    return {n: m.get("elige") for n, m in marcas_de(ruta).items() if m.get("aviso") is True}


def _cabecera(scope, nombre: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == nombre:
            return v.decode("latin-1")
    return None


def _cookie_de(scope) -> str | None:
    crudo = _cabecera(scope, b"cookie")
    return cookie_parser(crudo).get(COOKIE) if crudo else None


def _poner_cookie(scope, recibos: list[str]) -> bytes:
    partes = [f"{COOKIE}={'~'.join(recibos)}" if recibos else f"{COOKIE}=",
              "Path=/", "HttpOnly", "SameSite=Lax",
              f"Max-Age={VIDA_RECIBO if recibos else 0}"]
    if scope.get("scheme") == "https":
        partes.append("Secure")
    return "; ".join(partes).encode("latin-1")


def consulta_sin_avisos(app, ruta: str, pares) -> list[tuple[str, str]]:
    """Los parámetros de `pares` que NO son de aviso en `ruta` (para armar un enlace o un campo
    oculto que vuelva a la misma pantalla sin heredar el aviso de esta visita)."""
    try:
        avisos = avisos_de(_ruta_get(app, ruta))
        return [(k, v) for k, v in pares if k not in avisos]
    except Exception:                      # una página no se cae por esto: sin parámetros, que es lo seguro
        log.warning("Avisos: no se pudo limpiar la dirección de %s", ruta, exc_info=True)
        return []


def _valores_que_trajo_la_peticion(scope, cuerpo: bytes) -> list[str]:
    """Cada VALOR que vino de fuera en un POST, ya decodificado: los de los campos del
    formulario, los de la dirección y los de las cabeceras (menos `Referer`, que el navegador
    manda solo con la dirección de la página anterior, y `Cookie`). Un formulario con partes
    (`multipart`) se toma entero como un solo valor. Se miran los VALORES y no los nombres de
    los campos: un campo que se llama como un aviso (`id=1` al restaurar) es el argumento de la
    acción, y la ruta lo repite a propósito; lo que no puede repetir es un destino que otro
    escribió, que lleva un aviso DENTRO de un valor (`volver=/movimientos?guardados=1`)."""
    tipo = next((v.decode("latin-1").lower() for k, v in scope.get("headers", []) if k == b"content-type"), "")
    texto = cuerpo.decode("utf-8", "replace")
    valores = [texto] if "multipart" in tipo else [v for _, v in parse_qsl(texto, keep_blank_values=True)]
    valores += [v for _, v in parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)]
    valores += [v.decode("latin-1") for k, v in scope.get("headers", []) if k not in (b"referer", b"cookie")]
    return valores + [unquote_plus(v) for v in valores]       # y escrito dos veces codificado


def _viene_de_fuera(k: str, v: str, valores: list[str]) -> bool:
    patron = re.compile(r"(?<![\w%])" + re.escape(f"{k}={v}") + r"(?![\w%])")
    return any(patron.search(x) for x in valores)


class PuertaDeAvisos:
    """Middleware ASGI: ver el texto del módulo."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        aplicacion = scope.get("app")
        if aplicacion is None:
            return await self.app(scope, receive, send)
        if scope["method"] == "GET":
            try:
                scope, send = self._al_entrar(aplicacion, scope, send)
            except Exception:
                log.warning("Avisos: falló la puerta al entrar; la petición sale sin avisos", exc_info=True)
                scope = self._sin_ningun_aviso(aplicacion, scope)
        else:
            cuerpo = bytearray()
            truncado = []

            original = receive

            async def recibir():
                mensaje = await original()
                if mensaje.get("type") == "http.request":
                    trozo = mensaje.get("body", b"")
                    if len(cuerpo) + len(trozo) <= LIMITE_DE_CUERPO:
                        cuerpo.extend(trozo)
                    else:
                        truncado.append(True)
                return mensaje
            receive = recibir
            send = self._al_salir(aplicacion, scope, send, cuerpo, truncado)
        return await self.app(scope, receive, send)

    # ── GET: sin recibo, los avisos no existen ─────────────────────────────
    def _sin_ningun_aviso(self, aplicacion, scope):
        """Lo que se hace si la puerta falla al entrar: la petición llega SIN avisos (y, si ni
        eso se puede calcular, sin dirección)."""
        try:
            pares = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            limpios = consulta_sin_avisos(aplicacion, scope["path"], pares)
            return dict(scope, query_string=urlencode(limpios).encode("latin-1"))
        except Exception:
            return dict(scope, query_string=b"")

    def _al_entrar(self, aplicacion, scope, send):
        ruta = _ruta_get(aplicacion, scope["path"])
        avisos = avisos_de(ruta)
        if not avisos:
            return scope, send
        pares = parse_qsl(scope.get("query_string", b"").decode("latin-1"),
                          keep_blank_values=True)
        if not any(k in avisos for k, _ in pares):
            return scope, send
        ahora = time.time()
        recibos = _recibos_de(_cookie_de(scope), ahora)
        clave = clave_de(scope["path"], pares, avisos)
        for r in recibos:
            vence = int(r.partition(".")[0])
            if hmac.compare_digest(r.encode("ascii"), _recibo(clave, vence).encode("ascii")):
                # Recibo bueno: pasa tal cual, y se gasta UNO (dos iguales en el mismo segundo,
                # de dos pestañas, son dos avisos: cada uno se gasta una vez).
                restantes = list(recibos)
                restantes.remove(r)

                async def enviar(mensaje, _s=send, _rest=restantes):
                    if mensaje["type"] == "http.response.start":
                        # `no-store`: «atrás» no resucita del caché del navegador una página con un
                        # aviso que ya se gastó; vuelve a pedirla, y sin recibo sale sin aviso.
                        cabeceras = [(k, v) for k, v in mensaje.get("headers", []) if k.lower() != b"cache-control"]
                        cabeceras.append((b"cache-control", b"no-store"))
                        try:
                            cabeceras.append((b"set-cookie", _poner_cookie(scope, _rest)))
                        except Exception:
                            log.warning("Avisos: no se pudo gastar el recibo en la cookie", exc_info=True)
                        mensaje = dict(mensaje, headers=cabeceras)
                    await _s(mensaje)
                return scope, enviar
        # Sin recibo: la petición llega sin los avisos (y los que además
        # escogen algo conservan esa parte).
        limpios = [(k, v) for k, v in pares if k not in avisos]
        presentes = {k for k, _ in limpios}
        for k, v in pares:
            destino = avisos.get(k)
            if destino and v.isdigit() and destino not in presentes:
                limpios.append((destino, v))
                presentes.add(destino)
        return dict(scope, query_string=urlencode(limpios).encode("latin-1")), send

    # ── POST (y cualquier otro): la redirección con aviso deja su recibo ───
    def _al_salir(self, aplicacion, scope, send, cuerpo, truncado):
        async def enviar(mensaje):
            if mensaje["type"] == "http.response.start" and 300 <= mensaje["status"] < 400:
                try:
                    mensaje = self._con_recibo(aplicacion, scope, mensaje, bytes(cuerpo), bool(truncado))
                except Exception:
                    # lo que la ruta ya hizo, hecho está: la respuesta sale como la dio, sin recibo
                    log.warning("Avisos: falló la puerta al salir; la respuesta sale sin recibo", exc_info=True)
            await send(mensaje)
        return enviar

    def _con_recibo(self, aplicacion, scope, mensaje, cuerpo, truncado):
        destino = next((v.decode("latin-1") for k, v in mensaje.get("headers", [])
                        if k.lower() == b"location"), None)
        if not destino:
            return mensaje
        partes = urlsplit(destino)
        if partes.scheme or partes.netloc or not partes.path.startswith("/"):
            return mensaje              # solo direcciones de este mismo sitio
        avisos = avisos_de(_ruta_get(aplicacion, partes.path))
        pares = parse_qsl(partes.query, keep_blank_values=True)
        if not any(k in avisos for k, _ in pares):
            return mensaje
        # Lo que la petición trajo no es de esta acción: se quita del destino y no se firma.
        texto = _valores_que_trajo_la_peticion(scope, cuerpo)
        quedan, quitado = [], False
        for segmento in partes.query.split("&"):
            par = parse_qsl(segmento, keep_blank_values=True)
            if par and par[0][0] in avisos and (truncado or _viene_de_fuera(par[0][0], par[0][1], texto)):
                quitado = True
                continue
            quedan.append(segmento)
        headers = mensaje.get("headers", [])
        if quitado:
            nuevo = partes.path + ("?" + "&".join(quedan) if quedan else "") + (
                "#" + partes.fragment if partes.fragment else "")
            headers = [(k, nuevo.encode("latin-1") if k.lower() == b"location" else v) for k, v in headers]
            pares = parse_qsl("&".join(quedan), keep_blank_values=True)
            if not any(k in avisos for k, _ in pares):
                return dict(mensaje, headers=headers)
        ahora = time.time()
        vence = int(ahora) + VIDA_RECIBO
        recibos = _recibos_de(_cookie_de(scope), ahora) + [
            _recibo(clave_de(partes.path, pares, avisos), vence)]
        return dict(mensaje, headers=[
            *headers,
            (b"set-cookie", _poner_cookie(scope, recibos[-MAXIMO_DE_RECIBOS:]))])
