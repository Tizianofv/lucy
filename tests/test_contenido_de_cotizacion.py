"""La puerta del contenido de una cotización (pieza 1 del diseño «Cotizaciones en Proyectos», 14-oct-2026):
`db.contenido_de_cotizacion_que_vale` y `db.total_de_cotizacion_que_vale`.

Qué se vigila, con la función REAL (no hay SQL en esta pieza; la puerta es código puro):

  1. qué contenido vale: el mínimo, varias opciones y renglones, y el dinero escrito como «62,500»,
     «62500», «62500.5», «62,500.50», un `int` o un `Decimal`; todo sale como texto con dos decimales;
  2. cada borde que NO vale, con su `clave` de rechazo: sin opciones, total 0 / negativo / «abc» / vacío /
     `float` / `bool` / NaN / Infinity / más de dos decimales / coma mal puesta / dígitos de otro alfabeto
     / demasiado grande; textos largos; NUL, saltos de línea donde no van, U+2028 y un sustituto solo;
     clave de condición desconocida o repetida; `marcada` que no es booleano; campos que sobran; topes de
     cantidad;
  3. lo vacío de sobra que dibuja el formulario sin JavaScript se descarta (renglón, opción, condición);
  4. ningún `float` llega al JSON (se lee con `parse_float` que revienta);
  5. la salida es idempotente: pasarla otra vez por la puerta da lo mismo;
  6. el mensaje del rechazo no repite lo que se pidió;
  7. con entradas inventadas (semilla fija): lo que la puerta acepta como total es un `Decimal` mayor
     que cero, con a lo sumo dos decimales y a lo sumo el tope.

FRONTERA: la puerta no conoce `validez_dias` ni el número de opciones, así que NO rehace los textos de las
condiciones `validez` y `alternativas` (eso es de quien guarde, pieza 3). Los topes de largo y de cantidad
son de diseño, no medidos.

Correr:  python3 -m pytest tests/test_contenido_de_cotizacion.py -q
"""
from __future__ import annotations

import copy
import json
import random
from decimal import Decimal

import pytest

import test_grupo_ia  # noqa: F401  (deja puestas las variables de entorno que pide `config`)

import db.db as db

NoVale = db.CotizacionNoVale


def reng(descripcion="Diseño de logo", detalle="", total="62,500"):
    return {"descripcion": descripcion, "detalle": detalle, "total": total}


def opcion(nombre="Básica", *renglones):
    return {"nombre": nombre, "renglones": list(renglones) or [reng()]}


def cond(clave="deposito", texto="Se paga la mitad al empezar.", marcada=True):
    return {"clave": clave, "texto": texto, "marcada": marcada}


def valido(**cambios):
    base = {"opciones": [opcion()], "condiciones": [cond()]}
    base.update(cambios)
    return base


def clave_del_rechazo(crudo) -> str:
    with pytest.raises(NoVale) as e:
        db.contenido_de_cotizacion_que_vale(crudo)
    return e.value.clave


def sin_float(texto: str):
    return json.loads(texto, parse_float=lambda s: pytest.fail(f"llegó un float al JSON: {s}"))


# ── 1. Lo que vale ──────────────────────────────────────────────────────────────────────────────────

def test_el_contenido_minimo_vale_y_sale_con_su_forma():
    salida = sin_float(db.contenido_de_cotizacion_que_vale(valido()))
    assert salida == {
        "opciones": [{"nombre": "Básica", "renglones": [
            {"descripcion": "Diseño de logo", "detalle": "", "total": "62500.00"}]}],
        "condiciones": [{"clave": "deposito", "texto": "Se paga la mitad al empezar.", "marcada": True}]}


@pytest.mark.parametrize("escrito,sale", [
    ("62,500", "62500.00"), ("62500", "62500.00"), ("62500.00", "62500.00"), ("62500.5", "62500.50"),
    ("62,500.50", "62500.50"), ("  1,000,000.1  ", "1000000.10"), ("0.01", "0.01"), ("0.5", "0.50"),
    ("99,999,999.99", "99999999.99"), ("007", "7.00"),
    (62500, "62500.00"), (Decimal("10.5"), "10.50"), (Decimal("1E+3"), "1000.00"),
])
def test_el_dinero_se_lee_a_la_dominicana(escrito, sale):
    assert db.total_de_cotizacion_que_vale(escrito) == sale
    contenido = valido(opciones=[opcion("A", reng(total=escrito))])
    assert sin_float(db.contenido_de_cotizacion_que_vale(contenido))["opciones"][0]["renglones"][0]["total"] == sale


def test_varias_opciones_renglones_y_condiciones_conservan_su_orden_y_acentos():
    contenido = {"opciones": [opcion("Opción ñandú", reng("Uno"), reng("Dos", "Con\ndetalle", "5")),
                              opcion("Otra", reng("Tres", total="1,000"))],
                 "condiciones": [cond("validez", "Válida 15 días.", True), cond(None, "Una extra mía.", False),
                                 cond("impuestos", "No incluye ITBIS.", False)]}
    salida = sin_float(db.contenido_de_cotizacion_que_vale(contenido))
    assert [o["nombre"] for o in salida["opciones"]] == ["Opción ñandú", "Otra"]
    assert [r["descripcion"] for r in salida["opciones"][0]["renglones"]] == ["Uno", "Dos"]
    assert salida["opciones"][0]["renglones"][1]["detalle"] == "Con\ndetalle"
    assert [(c["clave"], c["marcada"]) for c in salida["condiciones"]] == [
        ("validez", True), (None, False), ("impuestos", False)]


def test_los_saltos_del_navegador_se_guardan_como_n_y_se_recorta_alrededor():
    contenido = valido(opciones=[opcion("  Plan  ", reng("  Logo ", "a\r\nb\rc ", "5"))])
    r = sin_float(db.contenido_de_cotizacion_que_vale(contenido))["opciones"][0]
    assert r["nombre"] == "Plan" and r["renglones"][0]["descripcion"] == "Logo"
    assert r["renglones"][0]["detalle"] == "a\nb\nc"


def test_sin_condiciones_vale_tambien_sea_lista_vacia_o_ausente():
    assert sin_float(db.contenido_de_cotizacion_que_vale({"opciones": [opcion()]}))["condiciones"] == []
    assert sin_float(db.contenido_de_cotizacion_que_vale({"opciones": [opcion()], "condiciones": None}))["condiciones"] == []


def test_la_salida_es_idempotente():
    primera = db.contenido_de_cotizacion_que_vale(valido(opciones=[opcion("A", reng(total="1,234.5"))]))
    assert db.contenido_de_cotizacion_que_vale(json.loads(primera)) == primera


# ── 2. Lo que NO vale ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("total,clave", [
    ("0", "total_cero"), ("0.00", "total_cero"), ("-5", "total_formato"), ("-62,500", "total_formato"),
    (-5, "total_cero"), (0, "total_cero"), (Decimal("-1"), "total_cero"),
    ("abc", "total_formato"), ("12abc", "total_formato"), ("RD$ 5", "total_formato"), ("5 000", "total_formato"),
    ("NaN", "total_formato"), ("Infinity", "total_formato"), ("-Infinity", "total_formato"), ("1e3", "total_formato"),
    ("1,2", "total_formato"), ("1,23,456", "total_formato"), ("1234,567", "total_formato"), (",500", "total_formato"),
    ("5.", "total_formato"), ("5..0", "total_formato"), ("٣٤٥", "total_formato"), ("1_000", "total_formato"),
    ("62500.001", "total_decimales"), ("1.005", "total_decimales"), ("0.001", "total_decimales"),
    ("100,000,000", "total_alto"), ("99999999.995", "total_alto"), ("1" + "0" * 40, "total_alto"),
    (Decimal("NaN"), "total_formato"), (Decimal("Infinity"), "total_formato"), (Decimal("1E+100"), "total_alto"),
    (62500.0, "total_tipo"), (0.5, "total_tipo"), (True, "total_tipo"), (False, "total_tipo"), ([5], "total_tipo"),
    (b"5", "total_tipo"), ({"x": 1}, "total_tipo"),
])
def test_cada_total_que_no_vale_se_rechaza_con_su_clave(total, clave):
    assert clave_del_rechazo(valido(opciones=[opcion("A", reng(total=total))])) == clave


def test_un_total_vacio_en_un_renglon_con_descripcion_se_rechaza():
    assert clave_del_rechazo(valido(opciones=[opcion("A", reng("Logo", total=""))])) == "total_vacio"
    assert clave_del_rechazo(valido(opciones=[opcion("A", reng("Logo", total=None))])) == "total_vacio"


@pytest.mark.parametrize("crudo,clave", [
    (None, "forma"), ([], "forma"), ("texto", "forma"), (5, "forma"),
    ({}, "forma"), ({"opciones": None}, "forma"), ({"opciones": "x"}, "forma"),
    ({"opciones": []}, "sin_opciones"),
    ({"opciones": [opcion()], "otra": 1}, "forma"),
    ({"opciones": [opcion()], "condiciones": "x"}, "forma"),
    ({"opciones": ["no soy opción"]}, "forma"),
    ({"opciones": [{"nombre": "A", "renglones": "x"}]}, "forma"),
    ({"opciones": [{"nombre": "A", "renglones": [reng()], "extra": 1}]}, "forma"),
    ({"opciones": [{"nombre": "A", "renglones": ["no soy renglón"]}]}, "forma"),
    ({"opciones": [{"nombre": "A", "renglones": [{**reng(), "extra": 1}]}]}, "forma"),
    ({"opciones": [opcion()], "condiciones": ["no soy condición"]}, "forma"),
    ({"opciones": [opcion()], "condiciones": [{**cond(), "extra": 1}]}, "forma"),
])
def test_la_forma_que_no_es_se_rechaza(crudo, clave):
    assert clave_del_rechazo(crudo) == clave


def test_cada_opcion_necesita_nombre_y_renglones():
    assert clave_del_rechazo(valido(opciones=[{"nombre": "A", "renglones": []}])) == "sin_renglones"
    assert clave_del_rechazo(valido(opciones=[{"nombre": "A"}])) == "sin_renglones"
    assert clave_del_rechazo(valido(opciones=[{"nombre": "", "renglones": [reng()]}])) == "nombre_vacio"
    assert clave_del_rechazo(valido(opciones=[{"nombre": None, "renglones": [reng()]}])) == "nombre_vacio"
    assert clave_del_rechazo(valido(opciones=[opcion("A", reng(descripcion="", detalle="algo", total="5"))])) == "descripcion_vacio"
    # el detalle ausente (None) vale y queda vacío
    db.contenido_de_cotizacion_que_vale(valido(opciones=[opcion("A", reng(descripcion="Logo", detalle=None, total="5"))]))


@pytest.mark.parametrize("campo,hacer,clave,largo_ok", [
    ("nombre", lambda n: valido(opciones=[opcion("x" * n)]), "nombre_largo", 120),
    ("descripcion", lambda n: valido(opciones=[opcion("A", reng("x" * n))]), "descripcion_largo", 200),
    ("detalle", lambda n: valido(opciones=[opcion("A", reng(detalle="x" * n))]), "detalle_largo", 1000),
    ("condicion", lambda n: valido(condiciones=[cond(texto="x" * n)]), "condicion_largo", 300),
])
def test_cada_largo_pasa_en_su_tope_y_no_un_caracter_mas(campo, hacer, clave, largo_ok):
    db.contenido_de_cotizacion_que_vale(hacer(largo_ok))
    assert clave_del_rechazo(hacer(largo_ok + 1)) == clave


@pytest.mark.parametrize("sucio", ["a\x00b", "a\x07b", "a b", "a b", "a\ud800b", "a\x7fb", "a\x1bb"])
def test_ningun_texto_acepta_caracteres_que_no_se_guardan(sucio):
    casos = [valido(opciones=[opcion(sucio)]),
             valido(opciones=[opcion("A", reng(sucio))]),
             valido(opciones=[opcion("A", reng(detalle=sucio))]),
             valido(condiciones=[cond(texto=sucio)])]
    for c in casos:
        assert clave_del_rechazo(c) == "caracteres", c


@pytest.mark.parametrize("campo", ["nombre", "descripcion"])
def test_el_nombre_y_la_descripcion_son_de_una_linea_y_el_detalle_no(campo):
    en_nombre = valido(opciones=[opcion("uno\ndos")])
    en_descripcion = valido(opciones=[opcion("A", reng("uno\ndos"))])
    assert clave_del_rechazo(en_nombre if campo == "nombre" else en_descripcion) == "caracteres"
    db.contenido_de_cotizacion_que_vale(valido(opciones=[opcion("A", reng(detalle="uno\ndos"))]))


@pytest.mark.parametrize("valor", [5, 5.5, True, ["x"], {"a": 1}, b"x"])
def test_un_texto_que_no_es_texto_se_rechaza(valor):
    assert clave_del_rechazo(valido(opciones=[opcion(valor)])) == "tipo"
    assert clave_del_rechazo(valido(opciones=[opcion("A", reng(valor))])) == "tipo"
    assert clave_del_rechazo(valido(condiciones=[cond(texto=valor)])) == "tipo"


def test_las_condiciones_son_de_la_lista_cerrada_una_vez_cada_una_y_con_marcada_booleana():
    desconocida = "clave-que-no-existe"
    assert desconocida not in db.CLAVES_DE_CONDICION_DE_COTIZACION
    assert clave_del_rechazo(valido(condiciones=[cond(desconocida)])) == "condicion_clave"
    assert clave_del_rechazo(valido(condiciones=[cond("")])) == "condicion_clave"
    assert clave_del_rechazo(valido(condiciones=[cond(5)])) == "condicion_clave"
    assert clave_del_rechazo(valido(condiciones=[cond(["validez"])])) == "condicion_clave"
    for clave in db.CLAVES_DE_CONDICION_DE_COTIZACION:
        db.contenido_de_cotizacion_que_vale(valido(condiciones=[cond(clave)]))
    assert clave_del_rechazo(valido(condiciones=[cond("validez"), cond("validez", "otra")])) == "condicion_repetida"
    for malo in ("true", 1, 0, None, "sí"):
        assert clave_del_rechazo(valido(condiciones=[cond(marcada=malo)])) == "condicion_marcada"
    assert clave_del_rechazo(valido(condiciones=[cond("deposito", "")])) == "condicion_vacio"
    assert clave_del_rechazo(valido(condiciones=[cond("deposito", None)])) == "condicion_vacio"
    # Varias extras (clave None) sí pueden ir, con el mismo texto.
    db.contenido_de_cotizacion_que_vale(valido(condiciones=[cond(None, "x"), cond(None, "x")]))


def test_los_topes_de_cantidad_pasan_en_el_tope_y_no_uno_mas():
    opciones = lambda n: [opcion(f"Op {i}") for i in range(n)]
    db.contenido_de_cotizacion_que_vale(valido(opciones=opciones(db.MAX_OPCIONES_DE_COTIZACION)))
    assert clave_del_rechazo(valido(opciones=opciones(db.MAX_OPCIONES_DE_COTIZACION + 1))) == "opciones_de_mas"
    renglones = lambda n: [opcion("A", *[reng(f"R{i}") for i in range(n)])]
    db.contenido_de_cotizacion_que_vale(valido(opciones=renglones(db.MAX_RENGLONES_DE_OPCION)))
    assert clave_del_rechazo(valido(opciones=renglones(db.MAX_RENGLONES_DE_OPCION + 1))) == "renglones_de_mas"
    condiciones = lambda n: [cond(None, f"c{i}") for i in range(n)]
    db.contenido_de_cotizacion_que_vale(valido(condiciones=condiciones(db.MAX_CONDICIONES_DE_COTIZACION)))
    assert clave_del_rechazo(valido(condiciones=condiciones(db.MAX_CONDICIONES_DE_COTIZACION + 1))) == "condiciones_de_mas"


def test_los_topes_no_se_cuentan_con_lo_vacio_que_se_descarta():
    de_sobra = [{"descripcion": "", "detalle": "", "total": ""}] * 30
    r = db.contenido_de_cotizacion_que_vale(valido(opciones=[opcion("A", reng(), *de_sobra)]))
    assert len(sin_float(r)["opciones"][0]["renglones"]) == 1


# ── 3. Lo vacío de sobra se descarta ────────────────────────────────────────────────────────────────

def test_lo_vacio_que_dibuja_el_formulario_sin_javascript_no_se_guarda():
    vacio = {"descripcion": "  ", "detalle": None, "total": ""}
    crudo = {"opciones": [opcion("Plan", reng(), vacio, vacio), {"nombre": "", "renglones": [vacio, vacio]},
                          {"nombre": "  ", "renglones": []}, {"nombre": None}],
             "condiciones": [cond(), {"clave": None, "texto": "", "marcada": False},
                             {"clave": None, "texto": None, "marcada": None}]}
    salida = sin_float(db.contenido_de_cotizacion_que_vale(crudo))
    assert len(salida["opciones"]) == 1 and len(salida["opciones"][0]["renglones"]) == 1
    assert len(salida["condiciones"]) == 1


def test_todo_vacio_es_sin_opciones_y_una_opcion_con_nombre_pero_sin_nada_no_se_inventa_renglon():
    vacio = {"descripcion": "", "detalle": "", "total": ""}
    assert clave_del_rechazo({"opciones": [{"nombre": "", "renglones": [vacio]}], "condiciones": []}) == "sin_opciones"
    assert clave_del_rechazo({"opciones": [{"nombre": "Plan", "renglones": [vacio]}]}) == "sin_renglones"


# ── 4-6. Sin float, sin eco, sin tocar lo que llega ─────────────────────────────────────────────────

def test_el_rechazo_no_repite_lo_que_se_pidio():
    secreto = "SECRETO-ZXQ-12345"
    casos = [valido(opciones=[opcion(secreto + "\x00")]), valido(opciones=[opcion(secreto * 50)]),
             valido(opciones=[opcion("A", reng(secreto * 20))]), valido(opciones=[opcion("A", reng(total=secreto))]),
             valido(opciones=[opcion("A", reng(total=Decimal("0")))]), valido(condiciones=[cond(secreto + "\x00")]),
             valido(condiciones=[cond("deposito", secreto * 50)]), {"opciones": [opcion()], secreto: 1}]
    for c in casos:
        with pytest.raises(NoVale) as e:
            db.contenido_de_cotizacion_que_vale(c)
        assert secreto not in str(e.value) and secreto not in e.value.clave, c


def test_la_puerta_no_modifica_lo_que_recibe():
    crudo = valido(opciones=[opcion("  A ", reng(" x ", "d\r\n", "1,000"))])
    antes = copy.deepcopy(crudo)
    db.contenido_de_cotizacion_que_vale(crudo)
    assert crudo == antes


def test_un_texto_de_ejemplo_con_comillas_y_etiquetas_sale_tal_cual_en_el_json():
    texto = 'Logo "grande" <b>&</b> \\ barra'
    salida = sin_float(db.contenido_de_cotizacion_que_vale(valido(opciones=[opcion(texto)])))
    assert salida["opciones"][0]["nombre"] == texto


# ── 7. Entradas inventadas ──────────────────────────────────────────────────────────────────────────

def test_con_totales_inventados_lo_aceptado_es_dinero_valido_y_lo_demas_se_rechaza_con_la_puerta():
    azar = random.Random(20261014)
    alfabeto = "0123456789,.-+eE $RD"
    aceptados = 0
    for _ in range(4000):
        escrito = "".join(azar.choice(alfabeto) for _ in range(azar.randint(0, 12)))
        try:
            sale = db.total_de_cotizacion_que_vale(escrito)
        except NoVale:
            continue
        aceptados += 1
        valor = Decimal(sale)
        assert 0 < valor <= db.TOPE_TOTAL_DE_RENGLON
        assert valor == valor.quantize(Decimal("0.01")) and sale == f"{valor:.2f}"
        assert Decimal(escrito.strip().replace(",", "")) == valor
    assert aceptados > 100, "la semilla ya no ejercita el camino de aceptar"
