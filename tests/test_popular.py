"""Tests del parser de Banco Popular (cerebro/bancos/popular.py).

Popular es el banco de menor volumen y el que más ausencias tiene, así que
buena parte de estos tests fija lo que NO manda — sobre todo que no notifica
consumos de tarjeta, que es lo que uno esperaría de un banco.

El test que más vale es el de los duplicados: Popular manda la misma
transacción dos veces con segundos de diferencia, y es la única evidencia real
que tenemos de que `clave_dedupe()` hace falta.

Correr:  python3 tests/test_popular.py
"""
from __future__ import annotations

import email
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from email.header import decode_header
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    buscar_parser,
)
from cerebro.bancos.popular import (  # noqa: E402
    REMITENTE_INFO,
    REMITENTE_NOTIF,
    parsear_cargo_bajo_balance,
    parsear_reverso_sobregiro,
    parsear_transferencia_recibida,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "popular"

RECIBIDA = ("Estimado(a) FAJARDO VARGAS TIZ Le informamos los detalles de la "
            "transacción de transferencia recibida en su cuenta terminada en 9142 "
            ": Monto Fecha Canal RD 17,000.00 10/8/2026 IBANKING Si necesita más "
            "información o no reconoce esta transacción, no dude en contactarnos.")

CARGO = ("Aviso cargo de comisión por bajo balance - Cuenta Estimado(a): TIZIANO "
         "FAJARDO VARGAS No. de identificación XXX-XXXX- 2037 Queremos "
         "notificarte que, tu cuenta AHORRO EN DÓLARES terminada en 0489 presenta "
         "un CARGO POR BAJO BALANCE MÍNIMO de USD$12.00 . Tu cuenta estuvo por "
         "debajo del balance mínimo requerido, que es de USD$500.00 .")

REVERSO = ("Estimado (a) FAJARDO VARGAS TIZ . Nos place informarle que hemos "
           "procedido con la devolución de RD 0.12 correspondiente al cargo por "
           "sobregiro aplicado a su cuenta No 9142 en fecha 26/3/26 .")


def _c(texto, remitente, asunto, fecha=datetime(2026, 6, 1, 9, 0)):
    return CorreoCrudo(remitente=remitente, asunto=asunto, fecha_correo=fecha,
                       html="", texto=texto, cuenta="tizianofv@gmail.com", uid="1")


def _revienta(fn, *a):
    try:
        fn(*a)
    except ErrorDeParseo:
        return True
    return False


# ── Lo que Popular NO manda ──────────────────────────────────────────────

def test_no_hay_consumos_de_tarjeta():
    """En 154 correos de un año no hay ni uno de consumo con tarjeta. No es un
    hueco del parser: Popular no lo notifica por correo."""
    if not FIXTURES.exists():
        return
    for f in FIXTURES.glob("*.eml"):
        msg = email.message_from_bytes(f.read_bytes())
        asunto = _cab(msg.get("Subject")).lower()
        assert "consumo" not in asunto, f"apareció un consumo: {f.name}"


def test_publicidad_no_se_enruta():
    assert buscar_parser(REMITENTE_INFO, "Boletín informativo Impulsa Popular") is None
    assert buscar_parser("experienciaclientes@encuestas.popularenlinea.com",
                         "En Banco Popular valoramos tu experiencia") is None


def test_el_cargo_sale_del_buzon_de_marketing():
    """Popular manda un cargo REAL desde el mismo buzón que la publicidad, así
    que acá el asunto pesa más que el remitente."""
    assert buscar_parser(REMITENTE_INFO,
                         "Aviso cargo comisión por bajo balance") is parsear_cargo_bajo_balance


# ── Los tres tipos ───────────────────────────────────────────────────────

def test_transferencia_recibida():
    m = parsear_transferencia_recibida(
        _c(RECIBIDA, REMITENTE_NOTIF, "Notificación transf recibida via app e IB"))[0]
    assert m.tipo == "ingreso" and m.monto == Decimal("17000.00")
    assert m.moneda == "DOP" and m.fecha == datetime(2026, 8, 10)
    assert "IBANKING" in m.referencia and "9142" in m.referencia


def test_cargo_por_bajo_balance_es_gasto_en_dolares():
    """De los pocos movimientos en USD del sistema fuera de los consumos BHD."""
    m = parsear_cargo_bajo_balance(
        _c(CARGO, REMITENTE_INFO, "Aviso cargo comisión por bajo balance"))[0]
    assert m.tipo == "gasto" and m.moneda == "USD"
    assert m.monto == Decimal("12.00"), f"¿agarró el mínimo de 500? {m.monto}"


def test_cargo_no_confunde_el_balance_minimo():
    """El correo trae dos cifras en USD: el cargo (12.00) y el balance mínimo
    requerido (500.00). Solo la primera es el movimiento."""
    m = parsear_cargo_bajo_balance(
        _c(CARGO, REMITENTE_INFO, "Aviso cargo comisión por bajo balance"))[0]
    assert m.monto != Decimal("500.00")


def test_cargo_usa_la_fecha_del_correo():
    """El aviso no trae fecha de transacción."""
    cuando = datetime(2026, 7, 3, 8, 30)
    m = parsear_cargo_bajo_balance(
        _c(CARGO, REMITENTE_INFO, "Aviso cargo comisión por bajo balance", cuando))[0]
    assert m.fecha == cuando


def test_reverso_es_ingreso():
    m = parsear_reverso_sobregiro(
        _c(REVERSO, REMITENTE_NOTIF, "Notificacion Reverso a cuenta por sobregiro"))[0]
    assert m.tipo == "ingreso" and m.monto == Decimal("0.12")
    assert m.fecha == datetime(2026, 3, 26)


# ── Regresiones: lo que encontró el testigo el 30-ago-2026 ───────────────

def test_canal_no_se_traga_la_frase_siguiente():
    """El grupo del canal era [A-Z ]+ bajo re.I, y bajo IGNORECASE esa clase
    también casa minúsculas: se comía "IBANKING Si necesita m" y solo frenaba
    en la "á" de "más". Afectaba a los 4 correos de este tipo, o sea al 100%.
    El test viejo usaba `in`, que pasaba igual de contento."""
    m = parsear_transferencia_recibida(
        _c(RECIBIDA, REMITENTE_NOTIF, "Notificación transf recibida via app e IB"))[0]
    assert m.referencia == ("Popular · cuenta ••9142 · IBANKING · "
                            "tizianofv@gmail.com")


def test_canal_sin_acentos_tampoco_desborda():
    """Popular usa plantillas sin acentos en otros correos suyos. Sin la "á"
    que frenaba el regex por accidente, se tragaba la frase entera."""
    sin_tilde = RECIBIDA.replace("más información", "mas informacion") \
                        .replace("transacción", "transaccion")
    m = parsear_transferencia_recibida(
        _c(sin_tilde, REMITENTE_NOTIF, "Notificación transf recibida via app e IB"))[0]
    assert m.referencia.endswith("IBANKING · tizianofv@gmail.com"), m.referencia


def test_el_cargo_conserva_su_numero_de_cuenta():
    """_CUENTA exigía "cuenta" pegado a los dígitos, pero el aviso dice "tu
    cuenta AHORRO EN DÓLARES terminada en 0489". Los 8 cargos reales perdían el
    número — y es la ÚNICA marca de que son sobre otra cuenta (0489) distinta
    de la de las transferencias (9142)."""
    m = parsear_cargo_bajo_balance(
        _c(CARGO, REMITENTE_INFO, "Aviso cargo comisión por bajo balance"))[0]
    assert "0489" in m.referencia, f"perdió la cuenta: {m.referencia!r}"


def test_sin_los_campos_revienta():
    assert _revienta(parsear_transferencia_recibida,
                     _c("Le informamos algo", REMITENTE_NOTIF, "transf recibida"))
    assert _revienta(parsear_cargo_bajo_balance,
                     _c("Hola", REMITENTE_INFO, "cargo comisión por bajo balance"))


# ── Capa 2: los correos reales, y los duplicados ─────────────────────────

def _cab(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def test_contra_los_fixtures_reales_y_los_duplicados():
    """POPULAR DUPLICA: manda la misma transacción dos veces con segundos de
    diferencia. Es la única evidencia real que tenemos de que clave_dedupe()
    hace falta — 16 correos son 13 movimientos distintos."""
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return
    ok, fallos, claves = 0, [], Counter()
    for f in sorted(FIXTURES.glob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        frm = _cab(msg.get("From"))
        addr = frm.split("<")[-1].strip("> ").lower() if "<" in frm else frm.lower()
        asunto = _cab(msg.get("Subject")).strip()
        fn = buscar_parser(addr, asunto)
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
        c = CorreoCrudo(remitente=addr, asunto=asunto, fecha_correo=fc,
                        html=html, texto=plano, cuenta="tizianofv@gmail.com",
                        uid=f.stem)
        try:
            for mv in fn(c):
                ok += 1
                claves[mv.clave_dedupe()] += 1
        except ErrorDeParseo as e:
            fallos.append(f"{f.name} [{asunto[:30]}]: {e}")

    repetidas = sum(1 for v in claves.values() if v > 1)
    print(f"     ({ok} correos → {len(claves)} movimientos distintos, "
          f"{repetidas} duplicados)")
    assert not fallos, "fallaron:\n  " + "\n  ".join(fallos[:5])
    assert ok == 16, f"esperaba 16 correos parseables, hubo {ok}"
    assert len(claves) == 13, (
        f"esperaba 13 movimientos distintos, hubo {len(claves)} — si sube, "
        "clave_dedupe dejó de reconocer los duplicados de Popular")
    assert repetidas == 3, "los 3 pares duplicados que confirmó el testigo"


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
