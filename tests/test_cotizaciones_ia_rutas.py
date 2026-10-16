"""Pieza 2 de «IA para los textos de las cotizaciones»: las dos rutas
(`POST /proyectos/{pid}/cotizaciones/ia/redaccion` y `…/ia/borrador`, en `web/app.py`).

Qué se vigila: casa → 200 con la propuesta; solo ver y sin sesión → 401 y el modelo NO recibe ninguna petición;
IA caída → 503 con la frase fija; propuesta que no sirve → 422 con la causa; las rutas NO escriben (base, Drive).

El modelo se dobla EN LA RED, como en `tests/test_cotizaciones_ia.py` (un `AsyncOpenAI` de verdad sobre
`httpx.MockTransport`; se reusan su doble y su fixture): corre el camino de producción entero, desde la ruta hasta
la lectura del `content`. Nada llama al modelo real. La base es la de las pruebas de la página (SQLite con el mismo
texto SQL).

FRONTERA, dicha: «no escriben» se comprueba con (a) la base de prueba entera antes y después y (b) las puertas de
escritura de cotizaciones y de Drive reemplazadas por una que revienta si alguien las llama. No ve un envío por
Telegram: ninguna ruta de este archivo importa nada de Telegram, y lo vigila la lectura del código, no una prueba.
"""
from __future__ import annotations

import json
import re

import pytest

from test_cotizaciones_en_la_pagina import (CASA, _casa, _guardar, _ver, m,  # noqa: F401  (fixture `m`)
                                            tabla_ausente_como_postgres)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)
from test_cotizaciones_ia import (SECRETO_DEL_SERVICIO, Modelo, bibliotecas_de_verdad,  # noqa: F401  (fixture autouse)
                                  modelo)
import cotizaciones_drive
import cotizaciones_ia as ia
import db.db as db
import web.app as panel
import web.auth as auth
from _navegador import Navegador

REDACCION = "/proyectos/1/cotizaciones/ia/redaccion"
BORRADOR = "/proyectos/1/cotizaciones/ia/borrador"

CUERPO_DE_REDACCION = {"opcion": "Opción uno", "descripcion": "Grabación de bajo", "detalle": "bajo, conga y guira"}
CUERPO_DE_BORRADOR = {"pedido": "quiere grabar un merengue con bajo y guira"}
RESPUESTA_DE_REDACCION = {"detalle": "Grabación de bajo, conga y güira."}
RESPUESTA_DE_BORRADOR = {"opciones": [{"nombre": "Opción A", "renglones": [
    {"descripcion": "Grabación de bajo y güira", "detalle": "Incluye horas de estudio.", "total": "5"}]}]}

CASOS = [(REDACCION, CUERPO_DE_REDACCION, RESPUESTA_DE_REDACCION),
         (BORRADOR, CUERPO_DE_BORRADOR, RESPUESTA_DE_BORRADOR)]


def _sin_sesion() -> Navegador:
    return Navegador(panel.app, base_url="https://testserver")


def _toda_la_base(mundo) -> list[str]:
    return list(mundo.con.iterdump())


class _Llamadas(list):
    """Las llamadas a las puertas de escritura; `guardia` es falso mientras la prueba prepara sus datos."""
    guardia = True


@pytest.fixture
def sin_escribir(monkeypatch):
    """Las puertas por las que una cotización se escribe o se sube revientan si alguien las llama (después de que la
    prueba pone `sin_escribir.guardia = True`; por defecto ya está puesta)."""
    llamadas = _Llamadas()
    originales = {}

    def _prohibida(nombre):
        async def f(*a, **k):
            if not llamadas.guardia:
                return await originales[nombre](*a, **k)
            llamadas.append(nombre)
            raise AssertionError(f"{nombre} no se llama desde las rutas de la IA")
        return f
    for nombre in ("guardar_cotizacion", "rehacer_carpetas_de_drive"):
        assert hasattr(db, nombre), nombre
        originales[f"db.{nombre}"] = getattr(db, nombre)
        monkeypatch.setattr(db, nombre, _prohibida(f"db.{nombre}"))
    for nombre in ("subir", "rehacer_la_carpeta"):
        originales[f"cotizaciones_drive.{nombre}"] = getattr(cotizaciones_drive, nombre)
        monkeypatch.setattr(cotizaciones_drive, nombre, _prohibida(f"cotizaciones_drive.{nombre}"))
    return llamadas


def _rutas_de_la_ia(app) -> list[str]:
    """Todo lo registrado que es POST y vive bajo `/cotizaciones/ia/`: SALE DE `app.routes`, no de una lista."""
    return sorted(r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or ())
                  and "/cotizaciones/ia/" in r.path)


# ── Casa: 200 con la propuesta ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ruta,cuerpo,respuesta", CASOS)
def test_la_casa_recibe_la_propuesta_con_una_sola_peticion_al_modelo(m, modelo, sin_escribir, ruta, cuerpo, respuesta):
    modelo.respuesta = respuesta
    r = _casa().post(ruta, json=cuerpo)
    assert r.status_code == 200, r.text
    assert len(modelo.peticiones) == 1
    if ruta == REDACCION:
        assert r.json() == {"detalle": "Grabación de bajo, conga y güira."}
    else:
        # el `total` que mandó el modelo no llega: solo nombre, descripcion y detalle
        assert r.json() == {"opciones": [{"nombre": "Opción A", "renglones": [
            {"descripcion": "Grabación de bajo y güira", "detalle": "Incluye horas de estudio."}]}]}
    assert "total" not in r.text and sin_escribir == []


def test_lo_que_recibe_el_modelo_es_lo_que_escribio_la_persona_y_los_ejemplos_sin_precio_ni_cliente(m, modelo):
    """Camino de producción: la ruta lee las cotizaciones de la base (`db.cotizaciones_de_proyectos`) y arma los
    ejemplos con `ejemplos_del_tono`. Una enviada con un total, un cliente y un teléfono únicos: su texto viaja; esos
    tres no."""
    contenido = {"opciones": [{"nombre": "Mezcla y master de un sencillo", "renglones": [
        {"descripcion": "Mezcla de una canción", "detalle": "Incluye revisión de la mezcla", "total": "73914.37"}]}],
        "condiciones": [{"clave": "validez", "texto": "x", "marcada": True}]}
    _guardar("enviar", contenido=contenido, para_nombre="Zulmira Quintanilla", para_telefono="829-555-7391")
    modelo.respuesta = RESPUESTA_DE_REDACCION
    assert _casa().post(REDACCION, json=CUERPO_DE_REDACCION).status_code == 200
    enviado = modelo.texto_enviado()
    assert "Mezcla y master de un sencillo" in enviado, "el ejemplo de la base no llegó: la prueba no mide nada"
    for prohibida in ("73914", "73,914", "Quintanilla", "Zulmira", "829-555-7391", "5557391"):
        assert prohibida not in enviado, prohibida
    assert "bajo, conga y guira" in enviado          # lo que la persona escribió va tal cual


def test_si_no_se_pueden_leer_las_cotizaciones_la_ia_usa_los_ejemplos_de_siempre(m, modelo, monkeypatch):
    async def _falla():
        raise RuntimeError("base caída")
    monkeypatch.setattr(db, "cotizaciones_de_proyectos", _falla)
    modelo.respuesta = RESPUESTA_DE_REDACCION
    r = _casa().post(REDACCION, json=CUERPO_DE_REDACCION)
    assert r.status_code == 200 and r.json() == {"detalle": "Grabación de bajo, conga y güira."}
    assert ia.EJEMPLOS_DE_LA_MAQUETA[0]["nombre"] in modelo.texto_enviado()


# ── Solo ver y sin sesión: 401 y el modelo no recibe nada ───────────────────────────────────────────────

@pytest.mark.parametrize("quien", [_ver, _sin_sesion], ids=["solo_ver", "sin_sesion"])
def test_cada_ruta_de_la_ia_registrada_da_401_sin_sesion_de_la_casa_y_no_llama_al_modelo(m, modelo, sin_escribir, quien):
    rutas = _rutas_de_la_ia(panel.app)
    assert rutas == ["/proyectos/{pid}/cotizaciones/ia/borrador", "/proyectos/{pid}/cotizaciones/ia/redaccion"], rutas
    modelo.respuesta = RESPUESTA_DE_BORRADOR           # si la ruta llegara al modelo, tendría con qué contestar
    for ruta in rutas:
        url = ruta.replace("{pid}", "1")
        for cuerpo in (CUERPO_DE_REDACCION, CUERPO_DE_BORRADOR, {}, None):
            r = quien().post(url, json=cuerpo)
            assert r.status_code == 401, (ruta, cuerpo, r.status_code)
            assert "error" in r.json()
    assert modelo.peticiones == [] and sin_escribir == []


def test_la_cookie_de_solo_ver_no_es_una_sesion_de_la_casa_en_las_rutas_de_la_ia(m, modelo):
    """Las dos cookies a la vez: manda la de la casa (como en el resto del panel) y entra."""
    c = _casa()
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    modelo.respuesta = RESPUESTA_DE_REDACCION
    assert c.post(REDACCION, json=CUERPO_DE_REDACCION).status_code == 200


# ── La IA no contesta: 503 con la frase fija ────────────────────────────────────────────────────────────

def _cae_de_cuatro_maneras():
    import httpx
    return {"500": lambda req: httpx.Response(500, json={"error": SECRETO_DEL_SERVICIO}),
            "red": lambda req: httpx.ConnectError(SECRETO_DEL_SERVICIO),
            "tiempo": lambda req: httpx.ReadTimeout(SECRETO_DEL_SERVICIO),
            "429": lambda req: httpx.Response(429, json={"error": SECRETO_DEL_SERVICIO})}


@pytest.mark.parametrize("ruta,cuerpo,_", CASOS)
@pytest.mark.parametrize("forma", ["500", "red", "tiempo", "429"])
def test_si_la_ia_no_contesta_es_503_con_la_frase_fija_y_nada_del_servicio(m, modelo, sin_escribir, ruta, cuerpo, _, forma):
    modelo.respuesta = _cae_de_cuatro_maneras()[forma]
    r = _casa().post(ruta, json=cuerpo)
    assert r.status_code == 503, (r.status_code, r.text)
    assert r.json() == {"error": ia.CAUSAS["no_contesto"], "causa": "no_contesto"}
    assert SECRETO_DEL_SERVICIO not in r.text
    assert len(modelo.peticiones) == 1, "una sola petición: la ruta no reintenta"
    assert sin_escribir == []


# ── La propuesta no sirve: 422 con la causa ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ruta,cuerpo,contesta,causa", [
    (REDACCION, CUERPO_DE_REDACCION, "esto no es json", "no_sirve"),
    (REDACCION, CUERPO_DE_REDACCION, {"detalle": ""}, "no_sirve"),
    (REDACCION, CUERPO_DE_REDACCION, {"detalle": "Incluye RD$45,000 de estudio"}, "cifra"),
    (BORRADOR, CUERPO_DE_BORRADOR, {"opciones": []}, "no_sirve"),
    (BORRADOR, CUERPO_DE_BORRADOR, {"opciones": [{"nombre": "A", "renglones": [
        {"descripcion": "B", "detalle": "Dura 8 horas"}]}]}, "cifra"),
    (BORRADOR, CUERPO_DE_BORRADOR, {"opciones": [{"nombre": "A 😀", "renglones": [
        {"descripcion": "B", "detalle": "C"}]}]}, "no_sirve"),
])
def test_una_propuesta_que_no_sirve_es_422_con_su_causa_y_no_enseña_nada(m, modelo, sin_escribir, ruta, cuerpo, contesta, causa):
    modelo.respuesta = contesta
    r = _casa().post(ruta, json=cuerpo)
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["causa"] == causa
    assert set(r.json()) == {"error", "causa"}, "ni detalle ni opciones: no se enseña una propuesta descartada"
    if causa == "cifra":
        assert "tú no diste" in r.json()["error"]
    assert sin_escribir == []


@pytest.mark.parametrize("ruta,cuerpo,causa", [
    (REDACCION, {"opcion": "A", "descripcion": "B", "detalle": "   "}, "detalle_vacio"),
    (REDACCION, {}, "detalle_vacio"),
    (BORRADOR, {"pedido": "   "}, "pedido_vacio"),
    (BORRADOR, {}, "pedido_vacio"),
    (BORRADOR, {"pedido": "x" * (ia.LARGO_DEL_PEDIDO + 1)}, "pedido_largo"),
])
def test_lo_pedido_que_no_sirve_es_422_y_el_modelo_no_recibe_nada(m, modelo, ruta, cuerpo, causa):
    modelo.respuesta = RESPUESTA_DE_BORRADOR
    r = _casa().post(ruta, json=cuerpo)
    assert r.status_code == 422 and r.json()["causa"] == causa, r.text
    assert modelo.peticiones == []


@pytest.mark.parametrize("ruta", [REDACCION, BORRADOR])
@pytest.mark.parametrize("crudo", [b"no es json", b"[1, 2]", b'"texto"', b"", b"\xff\xfe"])
def test_un_cuerpo_que_no_es_un_objeto_json_es_400_y_el_modelo_no_recibe_nada(m, modelo, ruta, crudo):
    modelo.respuesta = RESPUESTA_DE_BORRADOR
    r = _casa().post(ruta, content=crudo, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert modelo.peticiones == []


@pytest.mark.parametrize("ruta", [REDACCION, BORRADOR])
@pytest.mark.parametrize("valor", [5, None, ["a"], {"a": 1}, True])
def test_campos_de_otro_tipo_no_revientan_la_ruta(m, modelo, ruta, valor):
    modelo.respuesta = RESPUESTA_DE_BORRADOR
    r = _casa().post(ruta, json={"opcion": valor, "descripcion": valor, "detalle": valor, "pedido": valor})
    assert r.status_code == 422, r.text
    assert modelo.peticiones == []


# ── Las rutas no escriben nada ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ruta,cuerpo,respuesta", CASOS)
@pytest.mark.parametrize("final", ["bien", "cae", "no_sirve"])
def test_las_rutas_no_escriben_en_la_base_ni_suben_nada_a_drive(m, modelo, sin_escribir, ruta, cuerpo, respuesta, final):
    """La base de prueba ENTERA (todas las tablas) antes y después, en los tres finales posibles, con una cotización
    enviada y un borrador ya guardados. Y las puertas de escritura revientan si se llaman (`sin_escribir`)."""
    sin_escribir.guardia = False
    _guardar("enviar")
    _guardar("borrador")
    sin_escribir.guardia = True
    antes = _toda_la_base(m)
    assert any("INSERT INTO \"cotizaciones\"" in s for s in antes), "la prueba parte de cotizaciones guardadas"
    modelo.respuesta = {"bien": respuesta, "cae": _cae_de_cuatro_maneras()["500"], "no_sirve": "no es json"}[final]
    _casa().post(ruta, json=cuerpo)
    assert _toda_la_base(m) == antes
    assert sin_escribir == []


# ── La plantilla y el guion de la página no mandan al formulario nada de esto (pieza 3) ──────────────────

def test_el_cuerpo_de_cada_ruta_no_trae_precio_ni_cliente_aunque_el_modelo_los_mande(m, modelo):
    modelo.respuesta = {"opciones": [{"nombre": "A", "total": "9", "para_nombre": "Fulano", "precio": "8",
                                       "renglones": [{"descripcion": "B", "detalle": "C", "total": "62500.00",
                                                      "para_telefono": "8095550142"}]}], "total": "5"}
    r = _casa().post(BORRADOR, json=CUERPO_DE_BORRADOR)
    assert r.status_code == 200
    assert not re.search(r"total|precio|para_|62500|Fulano|8095550142", r.text), r.text
    assert json.loads(r.text) == {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "B", "detalle": "C"}]}]}
