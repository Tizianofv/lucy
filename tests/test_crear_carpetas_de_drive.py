"""El arreglo de una vez para los proyectos que ya existen (pieza 3 del diseño «Carpetas del proyecto», 18-oct-2026):
`tools/crear_carpetas_de_drive.py`. Garantía A10 del diseño.

CÓMO. Se corre el `main` REAL del guion, con sus argumentos, sobre la base de SQLite con las sentencias reales de `db`
(las mismas lecturas y la misma `preparar_la_carpeta` de producción) y el Drive de mentira con memoria (`Nube`, de
`tests/test_cotizaciones_drive.py`) detrás del cliente real `drive.py`. Lo ÚNICO que se reemplaza es abrir y cerrar el
pool de conexiones (`db.abrir`/`db.cerrar`, que en SQLite no tienen pool que abrir), y se cuenta que se llamaron.
Los proyectos salen de una tabla de siembra (`SEMBRADO`); lo que se espera de cada grupo se calcula de esa tabla
(`_esperado`), no se escribe a mano por grupo, y una prueba agrega proyectos nuevos para ver que el guion lee la base.

QUÉ SE VIGILA (A10 y lo que Tiziano condicionó el 18-oct-2026: «solo activos» y «si pero solo si también la agregas
al proyecto»):
  1. `--contar` (por omisión, y escrito) no escribe en la base (volcado completo antes/después) ni llama a Drive (cero
     pedidos), y no imprime nombres de proyectos;
  2. solo los vivos con estado «activo»: pausados, cerrados y la papelera no reciben carpeta, aunque el máximo sobre;
  3. `--maximo` corta; sin `--maximo` (o con 0, o sin `--crear`) el guion se niega antes de abrir la base;
  4. un proyecto con la carpeta ya escrita a mano (también un valor que `db` rechaza) no recibe otra;
  5. un proyecto con fila y sin enlace solo recibe el enlace: cero pedidos a Drive; y los que necesitan a Drive van primero;
  6. segunda corrida: cero pedidos a Drive y la base queda igual;
  7. «hecho» es lo RELEÍDO de la base (fila + `proyectos.carpeta` = enlace de esa carpeta), no lo que dice `preparar`;
  8. un fallo de Drive detiene la corrida, sale 1 y dice la frase real; lo que se dice de cada proyecto sale de
     `frase_de_la_carpeta`;
  9. faltan variables de Drive: `--crear` no hace nada (salida 2); `--contar` dice cuáles faltan y nunca un valor;
 10. la base sin la tabla o sin la columna: no se escribe nada y sale 1; se abre y se cierra la base siempre;
 11. el guion no habla con Drive ni escribe en la base por su cuenta: solo lee con `db` y pide por `preparar_la_carpeta`
     (censo con `ast` y entradas inventadas).

FRONTERA, dicha una vez: SQLite, no Postgres; el Drive de mentira afirma lo que afirma `tests/test_drive.py`; el guion
nunca corrió contra Google ni contra Railway; el censo con `ast` no ve un nombre armado en ejecución (`getattr`).

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

from test_cotizaciones_drive import (  # noqa: F401  (el Drive de mentira, sus fixtures y ayudas)
    FALLOS, MARCA_DE_GOOGLE, Nube, _carpetas, _filas, google, m, mundo, gente, nube)
from test_grupo_ia import _ROOT

import cotizaciones_drive as cd
import db.db as db
import drive
from acciones import crud

RUTA_DEL_GUION = _ROOT / "tools" / "crear_carpetas_de_drive.py"


def _cargar():
    spec = importlib.util.spec_from_file_location("crear_carpetas_de_drive", RUTA_DEL_GUION)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


guion = _cargar()


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# La base: una tabla de siembra, y lo que se espera sale de ella
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

ESCRITA = "https://ejemplo.test/lo-puso-una-persona"
# (id, estado, en la papelera, fila en drive_de_proyectos, lo que trae proyectos.carpeta, nombre)
SEMBRADO = [
    (1, "activo", False, True, None, "Nombre-secreto-uno"),        # con carpeta en Drive, sin enlace en el proyecto
    (2, "activo", False, False, None, "Nombre-repetido"),          # falta todo
    (3, "activo", False, False, None, "Nombre-secreto-tres"),      # falta todo
    (4, "pausado", False, False, None, "Nombre-secreto-cuatro"),   # no es activo
    (5, "cerrado", False, False, None, "Nombre-secreto-cinco"),    # no es activo
    (6, "activo", True, False, None, "Nombre-secreto-seis"),       # en la papelera
    (7, "activo", False, True, ESCRITA, "Nombre-secreto-siete"),   # completo
    (8, "activo", False, False, ESCRITA, "Nombre-secreto-ocho"),   # carpeta escrita a mano, sin fila
    (9, "activo", False, False, None, "Nombre-repetido"),          # falta todo; mismo nombre que el 2
    (10, "activo", False, False, "uno\ndos", "Nombre-secreto-diez"),  # algo escrito que `db` no deja pasar
]
NOMBRES = {n for *_, n in SEMBRADO}


def _esperado(sembrado):
    """Los grupos que el guion debe ver, calculados de la tabla de siembra con la regla dicha en palabras."""
    g = {"completos": [], "solo_enlace": [], "por_crear": [], "carpeta_ajena": []}
    for i, estado, papelera, fila, carpeta, _ in sembrado:
        if papelera or estado != "activo":
            continue
        g[("completos" if carpeta else "solo_enlace") if fila else ("carpeta_ajena" if carpeta else "por_crear")].append(i)
    return g


def _sembrar(m_, sembrado):
    m_.con.execute("DELETE FROM proyectos")
    for i, estado, papelera, fila, carpeta, nombre in sembrado:
        m_.proyecto(i, nombre, area="CDS", estado=estado, borrado=papelera)
        if carpeta is not None:
            m_.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = ?", (carpeta, i))
        if fila:
            m_.con.execute("INSERT INTO drive_de_proyectos (proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, "
                           "nombre_puesto) VALUES (?, ?, ?, ?)", (i, f"PREVIA{i}", f"PREVIAC{i}", nombre))
    m_.con.commit()


@pytest.fixture
def mundo_de_siembra(m, nube, monkeypatch):
    """La base sembrada, el Drive de mentira con las cuatro variables, y `db.abrir`/`db.cerrar` contados."""
    _sembrar(m, SEMBRADO)
    llamadas = {"abrir": 0, "cerrar": 0}

    async def abrir():
        llamadas["abrir"] += 1

    async def cerrar():
        llamadas["cerrar"] += 1
    monkeypatch.setattr(db, "abrir", abrir)
    monkeypatch.setattr(db, "cerrar", cerrar)
    m.nube, m.llamadas = nube, llamadas
    return m


def _volcado(m_):
    return list(m_.con.iterdump())


def _correr(capsys, *argv):
    """El `main` real del guion: (código de salida, lo que imprimió, lo que imprimió por el canal de errores)."""
    codigo = guion.main(list(argv))
    salida = capsys.readouterr()
    return codigo, salida.out, salida.err


def _pedidos(nube_):
    return len(nube_.google.pedidos)


def _ids_de(salida: str, titulo: str) -> list[int]:
    """Los ids de la línea `ids:` que sigue a la línea que contiene `titulo`."""
    lineas = salida.splitlines()
    sigue = lineas[[k for k, l in enumerate(lineas) if titulo in l][0] + 1]
    return [int(x.strip("# ")) for x in sigue.split("ids:")[1].split(",") if x.strip().startswith("#")]


def _carpeta_de(m_, pid):
    return _filas(m_, f"SELECT carpeta FROM proyectos WHERE id = {pid}")[0]["carpeta"]


def _enlace(carpeta_id):
    return f"https://drive.google.com/drive/folders/{carpeta_id}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Contar no escribe, ni en la base ni en Drive; y cuenta lo que hay
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("argv", [[], ["--contar"]], ids=["por_omision", "escrito"])
def test_contar_no_escribe_en_la_base_ni_llama_a_drive_y_cuenta_lo_que_hay(mundo_de_siembra, capsys, argv):
    antes = _volcado(mundo_de_siembra)
    codigo, salida, _ = _correr(capsys, *argv)
    assert codigo == 0, salida
    assert _volcado(mundo_de_siembra) == antes                      # ni una fila, ni un dato
    assert _pedidos(mundo_de_siembra.nube) == 0 and mundo_de_siembra.nube.pasos == []   # ni un pedido a la red
    e = _esperado(SEMBRADO)
    assert _ids_de(salida, "se crearía su carpeta") == e["por_crear"] == [2, 3, 9]
    assert _ids_de(salida, "solo se escribiría el enlace") == e["solo_enlace"] == [1]
    assert _ids_de(salida, "NO se tocan") == e["carpeta_ajena"] == [8, 10]
    assert "Proyectos vivos (fuera de la papelera): 9" in salida
    assert "activo 7 · pausado 1 · cerrado 1" in salida
    assert f"Por hacer con --crear: {len(e['por_crear']) + len(e['solo_enlace'])}" in salida
    assert "(la segunda carpeta llevaría «· #id»): #2, #9" in salida
    assert "no se escribió nada" in salida
    assert [mundo_de_siembra.llamadas[k] for k in ("abrir", "cerrar")] == [1, 1]


def test_la_salida_no_dice_ningun_nombre_de_proyecto(mundo_de_siembra, capsys):
    for argv in (["--contar"], ["--crear", "--maximo", "2"]):
        codigo, salida, err = _correr(capsys, *argv)
        assert codigo == 0
        for nombre in NOMBRES:
            assert nombre not in salida and nombre not in err, nombre
        assert "Nombre-" not in salida


def test_los_proyectos_salen_de_la_base_no_de_una_lista(mundo_de_siembra, capsys):
    """Entrada inventada: otra siembra, con otros ids y otros estados, y el guion cuenta ESA."""
    otra = [(40, "activo", False, False, None, "a"), (41, "activo", False, True, None, "b"),
            (42, "pausado", False, False, None, "c"), (43, "activo", False, False, ESCRITA, "d"),
            (44, "activo", False, False, None, "e"), (45, "activo", True, False, None, "f")]
    _sembrar(mundo_de_siembra, otra)
    codigo, salida, _ = _correr(capsys, "--contar")
    e = _esperado(otra)
    assert codigo == 0 and e["por_crear"] == [40, 44] and e["solo_enlace"] == [41]
    assert _ids_de(salida, "se crearía su carpeta") == e["por_crear"]
    assert _ids_de(salida, "solo se escribiría el enlace") == e["solo_enlace"]
    assert _ids_de(salida, "NO se tocan") == e["carpeta_ajena"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Crear: solo activos, respeta el máximo, enlace puesto, una vez
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_crear_uno_crea_una_carpeta_la_anota_y_deja_el_enlace_puesto_en_el_proyecto(mundo_de_siembra, capsys):
    nube = mundo_de_siembra.nube
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "1")
    # el primero que NECESITA a Drive (el 2), aunque el 1 (solo enlace) tenga un id más bajo
    assert codigo == 0, salida
    fila = _carpetas(mundo_de_siembra, 2)
    assert fila is not None and nube.carpetas_de_proyectos() == [fila["carpeta_proyecto_id"]]
    assert nube.cosas[fila["carpeta_proyecto_id"]]["nombre"] == "Nombre-repetido"
    assert nube.hijos(fila["carpeta_proyecto_id"]) == [fila["carpeta_cotizaciones_id"]]
    assert _carpeta_de(mundo_de_siembra, 2) == _enlace(fila["carpeta_proyecto_id"])    # LA CONDICIÓN DE TIZIANO
    for otro in (3, 9, 4, 5, 6, 8, 10):                                                # nadie más se tocó
        assert _carpetas(mundo_de_siembra, otro) is None
    assert _carpetas(mundo_de_siembra, 1)["carpeta_proyecto_id"] == "PREVIA1"
    assert _carpeta_de(mundo_de_siembra, 1) is None and _carpeta_de(mundo_de_siembra, 3) is None
    huella = _filas(mundo_de_siembra, "SELECT * FROM log_acciones WHERE tabla = 'proyectos' AND registro_id = 2 "
                                      "AND accion = 'editar'")
    assert [h["motivo"] for h in huella] == [guion.MOTIVO] and huella[0]["actor"] == "panel"
    # lo que dijo del proyecto es la frase real
    assert cd.frase_de_la_carpeta({"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_PUESTO}) in salida
    assert "#2:" in salida and "releídos de la base: sí" in salida and "hechos y releídos: 1 de 1" in salida
    assert "quedan fuera del máximo: #3, #9, #1" in salida
    assert [mundo_de_siembra.llamadas[k] for k in ("abrir", "cerrar")] == [1, 1]


def test_el_maximo_corta_y_solo_entran_los_activos_vivos(mundo_de_siembra, capsys):
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "2")
    assert codigo == 0, salida
    con_fila = {f["proyecto_id"] for f in _filas(mundo_de_siembra, "SELECT proyecto_id FROM drive_de_proyectos")}
    nuevas = con_fila - {1, 7}
    assert nuevas == {2, 3} and len(mundo_de_siembra.nube.carpetas_de_proyectos()) == 2
    # ahora sobra el máximo: entran todos los que faltan y NINGUNO de los que no son activos o están en la papelera
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "500")
    assert codigo == 0, salida
    con_fila = {f["proyecto_id"] for f in _filas(mundo_de_siembra, "SELECT proyecto_id FROM drive_de_proyectos")}
    assert con_fila == {1, 2, 3, 7, 9}                       # 4 pausado, 5 cerrado, 6 papelera, 8 y 10 con algo escrito
    assert len(mundo_de_siembra.nube.carpetas_de_proyectos()) == 3      # 2, 3 y 9; el 1 ya tenía la suya (no vive en la nube de mentira)
    for fuera in (4, 5, 6, 8, 10):
        assert _carpeta_de(mundo_de_siembra, fuera) in (None, ESCRITA, "uno\ndos")
    assert _carpeta_de(mundo_de_siembra, 8) == ESCRITA and _carpeta_de(mundo_de_siembra, 10) == "uno\ndos"
    for puesto in (1, 2, 3, 9):
        assert _carpeta_de(mundo_de_siembra, puesto) == _enlace(_carpetas(mundo_de_siembra, puesto)["carpeta_proyecto_id"])
    # los dos con el mismo nombre: la segunda carpeta lleva «· #id» y no reusa la ajena
    assert _carpetas(mundo_de_siembra, 9)["nombre_puesto"] == "Nombre-repetido · #9"


def test_un_proyecto_con_fila_y_sin_enlace_solo_recibe_el_enlace_y_drive_no_se_toca(mundo_de_siembra, capsys):
    mundo_de_siembra.con.execute("UPDATE proyectos SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id IN (2, 3, 9)")
    mundo_de_siembra.con.commit()
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "5")
    assert codigo == 0, salida
    assert _carpeta_de(mundo_de_siembra, 1) == _enlace("PREVIA1")
    assert _pedidos(mundo_de_siembra.nube) == 0                                  # ni siquiera pidió el token
    assert cd.frase_de_la_carpeta({"resultado": "ya_estaba", "causa": None, "carpeta": cd.ENLACE_PUESTO}) in salida


def test_la_segunda_corrida_no_hace_nada_ni_en_drive_ni_en_la_base(mundo_de_siembra, capsys):
    assert _correr(capsys, "--crear", "--maximo", "500")[0] == 0
    antes, pedidos = _volcado(mundo_de_siembra), _pedidos(mundo_de_siembra.nube)
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "500")
    assert codigo == 0, salida
    assert _volcado(mundo_de_siembra) == antes and _pedidos(mundo_de_siembra.nube) == pedidos
    assert "Por hacer con --crear: 0" in salida and "nada que hacer" in salida
    # y contar lo confirma
    codigo, salida, _ = _correr(capsys, "--contar")
    assert _ids_de(salida, "se crearía su carpeta") == [] and _ids_de(salida, "solo se escribiría el enlace") == []


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3. «Hecho» es lo releído de la base; un fallo de Drive detiene la corrida
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_si_el_enlace_no_quedo_escrito_no_es_hecho_y_sale_1(mundo_de_siembra, capsys, monkeypatch):
    async def no_escribe(*a, **k):
        raise RuntimeError("la puerta no deja")
    monkeypatch.setattr(crud, "editar", no_escribe)
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "1")
    assert codigo == 1, salida
    assert "NO se pudo poner su enlace" in salida and "releídos de la base: NO" in salida
    assert _carpeta_de(mundo_de_siembra, 2) is None


def test_si_preparar_dice_lista_pero_no_dejo_nada_la_base_releida_lo_desmiente(mundo_de_siembra, capsys, monkeypatch):
    async def miente(proyecto_id, **k):
        return {"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_PUESTO}
    monkeypatch.setattr(cd, "preparar_la_carpeta", miente)
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "1")
    assert codigo == 1 and "releídos de la base: NO" in salida and "hechos y releídos: 0 de 1" in salida


def test_si_el_enlace_de_la_base_es_otro_no_es_hecho(mundo_de_siembra, capsys, monkeypatch):
    real = cd.preparar_la_carpeta

    async def y_despues_alguien_lo_cambia(proyecto_id, **k):
        r = await real(proyecto_id, **k)
        mundo_de_siembra.con.execute("UPDATE proyectos SET carpeta = 'https://otro.test/x' WHERE id = ?", (proyecto_id,))
        mundo_de_siembra.con.commit()
        return r
    monkeypatch.setattr(cd, "preparar_la_carpeta", y_despues_alguien_lo_cambia)
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "1")
    assert codigo == 1 and "hechos y releídos: 0 de 1" in salida


@pytest.mark.parametrize("clave", sorted(FALLOS))
def test_un_fallo_de_drive_detiene_la_corrida_dice_la_frase_real_y_sale_1(mundo_de_siembra, capsys, clave):
    nube = mundo_de_siembra.nube
    nube.falla, causa = FALLOS[clave]
    codigo, salida, _ = _correr(capsys, "--crear", "--maximo", "3")
    assert codigo == 1, salida
    esperada = cd.frase_de_la_carpeta({"resultado": "fallo", "causa": causa, "carpeta": None})
    assert esperada in salida and "No se pudo crear la carpeta en Drive" in salida
    assert salida.count("releídos de la base: NO") == 1                  # el primero; los demás ni se intentaron
    assert "sin intentar por el fallo: #3, #9" in salida
    assert nube.pasos == [("buscar!", "/drive/v3/files")]                  # un solo pedido: no insistió
    assert _carpetas(mundo_de_siembra, 2) is None and _carpetas(mundo_de_siembra, 3) is None
    assert _carpeta_de(mundo_de_siembra, 2) is None
    assert MARCA_DE_GOOGLE not in salida


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 4. Uso mal hecho, entorno incompleto, base sin migrar
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("argv", [["--crear"], ["--crear", "--maximo", "0"], ["--crear", "--maximo", "-3"],
                                  ["--maximo", "2"], ["--crear", "--contar"], ["--crear", "--maximo", "x"]],
                         ids=["sin_maximo", "cero", "negativo", "maximo_sin_crear", "los_dos_modos", "no_es_numero"])
def test_un_uso_mal_hecho_se_niega_antes_de_abrir_la_base(mundo_de_siembra, capsys, argv):
    antes = _volcado(mundo_de_siembra)
    with pytest.raises(SystemExit) as e:
        guion.main(argv)
    capsys.readouterr()
    assert e.value.code == 2
    assert mundo_de_siembra.llamadas == {"abrir": 0, "cerrar": 0}
    assert _volcado(mundo_de_siembra) == antes and _pedidos(mundo_de_siembra.nube) == 0


def test_sin_las_variables_de_drive_crear_no_hace_nada_y_contar_dice_cuales_faltan_sin_decir_valores(
        mundo_de_siembra, capsys, monkeypatch):
    monkeypatch.setenv("LUCY_DRIVE_CLIENT_SECRET", "VALOR-QUE-NO-SE-DICE")
    monkeypatch.delenv("LUCY_DRIVE_REFRESH_TOKEN")
    antes = _volcado(mundo_de_siembra)
    codigo, salida, err = _correr(capsys, "--crear", "--maximo", "1")
    assert codigo == 2 and "LUCY_DRIVE_REFRESH_TOKEN" in err and "VALOR-QUE-NO-SE-DICE" not in err + salida
    assert _volcado(mundo_de_siembra) == antes and _pedidos(mundo_de_siembra.nube) == 0
    assert mundo_de_siembra.llamadas == {"abrir": 0, "cerrar": 0}
    codigo, salida, err = _correr(capsys, "--contar")
    assert codigo == 0 and "LUCY_DRIVE_REFRESH_TOKEN: FALTA" in salida
    assert "LUCY_DRIVE_CLIENT_SECRET: sí" in salida and "VALOR-QUE-NO-SE-DICE" not in salida + err


def test_las_variables_que_pide_son_las_cuatro_de_drive_sacadas_de_drive_py():
    assert set(guion.VARIABLES_DE_DRIVE) == {*drive.VARIABLES, drive.VARIABLE_CARPETA_PROYECTOS} and len(guion.VARIABLES_DE_DRIVE) == 4


def test_las_que_el_guion_lee_del_entorno_son_exactamente_esas_cuatro():
    """El guion lee el entorno por nombres escritos (la guarda del repositorio no deja una clave que no sea constante):
    los nombres que escribe tienen que ser los de `drive.py`, ni uno más ni uno menos."""
    leidas = {n.args[0].value for n in ast.walk(ast.parse(RUTA_DEL_GUION.read_text(encoding="utf-8")))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
              and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "environ"
              and n.args and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).startswith("LUCY_DRIVE_")}
    assert leidas == set(guion.VARIABLES_DE_DRIVE)


@pytest.mark.parametrize("que_falta", ["tabla", "columna"])
def test_una_base_sin_migrar_no_se_toca_sale_1_y_cierra_la_base(mundo_de_siembra, capsys, que_falta):
    if que_falta == "tabla":
        mundo_de_siembra.con.execute("DROP TABLE drive_de_proyectos")
    else:
        mundo_de_siembra.con.execute("ALTER TABLE proyectos DROP COLUMN carpeta")
    mundo_de_siembra.con.commit()
    antes = _volcado(mundo_de_siembra)
    for argv in (["--contar"], ["--crear", "--maximo", "1"]):
        codigo, salida, err = _correr(capsys, *argv)
        assert codigo == 1 and "No se pudo contar" in err, (salida, err)
        assert "Traceback" not in err
    assert _volcado(mundo_de_siembra) == antes and _pedidos(mundo_de_siembra.nube) == 0
    assert mundo_de_siembra.llamadas == {"abrir": 2, "cerrar": 2}


def test_el_estado_que_entra_es_uno_del_vocabulario_de_db():
    assert guion.ESTADO_QUE_ENTRA in db.ESTADOS_PROYECTO and guion.ESTADO_QUE_ENTRA == "activo"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 5. El guion no habla con Drive ni escribe en la base por su cuenta (censo con ast)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

MODULOS_QUE_PUEDE_IMPORTAR = {"__future__", "os", "sys", "argparse", "asyncio", "cotizaciones_drive", "db", "drive"}
DE_DRIVE_QUE_PUEDE_USAR = {"VARIABLES", "VARIABLE_CARPETA_PROYECTOS"}
DE_DB_QUE_PUEDE_USAR = {"abrir", "cerrar", "proyectos_vivos", "carpetas_de_proyectos", "carpetas_de_drive", "carpeta_guardada",
                        "CarpetaDeProyectoNoVale", "ESTADOS_PROYECTO"}
DE_CD_QUE_PUEDE_USAR = {"preparar_la_carpeta", "frase_de_la_carpeta", "enlace_de_carpeta_de_drive"}


def _infracciones(fuente: str) -> list[str]:
    """Todo lo que el guion hace y no debe: importar más de la cuenta, usar de `drive`/`db`/`cotizaciones_drive` algo
    que no sea lo permitido (lo de `db` es solo lectura y abrir/cerrar), o ejecutar SQL él mismo."""
    arbol, malas = ast.parse(fuente), []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            malas += [f"import {a.name}" for a in n.names if a.name.split(".")[0] not in MODULOS_QUE_PUEDE_IMPORTAR]
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] not in MODULOS_QUE_PUEDE_IMPORTAR:
                malas.append(f"from {n.module} import …")
        elif isinstance(n, ast.Attribute):
            permitido = {"drive": DE_DRIVE_QUE_PUEDE_USAR, "db": DE_DB_QUE_PUEDE_USAR, "cd": DE_CD_QUE_PUEDE_USAR}.get(
                n.value.id if isinstance(n.value, ast.Name) else None)
            if permitido is not None and n.attr not in permitido:
                malas.append(f"{n.value.id}.{n.attr}")
            if n.attr in ("execute", "executemany", "crear_carpeta", "editar"):
                malas.append(f".{n.attr}")
    return malas


def test_el_guion_solo_lee_con_db_y_pide_las_carpetas_por_preparar_la_carpeta():
    fuente = RUTA_DEL_GUION.read_text(encoding="utf-8")
    assert _infracciones(fuente) == []
    # y de verdad pide por esa puerta (no es un guion que no hace nada)
    pedidos = [n for n in ast.walk(ast.parse(fuente)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "preparar_la_carpeta"]
    assert len(pedidos) == 1


@pytest.mark.parametrize("fuente, esperada", [
    ("import drive\nasync def f():\n    await drive.crear_carpeta('x', 'y')\n", ["drive.crear_carpeta", ".crear_carpeta"]),
    ("import db.db as db\nasync def f():\n    await db.guardar_carpetas_de_drive(1, 'a', 'b', 'c')\n", ["db.guardar_carpetas_de_drive"]),
    ("from acciones import crud\n", ["from acciones import …"]),
    ("import httpx\n", ["import httpx"]),
    ("async def f(conn):\n    await conn.execute('UPDATE proyectos SET carpeta = 1')\n", [".execute"]),
    ("import cotizaciones_drive as cd\nasync def f():\n    await cd._crear_las_carpetas(1, 2, 'x', 'y')\n", ["cd._crear_las_carpetas"]),
    ("import db.db as db\nasync def f(a):\n    await db.pool.connection()\n", ["db.pool"]),
])
def test_el_censo_ve_las_entradas_inventadas(fuente, esperada):
    assert _infracciones(fuente) == esperada
