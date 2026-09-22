# -*- coding: utf-8 -*-
"""Los proyectos se ven (encargo 5): página de proyectos, botón «convertir en
proyecto», huella de `buscar_o_crear_proyecto`, y cambiar el área de una tarea
o un proyecto desde el panel.

Correr:  python3 -m pytest tests/test_proyectos_panel.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object()
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

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = timezone.utc
# NO se hardcodea 424242: `config.CHAT_ID_DUENO` se fija UNA vez por proceso,
# al primer `import config` -- y `tests/test_crud_dedup.py` corre antes en la
# suite completa y pide "1" con su propio `os.environ.setdefault`. El
# `setdefault` de acá arriba no gana esa carrera si ese archivo ya se
# importó. Leerlo de `config` en vez de tecleado es lo mismo que ya hacen
# `test_tarea_a_mano.py` y `test_comentarios_de_tareas.py`, y por la misma
# razón: medido, `DUENO = 424242` a mano hacía que `auth.puede_entrar`
# devolviera False y las rutas del panel respondieran 401 en vez de 303,
# SOLO cuando esta prueba corría junto con el resto de la suite.
DUENO = config.CHAT_ID_DUENO


def _split_top_level(texto: str) -> list[str]:
    """Parte por comas, salvo las que están DENTRO de una cadena
    entrecomillada. Un `motivo` literal como 'Proyecto nuevo, nombrado al
    crear una tarea' trae una coma propia -- un `.split(",")` a secas lo
    parte por la mitad y desalinea todo lo que viene después."""
    partes, actual, entre_comillas = [], [], False
    for c in texto:
        if c == "'":
            entre_comillas = not entre_comillas
        if c == "," and not entre_comillas:
            partes.append("".join(actual).strip())
            actual = []
        else:
            actual.append(c)
    partes.append("".join(actual).strip())
    return partes


# ── Una base de mentira para `db.convertir_tarea_en_proyecto` ────────────

class _CursorProyectos:
    def __init__(self, conn):
        self._conn = conn
        self._fila = None

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._conn.sql.append((s, p))

        if s.startswith("SELECT * FROM tareas WHERE id"):
            tid = p[0]
            self._fila = next(
                (dict(t) for t in self._conn.tareas
                 if t["id"] == tid and t.get("borrado_en") is None), None)

        elif s.startswith("INSERT INTO proyectos"):
            self._conn.sig_proyecto += 1
            nombre, descripcion, area = p
            fila = {"id": self._conn.sig_proyecto, "nombre": nombre,
                    "descripcion": descripcion, "area": area,
                    "estado": "activo", "borrado_en": None,
                    "creado_en": datetime(2026, 9, 22, tzinfo=UTC)}
            self._conn.proyectos.append(fila)
            self._fila = fila

        elif s.startswith("INSERT INTO log_acciones"):
            self._conn.sig_log += 1
            # Las columnas Y los valores se leen del propio SQL: algunos
            # valores son `%s` (vienen de `p`, en orden) y otros son
            # literales escritos en el SQL (`'panel'`, `'crear'`, `NULL`) --
            # `db.convertir_tarea_en_proyecto` mezcla las dos formas en la
            # MISMA sentencia, así que no alcanza con `zip(columnas, p)`.
            m = re.search(
                r"log_acciones\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", s,
                re.IGNORECASE)
            columnas = _split_top_level(m.group(1))
            crudos = _split_top_level(m.group(2))
            it = iter(p)
            fila = {}
            for col, val in zip(columnas, crudos):
                if val == "%s":
                    fila[col] = next(it)
                elif val.upper() == "NULL":
                    fila[col] = None
                else:
                    fila[col] = val.strip("'\"")
            fila["id"] = self._conn.sig_log
            self._conn.logs.append(fila)
            self._fila = {"id": self._conn.sig_log}

        elif s.startswith("UPDATE tareas SET borrado_en"):
            tid = p[0]
            for t in self._conn.tareas:
                if t["id"] == tid:
                    t["borrado_en"] = datetime(2026, 9, 22, tzinfo=UTC)
            self._fila = None

        else:
            raise AssertionError(f"SQL no modelado por _CursorProyectos: {s[:90]}")
        return self

    async def fetchone(self):
        return self._fila


class _ConnProyectos:
    def __init__(self, tareas=None):
        self.tareas = [dict(t) for t in (tareas or [])]
        self.proyectos: list[dict] = []
        self.logs: list[dict] = []
        self.sql: list = []
        self.sig_proyecto = 100
        self.sig_log = 5000

    def cursor(self, row_factory=None):
        return _CursorProyectos(self)

    async def execute(self, sql, params=None):
        return await _CursorProyectos(self).execute(sql, params)


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


def _fila_tarea(**extra):
    fila = {"id": 40, "bandeja_id": 900, "titulo": "Álbum nuevo",
            "detalle": "grabar el disco completo", "estado": "pendiente",
            "vence_en": None, "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "responsable_chat_id": None, "proyecto_id": None, "area": "CDS",
            "borrado_en": None}
    fila.update(extra)
    return fila


# ── `db.convertir_tarea_en_proyecto` ──────────────────────────────────────

def test_convertir_crea_el_proyecto_con_nombre_detalle_y_area_de_la_tarea():
    conn = _ConnProyectos([_fila_tarea()])
    resultado = _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))

    assert len(conn.proyectos) == 1
    p = conn.proyectos[0]
    assert p["nombre"] == "Álbum nuevo", "el nombre del proyecto no es el título"
    assert p["descripcion"] == "grabar el disco completo", (
        "la descripción del proyecto no es el detalle de la tarea")
    assert p["area"] == "CDS", "el proyecto no heredó el área de la tarea"
    assert resultado["proyecto_id"] == p["id"]
    assert resultado["proyecto_nombre"] == "Álbum nuevo"


def test_convertir_archiva_la_tarea_original_y_no_la_muda_al_proyecto():
    conn = _ConnProyectos([_fila_tarea()])
    _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))

    tarea = next(t for t in conn.tareas if t["id"] == 40)
    assert tarea["borrado_en"] is not None, "la tarea original no se archivó"
    # Y no se creó como la PRIMERA tarea del proyecto -- no hay ningún INSERT
    # INTO tareas en todo el SQL ejecutado, solo el UPDATE que la archiva.
    inserts_tareas = [s for s, _ in conn.sql if s.startswith("INSERT INTO tareas")]
    assert not inserts_tareas, (
        f"la tarea original no debía re-crearse dentro del proyecto: {inserts_tareas}")


def test_convertir_rechaza_una_tarea_que_ya_tiene_proyecto():
    conn = _ConnProyectos([_fila_tarea(proyecto_id=7)])
    try:
        _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))
        assert False, "debió rechazar: esa tarea ya tiene proyecto"
    except ValueError as e:
        assert "proyecto" in str(e).lower()
    assert conn.proyectos == [], "no debió crear ningún proyecto"
    tarea = next(t for t in conn.tareas if t["id"] == 40)
    assert tarea["borrado_en"] is None, "no debió archivar la tarea"


def test_convertir_rechaza_una_tarea_que_no_esta_pendiente():
    conn = _ConnProyectos([_fila_tarea(estado="hecha")])
    try:
        _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))
        assert False, "debió rechazar: esa tarea no está pendiente"
    except ValueError as e:
        assert "pendiente" in str(e).lower()
    assert conn.proyectos == []


def test_convertir_rechaza_una_tarea_que_no_existe():
    conn = _ConnProyectos([])
    try:
        _con_base(conn, lambda: db.convertir_tarea_en_proyecto(999))
        assert False, "debió rechazar: esa tarea no existe"
    except ValueError:
        pass
    assert conn.proyectos == []


def test_convertir_deja_dos_huellas_independientes_y_deshaceables():
    """Una `crear` sobre `proyectos` y una `borrar` sobre `tareas`, las DOS
    con la forma que `deshacer()` ya sabe revertir -- nada de un tercer tipo
    de acción inventado."""
    conn = _ConnProyectos([_fila_tarea()])
    resultado = _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))

    huella_proyecto = next(
        l for l in conn.logs if l["id"] == resultado["log_id_proyecto"])
    huella_tarea = next(
        l for l in conn.logs if l["id"] == resultado["log_id_tarea"])
    assert huella_proyecto["accion"] == "crear"
    assert huella_proyecto["tabla"] == "proyectos"
    assert huella_proyecto["registro_id"] == resultado["proyecto_id"]
    assert huella_tarea["accion"] == "borrar"
    assert huella_tarea["tabla"] == "tareas"
    assert huella_tarea["registro_id"] == 40
    assert huella_tarea["antes"] is not None, (
        "el 'antes' de la tarea archivada tiene que guardarse completo -- "
        "es lo que deshacer() usa para revertir un 'borrar'")
    for h in (huella_proyecto, huella_tarea):
        assert h["actor"] == "panel"


def test_convertir_conserva_responsable_y_comentarios_en_la_tarea_archivada():
    """El responsable no tiene a dónde ir en `proyectos` -- no existe esa
    columna ahí -- así que se queda en la fila archivada, no se pierde."""
    conn = _ConnProyectos([_fila_tarea(responsable_chat_id=DUENO)])
    _con_base(conn, lambda: db.convertir_tarea_en_proyecto(40))
    tarea = next(t for t in conn.tareas if t["id"] == 40)
    assert tarea["responsable_chat_id"] == DUENO, (
        "el responsable se perdió al convertir -- no había dónde guardarlo "
        "en el registro si esto fallara en silencio")


# ── La huella de `buscar_o_crear_proyecto` (encargo 5, requisito 4) ──────

class _CursorBuscarOCrear:
    def __init__(self, conn):
        self._conn = conn
        self._fila = None

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._conn.sql.append((s, p))

        if s.startswith("SELECT id FROM proyectos"):
            nombre = p[0]
            self._fila = next(
                ({"id": pr["id"]} for pr in self._conn.proyectos
                 if pr["nombre"].lower() == nombre.lower()), None)

        elif s.startswith("INSERT INTO proyectos (nombre)"):
            self._conn.sig += 1
            fila = {"id": self._conn.sig, "nombre": p[0]}
            self._conn.proyectos.append(fila)
            self._fila = fila

        elif s.startswith("INSERT INTO log_acciones"):
            m = re.search(
                r"log_acciones\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", s,
                re.IGNORECASE)
            columnas = _split_top_level(m.group(1))
            crudos = _split_top_level(m.group(2))
            it = iter(p)
            fila = {}
            for col, val in zip(columnas, crudos):
                fila[col] = (next(it) if val == "%s"
                             else None if val.upper() == "NULL"
                             else val.strip("'\""))
            self._conn.logs.append(fila)
            self._fila = None

        else:
            raise AssertionError(
                f"SQL no modelado por _CursorBuscarOCrear: {s[:90]}")
        return self

    async def fetchone(self):
        return self._fila


class _ConnBuscarOCrear:
    def __init__(self, proyectos=None):
        self.proyectos = [dict(p) for p in (proyectos or [])]
        self.logs: list = []
        self.sql: list = []
        self.sig = 200

    def cursor(self, row_factory=None):
        return _CursorBuscarOCrear(self)

    async def execute(self, sql, params=None):
        return await _CursorBuscarOCrear(self).execute(sql, params)


def test_buscar_o_crear_proyecto_deja_huella_al_crear_uno_nuevo():
    conn = _ConnBuscarOCrear([])
    pid = _con_base(conn, lambda: db.buscar_o_crear_proyecto(
        "Renovar el estudio", bandeja_id=77))

    assert len(conn.proyectos) == 1 and conn.proyectos[0]["id"] == pid
    assert len(conn.logs) == 1, (
        "crear un proyecto nuevo nombrándolo al vuelo tiene que dejar huella "
        "en el registro -- hallazgo del encargo 5: 3 de 4 proyectos de "
        "producción no tenían rastro")
    huella = conn.logs[0]
    assert huella["accion"] == "crear"
    assert huella["tabla"] == "proyectos"
    assert huella["registro_id"] == pid
    assert huella["bandeja_id"] == 77
    assert huella["actor"] == "lucy", (
        "esto lo dispara siempre una interpretación de Telegram, nunca el panel")


def test_buscar_o_crear_proyecto_no_deja_huella_si_ya_existia():
    conn = _ConnBuscarOCrear([{"id": 55, "nombre": "Renovar el estudio"}])
    pid = _con_base(conn, lambda: db.buscar_o_crear_proyecto(
        "renovar el estudio"))  # minúscula: mismo match insensible

    assert pid == 55, "debió encontrar el proyecto existente, no crear otro"
    assert len(conn.proyectos) == 1
    assert conn.logs == [], "no se creó nada, no hay nada que registrar"


# ── `db.proyectos_vivos` / `db.proyectos_con_tareas` ──────────────────────

class _CursorListas:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._conn.sql.append((s, p))
        if "FROM proyectos" in s and "tareas" not in s.split("FROM")[0]:
            self._filas = [dict(pr) for pr in self._conn.proyectos]
        elif s.strip().startswith("SELECT id, proyecto_id, titulo"):
            ids = set(p[0])
            self._filas = [dict(t) for t in self._conn.tareas
                           if t["proyecto_id"] in ids]
        else:
            raise AssertionError(f"SQL no modelado por _CursorListas: {s[:90]}")
        return self

    async def fetchall(self):
        return self._filas


class _ConnListas:
    def __init__(self, proyectos=None, tareas=None):
        self.proyectos = [dict(p) for p in (proyectos or [])]
        self.tareas = [dict(t) for t in (tareas or [])]
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CursorListas(self)


def test_proyectos_vivos_devuelve_lo_que_hay_sin_filtrar_por_estado():
    conn = _ConnListas(proyectos=[
        {"id": 1, "nombre": "Activo", "area": "CDS", "estado": "activo"},
        {"id": 2, "nombre": "Pausado", "area": None, "estado": "pausado"}])
    datos = _con_base(conn, lambda: db.proyectos_vivos())
    assert {p["nombre"] for p in datos} == {"Activo", "Pausado"}, (
        "un proyecto pausado tiene que seguir viéndose: 'vivo' es "
        "borrado_en IS NULL, no estado == activo")


def test_proyectos_con_tareas_reparte_cada_tarea_bajo_su_proyecto():
    conn = _ConnListas(
        proyectos=[{"id": 1, "nombre": "A", "descripcion": None,
                   "estado": "activo", "area": None, "color": None},
                  {"id": 2, "nombre": "B", "descripcion": None,
                   "estado": "activo", "area": None, "color": None}],
        tareas=[{"id": 10, "proyecto_id": 1, "titulo": "de A",
                "estado": "pendiente", "vence_en": None, "completado_en": None},
               {"id": 11, "proyecto_id": 2, "titulo": "de B",
                "estado": "pendiente", "vence_en": None, "completado_en": None}])
    datos = _con_base(conn, lambda: db.proyectos_con_tareas())
    por_nombre = {p["nombre"]: [t["titulo"] for t in p["tareas"]] for p in datos}
    assert por_nombre == {"A": ["de A"], "B": ["de B"]}, (
        f"una tarea apareció bajo el proyecto equivocado, o no apareció: "
        f"{por_nombre}")


# ── Las rutas del panel: MISMA puerta que Telegram, sin criterio propio ──
#
# En vez de otra base de mentira completa, se ESPÍA `crud.editar` (como ya
# hace `tests/test_comentarios_de_tareas.py` con otras funciones de `db`):
# lo que hace falta probar acá es que la RUTA llame a la puerta correcta, con
# los argumentos correctos -- no repetir la prueba de qué hace la puerta por
# dentro, que ya está en `tests/test_crud_dedup.py`.

from urllib.parse import urlencode  # noqa: E402


def _peticion(metodo, ruta, campos=None, chat=DUENO):
    from starlette.requests import Request

    cuerpo = urlencode(campos or {}).encode()
    cabeceras = [(b"host", b"t"),
                 (b"content-type", b"application/x-www-form-urlencoded"),
                 (b"content-length", str(len(cuerpo)).encode())]
    if chat is not None:
        galleta = f"{panel.COOKIE}={auth.crear_token(chat, auth.VIDA_SESION)}"
        cabeceras.append((b"cookie", galleta.encode()))

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": metodo,
                    "scheme": "https", "server": ("t", 443), "path": ruta,
                    "root_path": "", "query_string": b"", "headers": cabeceras,
                    "app": panel.app}, recibir)


def _llamar(coro_fn):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro_fn())
    finally:
        bucle.close()


def test_cambiar_area_de_tarea_llama_a_la_misma_puerta_que_telegram():
    llamadas = []

    async def _editar_espia(tabla, rid, cambios, motivo, actor="lucy"):
        llamadas.append((tabla, rid, cambios, actor))
        return {"id": rid, "area": cambios.get("area")}, 999

    guardado = panel.crud.editar
    panel.crud.editar = _editar_espia
    try:
        r = _llamar(lambda: panel.cambiar_area_de_tarea(
            _peticion("POST", "/tareas/5/area", {"area": "CDS"}), 5))
    finally:
        panel.crud.editar = guardado

    assert llamadas == [("tareas", 5, {"area": "CDS"}, "panel")], (
        f"la ruta no llamó a crud.editar con los argumentos esperados: {llamadas}")
    assert r.status_code == 303
    assert r.headers["location"] == "/tareas/5?area_guardada=1"


def test_cambiar_area_de_tarea_redirige_con_error_si_la_puerta_rechaza():
    """Si `_area_que_vale` rechaza -- área que no existe, o cualquier otro
    motivo -- la ruta NO inventa su propio criterio: repite lo que dijo la
    puerta, solo traducido a una redirección con `?error=`."""
    async def _editar_rechaza(tabla, rid, cambios, motivo, actor="lucy"):
        raise ValueError("'Marketing' no es un área. Son: \"CDS\"")

    guardado = panel.crud.editar
    panel.crud.editar = _editar_rechaza
    try:
        r = _llamar(lambda: panel.cambiar_area_de_tarea(
            _peticion("POST", "/tareas/5/area", {"area": "Marketing"}), 5))
    finally:
        panel.crud.editar = guardado

    assert r.status_code == 303
    assert r.headers["location"] == "/tareas/5?error=area"


def test_cambiar_area_de_proyecto_llama_a_la_misma_puerta_que_telegram():
    llamadas = []

    async def _editar_espia(tabla, rid, cambios, motivo, actor="lucy"):
        llamadas.append((tabla, rid, cambios, actor))
        return {"id": rid, "area": cambios.get("area")}, 999

    guardado = panel.crud.editar
    panel.crud.editar = _editar_espia
    try:
        r = _llamar(lambda: panel.cambiar_area_de_proyecto(
            _peticion("POST", "/proyectos/3/area", {"area": "ACD"}), 3))
    finally:
        panel.crud.editar = guardado

    assert llamadas == [("proyectos", 3, {"area": "ACD"}, "panel")], (
        f"la ruta no llamó a crud.editar con los argumentos esperados: {llamadas}")
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos?area_guardada=3"


def test_convertir_en_proyecto_llama_a_db_convertir_y_redirige_al_nuevo():
    llamadas = []

    async def _convertir_espia(tid):
        llamadas.append(tid)
        return {"proyecto_id": 42, "proyecto_nombre": "Álbum nuevo",
                "log_id_proyecto": 1, "log_id_tarea": 2}

    guardado = db.convertir_tarea_en_proyecto
    db.convertir_tarea_en_proyecto = _convertir_espia
    try:
        r = _llamar(lambda: panel.convertir_en_proyecto(
            _peticion("POST", "/tareas/40/convertir-en-proyecto"), 40))
    finally:
        db.convertir_tarea_en_proyecto = guardado

    assert llamadas == [40]
    assert r.status_code == 303
    assert r.headers["location"] == "/proyectos?creado=42#proyecto-42"


def test_convertir_en_proyecto_redirige_con_error_si_la_tarea_no_califica():
    async def _convertir_rechaza(tid):
        raise ValueError("Esa tarea ya tiene proyecto: no se convierte.")

    guardado = db.convertir_tarea_en_proyecto
    db.convertir_tarea_en_proyecto = _convertir_rechaza
    try:
        r = _llamar(lambda: panel.convertir_en_proyecto(
            _peticion("POST", "/tareas/40/convertir-en-proyecto"), 40))
    finally:
        db.convertir_tarea_en_proyecto = guardado

    assert r.status_code == 303
    assert r.headers["location"] == "/tareas/40?error=convertir"


def test_proyectos_pide_lo_que_hace_falta_para_pintar_la_pagina():
    """`GET /proyectos` trae `proyectos_con_tareas()` Y `areas()` -- sin la
    segunda, el <select> de cada tarjeta no tendría con qué llenarse."""
    llamados = []

    async def _proyectos_espia():
        llamados.append("proyectos_con_tareas")
        return []

    async def _areas_espia():
        llamados.append("areas")
        return [{"clave": "CDS", "color": "#1"}]

    g1, g2 = db.proyectos_con_tareas, db.areas
    db.proyectos_con_tareas, db.areas = _proyectos_espia, _areas_espia
    try:
        r = _llamar(lambda: panel.proyectos(_peticion("GET", "/proyectos")))
    finally:
        db.proyectos_con_tareas, db.areas = g1, g2

    assert set(llamados) == {"proyectos_con_tareas", "areas"}
    assert r.status_code == 200


# ── DE PUNTA A PUNTA: la ruta del panel hasta `log_acciones` de verdad ────
#
# NO PASA del testigo sobre `43e024e`: los tests de arriba espían
# `crud.editar` ENTERO, así que nunca ejercitan lo que pasa DENTRO -- que el
# `actor='panel'` que la ruta manda de verdad llegue hasta la fila que
# `_registrar` escribe en `log_acciones`. El testigo lo demostró mutando:
# quitar `actor=actor` de la llamada a `_registrar` en `crud.editar` (línea
# ~978) dejó la suite en VERDE. Acá se corre `crud.editar` REAL -- sin
# mockear nada de `acciones.crud` -- desde la ruta del panel hasta el INSERT
# de la huella, y se lee el `actor` que de verdad quedó escrito.

class _CursorEditarPanel:
    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self._conn.sql.append((s, p))
        if s.startswith("SELECT clave, color FROM areas"):
            self._filas = list(self._conn.areas)
        elif s.startswith("SELECT * FROM"):
            self._row = dict(self._conn.fila)
        else:
            raise AssertionError(
                f"SQL no modelado por _CursorEditarPanel: {s[:90]}")
        return self

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._filas


class _ConnEditarPanel:
    """Modela lo justo para que `crud.editar` corra ENTERO -- validación por
    `_area_que_vale` (que abre su PROPIA conexión vía `db.areas()`, de ahí el
    `.transaction()` y el `SELECT clave, color FROM areas`), el `UPDATE`, y
    el `INSERT INTO log_acciones` de `_registrar`, del que se lee el `actor`
    que de verdad quedó -- no uno espiado por fuera de la función."""

    def __init__(self, fila, areas=None):
        self.fila = dict(fila)
        self.areas = list(areas or [])
        self.logs: list[dict] = []
        self.sql: list = []
        self._logid = 9000

    def cursor(self, row_factory=None):
        return _CursorEditarPanel(self)

    def transaction(self):
        return _TransaccionPanel()

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self.sql.append((s, p))
        if s.startswith("UPDATE"):
            asignaciones = s.split(" SET ", 1)[1].split(" WHERE ")[0]
            columnas = [a.split("=")[0].strip() for a in asignaciones.split(", ")]
            self.fila.update(dict(zip(columnas, p)))
            return _CursorEditarPanel(self)
        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            # Todas las columnas de `_registrar` viajan como `%s` (ya no hay
            # un `'lucy'` literal desde que `actor` es parámetro) -- por eso
            # alcanza con `zip` posicional, sin distinguir literal de
            # placeholder como en `db.py`.
            m = re.search(r"log_acciones\s*\(([^)]*)\)", s)
            columnas = [c.strip() for c in m.group(1).split(",")]
            fila = dict(zip(columnas, p))
            fila["id"] = self._logid
            self.logs.append(fila)
            cur = _CursorEditarPanel(self)
            cur._row = (self._logid,)
            return cur
        raise AssertionError(f"SQL no modelado por _ConnEditarPanel: {s[:90]}")


class _TransaccionPanel:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _CMPanel:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *e):
        return False


class _PoolPanel:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _CMPanel(self._conn)


def test_cambiar_area_de_tarea_de_punta_a_punta_queda_con_actor_panel():
    """Corre la RUTA real -> `crud.editar` real -> `_registrar` real. Lo que
    se mide es la fila que de verdad quedó en `log_acciones`, no lo que la
    ruta le pasó a un espía."""
    conn = _ConnEditarPanel(
        _fila_tarea_area_panel(area=None),
        areas=[{"clave": "CDS", "color": "#1"}])
    guardado = db.pool
    db.pool = _PoolPanel(conn)
    try:
        r = _llamar(lambda: panel.cambiar_area_de_tarea(
            _peticion("POST", "/tareas/40/area", {"area": "CDS"}), 40))
    finally:
        db.pool = guardado

    assert r.status_code == 303
    assert conn.fila["area"] == "CDS", "el área no quedó escrita de verdad"
    assert len(conn.logs) == 1, (
        f"no quedó ninguna huella en log_acciones: {conn.sql}")
    huella = conn.logs[0]
    assert huella["accion"] == "editar"
    assert huella["tabla"] == "tareas"
    assert huella["actor"] == "panel", (
        f"la huella quedó con actor={huella['actor']!r} en vez de 'panel' -- "
        "un cambio de área hecho desde el panel se vería en el registro "
        "como si lo hubiera hecho Lucy por Telegram")


def test_cambiar_area_de_proyecto_de_punta_a_punta_queda_con_actor_panel():
    conn = _ConnEditarPanel(
        _fila_proyecto_area_panel(area=None),
        areas=[{"clave": "ACD", "color": "#2"}])
    guardado = db.pool
    db.pool = _PoolPanel(conn)
    try:
        r = _llamar(lambda: panel.cambiar_area_de_proyecto(
            _peticion("POST", "/proyectos/5/area", {"area": "ACD"}), 5))
    finally:
        db.pool = guardado

    assert r.status_code == 303
    assert conn.fila["area"] == "ACD"
    assert len(conn.logs) == 1, (
        f"no quedó ninguna huella en log_acciones: {conn.sql}")
    huella = conn.logs[0]
    assert huella["accion"] == "editar"
    assert huella["tabla"] == "proyectos"
    assert huella["actor"] == "panel", (
        f"la huella quedó con actor={huella['actor']!r} en vez de 'panel'")


def _fila_tarea_area_panel(**extra):
    fila = {"id": 40, "bandeja_id": 1, "titulo": "Mezclar el tema",
            "detalle": None, "vence_en": None, "estado": "pendiente",
            "recurrencia": None, "pospuesta_veces": 0, "anticipos_min": [0],
            "avisos_enviados": [], "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "responsable_chat_id": None, "proyecto_id": None, "area": None}
    fila.update(extra)
    return fila


def _fila_proyecto_area_panel(**extra):
    fila = {"id": 5, "nombre": "Álbum nuevo", "descripcion": None,
            "estado": "activo", "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, tzinfo=UTC), "area": None}
    fila.update(extra)
    return fila


# ── Hermanos: otros sitios que escriben `log_acciones` con un `actor` que
# viene de afuera de la función que escribe ────────────────────────────────
#
# SEGUNDA VUELTA (NO PASA del testigo sobre `25f7d0b`): la primera versión de
# esta prueba SOLO miraba `acciones/crud.py`, tecleado con un `import`
# directo. El comentario y el docstring decían "junto con db/db.py", pero el
# código nunca lo abría -- el testigo lo demostró agregando una función
# nueva en `db/db.py` con `actor` como variable, fuera de `_registrar`, y la
# prueba siguió en VERDE.
#
# Ahora la lista de módulos sale de lo real: TODO `.py` del repo, fuera de
# `tests/`, que contenga el texto `INSERT INTO log_acciones` -- con el MISMO
# barrido que ya usa `tests/test_responsable.py::_censo`
# (`test_buzon_que_no_se_ve._py_en_disco` / `_testpaths`, que a su vez
# pregunta a cada carpeta qué es -- venv, caché, `testpaths` de pytest.ini --
# en vez de tener una lista de nombres). No hace falta reinventar ese barrido
# ni tecleado uno nuevo.
#
# Por cada `INSERT INTO log_acciones` se leen sus COLUMNAS y sus VALORES del
# propio SQL: si el valor que le toca a `actor` es un `%s` (una VARIABLE que
# viaja como parámetro), ese sitio tiene que ser `acciones/crud.py::_registrar`
# -- cualquier otro es un lugar nuevo donde `actor` se puede perder en
# silencio, igual que pasó en el NO PASA anterior. Si el valor es un literal
# (`'panel'`, `'lucy'`, como en TODOS los `INSERT` de `db/db.py` hoy: ver
# `db.crear_tarea_desde_el_panel`, `db.convertir_tarea_en_proyecto`,
# `db._buscar_o_crear`), no hay parámetro que perder -- el texto ES el
# valor -- así que no hace falta que pase por `_registrar`.

def test_todo_actor_variable_en_log_acciones_pasa_por_registrar():
    """Deriva del disco -- no de una lista de dos nombres -- todo módulo que
    escribe en `log_acciones`, y exige que el ÚNICO sitio donde `actor` viaja
    como parámetro (no como literal) sea `acciones/crud.py::_registrar`."""
    import ast
    import re
    from pathlib import Path

    import test_buzon_que_no_se_ve as barrido

    raiz = Path(RAIZ).resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]

    def _columnas_y_valores(sql: str):
        m = re.search(
            r"log_acciones\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", sql,
            re.IGNORECASE | re.DOTALL)
        if not m:
            return None, None
        return (_split_top_level(m.group(1)), _split_top_level(m.group(2)))

    def _funcion_de(nodo, arbol):
        padre = None
        for f in ast.walk(arbol):
            if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(n is nodo for n in ast.walk(f))):
                if padre is None or len(ast.dump(f)) < len(ast.dump(padre)):
                    padre = f
        return padre.name if padre else "<módulo>"

    sitios_con_actor_variable = []
    modulos_vistos = []
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue  # los archivos de test no cuentan como "código real"
        texto = real.read_text(encoding="utf-8")
        if "INSERT INTO log_acciones" not in texto:
            continue
        modulos_vistos.append(real.relative_to(raiz).as_posix())
        arbol = ast.parse(texto, str(real))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            if not (isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "execute"):
                continue
            for arg in nodo.args:
                if not (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and "INSERT INTO log_acciones" in arg.value):
                    continue
                sql = " ".join(arg.value.split())
                columnas, valores = _columnas_y_valores(sql)
                if columnas is None or "actor" not in columnas:
                    continue
                i = columnas.index("actor")
                valor_actor = valores[i] if i < len(valores) else ""
                if valor_actor != "%s":
                    continue  # literal: no hay parámetro que perder
                nombre_funcion = _funcion_de(nodo, arbol)
                ruta = real.relative_to(raiz).as_posix()
                if not (ruta == "acciones/crud.py"
                        and nombre_funcion == "_registrar"):
                    sitios_con_actor_variable.append(f"{ruta}::{nombre_funcion}")

    assert "acciones/crud.py" in modulos_vistos, (
        "el barrido no encontró acciones/crud.py -- la prueba dejó de medir "
        f"algo. Módulos vistos: {modulos_vistos}")
    assert "db/db.py" in modulos_vistos, (
        "el barrido no encontró db/db.py -- exactamente el hueco que el "
        f"testigo encontró sobre 25f7d0b. Módulos vistos: {modulos_vistos}")
    assert not sitios_con_actor_variable, (
        f"estos sitios escriben 'actor' en log_acciones como VARIABLE, fuera "
        f"de acciones/crud.py::_registrar: {sitios_con_actor_variable} -- "
        f"cada uno es un lugar donde el actor se puede perder en silencio, "
        f"sin que las pruebas de punta a punta de arriba lo vean")
