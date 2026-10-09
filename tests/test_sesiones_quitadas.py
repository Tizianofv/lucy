"""Quitar una sesión de la lista de un proyecto, y devolverla (parte 11 de la página
de un proyecto, 9-oct-2026): las garantías de la fila 11 del diseño aprobado.

QUÉ SE VIGILA, una prueba por garantía:
  · quitar no le manda ninguna escritura al registro (el lector solo sabe GET);
  · la quitada sale de la lista Y de los totales;
  · se queda quitada tras cambiar el cliente y volverlo a poner;
  · «Quitadas» las lista y las devuelve;
  · solo la sesión de la casa (en solo ver no hay controles, y las rutas dan 401);
  · deja huella en `log_acciones`;
  · dos proyectos del mismo cliente: quitar en uno no la quita en el otro;
  · el identificador guardado es el que devolvió la App, no el del formulario.
Más lo que el encargo pide del borde: la página carga con la base SIN migrar y sin
controles, y sin la respuesta del registro no se ofrece quitar ni se guarda nada.

CÓMO. La base es la de SQLite con el esquema real (`tests/test_pagina_proyectos.py::mundo`),
así que el SQL de `sesiones_de_proyecto` se ejecuta de verdad; la App se dobla EN LA RED
(`tests/_app_de_registro.py`) y la página se pide por la RUTA real y la plantilla real.

FRONTERA, dicha para que no se dé por cubierta: el doble afirma la forma del contrato que
la sala le pasó a Lucy (la respuesta de `/api/lucy/sesiones`), NO con qué regla la App
reconoce las sesiones de una ficha ni de dónde saca cada cifra, ni que la App de verdad
conteste así. Nada de esto se corrió contra la App real ni contra Postgres.

Correr:  python3 -m pytest tests/test_sesiones_quitadas.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import test_grupo_ia as g  # noqa: E402
from _app_de_registro import AppDeRegistro  # noqa: E402
from _doble_postgres import ErrorSQL  # noqa: E402
from _navegador import Navegador  # noqa: E402
from test_pagina_proyectos import gente, mundo, ver, ver_r  # noqa: E402,F401
import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

# Nada de esto es real: ni la llave, ni las sesiones, ni el cliente.
LLAVE = "llave-de-prueba"
HOY = "2026-10-09"
DUENO = config.CHAT_ID_DUENO

SESION = {"ref": 11, "codigo": "s011", "es_trabajo": False, "fecha": HOY, "sala": "Sala P",
          "sala_mostrar": "Sala P", "horas": 3, "servicio": "Grabación", "atendio": "Ivan",
          "estado": "CONFIRMADA", "cancelada": False,
          "total": 9000.0, "abonado": 9000.0, "saldo": 0.0}
TRABAJO = {"ref": 12, "codigo": "t012", "es_trabajo": True, "fecha": HOY, "sala": "Sala P",
           "sala_mostrar": "Sala P", "horas": 2, "servicio": "Mezcla", "asignado_a": "Rosi",
           "estado": "EN PROCESO", "cancelada": False,
           "total": 6000.0, "abonado": 0.0, "saldo": 6000.0}


def _respuesta(sesiones=(SESION, TRABAJO), *, puede_ligar=True, motivo=""):
    return {"puede_ligar": puede_ligar, "motivo_sin_ligar": motivo, "sesiones": list(sesiones)}


@pytest.fixture
def registro(monkeypatch):
    """La App de mentira, con su llave puesta, y Lucy apuntada a ella."""
    doble = AppDeRegistro()
    doble.llave = LLAVE
    doble.sesiones = _respuesta()
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
    # La misma base que las otras pruebas de la página (`http://testserver`): el recibo de los
    # avisos viaja por la cookie, y una puesta en una respuesta `https` sale `Secure`.
    c = Navegador(panel.app)
    if con == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif con == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def _ver(con="casa", **consulta) -> str:
    r = _cliente(con).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:400]
    return r.text


def quitar(pid, ref, *, con="casa"):
    return _cliente(con).post(f"/proyectos/{pid}/sesiones/quitar", data={"ref": ref},
                              follow_redirects=False)


def devolver(pid, ref, *, con="casa"):
    return _cliente(con).post(f"/proyectos/{pid}/sesiones/devolver", data={"ref": ref},
                              follow_redirects=False)


def _donde(r) -> str:
    return r.headers["location"]


def _bloque(html: str) -> str:
    if 'id="sesiones-y-trabajos"' not in html:
        return ""
    dentro = html.split('id="sesiones-y-trabajos"', 1)[1].split("</section>", 1)[0]
    return " ".join(dentro.split())


def _pintadas(bloque: str) -> str:
    """Lo que hay ANTES de la lista «Quitadas»: los renglones que cuentan."""
    return bloque.split("<h3>Quitadas</h3>")[0]


def _quitadas(bloque: str) -> str:
    """El trozo de la lista «Quitadas», o ''."""
    return bloque.split("<h3>Quitadas</h3>", 1)[1] if "<h3>Quitadas</h3>" in bloque else ""


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
    return asyncio.run(espera)


def _noco(fichas):
    async def leer(noco_id):
        return fichas[noco_id]
    return leer


# ═══════════════════════════════════════════════════════════════════════
# La base SIN migrar: la página carga como hoy y no hay ningún control
# ═══════════════════════════════════════════════════════════════════════

class _CurPg(g._Cur):
    """Un cursor de SQLite que, ante una TABLA que no existe, lanza lo que lanzaría psycopg contra
    Postgres: un error con `.sqlstate == '42P01'` (undefined_table)."""

    async def execute(self, sql, params=()):
        try:
            return await super().execute(sql, params)
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise ErrorSQL("42P01", str(e)) from e
            raise


class _ConnPg(g._Conn):
    def cursor(self, row_factory=None):
        return _CurPg(self.con, como_dict=row_factory is not None)

    async def execute(self, sql, params=()):
        return await _CurPg(self.con, como_dict=False).execute(sql, params)


class _PoolPg(g._Pool):
    def connection(self):
        con = self.con

        class _CM:
            async def __aenter__(self_):
                return _ConnPg(con)

            async def __aexit__(self_, *e):
                return False
        return _CM()


def _sin_la_tabla(mundo, monkeypatch):
    """La base como está ANTES de aplicar la migración (con el 42P01 de verdad en el doble)."""
    mundo.con.execute("DROP TABLE sesiones_de_proyecto")
    monkeypatch.setattr(db, "pool", _PoolPg(mundo.con))


def test_la_pagina_carga_con_la_base_sin_migrar_y_sale_como_hoy(dos, monkeypatch):
    """Sin la tabla, el bloque se ve como antes de esta parte y sin ningún control."""
    _sin_la_tabla(dos, monkeypatch)
    bloque = _bloque(ver(dos, p=1))
    assert "s011" in bloque and "t012" in bloque and "RD$ 9,000.00" in bloque
    for rastro in ("<form", "<button", "<input", "Quitar", "Devolver", "Quitadas"):
        assert rastro not in bloque, f"con la base sin migrar salió {rastro!r}"
    assert 'id="de-que-se-trata"' in ver(dos, p=1)          # el resto de la página sigue


def test_sin_migrar_las_dos_rutas_dicen_que_falta_la_tabla_y_no_escriben(dos, monkeypatch):
    _sin_la_tabla(dos, monkeypatch)
    for r in (quitar(1, 11), devolver(1, 11)):
        assert r.status_code == 303 and "error=sesion_sin_tabla" in _donde(r), _donde(r)


def test_sin_la_respuesta_del_registro_no_hay_controles_ni_se_quita_nada(dos, registro, monkeypatch):
    """La App caída: ni botones ni una decisión guardada a ciegas."""
    monkeypatch.setattr(config, "REGISTRO_URL", "http://127.0.0.1:1")
    bloque = _bloque(_ver(p=1))
    assert "No se pudo consultar el registro." in bloque, bloque
    assert "<form" not in bloque and "Quitar" not in bloque
    r = quitar(1, 11)
    assert "error=sesion_sin_registro" in _donde(r), _donde(r)
    assert _filas(dos) == [] and _huellas(dos) == []


# ═══════════════════════════════════════════════════════════════════════
# Garantía: quitar no le manda ninguna escritura al registro
# ═══════════════════════════════════════════════════════════════════════

def test_quitar_no_le_manda_ninguna_escritura_al_registro(dos, registro):
    """Todo lo que sale hacia la App en el camino de quitar es un GET a su puerta de sesiones.

    La otra mitad la vigila `tests/test_sesiones_de_proyecto.py`
    (`test_la_unica_funcion_que_toca_la_red_es_get_y_solo_hace_get`): de todo el módulo del
    lector, la única función que abre un cliente es `_get`, y el único verbo que usa es `get`.
    Acá se mide el camino COMPLETO de quitar, que además vuelve a preguntarle al registro.
    """
    registro.pedidos.clear()
    r = quitar(1, 11)
    assert r.status_code == 303 and "hecho=sesion_quitada" in _donde(r), _donde(r)
    assert registro.pedidos, "no le preguntó nada al registro: la prueba no ejercitó el camino"
    assert {p["metodo"] for p in registro.pedidos} == {"GET"}, registro.pedidos
    assert all(p["ruta"].split("?")[0] == "/api/lucy/sesiones" for p in registro.pedidos)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: la quitada sale de la lista Y de los totales
# ═══════════════════════════════════════════════════════════════════════

def test_la_quitada_sale_de_la_lista_y_de_los_totales(dos):
    antes = _pintadas(_bloque(ver(dos, p=1)))
    assert "s011" in antes and "RD$ 9,000.00" in antes
    assert "<b>RD$ 15,000.00</b>" in antes, antes       # facturado: 9 000 + 6 000

    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    # Cómo se pinta el aviso, con el recibo puesto (como en las otras partes: la puerta de los
    # avisos no deja que una dirección escrita a mano diga que pasó algo).
    assert "Sesión quitada de la lista." in ver_r(dos, hecho="sesion_quitada", p=1)

    bloque = _bloque(ver(dos, p=1))
    pintadas = _pintadas(bloque)
    assert "s011" not in pintadas, pintadas
    assert "Grabación" not in pintadas, pintadas
    assert "RD$ 9,000.00" not in pintadas, "el dinero de la quitada siguió en la lista"
    assert "<b>RD$ 15,000.00</b>" not in pintadas
    assert "<b>RD$ 6,000.00</b>" in pintadas, pintadas   # solo queda el trabajo
    assert "t012" in pintadas
    assert "s011" in _quitadas(bloque), bloque           # sale en «Quitadas», que no suma


# ═══════════════════════════════════════════════════════════════════════
# Garantía: se queda quitada tras cambiar el cliente y volverlo a poner
# ═══════════════════════════════════════════════════════════════════════

def test_se_queda_quitada_al_cambiar_el_cliente_y_volverlo_a_poner(dos):
    ficha = _ficha(dos, 1)
    quitar(1, 11)
    assert "s011" not in _pintadas(_bloque(_ver(p=1)))

    _correr(db.poner_cliente(1, None, leer_persona=_noco({})))
    assert "Este proyecto no tiene cliente" in _bloque(_ver(p=1))

    _correr(db.poner_cliente(1, ficha, leer_persona=_noco({ficha: {"id": ficha, "nombre": "Cliente X"}})))
    bloque = _bloque(_ver(p=1))
    assert "s011" not in _pintadas(bloque), "al volver el cliente, la quitada se devolvió sola"
    assert "s011" in _quitadas(bloque)
    assert [f["borrado_en"] for f in _filas(dos, 1)] == [None], "la decisión se tocó"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: «Quitadas» las lista y las devuelve
# ═══════════════════════════════════════════════════════════════════════

def test_quitadas_las_lista_y_las_devuelve(dos):
    quitar(1, 11)
    lista = _quitadas(_bloque(_ver(p=1)))
    assert "s011" in lista and "Grabación" in lista, lista
    assert "/proyectos/1/sesiones/devolver" in lista and 'name="ref" value="11"' in lista, lista

    r = devolver(1, 11)
    assert r.status_code == 303 and "hecho=sesion_devuelta" in _donde(r), _donde(r)
    assert "Sesión devuelta a la lista." in ver_r(dos, hecho="sesion_devuelta", p=1)
    bloque = _bloque(_ver(p=1))
    assert "s011" in _pintadas(bloque), bloque
    assert "Quitadas" not in bloque, bloque
    assert "<b>RD$ 15,000.00</b>" in _pintadas(bloque)
    assert [f["borrado_en"] is not None for f in _filas(dos, 1)] == [True]


def test_una_quitada_que_la_app_ya_no_devuelve_sale_por_su_codigo_guardado(dos, registro):
    """El nombre de una quitada sale de lo que la App devuelve; si ya no la devuelve, del código
    que se guardó al quitarla (el diseño 3.4, y el encargo de esta parte)."""
    quitar(1, 11)
    registro.sesiones = _respuesta(sesiones=(TRABAJO,))          # la App ya no la trae
    lista = _quitadas(_bloque(_ver(p=1)))
    assert "Ya no está en el registro" in lista, lista
    assert "s011" in lista, lista
    assert "Grabación" not in lista, "la nombró con datos que la App ya no devuelve"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: solo la sesión de la casa
# ═══════════════════════════════════════════════════════════════════════

def test_quitar_y_devolver_son_solo_de_la_casa(dos):
    """En solo ver el bloque sale sin ningún control, y las dos rutas dan 401 sin escribir nada."""
    quitar(1, 11)
    bloque = _bloque(_ver("ver", p=1))
    for control in ("<form", "<button", "<input", "<select", "Quitar", "Devolver"):
        assert control not in bloque, f"el bloque de solo ver trae {control!r}"
    assert "s011" in _quitadas(bloque), "lo que se VE (la lista) sale igual"

    antes = _filas(dos)
    for r in (quitar(1, 12, con="ver"), devolver(1, 11, con="ver")):
        assert r.status_code == 401
    assert _filas(dos) == antes and len(_huellas(dos)) == 1


# ═══════════════════════════════════════════════════════════════════════
# Garantía: deja huella
# ═══════════════════════════════════════════════════════════════════════

def test_quitar_y_devolver_dejan_su_huella(dos):
    quitar(1, 11)
    fila = _filas(dos, 1)[0]
    huellas = _huellas(dos)
    assert len(huellas) == 1, huellas
    assert (huellas[0]["actor"], huellas[0]["accion"], huellas[0]["tabla"]) == \
        ("panel", "crear", "sesiones_de_proyecto")
    assert huellas[0]["registro_id"] == fila["id"]
    assert json.loads(huellas[0]["despues"])["sesion_ref"] == "11"

    devolver(1, 11)
    huellas = _huellas(dos)
    assert len(huellas) == 2, huellas
    assert (huellas[1]["actor"], huellas[1]["accion"]) == ("panel", "borrar")
    assert huellas[1]["registro_id"] == fila["id"]
    assert json.loads(huellas[1]["antes"])["sesion_ref"] == "11"


# ═══════════════════════════════════════════════════════════════════════
# Garantía: dos proyectos del mismo cliente
# ═══════════════════════════════════════════════════════════════════════

def test_quitar_en_un_proyecto_no_la_quita_en_el_otro(dos):
    assert _ficha(dos, 1) == _ficha(dos, 2) is not None, "los dos tienen que ser del mismo cliente"
    quitar(2, 11)
    assert "s011" not in _pintadas(_bloque(_ver(p=2))), "no se quitó del proyecto donde se pidió"
    assert "s011" in _pintadas(_bloque(_ver(p=1))), "se quitó en el proyecto de al lado"
    assert "Quitadas" not in _bloque(_ver(p=1))
    assert [f["proyecto_id"] for f in _vivas(dos)] == [2]
    assert _filas(dos, 1) == []


# ═══════════════════════════════════════════════════════════════════════
# Garantía: el identificador guardado es el que devolvió la App
# ═══════════════════════════════════════════════════════════════════════

def test_una_sesion_que_la_app_no_devuelve_no_se_quita(dos):
    """El `ref` del formulario solo sirve para BUSCAR entre lo que la App devolvió."""
    r = quitar(1, "9999-inventado")
    assert "error=sesion_no_esta" in _donde(r), _donde(r)
    assert _filas(dos) == [] and _huellas(dos) == []

    r = quitar(1, "")
    assert "error=sesion_no_esta" in _donde(r), _donde(r)
    assert _filas(dos) == []


def test_lo_que_se_guarda_es_el_identificador_de_la_app_no_el_del_formulario(dos):
    # El mismo identificador, escrito en el formulario de otra forma: lo que queda es el de la App.
    r = quitar(1, "  11  ")
    assert "hecho=sesion_quitada" in _donde(r), _donde(r)
    vivas = _vivas(dos, 1)
    assert [f["sesion_ref"] for f in vivas] == ["11"], vivas
    assert vivas[0]["codigo"] == "s011", "el código guardado no es el que devolvió la App"
    assert vivas[0]["creado_por_chat_id"] == DUENO


def test_quitar_dos_veces_lo_mismo_no_escribe_ni_deja_huella_la_segunda(dos):
    """Una sola decisión viva por proyecto y sesión (el diseño 3.3)."""
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    r = quitar(1, 11)
    assert r.status_code == 303 and "error=sesion_igual" in _donde(r), _donde(r)
    assert len(_filas(dos, 1)) == 1 and len(_huellas(dos)) == 1


def test_devolver_lo_que_no_estaba_quitado_no_cambia_nada(dos):
    r = devolver(1, 11)
    assert "error=sesion_no_esta" in _donde(r), _donde(r)
    assert _filas(dos) == [] and _huellas(dos) == []


def test_el_indice_deja_una_sola_decision_viva_y_la_borrada_no_estorba(dos):
    """El índice único parcial de la migración, EJECUTADO: la misma sesión dos veces vivas no
    entra, y una borrada deja volver a decidir sobre ella."""
    quitar(1, 11)
    with pytest.raises(sqlite3.IntegrityError):
        dos.con.execute("INSERT INTO sesiones_de_proyecto "
                        "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                        "VALUES (1, '11', 's011', 'quitada', ?)", (DUENO,))
    devolver(1, 11)
    assert "hecho=sesion_quitada" in _donde(quitar(1, 11))
    assert len(_filas(dos, 1)) == 2 and len(_vivas(dos, 1)) == 1


def test_la_migracion_corre_y_deja_la_tabla_con_su_modo_y_su_indice():
    """La migración EJECUTADA, traducida a SQLite como en las otras partes: crea la tabla para
    Postgres (el `BIGSERIAL` y el `DEFAULT now()` son los dos cambios de dialecto de siempre), con
    el vocabulario cerrado del `modo` y el índice único parcial entre las vivas."""
    import test_base_m2 as b2

    suyas = [a for a in g._migraciones()
             if "CREATE TABLE IF NOT EXISTS sesiones_de_proyecto" in a.read_text(encoding="utf-8")]
    assert len(suyas) == 1, suyas
    con = sqlite3.connect(":memory:")
    # `DEFAULT (now())` es de Postgres: SQLite necesita que alguien le conteste esa función (igual
    # que en `tests/test_base_m2.py::_base`).
    con.create_function("now", 0, lambda: "2026-10-09T12:00:00+00:00")
    con.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO proyectos (id) VALUES (1)")
    for sentencia in b2._ddl(suyas[0].read_text(encoding="utf-8"), "sesiones_de_proyecto"):
        con.execute(sentencia)
    assert con.execute("SELECT count(*) FROM sesiones_de_proyecto").fetchone()[0] == 0

    con.execute("INSERT INTO sesiones_de_proyecto "
                "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                "VALUES (1, '11', 's011', 'quitada', 42)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO sesiones_de_proyecto "
                    "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                    "VALUES (1, '12', 'x', 'otra-cosa', 42)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO sesiones_de_proyecto "
                    "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                    "VALUES (1, '11', 's011', 'quitada', 42)")
    con.execute("UPDATE sesiones_de_proyecto SET borrado_en = '2026-10-09T00:00:00+00:00'")
    con.execute("INSERT INTO sesiones_de_proyecto "
                "(proyecto_id, sesion_ref, codigo, modo, creado_por_chat_id) "
                "VALUES (1, '11', 's011', 'quitada', 42)")
    assert con.execute("SELECT count(*) FROM sesiones_de_proyecto").fetchone()[0] == 2
