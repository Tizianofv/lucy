"""La columna de dinero y los totales del bloque de sesiones y trabajos (parte
10 de la página de un proyecto, 9-oct-2026): las garantías de la fila 10 del
diseño aprobado.

QUÉ SE VIGILA, una prueba por garantía (más los bordes de cada fila de la tabla
del diseño 3.8, que son parte de la misma garantía):
  · la tabla de 3.8 fila por fila, con `Decimal`;
  · un caso que no encaja no se pinta verde ni rojo;
  · los totales son la suma de lo pintado;
  · se ve igual en la sesión de la casa y en la de solo ver.
Más: el formato del dinero es el de la casa para plata, y una cifra que no es un
número no se pinta.

CÓMO. La función pura se llama de frente con valores fijos. La página se pide por
HTTP por la ruta real y la plantilla real, con la App de registro doblada EN LA
RED (`tests/_app_de_registro.py`), como en la parte 9: nada reemplaza al lector.
La base es el modelo de prueba (`_proyectos_de_prueba.BaseQueNoSeToca`).

FRONTERA, dicha para que no se dé por cubierta: el doble afirma la FORMA de la
respuesta de la App, no de dónde saca cada cifra ni con qué regla arma el saldo.
Que las cifras que la App manda de verdad cuadren (`abonado + saldo == total`) no
se comprobó contra la App; si no cuadraran, cada renglón saldría «revísala en el
registro», que es justo lo que esta parte contesta en ese caso.

Correr:  python3 -m pytest tests/test_dinero_de_proyecto.py -q
"""
from __future__ import annotations

import os
import re
from decimal import Decimal

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import registro_lectura  # noqa: E402
from test_sesiones_de_proyecto import (  # noqa: E402,F401
    HOY, _abrir, _bloque, _respuesta, _sin_los_controles, registro)

D = Decimal


# ═══════════════════════════════════════════════════════════════════════
# Garantía: la tabla de 3.8, fila por fila, con `Decimal`
# ═══════════════════════════════════════════════════════════════════════

# Lo que llega | caso | clase (el color) | lo que se pinta grande
LA_TABLA = [
    ("saldo 0 y total mayor que 0", D("9000"), D("9000"), D("0"), "pago", "pago", D("9000")),
    ("saldo mayor que 0 y abonado 0", D("6000"), D("0"), D("6000"), "debe", "debe", D("6000")),
    ("saldo mayor que 0 y abonado mayor que 0", D("9000"), D("3000"), D("6000"),
     "debe_con_abono", "debe", D("6000")),
    ("sin total todavía (cero)", D("0"), D("0"), D("0"), "sin_total", "", None),
    ("sin total todavía (no vino)", None, None, None, "sin_total", "", None),
    ("saldo negativo", D("9000"), D("10000"), D("-1000"), "revisar", "", None),
    ("cifras que no cuadran", D("5000"), D("4000"), D("4000"), "revisar", "", None),
]


@pytest.mark.parametrize("fila,total,abonado,saldo,caso,clase,monto",
                         LA_TABLA, ids=[f[0] for f in LA_TABLA])
def test_la_tabla_del_diseno_fila_por_fila(fila, total, abonado, saldo, caso, clase, monto):
    """Cada fila de la tabla de 3.8, con sus valores, da el caso y el color que dice."""
    pintado = registro_lectura.dinero_de_sesion(total, abonado, saldo)
    assert pintado["caso"] == caso, fila
    assert pintado["clase"] == clase, fila
    assert pintado["monto"] == monto, fila


def test_las_cifras_de_la_nota_son_las_que_llegan():
    """«pagó X de TOTAL»: X es el abonado y TOTAL el total, los dos en `Decimal`."""
    pintado = registro_lectura.dinero_de_sesion(D("9000"), D("3000"), D("6000"))
    assert pintado["abonado"] == D("3000") and pintado["total"] == D("9000")
    assert isinstance(pintado["monto"], Decimal)


# ═══════════════════════════════════════════════════════════════════════
# Los bordes de cada fila: cero, negativo, no cuadra, falta una cifra
# ═══════════════════════════════════════════════════════════════════════

BORDES = [
    # verde: apenas no debe nada y hay total
    ("pago: todo pagado", D("100"), D("100"), D("0"), "pago"),
    ("pago: un centavo de total", D("0.01"), D("0.01"), D("0"), "pago"),
    # rojo: debe todo
    ("debe: nada pagado", D("100"), D("0"), D("100"), "debe"),
    # rojo con la nota: pagó una parte
    ("debe_con_abono: un centavo pagado", D("100"), D("0.01"), D("99.99"), "debe_con_abono"),
    # sin total todavía
    ("sin total: no vino el total", None, D("0"), D("0"), "sin_total"),
    ("sin total: el total es cero", D("0"), D("0"), D("0"), "sin_total"),
    # cualquier otra cosa
    ("saldo negativo (pagó de más)", D("100"), D("150"), D("-50"), "revisar"),
    ("total negativo", D("-100"), D("0"), D("-100"), "revisar"),
    ("abonado negativo", D("100"), D("-10"), D("110"), "revisar"),
    ("falta el abonado", D("100"), None, D("100"), "revisar"),
    ("falta el saldo", D("100"), D("0"), None, "revisar"),
    ("falta el total y hay abonado", None, D("50"), D("-50"), "sin_total"),
    ("no cuadra: sobra total", D("100"), D("40"), D("50"), "revisar"),
    ("no cuadra: falta total", D("100"), D("60"), D("60"), "revisar"),
    ("verde sin cuadrar (abonado de más)", D("100"), D("100.01"), D("0"), "revisar"),
]


@pytest.mark.parametrize("borde,total,abonado,saldo,caso", BORDES,
                         ids=[b[0] for b in BORDES])
def test_los_bordes_de_cada_fila(borde, total, abonado, saldo, caso):
    """Cero, negativo, falta una cifra o no cuadra: no se adivina un color."""
    assert registro_lectura.dinero_de_sesion(total, abonado, saldo)["caso"] == caso, borde


@pytest.mark.parametrize("total,abonado,saldo,esperadas", [
    (D("3000"), D("1000"), D("1000"), [D("3000"), D("1000"), D("1000")]),   # no cuadra
    (D("3000"), None, D("3000"), [D("3000"), D("3000")]),                   # falta el abonado
    (D("3000"), D("0"), None, [D("3000"), D("0")]),                         # falta el saldo
    (D("-1"), D("0"), D("-1"), [D("-1"), D("0"), D("-1")]),                 # negativo
])
def test_lo_que_no_encaja_muestra_las_cifras_que_vinieron(total, abonado, saldo, esperadas):
    """Las cifras del renglón que no encaja: las que llegaron, ninguna inventada."""
    assert registro_lectura.dinero_de_sesion(total, abonado, saldo)["cifras"] == esperadas


def test_un_sin_total_no_muestra_ninguna_cifra():
    """«—» de verdad: nada que enseñar, y no aporta a los totales."""
    pintado = registro_lectura.dinero_de_sesion(D("0"), D("0"), D("0"))
    assert pintado["monto"] is None and pintado["cifras"] == []
    assert pintado["aporta"] == (None, None, None)


def test_el_caso_que_no_encaja_no_es_ni_verde_ni_rojo():
    """Ningún borde que no encaje puede salir con la clase del verde o del rojo."""
    for borde, total, abonado, saldo, caso in BORDES:
        pintado = registro_lectura.dinero_de_sesion(total, abonado, saldo)
        if caso == "revisar":
            assert pintado["clase"] == "", f"{borde} salió con color {pintado['clase']!r}"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: con `Decimal`, sin pasar por `float` al hacer cuentas
# ═══════════════════════════════════════════════════════════════════════

def test_las_cifras_que_llegan_como_float_no_arrastran_la_sobra_del_float():
    """0.3 = 0.1 + 0.2. Convertido con `Decimal(0.1) + Decimal(0.2)` eso es falso
    (el float guarda otra cosa) y el renglón saldría «revísala»."""
    fila = {"total": 0.3, "abonado": 0.1, "saldo": 0.2, "cancelada": False}
    pintado = registro_lectura._renglon(fila)["dinero"]
    assert pintado["caso"] == "debe_con_abono", pintado
    assert pintado["monto"] == D("0.2")


def test_un_renglon_trae_sus_tres_cifras_como_decimal():
    """Lo que el lector deja pasar son `Decimal`, no `float`."""
    renglon = registro_lectura._renglon(
        {"total": 2000.0, "abonado": 500.0, "saldo": 1500.0, "cancelada": False})
    for clave in ("total", "abonado", "saldo"):
        assert isinstance(renglon[clave], Decimal), clave
    assert (renglon["total"], renglon["abonado"], renglon["saldo"]) == (D("2000"), D("500"), D("1500"))


@pytest.mark.parametrize("valor", ["2000", True, None, float("nan"), float("inf"), [], {}, "x"])
def test_una_cifra_que_no_es_un_numero_cuenta_como_no_vino(valor):
    """Un valor que no es un número no se convierte en 0: no vino."""
    assert registro_lectura._renglon({"total": valor, "cancelada": False})["total"] is None


# ═══════════════════════════════════════════════════════════════════════
# Garantía: los totales son la suma de lo pintado
# ═══════════════════════════════════════════════════════════════════════

def _renglon(total, abonado, saldo):
    return registro_lectura._renglon(
        {"total": total, "abonado": abonado, "saldo": saldo, "cancelada": False})


def test_los_totales_son_la_suma_de_lo_que_aporta_cada_renglon():
    renglones = [_renglon(9000.0, 9000.0, 0.0),        # pagada
                 _renglon(6000.0, 0.0, 6000.0),         # debe todo
                 _renglon(9000.0, 3000.0, 6000.0),      # pagó una parte
                 _renglon(5000.0, 4000.0, 4000.0)]      # no cuadra
    assert registro_lectura.totales_de_sesiones(renglones) == {
        "facturado": D("29000"), "cobrado": D("16000"), "por_cobrar": D("16000")}


def test_un_renglon_sin_total_no_aporta_nada_a_los_totales():
    """El que pinta «—» no suma, y no se le inventa un cero."""
    renglones = [_renglon(9000.0, 9000.0, 0.0), _renglon(None, None, None)]
    assert registro_lectura.totales_de_sesiones(renglones) == {
        "facturado": D("9000"), "cobrado": D("9000"), "por_cobrar": D("0")}


def test_sin_renglones_los_totales_son_cero():
    assert registro_lectura.totales_de_sesiones([]) == {
        "facturado": D("0"), "cobrado": D("0"), "por_cobrar": D("0")}


# ═══════════════════════════════════════════════════════════════════════
# Por el camino de producción: la página, con la App doblada en la red
# ═══════════════════════════════════════════════════════════════════════

PAGADA = {"ref": 101, "codigo": "a101", "es_trabajo": False, "fecha": HOY, "servicio": "Grabación",
          "cancelada": False, "total": 9000.0, "abonado": 9000.0, "saldo": 0.0}
DEBE = {"ref": 102, "codigo": "b102", "es_trabajo": False, "fecha": HOY, "servicio": "Voces",
        "cancelada": False, "total": 6000.0, "abonado": 0.0, "saldo": 6000.0}
PARCIAL = {"ref": 103, "codigo": "c103", "es_trabajo": False, "fecha": HOY, "servicio": "Mezcla",
           "cancelada": False, "total": 9000.0, "abonado": 3000.0, "saldo": 6000.0}
SIN_TOTAL = {"ref": 104, "codigo": "d104", "es_trabajo": False, "fecha": HOY, "servicio": "Ensayos",
             "cancelada": False}
NO_CUADRA = {"ref": 105, "codigo": "e105", "es_trabajo": False, "fecha": HOY, "servicio": "Máster",
             "cancelada": False, "total": 5000.0, "abonado": 4000.0, "saldo": 4000.0}
CANCELADA = {"ref": 106, "codigo": "f106", "es_trabajo": False, "fecha": HOY, "servicio": "Cancelada",
             "cancelada": True, "total": 999999.0, "abonado": 0.0, "saldo": 999999.0}
DE_CADA_CLASE = (PAGADA, DEBE, PARCIAL, SIN_TOTAL, NO_CUADRA)


def test_cada_fila_de_la_tabla_en_la_pagina(registro, monkeypatch):
    """Lo que se pinta en el renglón, texto exacto, para cada fila de la tabla."""
    registro.sesiones = _respuesta(sesiones=DE_CADA_CLASE)
    bloque = _bloque(_abrir(monkeypatch))
    assert '<span class="plata pago">RD$ 9,000.00</span>' in bloque, bloque
    assert '<span class="plata debe">RD$ 6,000.00</span>' in bloque, bloque
    assert ('<span class="plata debe">RD$ 6,000.00'
            '<small>pagó RD$ 3,000.00 de 9,000.00</small></span>') in bloque, bloque
    assert '<span class="plata">—</span>' in bloque, bloque
    assert ('<span class="plata">RD$ 5,000.00 · RD$ 4,000.00 · RD$ 4,000.00'
            '<small>revísala en el registro</small></span>') in bloque, bloque


def test_el_que_no_encaja_no_lleva_la_clase_del_color(registro, monkeypatch):
    """«no se pinta verde ni rojo»: la clase del verde y la del rojo no están en su renglón."""
    registro.sesiones = _respuesta(sesiones=(NO_CUADRA,))
    bloque = _bloque(_abrir(monkeypatch))
    assert "revísala en el registro" in bloque
    assert 'class="plata pago"' not in bloque and 'class="plata debe"' not in bloque, bloque
    assert re.search(r'<span class="plata">RD\$ 5,000\.00', bloque), bloque


def test_los_totales_del_bloque_y_la_cancelada_no_cuenta(registro, monkeypatch):
    """Los tres totales, y una cancelada con una cifra enorme no mueve ninguno."""
    registro.sesiones = _respuesta(sesiones=DE_CADA_CLASE + (CANCELADA,))
    bloque = _bloque(_abrir(monkeypatch))
    for total in ("RD$ 29,000.00", "RD$ 16,000.00"):
        assert f"<b>{total}</b>" in bloque, bloque
    assert bloque.count("RD$ 16,000.00") == 2, bloque      # cobrado y por cobrar
    assert "999,999" not in bloque, "la cancelada contó en los totales"
    assert "f106" not in bloque, "salió una sesión cancelada"

    # Y la otra cara: sin la cancelada, los totales son los MISMOS.
    registro.sesiones = _respuesta(sesiones=DE_CADA_CLASE)
    sin_cancelada = _bloque(_abrir(monkeypatch))
    for total in ("RD$ 29,000.00", "RD$ 16,000.00"):
        assert f"<b>{total}</b>" in sin_cancelada


def test_los_totales_son_la_suma_de_los_renglones_pintados(registro, monkeypatch):
    """El número de cada total, sacado de los renglones que la lista pinta."""
    registro.sesiones = _respuesta(sesiones=DE_CADA_CLASE)
    bloque = _bloque(_abrir(monkeypatch))
    de_los_que_vinieron = {
        "facturado": sum(r["total"] for r in DE_CADA_CLASE if r.get("total")),
        "cobrado": sum(r["abonado"] for r in DE_CADA_CLASE if r.get("abonado") is not None),
        "por cobrar": sum(r["saldo"] for r in DE_CADA_CLASE if r.get("saldo") is not None),
    }
    for nombre, cifra in de_los_que_vinieron.items():
        assert f"<b>RD$ {cifra:,.2f}</b><span>{nombre}</span>" in bloque, (nombre, bloque)


def test_el_dinero_tiene_el_formato_de_la_casa(registro, monkeypatch):
    """Miles con coma y dos decimales, como el `pesos` del panel."""
    registro.sesiones = _respuesta(sesiones=({"ref": 107, "codigo": "g107", "es_trabajo": False,
                                              "fecha": HOY, "cancelada": False,
                                              "total": 1234567.5, "abonado": 1234567.5, "saldo": 0.0},))
    bloque = _bloque(_abrir(monkeypatch))
    assert "RD$ 1,234,567.50" in bloque, bloque


def test_se_ve_igual_en_la_casa_y_en_solo_ver(registro, monkeypatch):
    """El dinero no es un control: la casa y la de solo ver ven lo mismo (parte 11: los controles
    de quitar y devolver son lo único que la de solo ver no lleva)."""
    registro.sesiones = _respuesta(sesiones=DE_CADA_CLASE)
    casa = _bloque(_abrir(monkeypatch, con="casa"))
    ver = _bloque(_abrir(monkeypatch, con="ver"))
    assert ver == _sin_los_controles(casa), "el bloque con dinero no se ve igual en las dos sesiones"
    assert "RD$ 29,000.00" in ver and "RD$ 16,000.00" in ver


def test_una_cifra_hostil_no_se_pinta(registro, monkeypatch):
    """El dinero viene de la red: lo que no es un número cuenta como «no vino»."""
    hostil = {"ref": 108, "codigo": "h108", "es_trabajo": False, "fecha": HOY,
              "cancelada": False, "total": "<b>hostil</b>", "abonado": "1", "saldo": None}
    registro.sesiones = _respuesta(sesiones=(hostil,))
    bloque = _bloque(_abrir(monkeypatch))
    assert "hostil" not in bloque, bloque
    assert '<span class="plata">—</span>' in bloque, bloque


def test_sin_renglones_no_hay_totales(registro, monkeypatch):
    """Sin nada que sumar no se pinta un bloque de ceros."""
    registro.sesiones = _respuesta(sesiones=())
    bloque = _bloque(_abrir(monkeypatch))
    assert "El registro no tiene sesiones ni trabajos de este cliente." in bloque
    assert "facturado" not in bloque and "RD$ 0.00" not in bloque, bloque
