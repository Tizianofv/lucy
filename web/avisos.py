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

FRONTERA (lo que esta puerta NO ve): una ruta que llame a su función a mano en
vez de pasar por la aplicación (las pruebas lo hacen) no pasa por la puerta; solo
las respuestas de un método que no sea GET dejan recibo (una ruta GET que redirija
a una dirección con avisos no lo deja; hoy ninguna lo hace); un
aviso que viaje por otro canal que un parámetro del GET (una cabecera, una
cookie propia, el cuerpo de un POST que se pinta sin redirigir) no se declara
aquí; el recibo se firma con lo que la ruta puso en SU redirección, así que una
ruta que copie a su redirección un parámetro de aviso que vino del usuario le da
recibo a un texto inventado (hoy ninguna lo hace: `volver` se arma con la
petición ya limpia). Un parámetro mal marcado como navegación sale tal cual.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Annotated, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Query
from starlette.requests import cookie_parser
from starlette.routing import Match

import config

T = TypeVar("T")

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
    for r in (valor or "").split("~"):
        vence, _, firma = r.partition(".")
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
    `{"aviso": True|False, "elige": …}`; un parámetro sin marca da `{}`."""
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


class PuertaDeAvisos:
    """Middleware ASGI: ver el texto del módulo."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        aplicacion = scope.get("app")
        if scope["method"] == "GET" and aplicacion is not None:
            scope, send = self._al_entrar(aplicacion, scope, send)
        elif aplicacion is not None:
            send = self._al_salir(aplicacion, scope, send)
        return await self.app(scope, receive, send)

    # ── GET: sin recibo, los avisos no existen ─────────────────────────────
    def _al_entrar(self, aplicacion, scope, send):
        ruta = _ruta_get(aplicacion, scope["path"])
        avisos = avisos_de(ruta) if ruta is not None else {}
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
            vence = r.partition(".")[0]
            if hmac.compare_digest(r, _recibo(clave, int(vence))):
                # Recibo bueno: pasa tal cual, y se gasta.
                restantes = [x for x in recibos if x != r]

                async def enviar(mensaje, _s=send, _rest=restantes):
                    if mensaje["type"] == "http.response.start":
                        # `no-store`: «atrás» no resucita del caché del navegador una página con un
                        # aviso que ya se gastó; vuelve a pedirla, y sin recibo sale sin aviso.
                        mensaje = dict(mensaje, headers=[
                            *[(k, v) for k, v in mensaje.get("headers", []) if k.lower() != b"cache-control"],
                            (b"cache-control", b"no-store"),
                            (b"set-cookie", _poner_cookie(scope, _rest))])
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
    def _al_salir(self, aplicacion, scope, send):
        async def enviar(mensaje):
            if mensaje["type"] == "http.response.start" and 300 <= mensaje["status"] < 400:
                mensaje = self._con_recibo(aplicacion, scope, mensaje)
            await send(mensaje)
        return enviar

    def _con_recibo(self, aplicacion, scope, mensaje):
        destino = next((v.decode("latin-1") for k, v in mensaje.get("headers", [])
                        if k.lower() == b"location"), None)
        if not destino:
            return mensaje
        partes = urlsplit(destino)
        if partes.scheme or partes.netloc or not partes.path.startswith("/"):
            return mensaje              # solo direcciones de este mismo sitio
        ruta = _ruta_get(aplicacion, partes.path)
        avisos = avisos_de(ruta) if ruta is not None else {}
        pares = parse_qsl(partes.query, keep_blank_values=True)
        if not any(k in avisos for k, _ in pares):
            return mensaje
        ahora = time.time()
        vence = int(ahora) + VIDA_RECIBO
        recibos = _recibos_de(_cookie_de(scope), ahora) + [
            _recibo(clave_de(partes.path, pares, avisos), vence)]
        return dict(mensaje, headers=[
            *mensaje.get("headers", []),
            (b"set-cookie", _poner_cookie(scope, recibos[-MAXIMO_DE_RECIBOS:]))])
