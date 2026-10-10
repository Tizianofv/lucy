"""Un doble de la App de registro para probar por el camino de producción: un
servidor HTTP de verdad en 127.0.0.1, y Lucy lo llama con `httpx` como llamaría
a la App. Trae dos puertas: el canje del pase (`/entrar-cds`) y las sesiones de
una ficha (parte 9 de la página de un proyecto).

DE DÓNDE SALE LO QUE AFIRMA este doble (y por eso no inventa nada): del contrato
que fija la prueba de la App, `tests/test_pase_proyectos.py` de su repositorio
(clase `ElContratoDelCanje`), y de su `canjear_pase` en `server.py`:

  · `POST /api/pase/canjear` con `{"codigo": ...}`, sin `X-Auth`;
  · 200 `{"nivel", "chat", "tecnico_id"}` y NADA más; `nivel` es `total` o `ver`;
    `chat` es TEXTO (el de la ficha) o vacío si la ficha no lo tiene;
  · 404 `{"error": "no"}` por CUALQUIER fallo, sin decir cuál;
  · el boleto se borra al leerlo, sirva o no: el segundo intento da 404.
La vida de 60 s y la baja de la ficha son cosas de la App y no se imitan acá.

Y de la puerta de las sesiones (parte 8 de la App, publicada el 9-oct-2026; el
contrato es el que la sala le pasó a Lucy en el encargo de la parte 9):

  · `GET /api/lucy/sesiones?persona=<Id de ficha>` con la cabecera
    `X-Lucy-Llave`; 401 si la llave no es la que la App tiene puesta;
  · 200 `{"puede_ligar", "motivo_sin_ligar", "sesiones", "no_halladas"}`;
    `puede_ligar` falso trae `motivo_sin_ligar` (`sin_telefono`, `ficha_inexistente`,
    `sin_persona`), que NO es lo mismo que una lista vacía;
  · la ruta acepta además `&sesion=<ref>` repetido (parte 13): devuelve, en la MISMA respuesta,
    las de la ficha y las pedidas una por una (sin repetir), y en `no_halladas` los `ref` pedidos
    que no existen. Sin `persona` (o con una ficha que no se puede ligar) igual devuelve las
    pedidas; `puede_ligar` es falso con su `motivo_sin_ligar` (`sin_persona`);
  · cada sesión trae `ref`, `codigo`, `es_trabajo`, `fecha` («AAAA-MM-DD»), `sala`,
    `sala_mostrar`, `horas`, `servicio`, `atendio`, `asignado_a`, `estado`,
    `cancelada` y el dinero (`total`, `abonado`, `saldo`);
  · 502 `{"error": "no_se_pudo_leer"}` si la App no pudo leer su base.

Y de la puerta de la búsqueda (parte 12 de la App; el contrato es el que la sala le pasó a Lucy
en el encargo de la parte 13):

  · `GET /api/lucy/sesiones/buscar?q=<texto>` con la misma cabecera; 401 sin la llave buena;
  · 200 `{"sesiones": [{los mismos campos de una sesión, más "nombre"}], "hay_mas": bool}`; un
    texto de menos de 2 caracteres devuelve la lista vacía;
  · 502 `{"error": "no_se_pudo_leer"}` si no pudo leer su base.
LO QUE ESTE DOBLE NO IMITA, y por eso no lo afirma: la regla con la que la App
reconoce las sesiones de una ficha (por teléfono), y de dónde saca cada cifra.

⚠️ LO QUE NO SE PUDO COMPROBAR AUTOMÁTICAMENTE: que este doble siga igual a la
App de verdad. Esas dos pruebas viven en repositorios distintos y una no puede
leer la otra. Si el contrato de la App cambia, hay que cambiar este archivo a
mano, y el cambio lo tiene que pedir quien cambie la App.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit


class AppDeRegistro:
    def __init__(self):
        self.boletos: dict[str, dict] = {}
        self.pedidos: list[dict] = []       # lo que le llegó, para que la prueba lo mire
        # Para imitar fallas que la App de verdad no tiene hoy (500, basura, lentitud):
        self.forzada: tuple | None = None   # (HTTP, cuerpo, tipo, demora[, {cabeceras}])
        # La puerta de las sesiones: la llave que la App tiene puesta (vacía = cerrada) y lo que
        # contesta. `sesiones` es fijo a propósito: las pruebas miran lo que Lucy HACE con él.
        self.llave = ""
        self.sesiones: dict = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": []}
        # Las sesiones que la App tiene pero que NO son de esa ficha (parte 13): por acá salen las
        # que se piden una por una (`&sesion=`). Una pedida que no esté ni acá ni en `sesiones` va
        # a `no_halladas`.
        self.otras: list[dict] = []
        # La puerta de la búsqueda (parte 12): lo que contesta `hay_mas` y las candidatas.
        self.busqueda: dict = {"sesiones": [], "hay_mas": False}
        self._lock = threading.Lock()
        doble = self

        class _Manejador(BaseHTTPRequestHandler):
            def log_message(self, *a):          # silencio
                pass

            def do_POST(self):
                largo = int(self.headers.get("Content-Length") or 0)
                crudo = self.rfile.read(largo) if largo else b""
                with doble._lock:
                    doble.pedidos.append({"ruta": self.path, "metodo": "POST",
                                          "cuerpo": crudo, "cabeceras": dict(self.headers)})
                    forzada = doble.forzada
                if forzada is not None:
                    estado, cuerpo, tipo, demora, *resto = forzada
                    if demora:
                        time.sleep(demora)
                    return self._responder(estado, cuerpo, tipo, resto[0] if resto else {})
                if self.path != "/api/pase/canjear":
                    return self._responder(404, b'{"error": "no"}')
                try:
                    cuerpo = json.loads(crudo or b"{}")
                except ValueError:
                    cuerpo = {}
                codigo = cuerpo.get("codigo") if isinstance(cuerpo, dict) else None
                with doble._lock:
                    b = doble.boletos.pop(codigo, None) if isinstance(codigo, str) else None
                if b is None:
                    return self._responder(404, b'{"error": "no"}')
                return self._responder(200, json.dumps(b).encode())

            def do_GET(self):
                with doble._lock:           # todo lo que llega se anota, sea la ruta que sea
                    doble.pedidos.append({"ruta": self.path, "metodo": "GET", "cuerpo": b"",
                                          "cabeceras": dict(self.headers)})
                    forzada = doble.forzada
                    llave = doble.llave
                    cuerpo_sesiones = dict(doble.sesiones)
                    otras = list(doble.otras)
                    busqueda = dict(doble.busqueda)
                if forzada is not None:
                    estado, cuerpo, tipo, demora, *resto = forzada
                    if demora:
                        time.sleep(demora)
                    return self._responder(estado, cuerpo, tipo, resto[0] if resto else {})
                partes = urlsplit(self.path)
                if partes.path not in ("/api/lucy/sesiones", "/api/lucy/sesiones/buscar"):
                    return self._responder(404, b'{"error": "no"}')
                if not llave or self.headers.get("X-Lucy-Llave") != llave:
                    return self._responder(401, b'{"error": "no"}')
                consulta = parse_qsl(partes.query)
                if partes.path == "/api/lucy/sesiones/buscar":
                    q = next((v for k, v in consulta if k == "q"), "")
                    if len(q) < 2:
                        return self._responder(200, json.dumps(
                            {"sesiones": [], "hay_mas": False}).encode())
                    return self._responder(200, json.dumps(busqueda).encode())
                return self._responder(200, json.dumps(
                    self._de_una_ficha(consulta, cuerpo_sesiones, otras)).encode())

            def _de_una_ficha(self, consulta, cuerpo_sesiones, otras):
                """Lo que la App contesta en `/api/lucy/sesiones`: las de la ficha (si se puede ligar)
                y las pedidas una por una (`&sesion=`), sin repetir, con las que no tiene en
                `no_halladas`. Sin `persona` no puede ligar ninguna: `sin_persona` (pero las pedidas
                las devuelve igual)."""
                persona = next((v for k, v in consulta if k == "persona"), None)
                filas = list(cuerpo_sesiones.get("sesiones") or [])
                refs = {str(f.get("ref")) for f in filas if isinstance(f, dict)}
                no_halladas = []
                for k, ref in consulta:
                    if k != "sesion" or ref in refs:
                        continue
                    hallada = next((f for f in otras if str(f.get("ref")) == ref), None)
                    if hallada is None:
                        no_halladas.append(ref)
                    else:
                        filas.append(hallada)
                        refs.add(ref)
                if persona is None:
                    return {"puede_ligar": False, "motivo_sin_ligar": "sin_persona",
                            "sesiones": filas, "no_halladas": no_halladas}
                return {"puede_ligar": cuerpo_sesiones.get("puede_ligar"),
                        "motivo_sin_ligar": cuerpo_sesiones.get("motivo_sin_ligar", ""),
                        "sesiones": filas, "no_halladas": no_halladas}

            def _responder(self, estado, cuerpo, tipo="application/json", cabeceras=None):
                self.send_response(estado)
                for k, v in (cabeceras or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                try:
                    self.wfile.write(cuerpo)
                except OSError:
                    pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Manejador)
        self._srv.daemon_threads = True
        self._hilo = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._hilo.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def emitir(self, nivel: str, chat, tecnico_id: int = 7) -> str:
        """Un boleto como los de la App: 256 bits al azar."""
        codigo = secrets.token_urlsafe(32)
        with self._lock:
            self.boletos[codigo] = {"nivel": nivel, "chat": "" if chat is None else str(chat),
                                    "tecnico_id": tecnico_id}
        return codigo

    def cerrar(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._hilo.join(timeout=5)
