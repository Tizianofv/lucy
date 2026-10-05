"""Un doble de la App de registro, SOLO la puerta del canje, para probar
`/entrar-cds` por el camino de producción: un servidor HTTP de verdad en
127.0.0.1, y `web.app` lo llama con `httpx` como llamaría a la App.

DE DÓNDE SALE LO QUE AFIRMA este doble (y por eso no inventa nada): del contrato
que fija la prueba de la App, `tests/test_pase_proyectos.py` de su repositorio
(clase `ElContratoDelCanje`), y de su `canjear_pase` en `server.py`:

  · `POST /api/pase/canjear` con `{"codigo": ...}`, sin `X-Auth`;
  · 200 `{"nivel", "chat", "tecnico_id"}` y NADA más; `nivel` es `total` o `ver`;
    `chat` es TEXTO (el de la ficha) o vacío si la ficha no lo tiene;
  · 404 `{"error": "no"}` por CUALQUIER fallo, sin decir cuál;
  · el boleto se borra al leerlo, sirva o no: el segundo intento da 404.
La vida de 60 s y la baja de la ficha son cosas de la App y no se imitan acá.

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


class AppDeRegistro:
    def __init__(self):
        self.boletos: dict[str, dict] = {}
        self.pedidos: list[dict] = []       # lo que le llegó, para que la prueba lo mire
        # Para imitar fallas que la App de verdad no tiene hoy (500, basura, lentitud):
        self.forzada: tuple[int, bytes, str, float] | None = None   # (HTTP, cuerpo, tipo, demora)
        self._lock = threading.Lock()
        doble = self

        class _Manejador(BaseHTTPRequestHandler):
            def log_message(self, *a):          # silencio
                pass

            def do_POST(self):
                largo = int(self.headers.get("Content-Length") or 0)
                crudo = self.rfile.read(largo) if largo else b""
                with doble._lock:
                    doble.pedidos.append({"ruta": self.path, "cuerpo": crudo,
                                          "cabeceras": dict(self.headers)})
                    forzada = doble.forzada
                if forzada is not None:
                    estado, cuerpo, tipo, demora = forzada
                    if demora:
                        time.sleep(demora)
                    return self._responder(estado, cuerpo, tipo)
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

            def _responder(self, estado, cuerpo, tipo="application/json"):
                self.send_response(estado)
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
