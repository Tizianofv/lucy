"""Tests del contrato de los parsers de banco (cerebro/bancos/contrato.py).

Herméticos de verdad: el contrato es stdlib puro —sin base, sin red, sin IA—
así que no hace falta stubear nada.

Lo que se prueba con más saña es `normalizar_monto`, porque es donde vive el
error más caro del sistema: confundir el separador de miles con el decimal
convierte RD$2,500.00 en RD$2.50, o al revés multiplica por mil. Un parser
que revienta se arregla en una tarde; uno que se equivoca en silencio
envenena los totales durante meses sin que nadie lo note.

Los casos vienen de correos reales: la muestra de 161 alertas de BHD trae
128 consumos en RD y 33 en US, con montos como "$2,500.00".

Correr:  python3 tests/test_contrato_bancos.py
(o con pytest si está instalado: pytest tests/test_contrato_bancos.py)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.contrato import (  # noqa: E402
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    buscar_parser,
    normalizar_estado,
    normalizar_fecha,
    normalizar_monto,
    normalizar_moneda,
    registrar,
)


def _revienta(fn, *args) -> bool:
    try:
        fn(*args)
    except ErrorDeParseo:
        return True
    return False


# ── Monto: el que puede costar dinero ────────────────────────────────────

def test_monto_formato_de_bhd():
    """El formato real de los correos: coma miles, punto decimal."""
    assert normalizar_monto("$2,500.00") == Decimal("2500.00")
    assert normalizar_monto("RD$ 1,234.56") == Decimal("1234.56")
    assert normalizar_monto("$45.90") == Decimal("45.90")
    assert normalizar_monto("$1,000,000.00") == Decimal("1000000.00")


def test_monto_formato_europeo():
    """Punto miles, coma decimal. Ningún banco lo usa HOY, pero el día que
    uno lo haga tiene que salir bien o salir con error — no salir mal."""
    assert normalizar_monto("2.500,00") == Decimal("2500.00")
    assert normalizar_monto("1.234.567,89") == Decimal("1234567.89")


def test_monto_un_solo_separador():
    """El caso peligroso: '2.500' es ambiguo para un humano, no para la regla.
    Tres dígitos después = miles (convención universal); uno o dos = decimal."""
    assert normalizar_monto("2.500") == Decimal("2500")     # miles
    assert normalizar_monto("2,500") == Decimal("2500")     # miles
    assert normalizar_monto("2.50") == Decimal("2.50")      # decimal
    assert normalizar_monto("2,5") == Decimal("2.5")        # decimal
    assert normalizar_monto("1.234.567") == Decimal("1234567")


def test_monto_ambiguo_revienta_en_vez_de_adivinar():
    """4 dígitos tras el separador no es ni decimal ni miles: error, no
    adivinanza. Esta es LA prueba que justifica todo el módulo."""
    assert _revienta(normalizar_monto, "2.5000")
    assert _revienta(normalizar_monto, "1,23456")


def test_monto_basura():
    assert _revienta(normalizar_monto, "")
    assert _revienta(normalizar_monto, "N/A")
    assert _revienta(normalizar_monto, "Monto")     # la fila de cabecera
    assert _revienta(normalizar_monto, "$0.00")     # cero no es un movimiento


def test_monto_es_decimal_no_float():
    """Con floats, sumar centavos acumula error. La tabla es NUMERIC(12,2)."""
    assert isinstance(normalizar_monto("$10.10"), Decimal)
    total = sum(normalizar_monto("$0.10") for _ in range(10))
    assert total == Decimal("1.00")


# ── Moneda: el otro error caro ───────────────────────────────────────────

def test_moneda_sale_iso():
    assert normalizar_moneda("RD") == "DOP"
    assert normalizar_moneda("US") == "USD"
    assert normalizar_moneda("RD$") == "DOP"
    assert normalizar_moneda(" us$ ") == "USD"


def test_moneda_desconocida_revienta():
    """Sin default a propósito: asumir pesos sobre un consumo en dólares
    divide el gasto real entre ~60 y nadie lo nota."""
    assert _revienta(normalizar_moneda, "EUR")
    assert _revienta(normalizar_moneda, "")
    assert _revienta(normalizar_moneda, "Moneda")   # la fila de cabecera


# ── Fecha ────────────────────────────────────────────────────────────────

def test_fecha_formato_bhd():
    assert normalizar_fecha("08/07/2026 04:07 pm") == datetime(2026, 7, 8, 16, 7)
    assert normalizar_fecha("29/08/2026 10:38 am") == datetime(2026, 8, 29, 10, 38)


def test_fecha_medianoche_y_mediodia():
    """El error clásico del 12h: 12am es 00:00 y 12pm es 12:00, no al revés."""
    assert normalizar_fecha("01/01/2026 12:00 am").hour == 0
    assert normalizar_fecha("01/01/2026 12:00 pm").hour == 12


def test_fecha_sin_hora_y_en_letras():
    assert normalizar_fecha("08/07/2026") == datetime(2026, 7, 8, 0, 0)
    assert normalizar_fecha("8 de julio de 2026") == datetime(2026, 7, 8)


def test_fecha_irreconocible_revienta():
    assert _revienta(normalizar_fecha, "ayer")
    assert _revienta(normalizar_fecha, "Fecha")     # la fila de cabecera


# ── Estado ───────────────────────────────────────────────────────────────

def test_estado_normaliza():
    assert normalizar_estado("Aprobada") == "aprobada"
    assert normalizar_estado("DECLINADA") == "declinada"
    assert normalizar_estado("Reversada") == "reversada"


def test_estado_desconocido_revienta():
    """Caer en 'aprobada' por defecto sumaría a los gastos plata que el banco
    nunca movió."""
    assert _revienta(normalizar_estado, "vaya uno a saber")
    assert _revienta(normalizar_estado, "")


# ── El dataclass se valida solo ──────────────────────────────────────────

def _mov(**kw):
    base = dict(banco="bhd", canal="tarjeta", tipo="gasto",
                fecha=datetime(2026, 7, 8, 16, 7), monto=Decimal("2500.00"),
                moneda="DOP", contraparte="INSTITUTO ESPAILLAT CA",
                estado="aprobada", referencia="****1234")
    base.update(kw)
    return Movimiento(**base)


def test_movimiento_valido():
    m = _mov()
    assert m.monto == Decimal("2500.00") and m.moneda == "DOP"


def test_movimiento_rechaza_basura():
    assert _revienta(lambda: _mov(moneda="RD"))          # sin normalizar
    assert _revienta(lambda: _mov(monto=2500.0))         # float
    assert _revienta(lambda: _mov(monto=Decimal("-1")))  # negativo
    assert _revienta(lambda: _mov(tipo="compra"))        # fuera del vocabulario
    assert _revienta(lambda: _mov(contraparte="  "))     # vacía


def test_clave_dedupe_distingue_e_iguala():
    a = _mov()
    assert a.clave_dedupe() == _mov().clave_dedupe()
    assert a.clave_dedupe() != _mov(monto=Decimal("2500.01")).clave_dedupe()
    assert a.clave_dedupe() != _mov(banco="banesco").clave_dedupe()
    # La hora entra en la clave: dos cafés del mismo día no se confunden,
    # aunque `movimientos.fecha` sea DATE y pierda la hora.
    assert a.clave_dedupe() != _mov(fecha=datetime(2026, 7, 8, 9, 0)).clave_dedupe()
    # Los acentos y mayúsculas del comercio no deberían partir la clave.
    assert a.clave_dedupe() == _mov(contraparte="instituto espaillat ca").clave_dedupe()


# ── Enrutamiento ─────────────────────────────────────────────────────────

def _falso(correo):
    return []


def _correo(remitente, asunto):
    return CorreoCrudo(remitente=remitente, asunto=asunto,
                       fecha_correo=datetime(2026, 7, 8), html="", texto="",
                       cuenta="x@y.com", uid="1")


def test_ruteo_por_remitente_exacto():
    """Lo que salió de los correos reales: bhd.com.do tiene tres remitentes y
    solo `alertas@` es transaccional; `info@` es publicidad."""
    registrar("alertas@bhd.com.do", _falso)
    assert buscar_parser("alertas@bhd.com.do", "BHD Notificación") is _falso
    assert buscar_parser("ALERTAS@BHD.COM.DO", "BHD Notificación") is _falso
    assert buscar_parser("info@bhd.com.do", "BHD Notificación") is None
    assert buscar_parser("infopb@bhd.com.do", "lo que sea") is None


def test_guion_vs_sin_guion():
    """Caso real de APAP: `no-reply@` es transaccional y `noreply@` publicidad.
    Se prueba con un dominio inventado a propósito — usar el real haría que este
    test dependiera de qué parsers estén registrados en producción."""
    registrar("no-reply@banco-de-prueba.test", _falso)
    assert buscar_parser("no-reply@banco-de-prueba.test", "Transferencia") is _falso
    assert buscar_parser("noreply@banco-de-prueba.test", "Transferencia") is None


def test_un_remitente_con_varios_asuntos():
    """`alertas@bhd.com.do` manda consumos Y traspasos entre cuentas propias:
    mismo remitente, formatos y significados distintos."""
    def _traspaso(correo):
        return []
    registrar("alertas@bhd.com.do", _traspaso, asunto=r"entre mis productos")
    assert buscar_parser("alertas@bhd.com.do",
                         "Transacciones entre mis productos") is _traspaso


def test_asuntos_ignorados():
    """Traen monto pero no son movimientos. Parsearlos inventaría gastos."""
    registrar("alertas@bhd.com.do", _falso)
    assert buscar_parser("alertas@bhd.com.do",
                         "Código de validación de compra") is None
    assert buscar_parser("no-reply@apap.com.do",
                         "Afiliación Nuevo Beneficiario") is None
    assert buscar_parser("alertas@bhd.com.do",
                         "Estado de cuenta de tu tarjeta") is None


if __name__ == "__main__":
    fallos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {nombre}")
        except AssertionError as e:
            fallos += 1
            print(f"  ✗ {nombre}  — {e or 'assert falló'}")
        except Exception as e:
            fallos += 1
            print(f"  ✗ {nombre}  — {type(e).__name__}: {e}")
    print(f"\n{'FALLARON ' + str(fallos) if fallos else 'Todo verde'}")
    sys.exit(1 if fallos else 0)
