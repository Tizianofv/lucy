# -*- coding: utf-8 -*-
"""Las citas con dueño (encargos 1+2 del diseño "lucy-citas-con-dueno",
22-sep-2026): la columna `eventos.duenos_chat_id` y crear/editar una cita
por Telegram.

Decisión de Tiziano, textual: «Que las citas tengan dueño» y, sobre la
forma, «Puede ser de los dos» — una cita puede tener a Tiziano y a Rosi a
la vez. Diseño completo en disenos/lucy-citas-con-dueno/DISENO.md.

CÓMO SE PRUEBA (mismo criterio que exigió el NO PASA sobre `69e1c9e`, del
encargo anterior de este diseño): las funciones REALES de
`acciones/crud.py` -- `crear_desde_interpretacion`, `editar`,
`_duenos_que_valen` -- corren SIN TOCAR; lo único que se reemplaza es
`db.pool`, por un adaptador (`_ConnSQLite`) que ejecuta el TEXTO SQL real
contra SQLite, traduciendo SOLO sintaxis:
  · `%s` (psycopg) por `?` (sqlite3);
  · un parámetro que es una LISTA de Python (el array `duenos_chat_id`,
    igual que `anticipos_min`) se guarda como texto JSON -- SQLite no tiene
    tipo array -- y se deshace mirando la FORMA del valor leído (si es un
    texto que empieza y termina con corchetes), no el NOMBRE de la
    columna, para no tecleded una lista de "cuáles son las columnas
    array".
Nada de esto reimplementa la lógica de la puerta ni del INSERT: son las
mismas funciones, el mismo SQL, ejecutándose de verdad.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- BETA y GAMMA, igual que el
resto de este diseño ("Rosi independiente"), para probar que la puerta
deriva de `config.puede_ser_responsable`/`personas_del_panel()` y no de un
nombre escrito en el código.

LA FRONTERA, dicha: `grep -c "INSERT INTO eventos" acciones/crud.py
cerebro/calendario.py` (comprobado el 22-sep-2026 sobre este mismo commit)
da exactamente 2 -- `crear_desde_interpretacion` (Telegram, lo que prueba
este archivo) y `cerebro/calendario.py::_guardar` (el espejo de Google
Calendar, que NO pasa por `PUERTAS`/`_duenos_que_valen`: hoy nunca escribe
`duenos_chat_id`, a propósito -- ponerle dueño automático por calendario es
el encargo 4 del diseño, que este encargo NO incluye). Ningún tercer
camino escribe `eventos` hoy.

Correr:  python3 -m pytest tests/test_citas_con_dueno.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "1001")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")


class _Cualquiera:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Cualquiera()

    def __call__(self, *a, **k):
        return _Cualquiera()


for _n, _attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object()}),
                   ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                   ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera}),
                   ("httpx", {"HTTPError": type("H", (Exception,), {})})):
    _m = types.ModuleType(_n)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    _m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[_n] = _m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import acciones.crud as crud  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
GAMMA = 3003
AJENO = 999999   # sin acceso: no puede ser dueño


def _con_gente():
    """`config.CHAT_IDS_PERMITIDOS`/`NOMBRES_POR_CHAT` de mentira -- mismo
    patrón que ya usan `tests/test_briefing_por_persona.py` y
    `tests/test_recordatorios_por_responsable.py`: `personas_del_panel()`
    lee estos dos módulo-globales directo, no del entorno, así que hace
    falta fijarlos para que este archivo no dependa de qué otro archivo de
    la suite importó `config` primero."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = (DUENO, BETA, GAMMA)
    config.NOMBRES_POR_CHAT = {DUENO: "Alfa", BETA: "Beta", GAMMA: "Gamma"}

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---------------------------------------------------------------------------
# 1) `_duenos_que_valen`: la puerta en sí, sin base -- pura.
# ---------------------------------------------------------------------------
def test_sin_dato_es_sin_dueno():
    restaurar = _con_gente()
    try:
        assert crud._duenos_que_valen(None) == []
        assert crud._duenos_que_valen("") == []
        assert crud._duenos_que_valen([]) == []
    finally:
        restaurar()


def test_un_solo_nombre_da_una_lista_de_uno():
    restaurar = _con_gente()
    try:
        assert crud._duenos_que_valen("Beta") == [BETA]
    finally:
        restaurar()


def test_dos_nombres_en_lista_da_los_dos_en_orden():
    """«Puede ser de los dos» -- Tiziano, textual."""
    restaurar = _con_gente()
    try:
        assert crud._duenos_que_valen(["Alfa", "Beta"]) == [DUENO, BETA]
        assert crud._duenos_que_valen(["Beta", "Alfa"]) == [BETA, DUENO]
    finally:
        restaurar()


def test_duplicados_en_la_lista_se_juntan():
    restaurar = _con_gente()
    try:
        assert crud._duenos_que_valen(["Beta", "Beta"]) == [BETA]
        assert crud._duenos_que_valen([BETA, "Beta"]) == [BETA]
    finally:
        restaurar()


def test_un_nombre_que_no_vale_revienta_la_lista_entera():
    """Ningún guardado parcial: si UN elemento no vale, nada se guarda."""
    restaurar = _con_gente()
    try:
        try:
            crud._duenos_que_valen(["Beta", "Pedro"])
            assert False, "tenía que rechazar 'Pedro'"
        except ValueError as e:
            assert "Pedro" not in str(e), (
                "el motivo no repite lo pedido, igual que _responsable_que_vale")
    finally:
        restaurar()


def test_un_chat_sin_acceso_no_vale():
    restaurar = _con_gente()
    try:
        try:
            crud._duenos_que_valen(AJENO)
            assert False, "tenía que rechazar un chat sin acceso"
        except ValueError:
            pass
    finally:
        restaurar()


def test_la_puerta_de_eventos_esta_en_puertas():
    assert crud.PUERTAS["eventos"]["duenos_chat_id"] is crud._duenos_que_valen


# ---------------------------------------------------------------------------
# 2) SQLite real: crear/editar una cita por Telegram, con el SQL de verdad
#    de acciones/crud.py.
# ---------------------------------------------------------------------------

_TABLA_EVENTOS_CON_DUENOS = """
    CREATE TABLE eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bandeja_id INTEGER,
        creado_en TEXT,
        titulo TEXT NOT NULL,
        inicia_en TEXT NOT NULL,
        termina_en TEXT,
        lugar TEXT,
        persona_id INTEGER,
        proyecto_id INTEGER,
        notas TEXT,
        avisos_enviados TEXT NOT NULL DEFAULT '[]',
        anticipos_min TEXT NOT NULL DEFAULT '[0]',
        gcal_id TEXT,
        gcal_cal_id TEXT,
        gcal_calendar TEXT,
        duenos_chat_id TEXT NOT NULL DEFAULT '[]',
        borrado_en TEXT
    )
"""

# La forma DE ANTES de este encargo: sin `duenos_chat_id`, para probar la
# caída de compatibilidad contra una base real que todavía no corrió la
# migración.
_TABLA_EVENTOS_SIN_DUENOS = """
    CREATE TABLE eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bandeja_id INTEGER,
        creado_en TEXT,
        titulo TEXT NOT NULL,
        inicia_en TEXT NOT NULL,
        termina_en TEXT,
        lugar TEXT,
        persona_id INTEGER,
        proyecto_id INTEGER,
        notas TEXT,
        avisos_enviados TEXT NOT NULL DEFAULT '[]',
        anticipos_min TEXT NOT NULL DEFAULT '[0]',
        gcal_id TEXT,
        gcal_cal_id TEXT,
        gcal_calendar TEXT,
        borrado_en TEXT
    )
"""

_TABLA_LOG_ACCIONES = """
    CREATE TABLE log_acciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        actor TEXT NOT NULL,
        accion TEXT NOT NULL,
        tabla TEXT NOT NULL,
        registro_id INTEGER NOT NULL,
        antes TEXT,
        despues TEXT,
        motivo TEXT,
        bandeja_id INTEGER
    )
"""


def _decodificar(valor):
    """Un TEXT que tiene FORMA de array JSON (empieza y termina con
    corchetes) se devuelve como lista de Python -- SQLite no tiene tipo
    array. Mirar la FORMA del valor y no el nombre de la columna es a
    propósito: así sirve para `duenos_chat_id`, `anticipos_min` y
    `avisos_enviados` sin tener que teclear esa lista acá."""
    if isinstance(valor, str) and valor.startswith("[") and valor.endswith("]"):
        try:
            return json.loads(valor)
        except (json.JSONDecodeError, ValueError):
            return valor
    return valor


def _traducir_params(params):
    salida = []
    for p in params or ():
        if isinstance(p, (list, tuple)):
            salida.append(json.dumps(list(p)))
        else:
            salida.append(p)
    return tuple(salida)


class _ErrorConSqlstate(Exception):
    """SQLite no etiqueta sus errores con un SQLSTATE de Postgres --
    `sqlite3.OperationalError` no tiene `.sqlstate`. Se le agrega acá,
    DESPUÉS de confirmar por el mensaje que el error es "la columna
    duenos_chat_id no existe" y ningún otro -- misma técnica que
    `tests/test_correo_directo_a_los_dos.py::_ErrorConSqlstate`."""

    def __init__(self, original: Exception, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(str(original))


class _CurSQLite:
    def __init__(self, cur, dict_mode=False):
        self._cur = cur
        self._dict_mode = dict_mode

    async def execute(self, sql, params=None):
        sql2 = sql.replace("%s", "?")
        params2 = _traducir_params(params)
        try:
            self._cur.execute(sql2, params2)
        except sqlite3.OperationalError as e:
            if "duenos_chat_id" in str(e) and (
                    "no such column" in str(e) or "has no column named" in str(e)
                    or "table eventos has no column" in str(e)):
                raise _ErrorConSqlstate(e, "42703") from e
            raise
        return self

    def _fila(self, row):
        if row is None:
            return None
        if self._dict_mode:
            return {k: _decodificar(row[k]) for k in row.keys()}
        return tuple(_decodificar(v) for v in row)

    async def fetchone(self):
        return self._fila(self._cur.fetchone())

    async def fetchall(self):
        return [self._fila(r) for r in self._cur.fetchall()]


class _Transaccion:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False   # no traga la excepción -- igual que el real


class _ConnSQLite:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    async def execute(self, sql, params=None):
        cur = _CurSQLite(self.con.cursor())
        return await cur.execute(sql, params)

    def cursor(self, row_factory=None):
        return _CurSQLite(self.con.cursor(), dict_mode=row_factory is not None)

    def transaction(self):
        return _Transaccion()


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _PoolSQLite:
    def __init__(self, con: sqlite3.Connection):
        self._conn = _ConnSQLite(con)

    def connection(self):
        return _PoolCM(self._conn)


def _instalar(sql_tabla_eventos: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(sql_tabla_eventos)
    con.execute(_TABLA_LOG_ACCIONES)
    con.commit()
    db.pool = _PoolSQLite(con)
    return con


AHORA = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _pedido(**extra):
    base = {"clasificacion": "cita", "titulo": "reunión de prueba",
            "cuando": AHORA.isoformat(), "detalle": ""}
    base.update(extra)
    return base


def test_crear_una_cita_sin_dueno_sigue_igual_que_antes():
    """Regresión: el caso normal (413/418 citas de hoy) no cambia."""
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        tabla, rid, log_id = _correr(
            crud.crear_desde_interpretacion(1, _pedido()))
        assert tabla == "eventos"
        fila = con.execute(
            "SELECT duenos_chat_id FROM eventos WHERE id=?", (rid,)).fetchone()
        assert json.loads(fila["duenos_chat_id"]) == []
    finally:
        restaurar()


def test_crear_una_cita_con_un_dueno():
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        tabla, rid, log_id = _correr(crud.crear_desde_interpretacion(
            1, _pedido(duenos_chat_id="Beta")))
        fila = con.execute(
            "SELECT duenos_chat_id FROM eventos WHERE id=?", (rid,)).fetchone()
        assert json.loads(fila["duenos_chat_id"]) == [BETA]
    finally:
        restaurar()


def test_crear_una_cita_con_los_dos_duenos():
    """LA GARANTÍA CENTRAL de la forma del dato: «Puede ser de los dos»."""
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        tabla, rid, log_id = _correr(crud.crear_desde_interpretacion(
            1, _pedido(duenos_chat_id=["Alfa", "Beta"])))
        fila = con.execute(
            "SELECT duenos_chat_id FROM eventos WHERE id=?", (rid,)).fetchone()
        assert json.loads(fila["duenos_chat_id"]) == [DUENO, BETA]
    finally:
        restaurar()


def test_crear_con_dueno_invalido_no_crea_nada():
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        try:
            _correr(crud.crear_desde_interpretacion(
                1, _pedido(duenos_chat_id="Pedro")))
            assert False, "tenía que rechazar la creación entera"
        except ValueError:
            pass
        total = con.execute("SELECT count(*) FROM eventos").fetchone()[0]
        assert total == 0, "no tenía que quedar ninguna fila"
    finally:
        restaurar()


def test_crear_sin_dueno_cae_sin_la_migracion():
    """Sin la columna (SQLSTATE 42703) y SIN pedir dueño: cae al INSERT de
    antes, la cita se crea igual."""
    con = _instalar(_TABLA_EVENTOS_SIN_DUENOS)
    restaurar = _con_gente()
    try:
        tabla, rid, log_id = _correr(
            crud.crear_desde_interpretacion(1, _pedido()))
        total = con.execute("SELECT count(*) FROM eventos").fetchone()[0]
        assert total == 1
    finally:
        restaurar()


def test_crear_con_dueno_avisa_sin_la_migracion_y_no_crea_nada():
    """Sin la columna Y pidiendo dueño: no se puede fingir que se guardó --
    ValueError explícito, y CERO filas nuevas (nada a medias)."""
    con = _instalar(_TABLA_EVENTOS_SIN_DUENOS)
    restaurar = _con_gente()
    try:
        try:
            _correr(crud.crear_desde_interpretacion(
                1, _pedido(duenos_chat_id="Beta")))
            assert False, "tenía que avisar que falta la migración"
        except ValueError as e:
            assert "migra" in str(e).lower()
        total = con.execute("SELECT count(*) FROM eventos").fetchone()[0]
        assert total == 0, "no tenía que quedar ninguna fila a medias"
    finally:
        restaurar()


def test_editar_agrega_un_segundo_dueno():
    """`editar()` -- genérico, sin tocarlo -- ya sabía validar por PUERTAS;
    esto comprueba que la entrada nueva (`eventos.duenos_chat_id`) corre de
    verdad, con SQL real: SELECT * (dict_row) + UPDATE + log_acciones."""
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        _correr(crud.crear_desde_interpretacion(
            1, _pedido(duenos_chat_id="Alfa")))
        rid = con.execute("SELECT id FROM eventos").fetchone()[0]

        despues, log_id = _correr(crud.editar(
            "eventos", rid, {"duenos_chat_id": ["Alfa", "Gamma"]},
            motivo="prueba"))
        assert despues["duenos_chat_id"] == [DUENO, GAMMA]

        fila = con.execute(
            "SELECT duenos_chat_id FROM eventos WHERE id=?", (rid,)).fetchone()
        assert json.loads(fila["duenos_chat_id"]) == [DUENO, GAMMA]
    finally:
        restaurar()


def test_editar_a_un_nombre_invalido_no_cambia_nada():
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        _correr(crud.crear_desde_interpretacion(
            1, _pedido(duenos_chat_id="Alfa")))
        rid = con.execute("SELECT id FROM eventos").fetchone()[0]

        try:
            _correr(crud.editar("eventos", rid, {"duenos_chat_id": "Pedro"},
                                motivo="prueba"))
            assert False, "tenía que rechazar el cambio"
        except ValueError:
            pass

        fila = con.execute(
            "SELECT duenos_chat_id FROM eventos WHERE id=?", (rid,)).fetchone()
        assert json.loads(fila["duenos_chat_id"]) == [DUENO], (
            "el dueño original no puede haberse tocado")
    finally:
        restaurar()


def test_editar_a_sin_dueno_vacia_la_lista():
    con = _instalar(_TABLA_EVENTOS_CON_DUENOS)
    restaurar = _con_gente()
    try:
        _correr(crud.crear_desde_interpretacion(
            1, _pedido(duenos_chat_id=["Alfa", "Beta"])))
        rid = con.execute("SELECT id FROM eventos").fetchone()[0]

        despues, _ = _correr(crud.editar(
            "eventos", rid, {"duenos_chat_id": None}, motivo="prueba"))
        assert despues["duenos_chat_id"] == []
    finally:
        restaurar()


def test_editar_sin_la_migracion_falla_por_columna_ausente():
    """`editar()` ya toleraba esto SOLO (el `SELECT *` con `dict_row` lee
    las columnas que existen de VERDAD): sin `duenos_chat_id` en la tabla,
    pedir editarla da el mismo mensaje que pedir cualquier columna que no
    existe -- no hace falta ningún SAVEPOINT nuevo acá."""
    con = _instalar(_TABLA_EVENTOS_SIN_DUENOS)
    restaurar = _con_gente()
    try:
        _correr(crud.crear_desde_interpretacion(1, _pedido()))
        rid = con.execute("SELECT id FROM eventos").fetchone()[0]
        try:
            _correr(crud.editar("eventos", rid, {"duenos_chat_id": "Beta"},
                                motivo="prueba"))
            assert False, "tenía que rechazar: la columna no existe"
        except ValueError as e:
            assert "duenos_chat_id" in str(e)
    finally:
        restaurar()


# ---------------------------------------------------------------------------
# 3) La frontera: quién más escribe `eventos`, y si pasa por la puerta.
# ---------------------------------------------------------------------------
def test_solo_dos_caminos_escriben_eventos_y_solo_uno_por_la_puerta():
    """Medido el 22-sep-2026 sobre este commit: `grep -c "INSERT INTO
    eventos"` en `acciones/crud.py` da 2 -- las DOS formas del MISMO sitio
    (`crear_desde_interpretacion`, rama `cita`): `con_duenos` y
    `sin_duenos`, la caída de compatibilidad sin la migración (ver el
    docstring de la migración). `cerebro/calendario.py` da 1 (`_guardar`,
    el espejo de Google). Ningún otro archivo de producción escribe
    `eventos`."""
    import pathlib
    raiz = pathlib.Path(_ROOT)
    crud_txt = (raiz / "acciones" / "crud.py").read_text(encoding="utf-8")
    calendario_txt = (raiz / "cerebro" / "calendario.py").read_text(encoding="utf-8")
    assert crud_txt.count("INSERT INTO eventos") == 2
    assert calendario_txt.count("INSERT INTO eventos") == 1
    # Y el de Google NO nombra duenos_chat_id -- no pasa por la puerta, a
    # propósito: ponerle dueño automático por calendario es el encargo 4,
    # que este encargo no incluye.
    assert "duenos_chat_id" not in calendario_txt


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
