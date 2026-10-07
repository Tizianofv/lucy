"""Un navegador de mentira: la cookie del recibo de los avisos (`web/avisos.py`) viaja
de un pedido al siguiente aunque cada pedido use otro `TestClient`.

Las pruebas de las pantallas hacen «POST sin seguir la redirección» y después «GET de
la dirección a la que mandó» con un cliente NUEVO (`ver(...)`): en un navegador de
verdad es la misma persona y la misma cookie. Este cliente la comparte entre todos
los de su misma prueba y nada más (la sesión sigue siendo de cada cliente). Lo que NO
hace: inventar recibos. Solo lleva el que la aplicación puso en una respuesta, así
que una dirección escrita a mano sigue sin tenerlo. `tests/conftest.py` lo vacía
antes de cada prueba (`reiniciar`)."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from web import avisos
from web.avisos import COOKIE


class Navegador(TestClient):
    recibo: str | None = None

    @classmethod
    def reiniciar(cls) -> None:
        cls.recibo = None

    def _borrar_la_cookie(self) -> None:
        for c in list(self.cookies.jar):
            if c.name == COOKIE:
                self.cookies.jar.clear(c.domain, c.path, c.name)

    def request(self, *a, **k):
        self._borrar_la_cookie()
        if Navegador.recibo:
            # el mismo dominio que le da el jar a lo que pone una respuesta
            self.cookies.set(COOKIE, Navegador.recibo, domain="testserver.local", path="/")
        r = super().request(*a, **k)
        puestas = [c.value for c in self.cookies.jar if c.name == COOKIE and c.value]
        Navegador.recibo = puestas[-1] if puestas else None
        return r


def dar_recibo(ruta: str, **consulta) -> None:
    """Deja en el navegador el recibo que la aplicación le habría dado si un POST
    hubiera redirigido a `ruta` con esos parámetros: la FIRMA REAL, con las mismas
    funciones que usa la puerta. Es un atajo solo para las pruebas que miden CÓMO SE
    PINTA un aviso (su texto, su estado comprobado); las que miden que el aviso sale
    de verdad de la acción hacen el POST. Sin llamarlo, una dirección escrita a mano
    no trae nada."""
    import web.app as panel
    av = avisos.avisos_de(avisos._ruta_get(panel.app, ruta))
    pares = [(k, str(v)) for k, v in consulta.items()]
    nuevo = avisos._recibo(avisos.clave_de(ruta, pares, av), int(time.time()) + avisos.VIDA_RECIBO)
    Navegador.recibo = "~".join(r for r in (Navegador.recibo, nuevo) if r)
