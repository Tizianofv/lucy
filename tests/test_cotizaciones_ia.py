"""Pieza 1 de «IA para los textos de las cotizaciones»: `cotizaciones_ia.py`.

El modelo se dobla EN LA RED, no en la función: un `AsyncOpenAI` de verdad (el de la biblioteca instalada,
con `max_retries=2` y `timeout=90` como el cliente de `cerebro/deepseek.py`) sobre `httpx.MockTransport`.
Así corre el camino de producción: `with_options`, `response_format`, la lectura de `content` y de `usage`.
La forma de la respuesta es la de `openai.types.chat.ChatCompletion` (la de `openai==2.46.0`, la del
`requirements.txt`); `test_la_respuesta_del_doble_tiene_la_forma_de_la_biblioteca` la valida contra ella.
Nada de esto llama al modelo real.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import datetime as dt
import json
import logging
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import cotizaciones_ia as ia  # noqa: E402  (después de las variables que `config.py` exige)
import db.db as db

RAIZ = Path(__file__).resolve().parent.parent

TOTAL = "73914.37"
NOMBRE_CLIENTE = "Zulmira Quintanilla"
TELEFONO = "829-555-7391"
TEXTO_DE_CONDICION = "CONDICION-UNICA-DEL-DEPOSITO"
# `main.py` configura el registro en INFO; a nivel DEBUG la biblioteca `openai` registra el cuerpo de cada
# petición, y eso no es de este módulo.
NIVEL_DE_PRODUCCION = logging.INFO
SECRETO_DEL_SERVICIO = "SECRETO-QUE-DICE-EL-SERVICIO-xyz"


_FAMILIAS = ("httpx", "openai")
_DE_VERDAD: dict = {}


def _de_la_familia(k: str) -> bool:
    return any(k == f or k.startswith(f + ".") for f in _FAMILIAS)


@contextlib.contextmanager
def _bibliotecas_de_verdad():
    """Otras pruebas (`test_al_fallar_no_va_a_code.py`, `test_reporte_una_vez_al_dia.py`, …) cambian
    `sys.modules["openai"]` y `sys.modules["httpx"]` por un doble cuando se importan, y en un mismo proceso
    de `-n 6` eso pasa entre que esta prueba se recoge y se corre. Durante cada prueba se usan `httpx` y
    `openai` DE VERDAD (los que trae el entorno) y al terminar `sys.modules` queda como estaba."""
    guardados = {k: v for k, v in sys.modules.items() if _de_la_familia(k)}
    estorba = any(not sys.modules[f].__dict__.get("__file__") for f in _FAMILIAS if f in sys.modules)
    if not estorba:
        yield
        return
    for k in guardados:
        del sys.modules[k]
    if not _DE_VERDAD:
        for f in _FAMILIAS:
            importlib.import_module(f)
        importlib.import_module("openai.types.chat")
        _DE_VERDAD.update({k: v for k, v in sys.modules.items() if _de_la_familia(k)})
    else:
        sys.modules.update(_DE_VERDAD)
    try:
        yield
    finally:
        for k in [k for k in sys.modules if _de_la_familia(k)]:
            del sys.modules[k]
        sys.modules.update(guardados)


@pytest.fixture(autouse=True)
def bibliotecas_de_verdad(monkeypatch):
    with _bibliotecas_de_verdad():
        monkeypatch.setitem(globals(), "httpx", importlib.import_module("httpx"))
        yield


httpx = None   # lo pone la fixture `bibliotecas_de_verdad` (ver arriba)


def _completion(content, usage=True):
    cuerpo = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "deepseek-v4-flash",
              "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": content}}]}
    if usage:
        cuerpo["usage"] = {"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801}
    return cuerpo


class Modelo:
    """El modelo, doblado en la red. `respuesta` es el `content` (str), un `dict`/`list` (se vuelve JSON), o
    una función `(request) -> httpx.Response | Exception | dict`."""

    def __init__(self, respuesta=None):
        self.respuesta = respuesta
        self.peticiones: list[httpx.Request] = []

    def cuerpos(self) -> list[dict]:
        return [json.loads(p.content) for p in self.peticiones]

    def texto_enviado(self) -> str:
        return "\n".join(p.content.decode() for p in self.peticiones)

    async def __call__(self, request: httpx.Request):
        self.peticiones.append(request)
        r = self.respuesta(request) if callable(self.respuesta) else self.respuesta
        if asyncio.iscoroutine(r):
            r = await r
        if isinstance(r, Exception):
            raise r
        if isinstance(r, httpx.Response):
            return r
        contenido = r if isinstance(r, str) or r is None else json.dumps(r)
        return httpx.Response(200, json=_completion(contenido))


@pytest.fixture
def modelo(monkeypatch):
    from openai import AsyncOpenAI
    m = Modelo()
    falso = AsyncOpenAI(api_key="llave-de-prueba", base_url="https://api.deepseek.com", timeout=90.0,
                        max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(m)))
    monkeypatch.setattr(ia, "cliente", falso)
    return m


def _caso(modelo, respuesta):
    modelo.respuesta = respuesta
    return modelo


PAL = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]


def _cotizacion(n, enviada_en, estado="enviada", **extra):
    c = {"id": n, "proyecto_id": 1, "estado": estado, "enviada_en": enviada_en, "codigo": f"COT-2026-{n:03d}",
         "para_nombre": NOMBRE_CLIENTE, "para_telefono": TELEFONO, "fecha": "2026-10-14",
         "contenido": {
             "opciones": [{"nombre": f"Opcion de prueba {PAL[n]}", "renglones": [
                 {"descripcion": f"Descripcion de prueba {PAL[n]}", "detalle": f"Detalle de prueba {PAL[n]}",
                  "total": TOTAL}]}],
             "condiciones": [{"clave": "deposito", "texto": TEXTO_DE_CONDICION, "marcada": True}]},
         "totales_por_opcion": [TOTAL]}
    c.update(extra)
    return c


def _dia(n):
    return dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=n)


COTIZACIONES = {1: [_cotizacion(i, _dia(i)) for i in range(1, 8)] + [_cotizacion(9, _dia(30), estado="borrador")]}

# ── 1. Los campos de precio y de cliente no llegan al modelo ────────────────────────────────────────────────

FORMAS_PROHIBIDAS = ["73914.37", "73,914.37", "73914", NOMBRE_CLIENTE, "Quintanilla", TELEFONO, "5557391",
                     TEXTO_DE_CONDICION, "COT-2026"]


@pytest.mark.parametrize("boton", ["redaccion", "borrador"])
def test_los_campos_de_precio_y_de_cliente_de_las_cotizaciones_viejas_no_llegan_al_modelo(modelo, boton):
    ejemplos = ia.ejemplos_del_tono(COTIZACIONES)
    if boton == "redaccion":
        _caso(modelo, {"detalle": "Texto nuevo"})
        asyncio.run(ia.mejorar_redaccion(
            {"descripcion": "Grabacion", "detalle": "bajo y guira", "total": TOTAL, "precio": "99887.11"},
            "Opcion A", ejemplos))
    else:
        _caso(modelo, {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": "C"}]}]})
        asyncio.run(ia.armar_borrador("quiere grabar un merengue", ejemplos))
    enviado = modelo.texto_enviado()
    assert f"Detalle de prueba {PAL[7]}" in enviado, "los ejemplos recientes no llegaron: la prueba no mide nada"
    for prohibida in FORMAS_PROHIBIDAS + ["99887.11", "99,887.11"]:
        assert prohibida not in enviado, f"llegó al modelo: {prohibida}"


def test_los_ejemplos_copian_solo_nombre_descripcion_y_detalle():
    ejemplos = ia.ejemplos_del_tono(COTIZACIONES)
    assert ejemplos
    for op in ejemplos:
        assert set(op) == {"nombre", "renglones"}
        for r in op["renglones"]:
            assert set(r) == {"descripcion", "detalle"}
    assert TOTAL not in json.dumps(ejemplos)


def test_ejemplos_siempre_los_de_la_maqueta_y_las_5_enviadas_mas_recientes():
    nombres = [o["nombre"] for o in ia.ejemplos_del_tono(COTIZACIONES)]
    assert nombres[:3] == [o["nombre"] for o in ia.EJEMPLOS_DE_LA_MAQUETA]
    # 7 enviadas (1..7) y un borrador más nuevo (9): quedan 7, 6, 5, 4, 3; el borrador nunca.
    assert nombres[3:] == [f"Opcion de prueba {PAL[n]}" for n in (7, 6, 5, 4, 3)]
    for caso in (None, {}, {1: []}):
        assert ia.ejemplos_del_tono(caso) == ia.EJEMPLOS_DE_LA_MAQUETA


def test_ejemplos_con_tope_de_caracteres_se_van_primero_las_mas_viejas(monkeypatch):
    grande = "x" * 700
    cotizaciones = {1: [_cotizacion(n, _dia(n), contenido={"opciones": [{"nombre": f"O{PAL[n]}", "renglones": [
        {"descripcion": grande, "detalle": ""}]}], "condiciones": []}) for n in range(1, 6)]}
    base = sum(ia._tamano_de_ejemplo(o) for o in ia.EJEMPLOS_DE_LA_MAQUETA)
    monkeypatch.setattr(ia, "TOPE_DE_LOS_EJEMPLOS", base + 2 * 710)
    nombres = [o["nombre"] for o in ia.ejemplos_del_tono(cotizaciones)][3:]
    assert nombres == [f"O{PAL[5]}", f"O{PAL[4]}"]


# ── 2. La respuesta no trae precio ──────────────────────────────────────────────────────────────────────

def test_el_borrador_solo_tiene_nombre_descripcion_y_detalle(modelo):
    _caso(modelo, {"total": "5", "opciones": [
        {"nombre": "A", "total": "9", "precio": "8", "renglones": [
            {"descripcion": "B", "detalle": "C", "total": "62500.00", "precio": "1", "monto": 3}]}]})
    salida = asyncio.run(ia.armar_borrador("quiere grabar", []))
    assert salida == [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": "C"}]}]


def test_la_redaccion_es_solo_el_texto_del_detalle(modelo):
    _caso(modelo, {"detalle": "Bajo y guira.", "total": "62500.00", "precio": "1"})
    assert asyncio.run(ia.mejorar_redaccion({"detalle": "bajo y guira"}, "", [])) == "Bajo y guira."


# ── 3. La guarda de cifras ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("propuesta,fuente,esperado", [
    ("Incluye RD$45,000 de estudio", "grabar un merengue", ["45000"]),
    ("8 horas de estudio", "grabar un merengue", ["8"]),
    ("deposito del 50%", "grabar un merengue", ["50"]),
    ("valido en 2026", "grabar un merengue", ["2026"]),
    ("2 canciones", "quiero 2 canciones", []),
    ("RD$ de lo acordado", "nada", []),                    # «RD$» sin número pasa
    ("45,000 de estudio", "nada", ["45000"]),               # un número sin «RD$» no pasa
    ("45.000 de estudio", "pagaria 45,000", []),            # misma cifra, otro separador de miles
    ("8h de bajo", "nada", ["8"]),
    ("x2 de guitarra", "nada", ["2"]),
    ("62,500.50", "62500.50", []),
    ("2,5 horas", "2.5 horas", []),
    ("sin cifras", "tampoco", []),
    ("2 y 2 y 3", "2", ["3"]),
])
def test_la_guarda_de_cifras_con_casos(propuesta, fuente, esperado):
    assert ia.cifras_que_no_estaban([propuesta], [fuente]) == esperado


def _escribir(n: int, rng: random.Random) -> str:
    """La cifra `n` escrita de una de las formas que existen."""
    miles = f"{n:,}"
    return rng.choice([str(n), miles, miles.replace(",", "."), f"{n}h", f"x{n}", f"RD${miles}", f"{n}%"])


def test_la_guarda_de_cifras_con_entradas_inventadas():
    rng = random.Random(20261016)
    for _ in range(300):
        dadas = [rng.randint(1, 900000) for _ in range(rng.randint(1, 3))]
        otra = rng.randint(1, 900000)
        while otra in dadas:
            otra = rng.randint(1, 900000)
        fuente = " y ".join(_escribir(n, rng) for n in dadas)
        propuesta_ok = " ".join(_escribir(n, rng) for n in dadas)
        assert ia.cifras_que_no_estaban([propuesta_ok], [fuente]) == []
        assert ia.cifras_que_no_estaban([propuesta_ok + " " + _escribir(otra, rng)], [fuente]) == [str(otra)]
        # y al revés: lo dado por la persona en la propuesta de otro y nada en la fuente
        assert ia.cifras_que_no_estaban([fuente], [propuesta_ok + " " + _escribir(otra, rng)]) == []
        assert ia.cifras_que_no_estaban([fuente], ["sin numeros"]) == [str(n) for n in dict.fromkeys(dadas)]


@pytest.mark.parametrize("escrito,cifra", [("RD$45,000", "45000"), ("8 horas", "8"), ("50%", "50"),
                                           ("2026", "2026")])
def test_una_propuesta_con_una_cifra_que_nadie_dio_no_se_enseña(modelo, escrito, cifra):
    _caso(modelo, {"detalle": f"Incluye {escrito}."})
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(ia.mejorar_redaccion({"detalle": "bajo y guira"}, "Opcion", []))
    assert e.value.causa == "cifra" and e.value.cifras == [cifra] and cifra in str(e.value)
    _caso(modelo, {"opciones": [{"nombre": "A", "renglones": [
        {"descripcion": "B", "detalle": f"Incluye {escrito}."}]}]})
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(ia.armar_borrador("grabar un merengue", []))
    assert e.value.causa == "cifra" and e.value.cifras == [cifra]


def test_una_cifra_que_la_persona_dio_pasa(modelo):
    _caso(modelo, {"detalle": "Grabacion de 2 canciones."})
    assert asyncio.run(ia.mejorar_redaccion({"detalle": "2 canciones"}, "", [])) == "Grabacion de 2 canciones."
    _caso(modelo, {"opciones": [{"nombre": "A", "renglones": [
        {"descripcion": "2 canciones", "detalle": ""}]}]})
    assert asyncio.run(ia.armar_borrador("quiere 2 canciones", []))[0]["renglones"][0]["descripcion"] == "2 canciones"


def test_una_cifra_que_solo_esta_en_los_ejemplos_se_rechaza(modelo):
    ejemplos = [{"nombre": "Ejemplo", "renglones": [{"descripcion": "Dura 2026 minutos", "detalle": ""}]}]
    _caso(modelo, {"detalle": "Dura 2026 minutos."})
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(ia.mejorar_redaccion({"detalle": "bajo y guira"}, "Opcion", ejemplos))
    assert e.value.cifras == ["2026"]
    _caso(modelo, {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "Dura 2026 minutos",
                                                                "detalle": ""}]}]})
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(ia.armar_borrador("grabar un merengue", ejemplos))
    assert e.value.cifras == ["2026"]


def test_la_cifra_del_nombre_de_la_opcion_o_de_la_descripcion_tambien_cuenta_como_dada(modelo):
    _caso(modelo, {"detalle": "Para 3 musicos y 4 horas."})
    assert asyncio.run(ia.mejorar_redaccion({"descripcion": "4 horas", "detalle": "bajo"}, "Banda de 3", []))


def test_frontera_declarada_la_guarda_no_ve_cifras_escritas_con_letras(modelo):
    """A PROPÓSITO: la guarda mira dígitos. «cincuenta mil pesos» pasa; lo único que hay
    detrás es decirle a la IA qué hacer y que su respuesta no tiene casilla de precio, y eso no lo impide. Este caso existe para que nadie crea que
    la guarda lo ve."""
    assert ia.cifras_que_no_estaban(["cincuenta mil pesos y dos horas"], ["nada"]) == []
    _caso(modelo, {"detalle": "Cuesta cincuenta mil pesos."})
    assert asyncio.run(ia.mejorar_redaccion({"detalle": "bajo"}, "", [])) == "Cuesta cincuenta mil pesos."


# ── 4. La propuesta pasa por la puerta del texto ────────────────────────────────────────────────────────

def test_rayas_y_comillas_tipograficas_salen_en_su_forma_simple(modelo):
    _caso(modelo, {"detalle": "Bajo — “guira” y ‘conga’…"})
    assert asyncio.run(ia.mejorar_redaccion({"detalle": "x"}, "", [])) == "Bajo - \"guira\" y 'conga'..."
    _caso(modelo, {"opciones": [{"nombre": "A—B", "renglones": [
        {"descripcion": "“C”", "detalle": "D…"}]}]})
    assert asyncio.run(ia.armar_borrador("x", [])) == [
        {"nombre": "A-B", "renglones": [{"descripcion": "\"C\"", "detalle": "D..."}]}]


def _renglon(**k):
    r = {"descripcion": "B", "detalle": "C"}
    r.update(k)
    return r


def _borrador(opciones):
    return {"opciones": opciones}


CASOS_QUE_NO_VALEN = {
    "emoji en el detalle": ("redaccion", {"detalle": "Bajo \U0001F600"}),
    "detalle demasiado largo": ("redaccion", {"detalle": "a" * (db.LARGO_DETALLE_DE_RENGLON + 1)}),
    "detalle vacio": ("redaccion", {"detalle": "   "}),
    "detalle que no es texto": ("redaccion", {"detalle": 5}),
    "sin detalle": ("redaccion", {}),
    "emoji en el nombre": ("borrador", _borrador([{"nombre": "\U0001F600", "renglones": [_renglon()]}])),
    "nombre demasiado largo": ("borrador", _borrador(
        [{"nombre": "a" * (db.LARGO_NOMBRE_DE_OPCION + 1), "renglones": [_renglon()]}])),
    "descripcion demasiado larga": ("borrador", _borrador(
        [{"nombre": "A", "renglones": [_renglon(descripcion="a" * (db.LARGO_DESCRIPCION_DE_RENGLON + 1))]}])),
    "descripcion vacia": ("borrador", _borrador([{"nombre": "A", "renglones": [_renglon(descripcion="")]}])),
    "7 opciones": ("borrador", _borrador(
        [{"nombre": "Opcion " + "abcdefghij"[i], "renglones": [_renglon()]}
         for i in range(db.MAX_OPCIONES_DE_COTIZACION + 1)])),
    "11 renglones": ("borrador", _borrador(
        [{"nombre": "A", "renglones": [_renglon() for _ in range(db.MAX_RENGLONES_DE_OPCION + 1)]}])),
    "sin opciones": ("borrador", _borrador([])),
    "opciones que no es lista": ("borrador", {"opciones": "hola"}),
    "opcion sin renglones": ("borrador", _borrador([{"nombre": "A", "renglones": []}])),
    "opcion sin la clave renglones": ("borrador", _borrador([{"nombre": "A"}])),
    "renglon que no es un objeto": ("borrador", _borrador([{"nombre": "A", "renglones": ["B"]}])),
    "emoji en el segundo renglon": ("borrador", _borrador(
        [{"nombre": "A", "renglones": [_renglon(), _renglon(detalle="\U0001F600")]}])),
}


@pytest.mark.parametrize("nombre", list(CASOS_QUE_NO_VALEN))
def test_una_propuesta_que_la_puerta_del_texto_rechaza_se_descarta_entera(modelo, nombre):
    boton, respuesta = CASOS_QUE_NO_VALEN[nombre]
    _caso(modelo, respuesta)
    llamada = (ia.mejorar_redaccion({"detalle": "bajo"}, "", []) if boton == "redaccion"
               else ia.armar_borrador("grabar", []))
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(llamada)
    assert e.value.causa == "no_sirve"
    assert str(e.value) == ia.CAUSAS["no_sirve"]


def test_el_limite_de_la_puerta_si_pasa(modelo):
    _caso(modelo, _borrador([{"nombre": "a" * db.LARGO_NOMBRE_DE_OPCION,
                              "renglones": [_renglon(descripcion="b" * db.LARGO_DESCRIPCION_DE_RENGLON,
                                                     detalle="")] * db.MAX_RENGLONES_DE_OPCION}]
                            * db.MAX_OPCIONES_DE_COTIZACION))
    salida = asyncio.run(ia.armar_borrador("grabar", []))
    assert len(salida) == db.MAX_OPCIONES_DE_COTIZACION
    assert len(salida[0]["renglones"]) == db.MAX_RENGLONES_DE_OPCION


# ── 5. Si la IA no contesta: una frase fija y nada más, una sola petición ───────────────────────────────

def _servicio_dice(codigo):
    return lambda r: httpx.Response(codigo, json={"error": {"message": SECRETO_DEL_SERVICIO}})


FALLOS_DE_RED = {
    "tiempo vencido en la red": lambda r: httpx.ReadTimeout(SECRETO_DEL_SERVICIO),
    "error 500": _servicio_dice(500),
    "error 429": _servicio_dice(429),
    "error 401 (falta la llave)": _servicio_dice(401),
    "red caida": lambda r: httpx.ConnectError(SECRETO_DEL_SERVICIO),
}

FALLOS_DE_CONTENIDO = {
    "JSON roto": SECRETO_DEL_SERVICIO + " {no es json",
    "content vacio": "",
    "content en blanco": "   ",
    "content nulo": None,
    "JSON que no es un objeto": "[1, 2]",
}


def _llamadas():
    return {"redaccion": lambda: ia.mejorar_redaccion({"detalle": "bajo"}, "", []),
            "borrador": lambda: ia.armar_borrador("grabar", [])}


@pytest.mark.parametrize("boton", ["redaccion", "borrador"])
@pytest.mark.parametrize("fallo", list(FALLOS_DE_RED))
def test_si_la_red_o_el_servicio_fallan_es_la_frase_fija_y_una_sola_peticion(modelo, caplog, boton, fallo):
    _caso(modelo, FALLOS_DE_RED[fallo])
    with caplog.at_level(NIVEL_DE_PRODUCCION):
        with pytest.raises(ia.IANoContesta) as e:
            asyncio.run(_llamadas()[boton]())
    assert len(modelo.peticiones) == 1, "hubo reintentos"
    assert str(e.value) == ia.CAUSAS["no_contesto"]
    assert SECRETO_DEL_SERVICIO not in repr(e.value) + str(e.value.args) + caplog.text
    assert e.value.__cause__ is None


@pytest.mark.parametrize("boton", ["redaccion", "borrador"])
@pytest.mark.parametrize("fallo", list(FALLOS_DE_CONTENIDO))
def test_si_la_respuesta_no_se_puede_usar_es_la_frase_fija(modelo, caplog, boton, fallo):
    _caso(modelo, FALLOS_DE_CONTENIDO[fallo])
    with caplog.at_level(NIVEL_DE_PRODUCCION):
        with pytest.raises(ia.IAPropuestaNoSirve) as e:
            asyncio.run(_llamadas()[boton]())
    assert len(modelo.peticiones) == 1
    assert e.value.causa == "no_sirve" and str(e.value) == ia.CAUSAS["no_sirve"]
    assert SECRETO_DEL_SERVICIO not in repr(e.value) + str(e.value.args) + caplog.text


def test_una_respuesta_sin_opciones_de_eleccion_es_la_frase_fija(modelo):
    _caso(modelo, lambda r: httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1,
                                                       "model": "m", "choices": []}))
    with pytest.raises(ia.IANoContesta):
        asyncio.run(ia.armar_borrador("grabar", []))


@pytest.mark.parametrize("boton", ["redaccion", "borrador"])
def test_el_tope_total_corta_una_respuesta_que_no_llega(modelo, monkeypatch, boton):
    async def lenta(request):
        await asyncio.sleep(5)
        return _completion("{}")
    monkeypatch.setattr(ia, "TOPE_REDACCION_S", 0.05)
    monkeypatch.setattr(ia, "TOPE_BORRADOR_S", 0.05)
    _caso(modelo, lambda r: lenta(r))
    with pytest.raises(ia.IANoContesta):
        asyncio.run(_llamadas()[boton]())
    assert len(modelo.peticiones) == 1


def test_sin_reintentos_aunque_el_cliente_de_siempre_los_tenga(modelo):
    """El cliente de `cerebro/deepseek.py` reintenta 2 veces; este módulo pide `max_retries=0`. El doble lo
    imita con `max_retries=2`: si el módulo no lo apaga, el 500 llegaría 3 veces."""
    assert ia.cliente.max_retries == 2
    _caso(modelo, _servicio_dice(500))
    with pytest.raises(ia.IANoContesta):
        asyncio.run(ia.armar_borrador("grabar", []))
    assert len(modelo.peticiones) == 1


# ── Lo que viaja a la red ───────────────────────────────────────────────────────────────────────────────

def test_cada_boton_pide_con_su_tope_el_modelo_de_siempre_y_json(modelo):
    _caso(modelo, {"detalle": "x", "opciones": [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": ""}]}]})
    asyncio.run(ia.mejorar_redaccion({"detalle": "bajo"}, "", []))
    asyncio.run(ia.armar_borrador("grabar", []))
    from cerebro.deepseek import MODELO
    for cuerpo in modelo.cuerpos():
        assert cuerpo["model"] == MODELO
        assert cuerpo["response_format"] == {"type": "json_object"}
        assert [m["role"] for m in cuerpo["messages"]] == ["system", "user"]
    topes = [p.extensions["timeout"]["read"] for p in modelo.peticiones]
    assert topes == [ia.TOPE_REDACCION_S, ia.TOPE_BORRADOR_S]
    assert ia.TOPE_REDACCION_S < ia.TOPE_BORRADOR_S


def test_el_mensaje_del_usuario_es_lo_que_escribio_la_persona(modelo):
    _caso(modelo, {"detalle": "x"})
    asyncio.run(ia.mejorar_redaccion({"descripcion": "Grabacion", "detalle": "bajo y guira"}, "Opcion A", []))
    usuario = json.loads(modelo.cuerpos()[0]["messages"][1]["content"])
    assert usuario == {"opcion": "Opcion A", "descripcion": "Grabacion", "detalle": "bajo y guira"}


@pytest.mark.parametrize("llamada,causa", [
    (lambda: ia.mejorar_redaccion({"detalle": "  "}, "", []), "detalle_vacio"),
    (lambda: ia.mejorar_redaccion({}, "", []), "detalle_vacio"),
    (lambda: ia.armar_borrador("   ", []), "pedido_vacio"),
    (lambda: ia.armar_borrador("a" * (ia.LARGO_DEL_PEDIDO + 1), []), "pedido_largo"),
])
def test_lo_vacio_o_demasiado_largo_no_llega_al_modelo(modelo, llamada, causa):
    with pytest.raises(ia.IAPropuestaNoSirve) as e:
        asyncio.run(llamada())
    assert e.value.causa == causa
    assert modelo.peticiones == []


def test_un_pedido_en_el_limite_si_se_manda(modelo):
    _caso(modelo, {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": ""}]}]})
    asyncio.run(ia.armar_borrador("a" * ia.LARGO_DEL_PEDIDO, []))
    assert len(modelo.peticiones) == 1


# ── 6. El registro anota tokens y tiempo, no texto ──────────────────────────────────────────────────────

PEDIDO = "PEDIDO-UNICO-DEL-CLIENTE-qwe"
PROPUESTA = "PROPUESTA-UNICA-DE-LA-IA-asd"


def test_el_registro_anota_boton_tokens_y_segundos_pero_no_texto(modelo, caplog):
    _caso(modelo, {"opciones": [{"nombre": PROPUESTA, "renglones": [{"descripcion": PROPUESTA, "detalle": PROPUESTA}]}]})
    with caplog.at_level(NIVEL_DE_PRODUCCION):
        asyncio.run(ia.armar_borrador(PEDIDO, []))
        _caso(modelo, {"detalle": PROPUESTA})
        asyncio.run(ia.mejorar_redaccion({"detalle": PEDIDO}, "", []))
    registro = [r for r in caplog.records if r.name == "lucy.cotizaciones_ia"]
    assert len(registro) == 2
    for r, boton in zip(registro, ("armar_borrador", "mejorar_redaccion")):
        m = r.getMessage()
        assert f"boton={boton}" in m and "prompt_tokens=1234" in m and "completion_tokens=567" in m
        assert "segundos=" in m
    assert PEDIDO not in caplog.text and PROPUESTA not in caplog.text


def test_el_registro_de_un_fallo_dice_la_clase_y_no_el_texto(modelo, caplog):
    _caso(modelo, FALLOS_DE_RED["tiempo vencido en la red"])
    with caplog.at_level(NIVEL_DE_PRODUCCION):
        with pytest.raises(ia.IANoContesta):
            asyncio.run(ia.armar_borrador(PEDIDO, []))
    registro = [r.getMessage() for r in caplog.records if r.name == "lucy.cotizaciones_ia"]
    assert len(registro) == 1 and "fallo=" in registro[0] and "boton=armar_borrador" in registro[0]
    assert PEDIDO not in caplog.text and SECRETO_DEL_SERVICIO not in caplog.text


def test_sin_usage_en_la_respuesta_tampoco_revienta(modelo):
    modelo.respuesta = lambda r: httpx.Response(200, json=_completion('{"detalle": "x"}', usage=False))
    assert asyncio.run(ia.mejorar_redaccion({"detalle": "bajo"}, "", [])) == "x"


# ── 7. Una sola llamada al modelo, declarada ────────────────────────────────────────────────────────────

def test_la_forma_de_la_respuesta_del_doble_es_la_de_la_biblioteca_instalada():
    """El doble habla en la forma de `openai.types.chat.ChatCompletion` de la biblioteca instalada (la del
    `requirements.txt`): si la biblioteca la cambiara, esto se pone rojo."""
    from openai.types.chat import ChatCompletion
    c = ChatCompletion.model_validate(_completion('{"a": 1}'))
    assert c.choices[0].message.content == '{"a": 1}'
    assert (c.usage.prompt_tokens, c.usage.completion_tokens) == (1234, 567)


def test_el_modulo_tiene_una_sola_llamada_al_modelo_y_es__pedir():
    arbol = ast.parse((RAIZ / "cotizaciones_ia.py").read_text(encoding="utf-8"))
    llamadas = []
    for f in ast.walk(arbol):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(f):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "create"):
                    llamadas.append(f.name)
    assert llamadas == ["_pedir"]


# ── Los ejemplos de la maqueta ──────────────────────────────────────────────────────────────────────────

def test_los_ejemplos_de_la_maqueta_estan_completos_y_sin_cifras_de_dinero():
    assert len(ia.EJEMPLOS_DE_LA_MAQUETA) == 3
    textos = []
    for op in ia.EJEMPLOS_DE_LA_MAQUETA:
        assert set(op) == {"nombre", "renglones"} and op["nombre"].strip()
        for r in op["renglones"]:
            assert set(r) == {"descripcion", "detalle"}
            assert r["descripcion"].strip() and r["detalle"].strip()
            textos += [r["descripcion"], r["detalle"]]
        textos.append(op["nombre"])
    assert ia.cifras_que_no_estaban(textos, []) == [], "un ejemplo trae una cifra"
    for t in textos:
        assert "RD$" not in t and "$" not in t
        db._texto_de_cotizacion(t, "ejemplo", 1000)    # y valen para la puerta del texto


def test_los_ejemplos_de_la_maqueta_viajan_al_modelo_y_no_cuentan_como_fuente(modelo):
    _caso(modelo, {"detalle": "Dura 3 horas."})
    with pytest.raises(ia.IAPropuestaNoSirve):   # la cifra no la dio la persona
        asyncio.run(ia.mejorar_redaccion({"detalle": "bajo"}, "", ia.ejemplos_del_tono(None)))
    assert "conservatorio" in modelo.texto_enviado()
    fuente_de_los_ejemplos = [t for o in ia.EJEMPLOS_DE_LA_MAQUETA for r in o["renglones"]
                              for t in (r["descripcion"], r["detalle"])]
    assert ia.cifras_que_no_estaban(["3"], fuente_de_los_ejemplos) == ["3"]


# ── Los ejemplos reales nunca traen precio ni cliente (H1 del testigo) ──────────────────────────────────

def _cot_con(detalle="Detalle limpio", descripcion="Descripcion limpia", nombre_op="Opcion limpia", n=1, **extra):
    return _cotizacion(n, _dia(n), contenido={"opciones": [{"nombre": nombre_op, "renglones": [
        {"descripcion": descripcion, "detalle": detalle, "total": TOTAL}]}], "condiciones": []}, **extra)


def _ejemplos_reales(*cots):
    return ia.ejemplos_del_tono({1: list(cots)})[len(ia.EJEMPLOS_DE_LA_MAQUETA):]


SUCIOS = {
    "la sonda del testigo": "Para Zulmira Quintanilla, RD$73,914.37 por la sesion",
    "el precio solo": "Cuesta RD$73,914.37",
    "una cifra suelta": "Incluye 8 horas",
    "el precio sin RD": "73914",
    "el nombre solo": "Sesion de Zulmira",
    "el apellido con otra mayuscula": "sesion de QUINTANILLA",
    "el apellido con acento": "sesion de Quintanílla",
    "el telefono con otro formato": "llamar al 829 555 7391",
    "el telefono con puntos": "829.555.7391",
    "un digito de otro alfabeto": "Incluye \u0663 horas",
}


@pytest.mark.parametrize("nombre", list(SUCIOS))
@pytest.mark.parametrize("campo", ["detalle", "descripcion", "nombre_op"])
def test_un_renglon_con_cifra_o_con_el_cliente_no_entra_como_ejemplo(nombre, campo):
    sucio = SUCIOS[nombre]
    cot = _cot_con(**{campo: sucio})
    assert _ejemplos_reales(cot) == []
    enviado = json.dumps(ia.ejemplos_del_tono({1: [cot]}), ensure_ascii=False)
    for prohibido in ("73,914.37", "Zulmira", "Quintanilla", "7391"):
        assert prohibido not in enviado


def test_el_renglon_sucio_se_deja_fuera_y_los_limpios_de_la_misma_opcion_entran():
    cot = _cotizacion(1, _dia(1), contenido={"opciones": [{"nombre": "Banda", "renglones": [
        {"descripcion": "Limpio uno", "detalle": "Texto limpio", "total": TOTAL},
        {"descripcion": "Sucio", "detalle": "RD$73,914.37", "total": TOTAL},
        {"descripcion": "Limpio dos", "detalle": "", "total": TOTAL}]}], "condiciones": []})
    assert _ejemplos_reales(cot) == [{"nombre": "Banda", "renglones": [
        {"descripcion": "Limpio uno", "detalle": "Texto limpio"},
        {"descripcion": "Limpio dos", "detalle": ""}]}]


def test_las_palabras_cortas_del_nombre_del_cliente_no_dejan_fuera_a_nadie():
    cot = _cot_con("Sesion de la banda", para_nombre="Juan de la Cruz")
    assert len(_ejemplos_reales(cot)) == 1
    assert _ejemplos_reales(_cot_con("Sesion con Cruz", para_nombre="Juan de la Cruz")) == []


def test_entradas_inventadas_de_cliente_y_cifras():
    rng = random.Random(7)
    letras = "abcdefghijklmnopqrstuvwxyz"
    for _ in range(200):
        palabra = "".join(rng.choice(letras) for _ in range(rng.randint(3, 9)))
        tel = "".join(rng.choice("0123456789") for _ in range(10))
        cot = _cot_con("Texto", para_nombre=f"Fulano {palabra}", para_telefono=tel)
        assert len(_ejemplos_reales(cot)) == 1
        for sucio in (f"hola {palabra.upper()} adios", f"{palabra}", f"x {rng.randint(0, 99999)} y",
                      f"tel {tel[:3]}-{tel[3:6]} {tel[6:]}"):
            for campo in ("detalle", "descripcion", "nombre_op"):
                cot = _cot_con(**{campo: sucio}, para_nombre=f"Fulano {palabra}", para_telefono=tel)
                assert _ejemplos_reales(cot) == [], sucio


def test_una_enviada_sin_opciones_no_corta_las_siguientes():
    vacia = _cotizacion(8, _dia(20), contenido={"opciones": [], "condiciones": []})
    sucia = _cot_con("RD$5", n=9)
    sucia["enviada_en"] = _dia(25)
    buena = _cotizacion(2, _dia(2))
    assert [o["nombre"] for o in _ejemplos_reales(vacia, sucia, buena)] == [f"Opcion de prueba {PAL[2]}"]


# ── La instruccion «no escribas montos» está en el mensaje de sistema (H2 del testigo) ──────────────────

def test_los_dos_botones_le_dicen_a_la_ia_que_no_escriba_montos_y_que_escriba_sin_cantidad(modelo):
    _caso(modelo, {"detalle": "x", "opciones": [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": ""}]}]})
    asyncio.run(ia.mejorar_redaccion({"detalle": "bajo"}, "", []))
    asyncio.run(ia.armar_borrador("grabar", []))
    sistemas = [c["messages"][0]["content"] for c in modelo.cuerpos()]
    assert len(sistemas) == 2
    for sistema in sistemas:
        assert "no escribas montos ni porcentajes" in sistema
        assert "escribe sin cantidad" in sistema
        assert "úsala tal cual" in sistema
