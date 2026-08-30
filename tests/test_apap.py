"""Tests del parser de APAP (cerebro/bancos/apap.py).

APAP manda 16 asuntos distintos por un mismo remitente y solo 12 mueven dinero,
así que lo que más se prueba acá es qué NO se parsea: afiliar un beneficiario o
abrir un producto no son movimientos, y colarlos inventaría plata.

Correr:  python3 tests/test_apap.py
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

from cerebro.bancos.apap import REMITENTE, parsear  # noqa: E402
from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    buscar_parser,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "apap"

ACH = ("HOLAPAP Hola ROSILIS YANELY ROMERO JIMENEZ, Tu Cuenta de Ahorros APAP "
       "terminada en 9854 presenta un(a) Transferencia ACH con el siguiente "
       "detalle: Detalle de la transferencia Fecha: 14/10/2025 Hora: 11:44 "
       "Número de referencia: FT25287TCHM7 Monto RD$: 18,345.00 Tipo: "
       "Transferencia ACH Beneficiario: WENDY MARISOL CANELA CRUZ Cuenta "
       "destino: ****9639")

INTERESES = ("HOLAPAP Hola Tu Certificado de Depósito APAP terminado en 2078 "
             "presenta un pago de intereses, con el siguiente detalle: Detalle "
             "de la transacción Moneda: RD pesos dominicanos Monto interés: "
             "9,000.00 Impuesto retenido: 900.00 Neto pagado: 8,100.00 Forma de "
             "pago: Crédito a cuenta Fecha: 20/03/2026 Hora: 09:15")

ENTRANTE = ("HOLAPAP Hola ROSILIS YANELY ROMERO JIMENEZ Tu Cuenta de Ahorro APAP "
            "terminada en 9854, presenta un Pago al Instante BCRD entrante, con "
            "el siguiente detalle: Detalle de la transacción Fecha: 6/11/2025 "
            "Hora: 92:20 Número de referencia: FT25310127P4 Monto RD$: 40,000.00 "
            "Tipo: Transferencia LBTR")


def _correo(texto: str, asunto: str) -> CorreoCrudo:
    return CorreoCrudo(remitente=REMITENTE, asunto=asunto,
                       fecha_correo=datetime(2026, 1, 1), html="", texto=texto,
                       cuenta="rosilisr04@gmail.com", uid="1")


def _revienta(fn, *a) -> bool:
    try:
        fn(*a)
    except ErrorDeParseo:
        return True
    return False


# ── Lo que NO es un movimiento ───────────────────────────────────────────

def test_no_movimientos_no_se_enrutan():
    """20 de los 75 correos de este remitente no mueven dinero. Si alguno
    llegara a un parser, se inventaría un movimiento."""
    for asunto in ("Afiliación Nuevo Beneficiario", "Apertura de nuevo producto",
                   "Código Temporal – APAP",
                   "¡Has subido de nivel! Nuevos beneficios te esperan"):
        assert buscar_parser(REMITENTE, asunto) is None, asunto


def test_el_otro_buzon_de_apap_no_se_parsea():
    """`no-reply@` es transaccional y `noreply@` es publicidad. Las dos grafías
    son del mismo banco y solo una mueve dinero."""
    assert buscar_parser("noreply@apap.com.do", "Transferencia ACH") is None
    assert buscar_parser(REMITENTE, "Transferencia ACH") is parsear


def test_asunto_fuera_del_mapa_revienta():
    """Un asunto nuevo tiene que avisar, no colarse como gasto."""
    assert _revienta(parsear, _correo(ACH, "Transferencia Cuántica APAP"))


# ── Dirección del dinero ─────────────────────────────────────────────────

def test_ach_es_gasto():
    m = parsear(_correo(ACH, "Transferencia ACH"))[0]
    assert m.tipo == "gasto" and m.canal == "transferencia"
    assert m.monto == Decimal("18345.00") and m.moneda == "DOP"
    assert m.contraparte == "WENDY MARISOL CANELA CRUZ"
    assert m.fecha == datetime(2025, 10, 14, 11, 44)


def test_entrante_es_ingreso():
    m = parsear(_correo(ENTRANTE,
                        "Transferencia Pago al Instante BCRD entrante completada"))[0]
    assert m.tipo == "ingreso" and m.monto == Decimal("40000.00")


def test_entre_cuentas_propias_es_traspaso():
    """Mover plata entre dos cuentas propias de APAP no es gasto ni ingreso.
    Marcarlo como cualquiera de los dos contaría el mismo dinero dos veces."""
    t = ACH.replace("Transferencia ACH", "IB-Transferencia entre Cuentas APAP")
    m = parsear(_correo(t, "IB-Transferencia entre Cuentas APAP"))[0]
    assert m.tipo == "transferencia"


def test_no_procesada_queda_declinada():
    t = ACH.replace("Transferencia ACH", "Transferencia ACH no procesada")
    m = parsear(_correo(t, "Transferencia ACH no procesada"))[0]
    assert m.estado == "declinada", "una transferencia rechazada no es un gasto"


# ── Los tres montos del pago de intereses ────────────────────────────────

def test_intereses_usa_el_neto_no_el_bruto():
    """Trae "Monto interés: 9,000.00", "Impuesto retenido: 900.00" y "Neto
    pagado: 8,100.00". Lo que entró a la cuenta son 8,100 — registrar el bruto
    sería un ingreso que nunca llegó completo."""
    m = parsear(_correo(INTERESES, "Depósito de intereses"))[0]
    assert m.monto == Decimal("8100.00"), f"usó el bruto: {m.monto}"
    assert m.tipo == "ingreso" and m.canal == "interes"


# ── Datos sucios que manda APAP ──────────────────────────────────────────

def test_hora_corrupta_no_tira_el_movimiento():
    """APAP manda "Hora: 92:20" y "Hora: 70:90" en correos reales. No son horas.
    Perder un movimiento bueno por un campo accesorio sería peor que perder la
    hora: se conserva la fecha."""
    m = parsear(_correo(ENTRANTE,
                        "Transferencia Pago al Instante BCRD entrante completada"))[0]
    assert m.fecha.date() == datetime(2025, 11, 6).date()
    assert m.fecha.hour == 0, "la hora inválida se descarta, no se inventa"


def test_fecha_partida_por_espacios():
    """Un correo real trae "Fecha: 16/04/ 2026" — un &nbsp; dentro de la fecha."""
    t = ACH.replace("Fecha: 14/10/2025", "Fecha:      16/04/ 2026")
    m = parsear(_correo(t, "Transferencia ACH"))[0]
    assert m.fecha.date() == datetime(2026, 4, 16).date()


def test_beneficiario_no_arrastra_la_etiqueta_siguiente():
    m = parsear(_correo(ACH, "Transferencia ACH"))[0]
    assert "Cuenta" not in m.contraparte and "destino" not in m.contraparte


# ── Capa 2: los correos reales ───────────────────────────────────────────

def _cab(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def test_contra_los_fixtures_reales():
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return
    ok, fallos, tipos, ignorados = 0, [], {}, 0
    for f in sorted(FIXTURES.glob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        if REMITENTE not in _cab(msg.get("From")).lower():
            continue
        asunto = _cab(msg.get("Subject")).strip()
        if buscar_parser(REMITENTE, asunto) is None:
            ignorados += 1
            continue
        plano = html = ""
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            d = (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/plain" and not plano:
                plano = d
            elif p.get_content_type() == "text/html" and not html:
                html = d
        c = CorreoCrudo(remitente=REMITENTE, asunto=asunto,
                        fecha_correo=datetime(2026, 1, 1), html=html, texto=plano,
                        cuenta="rosilisr04@gmail.com", uid=f.stem)
        try:
            for mv in parsear(c):
                ok += 1
                k = f"{mv.canal}/{mv.tipo}"
                tipos[k] = tipos.get(k, 0) + 1
        except ErrorDeParseo as e:
            fallos.append(f"{f.name} [{asunto[:30]}]: {e}")

    print(f"     ({ok} movimientos, {ignorados} correos sin dinero ignorados)")
    assert not fallos, "fallaron:\n  " + "\n  ".join(fallos[:5])
    assert ok == 55, f"esperaba 55 movimientos, hubo {ok}"
    assert ignorados == 20, f"esperaba 20 no-movimientos, hubo {ignorados}"
    assert tipos.get("interes/ingreso") == 8
    assert tipos.get("transferencia/transferencia") == 1, "el traspaso interno"


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
