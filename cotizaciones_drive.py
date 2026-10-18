"""Subir el PDF de una cotización enviada a Drive (Cotizaciones, pieza 8 del diseño §3.7).

QUÉ ES ESTO. La política de la subida: qué carpeta, en qué orden, qué se anota y qué dice la pantalla. Habla con
Drive SOLO por `drive.py` (pieza 7) y con la base SOLO por `db.py`; no abre ninguna conexión ni crea ningún cliente
HTTP. Lo llaman las rutas de «Guardar y descargar PDF» (nueva, borrador que pasa a enviada, corrección) y de
«Volver a subir a Drive», DESPUÉS de que la cotización ya quedó guardada y confirmada en Lucy.

EL ORDEN, siempre este (diseño §3.7): (1) la cotización ya está guardada (el `COMMIT` fue de `db.guardar_cotizacion`
antes de llegar aquí); (2) se arma el PDF de ESA versión; (3) se sube; (4) solo si Drive contesta con el id del
archivo, `db.marcar_cotizacion_en_drive` pone `drive_archivo_id`, `drive_version = <el cambiado_en que se subió>` y
borra `drive_error`, y solo si `cambiado_en` sigue siendo ese. Si algo falla en (2) o (3), `drive_error` queda con
una frase fija (`CAUSAS`) y la cotización sigue guardada y enviada. Nada de lo que Google contesta llega a una frase.

«EN DRIVE» tiene UNA sola puerta: `estado_de_drive`. Una cotización está en Drive si y solo si hay `drive_archivo_id`
y `drive_version == cambiado_en`. Todas las frases de la pantalla salen de lo que esa función dice; ninguna se
escribe aparte.

LAS CARPETAS. `<Proyectos>/<nombre del proyecto>/cotizaciones`, siempre por id desde que se crean
(`drive_de_proyectos`). «Proyectos» es el id de `LUCY_DRIVE_CARPETA_PROYECTOS` (`drive.carpeta_proyectos`); si falta,
nada se crea a ciegas. Si dentro de «Proyectos» ya hay una carpeta con ese nombre, la nueva se llama
«<nombre> · #<número del proyecto>». Si el proyecto se renombra, se le cambia el nombre a SU carpeta (por id). Si la
carpeta ya no está en Drive (404) NO se rehace sola: se dice la causa. La carpeta del proyecto se anota en
`proyectos.carpeta` SOLO si estaba vacía, y se vuelve a leer la base para decir si quedó (`ENLACE_*`).

LA CARPETA NACE CON EL PROYECTO (diseño «Carpetas del proyecto», pieza 1). `preparar_la_carpeta` crea esas mismas
carpetas SIN cotización, por la misma `_crear_las_carpetas`; la llaman, DESPUÉS de guardar el proyecto, las tres rutas que
lo crean (el panel, Telegram y «convertir en proyecto»). Nunca sube una excepción: si Drive falla el proyecto ya existe y
`frase_de_la_carpeta` (la única fuente de lo que se dice) cuenta que la carpeta NO se creó. Lucy no borra ni mueve nada en Drive.

EL TIEMPO. `TIEMPO_DEL_INTENTO` (15 s, de diseño, no medido) es el tope de TODAS las llamadas a Drive de un intento
juntas, no de cada una (cada una además tiene el suyo en `drive.py`). Las escrituras a la base no se cortan.

LO QUE QUEDA FUERA, dicho: dos subidas a la vez de la misma cotización o del mismo proyecto nuevo pueden dejar una
carpeta o un archivo de más en Drive (la búsqueda por nombre lo reduce, no lo cierra); un archivo borrado a mano en
Drive deja «el archivo ya no está» hasta que alguien decida qué hacer (no se rehace solo, igual que la carpeta); con
`drive.file` Lucy solo ve lo que ella creó, así que «ya hay una carpeta con ese nombre» no ve las que se crearon a mano.
"""
from __future__ import annotations

import asyncio
import logging
import re

import cotizaciones
import db.db as db
import drive
from acciones import crud

log = logging.getLogger("lucy.cotizaciones_drive")

# Cuánto se espera a Drive en TODO un intento (las llamadas juntas). De diseño (§3.7), no medido.
TIEMPO_DEL_INTENTO = 15.0

NOMBRE_DE_LA_CARPETA_DE_COTIZACIONES = "cotizaciones"

# Las causas que se guardan en `drive_error` y se enseñan. Las que vienen de Drive son las de `drive.py` (no se
# repiten aquí: se leen de sus clases); `DriveNoEsta` no se guarda tal cual, porque según el paso significa otra cosa.
SIN_LA_CARPETA_DEL_PROYECTO = "la carpeta del proyecto ya no está en Drive"
SIN_EL_ARCHIVO = "el archivo de la cotización ya no está en Drive"
SIN_PDF = "no se pudo armar el PDF"
CAUSAS = (
    drive.DriveNoContesta.causa,
    drive.DriveSinPermiso.causa,
    drive.DriveSinConfigurar.causa,
    drive.DriveSinCarpetaProyectos.causa,
    drive.DriveError.causa,
    SIN_LA_CARPETA_DEL_PROYECTO,
    SIN_EL_ARCHIVO,
    SIN_PDF,
)

# Por qué se escribió `proyectos.carpeta` (queda en la huella). Dos orígenes, dos frases verdaderas.
MOTIVO_AL_SUBIR = "La carpeta del proyecto en Drive (la creó Lucy al subir una cotización)"
MOTIVO_AL_CREAR_EL_PROYECTO = "La carpeta del proyecto en Drive (la creó Lucy al crear el proyecto)"

# Qué pasó con el enlace en `proyectos.carpeta` cuando Lucy creó (o ya tenía) la carpeta de Drive del proyecto.
ENLACE_PUESTO = "puesto"            # estaba vacía y ahora tiene el enlace (se volvió a leer de la base)
ENLACE_YA_TENIA = "ya_tenia"        # tenía algo escrito: no se tocó
ENLACE_NO_SE_PUDO = "no_se_pudo"    # no se pudo escribir (o no se pudo comprobar que quedó): se dice, no se calla

ETIQUETA_EN_DRIVE = "En Drive"
ETIQUETA_VIEJA = "La versión de Drive es vieja"
ETIQUETA_NO_ESTA = "No está en Drive"

_ID_DE_DRIVE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class _Falla(Exception):
    """Un paso del intento no salió; `causa` es una de `CAUSAS`."""

    def __init__(self, causa: str):
        super().__init__(causa)
        self.causa = causa


# ── Lo que la pantalla dice ───────────────────────────────────────────────────────────────────────

def estado_de_drive(cot: dict) -> dict | None:
    """LA PUERTA de «¿está en Drive?». `None` para lo que no es una enviada (un borrador no se sube). Para una
    enviada: `estado` (`en_drive`, `vieja` o `no_esta`), `etiqueta` (lo que dice la lista), `causa` (la del último
    intento que falló, solo si es una de `CAUSAS`) y las dos frases del aviso. «En Drive» exige `drive_archivo_id`
    y `drive_version == cambiado_en`; que no se pueda comparar cuenta como que no."""
    if cot["estado"] != "enviada":
        return None
    archivo, version = cot.get("drive_archivo_id"), cot.get("drive_version")
    if archivo and version is not None and version == cot["cambiado_en"]:
        estado, etiqueta = "en_drive", ETIQUETA_EN_DRIVE
    elif archivo:
        estado, etiqueta = "vieja", ETIQUETA_VIEJA
    else:
        estado, etiqueta = "no_esta", ETIQUETA_NO_ESTA
    causa = cot.get("drive_error")
    causa = causa if causa in CAUSAS else None
    if estado == "en_drive":
        al_guardar, al_reintentar = "Guardada en Lucy y en Drive.", "Subida a Drive."
    else:
        porque = f" ({causa})" if causa else ""
        al_guardar = f"Guardada en Lucy, pero NO llegó a Drive{porque}. Puedes volver a intentarlo."
        al_reintentar = f"NO llegó a Drive{porque}. Puedes volver a intentarlo."
    return {"estado": estado, "etiqueta": etiqueta, "causa": causa,
            "rehacer_carpeta": estado != "en_drive" and causa == SIN_LA_CARPETA_DEL_PROYECTO,
            "aviso_al_guardar": al_guardar, "aviso_al_reintentar": al_reintentar}


SE_CREARA_CON_LA_COTIZACION = "Se creará con la primera cotización que se envíe."


def frase_de_la_carpeta(resultado: dict) -> str:
    """LA PUERTA de lo que se dice de la carpeta de Drive de un proyecto nuevo (la de Telegram, el aviso del
    panel y, en su día, la página salen de aquí; ninguna se escribe aparte). `resultado` es lo que devuelve
    `preparar_la_carpeta`. Solo `lista` y `ya_estaba` afirman que la carpeta existe; cualquier otra cosa, o
    algo que no se entiende, dice que NO se creó. Si la carpeta está pero su enlace no quedó en el proyecto, lo dice."""
    r = resultado.get("resultado") if isinstance(resultado, dict) else None
    if r not in ("lista", "ya_estaba"):
        if r == "no_esta":
            return "No se pudo crear la carpeta en Drive: el proyecto ya no está."
        causa = resultado.get("causa") if isinstance(resultado, dict) else None
        porque = f" ({causa})" if causa in CAUSAS else ""
        return f"No se pudo crear la carpeta en Drive{porque}. {SE_CREARA_CON_LA_COTIZACION}"
    frase = "Carpeta de Drive creada." if r == "lista" else "El proyecto ya tiene su carpeta en Drive."
    carpeta = resultado.get("carpeta")
    if carpeta == ENLACE_NO_SE_PUDO:
        frase += " Pero NO se pudo poner su enlace en el proyecto: «Abrir la carpeta» no lo muestra."
    elif carpeta == ENLACE_YA_TENIA and r == "lista":
        frase += " El proyecto ya tenía otra carpeta escrita y no se cambió."
    return frase


# Las claves de aviso del panel (`?error=`) y el resultado que cada una cuenta. La causa no viaja en la dirección.
AVISOS_DE_LA_CARPETA = {
    "carpeta_drive_fallo": {"resultado": "fallo", "causa": None},
    "carpeta_drive_enlace": {"resultado": "lista", "causa": None, "carpeta": ENLACE_NO_SE_PUDO},
}


def clave_de_aviso_de_la_carpeta(resultado: dict) -> str | None:
    """La clave de aviso del panel para lo que pasó con la carpeta, o `None` si no hay nada que avisar (la carpeta
    quedó y su enlace también, o ya los tenía)."""
    r = resultado.get("resultado") if isinstance(resultado, dict) else None
    if r not in ("lista", "ya_estaba"):
        return "carpeta_drive_fallo"
    return "carpeta_drive_enlace" if resultado.get("carpeta") == ENLACE_NO_SE_PUDO else None


def enlace_de_carpeta_de_drive(carpeta_id) -> str | None:
    """La dirección de una carpeta de Drive por su id, o `None` si lo que llega no puede ser un id."""
    if isinstance(carpeta_id, str) and _ID_DE_DRIVE.fullmatch(carpeta_id):
        return f"https://drive.google.com/drive/folders/{carpeta_id}"
    return None


# ── El intento ────────────────────────────────────────────────────────────────────────────────────

class _Intento:
    """El tiempo de UN intento, repartido entre sus llamadas a Drive."""

    def __init__(self):
        self.limite = asyncio.get_running_loop().time() + TIEMPO_DEL_INTENTO

    async def drive(self, llamada, *, no_esta: str):
        """Una llamada a Drive (`llamada` es una función sin argumentos que arma la corrutina) dentro de lo que
        queda del tiempo. Lo que falla sube como `_Falla` con su causa; un 404 dice `no_esta`, según el paso."""
        restante = self.limite - asyncio.get_running_loop().time()
        if restante <= 0:
            raise _Falla(drive.DriveNoContesta.causa)
        try:
            return await asyncio.wait_for(llamada(), restante)
        except asyncio.TimeoutError:
            raise _Falla(drive.DriveNoContesta.causa) from None
        except drive.DriveNoEsta:
            raise _Falla(no_esta) from None
        except drive.DriveError as e:
            raise _Falla(e.causa) from None


def _la_raiz() -> str:
    try:
        return drive.carpeta_proyectos()
    except drive.DriveError as e:
        raise _Falla(e.causa) from None


def _nombre_con_numero(nombre: str, proyecto_id: int) -> str:
    return f"{nombre} · #{proyecto_id}"


async def _nombre_para_la_carpeta(intento: _Intento, raiz: str, nombre: str, proyecto_id: int, propia) -> str:
    """El nombre del proyecto, salvo que dentro de «Proyectos» ya haya OTRA carpeta con ese nombre (una que no
    sea `propia`): entonces «<nombre> · #<número>». Nunca se reusa por nombre la carpeta de otro proyecto."""
    hay = await intento.drive(lambda: drive.buscar_en(raiz, nombre), no_esta=drive.DriveSinCarpetaProyectos.causa)
    if any(x["id"] != propia for x in hay):
        return _nombre_con_numero(nombre, proyecto_id)
    return nombre


async def _llenar_la_carpeta_del_proyecto(proyecto_id: int, carpeta_id: str, motivo: str) -> str:
    """Si el proyecto NO tiene carpeta escrita, le pone la dirección de la que Lucy acaba de crear, por la misma
    puerta de siempre (`crud.editar`), y VUELVE A LEER la base para decir que quedó. Una ya escrita (aunque Lucy no
    la entienda) no se toca. Devuelve `ENLACE_PUESTO`, `ENLACE_YA_TENIA` o `ENLACE_NO_SE_PUDO` (si algo falla, o si
    no se puede comprobar que quedó, se registra y se dice: no importa para la subida, pero no se calla)."""
    try:
        enlace = enlace_de_carpeta_de_drive(carpeta_id)
        actuales = await db.carpetas_de_proyectos()
        if enlace is None or actuales is None or proyecto_id not in actuales:
            return ENLACE_NO_SE_PUDO
        try:
            vacia = db.carpeta_guardada(actuales[proyecto_id]) is None
        except db.CarpetaDeProyectoNoVale:
            vacia = False
        if not vacia:
            return ENLACE_YA_TENIA
        await crud.editar("proyectos", proyecto_id, {"carpeta": enlace}, motivo=motivo, actor="panel")
        despues = await db.carpetas_de_proyectos()
        if despues is not None and db.carpeta_guardada(despues.get(proyecto_id)) == enlace:
            return ENLACE_PUESTO
        log.warning("cotizaciones: el enlace de la carpeta del proyecto #%s no quedó escrito", proyecto_id)
    except Exception as e:
        log.warning("cotizaciones: no se pudo llenar la carpeta del proyecto #%s (%s)", proyecto_id, type(e).__name__)
    return ENLACE_NO_SE_PUDO


async def _crear_las_carpetas(intento: _Intento, proyecto_id: int, nombre_del_proyecto: str, motivo: str) -> dict:
    """Crea `Proyectos/<nombre>` y su `cotizaciones`, las anota y llena `proyectos.carpeta` si estaba vacía. La
    llaman la primera subida de una cotización y `preparar_la_carpeta`: es la ÚNICA manera de crear las carpetas de
    un proyecto. Devuelve `{"carpetas": <las que quedaron anotadas>, "propia": <¿son las que se acaban de crear?>,
    "enlace": <qué pasó con proyectos.carpeta, o None si otra creación se adelantó>}`."""
    raiz = _la_raiz()
    nombre = await _nombre_para_la_carpeta(intento, raiz, nombre_del_proyecto, proyecto_id, propia=None)
    del_proyecto = await intento.drive(lambda: drive.crear_carpeta(nombre, raiz),
                                       no_esta=drive.DriveSinCarpetaProyectos.causa)
    de_cotizaciones = await intento.drive(
        lambda: drive.crear_carpeta(NOMBRE_DE_LA_CARPETA_DE_COTIZACIONES, del_proyecto),
        no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
    quedaron = await db.guardar_carpetas_de_drive(proyecto_id, del_proyecto, de_cotizaciones, nombre)
    propia = quedaron["carpeta_proyecto_id"] == del_proyecto
    enlace = None
    if propia:
        enlace = await _llenar_la_carpeta_del_proyecto(proyecto_id, del_proyecto, motivo)
    else:
        log.warning("cotizaciones: otra subida se adelantó con las carpetas del proyecto #%s", proyecto_id)
    return {"carpetas": quedaron, "propia": propia, "enlace": enlace}


async def preparar_la_carpeta(proyecto_id: int, *, motivo: str = MOTIVO_AL_CREAR_EL_PROYECTO) -> dict:
    """La carpeta de Drive de un proyecto, sin que haya una cotización: la llaman las tres rutas que crean un proyecto
    (DESPUÉS de guardarlo y confirmarlo). Devuelve `{"resultado", "causa", "carpeta"}`:
      · `lista`: se crearon `Proyectos/<nombre>` y `cotizaciones`, se anotaron y se leyeron de vuelta;
      · `ya_estaba`: el proyecto ya tenía fila en `drive_de_proyectos`: NO se llama a Drive para escribir nada;
      · `no_esta`: el proyecto no existe o está en la papelera;
      · `fallo`: no se pudo (`causa` es una de `CAUSAS`).
    `carpeta` dice qué pasó con `proyectos.carpeta` (`ENLACE_*`): con `lista` y `ya_estaba` se intenta que quede el
    enlace puesto si estaba vacía, y si no se pudo se dice. NUNCA sube una excepción (solo `CancelledError`)."""
    try:
        proyecto = await db.proyecto_para_la_carpeta(proyecto_id)
        if proyecto is None:
            return {"resultado": "no_esta", "causa": None, "carpeta": None}
        carpetas = await db.carpetas_de_drive(proyecto_id)
        if carpetas is not None:
            enlace = await _llenar_la_carpeta_del_proyecto(proyecto_id, carpetas["carpeta_proyecto_id"], motivo)
            return {"resultado": "ya_estaba", "causa": None, "carpeta": enlace}
        hecho = await _crear_las_carpetas(_Intento(), proyecto_id, proyecto["nombre"], motivo)
        if not hecho["propia"]:
            return {"resultado": "ya_estaba", "causa": None, "carpeta": None}
        return {"resultado": "lista", "causa": None, "carpeta": hecho["enlace"]}
    except _Falla as f:
        log.warning("cotizaciones: no se pudo crear la carpeta del proyecto #%s (%s)", proyecto_id, f.causa)
        return {"resultado": "fallo", "causa": f.causa, "carpeta": None}
    except Exception as e:
        log.warning("cotizaciones: no se pudo crear la carpeta del proyecto #%s (%s)", proyecto_id, type(e).__name__)
        return {"resultado": "fallo", "causa": drive.DriveError.causa, "carpeta": None}


async def _poner_al_dia_el_nombre(intento: _Intento, cot: dict, carpetas: dict) -> None:
    """Si el proyecto ya no se llama como se puso su carpeta, se le cambia el nombre A ESA CARPETA (por id). Si
    falla, la subida sigue (el archivo va por id) y se intenta en la próxima."""
    pid, nombre = cot["proyecto_id"], cot["proyecto_nombre"]
    if carpetas["nombre_puesto"] in (nombre, _nombre_con_numero(nombre, pid)):
        return
    carpeta = carpetas["carpeta_proyecto_id"]
    try:
        nuevo = await _nombre_para_la_carpeta(intento, _la_raiz(), nombre, pid, propia=carpeta)
        await intento.drive(lambda: drive.renombrar(carpeta, nuevo), no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
        await db.poner_nombre_de_carpeta_de_drive(pid, nuevo)
    except _Falla as f:
        log.info("cotizaciones: no se pudo renombrar la carpeta del proyecto #%s (%s)", pid, f.causa)


async def _llevar_a_drive(intento: _Intento, cot: dict) -> str:
    """Los pasos (2) y (3): arma el PDF de esta versión, deja lista la carpeta y sube. Devuelve el id del archivo."""
    try:
        pdf = cotizaciones.pdf_de_cotizacion(cotizaciones.hoja_de_cotizacion(cot), borrador=False)
    except Exception:
        raise _Falla(SIN_PDF) from None
    carpetas = await db.carpetas_de_drive(cot["proyecto_id"])
    if carpetas is None:
        carpetas = (await _crear_las_carpetas(intento, cot["proyecto_id"], cot["proyecto_nombre"], MOTIVO_AL_SUBIR))["carpetas"]
    else:
        await _poner_al_dia_el_nombre(intento, cot, carpetas)
    carpeta = carpetas["carpeta_cotizaciones_id"]
    nombre_del_pdf = f"{cot['codigo']}.pdf"
    archivo = cot.get("drive_archivo_id") or None
    if archivo is not None:
        try:
            return await intento.drive(lambda: drive.subir_pdf(carpeta, nombre_del_pdf, pdf, archivo_id=archivo),
                                       no_esta=SIN_EL_ARCHIVO)
        except _Falla as f:
            if f.causa != SIN_EL_ARCHIVO:
                raise
            # Drive dice que ese archivo ya no existe (alguien lo borró, o se fue con su carpeta): el id guardado no
            # vale. Se suelta y el MISMO intento sigue como una subida sin id (el id nuevo lo anota la marca).
            log.info("cotizaciones: el archivo guardado de #%s ya no está; se sube como nuevo", cot["proyecto_id"])
            archivo = None
    if archivo is None:
        # Un intento anterior pudo llegar a crear el archivo y caerse antes de anotarlo: se reemplaza ese.
        hay = await intento.drive(lambda: drive.buscar_en(carpeta, nombre_del_pdf), no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
        if hay:
            archivo = hay[0]["id"]
    return await intento.drive(
        lambda: drive.subir_pdf(carpeta, nombre_del_pdf, pdf, archivo_id=archivo),
        no_esta=SIN_EL_ARCHIVO if archivo else SIN_LA_CARPETA_DEL_PROYECTO)


async def _rehacer_las_carpetas(intento: _Intento, cot: dict, carpetas: dict):
    """Crea de nuevo lo que falta y devuelve `(carpeta_proyecto_id, carpeta_cotizaciones_id, nombre_puesto)`, o `None`
    si la carpeta de cotizaciones sigue en Drive (no hay nada que rehacer). Se mira ANTES de crear: si la del proyecto
    ya no está (404) se rehacen las dos, con la misma regla de nombre y «· #N»; si está pero sin su `cotizaciones`,
    solo esta."""
    pid, proyecto, vieja = cot["proyecto_id"], carpetas["carpeta_proyecto_id"], carpetas["carpeta_cotizaciones_id"]
    try:
        hijos = await intento.drive(lambda: drive.buscar_en(proyecto, NOMBRE_DE_LA_CARPETA_DE_COTIZACIONES),
                                    no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
        proyecto_vive = True
    except _Falla as f:
        if f.causa != SIN_LA_CARPETA_DEL_PROYECTO:
            raise
        hijos, proyecto_vive = [], False
    if any(h["id"] == vieja for h in hijos):
        return None
    nombre = carpetas["nombre_puesto"]
    if proyecto_vive:
        # Buscar dentro de una carpeta que ya no está puede contestar «nadie» y no un 404: es al crear dentro cuando
        # Drive dice que no existe. Entonces el proyecto también estaba muerto.
        try:
            nueva = await intento.drive(lambda: drive.crear_carpeta(NOMBRE_DE_LA_CARPETA_DE_COTIZACIONES, proyecto),
                                        no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
            return proyecto, nueva, nombre
        except _Falla as f:
            if f.causa != SIN_LA_CARPETA_DEL_PROYECTO:
                raise
    raiz = _la_raiz()
    nombre = await _nombre_para_la_carpeta(intento, raiz, cot["proyecto_nombre"], pid, propia=None)
    proyecto = await intento.drive(lambda: drive.crear_carpeta(nombre, raiz),
                                   no_esta=drive.DriveSinCarpetaProyectos.causa)
    nueva = await intento.drive(lambda: drive.crear_carpeta(NOMBRE_DE_LA_CARPETA_DE_COTIZACIONES, proyecto),
                                no_esta=SIN_LA_CARPETA_DEL_PROYECTO)
    return proyecto, nueva, nombre


async def rehacer_la_carpeta(proyecto_id: int) -> str:
    """«Crear de nuevo la carpeta en Drive» (§3.7). Solo si alguna cotización enviada del proyecto dice, en
    `drive_error`, que la carpeta del proyecto ya no está (`sin_causa` si no), y solo si de verdad no está (`sigue` si Drive la
    ve: no se crea otra). Crea, anota las nuevas y suelta los archivos de las cotizaciones del proyecto, que quedan
    «No está en Drive» y se suben con «Volver a subir». Resultados: `rehecha` (las dos carpetas nuevas),
    `rehecha_enlace_viejo` (igual, y `proyectos.carpeta` sigue con el enlace de la vieja), `rehecha_subcarpeta` (solo
    `cotizaciones`; el proyecto conserva la suya) o `fallo` (algo salió mal: no se anota nada)."""
    todas = await db.cotizaciones_de_proyectos()
    enviadas = [c for c in (todas or {}).get(proyecto_id, []) if c["estado"] == "enviada"]
    muertas = [c for c in enviadas if c.get("drive_error") == SIN_LA_CARPETA_DEL_PROYECTO]
    carpetas = await db.carpetas_de_drive(proyecto_id) if muertas else None
    cot = await db.cotizacion_para_drive(proyecto_id, muertas[0]["id"]) if carpetas else None
    if cot is None:
        return "sin_causa"
    try:
        nuevas = await _rehacer_las_carpetas(_Intento(), cot, carpetas)
    except _Falla as f:
        log.warning("cotizaciones: no se pudo rehacer la carpeta del proyecto #%s (%s)", proyecto_id, f.causa)
        return "fallo"
    except Exception as e:
        log.warning("cotizaciones: no se pudo rehacer la carpeta del proyecto #%s (%s)", proyecto_id, type(e).__name__)
        return "fallo"
    if nuevas is None:
        return "sigue"
    if not await db.rehacer_carpetas_de_drive(proyecto_id, carpetas["carpeta_cotizaciones_id"], *nuevas,
                                              [(c["id"], c["cambiado_en"]) for c in enviadas]):
        log.warning("cotizaciones: las carpetas del proyecto #%s ya eran otras; no se anotó nada", proyecto_id)
        return "fallo"
    if nuevas[0] == carpetas["carpeta_proyecto_id"]:
        return "rehecha_subcarpeta"
    # Se rehízo también la carpeta del proyecto. `proyectos.carpeta` NO se cambia (solo se llena si está vacía): si
    # vale EXACTAMENTE el enlace de la carpeta vieja (`https://drive.google.com/drive/folders/<id>`, tal como lo
    # escribe Lucy), «Abrir la carpeta» sigue apuntando a ella y el aviso lo dice. Si el enlace trae algo más
    # (`?usp=sharing`) o es otro texto, la comparación no coincide y el aviso NO lo dice.
    try:
        actuales = await db.carpetas_de_proyectos()
        vieja = enlace_de_carpeta_de_drive(carpetas["carpeta_proyecto_id"])
        if actuales is not None and vieja is not None and actuales.get(proyecto_id) == vieja:
            return "rehecha_enlace_viejo"
    except Exception as e:
        log.warning("cotizaciones: no se pudo mirar la carpeta escrita del proyecto #%s (%s)", proyecto_id, type(e).__name__)
    return "rehecha"


async def _anotar_el_error(cot: dict, causa: str, version) -> None:
    try:
        await db.marcar_error_de_drive(cot["proyecto_id"], cot["id"], causa, version)
    except Exception as e:
        log.warning("cotizaciones: no se pudo anotar el error de Drive de #%s (%s)", cot["proyecto_id"], type(e).__name__)


async def subir(proyecto_id: int, cotizacion_id: int) -> str:
    """Sube a Drive el PDF de UNA cotización enviada y dice qué pasó: `no_esta` (la cotización no existe, no es de
    ese proyecto o falta la tabla), `borrador` (no se sube), `subida` (Drive contestó y la marca quedó puesta),
    `cambio` (Drive contestó pero la cotización cambió mientras se subía: no se marcó) o `fallo` (queda la causa en
    `drive_error`). Nunca sube una excepción por lo que haga Drive; solo por la base, si no se puede ni leer."""
    cot = await db.cotizacion_para_drive(proyecto_id, cotizacion_id)
    if cot is None:
        return "no_esta"
    if cot["estado"] != "enviada":
        return "borrador"
    version = cot["cambiado_en"]
    try:
        archivo = await _llevar_a_drive(_Intento(), cot)
    except _Falla as f:
        log.warning("cotizaciones: la cotización de #%s no llegó a Drive (%s)", proyecto_id, f.causa)
        await _anotar_el_error(cot, f.causa, version)
        return "fallo"
    except Exception as e:
        log.warning("cotizaciones: la subida a Drive de #%s falló (%s)", proyecto_id, type(e).__name__)
        await _anotar_el_error(cot, drive.DriveError.causa, version)
        return "fallo"
    if await db.marcar_cotizacion_en_drive(proyecto_id, cotizacion_id, archivo, version):
        return "subida"
    log.info("cotizaciones: la cotización de #%s cambió mientras se subía; la marca no se puso", proyecto_id)
    return "cambio"
