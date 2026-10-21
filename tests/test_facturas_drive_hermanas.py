"""El Drive de las facturas (pieza 8), lado HERMANOS: la cotización y la factura comparten las carpetas de un proyecto.

El mundo es el de las cotizaciones (`tests/test_cotizaciones_en_la_pagina.py::m`, armado del esquema real) con las tablas de las
facturas y lo de Drive de la pieza 8 agregados con el SQL REAL de las migraciones; el Drive es el `Nube` de
`tests/test_cotizaciones_drive.py`; las cotizaciones se envían por SUS rutas reales (`POST /proyectos/{pid}/cotizaciones`), y las
facturas suben por `facturas_drive.subir`, la misma función que llaman las rutas. Va en un archivo aparte porque el fixture
`mundo` de este mundo (el de la página de proyectos) y el de las facturas se llaman igual.

Lo que se vigila: en los DOS órdenes (primero la cotización, primero la factura) hay UNA carpeta de proyecto con sus hermanas
`cotizaciones` y `facturas`; corregir una cotización no toca el archivo de la factura; la creación de las carpetas por una
factura es la de siempre (`preparar_la_carpeta` la da por hecha, el nombre del proyecto se pone al día, un proyecto en la
papelera no se inventa carpeta, y una subcarpeta anotada que ya no está se dice y no se rehace sola).

FRONTERA: la de `tests/test_facturas_drive.py` y la de `tests/test_cotizaciones_drive.py` (SQLite, Drive de mentira).
"""
from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import pytest

import test_facturas_tablas as ft
import test_guardar_factura as tgf
from test_cotizaciones_drive import CARPETA_PROYECTOS, _corregir, _enviar, nube  # noqa: F401
from test_cotizaciones_en_la_pagina import m  # noqa: F401  (fixture del mundo de las cotizaciones)
from test_drive import google  # noqa: F401  (fixture)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)

import cotizaciones_drive as cd
import facturas_drive as fd

CASA = tgf.CASA


@pytest.fixture
def nube_f(nube):
    nube.cosas["root"] = {"tipo": "carpeta", "nombre": "Mi unidad", "padre": None}
    return nube



def _con_facturas(m_):
    """El mundo de las cotizaciones (`m`) más las tablas de las facturas y lo de Drive de la pieza 8, con el SQL REAL de las
    migraciones. (`drive_de_proyectos` ya trae `carpeta_facturas_id`: ese mundo se arma del esquema.)"""
    for s in ft._sentencias_que_corren(ft._texto_migracion()):
        m_.con.execute(ft._a_sqlite(s))
    for s in ft._sentencias_que_corren(ft._texto_migracion_de_drive()):
        if re.match(r"ALTER\s+TABLE\s+facturas\b|CREATE\s+TABLE", s, re.I):
            m_.con.execute(ft._a_sqlite(s))
    m_.con.commit()


def _factura_emitida_en(m_, proyecto, numero=1):
    cur = m_.con.execute(
        "INSERT INTO facturas (anio, numero, estado, cliente_noco_id, para_nombre, fecha, proyecto_id, hecha_por, "
        "hecha_por_chat_id, emitida_en) VALUES (2026, ?, 'emitida', 7, 'Cliente de Prueba', '2026-10-21', ?, 'casa', ?, "
        "'2026-10-21 12:00:00')", (numero, proyecto, CASA))
    fid = cur.lastrowid
    m_.con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, monto) VALUES (?, 1, 'Ensayo', 1000)", (fid,))
    m_.con.commit()
    return fid


def test_cotizacion_primero_y_factura_despues_comparten_la_carpeta_del_proyecto(m, nube_f):
    _con_facturas(m)
    _enviar(proyecto=1)
    antes = len(nube_f.carpetas_de_proyectos())
    fid = _factura_emitida_en(m, 1)
    assert asyncio.run(fd.subir(fid)) == "subida"
    assert len(nube_f.carpetas_de_proyectos()) == antes == 1                         # la misma carpeta de proyecto
    del_proyecto = nube_f.carpetas_de_proyectos()[0]
    assert sorted(nube_f.cosas[i]["nombre"] for i in nube_f.hijos(del_proyecto, tipo="carpeta")) == ["cotizaciones", "facturas"]
    assert len(nube_f.archivos("COT-2026-001.pdf")) == 1 and len(nube_f.archivos("FAC-2026-001.pdf")) == 1
    # y la cotización sigue siendo suya: corregirla reemplaza SU archivo y no toca el de la factura
    factura_archivo = nube_f.archivos("FAC-2026-001.pdf")[0]
    datos = bytes(nube_f.cosas[factura_archivo]["datos"])
    _corregir()
    assert len(nube_f.archivos("COT-2026-001.pdf")) == 1 and nube_f.cosas[factura_archivo]["datos"] == datos
    assert not [p for p in nube_f.pasos if p[0] == "reemplazar" and p[1] == factura_archivo]


def test_factura_primero_y_cotizacion_despues_comparten_la_carpeta_del_proyecto(m, nube_f):
    _con_facturas(m)
    fid = _factura_emitida_en(m, 1)
    assert asyncio.run(fd.subir(fid)) == "subida"
    una = nube_f.carpetas_de_proyectos()
    _, html = _enviar(proyecto=1)
    assert nube_f.carpetas_de_proyectos() == una and len(una) == 1                   # la cotización NO crea otra del proyecto
    assert "Guardada en Lucy y en Drive." in html
    carpetas = tgf.filas(m, "drive_de_proyectos")[0]
    cot = nube_f.archivos("COT-2026-001.pdf")[0]
    assert nube_f.cosas[cot]["padre"] == carpetas["carpeta_cotizaciones_id"] != carpetas["carpeta_facturas_id"]
    assert nube_f.cosas[nube_f.archivos("FAC-2026-001.pdf")[0]]["padre"] == carpetas["carpeta_facturas_id"]


def test_la_carpeta_que_creo_una_factura_es_la_que_la_cotizacion_da_por_hecha(m, nube_f):
    """`preparar_la_carpeta` (la del alta de un proyecto) ve las carpetas que creó una factura y no escribe nada en Drive."""
    _con_facturas(m)
    assert asyncio.run(fd.subir(_factura_emitida_en(m, 3))) == "subida"
    pasos = list(nube_f.pasos)
    r = asyncio.run(cd.preparar_la_carpeta(3))
    assert r["resultado"] == "ya_estaba" and nube_f.pasos == pasos


def test_la_factura_que_crea_las_carpetas_de_un_proyecto_deja_el_motivo_de_una_factura_no_el_de_una_cotizacion(m, nube_f):
    _con_facturas(m)
    fid = _factura_emitida_en(m, 3)
    assert asyncio.run(fd.subir(fid)) == "subida"
    motivos = [r[0] for r in m.con.execute("SELECT motivo FROM log_acciones WHERE tabla = 'proyectos'")]
    assert all("cotización" not in x for x in motivos), motivos
    assert not motivos or motivos == [fd.MOTIVO_AL_SUBIR_UNA_FACTURA]


def test_subir_una_factura_de_un_proyecto_renombrado_le_cambia_el_nombre_a_la_misma_carpeta(m, nube_f):
    _con_facturas(m)
    fid = _factura_emitida_en(m, 1)
    assert asyncio.run(fd.subir(fid)) == "subida"
    carpeta = nube_f.carpetas_de_proyectos()[0]
    m.con.execute("UPDATE proyectos SET nombre = 'Nombre nuevo' WHERE id = 1")
    m.con.commit()
    fid2 = _factura_emitida_en(m, 1, numero=2)
    assert asyncio.run(fd.subir(fid2)) == "subida"
    assert nube_f.carpetas_de_proyectos() == [carpeta] and nube_f.cosas[carpeta]["nombre"] == "Nombre nuevo"


def test_un_proyecto_en_la_papelera_no_se_inventa_una_carpeta(m, nube_f):
    _con_facturas(m)
    fid = _factura_emitida_en(m, 1)
    m.con.execute("UPDATE proyectos SET borrado_en = '2026-10-20' WHERE id = 1")
    m.con.commit()
    assert asyncio.run(fd.subir(fid)) == "fallo"
    assert nube_f.google.pedidos == []
    assert tgf.filas(m, "facturas")[0]["drive_error"] == cd.SIN_EL_PROYECTO


def test_si_la_subcarpeta_facturas_anotada_ya_no_esta_se_dice_y_no_se_rehace_sola(m, nube_f):
    _con_facturas(m)
    assert asyncio.run(fd.subir(_factura_emitida_en(m, 1))) == "subida"
    facturas_id = tgf.filas(m, "drive_de_proyectos")[0]["carpeta_facturas_id"]
    del nube_f.cosas[facturas_id]
    pasos = len(nube_f.pasos_de("crear_carpeta"))
    fid2 = _factura_emitida_en(m, 1, numero=2)
    assert asyncio.run(fd.subir(fid2)) == "fallo"
    assert tgf.filas(m, "facturas")[1]["drive_error"] == cd.SIN_LA_CARPETA_DE_FACTURAS
    assert len(nube_f.pasos_de("crear_carpeta")) == pasos                           # no se creó otra a ciegas
