"""Lo que la pantalla «Facturas» necesita y no es de la base ni de la hoja (pieza 4 del diseño «Facturas», 21-oct-2026).

Es el hermano de `cotizaciones.py` para las páginas: funciones PURAS (no abren la base, no llaman a la App ni a Noco).
Las rutas de `web/app.py` leen, llaman a la puerta de `db/db.py` (`db.crear_borrador_de_factura`,
`db.corregir_borrador_de_factura`, …) y esto traduce: del formulario a lo que pide la puerta, y de lo que la puerta
rechazó a palabras para la persona.

QUÉ NO HACE, a propósito:
  · no decide qué vale: eso es de la puerta (`db`). Aquí solo se leen los nombres de las casillas.
  · no guarda nada ni sabe de SQL.
  · el TIPO de una liga (sesión o trabajo) no lo escribe quien llena el formulario: sale de lo que la App contestó
    para esa referencia (`es_trabajo`), en un solo sitio (`ligas_de_las_refs`).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal

import cotizaciones as cz
import db.db as db

# Cuántos renglones en blanco se dibujan (sin JavaScript no hay botón de «otro renglón»). Es una decisión de la
# pantalla, no una medición.
RENGLONES_EN_BLANCO = 3
RENGLONES_DE_SOBRA = 2

_RE_RENGLON = re.compile(r"^r-([0-9]{1,3})-(descripcion|detalle|cantidad|unidad|precio)$")

ETIQUETA_DE_ESTADO = {"borrador": "Borrador", "emitida": "Emitida", "anulada": "Anulada"}
ETIQUETA_DEL_FILTRO = {"borrador": "Borradores", "emitida": "Emitidas", "anulada": "Anuladas"}
ETIQUETA_DE_LIGA = {"sesion": "Sesión", "trabajo": "Trabajo"}


def estado_pedido(texto) -> str | None:
    """El estado por el que se filtra la lista: uno de los de la base, o `None` (todas). Lo que no se conoce es
    «todas», nunca un filtro inventado."""
    return texto if isinstance(texto, str) and texto in ETIQUETA_DE_ESTADO else None


def etiqueta_del_estado(estado) -> str:
    return ETIQUETA_DE_ESTADO.get(estado, str(estado))


def fila_de_la_lista(f: dict, nombres_de_proyecto: dict[int, str], pago: dict | None = None) -> dict:
    """Una fila de la lista: lo que la plantilla pinta, ya escrito. `f` es una fila de
    `db.facturas_para_la_lista`. El dinero lo escribe `cotizaciones.dinero_en_pesos` (el mismo de la hoja). `pago` es
    lo que `el_pago_de_la_factura` dijo de esa factura (pieza 7); sin él (la lista del proyecto no lo lee) no se pinta
    ningún estado de pago."""
    return {
        "id": f["id"],
        "numero": f["codigo"] or "Borrador",
        "es_borrador": f["estado"] == "borrador",
        "cliente": f["para_nombre"],
        "fecha": cz.fecha_corta(f["fecha"]),
        "total": cz.dinero_en_pesos(f["total"]),
        # Con comprobante (NCF) la lista dice además lo que el cliente paga de verdad (`db.totales_de_factura`). Sin
        # NCF es `None` y no se pinta: el total ya es lo que paga.
        "total_a_pagar": cz.dinero_en_pesos(f["total_a_pagar"]) if f.get("ncf") else None,
        "estado": f["estado"],
        "estado_texto": etiqueta_del_estado(f["estado"]),
        "proyecto_id": f["proyecto_id"],
        "proyecto": (None if f["proyecto_id"] is None
                     else nombres_de_proyecto.get(f["proyecto_id"]) or f"Proyecto {f['proyecto_id']}"),
        "sesiones": f["ligas_vivas"],
        "pago": dict(PAGO_QUE_NO_SE_PINTA) if pago is None else pago,
    }


# ── Lo que llega del formulario ───────────────────────────────────────────────────────────────────

def texto_de(valor) -> str:
    """Un campo que el POST trajo: solo un texto cuenta (un archivo subido o cualquier otra cosa es «nada»)."""
    return valor if isinstance(valor, str) else ""


def _entero_o_el_texto(valor):
    """Vacío es «ninguno» (`None`); dígitos, el entero; cualquier otra cosa se deja como texto para que la puerta
    la rechace con su razón (nunca se adivina)."""
    texto = texto_de(valor).strip()
    if texto == "":
        return None
    return int(texto) if texto.isascii() and texto.isdigit() else texto


def renglones_del_formulario(formulario) -> list[dict]:
    """Los renglones que el formulario trae (`r-<n>-descripcion|detalle|cantidad|unidad|precio`), en el orden de
    `n`. Un renglón sin nada escrito en ninguna de sus cinco casillas es de sobra y se descarta; uno con algo
    escrito va a la puerta tal cual (si le falta la cantidad, el precio o la descripción, ella lo rechaza). El
    detalle y la unidad vacíos no se mandan; la cantidad y el precio SÍ van siempre (vacíos, si lo están) para que
    la puerta diga cuál falta. El total del renglón no se manda: lo calcula la puerta."""
    indices = sorted({int(m.group(1)) for m in map(_RE_RENGLON.match, list(formulario.keys())) if m})
    hechos = []
    for n in indices:
        descripcion = texto_de(formulario.get(f"r-{n}-descripcion"))
        detalle = texto_de(formulario.get(f"r-{n}-detalle"))
        cantidad = texto_de(formulario.get(f"r-{n}-cantidad"))
        unidad = texto_de(formulario.get(f"r-{n}-unidad"))
        precio = texto_de(formulario.get(f"r-{n}-precio"))
        if not any(x.strip() for x in (descripcion, detalle, cantidad, unidad, precio)):
            continue
        renglon = {"descripcion": descripcion, "cantidad": cantidad, "precio_unitario": precio}
        if detalle.strip():
            renglon["detalle"] = detalle
        if unidad.strip():
            renglon["unidad"] = unidad
        hechos.append(renglon)
    return hechos


def campos_del_formulario(formulario) -> dict:
    """Lo que el POST de una factura trae, ya en la forma que pide la puerta. No decide qué vale. `formulario` es el
    de la petición (se le pregunta con `.get`, `.getlist` y `.keys()`). El ITBIS es una casilla: marcada es el
    porcentaje que usa la casa (`db.ITBIS_DE_FACTURA`), sin marcar es «sin ITBIS». El descuento es una cifra escrita
    (vacía = sin descuento)."""
    refs = [r.strip() for r in formulario.getlist("ref") if isinstance(r, str) and r.strip()]
    return {
        "cliente": _entero_o_el_texto(formulario.get("cliente")),
        "proyecto": _entero_o_el_texto(formulario.get("proyecto")),
        "cotizacion": _entero_o_el_texto(formulario.get("cotizacion")),
        "opcion": _entero_o_el_texto(formulario.get("opcion")),
        "para_nombre": texto_de(formulario.get("para_nombre")),
        "fecha": texto_de(formulario.get("fecha")).strip(),
        "itbis_pct": str(db.ITBIS_DE_FACTURA) if "itbis" in formulario else None,
        "descuento": texto_de(formulario.get("descuento")).strip(),
        "ncf": texto_de(formulario.get("ncf")),
        "ncf_tipo": texto_de(formulario.get("ncf_tipo")),
        "rnc_cedula": texto_de(formulario.get("rnc_cedula")),
        "renglones": renglones_del_formulario(formulario),
        "cambiado_en": texto_de(formulario.get("cambiado_en")) or None,
        "refs": refs,
    }


def candado(cambiado_en) -> str:
    """El `cambiado_en` leído, como texto ISO: es lo que viaja escondido en el formulario y vuelve a la puerta."""
    return cambiado_en.isoformat() if isinstance(cambiado_en, (date, datetime)) else str(cambiado_en)


def _sin_rd(total) -> str:
    """«62500.00» → «62,500» (lo que se escribe en la casilla del precio)."""
    return cz.dinero_en_pesos(total)[len("RD$"):]


def _cantidad_escrita(valor) -> str:
    """Una cantidad como se escribe en su casilla: `Decimal("2.000")` → «2», `Decimal("1.500")` → «1.5». Un texto
    que la persona escribió se devuelve tal cual (un rechazo no pierde lo escrito)."""
    if valor in (None, ""):
        return ""
    if not isinstance(valor, Decimal):
        return str(valor)
    return f"{valor.normalize():f}"


def _precio_escrito(valor) -> str:
    """Un precio como se escribe en su casilla: `Decimal("62500.00")` → «62,500»; un texto, tal cual."""
    if valor in (None, ""):
        return ""
    return _sin_rd(valor) if isinstance(valor, Decimal) else str(valor)


def _total_escrito(cantidad, precio) -> str:
    """El total que se ve en el formulario, calculado con lo que hay escrito (solo para mirarlo: la puerta lo vuelve
    a calcular al guardar). Vacío si las dos casillas no son números que sirvan."""
    try:
        c = Decimal(str(cantidad).replace(",", ""))
        p = Decimal(str(precio).replace(",", ""))
    except (ArithmeticError, ValueError):
        return ""
    if not c.is_finite() or not p.is_finite() or c <= 0 or p <= 0:
        return ""
    return _sin_rd(db.total_del_renglon(c.quantize(Decimal("0.001")), p.quantize(Decimal("0.01"))))


def _descuento_escrito(valor) -> str:
    """El descuento como se escribe en su casilla: cero (o nada) se deja en blanco; `Decimal("500.00")` → «500»;
    un texto que la persona escribió se devuelve tal cual (un rechazo no pierde lo escrito)."""
    if valor in (None, "") or (isinstance(valor, Decimal) and valor == 0):
        return ""
    return _precio_escrito(valor)


def descuento_de_las_sesiones(filas: list[dict]) -> Decimal:
    """La suma de los `descuento` de las sesiones o trabajos escogidos (0 si ninguno trae). Es con lo que la pantalla
    llena la casilla del descuento al crear una factura desde sesiones; el descuento de cada sesión lo dice la App."""
    return sum((r["descuento"] for r in filas if isinstance(r.get("descuento"), Decimal)), Decimal("0"))


def casillas_de_renglones(renglones: list[dict], *, con_sobra: bool = True) -> list[dict]:
    """Los renglones como casillas: los que hay, con su cantidad, su unidad y su precio como se escriben, el total
    calculado para mirarlo, y los en blanco que faltan para llegar a `RENGLONES_EN_BLANCO` (más
    `RENGLONES_DE_SOBRA` si `con_sobra`, que sin JavaScript hacen de «otro renglón»). `renglones` es
    `[{"descripcion", "detalle", "cantidad", "unidad", "precio_unitario", "monto"}]`, con los números como texto o
    `Decimal`; un renglón de antes del detalle (solo `monto`) se muestra como 1 × su monto."""
    hechos = []
    for r in renglones:
        cantidad, precio, monto = r.get("cantidad"), r.get("precio_unitario"), r.get("monto")
        if precio in (None, "") and monto not in (None, ""):
            cantidad, precio = 1, monto
        hechos.append({"descripcion": r.get("descripcion") or "", "detalle": r.get("detalle") or "",
                       "cantidad": _cantidad_escrita(cantidad), "unidad": r.get("unidad") or "",
                       "precio": _precio_escrito(precio),
                       "total": _total_escrito(cantidad, precio)})
    en_blanco = {"descripcion": "", "detalle": "", "cantidad": "", "unidad": "", "precio": "", "total": ""}
    faltan = max(RENGLONES_EN_BLANCO - len(hechos), 0) + (RENGLONES_DE_SOBRA if con_sobra else 0)
    return hechos + [dict(en_blanco) for _ in range(faltan)]


def valores_del_formulario(*, factura: dict | None = None, escrito: dict | None = None, nombre_del_cliente: str = "",
                           desde_la_opcion: list[dict] | None = None, desde_las_sesiones: list[dict] | None = None,
                           renglones_de_mas: list[dict] | None = None,
                           descuento_de_las_sesiones: Decimal | None = None,
                           hoy: date | None = None) -> dict:
    """Con qué se llenan las casillas. Manda lo ESCRITO (un guardado rechazado vuelve con lo que la persona puso),
    después la factura que se corrige (más `renglones_de_mas`, los que se ofrecieron agregar de una sesión recién
    ligada), después la opción de la cotización de la que nace, después un renglón por cada sesión o trabajo escogido
    (`desde_las_sesiones`), y si no hay nada, en blanco con la fecha de hoy y el nombre del cliente. El descuento sale
    de lo escrito, del que tiene la factura, o de la suma de los de las sesiones escogidas (`descuento_de_las_sesiones`,
    que ya viene sumada): la persona lo puede cambiar."""
    if escrito is not None:
        return {"para_nombre": escrito["para_nombre"], "fecha": escrito["fecha"],
                "itbis": escrito["itbis_pct"] is not None, "ncf": escrito["ncf"], "ncf_tipo": escrito["ncf_tipo"],
                "rnc_cedula": escrito["rnc_cedula"], "descuento": escrito["descuento"],
                "renglones": casillas_de_renglones(escrito["renglones"])}
    if factura is not None:
        return {"para_nombre": factura["para_nombre"], "fecha": _iso(factura["fecha"]),
                "itbis": factura["itbis_pct"] is not None, "ncf": factura["ncf"] or "",
                "ncf_tipo": factura["ncf_tipo"] or "", "rnc_cedula": factura["rnc_cedula"] or "",
                "descuento": _descuento_escrito(factura.get("descuento")),
                "renglones": casillas_de_renglones(list(factura["renglones"]) + list(renglones_de_mas or []))}
    return {"para_nombre": nombre_del_cliente, "fecha": _iso(hoy) if hoy else "", "itbis": False, "ncf": "",
            "ncf_tipo": "", "rnc_cedula": "", "descuento": _descuento_escrito(descuento_de_las_sesiones),
            "renglones": casillas_de_renglones(desde_la_opcion or desde_las_sesiones or [])}


def _iso(fecha) -> str:
    return fecha.isoformat() if isinstance(fecha, date) else str(fecha or "")


def renglones_de_una_opcion(cotizacion: dict | None, opcion) -> list[dict]:
    """Los renglones de la opción `opcion` (1, 2…) de la cotización, con su descripción, su detalle y su total
    como monto; `[]` si la cotización o la opción no están. Es solo un punto de partida que la persona corrige:
    la puerta vuelve a validar todo al guardar."""
    if not cotizacion or isinstance(opcion, bool) or not isinstance(opcion, int):
        return []
    opciones = (cotizacion.get("contenido") or {}).get("opciones") or []
    if not 1 <= opcion <= len(opciones):
        return []
    return [{"descripcion": r.get("descripcion", ""), "detalle": r.get("detalle", ""),
             "monto": Decimal(r["total"]) if r.get("total") not in (None, "") else ""}
            for r in opciones[opcion - 1].get("renglones", [])]


# ── Las sesiones o trabajos que se ligan ─────────────────────────────────────────────────────────

# Por qué una sesión que la búsqueda trae no se puede escoger (se pinta en su lugar de la casilla).
POR_QUE_NO_SE_PUEDE_ESCOGER = {
    "ajena": "No está entre las sesiones de este cliente en el registro: no se puede escoger",
    "sin_comprobar": "No se pudo comprobar de quién es: no se puede escoger",
}

def candidata_de_la_busqueda(c: dict, lectura: dict | None) -> dict:
    """Una sesión que la BÚSQUEDA trajo y no está entre las de la ficha, con su marca. ES LA ÚNICA REGLA (la usan «Nueva
    factura» y la página de una factura). `lectura` es la de la ficha (`registro_lectura.sesiones_de_cliente`) o `None`.
    Si la ficha se leyó bien: la que la App marcó cancelada PARA ESA FICHA es del cliente y sale como cancelada (la ficha no
    la lista, pero sí la nombra en `canceladas`); cualquier otra NO ESTÁ entre las de la ficha (`ajena`): puede ser de otra persona o de este cliente y la ficha no la lista (por ejemplo un trabajo sin su teléfono), el código solo sabe que no está. Si la ficha no se leyó no se sabe
    de quién es ninguna (`sin_comprobar`). Con `no_se_puede` no hay casilla ni botón."""
    if lectura is not None and lectura.get("estado") == "ok":
        if c["ref"] in set(lectura.get("canceladas") or []):
            return dict(c, cancelada=True, no_se_puede="")
        return dict(c, no_se_puede=POR_QUE_NO_SE_PUEDE_ESCOGER["ajena"])
    return dict(c, no_se_puede=POR_QUE_NO_SE_PUEDE_ESCOGER["sin_comprobar"])


MOTIVOS_DE_FICHA = {
    "sin_telefono": "la ficha no tiene teléfono",
    "ficha_inexistente": "la ficha no existe en el registro",
    "sin_persona": "no se le dijo al registro de quién",
}


def ligas_de_las_refs(refs: list[str], respuesta: dict) -> tuple[list[dict], list[dict]]:
    """`(ligas, problemas)` de las referencias escogidas, a partir de lo que la App CONTESTÓ por la FICHA DEL CLIENTE
    DE LA FACTURA (`registro_lectura.sesiones_de_cliente(cliente)`, SIN pedir sesiones sueltas).

    ESA ES LA COMPROBACIÓN DE QUE LA SESIÓN ES DEL CLIENTE: la App, cuando se le pide `&sesion=<ref>`, devuelve esa
    sesión sea de quien sea y la mezcla con las de la ficha (no hay forma de separarlas en la respuesta), y `no_halladas`
    solo dice que la fila no existe. Por eso aquí solo vale lo que la App listó para la ficha: una referencia que no está
    en esa lista es `ajena` y NO se liga. Si la ficha no se pudo leer o la App no la liga (`estado` distinto de `ok`),
    ninguna referencia se puede comprobar y ninguna se liga (`sin_ficha`, con el motivo que dio la App).

    EL TIPO NO LO PONE QUIEN LLENA EL FORMULARIO: es `trabajo` si la App dice `es_trabajo` y `sesion` si no. Cada
    liga es `{"tipo", "ref", "codigo"}`, en el orden en que se escogieron y sin repetir. Un problema es
    `{"ref", "motivo"}`: `cancelada` (la App la devolvió cancelada o pospuesta: no se factura), `ajena` (no está entre las
    del cliente) o `sin_ficha` (con `porque`). `respuesta` sin la lectura (`no_se_pudo`) la trata quien llama."""
    por_ref = {r["ref"]: r for r in respuesta.get("sesiones", []) + respuesta.get("trabajos", [])}
    canceladas = set(respuesta.get("canceladas") or [])
    estado = respuesta.get("estado")
    porque = MOTIVOS_DE_FICHA.get(respuesta.get("motivo") or "", "") if estado == "sin_ligar" else ""
    ligas, problemas, vistas = [], [], set()
    for ref in refs:
        if ref in vistas:
            continue
        vistas.add(ref)
        if estado != "ok":
            problemas.append({"ref": ref, "motivo": "sin_ficha", "estado": estado, "porque": porque})
        elif ref in canceladas:
            problemas.append({"ref": ref, "motivo": "cancelada"})
        elif ref not in por_ref:
            problemas.append({"ref": ref, "motivo": "ajena"})
        else:
            r = por_ref[ref]
            ligas.append({"tipo": "trabajo" if r["es_trabajo"] is True else "sesion", "ref": ref,
                          "codigo": r.get("codigo")})
    return ligas, problemas


def frase_de_una_liga_que_no_se_pudo(liga: dict, error: dict) -> str:
    """Una sesión o trabajo que NO se ligó cuando el borrador YA se había guardado: dice cuál y por qué."""
    return f"No se pudo ligar «{liga.get('codigo') or liga['ref']}» a la factura: {error['razon']}"


def frase_del_problema_de_liga(p: dict) -> str:
    """Lo que se le dice a la persona de una sesión o trabajo que NO se pudo ligar."""
    quien = f"«{p['codigo']}»" if p.get("codigo") else f"La sesión o trabajo ref. {p['ref']}"
    if p["motivo"] == "cancelada":
        return f"{quien} está cancelado o pospuesto: no se factura."
    if p["motivo"] == "ajena":
        return (f"{quien} no aparece entre las sesiones y trabajos de este cliente en el registro: no se ligó. "
                "Una factura solo se liga a lo que es de su cliente.")
    if p["motivo"] == "sin_ficha":
        if p.get("estado") == "sin_cliente":
            return (f"{quien} no se pudo comprobar: la factura no tiene un cliente con ficha en el registro, y sin eso no se "
                    "sabe de quién es. No se ligó.")
        razon = f" ({p['porque']})" if p.get("porque") else ""
        return (f"{quien} no se pudo comprobar: el registro no dio las sesiones de la ficha de este cliente{razon}, y sin "
                "eso no se sabe de quién es. No se ligó.")
    return f"{quien} ya no está en el registro: no se ligó."


# ── Los renglones que salen de las sesiones, y lo que la página dice de cada una ───────────────────

def _dinero_que_cabe(total) -> Decimal | None:
    """El total de la sesión como monto de renglón, o None si la App no lo trae, es cero o lleva más de dos decimales
    (la puerta lo rechazaría: no se redondea, la persona lo escribe)."""
    if not isinstance(total, Decimal) or not total.is_finite() or total <= 0:
        return None
    return total if total == total.quantize(Decimal("0.01")) else None


def renglon_de_una_sesion(r: dict) -> dict:
    """El renglón de RESPALDO de UNA sesión o trabajo (`r` es un renglón de `registro_lectura`): un solo renglón con
    el `total` que la App tiene (sin ITBIS: el ITBIS lo suma la factura), cantidad 1 y sin unidad. Se usa cuando la
    cuenta de la sesión no se pudo leer, no trae renglones o no cuadra. Sin total que sirva el precio queda vacío y
    `sin_total` verdadero: la persona lo escribe. Es solo un punto de partida; la puerta vuelve a validar al guardar."""
    partes = []
    if r.get("sala"):
        partes.append(r["sala"])
    if r.get("fecha"):
        partes.append(cz.fecha_corta(r["fecha"]))
    if r.get("horas"):
        partes.append(f"{r['horas']} h")
    monto = _dinero_que_cabe(r.get("total"))
    return {"descripcion": (r.get("concepto") or ("Trabajo" if r.get("es_trabajo") else "Sesión"))[:db.LARGOS_DE_FACTURA["descripcion"]],
            "detalle": " · ".join(partes)[:db.LARGOS_DE_FACTURA["detalle"]],
            "cantidad": 1, "unidad": "",
            "precio_unitario": "" if monto is None else monto, "monto": "" if monto is None else monto,
            "sin_total": monto is None, "sin_detalle": "", "aviso_del_detalle": "",
            "codigo": r.get("codigo"), "ref": r.get("ref")}


def _numero(valor) -> Decimal | None:
    """Un número de la cuenta (`cantidad`, `horas`, `precio_unitario`, `subtotal`) como `Decimal`, o None si no vino
    o no es un número finito. La App los manda como número de JSON; `registro_lectura` ya los pasa a `Decimal`."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        return None
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    numero = Decimal(str(valor))
    return numero if numero.is_finite() else None


def _detalle_de_la_cuenta(r: dict) -> str:
    """La descripción de un renglón que sale de la cuenta: la sala y la fecha de la sesión (las horas del renglón
    ya van en su cantidad)."""
    partes = [p for p in (r.get("sala"), cz.fecha_corta(r["fecha"]) if r.get("fecha") else None) if p]
    return " · ".join(partes)[:db.LARGOS_DE_FACTURA["detalle"]]


def _renglon_de_una_cuenta(r: dict, x: dict) -> dict | None:
    """Un renglón de la factura a partir de UN renglón de la cuenta de la sesión (`x`, tal como lo manda la App), o
    None si ese renglón no se puede cobrar (sin subtotal, sin cantidad o sin precio).

    El total del renglón TIENE que dar el `subtotal` de NocoDB, que es lo que suma el total de la sesión. Con
    `tipo_cobro` `hora` la cantidad es `cantidad × horas` y la unidad «horas»; si no, la cantidad del renglón y sin
    unidad (la App no guarda el nombre de la unidad: no se inventa «canciones»). Si la cuenta no da (por el
    redondeo o por horas raras) se usa 1 × el subtotal y se avisa en `aviso_del_detalle`."""
    subtotal = _dinero_que_cabe(_numero(x.get("subtotal")))
    cantidad, horas = _numero(x.get("cantidad")), _numero(x.get("horas"))
    precio = _numero(x.get("precio_unitario"))
    if subtotal is None or cantidad is None or cantidad <= 0 or precio is None or precio <= 0:
        return None
    titulo = (x.get("titulo") or "").strip() or (r.get("concepto") or "")
    titulo = (titulo or ("Trabajo" if r.get("es_trabajo") else "Sesión"))[:db.LARGOS_DE_FACTURA["descripcion"]]
    if x.get("tipo_cobro") == "hora":
        unidades, unidad = (cantidad * horas if horas is not None and horas > 0 else None), "horas"
    else:
        unidades, unidad = cantidad, ""
    if unidades is not None and db.producto_exacto_del_renglon(unidades, precio) == subtotal:
        return {"descripcion": titulo, "detalle": _detalle_de_la_cuenta(r), "cantidad": unidades,
                "unidad": unidad, "precio_unitario": precio, "monto": subtotal, "sin_total": False,
                "sin_detalle": "", "aviso_del_detalle": "", "codigo": r.get("codigo"), "ref": r.get("ref")}
    return {"descripcion": titulo, "detalle": _detalle_de_la_cuenta(r), "cantidad": 1, "unidad": "",
            "precio_unitario": subtotal, "monto": subtotal, "sin_total": False, "sin_detalle": "",
            "codigo": r.get("codigo"), "ref": r.get("ref"),
            "aviso_del_detalle": (f"«{titulo}»: la cantidad y el precio del registro no dan su subtotal; "
                                  f"se puso 1 × {cz.dinero_en_pesos(subtotal)}.")}


def renglones_de_una_sesion(r: dict) -> list[dict]:
    """Los renglones de la factura que salen de UNA sesión o trabajo: uno por cada renglón de su cuenta (`renglones`,
    pieza D1 de la App), en su orden. Si la cuenta no se pudo leer (`renglones` null), está vacía, o alguno de sus
    renglones no se puede cobrar, cae a UN renglón con el total de la sesión (`renglon_de_una_sesion`) y lo dice:
    `sin_detalle` lleva el motivo que dio la App (`cuenta_no_se_pudo_leer`) y `aviso_del_detalle`, lo que pasó."""
    cuenta = r.get("renglones")
    if isinstance(cuenta, list) and cuenta:
        hechos = []
        for x in cuenta:
            hecho = _renglon_de_una_cuenta(r, x) if isinstance(x, dict) else None
            if hecho is None:
                hechos = []
                break
            hechos.append(hecho)
        if hechos:
            return hechos
        aviso = ("Un renglón de la cuenta no se puede cobrar (sin cantidad, sin precio o sin subtotal): va un solo "
                 "renglón con el total de la sesión.")
    elif isinstance(cuenta, list):
        aviso = ""                                    # la cuenta está vacía: manda el total de la sesión
    else:
        aviso = ""                                    # sin detalle: la App de antes de D1, o la cuenta que no se leyó
    unico = renglon_de_una_sesion(r)
    if cuenta is None:
        unico["sin_detalle"] = r.get("renglones_motivo") or ""
    unico["aviso_del_detalle"] = aviso
    return [unico]


def renglones_de_las_sesiones(filas: list[dict]) -> list[dict]:
    """Los renglones de todas las sesiones o trabajos, en el orden en que se escogieron (y dentro de cada uno, el de
    su cuenta)."""
    return [renglon for r in filas for renglon in renglones_de_una_sesion(r)]


def ligas_con_lo_leido(ligas: list[dict], lectura: dict) -> list[dict]:
    """Cada liga viva con lo que la App dice de ella AHORA (la página lo lee en el momento; nada de esto se guarda):
    `leida` es `ok` (con `fila`, el renglón de `registro_lectura`), `cancelada`, `no_esta` (la App ya no la devuelve) o
    `no_se_pudo` (la App no contestó: se dice, no se disfraza de otra cosa). `lectura` es la respuesta de
    `registro_lectura.sesiones_de_cliente` pidiendo esas referencias."""
    if lectura.get("estado") == "no_se_pudo":
        return [dict(l, leida="no_se_pudo", fila=None) for l in ligas]
    por_ref = {r["ref"]: r for r in lectura.get("sesiones", []) + lectura.get("trabajos", [])}
    canceladas = set(lectura.get("canceladas") or [])
    salida = []
    for l in ligas:
        if l["ref"] in por_ref:
            fila = por_ref[l["ref"]]
            salida.append(dict(l, leida="ok", fila=dict(fila, dinero_texto=_dinero_de_la_fila(fila))))
        elif l["ref"] in canceladas:
            salida.append(dict(l, leida="cancelada", fila=None))
        else:
            salida.append(dict(l, leida="no_esta", fila=None))
    return salida


def _dinero_de_la_fila(fila: dict) -> dict:
    """Total, abonado y saldo de una sesión, escritos con el dinero de la hoja; «—» donde la App no trajo la cifra."""
    return {k: ("—" if fila.get(k) is None else cz.dinero_en_pesos(fila[k])) for k in ("total", "abonado", "saldo")}


# ── El pago, leído de la App (pieza 7, 21-oct-2026) ─────────────────────────────────────────────────
#
# EL PAGO SE ANOTA EN UN SOLO SITIO: LA APP, EN CADA SESIÓN (diseño §7, y Tiziano el 20-oct-2026: «Claro, si alguien paga,
# en la sesión tiene que aparecer eso»). La factura NO guarda ningún pago ni tiene botón de pago: cada vez que se pinta
# lee de la App lo que se ha pagado de sus sesiones y trabajos ligados vivos. Nada de esto se guarda.
#
# CUÁNDO ESTÁ PAGADA (la regla aprobada, corregida tres veces el 21-oct-2026 por los NO PASA del testigo): cuando TODAS
# sus sesiones y trabajos ligados vivos —sin contar los cancelados— tienen el `saldo` que da la App en cero o menos. Se
# mira SESIÓN POR SESIÓN, SIN COMPENSAR: lo que una sesión pagó de más no tapa lo que debe otra (el testigo midió una de
# RD$1,000 que debía y otra que había pagado RD$1,000 de más; la suma daba cero y la factura decía «Pagada»). Lo que se
# muestra como pagado es la suma del `abonado`. Si TODAS las sesiones contadas tienen total cero **y no hay nada
# abonado**, la factura dice «Sin cargos en el registro» (no hay nada que cobrar ni nada pagado); si algo se abonó, manda
# la regla de siempre y el detalle dice cuánto se pagó (el testigo midió una sesión de total cero con RD$100 abonados que
# salía «Sin cargos» y no decía el pago). Mientras alguna sesión deba, el texto dice cuánto FALTA, que es la suma de los
# saldos POSITIVOS sin restarle lo que otra pagó de más: «Pagado RD$X · falta RD$Y» (o «Sin pagos · falta RD$Y»), para
# que nunca se lea «Pagado RD$1,500 de RD$1,500» con una sesión debiendo. (La primera versión medía «pagada» contra el
# subtotal de la factura; también fue un NO PASA del testigo.)
#
# EL AVISO DE «NO CUADRA» ES OTRA COSA Y NO CAMBIA EL ESTADO: cuando el subtotal de la factura (sin ITBIS) no es igual a
# la suma de los `total` de las sesiones —porque se cambió un renglón a mano o porque la factura lleva un renglón que no
# es de una sesión—, la factura muestra las dos cifras y dice «no cuadra con la App: revísala» (diseño §7). Quien decide
# si está pagada son las sesiones, no la factura.
#
# EL ITBIS NO CUENTA para nada de esto: por la respuesta A de Tiziano (21-oct-2026) no pasa por la App —se cobra
# aparte— y la pantalla lo dice con sus palabras.

TEXTO_SIN_LEER_EL_PAGO = "No se pudo leer lo pagado."

PAGO_QUE_NO_SE_PINTA = {"estado": "no_aplica", "texto": "", "detalle": "", "itbis": ""}


def _pago_sin_leer(itbis: str, motivo: str) -> dict:
    """El pago cuando no se puede decir cuánto se ha pagado: se dice, nunca se disfraza de «sin pagos»."""
    return {"estado": "no_se_pudo", "texto": TEXTO_SIN_LEER_EL_PAGO, "detalle": motivo, "itbis": itbis}


def _aviso_de_lo_que_no_cuadra(f: dict, ligas_leidas: list[dict], total_de_las_sesiones) -> str:
    """La frase de cuando la factura y la App no dicen lo mismo del dinero de las sesiones, o `""`.

    Compara el subtotal de la factura (sin ITBIS) con la suma de los `total` que la App tiene de sus sesiones ligadas. Solo
    se afirma cuando se leyeron TODAS las ligas (con una cancelada o una que ya no está, la comparación no está completa y
    no se dice nada: la página ya nombra cada una). Es un aviso: NO cambia el estado del pago.
    """
    if not all(l["leida"] == "ok" for l in ligas_leidas) or total_de_las_sesiones == f["subtotal"]:
        return ""
    return (f"No cuadra con la App: revísala (la factura suma {cz.dinero_en_pesos(f['subtotal'])} y el registro dice "
            f"{cz.dinero_en_pesos(total_de_las_sesiones)} de sus sesiones).")


def el_pago_de_la_factura(f: dict, ligas_leidas: list[dict] | None) -> dict:
    """Lo que la factura dice de su pago, leído de lo que la App contestó AHORA (`ligas_leidas`, lo que devuelve
    `ligas_con_lo_leido`): `{"estado", "texto", "detalle", "itbis"}`.

    `estado` es `pagada` (TODAS las sesiones y trabajos ligados vivos tienen `saldo` cero o menos: se mira una por una,
    sin compensar), `sin_cargos` («Sin cargos en el registro»: todas las contadas tienen total cero y no hay nada
    abonado), `con_abono` («Pagado RD$X · falta RD$Y»: algo pagado y alguna todavía debe), `sin_pagos` («Sin pagos ·
    falta RD$Y»: nada pagado y alguna debe), `no_se_pudo` (no se puede decir cuánto se ha pagado: la App no contestó, ya
    no devuelve una de sus sesiones o no trajo una cifra: se dice y no se adivina) o `no_aplica` (la factura está
    anulada: una anulada no tiene estado de pago). «Falta» es la suma de los saldos POSITIVOS, sin restarle lo que otra
    sesión pagó de más. `detalle` es lo que hay que decir además (el porqué de un `no_se_pudo`, cuánto se pagó cuando
    quedó pagada, las dos cifras cuando la App y la factura no cuadran, o que una sesión pagó de más y eso no tapa lo que
    debe otra) e `itbis`, la frase del ITBIS cuando la factura lo lleva (con comprobante dice que se retiene completo,
    no que se cobra aparte: pieza D4).

    `ligas_leidas` en `None` quiere decir que no se pudo leer ni de qué sesiones es la factura. Nada de esto se guarda.
    """
    if f["estado"] == "anulada":
        return dict(PAGO_QUE_NO_SE_PINTA)
    if f["itbis_pct"] is None:
        itbis = ""
    elif f.get("ncf"):
        # Con comprobante el ITBIS no se le cobra al cliente: se retiene completo (pieza D4). Decir «se cobra
        # aparte» mandaría a cobrarle un ITBIS que él retiene.
        itbis = (f"Con comprobante, el ITBIS ({cz.dinero_en_pesos(f['itbis'])}) se retiene completo y no se le cobra "
                 f"al cliente: paga {cz.dinero_en_pesos(f['total_a_pagar'])}.")
    else:
        itbis = f"El ITBIS ({cz.dinero_en_pesos(f['itbis'])}) se cobra aparte; no se lleva en la App."
    if ligas_leidas is None:
        return _pago_sin_leer(itbis, "No se pudieron leer las sesiones de la factura.")
    if any(l["leida"] == "no_se_pudo" for l in ligas_leidas):
        return _pago_sin_leer(itbis, "El registro no contestó.")
    if any(l["leida"] == "no_esta" for l in ligas_leidas):
        return _pago_sin_leer(itbis, "El registro ya no devuelve una de sus sesiones o trabajos.")
    pagado, total_de_las_sesiones, falta, contadas = Decimal("0"), Decimal("0"), Decimal("0"), 0
    todas_saldadas, todas_sin_cargo, alguna_pago_de_mas = True, True, False
    for l in ligas_leidas:
        if l["leida"] == "cancelada":
            # Una cancelada no se cobra ni se paga: no entra en ninguna de las cifras (la página la nombra aparte).
            continue
        fila = l["fila"] or {}
        abonado, total, saldo = fila.get("abonado"), fila.get("total"), fila.get("saldo")
        if abonado is None or total is None or saldo is None:
            return _pago_sin_leer(itbis, "El registro no dijo cuánto cuesta, cuánto se ha pagado o cuánto debe una de "
                                         "sus sesiones.")
        if abonado < 0 or total < 0:
            # Un `saldo` negativo NO es un error: es lo que debe menos quien pagó de más.
            return _pago_sin_leer(itbis, "El registro trajo una cifra de dinero que no se entiende.")
        pagado += abonado
        total_de_las_sesiones += total
        contadas += 1
        todas_saldadas = todas_saldadas and saldo <= 0
        todas_sin_cargo = todas_sin_cargo and total == 0
        alguna_pago_de_mas = alguna_pago_de_mas or saldo < 0
        if saldo > 0:
            # LO QUE FALTA ES LA SUMA DE LOS SALDOS POSITIVOS, sin restarle lo que otra sesión pagó de más.
            falta += saldo
    if not contadas:
        estado, texto = "sin_pagos", "Sin pagos"
    elif todas_sin_cargo and pagado == 0:
        # Sin precio en el registro y sin un peso abonado: no hay nada que cobrar. Si algo se abonó, manda la regla de
        # abajo (una sesión con total cero y algo pagado tiene el saldo en negativo y cuenta como pagada).
        estado, texto = "sin_cargos", "Sin cargos en el registro."
    elif todas_saldadas:
        estado, texto = "pagada", "Pagada"
    elif pagado > 0:
        estado, texto = "con_abono", (f"Pagado {cz.dinero_en_pesos(pagado)} · falta "
                                      f"{cz.dinero_en_pesos(falta)}")
    else:
        estado, texto = "sin_pagos", f"Sin pagos · falta {cz.dinero_en_pesos(falta)}"
    aviso = _aviso_de_lo_que_no_cuadra(f, ligas_leidas, total_de_las_sesiones)
    dichos = [f"Se pagaron {cz.dinero_en_pesos(pagado)}."] if estado == "pagada" and pagado > 0 else []
    if aviso:
        dichos.append(aviso)
    if alguna_pago_de_mas and estado == "con_abono":
        # El que pagó de más no tapa la deuda de otra sesión: la factura no está pagada, y se dice por qué.
        dichos.append("Una de sus sesiones pagó de más: eso no tapa lo que debe otra.")
    return {"estado": estado, "texto": texto, "detalle": " ".join(dichos), "itbis": itbis}


# ── Lo que la puerta rechazó, en palabras ───────────────────────────────────────────────────────

# Las razones que la pantalla conoce de verdad, por la CLAVE con la que la puerta rechaza. Lo que no esté aquí no
# se calla ni se inventa: se dice con el mensaje de la propia puerta (que no repite lo que se escribió) en
# `texto_del_error`. Una prueba saca las claves de `db/db.py` (no de esta lista) y exige que cada una salga con
# alguna frase.
TEXTO_POR_CLAVE = {
    "sesion_ya_facturada": "Esa sesión o ese trabajo ya está en otra factura (borrador o emitida). Si esa factura se anula, queda libre.",
    "ya_ligada": "Esa sesión o ese trabajo ya está en esta factura.",
    "ultima_liga": "La factura no puede quedar sin sesión ni trabajo: liga primero la otra y luego suelta ésta.",
    "liga_no_esta": "Esa sesión o ese trabajo ya no está ligado a la factura.",
    "sin_liga": "La factura necesita al menos una sesión o un trabajo.",
    "liga_forma": "La factura nace ligada a una sesión o a un trabajo: escoge al menos uno.",
    "ncf_repetido": "Ese NCF ya lo tiene otra factura (una anulada también cuenta: su NCF no se vuelve a usar).",
    "ncf_incompleto": "El NCF y su tipo van juntos: escribe los dos o deja los dos vacíos.",
    "ncf_formato": "El NCF solo lleva letras y cifras, de 1 a 20.",
    "rnc_formato": "El RNC lleva 9 cifras y la cédula 11.",
    "itbis": "El ITBIS es el 18 % o ninguno.",
    "sin_renglones": "La factura necesita al menos un renglón con su descripción y su monto.",
    "renglones_muchos": "La factura lleva demasiados renglones.",
    "renglon_forma": "Cada renglón lleva descripción y monto, o su cantidad con su precio unitario.",
    "cantidad_vacio": "A un renglón le falta la cantidad.",
    "cantidad_tipo": "La cantidad de un renglón tiene que ser un número.",
    "cantidad_formato": "Una cantidad no se entiende; escríbela como 2 o 1.5.",
    "cantidad_decimales": "Una cantidad lleva más de tres decimales.",
    "cantidad_cero": "Cada cantidad tiene que ser mayor que cero.",
    "cantidad_alta": "Una cantidad es demasiado grande.",
    "unidad_largo": "La unidad de un renglón es demasiado larga (hasta 20 caracteres).",
    "monto_vacio": "A un renglón le falta el monto.",
    "monto_cero": "Cada monto tiene que ser mayor que cero.",
    "monto_alto": "Un monto es demasiado grande.",
    "monto_decimales": "Un monto lleva más de dos decimales.",
    "monto_redondeo": "El total de un renglón no da en centavos exactos: cambia la cantidad o el precio unitario.",
    "monto_formato": "Un monto no se entiende; escríbelo como 62,500 o 62500.50.",
    "monto_tipo": "Un monto no se entiende; escríbelo como 62,500 o 62500.50.",
    "descuento_mayor": "El descuento no puede pasar del subtotal de la factura.",
    "descuento_negativo": "El descuento no puede ser negativo.",
    "descuento_alto": "El descuento es demasiado grande.",
    "descuento_decimales": "El descuento lleva más de dos decimales.",
    "descuento_formato": "El descuento no se entiende; escríbelo como 500 o 500.50.",
    "descuento_tipo": "El descuento no se entiende; escríbelo como 500 o 500.50.",
    "descripcion_vacio": "A un renglón le falta la descripción.",
    "descripcion_largo": "La descripción de un renglón es demasiado larga.",
    "detalle_largo": "El detalle de un renglón es demasiado largo.",
    "motivo_vacio": "Escribe el motivo de la anulación.",
    "motivo_largo": "El motivo de la anulación es demasiado largo (hasta 500 caracteres).",
    "para_nombre_vacio": "Falta el nombre de la persona a quien va la factura.",
    "para_nombre_largo": "El nombre de la persona a quien va la factura es demasiado largo.",
    "ncf_tipo_vacio": "El NCF y su tipo van juntos: escribe los dos o deja los dos vacíos.",
    "ncf_tipo_largo": "El tipo de NCF es demasiado largo.",
    "fecha": "La fecha no se entiende.",
    "fuera_de_latin1": "Hay un carácter que el PDF no puede escribir (por ejemplo un emoji): quítalo.",
    "caracteres": "Hay caracteres que no se pueden guardar (por ejemplo un salto de línea donde no cabe).",
    "cotizacion_de_otro_proyecto": "Esa cotización es de otro proyecto.",
    "opcion_no_esta": "Esa opción no está en la cotización.",
    "opcion_sin_cotizacion": "La opción de una cotización necesita la cotización.",
    "tiene_envios": "Un borrador con un envío esperando respuesta no se descarta.",
    "candado": "Falta saber qué versión de la factura estabas viendo: ábrela otra vez.",
    "sin_cambios": "No hay nada que cambiar.",
}

# Qué casilla marca cada razón (las que son de UNA casilla). Se busca por el comienzo de la clave.
CASILLA_POR_COMIENZO = (("ncf", "ncf"), ("rnc", "rnc_cedula"), ("para_nombre", "para_nombre"), ("fecha", "fecha"),
                        ("itbis", "itbis"), ("descuento", "descuento"))


def casilla_del_error(clave: str | None) -> str | None:
    if not clave:
        return None
    for comienzo, casilla in CASILLA_POR_COMIENZO:
        if clave.startswith(comienzo):
            return casilla
    return None


def texto_del_error(e: Exception) -> dict:
    """`{"clave", "razon", "texto", "casilla"}` de lo que rechazó un guardado (`razon` es la causa sola, `texto` la
    causa y que no se guardó nada). NUNCA calla: una razón que la pantalla no
    conoce sale con el mensaje de la propia puerta. Siempre dice que NO se guardó nada, salvo cuando no se sabe
    (un fallo de la base). `FacturaSinSesion` no llega aquí: la ruta contesta 401."""
    if isinstance(e, db.FacturaNoVale):
        cuerpo = TEXTO_POR_CLAVE.get(e.clave) or f"No se pudo: {str(e).rstrip('.')}."
        return {"clave": e.clave, "razon": cuerpo, "texto": f"{cuerpo} NO se guardó nada.",
                "casilla": casilla_del_error(e.clave)}
    if isinstance(e, db.FacturaYaNoSeCorrige):
        por_estado = {"emitida": "Una factura emitida no se corrige: si está mal, se anula y se hace otra.",
                      "anulada": "Una factura anulada ya no se toca.",
                      "borrador": "Esa acción no es para un borrador."}
        texto = por_estado.get(e.estado) or f"Esa factura está «{e.estado}» y no admite eso."
        return {"clave": f"ya_no_se_corrige_{e.estado}", "razon": texto, "texto": f"{texto} NO se guardó nada.",
                "casilla": None}
    if isinstance(e, db.FacturaCambio):
        razon = "La factura cambió mientras la mirabas."
        return {"clave": "cambio", "casilla": None, "razon": razon,
                "texto": f"{razon} Lo que hiciste NO se guardó. Abajo está como quedó."}
    if isinstance(e, db.FacturaNoEsta):
        razon = "Esa factura, ese proyecto o esa cotización ya no está."
        return {"clave": "no_esta", "casilla": None, "razon": razon, "texto": f"{razon} NO se guardó nada."}
    if isinstance(e, db.FacturasSinTabla):
        razon = "Las facturas todavía no se pueden guardar: falta actualizar la base de datos."
        return {"clave": "sin_tabla", "casilla": None, "razon": razon, "texto": f"{razon} NO se guardó nada."}
    razon = "La base de datos falló y no se pudo confirmar si se guardó."
    return {"clave": "base", "casilla": None, "razon": razon,
            "texto": f"{razon} Mira cómo quedó antes de volver a intentarlo."}


# ── Quién la hizo y cómo van sus envíos (pieza 9, 21-oct-2026) ───────────────────────────────────────

def quien_hizo_la_factura(f: dict, nombres: dict) -> str:
    """«Natalia», el nombre de la persona de la casa que la hizo, o «Alguien de la casa» si ese chat no tiene nombre
    (nunca el número de chat). Sale de lo que la base guarda (`hecha_por`, `hecha_por_chat_id`)."""
    if f["hecha_por"] == "natalia":
        return "Natalia"
    return nombres.get(f["hecha_por_chat_id"]) or "Alguien de la casa"


_CANAL_EN_PALABRAS = {"whatsapp": "WhatsApp", "correo": "correo", "mano": "a mano"}


def _dia_del_envio(valor) -> str:
    dia = db.dia_rd(valor)
    return cz.fecha_corta(dia) if dia is not None else ""


def frase_del_envio(e: dict, revisa: str) -> dict:
    """`{"estado", "etiqueta", "frase"}` de UN envío de `db.envios_de_factura`: lo que la base tiene, en palabras, y nada
    más. «Enviada» solo se dice de un envío `enviado`, que solo lo pone Natalia (cuando lo anota) o la casa (a mano);
    uno pendiente o aprobado nunca se dice mandado. `revisa` es el nombre de quien revisa (para «esperando a …»)."""
    canal = _CANAL_EN_PALABRAS.get(e["canal"], str(e["canal"]))
    estado = e["estado"]
    motivo = f": {e['motivo']}" if e.get("motivo") else ""
    if estado == "pendiente":
        return {"estado": estado, "etiqueta": "Esperando revisión",
                "frase": f"Natalia pidió mandarla por {canal}. Está esperando que {revisa} la revise en Telegram."}
    if estado == "aprobado":
        return {"estado": estado, "etiqueta": "Aprobada",
                "frase": f"Aprobada para mandarla por {canal}. Natalia todavía no anotó que salió."}
    if estado == "rechazado":
        return {"estado": estado, "etiqueta": "Rechazada", "frase": f"Rechazada{motivo}. No se mandó."}
    if estado == "enviado" and e["canal"] == "mano":
        return {"estado": estado, "etiqueta": "Mandada a mano",
                "frase": f"Marcada como mandada a mano el {_dia_del_envio(e['enviado_en'])}."}
    if estado == "enviado":
        return {"estado": estado, "etiqueta": "Enviada",
                "frase": f"Natalia anotó que la mandó por {canal} el {_dia_del_envio(e['enviado_en'])}."}
    return {"estado": estado, "etiqueta": "No salió", "frase": f"No salió por {canal}{motivo}."}

