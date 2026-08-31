"""Parser de las notificaciones de Banco Popular Dominicano.

Popular es el banco que menos notifica de los cinco, y hay que decir qué NO
manda porque es lo que decidió el alcance: en 154 correos de un año no hay ni
uno solo de consumo con tarjeta (verificado por testigo, 2026-08-30). Quien
espere ver aquí sus compras no las va a encontrar; Popular avisa por la app.

Lo que sí manda, y este parser cubre:

   4  "Notificación transf recibida via app e IB"    ingreso   DOP
   8  "Aviso cargo comisión por bajo balance"        gasto     USD
   4  "Notificacion Reverso a cuenta por sobregiro"  ingreso   DOP

Los otros 130 correos son publicidad de `popularteinforma@`, salvo 8 cargos
por bajo balance que salen de ese mismo buzón —el remitente de marketing manda
también un cargo real, así que aquí el asunto pesa más que el remitente.

DOS COSAS QUE NO ARREGLA ESTE MÓDULO Y CONVIENE SABER:

  · POPULAR DUPLICA. De los 4 correos de transferencia recibida, dos pares son
    la misma transacción notificada dos veces con 6 y 1 segundo de diferencia;
    de los 4 reversos, dos son el mismo. Este parser emite un movimiento por
    correo, como todos: quien deduplica es la ingesta, comparando
    `Movimiento.clave_dedupe()`. Los duplicados de Popular producen claves
    idénticas a propósito.

  · LO GORDO ESTÁ EN LOS ADJUNTOS. Los correos de `pagoselectronicos@` traen
    el detalle en un PDF, no en el cuerpo: cuatro transferencias por DOP
    187,000.00, casi cinco veces todo lo que se parsea acá. No se cubren
    todavía (t-07c).
"""
from __future__ import annotations

import html as _html
import re

from cerebro.bancos.contrato import (
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    normalizar_fecha,
    normalizar_monto,
    normalizar_moneda,
    registrar,
)

BANCO = "popular"
REMITENTE_NOTIF = "notificaciones@popularenlinea.com"
REMITENTE_INFO = "popularteinforma@popularenlinea.com"

# "Monto Fecha Canal RD 17,000.00 10/8/2026 IBANKING"
#
# El canal va enumerado, NO como clase abierta. Con [A-Z ]+ bajo re.I —que en
# IGNORECASE también casa minúsculas— el grupo se comía la frase siguiente
# entera ("IBANKING Si necesita mas informacion o no reconoce esta
# transaccion") y solo frenaba por accidente en el primer carácter acentuado.
# Afectaba a los 4 correos de este tipo, el 100%. Quitar el re.I del grupo no
# alcanzaría: el texto que sigue empieza con "Si" en mayúscula.
_CANALES = r"APP\s+POPULAR|IBANKING|INTERNET\s+BANKING|SUCURSAL|CAJERO"
_TRANSF = re.compile(
    r"Monto\s+Fecha\s+Canal\s+(?P<moneda>RD|USD?)\$?\s*(?P<monto>[\d.,]*\d)\s+"
    rf"(?P<fecha>\d{{1,2}}/\d{{1,2}}/\d{{2,4}})\s+(?P<canal>{_CANALES})", re.I)

# "presenta un CARGO POR BAJO BALANCE MÍNIMO de USD$12.00"
_CARGO = re.compile(
    r"CARGO POR BAJO BALANCE M[IÍ]NIMO\s+de\s*(?P<moneda>RD\$?|USD?\$?)\s*"
    r"(?P<monto>[\d.,]*\d)", re.I)

# "devolución de RD 0.12 correspondiente al cargo por sobregiro aplicado a su
#  cuenta No 9142 en fecha 26/3/26"
_REVERSO = re.compile(
    r"devoluci[oó]n de\s*(?P<moneda>RD\$?|USD?\$?)\s*(?P<monto>[\d.,]*\d).{0,120}?"
    r"en fecha\s*(?P<fecha>\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.S)

# "cuenta terminada en 9142", "cuenta No 9142" y también
# "cuenta AHORRO EN DÓLARES terminada en 0489": entre "cuenta" y los dígitos
# puede ir el nombre del producto. Sin admitirlo, los 8 cargos por bajo balance
# perdían su número — y era la única marca de que son sobre otra cuenta.
_CUENTA = re.compile(
    r"cuenta\b(?:\s+No\.?)?(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,4}?"
    r"(?:\s+terminada\s+en)?\s*(\d{4})\b", re.I)


def _texto(correo: CorreoCrudo) -> str:
    crudo = correo.html or ""
    if crudo.strip():
        crudo = re.sub(r"(?is)<(script|style).*?</\1>", " ", crudo)
        crudo = re.sub(r"(?s)<[^>]+>", " ", crudo)
    else:
        crudo = correo.texto or ""
    return " ".join(_html.unescape(crudo).replace("\xa0", " ").split())


def _referencia(texto: str, correo: CorreoCrudo, extra: str = "") -> str:
    partes = ["Popular"]
    mc = _CUENTA.search(texto)
    if mc:
        partes.append(f"cuenta ••{mc.group(1)}")
    if extra:
        partes.append(extra)
    partes.append(correo.cuenta)
    return " · ".join(partes)


def parsear_transferencia_recibida(correo: CorreoCrudo) -> list[Movimiento]:
    """Transferencia que ENTRA a la cuenta. Es lo único de volumen real acá."""
    texto = _texto(correo)
    m = _TRANSF.search(texto)
    if not m:
        raise ErrorDeParseo(
            "transferencia recibida de Popular sin la tabla 'Monto Fecha Canal'")
    return [Movimiento(
        banco=BANCO, canal="transferencia", tipo="ingreso",
        fecha=normalizar_fecha(m.group("fecha")),
        monto=normalizar_monto(m.group("monto")),
        moneda=normalizar_moneda(m.group("moneda")),
        contraparte="(Popular no informa quién envió)",
        estado="aprobada",
        referencia=_referencia(texto, correo, m.group("canal").strip()))]


def parsear_cargo_bajo_balance(correo: CorreoCrudo) -> list[Movimiento]:
    """Comisión por caer bajo el balance mínimo. Sale plata, es gasto.

    Los 8 de la muestra son en USD sobre una cuenta de ahorro en dólares — de
    los pocos movimientos en dólares del sistema fuera de los consumos de BHD.
    El correo NO trae fecha de la transacción, así que se usa la del correo:
    es un aviso automático, llega el mismo día del cargo.
    """
    texto = _texto(correo)
    m = _CARGO.search(texto)
    if not m:
        raise ErrorDeParseo("aviso de Popular sin 'CARGO POR BAJO BALANCE MÍNIMO'")
    return [Movimiento(
        banco=BANCO, canal="servicio", tipo="gasto",
        fecha=correo.fecha_correo,
        monto=normalizar_monto(m.group("monto")),
        moneda=normalizar_moneda(m.group("moneda")),
        contraparte="Banco Popular · comisión por bajo balance",
        estado="aprobada",
        referencia=_referencia(texto, correo))]


def parsear_reverso_sobregiro(correo: CorreoCrudo) -> list[Movimiento]:
    """Devolución de un cargo por sobregiro. Plata que vuelve: ingreso."""
    texto = _texto(correo)
    m = _REVERSO.search(texto)
    if not m:
        raise ErrorDeParseo("reverso de Popular sin monto o sin fecha")
    return [Movimiento(
        banco=BANCO, canal="servicio", tipo="ingreso",
        fecha=normalizar_fecha(m.group("fecha")),
        monto=normalizar_monto(m.group("monto")),
        moneda=normalizar_moneda(m.group("moneda")),
        contraparte="Banco Popular · reverso de cargo por sobregiro",
        estado="aprobada",
        referencia=_referencia(texto, correo))]


registrar(REMITENTE_NOTIF, parsear_transferencia_recibida,
          asunto=r"transf\s+recibida")
registrar(REMITENTE_NOTIF, parsear_reverso_sobregiro,
          asunto=r"Reverso a cuenta por sobregiro")
# El cargo por bajo balance sale del buzón de MARKETING, no del de
# notificaciones: acá el asunto pesa más que el remitente.
registrar(REMITENTE_INFO, parsear_cargo_bajo_balance,
          asunto=r"cargo comisi[oó]n por bajo balance")
