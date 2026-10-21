"""El Drive de las cotizaciones (pieza 8 del diseño «Cotizaciones en Proyectos», 16-oct-2026, §3.7):
`cotizaciones_drive.py`, `db.cotizacion_para_drive`/`marcar_*`/`*_de_drive`, la ruta «Volver a subir a Drive» y lo
que la página dice de Drive.

CÓMO. El camino es el DE PRODUCCIÓN con dos cambios, los dos en el borde: la base es SQLite con las sentencias
REALES de `db` sobre las tablas del esquema, y Drive es un transporte de mentira de `httpx` (`httpx.MockTransport`,
como `tests/test_drive.py`) detrás del cliente de verdad (`drive.py`). Ni `cotizaciones_drive` ni `drive` ni la
ruta se reemplazan. Se recorren las rutas reales (`POST /proyectos/{pid}/cotizaciones[/{cid}]`, `POST …/drive`,
`GET /proyectos`) con la plantilla real. Ningún pedido real sale: la fixture `google` de `tests/test_drive.py`
revienta y anota cualquier salida a la red que no sea el transporte de mentira.

QUÉ SE VIGILA (las garantías de la pieza 8 del diseño):
  1. NINGUNA FRASE DICE «EN DRIVE» si `drive_version != cambiado_en`: se recorren las claves de aviso que la
     plantilla conoce (sacadas del texto de la plantilla), cada estado de Drive de la fila, y la página pintada
     con Drive fallando en cada llamada de la subida, con cuatro clases de fallo;
  2. con Drive caído la cotización queda guardada y enviada (la base dice lo mismo que sin Drive);
  3. corregir reemplaza el MISMO archivo (el doble ve un PATCH al mismo id y no un archivo nuevo);
  4. un reintento sin id guardado no crea un segundo archivo si ya hay uno con ese nombre;
  5. un proyecto renombrado no crea otra carpeta (se le cambia el nombre a la misma, por id);
  6. dos proyectos con el mismo nombre tienen carpetas distintas;
  7. una carpeta escrita a mano no se pisa; una vacía se llena con la de Drive, por la puerta de siempre;
  8. si alguien corrige mientras se sube, la marca no se pone;
  9. `drive_version` no se marca antes de que Drive conteste;
 10. las causas son un vocabulario cerrado (nunca texto de Google); una carpeta borrada a mano no se rehace sola;
 11. el tope de tiempo es de TODO el intento; borrador, solo ver y sin sesión no suben nada;
 12. la migración, el esquema y la lectura (sin las columnas de Drive la página carga sin el bloque).

FRONTERA, dicha una vez:
  · NO hay Postgres: SQLite (ver `tests/test_guardar_cotizacion.py`); `now()` sube un microsegundo por llamada.
  · El Drive de mentira afirma lo que afirma `tests/test_drive.py` (documentación de Drive v3 leída de memoria, sin
    una captura real): que listar filtra por padre y nombre, que crear contesta `{id}`, que el reemplazo conserva el
    id. Que Google se porte así NO está comprobado: lo primero que mira la sala en la puesta en marcha.
  · Las dos subidas simultáneas del mismo proyecto nuevo no se prueban (el doble corre en un solo hilo).

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import logging
import re
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)
from pypdf import PdfReader

import test_buzon_que_no_se_ve as buzon
import test_guardar_cotizacion as tg
import test_cotizaciones_tablas as tt
import test_grupo_ia as g
from test_cotizaciones_en_la_pagina import (  # noqa: F401  (fixtures y ayudas de la página)
    _casa, _contenido, _filas, _formulario, _guardar, _pagina, _post_de_la_pagina, _seccion_de_cotizaciones,
    _ver, m)
from test_drive import (  # noqa: F401  (el Google de mentira y su guarda de red)
    Google, HTTPX, _R, google)
from test_grupo_ia import _ROOT
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)
from _navegador import dar_recibo

import cotizaciones as cz
import cotizaciones_drive as cd
import db.db as db
import drive
import noco_lectura  # noqa: F401
import config

CARPETA_PROYECTOS = "PROYECTOS-RAIZ"
MARCA_DE_GOOGLE = "TEXTO-DE-GOOGLE-QUE-NO-SE-DICE"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# El Drive de mentira: con memoria (carpetas, archivos), para ver qué quedó, no solo qué se pidió
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _partes_de(cuerpo: bytes, limite: str) -> list[bytes]:
    cortes = cuerpo.split(("--" + limite).encode())
    return [c[2:-2] if c.startswith(b"\r\n") and c.endswith(b"\r\n") else c for c in cortes[1:-1]]


class Nube:
    """Lo que `Google.api` contesta: un Drive de mentira con memoria. Forma de cada respuesta = la que afirma
    `tests/test_drive.py` (ver su cabecera). `falla`: una función `(paso, pedido) -> Response | None`; si devuelve
    algo, eso contesta (y el pedido NO toca la memoria). `al_subir`: una función que corre cuando llega una
    subida, ANTES de contestarla (para meter algo en la base en ese instante)."""

    def __init__(self):
        self.cosas: dict[str, dict] = {CARPETA_PROYECTOS: {"tipo": "carpeta", "nombre": "Proyectos", "padre": None}}
        self.pasos: list[tuple[str, str]] = []          # (paso, id o nombre) en el orden en que llegaron
        self.falla = None
        self.al_subir = None
        self._n = 0

    # ── lo que quedó ──
    def hijos(self, padre: str, nombre: str | None = None, tipo: str | None = None) -> list[str]:
        return [i for i, c in self.cosas.items() if c["padre"] == padre
                and (nombre is None or c["nombre"] == nombre) and (tipo is None or c["tipo"] == tipo)]

    def archivos(self, nombre: str | None = None) -> list[str]:
        return [i for i, c in self.cosas.items() if c["tipo"] == "pdf" and (nombre is None or c["nombre"] == nombre)]

    def carpetas_de_proyectos(self) -> list[str]:
        return self.hijos(CARPETA_PROYECTOS, tipo="carpeta")

    def pasos_de(self, paso: str) -> list[str]:
        return [x for p, x in self.pasos if p == paso]

    def _nuevo(self, prefijo: str) -> str:
        self._n += 1
        return f"{prefijo}{self._n}"

    # ── el manejador ──
    async def manejar(self, p):
        ruta, metodo = p.url.path, p.method
        if metodo == "GET" and ruta == "/drive/v3/files":
            paso = "buscar"
        elif metodo == "POST" and ruta == "/drive/v3/files":
            paso = "crear_carpeta"
        elif metodo == "PATCH" and ruta.startswith("/drive/v3/files/"):
            paso = "renombrar"
        elif metodo == "POST" and ruta == "/upload/drive/v3/files":
            paso = "subir_nuevo"
        elif metodo == "PATCH" and ruta.startswith("/upload/drive/v3/files/"):
            paso = "reemplazar"
        else:
            return _R(418, json={"msg": "ruta que el doble no conoce"})
        if paso in ("subir_nuevo", "reemplazar") and self.al_subir is not None:
            self.al_subir()
        if self.falla is not None:
            try:
                r = self.falla(paso, p)
                if inspect.isawaitable(r):
                    r = await r
            except BaseException:
                self.pasos.append((paso + "!", ruta))
                raise
            if r is not None:
                self.pasos.append((paso + "!", ruta))
                return r
        return getattr(self, "_" + paso)(p)

    def _buscar(self, p):
        q = p.url.params["q"]
        m_ = re.fullmatch(r"'([A-Za-z0-9_-]+)' in parents and name = '((?:[^'\\]|\\.)*)' and trashed = false", q)
        assert m_, q
        padre, nombre = m_.group(1), re.sub(r"\\(.)", r"\1", m_.group(2))
        self.pasos.append(("buscar", nombre))
        return _R(200, json={"files": [{"id": i, "name": self.cosas[i]["nombre"]} for i in self.hijos(padre, nombre)]})

    def _crear_carpeta(self, p):
        cuerpo = json.loads(p.content)
        assert cuerpo["mimeType"] == drive.MIME_CARPETA and len(cuerpo["parents"]) == 1
        if cuerpo["parents"][0] not in self.cosas:
            return _R(404, json={"error": {"errors": [{"reason": "notFound"}]}})
        nuevo = self._nuevo("CARPETA")
        self.cosas[nuevo] = {"tipo": "carpeta", "nombre": cuerpo["name"], "padre": cuerpo["parents"][0]}
        self.pasos.append(("crear_carpeta", nuevo))
        return _R(200, json={"id": nuevo})

    def _renombrar(self, p):
        ident = p.url.path.rsplit("/", 1)[1]
        if ident not in self.cosas:
            return _R(404, json={})
        self.cosas[ident]["nombre"] = json.loads(p.content)["name"]
        self.pasos.append(("renombrar", ident))
        return _R(200, json={"id": ident})

    def _subir_nuevo(self, p):
        limite = re.search(r"boundary=(\S+)", p.headers["content-type"]).group(1)
        meta, datos = _partes_de(p.content, limite)
        cuerpo = json.loads(meta.split(b"\r\n\r\n", 1)[1])
        pdf = datos.split(b"\r\n\r\n", 1)[1]
        if cuerpo["parents"][0] not in self.cosas:
            return _R(404, json={})
        nuevo = self._nuevo("ARCHIVO")
        self.cosas[nuevo] = {"tipo": "pdf", "nombre": cuerpo["name"], "padre": cuerpo["parents"][0], "datos": pdf}
        self.pasos.append(("subir_nuevo", nuevo))
        return _R(200, json={"id": nuevo})

    def _reemplazar(self, p):
        ident = p.url.path.rsplit("/", 1)[1]
        if ident not in self.cosas:
            return _R(404, json={})
        self.cosas[ident]["datos"] = p.content
        self.pasos.append(("reemplazar", ident))
        return _R(200, json={"id": ident})


@pytest.fixture
def nube(google, monkeypatch):
    """Google con memoria y las variables puestas (las del cliente las pone `google`; la de «Proyectos» aquí)."""
    n = Nube()
    google.api = n.manejar
    monkeypatch.setenv("LUCY_DRIVE_CARPETA_PROYECTOS", CARPETA_PROYECTOS)
    n.google = google
    return n


def _responde(estado, **kw):
    return lambda paso, p: _R(estado, **kw)


def _falla_desde(k: int, respuesta):
    """Contesta con `respuesta` (una función `(paso, p) -> Response`) de la llamada número `k` (desde 0) en adelante."""
    cuenta = {"n": -1}

    def f(paso, p):
        cuenta["n"] += 1
        return respuesta(paso, p) if cuenta["n"] >= k else None
    return f


# Las cuatro clases de fallo de Drive de la subida (la causa que debe quedar sale de `drive.py`, no se teclea):
FALLOS = {
    "500": (lambda paso, p: _R(500, json={"error": {"message": MARCA_DE_GOOGLE}}), drive.DriveNoContesta.causa),
    "403": (lambda paso, p: _R(403, json={"error": {"message": MARCA_DE_GOOGLE, "errors": [{"reason": "forbidden"}]}}),
            drive.DriveSinPermiso.causa),
    "red": (lambda paso, p: (_ for _ in ()).throw(HTTPX.ConnectError(MARCA_DE_GOOGLE, request=p)),
            drive.DriveNoContesta.causa),
    "401": (lambda paso, p: _R(401, json={"error": MARCA_DE_GOOGLE}), drive.DriveSinPermiso.causa),
}


# ── la base y las rutas ──

def _enviar(proyecto=1, c=None, accion="enviar"):
    """«Guardar y descargar PDF» de una cotización NUEVA por la ventana real: (respuesta del POST, página que sigue)."""
    c = c or _casa()
    f = _formulario(_pagina(c, p=proyecto, cotizacion="nueva"))
    r = c.post(f"/proyectos/{proyecto}/cotizaciones", data=_post_de_la_pagina(f, accion), follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r, c.get(r.headers["location"].split("#")[0]).text


def _corregir(cid=1, proyecto=1, c=None):
    c = c or _casa()
    f = _formulario(_pagina(c, p=proyecto, cotizacion=cid, corregir=1))
    r = c.post(f"/proyectos/{proyecto}/cotizaciones/{cid}", data=_post_de_la_pagina(f, "enviar"),
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r, c.get(r.headers["location"].split("#")[0]).text


def _volver_a_subir(cid=1, proyecto=1, c=None):
    c = c or _casa()
    r = c.post(f"/proyectos/{proyecto}/cotizaciones/{cid}/drive", follow_redirects=False)
    return r, (c.get(r.headers["location"].split("#")[0]).text if r.status_code == 303 else None)


def _fila(m_, cid=1):
    return _filas(m_, f"SELECT * FROM cotizaciones WHERE id = {cid}")[0]


def _carpetas(m_, proyecto=1):
    f = _filas(m_, f"SELECT * FROM drive_de_proyectos WHERE proyecto_id = {proyecto}")
    return f[0] if f else None


def _afirmaciones_de_estar_en_drive(texto: str) -> list[str]:
    """Las frases de una página que AFIRMAN que algo está en Drive: «En Drive» (la etiqueta), «y en Drive» y «Subida a
    Drive». Se quitan antes las que dicen otra cosa con las mismas palabras (los enlaces y las negaciones), declaradas
    una por una."""
    permitidas = ("Abrir en Drive", "No está en Drive", "ya no está en Drive", "Crear de nuevo la carpeta en Drive")
    for frase in permitidas:
        texto = texto.replace(frase, "")
    return re.findall(r"(?i)\ben drive\b|subida a drive", texto)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 0. El camino bueno, con lo que quedó en Drive y en la base
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_enviar_una_nueva_la_sube_a_proyectos_nombre_cotizaciones_y_la_marca_en_drive(m, nube):
    r, html = _enviar()
    fila, carpetas = _fila(m), _carpetas(m)
    # en Drive: Proyectos / <nombre del proyecto> / cotizaciones / COT-2026-001.pdf
    del_proyecto = nube.hijos(CARPETA_PROYECTOS, "Disco de prueba", "carpeta")
    assert len(del_proyecto) == 1 and carpetas["carpeta_proyecto_id"] == del_proyecto[0]
    de_cotizaciones = nube.hijos(del_proyecto[0], "cotizaciones", "carpeta")
    assert len(de_cotizaciones) == 1 and carpetas["carpeta_cotizaciones_id"] == de_cotizaciones[0]
    assert carpetas["nombre_puesto"] == "Disco de prueba"
    archivos = nube.archivos("COT-2026-001.pdf")
    assert len(archivos) == 1 and nube.cosas[archivos[0]]["padre"] == de_cotizaciones[0]
    # el PDF que subió es el de ESA versión, sin marca de borrador
    texto = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(nube.cosas[archivos[0]]["datos"])).pages)
    assert "COT-2026-001" in texto and "BORRADOR" not in texto
    # la marca: lo que Drive contestó, en la versión que se subió
    assert (fila["drive_archivo_id"], fila["drive_version"], fila["drive_error"]) == (
        archivos[0], fila["cambiado_en"], None)
    assert "Guardada en Lucy y en Drive." in html and "NO llegó" not in html
    assert _afirmaciones_de_estar_en_drive(_seccion_de_cotizaciones(html)) != []
    assert not google_hablo_de_mas(nube)


def google_hablo_de_mas(nube) -> bool:
    """Todo pedido a Drive fue a la API de Drive o al servidor de tokens (la guarda de red de `google` ya vigila lo demás)."""
    return any(p.url.host not in ("www.googleapis.com", "oauth2.googleapis.com") for p in nube.google.pedidos)


def test_un_borrador_no_se_sube_y_el_aviso_no_habla_de_drive(m, nube):
    r, html = _enviar(accion="borrador")
    assert nube.google.pedidos == [] and nube.cosas.keys() == {CARPETA_PROYECTOS}
    assert "Drive" not in _seccion_de_cotizaciones(html).replace("Abrir en Drive", "")
    assert _fila(m)["estado"] == "borrador" and _carpetas(m) is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Ninguna frase dice «en Drive» si drive_version != cambiado_en
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _cot(**kw):
    base = {"estado": "enviada", "cambiado_en": "2026-10-14 12:00:00.000001+00:00", "drive_archivo_id": None,
            "drive_version": None, "drive_error": None}
    base.update(kw)
    return base


@pytest.mark.parametrize("archivo", [None, "", "ARCH1"])
@pytest.mark.parametrize("version", [None, "2026-10-14 12:00:00.000001+00:00", "2026-10-14 11:00:00.000001+00:00", 5, ""])
@pytest.mark.parametrize("error", [None, drive.DriveNoContesta.causa, "texto que Google dijo"])
def test_en_drive_es_solo_archivo_y_version_igual_a_cambiado_en_y_ninguna_otra_frase_lo_dice(archivo, version, error):
    cot = _cot(drive_archivo_id=archivo, drive_version=version, drive_error=error)
    e = cd.estado_de_drive(cot)
    esta = bool(archivo) and version == cot["cambiado_en"]
    assert (e["estado"] == "en_drive") is esta
    frases = [e["etiqueta"], e["aviso_al_guardar"], e["aviso_al_reintentar"]]
    for f in frases:
        dice_que_esta = bool(_afirmaciones_de_estar_en_drive(f))
        assert dice_que_esta is esta, (cot, f)
    # el texto que no es de las causas fijas nunca llega a una frase
    assert "texto que Google dijo" not in " ".join(frases)


def test_un_borrador_no_tiene_estado_de_drive():
    assert cd.estado_de_drive(_cot(estado="borrador")) is None


def _hechos_de_la_plantilla() -> set[str]:
    """Las claves de aviso (`hecho`) de las cotizaciones que la plantilla conoce, sacadas de su texto."""
    texto = (_ROOT / "web" / "plantillas" / "_cotizaciones_del_proyecto.html").read_text(encoding="utf-8")
    claves = set()
    for etiqueta in re.findall(r"\{%-?.*?%\}", texto, re.S):          # cada etiqueta de Jinja que mira `hecho`
        if re.search(r"\bhecho\b", etiqueta):
            claves |= set(re.findall(r"'(cotizacion_\w+)'", etiqueta))
    return claves


def test_las_claves_de_aviso_de_las_cotizaciones_que_conoce_la_plantilla_son_las_que_las_rutas_mandan():
    app = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
    mandadas = set(re.findall(r"hecho=(cotizacion_\w+)", app)) | set(re.findall(r'hecho = "(cotizacion_\w+)"', app)) \
        | set(re.findall(r'else "(cotizacion_\w+)"', app))
    assert mandadas and mandadas == _hechos_de_la_plantilla(), (mandadas, _hechos_de_la_plantilla())


ESTADOS_DE_LA_FILA = {
    "en drive": lambda c: {"drive_archivo_id": "ARCH1", "drive_version": c, "drive_error": None},
    "vieja": lambda c: {"drive_archivo_id": "ARCH1", "drive_version": "2026-01-01 00:00:00.000001+00:00", "drive_error": None},
    "vieja con causa": lambda c: {"drive_archivo_id": "ARCH1", "drive_version": "2026-01-01 00:00:00.000001+00:00",
                                  "drive_error": drive.DriveNoContesta.causa},
    "no esta": lambda c: {"drive_archivo_id": None, "drive_version": None, "drive_error": None},
    "no esta con causa": lambda c: {"drive_archivo_id": None, "drive_version": None,
                                    "drive_error": drive.DriveSinPermiso.causa},
    "causa que no es de las fijas": lambda c: {"drive_archivo_id": None, "drive_version": None,
                                               "drive_error": MARCA_DE_GOOGLE},
    "version sin archivo": lambda c: {"drive_archivo_id": None, "drive_version": c, "drive_error": None},
}


@pytest.mark.parametrize("estado", sorted(ESTADOS_DE_LA_FILA))
@pytest.mark.parametrize("hecho", sorted(_hechos_de_la_plantilla()))
def test_cada_aviso_con_cada_estado_de_la_fila_solo_dice_en_drive_si_esta(m, estado, hecho):
    """Recorre las claves de aviso de la plantilla (de su texto) con cada estado de Drive de la fila y la página
    REAL: lo que se pinta dice «en Drive» si y solo si `drive_version == cambiado_en` (con archivo)."""
    _guardar("enviar")
    antes = _fila(m)
    m.con.execute("UPDATE cotizaciones SET drive_archivo_id = ?, drive_version = ?, drive_error = ? WHERE id = 1",
                  tuple(ESTADOS_DE_LA_FILA[estado](antes["cambiado_en"]).values()))
    m.con.commit()
    fila = _fila(m)
    esta = bool(fila["drive_archivo_id"]) and fila["drive_version"] == fila["cambiado_en"]
    dar_recibo("/proyectos", p=1, hecho=hecho, cotizacion=1)
    html = _casa().get("/proyectos", params={"p": 1, "hecho": hecho, "cotizacion": 1}).text
    seccion = _seccion_de_cotizaciones(html)
    assert bool(_afirmaciones_de_estar_en_drive(seccion)) is esta, (hecho, estado, _afirmaciones_de_estar_en_drive(seccion))
    assert MARCA_DE_GOOGLE not in html
    assert "texto que Google" not in html
    if not esta:
        assert "Volver a subir a Drive" in seccion       # con la salida a mano: la casa siempre puede reintentar


@pytest.mark.parametrize("estado", sorted(ESTADOS_DE_LA_FILA))
def test_la_lista_y_la_hoja_en_solo_ver_dicen_lo_mismo_y_no_traen_el_boton(m, estado):
    _guardar("enviar")
    antes = _fila(m)
    m.con.execute("UPDATE cotizaciones SET drive_archivo_id = ?, drive_version = ?, drive_error = ? WHERE id = 1",
                  tuple(ESTADOS_DE_LA_FILA[estado](antes["cambiado_en"]).values()))
    m.con.commit()
    fila = _fila(m)
    esta = bool(fila["drive_archivo_id"]) and fila["drive_version"] == fila["cambiado_en"]
    for params in ({"p": 1}, {"p": 1, "cotizacion": 1}):
        seccion = _seccion_de_cotizaciones(_ver().get("/proyectos", params=params).text)
        assert bool(_afirmaciones_de_estar_en_drive(seccion)) is esta
        assert "Volver a subir" not in seccion and "/drive\"" not in seccion
    assert ("La versión de Drive es vieja" in _seccion_de_cotizaciones(_ver().get("/proyectos", params={"p": 1}).text)) \
        is (bool(fila["drive_archivo_id"]) and not esta)


# Fallando cada llamada de la subida con cada clase de fallo (la causa sale de `drive.py`):

def _llamadas_de_una_subida_limpia(m, nube) -> int:
    _enviar()
    return len([p for p, _ in nube.pasos])


def test_una_subida_limpia_hace_cinco_llamadas_a_drive_y_el_barrido_las_cubre(m, nube):
    """El barrido de abajo falla la llamada número `k`, de 0 a `LLAMADAS_A_BARRER - 1`: tiene que cubrir TODAS las de
    una primera subida (medidas aquí con el doble: buscar el nombre, crear las dos carpetas, buscar el PDF, subirlo)."""
    n = _llamadas_de_una_subida_limpia(m, nube)
    assert [p for p, _ in nube.pasos] == ["buscar", "crear_carpeta", "crear_carpeta", "buscar", "subir_nuevo"]
    assert n <= LLAMADAS_A_BARRER


LLAMADAS_A_BARRER = 6


@pytest.mark.parametrize("fallo", sorted(FALLOS))
@pytest.mark.parametrize("k", range(LLAMADAS_A_BARRER))
def test_con_drive_fallando_desde_cada_llamada_la_cotizacion_queda_guardada_y_ninguna_frase_dice_en_drive(
        m, nube, k, fallo):
    respuesta, causa = FALLOS[fallo]
    nube.falla = _falla_desde(k, respuesta)
    solo_enviar = m.con.execute("SELECT count(*) FROM cotizaciones").fetchone()[0]
    r, html = _enviar()
    fila = _fila(m)
    llamadas = len([p for p, _ in nube.pasos if not p.endswith("!")]) + len([p for p, _ in nube.pasos if p.endswith("!")])
    hubo_fallo = any(p.endswith("!") for p, _ in nube.pasos)
    # 2. guardada y enviada, pase lo que pase en Drive
    assert (fila["estado"], fila["numero"], fila["anio"]) == ("enviada", 1, 2026) and fila["enviada_en"] is not None
    assert m.con.execute("SELECT count(*) FROM cotizaciones").fetchone()[0] == solo_enviar + 1
    seccion = _seccion_de_cotizaciones(html)
    if hubo_fallo:
        # 1. no llegó: ni la marca ni una frase que lo diga
        assert fila["drive_version"] is None and fila["drive_archivo_id"] is None
        assert fila["drive_error"] == causa and fila["drive_error"] in cd.CAUSAS
        assert _afirmaciones_de_estar_en_drive(seccion) == []
        assert f"NO llegó a Drive ({causa})" in seccion and "Puedes volver a intentarlo" in seccion
    else:
        assert k >= llamadas                       # el fallo cayó después de la última llamada: subió limpia
        assert fila["drive_version"] == fila["cambiado_en"]
    assert MARCA_DE_GOOGLE not in html and MARCA_DE_GOOGLE not in repr(fila)
    # el PDF que se bajaría es el de lo guardado (la ruta del PDF no depende de Drive)
    assert _casa().get("/proyectos/1/cotizaciones/1/pdf").status_code == 200


def test_con_drive_caido_la_base_queda_igual_que_sin_drive(m, nube, monkeypatch):
    """Lo que escribe la base al guardar y enviar es lo mismo con Drive caído, con Drive bueno y sin Drive
    configurado (menos las tres columnas de Drive): la huella, el número, el estado y el contenido."""
    def lo_de_la_base():
        f = _fila(m)
        return ({k: v for k, v in f.items() if not k.startswith("drive_") and k not in ("cambiado_en", "creado_en", "enviada_en")},
                [(h["accion"], h["tabla"]) for h in _filas(m, "SELECT accion, tabla FROM log_acciones ORDER BY id")])
    nube.falla = _falla_desde(0, FALLOS["500"][0])
    _enviar()
    con_drive_caido = lo_de_la_base()
    m.con.execute("DELETE FROM cotizaciones")
    m.con.execute("DELETE FROM serie_cotizaciones")
    m.con.execute("DELETE FROM log_acciones")
    m.con.commit()
    nube.falla = None
    _enviar()
    con_drive_bueno = lo_de_la_base()
    m.con.execute("DELETE FROM cotizaciones")
    m.con.execute("DELETE FROM serie_cotizaciones")
    m.con.execute("DELETE FROM log_acciones")
    m.con.commit()
    monkeypatch.delenv("LUCY_DRIVE_CLIENT_ID")
    _enviar()
    sin_drive = lo_de_la_base()
    assert con_drive_caido == con_drive_bueno == sin_drive
    assert _fila(m)["drive_error"] == drive.DriveSinConfigurar.causa


def test_si_la_subida_entera_revienta_por_cualquier_motivo_la_cotizacion_sigue_guardada(m, nube, monkeypatch):
    async def revienta(pid, cid):
        raise RuntimeError("la base se cayó")
    monkeypatch.setattr(cd, "subir", revienta)
    r, html = _enviar()
    assert (_fila(m)["estado"], _fila(m)["drive_version"]) == ("enviada", None)
    assert _afirmaciones_de_estar_en_drive(_seccion_de_cotizaciones(html)) == []


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3. Corregir reemplaza el MISMO archivo   ·   4. un reintento sin id no crea un segundo
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_corregir_una_enviada_reemplaza_el_mismo_archivo_y_la_marca_sigue_a_la_version_nueva(m, nube):
    _enviar()
    primero = _fila(m)
    archivo = primero["drive_archivo_id"]
    nube.google.pedidos.clear()
    nube.pasos.clear()
    r, html = _corregir()
    segundo = _fila(m)
    assert segundo["cambiado_en"] != primero["cambiado_en"]
    assert nube.pasos == [("reemplazar", archivo)], nube.pasos          # un PATCH al MISMO id, ningún archivo nuevo
    assert nube.archivos() == [archivo] and len(nube.archivos("COT-2026-001.pdf")) == 1
    assert (segundo["drive_archivo_id"], segundo["drive_version"]) == (archivo, segundo["cambiado_en"])
    assert segundo["numero"] == primero["numero"] == 1
    cuerpo = [p for p in nube.google.de_api() if p.method == "PATCH"]
    assert len(cuerpo) == 1 and cuerpo[0].url.path == f"/upload/drive/v3/files/{archivo}"
    assert "Guardada en Lucy y en Drive." in html


def test_corregir_con_drive_caido_deja_la_version_de_drive_como_vieja_y_lo_dice(m, nube):
    _enviar()
    nube.falla = _responde(500, json={})
    _corregir()
    fila = _fila(m)
    assert fila["drive_version"] != fila["cambiado_en"] and fila["drive_archivo_id"] is not None
    html = _casa().get("/proyectos", params={"p": 1, "cotizacion": 1}).text
    assert "La versión de Drive es vieja" in _seccion_de_cotizaciones(html)
    assert _afirmaciones_de_estar_en_drive(_seccion_de_cotizaciones(html)) == []


def test_un_reintento_sin_id_guardado_reemplaza_el_archivo_que_ya_hay_y_no_crea_un_segundo(m, nube, monkeypatch):
    """Se cae entre «Drive contestó» y «se anotó el id»: la fila queda sin id pero el archivo ya está en Drive. El
    reintento lo encuentra por nombre y lo reemplaza."""
    real = db.marcar_cotizacion_en_drive

    async def se_cae(*a, **k):
        raise RuntimeError("se cayó la base justo después de subir")
    monkeypatch.setattr(db, "marcar_cotizacion_en_drive", se_cae)
    _enviar()
    assert _fila(m)["drive_archivo_id"] is None and len(nube.archivos()) == 1
    ya_estaba = nube.archivos()[0]
    monkeypatch.setattr(db, "marcar_cotizacion_en_drive", real)
    nube.pasos.clear()
    r, html = _volver_a_subir()
    assert nube.archivos() == [ya_estaba], "se creó un segundo archivo"
    assert nube.pasos == [("buscar", "COT-2026-001.pdf"), ("reemplazar", ya_estaba)]
    assert _fila(m)["drive_archivo_id"] == ya_estaba and _fila(m)["drive_version"] == _fila(m)["cambiado_en"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 5. Un proyecto renombrado no crea otra carpeta   ·   6. dos proyectos con el mismo nombre
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_un_proyecto_renombrado_no_crea_otra_carpeta_se_le_cambia_el_nombre_a_la_misma(m, nube):
    _enviar()
    carpetas = _carpetas(m)
    m.con.execute("UPDATE proyectos SET nombre = 'Disco con otro nombre' WHERE id = 1")
    m.con.commit()
    nube.pasos.clear()
    _guardar("enviar")                                       # la 2, del mismo proyecto
    r, html = _corregir(cid=2)
    assert nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]], "se creó otra carpeta"
    assert nube.cosas[carpetas["carpeta_proyecto_id"]]["nombre"] == "Disco con otro nombre"
    assert _carpetas(m)["nombre_puesto"] == "Disco con otro nombre"
    assert _carpetas(m)["carpeta_proyecto_id"] == carpetas["carpeta_proyecto_id"]
    assert nube.pasos_de("renombrar") == [carpetas["carpeta_proyecto_id"]]
    assert nube.cosas[nube.archivos("COT-2026-002.pdf")[0]]["padre"] == carpetas["carpeta_cotizaciones_id"]


def test_si_el_renombre_falla_la_subida_sigue_y_el_nombre_se_arregla_en_la_proxima(m, nube):
    _enviar()
    m.con.execute("UPDATE proyectos SET nombre = 'Otro nombre' WHERE id = 1")
    m.con.commit()
    nube.falla = lambda paso, p: _R(500, json={}) if paso == "renombrar" else None
    _guardar("enviar")
    _corregir(cid=2)
    assert _fila(m, 2)["drive_version"] == _fila(m, 2)["cambiado_en"]       # el PDF llegó
    assert _carpetas(m)["nombre_puesto"] == "Disco de prueba"               # el nombre sigue sin cambiar
    nube.falla = None
    _volver_a_subir(cid=2)         # ya está en Drive: no hace nada
    _corregir(cid=2)               # una subida nueva
    assert nube.cosas[_carpetas(m)["carpeta_proyecto_id"]]["nombre"] == "Otro nombre"
    assert _carpetas(m)["nombre_puesto"] == "Otro nombre"


def test_dos_proyectos_con_el_mismo_nombre_tienen_carpetas_distintas_y_la_segunda_lleva_su_numero(m, nube):
    m.con.execute("UPDATE proyectos SET nombre = 'Disco de prueba' WHERE id = 3")
    m.con.commit()
    _enviar(proyecto=1)
    _enviar(proyecto=3)
    a, b = _carpetas(m, 1), _carpetas(m, 3)
    assert a["carpeta_proyecto_id"] != b["carpeta_proyecto_id"] and a["carpeta_cotizaciones_id"] != b["carpeta_cotizaciones_id"]
    assert nube.cosas[a["carpeta_proyecto_id"]]["nombre"] == "Disco de prueba"
    assert nube.cosas[b["carpeta_proyecto_id"]]["nombre"] == "Disco de prueba · #3" == b["nombre_puesto"]
    # cada PDF en la carpeta de SU proyecto
    padres = {nube.cosas[i]["nombre"]: nube.cosas[i]["padre"] for i in nube.archivos()}
    assert padres == {"COT-2026-001.pdf": a["carpeta_cotizaciones_id"], "COT-2026-002.pdf": b["carpeta_cotizaciones_id"]}


def test_una_segunda_cotizacion_del_mismo_proyecto_usa_las_mismas_carpetas(m, nube):
    _enviar()
    carpetas = _carpetas(m)
    _enviar()
    assert nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]] and _carpetas(m) == carpetas
    assert len(nube.hijos(carpetas["carpeta_proyecto_id"], "cotizaciones")) == 1 and len(nube.archivos()) == 2


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 7. Una carpeta escrita a mano no se pisa; una vacía se llena
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("escrita", ["https://drive.example.test/carpetas/uno", "D:\\Proyectos\\Disco", "~/Disco", " x "])
def test_una_carpeta_escrita_a_mano_no_se_pisa(m, nube, escrita):
    m.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 1", (escrita,))
    m.con.commit()
    antes = _filas(m, "SELECT count(*) AS n FROM log_acciones WHERE tabla = 'proyectos'")[0]["n"]
    _enviar()
    _enviar()
    assert _filas(m, "SELECT carpeta FROM proyectos WHERE id = 1")[0]["carpeta"] == escrita
    assert _filas(m, "SELECT count(*) AS n FROM log_acciones WHERE tabla = 'proyectos'")[0]["n"] == antes


def test_una_carpeta_vacia_se_llena_con_la_de_drive_una_vez_y_con_huella_de_panel(m, nube):
    for vacia in (None, "", "   "):
        m.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 2", (vacia,))
        m.con.execute("DELETE FROM drive_de_proyectos")
        m.con.commit()
        nube.cosas = {CARPETA_PROYECTOS: nube.cosas[CARPETA_PROYECTOS]}
        _enviar(proyecto=2)
        puesta = _filas(m, "SELECT carpeta FROM proyectos WHERE id = 2")[0]["carpeta"]
        assert puesta == f"https://drive.google.com/drive/folders/{_carpetas(m, 2)['carpeta_proyecto_id']}", vacia
    h = _filas(m, "SELECT actor, accion, tabla, registro_id FROM log_acciones WHERE tabla = 'proyectos' ORDER BY id")
    assert h and all((x["actor"], x["accion"], x["registro_id"]) == ("panel", "editar", 2) for x in h)
    # y una segunda subida del mismo proyecto ya no la toca
    antes = len(h)
    _enviar(proyecto=2)
    assert len(_filas(m, "SELECT 1 FROM log_acciones WHERE tabla = 'proyectos'")) == antes


def test_el_enlace_de_drive_que_se_escribe_es_uno_que_la_pagina_deja_abrir(m, nube):
    _enviar(proyecto=2)
    puesta = _filas(m, "SELECT carpeta FROM proyectos WHERE id = 2")[0]["carpeta"]
    assert db.enlace_de_carpeta(puesta) == puesta


def test_la_unica_llamada_de_la_subida_a_crud_editar_es_la_carpeta_del_proyecto_con_actor_panel():
    arbol = ast.parse((_ROOT / "cotizaciones_drive.py").read_text(encoding="utf-8"))
    llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("editar", "borrar", "deshacer", "crear")]
    assert len(llamadas) == 1
    n = llamadas[0]
    assert ast.unparse(n.func) == "crud.editar" and n.args[0].value == "proyectos"
    assert [k.value for k in n.args[2].keys] == ["carpeta"]
    assert {k.arg: k.value.value for k in n.keywords if isinstance(k.value, ast.Constant)}["actor"] == "panel"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 8. Si alguien corrige mientras se sube, la marca no se pone   ·   9. no se marca antes de que Drive conteste
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_si_alguien_corrige_mientras_se_sube_la_marca_no_se_pone(m, nube):
    _guardar("enviar")

    def corrige_otro():
        m.con.execute("UPDATE cotizaciones SET cambiado_en = '2026-10-15 00:00:00.000001+00:00' WHERE id = 1")
    nube.al_subir = corrige_otro
    estado = asyncio.run(cd.subir(1, 1))
    fila = _fila(m)
    assert estado == "cambio"
    assert (fila["drive_archivo_id"], fila["drive_version"]) == (None, None)
    html = _casa().get("/proyectos", params={"p": 1, "cotizacion": 1}).text
    assert _afirmaciones_de_estar_en_drive(_seccion_de_cotizaciones(html)) == []


def test_si_alguien_corrige_mientras_se_sube_una_correccion_la_version_de_drive_queda_vieja(m, nube):
    _enviar()
    primero = _fila(m)
    nube.al_subir = lambda: m.con.execute("UPDATE cotizaciones SET cambiado_en = '2026-10-15 00:00:00.000001+00:00' WHERE id = 1")
    _corregir()
    fila = _fila(m)
    assert fila["drive_version"] == primero["cambiado_en"] and fila["drive_version"] != fila["cambiado_en"]
    assert cd.estado_de_drive({**fila, "contenido": None})["estado"] == "vieja"


def test_drive_version_no_se_marca_antes_de_que_drive_conteste(m, nube):
    """En el instante en que llega la subida a Drive (antes de contestarla) la base todavía no dice nada de Drive;
    cuando contesta con el id, sí."""
    visto = []

    def mirar():
        f = _fila(m)
        visto.append((f["drive_archivo_id"], f["drive_version"], f["drive_error"]))
    nube.al_subir = mirar
    _enviar()
    assert visto == [(None, None, None)]
    assert _fila(m)["drive_version"] == _fila(m)["cambiado_en"]


def test_un_error_de_drive_no_marca_nada_ni_borra_lo_que_ya_estaba(m, nube):
    _enviar()
    antes = _fila(m)
    nube.falla = _responde(500, json={})
    _corregir()
    fila = _fila(m)
    assert (fila["drive_archivo_id"], fila["drive_version"]) == (antes["drive_archivo_id"], antes["drive_version"])
    assert fila["drive_error"] == drive.DriveNoContesta.causa


def test_una_subida_buena_borra_la_causa_del_intento_anterior(m, nube):
    nube.falla = _responde(500, json={})
    _enviar()
    assert _fila(m)["drive_error"] == drive.DriveNoContesta.causa
    nube.falla = None
    _volver_a_subir()
    assert _fila(m)["drive_error"] is None and _fila(m)["drive_version"] == _fila(m)["cambiado_en"]


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 10. Las causas: un vocabulario cerrado, nunca texto de Google; carpetas borradas a mano
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _subclases(clase):
    for c in clase.__subclasses__():
        yield c
        yield from _subclases(c)


def test_toda_causa_de_una_excepcion_de_drive_esta_en_el_vocabulario_menos_la_que_cambia_segun_el_paso():
    """Los hermanos, de lo real: cada hija de `DriveError` (y la base) dice una causa que `CAUSAS` conoce. `DriveNoEsta`
    significa otra cosa según el paso (la carpeta «Proyectos», la del proyecto o el archivo) y por eso nunca se guarda tal cual."""
    clases = {drive.DriveError, *_subclases(drive.DriveError)}
    assert drive.DriveNoEsta in clases and drive.DriveSinCarpetaProyectos in clases and len(clases) == 6
    for c in clases - {drive.DriveNoEsta}:
        assert c.causa in cd.CAUSAS, c
    assert drive.DriveNoEsta.causa not in cd.CAUSAS
    assert len(set(cd.CAUSAS)) == len(cd.CAUSAS)


def test_sin_credenciales_la_causa_es_que_lucy_no_tiene_configurado_drive_y_no_sale_ningun_pedido(m, nube, monkeypatch):
    monkeypatch.delenv("LUCY_DRIVE_REFRESH_TOKEN")
    r, html = _enviar()
    assert nube.google.pedidos == [] and _fila(m)["drive_error"] == drive.DriveSinConfigurar.causa
    assert f"NO llegó a Drive ({drive.DriveSinConfigurar.causa})" in html


@pytest.mark.parametrize("valor", [None, "", "   ", "root", "con espacio", "a/b"])
def test_sin_la_carpeta_proyectos_nada_se_crea_a_ciegas(m, nube, monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("LUCY_DRIVE_CARPETA_PROYECTOS")
    else:
        monkeypatch.setenv("LUCY_DRIVE_CARPETA_PROYECTOS", valor)
    r, html = _enviar()
    assert nube.pasos == [] and nube.cosas.keys() == {CARPETA_PROYECTOS} and _carpetas(m) is None
    assert _fila(m)["drive_error"] == drive.DriveSinCarpetaProyectos.causa == "la carpeta Proyectos no está"
    assert f"NO llegó a Drive ({drive.DriveSinCarpetaProyectos.causa})" in html


def test_si_la_carpeta_proyectos_ya_no_esta_en_drive_la_causa_es_esa_y_no_se_crea_nada(m, nube, monkeypatch):
    monkeypatch.setenv("LUCY_DRIVE_CARPETA_PROYECTOS", "NOEXISTE")
    nube.falla = lambda paso, p: _R(404, json={}) if paso == "buscar" else None
    _enviar()
    assert _fila(m)["drive_error"] == drive.DriveSinCarpetaProyectos.causa and _carpetas(m) is None


def test_una_carpeta_borrada_a_mano_en_drive_no_se_rehace_sola_ni_se_borra_su_fila(m, nube):
    _enviar()
    carpetas = _carpetas(m)
    del nube.cosas[carpetas["carpeta_cotizaciones_id"]]            # alguien la borra en Drive
    nube.cosas = {i: c for i, c in nube.cosas.items() if c["padre"] != carpetas["carpeta_cotizaciones_id"]}
    _guardar("enviar")
    nube.pasos.clear()
    _corregir(cid=2)
    assert _fila(m, 2)["drive_error"] == cd.SIN_LA_CARPETA_DEL_PROYECTO == "la carpeta del proyecto ya no está en Drive"
    assert _carpetas(m) == carpetas                                # ni se borra ni se rehace la fila
    assert nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]]
    assert not any(p in ("crear_carpeta", "subir_nuevo") for p, _ in nube.pasos), nube.pasos
    html = _casa().get("/proyectos", params={"p": 1, "cotizacion": 2}).text
    renglon = re.search(r"<li>(?:(?!</li>).)*COT-2026-002(?:(?!</li>).)*</li>", _seccion_de_cotizaciones(html), re.S).group(0)
    assert "No está en Drive" in renglon and _afirmaciones_de_estar_en_drive(renglon) == []


def test_un_archivo_borrado_a_mano_con_la_carpeta_viva_se_sube_como_nuevo_y_queda_en_drive(m, nube):
    """El 404 del reemplazo suelta el id guardado y el MISMO intento sigue como una subida sin id (buscar por nombre y,
    si no está, crear). Sin pasar por «Crear de nuevo la carpeta»."""
    _enviar()
    viejo = _fila(m)["drive_archivo_id"]
    del nube.cosas[viejo]
    nube.pasos.clear()
    _corregir()
    fila = _fila(m)
    assert [p for p, _ in nube.pasos if not p.endswith("!")] == ["buscar", "subir_nuevo"]
    assert fila["drive_archivo_id"] not in (None, viejo) and fila["drive_version"] == fila["cambiado_en"]
    assert fila["drive_error"] is None and nube.archivos() == [fila["drive_archivo_id"]]
    assert "En Drive" in _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1}).text)


def test_un_archivo_borrado_a_mano_que_reaparece_con_el_mismo_nombre_se_reemplaza_y_no_se_duplica(m, nube):
    _enviar()
    viejo = _fila(m)["drive_archivo_id"]
    padre = nube.cosas.pop(viejo)["padre"]
    otro = nube._nuevo("ARCHIVO")                               # alguien (Lucy, antes) dejó otro con ese nombre
    nube.cosas[otro] = {"tipo": "pdf", "nombre": "COT-2026-001.pdf", "padre": padre, "datos": b"x"}
    _corregir()
    assert nube.archivos() == [otro] and _fila(m)["drive_archivo_id"] == otro


def test_si_el_archivo_y_la_carpeta_ya_no_estan_la_causa_es_la_de_la_carpeta(m, nube):
    _enviar()
    _borrar_en_drive(nube, _carpetas(m)["carpeta_cotizaciones_id"])
    _corregir()
    assert _fila(m)["drive_error"] == cd.SIN_LA_CARPETA_DEL_PROYECTO


def test_el_boton_de_crear_la_carpeta_no_sale_con_la_causa_del_archivo(m, nube):
    _enviar()
    m.con.execute("UPDATE cotizaciones SET drive_error = ?, drive_version = NULL WHERE id = 1", (cd.SIN_EL_ARCHIVO,))
    m.con.commit()
    hoja = _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1, "cotizacion": 1}).text)
    assert "Crear de nuevo" not in hoja and "Volver a subir a Drive" in hoja


def test_el_texto_que_google_contesta_no_llega_a_la_base_ni_a_la_pagina_ni_a_los_registros(m, nube, caplog):
    caplog.set_level(logging.DEBUG)
    for k, (respuesta, causa) in sorted(FALLOS.items()):
        m.con.execute("DELETE FROM drive_de_proyectos")
        m.con.commit()
        nube.falla = _falla_desde(0, respuesta)
        r, html = _enviar()
        assert MARCA_DE_GOOGLE not in html and MARCA_DE_GOOGLE not in json.dumps(
            _filas(m, "SELECT * FROM cotizaciones"), default=str)
    assert MARCA_DE_GOOGLE not in caplog.text
    assert {f["drive_error"] for f in _filas(m, "SELECT drive_error FROM cotizaciones")} <= set(cd.CAUSAS)


def test_si_el_pdf_no_se_puede_armar_la_causa_es_esa_y_no_sale_ningun_pedido(m, nube, monkeypatch):
    def revienta(*a, **k):
        raise ValueError("fpdf dice algo con datos del cliente")
    monkeypatch.setattr(cz, "pdf_de_cotizacion", revienta)
    r, html = _enviar()
    assert nube.google.pedidos == [] and _fila(m)["drive_error"] == cd.SIN_PDF
    assert "datos del cliente" not in html


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 11. El tope es de TODO el intento; borrador, solo ver y sin sesión no suben nada
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_el_tope_de_tiempo_es_de_todo_el_intento_y_no_de_cada_llamada(m, nube, monkeypatch):
    """Cada llamada cabe en el tope de `drive.py` (más de 0.04 s menos de 0.5 s) pero las cinco juntas pasan del tope
    del intento: la subida se corta con «Drive no contestó» sin marcar nada y sin esperar a las cinco."""
    monkeypatch.setattr(cd, "TIEMPO_DEL_INTENTO", 0.25)

    async def lenta(paso, p):
        await asyncio.sleep(0.1)
        return None
    nube.falla = lenta
    inicio = time.monotonic()
    r, html = _enviar()
    gastado = time.monotonic() - inicio
    fila = _fila(m)
    assert (fila["estado"], fila["drive_version"]) == ("enviada", None)
    assert fila["drive_error"] == drive.DriveNoContesta.causa
    assert len([p for p, _ in nube.pasos]) < 5, nube.pasos        # no llegó a hacer las cinco
    assert gastado < 1.5


def test_con_tiempo_de_sobra_las_mismas_llamadas_lentas_si_terminan(m, nube, monkeypatch):
    monkeypatch.setattr(cd, "TIEMPO_DEL_INTENTO", 5.0)

    async def lenta(paso, p):
        await asyncio.sleep(0.05)
        return None
    nube.falla = lenta
    _enviar()
    assert _fila(m)["drive_version"] == _fila(m)["cambiado_en"]


def test_volver_a_subir_sube_solo_lo_que_no_esta_y_el_boton_sale_solo_en_la_casa_y_solo_si_no_esta(m, nube):
    nube.falla = _responde(500, json={})
    _enviar()
    nube.falla = None
    nube.google.pedidos.clear()
    hoja = _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1, "cotizacion": 1}).text)
    assert 'action="/proyectos/1/cotizaciones/1/drive"' in hoja and "Volver a subir a Drive" in hoja
    assert "Volver a subir" not in _seccion_de_cotizaciones(_ver().get("/proyectos", params={"p": 1, "cotizacion": 1}).text)
    r, despues = _volver_a_subir()
    assert r.headers["location"] == "/proyectos?hecho=cotizacion_drive&p=1&cotizacion=1#cotizaciones"
    assert "Cotización COT-2026-001: Subida a Drive." in despues
    assert "Volver a subir" not in _seccion_de_cotizaciones(despues)           # ya está: sin botón
    pedidos = len(nube.google.pedidos)
    r2, otra = _volver_a_subir()                                                 # ya está en Drive: no sube nada
    assert len(nube.google.pedidos) == pedidos and r2.status_code == 303


def test_volver_a_subir_con_otro_fallo_lo_dice_con_su_causa(m, nube):
    nube.falla = _responde(500, json={})
    _enviar()
    nube.falla = _responde(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    r, html = _volver_a_subir()
    assert f"NO llegó a Drive ({drive.DriveSinPermiso.causa})" in html and _fila(m)["drive_error"] == drive.DriveSinPermiso.causa


def test_volver_a_subir_un_borrador_o_algo_que_no_existe_no_sube_nada(m, nube):
    _guardar("borrador")
    r, _ = _volver_a_subir()
    assert r.headers["location"].startswith("/proyectos?error=cotizacion_drive_borrador")
    r, _ = _volver_a_subir(cid=99)
    assert r.headers["location"].startswith("/proyectos?error=cotizacion_no_esta")
    r, _ = _volver_a_subir(cid=1, proyecto=3)           # de otro proyecto
    assert r.headers["location"].startswith("/proyectos?error=cotizacion_no_esta")
    assert nube.google.pedidos == []
    dar_recibo("/proyectos", p=1, cotizacion=1, error="cotizacion_drive_borrador")
    assert "Un borrador no se sube a Drive" in _casa().get(
        "/proyectos", params={"p": 1, "cotizacion": 1, "error": "cotizacion_drive_borrador"}).text


def test_solo_ver_y_sin_sesion_no_suben_nada_y_reciben_401(m, nube):
    _guardar("enviar")
    for cliente in (_ver(), TestClient(__import__("web.app", fromlist=["app"]).app, base_url="https://testserver")):
        r = cliente.post("/proyectos/1/cotizaciones/1/drive", follow_redirects=False)
        assert r.status_code == 401, r.status_code
    assert nube.google.pedidos == [] and _fila(m)["drive_error"] is None


def test_la_ruta_de_volver_a_subir_es_una_de_las_que_pide_sesion_de_la_casa(m, nube):
    """Los hermanos, de lo real: toda ruta POST de `/proyectos…/cotizaciones…` pide sesión de la casa dentro de su
    función (`puede_entrar`), la lista sale del árbol sintáctico de `web/app.py`."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    rutas = {}
    for f in arbol.body:
        if isinstance(f, ast.AsyncFunctionDef):
            for d in f.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
                        and d.args and "/cotizaciones" in d.args[0].value):
                    rutas[d.args[0].value] = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                                 and n.func.attr == "puede_entrar" for n in ast.walk(f))
    assert set(rutas) == {"/proyectos/{pid}/cotizaciones", "/proyectos/{pid}/cotizaciones/{cid}",
                          "/proyectos/{pid}/cotizaciones/{cid}/drive",
                          # 16-oct-2026, pieza 2 de la IA de las cotizaciones (no suben nada a Drive)
                          "/proyectos/{pid}/cotizaciones/ia/redaccion", "/proyectos/{pid}/cotizaciones/ia/borrador"}
    assert all(rutas.values()), rutas


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 12. La migración, el esquema y la lectura
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

_COLUMNAS_DE_DRIVE = {"drive_archivo_id": "text", "drive_version": "timestamptz", "drive_error": "text"}


def test_la_migracion_de_drive_agrega_las_tres_columnas_y_la_tabla_y_el_esquema_dice_lo_mismo():
    import test_base_m2 as b2
    texto = tt._texto_migracion_de_drive()
    sin_comentarios = db._sin_comentarios(texto)
    agregadas = {c.lower(): t.lower() for c, t in re.findall(
        r"ALTER\s+TABLE\s+cotizaciones\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)", sin_comentarios, re.I)}
    assert agregadas == _COLUMNAS_DE_DRIVE
    assert tt._columnas_que_agregan_otras_migraciones("cotizaciones") == set(_COLUMNAS_DE_DRIVE)
    esquema = tt._ESQUEMA
    for c in _COLUMNAS_DE_DRIVE:
        assert re.search(rf"^\s+{c}\s", b2._ddl(esquema, "cotizaciones")[0], re.M), c
    # (`carpeta_facturas_id` la agrega otra migración, la 2026-10-21 de las facturas: el esquema la declara dentro del
    # `CREATE TABLE` y la migración que crea la tabla, no; ver `tests/test_facturas_drive.py`)
    otras = tt._columnas_que_agregan_otras_migraciones_a("drive_de_proyectos", tt._migracion_de_drive())
    assert otras == {"carpeta_facturas_id"}
    assert [b2._normal(s).replace("if not exists ", "") for s in b2._ddl(texto, "drive_de_proyectos")] == \
        [b2._normal(tt._sin_columnas(s, otras)) for s in b2._ddl(esquema, "drive_de_proyectos")] != []
    # idempotente: toda sentencia que crea dice IF NOT EXISTS; y no escribe ni una fila ni borra nada
    for s in g._sentencias(texto):
        if re.match(r"ALTER\s+TABLE", s, re.I):
            assert re.search(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", s, re.I), s
        elif re.match(r"CREATE\s+TABLE", s, re.I):
            assert re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", s, re.I), s
        else:
            assert re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I), s


def test_la_migracion_de_drive_corre_sobre_la_de_las_cotizaciones_y_deja_lo_de_hoy_sin_drive():
    import sqlite3
    con = tt._base_con_migracion()
    tt._insertar(con, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00", numero=5)
    for s in g._sentencias(tt._texto_migracion_de_drive()):
        if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
            con.execute(tt._a_sqlite(s))
    fila = con.execute("SELECT drive_archivo_id, drive_version, drive_error FROM cotizaciones").fetchone()
    assert fila == (None, None, None)
    assert con.execute("SELECT count(*) FROM drive_de_proyectos").fetchone()[0] == 0
    assert cd.estado_de_drive({"estado": "enviada", "cambiado_en": "x", "drive_archivo_id": fila[0],
                               "drive_version": fila[1], "drive_error": fila[2]})["etiqueta"] == "No está en Drive"
    # la llave foránea: una carpeta de un proyecto que no existe no entra
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO drive_de_proyectos (proyecto_id, carpeta_proyecto_id, carpeta_cotizaciones_id, "
                    "nombre_puesto) VALUES (999, 'a', 'b', 'c')")


def test_las_lecturas_traen_las_tres_de_drive_y_sin_las_columnas_o_la_tabla_la_pagina_carga_sin_el_bloque(m, nube, monkeypatch):
    _enviar()
    lectura = asyncio.run(db.cotizaciones_de_proyectos())[1][0]
    assert {"drive_archivo_id", "drive_version", "drive_error"} <= set(lectura)
    una = asyncio.run(db.cotizacion_para_drive(1, 1))
    assert una["proyecto_nombre"] == "Disco de prueba" and una["codigo"] == "COT-2026-001" and una["drive_archivo_id"]
    assert asyncio.run(db.cotizacion_para_drive(3, 1)) is None and asyncio.run(db.cotizacion_para_drive(1, 99)) is None

    class _FaltaColumna(Exception):
        sqlstate = "42703"
    original = g._Cur.execute

    async def sin_la_columna(self, sql, params=()):
        if "drive_archivo_id" in sql:
            raise _FaltaColumna()
        return await original(self, sql, params)
    monkeypatch.setattr(g._Cur, "execute", sin_la_columna)
    assert asyncio.run(db.cotizaciones_de_proyectos()) is None
    assert asyncio.run(db.cotizacion_para_drive(1, 1)) is None
    html = _casa().get("/proyectos", params={"p": 1}).text
    assert 'id="cotizaciones"' not in html


def test_humo_corre_las_lecturas_de_drive_contra_la_base_de_verdad():
    """`tools/humo.py` tiene en su lista de pruebas (la que corre de verdad) la que lee las tres columnas de Drive y la
    tabla nueva sin tolerancia, y esa función las nombra."""
    arbol = ast.parse((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"))
    en_la_lista = [e.elts[1].id for n in ast.walk(arbol) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "pruebas" for t in n.targets)
                   for e in n.value.elts if isinstance(e, ast.Tuple) and isinstance(e.elts[1], ast.Name)]
    assert "_tablas_de_drive_de_las_cotizaciones" in en_la_lista, en_la_lista
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_tablas_de_drive_de_las_cotizaciones")
    cuerpo = ast.get_source_segment((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"), funcion)
    assert "drive_archivo_id" in cuerpo and "drive_version" in cuerpo and "drive_error" in cuerpo \
        and "drive_de_proyectos" in cuerpo


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# «Crear de nuevo la carpeta en Drive» (§3.7)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _borrar_en_drive(nube, ident):
    """Alguien borra a mano una carpeta (o archivo) y todo lo que tenía dentro."""
    for hijo in [i for i, c in nube.cosas.items() if c["padre"] == ident]:
        _borrar_en_drive(nube, hijo)
    nube.cosas.pop(ident, None)


def _con_la_carpeta_muerta(m, nube, que="proyecto"):
    """Dos enviadas subidas; se borra la carpeta (`proyecto` entera, o solo `cotizaciones`); se corrige la 2 y queda con
    la causa de la carpeta que ya no está."""
    _enviar()
    _guardar("enviar")
    _volver_a_subir(cid=2)
    viejas = _carpetas(m)
    _borrar_en_drive(nube, viejas["carpeta_proyecto_id" if que == "proyecto" else "carpeta_cotizaciones_id"])
    _corregir(cid=2)
    # (con archivo guardado también: el 404 del archivo suelta el id y el intento sigue hasta ver que la carpeta no está)
    assert _fila(m, 2)["drive_error"] == cd.SIN_LA_CARPETA_DEL_PROYECTO
    return viejas


def _rehacer(c=None, proyecto=1, cot=2):
    c = c or _casa()
    r = c.post(f"/proyectos/{proyecto}/drive/carpeta", data={"cotizacion": str(cot)}, follow_redirects=False)
    return r, (c.get(r.headers["location"].split("#")[0]).text if r.status_code == 303 else None)


def test_con_la_carpeta_muerta_la_hoja_ofrece_crearla_de_nuevo_solo_a_la_casa_y_solo_con_esa_causa(m, nube):
    _con_la_carpeta_muerta(m, nube)
    hoja = _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1, "cotizacion": 2}).text)
    assert 'action="/proyectos/1/drive/carpeta"' in hoja and "Crear de nuevo la carpeta en Drive" in hoja
    assert "Crear de nuevo" not in _seccion_de_cotizaciones(_ver().get("/proyectos", params={"p": 1, "cotizacion": 2}).text)
    # con otra causa, o ya en Drive, no sale
    m.con.execute("UPDATE cotizaciones SET drive_error = ? WHERE id = 2", (drive.DriveNoContesta.causa,))
    m.con.commit()
    assert "Crear de nuevo" not in _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1, "cotizacion": 2}).text)
    # la lista no lo ofrece en ningún caso (solo la hoja abierta)
    assert "Crear de nuevo" not in _seccion_de_cotizaciones(_casa().get("/proyectos", params={"p": 1}).text)


def test_crear_de_nuevo_la_carpeta_rehace_las_dos_anota_las_nuevas_y_suelta_los_archivos_viejos(m, nube):
    viejas = _con_la_carpeta_muerta(m, nube)
    r, html = _rehacer()
    assert r.headers["location"] == "/proyectos?hecho=cotizacion_carpeta&p=1&cotizacion=2#cotizaciones"
    nuevas = _carpetas(m)
    assert nuevas["carpeta_proyecto_id"] != viejas["carpeta_proyecto_id"] and nuevas["carpeta_cotizaciones_id"] != viejas["carpeta_cotizaciones_id"]
    assert nube.cosas[nuevas["carpeta_proyecto_id"]]["nombre"] == "Disco de prueba" == nuevas["nombre_puesto"]
    assert nube.cosas[nuevas["carpeta_cotizaciones_id"]]["padre"] == nuevas["carpeta_proyecto_id"]
    # las dos cotizaciones quedan «No está en Drive», sin causa, y la hoja lo dice
    for cid in (1, 2):
        f = _fila(m, cid)
        assert (f["drive_archivo_id"], f["drive_version"], f["drive_error"]) == (None, None, None)
    assert "Se crearon de nuevo la carpeta del proyecto y su subcarpeta «cotizaciones»." in html
    assert "sigue apuntando a la carpeta vieja" not in html           # la carpeta escrita a mano de este proyecto no es la vieja de Drive
    assert _afirmaciones_de_estar_en_drive(_seccion_de_cotizaciones(html).split('class="cz-ventana"')[1]) == []
    assert "Crear de nuevo" not in _seccion_de_cotizaciones(html)
    # y se suben de nuevo con «Volver a subir», sin otra lógica: caen en la carpeta nueva y quedan en Drive
    assert nube.archivos() == []
    for cid in (1, 2):
        _volver_a_subir(cid=cid)
        assert _fila(m, cid)["drive_version"] == _fila(m, cid)["cambiado_en"]
    assert {nube.cosas[i]["padre"] for i in nube.archivos()} == {nuevas["carpeta_cotizaciones_id"]}
    assert len(nube.archivos()) == 2


def test_el_aviso_dice_si_el_enlace_abrir_la_carpeta_quedo_apuntando_a_la_vieja_y_no_lo_cambia(m, nube):
    viejas = _con_la_carpeta_muerta(m, nube)
    vieja = cd.enlace_de_carpeta_de_drive(viejas["carpeta_proyecto_id"])
    m.con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 1", (vieja,))
    m.con.commit()
    r, html = _rehacer()
    assert r.headers["location"] == "/proyectos?hecho=cotizacion_carpeta_enlace&p=1&cotizacion=2#cotizaciones"
    assert "Se crearon de nuevo la carpeta del proyecto y su subcarpeta «cotizaciones»." in html
    assert "El enlace «Abrir la carpeta» del proyecto sigue apuntando a la carpeta vieja" in html
    assert _filas(m, "SELECT carpeta FROM proyectos WHERE id = 1")[0]["carpeta"] == vieja      # NO se cambia


def test_si_solo_falta_la_subcarpeta_el_aviso_lo_dice_y_no_habla_de_la_carpeta_del_proyecto(m, nube):
    _con_la_carpeta_muerta(m, nube, que="cotizaciones")
    r, html = _rehacer()
    assert r.headers["location"] == "/proyectos?hecho=cotizacion_carpeta_subcarpeta&p=1&cotizacion=2#cotizaciones"
    assert "Se creó de nuevo solo la subcarpeta «cotizaciones»: la carpeta del proyecto sigue siendo la misma." in html
    assert "Se crearon de nuevo la carpeta del proyecto" not in html and "sigue apuntando" not in html


def test_si_solo_falta_la_subcarpeta_cotizaciones_se_rehace_esa_y_el_proyecto_conserva_su_carpeta(m, nube):
    viejas = _con_la_carpeta_muerta(m, nube, que="cotizaciones")
    _rehacer()
    nuevas = _carpetas(m)
    assert nuevas["carpeta_proyecto_id"] == viejas["carpeta_proyecto_id"]
    assert nuevas["carpeta_cotizaciones_id"] != viejas["carpeta_cotizaciones_id"]
    assert nube.carpetas_de_proyectos() == [viejas["carpeta_proyecto_id"]]
    assert _fila(m, 2)["drive_archivo_id"] is None


def test_si_la_carpeta_todavia_esta_en_drive_no_se_crea_otra_ni_se_suelta_nada(m, nube):
    _enviar()
    carpetas = _carpetas(m)
    m.con.execute("UPDATE cotizaciones SET drive_error = ? WHERE id = 1", (cd.SIN_LA_CARPETA_DEL_PROYECTO,))
    m.con.commit()
    antes = _fila(m)
    nube.pasos.clear()
    r, html = _rehacer(cot=1)
    assert r.headers["location"] == "/proyectos?error=cotizacion_carpeta_sigue&p=1&cotizacion=1#cotizaciones"
    assert "La carpeta todavía existe: no se creó otra." in html
    assert not any(p.startswith("crear_carpeta") for p, _ in nube.pasos) and nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]]
    assert _carpetas(m) == carpetas and _fila(m) == antes


def test_sin_ninguna_cotizacion_que_diga_que_la_carpeta_falta_no_hace_nada(m, nube):
    _enviar()
    nube.google.pedidos.clear()
    r, html = _rehacer(cot=1)
    assert "error=cotizacion_carpeta_sin_causa" in r.headers["location"] and nube.google.pedidos == []
    r, _ = _rehacer(proyecto=3, cot=1)                                   # un proyecto sin cotizaciones
    assert "error=cotizacion_carpeta_sin_causa" in r.headers["location"] and nube.google.pedidos == []


def test_solo_ver_y_sin_sesion_no_rehacen_la_carpeta(m, nube):
    _con_la_carpeta_muerta(m, nube)
    antes, pedidos = _carpetas(m), len(nube.google.pedidos)
    for cliente in (_ver(), TestClient(__import__("web.app", fromlist=["app"]).app, base_url="https://testserver")):
        assert cliente.post("/proyectos/1/drive/carpeta", data={"cotizacion": "2"}, follow_redirects=False).status_code == 401
    assert _carpetas(m) == antes and len(nube.google.pedidos) == pedidos


def test_la_carpeta_nueva_sigue_la_regla_del_nombre_repetido(m, nube):
    _con_la_carpeta_muerta(m, nube)
    otro = nube._nuevo("CARPETA")                      # otra carpeta de Lucy con ese nombre en «Proyectos»
    nube.cosas[otro] = {"tipo": "carpeta", "nombre": "Disco de prueba", "padre": CARPETA_PROYECTOS}
    _rehacer()
    assert _carpetas(m)["nombre_puesto"] == "Disco de prueba · #1"
    assert nube.cosas[_carpetas(m)["carpeta_proyecto_id"]]["nombre"] == "Disco de prueba · #1"


def test_si_drive_falla_al_rehacer_no_se_anota_nada_ni_se_sueltan_los_archivos(m, nube):
    _con_la_carpeta_muerta(m, nube)
    antes = (_carpetas(m), _fila(m, 1), _fila(m, 2))
    nube.falla = lambda paso, p: _R(500, json={}) if paso == "crear_carpeta" else None
    r, html = _rehacer()
    assert "error=cotizacion_carpeta_fallo" in r.headers["location"] and "No se pudo crear la carpeta de nuevo" in html
    assert (_carpetas(m), _fila(m, 1), _fila(m, 2)) == antes


def test_rehacer_las_carpetas_en_la_base_es_atomico_y_respeta_el_candado_de_cada_cotizacion(m, nube):
    _enviar()
    _guardar("enviar")
    _volver_a_subir(cid=2)
    c1, c2 = _fila(m, 1), _fila(m, 2)
    viejas = _carpetas(m)
    # otras carpetas anotadas que no son las que se creían: no se escribe nada
    assert asyncio.run(db.rehacer_carpetas_de_drive(1, "OTRA", "N1", "N2", "x", [(1, c1["cambiado_en"])])) is False
    assert _carpetas(m) == viejas and _fila(m, 1) == c1
    # la 2 cambió en medio (otra versión): no se suelta; la 1 sí
    ok = asyncio.run(db.rehacer_carpetas_de_drive(
        1, viejas["carpeta_cotizaciones_id"], "N1", "N2", "x", [(1, c1["cambiado_en"]), (2, "una versión que ya no es")]))
    assert ok is True and _carpetas(m)["carpeta_cotizaciones_id"] == "N2"
    assert _fila(m, 1)["drive_archivo_id"] is None and _fila(m, 2) == c2


def test_si_alguien_corrige_mientras_se_sube_y_drive_falla_la_version_nueva_no_recibe_el_error(m, nube):
    """Drive falla y, justo antes, otra persona corrigió: el error es de la versión vieja y no se escribe sobre la nueva."""
    _guardar("enviar")

    def falla_y_otro_corrige(paso, p):
        if paso == "subir_nuevo":
            m.con.execute("UPDATE cotizaciones SET cambiado_en = '2026-10-15 00:00:00.000001+00:00' WHERE id = 1")
            return _R(500, json={})
        return None
    nube.falla = falla_y_otro_corrige
    assert asyncio.run(cd.subir(1, 1)) == "fallo"
    fila = _fila(m)
    assert fila["cambiado_en"] == "2026-10-15 00:00:00.000001+00:00"
    assert fila["drive_error"] is None and fila["drive_archivo_id"] is None and fila["drive_version"] is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# Quién escribe lo de Drive, y cómo se lee
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _textos(arbol):
    return [n for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_drive_version_y_drive_archivo_id_se_escriben_en_un_solo_sitio_y_con_el_candado():
    """Fuera de `tests/`, el único texto de SQL que pone `drive_version`/`drive_archivo_id` es la sentencia de marcar; la
    única función que la ejecuta es `db.marcar_cotizacion_en_drive`; y la única que la llama es `cotizaciones_drive.subir`."""
    escritores = []
    llamadores = []
    for ruta in buzon._py_en_disco(_ROOT):
        rel = ruta.relative_to(_ROOT).as_posix()
        if rel.startswith("tests/") or ruta.name == "conftest.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for n in _textos(arbol):
            # (las facturas tienen su propio `drive_archivo_id`, pieza 8 de «Facturas»: su guarda es
            # `tests/test_guardar_factura.py` y `tests/test_facturas_drive.py`; aquí se mira SOLO `cotizaciones`)
            if re.search(r"\bSET\b[^;]*\bdrive_(version|archivo_id)\s*=", n.value, re.I | re.S) \
                    and not re.match(r"\s*UPDATE\s+facturas\b", n.value, re.I):
                escritores.append((rel, n.value))
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "marcar_cotizacion_en_drive":
                llamadores.append(rel)
    # dos sentencias lo ponen: marcar (Drive contestó) y soltar (se rehízo la carpeta y los archivos dejaron de valer)
    assert [e[0] for e in escritores] == ["db/db.py", "db/db.py"]
    assert sorted(e[1] for e in escritores) == sorted((db.SENTENCIA_DE_MARCAR_EN_DRIVE, db.SENTENCIA_DE_SOLTAR_DRIVE))
    for sentencia in (db.SENTENCIA_DE_MARCAR_EN_DRIVE, db.SENTENCIA_DE_SOLTAR_DRIVE):
        assert re.search(r"\bcambiado_en\s*=\s*%s", sentencia)
    assert llamadores == ["cotizaciones_drive.py"]


def test_la_guarda_de_quien_escribe_drive_ve_lo_inventado(tmp_path):
    """Entradas inventadas: la misma regla sobre un archivo que escribe `drive_version` fuera de `db/db.py`."""
    (tmp_path / "otro.py").write_text('X = "UPDATE cotizaciones SET drive_version = %s WHERE id = %s"\n', encoding="utf-8")
    arbol = ast.parse((tmp_path / "otro.py").read_text(encoding="utf-8"))
    assert [n.value for n in _textos(arbol) if re.search(r"\bSET\b[^;]*\bdrive_(version|archivo_id)\s*=", n.value, re.I | re.S)]


def test_la_unica_forma_de_decir_que_esta_en_drive_es_estado_de_drive():
    """La plantilla no escribe «En Drive» ni «Guardada en Lucy y en Drive» a mano: esas frases salen de
    `cotizaciones_drive` (una sola puerta), y `app.py` solo las pasa."""
    for ruta in ("web/plantillas/_cotizaciones_del_proyecto.html", "web/app.py"):
        texto = (_ROOT / ruta).read_text(encoding="utf-8")
        texto = texto.replace("Abrir en Drive", "").replace("No está en Drive", "").replace(
            "Crear de nuevo la carpeta en Drive", "")
        hallados = [l.strip() for l in texto.splitlines() if re.search(r"\ben Drive\b|Subida a Drive", l, re.I)
                    and not l.strip().startswith(("#", '"""'))]
        # en `app.py` solo comentarios y docstrings pueden nombrarlo; la plantilla, ninguna línea
        if ruta.endswith(".html"):
            assert hallados == [], hallados
        else:
            codigo = ast.parse(texto)
            frases = [n.value for n in _textos(codigo) if re.search(r"\ben Drive\b", n.value, re.I) and "\n" not in n.value
                      and len(n.value) < 120]
            assert frases == [], frases
    assert cd.ETIQUETA_EN_DRIVE == "En Drive"
