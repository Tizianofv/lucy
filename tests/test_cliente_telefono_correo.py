"""El teléfono y el correo del cliente en la parte de cliente de la página de un proyecto
(11-oct-2026, maqueta aprobada en `disenos/lucy-proyecto-cliente/`).

CÓMO. La página se pide por HTTP a la ruta real, con la base de SQLite y el esquema real
(`test_pagina_proyectos.py::mundo`). Noco se dobla EN LA RED: el lector real
(`noco_lectura.contacto`) le habla con `httpx` a un transporte de mentira que contesta por el
`Id` del `where`, como el `Noco` de `test_noco_lectura.py`.

FRONTERA: la forma de las filas (`telefono`, `email`, `unida_a`) sale del encargo de la sala,
no de una captura del Noco de CDS; nada se corrió contra el Noco ni contra la App de verdad.

Correr:  python3 -m pytest tests/test_cliente_telefono_correo.py -q
"""
from __future__ import annotations

import logging
import os
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import config  # noqa: E402
import noco_lectura  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
from test_noco_lectura import HTTPX, Noco, _apuntar_a_noco  # noqa: E402
from test_pagina_proyectos import gente, mundo  # noqa: E402,F401

TELEFONO = "809-555-0142"
CORREO = "cliente@ejemplo.invalid"


class NocoPorId(Noco):
    """Un Noco que contesta la fila del `Id` que se le pide en el `where`."""

    def __init__(self, fichas):
        super().__init__()
        self.fichas = fichas

    def _manejar(self, pedido):
        self.pedidos.append(pedido)
        if self.falla is not None:
            raise self.falla
        if self.json_crudo is not None:
            return HTTPX.Response(self.status, json=self.json_crudo)
        if self.cuerpo is not None:
            return HTTPX.Response(self.status, text=self.cuerpo)
        if self.status != 200:
            return HTTPX.Response(self.status, json={"msg": "no"})
        quien = int(re.fullmatch(r"\(Id,eq,(\d+)\)", pedido.url.params["where"]).group(1))
        filas = [self.fichas[quien]] if quien in self.fichas else []
        return HTTPX.Response(200, json={"list": filas, "pageInfo": {"totalRows": len(filas)}})


def _ficha(i, **resto):
    return {"Id": i, "nombre": f"Cliente {i}", **resto}


@pytest.fixture
def noco(monkeypatch):
    return _apuntar_a_noco(monkeypatch, NocoPorId({7: _ficha(7, telefono=TELEFONO, email=CORREO)}))


def _con_cliente(mundo):
    mundo.proyecto(1, "Disco Uno", area="CDS", cliente="Cliente X")
    mundo.proyecto(2, "Sin cliente", area="CDS")


def _abrir(proyecto=1, con="casa", consulta=""):
    c = TestClient(panel.app)
    if con == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    else:
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    r = c.get(f"/proyectos?p={proyecto}{consulta}")
    assert r.status_code == 200, r.text[:300]
    return r.text


def _quienes(html):
    return html.split('<div class="quienes">', 1)[1].split("</main>")[0]


DATOS = ('<span class="dato"><span>Teléfono:</span> <b>%s</b></span>'
         '<span class="dato"><span>Correo:</span> <b>%s</b></span>')


# ── 1. los dos renglones, en la casa y en solo ver ──────────────────────────────────────

def test_la_casa_enseña_telefono_y_correo_debajo_de_la_caja_de_cliente(mundo, noco):
    _con_cliente(mundo)
    q = _quienes(_abrir())
    assert '<div class="cliente-bloque"><div class="persona-campo cliente-campo">' in q
    assert ('</div><div class="cliente-datos">' + DATOS % (TELEFONO, CORREO) + "</div></div>") in q
    assert q.index("Quitar el cliente") < q.index("Teléfono:")


def test_solo_ver_enseña_cliente_telefono_y_correo_apilados(mundo, noco):
    _con_cliente(mundo)
    q = _quienes(_abrir(con="ver"))
    assert ('<div class="cliente-datos chico"><span class="dato"><span>Cliente:</span> <b>Cliente X</b></span>'
            + DATOS % (TELEFONO, CORREO) + "</div>") in q
    assert q.index("Correo:") < q.index("Responsable:")


# ── 2. lo que falta no se pinta ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("con", ["casa", "ver"])
@pytest.mark.parametrize("ficha,sale,no_sale", [
    ({"telefono": TELEFONO, "email": None}, "Teléfono:", "Correo:"),
    ({"telefono": "  ", "email": CORREO}, "Correo:", "Teléfono:"),
    ({"telefono": 8095550142, "email": CORREO}, "Correo:", "Teléfono:"),
])
def test_el_renglon_que_falta_no_sale(mundo, noco, con, ficha, sale, no_sale):
    _con_cliente(mundo)
    noco.fichas[7] = _ficha(7, **ficha)
    q = _quienes(_abrir(con=con))
    assert sale in q and no_sale not in q


@pytest.mark.parametrize("con", ["casa", "ver"])
@pytest.mark.parametrize("ficha", [{}, {"telefono": "", "email": ""}, {"telefono": None, "email": "   "}])
def test_sin_ninguno_la_parte_de_cliente_queda_como_hoy(mundo, noco, con, ficha):
    _con_cliente(mundo)
    noco.fichas[7] = _ficha(7, **ficha)
    q = _quienes(_abrir(con=con))
    assert "cliente-datos" not in q and "cliente-bloque" not in q
    if con == "ver":
        assert q.startswith('<span class="nota">Cliente: <b>Cliente X</b></span>')
    else:
        assert q.lstrip().startswith('<div class="persona-campo cliente-campo">')


# ── 3. Noco no contesta ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("con", ["casa", "ver"])
@pytest.mark.parametrize("como", ["falla", "error", "basura", "otra_forma", "sin_configurar"])
def test_si_noco_no_contesta_la_pagina_abre_como_hoy_y_sin_ruido(mundo, noco, monkeypatch, caplog, con, como):
    _con_cliente(mundo)
    secreto = "texto-de-error-que-no-debe-salir"
    if como == "falla":
        noco.falla = HTTPX.ConnectError(secreto)
    elif como == "error":
        noco.status = 503
    elif como == "basura":
        noco.cuerpo = secreto
    elif como == "otra_forma":
        noco.json_crudo = {"mensaje": secreto}
    else:
        monkeypatch.delenv("NOCODB_BASE")
    with caplog.at_level(logging.DEBUG):
        html = _abrir(con=con)
    q = _quienes(html)
    assert "cliente-datos" not in q and "Cliente X" in q
    assert secreto not in html and secreto not in caplog.text
    assert TELEFONO not in caplog.text and CORREO not in caplog.text


def test_el_tiempo_de_espera_es_el_del_lector(mundo, noco, monkeypatch):
    _con_cliente(mundo)
    vistos = []
    real = noco_lectura.httpx.AsyncClient

    def cliente(*a, **k):
        vistos.append(k.get("timeout"))
        return real(*a, **k)
    monkeypatch.setattr(noco_lectura.httpx, "AsyncClient", cliente)
    _abrir()
    assert vistos == [noco_lectura.TIEMPO_LIMITE] == [8.0]


# ── 4. la ficha unida a otra ────────────────────────────────────────────────────────────

def test_una_ficha_unida_se_lee_de_la_que_quedo_y_no_cambia_el_cliente_guardado(mundo, noco):
    _con_cliente(mundo)
    noco.fichas = {7: _ficha(7, telefono=None, email=None, unida_a=8),
                   8: _ficha(8, telefono="809-555-0199", email="otro@ejemplo.invalid")}
    q = _quienes(_abrir())
    assert "809-555-0199" in q and "otro@ejemplo.invalid" in q
    assert [p.url.params["where"] for p in noco.pedidos] == ["(Id,eq,7)", "(Id,eq,8)"]
    fila = mundo.con.execute("SELECT cliente_noco_id, cliente_nombre FROM proyectos WHERE id = 1").fetchone()
    assert tuple(fila) == (7, "Cliente X")
    assert "Cliente X" in q


def _cadena(largo):
    """Fichas 7, 8, ... cada una unida a la siguiente; la última (la que quedó) trae los datos."""
    fichas = {7 + k: _ficha(7 + k, unida_a=8 + k) for k in range(largo - 1)}
    fichas[7 + largo - 1] = _ficha(7 + largo - 1, telefono=TELEFONO, email=CORREO)
    return fichas


def test_diez_saltos_se_siguen_y_el_once_ya_no(mundo, noco):
    _con_cliente(mundo)
    noco.fichas = _cadena(11)             # 10 saltos
    assert TELEFONO in _quienes(_abrir())
    assert len(noco.pedidos) == 11
    noco.pedidos.clear()
    noco.fichas = _cadena(12)             # 11 saltos
    q = _quienes(_abrir())
    assert TELEFONO not in q and "cliente-datos" not in q and "Cliente X" in q
    assert len(noco.pedidos) == 11


def test_una_cadena_que_da_la_vuelta_se_corta_en_el_tope(mundo, noco):
    _con_cliente(mundo)
    noco.fichas = {7: _ficha(7, unida_a=8), 8: _ficha(8, unida_a=7)}
    q = _quienes(_abrir())
    assert "cliente-datos" not in q
    assert len(noco.pedidos) == noco_lectura.TOPE_SALTOS + 1


def test_la_ficha_a_la_que_apunta_no_existe_y_se_trata_como_sin_datos(mundo, noco):
    _con_cliente(mundo)
    noco.fichas = {7: _ficha(7, unida_a=99)}
    assert "cliente-datos" not in _quienes(_abrir())


# ── cuántos pedidos ─────────────────────────────────────────────────────────────────────

def test_abrir_un_proyecto_con_cliente_es_un_pedido_y_nada_mas_lo_hace(mundo, noco):
    _con_cliente(mundo)
    _abrir(1)
    assert len(noco.pedidos) == 1
    p = noco.pedidos[0]
    assert p.method == "GET" and p.url.params["fields"] == noco_lectura.COLUMNAS_CONTACTO
    assert p.url.params["where"] == "(Id,eq,7)"
    noco.pedidos.clear()
    c = TestClient(panel.app)
    c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    mundo.tarea(10, "suelta del grupo", area="CDS")
    lista = c.get("/proyectos?g=CDS")                         # las tareas sueltas de un grupo
    assert lista.status_code == 200 and "suelta del grupo" in lista.text and 'class="quienes"' not in lista.text
    assert noco.pedidos == []
    _abrir(2)                                                 # un proyecto sin cliente
    assert noco.pedidos == []


# ── 5. el escapado ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("campo,hostil", [
    ("telefono", '"><script>alert(1)</script>&x<b>\''),
    ("email", '"><img src=x onerror=alert(1)>&y<i>\''),
])
@pytest.mark.parametrize("con", ["casa", "ver"])
def test_el_telefono_y_el_correo_salen_escapados_y_sin_enlace(mundo, noco, campo, hostil, con):
    _con_cliente(mundo)
    noco.fichas[7] = _ficha(7, **{"telefono": TELEFONO, "email": CORREO, campo: hostil})
    html = _abrir(con=con)
    assert hostil not in html
    assert str(escape(hostil)) in _quienes(html)
    datos = _quienes(html).split('class="cliente-datos')[1].split("</div>")[0]
    assert "href" not in datos and "mailto" not in datos and "<a " not in datos
    assert "<script>alert(1)" not in html and "<img src=x" not in html


# ── el lector, directo ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("unida", [0, -3, True, "8", None, 2.5])
async def test_una_marca_de_unida_que_no_es_un_id_no_se_sigue(noco, unida):
    noco.fichas = {7: _ficha(7, telefono=TELEFONO, email=CORREO, unida_a=unida), 8: _ficha(8, telefono="otro")}
    assert await noco_lectura.contacto(7) == {"telefono": TELEFONO, "email": CORREO}
    assert len(noco.pedidos) == 1


async def test_una_fila_que_no_es_un_objeto_se_trata_como_sin_ficha(noco):
    noco.json_crudo = {"list": [5], "pageInfo": {}}
    assert await noco_lectura.contacto(7) is None


async def test_una_cadena_que_pasa_del_tope_sube_no_contesta(noco):
    noco.fichas = _cadena(12)
    with pytest.raises(noco_lectura.NocoNoContesta):
        await noco_lectura.contacto(7)
    assert len(noco.pedidos) == noco_lectura.TOPE_SALTOS + 1
