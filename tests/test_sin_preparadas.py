# -*- coding: utf-8 -*-
r"""Ninguna conexión de Lucy prepara consultas (encargo 3).

POR QUÉ, medido el 10-sep-2026: psycopg prepara una consulta repetida a
partir de la quinta vez que la ve IDÉNTICA en la misma conexión
(`prepare_threshold`, default 5 — `psycopg/_preparing.py:36` del paquete
instalado) y le deja a Postgres un PLAN guardado con la forma del
resultado. Si a `tareas` se le agrega una columna MIENTRAS ese plan sigue
vivo, la siguiente ejecución revienta con
`psycopg.errors.FeatureNotSupported: cached plan must not change result
type` — pasó de verdad publicando el Responsable de Lucy, contra el
`SELECT * FROM tareas` de `cerebro/despertador.py:519`
(`_reprogramar_recurrentes`), que corre en cada vuelta del despertador.

`prepare_threshold=None` apaga la preparación entera. Leído en la fuente
del paquete instalado (no medido contra un Postgres real: en esta Mac no
hay uno):

  · `psycopg/_preparing.py:63` — `PrepareManager.get`: `if prepare is
    False or self.prepare_threshold is None: return Prepare.NO, b""`. Con
    `None`, ninguna consulta llega a pedir un plan.
  · `psycopg/_preparing.py:133` — `maybe_add_to_cache` también corta de
    entrada con `if self.prepare_threshold is None: return None`: ni
    siquiera se cuenta cuántas veces se repitió una consulta.

DÓNDE SE APAGA, las tres formas que hay en este repositorio HOY —medidas
con `grep` sobre `psycopg.connect(` y `ConnectionPool(` en todo el árbol,
fuera de `tests/`—, y las tres van con `prepare_threshold`:

  · `db/db.py:78` — el pool de la app (`AsyncConnectionPool`, abierto al
    arrancar el bot). Un pool no tiene `prepare_threshold` propio: se lo
    pasa a CADA conexión que abre a través de su parámetro `kwargs`
    (`psycopg_pool/pool_async.py:650`,
    `self.connection_class.connect(self.conninfo, **kwargs)`), así que va
    como `kwargs={"prepare_threshold": None}`.
  · `db/backup.py:385` — la conexión suelta que usa `python3 db/backup.py`
    (el respaldo antes de una migración, disparado por launchd).
  · `tools/rellenar_categorias.py:41` y `tools/verificar_respaldo.py:86` —
    los dos guiones sueltos que abren su propia conexión.

Los demás guiones de `tools/` (`humo.py`, `vaciar_papelera.py`) NO abren su
propia conexión: importan `db.db` y usan `db.pool`, así que heredan el
arreglo de `db/db.py` sin tocarlos. `tools/descubrir_bancos.py` no habla
con Postgres (solo IMAP). Verificado con
`grep -rln "^import psycopg" --include="*.py" .` (fuera de `tests/`) — la
lista completa de archivos que importan psycopg son exactamente los
cuatro de arriba más `cerebro/consultar.py`, `acciones/crud.py`,
`cerebro/despertador.py` y `cerebro/memoria.py`, que solo importan
`psycopg.rows.dict_row` (una fábrica de filas, no abre nada).

LA PRUEBA DE ABAJO —`test_ninguna_llamada_que_abre_conexion_deja_preparar`—
NO CONFÍA EN ESTA LISTA: recorre el árbol de sintaxis de cada `.py` del
repositorio (fuera de `testpaths`, por la misma puerta que usa el resto de
la suite, `test_buzon_que_no_se_ve._py_en_disco`) buscando llamadas que
abran una conexión, derivadas del PAQUETE INSTALADO y no tecleadas: los
nombres de pool salen de qué clases de `psycopg_pool` tienen un
`.connection` invocable, y el atajo de módulo sale de qué atributos de
`psycopg` son un método ya vinculado a una subclase de
`psycopg.BaseConnection` — la misma derivación que usa
`tests/test_cerrar_varias.py::_vias_de_conexion`, reimplementada acá
suelta y chiquita para no arrastrar las 2500 líneas de esa guarda, que
resuelve un problema distinto (qué SQL salió hacia la base, no qué
parámetros lleva una llamada).

LA FRONTERA, dicha para que se pueda predecir sin correr nada:

  · SÓLO VE keywords ESCRITOS LITERALMENTE en la llamada: `prepare_threshold=None`
    a secas, o `kwargs={...}` con un diccionario LITERAL que tenga la
    clave `"prepare_threshold"` mapeada al literal `None`. Un valor que
    venga de una variable, de una función, de `**algo`, o un `kwargs` que
    sea un nombre en vez de un `{...}` escrito ahí mismo — NADA de eso se
    puede leer sin correr el programa, y esta prueba no lo corre. Cae del
    lado ESTRICTO: si no se puede leer, CUENTA COMO QUE FALTA.
  · SÓLO MIRA EL NOMBRE del atributo o la llamada (`.connect`,
    `AsyncConnectionPool`, …), no de dónde vino el objeto. Un método
    `.connect` de otra cosa que no sea psycopg entraría igual al barrido —
    hoy no hay ninguno (medido con `grep -rn "\.connect(" --include="*.py"
    . | grep -v tests/`), y si apareciera, exigirle lo mismo es el lado
    seguro del error, no un falso positivo que haga daño.
  · NO ve SQL que le llegue a Postgres sin pasar por una de estas
    llamadas escritas: un subproceso con `psql`, la consola de Railway.
    Eso ya no es "una conexión que Lucy abre" en el sentido de este
    encargo.

LO QUE NO SE PUEDE MEDIR EN ESTA MAC, PORQUE NO HAY POSTGRES: que
`prepare_threshold=None` de verdad evite el `cached plan must not change
result type` contra una base real. Queda dicho así, y lo único que
respalda la afirmación es la lectura de la librería citada arriba — se
comprobaría de verdad mirando los registros del contenedor al desplegar
esto y hacer una migración con Lucy encendida, como ya dice el diseño.

Correr:  python3 -m pytest tests/test_sin_preparadas.py
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

RAIZ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _modulo_de_verdad(nombre: str):
    """`nombre` de VERDAD, apartando cualquier doble que otra suite haya
    dejado en `sys.modules`.

    LA MAYORÍA de las suites de este repositorio stubean `psycopg` y
    `psycopg_pool` con `sys.modules.setdefault(...)` para poder correr sin
    Postgres — y `setdefault` significa que GANA LA PRIMERA: cuando pytest
    recolecta el árbol entero, para cuando le toca el turno a este archivo
    ya hay un doble instalado, y un `import psycopg` de acá arriba se
    queda con ÉSE, no con el real. Medido: corriendo solo este archivo, la
    derivación encuentra los cuatro pools y el atajo; corriendo la suite
    entera, encuentra CERO — el mismo síntoma que
    `tests/test_cerrar_varias.py::_buscar_modulo_de_verdad` documenta y
    resuelve para su propio problema (ahí es identidad de objeto para
    poder capturar quién agarró qué; acá es más simple: solo hace falta
    `dir()` del paquete real, sin preservar nada).

    Se aparta la FAMILIA entera (todo lo que empiece por el mismo prefijo,
    `psycopg`) y no solo `nombre`: `psycopg_pool` necesita poder resolver
    `psycopg.errors` al importarse, y un doble de `psycopg` a medio armar
    revienta esa importación.
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


# ── Los nombres que abren una conexión, derivados del paquete instalado ──

def _nombres_de_pool() -> set[str]:
    """Las clases de `psycopg_pool` que abren conexiones: tienen un
    `.connection` invocable. Hoy son cuatro (Connection/AsyncConnection ×
    Null/no-Null); si el paquete publica una quinta mañana, entra sola."""
    psycopg_pool = _modulo_de_verdad("psycopg_pool")
    return {
        nombre for nombre in dir(psycopg_pool)
        if not nombre.startswith("__")
        and inspect.isclass(getattr(psycopg_pool, nombre, None))
        and callable(getattr(getattr(psycopg_pool, nombre), "connection", None))
    }


def _nombres_de_atajo() -> set[str]:
    """Los atributos de `psycopg` que son un método YA VINCULADO a una
    subclase de `BaseConnection` — `psycopg.connect` es exactamente esto
    (`psycopg.connect == psycopg.Connection.connect`, medido)."""
    psycopg = _modulo_de_verdad("psycopg")
    salida = set()
    for nombre in dir(psycopg):
        obj = getattr(psycopg, nombre, None)
        due = getattr(obj, "__self__", None)
        if (inspect.ismethod(obj) and inspect.isclass(due)
                and issubclass(due, psycopg.BaseConnection)):
            salida.add(nombre)
    return salida


# Se calculan UNA vez, al importar el archivo — pero después de que el
# propio módulo ya se ejecutó, así que `_modulo_de_verdad` ya corrió y
# devolvió el paquete real, no un doble. Recalcularlas en cada prueba no
# cambiaría el resultado: la familia apartada y restaurada dentro de
# `_modulo_de_verdad` es la misma sea cual sea el momento en que se llame.
NOMBRES_DE_POOL = _nombres_de_pool()
NOMBRES_DE_ATAJO = _nombres_de_atajo()


# ── Qué llamadas abren una conexión, y si dejan `prepare_threshold=None` ──

def _nombre_llamado(nodo: ast.Call) -> str | None:
    """El nombre por el que se llamó -atributo o nombre suelto-, o None si
    la llamada no tiene una forma reconocible (`f()()`, `obj[0]()`, …)."""
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    return None


def _tiene_threshold_none(keywords: list[ast.keyword]) -> bool:
    """`prepare_threshold=None` escrito literal entre los keywords."""
    return any(
        kw.arg == "prepare_threshold"
        and isinstance(kw.value, ast.Constant) and kw.value.value is None
        for kw in keywords)


def _kwargs_trae_threshold_none(keywords: list[ast.keyword]) -> bool:
    """`kwargs={"prepare_threshold": None, ...}` — un DICCIONARIO LITERAL
    entre los keywords, con esa clave mapeada a ese valor, también literal."""
    for kw in keywords:
        if kw.arg != "kwargs" or not isinstance(kw.value, ast.Dict):
            continue
        for clave, valor in zip(kw.value.keys, kw.value.values):
            if (isinstance(clave, ast.Constant) and clave.value == "prepare_threshold"
                    and isinstance(valor, ast.Constant) and valor.value is None):
                return True
    return False


def _llamadas_que_abren_conexion(arbol: ast.AST):
    """(nodo, tipo) por cada llamada del árbol que abre una conexión.

    `tipo` es 'pool' (necesita `kwargs={...}`) o 'atajo' (necesita el
    keyword directo) — las dos formas se verifican distinto porque un pool
    no tiene `prepare_threshold` propio, se lo pasa a cada conexión que
    abre (ver el docstring del módulo).
    """
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = _nombre_llamado(nodo)
        if nombre in NOMBRES_DE_POOL:
            yield nodo, "pool"
        elif nombre in NOMBRES_DE_ATAJO:
            yield nodo, "atajo"


def test_derivacion_no_esta_vacia():
    """Si esto da vacío, el resto de este archivo se pondría verde sin
    haber mirado nada — la guarda de la guarda."""
    assert NOMBRES_DE_POOL, "no se derivó ningún nombre de pool de psycopg_pool"
    assert NOMBRES_DE_ATAJO, "no se derivó ningún atajo de conexión de psycopg"
    assert "AsyncConnectionPool" in NOMBRES_DE_POOL
    assert "connect" in NOMBRES_DE_ATAJO


def test_ninguna_llamada_que_abre_conexion_deja_preparar():
    """LA PRUEBA DE VERDAD. Recorre cada `.py` del repositorio (fuera de
    `testpaths`) y exige `prepare_threshold=None` -literal- en cada
    llamada que abre una conexión. Ver LA FRONTERA en el docstring del
    módulo para qué formas no puede ver."""
    import test_buzon_que_no_se_ve as barrido

    raiz = RAIZ.resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]

    sitios: list[str] = []
    sin_apagar: list[str] = []
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        arbol = ast.parse(real.read_text(encoding="utf-8"), str(real))
        for nodo, tipo in _llamadas_que_abren_conexion(arbol):
            donde = f"{real.relative_to(raiz).as_posix()}:{nodo.lineno}"
            sitios.append(donde)
            ok = (_kwargs_trae_threshold_none(nodo.keywords) if tipo == "pool"
                  else _tiene_threshold_none(nodo.keywords))
            if not ok:
                sin_apagar.append(f"{donde} ({_nombre_llamado(nodo)}, {tipo})")

    assert sitios, (
        "el barrido no encontró ni una llamada que abra una conexión: esta "
        "guarda estaría verde sin haber mirado nada")
    assert not sin_apagar, (
        "estas llamadas abren una conexión SIN `prepare_threshold=None` "
        f"literal (o el `kwargs` que lo lleva): {sin_apagar}. Cualquiera de "
        "ellas puede terminar con un plan guardado que una migración "
        "invalide en caliente ('cached plan must not change result type')")


def test_hoy_son_exactamente_los_cuatro_sitios_medidos():
    """Cifra medida, no tecleada como verdad: si mañana aparece un quinto
    sitio (o desaparece uno), esto avisa — no bloquea el trabajo, PERO
    obliga a mirar por qué cambió el número antes de asumir que está bien.
    """
    import test_buzon_que_no_se_ve as barrido

    raiz = RAIZ.resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    total = 0
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        arbol = ast.parse(real.read_text(encoding="utf-8"), str(real))
        total += sum(1 for _ in _llamadas_que_abren_conexion(arbol))
    assert total == 4, (
        f"se esperaban 4 sitios (db/db.py, db/backup.py, "
        f"tools/rellenar_categorias.py, tools/verificar_respaldo.py) y se "
        f"encontraron {total}. Si agregaste uno nuevo y ya lleva "
        "prepare_threshold=None, actualizá este número; si no sabés por "
        "qué cambió, no lo toques y preguntá")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
