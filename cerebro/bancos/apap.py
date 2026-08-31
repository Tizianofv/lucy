"""Parser de las notificaciones de APAP (no-reply@apap.com.do).

APAP es una asociación de ahorros y préstamos, no un banco de tarjetas: acá no
hay consumos. Lo que manda son transferencias, depósitos y pagos de intereses.

OJO CON EL REMITENTE. APAP usa DOS grafías del mismo buzón, y no son un
descuido: `no-reply@` es transaccional y `noreply@` es publicidad y encuestas.
Un enrutador que normalizara el guion las mezclaría. Por eso solo se registra
la primera.

De los 75 correos de `no-reply@` en un año, 55 son movimientos. Los otros 20
—afiliación de beneficiario, apertura de producto, código temporal, "has
subido de nivel"— no mueven dinero y no se parsean.

Dos plantillas:

  47  Transferencias y depósitos — "Fecha:", "Hora:", "Monto RD$:", "Tipo:",
      "Número de referencia:", "Beneficiario:", "Cuenta destino:".
   8  Pago de intereses de certificado — otra cosa: trae TRES montos, y elegir
      mal cambia lo que entra a la cuenta (ver abajo).

Y una peculiaridad de sus datos: la hora llega corrupta en algunos correos
("Hora: 92:20", "Hora: 70:90"). No es un error de parseo nuestro, es lo que
manda APAP. Se ignora la hora y se conserva la fecha, que sí es válida.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from cerebro.bancos.contrato import (
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    normalizar_fecha,
    normalizar_monto,
    normalizar_moneda,
    registrar,
)

BANCO = "apap"
REMITENTE = "no-reply@apap.com.do"          # con guion: el transaccional

_TAGS = re.compile(r"(?s)<[^>]+>")
_SCRIPTS = re.compile(r"(?is)<(script|style).*?</\1>")

# La moneda viaja en la ETIQUETA, no en el valor: "Monto RD$: 18,345.00".
_MONTO = re.compile(r"Monto\s*(RD\$?|US\$?|DOP|USD)?\s*:\s*([\d.,]*\d)", re.I)
_NETO = re.compile(r"Neto pagado:\s*([\d.,]*\d)", re.I)
_INTERES = re.compile(r"Monto inter[eé]s:\s*([\d.,]*\d)", re.I)
_MONEDA_SUELTA = re.compile(r"Moneda:\s*(RD|US|DOP|USD)\b", re.I)
# APAP mete espacios (&nbsp;) DENTRO de la fecha: "16/04/ 2026". Se toleran
# acá y se limpian antes de normalizar; el contrato no acepta espacios.
# NUNCA capturar "Fecha de vencimiento": el pago de intereses no trae fecha de
# pago, solo la de vencimiento del certificado. Capturarla hacía que los pagos
# de TODOS los meses salieran con la misma fecha (la del vencimiento) y, con el
# mismo monto y la misma contraparte, produjeran la MISMA huella de dedupe: tres
# acreditaciones reales colapsaban en una y RD$94,725.00 se descartaban en
# silencio como "ya visto".
_FECHA = re.compile(r"Fecha(?!\s+de\s+vencimiento)(?:\s+de\s+\w+)?:\s*"
                    r"(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})", re.I)
_HORA = re.compile(r"Hora:\s*(\d{1,2}):(\d{1,2})", re.I)
_REF = re.compile(r"N[uú]mero de referencia:\s*([A-Z0-9]+)", re.I)
# Las etiquetas de APAP son de DOS palabras ("Cuenta destino:", "Número de
# referencia:"), así que el corte admite una palabra intermedia. Sin ella el
# beneficiario se llevaba "Cuenta destino: ****9639" dentro del nombre.
_BENEF = re.compile(r"Beneficiario:\s*(.+?)\s*"
                    r"(?=(?:N[uú]mero|Cuenta|Tipo|Entidad|Concepto|Monto|Fecha|"
                    r"Hora)(?:\s+\w+)*\s*:|$)", re.I)
_CUENTA_DEST = re.compile(r"Cuenta destino:\s*([\w\-*]+)", re.I)
_TARJETA = re.compile(r"terminad[ao]s? en\s*(\d{4})", re.I)

# Asunto → (canal, tipo). Lo que no está acá NO es un movimiento y no se
# registra: afiliar un beneficiario o abrir un producto no mueve dinero.
_MOVIMIENTOS = {
    "transferencia ach":                          ("transferencia", "gasto"),
    "transferencia ach trx unica":                ("transferencia", "gasto"),
    "pago al instante bcrd saliente":             ("transferencia", "gasto"),
    "transferencia apapenlinea":                  ("transferencia", "gasto"),
    "transferencia unica a tercero":              ("transferencia", "gasto"),
    "transferencia lbtr unica a otro banco":      ("transferencia", "gasto"),
    "transferencia pago al instante bcrd entrante completada":
                                                  ("transferencia", "ingreso"),
    "deposito en efectivo":                       ("transferencia", "ingreso"),
    "deposito por cancelacion de certificado":    ("transferencia", "ingreso"),
    "deposito de intereses":                      ("interes", "ingreso"),
    # Entre cuentas propias de APAP: ni gasto ni ingreso. Es el caso que evita
    # contar dos veces la misma plata.
    "ib-transferencia entre cuentas apap":        ("transferencia", "transferencia"),
    # Salió rechazada: se registra para poder auditarla, con estado declinada,
    # así no suma a los gastos.
    "transferencia ach no procesada":             ("transferencia", "gasto"),
}


def _sin_acentos(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", v.lower())
                   if not unicodedata.combining(c))


def _texto(correo: CorreoCrudo) -> str:
    crudo = correo.html or ""
    if crudo.strip():
        crudo = _TAGS.sub(" ", _SCRIPTS.sub(" ", crudo))
    else:
        crudo = correo.texto or ""
    return " ".join(crudo.replace("&nbsp;", " ").replace("\xa0", " ").split())


def _fecha_con_hora(texto: str, correo: CorreoCrudo | None = None):
    """Fecha + hora, tolerando que APAP mande la hora corrupta.

    En la muestra hay "Hora: 92:20" y "Hora: 70:90": no son horas. No es un
    fallo de parseo nuestro, es lo que llega. Tirar el movimiento entero por una
    hora inválida sería perder un dato bueno por un campo accesorio, así que se
    conserva la fecha y se descarta la hora.
    """
    mf = _FECHA.search(texto)
    if not mf:
        # El pago de intereses no trae fecha de pago. La del correo es la única
        # disponible y es fiable: el aviso llega el día de la acreditación.
        if correo is not None:
            return correo.fecha_correo
        raise ErrorDeParseo("correo de APAP sin 'Fecha:'")
    fecha = normalizar_fecha(re.sub(r"\s+", "", mf.group(1)))
    mh = _HORA.search(texto)
    if mh:
        hh, mi = int(mh.group(1)), int(mh.group(2))
        if hh < 24 and mi < 60:
            fecha = fecha + timedelta(hours=hh, minutes=mi)
    return fecha


def _monto_y_moneda(texto: str, es_interes: bool):
    if es_interes:
        # TRES montos: "Monto interés: 9,000.00", "Impuesto retenido: 900.00",
        # "Neto pagado: 8,100.00". Lo que entra a la cuenta es el NETO; el bruto
        # registraría un ingreso que nunca llegó completo.
        mn = _NETO.search(texto) or _INTERES.search(texto)
        if not mn:
            raise ErrorDeParseo("pago de intereses de APAP sin 'Neto pagado:'")
        mm = _MONEDA_SUELTA.search(texto)
        return normalizar_monto(mn.group(1)), normalizar_moneda(
            mm.group(1) if mm else "RD")
    m = _MONTO.search(texto)
    if not m:
        raise ErrorDeParseo("correo de APAP sin 'Monto:'")
    return normalizar_monto(m.group(2)), normalizar_moneda(m.group(1) or "RD")


def parsear(correo: CorreoCrudo) -> list[Movimiento]:
    texto = _texto(correo)
    if not texto:
        raise ErrorDeParseo("correo de APAP sin cuerpo legible")

    clave = " ".join(_sin_acentos(correo.asunto or "").split())
    if clave not in _MOVIMIENTOS:
        raise ErrorDeParseo(
            f"asunto de APAP que no está en _MOVIMIENTOS: {correo.asunto!r}. "
            "Si mueve dinero, agrégalo; si no, va a ASUNTOS_IGNORADOS.")
    canal, tipo = _MOVIMIENTOS[clave]
    es_interes = canal == "interes"

    monto, moneda = _monto_y_moneda(texto, es_interes)
    fecha = _fecha_con_hora(texto, correo)

    if es_interes:
        mt = _TARJETA.search(texto)
        contraparte = ("APAP · intereses del certificado "
                       + (f"••{mt.group(1)}" if mt else "(sin número)"))
    else:
        mb = _BENEF.search(texto)
        contraparte = " ".join(mb.group(1).split()) if mb else ""
        if not contraparte:
            md = _CUENTA_DEST.search(texto)
            contraparte = (f"cuenta {md.group(1)}" if md
                           else f"APAP · {correo.asunto}")

    partes = ["APAP"]
    mt = _TARJETA.search(texto)
    if mt:
        partes.append(f"••{mt.group(1)}")
    mr = _REF.search(texto)
    if mr:
        partes.append(f"ref {mr.group(1)}")
    partes.append(f"{fecha:%H:%M}")
    partes.append(correo.cuenta)

    return [Movimiento(
        banco=BANCO, canal=canal, tipo=tipo, fecha=fecha, monto=monto,
        moneda=moneda, contraparte=contraparte,
        estado="declinada" if "no procesada" in clave else "aprobada",
        referencia=" · ".join(partes))]


_ACENTOS = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uú]",
            "n": "[nñ]"}


def _patron(clave_sin_acentos: str) -> str:
    """Clave normalizada → regex que casa el asunto REAL, con acentos y todo.

    Las claves de _MOVIMIENTOS están sin acentos porque así se comparan dentro
    de parsear(), pero `buscar_parser` matchea contra el asunto tal como lo
    manda APAP ("Depósito de intereses"). Sin esta traducción el registro no
    casaría nunca y los 55 movimientos se descartarían en silencio — el mismo
    modo de fallo que t-04c.
    """
    salida = []
    for c in clave_sin_acentos:
        if c == " ":
            salida.append(r"\s+")
        elif c in _ACENTOS:
            salida.append(_ACENTOS[c])
        else:
            salida.append(re.escape(c))
    return "".join(salida)


for _clave in _MOVIMIENTOS:
    registrar(REMITENTE, parsear, asunto=_patron(_clave))
