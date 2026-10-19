# -*- coding: utf-8 -*-
"""Natalia como AUTORA RESERVADA en Lucy (18-oct-2026, pieza L2 del diseño
disenos/natalia-tareas-en-proyectos/DISENO.md, §7).

Regla de Tiziano: «Lucy ahora es la parte del GAL System que se comunica con
nosotros el equipo, Natalia es quien habla con el cliente». Natalia escribe en
Lucy como autora de comentarios, pero NUNCA es responsable ni entra al panel.

TRES GARANTÍAS
  G1  «Natalia» no pasa `puede_ser_responsable`, `puede_recibir_telegram`,
      `personas_del_panel`, `nombres_con_code` ni `web.auth.puede_entrar`.
  G2  su comentario se pinta «Natalia», no «Alguien» / «sin nombre», en los TRES
      sitios que pintan un autor: la página de Proyectos (`armar_pagina`), la
      pantalla de la tarea (`tarea_detalle.html`) y la puerta HTTP de la sala.
  G3  los HERMANOS -- toda función del repo que decide con `nombres_con_code`,
      `puede_ser_responsable` o `_chat_del_nombre` -- siguen rechazándola.

CAMINO DE PRODUCCIÓN: las funciones REALES de `db/db.py`, con su SQL corriendo
de verdad contra SQLite en memoria (`%s` -> `?`), la app FastAPI real y su
plantilla real. Ningún chat ni nombre de este archivo es real (repo PÚBLICO).

LA FRONTERA DE LA LISTA DE HERMANOS (G3): se saca recorriendo con `ast` cada
`.py` del repo fuera de `tests/` y se queda con las funciones que NOMBRAN
`nombres_con_code`, `puede_ser_responsable` o `_chat_del_nombre`. Un hermano que
llegue a esas puertas con un alias (`getattr`, `from config import x as y`,
un `import` dentro de una cadena) no aparece; el barrido de texto de
`test_responsable.py` cubre otra parte del mismo hueco.

Correr:  python3 -m pytest tests/test_natalia_autora_reservada.py -q
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import types
from pathlib import Path
from urllib.parse import urlencode

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-natalia-autora")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object()
_psycopg.rows = _rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
_pool_mod = types.ModuleType("psycopg_pool")
_pool_mod.AsyncConnectionPool = type("_StubPool", (), {"__init__": lambda *a, **k: None})
sys.modules.setdefault("psycopg_pool", _pool_mod)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import acciones.crud as crud  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402
import web.api_code as api_code  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
from datetime import date  # noqa: E402

RAIZ = Path(_ROOT)
DUENO = config.CHAT_ID_DUENO
OTRA = 777002          # una persona de la casa INVENTADA
SIN_NOMBRE = 777003    # un autor que ya no tiene nombre


def _bucle(coro):
    b = asyncio.new_event_loop()
    try:
        return b.run_until_complete(coro)
    finally:
        b.close()


@pytest.fixture
def casa():
    """El dueño y OTRA entran al panel y tienen nombre; devuelve todo al salir."""
    g = (config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT, db.pool)
    config.CHAT_IDS_PERMITIDOS = (DUENO, OTRA)
    config.NOMBRES_POR_CHAT = {DUENO: "Zutana Inventada", OTRA: "Mengano Inventado"}
    try:
        yield
    finally:
        config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT, db.pool = g


# ═══════════════════════════════════════════════════════════════════════
# G1 · «Natalia» no es nadie a quien se le asigne, escriba o deje entrar
# ═══════════════════════════════════════════════════════════════════════

def test_G1_el_valor_reservado_no_choca_con_nadie(casa):
    assert config.CHAT_ID_NATALIA < 0, "un chat real de persona es siempre positivo"
    assert config.CHAT_ID_NATALIA != config.CHAT_ID_CODE
    assert config.NOMBRE_NATALIA == "Natalia"
    assert config.autores_reservados() == {config.CHAT_ID_NATALIA: "Natalia"}


def test_G1_ninguna_puerta_de_responsable_ni_de_entrada_la_deja_pasar(casa):
    n = config.CHAT_ID_NATALIA
    assert config.puede_ser_responsable(n) is False
    assert config.puede_recibir_telegram(n) is False
    assert auth.puede_entrar(n) is False
    assert n not in [c for c, _ in config.personas_del_panel()]
    assert n not in config.nombres_con_code()
    assert "Natalia" not in config.nombres_con_code().values()
    assert "Natalia" not in [e for _, e in config.opciones_de_responsable()]
    assert str(n) not in [v for v, _ in config.opciones_de_responsable()]


def test_G1_ni_por_numero_ni_por_nombre_queda_de_responsable(casa):
    n = config.CHAT_ID_NATALIA
    # El nombre: la puerta de Telegram no la conoce.
    chat, motivo = crud._chat_del_nombre("Natalia")
    assert chat is None and motivo
    chat, motivo = crud._chat_del_nombre(" NATALIA ")
    assert chat is None and motivo
    # La puerta completa que usa el agente.
    for valor in (n, str(n), "Natalia"):
        with pytest.raises(ValueError):
            crud._responsable_que_vale(valor)
    # La puerta de la tabla: ni abre conexión (db.pool es None en este test).
    db.pool = None
    assert _bucle(db.asignar_responsable(1, n)) is False
    # Y comentar: el panel y la sala siguen sin dejarla (eso es L3, no L2).
    assert _bucle(db.comentar_tarea(1, n, "hola")) is None


def test_G1_el_filtro_de_la_lista_tampoco_la_conoce(casa):
    tipo, chat, clave, aviso = panel._filtro_pedido("Natalia")
    assert (tipo, chat, clave, aviso) == ("todas", None, "", True)


# ═══════════════════════════════════════════════════════════════════════
# G2 · Su comentario se pinta «Natalia»
# ═══════════════════════════════════════════════════════════════════════

def _base(comentarios):
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("now", 0, lambda: "2026-10-18 10:00:00")
    con.executescript("""
        CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, area TEXT);
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, titulo TEXT, detalle TEXT, estado TEXT,
          vence_en TEXT, creado_en TEXT, primero_id INTEGER, tomada_en TEXT,
          completado_en TEXT, bandeja_id INTEGER, responsable_chat_id INTEGER,
          area TEXT, proyecto_id INTEGER, borrado_en TEXT,
          grave INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE comentarios_tarea (
          id INTEGER PRIMARY KEY AUTOINCREMENT, tarea_id INTEGER NOT NULL,
          autor_chat_id INTEGER NOT NULL,
          creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, texto TEXT NOT NULL,
          borrado_en TEXT, borrado_por_chat_id INTEGER, editado_en TEXT);
        CREATE TABLE micro_pasos (id INTEGER PRIMARY KEY, tarea_id INTEGER,
          texto TEXT, hecho INTEGER, orden INTEGER, borrado_en TEXT);
        CREATE TABLE areas (clave TEXT, color TEXT, orden INTEGER);
        INSERT INTO areas VALUES ('IA', '#888888', 1);
        INSERT INTO proyectos VALUES (901, 'Proyecto técnico inventado', 'IA');
        INSERT INTO tareas (id, titulo, estado, bandeja_id, responsable_chat_id,
                            area, proyecto_id)
             VALUES (1, 'tarea inventada', 'pendiente', 901, %d, 'IA', 901);
    """ % config.CHAT_ID_CODE)
    for autor, texto in comentarios:
        con.execute("INSERT INTO comentarios_tarea (tarea_id, autor_chat_id, texto) "
                    "VALUES (1, ?, ?)", (autor, texto))
    con.commit()
    return con


class _Cur:
    def __init__(self, con):
        self._con, self._filas = con, []

    async def execute(self, sql, params=None):
        params = list(params or ())
        # `= ANY(%s)` con una lista (Postgres) -> `IN (?, ?, …)` (SQLite).
        while "= ANY(%s)" in sql:
            i = sql.index("= ANY(%s)")
            antes = sql[:i].count("%s")
            lista = list(params[antes])
            sql = sql.replace("= ANY(%s)", "IN (" + ",".join("%s" for _ in lista) + ")", 1)
            params[antes:antes + 1] = lista
        cur = self._con.execute(sql.replace("%s", "?"), tuple(params))
        self._filas = [dict(f) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _Conn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _Cur(self._con)

    def transaction(self):
        return _Tx()

    async def execute(self, sql, params=None):
        return await _Cur(self._con).execute(sql, params)


class _Pool:
    def __init__(self, con):
        self._conn = _Conn(con)

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


COMENTARIOS = [(config.CHAT_ID_NATALIA, "El cliente confirmó el viernes"),
               (OTRA, "Lo vi yo"), (SIN_NOMBRE, "de alguien sin nombre")]


def test_G2_la_base_devuelve_el_nombre_de_cada_autor(casa):
    db.pool = _Pool(_base(COMENTARIOS))
    filas = _bucle(db.comentarios_de_tarea(1))
    assert [f["autor"] for f in filas] == ["Natalia", "Mengano Inventado", None]


def test_G2_la_pagina_de_proyectos_pinta_Natalia(casa):
    import test_pagina_proyectos as tp

    comentarios = [{"id": i, "tarea_id": 10, "autor_chat_id": a, "creado_en": tp.CREADO,
                    "texto": t, "editado_en": None}
                   for i, (a, t) in enumerate(COMENTARIOS, 1)]
    proyecto = {"id": 1, "nombre": "P", "area": "CDS", "creado_en": tp.CREADO,
                "descripcion": None, "estado": "activo", "responsable_chat_id": None,
                "cliente_nombre": None}
    tarea = {"id": 10, "titulo": "t", "proyecto_id": 1, "area": None, "vence_en": None,
             "completado_en": None, "creado_en": tp.CREADO,
             "responsable_chat_id": None, "estado": "pendiente"}
    modelo = db.armar_pagina(list(tp.AREAS), [proyecto], [tarea], [], comentarios,
                             config.nombres_con_code(), tp.HOY)
    vistos = []

    def recorre(x):
        if isinstance(x, dict):
            if "autor" in x and "texto" in x:
                vistos.append((x["autor"], x["texto"]))
            for v in x.values():
                recorre(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                recorre(v)
    recorre(modelo)
    # El modelo repite la tarea en más de un sitio: se compara lo que se ve.
    assert set(vistos) == {
        ("Natalia", "El cliente confirmó el viernes"),
        ("Mengano Inventado", "Lo vi yo"),
        ("Alguien", "de alguien sin nombre")}, vistos


def _peticion(chat=DUENO):
    from starlette.requests import Request

    galleta = f"{panel.COOKIE}={auth.crear_token(chat, auth.VIDA_SESION)}"

    async def recibir():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "GET",
                    "scheme": "https", "server": ("t", 443), "path": "/tareas/1",
                    "root_path": "", "query_string": b"",
                    "headers": [(b"host", b"t"), (b"cookie", galleta.encode())],
                    "app": panel.app}, recibir)


def test_G2_la_pantalla_de_la_tarea_pinta_Natalia(casa):
    db.pool = _Pool(_base(COMENTARIOS))
    r = _bucle(panel.tarea_detalle(_peticion(), 1))
    assert r.status_code == 200
    html = r.body.decode()
    assert "<b>Natalia</b>" in html
    assert "<b>Mengano Inventado</b>" in html
    assert "<b>sin nombre</b>" in html
    assert html.count("<b>sin nombre</b>") == 1, "Natalia salió como «sin nombre»"


def test_G2_la_puerta_de_la_sala_tambien_la_pinta(casa):
    db.pool = _Pool(_base(COMENTARIOS))
    # Misma función que atiende la ruta, con su permiso ya resuelto.
    r = _bucle(api_code.leer_comentarios(1, quien="sala_mac"))
    assert [c["autor"] for c in r["comentarios"]] == [
        "Natalia", "Mengano Inventado", "sin nombre"]
    assert str(config.CHAT_ID_NATALIA) not in repr(r)


# ═══════════════════════════════════════════════════════════════════════
# G3 · Los hermanos: quien decide con esas puertas la sigue rechazando
# ═══════════════════════════════════════════════════════════════════════

PUERTAS = ("nombres_con_code", "puede_ser_responsable", "_chat_del_nombre")
# Los ÚNICOS sitios del repo que nombran a Natalia fuera de `config.py`: los
# que pintan un autor de comentario. Escrito acá porque es una frontera que se
# DECLARA; lo que la prueba exige es que no crezca sin que alguien lo decida.
PINTAN_AUTOR = {("db/db.py", "armar_pagina"), ("db/db.py", "comentarios_de_tarea"),
                ("web/api_code.py", "leer_comentarios")}
# L5 (18-oct-2026): los sitios que ESCRIBEN con ella como autora, y solo en la transacción del «Sí» del responsable
# (`db.contestar_consulta_de_natalia`). `_es_de_natalia` es la única pregunta que las escrituras generales le hacen
# al valor reservado; los otros dos pasan el valor como autor de lo que escriben. Ninguno decide quién es responsable
# ni quién entra.
ESCRIBEN_COMO_NATALIA = {("db/db.py", "_es_de_natalia"), ("db/db.py", "_anotar_la_tarea_de_la_consulta"),
                         ("db/db.py", "_poner_al_cliente_en_la_tarea")}
NOMBRAN_A_NATALIA = ("CHAT_ID_NATALIA", "NOMBRE_NATALIA", "autores_reservados",
                     "nombre_de_autor")


def _funciones_del_repo():
    """(archivo relativo, nombre de función, nodo) de cada `.py` fuera de tests/."""
    from test_buzon_que_no_se_ve import _py_en_disco   # LA puerta de los barridos del repo
    for ruta in _py_en_disco(RAIZ):
        rel = Path(ruta).resolve().relative_to(RAIZ.resolve()).as_posix()
        if rel.startswith("tests/"):
            continue
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield rel, nodo.name, nodo


def _nombres_usados(nodo):
    usados = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Name):
            usados.add(n.id)
        elif isinstance(n, ast.Attribute):
            usados.add(n.attr)
    return usados


def test_G3_los_hermanos_salen_de_lo_real_y_ninguno_nombra_a_Natalia():
    hermanos = [(rel, nombre) for rel, nombre, nodo in _funciones_del_repo()
                if _nombres_usados(nodo) & set(PUERTAS)
                and rel != "config.py"]
    # Se sacan de lo real: si el barrido no ve ninguno, la prueba no vigila nada.
    assert len(hermanos) >= 8, hermanos
    assert ("acciones/crud.py", "_chat_del_nombre") in hermanos
    assert ("web/app.py", "_filtro_pedido") in hermanos
    nombran = [(rel, nombre) for rel, nombre, nodo in _funciones_del_repo()
               if _nombres_usados(nodo) & set(NOMBRAN_A_NATALIA)
               and rel != "config.py"]
    assert set(nombran) == PINTAN_AUTOR | ESCRIBEN_COMO_NATALIA, (
        f"alguien más nombra a Natalia: {sorted(set(nombran) - PINTAN_AUTOR - ESCRIBEN_COMO_NATALIA)}; "
        f"si es un sitio que PINTA un autor, se agrega a PINTAN_AUTOR; si escribe CON ella como autora, a "
        f"ESCRIBEN_COMO_NATALIA; si DECIDE quién es responsable o quién entra, no puede")
    # Y ninguno de los hermanos que decide es uno de los que pintan, salvo
    # `armar_pagina`, que pinta con `nombres_con_code` y los reservados aparte.
    decidores = {h for h in hermanos if h not in PINTAN_AUTOR}
    assert decidores & set(nombran) == set()


def test_G3_los_hermanos_que_resuelven_un_nombre_dan_el_mismo_no(casa):
    n = config.CHAT_ID_NATALIA
    # Todos hacen la misma pregunta: «¿esto es alguien a quien se asigna?»
    veredictos = {
        "puede_ser_responsable": config.puede_ser_responsable(n),
        "puede_recibir_telegram": config.puede_recibir_telegram(n),
        "puede_entrar": auth.puede_entrar(n),
        "_chat_del_nombre": crud._chat_del_nombre("Natalia")[0] is not None,
        "filtro": panel._filtro_pedido("Natalia")[0] != "todas",
        "opciones": "Natalia" in [e for _, e in config.opciones_de_responsable()],
    }
    assert not any(veredictos.values()), veredictos
