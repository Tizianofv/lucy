"""La hoja de una factura y su PDF (pieza 3 del diseño «Facturas», 21-oct-2026).

Es hermana de `cotizaciones.py` y copia su camino: UNA SOLA FUENTE para la vista previa y el PDF.
`hoja_de_factura(f)` arma, sin tocar nada de afuera, un diccionario con TODOS los textos ya escritos; la macro
`web/plantillas/_hoja_de_factura.html` y `pdf_de_factura` solo lo DIBUJAN. La herramienta de PDF (`fpdf2`,
Helvetica de fábrica), la primera página (logo, raya, título, bloque Para), la tabla, el pie del estudio y la
marca en diagonal son LAS MISMAS funciones que usa la cotización (`cotizaciones._abrir_la_hoja`,
`_cabecera_de_la_tabla`, `_fila`, `_pie_de_la_hoja`, `_marcar_de_borrador`): no hay otra copia de la hoja.

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
"""
from __future__ import annotations

from pathlib import Path

import cotizaciones as cz

LOGO_DE_LA_HOJA = cz.LOGO_DE_LA_HOJA
TITULO_DE_LA_FACTURA = "FACTURA"
CODIGO_DEL_BORRADOR = "BORRADOR"
MARCA_DE_ANULADA = "ANULADA"
ENCABEZADOS_DE_LA_FACTURA = ("DESCRIPCIÓN", "MONTO")
ETIQUETA_DEL_SUBTOTAL = "Subtotal"
ETIQUETA_DEL_ITBIS = "ITBIS {pct}%"
ETIQUETA_DEL_TOTAL = "TOTAL"
ETIQUETAS_DEL_PARA_DE_LA_FACTURA = {
    "para": "PARA", "rnc_cedula": "RNC / CÉDULA", "fecha": "FECHA", "ncf": "NCF", "ncf_tipo": "TIPO DE NCF",
    "estado": "ESTADO", "motivo": "MOTIVO DE LA ANULACIÓN"}


def _porcentaje(pct) -> str:
    """`Decimal("18.00")` → «18»."""
    return str(int(pct)) if pct == pct.to_integral_value() else f"{pct.normalize():f}"


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
    renglones = [{"descripcion": r["descripcion"], "detalle": r.get("detalle") or "",
                  "monto": cz.dinero_en_pesos(r["monto"])} for r in f["renglones"]]
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
        t.append(r["monto"])
    for x in hoja["totales"]:
        t += [x["etiqueta"], x["total"]]
    t += list(hoja["pie"])
    return t


def nombre_del_archivo(f: dict) -> str:
    """`FAC-2026-001.pdf`; un borrador no tiene número y se baja como `Factura-borrador-<id>.pdf`."""
    if f["estado"] in ("emitida", "anulada") and f.get("codigo"):
        return f"{f['codigo']}.pdf"
    return f"Factura-borrador-{int(f['id'])}.pdf"


def pdf_de_factura(hoja: dict, logo: Path = LOGO_DE_LA_HOJA) -> bytes:
    """El PDF de una hoja (`hoja_de_factura`): el mismo papel que la cotización (carta, logo, raya y título en
    rojo, bloque Para, tabla con cabecera roja, pie del estudio), una sola tabla con los renglones y debajo los
    totales; salta de página cuando no cabe. Dibuja SOLO lo que trae `hoja`; si trae `marca`, la dibuja en
    diagonal en cada página."""
    pdf, x = cz._abrir_la_hoja(hoja, logo)
    pdf.set_font("Helvetica", "B", 10)
    montos = [r["monto"] for r in hoja["renglones"]] + [t["total"] for t in hoja["totales"]]
    ancho_total = max([cz._ANCHO_DEL_TOTAL] + [pdf.get_string_width(m) + 6 for m in montos])
    ancho_desc = pdf.epw - ancho_total
    cz._cabecera_de_la_tabla(pdf, hoja, ancho_desc, ancho_total)
    for r in hoja["renglones"]:
        cz._fila(pdf, hoja, ancho_desc, ancho_total, r["descripcion"], r["detalle"], r["monto"])
    for t in hoja["totales"]:
        cz._fila(pdf, hoja, ancho_desc, ancho_total, t["etiqueta"], "", t["total"])
    cz._pie_de_la_hoja(pdf, hoja, x)
    if hoja["marca"]:
        cz._marcar_de_borrador(pdf, hoja["marca"])
    return bytes(pdf.output())
