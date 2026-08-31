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
    REMITENTE_PAGOS,
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


# ── Los comprobantes que viajan en PDF ───────────────────────────────────

def _pdfs_de(nombre_eml):
    """(nombre, bytes) de los PDF adjuntos de un fixture."""
    msg = email.message_from_bytes((FIXTURES / nombre_eml).read_bytes())
    return tuple((p.get_filename(), p.get_payload(decode=True) or b"")
                 for p in msg.walk()
                 if (p.get_filename() or "").lower().endswith(".pdf"))


def _con_pdf(nombre_eml, remitente=REMITENTE_PAGOS, asunto="Notificaciones Popular"):
    return CorreoCrudo(remitente=remitente, asunto=asunto,
                       fecha_correo=datetime(2026, 6, 1, 9, 0), html="",
                       texto="", cuenta="rosilisr04@gmail.com", uid="1",
                       adjuntos=_pdfs_de(nombre_eml))


def test_el_dinero_del_popular_estaba_en_los_adjuntos():
    """DOP 187,000 en cuatro transferencias: casi cinco veces todo lo que hay en
    los CUERPOS de los correos del Popular. El cuerpo no trae una sola cifra —
    el comprobante entero va dentro del PDF— y yo los había descartado como
    inparseables sin abrirlos."""
    total = Decimal("0")
    vistos = 0
    for eml in ("rosilisr04_30565.eml", "rosilisr04_30823.eml",
                "rosilisr04_31335.eml", "rosilisr04_31790.eml"):
        correo = _con_pdf(eml)
        # Vía buscar_parser, no llamando a la función a mano: si el registro del
        # remitente se rompe, el movimiento desaparece y el test tiene que verlo.
        parser = buscar_parser(correo.remitente, correo.asunto)
        assert parser, f"{eml}: buscar_parser no encuentra el parser"
        movs = parser(correo)
        assert len(movs) == 1, f"{eml}: {len(movs)} movimientos"
        total += movs[0].monto
        vistos += 1
    assert vistos == 4
    assert total == Decimal("187000.00"), f"recuperado {total}, esperaba 187000.00"


def test_el_comprobante_dice_a_quien_y_en_que_sentido():
    m = buscar_parser(REMITENTE_PAGOS, "Notificaciones Popular")(
        _con_pdf("rosilisr04_30565.eml"))[0]
    assert m.monto == Decimal("56000.00")
    assert m.moneda == "DOP"
    # `origen → destino`: la forma que propios.reclasificar() sabe leer. El
    # origen va sin nombre porque el comprobante NO dice quién pagó — trae al
    # banco emisor y al beneficiario, y nada más.
    assert m.contraparte == "(el comprobante no dice quién pagó) → MARILANDIA VARGAS"
    # Provisional: es el papel leído solo. Quién decide de verdad es el registro
    # de cuentas propias — ver el test de abajo.
    assert m.tipo == "gasto"
    assert m.fecha.date().isoformat() == "2026-05-19"
    # La referencia sale del nombre del archivo: identifica el comprobante y no
    # depende de dónde caiga cada campo dentro del PDF.
    assert "PE2026051903805" in m.referencia


def test_un_pdf_de_publicidad_no_inventa_un_movimiento():
    """Al buzón también llegan PDF que no son comprobantes. Ignorarlos es lo
    correcto; convertirlos en un movimiento de cero pesos, no."""
    correo = CorreoCrudo(
        remitente=REMITENTE_PAGOS, asunto="Notificaciones Popular",
        fecha_correo=datetime(2026, 6, 1), html="", texto="",
        cuenta="tizianofv@gmail.com", uid="1",
        adjuntos=_pdfs_de("tizianofv_127746.eml"))
    assert buscar_parser(REMITENTE_PAGOS, "x")(correo) == []


def test_un_comprobante_con_dos_montos_revienta_en_vez_de_adivinar():
    """El monto es lo único con dos decimales. Si aparecieran dos, el formato
    cambió: elegir el primero daría una cifra equivocada SIN avisar, que es el
    modo de fallo que este proyecto trata como el peor."""
    from cerebro.bancos import popular
    real = popular._texto_de_pdf
    popular._texto_de_pdf = lambda _: (
        "NOTIFICACION CREDITO\nCR a Cta. de FULANO\n19-may-2026\n"
        "PESOS DOMINICANOS\n56,000.00\n1,234.56")
    try:
        assert _revienta(popular.parsear_comprobante_pdf,
                         _con_pdf("rosilisr04_30565.eml"))
    finally:
        popular._texto_de_pdf = real


def test_quien_decide_el_signo_es_el_registro_y_no_el_parser():
    """La corrección que costó RD$187,000 de signo.

    Yo leí "NOTIFICACION CREDITO a un tercero en otro banco" como dinero que
    sale. Estaba al revés: esa cuenta es de la hermana de Tiziano y algunos
    clientes depositan ahí, así que son INGRESOS.

    La lección no es acordarse de este caso: es que un parser no puede saber de
    qué lado del mostrador está. Eso lo sabe el registro de cuentas propias, y
    la regla ya existía —"solo el DESTINO es de la casa → ingreso"—. Emitiendo
    la contraparte como `origen → destino`, el mismo código deja como gasto una
    transferencia a un tercero de verdad, sin tocar el parser.
    """
    from cerebro.bancos.propios import Propios
    m = buscar_parser(REMITENTE_PAGOS, "Notificaciones Popular")(
        _con_pdf("rosilisr04_31790.eml"))[0]
    casa = ["ROSILIS", "FAJARDOVARGAS", "TIZIANOFAJARDO"]

    # Sin la cuenta registrada, se queda como lo literal del papel.
    assert Propios(casa).reclasificar(m).tipo == "gasto"
    # Con la cuenta de la hermana registrada, es lo que de verdad es.
    assert Propios(casa + ["MARILANDIA VARGAS"]).reclasificar(m).tipo == "ingreso"


def test_un_pago_de_empresa_tambien_se_lee():
    """El 31-ago el canario gritó "cambiaron el formato". No habían cambiado
    nada: llegó un comprobante de un tipo que yo no había visto —PAGOS A
    TERCEROS de una empresa— y mi parser sacaba el beneficiario de la frase
    "CR a Cta. de X", que es lo que el banco escribe en las transferencias que
    uno mismo hace. En un pago de empresa la descripción es real ("Adelanto 50
    Cot 2026-005"), así que no había de dónde sacarlo.

    Eran RD$29,000 de un cliente. Asumí una convención que solo valía para los
    cuatro comprobantes que tenía delante.
    """
    m = buscar_parser(REMITENTE_PAGOS, "Notificaciones Popular")(
        _con_pdf("cds_18341.eml"))[0]
    assert m.monto == Decimal("29000.00")
    assert m.moneda == "DOP"
    assert m.fecha.date().isoformat() == "2026-08-31"
    # El beneficiario sale del bloque anclado al monto, no de la descripción.
    assert "ROSILIS" in m.contraparte
    # Y acá SÍ se sabe quién paga: en un pago de empresa la "Empresa
    # Generadora" trae el nombre real, no el canal.
    assert m.contraparte.startswith("LEON ROJO PUBLICID")


def test_en_una_transferencia_propia_no_se_inventa_el_pagador():
    """Ahí la "Empresa Generadora" trae IBANKING, que es el canal por el que se
    hizo — no una persona. Ponerlo de pagador sería inventar."""
    m = buscar_parser(REMITENTE_PAGOS, "Notificaciones Popular")(
        _con_pdf("rosilisr04_30565.eml"))[0]
    assert m.contraparte.startswith("(el comprobante no dice quién pagó)")


def test_si_el_bloque_del_beneficiario_no_esta_revienta():
    """El beneficiario se lee anclado al monto, y se COMPRUEBA que después
    venga el "BANCO ..." que confirma que ese bloque es el que parece. Sin esa
    comprobación, mover un campo daría una contraparte inventada en silencio —
    y una contraparte inventada ensucia el aprendizaje de categorías para
    siempre."""
    from cerebro.bancos import popular
    real = popular._texto_de_pdf
    popular._texto_de_pdf = lambda _: (
        "NOTIFICACION CREDITO\n31-ago-2026\nPESOS DOMINICANOS\n"
        "29,000.00\nALGO QUE NO ES UN BLOQUE\nOTRA COSA")
    try:
        assert _revienta(popular.parsear_comprobante_pdf,
                         _con_pdf("cds_18341.eml"))
    finally:
        popular._texto_de_pdf = real


def test_los_canales_son_una_sola_lista():
    """Lo encontró testigo-b. Había DOS listas de canales —una regex para los
    correos y un set para los PDF— y no coincidían: "CAJERO" estaba en la
    primera y faltaba en la segunda. Consecuencia: un comprobante generado por
    cajero ponía "CAJERO" de PAGADOR, como si fuera una persona.

    El arreglo no es agregar CAJERO a la segunda lista: es que haya UNA. Dos
    copias del mismo hecho se desincronizan, siempre — es la tercera vez hoy
    que este proyecto tropieza con eso.
    """
    import re
    from cerebro.bancos import popular
    del_regex = {c.replace(r"\s+", " ") for c in popular._CANALES.split("|")}
    assert del_regex == set(popular._CANALES_PDF), (
        f"las dos listas de canales difieren: "
        f"solo en el regex {sorted(del_regex - set(popular._CANALES_PDF))}, "
        f"solo en el PDF {sorted(set(popular._CANALES_PDF) - del_regex)}")


def test_un_comprobante_por_cajero_no_pone_cajero_de_pagador():
    from cerebro.bancos import popular
    real = popular._texto_de_pdf
    popular._texto_de_pdf = lambda _: (
        "Número de Referencia: Monto :\nCAJERO\nTRANSFERENCIA A CUENTA\n"
        "0005422\n19-may-2026\nPESOS DOMINICANOS\n56,000.00\n"
        "MARILANDIA VARGAS\n00201449139\nBANCO BANESCO\n982*****55\n"
        "287791829\nNOTIFICACION CREDITO")
    try:
        m = popular.parsear_comprobante_pdf(_con_pdf("rosilisr04_30565.eml"))[0]
    finally:
        popular._texto_de_pdf = real
    assert m.contraparte.startswith("(el comprobante no dice quién pagó)"), (
        f"un canal se coló como pagador: {m.contraparte!r}")


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
