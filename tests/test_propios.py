"""Tests del registro de la casa (cerebro/bancos/propios.py).

Lo que se prueba es la decisión que evita contar la misma plata dos veces: si
los dos lados de una transferencia son de la casa, no es gasto ni ingreso.

Los patrones de estos tests son los que los bancos usan de verdad para los
mismos titulares — cinco grafías distintas del mismo nombre, incluida una sin
espacio. No son variantes inventadas.

Correr:  python3 tests/test_propios.py
"""
from __future__ import annotations

import email
import os
import pathlib
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal
from email.header import decode_header
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.contrato import Movimiento  # noqa: E402
from cerebro.bancos.propios import Propios, normalizar  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Los patrones que harían falta en la tabla `cuentas_propias`. Aquí van a mano
# porque el módulo es lógica pura: la carga desde la base es de otra capa.
CASA = ["ROSILIS", "FAJARDOVARGAS", "TIZIANOFAJARDO"]


def _mov(**kw):
    base = dict(banco="apap", canal="transferencia", tipo="gasto",
                fecha=datetime(2026, 5, 1, 10, 0), monto=Decimal("1000.00"),
                moneda="DOP", contraparte="UN TERCERO CUALQUIERA",
                estado="aprobada", referencia="x")
    base.update(kw)
    return Movimiento(**base)


# ── Normalización: las cinco grafías del mismo titular ───────────────────

def test_las_grafias_reales_de_los_bancos():
    """Los cinco textos son literales de correos reales del corpus."""
    reg = Propios(CASA)
    for texto in ("ROSILIS YANELY ROMERO JIMENEZ",
                  "ROSILISYANELY ROMERO JIMENEZ",   # Banreservas, sin espacio
                  "SRA ROSILIS Y ROMERO",
                  "Rosilis Romero",
                  "ROSILIS ROMERO, DOP - ***8354"):
        assert reg.es_de_la_casa(texto), texto


def test_no_casa_con_ajenos():
    reg = Propios(CASA)
    for texto in ("WENDY MARISOL CANELA CRUZ", "JOSE DOLORES FAJARDO REYES",
                  "PASTELITOS DON MALLEN SRL", "SM NACIONAL MAXIMO GOM", ""):
        assert not reg.es_de_la_casa(texto), texto


def test_patron_corto_se_rechaza():
    """"ANA" casaría dentro de "BANANA" y dentro de medio directorio."""
    try:
        Propios(["ANA"])
    except ValueError:
        return
    raise AssertionError("aceptó un patrón de 3 caracteres")


def test_jose_dolores_fajardo_no_es_tiziano_fajardo():
    """Caso real del corpus: un beneficiario apellidado FAJARDO que NO es de la
    casa. Por eso el patrón es "FAJARDOVARGAS" y no "FAJARDO"."""
    reg = Propios(CASA)
    assert not reg.es_de_la_casa("JOSE DOLORES FAJARDO REYES")
    assert reg.es_de_la_casa("TIZIANO FAJARDO VARGAS")


# ── La decisión ──────────────────────────────────────────────────────────

def test_los_dos_lados_de_la_casa_es_traspaso():
    """Caso real: RD$44,000 de Rosi a Rosi, hoy marcado gasto."""
    reg = Propios(CASA)
    m = reg.reclasificar(_mov(
        contraparte="ROSILIS YANELY ROMERO JIMENEZ → ROSILIS YANELY ROMERO JIMENEZ",
        monto=Decimal("44000.00")))
    assert m.tipo == "transferencia"


def test_origen_nuestro_destino_ajeno_es_gasto():
    reg = Propios(CASA)
    m = reg.reclasificar(_mov(
        contraparte="ROSILIS YANELY ROMERO JIMENEZ → WENDY MARISOL CANELA CRUZ"))
    assert m.tipo == "gasto"


def test_cliente_pagando_al_estudio_es_ingreso():
    """El caso más caro por unidad y el que el sistema tenía al revés: un
    cliente paga una sesión de grabación y se anotaba como gasto del estudio."""
    reg = Propios(CASA)
    m = reg.reclasificar(_mov(
        contraparte="JOSE APOLINAR BRETON FERNANDEZ → SRA ROSILIS Y ROMERO",
        monto=Decimal("500.00"), tipo="gasto"))
    assert m.tipo == "ingreso", "un cliente pagando no es un gasto"


def test_sin_flecha_y_de_la_casa_es_traspaso():
    """Cuando el parser solo conoce una contraparte y esa es de la casa, el otro
    extremo somos nosotros: el correo llegó a un buzón nuestro."""
    reg = Propios(CASA)
    m = reg.reclasificar(_mov(contraparte="ROSILIS YANELY ROMERO JIMENEZ"))
    assert m.tipo == "transferencia"


def test_los_consumos_de_tarjeta_no_se_tocan():
    """Un comercio que se llame como alguien de la casa sigue siendo un gasto.
    `canal='tarjeta'` lo dice sin ambigüedad."""
    reg = Propios(CASA)
    m = reg.reclasificar(_mov(canal="tarjeta", tipo="gasto",
                              contraparte="ROSILIS BEAUTY SALON"))
    assert m.tipo == "gasto"


def test_no_toca_lo_que_no_es_de_la_casa():
    reg = Propios(CASA)
    original = _mov(contraparte="PASTELITOS DON MALLEN SRL")
    assert reg.reclasificar(original) is original


# ── Regresiones: lo que encontró el verificador el 30-ago-2026 ───────────

def test_patron_de_cuatro_digitos_se_acepta():
    """La migración 002 documenta "8354" como patrón válido de clase 'cuenta',
    pero LARGO_MINIMO=5 lo rechazaba: las clases cuenta y tarjeta que la tabla
    crea eran inregistrables, y cargar una fila así hacía reventar desde_filas()
    y con ella el registro entero. Todos los enmascarados del corpus son de
    cuatro dígitos: ***8354, ••9639, terminada en 9854."""
    reg = Propios(["8354"])
    assert reg.es_de_la_casa("ROSILIS ROMERO, DOP - ***8354")
    assert not reg.es_de_la_casa("cuenta ****9639")


def test_cuatro_letras_sigue_rechazandose():
    """La excepción es solo para dígitos. Un nombre de 4 letras casaría con
    medio directorio."""
    try:
        Propios(["ANAX"])
    except ValueError:
        return
    raise AssertionError("aceptó un patrón alfabético de 4 caracteres")


def test_banesco_pone_al_beneficiario_no_solo_al_banco():
    """Caso real: rosilisr04_30632.eml, RD$35,000 con Concepto "PAGO TC" —el
    pago de la tarjeta— hacia una cuenta cuya titular es Rosi. Banesco ponía en
    contraparte "BANCO MULTIPLE BHD S.A." y descartaba "Nombre del
    Beneficiario", así que el matcher no tenía ningún nombre que reconocer y el
    traspaso se contaba como gasto real."""
    from cerebro.bancos.banesco import parsear_realizada
    from cerebro.bancos.contrato import CorreoCrudo
    texto = ("Hola , Nos place informarte que la transacción vía pago al instante "
             "realizada en fecha 25/05/2026 03:24 PM fue enviada al beneficiario. "
             "Monto: DOP35,000.00 Cuenta Origen: DO73BANS00000000098200073555 "
             "Fecha de la Transacción: 25/05/2026 No. Referencia: E000073.A9895595 "
             "Banco Beneficiario: BANCO MULTIPLE BHD S.A. Cuenta Destino: "
             "DO47BCBH00000000025226880013 Nombre del Beneficiario: ROSILIS YANELY "
             "ROMERO JIMENEZ Concepto: PAGO TC")
    c = CorreoCrudo(remitente="notificaciones@banesco.com.do",
                    asunto="Notificación de Transferencia Realizada",
                    fecha_correo=datetime(2026, 5, 25), html="", texto=texto,
                    cuenta="rosilisr04@gmail.com", uid="1")
    m = parsear_realizada(c)[0]
    assert "ROSILIS" in m.contraparte.upper(), (
        f"sin el beneficiario, el registro no puede reconocerlo: {m.contraparte!r}")
    assert Propios(CASA).reclasificar(m).tipo == "transferencia", (
        "un pago de tarjeta a cuenta propia no es un gasto nuevo")


# ── Capa 2: el impacto sobre los movimientos reales ──────────────────────

def _cab(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def test_impacto_sobre_los_movimientos_reales():
    """Cuántos movimientos reales cambian de tipo con el registro puesto. Es la
    medida de para qué sirve esto."""
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return
    import cerebro.bancos as B

    reg = Propios(CASA)
    cambios, total = Counter(), 0
    for f in sorted(FIXTURES.rglob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        frm = _cab(msg.get("From"))
        addr = frm.split("<")[-1].strip("> ").lower() if "<" in frm else frm.lower()
        asunto = _cab(msg.get("Subject")).strip()
        fn = B.buscar_parser(addr, asunto)
        if fn is None:
            continue
        plano = html = ""
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            if p.get_content_maintype() != "text":
                continue
            d = (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/plain" and not plano:
                plano = d
            elif p.get_content_type() == "text/html" and not html:
                html = d
        try:
            fc = parsedate_to_datetime(msg.get("Date")).replace(tzinfo=None)
        except Exception:
            fc = datetime(2026, 1, 1)
        try:
            movs = fn(B.CorreoCrudo(remitente=addr, asunto=asunto, fecha_correo=fc,
                                    html=html, texto=plano, cuenta="x", uid=f.stem))
        except Exception:
            continue
        for mv in movs:
            total += 1
            nuevo = reg.reclasificar(mv)
            if nuevo.tipo != mv.tipo:
                cambios[f"{mv.tipo} → {nuevo.tipo}"] += 1

    print(f"     ({total} movimientos, {sum(cambios.values())} reclasificados: "
          f"{dict(cambios)})")
    assert total > 400, f"esperaba más de 400 movimientos, hubo {total}"
    assert cambios["gasto → transferencia"] >= 20, (
        "las transferencias de APAP donde la beneficiaria es la titular tienen "
        "que dejar de contarse como gasto")
    assert cambios["gasto → ingreso"] >= 2, (
        "los pagos de clientes al estudio tienen que dejar de contarse como gasto")
    # Cota EXACTA, no inferior. Con solo cotas inferiores, un movimiento que
    # debía reclasificarse y no se reclasificó no movía ningún número: así se
    # escapó el pago de tarjeta de Banesco por RD$35,000.
    assert sum(cambios.values()) == 31, (
        f"esperaba 31 reclasificados, hubo {sum(cambios.values())}: "
        f"{dict(cambios)}")


if __name__ == "__main__":
    fallidos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {nombre}")
        except AssertionError as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {e or 'assert falló'}")
        except Exception as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {type(e).__name__}: {e}")
    print(f"\n{'FALLARON ' + str(fallidos) if fallidos else 'Todo verde'}")
    sys.exit(1 if fallidos else 0)
