"""Buscar el cliente desde «Nueva cotización» (cambio aprobado por Tiziano el 16-oct-2026, «Dale con la B»).

Cuando el proyecto no tiene cliente, la ventana de «Nueva cotización» deja buscarlo en Noco; al elegirlo se guarda
como cliente del proyecto por la MISMA escritura del encabezado (`POST /proyectos/N/cliente` → `db.poner_cliente`) y la
ventana vuelve a abrirse con el nombre y el teléfono de esa ficha. Se recorre la ruta REAL y la plantilla REAL sobre
SQLite con el SQL real de `poner_cliente` y de `cotizaciones_de_proyectos` (el mundo de
`test_cotizaciones_en_la_pagina.py`). Qué se vigila:

  1. sin cliente la ventana ofrece buscar; con cliente no, y las otras ventanas (borrador, corregir) tampoco;
  2. elegir una ficha llama a `db.poner_cliente` (el mismo que usa el encabezado), guarda el nombre que devuelve Noco
     y no el que mandó el formulario, vuelve a la ventana, y la ventana trae nombre y teléfono de ESA ficha;
  3. solo la sesión de la casa: en solo ver la ventana no se arma y el POST da 401 sin escribir;
  4. si Noco no contesta —al buscar o al elegir— la ventana lo dice y no se liga nada;
  5. ninguna frase dice que hay cliente si no se escribió (una dirección escrita a mano no lo afirma);
  6. nada de más: desde la ventana no se quita un cliente ni se cambia uno que el proyecto ya tiene.

FRONTERA, dicha una vez: Noco es un doble (`noco_lectura.persona`, `buscar_personas` y `contacto`; lo que dicen de la
red lo prueba `test_noco_lectura.py`); no es un navegador (el JavaScript de la búsqueda no corre aquí: lo que se prueba
es el camino sin JavaScript, que es el mismo formulario y la misma ruta); SQLite en vez de Postgres. La negativa de
«ya tiene cliente» lee y después escribe: dos personas a la vez podrían pisarse; no se prueba.

Correr:  python3 -m pytest tests/test_cotizacion_busca_cliente.py -q
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero, como en `test_cotizaciones_en_la_pagina.py`)

import test_cotizaciones_en_la_pagina as t
from test_cotizaciones_en_la_pagina import (  # noqa: F401  (fixtures y ayudas)
    CASA, _casa, _filas, _pagina, _seccion_de_cotizaciones, _ver, gente, m, mundo, tabla_ausente_como_postgres)
import db.db as db
import noco_lectura
import web.app as panel

FICHAS = {101: {"nombre": "Ficha Ciento Uno", "telefono": "809-555-0101"},
          102: {"nombre": "Ficha Ciento Dos", "telefono": "829-555-0102"}}


@pytest.fixture
def noco(m, monkeypatch):
    """Noco doblado: la búsqueda devuelve las dos fichas, `persona` da el nombre de ESA ficha y `contacto` su teléfono.
    `caido` lo apaga (todo levanta `NocoNoContesta`). `llamadas` anota las veces que se escribió por `poner_cliente`."""
    estado = {"caido": False, "poner_cliente": []}

    async def _buscar(texto):
        if estado["caido"]:
            raise noco_lectura.NocoNoContesta("no pude hablar con Noco")
        return [{"id": i, "nombre": f["nombre"]} for i, f in FICHAS.items()]

    async def _persona(noco_id):
        if estado["caido"]:
            raise noco_lectura.NocoNoContesta("no pude hablar con Noco")
        f = FICHAS.get(noco_id)
        return {"id": noco_id, "nombre": f["nombre"]} if f else None

    async def _contacto(noco_id):
        if estado["caido"]:
            raise noco_lectura.NocoNoContesta("no pude hablar con Noco")
        f = FICHAS.get(noco_id)
        return {"telefono": f["telefono"], "email": None} if f else None

    original = db.poner_cliente

    async def _poner(*a, **k):
        estado["poner_cliente"].append(a)
        return await original(*a, **k)

    monkeypatch.setattr(noco_lectura, "buscar_personas", _buscar)
    monkeypatch.setattr(noco_lectura, "persona", _persona)
    monkeypatch.setattr(noco_lectura, "contacto", _contacto)
    monkeypatch.setattr(db, "poner_cliente", _poner)
    return estado


def _cliente_de(m_, pid):
    return tuple(m_.con.execute("SELECT cliente_noco_id, cliente_nombre FROM proyectos WHERE id = ?", (pid,)).fetchone())


def _el_bloque(html):
    """El bloque de buscar el cliente DENTRO de la ventana, o `None`."""
    esta = re.search(r'<div class="cz-cliente-buscar">.*?</form>\s*</div>\s*</div>', _seccion_de_cotizaciones(html), re.S)
    return esta.group(0) if esta else None


def _valor(html, nombre):
    return re.search(rf'<input[^>]*name="{nombre}"[^>]*value="([^"]*)"', html).group(1)


# ═══ 1. Sin cliente se ofrece buscar; con cliente no ═══

def test_sin_cliente_la_ventana_nueva_ofrece_buscar_y_con_cliente_no(m, noco):
    sin = _pagina(_casa(), p=2, cotizacion="nueva")
    bloque = _el_bloque(sin)
    assert bloque is not None, "sin cliente la ventana no ofrece buscar"
    # el mismo formulario y la misma ruta que el encabezado, y vuelve a la ventana
    assert 'action="/proyectos/2/cliente"' in bloque and 'name="volver" value="cotizacion"' in bloque
    assert 'name="pdonde" value="cotizacion-cliente"' in bloque and 'name="cotizacion" value="nueva"' in bloque
    con = _pagina(_casa(), p=1, cotizacion="nueva")
    assert _el_bloque(con) is None and "cz-cliente-buscar\"" not in _seccion_de_cotizaciones(con)
    assert 'name="volver"' not in con


def test_solo_la_ventana_nueva_ofrece_buscar_no_la_de_un_borrador_ni_la_de_corregir(m, noco):
    borrador = t._guardar("borrador", proyecto=2, para_nombre="Alguien")
    enviada = t._guardar("enviar", proyecto=2, para_nombre="Alguien")
    for consulta in ({"cotizacion": borrador["id"]}, {"cotizacion": enviada["id"], "corregir": 1},
                     {"cotizacion": enviada["id"]}):
        assert 'name="volver"' not in _pagina(_casa(), p=2, **consulta), consulta


def test_el_encabezado_y_la_ventana_no_se_pisan_los_resultados(m, noco):
    """La búsqueda de la ventana tiene su propio `pdonde`: sus resultados salen en la ventana y no también en la
    caja «Cliente» del encabezado (dos botones del mismo Id serían dos sitios que escriben)."""
    html = _pagina(_casa(), p=2, cotizacion="nueva", pq="ficha", pdonde="cotizacion-cliente")
    assert html.count('class="sugerencia"') == len(FICHAS)
    assert html.count('title="Elegir a Ficha Ciento Uno"') == 1
    assert _el_bloque(html).count('class="sugerencia"') == len(FICHAS)


# ═══ 2. Elegir liga por el mismo `poner_cliente` y la ventana sale con nombre y teléfono ═══

def test_elegir_una_ficha_liga_por_poner_cliente_y_la_ventana_sale_con_su_nombre_y_su_telefono(m, noco):
    assert _cliente_de(m, 2) == (None, None)
    # lo que manda el navegador SIN JavaScript al tocar el resultado: el Id de Noco y `volver`; el nombre que quiera
    # decir el formulario no cuenta
    r = _casa().post("/proyectos/2/cliente", data={"noco_id": "101", "volver": "cotizacion",
                                                    "cliente_nombre": "Otro Nombre"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos?hecho=cliente&p=2&cotizacion=nueva#cotizaciones", r.headers["location"]
    assert noco["poner_cliente"] == [(2, 101)]                              # la MISMA escritura del encabezado
    assert _cliente_de(m, 2) == (101, "Ficha Ciento Uno")             # el nombre es el de Noco
    huella = m.con.execute("SELECT actor, accion, motivo FROM log_acciones WHERE tabla = 'proyectos' "
                           "AND registro_id = 2").fetchall()
    assert [tuple(h) for h in huella] == [("panel", "editar", "cliente cambiado desde el panel de proyectos")]
    # la ventana vuelve a abrirse, ya con el cliente y su teléfono, y sin el buscador
    ruta, consulta = r.headers["location"].split("#")[0].split("?")
    html = _casa().get(ruta + "?" + consulta).text
    assert _valor(html, "para-nombre") == "Ficha Ciento Uno" and _valor(html, "para-telefono") == "809-555-0101"
    assert _el_bloque(html) is None
    assert "El cliente de este proyecto es Ficha Ciento Uno" in _seccion_de_cotizaciones(html)


def test_otra_ficha_da_otro_nombre_y_otro_telefono(m, noco):
    _casa().post("/proyectos/2/cliente", data={"noco_id": "102", "volver": "cotizacion"}, follow_redirects=False)
    html = _casa().get("/proyectos", params={"p": 2, "cotizacion": "nueva"}).text
    assert _valor(html, "para-nombre") == "Ficha Ciento Dos" and _valor(html, "para-telefono") == "829-555-0102"


def test_una_ficha_que_noco_no_conoce_no_liga_nada(m, noco):
    r = _casa().post("/proyectos/2/cliente", data={"noco_id": "999", "volver": "cotizacion"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente&p=2&cotizacion=nueva#cotizaciones"
    assert _cliente_de(m, 2) == (None, None)


def test_sin_el_campo_volver_la_ruta_sigue_como_siempre(m, noco):
    """El encabezado no manda `volver`: vuelve como siempre, sin la ventana; y un `volver` que no es el de la
    cotización se ignora."""
    r = _casa().post("/proyectos/2/cliente", data={"noco_id": "101"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?hecho=cliente&p=2"
    for raro in ("https://otro.example", "COTIZACION", "1", ""):
        r = _casa().post("/proyectos/2/cliente", data={"noco_id": "102", "volver": raro}, follow_redirects=False)
        assert r.headers["location"].startswith("/proyectos?") and "cotizacion" not in r.headers["location"] \
            and "http" not in r.headers["location"], (raro, r.headers["location"])


# ═══ 3. Solo quien puede ligar un cliente en el panel ═══

def test_en_solo_ver_ni_se_arma_la_ventana_ni_el_buscador_y_el_post_da_401_sin_escribir(m, noco):
    t._guardar("enviar", proyecto=2, para_nombre="Alguien")        # con una cotización, solo ver SÍ dibuja la sección
    assert 'id="cotizaciones"' in _pagina(_ver(), p=2)
    html = _pagina(_ver(), p=2, cotizacion="nueva", pq="ficha", pdonde="cotizacion-cliente")
    assert 'name="volver"' not in html and 'class="cz-cliente-buscar"' not in html and 'class="sugerencia"' not in html
    r = _ver().post("/proyectos/2/cliente", data={"noco_id": "101", "volver": "cotizacion"}, follow_redirects=False)
    assert r.status_code == 401
    sin_sesion = t.Navegador(panel.app, base_url="https://testserver")
    assert sin_sesion.post("/proyectos/2/cliente", data={"noco_id": "101", "volver": "cotizacion"},
                           follow_redirects=False).status_code in (303, 401)
    assert _cliente_de(m, 2) == (None, None) and noco["poner_cliente"] == []


# ═══ 4. Noco no contesta ═══

def test_si_noco_no_contesta_al_buscar_la_ventana_lo_dice_y_no_ofrece_a_nadie(m, noco):
    noco["caido"] = True
    html = _pagina(_casa(), p=2, cotizacion="nueva", pq="ficha", pdonde="cotizacion-cliente")
    bloque = _el_bloque(html)
    assert "no pude hablar con Noco" in bloque and 'class="sugerencia"' not in bloque
    assert "Nadie con" not in bloque                  # una lista vacía no se disfraza de «esa persona no está»


def test_si_noco_no_contesta_al_elegir_no_se_liga_nada_y_la_ventana_lo_dice(m, noco):
    noco["caido"] = True
    r = _casa().post("/proyectos/2/cliente", data={"noco_id": "101", "volver": "cotizacion"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente_noco&p=2&cotizacion=nueva#cotizaciones"
    assert _cliente_de(m, 2) == (None, None)
    assert [x for x in _filas(m, "SELECT * FROM log_acciones WHERE tabla = 'proyectos'")] == []
    html = _casa().get("/proyectos", params={"p": 2, "cotizacion": "nueva", "error": "cliente_noco"}).text
    secc = _seccion_de_cotizaciones(html)
    assert "Noco no contestó: el cliente NO se ligó" in secc
    assert "El cliente de este proyecto es" not in secc


# ═══ 5. Ninguna frase dice que hay cliente si no se escribió ═══

def test_una_direccion_escrita_a_mano_no_afirma_que_se_ligo_un_cliente(m, noco):
    for consulta in ({"hecho": "cliente"}, {"hecho": "cliente", "error": "cliente_noco"}):
        html = _pagina(_casa(), con_recibo=True, p=2, cotizacion="nueva", **consulta)
        assert "El cliente de este proyecto es" not in html and 'class="aviso ok"' not in _seccion_de_cotizaciones(html)
    assert _cliente_de(m, 2) == (None, None)
    # y los avisos de «no se ligó» tampoco salen fuera de la ventana nueva (el encabezado ya tiene los suyos)
    fuera = _pagina(_casa(), con_recibo=True, p=2, error="cliente_noco")
    assert "NO se ligó" not in fuera


def test_los_avisos_de_la_ventana_dicen_que_no_se_ligo_nada(m, noco):
    for error in ("cliente", "cliente_noco", "cliente_ya_tiene"):
        html = _pagina(_casa(), con_recibo=True, p=2, cotizacion="nueva", error=error)
        avisos = re.findall(r'<p class="aviso">(.*?)</p>', _seccion_de_cotizaciones(html), re.S)
        assert avisos and all("NO" in a for a in avisos), (error, avisos)


# ═══ 6. Nada de más: ni quitar ni cambiar ═══

def test_desde_la_ventana_no_se_quita_un_cliente(m, noco):
    r = _casa().post("/proyectos/1/cliente", data={"noco_id": "", "volver": "cotizacion"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente&p=1&cotizacion=nueva#cotizaciones"
    assert _cliente_de(m, 1) == (7, "Cliente de la ficha") and noco["poner_cliente"] == []
    # el encabezado SOLO lo quita en IA (pieza 1 de «Un proyecto no se crea sin cliente», 18-oct-2026): en CDS y ACD el
    # proyecto necesita cliente y se rechaza.
    r = _casa().post("/proyectos/1/cliente", data={"noco_id": ""}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente&p=1"
    assert _cliente_de(m, 1) == (7, "Cliente de la ficha")
    m.con.execute("UPDATE proyectos SET area = 'IA' WHERE id = 1")
    m.con.commit()
    r = _casa().post("/proyectos/1/cliente", data={"noco_id": ""}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?hecho=cliente_quitado&p=1"
    assert _cliente_de(m, 1) == (None, None)


def test_desde_la_ventana_no_se_cambia_un_cliente_que_el_proyecto_ya_tiene(m, noco):
    r = _casa().post("/proyectos/1/cliente", data={"noco_id": "101", "volver": "cotizacion"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente_ya_tiene&p=1&cotizacion=nueva#cotizaciones"
    assert _cliente_de(m, 1) == (7, "Cliente de la ficha") and noco["poner_cliente"] == []
    # el encabezado sí lo cambia (no se tocó)
    r = _casa().post("/proyectos/1/cliente", data={"noco_id": "101"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?hecho=cliente&p=1" and _cliente_de(m, 1) == (101, "Ficha Ciento Uno")


def test_un_proyecto_que_no_esta_no_se_liga_desde_la_ventana(m, noco):
    r = _casa().post("/proyectos/77/cliente", data={"noco_id": "101", "volver": "cotizacion"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?error=cliente&p=77&cotizacion=nueva#cotizaciones"
    assert noco["poner_cliente"] == []


# ═══ Hermanos: los sitios donde se busca una persona salen de UNA lista ═══

def test_el_buscador_de_la_ventana_esta_en_la_lista_de_sitios_que_la_ruta_acepta(m):
    """Todo `pdonde` que la plantilla pinta se acepta por `panel.DONDE_DE_LA_BUSQUEDA` (la lista que usa la ruta y los
    dobles de las pruebas): uno que la plantilla pinte y la ruta no conozca buscaría en silencio nada."""
    html = _pagina(_casa(), p=2, cotizacion="nueva")
    pintados = set(re.findall(r'name="pdonde" value="([^"]+)"', html))
    assert "cotizacion-cliente" in pintados and "cliente" in pintados
    for donde in pintados:
        assert re.fullmatch(panel.DONDE_DE_LA_BUSQUEDA, donde), donde
