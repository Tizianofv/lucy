# -*- coding: utf-8 -*-
"""Lectura de SOLO lo de Tiziano en `bandeja`, `notas` y `eventos`
(§A.1-A.3, parte A del plan de construcción "Code como responsable de
tareas técnicas", 27-sep-2026, `db/lectura_dueno.py`). `movimientos` NO
tiene lectura -- ver `db/lectura_dueno.py` por qué (hallazgo de la sala,
27-sep-2026, contra producción: `bandeja.chat_id` no distingue el buzón
de origen, así que un movimiento de Rosi sale marcado como de Tiziano).

Parte A, y SOLO esa: funciones de lectura, de solo lectura de verdad
(`SET TRANSACTION READ ONLY`, dentro de una transacción), que deciden "de
Tiziano" por LA MISMA puerta (`_condicion_de_dueno`). `personas`/
`preferencias` NO están acá (van en la parte E). Sin ruta HTTP: eso no
está aprobado.

EVENTOS, corregido 27-sep-2026 (la sala encontró que la primera versión
se apartaba del diseño aprobado, `DISENO.md` §A.1/§A.2): un array
`duenos_chat_id` VACÍO es "de la casa" y SALE (mismo criterio que
`despertador._destinatarios_de_tareas`); con dueños puestos, sale solo si
incluye a `CHAT_ID_DUENO`.

NINGÚN chat_id ni clave de este archivo es real (regla del repo: es
PÚBLICO).

CÓMO SE PRUEBA: SQL de verdad. Las funciones corren tal cual contra
un `db.pool` que es SQLite de verdad (mismo patrón de
`tests/test_code_alarmas.py::_sqlite_para_alerta_real` -- `%s` -> `?`, y
`%s = ANY(duenos_chat_id)`/`duenos_chat_id = '{}'` traducidos a una
función SQLite registrada y a un literal JSON, porque SQLite no tiene
arrays). Ninguna prueba reimplementa el filtro en Python: si
`_condicion_de_dueno` cambiara su SQL, estas pruebas lo ejecutarían tal
cual sale, no una copia.

Correr:  python3 -m pytest tests/test_lectura_dueno.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import sqlite3
import sys
import textwrap
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-lectura")
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


class _StubPool:
    def __init__(self, *a, **k):
        pass


_pool_mod.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _pool_mod)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import db.lectura_dueno as lectura  # noqa: E402

DUENO = config.CHAT_ID_DUENO   # 424242
ROSI = 700300777
DESCONOCIDO = 999999999


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ═══════════════════════════════════════════════════════════════════════
# El arnés SQLite: %s -> ?, "%s = ANY(duenos_chat_id)" -> una función
# registrada (SQLite no tiene arrays; duenos_chat_id se guarda como JSON).
# ═══════════════════════════════════════════════════════════════════════

def _traducir(sql: str) -> str:
    sql2 = sql.replace("%s = ANY(duenos_chat_id)", "es_miembro(duenos_chat_id, ?)")
    # Postgres representa el array vacío como '{}'; el arnés SQLite lo
    # guarda como JSON ('[]'). Mismo patrón que el reemplazo de arriba:
    # se traduce el LITERAL, no se reimplementa la condición.
    sql2 = sql2.replace("duenos_chat_id = '{}'", "duenos_chat_id = '[]'")
    return sql2.replace("%s", "?")


def _es_miembro(arreglo_json, valor):
    if not arreglo_json:
        return 0
    return 1 if valor in json.loads(arreglo_json) else 0


class _CurLectura:
    def __init__(self, con):
        self._con = con
        self._cur = None

    async def execute(self, sql, params=None):
        self._cur = self._con.execute(_traducir(sql), params or ())
        return self

    async def fetchall(self):
        return [dict(f) for f in self._cur.fetchall()]

    async def fetchone(self):
        f = self._cur.fetchone()
        return dict(f) if f is not None else None


class _ConnLectura:
    def __init__(self, con, comandos):
        self._con = con
        self.comandos = comandos

    def cursor(self, row_factory=None):
        return _CurLectura(self._con)

    async def execute(self, sql, params=None):
        self.comandos.append(sql.strip())
        if sql.strip().upper() == "SET TRANSACTION READ ONLY":
            return None
        return await _CurLectura(self._con).execute(sql, params)

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False
        return _Tx()


class _PoolLectura:
    def __init__(self, con):
        self.comandos: list[str] = []
        self._conn = _ConnLectura(con, self.comandos)

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def _sqlite_de_dueno():
    con = sqlite3.connect(":memory:", isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    con.create_function("es_miembro", 2, _es_miembro)
    con.execute("""
        CREATE TABLE bandeja (
          id INTEGER PRIMARY KEY, creado_en TEXT, origen TEXT,
          tipo_entrada TEXT, contenido_raw TEXT, clasificacion TEXT,
          estado TEXT, chat_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE notas (
          id INTEGER PRIMARY KEY, creado_en TEXT, contenido TEXT,
          etiquetas TEXT, proyecto_id INTEGER, persona_id INTEGER,
          borrado_en TEXT, bandeja_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE movimientos (
          id INTEGER PRIMARY KEY, fecha TEXT, tipo TEXT, monto TEXT,
          moneda TEXT, contraparte TEXT, categoria TEXT, referencia TEXT,
          estado TEXT, borrado_en TEXT, bandeja_id INTEGER
        )""")
    con.execute("""
        CREATE TABLE eventos (
          id INTEGER PRIMARY KEY, titulo TEXT, inicia_en TEXT,
          termina_en TEXT, lugar TEXT, persona_id INTEGER,
          proyecto_id INTEGER, notas TEXT, borrado_en TEXT,
          duenos_chat_id TEXT
        )""")
    return con


def _instalar(con):
    guardado = lectura.pool
    lectura.pool = _PoolLectura(con)
    return guardado


def _restaurar(guardado):
    lectura.pool = guardado


# ═══════════════════════════════════════════════════════════════════════
# §A.1 -- bandeja: el chat_id vive EN LA FILA.
# ═══════════════════════════════════════════════════════════════════════

def test_bandeja_trae_solo_lo_de_tiziano():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una fila de Rosi, de un chat
    desconocido, o sin chat_id (origen de sistema) nunca sale."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (1, ?, 'telegram', 'de tiziano')", (DUENO,))
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (2, ?, 'telegram', 'de rosi')", (ROSI,))
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (3, ?, 'telegram', 'de un desconocido')", (DESCONOCIDO,))
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (4, NULL, 'telegram', 'sin chat_id, origen de sistema')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert [f["id"] for f in filas] == [1]


def test_bandeja_origen_no_confiable_no_sale():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE por la sala (27-sep-2026): un correo
    bancario (`origen='banco'`) con `chat_id=CHAT_ID_DUENO` -- el valor que
    `captura/consumos.py` pone SIEMPRE, sea de quien sea el buzón -- no
    sale, aunque el `chat_id` diga que sí."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (1, ?, 'banco', 'correo del banco de quien sea')",
                (DUENO,))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


def test_bandeja_origen_desconocido_no_sale():
    """ENTRADA INVENTADA: un origen que nadie clasificó todavía (ni
    confiable ni no-confiable) cae del lado ESTRICTO -- no sale. Ausencia
    de clasificación se trata como "no confiable", nunca como "sí"."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id, origen, contenido_raw) "
                "VALUES (1, ?, 'un-origen-que-nadie-clasifico-todavia', 'x')",
                (DUENO,))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


def test_bandeja_usa_transaccion_de_solo_lectura():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: `SET TRANSACTION READ ONLY`, DENTRO
    de una transacción -- el mismo literal exacto que ya reconoce
    `tests/test_responsable.py::_ejecuta_solo_lectura`, para que el censo
    de escritores genéricos no le pida una sonda a una función que nunca
    escribe nada."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO bandeja (id, chat_id) VALUES (1, ?)", (DUENO,))
    guardado = _instalar(con)
    pool_falso = lectura.pool
    try:
        _correr(lectura.leer_bandeja_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert any(c.strip().upper() == "SET TRANSACTION READ ONLY"
               for c in pool_falso.comandos), (
        f"no se puso la transacción de solo lectura: {pool_falso.comandos}")


# ═══════════════════════════════════════════════════════════════════════
# §A.2 -- notas: SIN chat_id propio, se decide por la `bandeja` que la
# originó.
# ═══════════════════════════════════════════════════════════════════════

def _con_bandejas(con):
    con.execute("INSERT INTO bandeja (id, chat_id, origen) VALUES "
                "(10, ?, 'telegram')", (DUENO,))
    con.execute("INSERT INTO bandeja (id, chat_id, origen) VALUES "
                "(20, ?, 'telegram')", (ROSI,))
    con.execute("INSERT INTO bandeja (id, chat_id, origen) VALUES "
                "(30, ?, 'telegram')", (DESCONOCIDO,))
    # De origen NO confiable, pero con chat_id=DUENO -- el caso exacto que
    # `captura/consumos.py` produce para el correo bancario de CUALQUIER
    # buzón: una nota que colgara de esta fila tampoco puede salir.
    con.execute("INSERT INTO bandeja (id, chat_id, origen) VALUES "
                "(40, ?, 'banco')", (DUENO,))


def test_notas_trae_solo_lo_de_tiziano():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: una nota de Rosi, de un chat
    desconocido, SIN `bandeja_id` (no hay forma de probar de quién es), o
    de una bandeja de origen NO confiable (mismo `chat_id` del dueño, pero
    puesto por default sobre contenido que no escribió él) nunca sale."""
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(1, 10, 'de tiziano')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(2, 20, 'de rosi')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(3, 30, 'de un desconocido')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(4, NULL, 'sin bandeja_id')")
    con.execute("INSERT INTO notas (id, bandeja_id, contenido) VALUES "
                "(5, 40, 'de origen banco, con chat_id del dueno')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_notas_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert [f["id"] for f in filas] == [1]


def test_notas_no_trae_borradas():
    con = _sqlite_de_dueno()
    _con_bandejas(con)
    con.execute("INSERT INTO notas (id, bandeja_id, contenido, borrado_en) "
                "VALUES (1, 10, 'borrada', '2026-01-01')")
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_notas_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


def test_movimientos_no_tiene_lectura_en_esta_parte():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE por la sala (27-sep-2026): sacar
    `leer_movimientos_de_dueno` de la parte A -- medido contra producción,
    `bandeja.chat_id` no distingue el buzón de origen (`captura/
    consumos.py` guarda TODO correo bancario con `chat_id=CHAT_ID_DUENO`,
    incluido el de Rosi), así que no hay dato confiable para decidir de
    quién es un movimiento."""
    assert not hasattr(lectura, "leer_movimientos_de_dueno"), (
        "leer_movimientos_de_dueno sigue existiendo -- se acordó sacarla "
        "de la parte A hasta que haya un dato confiable de buzón de origen")
    with pytest.raises(ValueError):
        lectura._condicion_de_dueno("movimientos")


# ═══════════════════════════════════════════════════════════════════════
# §A.3 -- eventos: `duenos_chat_id` es un ARRAY. DISEÑO APROBADO
# (`DISENO.md` §A.1/§A.2): un array VACÍO es "de la casa" y SALE -- mismo
# criterio que `despertador._destinatarios_de_tareas`. Con dueños puestos,
# sale solo si incluye a Tiziano.
# ═══════════════════════════════════════════════════════════════════════

def test_eventos_trae_las_suyas_las_compartidas_y_las_de_la_casa():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE (corregida 27-sep-2026 tras el
    hallazgo de la sala contra producción -- la primera versión excluía el
    array vacío, apartándose del diseño aprobado): una cita SOLO de Rosi
    no sale; una de Tiziano, una compartida (Tiziano Y Rosi), y una SIN
    dueño puesto (`duenos_chat_id = []`, "de la casa") SÍ salen."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (1, 'de tiziano', '2026-09-01', ?)",
                (json.dumps([DUENO]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (2, 'de rosi', '2026-09-02', ?)",
                (json.dumps([ROSI]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (3, 'compartida', '2026-09-03', ?)",
                (json.dumps([DUENO, ROSI]),))
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (4, 'de la casa', '2026-09-04', ?)",
                (json.dumps([]),))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_eventos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert sorted(f["id"] for f in filas) == [1, 3, 4]


def test_eventos_solo_de_rosi_no_sale():
    """LA PAREJA del test anterior, mirado al revés: un array CON dueños
    puestos que NO incluye a Tiziano no cuenta, aunque el array no esté
    vacío -- si la mutación borrara el `%s = ANY(...)` entero (dejando
    solo "array vacío = sale"), esta prueba lo vería."""
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id) "
                "VALUES (1, 'de rosi', '2026-09-01', ?)", (json.dumps([ROSI]),))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_eventos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


def test_eventos_no_trae_borrados():
    con = _sqlite_de_dueno()
    con.execute("INSERT INTO eventos (id, titulo, inicia_en, duenos_chat_id, "
                "borrado_en) VALUES (1, 'x', '2026-09-01', ?, '2026-09-10')",
                (json.dumps([DUENO]),))
    guardado = _instalar(con)
    try:
        filas = _correr(lectura.leer_eventos_de_dueno())
    finally:
        _restaurar(guardado)
        con.close()
    assert filas == []


# ═══════════════════════════════════════════════════════════════════════
# HERMANOS: las lecturas usan LA MISMA puerta -- se saca de lo real
# recorriendo el AST del módulo, no de memoria.
# ═══════════════════════════════════════════════════════════════════════

def _llama_a(funcion, nombre: str) -> bool:
    codigo = textwrap.dedent(inspect.getsource(funcion))
    arbol = ast.parse(codigo)
    return any(isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == nombre)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre))
               for n in ast.walk(arbol))


def test_las_lecturas_pasan_por_leer_de_dueno():
    for fn in (lectura.leer_bandeja_de_dueno, lectura.leer_notas_de_dueno,
               lectura.leer_eventos_de_dueno):
        assert _llama_a(fn, "_leer_de_dueno"), (
            f"{fn.__name__} no pasa por _leer_de_dueno -- arma su propio "
            "criterio de dueño")


def test_leer_de_dueno_pasa_por_la_puerta_de_condicion():
    assert _llama_a(lectura._leer_de_dueno, "_condicion_de_dueno"), (
        "_leer_de_dueno ya no llama a _condicion_de_dueno -- LA puerta "
        "dejó de ser la única fuente del filtro de dueño")


def test_condicion_de_dueno_cubre_las_tablas_con_lectura():
    for tabla in ("bandeja", "notas", "eventos"):
        assert lectura._condicion_de_dueno(tabla)  # no revienta, y no es ""


def test_condicion_de_dueno_rechaza_tabla_desconocida():
    with pytest.raises(ValueError):
        lectura._condicion_de_dueno("personas")  # personas: parte E, no acá


# ═══════════════════════════════════════════════════════════════════════
# CENSO DE ORÍGENES DE `bandeja`: recorre el AST del repositorio ENTERO
# (excepto pruebas) buscando cada escritor real de `bandeja` -- llamadas a
# `guardar_en_bandeja`/`registrar_aviso` y los INSERT literales de
# `crear_tarea_desde_el_panel`/`cerrar_y_derivar` -- y extrae el `origen`
# que cada uno pone. Hallazgo de la sala, 27-sep-2026: `bandeja.chat_id`
# solo no alcanza, porque `captura/consumos.py` lo pone al chat del dueño
# SIEMPRE, sea cual sea el correo bancario. Ningún origen queda sin
# clasificar: uno nuevo (o uno que dejó de existir) pone esto en rojo.
# ═══════════════════════════════════════════════════════════════════════

def _origenes_reales_de_bandeja() -> dict[str, list[str]]:
    """(origen -> [archivo:línea, ...]) de cada escritor REAL de `bandeja`
    en el repositorio, sacado del AST -- no de una lista tecleada."""
    import test_buzon_que_no_se_ve as barrido

    raiz = barrido.RAIZ
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    default_registrar_aviso = inspect.signature(
        db.registrar_aviso).parameters["origen"].default
    hallazgos: dict[str, list[str]] = {}

    def _anota(origen, rel, lineno):
        if isinstance(origen, str):
            hallazgos.setdefault(origen, []).append(f"{rel}:{lineno}")

    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        try:
            fuente = real.read_text(encoding="utf-8")
            arbol = ast.parse(fuente, filename=str(real))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = real.relative_to(raiz).as_posix()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                nombre = (nodo.func.id if isinstance(nodo.func, ast.Name)
                          else nodo.func.attr if isinstance(nodo.func, ast.Attribute)
                          else None)
                if nombre in ("guardar_en_bandeja", "registrar_aviso"):
                    kw = {k.arg: k.value for k in nodo.keywords}
                    valor = kw.get("origen")
                    if valor is not None and isinstance(valor, ast.Constant):
                        _anota(valor.value, rel, nodo.lineno)
                    elif valor is None:
                        # sin `origen=`: el default de la función que se
                        # está llamando (cada una tiene el suyo).
                        default = ("telegram" if nombre == "guardar_en_bandeja"
                                   else default_registrar_aviso)
                        _anota(default, rel, nodo.lineno)
                elif nombre in ("execute", "executemany"):
                    sql = nodo.args[0] if nodo.args else None
                    if (isinstance(sql, ast.Constant)
                            and isinstance(sql.value, str)
                            and "INSERT INTO bandeja" in sql.value):
                        m = re.search(r"VALUES\s*\(\s*'([a-z_]+)'", sql.value)
                        if m:
                            _anota(m.group(1), rel, nodo.lineno)
    return hallazgos


def test_el_censo_de_origenes_no_tiene_un_escritor_nuevo_sin_clasificar():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE por la sala: si aparece un escritor
    de `bandeja` con un `origen` que nadie clasificó (ni confiable ni no
    confiable), esto se pone rojo -- no se cuela silencioso a ninguno de
    los dos cubos."""
    hallados = set(_origenes_reales_de_bandeja())
    clasificados = (set(lectura._ORIGENES_CONFIABLES_DE_BANDEJA)
                     | set(lectura._ORIGENES_NO_CONFIABLES_DE_BANDEJA))
    assert hallados, (
        "el censo no encontró ni un escritor de bandeja: se quedó ciego, "
        "no es que no haya ninguno")
    assert hallados == clasificados, (
        f"orígenes encontrados en el código real: {sorted(hallados)}; "
        f"clasificados en db/lectura_dueno.py: {sorted(clasificados)}. "
        f"Sin clasificar: {sorted(hallados - clasificados)}. "
        f"Clasificados que ya no existen: {sorted(clasificados - hallados)}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
