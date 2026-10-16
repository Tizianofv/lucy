"""El cliente de Drive (Cotizaciones, pieza 7): `drive.py` y `tools/autorizar_drive.py`.

QUÉ SE PRUEBA, en una frase: que Lucy le hable a Drive solo con el permiso
`drive.file`, solo a los dos servidores de Google, sin seguir redirecciones, sin
dejar una credencial en ningún sitio que no sea el pedido del token, y que cada
fallo suba como una de las cuatro excepciones con su frase fija.

CÓMO. El camino es el DE PRODUCCIÓN con un solo cambio: el cliente HTTP es el
de verdad (`httpx.AsyncClient`) y lo que se reemplaza es el TRANSPORTE
(`httpx.MockTransport`), igual que `tests/test_noco_lectura.py`. La biblioteca
arma el pedido, sigue (o no) las redirecciones y aplica el tiempo; la prueba mira
el pedido que sale.

DE DÓNDE SALE LO QUE EL DOBLE AFIRMA DE DRIVE v3, y su frontera. De la
documentación pública de Google (Drive API v3: «files.create», «files.list»,
«files.update», «Upload file data», «Search for files», «Resolve errors» y el
flujo OAuth de aplicaciones instaladas), leída de memoria al escribir esto:
ningún pedido real salió, y NO hay una captura de una respuesta real. Concretamente
el doble afirma (1) que crear/subir contesta `{"id": "..."}` con `fields=id`;
(2) que listar contesta `{"files": [{"id","name"}]}`; (3) que el servidor de
tokens contesta `{"access_token", "expires_in"}` y, con un refresh token vencido,
400; (4) que un 403 por límite de pedidos trae
`{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}`. Cuando la sala corra
`tools/autorizar_drive.py` contra Google y capture una respuesta de cada una, lo
que se reemplaza son estas formas. Las pruebas de abajo deciden con lo que el
módulo PIDE y con la CLASE de excepción que sube, no con el detalle del cuerpo,
salvo el 403 por límite (punto 4), que sí depende de esa forma.

Las credenciales de aquí son falsas y el repositorio es público: ninguna es real.

Correr:  python3 -m pytest tests/test_drive.py -q
"""
from __future__ import annotations

import ast
import asyncio
import email
import importlib.util
import inspect
import json
import logging
import os
import pathlib
import stat
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request

import pytest

import drive

RAIZ = pathlib.Path(__file__).resolve().parent.parent

CLIENTE = "cliente-falso-123"
SECRETO = "secreto-falso-456"
REFRESCO = "refresco-falso-789"
ACCESO = "acceso-falso-abc"
CODIGO = "codigo-falso-def"
SECRETOS = (SECRETO, REFRESCO, ACCESO, CODIGO)
PDF = b"%PDF-1.4\n\xff\xfe binario \x00 con \r\n--lucy-- dentro\n%%EOF"
SERVIDORES = {"www.googleapis.com", "oauth2.googleapis.com"}


def _httpx_de_verdad():
    """El `httpx` DE VERDAD (varias pruebas pisan `sys.modules["httpx"]` con uno
    falso al importarse; ver `tests/test_noco_lectura.py::_httpx_de_verdad`)."""
    modulo = drive.httpx
    if inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                        ).endswith("httpx/__init__.py"):
        return modulo
    import starlette.testclient as testclient
    modulo = getattr(testclient, "httpx", None)
    assert inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                            ).endswith("httpx/__init__.py"), (
        "no encontré el httpx de verdad: sin él esta prueba no puede decir "
        "nada sobre lo que el módulo pide por la red.")
    return modulo


HTTPX = _httpx_de_verdad()
_CLIENTE_REAL = HTTPX.AsyncClient


class _ConDoble:
    """El módulo httpx de verdad con UNA pieza cambiada: `AsyncClient`."""

    def __init__(self, real, cliente):
        self._real = real
        self.AsyncClient = cliente

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)


def _R(estado=200, **kw):
    return HTTPX.Response(estado, **kw)


class Google:
    """Un Google de mentira: cliente HTTP de verdad, transporte de mentira.

    `api` reemplaza la respuesta de todo pedido que NO sea al servidor de
    tokens; `token` la del servidor de tokens. Cada uno es una función
    `pedido -> Response` (o una corrutina) y puede levantar una excepción.
    """

    def __init__(self):
        self.pedidos: list = []
        self.api = None
        self.token = None

    def cliente(self, *args, **kwargs):
        kwargs["transport"] = HTTPX.MockTransport(self._manejar)
        return _CLIENTE_REAL(*args, **kwargs)

    async def _manejar(self, p):
        self.pedidos.append(p)
        if p.url.host == "oauth2.googleapis.com":
            if self.token is not None:
                r = self.token(p)
                return await r if inspect.isawaitable(r) else r
            return _R(200, json={"access_token": ACCESO, "expires_in": 3599,
                                 "token_type": "Bearer"})
        if self.api is not None:
            r = self.api(p)
            return await r if inspect.isawaitable(r) else r
        return _por_defecto(p)

    def de_api(self):
        return [p for p in self.pedidos if p.url.host != "oauth2.googleapis.com"]


def _por_defecto(p):
    ruta, m = p.url.path, p.method
    if m == "GET" and ruta == "/drive/v3/files":
        return _R(200, json={"files": [{"id": "ID1", "name": "COT"}]})
    if m == "POST" and ruta == "/drive/v3/files":
        return _R(200, json={"id": "CARPETA1"})
    if m == "PATCH" and ruta.startswith("/drive/v3/files/"):
        return _R(200, json={"id": ruta.rsplit("/", 1)[1]})
    if m == "POST" and ruta == "/upload/drive/v3/files":
        return _R(200, json={"id": "ARCHIVO1"})
    if m == "PATCH" and ruta.startswith("/upload/drive/v3/files/"):
        return _R(200, json={"id": ruta.rsplit("/", 1)[1]})
    return _R(418, json={"msg": "ruta que el doble no conoce"})


_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _vigilar_toda_la_red(monkeypatch, violaciones):
    """UNA SOLA PUERTA para lo que ve (httpx y `socket`), decidida por lo que pasa y no por cómo está escrito el
    código: durante la prueba, cualquier salida a la red que NO sea el cliente
    sancionado (el `AsyncClient` de `drive.httpx`, con transporte de mentira) se
    anota y revienta. Lo que mira: los transportes REALES de httpx (síncrono y
    asíncrono, así que `httpx.get`, `httpx.Client(...)` o un `AsyncClient` sacado
    de otro sitio) y `socket` (resolver un nombre o conectar a algo que no sea
    local, que es lo que haría `urllib`, `requests` o un `subprocess` en el mismo
    proceso). FRONTERA EXACTA: esta guarda ve `httpx` (sus dos transportes
    reales) y el módulo `socket` de Python (`getaddrinfo` y `connect`). NO ve el
    módulo C `_socket` (`_socket.getaddrinfo` es otro objeto), ni `ctypes`, ni un
    proceso hijo. Medido por el testigo: resolver con `_socket.getaddrinfo`
    dentro de una operación deja la suite verde. No se agregan más vías: la
    serie de formas no se acaba, y la frase promete solo lo que se lista aquí.
    """
    import socket

    def anotar(que):
        def f(*a, **k):
            violaciones.append(que)
            raise OSError("salida a la red que no es la puerta de drive.py")
        return f

    monkeypatch.setattr(HTTPX.HTTPTransport, "handle_request",
                        anotar("transporte httpx sincrono real"))
    monkeypatch.setattr(HTTPX.AsyncHTTPTransport, "handle_async_request",
                        anotar("transporte httpx asincrono real"))
    real_gai, real_connect = socket.getaddrinfo, socket.socket.connect

    def gai(host, *a, **k):
        if host not in _LOCAL:
            violaciones.append(f"socket.getaddrinfo({host!r})")
            raise OSError("salida a la red que no es la puerta de drive.py")
        return real_gai(host, *a, **k)

    def conectar(self, direccion, *a, **k):
        if not (isinstance(direccion, tuple) and direccion[0] in _LOCAL):
            violaciones.append(f"socket.connect({direccion!r})")
            raise OSError("salida a la red que no es la puerta de drive.py")
        return real_connect(self, direccion, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket.socket, "connect", conectar)


@pytest.fixture
def google(monkeypatch):
    g = Google()
    violaciones: list = []
    _vigilar_toda_la_red(monkeypatch, violaciones)
    monkeypatch.setenv("LUCY_DRIVE_CLIENT_ID", CLIENTE)
    monkeypatch.setenv("LUCY_DRIVE_CLIENT_SECRET", SECRETO)
    monkeypatch.setenv("LUCY_DRIVE_REFRESH_TOKEN", REFRESCO)
    monkeypatch.setattr(drive, "httpx", _ConDoble(HTTPX, g.cliente))
    yield g
    assert violaciones == [], (
        f"algo salió a la red por una puerta que no es la de drive.py: {violaciones}")


# Cada función pública del módulo, llamada de las formas que existen. La lista
# de PÚBLICAS sale del módulo (`_publicas`) y una prueba exige que esta tabla
# las cubra todas: una función nueva que nadie ejercite se pone roja aquí.
_OPERACIONES = [
    ("crear_carpeta", "crear",
     lambda: drive.crear_carpeta("Proyecto X", "PADRE1")),
    ("buscar_en", "buscar",
     lambda: drive.buscar_en("PADRE1", "COT-2026-001.pdf")),
    ("renombrar", "renombrar", lambda: drive.renombrar("ARCH1", "Nuevo")),
    ("subir_pdf", "subir nuevo",
     lambda: drive.subir_pdf("PADRE1", "COT-2026-001.pdf", PDF)),
    ("subir_pdf", "subir reemplazo",
     lambda: drive.subir_pdf("PADRE1", "COT-2026-001.pdf", PDF,
                             archivo_id="ARCH1")),
]
_IDS_OP = [o[1] for o in _OPERACIONES]


def _publicas():
    return {n for n, f in inspect.getmembers(drive, inspect.iscoroutinefunction)
            if not n.startswith("_") and f.__module__ == drive.__name__}


def test_la_tabla_de_operaciones_cubre_todas_las_publicas():
    assert _publicas() == {o[0] for o in _OPERACIONES}, (
        "hay una función pública de drive.py que esta prueba no ejercita")


# ═══════════════════════════════════════════════════════════════════════
# Sin configuración no sale ningún pedido
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("falta", drive.VARIABLES)
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_sin_una_variable_no_sale_ningun_pedido(google, monkeypatch,
                                                      falta, op):
    monkeypatch.delenv(falta)
    with pytest.raises(drive.DriveSinConfigurar):
        await op[2]()
    assert google.pedidos == []


@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_una_variable_en_blanco_cuenta_como_falta(google, monkeypatch, op):
    monkeypatch.setenv("LUCY_DRIVE_REFRESH_TOKEN", "   ")
    with pytest.raises(drive.DriveSinConfigurar):
        await op[2]()
    assert google.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# Cada fallo es una de las cuatro excepciones, en TODAS las operaciones
# ═══════════════════════════════════════════════════════════════════════

def _devuelve(estado, **kw):
    return lambda p: _R(estado, **kw)


def _limite(p):
    return _R(403, json={"error": {"code": 403, "errors": [
        {"reason": "rateLimitExceeded", "domain": "usageLimits"}]}})


def _sin_red(p):
    raise HTTPX.ConnectError("sin red", request=p)


async def _lento(p):
    await asyncio.sleep(0.5)
    return _R(200, json={"id": "TARDE"})


_ESCENARIOS_API = [
    ("401", _devuelve(401, json={}), drive.DriveSinPermiso),
    ("403 sin permiso", _devuelve(403, json={"error": {"errors": [
        {"reason": "forbidden"}]}}), drive.DriveSinPermiso),
    ("403 sin cuerpo", _devuelve(403, text="no"), drive.DriveSinPermiso),
    ("403 por limite", _limite, drive.DriveNoContesta),
    ("404", _devuelve(404, json={}), drive.DriveNoEsta),
    ("429", _devuelve(429, json={}), drive.DriveNoContesta),
    ("500", _devuelve(500, json={}), drive.DriveNoContesta),
    ("503", _devuelve(503, text="caido"), drive.DriveNoContesta),
    ("400", _devuelve(400, json={}), drive.DriveNoContesta),
    ("200 que no es JSON", _devuelve(200, text="hola"), drive.DriveNoContesta),
    ("200 sin lo esperado", _devuelve(200, json={}), drive.DriveNoContesta),
    ("200 con una lista", _devuelve(200, json=[1]), drive.DriveNoContesta),
    ("red caida", _sin_red, drive.DriveNoContesta),
    ("url rechazada por httpx", lambda p: (_ for _ in ()).throw(
        HTTPX.InvalidURL("mala")), drive.DriveNoContesta),
    ("flujo ya consumido", lambda p: (_ for _ in ()).throw(
        HTTPX.StreamConsumed()), drive.DriveNoContesta),
]


@pytest.mark.parametrize("escenario", _ESCENARIOS_API, ids=[e[0] for e in _ESCENARIOS_API])
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_cada_fallo_de_drive_sube_su_excepcion(google, op, escenario):
    google.api = escenario[1]
    with pytest.raises(escenario[2]) as e:
        await op[2]()
    assert type(e.value) is escenario[2]
    assert str(e.value) == escenario[2].causa


@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_el_tiempo_vencido_es_no_contesta(google, monkeypatch, op):
    """El transporte de mentira se queda dormido más que `TIEMPO_LIMITE`: lo
    corta el `wait_for` de `_pedir` (el tiempo de httpx no corre sobre un
    transporte de mentira, así que esta es la prueba de ese tope)."""
    monkeypatch.setattr(drive, "TIEMPO_LIMITE", 0.05)
    google.api = _lento
    with pytest.raises(drive.DriveNoContesta):
        await op[2]()


@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_el_tope_de_tiempo_es_total_y_cuenta_el_token(google, monkeypatch, op):
    """El token tarda 0.04 s y Drive 0.04 s; cada uno cabe en el tope (0.06 s)
    pero juntos no: el diseño pide 15 s EN TOTAL por operación."""
    monkeypatch.setattr(drive, "TIEMPO_LIMITE", 0.06)

    async def tarda(p):
        await asyncio.sleep(0.04)
        if p.url.host == "oauth2.googleapis.com":
            return _R(200, json={"access_token": ACCESO})
        return _por_defecto(p)

    google.token = tarda
    google.api = tarda
    with pytest.raises(drive.DriveNoContesta):
        await op[2]()


_ESCENARIOS_TOKEN = [
    ("400 refresh vencido", _devuelve(400, json={"error": "invalid_grant"}),
     drive.DriveSinPermiso),
    ("401", _devuelve(401, json={"error": "invalid_client"}),
     drive.DriveSinPermiso),
    ("500", _devuelve(500, json={}), drive.DriveNoContesta),
    ("200 sin access_token", _devuelve(200, json={"expires_in": 5}),
     drive.DriveNoContesta),
    ("200 con access_token vacio", _devuelve(200, json={"access_token": ""}),
     drive.DriveNoContesta),
    ("red caida", _sin_red, drive.DriveNoContesta),
]


@pytest.mark.parametrize("escenario", _ESCENARIOS_TOKEN, ids=[e[0] for e in _ESCENARIOS_TOKEN])
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_si_el_token_falla_no_sale_el_pedido_de_drive(google, op, escenario):
    google.token = escenario[1]
    with pytest.raises(escenario[2]):
        await op[2]()
    assert google.de_api() == []


# ═══════════════════════════════════════════════════════════════════════
# Una redirección NO se sigue
# ═══════════════════════════════════════════════════════════════════════

def _redirige(estado):
    def f(p):
        return _R(estado, headers={"Location": "https://otro.invalid/robar"})
    return f


@pytest.mark.parametrize("estado", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_un_3xx_no_se_sigue(google, op, estado):
    google.api = _redirige(estado)
    with pytest.raises(drive.DriveNoContesta):
        await op[2]()
    assert [p.url.host for p in google.pedidos] == [
        "oauth2.googleapis.com", "www.googleapis.com"], (
        "salió un pedido de más: se siguió la redirección")


@pytest.mark.parametrize("estado", [302, 307])
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_un_3xx_del_servidor_de_tokens_no_se_sigue(google, op, estado):
    google.token = _redirige(estado)
    with pytest.raises(drive.DriveNoContesta):
        await op[2]()
    assert [p.url.host for p in google.pedidos] == ["oauth2.googleapis.com"]


# ═══════════════════════════════════════════════════════════════════════
# Las credenciales no van a ningún sitio que no sea el pedido del token
# ═══════════════════════════════════════════════════════════════════════

def _con_secretos_adentro(p):
    """Lo peor que podría pasar: Google (o la red) repite las credenciales en
    el cuerpo de un error o en el mensaje de una excepción."""
    raise HTTPX.ConnectError(" ".join(SECRETOS), request=p)


def _eco(estado):
    return lambda p: _R(estado, json={"error": {"message": " ".join(SECRETOS),
                                                "errors": []}})


_FALLOS_CON_ECO = (
    [(f"api {e[0]}", e[1], None) for e in _ESCENARIOS_API]
    + [("api eco 401", _eco(401), None), ("api eco 404", _eco(404), None),
       ("api eco 500", _eco(500), None), ("api eco 403", _eco(403), None),
       ("api excepcion con secretos", _con_secretos_adentro, None)]
    + [(f"token {e[0]}", None, e[1]) for e in _ESCENARIOS_TOKEN]
    + [("token eco 400", None, _eco(400)), ("token eco 500", None, _eco(500)),
       ("token excepcion con secretos", None, _con_secretos_adentro)])


def _no_hay_secretos(texto, donde):
    for s in SECRETOS:
        assert s not in texto, f"{donde}: aparece una credencial falsa"


@pytest.mark.parametrize("fallo", _FALLOS_CON_ECO, ids=[f[0] for f in _FALLOS_CON_ECO])
@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_ninguna_credencial_llega_a_un_registro_ni_a_una_excepcion(
        google, caplog, op, fallo):
    google.api, google.token = fallo[1], fallo[2]
    caplog.set_level(logging.DEBUG)
    with pytest.raises(drive.DriveError) as e:
        await op[2]()
    _no_hay_secretos(str(e.value) + repr(e.value), "texto de la excepcion")
    _no_hay_secretos("".join(traceback.format_exception(e.value)), "traceback")
    _no_hay_secretos(caplog.text, "registros")
    assert e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__, (
        "la causa original (que puede llevar texto de Google) quedó encadenada")


@pytest.mark.parametrize("op", _OPERACIONES, ids=_IDS_OP)
async def test_en_un_camino_bueno_las_credenciales_solo_van_donde_deben(
        google, caplog, op):
    caplog.set_level(logging.DEBUG)
    await op[2]()
    _no_hay_secretos(caplog.text, "registros")
    assert google.pedidos, "no salió ningún pedido"
    for p in google.pedidos:
        assert p.url.host in SERVIDORES and p.url.scheme == "https"
        url = str(p.url)
        _no_hay_secretos(url, "la URL")
        if p.url.host == "oauth2.googleapis.com":
            assert "authorization" not in p.headers
            cuerpo = urllib.parse.parse_qs(p.content.decode())
            assert cuerpo == {"grant_type": ["refresh_token"],
                              "client_id": [CLIENTE],
                              "client_secret": [SECRETO],
                              "refresh_token": [REFRESCO]}
        else:
            assert p.headers["authorization"] == "Bearer " + ACCESO
            for s in (SECRETO, REFRESCO, CODIGO):
                assert s.encode() not in p.content
                assert s not in str(dict(p.headers))
    # Un token por pedido, y el pedido de Drive va detrás de él.
    assert [p.url.host for p in google.pedidos] == [
        "oauth2.googleapis.com", "www.googleapis.com"]


@pytest.mark.parametrize("id_malo", ["ABC\n", "ABC\r\n", "\nABC", "a/b", "", 5, None])
@pytest.mark.parametrize("op", [o for o in _OPERACIONES if o[0] != "buscar_en"],
                         ids=[o[1] for o in _OPERACIONES if o[0] != "buscar_en"])
async def test_un_id_malo_en_la_respuesta_de_google_no_se_acepta(google, op, id_malo):
    """El hermano de la validación de entrada: el id que CONTESTA Google también
    pasa por `fullmatch` (un `ABC\\n` se aceptaría con `match`)."""
    google.api = _devuelve(200, json={"id": id_malo})
    with pytest.raises(drive.DriveNoContesta):
        await op[2]()
    assert len(google.de_api()) == 1


async def test_buscar_descarta_un_id_con_salto_de_linea_en_la_respuesta(google):
    google.api = _devuelve(200, json={"files": [
        {"id": "ABC\n", "name": "a"}, {"id": "BUENO", "name": "a"}]})
    assert await drive.buscar_en("PADRE1", "a") == [{"id": "BUENO", "nombre": "a"}]


def test_las_variables_leidas_son_las_declaradas():
    """`_credenciales` lee tres claves escritas a mano (la guarda del repo exige
    claves constantes); este es el candado de que son las de `VARIABLES`, y que
    la cuarta (`carpeta_proyectos`, pieza 8) es la de `VARIABLE_CARPETA_PROYECTOS`."""
    arbol = ast.parse((RAIZ / "drive.py").read_text(encoding="utf-8"))
    leidas = [n.args[0].value for n in ast.walk(arbol)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "get" and n.args
              and isinstance(n.args[0], ast.Constant)
              and isinstance(n.args[0].value, str)
              and n.args[0].value.startswith("LUCY_DRIVE_")]
    assert len(leidas) == 4 and set(leidas) == set(drive.VARIABLES) | {drive.VARIABLE_CARPETA_PROYECTOS}


def test_el_unico_permiso_que_se_pide_es_drive_file():
    assert drive.ALCANCE == "https://www.googleapis.com/auth/drive.file"


def test_ningun_valor_de_credencial_esta_escrito_en_el_codigo():
    """Lo que SÍ ve: el texto de los dos archivos no trae una cadena con la
    forma de un secreto de Google (`GOCSPX-…`, `1//…`, `ya29.…`). Lo que NO ve:
    un valor con otra forma."""
    for ruta in (RAIZ / "drive.py", RAIZ / "tools" / "autorizar_drive.py"):
        texto = ruta.read_text(encoding="utf-8")
        for marca in ("GOCSPX-", "ya29.", '"1//', "apps.googleusercontent.com"):
            assert marca not in texto, f"{ruta.name}: {marca}"


def test_solo_pedir_crea_un_cliente_http_y_no_sigue_redirecciones():
    """Frontera dicha: lee el TEXTO de `drive.py` (la conducta la vigilan las
    pruebas de redirecciones de arriba y `_vigilar_toda_la_red`, que reventan
    cualquier otra salida a la red). Exige que `AsyncClient` aparezca una sola
    vez, dentro de `_hacer`, con `follow_redirects=False`."""
    arbol = ast.parse((RAIZ / "drive.py").read_text(encoding="utf-8"))
    sitios = []
    for fn in ast.walk(arbol):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr == "AsyncClient":
                    sitios.append((fn.name, n))
    assert [s[0] for s in sitios] == ["_hacer"]
    llamada = next(n for n in ast.walk(arbol) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "AsyncClient")
    claves = {k.arg: k.value for k in llamada.keywords}
    assert isinstance(claves["follow_redirects"], ast.Constant)
    assert claves["follow_redirects"].value is False


# ═══════════════════════════════════════════════════════════════════════
# Lo que cada operación PIDE
# ═══════════════════════════════════════════════════════════════════════

async def test_crear_carpeta_pide_el_nombre_el_tipo_y_el_padre(google):
    assert await drive.crear_carpeta("Casa de Ana", "PADRE1") == "CARPETA1"
    p = google.de_api()[0]
    assert (p.method, p.url.path) == ("POST", "/drive/v3/files")
    assert json.loads(p.content) == {
        "name": "Casa de Ana", "parents": ["PADRE1"],
        "mimeType": "application/vnd.google-apps.folder"}
    assert p.url.params["fields"] == "id"


async def test_buscar_pide_el_padre_el_nombre_exacto_y_sin_papelera(google):
    hallados = await drive.buscar_en("PADRE1", "COT-2026-001.pdf")
    assert hallados == [{"id": "ID1", "nombre": "COT"}]
    p = google.de_api()[0]
    assert (p.method, p.url.path) == ("GET", "/drive/v3/files")
    assert p.url.params["q"] == (
        "'PADRE1' in parents and name = 'COT-2026-001.pdf' and trashed = false")


async def test_buscar_con_comilla_y_barra_en_el_nombre_las_escapa(google):
    await drive.buscar_en("PADRE1", "O'Brien \\ ' or name != '")
    q = google.de_api()[0].url.params["q"]
    assert q == ("'PADRE1' in parents and name = "
                 "'O\\'Brien \\\\ \\' or name != \\'' and trashed = false")


async def test_buscar_sin_nadie_devuelve_lista_vacia_y_no_una_excepcion(google):
    google.api = _devuelve(200, json={"files": []})
    assert await drive.buscar_en("PADRE1", "x") == []


async def test_buscar_descarta_filas_que_no_sirven(google):
    google.api = _devuelve(200, json={"files": [
        {"id": "BUENO", "name": "a"}, {"id": "../x", "name": "b"},
        {"id": "SINNOMBRE"}, "texto", {"id": 5, "name": "c"}]})
    assert await drive.buscar_en("PADRE1", "a") == [{"id": "BUENO", "nombre": "a"}]


_IDS_MALOS = ["x' or '1'='1", "../etc", "a b", "", 5, "a/b", "id?fields=*",
              "ABC\n", "ABC\n\n", "\nABC", "ABC\r\n"]


@pytest.mark.parametrize("malo", _IDS_MALOS + [None])
@pytest.mark.parametrize("op", ["crear", "buscar", "renombrar", "subir"])
async def test_un_id_que_no_puede_ser_de_drive_no_sale_a_la_red(google, malo, op):
    llamada = {
        "crear": lambda: drive.crear_carpeta("n", malo),
        "buscar": lambda: drive.buscar_en(malo, "n"),
        "renombrar": lambda: drive.renombrar(malo, "n"),
        "subir": lambda: drive.subir_pdf(malo, "n.pdf", PDF),
    }[op]
    with pytest.raises(drive.DriveNoEsta):
        await llamada()
    assert google.pedidos == []


@pytest.mark.parametrize("malo", _IDS_MALOS)
async def test_un_archivo_id_que_no_puede_ser_de_drive_no_sale_a_la_red(google, malo):
    with pytest.raises(drive.DriveNoEsta):
        await drive.subir_pdf("PADRE1", "n.pdf", PDF, archivo_id=malo)
    assert google.pedidos == []


async def test_el_alias_root_vale_como_padre(google):
    await drive.crear_carpeta("Proyectos", "root")
    assert json.loads(google.de_api()[0].content)["parents"] == ["root"]


@pytest.mark.parametrize("nombre", ["", "   ", None, 5])
async def test_un_nombre_vacio_es_un_error_de_quien_llama(google, nombre):
    with pytest.raises(ValueError):
        await drive.crear_carpeta(nombre, "PADRE1")
    assert google.pedidos == []


async def test_renombrar_es_un_patch_al_mismo_id(google):
    assert await drive.renombrar("ARCH1", "Nuevo nombre") == "ARCH1"
    p = google.de_api()[0]
    assert (p.method, p.url.path) == ("PATCH", "/drive/v3/files/ARCH1")
    assert json.loads(p.content) == {"name": "Nuevo nombre"}


def _partes(pedido):
    tipo = pedido.headers["content-type"]
    assert tipo.startswith("multipart/related; boundary=")
    msg = email.message_from_bytes(
        b"Content-Type: " + tipo.encode() + b"\r\n\r\n" + pedido.content)
    assert msg.is_multipart()
    return msg.get_payload()


async def test_subir_nuevo_es_un_post_multipart_con_metadatos_y_bytes(google):
    assert await drive.subir_pdf("PADRE1", "COT-2026-001.pdf", PDF) == "ARCHIVO1"
    p = google.de_api()[0]
    assert (p.method, p.url.path) == ("POST", "/upload/drive/v3/files")
    assert p.url.params["uploadType"] == "multipart"
    meta, datos = _partes(p)
    assert meta.get_content_type() == "application/json"
    assert json.loads(meta.get_payload(decode=True)) == {
        "name": "COT-2026-001.pdf", "parents": ["PADRE1"],
        "mimeType": "application/pdf"}
    assert datos.get_content_type() == "application/pdf"
    assert datos.get_payload(decode=True) == PDF


async def test_subir_con_archivo_id_reemplaza_el_contenido_y_no_crea(google):
    assert await drive.subir_pdf("PADRE1", "COT-2026-001.pdf", PDF,
                                 archivo_id="ARCH1") == "ARCH1"
    pedidos = google.de_api()
    assert [(p.method, p.url.path) for p in pedidos] == [
        ("PATCH", "/upload/drive/v3/files/ARCH1")]
    assert pedidos[0].url.params["uploadType"] == "media"
    assert pedidos[0].content == PDF
    assert pedidos[0].headers["content-type"] == "application/pdf"
    assert not [p for p in google.pedidos if p.method == "POST"
                and p.url.host != "oauth2.googleapis.com"]


async def test_un_reemplazo_que_contesta_con_otro_id_no_se_acepta(google):
    google.api = _devuelve(200, json={"id": "OTRO"})
    with pytest.raises(drive.DriveNoContesta):
        await drive.subir_pdf("PADRE1", "x.pdf", PDF, archivo_id="ARCH1")


@pytest.mark.parametrize("datos", [b"", "texto", None])
async def test_subir_sin_bytes_es_un_error_de_quien_llama(google, datos):
    with pytest.raises(ValueError):
        await drive.subir_pdf("PADRE1", "x.pdf", datos)
    assert google.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# tools/autorizar_drive.py
# ═══════════════════════════════════════════════════════════════════════

def _cargar_guion():
    espec = importlib.util.spec_from_file_location(
        "autorizar_drive_bajo_prueba", RAIZ / "tools" / "autorizar_drive.py")
    modulo = importlib.util.module_from_spec(espec)
    espec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def guion(monkeypatch, google):
    g = _cargar_guion()
    return g


def test_el_desafio_pkce_es_el_del_ejemplo_del_rfc_7636(guion):
    # RFC 7636, apéndice B.
    assert guion.desafio("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


def test_la_direccion_de_permiso_pide_solo_drive_file_y_ningun_secreto(guion):
    url = guion.url_de_permiso(CLIENTE, "http://127.0.0.1:5555", "EST", "VER")
    partes = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(partes.query)
    assert partes.netloc == "accounts.google.com"
    assert q["scope"] == [drive.ALCANCE]
    assert q["state"] == ["EST"] and q["client_id"] == [CLIENTE]
    assert q["redirect_uri"] == ["http://127.0.0.1:5555"]
    assert q["access_type"] == ["offline"] and q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [guion.desafio("VER")]
    _no_hay_secretos(url, "la direccion de permiso")
    assert "VER" not in url


def test_el_servidor_local_solo_acepta_el_codigo_con_el_state_nuestro(guion):
    import http.server
    servidor = http.server.HTTPServer(("127.0.0.1", 0),
                                      http.server.BaseHTTPRequestHandler)
    puerto = servidor.server_address[1]
    resultados = []

    def pegarle():
        for q in (f"code={CODIGO}&state=OTRO", "state=EST",
                  f"code={CODIGO}&state=EST"):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{puerto}/?{q}", timeout=10) as r:
                    resultados.append(r.status)
            except urllib.error.HTTPError as e:
                resultados.append(e.code)

    hilo = threading.Thread(target=pegarle)
    hilo.start()
    try:
        assert guion.esperar_codigo(servidor, "EST") == CODIGO
    finally:
        hilo.join(timeout=10)
        servidor.server_close()
    assert resultados == [400, 400, 200]


async def test_canjear_manda_el_codigo_al_servidor_de_tokens_y_devuelve_el_refresh(
        guion, google):
    google.token = _devuelve(200, json={
        "access_token": ACCESO, "refresh_token": REFRESCO,
        "scope": drive.ALCANCE, "token_type": "Bearer"})
    assert await guion.canjear(CLIENTE, SECRETO, CODIGO, "http://127.0.0.1:1",
                               "VER") == REFRESCO
    p = google.pedidos[0]
    assert (p.method, str(p.url)) == ("POST", drive.URL_TOKEN)
    assert urllib.parse.parse_qs(p.content.decode()) == {
        "grant_type": ["authorization_code"], "code": [CODIGO],
        "client_id": [CLIENTE], "client_secret": [SECRETO],
        "redirect_uri": ["http://127.0.0.1:1"], "code_verifier": ["VER"]}


@pytest.mark.parametrize("respuesta", [
    _devuelve(400, json={"error": "invalid_grant", "x": " ".join(SECRETOS)}),
    _devuelve(200, text="no es json " + " ".join(SECRETOS)),
    _devuelve(200, json={"access_token": ACCESO}),
    _devuelve(200, json={"refresh_token": REFRESCO,
                         "scope": "https://www.googleapis.com/auth/drive"}),
    _devuelve(200, json={"refresh_token": REFRESCO}),
    _devuelve(200, json=[REFRESCO]),
    _con_secretos_adentro,
], ids=["400", "no json", "sin refresh", "alcance completo",
        "sin alcance", "lista", "red con secretos"])
async def test_canjear_falla_con_una_frase_fija_sin_secretos(guion, google, respuesta):
    google.token = respuesta
    with pytest.raises(guion.Falla) as e:
        await guion.canjear(CLIENTE, SECRETO, CODIGO, "http://127.0.0.1:1", "VER")
    _no_hay_secretos(str(e.value), "la frase")


async def test_canjear_tiene_el_mismo_tope_total_que_el_cliente(guion, google, monkeypatch):
    monkeypatch.setattr(drive, "TIEMPO_LIMITE", 0.05)
    google.token = _lento
    with pytest.raises(guion.Falla):
        await guion.canjear(CLIENTE, SECRETO, CODIGO, "http://127.0.0.1:1", "VER")


def test_el_guion_no_crea_ningun_cliente_http_propio():
    """Frontera dicha: lee el texto. El guion no importa `httpx`; sale por
    `drive._pedir`. La conducta (nada sale por otra puerta) la vigila
    `_vigilar_toda_la_red` en las pruebas que usan `google`."""
    arbol = ast.parse((RAIZ / "tools" / "autorizar_drive.py").read_text(encoding="utf-8"))
    importados = {a.name for n in ast.walk(arbol)
                  if isinstance(n, ast.Import) for a in n.names}
    importados |= {(n.module or "") for n in ast.walk(arbol)
                   if isinstance(n, ast.ImportFrom)}
    assert not {m for m in importados
                if m.split(".")[0] in {"httpx", "requests", "socket", "aiohttp"}
                or m.startswith("urllib.request")}


async def test_canjear_no_sigue_redirecciones(guion, google):
    google.token = _redirige(307)
    with pytest.raises(guion.Falla):
        await guion.canjear(CLIENTE, SECRETO, CODIGO, "http://127.0.0.1:1", "VER")
    assert len(google.pedidos) == 1


def test_guardar_escribe_solo_para_el_dueno_y_no_sobrescribe(guion, tmp_path):
    ruta = guion.guardar(str(tmp_path), "X", "valor")
    assert pathlib.Path(ruta).read_text() == "valor"
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600
    with pytest.raises(guion.Falla):
        guion.guardar(str(tmp_path), "X", "otro")
    assert pathlib.Path(ruta).read_text() == "valor"


def _correr_guion(guion, monkeypatch, capsys, tmp_path, *extra):
    for n in ("LUCY_DRIVE_REFRESH_TOKEN",):
        monkeypatch.setenv(n, "")      # para que monkeypatch los restaure
    monkeypatch.setattr(guion, "esperar_codigo", lambda servidor, estado: CODIGO)
    codigo = guion.main(["--salida", str(tmp_path), *extra])
    visto = capsys.readouterr()
    return codigo, visto.out + visto.err


def _token_bueno(google):
    google.token = lambda p: (
        _R(200, json={"access_token": ACCESO, "refresh_token": REFRESCO,
                      "scope": drive.ALCANCE})
        if b"authorization_code" in p.content
        else _R(200, json={"access_token": ACCESO}))


def test_el_guion_entero_guarda_el_token_y_no_lo_imprime(
        guion, google, monkeypatch, capsys, tmp_path):
    _token_bueno(google)
    codigo, salida = _correr_guion(guion, monkeypatch, capsys, tmp_path)
    assert codigo == 0
    assert (tmp_path / "LUCY_DRIVE_REFRESH_TOKEN").read_text() == REFRESCO
    assert os.listdir(tmp_path) == ["LUCY_DRIVE_REFRESH_TOKEN"]
    _no_hay_secretos(salida, "lo impreso")
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in salida
    assert [p.url.host for p in google.pedidos] == ["oauth2.googleapis.com"]


def test_con_crear_proyectos_crea_la_carpeta_suelta_en_la_raiz(
        guion, google, monkeypatch, capsys, tmp_path):
    _token_bueno(google)
    google.api = lambda p: (
        _R(200, json={"files": []}) if p.method == "GET"
        else _R(200, json={"id": "CARPETAPROY"}))
    codigo, salida = _correr_guion(guion, monkeypatch, capsys, tmp_path,
                                   "--crear-proyectos")
    assert codigo == 0
    assert (tmp_path / "LUCY_DRIVE_CARPETA_PROYECTOS").read_text() == "CARPETAPROY"
    _no_hay_secretos(salida, "lo impreso")
    creacion = [p for p in google.de_api() if p.method == "POST"]
    assert len(creacion) == 1
    assert json.loads(creacion[0].content)["parents"] == ["root"]
    assert json.loads(creacion[0].content)["name"] == "Proyectos"


def test_con_crear_proyectos_no_repite_una_carpeta_que_ya_existe(
        guion, google, monkeypatch, capsys, tmp_path):
    _token_bueno(google)
    google.api = lambda p: (
        _R(200, json={"files": [{"id": "YAESTABA", "name": "Proyectos"}]})
        if p.method == "GET" else _R(500, json={}))
    codigo, _ = _correr_guion(guion, monkeypatch, capsys, tmp_path,
                              "--crear-proyectos")
    assert codigo == 0
    assert (tmp_path / "LUCY_DRIVE_CARPETA_PROYECTOS").read_text() == "YAESTABA"
    assert not [p for p in google.de_api() if p.method == "POST"]


def test_el_guion_sin_cliente_en_el_entorno_no_sale_a_ninguna_parte(
        guion, google, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("LUCY_DRIVE_CLIENT_SECRET")
    codigo, salida = _correr_guion(guion, monkeypatch, capsys, tmp_path)
    assert codigo == 1 and google.pedidos == []
    assert os.listdir(tmp_path) == []


def test_el_guion_no_pisa_un_token_que_ya_esta(guion, google, monkeypatch,
                                               capsys, tmp_path):
    (tmp_path / "LUCY_DRIVE_REFRESH_TOKEN").write_text("viejo")
    codigo, _ = _correr_guion(guion, monkeypatch, capsys, tmp_path)
    assert codigo == 1 and google.pedidos == []
    assert (tmp_path / "LUCY_DRIVE_REFRESH_TOKEN").read_text() == "viejo"


def test_el_guion_con_google_rechazando_no_deja_archivos_ni_secretos(
        guion, google, monkeypatch, capsys, tmp_path):
    google.token = _eco(400)
    codigo, salida = _correr_guion(guion, monkeypatch, capsys, tmp_path)
    assert codigo == 1 and os.listdir(tmp_path) == []
    _no_hay_secretos(salida, "lo impreso")
