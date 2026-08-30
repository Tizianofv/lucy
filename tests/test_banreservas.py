"""Tests del parser de Banreservas (cerebro/bancos/banreservas.py).

Banreservas manda cuatro cosas distintas bajo el mismo remitente Y el mismo
asunto, así que casi todos los tests de acá prueban la DISCRIMINACIÓN: que un
pago de nómina no acabe contado como gasto de tarjeta.

Igual que en BHD, dos capas: casos armados para los formatos que el banco
todavía no manda, y los 58 correos reales, que son los únicos que pueden
desmentirme. Los fixtures están fuera de git; sin ellos esa capa se salta.

Correr:  python3 tests/test_banreservas.py
"""
from __future__ import annotations

import email
import os
import pathlib
import sys
from datetime import datetime
from decimal import Decimal
from email.header import decode_header

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.banreservas import parsear  # noqa: E402
from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    buscar_parser,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "banreservas"
REMITENTE = "notificaciones@banreservas.com"
ASUNTO = "Notificaciones Banreservas"

CONSUMO = ("Notificación de Consumo Su tarjeta VISA PLATINUM ••8110 presenta un "
           "consumo. Monto: DOP 254.90 Estado: APROBADO Comercio: SM NACIONAL "
           "MAXIMO GOM SANTO DOMINGODO Fecha de transacción: 17/04/2026 10:28 AM "
           "Número de aprobación: 299209 Recibido por los valores indicados")

EN_PROCESO = ("Transferencia en proceso Su tarjeta VISA PLATINUM ••8110 presenta "
              "un consumo. Monto: DOP 350.00 Remitente: Fecha: 14/06/2026 05:33 PM "
              "Recibido por los valores indicados")

RECIBIDA = ("Transferencia LBTR Recibida Transferencia Recibida Te notificamos que "
            "la siguiente transferencia fue recibida: Monto: RD$ 1,500.00 "
            "Transacción: Pago al instante BCRD Origen: ROSILIS YANELY ROMERO "
            "JIMENEZ Banco Origen: BANCO BHD S.A. Destino: Cuenta de Ahorros •••• "
            "8354 Fecha: 27/07/2026 09:59 AM Recibido por los valores indicados")

NOMINA = ("Notificación Pago Nómina Le notificamos que se le presenta un pago de "
          "nómina a su cuenta: Monto: DOP$ 12,267.85 Cuenta: Cuenta de Ahorros "
          "•••• 5633 Fecha de transacción: 28/05/2026 12:55 PM Recibido por los "
          "valores indicados")


def _correo(texto: str) -> CorreoCrudo:
    return CorreoCrudo(remitente=REMITENTE, asunto=ASUNTO,
                       fecha_correo=datetime(2026, 1, 1), html="", texto=texto,
                       cuenta="rosilisr04@gmail.com", uid="1")


def _revienta(fn, *a) -> bool:
    try:
        fn(*a)
    except ErrorDeParseo:
        return True
    return False


# ── Discriminación: lo que de verdad puede salir mal ─────────────────────

def test_consumo():
    m = parsear(_correo(CONSUMO))[0]
    assert m.canal == "tarjeta" and m.tipo == "gasto" and m.estado == "aprobada"
    assert m.monto == Decimal("254.90") and m.moneda == "DOP"
    assert m.contraparte == "SM NACIONAL MAXIMO GOM SANTO DOMINGODO"
    assert m.fecha == datetime(2026, 4, 17, 10, 28)


def test_comercio_no_se_come_la_fecha():
    """Sin cortar en la siguiente etiqueta, "Comercio:" se llevaría dentro
    "Fecha de transacción: 17/04/2026" y el nombre del comercio sería basura."""
    m = parsear(_correo(CONSUMO))[0]
    assert "Fecha" not in m.contraparte and "2026" not in m.contraparte


def test_nomina_es_ingreso_no_gasto():
    """El fallo más caro posible acá: un pago de nómina contado como gasto de
    tarjeta invierte el signo de RD$12,267.85."""
    m = parsear(_correo(NOMINA))[0]
    assert m.tipo == "ingreso" and m.canal == "nomina"
    assert m.monto == Decimal("12267.85") and m.moneda == "DOP"


def test_transferencia_recibida_es_ingreso():
    m = parsear(_correo(RECIBIDA))[0]
    assert m.tipo == "ingreso" and m.canal == "transferencia"
    assert m.monto == Decimal("1500.00") and m.moneda == "DOP"
    assert "ROSILIS" in m.contraparte and "BHD" in m.contraparte


def test_transferencia_en_proceso_queda_pendiente():
    """Se titula "en proceso" y NO trae Estado, aunque el cuerpo diga "presenta
    un consumo" (texto heredado de la plantilla de tarjeta). Pendiente hasta que
    el banco confirme: los totales de gasto filtran por aprobada."""
    m = parsear(_correo(EN_PROCESO))[0]
    assert m.estado == "pendiente" and m.tipo == "gasto"
    assert m.canal == "transferencia", "no es un consumo de tarjeta aunque lo diga"


def test_remitente_vacio_no_revienta():
    """Los 2 casos reales llegan con "Remitente:" sin valor."""
    m = parsear(_correo(EN_PROCESO))[0]
    assert m.contraparte.strip(), "el contrato exige contraparte no vacía"


# ── Las tres notaciones de moneda del mismo remitente ────────────────────

def test_tres_notaciones_de_moneda():
    """"DOP 254.90", "RD$ 1,500.00" y "DOP$ 12,267.85" conviven en el mismo
    remitente y asunto. La tercera es la que nadie inventaría de cabeza."""
    assert parsear(_correo(CONSUMO))[0].moneda == "DOP"
    assert parsear(_correo(RECIBIDA))[0].moneda == "DOP"
    assert parsear(_correo(NOMINA))[0].moneda == "DOP"


def test_usd():
    usd = CONSUMO.replace("Monto: DOP 254.90", "Monto: USD 9.99")
    m = parsear(_correo(usd))[0]
    assert m.moneda == "USD" and m.monto == Decimal("9.99")


# ── Fallos que tienen que doler ──────────────────────────────────────────

def test_tipo_no_reconocido_revienta():
    assert _revienta(parsear, _correo(
        "Notificación de Algo Nuevo Monto: DOP 1.00 Fecha: 01/01/2026 10:00 AM"))


def test_sin_monto_revienta():
    assert _revienta(parsear, _correo(
        "Notificación de Consumo Estado: APROBADO Comercio: X "
        "Fecha de transacción: 01/01/2026 10:00 AM"))


def test_ruteo():
    assert buscar_parser(REMITENTE, ASUNTO) is parsear
    # Este remitente es publicidad, no transaccional.
    assert buscar_parser("banreservascomunicaciones@banreservas.com", ASUNTO) is None


# ── Capa 2: los 58 correos reales ────────────────────────────────────────

def _cab(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def test_contra_los_fixtures_reales():
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return
    ok, fallos, tipos = 0, [], {}
    for f in sorted(FIXTURES.glob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        if REMITENTE not in _cab(msg.get("From")).lower():
            continue
        if _cab(msg.get("Subject")).strip() != ASUNTO:
            continue
        plano = html = ""
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            d = (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/plain" and not plano:
                plano = d
            elif p.get_content_type() == "text/html" and not html:
                html = d
        c = CorreoCrudo(remitente=REMITENTE, asunto=ASUNTO,
                        fecha_correo=datetime(2026, 1, 1), html=html, texto=plano,
                        cuenta="x@y.com", uid=f.stem)
        try:
            for mv in parsear(c):
                ok += 1
                k = f"{mv.canal}/{mv.tipo}"
                tipos[k] = tipos.get(k, 0) + 1
        except ErrorDeParseo as e:
            fallos.append(f"{f.name}: {e}")

    print(f"     ({ok} correos reales parseados, {tipos})")
    assert not fallos, "fallaron:\n  " + "\n  ".join(fallos[:5])
    assert ok == 58, f"esperaba 58, hubo {ok}"
    # Los cuatro tipos que confirmó el testigo el 30-ago.
    assert tipos.get("tarjeta/gasto") == 53
    assert tipos.get("transferencia/gasto") == 2
    assert tipos.get("transferencia/ingreso") == 2
    assert tipos.get("nomina/ingreso") == 1


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
