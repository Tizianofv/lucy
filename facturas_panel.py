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

import re
from datetime import date, datetime
from decimal import Decimal

import cotizaciones as cz
import db.db as db

# Cuántos renglones en blanco se dibujan (sin JavaScript no hay botón de «otro renglón»). Es una decisión de la
# pantalla, no una medición.
RENGLONES_EN_BLANCO = 3
RENGLONES_DE_SOBRA = 2

_RE_RENGLON = re.compile(r"^r-([0-9]{1,3})-(descripcion|detalle|monto)$")

ETIQUETA_DE_ESTADO = {"borrador": "Borrador", "emitida": "Emitida", "anulada": "Anulada"}
ETIQUETA_DEL_FILTRO = {"borrador": "Borradores", "emitida": "Emitidas", "anulada": "Anuladas"}
ETIQUETA_DE_LIGA = {"sesion": "Sesión", "trabajo": "Trabajo"}


def estado_pedido(texto) -> str | None:
    """El estado por el que se filtra la lista: uno de los de la base, o `None` (todas). Lo que no se conoce es
    «todas», nunca un filtro inventado."""
    return texto if isinstance(texto, str) and texto in ETIQUETA_DE_ESTADO else None


def etiqueta_del_estado(estado) -> str:
    return ETIQUETA_DE_ESTADO.get(estado, str(estado))


def fila_de_la_lista(f: dict, nombres_de_proyecto: dict[int, str]) -> dict:
    """Una fila de la lista: lo que la plantilla pinta, ya escrito. `f` es una fila de
    `db.facturas_para_la_lista`. El dinero lo escribe `cotizaciones.dinero_en_pesos` (el mismo de la hoja)."""
    return {
        "id": f["id"],
        "numero": f["codigo"] or "Borrador",
        "es_borrador": f["estado"] == "borrador",
        "cliente": f["para_nombre"],
        "fecha": cz.fecha_corta(f["fecha"]),
        "total": cz.dinero_en_pesos(f["total"]),
        "estado": f["estado"],
        "estado_texto": etiqueta_del_estado(f["estado"]),
        "proyecto_id": f["proyecto_id"],
        "proyecto": (None if f["proyecto_id"] is None
                     else nombres_de_proyecto.get(f["proyecto_id"]) or f"Proyecto {f['proyecto_id']}"),
        "sesiones": f["ligas_vivas"],
    }


# ── Lo que llega del formulario ───────────────────────────────────────────────────────────────────

def texto_de(valor) -> str:
    """Un campo que el POST trajo: solo un texto cuenta (un archivo subido o cualquier otra cosa es «nada»)."""
    return valor if isinstance(valor, str) else ""


def _entero_o_eltexto_de(valor):
    """Vacío es «ninguno» (`None`); dígitos, el entero; cualquier otra cosa se deja como texto para que la puerta
    la rechace con su razón (nunca se adivina)."""
    texto = texto_de(valor).strip()
    if texto == "":
        return None
    return int(texto) if texto.isascii() and texto.isdigit() else texto


def renglones_del_formulario(formulario) -> list[dict]:
    """Los renglones que el formulario trae (`r-<n>-descripcion|detalle|monto`), en el orden de `n`. Un renglón
    sin nada escrito en ninguna de sus tres casillas es de sobra y se descarta; uno con algo escrito va a la
    puerta tal cual (si le falta el monto o la descripción, ella lo rechaza). El detalle vacío no se manda."""
    indices = sorted({int(m.group(1)) for m in map(_RE_RENGLON.match, list(formulario.keys())) if m})
    hechos = []
    for n in indices:
        descripcion = texto_de(formulario.get(f"r-{n}-descripcion"))
        detalle = texto_de(formulario.get(f"r-{n}-detalle"))
        monto = texto_de(formulario.get(f"r-{n}-monto"))
        if not (descripcion.strip() or detalle.strip() or monto.strip()):
            continue
        renglon = {"descripcion": descripcion, "monto": monto}
        if detalle.strip():
            renglon["detalle"] = detalle
        hechos.append(renglon)
    return hechos


def campos_del_formulario(formulario) -> dict:
    """Lo que el POST de una factura trae, ya en la forma que pide la puerta. No decide qué vale. `formulario` es el
    de la petición (se le pregunta con `.get`, `.getlist` y `.keys()`). El ITBIS es una casilla: marcada es el
    porcentaje que usa la casa (`db.ITBIS_DE_FACTURA`), sin marcar es «sin ITBIS»."""
    refs = [r.strip() for r in formulario.getlist("ref") if isinstance(r, str) and r.strip()]
    return {
        "cliente": _entero_o_eltexto_de(formulario.get("cliente")),
        "proyecto": _entero_o_eltexto_de(formulario.get("proyecto")),
        "cotizacion": _entero_o_eltexto_de(formulario.get("cotizacion")),
        "opcion": _entero_o_eltexto_de(formulario.get("opcion")),
        "para_nombre": texto_de(formulario.get("para_nombre")),
        "fecha": texto_de(formulario.get("fecha")).strip(),
        "itbis_pct": str(db.ITBIS_DE_FACTURA) if "itbis" in formulario else None,
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
    """«62500.00» → «62,500» (lo que se escribe en la casilla del monto)."""
    return cz.dinero_en_pesos(total)[len("RD$"):]


def casillas_de_renglones(renglones: list[dict], *, con_sobra: bool = True) -> list[dict]:
    """Los renglones como casillas: los que hay, con su monto como se escribe, y los en blanco que faltan para
    llegar a `RENGLONES_EN_BLANCO` (más `RENGLONES_DE_SOBRA` si `con_sobra`, que sin JavaScript hacen de «otro
    renglón»). `renglones` es `[{"descripcion", "detalle", "monto"}]` con el monto como texto o `Decimal`."""
    hechos = []
    for r in renglones:
        monto = r.get("monto")
        hechos.append({"descripcion": r.get("descripcion") or "", "detalle": r.get("detalle") or "",
                       "monto": "" if monto in (None, "") else (_sin_rd(monto) if isinstance(monto, Decimal) else str(monto))})
    en_blanco = {"descripcion": "", "detalle": "", "monto": ""}
    faltan = max(RENGLONES_EN_BLANCO - len(hechos), 0) + (RENGLONES_DE_SOBRA if con_sobra else 0)
    return hechos + [dict(en_blanco) for _ in range(faltan)]


def valores_del_formulario(*, factura: dict | None = None, escrito: dict | None = None, nombre_del_cliente: str = "",
                           desde_la_opcion: list[dict] | None = None, hoy: date | None = None) -> dict:
    """Con qué se llenan las casillas. Manda lo ESCRITO (un guardado rechazado vuelve con lo que la persona puso),
    después la factura que se corrige, después la opción de la cotización de la que nace, y si no hay nada, en
    blanco con la fecha de hoy y el nombre del cliente."""
    if escrito is not None:
        return {"para_nombre": escrito["para_nombre"], "fecha": escrito["fecha"],
                "itbis": escrito["itbis_pct"] is not None, "ncf": escrito["ncf"], "ncf_tipo": escrito["ncf_tipo"],
                "rnc_cedula": escrito["rnc_cedula"],
                "renglones": casillas_de_renglones(escrito["renglones"])}
    if factura is not None:
        return {"para_nombre": factura["para_nombre"], "fecha": _iso(factura["fecha"]),
                "itbis": factura["itbis_pct"] is not None, "ncf": factura["ncf"] or "",
                "ncf_tipo": factura["ncf_tipo"] or "", "rnc_cedula": factura["rnc_cedula"] or "",
                "renglones": casillas_de_renglones(factura["renglones"])}
    return {"para_nombre": nombre_del_cliente, "fecha": _iso(hoy) if hoy else "", "itbis": False, "ncf": "",
            "ncf_tipo": "", "rnc_cedula": "", "renglones": casillas_de_renglones(desde_la_opcion or [])}


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

def ligas_de_las_refs(refs: list[str], respuesta: dict) -> tuple[list[dict], list[dict]]:
    """`(ligas, problemas)` de las referencias escogidas, a partir de lo que la App CONTESTÓ para esas referencias
    (`registro_lectura.sesiones_de_cliente(None, refs)`).

    EL TIPO NO LO PONE QUIEN LLENA EL FORMULARIO: es `trabajo` si la App dice `es_trabajo` y `sesion` si no. Cada
    liga es `{"tipo", "ref", "codigo"}`, en el orden en que se escogieron y sin repetir. Un problema es
    `{"ref", "motivo"}` con motivo `cancelada` (la App la devolvió cancelada o pospuesta: no se factura) o
    `no_esta` (la App no la devolvió): esa referencia NO se liga. `respuesta` sin la lectura (`no_se_pudo`) la
    trata quien llama; aquí una referencia que no vino es `no_esta`."""
    por_ref = {r["ref"]: r for r in respuesta.get("sesiones", []) + respuesta.get("trabajos", [])}
    canceladas = set(respuesta.get("canceladas") or [])
    ligas, problemas, vistas = [], [], set()
    for ref in refs:
        if ref in vistas:
            continue
        vistas.add(ref)
        if ref in canceladas:
            problemas.append({"ref": ref, "motivo": "cancelada"})
        elif ref not in por_ref:
            problemas.append({"ref": ref, "motivo": "no_esta"})
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
    quien = f"«{p['codigo']}»" if p.get("codigo") else "Una sesión o trabajo"
    if p["motivo"] == "cancelada":
        return f"{quien} está cancelado o pospuesto: no se factura."
    return f"{quien} ya no está en el registro: no se ligó."


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
    "renglon_forma": "Cada renglón lleva descripción y monto, y a lo sumo un detalle.",
    "monto_vacio": "A un renglón le falta el monto.",
    "monto_cero": "Cada monto tiene que ser mayor que cero.",
    "monto_alto": "Un monto es demasiado grande.",
    "monto_decimales": "Un monto lleva más de dos decimales.",
    "monto_formato": "Un monto no se entiende; escríbelo como 62,500 o 62500.50.",
    "monto_tipo": "Un monto no se entiende; escríbelo como 62,500 o 62500.50.",
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
    "tiene_envios": "Un borrador que ya tiene envíos no se descarta.",
    "candado": "Falta saber qué versión de la factura estabas viendo: ábrela otra vez.",
    "sin_cambios": "No hay nada que cambiar.",
}

# Qué casilla marca cada razón (las que son de UNA casilla). Se busca por el comienzo de la clave.
CASILLA_POR_COMIENZO = (("ncf", "ncf"), ("rnc", "rnc_cedula"), ("para_nombre", "para_nombre"), ("fecha", "fecha"),
                        ("itbis", "itbis"))


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
