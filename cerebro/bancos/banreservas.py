"""Parser de las notificaciones de Banreservas (notificaciones@banreservas.com).

Banreservas manda TODO bajo el mismo asunto —"Notificaciones Banreservas"— así
que el remitente y el asunto no alcanzan para saber qué llegó. Lo que distingue
un consumo de una nómina es un título dentro del cuerpo. Verificado por testigo
sobre los 58 fixtures (2026-08-30):

     53  "Notificación de Consumo"       tarjeta, gasto      38 DOP / 15 USD
      2  "Transferencia en proceso"      salida, pendiente   Remitente vacío
      2  "Transferencia LBTR Recibida"   entrada             Origen + Banco Origen
      1  "Notificación Pago Nómina"      ingreso             sin comercio

El formato es de etiquetas, no de tabla: "Monto: DOP 254.90", "Comercio: X".
Eso lo hace más estable que el HTML de BHD —no depende de la maquetación— pero
más frágil ante un cambio de idioma o de nombre de etiqueta.

Y usa TRES notaciones de moneda distintas en el mismo remitente: "DOP 254.90",
"RD$ 1,500.00" y "DOP$ 12,267.85". Las tres van al contrato, que las normaliza.
"""
from __future__ import annotations

import re
import unicodedata

from cerebro.bancos.contrato import (
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    normalizar_estado,
    normalizar_fecha,
    normalizar_monto,
    normalizar_moneda,
    registrar,
)

BANCO = "banreservas"
REMITENTE = "notificaciones@banreservas.com"
ASUNTO = r"Notificaciones Banreservas"

_TAGS = re.compile(r"(?s)<[^>]+>")
_SCRIPTS = re.compile(r"(?is)<(script|style).*?</\1>")

# "Monto: DOP 254.90" / "Monto: RD$ 1,500.00" / "Monto: DOP$ 12,267.85"
_MONTO = re.compile(r"Monto:\s*([A-Z]{2,3}\$?|\$)\s*([\d.,]+)", re.I)
_TARJETA = re.compile(r"tarjeta\s+([A-Z][A-Z ]*?)\s*[•·•]+\s*(\d{4})", re.I)
_CUENTA = re.compile(r"[Cc]uenta(?: de [A-Za-z]+)?\s*[•·•]+\s*(\d{4})")


def _texto(correo: CorreoCrudo) -> str:
    """El cuerpo como una sola línea de texto plano, venga en plano o en HTML."""
    crudo = correo.texto or ""
    if not crudo.strip():
        html = _SCRIPTS.sub(" ", correo.html or "")
        crudo = _TAGS.sub(" ", html)
    return " ".join(crudo.replace("&nbsp;", " ").replace("\xa0", " ").split())


def _sin_acentos(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", v.lower())
                   if not unicodedata.combining(c))


def _campo(texto: str, etiqueta: str, hasta: str | None = None) -> str:
    """Valor de "Etiqueta: valor", cortando en la siguiente etiqueta conocida.

    Sin el corte, "Comercio: SM NACIONAL Fecha de transacción: 17/04/2026" se
    llevaría la fecha dentro del nombre del comercio. Devuelve "" si la etiqueta
    no está: hay campos legítimamente vacíos (el Remitente de una transferencia
    en proceso), y distinguir "vacío" de "ausente" es trabajo de quien llama.
    """
    fin = hasta or (r"(?:Monto|Estado|Comercio|Fecha(?: de transacci[oó]n)?|"
                    r"N[uú]mero de aprobaci[oó]n|Remitente|Origen|Banco Origen|"
                    r"Destino|Transacci[oó]n|Cuenta|Recibido por)\s*:|$")
    m = re.search(rf"{etiqueta}\s*:\s*(.*?)\s*(?={fin})", texto, re.I)
    return m.group(1).strip() if m else ""


def _monto_y_moneda(texto: str):
    m = _MONTO.search(texto)
    if not m:
        raise ErrorDeParseo("no encontré 'Monto:' en el correo de Banreservas")
    return normalizar_monto(m.group(2)), normalizar_moneda(m.group(1))


def _referencia(texto: str, correo: CorreoCrudo, fecha) -> str:
    """Los últimos dígitos de la tarjeta o cuenta, el nº de aprobación y la hora.

    La hora va acá porque `movimientos.fecha` es DATE y la pierde: sin ella dos
    consumos iguales del mismo día no se distinguen al deduplicar.
    """
    partes = ["BNR"]
    t = _TARJETA.search(texto)
    if t:
        partes.append(f"{t.group(1).strip()} ••{t.group(2)}")
    else:
        c = _CUENTA.search(texto)
        if c:
            partes.append(f"cuenta ••{c.group(1)}")
    aprob = _campo(texto, r"N[uú]mero de aprobaci[oó]n")
    if aprob:
        partes.append(f"aprob {aprob}")
    partes.append(f"{fecha:%H:%M}")
    partes.append(correo.cuenta)
    return " · ".join(partes)


def parsear(correo: CorreoCrudo) -> list[Movimiento]:
    """Un correo → un movimiento, según el título que trae dentro del cuerpo."""
    texto = _texto(correo)
    if not texto:
        raise ErrorDeParseo("correo de Banreservas sin cuerpo legible")
    plano = _sin_acentos(texto)

    fecha_txt = (_campo(texto, r"Fecha de transacci[oó]n")
                 or _campo(texto, r"Fecha"))
    if not fecha_txt:
        raise ErrorDeParseo("no encontré la fecha en el correo de Banreservas")
    fecha = normalizar_fecha(fecha_txt)
    monto, moneda = _monto_y_moneda(texto)

    # ── Consumo con tarjeta (53 de 58) ──────────────────────────────────
    if "notificacion de consumo" in plano:
        comercio = _campo(texto, "Comercio")
        if not comercio:
            raise ErrorDeParseo("consumo de Banreservas sin comercio")
        estado_txt = _campo(texto, "Estado")
        if not estado_txt:
            raise ErrorDeParseo("consumo de Banreservas sin estado")
        return [Movimiento(
            banco=BANCO, canal="tarjeta", tipo="gasto", fecha=fecha,
            monto=monto, moneda=moneda, contraparte=comercio,
            estado=normalizar_estado(estado_txt),
            referencia=_referencia(texto, correo, fecha))]

    # ── Transferencia saliente, todavía sin liquidar ────────────────────
    # El correo dice "presenta un consumo" (texto heredado de la plantilla de
    # tarjeta) pero se titula "en proceso" y NO trae Estado. Entra como
    # pendiente: los totales de gasto filtran por aprobada, así que no cuenta
    # hasta que el banco confirme. El "Remitente:" llega vacío en los dos casos
    # de la muestra, así que no se puede nombrar a la contraparte.
    if "transferencia en proceso" in plano:
        destinatario = _campo(texto, "Remitente") or _campo(texto, "Destino")
        return [Movimiento(
            banco=BANCO, canal="transferencia", tipo="gasto", fecha=fecha,
            monto=monto, moneda=moneda,
            contraparte=destinatario or "(transferencia en proceso · Banreservas "
                                        "no informa el destinatario)",
            estado="pendiente",
            referencia=_referencia(texto, correo, fecha))]

    # ── Transferencia recibida ──────────────────────────────────────────
    # OJO con el doble conteo: en los 2 casos de la muestra el Origen es la
    # propia titular moviendo plata entre sus bancos (APAP→Banreservas,
    # BHD→Banreservas). Registrarlo como ingreso infla los ingresos, y la
    # salida correspondiente en el otro banco se cuenta como gasto. Acá se
    # marca como ingreso porque es lo que el correo dice; separar los traspasos
    # propios es trabajo del registro de cuentas propias (t-09), que necesita
    # reconocer NOMBRES de titular y no solo números de cuenta.
    if "transferencia" in plano and "recibida" in plano:
        origen = _campo(texto, "Origen")
        banco_origen = _campo(texto, r"Banco Origen")
        if not origen:
            raise ErrorDeParseo("transferencia recibida sin Origen")
        contraparte = f"{origen} ({banco_origen})" if banco_origen else origen
        return [Movimiento(
            banco=BANCO, canal="transferencia", tipo="ingreso", fecha=fecha,
            monto=monto, moneda=moneda, contraparte=contraparte,
            estado="aprobada",
            referencia=_referencia(texto, correo, fecha))]

    # ── Pago de nómina ──────────────────────────────────────────────────
    if "pago nomina" in plano or "pago de nomina" in plano:
        return [Movimiento(
            banco=BANCO, canal="nomina", tipo="ingreso", fecha=fecha,
            monto=monto, moneda=moneda, contraparte="Pago de nómina",
            estado="aprobada",
            referencia=_referencia(texto, correo, fecha))]

    raise ErrorDeParseo(
        "tipo de notificación de Banreservas no reconocido. Primeras palabras: "
        f"{texto[:90]!r}")


registrar(REMITENTE, parsear, asunto=ASUNTO)
