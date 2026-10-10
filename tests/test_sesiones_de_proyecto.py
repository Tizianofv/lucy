"""El bloque «Sesiones y trabajos de este proyecto» (parte 9 de la página de un
proyecto, 9-oct-2026): las garantías de la fila 9 del diseño aprobado.

QUÉ SE VIGILA, una prueba por garantía:
  · solo GET, y solo a `config.REGISTRO_URL`;
  · la llave no sale en ningún registro ni mensaje;
  · si la App no contesta se dice, y la página carga;
  · solo se pregunta al abrir un proyecto, no al listar;
  · un proyecto sin cliente no pregunta nada;
  · las canceladas no salen;
  · el código se pinta como texto discreto al lado del concepto;
  · se ve igual en la sesión de la casa y en la de solo ver.

El DINERO (la columna de cada renglón y los tres totales) es la parte 10 y se
vigila en `tests/test_dinero_de_proyecto.py`. Acá quedó lo que aquella prueba
necesita del lector: que los renglones traigan sus cifras.

CÓMO. La App se dobla EN LA RED: un servidor HTTP de verdad en 127.0.0.1
(`tests/_app_de_registro.py`), al que `registro_lectura` le habla con `httpx`
como le hablaría a la App. Nada reemplaza la función del lector ni su cliente
HTTP: lo que se mira es el pedido que sale y lo que la página pinta con lo que
vuelve. La página se pinta por la ruta real y la plantilla real, con la base
reemplazada por el modelo de prueba (`_proyectos_de_prueba.BaseQueNoSeToca`).

FRONTERA, dicha para que no se dé por cubierta: el doble afirma el contrato que
la sala le pasó a Lucy (la forma de la respuesta, el 401 sin llave buena, el 502
cuando la App no pudo leer su base). NO afirma cómo la App reconoce las sesiones
de una ficha, ni de dónde saca cada cifra, ni que la App de verdad conteste así;
y nada de esto se corrió contra la App real.

Correr:  python3 -m pytest tests/test_sesiones_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import _proyectos_de_prueba as pp  # noqa: E402
import config  # noqa: E402
import registro_lectura  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
from _app_de_registro import AppDeRegistro  # noqa: E402
from test_pagina_proyectos import gente, mundo  # noqa: E402,F401  (la base de SQLite con el esquema real)

# Nada de esto es real: ni la llave ni los datos del registro.
LLAVE = "llave-de-prueba"
FICHA = 77                       # el Id de la ficha de Noco del cliente del proyecto
HOY = "2026-10-09"

SESION = {"ref": 11, "codigo": "s011", "es_trabajo": False, "fecha": HOY,
          "sala": "Sala P", "sala_mostrar": "Sala P", "horas": 3, "servicio": "Grabación",
          "atendio": "Ivan", "asignado_a": "Ricardo", "estado": "CONFIRMADA",
          "cancelada": False, "total": 987654.32, "abonado": 111111.11, "saldo": 876543.21}
TRABAJO = {"ref": 12, "codigo": "t012", "es_trabajo": True, "fecha": HOY,
           "sala": "Sala P", "sala_mostrar": "Sala P", "horas": 2, "servicio": "Mezcla",
           "atendio": None, "asignado_a": "Rosi", "estado": "EN PROCESO",
           "cancelada": False, "total": 765432.10, "abonado": 0.0, "saldo": 765432.10}
CANCELADA = {**SESION, "ref": 13, "codigo": "c013", "cancelada": True}


@pytest.fixture
def registro(monkeypatch):
    """La App de mentira, con su llave puesta, y Lucy apuntada a ella."""
    doble = AppDeRegistro()
    doble.llave = LLAVE
    monkeypatch.setattr(config, "REGISTRO_URL", doble.url)
    monkeypatch.setattr(config, "LUCY_LLAVE_SERVICIO", LLAVE)
    try:
        yield doble
    finally:
        doble.cerrar()


def _respuesta(sesiones=(SESION, TRABAJO), *, puede_ligar=True, motivo="", no_halladas=()):
    return {"puede_ligar": puede_ligar, "motivo_sin_ligar": motivo,
            "sesiones": list(sesiones), "no_halladas": list(no_halladas)}


@contextlib.contextmanager
def pagina(monkeypatch, noco_id=FICHA):
    """`GET /proyectos` por la ruta real y la plantilla real, con la base de mentira.

    El proyecto 1 del modelo es el mismo de siempre («Disco Uno»), con la ficha de
    cliente puesta: el modelo sale de `armar_pagina`, como en producción.
    """
    guardado = panel.db
    modelo = pp.modelo()
    modelo["proyectos"][1]["cliente_noco_id"] = noco_id
    monkeypatch.setattr(pp, "modelo", lambda: modelo)
    panel.db = pp.BaseQueNoSeToca()
    try:
        yield
    finally:
        panel.db = guardado


def _cliente(con: str = "casa") -> TestClient:
    c = TestClient(panel.app)
    if con == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    else:
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def _abrir(monkeypatch, consulta="p=1", con="casa", noco_id=FICHA) -> str:
    """La página del proyecto, pintada de verdad (ruta real, plantilla real)."""
    with pagina(monkeypatch, noco_id):
        r = _cliente(con).get(f"/proyectos?{consulta}")
    assert r.status_code == 200, r.text[:400]
    return r.text


def _bloque(html: str) -> str:
    """Lo que hay dentro del bloque de las sesiones, para mirar solo eso."""
    dentro = html.split('id="sesiones-y-trabajos"')[1].split("</section>")[0]
    return " ".join(dentro.split())


def _forzar(estado, cuerpo=b"", tipo="application/json"):
    return (estado, cuerpo, tipo, 0.0)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: solo GET y solo a config.REGISTRO_URL
# ═══════════════════════════════════════════════════════════════════════

def _publicas():
    """Las funciones públicas del módulo, sacadas de `dir()` del módulo REAL."""
    for nombre in dir(registro_lectura):
        if nombre.startswith("_"):
            continue
        objeto = getattr(registro_lectura, nombre)
        if (inspect.iscoroutinefunction(objeto)
                and getattr(objeto, "__module__", None) == registro_lectura.__name__):
            yield nombre, objeto


# Funciones públicas que NO salen a la red, declaradas una por una.
_SIN_RED: set[str] = set()


# Las DOS puertas del lector, declaradas una por una: las sesiones de una ficha (parte 9) y la
# búsqueda (parte 13). Una ruta nueva hacia afuera cae acá sin que nadie se acuerde de agregarla.
_SIN_RED: set[str] = set()
_RUTAS_DEL_LECTOR = {"/api/lucy/sesiones", "/api/lucy/sesiones/buscar"}


async def test_lo_que_sale_del_modulo_es_un_get_al_registro(registro):
    """Cada función pública, llamada de verdad, y cada pedido que salió mirado.

    La lista sale de `dir()` del módulo: una función nueva que hable con otro
    sitio cae acá sin que nadie se acuerde de agregarla.
    """
    vistas = set()
    for nombre, fn in _publicas():
        for argumentos in ((FICHA,), ("ana",), (None,)):
            antes = len(registro.pedidos)
            await fn(*argumentos)
            if len(registro.pedidos) > antes:
                vistas.add(nombre)
    assert {n for n, _ in _publicas()} - vistas - _SIN_RED == set(), (
        "estas funciones públicas no salieron a la red con ninguna de las formas "
        "con que esta prueba las llama: si no salen, van a `_SIN_RED` declaradas")
    assert registro.pedidos, "no salió ni un pedido: la prueba no ejerció nada"

    for pedido in registro.pedidos:
        assert pedido["metodo"] == "GET", f"el lector mandó un {pedido['metodo']}"
        assert pedido["ruta"].split("?")[0] in _RUTAS_DEL_LECTOR, pedido["ruta"]
        assert pedido["cabeceras"]["Host"] == registro.url.replace("http://", ""), (
            "el pedido salió a otro sitio que el registro")
        assert LLAVE not in pedido["ruta"], "la llave viajó en la URL"


def test_la_unica_funcion_que_toca_la_red_es_get_y_solo_hace_get():
    """De todas las funciones del módulo, la única que toca la red es `_get`, y
    el único verbo que usa es `get`."""
    fuente = open(registro_lectura.__file__, encoding="utf-8").read()
    arbol = ast.parse(fuente)
    verbos = {"get", "post", "put", "patch", "delete", "request", "send", "stream",
              "head", "options"}
    por_funcion = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        clientes = {d.id for sub in ast.walk(nodo) if isinstance(sub, ast.AsyncWith)
                    for i in sub.items if isinstance(i.optional_vars, ast.Name)
                    and isinstance(i.context_expr, ast.Call)
                    and isinstance(i.context_expr.func, ast.Attribute)
                    for d in [i.optional_vars]}
        usados = sorted({sub.func.attr for sub in ast.walk(nodo)
                         if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                         and isinstance(sub.func.value, ast.Name)
                         and sub.func.value.id in clientes and sub.func.attr in verbos})
        if usados:
            por_funcion[nodo.name] = usados
    assert por_funcion == {"_get": ["get"]}, por_funcion


def test_la_direccion_sale_solo_de_registro_url(registro, monkeypatch):
    """Con `REGISTRO_URL` apuntando a otra App, el pedido va a ESA y no a la primera."""
    otra = AppDeRegistro()
    otra.llave = LLAVE
    otra.sesiones = _respuesta(sesiones=(SESION,))
    try:
        monkeypatch.setattr(config, "REGISTRO_URL", otra.url)
        with pagina(monkeypatch):
            assert _cliente().get("/proyectos?p=1").status_code == 200
        assert len(otra.pedidos) == 1 and registro.pedidos == []
    finally:
        otra.cerrar()


# ═══════════════════════════════════════════════════════════════════════
# Garantía: la llave no sale en ningún registro ni mensaje
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("como", ["ok", "401", "502", "html", "forma_rara", "sin_red"])
def test_la_llave_no_sale_en_ningun_registro_ni_en_la_pagina(registro, monkeypatch, caplog, como):
    """Ni en el registro, ni en el mensaje de un error, ni en la página, ni en la URL."""
    if como == "401":
        registro.llave = "otra-llave"
    elif como == "502":
        registro.forzada = _forzar(502, b'{"error": "no_se_pudo_leer"}')
    elif como == "html":
        registro.forzada = _forzar(200, b"<html>no soy JSON</html>", "text/html")
    elif como == "forma_rara":
        registro.forzada = _forzar(200, b'{"puede_ligar": "si"}')
    elif como == "sin_red":
        monkeypatch.setattr(config, "REGISTRO_URL", "http://127.0.0.1:1")
    registro.sesiones = _respuesta()
    with caplog.at_level("DEBUG"):
        html = _abrir(monkeypatch)
    assert LLAVE not in caplog.text, "la llave terminó en un registro"
    assert LLAVE not in html, "la llave terminó en la página"
    for pedido in registro.pedidos:
        assert LLAVE not in str(pedido["ruta"]), "la llave viajó en la URL"


def test_la_llave_es_la_que_abre_la_puerta_del_registro(registro, monkeypatch):
    """Con la llave buena el registro contesta; con otra, no. La cabecera es esa."""
    registro.sesiones = _respuesta(sesiones=(SESION,))
    html = _abrir(monkeypatch)
    assert "s011" in html
    assert registro.pedidos[0]["cabeceras"]["X-Lucy-Llave"] == LLAVE


# ═══════════════════════════════════════════════════════════════════════
# Garantía: si la App no contesta se dice, y la página carga
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("como", ["sin_llave", "401", "502", "html", "forma_rara",
                                  "sin_red", "tarda"])
def test_si_el_registro_no_contesta_la_pagina_carga_y_lo_dice(registro, monkeypatch, como):
    """Ni «no tiene sesiones» ni cifras en cero: que no se pudo preguntar."""
    if como == "sin_llave":
        monkeypatch.setattr(config, "LUCY_LLAVE_SERVICIO", "")
    elif como == "401":
        registro.llave = "otra-llave"
    elif como == "502":
        registro.forzada = _forzar(502, b'{"error": "no_se_pudo_leer"}')
    elif como == "html":
        registro.forzada = _forzar(200, b"<html>no soy JSON</html>", "text/html")
    elif como == "forma_rara":
        registro.forzada = _forzar(200, b'{"puede_ligar": "si", "sesiones": []}')
    elif como == "sin_red":
        monkeypatch.setattr(config, "REGISTRO_URL", "http://127.0.0.1:1")
    elif como == "tarda":
        registro.forzada = (200, b"{}", "application/json", 30.0)
        monkeypatch.setattr(registro_lectura, "TIEMPO_LIMITE", 0.3)

    html = _abrir(monkeypatch)
    bloque = _bloque(html)
    assert "No se pudo consultar el registro." in bloque, bloque
    assert "no tiene sesiones ni trabajos" not in bloque, bloque
    assert "s011" not in html


@pytest.mark.parametrize("url", ["http://[::1", "http://[", "https://", "http://a b/",
                                 "no-es-una-direccion"])
def test_una_direccion_mal_escrita_no_tumba_la_pagina(registro, monkeypatch, url):
    """Una dirección que el cliente HTTP no sabe leer es «no se pudo», no un 500.

    `httpx.InvalidURL` NO es un `httpx.HTTPError`: se comprueba acá para que se
    vea por qué esta prueba existe. El `httpx` sale del módulo bajo prueba y no
    de un `import httpx`, que en la suite entera es el módulo de mentira que
    dejan las pruebas que lo pisan al importarse (`test_noco_lectura` tiene el
    mismo cuidado).
    """
    httpx = registro_lectura.httpx
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)
    monkeypatch.setattr(config, "REGISTRO_URL", url)
    bloque = _bloque(_abrir(monkeypatch))
    assert "No se pudo consultar el registro." in bloque, bloque
    assert registro.pedidos == []


def test_una_redireccion_del_registro_no_se_sigue(registro, monkeypatch):
    """Un 3xx hacia otra dirección no se sigue: la llave no sale de `REGISTRO_URL`."""
    otra = AppDeRegistro()
    otra.llave = LLAVE
    otra.sesiones = _respuesta(sesiones=(SESION,))
    try:
        registro.forzada = (302, b"", "text/html", 0.0,
                            {"Location": otra.url + "/api/lucy/sesiones"})
        bloque = _bloque(_abrir(monkeypatch))
        assert otra.pedidos == [], "Lucy siguió la redirección y le llevó la llave a otra dirección"
        assert "No se pudo consultar el registro." in bloque, bloque
        assert "s011" not in bloque
    finally:
        otra.cerrar()


def test_sin_llave_no_se_le_pregunta_nada_al_registro(registro, monkeypatch):
    """La variable vacía no se convierte en un pedido sin cabecera."""
    monkeypatch.setattr(config, "LUCY_LLAVE_SERVICIO", "")
    html = _abrir(monkeypatch)
    assert registro.pedidos == []
    assert "No se pudo consultar el registro." in _bloque(html)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: solo se pregunta al abrir un proyecto, no al listar
# ═══════════════════════════════════════════════════════════════════════

def test_listar_no_le_pregunta_nada_al_registro_y_abrir_un_proyecto_una_vez(registro, monkeypatch):
    registro.sesiones = _respuesta()
    with pagina(monkeypatch):
        lista = _cliente().get("/proyectos?g=CDS")          # las tareas sueltas de un grupo
        assert lista.status_code == 200
        assert registro.pedidos == [], "la lista de un grupo le preguntó al registro"
        assert 'id="sesiones-y-trabajos"' not in lista.text

        abierto = _cliente().get("/proyectos?p=1")
        assert abierto.status_code == 200
        assert len(registro.pedidos) == 1, (
            f"abrir un proyecto hizo {len(registro.pedidos)} pedido(s): es uno solo")

        # Y la vista por omisión (que también es un proyecto, el primero de la lista): un pedido,
        # no uno por cada proyecto del modelo.
        registro.pedidos.clear()
        por_omision = _cliente().get("/proyectos")
        assert por_omision.status_code == 200
        assert len(registro.pedidos) == 1, len(registro.pedidos)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: un proyecto sin cliente no pregunta nada
# ═══════════════════════════════════════════════════════════════════════

def test_la_ficha_sale_de_la_base_por_el_camino_real(registro, monkeypatch, mundo):
    """El Id con el que se le pregunta al registro sale del SELECT de
    `pagina_de_proyectos` y pasa por `armar_pagina`.

    La base es la de SQLite con el esquema real y el SQL se ejecuta (como en las
    otras pruebas de la página: `tests/test_pagina_proyectos.py::mundo`), no un
    modelo ya armado a mano. El Id que se le pide al registro es el que quedó
    escrito en la base, leído de ahí.
    """
    mundo.proyecto(1, "Con cliente", area="CDS", cliente="Cliente X")
    esperado = mundo.con.execute(
        "SELECT cliente_noco_id FROM proyectos WHERE id = 1").fetchone()[0]
    assert esperado is not None, "el proyecto de prueba quedó sin ficha de cliente"
    registro.sesiones = _respuesta(sesiones=(SESION,))

    r = _cliente().get("/proyectos?p=1")
    assert r.status_code == 200, r.text[:300]
    assert [p["ruta"] for p in registro.pedidos] == [f"/api/lucy/sesiones?persona={esperado}"]
    assert "s011" in r.text, "la página no pintó lo que contestó el registro"


def test_un_proyecto_sin_cliente_no_pregunta_nada(registro, monkeypatch):
    html = _abrir(monkeypatch, noco_id=None)
    assert registro.pedidos == [], "le preguntó al registro sin cliente a quién"
    assert "Este proyecto no tiene cliente, así que no se le pregunta al registro." in _bloque(html)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: las canceladas no salen
# ═══════════════════════════════════════════════════════════════════════

def test_las_canceladas_no_salen(registro, monkeypatch):
    registro.sesiones = _respuesta(sesiones=(SESION, CANCELADA, TRABAJO))
    html = _abrir(monkeypatch)
    assert "s011" in html and "t012" in html
    assert "c013" not in html, "salió una sesión cancelada"


def test_un_renglon_que_no_sirve_no_se_pinta(registro, monkeypatch):
    """Lo que no es una fila, o no trae nada que enseñar, no se inventa."""
    registro.sesiones = _respuesta(sesiones=(SESION, "no soy una fila", {"ref": 14}))
    html = _abrir(monkeypatch)
    assert "s011" in html


# ═══════════════════════════════════════════════════════════════════════
# Garantía: el código se pinta como texto discreto al lado del concepto
# ═══════════════════════════════════════════════════════════════════════

def test_el_codigo_va_discreto_al_lado_del_concepto(registro, monkeypatch):
    registro.sesiones = _respuesta(sesiones=(SESION,))
    bloque = _bloque(_abrir(monkeypatch))
    assert re.search(r'<span class="que">[^<]*Grabación<span class="cod">s011</span></span>',
                     bloque), bloque
    assert "Sala P · 3 h · Grabación" in bloque, bloque


def _claves_que_el_lector_lee() -> list[str]:
    """Las claves de la respuesta que `_renglon` mira, sacadas de su árbol
    sintáctico: un campo nuevo que el lector deje pasar entra acá solo."""
    fuente = open(registro_lectura.__file__, encoding="utf-8").read()
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_renglon":
            return sorted({n.args[0].value for n in ast.walk(nodo)
                           if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute)
                           and n.func.attr == "get" and n.args
                           and isinstance(n.args[0], ast.Constant)
                           and isinstance(n.args[0].value, str)})
    raise AssertionError("no encontré `_renglon` en el lector")


HOSTIL = "<b>hostil</b>"


@pytest.mark.parametrize("campo", _claves_que_el_lector_lee())
def test_lo_que_manda_el_registro_sale_escapado(registro, monkeypatch, campo):
    """Cada campo que el bloque pinta, con un valor hostil: sale escapado.

    El campo en juego es el ÚNICO con algo en la fila, porque la plantilla
    esconde unos detrás de otros (el asignado detrás del «atendió»).
    """
    fila = dict.fromkeys(_claves_que_el_lector_lee())
    fila.update(cancelada=False, es_trabajo=False)
    fila[campo] = HOSTIL
    registro.sesiones = _respuesta(sesiones=(fila,))
    bloque = _bloque(_abrir(monkeypatch))
    assert HOSTIL not in bloque, f"{campo!r} salió crudo: {bloque}"

    renglon = registro_lectura._renglon(fila) or {}
    if any("hostil" in v for v in renglon.values() if isinstance(v, str)):
        assert "&lt;b&gt;hostil&lt;/b&gt;" in bloque, f"{campo!r} no salió escapado: {bloque}"


def _campos_que_quitadas_pinta() -> list[str]:
    """Los campos que la plantilla escribe en la lista «Quitadas», sacados de su fuente: los
    `q.renglon.…` del renglón, más el `ref` de la decisión (el `value` del formulario de devolver).
    """
    fuente = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    trozo = fuente.split("<h3>Quitadas</h3>", 1)[1].split("{% endfor %}", 1)[0]
    return sorted(set(re.findall(r"q\.renglon\.(\w+)", trozo)) | {"ref"})


@pytest.mark.parametrize("campo", _campos_que_quitadas_pinta())
def test_lo_que_la_lista_quitadas_pinta_sale_escapado(registro, monkeypatch, campo):
    """Cada campo que la lista «Quitadas» escribe, con un valor hostil: sale escapado.

    «Quitadas» (parte 11) pinta los mismos campos que la lista con otros nombres de variable
    (`q.renglon.…`), y solo sale si la fila está quitada de este proyecto: por eso necesita su
    propio recorrido y no alcanza con el de arriba. El campo en juego es el ÚNICO con algo en la
    fila (la plantilla esconde unos detrás de otros).
    """
    fila = dict.fromkeys(_claves_que_el_lector_lee())
    fila.update(cancelada=False, es_trabajo=False, ref=11, horas=3)
    fila[campo] = HOSTIL
    monkeypatch.setattr(pp, "QUITADAS_DEL_MODELO",
                        {1: {registro_lectura._ref(fila["ref"]): "s011"}})
    registro.sesiones = _respuesta(sesiones=(fila,))
    bloque = _bloque(_abrir(monkeypatch))
    assert "<h3>Quitadas</h3>" in bloque, bloque
    en_quitadas = bloque.split("<h3>Quitadas</h3>", 1)[1]
    assert HOSTIL not in en_quitadas, f"{campo!r} salió crudo en «Quitadas»: {en_quitadas}"

    renglon = registro_lectura._renglon(fila) or {}
    if any("hostil" in v for v in renglon.values() if isinstance(v, str)):
        assert "&lt;b&gt;hostil&lt;/b&gt;" in en_quitadas, (
            f"{campo!r} no salió escapado en «Quitadas»: {en_quitadas}")


# ═══════════════════════════════════════════════════════════════════════
# Lo que la App dice de una ficha (y que no es «no tiene sesiones»)
# ═══════════════════════════════════════════════════════════════════════

def test_sin_sesiones_y_sin_poder_ligar_se_dicen_distinto(registro, monkeypatch):
    registro.sesiones = _respuesta(sesiones=())
    vacio = _bloque(_abrir(monkeypatch))
    assert "El registro no tiene sesiones ni trabajos de este cliente." in vacio

    registro.sesiones = _respuesta(sesiones=(), puede_ligar=False, motivo="sin_telefono")
    sin_ligar = _bloque(_abrir(monkeypatch))
    assert "la ficha no tiene teléfono allá" in sin_ligar, sin_ligar
    assert "no tiene sesiones ni trabajos" not in sin_ligar

    registro.sesiones = _respuesta(sesiones=(), puede_ligar=False, motivo="ficha_inexistente")
    otro = _bloque(_abrir(monkeypatch))
    assert "El registro no puede ligar las sesiones de este cliente." in otro, otro
    assert "la ficha no tiene teléfono allá" not in otro


def test_un_campo_que_no_viene_no_se_pinta(registro, monkeypatch):
    """Sin sala, sin horas, sin concepto, sin quién: no se inventa nada."""
    registro.sesiones = _respuesta(sesiones=({"ref": 15, "codigo": "v015", "es_trabajo": False},))
    bloque = _bloque(_abrir(monkeypatch))
    assert "v015" in bloque
    for invento in ("sin asignar", "atendió", " h", "sin fecha"):
        assert invento not in bloque, f"el renglón se inventó {invento!r}"


# ═══════════════════════════════════════════════════════════════════════
# El lector, sin la página de por medio
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ficha", [None, 0, -3, "77", 77.0, True])
async def test_sin_ficha_no_le_pregunta_a_nadie(registro, ficha):
    respuesta = await registro_lectura.sesiones_de_cliente(ficha)
    assert respuesta["estado"] == "sin_cliente" and respuesta["sesiones"] == []
    assert registro.pedidos == []


async def test_lo_que_devuelve_el_lector_es_lo_que_la_pagina_pinta(registro):
    """Las dos listas, partidas por `es_trabajo`, con sus cifras y con el `ref` (parte 11: es con
    lo que se guarda la decisión de quitarla, y viaja al formulario)."""
    registro.sesiones = _respuesta(sesiones=(SESION, CANCELADA, TRABAJO))
    respuesta = await registro_lectura.sesiones_de_cliente(FICHA)
    assert respuesta["estado"] == "ok"
    assert [r["codigo"] for r in respuesta["sesiones"]] == ["s011"]
    assert [r["codigo"] for r in respuesta["trabajos"]] == ["t012"]
    for renglon in respuesta["sesiones"] + respuesta["trabajos"]:
        assert set(renglon) == {"ref", "fecha", "sala", "horas", "concepto", "codigo",
                                "atendio", "asignado", "estado", "es_trabajo",
                                "total", "abonado", "saldo", "dinero"}
        assert renglon["fecha"].isoformat() == HOY
    assert respuesta["sesiones"][0]["ref"] == "11", "el ref de la App no sale en texto"


async def test_la_pregunta_lleva_la_ficha_del_cliente(registro):
    await registro_lectura.sesiones_de_cliente(FICHA)
    assert registro.pedidos[0]["ruta"] == f"/api/lucy/sesiones?persona={FICHA}"


# ═══════════════════════════════════════════════════════════════════════
# Se ve igual en la sesión de la casa y en la de solo ver
# ═══════════════════════════════════════════════════════════════════════

def _sin_los_controles(bloque: str) -> str:
    """El bloque sin los formularios (parte 11: quitar y devolver son de la casa) ni el buscador
    (parte 13: es del mismo lado, y va al final del bloque). Lo que se compara entre las dos sesiones
    es lo que se VE —los renglones, el dinero, los totales, «Quitadas» y los avisos—, que no cambia;
    los controles son lo único que la sesión de solo ver no lleva."""
    sin_formularios = re.sub(r"<form\b.*?</form>", " ", bloque, flags=re.S)
    return " ".join(sin_formularios.split('<div class="buscar-sesion">')[0].split())


def test_el_bloque_se_ve_igual_en_la_casa_y_en_solo_ver(registro, monkeypatch):
    """Lo que se VE (renglones, dinero y totales) es igual; los controles son solo de la casa
    (parte 11: quitar y devolver), y la sesión de solo ver no lleva ninguno."""
    registro.sesiones = _respuesta(sesiones=(SESION, TRABAJO))
    casa = _abrir(monkeypatch, con="casa")
    ver = _abrir(monkeypatch, con="ver")
    assert _bloque(ver) == _sin_los_controles(_bloque(casa)), (
        "lo que se ve no es igual en las dos sesiones")
    assert "<form" in _bloque(casa), "la casa no tiene los controles de quitar (¿se perdió el camino?)"
    assert "s011" in ver
    for control in ("<form", "<button", "<a ", "<script", "<input", "<select"):
        assert control not in _bloque(ver), f"el bloque de solo ver trae {control}"
