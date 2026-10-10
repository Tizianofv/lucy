"""Buscar una sesión del registro y agregarla a un proyecto (parte 13 de la página de
un proyecto, 9-oct-2026): las garantías de la fila 13 del diseño aprobado.

QUÉ SE VIGILA, una prueba por garantía (los punto y coma de la fila):
  · el texto de la búsqueda llega limpio a la App;
  · al agregar se vuelve a pedir la sesión a la App, y una que no existe no se agrega;
  · agregar una que ya entra sola no guarda nada;
  · agregar una que estaba quitada la devuelve;
  · nunca queda una sesión quitada y agregada a la vez (la base lo impide), y quitar una
    agregada la saca de su propia fila;
  · una agregada que la App ya no devuelve o que está cancelada se avisa con su código y
    se puede sacar;
  · ese aviso no sale si la App no contestó;
  · las agregadas se quedan al cambiar el cliente;
  · buscar, agregar y sacar son solo de la sesión de la casa;
  · funciona sin JavaScript.
Más el escapado de TODO lo nuevo que se pinta (las candidatas del buscador, con su
`nombre`, y los avisos, con el código guardado).

CÓMO. La base es la de SQLite con el esquema real (`tests/test_pagina_proyectos.py::mundo`),
así que el SQL de `sesiones_de_proyecto` se ejecuta de verdad; la App se dobla EN LA RED
(`tests/_app_de_registro.py`) y la página se pide por la RUTA real y la plantilla real.

FRONTERA, dicha para que no se dé por cubierta: el doble afirma el contrato que la sala le pasó
a Lucy (la forma de `/api/lucy/sesiones` con `&sesion=` y la de `/api/lucy/sesiones/buscar`), NO
con qué regla la App reconoce las sesiones de una ficha ni cómo busca (mayúsculas, tildes), ni
que la App de verdad conteste así. Nada de esto se corrió contra la App real ni contra Postgres.

Correr:  python3 -m pytest tests/test_sesiones_agregadas.py -q
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

from _app_de_registro import AppDeRegistro  # noqa: E402
from _navegador import Navegador  # noqa: E402
from test_pagina_proyectos import gente, mundo, ver_r  # noqa: E402,F401
import config  # noqa: E402
import db.db as db  # noqa: E402
import registro_lectura  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

# Nada de esto es real: ni la llave, ni las sesiones, ni el cliente.
LLAVE = "llave-de-prueba"
HOY = "2026-10-09"
DUENO = config.CHAT_ID_DUENO
HOSTIL = "<b>hostil</b>"

# Del cliente del proyecto (entran solas por la ficha) y dos que NO son suyas (solo salen si se
# piden una por una con `&sesion=`): una se agrega a mano, la otra se cancela.
SESION = {"ref": 11, "codigo": "s011", "es_trabajo": False, "fecha": HOY, "sala": "Sala P",
          "sala_mostrar": "Sala P", "horas": 3, "servicio": "Grabación", "atendio": "Ivan",
          "estado": "CONFIRMADA", "cancelada": False,
          "total": 9000.0, "abonado": 9000.0, "saldo": 0.0}
TRABAJO = {"ref": 12, "codigo": "t012", "es_trabajo": True, "fecha": HOY, "sala": "Sala P",
           "sala_mostrar": "Sala P", "horas": 2, "servicio": "Mezcla", "asignado_a": "Rosi",
           "estado": "EN PROCESO", "cancelada": False,
           "total": 6000.0, "abonado": 0.0, "saldo": 6000.0}
OTRA = {"ref": 21, "codigo": "x021", "es_trabajo": False, "fecha": HOY, "sala": "Sala Q",
        "sala_mostrar": "Sala Q", "horas": 2, "servicio": "Voces", "nombre": "Ana Pérez",
        "estado": "CONFIRMADA", "cancelada": False,
        "total": 5000.0, "abonado": 0.0, "saldo": 5000.0}
OTRO = {"ref": 22, "codigo": "x022", "es_trabajo": False, "fecha": HOY, "sala": "Sala Q",
        "sala_mostrar": "Sala Q", "horas": 1, "servicio": "Edición", "nombre": "Luis Gómez",
        "estado": "CONFIRMADA", "cancelada": False,
        "total": 2000.0, "abonado": 2000.0, "saldo": 0.0}


def _respuesta(sesiones=(SESION, TRABAJO), *, puede_ligar=True, motivo=""):
    return {"puede_ligar": puede_ligar, "motivo_sin_ligar": motivo, "sesiones": list(sesiones)}


@pytest.fixture
def registro(monkeypatch):
    """La App de mentira, con su llave puesta, y Lucy apuntada a ella."""
    doble = AppDeRegistro()
    doble.llave = LLAVE
    doble.sesiones = _respuesta()
    doble.otras = [OTRA, OTRO]          # las que no son del cliente: solo salen por `&sesion=`
    monkeypatch.setattr(config, "REGISTRO_URL", doble.url)
    monkeypatch.setattr(config, "LUCY_LLAVE_SERVICIO", LLAVE)
    try:
        yield doble
    finally:
        doble.cerrar()


@pytest.fixture
def dos(mundo, registro):
    """Dos proyectos del MISMO cliente y uno sin cliente, sobre la base real."""
    mundo.proyecto(1, "Disco Uno", area="CDS", cliente="Cliente X")
    mundo.proyecto(2, "Disco Dos", area="CDS", cliente="Cliente X")
    mundo.proyecto(3, "Sin cliente", area="CDS")
    return mundo


# ═══════════════════════════════════════════════════════════════════════
# La página, por la ruta real; y los POST, con el navegador de mentira
# ═══════════════════════════════════════════════════════════════════════

def _cliente(con="casa") -> Navegador:
    c = Navegador(panel.app)
    if con == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif con == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def _cliente_de(chat) -> Navegador:
    """La cookie de la CASA de ese chat, entre o no al panel."""
    c = Navegador(panel.app)
    c.cookies.set(panel.COOKIE, auth.crear_token(chat, auth.VIDA_SESION))
    return c


def _ver(con="casa", **consulta) -> str:
    r = _cliente(con).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:400]
    return r.text


def quitar(pid, ref, *, con="casa"):
    return _cliente(con).post(f"/proyectos/{pid}/sesiones/quitar", data={"ref": ref},
                              follow_redirects=False)


def agregar(pid, ref, *, con="casa"):
    return _cliente(con).post(f"/proyectos/{pid}/sesiones/agregar", data={"ref": ref},
                              follow_redirects=False)


def _donde(r) -> str:
    return r.headers["location"]


def _bloque(html: str) -> str:
    if 'id="sesiones-y-trabajos"' not in html:
        return ""
    dentro = html.split('id="sesiones-y-trabajos"', 1)[1].split("</section>", 1)[0]
    return " ".join(dentro.split())


def _listas(bloque: str) -> str:
    """Los renglones de las dos listas que CUENTAN (antes de los totales): lo que está aquí es lo
    que la casa ve en la lista del proyecto."""
    return bloque.split('<div class="cifras">')[0]


def _quitadas(bloque: str) -> str:
    """El trozo de la lista «Quitadas», o ''."""
    return bloque.split("<h3>Quitadas</h3>", 1)[1] if "<h3>Quitadas</h3>" in bloque else ""


def _avisos(bloque: str) -> str:
    return bloque.split('<div class="etapa avisos-sesion">', 1)[1] if "avisos-sesion" in bloque else ""


def _filas(mundo, pid=None) -> list[dict]:
    sql = "SELECT * FROM sesiones_de_proyecto"
    params: tuple = ()
    if pid is not None:
        sql, params = sql + " WHERE proyecto_id = ?", (pid,)
    return [dict(f) for f in mundo.con.execute(sql + " ORDER BY id", params)]


def _vivas(mundo, pid=None) -> list[dict]:
    return [f for f in _filas(mundo, pid) if f["borrado_en"] is None]


def _huellas(mundo) -> list[dict]:
    return [dict(f) for f in mundo.con.execute(
        "SELECT * FROM log_acciones WHERE tabla = 'sesiones_de_proyecto' ORDER BY id")]


def _ficha(mundo, pid) -> int:
    return mundo.con.execute(
        "SELECT cliente_noco_id FROM proyectos WHERE id = ?", (pid,)).fetchone()[0]


def _correr(espera):
    """Una función `async` de `db`, desde una prueba sincrónica."""
    import asyncio
    return asyncio.run(espera)


def _noco(fichas):
    async def leer(noco_id):
        return fichas[noco_id]
    return leer


def _rutas_al_registro(registro) -> list[str]:
    return [p["ruta"] for p in registro.pedidos]


# ═══════════════════════════════════════════════════════════════════════
# Garantía: el texto de la búsqueda llega limpio a la App
# ═══════════════════════════════════════════════════════════════════════

def test_el_texto_de_la_busqueda_llega_limpio_a_la_app(dos, registro):
    """Los caracteres con los que se armaría una consulta propia no viajan (como el buscador de
    personas: `noco_lectura._limpio`). Lo que vuelve se pinta: fecha, sala, concepto, el nombre de
    quien reservó y el código."""
    registro.busqueda = {"sesiones": [OTRA], "hay_mas": True}
    html = _ver(p=1, sq="Pérez,Juan (a)~b")

    buscar = [r for r in _rutas_al_registro(registro) if r.split("?")[0] == "/api/lucy/sesiones/buscar"]
    assert buscar, "no le preguntó al registro por la búsqueda"
    crudo = buscar[-1]
    assert "," not in crudo and "(" not in crudo and "~" not in crudo, crudo
    assert parse_qs(urlsplit(crudo).query)["q"][0] == "Pérez Juan a b", crudo
    assert registro_lectura._limpio("Pérez,Juan (a)~b") == "Pérez Juan a b"

    bloque = _bloque(html)
    assert "x021" in bloque and "Ana Pérez" in bloque and "Voces" in bloque, bloque
    assert "Hay más de 20" in bloque, bloque


def test_sin_texto_que_buscar_no_se_le_pregunta_al_registro(dos, registro):
    """Abrir el proyecto (sin `sq`) no busca; con texto que queda vacío al limpiarlo, tampoco."""
    _ver(p=1)
    _ver(p=1, sq="  ,()~ ")
    assert all(r.split("?")[0] != "/api/lucy/sesiones/buscar" for r in _rutas_al_registro(registro))


# ═══════════════════════════════════════════════════════════════════════
# Garantía: al agregar se vuelve a pedir la sesión; una que no existe no se agrega
# ═══════════════════════════════════════════════════════════════════════

def test_al_agregar_se_vuelve_a_pedir_la_sesion_y_una_que_no_esta_no_se_agrega(dos, registro):
    ficha = _ficha(dos, 1)
    registro.pedidos.clear()
    r = agregar(1, 21)
    assert r.status_code == 303 and "hecho=sesion_agregada" in _donde(r), _donde(r)
    assert _rutas_al_registro(registro) == [
        f"/api/lucy/sesiones?persona={ficha}",
        f"/api/lucy/sesiones?persona={ficha}&sesion=21"], _rutas_al_registro(registro)
    vivas = _vivas(dos, 1)
    assert [(f["sesion_ref"], f["codigo"], f["modo"]) for f in vivas] == [("21", "x021", "agregada")]

    r = agregar(1, 9999)
    assert "error=sesion_agregar_no_esta" in _donde(r), _donde(r)
    assert len(_vivas(dos, 1)) == 1, "se guardó una sesión que el registro no devolvió"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: agregar una que ya entra sola no guarda nada
# ═══════════════════════════════════════════════════════════════════════

def test_agregar_una_que_ya_entra_sola_no_guarda_nada(dos):
    r = agregar(1, 11)                      # SESION es del cliente: entra sola
    assert "error=sesion_agregar_igual" in _donde(r), _donde(r)
    assert "Esa sesión ya está en la lista de este proyecto: no se guardó nada." in \
        ver_r(dos, error="sesion_agregar_igual", p=1)
    assert _filas(dos) == [] and _huellas(dos) == []


# ═══════════════════════════════════════════════════════════════════════
# Garantía: agregar una que estaba quitada la devuelve
# ═══════════════════════════════════════════════════════════════════════

def test_agregar_una_que_estaba_quitada_la_devuelve(dos):
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    assert "s011" not in _listas(_bloque(_ver(p=1)))

    r = agregar(1, 11)
    assert "hecho=sesion_devuelta" in _donde(r), _donde(r)
    assert "s011" in _listas(_bloque(_ver(p=1))), "no volvió a la lista"
    assert len(_vivas(dos, 1)) == 0, "quedó una decisión viva"
    assert [(f["modo"], f["borrado_en"] is not None) for f in _filas(dos, 1)] == [("quitada", True)]


def test_una_quitada_que_el_registro_no_devuelve_no_se_devuelve(dos, registro):
    """Antes de devolver hay que preguntarle al registro: una quitada que ya no devuelve se queda
    como estaba (su fila, viva), en vez de deshacer la decisión de la casa sin que nadie lo pida."""
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    registro.sesiones = _respuesta(sesiones=(TRABAJO,))          # el registro ya no la devuelve
    registro.otras = []
    registro.pedidos.clear()
    r = agregar(1, 11)
    assert "error=sesion_agregar_no_esta" in _donde(r), _donde(r)
    assert registro.pedidos, "no le preguntó al registro antes de decidir"
    assert [f["borrado_en"] for f in _filas(dos, 1)] == [None], "deshizo la decisión sin preguntar"
    assert [f["sesion_ref"] for f in _vivas(dos, 1)] == ["11"]
    assert "s011" not in _listas(_bloque(_ver(p=1)))

    # Y cuando el registro la vuelve a devolver, sigue quitada (hasta que se pida devolverla).
    registro.sesiones = _respuesta(sesiones=(SESION, TRABAJO))
    assert "s011" not in _listas(_bloque(_ver(p=1))), "la quitada se devolvió sola"
    assert "hecho=sesion_devuelta" in _donde(agregar(1, 11))
    assert "s011" in _listas(_bloque(_ver(p=1)))


# ═══════════════════════════════════════════════════════════════════════
# Garantía: nunca quitada y agregada a la vez (la base lo impide); quitar una agregada la saca
# ═══════════════════════════════════════════════════════════════════════

def test_la_base_impide_una_quitada_y_una_agregada_a_la_vez(dos):
    """El índice único parcial de la tabla, EJECUTADO: la misma sesión no puede tener dos
    decisiones vivas, en ningún orden."""
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    with pytest.raises(sqlite3.IntegrityError):
        dos.con.execute("INSERT INTO sesiones_de_proyecto "
                        "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                        "VALUES (1, '21', 'x021', 'quitada', ?)", (DUENO,))
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    with pytest.raises(sqlite3.IntegrityError):
        dos.con.execute("INSERT INTO sesiones_de_proyecto "
                        "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                        "VALUES (1, '11', 's011', 'agregada', ?)", (DUENO,))


def test_quitar_una_agregada_la_saca_de_su_fila(dos):
    """«Quitar» sobre una sesión que la casa agregó a mano no guarda una quitada: saca esa fila."""
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    r = quitar(1, 21)
    assert "hecho=sesion_sacada" in _donde(r), _donde(r)
    assert _vivas(dos, 1) == []
    assert [f["modo"] for f in _filas(dos, 1)] == ["agregada"]        # la fila queda, con `borrado_en`
    assert "x021" not in _listas(_bloque(_ver(p=1)))


# ═══════════════════════════════════════════════════════════════════════
# Garantía: una agregada que ya no devuelve o está cancelada se avisa con su código y se saca
# ═══════════════════════════════════════════════════════════════════════

def test_una_agregada_que_la_app_ya_no_devuelve_se_avisa_con_su_codigo_y_se_puede_sacar(dos, registro):
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    assert "x021" in _listas(_bloque(_ver(p=1))), "mientras el registro la devuelve, se pinta normal"

    registro.otras = []                     # el registro ya no la devuelve
    bloque = _bloque(_ver(p=1))
    assert "1 sesión agregada ya no está en el registro (código x021)" in _avisos(bloque), bloque
    assert "x021" not in _listas(bloque), "la agregada que ya no está siguió en la lista"

    registro.pedidos.clear()
    r = quitar(1, 21)                       # el botón «Sacar» del aviso
    assert "hecho=sesion_sacada" in _donde(r), _donde(r)
    assert registro.pedidos == [], "para sacar una agregada le preguntó al registro"
    assert "ya no está en el registro" not in _bloque(_ver(p=1))
    assert _vivas(dos, 1) == []


def test_una_agregada_cancelada_se_avisa_con_su_codigo(dos, registro):
    assert "hecho=sesion_agregada" in _donde(agregar(1, 22))
    assert "x022" in _listas(_bloque(_ver(p=1)))

    registro.otras = [{**OTRO, "cancelada": True}]
    bloque = _bloque(_ver(p=1))
    assert "1 sesión agregada está cancelada (código x022)" in _avisos(bloque), bloque
    assert "x022" not in _listas(bloque), "la cancelada siguió en la lista"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: ese aviso no sale si la App no contestó
# ═══════════════════════════════════════════════════════════════════════

def test_el_aviso_de_una_agregada_no_sale_si_la_app_no_contesto(dos, registro, monkeypatch):
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    registro.otras = []
    monkeypatch.setattr(config, "REGISTRO_URL", "http://127.0.0.1:1")

    bloque = _bloque(_ver(p=1))
    assert "No se pudo consultar el registro." in bloque, bloque
    assert "ya no está en el registro" not in bloque and "está cancelada" not in bloque
    assert _avisos(bloque) == "", bloque
    assert [f["sesion_ref"] for f in _vivas(dos, 1)] == ["21"], "la decisión se perdió"
    # y mientras tanto no se ofrece buscar ni agregar
    assert "buscar-sesion" not in bloque and "Agregar a este proyecto" not in bloque


# ═══════════════════════════════════════════════════════════════════════
# Garantía: las agregadas se quedan al cambiar el cliente
# ═══════════════════════════════════════════════════════════════════════

def test_las_agregadas_se_quedan_al_cambiar_el_cliente(dos):
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    ficha = _ficha(dos, 1)

    _correr(db.poner_cliente(1, None, leer_persona=_noco({})))
    bloque = _bloque(_ver(p=1))
    assert "Este proyecto no tiene cliente" in bloque, bloque
    assert "x021" in _listas(bloque), "al quitar el cliente, la agregada se perdió"

    _correr(db.poner_cliente(1, ficha, leer_persona=_noco({ficha: {"id": ficha, "nombre": "Cliente X"}})))
    assert "x021" in _listas(_bloque(_ver(p=1)))
    assert [f["sesion_ref"] for f in _vivas(dos, 1)] == ["21"]
    assert [f["borrado_en"] for f in _filas(dos, 1)] == [None], "la decisión se tocó"


# ═══════════════════════════════════════════════════════════════════════
# Lo que la App contesta de la ficha: solo se pinta lo que se pidió, y cada frase es verdad
# ═══════════════════════════════════════════════════════════════════════

def test_con_puede_ligar_falso_solo_sale_lo_que_se_pidio(dos, registro):
    """La App contestó que no puede ligar las del cliente: de lo que mandó solo valen las que Lucy
    pidió una por una (`&sesion=`), y una que venga de más no se pinta."""
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))        # la 21 queda agregada a mano
    registro.sesiones = _respuesta(sesiones=(SESION,), puede_ligar=False, motivo="sin_telefono")
    bloque = _bloque(_ver(p=1))
    assert "no puede ligar las sesiones de este cliente" in bloque, bloque
    assert "x021" in _listas(bloque), "no salió la sesión que se pidió"
    assert "s011" not in bloque, "salió una sesión del cliente que la App mandó de más"


def test_si_todas_las_del_cliente_estan_quitadas_lo_dice(dos):
    """La lista vacía porque están quitadas no se cuenta como «el registro no tiene»."""
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    bloque = _bloque(_ver(p=1))                                    # queda el trabajo 12
    assert "El registro no tiene sesiones ni trabajos de este cliente." not in bloque, bloque

    assert "hecho=sesion_quitada" in _donde(quitar(1, 12))
    bloque = _bloque(_ver(p=1))
    assert "Todas las sesiones de este cliente están quitadas." in bloque, bloque
    assert "El registro no tiene sesiones ni trabajos de este cliente." not in bloque, bloque
    assert "s011" in _quitadas(bloque) and "t012" in _quitadas(bloque), bloque


def test_sin_sesiones_del_cliente_lo_dice(dos, registro):
    """Cuando la App contestó y no devolvió ninguna del cliente, sí se dice."""
    registro.sesiones = _respuesta(sesiones=())
    bloque = _bloque(_ver(p=1))
    assert "El registro no tiene sesiones ni trabajos de este cliente." in bloque, bloque
    assert "Todas las sesiones de este cliente están quitadas." not in bloque, bloque


def test_sin_cliente_sin_agregadas_no_se_le_pregunta(dos, registro):
    registro.pedidos.clear()
    bloque = _bloque(_ver(p=3))
    assert "Este proyecto no tiene cliente, así que no se le pregunta al registro." in bloque, bloque
    assert "solo salen las sesiones agregadas a mano" not in bloque, bloque
    assert registro.pedidos == []


def test_sin_cliente_con_agregada_la_frase_es_la_verdadera(dos, registro):
    """Con agregadas y sin cliente SÍ se le pregunta (por esas sesiones): la frase lo dice."""
    registro.sesiones = _respuesta(sesiones=())
    assert "hecho=sesion_agregada" in _donde(agregar(3, 21))
    registro.pedidos.clear()
    bloque = _bloque(_ver(p=3))
    assert "Este proyecto no tiene cliente: solo salen las sesiones agregadas a mano." in bloque, bloque
    assert "Este proyecto no tiene cliente, así que no se le pregunta al registro." not in bloque, bloque
    assert "x021" in _listas(bloque), bloque
    assert any("sesion=21" in r for r in _rutas_al_registro(registro)), \
        "no se le preguntó al registro por la agregada"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: buscar, agregar y sacar son solo de la sesión de la casa
# ═══════════════════════════════════════════════════════════════════════

def test_buscar_agregar_y_sacar_son_solo_de_la_casa(dos, registro):
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    registro.busqueda = {"sesiones": [OTRA], "hay_mas": False}
    registro.pedidos.clear()
    bloque = _bloque(_ver("ver", p=1, sq="ana"))
    assert all(r.split("?")[0] != "/api/lucy/sesiones/buscar" for r in _rutas_al_registro(registro)), \
        "la sesión de solo ver buscó en el registro con un `sq` escrito a mano"
    for control in ("<form", "<button", "<input", "<select", "buscar-sesion",
                    "Agregar a este proyecto", "Sacar", "Quitar", "Devolver"):
        assert control not in bloque, f"el bloque de solo ver trae {control!r}"
    assert "x021" in bloque, "lo que se VE (la lista y los avisos) sale igual"

    antes = _filas(dos)
    for r in (agregar(1, 22, con="ver"), quitar(1, 21, con="ver")):
        assert r.status_code == 401, r.status_code
    assert _filas(dos) == antes and len(_huellas(dos)) == 1


def test_un_chat_de_la_casa_que_no_esta_permitido_no_puede_buscar_ni_agregar(dos, registro, monkeypatch):
    """La cookie de la casa de un chat que no entra al panel: 401 y nada escrito."""
    ajeno = 700100999
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO,))
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {DUENO: "Dueño"})
    assert not auth.puede_entrar(ajeno)

    antes = _filas(dos)
    registro.pedidos.clear()
    cliente = _cliente_de(ajeno)
    r = cliente.post("/proyectos/1/sesiones/agregar", data={"ref": 21}, follow_redirects=False)
    assert r.status_code == 401, r.status_code
    assert registro.pedidos == [], "la ruta le preguntó al registro antes de mirar quién entra"
    assert _filas(dos) == antes


def test_agregar_en_solo_ver_y_sin_sesion_no_escribe(dos):
    for con in ("ver", "nadie"):
        r = _cliente(con).post("/proyectos/1/sesiones/agregar", data={"ref": 21},
                               follow_redirects=False)
        assert r.status_code == 401, (con, r.status_code)
    assert _filas(dos) == [] and _huellas(dos) == []


def test_las_funciones_de_db_de_agregar_y_sacar_rechazan_a_quien_no_entra(dos, monkeypatch):
    """El mismo rechazo por debajo de la ruta (la segunda puerta de «solo la casa»): un chat que no
    entra al panel y ninguno (`None`)."""
    ajeno = 700100999
    monkeypatch.setattr(config, "CHAT_IDS_PERMITIDOS", (DUENO,))
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {DUENO: "Dueño"})
    antes = _filas(dos)
    for chat in (ajeno, None):
        with pytest.raises(db.SesionSinSesion):
            _correr(db.agregar_sesion_a_proyecto(1, 21, "x021", chat))
        with pytest.raises(db.SesionSinSesion):
            _correr(db.sacar_sesion_agregada_de_proyecto(1, 21, chat))
    assert _filas(dos) == antes and _huellas(dos) == []


# ═══════════════════════════════════════════════════════════════════════
# Garantía: funciona sin JavaScript
# ═══════════════════════════════════════════════════════════════════════

def test_el_buscador_y_agregar_funcionan_sin_javascript(dos, registro):
    """El buscador es un GET a esta misma página y agregar y sacar son POST de formulario: el
    navegador no necesita ningún guion. (Todo lo demás de este archivo se corre así: pedidos
    normales.)"""
    registro.busqueda = {"sesiones": [OTRA], "hay_mas": False}
    html = _ver(p=1, sq="ana")
    assert re.search(r'<form class="buscar-sesion" method="get" action="/proyectos"[^>]*>', html), html
    assert 'name="sq"' in html
    assert re.search(r'<form class="agregar-sesion" method="post" '
                     r'action="/proyectos/1/sesiones/agregar">', html), html
    bloque = _bloque(html)
    assert "javascript:" not in bloque.lower() and "fetch(" not in bloque


# ═══════════════════════════════════════════════════════════════════════
# El escapado: TODO lo nuevo que se pinta
# ═══════════════════════════════════════════════════════════════════════

def _claves_que_el_candidato_lee() -> list[str]:
    """Las claves de la respuesta que `_candidato` mira, sacadas de su árbol sintáctico: un campo
    nuevo que el lector deje pasar entra acá solo."""
    fuente = open(registro_lectura.__file__, encoding="utf-8").read()
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_candidato":
            return sorted({n.args[0].value for n in ast.walk(nodo)
                           if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute)
                           and n.func.attr == "get" and n.args
                           and isinstance(n.args[0], ast.Constant)
                           and isinstance(n.args[0].value, str)})
    raise AssertionError("no encontré `_candidato` en el lector")


@pytest.mark.parametrize("campo", _claves_que_el_candidato_lee())
def test_lo_que_manda_el_registro_en_la_busqueda_sale_escapado(dos, registro, campo):
    """Cada campo de una candidata, con un valor hostil: sale escapado. El campo en juego es el
    ÚNICO con algo en la fila (la plantilla esconde unos detrás de otros)."""
    fila = dict.fromkeys(_claves_que_el_candidato_lee())
    fila.update(ref=31, es_trabajo=False)
    fila[campo] = HOSTIL
    registro.busqueda = {"sesiones": [fila], "hay_mas": False}
    bloque = _bloque(_ver(p=1, sq="ana"))
    assert HOSTIL not in bloque, f"{campo!r} salió crudo: {bloque}"

    candidato = registro_lectura._candidato(fila) or {}
    if any("hostil" in v for v in candidato.values() if isinstance(v, str)):
        assert "&lt;b&gt;hostil&lt;/b&gt;" in bloque, f"{campo!r} no salió escapado: {bloque}"


@pytest.mark.parametrize("columna", ["codigo", "sesion_ref"])
def test_lo_que_el_aviso_de_una_agregada_pinta_sale_escapado(dos, registro, columna):
    """El aviso pinta el `codigo` guardado y el `ref` (en el formulario de «Sacar»): los dos, con
    un valor hostil, salen escapados."""
    assert "hecho=sesion_agregada" in _donde(agregar(1, 21))
    dos.con.execute(f"UPDATE sesiones_de_proyecto SET {columna} = ? WHERE modo = 'agregada'",
                    (HOSTIL,))
    registro.otras = []
    bloque = _bloque(_ver(p=1))
    assert "1 sesión agregada ya no está en el registro" in _avisos(bloque), bloque
    assert HOSTIL not in bloque, f"el {columna} salió crudo en el aviso"
    assert "&lt;b&gt;hostil&lt;/b&gt;" in bloque, f"el {columna} no salió escapado"
