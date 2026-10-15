"""Google Drive, visto desde Lucy (Cotizaciones, pieza 7): el cliente.

QUÉ ES ESTO, Y QUÉ NO. Es el único módulo de Lucy que le habla a Drive, y solo
sabe cuatro cosas: crear una carpeta, buscar por nombre dentro de una carpeta,
renombrar y subir un PDF (crearlo, o reemplazar el contenido de uno que ya
existe). NO decide qué se sube, cuándo, ni a qué carpeta: eso es de la pieza 8.
Nada en este archivo lee la base ni escribe en ella.

LA CREDENCIAL. Es OAuth **de usuario** con un único alcance, `drive.file`
(`ALCANCE`): Lucy solo ve y toca lo que ella misma crea. No es una cuenta de
servicio porque esa no puede guardar archivos en «Mi unidad». Se vive en tres
variables de entorno del servicio `lucy` de Railway (`VARIABLES`), que se leen
en CADA llamada, no al importar: así Lucy arranca aunque falten, y lo que falla,
con su causa, es la subida. El REPOSITORIO ES PÚBLICO: aquí no hay ni un
identificador de cliente, ni un secreto, ni un token, ni el id de ninguna
carpeta.

LA PUERTA. `_pedir` es la ÚNICA función que crea un cliente HTTP, y lo hace
con `follow_redirects=False`, un tiempo máximo (`TIEMPO_LIMITE`) y nada más. Por
ella pasan también los pedidos del token (`_token`). Un redireccionamiento NO se
sigue: se cuenta como «Drive no contestó».

CÓMO SE DICE UN FALLO. Cada fallo sube una excepción propia (`DriveError` y sus
cuatro hijas) cuyo texto es una frase FIJA (`causa`), la que la pantalla puede
enseñar. Nunca el texto de Google, nunca una URL, nunca un valor de credencial:
las excepciones se levantan `from None` y los registros llevan la operación y el
código de estado, y nada de lo que vuelve de Google.

LO QUE ESTE ARCHIVO SUPONE DE LA API DE DRIVE v3 (de la documentación pública
—«Files: create/list/update», «Upload file data», «Search for files» y «Resolve
errors»—, NO medido contra Google desde aquí: ningún pedido real salió al
escribirlo; la frontera de cada punto está en `tests/test_drive.py`):

  · crear carpeta: `POST /drive/v3/files` con JSON `{name, mimeType:
    application/vnd.google-apps.folder, parents:[id]}`; contesta `{id}` con
    `fields=id`.
  · buscar: `GET /drive/v3/files?q=…` con `'<padre>' in parents and name = '…'
    and trashed = false`, y `fields=files(id,name)`; los `'` y `\\` del nombre
    se escapan con `\\`.
  · renombrar: `PATCH /drive/v3/files/{id}` con `{name}`.
  · subir un archivo nuevo: `POST /upload/drive/v3/files?uploadType=multipart`
    con cuerpo `multipart/related` (primero el JSON de metadatos, luego los
    bytes).
  · reemplazar el contenido: `PATCH /upload/drive/v3/files/{id}?uploadType=media`
    con los bytes. Que Drive guarde la versión anterior en su historial NO se
    comprobó.
  · el token se pide con `POST https://oauth2.googleapis.com/token`, formulario
    `grant_type=refresh_token`, y contesta `{access_token, expires_in}`; un
    refresh token vencido o revocado contesta 400 (`invalid_grant`).
  · 401 y 403 son «sin permiso», salvo un 403 cuyo motivo sea de límite de
    pedidos, que se trata como «no contestó».

LO QUE QUEDA FUERA, dicho: no se guarda el token de acceso entre pedidos (cada
pedido pide uno; cuesta una ida más, y no hay estado que se pueda ensuciar); no
se pagina la búsqueda (hasta `TOPE_BUSQUEDA` resultados); no hay reintentos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets

import httpx

log = logging.getLogger("lucy.drive")

# El único permiso que se pide: lo que Lucy crea, y nada más del Drive.
ALCANCE = "https://www.googleapis.com/auth/drive.file"

API = "https://www.googleapis.com/drive/v3"
SUBIDA = "https://www.googleapis.com/upload/drive/v3"
URL_TOKEN = "https://oauth2.googleapis.com/token"

# Las tres variables del servicio `lucy`: se leen por su nombre desde aquí.
VARIABLES = ("GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET",
             "GOOGLE_DRIVE_REFRESH_TOKEN")

# Cuánto se espera a Google por pedido. Es un número elegido (el diseño, §3.7),
# no medido.
TIEMPO_LIMITE = 15.0

# Hasta cuántos resultados devuelve `buscar_en`. Sin paginar: una carpeta de
# Lucy no tiene cien archivos con el mismo nombre.
TOPE_BUSQUEDA = 100

MIME_CARPETA = "application/vnd.google-apps.folder"

# Un id de Drive va dentro de una URL o de una búsqueda: solo estos caracteres
# (y el alias «root»). Algo distinto no puede ser un id.
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Los motivos de un 403 que son «vas muy rápido», no «no tienes permiso».
_MOTIVOS_DE_LIMITE = frozenset({
    "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
    "sharingRateLimitExceeded"})


class DriveError(RuntimeError):
    """Base: el texto de la excepción es `causa`, una frase fija."""
    causa = "Drive falló"

    def __init__(self):
        super().__init__(self.causa)


class DriveNoContesta(DriveError):
    causa = "Drive no contestó"


class DriveSinPermiso(DriveError):
    causa = "Drive no dio permiso"


class DriveSinConfigurar(DriveError):
    causa = "Lucy no tiene configurado Drive"


class DriveNoEsta(DriveError):
    causa = "la carpeta o el archivo ya no está en Drive"


def _id(valor) -> str:
    """Un id que puede ir en una URL o en una búsqueda. Si no lo es, no puede
    existir en Drive: se dice `DriveNoEsta` y no sale ningún pedido."""
    if isinstance(valor, str) and (valor == "root" or _ID.match(valor)):
        return valor
    raise DriveNoEsta()


def _nombre(valor) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("el nombre tiene que ser un texto con algo dentro")
    return valor


def _escapar(texto: str) -> str:
    """Para ir entre comillas simples en el `q` de una búsqueda."""
    return texto.replace("\\", "\\\\").replace("'", "\\'")


def _credenciales() -> tuple[str, str, str]:
    # Claves escritas una por una: la guarda del repositorio (`tests/test_noco_
    # lectura.py`) solo deja leer el entorno por una clave constante.
    # `test_las_variables_leidas_son_las_declaradas` exige que sean `VARIABLES`.
    valores = (os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "").strip(),
               os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "").strip(),
               os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip())
    faltan = [n for n, v in zip(VARIABLES, valores) if not v]
    if faltan:
        # Nombres, nunca valores.
        log.warning("drive: falta configurar %s", ", ".join(faltan))
        raise DriveSinConfigurar()
    return valores


def _motivos(cuerpo) -> set:
    """Los `reason` de un error de Drive (`{"error": {"errors": [...]}}`)."""
    try:
        return {e.get("reason") for e in cuerpo["error"]["errors"]}
    except (KeyError, TypeError, AttributeError):
        return set()


async def _pedir(operacion: str, metodo: str, url: str, *, autorizado=True,
                 params=None, json_=None, contenido=None, cabeceras=None,
                 formulario=None) -> dict:
    """LA PUERTA DE SALIDA: lo único de este repositorio que le habla a Drive
    (y a su servidor de tokens). Devuelve el JSON de una respuesta 2xx; todo lo
    demás sube como una de las cuatro excepciones.
    """
    credencial = None
    if autorizado:
        credencial = await _token()
    cab = dict(cabeceras or {})
    if credencial is not None:
        cab["Authorization"] = "Bearer " + credencial
    try:
        async with httpx.AsyncClient(timeout=TIEMPO_LIMITE,
                                     follow_redirects=False) as cliente:
            respuesta = await asyncio.wait_for(
                cliente.request(metodo, url, params=params, json=json_,
                                content=contenido, data=formulario,
                                headers=cab),
                TIEMPO_LIMITE)
    except (httpx.HTTPError, asyncio.TimeoutError):
        log.warning("drive: %s sin respuesta", operacion)
        raise DriveNoContesta() from None
    estado = respuesta.status_code
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = None
    if 200 <= estado < 300:
        if isinstance(cuerpo, dict):
            return cuerpo
        log.warning("drive: %s contestó %s sin JSON", operacion, estado)
        raise DriveNoContesta()
    log.warning("drive: %s contestó %s", operacion, estado)
    if estado == 401 or (estado == 400 and operacion == "token"):
        raise DriveSinPermiso()
    if estado == 403:
        if _motivos(cuerpo) & _MOTIVOS_DE_LIMITE:
            raise DriveNoContesta()
        raise DriveSinPermiso()
    if estado == 404:
        raise DriveNoEsta()
    raise DriveNoContesta()


async def _token() -> str:
    """Un access token nuevo, pedido con el refresh token de Lucy."""
    cliente, secreto, refresco = _credenciales()
    cuerpo = await _pedir(
        "token", "POST", URL_TOKEN, autorizado=False,
        formulario={"grant_type": "refresh_token", "client_id": cliente,
                    "client_secret": secreto, "refresh_token": refresco})
    token = cuerpo.get("access_token")
    if not isinstance(token, str) or not token:
        log.warning("drive: el servidor de tokens no dio access_token")
        raise DriveNoContesta()
    return token


def _id_de(cuerpo: dict) -> str:
    ident = cuerpo.get("id")
    if not isinstance(ident, str) or not _ID.match(ident):
        log.warning("drive: la respuesta no trae un id que sirva")
        raise DriveNoContesta()
    return ident


async def crear_carpeta(nombre: str, padre: str) -> str:
    """Crea una carpeta dentro de `padre` (un id, o `root`) y devuelve su id."""
    cuerpo = await _pedir(
        "crear_carpeta", "POST", f"{API}/files", params={"fields": "id"},
        json_={"name": _nombre(nombre), "mimeType": MIME_CARPETA,
               "parents": [_id(padre)]})
    return _id_de(cuerpo)


async def buscar_en(carpeta: str, nombre: str) -> list[dict]:
    """Los archivos o carpetas, fuera de la papelera, que están DENTRO de
    `carpeta` y se llaman exactamente `nombre`: `[{"id", "nombre"}, ...]`,
    del más viejo al más nuevo. Una lista vacía quiere decir que Drive
    contestó y no hay nadie; si Drive no contesta, sube la excepción.
    """
    consulta = (f"'{_id(carpeta)}' in parents and "
                f"name = '{_escapar(_nombre(nombre))}' and trashed = false")
    cuerpo = await _pedir(
        "buscar", "GET", f"{API}/files",
        params={"q": consulta, "fields": "files(id,name)", "spaces": "drive",
                "orderBy": "createdTime", "pageSize": str(TOPE_BUSQUEDA)})
    archivos = cuerpo.get("files")
    if not isinstance(archivos, list):
        log.warning("drive: la búsqueda no trae `files`")
        raise DriveNoContesta()
    salida = []
    for a in archivos:
        if (isinstance(a, dict) and isinstance(a.get("id"), str)
                and _ID.match(a["id"]) and isinstance(a.get("name"), str)):
            salida.append({"id": a["id"], "nombre": a["name"]})
    return salida


async def renombrar(ident: str, nombre: str) -> str:
    """Le cambia el nombre a un archivo o carpeta y devuelve su id."""
    cuerpo = await _pedir(
        "renombrar", "PATCH", f"{API}/files/{_id(ident)}",
        params={"fields": "id"}, json_={"name": _nombre(nombre)})
    return _id_de(cuerpo)


def _multipart(metadatos: dict, datos: bytes, mime: str) -> tuple[bytes, str]:
    limite = "lucy" + secrets.token_hex(16)
    cuerpo = (
        f"--{limite}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        .encode() + json.dumps(metadatos).encode()
        + f"\r\n--{limite}\r\nContent-Type: {mime}\r\n\r\n".encode()
        + datos + f"\r\n--{limite}--".encode())
    return cuerpo, f"multipart/related; boundary={limite}"


async def subir_pdf(carpeta: str, nombre: str, datos: bytes,
                    archivo_id: str | None = None) -> str:
    """Sube un PDF y devuelve el id del archivo en Drive.

    Sin `archivo_id` crea uno nuevo dentro de `carpeta`. Con `archivo_id`
    REEMPLAZA el contenido de ese mismo archivo (el enlace no cambia) y no crea
    nada; si Drive contesta con otro id, no se acepta como respuesta buena.
    """
    if not isinstance(datos, (bytes, bytearray)) or not datos:
        raise ValueError("los datos del PDF tienen que ser bytes con algo dentro")
    datos = bytes(datos)
    if archivo_id is None:
        cuerpo, tipo = _multipart(
            {"name": _nombre(nombre), "parents": [_id(carpeta)],
             "mimeType": "application/pdf"}, datos, "application/pdf")
        respuesta = await _pedir(
            "subir", "POST", f"{SUBIDA}/files",
            params={"uploadType": "multipart", "fields": "id"},
            contenido=cuerpo, cabeceras={"Content-Type": tipo})
        return _id_de(respuesta)
    ident = _id(archivo_id)
    respuesta = await _pedir(
        "reemplazar", "PATCH", f"{SUBIDA}/files/{ident}",
        params={"uploadType": "media", "fields": "id"},
        contenido=datos, cabeceras={"Content-Type": "application/pdf"})
    if _id_de(respuesta) != ident:
        log.warning("drive: el reemplazo contestó con otro id")
        raise DriveNoContesta()
    return ident
