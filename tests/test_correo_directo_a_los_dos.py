# -*- coding: utf-8 -*-
"""El resumen del correo, directo a los dos (encargo 3, 22-sep-2026).

Diseño: `disenos/lucy-rosi-independiente/DISENO.md`, "Encargo 3". Decisión de
Tiziano, textual: el buzón del estudio también le llega a Rosi; el suyo (el
mixto) NO. Él sigue recibiendo el suyo igual que hoy, con los dos buzones.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- el segundo destino de este
archivo se llama BETA, igual que en `tests/test_briefing_por_persona.py` y
`tests/test_recordatorios_por_responsable.py`, para probar que el reparto
sale de `reporte_a` (una LISTA de chats, ahora) y no de un nombre escrito en
el código.

CORREGIDO tras el NO PASA del testigo sobre `69e1c9e`: la versión anterior
de este archivo tenía una clase `_BaseSQLite` que REEMPLAZABA
`db.correos_ya_reportados`/`db.marcar_correo_reportado` por su propia
reimplementación en Python -- corría SQL real, pero NUNCA el de
`db/db.py`. Dos mutaciones del testigo (quitar el `OR (destino_chat_id IS
NULL AND %s)` de `correos_ya_reportados`, y hacer que `marcar_correo_
reportado` siempre grabara `destino_chat_id = NULL`) dejaban la suite
entera en verde, porque esas funciones reales nunca se ejecutaban.

Ahora las pruebas de la sección 2 llaman a `db.correos_ya_reportados` y
`db.marcar_correo_reportado` DIRECTO -- las funciones reales, sin tocar --
y lo único que se reemplaza es `db.pool`, por un adaptador que traduce SOLO
sintaxis (`%s`→`?`, `= ANY(%s)`→`IN (?, ?, …)` con la lista aplanada) antes
de correr el texto TAL CUAL contra SQLite. El esquema de la tabla sale de
`db/schema.sql` y de la migración
(`db/migrations/2026-09-22_correo_reportado_por_destino.sql`), no tecleado
aparte: `_TABLA_CON_MIGRACION`/`_TABLA_SIN_MIGRACION` citan sus columnas.

LA FRONTERA, dicha: este archivo prueba `db.correos_ya_reportados`,
`db.marcar_correo_reportado`, `captura/correo.py::reporte_diario` y
`_pendientes_de` (el reparto por destino), `config.destinos_del_reporte`
(el vocabulario de `reporte_a`), y la extensión de
`cerebro/interpretar.py::_es_encargo_propio_del_dueno` al origen 'correo'.
NO vuelve a probar el candado de "una vez al día" en sí mismo -- eso ya lo
cubre `test_reporte_una_vez_al_dia.py` -- ni el filtro de bancos ni el cupo
de clasificación -- eso ya lo cubren `test_reporte_sin_bancos.py` y
`test_correo_no_descarta_callado.py`. Tampoco vuelve a probar `ON CONFLICT
DO NOTHING` en general (eso es SQL estándar); prueba que la LLAMADA real
de `marcar_correo_reportado` no duplique una fila.

QUÉ NO SE PUDO TRASLADAR A SQLITE, y por qué (punto d del NO PASA): sqlite3
no etiqueta sus errores con un SQLSTATE de Postgres -- `sqlite3.
OperationalError` no tiene `.sqlstate`. La caída de `db/db.py` (SQLSTATE
42703, columna ausente) se prueba igual: el adaptador de este archivo
detecta el ÚNICO caso real posible -- "no such column: destino_chat_id",
que es la traducción honesta de esa misma condición -- y le agrega
`.sqlstate = "42703"` antes de dejarlo subir, para que el `except` de
`db/db.py` (que sí mira `.sqlstate`) se dispare con la MISMA rama de código
que dispararía Postgres. El resto de la función -- el SELECT/INSERT de
respaldo -- corre tal cual, sin ningún doble.

Herméticos: sin Postgres ni red -- mismos stubs que el resto de la suite. La
única base real que toca este archivo es un SQLite en memoria, propio,
descartado al terminar cada prueba.

Correr:  python3 -m pytest tests/test_correo_directo_a_los_dos.py -q
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import types
from datetime import datetime

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


for _n, _attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object}),
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

import captura.correo as correo  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
TZ = config.TZ

PERSONAL = "personal@ejemplo.com"   # el buzón mixto, solo del dueño
ESTUDIO = "estudio@ejemplo.com"     # el buzón que ahora también ve BETA


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _clasificar_por_asunto(c, reglas=""):
    """Como el clasificador real: el asunto_corto sale del correo de VERDAD,
    no de un valor fijo -- si no, "del estudio"/"personal" no aparecerían
    nunca en el texto del encargo y las pruebas de contenido no probarían
    nada."""
    async def _f():
        return {"ambito": "laboral", "area": "cds_clientes", "nivel": "accion",
                "asunto_corto": c["subject"][:120], "motivo": ""}
    return _f()


# ---------------------------------------------------------------------------
# 1) config.destinos_del_reporte: el vocabulario de `reporte_a`, con lista
# ---------------------------------------------------------------------------
def test_sin_el_campo_va_solo_al_dueno():
    assert config.destinos_del_reporte({"user": PERSONAL}) == (DUENO,)


def test_reporte_a_0_es_a_nadie():
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte_a": 0}) == ()
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte": False}) == ()


def test_reporte_a_un_entero_es_compatible():
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte_a": 555}) == (555,)


def test_reporte_a_una_lista_da_varios_destinos():
    """El caso nuevo: el buzón del estudio, a los dos."""
    assert config.destinos_del_reporte(
        {"user": ESTUDIO, "reporte_a": [DUENO, BETA]}) == (DUENO, BETA)


def test_reporte_a_invalido_cae_al_dueno():
    assert config.destinos_del_reporte(
        {"user": PERSONAL, "reporte_a": "no-es-un-numero"}) == (DUENO,)
    assert config.destinos_del_reporte(
        {"user": PERSONAL, "reporte_a": [DUENO, "x"]}) == (DUENO,)


def test_destino_del_reporte_compat_es_el_primero():
    """El código de antes de este encargo sigue andando: `destino_del_reporte`
    (singular) es el PRIMERO de la lista."""
    assert correo.destino_del_reporte(
        {"user": ESTUDIO, "reporte_a": [DUENO, BETA]}) == DUENO
    assert correo.destino_del_reporte({"user": PERSONAL, "reporte_a": 0}) == 0


# ---------------------------------------------------------------------------
# 2) db.correos_ya_reportados / db.marcar_correo_reportado, LAS FUNCIONES
#    REALES de db/db.py, contra SQLite -- ni un doble reimplementado.
# ---------------------------------------------------------------------------

# Columnas sacadas de db/schema.sql (tras el encargo 3) y de
# db/migrations/2026-09-22_correo_reportado_por_destino.sql -- no tecleadas
# aparte de esos dos archivos, que son la fuente.
_TABLA_CON_MIGRACION = """
    CREATE TABLE correo_reportado (
        cuenta TEXT NOT NULL,
        uid INTEGER NOT NULL,
        reportado_en TEXT,
        nivel TEXT,
        ambito TEXT,
        area TEXT,
        asunto TEXT,
        bandeja_id INTEGER,
        leido_en TEXT,
        destino_chat_id INTEGER,
        UNIQUE(cuenta, uid, destino_chat_id)
    )
"""

# La forma DE ANTES de este encargo (db/schema.sql previo a `69e1c9e`): sin
# destino_chat_id, PRIMARY KEY (cuenta, uid) -- para probar la caída de
# compatibilidad contra una base real que todavía no corrió la migración.
_TABLA_SIN_MIGRACION = """
    CREATE TABLE correo_reportado (
        cuenta TEXT NOT NULL,
        uid INTEGER NOT NULL,
        reportado_en TEXT,
        nivel TEXT,
        ambito TEXT,
        area TEXT,
        asunto TEXT,
        bandeja_id INTEGER,
        leido_en TEXT,
        UNIQUE(cuenta, uid)
    )
"""


def _traducir(sql: str, params: tuple):
    """Traduce SOLO sintaxis entre psycopg y sqlite3: `%s` por `?`, y
    `= ANY(%s)` (arreglo de Postgres -- la ÚNICA forma en que estas dos
    funciones usan un array) por `IN (?, ?, …)` con la lista aplanada en los
    parámetros. Ninguna otra reescritura: el resto del texto -- las
    cláusulas AND, el OR con NULL, el ON CONFLICT -- llega a sqlite tal cual
    lo escribió `db/db.py`.
    """
    import re
    m = re.search(r"=\s*ANY\(%s\)", sql)
    if m:
        indice = sql[:m.start()].count("%s")
        valores = params[indice]
        marcas = ", ".join("?" * len(valores))
        sql = sql[:m.start()] + f"IN ({marcas})" + sql[m.end():]
        params = params[:indice] + tuple(valores) + params[indice + 1:]
    return sql.replace("%s", "?"), params


class _ErrorConSqlstate(Exception):
    """El MISMO efecto que vería `db/db.py` con Postgres real: una consulta
    que nombra `destino_chat_id` sobre una tabla que no la tiene es
    SQLSTATE 42703 (undefined_column) en los dos motores -- sqlite3
    simplemente no le pone esa etiqueta a su excepción, así que se la
    agrega ACÁ, después de confirmar por el mensaje que es ESE error y
    ningún otro."""

    def __init__(self, original: Exception, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(str(original))


class _Cur:
    def __init__(self, cur):
        self._cur = cur

    async def fetchall(self):
        return self._cur.fetchall()

    async def fetchone(self):
        return self._cur.fetchone()


class _Transaccion:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False   # no traga la excepción -- igual que el real


class _ConnSQLite:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    async def execute(self, sql, params=None):
        sql2, params2 = _traducir(sql, params or ())
        try:
            cur = self.con.execute(sql2, params2)
        except sqlite3.OperationalError as e:
            # SELECT dice "no such column: destino_chat_id"; INSERT dice
            # "table correo_reportado has no column named destino_chat_id"
            # -- los DOS mensajes que sqlite3 usa para la MISMA condición
            # (columna ausente), según la sentencia. Cualquier otro
            # OperationalError sigue de largo sin etiquetar.
            if "destino_chat_id" in str(e) and (
                    "no such column" in str(e) or "has no column named" in str(e)):
                raise _ErrorConSqlstate(e, "42703") from e
            raise
        self.con.commit()
        return _Cur(cur)

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


def _instalar_pool(sql_tabla: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(sql_tabla)
    db.pool = _PoolSQLite(con)
    return con


def test_marcar_graba_el_destino_real():
    """`db.marcar_correo_reportado` de verdad -- se llama tal cual la llama
    `captura/correo.py`, y se comprueba la fila que quedó en la tabla."""
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    _correr(db.marcar_correo_reportado(
        ESTUDIO, 7, destino=BETA, nivel="accion", asunto="algo"))
    filas = con.execute(
        "SELECT cuenta, uid, destino_chat_id FROM correo_reportado").fetchall()
    assert filas == [(ESTUDIO, 7, BETA)]


def test_marcar_no_duplica_la_fila_si_ya_existe():
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    _correr(db.marcar_correo_reportado(ESTUDIO, 7, destino=BETA))
    _correr(db.marcar_correo_reportado(ESTUDIO, 7, destino=BETA))
    filas = con.execute(
        "SELECT count(*) FROM correo_reportado WHERE cuenta=? AND uid=? "
        "AND destino_chat_id=?", (ESTUDIO, 7, BETA)).fetchall()
    assert filas == [(1,)]


def test_marcar_dos_destinos_deja_dos_filas_del_mismo_correo():
    """LA GARANTÍA CENTRAL, del lado de la escritura: `marcar_correo_
    reportado` llamado dos veces para el MISMO correo, una vez por
    destino, deja DOS filas -- no una que se pise con la otra."""
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    _correr(db.marcar_correo_reportado(ESTUDIO, 7, destino=DUENO))
    _correr(db.marcar_correo_reportado(ESTUDIO, 7, destino=BETA))
    filas = con.execute(
        "SELECT destino_chat_id FROM correo_reportado WHERE cuenta=? AND uid=?",
        (ESTUDIO, 7)).fetchall()
    assert {f[0] for f in filas} == {DUENO, BETA}


def test_correos_ya_reportados_no_confunde_destinos():
    """LA GARANTÍA CENTRAL, del lado de la lectura: `db.correos_ya_
    reportados` -- la función real -- filtra por destino. Insertado para
    DUENO, no puede salir para BETA."""
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    con.execute(
        "INSERT INTO correo_reportado (cuenta, uid, destino_chat_id) "
        "VALUES (?, ?, ?)", (ESTUDIO, 2, DUENO))
    con.commit()

    ya_dueno = _correr(db.correos_ya_reportados(ESTUDIO, [2], DUENO))
    ya_beta = _correr(db.correos_ya_reportados(ESTUDIO, [2], BETA))
    assert ya_dueno == {2}, "el dueño ya lo tenía marcado: tiene que verlo"
    assert ya_beta == set(), (
        "Beta nunca lo vio -- que el dueño lo tenga marcado no puede "
        "robarle la novedad")


def test_fila_vieja_con_null_cuenta_solo_para_el_dueno():
    """`destino_chat_id IS NULL` (filas de antes del encargo 3) cuenta como
    "informada al dueño" -- el único destino que existía -- y NO para
    cualquier otro."""
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    con.execute(
        "INSERT INTO correo_reportado (cuenta, uid, destino_chat_id) "
        "VALUES (?, ?, NULL)", (ESTUDIO, 3))
    con.commit()

    ya_dueno = _correr(db.correos_ya_reportados(ESTUDIO, [3], DUENO))
    ya_beta = _correr(db.correos_ya_reportados(ESTUDIO, [3], BETA))
    assert ya_dueno == {3}, "una fila vieja (NULL) es del dueño, el único que había"
    assert ya_beta == set(), "una fila vieja no le dice nada a Beta"


def test_correos_ya_reportados_cae_sin_la_columna():
    """SIN la migración (tabla vieja, sin `destino_chat_id`): la función
    real cae a la consulta de antes -- CUALQUIER destino ve como
    "informado" lo que ya se marcó, sin filtrar por destino. Es el lado
    seguro documentado en `db/db.py`: sub-informa, nunca sobre-informa."""
    con = _instalar_pool(_TABLA_SIN_MIGRACION)
    con.execute("INSERT INTO correo_reportado (cuenta, uid) VALUES (?, ?)",
                (ESTUDIO, 4))
    con.commit()

    ya_beta = _correr(db.correos_ya_reportados(ESTUDIO, [4], BETA))
    assert ya_beta == {4}, (
        "sin la migración, la función tiene que caer a la consulta vieja "
        "(sin destino) y ver el correo como ya informado, para cualquiera")


def test_marcar_cae_sin_la_columna():
    """SIN la migración: `marcar_correo_reportado` cae al INSERT viejo (sin
    `destino_chat_id`, que no existe) y la fila queda igual."""
    con = _instalar_pool(_TABLA_SIN_MIGRACION)
    _correr(db.marcar_correo_reportado(ESTUDIO, 8, destino=BETA, nivel="accion"))
    filas = con.execute(
        "SELECT cuenta, uid, nivel FROM correo_reportado").fetchall()
    assert filas == [(ESTUDIO, 8, "accion")]


def test_marcar_sin_la_columna_no_duplica():
    con = _instalar_pool(_TABLA_SIN_MIGRACION)
    _correr(db.marcar_correo_reportado(ESTUDIO, 8, destino=BETA))
    _correr(db.marcar_correo_reportado(ESTUDIO, 8, destino=BETA))
    filas = con.execute(
        "SELECT count(*) FROM correo_reportado WHERE cuenta=? AND uid=?",
        (ESTUDIO, 8)).fetchall()
    assert filas == [(1,)]


# ---------------------------------------------------------------------------
# 3) El reparto de extremo a extremo: `captura/correo.py::reporte_diario`
#    corriendo con la dedupe REAL de db/db.py contra SQLite.
# ---------------------------------------------------------------------------

class _Reloj:
    def __init__(self, ahora):
        self.ahora = ahora

    def now(self, tz=None):
        return self.ahora if tz is None else self.ahora.astimezone(tz)


class _Bandeja:
    """La única pieza que sigue siendo una lista en memoria: qué encargos
    se dejaron y cuándo -- el candado de "ya salió hoy" no es parte de la
    garantía por destino que pidió corregir el testigo (ésa vive en
    `correo_reportado`, ya cubierta arriba con SQL real)."""

    def __init__(self, reloj):
        self.filas: list[dict] = []
        self.reloj = reloj

    async def guardar_en_bandeja(self, **kw):
        self.filas.append({**kw, "creado_en": self.reloj.ahora})
        return len(self.filas)

    async def destinos_con_encargo_hoy(self, origen, prefijo, desde):
        return {f["chat_id"] for f in self.filas
                if f.get("origen") == origen
                and (f.get("contenido_raw") or "").startswith(prefijo)
                and f["creado_en"] >= desde
                and f.get("chat_id") is not None}

    async def listar_preferencias(self):
        return []

    def encargo_de(self, chat_id):
        for f in self.filas:
            if f.get("chat_id") == chat_id and f.get("origen") == "correo":
                return f
        return None


def _uno(uid, asunto):
    return {"uid": uid, "from": "Jorge <jorge@ejemplo.com>", "subject": asunto,
            "snippet": "hola", "ruido_barato": None}


def _montar(correo_de: dict, hora=7, minuto=10):
    """Instala `db.pool` (SQLite real, esquema con migración) para
    `correos_ya_reportados`/`marcar_correo_reportado`, y una bandeja en
    memoria para el resto. Devuelve (bandeja, reloj, con)."""
    con = _instalar_pool(_TABLA_CON_MIGRACION)
    reloj = _Reloj(datetime(2026, 9, 22, hora, minuto, tzinfo=TZ))
    bandeja = _Bandeja(reloj)
    correo.datetime = reloj
    for n in ("guardar_en_bandeja", "destinos_con_encargo_hoy",
              "listar_preferencias"):
        setattr(db, n, getattr(bandeja, n))
    correo._sin_leer_sync = lambda cuenta, dias, **kw: [
        dict(c, cuenta=cuenta["user"]) for c in correo_de.get(cuenta["user"], [])]
    correo.clasificar = _clasificar_por_asunto
    return bandeja, reloj, con


def test_tiziano_recibe_los_dos_buzones_beta_solo_el_del_estudio():
    config.CORREO_CUENTAS = [
        {"user": PERSONAL, "pass": "x"},
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    bandeja, _, con = _montar({PERSONAL: [_uno(1, "personal")],
                               ESTUDIO: [_uno(2, "del estudio")]})
    total = _correr(correo.reporte_diario())
    assert total == 3, (
        "3 pares (correo, destino): el del personal solo al dueño, el del "
        f"estudio a los dos -- salió {total}")

    del_dueno = bandeja.encargo_de(DUENO)
    del_beta = bandeja.encargo_de(BETA)
    assert del_dueno is not None and del_beta is not None

    assert "personal" in del_dueno["contenido_raw"]
    assert "del estudio" in del_dueno["contenido_raw"], (
        "el dueño tiene que seguir viendo el buzón del estudio, sin cambio")
    assert "informaste a Tiziano" in del_dueno["contenido_raw"]

    assert "del estudio" in del_beta["contenido_raw"]
    assert "personal" not in del_beta["contenido_raw"], (
        "ninguna línea del buzón personal de Tiziano puede llegarle a Beta")
    assert "informaste a Tiziano" not in del_beta["contenido_raw"], (
        "el encargo de Beta no puede decir que es un reporte para Tiziano")

    # Y quedaron DOS filas reales en correo_reportado para el correo del
    # estudio (una por destino) -- la garantía escrita en la base, no solo
    # en el conteo del reporte.
    filas = con.execute(
        "SELECT destino_chat_id FROM correo_reportado WHERE cuenta=? AND uid=2",
        (ESTUDIO,)).fetchall()
    assert {f[0] for f in filas} == {DUENO, BETA}

    # Y el candado quedó por destino: una segunda pasada no repite ninguno.
    assert _correr(correo.reporte_diario()) == 0


def test_lo_que_ya_vio_el_dueno_no_le_roba_la_novedad_a_beta():
    """LA GARANTÍA CENTRAL, de punta a punta: un correo del buzón del
    estudio que YA se le informó al dueño (un día anterior, digamos) sigue
    siendo NUEVO para Beta -- y viceversa."""
    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    bandeja, _, con = _montar({ESTUDIO: [_uno(2, "del estudio, viejo para el dueño")]})
    # Simula que AYER el correo #2 ya se le informó al dueño, pero nunca a
    # Beta -- el estado que deja "Beta se sumó como destino después de que
    # el dueño ya viera algunos correos de ese buzón".
    con.execute(
        "INSERT INTO correo_reportado (cuenta, uid, destino_chat_id) "
        "VALUES (?, ?, ?)", (ESTUDIO, 2, DUENO))
    con.commit()

    total = _correr(correo.reporte_diario())
    assert total == 1, f"solo Beta tenía que recibir este correo: salieron {total}"

    assert bandeja.encargo_de(DUENO) is None, (
        "el dueño ya lo había visto: no puede volver a aparecerle")
    del_beta = bandeja.encargo_de(BETA)
    assert del_beta is not None, "Beta nunca lo había visto: tenía que llegarle"
    assert "del estudio" in del_beta["contenido_raw"]

    filas = con.execute(
        "SELECT destino_chat_id FROM correo_reportado WHERE cuenta=? AND uid=2",
        (ESTUDIO,)).fetchall()
    assert {DUENO, BETA} == {f[0] for f in filas}


def test_un_correo_ya_visto_por_los_dos_no_vuelve_a_ninguno():
    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    bandeja, _, con = _montar({ESTUDIO: [_uno(5, "único")]})
    # 2, no 1: el mismo correo cuenta una vez POR DESTINO (dueño y Beta) --
    # `reporte_diario` documenta esto explícitamente ("pendientes es solo
    # para CONTAR, no para deduplicar entre destinos").
    assert _correr(correo.reporte_diario()) == 2

    # Un día después: candado de bandeja reabierto (otro `hoy_arranca`),
    # pero la dedupe REAL de correo_reportado (la misma conexión sqlite,
    # que sobrevive porque no se vuelve a instalar `db.pool`) sigue teniendo
    # las dos filas de ayer.
    reloj2 = _Reloj(datetime(2026, 9, 23, 7, 10, tzinfo=TZ))
    correo.datetime = reloj2
    bandeja2 = _Bandeja(reloj2)
    for n in ("guardar_en_bandeja", "destinos_con_encargo_hoy",
              "listar_preferencias"):
        setattr(db, n, getattr(bandeja2, n))
    correo._sin_leer_sync = lambda cuenta, dias, **kw: [
        dict(c, cuenta=cuenta["user"]) for c in {ESTUDIO: [_uno(5, "único")]}.get(
            cuenta["user"], [])]
    assert _correr(correo.reporte_diario()) == 0


def test_reporte_a_0_en_el_buzon_del_estudio_no_le_llega_a_nadie():
    """La frontera del campo sigue viva con lista o sin ella: si el buzón
    dice que no informa a nadie, ni el dueño lo ve."""
    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": 0},
    ]
    bandeja, _, _ = _montar({ESTUDIO: [_uno(9, "no debería salir")]})
    assert _correr(correo.reporte_diario()) == 0
    assert bandeja.filas == []


# ---------------------------------------------------------------------------
# 4) La copia general: el reporte del dueño no se copia a Beta
# ---------------------------------------------------------------------------
def test_interpretar_reconoce_el_reporte_de_correo_del_dueno():
    fila = {"origen": "correo", "chat_id": DUENO,
            "contenido_raw": correo.MARCA_ENCARGO + " Estos son los 3..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is True


def test_el_reporte_de_beta_no_es_el_del_dueno():
    """Hermano por tipo (mismo origen, misma marca) pero de OTRO chat: no
    tiene que activar la supresión -- va directo a Beta, la copia general
    ni se dispara para ese chat_id."""
    fila = {"origen": "correo", "chat_id": BETA,
            "contenido_raw": correo.MARCA_ENCARGO + " Estos son los 1..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is False


def test_la_alerta_911_no_se_confunde_con_el_reporte():
    """Hermano por chat (mismo origen 'correo', mismo chat_id=dueño) pero
    OTRO tipo de mensaje -- la 911 no tiene camino propio en este encargo,
    así que sigue copiándose como antes."""
    fila = {"origen": "correo", "chat_id": DUENO,
            "contenido_raw": "ALERTA DE INFRAESTRUCTURA por correo..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is False


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
