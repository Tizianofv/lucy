"""La lista y la ventana de las cotizaciones en la página de Proyectos (pieza 5 del diseño «Cotizaciones en
Proyectos», 14-oct-2026) y la marca «BORRADOR» del PDF de un borrador.

Se recorre la ruta REAL (`GET /proyectos`, `POST /proyectos/{pid}/cotizaciones[/{cid}]`, `GET …/pdf`) con la
plantilla REAL. La base es SQLite con el SQL REAL de `db.guardar_cotizacion` y `db.cotizaciones_de_proyectos`
sobre las tablas del esquema (`tests/test_base_m2.py::_base`); el modelo de la página sale de `db.armar_pagina`
de verdad. Qué se vigila:

  1. la lista sale en la casa y en solo ver, con y sin carpeta (y sin la columna de la carpeta), y en solo ver
     no trae ningún control: ni «+», ni «Corregir», ni formulario; lo que escribe da 401;
  2. los nombres de las casillas que pinta la plantilla son EXACTAMENTE los que lee la ruta: se arma el POST
     con lo que trae la página pintada (todas las casillas, también las de sobra) y se comprueba que todo lo
     escrito llegó a lo guardado; sin JavaScript se crea una cotización de 2 opciones y 2 renglones;
  3. el precio de la lista sale de los totales por opción: uno solo, o «RD$X a RD$Y» del menor al mayor;
  4. el sistema no dice «guardada» si no se guardó (ni aviso escrito a mano, ni con el estado que no es);
     lo rechazado redirige con la ventana abierta y no escribe nada ni gasta número; el candado de edición;
  5. los censos de `test_proyectos_solo_ver.py` y `test_pagina_sin_saltos.py` incluyen los controles nuevos;
  6. la marca «BORRADOR» sale en el PDF de un borrador (en cada página) y NO en el de una enviada;
  7. el guion de la ventana: las dos cuentas que hace en el navegador (dinero y fecha en palabras) dan lo que
     dan `cotizaciones.dinero_en_pesos` y `fecha_en_palabras` (corrido en JavaScriptCore).

FRONTERA, dicha una vez:
  · No es un navegador: el guion de la ventana (agregar renglón, quitar opción, rehacer la vista previa) solo
    se prueba en sus dos cuentas puras; lo demás lo mira la sala en el servidor local de prueba.
  · SQLite en vez de Postgres (ver `tests/test_guardar_cotizacion.py`); `now()` sube un microsegundo por llamada.
  · El teléfono sale de Noco: se dobla `noco_lectura.contacto` (lo que dice de la red lo prueba su archivo).

Correr:  python3 -m pytest tests/test_cotizaciones_en_la_pagina.py -q
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)
from pypdf import PdfReader

import test_guardar_cotizacion  # noqa: F401  (primero: deja puestas las variables de entorno que pide `config`)
from _navegador import Navegador, dar_recibo
import test_pagina_proyectos as tp
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)
import test_proyectos_solo_ver as sv
import acciones.crud  # noqa: F401
import config
import cotizaciones as cz
import db.db as db
import noco_lectura
import web.app as panel
import web.auth as auth

CASA = config.CHAT_ID_DUENO
TELEFONO_DE_NOCO = "809-555-0142"


# ── El mundo: SQLite con la página y las cotizaciones, el teléfono de Noco doblado ───────────────────

@pytest.fixture
def tabla_ausente_como_postgres(monkeypatch):
    """SQLite dice «no such table»; Postgres dice SQLSTATE 42P01, que es lo que la lectura y el guardado reconocen
    (`db._tabla_ausente`). Esto traduce lo primero a lo segundo y NADA más: el código de `db` corre tal cual."""
    original = tp.g._Cur.execute

    async def execute(self, sql, params=()):
        try:
            return await original(self, sql, params)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            raise _TablaAusente(str(e)) from None
    monkeypatch.setattr(tp.g._Cur, "execute", execute)


class _TablaAusente(Exception):
    sqlstate = "42P01"


@pytest.fixture
def m(mundo, monkeypatch):
    """`Mundo` de la página con un `now()` que sube un microsegundo por llamada (sin eso el candado `cambiado_en`
    nunca cambia) y el teléfono del cliente de Noco doblado. Proyecto 1 con cliente y carpeta; 2 sin cliente y
    sin carpeta; 3 otro."""
    contador = iter(range(1, 10 ** 7))
    inicio = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    mundo.con.create_function(
        "now", 0, lambda: (inicio + timedelta(microseconds=next(contador))).isoformat(sep=" ", timespec="microseconds"))

    async def _contacto(noco_id):
        return {"telefono": TELEFONO_DE_NOCO, "email": None}
    monkeypatch.setattr(noco_lectura, "contacto", _contacto)
    mundo.proyecto(1, "Disco de prueba", area="CDS", cliente="Cliente de la ficha", responsable=CASA)
    mundo.proyecto(2, "Sin cliente ni carpeta", area="CDS", responsable=CASA)
    mundo.proyecto(3, "Otro proyecto", area="CDS", responsable=CASA, cliente="Otro cliente")
    mundo.con.execute("UPDATE proyectos SET carpeta = 'https://drive.example.test/carpetas/uno' WHERE id = 1")
    mundo.con.commit()
    return mundo


def _contenido(opciones=2, renglones=1, totales=None):
    """Lo que la ventana mandaría (la puerta de `db` lo valida): `opciones` opciones, cada una con `renglones`."""
    totales = totales or [1000 * (i + 1) for i in range(opciones)]
    return {"opciones": [{"nombre": f"Opción de prueba {i + 1}", "renglones": [
                {"descripcion": f"Trabajo {i + 1}.{k + 1}", "detalle": "", "total": str(totales[i] // renglones)}
                for k in range(renglones)]} for i in range(opciones)],
            "condiciones": [{"clave": "validez", "texto": "x", "marcada": True},
                            {"clave": "deposito", "texto": "Un depósito.", "marcada": True}]}


def _guardar(accion="borrador", proyecto=1, contenido=None, **kw):
    return asyncio.run(db.guardar_cotizacion(proyecto, CASA, accion, contenido or _contenido(), 15, **kw))


def _filas(m_, sql="SELECT * FROM cotizaciones ORDER BY id"):
    cur = m_.con.execute(sql)
    return [dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()]


def _casa() -> Navegador:
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(CASA, auth.VIDA_SESION))
    return c


def _ver() -> Navegador:
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def _pagina(cliente: Navegador, con_recibo=False, **consulta) -> str:
    if con_recibo:
        dar_recibo("/proyectos", **consulta)
    r = cliente.get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _seccion_de_cotizaciones(html: str) -> str:
    desde = html.index('id="cotizaciones"')
    return html[desde:html.index("</section>", desde)]


# ── Lo que pinta la ventana, sacado del HTML ─────────────────────────────────────────────────────────

class _Formulario(HTMLParser):
    """Las casillas del `<form class="cz-form">` pintado, SIN lo que está dentro de un `<template>` (eso lo pone
    el botón «Agregar»): `(etiqueta, nombre, tipo, valor)` y los botones de envío."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.casillas: list[tuple] = []
        self.botones: list[tuple] = []
        self.opciones_de_validez: list[str] = []
        self._dentro = False
        self._plantilla = 0
        self._textarea = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form" and "cz-form" in a.get("class", "").split():
            self._dentro = True
            self.accion = a["action"]
        if tag == "template":
            self._plantilla += 1
        if not self._dentro or self._plantilla:
            return
        if tag == "input":
            self.casillas.append(("input", a.get("name"), a.get("type", "text"), a.get("value", ""), "checked" in a))
        elif tag == "textarea":
            self._textarea = [a.get("name"), ""]
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self.opciones_de_validez.append(a.get("value", ""))
        elif tag == "button" and a.get("name"):
            self.botones.append((a["name"], a.get("value", "")))

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "template":
            self._plantilla -= 1
        if tag == "textarea" and self._textarea is not None:
            self.casillas.append(("textarea", self._textarea[0], "textarea", self._textarea[1], False))
            self._textarea = None
        if tag == "select":
            self._select = None
        if tag == "form":
            self._dentro = False


def _formulario(html: str) -> _Formulario:
    f = _Formulario()
    f.feed(html)
    assert f.casillas, "la página no trae el formulario de la cotización"
    return f


def _post_de_la_pagina(f: _Formulario, accion: str, texto=lambda nombre: f"Texto-{nombre}"):
    """El POST que armaría un navegador SIN JavaScript con lo que la página pintó: cada casilla de texto lleva
    un valor propio (el que dé `texto(nombre)`), las casillas marcadas siguen marcadas y el botón apretado manda
    su `accion`. Los totales llevan un número distinto por casilla."""
    datos = {}
    totales = iter(range(11, 10 ** 4))
    for etiqueta, nombre, tipo, valor, marcada in f.casillas:
        if nombre is None:
            continue
        if tipo == "checkbox":
            if marcada:
                datos[nombre] = "on"
        elif tipo == "hidden" or nombre in ("validez", "fecha"):
            datos[nombre] = valor
        elif nombre.endswith("-total"):
            datos[nombre] = str(next(totales) * 100)
        elif nombre == "para-telefono" or nombre == "para-nombre":
            datos[nombre] = valor or texto(nombre)
        else:
            datos[nombre] = valor or texto(nombre)
    datos["validez"] = "15"
    datos["accion"] = accion
    return datos


def _precio(html: str, codigo: str) -> str:
    """El renglón de la lista de ese código: lo que dice su línea de detalle («fecha · N opciones · precio»)."""
    fila = re.search(r"<li>.*?<b>%s</b>.*?<span class=\"sub\">(.*?)</span>" % re.escape(codigo), html, re.S)
    assert fila, (codigo, "no está en la lista")
    return fila.group(1)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. La lista: casa y solo ver, con y sin carpeta; en solo ver, ningún control
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_la_lista_sale_en_la_casa_con_su_mas_y_con_los_datos_de_cada_una(m):
    borrador = _guardar("borrador", contenido=_contenido(2, totales=[1000, 3500]))
    enviada = _guardar("enviar", contenido=_contenido(1, totales=[1000]))
    html = _pagina(_casa(), p=1)
    seccion = _seccion_de_cotizaciones(html)
    assert "<h3>Cotizaciones</h3>" in seccion and 'href="/proyectos?p=1&amp;cotizacion=nueva#cotizaciones"' in seccion
    assert borrador["codigo"] == "COT-2026-001" and enviada["codigo"] == "COT-2026-002"
    assert seccion.index("COT-2026-002") < seccion.index("COT-2026-001")                   # la más nueva arriba
    assert "Enviada" in seccion and "Borrador" in seccion
    assert f'href="/proyectos/1/cotizaciones/{borrador["id"]}/pdf" download' in seccion     # se baja con `download`
    assert f'href="/proyectos?p=1&amp;cotizacion={borrador["id"]}#cotizaciones"' in seccion  # y se abre


def test_sin_ninguna_la_casa_dice_que_todavia_no_hay_y_ofrece_el_mas(m):
    seccion = _seccion_de_cotizaciones(_pagina(_casa(), p=1))
    assert "Todavía no hay cotizaciones." in seccion and "cotizacion=nueva" in seccion


@pytest.mark.parametrize("sesion", ["casa", "ver"])
def test_la_lista_sale_con_carpeta_y_sin_ella(m, sesion):
    """Proyecto 1: con carpeta. Proyecto 2: la columna existe pero no hay carpeta. En los dos la lista sale, en la
    casa y en solo ver (que además no ve «Cambiar» ni «Poner»)."""
    cliente = _casa if sesion == "casa" else _ver
    _guardar(proyecto=1)
    _guardar(proyecto=2, accion="enviar", para_nombre="Cliente del dos")
    con_carpeta = _pagina(cliente(), p=1)
    sin_carpeta = _pagina(cliente(), p=2)
    assert "Abrir la carpeta" in con_carpeta and "Abrir la carpeta" not in sin_carpeta
    for html in (con_carpeta, sin_carpeta):
        assert "COT-2026-00" in _seccion_de_cotizaciones(html)
    if sesion == "ver":
        assert ">Cambiar</a>" not in con_carpeta and ">Poner</a>" not in sin_carpeta
    else:
        assert "Todavía no hay carpeta." in sin_carpeta


def test_en_solo_ver_la_seccion_no_sale_si_no_hay_carpeta_ni_cotizaciones_y_en_la_casa_si_(m):
    ver_sin_nada = _pagina(_ver(), p=2)
    assert 'id="cotizaciones"' not in ver_sin_nada and "carpeta-p" not in ver_sin_nada.split("</style>", 1)[1]
    assert 'id="cotizaciones"' in _pagina(_casa(), p=2)             # la casa siempre tiene su «+»


def test_sin_la_columna_de_la_carpeta_las_cotizaciones_salen_igual_y_la_carpeta_no(m):
    _guardar(proyecto=1)
    guardada, guardada_audio = panel.db.carpetas_de_proyectos, panel.db.carpetas_de_audio

    async def _sin_columna():
        return None                       # como SQLSTATE 42703 (la columna no existe): `carpeta_disponible` falso
    # Una base de antes de las dos migraciones (la de la carpeta y la de la carpeta de audio, pieza 4): ninguna de las
    # dos líneas sale. (La de audio sola sin migrar la vigila `tests/test_carpeta_de_audio.py`.)
    panel.db.carpetas_de_proyectos = _sin_columna
    panel.db.carpetas_de_audio = _sin_columna
    try:
        html = _pagina(_casa(), p=1)
    finally:
        panel.db.carpetas_de_proyectos, panel.db.carpetas_de_audio = guardada, guardada_audio
    assert "Carpetas del proyecto" not in html and "COT-2026-001" in _seccion_de_cotizaciones(html)
    assert "<h2>Cotizaciones</h2>" in html


def test_sin_la_tabla_la_pagina_carga_y_no_sale_nada_de_cotizaciones(m, tabla_ausente_como_postgres):
    m.con.execute("DROP TABLE cotizaciones")
    m.con.commit()
    for cliente in (_casa(), _ver()):
        html = _pagina(cliente, p=1)
        assert 'id="cotizaciones"' not in html and "Cotizaciones" not in html.split("</style>", 1)[1]


def test_en_solo_ver_la_lista_se_ve_sin_ningun_control_y_la_ventana_que_escribe_no_se_arma(m):
    _guardar("borrador", contenido=_contenido(2))
    enviada = _guardar("enviar", contenido=_contenido(1))
    for consulta in ({"p": 1}, {"p": 1, "cotizacion": "nueva"}, {"p": 1, "cotizacion": enviada["id"], "corregir": 1},
                     {"p": 1, "cotizacion": 1, "corregir": 1}):
        html = _pagina(_ver(), **consulta)
        seccion = _seccion_de_cotizaciones(html)
        assert "cotizacion=nueva" not in seccion and "cotiz-mas" not in seccion          # ni «+»
        assert "<form" not in seccion and "<button" not in seccion and "<input" not in seccion
        assert "Corregir" not in seccion and "corregir=1" not in seccion
        assert 'class="cz-form"' not in html and "guion-cotizaciones" not in html        # ni el guion de la ventana
        assert sv.controles_que_sobran(html) == [], consulta
    # …pero ve la lista, abre una (su hoja) y baja el PDF
    abierta = _pagina(_ver(), p=1, cotizacion=enviada["id"])
    assert "COTIZACIÓN DE SERVICIOS" in abierta and "Descargar PDF" in abierta
    assert f'/proyectos/1/cotizaciones/{enviada["id"]}/pdf' in abierta
    assert _ver().get(f"/proyectos/1/cotizaciones/{enviada['id']}/pdf").status_code == 200


def test_solo_ver_y_sin_sesion_no_guardan_ni_gastan_numero(m):
    antes = (len(_filas(m)), len(_filas(m, "SELECT * FROM serie_cotizaciones")))
    for ruta in ("/proyectos/1/cotizaciones", "/proyectos/1/cotizaciones/1"):
        for cliente in (_ver(), Navegador(panel.app, base_url="https://testserver")):
            r = cliente.post(ruta, data={"accion": "enviar", "para-nombre": "X"}, follow_redirects=False)
            assert r.status_code == 401, (ruta, r.status_code)
    assert (len(_filas(m)), len(_filas(m, "SELECT * FROM serie_cotizaciones"))) == antes


def test_la_casa_ve_el_corregir_de_una_enviada_y_solo_ella(m):
    enviada = _guardar("enviar")
    casa = _pagina(_casa(), p=1, cotizacion=enviada["id"])
    assert f'cotizacion={enviada["id"]}&amp;corregir=1' in casa and "Corregir" in casa
    corregir = _pagina(_casa(), p=1, cotizacion=enviada["id"], corregir=1)
    assert 'class="cz-form"' in corregir
    pie = corregir.split('class="ventana-pie"', 1)[1].split("</form>", 1)[0]
    assert "Guardar y descargar PDF" in pie and "Guardar borrador" not in pie     # una enviada no vuelve a borrador


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Los nombres de las casillas son los que lee la ruta (con lo que la página pintó)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_la_ventana_nueva_trae_al_cliente_su_telefono_hoy_y_el_numero_que_toca(m):
    html = _pagina(_casa(), p=1, cotizacion="nueva")
    f = _formulario(html)
    por_nombre = {n: v for _, n, _, v, _ in f.casillas}
    assert por_nombre["para-nombre"] == "Cliente de la ficha" and por_nombre["para-telefono"] == TELEFONO_DE_NOCO
    assert por_nombre["fecha"] == "2026-10-10" and f.opciones_de_validez == ["7", "15", "30"]
    assert "COT-2026-001 · número automático" in html           # serie vacía: la primera es la 001
    _guardar()
    assert "COT-2026-002 · número automático" in _pagina(_casa(), p=1, cotizacion="nueva")
    assert [("accion", "borrador"), ("accion", "enviar")] == f.botones


def test_si_noco_no_contesta_el_telefono_queda_en_blanco_y_la_ventana_abre(m, monkeypatch):
    async def _cae(noco_id):
        raise noco_lectura.NocoNoContesta("sin red")
    monkeypatch.setattr(noco_lectura, "contacto", _cae)
    por_nombre = {n: v for _, n, _, v, _ in _formulario(_pagina(_casa(), p=1, cotizacion="nueva")).casillas}
    assert por_nombre["para-telefono"] == "" and por_nombre["para-nombre"] == "Cliente de la ficha"


def test_sin_javascript_se_crea_una_de_dos_opciones_y_dos_renglones_y_todo_lo_que_la_pagina_pinta_se_lee(m):
    """Se arma el POST con TODAS las casillas que la página pintó (también las de sobra, que con JavaScript se
    esconden) y un valor propio en cada una. Todo lo escrito tiene que estar en lo guardado: una casilla que la
    ruta no lee (porque la plantilla la nombra distinto) se pierde y esta prueba cae."""
    f = _formulario(_pagina(_casa(), p=1, cotizacion="nueva"))
    nombres = [n for _, n, _, _, _ in f.casillas]
    # 2 opciones y 2 renglones en cada una, sin un solo botón «Agregar» apretado
    assert {"op-0-nombre", "op-1-nombre", "op-0-r-0-total", "op-0-r-1-total", "op-1-r-0-total", "op-1-r-1-total"} <= set(nombres)
    datos = _post_de_la_pagina(f, "enviar")
    r = _casa().post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "hecho=cotizacion_enviada" in r.headers["location"], r.headers["location"]
    guardada = _filas(m)
    assert len(guardada) == 1 and guardada[0]["estado"] == "enviada" and guardada[0]["numero"] == 1
    contenido = json.loads(guardada[0]["contenido"])
    assert [len(o["renglones"]) for o in contenido["opciones"]] == [2, 2]
    # cada casilla de texto de la página llegó a lo guardado
    todo = guardada[0]["contenido"] + "|" + (guardada[0]["para_nombre"] or "") + "|" + (guardada[0]["para_telefono"] or "")
    for _, nombre, tipo, valor, _ in f.casillas:
        if nombre is None:
            continue           # sin `name` no viaja en el POST: la casilla de la IA («Lo que pidió el cliente»), probada en `test_cotizaciones_ia_en_la_ventana.py`
        if tipo == "text" or nombre in ("para-nombre", "para-telefono") or tipo == "textarea":
            if nombre.endswith("-total"):
                continue                                          # el dinero se guarda ya limpio: se mira abajo
            esperado = datos[nombre]
            assert esperado in todo, (nombre, esperado)
    guardados = [r_["total"] for o in contenido["opciones"] for r_ in o["renglones"]]
    enviados = sorted(Decimal(v) for k, v in datos.items() if k.endswith("-total"))
    assert len(enviados) == 4 and sorted(Decimal(t) for t in guardados) == enviados          # los cuatro totales llegaron


def test_cada_casilla_de_la_ventana_es_leida_por_la_ruta_aunque_se_corrija_una_enviada(m):
    """La de «Corregir» y la de un borrador salen del MISMO formulario: lo escrito en cada casilla llega."""
    enviada = _guardar("enviar", contenido=_contenido(2, 2, totales=[2000, 4000]))
    f = _formulario(_pagina(_casa(), p=1, cotizacion=enviada["id"], corregir=1))
    datos = _post_de_la_pagina(f, "enviar", texto=lambda n: f"Cambiado-{n}")
    datos["para-nombre"] = "Cliente corregido"
    r = _casa().post(f"/proyectos/1/cotizaciones/{enviada['id']}", data=datos, follow_redirects=False)
    assert r.status_code == 303 and "hecho=cotizacion_enviada" in r.headers["location"]
    fila = _filas(m)[0]
    assert fila["para_nombre"] == "Cliente corregido" and fila["numero"] == 1 and fila["estado"] == "enviada"
    assert "Cambiado-op-0-nombre" in fila["contenido"] or "Opción de prueba 1" in fila["contenido"]


def test_lo_que_la_pagina_pinta_para_las_condiciones_llega_marcado_o_no(m):
    f = _formulario(_pagina(_casa(), p=1, cotizacion="nueva"))
    nombres = {n for _, n, t, _, _ in f.casillas if t == "checkbox"}
    assert {f"cond-{c}" for c in db.CLAVES_DE_CONDICION_DE_COTIZACION} <= nombres
    datos = _post_de_la_pagina(f, "borrador")
    del datos["cond-balance"]                                    # desmarcada: no sale
    datos["cond-extra-0-texto"] = "Una condición propia"
    datos["cond-extra-0-marcada"] = "on"
    _casa().post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    cond = {(c["clave"], c["texto"]): c["marcada"] for c in json.loads(_filas(m)[0]["contenido"])["condiciones"]}
    assert cond[("balance", cz.texto_de_condicion("balance", 15, 2))] is False
    assert cond[("impuestos", cz.texto_de_condicion("impuestos", 15, 2))] is True
    assert cond[(None, "Una condición propia")] is True


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3. El precio de la lista sale de los totales por opción
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _con_totales(*por_opcion):
    """Una `cot` mínima con la forma de la lectura: `por_opcion` son listas de totales (los renglones de cada opción)."""
    return {"id": 1, "codigo": "COT-2026-001", "estado": "enviada", "fecha": "2026-10-14",
            "totales_por_opcion": [sum((Decimal(t) for t in ts), Decimal("0")) for ts in por_opcion]}


@pytest.mark.parametrize("opciones, esperado", [
    ([["1000"]], "RD$1,000"),                                                  # una sola opción: un precio
    ([["1000"], ["1000"]], "RD$1,000"),                                        # todas iguales: un precio
    ([["62500"], ["72500"], ["98500"]], "RD$62,500 a RD$98,500"),              # del menor al mayor
    ([["98500"], ["62500"], ["72500"]], "RD$62,500 a RD$98,500"),              # sin importar el orden
    ([["1500.50", "2000"], ["1000"]], "RD$1,000 a RD$3,500.50"),               # cada opción SUMA sus renglones
    ([["0.50"], ["12000.5"]], "RD$0.50 a RD$12,000.50"),
])
def test_el_rango_sale_de_los_totales_por_opcion(opciones, esperado):
    assert cz.resumen_de_cotizacion(_con_totales(*opciones))["precio"] == esperado


def test_la_lista_dice_cuantas_opciones_y_el_rango_de_lo_guardado_de_verdad(m):
    _guardar("borrador", contenido=_contenido(3, totales=[62500, 72500, 98500]))
    _guardar("enviar", contenido=_contenido(1, totales=[18000]))
    html = _pagina(_casa(), p=1)
    assert "3 opciones · RD$62,500 a RD$98,500" in _precio(html, "COT-2026-001").replace("\n", " ")
    assert "1 opción · RD$18,000" in _precio(html, "COT-2026-002")
    assert cz.resumen_de_cotizacion(_con_totales(["1"]))["fecha"] == "14 oct 2026"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 4. El sistema no dice «guardada» si no se guardó
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_guardar_borrador_y_enviar_dicen_lo_que_paso_y_lo_comprueban_al_pintar(m):
    f = _formulario(_pagina(_casa(), p=1, cotizacion="nueva"))
    datos = _post_de_la_pagina(f, "borrador")
    c = _casa()
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert destino == "/proyectos?hecho=cotizacion_borrador&p=1&cotizacion=1#cotizaciones"
    html = c.get(destino.split("#")[0]).text
    assert "Borrador COT-2026-001 guardado." in html and 'class="cz-form"' in html      # sigue abierto, con lo guardado
    assert [x["estado"] for x in _filas(m)] == ["borrador"]
    # …y lo mismo al enviar: aviso de «enviada», el enlace para bajar el PDF y la hoja
    f2 = _formulario(html)
    datos2 = _post_de_la_pagina(f2, "enviar")
    r2 = c.post("/proyectos/1/cotizaciones/1", data=datos2, follow_redirects=False)
    assert r2.headers["location"] == "/proyectos?hecho=cotizacion_enviada&p=1&cotizacion=1#cotizaciones"
    html2 = c.get(r2.headers["location"].split("#")[0]).text
    assert "Cotización COT-2026-001 guardada como enviada." in html2
    assert 'href="/proyectos/1/cotizaciones/1/pdf" download data-bajar-ya>Descargar el PDF</a>' in html2
    assert "COTIZACIÓN DE SERVICIOS" in html2 and [x["estado"] for x in _filas(m)] == ["enviada"]


def test_el_recibo_de_guardar_una_cotizacion_no_sirve_para_pedir_otra_ni_marca_su_pdf(m):
    """Por la ruta real: la 2 ya está enviada; se guarda la 1 como enviada (recibo y aviso de verdad). Pedir la página
    de la 2 con ese recibo no dice «guardada» ni lleva la marca que baja el PDF; la de la 1 sí."""
    _guardar("enviar")                                                    # la 1
    _guardar("enviar")                                                    # la 2, ya enviada
    c = _casa()
    r = c.post("/proyectos/1/cotizaciones/1", data=_post_de_la_pagina(_formulario(_pagina(c, p=1, cotizacion=1, corregir=1)), "enviar"),
               follow_redirects=False)
    assert r.headers["location"] == "/proyectos?hecho=cotizacion_enviada&p=1&cotizacion=1#cotizaciones"
    ajena = c.get("/proyectos?hecho=cotizacion_enviada&p=1&cotizacion=2").text
    assert "COT-2026-002 guardada como enviada" not in ajena and "download data-bajar-ya>" not in ajena
    suya = c.get("/proyectos?hecho=cotizacion_enviada&p=1&cotizacion=1").text         # el recibo seguía vivo
    assert "COT-2026-001 guardada como enviada" in suya and suya.count("download data-bajar-ya>") == 1
    assert "download data-bajar-ya>" not in c.get("/proyectos?hecho=cotizacion_enviada&p=1&cotizacion=1").text   # y se gastó


def test_un_aviso_escrito_a_mano_no_pinta_nada_y_con_recibo_solo_si_el_estado_es_el_que_dice(m):
    borrador = _guardar("borrador")
    enviada = _guardar("enviar")
    # sin recibo, escrito a mano: la puerta de los avisos lo quita
    a_mano = _casa().get("/proyectos", params={"p": 1, "hecho": "cotizacion_enviada", "cotizacion": enviada["id"]}).text
    assert "guardada como enviada" not in a_mano
    # con recibo pero el estado NO es el que el aviso dice: tampoco se dice
    x = _pagina(_casa(), con_recibo=True, p=1, hecho="cotizacion_enviada", cotizacion=borrador["id"])
    assert "guardada como enviada" not in x
    y = _pagina(_casa(), con_recibo=True, p=1, hecho="cotizacion_borrador", cotizacion=enviada["id"])
    assert "guardado." not in y
    # con recibo y el estado que es: se dice
    assert "guardada como enviada" in _pagina(_casa(), con_recibo=True, p=1, hecho="cotizacion_enviada", cotizacion=enviada["id"])
    assert "Borrador COT-2026-001 guardado." in _pagina(_casa(), con_recibo=True, p=1, hecho="cotizacion_borrador", cotizacion=borrador["id"])


def test_lo_que_se_rechaza_redirige_con_la_ventana_abierta_y_no_escribe_ni_gasta_numero(m):
    f = _formulario(_pagina(_casa(), p=1, cotizacion="nueva"))
    datos = _post_de_la_pagina(f, "enviar")
    for k in [k for k in datos if k.startswith("op-")]:
        datos[k] = ""                                         # una cotización sin ninguna opción
    c = _casa()
    r = c.post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    # (la dirección lleva además `rechazo`: la contraseña de lo escrito, sin ningún dato; ver `test_aviso_de_cotizacion_rechazada.py`)
    assert r.status_code == 303 and re.sub(r"&rechazo=[\w-]+", "", r.headers["location"]) == \
        "/proyectos?error=cotizacion_sin_opciones&p=1&cotizacion=nueva#cotizaciones"
    html = c.get(r.headers["location"].split("#")[0]).text
    assert "NO se guardó nada" in html and 'class="cz-form"' in html
    assert _filas(m) == [] and _filas(m, "SELECT * FROM serie_cotizaciones") == []      # y no se gastó el 001
    assert "guardada" not in _seccion_de_cotizaciones(html).split("<form")[0]


def test_el_candado_de_edicion_la_segunda_pestana_no_pisa_a_la_primera(m):
    borrador = _guardar("borrador", contenido=_contenido(1))
    f1 = _formulario(_pagina(_casa(), p=1, cotizacion=borrador["id"]))             # la pestaña 1 lee el candado
    datos1 = _post_de_la_pagina(f1, "borrador")
    c = _casa()
    ok = c.post(f"/proyectos/1/cotizaciones/{borrador['id']}", data=datos1, follow_redirects=False)
    assert "hecho=cotizacion_borrador" in ok.headers["location"]
    quedo = _filas(m)[0]
    # la pestaña 2 todavía trae el candado viejo: no escribe, dice que cambió, y la ventana muestra lo guardado
    datos2 = dict(datos1, **{"para-nombre": "Lo de la segunda pestaña"})
    r = c.post(f"/proyectos/1/cotizaciones/{borrador['id']}", data=datos2, follow_redirects=False)
    assert "error=cotizacion_cambio" in r.headers["location"]
    assert _filas(m)[0] == quedo and "segunda pestaña" not in json.dumps(_filas(m))
    assert "cambió mientras la editabas" in c.get(r.headers["location"].split("#")[0]).text


def test_una_enviada_no_vuelve_a_borrador_por_la_ventana(m):
    enviada = _guardar("enviar")
    f = _formulario(_pagina(_casa(), p=1, cotizacion=enviada["id"], corregir=1))
    datos = _post_de_la_pagina(f, "borrador")                   # un POST a mano con el botón que la ventana no ofrece
    r = _casa().post(f"/proyectos/1/cotizaciones/{enviada['id']}", data=datos, follow_redirects=False)
    assert "error=cotizacion_enviada_no_vuelve" in r.headers["location"]
    assert _filas(m)[0]["estado"] == "enviada"


def test_corregir_una_enviada_conserva_el_numero_y_sigue_enviada(m):
    enviada = _guardar("enviar")
    f = _formulario(_pagina(_casa(), p=1, cotizacion=enviada["id"], corregir=1))
    datos = _post_de_la_pagina(f, "enviar")
    datos["para-nombre"] = "Nombre corregido"
    _casa().post(f"/proyectos/1/cotizaciones/{enviada['id']}", data=datos, follow_redirects=False)
    fila = _filas(m)[0]
    assert (fila["anio"], fila["numero"], fila["estado"], fila["para_nombre"]) == (2026, 1, "enviada", "Nombre corregido")
    assert len(_filas(m)) == 1


def test_una_cotizacion_de_otro_proyecto_no_se_abre_ni_se_guarda_por_este(m):
    otra = _guardar("borrador", proyecto=3)
    html = _pagina(_casa(), p=1, cotizacion=otra["id"])
    assert 'class="cz-form"' not in html and "COT-2026-001" not in _seccion_de_cotizaciones(html)
    datos = _post_de_la_pagina(_formulario(_pagina(_casa(), p=3, cotizacion=otra["id"])), "enviar")
    r = _casa().post(f"/proyectos/1/cotizaciones/{otra['id']}", data=datos, follow_redirects=False)
    assert "error=cotizacion_no_esta" in r.headers["location"] and _filas(m)[0]["estado"] == "borrador"


def test_sin_la_tabla_el_guardado_lo_dice_y_no_dice_guardada(m, tabla_ausente_como_postgres):
    f = _formulario(_pagina(_casa(), p=1, cotizacion="nueva"))
    datos = _post_de_la_pagina(f, "borrador")
    m.con.execute("DROP TABLE cotizaciones")
    m.con.commit()
    r = _casa().post("/proyectos/1/cotizaciones", data=datos, follow_redirects=False)
    assert "error=cotizacion_sin_tabla" in r.headers["location"] and "hecho=" not in r.headers["location"]


def _claves_que_la_puerta_puede_rechazar() -> set[str]:
    """Las claves de `CotizacionNoVale` que `db/db.py` escribe, sacadas del árbol sintáctico: las constantes tal cual
    y las armadas con f-string (`f"{campo}_vacio"`, `f"{campo}_largo"`) con cada `campo` que usa el código."""
    fuente = open("db/db.py", encoding="utf-8").read()
    claves, campos = set(), set()
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "CotizacionNoVale" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant):
                claves.add(a.value)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_texto_de_cotizacion" and len(n.args) >= 2:
            if isinstance(n.args[1], ast.Constant):
                campos.add(n.args[1].value)
    for campo in campos:
        claves |= {f"{campo}_vacio", f"{campo}_largo"}
    return claves


def test_toda_clave_que_la_puerta_rechaza_se_pinta_como_un_aviso_que_dice_que_no_se_guardo(m):
    claves = _claves_que_la_puerta_puede_rechazar()
    assert {"sin_opciones", "total_cero", "fuera_de_latin1", "validez", "para_nombre_vacio", "descripcion_largo"} <= claves, claves
    claves |= {"cambio", "no_esta", "enviada_no_vuelve", "sin_tabla", "base"}
    for clave in sorted(claves):
        html = _pagina(_casa(), con_recibo=True, p=1, cotizacion="nueva", error=f"cotizacion_{clave}")
        avisos = re.findall(r'<p class="aviso">(.*?)</p>', _seccion_de_cotizaciones(html), re.S)
        assert avisos, f"la clave {clave!r} no se pinta"
        assert all(("NO se guardó" in a) or clave in ("base", "cambio") for a in avisos), (clave, avisos)
        assert all("guardada" not in a and "guardado." not in a for a in avisos), (clave, avisos)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 5. Los censos incluyen los controles nuevos
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_el_censo_de_controles_de_solo_ver_ve_la_plantilla_de_las_cotizaciones(monkeypatch):
    """`sitios_sin_cubrir` lee las plantillas de la página, también la que se incluye: los controles de las
    cotizaciones tienen que estar en su lista y aparecer en alguna página pintada."""
    monkeypatch.setattr(config, "REGISTRO_URL", "https://registro.example.test")      # como el censo de `test_proyectos_solo_ver.py`
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {CASA: "Dueño"})
    fuente = sv._fuente_sin_jinja()
    sitios = sv._sitios(fuente)
    nombres = {a.get("name") for t, a in sitios if t == "input"}
    assert {"para-nombre", "para-telefono", "fecha"} <= {n for n in nombres if n}
    assert any(t == "form" and "cotizaciones" in a.get("action", "") for t, a in sitios)
    assert any(t == "a" and "cotizacion=nueva" in a.get("href", "") for t, a in sitios)
    assert sv.sitios_sin_cubrir(fuente, sv._paginas_de_la_casa()) == []


def test_el_censo_de_sin_saltar_ve_los_controles_nuevos_en_la_casa_y_en_solo_ver(monkeypatch):
    import test_pagina_sin_saltos as ss
    monkeypatch.setattr(config, "REGISTRO_URL", "https://registro.example.test")
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {CASA: "Dueño"})
    casa = {(f["tag"], f["href"]) for f in ss.lo_que_se_toca("casa")}
    ver = {(f["tag"], f["href"]) for f in ss.lo_que_se_toca("ver")}
    assert ("form", "/proyectos/1/cotizaciones") in casa and ("form", "/proyectos/1/cotizaciones/2") in casa
    assert ("a", "/proyectos?p=1&cotizacion=nueva#cotizaciones") in casa
    assert ("a", "/proyectos/1/cotizaciones/1/pdf") in casa and ("a", "/proyectos/1/cotizaciones/2/pdf") in casa
    assert ("a", "/proyectos/1/cotizaciones/1/pdf") in ver
    assert not [x for x in ver if x[0] == "form" and "/cotizaciones" in x[1]]
    assert ("a", "/proyectos?p=1&cotizacion=nueva#cotizaciones") not in ver


def test_el_estilo_de_la_lista_no_alcanza_a_la_hoja_de_la_vista_previa():
    """La ventana vive DENTRO de `.cotiz`: una regla `.cotiz li` o `.cotiz ul` (descendiente) le daría caja y fondo
    a las condiciones de la hoja (salían como cajas negras, medido en el navegador el 14-oct-2026). Las reglas de
    la lista van con `>`: solo los hijos directos."""
    hoja = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    estilos = hoja.split("<style>", 1)[1].split("</style>", 1)[0]
    selectores = [x.strip() for x in re.findall(r"([^{}]+)\{", estilos)]
    de_la_lista = [x for x in selectores if x.startswith(".cotiz ") or x.startswith(".cotiz>")]
    assert de_la_lista, "no encontré las reglas de la lista"
    descendientes = [x for x in de_la_lista if re.match(r"\.cotiz\s+(ul|li|b|\.pild|\.sub)\b", x)]
    assert descendientes == [], descendientes


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 6. «BORRADOR» en el PDF de un borrador y no en el de una enviada
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _texto_del_pdf(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _hoja():
    from test_hoja_y_pdf_de_cotizacion import _cot
    return cz.hoja_de_cotizacion(_cot())


def test_la_marca_sale_en_el_pdf_de_un_borrador_y_no_en_el_de_una_enviada():
    con_marca = cz.pdf_de_cotizacion(_hoja(), borrador=True)
    sin_marca = cz.pdf_de_cotizacion(_hoja(), borrador=False)
    assert cz.MARCA_DE_BORRADOR in _texto_del_pdf(con_marca)
    assert cz.MARCA_DE_BORRADOR not in _texto_del_pdf(sin_marca)
    assert cz.MARCA_DE_BORRADOR not in _texto_del_pdf(cz.pdf_de_cotizacion(_hoja()))        # por omisión, sin marca
    # lo demás del PDF es lo mismo: la marca no se come ni cambia ningún texto de la hoja
    quitada = _texto_del_pdf(con_marca).replace(cz.MARCA_DE_BORRADOR, "")
    assert re.sub(r"\s+", "", quitada) == re.sub(r"\s+", "", _texto_del_pdf(sin_marca))


def test_la_marca_sale_en_cada_pagina_de_un_borrador_largo():
    from test_hoja_y_pdf_de_cotizacion import _cot
    opciones = [{"nombre": f"Opción {i}", "renglones": [
        {"descripcion": f"Trabajo {i}.{k}", "detalle": "Un detalle largo. " * 12, "total": "1000.00"} for k in range(8)]}
        for i in range(6)]
    cot = _cot(contenido={"opciones": opciones, "condiciones": []})
    pdf = cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(cot), borrador=True)
    paginas = PdfReader(io.BytesIO(pdf)).pages
    assert len(paginas) >= 3
    assert all(cz.MARCA_DE_BORRADOR in p.extract_text() for p in paginas)


def test_por_la_ruta_el_borrador_baja_con_marca_y_la_enviada_sin_ella(m):
    borrador = _guardar("borrador")
    enviada = _guardar("enviar")
    for cliente in (_casa(), _ver()):
        b = cliente.get(f"/proyectos/1/cotizaciones/{borrador['id']}/pdf")
        e = cliente.get(f"/proyectos/1/cotizaciones/{enviada['id']}/pdf")
        assert b.status_code == e.status_code == 200
        assert cz.MARCA_DE_BORRADOR in _texto_del_pdf(b.content)
        assert cz.MARCA_DE_BORRADOR not in _texto_del_pdf(e.content)


def test_una_cotizacion_enviada_corregida_sigue_sin_marca_y_la_recien_guardada_como_borrador_la_lleva(m):
    c = _casa()
    f = _formulario(_pagina(c, p=1, cotizacion="nueva"))
    c.post("/proyectos/1/cotizaciones", data=_post_de_la_pagina(f, "borrador"), follow_redirects=False)
    assert cz.MARCA_DE_BORRADOR in _texto_del_pdf(c.get("/proyectos/1/cotizaciones/1/pdf").content)
    f2 = _formulario(_pagina(c, p=1, cotizacion=1))
    c.post("/proyectos/1/cotizaciones/1", data=_post_de_la_pagina(f2, "enviar"), follow_redirects=False)
    assert cz.MARCA_DE_BORRADOR not in _texto_del_pdf(c.get("/proyectos/1/cotizaciones/1/pdf").content)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 7. Funciones puras con entradas inventadas
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class _Form(dict):
    """Como el formulario de la petición: `.get`, `in` y `.keys()`."""


def test_el_formulario_se_lee_con_indices_salteados_y_sin_romperse_con_lo_raro():
    f = _Form({"op-0-nombre": "A", "op-0-r-0-descripcion": "d", "op-0-r-0-total": "1", "op-0-r-0-detalle": "",
               "op-7-nombre": "B", "op-7-r-3-descripcion": "e", "op-7-r-3-total": "2", "op-7-r-3-detalle": "x",
               "op-1000-nombre": "NO", "op-x-nombre": "NO", "cond-deposito": "on",
               "cond-extra-2-texto": "Propia", "cond-extra-2-marcada": "on", "cond-extra-9999-texto": "NO",
               "validez": " 30 ", "para-nombre": "N", "fecha": "2026-10-14", "accion": "enviar"})
    campos = cz.campos_del_formulario(f)
    assert [o["nombre"] for o in campos["contenido"]["opciones"]] == ["A", "B"]
    assert campos["contenido"]["opciones"][1]["renglones"] == [{"descripcion": "e", "detalle": "x", "total": "2"}]
    marcadas = {c["clave"]: c["marcada"] for c in campos["contenido"]["condiciones"] if c["clave"]}
    assert marcadas == {"validez": False, "alternativas": False, "deposito": True, "balance": False, "impuestos": False}
    assert [c["texto"] for c in campos["contenido"]["condiciones"] if c["clave"] is None] == ["Propia"]
    assert campos["validez"] == 30 and campos["para_telefono"] is None and campos["cambiado_en"] is None


def test_las_cinco_condiciones_de_la_ventana_son_las_de_la_lista_cerrada_de_la_puerta():
    assert set(cz.TEXTOS_FIJOS_DE_CONDICION) | {"validez", "alternativas"} == set(db.CLAVES_DE_CONDICION_DE_COTIZACION)
    for clave in db.CLAVES_DE_CONDICION_DE_COTIZACION:
        assert cz.texto_de_condicion(clave, 15, 3)
    assert "15 días" in cz.texto_de_condicion("validez", 15, 2) and "tres opciones" in cz.texto_de_condicion("alternativas", 15, 3)


@pytest.mark.parametrize("pedido, corregir, solo_ver, esperado", [
    ("nueva", 0, False, ("nueva", 0)), ("nueva", 0, True, ("", 0)),
    ("7", 1, False, ("7", 1)), ("7", 1, True, ("7", 0)), ("7", 0, False, ("7", 0)),
    ("", 0, False, ("", 0)), ("abc", 1, False, ("", 0)), ("-1", 0, False, ("", 0)), ("1e3", 0, False, ("", 0)),
    ("٣", 0, False, ("", 0)), ("9" * 40, 0, False, ("", 0)),
])
def test_que_ventana_se_pide(pedido, corregir, solo_ver, esperado):
    assert cz.ventana_pedida(pedido, corregir, solo_ver) == esperado


def test_la_siguiente_que_toca_se_lee_de_la_serie_y_sin_tabla_es_none(m, tabla_ausente_como_postgres):
    assert asyncio.run(db.numero_que_sigue_de_cotizacion(2026)) == 1
    _guardar()
    _guardar()
    assert asyncio.run(db.numero_que_sigue_de_cotizacion(2026)) == 3
    assert asyncio.run(db.numero_que_sigue_de_cotizacion(2027)) == 1
    antes = _filas(m, "SELECT * FROM serie_cotizaciones")
    asyncio.run(db.numero_que_sigue_de_cotizacion(2026))
    assert _filas(m, "SELECT * FROM serie_cotizaciones") == antes                      # solo LEE
    m.con.execute("DROP TABLE serie_cotizaciones")
    m.con.commit()
    assert asyncio.run(db.numero_que_sigue_de_cotizacion(2026)) is None
    with pytest.raises(ValueError):
        asyncio.run(db.numero_que_sigue_de_cotizacion(99))


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 8. El guion de la ventana: sus dos cuentas, corridas en JavaScriptCore
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

hay_osascript = pytest.mark.skipif(shutil.which("osascript") is None, reason="no hay osascript (JavaScriptCore de macOS)")


def _funciones_puras_del_guion() -> str:
    fuente = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    return fuente.split("/*PURAS*/", 1)[1].split("/*FIN DE LAS PURAS*/", 1)[0]


def _en_jxa(expresion: str):
    codigo = _funciones_puras_del_guion() + "\nJSON.stringify(" + expresion + ")"
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", codigo], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


@hay_osascript
def test_el_dinero_del_guion_es_el_del_servidor_para_todo_lo_que_la_puerta_deja_pasar_y_no_inventa_el_resto():
    aceptados = ["62500", "62,500", "62,500.5", "62500.50", "1", "0.5", "0062500", "999,999.99", "99,999,999.99", "1000", "12,000.5",
                 "  62 500 ".replace(" ", "")]
    rechazados = ["", "abc", "62.500,5", "1,50", "62,50", "12.345", "-5", "1e3", "62,5000", ",5", "5,", "NaN", "٣٤"]
    for t in aceptados:
        servidor = cz.dinero_en_pesos(db.total_de_cotizacion_que_vale(t))
        assert _en_jxa(f"pesosDeLaHoja({json.dumps(t)})") == servidor, t
    for t in rechazados:
        with pytest.raises(db.CotizacionNoVale):
            db.total_de_cotizacion_que_vale(t)
        assert _en_jxa(f"pesosDeLaHoja({json.dumps(t)})") is None, t


@hay_osascript
def test_la_fecha_en_palabras_del_guion_es_la_del_servidor_en_cada_mes_y_no_inventa_el_resto():
    meses = json.dumps(list(cz._MESES))
    from datetime import date
    for mes in range(1, 13):
        for dia in (1, 9, 28):
            iso = date(2026, mes, dia).isoformat()
            assert _en_jxa(f"fechaEnPalabrasDeLaHoja({json.dumps(iso)}, {meses})") == cz.fecha_en_palabras(iso), iso
    for malo in ("", "2026-13-01", "2026-00-10", "hoy", "14/10/2026"):
        assert _en_jxa(f"fechaEnPalabrasDeLaHoja({json.dumps(malo)}, {meses})") == "", malo


def test_el_guion_solo_sale_en_la_casa(m):
    casa = _pagina(_casa(), p=1)
    assert '<script id="guion-cotizaciones">' in casa
    assert 'id="guion-cotizaciones"' not in _pagina(_ver(), p=1)


# ── 9. Lo que el guion puede tocar del navegador (decide por los NOMBRES que usa, no por una búsqueda de `fetch(`) ──

def _guion_de_las_cotizaciones() -> str:
    fuente = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    return fuente.split('<script id="guion-cotizaciones">', 1)[1].split("</script>", 1)[0]


def _sin_textos_ni_comentarios(codigo: str) -> str:
    codigo = re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)
    codigo = re.sub(r"//[^\n]*", "", codigo)
    codigo = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', codigo)
    codigo = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", codigo)
    return re.sub(r"(?<=[(,=:!&|?{};])\s*/(?:\\.|\[[^\]]*\]|[^/\n\\\[])+/[gimsuy]*", "0", codigo)


_PALABRAS_DE_JS = {"var", "function", "return", "if", "else", "while", "for", "true", "false", "null", "undefined",
                   "new", "typeof", "this", "try", "catch", "throw", "in", "of"}

# LO ÚNICO DEL NAVEGADOR QUE EL GUION PUEDE USAR (todo lo demás es rojo, y hay que mirarlo antes de agregarlo):
# los globales que lee y las propiedades/métodos que llama sobre elementos, arreglos y textos. OJO: `innerHTML` e `insertAdjacentHTML` están permitidas y copian HTML que puede traer un pedido
# (un `<img src>`): esta lista NO cierra eso.
GLOBALES_PERMITIDOS = {"document", "Array", "JSON", "Math", "MutationObserver", "Number", "String"}
PROPIEDADES_PERMITIDAS = {
    "add", "addEventListener", "alt", "apply", "appendChild", "body", "call", "checked", "className", "classList", "click",
    "closest", "concat", "contains", "createElement", "createTextNode", "documentElement", "elements", "exec", "execCommand", "firstChild", "focus", "forEach",
    "getAttribute", "hidden", "innerHTML", "insertAdjacentHTML", "length", "map", "max", "min", "name", "observe", "parentNode", "parse",
    "preventDefault", "prototype", "push", "querySelector", "querySelectorAll", "readyState", "remove", "removeAttribute",
    "removeChild",
    "replace", "select", "setAttribute", "slice", "split", "src", "target", "test", "textContent", "toFixed", "trim", "value"}


# Los datos que el guion lee de SU PROPIA información (no del navegador): las claves que le da el servidor
# (`datos_de_la_vista_previa`, sacadas de lo real) y la forma de lo que `leer` arma de las casillas.
def _datos_propios() -> set[str]:
    return set(cz.datos_de_la_vista_previa()) | {"descripcion", "detalle", "nombre", "renglones", "total"}


def lo_que_el_guion_toca(codigo: str) -> tuple[set[str], set[str]]:
    """(globales libres, propiedades) que usa el código: las propiedades son los nombres que siguen a un punto; los
    globales, los nombres que no se declaran con `var`/`function`/parámetro, no son claves de objeto ni palabras de JS."""
    limpio = _sin_textos_ni_comentarios(codigo)
    propiedades = set(re.findall(r"\.\s*([A-Za-z_$][\w$]*)", limpio))
    declarados = set(re.findall(r"\bvar\s+([A-Za-z_$][\w$]*)", limpio))
    declarados |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", limpio))
    for parametros in re.findall(r"\bfunction\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", limpio):
        declarados |= set(re.findall(r"[A-Za-z_$][\w$]*", parametros))
    for var in re.findall(r"\bvar\s+([^;]*);", limpio):                    # `var a = 1, b = 2;`
        declarados |= set(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?==|,|$)", var))
    claves = set(re.findall(r"[{,]\s*([A-Za-z_$][\w$]*)\s*:", limpio))
    libres = set()
    for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)", limpio):
        n = m.group(1)
        if n in _PALABRAS_DE_JS or n in declarados or n in claves:
            continue
        libres.add(n)
    return libres, propiedades


def test_el_guion_solo_usa_los_nombres_del_navegador_que_no_hacen_pedidos():
    """Decide por los NOMBRES que el guion usa. Cualquier nombre que no esté en las dos listas cerradas
    (`fetch`, `XMLHttpRequest`, `navigator.sendBeacon`, `location`, `window`, `open`, `submit`, `WebSocket`, `Image`,
    `eval`…) lo pone rojo, sin tener que conocerlo de antemano, y exige que todo `.src =` sea el logo.

    LO QUE VIGILA: nombres de red escritos DIRECTO en el código del guion y el `.src`. LO QUE NO VE (frontera):
    (a) HTML copiado con `innerHTML`/`insertAdjacentHTML`, también el de las plantillas `<template>` del servidor;
    (b) nombres armados en variables sobre un global permitido (`document[k]`); (c) un `/` después de `}` que el
    limpiador lee como regex; (d) un `//` dentro de un texto que esconde el resto de la línea. Que hoy solo se
    pida el logo es lo que se comprobó leyendo el guion y las plantillas, no lo que garantiza esta prueba.
    (e) `click` y `removeAttribute` están permitidas sobre CUALQUIER elemento que devuelva `querySelector`: pulsar o
    habilitar un botón cualquiera pasa en verde. Que el guion solo pulse `a[data-bajar-ya]` NO lo vigila ninguna prueba
    por completo: la sección 11 corre el guion y mira qué se pulsa solo en sus escenarios, y un `.click()` en una función
    que esos escenarios no llaman (o con otro selector) queda verde. Se comprobó leyendo el guion."""
    libres, propiedades = lo_que_el_guion_toca(_guion_de_las_cotizaciones())
    assert libres - GLOBALES_PERMITIDOS == set(), sorted(libres - GLOBALES_PERMITIDOS)
    assert propiedades - PROPIEDADES_PERMITIDAS - _datos_propios() == set(), sorted(propiedades - PROPIEDADES_PERMITIDAS - _datos_propios())
    crudo = _guion_de_las_cotizaciones()
    assert re.findall(r"\.src\s*=\s*([^;]*);", crudo) == ['"/logo-cds.png"']
    assert not re.search(r"\[\s*[\"'][^\]]*[\"']\s*\]", crudo)                 # ningún `objeto["nombre"+"armado"]`


@pytest.mark.parametrize("intruso", [
    'navigator.sendBeacon("/x", d);', 'fetch("/x");', 'new Image().src = "/x";', 'window.open("/x");',
    'form.submit();', 'var r = new XMLHttpRequest(); r.open("GET", "/x");', 'location.href = "/x";',
    'document.defaultView.fetch("/x");', 'var f = window["fe" + "tch"]; f("/x");', 'eval("fetch(1)");',
    'new WebSocket("ws://x");', 'document.forms[0].submit();', 'e.target.ownerDocument.location = "/x";'])
def test_la_prueba_de_los_nombres_ve_a_un_intruso(intruso):
    codigo = _guion_de_las_cotizaciones() + "\n" + intruso
    libres, propiedades = lo_que_el_guion_toca(codigo)
    malo = (libres - GLOBALES_PERMITIDOS) or (propiedades - PROPIEDADES_PERMITIDAS - _datos_propios()) or re.search(r"\[\s*[\"'][^\]]*[\"']\s*\]", codigo)
    assert malo, intruso


def test_el_guion_de_hoy_no_se_come_la_prueba_de_los_nombres():
    """Control del limpiador: lo que ve es de verdad el código (no quedó vacío)."""
    libres, propiedades = lo_que_el_guion_toca(_guion_de_las_cotizaciones())
    assert {"document"} <= libres and {"querySelectorAll", "insertAdjacentHTML", "createElement", "src"} <= propiedades
    assert len(propiedades) > 30
    assert _datos_propios() & {"submit", "open", "sendBeacon", "fetch", "send", "href", "location"} == set()


def test_el_guion_corrido_en_javascriptcore_no_pide_nada_al_cargar():
    """FRONTERA, dicha: esto NO prueba que el guion no haga pedidos. Lo corre con un navegador de mentira donde cada
    `fetch`, `XMLHttpRequest` y `navigator.sendBeacon` se cuenta, y mira que al cargar la página (sin ventana abierta) no
    salga ninguno. Lo que no corre al cargar (clics, escribir) NO lo vigila esta prueba, y la de los nombres de arriba solo
    mira nombres de red: ver su frontera."""
    if shutil.which("osascript") is None:
        pytest.skip("no hay osascript")
    guion = _guion_de_las_cotizaciones()
    codigo = ("var pedidos = [];\n"
              "function fetch() { pedidos.push('fetch'); }\n"
              "function XMLHttpRequest() { pedidos.push('xhr'); }\n"
              "var navigator = {sendBeacon: function () { pedidos.push('beacon'); }};\n"
              "var oyentes = {};\n"
              "var MutationObserver = function () { this.observe = function () { oyentes.observador = true; }; };\n"
              "var document = {readyState: 'complete', documentElement: {classList: {add: function (c) { oyentes.clase = c; }}}, body: {},\n"
              "  addEventListener: function (t) { oyentes[t] = true; }, querySelector: function () { return null; },\n"
              "  querySelectorAll: function () { return []; }};\n"
              + guion + "\nJSON.stringify({pedidos: pedidos, oyentes: Object.keys(oyentes).sort()})")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", codigo], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    visto = json.loads(r.stdout.strip())
    assert visto["pedidos"] == [] and visto["oyentes"] == ["change", "clase", "click", "input", "observador", "submit"], visto


# ── 10. Un estado que no se conoce sale con la marca ────────────────────────────────────────────────────

@pytest.mark.parametrize("estado", ["borrador", "raro", "", None, "ENVIADA", "enviada "])
def test_el_pdf_de_un_estado_que_no_es_enviada_sale_con_marca(m, monkeypatch, estado):
    """La base no deja guardar otro estado (CHECK), así que se le da a la ruta una cotización leída con el estado
    cambiado: solo `enviada`, tal cual, baja sin marca; cualquier otra cosa sale marcada (no sale como definitiva)."""
    _guardar("enviar")
    real = db.cotizaciones_de_proyectos

    async def _con_otro_estado():
        todo = await real()
        for cs in todo.values():
            for c in cs:
                c["estado"] = estado
        return todo
    monkeypatch.setattr(db, "cotizaciones_de_proyectos", _con_otro_estado)
    pdf = _casa().get("/proyectos/1/cotizaciones/1/pdf")
    assert pdf.status_code == 200 and cz.MARCA_DE_BORRADOR in _texto_del_pdf(pdf.content)


# ── 11. «Guardar y descargar PDF» baja solo: el guion pulsa una vez `a[data-bajar-ya]` ───────────────────

# Un documento de mentira para JavaScriptCore. FRONTERA, dicha: NO es un navegador. Solo entiende selectores `a[atributo]`
# (cualquier otro contesta «nada»: si el guion cambia de selector, no encuentra el enlace y la prueba se pone roja), cuenta los
# clics y anota si la marca ya estaba quitada al pulsar. Que un clic de verdad sobre un `<a download>` baje el archivo, y que el
# navegador deje bajar uno sin que la persona haya tocado en ese instante, NO se probó aquí: se mira en un navegador.
_DOCUMENTO_DE_MENTIRA = r"""
var clics = [], observadores = [], enlaces = [];
function enlace(atributos) {
  var a = {atributos: atributos, id: enlaces.length};
  a.removeAttribute = function (n) { delete this.atributos[n]; };
  a.click = function () { clics.push({id: this.id, marcaAlPulsar: "data-bajar-ya" in this.atributos}); };
  enlaces.push(a);
  return a;
}
function MutationObserver(f) { this.f = f; this.sobre = null; observadores.push(this); }
MutationObserver.prototype.observe = function (nodo, opciones) { this.sobre = [nodo === document.body, opciones.childList, opciones.subtree]; };
function repintar() { observadores.forEach(function (o) { o.f([]); }); }
var document = {readyState: "complete", body: {}, documentElement: {classList: {add: function () {}}},
  addEventListener: function () {}, querySelectorAll: function () { return []; },
  querySelector: function (sel) {
    var m = /^a\[([a-z-]+)\]$/.exec(sel);
    if (!m) return null;
    for (var i = 0; i < enlaces.length; i++) if (m[1] in enlaces[i].atributos) return enlaces[i];
    return null;
  }};
"""


def _correr_la_bajada(antes: str, despues: str) -> dict:
    """Corre el guion REAL (sacado de `proyectos.html`) con `antes` puesto antes de que cargue y `despues` corrido
    después; `despues` termina en una expresión JSON."""
    if shutil.which("osascript") is None:
        pytest.skip("no hay osascript")
    programa = _DOCUMENTO_DE_MENTIRA + antes + "\n" + _guion_de_las_cotizaciones() + "\n" + despues
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", programa], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


_CUENTA = "JSON.stringify({clics: clics, quedan: enlaces.map(function (a) { return 'data-bajar-ya' in a.atributos; })})"
_AVISO = 'enlace({href: "/proyectos/1/cotizaciones/1/pdf", download: "", "data-bajar-ya": ""});'
_EL_PDF_DE_LA_LISTA = 'enlace({href: "/proyectos/1/cotizaciones/2/pdf", download: ""});'


@hay_osascript
def test_con_la_marca_pulsa_una_sola_vez_y_la_quita_antes_de_pulsar():
    v = _correr_la_bajada(_AVISO, _CUENTA)
    assert v["clics"] == [{"id": 0, "marcaAlPulsar": False}], v
    assert v["quedan"] == [False]


@hay_osascript
def test_sin_la_marca_no_pulsa_nada_ni_el_pdf_de_la_lista():
    v = _correr_la_bajada(_EL_PDF_DE_LA_LISTA + 'enlace({href: "/x"});', _CUENTA)
    assert v["clics"] == [] and v["quedan"] == [False, False], v
    assert _correr_la_bajada("", _CUENTA)["clics"] == []


@hay_osascript
def test_repintada_la_pagina_sin_volver_a_correr_guiones_baja_una_vez_por_cada_aviso_nuevo():
    """La pieza de «sin saltar» cambia las regiones y no corre guiones: el guion ya cargado se entera por el observador
    del documento. Las veces que el observador avise sin aviso nuevo, no se pulsa; un aviso nuevo se pulsa una vez."""
    v = _correr_la_bajada(_EL_PDF_DE_LA_LISTA, "repintar(); repintar();\n" + _AVISO + "\nrepintar(); repintar(); repintar();\n"
                          + _AVISO + "\nrepintar(); repintar();\n" + _CUENTA)
    assert v["clics"] == [{"id": 1, "marcaAlPulsar": False}, {"id": 2, "marcaAlPulsar": False}], v
    assert v["quedan"] == [False, False, False]


@hay_osascript
def test_el_guion_se_entera_de_los_cambios_de_todo_el_documento():
    v = _correr_la_bajada("", "JSON.stringify(observadores.map(function (o) { return o.sobre; }))")
    assert v == [[True, True, True]], v                     # un solo observador, sobre `document.body`, con hijos y subárbol


def test_el_selector_del_guion_es_el_de_la_marca_que_escribe_la_plantilla():
    plantilla = open("web/plantillas/_cotizaciones_del_proyecto.html", encoding="utf-8").read()
    enlaces = re.findall(r"<a\b[^>]*\bdata-bajar-ya\b[^>]*>", plantilla)
    assert len(enlaces) == 1 and re.search(r"\bdownload\b", enlaces[0]) and 'href="/proyectos/' in enlaces[0], enlaces
    assert len(re.findall(r'uno\("a\[data-bajar-ya\]"\)', _guion_de_las_cotizaciones())) == 1
