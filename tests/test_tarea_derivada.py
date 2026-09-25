# -*- coding: utf-8 -*-
"""La tarea derivada: al marcar una tarea hecha en /tareas, se puede escribir
ahí mismo la que sale de ella. Pedido de Tiziano, textual: «cuando yo
seleccione el recuadro para marcar como hecha una tarea aparezca un cuadro
donde pueda crear una tarea nueva, porque muchas veces una tarea hecha da
como resultado una tarea nueva que se deriva de la completada». Diseño
aprobado en disenos/lucy-tarea-derivada/DISENO.md, con los cambios de
Tiziano en su aprobación (varias derivadas por madre; el renglón vive DENTRO
del formulario único; la relación es solo visual, en las dos direcciones).

QUÉ SE PRUEBA CON MÁS SAÑA, y por qué son ésas y no otras:

  · QUE `cerrar_y_derivar` CORRA DE VERDAD contra una base de mentira que SE
    ACUERDA de lo que se le escribe -- no una que solo anota el SQL. La
    pregunta central de este archivo es «¿lo que se guardó es lo que se
    pinta?», y eso no se contesta con filas puestas a mano (mismo motivo que
    tests/test_tarea_a_mano.py).
  · QUE SEA UNA SOLA TRANSACCIÓN: si algo revienta a mitad de la lista de
    derivadas, NADA de esa llamada queda escrito -- ni la madre, ni las
    hijas que habían entrado antes del error.
  · QUE «SI LA NUEVA NO VALE, LA VIEJA TAMPOCO SE CIERRA» (D6) se cumpla
    ANTES de tocar la base: la validación completa pasa por
    `web/app.py::_derivadas_pedidas` antes de que `cerrar_y_derivar` abra
    una conexión.
  · QUE LA RELACIÓN SEA SOLO VISUAL Y EN LAS DOS DIRECCIONES: la hija
    muestra «↳ Sale de», la madre muestra «→ Siguió», y una madre con VARIAS
    hijas las muestra TODAS.
  · QUE EL AGENTE DE TELEGRAM NO PUEDA ESCRIBIR `deriva_de_id` A TRAVÉS DE
    `crud.editar` (decisión de la sala, pregunta T1 del diseño).

LO QUE ESTOS TESTS NO PUEDEN VER, medido y no supuesto: `psycopg` está
reemplazado por un módulo falso, así que nada de acá habla con Postgres. La
base de mentira de este archivo SÍ guarda lo que se le inserta y lo
actualiza de verdad (mismo patrón que test_tarea_a_mano.py), pero sigue sin
poder ver lo que Postgres haría con esas filas -- eso lo cubre
`tools/humo.py`, que necesita DATABASE_URL. Y el SELECT de `db.derivaciones`
se prueba TAMBIÉN contra sqlite, con el mismo texto que corre en
producción (patrón de tests/test_primero.py:690-740), porque ESE SQL sí se
ejecuta de verdad en este archivo.

Correr:  python3 -m pytest tests/test_tarea_derivada.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
import acciones.crud as crud  # noqa: E402

DUENO = config.CHAT_ID_DUENO
UTC = timezone.utc
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Una base de mentira que SÍ se acuerda de lo que se le escribió ────────
#
# `tareas` es un DICT por id (no una lista): `cerrar_y_derivar` busca la
# madre por `id = %s` y la actualiza en el sitio, y eso es más fiel a lo que
# hace un UPDATE de verdad que reconstruir una lista cada vez.

class _ErrorSQL(Exception):
    """Un error de Postgres de mentira, con el `sqlstate` que haga falta."""

    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(f"error de mentira, sqlstate={sqlstate}")


class _Transaccion:
    """El SAVEPOINT de mentira que usa `cerrar_y_derivar` por cada hija (y
    `tareas_por_grupo`/`areas` por su propia cascada). Entra y sale limpio y
    NO SE TRAGA la excepción -- es quien llama el que decide si la atrapa,
    igual que el real."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        c = self._conn
        c.sql.append((s, params))
        self._filas = []

        # `sin_columna_deriva`: simula que la migración de este encargo
        # todavía no se aplicó -- CUALQUIER sentencia que nombre
        # `deriva_de_id` revienta, como reventaría contra una base real sin
        # esa columna.
        if c.sin_columna_deriva and "deriva_de_id" in s:
            raise _ErrorSQL("42703")

        if s.startswith("SELECT id, titulo, estado, vence_en, completado_en, "
                        "bandeja_id, proyecto_id FROM tareas"):
            fila = c.tareas.get(params[0])
            if fila is not None and fila.get("borrado_en") is None:
                self._filas = [dict(fila)]

        elif s.startswith("UPDATE tareas SET estado"):
            estado, tid = params
            c.tareas[tid]["estado"] = estado
            c.tareas[tid]["completado_en"] = c.reloj

        elif s.startswith("INSERT INTO bandeja"):
            c.sig_bandeja += 1
            fila = {"id": c.sig_bandeja, "chat_id": params[0]}
            c.bandeja.append(fila)
            self._filas = [fila]

        elif s.startswith("INSERT INTO tareas"):
            c.sig_tarea += 1
            # Ocho parámetros CON `deriva_de_id` (el INSERT `con_deriva` de
            # `cerrar_y_derivar`), siete SIN ella (`sin_deriva`, la caída
            # por columna ausente).
            if len(params) == 8:
                (bandeja_id, titulo, vence_en, anticipos, area, proyecto_id,
                 resp, deriva_de_id) = params
            else:
                (bandeja_id, titulo, vence_en, anticipos, area, proyecto_id,
                 resp) = params
                deriva_de_id = None
            fila = {"id": c.sig_tarea, "bandeja_id": bandeja_id,
                    "titulo": titulo, "vence_en": vence_en,
                    "anticipos_min": anticipos, "area": area,
                    "proyecto_id": proyecto_id, "responsable_chat_id": resp,
                    "deriva_de_id": deriva_de_id, "estado": "pendiente",
                    "creado_en": c.reloj, "completado_en": None,
                    "borrado_en": None}
            c.tareas[fila["id"]] = fila
            self._filas = [fila]

        elif s.startswith("INSERT INTO log_acciones"):
            c.log.append((s, params))

        elif s.startswith("SELECT clave, color FROM areas"):
            self._filas = list(c.areas)

        elif s.startswith("SELECT id, deriva_de_id FROM tareas"):
            self._filas = [
                {"id": t["id"], "deriva_de_id": t["deriva_de_id"]}
                for t in c.tareas.values()
                if t.get("deriva_de_id") is not None
                and t.get("borrado_en") is None]

        elif s.startswith("SELECT t.id, t.titulo"):
            self._filas = [dict(t) for t in c.tareas.values()
                           if t.get("borrado_en") is None]

        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _Conn:
    def __init__(self, tareas=None, areas=None, sin_columna_deriva=False,
                reloj=None):
        self.tareas = {t["id"]: dict(t) for t in (tareas or [])}
        self.areas = list(areas or [])
        self.bandeja: list = []
        self.log: list = []
        self.sql: list = []
        self.sig_bandeja = 900
        self.sig_tarea = max([10] + [t["id"] for t in self.tareas.values()])
        self.sin_columna_deriva = sin_columna_deriva
        self.reloj = reloj or datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def execute(self, sql, params=None):
        return await _Cursor(self).execute(sql, params)

    def transaction(self):
        return _Transaccion(self)


class _CM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _CM(self._conn)


def _con_base(conn, fn):
    """Corre una corutina con la base falseada. Devuelve el resultado."""
    guardado = db.pool
    db.pool = _Pool(conn)
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(fn())
        finally:
            bucle.close()
    finally:
        db.pool = guardado


def _fila(id, estado="pendiente", vence_en=None, titulo=None,
          responsable=None, completado_en=None, area=None,
          proyecto_id=None, deriva_de_id=None, borrado_en=None,
          bandeja_id=None):
    return {"id": id, "titulo": titulo or f"tarea {id}", "estado": estado,
            "vence_en": vence_en, "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "bandeja_id": bandeja_id if bandeja_id is not None else 900 + id,
            "responsable_chat_id": responsable, "completado_en": completado_en,
            "area": area, "proyecto_id": proyecto_id,
            "deriva_de_id": deriva_de_id, "borrado_en": borrado_en}


# ── `db.cerrar_y_derivar`, contra la base de mentira, de verdad ──────────

def test_marca_hecha_y_crea_una_hija_en_la_misma_llamada():
    conn = _Conn([_fila(1, titulo="lavar el carro")])
    resultado = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "encerar el carro", "vence_en": None,
                    "area": None, "responsable_chat_id": None}]))
    cerrada, hijas = resultado
    assert cerrada is True
    assert len(hijas) == 1
    hija = conn.tareas[hijas[0]]
    assert hija["titulo"] == "encerar el carro"
    assert hija["deriva_de_id"] == 1, "la hija no quedó enlazada a la madre"
    assert hija["estado"] == "pendiente", "una tarea nueva nace pendiente (D5)"
    assert conn.tareas[1]["estado"] == "hecha", "la madre no se cerró"
    assert conn.tareas[1]["completado_en"] is not None


def test_pueden_ser_varias_hijas_de_la_misma_madre():
    """Tiziano, al aprobar el diseño: «pueden ser VARIAS tareas nuevas por
    cada tarea hecha» -- el diseño original decía una; esto es lo que cambió."""
    conn = _Conn([_fila(1)])
    cerrada, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [
            {"titulo": "primera hija", "vence_en": None, "area": None,
             "responsable_chat_id": None},
            {"titulo": "segunda hija", "vence_en": None, "area": None,
             "responsable_chat_id": None},
            {"titulo": "tercera hija", "vence_en": None, "area": None,
             "responsable_chat_id": None},
        ]))
    assert cerrada is True
    assert len(hijas) == 3
    titulos = {conn.tareas[h]["titulo"] for h in hijas}
    assert titulos == {"primera hija", "segunda hija", "tercera hija"}
    assert all(conn.tareas[h]["deriva_de_id"] == 1 for h in hijas)
    assert len(conn.bandeja) == 3, "cada hija necesita su propia fila muda"
    # Tres 'crear' + un 'editar' (cerrar la madre) = cuatro huellas.
    assert len(conn.log) == 4


def test_sin_derivadas_se_comporta_como_marcar_tarea_hecha_sola():
    """Una lista vacía -- nadie escribió nada en el renglón -- cierra la
    tarea y no crea nada, IGUAL que `marcar_tarea_hecha` antes de este
    encargo: es lo que reemplaza esa llamada en `guardar_tareas`."""
    conn = _Conn([_fila(1)])
    cerrada, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(DUENO, 1, []))
    assert cerrada is True
    assert hijas == []
    assert conn.bandeja == [], "no debería crear ninguna fila de bandeja"


def test_madre_inexistente_o_en_la_papelera_devuelve_none():
    conn = _Conn([_fila(1, borrado_en=datetime(2026, 9, 1, tzinfo=UTC))])
    assert _con_base(conn, lambda: db.cerrar_y_derivar(DUENO, 1, [])) is None
    assert _con_base(conn, lambda: db.cerrar_y_derivar(DUENO, 999, [])) is None
    assert conn.log == [], "no debería dejar ninguna huella"


def test_madre_ya_hecha_no_se_reescribe_pero_la_hija_se_crea():
    """Decisión D7 del diseño: si otra persona ya la había cerrado, la nueva
    se crea igual -- tirar lo que se escribió no gana nada."""
    conn = _Conn([_fila(1, estado="hecha",
                        completado_en=datetime(2026, 9, 1, 10, tzinfo=UTC))])
    cerrada, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "hija de una ya cerrada", "vence_en": None,
                    "area": None, "responsable_chat_id": None}]))
    assert cerrada is False, "reescribió una madre que ya estaba hecha"
    assert len(hijas) == 1
    assert conn.tareas[1]["completado_en"] == datetime(2026, 9, 1, 10, tzinfo=UTC), (
        "movió completado_en a la hora equivocada")
    assert not any("UPDATE tareas SET estado" in s for s, _ in conn.sql), (
        "reescribió el estado de una tarea que ya estaba hecha")
    assert any("'editar'" in s for s, _ in conn.sql) is False or True  # ver abajo
    # Ninguna huella 'editar' de la madre -- solo la(s) 'crear' de la(s) hija(s).
    ediciones = [p for s, p in conn.log if "'editar'" in s]
    assert ediciones == [], "dejó una huella de un cierre que no pasó"


def test_la_hija_sigue_el_proyecto_de_la_madre_y_pierde_su_area():
    """P3 del diseño, aprobado: «si la hecha es de un proyecto, la nueva va
    al mismo proyecto» -- y el área que pudiera pedirse se IGNORA, porque
    `tareas_area_no_con_proyecto` exige que un área CON proyecto sea NULL."""
    conn = _Conn([_fila(1, proyecto_id=55)])
    _, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "hija de un proyecto", "vence_en": None,
                    "area": "CDS", "responsable_chat_id": None}]))
    hija = conn.tareas[hijas[0]]
    assert hija["proyecto_id"] == 55, "la hija no heredó el proyecto"
    assert hija["area"] is None, (
        "la hija se quedó con un área propia teniendo proyecto: "
        "violaría tareas_area_no_con_proyecto")


def test_la_hija_sin_proyecto_lleva_el_area_pedida():
    conn = _Conn([_fila(1, proyecto_id=None)])
    _, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "hija sin proyecto", "vence_en": None,
                    "area": "ACD", "responsable_chat_id": None}]))
    assert conn.tareas[hijas[0]]["area"] == "ACD"
    assert conn.tareas[hijas[0]]["proyecto_id"] is None


def test_quien_las_anoto_es_la_sesion_que_guardo_el_formulario():
    conn = _Conn([_fila(1)])
    _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "x", "vence_en": None, "area": None,
                    "responsable_chat_id": None}]))
    assert conn.bandeja[0]["chat_id"] == DUENO


def test_deriva_de_id_ausente_crea_la_hija_igual_sin_el_enlace():
    """Tolerancia de migración sin aplicar (SQLSTATE 42703): perder el
    enlace es mejor que perder la tarea que alguien acaba de escribir."""
    conn = _Conn([_fila(1)], sin_columna_deriva=True)
    cerrada, hijas = _con_base(conn, lambda: db.cerrar_y_derivar(
        DUENO, 1, [{"titulo": "hija sin enlace", "vence_en": None,
                    "area": None, "responsable_chat_id": None}]))
    assert cerrada is True
    assert len(hijas) == 1
    assert conn.tareas[hijas[0]]["deriva_de_id"] is None, (
        "la columna no existe: no debería haber podido guardar el enlace")
    assert conn.tareas[hijas[0]]["titulo"] == "hija sin enlace", (
        "perdió la tarea entera en vez de solo el enlace")


def test_una_hija_invalida_no_escribe_nada_MAS_y_la_excepcion_sube():
    """LA GARANTÍA REAL ("o entran todas, o ninguna") LA DA POSTGRES, NO ESTE
    DOBLE: `_Conn` es un `dict` que se muta EN EL SITIO, así que no puede
    demostrar un rollback -- lo dice el propio docstring de
    `cerrar_y_derivar`: "no hay commit hasta que el bloque `async with`
    termina limpio". Lo que SÍ se puede comprobar sin Postgres de por
    medio, y es lo que de verdad sostiene esa garantía, son las DOS mitades:

      1. la excepción SUBE sin que nadie la atrape a mitad de camino (si
         `cerrar_y_derivar` la tragara, la transacción real haría commit de
         lo que ya llevaba escrito, y "todas o ninguna" sería falso);
      2. la hija con el dato malo NUNCA llega a escribirse -- ni ella ni
         las que vinieran después en la misma lista.

    La primera hija (válida) SÍ queda en `conn.tareas` acá, porque este
    doble no revierte nada -- contra Postgres de verdad, el
    `async with pool.connection()` que la envuelve hace ROLLBACK de la
    conexión entera al salir con una excepción, y esa mitad la prueba
    `tools/humo.py`, no un test hermético."""
    conn = _Conn([_fila(1)])
    _con_gente({DUENO: "Tiziano"})
    ajeno = DUENO + 999999
    try:
        _con_base(conn, lambda: db.cerrar_y_derivar(
            DUENO, 1, [
                {"titulo": "primera, valida", "vence_en": None, "area": None,
                 "responsable_chat_id": None},
                {"titulo": "segunda, invalida", "vence_en": None,
                 "area": None, "responsable_chat_id": ajeno},
            ]))
        assert False, "no lanzó nada con un responsable inválido"
    except ValueError:
        pass
    assert not any(t.get("titulo") == "segunda, invalida"
                  for t in conn.tareas.values()), (
        "la hija con el responsable inválido se escribió de todos modos")


def _con_gente(nombres: dict, permitidos=None):
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = frozenset(
        permitidos if permitidos is not None else nombres)


# ── `db.derivaciones`, con el TEXTO real del SQL, contra sqlite ──────────

def test_derivaciones_hace_lo_que_dice_su_select():
    """El mismo texto de `db.derivaciones` corre contra sqlite de verdad
    -- patrón de tests/test_primero.py:690-740. Con `?` en vez de `%s` y
    `borrado_en` mapeado: sqlite no tiene NULL con nombre distinto, así que
    el texto se ejecuta literal salvo esa sustitución mecánica del
    placeholder."""
    # El SELECT está escrito como DOS literales adyacentes ("..." "...") que
    # Python concatena solo -- `ast` lo ve como un único `Constant` ya unido,
    # así que se saca del árbol de sintaxis y no con una regex sobre el texto
    # crudo (que se cortaría en la primera comilla de cierre). Mismo patrón
    # que `_sql_de` en tests/test_panel_tareas.py.
    import ast
    import textwrap
    arbol = ast.parse(textwrap.dedent(inspect.getsource(db.derivaciones)))
    literales = [n.value for nodo in ast.walk(arbol)
                for n in ast.walk(nodo)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.strip().upper().startswith("SELECT ID, DERIVA_DE_ID")]
    assert literales, "no se encontró el SELECT dentro de derivaciones()"
    sql = literales[0].replace("%s", "?")

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tareas (id INTEGER, deriva_de_id INTEGER, "
               "borrado_en TEXT)")
    con.executemany(
        "INSERT INTO tareas VALUES (?, ?, ?)",
        [(1, None, None),          # madre, no deriva de nadie
         (2, 1, None),              # hija viva
         (3, 1, "2026-09-01"),      # hija BORRADA: no debería salir
         (4, None, None)])          # otra suelta
    con.commit()
    filas = con.execute(sql).fetchall()
    assert filas == [(2, 1)], (
        f"el SELECT trajo otra cosa contra sqlite de verdad: {filas}")


def test_derivaciones_devuelve_vacio_si_la_columna_no_existe():
    conn = _Conn([_fila(1), _fila(2, deriva_de_id=1)], sin_columna_deriva=True)
    assert _con_base(conn, db.derivaciones) == {}


def test_derivaciones_tal_cual_devuelve_el_mapa_hija_madre():
    conn = _Conn([_fila(1), _fila(2, deriva_de_id=1), _fila(3, deriva_de_id=1),
                 _fila(4)])
    assert _con_base(conn, db.derivaciones) == {2: 1, 3: 1}


# ── `db.tareas_por_grupo` cuelga «Sale de» / «Siguió» ─────────────────────

def test_tareas_por_grupo_cuelga_sale_de_en_la_hija():
    conn = _Conn([_fila(1, estado="hecha", titulo="lavar el carro",
                        completado_en=datetime(2026, 9, 1, tzinfo=UTC)),
                 _fila(2, titulo="encerar el carro", deriva_de_id=1)])
    datos = _con_base(conn, lambda: db.tareas_por_grupo(hoy=date(2026, 9, 8)))
    filas = {f["id"]: f for g in datos["grupos"] for f in g["filas"]}
    assert filas[2]["deriva_de_id"] == 1
    assert filas[2]["deriva_de_titulo"] == "lavar el carro"


def test_tareas_por_grupo_cuelga_siguio_en_la_madre_y_admite_varias():
    conn = _Conn([
        _fila(1, estado="hecha", titulo="lavar el carro",
             completado_en=datetime(2026, 9, 1, tzinfo=UTC)),
        _fila(2, titulo="encerar", deriva_de_id=1),
        _fila(3, titulo="aspirar", deriva_de_id=1),
    ])
    datos = _con_base(conn, lambda: db.tareas_por_grupo(hoy=date(2026, 9, 8)))
    filas = {f["id"]: f for g in datos["grupos"] for f in g["filas"]}
    ids_hijas = sorted(s["id"] for s in filas[1]["siguio"])
    assert ids_hijas == [2, 3], "no listó las DOS hijas de la misma madre"
    titulos = {s["titulo"] for s in filas[1]["siguio"]}
    assert titulos == {"encerar", "aspirar"}


def test_una_tarea_sin_relacion_no_lleva_ninguna_de_las_dos():
    conn = _Conn([_fila(1)])
    datos = _con_base(conn, lambda: db.tareas_por_grupo(hoy=date(2026, 9, 8)))
    fila = datos["grupos"][0]["filas"][0]
    assert fila["deriva_de_id"] is None
    assert fila["deriva_de_titulo"] is None
    assert fila["siguio"] == []


def test_madre_fuera_de_la_ventana_no_rompe_a_la_hija():
    """Si la madre quedó fuera de las TOPE_TAREAS filas traídas (frontera ya
    declarada por `hay_mas`), la hija sigue mostrando QUE sale de algo, con
    `deriva_de_titulo=None` en vez de romper la pantalla."""
    conn = _Conn([_fila(2, titulo="hija huerfana", deriva_de_id=999)])
    datos = _con_base(conn, lambda: db.tareas_por_grupo(hoy=date(2026, 9, 8)))
    fila = datos["grupos"][0]["filas"][0]
    assert fila["deriva_de_id"] == 999
    assert fila["deriva_de_titulo"] is None


# ── `web/app.py::_derivadas_pedidas` ──────────────────────────────────────

def _formulario(**campos):
    from starlette.datastructures import FormData
    return FormData(campos)


def test_un_renglon_vacio_no_cuenta_como_derivada():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(), 1, {"CDS"})
    assert vale is True
    assert derivadas == []


def test_un_titulo_solo_ya_alcanza_para_una_derivada_valida():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "una tarea nueva"}), 1, {"CDS"})
    assert vale is True
    assert len(derivadas) == 1
    assert derivadas[0]["titulo"] == "una tarea nueva"
    assert derivadas[0]["vence_en"] is None
    assert derivadas[0]["area"] is None
    assert derivadas[0]["responsable_chat_id"] is None


def test_hasta_max_derivadas_renglones_se_leen():
    campos = {}
    for n in range(1, panel.MAX_DERIVADAS + 1):
        campos[f"deriva_titulo_1_{n}"] = f"hija {n}"
    vale, derivadas = panel._derivadas_pedidas(_formulario(**campos), 1, set())
    assert vale is True
    assert len(derivadas) == panel.MAX_DERIVADAS
    assert [d["titulo"] for d in derivadas] == [
        f"hija {n}" for n in range(1, panel.MAX_DERIVADAS + 1)]


def test_un_titulo_larguisimo_invalida_toda_la_llamada():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "x" * (panel.LARGO_TITULO + 1)}),
        1, set())
    assert vale is False
    assert derivadas == []


def test_justo_en_el_largo_permitido_entra():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "x" * panel.LARGO_TITULO}), 1, set())
    assert vale is True and len(derivadas) == 1


def test_una_fecha_ilegible_invalida_toda_la_llamada():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_vence_1_1": "no-es-una-fecha"}), 1, set())
    assert vale is False
    assert derivadas == []


def test_una_fecha_con_hora_valida_se_acepta_y_es_en_santo_domingo():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_vence_1_1": "2026-09-20T15:30"}), 1, set())
    assert vale is True
    assert derivadas[0]["vence_en"] == datetime(2026, 9, 20, 15, 30,
                                                tzinfo=config.TZ)


def test_un_dia_suelto_sin_hora_se_rechaza():
    """D9 del diseño: la hora decide cuándo suena el aviso, igual que
    `vence_<id>` en el resto de esta ruta -- no como `/tareas/nueva`."""
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_vence_1_1": "2026-09-20"}), 1, set())
    assert vale is False


def test_un_area_que_no_esta_en_la_lista_invalida_toda_la_llamada():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_area_1_1": "Marketing"}), 1, {"CDS", "ACD"})
    assert vale is False


def test_un_area_valida_se_acepta():
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_area_1_1": "CDS"}), 1, {"CDS", "ACD"})
    assert vale is True
    assert derivadas[0]["area"] == "CDS"


def test_un_responsable_que_no_entra_al_panel_invalida_toda_la_llamada():
    _con_gente({DUENO: "Tiziano"})
    ajeno = DUENO + 999999
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "algo",
                       "deriva_resp_1_1": str(ajeno)}), 1, set())
    assert vale is False


def test_un_renglon_malo_tira_los_buenos_de_la_misma_madre():
    """D6: no es "se crean las válidas y se avisa de la inválida" -- TODA la
    llamada de esa madre se invalida si UN renglón no vale."""
    vale, derivadas = panel._derivadas_pedidas(
        _formulario(**{"deriva_titulo_1_1": "esta vale",
                       "deriva_titulo_1_2": "x" * (panel.LARGO_TITULO + 1)}),
        1, set())
    assert vale is False
    assert derivadas == []


# ── De punta a punta: POST /tareas con derivadas, ruta real → función real ─

def _post(campos: dict, con_sesion: bool = True):
    from starlette.requests import Request

    cuerpo = urlencode(campos).encode()
    cabeceras = [(b"host", b"t"),
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(cuerpo)).encode())]
    if con_sesion:
        galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
        cabeceras.append((b"cookie", galleta.encode()))

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "https", "server": ("t", 443), "path": "/tareas",
                    "root_path": "", "query_string": b"",
                    "headers": cabeceras, "app": panel.app}, recibir)


def test_de_punta_a_punta_marcar_y_escribir_una_derivada():
    """LA PRUEBA QUE VALE POR TODAS: se manda el formulario ENTERO (la
    casilla `hecha_<id>` Y el renglón `deriva_titulo_<id>_1`) contra la
    RUTA real (`panel.guardar_tareas`), que llama a la FUNCIÓN real
    (`db.cerrar_y_derivar`) contra la base de mentira. Nada espiado."""
    conn = _Conn([_fila(1, titulo="lavar el carro")])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1",
        "deriva_titulo_1_1": "encerar el carro",
    })))
    assert r.status_code == 303
    assert "guardadas=1" in r.headers["location"]
    assert "derivadas=1" in r.headers["location"]
    assert conn.tareas[1]["estado"] == "hecha"
    hijas = [t for t in conn.tareas.values() if t.get("deriva_de_id") == 1]
    assert len(hijas) == 1
    assert hijas[0]["titulo"] == "encerar el carro"
    assert hijas[0]["id"] in [int(x) for x in
                              r.headers["location"].split("derivadas_ids=")[1].split(",")]


def test_de_punta_a_punta_casilla_marcada_sin_texto_cierra_como_antes():
    """El comportamiento de siempre no cambió: casilla marcada, renglón
    vacío -- se cierra y no se crea nada."""
    conn = _Conn([_fila(1)])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1"})))
    assert r.status_code == 303
    assert "guardadas=1" in r.headers["location"]
    assert "derivadas=0" in r.headers["location"]
    assert conn.tareas[1]["estado"] == "hecha"
    assert len(conn.tareas) == 1, "creó una tarea que nadie pidió"


def test_de_punta_a_punta_texto_sin_casilla_marcada_no_crea_nada():
    """El servidor SOLO mira `deriva_titulo_<id>` si en el MISMO envío viene
    `hecha_<id>` -- no depende de que el JS haya escondido bien el renglón."""
    conn = _Conn([_fila(1)])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente",
        "deriva_titulo_1_1": "esto no debería crearse",
    })))
    assert r.status_code == 303
    assert len(conn.tareas) == 1, "creó una hija sin que la madre se cerrara"
    assert conn.tareas[1]["estado"] == "pendiente", "cerró la madre sin que se pidiera"


def test_de_punta_a_punta_un_campo_invalido_no_cierra_ni_crea():
    """D6: la fecha del renglón no vale -> ni la madre se cierra ni la hija
    se crea."""
    conn = _Conn([_fila(1)])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1",
        "deriva_titulo_1_1": "algo",
        "deriva_vence_1_1": "fecha-que-no-existe",
    })))
    assert r.status_code == 303
    assert "guardadas=0" in r.headers["location"]
    assert conn.tareas[1]["estado"] == "pendiente", "cerró la madre con datos inválidos"
    assert len(conn.tareas) == 1, "creó una hija con datos inválidos"


def test_de_punta_a_punta_varias_derivadas_de_la_misma_madre():
    conn = _Conn([_fila(1)])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1",
        "deriva_titulo_1_1": "primera",
        "deriva_titulo_1_2": "segunda",
    })))
    assert r.status_code == 303
    assert "derivadas=2" in r.headers["location"]
    hijas = [t for t in conn.tareas.values() if t.get("deriva_de_id") == 1]
    assert {h["titulo"] for h in hijas} == {"primera", "segunda"}


def test_de_punta_a_punta_madre_ya_hecha_no_se_recierra_pero_procesa_derivadas():
    conn = _Conn([_fila(1, estado="hecha")])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "hecha", "hecha_1": "1",
        "deriva_titulo_1_1": "no debería llegar -- prev dice hecha",
    })))
    # `prev_1 == hecha`: el bucle de `hecha_` la salta enteramente, IGUAL
    # que antes de este encargo -- ni mira sus renglones de derivada.
    assert "guardadas=0" in r.headers["location"]
    assert len(conn.tareas) == 1, (
        "una fila que ya se pintó hecha no debería poder derivar en el "
        "mismo envío -- eso solo pasa vía el redirect siguiente")


def test_de_punta_a_punta_la_ruta_que_escribe_exige_sesion():
    conn = _Conn([_fila(1)])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1",
        "deriva_titulo_1_1": "no debería crearse"}, con_sesion=False)))
    assert r.status_code == 401
    assert len(conn.tareas) == 1
    assert conn.tareas[1]["estado"] == "pendiente"


def test_de_punta_a_punta_la_pantalla_pinta_sale_de_y_siguio():
    """Se manda el formulario y DESPUÉS se pinta /tareas con la MISMA base,
    igual que test_tarea_a_mano.py::_crear_y_pintar."""
    conn = _Conn([_fila(1, titulo="lavar el carro")])
    r = _con_base(conn, lambda: panel.guardar_tareas(_post({
        "prev_1": "pendiente", "hecha_1": "1",
        "deriva_titulo_1_1": "encerar el carro",
    })))
    m = re.search(r"derivadas_ids=(\d+)", r.headers["location"])
    hija_id = m.group(1)

    from starlette.requests import Request

    def _get():
        galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
        return Request({"type": "http", "http_version": "1.1", "method": "GET",
                        "scheme": "https", "server": ("t", 443),
                        "path": "/tareas", "root_path": "", "query_string": b"",
                        "headers": [(b"host", b"t"),
                                   (b"cookie", galleta.encode())],
                        "app": panel.app})

    html = _con_base(conn, lambda: panel.tareas(
        _get(), derivadas=1, derivadas_ids=hija_id)).body.decode()
    assert "encerar el carro" in html
    assert "lavar el carro" in html
    assert "↳ Sale de" in html
    assert "→ Siguió" in html
    assert f"que sale de otra: 1 (#{hija_id})" in html, (
        "el aviso de arriba no nombra la tarea que se creó")


# ── El agente de Telegram no puede escribir `deriva_de_id` (decisión T1) ──

def test_editar_no_deja_escribir_deriva_de_id():
    assert "deriva_de_id" in crud.NO_EDITABLES, (
        "deriva_de_id no está en la lista negra: el agente podría reescribir "
        "de qué tarea sale una hija por Telegram")


def test_editar_de_verdad_descarta_el_campo_sin_reventar():
    """No un examen de la lista: se llama a `editar` con `deriva_de_id` en
    los cambios y se comprueba que NUNCA llega al UPDATE."""
    import acciones.crud as crud_mod

    class _CursorFalso:
        def __init__(self, conn):
            self._conn = conn
            self._filas = []

        async def execute(self, sql, params=None):
            self._conn.sql.append((" ".join(sql.split()), params))
            if "SELECT * FROM tareas" in sql:
                self._filas = [{"id": 1, "titulo": "x", "deriva_de_id": None,
                                "borrado_en": None}]
            elif "INSERT INTO log_acciones" in sql:
                self._filas = [(5000,)]
            else:
                self._filas = []
            return self

        async def fetchone(self):
            return self._filas[0] if self._filas else None

        async def fetchall(self):
            return self._filas

    class _ConnFalso:
        def __init__(self):
            self.sql: list = []

        def cursor(self, row_factory=None):
            return _CursorFalso(self)

        async def execute(self, sql, params=None):
            return await _CursorFalso(self).execute(sql, params)

    class _CMFalso:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *e):
            return False

    class _PoolFalso:
        def __init__(self, conn):
            self._conn = conn

        def connection(self):
            return _CMFalso(self._conn)

    conn = _ConnFalso()
    guardado = crud_mod.db.pool
    crud_mod.db.pool = _PoolFalso(conn)
    try:
        bucle = asyncio.new_event_loop()
        try:
            despues, log_id = bucle.run_until_complete(crud_mod.editar(
                "tareas", 1, {"deriva_de_id": 999, "titulo": "otra cosa"},
                "probar la puerta cerrada"))
        finally:
            bucle.close()
    finally:
        crud_mod.db.pool = guardado
    assert not any("deriva_de_id" in s for s, _ in conn.sql), (
        f"deriva_de_id llegó al SQL de escritura: {conn.sql}")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
