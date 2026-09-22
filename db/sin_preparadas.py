"""UN solo sitio apaga las consultas preparadas, para el proceso entero.

SEGUNDA VUELTA DEL ENCARGO 3, 22-sep-2026 — hallazgo del testigo sobre
`55ad0a2`. La primera versión ponía `prepare_threshold=None` en cada
llamada que abre una conexión (`db/db.py`, `db/backup.py`,
`tools/rellenar_categorias.py`, `tools/verificar_respaldo.py`), y una
prueba que recorría el árbol de sintaxis buscando ese texto en cada sitio.
El testigo escribió un archivo nuevo —`tools/exportar_movimientos.py`,
descartado, nunca llegó a existir de verdad en este repo— con
`from psycopg import connect as conectar_a_la_base` y sin el keyword: ni
el código ni la prueba lo veían, porque los dos dependían de CÓMO se
escribe la llamada. Es la misma trampa que ya cerró Max/Natalia con la
puerta a Telegram, y la pregunta que la cierra es la misma: no "¿cómo
detecto todas las formas de escribir la llamada?", sino "¿se puede hacer
que el caso no exista?".

LA RESPUESTA, medida contra el psycopg instalado (versión 3.x, paquete
`psycopg`) — Y CORREGIDA el 22-sep-2026, tercera vuelta: un testigo midió
la frase de acá abajo y encontró que decía algo que NO es cierto.

  `psycopg.connect` y `psycopg.Connection.connect` NO son el mismo objeto:
  `Connection.connect` es un `classmethod`, y CADA VEZ que se lo lee por
  atributo (`psycopg.connect`, o `Connection.connect` de nuevo) Python crea
  un objeto "bound method" NUEVO. Medido:

      psycopg.connect is psycopg.Connection.connect        # False
      a = psycopg.Connection.connect
      b = psycopg.Connection.connect
      a is b                                                # False

  Lo que SÍ es el mismo objeto, en cualquier lectura, es lo de ADENTRO: la
  función plana (`__func__`) y el diccionario de sus valores por omisión
  (`__kwdefaults__`), que Python guarda una única vez por función y no por
  cada bound method que se cree para envolverla. Medido:

      psycopg.connect.__func__ is psycopg.Connection.connect.__func__       # True
      psycopg.connect.__kwdefaults__ is psycopg.Connection.connect.__kwdefaults__  # True
      a.__kwdefaults__ is b.__kwdefaults__                                  # True

  (Python reenvía `__kwdefaults__` del bound method a `__func__` cuando se
  lo pide por atributo — por eso alcanza con escribir `clase.connect.
  __kwdefaults__[...]`, sin pasar por `.__func__` a mano.)

  Un alias —`from psycopg import connect as lo_que_sea`— tampoco copia
  nada de esto: guarda una REFERENCIA a un bound method (o, si se hace
  `x = Connection.connect`, un bound method fresco, pero que envuelve el
  MISMO `__func__`). Y Python resuelve los valores por omisión de un
  argumento solo-por-nombre CADA VEZ QUE SE LLAMA, leyendo
  `__func__.__kwdefaults__` en ese momento — no cuando se creó el bound
  method ni cuando se importó el alias. Por eso cambiar
  `__kwdefaults__["prepare_threshold"]` UNA vez, acá, cambia lo que ve
  cualquier nombre que en algún momento llame a `connect` —se haya
  capturado antes o después de este cambio, y aunque el nombre no exista
  todavía—: todos terminan resolviendo el mismo `__func__`, y ahí es donde
  vive el valor. No hay lista de alias que perseguir porque no hay alias
  que mirar: hay UNA función por debajo, y se le cambia el default.

  Se repite para `psycopg.AsyncConnection.connect` (la que usa el pool de
  `db/db.py`, vía `AsyncConnectionPool(..., connection_class=AsyncConnection)`
  por omisión): son dos funciones DISTINTAS —sync y async— y las dos
  tienen su propio `prepare_threshold=5` por separado, medido.

  Con esto, `db/db.py` ya NO necesita pasarle `kwargs={"prepare_threshold":
  None}` al pool: el pool llama `connection_class.connect(conninfo,
  **kwargs)` (`psycopg_pool/pool_async.py:650`), y si `kwargs` no trae la
  clave, `connect()` usa SU default — que ya es `None` — sin que nadie se
  lo tenga que decir en el sitio de la llamada.

QUÉ SE DESCARTÓ Y POR QUÉ. `psycopg_pool.AsyncConnectionPool` tiene un
`configure=` documentado —un callback que corre sobre cada conexión nueva
del pool— que también serviría, y sin tocar nada privado. Se descartó como
la solución ÚNICA porque solo cubre al POOL: `db/backup.py` y los dos
guiones de `tools/` abren su conexión con `psycopg.connect(...)` a pelo,
sin pool, así que un `configure=` no los alcanza. Habría hecho falta DOS
mecanismos —uno para el pool, otro para las conexiones sueltas—, que es
exactamente la clase de "un sitio por cada forma" que este arreglo vino a
evitar. `__kwdefaults__` cubre las dos formas con el mismo cambio, porque
las dos formas terminan llamando a la MISMA función `connect`.

EL RIESGO, dicho porque el encargo lo pide: `__kwdefaults__` es un detalle
de CÓMO CPython guarda los valores por omisión de los argumentos
solo-por-nombre — no es una API pública ni documentada de psycopg. Si una
versión futura de la librería deja de declarar `prepare_threshold` como
argumento con nombre de `connect` (lo envuelve en un decorador, lo saca de
la firma, cambia `connect` por algo que no sea una función Python plana),
este cambio deja de tener efecto — y lo hace EN SILENCIO, porque asignar a
una clave que ya no importa no lanza ningún error. Por ESO `aplicar()` se
relee a sí misma antes de terminar y revienta con un mensaje claro si el
valor no quedó en `None`: mejor una falla ruidosa al arrancar el proceso
que un plan preparado que nadie esperaba, descubierto el día de una
migración. Y por eso también existe `tests/test_sin_preparadas.py`,
corriendo el hecho contra la función viva — no leyendo el texto de este
archivo ni el de quien lo llama.
"""
from __future__ import annotations

import inspect

import psycopg

_YA_APLICADO = False


def _clases_de_conexion(modulo) -> list:
    """`Connection` y `AsyncConnection` del módulo dado, si son de verdad
    clases con un `.connect` cuyo default se pueda tocar — o lista vacía
    si `modulo` es un psycopg de mentira (la mayoría de las suites de este
    repo lo reemplazan por un `types.ModuleType` vacío para correr sin
    Postgres; ver la cabecera de cualquier `tests/test_*.py`). Misma
    tolerancia que ya usa `db/db.py` para `_ERRORES_DE_FILA`: `getattr`
    con default, nunca un acceso directo que reviente al importar bajo un
    doble.
    """
    return [
        c for c in (getattr(modulo, "Connection", None),
                    getattr(modulo, "AsyncConnection", None))
        if inspect.isclass(c)
        and hasattr(getattr(c, "connect", None), "__kwdefaults__")
        and "prepare_threshold" in c.connect.__kwdefaults__
    ]


def aplicar(modulo=None) -> None:
    """Deja `prepare_threshold=None` como valor por omisión de CUALQUIER
    conexión que este proceso abra de acá en adelante, la escriba quien la
    escriba y la llame como la llame.

    `modulo` es el `psycopg` a parchear — por omisión, el que este archivo
    importó arriba. Existe como parámetro (y no como el nombre de módulo
    fijo) para que una prueba pueda pasarle el psycopg REAL cuando el de
    arriba resultó ser un doble por el orden en que corrió la suite — ver
    `tests/test_sin_preparadas.py::_modulo_de_verdad`.

    Idempotente: la segunda llamada (con el mismo psycopg ya parchado) no
    hace nada. Sobre un psycopg de mentira, se sale callada — no hay nada
    que apagar y no es un error que no lo haya.
    """
    global _YA_APLICADO
    mod = modulo if modulo is not None else psycopg
    clases = _clases_de_conexion(mod)
    if not clases:
        return
    for clase in clases:
        clase.connect.__kwdefaults__["prepare_threshold"] = None
        if clase.connect.__kwdefaults__.get("prepare_threshold") is not None:
            raise RuntimeError(
                f"No pude apagar prepare_threshold en {clase.__name__}.connect: "
                "el paquete psycopg instalado cambió de forma respecto a lo "
                "que este archivo asume. Revisar db/sin_preparadas.py contra "
                "la versión nueva de psycopg antes de seguir.")
    _YA_APLICADO = True
