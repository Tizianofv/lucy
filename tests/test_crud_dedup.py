"""Tests de la deduplicación de crear_desde_interpretacion (acciones/crud.py).

Por qué son herméticos (sin Postgres, sin red): el bug era que el agente
re-crea lo que acaba de crear y llegaban recordatorios dobles. Lo que hay que
probar es la DECISIÓN de crear o no crear otra fila, no Postgres. Así que se
stubea psycopg y se corre `crear_desde_interpretacion` contra una conexión de
mentira en memoria (FakeConn) que modela las mismas consultas que emite el
código. No toca ninguna base — de producción, menos.

Correr:  python3 tests/test_crud_dedup.py
(o con pytest si está instalado: pytest tests/test_crud_dedup.py)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types
from datetime import datetime

# ---------------------------------------------------------------------------
# 1) Entorno mínimo + stubs de psycopg ANTES de importar el código real.
#    config.py exige estas variables; db.db crea el pool al importarse. Nada
#    de esto necesita una base viva para lo que probamos.
# ---------------------------------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

_psycopg = types.ModuleType("psycopg")
_psycopg_rows = types.ModuleType("psycopg.rows")
_psycopg_rows.dict_row = object()  # solo tiene que existir como atributo
_psycopg.rows = _psycopg_rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _psycopg_rows)

_psycopg_pool = types.ModuleType("psycopg_pool")


class _StubPool:  # AsyncConnectionPool(DATABASE_URL, open=False)
    def __init__(self, *a, **k):
        pass


_psycopg_pool.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _psycopg_pool)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import db.db as db  # noqa: E402
from acciones import crud  # noqa: E402


def _con_gente(nombres):
    """Casa de mentira: quién puede ser responsable y cómo se llama.

    Igual que `_con_gente` de tests/test_responsable.py, y por el mismo
    motivo: `config.NOMBRES_POR_CHAT` y `config.CHAT_IDS_PERMITIDOS` se leen
    EN CADA LLAMADA, así que alcanza con cambiar el atributo del módulo. El
    fixture autouse de conftest.py devuelve los módulos a su sitio después de
    cada prueba.
    """
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = tuple(nombres)


# ---------------------------------------------------------------------------
# 2) Conexión de mentira: un Postgres en memoria que entiende exactamente las
#    consultas que emite crud.py (dedup, insert, log). Reimplementa la
#    semántica pretendida (IS NOT DISTINCT FROM para fechas NULL incluido).
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, row=None, filas=None):
        self._row = row
        # `filas`, para `db.areas()`: una lista de dicts para `fetchall()`.
        # Si no se pasa, se deriva de `row` (una sola fila) para no romper
        # a nadie que ya usaba `_Cur(row)` esperando solo `fetchone()`.
        self._filas = filas if filas is not None else (
            [row] if row is not None else [])

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._filas


class _CursorGenerico:
    """El `cursor()` que usa `db.areas()` -- ejecuta y se acuerda del
    resultado, a diferencia de `FakeConn.execute()`, que devuelve un `_Cur`
    nuevo cada vez sin que nadie lo retenga."""

    def __init__(self, conn):
        self._conn = conn
        self._cur = None

    async def execute(self, sql, params=None):
        self._cur = await self._conn.execute(sql, params)
        return self._cur

    async def fetchone(self):
        return await self._cur.fetchone()

    async def fetchall(self):
        return await self._cur.fetchall()


class _ErrorSQL(Exception):
    """Un error de Postgres de mentira, con el `sqlstate` que haga falta --
    para probar el `except` del INSERT con área sin necesitar una base real."""

    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(f"error de mentira, sqlstate={sqlstate}")


class _Transaccion:
    """El SAVEPOINT de mentira que usa `crear_desde_interpretacion` (encargo
    4) alrededor del INSERT de `tareas`, para poder caer a la versión sin
    `area` si la columna todavía no existe. Acá nunca falla -- `FakeConn`
    nunca lanza 42703 -- pero el `async with conn.transaction():` del código
    real necesita el método para no reventar con AttributeError antes de
    ejecutar nada."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class FakeConn:
    def __init__(self, sin_columna_area: bool = False, areas=None):
        self.tareas: list[dict] = []
        self.eventos: list[dict] = []
        self.logs: list[dict] = []
        self._ids = {"tareas": 0, "eventos": 0}
        self._logid = 1000
        self.sql: list[tuple[str, tuple]] = []  # todo lo ejecutado, para espiar
        # Simula que `tareas.area` todavía no existe (la migración del
        # encargo 4 no se aplicó): el INSERT de 10 parámetros (con área)
        # revienta con 42703 y `crear_desde_interpretacion` tiene que caer al
        # de 9 (sin área).
        self.sin_columna_area = sin_columna_area
        # Lo que `db.areas()` devuelve -- la puerta `_area_que_vale` valida
        # contra esto. Vacía por omisión: como si la migración no se hubiera
        # aplicado, igual que `sin_columna_area`. Los dos SIEMPRE van juntos
        # en una base real -- la migración crea la tabla `areas` y las
        # columnas `area` en la MISMA transacción -- así que un test que
        # pida un área con `areas=[]` tiene que esperar un rechazo, no un
        # área guardada.
        self.areas = list(areas or [])

    def transaction(self):
        return _Transaccion(self)

    def cursor(self, row_factory=None):
        return _CursorGenerico(self)

    # -- helpers de siembra (una fila que "ya existía") ---------------------
    def seed_tarea(self, titulo, vence, *, estado="pendiente", borrado_en=None,
                  responsable_chat_id=None):
        self._ids["tareas"] += 1
        rid = self._ids["tareas"]
        self.tareas.append({"id": rid, "titulo": titulo, "vence_en": vence,
                            "estado": estado, "borrado_en": borrado_en,
                            "responsable_chat_id": responsable_chat_id})
        self._logid += 1
        self.logs.append({"id": self._logid, "tabla": "tareas",
                          "registro_id": rid, "accion": "crear"})
        return rid, self._logid

    def seed_evento(self, titulo, inicia, *, borrado_en=None):
        self._ids["eventos"] += 1
        rid = self._ids["eventos"]
        self.eventos.append({"id": rid, "titulo": titulo, "inicia_en": inicia,
                             "borrado_en": borrado_en})
        self._logid += 1
        self.logs.append({"id": self._logid, "tabla": "eventos",
                          "registro_id": rid, "accion": "crear"})
        return rid, self._logid

    # -- el "motor SQL" de mentira -----------------------------------------
    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        self.sql.append((s, p))

        if s.startswith("SELECT clave, color FROM areas"):
            return _Cur(filas=list(self.areas))

        if s.startswith("SELECT id FROM tareas WHERE borrado_en IS NULL AND estado"):
            titulo, cuando = p
            hits = [t for t in self.tareas
                    if t["borrado_en"] is None and t["estado"] == "pendiente"
                    and t["titulo"] == titulo and t["vence_en"] == cuando]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("SELECT id FROM eventos WHERE borrado_en IS NULL"):
            titulo, inicia = p
            hits = [e for e in self.eventos
                    if e["borrado_en"] is None
                    and e["titulo"] == titulo and e["inicia_en"] == inicia]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("SELECT id FROM log_acciones WHERE tabla"):
            tabla, rid = p
            hits = [l for l in self.logs if l["tabla"] == tabla
                    and l["registro_id"] == rid and l["accion"] == "crear"]
            return _Cur((hits[-1]["id"],) if hits else None)

        if s.startswith("INSERT INTO tareas"):
            if self.sin_columna_area and len(p) > 9:
                raise _ErrorSQL("42703")
            self._ids["tareas"] += 1
            rid = self._ids["tareas"]
            _bandeja, titulo, _detalle, vence = p[0], p[1], p[2], p[3]
            # El responsable viaja en la posición 8 (encargo 2) y el área en
            # la 9 (encargo 4, SOLO en la versión `con_area` -- la de
            # `sin_area`, a la que se cae si la columna no existe, tiene 9
            # parámetros y no 10). Por índice y no por desempaquetado fijo,
            # así que agregar otra columna al final no le rompe la lectura a
            # nadie más.
            responsable = p[8] if len(p) > 8 else None
            area = p[9] if len(p) > 9 else None
            self.tareas.append({"id": rid, "titulo": titulo, "vence_en": vence,
                                "estado": "pendiente", "borrado_en": None,
                                "responsable_chat_id": responsable,
                                "area": area})
            return _Cur((rid,))

        if s.startswith("INSERT INTO eventos"):
            self._ids["eventos"] += 1
            rid = self._ids["eventos"]
            titulo, inicia = p[1], p[2]
            self.eventos.append({"id": rid, "titulo": titulo, "inicia_en": inicia,
                                 "borrado_en": None})
            return _Cur((rid,))

        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            lid = self._logid
            # Por NOMBRE de columna, leído del propio INSERT -- no por
            # posición fija. `_registrar` agregó `actor` como parámetro
            # (encargo 5, antes iba como literal 'lucy' en el SQL), y una
            # lectura por índice fijo se habría desincronizado en silencio.
            m = re.search(r"log_acciones\s*\(([^)]*)\)", s)
            columnas = [c.strip() for c in m.group(1).split(",")]
            fila = dict(zip(columnas, p))
            self.logs.append({"id": lid, "tabla": fila.get("tabla"),
                              "registro_id": fila.get("registro_id"),
                              "accion": fila.get("accion"),
                              "actor": fila.get("actor")})
            return _Cur((lid,))

        # El responsable de una tarea que YA existía (encargo 2, arreglo del
        # NO PASA sobre eeb07dd): leer lo que tiene de verdad antes de
        # decidir, y escribirlo si corresponde.
        if s.startswith("SELECT responsable_chat_id FROM tareas WHERE id"):
            (rid,) = p
            hit = next((t for t in self.tareas if t["id"] == rid), None)
            return _Cur((hit.get("responsable_chat_id"),) if hit else None)

        if s.startswith("UPDATE tareas SET responsable_chat_id"):
            responsable, rid = p
            for t in self.tareas:
                if t["id"] == rid:
                    t["responsable_chat_id"] = responsable
            return _Cur(None)

        raise AssertionError(f"SQL no modelado por FakeConn: {s[:90]}")


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _PoolCM(self._conn)


def _instalar(conn):
    """Deja a crud.py hablando con la conexión de mentira."""
    db.pool = FakePool(conn)

    async def _cero_persona(_):
        return None

    async def _cero_proyecto(_, bandeja_id=None):
        return None

    db.buscar_o_crear_persona = _cero_persona
    db.buscar_o_crear_proyecto = _cero_proyecto


# ---------------------------------------------------------------------------
# 3) Tests
# ---------------------------------------------------------------------------
async def test_tarea_mismo_titulo_misma_fecha_no_crea_segunda():
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    rid0, log0 = conn.seed_tarea("Comprar café", vence)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Comprar café",
            "cuando": "2026-08-01T10:00:00"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 1, "no debió crear una segunda fila"
    assert rid == rid0, "debió devolver la tarea que ya existía"
    assert log_id == log0, "debió devolver el log de la creación original"


async def test_tarea_mismo_titulo_fecha_distinta_si_crea():
    conn = FakeConn()
    _instalar(conn)
    conn.seed_tarea("Pagar la luz",
                    datetime.fromisoformat("2026-08-01T09:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Pagar la luz",
            "cuando": "2026-09-01T09:00:00"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 2, "misma etiqueta, otra fecha = otra tarea"
    assert rid != conn.tareas[0]["id"]
    assert log_id > 1000, "una creación real escribe un log nuevo"


async def test_tarea_sin_fecha_dedup_por_null():
    # IS NOT DISTINCT FROM: dos tareas sin fecha son la misma; no se duplican.
    conn = FakeConn()
    _instalar(conn)
    rid0, log0 = conn.seed_tarea("Llamar al banco", None)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al banco"})

    assert len(conn.tareas) == 1
    assert (rid, log_id) == (rid0, log0)


async def test_tarea_borrada_no_bloquea():
    # Una fila soft-deleteada no cuenta como duplicado: se crea de nuevo.
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    conn.seed_tarea("Renovar dominio", vence,
                    borrado_en=datetime.fromisoformat("2026-07-01T00:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Renovar dominio",
            "cuando": "2026-08-01T10:00:00"})

    assert len(conn.tareas) == 2, "la borrada no debe frenar una nueva"


async def test_tarea_hecha_no_bloquea():
    # Solo 'pendiente' dedup: una tarea ya 'hecha' no impide recrearla.
    conn = FakeConn()
    _instalar(conn)
    vence = datetime.fromisoformat("2026-08-01T10:00:00")
    conn.seed_tarea("Sacar la basura", vence, estado="hecha")

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Sacar la basura",
            "cuando": "2026-08-01T10:00:00"})

    assert len(conn.tareas) == 2


# ── Encargo 2: el responsable, al crear ──────────────────────────────────
#
# «crea X para Rosi» tiene que quedar en UN paso: nace con Rosi ya puesta, sin
# crear y después editar. La validación es la MISMA función que ya usa
# `editar` (`acciones/crud.py::_por_las_puertas`, con `PUERTAS["tareas"]`), no
# una copia del criterio para crear.

DUENO = 424242
ROSI = 700000001
AJENO = 700000999   # tiene nombre pero NO puede ser responsable (no entra)


async def test_responsable_valido_se_guarda_al_crear_en_un_solo_paso():
    """«crea X para Rosi»: la tarea nace YA con el chat de Rosi, sin un
    segundo paso de `editar`."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    tabla, rid, _log = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al dentista",
            "responsable_chat_id": "Rosi"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 1, "tiene que crear UNA tarea, no dos pasos"
    assert conn.tareas[0]["id"] == rid
    assert conn.tareas[0]["responsable_chat_id"] == ROSI, (
        "el responsable no quedó puesto en la misma creación")


async def test_un_numero_de_chat_tambien_vale_como_responsable():
    """El responsable, de tres formas — acá la del número — y las tres
    terminan en la misma puerta que usa `editar` (ver
    `acciones/crud.py::_responsable_que_vale`)."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Pagar el internet",
            "responsable_chat_id": ROSI})

    assert conn.tareas[0]["responsable_chat_id"] == ROSI


async def test_responsable_que_no_vale_no_crea_NADA():
    """Si el nombre pedido no vale, `crear` se rechaza ENTERO: no crea la
    tarea sin responsable como si no se hubiera pedido nada — eso perdería
    en silencio el «para Rosi» que sí se pidió."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "Comprar pintura",
                "responsable_chat_id": "un nombre que no es de nadie"})
        assert False, "debió rechazar un responsable que no vale"
    except ValueError as e:
        assert "no creé la tarea" in str(e).lower()

    assert conn.tareas == [], "no debió escribir nada"
    assert conn.logs == [], "no debió dejar ninguna huella"


async def test_un_chat_que_no_puede_ser_responsable_tambien_se_rechaza():
    """Tiene nombre pero no entra al panel: la puerta lo conoce y lo rechaza
    igual que al editar (`config.puede_ser_responsable`)."""
    _con_gente({DUENO: "Tiziano", AJENO: "Un Ajeno"})
    config.CHAT_IDS_PERMITIDOS = (DUENO,)  # AJENO tiene nombre, no permiso
    conn = FakeConn()
    _instalar(conn)

    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "Revisar el aire",
                "responsable_chat_id": AJENO})
        assert False, "debió rechazar a alguien que no puede ser responsable"
    except ValueError:
        pass
    assert conn.tareas == []


async def test_sin_responsable_todo_queda_igual_que_hoy():
    """No mandar el campo (el caso normal, hoy) no cambia nada: se crea igual
    y sin responsable, como antes de este encargo."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Sacar la basura"})

    assert len(conn.tareas) == 1
    assert conn.tareas[0]["responsable_chat_id"] is None


async def test_una_cita_con_responsable_no_lo_escribe_ni_lo_valida():
    """`responsable_chat_id` es de TAREAS. Una cita que traiga ese campo por
    error no lo escribe -- ni siquiera lo valida-- porque no es su columna."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    tabla, rid, _log = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Dentista",
            "cuando": "2026-08-01T10:00:00",
            "responsable_chat_id": "un nombre que no es de nadie"})

    assert tabla == "eventos"
    assert len(conn.eventos) == 1, (
        "una clasificación que no valga como responsable no puede tumbar la "
        "creación de una cita: ese campo no es suyo")


# ── El área (encargo 4) ────────────────────────────────────────────────
#
# Vuelta corregida tras el NO PASA del testigo sobre `e94b37a`: el área tiene
# que validarse en CÓDIGO, con el mismo tipo de mensaje que ya usa la
# categoría de un movimiento (`crud.editar`, más abajo en este archivo) --
# eso NO es una guarda sobre lo que escribe el modelo, es vocabulario
# cerrado, igual que las categorías. `crud._area_que_vale` es la única
# función que decide esto, para `crear_desde_interpretacion` y para
# `editar()` (tareas y proyectos) por igual.
#
# LA FRONTERA que SÍ sigue en pie: la FK `area → areas.clave` y el CHECK
# `tareas_area_no_con_proyecto` son la red de seguridad de la BASE, y no se
# pueden probar sin Postgres -- `FakeConn` no los modela. Lo que se prueba
# acá es que el CÓDIGO rechace antes de llegar a esa red, con un motivo
# legible, en los sitios que la prueba de hermanos (más abajo) puede ver --
# que no son todos: su propio límite está declarado ahí, con la misma forma
# de esta frase que un testigo encontró prometiendo de más (NO PASA sobre
# `522fedf`).

async def test_el_area_pedida_se_guarda_si_la_tarea_no_tiene_proyecto():
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(areas=[{"clave": "CDS", "color": "#2b6cb0"}])
    _instalar(conn)  # buscar_o_crear_proyecto -> None, siempre

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "grabar la intro",
            "area": "CDS"})

    assert len(conn.tareas) == 1
    assert conn.tareas[0]["area"] == "CDS", (
        f"el área pedida no se guardó: {conn.tareas[0]}")


async def test_sin_pedir_area_la_tarea_queda_sin_area():
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(areas=[{"clave": "CDS", "color": "#2b6cb0"}])
    _instalar(conn)

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "ordenar cables"})

    assert conn.tareas[0]["area"] is None


async def test_un_area_que_no_existe_rechaza_la_creacion_entera():
    """Vocabulario cerrado, como las categorías: una clave que no está en
    `db.areas()` no crea la tarea a medias sin área -- la rechaza entera, con
    un mensaje que dice cuáles hay, igual que un responsable que no vale."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(areas=[{"clave": "CDS", "color": "#1"},
                           {"clave": "ACD", "color": "#2"}])
    _instalar(conn)

    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "algo",
                "area": "Marketing"})
        assert False, "debió rechazar un área que no existe"
    except ValueError as e:
        mensaje = str(e)
        assert "Marketing" in mensaje
        assert '"CDS"' in mensaje and '"ACD"' in mensaje, (
            f"el motivo no dice cuáles áreas hay: {mensaje!r}")
    assert conn.tareas == [], "no debió crear nada"


async def test_el_area_se_ignora_en_silencio_si_la_tarea_tiene_proyecto():
    """SEGUNDA VUELTA (Tiziano corrigió el diseño anterior tras el NO PASA
    sobre `2d8451c`): «la tarea con proyecto HEREDA el área del proyecto.
    Cuando una tarea pasa a tener proyecto, su área propia se limpia sola,
    no se rechaza el pedido». Si Lucy manda "area" Y "proyecto" juntos, la
    tarea se crea igual -- CON el proyecto -- y el área pedida se descarta
    sin avisar: no hace falta, porque la que cuenta sale del proyecto."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(areas=[{"clave": "CDS", "color": "#1"}])

    async def _cero_persona(_):
        return None

    async def _con_proyecto_77(_, bandeja_id=None):
        return 77

    db.pool = FakePool(conn)
    db.buscar_o_crear_persona = _cero_persona
    db.buscar_o_crear_proyecto = _con_proyecto_77

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "algo del proyecto",
            "proyecto": "Álbum nuevo", "area": "CDS"})

    assert len(conn.tareas) == 1, "la tarea sí se tenía que crear"
    assert conn.tareas[0]["area"] is None, (
        f"el área pedida tenía que ignorarse, no guardarse: {conn.tareas[0]}")


async def test_sin_pedir_area_se_crea_igual_con_proyecto():
    """Sin "area" en el pedido no hay nada que rechazar: una tarea con
    proyecto y sin área pedida se crea normal, como siempre."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(areas=[{"clave": "CDS", "color": "#1"}])

    async def _cero_persona(_):
        return None

    async def _con_proyecto_77(_, bandeja_id=None):
        return 77

    db.pool = FakePool(conn)
    db.buscar_o_crear_persona = _cero_persona
    db.buscar_o_crear_proyecto = _con_proyecto_77

    await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "algo del proyecto",
            "proyecto": "Álbum nuevo"})

    assert len(conn.tareas) == 1
    assert conn.tareas[0]["area"] is None


async def test_sin_pedir_area_se_crea_igual_si_la_columna_no_existe():
    """La migración del encargo 4 puede no haber corrido todavía. Sin
    "area" pedida no hay nada que validar (la puerta ni llama a
    `db.areas()`), así que el INSERT con área (10 parámetros, área en NULL)
    revienta con 42703 igual -- porque la COLUMNA no existe, aunque el valor
    sea NULL -- y el código cae al INSERT de 9 -- sin área -- en vez de
    fallar la creación entera."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(sin_columna_area=True)
    _instalar(conn)

    tabla, rid, _log = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "sin migrar todavía"})

    assert tabla == "tareas"
    assert len(conn.tareas) == 1, (
        "con la columna ausente, la tarea tiene que crearse igual")
    assert conn.tareas[0]["area"] is None
    insert_tareas = [s for s, _ in conn.sql if s.startswith("INSERT INTO tareas")]
    assert len(insert_tareas) == 2, (
        f"tenía que intentar con área (y fallar) y después sin área: "
        f"{insert_tareas}")


async def test_pedir_un_area_si_la_columna_no_existe_se_rechaza_con_motivo():
    """Y si SÍ se pide un área mientras la migración no corrió -- tabla
    `areas` y columna `tareas.area` van en la MISMA transacción, así que las
    dos faltan juntas --, la puerta la rechaza ANTES de tocar la base: ningún
    área es válida todavía, y el motivo lo dice, en vez de un texto crudo de
    Postgres o una tarea creada fingiendo que el área se guardó."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn(sin_columna_area=True)  # areas=[] por omisión: coherente
    _instalar(conn)

    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "sin migrar todavía",
                "area": "CDS"})
        assert False, "debió rechazar: no hay ningún área declarada todavía"
    except ValueError as e:
        assert "ninguna" in str(e).lower()
    assert conn.tareas == []


# ── `crud.editar` sobre `area` (encargo 4, NO PASA sobre e94b37a) ─────────
#
# El testigo encontró que `editar()` no validaba `area` en absoluto -- ni
# contra `db.areas()`, ni contra "esta tarea ya tiene proyecto" -- así que
# los dos casos pasaban derecho hasta la FK/CHECK de Postgres, que revienta
# con su texto crudo. Estos tests corren `crud.editar` DE VERDAD, con
# `crud._area_que_vale` sin mockear -- la MISMA función que ya prueban los
# tests de `crear_desde_interpretacion` de arriba.

class _CurEditarArea:
    """Sirve los `SELECT * FROM <tabla>` (antes y después del UPDATE) y el
    `SELECT clave, color FROM areas` que ahora pide `_area_que_vale`."""

    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT clave, color FROM areas"):
            self._filas = list(self._conn.areas)
            return self
        if s.startswith("SELECT * FROM"):
            self._row = dict(self._conn.fila)
            return self
        raise AssertionError(f"SQL no modelado por _CurEditarArea: {s[:90]}")

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._filas


class _ConnEditarArea:
    """Modela lo justo para `crud.editar` sobre UNA fila de `tareas` o
    `proyectos`, con soporte para `db.areas()` (el SAVEPOINT que usa
    `_area_que_vale` a través de `db.areas()`)."""

    def __init__(self, fila, areas=None):
        self.fila = dict(fila)
        self.campos_guardados: dict | None = None
        self.areas = list(areas or [])
        self._logid = 3000

    def cursor(self, row_factory=None):
        return _CurEditarArea(self)

    def transaction(self):
        return _Transaccion(self)

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or ()
        if s.startswith("UPDATE"):
            asignaciones = s.split(" SET ", 1)[1].split(" WHERE ")[0]
            columnas = [a.split("=")[0].strip() for a in asignaciones.split(", ")]
            self.campos_guardados = dict(zip(columnas, p))
            self.fila.update(self.campos_guardados)
            return _Cur(None)
        if s.startswith("INSERT INTO log_acciones"):
            self._logid += 1
            return _Cur((self._logid,))
        raise AssertionError(f"SQL no modelado por _ConnEditarArea: {s[:90]}")


def _fila_tarea_area(**extra):
    fila = {"id": 40, "bandeja_id": 1, "titulo": "Mezclar el tema",
            "detalle": None, "vence_en": None, "estado": "pendiente",
            "recurrencia": None, "pospuesta_veces": 0, "anticipos_min": [0],
            "avisos_enviados": [], "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, 9, 0),
            "responsable_chat_id": None, "proyecto_id": None, "area": None}
    fila.update(extra)
    return fila


def _fila_proyecto_area(**extra):
    fila = {"id": 5, "nombre": "Álbum nuevo", "borrado_en": None,
            "creado_en": datetime(2026, 8, 1, 9, 0), "area": None}
    fila.update(extra)
    return fila


async def test_editar_guarda_el_area_en_una_tarea_sin_proyecto():
    conn = _ConnEditarArea(_fila_tarea_area(),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"area": "CDS"}, motivo="test")
    assert despues["area"] == "CDS"
    assert log_id is not None


async def test_editar_rechaza_un_area_que_no_esta_declarada():
    conn = _ConnEditarArea(_fila_tarea_area(),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    try:
        await crud.editar("tareas", 40, {"area": "Marketing"}, motivo="test")
        assert False, "debió rechazar un área que no existe"
    except ValueError as e:
        assert "Marketing" in str(e) and '"CDS"' in str(e)
    assert conn.campos_guardados is None, "no debió escribir nada"


async def test_editar_ignora_el_area_pedida_si_la_tarea_ya_tiene_proyecto():
    """SEGUNDA VUELTA (el testigo sobre `2d8451c` encontró que `editar` no
    miraba `antes["proyecto_id"]` en absoluto -- eso llegaba derecho a la
    FK/CHECK de Postgres -- y Tiziano corrigió además el diseño: ya no se
    rechaza, se limpia sola). Pedir un área explícita sobre una tarea que ya
    tiene proyecto no falla: se edita igual, con el área quedando en None."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=5),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"area": "CDS"}, motivo="test")
    assert despues["area"] is None, (
        f"el área pedida tenía que ignorarse, no guardarse: {despues}")
    assert log_id is not None


async def test_editar_ignora_el_area_si_el_proyecto_se_pone_en_el_mismo_pedido():
    """Y si el proyecto se le pone EN ESTE MISMO `editar` -- junto con el
    área, en un solo pedido --, la pregunta es la misma: ¿con qué proyecto
    va a quedar DESPUÉS de esto? El área se ignora igual, mirando
    `campos["proyecto_id"]` y no solo `antes["proyecto_id"]`."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=None),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"proyecto_id": 5, "area": "CDS"}, motivo="test")
    assert despues["proyecto_id"] == 5
    assert despues["area"] is None, (
        f"el área pedida tenía que ignorarse al ponerle proyecto: {despues}")


async def test_poner_proyecto_le_limpia_el_area_sola_sin_tocar_area():
    """EL CASO DEL TESTIGO, textual: "poné esta tarea en el proyecto X"
    -- SIN mencionar "area" para nada -- sobre una tarea que YA tenía un
    área propia. Antes de este arreglo, la fila quedaba con `proyecto_id`
    puesto Y el área vieja intacta: un estado que solo el CHECK de la base
    atajaba, con su texto crudo. Ahora `editar` lo resuelve solo, en el
    mismo paso, sin que nadie tenga que pedirlo aparte."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=None, area="CDS"),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"proyecto_id": 5}, motivo="test")
    assert despues["proyecto_id"] == 5
    assert despues["area"] is None, (
        f"poner proyecto tenía que limpiar el área vieja sola: {despues}")
    assert conn.campos_guardados["area"] is None, (
        "el UPDATE tiene que escribir area=NULL de verdad, no solo el dict "
        "de retorno")


async def test_quitar_el_proyecto_deja_la_tarea_sin_area_no_recupera_la_vieja():
    """Sacarle el proyecto a una tarea la deja SIN área hasta que alguien le
    ponga una -- no revive ninguna área anterior a que tuviera el
    proyecto, porque esa información ya no está en ningún lado (nunca se
    guardó: mientras tuvo proyecto, `tareas.area` se mantuvo en NULL)."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=5, area=None),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"proyecto_id": None}, motivo="test")
    assert despues["proyecto_id"] is None
    assert despues["area"] is None


async def test_cambiar_de_proyecto_sigue_sin_area_propia():
    """Cambiar de un proyecto a OTRO proyecto: la tarea sigue sin área
    propia -- la sigue heredando, ahora del proyecto nuevo -- y si tenía
    algo en `area` (no debería, pero por las dudas) se limpia igual."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=5, area=None),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"proyecto_id": 9}, motivo="test")
    assert despues["proyecto_id"] == 9
    assert despues["area"] is None
    # Y NO se tocó "area" en el UPDATE -- no hacía falta, ya estaba en
    # None y sigue en None -- así que una base sin la columna `area`
    # todavía (migración no aplicada) no revienta por un cambio de
    # proyecto que ni siquiera necesitaba tocarla.
    assert "area" not in (conn.campos_guardados or {}), (
        f"no hacía falta escribir 'area' acá: {conn.campos_guardados}")


async def test_editar_area_null_no_se_rechaza_aunque_tenga_proyecto():
    """Quitarle el área a una tarea (`"area": null`) es un no-op inofensivo
    incluso si tiene proyecto: no hay nada que rechazar, porque no se está
    pidiendo un área propia."""
    conn = _ConnEditarArea(_fila_tarea_area(proyecto_id=5, area=None),
                           areas=[{"clave": "CDS", "color": "#1"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "tareas", 40, {"area": None}, motivo="test")
    assert despues["area"] is None
    assert log_id is not None


async def test_editar_guarda_el_area_en_un_proyecto_siempre():
    """En un PROYECTO no hay "ya tiene proyecto" que mirar -- se valida
    solo contra `db.areas()`."""
    conn = _ConnEditarArea(_fila_proyecto_area(),
                           areas=[{"clave": "ACD", "color": "#2"}])
    db.pool = FakePool(conn)
    despues, log_id = await crud.editar(
        "proyectos", 5, {"area": "ACD"}, motivo="test")
    assert despues["area"] == "ACD"


async def test_editar_rechaza_un_area_de_proyecto_que_no_existe():
    conn = _ConnEditarArea(_fila_proyecto_area(),
                           areas=[{"clave": "ACD", "color": "#2"}])
    db.pool = FakePool(conn)
    try:
        await crud.editar("proyectos", 5, {"area": "Marketing"}, motivo="t")
        assert False, "debió rechazar un área que no existe"
    except ValueError as e:
        assert "Marketing" in str(e)
    assert conn.campos_guardados is None


# ── LA PUERTA ÚNICA, y hasta dónde la prueba de abajo puede verlo ────────

def test_todo_lo_que_toca_area_o_proyecto_id_en_crud_pasa_por_la_misma_puerta():
    """Se recorre CADA función de `acciones/crud.py` que mencione `area` O
    `proyecto_id` COMO CLAVE DE DICCIONARIO ENTRECOMILLADA en su cuerpo --
    `campos["area"]`, `{"proyecto_id": ...}` -- y se exige que TODAS llamen a
    `_area_que_vale`.

    POR QUÉ TAMBIÉN `proyecto_id`, no solo `area` (segunda vuelta, NO PASA
    sobre `2d8451c`): el testigo encontró que `editar("tareas", id,
    {"proyecto_id": 5}, ...)` -- SIN mencionar "area" para nada -- dejaba una
    fila con proyecto puesto y el área vieja intacta, porque la primera
    versión de esta prueba (y del código) solo miraba si la función
    mencionaba "area". Una función puede decidir el área de una tarea sin
    escribir la palabra "area" en esa rama -- alcanza con tocar
    `proyecto_id`, porque `_area_que_vale` mira el proyecto para decidir el
    área. Así que ahora la lista de "sitios a vigilar" se deriva de las DOS
    palabras, no de una.

    LA FRONTERA (corregida -- un testigo, NO PASA sobre `522fedf`, encontró
    que la frase que había acá antes prometía de más, con la MISMA forma que
    ya se había corregido dos veces en este archivo): esto ve un sitio que
    escribe `area`/`proyecto_id` como CLAVE DE DICT -- `campos["area"] = ...`,
    `{"proyecto_id": x}` --, porque así es como `ast.unparse` deja el
    nombre, entre comillas. NO VE un sitio que escriba la columna como TEXTO
    SUELTO dentro de una cadena de SQL crudo -- `"UPDATE tareas SET area = %s
    WHERE id = %s"` -- porque ahí `ast.unparse` deja la cadena ENTERA entre
    comillas y la palabra `area` queda suelta en el medio, sin las suyas
    propias: ni `'area'` ni `"area"` aparecen como subcadena. Y este archivo
    YA TIENE un sitio así, para otra columna: `acciones/crud.py:332`,
    `"UPDATE tareas SET responsable_chat_id = %s WHERE id = %s"`, dentro de
    `crear_desde_interpretacion`. Nada impide que mañana alguien escriba
    `area` de la misma forma -- esta prueba no lo vería, y el propio testigo
    lo demostró mutando: reescribir la limpieza del área como un `UPDATE`
    crudo con `area` suelta en el texto deja esta prueba en VERDE. (La
    mutación de control -- la misma idea pero con `area` como clave de dict
    -- SÍ la atrapa; eso es lo que de verdad está probado.)

    LO QUE QUEDA DE RED en ese caso, y no es poco: el CHECK
    `tareas_area_no_con_proyecto` y la FK `area → areas.clave` siguen ahí, en
    la base -- ver la frontera de la FK/CHECK más arriba en este archivo. Un
    `UPDATE` crudo que dejara `area` y `proyecto_id` inconsistentes lo
    rechazaría igual. Lo que se pierde no es la integridad del dato: es el
    mensaje claro -- Tiziano vería el texto crudo de psycopg
    (`CheckViolation: ...`) en vez de "esa tarea tiene proyecto, y el área
    sale del proyecto", que es justo el defecto que esta puerta se armó para
    evitar.

    QUÉ HARÍA FALTA PARA CERRARLO DE VERDAD, para quien lo retome: no basta
    con buscar `'area'`/`'proyecto_id'` como subcadena en el texto ya
    renderizado -- hay que mirar la ESTRUCTURA de cada literal de SQL, no su
    texto. El patrón ya existe en este repo, dos veces: `tests/
    test_panel_tareas.py::_sql_de` saca del árbol de sintaxis los literales
    de cadena de una función (sin su docstring) y los concatena para poder
    aplicarles una expresión regular al SQL de verdad, no a la prosa que lo
    explica; y `tests/test_backup_alerta.py::
    test_las_columnas_que_el_codigo_inserta_existen_en_el_esquema` saca
    nombres de columna de sentencias `INSERT INTO tabla (col1, col2, ...)`
    con una expresión regular sobre el texto de esos literales. Lo que
    falta acá es lo mismo aplicado a `UPDATE tabla SET col = ...`: extraer
    cada `col` de cada `UPDATE` literal de `crud.py` con una regex tipo
    `r"UPDATE\\s+(\\w+)\\s+SET\\s+([^WHERE]+)"`, partir las asignaciones por
    coma y comparar los nombres de columna contra los que sí pasan por
    `_area_que_vale` -- sumando esa lista a la que ya arma este `ast.walk`
    en vez de reemplazarla, porque los dos caminos (dict genérico de
    `editar()`, `UPDATE` literal de `crear_desde_interpretacion`) son reales
    y conviven en el mismo archivo.
    """
    import ast
    import inspect

    fuente = inspect.getsource(crud)
    arbol = ast.parse(fuente)
    culpables = []
    vistas = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if nodo.name == "_area_que_vale":
            continue  # la puerta misma, obviamente menciona las dos
        texto = ast.unparse(nodo)
        # Busca el nombre ENTRE SUS PROPIAS COMILLAS -- `'area'`, `"area"` --
        # que es como queda una clave de dict (`campos["area"]`) al pasar por
        # `ast.unparse`. NO ve `area` suelta dentro de un literal de SQL más
        # grande (`"UPDATE tareas SET area = %s"`): ahí las comillas que
        # `ast.unparse` pone son las de LA CADENA ENTERA, no las de la
        # palabra `area`. Ver el docstring de esta prueba, sección
        # "LA FRONTERA", para el sitio real de este archivo que ya escribe
        # así (`crud.py:332`, con `responsable_chat_id`) y lo que haría
        # falta para verlo también.
        menciona_area = '"area"' in texto or "'area'" in texto
        menciona_proyecto = '"proyecto_id"' in texto or "'proyecto_id'" in texto
        if not (menciona_area or menciona_proyecto):
            continue
        vistas.append(nodo.name)
        if "_area_que_vale(" not in texto:
            culpables.append(nodo.name)
    assert vistas, ("no se encontró ninguna función que mencione 'area' ni "
                    "'proyecto_id' -- la prueba dejó de medir algo")
    assert set(vistas) == {"crear_desde_interpretacion", "editar"}, (
        f"aparecieron funciones nuevas que tocan 'area' o 'proyecto_id': "
        f"{set(vistas) - {'crear_desde_interpretacion', 'editar'}}. Revisá "
        f"si pasan por _area_que_vale y agregalas a la lista esperada de "
        f"esta prueba.")
    assert not culpables, (
        f"estas funciones de crud.py mencionan 'area' o 'proyecto_id' sin "
        f"llamar a _area_que_vale: {culpables}")


# ── El testigo sobre eeb07dd: el duplicado NO PUEDE tirar el responsable ──
#
# `_duplicado_pendiente` devuelve la fila existente sin ejecutar el INSERT,
# así que `responsable_chat_id` — ya validado más arriba — se perdía en
# silencio: "OK" sin que la fila cambiara. Los tres casos, y los tres se
# deciden contra lo que la fila YA TIENE, no contra lo que se pidió.

async def test_duplicado_sin_responsable_se_lo_pone_y_deja_huella_de_editar():
    """La tarea ya existía y no tenía responsable: se le pone, con una huella
    'editar' (no 'crear') para que el botón de deshacer apunte a ESTA
    edición y no a la creación original."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)
    rid0, log0 = conn.seed_tarea("Llamar al dentista", None)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al dentista",
            "responsable_chat_id": "Rosi"})

    assert (tabla, rid) == ("tareas", rid0), "no debió crear una segunda fila"
    assert len(conn.tareas) == 1
    assert conn.tareas[0]["responsable_chat_id"] == ROSI, (
        "el responsable pedido sobre el duplicado no quedó puesto")
    assert log_id != log0, (
        "el asa devuelta sigue siendo la de la creación original: deshacer "
        "esto archivaría la tarea entera en vez de solo quitarle el "
        "responsable que se le acaba de poner")
    huella = next(l for l in conn.logs if l["id"] == log_id)
    assert huella["accion"] == "editar", (
        "la huella de poner el responsable tiene que ser un 'editar', igual "
        "que si se hubiera cambiado a mano")


async def test_duplicado_con_el_mismo_responsable_no_toca_nada():
    """Ya tiene a Rosi y se vuelve a pedir Rosi: nada que hacer, nada que
    avisar, y CERO escrituras nuevas — ni UPDATE ni huella."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)
    rid0, log0 = conn.seed_tarea("Llamar al dentista", None,
                                 responsable_chat_id=ROSI)
    logs_antes = len(conn.logs)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "tarea", "titulo": "Llamar al dentista",
            "responsable_chat_id": "Rosi"})

    assert (tabla, rid, log_id) == ("tareas", rid0, log0)
    assert conn.tareas[0]["responsable_chat_id"] == ROSI
    assert len(conn.logs) == logs_antes, (
        "pedir el mismo responsable que ya tenía no debía escribir ninguna "
        "huella nueva")


async def test_duplicado_con_OTRO_responsable_no_se_pisa_y_avisa():
    """Ya la tiene Tiziano y alguien pide "para Rosi": NO se reasigna en
    silencio. Se corta con información -el nombre de quien la tiene, nunca
    el chat- para que el modelo se lo pueda contar a quien lo pidió."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)
    conn.seed_tarea("Llamar al dentista", None, responsable_chat_id=DUENO)

    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "Llamar al dentista",
                "responsable_chat_id": "Rosi"})
        assert False, "debió avisar en vez de reasignar en silencio"
    except ValueError as e:
        mensaje = str(e)
        assert "Tiziano" in mensaje, (
            f"el aviso no dice quién la tiene de verdad: {mensaje!r}")
        assert str(DUENO) not in mensaje, (
            "el aviso no puede enseñar el número de chat")

    # Y no se tocó nada: sigue siendo de Tiziano, sin UPDATE ni huella nueva.
    assert conn.tareas[0]["responsable_chat_id"] == DUENO
    assert not any(s.startswith("UPDATE") for s, _ in conn.sql), (
        "no debía haber ningún UPDATE: el responsable existente no se toca")


async def test_crear_pasa_por_la_MISMA_puerta_que_usa_editar():
    """LA prueba de que no hay una copia del criterio: se espía
    `crud._por_las_puertas` -la función que usa `editar()`- y se comprueba
    que `crear_desde_interpretacion` la llama con la MISMA tabla y el MISMO
    valor. Si mañana alguien le escribiera a `crear` su propio chequeo del
    responsable en vez de reusar la puerta, este espía no vería la llamada y
    la prueba se pondría roja."""
    _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    conn = FakeConn()
    _instalar(conn)

    llamadas = []
    original = crud._por_las_puertas

    def _espia(tabla, valores):
        llamadas.append((tabla, dict(valores)))
        return original(tabla, valores)

    crud._por_las_puertas = _espia
    try:
        await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "Avisar a Rosi",
                "responsable_chat_id": "Rosi"})
    finally:
        crud._por_las_puertas = original

    assert ("tareas", {"responsable_chat_id": "Rosi"}) in llamadas, (
        "crear_desde_interpretacion no llamó a _por_las_puertas -la misma "
        "función que editar()- con el responsable pedido")


async def test_cita_mismo_titulo_mismo_inicio_no_crea_segunda():
    conn = FakeConn()
    _instalar(conn)
    inicia = datetime.fromisoformat("2026-08-02T15:00:00")
    rid0, log0 = conn.seed_evento("Dentista", inicia)

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Dentista",
            "cuando": "2026-08-02T15:00:00"})

    assert tabla == "eventos"
    assert len(conn.eventos) == 1, "no debió crear una segunda cita"
    assert (rid, log_id) == (rid0, log0)


async def test_cita_mismo_titulo_inicio_distinto_si_crea():
    conn = FakeConn()
    _instalar(conn)
    conn.seed_evento("Reunión de equipo",
                     datetime.fromisoformat("2026-08-02T15:00:00"))

    tabla, rid, log_id = await crud.crear_desde_interpretacion(
        1, {"clasificacion": "cita", "titulo": "Reunión de equipo",
            "cuando": "2026-08-09T15:00:00"})

    assert tabla == "eventos"
    assert len(conn.eventos) == 2, "otra hora de inicio = otra cita"


def test_la_categoria_por_telegram_pasa_por_el_mismo_vocabulario():
    """Corregir por Telegram tiene que ser tan estricto como corregir en el
    panel. El panel ya valida contra la lista cerrada; sin esto, un "ponelo en
    supermercado" en minúscula partía el total en dos para siempre. Un
    vocabulario que solo se respeta en una de las dos puertas no es cerrado.
    """
    import inspect
    from acciones import crud
    fuente = inspect.getsource(crud.editar)
    assert "CATEGORIAS" in fuente, "editar no valida la categoría"
    assert "aprender_categoria" in fuente, (
        "corregir por Telegram no enseña, y por el panel sí: dos caminos que "
        "dan resultados distintos para la misma corrección")


def test_el_agente_conoce_el_codigo_y_las_categorias_del_codigo():
    """El prompt no puede llevar la lista de categorías copiada a mano: se
    desincroniza el día que se agregue una, y el agente le ofrecería a Tiziano
    categorías que ya no existen. Se inyecta desde CATEGORIAS."""
    import os
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fuente = open(os.path.join(raiz, "cerebro", "agente.py"),
                  encoding="utf-8").read()
    assert "EL CÓDIGO M-####" in fuente, "el agente no sabe leer M-0086"
    assert "{CATEGORIAS}" in fuente, "el marcador de categorías desapareció"
    assert 'HERRAMIENTAS.replace(' in fuente, (
        "las categorías ya no se inyectan desde el código")
    # Y ninguna categoría puede estar escrita a mano en el prompt.
    from cerebro.bancos.categorias import CATEGORIAS
    bloque = fuente[fuente.index("HERRAMIENTAS = "):fuente.index("{CATEGORIAS}")]
    a_mano = [c for c in CATEGORIAS if f'"{c}"' in bloque]
    assert not a_mano, f"categorías copiadas a mano en el prompt: {a_mano}"


class _ConnMovimiento:
    """Conexión de mentira mínima para ejercitar `editar` sobre movimientos.

    Existe porque el test que había NO EJECUTABA `editar`: hacía grep sobre el
    código fuente buscando las palabras "CATEGORIAS" y "aprender_categoria".
    Eso comprueba que el texto está escrito, no que la validación corra — y el
    hueco que encontró el testigo (cambiar `tipo` sin tocar `categoria` saltaba
    las dos comprobaciones) vive justo en el camino que ese grep no recorría.
    """

    def __init__(self, fila):
        self.fila = dict(fila)
        self.logs: list[tuple] = []

    def cursor(self, row_factory=None):
        return self

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT * FROM movimientos"):
            self._ultimo = dict(self.fila)
        elif s.startswith("UPDATE movimientos SET"):
            cols = [c.split("=")[0].strip()
                    for c in s[len("UPDATE movimientos SET"):].split("WHERE")[0].split(",")]
            for col, val in zip(cols, params):
                self.fila[col] = val
            self._ultimo = None
        elif "INSERT INTO log_acciones" in s:
            self.logs.append(params)
            self._ultimo = (1,)
        else:
            self._ultimo = None
        return self

    async def fetchone(self):
        return self._ultimo


class _PoolMovimiento:
    def __init__(self, conn):
        self._c = conn

    def connection(self):
        return _PoolCM(self._c) if "_PoolCM" in globals() else _CM(self._c)


class _CM:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


async def test_cambiar_el_tipo_no_puede_dejar_un_rubro_en_un_ingreso():
    """Lo encontró el testigo. `editar` comprobaba `categoria_permitida` SOLO
    si "categoria" venía en los cambios. Editar únicamente `tipo` —"el M-86 en
    realidad es un ingreso"— saltaba las dos validaciones y dejaba un ingreso
    con categoría "Restaurantes": justo el estado que la regla dice que no
    puede existir, y que ensucia los totales por rubro con dinero que entró.
    """
    from acciones import crud
    conn = _ConnMovimiento({"id": 86, "tipo": "gasto", "categoria": "Restaurantes",
                            "contraparte": "SM NACIONAL", "bandeja_id": None,
                            "borrado_en": None})
    real_pool, real_aprender = db.pool, db.aprender_categoria

    async def _nada(*a, **k):
        return None

    db.pool = _PoolMovimiento(conn)
    db.aprender_categoria = _nada
    try:
        await crud.editar("movimientos", 86, {"tipo": "ingreso"}, "corrijo tipo")
    finally:
        db.pool, db.aprender_categoria = real_pool, real_aprender

    assert conn.fila["tipo"] == "ingreso", "no aplicó el cambio pedido"
    assert not conn.fila["categoria"], (
        f"quedó un ingreso con categoría {conn.fila['categoria']!r}: "
        "los rubros dicen EN QUÉ se gastó, y esto ya no es un gasto")


def test_el_agente_no_duplica_un_movimiento_que_ya_trajo_el_banco():
    """El 1-sep, procesando "dame todas las tareas pendientes", Lucy anotó una
    transferencia de RD$18,280 que el correo de Banreservas ya había guardado.

    El camino automático calcula una huella y ON CONFLICT lo frena; el manual
    no tiene huella, así que nada lo paraba. Y ninguna comparación de texto los
    hubiera juntado: el banco escribió "ROSILIS ... → WENDY MARISOL CANELA
    CRUZ" y el agente "WENDY MARISOL CANELA CRUZ".

    Se compara por fecha, monto y moneda — lo único que las dos versiones no
    pueden escribir distinto — y solo contra filas que vinieron del banco
    (hash_contenido no nulo).
    """
    import inspect
    from acciones import crud
    fuente = inspect.getsource(crud.crear_desde_interpretacion)
    # El límite es el INSERT, que es donde acaba de verdad la comprobación —
    # no un número de caracteres a ojo, que ya me falló antes: un comentario
    # largo empuja la línea fuera y el test falla por su propio recorte.
    i = fuente.index("tabla = \"movimientos\"")
    bloque = fuente[i:fuente.index("INSERT INTO movimientos", i)]
    assert "hash_contenido IS NOT NULL" in bloque, (
        "no compara contra lo que trajo el banco")
    for campo in ("fecha = %s", "monto = %s", "moneda = %s"):
        assert campo in bloque, f"la comparación no usa {campo}"
    assert "FaltanDatos" in bloque, (
        "tiene que avisar, no crear en silencio ni tragárselo")


def _sincrono(fn):
    """Envuelve un test síncrono para el runner, que espera corrutinas."""
    async def envuelto():
        return fn()
    envuelto.__name__ = fn.__name__
    return envuelto


def test_el_monto_manual_no_pasa_por_float():
    """El camino manual —cuando Tiziano le dice a Lucy "gasté 500"— era el ÚNICO
    sitio del sistema donde el dinero pasaba por float. El automático ya usaba
    Decimal de punta a punta.

    Postgres redondea al guardar en NUMERIC(12,2), así que hoy no se ve nada
    raro. El punto es que el error entra ANTES de guardar, y una cifra torcida
    que nace de un redondeo no deja rastro de dónde salió.
    """
    from decimal import Decimal
    from acciones.crud import _monto_exacto
    assert _monto_exacto("1234.56") == Decimal("1234.56")
    assert isinstance(_monto_exacto(500), Decimal)
    # Positivo SIEMPRE: el signo lo da `tipo`, y guardarlo dos veces es cómo se
    # termina restando un ingreso.
    assert _monto_exacto(-500) == Decimal("500")
    assert _monto_exacto("0.10") * 3 == Decimal("0.30")


def test_un_monto_ilegible_no_se_guarda_como_cero():
    """Tragarse la basura como 0.00 anotaría un gasto de cero pesos que nadie
    entendería después. Si el clasificador manda algo que no es un número, eso
    es un fallo suyo y tiene que verse."""
    from acciones.crud import _monto_exacto
    for basura in ("quinientos", "", None, "12,50,30"):
        try:
            _monto_exacto(basura)
            assert False, f"{basura!r} no debería haber pasado"
        except ValueError:
            pass


_TESTS = [
    test_tarea_mismo_titulo_misma_fecha_no_crea_segunda,
    test_tarea_mismo_titulo_fecha_distinta_si_crea,
    test_tarea_sin_fecha_dedup_por_null,
    test_tarea_borrada_no_bloquea,
    test_tarea_hecha_no_bloquea,
    test_responsable_valido_se_guarda_al_crear_en_un_solo_paso,
    test_un_numero_de_chat_tambien_vale_como_responsable,
    test_responsable_que_no_vale_no_crea_NADA,
    test_un_chat_que_no_puede_ser_responsable_tambien_se_rechaza,
    test_sin_responsable_todo_queda_igual_que_hoy,
    test_una_cita_con_responsable_no_lo_escribe_ni_lo_valida,
    test_duplicado_sin_responsable_se_lo_pone_y_deja_huella_de_editar,
    test_duplicado_con_el_mismo_responsable_no_toca_nada,
    test_duplicado_con_OTRO_responsable_no_se_pisa_y_avisa,
    test_crear_pasa_por_la_MISMA_puerta_que_usa_editar,
    test_cita_mismo_titulo_mismo_inicio_no_crea_segunda,
    test_cita_mismo_titulo_inicio_distinto_si_crea,
    # Los dos de abajo son síncronos; el runner hace `await t()`, así que se
    # envuelven acá en vez de volverlos async por una razón de plomería.
    _sincrono(test_el_monto_manual_no_pasa_por_float),
    _sincrono(test_un_monto_ilegible_no_se_guarda_como_cero),
    _sincrono(test_la_categoria_por_telegram_pasa_por_el_mismo_vocabulario),
    _sincrono(test_el_agente_conoce_el_codigo_y_las_categorias_del_codigo),
    _sincrono(test_el_agente_no_duplica_un_movimiento_que_ya_trajo_el_banco),
    test_cambiar_el_tipo_no_puede_dejar_un_rubro_en_un_ingreso,
]


async def _main():
    fallos = 0
    for t in _TESTS:
        try:
            await t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            fallos += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fallos += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - fallos}/{len(_TESTS)} en verde")
    return fallos


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
