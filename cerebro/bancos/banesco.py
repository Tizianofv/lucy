"""Parser de las notificaciones de Banesco (notificaciones@banesco.com.do).

Banesco no manda campos etiquetados: manda PROSA. Y bajo el mismo asunto
—"Alerta de Consumo Banesco RD"— usa dos redacciones distintas según el
resultado de la transacción. Verificado sobre los 111 fixtures (2026-08-30):

  109  "Alerta de Consumo Banesco RD"
         103  aprobadas — "...presenta un consumo de RD$ 3,183.38, en SM
              NACIONAL MAXIMO GOM y su estado es aprobada. Dispones de
              RD$ 663,903.86."
           6  declinadas — "...la transaccion realizada en el COMERCIAL DE PENA
              por un monto de RD 6,350.00 ... ha sido rechazada. Motivo de la
              declinación: Incorrect PIN."
    1  "Notificación de Transferencia Recibida"    entrada
    1  "Notificación de Transferencia Realizada"   salida

TRES trampas que este banco pone y que un parser ingenuo no ve:

  · DOS MONTOS en la misma frase. "Dispones de RD$ 663,903.86" es el BALANCE
    disponible, no la transacción. Un regex de "el primer RD$ que aparezca"
    acierta por accidente; uno de "el último" registraría el balance como
    gasto. Por eso todo va anclado a "consumo de" o "por un monto de".

  · Las declinadas NO TRAEN FECHA. Ninguna de las 6. Se usa la fecha del
    correo, que para una alerta transaccional llega a los segundos.

  · CUATRO notaciones de moneda entre los dos formatos: "RD$ 3,183.38" en las
    aprobadas, "RD 6,350.00" (sin símbolo) en las declinadas y "DOP50,000.00"
    (sin espacio) en las transferencias.
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

BANCO = "banesco"
REMITENTE = "notificaciones@banesco.com.do"

_TAGS = re.compile(r"(?s)<[^>]+>")
_SCRIPTS = re.compile(r"(?is)<(script|style).*?</\1>")

# Aprobada: el monto va SIEMPRE detrás de "consumo de". Anclarlo ahí es lo que
# evita capturar el "Dispones de RD$ ..." del final, que es el balance.
# El monto termina OBLIGATORIAMENTE en dígito ([\d.,]*\d). Sin eso se traga la
# coma que separa el monto del comercio —"RD$ 1,000.00, en COMERCIO"— y llega a
# normalizar_monto como "1,000.00,", que lo rechaza. Lo rechaza bien: es basura.
_APROBADA = re.compile(
    r"terminada en\s+(?P<tarjeta>\d+).{0,40}?"
    r"en fecha\s+(?P<fecha>[\d/]+).{0,40}?"
    r"consumo de\s+(?P<moneda>[A-Z]{2,3}\$?)\s*(?P<monto>[\d.,]*\d)\s*,?\s*"
    r"en\s+(?P<comercio>.+?)\s+y su estado es\s+(?P<estado>[a-záéíóúñ ]+?)\s*\.",
    re.I | re.S)

# Declinada: otra redacción, sin fecha y con la moneda sin símbolo.
_DECLINADA = re.compile(
    r"transacci[oó]n realizada en el\s+(?P<comercio>.+?)\s+"
    r"por un monto de\s+(?P<moneda>[A-Z]{2,3}\$?)\s*(?P<monto>[\d.,]*\d)\s*,\s*"
    r"con tu Tarjeta[^.]*?terminada en\s+(?P<tarjeta>\d+)[^.]*?ha sido rechazada\."
    r"(?:\s*Motivo de la declinaci[oó]n:\s*(?P<motivo>.+?)\s*\.)?",
    re.I | re.S)

# Transferencias: "Monto: DOP50,000.00" — sin espacio entre moneda y cifra.
_TRANSF_MONTO = re.compile(r"Monto:\s*([A-Z]{2,3}\$?)\s*([\d.,]*\d)", re.I)
_TRANSF_FECHA = re.compile(r"Fecha de la Transacci[oó]n:\s*([\d/]+)", re.I)
_TRANSF_REF = re.compile(r"No\.?\s*Referencia:\s*(\S+)", re.I)
_TRANSF_BANCO = re.compile(r"Banco (?:Emisor|Beneficiario):\s*(.+?)\s*"
                           r"(?=Cuenta|No\.|Fecha|$)", re.I)


def _texto(correo: CorreoCrudo) -> str:
    crudo = correo.texto or ""
    if not crudo.strip():
        crudo = _TAGS.sub(" ", _SCRIPTS.sub(" ", correo.html or ""))
    return " ".join(crudo.replace("&nbsp;", " ").replace("\xa0", " ").split())


def _sin_acentos(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", v.lower())
                   if not unicodedata.combining(c))


def parsear_consumo(correo: CorreoCrudo) -> list[Movimiento]:
    """Alerta de consumo con tarjeta, aprobada o declinada."""
    texto = _texto(correo)
    if not texto:
        raise ErrorDeParseo("correo de Banesco sin cuerpo legible")

    m = _APROBADA.search(texto)
    if m:
        comercio = " ".join(m.group("comercio").split())
        if not comercio:
            raise ErrorDeParseo("consumo de Banesco sin comercio")
        return [Movimiento(
            banco=BANCO, canal="tarjeta", tipo="gasto",
            fecha=normalizar_fecha(m.group("fecha")),
            monto=normalizar_monto(m.group("monto")),
            moneda=normalizar_moneda(m.group("moneda")),
            contraparte=comercio,
            estado=normalizar_estado(m.group("estado")),
            referencia=f"Banesco ••{m.group('tarjeta')} · {correo.cuenta}")]

    m = _DECLINADA.search(texto)
    if m:
        motivo = " ".join((m.group("motivo") or "").split())
        return [Movimiento(
            banco=BANCO, canal="tarjeta", tipo="gasto",
            # Las 6 declinadas de la muestra no traen fecha de transacción.
            # La del correo es la mejor aproximación disponible: una alerta de
            # rechazo llega en segundos, no al día siguiente.
            fecha=correo.fecha_correo,
            monto=normalizar_monto(m.group("monto")),
            moneda=normalizar_moneda(m.group("moneda")),
            contraparte=" ".join(m.group("comercio").split()),
            estado="declinada",
            referencia=(f"Banesco ••{m.group('tarjeta')}"
                        + (f" · rechazo: {motivo}" if motivo else "")
                        + f" · {correo.cuenta}"))]

    raise ErrorDeParseo(
        "alerta de Banesco que no encaja ni en la redacción de aprobada ni en "
        f"la de declinada. Empieza: {texto[:110]!r}")


def _transferencia(correo: CorreoCrudo, tipo: str) -> list[Movimiento]:
    texto = _texto(correo)
    mm = _TRANSF_MONTO.search(texto)
    mf = _TRANSF_FECHA.search(texto)
    if not mm or not mf:
        raise ErrorDeParseo(
            "transferencia de Banesco sin Monto o sin Fecha de la Transacción")
    banco = _TRANSF_BANCO.search(texto)
    ref = _TRANSF_REF.search(texto)
    otro = " ".join(banco.group(1).split()) if banco else "(banco no informado)"
    return [Movimiento(
        banco=BANCO, canal="transferencia", tipo=tipo,
        fecha=normalizar_fecha(mf.group(1)),
        monto=normalizar_monto(mm.group(2)),
        moneda=normalizar_moneda(mm.group(1)),
        contraparte=otro,
        estado="aprobada",
        referencia=(f"Banesco transf"
                    + (f" · ref {ref.group(1)}" if ref else "")
                    + f" · {correo.cuenta}"))]


def parsear_recibida(correo: CorreoCrudo) -> list[Movimiento]:
    return _transferencia(correo, "ingreso")


def parsear_realizada(correo: CorreoCrudo) -> list[Movimiento]:
    return _transferencia(correo, "gasto")


registrar(REMITENTE, parsear_consumo, asunto=r"Alerta de Consumo")
registrar(REMITENTE, parsear_recibida, asunto=r"Transferencia Recibida")
registrar(REMITENTE, parsear_realizada, asunto=r"Transferencia Realizada")
