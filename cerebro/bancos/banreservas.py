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
    asentar_reverso,
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
_MONTO = re.compile(r"Monto:\s*([A-Z]{2,3}\$?|\$)\s*([\d.,]*\d)", re.I)
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
                    r"Destino|Transacci[oó]n|Cuenta)\s*:"
                    # El pie del correo NO lleva dos puntos: "Recibido por los
                    # valores indicados en este comprobante. Este correo fue
                    # enviado a...". Exigirle ':' hacía que el corte no
                    # disparara nunca y que `referencia` se tragara 380
                    # caracteres de pie, con el correo del titular dentro.
                    r"|Recibido por los valores|Este correo fue enviado|$")
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
        # El campo "Estado:" lo escribe el banco, y `normalizar_estado()`
        # devuelve `reversada` para anulada / devuelta / reversada. Ese valor NO
        # lo acepta la base: hay que asentarlo antes. Hasta el 4-sep-2026 esto
        # pasaba el estado derecho al Movimiento, y el primer consumo anulado de
        # Banreservas habría violado el CHECK de `movimientos_estado_valido`.
        tipo, estado = asentar_reverso("gasto", normalizar_estado(estado_txt))
        return [Movimiento(
            banco=BANCO, canal="tarjeta", tipo=tipo, fecha=fecha,
            monto=monto, moneda=moneda, contraparte=comercio,
            estado=estado,
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


# ═══ La App: comprobantes de pago ════════════════════════════════════════
#
# Otro remitente, otro formato: notificacionestubancoapp@ manda 43 comprobantes
# con asunto "Recibo de la transacción", todos del mismo tipo. Los campos vienen
# etiquetados como en notificaciones@, pero con dos diferencias que muerden:
#
#   · La FECHA va en español con la hora colgando de un guion: "02 de Marzo
#     2026 - 09:03 PM". La maneja normalizar_fecha.
#   · Hay UN SEGUNDO MONTO: "Impuestos: DOP 0.75". Anclar el monto a "Monto:"
#     es lo que evita registrar el impuesto como si fuera la transacción.

REMITENTE_APP = "notificacionestubancoapp@banreservas.com"
ASUNTO_APP = r"Recibo de la transacci[oó]n|Comprobante de Pago"

_APP_MONTO = re.compile(r"Monto:\s*([A-Z]{2,3}\$?)\s*([\d.,]*\d)", re.I)
_APP_TIPO = re.compile(r"Transacci[oó]n:\s*(.+?)\s+Origen:", re.I)
_APP_ORIGEN = re.compile(r"Origen:\s*(.+?)\s*,\s*Cuenta", re.I)
_APP_DESTINO = re.compile(r"Destino:\s*(.+?)\s*,\s*Cuenta", re.I)
_APP_FECHA = re.compile(r"Fecha de transacci[oó]n:\s*(.+?)\s+(?:Impuestos|N[uú]mero)",
                        re.I)
_APP_NUM = re.compile(r"N[uú]mero de transacci[oó]n:?\s*(\S+)", re.I)


def parsear_app(correo: CorreoCrudo) -> list[Movimiento]:
    """Comprobante de pago hecho desde la App Banreservas.

    DIRECCIÓN — el punto delicado. El correo dice "fue realizada desde tu App",
    o sea que quien lo recibe es quien pagó: por defecto es un GASTO. En los 43
    de la muestra eso acierta 41 veces (Origen es Rosi en 40 y Tiziano en 1).

    Las otras 2 llegaron al buzón de CDS con Origen JOSE APOLINAR BRETON
    FERNANDEZ y concepto "reserva estudio marzo 3, 10-11": es un cliente
    pagando una sesión, o sea un INGRESO del estudio, y acá quedarían mal
    marcadas como gasto.

    No se arregla adivinando desde el parser. Se arregla con el registro de
    titulares propios (t-09): si el Origen NO es de la casa y el Destino SÍ, el
    movimiento es un ingreso. Por eso este parser guarda las DOS partes en
    `contraparte` — "ORIGEN → DESTINO" — para que ese paso tenga con qué
    corregir sin volver a abrir los correos.
    """
    texto = _texto(correo)
    if not texto:
        raise ErrorDeParseo("comprobante de la App sin cuerpo legible")

    mm = _APP_MONTO.search(texto)
    mf = _APP_FECHA.search(texto)
    if not mm or not mf:
        raise ErrorDeParseo("comprobante de la App sin Monto o sin Fecha")

    mo, md = _APP_ORIGEN.search(texto), _APP_DESTINO.search(texto)
    origen = " ".join(mo.group(1).split()) if mo else ""
    destino = " ".join(md.group(1).split()) if md else ""
    if not origen and not destino:
        raise ErrorDeParseo("comprobante de la App sin Origen ni Destino")

    tipo_txt = _APP_TIPO.search(texto)
    num = _APP_NUM.search(texto)
    fecha = normalizar_fecha(mf.group(1))

    partes_ref = ["BNR app"]
    if tipo_txt:
        partes_ref.append(" ".join(tipo_txt.group(1).split()))
    if num:
        partes_ref.append(f"nº {num.group(1)}")
    partes_ref.append(f"{fecha:%H:%M}")
    partes_ref.append(correo.cuenta)

    return [Movimiento(
        banco=BANCO, canal="transferencia", tipo="gasto", fecha=fecha,
        # Anclado a "Monto:" a propósito: el correo trae también
        # "Impuestos: DOP 0.75", y un patrón de moneda suelto lo confundiría.
        monto=normalizar_monto(mm.group(2)),
        moneda=normalizar_moneda(mm.group(1)),
        contraparte=f"{origen or '?'} → {destino or '?'}",
        estado="aprobada",
        referencia=" · ".join(partes_ref))]


registrar(REMITENTE_APP, parsear_app, asunto=ASUNTO_APP)


# ═══ Depósitos hechos en sucursal ════════════════════════════════════════
#
# Llegan por el remitente notificaciones@ pero con asunto propio, así que el
# parser general no los ve y hasta el 30-ago se descartaban EN SILENCIO: un
# ingreso real desaparecía sin dejar rastro ni error. Lo encontró el testigo
# de t-04b.
#
# Usan la plantilla de comprobante, con dos diferencias:
#   · La contraparte se llama "Depositante:", no "Origen:".
#   · Hay TRES montos — "Monto: DOP 500.00", "Efectivo DOP 500.00" y
#     "Cheques: 0 DOP 0.00". Anclar a "Monto:" evita registrar el cero.

ASUNTO_DEPOSITO = r"Dep(?:[oó]sito)?\.? de ahorros|Dep de ahorros"

# El corte va contra la siguiente ETIQUETA conocida, nunca contra una coma
# suelta. Dos motivos, los dos reales:
#   · Las razones sociales dominicanas llevan coma: el corpus ya tiene
#     "CENTRO DE TECNOLOGIA UNIVERSAL, SRL". Cortar en la coma la trunca.
#   · Si la etiqueta llega VACÍA —pasa en este banco: los dos correos de
#     transferencia en proceso traen "Remitente:" sin valor— un cortador laxo
#     se salta la etiqueta vacía y se traga el campo siguiente entero, nombre
#     de etiqueta incluido. Salía "Destino: ROSILIS ROMERO" como depositante y
#     la guarda de `if not depositante` no disparaba nunca: código muerto.
_DEP_FIN = (r"(?=\s*(?:-\s*ID|Destino|Cuenta est[aá]ndar|Fecha de transacci[oó]n|"
            r"Oficina de atenci[oó]n|Cajero|N[uú]mero de transacci[oó]n|"
            r"Comentario)\s*:?|$)")
_DEP_DEPOSITANTE = re.compile(rf"Depositante:\s*(.*?)\s*{_DEP_FIN}", re.I)
_DEP_DESTINO = re.compile(rf"Destino:\s*(.*?)\s*{_DEP_FIN}", re.I)


def parsear_deposito(correo: CorreoCrudo) -> list[Movimiento]:
    """Depósito recibido en una oficina comercial. Siempre es un INGRESO.

    A diferencia de las transferencias de la App —donde la dirección depende de
    quién sea de la casa— acá no hay ambigüedad: alguien fue a una sucursal y
    puso dinero en la cuenta. Entra.
    """
    texto = _texto(correo)
    if not texto:
        raise ErrorDeParseo("comprobante de depósito sin cuerpo legible")

    mm = _APP_MONTO.search(texto)
    mf = _APP_FECHA.search(texto) or re.search(
        r"Fecha de transacci[oó]n:\s*(.+?)\s+(?:Oficina|Cajero|N[uú]mero)", texto, re.I)
    if not mm or not mf:
        raise ErrorDeParseo("comprobante de depósito sin Monto o sin Fecha")

    md, mdest = _DEP_DEPOSITANTE.search(texto), _DEP_DESTINO.search(texto)
    depositante = " ".join(md.group(1).split()) if md else ""
    destino = " ".join(mdest.group(1).split()) if mdest else ""
    if not depositante:
        raise ErrorDeParseo("comprobante de depósito sin Depositante")

    fecha = normalizar_fecha(mf.group(1))
    num = _APP_NUM.search(texto)
    oficina = re.search(r"Oficina de atenci[oó]n:\s*(.+?)\s+(?:Cajero|N[uú]mero)",
                        texto, re.I)

    partes = ["BNR depósito"]
    if oficina:
        partes.append(" ".join(oficina.group(1).split()))
    if num:
        partes.append(f"nº {num.group(1)}")
    partes.append(f"{fecha:%H:%M}")
    partes.append(correo.cuenta)

    return [Movimiento(
        banco=BANCO, canal="transferencia", tipo="ingreso", fecha=fecha,
        # Anclado a "Monto:": el comprobante trae también "Efectivo DOP 500.00"
        # y "Cheques: 0 DOP 0.00", y ese cero no es la transacción.
        monto=normalizar_monto(mm.group(2)),
        moneda=normalizar_moneda(mm.group(1)),
        contraparte=f"{depositante}" + (f" → {destino}" if destino else ""),
        estado="aprobada",
        referencia=" · ".join(partes))]


registrar(REMITENTE, parsear_deposito, asunto=ASUNTO_DEPOSITO)
