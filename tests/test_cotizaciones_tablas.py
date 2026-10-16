"""Las tablas de las cotizaciones de un proyecto (pieza 1 del diseño «Cotizaciones en Proyectos»,
14-oct-2026): `cotizaciones` y `serie_cotizaciones`, su migración, su clasificación y el guion de ensayo.

Qué se vigila, con el TEXTO REAL de `db/schema.sql` y de la migración, EJECUTADO en SQLite:

  1. la migración y el esquema declaran lo mismo (las dos tablas, sus restricciones con nombre incluidas);
  2. la migración corre entera (todas sus sentencias menos `BEGIN`/`COMMIT`/`COMMENT ON`, que SQLite no
     tiene), las dos tablas nacen VACÍAS (la serie no se siembra: respuesta 1 de Tiziano), y no hay
     ningún `INSERT`/`UPDATE`/`DELETE`/`CREATE TRIGGER` en ella;
  3. cada restricción hace lo que dice, con filas inventadas: `UNIQUE (anio, numero)`, estados, validez,
     nombre, teléfono, `enviada_en` si y solo si es enviada, serie sin negativos, llave foránea;
  4. una enviada SE PUEDE corregir con el mismo número (respuesta 2): el esquema no lo impide;
  5. los vocabularios cerrados de la base (estados, validez) son los mismos que declara el código
     (`db.ESTADOS_DE_COTIZACION`, `db.VALIDEZ_DE_COTIZACION`): se leen de los CHECK, no se teclean;
  6. las dos tablas están clasificadas en `cerebro/consultar.py` como maquinaria con su motivo (y no en
     la lista de las que Lucy consulta ni en las que Telegram edita); las tablas salen de la migración;
     los nombres de restricción de la migración entran a lo que el arranque compara con la base real;
  7. el guion de ensayo (`tools/ensayo_cotizaciones.py`, que imprime SQL para `psql`): saca el `COMMIT` y
     solo ese, `--cuenta` solo lee, `--ensayo` cierra con `ROLLBACK` y nunca manda un `COMMIT`.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. Las restricciones corren en SQLite con el texto de la migración; los
    dos cambios de dialecto son los de siempre (`BIGSERIAL` y `DEFAULT now()`). `COMMENT ON` no corre. El
    candado que pide `REFERENCES proyectos` y que `psycopg` acepte el texto entero en una llamada NO se
    ejercen aquí: es lo que cubre el ensayo contra la base real (`tools/ensayo_cotizaciones.py`).
  · El guion solo ARMA SQL (no se conecta); se prueba el texto que arma. Lo que contestarían
    `information_schema` y `pg_constraint` a sus comprobaciones no se ejerció: el ensayo de verdad lo
    corre la sala con `psql`.

Correr:  python3 -m pytest tests/test_cotizaciones_tablas.py -q
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3

import pytest

import test_base_m2 as b2
import test_grupo_ia as g
from test_grupo_ia import _ROOT

import config
import db.db as db
from acciones import crud
from cerebro import consultar

TABLAS = ("cotizaciones", "serie_cotizaciones")
_ESQUEMA = (_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")


def _migracion():
    suyas = [a for a in g._migraciones()
             if "CREATE TABLE IF NOT EXISTS cotizaciones" in a.read_text(encoding="utf-8")]
    assert len(suyas) == 1, suyas
    return suyas[0]


def _texto_migracion() -> str:
    return _migracion().read_text(encoding="utf-8")


def _migracion_de_drive():
    """La migración de la pieza 8 (las tres columnas de Drive y `drive_de_proyectos`), buscada por lo que hace."""
    suyas = [a for a in g._migraciones()
             if "CREATE TABLE IF NOT EXISTS drive_de_proyectos" in a.read_text(encoding="utf-8")]
    assert len(suyas) == 1, suyas
    return suyas[0]


def _texto_migracion_de_drive() -> str:
    return _migracion_de_drive().read_text(encoding="utf-8")


def _guion():
    spec = importlib.util.spec_from_file_location("ensayo_cotizaciones", _ROOT / "tools" / "ensayo_cotizaciones.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _a_sqlite(s: str) -> str:
    return (s.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY").replace("DEFAULT now()", "DEFAULT (now())")
            .replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN"))


def _base_con_migracion(texto: str | None = None) -> sqlite3.Connection:
    """Una base con `proyectos` (mínima) y TODAS las sentencias de la migración que SQLite entiende."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.create_function("now", 0, lambda: "2026-10-14T12:00:00+00:00")
    con.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO proyectos (id) VALUES (1)")
    for s in g._sentencias(_texto_migracion() if texto is None else texto):
        if re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
            continue
        con.execute(_a_sqlite(s))
    return con


def _fila(**otros) -> dict:
    base = {"proyecto_id": 1, "anio": 2026, "numero": 1, "estado": "borrador", "para_nombre": "Cliente de prueba",
            "fecha": "2026-10-14", "validez_dias": 15, "contenido": "{}"}
    base.update(otros)
    return base


def _insertar(con, **otros):
    f = _fila(**otros)
    con.execute(f"INSERT INTO cotizaciones ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", tuple(f.values()))


# ── 1. La migración y el esquema dicen lo mismo ─────────────────────────────────────────────────────

def _columnas_que_agregan_otras_migraciones(tabla: str) -> set[str]:
    """Las columnas que una migración POSTERIOR agrega con `ALTER TABLE <tabla> ADD COLUMN` (sacadas del texto de
    todas las migraciones, no tecleadas): el esquema las declara dentro del `CREATE TABLE` y la migración que
    crea la tabla, no."""
    agregadas = set()
    for archivo in g._migraciones():
        if archivo == _migracion():
            continue
        agregadas |= {c.lower() for c in re.findall(
            rf"ALTER\s+TABLE\s+{tabla}\s+ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)",
            archivo.read_text(encoding="utf-8"), re.I)}
    return agregadas


def _sin_columnas(sentencia: str, columnas: set[str]) -> str:
    """El `CREATE TABLE` sin las líneas de esas columnas (una columna por línea, como lo escribe el esquema)."""
    return "\n".join(l for l in sentencia.splitlines()
                     if not (l.split() and l.split()[0].lower() in columnas))


@pytest.mark.parametrize("tabla", TABLAS)
def test_la_migracion_y_el_esquema_dicen_lo_mismo(tabla):
    de_la_migracion = [b2._normal(s).replace("if not exists ", "") for s in b2._ddl(_texto_migracion(), tabla)]
    # lo que otra migración agrega después (pieza 8: las tres columnas de Drive) se compara en
    # `tests/test_cotizaciones_drive.py`; aquí el esquema se mira sin esas columnas
    quitar = _columnas_que_agregan_otras_migraciones(tabla)
    del_esquema = [b2._normal(_sin_columnas(s, quitar)) for s in b2._ddl(_ESQUEMA, tabla)]
    assert del_esquema, f"{tabla} no está en db/schema.sql"
    assert de_la_migracion == del_esquema


def test_las_tablas_de_la_migracion_son_las_dos_y_nada_mas():
    assert tuple(_guion().tablas_de(_texto_migracion())) == TABLAS


# ── 2. La migración corre y las tablas nacen vacías ─────────────────────────────────────────────────

def test_la_migracion_corre_y_las_dos_tablas_nacen_vacias():
    con = _base_con_migracion()
    for t in TABLAS:
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, f"{t} no nació vacía"
    # La segunda vez no hace nada (idempotente): las mismas sentencias otra vez.
    for s in g._sentencias(_texto_migracion()):
        if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
            con.execute(_a_sqlite(s))
    assert con.execute("SELECT count(*) FROM serie_cotizaciones").fetchone()[0] == 0


def test_la_migracion_no_escribe_filas_ni_lleva_disparadores():
    """Ni siembra la serie (respuesta 1 de Tiziano) ni impide corregir una enviada (respuesta 2)."""
    ajenas = [s for s in g._sentencias(_texto_migracion())
              if not re.match(r"(BEGIN|COMMIT|CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|COMMENT\s+ON)\b", s, re.I)]
    assert ajenas == [], ajenas


# ── 3. Cada restricción hace lo que dice ────────────────────────────────────────────────────────────

def test_una_fila_normal_entra_y_el_estado_nace_borrador():
    con = _base_con_migracion()
    f = _fila()
    del f["estado"]
    con.execute(f"INSERT INTO cotizaciones ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", tuple(f.values()))
    assert con.execute("SELECT estado FROM cotizaciones").fetchone()[0] == "borrador"


def test_el_numero_no_se_repite_en_el_anio_y_si_en_otro_anio():
    con = _base_con_migracion()
    _insertar(con, numero=1)
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, numero=1)
    _insertar(con, numero=2)
    _insertar(con, anio=2027, numero=1)
    assert con.execute("SELECT count(*) FROM cotizaciones").fetchone()[0] == 3


@pytest.mark.parametrize("campo,valor", [
    ("numero", 0), ("numero", -3),
    ("estado", "congelada"), ("estado", ""), ("estado", "Borrador"),
    ("validez_dias", 10), ("validez_dias", 0), ("validez_dias", 31),
    ("para_nombre", ""), ("para_nombre", "   "), ("para_nombre", "x" * 201),
    ("para_telefono", "9" * 41),
    ("proyecto_id", 999),
    ("enviada_en", "2026-10-14T12:00:00+00:00"),        # borrador con fecha de envío
])
def test_cada_restriccion_frena_su_borde(campo, valor):
    con = _base_con_migracion()
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, **{campo: valor})
    assert con.execute("SELECT count(*) FROM cotizaciones").fetchone()[0] == 0


@pytest.mark.parametrize("campo,valor", [
    ("validez_dias", 7), ("validez_dias", 15), ("validez_dias", 30),
    ("para_nombre", "x" * 200), ("para_telefono", "9" * 40), ("para_telefono", None),
])
def test_cada_restriccion_deja_pasar_su_borde_de_adentro(campo, valor):
    con = _base_con_migracion()
    _insertar(con, **{campo: valor})


def test_enviada_pide_su_fecha_y_la_fecha_pide_enviada():
    con = _base_con_migracion()
    with pytest.raises(sqlite3.IntegrityError):
        _insertar(con, estado="enviada")                                    # enviada sin enviada_en
    _insertar(con, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00")


def test_la_serie_no_baja_de_cero_y_un_anio_va_una_vez():
    con = _base_con_migracion()
    con.execute("INSERT INTO serie_cotizaciones (anio, ultimo) VALUES (2026, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serie_cotizaciones (anio, ultimo) VALUES (2027, -1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serie_cotizaciones (anio, ultimo) VALUES (2026, 5)")


# ── 4. Una enviada se puede corregir con el mismo número ────────────────────────────────────────────

def test_una_enviada_se_puede_corregir_y_conserva_su_numero():
    con = _base_con_migracion()
    _insertar(con, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00", numero=7)
    con.execute("UPDATE cotizaciones SET contenido = '{\"corregida\": true}', para_nombre = 'Otro nombre', "
                "validez_dias = 30, cambiado_en = '2026-10-15T08:00:00+00:00', "
                "enviada_en = '2026-10-15T08:00:00+00:00' WHERE numero = 7")
    fila = con.execute("SELECT anio, numero, estado, contenido FROM cotizaciones").fetchall()
    assert fila == [(2026, 7, "enviada", '{"corregida": true}')]


# ── 5. Los vocabularios del código son los de la base ───────────────────────────────────────────────

def _valores_del_check(texto: str, nombre: str) -> tuple[str, ...]:
    """Los valores de `CONSTRAINT <nombre> CHECK (<campo> IN (…))`, leídos del texto SQL."""
    m = re.search(rf"CONSTRAINT\s+{nombre}\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)", texto, re.I)
    assert m, f"no encuentro {nombre} en el SQL"
    return tuple(v.strip().strip("'") for v in m.group(1).split(","))


@pytest.mark.parametrize("texto", [_ESQUEMA, _texto_migracion()], ids=["esquema", "migracion"])
def test_los_vocabularios_del_codigo_son_los_de_los_check(texto):
    assert _valores_del_check(texto, "cotizaciones_estado_valido") == db.ESTADOS_DE_COTIZACION
    assert tuple(int(v) for v in _valores_del_check(texto, "cotizaciones_validez_valida")) == db.VALIDEZ_DE_COTIZACION


# ── 6. Clasificación y comparación con la base real ─────────────────────────────────────────────────

@pytest.mark.parametrize("tabla", TABLAS)
def test_las_tablas_nuevas_son_maquinaria_con_su_motivo(tabla):
    assert tabla in db.columnas_declaradas(), "db/schema.sql no la declara"
    motivo = consultar.TABLAS_DE_MAQUINARIA.get(tabla)
    assert isinstance(motivo, str) and len(motivo.split()) >= 4, f"{tabla} sin motivo en TABLAS_DE_MAQUINARIA"
    assert tabla not in consultar.TABLAS_DE_TIZIANO
    assert tabla not in crud.TABLAS, "Telegram no las edita en esta versión"
    assert tabla not in consultar.ESQUEMA, "Lucy no las ve por Telegram"


def test_las_restricciones_con_nombre_entran_a_lo_que_el_arranque_compara_con_la_base():
    declaradas = db.objetos_declarados()["restricciones"]
    en_la_migracion = _guion().restricciones_de(_texto_migracion())
    assert len(en_la_migracion) == len(set(en_la_migracion)) == 8
    for nombre in en_la_migracion:
        tabla = "serie_cotizaciones" if nombre.startswith("serie_") else "cotizaciones"
        assert declaradas.get(nombre) == tabla, nombre
    columnas = db.columnas_declaradas()
    assert columnas["serie_cotizaciones"] == ["anio", "ultimo"]
    assert columnas["cotizaciones"][:3] == ["id", "proyecto_id", "anio"]
    assert "contenido" in columnas["cotizaciones"] and "enviada_en" in columnas["cotizaciones"]


# ── 7. El guion de ensayo ───────────────────────────────────────────────────────────────────────────

def _sentencias_sin_meta(sql: str) -> list[str]:
    """Las sentencias de un SQL de psql, sin las líneas de metacomandos (`\\set`, `\\echo`) y con el
    `\\gexec` final leído como el `;` que cierra esa sentencia."""
    limpio = "\n".join(l.replace("\\gexec", ";") for l in sql.splitlines() if not l.startswith("\\set"))
    return g._sentencias(limpio)


def test_el_guion_saca_el_commit_y_solo_el_commit():
    guion = _guion()
    texto = _texto_migracion()
    ensayo = guion.texto_del_ensayo(texto)
    assert "COMMIT" not in re.sub(r"--[^\n]*", "", ensayo)
    assert texto.rstrip().endswith("COMMIT;")
    assert ensayo == texto.rstrip()[: -len("COMMIT;")].rstrip() + "\n"
    assert "BEGIN;" in ensayo and "CREATE TABLE IF NOT EXISTS cotizaciones" in ensayo


@pytest.mark.parametrize("malo", ["BEGIN;\nCREATE TABLE a (x INT);", "COMMIT;\nSELECT 1;",
                                   "BEGIN;\nCOMMIT;\nCOMMIT;"])
def test_el_guion_se_niega_si_no_hay_un_unico_commit_al_final(malo):
    with pytest.raises(ValueError):
        _guion().texto_del_ensayo(malo)


def test_el_sql_del_ensayo_nunca_confirma_y_cierra_con_rollback():
    guion = _guion()
    sql = guion.sql_del_ensayo(_texto_migracion())
    sentencias = _sentencias_sin_meta(sql)
    assert not any(re.match(r"COMMIT\b", s, re.I) for s in sentencias)
    assert sum(bool(re.match(r"BEGIN\b", s, re.I)) for s in sentencias) == 1
    posiciones = {"crea": [i for i, s in enumerate(sentencias) if s.upper().startswith("CREATE TABLE")],
                  "rollback": [i for i, s in enumerate(sentencias) if s.upper() == "ROLLBACK"]}
    assert len(posiciones["crea"]) == 2 and len(posiciones["rollback"]) == 1
    assert posiciones["rollback"][0] > max(posiciones["crea"])
    # Lo de antes del ROLLBACK que no es migración son comprobaciones (SELECT); lo de después, solo SELECT.
    for s in sentencias[posiciones["rollback"][0] + 1:]:
        assert s.upper().startswith("SELECT"), s
    # Las comprobaciones salen de la migración (tablas y restricciones), una fila `ok` cada una.
    assert sql.count(" AS ok") == 2 + 1 + 1 + 2           # filas de cada tabla, information_schema, restricciones, tras el ROLLBACK
    for nombre in guion.restricciones_de(_texto_migracion()):
        assert f"'{nombre}'" in sql, nombre
    assert re.search(r"lock_timeout", sql) and sql.startswith("\\set ON_ERROR_STOP on")


def test_el_sql_de_la_cuenta_solo_lee():
    sql = _guion().sql_de_la_cuenta(_texto_migracion())
    sentencias = _sentencias_sin_meta(sql)
    assert sentencias[0].upper() == "BEGIN READ ONLY" and sentencias[-1].upper() == "ROLLBACK"
    for s in sentencias[1:-1]:
        assert s.upper().startswith("SELECT"), s
    assert not re.search(r"\b(CREATE|INSERT|UPDATE|DELETE|DROP|ALTER|COMMIT)\b", sql, re.I)


def test_el_guion_imprime_sin_conectarse_y_se_niega_con_argumentos_raros(capsys):
    guion = _guion()
    assert guion.main(["--aplicar"]) == 2 and guion.main(["--cuenta", "--ensayo"]) == 2
    assert capsys.readouterr().out == ""
    assert guion.main(["--ensayo"]) == 0
    assert capsys.readouterr().out == guion.sql_del_ensayo(_texto_migracion())
    assert guion.main([]) == 0
    assert capsys.readouterr().out == guion.sql_de_la_cuenta(_texto_migracion())


def test_el_guion_no_se_conecta_a_nada():
    """No importa ningún cliente de base ni nombra variables de conexión: solo arma texto."""
    fuente = (_ROOT / "tools" / "ensayo_cotizaciones.py").read_text(encoding="utf-8")
    codigo = re.sub(r'"""(.*?)"""', "", fuente, flags=re.S)
    assert not re.search(r"^\s*(import|from)\s+(psycopg|db|config|os)\b", codigo, re.M)
    assert "DATABASE" not in codigo and ".execute(" not in codigo
