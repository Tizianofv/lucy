"""Parser de las alertas de BHD (alertas@bhd.com.do).

Cubre "BHD Notificación de Transacciones", que es el consumo con tarjeta y el
grueso del volumen: 161 de los 179 correos de ese remitente en un año.

El formato, verificado sobre los 161 fixtures:

    <thead>  Fecha | Moneda | Monto | Comercio | Estado | Tipo
    <tbody>  08/07/2026 11:14 am | US | $1.19 | APPLE.COM/BILL | Aprobada | Compra

La cabecera va en <thead>, fuera del <tbody>, y hoy el <tbody> trae siempre UNA
fila. Aun así esto itera <tr> en vez de aplanar las <td> del <tbody>: aplanar
da el mismo resultado mientras haya una sola transacción, y se queda callado
con la primera el día que lleguen dos. La diferencia entre los dos enfoques no
se ve hasta que el banco cambia algo, que es justo cuando importa.

Lo que NO parsea, y por qué:
  · "Código de validación de compra" — trae un monto, pero es un OTP de una
    compra que todavía no ocurrió. Lo frena ASUNTOS_IGNORADOS del contrato.
  · Transferencias y traspasos entre productos — otros asuntos del mismo
    remitente, con formato propio. Van aparte (t-06).
"""
from __future__ import annotations

import re

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

BANCO = "bhd"
REMITENTE = "alertas@bhd.com.do"
ASUNTO_CONSUMO = r"Notificaci[oó]n de Transacciones"

_TBODY = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.S | re.I)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")

# El "Tipo" que manda BHD en la última celda, mapeado a nuestro vocabulario:
#   clave normalizada → (canal, tipo, estado_forzado)
# Un tipo desconocido revienta en vez de caer en 'gasto': si BHD empieza a
# mandar algo nuevo quiero enterarme, no promediarlo. Ya pasó — "Goods services
# with cash back" y "Reserva de Fondos (Hold)" aparecieron en la muestra real y
# no estaban en mi primera versión de esta tabla.
#
# El estado_forzado existe por las RETENCIONES. Una "Reserva de Fondos (Hold)"
# llega con Estado "Aprobada", y es verdad que fue aprobada — pero es el
# pre-autorizado de un hotel o una bomba, no un cargo liquidado: puede
# liberarse, o liquidar por otro monto. Contarla como gasto infla el total, y
# cuando después entra el cargo de verdad se cuenta dos veces. Por eso entra
# como 'pendiente' y los totales de gasto filtran por 'aprobada'.
_TIPOS_BHD = {
    "compra":                        ("tarjeta", "gasto", None),
    "consumo":                       ("tarjeta", "gasto", None),
    "goods services with cash back": ("tarjeta", "gasto", None),
    "retiro":                        ("tarjeta", "gasto", None),
    "avance":                        ("tarjeta", "gasto", None),
    "pago":                          ("servicio", "gasto", None),
    "reserva de fondos (hold)":      ("tarjeta", "gasto", "pendiente"),
    "devolucion":                    ("tarjeta", "ingreso", None),
    "reverso":                       ("tarjeta", "ingreso", None),
}


def _celda(html: str) -> str:
    """Contenido de una <td> como texto plano."""
    return _TAGS.sub("", html).replace("&nbsp;", " ").replace("\xa0", " ").strip()


def _sin_acentos_min(v: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", v.lower())
                   if not unicodedata.combining(c))


def parsear_consumo(correo: CorreoCrudo) -> list[Movimiento]:
    """Las filas de la tabla de transacciones → Movimiento, una por <tr>."""
    m = _TBODY.search(correo.html or "")
    if not m:
        raise ErrorDeParseo(
            "no encontré <tbody> en el correo de BHD; ¿cambió la plantilla?")

    movimientos: list[Movimiento] = []
    for fila in _TR.findall(m.group(1)):
        celdas = [_celda(c) for c in _TD.findall(fila)]
        if len(celdas) < 6:
            continue  # separadores y filas de maquetación
        fecha_txt, moneda_txt, monto_txt, comercio, estado_txt, tipo_txt = celdas[:6]

        # Cinturón por si algún día la cabecera cae dentro del <tbody>: la
        # celda de moneda diría "Moneda" y normalizar_moneda reventaría. Mejor
        # saltarla en silencio que convertir un cambio de maquetación en un
        # fallo de parseo — pero solo esta fila exacta, nada más.
        if _sin_acentos_min(moneda_txt) == "moneda":
            continue

        clave = " ".join(_sin_acentos_min(tipo_txt).split())
        if clave not in _TIPOS_BHD:
            raise ErrorDeParseo(
                f"tipo de transacción desconocido en BHD: {tipo_txt!r}. "
                "Agregalo a _TIPOS_BHD si es legítimo — no lo asumas gasto.")
        canal, tipo, estado_forzado = _TIPOS_BHD[clave]

        estado = normalizar_estado(estado_txt)

        # UN REVERSO ES PLATA QUE VUELVE, no un gasto que se ignora.
        #
        # BHD notifica el cargo original y el reverso en correos separados, así
        # que el cargo YA entró como gasto aprobado. Si el reverso se guardara
        # como gasto/reversada y los totales filtraran por 'aprobada', el cargo
        # quedaría contado y la devolución no: el gasto sale inflado.
        #
        # Por eso el reverso se invierte —gasto pasa a ingreso— y queda
        # 'aprobada', porque el reverso sí ocurrió. Los dos movimientos se
        # netean solos sin tener que emparejarlos con el original, que es
        # trabajo que BHD no nos da forma de hacer (no manda un id común).
        #
        # SUPUESTO A CONFIRMAR: que BHD siempre notificó antes el cargo que
        # revierte. Si algún día llega un reverso de algo que nunca se notificó,
        # esto crea un ingreso fantasma.
        if estado == "reversada":
            tipo = "ingreso" if tipo == "gasto" else "gasto"
            estado = "aprobada"
            estado_forzado = None

        if not comercio.strip():
            # Los reversos llegan sin comercio: BHD referencia la transacción
            # original en vez de repetirlo. Es legítimo, y decirlo explícito es
            # mejor que inventar un nombre o que tirar el movimiento.
            if estado_txt and normalizar_estado(estado_txt) == "reversada":
                comercio = "(reverso · comercio no informado por BHD)"
            else:
                raise ErrorDeParseo(
                    "comercio vacío en una fila de BHD que no es un reverso")

        movimientos.append(Movimiento(
            banco=BANCO,
            canal=canal,
            tipo=tipo,
            fecha=normalizar_fecha(fecha_txt),
            monto=normalizar_monto(monto_txt),
            moneda=normalizar_moneda(moneda_txt),
            contraparte=" ".join(comercio.split()),
            # Una retención se fuerza a 'pendiente' aunque el banco la reporte
            # aprobada: aprobada lo está, liquidada no. Ver _TIPOS_BHD. Si el
            # estado ya era declinada, eso gana — una retención denegada no
            # está pendiente de nada. Los reversos ya se resolvieron arriba.
            estado=(estado_forzado if estado_forzado and estado == "aprobada"
                    else estado),
            # La hora exacta viaja acá porque `movimientos.fecha` es DATE y la
            # pierde: sin ella, dos compras iguales del mismo día no se pueden
            # distinguir al deduplicar.
            referencia=f"BHD {normalizar_fecha(fecha_txt):%H:%M} · {correo.cuenta}",
        ))

    if not movimientos:
        raise ErrorDeParseo(
            "el <tbody> de BHD no tenía ninguna fila con 6 celdas útiles")
    return movimientos


registrar(REMITENTE, parsear_consumo, asunto=ASUNTO_CONSUMO)
