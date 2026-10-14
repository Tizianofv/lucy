"""Todo paquete de fuera de la biblioteca estándar que el código importa tiene
que estar en `requirements.txt`.

Por qué existe: Railway instala `requirements.txt` y nada más. Un `import` que
hoy funciona solo porque otra librería lo arrastra (o porque está instalado en
el entorno de quien programa) pasa la construcción y tumba a Lucy al arrancar el
día que esa librería cambie.

Cómo decide (nada de listas tecleadas de «paquete X se importa como Y»):
  * los `import` se sacan con `ast` de todo `.py` del repositorio que no sea de
    pruebas (`tests/`, `conftest.py`);
  * lo propio del repositorio se saca de lo que hay en la raíz (carpetas y
    archivos `.py`);
  * la biblioteca estándar es `sys.stdlib_module_names`;
  * de import a paquete instalado se pasa con `importlib.metadata.
    packages_distributions()`, lo que de verdad dice el entorno.

Frontera, dicha: no ve `importlib.import_module(...)`, `__import__(...)` ni
nombres armados al vuelo, y solo ve los paquetes instalados en el entorno que
corre la prueba (uno que no esté instalado ni declarado sale como no declarado).
"""
import ast
import re
import sys
from importlib.metadata import packages_distributions, requires
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# `tests/` y `conftest.py` son pruebas: sus
# dependencias van en requirements-dev.txt, que Railway no instala.
_CARPETAS_FUERA = {"tests"}

# Importados por el código y NO declarados en requirements.txt, a propósito.
# Cada uno debe seguir siendo cierto (lo exige test_los_exentos_siguen_siendo_ciertos):
# se importa de verdad, no está declarado, y lo trae una librería que sí lo está.
EXENTOS = {
    "pydantic": "lo trae fastapi (Requires-Dist de fastapi). Deuda conocida: declararlo con versión fija.",
    "starlette": "lo trae fastapi (Requires-Dist de fastapi). Deuda conocida: declararlo con versión fija.",
}


def _nombre(texto):
    """Nombre normalizado de un requisito: 'psycopg[binary]==3.2.3' -> 'psycopg'."""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", texto)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


def declaradas(ruta=None):
    """Nombres declarados en requirements.txt (sin comentarios ni opciones `-r`)."""
    ruta = ruta or RAIZ / "requirements.txt"
    nombres = set()
    for linea in ruta.read_text().splitlines():
        linea = linea.split("#", 1)[0].strip()
        if linea and not linea.startswith("-"):
            nombres.add(_nombre(linea))
    return nombres


def _py_del_codigo(raiz):
    """Los `.py` que no son pruebas, por LA puerta de los barridos del repo
    (`test_buzon_que_no_se_ve._py_en_disco`): no se mete en entornos virtuales."""
    from test_buzon_que_no_se_ve import _py_en_disco
    return [p for p in _py_en_disco(raiz)
            if not (_CARPETAS_FUERA & set(p.relative_to(raiz).parts)) and p.name != "conftest.py"]


def _propios(raiz):
    """Lo que el repo define en su raíz: la primera pieza de la ruta de cada .py."""
    return {p.relative_to(raiz).parts[0].removesuffix(".py") for p in _py_del_codigo(raiz)}


def _fuentes(raiz):
    for f in _py_del_codigo(raiz):
        yield f.relative_to(raiz), f.read_text()


def imports_de_terceros(fuentes, propios):
    """{paquete de primer nivel: {archivos}} de lo que no es estándar ni propio."""
    vistos = {}
    for rel, texto in fuentes:
        for n in ast.walk(ast.parse(texto)):
            if isinstance(n, ast.Import):
                tops = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                tops = [n.module.split(".")[0]]
            else:
                continue
            for t in tops:
                if t not in sys.stdlib_module_names and t not in propios:
                    vistos.setdefault(t, set()).add(str(rel))
    return vistos


def sin_declarar(imports, declarados, exentos):
    """Paquetes importados cuyo paquete instalado no está declarado ni exento.
    Un import que ningún paquete instalado provee cae del lado estricto."""
    quien = packages_distributions()
    malos = {}
    for top, archivos in imports.items():
        if top in exentos:
            continue
        dists = {_nombre(d) for d in quien.get(top, [])}
        if not (dists & declarados):
            malos[top] = (sorted(dists), sorted(archivos))
    return malos


def test_todo_import_de_terceros_esta_declarado():
    imports = imports_de_terceros(_fuentes(RAIZ), _propios(RAIZ))
    assert imports, "la sonda no vio ningún import de terceros: está ciega"
    malos = sin_declarar(imports, declaradas(), EXENTOS)
    assert not malos, f"importados y no declarados en requirements.txt: {malos}"


def test_los_exentos_siguen_siendo_ciertos():
    imports = imports_de_terceros(_fuentes(RAIZ), _propios(RAIZ))
    quien = packages_distributions()
    declarados = declaradas()
    # Lo que traen, por Requires-Dist, las librerías declaradas.
    arrastrados = set()
    for d in declarados:
        try:
            for r in requires(d) or []:
                if "extra ==" not in r:
                    arrastrados.add(_nombre(r))
        except Exception:
            pass
    for top, motivo in EXENTOS.items():
        assert motivo.strip(), top
        assert top in imports, f"{top} ya no se importa: sácalo de EXENTOS"
        dists = {_nombre(d) for d in quien.get(top, [])}
        assert not (dists & declarados), f"{top} ya está declarado: sácalo de EXENTOS"
        assert dists & arrastrados, f"{top} ya no lo trae ninguna librería declarada: declararlo"


def test_la_sonda_ve_una_entrada_inventada():
    """El cubo estricto es alcanzable: un paquete que nadie provee sale como no declarado."""
    propios = _propios(RAIZ)
    imports = imports_de_terceros([(Path("x.py"), "import paquete_inventado_zzz\nfrom otro_inventado_zzz.sub import a")], propios)
    assert set(imports) == {"paquete_inventado_zzz", "otro_inventado_zzz"}
    assert set(sin_declarar(imports, declaradas(), EXENTOS)) == set(imports)


def test_la_sonda_ve_un_import_de_un_paquete_instalado_pero_no_declarado():
    imports = imports_de_terceros([(Path("x.py"), "import pytest_asyncio")], _propios(RAIZ))
    assert "pytest_asyncio" in sin_declarar(imports, declaradas(), EXENTOS)


def test_todo_lo_declarado_esta_fijado_con_version():
    for linea in (RAIZ / "requirements.txt").read_text().splitlines():
        linea = linea.split("#", 1)[0].strip()
        if linea:
            assert re.match(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?==\d+(\.\d+)*$", linea), linea
