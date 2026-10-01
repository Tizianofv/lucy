"""El lector de Noco (Lucy 1.0, E3): G13, G14 y G16, y la ruta del buscador.

QUÉ SE PRUEBA ACÁ, en una frase: que Lucy **lea** personas del Noco de CDS sin
poder escribir en él, sin pedir de más, y sin inventarse una lista cuando Noco no
contesta.

CÓMO. El camino es el DE PRODUCCIÓN, con un solo cambio: el cliente HTTP es el
de verdad (`httpx.AsyncClient`) y lo que se reemplaza es el **transporte**
(`httpx.MockTransport`), o sea el enchufe a la red. Todo lo demás corre como
corre: la biblioteca arma el pedido, el módulo decide el método, los parámetros
y las cabeceras. Un doble que reemplazara al cliente entero podría afirmar cosas
que la biblioteca de verdad no hace; así, lo que esta prueba mira es el pedido
que sale, no lo que a mí me gustaría que saliera.

LAS TRES GARANTÍAS, cada una con lo que la haría falsa:

  · **G13 — Lucy no escribe en Noco.** Se llama a TODAS las funciones públicas
    del módulo (la lista sale del módulo, no de un inventario tecleado) y cada
    pedido que sale tiene que ser un GET, a la base de Noco y a ningún otro
    sitio. La pareja de mutaciones de la vuelta es un POST escrito de dos
    formas: con `cliente.post(...)` y con `cliente.request("POST", ...)`.
  · **G14 — el buscador no deja meter condiciones.** El texto pasa por `_limpio`
    antes de armar el `where`, y se comprueba por la ruta real del panel, no
    solo por la función.
  · **G16 — nunca se piden teléfono, correo ni `bsuid`.** Lo que sale lleva
    `fields=Id,nombre` y nada más, y lo que VUELVE también: una fila de Noco con
    columnas de más sale de este módulo con dos claves.

LA FRONTERA DEL CENSO (la prueba del final), dicha en una línea para poder
predecirla sin correrla: es culpable todo `.py` del repositorio, fuera de
`tests/`, cuyo texto contenga `NOCODB_` — el prefijo con el que empiezan las
tres variables del servicio. La primera versión buscaba `NOCODB` a secas y
marcaba `captura/correo.py:1055`, que trae `"nocodb"` en la lista de dominios
de los que NO hay que avisar por correo: un falso positivo medido, y la razón
del guion bajo.

Lo que NO ve: un nombre de variable armado al vuelo (`"NOCODB_" + "BASE"`), uno
escrito en minúsculas (que no sería ninguna de las tres), y una URL de Noco
escrita a mano, que no necesita mencionar ninguna variable. Las tres cosas están
prohibidas por la misma regla que esto vigila —el repositorio es público y no
puede llevar ni la URL ni el id de la tabla—, pero esta prueba no las ve.

Correr:  python3 -m pytest tests/test_noco_lectura.py -q
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from fastapi.testclient import TestClient

import config
import noco_lectura
import web.app as panel
import web.auth as auth

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _httpx_de_verdad():
    """El módulo `httpx` DE VERDAD, no el de mentira que esta suite deja puesto.

    POR QUÉ NO ALCANZA CON `import httpx`. Varios archivos de prueba de este
    repositorio —`tests/test_buzon_que_no_se_ve.py:282`, entre otros— pisan
    `sys.modules["httpx"]` con un módulo falso **al importarse**, no dentro de
    una prueba: no hay ningún `monkeypatch` que lo devuelva a su sitio, y queda
    así para el resto de la corrida. Como pytest importa los archivos por orden
    alfabético, cualquier archivo que venga después y haga `import httpx` recibe
    el falso (`_Cualquiera`), y con él no se puede armar un cliente de verdad.

    El módulo bajo prueba sí tiene el de verdad: se importó mucho antes, cuando
    lo importó el panel. Si algún día tampoco fuera el de verdad, esta función
    lo dice en vez de entregar un doble y dejar que las pruebas mientan.
    """
    modulo = noco_lectura.httpx
    if inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                        ).endswith("httpx/__init__.py"):
        return modulo
    import starlette.testclient as testclient   # el que usa TestClient: ése sí
    modulo = getattr(testclient, "httpx", None)
    assert inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                            ).endswith("httpx/__init__.py"), (
        "no encontré el httpx de verdad: el que hay en sys.modules es el módulo "
        "falso que dejan las pruebas que lo pisan al importarse, y `starlette."
        "testclient` tampoco tiene el real. Sin el httpx de verdad esta prueba "
        "no puede decir nada sobre lo que el módulo pide por la red.")
    return modulo


HTTPX = _httpx_de_verdad()

# La clase REAL del cliente y la del transporte de mentira, del httpx de verdad.
_CLIENTE_REAL = HTTPX.AsyncClient

# Nada de esto es real: ni la URL, ni el id de la tabla, ni el token. El
# repositorio es público y estos valores viven en Railway.
BASE = "https://noco.invalid"
ESPERADA = HTTPX.URL(BASE)
TABLA = "tabla-de-prueba"
TOKEN = "token-de-prueba"


class _ConDoble:
    """El módulo httpx de verdad con UNA pieza cambiada: `AsyncClient`.

    Es un envoltorio, no un reemplazo: todo lo demás —`HTTPError`, que el
    módulo atrapa; `MockTransport`, que arma el doble; la clase del cliente—
    sale del httpx de verdad. Y se pone sobre el ATRIBUTO del módulo bajo prueba
    (`noco_lectura.httpx`), no sobre la biblioteca instalada: así ninguna otra
    prueba de la corrida se encuentra con esto puesto.
    """

    def __init__(self, real, cliente):
        self._real = real
        self.AsyncClient = cliente

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

# Lo que Noco contesta, en la forma del API v2 (ver `Noco`, abajo).
def _respuesta(filas, total=None) -> dict:
    return {"list": list(filas),
            "pageInfo": {"totalRows": len(filas) if total is None else total,
                         "page": 1, "pageSize": 25, "isLastPage": True}}


FILA = {"Id": 7, "nombre": "Ana Pérez"}


class Noco:
    """Un Noco de mentira: un cliente HTTP de VERDAD y un transporte de mentira.

    DE DÓNDE SALE LA FORMA QUE IMITA. Del API v2 de NocoDB y de lo que la sala
    midió contra el Noco de CDS el 1-oct-2026: `GET {NOCODB_BASE}/api/v2/tables/
    {tabla}/records` con la cabecera `xc-token`, paginado, contestando
    `{"list": [...], "pageInfo": {...}}`. La medición fue HTTP 200 y
    `pageInfo.totalRows` = 733 con `limit=1&fields=Id`; el lector paginado que ya
    existe en el sistema, con esa misma forma, está citado en §1.4 del diseño.

    FRONTERA, para que no se dé por cubierta: esto NO es una captura de una
    respuesta real. Los dos datos que no salen de una respuesta capturada son el
    nombre de la clave de las filas (`list`) y que cada fila traiga `Id` y
    `nombre` con esas mayúsculas; los dos vienen del diseño (§4 pide
    `fields=Id,nombre`) y de la medición de arriba. Cuando la sala traiga la
    captura de dos filas, lo que se reemplaza es este comentario y estas dos
    claves — y todas las pruebas de abajo siguen valiendo igual, porque ninguna
    decide con la forma: deciden con lo que el módulo PIDE y con lo que DEVUELVE.
    """

    def __init__(self, *, filas=(FILA,), total=None, status=200, cuerpo=None,
                 json=None, falla=None):
        self.filas = filas
        self.total = total
        self.status = status
        self.cuerpo = cuerpo          # texto crudo, para el caso «no es JSON»
        self.json_crudo = json        # un JSON con otra forma
        self.falla = falla            # una excepción del transporte
        self.pedidos: list = []       # `httpx.Request` de verdad, en orden

    def cliente(self, *args, **kwargs):
        """Lo que se pone en lugar de `httpx.AsyncClient`: el cliente REAL,
        con el enchufe a la red cambiado por este doble.

        Se construye con `_CLIENTE_REAL` —la clase guardada al importar— y no
        con `httpx.AsyncClient`, que en ese momento ES esta función: llamarla
        desde acá sería llamarse a sí misma.
        """
        kwargs["transport"] = HTTPX.MockTransport(self._manejar)
        return _CLIENTE_REAL(*args, **kwargs)

    def _manejar(self, pedido):
        self.pedidos.append(pedido)
        if self.falla is not None:
            raise self.falla
        if self.json_crudo is not None:
            return HTTPX.Response(self.status, json=self.json_crudo)
        if self.cuerpo is not None:
            return HTTPX.Response(self.status, text=self.cuerpo)
        if self.status != 200:
            return HTTPX.Response(self.status, json={"msg": "no"})
        return HTTPX.Response(200, json=_respuesta(self.filas, self.total))

    def donde(self, i: int = 0):
        assert len(self.pedidos) > i, f"no salió el pedido {i}: {self.pedidos}"
        return self.pedidos[i]

    def params(self, i: int = 0) -> dict[str, str]:
        return dict(self.donde(i).url.params)


def _apuntar_a_noco(monkeypatch, doble):
    """Deja el lector hablándole a un Noco de mentira.

    Se ponen las variables del servicio `lucy` (los mismos nombres que hay en
    Railway, con valores inventados): el módulo las lee en cada llamada, así que
    no hace falta recargarlo, y `monkeypatch` las saca al terminar.

    Y se le cambia al módulo `httpx` —solo a él, ver `_ConDoble`— por el httpx
    de verdad con el `AsyncClient` apuntando al doble.
    """
    monkeypatch.setenv("NOCODB_BASE", BASE)
    monkeypatch.setenv("NOCODB_T_PERSONAS", TABLA)
    monkeypatch.setenv("NOCODB_TOKEN_LUCY", TOKEN)
    monkeypatch.setattr(noco_lectura, "httpx",
                        _ConDoble(HTTPX, doble.cliente))
    return doble


@pytest.fixture
def noco(monkeypatch):
    return _apuntar_a_noco(monkeypatch, Noco())


def _cliente(chat=None) -> TestClient:
    c = TestClient(panel.app)
    if chat is not None:
        c.cookies.set(panel.COOKIE, auth.crear_token(chat, auth.VIDA_SESION))
    return c


# ═══════════════════════════════════════════════════════════════════════
# G13: la única puerta de salida, y solo GET
# ═══════════════════════════════════════════════════════════════════════

def _publicas():
    """Las funciones públicas del módulo, sacadas de `dir()` del módulo REAL.

    `dir()` y no una lista tecleada: una función nueva que alguien agregue
    mañana entra acá sola, y con ella entra la obligación de que sus pedidos
    sean GET. Las que no son funciones del módulo (lo importado, los datos)
    quedan afuera por sus atributos, no por su nombre.
    """
    for nombre in dir(noco_lectura):
        if nombre.startswith("_"):
            continue
        objeto = getattr(noco_lectura, nombre)
        if not inspect.iscoroutinefunction(objeto):
            continue
        if getattr(objeto, "__module__", None) != noco_lectura.__name__:
            continue
        yield nombre, objeto


# Funciones públicas que NO salen a la red, declaradas una por una. Hoy no hay
# ninguna: el cubo estricto es el que decide, y una función pública nueva que no
# toque la red tiene que venir a declararse acá a propósito.
_SIN_RED: set[str] = set()


def _argumentos(fn):
    """Con qué se llama a una función pública para que haga su trabajo.

    Sale de la ANOTACIÓN de la firma, no de una tabla de nombres: `str` recibe
    un texto con algo dentro (uno vacío no llega a Noco, y eso es a propósito:
    ver `buscar_personas`), `int` recibe un id.

    Y LO QUE NO ESTÁ ANOTADO NO SE PERDONA: se prueba con las dos formas y, si
    ninguna produce un pedido, la prueba se pone roja. Es el lado estricto a
    propósito — una función pública nueva que nadie pueda ejercitar es
    exactamente el sitio donde se escondería un POST.
    """
    valores = []
    for parametro in inspect.signature(fn).parameters.values():
        if parametro.kind in (parametro.VAR_POSITIONAL, parametro.VAR_KEYWORD):
            continue
        anotacion = parametro.annotation
        if anotacion is int:
            valores.append([7])
        elif anotacion is str:
            valores.append(["ana"])
        else:
            valores.append(["ana", 7])
    if not valores:
        return [()]
    combinaciones = [()]
    for opciones in valores:
        combinaciones = [c + (o,) for c in combinaciones for o in opciones]
    return combinaciones


async def test_G13_todo_lo_que_sale_del_modulo_es_un_get_a_noco(noco):
    """Cada función pública, llamada de verdad, y cada pedido que sale mirado.

    Es la garantía entera en una prueba: la lista de funciones sale de `dir()`
    del módulo, así que una función nueva que escriba en Noco cae acá sin que
    nadie se acuerde de venir a agregarla.
    """
    vistas = set()
    for nombre, fn in _publicas():
        for argumentos in _argumentos(fn):
            antes = len(noco.pedidos)
            try:
                await fn(*argumentos)
            except (ValueError, noco_lectura.NocoNoContesta):
                pass          # cómo se porta con eso lo prueban las de abajo
            if len(noco.pedidos) > antes:
                vistas.add(nombre)

    sin_ejercitar = {n for n, _ in _publicas()} - vistas - _SIN_RED
    assert not sin_ejercitar, (
        f"estas funciones públicas no llegaron a salir a la red con ninguna de "
        f"las formas con que esta prueba las llama: {sorted(sin_ejercitar)}. O "
        f"no las pude llamar (anotá su firma), o no salen a la red —y entonces "
        f"van a `_SIN_RED`, declaradas una por una, para que se vea que fue una "
        f"decisión y no un olvido.")
    assert noco.pedidos, "no salió ni un pedido: la prueba no ejerció nada"

    for pedido in noco.pedidos:
        assert pedido.method == "GET", (
            f"el lector de Noco mandó un {pedido.method}. El token de Railway "
            f"puede escribir: que Lucy no escriba lo sostiene este módulo.")
        # `url.netloc` de httpx es `bytes` (arma la petición cruda): el sitio se
        # compara por esquema, host y puerto, que sí son texto.
        assert (pedido.url.scheme, pedido.url.host, pedido.url.port) == (
            ESPERADA.scheme, ESPERADA.host, ESPERADA.port), (
            f"salió un pedido a {pedido.url.scheme}://{pedido.url.host}, y el "
            f"lector de Noco solo puede hablar con la base de Noco")
        assert pedido.url.path == f"/api/v2/tables/{TABLA}/records", (
            f"ruta inesperada: {pedido.url.path}")
        assert pedido.headers.get("xc-token") == TOKEN, (
            "el pedido salió sin la cabecera del token de Noco")
        assert TOKEN not in str(pedido.url), (
            "el token viajó en la URL: una URL termina en un registro")


async def test_G16_lo_que_sale_pide_un_nombre_y_nada_mas(noco):
    """Las columnas que se piden son `Id,nombre`, en TODOS los pedidos.

    Se mira lo que salió, no lo que dice el código: se llaman las funciones
    públicas y se leen los `fields` de los pedidos que llegaron al transporte.
    Es además el criterio de hermanos de esta garantía: no se pide «que
    `buscar_personas` pida bien», se pide que TODO lo que salga del módulo pida
    lo mismo.
    """
    await noco_lectura.buscar_personas("ana")
    await noco_lectura.persona(7)

    assert len(noco.pedidos) == 2, "no salieron los dos pedidos que se esperaban"
    for pedido in noco.pedidos:
        assert pedido.url.params["fields"] == "Id,nombre", (
            f"se pidieron las columnas {pedido.url.params['fields']!r}")
        for prohibido in ("telefono", "email", "bsuid", "phone", "mail"):
            assert prohibido not in str(pedido.url).lower(), (
                f"el pedido nombra {prohibido!r}: {pedido.url}")


async def test_G16_una_fila_con_columnas_de_mas_sale_con_dos_claves(noco):
    """Aunque Noco mande la ficha entera, de este módulo sale un nombre.

    Es la última puerta, y por eso se prueba con la fila REALMENTE sucia: si el
    `fields` no bastara —porque Noco lo ignora, o porque mañana alguien lo
    cambie— lo que se guarda en Lucy sigue sin teléfonos.
    """
    noco.filas = [{"Id": 7, "nombre": "Ana Pérez", "telefono": "8090000000",
                   "email": "a@b.invalid", "bsuid": "xx-99"}]

    personas = await noco_lectura.buscar_personas("ana")
    assert personas == [{"id": 7, "nombre": "Ana Pérez"}]
    assert set(personas[0]) == {"id", "nombre"}


async def test_el_buscador_no_trae_mas_de_lo_que_dice_el_diseno(noco):
    """Hasta 20 resultados, y el `limit` que se le pide a Noco es ese mismo
    número: no se traen 733 fichas para enseñar veinte."""
    noco.filas = [{"Id": i, "nombre": f"Persona {i}"} for i in range(1, 26)]
    personas = await noco_lectura.buscar_personas("persona")
    assert len(personas) == noco_lectura.TOPE_BUSQUEDA == 20
    assert noco.params()["limit"] == "20"


# ═══════════════════════════════════════════════════════════════════════
# G14: el buscador no deja meter condiciones propias
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("texto,esperado", [
    ("a),(Id,gt,0", "a Id gt 0"),      # el ataque del §8 del diseño
    ("Pérez,Juan", "Pérez Juan"),      # una coma normal, de un nombre normal
    ("(nombre,like,%", "nombre like %"),
    ("~~ana~~", "ana"),
    ("  ana   pérez ", "ana pérez"),
])
async def test_G14_el_texto_llega_limpio_al_where(noco, texto, esperado):
    await noco_lectura.buscar_personas(texto)
    where = noco.params()["where"]
    assert where == f"(nombre,like,%{esperado}%)"
    patron = where[len("(nombre,like,%"):-len("%)")]
    for molde in ",()~":
        assert molde not in patron, (
            f"{molde!r} sobrevivió en el patrón {patron!r}: con eso se cierra "
            f"el patrón y se agrega una condición propia")


@pytest.mark.parametrize("texto", ["", "   ", "(),~", "~,()", None, 7, ["a"]])
async def test_un_texto_que_no_deja_nada_no_le_pregunta_a_noco(noco, texto):
    """Sin nada que buscar, la lista vacía — y ni un pedido.

    No es lo mismo que «Noco no contestó» (eso levanta, ver abajo): acá no hay
    nada que preguntar, y pedir «todos» para enseñar veinte nombres al azar no
    es una búsqueda. Un texto que no es texto (un número, una lista que llegó
    por la URL) cae del mismo lado.
    """
    assert await noco_lectura.buscar_personas(texto) == []
    assert noco.pedidos == []


async def test_G14_lo_mismo_por_la_ruta_de_verdad(noco):
    """El ataque, por la ruta del panel que lo va a recibir del navegador.

    La función sola ya está probada arriba; esto comprueba que la ruta no le
    agrega ni le saca nada al texto antes de pasarlo (por ejemplo, un `q` que
    se use además para otra cosa).
    """
    r = _cliente(config.CHAT_ID_DUENO).get("/personas/buscar",
                                           params={"q": "a),(Id,gt,0"})
    assert r.status_code == 200, r.text[:300]
    assert noco.params()["where"] == "(nombre,like,%a Id gt 0%)"


# ═══════════════════════════════════════════════════════════════════════
# «Si Noco no contesta, se dice»
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("como", [
    "sin_red", "error_500", "no_es_json", "json_con_otra_forma", "sin_list",
])
async def test_si_noco_no_contesta_no_se_devuelve_una_lista_inventada(
        monkeypatch, como):
    """Los cinco modos de fallar, uno por uno, y ninguno devuelve `[]`.

    Un `[]` por un error de red es la mentira más cara de este módulo: quien
    busca concluye que esa persona no está en el CRM y crea una ficha repetida.
    """
    _apuntar_a_noco(monkeypatch, Noco(**{
        "sin_red": {"falla": HTTPX.ConnectError("no hay red")},
        "error_500": {"status": 503},
        "no_es_json": {"cuerpo": "<html>no soy JSON</html>"},
        "json_con_otra_forma": {"json": {"list": "no es una lista"}},
        "sin_list": {"json": {"pageInfo": {"totalRows": 733}}},
    }[como]))

    with pytest.raises(noco_lectura.NocoNoContesta) as fallo:
        await noco_lectura.buscar_personas("ana")
    assert TOKEN not in str(fallo.value), "el motivo del error lleva el token"


async def test_sin_las_variables_se_dice_cual_falta_y_no_se_llama(noco, monkeypatch):
    """Sin configurar no es «no hay nadie»: es un error que nombra la variable.

    Y no sale ni un pedido: no se llama a una URL a medias.
    """
    for nombre in noco_lectura._VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
        with pytest.raises(noco_lectura.NocoNoContesta) as fallo:
            await noco_lectura.buscar_personas("ana")
        assert nombre in str(fallo.value), (
            f"el motivo tiene que nombrar la variable que falta: {fallo.value}")
        monkeypatch.setenv(nombre, {"NOCODB_BASE": BASE, "NOCODB_T_PERSONAS": TABLA,
                                    "NOCODB_TOKEN_LUCY": TOKEN}[nombre])
    assert noco.pedidos == [], "salió un pedido con la configuración a medias"


def test_la_variable_del_token_se_llama_lucy_y_no_lectura():
    """El diseño decía `NOCODB_TOKEN_LECTURA`; la que existe es `…_LUCY`.

    No es un detalle de nombre: con el del diseño, Lucy se queda sin token y
    cada búsqueda dice «falta configurar». Tiziano decidió usar su cuenta normal
    (1-oct-2026) y por eso el nombre cambió; esta prueba existe para que nadie
    lo «arregle» de vuelta contra el diseño viejo.
    """
    assert "NOCODB_TOKEN_LUCY" in noco_lectura._VARIABLES
    assert "NOCODB_TOKEN_LECTURA" not in noco_lectura._VARIABLES


# ═══════════════════════════════════════════════════════════════════════
# `persona(id)`: la ficha que se vuelve a leer antes de guardar
# ═══════════════════════════════════════════════════════════════════════

async def test_persona_devuelve_la_ficha_por_su_id(noco):
    noco.filas = [FILA]
    assert await noco_lectura.persona(7) == {"id": 7, "nombre": "Ana Pérez"}
    assert noco.params()["where"] == "(Id,eq,7)"
    assert noco.params()["limit"] == "1"


async def test_persona_de_una_ficha_que_no_existe_es_none(noco):
    noco.filas = []
    assert await noco_lectura.persona(7) is None


@pytest.mark.parametrize("fila", [
    {"Id": "siete", "nombre": "Ana"},      # el Id no es un número
    {"Id": 7},                             # sin nombre
    {"Id": 7, "nombre": "   "},            # con el nombre vacío
    {"Id": True, "nombre": "Ana"},         # `True` es 1, y no es una ficha
    {"nombre": "Ana"},                     # sin Id
    "no soy una fila",
])
async def test_persona_no_se_inventa_una_ficha_que_no_vale(noco, fila):
    """Una fila que no sirve no se completa ni se inventa: la ficha no está."""
    noco.filas = [fila]
    assert await noco_lectura.persona(7) is None


@pytest.mark.parametrize("malo", [True, False, 0, -3, "7", 7.0, None, [7]])
async def test_persona_no_arma_un_where_con_cualquier_cosa(noco, malo):
    """El `Id` entra en el `where`: un texto libre ahí sería el mismo agujero
    que el buscador cierra con `_limpio`. Se rechaza antes de tocar la red."""
    with pytest.raises(ValueError):
        await noco_lectura.persona(malo)
    assert noco.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# La ruta del panel: JSON, con sesión, y dice cuando no pudo
# ═══════════════════════════════════════════════════════════════════════

def test_la_busqueda_sin_sesion_no_le_pregunta_nada_a_noco(noco):
    r = _cliente().get("/personas/buscar", params={"q": "ana"})
    assert r.status_code == 401, r.text[:200]
    assert noco.pedidos == [], "le preguntó a Noco sin sesión"


def test_la_busqueda_con_sesion_devuelve_json(noco):
    noco.filas = [FILA, {"Id": 8, "nombre": "Juan Pérez"}]
    r = _cliente(config.CHAT_ID_DUENO).get("/personas/buscar", params={"q": "pérez"})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"personas": [{"id": 7, "nombre": "Ana Pérez"},
                                     {"id": 8, "nombre": "Juan Pérez"}]}


def test_la_busqueda_dice_cuando_noco_no_contesta(monkeypatch):
    """503 con el motivo, nunca una lista vacía: el buscador tiene que poder
    decir «no pude preguntar» en vez de «no hay nadie»."""
    _apuntar_a_noco(monkeypatch, Noco(falla=HTTPX.ConnectError("no hay red")))

    r = _cliente(config.CHAT_ID_DUENO).get("/personas/buscar", params={"q": "ana"})
    assert r.status_code == 503, r.text[:300]
    assert "personas" not in r.json()
    assert r.json()["error"], "el 503 va sin motivo"
    assert TOKEN not in r.text


def test_la_ruta_exige_sesion_como_todas_las_del_panel():
    """El hermano de esta ruta es TODA ruta del panel, y quien las vigila a
    todas es `tests/test_panel.py::test_todas_las_rutas_estan_protegidas`: las
    saca de `app.routes`, así que ésta entra sola. Acá se comprueba que la ruta
    esté en esa lista — que es lo que ata esta prueba a aquélla."""
    rutas = {getattr(r, "path", "") for r in panel.app.routes}
    assert "/personas/buscar" in rutas


# ═══════════════════════════════════════════════════════════════════════
# El censo: nadie más del repositorio le habla a Noco
# ═══════════════════════════════════════════════════════════════════════

def _fuente() -> str:
    return pathlib.Path(noco_lectura.__file__).read_text(encoding="utf-8")


_VERBOS = ["get", "post", "put", "patch", "delete", "request", "send",
           "stream", "head", "options"]


def _clientes_de_httpx(nodo) -> set[str]:
    """Los nombres que, DENTRO de esa función, quedan atados a un cliente de
    `httpx` — por `x = httpx.AsyncClient(...)` o por `async with … as x`.

    Se busca el nombre atado y no el verbo suelto a propósito: `fila.get("Id")`
    es un `dict.get`, y contarlo como red convertiría esta guarda en una que
    grita por cualquier diccionario. El nombre del cliente se sigue de lo que el
    código HACE con él, no de cómo se llama la variable.
    """
    nombres = set()
    for sub in ast.walk(nodo):
        llamadas = []
        if isinstance(sub, ast.Assign):
            destinos = [t for t in sub.targets if isinstance(t, ast.Name)]
            llamadas = [(sub.value, destinos)]
        elif isinstance(sub, ast.AsyncWith):
            llamadas = [(item.context_expr, [item.optional_vars])
                        for item in sub.items]
        for valor, destinos in llamadas:
            if not isinstance(valor, ast.Call):
                continue
            funcion = valor.func
            if (isinstance(funcion, ast.Attribute)
                    and isinstance(funcion.value, ast.Name)
                    and funcion.value.id == "httpx"):
                nombres |= {d.id for d in destinos if isinstance(d, ast.Name)}
    return nombres


def _llamadas_a_la_red(arbol) -> dict[str, list[str]]:
    """{función del módulo: verbos con los que le habla a la red}.

    La lista de funciones sale del árbol REAL del módulo, no de un inventario:
    una función nueva que toque la red entra acá sola.
    """
    por_funcion = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        clientes = _clientes_de_httpx(nodo)
        if not clientes:
            continue
        verbos = sorted({sub.func.attr for sub in ast.walk(nodo)
                         if isinstance(sub, ast.Call)
                         and isinstance(sub.func, ast.Attribute)
                         and isinstance(sub.func.value, ast.Name)
                         and sub.func.value.id in clientes
                         and sub.func.attr in _VERBOS})
        if verbos:
            por_funcion[nodo.name] = verbos
    return por_funcion


def test_G13_la_unica_funcion_que_toca_la_red_es_get_y_solo_hace_get():
    """Una sola puerta, y es `_get`; y el único verbo que usa es `get`.

    Criterio de hermanos: no se mira «que `buscar_personas` no escriba», se mira
    que de TODAS las funciones del módulo la única que toca la red sea `_get` —
    y que el verbo que usa sea uno solo y sea GET. Las dos formas de escribir un
    POST (el verbo en un `cliente.post(...)` o dentro de un
    `cliente.request("POST", …)`) cambian esta lista.
    """
    hallado = _llamadas_a_la_red(ast.parse(_fuente()))
    assert hallado == {"_get": ["get"]}, (
        f"las funciones que le hablan a la red, con sus verbos: {hallado}. "
        f"Tiene que ser una sola, `_get`, y con `get` como único verbo.")


def test_G13_solo_se_importa_una_via_de_red_y_es_httpx():
    """Las vías de red del módulo, sacadas de sus `import` de verdad.

    Hoy es una sola biblioteca. Si mañana entra otra (`urllib`, `requests`), la
    puerta única deja de ser única y hay que verlo acá, no en producción.
    """
    arbol = ast.parse(_fuente())
    vias = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            vias |= {a.name.split(".")[0] for a in nodo.names}
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            vias.add(nodo.module.split(".")[0])
    assert vias & {"httpx", "requests", "urllib", "urllib3", "aiohttp",
                   "http", "socket", "ftplib", "smtplib", "imaplib"} == {"httpx"}


def test_G13_ninguna_otra_parte_del_repositorio_nombra_a_noco():
    """El censo: fuera de `tests/`, el único archivo que nombra `NOCODB` es el
    lector.

    LA LISTA DE ARCHIVOS SALE DEL DISCO, no de un inventario: se la pide a
    `test_buzon_que_no_se_ve._py_en_disco`, la puerta del repositorio para
    recorrerlo entero (la misma que usan los otros barridos). Un archivo nuevo
    que nombre a Noco se pone rojo solo.

    Y LOS NOMBRES NO SE TECLEAN: se sacan del propio módulo (`_VARIABLES`), que
    es donde vive la lista de variables del servicio. Lo que sí se exige es que
    sean exactamente los tres del diseño, para que «falta configurar» no se
    vuelva la respuesta normal de un nombre mal escrito.
    """
    import test_buzon_que_no_se_ve as barrido

    culpables = []
    for ruta in barrido._py_en_disco(RAIZ):
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel.startswith("tests/"):
            continue
        if "NOCODB_" in ruta.read_text(encoding="utf-8"):
            culpables.append(rel)
    assert culpables == ["noco_lectura.py"], (
        f"estos archivos nombran a Noco y no deberían: "
        f"{sorted(set(culpables) - {'noco_lectura.py'})}. Que Lucy no escriba en "
        f"Noco se sostiene en que haya UNA sola puerta; la segunda que aparezca "
        f"es la que nadie mira.")

    nombres = {n.value for n in ast.walk(ast.parse(_fuente()))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value.upper().startswith("NOCODB")}
    assert nombres == set(noco_lectura._VARIABLES) == {
        "NOCODB_BASE", "NOCODB_T_PERSONAS", "NOCODB_TOKEN_LUCY"}


def test_el_modulo_no_escribe_nada():
    """La otra mitad de G13, leída del código: de todo el módulo, la única
    llamada que podría escribir es `cliente.get(...)`. No hay `open(...)`, ni
    `execute`, ni un `post`.

    No reemplaza al transporte de arriba —que mira el pedido que sale— sino que
    cubre lo que ese no ve: que el módulo ni siquiera intente escribir en otro
    sitio (un archivo, la base).
    """
    arbol = ast.parse(_fuente())
    verbos = {"post", "put", "patch", "delete", "execute", "executemany",
              "write_text", "write_bytes", "remove", "unlink"}
    hallados = {(n.func.attr, n.lineno) for n in ast.walk(arbol)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in verbos}
    assert hallados == set(), f"el lector de Noco llama a algo que escribe: {hallados}"


# ═══════════════════════════════════════════════════════════════════════
# La garantía de TODO el repositorio: solo el lector tiene el token, y el
# lector solo hace GET. Decide por lo que el código hace, no por cómo lo escribe.
# ═══════════════════════════════════════════════════════════════════════
#
# POR QUÉ ESTÁ AQUÍ, medido por el testigo el 1-oct-2026 sobre `5dac3b9`: la
# primera versión de esta garantía miraba solo `noco_lectura.py` y buscaba el
# texto `NOCODB_` en el resto. Tres roturas la burlaron con la suite verde:
#   u01  `web/auth.py` gana una función con `urllib` + DELETE a Noco, que usa
#        `noco_lectura._configurado()` para sacar la URL y el token;
#   t05  `subprocess.run(["curl", "-X", "POST", ...])` dentro del lector;
#   u02  `httpx.request("POST", ...)` dentro del lector, en una función sin
#        llamar. Con el token de la cuenta normal de Tiziano (puede escribir en
#        el CRM), cualquiera de las tres borra o cambia clientes.
#
# LA REGLA, en dos líneas y sin lista de verbos que vigilar:
#
#   FUERA DEL LECTOR (todo `.py` del repositorio que no es de `tests/`): nadie
#   puede obtener la URL ni el token. No se importa nada privado del lector, no
#   se nombra ninguna variable `NOCODB_*` (en la forma en que esté escrita, si
#   se puede plegar a un texto) y el entorno no se lee entero. Sin la URL y el
#   token no hay petición a Noco que escribir: es lo que cierra a u01 sin tener
#   que saber con qué biblioteca se habla a la red.
#
#   DENTRO DEL LECTOR: una LISTA BLANCA. Las únicas importaciones son las de
#   `_IMPORTS_DEL_LECTOR`; las únicas llamadas son las de `_LLAMADAS_INOCUAS`, las
#   funciones y clases del propio módulo, y las dos de la red —`httpx.AsyncClient`
#   y `cliente.get`—, que solo pueden estar en `_get`, una vez cada una, y
#   `cliente` tiene que ser lo que ata ese `async with`. Lo que no esté en la lista
#   cae del lado estricto: `subprocess`, `os.system`, `socket`, `httpx.request`,
#   `cliente.post`, `getattr(cliente, "post")`… todo se detiene por lo mismo:
#   no está en la lista, y no por cómo está escrito.
#
# LA FRONTERA (lo que esta garantía NO ve), dicha para que nadie la dé por cubierta:
#   · un `.py` de `tests/` que hable con Noco (los tests no se despliegan);
#   · lo que no es `.py`: scripts de shell, `.yml`, SQL;
#   · un texto de `NOCODB_*` armado de formas que no se pueden plegar sin correr el
#     código (`"".join([...])`, `"%s" % x`, una lectura del archivo `.env`);
#   · una URL de Noco o un token escritos a mano: no pasan por ninguna variable.
#     Están prohibidos por la regla del repositorio público, pero esto no los ve;
#   · el entorno entero pasado a un hijo: `db/backup.py` lo hace una vez
#     (declarado abajo, `_ENTORNO_ENTERO_DECLARADO`) y no manda nada a Noco.

_IMPORTS_DEL_LECTOR = {"__future__", "logging", "os", "httpx"}

# Llamadas que no salen a ningún sitio, exactamente como se escriben. Lo que no
# esté acá —y no sea una función o clase del propio módulo— hace fallar la prueba.
_LLAMADAS_INOCUAS = {
    "isinstance", "len", "str", "zip", "type", "ValueError",
    "logging.getLogger", "log.warning",
    "os.environ.get", "os.environ.get().strip", "os.environ.get().strip().rstrip",
    "<Constant>.join", "texto.replace", "texto.split", "nombre.strip",
    "fila.get", "datos.get", "respuesta.json",
}

# La red: lo único que sale del módulo. Cada una, una vez, y solo en `_get`.
_LLAMADAS_DE_RED = {"httpx.AsyncClient": 1, "cliente.get": 1}
_FUNCION_DE_LA_RED = "_get"

# Entorno entero leído fuera del lector, declarado uno por uno (cubo que perdona).
_ENTORNO_ENTERO_DECLARADO = {"db/backup.py": 1}


def _punteado(nodo) -> str:
    """La llamada escrita como texto: `a.b.c`; una llamada en medio es `()`, y lo
    que no sea nombre ni atributo se escribe `<TipoDeNodo>`."""
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return f"{_punteado(nodo.value)}.{nodo.attr}"
    if isinstance(nodo, ast.Call):
        return f"{_punteado(nodo.func)}()"
    return f"<{type(nodo).__name__}>"


def _padres(arbol) -> dict:
    return {hijo: padre for padre in ast.walk(arbol)
            for hijo in ast.iter_child_nodes(padre)}


def _plegar(nodo):
    """El texto que vale un nodo, si se sabe sin correr nada: una constante, o
    una suma de constantes (`"NOCODB_" + "BASE"`). Si no, `None`."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
        izq, der = _plegar(nodo.left), _plegar(nodo.right)
        if izq is not None and der is not None:
            return izq + der
    return None


def _infracciones_del_lector(fuente: str) -> list[str]:
    """Todo lo que, DENTRO del lector, no está en la lista blanca."""
    arbol = ast.parse(fuente)
    malas: list[str] = []
    propios = {n.name for n in arbol.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef))}
    # 1. Importaciones.
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name not in _IMPORTS_DEL_LECTOR or a.asname:
                    malas.append(f"línea {n.lineno}: import {a.name}")
        elif isinstance(n, ast.ImportFrom):
            if n.module != "__future__" or n.level:
                malas.append(f"línea {n.lineno}: from {n.module} import …")
    # 2. Llamadas: solo las de la lista, las del módulo y las dos de la red.
    cuenta = {k: 0 for k in _LLAMADAS_DE_RED}
    for funcion in ast.walk(arbol):
        if not isinstance(funcion, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Solo las llamadas propias de esta función, no las de las anidadas.
        for n in _propias(funcion):
            if not isinstance(n, ast.Call):
                continue
            quien = _punteado(n.func)
            if quien in _LLAMADAS_DE_RED:
                cuenta[quien] += 1
                if funcion.name != _FUNCION_DE_LA_RED:
                    malas.append(f"línea {n.lineno}: {quien} fuera de "
                                 f"{_FUNCION_DE_LA_RED} (en {funcion.name})")
            elif quien not in _LLAMADAS_INOCUAS and quien not in propios:
                malas.append(f"línea {n.lineno}: llamada fuera de la lista "
                             f"blanca: {quien}")
    # Llamadas a nivel de módulo (fuera de toda función).
    for n in _propias(arbol):
        if isinstance(n, ast.Call):
            quien = _punteado(n.func)
            if quien not in _LLAMADAS_INOCUAS and quien not in propios:
                malas.append(f"línea {n.lineno}: llamada de módulo fuera de la "
                             f"lista blanca: {quien}")
    for quien, esperadas in _LLAMADAS_DE_RED.items():
        if cuenta[quien] != esperadas:
            malas.append(f"{quien} aparece {cuenta[quien]} vez/veces y tiene "
                         f"que ser {esperadas}")
    # 3. `httpx` solo se toca con AsyncClient y HTTPError, y solo en `_get`.
    for f in arbol.body:
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(f):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id == "httpx"):
                if f.name != _FUNCION_DE_LA_RED or n.attr not in (
                        "AsyncClient", "HTTPError"):
                    malas.append(f"línea {n.lineno}: httpx.{n.attr} en {f.name}")
            elif (isinstance(n, ast.Name) and n.id == "httpx"
                    and not isinstance(_padres(f).get(n), ast.Attribute)):
                malas.append(f"línea {n.lineno}: `httpx` suelto en {f.name}")
    # 4. `cliente` es lo que ata el único `async with httpx.AsyncClient(...)`.
    atados = [(i.optional_vars.id if isinstance(i.optional_vars, ast.Name)
               else None, _punteado(i.context_expr.func)
               if isinstance(i.context_expr, ast.Call) else None)
              for n in ast.walk(arbol) if isinstance(n, ast.AsyncWith)
              for i in n.items]
    if atados != [("cliente", "httpx.AsyncClient")]:
        malas.append(f"los `async with` del módulo son {atados}; tiene que ser "
                     f"uno solo: httpx.AsyncClient … as cliente")
    if any(isinstance(n, ast.With) for n in ast.walk(arbol)):
        malas.append("hay un `with` síncrono en el lector")
    return malas


def _propias(nodo):
    """Los nodos de `nodo` sin entrar en las funciones definidas dentro."""
    pila = list(ast.iter_child_nodes(nodo))
    while pila:
        n = pila.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pila.extend(ast.iter_child_nodes(n))


def _nombres_del_lector():
    """(públicos, privados) sacados del módulo REAL, no de una lista.

    Públicos: funciones y clases definidas en `noco_lectura` cuyo nombre no
    empieza por `_`. Privados: todo lo demás que el módulo expone (`_get`,
    `_configurado`, pero también `httpx`, `os`, `COLUMNAS`…): nadie de fuera lo
    necesita. (Para los TEXTOS solo cuentan los que empiezan por `_`: `\"httpx\"` o
    `\"os\"` son palabras de cualquier archivo.)"""
    publicos = {n for n in dir(noco_lectura) if not n.startswith("_")
                and getattr(getattr(noco_lectura, n), "__module__", None)
                == "noco_lectura"
                and (inspect.isfunction(getattr(noco_lectura, n))
                     or inspect.isclass(getattr(noco_lectura, n)))}
    privados = {n for n in dir(noco_lectura)
                if n not in publicos and not n.startswith("__")}
    return publicos, privados


def _infracciones_de_afuera(fuente: str) -> tuple[list[str], int]:
    """(infracciones, usos del entorno entero) de un `.py` que NO es el lector."""
    publicos, privados = _nombres_del_lector()
    arbol = ast.parse(fuente)
    padres = _padres(arbol)
    malas: list[str] = []
    alias: set[str] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == "noco_lectura":
                    if a.name != "noco_lectura":
                        malas.append(f"línea {n.lineno}: import {a.name}")
                    alias.add(a.asname or a.name)
        elif isinstance(n, ast.ImportFrom):
            modulo = (n.module or "").split(".")[0]
            for a in n.names:
                if modulo == "noco_lectura":
                    if a.name not in publicos:
                        malas.append(f"línea {n.lineno}: from noco_lectura "
                                     f"import {a.name} (no es público)")
                elif a.name == "noco_lectura":
                    alias.add(a.asname or a.name)
                elif modulo == "os" and a.name in ("environ", "getenv"):
                    malas.append(f"línea {n.lineno}: from os import {a.name}")
    for n in ast.walk(arbol):
        # El alias del módulo solo vale como `alias.<público>`.
        if isinstance(n, ast.Name) and n.id in alias:
            padre = padres.get(n)
            if isinstance(padre, (ast.Import, ast.ImportFrom, ast.alias)):
                continue
            if not (isinstance(padre, ast.Attribute) and padre.value is n
                    and padre.attr in publicos):
                malas.append(f"línea {n.lineno}: `{n.id}` se usa de otra forma "
                             f"que `{n.id}.<función pública>`")
        # Los textos: ningún NOCODB_*, ni el nombre del módulo, ni un privado.
        valor = _plegar(n) if isinstance(n, (ast.Constant, ast.BinOp)) else None
        if valor is not None and not isinstance(padres.get(n), ast.BinOp):
            if "NOCODB_" in valor.upper():
                malas.append(f"línea {n.lineno}: nombra una variable NOCODB_*")
            if valor == "noco_lectura" or (valor in privados
                                           and valor.startswith("_")):
                malas.append(f"línea {n.lineno}: el texto {valor!r} apunta al "
                             f"lector por la puerta de atrás")
    # El entorno: solo se lee por una clave que es una constante.
    entero = 0
    for n in ast.walk(arbol):
        es_environ = ((isinstance(n, ast.Attribute) and n.attr == "environ"
                       and isinstance(n.value, ast.Name) and n.value.id == "os")
                      or (isinstance(n, ast.Name) and n.id == "environ"))
        if es_environ:
            padre = padres.get(n)
            por_clave = isinstance(padre, ast.Subscript) and padre.value is n \
                and _plegar(padre.slice) is not None
            por_metodo = (isinstance(padre, ast.Attribute) and padre.value is n
                          and padre.attr in ("get", "pop", "setdefault")
                          and isinstance(padres.get(padre), ast.Call)
                          and padres[padre].args
                          and _plegar(padres[padre].args[0]) is not None)
            if not (por_clave or por_metodo):
                entero += 1
        elif isinstance(n, ast.Call) and _punteado(n.func) == "os.getenv":
            if not (n.args and _plegar(n.args[0]) is not None):
                entero += 1
    return malas, entero


def test_G13_el_lector_tiene_una_sola_puerta_y_es_un_get():
    """DENTRO del lector, lista blanca: sale a la red `_get`, una vez, con GET."""
    malas = _infracciones_del_lector(_fuente())
    assert malas == [], "el lector hace algo que no está en la lista blanca:\n" \
        + "\n".join(malas)


def test_G13_en_todo_el_repositorio_nadie_mas_obtiene_la_url_ni_el_token():
    """FUERA del lector: todo `.py` que no es de `tests/` (la lista sale del
    disco, por `_py_en_disco`), sin ninguna lista tecleada de archivos.

    Lo que se exige a cada uno: no importa nada privado del lector, no usa el
    módulo de otra forma que `noco_lectura.<función pública>`, no nombra ninguna
    variable `NOCODB_*` ni apunta al lector con un texto, y no lee el entorno
    entero. La lista de qué es público y qué privado sale del módulo real.
    """
    import test_buzon_que_no_se_ve as barrido

    visto = 0
    entero_por_archivo: dict[str, int] = {}
    culpas: list[str] = []
    for ruta in barrido._py_en_disco(RAIZ):
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel.startswith("tests/") or rel == "noco_lectura.py":
            continue
        visto += 1
        malas, entero = _infracciones_de_afuera(ruta.read_text(encoding="utf-8"))
        culpas += [f"{rel}: {m}" for m in malas]
        if entero:
            entero_por_archivo[rel] = entero
    assert visto > 30, f"el barrido solo vio {visto} archivos: algo no anda"
    assert culpas == [], "alguien más del repositorio puede llegar a Noco:\n" \
        + "\n".join(culpas)
    assert entero_por_archivo == _ENTORNO_ENTERO_DECLARADO, (
        f"lee el entorno entero: {entero_por_archivo}; declarado: "
        f"{_ENTORNO_ENTERO_DECLARADO}. Un token que vive en el entorno se lleva "
        f"con él: si de verdad hace falta, se declara uno por uno.")


# Entradas inventadas: a la guarda no se le da solo lo que hay hoy (Regla 18).
def _lector_con(viejo: str, nuevo: str) -> str:
    fuente = _fuente()
    assert fuente.count(viejo) == 1, f"el texto a romper no está: {viejo!r}"
    return fuente.replace(viejo, nuevo)


_ROTURAS_DEL_LECTOR = {
    "t05 subprocess curl POST": lambda: _lector_con(
        "import os\n", "import os\nimport subprocess\n").replace(
        "async def _get(", "def _x():\n    subprocess.run(['curl', '-X', "
        "'POST', 'x'])\n\n\nasync def _get(", 1),
    "t05b os.system": lambda: _lector_con(
        "async def _get(", "def _x():\n    os.system('curl -X POST x')\n\n\n"
        "async def _get(", ),
    "u02 httpx.request POST sin llamar": lambda: _lector_con(
        "async def _get(", "def _x():\n    httpx.request('POST', 'x')\n\n\n"
        "async def _get("),
    "u02b httpx.post": lambda: _lector_con(
        "async def _get(", "def _x():\n    httpx.post('x')\n\n\nasync def _get("),
    "m01 cliente.post": lambda: _lector_con("await cliente.get(", "await cliente.post("),
    "m02 cliente.request": lambda: _lector_con(
        "await cliente.get(", "await cliente.request('POST', "),
    "getattr al verbo": lambda: _lector_con(
        "await cliente.get(", "await getattr(cliente, 'po' + 'st')("),
    "urllib": lambda: _lector_con("import os\n", "import os\nimport urllib.request\n"),
    "socket": lambda: _lector_con("import os\n", "import os\nimport socket\n"),
    "otro cliente": lambda: _lector_con(
        "async def _get(", "async def _y():\n    async with httpx.AsyncClient() "
        "as c:\n        await c.delete('x')\n\n\nasync def _get("),
    "segunda llamada get": lambda: _lector_con(
        "    return datos\n", "    await cliente.get('x')\n    return datos\n"),
}


@pytest.mark.parametrize("nombre", sorted(_ROTURAS_DEL_LECTOR))
def test_G13_la_lista_blanca_del_lector_se_pone_roja_con_entradas_inventadas(nombre):
    """Cada rotura es la fuente REAL del lector con una cosa más; la guarda tiene
    que verla. Si una se escapa, la lista blanca prometía de más."""
    assert _infracciones_del_lector(_ROTURAS_DEL_LECTOR[nombre]()) != []


def test_G13_el_lector_sin_romper_no_tiene_infracciones():
    """El control: la misma función que acusa a las roturas no acusa al original."""
    assert _infracciones_del_lector(_fuente()) == []


_ROTURAS_DE_AFUERA = {
    "u01 usa _configurado": "import noco_lectura\n"
        "def f():\n    return noco_lectura._configurado()\n",
    "u01b from import privado": "from noco_lectura import _configurado\n",
    "u01c from import _get": "from noco_lectura import _get\n",
    "u01d import del módulo httpx del lector": "from noco_lectura import httpx\n",
    "u01e import * ": "from noco_lectura import *\n",
    "alias": "import noco_lectura as nl\ndef f():\n    return nl._get\n",
    "getattr sobre el módulo": "import noco_lectura\n"
        "def f():\n    return getattr(noco_lectura, '_get')\n",
    "el módulo pasado como valor": "import noco_lectura\nx = noco_lectura\n",
    "from . import": "from web import noco_lectura\nx = noco_lectura._get\n",
    "texto privado": "import importlib\nm = importlib.import_module('noco_lectura')\n",
    "texto _configurado": "x = '_configurado'\n",
    "variable directa": "import os\nx = os.environ['NOCODB_BASE']\n",
    "variable en minúsculas": "import os\nx = os.getenv('nocodb_token_lucy')\n",
    "variable armada": "import os\nx = os.environ.get('NOCODB_' + 'TOKEN_LUCY')\n",
    "variable en f-string": "x = f'NOCODB_{1}'\n",
    "entorno entero": "import os\nx = dict(os.environ)\n",
    "entorno recorrido": "import os\nx = [k for k in os.environ]\n",
    "environ importado": "from os import environ\n",
    "getenv con clave dinámica": "import os\nx = os.getenv(k)\n",
}


@pytest.mark.parametrize("nombre", sorted(_ROTURAS_DE_AFUERA))
def test_G13_la_guarda_de_afuera_se_pone_roja_con_entradas_inventadas(nombre):
    malas, entero = _infracciones_de_afuera(_ROTURAS_DE_AFUERA[nombre])
    assert malas or entero, f"se le escapó: {nombre}"


@pytest.mark.parametrize("fuente", [
    "import noco_lectura\nasync def f(q):\n    return await noco_lectura.buscar_personas(q)\n",
    "from noco_lectura import buscar_personas, persona, NocoNoContesta\n",
    "import os\nx = os.environ.get('TELEGRAM_TOKEN')\ny = os.environ['DATABASE_URL']\n",
    "x = 'nocodb'\n",
])
def test_G13_la_guarda_de_afuera_deja_pasar_lo_normal(fuente):
    assert _infracciones_de_afuera(fuente) == ([], 0)
