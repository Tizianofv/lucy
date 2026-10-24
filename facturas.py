"""La hoja de una factura y su PDF (pieza 3 del diseño «Facturas», 21-oct-2026; estilo del modelo de Tiziano, 23-oct-2026).

Es hermana de `cotizaciones.py` y copia su camino: UNA SOLA FUENTE para la vista previa y el PDF.
`hoja_de_factura(f)` arma, sin tocar nada de afuera, un diccionario con TODOS los textos ya escritos; la macro
`web/plantillas/_hoja_de_factura.html` y `pdf_de_factura` solo lo DIBUJAN. El pie del estudio, la marca en diagonal y la
herramienta de PDF (`fpdf2`, Helvetica de fábrica) son LAS MISMAS funciones que usa la cotización (`cotizaciones._pie_de_la_hoja`,
`_marcar_de_borrador`, `dinero_en_pesos`, `_altura`). La primera página de la factura NO es la de la cotización: el modelo de
Tiziano no lleva logo, sí lleva arriba el nombre y el RNC de quien factura (`_abrir_la_hoja_de_factura`, acá). La cotización
sigue con su `_abrir_la_hoja` y no cambia.

Quién factura (nombre y RNC) NO está escrito en el código: sale de las variables `FACTURA_EMISOR_NOMBRE` y
`FACTURA_EMISOR_RNC` (`datos_del_emisor`). Si no están, la hoja no escribe esas dos líneas (solo queda la raya roja arriba).

Los montos NO se calculan acá: `f` es lo que devuelve `db.factura_por_id`, que trae `subtotal`, `itbis` y
`total` ya hechos por `db.totales_de_factura` (el único sitio de esa cuenta). La hoja solo los escribe.

Qué dice cada estado:
  · emitida: «FACTURA DE SERVICIOS» y su número `FAC-AAAA-NNN`, sin marca;
  · borrador: el mismo título y en el lugar del número la palabra «BORRADOR» (un borrador no tiene número, y aunque el
    diccionario trajera uno no se escribe), con la marca «BORRADOR» en diagonal en cada página;
  · anulada: su número, además «ESTADO: ANULADA» y «MOTIVO DE LA ANULACIÓN», y la marca «ANULADA» en cada página;
  · cualquier otro estado, que no se conoce, sale como borrador (lado estricto: nunca aparenta ser válida).
El NCF sale en rojo debajo del número (`ncf`, con su texto «NCF: …») solo si la factura lo lleva; su tipo va en el bloque
Para. El RNC o cédula del cliente, solo si lo lleva. El subtotal y el ITBIS, solo si lleva ITBIS, y el subtotal también si
lleva descuento. El descuento va en su línea debajo del subtotal («Descuento», «- RD$X») y solo si hay descuento.

Con comprobante (NCF) —regla de Tiziano, pieza D4—, debajo del total van tres líneas: la retención del ISR, la retención
del ITBIS y «Neto a pagar» (el bruto que de verdad paga el cliente). Las retenciones salen en negativo (`- RD$X`). Las
cifras las calcula `db.totales_de_factura`; sin NCF no aparece ninguna.

La tabla lleva las cuatro columnas de la cotización con detalle: SERVICIO (con su descripción debajo), CANTIDAD con su unidad,
PRECIO UNITARIO y TOTAL. Cada renglón trae ya escritos `cantidad` («2 horas») y `precio_unitario`; un renglón de antes del
detalle —sin cantidad ni precio— se escribe «1» y su total como precio unitario.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF

import cotizaciones as cz

LOGO_DE_LA_HOJA = cz.LOGO_DE_LA_HOJA
TITULO_DE_LA_FACTURA = "FACTURA DE SERVICIOS"
SECCION_DE_LOS_SERVICIOS = "SERVICIOS"
CODIGO_DEL_BORRADOR = "BORRADOR"
MARCA_DE_ANULADA = "ANULADA"
ENCABEZADOS_DE_LA_FACTURA = ("SERVICIO", "CANTIDAD", "PRECIO UNITARIO", "TOTAL")
ETIQUETA_DEL_SUBTOTAL = "Subtotal"
ETIQUETA_DEL_DESCUENTO = "Descuento"
ETIQUETA_DEL_ITBIS = "ITBIS ({pct}%)"
ETIQUETA_DEL_TOTAL = "Total factura (RD$)"
# Las tres de una factura CON comprobante (NCF), pieza D4, con el texto del modelo de Tiziano.
ETIQUETA_DE_LA_RETENCION_DEL_ISR = "Retención ISR (15%) sobre el servicio"
ETIQUETA_DEL_ITBIS_RETENIDO = "Retención ITBIS ({pct}%)"
ETIQUETA_DEL_NETO_A_PAGAR = "Neto a pagar"
ETIQUETA_DEL_NCF = "NCF: {ncf}"
RNC_DEL_EMISOR = "RNC {rnc}"
# Estilo de cada línea del total (lo que la hoja y el PDF hacen con ella): «total» es la franja negra, «gris» es el ITBIS
# y las retenciones (cursiva gris), «neto» es el neto a pagar (negrita cursiva).
ESTILO_DEL_TOTAL = cz.ESTILO_DEL_TOTAL
ESTILO_DE_LA_LINEA = "gris"
ESTILO_DEL_NETO = cz.ESTILO_DEL_NETO
ETIQUETAS_DEL_PARA_DE_LA_FACTURA = {
    "para": "PARA", "rnc_cedula": "RNC", "fecha": "FECHA", "ncf_tipo": "TIPO DE NCF",
    "estado": "ESTADO", "motivo": "MOTIVO DE LA ANULACIÓN"}


def datos_del_emisor() -> tuple[str, str]:
    """`(nombre, rnc)` de quien factura, de las variables de entorno. Vacío si no están: la hoja no escribe esas líneas
    (no se escribe ningún nombre ni RNC a mano en el código)."""
    return (os.environ.get("FACTURA_EMISOR_NOMBRE", "").strip(),
            os.environ.get("FACTURA_EMISOR_RNC", "").strip())


def _porcentaje(pct) -> str:
    """`Decimal("18.00")` → «18»."""
    return str(int(pct)) if pct == pct.to_integral_value() else f"{pct.normalize():f}"


def cantidad_en_texto(cantidad, unidad=None) -> str:
    """`2`, `«horas»` → «2 horas»; `1.500`, sin unidad → «1.5». Sin ceros de cola (así lo escribe el modelo)."""
    texto = f"{Decimal(cantidad).normalize():f}"
    return f"{texto} {unidad}" if unidad else texto


def marca_de_la_factura(estado: str) -> str | None:
    """La palabra que se dibuja en diagonal: nada en una emitida, «ANULADA» en una anulada y «BORRADOR» en lo
    demás (también en un estado que no se conoce)."""
    if estado == "emitida":
        return None
    return MARCA_DE_ANULADA if estado == "anulada" else cz.MARCA_DE_BORRADOR


def _armar_la_hoja(*, estado: str, codigo, para: list, renglones: list, totales: list, ncf: str | None = None) -> dict:
    """Las piezas fijas de la hoja (título, número o BORRADOR, marca, emisor, sección y pie) con lo que le den ya
    escrito. La usan la hoja de una factura guardada (`hoja_de_factura`) y la de lo que se está escribiendo
    (`hoja_de_lo_escrito`), para que las dos digan lo mismo."""
    tiene_numero = estado in ("emitida", "anulada") and bool(codigo)
    nombre, rnc = datos_del_emisor()
    return {
        "emisor": (nombre, RNC_DEL_EMISOR.format(rnc=rnc) if rnc else ""),
        "titulo": TITULO_DE_LA_FACTURA,
        "codigo": codigo if tiene_numero else CODIGO_DEL_BORRADOR,
        "ncf": ETIQUETA_DEL_NCF.format(ncf=ncf) if ncf else None,
        "marca": marca_de_la_factura(estado),
        "para": para,
        "seccion": SECCION_DE_LOS_SERVICIOS,
        "encabezados": ENCABEZADOS_DE_LA_FACTURA,
        "renglones": renglones,
        "totales": totales,
        "pie": cz.PIE_DEL_ESTUDIO,
    }


def renglon_de_la_hoja(r: dict) -> dict:
    """Un renglón de la hoja, ya escrito: servicio, su descripción, la cantidad con su unidad, el precio de una
    unidad y su total. `r` es un renglón de los que devuelve `db.factura_por_id` o la puerta al validar (con
    `cantidad`, `unidad`, `precio_unitario` y `monto`)."""
    # Un renglón de antes del detalle (pieza D2) no trae cantidad ni precio unitario: se lee 1 × su monto, sin
    # unidad, igual que lo lee la puerta (`db._factura_completa`).
    viejo = r.get("cantidad") is None or r.get("precio_unitario") is None
    return {
        "descripcion": r["descripcion"], "detalle": r.get("detalle") or "",
        "cantidad": cantidad_en_texto(1 if viejo else r["cantidad"], None if viejo else r.get("unidad")),
        "precio_unitario": cz.dinero_en_pesos(r["monto"] if viejo else r["precio_unitario"]),
        "monto": cz.dinero_en_pesos(r["monto"])}


def _linea(etiqueta: str, total: str, estilo: str) -> dict:
    return {"etiqueta": etiqueta, "total": total, "estilo": estilo}


def totales_de_la_hoja(f: dict) -> list[dict]:
    """Las líneas del total de la hoja, en su orden: subtotal y descuento si los hay, el ITBIS si lleva, el TOTAL
    («Total factura (RD$)», franja negra) y —solo con comprobante (NCF)— la retención del ISR, la retención del ITBIS
    (en negativo) y el neto a pagar. Las cifras llegan calculadas (`db.totales_de_factura`); esto solo las escribe. La
    usan la hoja de una factura guardada y la de lo que se está escribiendo, para que las dos digan lo mismo."""
    totales = []
    descuento = Decimal(f.get("descuento") or 0)
    pct = f.get("itbis_pct")
    if pct is not None or descuento > 0:
        totales.append(_linea(ETIQUETA_DEL_SUBTOTAL, cz.dinero_en_pesos(f["subtotal"]), ESTILO_DE_LA_LINEA))
    if descuento > 0:
        # El signo es el guion ASCII: la letra de fábrica del PDF (Helvetica) no tiene el menos tipográfico (U+2212) y
        # `fpdf2` revienta con él. Es la misma línea en la vista previa y en el PDF (una sola hoja para las dos).
        totales.append(_linea(ETIQUETA_DEL_DESCUENTO, "- " + cz.dinero_en_pesos(descuento), ESTILO_DE_LA_LINEA))
    if pct is not None:
        totales.append(_linea(ETIQUETA_DEL_ITBIS.format(pct=_porcentaje(pct)),
                              cz.dinero_en_pesos(f["itbis"]), ESTILO_DE_LA_LINEA))
    totales.append(_linea(ETIQUETA_DEL_TOTAL, cz.dinero_en_pesos(f["total"]), ESTILO_DEL_TOTAL))
    if f.get("ncf"):
        # Una factura CON comprobante: lo que el cliente retiene y lo que de verdad paga. Las cifras salen de
        # `db.totales_de_factura`; la hoja no las calcula. Una factura con NCF siempre lleva ITBIS (la puerta no la deja
        # guardar sin él, pieza D4b), así que la línea del ITBIS retenido siempre tiene su porcentaje.
        retenido = ETIQUETA_DEL_ITBIS_RETENIDO.format(pct=_porcentaje(pct)) if pct is not None else "Retención ITBIS"
        totales += [
            _linea(ETIQUETA_DE_LA_RETENCION_DEL_ISR, "- " + cz.dinero_en_pesos(f["retencion_isr"]), ESTILO_DE_LA_LINEA),
            _linea(retenido, "- " + cz.dinero_en_pesos(f["itbis_retenido"]), ESTILO_DE_LA_LINEA),
            _linea(ETIQUETA_DEL_NETO_A_PAGAR, cz.dinero_en_pesos(f["total_a_pagar"]), ESTILO_DEL_NETO)]
    return totales


def hoja_de_factura(f: dict) -> dict:
    """Los textos de la hoja de una factura. `f` es lo que devuelve `db.factura_por_id`: `estado`, `codigo`,
    `para_nombre`, `rnc_cedula`, `fecha`, `ncf`, `ncf_tipo`, `itbis_pct`, `descuento`, `motivo_anulacion`,
    `renglones` (con `descripcion`, `detalle`, `monto`) y los totales (`subtotal`/`itbis`/`total` y, con NCF,
    `retencion_isr`/`itbis_retenido`/`total_a_pagar`). Función pura (salvo leer las variables de quien factura)."""
    etiquetas = ETIQUETAS_DEL_PARA_DE_LA_FACTURA
    estado = f["estado"]
    para = [(etiquetas["para"], f["para_nombre"])]
    if f.get("rnc_cedula"):
        para.append((etiquetas["rnc_cedula"], f["rnc_cedula"]))
    para.append((etiquetas["fecha"], cz.fecha_en_palabras(f["fecha"])))
    if f.get("ncf") and f.get("ncf_tipo"):
        para.append((etiquetas["ncf_tipo"], f["ncf_tipo"]))
    if estado == "anulada":
        para.append((etiquetas["estado"], MARCA_DE_ANULADA))
        para.append((etiquetas["motivo"], f["motivo_anulacion"]))
    renglones = [renglon_de_la_hoja(r) for r in f["renglones"]]
    return _armar_la_hoja(estado=estado, codigo=f.get("codigo"), para=para, renglones=renglones,
                          totales=totales_de_la_hoja(f), ncf=f.get("ncf") or None)


def hoja_de_lo_escrito(*, para: list, renglones: list, totales: list, ncf: str | None = None) -> dict:
    """La hoja de lo que se está escribiendo en el formulario (pieza 4b): SIEMPRE un borrador —«BORRADOR» en el
    lugar del número y su marca—, con lo que ya vale de lo escrito. `para`, `renglones`, `totales` y `ncf` llegan ya
    escritos; esto solo les pone las piezas fijas, las MISMAS de la hoja de una factura guardada. Función pura:
    no calcula dinero y no guarda nada."""
    return _armar_la_hoja(estado="borrador", codigo=None, para=para, renglones=renglones, totales=totales, ncf=ncf)


def textos_de_la_factura(hoja: dict) -> list[str]:
    """Todos los textos de la hoja, en el orden en que se dibujan (los vacíos no se dibujan). La marca en diagonal no
    es texto de la hoja: se comprueba aparte."""
    t = [x for x in hoja["emisor"] if x]
    t += [hoja["titulo"], hoja["codigo"]]
    if hoja["ncf"]:
        t.append(hoja["ncf"])
    for etiqueta, valor in hoja["para"]:
        t += [etiqueta, valor]
    t.append(hoja["seccion"])
    t += list(hoja["encabezados"])
    for r in hoja["renglones"]:
        t.append(r["descripcion"])
        if r["detalle"]:
            t.append(r["detalle"])
        t += [r["cantidad"], r["precio_unitario"], r["monto"]]
    for x in hoja["totales"]:
        t += [x["etiqueta"], x["total"]]
    t += list(hoja["pie"])
    return t


def nombre_del_archivo(f: dict) -> str:
    """`FAC-2026-001.pdf`; un borrador no tiene número y se baja como `Factura-borrador-<id>.pdf`."""
    if f["estado"] in ("emitida", "anulada") and f.get("codigo"):
        return f"{f['codigo']}.pdf"
    return f"Factura-borrador-{int(f['id'])}.pdf"


# ── La primera página de la factura (modelo de Tiziano: sin logo, nombre y RNC arriba) ───────────────
_MARGEN_SUPERIOR = 18.0


def _abrir_la_hoja_de_factura(hoja: dict) -> tuple[FPDF, float]:
    """La primera página de una factura: el nombre y el RNC de quien factura (si están), raya roja, el título
    «FACTURA DE SERVICIOS» con el código, el NCF en rojo (si lo lleva), el bloque Para en DOS columnas y el título
    «SERVICIOS». Devuelve el PDF y el margen izquierdo."""
    pdf = FPDF(unit="mm", format="Letter")
    pdf.set_margins(cz._MARGEN, _MARGEN_SUPERIOR, cz._MARGEN)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(hoja["codigo"])
    pdf.set_creator("Lucy")
    pdf.add_page()
    x = pdf.l_margin
    nombre, rnc = hoja["emisor"]
    pdf.set_y(_MARGEN_SUPERIOR)
    if nombre:
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(*cz._NEGRO)
        pdf.multi_cell(pdf.epw, 7, nombre, new_x="LMARGIN", new_y="NEXT")
    if rnc:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*cz._GRIS_TEXTO)
        pdf.cell(0, 5, rnc, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*cz.ROJO)
    pdf.set_line_width(0.7)
    y = pdf.get_y() + 2
    pdf.line(x, y, x + pdf.epw, y)
    pdf.set_y(y + 7)

    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*cz.ROJO)
    pdf.cell(0, 9, hoja["titulo"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(119, 119, 119)
    pdf.cell(0, 5, hoja["codigo"], align="C", new_x="LMARGIN", new_y="NEXT")
    if hoja["ncf"]:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*cz.ROJO)
        pdf.cell(0, 5, hoja["ncf"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # El bloque Para en dos columnas (PARA | RNC, FECHA | …): fondo gris y raya roja. Cada valor puede ocupar varias
    # líneas: se mide antes de dibujar el fondo, y el bloque mide lo que mide la columna más alta.
    # Dos columnas por FILA, en el mismo orden que la hoja en pantalla (PARA | RNC, FECHA | …): así el texto del PDF
    # se lee igual que la vista previa. 5 mm de sangría, 8 mm entre columnas y 2 mm de holgura a la derecha.
    ancho_col = (pdf.epw - 15) / 2
    filas = [hoja["para"][i:i + 2] for i in range(0, len(hoja["para"]), 2)]
    pdf.set_font("Helvetica", "B", 10)
    alturas_de_fila = [max(3.2 + cz._altura(pdf, ancho_col, 5.2, valor) + 0.8 for _, valor in fila) for fila in filas]
    y0 = pdf.get_y()
    alto = 4 + sum(alturas_de_fila)
    pdf.set_fill_color(244, 244, 244)
    pdf.rect(x, y0, pdf.epw, alto, style="F")
    pdf.set_fill_color(*cz.ROJO)
    pdf.rect(x, y0, 1, alto, style="F")
    y = y0 + 2
    for fila, alto_de_la_fila in zip(filas, alturas_de_fila):
        for n, (etiqueta, valor) in enumerate(fila):
            xx = x + 5 + n * (ancho_col + 8)
            pdf.set_xy(xx, y)
            pdf.set_font("Helvetica", "", 6.5)
            pdf.set_text_color(136, 136, 136)
            pdf.cell(ancho_col, 3.2, etiqueta)
            pdf.set_xy(xx, y + 3.2)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*cz._NEGRO)
            pdf.multi_cell(ancho_col, 5.2, valor, new_x="LMARGIN", new_y="NEXT")
        y += alto_de_la_fila
    pdf.set_y(y0 + alto + 7)

    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*cz.ROJO)
    pdf.set_x(x)
    pdf.cell(0, 4, hoja["seccion"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(230, 230, 230)
    pdf.set_line_width(0.2)
    y2 = pdf.get_y() + 1
    pdf.line(x, y2, x + pdf.epw, y2)
    pdf.set_y(y2 + 3)
    return pdf, x


def pdf_de_factura(hoja: dict, logo: Path = LOGO_DE_LA_HOJA) -> bytes:
    """El PDF de una hoja (`hoja_de_factura`), en el estilo del modelo de Tiziano: nombre y RNC de quien factura, raya
    roja, «FACTURA DE SERVICIOS», bloque Para en dos columnas, «SERVICIOS», una tabla de CUATRO columnas, los totales
    (la franja negra del total, las retenciones en negativo y el neto a pagar) y el pie del estudio. Salta de página
    cuando no cabe y repite la cabecera. Dibuja SOLO lo que trae `hoja`; si trae `marca`, la dibuja en diagonal en cada
    página. `logo` se conserva por compatibilidad con quien llama y NO se dibuja: el modelo no lleva logo."""
    del logo  # la factura no lleva logo (modelo de Tiziano, 23-oct-2026)
    pdf, x = _abrir_la_hoja_de_factura(hoja)
    anchos = cz._anchos_de_las_columnas(pdf, hoja)
    cz._cabecera_de_las_columnas(pdf, hoja, anchos)
    for r in hoja["renglones"]:
        cz._fila_de_las_columnas(pdf, hoja, anchos, r)
    for t in hoja["totales"]:
        cz._fila_de_un_total(pdf, hoja, anchos, t)
    cz._pie_de_la_hoja(pdf, hoja, x)
    if hoja["marca"]:
        cz._marcar_de_borrador(pdf, hoja["marca"])
    return bytes(pdf.output())
