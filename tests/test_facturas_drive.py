"""El Drive de las facturas (pieza 8 del diseño «Facturas», 21-oct-2026): `facturas_drive.py`, la ruta «Subir a Drive», lo que
la página de una factura dice de Drive, `db.drive_de_factura`/`marcar_*`/`*_de_facturas_de_drive`, la migración
`2026-10-21_facturas_drive.sql` y su guion de ensayo.

CÓMO. El camino es el DE PRODUCCIÓN con dos cambios, los dos en el borde, y son los mismos que usan las pruebas de las
cotizaciones: la base es SQLite con las sentencias REALES de `db` (`tests/test_guardar_factura.py::mundo`) y Drive es el
`Nube` de `tests/test_cotizaciones_drive.py` —un transporte de mentira de `httpx` con memoria (carpetas y archivos) detrás
del cliente de verdad (`drive.py`)—. Ni `facturas_drive` ni `cotizaciones_drive` ni `drive` ni las rutas se reemplazan. Se
recorren las rutas reales (`POST /facturas/{id}/emitir|anular|drive`, `GET /facturas/{id}`) con la plantilla real. Ningún
pedido real sale: la fixture `google` de `tests/test_drive.py` revienta y anota cualquier salida a la red que no sea el
transporte de mentira.

QUÉ SE VIGILA (las garantías de la pieza 8):
  1. EMITIR SUBE el PDF a la carpeta correcta: `<Proyectos>/<proyecto>/facturas` (hermana de `cotizaciones`) con proyecto;
     la carpeta «Facturas» de la raíz sin proyecto; el PDF que sube es el de ESA factura, sin marca;
  2. ANULAR SUBE el PDF «ANULADA» con su propio nombre y NO toca el de la emitida;
  3. DRIVE CAÍDO NO FRENA emitir ni anular (la factura queda emitida o anulada con su número), la causa queda en una frase
     fija, y NINGUNA frase dice «en Drive» si no hay un archivo anotado en la carpeta de ahora;
  4. REINTENTAR sube, borra el error y no crea un segundo archivo;
  5. EL ENLACE al PDF sale solo si hay un id de archivo que sirva;
  6. PONERLE O QUITARLE EL PROYECTO a una emitida ya subida: el PDF se queda donde estaba (Lucy no mueve ni borra), la pantalla lo
     dice y ofrece subirlo a la carpeta de ahora, y no queda «En Drive» mintiendo;
  7. LA PUERTA de «¿está en Drive?» con entradas inventadas, y la lectura que tolera la migración sin aplicar;
  8. LA MIGRACIÓN, el esquema, la guarda de quién escribe y el guion de ensayo.

FRONTERA, dicha una vez:
  · NO hay Postgres: SQLite (ver `tests/test_guardar_factura.py`).
  · El Drive de mentira afirma lo que afirman `tests/test_drive.py` y `tests/test_cotizaciones_drive.py` (documentación de Drive
    v3 leída de memoria, sin una captura real): que listar filtra por padre y nombre, que crear contesta `{id}`, que el reemplazo
    conserva el id. A esto se suma UNA cosa que las cotizaciones no ejercen: que `root` sirve como padre al crear y al buscar
    (`drive.py` ya lo acepta como id; el doble lo trata como una carpeta más). Que Google se porte así NO está comprobado.
  · Las dos subidas simultáneas de la misma factura no se prueban (el doble corre en un solo hilo).

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)
from pypdf import PdfReader

import test_buzon_que_no_se_ve as buzon
import test_cotizaciones_tablas as ct
import test_facturas_tablas as ft
import test_guardar_factura as tgf
import test_grupo_ia as g
from test_cotizaciones_drive import (  # noqa: F401  (el Drive de mentira con memoria y sus fallos)
    CARPETA_PROYECTOS, FALLOS, MARCA_DE_GOOGLE, _R, nube)
from test_drive import google  # noqa: F401  (fixture)
from test_grupo_ia import _ROOT
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudas de la pantalla)
    cliente, pantalla, registro, _pagina, unica)

import cotizaciones_drive as cd
import db.db as db
import drive
import facturas_drive as fd
import registro_lectura

CASA = tgf.CASA


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# El mundo: la pantalla de las facturas, con la raíz de Mi unidad en el Drive de mentira
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def sin_registro(monkeypatch):
    """La página de una factura lee las sesiones ligadas de la App de registro por HTTP (real, contra un doble local). Eso no es lo
    que se vigila aquí y la guarda de red de la fixture `google` (que vigila TODA salida de httpx que no sea la de `drive.py`) lo
    anotaría como una salida: se contesta «no se pudo» con la forma de verdad (`registro_lectura._vacio`). Lo del registro lo
    vigilan `tests/test_pantalla_de_facturas.py` y `tests/test_facturas_sesiones_del_registro.py`."""
    async def no_contesta(noco_id, sesiones=()):
        return registro_lectura._vacio("no_se_pudo")
    monkeypatch.setattr(registro_lectura, "sesiones_de_cliente", no_contesta)


@pytest.fixture
def nube_f(nube):
    """El `Nube` de las cotizaciones más la raíz de Mi unidad (`root`), donde nace la carpeta «Facturas» sin proyecto."""
    nube.cosas["root"] = {"tipo": "carpeta", "nombre": "Mi unidad", "padre": None}
    return nube


def _emitir(f, c=None):
    """«Emitir» por la pantalla real: la confirmación trae el candado."""
    c = c or cliente()
    pagina = _pagina(f["id"], confirmar="emitir")
    cambiado = re.search(r'name="cambiado_en" value="([^"]+)"', pagina).group(1)
    r = c.post(f"/facturas/{f['id']}/emitir", data={"cambiado_en": cambiado}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r


def _anular(fid, motivo="Se hizo dos veces", c=None):
    r = (c or cliente()).post(f"/facturas/{fid}/anular", data={"motivo": motivo}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r


def _subir(fid, c=None):
    return (c or cliente()).post(f"/facturas/{fid}/drive", follow_redirects=False)


def _fila(m_, fid=1):
    return tgf.filas(m_, "facturas")[fid - 1]


def _texto_del_pdf(datos: bytes) -> str:
    return " ".join(p.extract_text() for p in PdfReader(io.BytesIO(datos)).pages)


def _emitida(proyecto=None):
    """Un borrador (con o sin proyecto) listo para emitir por la pantalla."""
    return tgf.otra(proyecto_id=proyecto)


def _bloque_de_drive(html: str) -> str:
    m_ = re.search(r'<div class="tarjeta" data-drive=.*?</div>', html, re.S)
    return m_.group(0) if m_ else ""


def _afirma_estar_en_drive(html: str) -> bool:
    """La página afirma que el PDF está en Drive: la etiqueta «En Drive» de un estado `en_drive` o `otra_carpeta`, o la frase
    «… está en Drive». Lo demás con esas palabras («No está en Drive», «NO llegó a Drive», los botones) dice otra cosa."""
    bloque = _bloque_de_drive(html)
    return 'data-drive="en_drive"' in bloque or 'data-drive="otra_carpeta"' in bloque


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Emitir sube el PDF a la carpeta correcta
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_emitir_sin_proyecto_sube_a_la_carpeta_facturas_de_la_raiz_y_la_marca_en_drive(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    fila = _fila(pantalla)
    suelta = nube_f.hijos("root", "Facturas", "carpeta")
    assert len(suelta) == 1
    archivos = nube_f.archivos("FAC-2026-001.pdf")
    assert len(archivos) == 1 and nube_f.cosas[archivos[0]]["padre"] == suelta[0]
    # la carpeta quedó anotada por id, y no se creó nada de proyectos
    assert [r["carpeta_id"] for r in tgf.filas(pantalla, "drive_carpetas_sueltas")] == suelta
    assert nube_f.carpetas_de_proyectos() == [] and tgf.filas(pantalla, "drive_de_proyectos") == []
    # lo que Drive contestó, anotado: sin proyecto
    assert (fila["drive_archivo_id"], fila["drive_proyecto_id"], fila["drive_error"], fila["drive_anulada_archivo_id"]) == (
        archivos[0], None, None, None)
    # el PDF que subió es el de ESA factura, sin marca
    texto = _texto_del_pdf(nube_f.cosas[archivos[0]]["datos"])
    assert "FAC-2026-001" in texto and "BORRADOR" not in texto and "ANULADA" not in texto
    html = _pagina(f["id"])
    assert 'data-drive="en_drive"' in html and "El PDF de la factura está en Drive." in html
    assert not nube_f.google.pedidos == [] and all(p.url.host in ("www.googleapis.com", "oauth2.googleapis.com")
                                                   for p in nube_f.google.pedidos)


def test_emitir_con_proyecto_sube_a_su_subcarpeta_facturas_hermana_de_cotizaciones(pantalla, nube_f):
    f = _emitida(proyecto=1)
    _emitir(f)
    fila, carpetas = _fila(pantalla), tgf.filas(pantalla, "drive_de_proyectos")[0]
    del_proyecto = nube_f.hijos(CARPETA_PROYECTOS, "Proyecto 1", "carpeta")
    assert len(del_proyecto) == 1 and carpetas["carpeta_proyecto_id"] == del_proyecto[0]
    de_facturas = nube_f.hijos(del_proyecto[0], "facturas", "carpeta")
    de_cotizaciones = nube_f.hijos(del_proyecto[0], "cotizaciones", "carpeta")
    assert len(de_facturas) == 1 and len(de_cotizaciones) == 1                      # hermanas: el mismo padre
    assert carpetas["carpeta_facturas_id"] == de_facturas[0] and carpetas["carpeta_cotizaciones_id"] == de_cotizaciones[0]
    archivos = nube_f.archivos("FAC-2026-001.pdf")
    assert len(archivos) == 1 and nube_f.cosas[archivos[0]]["padre"] == de_facturas[0]
    assert (fila["drive_archivo_id"], fila["drive_proyecto_id"], fila["drive_error"]) == (archivos[0], 1, None)
    assert nube_f.hijos("root", "Facturas") == [] and tgf.filas(pantalla, "drive_carpetas_sueltas") == []


def test_la_segunda_factura_del_proyecto_usa_la_misma_carpeta_y_otro_proyecto_tiene_la_suya(pantalla, nube_f):
    for pid in (1, 1, 2):
        _emitir(_emitida(proyecto=pid))
    carpetas = {r["proyecto_id"]: r["carpeta_facturas_id"] for r in tgf.filas(pantalla, "drive_de_proyectos")}
    assert set(carpetas) == {1, 2} and carpetas[1] != carpetas[2]
    assert len(nube_f.hijos(CARPETA_PROYECTOS, tipo="carpeta")) == 2                 # dos carpetas de proyecto, no cuatro
    for pid, carpeta in carpetas.items():
        assert [nube_f.cosas[a]["nombre"] for a in nube_f.hijos(carpeta, tipo="pdf")] == (
            ["FAC-2026-001.pdf", "FAC-2026-002.pdf"] if pid == 1 else ["FAC-2026-003.pdf"])
    assert nube_f.pasos_de("crear_carpeta").__len__() == 2 * 3                       # (proyecto + cotizaciones + facturas) x 2


def test_dos_facturas_sin_proyecto_usan_la_misma_carpeta_facturas(pantalla, nube_f):
    _emitir(_emitida())
    _emitir(_emitida())
    assert len(nube_f.hijos("root", "Facturas", "carpeta")) == 1
    assert sorted(nube_f.cosas[a]["nombre"] for a in nube_f.archivos()) == ["FAC-2026-001.pdf", "FAC-2026-002.pdf"]
    assert len(tgf.filas(pantalla, "drive_carpetas_sueltas")) == 1


def test_una_carpeta_facturas_que_un_intento_anterior_creo_y_no_anoto_se_reusa(pantalla, nube_f):
    """El intento anterior creó «Facturas» en la raíz y se cayó antes de anotarla: el siguiente la encuentra por nombre."""
    ya = nube_f._nuevo("CARPETA")
    nube_f.cosas[ya] = {"tipo": "carpeta", "nombre": "Facturas", "padre": "root"}
    _emitir(_emitida())
    assert nube_f.hijos("root", "Facturas", "carpeta") == [ya]
    assert [r["carpeta_id"] for r in tgf.filas(pantalla, "drive_carpetas_sueltas")] == [ya]
    assert nube_f.pasos_de("crear_carpeta") == []


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Anular sube el PDF «ANULADA» y no toca el de la emitida
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("proyecto", [None, 1])
def test_anular_sube_el_pdf_anulada_con_su_nombre_y_deja_el_de_la_emitida_como_estaba(pantalla, nube_f, proyecto):
    f = _emitida(proyecto=proyecto)
    _emitir(f)
    emitida_id = nube_f.archivos("FAC-2026-001.pdf")[0]
    antes = bytes(nube_f.cosas[emitida_id]["datos"])
    pasos_antes = len(nube_f.pasos)
    _anular(f["id"], "Se facturó dos veces")
    fila = _fila(pantalla)
    carpeta = nube_f.cosas[emitida_id]["padre"]
    anuladas = nube_f.archivos("FAC-2026-001-ANULADA.pdf")
    assert len(anuladas) == 1 and nube_f.cosas[anuladas[0]]["padre"] == carpeta      # en la MISMA carpeta
    # la de la emitida: mismo id, mismos bytes, sin un solo pedido que la toque
    assert nube_f.archivos("FAC-2026-001.pdf") == [emitida_id] and nube_f.cosas[emitida_id]["datos"] == antes
    assert not [p for p in nube_f.pasos[pasos_antes:] if p[0] in ("reemplazar", "renombrar") or p[1] == emitida_id]
    texto = _texto_del_pdf(nube_f.cosas[anuladas[0]]["datos"])
    assert "FAC-2026-001" in texto and "ANULADA" in texto and "Se facturó dos veces" in texto
    assert (fila["estado"], fila["drive_archivo_id"], fila["drive_anulada_archivo_id"], fila["drive_error"]) == (
        "anulada", emitida_id, anuladas[0], None)
    html = _pagina(f["id"])
    assert "El PDF «ANULADA» está en Drive." in html and 'data-drive="en_drive"' in html
    assert f"/file/d/{emitida_id}/view" in html and f"/file/d/{anuladas[0]}/view" in html


def test_anular_una_cuya_emitida_nunca_subio_sube_solo_la_anulada(pantalla, nube_f):
    """Drive estaba caído al emitir: al anular se sube lo que corresponde al estado de ahora, nada más."""
    f = _emitida()
    nube_f.falla = lambda paso, p: _R(500, json={})
    _emitir(f)
    assert nube_f.archivos() == [] and _fila(pantalla)["drive_archivo_id"] is None
    nube_f.falla = None
    _anular(f["id"])
    fila = _fila(pantalla)
    assert [nube_f.cosas[a]["nombre"] for a in nube_f.archivos()] == ["FAC-2026-001-ANULADA.pdf"]
    assert fila["drive_archivo_id"] is None and fila["drive_anulada_archivo_id"] == nube_f.archivos()[0]
    html = _pagina(f["id"])
    assert 'data-drive="en_drive"' in html and "Abrir el PDF de la emitida" not in html


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3. Drive caído no frena emitir ni anular, y nada dice «en Drive» si no subió
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fallo", sorted(FALLOS))
def test_con_drive_caido_la_factura_queda_emitida_con_su_numero_y_la_pantalla_dice_que_no_se_subio(pantalla, nube_f, fallo):
    respuesta, causa = FALLOS[fallo]
    f = _emitida()
    nube_f.falla = respuesta
    r = _emitir(f)
    assert r.headers["location"] == f"/facturas/{f['id']}"
    fila = _fila(pantalla)
    assert (fila["estado"], fila["numero"]) == ("emitida", 1)                        # emitir NO se frenó
    assert (fila["drive_archivo_id"], fila["drive_anulada_archivo_id"], fila["drive_error"]) == (None, None, causa)
    assert nube_f.archivos() == []
    html = _pagina(f["id"])
    assert 'data-drive="no_esta"' in html and f"NO llegó a Drive ({causa})" in html and "Volver a subir a Drive" in html
    assert not _afirma_estar_en_drive(html) and "drive.google.com" not in html
    assert MARCA_DE_GOOGLE not in html                                              # nada de lo que dice Google llega a la pantalla
    assert "FAC-2026-001" in html                                                   # y la factura se ve emitida


def test_con_drive_caido_anular_tampoco_se_frena(pantalla, nube_f):
    f = _emitida(proyecto=1)
    _emitir(f)
    nube_f.falla = lambda paso, p: _R(500, json={"error": {"message": MARCA_DE_GOOGLE}})
    _anular(f["id"])
    fila = _fila(pantalla)
    assert (fila["estado"], fila["drive_anulada_archivo_id"], fila["drive_error"]) == ("anulada", None, drive.DriveNoContesta.causa)
    html = _pagina(f["id"])
    # la emitida SÍ está en Drive; la anulada NO: la pantalla habla de la anulada, y de la emitida solo da su enlace
    assert 'data-drive="no_esta"' in html and "El PDF «ANULADA» NO llegó a Drive" in html and "Abrir el PDF de la emitida" in html
    assert MARCA_DE_GOOGLE not in html


def test_drive_sin_configurar_tambien_es_una_causa_y_no_sale_ningun_pedido(pantalla, nube_f, monkeypatch):
    monkeypatch.delenv("LUCY_DRIVE_REFRESH_TOKEN")
    f = _emitida()
    _emitir(f)
    assert nube_f.google.pedidos == []
    assert _fila(pantalla)["drive_error"] == drive.DriveSinConfigurar.causa and _fila(pantalla)["estado"] == "emitida"


def test_si_la_base_no_deja_anotar_el_error_la_ruta_igual_contesta(pantalla, nube_f, monkeypatch):
    async def revienta(*a, **k):
        raise RuntimeError("la base no contesta")
    monkeypatch.setattr(db, "marcar_factura_en_drive", revienta)
    monkeypatch.setattr(db, "marcar_error_de_drive_de_factura", revienta)
    f = _emitida()
    r = _emitir(f)
    assert r.status_code == 303 and _fila(pantalla)["estado"] == "emitida"
    assert _fila(pantalla)["drive_archivo_id"] is None
    assert not _afirma_estar_en_drive(_pagina(f["id"]))


def test_la_causa_es_un_vocabulario_cerrado_una_causa_ajena_no_se_enseña(pantalla, nube_f):
    f = _emitida()
    nube_f.falla = lambda paso, p: _R(500, json={})
    _emitir(f)
    pantalla.con.execute("UPDATE facturas SET drive_error = ?", (MARCA_DE_GOOGLE,))
    pantalla.con.commit()
    html = _pagina(f["id"])
    assert MARCA_DE_GOOGLE not in html and "todavía no se ha subido a Drive" in html


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 4. Reintentar
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_reintentar_sube_borra_el_error_y_la_pantalla_pasa_a_en_drive(pantalla, nube_f):
    f = _emitida(proyecto=2)
    nube_f.falla = lambda paso, p: _R(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    _emitir(f)
    assert _fila(pantalla)["drive_error"] == drive.DriveSinPermiso.causa and nube_f.archivos() == []
    nube_f.falla = None
    r = _subir(f["id"])
    assert r.status_code == 303 and r.headers["location"] == f"/facturas/{f['id']}"
    fila = _fila(pantalla)
    assert (fila["drive_archivo_id"], fila["drive_proyecto_id"], fila["drive_error"]) == (nube_f.archivos("FAC-2026-001.pdf")[0], 2, None)
    html = _pagina(f["id"])
    assert 'data-drive="en_drive"' in html and "NO llegó" not in html and "data-drive-boton" not in html


def test_reintentar_dos_veces_no_crea_un_segundo_archivo(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    primero = nube_f.archivos("FAC-2026-001.pdf")
    assert len(primero) == 1
    _subir(f["id"])                                                                 # ya estaba: se reemplaza el mismo
    _subir(f["id"])
    assert nube_f.archivos("FAC-2026-001.pdf") == primero and len(nube_f.hijos("root", "Facturas")) == 1
    assert _fila(pantalla)["drive_archivo_id"] == primero[0]


def test_un_intento_que_subio_y_se_cayo_antes_de_anotar_se_reemplaza_y_no_se_duplica(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    antes = nube_f.archivos("FAC-2026-001.pdf")
    pantalla.con.execute("UPDATE facturas SET drive_archivo_id = NULL, drive_proyecto_id = NULL")    # la marca no quedó
    pantalla.con.commit()
    _subir(f["id"])
    assert nube_f.archivos("FAC-2026-001.pdf") == antes and _fila(pantalla)["drive_archivo_id"] == antes[0]
    assert nube_f.pasos_de("reemplazar") == antes


def test_un_reintento_que_vuelve_a_fallar_sigue_diciendo_que_no_llego(pantalla, nube_f):
    f = _emitida()
    nube_f.falla = lambda paso, p: _R(500, json={})
    _emitir(f)
    _subir(f["id"])
    fila = _fila(pantalla)
    assert fila["drive_archivo_id"] is None and fila["drive_error"] == drive.DriveNoContesta.causa
    assert 'data-drive="no_esta"' in _pagina(f["id"])


def test_un_borrador_no_se_sube_ni_la_ruta_toca_a_drive(pantalla, nube_f):
    b = tgf.crear()
    r = _subir(b["id"])
    assert r.status_code == 303 and nube_f.google.pedidos == []
    assert _fila(pantalla)["drive_error"] is None
    assert asyncio.run(fd.subir(b["id"])) == "borrador" and asyncio.run(fd.subir(999)) == "no_esta"
    assert "data-drive=" not in _pagina(b["id"])


def test_subir_una_factura_que_no_existe_es_404_y_a_drive_no_le_llega_nada(pantalla, nube_f):
    assert _subir(99).status_code == 404 and nube_f.google.pedidos == []


@pytest.mark.parametrize("sesion", [None, "ver", "ajena", "falsa"])
def test_solo_la_casa_sube_y_a_drive_no_le_llega_nada(pantalla, nube_f, sesion):
    f = tgf.emitir(tgf.crear())
    antes = tgf.foto(pantalla)
    r = _subir(f["id"], cliente(sesion))
    assert r.status_code == 401 and nube_f.google.pedidos == [] and tgf.foto(pantalla) == antes


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 5. El enlace sale solo si hay un archivo
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_la_pagina_enlaza_al_pdf_solo_cuando_existe(pantalla, nube_f):
    f = _emitida()
    nube_f.falla = lambda paso, p: _R(500, json={})
    _emitir(f)
    sin = _pagina(f["id"])
    assert "data-drive-enlace" not in sin and "drive.google.com" not in sin
    nube_f.falla = None
    _subir(f["id"])
    archivo = _fila(pantalla)["drive_archivo_id"]
    con = _pagina(f["id"])
    assert con.count("data-drive-enlace") == 1
    assert f'href="https://drive.google.com/file/d/{archivo}/view"' in con


@pytest.mark.parametrize("ident", ["", "../../etc", "a b", "x" * 200, "ID\"><script>", "ID?x=1"])
def test_un_id_que_no_puede_ser_de_drive_no_se_vuelve_enlace(pantalla, nube_f, ident):
    f = tgf.emitir(tgf.crear())
    pantalla.con.execute("UPDATE facturas SET drive_archivo_id = ?, drive_proyecto_id = NULL", (ident,))
    pantalla.con.commit()
    html = _pagina(f["id"])
    assert "data-drive-enlace" not in html and "drive.google.com" not in html and "<script>" not in html
    assert 'data-drive="en_drive"' not in html                                       # tampoco dice que está


def test_un_borrador_no_tiene_bloque_de_drive_ni_enlace(pantalla, nube_f):
    html = _pagina(tgf.crear()["id"])
    assert "data-drive=" not in html and "drive.google.com" not in html


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 6. Ponerle o quitarle el proyecto a una emitida que ya está en Drive
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_ponerle_proyecto_a_una_emitida_ya_subida_no_mueve_el_pdf_y_la_pantalla_no_miente(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    archivo = _fila(pantalla)["drive_archivo_id"]
    cliente().post(f"/facturas/{f['id']}/proyecto", data={"proyecto": "1"}, follow_redirects=False)
    assert _fila(pantalla)["proyecto_id"] == 1
    # Lucy no tocó nada en Drive y no cambió lo anotado
    assert nube_f.archivos("FAC-2026-001.pdf") == [archivo] and nube_f.cosas[archivo]["padre"] == nube_f.hijos("root", "Facturas")[0]
    assert (_fila(pantalla)["drive_archivo_id"], _fila(pantalla)["drive_proyecto_id"]) == (archivo, None)
    html = _pagina(f["id"])
    assert 'data-drive="otra_carpeta"' in html and "Lucy no mueve ni borra nada en Drive" in html
    assert 'data-drive="en_drive"' not in html and "Subir a la carpeta de ahora" in html
    assert f"/file/d/{archivo}/view" in html                                         # el PDF sigue existiendo y se puede abrir


def test_subir_a_la_carpeta_de_ahora_crea_el_archivo_alli_y_deja_el_de_antes_donde_estaba(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    viejo = _fila(pantalla)["drive_archivo_id"]
    cliente().post(f"/facturas/{f['id']}/proyecto", data={"proyecto": "1"}, follow_redirects=False)
    _subir(f["id"])
    fila = _fila(pantalla)
    nuevo = fila["drive_archivo_id"]
    assert nuevo != viejo and (fila["drive_proyecto_id"], fila["drive_error"]) == (1, None)
    de_facturas = tgf.filas(pantalla, "drive_de_proyectos")[0]["carpeta_facturas_id"]
    assert nube_f.cosas[nuevo]["padre"] == de_facturas
    assert viejo in nube_f.archivos() and nube_f.cosas[viejo]["padre"] == nube_f.hijos("root", "Facturas")[0]   # no se borró ni se movió
    assert 'data-drive="en_drive"' in _pagina(f["id"])


def test_quitarle_el_proyecto_tambien_dice_que_el_pdf_quedo_en_la_carpeta_de_antes(pantalla, nube_f):
    f = _emitida(proyecto=1)
    _emitir(f)
    cliente().post(f"/facturas/{f['id']}/proyecto", data={"proyecto": ""}, follow_redirects=False)
    assert _fila(pantalla)["proyecto_id"] is None
    assert 'data-drive="otra_carpeta"' in _pagina(f["id"])
    _subir(f["id"])
    fila = _fila(pantalla)
    assert fila["drive_proyecto_id"] is None
    assert nube_f.cosas[fila["drive_archivo_id"]]["padre"] == nube_f.hijos("root", "Facturas")[0]
    assert len(nube_f.archivos("FAC-2026-001.pdf")) == 2                             # el de antes se queda en el proyecto


def test_cambiar_el_proyecto_de_una_que_no_estaba_en_drive_no_inventa_nada(pantalla, nube_f):
    f = _emitida()
    nube_f.falla = lambda paso, p: _R(500, json={})
    _emitir(f)
    cliente().post(f"/facturas/{f['id']}/proyecto", data={"proyecto": "2"}, follow_redirects=False)
    html = _pagina(f["id"])
    assert 'data-drive="no_esta"' in html and not _afirma_estar_en_drive(html)


def test_si_le_cambian_el_proyecto_mientras_se_sube_no_se_marca_nada(pantalla, nube_f):
    f = _emitida()
    nube_f.al_subir = lambda: (pantalla.con.execute("UPDATE facturas SET proyecto_id = 2"), pantalla.con.commit())
    _emitir(f)
    fila = _fila(pantalla)
    assert fila["proyecto_id"] == 2 and fila["drive_archivo_id"] is None and fila["drive_proyecto_id"] is None
    html = _pagina(f["id"])
    assert not _afirma_estar_en_drive(html)
    assert asyncio.run(db.marcar_factura_en_drive(f["id"], "emitida", "ARCH", None)) is False


def test_los_dos_pdf_de_una_factura_comparten_sitio_y_al_cambiar_de_sitio_el_otro_deja_de_contarse(pantalla, nube_f):
    f = _emitida()
    _emitir(f)
    _anular(f["id"])
    antes = _fila(pantalla)
    assert antes["drive_archivo_id"] and antes["drive_anulada_archivo_id"]
    # un instante después, su sitio es otro (la base ya no deja cambiar el proyecto de una anulada: se prueba la puerta de la marca)
    pantalla.con.execute("UPDATE facturas SET proyecto_id = 1")
    pantalla.con.commit()
    assert asyncio.run(db.marcar_factura_en_drive(f["id"], "anulada", "NUEVO", 1)) is True
    fila = _fila(pantalla)
    assert (fila["drive_anulada_archivo_id"], fila["drive_proyecto_id"], fila["drive_archivo_id"]) == ("NUEVO", 1, None)


def test_marcar_respeta_el_estado_la_emitida_no_se_marca_como_anulada_ni_el_borrador(pantalla):
    e = tgf.emitir(tgf.crear())
    b = tgf.crear(liga={"tipo": "sesion", "ref": "otra"})
    assert asyncio.run(db.marcar_factura_en_drive(e["id"], "anulada", "A1", None)) is False
    assert asyncio.run(db.marcar_factura_en_drive(b["id"], "emitida", "A2", None)) is False
    assert asyncio.run(db.marcar_error_de_drive_de_factura(b["id"], "x")) is False
    assert asyncio.run(db.marcar_factura_en_drive(e["id"], "emitida", "A3", None)) is True
    with pytest.raises(ValueError):
        asyncio.run(db.marcar_factura_en_drive(e["id"], "otra", "A4", None))
    fila = tgf.filas(pantalla, "facturas")
    assert [(x["drive_archivo_id"], x["drive_anulada_archivo_id"]) for x in fila] == [("A3", None), (None, None)]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 7. La puerta de «¿está en Drive?», con entradas inventadas
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _d(**kw):
    base = {"estado": "emitida", "proyecto_id": None, "drive_archivo_id": None, "drive_anulada_archivo_id": None,
            "drive_proyecto_id": None, "drive_error": None}
    base.update(kw)
    return base


def test_la_puerta_solo_dice_en_drive_con_el_archivo_que_le_toca_y_en_la_carpeta_de_ahora():
    en = fd.estado_de_drive
    assert en(_d(drive_archivo_id="A1"))["estado"] == "en_drive"
    assert en(_d(estado="anulada", drive_anulada_archivo_id="A2"))["estado"] == "en_drive"
    # la anulada con SOLO el archivo de la emitida no está en Drive (le falta el suyo)
    assert en(_d(estado="anulada", drive_archivo_id="A1"))["estado"] == "no_esta"
    # la emitida con SOLO el de la anulada tampoco
    assert en(_d(drive_anulada_archivo_id="A2"))["estado"] == "no_esta"
    # otra carpeta: el proyecto de ahora no es el de cuando se subió (en los dos sentidos)
    assert en(_d(drive_archivo_id="A1", proyecto_id=3))["estado"] == "otra_carpeta"
    assert en(_d(drive_archivo_id="A1", drive_proyecto_id=3))["estado"] == "otra_carpeta"
    assert en(_d(drive_archivo_id="A1", drive_proyecto_id=3, proyecto_id=3))["estado"] == "en_drive"
    for sin_id in (None, "", "../x", "a b", 7, "x" * 129):
        assert en(_d(drive_archivo_id=sin_id))["estado"] == "no_esta", sin_id


def test_la_puerta_no_afirma_nada_de_un_borrador_ni_de_lo_que_no_pudo_leer():
    assert fd.estado_de_drive(None) is None and fd.estado_de_drive(_d(estado="borrador", drive_archivo_id="A1")) is None
    assert fd.estado_de_drive({}) is None and fd.estado_de_drive("emitida") is None


def test_las_frases_de_la_puerta_afirman_solo_lo_que_es_verdad():
    todas = []
    for estado in ("emitida", "anulada"):
        for archivo in (None, "A1"):
            for anulada in (None, "A2"):
                for sitio in (None, 3):
                    for ahora in (None, 3):
                        for error in (None, drive.DriveNoContesta.causa, MARCA_DE_GOOGLE):
                            r = fd.estado_de_drive(_d(estado=estado, drive_archivo_id=archivo, drive_anulada_archivo_id=anulada,
                                                      drive_proyecto_id=sitio, proyecto_id=ahora, drive_error=error))
                            todas.append(r)
                            esta = r["estado"] != "no_esta"
                            assert ("está en Drive" in r["frase"]) == esta, r
                            assert (r["boton"] is None) == (r["estado"] == "en_drive"), r
                            assert MARCA_DE_GOOGLE not in r["frase"] and r["causa"] != MARCA_DE_GOOGLE
                            assert all(e["url"].startswith("https://drive.google.com/file/d/") for e in r["enlaces"])
                            assert (r["enlaces"] != []) == bool(archivo or anulada), r
    assert {r["estado"] for r in todas} == {"en_drive", "otra_carpeta", "no_esta"}


def test_las_causas_que_se_guardan_son_las_de_drive_py_y_las_de_aqui_y_ninguna_menciona_cotizaciones():
    assert set(drive.DriveError.__subclasses__()) >= {drive.DriveNoContesta, drive.DriveSinPermiso}
    for c in (drive.DriveNoContesta.causa, drive.DriveSinPermiso.causa, drive.DriveSinConfigurar.causa,
              drive.DriveSinCarpetaProyectos.causa, drive.DriveError.causa, cd.SIN_PDF, cd.SIN_LA_CARPETA_DEL_PROYECTO,
              cd.SIN_LA_CARPETA_DE_FACTURAS, cd.SIN_EL_PROYECTO, fd.SIN_EL_ARCHIVO_DE_LA_FACTURA):
        assert c in fd.CAUSAS_DE_FACTURA, c
    assert not [c for c in fd.CAUSAS_DE_FACTURA if "cotizaci" in c.lower()]
    assert len(set(fd.CAUSAS_DE_FACTURA)) == len(fd.CAUSAS_DE_FACTURA)


def test_si_falta_la_migracion_de_drive_la_pagina_carga_sin_el_bloque_y_sin_afirmar_nada(pantalla, nube_f, monkeypatch):
    """La lectura del estado de Drive tolera que falte una columna (SQLSTATE 42703): `db.drive_de_factura` devuelve `None`."""
    f = tgf.emitir(tgf.crear())

    class _FaltaLaColumna(Exception):
        sqlstate = "42703"
    original = tgf._Cur.execute

    async def execute(self, sql, params=None):
        if sql == db.SENTENCIA_DE_LEER_EL_DRIVE_DE_FACTURA:
            raise _FaltaLaColumna("column drive_error does not exist")
        return await original(self, sql, params)
    monkeypatch.setattr(tgf._Cur, "execute", execute)
    assert asyncio.run(db.drive_de_factura(f["id"])) is None
    r = cliente().get(f"/facturas/{f['id']}")
    assert r.status_code == 200 and "FAC-2026-001" in r.text and "data-drive=" not in r.text


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 8. La migración, el esquema, quién escribe y el guion de ensayo
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

COLUMNAS_DE_LAS_FACTURAS = {"drive_archivo_id": "text", "drive_anulada_archivo_id": "text", "drive_proyecto_id": "bigint",
                            "drive_error": "text"}


def test_la_migracion_agrega_las_columnas_y_la_tabla_y_el_esquema_dice_lo_mismo():
    import test_base_m2 as b2
    texto = ft._texto_migracion_de_drive()
    sin_comentarios = db._sin_comentarios(texto)
    de_facturas = {c.lower(): t.lower() for c, t in re.findall(
        r"ALTER\s+TABLE\s+facturas\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)", sin_comentarios, re.I)}
    assert de_facturas == COLUMNAS_DE_LAS_FACTURAS
    assert re.findall(r"ALTER\s+TABLE\s+drive_de_proyectos\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)",
                      sin_comentarios, re.I) == [("carpeta_facturas_id", "TEXT")]
    # Las de Drive, más las que agreguen otras piezas (D3: el descuento). Que cada columna de `facturas` esté decidida
    # lo vigila el cubo estricto de `test_guardar_factura.py`.
    assert set(COLUMNAS_DE_LAS_FACTURAS) <= ct._columnas_que_agregan_otras_migraciones_a("facturas", ft._migracion())
    esquema = ft._ESQUEMA
    for c in COLUMNAS_DE_LAS_FACTURAS:
        assert re.search(rf"^\s+{c}\s", b2._ddl(esquema, "facturas")[0], re.M), c
    assert re.search(r"^\s+carpeta_facturas_id\s", b2._ddl(esquema, "drive_de_proyectos")[0], re.M)
    assert [b2._normal(s).replace("if not exists ", "") for s in b2._ddl(texto, "drive_carpetas_sueltas")] == \
        [b2._normal(s) for s in b2._ddl(esquema, "drive_carpetas_sueltas")] != []
    # idempotente, y no escribe ni una fila ni borra nada
    for s in g._sentencias(texto):
        if re.match(r"ALTER\s+TABLE", s, re.I):
            assert re.search(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", s, re.I), s
        elif re.match(r"CREATE\s+TABLE", s, re.I):
            assert re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", s, re.I), s
        else:
            assert re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I), s


def test_la_migracion_corre_sobre_las_anteriores_y_deja_lo_de_hoy_sin_drive():
    con = ft._base()
    ft._poner(con, "facturas", **ft._emitida(1))
    antes = con.execute("SELECT count(*) FROM facturas").fetchone()[0]
    con.execute("CREATE TABLE drive_de_proyectos (proyecto_id INTEGER PRIMARY KEY, carpeta_proyecto_id TEXT NOT NULL, "
                "carpeta_cotizaciones_id TEXT NOT NULL, nombre_puesto TEXT NOT NULL, creado_en TEXT)")
    for s in ft._sentencias_que_corren(ft._texto_migracion_de_drive()):
        con.execute(ft._a_sqlite(s))
    assert con.execute("SELECT count(*) FROM facturas").fetchone()[0] == antes == 1
    assert con.execute("SELECT count(*) FROM drive_carpetas_sueltas").fetchone()[0] == 0
    assert con.execute("SELECT drive_archivo_id, drive_anulada_archivo_id, drive_proyecto_id, drive_error FROM facturas"
                       ).fetchone() == (None, None, None, None)
    for tabla, columnas in (("facturas", set(COLUMNAS_DE_LAS_FACTURAS)), ("drive_de_proyectos", {"carpeta_facturas_id"})):
        info = {c[1]: c for c in con.execute(f"PRAGMA table_info({tabla})")}
        for c in columnas:
            assert info[c][3] == 0 and info[c][4] is None, c                          # acepta NULL y sin valor por omisión


def test_solo_db_py_escribe_lo_de_drive_de_las_facturas_y_solo_facturas_drive_lo_llama():
    escritores, llamadores = [], {}
    nombres = ("marcar_factura_en_drive", "marcar_error_de_drive_de_factura", "guardar_carpeta_de_facturas_de_drive",
               "guardar_carpeta_suelta_de_drive")
    for ruta in buzon._py_en_disco(_ROOT):                                           # la única puerta para recorrer el repositorio
        rel = ruta.relative_to(_ROOT).as_posix()
        if rel.startswith("tests/") or ruta.name == "conftest.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for n in ast.walk(arbol):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(
                    r"\b(UPDATE|INSERT\s+INTO)\s+(facturas|drive_carpetas_sueltas)\b[^;]*\bdrive_|"
                    r"\bUPDATE\s+drive_de_proyectos\s+SET\s+carpeta_facturas_id|INSERT\s+INTO\s+drive_carpetas_sueltas",
                    n.value, re.I | re.S):
                escritores.append(rel)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in nombres:
                llamadores.setdefault(n.func.attr, set()).add(rel)
    # cinco sentencias: marcar la emitida, marcar la anulada, anotar el error, anotar la `facturas` del proyecto y anotar la suelta
    assert set(escritores) == {"db/db.py"} and len(escritores) == 5, escritores
    assert llamadores == {"marcar_factura_en_drive": {"facturas_drive.py"},
                          "marcar_error_de_drive_de_factura": {"facturas_drive.py"},
                          "guardar_carpeta_de_facturas_de_drive": {"cotizaciones_drive.py"},
                          "guardar_carpeta_suelta_de_drive": {"cotizaciones_drive.py"}}, llamadores


def test_facturas_drive_no_abre_conexiones_ni_escribe_sql_ni_crea_carpetas_por_su_cuenta():
    fuente = (_ROOT / "facturas_drive.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    importados = {a.name for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    importados |= {n.module for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}
    assert not importados & {"httpx", "requests", "urllib", "psycopg", "sqlite3", "aiohttp"}, importados
    docstrings = {id(n.body[0].value) for n in ast.walk(arbol)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    sql = [n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and id(n) not in docstrings and re.search(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", n.value)]
    assert sql == []
    assert "crear_carpeta" not in re.sub(r'""".*?"""', "", fuente, flags=re.S)       # (lo guarda también test_carpeta_nace_con_el_proyecto)


def test_el_guion_de_ensayo_de_drive_saca_el_commit_cierra_con_rollback_y_solo_lee_con_cuenta(capsys):
    spec = importlib.util.spec_from_file_location("ensayo_facturas_drive", _ROOT / "tools" / "ensayo_facturas_drive.py")
    guion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guion)
    texto = ft._texto_migracion_de_drive()
    ensayo = guion.sql_del_ensayo(texto)
    sin_comentarios = re.sub(r"--[^\n]*", "", ensayo)
    # dos ROLLBACK de verdad: el de la cuenta de filas de antes y el que cierra el ensayo; ningún COMMIT
    assert "COMMIT" not in sin_comentarios and sin_comentarios.count("ROLLBACK;") == 2 and "BEGIN;" in ensayo
    assert "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS drive_archivo_id TEXT;" in ensayo
    for c in list(COLUMNAS_DE_LAS_FACTURAS) + ["carpeta_facturas_id"]:
        assert f"'{c}'" in ensayo, c
    assert sin_comentarios.rindex("ROLLBACK;") < sin_comentarios.index("tras el ROLLBACK")
    assert sin_comentarios.index("ALTER TABLE facturas") > sin_comentarios.index("ROLLBACK;")
    cuenta = guion.sql_de_la_cuenta(texto)
    assert cuenta.startswith("BEGIN READ ONLY;") and cuenta.rstrip().endswith("ROLLBACK;")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", cuenta, re.I)
    assert guion.main(["--cuenta"]) == 0 and capsys.readouterr().out == cuenta
    assert guion.main(["--ensayo"]) == 0 and capsys.readouterr().out == ensayo
    assert guion.main(["--aplicar"]) == 2 and guion.main(["--cuenta", "--ensayo"]) == 2
    fuente = (_ROOT / "tools" / "ensayo_facturas_drive.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(psycopg|httpx|requests|socket|subprocess)\b", fuente, re.M)


def test_el_humo_lee_las_columnas_y_la_tabla_de_drive_de_las_facturas_sin_tolerancia():
    humo = (_ROOT / "tools" / "humo.py").read_text(encoding="utf-8")
    assert "_drive_de_las_facturas" in humo
    i = humo.index("async def _drive_de_las_facturas")
    cuerpo = humo[i:humo.index("\nasync def ", i + 10)]
    for nombre in list(COLUMNAS_DE_LAS_FACTURAS) + ["carpeta_facturas_id", "drive_carpetas_sueltas"]:
        assert nombre in cuerpo, nombre
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", cuerpo)
    assert "_drive_de_las_facturas" in humo[humo.index("pruebas = ["):]
