"""Lo que la persona escribió en la ventana de una cotización y el servidor RECHAZÓ (17-oct-2026).

Antes, un guardado rechazado redirigía a una ventana en blanco: se perdía todo lo escrito (también el borrador que armó
la IA). Un rechazo no puede ser silencioso ni costar lo escrito. Pero lo escrito NO puede viajar en la dirección ni quedar
en el registro (datos de un cliente y precios): así que se guarda AQUÍ, en la memoria del proceso, por unos minutos, y la
redirección lleva solo una contraseña de un uso corto (`?rechazo=`), sin ningún dato.

  · La entrada sabe de QUIÉN es (`chat`), de qué proyecto (`pid`) y de qué cotización (`cid`: `None` es la nueva). Quien
    la pide tiene que ser la misma sesión, el mismo proyecto y la misma cotización: otra persona, otro proyecto u otra
    cotización con la misma contraseña no recibe nada.
  · Dura `VIDA` segundos y caben `MAXIMO` entradas (la más vieja sale primero). No se escribe en la base ni en el
    disco, y nada de lo escrito entra en un mensaje del registro.
  · NO es la puerta de nada: no decide qué vale. Solo devuelve lo mismo que llegó, para volver a dibujarlo. El que
    decide sigue siendo `db.guardar_cotizacion`.

FRONTERA: la memoria es de UN proceso. Si Lucy corriera en dos copias a la vez y el rechazo cayera en una y la lectura
en la otra, la ventana saldría en blanco (con su aviso, que no depende de esto). Cuántas copias corren en Railway no está
comprobado desde el repositorio."""
from __future__ import annotations

import copy
import secrets
import threading
import time

VIDA = 900                  # segundos: lo que tarda una persona en volver a mirar su ventana
MAXIMO = 64                 # entradas a la vez
LARGO_MAXIMO = 400_000      # caracteres de lo escrito; más que eso no se guarda (el formulario ya trae límites por casilla)

_guardado: dict[str, tuple[float, int, int, int | None, dict]] = {}
_cerrojo = threading.Lock()


def _largo(valor) -> int:
    if isinstance(valor, str):
        return len(valor)
    if isinstance(valor, dict):
        return sum(_largo(k) + _largo(v) for k, v in valor.items())
    if isinstance(valor, (list, tuple)):
        return sum(_largo(v) for v in valor)
    return 1


def guardar(chat: int, pid: int, cid: int | None, campos: dict) -> str | None:
    """Guarda lo escrito (la forma de `cotizaciones.campos_del_formulario`) y devuelve la contraseña, o `None` si no se
    guardó (demasiado grande). Nunca lanza."""
    try:
        if _largo(campos) > LARGO_MAXIMO:
            return None
        clave = secrets.token_urlsafe(12)
        ahora = time.monotonic()
        with _cerrojo:
            for k in [k for k, v in _guardado.items() if v[0] <= ahora]:
                del _guardado[k]
            while len(_guardado) >= MAXIMO:
                del _guardado[next(iter(_guardado))]
            _guardado[clave] = (ahora + VIDA, chat, pid, cid, copy.deepcopy(campos))
        return clave
    except Exception:
        return None


def leer(chat: int, pid: int, cid: int | None, clave) -> dict | None:
    """Lo escrito de esa contraseña, si es de esta sesión, de este proyecto y de esta cotización y no venció; si no,
    `None`. No gasta la contraseña: recargar la página sigue mostrando lo escrito hasta que venza."""
    if not isinstance(clave, str) or not clave:
        return None
    with _cerrojo:
        entrada = _guardado.get(clave)
    if entrada is None:
        return None
    vence, de_chat, de_pid, de_cid, campos = entrada
    if vence <= time.monotonic() or (de_chat, de_pid, de_cid) != (chat, pid, cid):
        return None
    return copy.deepcopy(campos)
