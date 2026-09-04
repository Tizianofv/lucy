"""Reglas de recolección de pytest para Lucy.

Resuelve un solo problema: que la suite distinga **«esto no se puede probar
acá»** de **«esto está roto»**.

El corpus de correos bancarios (`tests/fixtures/**/*.eml`) son movimientos
reales de Tiziano y de Rosi — montos, comercios, últimos dígitos de tarjeta — y
por eso está excluido en `.gitignore` línea 23. En la máquina de Tiziano existe;
en un runner limpio de GitHub, no. Sin este archivo pasan las dos cosas malas a
la vez:

  1. Nueve pruebas de `tests/test_popular.py` abren un `.eml` directamente y
     revientan con `FileNotFoundError`. En GitHub eso es rojo permanente, y un
     rojo permanente es un freno que nadie mira.
  2. Nueve funciones repartidas en ocho archivos hacen `if not FIXTURES.exists():
     return` y se reportan **VERDES sin haber probado nada**. Eso es peor que el
     rojo: miente en la dirección cómoda.

Acá los dos casos pasan a ser SKIP con motivo. Ni se sube un solo correo real a
GitHub, ni se le baja la vara a ninguna prueba: se dice que no corrieron. Con
`-ra` (ver pytest.ini) el motivo sale impreso en el resumen del runner.

Cuando los fixtures SÍ están en disco, este archivo no hace absolutamente nada:
las pruebas corren enteras, como siempre.
"""
from __future__ import annotations


import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "tests" / "fixtures"

_MOTIVO = (
    "necesita el corpus de correos bancarios reales (tests/fixtures/**/*.eml), "
    "que está fuera de git por .gitignore:23 y no existe en este entorno"
)


def _hay_fixtures() -> bool:
    return FIXTURES.exists() and any(FIXTURES.rglob("*.eml"))


def pytest_collection_modifyitems(config, items):
    """Marca como saltada toda prueba que dependa del corpus, cuando no está.

    El criterio es el bytecode de la función, no su texto: se mira si usa el
    nombre `FIXTURES` (global del módulo) / `fixtures` (local), o si construye la
    ruta con el literal exacto `"fixtures"` — que es como lo hace
    `tests/test_categorias.py`, con una variable local `fx`.

    Se compara por igualdad exacta y no por «contiene». Con «contiene» sobre el
    texto de la función entraban los docstrings: en `tests/test_consumos.py`
    saltaban pruebas que solo nombran el corpus al explicarse.

    Las que llegan al corpus por un ayudante (`_con_pdf`, `_pdfs_de`) no usan
    ninguno de esos nombres y no caen acá; de ésas se encarga la red de abajo.
    """
    if _hay_fixtures():
        return

    saltar = pytest.mark.skip(reason=_MOTIVO)
    for item in items:
        fn = getattr(item, "function", None)
        codigo = getattr(fn, "__code__", None)
        if codigo is None:
            continue
        usa = set(codigo.co_names) | set(codigo.co_varnames)
        usa |= {c for c in codigo.co_consts if isinstance(c, str)}
        if usa & {"FIXTURES", "fixtures"}:
            item.add_marker(saltar)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Red de seguridad: un `.eml` del corpus que falta es una saltada, no un fallo.

    Solo se aplica al archivo que falta si está dentro de `tests/fixtures/`.
    Cualquier otro `FileNotFoundError` sigue siendo un fallo: si mañana el
    código de Lucy no encuentra un archivo que sí debería estar, esto no lo tapa.
    """
    try:
        return (yield)
    except FileNotFoundError as e:
        ruta = getattr(e, "filename", None)
        if ruta and FIXTURES in pathlib.Path(str(ruta)).resolve().parents:
            pytest.skip(f"{_MOTIVO} — falta {pathlib.Path(str(ruta)).name}")
        raise


# ── Aislamiento entre pruebas ────────────────────────────────────────────
#
# Los tests de Lucy son funciones sueltas, sin fixtures de pytest: cada uno
# instala sus dobles asignándolos directamente sobre el módulo real
# (`db.guardar_movimiento = ...`) y NADIE los devuelve a su sitio.
#
# `tests/test_consumos.py:136` y `tests/test_canario_remitentes.py:131` hacen
# `setattr(db, n, getattr(reg, n))` sobre cinco funciones de `db.db`. A partir de
# ahí, toda prueba posterior que llame a `db.guardar_movimiento` habla con el
# doble del archivo anterior en vez de con el código real, su propio doble nunca
# se toca, y revienta con `AttributeError: 'NoneType' object has no attribute ...`.
#
# Medido el 4-sep-2026: los tres archivos afectados pasan al 100% corridos solos
# (9+13+34) y suman 17 fallos dentro de la suite completa. No es un defecto del
# código de Lucy: es contaminación entre pruebas por orden de ejecución.
#
# El arreglo va acá y no en los dos archivos porque este repo corre sus tests
# también con `python3 tests/test_x.py` (cada archivo tiene su bloque __main__):
# meterles fixtures de pytest rompería esa segunda forma de correrlos.

import sys  # noqa: E402


_RAIZ = pathlib.Path(__file__).parent.resolve()
_ES_DEL_PROYECTO: dict = {}


def _modulos_del_proyecto():
    """Módulos ya importados cuyo archivo vive dentro del repo, menos los tests.

    La clasificación se cachea por nombre de módulo: sin caché, resolver la ruta
    de cada módulo en cada una de las 353 pruebas costaba 10.9 s de suite.
    """
    raiz = _RAIZ
    for nombre, mod in list(sys.modules.items()):
        cacheado = _ES_DEL_PROYECTO.get(nombre)
        if cacheado is True:
            yield nombre, mod
            continue
        if cacheado is False:
            continue
        _ES_DEL_PROYECTO[nombre] = False
        archivo = getattr(mod, "__file__", None)
        # Hay pruebas que sustituyen atributos de un módulo por dobles que
        # responden a cualquier nombre (`_Cualquiera` en
        # tests/test_reporte_una_vez_al_dia.py), y `__file__` puede dejar de ser
        # un str. Si no es un str, no es un módulo del que podamos decir dónde vive.
        if not isinstance(archivo, str) or not archivo:
            continue
        try:
            ruta = pathlib.Path(archivo).resolve()
        except (OSError, ValueError):
            continue
        if raiz not in ruta.parents:
            continue
        if ruta.name == "conftest.py" or "tests" in ruta.relative_to(raiz).parts:
            continue
        _ES_DEL_PROYECTO[nombre] = True
        yield nombre, mod


@pytest.fixture(autouse=True)
def _devolver_los_modulos_a_su_sitio():
    """Deja cada módulo de Lucy como estaba antes de la prueba.

    Solo toca lo que la prueba haya cambiado: si no ensució nada, no escribe nada.
    """
    antes = {nombre: dict(vars(mod)) for nombre, mod in _modulos_del_proyecto()}
    yield
    for nombre, original in antes.items():
        mod = sys.modules.get(nombre)
        if mod is None:
            continue
        actual = vars(mod)
        for clave in list(actual):
            if clave not in original:
                del actual[clave]
            elif actual[clave] is not original[clave]:
                actual[clave] = original[clave]
        for clave, valor in original.items():
            if clave not in actual:
                actual[clave] = valor
