"""Tests del parser de BHD (cerebro/bancos/bhd.py).

Dos capas, y las dos hacen falta:

  1. Casos armados a mano — cubren lo que los fixtures NO tienen todavía: una
     tabla con DOS transacciones, un tipo desconocido, un <tbody> ausente. Son
     los formatos que el banco puede empezar a mandar cualquier día.

  2. Los 161 fixtures reales, si están en disco. Es el único test que puede
     desmentirme: yo escribí los casos de arriba mirando el formato, así que
     prueban lo que creo que hace el banco. Los correos reales prueban lo que
     el banco hace de verdad.

Los fixtures son correos con datos financieros y están fuera de git (ver
.gitignore), así que esa parte se SALTA si no están, en vez de fallar. Un CI
que no los tiene no puede correr esa capa, y eso no es un fallo del parser.

Correr:  python3 tests/test_bhd.py
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

from cerebro.bancos.bhd import (  # noqa: E402
    parsear_consumo,
    parsear_transferencia,
)
from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    buscar_parser,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "bhd"
ASUNTO = "BHD Notificación de Transacciones"


def _correo(html: str, asunto: str = ASUNTO) -> CorreoCrudo:
    return CorreoCrudo(remitente="alertas@bhd.com.do", asunto=asunto,
                       fecha_correo=datetime(2026, 7, 8), html=html, texto="",
                       cuenta="tizianofv@gmail.com", uid="1")


def _tabla(*filas: str) -> str:
    cuerpo = "".join(f"<tr>{f}</tr>" for f in filas)
    return f"<table><thead><tr><td>Fecha</td></tr></thead><tbody>{cuerpo}</tbody></table>"


def _fila(fecha="08/07/2026 11:14 am", moneda="US", monto="$1.19",
          comercio="APPLE.COM/BILL", estado="Aprobada", tipo="Compra") -> str:
    return "".join(f"<td>{v}</td>" for v in
                   (fecha, moneda, monto, comercio, estado, tipo))


def _revienta(fn, *a) -> bool:
    try:
        fn(*a)
    except ErrorDeParseo:
        return True
    return False


# ── Capa 1: casos armados ────────────────────────────────────────────────

def test_fila_tipica():
    m = parsear_consumo(_correo(_tabla(_fila())))[0]
    assert m.banco == "bhd" and m.tipo == "gasto" and m.canal == "tarjeta"
    assert m.monto == Decimal("1.19") and m.moneda == "USD"
    assert m.contraparte == "APPLE.COM/BILL" and m.estado == "aprobada"
    assert m.fecha == datetime(2026, 7, 8, 11, 14)


def test_pesos_con_separador_de_miles():
    m = parsear_consumo(_correo(_tabla(
        _fila(moneda="RD", monto="$2,500.00",
              comercio="INSTITUTO ESPAILLAT CA"))))[0]
    assert m.monto == Decimal("2500.00") and m.moneda == "DOP"


def test_dos_transacciones_en_un_correo():
    """Hoy no ocurre, pero es exactamente lo que un parser que aplana las <td>
    perdería en silencio. Por eso este itera <tr>."""
    movs = parsear_consumo(_correo(_tabla(
        _fila(comercio="UNO", monto="$1.00"),
        _fila(comercio="DOS", monto="$2.00"))))
    assert len(movs) == 2
    assert [m.contraparte for m in movs] == ["UNO", "DOS"]


def test_declinada_no_se_vuelve_aprobada():
    m = parsear_consumo(_correo(_tabla(_fila(estado="Declinada"))))[0]
    assert m.estado == "declinada"


def test_cabecera_dentro_del_tbody_se_salta():
    """Cinturón: si la maquetación cambia y la cabecera cae dentro del tbody,
    esa fila se salta en vez de reventar el correo entero."""
    cab = "".join(f"<td>{v}</td>" for v in
                  ("Fecha", "Moneda", "Monto", "Comercio", "Estado", "Tipo"))
    movs = parsear_consumo(_correo(_tabla(cab, _fila())))
    assert len(movs) == 1 and movs[0].contraparte == "APPLE.COM/BILL"


def test_tipo_desconocido_revienta():
    """Que BHD mande un tipo nuevo tiene que enterarme, no promediarse a gasto.
    No es hipotético: así aparecieron "Goods services with cash back" y
    "Reserva de Fondos (Hold)", que no estaban en la primera versión."""
    assert _revienta(parsear_consumo, _correo(_tabla(_fila(tipo="Criptomagia"))))


def test_compra_con_cash_back():
    m = parsear_consumo(_correo(_tabla(
        _fila(tipo="Goods services with cash back"))))[0]
    assert m.tipo == "gasto" and m.canal == "tarjeta" and m.estado == "aprobada"


def test_retencion_entra_como_pendiente():
    """Una "Reserva de Fondos (Hold)" llega Aprobada, pero es un pre-autorizado
    (hotel, bomba de gasolina), no un cargo liquidado. Contarla como gasto
    infla el total y vuelve a contarse cuando entra el cargo real."""
    m = parsear_consumo(_correo(_tabla(
        _fila(tipo="Reserva de Fondos (Hold)", estado="Aprobada"))))[0]
    assert m.estado == "pendiente"


def test_retencion_denegada_sigue_denegada():
    """El forzado a 'pendiente' solo aplica sobre una aprobada: una retención
    denegada no está pendiente de nada."""
    m = parsear_consumo(_correo(_tabla(
        _fila(tipo="Reserva de Fondos (Hold)", estado="Denegada"))))[0]
    assert m.estado == "declinada"


def test_estados_reales_de_la_muestra():
    """Las variantes que manda BHD de verdad, incluidas las compuestas."""
    esperado = {"Aprobada": "aprobada", "Denegada": "declinada",
                "Denegada- Confirme Banco": "declinada",
                "Denegada - CVV Invalido": "declinada"}
    for crudo, esp in esperado.items():
        m = parsear_consumo(_correo(_tabla(_fila(estado=crudo))))[0]
        assert m.estado == esp, f"{crudo!r} → {m.estado}, esperaba {esp}"


def test_reverso_se_invierte_a_ingreso():
    """BHD manda el cargo y su reverso en correos separados, así que el cargo
    ya entró como gasto aprobado. Si el reverso quedara como gasto/reversada y
    los totales filtraran por aprobada, el gasto quedaría contado y la
    devolución no. Invertirlo hace que se neteen sin emparejarlos."""
    m = parsear_consumo(_correo(_tabla(
        _fila(estado="Reversada", comercio="SUPERMERCADO X"))))[0]
    assert m.tipo == "ingreso"
    assert m.estado == "aprobada", "el reverso sí ocurrió; debe contar"
    assert m.monto == Decimal("1.19")


def test_reverso_sin_comercio():
    """Los 6 reversos de la muestra real llegan con la celda de comercio vacía:
    BHD referencia la transacción original en vez de repetirla."""
    m = parsear_consumo(_correo(_tabla(
        _fila(estado="Reversada", comercio=""))))[0]
    assert "reverso" in m.contraparte.lower()
    assert m.tipo == "ingreso"


def test_comercio_vacio_sin_ser_reverso_revienta():
    """La tolerancia es SOLO para reversos. Un comercio vacío en una compra
    normal es un fallo de parseo y tiene que doler."""
    assert _revienta(parsear_consumo,
                     _correo(_tabla(_fila(estado="Aprobada", comercio=""))))


def test_sin_tbody_revienta():
    assert _revienta(parsear_consumo, _correo("<p>hola</p>"))


def test_moneda_desconocida_revienta():
    assert _revienta(parsear_consumo, _correo(_tabla(_fila(moneda="EUR"))))


def test_otp_no_llega_al_parser():
    """"Código de validación de compra" trae monto pero no es un movimiento."""
    assert buscar_parser("alertas@bhd.com.do",
                         "Código de validación de compra") is None


def test_ruteo_registrado():
    assert buscar_parser("alertas@bhd.com.do", ASUNTO) is parsear_consumo
    assert buscar_parser("info@bhd.com.do", ASUNTO) is None


# ── Transferencias y pagos de servicio (sin tabla, en prosa) ─────────────

TRASPASO = ("Estimado(a): ROSILIS YANELY ROMERO A continuaci&oacute;n la "
            "informaci&oacute;n relacionada a tu transacci&oacute;n: Producto "
            "origen: DO47BCBH000000000XXXXXXX0013 Producto destino: "
            "XXXXXXXXXXXX9804 Descripci&oacute;n: Pago TC Monto: RD$ 110,000.00 "
            "Beneficiario: ROSILIS ROMERO N&uacute;mero de confirmaci&oacute;n: "
            "W02-1784-5754-9256-6 Fecha y hora de la transacci&oacute;n: "
            "20/07/2026 - 3:24 PM Tipo de transacci&oacute;n: Transacciones entre "
            "mis productos")

A_TERCERO = TRASPASO.replace("Descripci&oacute;n: Pago TC", "Descripci&oacute;n: Botell&oacute;n Criscar") \
                    .replace("Beneficiario: ROSILIS ROMERO",
                             "Beneficiario: PEREZ MARTINEZ, LUIS ALBERTO") \
                    .replace("Monto: RD$ 110,000.00", "Monto: RD$ 170.00")

SERVICIO = ("Estimado(a): ROSILIS YANELY ROMERO Has realizado exitosamente el "
            "pago de un servicio. Producto origen: XXXXXXXXXXXX9804 Monto: RD$ "
            "2823.07 N&uacute;mero de referencia: 1110400280 Proveedor del "
            "servicio: ALTICE HOGAR Servicio: Voz Data y Cable "
            "Descripci&oacute;n: Pago Telefono Internet 8095285695 "
            "N&uacute;mero de confirmaci&oacute;n: W20-1785-9425-2387-5 Fecha y "
            "hora de la transacci&oacute;n: 05/08/2026 |  11:08 AM")


def _correo_t(texto: str, asunto: str) -> CorreoCrudo:
    return CorreoCrudo(remitente="alertas@bhd.com.do", asunto=asunto,
                       fecha_correo=datetime(2026, 7, 20), html=texto, texto="",
                       cuenta="rosilisr04@gmail.com", uid="1")


def test_pago_de_tarjeta_no_es_un_gasto_nuevo():
    """EL caso de contar doble del proyecto. "Pago TC" por RD$110,000 mueve
    plata de la cuenta a la tarjeta: esos consumos YA se registraron uno a uno
    cuando se pasó la tarjeta. Como gasto, duplicaría ciento diez mil pesos."""
    m = parsear_transferencia(_correo_t(TRASPASO,
                                        "Transacciones entre mis productos"))[0]
    assert m.tipo == "transferencia", "un pago de tarjeta no es gasto nuevo"
    assert m.canal == "traspaso" and m.monto == Decimal("110000.00")


def test_transferencia_a_tercero_si_es_gasto():
    m = parsear_transferencia(_correo_t(
        A_TERCERO, "Transacciones entre productos BHD y a otros Bancos"))[0]
    assert m.tipo == "gasto" and m.monto == Decimal("170.00")
    assert "PEREZ MARTINEZ" in m.contraparte


def test_pago_de_servicio():
    m = parsear_transferencia(_correo_t(SERVICIO, "Pago de Servicio e Impuestos"))[0]
    assert m.tipo == "gasto" and m.canal == "servicio"
    assert m.contraparte == "ALTICE HOGAR" and m.monto == Decimal("2823.07")


def test_entidades_html_se_decodifican():
    """Estos correos llegan con las entidades sin resolver:
    "N&uacute;mero de confirmaci&oacute;n". Sin decodificarlas, ningún campo
    con acento se encuentra."""
    m = parsear_transferencia(_correo_t(SERVICIO, "Pago de Servicio e Impuestos"))[0]
    assert "&" not in m.contraparte and "&" not in m.referencia
    assert "conf W20-1785-9425-2387-5" in m.referencia


def test_fecha_con_separadores_raros():
    """BHD usa " - " en las transferencias y " | " en los pagos de servicio."""
    a = parsear_transferencia(_correo_t(TRASPASO,
                                        "Transacciones entre mis productos"))[0]
    b = parsear_transferencia(_correo_t(SERVICIO, "Pago de Servicio e Impuestos"))[0]
    assert a.fecha == datetime(2026, 7, 20, 15, 24)
    assert b.fecha == datetime(2026, 8, 5, 11, 8)


def test_beneficiario_no_arrastra_la_etiqueta_siguiente():
    m = parsear_transferencia(_correo_t(A_TERCERO,
                                        "Transacciones entre productos BHD y a otros Bancos"))[0]
    assert "mero" not in m.contraparte and "confirmaci" not in m.contraparte


# ── Capa 2: los 161 correos reales ───────────────────────────────────────

def _texto_cabecera(v):
    if not v:
        return ""
    return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, e in decode_header(v))


def _html_de(msg) -> str:
    for parte in (msg.walk() if msg.is_multipart() else [msg]):
        if parte.get_content_type() == "text/html":
            return (parte.get_payload(decode=True) or b"").decode(
                parte.get_content_charset() or "utf-8", "replace")
    return ""


def test_contra_los_fixtures_reales():
    if not FIXTURES.exists():
        print("     (sin fixtures en disco — capa 2 saltada)")
        return

    parseados, fallos, monedas = 0, [], {}
    for f in sorted(FIXTURES.glob("*.eml")):
        msg = email.message_from_bytes(f.read_bytes())
        if _texto_cabecera(msg.get("Subject")).strip() != ASUNTO:
            continue
        try:
            movs = parsear_consumo(_correo(_html_de(msg)))
            assert movs, f"{f.name}: no devolvió movimientos"
            for m in movs:
                monedas[m.moneda] = monedas.get(m.moneda, 0) + 1
            parseados += 1
        except ErrorDeParseo as e:
            fallos.append(f"{f.name}: {e}")

    print(f"     ({parseados} correos reales parseados, monedas={monedas})")
    assert not fallos, "fallaron:\n  " + "\n  ".join(fallos[:5])
    assert parseados == 161, f"esperaba 161 correos de consumo, encontré {parseados}"
    # El descubrimiento contó 128 RD y 33 US sobre esta misma muestra.
    assert monedas.get("USD", 0) == 33, f"esperaba 33 en USD, hubo {monedas.get('USD')}"
    assert monedas.get("DOP", 0) == 128, f"esperaba 128 en DOP, hubo {monedas.get('DOP')}"


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
