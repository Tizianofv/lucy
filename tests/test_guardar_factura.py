"""Guardar y leer las facturas (pieza 2 del diseño «Facturas», 21-oct-2026).

Qué se vigila, con las sentencias REALES de `db/db.py` EJECUTADAS en SQLite sobre las tablas que crean las
migraciones reales (cotizaciones y facturas):

  1. crear: el borrador nace con sus renglones y su liga en la misma transacción, SIN número (se numera al
     emitir: un borrador abandonado no deja hueco), `hecha_por = casa` con su chat; lo que no vale no toca nada;
  2. las validaciones que la base no puede hacer: ITBIS solo 18 o ninguno, dinero con dos decimales, RNC de 9 u
     11 cifras, NCF normalizado y sin repetir (anuladas incluidas), cotización del mismo proyecto;
  3. la misma sesión o trabajo no va en dos facturas vivas (la anulada la suelta); candado de asesoría por
     (tipo, ref) antes de mirar si está ocupada;
  4. corregir un borrador (candado `cambiado_en`); la emitida y la anulada ya no se corrigen;
  5. ligar y desligar sesión o trabajo (la última liga viva no se suelta); proyecto en borrador y emitida;
     cotización solo en borrador;
  6. emitir saca el número del año de HOY (no el de `fecha`) en la misma transacción; anular guarda número y NCF,
     suelta las sesiones y pasa a «no se pudo» los envíos colgados;
  7. la lectura (completa y lista) con totales calculados, nunca guardados;
  8. una sola puerta: las sentencias que escriben las cuatro tablas son las de `db/db.py` y cumplen las reglas;
     cada escritor público pide sesión de la casa antes de abrir la conexión y deja UNA huella;
  9. los vocabularios, largos y columnas que la puerta repite se comparan con el esquema.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. SQLite no impone `NUMERIC(14,2)`, no tiene `timestamptz`, no bloquea filas
    ni tiene candados de asesoría: `pg_advisory_xact_lock` es una función de la prueba que anota la llave, y lo
    que se vigila es QUE se pide antes de mirar si la sesión está ocupada, no que Postgres serialice. Tampoco se
    ejerce que dos escritores simultáneos no dejen una factura sin liga (eso lo da el bloqueo de fila del
    `UPDATE` que toma la factura). `psycopg` no se ejerce: el cursor es un doble mínimo (`%s` → `?`, filas como
    dict). `CAST(%s AS …)` de la lista y `pg_advisory_xact_lock` corren contra Postgres solo en `tools/humo.py`
    (la lista) y en el ensayo de la sala.
  · `now()` sube un microsegundo por llamada; en Postgres es la hora del inicio de la transacción.
  · Los montos salen de SQLite como `float`/entero; `_dinero_leido` los vuelve `Decimal` (en Postgres ya lo son).
  · La ruta y la pantalla no existen todavía (pieza 4): «solo ver no escribe» se prueba en la función.

Correr:  python3 -m pytest tests/test_guardar_factura.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import test_buzon_que_no_se_ve as buzon
import test_cotizaciones_tablas as ct
import test_facturas_tablas as ft
import test_grupo_ia as g
import test_numero_de_cotizacion as num
from test_grupo_ia import _ROOT

import config
import db.db as db

CASA = config.CHAT_ID_DUENO
EXTRANO = CASA + 12345        # no entra al panel (así es el de solo ver para esta función)
TABLAS_QUE_ESCRIBE_LA_PUERTA = ("facturas", "factura_renglones", "factura_ligas", "factura_envios")


class _SinTabla(Exception):
    sqlstate = "42P01"


def _a_texto(v):
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="microseconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


class _Cur:
    def __init__(self, con, registro):
        self._con, self._registro = con, registro
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        self._registro.append(sql)
        try:
            cur = self._con.execute(sql.replace("%s", "?"), tuple(_a_texto(p) for p in (params or ())))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise _SinTabla(str(e)) from None
            raise
        self._filas = ([dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()]
                       if cur.description else [])
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Conn:
    def __init__(self, con, registro):
        self._con, self._registro = con, registro

    def cursor(self, row_factory=None):
        return _Cur(self._con, self._registro)

    async def execute(self, sql, params=None):
        return await _Cur(self._con, self._registro).execute(sql, params)


class _Pool:
    """Como el de psycopg: confirma al salir bien y DESHACE todo si algo revienta."""

    def __init__(self, con, registro):
        self.con, self.registro = con, registro

    def connection(self):
        con, registro = self.con, self.registro

        class _CM:
            async def __aenter__(self):
                return _Conn(con, registro)

            async def __aexit__(self, tipo, *e):
                con.commit() if tipo is None else con.rollback()
                return False
        return _CM()


class _PoolQueNoSeToca:
    def connection(self):
        raise AssertionError("se abrió la conexión sin haber pedido sesión")


class _Mundo:
    """La base, el doble y lo que anotan: las llaves de asesoría pedidas y el SQL ejecutado, en orden."""

    def __init__(self):
        self.llaves: list = []
        self.sql: list[str] = []
        self.hoy = date(2026, 10, 21)
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.row_factory = None
        contador = iter(range(1, 10**6))
        inicio = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        con.create_function("now", 0, lambda: _a_texto(inicio + timedelta(microseconds=next(contador))))
        con.create_function("pg_advisory_xact_lock", 1, lambda k: self.llaves.append(k))
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, cliente_nombre TEXT, borrado_en TEXT)")
        for i in (1, 2, 3):
            con.execute("INSERT INTO proyectos (id, nombre, cliente_nombre) VALUES (?, ?, 'Cliente')", (i, f"Proyecto {i}"))
        con.execute("UPDATE proyectos SET borrado_en = '2026-10-01' WHERE id = 3")
        con.execute("CREATE TABLE log_acciones (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT, "
                    "tabla TEXT, registro_id INTEGER, antes TEXT, despues TEXT, motivo TEXT, bandeja_id INTEGER)")
        for texto in (ct._texto_migracion(), ct._texto_migracion_de_drive()):
            for s in g._sentencias(texto):
                if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
                    con.execute(ct._a_sqlite(s))
        for s in ft._sentencias_que_corren(ft._texto_migracion()):
            con.execute(ft._a_sqlite(s))
        for s in ft._sentencias_que_corren(ft._texto_migracion_de_drive()):       # pieza 8: el Drive de las facturas
            con.execute(ft._a_sqlite(s))
        for s in ft._sentencias_que_corren(ft._texto_migracion_de_natalia()):     # pieza 9: la tarjeta de los envíos
            con.execute(ft._a_sqlite(s))
        dos_opciones = json.dumps({"opciones": [{"nombre": "A", "renglones": []}, {"nombre": "B", "renglones": []}]})
        una_opcion = json.dumps({"opciones": [{"nombre": "A", "renglones": []}]})
        for cid, proyecto, contenido in ((1, 1, dos_opciones), (2, 2, una_opcion), (3, 1, una_opcion)):
            ct._insertar(con, proyecto_id=proyecto, numero=cid, contenido=contenido)
        con.execute("UPDATE cotizaciones SET borrado_en = '2026-10-01' WHERE id = 3")
        con.commit()
        self.con = con


@pytest.fixture
def mundo(monkeypatch):
    m = _Mundo()
    monkeypatch.setattr(db, "pool", _Pool(m.con, m.sql))
    monkeypatch.setattr(db, "hoy_rd", lambda: m.hoy)
    assert EXTRANO not in config.CHAT_IDS_PERMITIDOS
    return m


def correr(coro):
    return asyncio.run(coro)


RENGLONES = [{"descripcion": "Ensayo 1", "monto": "1000.00"},
             {"descripcion": "Ensayo 2", "detalle": "dos horas", "monto": "2500.50"}]


def crear(chat=CASA, **kw):
    base = dict(cliente_noco_id=1001, para_nombre="Cliente de prueba", renglones=RENGLONES,
                liga={"tipo": "sesion", "ref": "ses-1"})
    base.update(kw)
    return correr(db.crear_borrador_de_factura(chat, **base))


_SESIONES = iter(range(1, 10**6))


def otra(**kw):
    """Una factura con una sesión que nadie más tiene."""
    kw.setdefault("liga", {"tipo": "sesion", "ref": f"ses-nueva-{next(_SESIONES)}"})
    return crear(**kw)


def emitir(f, chat=CASA):
    return correr(db.emitir_factura(f["id"], chat, f["cambiado_en"]))


def leer(f):
    return correr(db.factura_por_id(f["id"] if isinstance(f, dict) else f))


def filas(m, tabla):
    cur = m.con.execute(f"SELECT * FROM {tabla} ORDER BY 1")
    return [dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()]


def foto(m):
    """Todo lo que la puerta puede escribir, más la serie y las huellas."""
    return {t: filas(m, t) for t in (*TABLAS_QUE_ESCRIBE_LA_PUERTA, "serie_facturas", "log_acciones")}


def huellas(m):
    return [(h["accion"], h["tabla"], h["registro_id"]) for h in filas(m, "log_acciones")]


def no_vale(clave, llamada):
    with pytest.raises(db.FacturaNoVale) as e:
        llamada()
    assert e.value.clave == clave, (e.value.clave, str(e.value))


# ── 1. Crear ────────────────────────────────────────────────────────────────────────────────────────

def test_un_borrador_nace_con_renglones_liga_y_huella_pero_sin_numero(mundo):
    f = crear()
    assert (f["estado"], f["anio"], f["numero"], f["codigo"], f["creada"]) == ("borrador", None, None, None, True)
    assert (f["hecha_por"], f["hecha_por_chat_id"], f["emitida_en"], f["itbis_pct"]) == ("casa", CASA, None, None)
    assert [(r["orden"], r["descripcion"], r["detalle"], r["monto"]) for r in f["renglones"]] == \
        [(1, "Ensayo 1", None, Decimal("1000.00")), (2, "Ensayo 2", "dos horas", Decimal("2500.50"))]
    assert [(l["tipo"], l["ref"], l["viva"]) for l in f["ligas"]] == [("sesion", "ses-1", True)]
    assert filas(mundo, "serie_facturas") == []
    assert huellas(mundo) == [("crear", "facturas", f["id"])]


def test_crear_no_gasta_numero_y_el_numero_sale_al_emitir_sin_huecos(mundo):
    borradores = [otra() for _ in range(3)]
    assert filas(mundo, "serie_facturas") == []
    e = emitir(borradores[2])
    assert (e["codigo"], e["estado"]) == ("FAC-2026-001", "emitida")
    assert emitir(borradores[0])["codigo"] == "FAC-2026-002"
    assert [r["ultimo"] for r in filas(mundo, "serie_facturas")] == [2]
    assert leer(borradores[1])["codigo"] is None


def test_quien_no_entra_al_panel_no_crea_nada(mundo):
    antes = foto(mundo)
    with pytest.raises(db.FacturaSinSesion):
        crear(chat=EXTRANO)
    with pytest.raises(db.FacturaSinSesion):
        crear(chat=None)
    assert foto(mundo) == antes


def test_una_factura_sin_liga_o_con_la_liga_mal_no_se_crea(mundo):
    antes = foto(mundo)
    for liga, clave in ((None, "liga_forma"), ({}, "liga_forma"), ({"tipo": "sesion"}, "liga_forma"),
                        ({"tipo": "sesion", "ref": "a", "otro": 1}, "liga_forma"),
                        ({"tipo": "proyecto", "ref": "a"}, "liga_tipo"),
                        ({"tipo": "sesion", "ref": ""}, "liga_ref"), ({"tipo": "sesion", "ref": " a"}, "liga_ref"),
                        ({"tipo": "sesion", "ref": "a\nb"}, "liga_ref"), ({"tipo": "sesion", "ref": "x" * 101}, "liga_ref"),
                        ({"tipo": "sesion", "ref": True}, "liga_ref"), ({"tipo": "sesion", "ref": None}, "liga_ref")):
        no_vale(clave, lambda liga=liga: crear(liga=liga))
    assert foto(mundo) == antes


def test_la_referencia_se_guarda_tal_cual_llega_y_un_entero_se_vuelve_texto(mundo):
    assert crear(liga={"tipo": "trabajo", "ref": 77, "codigo": "TR-77"})["ligas"][0]["ref"] == "77"
    f = otra(liga={"tipo": "sesion", "ref": "A-b/Ñ 3", "codigo": "  SES-3 "})
    assert (f["ligas"][0]["ref"], f["ligas"][0]["codigo"]) == ("A-b/Ñ 3", "SES-3")


@pytest.mark.parametrize("kw,clave", [
    ({"cliente_noco_id": 0}, "cliente_tipo"), ({"cliente_noco_id": True}, "cliente_tipo"),
    ({"cliente_noco_id": "5"}, "cliente_tipo"),
    ({"para_nombre": "  "}, "para_nombre_vacio"), ({"para_nombre": "x" * 201}, "para_nombre_largo"),
    ({"para_nombre": None}, "para_nombre_vacio"), ({"para_nombre": 5}, "tipo"),
    ({"fecha": "ayer"}, "fecha"), ({"fecha": 5}, "fecha"),
    ({"renglones": []}, "sin_renglones"), ({"renglones": "x"}, "renglones_tipo"), ({"renglones": {"a": 1}}, "renglones_tipo"),
    ({"renglones": [{"descripcion": "a"}]}, "renglon_forma"), ({"renglones": [{"monto": "5"}]}, "renglon_forma"),
    ({"renglones": [{"descripcion": "a", "monto": "5", "extra": 1}]}, "renglon_forma"), ({"renglones": ["a"]}, "renglon_forma"),
    ({"renglones": [{"descripcion": "", "monto": "5"}]}, "descripcion_vacio"),
    ({"renglones": [{"descripcion": "x" * 301, "monto": "5"}]}, "descripcion_largo"),
    ({"renglones": [{"descripcion": "a", "monto": "5", "detalle": "x" * 2001}]}, "detalle_largo"),
    ({"renglones": [{"descripcion": "a", "monto": "5"}] * 101}, "renglones_muchos"),
    ({"renglones": [{"descripcion": "a", "monto": "0"}]}, "monto_cero"),
    ({"renglones": [{"descripcion": "a", "monto": "-5"}]}, "monto_formato"),
    ({"renglones": [{"descripcion": "a", "monto": 1.5}]}, "monto_tipo"),
    ({"renglones": [{"descripcion": "a", "monto": "100.505"}]}, "monto_decimales"),
    ({"renglones": [{"descripcion": "a", "monto": Decimal("100.505")}]}, "monto_decimales"),
    ({"renglones": [{"descripcion": "a", "monto": "100000000"}]}, "monto_alto"),
    ({"renglones": [{"descripcion": "a", "monto": "abc"}]}, "monto_formato"),
    ({"renglones": [{"descripcion": "a", "monto": None}]}, "monto_vacio"),
    ({"itbis_pct": 16}, "itbis"), ({"itbis_pct": 0}, "itbis"), ({"itbis_pct": 18.0}, "itbis"),
    ({"itbis_pct": True}, "itbis"), ({"itbis_pct": "18%"}, "itbis"), ({"itbis_pct": "NaN"}, "itbis"),
    ({"itbis_pct": Decimal("18.5")}, "itbis"),
    ({"rnc_cedula": "12345678"}, "rnc_formato"), ({"rnc_cedula": "1234567890"}, "rnc_formato"),
    ({"rnc_cedula": "12345678A"}, "rnc_formato"), ({"rnc_cedula": "١٢٣٤٥٦٧٨٩"}, "rnc_formato"),
    ({"rnc_cedula": 123456789}, "rnc_tipo"),
    ({"ncf": "B0100000001"}, "ncf_incompleto"), ({"ncf_tipo": "B01"}, "ncf_incompleto"),
    ({"ncf": "B01-00 ñ", "ncf_tipo": "B01"}, "ncf_formato"), ({"ncf": "ßB01", "ncf_tipo": "B01"}, "ncf_formato"),
    ({"ncf": "B" * 21, "ncf_tipo": "B01"}, "ncf_formato"), ({"ncf": " - ", "ncf_tipo": "B01"}, "ncf_formato"),
    ({"ncf": 5, "ncf_tipo": "B01"}, "ncf_tipo"), ({"ncf": "B01", "ncf_tipo": "x" * 21}, "ncf_tipo_largo"),
    ({"proyecto_id": 0}, "proyecto_tipo"), ({"cotizacion_opcion": 1}, "opcion_sin_cotizacion"),
    ({"cotizacion_id": 1, "cotizacion_opcion": 0}, "opcion_tipo"),
    ({"cotizacion_id": 1, "cotizacion_opcion": 3}, "opcion_no_esta"),
])
def test_lo_que_no_vale_no_escribe_ni_gasta_numero(mundo, kw, clave):
    antes = foto(mundo)
    no_vale(clave, lambda: crear(**kw))
    assert foto(mundo) == antes


# ── 2. Lo que la base no hace ───────────────────────────────────────────────────────────────────────

def test_el_itbis_es_el_18_o_ninguno_y_los_totales_se_calculan(mundo):
    sin = crear()
    assert (sin["itbis_pct"], sin["subtotal"], sin["itbis"], sin["total"]) == \
        (None, Decimal("3500.50"), Decimal("0.00"), Decimal("3500.50"))
    for i, crudo in enumerate((18, "18", "18.00", Decimal("18"), " 18 ")):
        f = otra(itbis_pct=crudo)
        assert f["itbis_pct"] == Decimal("18.00")
        assert (f["subtotal"], f["itbis"], f["total"]) == (Decimal("3500.50"), Decimal("630.09"), Decimal("4130.59"))
    assert db.ITBIS_DE_FACTURA == Decimal("18")


def test_el_itbis_redondea_a_centavos_con_la_mitad_hacia_arriba():
    assert db.totales_de_factura([Decimal("10.25")], Decimal("18")) == \
        {"subtotal": Decimal("10.25"), "itbis": Decimal("1.85"), "total": Decimal("12.10")}      # 1.845
    assert db.totales_de_factura([Decimal("0.03"), Decimal("0.02")], Decimal("18"))["itbis"] == Decimal("0.01")   # 0.009
    assert db.totales_de_factura([Decimal("1.00")], None)["itbis"] == Decimal("0.00")


def test_los_montos_con_dos_decimales_se_guardan_y_los_de_mas_se_rechazan(mundo):
    f = crear(renglones=[{"descripcion": "a", "monto": "1,250.5"}, {"descripcion": "b", "monto": 7},
                         {"descripcion": "c", "monto": Decimal("0.10")}])
    assert [r["monto"] for r in f["renglones"]] == [Decimal("1250.50"), Decimal("7.00"), Decimal("0.10")]
    assert f["subtotal"] == Decimal("1257.60")


def test_rnc_o_cedula_se_guarda_solo_con_cifras(mundo):
    assert otra(rnc_cedula="101-00000-1")["rnc_cedula"] == "101000001"
    assert otra(rnc_cedula=" 001-0000000-1 ")["rnc_cedula"] == "00100000001"
    assert otra(rnc_cedula=None)["rnc_cedula"] is None and otra(rnc_cedula="")["rnc_cedula"] is None


def test_el_ncf_se_guarda_normalizado_y_no_se_repite_ni_con_la_anulada(mundo):
    f = crear(ncf=" b01-0000 0001 ", ncf_tipo=" B01 ")
    assert (f["ncf"], f["ncf_tipo"]) == ("B0100000001", "B01")
    for escrito in ("B0100000001", "b01-0000-0001", " B01 0000 0001"):
        no_vale("ncf_repetido", lambda e=escrito: otra(ncf=e, ncf_tipo="B01"))
    e = emitir(f)
    correr(db.anular_factura(e["id"], CASA, "se hizo por error"))
    no_vale("ncf_repetido", lambda: otra(ncf="B0100000001", ncf_tipo="B01"))
    assert otra(ncf="B0100000002", ncf_tipo="B01")["ncf"] == "B0100000002"


def test_la_forma_del_ncf_de_la_puerta_es_la_que_acepta_la_base(mundo):
    """Lo que `ncf_normalizado` deja pasar, la base lo acepta; lo que rechaza por largo o forma, la base también."""
    for bueno in ("B01", "E310000000001", "A" * 20, "0"):
        assert db.ncf_normalizado(bueno) == bueno
        mundo.con.execute("INSERT INTO facturas (cliente_noco_id, para_nombre, fecha, hecha_por, hecha_por_chat_id, ncf, ncf_tipo) "
                          "VALUES (1, 'x', '2026-10-21', 'casa', 1, ?, 'B01')", (bueno,))
    for malo in ("", "b01", "B 01", "B-01", "B01Ñ"):
        assert not re.fullmatch(r"[A-Z0-9]{1,20}", malo, re.A)
    no_vale("ncf_formato", lambda: db.ncf_normalizado("A" * 21))


def test_cotizacion_y_proyecto(mundo):
    f = crear(proyecto_id=1, cotizacion_id=1, cotizacion_opcion=2)
    assert (f["proyecto_id"], f["cotizacion_id"], f["cotizacion_opcion"]) == (1, 1, 2)
    assert otra(cotizacion_id=2)["proyecto_id"] is None            # sin proyecto, la cotización basta con existir
    antes = foto(mundo)
    no_vale("cotizacion_de_otro_proyecto", lambda: otra(proyecto_id=2, cotizacion_id=1))
    no_vale("opcion_no_esta", lambda: otra(proyecto_id=2, cotizacion_id=2, cotizacion_opcion=2))
    for kw in ({"proyecto_id": 3}, {"proyecto_id": 99}, {"cotizacion_id": 3}, {"cotizacion_id": 99}):
        with pytest.raises(db.FacturaNoEsta):
            otra(**kw)
    assert [x for x in foto(mundo)["facturas"]] == antes["facturas"]


# ── 3. La misma sesión en dos facturas ──────────────────────────────────────────────────────────────

def test_la_misma_sesion_no_va_en_dos_facturas_vivas_pero_si_en_una_anulada(mundo):
    primera = crear(liga={"tipo": "sesion", "ref": "ses-9"})
    no_vale("sesion_ya_facturada", lambda: crear(liga={"tipo": "sesion", "ref": "ses-9"}))
    assert crear(liga={"tipo": "trabajo", "ref": "ses-9"})["ligas"][0]["tipo"] == "trabajo"      # otro tipo, otra cosa
    e = emitir(primera)
    no_vale("sesion_ya_facturada", lambda: crear(liga={"tipo": "sesion", "ref": "ses-9"}))      # ni con la emitida
    correr(db.anular_factura(e["id"], CASA, "se hizo por error"))
    assert crear(liga={"tipo": "sesion", "ref": "ses-9"})["estado"] == "borrador"                # la anulada la soltó


def test_pedir_el_candado_de_la_sesion_va_antes_de_mirar_si_esta_ocupada(mundo):
    crear(liga={"tipo": "sesion", "ref": "ses-1"})
    assert mundo.llaves == [db._llave_de_asesoria_de_liga("sesion", "ses-1")]
    sql = mundo.sql
    assert sql.index(db.SENTENCIA_DEL_CANDADO_DE_LIGA) < sql.index(db.SENTENCIA_DE_LA_LIGA_OCUPADA) < \
        sql.index(db.SENTENCIA_DE_CREAR_FACTURA)
    otra_f = otra()
    mundo.llaves.clear()
    correr(db.ligar_factura_a_registro(otra_f["id"], CASA, "trabajo", "tr-1"))
    assert mundo.llaves == [db._llave_de_asesoria_de_liga("trabajo", "tr-1")]


def test_las_llaves_de_asesoria_son_estables_distintas_y_caben_en_64_bits():
    llaves = {db._llave_de_asesoria_de_liga(t, r) for t in db.TIPOS_DE_LIGA_DE_FACTURA for r in ("1", "2", "ses-1")}
    assert len(llaves) == 6
    assert all(-2**63 <= k < 2**63 for k in llaves)
    assert db._llave_de_asesoria_de_liga("sesion", "1") == db._llave_de_asesoria_de_liga("sesion", "1")


# ── 4. Corregir un borrador ─────────────────────────────────────────────────────────────────────────

def test_corregir_cambia_lo_pedido_deja_lo_demas_y_sube_el_candado(mundo):
    f = crear(ncf="B01", ncf_tipo="B01", rnc_cedula="101000001")
    r = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {
        "para_nombre": "Otro nombre", "cliente_noco_id": 2002, "fecha": "2026-10-01", "itbis_pct": 18,
        "renglones": [{"descripcion": "Solo uno", "monto": "50"}]}))
    assert (r["para_nombre"], r["cliente_noco_id"], r["fecha"], r["itbis_pct"]) == \
        ("Otro nombre", 2002, "2026-10-01", Decimal("18.00"))
    assert (r["ncf"], r["rnc_cedula"], r["creada"]) == ("B01", "101000001", False)
    assert [(x["orden"], x["monto"]) for x in r["renglones"]] == [(1, Decimal("50.00"))]
    assert r["cambiado_en"] != f["cambiado_en"] and r["total"] == Decimal("59.00")
    assert [h[0] for h in huellas(mundo)] == ["crear", "corregir"]
    h = filas(mundo, "log_acciones")[1]
    assert json.loads(h["antes"])["para_nombre"] == "Cliente de prueba" and json.loads(h["despues"])["para_nombre"] == "Otro nombre"


def test_corregir_el_ncf_lo_pone_lo_cambia_y_lo_quita_siempre_en_pareja(mundo):
    f = crear()
    f = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"ncf": "b01 77", "ncf_tipo": "B01"}))
    assert (f["ncf"], f["ncf_tipo"]) == ("B0177", "B01")
    no_vale("ncf_incompleto", lambda: correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"ncf": None})))
    f = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"ncf_tipo": "B02"}))
    assert (f["ncf"], f["ncf_tipo"]) == ("B0177", "B02")
    f = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"ncf": None, "ncf_tipo": None}))
    assert (f["ncf"], f["ncf_tipo"]) == (None, None)
    otro = otra(ncf="B05", ncf_tipo="B01")
    no_vale("ncf_repetido", lambda: correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"ncf": "b05", "ncf_tipo": "B01"})))
    # su propio NCF no choca consigo mismo
    again = correr(db.corregir_borrador_de_factura(otro["id"], CASA, otro["cambiado_en"], {"ncf": "B05", "ncf_tipo": "B01"}))
    assert again["ncf"] == "B05"


def test_el_itbis_y_el_rnc_se_quitan_con_none(mundo):
    f = crear(itbis_pct=18, rnc_cedula="101000001")
    f = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"itbis_pct": None, "rnc_cedula": None}))
    assert (f["itbis_pct"], f["rnc_cedula"], f["itbis"]) == (None, None, Decimal("0.00"))


def test_con_un_candado_viejo_no_se_corrige_nada(mundo):
    f = crear()
    correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"para_nombre": "Primero"}))
    antes = foto(mundo)
    with pytest.raises(db.FacturaCambio):
        correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"para_nombre": "Segundo"}))
    assert foto(mundo) == antes
    for malo in (None, 5, "ayer"):
        no_vale("candado", lambda m=malo: correr(db.corregir_borrador_de_factura(f["id"], CASA, m, {"para_nombre": "x"})))
    with pytest.raises(db.FacturaNoEsta):
        correr(db.corregir_borrador_de_factura(999, CASA, f["cambiado_en"], {"para_nombre": "x"}))


@pytest.mark.parametrize("cambios,clave", [
    ({}, "sin_cambios"), (None, "sin_cambios"), ({"estado": "emitida"}, "campo_no_se_corrige"),
    ({"numero": 5}, "campo_no_se_corrige"), ({"proyecto_id": 1}, "campo_no_se_corrige"),
    ({"hecha_por": "natalia"}, "campo_no_se_corrige"), ({"cotizacion_id": 1}, "campo_no_se_corrige"),
    ({"para_nombre": ""}, "para_nombre_vacio"), ({"renglones": []}, "sin_renglones"), ({"itbis_pct": 16}, "itbis"),
])
def test_corregir_lo_que_no_vale_no_escribe(mundo, cambios, clave):
    f = crear()
    antes = foto(mundo)
    no_vale(clave, lambda: correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], cambios)))
    assert foto(mundo) == antes


def test_una_emitida_o_anulada_ya_no_se_corrige_ni_en_renglones_montos_o_ncf(mundo):
    f = crear(ncf="B01", ncf_tipo="B01", itbis_pct=18)
    e = emitir(f)
    antes = foto(mundo)
    for cambios in ({"renglones": [{"descripcion": "otro", "monto": "1"}]}, {"itbis_pct": None}, {"ncf": "B09", "ncf_tipo": "B01"},
                    {"para_nombre": "otro"}, {"rnc_cedula": "101000001"}):
        with pytest.raises(db.FacturaYaNoSeCorrige) as err:
            correr(db.corregir_borrador_de_factura(e["id"], CASA, e["cambiado_en"], cambios))
        assert err.value.estado == "emitida"
    assert foto(mundo) == antes
    a = correr(db.anular_factura(e["id"], CASA, "motivo"))
    with pytest.raises(db.FacturaYaNoSeCorrige) as err:
        correr(db.corregir_borrador_de_factura(a["id"], CASA, a["cambiado_en"], {"para_nombre": "x"}))
    assert err.value.estado == "anulada"


# ── 5. Ligar y desligar ─────────────────────────────────────────────────────────────────────────────

def test_ligar_otra_sesion_y_soltar_una_pero_nunca_la_ultima(mundo):
    f = crear(liga={"tipo": "sesion", "ref": "a"})
    f = correr(db.ligar_factura_a_registro(f["id"], CASA, "trabajo", "t1", "TR-1"))
    assert [(l["tipo"], l["ref"], l["codigo"], l["viva"]) for l in f["ligas"]] == \
        [("sesion", "a", None, True), ("trabajo", "t1", "TR-1", True)]
    f = correr(db.desligar_factura_de_registro(f["id"], CASA, "sesion", "a"))
    assert [(l["ref"], l["viva"]) for l in f["ligas"]] == [("a", False), ("t1", True)]
    assert len(filas(mundo, "factura_ligas")) == 2                      # no se borra: queda con `desligada_en`
    antes = foto(mundo)
    no_vale("ultima_liga", lambda: correr(db.desligar_factura_de_registro(f["id"], CASA, "trabajo", "t1")))
    no_vale("liga_no_esta", lambda: correr(db.desligar_factura_de_registro(f["id"], CASA, "sesion", "a")))
    no_vale("liga_no_esta", lambda: correr(db.desligar_factura_de_registro(f["id"], CASA, "sesion", "zzz")))
    assert foto(mundo) == antes
    # soltada y ligada otra vez: el índice parcial lo permite
    f = correr(db.ligar_factura_a_registro(f["id"], CASA, "sesion", "a"))
    assert [(l["ref"], l["viva"]) for l in f["ligas"]] == [("a", False), ("t1", True), ("a", True)]
    assert [h[0] for h in huellas(mundo)] == ["crear", "ligar", "desligar", "ligar"]


def test_ligar_lo_que_ya_esta_o_lo_que_no_vale_no_escribe(mundo):
    f = crear(liga={"tipo": "sesion", "ref": "a"})
    otra_f = crear(liga={"tipo": "sesion", "ref": "b"})
    antes = foto(mundo)
    no_vale("ya_ligada", lambda: correr(db.ligar_factura_a_registro(f["id"], CASA, "sesion", "a")))
    no_vale("sesion_ya_facturada", lambda: correr(db.ligar_factura_a_registro(f["id"], CASA, "sesion", "b")))
    no_vale("liga_tipo", lambda: correr(db.ligar_factura_a_registro(f["id"], CASA, "proyecto", "b")))
    no_vale("liga_ref", lambda: correr(db.ligar_factura_a_registro(f["id"], CASA, "sesion", " b")))
    with pytest.raises(db.FacturaNoEsta):
        correr(db.ligar_factura_a_registro(999, CASA, "sesion", "zz"))
    assert foto(mundo) == antes and otra_f


def test_ligar_o_desligar_sesiones_cambia_el_candado_de_la_factura(mundo):
    f = crear(liga={"tipo": "sesion", "ref": "a"})
    g1 = correr(db.ligar_factura_a_registro(f["id"], CASA, "sesion", "b"))
    assert g1["cambiado_en"] != f["cambiado_en"]
    with pytest.raises(db.FacturaCambio):
        correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"]))


def test_una_emitida_o_anulada_no_cambia_de_sesiones_ni_de_cotizacion(mundo):
    e = emitir(crear(liga={"tipo": "sesion", "ref": "a"}))
    antes = foto(mundo)
    for llamada in (lambda: db.ligar_factura_a_registro(e["id"], CASA, "sesion", "nueva"),
                    lambda: db.desligar_factura_de_registro(e["id"], CASA, "sesion", "a"),
                    lambda: db.poner_cotizacion_de_factura(e["id"], CASA, 1)):
        with pytest.raises(db.FacturaYaNoSeCorrige):
            correr(llamada())
    assert foto(mundo) == antes
    a = correr(db.anular_factura(e["id"], CASA, "motivo"))
    for llamada in (lambda: db.ligar_factura_a_registro(a["id"], CASA, "sesion", "nueva"),
                    lambda: db.poner_proyecto_de_factura(a["id"], CASA, 1)):
        with pytest.raises(db.FacturaYaNoSeCorrige) as err:
            correr(llamada())
        assert err.value.estado == "anulada"


def test_el_proyecto_se_pone_y_se_quita_en_borrador_y_en_emitida_sin_tocar_lo_que_dice_la_factura(mundo):
    f = crear(itbis_pct=18)
    f = correr(db.poner_proyecto_de_factura(f["id"], CASA, 1))
    assert f["proyecto_id"] == 1
    e = emitir(f)
    renglones, total, ncf = e["renglones"], e["total"], e["ncf"]
    e = correr(db.poner_proyecto_de_factura(e["id"], CASA, 2))
    assert (e["proyecto_id"], e["estado"], e["codigo"]) == (2, "emitida", "FAC-2026-001")
    assert (e["renglones"], e["total"], e["ncf"]) == (renglones, total, ncf)
    e = correr(db.poner_proyecto_de_factura(e["id"], CASA, None))
    assert e["proyecto_id"] is None
    assert [h[0] for h in huellas(mundo)] == ["crear", "ligar_proyecto", "emitir", "ligar_proyecto", "desligar_proyecto"]
    with pytest.raises(db.FacturaNoEsta):
        correr(db.poner_proyecto_de_factura(e["id"], CASA, 3))        # en la papelera
    with pytest.raises(db.FacturaNoEsta):
        correr(db.poner_proyecto_de_factura(e["id"], CASA, 99))
    no_vale("proyecto_tipo", lambda: correr(db.poner_proyecto_de_factura(e["id"], CASA, 0)))


def test_cambiar_de_proyecto_respeta_la_cotizacion_que_ya_tiene(mundo):
    f = crear(proyecto_id=1, cotizacion_id=1)
    no_vale("cotizacion_de_otro_proyecto", lambda: correr(db.poner_proyecto_de_factura(f["id"], CASA, 2)))
    assert leer(f)["proyecto_id"] == 1
    f = correr(db.poner_cotizacion_de_factura(f["id"], CASA, None))
    assert correr(db.poner_proyecto_de_factura(f["id"], CASA, 2))["proyecto_id"] == 2


def test_la_cotizacion_se_pone_con_su_opcion_y_se_quita_con_ella(mundo):
    f = crear(proyecto_id=1)
    f = correr(db.poner_cotizacion_de_factura(f["id"], CASA, 1, 2))
    assert (f["cotizacion_id"], f["cotizacion_opcion"]) == (1, 2)
    no_vale("cotizacion_de_otro_proyecto", lambda: correr(db.poner_cotizacion_de_factura(f["id"], CASA, 2)))
    no_vale("opcion_no_esta", lambda: correr(db.poner_cotizacion_de_factura(f["id"], CASA, 1, 3)))
    no_vale("opcion_sin_cotizacion", lambda: correr(db.poner_cotizacion_de_factura(f["id"], CASA, None, 1)))
    with pytest.raises(db.FacturaNoEsta):
        correr(db.poner_cotizacion_de_factura(f["id"], CASA, 3))
    f = correr(db.poner_cotizacion_de_factura(f["id"], CASA, None))
    assert (f["cotizacion_id"], f["cotizacion_opcion"]) == (None, None)
    assert [h[0] for h in huellas(mundo)][-2:] == ["ligar_cotizacion", "desligar_cotizacion"]


# ── 6. Emitir y anular ──────────────────────────────────────────────────────────────────────────────

def test_emitir_numera_con_el_ano_de_hoy_no_el_de_la_fecha_de_la_factura(mundo):
    mundo.hoy = date(2027, 1, 2)
    e = emitir(crear(fecha="2026-12-31"))
    assert (e["anio"], e["numero"], e["codigo"], e["fecha"]) == (2027, 1, "FAC-2027-001", "2026-12-31")
    assert e["emitida_en"] is not None
    mundo.hoy = date(2026, 12, 31)
    assert emitir(otra())["codigo"] == "FAC-2026-001"             # la serie de otro año es otra
    assert emitir(otra())["codigo"] == "FAC-2026-002"


def test_emitir_pide_el_candado_y_un_error_no_gasta_numero(mundo):
    f = crear()
    f2 = correr(db.corregir_borrador_de_factura(f["id"], CASA, f["cambiado_en"], {"para_nombre": "otro"}))
    with pytest.raises(db.FacturaCambio):
        correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"]))
    assert filas(mundo, "serie_facturas") == []
    no_vale("candado", lambda: correr(db.emitir_factura(f["id"], CASA, None)))
    with pytest.raises(db.FacturaNoEsta):
        correr(db.emitir_factura(999, CASA, f["cambiado_en"]))
    e = emitir(f2)
    with pytest.raises(db.FacturaYaNoSeCorrige):
        correr(db.emitir_factura(e["id"], CASA, e["cambiado_en"]))
    assert [r["ultimo"] for r in filas(mundo, "serie_facturas")] == [1]


def test_una_factura_sin_liga_viva_no_se_emite_y_el_numero_vuelve(mundo):
    f = crear()
    mundo.con.execute("UPDATE factura_ligas SET desligada_en = '2026-10-21'")      # lo que la puerta no permite
    no_vale("sin_liga", lambda: correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"])))
    mundo.con.execute("DELETE FROM factura_renglones")
    mundo.con.execute("UPDATE factura_ligas SET desligada_en = NULL")
    no_vale("sin_renglones", lambda: correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"])))
    assert filas(mundo, "serie_facturas") == [] and leer(f)["estado"] == "borrador"


def test_si_la_escritura_falla_despues_de_sacar_el_numero_el_numero_se_deshace(mundo, monkeypatch):
    f = crear()
    monkeypatch.setattr(db, "SENTENCIA_DE_EMITIR", "UPDATE facturas SET columna_que_no_existe = %s WHERE id = %s AND anio = %s")
    with pytest.raises(Exception):
        correr(db.emitir_factura(f["id"], CASA, f["cambiado_en"]))
    assert filas(mundo, "serie_facturas") == [] and leer(f)["estado"] == "borrador"
    assert db.SENTENCIA_DEL_NUMERO_DE_FACTURA in mundo.sql            # el número SÍ se había sacado antes de fallar


def test_anular_guarda_numero_y_ncf_suelta_las_sesiones_y_cierra_los_envios_colgados(mundo):
    e = emitir(crear(ncf="B01", ncf_tipo="B01", liga={"tipo": "sesion", "ref": "ses-1"}))
    for canal, estado, extra in (("whatsapp", "pendiente", ""), ("correo", "aprobado", ", aprobado_por_chat_id = 7, aprobado_en = '2026-10-21'"),
                                 ("mano", "enviado", ", enviado_en = '2026-10-21'"), ("whatsapp", "rechazado", ", motivo = 'no'")):
        mundo.con.execute("INSERT INTO factura_envios (factura_id, canal, estado) VALUES (?, ?, 'pendiente')", (e["id"], canal))
        mundo.con.execute(f"UPDATE factura_envios SET estado = '{estado}'{extra} WHERE id = (SELECT max(id) FROM factura_envios)")
    a = correr(db.anular_factura(e["id"], CASA, "  Se emitió por error  "))
    assert (a["estado"], a["codigo"], a["ncf"], a["motivo_anulacion"], a["anulada_por_chat_id"]) == \
        ("anulada", "FAC-2026-001", "B01", "Se emitió por error", CASA)
    assert a["anulada_en"] is not None
    envios = filas(mundo, "factura_envios")
    assert [(x["canal"], x["estado"]) for x in envios] == [("whatsapp", "no_se_pudo"), ("correo", "no_se_pudo"),
                                                           ("mano", "enviado"), ("whatsapp", "rechazado")]
    assert envios[0]["motivo"] == "la factura se anuló" and envios[3]["motivo"] == "no"
    assert crear(liga={"tipo": "sesion", "ref": "ses-1"})["estado"] == "borrador"
    assert huellas(mundo)[:3] == [("crear", "facturas", e["id"]), ("emitir", "facturas", e["id"]), ("anular", "facturas", e["id"])]


def test_anular_pide_motivo_y_solo_una_emitida(mundo):
    b = crear()
    with pytest.raises(db.FacturaYaNoSeCorrige) as err:
        correr(db.anular_factura(b["id"], CASA, "motivo"))
    assert err.value.estado == "borrador"
    e = emitir(b)
    antes = foto(mundo)
    for motivo in ("", "   ", None, 5, "x" * 501):
        with pytest.raises(db.FacturaNoVale):
            correr(db.anular_factura(e["id"], CASA, motivo))
    assert foto(mundo) == antes
    a = correr(db.anular_factura(e["id"], CASA, "x" * 500))
    with pytest.raises(db.FacturaYaNoSeCorrige) as err:
        correr(db.anular_factura(a["id"], CASA, "otra vez"))
    assert err.value.estado == "anulada"
    with pytest.raises(db.FacturaNoEsta):
        correr(db.anular_factura(999, CASA, "motivo"))


# ── 7. Leer ─────────────────────────────────────────────────────────────────────────────────────────

def test_leer_una_factura_trae_todo_calculado_y_lo_que_no_existe_da_none(mundo):
    f = crear(itbis_pct=18, proyecto_id=1)
    e = emitir(f)
    leida = leer(e["id"])
    assert leida["codigo"] == "FAC-2026-001" and leida["total"] == Decimal("4130.59")
    assert (leida["subtotal"], leida["itbis"]) == (Decimal("3500.50"), Decimal("630.09"))
    assert leida == {k: v for k, v in e.items() if k != "creada"}
    assert leer(999) is None
    columnas = {c[1] for c in mundo.con.execute("PRAGMA table_info(facturas)")}
    assert not columnas & {"subtotal", "itbis", "total", "pagada", "pagado"}                 # los totales no se guardan


def test_la_lista_va_de_la_mas_nueva_a_la_mas_vieja_con_codigo_ligas_y_totales(mundo):
    a = emitir(crear(itbis_pct=18, proyecto_id=1, cliente_noco_id=11))
    b = crear(cliente_noco_id=22, proyecto_id=2, liga={"tipo": "sesion", "ref": "x"})
    b = correr(db.ligar_factura_a_registro(b["id"], CASA, "sesion", "y"))
    c = crear(cliente_noco_id=11, liga={"tipo": "sesion", "ref": "z"})
    lista = correr(db.facturas_para_la_lista())
    assert [x["id"] for x in lista] == [c["id"], b["id"], a["id"]]
    por_id = {x["id"]: x for x in lista}
    assert (por_id[a["id"]]["codigo"], por_id[a["id"]]["total"], por_id[a["id"]]["ligas_vivas"]) == \
        ("FAC-2026-001", Decimal("4130.59"), 1)
    assert (por_id[b["id"]]["codigo"], por_id[b["id"]]["ligas_vivas"], por_id[b["id"]]["itbis"]) == (None, 2, Decimal("0.00"))
    assert "renglones" not in lista[0]
    assert [x["id"] for x in correr(db.facturas_para_la_lista(estado="emitida"))] == [a["id"]]
    assert [x["id"] for x in correr(db.facturas_para_la_lista(proyecto_id=2))] == [b["id"]]
    assert [x["id"] for x in correr(db.facturas_para_la_lista(cliente_noco_id=11))] == [c["id"], a["id"]]
    assert correr(db.facturas_para_la_lista(estado="anulada")) == []


def test_la_lista_y_la_lectura_cuadran_con_los_totales_de_una_sola_cuenta(mundo):
    e = emitir(crear(itbis_pct=18, renglones=[{"descripcion": "a", "monto": "10.25"}]))
    en_lista = correr(db.facturas_para_la_lista())[0]
    for k in ("subtotal", "itbis", "total"):
        assert en_lista[k] == e[k]
    assert e["itbis"] == Decimal("1.85")


def test_sin_las_tablas_cada_funcion_dice_que_falta_la_migracion(mundo):
    for t in ("factura_envios", "factura_ligas", "factura_renglones", "facturas"):
        mundo.con.execute(f"DROP TABLE {t}")
    with pytest.raises(db.FacturasSinTabla):
        correr(db.factura_por_id(1))
    with pytest.raises(db.FacturasSinTabla):
        correr(db.facturas_para_la_lista())
    with pytest.raises(db.FacturasSinTabla):
        crear()
    with pytest.raises(db.FacturasSinTabla):
        correr(db.emitir_factura(1, CASA, "2026-10-21 12:00:00+00:00"))


def test_otro_error_de_la_base_se_propaga_tal_cual(mundo, monkeypatch):
    class _Roto(Exception):
        sqlstate = "XX000"
    original = _Cur.execute

    async def roto(self, sql, params=None):
        if sql == db.SENTENCIA_DE_LEER_FACTURA:
            raise _Roto("falla")
        return await original(self, sql, params)
    monkeypatch.setattr(_Cur, "execute", roto)
    with pytest.raises(_Roto):
        correr(db.factura_por_id(1))


def test_el_choque_del_ncf_en_postgres_se_traduce(mundo, monkeypatch):
    """Si otro escribe el mismo NCF entre la revisión y el INSERT, Postgres dice 23505 de `facturas_ncf_unico`."""
    class _Choque(Exception):
        sqlstate = "23505"

        class diag:
            constraint_name = "facturas_ncf_unico"
    original = _Cur.execute

    async def choca(self, sql, params=None):
        if sql == db.SENTENCIA_DE_CREAR_FACTURA:
            raise _Choque("duplicado")
        return await original(self, sql, params)
    monkeypatch.setattr(_Cur, "execute", choca)
    no_vale("ncf_repetido", lambda: crear(ncf="B01", ncf_tipo="B01"))
    assert filas(mundo, "facturas") == []


# ── Descartar un borrador ───────────────────────────────────────────────────────────────────────────

def test_descartar_un_borrador_lo_borra_con_sus_renglones_y_ligas_y_libera_la_sesion(mundo):
    f = crear(ncf="B09", ncf_tipo="B01", liga={"tipo": "sesion", "ref": "ses-d"})
    f = correr(db.ligar_factura_a_registro(f["id"], CASA, "trabajo", "tr-d"))
    r = correr(db.descartar_borrador_de_factura(f["id"], CASA))
    assert r == {"id": f["id"], "descartada": True}
    for t in TABLAS_QUE_ESCRIBE_LA_PUERTA:
        assert filas(mundo, t) == [], t
    assert leer(f) is None and correr(db.facturas_para_la_lista()) == []
    assert filas(mundo, "serie_facturas") == []                       # sin hueco: nunca tuvo número
    h = filas(mundo, "log_acciones")[-1]
    assert (h["accion"], h["tabla"], h["registro_id"], h["despues"]) == ("descartar", "facturas", f["id"], None)
    assert json.loads(h["antes"])["ncf"] == "B09" and len(json.loads(h["antes"])["ligas"]) == 2
    nueva = crear(ncf="B09", ncf_tipo="B01", liga={"tipo": "sesion", "ref": "ses-d"})      # sesión y NCF libres
    assert nueva["estado"] == "borrador"


def test_solo_un_borrador_sin_envios_se_descarta(mundo):
    b = crear()
    mundo.con.execute("INSERT INTO factura_envios (factura_id, canal) VALUES (?, 'mano')", (b["id"],))
    mundo.con.commit()
    antes = foto(mundo)
    no_vale("tiene_envios", lambda: correr(db.descartar_borrador_de_factura(b["id"], CASA)))
    assert foto(mundo) == antes
    e = emitir(otra())
    with pytest.raises(db.FacturaYaNoSeCorrige) as err:
        correr(db.descartar_borrador_de_factura(e["id"], CASA))
    assert err.value.estado == "emitida"
    a = correr(db.anular_factura(e["id"], CASA, "motivo"))
    with pytest.raises(db.FacturaYaNoSeCorrige):
        correr(db.descartar_borrador_de_factura(a["id"], CASA))
    with pytest.raises(db.FacturaNoEsta):
        correr(db.descartar_borrador_de_factura(999, CASA))
    assert leer(e)["estado"] == "anulada" and len(filas(mundo, "factura_ligas")) >= 1


def test_descartar_borra_solo_la_suya(mundo):
    a, b = otra(), otra()
    correr(db.descartar_borrador_de_factura(a["id"], CASA))
    assert leer(b)["renglones"] and leer(b)["ligas"] and len(filas(mundo, "facturas")) == 1


# ── 8. Una sola puerta ──────────────────────────────────────────────────────────────────────────────

_ESCRIBE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+(facturas|factura_renglones|factura_ligas|factura_envios)\b", re.I)


def _sentencias_sobre_facturas(archivos, raiz) -> list[tuple[str, int, str]]:
    """(archivo, línea, texto) de toda constante de texto (o suma de ellas) de un `.py` fuera de `tests/` que
    INSERTA, ACTUALIZA o BORRA en las cuatro tablas. Misma regla y misma frontera que la de la serie
    (`num._textos_que_escriben_la_serie`): no ve el nombre de la tabla en una variable, f-strings ni SQL armado
    en ejecución."""
    hallados = []
    for ruta in archivos:
        rel = ruta.relative_to(raiz).as_posix()
        if rel.startswith("tests/") or ruta.name == "conftest.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(arbol)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        absorbidos: set[int] = set()
        for nodo in ast.walk(arbol):
            if id(nodo) in absorbidos or id(nodo) in docstrings:
                continue
            v = num._texto_fijo(nodo)
            if v is not None:
                absorbidos.update(id(h) for h in ast.walk(nodo))
                if _ESCRIBE.search(v):
                    hallados.append((rel, nodo.lineno, v))
    return hallados


def _lo_que_viola(hallados):
    """Reglas: solo `db/db.py` escribe; nadie borra salvo los renglones de un borrador (y, con él, sus envíos que ya
    terminaron mal); las ligas solo se marcan `desligada_en`; un envío solo nace de dos formas (Natalia lo pide `pendiente`,
    la casa lo marca `mano` ya enviado), solo cambia las columnas de su vida, TODO UPDATE suyo dice el estado en su WHERE y
    solo dos sentencias pueden ponerlo `enviado` (la que anota Natalia y la de «a mano»: nada dice «enviado» sin que alguien
    lo anote; pieza 9); todo UPDATE de `facturas`
    (menos el que solo toma la fila) dice en su WHERE en qué estado vale y nunca toca `hecha_por*`, `creado_en`
    ni `id`; `anio`/`numero` solo los pone el que emite; el NCF solo lo toca el que corrige un borrador."""
    malos = []
    for rel, linea, v in hallados:
        if rel != "db/db.py":
            malos.append((rel, linea, "escribe fuera de db/db.py"))
        tabla = _ESCRIBE.search(v).group(2).lower()
        if re.search(r"\bREPLACE\s+INTO\b", v, re.I):
            malos.append((rel, linea, "reemplaza"))
        if re.search(r"\bDELETE\s+FROM\b", v, re.I):
            if "'borrador'" not in v and not (tabla == "factura_renglones" and v == db.SENTENCIA_DE_BORRAR_RENGLONES):
                malos.append((rel, linea, f"borra de {tabla} sin decir borrador"))
        if tabla == "factura_renglones" and re.search(r"\bUPDATE\b", v, re.I):
            malos.append((rel, linea, "cambia un renglón en su sitio"))
        if tabla == "factura_envios" and re.search(r"\bINSERT\b", v, re.I) and v not in (
                db.SENTENCIA_DE_PEDIR_ENVIO, db.SENTENCIA_DE_MANDADA_A_MANO):
            malos.append((rel, linea, "crea un envío de una forma que no es de las dos"))
        if tabla == "factura_envios" and re.search(r"'enviado'", v) and v not in (
                db.SENTENCIA_DE_ANOTAR_QUE_SALIO, db.SENTENCIA_DE_MANDADA_A_MANO):
            malos.append((rel, linea, "pone enviado fuera de las dos que lo anotan"))
        m = re.search(r"\bUPDATE\s+(\w+)\s+SET\b(.*?)\bWHERE\b(.*)", v, re.I | re.S)
        if re.search(r"\bUPDATE\b", v, re.I) and m is None:
            malos.append((rel, linea, "UPDATE sin WHERE"))
            continue
        if m is None:
            continue
        puesto, donde = m.group(2), m.group(3)
        columnas = set(re.findall(r"\b(\w+)\s*=", puesto))
        if tabla == "factura_ligas" and columnas != {"desligada_en"}:
            malos.append((rel, linea, "cambia en una liga algo que no es desligada_en"))
        if tabla == "factura_envios":
            if not columnas <= {"estado", "motivo", "aprobado_por_chat_id", "aprobado_en", "enviado_en", "tarjeta_msg_id",
                                "visto_cambiado_en"}:
                malos.append((rel, linea, "cambia en un envío una columna que no es de su vida"))
            if not re.search(r"\bestado\b", donde):
                malos.append((rel, linea, "UPDATE de envíos sin el estado en el WHERE"))
        if tabla == "facturas":
            if puesto.strip() == "creado_en = creado_en":
                continue                                                   # tomar la fila: no cambia nada
            if not re.search(r"\bestado\b", donde):
                malos.append((rel, linea, "UPDATE de facturas sin el estado en el WHERE"))
            for col in ("hecha_por", "hecha_por_chat_id", "creado_en", "id"):
                if col in columnas:
                    malos.append((rel, linea, f"cambia {col}"))
            if ({"anio", "numero"} & columnas) and "emitida_en" not in columnas:
                malos.append((rel, linea, "pone el número fuera de emitir"))
            if "ncf" in columnas and "'borrador'" not in donde:
                malos.append((rel, linea, "toca el NCF fuera de un borrador"))
    return malos


def test_las_sentencias_que_escriben_facturas_son_las_de_la_puerta_y_cumplen_las_reglas():
    hallados = _sentencias_sobre_facturas(buzon._py_en_disco(_ROOT), _ROOT)
    textos = sorted(h[2] for h in hallados)
    esperadas = sorted((
        db.SENTENCIA_DE_TOMAR_FACTURA, db.SENTENCIA_DE_TOMAR_FACTURA_CON_CANDADO, db.SENTENCIA_DE_CREAR_FACTURA,
        db.SENTENCIA_DE_CREAR_RENGLON, db.SENTENCIA_DE_BORRAR_RENGLONES, db.SENTENCIA_DE_CREAR_LIGA,
        db.SENTENCIA_DE_DESLIGAR, db.SENTENCIA_DE_CORREGIR_BORRADOR, db.SENTENCIA_DE_PONER_PROYECTO,
        db.SENTENCIA_DE_PONER_COTIZACION, db.SENTENCIA_DE_TOCAR_BORRADOR, db.SENTENCIA_DE_EMITIR,
        db.SENTENCIA_DE_ANULAR, db.SENTENCIA_DE_SOLTAR_ENVIOS, db.SENTENCIA_DE_DESCARTAR_LIGAS,
        db.SENTENCIA_DE_DESCARTAR_RENGLONES, db.SENTENCIA_DE_DESCARTAR_FACTURA,
        # pieza 8 (Drive): anotar dónde quedó el PDF y el error del intento; no piden sesión ni dejan huella (como los de
        # las cotizaciones: los llama una ruta que ya la pidió) y todas dicen el estado en su WHERE
        db.SENTENCIA_DE_MARCAR_FACTURA_EN_DRIVE, db.SENTENCIA_DE_MARCAR_ANULADA_EN_DRIVE,
        db.SENTENCIA_DE_ERROR_DE_DRIVE_DE_FACTURA,
        # pieza 9 (la puerta de Natalia): la vida de un envío, y borrar con un borrador los que ya terminaron mal
        db.SENTENCIA_DE_PEDIR_ENVIO, db.SENTENCIA_DE_APROBAR_ENVIO, db.SENTENCIA_DE_RECHAZAR_ENVIO,
        db.SENTENCIA_DE_ENVIO_QUE_NO_SE_PUDO_APROBAR, db.SENTENCIA_DE_ANOTAR_QUE_SALIO,
        db.SENTENCIA_DE_ANOTAR_QUE_NO_SALIO, db.SENTENCIA_DE_MANDADA_A_MANO, db.SENTENCIA_DE_MARCAR_LA_TARJETA,
        db.SENTENCIA_DE_DESCARTAR_ENVIOS))
    assert textos == esperadas, [h[:2] for h in hallados]
    assert _lo_que_viola(hallados) == []


def test_la_guarda_ve_lo_inventado(tmp_path):
    """Entradas inventadas: lo que la guarda tiene que rechazar, escrito de varias formas."""
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "db.py").write_text(
        'A = "UPDATE facturas SET numero = 1 WHERE id = 1 AND estado = \'borrador\'"\n'
        'B = "UPDATE facturas SET " + "ncf = \'X\' WHERE id = 1 AND estado = \'emitida\'"\n'
        'C = "UPDATE facturas SET para_nombre = %s WHERE id = %s"\n'
        'D = "DELETE FROM facturas WHERE id = 1"\n'
        'E = ("UPDATE facturas SET hecha_por = 1 "\n     "WHERE estado = %s")\n'
        'F = "UPDATE factura_ligas SET ref = 1 WHERE id = 1"\n'
        'G = "DELETE FROM factura_ligas WHERE id = 1"\n'
        'H = "UPDATE factura_renglones SET monto = 1 WHERE id = 1"\n'
        'I = "INSERT INTO factura_envios (factura_id) VALUES (1)"\n'
        'J = "UPDATE factura_envios SET aprobado_en = now() WHERE id = 1"\n'
        'N = "UPDATE factura_envios SET canal = \'mano\' WHERE id = 1 AND estado = \'pendiente\'"\n'
        'O = "UPDATE factura_envios SET estado = \'enviado\' WHERE id = 1 AND estado = \'pendiente\'"\n'
        'P = "DELETE FROM factura_envios WHERE id = 1"\n'
        'K = "UPDATE facturas SET estado = \'anulada\'"\n'
        'L = "REPLACE INTO facturas (id) VALUES (1)"\n', encoding="utf-8")
    (tmp_path / "otro.py").write_text('M = "INSERT INTO facturas (a) VALUES (1)"\n', encoding="utf-8")
    hallados = _sentencias_sobre_facturas([tmp_path / "db" / "db.py", tmp_path / "otro.py"], tmp_path)
    razones = sorted(set(r for _, _, r in _lo_que_viola(hallados)))
    assert razones == sorted([
        "pone el número fuera de emitir", "toca el NCF fuera de un borrador", "UPDATE de facturas sin el estado en el WHERE",
        "borra de facturas sin decir borrador", "cambia hecha_por", "cambia en una liga algo que no es desligada_en",
        "borra de factura_ligas sin decir borrador",
        "cambia un renglón en su sitio", "crea un envío de una forma que no es de las dos",
        "cambia en un envío una columna que no es de su vida", "UPDATE de envíos sin el estado en el WHERE",
        "pone enviado fuera de las dos que lo anotan", "borra de factura_envios sin decir borrador",
        "UPDATE sin WHERE", "reemplaza", "escribe fuera de db/db.py"])


def test_la_frontera_de_la_guarda_no_lo_ve_a_proposito(tmp_path):
    (tmp_path / "f.py").write_text(
        'T = "facturas"\nA = "UPDATE " + T + " SET numero = 1"\nB = f"DELETE FROM {T}"\n', encoding="utf-8")
    assert _sentencias_sobre_facturas([tmp_path / "f.py"], tmp_path) == []


def _escritores_publicos() -> dict:
    """Las funciones públicas de `db.db` que reciben `chat_id` y hablan de facturas, SACADAS DEL MÓDULO."""
    return {n: f for n, f in vars(db).items()
            if inspect.iscoroutinefunction(f) and not n.startswith("_") and "factura" in n
            and "chat_id" in inspect.signature(f).parameters and f.__module__ == db.__name__}


# Cómo llamar a cada escritor con LO MÍNIMO que vale. Si aparece un escritor nuevo, la prueba de abajo se pone
# roja hasta que se agregue aquí.
def _llamadas(f, chat):
    return {
        "crear_borrador_de_factura": lambda: db.crear_borrador_de_factura(
            chat, cliente_noco_id=1, para_nombre="N", renglones=[{"descripcion": "a", "monto": "1"}],
            liga={"tipo": "sesion", "ref": "hermano-1"}),
        "corregir_borrador_de_factura": lambda: db.corregir_borrador_de_factura(f["id"], chat, f["cambiado_en"], {"para_nombre": "Z"}),
        "ligar_factura_a_registro": lambda: db.ligar_factura_a_registro(f["id"], chat, "trabajo", "hermano-2"),
        "desligar_factura_de_registro": lambda: db.desligar_factura_de_registro(f["id"], chat, "sesion", "hermano-0"),
        "poner_proyecto_de_factura": lambda: db.poner_proyecto_de_factura(f["id"], chat, 1),
        "poner_cotizacion_de_factura": lambda: db.poner_cotizacion_de_factura(f["id"], chat, 1),
        "emitir_factura": lambda: db.emitir_factura(f["id"], chat, f["cambiado_en"]),
        "anular_factura": lambda: db.anular_factura(f["id"], chat, "motivo"),
        "descartar_borrador_de_factura": lambda: db.descartar_borrador_de_factura(f["id"], chat),
        "marcar_factura_mandada_a_mano": lambda: db.marcar_factura_mandada_a_mano(f["id"], chat),
        "contestar_envio_de_factura": lambda: db.contestar_envio_de_factura(1, chat, True),
    }


def test_hay_los_escritores_que_la_prueba_conoce():
    assert sorted(_escritores_publicos()) == sorted(_llamadas({"id": 1, "cambiado_en": "x"}, CASA))


def test_cada_escritor_pide_sesion_de_la_casa_antes_de_abrir_la_conexion(monkeypatch):
    """Los hermanos: TODOS, sacados del módulo. Con un `pool` que revienta si alguien lo toca."""
    monkeypatch.setattr(db, "pool", _PoolQueNoSeToca())
    for nombre in _escritores_publicos():
        for extrano in (EXTRANO, None):
            with pytest.raises(db.FacturaSinSesion):
                correr(_llamadas({"id": 1, "cambiado_en": "2026-10-21 12:00:00+00:00"}, extrano)[nombre]())


def test_cada_escritor_deja_una_huella_y_ninguna_mas(mundo):
    base = correr(db.ligar_factura_a_registro(crear(liga={"tipo": "sesion", "ref": "hermano-0"})["id"], CASA, "sesion", "hermano-x"))
    e = emitir(crear(liga={"tipo": "sesion", "ref": "hermano-e"}))
    mundo.con.execute("DELETE FROM log_acciones")
    # el orden de una vida: se corrige, se liga, se emite, se anula (la emitida congela lo anterior)
    orden = ["corregir_borrador_de_factura", "ligar_factura_a_registro", "desligar_factura_de_registro",
             "poner_cotizacion_de_factura", "poner_proyecto_de_factura", "emitir_factura",
             "marcar_factura_mandada_a_mano", "anular_factura", "descartar_borrador_de_factura"]
    # `contestar_envio_de_factura` (pieza 9) necesita a quien revisa y una tarjeta enviada: su huella (una al rechazar, dos al
    # aprobar un borrador, que también lo emite) la vigila `tests/test_facturas_natalia.py`.
    assert sorted(orden + ["crear_borrador_de_factura", "contestar_envio_de_factura"]) == sorted(_escritores_publicos())
    for nombre in ["crear_borrador_de_factura"] + orden:
        sujeto = e if nombre in ("anular_factura", "marcar_factura_mandada_a_mano") else leer(base)
        if nombre == "descartar_borrador_de_factura":
            sujeto = crear(liga={"tipo": "sesion", "ref": "hermano-d"})
            mundo.con.execute("DELETE FROM log_acciones WHERE registro_id = ?", (sujeto["id"],))           # el candado se vuelve a leer en cada intento
        antes = len(filas(mundo, "log_acciones"))
        correr(_llamadas(sujeto, CASA)[nombre]())
        nuevas = filas(mundo, "log_acciones")[antes:]
        assert [(h["actor"], h["tabla"]) for h in nuevas] == [("panel", "facturas")], nombre


# ── 9. Lo que la puerta repite del esquema ──────────────────────────────────────────────────────────

def _columnas_reales(mundo) -> list[str]:
    return [c[1] for c in mundo.con.execute("PRAGMA table_info(facturas)")]


def test_el_select_de_la_factura_lee_todas_las_columnas_de_la_tabla_y_ninguna_mas(mundo):
    pedidas = re.search(r"SELECT (.*) FROM facturas", db.SENTENCIA_DE_LEER_FACTURA).group(1).split(", ")
    # Las columnas de Drive (pieza 8) las lee APARTE `db.drive_de_factura`, que tolera que falten: la factura entera se lee
    # igual con la migración de Drive sin aplicar. Tiene que pedir TODAS las que hay de Drive.
    drive = [c for c in _columnas_reales(mundo) if c.startswith("drive_")]
    leidas_por_drive = re.search(r"SELECT (.*) FROM facturas", db.SENTENCIA_DE_LEER_EL_DRIVE_DE_FACTURA).group(1).split(", ")
    assert drive and set(drive) <= set(leidas_por_drive) and not set(drive) & set(pedidas)
    assert sorted(pedidas) == sorted(c for c in _columnas_reales(mundo) if c not in drive)


def test_cada_columna_de_facturas_esta_decidida_corregible_o_manejada_por_otra_funcion(mundo):
    """Cubo estricto: una columna nueva de la tabla se pone roja hasta que alguien decida quién la escribe."""
    corregibles = set(db.CAMPOS_QUE_CORRIGE_UN_BORRADOR) - {"renglones"}
    de_otra_funcion = {
        "id", "anio", "numero", "estado", "emitida_en",                                   # emitir
        "anulada_en", "anulada_por_chat_id", "motivo_anulacion",                           # anular
        "proyecto_id", "cotizacion_id", "cotizacion_opcion",                               # poner_proyecto / poner_cotizacion
        "hecha_por", "hecha_por_chat_id", "creado_en", "cambiado_en",                      # crear / todas
        "drive_archivo_id", "drive_anulada_archivo_id", "drive_proyecto_id", "drive_error"}   # Drive (pieza 8)
    assert corregibles <= set(_columnas_reales(mundo))
    assert set(_columnas_reales(mundo)) == corregibles | de_otra_funcion
    assert not corregibles & de_otra_funcion


def test_el_insert_de_crear_nombra_columnas_que_existen_y_las_que_no_nombra_las_pone_la_base(mundo):
    cols = re.search(r"INSERT INTO facturas \((.*?)\)", db.SENTENCIA_DE_CREAR_FACTURA).group(1).split(", ")
    assert set(cols) <= set(_columnas_reales(mundo))
    obligatorias_sin_defecto = {c[1] for c in mundo.con.execute("PRAGMA table_info(facturas)")
                                if c[3] and c[4] is None and not c[5]}
    assert obligatorias_sin_defecto <= set(cols)


def test_los_vocabularios_y_largos_de_la_puerta_son_los_de_la_base():
    esquema = (_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    tipos = re.search(r"factura_ligas_tipo_valido\s+CHECK \(tipo IN \(([^)]*)\)\)", esquema).group(1)
    assert tuple(re.findall(r"'(\w+)'", tipos)) == db.TIPOS_DE_LIGA_DE_FACTURA
    cuerpo = esquema[esquema.index("CREATE TABLE facturas"):esquema.index("CREATE TABLE serie_facturas")]
    def entre(patron):
        return int(re.search(patron + r"\s+BETWEEN 1 AND (\d+)", cuerpo).group(1))
    assert entre(r"length\(trim\(para_nombre\)\)") == db.LARGOS_DE_FACTURA["para_nombre"]
    assert entre(r"length\(trim\(descripcion\)\)") == db.LARGOS_DE_FACTURA["descripcion"]
    assert entre(r"length\(trim\(ncf_tipo\)\)") == db.LARGOS_DE_FACTURA["ncf_tipo"]
    assert entre(r"length\(ncf\)") == db.LARGOS_DE_FACTURA["ncf"]
    assert entre(r"length\(trim\(motivo_anulacion\)\)") == db.LARGOS_DE_FACTURA["motivo"]
    assert entre(r"length\(trim\(motivo\)\)") >= db.LARGOS_DE_FACTURA["motivo"]       # el del envío
    assert int(re.search(r"detalle IS NULL OR length\(detalle\) <= (\d+)", cuerpo).group(1)) == db.LARGOS_DE_FACTURA["detalle"]
    assert int(re.search(r"length\(ref\) BETWEEN 1 AND (\d+)", cuerpo).group(1)) == db.LARGOS_DE_FACTURA["ref"]
    assert int(re.search(r"codigo IS NULL OR length\(codigo\) <= (\d+)", cuerpo).group(1)) == db.LARGOS_DE_FACTURA["codigo"]


def test_lo_que_la_puerta_acepta_en_el_limite_lo_acepta_la_base_y_un_caracter_mas_ya_no(mundo):
    f = crear(para_nombre="n" * 200, renglones=[{"descripcion": "d" * 300, "detalle": "t" * 2000, "monto": "99999999.99"}],
              liga={"tipo": "sesion", "ref": "r" * 100, "codigo": "c" * 100}, ncf="N" * 20, ncf_tipo="T" * 20)
    e = emitir(f)
    assert len(correr(db.anular_factura(e["id"], CASA, "m" * 500))["motivo_anulacion"]) == 500
    ids = [r["id"] for r in filas(mundo, "facturas")]
    with pytest.raises(sqlite3.IntegrityError):
        mundo.con.execute("INSERT INTO facturas (cliente_noco_id, para_nombre, fecha, hecha_por, hecha_por_chat_id) "
                          "VALUES (1, ?, '2026-10-21', 'casa', 1)", ("n" * 201,))
    with pytest.raises(sqlite3.IntegrityError):
        mundo.con.execute("INSERT INTO factura_renglones (factura_id, orden, descripcion, monto) VALUES (?, 9, ?, 1)", (ids[0], "d" * 301))
    with pytest.raises(sqlite3.IntegrityError):
        mundo.con.execute("INSERT INTO factura_ligas (factura_id, tipo, ref) VALUES (?, 'sesion', ?)", (ids[0], "r" * 101))


def test_el_tope_de_renglones_por_el_tope_de_cada_uno_cabe_en_la_columna_con_su_itbis():
    peor = db.TOPE_TOTAL_DE_RENGLON * db.LIMITE_DE_RENGLONES_DE_FACTURA
    total = db.totales_de_factura([peor], db.ITBIS_DE_FACTURA)["total"]
    assert total < Decimal(10) ** (14 - 2)          # NUMERIC(14,2): doce cifras enteras


def test_humo_corre_las_dos_lecturas_de_las_facturas_contra_la_base_de_verdad():
    arbol = ast.parse((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"))
    llamadas = {n.func.attr for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {"factura_por_id", "facturas_para_la_lista"} <= llamadas
