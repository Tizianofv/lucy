"""La hoja de una cotización: lo que se ve en la vista previa y lo que sale en el PDF (pieza 4 del diseño
«Cotizaciones en Proyectos», 14-oct-2026).

UNA SOLA FUENTE para los dos: `hoja_de_cotizacion(cot)` arma, sin tocar nada de afuera, un diccionario con
TODOS los textos de la hoja ya escritos como se leen (fecha en palabras, dinero con «RD$», solo las
condiciones marcadas, el pie del estudio). La vista previa (`web/plantillas/_hoja_de_cotizacion.html`)
y el escritor del PDF (`pdf_de_cotizacion`) solo DIBUJAN ese diccionario: ninguno decide qué texto sale.
`textos_de_la_hoja(hoja)` los da en el orden en que se dibujan, para las pruebas.

La letra del PDF es la de fábrica de `fpdf2` (Helvetica), sin descargar nada: solo dibuja Latin-1, y de eso
se ocupa la puerta del contenido (`db.contenido_de_cotizacion_que_vale`), que rechaza lo demás. Los títulos
de las opciones salen como se escribieron (no en mayúsculas: pasar a mayúsculas «ÿ» o «ß» las saca de
Latin-1); lo fijo de la hoja (DESCRIPCIÓN, TOTAL, CONDICIONES…) sí va en mayúsculas.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF

LOGO_DE_LA_HOJA = Path(__file__).resolve().parent / "web" / "imagenes" / "logo-cds.png"

# El pie del estudio es una constante del código, como en la maqueta aprobada.
PIE_DEL_ESTUDIO = ("Santo Domingo, República Dominicana",
                   "(809) 687-9641 · caribbeandreamstudios@gmail.com")

TITULO_DE_LA_HOJA = "COTIZACIÓN DE SERVICIOS"
ENCABEZADOS_DE_LA_TABLA = ("DESCRIPCIÓN", "TOTAL")
TITULO_DE_LAS_CONDICIONES = "CONDICIONES"
ETIQUETA_TOTAL_DE_LA_OPCION = "Total de la opción"

_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")

ROJO = (192, 39, 45)        # #c0272d, el de la maqueta


def fecha_en_palabras(fecha) -> str:
    """`2026-10-14` (o un `date`) → «14 de octubre de 2026»."""
    d = date.fromisoformat(fecha) if isinstance(fecha, str) else fecha
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


def dinero_en_pesos(valor) -> str:
    """`"62500.00"` → «RD$62,500»; `"62500.50"` → «RD$62,500.50»."""
    q = Decimal(valor).quantize(Decimal("0.01"))
    if q == q.to_integral_value():
        return f"RD${int(q):,}"
    return f"RD${q:,.2f}"


def hoja_de_cotizacion(cot: dict) -> dict:
    """Los textos de la hoja de una cotización. `cot` es una de las que devuelve
    `db.cotizaciones_de_proyectos()`: `codigo`, `para_nombre`, `para_telefono`, `fecha`, `validez_dias` y
    `contenido` ya leído. Función pura."""
    contenido = cot["contenido"]
    para = [("PARA", cot["para_nombre"])]
    if cot.get("para_telefono"):
        para.append(("TELÉFONO", cot["para_telefono"]))
    para.append(("FECHA", fecha_en_palabras(cot["fecha"])))
    para.append(("VALIDEZ", f"{cot['validez_dias']} días"))
    opciones = []
    for n, op in enumerate(contenido["opciones"], start=1):
        renglones = [{"descripcion": r["descripcion"], "detalle": r["detalle"],
                      "total": dinero_en_pesos(r["total"])} for r in op["renglones"]]
        total = None
        if len(op["renglones"]) > 1:
            suma = sum((Decimal(r["total"]) for r in op["renglones"]), Decimal("0"))
            total = {"etiqueta": ETIQUETA_TOTAL_DE_LA_OPCION, "total": dinero_en_pesos(suma)}
        opciones.append({"titulo": f"Opción {n} - {op['nombre']}", "renglones": renglones, "total": total})
    return {
        "titulo": TITULO_DE_LA_HOJA,
        "codigo": cot["codigo"],
        "para": para,
        "encabezados": ENCABEZADOS_DE_LA_TABLA,
        "opciones": opciones,
        "condiciones_titulo": TITULO_DE_LAS_CONDICIONES,
        "condiciones": [c["texto"] for c in contenido["condiciones"] if c["marcada"]],
        "pie": PIE_DEL_ESTUDIO,
    }


def textos_de_la_hoja(hoja: dict) -> list[str]:
    """Todos los textos de la hoja, en el orden en que se dibujan (el detalle vacío no se dibuja)."""
    t = [hoja["titulo"], hoja["codigo"]]
    for etiqueta, valor in hoja["para"]:
        t += [etiqueta, valor]
    for op in hoja["opciones"]:
        t.append(op["titulo"])
        t += list(hoja["encabezados"])
        for r in op["renglones"]:
            t.append(r["descripcion"])
            if r["detalle"]:
                t.append(r["detalle"])
            t.append(r["total"])
        if op["total"]:
            t += [op["total"]["etiqueta"], op["total"]["total"]]
    if hoja["condiciones"]:
        t.append(hoja["condiciones_titulo"])
        t += list(hoja["condiciones"])
    t += list(hoja["pie"])
    return t


# ── El PDF ──────────────────────────────────────────────────────────────────────────────────────────

_MARGEN = 20.0
_ANCHO_DEL_TOTAL = 34.0     # el mínimo: crece si algún total no cabe (ver `pdf_de_cotizacion`)
_GRIS_TEXTO = (102, 102, 102)
_NEGRO = (29, 29, 29)


def _altura(pdf: FPDF, ancho: float, alto_de_linea: float, texto: str) -> float:
    """Lo que ocupa `texto` en `ancho` sin dibujarlo."""
    return pdf.multi_cell(ancho, alto_de_linea, texto, dry_run=True, output="HEIGHT")


def _cabecera_de_la_tabla(pdf: FPDF, hoja: dict, ancho_desc: float, ancho_total: float) -> None:
    x = pdf.l_margin
    y = pdf.get_y()
    pdf.set_fill_color(*ROJO)
    pdf.rect(x, y, pdf.epw, 6, style="F")
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(x + 2, y)
    pdf.cell(ancho_desc - 2, 6, hoja["encabezados"][0])
    pdf.set_xy(x + ancho_desc, y)
    pdf.cell(ancho_total - 2, 6, hoja["encabezados"][1], align="R")
    pdf.set_y(y + 6)


def _fila(pdf: FPDF, hoja: dict, ancho_desc: float, ancho_total: float, descripcion: str, detalle: str,
          total: str) -> None:
    """Una fila de la tabla: descripción en negrita, detalle debajo, total a la derecha. Si no cabe en lo que
    queda de la página, salta de página y repite la cabecera."""
    x = pdf.l_margin
    ancho_texto = ancho_desc - 4
    pdf.set_font("Helvetica", "B", 10)
    h_desc = _altura(pdf, ancho_texto, 5, descripcion)
    h_det = 0.0
    if detalle:
        pdf.set_font("Helvetica", "", 8.5)
        h_det = _altura(pdf, ancho_texto, 4.2, detalle)
    alto = h_desc + h_det + 4
    if pdf.will_page_break(alto):
        pdf.add_page()
        _cabecera_de_la_tabla(pdf, hoja, ancho_desc, ancho_total)
    y = pdf.get_y()
    pdf.set_xy(x + 2, y + 2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_NEGRO)
    pdf.multi_cell(ancho_texto, 5, descripcion, new_x="LEFT", new_y="NEXT")
    if detalle:
        pdf.set_x(x + 2)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*_GRIS_TEXTO)
        pdf.multi_cell(ancho_texto, 4.2, detalle, new_x="LEFT", new_y="NEXT")
    pdf.set_xy(x + ancho_desc, y + 2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_NEGRO)
    pdf.cell(ancho_total - 2, 5, total, align="R")
    pdf.set_draw_color(230, 230, 230)
    pdf.set_line_width(0.2)
    pdf.line(x, y + alto, x + pdf.epw, y + alto)
    pdf.set_y(y + alto)


def pdf_de_cotizacion(hoja: dict, logo: Path = LOGO_DE_LA_HOJA) -> bytes:
    """El PDF de una hoja (`hoja_de_cotizacion`): carta, logo, raya y título en rojo, bloque Para con raya
    roja, una tabla por opción con cabecera roja, condiciones con viñeta y el pie del estudio; salta de página
    cuando no cabe. Dibuja SOLO lo que trae `hoja`."""
    pdf = FPDF(unit="mm", format="Letter")
    pdf.set_margins(_MARGEN, 18, _MARGEN)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(hoja["codigo"])
    pdf.set_creator("Lucy")
    pdf.add_page()
    x = pdf.l_margin
    ancho_logo = 48.0
    pdf.image(str(logo), x=x, y=18, w=ancho_logo)
    y = 18 + ancho_logo * 114 / 1200 + 3
    pdf.set_draw_color(*ROJO)
    pdf.set_line_width(0.7)
    pdf.line(x, y, x + pdf.epw, y)
    pdf.set_y(y + 8)

    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*ROJO)
    pdf.cell(0, 9, hoja["titulo"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(119, 119, 119)
    pdf.cell(0, 5, hoja["codigo"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # El bloque Para: fondo gris y raya roja a la izquierda. Cada valor puede ocupar varias líneas (un nombre
    # de 200 caracteres no cabe en una): se mide antes de dibujar el fondo.
    ancho_valor = pdf.epw - 8
    pdf.set_font("Helvetica", "B", 10)
    altos = [_altura(pdf, ancho_valor, 5.2, valor) for _, valor in hoja["para"]]
    y0 = pdf.get_y()
    alto = 4 + sum(3.2 + h + 0.8 for h in altos)
    pdf.set_fill_color(244, 244, 244)
    pdf.rect(x, y0, pdf.epw, alto, style="F")
    pdf.set_fill_color(*ROJO)
    pdf.rect(x, y0, 1, alto, style="F")
    pdf.set_y(y0 + 2)
    for etiqueta, valor in hoja["para"]:
        pdf.set_x(x + 5)
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_text_color(136, 136, 136)
        pdf.cell(ancho_valor, 3.2, etiqueta, new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x + 5)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*_NEGRO)
        pdf.multi_cell(ancho_valor, 5.2, valor, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.8)
    pdf.set_y(y0 + alto + 7)

    # La columna del total crece si algún total no cabe en el mínimo (hasta RD$999,999,999.90 con 10 renglones).
    pdf.set_font("Helvetica", "B", 10)
    totales = [r["total"] for op in hoja["opciones"] for r in op["renglones"]]
    totales += [op["total"]["total"] for op in hoja["opciones"] if op["total"]]
    ancho_total = max([_ANCHO_DEL_TOTAL] + [pdf.get_string_width(t) + 6 for t in totales])
    ancho_desc = pdf.epw - ancho_total
    for op in hoja["opciones"]:
        # El título de la opción no se queda solo al final de una página.
        pdf.set_font("Helvetica", "B", 9)
        alto_titulo = _altura(pdf, pdf.epw, 5, op["titulo"]) + 2
        if pdf.will_page_break(alto_titulo + 6 + 20):
            pdf.add_page()
        pdf.set_text_color(*ROJO)
        pdf.set_x(x)
        pdf.multi_cell(pdf.epw, 5, op["titulo"], new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        _cabecera_de_la_tabla(pdf, hoja, ancho_desc, ancho_total)
        for r in op["renglones"]:
            _fila(pdf, hoja, ancho_desc, ancho_total, r["descripcion"], r["detalle"], r["total"])
        if op["total"]:
            _fila(pdf, hoja, ancho_desc, ancho_total, op["total"]["etiqueta"], "", op["total"]["total"])
        pdf.ln(4)

    if hoja["condiciones"]:
        if pdf.will_page_break(7 + 12):
            pdf.add_page()
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*ROJO)
        pdf.set_x(x)
        pdf.cell(0, 7, hoja["condiciones_titulo"], new_x="LMARGIN", new_y="NEXT")
        ancho_texto = pdf.epw - 8
        for texto in hoja["condiciones"]:
            pdf.set_font("Helvetica", "", 9)
            if pdf.will_page_break(_altura(pdf, ancho_texto, 4.8, texto) + 1.5):
                pdf.add_page()
            y1 = pdf.get_y()
            pdf.set_fill_color(*ROJO)
            pdf.ellipse(x + 2.2, y1 + 1.9, 1.2, 1.2, style="F")      # la viñeta se dibuja: «•» no es Latin-1
            pdf.set_xy(x + 8, y1)
            pdf.set_text_color(68, 68, 68)
            pdf.multi_cell(ancho_texto, 4.8, texto, new_x="LEFT", new_y="NEXT")
            pdf.ln(1.5)

    if pdf.will_page_break(16):
        pdf.add_page()
    pdf.ln(5)
    y2 = pdf.get_y()
    pdf.set_draw_color(230, 230, 230)
    pdf.set_line_width(0.2)
    pdf.line(x, y2, x + pdf.epw, y2)
    pdf.set_y(y2 + 2)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(136, 136, 136)
    for linea in hoja["pie"]:
        pdf.set_x(x)
        pdf.cell(0, 4, linea, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
