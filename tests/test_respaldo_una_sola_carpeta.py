"""Quien guarda el respaldo y quien lo verifica miran la MISMA carpeta.

El defecto (revisión del 1-oct-2026, hallazgo EG): `tools/verificar_respaldo.py`
miraba una carpeta clavada y `db/backup.py::_destino()` elige entre varias
candidatas. Con Drive montado en `~/Library/CloudStorage/GoogleDrive-*` el
verificador decía «No hay respaldos» y `tools/respaldo_diario.sh`, que decide
con su código de salida, no vaciaba la papelera.

Estas pruebas corren los dos caminos de producción en procesos aparte, con un
HOME falso, y exigen que coincidan. La lista de candidatas se saca de
`db.backup._candidatos()` (lo real), no se teclea acá.

Hermanos: se buscan en todos los .py y .sh del repo (excepto `tests/`) con una
regla de una línea: nadie fuera de `db/backup.py` escribe la ruta
`Lucy/backups`. Frontera declarada: la documentación (.md) puede nombrarla.
"""
from __future__ import annotations

import ast
import gzip
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

_RUNNER = r"""
import runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import db.backup as b
modo, inventada = sys.argv[2], sys.argv[3]
if inventada:
    b._candidatos = lambda: [Path(inventada)]
if modo == "candidatas":
    for c in b._candidatos():
        print(c)
elif modo == "guardar":
    print(b._destino())
else:
    runpy.run_path(sys.argv[1] + "/tools/verificar_respaldo.py", run_name="__main__")
"""


def _correr(home, modo, inventada=""):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "DATABASE_URL": "postgresql://nadie:x@127.0.0.1:1/x"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, str(RAIZ), modo, inventada],
        env=env, capture_output=True, text=True, timeout=60)


def _casa(tmp_path, nombre):
    casa = tmp_path / nombre
    (casa / "Library" / "CloudStorage" / "GoogleDrive-x@y.z").mkdir(parents=True)
    return casa


def _respaldo_roto(carpeta):
    carpeta.mkdir(parents=True, exist_ok=True)
    # gzip válido con basura adentro: el verificador abre, falla al leer JSON
    # y sale ANTES de tocar la base; ya imprimió la carpeta que eligió.
    with gzip.open(carpeta / "lucy_20260101_000000.json.gz", "wt") as f:
        f.write("no es json")


def _carpeta_que_dice_el_verificador(res):
    m = re.search(r"\(carpeta: (.+)\)", res.stdout)
    assert m, f"el verificador no dijo qué carpeta miró: {res.stdout!r} {res.stderr!r}"
    return m.group(1)


def _candidatas_absolutas(casa):
    res = _correr(casa, "candidatas")
    assert res.returncode == 0, res.stderr
    return [Path(l) for l in res.stdout.splitlines() if Path(l).is_absolute()]


def test_cada_candidata_la_ven_igual_el_que_guarda_y_el_que_verifica(tmp_path):
    casa0 = _casa(tmp_path, "h0")
    candidatas = _candidatas_absolutas(casa0)
    assert len(candidatas) >= 4, candidatas
    for i, _ in enumerate(candidatas):
        casa = _casa(tmp_path, f"h{i + 1}")
        elegida = _candidatas_absolutas(casa)[i]
        _respaldo_roto(elegida)
        guarda = _correr(casa, "guardar")
        verifica = _correr(casa, "verificar")
        assert guarda.returncode == 0, guarda.stderr
        assert Path(guarda.stdout.strip()) == elegida, (guarda.stdout, elegida)
        assert Path(_carpeta_que_dice_el_verificador(verifica)) == elegida, (
            verifica.stdout, verifica.stderr, elegida)


def test_una_candidata_inventada_tambien_coincide(tmp_path):
    """Entrada que la lista real de hoy no tiene: si el verificador tuviera su
    propia lista, no la vería."""
    casa = _casa(tmp_path, "h")
    inventada = tmp_path / "Unidad Rara" / "Lucy" / "backups"
    _respaldo_roto(inventada)
    guarda = _correr(casa, "guardar", str(inventada))
    verifica = _correr(casa, "verificar", str(inventada))
    assert Path(guarda.stdout.strip()) == inventada, guarda.stderr
    assert Path(_carpeta_que_dice_el_verificador(verifica)) == inventada


def test_sin_respaldos_el_verificador_nombra_la_misma_carpeta_y_no_la_crea(tmp_path):
    casa = _casa(tmp_path, "h")
    drive = casa / "Library" / "CloudStorage" / "GoogleDrive-x@y.z" / "My Drive" / "Lucy"
    drive.mkdir(parents=True)                  # existe Lucy, falta `backups`
    verifica = _correr(casa, "verificar")
    assert verifica.returncode == 1
    assert str(drive / "backups") in verifica.stderr, verifica.stderr
    assert not (drive / "backups").exists(), "el que verifica no debe crear carpetas"
    guarda = _correr(casa, "guardar")           # el que guarda sí la crea
    assert Path(guarda.stdout.strip()) == drive / "backups"
    assert (drive / "backups").is_dir()


def _archivos_del_repo():
    """Los .py (por la puerta única de barridos, `_py_en_disco`) y los .sh que
    hay junto a ellos o en `tools/`. Frontera: un .sh fuera de las carpetas que
    ya tienen algún .py no se ve; hoy el único .sh del repo está en `tools/`."""
    import test_buzon_que_no_se_ve as barrido

    raiz = RAIZ.resolve()
    py = barrido._py_en_disco(raiz)
    carpetas = {p.parent for p in py}
    sh = [f for c in carpetas for f in c.glob("*.sh")]
    return sorted(py + sh)


def _llamadas_a_destino(arbol):
    return [n for n in ast.walk(arbol) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "_destino"]


def test_los_dos_caminos_de_produccion_llaman_a_la_puerta_unica():
    backup = ast.parse((RAIZ / "db" / "backup.py").read_text(encoding="utf-8"))
    hacer = next(n for n in backup.body
                 if isinstance(n, ast.FunctionDef) and n.name == "hacer_backup")
    assert _llamadas_a_destino(hacer), "hacer_backup ya no pide la carpeta a _destino()"
    verif = ast.parse((RAIZ / "tools" / "verificar_respaldo.py").read_text(encoding="utf-8"))
    assert _llamadas_a_destino(verif), "el verificador ya no pide la carpeta a _destino()"


def test_nadie_mas_escribe_la_carpeta_de_respaldos():
    dueno = "db/backup.py"
    patron = re.compile(r"Lucy[/\\]+backups|\"Lucy\"\s*/\s*\"backups\"")
    leidos, culpables = 0, []
    for ruta in _archivos_del_repo():
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/", ".git/")) or "/site-packages/" in rel:
            continue
        leidos += 1
        if rel != dueno and patron.search(ruta.read_text(encoding="utf-8", errors="replace")):
            culpables.append(rel)
    assert leidos > 20, leidos
    assert not culpables, f"estos archivos escriben su propia carpeta de respaldos: {culpables}"


def test_ningun_guion_lee_variables_de_railway_con_kv():
    """La sala lo prohíbe desde el 8-sep (así se filtró una llave): se lee el
    JSON y se saca solo la variable."""
    leidos, culpables = 0, []
    for ruta in _archivos_del_repo():
        rel = ruta.relative_to(RAIZ).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/", ".git/")) or "/site-packages/" in rel:
            continue
        leidos += 1
        if re.search(r"railway\s+variables[^\n]*--kv", ruta.read_text(encoding="utf-8", errors="replace")):
            culpables.append(rel)
    assert leidos > 20, leidos
    assert not culpables, culpables
    sh = (RAIZ / "tools" / "respaldo_diario.sh").read_text(encoding="utf-8")
    assert "railway variables --service Postgres --json" in sh
    assert "cut -d=" not in sh


def test_una_ruta_de_windows_no_fabrica_carpetas_en_la_mac(tmp_path):
    """`G:\\My Drive\\Lucy\\backups` no es absoluta en macOS: antes ganaba
    la búsqueda por "su padre existe" (la carpeta de trabajo) y el respaldo se
    habría guardado en una carpeta con ese nombre dentro del cwd."""
    casa = _casa(tmp_path, "h")
    drive = casa / "Library" / "CloudStorage" / "GoogleDrive-x@y.z" / "My Drive" / "Lucy"
    drive.mkdir(parents=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(casa)}
    res = subprocess.run([sys.executable, "-c", _RUNNER, str(RAIZ), "guardar", ""],
                         env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
    assert Path(res.stdout.strip()) == drive / "backups", (res.stdout, res.stderr)
    assert list(cwd.iterdir()) == [], list(cwd.iterdir())
