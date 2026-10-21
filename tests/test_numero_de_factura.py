"""El número de una factura (pieza 1 del diseño «Facturas», 20-oct-2026): `FAC-AAAA-NNN`.

Es el mismo camino que el de las cotizaciones (`tests/test_numero_de_cotizacion.py`): la sentencia REAL de
`db.siguiente_numero_de_factura` EJECUTADA en SQLite (>= 3.35 entiende `INSERT ... ON CONFLICT ... DO UPDATE ...
RETURNING`) sobre las tablas que crea la migración real. Qué se vigila:

  1. la primera del año es 1 (la serie nace vacía); dos seguidas dan N y N+1, sin saltos;
  2. cada año lleva su cuenta;
  3. NO REPITE NI SALTA bajo concurrencia: (a) dos conexiones intercaladas; (b) varios hilos, cada uno con su
     conexión a un archivo, numerando y guardando una factura en la misma transacción: los números que salen son
     exactamente 1..N; (c) una transacción que se deshace devuelve su número: el que sigue lo recibe;
  4. `UNIQUE (anio, numero)` frena un repetido aunque el contador falle;
  5. el formato `FAC-AAAA-NNN`;
  6. un año que no es año no llega a la base;
  7. una sola puerta, hasta donde llega una guarda de TEXTO: ningún `.py` fuera de `tests/` trae una constante de
     texto con `serie_facturas` e INSERT/UPDATE/DELETE/REPLACE salvo la sentencia de `db`. NO ve el nombre de la
     tabla en una variable, ni f-strings, ni SQL armado en ejecución (frontera declarada en
     `tests/test_numero_de_cotizacion.py::test_la_frontera_de_la_guarda_no_lo_ve_a_proposito`).

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. SQLite bloquea la base ENTERA mientras una conexión escribe; Postgres
    bloquea solo la fila del año y el segundo ESPERA al `COMMIT` del primero. Aquí el segundo recibe
    «database is locked» y las pruebas reintentan: lo que se prueba es que NO salen números repetidos ni
    saltados. La espera real de Postgres no se ejerce; la del `ON CONFLICT DO UPDATE` sobre la misma fila la
    cubre el ensayo de la sala (`tools/ensayo_facturas.py`).
  · `psycopg` no se ejerce: el cursor es un doble mínimo (`%s` → `?`, filas como dict) que NO cambia la
    sentencia que le llega; la prueba compara el texto recibido con `db.SENTENCIA_DEL_NUMERO_DE_FACTURA`.

Correr:  python3 -m pytest tests/test_numero_de_factura.py -q
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import threading

import pytest

import test_buzon_que_no_se_ve as buzon
import test_cotizaciones_tablas as ct
import test_facturas_tablas as tf
import test_numero_de_cotizacion as nc
from test_grupo_ia import _ROOT

import db.db as db


def _numero(con, anio):
    return asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(con), anio))


def _factura_numerada(con, anio, numero):
    con.execute("INSERT INTO facturas (cliente_noco_id, para_nombre, fecha, hecha_por, hecha_por_chat_id, "
                "estado, anio, numero, emitida_en) VALUES (1, 'Cliente de prueba', '2026-10-20', 'casa', 7, "
                "'emitida', ?, ?, '2026-10-20T12:00:00+00:00')", (anio, numero))


# ── 1. La primera es 1 y las siguientes suben de uno en uno ─────────────────────────────────────────

def test_la_primera_del_anio_es_1_sobre_la_serie_vacia():
    con = tf._base()
    assert con.execute("SELECT count(*) FROM serie_facturas").fetchone()[0] == 0
    assert _numero(con, 2026) == 1
    assert con.execute("SELECT anio, ultimo FROM serie_facturas").fetchall() == [(2026, 1)]


def test_dos_seguidas_dan_n_y_n_mas_1_y_siguen_subiendo_sin_saltos():
    con = tf._base()
    assert [_numero(con, 2026) for _ in range(5)] == [1, 2, 3, 4, 5]
    assert con.execute("SELECT ultimo FROM serie_facturas WHERE anio = 2026").fetchone()[0] == 5


def test_el_contador_escribe_solo_su_fila_y_no_toca_ni_las_facturas_ni_la_serie_de_cotizaciones():
    con = tf._base()
    _numero(con, 2026)
    assert con.execute("SELECT count(*) FROM facturas").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM serie_cotizaciones").fetchone()[0] == 0


def test_a_la_base_llega_exactamente_la_sentencia_del_diseno_con_el_anio_como_parametro():
    con = tf._base()
    cur = nc._Cursor(con)
    asyncio.run(db.siguiente_numero_de_factura(cur, 2026))
    assert cur.recibidas == [(db.SENTENCIA_DEL_NUMERO_DE_FACTURA, (2026,))]
    plana = " ".join(db.SENTENCIA_DEL_NUMERO_DE_FACTURA.split())
    assert plana == ("INSERT INTO serie_facturas (anio, ultimo) VALUES (%s, 1) ON CONFLICT (anio) "
                     "DO UPDATE SET ultimo = serie_facturas.ultimo + 1 RETURNING ultimo")


# ── 2. Cada año lleva su cuenta ─────────────────────────────────────────────────────────────────────

def test_el_anio_nuevo_empieza_en_1_y_no_toca_la_cuenta_del_anterior():
    con = tf._base()
    assert [_numero(con, 2026) for _ in range(3)] == [1, 2, 3]
    assert _numero(con, 2027) == 1
    assert _numero(con, 2026) == 4
    assert _numero(con, 2027) == 2
    assert con.execute("SELECT anio, ultimo FROM serie_facturas ORDER BY anio").fetchall() == [(2026, 4), (2027, 2)]


# ── 3. Ni repite ni salta bajo concurrencia ─────────────────────────────────────────────────────────

def _conexion_a_archivo(ruta: str) -> sqlite3.Connection:
    con = sqlite3.connect(ruta, timeout=0, isolation_level=None, check_same_thread=False)
    con.create_function("now", 0, lambda: "2026-10-20T12:00:00+00:00")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _crear_tablas_en_archivo(ruta: str) -> None:
    con = _conexion_a_archivo(ruta)
    con.execute("CREATE TABLE IF NOT EXISTS proyectos (id INTEGER PRIMARY KEY)")
    con.execute("INSERT OR IGNORE INTO proyectos (id) VALUES (1)")
    for texto in (ct._texto_migracion(), tf._texto_migracion()):
        for s in tf._sentencias_que_corren(texto):
            con.execute(tf._a_sqlite(s))
    con.close()


def test_dos_conexiones_intercaladas_no_sacan_el_mismo_numero(tmp_path):
    """A saca su número y NO ha cerrado; B pide el suyo en medio. B no puede recibir el de A. Cuando A guarda su
    factura y cierra, B recibe el siguiente. (Una sentencia `max(numero)+1` dejaría a los dos con el 1.)"""
    ruta = str(tmp_path / "dos_conexiones.db")
    _crear_tablas_en_archivo(ruta)
    a, b = _conexion_a_archivo(ruta), _conexion_a_archivo(ruta)
    a.execute("BEGIN")
    n_a = asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(a), 2026))
    assert n_a == 1
    b.execute("BEGIN")
    try:
        n_b = asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(b), 2026))
    except sqlite3.OperationalError as e:
        assert "locked" in str(e)
        n_b = None
    assert n_b != n_a, "las dos conexiones sacaron el mismo número"
    if n_b is None:
        b.execute("ROLLBACK")
    _factura_numerada(a, 2026, n_a)
    a.execute("COMMIT")
    if n_b is None:
        b.execute("BEGIN")
        n_b = asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(b), 2026))
    assert n_b == n_a + 1
    _factura_numerada(b, 2026, n_b)
    b.execute("COMMIT")
    assert sorted(r[0] for r in a.execute("SELECT numero FROM facturas")) == [1, 2]
    a.close()
    b.close()


def test_varios_hilos_numerando_y_guardando_a_la_vez_sacan_exactamente_1_a_n(tmp_path):
    """8 hilos x 6 facturas, cada hilo con SU conexión al mismo archivo: numera y guarda la factura en la misma
    transacción (como hará la pieza 2) y reintenta si la base está ocupada. Al final los números guardados son
    1..48, sin repetir ni saltar, y el contador dice 48."""
    ruta = str(tmp_path / "hilos.db")
    _crear_tablas_en_archivo(ruta)
    hilos, por_hilo = 8, 6
    errores: list[BaseException] = []

    def trabajar():
        try:
            con = _conexion_a_archivo(ruta)
            for _ in range(por_hilo):
                for _intento in range(20000):
                    try:
                        con.execute("BEGIN")
                        n = asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(con), 2026))
                        _factura_numerada(con, 2026, n)
                        con.execute("COMMIT")
                        break
                    except sqlite3.OperationalError as e:
                        assert "locked" in str(e) or "busy" in str(e), e
                        if con.in_transaction:
                            con.execute("ROLLBACK")
                else:
                    raise AssertionError("no consiguió turno")
            con.close()
        except BaseException as e:           # noqa: BLE001 - se junta y se reporta en el hilo principal
            errores.append(e)

    ts = [threading.Thread(target=trabajar) for _ in range(hilos)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=120)
    assert not errores, errores
    con = _conexion_a_archivo(ruta)
    guardados = sorted(r[0] for r in con.execute("SELECT numero FROM facturas WHERE anio = 2026"))
    assert guardados == list(range(1, hilos * por_hilo + 1))
    assert con.execute("SELECT ultimo FROM serie_facturas WHERE anio = 2026").fetchone()[0] == hilos * por_hilo


def test_una_transaccion_que_se_deshace_devuelve_su_numero_y_no_deja_hueco(tmp_path):
    ruta = str(tmp_path / "deshacer.db")
    _crear_tablas_en_archivo(ruta)
    con = _conexion_a_archivo(ruta)
    con.execute("BEGIN")
    assert asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(con), 2026)) == 1
    con.execute("ROLLBACK")                                           # la factura no se pudo guardar
    assert con.execute("SELECT count(*) FROM serie_facturas").fetchone()[0] == 0
    con.execute("BEGIN")
    n = asyncio.run(db.siguiente_numero_de_factura(nc._Cursor(con), 2026))
    assert n == 1, "el número de una transacción deshecha se perdió"
    _factura_numerada(con, 2026, n)
    con.execute("COMMIT")
    con.close()


# ── 4. El UNIQUE es la última puerta ────────────────────────────────────────────────────────────────

def test_el_unique_frena_un_repetido_aunque_el_contador_falle():
    con = tf._base()
    n = _numero(con, 2026)
    _factura_numerada(con, 2026, n)
    con.execute("UPDATE serie_facturas SET ultimo = 0 WHERE anio = 2026")     # el contador "falla"
    repetido = _numero(con, 2026)
    assert repetido == n
    with pytest.raises(sqlite3.IntegrityError):
        _factura_numerada(con, 2026, repetido)
    _factura_numerada(con, 2027, 1)                                           # en otro año el mismo número vale


# ── 5. El formato ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("anio,numero,esperado", [
    (2026, 1, "FAC-2026-001"), (2026, 42, "FAC-2026-042"), (2026, 999, "FAC-2026-999"),
    (2026, 1000, "FAC-2026-1000"), (2027, 1, "FAC-2027-001"),
])
def test_el_codigo_es_fac_anio_tres_cifras(anio, numero, esperado):
    assert db.codigo_de_factura(anio, numero) == esperado


def test_la_primera_que_haga_lucy_es_la_fac_2026_001():
    assert db.codigo_de_factura(2026, _numero(tf._base(), 2026)) == "FAC-2026-001"


@pytest.mark.parametrize("anio,numero", [(2026, 0), (2026, -1), (2026, True), (2026, "1"), (2026, 1.0), (2026, None),
                                         (1999, 1), (10000, 1), ("2026", 1), (True, 1), (None, 1)])
def test_el_codigo_rechaza_lo_que_no_es_un_numero_o_un_anio(anio, numero):
    with pytest.raises(ValueError):
        db.codigo_de_factura(anio, numero)


# ── 6. Un año que no es año no llega a la base ──────────────────────────────────────────────────────

@pytest.mark.parametrize("anio", [None, "2026", 2026.0, True, 1999, 10000, 0, -2026])
def test_un_anio_que_no_es_anio_no_toca_la_base(anio):
    con = tf._base()
    cur = nc._Cursor(con)
    with pytest.raises(ValueError):
        asyncio.run(db.siguiente_numero_de_factura(cur, anio))
    assert cur.recibidas == []
    assert con.execute("SELECT count(*) FROM serie_facturas").fetchone()[0] == 0


# ── 7. Una sola puerta ──────────────────────────────────────────────────────────────────────────────

def test_ningun_texto_literal_fuera_de_tests_escribe_la_serie_de_facturas_salvo_el_del_contador():
    hallados = nc._textos_que_escriben_la_serie(buzon._py_en_disco(_ROOT), _ROOT, "serie_facturas")
    assert [(r, v) for r, _, v in hallados] == [("db/db.py", db.SENTENCIA_DEL_NUMERO_DE_FACTURA)], hallados


def test_la_busqueda_de_la_puerta_unica_ve_un_escritor_inventado_de_la_serie_de_facturas(tmp_path):
    (tmp_path / "otro.py").write_text(
        'SQL = ("UPDATE serie_facturas "\n       "SET ultimo = 99")\nLEE = "SELECT * FROM serie_facturas"\n'
        'OTRA = "UPDATE serie_cotizaciones SET ultimo = 1"\n', encoding="utf-8")
    hallados = nc._textos_que_escriben_la_serie([tmp_path / "otro.py"], tmp_path, "serie_facturas")
    assert [v for _, _, v in hallados] == ["UPDATE serie_facturas SET ultimo = 99"]


def test_la_serie_de_facturas_y_la_de_cotizaciones_son_dos_series():
    """Cada contador tiene su tabla y su puerta: una sentencia no nombra la serie de la otra."""
    assert "serie_cotizaciones" not in db.SENTENCIA_DEL_NUMERO_DE_FACTURA
    assert "serie_facturas" not in db.SENTENCIA_DEL_NUMERO_DE_COTIZACION
    assert re.search(r"\bserie_facturas\b", db.SENTENCIA_DEL_NUMERO_DE_FACTURA)
