"""La carpeta de Drive nace con el proyecto (pieza 1 del diseño «Carpetas del proyecto», 18-oct-2026):
`cotizaciones_drive.preparar_la_carpeta`, `frase_de_la_carpeta`, y las tres rutas que crean un proyecto.

CÓMO. El camino es el DE PRODUCCIÓN con dos cambios, los dos en el borde: la base es SQLite con las sentencias REALES de
`db`/`crud` sobre las tablas del esquema, y Drive es el `Nube` de `tests/test_cotizaciones_drive.py` (un Drive de mentira
con memoria sobre `httpx.MockTransport`, detrás del cliente de verdad `drive.py`). Se recorren las rutas reales:
`POST /proyectos/nuevo`, `POST /tareas/{tid}/convertir-en-proyecto` y la herramienta `crear_proyecto` de Telegram
(`cerebro/agente.py::_ejecutar_herramienta` → `crud.crear_proyecto`). Ni `preparar_la_carpeta` ni `drive` se reemplazan
(salvo las pruebas que lo dicen: hacen que `preparar_la_carpeta` reviente, para ver que la ruta no se cae).

QUÉ SE VIGILA (garantías de la pieza 1; los números son los del diseño):
  A1  todo camino de producción que crea un proyecto pide la carpeta DESPUÉS de guardarlo (censo con `ast`, sacado del
      árbol, más una entrada inventada); y el del panel la pide aunque el cliente falle;
  A2  el proyecto se crea aunque Drive falle, y `preparar_la_carpeta` nunca sube una excepción (3 caminos × 13 fallas);
  A3  un proyecto, una carpeta: con la fila hecha no hay ni un pedido a Drive;
  A4  una sola manera de crear las carpetas; una cotización enviada después reusa las de `preparar`;
  A5  `proyectos.carpeta` se llena solo si está vacía, y LA CONDICIÓN DE TIZIANO (18-oct): el enlace queda puesto en el
      proyecto (se vuelve a leer de la base) y, si no se pudo poner, se DICE;
  A6  Lucy no borra ni mueve nada en Drive;
  A8  lo que se dice (Telegram, aviso del panel) sale de UNA función y coincide con lo que pasó;
  A9  una sola puerta para crear (ningún otro módulo toca `crear_carpeta`);
  A11 un Drive que no contesta no cuelga la creación más del tope del intento;
  A12 sin la tabla de Drive la creación no se rompe.

FRONTERA, dicha una vez:
  · NO hay Postgres: SQLite; `ON CONFLICT DO NOTHING` es el mismo texto pero no el mismo motor.
  · El Drive de mentira afirma lo que afirma `tests/test_drive.py` (documentación leída de memoria, sin captura real).
  · Dos creaciones a la vez del mismo proyecto corren en un solo hilo (se interfolian en cada `await`, no en paralelo);
    puede sobrar una carpeta en Drive y se declara (H5 del mapa).
  · La lista de «quién crea un proyecto» sale de `ast` sobre el árbol: no ve un nombre armado en ejecución (`getattr`).

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import asyncio
import html as _html
import re
import time

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_buzon_que_no_se_ve as buzon
from test_cotizaciones_drive import (  # noqa: F401  (el Drive de mentira, sus fixtures y ayudas)
    CARPETA_PROYECTOS, FALLOS, MARCA_DE_GOOGLE, Nube, _carpetas, _enviar, _filas, _R, google, m, mundo, gente, nube)
from test_cotizaciones_en_la_pagina import _casa
from test_grupo_ia import _ROOT

import cerebro.agente as agente
import cotizaciones_drive as cd
import db.db as db
import drive
from acciones import crud

NOMBRE = "Proyecto de la prueba"
# Estas pruebas miden la CARPETA DE DRIVE, no el cliente: se crean en el grupo IA, el único que no exige cliente y
# donde el nombre del proyecto se escribe (pieza 1 de «Un proyecto no se crea sin cliente», 18-oct-2026).
AREA = db.AREA_TECNICA


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# Los tres caminos de producción, tal como los recorre un usuario
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _id_de(m_, nombre) -> int:
    f = _filas(m_, f"SELECT id FROM proyectos WHERE nombre = '{nombre}'")
    assert len(f) == 1, (nombre, f)
    return f[0]["id"]


def _por_el_panel(m_, nombre=NOMBRE, cliente=None, c=None):
    """«+ Proyecto» del panel: (respuesta del POST, el navegador, página que sigue, id del proyecto)."""
    c = c or _casa()
    datos = {"nombre": nombre, "area": AREA, "responsable": "Persona Dos"}
    if cliente is not None:
        datos["cliente"] = cliente
    r = c.post("/proyectos/nuevo", data=datos, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    pagina = c.get(r.headers["location"].split("#")[0]).text
    return r, c, _html.unescape(pagina), _id_de(m_, nombre)


def _por_telegram(m_, nombre=NOMBRE):
    """La herramienta `crear_proyecto` del agente, REAL: (lo que le contesta al modelo, id del proyecto)."""
    acciones: list = []
    r = asyncio.run(agente._ejecutar_herramienta(
        "crear_proyecto", {"nombre": nombre, "grupo": AREA, "responsable": "Persona Dos"}, 1, acciones))
    return r, _id_de(m_, nombre)


def _convirtiendo(m_, nombre=NOMBRE, c=None):
    """«Convertir en proyecto» una tarea suelta: (respuesta del POST, id del proyecto)."""
    m_.tarea(50, nombre, area=AREA)
    m_.con.commit()
    c = c or _casa()
    r = c.post("/tareas/50/convertir-en-proyecto", follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r, _id_de(m_, nombre)


CAMINOS = ["panel", "telegram", "convertir"]


def _crear(m_, camino, nombre=NOMBRE):
    """Crea un proyecto por ese camino y devuelve `(pid, lo_que_dijo)`. `lo_que_dijo` es la página que sigue (panel), lo
    que le contesta al modelo (Telegram) o la dirección a la que redirige (convertir)."""
    if camino == "panel":
        r, _, pagina, pid = _por_el_panel(m_, nombre)
        return pid, pagina
    if camino == "telegram":
        texto, pid = _por_telegram(m_, nombre)
        return pid, texto
    r, pid = _convirtiendo(m_, nombre)
    return pid, r.headers["location"]


def _proyecto(m_, pid):
    return _filas(m_, f"SELECT * FROM proyectos WHERE id = {pid}")[0]


def _huellas(m_, pid, accion=None):
    extra = f" AND accion = '{accion}'" if accion else ""
    return _filas(m_, f"SELECT * FROM log_acciones WHERE tabla = 'proyectos' AND registro_id = {pid}{extra} ORDER BY id")


def _preparar(pid, **kw):
    return asyncio.run(cd.preparar_la_carpeta(pid, **kw))


def _enlace(carpeta_id):
    return f"https://drive.google.com/drive/folders/{carpeta_id}"


@pytest.fixture
def espia(monkeypatch):
    """Anota lo que `preparar_la_carpeta` contesta, SIN cambiar nada: llama a la de verdad."""
    resultados: list = []
    real = cd.preparar_la_carpeta

    async def con_espia(*a, **k):
        r = await real(*a, **k)
        resultados.append(r)
        return r
    monkeypatch.setattr(cd, "preparar_la_carpeta", con_espia)
    return resultados


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 0. El camino bueno por cada camino, con lo que quedó en Drive y en la base — y el enlace en el proyecto
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("camino", CAMINOS)
def test_al_crear_el_proyecto_nacen_sus_dos_carpetas_en_drive_y_su_enlace_queda_puesto_en_el_proyecto(m, nube, camino, espia):
    pid, dijo = _crear(m, camino)
    carpetas = _carpetas(m, pid)
    # Drive: Proyectos/<nombre>/cotizaciones, y nada más
    assert nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]]
    assert nube.cosas[carpetas["carpeta_proyecto_id"]]["nombre"] == NOMBRE == carpetas["nombre_puesto"]
    assert nube.hijos(carpetas["carpeta_proyecto_id"]) == [carpetas["carpeta_cotizaciones_id"]]
    assert nube.cosas[carpetas["carpeta_cotizaciones_id"]]["nombre"] == "cotizaciones"
    assert nube.archivos() == []
    # LA CONDICIÓN DE TIZIANO: el enlace quedó escrito en el proyecto, leído de la base DESPUÉS de crear
    assert _proyecto(m, pid)["carpeta"] == _enlace(carpetas["carpeta_proyecto_id"])
    assert db.enlace_de_carpeta(_proyecto(m, pid)["carpeta"]) == _proyecto(m, pid)["carpeta"]     # y la página lo deja abrir
    # con su huella: la del alta y la del enlace, de panel, con su motivo verdadero
    assert [h["accion"] for h in _huellas(m, pid)] == ["crear", "editar"]
    editar = _huellas(m, pid, "editar")[0]
    assert editar["actor"] == "panel" and editar["motivo"] == cd.MOTIVO_AL_CREAR_EL_PROYECTO
    assert "al crear el proyecto" in editar["motivo"] and "cotización" not in editar["motivo"]
    # y lo que contestó la función
    assert espia == [{"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_PUESTO}]
    assert not nube.pasos_de("subir_nuevo") and not [p for p in nube.google.de_api() if p.method not in ("GET", "POST")]
    # lo que dijo la pantalla: nada de fallo
    assert "No se pudo crear la carpeta" not in dijo and "NO se pudo poner" not in dijo and "carpeta_drive" not in dijo
    if camino == "telegram":
        assert dijo.endswith(cd.frase_de_la_carpeta(espia[0])) and "Carpeta de Drive creada." in dijo


def test_el_motivo_de_la_subida_de_una_cotizacion_sigue_diciendo_que_fue_por_la_cotizacion(m, nube):
    _enviar(proyecto=2)
    h = _huellas(m, 2, "editar")
    assert [x["motivo"] for x in h] == [cd.MOTIVO_AL_SUBIR] and "al subir una cotización" in h[0]["motivo"]


def test_un_proyecto_que_no_existe_o_esta_en_la_papelera_no_tiene_carpeta(m, nube):
    m.con.execute("UPDATE proyectos SET borrado_en = '2026-10-01T00:00:00+00:00' WHERE id = 3")
    m.con.commit()
    for pid in (3, 9999):
        assert _preparar(pid) == {"resultado": "no_esta", "causa": None, "carpeta": None}
    assert nube.pasos == [] and nube.google.pedidos == [] and _carpetas(m, 3) is None
    assert "no se pudo crear" in cd.frase_de_la_carpeta({"resultado": "no_esta"}).lower()


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A1. Todo camino que crea un proyecto pide la carpeta, DESPUÉS de guardarlo (los hermanos, sacados del árbol)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

CREADORES = ("crear_proyecto", "convertir_tarea_en_proyecto", "buscar_o_crear_proyecto")
PIDE_LA_CARPETA = "preparar_la_carpeta"


def _modulos(raiz):
    """[(ruta relativa, árbol)] de todo `.py` del repo menos `tests/` (la lista sale del barrido de
    `test_buzon_que_no_se_ve`, que le pregunta a cada carpeta si es un entorno virtual)."""
    pruebas = [p.resolve() for p in buzon._testpaths(raiz)]
    salida = []
    for py in buzon._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        salida.append((real.relative_to(raiz.resolve()).as_posix(), ast.parse(real.read_text(encoding="utf-8"), str(real))))
    return salida


def _funciones(arbol):
    return [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _nombre_llamado(n):
    return n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else None


def _alias_de_db(arbol) -> set:
    """Los nombres con que un módulo trae `db/db.py` (`import db.db as db`, `import db.db as basedatos`, `from db import
    db as x`)."""
    nombres = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            nombres |= {a.asname or a.name.split(".")[0] for a in n.names if a.name == "db.db"}
        elif isinstance(n, ast.ImportFrom) and n.module == "db":
            nombres |= {a.asname or a.name for a in n.names if a.name == "db"}
    return nombres


def _crea_un_proyecto(n, alias) -> bool:
    """Una llamada `<alias de db>.crear_proyecto(...)` (o `convertir_tarea_en_proyecto`, `buscar_o_crear_proyecto`)."""
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CREADORES
            and isinstance(n.func.value, ast.Name) and n.func.value.id in alias)


def _sin_la_carpeta(raiz) -> list:
    """Las funciones que LLAMAN a un creador de proyectos (`db.crear_proyecto`, `db.convertir_tarea_en_proyecto`,
    `db.buscar_o_crear_proyecto`, con `db` tal como el módulo la trae) y no piden la carpeta DESPUÉS: pedirla es llamar
    a `preparar_la_carpeta`, o a una función del mismo módulo que la llama. «Después»: la primera petición está en una
    línea posterior a la última llamada al creador."""
    malos = []
    for rel, arbol in _modulos(raiz):
        alias = _alias_de_db(arbol)
        ayudantes = {f.name for f in _funciones(arbol)
                     if any(isinstance(n, ast.Call) and _nombre_llamado(n) == PIDE_LA_CARPETA for n in ast.walk(f))}
        for f in _funciones(arbol):
            llamadas = [n for n in ast.walk(f) if isinstance(n, ast.Call)]
            creando = [n.lineno for n in llamadas if _crea_un_proyecto(n, alias)]
            if not creando:
                continue
            pidiendo = [n.lineno for n in llamadas if _nombre_llamado(n) == PIDE_LA_CARPETA
                        or (isinstance(n.func, ast.Name) and n.func.id in ayudantes)]
            if not pidiendo or min(pidiendo) <= max(creando):
                malos.append((rel, f.name))
    return sorted(malos)


def _con_creadores(raiz) -> list:
    salida = []
    for rel, arbol in _modulos(raiz):
        alias = _alias_de_db(arbol)
        salida += [(rel, f.name) for f in _funciones(arbol) if any(_crea_un_proyecto(n, alias) for n in ast.walk(f))]
    return sorted(salida)


def test_todo_camino_que_crea_un_proyecto_pide_la_carpeta_despues_de_guardarlo():
    creadores = _con_creadores(_ROOT)
    # el trinquete: hoy son estos tres; uno nuevo se pone rojo hasta que alguien lo mire (y la lista sale del árbol)
    assert creadores == [("acciones/crud.py", "crear_proyecto"), ("web/app.py", "convertir_en_proyecto"),
                         ("web/app.py", "crear_proyecto_nuevo")], creadores
    assert _sin_la_carpeta(_ROOT) == []


def test_el_censo_ve_un_creador_que_no_pide_la_carpeta_o_la_pide_antes(tmp_path):
    """Entradas inventadas: la misma regla sobre módulos que no están en el repo."""
    (tmp_path / "mal.py").write_text(
        "import db.db as db\n"
        "async def sin_pedirla():\n    await db.crear_proyecto('x', 'CDS', 1)\n"
        "async def pidiendola_antes(cd):\n    await cd.preparar_la_carpeta(1)\n    await db.convertir_tarea_en_proyecto(1)\n"
        "async def por_otro_nombre():\n    await db.buscar_o_crear_proyecto('x')\n",
        encoding="utf-8")
    (tmp_path / "alias.py").write_text(
        "import db.db as basedatos\n"
        "async def con_otro_alias():\n    await basedatos.crear_proyecto('x', 'CDS', 1)\n",
        encoding="utf-8")
    (tmp_path / "bien.py").write_text(
        "import db.db as db\n"
        "async def pidiendola(cd):\n    p = await db.crear_proyecto('x', 'CDS', 1)\n    await cd.preparar_la_carpeta(p['id'])\n"
        "async def ayudante(cd, pid):\n    await cd.preparar_la_carpeta(pid)\n"
        "async def con_ayudante():\n    p = await db.crear_proyecto('x', 'CDS', 1)\n    await ayudante(None, p['id'])\n"
        "class Otro:\n    def crear_proyecto(self):\n        return 1\n"
        "async def no_es_db(x):\n    await x.crear_proyecto()\n",
        encoding="utf-8")
    assert _sin_la_carpeta(tmp_path) == [("alias.py", "con_otro_alias"), ("mal.py", "pidiendola_antes"),
                                         ("mal.py", "por_otro_nombre"), ("mal.py", "sin_pedirla")]


def test_en_el_panel_si_noco_no_contesta_no_hay_proyecto_ni_carpeta(m, nube, monkeypatch):
    """Pieza 1 de «Un proyecto no se crea sin cliente» (18-oct-2026): ya no hay «crear sin cliente y avisar». Si Noco no
    contesta la puerta no escribe NADA, y como la carpeta se pide DESPUÉS de guardar, tampoco hay carpeta en Drive."""
    import noco_lectura

    async def sin_noco(noco_id, **k):
        raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
    monkeypatch.setattr(noco_lectura, "persona", sin_noco)
    antes = [_filas(m, f"SELECT * FROM {t}") for t in ("proyectos", "log_acciones", "drive_de_proyectos")]
    c = _casa()
    r = c.post("/proyectos/nuevo", data={"nombre": NOMBRE, "area": AREA, "responsable": "Persona Dos", "cliente": "101"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proyectos?nuevo={AREA}&error=cliente_noco"
    despues = [_filas(m, f"SELECT * FROM {t}") for t in ("proyectos", "log_acciones", "drive_de_proyectos")]
    assert despues == antes, "escribió algo (proyecto, huella o carpeta) aunque Noco no contestó"


def test_en_el_panel_con_cliente_el_proyecto_nace_con_el_y_la_carpeta_se_pide_despues(m, nube, monkeypatch):
    import noco_lectura

    async def ficha(noco_id, **k):
        return {"id": noco_id, "nombre": "Ficha de Noco"}
    monkeypatch.setattr(noco_lectura, "persona", ficha)
    r, _, pagina, pid = _por_el_panel(m, cliente="101")
    assert (_proyecto(m, pid)["cliente_noco_id"], _proyecto(m, pid)["cliente_nombre"]) == (101, "Ficha de Noco")
    assert _carpetas(m, pid) is not None and _proyecto(m, pid)["carpeta"] == _enlace(_carpetas(m, pid)["carpeta_proyecto_id"])


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A2. El proyecto se crea aunque Drive falle; `preparar_la_carpeta` nunca sube una excepción
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

async def _lenta(paso, p):
    await asyncio.sleep(0.1)
    return None


def _falla_en_la_base(nombre):
    async def revienta(*a, **k):
        raise RuntimeError(MARCA_DE_GOOGLE)
    return lambda mp, n, m_: mp.setattr(db, nombre, revienta)


def _sin_la_tabla(mp, n, m_):
    m_.con.execute("DROP TABLE drive_de_proyectos")
    m_.con.commit()


def _el_tope_corto(mp, n, m_):
    mp.setattr(cd, "TIEMPO_DEL_INTENTO", 0.25)
    n.falla = _lenta


def _con_falla(clave):
    return lambda mp, n, m_: setattr(n, "falla", FALLOS[clave][0])


# nombre → (cómo se rompe, la causa que debe decir la frase; None = no hay causa que enseñar)
FALLAS_DE_DRIVE = {
    "sin_configurar": (lambda mp, n, m_: mp.delenv("LUCY_DRIVE_REFRESH_TOKEN"), drive.DriveSinConfigurar.causa),
    "sin_carpeta_proyectos": (lambda mp, n, m_: mp.delenv("LUCY_DRIVE_CARPETA_PROYECTOS"), drive.DriveSinCarpetaProyectos.causa),
    "401": (_con_falla("401"), FALLOS["401"][1]),
    "403": (_con_falla("403"), FALLOS["403"][1]),
    "500": (_con_falla("500"), FALLOS["500"][1]),
    "red": (_con_falla("red"), FALLOS["red"][1]),
    "404_al_crear": (lambda mp, n, m_: setattr(n, "falla", lambda paso, p: _R(404, json={}) if paso == "crear_carpeta" else None),
                     drive.DriveSinCarpetaProyectos.causa),
    "mas_de_15_s": (_el_tope_corto, drive.DriveNoContesta.causa),
    "la_base_no_deja_leer_el_proyecto": (_falla_en_la_base("proyecto_para_la_carpeta"), drive.DriveError.causa),
    "la_base_no_deja_leer_las_carpetas": (_falla_en_la_base("carpetas_de_drive"), drive.DriveError.causa),
    "la_base_no_deja_anotar_las_carpetas": (_falla_en_la_base("guardar_carpetas_de_drive"), drive.DriveError.causa),
    "falta_la_tabla": (_sin_la_tabla, drive.DriveError.causa),
}


@pytest.mark.parametrize("falla", sorted(FALLAS_DE_DRIVE))
@pytest.mark.parametrize("camino", CAMINOS)
def test_con_drive_roto_el_proyecto_se_crea_la_ruta_contesta_lo_de_siempre_y_la_pantalla_dice_que_no_hay_carpeta(
        m, nube, monkeypatch, camino, falla, espia):
    rompe, causa = FALLAS_DE_DRIVE[falla]
    rompe(monkeypatch, nube, m)
    pid, dijo = _crear(m, camino)
    # el proyecto existe, vivo, con su huella de creación
    assert _proyecto(m, pid)["borrado_en"] is None and [h["accion"] for h in _huellas(m, pid)] == ["crear"]
    # nada quedó a medias en la base ni se llenó la carpeta
    assert _proyecto(m, pid)["carpeta"] is None
    if falla != "falta_la_tabla":
        assert _carpetas(m, pid) is None
    # lo que la función contestó
    assert espia == [{"resultado": "fallo", "causa": causa, "carpeta": None}], espia
    frase = cd.frase_de_la_carpeta(espia[0])
    assert frase.startswith("No se pudo crear la carpeta en Drive") and "creada" not in frase
    assert (f"({causa})" in frase) and MARCA_DE_GOOGLE not in frase
    # y lo que dijo cada camino
    if camino == "panel":
        assert "El proyecto se creó." in dijo and cd.frase_de_la_carpeta(cd.AVISOS_DE_LA_CARPETA["carpeta_drive_fallo"]) in dijo
    elif camino == "telegram":
        assert dijo.startswith("OK: proyecto «") and "(acción #" in dijo and dijo.endswith(frase)
        assert "Carpeta de Drive creada" not in dijo
    else:
        assert dijo == f"/proyectos?creado={pid}#proyecto-{pid}"
    # y de lo que se haya creado en Drive, nada que mienta: sin fila no hay "propias" a medias salvo las que Drive hizo
    assert not nube.pasos_de("subir_nuevo")


@pytest.mark.parametrize("camino", CAMINOS)
def test_si_preparar_la_carpeta_reventara_la_ruta_contesta_lo_mismo_y_el_proyecto_existe(m, nube, monkeypatch, camino):
    """El cinturón de la ruta: aunque la función prometa no subir excepciones, si algún día sube una, el proyecto ya
    guardado, la redirección y la respuesta salen como siempre."""
    async def revienta(*a, **k):
        raise RuntimeError(MARCA_DE_GOOGLE)
    monkeypatch.setattr(cd, "preparar_la_carpeta", revienta)
    pid, dijo = _crear(m, camino)
    assert _proyecto(m, pid)["borrado_en"] is None and [h["accion"] for h in _huellas(m, pid)] == ["crear"]
    assert MARCA_DE_GOOGLE not in dijo
    if camino == "panel":
        assert "No se pudo crear la carpeta en Drive" in dijo
    elif camino == "telegram":
        assert dijo.startswith("OK: proyecto «") and "No se pudo crear la carpeta en Drive" in dijo
    else:
        assert dijo == f"/proyectos?creado={pid}#proyecto-{pid}"


def test_la_funcion_nunca_sube_una_excepcion_ni_siquiera_si_todo_lo_que_toca_revienta(m, nube, monkeypatch):
    async def revienta(*a, **k):
        raise RuntimeError(MARCA_DE_GOOGLE)
    for nombre in ("proyecto_para_la_carpeta", "carpetas_de_drive", "guardar_carpetas_de_drive", "carpetas_de_proyectos"):
        monkeypatch.setattr(db, nombre, revienta)
    monkeypatch.setattr(crud, "editar", revienta)
    monkeypatch.setattr(drive, "crear_carpeta", revienta)
    monkeypatch.setattr(drive, "buscar_en", revienta)
    r = _preparar(2)
    assert r["resultado"] == "fallo" and r["causa"] in cd.CAUSAS and MARCA_DE_GOOGLE not in repr(r)


def test_el_texto_de_google_no_llega_a_los_registros(m, nube, caplog):
    nube.falla = FALLOS["500"][0]
    with caplog.at_level("DEBUG"):
        _preparar(2)
    assert MARCA_DE_GOOGLE not in caplog.text and "no se pudo crear la carpeta del proyecto #2" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A3. Un proyecto, una carpeta
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_la_segunda_vez_no_hace_ni_un_pedido_a_drive_y_dice_que_ya_estaba(m, nube):
    assert _preparar(2)["resultado"] == "lista"
    antes, pedidos = list(nube.pasos), len(nube.google.pedidos)
    segunda = _preparar(2)
    assert segunda == {"resultado": "ya_estaba", "causa": None, "carpeta": cd.ENLACE_YA_TENIA}
    assert nube.pasos == antes and len(nube.google.pedidos) == pedidos
    assert len(nube.carpetas_de_proyectos()) == 1 and len(_filas(m, "SELECT * FROM drive_de_proyectos")) == 1
    assert cd.frase_de_la_carpeta(segunda) == "El proyecto ya tiene su carpeta en Drive."


def test_dos_creaciones_a_la_vez_dejan_una_sola_fila_y_el_que_pierde_dice_que_ya_estaba(m, nube):
    async def las_dos():
        return await asyncio.gather(cd.preparar_la_carpeta(2), cd.preparar_la_carpeta(2))
    resultados = asyncio.run(las_dos())
    assert sorted(r["resultado"] for r in resultados) == ["lista", "ya_estaba"], resultados
    fila = _carpetas(m, 2)
    assert len(_filas(m, "SELECT * FROM drive_de_proyectos")) == 1 and fila["carpeta_proyecto_id"] in nube.cosas
    assert _proyecto(m, 2)["carpeta"] == _enlace(fila["carpeta_proyecto_id"])
    # FRONTERA declarada (H5 del mapa): el que perdió puede dejar una carpeta de más en Drive; no se cierra aquí
    assert 1 <= len(nube.carpetas_de_proyectos()) <= 2


def test_con_la_fila_hecha_y_el_enlace_sin_poner_se_pone_sin_llamar_a_drive(m, nube):
    """Si la creación anterior se cayó entre anotar las carpetas y escribir el enlace, el reintento lo arregla."""
    _preparar(2)
    m.con.execute("UPDATE proyectos SET carpeta = NULL WHERE id = 2")
    m.con.commit()
    pedidos = len(nube.google.pedidos)
    r = _preparar(2)
    assert r == {"resultado": "ya_estaba", "causa": None, "carpeta": cd.ENLACE_PUESTO}
    assert len(nube.google.pedidos) == pedidos
    assert _proyecto(m, 2)["carpeta"] == _enlace(_carpetas(m, 2)["carpeta_proyecto_id"])


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A4. Una sola manera de crear las carpetas: la de la subida de cotizaciones
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_una_cotizacion_enviada_despues_reusa_las_carpetas_que_dejo_preparar(m, nube):
    _preparar(2)
    carpetas = _carpetas(m, 2)
    _enviar(proyecto=2)
    assert _carpetas(m, 2) == carpetas
    assert nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]]
    assert [c["tipo"] for i, c in nube.cosas.items() if i != CARPETA_PROYECTOS].count("carpeta") == 2, nube.cosas
    assert [nube.cosas[i]["padre"] for i in nube.archivos()] == [carpetas["carpeta_cotizaciones_id"]]


def test_dos_proyectos_de_igual_nombre_uno_en_la_papelera_la_segunda_carpeta_lleva_su_numero_y_no_reusa_la_ajena(m, nube):
    m.con.execute("UPDATE proyectos SET nombre = 'Disco repetido' WHERE id IN (2, 3)")
    m.con.commit()
    _preparar(2)
    m.con.execute("UPDATE proyectos SET borrado_en = '2026-10-01T00:00:00+00:00' WHERE id = 2")
    m.con.commit()
    _preparar(3)
    a, b = _carpetas(m, 2), _carpetas(m, 3)
    assert a["carpeta_proyecto_id"] != b["carpeta_proyecto_id"]
    assert nube.cosas[a["carpeta_proyecto_id"]]["nombre"] == "Disco repetido"
    assert nube.cosas[b["carpeta_proyecto_id"]]["nombre"] == "Disco repetido · #3" == b["nombre_puesto"]


@pytest.mark.parametrize("viejo", ["cerrado", "pausado"])
def test_un_cliente_que_vuelve_con_un_proyecto_nuevo_la_carpeta_del_nuevo_lleva_su_numero_y_la_vieja_conserva_su_nombre(
        m, nube, viejo):
    """G7r de «Un proyecto no se crea sin cliente» (18-oct-2026): en CDS y ACD el nombre del proyecto es el del cliente, así
    que el proyecto nuevo del mismo cliente se llama IGUAL que el viejo (pausado o cerrado). Las carpetas no chocan: la del
    nuevo queda «<nombre> · #<número>» y la del viejo no se toca ni se reusa."""
    m.con.execute("UPDATE proyectos SET nombre = 'Cliente repetido' WHERE id IN (2, 3)")
    m.con.commit()
    _preparar(2)
    m.con.execute("UPDATE proyectos SET estado = ? WHERE id = 2", (viejo,))
    m.con.commit()
    _preparar(3)
    a, b = _carpetas(m, 2), _carpetas(m, 3)
    assert a["carpeta_proyecto_id"] != b["carpeta_proyecto_id"]
    assert nube.cosas[a["carpeta_proyecto_id"]]["nombre"] == "Cliente repetido" == a["nombre_puesto"]
    assert nube.cosas[b["carpeta_proyecto_id"]]["nombre"] == "Cliente repetido · #3" == b["nombre_puesto"]


def test_preparar_no_habla_con_drive_por_su_cuenta_y_crea_por_la_unica_funcion_de_las_carpetas():
    arbol = ast.parse((_ROOT / "cotizaciones_drive.py").read_text(encoding="utf-8"))
    preparar = next(f for f in _funciones(arbol) if f.name == "preparar_la_carpeta")
    llamadas = [n for n in ast.walk(preparar) if isinstance(n, ast.Call)]
    assert [ast.unparse(n.func) for n in llamadas if ast.unparse(n.func).startswith("drive.")] == []
    assert [ast.unparse(n.func) for n in llamadas].count("_crear_las_carpetas") == 1
    creadoras = sorted(f.name for f in _funciones(arbol)
                       if any(isinstance(n, ast.Call) and ast.unparse(n.func) == "_crear_las_carpetas" for n in ast.walk(f)))
    # (`la_carpeta_de_facturas_del_proyecto`, pieza 8 de «Facturas»: la subcarpeta `facturas` de un proyecto que todavía no
    # tenía carpetas las crea por la MISMA función, no por otra)
    assert creadoras == ["_llevar_a_drive", "la_carpeta_de_facturas_del_proyecto", "preparar_la_carpeta"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A5. `proyectos.carpeta`: solo si está vacía; el enlace queda puesto, y si no se pudo se DICE
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("escrita", ["https://drive.example.test/carpetas/uno", "D:\\Proyectos\\Disco", "~/Disco", " x "])
def test_una_carpeta_escrita_no_se_pisa_se_crea_la_de_drive_y_se_dice(m, nube, escrita):
    m.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 2", (escrita,))
    m.con.commit()
    antes = len(_huellas(m, 2))
    r = _preparar(2)
    assert r == {"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_YA_TENIA}
    assert _proyecto(m, 2)["carpeta"] == escrita and len(_huellas(m, 2)) == antes
    assert _carpetas(m, 2) is not None and len(nube.carpetas_de_proyectos()) == 1
    assert "ya tenía otra carpeta escrita y no se cambió" in cd.frase_de_la_carpeta(r)


@pytest.mark.parametrize("vacia", [None, "", "   "])
def test_una_carpeta_vacia_se_llena_una_vez_con_huella(m, nube, vacia):
    m.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 2", (vacia,))
    m.con.commit()
    r = _preparar(2)
    assert r["carpeta"] == cd.ENLACE_PUESTO
    assert _proyecto(m, 2)["carpeta"] == _enlace(_carpetas(m, 2)["carpeta_proyecto_id"])
    assert [h["accion"] for h in _huellas(m, 2)] == ["editar"]
    _preparar(2)
    assert [h["accion"] for h in _huellas(m, 2)] == ["editar"]          # la segunda no escribe otra


def _enlace_roto(mp, m_):
    async def revienta(*a, **k):
        raise RuntimeError(MARCA_DE_GOOGLE)
    mp.setattr(crud, "editar", revienta)


def _editar_que_no_escribe(mp, m_):
    async def calla(*a, **k):
        return {"id": 2}, None            # dice que sí y no escribe nada
    mp.setattr(crud, "editar", calla)


def _editar_que_escribe_otro_valor(mp, m_):
    """Otro escritor se adelanta: `editar` deja en `carpeta` un valor que no es el enlace de Lucy."""
    real = crud.editar

    async def otro(tabla, pid, cambios, **k):
        return await real(tabla, pid, {"carpeta": "https://otro.example.test/otra"}, **k)
    mp.setattr(crud, "editar", otro)


def _editar_que_la_puerta_rechaza(mp, m_):
    async def rechaza(*a, **k):
        raise ValueError("la puerta de la carpeta dijo que no")
    mp.setattr(crud, "editar", rechaza)


def _sin_la_columna(mp, m_):
    async def sin_columna():
        return None                       # 42703: la columna no existe todavía
    mp.setattr(db, "carpetas_de_proyectos", sin_columna)


@pytest.mark.parametrize("como", [_enlace_roto, _editar_que_no_escribe, _editar_que_escribe_otro_valor,
                                  _editar_que_la_puerta_rechaza, _sin_la_columna],
                         ids=["editar_revienta", "editar_no_escribe", "editar_escribe_otro_valor", "la_puerta_rechaza",
                              "sin_la_columna"])
def test_si_el_enlace_no_se_pudo_poner_la_carpeta_de_drive_se_crea_pero_la_funcion_lo_dice_y_nada_dice_que_quedo(m, nube, monkeypatch, como):
    como(monkeypatch, m)
    r = _preparar(2)
    assert r == {"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_NO_SE_PUDO}, r
    assert _carpetas(m, 2) is not None
    otro = "https://otro.example.test/otra"
    assert _proyecto(m, 2)["carpeta"] == (otro if como is _editar_que_escribe_otro_valor else None)
    frase = cd.frase_de_la_carpeta(r)
    assert frase.startswith("Carpeta de Drive creada.") and "NO se pudo poner su enlace en el proyecto" in frase
    assert cd.clave_de_aviso_de_la_carpeta(r) == "carpeta_drive_enlace"
    assert MARCA_DE_GOOGLE not in frase


@pytest.mark.parametrize("camino", CAMINOS)
def test_si_el_enlace_no_se_pudo_poner_cada_camino_lo_dice(m, nube, monkeypatch, camino, espia):
    _enlace_roto(monkeypatch, m)
    pid, dijo = _crear(m, camino)
    assert espia == [{"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_NO_SE_PUDO}]
    assert _carpetas(m, pid) is not None and _proyecto(m, pid)["carpeta"] is None
    if camino == "panel":
        assert cd.frase_de_la_carpeta(espia[0]) in dijo and "NO se pudo poner su enlace" in dijo
    elif camino == "telegram":
        assert dijo.endswith(cd.frase_de_la_carpeta(espia[0])) and "NO se pudo poner su enlace" in dijo
    # (convertir no tiene aviso propio: redirige a la página del proyecto)


def test_el_enlace_que_se_escribe_pasa_la_puerta_de_la_carpeta_del_proyecto(m, nube):
    _preparar(2)
    puesta = _proyecto(m, 2)["carpeta"]
    assert db.carpeta_guardada(puesta) == puesta and len(puesta) <= db.LARGO_CARPETA_PROYECTO
    assert crud.PUERTAS["proyectos"]["carpeta"](puesta) == puesta


def test_deshacer_la_creacion_de_telegram_sigue_funcionando_con_la_edicion_del_enlace_en_medio(m, nube):
    texto, pid = _por_telegram(m)
    creada = _huellas(m, pid, "crear")[0]
    asyncio.run(crud.deshacer(creada["id"]))
    assert _proyecto(m, pid)["borrado_en"] is not None


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A6. Lucy no borra ni mueve nada en Drive
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _lo_que_borra_o_mueve(arbol) -> list:
    """Dentro de un módulo: cualquier método HTTP que no sea GET/POST/PATCH, `trashed` fuera de la consulta de
    `trashed = false`, `addParents`/`removeParents`, y el método `.delete(`/`DELETE`."""
    hallados = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if v.upper() in ("DELETE", "PUT", "TRASH", "EMPTYTRASH", "COPY", "MOVE"):
                hallados.append(v)
            if re.search(r"addParents|removeParents|\btrashed\b(?!\s*=\s*false)", v):
                hallados.append(v[:40])
        if isinstance(n, ast.Attribute) and n.attr in ("delete", "trash"):
            hallados.append(n.attr)
        if isinstance(n, ast.keyword) and n.arg in ("addParents", "removeParents", "trashed"):
            hallados.append(n.arg)
        if isinstance(n, ast.Dict):
            hallados += [k.value for k in n.keys if isinstance(k, ast.Constant) and k.value in ("trashed", "addParents", "removeParents")]
    return hallados


def test_drive_py_y_la_politica_no_borran_ni_mueven_nada():
    for ruta in ("drive.py", "cotizaciones_drive.py"):
        assert _lo_que_borra_o_mueve(ast.parse((_ROOT / ruta).read_text(encoding="utf-8"))) == [], ruta
    # y la guarda ve lo inventado, escrito de varias maneras
    for fuente in ('_pedir("borrar", "DELETE", url)', 'c.delete(url)', 'json_={"trashed": True}',
                   'params={"addParents": "x"}', 'q = "x and trashed = true"', 'f(removeParents="x")'):
        assert _lo_que_borra_o_mueve(ast.parse(fuente)) != [], fuente


@pytest.mark.parametrize("camino", CAMINOS)
def test_crear_un_proyecto_solo_lee_y_crea_en_drive(m, nube, camino):
    _crear(m, camino)
    assert {(p.method, p.url.path) for p in nube.google.de_api()} == {("GET", "/drive/v3/files"), ("POST", "/drive/v3/files")}
    assert all("DELETE" != p.method for p in nube.google.pedidos)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A8. Lo que se dice sale de UNA función y coincide con lo que pasó
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

RESULTADOS = [{"resultado": r, "causa": c, "carpeta": k} for r in ("lista", "ya_estaba", "no_esta", "fallo")
              for c in (None, drive.DriveNoContesta.causa, MARCA_DE_GOOGLE)
              for k in (None, cd.ENLACE_PUESTO, cd.ENLACE_YA_TENIA, cd.ENLACE_NO_SE_PUDO)]


@pytest.mark.parametrize("r", RESULTADOS, ids=lambda r: f"{r['resultado']}-{r['carpeta']}-{(r['causa'] or 'sin')[:6]}")
def test_la_frase_solo_afirma_la_carpeta_si_el_resultado_lo_dice_y_nunca_enseña_texto_ajeno(r):
    frase = cd.frase_de_la_carpeta(r)
    afirma = "creada" in frase or "ya tiene su carpeta" in frase
    assert afirma == (r["resultado"] in ("lista", "ya_estaba"))
    assert MARCA_DE_GOOGLE not in frase
    if r["causa"] == drive.DriveNoContesta.causa and r["resultado"] == "fallo":
        assert f"({drive.DriveNoContesta.causa})" in frase
    assert ("NO se pudo poner su enlace" in frase) == (r["resultado"] in ("lista", "ya_estaba") and r["carpeta"] == cd.ENLACE_NO_SE_PUDO)
    assert (cd.clave_de_aviso_de_la_carpeta(r) is None) == (r["resultado"] in ("lista", "ya_estaba") and r["carpeta"] != cd.ENLACE_NO_SE_PUDO)


@pytest.mark.parametrize("basura", [None, "lista", 5, {}, {"resultado": "quizá"}, {"resultado": None}, {"resultado": ["lista"]}])
def test_lo_que_la_frase_no_entiende_lo_dice_como_que_no_se_creo(basura):
    frase = cd.frase_de_la_carpeta(basura)
    assert frase.startswith("No se pudo crear la carpeta en Drive") and "creada" not in frase
    assert cd.clave_de_aviso_de_la_carpeta(basura) == "carpeta_drive_fallo"


def test_cada_aviso_del_panel_cuenta_un_resultado_y_la_clave_vuelve_a_su_resultado():
    assert sorted(cd.AVISOS_DE_LA_CARPETA) == ["carpeta_drive_enlace", "carpeta_drive_fallo", "carpeta_drive_no_esta",
                                               "carpeta_drive_ya_enlace"]
    for clave, resultado in cd.AVISOS_DE_LA_CARPETA.items():
        assert cd.clave_de_aviso_de_la_carpeta(resultado) == clave


def _resultados_que_preparar_puede_dar() -> list[dict]:
    """Todos los resultados posibles, SACADOS DE LO REAL: los textos de `"resultado"` de los diccionarios que
    `preparar_la_carpeta` devuelve (por `ast`) combinados con cada `ENLACE_*` del módulo (y sin enlace)."""
    arbol = ast.parse((_ROOT / "cotizaciones_drive.py").read_text(encoding="utf-8"))
    preparar = next(f for f in _funciones(arbol) if f.name == "preparar_la_carpeta")
    nombres = {v.value for n in ast.walk(preparar) if isinstance(n, ast.Dict)
               for k, v in zip(n.keys, n.values)
               if isinstance(k, ast.Constant) and k.value == "resultado" and isinstance(v, ast.Constant)}
    enlaces = [None] + [getattr(cd, a) for a in dir(cd) if a.startswith("ENLACE_")]
    assert nombres >= {"lista", "ya_estaba", "no_esta", "fallo"}, nombres
    return [{"resultado": r, "causa": None, "carpeta": e} for r in sorted(nombres) for e in enlaces]


def test_para_cada_resultado_posible_el_panel_dice_lo_mismo_que_la_frase_real():
    """El panel no lleva la causa en la dirección: su frase es la del resultado SIN causa. Todo resultado con algo que
    avisar tiene clave, y la frase de esa clave es idéntica a `frase_de_la_carpeta` de ese resultado."""
    resultados = _resultados_que_preparar_puede_dar()
    assert len(resultados) >= 12
    for r in resultados:
        clave = cd.clave_de_aviso_de_la_carpeta(r)
        if clave is None:
            assert r["resultado"] in ("lista", "ya_estaba") and r["carpeta"] != cd.ENLACE_NO_SE_PUDO, r
            continue
        assert cd.frase_de_la_carpeta(cd.AVISOS_DE_LA_CARPETA[clave]) == cd.frase_de_la_carpeta(r), (r, clave)
    # y la guarda ve lo inventado: un resultado nuevo que la clave no distingue se nota
    assert cd.frase_de_la_carpeta({"resultado": "no_esta"}) != cd.frase_de_la_carpeta({"resultado": "fallo"})


@pytest.mark.parametrize("clave", sorted(cd.AVISOS_DE_LA_CARPETA))
def test_la_pagina_pinta_la_frase_de_cada_clave_de_aviso(m, nube, clave):
    from test_pagina_proyectos import ver_r
    html = _html.unescape(ver_r(m, error=clave, p=1))
    assert cd.frase_de_la_carpeta(cd.AVISOS_DE_LA_CARPETA[clave]) in html
    assert "SIN cliente" not in html       # ya no hay «crear sin cliente y avisar» (pieza 2)


def _fragmentos_de_las_frases() -> set[str]:
    """Los comienzos de TODAS las frases que `frase_de_la_carpeta` puede decir (sacados de la función, con los
    resultados de arriba): las siete primeras palabras de cada trozo."""
    trozos = set()
    for r in RESULTADOS:
        for t in re.split(r"(?<=\.)\s+", cd.frase_de_la_carpeta(r)):
            t = re.sub(r"\(.*?\)", "", t)
            trozos.add(" ".join(t.split()[:7]))
    # las palabras de la página y del botón (pieza 2), sacadas de la función que las dice
    estado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    trozos |= {" ".join(estado[k].split()[:7]) for k in ("frase", "boton")}
    return {t for t in trozos if len(t.split()) >= 3}


def _frases_a_mano(raiz, fragmentos) -> list:
    """Dónde se escribe, fuera de `cotizaciones_drive.py`, alguna de las frases de la carpeta: en toda cadena de todo
    `.py` del repo que no es de `tests/` (la lista sale de `_modulos`) y en el texto de todo `.html` de `web/plantillas/` (menos
    lo que va dentro de `{{ }}` y `{% %}`). FRONTERA: no ve una frase armada por pedazos ni una plantilla fuera de `web/plantillas/`."""
    hallados = []
    for rel, arbol in _modulos(raiz):
        if rel == "cotizaciones_drive.py":
            continue
        for n in ast.walk(arbol):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                hallados += [(rel, f) for f in fragmentos if f in " ".join(n.value.split())]
    for html in sorted((raiz / "web" / "plantillas").glob("*.html")):
        texto = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", html.read_text(encoding="utf-8"), flags=re.S)
        hallados += [(html.relative_to(raiz).as_posix(), f) for f in fragmentos if f in " ".join(texto.split())]
    return sorted(set(hallados))


def test_ninguna_otra_pieza_escribe_las_frases_de_la_carpeta_a_mano():
    fragmentos = _fragmentos_de_las_frases()
    assert len(fragmentos) >= 5, fragmentos
    assert _frases_a_mano(_ROOT, fragmentos) == []


def test_la_guarda_de_las_frases_a_mano_ve_un_archivo_inventado(tmp_path):
    """Pasa por la MISMA función que escanea el repo, con archivos que no están en él."""
    fragmentos = _fragmentos_de_las_frases()
    frase = cd.frase_de_la_carpeta({"resultado": "fallo"})
    (tmp_path / "cerebro").mkdir()
    (tmp_path / "cerebro" / "zz_frase_a_mano.py").write_text(f"MENSAJE = {frase!r}\n", encoding="utf-8")
    (tmp_path / "otro.py").write_text("X = 'nada que ver'\n", encoding="utf-8")
    (tmp_path / "web" / "plantillas").mkdir(parents=True)
    (tmp_path / "web" / "plantillas" / "nueva.html").write_text(f"<p>{frase}</p>", encoding="utf-8")
    (tmp_path / "web" / "plantillas" / "limpia.html").write_text("<p>{{ frase }}</p>", encoding="utf-8")
    (tmp_path / "cotizaciones_drive.py").write_text(f"OK = {frase!r}\n", encoding="utf-8")
    hallados = {r for r, _ in _frases_a_mano(tmp_path, fragmentos)}
    assert hallados == {"cerebro/zz_frase_a_mano.py", "web/plantillas/nueva.html"}, hallados


@pytest.mark.parametrize("falla", ["500", "sin_configurar", "mas_de_15_s", "falta_la_tabla"])
@pytest.mark.parametrize("camino", ["panel", "telegram"])
def test_lo_que_dice_cada_camino_coincide_con_lo_que_la_funcion_contesto_de_verdad(m, nube, monkeypatch, camino, falla, espia):
    FALLAS_DE_DRIVE[falla][0](monkeypatch, nube, m)
    pid, dijo = _crear(m, camino)
    esperado = cd.frase_de_la_carpeta(espia[0])
    if camino == "telegram":
        assert dijo.endswith(esperado) and "Carpeta de Drive creada" not in dijo
    else:
        assert cd.frase_de_la_carpeta(cd.AVISOS_DE_LA_CARPETA[cd.clave_de_aviso_de_la_carpeta(espia[0])]) in dijo


def test_el_aviso_de_la_carpeta_se_apaga_en_solo_ver_y_sin_recibo(m, nube):
    """Una dirección escrita a mano no cuenta lo que no pasó (la puerta de los avisos), ni en solo ver."""
    from test_cotizaciones_en_la_pagina import _ver
    for cliente in (_casa(), _ver()):
        pagina = _html.unescape(cliente.get("/proyectos?hecho=proyecto_nuevo&error=carpeta_drive_fallo&p=1").text)
        assert "No se pudo crear la carpeta" not in pagina


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A9. Una sola puerta para crear
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

# La ÚNICA mención declarada fuera de las dos casas de Drive: el guion local de una vez que autoriza la cuenta y crea la
# carpeta RAÍZ «Proyectos» (`root` como padre); no crea la carpeta de ningún proyecto.
CREA_LA_RAIZ = {("tools/autorizar_drive.py", "carpeta_proyectos")}


def _menciones_de_crear_carpeta(arbol) -> list:
    """Toda manera de nombrar `crear_carpeta`: el atributo (`drive.crear_carpeta`, `x.crear_carpeta`), un nombre suelto o
    un alias importado (`from drive import crear_carpeta as cc`, `cc(...)`), o el texto (`getattr(drive, "crear_carpeta")`)."""
    alias = {"crear_carpeta"}
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "crear_carpeta":
                    alias.add(a.asname or a.name)
    hallados = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Attribute) and n.attr in alias:
            hallados.append(n.lineno)
        elif isinstance(n, ast.Name) and n.id in alias:
            hallados.append(n.lineno)
        elif isinstance(n, ast.Constant) and n.value == "crear_carpeta":
            hallados.append(n.lineno)
        elif isinstance(n, ast.ImportFrom) and any(a.name == "crear_carpeta" for a in n.names):
            hallados.append(n.lineno)
    return sorted(set(hallados))


def test_fuera_de_las_dos_casas_de_drive_nadie_nombra_crear_carpeta():
    fuera = []
    for rel, arbol in _modulos(_ROOT):
        if rel in ("drive.py", "cotizaciones_drive.py"):
            continue
        for f in _funciones(arbol):
            if _menciones_de_crear_carpeta(ast.Module(body=[f], type_ignores=[])):
                fuera.append((rel, f.name))
        resto = ast.Module(body=[n for n in arbol.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
                           type_ignores=[])
        if _menciones_de_crear_carpeta(resto):
            fuera.append((rel, "<módulo>"))
    assert sorted(set(fuera)) == sorted(CREA_LA_RAIZ), fuera
    # la excepción sigue siendo la de la raíz y nada más: su llamada lleva `root` como padre
    arbol = next(a for rel, a in _modulos(_ROOT) if rel == "tools/autorizar_drive.py")
    llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call) and ast.unparse(n.func) == "drive.crear_carpeta"]
    assert [ast.unparse(n.args[1]) for n in llamadas] == ["'root'"]


@pytest.mark.parametrize("fuente", [
    "import drive\nasync def f():\n    await drive.crear_carpeta('x', 'y')\n",
    "from drive import crear_carpeta as cc\nasync def f():\n    await cc('x', 'y')\n",
    "import drive\nasync def f():\n    g = getattr(drive, 'crear_carpeta')\n    await g('x', 'y')\n",
    "import drive as d\nasync def f():\n    await d.crear_carpeta('x', 'y')\n",
])
def test_la_guarda_de_una_sola_puerta_ve_las_cuatro_formas_de_llamar_a_crear_carpeta(fuente):
    assert _menciones_de_crear_carpeta(ast.parse(fuente)) != []


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A11. El tope de tiempo
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_un_drive_que_no_contesta_no_cuelga_la_creacion_mas_del_tope_del_intento(m, nube, monkeypatch):
    """Cada llamada cabe en el tope de `drive.py` (0.1 s) pero las tres de crear juntas pasan del tope del intento
    (0.25 s): el proyecto se crea, la ruta contesta a tiempo con «no se pudo» y no espera a las tres."""
    monkeypatch.setattr(cd, "TIEMPO_DEL_INTENTO", 0.25)
    nube.falla = _lenta
    inicio = time.monotonic()
    r, _, pagina, pid = _por_el_panel(m)
    gastado = time.monotonic() - inicio
    assert gastado < 1.5, gastado
    assert _proyecto(m, pid)["borrado_en"] is None and _carpetas(m, pid) is None and _proyecto(m, pid)["carpeta"] is None
    assert len([p for p, _ in nube.pasos if not p.endswith("!")]) < 3, nube.pasos      # la tercera quedó cortada ("!")
    assert r.headers["location"].endswith(f"error=carpeta_drive_fallo&p={pid}") and "No se pudo crear la carpeta" in pagina


def test_con_tiempo_de_sobra_las_mismas_llamadas_lentas_si_terminan(m, nube, monkeypatch):
    monkeypatch.setattr(cd, "TIEMPO_DEL_INTENTO", 5.0)
    nube.falla = _lenta
    pid, _ = _crear(m, "panel")
    assert _carpetas(m, pid) is not None and _proyecto(m, pid)["carpeta"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A12. Sin la tabla de Drive la creación no se rompe
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_sin_la_tabla_de_drive_el_proyecto_se_crea_no_se_toca_drive_y_la_pagina_carga(m, nube):
    _sin_la_tabla(None, nube, m)
    r, c, pagina, pid = _por_el_panel(m)
    assert r.status_code == 303 and _proyecto(m, pid)["borrado_en"] is None
    assert nube.pasos == [] and nube.google.pedidos == [], "no debe crear nada en Drive si no puede anotarlo"
    assert "No se pudo crear la carpeta" in pagina
    assert c.get(f"/proyectos?p={pid}").status_code == 200
