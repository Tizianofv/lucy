"""Lo que hay en el repo tiene que reproducir la base que corre en producción.

EL FALLO QUE MOTIVA ESTE ARCHIVO, medido el 5-sep-2026 contra 13e2f81, con
producción por un lado (PostgreSQL 18.6) y dos bases efímeras locales por el
otro (PostgreSQL 16.2), comparadas columna por columna, índice por índice y
restricción por restricción:

  · `idx_movimientos_hash` vivía SOLO en db/migrations/2026-08-30_ingesta_
    bancaria.sql. Una base creada con el `psql -f db/schema.sql` que manda el
    README (paso 4) no lo tenía, y entonces el
    `ON CONFLICT (hash_contenido) WHERE hash_contenido IS NOT NULL` de
    db.guardar_movimiento devolvía, medido:
        InvalidColumnReference: there is no unique or exclusion constraint
        matching the ON CONFLICT specification
    O sea: base nueva = ni un movimiento bancario entra. Nunca.
  · `cuentas_propias_patron_unico` estaba en esa misma migración, pero
    db/schema.sql crea la tabla con CREATE TABLE IF NOT EXISTS, así que al
    correr las dos cosas en orden el CREATE de la migración se saltaba entero
    y la restricción no llegaba NUNCA. Ni por schema.sql ni por la migración.
  · `idx_movimientos_banco` e `idx_correo_reportado_fecha` estaban en la base
    real y en ningún archivo del repo.

Los cuatro pasaron desapercibidos por la misma razón: los detectores que ya
existían —tablas_que_faltan(), columnas_que_faltan(), y la deriva de
db/backup.py— miran TABLAS y COLUMNAS. Nadie miraba los índices ni las
restricciones, que es donde vive la mitad del contrato de una base.

Lo que se prueba acá NO es que hoy estén esos cuatro objetos —eso se arregla
una vez y se vuelve a romper—. Es que las dos declaraciones del repo (el
archivo y las migraciones) no se puedan separar en silencio, y que ningún
ON CONFLICT del código pueda quedarse sin el índice que lo sostiene.

Lo que NO puede probar: lo que aparece a mano en producción y en ningún
archivo. Eso no lo ve ningún test hermético — lo ve db.objetos_que_faltan(),
que corre en el arranque (main.py) y en tools/humo.py.

Herméticos: se stubea psycopg antes de importar. No tocan ninguna base.

Correr:  python3 -m pytest tests/test_esquema_reproduce_la_base.py -q
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

# ── Stubs de psycopg ANTES de importar db.db ─────────────────────────────
_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")


class _FalsoPool:
    def __init__(self, *a, **k):
        pass


_pool.AsyncConnectionPool = _FalsoPool
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import db.db as base  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA = os.path.join(_ROOT, "db", "schema.sql")


def _sin_comentarios(texto: str) -> str:
    return "\n".join(l.split("--")[0] for l in texto.splitlines())


def _fuentes() -> list[tuple[str, str]]:
    """(ruta relativa, texto) de todo el Python del proyecto, sin tests.

    Se recorre el árbol en vez de listar los archivos a mano: un ON CONFLICT
    nuevo en un módulo que hoy no existe tiene que quedar cubierto igual. La
    lista escrita a mano es justo la que se queda atrás — pasó con la de
    tests/test_backup_alerta.py, que enumera tres nombres.
    """
    salida = []
    for carpeta, subs, archivos in os.walk(_ROOT):
        subs[:] = [s for s in subs if s not in
                   {".venv", ".git", "tests", "__pycache__", ".uv-cache",
                    ".uv-python", ".pytest_cache", "venv"}]
        for a in archivos:
            if a.endswith(".py"):
                ruta = os.path.join(carpeta, a)
                with open(ruta, encoding="utf-8") as f:
                    salida.append((os.path.relpath(ruta, _ROOT), f.read()))
    return salida


def _columnas(txt: str) -> tuple[str, ...]:
    return tuple(c.strip().lower() for c in txt.split(",") if c.strip())


def _claves_unicas_del_schema() -> set[tuple[str, tuple[str, ...]]]:
    """{(tabla, (columnas,))} que db/schema.sql declara como únicas.

    SOLO db/schema.sql: es el archivo que el README manda correr para crear la
    base (paso 4), así que es contra él que hay que poder guardar un
    movimiento. Que la clave esté en una migración no salva a la base nueva —
    que es exactamente lo que pasó con idx_movimientos_hash.
    """
    with open(_SCHEMA, encoding="utf-8") as f:
        texto = _sin_comentarios(f.read())

    claves: set[tuple[str, tuple[str, ...]]] = set()

    # CREATE [UNIQUE] INDEX ... ON tabla (cols)  — solo los UNIQUE sirven para
    # un ON CONFLICT; un índice normal no lo sostiene.
    for m in re.finditer(
            r"CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+"
            r"(\w+)\s*\(([^)]*)\)", texto, re.I):
        claves.add((m.group(1).lower(), _columnas(m.group(2))))

    # Lo declarado dentro del CREATE TABLE: PRIMARY KEY (...) y UNIQUE (...),
    # con o sin CONSTRAINT delante.
    for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);",
            texto, re.S | re.I):
        tabla, cuerpo = m.group(1).lower(), m.group(2)
        for c in re.finditer(
                r"(?:CONSTRAINT\s+\w+\s+)?(?:PRIMARY\s+KEY|UNIQUE)\s*\(([^)]*)\)",
                cuerpo, re.I):
            claves.add((tabla, _columnas(c.group(1))))
        # Y la PK de una sola columna escrita en la línea de la columna:
        #   cuenta TEXT PRIMARY KEY
        for linea in cuerpo.splitlines():
            palabras = linea.strip().split()
            if len(palabras) >= 2 and re.search(r"PRIMARY\s+KEY", linea, re.I) \
                    and not re.match(r"\s*(?:CONSTRAINT|PRIMARY)", linea, re.I):
                claves.add((tabla, (palabras[0].lower(),)))

    # ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)
    for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+ADD\s+CONSTRAINT\s+\w+"
            r"\s+UNIQUE\s*\(([^)]*)\)", texto, re.I):
        claves.add((m.group(1).lower(), _columnas(m.group(2))))

    return claves


def _on_conflict_del_codigo() -> list[tuple[str, str, tuple[str, ...]]]:
    """(archivo, tabla, columnas) de cada ON CONFLICT (...) que escribe el código.

    La tabla se saca del INSERT INTO que lo precede: un ON CONFLICT solo puede
    referirse a la tabla en la que se está insertando.
    """
    encontrados = []
    for ruta, fuente in _fuentes():
        for m in re.finditer(
                r"INSERT\s+INTO\s+(\w+)(.*?)(?=INSERT\s+INTO|\Z)",
                fuente, re.S):
            tabla, cuerpo = m.group(1).lower(), m.group(2)
            for c in re.finditer(r"ON\s+CONFLICT\s*\(([^)]*)\)", cuerpo, re.I):
                encontrados.append((ruta, tabla, _columnas(c.group(1))))
    return encontrados


# ── El candado que se pone rojo cuando las dos declaraciones se separan ──

def test_toda_migracion_declara_sus_objetos_tambien_en_schema():
    """Un índice o una restricción que vive SOLO en una migración es una bomba.

    La base nueva se crea con `psql -f db/schema.sql` (README, paso 4) y no
    con las migraciones — que además, medido el 5-sep-2026, no arman nada:
    contra una base vacía las siete fallan (`relation "tareas" does not
    exist`) y dejan 0 tablas. O sea que lo que solo esté en una migración NO
    ESTÁ en ninguna base creada desde el repo.

    Éste es el test que habría dado rojo el 30-ago-2026, el día que
    `idx_movimientos_hash` entró solo por la migración.
    """
    solo_schema = base.objetos_declarados(con_migraciones=False)
    con_todo = base.objetos_declarados()
    huerfanos = []
    for clase, etiqueta in (("indices", "índice"),
                            ("restricciones", "restricción")):
        for nombre, tabla in sorted(con_todo[clase].items()):
            if nombre not in solo_schema[clase]:
                huerfanos.append(f"{etiqueta} {tabla}.{nombre}")
    assert not huerfanos, (
        f"objetos que declaran las migraciones y db/schema.sql no: {huerfanos}. "
        "Una base creada con `psql -f db/schema.sql` no los tendría. Bajalos "
        "al archivo; la migración se queda igual, para las bases que ya existen.")


def test_todo_on_conflict_tiene_su_clave_unica_en_schema():
    """Un ON CONFLICT sin índice único detrás no es lento: revienta.

    Medido el 5-sep-2026 sobre una base efímera armada solo con db/schema.sql:
    el INSERT de db.guardar_movimiento devolvía
        InvalidColumnReference: there is no unique or exclusion constraint
        matching the ON CONFLICT specification
    y sobre la misma base con las migraciones aplicadas, id=1. La diferencia
    entera era `idx_movimientos_hash`.

    Se compara CONTRA db/schema.sql solo, a propósito: es el archivo con el que
    se crea una base nueva.
    """
    claves = _claves_unicas_del_schema()
    conflictos = _on_conflict_del_codigo()
    assert conflictos, (
        "no se encontró ni un ON CONFLICT en el código: el test dejó de mirar "
        "lo que dice que mira (¿cambió la forma de escribir los INSERT?)")
    sin_respaldo = [f"{ruta}: ON CONFLICT {tabla}{cols}"
                    for ruta, tabla, cols in conflictos
                    if (tabla, cols) not in claves]
    assert not sin_respaldo, (
        f"{len(sin_respaldo)} ON CONFLICT sin índice único ni restricción que "
        f"lo sostenga en db/schema.sql: {sin_respaldo}. Contra una base creada "
        "con ese archivo, esa consulta no es lenta: revienta con "
        "InvalidColumnReference y no guarda nada.")


def test_una_frase_en_prosa_no_inventa_una_tabla(tmp_path):
    """Una alarma que grita en falso enseña a ignorar la alarma.

    `tablas_que_faltan()` leía db/schema.sql SIN quitar los comentarios, así
    que cualquier frase que dijera "CREATE TABLE" le inventaba una tabla. El
    5-sep-2026, al documentar por qué el CREATE TABLE de una migración se salta,
    el detector empezó a reportar contra producción una tabla llamada `se`:
        tablas_que_faltan : ['se']
    El aviso del arranque dice «FALTAN TABLAS EN LA BASE», con log.error. Dos o
    tres de esos y nadie vuelve a leer ese renglón.

    Se prueba sobre un esquema de mentira en un directorio temporal, FUERA del
    repositorio: contra db/schema.sql pasaría aunque el parser volviera a
    ignorar los comentarios, hasta que a alguien se le ocurra la frase.
    """
    falso = tmp_path / "schema.sql"
    falso.write_text(
        "-- El CREATE TABLE se salta cuando la tabla ya existe.\n"
        "-- Ojo con CREATE TABLE IF NOT EXISTS en prosa.\n"
        "CREATE TABLE movimientos (\n"
        "  id BIGSERIAL PRIMARY KEY\n"
        ");\n", encoding="utf-8")
    assert base.tablas_declaradas(str(falso)) == {"movimientos"}, (
        "el lector de tablas se cree las frases de los comentarios")


def test_los_dos_lectores_de_schema_dicen_lo_mismo():
    """En el repo hay DOS parsers de db/schema.sql, y pueden separarse.

    `db.db.columnas_declaradas()` (lo usa el prompt del modelo y el chequeo del
    arranque) y `db.backup._tablas_del_repo()` (lo usa la deriva que imprime
    cada respaldo). Dos lectores del mismo archivo que responden distinto son
    la misma enfermedad que este archivo entero viene a atajar, un piso más
    abajo: uno de los dos avisaría de una deriva que el otro no ve.
    """
    import db.backup as backup

    del_prompt = {t: set(c) for t, c in
                  base.columnas_declaradas(_SCHEMA).items()}
    del_respaldo = backup._tablas_del_repo()
    assert del_respaldo is not None, "db/backup.py no pudo leer db/schema.sql"

    faltan_tablas = sorted(set(del_prompt) ^ set(del_respaldo))
    assert not faltan_tablas, (
        f"los dos lectores de db/schema.sql no ven las mismas tablas: "
        f"{faltan_tablas}")
    distintas = {t: sorted(del_prompt[t] ^ del_respaldo[t])
                 for t in del_prompt if del_prompt[t] != del_respaldo[t]}
    assert not distintas, (
        f"los dos lectores de db/schema.sql no ven las mismas columnas: "
        f"{distintas}")


# ── El detector contra la base real, que ningún test hermético reemplaza ─

def test_objetos_que_faltan_mira_las_dos_direcciones():
    """Igual que columnas_que_faltan: la dirección silenciosa es la que mata.

    · declarado y no en la base  → revienta, ruidoso.
    · en la base y no declarado  → la base nueva no lo tiene, y nadie se
      entera hasta el día de la recuperación. `idx_movimientos_banco` e
      `idx_correo_reportado_fecha` estuvieron así.
    """
    fuente = inspect.getsource(base.objetos_que_faltan)
    assert "set(decl) - set(reales)" in fuente, (
        "no se detecta lo declarado en db/schema.sql que la base no tiene")
    assert "set(reales) - set(decl)" in fuente, (
        "no se detecta lo que está en la base y db/schema.sql no declara — la "
        "dirección silenciosa, la que deja una base nueva sin ese objeto")
    assert "pg_index" in inspect.getsource(base) and "pg_constraint" in \
        inspect.getsource(base), "no se le pregunta al catálogo de la base"


def test_el_arranque_y_el_humo_avisan_de_los_objetos():
    """Un detector que no corre no detecta nada.

    Mismo candado que ya tienen tablas_que_faltan y columnas_que_faltan en
    tests/test_esquema_del_modelo.py: si el chequeo se cae del arranque o del
    humo, esto se pone rojo en vez de dejarlo mudo.
    """
    for nombre, ruta in (("main.py", os.path.join(_ROOT, "main.py")),
                         ("tools/humo.py", os.path.join(_ROOT, "tools", "humo.py"))):
        with open(ruta, encoding="utf-8") as f:
            texto = f.read()
        assert "objetos_que_faltan" in texto, (
            f"{nombre} no comprueba los índices ni las restricciones contra "
            "la base")
    with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as f:
        arranque = f.read()
    assert "log.error" in arranque.split("objetos_que_faltan")[-1][:400], (
        "el descuadre de índices se avisa por debajo de error: en el log del "
        "arranque eso no lo ve nadie")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
