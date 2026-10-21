"""Facturas, pieza D2 (21-oct-2026): la factura con detalle.

Tiziano, textual: «Si, las facturas deben salir con los detalles.» El modelo es la cotización con detalle
(`disenos/lucy-cotizacion-con-detalle/`): SERVICIO (con su descripción debajo), CANTIDAD con su unidad, PRECIO
UNITARIO y TOTAL.

Qué se vigila, con la puerta REAL de `db/db.py` en SQLite, las rutas y plantillas REALES y la App de registro doblada
EN LA RED (`tests/_app_de_registro.py`):

  1. EL TOTAL DEL RENGLÓN ES CANTIDAD × PRECIO UNITARIO, y se guarda en `monto` (la única cifra que suman la factura,
     la lista y el humo). Un producto que no da en centavos exactos se RECHAZA (como el dinero de las cotizaciones),
     no se redondea; la base lo vuelve a exigir con su CHECK.
  2. LOS RENGLONES DE ANTES SIGUEN VALIENDO: sin cantidad ni precio unitario se leen 1 × su monto, sin unidad, y se
     ven igual en la hoja.
  3. DESDE LAS SESIONES: cada renglón de la cuenta de la sesión (pieza D1 de la App) es un renglón de la factura; su
     total da el `subtotal` de NocoDB. Con `tipo_cobro` `hora` la cantidad es cantidad × horas y la unidad «horas».
     Si no cuadra, 1 × el subtotal y se dice. Si `renglones` viene `null` (o no viene), un renglón con el total de la
     sesión, y con `null` la pantalla avisa que no se pudo leer el detalle. El descuento de la sesión no va en los
     renglones: desde la pieza D3 llena la casilla del descuento de la factura (aquí solo se comprueba la casilla).
  4. LA HOJA Y EL PDF pintan las cuatro columnas; la vista previa, lo mismo.
  5. LA PANTALLA de crear y corregir deja escribir cantidad, unidad y precio; el total del renglón no es una casilla.
  6. LA PUERTA DE NATALIA recibe renglones con detalle y, sin renglones, los arma desde las sesiones como la pantalla.
  7. LA MIGRACIÓN, el esquema, el ensayo y el humo: las tres columnas, su CHECK y la cuenta.
  8. HERMANOS: la cotización sigue con su tabla de dos columnas y su mismo PDF.

FRONTERA, dicha una vez: SQLite y no Postgres (el CHECK del total corre en los dos, pero `NUMERIC(14,3)` no impone los
decimales en SQLite: los rechaza la puerta); la App es un doble que afirma el contrato de D1 que está en el mapa de
Registro; se miran el texto del PDF y el HTML, no el aspecto.

Correr:  python3 -m pytest tests/test_facturas_el_detalle.py -q
"""
from __future__ import annotations

import io
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos cambian `httpx` al importarse)
from pypdf import PdfReader

import test_guardar_factura as tgf
from test_guardar_factura import mundo  # noqa: F401  (fixture)
import test_facturas_tablas as ft
import test_hoja_y_pdf_de_cotizacion as thc
from test_pantalla_de_facturas import (  # noqa: F401  (fixtures y ayudantes de la pantalla)
    NOMBRE, SESION, SESION_B, TRABAJO, cliente, crear_por_la_pantalla, pantalla, registro)
from test_facturas_sesiones_del_registro import _renglones_de
from test_facturas_natalia import api, crear_api  # noqa: F401  (fixture de la puerta de Natalia y su ayudante)

import cotizaciones as cz
import db.db as db
import facturas as fa
import facturas_panel as fp


def _renglon(**kw):
    base = {"descripcion": "Mezcla", "cantidad": "2", "precio_unitario": "18000.00"}
    base.update(kw)
    return base


def _cuenta(**kw):
    """Un renglón de la cuenta de una sesión, como lo manda la App (D1)."""
    base = {"titulo": "Mezcla y Masterización", "cantidad": 2, "horas": 1, "precio_unitario": 18000,
            "subtotal": 36000, "nota": None, "producto_id": 1, "tipo_cobro": None}
    base.update(kw)
    return base


# ── 1. El total del renglón ─────────────────────────────────────────────────────────────────────────

def test_el_total_del_renglon_es_cantidad_por_precio_y_eso_es_lo_que_se_guarda(mundo):
    f = tgf.crear(renglones=[_renglon(cantidad="1.5", precio_unitario="2000.00", unidad="horas")])
    r = f["renglones"][0]
    assert (r["cantidad"], r["unidad"], r["precio_unitario"], r["monto"]) == (
        Decimal("1.500"), "horas", Decimal("2000.00"), Decimal("3000.00"))
    fila = tgf.filas(mundo, "factura_renglones")[0]
    assert (Decimal(str(fila["cantidad"])), fila["unidad"], Decimal(str(fila["precio_unitario"])),
            Decimal(str(fila["monto"]))) == (Decimal("1.5"), "horas", Decimal("2000"), Decimal("3000"))
    # y el subtotal de la factura es la suma de los `monto`, sin una segunda cuenta
    assert f["subtotal"] == Decimal("3000.00")


def test_la_cantidad_y_la_unidad_se_leen_de_vuelta_y_la_unidad_vacia_es_sin_unidad(mundo):
    f = tgf.crear(renglones=[_renglon(cantidad="2", unidad="  ")])
    assert (f["renglones"][0]["unidad"], f["renglones"][0]["cantidad"]) == (None, Decimal("2.000"))


@pytest.mark.parametrize("renglon,clave", [
    (_renglon(cantidad="0"), "cantidad_cero"), (_renglon(cantidad="-1"), "cantidad_formato"),
    (_renglon(cantidad="1.2345"), "cantidad_decimales"), (_renglon(cantidad="dos"), "cantidad_formato"),
    (_renglon(cantidad=None), "cantidad_vacio"), (_renglon(cantidad=""), "cantidad_vacio"),
    (_renglon(cantidad=True), "cantidad_tipo"), (_renglon(cantidad="1e3"), "cantidad_formato"),
    (_renglon(precio_unitario="10.123"), "monto_decimales"), (_renglon(precio_unitario="0"), "monto_cero"),
    (_renglon(unidad="x" * 21), "unidad_largo"),
    (_renglon(cantidad="0.333", precio_unitario="30.01"), "monto_redondeo"),
])
def test_lo_que_no_vale_como_cantidad_precio_o_unidad_no_toca_la_base(mundo, renglon, clave):
    antes = tgf.foto(mundo)
    tgf.no_vale(clave, lambda: tgf.otra(renglones=[renglon]))
    assert tgf.foto(mundo) == antes


@pytest.mark.parametrize("renglon", [
    {"descripcion": "A", "monto": "10", "cantidad": "2", "precio_unitario": "5"},   # las dos formas a la vez
    {"descripcion": "A", "cantidad": "2"},                                         # cantidad sin precio
    {"descripcion": "A", "unidad": "horas"},                                       # unidad sin precio
    {"descripcion": "A"},                                                          # sin monto ni cantidad
])
def test_las_dos_formas_del_renglon_no_se_mezclan(mundo, renglon):
    tgf.no_vale("renglon_forma", lambda: tgf.otra(renglones=[renglon]))


def test_una_cantidad_de_tres_decimales_cabe_y_su_producto_exacto_tambien(mundo):
    f = tgf.otra(renglones=[_renglon(cantidad="0.125", precio_unitario="2000.00")])   # 250.00 exacto
    assert f["renglones"][0]["monto"] == Decimal("250.00")


def test_la_base_no_deja_guardar_un_renglon_cuyo_total_no_sea_cantidad_por_precio(mundo):
    f = tgf.crear()
    with pytest.raises(Exception) as e:
        mundo.con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, cantidad, unidad, "
                          "precio_unitario, monto) VALUES (?, 9, 'Malo', 2, 'horas', 100, 300)", (f["id"],))
        mundo.con.commit()
    assert "CHECK" in str(e.value) or "constraint" in str(e.value).lower()
    mundo.con.rollback()
    # y la cantidad y el precio van juntos o ninguno (el renglón viejo no tiene ninguno de los dos)
    with pytest.raises(Exception):
        mundo.con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, cantidad, monto) "
                          "VALUES (?, 9, 'A medias', 1, 100)", (f["id"],))
    mundo.con.rollback()


def test_un_renglon_de_antes_del_detalle_sigue_valiendo_y_se_lee_1_por_su_monto(mundo):
    f = tgf.crear()
    mundo.con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, detalle, monto) "
                      "VALUES (?, 3, 'Viejo', NULL, 500)", (f["id"],))
    mundo.con.commit()
    viejo = next(r for r in tgf.leer(f)["renglones"] if r["orden"] == 3)
    assert (viejo["cantidad"], viejo["unidad"], viejo["precio_unitario"], viejo["monto"]) == (
        Decimal("1.000"), None, Decimal("500.00"), Decimal("500.00"))


# ── 2. Desde las sesiones: el total da el subtotal de NocoDB ────────────────────────────────────────

def test_cada_renglon_de_la_cuenta_es_un_renglon_de_la_factura_y_su_total_da_el_subtotal():
    sesion = {"ref": "11", "codigo": "s011", "concepto": "Grabación", "sala": "Sala P",
              "fecha": __import__("datetime").date(2026, 10, 21), "es_trabajo": False, "total": Decimal("44000"),
              "descuento": Decimal("0"), "renglones": [
                  _cuenta(),                                                        # 2 × 18000, sin unidad
                  _cuenta(titulo="Grabación de Batería", cantidad=2, horas=2, precio_unitario=2000,
                          subtotal=8000, producto_id=2, tipo_cobro="hora")]}        # 2 × 2 h = 4 horas
    filas = fp.renglones_de_una_sesion(sesion)
    assert [(r["descripcion"], r["cantidad"], r["unidad"], r["precio_unitario"], r["monto"]) for r in filas] == [
        ("Mezcla y Masterización", Decimal("2"), "", Decimal("18000"), Decimal("36000")),
        ("Grabación de Batería", Decimal("4"), "horas", Decimal("2000"), Decimal("8000"))]
    assert [r["detalle"] for r in filas] == ["Sala P · 21 oct 2026", "Sala P · 21 oct 2026"]
    assert sum(r["monto"] for r in filas) == sesion["total"]
    assert all(not r["sin_total"] and not r["sin_detalle"] and not r["aviso_del_detalle"] for r in filas)


def test_si_la_cuenta_no_da_se_usa_1_por_el_subtotal_y_se_avisa():
    # el producto del renglón no da su subtotal (horas raras, redondeo del registro)
    sesion = {"ref": "11", "codigo": "s011", "concepto": "Grabación", "sala": "Sala P",
              "fecha": __import__("datetime").date(2026, 10, 21), "es_trabajo": False, "total": Decimal("5000"),
              "renglones": [_cuenta(cantidad=3, horas=1, precio_unitario=2000, subtotal=5000)]}
    (fila,) = fp.renglones_de_una_sesion(sesion)
    assert (fila["cantidad"], fila["unidad"], fila["precio_unitario"], fila["monto"]) == (1, "", Decimal("5000"), Decimal("5000"))
    assert "no dan su subtotal" in fila["aviso_del_detalle"] and "RD$5,000" in fila["aviso_del_detalle"]


def test_un_renglon_de_la_cuenta_que_no_se_puede_cobrar_cae_al_total_de_la_sesion():
    sesion = {"ref": "11", "codigo": "s011", "concepto": "Grabación", "sala": "Sala P",
              "fecha": __import__("datetime").date(2026, 10, 21), "es_trabajo": False, "total": Decimal("9000"),
              "renglones": [_cuenta(subtotal=0)]}
    (fila,) = fp.renglones_de_una_sesion(sesion)
    assert (fila["cantidad"], fila["precio_unitario"], fila["monto"]) == (1, Decimal("9000"), Decimal("9000"))
    assert "no se puede cobrar" in fila["aviso_del_detalle"]


def test_sin_detalle_la_sesion_sigue_dando_un_renglon_con_su_total_y_lo_dice():
    base = {"ref": "11", "codigo": "s011", "concepto": "Grabación", "sala": "Sala P",
            "fecha": __import__("datetime").date(2026, 10, 21), "es_trabajo": False, "total": Decimal("9000")}
    (sin_leer,) = fp.renglones_de_una_sesion(dict(base, renglones=None, renglones_motivo="cuenta_no_se_pudo_leer"))
    assert (sin_leer["monto"], sin_leer["sin_detalle"], sin_leer["cantidad"]) == (Decimal("9000"), "cuenta_no_se_pudo_leer", 1)
    # la App de antes de D1 no manda la clave: se cae a lo de hoy SIN avisar de una cuenta que nadie intentó leer
    (sin_clave,) = fp.renglones_de_una_sesion(base)
    assert sin_clave["sin_detalle"] == "" and sin_clave["monto"] == Decimal("9000")
    # una cuenta vacía de verdad también manda el total, sin aviso
    (vacia,) = fp.renglones_de_una_sesion(dict(base, renglones=[]))
    assert vacia["sin_detalle"] == "" and vacia["monto"] == Decimal("9000")


# ── 3. La pantalla: crear con detalle, y lo que avisa ───────────────────────────────────────────────

def _con_cuenta(registro_, *sesiones):
    registro_.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "",
                          "sesiones": [dict(s) for s in sesiones]}
    return sesiones


DETALLADA = dict(SESION, total=44000.0, abonado=0.0, saldo=44000.0, descuento=0.0, renglones=[
    _cuenta(),
    _cuenta(titulo="Grabación de Batería", cantidad=2, horas=2, precio_unitario=2000, subtotal=8000,
            producto_id=2, tipo_cobro="hora")])


def test_la_pantalla_de_escribir_trae_un_renglon_por_cada_renglon_de_la_cuenta(pantalla, registro):
    _con_cuenta(registro, DETALLADA)
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    llenos = [r for r in _renglones_de(html) if r["descripcion"]]
    assert llenos == [
        {"descripcion": "Mezcla y Masterización", "detalle": "Sala P · 21 oct 2026", "cantidad": "2", "unidad": "",
         "precio": "18,000"},
        {"descripcion": "Grabación de Batería", "detalle": "Sala P · 21 oct 2026", "cantidad": "4", "unidad": "horas",
         "precio": "2,000"}]
    assert re.findall(r'<td class="total-renglon">([^<]*)</td>', html)[:2] == ["36,000", "8,000"]
    assert "data-renglones-de-sesiones" in html and "data-sin-detalle" not in html


def test_lo_que_la_pantalla_muestra_de_la_cuenta_se_guarda_con_su_total(pantalla, registro):
    _con_cuenta(registro, DETALLADA)
    r = crear_por_la_pantalla(cliente="7", refs=("11",), **{
        "r-1-descripcion": "Mezcla y Masterización", "r-1-detalle": "Sala P · 21 oct 2026",
        "r-1-cantidad": "2", "r-1-unidad": "", "r-1-precio": "18,000",
        "r-2-descripcion": "Grabación de Batería", "r-2-detalle": "Sala P · 21 oct 2026",
        "r-2-cantidad": "4", "r-2-unidad": "horas", "r-2-precio": "2,000"})
    assert r.status_code == 303, r.text
    filas = tgf.filas(pantalla, "factura_renglones")
    assert [(x["descripcion"], float(x["cantidad"]), x["unidad"], float(x["precio_unitario"]), float(x["monto"]))
            for x in filas] == [
        ("Mezcla y Masterización", 2.0, None, 18000.0, 36000.0),
        ("Grabación de Batería", 4.0, "horas", 2000.0, 8000.0)]
    assert tgf.leer(tgf.filas(pantalla, "facturas")[0]["id"])["subtotal"] == Decimal("44000.00")


def test_si_no_se_pudo_leer_el_detalle_la_pantalla_lo_avisa(pantalla, registro):
    _con_cuenta(registro, dict(SESION, renglones=None, renglones_motivo="cuenta_no_se_pudo_leer"))
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert "data-sin-detalle" in html and "No se pudo leer el detalle de la cuenta de «s011»" in html
    # y el renglón sigue saliendo con el total de la sesión
    assert [r["precio"] for r in _renglones_de(html) if r["descripcion"]] == ["9,000"]


def test_el_descuento_de_la_sesion_llena_la_casilla_de_la_factura(pantalla, registro):
    # Desde la pieza D3 la factura ya lo resta: la casilla del descuento viene con la suma de los de las sesiones.
    _con_cuenta(registro, dict(DETALLADA, descuento=500.0))
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert 'name="descuento" value="500"' in html
    assert "data-descuento" not in html and "todavía no lo resta" not in html
    # los renglones siguen siendo los de la cuenta, sin descontar nada en ellos (el descuento va aparte)
    assert re.findall(r'<td class="total-renglon">([^<]*)</td>', html)[:2] == ["36,000", "8,000"]


def test_la_pantalla_de_corregir_deja_los_tres_campos_y_el_total_calculado(pantalla):
    f = tgf.crear(renglones=[_renglon(cantidad="1.5", unidad="horas", precio_unitario="2000.00")])
    html = cliente().get(f"/facturas/{f['id']}", params={"corregir": "1"}).text
    for campo, valor in (("descripcion", "Mezcla"), ("cantidad", "1.5"), ("unidad", "horas"), ("precio", "2,000")):
        assert f'name="r-1-{campo}" value="{valor}"' in html, campo
    assert '<td class="total-renglon">3,000</td>' in html
    assert 'name="r-1-monto"' not in html          # el total no se escribe: sale de la cantidad y el precio


# ── 4. La hoja y el PDF ─────────────────────────────────────────────────────────────────────────────

def _f(renglones=None, **kw):
    base = {"id": 7, "anio": 2026, "numero": 1, "estado": "emitida", "codigo": "FAC-2026-001",
            "para_nombre": "Cliente de prueba", "rnc_cedula": None, "fecha": "2026-10-14", "ncf": None,
            "ncf_tipo": None, "itbis_pct": None, "motivo_anulacion": None,
            "renglones": renglones or [
                {"orden": 1, "descripcion": "Mezcla y Masterización", "detalle": "Mezcla estéreo", "cantidad": Decimal("2"),
                 "unidad": "", "precio_unitario": Decimal("18000.00"), "monto": Decimal("36000.00")},
                {"orden": 2, "descripcion": "Grabación de Batería", "detalle": "Sesión en cabina",
                 "cantidad": Decimal("4.000"), "unidad": "horas", "precio_unitario": Decimal("2000.00"),
                 "monto": Decimal("8000.00")}]}
    base.update(kw)
    base.update(db.totales_de_factura([r["monto"] for r in base["renglones"]], base["itbis_pct"]))
    return base


def test_la_hoja_dice_las_cuatro_columnas_y_la_cantidad_con_su_unidad():
    h = fa.hoja_de_factura(_f())
    assert h["encabezados"] == ("SERVICIO", "CANTIDAD", "PRECIO UNITARIO", "TOTAL")
    assert [(r["descripcion"], r["cantidad"], r["precio_unitario"], r["monto"]) for r in h["renglones"]] == [
        ("Mezcla y Masterización", "2", "RD$18,000", "RD$36,000"),
        ("Grabación de Batería", "4 horas", "RD$2,000", "RD$8,000")]


def test_el_pdf_lleva_las_columnas_y_el_detalle_y_un_renglon_viejo_se_ve_1_por_su_monto():
    pdf = fa.pdf_de_factura(fa.hoja_de_factura(_f(renglones=[
        {"orden": 1, "descripcion": "Servicio viejo", "detalle": "", "monto": Decimal("1250.00")}])))
    leido = re.sub(r"\s+", "", "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages))
    assert "SERVICIO" in leido and "CANTIDAD" in leido and "PRECIOUNITARIO" in leido and "TOTAL" in leido
    assert "Servicioviejo" in leido and "1RD$1,250RD$1,250" in leido


def test_la_vista_previa_trae_las_cuatro_columnas_y_la_cantidad_con_su_unidad():
    h = fa.hoja_de_factura(_f())
    html = str(__import__("web.app", fromlist=["x"]).plantillas.env.get_template(
        "_hoja_de_factura.html").module.hoja_de_factura(h))
    for texto in ("SERVICIO", "CANTIDAD", "PRECIO UNITARIO", "TOTAL", "Mezcla y Masterización", "2", "4 horas",
                  "RD$18,000", "RD$36,000"):
        assert texto in html, texto
    assert html.index("SERVICIO") < html.index("CANTIDAD") < html.index("PRECIO UNITARIO") < html.index("TOTAL")


# ── 5. La migración, el esquema, el ensayo y el humo ───────────────────────────────────────────────

COLUMNAS = {"cantidad": "numeric", "precio_unitario": "numeric", "unidad": "text"}


def test_la_migracion_agrega_las_tres_columnas_y_el_esquema_dice_lo_mismo():
    import test_base_m2 as b2
    import test_cotizaciones_tablas as ct
    texto = ft._texto_migracion_del_detalle()
    sin_comentarios = db._sin_comentarios(texto)
    de_la_migracion = {c.lower(): t.lower() for c, t in re.findall(
        r"ALTER\s+TABLE\s+factura_renglones\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)", sin_comentarios, re.I)}
    assert de_la_migracion == COLUMNAS
    assert ct._columnas_que_agregan_otras_migraciones_a("factura_renglones", ft._migracion()) == set(COLUMNAS)
    for c in COLUMNAS:
        assert re.search(rf"^\s+{c}\s", b2._ddl(ft._ESQUEMA, "factura_renglones")[0], re.M), c
    # idempotente, y no reescribe ninguna fila
    for s in ft._sentencias_que_corren(texto):
        if re.match(r"ALTER\s+TABLE", s, re.I):
            assert re.search(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", s, re.I), s
        else:
            assert re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I), s


def test_la_migracion_corre_sobre_lo_de_hoy_y_no_reescribe_los_renglones():
    """`_base()` ya la aplicó; aquí se comprueba lo que dejó sobre los renglones de hoy (las tres columnas en NULL).
    La segunda corrida no se ejerce en SQLite (`ADD COLUMN IF NOT EXISTS` no existe ahí; la idempotencia la prueba
    el texto en `test_la_migracion_agrega_las_tres_columnas...` y la ejerce Postgres en el ensayo)."""
    con = ft._base()
    f = ft._factura(con)
    con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, monto) VALUES (?, 1, 'Viejo', 500)", (f,))
    con.commit()
    assert con.execute("SELECT count(*), count(cantidad), count(precio_unitario), count(unidad) FROM factura_renglones"
                       ).fetchone() == (1, 0, 0, 0)
    info = {c[1]: c for c in con.execute("PRAGMA table_info(factura_renglones)")}
    for c in COLUMNAS:
        assert info[c][3] == 0 and info[c][4] is None, c          # acepta NULL y sin valor por omisión


def test_el_ensayo_arma_el_sql_sin_conectarse_y_cierra_con_rollback():
    import importlib.util
    from test_grupo_ia import _ROOT
    spec = importlib.util.spec_from_file_location("ensayo_facturas_detalle",
                                                  _ROOT / "tools" / "ensayo_facturas_detalle.py")
    guion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guion)
    texto = ft._texto_migracion_del_detalle()
    ensayo = guion.sql_del_ensayo(texto)
    assert "ROLLBACK;" in ensayo and ensayo.count("COMMIT") == 0
    assert "factura_renglones.cantidad" in ensayo and "precio_unitario" in ensayo and "unidad" in ensayo
    assert "los renglones de hoy quedan sin detalle" in ensayo
    cuenta = guion.sql_de_la_cuenta(texto)
    assert cuenta.strip().startswith("BEGIN READ ONLY;") and "count(*)" in cuenta


def test_el_humo_lee_las_tres_columnas_y_cuenta_los_renglones_fuera_de_cuenta():
    import ast
    from test_grupo_ia import _ROOT
    fuente = (_ROOT / "tools" / "humo.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_renglones_de_la_factura")
    cuerpo = ast.unparse(funcion)
    for columna in ("cantidad", "unidad", "precio_unitario"):
        assert columna in cuerpo
    assert "round(cantidad * precio_unitario, 2)" in cuerpo
    assert "_renglones_de_la_factura" in fuente.split("pruebas = [", 1)[1]


# ── 6. La puerta de Natalia ────────────────────────────────────────────────────────────────────────

def test_natalia_sin_renglones_los_arma_desde_la_cuenta_de_la_sesion(api, registro):
    registro.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [dict(DETALLADA)]}
    r = crear_api(("11",))
    assert r.status_code == 200, r.text
    assert [(x["descripcion"], x["cantidad"], x["unidad"], x["precio_unitario"], x["monto"])
            for x in r.json()["factura"]["renglones"]] == [
        ("Mezcla y Masterización", "2", "", "18000.00", "36000.00"),
        ("Grabación de Batería", "4", "horas", "2000.00", "8000.00")]
    assert r.json()["factura"]["subtotal"] == "44000.00"


def test_natalia_puede_mandar_renglones_con_detalle(api):
    r = crear_api(("11",), renglones=[
        {"descripcion": "Mezcla", "cantidad": "2", "unidad": "canciones", "precio_unitario": "18000.00"}])
    assert r.status_code == 200, r.text
    assert r.json()["factura"]["renglones"] == [
        {"descripcion": "Mezcla", "detalle": None, "cantidad": "2", "unidad": "canciones",
         "precio_unitario": "18000.00", "monto": "36000.00"}]


@pytest.mark.parametrize("renglon", [
    {"descripcion": "A", "cantidad": "2", "precio_unitario": "5", "monto": "10"},     # las dos formas
    {"descripcion": "A", "cantidad": "2"},                                           # cantidad sin precio
    {"descripcion": "A", "unidad": "horas", "precio_unitario": "5"},                 # unidad sin cantidad
    {"descripcion": "A", "cantidad": "0", "precio_unitario": "5"},                   # cantidad cero
])
def test_natalia_con_un_renglon_que_no_vale_no_escribe(api, renglon):
    antes = tgf.foto(api)
    r = crear_api(("11",), renglones=[renglon])
    assert r.status_code == 422, (r.status_code, r.text)
    assert tgf.foto(api) == antes


# ── 7. Hermanos: la cotización sigue con sus dos columnas ──────────────────────────────────────────

def test_la_cotizacion_sigue_con_su_tabla_de_dos_columnas_y_su_pdf(monkeypatch):
    hoja = cz.hoja_de_cotizacion(thc._cot())
    assert hoja["encabezados"] == ("DESCRIPCIÓN", "TOTAL")
    llamadas = []
    real = cz._fila
    monkeypatch.setattr(cz, "_fila", lambda pdf, h, a, b, d, det, t: (llamadas.append(d), real(pdf, h, a, b, d, det, t))[1])
    pdf = cz.pdf_de_cotizacion(hoja)
    leido = re.sub(r"\s+", "", "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages))
    assert llamadas and "DESCRIPCIÓN" in leido and "PRECIOUNITARIO" not in leido and "CANTIDAD" not in leido
    assert leido == re.sub(r"\s+", "", "\n".join(p.extract_text() for p in
                                                 PdfReader(io.BytesIO(cz.pdf_de_cotizacion(hoja))).pages))
    # y la factura NO usa la tabla de la cotización
    llamadas.clear()
    fa.pdf_de_factura(fa.hoja_de_factura(_f()))
    assert llamadas == []
