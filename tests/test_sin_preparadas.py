# -*- coding: utf-8 -*-
r"""Ninguna conexión de Lucy prepara consultas — mide el HECHO, no el texto.

SEGUNDA VUELTA, 22-sep-2026. La primera versión de este archivo (ver
`55ad0a2`) recorría el árbol de sintaxis del repositorio buscando el texto
`prepare_threshold=None` en cada llamada que abre una conexión. El
testigo la rompió así: creó un archivo con
`from psycopg import connect as conectar_a_la_base` y llamó a ese alias
sin el keyword. El código real habría preparado consultas igual que
antes del encargo, y la prueba de texto —que solo sabe buscar el nombre
`connect`, `AsyncConnectionPool`, etc.— no lo veía: medía CÓMO ESTABA
ESCRITA la llamada, no qué hace. Es la trampa de "puerta única reconocida
por un texto", la misma familia que ya rompió guardas de Max y Natalia
contra la puerta a Telegram.

EL ARREGLO DE FONDO no está acá: está en `db/sin_preparadas.py`. En vez
de un keyword por cada sitio que abre una conexión, hay un ÚNICO cambio
sobre la función `connect` misma —`Connection.connect.__kwdefaults__` y
`AsyncConnection.connect.__kwdefaults__`—, así que CUALQUIER nombre que
apunte a esa función —`psycopg.connect`, `Connection.connect`, un alias
viejo o uno que alguien escriba mañana— ve el mismo default cambiado,
porque Python resuelve los valores por omisión AL LLAMAR, leyendo esa
misma estructura, no al importar el alias. No hay lista de alias que
mantener porque no hay alias que reconocer: hay una función, y se le
cambió el default.

POR ESO ESTA PRUEBA YA NO LEE TEXTO. Corre la función `connect` DE
VERDAD —con la espera de red apagada, ver LA FRONTERA más abajo— y mira
el resultado en el objeto de conexión real, para varias formas de
llamarla, incluida una alias fabricada en el momento (la forma exacta que
usó el testigo). Si `db/sin_preparadas.py` dejara de aplicarse, o si
alguien reescribiera `connect` de una forma que ya no tenga
`__kwdefaults__`, estas pruebas se rompen SOLAS: no hace falta acordarse
de agregar un caso nuevo.

── LA FRONTERA, dicha para que se pueda predecir sin correr nada ────────

  QUÉ SE MIDE: el valor que la función `connect()` REAL le asigna a
  `.prepare_threshold` del objeto que devuelve, para una llamada sin ese
  keyword (tiene que dar `None`) y para una CON el keyword explícito
  (tiene que respetarlo — sin esto, una prueba que solo comprobara "da
  None" no distinguiría "funciona bien" de "siempre da None pase lo que
  pase", que sería otro defecto).

  CÓMO SE CORRE SIN POSTGRES: `connect()` hace, en orden, (1) resolver
  parámetros de conexión, (2) abrir la conexión de verdad —lo único que
  necesita red—, (3) asignar `.prepare_threshold` y otros atributos sobre
  el objeto ya conectado. El paso (2) vive en `psycopg.waiting.wait_conn`
  (síncrono) y `psycopg.waiting.wait_conn_async` — un doble que devuelve
  un objeto vacío al instante, sin tocar la red, dejando correr (1) y (3)
  con código real. Medido: sin el doble, `connect("postgresql://x/y")`
  falla ANTES de intentar conectar (no resuelve el host `x`); con
  `localhost` como host llega hasta pedir la conexión real y con el doble
  puesto no hace falta ni eso.

  QUÉ NO SE PUEDE VER ASÍ: que una conexión REALMENTE ABIERTA contra un
  Postgres de verdad no prepare — eso pide un servidor, y en esta Mac no
  hay uno. Lo que sostiene esa mitad es la lectura de la librería
  (`psycopg/_preparing.py`, citada en `db/sin_preparadas.py`), no esta
  prueba. Lo que ESTA prueba sostiene es que el VALOR que `connect()` le
  pone a `.prepare_threshold` —lo único que decide si más tarde se
  prepara o no— es `None`, para cualquier forma de llamar a la función.

Correr:  python3 -m pytest tests/test_sin_preparadas.py
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

RAIZ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Host que resuelve sin red de verdad (está en /etc/hosts de cualquier
# máquina): hace falta para que `connect()` llegue hasta el paso que se
# dobla más abajo. No se conecta nada: el doble de la espera corta antes.
_HOST_QUE_RESUELVE = "postgresql://usuario:clave@localhost/basequenoexiste"


def _modulo_de_verdad(nombre: str):
    """`nombre` de VERDAD, apartando cualquier doble que otra suite haya
    dejado en `sys.modules`.

    La mayoría de las suites de este repositorio reemplazan `psycopg` (y
    `psycopg_pool`) por un `types.ModuleType` vacío —`sys.modules.setdefault
    ("psycopg", ...)`, ver la cabecera de cualquier `tests/test_*.py`— para
    poder correr sin Postgres. `setdefault` significa que GANA LA PRIMERA:
    cuando pytest recolecta el árbol entero, para cuando le toca el turno a
    este archivo ya puede haber un doble instalado. Mismo síntoma y misma
    solución que `tests/test_cerrar_varias.py::_buscar_modulo_de_verdad`
    —re-importar apartando la familia—, reimplementado acá suelto para no
    arrastrar el resto de ese archivo, que resuelve un problema distinto.
    """
    familia = nombre.split("_")[0]
    apartados = {n: m for n, m in list(sys.modules.items())
                if n == familia or n.startswith(familia + ".")
                or n.startswith(familia + "_")}
    for n in apartados:
        del sys.modules[n]
    try:
        return importlib.import_module(nombre)
    finally:
        for n, m in apartados.items():
            sys.modules[n] = m


PSYCOPG = _modulo_de_verdad("psycopg")


@contextlib.contextmanager
def _sin_red():
    """Mientras dure el `with`, `connect()` (sync y async) no toca la red:
    `psycopg.waiting.wait_conn` / `wait_conn_async` devuelven un objeto
    vacío al instante. Todo lo demás de `connect()` corre real. Ver LA
    FRONTERA en el docstring del módulo.
    """
    waiting = PSYCOPG.waiting
    orig_sync = waiting.wait_conn
    orig_async = waiting.wait_conn_async

    def _stub_sync(gen, interval=None):
        return types.SimpleNamespace(_autocommit=False)

    async def _stub_async(gen, interval=None):
        return types.SimpleNamespace(_autocommit=False)

    waiting.wait_conn = _stub_sync
    waiting.wait_conn_async = _stub_async
    try:
        yield
    finally:
        waiting.wait_conn = orig_sync
        waiting.wait_conn_async = orig_async


def _aplicar_el_arreglo():
    """Llama a `db.sin_preparadas.aplicar()` contra el psycopg REAL —no el
    que `db/sin_preparadas.py` haya importado en SU momento, que puede ser
    un doble por el mismo problema de orden que resuelve `_modulo_de_verdad`.
    """
    import db.sin_preparadas as sin_preparadas
    sin_preparadas.aplicar(modulo=PSYCOPG)


def test_psycopg_de_verdad_tiene_lo_que_esta_prueba_necesita():
    """La guarda de la guarda: si esto no se cumple, el resto de este
    archivo pasaría en verde sin haber corrido nada de verdad."""
    assert hasattr(PSYCOPG, "Connection") and hasattr(PSYCOPG, "AsyncConnection"), (
        "no se pudo resolver el psycopg real (¿sigue habiendo un doble en "
        "sys.modules que _modulo_de_verdad no apartó?)")
    assert hasattr(PSYCOPG.waiting, "wait_conn") and hasattr(
        PSYCOPG.waiting, "wait_conn_async")


def test_connect_sync_sin_pedirlo_da_none():
    """`psycopg.connect(...)` — la forma que usan db/backup.py y los dos
    guiones de tools/ — sin decir `prepare_threshold`, corriendo de
    verdad, da `None`."""
    _aplicar_el_arreglo()
    with _sin_red():
        conn = PSYCOPG.connect(_HOST_QUE_RESUELVE)
    assert conn.prepare_threshold is None


def test_connect_async_sin_pedirlo_da_none():
    """`AsyncConnection.connect(...)` — la que usa el pool de db/db.py por
    dentro — sin decir `prepare_threshold`, corriendo de verdad, da
    `None`."""
    import asyncio

    _aplicar_el_arreglo()

    async def _correr():
        with _sin_red():
            return await PSYCOPG.AsyncConnection.connect(_HOST_QUE_RESUELVE)

    conn = asyncio.new_event_loop().run_until_complete(_correr())
    assert conn.prepare_threshold is None


def test_un_alias_nuevo_TAMBIEN_da_none_sin_pedirlo():
    """LA PRUEBA QUE ATRAPA EL HALLAZGO DEL TESTIGO. Fabrica un alias
    fresco —un nombre nuevo que apunta a la MISMA función, la forma exacta
    que rompió la versión anterior (`from psycopg import connect as
    <nombre>`)— DESPUÉS de aplicar el arreglo, y confirma que ese alias
    también da `None` sin pedirlo. Si `db/sin_preparadas.py` volviera a
    depender de tocar cada sitio de llamada por su texto, este alias
    nunca habría sido tocado y esto se pondría rojo.

    POR QUÉ ACÁ ES `PSYCOPG.connect` Y NO UN `from psycopg import connect
    as ...` LITERAL: un `from ... import` vuelve a mirar `sys.modules`
    EN ESE INSTANTE, y `_modulo_de_verdad` (arriba) restaura los dobles de
    otras suites ahí apenas termina de resolver el real — es la misma
    protección que necesita para no ensuciarle el aislamiento a las demás
    pruebas. Un `import` literal DESPUÉS de esa resolución puede volver a
    agarrar un doble sin que esto sea un defecto del arreglo. `PSYCOPG`
    es la referencia YA resuelta al paquete real, y `PSYCOPG.connect` es
    EL MISMO objeto función que produciría ese `from...import` si
    `sys.modules` estuviera limpio en ese momento — capturarlo por
    atributo en vez de por sentencia de import prueba exactamente lo
    mismo que probaba el ataque del testigo: un nombre nuevo, capturado
    después del arreglo, viendo el default cambiado."""
    _aplicar_el_arreglo()
    conectar_a_la_base = PSYCOPG.connect  # el alias del testigo, por atributo
    with _sin_red():
        conn = conectar_a_la_base(_HOST_QUE_RESUELVE)
    assert conn.prepare_threshold is None


def test_un_alias_de_la_clase_async_tambien_da_none():
    """El mismo ataque, sobre la clase ASYNC —la que usa el pool—: un
    alias nuevo, fabricado después del arreglo, con un nombre que no
    existe en ningún lado del código de Lucy."""
    import asyncio

    _aplicar_el_arreglo()
    conectar_como_quiera_el_que_lo_escriba = PSYCOPG.AsyncConnection.connect

    async def _correr():
        with _sin_red():
            return await conectar_como_quiera_el_que_lo_escriba(_HOST_QUE_RESUELVE)

    conn = asyncio.new_event_loop().run_until_complete(_correr())
    assert conn.prepare_threshold is None


def test_pedirlo_explicito_TODAVIA_se_respeta():
    """CONTROL POSITIVO. Si esta prueba diera `None` incluso pidiendo otra
    cosa, `aplicar()` no estaría cambiando un DEFAULT: estaría rompiendo
    la posibilidad de pedir lo contrario, que no es lo que se pidió y
    escondería un defecto distinto detrás de un verde."""
    _aplicar_el_arreglo()
    with _sin_red():
        conn = PSYCOPG.connect(_HOST_QUE_RESUELVE, prepare_threshold=7)
    assert conn.prepare_threshold == 7, (
        "pedir un prepare_threshold explícito dejó de respetarse: el "
        "arreglo cambia el DEFAULT, no debería impedir pedir otra cosa")


def test_aplicar_es_idempotente_y_no_revienta_con_un_psycopg_de_mentira():
    """`db/sin_preparadas.py::aplicar()` la llaman `db/db.py`, `db/backup.py`
    y los dos guiones de `tools/` — y también casi toda la suite de
    pruebas del repositorio, indirectamente, al importar `db.db` sobre un
    `psycopg` de mentira (`sys.modules.setdefault(...)`). Tiene que poder
    correr dos veces seguidas y no reventar cuando `psycopg` no tiene
    `Connection` ni `AsyncConnection` — si reventara, CUALQUIER prueba de
    este repositorio que importe `db.db` se caería."""
    import db.sin_preparadas as sin_preparadas

    doble = types.ModuleType("psycopg_de_mentira")
    sin_preparadas.aplicar(modulo=doble)  # no debe reventar
    sin_preparadas.aplicar(modulo=doble)  # ni la segunda vez

    _aplicar_el_arreglo()
    _aplicar_el_arreglo()  # idempotente sobre el real también


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
