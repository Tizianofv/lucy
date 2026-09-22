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

TERCERA VUELTA, mismo día — otro NO PASA, y era real. Todas las pruebas
de arriba llaman a `_aplicar_el_arreglo()` ELLAS MISMAS antes de medir.
Eso prueba que el MECANISMO funciona cuando se lo llama — no prueba que
CADA PUNTO DE ARRANQUE de verdad lo llame. Un testigo borró
`sin_preparadas.aplicar()` de `db/db.py` y la suite entera siguió en
verde (`709 passed`): dentro del mismo proceso de pytest, alguna OTRA
suite —o esta misma, con su propio `_aplicar_el_arreglo()`— ya había
aplicado el parche de forma GLOBAL para toda la sesión, así que la falta
de la línea en `db/db.py` quedó tapada. Medido aparte, en un proceso que
SOLO importa `db.db` sin llamar nada más: con la línea borrada, el
default queda en `5`.

La sección «PUNTOS DE ARRANQUE» de más abajo corrige esto: por cada
archivo que de verdad abre una conexión (más `main.py`, el arranque real
del bot), un PROCESO NUEVO —`subprocess`, mismo intérprete— que solo
IMPORTA ese archivo, sin llamar a `aplicar()` a mano, y lee el default
desde ahí. Lo que haya aplicado cualquier otra cosa no puede tapar esto,
porque no hay ninguna otra cosa en ese proceso.

Y la frase de la sección de arriba que decía
«`psycopg.connect is psycopg.Connection.connect # True`» — TAMBIÉN
medida por el mismo testigo, y también falsa: es un `classmethod`, y cada
lectura por atributo crea un "bound method" nuevo. Lo que SÍ es el mismo
objeto en cualquier lectura es `__func__` (la función de adentro) y su
`__kwdefaults__`. La corrección completa, con la medición, está en
`db/sin_preparadas.py`.

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

── PUNTOS DE ARRANQUE, la sección que agregó la tercera vuelta ──────────

  QUÉ SE MIDE: por cada archivo que de verdad puede ser el primer código
  de Lucy en correr en un proceso —`main.py`, más todo `.py` fuera de
  `tests/` que abre una conexión por su cuenta—, un PROCESO NUEVO que
  SOLO importa ese archivo (nada de la suite, nada de `_aplicar_el_
  arreglo()`) y lee `Connection.connect.__kwdefaults__["prepare_
  threshold"]` y el de `AsyncConnection` apenas termina el import. Si el
  archivo no deja los dos en `None` por sí solo, esto es rojo — sin
  importar qué haya aplicado cualquier otra cosa en cualquier otro
  proceso.

  DE DÓNDE SALE LA LISTA, y por qué no está tecleada: `_puntos_de_
  arranque()` recorre TODOS los `.py` fuera de `testpaths`
  (`test_buzon_que_no_se_ve._py_en_disco`, la misma puerta que usa el
  resto de la suite) y se queda con los que tengan una llamada que abre
  una conexión —`Connection`/`AsyncConnection`/`connect`/`AsyncConnectionPool`
  y las demás clases de pool, derivadas del PAQUETE INSTALADO por la
  misma `_nombres_de_pool`/`_nombres_de_atajo` de arriba, no una lista de
  nombres escrita a mano—, y le suma `main.py` aparte, porque ÉSE no abre
  la conexión él mismo: la abre transitivamente, importando `db.db`, y es
  el arranque real del proceso del bot. Un guion nuevo en `tools/` que
  abra su propia conexión mañana ENTRA SOLO en la lista la próxima vez
  que esto corra — no hay que acordarse de agregarlo.

  CÓMO SE MIDE SIN QUE EL GUION HAGA TRABAJO DE VERDAD: tres de los
  cuatro archivos que abren conexión (`db/db.py`, `db/backup.py`,
  `tools/rellenar_categorias.py`) guardan su lógica bajo
  `if __name__ == "__main__":`, así que IMPORTARLOS no corre nada más
  que las líneas de arriba —donde vive `sin_preparadas.aplicar()`—. El
  cuarto, `tools/verificar_respaldo.py`, llama `sys.exit(main())` SIN esa
  guardia (hallazgo lateral, no arreglado acá: ver el reporte), así que
  importarlo SÍ ejecuta `main()` — y con `DATABASE_URL` apuntando a una
  base que no existe, intenta conectarse de verdad y falla con
  `OperationalError`. Da igual: `sin_preparadas.aplicar()` está en el
  nivel del módulo, ANTES de que `main()` se llame, así que el valor ya
  quedó fijado antes de esa falla. El proceso hijo atrapa CUALQUIER
  excepción de la importación —no le importa cuál, ni si es la falla de
  red esperada o alguna otra— y sigue: lo único que necesita es que el
  `__kwdefaults__` ya esté escrito, y eso pasó antes.

Correr:  python3 -m pytest tests/test_sin_preparadas.py
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import os
import subprocess
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
PSYCOPG_POOL = _modulo_de_verdad("psycopg_pool")


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


# ── PUNTOS DE ARRANQUE: un proceso nuevo por archivo, nada llamado a mano ──

def _nombres_de_pool() -> set[str]:
    """Las clases de `psycopg_pool` que abren conexiones: tienen un
    `.connection` invocable — derivado del paquete instalado (vía
    `PSYCOPG_POOL`, ya resuelto contra dobles de otras suites), no
    tecleado."""
    return {
        nombre for nombre in dir(PSYCOPG_POOL)
        if not nombre.startswith("__")
        and inspect.isclass(getattr(PSYCOPG_POOL, nombre, None))
        and callable(getattr(getattr(PSYCOPG_POOL, nombre), "connection", None))
    }


def _nombres_de_atajo() -> set[str]:
    """Los atributos de `psycopg` que son un método ya vinculado a una
    subclase de `BaseConnection` — `psycopg.connect` es exactamente
    esto. Mismo comentario que `_nombres_de_pool()` sobre por qué no hace
    falta el módulo resuelto acá."""
    salida = set()
    due = getattr(PSYCOPG, "BaseConnection", None)
    if due is None:
        return salida
    for nombre in dir(PSYCOPG):
        obj = getattr(PSYCOPG, nombre, None)
        clase = getattr(obj, "__self__", None)
        if inspect.ismethod(obj) and inspect.isclass(clase) and issubclass(clase, due):
            salida.add(nombre)
    return salida


def _nombre_llamado(nodo: ast.Call) -> str | None:
    """El nombre por el que se llamó a algo —atributo o nombre suelto—, o
    None si la llamada no tiene una forma reconocible (`f()()`, …)."""
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    return None


def _abre_conexion(arbol: ast.AST) -> bool:
    """True si el árbol de sintaxis de un archivo tiene AL MENOS una
    llamada cuyo nombre es uno de los que abren una conexión —derivados
    del paquete instalado, ver `_nombres_de_pool()` / `_nombres_de_atajo()`
    más arriba—. No importa si lleva `prepare_threshold` o no: eso ya no
    lo mide esta parte del archivo (lo miden las pruebas del `connect()`
    real, arriba). Acá solo se usa para saber QUÉ ARCHIVOS vale la pena
    correr en un proceso aparte.
    """
    nombres = _nombres_de_pool() | _nombres_de_atajo()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and _nombre_llamado(nodo) in nombres:
            return True
    return False


def _puntos_de_arranque() -> list[Path]:
    """Los `.py` fuera de `testpaths` que de verdad pueden ser el primer
    código de Lucy en correr: los que abren una conexión por su cuenta
    (derivado, recorriendo el disco — ver `_abre_conexion`), más
    `main.py`, que es el arranque real del proceso del bot aunque la
    conexión la abra `db.db` por dentro.
    """
    import test_buzon_que_no_se_ve as barrido

    raiz = RAIZ.resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    encontrados: list[Path] = []
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        try:
            arbol = ast.parse(real.read_text(encoding="utf-8"), str(real))
        except SyntaxError:
            continue
        if _abre_conexion(arbol):
            encontrados.append(real)

    main_py = (raiz / "main.py").resolve()
    if main_py.is_file() and main_py not in encontrados:
        encontrados.append(main_py)

    return sorted(encontrados)


def _modulo_de(archivo: Path) -> str:
    """El nombre de import punteado de `archivo`, relativo a RAIZ —
    `db/db.py` -> `"db.db"`, `main.py` -> `"main"`."""
    rel = archivo.resolve().relative_to(RAIZ.resolve())
    partes = rel.with_suffix("").parts
    return ".".join(partes)


def _medir_en_proceso_aislado(archivo: Path) -> tuple[int | None, int | None]:
    """Arranca un proceso NUEVO —mismo intérprete, `subprocess`— que SOLO
    importa `archivo` y nada más de este repositorio: no llama a
    `_aplicar_el_arreglo()`, no importa este archivo de pruebas, no
    comparte memoria con pytest. Devuelve (default sync, default async)
    leídos justo después de que termine el import.

    `DATABASE_URL` se deja SIN PONER a propósito: es la comprobación con la
    que arranca `main()` de los guiones que no tienen guardia de
    `__name__` (`tools/verificar_respaldo.py`, hallazgo lateral — ver el
    reporte), y corta ANTES de tocar Postgres. `sys.exit` se reemplaza por
    algo que se pueda atrapar, así el proceso no termina antes de que se
    pueda leer el `__kwdefaults__`.
    """
    modulo = _modulo_de(archivo)
    script = f"""
import os, sys
os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://usuario:clave@localhost/basequenoexiste")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
sys.path.insert(0, {str(RAIZ.resolve())!r})

class _Salida(Exception):
    pass

def _exit_falso(code=0):
    raise _Salida(code)

sys.exit = _exit_falso
try:
    import {modulo}
except BaseException:
    # No importa POR QUÉ `main()` no pudo terminar -DATABASE_URL apunta a
    # una base que no existe, y algunos guiones intentan conectarse de
    # verdad antes de que este proceso llegue a leer nada-. Lo que importa
    # es que `sin_preparadas.aplicar()`, si el archivo la llama, corre
    # ANTES de esa falla -está en el nivel del módulo, antes de cualquier
    # `main()`-, así que el valor ya quedó fijado pase lo que pase después.
    pass

import psycopg
print("SYNC", psycopg.Connection.connect.__kwdefaults__.get("prepare_threshold"))
print("ASYNC", psycopg.AsyncConnection.connect.__kwdefaults__.get("prepare_threshold"))
"""
    r = subprocess.run([sys.executable, "-c", script], cwd=str(RAIZ.resolve()),
                       capture_output=True, text=True, timeout=30)
    valores: dict[str, str] = {}
    for linea in r.stdout.splitlines():
        partes = linea.split(maxsplit=1)
        if len(partes) == 2 and partes[0] in ("SYNC", "ASYNC"):
            valores[partes[0]] = partes[1]
    if "SYNC" not in valores or "ASYNC" not in valores:
        raise AssertionError(
            f"el proceso aislado para {modulo} no llegó a imprimir los dos "
            f"valores (exit={r.returncode}).\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr[-4000:]}")

    def _leer(s: str) -> int | None:
        return None if s == "None" else int(s)

    return _leer(valores["SYNC"]), _leer(valores["ASYNC"])


def test_los_puntos_de_arranque_no_estan_vacios():
    """La guarda de la guarda: si `_puntos_de_arranque()` da vacío, el
    resto de esta sección pasaría en verde sin haber corrido nada."""
    puntos = _puntos_de_arranque()
    assert puntos, "no se derivó ningún punto de arranque: revisar _abre_conexion"
    relativos = {p.relative_to(RAIZ.resolve()).as_posix() for p in puntos}
    esperados = {"main.py", "db/db.py", "db/backup.py",
                "tools/rellenar_categorias.py", "tools/verificar_respaldo.py"}
    assert esperados <= relativos, (
        f"faltan puntos de arranque conocidos: {esperados - relativos}")


def test_cada_punto_de_arranque_deja_sin_preparar_SOLO_con_importarlo():
    """LA PRUEBA QUE ATRAPA EL SEGUNDO NO PASA. Por cada punto de arranque,
    un proceso nuevo que solo lo importa — sin llamar a `aplicar()` a
    mano, sin que ninguna otra suite haya corrido antes en ese proceso —
    tiene que dar `None` en los dos defaults. Si a alguno de
    `db/db.py`, `db/backup.py`, `tools/rellenar_categorias.py` o
    `tools/verificar_respaldo.py` le borraran su `sin_preparadas.
    aplicar()`, esto se pone rojo SOLO con ese archivo — no hace falta que
    ningún otro archivo lo delate."""
    fallos = []
    for archivo in _puntos_de_arranque():
        rel = archivo.relative_to(RAIZ.resolve()).as_posix()
        sync, asinc = _medir_en_proceso_aislado(archivo)
        if sync is not None:
            fallos.append(f"{rel}: sync quedó en {sync}, no None")
        if asinc is not None:
            fallos.append(f"{rel}: async quedó en {asinc}, no None")
    assert not fallos, (
        "estos puntos de arranque preparan consultas apenas se importan, "
        "en un proceso propio, sin que nada más los proteja:\n  "
        + "\n  ".join(fallos))


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
