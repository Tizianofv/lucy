"""Las personas del Noco de CDS, LEÍDAS desde Lucy (Lucy 1.0, E3).

QUÉ ES ESTO, Y QUÉ NO. El CRM de CDS vive en un NocoDB que no es de Lucy: ahí
están los clientes de Natalia y de la App de registro. Este módulo es la única
parte de Lucy que lo mira, y lo mira para leer: `buscar_personas` para el
buscador del panel, `persona` para volver a pedir la ficha por su Id antes de
guardarla (`db.poner_cliente`, E2: el nombre que se guarda es el que Noco
devuelve, nunca el que mandó el navegador) y `contacto` para enseñar el teléfono
y el correo del cliente de un proyecto.

LA GARANTÍA QUE SOSTIENE TODO LO DEMÁS, y por la que este archivo está escrito
así: **Lucy no escribe en Noco, punto.** El token de Railway
(`NOCODB_TOKEN_LUCY`) es de la cuenta normal de Tiziano —decisión suya del
1-oct-2026, y por eso puede escribir—, así que la garantía NO la da el token: la
da el código, y por eso:

  · `_get` es la única función de este módulo que llama a la red, y solo sabe
    hacer GET;
  · `_get` es además la que decide qué columnas salen (`fields`): quien llama
    elige entre dos juegos fijos de este archivo, nunca escribe una columna, y
    `bsuid` no está en ninguno;
  · **fuera de este archivo nadie obtiene la URL ni el token por las formas
    que la prueba ve** (`os` y sus alias, `environ`/`getenv` con clave constante,
    imports del lector). Ningún
    `.py` del repositorio que no sea de `tests/` importa algo privado de acá
    (`_configurado`, `_get`…), ni usa el módulo de otra forma que
    `noco_lectura.<función pública>`, ni nombra una variable `NOCODB_*`, ni lee
    el entorno entero. Sin la URL y el token no hay petición a Noco que escribir,
    con la biblioteca que sea;
  · **dentro de este archivo, lista blanca:** solo se importan `__future__`,
    `logging`, `os` y `httpx`; solo se llama a lo declarado en
    `tests/test_noco_lectura.py` (`_LLAMADAS_INOCUAS`), a las funciones propias y
    a las dos de la red, `httpx.AsyncClient` y `cliente.get`, que solo pueden
    estar en `_get`. `subprocess`, `socket`, `os.system`, `httpx.request` o
    `cliente.post` no están en la lista y por eso rompen la prueba, escritos
    como se escriban.

Todo eso lo exige `tests/test_noco_lectura.py` recorriendo el repositorio
entero (la lista de archivos sale del disco) y dándole a la guarda entradas
inventadas, no solo lo que hay hoy.

LÍMITE QUE NINGÚN CÓDIGO ARREGLA: el token del proceso puede escribir. Cualquier
código del mismo proceso que lea el entorno de formas que la guarda no ve podría
usarlo. La protección completa es un token de solo lectura en NocoDB.

LO QUE ESA GARANTÍA NO VE, dicho para que no se dé por cubierto: las formas
rebuscadas (`persona.__globals__`, `leer = os.getenv; leer(clave)`, `db/backup.py`
cambiando `pg_dump` por `curl`, `subprocess` con `env`); un `.py` de
`tests/`; lo que no es `.py` (scripts de shell, `.yml`, SQL); un nombre `NOCODB_*`
armado de una forma que no se puede plegar a un texto sin correr el código
(`"".join([...])`, `"%s" % x`); una URL de Noco o un token escritos a mano
(prohibido por ser público el repositorio, pero esto no lo ve); y el entorno
entero pasado a un hijo, que `db/backup.py` hace una vez y está declarado en la
prueba. La otra mitad es de comportamiento: todo pedido que sale de este módulo
se mira con un cliente HTTP de verdad y un transporte de mentira, y tiene que ser
un GET.

SI NOCO NO CONTESTA, SE DICE. `NocoNoContesta` sube hasta quien llamó —la ruta
del panel la traduce a un 503 con su motivo— y NUNCA se devuelve una lista
inventada ni un «no hay nadie» que mienta: un buscador vacío por un error de red
le haría creer a quien busca que esa persona no está en el CRM.

EL REPOSITORIO ES PÚBLICO: acá no se escribe la URL del CRM ni el id de la tabla
de personas ni el token. Los tres viven en variables de entorno del servicio
`lucy` de Railway y se leen en CADA LLAMADA, no al importar: así el panel
arranca aunque falten (lo que falla, con su motivo, es la búsqueda), y una
prueba puede ponerlas y quitarlas sin volver a cargar el módulo.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("lucy.noco")

# Cuánto se espera a Noco. Es una persona mirando un buscador: ocho segundos ya
# es una pantalla que parece colgada, y menos cortaría a Noco en un momento
# lento. Es un número elegido, no medido — que quede dicho.
TIEMPO_LIMITE = 8.0

# Hasta cuántas personas devuelve el buscador (§4 del diseño). Es también el
# `limit` que se le pide a Noco: no se traen 733 fichas para enseñar 20.
TOPE_BUSQUEDA = 20

# Las columnas de `buscar_personas` y `persona`. Salen `{id, nombre}`, pero para
# armar el nombre de una ficha sin `nombre` hacen falta `nombre_whatsapp` y el
# teléfono (de este solo se usan los últimos 4 dígitos, y no sale de `_ficha`).
COLUMNAS = "Id,nombre,nombre_whatsapp,telefono"

# Mínimo de dígitos para que el texto del buscador se busque también como teléfono
# (el mismo mínimo que la App de registro).
MIN_DIGITOS_TELEFONO = 4

# Las columnas de `contacto`: teléfono y correo principales y la marca de ficha unida a otra.
COLUMNAS_CONTACTO = "Id,telefono,email,unida_a"

# Cuántas fichas unidas se siguen desde la del cliente antes de darse por vencido.
TOPE_SALTOS = 10

# Las tres variables del servicio `lucy`. Se leen por su nombre desde acá y
# desde ningún otro archivo del repositorio (lo comprueba la prueba del censo).
# OJO con el nombre del token: es `NOCODB_TOKEN_LUCY`, NO `NOCODB_TOKEN_LECTURA`
# (que es lo que decía el diseño, escrito antes de que Tiziano decidiera usar su
# cuenta normal). Cambiarlo por el del diseño deja a Lucy sin token: cada
# búsqueda diría «falta configurar», y la prueba del censo lo pone rojo.
_VARIABLES = ("NOCODB_BASE", "NOCODB_T_PERSONAS", "NOCODB_TOKEN_LUCY")

# Lo que se le quita al texto del buscador antes de armar el `where` (§4). Son
# los caracteres con los que se podría cerrar el patrón y agregar una condición
# propia: `(nombre,like,%lo que sea%)`. Sin esto, escribir «a),(Id,gt,0» en el
# buscador sería escribir la consulta.
_QUITAR = ",()~"


class NocoNoContesta(RuntimeError):
    """Noco no contestó, contestó algo que no se entiende, o falta configurarlo.

    Es una excepción propia y no un `None` a propósito: quien llama tiene que
    poder distinguir «no hay nadie con ese nombre» de «no pude preguntar», y
    tratarlos distinto. Devolver `[]` en los dos casos convierte una caída de
    Noco en un «esa persona no está en el CRM», que es una mentira útil para
    nadie.
    """


def _configurado() -> tuple[str, str, str]:
    """(base, tabla, token) de Noco, leídos del entorno AHORA.

    Si falta cualquiera de los tres, se dice cuál falta y no se llama a nadie.
    El mensaje nombra la VARIABLE, nunca su valor: esto termina en una pantalla
    y en un registro, y el token no se imprime —ni entero ni cortado— en ningún
    camino de este módulo.
    """
    base = os.environ.get("NOCODB_BASE", "").strip().rstrip("/")
    tabla = os.environ.get("NOCODB_T_PERSONAS", "").strip()
    token = os.environ.get("NOCODB_TOKEN_LUCY", "").strip()
    faltan = [n for n, v in zip(_VARIABLES, (base, tabla, token)) if not v]
    if faltan:
        raise NocoNoContesta(
            "Noco no está configurado en este servicio: falta "
            + ", ".join(faltan))
    return base, tabla, token


def _limpio(texto) -> str:
    """El texto del buscador, sin los caracteres con los que se armaría una
    condición propia (§4): `,` `(` `)` `~`.

    Se cambian por un espacio y no se borran pegados: «Pérez,Juan» tiene que
    seguir siendo dos palabras y no «PérezJuan». Después se colapsan los
    espacios. Lo que no sea texto (un `None`, un número, una lista que llegó por
    un parámetro raro de la URL) se trata como vacío: no hay nada que buscar, y
    quien llama lo distingue con la lista vacía.
    """
    if not isinstance(texto, str):
        return ""
    for molde in _QUITAR:
        texto = texto.replace(molde, " ")
    return " ".join(texto.split())


def _digitos(texto) -> str:
    """Solo los dígitos 0-9 de un texto (quita `+`, espacios, guiones). Sin suponer
    prefijos de país: lo que no es dígito no cuenta y nada se agrega."""
    if not isinstance(texto, str):
        return ""
    return "".join(c for c in texto if c in "0123456789")


def _nombre_para_mostrar(fila: dict) -> str | None:
    """El nombre con el que se muestra (y se guarda) una ficha: `nombre`; si no
    tiene, `nombre_whatsapp`; si tampoco, «Sin nombre · <últimos 4 dígitos del
    teléfono>». `None` si no tiene ninguno de los tres: no hay nada que mostrar."""
    for clave in ("nombre", "nombre_whatsapp"):
        valor = fila.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    digitos = _digitos(fila.get("telefono"))
    if digitos:
        return f"Sin nombre · {digitos[-4:]}"
    return None


def _ficha(fila) -> dict | None:
    """Una fila de Noco como `{id, nombre}`, o `None` si no sirve.

    DOS TRABAJOS, y el segundo importa tanto como el primero:

    1. Traducir el `Id`/`nombre` de Noco a lo que el resto de Lucy espera
       (`{id, nombre}`, §4 del diseño).
    2. **No dejar pasar nada más.** El diccionario que sale de acá tiene dos
       claves: aunque Noco devolviera la fila entera
       —teléfono, correo, `bsuid`— por no respetar el `fields`, o porque mañana
       alguien cambie el `fields`, lo que sale de este módulo sigue siendo un
       nombre. Es la última puerta, y por eso se arma acá y no con un `dict(fila)`.

    El `nombre` es el de mostrar (`_nombre_para_mostrar`): una ficha sin `nombre`
    sale con `nombre_whatsapp` o «Sin nombre · <4 dígitos>». Una fila sin `Id`
    entero, o sin nombre, nombre_whatsapp ni teléfono con algo dentro, no se
    inventa ni se completa: se descarta (y se cuenta en el registro, sin el nombre: los
    nombres de las personas no van a un log).
    """
    if not isinstance(fila, dict):
        return None
    ident = fila.get("Id")
    if isinstance(ident, bool) or not isinstance(ident, int):
        return None
    nombre = _nombre_para_mostrar(fila)
    if nombre is None:
        return None
    return {"id": ident, "nombre": nombre}


async def _get(ruta: str, parametros: dict[str, str],
               con_contacto: bool = False) -> dict:
    """LA PUERTA DE SALIDA. Lo único de este repositorio que le habla a
    Noco, y solo sabe hacer GET.

    Qué decide acá y no en quien llama, a propósito:

      · **El método.** Se escribe `cliente.get(...)`, literal. No hay un
        parámetro «método» que alguien pueda llenar con «POST» desde arriba, ni
        un `request(...)` con el verbo en una variable.
      · **Las columnas.** `fields` lo pone esta función: `COLUMNAS`, o
        `COLUMNAS_CONTACTO` si quien llama lo pide con `con_contacto`. Si lo
        pusiera quien llama, agregar un `fields=` en un camino de arriba sería
        pedir otras columnas sin tocar este archivo.
      · **El token.** Va en la cabecera `xc-token` y en ningún otro sitio: ni en
        la URL, ni en un registro, ni en el mensaje de un error.

    Un fallo de red, un código de error, una respuesta que no es JSON o una que
    no tiene la forma que Noco tiene se levantan como `NocoNoContesta` con el
    motivo; una URL base que httpx rechaza sube con la excepción de httpx. La forma que se espera —`{"list": [...], "pageInfo": {...}}`— es la
    del API v2 medido por la sala el 1-oct-2026 (HTTP 200 y
    `pageInfo.totalRows` = 733 con `limit=1&fields=Id`), no una suposición de
    este archivo; el doble de la prueba dice de dónde sale y su frontera.
    """
    base, tabla, token = _configurado()
    url = f"{base}/api/v2/tables/{tabla}/{ruta}"
    pedido = {**parametros,
              "fields": COLUMNAS_CONTACTO if con_contacto else COLUMNAS}
    try:
        async with httpx.AsyncClient(timeout=TIEMPO_LIMITE) as cliente:
            respuesta = await cliente.get(
                url, params=pedido,
                headers={"xc-token": token, "Accept": "application/json"})
    except httpx.HTTPError as e:
        log.warning("Noco no contestó (%s)", type(e).__name__)
        raise NocoNoContesta("no pude hablar con Noco") from e
    if respuesta.status_code != 200:
        log.warning("Noco contestó %s", respuesta.status_code)
        raise NocoNoContesta(f"Noco contestó {respuesta.status_code}")
    try:
        datos = respuesta.json()
    except ValueError as e:
        log.warning("Noco contestó algo que no es JSON")
        raise NocoNoContesta("Noco contestó algo que no entiendo") from e
    if not isinstance(datos, dict) or not isinstance(datos.get("list"), list):
        log.warning("Noco contestó un JSON con otra forma")
        raise NocoNoContesta("Noco contestó algo que no entiendo")
    return datos


def _fichas(datos: dict) -> list[dict]:
    """Las filas que sirven, en orden, de una respuesta de Noco ya validada."""
    filas = [f for f in (_ficha(fila) for fila in datos["list"]) if f]
    if len(filas) != len(datos["list"]):
        log.warning("Noco devolvió %d fila(s) que no pude usar de %d",
                    len(datos["list"]) - len(filas), len(datos["list"]))
    return filas


async def buscar_personas(texto: str) -> list[dict]:
    """Hasta `TOPE_BUSQUEDA` personas cuyo nombre o `nombre_whatsapp` se parezca a
    `texto` o, si el texto no tiene letras y trae al menos `MIN_DIGITOS_TELEFONO`
    dígitos, cuyo teléfono los contenga (solo dígitos; sin suponer prefijos).

    Devuelve `[{"id": …, "nombre": …}]`. Es lo que llena el buscador del panel
    (no un desplegable: el Noco de CDS tiene 733 personas, medido por la sala el
    1-oct-2026).

    Un texto que no deja nada después de limpiarlo —vacío, o solo con los
    caracteres que se quitan— devuelve la lista vacía SIN preguntarle a Noco:
    no hay nada que buscar, y pedir «todos» para enseñar veinte nombres al azar
    no es una búsqueda.
    """
    limpio = _limpio(texto)
    if not limpio:
        return []
    donde = (f"(nombre,like,%{limpio}%)~or(nombre_whatsapp,like,%{limpio}%)")
    digitos = _digitos(limpio)
    if len(digitos) >= MIN_DIGITOS_TELEFONO and not any(c.isalpha() for c in limpio):
        donde += f"~or(telefono,like,%{digitos}%)"
    datos = await _get("records", {
        "where": donde,
        "limit": str(TOPE_BUSQUEDA),
    })
    return _fichas(datos)[:TOPE_BUSQUEDA]


async def persona(noco_id: int) -> dict | None:
    """La ficha de Noco con ese `Id`, como `{id, nombre}`, o `None` si no existe.

    Es la función que vuelve a preguntarle a Noco antes de guardar
    (`db.poner_cliente`, E2): el nombre que se guarda es el que Noco devuelve,
    nunca el que mandó el navegador. Si la ficha no existe, `None` — y quien
    llama rechaza.

    El `Id` tiene que ser un entero positivo de verdad. No es una formalidad: el
    `where` se arma con ese número, así que un texto libre acá sería el mismo
    agujero que el buscador cierra con `_limpio`. Se comprueba antes de tocar la
    red, y un `bool` no pasa aunque en Python sea un `int` (`True` es 1, y
    «la ficha 1» no es lo que nadie quiso decir).
    """
    if isinstance(noco_id, bool) or not isinstance(noco_id, int) or noco_id <= 0:
        raise ValueError("una ficha de Noco se pide por su Id: un entero positivo")
    datos = await _get("records", {
        "where": f"(Id,eq,{noco_id})",
        "limit": "1",
    })
    fichas = _fichas(datos)
    return fichas[0] if fichas else None


def _texto_o_none(valor) -> str | None:
    """El texto recortado de un valor de Noco, o `None` si no hay texto."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    return valor.strip()


async def contacto(noco_id: int) -> dict | None:
    """El teléfono y el correo de la ficha con ese `Id`, siguiendo las fichas unidas."""
    if isinstance(noco_id, bool) or not isinstance(noco_id, int) or noco_id <= 0:
        raise ValueError("una ficha de Noco se pide por su Id: un entero positivo")
    for _ in range(TOPE_SALTOS + 1):
        datos = await _get("records", {
            "where": f"(Id,eq,{noco_id})",
            "limit": "1",
        }, con_contacto=True)
        if not datos["list"] or not isinstance(datos["list"][0], dict):
            return None
        fila = datos["list"][0]
        unida = fila.get("unida_a")
        if isinstance(unida, bool) or not isinstance(unida, int) or unida <= 0:
            telefono = _texto_o_none(fila.get("telefono"))
            email = _texto_o_none(fila.get("email"))
            if telefono is None and email is None:
                return None
            return {"telefono": telefono, "email": email}
        noco_id = unida
    log.warning("Noco: la cadena de fichas unidas pasa del tope")
    raise NocoNoContesta("la cadena de fichas unidas es más larga que el tope")
