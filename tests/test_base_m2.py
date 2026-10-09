"""La base de Lucy 1.0 (encargo E2, M2): G5, G6, G7, G9, G12 y G15.

M2 le da al proyecto un responsable y un cliente opcional, a los comentarios su
`editado_en` y crea `participantes` (las personas de un proyecto o de una tarea).
Las puertas que no tienen pantalla todavía:

  G5   El responsable de un proyecto solo es alguien que `puede_ser_responsable`,
       por `crud.editar` y por `crud.deshacer` (el panel llega en E5 y usará la
       misma puerta).
  G6   El cliente que se guarda es el que Noco devuelve para ese Id (`db.poner_
       cliente`, con un doble de Noco), y Telegram no escribe `cliente_*`.
  G7   Una persona está en un solo sitio, una sola vez y siempre con rol: el
       `CHECK` y los índices se EJECUTAN de verdad.
  G9   `proyectos.estado` solo acepta activo/pausado/cerrado y un cerrado no
       recibe tareas.
  G12  El título de una tarea no queda vacío ni largo por `crud.editar`.
  G15  Borrar con `actor='panel'` es soft-delete con ese actor y se deshace.

LA FRONTERA, dicha una vez (la de `tests/test_grupo_ia.py`, más lo de esta
prueba):

  · NO hay Postgres en esta máquina. Todo corre sobre SQLite. Las tablas
    `proyectos`, `comentarios_tarea` y `participantes` se crean con el
    `CREATE TABLE` REAL de `db/schema.sql` (y `participantes` también con el de
    la migración): salvo dos cambios de dialecto (`BIGSERIAL PRIMARY KEY` →
    `INTEGER PRIMARY KEY` y `DEFAULT now()` → `DEFAULT (now())`), el texto es el
    mismo. `tareas`, `areas` y `log_acciones` se crean con las columnas que
    declara `db.columnas_declaradas()` más el `CHECK` de la migración de áreas.
  · `crud.editar`, `crud.borrar` y `db.poner_cliente` corren enteros contra
    SQLite con su SQL real. `crud.deshacer` solo se prueba por el camino de
    RECHAZO (la puerta decide antes del SQL); su camino de éxito usa
    `jsonb_populate_record`, que SQLite no tiene, salvo el de `borrar`.
  · El doble de Noco (`_noco_de_mentira`) afirma SOLO la forma que el diseño de
    Lucy 1.0 (§4) le fija a `noco_lectura.persona`: `{id, nombre}` o `None`. NO
    es una captura de Noco: esa respuesta real la captura la sala (E0) y la
    reemplaza E3.
  · `ALTER TABLE ... ADD CONSTRAINT` no corre en SQLite; por eso el `CHECK` del
    cliente se ejecuta desde `db/schema.sql` y una prueba exige que la
    migración declare EL MISMO texto.

Correr:  python3 -m pytest tests/test_base_m2.py -q
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import types

import pytest

import test_grupo_ia as g
import config
import acciones.crud as crud
import db.db as db

_SCHEMA = g._ROOT / "db" / "schema.sql"


# ═══════════════════════════════════════════════════════════════════════
# La base de prueba
# ═══════════════════════════════════════════════════════════════════════

def _migracion_m2():
    """La migración que crea `participantes` (se busca por lo que hace, no por
    su nombre)."""
    suyas = [a for a in g._migraciones()
             if re.search(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?participantes\b",
                          a.read_text(encoding="utf-8"), re.I)]
    assert len(suyas) == 1, suyas
    return suyas[0]


def _ddl(texto: str, tabla: str) -> list[str]:
    """Las sentencias `CREATE TABLE`/`CREATE [UNIQUE] INDEX` de `tabla` en
    `texto`, con los dos cambios de dialecto."""
    salida = []
    for s in g._sentencias(texto):
        if re.match(rf"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?{tabla}\b", s, re.I) or \
                re.match(rf"CREATE\s+(UNIQUE\s+)?INDEX\s+.*?\sON\s+{tabla}\b", s, re.I | re.S):
            salida.append(
                s.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY")
                 .replace("DEFAULT now()", "DEFAULT (now())"))
    return salida


def _base(participantes_desde: str = "schema") -> sqlite3.Connection:
    # `check_same_thread=False`: el cliente HTTP de prueba corre la ruta en otro
    # hilo (`tests/test_pagina_proyectos.py`).
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.create_function("now", 0, lambda: "2026-10-02T12:00:00+00:00")
    declaradas = db.columnas_declaradas()
    schema = _SCHEMA.read_text(encoding="utf-8")
    con.execute("CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER)")
    con.execute("INSERT INTO areas (clave, color, orden) VALUES ('CDS', '#1', 1)")
    for tabla in ("tareas", "log_acciones"):
        columnas = []
        for c in declaradas[tabla]:
            # (`estado` con su DEFAULT de `db/schema.sql`: una tarea creada sin
            # decir estado nace pendiente, como en Postgres.)
            columnas.append("id INTEGER PRIMARY KEY" if c == "id" else
                            "area TEXT REFERENCES areas(clave)" if c == "area" else
                            "estado TEXT NOT NULL DEFAULT 'pendiente'"
                            if (c == "estado" and tabla == "tareas") else c)
        con.execute(f"CREATE TABLE {tabla} ({', '.join(columnas)})")
    for tabla in ("proyectos", "comentarios_tarea"):
        for s in _ddl(schema, tabla):
            con.execute(s)
    # `notas` y `bandeja` (parte 6 de la página del proyecto): la página lee las notas de un proyecto y el
    # autor de las de Telegram sale de su bandeja. Solo el `CREATE TABLE` (el índice `hnsw` de `bandeja`
    # es de pgvector y SQLite no lo conoce).
    for tabla in ("bandeja", "notas"):
        for s in _ddl(schema, tabla):
            if s.lstrip().upper().startswith("CREATE TABLE"):
                con.execute(s)
    # `sesiones_de_proyecto` (parte 11 de la página del proyecto): la página lee las decisiones de la
    # casa sobre las sesiones de un proyecto, y las dos rutas que las escriben. Con su índice único
    # parcial (`WHERE borrado_en IS NULL`), que SQLite entiende.
    for s in _ddl(schema, "sesiones_de_proyecto"):
        con.execute(s)
    fuente = schema if participantes_desde == "schema" else \
        _migracion_m2().read_text(encoding="utf-8")
    for s in _ddl(fuente, "participantes"):
        con.execute(s)
    con.commit()
    return con


@pytest.fixture
def base(monkeypatch):
    con = _base()
    monkeypatch.setattr(db, "pool", g._Pool(con))
    return con


@pytest.fixture
def gente():
    """Rosi y el dueño entran al panel; AJENO tiene nombre pero no entra."""
    permitidos, nombres = config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT
    dueno, rosi, ajeno = config.CHAT_ID_DUENO, 700100001, 700100999
    config.NOMBRES_POR_CHAT = {dueno: "Tiziano", rosi: "Rosi", ajeno: "Ajeno"}
    config.CHAT_IDS_PERMITIDOS = (dueno, rosi)
    yield types.SimpleNamespace(dueno=dueno, rosi=rosi, ajeno=ajeno)
    config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT = permitidos, nombres


def _proyecto(con, id=1, nombre="Proyecto de prueba", estado="activo", **otros):
    campos = {"id": id, "nombre": nombre, "estado": estado, **otros}
    con.execute(f"INSERT INTO proyectos ({', '.join(campos)}) VALUES "
                f"({', '.join('?' for _ in campos)})", tuple(campos.values()))
    con.commit()


def _tarea(con, id=1, titulo="tarea de prueba", **otros):
    campos = {"id": id, "titulo": titulo, "estado": "pendiente",
              "bandeja_id": 9000 + id, **otros}
    con.execute(f"INSERT INTO tareas ({', '.join(campos)}) VALUES "
                f"({', '.join('?' for _ in campos)})", tuple(campos.values()))
    con.commit()


def _una(con, sql, *params):
    f = con.execute(sql, params).fetchone()
    return dict(f) if f is not None else None


def _huellas(con):
    return [dict(f) for f in con.execute("SELECT * FROM log_acciones ORDER BY id")]


def _huella(con, accion, tabla, registro_id, antes, despues):
    con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) "
        "VALUES ('lucy', ?, ?, ?, ?, ?)",
        (accion, tabla, registro_id, json.dumps(antes), json.dumps(despues)))
    con.commit()
    return con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════
# G7: una persona, un sitio, una vez, con rol (el DDL se ejecuta de verdad)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(params=["schema", "migracion"])
def con_participantes(request):
    con = _base(request.param)
    _proyecto(con, 1)
    _proyecto(con, 2, nombre="Otro")
    _tarea(con, 1)
    _tarea(con, 2)
    return con


def _persona(con, *, proyecto_id=None, tarea_id=None, noco_id=10, nombre="Persona",
             rol="Mezcla", borrado_en=None, **resto):
    campos = {"proyecto_id": proyecto_id, "tarea_id": tarea_id, "noco_id": noco_id,
              "nombre": nombre, "rol": rol, "creado_por_chat_id": 1,
              "borrado_en": borrado_en, **resto}
    con.execute(f"INSERT INTO participantes ({', '.join(campos)}) VALUES "
                f"({', '.join('?' for _ in campos)})", tuple(campos.values()))


@pytest.mark.parametrize("fuente", ["schema", "migracion"])
def test_G7_el_ddl_trae_la_tabla_y_sus_dos_indices(fuente):
    texto = (_SCHEMA if fuente == "schema" else _migracion_m2()).read_text(encoding="utf-8")
    ddl = _ddl(texto, "participantes")
    assert [bool(re.match(r"CREATE\s+TABLE", d, re.I)) for d in ddl] == [True, False, False], ddl
    assert sum("UNIQUE INDEX" in d.upper() for d in ddl) == 2, ddl


def test_G7_una_persona_en_un_proyecto_y_en_una_tarea_entra(con_participantes):
    _persona(con_participantes, proyecto_id=1)
    _persona(con_participantes, tarea_id=1)
    assert con_participantes.execute("SELECT count(*) FROM participantes").fetchone()[0] == 2


def test_G7_tiene_que_estar_en_exactamente_un_sitio(con_participantes):
    with pytest.raises(sqlite3.IntegrityError):
        _persona(con_participantes)                              # en ninguno
    with pytest.raises(sqlite3.IntegrityError):
        _persona(con_participantes, proyecto_id=1, tarea_id=1)   # en los dos


def test_G7_la_misma_persona_dos_veces_en_el_mismo_proyecto_no(con_participantes):
    _persona(con_participantes, proyecto_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _persona(con_participantes, proyecto_id=1, rol="Otro rol")
    _persona(con_participantes, proyecto_id=2)                   # otro proyecto: sí
    _persona(con_participantes, proyecto_id=1, noco_id=11)       # otra persona: sí


def test_G7_la_misma_persona_dos_veces_en_la_misma_tarea_no(con_participantes):
    _persona(con_participantes, tarea_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _persona(con_participantes, tarea_id=1, rol="Otro rol")
    _persona(con_participantes, tarea_id=2)


def test_G7_una_persona_borrada_se_puede_volver_a_agregar(con_participantes):
    """Los índices son parciales: solo cuentan a las que no están borradas."""
    _persona(con_participantes, proyecto_id=1, borrado_en="2026-10-01T00:00:00+00:00")
    _persona(con_participantes, proyecto_id=1)
    _persona(con_participantes, tarea_id=1, borrado_en="2026-10-01T00:00:00+00:00")
    _persona(con_participantes, tarea_id=1)


@pytest.mark.parametrize("rol", ["", "   ", "x" * 81, " " + "x" * 81])
def test_G7_el_rol_no_puede_ser_vacio_ni_de_mas_de_80(con_participantes, rol):
    with pytest.raises(sqlite3.IntegrityError):
        _persona(con_participantes, proyecto_id=1, rol=rol)


@pytest.mark.parametrize("rol", ["x", "x" * 80])
def test_G7_el_rol_de_1_y_de_80_entra(con_participantes, rol):
    _persona(con_participantes, proyecto_id=1, rol=rol)


@pytest.mark.parametrize("campo", ["noco_id", "nombre", "rol", "creado_por_chat_id"])
def test_G7_lo_obligatorio_no_acepta_NULL(con_participantes, campo):
    campos = {"proyecto_id": 1, "noco_id": 10, "nombre": "P", "rol": "R",
              "creado_por_chat_id": 1}
    campos[campo] = None
    with pytest.raises(sqlite3.IntegrityError):
        con_participantes.execute(
            f"INSERT INTO participantes ({', '.join(campos)}) VALUES "
            f"({', '.join('?' for _ in campos)})", tuple(campos.values()))


def _normal(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().lower()


def test_G7_la_migracion_y_el_esquema_dicen_lo_mismo_de_participantes():
    """Una base nueva sale de `db/schema.sql` y la de producción, de la
    migración: el DDL de las dos, igual salvo `IF NOT EXISTS`."""
    del_esquema = [_normal(s) for s in _ddl(_SCHEMA.read_text(encoding="utf-8"), "participantes")]
    de_la_migracion = [_normal(s).replace("if not exists ", "")
                       for s in _ddl(_migracion_m2().read_text(encoding="utf-8"), "participantes")]
    assert del_esquema == de_la_migracion


# ═══════════════════════════════════════════════════════════════════════
# G6 (la base): el cliente son dos columnas juntas o ninguna
# ═══════════════════════════════════════════════════════════════════════

def _check_balanceado(texto: str, nombre: str) -> str:
    i = texto.index(nombre)
    j = texto.index("CHECK", i)
    k = texto.index("(", j)
    prof = 0
    for n in range(k, len(texto)):
        prof += (texto[n] == "(") - (texto[n] == ")")
        if prof == 0:
            return _normal(texto[k:n + 1])
    raise AssertionError("paréntesis sin cerrar")


def test_G6_las_dos_columnas_del_cliente_juntas_o_ninguna(base):
    _proyecto(base, 1)                                           # sin cliente: el normal
    _proyecto(base, 2, nombre="B", cliente_noco_id=5, cliente_nombre="Ficha")
    for resto in ({"cliente_noco_id": 5}, {"cliente_nombre": "Solo nombre"}):
        with pytest.raises(sqlite3.IntegrityError):
            _proyecto(base, 3, nombre="C", **resto)


def test_G6_la_migracion_declara_el_mismo_check_del_cliente_que_el_esquema():
    esquema = _check_balanceado(_SCHEMA.read_text(encoding="utf-8"), "proyectos_cliente_entero")
    migracion = _check_balanceado(
        _migracion_m2().read_text(encoding="utf-8"), "proyectos_cliente_entero")
    assert esquema == migracion


# ═══════════════════════════════════════════════════════════════════════
# G5: el responsable del proyecto
# ═══════════════════════════════════════════════════════════════════════

async def test_G5_editar_acepta_a_quien_puede_por_nombre_y_por_chat(base, gente):
    _proyecto(base, 1)
    for pedido, esperado in (("Rosi", gente.rosi), (gente.dueno, gente.dueno),
                             (config.CHAT_ID_CODE, config.CHAT_ID_CODE)):
        fila, log_id = await crud.editar("proyectos", 1, {"responsable_chat_id": pedido}, "prueba")
        assert _una(base, "SELECT responsable_chat_id r FROM proyectos WHERE id = 1")["r"] == esperado
        assert log_id is not None


async def test_G5_editar_acepta_quitarlo(base, gente):
    _proyecto(base, 1, responsable_chat_id=gente.rosi)
    await crud.editar("proyectos", 1, {"responsable_chat_id": ""}, "prueba")
    assert _una(base, "SELECT responsable_chat_id r FROM proyectos WHERE id = 1")["r"] is None


@pytest.mark.parametrize("pedido", ["AJENO", "Nadie", "ajeno-numero", 12345, True, ["Rosi"]])
async def test_G5_editar_rechaza_a_quien_no_puede_y_no_escribe_nada(base, gente, pedido):
    if pedido == "AJENO":
        pedido = gente.ajeno                  # tiene nombre pero no entra al panel
    if pedido == "ajeno-numero":
        pedido = str(gente.ajeno)
    _proyecto(base, 1)
    with pytest.raises(ValueError):
        await crud.editar("proyectos", 1, {"responsable_chat_id": pedido}, "prueba")
    assert _una(base, "SELECT responsable_chat_id r FROM proyectos WHERE id = 1")["r"] is None
    assert _huellas(base) == []


async def test_G5_un_responsable_que_no_vale_rechaza_tambien_el_resto_de_la_edicion(base, gente):
    _proyecto(base, 1)
    with pytest.raises(ValueError):
        await crud.editar("proyectos", 1, {"estado": "pausado",
                                           "responsable_chat_id": gente.ajeno}, "prueba")
    assert _una(base, "SELECT estado e FROM proyectos WHERE id = 1")["e"] == "activo"


async def test_G5_deshacer_no_devuelve_un_responsable_que_ya_no_vale(base, gente):
    _proyecto(base, 1, responsable_chat_id=gente.rosi)
    antes = {"id": 1, "nombre": "Proyecto de prueba", "estado": "activo",
             "responsable_chat_id": gente.ajeno}
    log_id = _huella(base, "editar", "proyectos", 1, antes,
                     {**antes, "responsable_chat_id": gente.rosi})
    with pytest.raises(ValueError) as e:
        await crud.deshacer(log_id)
    assert re.fullmatch(
        r"No lo deshice: el proyecto volvería a quien lo llevaba, y ese chat no "
        r"puede ser responsable: solo quien entra al panel y tiene nombre "
        r"\(hoy: .*\)\.", str(e.value)), str(e.value)
    assert _una(base, "SELECT responsable_chat_id r FROM proyectos WHERE id = 1")["r"] == gente.rosi


# ═══════════════════════════════════════════════════════════════════════
# G6: el cliente es el que Noco devuelve; Telegram no lo escribe
# ═══════════════════════════════════════════════════════════════════════

def _noco_de_mentira(fichas: dict, pedidos: list | None = None):
    """Un `leer_persona` de mentira. Afirma solo la forma de §4 del diseño de
    Lucy 1.0 (`{id, nombre}` o `None`); NO es una captura de Noco (ver la
    frontera del archivo)."""
    async def leer(noco_id):
        if pedidos is not None:
            pedidos.append(noco_id)
        r = fichas[noco_id]
        if isinstance(r, Exception):
            raise r
        return r
    return leer


async def test_G6_se_guarda_el_nombre_que_devuelve_noco_para_ese_id(base):
    _proyecto(base, 1)
    leer = _noco_de_mentira({7: {"id": 7, "nombre": "  Nombre Desde Noco "}})
    assert await db.poner_cliente(1, 7, leer_persona=leer) is True
    fila = _una(base, "SELECT cliente_noco_id i, cliente_nombre n FROM proyectos WHERE id = 1")
    assert fila == {"i": 7, "n": "Nombre Desde Noco"}
    huellas = _huellas(base)
    assert len(huellas) == 1
    assert (huellas[0]["actor"], huellas[0]["accion"], huellas[0]["tabla"]) == ("panel", "editar", "proyectos")
    assert json.loads(huellas[0]["despues"]) == {"cliente_noco_id": 7, "cliente_nombre": "Nombre Desde Noco"}
    assert json.loads(huellas[0]["antes"])["cliente_noco_id"] is None


async def test_G6_lo_mismo_dos_veces_no_escribe_ni_deja_huella(base):
    _proyecto(base, 1)
    leer = _noco_de_mentira({7: {"id": 7, "nombre": "Ficha"}})
    await db.poner_cliente(1, 7, leer_persona=leer)
    assert await db.poner_cliente(1, 7, leer_persona=leer) is False
    assert len(_huellas(base)) == 1


async def test_G6_cambiar_de_cliente_y_quitarlo(base):
    _proyecto(base, 1)
    leer = _noco_de_mentira({7: {"id": 7, "nombre": "Uno"}, 8: {"id": 8, "nombre": "Dos"}})
    await db.poner_cliente(1, 7, leer_persona=leer)
    await db.poner_cliente(1, 8, leer_persona=leer)
    assert _una(base, "SELECT cliente_nombre n FROM proyectos WHERE id = 1")["n"] == "Dos"
    assert await db.poner_cliente(1, None, leer_persona=leer) is True
    assert _una(base, "SELECT cliente_noco_id i, cliente_nombre n FROM proyectos WHERE id = 1") == {"i": None, "n": None}
    assert await db.poner_cliente(1, None, leer_persona=leer) is False      # ya no tenía


@pytest.mark.parametrize("ficha", [
    None, {}, {"id": 99, "nombre": "De otro Id"}, {"id": 7, "nombre": ""},
    {"id": 7, "nombre": "   "}, {"id": 7, "nombre": None}, {"id": 7}])
async def test_G6_una_ficha_que_no_vale_no_guarda_nada(base, ficha):
    _proyecto(base, 1)
    with pytest.raises(ValueError):
        await db.poner_cliente(1, 7, leer_persona=_noco_de_mentira({7: ficha}))
    assert _una(base, "SELECT cliente_noco_id i FROM proyectos WHERE id = 1")["i"] is None
    assert _huellas(base) == []


async def test_G6_si_noco_no_contesta_no_se_guarda_ni_se_abre_la_base(base, monkeypatch):
    _proyecto(base, 1)

    class _SinConexion:
        def connection(self):
            raise AssertionError("se abrió la base antes de tener la ficha")
    monkeypatch.setattr(db, "pool", _SinConexion())
    with pytest.raises(ConnectionError):
        await db.poner_cliente(1, 7, leer_persona=_noco_de_mentira({7: ConnectionError("caído")}))


@pytest.mark.parametrize("malo", [0, -3, True, "7", 7.0])
async def test_G6_un_id_que_no_es_un_numero_ni_se_le_pregunta_a_noco(base, malo):
    _proyecto(base, 1)
    pedidos = []
    with pytest.raises(ValueError):
        await db.poner_cliente(1, malo, leer_persona=_noco_de_mentira({}, pedidos))
    assert pedidos == []


async def test_G6_un_proyecto_que_no_existe_o_esta_en_la_papelera_se_rechaza(base):
    _proyecto(base, 1, borrado_en="2026-10-01T00:00:00+00:00")
    leer = _noco_de_mentira({7: {"id": 7, "nombre": "Ficha"}})
    for pid in (1, 404):
        with pytest.raises(ValueError):
            await db.poner_cliente(pid, 7, leer_persona=leer)


@pytest.mark.parametrize("cambios", [
    {"cliente_nombre": "Inventado por el chat"}, {"cliente_noco_id": 7},
    {"cliente_noco_id": None}, {"cliente_nombre": None},
    {"estado": "pausado", "cliente_nombre": "X", "cliente_noco_id": 7}])
async def test_G6_por_el_chat_no_se_escribe_el_cliente(base, gente, cambios):
    _proyecto(base, 1, cliente_noco_id=5, cliente_nombre="Ficha")
    with pytest.raises(ValueError, match="en el panel"):
        await crud.editar("proyectos", 1, cambios, "prueba")
    assert _una(base, "SELECT estado e, cliente_noco_id i, cliente_nombre n FROM proyectos WHERE id = 1") == \
        {"e": "activo", "i": 5, "n": "Ficha"}
    assert _huellas(base) == []


async def test_G6_deshacer_tampoco_escribe_el_cliente_desde_el_chat(base, gente):
    _proyecto(base, 1)
    antes = {"id": 1, "nombre": "Proyecto de prueba", "estado": "activo",
             "cliente_noco_id": 5, "cliente_nombre": "Vieja"}
    log_id = _huella(base, "editar", "proyectos", 1, antes,
                     {"cliente_noco_id": None, "cliente_nombre": None})
    with pytest.raises(ValueError) as e:
        await crud.deshacer(log_id)
    assert str(e.value) == ("No lo deshice: el proyecto volvería al cliente que "
                            "tenía, y el cliente de un proyecto se elige en el panel."), str(e.value)


def _funciones_que_escriben_cliente(arbol) -> set[str]:
    hallados = set()
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.search(r"\b(UPDATE|INSERT)\b", n.value, re.I)
                    and re.search(r"cliente_(noco_id|nombre)", n.value)):
                hallados.add(fn.name)
    return hallados


def _escritores_de_cliente() -> set[str]:
    """Las funciones del código (fuera de `tests/`) que arman un INSERT/UPDATE
    con `cliente_noco_id` o `cliente_nombre`. LA LISTA SALE DE LO REAL: se
    recorren los `.py` del repo. Frontera: un escritor que arme el nombre de la
    columna por partes no aparece (los genéricos, `crud.editar` y `deshacer`, no
    la nombran y los cubren las pruebas de arriba)."""
    hallados = set()
    for archivo in g._archivos_de_texto():
        if archivo.suffix == ".py":
            try:
                hallados |= _funciones_que_escriben_cliente(
                    ast.parse(archivo.read_text(encoding="utf-8")))
            except SyntaxError:
                continue
    return hallados


def test_G6_el_unico_escritor_del_cliente_es_poner_cliente():
    assert _escritores_de_cliente() == {"poner_cliente"}


def test_G6_el_censo_ve_un_escritor_inventado():
    """Una guarda probada solo con lo que hay hoy promete de más: se la prueba
    con código que no existe en el repo."""
    falso = ast.parse(
        "async def otro(c):\n"
        "    await c.execute('UPDATE proyectos SET cliente_nombre = 1 WHERE id = 2')\n"
        "async def y_otro_mas(c):\n"
        "    await c.execute('INSERT INTO proyectos (nombre, cliente_noco_id) VALUES (1, 2)')\n"
        "async def lector(c):\n"
        "    await c.execute('SELECT cliente_nombre FROM proyectos')\n")
    assert _funciones_que_escriben_cliente(falso) == {"otro", "y_otro_mas"}


# ═══════════════════════════════════════════════════════════════════════
# G9: el estado del proyecto
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pedido,queda", [
    ("activo", "activo"), ("pausado", "pausado"), ("cerrado", "cerrado"),
    ("  Cerrado ", "cerrado"), ("PAUSADO", "pausado")])
async def test_G9_los_tres_estados_entran(base, gente, pedido, queda):
    _proyecto(base, 1)
    await crud.editar("proyectos", 1, {"estado": pedido}, "prueba")
    assert _una(base, "SELECT estado e FROM proyectos WHERE id = 1")["e"] == queda


@pytest.mark.parametrize("pedido", ["terminado", "", "   ", None, 3, "cerrado y listo", "archivado"])
async def test_G9_otro_estado_se_rechaza_y_no_escribe_nada(base, gente, pedido):
    _proyecto(base, 1)
    with pytest.raises(ValueError, match="activo, pausado, cerrado"):
        await crud.editar("proyectos", 1, {"estado": pedido}, "prueba")
    assert _una(base, "SELECT estado e FROM proyectos WHERE id = 1")["e"] == "activo"
    assert _huellas(base) == []


async def test_G9_deshacer_no_deja_un_estado_fuera_del_vocabulario(base, gente):
    _proyecto(base, 1, estado="activo")
    antes = {"id": 1, "nombre": "Proyecto de prueba", "estado": "terminado"}
    log_id = _huella(base, "editar", "proyectos", 1, antes, {**antes, "estado": "activo"})
    with pytest.raises(ValueError) as e:
        await crud.deshacer(log_id)
    assert str(e.value) == ("No lo deshice: el proyecto volvería al estado que tenía, "
                            "y el estado de un proyecto es uno de: activo, pausado, "
                            "cerrado."), str(e.value)


async def test_G9_un_proyecto_cerrado_por_la_puerta_no_recibe_tareas(base, gente):
    _proyecto(base, 1)
    await crud.editar("proyectos", 1, {"estado": "cerrado"}, "prueba")
    cur = g._Conn(base).cursor(row_factory=dict)
    with pytest.raises(db.ProyectoNoAdmiteTareas) as e:
        await db.proyecto_admite_tareas(cur, 1)
    assert e.value.clave == "cerrado"
    await crud.editar("proyectos", 1, {"estado": "pausado"}, "prueba")
    assert (await db.proyecto_admite_tareas(g._Conn(base).cursor(row_factory=dict), 1))["estado"] == "pausado"


def test_G9_los_estados_con_los_que_nace_un_proyecto_estan_en_el_vocabulario():
    """Los INSERT de `proyectos` no nombran `estado`: nace con el DEFAULT de la
    base. Ese DEFAULT sale de `db/schema.sql`, no se teclea acá."""
    m = re.search(r"CREATE TABLE proyectos\s*\((.*?)\n\);",
                  db._sin_comentarios(_SCHEMA.read_text(encoding="utf-8")), re.S)
    por_omision = re.search(r"estado\s+TEXT NOT NULL DEFAULT '(\w+)'", m.group(1)).group(1)
    assert por_omision in db.ESTADOS_PROYECTO
    assert db.ESTADO_PROYECTO_CERRADO in db.ESTADOS_PROYECTO


# ═══════════════════════════════════════════════════════════════════════
# G12: el título de una tarea
# ═══════════════════════════════════════════════════════════════════════

async def test_G12_un_titulo_normal_se_guarda_sin_espacios_de_alrededor(base, gente):
    _tarea(base, 1)
    await crud.editar("tareas", 1, {"titulo": "  llamar al cliente "}, "prueba")
    assert _una(base, "SELECT titulo t FROM tareas WHERE id = 1")["t"] == "llamar al cliente"


async def test_G12_el_tope_son_200_y_201_no_entra(base, gente):
    _tarea(base, 1)
    await crud.editar("tareas", 1, {"titulo": "x" * db.LARGO_TITULO_TAREA}, "prueba")
    with pytest.raises(ValueError, match="no puede pasar de"):
        await crud.editar("tareas", 1, {"titulo": "x" * (db.LARGO_TITULO_TAREA + 1)}, "prueba")
    assert len(_una(base, "SELECT titulo t FROM tareas WHERE id = 1")["t"]) == 200


@pytest.mark.parametrize("pedido", ["", "   ", None, 5])
async def test_G12_un_titulo_vacio_se_rechaza_y_no_escribe_nada(base, gente, pedido):
    _tarea(base, 1, titulo="el de antes")
    with pytest.raises(ValueError, match="no puede quedar vacío"):
        await crud.editar("tareas", 1, {"titulo": pedido}, "prueba")
    assert _una(base, "SELECT titulo t FROM tareas WHERE id = 1")["t"] == "el de antes"
    assert _huellas(base) == []


async def test_G12_deshacer_no_deja_un_titulo_vacio(base, gente):
    _tarea(base, 1, titulo="el de ahora")
    antes = {"id": 1, "titulo": "", "estado": "pendiente"}
    log_id = _huella(base, "editar", "tareas", 1, antes, {**antes, "titulo": "el de ahora"})
    with pytest.raises(ValueError) as e:
        await crud.deshacer(log_id)
    # La frase COMPLETA: antes decía «la tarea volvería a quien la tenía», que
    # habla del responsable, aunque lo que no valiera fuera el título.
    assert str(e.value) == ("No lo deshice: la tarea volvería a tener el título que "
                            "tenía, y el título de una tarea no puede quedar vacío."), str(e.value)


def test_G12_el_panel_y_la_puerta_usan_el_mismo_tope():
    import web.app as panel
    assert panel.LARGO_TITULO == db.LARGO_TITULO_TAREA


# ═══════════════════════════════════════════════════════════════════════
# G15: borrar con la × es soft-delete, con el actor que lo hizo, y se deshace
# ═══════════════════════════════════════════════════════════════════════

async def test_G15_borrar_desde_el_panel_deja_actor_panel_y_la_fila_sigue_ahi(base):
    _tarea(base, 1)
    log_id = await crud.borrar("tareas", 1, "borrada con la ×", actor="panel")
    fila = _una(base, "SELECT id, borrado_en FROM tareas WHERE id = 1")
    assert fila is not None and fila["borrado_en"] is not None
    huella = _una(base, "SELECT * FROM log_acciones WHERE id = ?", log_id)
    assert (huella["actor"], huella["accion"], huella["tabla"], huella["registro_id"]) == \
        ("panel", "borrar", "tareas", 1)
    assert json.loads(huella["antes"])["titulo"] == "tarea de prueba"


async def test_G15_sin_decir_actor_sigue_siendo_lucy(base):
    _tarea(base, 1)
    log_id = await crud.borrar("tareas", 1, "borrada por el chat")
    assert _una(base, "SELECT actor FROM log_acciones WHERE id = ?", log_id)["actor"] == "lucy"


async def test_G15_lo_borrado_con_actor_panel_se_puede_deshacer(base):
    _tarea(base, 1)
    log_id = await crud.borrar("tareas", 1, "borrada con la ×", actor="panel")
    assert await crud.deshacer(log_id) == "lo que había archivado"
    assert _una(base, "SELECT borrado_en b FROM tareas WHERE id = 1")["b"] is None


async def test_G15_borrar_dos_veces_no_hace_nada_la_segunda(base):
    _tarea(base, 1)
    assert await crud.borrar("tareas", 1, "x", actor="panel") is not None
    assert await crud.borrar("tareas", 1, "x", actor="panel") is None
    assert len(_huellas(base)) == 1


# ═══════════════════════════════════════════════════════════════════════
# Lo que cierra la vuelta del NO PASA sobre 73181ff
# ═══════════════════════════════════════════════════════════════════════

async def test_G12_el_largo_se_mide_despues_de_quitar_los_espacios(base, gente):
    """200 caracteres con espacios alrededor valen: el tope es del título que
    queda, no del que se escribió."""
    _tarea(base, 1)
    await crud.editar("tareas", 1, {"titulo": "  " + "x" * db.LARGO_TITULO_TAREA + "  "}, "prueba")
    assert len(_una(base, "SELECT titulo t FROM tareas WHERE id = 1")["t"]) == db.LARGO_TITULO_TAREA


async def test_G6_el_mismo_id_con_otro_nombre_en_noco_actualiza_el_nombre(base):
    """El diseño promete guardar el nombre que Noco devuelve. Si la ficha cambió
    de nombre en Noco (mismo Id), el proyecto toma el nombre nuevo y deja huella."""
    _proyecto(base, 1, cliente_noco_id=7, cliente_nombre="Nombre viejo")
    leer = _noco_de_mentira({7: {"id": 7, "nombre": "Nombre nuevo"}})
    assert await db.poner_cliente(1, 7, leer_persona=leer) is True
    assert _una(base, "SELECT cliente_nombre n FROM proyectos WHERE id = 1")["n"] == "Nombre nuevo"
    assert len(_huellas(base)) == 1


@pytest.mark.parametrize("lugar", ["proyecto_id", "tarea_id"])
def test_G7_el_proyecto_o_la_tarea_tiene_que_existir(con_participantes, lugar):
    """La FK de `participantes`: no se puede agregar a alguien a un sitio que no
    existe (SQLite las hace cumplir porque `_base` enciende `foreign_keys`)."""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _persona(con_participantes, **{lugar: 999})


_PALABRAS_DE_RESTRICCION = re.compile(
    r"\b(DEFAULT|NOT\s+NULL|NULL|REFERENCES|CHECK|PRIMARY|UNIQUE|CONSTRAINT)\b", re.I)


def _tipo(definicion: str):
    resto = definicion.strip().split(None, 1)
    if len(resto) < 2:
        return None
    m = _PALABRAS_DE_RESTRICCION.search(resto[1])
    return re.sub(r"\s+", "", resto[1][:m.start()] if m else resto[1]).lower()


def _tipos_de_los_create(texto: str) -> dict:
    salida = {}
    for m in db._RE_TABLA.finditer(texto):
        piezas, pieza, prof = [], "", 0
        for ch in m.group(2):
            prof += (ch == "(") - (ch == ")")
            if ch == "," and prof == 0:
                piezas.append(pieza)
                pieza = ""
            else:
                pieza += ch
        piezas.append(pieza)
        cols = {}
        for pz in piezas:
            w = pz.split()
            if w and w[0].lower() not in ("primary", "unique", "foreign", "check", "constraint"):
                cols[w[0].lower()] = _tipo(pz)
        salida[m.group(1).lower()] = cols
    return salida


def _tipos_que_declaran_las_migraciones() -> list[tuple]:
    """(archivo, tabla, columna, tipo) de cada `ADD COLUMN` y de cada columna de
    un `CREATE TABLE` de CUALQUIER migración. Sale de lo real, no de una lista."""
    salida = []
    for archivo in g._migraciones():
        texto = db._sin_comentarios(archivo.read_text(encoding="utf-8"))
        for m in re.finditer(
                r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s+([^;]*)",
                texto, re.I | re.S):
            salida.append((archivo.name, m.group(1).lower(), m.group(2).lower(),
                           _tipo(m.group(2) + " " + m.group(3))))
        for tabla, cols in _tipos_de_los_create(texto).items():
            salida += [(archivo.name, tabla, c, t) for c, t in cols.items()]
    return salida


def test_el_tipo_de_cada_columna_es_el_mismo_en_la_migracion_y_en_el_esquema():
    """Una base nueva sale de `db/schema.sql` y la de producción, de las
    migraciones: si una columna dice BIGINT en un lado e INTEGER en el otro, las
    dos bases quedan distintas y nadie se entera (el 2-oct se vio con
    `responsable_chat_id`). Se miran TODAS las migraciones."""
    esquema = _tipos_de_los_create(db._sin_comentarios(_SCHEMA.read_text(encoding="utf-8")))
    declaradas = _tipos_que_declaran_las_migraciones()
    assert len(declaradas) > 50, "casi no se leyeron columnas: ¿se rompió el parseo?"
    diferencias = [(a, t, c, tipo, esquema.get(t, {}).get(c, "no está en schema.sql"))
                   for a, t, c, tipo in declaradas
                   if esquema.get(t, {}).get(c, "no está en schema.sql") != tipo]
    assert not diferencias, "\n".join(
        f"{a}: {t}.{c} es {tipo!r} en la migración y {en!r} en schema.sql"
        for a, t, c, tipo, en in diferencias)


def test_la_guarda_de_tipos_ve_un_tipo_distinto_inventado():
    migracion = _tipos_de_los_create("CREATE TABLE x (\n  a BIGINT NOT NULL,\n  b TEXT\n);")
    esquema = _tipos_de_los_create("CREATE TABLE x (\n  a INTEGER NOT NULL,\n  b TEXT\n);")
    assert migracion["x"]["a"] == "bigint" and esquema["x"]["a"] == "integer"
    assert migracion["x"]["b"] == esquema["x"]["b"] == "text"


# ── Los hermanos de `deshacer`: cada columna con puerta dice lo que se rechazó ──

_MALOS = ["", None, -5, 10 ** 13, "zzz-nadie", ["zzz-nadie"], 3.5, "x" * 500]


def _columnas_con_puerta():
    return [(t, c) for t, cs in crud.PUERTAS.items() for c in cs]


def test_cada_columna_con_puerta_tiene_su_frase_en_deshacer():
    """La lista sale de `crud.PUERTAS`: si alguien le agrega una columna, tiene
    que decir qué volvería a quedar, o `deshacer` hablaría de otra cosa."""
    assert set(_columnas_con_puerta()) == set(crud._VUELVE_A)


@pytest.mark.parametrize("tabla,columna", _columnas_con_puerta())
async def test_deshacer_nombra_lo_que_la_puerta_rechazo(base, gente, tabla, columna):
    puerta = crud.PUERTAS[tabla][columna]
    malo, motivo = None, None
    for candidato in _MALOS:
        try:
            puerta(candidato)
        except ValueError as e:
            malo, motivo = candidato, str(e)
            break
    assert motivo is not None, (
        f"no hay un valor de prueba que la puerta {tabla}.{columna} rechace: "
        f"agregá uno a _MALOS")
    _proyecto(base, 1)
    _tarea(base, 1)
    antes = {"id": 1, columna: malo}
    log_id = _huella(base, "editar", tabla, 1, antes, {columna: "otro valor distinto"})
    with pytest.raises(ValueError) as e:
        await crud.deshacer(log_id)
    que = crud._VUELVE_A[(tabla, columna)]
    assert str(e.value) == f"No lo deshice: {que}, y {motivo}.", str(e.value)
    # Y no habla de ninguna de las OTRAS columnas.
    for otra, frase in crud._VUELVE_A.items():
        if frase != que:
            assert frase not in str(e.value), (otra, str(e.value))
