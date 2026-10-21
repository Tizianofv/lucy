"""La hoja de una factura y su PDF (pieza 3 del diseño «Facturas», 21-oct-2026).

Es hermana de `cotizaciones.py` y copia su camino: UNA SOLA FUENTE para la vista previa y el PDF.
`hoja_de_factura(f)` arma, sin tocar nada de afuera, un diccionario con TODOS los textos ya escritos; la macro
`web/plantillas/_hoja_de_factura.html` y `pdf_de_factura` solo lo DIBUJAN. La herramienta de PDF (`fpdf2`,
Helvetica de fábrica), la primera página (logo, raya, título, bloque Para), el pie del estudio y la marca en
diagonal son LAS MISMAS funciones que usa la cotización (`cotizaciones._abrir_la_hoja`, `_pie_de_la_hoja`,
`_marcar_de_borrador`). La tabla NO es la misma desde la pieza D2: la cotización sigue con sus dos columnas
(`_cabecera_de_la_tabla`/`_fila`) y la factura tiene la suya de cuatro, acá abajo.

Los montos NO se calculan acá: `f` es lo que devuelve `db.factura_por_id`, que trae `subtotal`, `itbis` y
`total` ya hechos por `db.totales_de_factura` (el único sitio de esa cuenta). La hoja solo los escribe.

Qué dice cada estado:
  · emitida: «FACTURA» y su número `FAC-AAAA-NNN`, sin marca;
  · borrador: «FACTURA» y en el lugar del número la palabra «BORRADOR» (un borrador no tiene número, y aunque el
    diccionario trajera uno no se escribe), con la marca «BORRADOR» en diagonal en cada página;
  · anulada: su número, además «ESTADO: ANULADA» y «MOTIVO DE LA ANULACIÓN», y la marca «ANULADA» en cada página;
  · cualquier otro estado, que no se conoce, sale como borrador (lado estricto: nunca aparenta ser válida).
El NCF y su tipo salen solo si la factura los lleva; el RNC o cédula, solo si lo lleva; el ITBIS y el subtotal,
solo si lleva ITBIS (sin ITBIS la hoja dice solo el total).

Desde la pieza D2 (21-oct-2026, Tiziano: «Si, las facturas deben salir con los detalles.») la tabla lleva las cuatro
columnas de la cotización con detalle: SERVICIO (con su descripción debajo), CANTIDAD con su unidad, PRECIO
UNITARIO y TOTAL. Cada renglón trae ya escritos `cantidad` («2 horas») y `precio_unitario`; un renglón de antes del
detalle —sin cantidad ni precio— se escribe «1» y su total como precio unitario.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import cotizaciones as cz

LOGO_DE_LA_HOJA = cz.LOGO_DE_LA_HOJA
TITULO_DE_LA_FACTURA = "FACTURA"
CODIGO_DEL_BORRADOR = "BORRADOR"
MARCA_DE_ANULADA = "ANULADA"
ENCABEZADOS_DE_LA_FACTURA = ("SERVICIO", "CANTIDAD", "PRECIO UNITARIO", "TOTAL")
ETIQUETA_DEL_SUBTOTAL = "Subtotal"
ETIQUETA_DEL_ITBIS = "ITBIS {pct}%"
ETIQUETA_DEL_TOTAL = "TOTAL"
ETIQUETAS_DEL_PARA_DE_LA_FACTURA = {
    "para": "PARA", "rnc_cedula": "RNC / CÉDULA", "fecha": "FECHA", "ncf": "NCF", "ncf_tipo": "TIPO DE NCF",
    "estado": "ESTADO", "motivo": "MOTIVO DE LA ANULACIÓN"}


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


def hoja_de_factura(f: dict) -> dict:
    """Los textos de la hoja de una factura. `f` es lo que devuelve `db.factura_por_id`: `estado`, `codigo`,
    `para_nombre`, `rnc_cedula`, `fecha`, `ncf`, `ncf_tipo`, `itbis_pct`, `motivo_anulacion`, `renglones` (con
    `descripcion`, `detalle`, `monto`) y `subtotal`/`itbis`/`total`. Función pura."""
    etiquetas = ETIQUETAS_DEL_PARA_DE_LA_FACTURA
    estado = f["estado"]
    tiene_numero = estado in ("emitida", "anulada") and bool(f.get("codigo"))
    para = [(etiquetas["para"], f["para_nombre"])]
    if f.get("rnc_cedula"):
        para.append((etiquetas["rnc_cedula"], f["rnc_cedula"]))
    para.append((etiquetas["fecha"], cz.fecha_en_palabras(f["fecha"])))
    if f.get("ncf"):
        para.append((etiquetas["ncf"], f["ncf"]))
        if f.get("ncf_tipo"):
            para.append((etiquetas["ncf_tipo"], f["ncf_tipo"]))
    if estado == "anulada":
        para.append((etiquetas["estado"], MARCA_DE_ANULADA))
        para.append((etiquetas["motivo"], f["motivo_anulacion"]))
    renglones = []
    for r in f["renglones"]:
        # Un renglón de antes del detalle (pieza D2) no trae cantidad ni precio unitario: se lee 1 × su monto, sin
        # unidad, igual que lo lee la puerta (`db._factura_completa`).
        viejo = r.get("cantidad") is None or r.get("precio_unitario") is None
        renglones.append({
            "descripcion": r["descripcion"], "detalle": r.get("detalle") or "",
            "cantidad": cantidad_en_texto(1 if viejo else r["cantidad"], None if viejo else r.get("unidad")),
            "precio_unitario": cz.dinero_en_pesos(r["monto"] if viejo else r["precio_unitario"]),
            "monto": cz.dinero_en_pesos(r["monto"])})
    totales = []
    if f.get("itbis_pct") is not None:
        totales.append({"etiqueta": ETIQUETA_DEL_SUBTOTAL, "total": cz.dinero_en_pesos(f["subtotal"])})
        totales.append({"etiqueta": ETIQUETA_DEL_ITBIS.format(pct=_porcentaje(f["itbis_pct"])),
                        "total": cz.dinero_en_pesos(f["itbis"])})
    totales.append({"etiqueta": ETIQUETA_DEL_TOTAL, "total": cz.dinero_en_pesos(f["total"])})
    return {
        "titulo": TITULO_DE_LA_FACTURA,
        "codigo": f["codigo"] if tiene_numero else CODIGO_DEL_BORRADOR,
        "marca": marca_de_la_factura(estado),
        "para": para,
        "encabezados": ENCABEZADOS_DE_LA_FACTURA,
        "renglones": renglones,
        "totales": totales,
        "pie": cz.PIE_DEL_ESTUDIO,
    }


def textos_de_la_factura(hoja: dict) -> list[str]:
    """Todos los textos de la hoja, en el orden en que se dibujan (el detalle vacío no se dibuja). La marca en
    diagonal no es texto de la hoja: se comprueba aparte."""
    t = [hoja["titulo"], hoja["codigo"]]
    for etiqueta, valor in hoja["para"]:
        t += [etiqueta, valor]
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


# ── La tabla de cuatro columnas del PDF (pieza D2) ──────────────────────────────────────────────────
#
# La cotización sigue con su tabla de dos columnas (`cotizaciones._cabecera_de_la_tabla` / `_fila`): esta es de la
# factura y no se toca aquélla. Los anchos salen de MEDIR lo que se va a dibujar (una cifra larga no se sale), y la
# columna del servicio se queda con lo que sobra. La cantidad es la única que puede ser larga de más («99999999.99
# unidades»): se le pone tope y, si no cabe, se parte en dos líneas —nunca se recorta.
_ANCHO_DE_LA_CANTIDAD = 26.0
_ANCHO_DEL_PRECIO = 32.0
_TOPE_DE_LA_CANTIDAD = 45.0
_ALTO_DE_LA_CABECERA = 9.0
_LINEA_DE_LA_TABLA = (230, 230, 230)


def _anchos_de_las_columnas(pdf, hoja: dict) -> tuple[float, float, float, float]:
    """`(servicio, cantidad, precio unitario, total)`: los tres de la derecha crecen con lo que se va a escribir; el
    servicio se queda con el resto del ancho de la carta."""
    pdf.set_font("Helvetica", "B", 10)
    ancho_precio = max([_ANCHO_DEL_PRECIO] + [pdf.get_string_width(r["precio_unitario"]) + 6 for r in hoja["renglones"]])
    montos = [r["monto"] for r in hoja["renglones"]] + [t["total"] for t in hoja["totales"]]
    ancho_total = max([cz._ANCHO_DEL_TOTAL] + [pdf.get_string_width(m) + 6 for m in montos])
    ancho_cantidad = min(
        max([_ANCHO_DE_LA_CANTIDAD] + [pdf.get_string_width(r["cantidad"]) + 8 for r in hoja["renglones"]]),
        _TOPE_DE_LA_CANTIDAD)
    return pdf.epw - ancho_cantidad - ancho_precio - ancho_total, ancho_cantidad, ancho_precio, ancho_total


def _cabecera_de_las_columnas(pdf, hoja: dict, anchos) -> None:
    """La franja roja con los cuatro encabezados. El del precio unitario va en dos líneas, como el modelo; las
    palabras salen del encabezado que trae la hoja, no se escriben acá."""
    x, y = pdf.l_margin, pdf.get_y()
    a_serv, a_cant, a_precio, a_total = anchos
    pdf.set_fill_color(*cz.ROJO)
    pdf.rect(x, y, pdf.epw, _ALTO_DE_LA_CABECERA, style="F")
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(x + 2, y)
    pdf.cell(a_serv - 2, _ALTO_DE_LA_CABECERA, hoja["encabezados"][0])
    pdf.set_xy(x + a_serv, y)
    pdf.cell(a_cant - 2, _ALTO_DE_LA_CABECERA, hoja["encabezados"][1], align="R")
    partes = hoja["encabezados"][2].split()
    pdf.set_xy(x + a_serv + a_cant, y)
    if len(partes) == 2:
        pdf.cell(a_precio - 2, 4.5, partes[0], align="R", new_x="LEFT", new_y="NEXT")
        pdf.set_xy(x + a_serv + a_cant, y + 4.5)
        pdf.cell(a_precio - 2, 4.5, partes[1], align="R", new_x="LEFT", new_y="NEXT")
    else:
        pdf.cell(a_precio - 2, _ALTO_DE_LA_CABECERA, hoja["encabezados"][2], align="R")
    pdf.set_xy(x + a_serv + a_cant + a_precio, y)
    pdf.cell(a_total - 2, _ALTO_DE_LA_CABECERA, hoja["encabezados"][3], align="R")
    pdf.set_y(y + _ALTO_DE_LA_CABECERA)


def _fila_de_las_columnas(pdf, hoja: dict, anchos, r: dict) -> None:
    """Un renglón: servicio (negrita) con su descripción debajo, cantidad con su unidad, precio unitario y total. Si
    no cabe en lo que queda de la página, salta de página y repite la cabecera."""
    a_serv, a_cant, a_precio, a_total = anchos
    x = pdf.l_margin
    ancho_texto = a_serv - 4
    pdf.set_font("Helvetica", "B", 10)
    h_desc = cz._altura(pdf, ancho_texto, 5, r["descripcion"])
    h_det = 0.0
    if r["detalle"]:
        pdf.set_font("Helvetica", "", 8.5)
        h_det = cz._altura(pdf, ancho_texto, 4.2, r["detalle"])
    h_cant = cz._altura(pdf, a_cant - 4, 5, r["cantidad"])
    alto = max(h_desc + h_det, h_cant, 5) + 4
    if pdf.will_page_break(alto):
        pdf.add_page()
        _cabecera_de_las_columnas(pdf, hoja, anchos)
    y = pdf.get_y()
    pdf.set_xy(x + 2, y + 2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*cz._NEGRO)
    pdf.multi_cell(ancho_texto, 5, r["descripcion"], new_x="LEFT", new_y="NEXT")
    if r["detalle"]:
        pdf.set_x(x + 2)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*cz._GRIS_TEXTO)
        pdf.multi_cell(ancho_texto, 4.2, r["detalle"], new_x="LEFT", new_y="NEXT")
    pdf.set_xy(x + a_serv, y + 2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*cz._NEGRO)
    pdf.multi_cell(a_cant - 4, 5, r["cantidad"], align="R", new_x="LEFT", new_y="NEXT")
    pdf.set_xy(x + a_serv + a_cant, y + 2)
    pdf.cell(a_precio - 2, 5, r["precio_unitario"], align="R")
    pdf.set_xy(x + a_serv + a_cant + a_precio, y + 2)
    pdf.cell(a_total - 2, 5, r["monto"], align="R")
    pdf.set_draw_color(*_LINEA_DE_LA_TABLA)
    pdf.set_line_width(0.2)
    pdf.line(x, y + alto, x + pdf.epw, y + alto)
    pdf.set_y(y + alto)


def _fila_de_un_total(pdf, hoja: dict, anchos, t: dict) -> None:
    """Una fila de los totales: la etiqueta a la izquierda (debajo del servicio) y la cifra en la columna del total."""
    a_serv, a_cant, a_precio, a_total = anchos
    x = pdf.l_margin
    alto = 9.0
    if pdf.will_page_break(alto):
        pdf.add_page()
        _cabecera_de_las_columnas(pdf, hoja, anchos)
    y = pdf.get_y()
    pdf.set_xy(x + 2, y + 2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*cz._NEGRO)
    pdf.cell(a_serv + a_cant + a_precio - 2, 5, t["etiqueta"])
    pdf.set_xy(x + a_serv + a_cant + a_precio, y + 2)
    pdf.cell(a_total - 2, 5, t["total"], align="R")
    pdf.set_draw_color(*_LINEA_DE_LA_TABLA)
    pdf.set_line_width(0.2)
    pdf.line(x, y + alto, x + pdf.epw, y + alto)
    pdf.set_y(y + alto)


def pdf_de_factura(hoja: dict, logo: Path = LOGO_DE_LA_HOJA) -> bytes:
    """El PDF de una hoja (`hoja_de_factura`): el mismo papel que la cotización (carta, logo, raya y título en
    rojo, bloque Para, cabecera roja, pie del estudio), una sola tabla de CUATRO columnas —servicio con su
    descripción, cantidad con su unidad, precio unitario y total— y debajo los totales; salta de página cuando no
    cabe y repite la cabecera. Dibuja SOLO lo que trae `hoja`; si trae `marca`, la dibuja en diagonal en cada
    página. Las piezas compartidas con la cotización (`_abrir_la_hoja`, `_pie_de_la_hoja`, `_marcar_de_borrador`)
    son las mismas; la tabla de la cotización sigue con sus dos columnas (`_cabecera_de_la_tabla`/`_fila`)."""
    pdf, x = cz._abrir_la_hoja(hoja, logo)
    anchos = _anchos_de_las_columnas(pdf, hoja)
    _cabecera_de_las_columnas(pdf, hoja, anchos)
    for r in hoja["renglones"]:
        _fila_de_las_columnas(pdf, hoja, anchos, r)
    for t in hoja["totales"]:
        _fila_de_un_total(pdf, hoja, anchos, t)
    cz._pie_de_la_hoja(pdf, hoja, x)
    if hoja["marca"]:
        cz._marcar_de_borrador(pdf, hoja["marca"])
    return bytes(pdf.output())
