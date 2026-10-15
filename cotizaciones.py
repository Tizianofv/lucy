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

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF

import db.db as db

LOGO_DE_LA_HOJA = Path(__file__).resolve().parent / "web" / "imagenes" / "logo-cds.png"

# El pie del estudio es una constante del código, como en la maqueta aprobada.
PIE_DEL_ESTUDIO = ("Santo Domingo, República Dominicana",
                   "(809) 687-9641 · caribbeandreamstudios@gmail.com")

TITULO_DE_LA_HOJA = "COTIZACIÓN DE SERVICIOS"
ENCABEZADOS_DE_LA_TABLA = ("DESCRIPCIÓN", "TOTAL")
TITULO_DE_LAS_CONDICIONES = "CONDICIONES"
ETIQUETA_TOTAL_DE_LA_OPCION = "Total de la opción"
ETIQUETAS_DEL_PARA = ("PARA", "TELÉFONO", "FECHA", "VALIDEZ")
TITULO_DE_OPCION = "Opción {n} - {nombre}"

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
    et_para, et_telefono, et_fecha, et_validez = ETIQUETAS_DEL_PARA
    para = [(et_para, cot["para_nombre"])]
    if cot.get("para_telefono"):
        para.append((et_telefono, cot["para_telefono"]))
    para.append((et_fecha, fecha_en_palabras(cot["fecha"])))
    para.append((et_validez, f"{cot['validez_dias']} días"))
    opciones = []
    for n, op in enumerate(contenido["opciones"], start=1):
        renglones = [{"descripcion": r["descripcion"], "detalle": r["detalle"],
                      "total": dinero_en_pesos(r["total"])} for r in op["renglones"]]
        total = None
        if len(op["renglones"]) > 1:
            suma = sum((Decimal(r["total"]) for r in op["renglones"]), Decimal("0"))
            total = {"etiqueta": ETIQUETA_TOTAL_DE_LA_OPCION, "total": dinero_en_pesos(suma)}
        opciones.append({"titulo": TITULO_DE_OPCION.format(n=n, nombre=op["nombre"]), "renglones": renglones, "total": total})
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


# ── La lista y la ventana de la página de Proyectos (pieza 5) ───────────────────────────────────────
#
# Todo lo que la plantilla necesita para dibujar la lista de cotizaciones de un proyecto y su ventana
# («Nueva cotización», «Corregir», o la hoja de una enviada) se arma ACÁ, con funciones puras: la plantilla no
# cuenta ni formatea dinero. Y la ruta que guarda lee el formulario con `campos_del_formulario`, que lee los
# mismos nombres de campo que dibuja `web/plantillas/_cotizaciones_del_proyecto.html` (una prueba arma el POST
# a partir de la página pintada y comprueba que lo guardado es lo escrito).

# Las tres condiciones que no cambian con la cotización (maqueta aprobada). Las otras dos (`validez` y
# `alternativas`) las escribe `db.condiciones_con_sus_textos_al_dia` con los textos de `db`.
TEXTOS_FIJOS_DE_CONDICION = {
    "deposito": "Se requiere un depósito del 50% para confirmar e iniciar los trabajos.",
    "balance": "El balance restante se cancela contra entrega.",
    "impuestos": "Los precios indicados no incluyen impuestos.",
}
VALIDEZ_POR_OMISION = 15
RENGLONES_DE_UNA_OPCION_NUEVA = 2          # sin JavaScript: dos renglones en blanco para llenar
OPCIONES_EN_BLANCO_DE_SOBRA = 1            # sin JavaScript: una opción más en blanco, para la segunda
CONDICIONES_EN_BLANCO_DE_SOBRA = 1         # sin JavaScript: una condición propia en blanco

MARCA_DE_BORRADOR = "BORRADOR"


def _dia(valor) -> date:
    return date.fromisoformat(valor) if isinstance(valor, str) else valor


def fecha_corta(fecha) -> str:
    """`2026-10-14` → «14 oct 2026» (la lista)."""
    d = _dia(fecha)
    return f"{d.day} {_MESES[d.month - 1][:3]} {d.year}"


def palabra_de_las_opciones(n: int) -> str:
    return db._NUMERO_EN_PALABRAS[min(max(n, 2), 6)]


def texto_de_condicion(clave: str, validez_dias: int, n_opciones: int) -> str:
    """El texto con que sale la condición de esa clave (la lista cerrada de `db`)."""
    if clave == "validez":
        return db.TEXTO_DE_VALIDEZ_DE_COTIZACION.format(dias=validez_dias)
    if clave == "alternativas":
        return db.TEXTO_DE_ALTERNATIVAS_DE_COTIZACION.format(palabra=palabra_de_las_opciones(n_opciones))
    return TEXTOS_FIJOS_DE_CONDICION[clave]


def resumen_de_cotizacion(cot: dict) -> dict:
    """Lo que dice un renglón de la lista: código, estado, «14 oct 2026», «3 opciones» y el precio, que es
    UNO si todas las opciones valen lo mismo y «RD$X a RD$Y» (del más barato al más caro) si no. Los totales
    son los de `cot["totales_por_opcion"]`, los de cada opción; el dinero no se vuelve a calcular acá."""
    totales = cot["totales_por_opcion"]
    n = len(totales)
    menor, mayor = min(totales), max(totales)
    precio = dinero_en_pesos(menor) if menor == mayor else f"{dinero_en_pesos(menor)} a {dinero_en_pesos(mayor)}"
    return {"id": cot["id"], "codigo": cot["codigo"], "estado": cot["estado"],
            "enviada": cot["estado"] == "enviada", "fecha": fecha_corta(cot["fecha"]),
            "opciones": f"{n} opción" if n == 1 else f"{n} opciones", "precio": precio}


def _candado(cambiado_en) -> str:
    return cambiado_en.isoformat() if isinstance(cambiado_en, (date, datetime)) else str(cambiado_en)


def _sin_rd(total: str) -> str:
    """«62500.00» → «62,500» (lo que se escribe en la casilla: sin el «RD$»)."""
    return dinero_en_pesos(total)[len("RD$"):]


def _renglon_en_blanco(sobra: bool) -> dict:
    return {"descripcion": "", "detalle": "", "total": "", "sobra": sobra}


def condiciones_de_la_ventana(guardadas: list | None, validez_dias: int, n_opciones: int) -> tuple[list[dict], list[dict]]:
    """Las condiciones que la ventana dibuja, en dos listas: las cinco de la lista cerrada de `db` (cada una
    marcada o no, con su texto) y las propias que hubiera. `guardadas=None` es una cotización nueva: las
    cinco marcadas."""
    por_clave = {c["clave"]: c for c in (guardadas or []) if c["clave"] is not None}
    hechas = []
    for clave in db.CLAVES_DE_CONDICION_DE_COTIZACION:
        marcada = True if guardadas is None else bool(por_clave.get(clave, {}).get("marcada", False))
        hechas.append({"clave": clave, "texto": texto_de_condicion(clave, validez_dias, n_opciones),
                       "marcada": marcada})
    propias = [c for c in (guardadas or []) if c["clave"] is None]
    return hechas, propias


def valores_del_formulario(cot: dict | None, *, nombre_del_cliente: str | None,
                           telefono_del_cliente: str | None, hoy: date, codigo_que_toca: str | None) -> dict:
    """Lo que la ventana pone en cada casilla. `cot` es una de `db.cotizaciones_de_proyectos` (borrador o
    enviada a corregir) o `None` (nueva: el cliente del proyecto, su teléfono, hoy, 15 días y las cinco
    condiciones marcadas). Sin JavaScript sobran casillas en blanco: un renglón más por opción, una opción
    entera y una condición propia, para poder agregar sin el botón; lo vacío no se guarda."""
    if cot is None:
        validez = VALIDEZ_POR_OMISION
        guardadas = None
        opciones = []
        nombre, telefono, fecha = nombre_del_cliente or "", telefono_del_cliente or "", hoy.isoformat()
        codigo, candado = codigo_que_toca or "", ""
    else:
        validez = cot["validez_dias"]
        guardadas = cot["contenido"]["condiciones"]
        opciones = [{"nombre": op["nombre"],
                     "renglones": [{"descripcion": r["descripcion"], "detalle": r["detalle"], "total": _sin_rd(r["total"])}
                                   for r in op["renglones"]]}
                    for op in cot["contenido"]["opciones"]]
        nombre, telefono = cot["para_nombre"], cot["para_telefono"] or ""
        fecha = _dia(cot["fecha"]).isoformat()
        codigo, candado = cot["codigo"], _candado(cot["cambiado_en"])
    # `sobra` marca lo que dibuja el servidor para quien no tiene JavaScript (con JavaScript se esconde y el
    # botón «Agregar» pone lo que haga falta). Una cotización sin ninguna opción empieza con una en blanco
    # que SÍ se ve, de dos renglones, el segundo de los cuales sobra.
    for op in opciones:
        op["sobra"] = False
        op["renglones"] = [{**r, "sobra": False} for r in op["renglones"]] + [_renglon_en_blanco(True)]
    if not opciones:
        opciones.append({"nombre": "", "sobra": False,
                         "renglones": [_renglon_en_blanco(False)]
                         + [_renglon_en_blanco(True) for _ in range(RENGLONES_DE_UNA_OPCION_NUEVA - 1)]})
    for _ in range(OPCIONES_EN_BLANCO_DE_SOBRA):
        opciones.append({"nombre": "", "sobra": True,
                         "renglones": [_renglon_en_blanco(True) for _ in range(RENGLONES_DE_UNA_OPCION_NUEVA)]})
    n_opciones = len([o for o in opciones if o["nombre"]]) or 1
    conocidas, propias = condiciones_de_la_ventana(guardadas, validez, n_opciones)
    for c in conocidas:      # «Las N opciones son alternativas» no se ofrece con una sola opción
        c["oculta"] = c["clave"] == "alternativas" and n_opciones < 2
    propias = [{"texto": c["texto"], "marcada": c["marcada"], "sobra": False} for c in propias]
    propias += [{"texto": "", "marcada": True, "sobra": True} for _ in range(CONDICIONES_EN_BLANCO_DE_SOBRA)]
    return {"codigo": codigo, "candado": candado, "para_nombre": nombre, "para_telefono": telefono,
            "fecha": fecha, "validez": validez, "opciones": opciones, "condiciones": conocidas,
            "propias": propias}


def cotizacion_en_blanco(*, nombre: str, telefono: str, hoy: date, codigo: str) -> dict:
    """La cotización de mentira con que se dibuja «Así va a salir» de una NUEVA (aún no hay nada guardado):
    el cliente, su teléfono, hoy, 15 días, ninguna opción y las cinco condiciones marcadas."""
    conocidas, _ = condiciones_de_la_ventana(None, VALIDEZ_POR_OMISION, 1)
    return {"codigo": codigo, "estado": "borrador", "para_nombre": nombre, "para_telefono": telefono or None,
            "fecha": hoy, "validez_dias": VALIDEZ_POR_OMISION,
            "contenido": {"opciones": [], "condiciones": [c for c in conocidas if c["clave"] != "alternativas"]}}


# El nombre de cada casilla del formulario. `campos_del_formulario` los lee; la plantilla los escribe igual.
_RE_OPCION = re.compile(r"^op-([0-9]{1,3})-nombre$")
_RE_RENGLON = re.compile(r"^op-([0-9]{1,3})-r-([0-9]{1,3})-(descripcion|detalle|total)$")
_RE_PROPIA = re.compile(r"^cond-extra-([0-9]{1,3})-texto$")


def campos_del_formulario(formulario) -> dict:
    """Lo que el POST de la ventana trae, ya en la forma que pide `db.guardar_cotizacion`. No decide qué
    vale: eso lo hace la puerta de `db`; acá solo se leen los nombres. `formulario` es el de la petición
    (se le pregunta con `.get`, `in` y `.keys()`). Un campo que falta llega como `None` (nombre y teléfono:
    no se tocan) o vacío (los demás) y la puerta lo rechaza o lo descarta."""
    claves = list(formulario.keys())
    ops, rengs = set(), {}
    for k in claves:
        m = _RE_OPCION.match(k)
        if m:
            ops.add(int(m.group(1)))
        m = _RE_RENGLON.match(k)
        if m:
            ops.add(int(m.group(1)))
            rengs.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    opciones = []
    for o in sorted(ops):
        opciones.append({"nombre": formulario.get(f"op-{o}-nombre", ""), "renglones": [
            {"descripcion": formulario.get(f"op-{o}-r-{r}-descripcion", ""),
             "detalle": formulario.get(f"op-{o}-r-{r}-detalle", ""),
             "total": formulario.get(f"op-{o}-r-{r}-total", "")} for r in sorted(rengs.get(o, ()))]})
    condiciones = [{"clave": clave, "texto": texto_de_condicion(clave, VALIDEZ_POR_OMISION, 2),
                    "marcada": f"cond-{clave}" in formulario} for clave in db.CLAVES_DE_CONDICION_DE_COTIZACION]
    for k in sorted((int(m.group(1)) for m in map(_RE_PROPIA.match, claves) if m)):
        condiciones.append({"clave": None, "texto": formulario.get(f"cond-extra-{k}-texto", ""),
                            "marcada": f"cond-extra-{k}-marcada" in formulario})
    validez = formulario.get("validez")
    if isinstance(validez, str) and validez.strip().isdigit():
        validez = int(validez.strip())
    return {"contenido": {"opciones": opciones, "condiciones": condiciones}, "validez": validez,
            "para_nombre": formulario.get("para-nombre"), "para_telefono": formulario.get("para-telefono"),
            "fecha": formulario.get("fecha"), "cambiado_en": formulario.get("cambiado_en") or None,
            "accion": formulario.get("accion")}


def datos_de_la_vista_previa() -> dict:
    """Lo que el guion de la ventana necesita para rehacer «Así va a salir» en el navegador sin tener un
    solo texto escrito aparte: sale de las MISMAS constantes que usa `hoja_de_cotizacion` y de los textos de
    `db`. (Las dos cuentas que sí hace el guion, el dinero y la fecha en palabras, las vigila una prueba que
    corre el guion y lo compara con `dinero_en_pesos` y `fecha_en_palabras`.)"""
    return {"titulo": TITULO_DE_LA_HOJA, "encabezados": list(ENCABEZADOS_DE_LA_TABLA),
            "condiciones": TITULO_DE_LAS_CONDICIONES, "total_de_la_opcion": ETIQUETA_TOTAL_DE_LA_OPCION,
            "para": list(ETIQUETAS_DEL_PARA), "opcion": TITULO_DE_OPCION, "pie": list(PIE_DEL_ESTUDIO),
            "meses": list(_MESES), "validez": db.TEXTO_DE_VALIDEZ_DE_COTIZACION,
            "alternativas": db.TEXTO_DE_ALTERNATIVAS_DE_COTIZACION,
            "palabras": {str(k): v for k, v in db._NUMERO_EN_PALABRAS.items()}}


def ventana_pedida(cotizacion: str, corregir: int, solo_ver: bool) -> tuple[str, int]:
    """`?cotizacion=` y `?corregir=` → `("nueva", 0)`, `("<id>", 0|1)` o `("", 0)` si no piden ventana.
    En solo ver no se puede pedir una ventana que escribe: ni la nueva ni corregir."""
    pedida = (cotizacion or "").strip()
    if pedida == "nueva" and not solo_ver:
        return "nueva", 0
    if pedida.isdigit() and pedida.isascii() and len(pedida) < 12:
        return pedida, 0 if solo_ver else (1 if corregir else 0)
    return "", 0


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


def _marcar_de_borrador(pdf: FPDF) -> None:
    """«BORRADOR» en diagonal y en grande, transparente, ENCIMA de lo demás en cada página (debajo lo taparía
    el fondo gris del bloque Para). Es lo que distingue el PDF de un borrador del de una enviada (decisión de
    Tiziano, 14-oct-2026: «B»). El tamaño sale de medir la palabra: 190 mm de largo en diagonal."""
    pdf.set_font("Helvetica", "B", 100)
    tamano = 100 * 190 / pdf.get_string_width(MARCA_DE_BORRADOR)
    for n in range(1, pdf.pages_count + 1):
        pdf.page = n
        pdf.set_font("Helvetica", "B", tamano)
        ancho = pdf.get_string_width(MARCA_DE_BORRADOR)
        cx, cy = pdf.w / 2, pdf.h / 2
        with pdf.local_context(fill_opacity=0.16, text_color=ROJO):
            with pdf.rotation(45, x=cx, y=cy):
                pdf.text(cx - ancho / 2, cy + 0.36 * tamano / 2.835, MARCA_DE_BORRADOR)   # media altura de la mayúscula, en mm


def pdf_de_cotizacion(hoja: dict, logo: Path = LOGO_DE_LA_HOJA, *, borrador: bool = False) -> bytes:
    """El PDF de una hoja (`hoja_de_cotizacion`): carta, logo, raya y título en rojo, bloque Para con raya
    roja, una tabla por opción con cabecera roja, condiciones con viñeta y el pie del estudio; salta de página
    cuando no cabe. Dibuja SOLO lo que trae `hoja`; con `borrador=True` además la marca «BORRADOR» en cada
    página (el de una enviada va sin marca)."""
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
    if borrador:
        _marcar_de_borrador(pdf)
    return bytes(pdf.output())
