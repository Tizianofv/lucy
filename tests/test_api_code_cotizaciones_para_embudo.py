# -*- coding: utf-8 -*-
"""Lo que el Registro lee de las cotizaciones formales (L-COT, 23-oct-2026; diseño
`disenos/clientes-clasificar/DISENO.md`, pieza L-COT): `GET /api/code/cotizaciones/para-embudo`,
permiso `cotizaciones:leer_para_embudo`.

CAMINO DE PRODUCCIÓN: la app FastAPI real, la puerta real (`requiere`), claves y permisos reales de
`config`, y `db.cotizaciones_para_embudo` corriendo su SQL de verdad contra SQLite en memoria (`%s` -> `?`).
Lo único que se sustituye es el `pool` (la conexión) y `noco_lectura.contacto` (la red de Noco).

Para que la prueba de «solo borrador/enviada» tenga fuerza, la tabla de prueba NO trae el CHECK de estado:
una fila `aceptada` tiene que salir por el filtro del SQL, no por la base.

NINGÚN nombre, clave ni número de este archivo es real (el repo es PÚBLICO).

Correr:  python3 -m pytest tests/test_api_code_cotizaciones_para_embudo.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-cotizaciones-embudo")
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

import config  # noqa: E402
import db.db as db  # noqa: E402
import noco_lectura  # noqa: E402
import web.app as panel  # noqa: E402
import web.api_code as api_code  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENTE = TestClient(panel.app)

CLAVE_REGISTRO = "clave-de-prueba-registro-embudo-no-es-real"
CLAVE_OTRA = "clave-de-prueba-natalia-sin-embudo-no-es-real"
PERMISO = "cotizaciones:leer_para_embudo"
RUTA = "/api/code/cotizaciones/para-embudo"

CLI_CON_TEL, CLI_CON_TEL_Y_MAIL = 5001, 5002   # fichas de Noco inventadas
ANIO = 2026
AHORA = datetime.now(timezone.utc)

FORMA = {"id", "codigo", "estado", "enviada_en", "creado_en", "opciones", "telefono"}
FORMA_OPCION = {"nombre", "total"}


def hace(dias: int) -> str:
    return (AHORA - timedelta(days=dias)).isoformat()


sqlite3.register_converter(
    "TSTZ", lambda b: datetime.fromisoformat(b.decode()).astimezone(timezone.utc))


class _ErrorSQL(Exception):
    """Imita un error de psycopg con su SQLSTATE: la sentencia no encuentra la tabla o la columna."""
    def __init__(self, sqlstate: str, msg: str):
        super().__init__(msg)
        self.sqlstate = sqlstate


class _Cur:
    def __init__(self, con, pool):
        self._con = con
        self._pool = pool
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        if self._pool.fallar:
            raise RuntimeError("la base no contesta (simulado)")
        try:
            cur = self._con.execute(sql.replace("%s", "?"), tuple(params or ()))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise _ErrorSQL("42P01", str(e))
            if "no such column" in str(e):
                raise _ErrorSQL("42703", str(e))
            raise
        self._filas = [dict(f) for f in cur.fetchall()] if cur.description else []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Conn:
    def __init__(self, con, pool):
        self._con, self._pool = con, pool

    def cursor(self, row_factory=None):
        return _Cur(self._con, self._pool)


class _Pool:
    def __init__(self, con):
        self.con = con
        self.usos = 0       # cuántas veces se pidió una conexión: 0 = la ruta no tocó la base
        self.fallar = False

    def connection(self):
        self.usos += 1
        conn = _Conn(self.con, self)

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


ESQUEMA_SIN_CHECK = """
CREATE TABLE proyectos (
  id INTEGER PRIMARY KEY, nombre TEXT, estado TEXT DEFAULT 'activo',
  cliente_noco_id INTEGER, borrado_en TSTZ);
CREATE TABLE cotizaciones (
  id INTEGER PRIMARY KEY, proyecto_id INTEGER, anio INTEGER, numero INTEGER, estado TEXT,
  para_nombre TEXT, para_telefono TEXT, fecha TEXT, validez_dias INTEGER, contenido TEXT,
  creado_en TSTZ, enviada_en TSTZ, borrado_en TSTZ);
"""

# (id, nombre, cliente_noco_id, borrado)
PROYECTOS = [
    (1, "Proyecto Uno", CLI_CON_TEL, None),
    (2, "Proyecto Dos", CLI_CON_TEL_Y_MAIL, None),
    (3, "Proyecto sin cliente", None, None),
    (4, "Proyecto en la papelera", CLI_CON_TEL, hace(1)),
]


def _contenido(opciones, condiciones=("CONDICION-SECRETA",)):
    """Opciones como las guarda `cotizaciones.py`: nombre + renglones (descripción, detalle, total)."""
    return ('{"opciones": [' + ", ".join(
        '{"nombre": "%s", "renglones": [%s]}' % (
            nombre, ", ".join('{"descripcion": "TEXTO-LIBRE-SECRETO", "detalle": "DETALLE-SECRETO", '
                              '"total": "%s"}' % t for t in totales))
        for nombre, totales in opciones)
        + '], "condiciones": ["%s"]}' % condiciones[0])


# (id, proyecto, numero, estado, creado hace N días, enviada hace N días o None, borrado, opciones)
COTIZACIONES = [
    (1, 1, 1, "enviada", 3, 2, None, [("Opción Uno", ["1000.00", "250.50"]), ("Opción Dos", ["2500.5"])]),
    (2, 1, 2, "borrador", 1, None, None, [("Opción Única", ["300"])]),
    (3, 2, 3, "enviada", 2, 2, None, [("Otra", ["99.9"])]),
    (4, 3, 4, "borrador", 4, None, None, [("Sin cliente", ["10"])]),
    (5, 1, 5, "aceptada", 0, 0, None, [("Estado que no existe", ["5"])]),   # por el filtro, no por la base
    (6, 1, 6, "enviada", 0, 0, hace(0), [("Borrada", ["7"])]),
    (7, 4, 7, "enviada", 0, 0, None, [("De papelera", ["8"])]),
]


def _base(proyectos=PROYECTOS, cotizaciones=COTIZACIONES, con_tabla=True):
    con = sqlite3.connect(":memory:", check_same_thread=False,
                          detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    if con_tabla:
        con.executescript(ESQUEMA_SIN_CHECK)
    else:
        con.executescript("CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, "
                          "cliente_noco_id INTEGER, borrado_en TSTZ);")
    for pid, nombre, cliente, borrado in proyectos:
        con.execute("INSERT INTO proyectos (id, nombre, cliente_noco_id, borrado_en) VALUES (?, ?, ?, ?)",
                    (pid, nombre, cliente, borrado))
    if con_tabla:
        for cid, pid, numero, estado, creado, enviada, borrado, opciones in cotizaciones:
            con.execute(
                "INSERT INTO cotizaciones (id, proyecto_id, anio, numero, estado, para_nombre, para_telefono, "
                "fecha, validez_dias, contenido, creado_en, enviada_en, borrado_en) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, pid, ANIO, numero, estado, "NOMBRE-SECRETO-DEL-PARA", "TEL-SECRETO-DEL-PARA",
                 "2026-10-20", 15, _contenido(opciones), hace(creado),
                 None if enviada is None else hace(enviada), borrado))
    con.commit()
    return con


@pytest.fixture
def embudo(monkeypatch):
    """Claves: `registro` con el permiso nuevo y `natalia` con otro permiso (no el del embudo). Noco
    simulado: la ficha 5001 tiene teléfono, la 5002 teléfono y correo, cualquier otra no existe."""
    claves0, permisos0 = config.CLAVES_API_CODE, config.PERMISOS_API_CODE
    pool0 = db.pool
    config.CLAVES_API_CODE = {CLAVE_REGISTRO: "registro", CLAVE_OTRA: "natalia"}
    config.PERMISOS_API_CODE = {
        "registro": frozenset({PERMISO}),
        "natalia": frozenset({"proyectos:leer_de_cliente"}),
    }
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0

    llamadas_noco: list[int] = []
    fichas = {
        CLI_CON_TEL: {"telefono": "tel-prueba-5001", "email": None},
        CLI_CON_TEL_Y_MAIL: {"telefono": "tel-prueba-5002", "email": "correo-secreto@ejemplo.invalido"},
    }

    async def contacto_falso(noco_id):
        llamadas_noco.append(noco_id)
        if noco_id == "caido":
            raise RuntimeError("Noco no contesta (simulado)")
        return fichas.get(noco_id)

    monkeypatch.setattr(noco_lectura, "contacto", contacto_falso)

    def poner_base(**kw):
        con = _base(**kw)
        pool = _Pool(con)
        db.pool = pool
        api_code._pedidos_por_quien.clear()
        return con, pool

    poner_base()
    try:
        yield poner_base, llamadas_noco
    finally:
        config.CLAVES_API_CODE, config.PERMISOS_API_CODE = claves0, permisos0
        db.pool = pool0


def _h(clave=CLAVE_REGISTRO):
    return {"Authorization": f"Bearer {clave}"}


def _leer(clave=CLAVE_REGISTRO):
    return CLIENTE.get(RUTA, headers=_h(clave))


def _por_id(cuerpo):
    return {c["id"]: c for c in cuerpo["cotizaciones"]}


# ═══════════════════════════════════════════════════════════════════════
# La ruta, su permiso y la puerta
# ═══════════════════════════════════════════════════════════════════════

def test_el_censo_de_rutas_dice_el_permiso_y_solo_esta_ruta_lo_usa():
    registradas = {(m, p): perm for m, p, perm in api_code.rutas_registradas(panel.app)}
    assert registradas[("GET", RUTA)] == PERMISO
    assert [k for k, v in registradas.items() if v == PERMISO] == [("GET", RUTA)]


def test_sin_clave_o_con_clave_mala_es_401_y_no_lee_nada(embudo):
    _, pool = embudo[0]()
    assert CLIENTE.get(RUTA).status_code == 401
    assert _leer("clave-inventada").status_code == 401
    assert pool.usos == 0


def test_una_clave_sin_el_permiso_es_403_y_no_lee_nada(embudo):
    poner, llamadas = embudo
    _, pool = poner()
    r = _leer(CLAVE_OTRA)
    assert r.status_code == 403
    assert "cotizacion" not in r.text.lower()
    assert pool.usos == 0
    assert llamadas == []


# ═══════════════════════════════════════════════════════════════════════
# Qué sale
# ═══════════════════════════════════════════════════════════════════════

def test_solo_borrador_y_enviada_vivas_y_de_proyectos_vivos(embudo):
    poner, _ = embudo
    poner()
    r = _leer()
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    # 2 (borrador), 3 (enviada), 1 (enviada), 4 (borrador, sin cliente). Fuera: 5 (aceptada), 6 (borrada),
    # 7 (de un proyecto en la papelera).
    assert [c["id"] for c in cuerpo["cotizaciones"]] == [2, 3, 1, 4]
    assert {c["estado"] for c in cuerpo["cotizaciones"]} == {"borrador", "enviada"}


def test_la_forma_es_la_del_contrato_y_no_sale_texto_libre(embudo):
    poner, _ = embudo
    poner()
    r = _leer()
    # Lista EXACTA de llaves: una llave nueva en la respuesta pone esta prueba roja.
    assert set(r.json()) == {"cotizaciones"}
    assert r.json()["cotizaciones"], "la prueba no vigila nada si no hay cotizaciones"
    for c in r.json()["cotizaciones"]:
        assert set(c) == FORMA
        for op in c["opciones"]:
            assert set(op) == FORMA_OPCION
    texto = r.text
    for secreto in ("TEXTO-LIBRE-SECRETO", "DETALLE-SECRETO", "CONDICION-SECRETA",
                    "NOMBRE-SECRETO-DEL-PARA", "TEL-SECRETO-DEL-PARA", "correo-secreto", "ejemplo.invalido"):
        assert secreto not in texto, secreto


def test_el_total_sale_por_opcion_y_no_se_suman_las_alternativas(embudo):
    poner, _ = embudo
    poner()
    c1 = _por_id(_leer().json())[1]
    assert c1["opciones"] == [
        {"nombre": "Opción Uno", "total": "1250.50"},
        {"nombre": "Opción Dos", "total": "2500.50"},
    ]
    assert c1["codigo"] == db.codigo_de_cotizacion(ANIO, 1)
    assert c1["enviada_en"] is not None
    assert _por_id(_leer().json())[2]["enviada_en"] is None


def test_el_telefono_sale_de_noco_una_vez_por_cliente_y_sin_cliente_es_null(embudo):
    poner, llamadas = embudo
    poner()
    cuerpo = _leer().json()
    por_id = _por_id(cuerpo)
    assert por_id[2]["telefono"] == "tel-prueba-5001"       # proyecto 1, cliente 5001
    assert por_id[3]["telefono"] == "tel-prueba-5002"       # proyecto 2, cliente 5002
    assert por_id[1]["telefono"] == "tel-prueba-5001"       # mismo cliente: sale del caché del pedido
    assert por_id[4]["telefono"] is None                    # proyecto sin cliente: no pregunta a Noco
    assert llamadas == [CLI_CON_TEL, CLI_CON_TEL_Y_MAIL]


def test_sin_telefono_en_noco_da_null_y_no_es_error(embudo, monkeypatch):
    poner, _ = embudo
    poner()

    async def ficha_sin_datos(noco_id):
        return None
    monkeypatch.setattr(noco_lectura, "contacto", ficha_sin_datos)
    r = _leer()
    assert r.status_code == 200, r.text
    assert all(c["telefono"] is None for c in r.json()["cotizaciones"])


def test_una_lista_sin_cotizaciones_es_200_vacia_y_no_es_un_error(embudo):
    poner, _ = embudo
    poner(cotizaciones=[])
    r = _leer()
    assert r.status_code == 200
    assert r.json() == {"cotizaciones": []}


# ═══════════════════════════════════════════════════════════════════════
# Errores: 502, nunca lista vacía en su lugar
# ═══════════════════════════════════════════════════════════════════════

def test_si_la_base_falla_es_502_y_no_una_lista_vacia(embudo):
    poner, llamadas = embudo
    _, pool = poner()
    pool.fallar = True
    r = _leer()
    assert r.status_code == 502
    assert "cotizaciones" not in r.text
    assert llamadas == []


def test_si_falta_la_tabla_es_502_y_no_una_lista_vacia(embudo):
    poner, _ = embudo
    poner(con_tabla=False)
    r = _leer()
    assert r.status_code == 502
    assert r.json() != {"cotizaciones": []}
    assert "cotizaciones" not in r.text


def test_si_noco_no_contesta_es_502_y_no_un_telefono_en_blanco(embudo, monkeypatch):
    poner, _ = embudo
    poner()
    # El cliente 5001 falla en Noco: toda la respuesta es 502, no una lista con telefono null.
    original = noco_lectura.contacto

    async def caido(noco_id):
        if noco_id == CLI_CON_TEL:
            raise RuntimeError("Noco no contesta (simulado)")
        return await original(noco_id)
    monkeypatch.setattr(noco_lectura, "contacto", caido)
    r = _leer()
    assert r.status_code == 502
    assert "cotizaciones" not in r.text


def test_leer_no_escribe_nada_en_la_base(embudo):
    poner, _ = embudo
    con, _ = poner()
    antes = con.total_changes
    assert _leer().status_code == 200
    assert con.total_changes == antes


def test_el_nombre_de_la_opcion_sale_en_una_linea_y_con_tope(embudo):
    """El nombre es el único texto de la cotización que sale (el servicio del seguimiento). Por el camino de
    producción: saltos y tabulaciones colapsados a espacios, y un nombre de 200 caracteres recortado a 80 con la
    marca de la base al final."""
    poner, _ = embudo
    nombre_sucio = "Línea uno\\nLínea dos\\t" + "X" * 200   # JSON escapado: llega con saltos reales
    poner(cotizaciones=[(1, 1, 1, "borrador", 1, None, None, [(nombre_sucio, ["10"])])])
    op = _leer().json()["cotizaciones"][0]["opciones"][0]
    assert set(op) == FORMA_OPCION
    assert "\n" not in op["nombre"] and "\t" not in op["nombre"]
    assert op["nombre"].startswith("Línea uno Línea dos X")
    assert len(op["nombre"]) == 80
    assert op["nombre"].endswith(api_code._MARCA_RECORTE)
    assert op["total"] == "10.00"


def test_un_nombre_corto_sale_igual_que_en_la_base(embudo):
    poner, _ = embudo
    poner(cotizaciones=[(1, 1, 1, "borrador", 1, None, None, [("Opción   Corta", ["5"])])])
    op = _leer().json()["cotizaciones"][0]["opciones"][0]
    assert op["nombre"] == "Opción Corta"   # los espacios dobles se colapsan; nada más se toca
