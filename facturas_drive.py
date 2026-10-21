"""Subir a Drive el PDF de una factura (Facturas, pieza 8 del diseño, 21-oct-2026).

HERMANA de `cotizaciones_drive.py` y COPIA SU CAMINO: habla con Drive SOLO por `drive.py` (el único módulo que
tiene un cliente HTTP, con el permiso `drive.file` y nada más), con la base SOLO por `db/db.py`, y usa del camino de
las cotizaciones lo mismo que ellas: el intento con su tope de tiempo (`cd._Intento`), la falla con causa fija
(`cd._Falla`), el crear la carpeta del proyecto con su `cotizaciones` si no existe (`cd._crear_las_carpetas`) y el
ponerle al día el nombre si el proyecto se renombró (`cd._poner_al_dia_el_nombre`). Aquí no hay otra copia de nada de eso.

QUÉ SE SUBE Y CUÁNDO. Lo llaman las rutas de «Emitir» y «Anular» DESPUÉS de que la factura ya quedó emitida o anulada
en la base (el cambio de estado no depende de Drive y no se deshace si Drive falla), y la de «Volver a subir a Drive».
  · una EMITIDA sube `FAC-AAAA-NNN.pdf` (la hoja sin marca);
  · una ANULADA sube `FAC-AAAA-NNN-ANULADA.pdf` (la hoja con la marca y el motivo). El de la emitida NO se toca ni se
    borra: tiene otro nombre y otra columna (`drive_archivo_id` frente a `drive_anulada_archivo_id`). Si la emitida
    nunca llegó a subir, tampoco se sube al anular: solo se sube lo que corresponde al estado de ahora.
  · un borrador no se sube.

A QUÉ CARPETA. Con proyecto: `<Proyectos>/<nombre del proyecto>/facturas`, hermana de `cotizaciones`, creada la primera
vez que se sube una factura de ese proyecto y anotada por id (columna `carpeta_facturas_id` de `drive_de_proyectos`). Sin proyecto: una
carpeta «Facturas» en la RAÍZ de Mi unidad (parent `root`, que `drive.py` ya acepta como id), creada la primera vez y
anotada por id (`drive_carpetas_sueltas`, clave `facturas`). Se puso en la raíz porque «Proyectos» es del proyecto y la
única carpeta que Lucy conoce por configuración es esa; si Tiziano quiere la carpeta en otro sitio la mueve a mano en Drive:
el id no cambia y Lucy sigue encontrándola. Antes de crearla se busca por nombre en la raíz (con `drive.file` Lucy solo
ve lo que ella creó), por si un intento anterior la creó y se cayó antes de anotarla.

SI LE PONEN O LE QUITAN EL PROYECTO a una emitida (la base lo permite, `db.poner_proyecto_de_factura`): el PDF que ya
está en Drive SE QUEDA donde se subió (Lucy no mueve ni borra nada en Drive). `facturas.drive_proyecto_id` guarda el
proyecto que la factura tenía al subirlo; si ya no es el de ahora, `estado_de_drive` dice `otra_carpeta` (el PDF sigue en
Drive, en la carpeta de antes, y la pantalla lo dice y ofrece subirlo a la de ahora). Al subir a la carpeta de ahora se crea
un archivo NUEVO allí; el de antes se queda.

EL ORDEN, siempre este: (1) la factura ya está emitida o anulada (el cambio fue de `db.emitir_factura` o
`db.anular_factura` antes de llegar aquí); (2) se arma el PDF de ESE estado; (3) se deja lista la carpeta; (4) se sube
(si en esa carpeta ya hay un archivo con ese nombre, de un intento que se cayó antes de anotarlo, se REEMPLAZA ese: no se
crea un segundo); (5) solo si Drive contesta con el id, `db.marcar_factura_en_drive` lo anota y borra `drive_error`, y solo
si la factura sigue en el mismo proyecto. Si algo falla, `drive_error` queda con una frase fija (`CAUSAS_DE_FACTURA`) y la
factura sigue emitida o anulada. Nada de lo que Google contesta llega a una frase.

«EN DRIVE» tiene UNA sola puerta: `estado_de_drive`. Todas las frases de la pantalla salen de lo que esa función dice;
ninguna se escribe aparte, y ninguna dice «en Drive» si no hay un id de archivo anotado en la carpeta de ahora.

LO QUE QUEDA FUERA, dicho (igual que en las cotizaciones): si la carpeta anotada (la `facturas` de un proyecto, la
«Facturas» suelta) ya no está en Drive (404) NO se rehace sola: se dice la causa, y reintentar vuelve a decir lo mismo hasta que
alguien decida qué hacer; las cotizaciones tienen un botón para rehacer la carpeta del proyecto y ESE botón no suelta los ids
ni la `facturas` anotada de las facturas (una factura de ese proyecto seguiría diciendo «En Drive»). Un archivo borrado a mano en
Drive tampoco se ve desde aquí. Dos subidas a la vez de la misma factura pueden dejar una carpeta de más.
"""
from __future__ import annotations

import logging
import re

import cotizaciones_drive as cd
import db.db as db
import drive
import facturas

log = logging.getLogger("lucy.facturas_drive")

SUFIJO_DE_LA_ANULADA = "-ANULADA"

SIN_EL_ARCHIVO_DE_LA_FACTURA = "el archivo de la factura ya no está en Drive"
# Las causas que se guardan en `drive_error` y se enseñan: las de `drive.py` y las del camino de las cotizaciones (sin la que
# habla de la cotización) más las tres de aquí. `cd.SIN_LA_CARPETA_DEL_PROYECTO` sale si, al crear `facturas`, Drive dice que
# la carpeta del proyecto ya no está.
CAUSAS_DE_FACTURA = tuple(c for c in cd.CAUSAS if c != cd.SIN_EL_ARCHIVO) + (
    cd.SIN_LA_CARPETA_DE_FACTURAS, SIN_EL_ARCHIVO_DE_LA_FACTURA, cd.SIN_EL_PROYECTO)

# Por qué se escribió el enlace de la carpeta del proyecto si la creó una factura (queda en la huella).
MOTIVO_AL_SUBIR_UNA_FACTURA = "La carpeta del proyecto en Drive (la creó Lucy al subir una factura)"

ETIQUETA_EN_DRIVE = "En Drive"
ETIQUETA_OTRA_CARPETA = "En Drive, en otra carpeta"
ETIQUETA_NO_ESTA = "No está en Drive"
BOTON_PARA_SUBIR = "Subir a Drive"
BOTON_PARA_VOLVER_A_SUBIR = "Volver a subir a Drive"
BOTON_PARA_SUBIR_A_LA_DE_AHORA = "Subir a la carpeta de ahora"

_ID_DE_DRIVE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def enlace_del_archivo(archivo_id) -> str | None:
    """La dirección de un archivo de Drive por su id, o `None` si lo que llega no puede ser un id."""
    if isinstance(archivo_id, str) and _ID_DE_DRIVE.fullmatch(archivo_id):
        return f"https://drive.google.com/file/d/{archivo_id}/view"
    return None


def nombre_del_pdf(f: dict, cual: str) -> str:
    """`FAC-2026-001.pdf` (la emitida) o `FAC-2026-001-ANULADA.pdf` (la anulada)."""
    return f"{f['codigo']}{SUFIJO_DE_LA_ANULADA if cual == 'anulada' else ''}.pdf"


def _del_mismo_sitio(d: dict) -> bool:
    """El PDF está en la carpeta que le toca a la factura HOY: se subió cuando tenía el proyecto que tiene ahora (o
    ninguno, las dos veces)."""
    return (d.get("drive_proyecto_id") or 0) == (d.get("proyecto_id") or 0)


# ── Lo que la pantalla dice ───────────────────────────────────────────────────────────────────────

def estado_de_drive(d: dict | None) -> dict | None:
    """LA PUERTA de «¿está en Drive?». `d` es lo que devuelve `db.drive_de_factura`. `None` para lo que no se sube (un
    borrador) y cuando no se pudo leer (`d` es `None`: no se afirma ni se niega nada). Para una emitida o una anulada:
      · `estado`: `en_drive`, `otra_carpeta` o `no_esta`;
      · `etiqueta`, `frase` y `boton` (`None` si no hay nada que ofrecer), todos escritos aquí;
      · `causa`: la del último intento que falló, solo si es una de `CAUSAS_DE_FACTURA`;
      · `enlaces`: los PDF que están anotados, `[{"etiqueta", "url"}]`, vacío si no hay ninguno.
    «En Drive» exige el id del PDF que le corresponde al estado de la factura (`drive_archivo_id` de una emitida,
    `drive_anulada_archivo_id` de una anulada) Y que esté en la carpeta de ahora. Un PDF anotado en otra carpeta es
    `otra_carpeta`, nunca `en_drive`."""
    if not isinstance(d, dict) or d.get("estado") not in ("emitida", "anulada"):
        return None
    anulada = d["estado"] == "anulada"
    suyo = d.get("drive_anulada_archivo_id") if anulada else d.get("drive_archivo_id")
    causa = d.get("drive_error")
    causa = causa if causa in CAUSAS_DE_FACTURA else None
    cual = "El PDF «ANULADA»" if anulada else "El PDF de la factura"
    enlaces = []
    for etiqueta, ident in (("Abrir el PDF de la emitida", d.get("drive_archivo_id")),
                            ("Abrir el PDF «ANULADA»", d.get("drive_anulada_archivo_id"))):
        url = enlace_del_archivo(ident)
        if url is not None:
            enlaces.append({"etiqueta": etiqueta, "url": url})
    if not suyo or enlace_del_archivo(suyo) is None:
        porque = f" ({causa})" if causa else ""
        if causa:
            frase = f"{cual} NO llegó a Drive{porque}. Puedes volver a intentarlo."
        else:
            frase = f"{cual} todavía no se ha subido a Drive."
        return {"estado": "no_esta", "etiqueta": ETIQUETA_NO_ESTA, "frase": frase,
                "boton": BOTON_PARA_VOLVER_A_SUBIR if causa else BOTON_PARA_SUBIR, "causa": causa,
                "enlaces": enlaces}
    if not _del_mismo_sitio(d):
        return {"estado": "otra_carpeta", "etiqueta": ETIQUETA_OTRA_CARPETA,
                "frase": f"{cual} está en Drive, pero en la carpeta que le tocaba antes de cambiarle el proyecto. "
                         "Lucy no mueve ni borra nada en Drive: si lo subes a la carpeta de ahora, el de antes se queda donde está.",
                "boton": BOTON_PARA_SUBIR_A_LA_DE_AHORA, "causa": causa, "enlaces": enlaces}
    return {"estado": "en_drive", "etiqueta": ETIQUETA_EN_DRIVE, "frase": f"{cual} está en Drive.",
            "boton": None, "causa": None, "enlaces": enlaces}


# ── El intento ────────────────────────────────────────────────────────────────────────────────────

async def _llevar_a_drive(intento, f: dict, cual: str) -> str:
    """Los pasos (2), (3) y (4): arma el PDF de este estado, deja lista la carpeta y sube. Devuelve el id del archivo."""
    try:
        pdf = facturas.pdf_de_factura(facturas.hoja_de_factura(f))
    except Exception:
        raise cd._Falla(cd.SIN_PDF) from None
    if f["proyecto_id"] is None:
        carpeta = await cd.la_carpeta_suelta(intento)
    else:
        carpeta = await cd.la_carpeta_de_facturas_del_proyecto(intento, f["proyecto_id"], MOTIVO_AL_SUBIR_UNA_FACTURA)
    nombre = nombre_del_pdf(f, cual)
    # Un intento anterior pudo llegar a crear el archivo y caerse antes de anotarlo: se reemplaza ese.
    hay = await intento.drive(lambda: drive.buscar_en(carpeta, nombre), no_esta=cd.SIN_LA_CARPETA_DE_FACTURAS)
    archivo = hay[0]["id"] if hay else None
    return await intento.drive(
        lambda: drive.subir_pdf(carpeta, nombre, pdf, archivo_id=archivo),
        no_esta=SIN_EL_ARCHIVO_DE_LA_FACTURA if archivo else cd.SIN_LA_CARPETA_DE_FACTURAS)


async def _anotar_el_error(factura_id: int, causa: str) -> None:
    try:
        await db.marcar_error_de_drive_de_factura(factura_id, causa)
    except Exception as e:
        log.warning("facturas: no se pudo anotar el error de Drive de la factura #%s (%s)", factura_id, type(e).__name__)


async def subir(factura_id: int) -> str:
    """Sube a Drive el PDF que le toca a UNA factura (el de la emitida o el de la anulada) y dice qué pasó: `no_esta` (no
    existe o falta la tabla), `borrador` (no se sube), `subida` (Drive contestó y la marca quedó puesta), `cambio` (Drive
    contestó pero la factura cambió de proyecto o de estado mientras se subía: no se marcó) o `fallo` (queda la causa en
    `drive_error`). Nunca sube una excepción por lo que haga Drive; solo por la base, si no se puede ni leer."""
    try:
        f = await db.factura_por_id(factura_id)
    except db.FacturasSinTabla:
        return "no_esta"
    if f is None:
        return "no_esta"
    if f["estado"] not in ("emitida", "anulada"):
        return "borrador"
    cual = "anulada" if f["estado"] == "anulada" else "emitida"
    try:
        archivo = await _llevar_a_drive(cd._Intento(), f, cual)
    except cd._Falla as e:
        log.warning("facturas: la factura #%s no llegó a Drive (%s)", factura_id, e.causa)
        await _anotar_el_error(factura_id, e.causa)
        return "fallo"
    except Exception as e:
        log.warning("facturas: la subida a Drive de la factura #%s falló (%s)", factura_id, type(e).__name__)
        await _anotar_el_error(factura_id, drive.DriveError.causa)
        return "fallo"
    if await db.marcar_factura_en_drive(factura_id, cual, archivo, f["proyecto_id"]):
        return "subida"
    log.info("facturas: la factura #%s cambió mientras se subía; la marca no se puso", factura_id)
    return "cambio"
