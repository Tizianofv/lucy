# -*- coding: utf-8 -*-
"""Editar el nombre de un proyecto (pieza 1 del diseño «proyectos», pedido de
Tiziano del 29-sep-2026: «poder editar los titulos de los proyectos»).

QUÉ SE VIGILA, y con qué camino:

  · La ruta real del panel (`panel.cambiar_nombre_de_proyecto`) y las funciones
    reales (`crud.editar`, `crud.deshacer`, `crud.perfil`, `db._buscar_o_crear`,
    `db.convertir_tarea_en_proyecto`) contra una base sqlite que EJECUTA DE
    VERDAD el SQL del repositorio (el mismo texto, `%s` → `?`). Límites dichos:
    sqlite no es Postgres, y `jsonb_populate_record` (el UPDATE de `deshacer`)
    se EMULA acá con Python; lo que se comprueba es el texto y la lógica, no lo
    que Postgres haría con ellos.
  · Una sola puerta para el nombre, la misma por Telegram y por el panel:
    vacío, solo espacios, 201 caracteres, otro proyecto vivo con el mismo
    nombre (con otras mayúsculas), el mismo nombre, un id que no existe y un
    proyecto borrado.
  · LOS HERMANOS SALEN DE LO REAL: un censo del AST del repositorio encuentra
    todo lo que escribe `proyectos.nombre` (SQL legible) y los escritores
    genéricos; cada uno tiene que nombrar la puerta y la función de duplicados.

Correr:  python3 -m pytest tests/test_nombre_de_proyecto.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tarea_a_mano as base  # noqa: E402  (deja `psycopg` falseado)
import test_responsable as tr  # noqa: E402  (las piezas del censo por AST)
import test_buzon_que_no_se_ve as barrido  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import acciones.crud as crud  # noqa: E402
import cerebro.agente as agente  # noqa: E402
import web.app as panel  # noqa: E402

# EL 200 DEL DISEÑO, ESCRITO ACÁ A PROPÓSITO y no leído de `db`: si alguien
# cambiara la constante del código, una prueba que la leyera se movería con
# ella y seguiría en verde (hallazgo del testigo sobre 2cb3e6d).
LARGO = 200


def test_el_largo_maximo_es_el_200_del_diseno():
    assert db.LARGO_NOMBRE_PROYECTO == 200


# ── Una base sqlite que ejecuta el SQL del repo ──────────────────────────

class _Cur:
    def __init__(self, base_, dict_rows):
        self._b = base_
        self._dict = dict_rows
        self._cur = None

    async def execute(self, sql, params=None):
        self._b.sql.append(" ".join(sql.split()))
        emulado = self._emular_deshacer(sql, params)
        if emulado is not None:
            self._cur = emulado
            return self
        s = sql.replace("%s", "?").replace("now()", "CURRENT_TIMESTAMP")
        conv = [p.isoformat() if isinstance(p, datetime) else p
                for p in (params or ())]
        self._cur = self._b.con.execute(s, conv)
        return self

    def _emular_deshacer(self, sql, params):
        """`jsonb_populate_record` es de Postgres: se emula con Python."""
        m = re.search(r"UPDATE (\w+) t SET (.+?) FROM jsonb_populate_record",
                      " ".join(sql.split()))
        if not m:
            return None
        tabla = m.group(1)
        columnas = [c.split("=")[0].strip() for c in m.group(2).split(",")]
        datos = json.loads(params[0])
        return self._b.con.execute(
            f"UPDATE {tabla} SET " + ", ".join(f"{c} = ?" for c in columnas)
            + " WHERE id = ?", [datos[c] for c in columnas] + [params[1]])

    def _fila(self, f):
        if f is None:
            return None
        nombres = [d[0] for d in self._cur.description]
        fila = dict(zip(nombres, f))
        for k in ("antes", "despues"):       # psycopg entrega jsonb como dict
            if isinstance(fila.get(k), str):
                fila[k] = json.loads(fila[k])
        return fila if self._dict else tuple(fila.values())

    async def fetchone(self):
        return self._fila(self._cur.fetchone())

    async def fetchall(self):
        return [self._fila(f) for f in self._cur.fetchall()]


class _Conn:
    def __init__(self, b):
        self._b = b

    def cursor(self, row_factory=None):
        return _Cur(self._b, row_factory is not None)

    async def execute(self, sql, params=None):
        return await _Cur(self._b, False).execute(sql, params)

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *e):
                return False
        return _T()


class Base:
    def __init__(self):
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.sql: list = []
        self.con.executescript("""
            CREATE TABLE proyectos (id INTEGER PRIMARY KEY, creado_en, nombre TEXT
              NOT NULL, descripcion, estado TEXT DEFAULT 'activo', area,
              borrado_en, bandeja_id);
            CREATE TABLE log_acciones (id INTEGER PRIMARY KEY, actor, accion,
              tabla, registro_id, antes, despues, motivo, bandeja_id);
            CREATE TABLE tareas (id INTEGER PRIMARY KEY, bandeja_id, titulo,
              detalle, estado TEXT DEFAULT 'pendiente', area, proyecto_id,
              borrado_en);
        """)

    def proyecto(self, nombre, borrado=False):
        c = self.con.execute(
            "INSERT INTO proyectos (nombre, borrado_en) VALUES (?, ?)",
            (nombre, "2026-09-01" if borrado else None))
        return c.lastrowid

    def nombre_de(self, pid):
        f = self.con.execute("SELECT nombre FROM proyectos WHERE id = ?",
                             (pid,)).fetchone()
        return f[0] if f else None

    def nombres_vivos(self):
        return [f[0] for f in self.con.execute(
            "SELECT nombre FROM proyectos WHERE borrado_en IS NULL ORDER BY id")]

    def huellas(self):
        return self.con.execute(
            "SELECT actor, accion, tabla, registro_id, antes, despues "
            "FROM log_acciones ORDER BY id").fetchall()

    def updates(self):
        return [q for q in self.sql if q.startswith("UPDATE proyectos")]

    def pool(self):
        b = self

        class _CM:
            async def __aenter__(s):
                return _Conn(b)

            async def __aexit__(s, *e):
                return False

        class _P:
            def connection(s):
                return _CM()
        return _P()


def _correr(b, fn):
    guardado = db.pool
    db.pool = b.pool()
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(fn())
    finally:
        bucle.close()
        db.pool = guardado


def _rechazo(b, fn):
    try:
        _correr(b, fn)
    except ValueError as e:
        return e
    return None


def _renombrar(b, pid, nombre, con_sesion=True):
    return _correr(b, lambda: panel.cambiar_nombre_de_proyecto(
        base._post({"nombre": nombre}, con_sesion), pid))


def _editar(pid, nombre):
    return lambda: crud.editar("proyectos", pid, {"nombre": nombre},
                               motivo="prueba")


# ── El camino feliz, por el panel ────────────────────────────────────────

def test_renombrar_desde_el_panel_escribe_solo_el_nombre_con_huella_de_panel():
    b = Base()
    pid = b.proyecto("Viejo")
    r = _renombrar(b, pid, "  Nombre nuevo  ")
    assert r.status_code == 303
    assert r.headers["location"] == f"/proyectos?nombre_guardado={pid}#proyecto-{pid}"
    assert b.nombre_de(pid) == "Nombre nuevo", "no quedó limpio y guardado"
    assert b.updates() == ["UPDATE proyectos SET nombre = ? WHERE id = ?".replace(
        "?", "%s")], f"tocó más que el nombre: {b.updates()}"
    (actor, accion, tabla, rid, antes, despues), = b.huellas()
    assert (actor, accion, tabla, rid) == ("panel", "editar", "proyectos", pid)
    assert json.loads(antes)["nombre"] == "Viejo"
    assert json.loads(despues)["nombre"] == "Nombre nuevo"


def test_el_largo_maximo_es_200_y_201_no_entra():
    b = Base()
    pid = b.proyecto("x")
    assert _renombrar(b, pid, "a" * LARGO).headers["location"].startswith(
        "/proyectos?nombre_guardado=")
    assert b.nombre_de(pid) == "a" * LARGO
    antes = b.huellas()
    r = _renombrar(b, pid, "b" * (LARGO + 1))
    assert r.headers["location"] == f"/proyectos?error=nombre_largo#proyecto-{pid}"
    assert b.nombre_de(pid) == "a" * LARGO and b.huellas() == antes


# ── Las entradas inventadas: por el panel y por Telegram ─────────────────

def test_los_rechazos_no_escriben_nada_y_dicen_su_clave_por_el_panel():
    casos = [("", "nombre_vacio"), ("   ", "nombre_vacio"),
             ("z" * (LARGO + 1), "nombre_largo"),
             ("OTRO PROYECTO", "nombre_repetido"),   # otras mayúsculas
             ("otro proyecto", "nombre_repetido")]
    for texto, clave in casos:
        b = Base()
        pid = b.proyecto("Mio")
        b.proyecto("Otro Proyecto")
        b.proyecto("Ya no existe", borrado=True)
        r = _renombrar(b, pid, texto)
        assert r.headers["location"] == f"/proyectos?error={clave}#proyecto-{pid}", (
            f"{texto[:12]!r}: {r.headers['location']}")
        assert b.nombre_de(pid) == "Mio", f"{texto[:12]!r}: escribió"
        assert b.updates() == [] and b.huellas() == [], (
            f"{texto[:12]!r}: dejó rastro de un cambio que no pasó")


def test_los_mismos_rechazos_valen_por_telegram_con_crud_editar():
    for texto in ("", "   ", "z" * (LARGO + 1), "OTRO proyecto"):
        b = Base()
        pid = b.proyecto("Mio")
        b.proyecto("Otro Proyecto")
        e = _rechazo(b, _editar(pid, texto))
        assert isinstance(e, ValueError), f"Telegram aceptó {texto[:12]!r}"
        assert str(e).startswith("No cambié nada:"), str(e)
        assert texto.strip() == "" or texto not in str(e), (
            "el mensaje repite el nombre pedido")
        assert b.nombre_de(pid) == "Mio" and b.updates() == []


def test_un_proyecto_borrado_no_cuenta_como_repetido():
    b = Base()
    pid = b.proyecto("Mio")
    b.proyecto("Viejo Borrado", borrado=True)
    r = _renombrar(b, pid, "viejo borrado")
    assert r.headers["location"].startswith("/proyectos?nombre_guardado=")
    assert b.nombre_de(pid) == "viejo borrado"


def test_id_que_no_existe_o_proyecto_borrado_dan_error_proyecto_sin_escribir():
    b = Base()
    borrado = b.proyecto("Borrado", borrado=True)
    for pid in (9999, borrado):
        r = _renombrar(b, pid, "Loquesea")
        assert r.headers["location"] == "/proyectos?error=proyecto", pid
    assert b.updates() == [] and b.huellas() == []
    assert b.nombre_de(borrado) == "Borrado"


def test_el_mismo_nombre_no_escribe_ni_deja_huella_pero_las_mayusculas_si_cuentan():
    b = Base()
    pid = b.proyecto("Casa")
    r = _renombrar(b, pid, " Casa ")
    assert r.headers["location"] == f"/proyectos?error=nombre_igual#proyecto-{pid}"
    assert b.updates() == [] and b.huellas() == []
    # Por Telegram, lo mismo: no escribe y no hay log_id.
    despues, log_id = _correr(b, _editar(pid, "Casa"))
    assert log_id is None and b.updates() == [] and b.huellas() == []
    # Cambiar SOLO las mayúsculas de sí mismo es un cambio válido (no choca
    # consigo mismo).
    r = _renombrar(b, pid, "CASA")
    assert r.headers["location"].startswith("/proyectos?nombre_guardado=")
    assert b.nombre_de(pid) == "CASA" and len(b.huellas()) == 1


def test_sin_sesion_no_se_renombra():
    b = Base()
    pid = b.proyecto("Mio")
    r = _renombrar(b, pid, "Otro", con_sesion=False)
    assert r.status_code != 303 or "/proyectos" not in r.headers.get("location", "")
    assert b.nombre_de(pid) == "Mio" and b.updates() == []


def test_el_rechazo_no_lleva_el_nombre_pedido_ni_en_la_url_ni_en_el_log(caplog):
    b = Base()
    pid = b.proyecto("Mio")
    b.proyecto("Existente")
    secreto = "EXISTENTE"
    with caplog.at_level(logging.WARNING):
        r = _renombrar(b, pid, secreto)
    assert secreto not in r.headers["location"]
    assert secreto not in caplog.text, "el log lleva el nombre pedido"


# ── La consulta de duplicados, ejecutada de verdad ───────────────────────

def test_proyecto_vivo_con_nombre_ejecuta_su_sql_de_verdad():
    b = Base()
    a = b.proyecto("Álbum")
    c = b.proyecto("Casa")
    borrado = b.proyecto("Fantasma", borrado=True)

    def buscar(nombre, excluir=None):
        async def f():
            cur = _Conn(b).cursor(row_factory=object)
            return await db.proyecto_vivo_con_nombre(cur, nombre, excluir)
        return _correr(b, f)

    assert buscar("casa") == c and buscar("CASA") == c
    assert buscar("casa", excluir=c) is None, "chocó consigo mismo"
    assert buscar("fantasma") is None, "un borrado cuenta"
    # Ignorar tildes NO lo hace la búsqueda de Lucy (solo `lower()`), y el
    # chequeo usa la misma consulta: «Album» no es «Álbum». (El `lower()` de
    # sqlite no baja mayúsculas con tilde, el de Postgres sí: por eso acá no se
    # prueba «ÁLBUM» contra «álbum».)
    assert buscar("Album") is None
    assert buscar("Álbum") == a
    del borrado


# ── La misma puerta para todos los que escriben el nombre ────────────────

def test_lucy_encuentra_el_proyecto_por_la_misma_comparacion_y_no_duplica():
    b = Base()
    pid = b.proyecto("Mi Proyecto")
    assert _correr(b, lambda: db.buscar_o_crear_proyecto("mi proyecto")) == pid
    assert b.nombres_vivos() == ["Mi Proyecto"], "creó un duplicado"
    r = _correr(b, lambda: crud.perfil("proyecto", "MI PROYECTO", nota="hola"))
    assert "actualizado" in r[0] and b.nombres_vivos() == ["Mi Proyecto"]


def test_crear_un_proyecto_por_los_otros_caminos_tambien_pasa_por_la_puerta():
    largo = "n" * (LARGO + 1)
    b = Base()
    assert isinstance(_rechazo(b, lambda: db.buscar_o_crear_proyecto(largo)),
                      ValueError)
    assert isinstance(_rechazo(b, lambda: crud.perfil("proyecto", largo,
                                                       nota="x")), ValueError)
    assert b.nombres_vivos() == [], "creó un proyecto con nombre inválido"
    # Convertir una tarea: título repetido (otras mayúsculas) o demasiado largo.
    for titulo in ("PROYECTO YA HECHO", largo):
        b = Base()
        b.proyecto("Proyecto Ya Hecho")
        b.con.execute("INSERT INTO tareas (id, titulo, bandeja_id) VALUES (7, ?, 1)",
                      (titulo,))
        e = _rechazo(b, lambda: db.convertir_tarea_en_proyecto(7))
        assert isinstance(e, ValueError), f"convirtió {titulo[:10]!r}"
        assert b.nombres_vivos() == ["Proyecto Ya Hecho"]
        assert b.con.execute("SELECT borrado_en FROM tareas WHERE id=7").fetchone()[0] \
            is None, "archivó la tarea aunque no creó el proyecto"
    b = Base()
    b.con.execute("INSERT INTO tareas (id, titulo, bandeja_id) VALUES (7, 'Nuevo', 1)")
    res = _correr(b, lambda: db.convertir_tarea_en_proyecto(7))
    assert res["proyecto_nombre"] == "Nuevo"


# ── Deshacer ─────────────────────────────────────────────────────────────

def _deshacer(b, log_id):
    return lambda: crud.deshacer(log_id)


def _ultimo_log(b):
    return b.con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]


def test_deshacer_un_renombre_devuelve_el_nombre():
    b = Base()
    pid = b.proyecto("Viejo")
    _renombrar(b, pid, "Nuevo")
    _correr(b, _deshacer(b, _ultimo_log(b)))
    assert b.nombre_de(pid) == "Viejo"


def test_deshacer_un_renombre_no_reintroduce_un_duplicado():
    b = Base()
    pid = b.proyecto("Viejo")
    _renombrar(b, pid, "Nuevo")
    log_id = _ultimo_log(b)
    b.proyecto("viejo")            # otro proyecto tomó el nombre de antes
    e = _rechazo(b, _deshacer(b, log_id))
    assert isinstance(e, ValueError) and "ya hay otro proyecto" in str(e)
    assert b.nombre_de(pid) == "Nuevo", "restauró saltándose la regla"


def test_deshacer_el_borrado_de_un_proyecto_no_lo_revive_duplicado():
    b = Base()
    pid = b.proyecto("Casa")
    log_id = _correr(b, lambda: crud.borrar("proyectos", pid, "prueba"))
    b.proyecto("casa")             # nació otro con ese nombre
    e = _rechazo(b, _deshacer(b, log_id))
    assert isinstance(e, ValueError) and "ya hay otro proyecto" in str(e)
    assert b.con.execute("SELECT borrado_en FROM proyectos WHERE id=?",
                         (pid,)).fetchone()[0] is not None
    # Sin choque, sí lo revive.
    b2 = Base()
    pid2 = b2.proyecto("Solo")
    log2 = _correr(b2, lambda: crud.borrar("proyectos", pid2, "prueba"))
    _correr(b2, _deshacer(b2, log2))
    assert b2.con.execute("SELECT borrado_en FROM proyectos WHERE id=?",
                          (pid2,)).fetchone()[0] is None


def test_deshacer_que_volveria_a_un_nombre_invalido_lo_dice_sin_hablar_de_tareas():
    b = Base()
    pid = b.proyecto("Nuevo")
    b.con.execute(
        "INSERT INTO log_acciones (id, actor, accion, tabla, registro_id, antes, despues)"
        " VALUES (50, 'lucy', 'editar', 'proyectos', ?, ?, ?)",
        (pid, json.dumps({"id": pid, "nombre": "", "estado": "activo"}),
         json.dumps({"id": pid, "nombre": "Nuevo", "estado": "activo"})))
    e = _rechazo(b, _deshacer(b, 50))
    assert isinstance(e, ValueError) and str(e).startswith("No lo deshice:")
    assert "tarea" not in str(e).lower(), f"el mensaje habla de tareas: {e}"
    assert b.nombre_de(pid) == "Nuevo"


def test_deshacer_una_edicion_que_no_toco_el_nombre_no_lo_toca_ni_lo_revisa():
    b = Base()
    pid = b.proyecto("Uno")
    _correr(b, lambda: crud.editar("proyectos", pid, {"estado": "pausado"},
                                   motivo="prueba"))
    log_estado = _ultimo_log(b)
    _correr(b, lambda: crud.editar("proyectos", pid, {"nombre": "Tres"},
                                   motivo="prueba"))
    b.proyecto("uno")   # otro proyecto con el nombre de ANTES: no debe importar
    _correr(b, _deshacer(b, log_estado))
    assert b.nombre_de(pid) == "Tres", "deshacer el estado tocó el nombre"
    assert b.con.execute("SELECT estado FROM proyectos WHERE id=?",
                         (pid,)).fetchone()[0] == "activo"


# ── Lo que lee el agente y lo que pinta la pantalla ──────────────────────

def test_el_nombre_nuevo_llega_a_la_lista_de_lucy_y_a_su_prompt():
    b = Base()
    pid = b.proyecto("Viejo")
    _renombrar(b, pid, "Nombre nuevo")
    vivos = _correr(b, db.proyectos_vivos)
    assert [p["nombre"] for p in vivos] == ["Nombre nuevo"]
    assert '"Nombre nuevo"' in agente.herramientas_del_prompt([], vivos)
    assert '"Viejo"' not in agente.herramientas_del_prompt([], vivos)


def _pintar(proyectos, **kw):
    """La página con la lista de proyectos que se le da (sin base)."""
    async def _lista():
        return proyectos

    async def _areas():
        return []
    g = (db.proyectos_con_tareas, db.areas)
    db.proyectos_con_tareas, db.areas = _lista, _areas
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(
                panel.proyectos(base._get("/proyectos"), **kw)).body.decode()
        finally:
            bucle.close()
    finally:
        db.proyectos_con_tareas, db.areas = g


def _proyecto_falso(pid, nombre):
    return {"id": pid, "nombre": nombre, "descripcion": None, "estado": "activo",
            "area": None, "color": None, "tareas": []}


def test_cada_tarjeta_trae_su_formulario_de_nombre_con_el_largo_y_escapado():
    peligroso = '"><script>alert(1)</script>'
    html = _pintar([_proyecto_falso(1, "Uno"), _proyecto_falso(2, peligroso)])
    for pid in (1, 2):
        assert f'action="/proyectos/{pid}/nombre"' in html
    assert html.count(f'maxlength="{LARGO}"') == 2
    assert "<script>alert(1)" not in html, "el nombre salió sin escapar"
    assert 'value="Uno"' in html


def test_los_rechazos_y_el_guardado_se_traducen_a_palabras():
    for clave in ("nombre_vacio", "nombre_largo", "nombre_repetido",
                  "nombre_igual", "nombre_invalido"):
        html = _pintar([_proyecto_falso(1, "Uno")], error=clave)
        assert 'class="aviso"' in html, f"{clave} no se traduce"
    assert "Nombre guardado" in _pintar([_proyecto_falso(1, "Uno")],
                                        nombre_guardado=1)
    assert 'class="aviso"' not in _pintar([_proyecto_falso(1, "Uno")],
                                          error="inventada")


# ── LOS HERMANOS, sacados de lo real ─────────────────────────────────────

_ESCRIBE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE)\s+(?:proyectos\b|<tabla>)",
                      re.I)
_NOMBRA = re.compile(r"\bnombre\b")
_PUERTA = db.nombre_de_proyecto_que_vale.__name__
_UNICA = db.proyecto_vivo_con_nombre.__name__


def _censo_del_nombre() -> dict:
    """Todo `execute` del repo, en su cubo: SQL LEGIBLE que escribe `nombre` en
    `proyectos` (o en una tabla armada al vuelo), y escritores GENÉRICOS (SQL que
    no se puede reconstruir). Misma frontera que
    `tests/test_responsable.py::_censo`, cuyas piezas usa."""
    raiz = Path(base.RAIZ).resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    salida = {"sitios": 0, "legibles": {}, "genericos": {}}
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        rel = real.relative_to(raiz).as_posix()
        modulo = ast.parse(real.read_text(encoding="utf-8"), str(real))
        globales = tr._asignaciones(modulo)
        for funcion, llamada in tr._llamadas_a_execute(modulo):
            salida["sitios"] += 1
            quien = f"{rel}::{funcion.name if funcion else '<módulo>'}"
            sql = tr._sql_de(llamada)
            piezas = (None if sql is None else tr._piezas(
                sql, tr._asignaciones(funcion) if funcion else {}, globales,
                tr._parametros(funcion)))
            legible, texto = tr._legible(piezas)
            if not legible:
                if not (funcion is not None and tr._ejecuta_solo_lectura(funcion)):
                    salida["genericos"].setdefault(quien, funcion)
            elif _ESCRIBE.search(texto) and _NOMBRA.search(texto):
                salida["legibles"].setdefault(quien, funcion)
    return salida


def _llamadas(nodo) -> set:
    """Los nombres de las funciones que un nodo LLAMA (no los que solo
    menciona): `f(...)` y `x.f(...)`. Una referencia suelta (`_ = f`) no cuenta.
    FRONTERA DICHA: no ve si la llamada está en una rama que nunca corre
    (`if False:`); eso lo miden las pruebas de comportamiento de este archivo."""
    salida = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                salida.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                salida.add(n.func.attr)
    return salida


def test_todo_lo_que_escribe_el_nombre_pasa_por_la_puerta_y_la_funcion_unica():
    censo = _censo_del_nombre()
    assert censo["sitios"], "el censo no vio ni un execute: estaría verde sin mirar"
    legibles = censo["legibles"]
    # El censo VE a los escritores que se sabe que existen (si dejara de verlos,
    # estaría ciego, no arreglado).
    for fn in (db._buscar_o_crear, db.convertir_tarea_en_proyecto, crud.perfil):
        assert tr._id_de(fn) in legibles, (
            f"el censo no ve a {tr._id_de(fn)}: dejó de ver")
    sin_puerta = sorted(q for q, f in legibles.items()
                        if _PUERTA not in _llamadas(f))
    assert not sin_puerta, (
        f"escriben el nombre sin LLAMAR a la puerta (nombrarla no basta): "
        f"{sin_puerta}")
    # Los genéricos: los mismos que ya conoce `test_responsable` (editar y
    # deshacer). Uno nuevo se pone rojo acá hasta que se le dé sonda.
    genericos = set(censo["genericos"])
    assert {tr._id_de(crud.editar), tr._id_de(crud.deshacer)} <= genericos
    assert genericos == {tr._id_de(fn) for fn in tr.SONDAS}, (
        f"escritores genéricos que este censo y el de responsable no ven igual: "
        f"{sorted(genericos ^ {tr._id_de(fn) for fn in tr.SONDAS})}")
    # Y TODOS (legibles y genéricos) miran duplicados con LA función única, no
    # con un SELECT propio: `convertir_tarea_en_proyecto`, `_buscar_o_crear` y
    # `perfil` buscan; `editar` y `deshacer` comprueban. Los cinco la nombran.
    escritores = {**legibles,
                  tr._id_de(crud.editar): censo["genericos"][tr._id_de(crud.editar)],
                  tr._id_de(crud.deshacer): censo["genericos"][tr._id_de(crud.deshacer)]}
    sin_unica = sorted(q for q, f in escritores.items()
                       if _UNICA not in _llamadas(f))
    assert not sin_unica, f"no LLAMAN a {_UNICA}: {sin_unica}"
    # Los genéricos llegan a la puerta por `crud._por_las_puertas`.
    for fn in (crud.editar, crud.deshacer):
        assert crud._por_las_puertas.__name__ in _llamadas(
            censo["genericos"][tr._id_de(fn)]), (
            f"{fn.__name__} no llama a _por_las_puertas")


def test_la_puerta_de_crud_es_la_funcion_del_nombre_y_no_otra_copia():
    assert crud.PUERTAS["proyectos"]["nombre"] is db.nombre_de_proyecto_que_vale


def test_la_funcion_del_nombre_con_entradas_inventadas():
    ok = db.nombre_de_proyecto_que_vale
    assert ok("  a  ") == "a" and ok("x" * LARGO) == "x" * LARGO
    for malo in (None, "", "   ", "\n\t", 5, "x" * (LARGO + 1)):
        try:
            ok(malo)
        except db.NombreDeProyectoNoVale as e:
            assert e.clave in ("vacio", "largo")
        else:
            raise AssertionError(f"aceptó {malo!r}")


# ── H3: el duplicado solo se revisa si el nombre CAMBIA ──────────────────

def test_reenviar_el_mismo_nombre_con_otro_campo_no_se_rechaza_por_un_duplicado_viejo():
    """Caso del testigo: «Dup» y «dup» ya estaban vivos (de antes de la regla).
    Editar «Dup» pidiendo el nombre que ya tiene MÁS otro campo: el nombre no
    cambia, así que ni se escribe ni se revisa; el otro campo sí se guarda."""
    b = Base()
    a = b.proyecto("Dup")
    b.proyecto("dup")
    despues, log_id = _correr(b, lambda: crud.editar(
        "proyectos", a, {"nombre": "Dup", "estado": "pausado"}, motivo="x"))
    assert log_id is not None, "rechazó o no escribió el otro campo"
    assert b.updates() == ["UPDATE proyectos SET estado = %s WHERE id = %s"], (
        f"tocó el nombre sin que cambiara: {b.updates()}")
    assert b.con.execute("SELECT estado FROM proyectos WHERE id=?",
                         (a,)).fetchone()[0] == "pausado"
    assert b.nombre_de(a) == "Dup"


def test_cambiar_el_nombre_a_un_duplicado_junto_con_otro_campo_si_se_rechaza():
    b = Base()
    a = b.proyecto("Uno")
    b.proyecto("Otro")
    e = _rechazo(b, lambda: crud.editar(
        "proyectos", a, {"nombre": "otro", "estado": "pausado"}, motivo="x"))
    assert isinstance(e, ValueError)
    assert b.updates() == [] and b.nombre_de(a) == "Uno"
    assert b.con.execute("SELECT estado FROM proyectos WHERE id=?",
                         (a,)).fetchone()[0] == "activo", "escribió a medias"


# ── H2: lo que le dice el agente de Telegram a Lucy ──────────────────────

def _herramienta(b, nombre, args):
    acciones: list = []
    resultado = _correr(b, lambda: agente._ejecutar_herramienta(
        nombre, args, 1, acciones))
    return resultado, acciones


def test_el_agente_no_dice_editado_cuando_no_se_escribio_nada():
    b = Base()
    p = b.proyecto("Casa")
    r, acciones = _herramienta(b, "editar", {
        "tabla": "proyectos", "id": p, "cambios": {"nombre": "Casa"}})
    assert r.startswith("SIN CAMBIOS"), r
    assert "#None" not in r and not r.startswith("OK"), r
    assert acciones == [], "anotó una acción que no existe"
    assert b.updates() == [] and b.huellas() == []


def test_el_agente_si_confirma_una_edicion_que_de_verdad_escribio():
    b = Base()
    p = b.proyecto("Casa")
    r, acciones = _herramienta(b, "editar", {
        "tabla": "proyectos", "id": p, "cambios": {"nombre": "Casa nueva"}})
    assert re.match(r"OK: editado \(acción #\d+, reversible\)\.", r), r
    assert len(acciones) == 1 and acciones[0]["log_id"] > 0


def test_ninguna_herramienta_del_agente_devuelve_accion_none():
    """Las hermanas que arman «acción #{log_id}»: se corren las que pueden
    terminar sin escribir. Ninguna puede decir «#None»."""
    b = Base()
    p = b.proyecto("Casa")
    casos = [
        ("editar", {"tabla": "proyectos", "id": p, "cambios": {"nombre": "Casa"}}),
        ("editar", {"tabla": "proyectos", "id": 9999, "cambios": {"nombre": "X"}}),
        ("archivar", {"tabla": "proyectos", "id": 9999}),
        ("perfil", {"tipo": "proyecto", "nombre": "Casa"}),
    ]
    for herramienta, args in casos:
        r, _ = _herramienta(b, herramienta, args)
        assert "#None" not in r, f"{herramienta} {args}: {r}"


# ── H1: convertir una tarea en proyecto dice su motivo REAL ──────────────

def _convertir(b, tid):
    return _correr(b, lambda: panel.convertir_en_proyecto(base._post({}, True), tid))


def test_convertir_rechazado_por_el_nombre_dice_el_motivo_real_no_el_de_siempre():
    casos = [("PROYECTO YA HECHO", "convertir_repetido"),
             ("t" * (LARGO + 1), "convertir_largo")]
    for titulo, clave in casos:
        b = Base()
        b.proyecto("Proyecto Ya Hecho")
        b.con.execute("INSERT INTO tareas (id, titulo, bandeja_id) VALUES (7, ?, 1)",
                      (titulo,))
        r = _convertir(b, 7)
        assert r.headers["location"] == f"/tareas/7?error={clave}", (
            f"{clave}: {r.headers['location']}")
    # Y lo que de verdad es «no califica» sigue con su clave de siempre.
    b = Base()
    b.con.execute("INSERT INTO tareas (id, titulo, estado, bandeja_id) "
                  "VALUES (8, 'algo', 'hecha', 1)")
    assert _convertir(b, 8).headers["location"] == "/tareas/8?error=convertir"


def test_cada_motivo_que_la_puerta_puede_dar_tiene_su_mensaje_en_la_pantalla():
    """Las claves de rechazo salen del CÓDIGO (las que pasa `db.py` a
    `NombreDeProyectoNoVale`), no de una lista escrita acá; cada una tiene su
    propio aviso en el detalle de la tarea, distinto del genérico."""
    fuente = Path(base.RAIZ, "db", "db.py").read_text(encoding="utf-8")
    claves = set()
    for n in ast.walk(ast.parse(fuente)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "NombreDeProyectoNoVale"
                and isinstance(n.args[0], ast.Constant)):
            claves.add(n.args[0].value)
    assert claves >= {"vacio", "largo", "repetido"}, claves
    plantilla = Path(base.RAIZ, "web", "plantillas",
                     "tarea_detalle.html").read_text(encoding="utf-8")
    mensajes = {}
    for clave in claves:
        m = re.search(r"error == 'convertir_%s' %%\}\s*<p class=\"aviso\">(.*?)</p>"
                      % clave, plantilla, re.S)
        assert m, f"convertir_{clave} no tiene aviso en tarea_detalle.html"
        mensajes[clave] = " ".join(m.group(1).split())
    generico = re.search(r"error == 'convertir' %\}\s*<p class=\"aviso\">(.*?)</p>",
                         plantilla, re.S).group(1)
    assert len(set(mensajes.values())) == len(mensajes), "avisos repetidos"
    assert " ".join(generico.split()) not in mensajes.values()
    assert "ya hay otro proyecto" in mensajes["repetido"]


# ── El mensaje dice que el problema es el NOMBRE DEL PROYECTO ────────────

def test_todo_rechazo_del_nombre_dice_que_es_el_nombre_del_proyecto():
    largo = "n" * (LARGO + 1)
    b = Base()
    b.proyecto("Casa")
    p = b.proyecto("Otra")
    mensajes = [
        str(_rechazo(b, _editar(p, largo))),
        str(_rechazo(b, _editar(p, ""))),
        str(_rechazo(b, _editar(p, "casa"))),
        str(_rechazo(b, lambda: db.buscar_o_crear_proyecto(largo))),
        str(_rechazo(b, lambda: crud.perfil("proyecto", largo, nota="x"))),
    ]
    for m in mensajes:
        assert "proyecto" in m.lower(), f"el mensaje no dice que es el proyecto: {m}"
    # Por la herramienta de Telegram, tal como lo lee Lucy.
    r, _ = _herramienta(b, "editar", {
        "tabla": "proyectos", "id": p, "cambios": {"nombre": largo}})
    assert r.startswith("ERROR") and "nombre del proyecto" in r, r


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
