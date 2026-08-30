"""Tests del parser de Banesco (cerebro/bancos/banesco.py).

Banesco manda prosa, no campos. Eso hace que los tests importantes de acá sean
sobre AMBIGÜEDAD: el correo trae dos cantidades de dinero en la misma frase y
solo una es la transacción.

Correr:  python3 tests/test_banesco.py
"""
from __future__ import annotations

import email
import os
import pathlib
import sys
from datetime import datetime
from decimal import Decimal
from email.header import decode_header
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.banesco import (  # noqa: E402
    parsear_consumo,
    parsear_realizada,
    parsear_recibida,
)
from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    buscar_parser,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "banesco"
REMITENTE = "notificaciones@banesco.com.do"

APROBADA = ("Estimado(a) ROSILIS ROMERO, te notificamos que tu tarjeta VISA CLASICA "
            "SUPERCASHBACK Banesco terminada en 9639, en fecha 06/05/26, presenta un "
            "consumo de RD$ 3,183.38, en SM NACIONAL MAXIMO GOM y su estado es "
            "aprobada. Dispones de RD$ 663,903.86.. En caso de no reconocer la "
            "transacción por favor repórtelo inmediatamente llamando al 829-893-8200.")

DECLINADA = ("Estimado(a) cliente: Te informamos que la transaccion realizada en el "
             "COMERCIAL DE PENA por un monto de RD 6,350.00, con tu Tarjeta de "
             "Crédito Banesco terminada en 6959, ha sido rechazada. Motivo de la "
             "declinación: Transaccion no permitida al ti. Si desconoces este consumo "
             "puedes comunicarte con nuestro Centro de Atención al Cliente.")

RECIBIDA = ("Hola , Nos place informarte que recibiste una transferencia externa "
            "realizada en fecha 05/05/2026 11:38 AM de manera satisfactoria. A "
            "continuación, detalles de la transacción: Monto: DOP50,000.00 Cuenta "
            "Origen: DO25BPDO00000000000846141885 Fecha de la Transacción: 05/05/2026 "
            "No. Referencia: E000073.A112248 Banco Emisor: BANCO POPULAR DOMINICANO, "
            "C. POR A. Cuenta Destino: DO73BANS0000000009820007")

REALIZADA = ("Hola , Nos place informarte que la transacción vía pago al instante "
             "realizada en fecha 25/05/2026 03:24 PM fue enviada al beneficiario. A "
             "continuación, detalles de la transacción: Monto: DOP35,000.00 Cuenta "
             "Origen: DO73BANS00000000098200073555 Fecha de la Transacción: "
             "25/05/2026 No. Referencia: E000073.A9895595 Banco Beneficiario: BANCO "
             "MULTIPLE BHD S.A. Cuenta Destino: DO11BSHD000000000")


def _correo(texto: str, asunto: str = "Alerta de Consumo Banesco RD",
            fecha=datetime(2026, 8, 1, 15, 0)) -> CorreoCrudo:
    return CorreoCrudo(remitente=REMITENTE, asunto=asunto, fecha_correo=fecha,
                       html="", texto=texto, cuenta="rosilisr04@gmail.com", uid="1")


def _revienta(fn, *a) -> bool:
    try:
        fn(*a)
    except ErrorDeParseo:
        return True
    return False


# ── La trampa principal: dos montos en la misma frase ────────────────────

def test_no_confunde_el_balance_con_el_consumo():
    """"Dispones de RD$ 663,903.86" es el balance disponible. Registrarlo como
    gasto convertiría una compra de 3 mil pesos en una de 663 mil."""
    m = parsear_consumo(_correo(APROBADA))[0]
    assert m.monto == Decimal("3183.38"), f"agarró el balance: {m.monto}"


def test_aprobada():
    m = parsear_consumo(_correo(APROBADA))[0]
    assert m.canal == "tarjeta" and m.tipo == "gasto" and m.estado == "aprobada"
    assert m.moneda == "DOP" and m.contraparte == "SM NACIONAL MAXIMO GOM"
    assert "9639" in m.referencia


def test_anio_de_dos_digitos():
    """Banesco manda "06/05/26", no "06/05/2026"."""
    assert parsear_consumo(_correo(APROBADA))[0].fecha == datetime(2026, 5, 6)


# ── Declinadas: otra redacción entera ────────────────────────────────────

def test_declinada_no_cuenta_como_gasto_aprobado():
    m = parsear_consumo(_correo(DECLINADA))[0]
    assert m.estado == "declinada"
    assert m.monto == Decimal("6350.00") and m.moneda == "DOP"
    assert m.contraparte == "COMERCIAL DE PENA"


def test_declinada_usa_la_fecha_del_correo():
    """Las 6 declinadas reales no traen fecha de transacción."""
    cuando = datetime(2026, 8, 15, 9, 30)
    m = parsear_consumo(_correo(DECLINADA, fecha=cuando))[0]
    assert m.fecha == cuando


def test_declinada_guarda_el_motivo():
    m = parsear_consumo(_correo(DECLINADA))[0]
    assert "no permitida" in m.referencia.lower()


def test_moneda_sin_simbolo():
    """Las aprobadas dicen "RD$", las declinadas "RD" a secas."""
    assert parsear_consumo(_correo(DECLINADA))[0].moneda == "DOP"


# ── Transferencias ───────────────────────────────────────────────────────

def test_transferencia_recibida_es_ingreso():
    m = parsear_recibida(_correo(RECIBIDA, "Notificación de Transferencia Recibida"))[0]
    assert m.tipo == "ingreso" and m.monto == Decimal("50000.00")
    assert "POPULAR" in m.contraparte


def test_transferencia_realizada_es_gasto():
    m = parsear_realizada(_correo(REALIZADA, "Notificación de Transferencia Realizada"))[0]
    assert m.tipo == "gasto" and m.monto == Decimal("35000.00")
    assert "BHD" in m.contraparte


def test_moneda_pegada_a_la_cifra():
    """"DOP50,000.00", sin espacio."""
    assert parsear_recibida(_correo(RECIBIDA, "Notificación de Transferencia Recibida"))[0].moneda == "DOP"


# ── Fallos y ruteo ───────────────────────────────────────────────────────

def test_redaccion_desconocida_revienta():
    assert _revienta(parsear_consumo, _correo("Te informamos cualquier otra cosa."))


def test_ruteo_por_asunto():
    assert buscar_parser(REMITENTE, "Alerta de Consumo Banesco RD") is parsear_consumo
    assert buscar_parser(REMITENTE, "Notificación de Transferencia Recibida") is parsear_recibida
    assert buscar_parser(REMITENTE, "Notificación de Transferencia Realizada") is parsear_realizada
    # Publicidad y encuestas, no transaccional.
    assert buscar_parser("banescontigo@banesco.com.do", "lo que sea") is None
    assert buscar_parser(REMITENTE, "Estado de Cuenta Tarjeta Banesco") is None


# ── Capa 2: los 111 correos reales ───────────────────────────────────────

def _cab(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def test_contra_los_fixtures_reales():
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return
    ruta = {"Alerta de Consumo Banesco RD": parsear_consumo,
            "Notificación de Transferencia Recibida": parsear_recibida,
            "Notificación de Transferencia Realizada": parsear_realizada}
    ok, fallos, estados = 0, [], {}
    for f in sorted(FIXTURES.glob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        if REMITENTE not in _cab(msg.get("From")).lower():
            continue
        fn = ruta.get(_cab(msg.get("Subject")).strip())
        if not fn:
            continue
        plano = html = ""
        for p in (msg.walk() if msg.is_multipart() else [msg]):
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
        c = CorreoCrudo(remitente=REMITENTE, asunto=_cab(msg.get("Subject")),
                        fecha_correo=fc, html=html, texto=plano,
                        cuenta="rosilisr04@gmail.com", uid=f.stem)
        try:
            for mv in fn(c):
                ok += 1
                k = f"{mv.canal}/{mv.tipo}/{mv.estado}"
                estados[k] = estados.get(k, 0) + 1
        except ErrorDeParseo as e:
            fallos.append(f"{f.name}: {e}")

    print(f"     ({ok} correos reales parseados, {estados})")
    assert not fallos, "fallaron:\n  " + "\n  ".join(fallos[:5])
    assert ok == 111, f"esperaba 111, hubo {ok}"
    assert estados.get("tarjeta/gasto/aprobada") == 103
    assert estados.get("tarjeta/gasto/declinada") == 6
    assert estados.get("transferencia/ingreso/aprobada") == 1
    assert estados.get("transferencia/gasto/aprobada") == 1


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
