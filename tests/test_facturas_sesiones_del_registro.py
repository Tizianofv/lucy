"""Facturas, pieza 6 (21-oct-2026): ligar sesiones del Registro a una factura y pasar sus renglones.

Qué se vigila, por las RUTAS reales, las plantillas reales y la puerta REAL de `db/db.py` en SQLite, con la App de registro
doblada en la red (`tests/_app_de_registro.py`, que afirma el contrato de la App):

  1. LA SESIÓN TIENE QUE SER DEL CLIENTE DE LA FACTURA. Se comprueba con lo que la App lista para la FICHA del cliente
     (`GET /api/lucy/sesiones?persona=<Id>`), SIN pedirle sesiones sueltas: cuando se le pide `&sesion=<ref>` la App devuelve
     esa sesión sea de quien sea y la mezcla con las de la ficha, así que ni `no_halladas` ni lo devuelto prueban de quién es.
     Vale al crear, al ligar otra, desde una cotización y desde un proyecto; la de otro cliente no se liga y la persona ve por
     qué; si la App no contesta, tampoco (no se liga a ciegas).
  2. HERMANOS: todo sitio del código que escribe una liga (se SACAN del código, no de una lista) comprueba antes y con el
     cliente de la factura.
  3. LOS RENGLONES SALEN DE LAS SESIONES: uno por sesión, con el servicio, la sala, la fecha, las horas y el `total` que la App
     tiene; la cotización manda si la hay; la persona los puede cambiar; al ligar otra se ofrece su renglón.
  4. LA PÁGINA DE LA FACTURA muestra de cada sesión ligada lo que la App dice AHORA (fecha, servicio, sala, total, abonado y
     saldo), y si la App no contesta lo dice.

FRONTERA: SQLite, no Postgres; la App es un doble; se miran HTML y respuestas, no el aspecto. NO se hace aquí el pago leído
de la App ni su cruce con la factura (pieza 7).

Correr:  python3 -m pytest tests/test_facturas_sesiones_del_registro.py -q
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudantes de la pantalla)
    CASA, NOMBRE, OTRA, OTRO_TRABAJO, SESION, SESION_B, SU_CANCELADA, TRABAJO, cliente, crear_por_la_pantalla, cuerpo,
    ligas, pantalla, registro, textos_de_avisos, unica)

import facturas_panel as fp
import registro_lectura

RAIZ = Path(__file__).resolve().parent.parent
AJENA = "no aparece entre las sesiones y trabajos de este cliente"


def pedidas_a_la_app(registro, ruta="/api/lucy/sesiones"):
    """Las consultas `GET` que le llegaron a la App a esa ruta, como lista de `[(clave, valor)]`."""
    return [parse_qsl(urlsplit(p["ruta"]).query) for p in registro.pedidos
            if p["metodo"] == "GET" and urlsplit(p["ruta"]).path == ruta]


# ── 1. La sesión tiene que ser del cliente de la factura ──────────────────────────────────────────

def test_crear_con_una_sesion_de_otra_persona_no_se_guarda_y_se_dice_por_que(pantalla):
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("21",))
    assert r.status_code == 400 and AJENA in r.text and "NO se guardó nada" in r.text
    assert tgf.foto(pantalla) == antes
    # una buena junto a una ajena: tampoco se guarda la buena (nada a medias)
    r = crear_por_la_pantalla(refs=("11", "21"))
    assert r.status_code == 400 and AJENA in r.text and tgf.foto(pantalla) == antes
    # y la pantalla vuelve al paso de escoger (como con una cancelada), sin las de otra persona marcadas
    assert "Continuar con las sesiones marcadas" in r.text


def test_un_trabajo_de_otra_persona_tampoco(pantalla):
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=(str(OTRO_TRABAJO["ref"]),))
    assert r.status_code == 400 and AJENA in r.text and tgf.foto(pantalla) == antes


def test_ligar_otra_sesion_de_otra_persona_a_un_borrador_no_se_liga(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    antes = tgf.foto(pantalla)
    for ref in ("21", "23"):
        r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": ref}, follow_redirects=False)
        assert r.status_code == 400 and AJENA in r.text and "NO se ligó nada" in r.text, ref
    assert tgf.foto(pantalla) == antes and ligas(pantalla) == [("sesion", "11", True)]


def test_la_comprobacion_es_contra_la_ficha_del_cliente_de_LA_FACTURA_y_no_contra_el_formulario(pantalla, registro):
    """La App devuelve las de `persona=<Id>`. La sesión 21 es de la ficha 8: con una factura del cliente 8 se liga, con una
    del cliente 7 no, y un `cliente` que traiga el formulario de ligar no cambia nada."""
    registro.por_persona = {"7": [dict(SESION), dict(TRABAJO)], "8": [dict(OTRA)]}
    del_siete = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    del_ocho = tgf.crear(liga={"tipo": "sesion", "ref": "99"}, cliente_noco_id=8, para_nombre="Ocho")
    c = cliente()
    r = c.post(f"/facturas/{del_siete['id']}/ligar", data={"ref": "21", "cliente": "8"}, follow_redirects=False)
    assert r.status_code == 400 and AJENA in r.text
    r = c.post(f"/facturas/{del_ocho['id']}/ligar", data={"ref": "21", "cliente": "7"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    assert ("sesion", "21", True) in ligas(pantalla, del_ocho["id"])
    # al crear: el cliente del formulario es el de la factura que se crea
    assert crear_por_la_pantalla(cliente="7", refs=("23",)).status_code == 400
    registro.por_persona["8"].append(dict(OTRO_TRABAJO))
    assert crear_por_la_pantalla(cliente="8", refs=("23",)).status_code == 303


def test_a_la_app_se_le_pide_la_ficha_y_nunca_sesiones_sueltas(pantalla, registro):
    """Si se le pidiera `&sesion=<ref>` ella devolvería la ajena mezclada con las de la ficha y nada la distinguiría."""
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    registro.pedidos.clear()
    crear_por_la_pantalla(refs=("21",))
    cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "21"}, follow_redirects=False)
    # la pantalla rechazada se vuelve a dibujar y lee la App otra vez; lo que importa es la lectura con la que se DECIDIÓ,
    # la primera de cada petición
    consultas = pedidas_a_la_app(registro)
    assert consultas[0] == [("persona", "7")]
    registro.pedidos.clear()
    cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "21"}, follow_redirects=False)
    assert pedidas_a_la_app(registro)[0] == [("persona", "7")]
    registro.pedidos.clear()
    assert crear_por_la_pantalla(refs=("13",)).status_code == 303    # una buena: se decide con la ficha y se guarda
    assert pedidas_a_la_app(registro) == [[("persona", "7")]]


@pytest.mark.parametrize("falla", [
    (500, b"{}", "application/json", 0),
    (502, b'{"error": "no_se_pudo_leer"}', "application/json", 0),
    (200, b"no es json", "text/plain", 0),
    (200, b'["no", "es", "un", "objeto"]', "application/json", 0),
    (401, b'{"error": "no"}', "application/json", 0),
], ids=["500", "502", "basura", "otra-forma", "401"])
def test_si_la_app_no_contesta_no_se_liga_a_ciegas_ni_al_crear_ni_al_ligar(pantalla, registro, falla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    antes = tgf.foto(pantalla)
    registro.forzada = falla
    r = crear_por_la_pantalla(refs=("11",))
    assert r.status_code == 400 and "No se pudo consultar el registro para comprobar" in r.text and "NO se guardó nada" in r.text
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "12"}, follow_redirects=False)
    assert r.status_code == 400 and "No se pudo consultar el registro para comprobar" in r.text and "NO se ligó nada" in r.text
    assert tgf.foto(pantalla) == antes


def test_si_la_app_no_puede_ligar_la_ficha_no_hay_nada_que_comprobar_y_no_se_liga(pantalla, registro):
    """`puede_ligar` falso (ficha sin teléfono): la App igual devolvería las sesiones pedidas una por una, pero eso no prueba
    de quién son. Se dice el motivo que dio la App."""
    registro.sesiones = {"puede_ligar": False, "motivo_sin_ligar": "sin_telefono", "sesiones": []}
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("11",))
    assert r.status_code == 400 and "no se pudo comprobar" in r.text and "la ficha no tiene teléfono" in r.text
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "12"}, follow_redirects=False)
    assert r.status_code == 400 and "no se pudo comprobar" in r.text and "la ficha no tiene teléfono" in r.text
    assert tgf.foto(pantalla) == antes


def test_un_cliente_que_no_es_una_ficha_no_se_pregunta_ni_se_liga(pantalla, registro):
    antes = tgf.foto(pantalla)
    registro.pedidos.clear()
    r = crear_por_la_pantalla(cliente="abc", refs=("11",))
    assert r.status_code == 400 and "no tiene un cliente con ficha" in r.text and tgf.foto(pantalla) == antes
    assert pedidas_a_la_app(registro) == []


def test_en_la_busqueda_las_ajenas_salen_marcadas_y_sin_casilla_ni_boton(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "sq": "pe"}).text
    cajas = re.findall(r'<input type="checkbox" name="ref" value="(\d+)"( checked)?( disabled)?>', html)
    assert [c[0] for c in cajas if c[2]] == ["21", "31", "32"] and html.count("data-no-se-puede") == 2
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    html = cliente().get(f"/facturas/{b['id']}", params={"sq": "pe"}).text
    botones = re.findall(r'action="/facturas/\d+/ligar"[^>]*><input type="hidden" name="ref" value="(\d+)">', html)
    assert botones == ["13", "12"] and html.count("data-no-se-puede") == 2


def test_en_la_busqueda_con_la_app_sin_comprobar_ninguna_se_puede_escoger(pantalla, registro):
    """La búsqueda contesta y la ficha no: no se sabe de quién es ninguna, así que ninguna tiene casilla."""
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    registro.sesiones = {"puede_ligar": False, "motivo_sin_ligar": "sin_telefono", "sesiones": []}
    html = cliente().get(f"/facturas/{b['id']}", params={"sq": "pe"}).text
    assert "No se pudo comprobar de quién es" in html and html.count("data-no-se-puede") == 3
    assert not re.findall(r'action="/facturas/\d+/ligar"[^>]*><input type="hidden" name="ref"', html)


def _etiquetas_de(html, ref):
    """El texto de la fila de la búsqueda que lleva esa `ref` (en «Nueva» la casilla, en la página el botón o su ausencia)."""
    filas = re.findall(r'<(?:label|div) class="sesion">(.*?)</(?:label|div)>', html, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", f)) for f in filas if "s0" in f or "c0" in f or "x0" in f]


def test_la_cancelada_propia_sale_como_cancelada_y_no_como_de_otra_persona_en_las_dos_pantallas(pantalla):
    """La 32 es del cliente (la ficha la nombra en `canceladas`) pero la ficha no la lista; la 31 es de otra persona y también
    viene cancelada. Una sola regla (`fp.candidata_de_la_busqueda`) decide en «Nueva factura» y en la página de la factura."""
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    for html in (cliente().get("/facturas/nueva", params={"cliente": 7, "sq": "pe"}).text,
                 cliente().get(f"/facturas/{b['id']}", params={"sq": "pe"}).text):
        filas = {re.search(r"c0(\d\d)", f).group(1): f for f in _etiquetas_de(html, "")
                 if re.search(r"c0\d\d", f)}
        assert "Cancelada: no se factura" in filas["32"] and "No está entre las sesiones de este cliente" not in filas["32"]
        assert "No está entre las sesiones de este cliente" in filas["31"]                      # la de otra persona sigue siendo ajena
    # y no se puede facturar: ni casilla marcable ni se guarda
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("32",))
    assert r.status_code == 400 and "cancelado o pospuesto" in r.text and tgf.foto(pantalla) == antes


def test_la_regla_de_la_busqueda_es_pura_y_cae_del_lado_estricto():
    c = {"ref": "9", "cancelada": False}
    ok = {"estado": "ok", "canceladas": ["9"]}
    assert fp.candidata_de_la_busqueda(c, ok) == {"ref": "9", "cancelada": True, "no_se_puede": ""}
    assert fp.candidata_de_la_busqueda(c, {"estado": "ok", "canceladas": []})["no_se_puede"] == fp.POR_QUE_NO_SE_PUEDE_ESCOGER["ajena"]
    for malo in (None, {"estado": "sin_ligar", "canceladas": ["9"]}, {"estado": "sin_cliente"}, {}):
        assert fp.candidata_de_la_busqueda(c, malo)["no_se_puede"] == fp.POR_QUE_NO_SE_PUEDE_ESCOGER["sin_comprobar"], malo


def test_una_ficha_inexistente_no_liga_nada_como_contesta_la_app_real(pantalla, registro):
    """La App real contesta `ficha_inexistente` con `puede_ligar` falso (mapa de la App); el doble ya no inventa las sesiones de otra ficha."""
    registro.fichas_que_existen = {"7"}
    s = registro_lectura  # noqa: F841
    import asyncio
    lectura = asyncio.run(registro_lectura.sesiones_de_cliente(99))
    assert (lectura["estado"], lectura["motivo"], lectura["sesiones"], lectura["trabajos"]) == ("sin_ligar", "ficha_inexistente", [], [])
    assert asyncio.run(registro_lectura.sesiones_de_cliente(7))["estado"] == "ok"
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(cliente="99", refs=("11",))
    assert r.status_code == 400 and "la ficha no existe en el registro" in r.text and tgf.foto(pantalla) == antes
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=99, para_nombre="Fantasma")
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "12"}, follow_redirects=False)
    assert r.status_code == 400 and "la ficha no existe en el registro" in r.text and tgf.foto(pantalla) == antes


@pytest.mark.parametrize("entrada", ["crear", "desde-la-cotizacion", "desde-el-proyecto", "ligar"])
def test_todos_los_caminos_que_ligan_una_sesion_exigen_que_sea_del_cliente(pantalla, entrada):
    """Los cuatro caminos que hay hoy: «Nueva factura» a secas, desde una cotización, desde un proyecto, y ligar otra a un
    borrador. Ninguno liga la de otra persona."""
    antes = tgf.foto(pantalla)
    if entrada == "crear":
        r = crear_por_la_pantalla(refs=("21",))
    elif entrada == "desde-la-cotizacion":
        r = crear_por_la_pantalla(refs=("21",), proyecto="1", cotizacion="1", opcion="2")
    elif entrada == "desde-el-proyecto":
        r = crear_por_la_pantalla(refs=("21",), proyecto="1")
    else:
        b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
        antes = tgf.foto(pantalla)
        r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "21"}, follow_redirects=False)
    assert r.status_code == 400 and AJENA in r.text
    assert tgf.foto(pantalla) == antes
    assert ("sesion", "21", True) not in ligas(pantalla)


# ── 2. Hermanos: todo sitio que escribe una liga comprueba, con el cliente de la factura ─────────────

# + `crear_borrador_de_factura_de_natalia` (pieza 9, 21-oct-2026): el camino nuevo de Natalia liga sesiones igual que la pantalla.
ESCRITORES_DE_LIGA = ("crear_borrador_de_factura", "ligar_factura_a_registro", "crear_borrador_de_factura_de_natalia")


def _archivos_del_codigo():
    """Los `.py` del repo que no son pruebas, pedidos a LA puerta de los barridos (`_py_en_disco`: no entra en entornos virtuales)."""
    from test_buzon_que_no_se_ve import _py_en_disco
    for ruta in sorted(_py_en_disco(RAIZ)):
        if "tests" not in ruta.relative_to(RAIZ).parts:
            yield ruta


def _funciones_que_escriben_una_liga():
    """`{(archivo, función): [llamadas a escritores]}` sacado del código del repo (todo `.py` que no es prueba): cada función
    que llama a `db.crear_borrador_de_factura`, `db.ligar_factura_a_registro` o `db.crear_borrador_de_factura_de_natalia`. `db/db.py` se salta: ahí viven los escritores."""
    halladas = {}
    for ruta in _archivos_del_codigo():
        if ruta == RAIZ / "db" / "db.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for f in ast.walk(arbol):
            if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            llamadas = [n for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ESCRITORES_DE_LIGA]
            if llamadas:
                halladas[(str(ruta.relative_to(RAIZ)), f.name)] = (f, llamadas)
    return halladas


def test_los_sitios_que_escriben_una_liga_se_sacan_del_codigo_y_son_los_que_hay():
    """Para que lo de abajo no sea verde por no mirar nada."""
    assert sorted(_funciones_que_escriben_una_liga()) == [
        ("web/api_code.py", "crear_factura_de_natalia"), ("web/app.py", "guardar_factura_nueva"),
        ("web/app.py", "ligar_a_una_factura")]


def test_cada_sitio_que_escribe_una_liga_comprueba_antes_con_el_cliente_de_la_factura():
    for (archivo, nombre), (f, llamadas) in _funciones_que_escriben_una_liga().items():
        comprobaciones = [n for n in ast.walk(f) if isinstance(n, ast.Call)
                          and (getattr(n.func, "id", None) == "_lo_que_dice_el_registro_del_cliente"
                               or getattr(n.func, "attr", None) == "lo_que_dice_el_registro_del_cliente")]
        decisiones = [n for n in ast.walk(f) if isinstance(n, ast.Call)
                      and getattr(n.func, "attr", None) == "ligas_de_las_refs"]
        assert comprobaciones and decisiones, (archivo, nombre)
        primera_escritura = min(n.lineno for n in llamadas)
        assert max(n.lineno for n in comprobaciones) < primera_escritura and max(n.lineno for n in decisiones) < primera_escritura, \
            f"{nombre}: la comprobación tiene que ir ANTES de escribir"
        for c in comprobaciones:
            quien = ast.unparse(c.args[0])
            if nombre == "crear_factura_de_natalia":
                # el cliente que se comprueba es el MISMO con el que se crea la factura (el primer argumento)
                crea = next(n for n in llamadas if n.func.attr == "crear_borrador_de_factura_de_natalia")
                assert quien == ast.unparse(crea.args[0]), (quien, ast.unparse(crea.args[0]))
            elif nombre == "guardar_factura_nueva":
                # el cliente que se comprueba es el MISMO con el que se crea la factura
                crea = next(n for n in llamadas if n.func.attr == "crear_borrador_de_factura")
                con_que_se_crea = next(k.value for k in crea.keywords if k.arg == "cliente_noco_id")
                assert quien == ast.unparse(con_que_se_crea), (quien, ast.unparse(con_que_se_crea))
            else:
                assert "cliente_noco_id" in quien and "form" not in quien and "campos" not in quien, quien


def test_la_comprobacion_no_pide_sesiones_sueltas_a_la_app():
    """`registro_lectura.lo_que_dice_el_registro_del_cliente` (la ÚNICA lectura, la de la pantalla y la de Natalia) llama a
    `sesiones_de_cliente` con SOLO el cliente (sin `sesiones=`), y `web/app.py::_lo_que_dice_el_registro_del_cliente` no hace
    otra cosa que llamarla."""
    fuente = ast.parse(Path(registro_lectura.__file__).read_text(encoding="utf-8"))
    f = next(n for n in ast.walk(fuente) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lo_que_dice_el_registro_del_cliente")
    llamadas = [n for n in ast.walk(f) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "sesiones_de_cliente"]
    assert len(llamadas) == 1 and len(llamadas[0].args) == 1 and not llamadas[0].keywords
    import web.app as panel
    app_py = ast.parse(Path(panel.__file__).read_text(encoding="utf-8"))
    g = next(n for n in ast.walk(app_py) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lo_que_dice_el_registro_del_cliente")
    delegadas = [n for n in ast.walk(g) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "lo_que_dice_el_registro_del_cliente"]
    assert len(delegadas) == 1 and not any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "sesiones_de_cliente"
                                           for n in ast.walk(g))


# ── 3. Los renglones salen de las sesiones ───────────────────────────────────────────────────────

def _casillas(html):
    return re.findall(r'name="r-(\d+)-(descripcion|detalle|cantidad|unidad|precio)"[^>]*?(?:value="([^"]*)"|>([^<]*)</textarea)', html)


def _renglones_de(html):
    por_n: dict[str, dict] = {}
    for n, campo, valor, texto in _casillas(html):
        por_n.setdefault(n, {})[campo] = valor or texto or ""
    return [por_n[k] for k in sorted(por_n, key=int)]


def test_el_paso_de_escribir_trae_un_renglon_por_sesion_con_el_total_de_la_app(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "12", "13"], "paso": "escribir"}).text
    llenos = [r for r in _renglones_de(html) if r["descripcion"]]
    # La App de este doble no manda `renglones` (es la de antes de D1): un renglón por sesión, 1 × su total.
    assert llenos == [
        {"descripcion": "Grabación", "detalle": "Sala P · 21 oct 2026 · 3 h", "cantidad": "1", "unidad": "",
         "precio": "9,000"},
        {"descripcion": "Mezcla", "detalle": "Sala P · 21 oct 2026 · 2 h", "cantidad": "1", "unidad": "",
         "precio": "6,000"},
        {"descripcion": "Ensayo", "detalle": "Sala P · 21 oct 2026 · 1.5 h", "cantidad": "1", "unidad": "",
         "precio": "4,500"}]
    # el total de cada renglón se ve calculado (no es una casilla: no viaja en el POST)
    assert re.findall(r'<td class="total-renglon">([^<]*)</td>', html)[:3] == ["9,000", "6,000", "4,500"]
    assert "data-renglones-de-sesiones" in html and "data-sin-total" not in html and "data-sin-detalle" not in html


def test_lo_que_sale_de_las_sesiones_se_guarda_y_se_puede_cambiar_antes(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "13"], "paso": "escribir"}).text
    datos = {"cliente": "7", "para_nombre": NOMBRE, "fecha": "2026-10-21", "ref": ["11", "13"]}
    for i, r in enumerate([x for x in _renglones_de(html) if x["descripcion"]], start=1):
        datos.update({f"r-{i}-descripcion": r["descripcion"], f"r-{i}-detalle": r["detalle"],
                      f"r-{i}-cantidad": r["cantidad"], f"r-{i}-unidad": r["unidad"], f"r-{i}-precio": r["precio"]})
    datos["r-2-precio"] = "4,000"          # la persona cambia un precio
    assert cliente().post("/facturas/nueva", data=datos, follow_redirects=False).status_code == 303
    assert [(x["descripcion"], float(x["monto"])) for x in tgf.filas(pantalla, "factura_renglones")] == [
        ("Grabación", 9000.0), ("Ensayo", 4000.0)]


def test_con_una_cotizacion_mandan_los_renglones_de_la_cotizacion_y_no_los_de_las_sesiones(pantalla):
    import json
    import test_cotizaciones_tablas as ct
    contenido = json.dumps({"opciones": [{"nombre": "Básica", "renglones": [
        {"descripcion": "Mezcla de la cotización", "detalle": "", "total": "1000.00"}]}]})
    ct._insertar(pantalla.con, proyecto_id=1, numero=4, contenido=contenido)
    cid = pantalla.con.execute("SELECT max(id) FROM cotizaciones").fetchone()[0]
    html = cliente().get("/facturas/nueva", params={"proyecto": 1, "cotizacion": cid, "ref": ["11", "12"], "paso": "escribir"}).text
    assert [r["descripcion"] for r in _renglones_de(html) if r["descripcion"]] == ["Mezcla de la cotización"]
    assert "data-renglones-de-sesiones" not in html


def test_una_sesion_sin_total_deja_el_monto_vacio_y_lo_dice(pantalla, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [
        dict(SESION, total=0.0, abonado=0.0, saldo=0.0), dict(SESION_B, total=None), dict(TRABAJO, total=12.345)]}
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "12", "13"], "paso": "escribir"}).text
    llenos = [r for r in _renglones_de(html) if r["descripcion"]]
    assert [r["precio"] for r in llenos] == ["", "", ""]          # sin total, o con más de dos decimales: lo escribe la persona
    assert "data-sin-total" in html and "«s011»" in html and "«s013»" in html and "«t012»" in html


def test_un_guardado_rechazado_devuelve_lo_escrito_y_no_vuelve_a_poner_los_de_las_sesiones(pantalla):
    r = crear_por_la_pantalla(refs=("11", "13"), ncf="B0100000009", ncf_tipo="", **{"r-1-descripcion": "Lo mío",
                                                                                  "r-1-cantidad": "1",
                                                                                  "r-1-precio": "1,234.50"})
    assert r.status_code == 400
    llenos = [x for x in _renglones_de(r.text) if x["descripcion"]]
    assert llenos == [{"descripcion": "Lo mío", "detalle": "dos horas", "cantidad": "1", "unidad": "",
                       "precio": "1,234.50"}]
    assert "data-renglones-de-sesiones" not in r.text


def test_el_renglon_de_una_sesion_es_una_cuenta_pura_sobre_lo_que_la_app_dijo():
    base = {"ref": "5", "codigo": "z005", "concepto": "Voces", "sala": "Sala A", "fecha": __import__("datetime").date(2026, 10, 3),
            "horas": "2", "total": Decimal("1500.50"), "es_trabajo": False}
    r = fp.renglon_de_una_sesion(base)
    assert (r["descripcion"], r["detalle"], r["monto"], r["sin_total"]) == ("Voces", "Sala A · 3 oct 2026 · 2 h", Decimal("1500.50"), False)
    # el renglón de respaldo es 1 × el total, sin unidad
    assert (r["cantidad"], r["unidad"], r["precio_unitario"]) == (1, "", Decimal("1500.50"))
    sin = fp.renglon_de_una_sesion(dict(base, concepto=None, sala=None, fecha=None, horas=None, total=None, es_trabajo=True))
    assert (sin["descripcion"], sin["detalle"], sin["monto"], sin["sin_total"]) == ("Trabajo", "", "", True)
    for malo in (Decimal("0"), Decimal("-3"), Decimal("1.234"), Decimal("NaN"), 100.0, None):
        assert fp.renglon_de_una_sesion(dict(base, total=malo))["sin_total"] is True, malo
    largo = fp.renglon_de_una_sesion(dict(base, concepto="x" * 400, sala="y" * 3000))
    assert len(largo["descripcion"]) == 300 and len(largo["detalle"]) == 2000


def test_al_ligar_otra_se_ofrece_su_renglon_y_aceptar_lo_pone_en_el_formulario(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "13"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/facturas/{b['id']}?ofrecer=13"
    oferta = cliente().get(r.headers["location"]).text
    assert 'data-ofrecer="13"' in oferta and "¿Agregar el renglón de «s013»?" in oferta and "RD$4,500" in oferta
    assert f'href="/facturas/{b["id"]}?agregar=13"' in oferta
    # ofrecer no escribe nada
    antes = tgf.foto(pantalla)
    form = cliente().get(f"/facturas/{b['id']}", params={"agregar": "13"}).text
    assert tgf.foto(pantalla) == antes
    assert "data-corregir" in form and "data-renglon-agregado" in form
    llenos = [x for x in _renglones_de(form) if x["descripcion"]]
    assert llenos[-1] == {"descripcion": "Ensayo", "detalle": "Sala P · 21 oct 2026 · 1.5 h", "cantidad": "1",
                          "unidad": "", "precio": "4,500"}
    assert [x["descripcion"] for x in llenos[:-1]] == ["Ensayo 1", "Ensayo 2"]          # los que ya tenía, intactos


def test_no_se_ofrece_el_renglon_de_lo_que_no_esta_ligado_a_esa_factura_ni_en_otro_estado(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    for ref in ("12", "21", "99", ""):
        html = cliente().get(f"/facturas/{b['id']}", params={"ofrecer": ref, "agregar": ref}).text
        assert "data-ofrecer" not in html and "data-renglon-agregado" not in html and "data-corregir" not in html, ref
    e = tgf.emitir(b)
    html = cliente().get(f"/facturas/{e['id']}", params={"ofrecer": "11", "agregar": "11"}).text
    assert "data-ofrecer" not in html and "data-renglon-agregado" not in html and "data-corregir" not in html
    assert "data-agregar-renglon" not in html


def test_agregar_el_renglon_de_una_ligada_sin_total_lo_deja_vacio_para_que_la_persona_lo_escriba(pantalla, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                         "sesiones": [dict(SESION), dict(SESION_B, total=0.0, abonado=0.0, saldo=0.0)]}
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"}, cliente_noco_id=7)
    tgf.correr(__import__("db.db", fromlist=["x"]).ligar_factura_a_registro(b["id"], CASA, "sesion", "13", "s013"))
    oferta = cliente().get(f"/facturas/{b['id']}", params={"ofrecer": "13"}).text
    assert "El registro no trae su total: lo escribes tú." in oferta
    form = cliente().get(f"/facturas/{b['id']}", params={"agregar": "13"}).text
    assert [x for x in _renglones_de(form) if x["descripcion"]][-1]["precio"] == ""


# ── 4. La página de la factura dice lo que la App dice de cada sesión ──────────────────────────────

def _tarjeta_de_ligas(html):
    return html[html.index("data-ligas"):]


def test_la_pagina_muestra_de_cada_sesion_ligada_fecha_servicio_sala_total_abonado_y_saldo(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, cliente_noco_id=7)
    tgf.correr(__import__("db.db", fromlist=["x"]).ligar_factura_a_registro(b["id"], CASA, "trabajo", "12", "t012"))
    for estado in ("borrador", "emitida"):
        if estado == "emitida":
            b = tgf.emitir(tgf.leer(b))
        html = cliente().get(f"/facturas/{b['id']}").text
        tarjeta = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _tarjeta_de_ligas(html)))
        assert "s013" in tarjeta and "21 oct" in tarjeta and "Ensayo" in tarjeta and "Sala P" in tarjeta, estado
        assert "Total RD$4,500 · Abonado RD$1,000 · Saldo RD$3,500" in tarjeta, estado
        assert "t012" in tarjeta and "Mezcla" in tarjeta and "Total RD$6,000 · Abonado RD$0 · Saldo RD$6,000" in tarjeta, estado
        assert "No se pudo leer el registro" not in html


def test_los_datos_de_la_pagina_son_los_de_ahora_y_no_los_de_cuando_se_ligo(pantalla, registro):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, cliente_noco_id=7)
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                         "sesiones": [dict(SESION_B, total=5000.0, abonado=5000.0, saldo=0.0)]}
    tarjeta = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _tarjeta_de_ligas(cliente().get(f"/facturas/{b['id']}").text)))
    assert "Total RD$5,000 · Abonado RD$5,000 · Saldo RD$0" in tarjeta and "RD$4,500" not in tarjeta


@pytest.mark.parametrize("estado", ["borrador", "emitida"])
def test_si_la_app_no_contesta_la_pagina_lo_dice_y_no_pinta_dinero_inventado(pantalla, registro, estado):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, cliente_noco_id=7)
    if estado == "emitida":
        b = tgf.emitir(b)
    registro.forzada = (500, b"{}", "application/json", 0)
    r = cliente().get(f"/facturas/{b['id']}")
    assert r.status_code == 200 and "data-registro-no-contesta" in r.text and "No se pudo leer el registro ahora" in r.text
    assert "data-dinero" not in r.text and "ref. 13" in r.text


def test_una_ligada_que_la_app_ya_no_devuelve_o_esta_cancelada_se_dice(pantalla, registro):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "32"}, cliente_noco_id=7)
    tgf.correr(__import__("db.db", fromlist=["x"]).ligar_factura_a_registro(b["id"], CASA, "sesion", "404", "z404"))
    tarjeta = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _tarjeta_de_ligas(cliente().get(f"/facturas/{b['id']}").text)))
    assert "Cancelada o pospuesta en el registro" in tarjeta and "El registro ya no la devuelve" in tarjeta
    assert "Total RD$" not in tarjeta


def test_ver_la_pagina_no_escribe_nada_y_pide_a_la_app_la_ficha_y_las_ligadas(pantalla, registro):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, cliente_noco_id=7)
    antes = tgf.foto(pantalla)
    registro.pedidos.clear()
    cliente().get(f"/facturas/{b['id']}")
    assert tgf.foto(pantalla) == antes
    assert pedidas_a_la_app(registro) == [[("persona", "7"), ("sesion", "13")]]


def test_las_ligas_leidas_son_una_funcion_pura_de_lo_que_la_app_contesto():
    ligas_ = [{"tipo": "sesion", "ref": "1", "codigo": "a"}, {"tipo": "trabajo", "ref": "2", "codigo": "b"},
              {"tipo": "sesion", "ref": "3", "codigo": "c"}, {"tipo": "sesion", "ref": "4", "codigo": "d"}]
    fila = {"ref": "1", "total": Decimal("10"), "abonado": None, "saldo": Decimal("10.5")}
    lectura = {"estado": "ok", "sesiones": [fila], "trabajos": [], "canceladas": ["3"], "no_halladas": ["4"]}
    leidas = fp.ligas_con_lo_leido(ligas_, lectura)
    assert [l["leida"] for l in leidas] == ["ok", "no_esta", "cancelada", "no_esta"]
    assert leidas[0]["fila"]["dinero_texto"] == {"total": "RD$10", "abonado": "—", "saldo": "RD$10.50"}
    assert [l["leida"] for l in fp.ligas_con_lo_leido(ligas_, {"estado": "no_se_pudo"})] == ["no_se_pudo"] * 4
    # y la lectura real de la App que no pudo (`registro_lectura`) llega a ese mismo estado
    assert registro_lectura.RegistroNoContesta
