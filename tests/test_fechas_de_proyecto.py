"""Las fechas del proyecto (`proyectos.inicio`, `entrega`, `termina_cuando`): parte 4 del diseño
de la página completa del proyecto (8-oct-2026).

Qué se vigila, por la ruta REAL (`POST /proyectos/{pid}/fechas`), la plantilla REAL,
`crud.editar` / `crud.deshacer` / `db.crear_proyecto` / `db.convertir_tarea_en_proyecto` REALES y
SQL que se ejecuta de verdad (SQLite con el `CREATE TABLE proyectos` de `db/schema.sql`, que trae el
CHECK de entrega-después-de-inicio):

  1. la página carga con la base SIN migrar (error SQLSTATE 42703 de verdad en el doble) y no dibuja
     fechas ni el enlace para cambiarlas;
  2. qué vale como día y como «Termina cuando» (entradas inventadas), y que un rechazo no escribe
     nada, ni la mitad, ni deja huella;
  3. un proyecto nuevo nace con `inicio` = hoy en Santo Domingo por TODOS los sitios que crean uno
     (sacados del código con una sonda), con el reloj movido alrededor de la medianoche de allá;
  4. «N días para la entrega»: la cuenta, «venció» sin número negativo, cerrado o sin fecha no lo
     enseña, y el día cambia a medianoche de Santo Domingo y no de UTC;
  5. la entrega nunca antes del inicio, por `editar`, por la ruta, por `deshacer` y por la base;
  6. quién ve y quién puede: la casa ve y escribe; solo ver ve (lo que tiene valor) y no escribe;
     sin sesión no se ve ni se escribe;
  7. lo que se pinta sale escapado;
  8. el texto de la migración se EJECUTA (traducido a SQLite) sobre una tabla sin las columnas;
  9. los escritores de las tres columnas, sacados del código.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. La migración corre en SQLite con tres traducciones declaradas en
    `_aplicar_migracion` (`ADD COLUMN IF NOT EXISTS` se emula mirando las columnas;
    `AT TIME ZONE 'America/Santo_Domingo'` se traduce a `-4 hours`, que es el desfase de Santo Domingo
    sin horario de verano; los `COMMENT ON` y el `ADD CONSTRAINT` no corren: el CHECK se compara como
    texto con el de `db/schema.sql`, que SÍ corre al crear la tabla). Lo que hace Postgres con
    `AT TIME ZONE 'America/Santo_Domingo'`, con el candado de la tabla y con `DATE` NO se ejerció aquí.
  · Un `DATE` de SQLite es texto `AAAA-MM-DD`: `tests/test_grupo_ia.py::_fila` lo devuelve como `date`
    (como psycopg) y `_Cur.execute` manda un `date` como su texto ISO.
  · El reloj se mueve imitando `datetime.now` (`_reloj`), no se probó con el reloj de Postgres.
  · No es un navegador: el `<input type="date">` y su calendario no se ejercen.

Correr:  python3 -m pytest tests/test_fechas_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import re
import sqlite3
from datetime import date, datetime, timezone

import pytest

import test_base_m2 as b2
import test_grupo_ia as g
from test_grupo_ia import _ROOT
from test_pagina_proyectos import (Mundo, gente, mundo, ver, ver_r, HOY, CREADO)  # noqa: F401
import acciones.crud as crud
import config
import db.db as db
from _doble_postgres import ConexionPostgresFiel, ErrorSQL
from _navegador import Navegador
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


def _fila(m, pid=1):
    f = m.con.execute("SELECT * FROM proyectos WHERE id = ?", (pid,)).fetchone()
    return dict(f) if f is not None else None


def _huellas(m):
    return [dict(f) for f in m.con.execute("SELECT * FROM log_acciones ORDER BY id")]


def _poner(m, pid=1, **valores):
    for k, v in valores.items():
        m.con.execute(f"UPDATE proyectos SET {k} = ? WHERE id = ?", (v, pid))


def mandar(pid, campos, *, chat="dueno", files=None):
    """Un POST al panel; `chat=None` sin sesión; `chat='ver'` con la sesión de solo ver."""
    c = Navegador(panel.app, base_url="https://testserver")
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c.post(f"/proyectos/{pid}/fechas", data=campos, files=files, follow_redirects=False)


def pagina_ver(m, **consulta):
    """`GET /proyectos` con la sesión de solo ver (la que viene de la App con nivel `ver`)."""
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    r = c.get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:200]
    return r.text


@pytest.fixture
def uno(mundo):
    mundo.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    return mundo


@pytest.fixture
def uno_con_fechas(uno):
    _poner(uno, inicio="2026-10-01", entrega="2026-10-30", termina_cuando="Los másters entregados")
    return uno


# ═══════════════════════════════════════════════════════════════════════
# 1. La página con la base SIN migrar
# ═══════════════════════════════════════════════════════════════════════

class _CurPg(g._Cur):
    """Un cursor de SQLite que, ante una columna que no existe, lanza lo que lanzaría psycopg contra
    Postgres: un error con `.sqlstate == '42703'` (`tests/_doble_postgres.py::ErrorSQL`). Sin esto la
    prueba de «sin migrar» no puede ejercer la tolerancia (el error de SQLite no trae SQLSTATE)."""

    async def execute(self, sql, params=()):
        try:
            return await super().execute(sql, params)
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                raise ErrorSQL("42703", str(e)) from e
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


def _ddl_sin_las_fechas() -> str:
    """El `CREATE TABLE proyectos` de `db/schema.sql` como era ANTES de la migración: sin las tres
    columnas y sin su CHECK."""
    ddl = b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "proyectos")[0]
    for col in ("inicio", "entrega", "termina_cuando"):
        ddl = re.sub(rf"\n\s*{col}\s+\w+,", "", ddl)
    ddl = re.sub(r",\s*CONSTRAINT proyectos_entrega_despues_del_inicio\s+CHECK \([^\n]*\)", "", ddl)
    for col in ("inicio", "entrega", "termina_cuando"):
        assert col not in ddl, (col, ddl)
    return ddl


def _sin_migrar(m, monkeypatch):
    m.con.execute("DROP TABLE proyectos")
    m.con.execute(_ddl_sin_las_fechas())
    monkeypatch.setattr(db, "pool", _PoolPg(m.con))


def test_la_pagina_carga_con_la_base_sin_migrar_y_no_dibuja_fechas(uno, monkeypatch):
    _sin_migrar(uno, monkeypatch)
    uno.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    for consulta in ({"p": 1}, {"p": 1, "editar": "fechas"}, {}, {"g": "CDS"}):
        html = ver(uno, **consulta)
        assert "Empezó" not in html and "Termina cuando" not in html and "Cambiar las fechas" not in html
        assert "/proyectos/1/fechas" not in html and "editar=fechas" not in html
        assert "para la entrega" not in html
    assert 'id="de-que-se-trata"' in ver(uno, p=1)          # el resto de la página sigue


def test_sin_migrar_las_columnas_dan_no_disponible_y_otro_error_no_se_traga(uno, monkeypatch):
    assert _correr(db.fechas_de_proyectos()) == {1: {"inicio": None, "entrega": None, "termina_cuando": None}}
    _sin_migrar(uno, monkeypatch)
    assert _correr(db.fechas_de_proyectos()) is None
    for estado in ("08006", "57014", "23505"):
        class _Cur:
            async def execute(self, *a):
                raise ErrorSQL(estado)

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cur()

        class _P:
            def connection(self):
                class CM:
                    async def __aenter__(s):
                        return _Conn()

                    async def __aexit__(s, *e):
                        return False
                return CM()
        monkeypatch.setattr(db, "pool", _P())
        with pytest.raises(ErrorSQL):
            _correr(db.fechas_de_proyectos())
    class _Sin(Exception):
        pass

    class _CurSin:
        async def execute(self, *a):
            raise _Sin("sin sqlstate")

    class _ConnSin:
        def cursor(self, row_factory=None):
            return _CurSin()

    class _PSin:
        def connection(self):
            class CM:
                async def __aenter__(s):
                    return _ConnSin()

                async def __aexit__(s, *e):
                    return False
            return CM()
    monkeypatch.setattr(db, "pool", _PSin())
    with pytest.raises(_Sin):
        _correr(db.fechas_de_proyectos())


def test_sin_migrar_una_escritura_a_mano_se_rechaza_diciendo_que_falta_actualizar(uno, monkeypatch):
    _sin_migrar(uno, monkeypatch)
    uno.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    r = mandar(1, {"inicio": "2026-10-01", "entrega": "2026-10-30", "termina_cuando": "x"})
    assert r.status_code == 303 and "error=fechas_sin_columnas" in r.headers["location"]
    assert "hecho=" not in r.headers["location"] and _huellas(uno) == []
    assert "falta actualizar la base de datos" in ver_r(uno, p=1, error="fechas_sin_columnas")


def test_armar_pagina_sin_fechas_marca_no_disponible_aunque_el_proyecto_exista():
    m = db.armar_pagina(list(b2.g.__dict__.get("AREAS", [])) or [], [dict(
        id=1, nombre="Uno", descripcion=None, estado="activo", area=None, creado_en=CREADO,
        responsable_chat_id=None, cliente_nombre=None)], [], [], [], {}, HOY)
    p = m["proyectos"][1]
    assert p["fechas_disponibles"] is False
    assert (p["inicio"], p["entrega"], p["termina_cuando"], p["entrega_frase"]) == (None, None, None, None)


# ═══════════════════════════════════════════════════════════════════════
# 2. Las puertas: qué vale y que un rechazo no escribe nada
# ═══════════════════════════════════════════════════════════════════════

_DIAS_QUE_VALEN = [
    ("2026-10-30", date(2026, 10, 30)), (" 2026-10-30 ", date(2026, 10, 30)), ("2026-10-30\n", date(2026, 10, 30)), (date(2026, 10, 30), date(2026, 10, 30)),
    ("2000-01-01", date(2000, 1, 1)), ("2100-12-31", date(2100, 12, 31)), ("2024-02-29", date(2024, 2, 29)),
    (None, None), ("", None), ("   ", None),
]
_DIAS_QUE_NO = [
    ("30/10/2026", "formato"), ("2026-13-01", "formato"), ("2026-02-30", "formato"), ("2025-02-29", "formato"),
    ("2026-10-30T10:00", "formato"), ("2026-10-30 10:00", "formato"), ("2026-1-5", "formato"),
    ("20261030", "formato"), ("hoy", "formato"), ("２０２６-１０-３０", "formato"), ("2026-10-3\n0", "formato"),
    ("+2026-10-30", "formato"), ("2026-10-30;", "formato"), ("'; DROP TABLE proyectos;--", "formato"),
    (datetime(2026, 10, 30), "tipo"), (datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc), "tipo"),
    (20261030, "tipo"), (True, "tipo"), (False, "tipo"), (1.5, "tipo"), ([], "tipo"), ({}, "tipo"), (b"2026-10-30", "tipo"),
    ("1999-12-31", "rango"), ("2101-01-01", "rango"), ("0001-01-01", "rango"), ("9999-12-31", "rango"),
    (date(1999, 12, 31), "rango"), (date(2101, 1, 1), "rango"),
]


@pytest.mark.parametrize("valor,esperado", _DIAS_QUE_VALEN)
def test_la_puerta_del_dia_deja_pasar_lo_que_es_un_dia(valor, esperado):
    assert db.dia_de_proyecto_que_vale(valor) == esperado
    assert crud.PUERTAS["proyectos"]["entrega"](valor) == esperado


@pytest.mark.parametrize("valor,clave", _DIAS_QUE_NO)
def test_la_puerta_del_dia_rechaza_lo_que_no_es_un_dia(valor, clave):
    for puerta in (db.dia_de_proyecto_que_vale, crud.PUERTAS["proyectos"]["entrega"],
                   crud.PUERTAS["proyectos"]["inicio"]):
        with pytest.raises(db.FechaDeProyectoNoVale) as e:
            puerta(valor)
        assert e.value.clave == clave
        assert str(valor) not in str(e.value) or str(valor) in ("", "hoy") or len(str(valor)) < 3


def test_el_inicio_no_se_deja_vacio_pero_la_entrega_si():
    for vacio in (None, "", "   "):
        with pytest.raises(db.FechaDeProyectoNoVale) as e:
            crud.PUERTAS["proyectos"]["inicio"](vacio)
        assert e.value.clave == "vacio"
        assert crud.PUERTAS["proyectos"]["entrega"](vacio) is None


_TEXTOS_QUE_VALEN = [
    ("Los cinco másters entregados", "Los cinco másters entregados"), ("  con espacios  ", "con espacios"),
    ("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("", None), ("   \n ", None), (None, None),
    ("x" * db.LARGO_TERMINA_CUANDO, "x" * db.LARGO_TERMINA_CUANDO), ("ñandú — «ok» 🙂", "ñandú — «ok» 🙂"),
]
_TEXTOS_QUE_NO = [
    ("x" * (db.LARGO_TERMINA_CUANDO + 1), "largo"), ("a\x00b", "caracteres"), ("a\ud800b", "caracteres"),
    (5, "tipo"), (True, "tipo"), (["a"], "tipo"), (b"a", "tipo"), (date(2026, 10, 30), "tipo"),
]


@pytest.mark.parametrize("valor,esperado", _TEXTOS_QUE_VALEN)
def test_la_puerta_de_termina_cuando_deja_pasar_texto(valor, esperado):
    assert db.termina_cuando_que_vale(valor) == esperado
    assert crud.PUERTAS["proyectos"]["termina_cuando"](valor) == esperado


@pytest.mark.parametrize("valor,clave", _TEXTOS_QUE_NO)
def test_la_puerta_de_termina_cuando_rechaza_lo_demas(valor, clave):
    with pytest.raises(db.TerminaCuandoNoVale) as e:
        crud.PUERTAS["proyectos"]["termina_cuando"](valor)
    assert e.value.clave == clave


def test_las_tres_columnas_tienen_puerta_y_frase_de_deshacer():
    for c in ("inicio", "entrega", "termina_cuando"):
        assert c in crud.PUERTAS["proyectos"] and ("proyectos", c) in crud._VUELVE_A


@pytest.mark.parametrize("columna", ["inicio", "entrega"])
@pytest.mark.parametrize("valor,clave", [v for v in _DIAS_QUE_NO])
def test_editar_rechaza_un_dia_malo_sin_escribir_nada(uno_con_fechas, columna, valor, clave):
    antes = _fila(uno_con_fechas)
    with pytest.raises(ValueError) as e:
        _correr(crud.editar("proyectos", 1, {columna: valor}, motivo="prueba"))
    assert isinstance(e.value.__cause__, db.FechaDeProyectoNoVale) and e.value.__cause__.clave == clave
    assert e.value.__cause__.columna_con_puerta == columna
    assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []


def test_editar_rechaza_el_conjunto_entero_si_una_columna_no_vale(uno_con_fechas):
    antes = _fila(uno_con_fechas)
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"inicio": "2026-09-01", "entrega": "mañana",
                                             "termina_cuando": "otro"}, motivo="prueba"))
    assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []


def test_editar_escribe_el_dia_como_dia_y_lo_deja_en_la_huella(uno_con_fechas):
    despues, log_id = _correr(crud.editar("proyectos", 1, {"entrega": "2026-11-15"}, motivo="prueba"))
    assert despues["entrega"] == date(2026, 11, 15) and log_id is not None
    assert despues.escritas == frozenset({"entrega"})
    assert _fila(uno_con_fechas)["entrega"] == "2026-11-15"
    h, = _huellas(uno_con_fechas)
    assert (h["accion"], h["tabla"], h["registro_id"]) == ("editar", "proyectos", 1)


def test_un_valor_igual_a_lo_guardado_no_escribe_ni_deja_huella(uno_con_fechas):
    despues, log_id = _correr(crud.editar(
        "proyectos", 1, {"inicio": "2026-10-01", "entrega": date(2026, 10, 30),
                         "termina_cuando": "  Los másters entregados "}, motivo="prueba"))
    assert log_id is None and despues["entrega"] == date(2026, 10, 30) and _huellas(uno_con_fechas) == []


def test_un_formulario_que_reenvia_las_tres_solo_escribe_la_que_cambio(uno_con_fechas):
    despues, log_id = _correr(crud.editar(
        "proyectos", 1, {"inicio": "2026-10-01", "entrega": "2026-12-01",
                         "termina_cuando": "Los másters entregados"}, motivo="prueba"))
    assert despues.escritas == frozenset({"entrega"}) and log_id is not None


def test_se_puede_quitar_la_entrega_y_el_termina_cuando_pero_no_el_inicio(uno_con_fechas):
    despues, _ = _correr(crud.editar("proyectos", 1, {"entrega": "", "termina_cuando": ""}, motivo="prueba"))
    assert despues["entrega"] is None and despues["termina_cuando"] is None
    antes = _fila(uno_con_fechas)
    with pytest.raises(ValueError) as e:
        _correr(crud.editar("proyectos", 1, {"inicio": ""}, motivo="prueba"))
    assert e.value.__cause__.clave == "vacio" and _fila(uno_con_fechas) == antes


def test_telegram_editar_pasa_por_las_mismas_puertas(uno_con_fechas):
    """`cerebro/agente.py` llama `crud.editar` con lo que el modelo mandó (texto): la misma puerta."""
    _correr(crud.editar("proyectos", 1, {"entrega": "2026-11-20", "termina_cuando": "Cuando pague"},
                        motivo="Tiziano lo pidió por Telegram"))
    f = _fila(uno_con_fechas)
    assert (f["entrega"], f["termina_cuando"]) == ("2026-11-20", "Cuando pague")
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"entrega": "20 de noviembre"}, motivo="x"))
    assert _fila(uno_con_fechas)["entrega"] == "2026-11-20"


# ── Por la ruta ─────────────────────────────────────────────────────────

def test_la_ruta_guarda_las_tres_y_dice_guardadas(uno_con_fechas):
    r = mandar(1, {"inicio": "2026-10-02", "entrega": "2026-11-01", "termina_cuando": "Todo pagado"})
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?hecho=fechas&p=1#de-que-se-trata"
    f = _fila(uno_con_fechas)
    assert (f["inicio"], f["entrega"], f["termina_cuando"]) == ("2026-10-02", "2026-11-01", "Todo pagado")
    h, = _huellas(uno_con_fechas)
    assert h["actor"] == "panel" and h["accion"] == "editar"
    assert "Fechas guardadas." in ver_r(uno_con_fechas, p=1, hecho="fechas")


def test_la_ruta_no_dice_guardadas_si_nada_cambio(uno_con_fechas):
    r = mandar(1, {"inicio": "2026-10-01", "entrega": "2026-10-30", "termina_cuando": "Los másters entregados"})
    assert "error=fechas_igual" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _huellas(uno_con_fechas) == []
    assert "Las fechas ya estaban así" in ver_r(uno_con_fechas, p=1, error="fechas_igual")


_RECHAZOS_DE_LA_RUTA = [
    ({"inicio": "ayer"}, "inicio_formato"), ({"entrega": "30/10/2026"}, "entrega_formato"),
    ({"entrega": "1999-01-01"}, "entrega_rango"), ({"inicio": "2200-01-01"}, "inicio_rango"),
    ({"inicio": ""}, "inicio_vacio"), ({"entrega": "2026-09-30"}, "fechas_orden"),
    ({"inicio": "2026-11-01"}, "fechas_orden"), ({"inicio": "2026-11-01", "entrega": "2026-10-31"}, "fechas_orden"),
    ({"termina_cuando": "x" * (db.LARGO_TERMINA_CUANDO + 1)}, "termina_largo"),
    ({"termina_cuando": "a\x00b"}, "termina_caracteres"),
    ({"entrega": "2026-12-12", "termina_cuando": "x" * (db.LARGO_TERMINA_CUANDO + 1)}, "termina_largo"),
    ({"inicio": "2026-10-05", "entrega": "mañana"}, "entrega_formato"),
    ({}, "fechas_invalida"),
]


@pytest.mark.parametrize("campos,clave", _RECHAZOS_DE_LA_RUTA)
def test_la_ruta_rechaza_con_su_clave_y_no_escribe_nada(uno_con_fechas, campos, clave):
    antes = _fila(uno_con_fechas)
    r = mandar(1, campos)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert f"error={clave}&" in destino and "editar=fechas" in destino and "hecho=" not in destino
    assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []
    for trozo in campos.values():
        assert not trozo or trozo not in destino            # lo escrito nunca viaja en la dirección


def test_la_ruta_rechaza_un_archivo_subido_en_vez_de_un_dia(uno_con_fechas):
    antes = _fila(uno_con_fechas)
    r = mandar(1, {}, files={"entrega": ("e.txt", b"2026-10-30", "text/plain")})
    assert "error=entrega_formato" in r.headers["location"]
    assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []


def test_la_ruta_de_un_proyecto_que_no_esta_no_dice_guardadas(uno):
    r = mandar(99, {"entrega": "2026-10-30"})
    assert r.headers["location"] == "/proyectos?error=proyecto"
    uno.con.execute("UPDATE proyectos SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id = 1")
    assert mandar(1, {"entrega": "2026-10-30"}).headers["location"] == "/proyectos?error=proyecto"
    assert _fila(uno)["entrega"] is None


def test_si_la_base_falla_al_guardar_la_ruta_no_dice_ni_guardadas_ni_no_se_guardo(uno_con_fechas, monkeypatch):
    class _Falla(g._Conn):
        async def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("UPDATE PROYECTOS"):
                raise RuntimeError("se cayó la base")
            return await super().execute(sql, params)

    class _P(g._Pool):
        def connection(self):
            con = self.con

            class CM:
                async def __aenter__(s):
                    return _Falla(con)

                async def __aexit__(s, *e):
                    return False
            return CM()
    monkeypatch.setattr(db, "pool", _P(uno_con_fechas.con))
    r = mandar(1, {"entrega": "2026-12-12"})
    assert "error=fechas_base&" in r.headers["location"] and "hecho=" not in r.headers["location"]
    texto = ver_r(uno_con_fechas, p=1, error="fechas_base", editar="fechas")
    assert "no se pudo confirmar" in texto and "Fechas guardadas" not in texto


# ═══════════════════════════════════════════════════════════════════════
# 3. Un proyecto nuevo nace con `inicio` = hoy en Santo Domingo
# ═══════════════════════════════════════════════════════════════════════

def _reloj(monkeypatch, instante_utc: datetime):
    """Mueve `datetime.now` dentro de `db.db` a `instante_utc`. `hoy_rd` (la REAL) lo convierte a
    Santo Domingo. Un `isinstance(x, datetime)` sigue siendo cierto para un `datetime` de verdad."""
    real = _dt.datetime

    class _Meta(type):
        def __instancecheck__(cls, x):
            return isinstance(x, real)

        def __subclasscheck__(cls, c):
            return issubclass(c, real)

    class Falso(real, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            return instante_utc.astimezone(tz) if tz else instante_utc.replace(tzinfo=None)

    monkeypatch.setattr(db, "datetime", Falso)


# 03:59:59 UTC del 8-oct = 23:59:59 del 7-oct en Santo Domingo; 04:00:00 UTC = 00:00:00 del 8-oct.
_ANTES_DE_MEDIANOCHE = datetime(2026, 10, 8, 3, 59, 59, tzinfo=timezone.utc)
_DESPUES_DE_MEDIANOCHE = datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mundo_real(monkeypatch, gente):
    """Como `mundo`, pero SIN fijar `db.hoy_rd`: la fecha de hoy la decide el reloj de cada prueba."""
    m = Mundo()
    monkeypatch.setattr(db, "pool", g._Pool(m.con))
    return m


def test_hoy_rd_cambia_a_medianoche_de_santo_domingo_y_no_de_utc(monkeypatch):
    _reloj(monkeypatch, _ANTES_DE_MEDIANOCHE)
    assert db.hoy_rd() == date(2026, 10, 7)
    _reloj(monkeypatch, _DESPUES_DE_MEDIANOCHE)
    assert db.hoy_rd() == date(2026, 10, 8)


def _el_proyecto_nuevo(m):
    f, = [dict(f) for f in m.con.execute("SELECT * FROM proyectos")]
    return f


@pytest.mark.parametrize("camino", ["db.crear_proyecto", "crud.crear_proyecto (Telegram)",
                                    "db.convertir_tarea_en_proyecto", "db.buscar_o_crear_proyecto"])
@pytest.mark.parametrize("instante,dia", [(_ANTES_DE_MEDIANOCHE, "2026-10-07"),
                                          (_DESPUES_DE_MEDIANOCHE, "2026-10-08")])
def test_todo_sitio_que_crea_un_proyecto_lo_deja_con_inicio_igual_a_hoy_de_santo_domingo(
        mundo_real, gente, monkeypatch, camino, instante, dia):
    _reloj(monkeypatch, instante)
    if camino == "db.crear_proyecto":
        _correr(db.crear_proyecto("Por la puerta", "CDS", gente.dueno))
    elif camino.startswith("crud.crear_proyecto"):
        _correr(crud.crear_proyecto("Por Telegram", "CDS", gente.dueno))
    elif camino == "db.convertir_tarea_en_proyecto":
        mundo_real.tarea(5, "Una tarea que se vuelve proyecto", area="CDS")
        _correr(db.convertir_tarea_en_proyecto(5))
    else:
        assert _correr(db.buscar_o_crear_proyecto("Nombrado al vuelo", bandeja_id=1))
    f = _el_proyecto_nuevo(mundo_real)
    assert f["inicio"] == dia and f["entrega"] is None and f["termina_cuando"] is None
    # La huella `crear` dice lo mismo que quedó guardado.
    h = [x for x in _huellas(mundo_real) if x["tabla"] == "proyectos" and x["accion"] == "crear"]
    assert len(h) == 1 and f"\"inicio\": \"{dia}\"" in h[0]["despues"]


def test_por_las_rutas_del_panel_el_proyecto_nace_con_inicio_igual_a_hoy(mundo_real, gente, monkeypatch):
    _reloj(monkeypatch, _ANTES_DE_MEDIANOCHE)
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    nombres = {n: ch for ch, n in config.nombres_con_code().items()}
    r = c.post("/proyectos/nuevo", data={"nombre": "Desde el botón", "area": "CDS",
                                         "responsable": "Persona Uno"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]
    mundo_real.tarea(5, "Convertida con el botón", area="CDS")
    r = c.post("/tareas/5/convertir-en-proyecto", follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"], r.headers["location"]
    filas = [dict(f) for f in mundo_real.con.execute("SELECT nombre, inicio FROM proyectos ORDER BY id")]
    assert [f["inicio"] for f in filas] == ["2026-10-07", "2026-10-07"] and len(filas) == 2


def test_los_sitios_que_insertan_un_proyecto_son_estos_tres_y_cada_uno_llama_a_con_su_inicio():
    """Sonda sobre el código (no un `grep`): toda función fuera de `tests/` cuyo SQL, con los f-strings
    rearmados, dice `INSERT INTO proyectos` o `INSERT INTO {…}`. El trinquete: uno nuevo se pone rojo
    hasta que alguien lo declare aquí y le pida su `inicio`."""
    insertores: dict[str, set] = {}
    for archivo in _py_del_repo():
        partes = archivo.relative_to(_ROOT).parts
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                texto = _texto_de(n)
                if texto and re.search(r"INSERT\s+INTO\s+(proyectos\b|\{)", texto, re.I):
                    llamadas = {c.func.id for c in ast.walk(fn)
                                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                    insertores.setdefault(f"{'/'.join(partes)}::{fn.name}", set()).update(llamadas)
    assert set(insertores) == {"db/db.py::crear_proyecto", "db/db.py::convertir_tarea_en_proyecto",
                               "db/db.py::_buscar_o_crear"}, set(insertores)
    for sitio, llamadas in insertores.items():
        assert "_con_su_inicio" in llamadas, f"{sitio} inserta un proyecto sin ponerle su inicio"


def _py_del_repo():
    """Los `.py` del repo que no son pruebas, por LA puerta de los barridos (`_py_en_disco`)."""
    from test_buzon_que_no_se_ve import _py_en_disco
    return [a for a in _py_en_disco(_ROOT) if "tests" not in a.relative_to(_ROOT).parts]


def _texto_de(n) -> str | None:
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    if isinstance(n, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in n.values)
    return None


def test_la_sonda_ve_un_insertor_inventado():
    arbol = ast.parse("async def inventado(c):\n    await c.execute(f'INSERT INTO {t} (a) VALUES (1)')\n")
    textos = [_texto_de(n) for n in ast.walk(arbol)]
    assert any(t and re.search(r"INSERT\s+INTO\s+(proyectos\b|\{)", t, re.I) for t in textos)


def test_con_su_inicio_sin_migrar_no_rompe_la_transaccion_y_deja_la_fila_como_vino():
    conexion = ConexionPostgresFiel([("SELECT 1", None), ("UPDATE PROYECTOS SET INICIO", "FALLA_42703")])
    cur = conexion.cursor()
    fila = {"id": 7, "nombre": "x"}

    async def caso():
        await conexion.execute("SELECT 1")                 # ya hay una transacción abierta (el INSERT)
        return await db._con_su_inicio(conexion, cur, fila)
    assert _correr(caso()) is fila
    assert "SAVEPOINT" in conexion.eventos and "ROLLBACK TO SAVEPOINT" in conexion.eventos
    assert "ROLLBACK" not in conexion.eventos and "COMMIT" not in conexion.eventos
    assert conexion.abortada is False                      # la transacción sigue sana para lo que venga


def test_con_su_inicio_con_otro_error_de_la_base_lo_deja_pasar():
    class _Conn:
        def transaction(self):
            class T:
                async def __aenter__(s):
                    return s

                async def __aexit__(s, *e):
                    return False
            return T()

    class _Cur:
        async def execute(self, *a):
            raise ErrorSQL("40001")
    with pytest.raises(ErrorSQL):
        _correr(db._con_su_inicio(_Conn(), _Cur(), {"id": 1}))


# ═══════════════════════════════════════════════════════════════════════
# 4. «N días para la entrega»
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("faltan,frase,vencida", [
    (23, "23 días para la entrega", False), (2, "2 días para la entrega", False),
    (1, "1 día para la entrega", False), (0, "La entrega es hoy", False),
    (-1, "La entrega venció", True), (-400, "La entrega venció", True)])
def test_la_frase_de_la_entrega(faltan, frase, vencida):
    assert db.frase_de_entrega(HOY + _dt.timedelta(days=faltan), HOY) == (frase, vencida)
    assert "-" not in frase.replace("La entrega", "")           # nunca un número negativo


@pytest.mark.parametrize("sin_fecha", [None, "2026-10-30", datetime(2026, 10, 30), 5, True])
def test_la_frase_de_la_entrega_sin_un_dia_de_verdad_no_dice_nada(sin_fecha):
    assert db.frase_de_entrega(sin_fecha, HOY) is None


def test_la_pagina_dice_cuantos_dias_faltan_y_en_rojo_cuando_vencio(uno_con_fechas):
    _poner(uno_con_fechas, entrega=(HOY + _dt.timedelta(days=23)).isoformat())
    html = ver(uno_con_fechas, p=1)
    assert "<p class=\"entrega-en\">23 días para la entrega</p>" in html
    _poner(uno_con_fechas, entrega=HOY.isoformat())
    assert "<p class=\"entrega-en\">La entrega es hoy</p>" in ver(uno_con_fechas, p=1)
    _poner(uno_con_fechas, entrega=(HOY - _dt.timedelta(days=3)).isoformat(), inicio="2026-01-01")
    html = ver(uno_con_fechas, p=1)
    assert '<p class="entrega-en roja">La entrega venció</p>' in html
    assert not re.search(r"-\d+ d[ií]as?", html)


def test_cerrado_o_sin_fecha_no_enseña_la_cuenta(uno_con_fechas):
    _poner(uno_con_fechas, entrega=(HOY + _dt.timedelta(days=5)).isoformat())
    assert "para la entrega" in ver(uno_con_fechas, p=1)
    _poner(uno_con_fechas, estado="cerrado")
    html = ver(uno_con_fechas, p=1)
    assert "para la entrega" not in html and "La entrega venció" not in html
    assert "Entrega</dt>" in html                           # la fecha escrita sí se ve; la cuenta no
    _poner(uno_con_fechas, estado="cerrado", entrega=(HOY - _dt.timedelta(days=9)).isoformat(), inicio="2026-01-01")
    assert "La entrega venció" not in ver(uno_con_fechas, p=1)
    _poner(uno_con_fechas, estado="activo", entrega=None)
    html = ver(uno_con_fechas, p=1)
    assert "para la entrega" not in html and "entrega-en" not in html.split("</style>", 1)[1]


def test_la_cuenta_de_la_pagina_cambia_a_medianoche_de_santo_domingo(mundo_real, gente, monkeypatch):
    mundo_real.proyecto(1, "Disco", area="CDS", responsable=DUENO)
    _poner(mundo_real, inicio="2026-10-01", entrega="2026-10-08")
    _reloj(monkeypatch, _ANTES_DE_MEDIANOCHE)              # en Santo Domingo todavía es 7-oct
    assert "1 día para la entrega" in ver(mundo_real, p=1)
    _reloj(monkeypatch, _DESPUES_DE_MEDIANOCHE)            # ya es 8-oct allá
    assert "La entrega es hoy" in ver(mundo_real, p=1)
    _reloj(monkeypatch, datetime(2026, 10, 9, 4, 0, 0, tzinfo=timezone.utc))
    assert "La entrega venció" in ver(mundo_real, p=1)


# ═══════════════════════════════════════════════════════════════════════
# 5. La entrega no puede ser antes del inicio
# ═══════════════════════════════════════════════════════════════════════

def test_editar_rechaza_una_entrega_antes_del_inicio_de_las_tres_formas(uno_con_fechas):
    antes = _fila(uno_con_fechas)
    for cambios in ({"entrega": "2026-09-30"},                       # solo la entrega, contra el inicio de hoy
                    {"inicio": "2026-11-01"},                        # solo el inicio, contra la entrega de hoy
                    {"inicio": "2026-12-01", "entrega": "2026-11-01"}):  # las dos en la misma edición
        with pytest.raises(ValueError, match="la entrega no puede ser antes del inicio") as e:
            _correr(crud.editar("proyectos", 1, cambios, motivo="prueba"))
        assert e.value.__cause__.clave == "orden"
        assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []


def test_el_mismo_dia_vale_y_sin_el_otro_dia_no_hay_con_que_comparar(uno):
    _correr(crud.editar("proyectos", 1, {"inicio": "2026-10-01", "entrega": "2026-10-01"}, motivo="x"))
    uno.con.execute("UPDATE proyectos SET inicio = NULL, entrega = NULL WHERE id = 1")
    _correr(crud.editar("proyectos", 1, {"entrega": "2020-01-01"}, motivo="x"))      # inicio vacío: no hay con qué
    assert _fila(uno)["entrega"] == "2020-01-01"


def test_la_base_tambien_lo_rechaza_aunque_alguien_se_salte_la_puerta(uno_con_fechas):
    """El CHECK de `db/schema.sql` corre de verdad en SQLite."""
    with pytest.raises(sqlite3.IntegrityError):
        uno_con_fechas.con.execute("UPDATE proyectos SET entrega = '2026-01-01' WHERE id = 1")
    uno_con_fechas.con.execute("UPDATE proyectos SET entrega = '2026-10-01' WHERE id = 1")     # el mismo día vale
    uno_con_fechas.con.execute("UPDATE proyectos SET entrega = NULL WHERE id = 1")


def _huella_de_edicion(m, antes: dict, despues: dict) -> int:
    import json
    m.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) VALUES "
        "('lucy', 'editar', 'proyectos', 1, ?, ?)", (json.dumps(antes), json.dumps(despues)))
    return m.con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]


def test_deshacer_no_deja_la_entrega_antes_del_inicio(uno_con_fechas):
    """Una edición devolvió el inicio de un mes atrás, y después se movió la entrega a antes: volver
    atrás SOLO el inicio dejaría la entrega primero. Se rechaza antes de tocar la base."""
    antes = _fila(uno_con_fechas)
    log_id = _huella_de_edicion(uno_con_fechas, {"inicio": "2026-11-15"}, {"inicio": "2026-10-01"})
    with pytest.raises(ValueError, match="la entrega quedaría antes del inicio"):
        _correr(crud.deshacer(log_id))
    assert _fila(uno_con_fechas) == antes


def test_deshacer_rechaza_un_dia_que_la_puerta_no_deja(uno_con_fechas):
    log_id = _huella_de_edicion(uno_con_fechas, {"entrega": "30/10/2026"}, {"entrega": "2026-10-30"})
    with pytest.raises(ValueError, match="la fecha de entrega que tenía"):
        _correr(crud.deshacer(log_id))
    log_id = _huella_de_edicion(uno_con_fechas, {"inicio": None}, {"inicio": "2026-10-01"})
    with pytest.raises(ValueError, match="la fecha de inicio que tenía"):
        _correr(crud.deshacer(log_id))


# ═══════════════════════════════════════════════════════════════════════
# 6. Quién ve y quién puede
# ═══════════════════════════════════════════════════════════════════════

def test_la_casa_ve_las_fechas_el_enlace_y_el_formulario(uno_con_fechas):
    html = ver(uno_con_fechas, p=1)
    assert "<dt>Empezó</dt><dd>1 oct 2026</dd>" in html and "<dt>Entrega</dt><dd>30 oct 2026</dd>" in html
    assert "<dt>Termina cuando</dt><dd>Los másters entregados</dd>" in html
    assert 'href="/proyectos?p=1&amp;editar=fechas"' in html and "Cambiar las fechas" in html
    assert 'action="/proyectos/1/fechas"' not in html
    form = ver(uno_con_fechas, p=1, editar="fechas")
    assert 'action="/proyectos/1/fechas"' in form
    for trozo in ('name="inicio"', 'value="2026-10-01"', 'name="entrega"', 'value="2026-10-30"',
                  'name="termina_cuando"', 'value="Los másters entregados"', f'maxlength="{db.LARGO_TERMINA_CUANDO}"'):
        assert trozo in form, trozo
    assert "Cambiar las fechas" not in form


def test_la_casa_ve_el_bloque_aunque_no_haya_fechas_puestas(uno):
    html = ver(uno, p=1)
    assert "<dt>Empezó</dt><dd>sin fecha</dd>" in html and "<dt>Entrega</dt><dd>sin fecha</dd>" in html
    assert "<dt>Termina cuando</dt><dd>sin escribir</dd>" in html


def test_solo_ver_ve_lo_que_tiene_valor_y_ningun_control(uno_con_fechas):
    html = pagina_ver(uno_con_fechas, p=1)
    assert "<dt>Empezó</dt><dd>1 oct 2026</dd>" in html and "Los másters entregados" in html
    assert "/fechas" not in html and "Cambiar las fechas" not in html
    # la dirección escrita a mano no abre el formulario
    html = pagina_ver(uno_con_fechas, p=1, editar="fechas")
    assert 'name="entrega"' not in html and "/proyectos/1/fechas" not in html
    # un proyecto sin ninguna fecha ni descripción no dibuja el bloque en solo ver
    _poner(uno_con_fechas, inicio=None, entrega=None, termina_cuando=None)
    assert 'id="de-que-se-trata"' not in pagina_ver(uno_con_fechas, p=1)
    # solo con la entrega: sale esa fila y no las vacías
    _poner(uno_con_fechas, entrega="2026-10-30", inicio="2026-10-01")
    html = pagina_ver(uno_con_fechas, p=1)
    assert "<dt>Entrega</dt>" in html and "Termina cuando" not in html.split("</style>", 1)[1]


def test_solo_ver_y_sin_sesion_no_escriben(uno_con_fechas):
    antes = _fila(uno_con_fechas)
    for chat in ("ver", None):
        r = mandar(1, {"entrega": "2026-12-12", "termina_cuando": "hackeado"}, chat=chat)
        assert r.status_code in (401, 403), (chat, r.status_code)
        assert _fila(uno_con_fechas) == antes and _huellas(uno_con_fechas) == []


def test_sin_sesion_la_pagina_no_se_ve(uno_con_fechas):
    c = Navegador(panel.app, base_url="https://testserver")
    r = c.get("/proyectos", params={"p": 1}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
    assert "Los másters" not in r.text


# ═══════════════════════════════════════════════════════════════════════
# 7. Lo que se pinta sale escapado
# ═══════════════════════════════════════════════════════════════════════

_INVENTADOS = [
    '<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "' onfocus='alert(1)", "</dd><dd>colado",
    "{{ 7*7 }}", "{% raw %}x{% endraw %}", "<svg/onload=alert(1)>", "&lt;ya escapado&gt; & más", "a\"b'c<d>e&f",
    "javascript:alert(1)", "</textarea><script>x</script>",
]


@pytest.mark.parametrize("texto", _INVENTADOS)
def test_termina_cuando_sale_escapado_en_la_lectura_y_en_el_formulario(uno_con_fechas, texto):
    _poner(uno_con_fechas, termina_cuando=texto)
    for html in (ver(uno_con_fechas, p=1), ver(uno_con_fechas, p=1, editar="fechas"),
                 pagina_ver(uno_con_fechas, p=1)):
        cuerpo = html.split("</style>", 1)[1]
        assert "<script>alert" not in cuerpo and "<img src=x" not in cuerpo and "<svg/onload" not in cuerpo
        assert "onerror=alert(1)>" not in cuerpo.replace("&gt;", "") or "&lt;" in cuerpo
        assert "49" not in re.findall(r"<dd>([^<]*)</dd>", cuerpo)[2:3]
        assert "</dd><dd>colado" not in cuerpo and "</textarea><script>" not in cuerpo
    # y el texto, tal cual, llega entero al formulario como valor del campo (el HTML lo escapa)
    from html.parser import HTMLParser

    class L(HTMLParser):
        valor = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("name") == "termina_cuando":
                self.valor = a.get("value")
    lector = L()
    lector.feed(ver(uno_con_fechas, p=1, editar="fechas"))
    assert lector.valor == texto[: db.LARGO_TERMINA_CUANDO]


def test_el_aviso_de_un_rechazo_no_repite_lo_que_se_escribio(uno_con_fechas):
    r = mandar(1, {"entrega": "<script>alert(1)</script>"})
    assert "alert" not in r.headers["location"] and "script" not in r.headers["location"]
    assert "alert(1)" not in ver_r(uno_con_fechas, p=1, error="entrega_formato", editar="fechas")


# ═══════════════════════════════════════════════════════════════════════
# 8. La migración, EJECUTADA
# ═══════════════════════════════════════════════════════════════════════

_MIGRACION = _ROOT / "db" / "migrations" / "2026-10-08_proyectos_fechas.sql"


def _aplicar_migracion(con: sqlite3.Connection, texto: str) -> list[str]:
    """Ejecuta en SQLite las sentencias de la migración. TRADUCCIONES DECLARADAS (ver la FRONTERA del
    archivo): `ADD COLUMN IF NOT EXISTS` solo si la columna no está; `(creado_en AT TIME ZONE
    'America/Santo_Domingo')::date` → `date(creado_en, '-4 hours')`. No se ejecutan `BEGIN`, `COMMIT`,
    `COMMENT ON` ni las dos sentencias del CHECK. Devuelve las sentencias que NO se ejecutaron."""
    saltadas = []
    for s in g._sentencias(texto):
        s = " ".join(s.split())
        if s.upper() in ("BEGIN", "COMMIT") or s.upper().startswith("COMMENT ON"):
            saltadas.append(s)
        elif re.match(r"ALTER TABLE proyectos (DROP|ADD) CONSTRAINT", s, re.I):
            saltadas.append(s)
        elif m := re.fullmatch(r"ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS (\w+) (\w+)", s, re.I):
            if m.group(1) not in [c[1] for c in con.execute("PRAGMA table_info(proyectos)")]:
                con.execute(f"ALTER TABLE proyectos ADD COLUMN {m.group(1)} {m.group(2)}")
        elif s.startswith("UPDATE proyectos SET inicio = (creado_en AT TIME ZONE 'America/Santo_Domingo')::date"):
            con.execute(s.replace("(creado_en AT TIME ZONE 'America/Santo_Domingo')::date",
                                  "date(creado_en, '-4 hours')"))
        else:
            raise AssertionError(f"sentencia de la migración que la prueba no sabe ejecutar: {s[:100]}")
    return saltadas


def _tabla_vieja() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER)")
    con.execute(_ddl_sin_las_fechas())
    for pid, creado, borrado in ((1, "2026-10-08T02:00:00+00:00", None),        # 7-oct, 22:00 en Santo Domingo
                                 (2, "2026-10-08T04:00:00+00:00", None),        # 8-oct, 00:00 allá
                                 (3, "2026-03-01T12:00:00+00:00", "2026-04-01T00:00:00+00:00"),
                                 (4, "2026-12-31T23:30:00+00:00", None)):       # 31-dic 19:30 allá
        con.execute("INSERT INTO proyectos (id, nombre, creado_en, borrado_en) VALUES (?,?,?,?)",
                    (pid, f"p{pid}", creado, borrado))
    return con


def test_la_migracion_se_ejecuta_y_pone_el_dia_de_creacion_de_santo_domingo():
    con = _tabla_vieja()
    saltadas = _aplicar_migracion(con, _MIGRACION.read_text(encoding="utf-8"))
    assert [c[1] for c in con.execute("PRAGMA table_info(proyectos)")][-3:] == ["inicio", "entrega", "termina_cuando"]
    filas = {f["id"]: dict(f) for f in con.execute("SELECT * FROM proyectos")}
    assert {i: f["inicio"] for i, f in filas.items()} == {
        1: "2026-10-07", 2: "2026-10-08", 3: "2026-03-01", 4: "2026-12-31"}
    assert all(f["entrega"] is None and f["termina_cuando"] is None for f in filas.values())
    assert sum(s.upper().startswith("COMMENT ON") for s in saltadas) == 3
    assert sum("CONSTRAINT" in s for s in saltadas) == 2


def test_la_migracion_corrida_dos_veces_deja_lo_mismo_y_no_pisa_lo_corregido():
    con = _tabla_vieja()
    texto = _MIGRACION.read_text(encoding="utf-8")
    _aplicar_migracion(con, texto)
    con.execute("UPDATE proyectos SET inicio = '2026-05-05', entrega = '2026-06-06' WHERE id = 1")
    primera = [dict(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    _aplicar_migracion(con, texto)
    assert [dict(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")] == primera


def test_la_migracion_y_el_esquema_declaran_el_mismo_check_y_las_mismas_columnas():
    mig = " ".join(g._sentencias(_MIGRACION.read_text(encoding="utf-8")) and
                   [s for s in g._sentencias(_MIGRACION.read_text(encoding="utf-8")) if "ADD CONSTRAINT" in s])
    esquema = " ".join(b2._SCHEMA.read_text(encoding="utf-8").split())
    check = re.search(r"CHECK \(entrega IS NULL OR inicio IS NULL OR entrega >= inicio\)", " ".join(mig.split()))
    assert check and check.group(0) in esquema
    assert "CONSTRAINT proyectos_entrega_despues_del_inicio" in esquema
    declaradas = db.columnas_declaradas()["proyectos"]
    assert {"inicio", "entrega", "termina_cuando"} <= set(declaradas)


def test_el_esquema_de_la_migracion_es_el_de_la_tabla_nueva():
    """Después de migrar, la tabla vieja tiene las mismas columnas (nombre y tipo) que la del esquema."""
    con = _tabla_vieja()
    _aplicar_migracion(con, _MIGRACION.read_text(encoding="utf-8"))
    nueva = b2._base()
    tipos = lambda c: {f[1]: f[2].upper() for f in c.execute("PRAGMA table_info(proyectos)")}   # noqa: E731
    assert tipos(con) == tipos(nueva)


def test_la_migracion_dice_como_se_deshace_y_no_se_aplica_sola():
    texto = _MIGRACION.read_text(encoding="utf-8")
    assert "NO SE APLICA ACÁ" in texto and "CÓMO SE DESHACE" in texto and "db/backup.py" in texto
    for col in ("inicio", "entrega", "termina_cuando"):
        assert f"DROP COLUMN IF EXISTS {col}" in texto


# ═══════════════════════════════════════════════════════════════════════
# 9. Quién escribe las tres columnas, y lo que Lucy puede leer por Telegram
# ═══════════════════════════════════════════════════════════════════════

# Medido con la sonda de abajo el 8-oct-2026. Tres escriben una columna FIJA que no es de estas tres
# (`borrar`: `borrado_en`; `despertador.revisar`: `avisos_enviados`; `rellenar_duenos`: `bandeja_id`);
# `editar` y `deshacer` arman las columnas de la edición y las validan por `crud.PUERTAS` (ejercido
# arriba: un día malo se rechaza sin escribir, por `editar` y por `deshacer`).
GENERICOS_MEDIDOS = {"acciones/crud.py::borrar", "acciones/crud.py::deshacer", "acciones/crud.py::editar",
                     "cerebro/despertador.py::revisar", "tools/rellenar_duenos.py::main"}


def test_el_unico_sql_a_mano_que_escribe_las_fechas_es_con_su_inicio():
    escritores: dict[str, set] = {"inicio": set(), "entrega": set(), "termina_cuando": set()}
    genericos = set()
    for archivo in _py_del_repo():
        partes = archivo.relative_to(_ROOT).parts
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                texto = _texto_de(n)
                if not texto:
                    continue
                for m in re.finditer(r"UPDATE\s+(proyectos\b|\{\})\s*(?:\w+\s+)?SET\s+(.*?)(?:WHERE|$)", texto, re.I | re.S):
                    if m.group(1) == "{}":
                        genericos.add(f"{'/'.join(partes)}::{fn.name}")
                    for c in escritores:
                        if re.search(rf"\b{c}\s*=", m.group(2)):
                            escritores[c].add(fn.name)
    assert escritores == {"inicio": {"_con_su_inicio"}, "entrega": set(), "termina_cuando": set()}
    # Los que arman `UPDATE {tabla}` al vuelo: o validan por las puertas (`editar`, ejercido arriba) o
    # escriben una columna fija que no es de estas tres. La lista sale de recorrer el código.
    assert genericos == GENERICOS_MEDIDOS, genericos


def test_lucy_por_telegram_ve_las_tres_columnas_y_sabe_que_son_dias_sin_hora():
    from cerebro import consultar
    bloque = consultar.BLOQUES["proyectos"]
    for c in ("inicio", "entrega", "termina_cuando"):
        assert c in bloque
    assert "inicio (date, un día de Santo Domingo sin hora" in " ".join(bloque.split())
    assert ("proyectos", "entrega") in consultar.NOTAS_DE_COLUMNA
    assert "proyectos" in consultar.TABLAS_DE_TIZIANO
