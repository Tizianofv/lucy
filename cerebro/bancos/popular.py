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

from cerebro.bancos.propios import FLECHA
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


# ── Comprobantes en PDF (pagoselectronicos@) ─────────────────────────────
#
# Estas cuatro transferencias son DOP 187,000 — casi cinco veces todo el dinero
# que hay en los CUERPOS de los correos del Popular. El cuerpo del correo no
# trae una sola cifra: el comprobante entero va dentro de un PDF adjunto, y yo
# los había descartado como inparseables sin abrirlos.
#
# POR QUÉ SE ANCLA A PATRONES Y NO A POSICIONES. `extract_text` devuelve el
# formulario desarmado: todas las etiquetas primero y todos los valores después,
# en un orden que depende de cómo el generador dibujó el PDF. Leer "el valor
# número 14" funcionaría con estos cuatro y se rompería en silencio —dando otra
# cifra, no un error— el día que el banco mueva un campo. Cada dato se busca por
# lo que ES: el monto es lo único con dos decimales, la moneda está enumerada,
# el beneficiario va detrás de "CR a Cta. de".
#
# LA DIRECCIÓN DEL DINERO NO SE DECIDE ACÁ, y esa es la corrección importante.
#
# Yo había leído "NOTIFICACION CREDITO a un tercero en otro banco" como dinero
# que sale, y estaba al revés: la cuenta beneficiaria es de la hermana de
# Tiziano, y algunos clientes depositan ahí. Son RD$187,000 de INGRESO que iban
# a entrar como gasto — el signo cambiado en la cifra más grande del banco.
#
# La lección no es "acordarse de este caso": es que un parser no puede saber de
# qué lado del mostrador está. Eso lo sabe el registro de cuentas propias, y
# `propios.reclasificar()` ya tiene la regla exacta —"solo el DESTINO es de la
# casa → ingreso"—. Por eso la contraparte se emite como `origen → destino`: es
# la forma que ese registro sabe leer. Si mañana el beneficiario es un tercero
# de verdad, la misma regla lo deja como gasto sin que nadie toque este archivo.
#
# El comprobante NO dice quién pagó: trae al banco emisor y al beneficiario, y
# nada más. Se dice así, con la misma fórmula que ya usa el parser de arriba,
# en vez de inventar un pagador.

REMITENTE_PAGOS = "pagoselectronicos@popularenlinea.com"

# El comprobante no trae al pagador. Se dice, no se inventa: un movimiento con
# un pagador fabricado ensucia el aprendizaje de categorías para siempre.
SIN_PAGADOR = "(el comprobante no dice quién pagó)"

# El monto es el único número con dos decimales del comprobante. Si aparece más
# de uno, el formato cambió y hay que mirarlo: elegir "el primero" sería fingir
# que se entendió.
_PDF_MONTO = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")
_PDF_MONEDA = re.compile(r"\b(PESOS DOMINICANOS|D[OÓ]LARES(?:\s+\w+)?)\b", re.I)
_PDF_FECHA = re.compile(
    r"\b\d{2}-(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)-\d{4}\b", re.I)
# "CR a Cta. de MARILANDIA VARGAS" — el CR/DB dice el sentido dentro del PDF.
_PDF_BENEF = re.compile(r"\b(CR|DB)\s+a\s+Cta\.\s*de\s+(.+?)\s*$",
                        re.I | re.M)
_PDF_TIPO = re.compile(r"NOTIFICACION\s+(CREDITO|DEBITO)", re.I)


def _texto_de_pdf(datos: bytes) -> str:
    """El texto del PDF. Cadena vacía si no se puede leer.

    pypdf se importa acá adentro y no arriba: si algún día falta la
    dependencia, lo único que deja de funcionar es este parser, y no se cae el
    import de todo el módulo de bancos con él.
    """
    try:
        import io

        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(datos))
        return "\n".join(p.extract_text() or "" for p in r.pages)
    except Exception:
        return ""


def parsear_comprobante_pdf(correo: CorreoCrudo) -> list[Movimiento]:
    """El comprobante de transferencia que viaja como PDF adjunto."""
    movs: list[Movimiento] = []
    for nombre, datos in correo.adjuntos:
        texto = _texto_de_pdf(datos)
        tipo_doc = _PDF_TIPO.search(texto)
        if not tipo_doc:
            # No es un comprobante. El buzón también recibe PDF de marketing
            # ("Resumen-Webinar-Seguridad-en-la-Era-de-la-IA.pdf"), y esos no
            # son un fallo: se ignoran sin ruido.
            continue

        # dict.fromkeys y no set: quita repetidos conservando el orden, para
        # que el mensaje de error muestre los montos como salen en el papel.
        montos = list(dict.fromkeys(_PDF_MONTO.findall(texto)))
        if len(montos) != 1:
            raise ErrorDeParseo(
                f"{nombre}: esperaba UN monto con decimales y encontré "
                f"{len(montos)} ({montos[:4]}). El formato del comprobante "
                "cambió; elegir uno a ojo daría una cifra equivocada sin avisar.")

        moneda = _PDF_MONEDA.search(texto)
        if not moneda:
            raise ErrorDeParseo(f"{nombre}: no dice la moneda.")
        cruda = moneda.group(1).upper()
        divisa = "DOP" if cruda.startswith("PESOS") else "USD"

        fechas = _PDF_FECHA.findall(texto)
        if not fechas:
            raise ErrorDeParseo(f"{nombre}: no encuentro la fecha.")
        # La última es la Fecha Efectiva —cuándo se movió el dinero— y es la que
        # importa; la primera es la de emisión del documento.
        cuando = normalizar_fecha(fechas[-1])

        benef = _PDF_BENEF.search(texto)
        if not benef:
            raise ErrorDeParseo(
                f"{nombre}: no encuentro a quién se le transfirió. Sin "
                "contraparte el movimiento no se puede deduplicar ni clasificar.")
        sentido, quien = benef.group(1).upper(), benef.group(2).strip()

        # `origen → destino`, que es lo que propios.reclasificar() sabe leer. El
        # origen va vacío de nombre porque el comprobante no lo trae, y eso hace
        # justo lo correcto: no es de la casa, el destino sí, luego ingreso.
        es_credito = tipo_doc.group(1).upper() == "CREDITO" and sentido == "CR"
        contraparte = (f"{SIN_PAGADOR} {FLECHA} {quien}" if es_credito
                       else f"{quien} {FLECHA} {SIN_PAGADOR}")
        movs.append(Movimiento(
            banco=BANCO,
            # El canal es "transferencia", el vocabulario cerrado del contrato.
            # "IBANKING" es el sistema por el que se hizo, no lo que es — y el
            # contrato rechazándolo hizo exactamente lo que tiene que hacer.
            canal="transferencia",
            # Provisional: es lo que dice el papel leído solo. Quien tiene la
            # última palabra es propios.reclasificar(), que sí sabe qué cuentas
            # son de la casa. Si el registro está vacío, esto es lo que queda —
            # y por eso el default es el literal, no una corazonada.
            tipo="gasto" if es_credito else "ingreso",
            fecha=cuando,
            monto=normalizar_monto(montos[0]),
            moneda=normalizar_moneda(divisa),
            contraparte=contraparte,
            estado="aprobada",
            # El nombre del archivo identifica el comprobante y no depende de
            # dónde caiga cada campo dentro del PDF.
            referencia=nombre.removesuffix(".pdf").removesuffix(".PDF")))
    return movs


registrar(REMITENTE_PAGOS, parsear_comprobante_pdf)
