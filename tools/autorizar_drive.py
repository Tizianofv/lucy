"""Autoriza a Lucy en el Drive del estudio, UNA vez (Cotizaciones, pieza 7).

Es un guion LOCAL, de una sola vez, que corre la sala con la cuenta del estudio
delante. Hace esto y nada más:

  1. Imprime la dirección de Google donde la persona pulsa «Permitir». El único
     permiso que pide es `drive.file` (`drive.ALCANCE`): Lucy solo verá lo que
     ella misma cree.
  2. Recibe el código que Google le devuelve a esta misma máquina
     (`http://127.0.0.1:<puerto>`, el puerto lo elige el sistema) y comprueba
     el `state` que él mismo inventó.
  3. Cambia el código por un refresh token y lo guarda en un archivo NUEVO
     (nunca sobrescribe uno que exista), con permisos de solo el dueño, dentro
     de la carpeta que se le da en `--salida`. **El token no se imprime**: ni
     entero ni cortado. Tampoco el secreto del cliente, ni el código, ni el
     texto que conteste Google.
  4. Con `--crear-proyectos`, además busca o crea la carpeta «Proyectos» en la
     raíz del Drive y guarda su id (no es secreto) en otro archivo de `--salida`.
     Con `drive.file` la carpeta queda suelta en «Mi unidad»: moverla a
     «CDS y ACD» es un paso a mano de la casa.

El REPOSITORIO ES PÚBLICO: el identificador y el secreto del cliente OAuth se
leen del entorno (`GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`) y no
están escritos en ningún sitio de aquí.

Uso (lo corre la sala, no un agente de Lucy):

    GOOGLE_DRIVE_CLIENT_ID=... GOOGLE_DRIVE_CLIENT_SECRET=... \\
        python3 tools/autorizar_drive.py --salida "<carpeta>" [--crear-proyectos]

Lo que deja en `--salida`: `GOOGLE_DRIVE_REFRESH_TOKEN` (el valor de la variable
de Railway del mismo nombre) y, si se pidió, `LUCY_DRIVE_CARPETA_PROYECTOS`.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import http.server
import os
import secrets
import sys
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drive  # noqa: E402

URL_PERMISO = "https://accounts.google.com/o/oauth2/v2/auth"
ESPERA_SEGUNDOS = 300
MAX_PEDIDOS = 20          # el navegador también pide /favicon.ico y similares

NOMBRE_TOKEN = "GOOGLE_DRIVE_REFRESH_TOKEN"
NOMBRE_CARPETA = "LUCY_DRIVE_CARPETA_PROYECTOS"


class Falla(Exception):
    """Un fallo que se le dice a la persona con una frase fija."""


def desafio(verificador: str) -> str:
    """PKCE (S256): el hash del verificador, en base64 de URL y sin relleno."""
    h = hashlib.sha256(verificador.encode("ascii")).digest()
    return base64.urlsafe_b64encode(h).rstrip(b"=").decode("ascii")


def url_de_permiso(cliente_id: str, redireccion: str, estado: str,
                   verificador: str) -> str:
    consulta = urllib.parse.urlencode({
        "client_id": cliente_id, "redirect_uri": redireccion,
        "response_type": "code", "scope": drive.ALCANCE,
        "access_type": "offline", "prompt": "consent", "state": estado,
        "code_challenge": desafio(verificador),
        "code_challenge_method": "S256"})
    return f"{URL_PERMISO}?{consulta}"


def esperar_codigo(servidor: http.server.HTTPServer, estado: str) -> str:
    """Atiende pedidos locales hasta recibir `?code=…&state=<el nuestro>`."""
    recibido: dict = {}

    class Manejador(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            codigo = (q.get("code") or [""])[0]
            otro = (q.get("state") or [""])[0]
            bueno = bool(codigo) and hmac.compare_digest(
                otro.encode(), estado.encode())
            self.send_response(200 if bueno else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Listo, ya puedes cerrar esta ventana." if bueno
                else b"Este pedido no es el de la autorizacion.")
            if bueno:
                recibido["codigo"] = codigo

        def log_message(self, *a):          # nada de la URL (lleva el código)
            pass

    servidor.timeout = ESPERA_SEGUNDOS
    servidor.RequestHandlerClass = Manejador
    for _ in range(MAX_PEDIDOS):
        servidor.handle_request()
        if "codigo" in recibido:
            return recibido["codigo"]
    raise Falla("No llegó el código de Google a tiempo.")


async def canjear(cliente_id: str, secreto: str, codigo: str,
                  redireccion: str, verificador: str) -> str:
    """El código por un refresh token. No devuelve ni imprime nada más."""
    try:
        async with httpx.AsyncClient(timeout=drive.TIEMPO_LIMITE,
                                     follow_redirects=False) as c:
            r = await c.post(drive.URL_TOKEN, data={
                "grant_type": "authorization_code", "code": codigo,
                "client_id": cliente_id, "client_secret": secreto,
                "redirect_uri": redireccion, "code_verifier": verificador})
    except httpx.HTTPError:
        raise Falla("Google no contestó al canjear el código.") from None
    if r.status_code != 200:
        raise Falla(f"Google rechazó el código (HTTP {r.status_code}).")
    try:
        cuerpo = r.json()
    except ValueError:
        raise Falla("Google contestó algo que no es JSON.") from None
    token = cuerpo.get("refresh_token") if isinstance(cuerpo, dict) else None
    if not isinstance(token, str) or not token:
        raise Falla("Google no devolvió refresh token.")
    if cuerpo.get("scope") != drive.ALCANCE:
        raise Falla("Google concedió un permiso distinto de drive.file.")
    return token


def guardar(carpeta: str, nombre: str, valor: str) -> str:
    """Escribe `valor` en un archivo NUEVO, legible solo por el dueño."""
    ruta = os.path.join(carpeta, nombre)
    try:
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Falla(f"Ya existe {ruta}; no lo sobrescribo.") from None
    with os.fdopen(fd, "w") as f:
        f.write(valor)
    return ruta


async def carpeta_proyectos(cliente_id: str, secreto: str, token: str) -> str:
    """Busca «Proyectos» en la raíz (entre lo que Lucy creó) o la crea."""
    os.environ["GOOGLE_DRIVE_CLIENT_ID"] = cliente_id
    os.environ["GOOGLE_DRIVE_CLIENT_SECRET"] = secreto
    os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"] = token
    try:
        hay = await drive.buscar_en("root", "Proyectos")
        if hay:
            return hay[0]["id"]
        return await drive.crear_carpeta("Proyectos", "root")
    except drive.DriveError as e:
        raise Falla(f"Drive: {e}") from None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--salida", required=True,
                    help="carpeta (que ya existe) donde se guardan los archivos")
    ap.add_argument("--crear-proyectos", action="store_true")
    args = ap.parse_args(argv)
    cliente_id = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "").strip()
    secreto = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
    try:
        if not cliente_id or not secreto:
            raise Falla("Faltan GOOGLE_DRIVE_CLIENT_ID o "
                        "GOOGLE_DRIVE_CLIENT_SECRET en el entorno.")
        if not os.path.isdir(args.salida):
            raise Falla("La carpeta de --salida no existe.")
        for n in (NOMBRE_TOKEN, NOMBRE_CARPETA if args.crear_proyectos else ""):
            if n and os.path.exists(os.path.join(args.salida, n)):
                raise Falla(f"Ya existe {n} en --salida; no lo sobrescribo.")
        estado = secrets.token_urlsafe(24)
        verificador = secrets.token_urlsafe(48)
        servidor = http.server.HTTPServer(("127.0.0.1", 0),
                                          http.server.BaseHTTPRequestHandler)
        try:
            redireccion = f"http://127.0.0.1:{servidor.server_address[1]}"
            print("Abre esta direccion con la cuenta del estudio y pulsa "
                  "Permitir:\n")
            print(url_de_permiso(cliente_id, redireccion, estado, verificador))
            print("\nEsperando a Google...", flush=True)
            codigo = esperar_codigo(servidor, estado)
        finally:
            servidor.server_close()
        token = asyncio.run(canjear(cliente_id, secreto, codigo, redireccion,
                                    verificador))
        print("Guardado:", guardar(args.salida, NOMBRE_TOKEN, token))
        if args.crear_proyectos:
            ident = asyncio.run(carpeta_proyectos(cliente_id, secreto, token))
            print("Guardado:", guardar(args.salida, NOMBRE_CARPETA, ident))
    except Falla as e:
        print("ERROR:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
