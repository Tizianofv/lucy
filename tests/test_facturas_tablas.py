"""Las tablas de las facturas (pieza 1 del diseño «Facturas», 20-oct-2026): `facturas`, `factura_renglones`,
`factura_ligas`, `factura_envios` y `serie_facturas`, su migración, su clasificación, el guion de ensayo y la
línea del humo.

Qué se vigila, con el TEXTO REAL de `db/schema.sql` y de la migración, EJECUTADO en SQLite:

  1. la migración y el esquema declaran lo mismo (tablas, índices y restricciones con nombre incluidas);
  2. la migración corre entera (todas sus sentencias menos `BEGIN`/`COMMIT`/`COMMENT ON`, que SQLite no
     tiene), las cinco tablas nacen VACÍAS, y no hay ningún `INSERT`/`UPDATE`/`DELETE`/disparador en ella;
  3. cada restricción hace lo que dice, con filas inventadas: el número no se repite (y los borradores sin
     número no chocan entre sí), el NCF no se repite (anuladas incluidas) ni entra escrito de otra forma, los
     estados, quién la hizo, el motivo de anular, las ligas opcionales, los renglones, las ligas a sesión o
     trabajo (una viva por factura y referencia), los envíos (nada sale sin aprobar salvo a mano);
  4. no hay columnas de pago ni de totales (el pago se lee de la App; los totales salen de los renglones);
  5. los vocabularios cerrados de la base son los del diseño;
  6. las cinco tablas están clasificadas en `cerebro/consultar.py` como maquinaria con su motivo, y todas las
     restricciones con nombre y los índices de la migración entran a lo que el arranque compara con la base;
  7. el guion de ensayo (`tools/ensayo_facturas.py`) saca el `COMMIT` y solo ese, `--cuenta` solo lee,
     `--ensayo` cierra con `ROLLBACK`;
  8. el humo lee las cinco tablas y cuenta las facturas sin liga viva (el SQL de esa cuenta corre acá).

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. Las restricciones corren en SQLite con el texto de la migración; los dos
    cambios de dialecto son los de siempre (`BIGSERIAL` y `DEFAULT now()`). `COMMENT ON` no corre. Dos
    diferencias con Postgres que importan: SQLite no impone el largo ni los decimales de `NUMERIC(14,2)` (la
    puerta de la pieza 2 tiene que redondear a dos decimales) y `psycopg` no se ejerce. Que la llave foránea
    pida candado sobre `proyectos` y `cotizaciones` y que `psycopg` acepte el texto entero en una llamada es
    lo que cubre el ensayo contra la base real (`tools/ensayo_facturas.py`), que corre la sala.
  · Que toda factura tenga una liga viva NO la puede exigir la base (regla entre dos tablas): la exige la
    pieza 2 y la vigila el humo; aquí solo se prueba el SQL que la cuenta.
  · El guion solo ARMA SQL (no se conecta); se prueba el texto que arma.

Correr:  python3 -m pytest tests/test_facturas_tablas.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sqlite3

import pytest

import test_base_m2 as b2
import test_cotizaciones_tablas as ct
import test_grupo_ia as g
from test_grupo_ia import _ROOT

import db.db as db
from acciones import crud
from cerebro import consultar

TABLAS = ("facturas", "factura_renglones", "factura_ligas", "factura_envios", "serie_facturas")
_ESQUEMA = (_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
ESTADOS_DEL_DISENO = ("borrador", "emitida", "anulada")


def _migracion():
    suyas = [a for a in g._migraciones() if "CREATE TABLE IF NOT EXISTS facturas " in a.read_text(encoding="utf-8")]
    assert len(suyas) == 1, suyas
    return suyas[0]


def _texto_migracion() -> str:
    return _migracion().read_text(encoding="utf-8")


def _guion():
    spec = importlib.util.spec_from_file_location("ensayo_facturas", _ROOT / "tools" / "ensayo_facturas.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _a_sqlite(s: str) -> str:
    """El cambio de dialecto de siempre (`ct._a_sqlite`) y uno más: el `~` de Postgres no existe en SQLite, y
    `ncf ~ '^[A-Z0-9]+$'` se corre como `ncf NOT GLOB '*[^A-Z0-9]*'` (GLOB distingue mayúsculas y [^...] incluye
    el tabulador y el salto de línea). Lo que NO se ejerce aquí es el motor de expresiones regulares de Postgres."""
    return ct._a_sqlite(s).replace("ncf ~ '^[A-Z0-9]+$'", "ncf NOT GLOB '*[^A-Z0-9]*'")


def _sentencias_que_corren(texto: str) -> list[str]:
    return [s for s in g._sentencias(texto) if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I)]


def _base() -> sqlite3.Connection:
    """`proyectos` (1) y las cotizaciones (la migración real, una cotización inventada con id 1) y encima TODAS
    las sentencias de la migración de las facturas que SQLite entiende."""
    con = ct._base_con_migracion()
    ct._insertar(con)
    for s in _sentencias_que_corren(_texto_migracion()):
        con.execute(_a_sqlite(s))
    return con


def _poner(con, tabla, **campos):
    con.execute(f"INSERT INTO {tabla} ({', '.join(campos)}) VALUES ({', '.join('?' for _ in campos)})",
                tuple(campos.values()))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


_AHORA = "2026-10-20T12:00:00+00:00"


def _borrador(**otros) -> dict:
    base = {"cliente_noco_id": 1001, "para_nombre": "Cliente de prueba", "fecha": "2026-10-20",
            "hecha_por": "casa", "hecha_por_chat_id": 7}
    base.update(otros)
    return base


def _emitida(numero=1, **otros) -> dict:
    return _borrador(**{"estado": "emitida", "anio": 2026, "numero": numero, "emitida_en": _AHORA, **otros})


def _anulada(numero=1, **otros) -> dict:
    return _emitida(numero, **{"estado": "anulada", "anulada_en": _AHORA, "motivo_anulacion": "se hizo por error",
                               **otros})


def _factura(con, **otros) -> int:
    return _poner(con, "facturas", **_borrador(**otros))


def _rechaza(con, tabla, **campos):
    antes = con.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        _poner(con, tabla, **campos)
    assert con.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0] == antes


# ── 1. La migración y el esquema dicen lo mismo ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tabla", TABLAS)
def test_la_migracion_y_el_esquema_dicen_lo_mismo(tabla):
    de_la_migracion = [b2._normal(s).replace("if not exists ", "") for s in b2._ddl(_texto_migracion(), tabla)]
    del_esquema = [b2._normal(s) for s in b2._ddl(_ESQUEMA, tabla)]
    assert del_esquema, f"{tabla} no está en db/schema.sql"
    assert de_la_migracion == del_esquema


def test_las_tablas_de_la_migracion_son_las_cinco_y_nada_mas():
    assert set(_guion().tablas_de(_texto_migracion())) == set(TABLAS)
    assert len(_guion().tablas_de(_texto_migracion())) == len(TABLAS)


# ── 2. La migración corre y las tablas nacen vacías ─────────────────────────────────────────────────

def test_la_migracion_corre_las_cinco_tablas_nacen_vacias_y_la_segunda_vez_no_hace_nada():
    con = _base()
    for t in TABLAS:
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, f"{t} no nació vacía"
    for s in _sentencias_que_corren(_texto_migracion()):
        con.execute(_a_sqlite(s))
    assert con.execute("SELECT count(*) FROM serie_facturas").fetchone()[0] == 0


def test_la_migracion_solo_crea_y_no_escribe_filas_ni_lleva_disparadores():
    ajenas = [s for s in _sentencias_que_corren(_texto_migracion())
              if not re.match(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\b", s, re.I)]
    assert ajenas == [], ajenas


# ── 3. facturas: cada restricción hace lo que dice ──────────────────────────────────────────────────

def test_un_borrador_normal_entra_nace_borrador_y_sin_numero():
    con = _base()
    f = _factura(con)
    assert con.execute("SELECT estado, anio, numero, ncf, proyecto_id, cotizacion_id FROM facturas WHERE id = ?",
                       (f,)).fetchone() == ("borrador", None, None, None, None, None)


def test_el_numero_no_se_repite_en_el_anio_y_si_en_otro_anio():
    con = _base()
    _poner(con, "facturas", **_emitida(1))
    _rechaza(con, "facturas", **_emitida(1, emitida_en=_AHORA))
    _poner(con, "facturas", **_emitida(2))
    _poner(con, "facturas", **_emitida(1, anio=2027))
    assert con.execute("SELECT count(*) FROM facturas").fetchone()[0] == 3


def test_los_borradores_sin_numero_no_chocan_entre_si():
    con = _base()
    for _ in range(3):
        _factura(con)
    assert con.execute("SELECT count(*) FROM facturas WHERE numero IS NULL").fetchone()[0] == 3


@pytest.mark.parametrize("campos", [{"anio": 2026}, {"numero": 1}, {"anio": 2026, "numero": 0},
                                    {"anio": 2026, "numero": -1}])
def test_anio_y_numero_van_juntos_y_el_numero_es_positivo(campos):
    _rechaza(_base(), "facturas", **_borrador(**campos))


def test_una_emitida_o_anulada_sin_numero_no_entra_y_un_borrador_con_numero_si():
    con = _base()
    _rechaza(con, "facturas", **_emitida(anio=None, numero=None))
    _rechaza(con, "facturas", **_anulada(anio=None, numero=None))
    _poner(con, "facturas", **_borrador(anio=2026, numero=5))


@pytest.mark.parametrize("estado", ["cancelada", "", "Borrador", "EMITIDA", "pagada", "con abono"])
def test_el_estado_solo_puede_ser_uno_de_los_tres(estado):
    _rechaza(_base(), "facturas", **_emitida(estado=estado))


def test_el_vocabulario_de_estados_de_la_base_es_el_del_diseno():
    for texto in (_ESQUEMA, _texto_migracion()):
        m = re.search(r"CONSTRAINT\s+facturas_estado_valido\s+CHECK\s*\(\s*estado\s+IN\s*\(([^)]*)\)", texto, re.I)
        assert m, "no encuentro facturas_estado_valido"
        assert tuple(v.strip().strip("'") for v in m.group(1).split(",")) == ESTADOS_DEL_DISENO


def test_emitida_en_existe_si_y_solo_si_no_es_borrador():
    con = _base()
    _rechaza(con, "facturas", **_emitida(emitida_en=None))
    _rechaza(con, "facturas", **_borrador(emitida_en=_AHORA))
    _poner(con, "facturas", **_emitida(1))


def test_anular_pide_su_motivo_y_el_motivo_solo_existe_si_esta_anulada():
    con = _base()
    _poner(con, "facturas", **_anulada(1))
    _rechaza(con, "facturas", **_anulada(2, motivo_anulacion=None))
    _rechaza(con, "facturas", **_anulada(3, motivo_anulacion="   "))
    _rechaza(con, "facturas", **_anulada(4, motivo_anulacion="x" * 501))
    _rechaza(con, "facturas", **_anulada(5, anulada_en=None))
    _rechaza(con, "facturas", **_emitida(6, motivo_anulacion="no debería"))
    _rechaza(con, "facturas", **_emitida(7, anulada_en=_AHORA))
    _rechaza(con, "facturas", **_emitida(8, anulada_por_chat_id=7))
    _poner(con, "facturas", **_anulada(9, motivo_anulacion="x" * 500))


def test_una_emitida_se_anula_con_un_update_pero_sin_motivo_no():
    con = _base()
    f = _poner(con, "facturas", **_emitida(1))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facturas SET estado = 'anulada', anulada_en = ? WHERE id = ?", (_AHORA, f))
    con.execute("UPDATE facturas SET estado = 'anulada', anulada_en = ?, motivo_anulacion = 'duplicada' "
                "WHERE id = ?", (_AHORA, f))
    assert con.execute("SELECT estado, numero FROM facturas WHERE id = ?", (f,)).fetchone() == ("anulada", 1)


# NCF ---------------------------------------------------------------------------------------------

def test_el_ncf_no_se_repite_ni_siquiera_el_de_una_anulada():
    con = _base()
    _poner(con, "facturas", **_anulada(1, ncf="B0100000001", ncf_tipo="B01"))
    _rechaza(con, "facturas", **_emitida(2, ncf="B0100000001", ncf_tipo="B01"))
    _rechaza(con, "facturas", **_borrador(ncf="B0100000001", ncf_tipo="B01"))
    _poner(con, "facturas", **_emitida(2, ncf="B0100000002", ncf_tipo="B01"))


def test_las_facturas_sin_ncf_no_chocan_entre_si():
    con = _base()
    for n in (1, 2, 3):
        _poner(con, "facturas", **_emitida(n))
    assert con.execute("SELECT count(*) FROM facturas WHERE ncf IS NULL").fetchone()[0] == 3


@pytest.mark.parametrize("ncf", ["b0100000001", "B0100000001 ", " B0100000001", "B01 00000001", "B01\t00000001",
                                 "B01\n00000001", "B0100000001\n", "B0100000001\t", "B01-00000001", "", "B" * 21])
def test_el_ncf_entra_solo_en_la_forma_normal(ncf):
    con = _base()
    _poner(con, "facturas", **_emitida(1, ncf="B0100000001", ncf_tipo="B01"))
    _rechaza(con, "facturas", **_emitida(2, ncf=ncf, ncf_tipo="B01"))


def test_el_ncf_y_su_tipo_van_juntos_o_ninguno():
    con = _base()
    _rechaza(con, "facturas", **_emitida(1, ncf="B0100000001"))
    _rechaza(con, "facturas", **_emitida(1, ncf_tipo="B01"))
    _rechaza(con, "facturas", **_emitida(1, ncf="B0100000001", ncf_tipo="  "))
    _poner(con, "facturas", **_emitida(1, ncf="B" * 20, ncf_tipo="B" * 20))


def test_el_rnc_o_cedula_es_opcional_con_su_largo():
    con = _base()
    _poner(con, "facturas", **_emitida(1, rnc_cedula="131000000"))
    _rechaza(con, "facturas", **_emitida(2, rnc_cedula="  "))
    _rechaza(con, "facturas", **_emitida(2, rnc_cedula="9" * 21))


# ITBIS, cliente y quién la hizo ------------------------------------------------------------------

@pytest.mark.parametrize("pct", [0, -1, 100.01, 101])
def test_el_itbis_es_un_porcentaje_mayor_que_cero_y_hasta_cien(pct):
    _rechaza(_base(), "facturas", **_borrador(itbis_pct=pct))


@pytest.mark.parametrize("pct", [None, 18, 0.01, 100])
def test_el_itbis_deja_pasar_su_borde_de_adentro_y_la_ausencia(pct):
    _factura(_base(), itbis_pct=pct)


@pytest.mark.parametrize("campo,valor", [("para_nombre", ""), ("para_nombre", "   "), ("para_nombre", "x" * 201),
                                         ("cliente_noco_id", None), ("fecha", None), ("hecha_por", None)])
def test_cada_dato_obligatorio_de_la_factura_frena_su_borde(campo, valor):
    _rechaza(_base(), "facturas", **_borrador(**{campo: valor}))


def test_quien_la_hizo_es_la_casa_con_su_persona_o_natalia_sin_ella():
    con = _base()
    _poner(con, "facturas", **_borrador(hecha_por="natalia", hecha_por_chat_id=None))
    _poner(con, "facturas", **_borrador(hecha_por="casa", hecha_por_chat_id=7))
    _rechaza(con, "facturas", **_borrador(hecha_por="casa", hecha_por_chat_id=None))
    _rechaza(con, "facturas", **_borrador(hecha_por="natalia", hecha_por_chat_id=7))
    _rechaza(con, "facturas", **_borrador(hecha_por="rosi", hecha_por_chat_id=7))
    _rechaza(con, "facturas", **_borrador(hecha_por="rosi", hecha_por_chat_id=None))   # solo el vocabulario la frena


# Ligas opcionales a proyecto y cotización --------------------------------------------------------

def test_el_proyecto_y_la_cotizacion_son_opcionales_y_si_se_ponen_existen():
    con = _base()
    _factura(con)                                                    # suelta: sin proyecto ni cotización
    _factura(con, proyecto_id=1)
    _factura(con, cotizacion_id=1, cotizacion_opcion=1)
    _rechaza(con, "facturas", **_borrador(proyecto_id=999))
    _rechaza(con, "facturas", **_borrador(cotizacion_id=999))


@pytest.mark.parametrize("campos", [{"cotizacion_opcion": 1}, {"cotizacion_id": 1, "cotizacion_opcion": 0},
                                    {"cotizacion_id": 1, "cotizacion_opcion": -1}])
def test_la_opcion_de_la_cotizacion_no_existe_sin_cotizacion_y_empieza_en_1(campos):
    _rechaza(_base(), "facturas", **_borrador(**campos))


def test_ligar_y_quitar_proyecto_y_cotizacion_despues_es_un_update():
    con = _base()
    f = _poner(con, "facturas", **_emitida(1))
    con.execute("UPDATE facturas SET proyecto_id = 1, cotizacion_id = 1, cotizacion_opcion = 2 WHERE id = ?", (f,))
    con.execute("UPDATE facturas SET proyecto_id = NULL, cotizacion_id = NULL, cotizacion_opcion = NULL "
                "WHERE id = ?", (f,))
    assert con.execute("SELECT proyecto_id, cotizacion_id FROM facturas WHERE id = ?", (f,)).fetchone() == (None, None)


# ── 3b. Los renglones ───────────────────────────────────────────────────────────────────────────────

def _renglon(factura, **otros) -> dict:
    base = {"factura_id": factura, "orden": 1, "descripcion": "Ensayo de dos horas", "monto": 2500}
    base.update(otros)
    return base


def test_un_renglon_normal_entra_y_el_orden_no_se_repite_dentro_de_la_factura():
    con = _base()
    a, b = _factura(con), _factura(con)
    _poner(con, "factura_renglones", **_renglon(a))
    _rechaza(con, "factura_renglones", **_renglon(a))
    _poner(con, "factura_renglones", **_renglon(a, orden=2, detalle="con ingeniero"))
    _poner(con, "factura_renglones", **_renglon(b))


@pytest.mark.parametrize("campo,valor", [
    ("monto", 0), ("monto", -1), ("monto", None), ("orden", 0), ("orden", -1),
    ("descripcion", ""), ("descripcion", "  "), ("descripcion", "x" * 301), ("detalle", "x" * 2001),
    ("factura_id", 999), ("factura_id", None)])
def test_cada_restriccion_del_renglon_frena_su_borde(campo, valor):
    con = _base()
    f = _factura(con)
    _rechaza(con, "factura_renglones", **_renglon(f, **{campo: valor}))


@pytest.mark.parametrize("campo,valor", [("monto", 0.01), ("descripcion", "x" * 300), ("detalle", "x" * 2000),
                                         ("detalle", None)])
def test_cada_restriccion_del_renglon_deja_pasar_su_borde_de_adentro(campo, valor):
    con = _base()
    _poner(con, "factura_renglones", **_renglon(_factura(con), **{campo: valor}))


# ── 3c. Las ligas a sesión o trabajo ────────────────────────────────────────────────────────────────

def _liga(factura, **otros) -> dict:
    base = {"factura_id": factura, "tipo": "sesion", "ref": "4521"}
    base.update(otros)
    return base


def test_una_factura_se_liga_a_varias_sesiones_o_a_un_trabajo():
    con = _base()
    f = _factura(con)
    _poner(con, "factura_ligas", **_liga(f, ref="1"))
    _poner(con, "factura_ligas", **_liga(f, ref="2", codigo="SES-0002", creado_por_chat_id=7))
    _poner(con, "factura_ligas", **_liga(f, tipo="trabajo", ref="1"))      # mismo texto, otro tipo: otra liga
    assert con.execute("SELECT count(*) FROM factura_ligas WHERE factura_id = ?", (f,)).fetchone()[0] == 3


@pytest.mark.parametrize("campo,valor", [
    ("tipo", "proyecto"), ("tipo", "cotizacion"), ("tipo", ""), ("tipo", "Sesion"), ("tipo", None),
    ("ref", ""), ("ref", " 12"), ("ref", "12 "), ("ref", "9" * 101), ("ref", None),
    ("codigo", "x" * 101), ("factura_id", 999), ("factura_id", None)])
def test_cada_restriccion_de_la_liga_frena_su_borde(campo, valor):
    con = _base()
    f = _factura(con)
    _rechaza(con, "factura_ligas", **_liga(f, **{campo: valor}))


def test_una_sola_liga_viva_por_factura_tipo_y_referencia_pero_desligada_deja_volver_a_ligar():
    con = _base()
    f, otra = _factura(con), _factura(con)
    primera = _poner(con, "factura_ligas", **_liga(f))
    _rechaza(con, "factura_ligas", **_liga(f))
    _poner(con, "factura_ligas", **_liga(otra))                      # la misma sesión en OTRA factura: la base deja
    con.execute("UPDATE factura_ligas SET desligada_en = ? WHERE id = ?", (_AHORA, primera))
    _poner(con, "factura_ligas", **_liga(f))                         # volver a ligar tras desligar
    _poner(con, "factura_ligas", **_liga(f, desligada_en=_AHORA))    # y varias desligadas conviven
    assert con.execute("SELECT count(*) FROM factura_ligas WHERE factura_id = ? AND desligada_en IS NULL",
                       (f,)).fetchone()[0] == 1


# ── 3d. Los envíos y la aprobación ──────────────────────────────────────────────────────────────────

def _envio(factura, **otros) -> dict:
    base = {"factura_id": factura, "canal": "whatsapp"}
    base.update(otros)
    return base


_APROBADO = {"aprobado_por_chat_id": 9, "aprobado_en": _AHORA}


def test_el_camino_completo_de_un_envio_por_natalia_con_su_aprobacion():
    con = _base()
    f = _factura(con)
    e = _poner(con, "factura_envios", **_envio(f))                                   # pendiente
    assert con.execute("SELECT estado FROM factura_envios WHERE id = ?", (e,)).fetchone()[0] == "pendiente"
    con.execute("UPDATE factura_envios SET estado = 'aprobado', aprobado_por_chat_id = 9, aprobado_en = ? "
                "WHERE id = ?", (_AHORA, e))
    con.execute("UPDATE factura_envios SET estado = 'enviado', enviado_en = ? WHERE id = ?", (_AHORA, e))
    assert con.execute("SELECT estado, aprobado_por_chat_id FROM factura_envios WHERE id = ?", (e,)).fetchone() == \
        ("enviado", 9)


@pytest.mark.parametrize("canal", ["whatsapp", "correo"])
@pytest.mark.parametrize("estado,extra", [("aprobado", {}), ("enviado", {"enviado_en": _AHORA})])
def test_por_whatsapp_o_correo_no_se_llega_a_aprobado_ni_a_enviado_sin_aprobacion(canal, estado, extra):
    con = _base()
    f = _factura(con)
    _rechaza(con, "factura_envios", **_envio(f, canal=canal, estado=estado, **extra))
    _poner(con, "factura_envios", **_envio(f, canal=canal, estado=estado, **extra, **_APROBADO))


def test_a_mano_se_marca_enviada_sin_aprobacion():
    con = _base()
    _poner(con, "factura_envios", **_envio(_factura(con), canal="mano", estado="enviado", enviado_en=_AHORA))


@pytest.mark.parametrize("campos", [{"aprobado_por_chat_id": 9}, {"aprobado_en": _AHORA}])
def test_la_aprobacion_son_dos_datos_juntos_o_ninguno(campos):
    con = _base()
    _rechaza(con, "factura_envios", **_envio(_factura(con), canal="mano", **campos))


@pytest.mark.parametrize("estado,extra,cabe", [
    ("enviado", {}, False), ("pendiente", {"enviado_en": _AHORA}, False), ("aprobado", {"enviado_en": _AHORA}, False),
    ("no_se_pudo", {"enviado_en": _AHORA, "motivo": "no contestó"}, False),
    ("enviado", {"enviado_en": _AHORA}, True)])
def test_enviado_y_solo_enviado_lleva_su_fecha(estado, extra, cabe):
    con = _base()
    f = _factura(con)
    campos = _envio(f, canal="mano", estado=estado, **extra)
    if cabe:
        _poner(con, "factura_envios", **campos)
    else:
        _rechaza(con, "factura_envios", **campos)


@pytest.mark.parametrize("estado", ["rechazado", "no_se_pudo"])
def test_rechazado_y_no_se_pudo_llevan_su_motivo(estado):
    con = _base()
    f = _factura(con)
    for malo in (None, "", "   ", "x" * 501):
        _rechaza(con, "factura_envios", **_envio(f, canal="mano", estado=estado, motivo=malo))
    _poner(con, "factura_envios", **_envio(f, canal="mano", estado=estado, motivo="el cliente dijo que no"))


@pytest.mark.parametrize("campo,valor", [("canal", "sms"), ("canal", ""), ("canal", None), ("estado", "mandada"),
                                         ("estado", ""), ("estado", None), ("factura_id", 999), ("factura_id", None)])
def test_cada_restriccion_del_envio_frena_su_borde(campo, valor):
    con = _base()
    _rechaza(con, "factura_envios", **_envio(_factura(con), **{campo: valor}))


# ── 3e. La serie ────────────────────────────────────────────────────────────────────────────────────

def test_la_serie_no_baja_de_cero_y_un_anio_va_una_vez():
    con = _base()
    con.execute("INSERT INTO serie_facturas (anio, ultimo) VALUES (2026, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serie_facturas (anio, ultimo) VALUES (2027, -1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serie_facturas (anio, ultimo) VALUES (2026, 5)")


# ── 4. No hay columnas de pago ni de totales ────────────────────────────────────────────────────────

def test_ninguna_tabla_guarda_pagos_ni_totales():
    """El pago se anota en la App y la factura lo lee; los totales salen de los renglones y de `itbis_pct`.
    Las columnas se leen de las tablas ya creadas (no de una lista tecleada)."""
    con = _base()
    columnas = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in TABLAS}
    assert all(columnas.values())
    sospechosas = [(t, c) for t, cs in columnas.items() for c in cs
                   if re.search(r"pag|cobr|saldo|abono|total|subtotal", c, re.I)]
    assert sospechosas == [], sospechosas
    assert "monto" in columnas["factura_renglones"]


# ── 6. Clasificación y comparación con la base real ─────────────────────────────────────────────────

@pytest.mark.parametrize("tabla", TABLAS)
def test_las_tablas_nuevas_son_maquinaria_con_su_motivo(tabla):
    assert tabla in db.columnas_declaradas(), "db/schema.sql no la declara"
    motivo = consultar.TABLAS_DE_MAQUINARIA.get(tabla)
    assert isinstance(motivo, str) and len(motivo.split()) >= 4, f"{tabla} sin motivo en TABLAS_DE_MAQUINARIA"
    assert tabla not in consultar.TABLAS_DE_TIZIANO
    assert tabla not in crud.TABLAS, "Telegram no las edita en esta versión"
    assert tabla not in consultar.ESQUEMA, "Lucy no las ve por Telegram"


def _bloques_de_la_migracion() -> dict[str, str]:
    """{tabla: texto del CREATE TABLE} sacado de la migración (sin comentarios)."""
    limpio = "\n".join(l.split("--")[0] for l in _texto_migracion().splitlines())
    return {m.group(1): m.group(2) for m in re.finditer(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\n\);", limpio, re.S | re.I)}


def test_las_restricciones_y_los_indices_con_nombre_entran_a_lo_que_el_arranque_compara_con_la_base():
    declarados = db.objetos_declarados()
    bloques = _bloques_de_la_migracion()
    assert set(bloques) == set(TABLAS)
    total = 0
    for tabla, cuerpo in bloques.items():
        for nombre in re.findall(r"CONSTRAINT\s+(\w+)", cuerpo, re.I):
            assert declarados["restricciones"].get(nombre) == tabla, nombre
            total += 1
    assert total == len(_guion().restricciones_de(_texto_migracion()))
    limpio = "\n".join(l.split("--")[0] for l in _texto_migracion().splitlines())
    indices = re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)", limpio, re.I)
    assert len(indices) >= 5
    for nombre, tabla in indices:
        assert declarados["indices"].get(nombre) == tabla, nombre
    columnas = db.columnas_declaradas()
    assert columnas["serie_facturas"] == ["anio", "ultimo"]
    assert columnas["facturas"][:3] == ["id", "anio", "numero"]


# ── 7. El guion de ensayo ───────────────────────────────────────────────────────────────────────────

def test_el_guion_saca_el_commit_y_solo_el_commit():
    guion = _guion()
    texto = _texto_migracion()
    ensayo = guion.texto_del_ensayo(texto)
    assert "COMMIT" not in re.sub(r"--[^\n]*", "", ensayo)
    assert ensayo == texto.rstrip()[: -len("COMMIT;")].rstrip() + "\n"
    assert "BEGIN;" in ensayo and "CREATE TABLE IF NOT EXISTS facturas" in ensayo


def test_el_sql_del_ensayo_nunca_confirma_y_cierra_con_rollback():
    guion = _guion()
    sql = guion.sql_del_ensayo(_texto_migracion())
    sentencias = ct._sentencias_sin_meta(sql)
    assert not any(re.match(r"COMMIT\b", s, re.I) for s in sentencias)
    assert sum(bool(re.match(r"BEGIN\b", s, re.I)) for s in sentencias) == 1
    crea = [i for i, s in enumerate(sentencias) if s.upper().startswith("CREATE TABLE")]
    rollback = [i for i, s in enumerate(sentencias) if s.upper() == "ROLLBACK"]
    assert len(crea) == len(TABLAS) and len(rollback) == 1 and rollback[0] > max(crea)
    for s in sentencias[rollback[0] + 1:]:
        assert s.upper().startswith("SELECT"), s
    assert sql.count(" AS ok") == len(TABLAS) + 1 + 1 + len(TABLAS)
    for nombre in guion.restricciones_de(_texto_migracion()):
        assert f"'{nombre}'" in sql, nombre
    assert re.search(r"lock_timeout", sql) and sql.startswith("\\set ON_ERROR_STOP on")


def test_el_sql_de_la_cuenta_solo_lee_y_nombra_las_cinco():
    sql = _guion().sql_de_la_cuenta(_texto_migracion())
    sentencias = ct._sentencias_sin_meta(sql)
    assert sentencias[0].upper() == "BEGIN READ ONLY" and sentencias[-1].upper() == "ROLLBACK"
    assert not re.search(r"\b(CREATE|INSERT|UPDATE|DELETE|DROP|ALTER|COMMIT)\b", sql, re.I)
    for t in TABLAS:
        assert f"'{t}'" in sql


def test_el_guion_imprime_sin_conectarse_y_se_niega_con_argumentos_raros(capsys):
    guion = _guion()
    assert guion.main(["--aplicar"]) == 2 and guion.main(["--cuenta", "--ensayo"]) == 2
    assert capsys.readouterr().out == ""
    assert guion.main(["--ensayo"]) == 0
    assert capsys.readouterr().out == guion.sql_del_ensayo(_texto_migracion())
    assert guion.main([]) == 0
    assert capsys.readouterr().out == guion.sql_de_la_cuenta(_texto_migracion())


def test_el_guion_no_se_conecta_a_nada():
    fuente = (_ROOT / "tools" / "ensayo_facturas.py").read_text(encoding="utf-8")
    codigo = re.sub(r'"""(.*?)"""', "", fuente, flags=re.S)
    assert not re.search(r"^\s*(import|from)\s+(psycopg|db|config|os)\b", codigo, re.M)
    assert "DATABASE" not in codigo and ".execute(" not in codigo


# ── 8. El humo ──────────────────────────────────────────────────────────────────────────────────────

def _humo():
    return ast.parse((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"))


def test_humo_lee_las_cinco_tablas_de_las_facturas_y_la_corre_en_su_lista():
    """Se lee el archivo con `ast` (no se importa: al importarlo arranca y se conecta). Las tablas que recorre
    `_tablas_de_facturas` son las de la migración, la función está en la lista `pruebas` y la cuenta de
    facturas sin liga se ejecuta con el texto que expone el archivo."""
    arbol = _humo()
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tablas_de_facturas")
    recorridas = [e for n in ast.walk(funcion) if isinstance(n, ast.For)
                  for e in ast.walk(n.iter) if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert sorted(c.value for c in recorridas) == sorted(TABLAS)
    nombres = {n.id for n in ast.walk(funcion) if isinstance(n, ast.Name)}
    assert "SENTENCIA_DE_FACTURAS_SIN_LIGA" in nombres
    en_la_lista = [e for n in ast.walk(arbol) if isinstance(n, ast.Assign)
                   and any(isinstance(x, ast.Name) and x.id == "pruebas" for x in n.targets)
                   for e in ast.walk(n.value) if isinstance(e, ast.Name) and e.id == "_tablas_de_facturas"]
    assert en_la_lista, "la función del humo existe pero no está en la lista `pruebas`"


def _sentencia_de_facturas_sin_liga() -> str:
    for n in ast.walk(_humo()):
        if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "SENTENCIA_DE_FACTURAS_SIN_LIGA"
                                             for x in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError("no está SENTENCIA_DE_FACTURAS_SIN_LIGA en tools/humo.py")


def test_la_cuenta_de_facturas_sin_liga_viva_corre_y_cuenta_bien():
    sentencia = _sentencia_de_facturas_sin_liga()
    con = _base()
    cuenta = lambda: con.execute(sentencia).fetchone()[0]          # noqa: E731
    assert cuenta() == 0                                           # sin facturas
    a, b, c = _factura(con), _poner(con, "facturas", **_emitida(1)), _poner(con, "facturas", **_anulada(2))
    assert cuenta() == 3                                           # tres sin liga, de cualquier estado
    _poner(con, "factura_ligas", **_liga(a))
    _poner(con, "factura_ligas", **_liga(b, tipo="trabajo", ref="77"))
    assert cuenta() == 1
    _poner(con, "factura_ligas", **_liga(c, desligada_en=_AHORA))  # una desligada no cuenta como viva
    assert cuenta() == 1
    _poner(con, "factura_ligas", **_liga(c, ref="9"))
    assert cuenta() == 0
