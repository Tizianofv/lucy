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
    packages_distributions()`, lo que de verdad dice el entorno. Si el primer
    nivel es un espacio de nombres compartido (`google`: lo reparten varias
    distribuciones y no tiene `__init__.py`), no basta con la primera pieza: se
    exige el módulo COMPLETO (`google.cloud`, `google.cloud.storage`) y que
    alguna distribución instalada traiga esa ruta en sus archivos; si ninguna
    la trae, cae como no declarado (`from google.cloud import storage` no se
    cuela por tener `google-auth`).

Frontera, dicha: no ve `importlib.import_module(...)`, `__import__(...)` ni
nombres armados al vuelo, y solo ve los paquetes instalados en el entorno que
corre la prueba (uno que no esté instalado ni declarado sale como no declarado).
"""
import ast
import re
import sys
from importlib.metadata import distributions, packages_distributions, requires
from importlib.util import find_spec
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
    (`test_buzon_que_no_se_ve._py_en_disco`): no se mete en entornos virtuales.
    Solo la carpeta `tests/` DE LA RAÍZ (y los `conftest.py`) es de pruebas: un
    `cerebro/tests/` con código de Lucy sí se mira."""
    from test_buzon_que_no_se_ve import _py_en_disco
    return [p for p in _py_en_disco(raiz)
            if p.relative_to(raiz).parts[0] not in _CARPETAS_FUERA and p.name != "conftest.py"]


def _propios(raiz):
    """Lo que el repo define en su raíz: la primera pieza de la ruta de cada .py."""
    return {p.relative_to(raiz).parts[0].removesuffix(".py") for p in _py_del_codigo(raiz)}


def _fuentes(raiz):
    for f in _py_del_codigo(raiz):
        yield f.relative_to(raiz), f.read_text()


def imports_de_terceros(fuentes, propios):
    """{paquete de primer nivel: {"archivos": {...}, "usos": [(archivo, candidatos)]}}
    de lo que no es estándar ni propio. Cada `import` es un uso aparte (si no,
    un `google.auth` bueno taparía un `google.cloud` malo). Para `from a.b import
    c` los candidatos son `a.b` y `a.b.c` (puede ser un submódulo); para
    `import a.b.c`, `a.b.c`."""
    vistos = {}
    for rel, texto in fuentes:
        for n in ast.walk(ast.parse(texto)):
            if isinstance(n, ast.Import):
                pares = [(a.name, [a.name]) for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                pares = [(n.module, [n.module] + [f"{n.module}.{a.name}" for a in n.names])]
            else:
                continue
            for completo, candidatos in pares:
                t = completo.split(".")[0]
                if t not in sys.stdlib_module_names and t not in propios:
                    v = vistos.setdefault(t, {"archivos": set(), "usos": []})
                    v["archivos"].add(str(rel))
                    v["usos"].append((str(rel), tuple(candidatos)))
    return vistos


def _modulos_por_distribucion():
    """{módulo con punto de al menos 2 piezas: {distribuciones}} sacado de los
    archivos que cada distribución instalada trae de verdad."""
    out = {}
    for d in distributions():
        nombre = _nombre(d.metadata["Name"] or "")
        for f in d.files or []:
            partes = list(f.parts)
            if not partes[-1].split(".")[0] or partes[0].endswith((".dist-info", ".data")) or partes[0] == "..":
                continue
            ult = partes[-1].split(".")[0]
            piezas = partes[:-1] + ([] if ult == "__init__" else [ult])
            for k in range(2, len(piezas) + 1):
                out.setdefault(".".join(piezas[:k]), set()).add(nombre)
    return out


def _es_espacio_compartido(top):
    """Primer nivel sin `__init__.py` (PEP 420): varias distribuciones lo reparten."""
    try:
        spec = find_spec(top)
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.origin in (None, "namespace")


def sin_declarar(imports, declarados, exentos):
    """Paquetes importados cuyo paquete instalado no está declarado ni exento.
    Un import que ningún paquete instalado provee cae del lado estricto."""
    quien = packages_distributions()
    por_modulo = None
    malos = {}
    for top, info in imports.items():
        if top in exentos:
            continue
        if _es_espacio_compartido(top):
            por_modulo = por_modulo or _modulos_por_distribucion()
            sueltos = []
            todas = set()
            for archivo, candidatos in info["usos"]:
                d = set().union(*(por_modulo.get(c, set()) for c in candidatos))
                todas |= d
                if not (d & declarados):
                    sueltos.append(archivo)
            if sueltos:
                malos[top] = (sorted(todas), sorted(set(sueltos)))
            continue
        else:
            dists = {_nombre(d) for d in quien.get(top, [])}
        if not (dists & declarados):
            malos[top] = (sorted(dists), sorted(info["archivos"]))
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


def test_un_subpaquete_de_espacio_compartido_no_instalado_cae_como_no_declarado():
    """`google` lo reparte google-auth (declarada) y no tiene __init__.py:
    `google.cloud.storage` no está instalado y no puede colarse por eso."""
    propios = _propios(RAIZ)
    declarados = declaradas()
    assert "google-auth" in declarados and _es_espacio_compartido("google")
    nuevo = imports_de_terceros([(Path("x.py"), "from google.cloud import storage")], propios)
    assert set(sin_declarar(nuevo, declarados, EXENTOS)) == {"google"}
    nuevo = imports_de_terceros([(Path("x.py"), "import google.cloud.storage")], propios)
    assert set(sin_declarar(nuevo, declarados, EXENTOS)) == {"google"}
    # un uso bueno en OTRO archivo no tapa al malo
    mezcla = imports_de_terceros([(Path("a.py"), "from google.oauth2 import service_account"),
                                  (Path("b.py"), "from google.cloud import storage")], propios)
    assert sin_declarar(mezcla, declarados, EXENTOS) == {"google": (["google-auth"], ["b.py"])}
    # y lo que sí es de google-auth sigue pasando, de las dos formas
    ok = imports_de_terceros([(Path("x.py"), "from google.oauth2 import service_account\nimport google.auth.transport.requests")], propios)
    assert sin_declarar(ok, declarados, EXENTOS) == {}


def test_solo_la_carpeta_tests_de_la_raiz_es_de_pruebas(tmp_path):
    for rel in ("tests/a.py", "cerebro/tests/b.py", "cerebro/c.py", "conftest.py", "main.py"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
    vistos = {str(p.relative_to(tmp_path)) for p in _py_del_codigo(tmp_path)}
    assert vistos == {"cerebro/tests/b.py", "cerebro/c.py", "main.py"}, vistos


def test_las_dependencias_que_trae_fpdf2_estan_declaradas_y_fijadas():
    """Railway instala lo de requirements.txt: lo que fpdf2 arrastra va fijado a
    lo que se probó, no a lo que resuelva pip ese día."""
    traidas = {_nombre(r) for r in requires("fpdf2") if "extra ==" not in r}
    assert traidas, "fpdf2 ya no declara dependencias: revisa esta prueba"
    assert traidas <= declaradas(), traidas - declaradas()


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
