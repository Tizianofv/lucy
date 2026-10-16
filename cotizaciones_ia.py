"""La IA que ayuda a escribir los TEXTOS de una cotización (pieza 1 del diseño «IA para los textos de las
cotizaciones», 16-oct-2026).

Dos servicios, los dos devuelven texto y NO escriben nada (ni base, ni Drive, ni Telegram):

* `mejorar_redaccion(renglon, nombre_opcion, ejemplos)` → el detalle de un renglón bien escrito (`str`).
* `armar_borrador(pedido, ejemplos)` → opciones con renglones `{"descripcion", "detalle"}`. SIN precio: el
  precio lo pone la persona en su casilla.

`_pedir` es la ÚNICA función de este módulo que llama al modelo (el censo `LLAMADAS_AL_MODELO_SIN_JUNTA` de
`tests/test_responsable.py` la declara). Usa el cliente y el modelo de siempre (`cerebro.deepseek`) pero
con su propio tope de tiempo y SIN reintentos: el cliente de siempre reintenta 2 veces y la persona
esperaría hasta el triple.

Cómo se evita que la IA ponga precios, en el orden de la regla 19 de la sala:
1. nunca ve un precio: los ejemplos copian campo por campo, por nombre, `nombre`, `descripcion` y `detalle`
   (`ejemplos_del_tono`), y lo que la persona escribió de un renglón se lee igual (`mejorar_redaccion`);
2. el mensaje de sistema le dice qué SÍ hacer (`_REGLAS`);
3. su respuesta no tiene casilla de precio: el código lee solo `nombre`, `descripcion` y `detalle`;
4. al final, una guarda que contrasta un VALOR contra su FUENTE (`cifras_que_no_estaban`): toda cifra con
   dígitos de la propuesta tiene que estar entre las cifras de lo que la persona escribió en esa llamada.
   FRONTERA: no ve cifras escritas con letras («cincuenta mil pesos», «dos horas»); lo cubren los pasos 1
   a 3. No juzga cómo está escrita la frase.

Todo texto de la propuesta pasa por `db._texto_de_cotizacion` con el tope de su campo (la misma puerta de
cuando se guarda), así que lo que la persona ve es lo que se podrá guardar y salir en el PDF.

El registro (`lucy.cotizaciones_ia`) anota el botón, los tokens y los segundos; NUNCA el pedido ni la
propuesta ni el texto del servicio. Los topes de tiempo, el de largo del pedido, el número de ejemplos y el
tope de los ejemplos son de diseño: no salen de medir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import db.db as db
from cerebro.deepseek import MODELO, cliente

log = logging.getLogger("lucy.cotizaciones_ia")

# De diseño, no medidos (diseño §3.6, §3.2, §3.3).
TOPE_REDACCION_S = 30.0
TOPE_BORRADOR_S = 60.0
LARGO_DEL_PEDIDO = 2000
EJEMPLOS_RECIENTES = 5
TOPE_DE_LOS_EJEMPLOS = 6000   # caracteres de texto entre todos los ejemplos

# Las frases que ve la persona. Nunca el texto del servicio.
CAUSAS = {
    "no_contesto": "La IA no contestó. Lo que escribiste sigue igual.",
    "no_sirve": "La IA contestó algo que no se puede usar. Vuelve a intentarlo.",
    "cifra": "La IA escribió una cifra que tú no diste ({cifra}). No te la enseño: vuelve a intentarlo o escríbelo tú.",
    "detalle_vacio": "Escribe algo en el detalle primero.",
    "pedido_vacio": "Escribe lo que pidió el cliente primero.",
    "pedido_largo": f"Lo que pidió el cliente pasa de {LARGO_DEL_PEDIDO} caracteres. Acórtalo.",
}


class IANoContesta(Exception):
    """La IA no contestó (tiempo, red, error del servicio, falta la llave). El texto de la excepción es
    siempre la frase fija."""

    def __init__(self):
        super().__init__(CAUSAS["no_contesto"])


class IAPropuestaNoSirve(Exception):
    """La propuesta (o lo que se pidió) no se puede usar. `causa` es una clave de `CAUSAS`; `cifras` son las
    cifras que la persona no dio, cuando la causa es `cifra`."""

    def __init__(self, causa: str, cifras: list[str] | None = None):
        if causa not in CAUSAS:
            raise ValueError(f"causa desconocida: {causa!r}")
        self.causa = causa
        self.cifras = list(cifras or [])
        super().__init__(CAUSAS[causa].format(cifra=", ".join(self.cifras)))


# ── Los ejemplos del tono ───────────────────────────────────────────────────────────────────────────────

# Las tres opciones de la cotización de muestra que Tiziano aprobó, copiadas textuales por la sala de
# `disenos/lucy-cotizaciones/maqueta-aprobada.html:1795-1820`, SIN los totales: la IA nunca ve un precio.
_DESCRIPCION_DE_LA_MAQUETA = "Grabación de bajo, bongó, conga, güira, guitarra y requinto"
EJEMPLOS_DE_LA_MAQUETA = [
    {"nombre": "Músicos formados en la práctica", "renglones": [{
        "descripcion": _DESCRIPCION_DE_LA_MAQUETA,
        "detalle": "Músicos formados en la práctica, con experiencia tocando el género en vivo y en la calle: "
                   "conocen el estilo desde la tarima y el trabajo diario. Incluye horas de estudio para "
                   "grabación, mezcla y máster. Canción terminada, lista para publicar en plataformas "
                   "digitales."}]},
    {"nombre": "Músicos graduados de conservatorio", "renglones": [{
        "descripcion": _DESCRIPCION_DE_LA_MAQUETA,
        "detalle": "Músicos graduados de conservatorio, con formación académica en su instrumento, dominio "
                   "de lectura musical y teoría. Incluye horas de estudio para grabación, mezcla y máster. "
                   "Canción terminada, lista para publicar."}]},
    {"nombre": "Músicos graduados en el extranjero", "renglones": [{
        "descripcion": _DESCRIPCION_DE_LA_MAQUETA,
        "detalle": "Músicos graduados en el extranjero, con formación académica internacional. Incluye horas "
                   "de estudio para grabación, mezcla y máster. Canción terminada, lista para publicar."}]},
]


def _texto(v) -> str:
    return v if isinstance(v, str) else ""


def _opcion_de_ejemplo(op) -> dict | None:
    """Una opción de una cotización → solo `nombre`, y de cada renglón solo `descripcion` y `detalle`,
    copiados POR NOMBRE. Un campo que no está en esta lista no viaja, aunque mañana se agregue al contenido."""
    if not isinstance(op, dict):
        return None
    renglones = [{"descripcion": _texto(r.get("descripcion")), "detalle": _texto(r.get("detalle"))}
                 for r in op.get("renglones") or [] if isinstance(r, dict)]
    return {"nombre": _texto(op.get("nombre")), "renglones": renglones}


def _tamano_de_ejemplo(op: dict) -> int:
    return len(op["nombre"]) + sum(len(r["descripcion"]) + len(r["detalle"]) for r in op["renglones"])


def ejemplos_del_tono(cotizaciones: dict | None) -> list:
    """Los ejemplos que ve la IA: SIEMPRE los de la maqueta, y además los textos de las `EJEMPLOS_RECIENTES`
    cotizaciones enviadas más recientes (por `enviada_en`) de `cotizaciones` (la forma de
    `db.cotizaciones_de_proyectos()`: `{proyecto_id: [cotización, ...]}`, o `None`). Entre todos, a lo sumo
    `TOPE_DE_LOS_EJEMPLOS` caracteres; lo que no cabe se queda fuera, empezando por las más viejas. Pura."""
    ejemplos = [dict(o) for o in EJEMPLOS_DE_LA_MAQUETA]
    gastado = sum(_tamano_de_ejemplo(o) for o in ejemplos)
    enviadas = []
    for lista in (cotizaciones or {}).values():
        for c in lista or []:
            if isinstance(c, dict) and c.get("estado") == "enviada" and c.get("enviada_en") is not None:
                enviadas.append(c)
    enviadas.sort(key=lambda c: c["enviada_en"], reverse=True)
    for c in enviadas[:EJEMPLOS_RECIENTES]:
        contenido = c.get("contenido")
        ops = [o for o in (_opcion_de_ejemplo(x) for x in (contenido or {}).get("opciones") or []) if o]
        costo = sum(_tamano_de_ejemplo(o) for o in ops)
        if not ops or gastado + costo > TOPE_DE_LOS_EJEMPLOS:
            break
        ejemplos.extend(ops)
        gastado += costo
    return ejemplos


# ── La guarda de cifras ─────────────────────────────────────────────────────────────────────────────────

_RE_CIFRA = re.compile(r"[0-9]+(?:[.,][0-9]+)*", re.ASCII)
_RE_MILES = re.compile(r"[0-9]{1,3}(?:[.,][0-9]{3})+")
_RE_MILES_Y_DECIMALES = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+)\.([0-9]+)")


def _forma_de_la_cifra(c: str) -> str:
    """«45,000» y «45.000» → «45000»; «62,500.50» → «62500.50»; «2,5» → «2.5». Las dos puntas de la
    comparación pasan por aquí."""
    if _RE_MILES.fullmatch(c):
        return re.sub(r"[.,]", "", c)
    m = _RE_MILES_Y_DECIMALES.fullmatch(c)
    if m:
        return m.group(1).replace(",", "") + "." + m.group(2)
    return c.replace(",", ".")


def _cifras_de(textos: list[str]) -> list[str]:
    return [_forma_de_la_cifra(m) for t in textos for m in _RE_CIFRA.findall(t)]


def cifras_que_no_estaban(propuesta: list[str], fuente: list[str]) -> list[str]:
    """Las cifras (con dígitos, ya sin separadores de miles) de `propuesta` que NO están entre las de
    `fuente`, sin repetir y en el orden en que salen. Mira valores, no cómo está escrita la frase. No ve
    cifras escritas con letras."""
    dadas = set(_cifras_de(fuente))
    faltan: list[str] = []
    for c in _cifras_de(propuesta):
        if c not in dadas and c not in faltan:
            faltan.append(c)
    return faltan


# ── La única llamada al modelo ──────────────────────────────────────────────────────────────────────────

async def _pedir(mensajes: list[dict], tope_s: float, boton: str) -> str:
    """LA ÚNICA función de este módulo que llama al modelo. Una petición, sin reintentos, con su tope
    (el del cliente HTTP y uno total por encima). Devuelve el `content` o lanza `IANoContesta`. Anota en el
    registro el botón, los tokens y los segundos; nunca el texto."""
    t0 = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            cliente.with_options(timeout=tope_s, max_retries=0).chat.completions.create(
                model=MODELO, messages=mensajes, response_format={"type": "json_object"}, temperature=0.3),
            timeout=tope_s)
        contenido = resp.choices[0].message.content
        uso = resp.usage
        log.info("ia de cotizaciones: boton=%s prompt_tokens=%s completion_tokens=%s segundos=%.2f",
                 boton, uso.prompt_tokens if uso else None, uso.completion_tokens if uso else None,
                 time.monotonic() - t0)
    except Exception as e:       # tiempo, red, error del servicio, forma inesperada: todo es «no contestó»
        log.warning("ia de cotizaciones: boton=%s fallo=%s segundos=%.2f",
                    boton, type(e).__name__, time.monotonic() - t0)
        raise IANoContesta() from None
    if not isinstance(contenido, str) or not contenido.strip():
        raise IAPropuestaNoSirve("no_sirve")
    return contenido


def _json_de(contenido: str) -> dict:
    try:
        datos = json.loads(contenido)
    except ValueError:
        raise IAPropuestaNoSirve("no_sirve") from None
    if not isinstance(datos, dict):
        raise IAPropuestaNoSirve("no_sirve")
    return datos


def _por_la_puerta(crudo, campo: str, largo: int, *, minimo: int = 1, varias_lineas: bool = False) -> str:
    """Un texto de la propuesta por la puerta de siempre; si no vale, la propuesta entera se descarta."""
    try:
        return db._texto_de_cotizacion(crudo, campo, largo, minimo=minimo, varias_lineas=varias_lineas)
    except db.CotizacionNoVale:
        raise IAPropuestaNoSirve("no_sirve") from None


def _revisar_cifras(propuesta: list[str], fuente: list[str]) -> None:
    faltan = cifras_que_no_estaban(propuesta, fuente)
    if faltan:
        raise IAPropuestaNoSirve("cifra", faltan)


# ── Lo que lee la IA ────────────────────────────────────────────────────────────────────────────────────

_REGLAS = """Reglas:
- Escribe solo lo que se pide: nombre de cada opción, descripción y detalle. El precio lo pone la persona en su casilla: no escribas montos ni porcentajes; describe lo que incluye.
- Si hace falta una cantidad (horas, canciones, músicos) y la persona no la dio, escribe sin cantidad, por ejemplo «horas de estudio para grabación». Si la persona dio una cantidad, úsala tal cual.
- No escribas fechas, años ni números de opción.
- Escribe en español, con el tono de los ejemplos: claro, directo y sin adornos."""


def _sistema(tarea: str, forma: str, ejemplos: list) -> str:
    return (
        "Eres quien redacta las cotizaciones de Caribbean Dream Studios, un estudio de grabación en Santo "
        "Domingo.\n\n"
        "Estos son textos de cotizaciones del estudio. Sirven solo para que copies el tono; no los repitas:\n"
        + json.dumps(ejemplos, ensure_ascii=False) + "\n\n"
        + tarea + "\n\n" + _REGLAS + "\n\n"
        "Contesta SOLO con un JSON de esta forma: " + forma)


async def mejorar_redaccion(renglon: dict, nombre_opcion: str, ejemplos: list) -> str:
    """El detalle del renglón, bien escrito. De `renglon` se leen solo `descripcion` y `detalle` (el precio
    no viaja ni se lee). Lanza `IANoContesta` o `IAPropuestaNoSirve`."""
    descripcion = _texto(renglon.get("descripcion")).strip()
    detalle = _texto(renglon.get("detalle")).strip()
    opcion = _texto(nombre_opcion).strip()
    if not detalle:
        raise IAPropuestaNoSirve("detalle_vacio")
    mensajes = [
        {"role": "system", "content": _sistema(
            "Te paso el detalle de un renglón de una cotización, escrito rápido. Reescríbelo bien, con lo "
            "mismo que dice y sin agregar servicios que la persona no nombró.",
            '{"detalle": "..."}', ejemplos)},
        {"role": "user", "content": json.dumps(
            {"opcion": opcion, "descripcion": descripcion, "detalle": detalle}, ensure_ascii=False)},
    ]
    datos = _json_de(await _pedir(mensajes, TOPE_REDACCION_S, "mejorar_redaccion"))
    texto = _por_la_puerta(datos.get("detalle"), "detalle", db.LARGO_DETALLE_DE_RENGLON, varias_lineas=True)
    _revisar_cifras([texto], [opcion, descripcion, detalle])
    return texto


async def armar_borrador(pedido: str, ejemplos: list) -> list[dict]:
    """Opciones con renglones `{"descripcion", "detalle"}` a partir de lo que pidió el cliente. SIN `total`:
    solo se leen `nombre`, `descripcion` y `detalle` de lo que contesta el modelo. Lanza `IANoContesta` o
    `IAPropuestaNoSirve`."""
    pedido = _texto(pedido).strip()
    if not pedido:
        raise IAPropuestaNoSirve("pedido_vacio")
    if len(pedido) > LARGO_DEL_PEDIDO:
        raise IAPropuestaNoSirve("pedido_largo")
    mensajes = [
        {"role": "system", "content": _sistema(
            "Te paso lo que pidió un cliente. Arma las opciones de la cotización: las opciones son "
            f"alternativas (el cliente escoge una). Hasta {db.MAX_OPCIONES_DE_COTIZACION} opciones y hasta "
            f"{db.MAX_RENGLONES_DE_OPCION} renglones por opción; el nombre de la opción no pasa de "
            f"{db.LARGO_NOMBRE_DE_OPCION} caracteres, la descripción de {db.LARGO_DESCRIPCION_DE_RENGLON} y "
            f"el detalle de {db.LARGO_DETALLE_DE_RENGLON}.",
            '{"opciones": [{"nombre": "...", "renglones": [{"descripcion": "...", "detalle": "..."}]}]}',
            ejemplos)},
        {"role": "user", "content": pedido},
    ]
    datos = _json_de(await _pedir(mensajes, TOPE_BORRADOR_S, "armar_borrador"))
    crudas = datos.get("opciones")
    if (not isinstance(crudas, list) or not 1 <= len(crudas) <= db.MAX_OPCIONES_DE_COTIZACION):
        raise IAPropuestaNoSirve("no_sirve")
    opciones = []
    for op in crudas:
        renglones_crudos = op.get("renglones") if isinstance(op, dict) else None
        if (not isinstance(renglones_crudos, list)
                or not 1 <= len(renglones_crudos) <= db.MAX_RENGLONES_DE_OPCION):
            raise IAPropuestaNoSirve("no_sirve")
        renglones = []
        for r in renglones_crudos:
            if not isinstance(r, dict):
                raise IAPropuestaNoSirve("no_sirve")
            renglones.append({
                "descripcion": _por_la_puerta(r.get("descripcion"), "descripcion",
                                              db.LARGO_DESCRIPCION_DE_RENGLON),
                "detalle": _por_la_puerta(r.get("detalle"), "detalle", db.LARGO_DETALLE_DE_RENGLON,
                                          minimo=0, varias_lineas=True)})
        opciones.append({"nombre": _por_la_puerta(op.get("nombre"), "nombre", db.LARGO_NOMBRE_DE_OPCION),
                         "renglones": renglones})
    textos = [o["nombre"] for o in opciones]
    textos += [t for o in opciones for r in o["renglones"] for t in (r["descripcion"], r["detalle"])]
    _revisar_cifras(textos, [pedido])
    return opciones
