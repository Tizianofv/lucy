"""El aislamiento entre pruebas tiene que abarcar a Lucy, y SOLO a Lucy.

`conftest.py` guarda y restaura los atributos de cada módulo de Lucy alrededor de
cada prueba, para que un doble que una prueba se deja puesto no contamine a la
siguiente. Para eso tiene que decidir qué módulo es «de Lucy», y la regla que
usaba —«su archivo vive dentro del repo»— era falsa en la máquina de Tiziano: el
venv y el Python que baja `uv` viven DENTRO de la carpeta de trabajo.

Resultado del 4-sep-2026: 517 módulos clasificados como de Lucy en vez de 25. El
fixture restauraba `os`, `asyncio`, `pytest` y todo lo instalado después de cada
prueba, y eso TAPABA los parches que las pruebas dejaban sobre la biblioteca
estándar. En GitHub el intérprete vive fuera del checkout, no se tapaba nada, y
la primera corrida del freno (commit 347ff14) salió con 8 fallos que en esta Mac
eran invisibles.

Lo que se protege acá es justo eso: que la frontera del aislamiento sea el código
de Lucy y no la carpeta donde alguien puso su venv. Sin este test, el defecto
vuelve el día que alguien simplifique la condición, y vuelve invisible.

Correr:  python3 tests/test_conftest_aislamiento.py
(o con pytest: pytest tests/test_conftest_aislamiento.py)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import conftest  # noqa: E402

# Ajenos con archivo propio: dos de la biblioteca estándar, uno del intérprete y
# uno instalado con pip. Si el aislamiento agarra a cualquiera de estos, agarra
# la instalación entera.
AJENOS = (os, asyncio, json, pytest)


def test_el_aislamiento_no_agarra_al_interprete_ni_a_los_paquetes():
    """Ninguno de los ajenos puede salir en la lista de módulos de Lucy."""
    nombres = {n for n, _ in conftest._modulos_del_proyecto()}
    for mod in AJENOS:
        assert mod.__name__ not in nombres, (
            f"el aislamiento entre pruebas cree que `{mod.__name__}` es código "
            "de Lucy. Eso restaura la biblioteca estándar después de cada "
            "prueba y tapa los parches que las pruebas se dejan puestos: es el "
            "defecto que el 4-sep-2026 hizo que la suite saliera verde en la "
            "Mac y con 8 fallos en GitHub.")


def test_las_carpetas_del_entorno_se_reconocen_aunque_esten_dentro_del_repo():
    """`_del_entorno` es la frontera. Acá se comprueba de los dos lados."""
    for mod in AJENOS:
        ruta = pathlib.Path(mod.__file__).resolve()
        assert conftest._del_entorno(ruta), (
            f"`{mod.__name__}` vive en {ruta} y el conftest no lo reconoce como "
            "parte del entorno")

    # Y del otro lado: código de Lucy de verdad no puede caer en la exclusión,
    # porque entonces las pruebas volverían a pisarse entre archivos.
    fuente = conftest._RAIZ / "cerebro" / "agente.py"
    assert fuente.is_file(), (
        f"no encuentro {fuente}: cambió el árbol del repo y este test hay que "
        "ajustarlo, no borrarlo")
    assert not conftest._del_entorno(fuente), (
        "el conftest dejó fuera del aislamiento a código de Lucy")


if __name__ == "__main__":
    fallidos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {nombre}")
        except AssertionError as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {e or 'assert falló'}")
        except Exception as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {type(e).__name__}: {e}")
    print(f"\n{'FALLARON ' + str(fallidos) if fallidos else 'Todo verde'}")
    sys.exit(1 if fallidos else 0)
