"""Copia de seguridad de la base de Lucy (pilar #40).

Vuelca cada tabla del esquema public a un JSON comprimido, con marca de tiempo,
en la carpeta de backups. Restaurar = crear las tablas con el esquema y recargar
las filas de este archivo.

Se eligió un dump propio en Python, y no pg_dump, por dos razones: no depende
de tener instalado el cliente de PostgreSQL (ni de que su versión coincida con
la del servidor), y el formato JSON es legible y portable — un backup que solo
se puede restaurar con la herramienta exacta que lo creó es medio backup.

Pero un dump de DATOS sin la ESTRUCTURA tampoco es un backup: son filas sin
casa. Por eso desde el 30-ago-2026 cada corrida guarda además el esquema, en
dos capas:

  · `lucy_<sello>.schema.sql.gz` — el `pg_dump --schema-only` de verdad, cuando
    hay un pg_dump utilizable. Es el que restaura solo.
  · `datos["esquema"]` adentro del JSON — el catálogo leído por SQL (columnas,
    tipos, defaults, índices, constraints, extensiones). No hace falta ninguna
    herramienta para leerlo, y siempre está. Es el piso: con esto la base se
    puede reconstruir a mano aunque pg_dump no exista.

La carpeta destino vive en Google Drive: así la copia queda FUERA de Railway.
De nada sirve respaldar la base en el mismo lugar que podría caerse con ella.

── Por qué esto dejó de correr (30-ago-2026) ────────────────────────────────
Este archivo nunca tuvo quien lo llamara. No lo importa `main.py`, no lo llama
el bucle de `cerebro/interpretar.py`, no hay cron en `railway.json`. Y la ruta
destino era `G:\\My Drive\\Lucy\\backups`: una letra de unidad de Windows, que
en el contenedor Linux de Railway ni siquiera es una ruta absoluta. O sea que
esto JAMÁS corrió en producción — corría a las 20:00 desde la PC de Tiziano.
Cuando esa PC dejó de hacerlo (29-jul), no falló nada: simplemente nadie lo
volvió a ejecutar, y el sistema no tenía forma de notarlo porque la única
prueba de vida del respaldo era un archivo en una carpeta que Railway no ve.

Dos cambios cierran ese agujero:
  1. El destino ya no está clavado a una máquina (ver `_destino()`).
  2. Cada backup exitoso deja una fila en la tabla `backups`. Esa fila es lo
     que `cerebro/despertador.py` mira para avisar por Telegram cuando pasan
     más de 48 horas sin respaldo. El silencio ahora tiene quien lo denuncie.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

# En Windows, psycopg async necesita otra política; el backup es sincrónico,
# así que no aplica. Se deja el import de psycopg sincrónico a propósito.

# Cuántos backups se conservan. Una copia infinita llena el Drive.
CONSERVAR = 30

# Cuánto se le da a pg_dump antes de considerarlo perdido. El esquema de Lucy
# es chico; si tarda más de esto, algo está mal y no vale la pena esperar.
PG_DUMP_TIMEOUT_S = 120


# ── Dónde se guarda ──────────────────────────────────────────────────────────

def _candidatos() -> list[Path]:
    """Las rutas donde puede estar la carpeta de backups, en orden de preferencia.

    La lista existe porque la carpeta es la MISMA carpeta de Google Drive vista
    desde máquinas distintas, y cada sistema la monta en otro lado: `G:\\My Drive`
    en la PC de Windows, `~/Library/CloudStorage/GoogleDrive-<cuenta>/My Drive`
    en la Mac. Clavar una sola —como estaba— es atar el respaldo a una máquina,
    que es exactamente cómo se perdieron 25 días de copias.
    """
    casa = Path.home()
    rutas: list[Path] = [
        Path(r"G:\My Drive\Lucy\backups"),          # la de siempre (Windows)
        casa / "My Drive" / "Lucy" / "backups",
        casa / "Google Drive" / "My Drive" / "Lucy" / "backups",
        casa / "Google Drive" / "Lucy" / "backups",
    ]
    # macOS: la cuenta va en el nombre de la carpeta, así que hay que buscarla.
    nube = casa / "Library" / "CloudStorage"
    if nube.is_dir():
        for unidad in sorted(nube.glob("GoogleDrive-*")):
            rutas.append(unidad / "My Drive" / "Lucy" / "backups")
    return rutas


def _destino() -> Path:
    """La carpeta de backups de ESTA máquina. Revienta si no la encuentra.

    Con LUCY_BACKUP_DIR se manda a mano y no se discute (es la salida para
    Railway el día que el respaldo corra allá, o para un disco montado).

    La regla que importa está en el `else`: solo se crea la carpeta hoja
    `backups`, y únicamente si su PADRE ya existe. Crear el árbol entero sería
    fabricar un `~/Google Drive/...` local que NO sincroniza con nada y que se
    ve idéntico a uno que sí: el backup "andaría" durante meses y el día que
    haga falta no habría nada del otro lado. Preferimos reventar acá, ruidoso,
    a dar esa confianza falsa.
    """
    manual = os.environ.get("LUCY_BACKUP_DIR", "").strip()
    if manual:
        destino = Path(manual)
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    candidatas = _candidatos()

    # Primero, las carpetas que YA tienen backups adentro. Es la señal más
    # fuerte de "esta es la de verdad": si una máquina arrastra un
    # `~/Google Drive` viejo que ya no sincroniza al lado del montaje nuevo,
    # las dos existen y solo una tiene la historia. Elegir por existir a secas
    # podría mandar las copias a la muerta, y se vería idéntico desde afuera.
    con_historia = [r for r in candidatas
                    if r.is_dir() and any(r.glob("lucy_*.json.gz"))]
    if con_historia:
        return con_historia[0]

    for ruta in candidatas:
        if ruta.is_dir():
            return ruta
    for ruta in candidatas:
        if ruta.parent.is_dir():        # existe .../Lucy → falta solo 'backups'
            ruta.mkdir(exist_ok=True)
            return ruta

    raise SystemExit(
        "No encuentro la carpeta de backups de Google Drive en esta máquina.\n"
        "Busqué en:\n  " + "\n  ".join(str(r) for r in candidatas) + "\n"
        "Si Drive está montado en otro lado, pasá la ruta explícita:\n"
        "  LUCY_BACKUP_DIR='/ruta/a/Lucy/backups' python db/backup.py"
    )


def _url() -> str:
    """La DATABASE_URL: del entorno (Railway) o del .env local."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"].strip()
    env = Path(__file__).resolve().parent.parent / ".env"
    m = re.search(r"^DATABASE_URL=(.+)$", env.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("No encuentro DATABASE_URL.")
    return m.group(1).strip()


def _serializable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


# ── El esquema ───────────────────────────────────────────────────────────────

def _catalogo(conn) -> dict:
    """La estructura de la base leída por SQL: el piso que siempre está.

    No reemplaza a `pg_dump --schema-only` —esto no se restaura solo— pero se
    lee sin ninguna herramienta y no puede fallar por versiones que no coinciden.
    Con estas cuatro consultas se reconstruye la base a mano, y sobre todo se
    puede COMPARAR contra `db/schema.sql` para ver la deriva antes de que muerda.
    """
    cur = conn.cursor(row_factory=dict_row)

    columnas = cur.execute(
        """
        SELECT table_name, ordinal_position, column_name, data_type,
               udt_name, is_nullable, column_default,
               character_maximum_length, numeric_precision, numeric_scale
          FROM information_schema.columns
         WHERE table_schema = 'public'
         ORDER BY table_name, ordinal_position
        """
    ).fetchall()

    indices = cur.execute(
        "SELECT tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' ORDER BY tablename, indexname"
    ).fetchall()

    # pg_get_constraintdef devuelve el texto exacto del constraint: PK, FK,
    # UNIQUE y CHECK con su definición completa, listos para pegar en un CREATE.
    constraints = cur.execute(
        """
        SELECT rel.relname AS tabla, con.conname AS nombre,
               pg_get_constraintdef(con.oid) AS definicion
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
          JOIN pg_namespace ns ON ns.oid = rel.relnamespace
         WHERE ns.nspname = 'public'
         ORDER BY rel.relname, con.conname
        """
    ).fetchall()

    extensiones = cur.execute(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname"
    ).fetchall()

    por_tabla: dict[str, list] = {}
    for c in columnas:
        por_tabla.setdefault(c["table_name"], []).append(
            {k: _serializable(v) for k, v in c.items() if k != "table_name"})

    return {
        "columnas": por_tabla,
        "indices": [dict(i) for i in indices],
        "constraints": [dict(c) for c in constraints],
        "extensiones": [dict(e) for e in extensiones],
    }


def _sin_clave(url: str) -> tuple[str, dict[str, str]]:
    """Saca la contraseña de la URL y la manda por entorno.

    La línea de comandos de un proceso la lee cualquiera con un `ps` en la
    misma máquina; el entorno, no. Como acá la URL se le pasa a pg_dump como
    argumento, dejar la clave adentro sería publicar la credencial de la base
    entera cada vez que corre el backup. libpq lee PGPASSWORD sin chistar.
    """
    partes = urlsplit(url)
    if not partes.password:
        return url, {}
    # `partes.password` viene tal cual está en la URL (puede estar
    # percent-encodeada); libpq espera la contraseña ya decodificada.
    neto = partes.netloc.replace(f":{partes.password}@", "@", 1)
    limpia = urlunsplit(
        (partes.scheme, neto, partes.path, partes.query, partes.fragment))
    return limpia, {"PGPASSWORD": unquote(partes.password)}


def _pg_dump_esquema(url: str, archivo: Path) -> bool:
    """Escribe `pg_dump --schema-only` comprimido. False si no se pudo.

    Que falle es ESPERABLE y no es un error del backup: puede no haber pg_dump
    instalado, o su versión puede ser más vieja que la del servidor (pg_dump se
    niega, con razón, a volcar una base más nueva que él). Por eso devuelve un
    bool en vez de reventar: el JSON con el catálogo ya salió, y quedarse sin
    este archivo degrada la calidad del backup, no lo anula.
    """
    url_sin_clave, extra = _sin_clave(url)
    try:
        r = subprocess.run(
            ["pg_dump", "--schema-only", "--no-owner", "--no-privileges",
             "--dbname", url_sin_clave],
            capture_output=True, timeout=PG_DUMP_TIMEOUT_S,
            env={**os.environ, **extra},
        )
    except FileNotFoundError:
        print("    (sin pg_dump instalado; el esquema queda en el catálogo del JSON)")
        return False
    except subprocess.TimeoutExpired:
        print(f"    (pg_dump no terminó en {PG_DUMP_TIMEOUT_S}s; sigo sin él)",
              file=sys.stderr)
        return False

    if r.returncode != 0 or not r.stdout.strip():
        detalle = r.stderr.decode("utf-8", "replace").strip().splitlines()
        print(f"    (pg_dump falló: {detalle[-1] if detalle else r.returncode})",
              file=sys.stderr)
        return False

    with gzip.open(archivo, "wb") as f:
        f.write(r.stdout)
    return True


# ── Deriva contra db/schema.sql ──────────────────────────────────────────────

def _tablas_del_repo() -> dict[str, set[str]] | None:
    """Tablas y columnas declaradas en db/schema.sql. None si no se puede leer.

    Es un parseo a propósito ingenuo —lee los bloques `CREATE TABLE x (...)` y
    se queda con la primera palabra de cada línea— porque solo tiene que
    responder una pregunta: ¿el archivo que el README manda correr describe la
    base que existe? Para eso alcanza con los nombres.

    Existe por lo que pasó: `correo_reportado` vivió meses en producción sin
    estar en el esquema del repo, y `psql -f db/schema.sql` creaba una base
    rota. Nadie lo notó porque nada lo miraba. Ahora lo mira cada backup.
    """
    ruta = Path(__file__).resolve().parent / "schema.sql"
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError:
        return None

    # Los comentarios se van primero: adentro hay ejemplos de SQL que no son
    # esquema, y contarlos daría una deriva fantasma.
    texto = re.sub(r"--[^\n]*", "", texto)

    declarado: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\)\s*;",
                         texto, re.S | re.I):
        tabla, cuerpo = m.group(1).lower(), m.group(2)
        columnas = set()
        for linea in cuerpo.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            primera = linea.split()[0].strip(",").lower()
            # CONSTRAINT/PRIMARY/UNIQUE/FOREIGN/CHECK abren definiciones de
            # tabla, no columnas.
            if primera in {"constraint", "primary", "unique", "foreign", "check",
                           "exclude", "like"}:
                continue
            if primera.isidentifier():
                columnas.add(primera)
        declarado[tabla] = columnas
    return declarado or None


def _avisar_deriva(catalogo: dict) -> list[str]:
    """Compara la base viva contra db/schema.sql y devuelve las diferencias."""
    declarado = _tablas_del_repo()
    if declarado is None:
        return []

    vivo = {t.lower(): {c["column_name"].lower() for c in cols}
            for t, cols in catalogo["columnas"].items()}

    problemas: list[str] = []
    for tabla in sorted(set(vivo) - set(declarado)):
        problemas.append(f"tabla '{tabla}' existe en producción y NO en schema.sql")
    for tabla in sorted(set(declarado) - set(vivo)):
        problemas.append(f"tabla '{tabla}' está en schema.sql y NO en producción")
    for tabla in sorted(set(vivo) & set(declarado)):
        for col in sorted(vivo[tabla] - declarado[tabla]):
            problemas.append(f"{tabla}.{col} existe en producción y NO en schema.sql")
        for col in sorted(declarado[tabla] - vivo[tabla]):
            problemas.append(f"{tabla}.{col} está en schema.sql y NO en producción")
    return problemas


# ── El latido ────────────────────────────────────────────────────────────────

def _registrar(conn, archivo: Path, tablas: int, filas: int, esquema: str) -> None:
    """Deja la fila que prueba que este backup existió y terminó bien.

    Va DESPUÉS de cerrar el archivo, nunca antes: la fila significa "el archivo
    está completo en el disco". Un backup que revienta a la mitad no deja fila,
    y a las 48 horas el despertador avisa.

    Si esto falla (típico: la migración del 30-ago no se corrió y la tabla no
    existe), NO se pierde el backup — el archivo ya está escrito. Se avisa
    fuerte y se sigue. El costo de fallar acá es una alerta de más, que es el
    lado correcto del que equivocarse.
    """
    conn.execute(
        "INSERT INTO backups (archivo, bytes, tablas, filas, esquema, origen) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (archivo.name, archivo.stat().st_size, tablas, filas, esquema,
         socket.gethostname()),
    )


def hacer_backup() -> Path:
    destino = _destino()
    sello = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archivo = destino / f"lucy_{sello}.json.gz"
    archivo_esquema = destino / f"lucy_{sello}.schema.sql.gz"

    datos: dict = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "esquema": {},
        "tablas": {},
    }

    url = _url()
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        datos["esquema"] = _catalogo(conn)

        tablas = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "ORDER BY tablename")]
        for t in tablas:
            filas = conn.execute(f"SELECT * FROM {t}").fetchall()
            datos["tablas"][t] = [
                {k: _serializable(v) for k, v in fila.items()} for fila in filas
            ]

        crudo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        with gzip.open(archivo, "wb") as f:
            f.write(crudo)

        con_pg_dump = _pg_dump_esquema(url, archivo_esquema)

        resumen = {t: len(v) for t, v in datos["tablas"].items()}
        total = sum(resumen.values())

        try:
            _registrar(conn, archivo, len(resumen), total,
                       "pg_dump" if con_pg_dump else "catalogo")
        except Exception as e:
            print(f"    ⚠️ El archivo se guardó, pero NO pude registrarlo en la "
                  f"tabla 'backups' ({type(e).__name__}: {e}).\n"
                  f"       Lucy va a avisar que no hay respaldo aunque sí lo haya. "
                  f"¿Corriste db/migrations/2026-08-30_esquema_al_dia_y_latido_de_backup.sql?",
                  file=sys.stderr)

    print(f"OK: {archivo}")
    print(f"    {len(resumen)} tablas, {total} filas, "
          f"{archivo.stat().st_size / 1024:.0f} KB comprimido")
    if con_pg_dump:
        print(f"    esquema: {archivo_esquema.name} "
              f"({archivo_esquema.stat().st_size / 1024:.0f} KB) + catálogo en el JSON")
    else:
        print("    esquema: solo el catálogo adentro del JSON (sin pg_dump)")
    for t, n in resumen.items():
        print(f"      {t:16} {n}")

    deriva = _avisar_deriva(datos["esquema"])
    if deriva:
        print("\n    ⚠️ db/schema.sql NO describe la base real:", file=sys.stderr)
        for p in deriva:
            print(f"       · {p}", file=sys.stderr)
        print("       Reconciliá el esquema: una base que se instala rota con el\n"
              "       propio archivo del repo es peor que no tener esquema versionado.",
              file=sys.stderr)

    _rotar(destino)
    return archivo


def _rotar(destino: Path, conservar: int = CONSERVAR) -> None:
    """Deja solo los últimos N backups: una copia infinita llena el Drive.

    El `.schema.sql.gz` se va con SU json: separarlos dejaría esquemas huérfanos
    de datos que ya no están, o —peor— datos viejos junto a un esquema nuevo,
    que es la forma más silenciosa de que una restauración salga mal.
    """
    copias = sorted(destino.glob("lucy_*.json.gz"))
    for viejo in copias[:-conservar]:
        viejo.unlink()
        print(f"    (rotación: borré {viejo.name})")
        hermano = viejo.with_name(viejo.name[:-len(".json.gz")] + ".schema.sql.gz")
        if hermano.exists():
            hermano.unlink()
            print(f"    (rotación: borré {hermano.name})")


if __name__ == "__main__":
    try:
        hacer_backup()
    except Exception as e:
        print(f"FALLO EL BACKUP: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
