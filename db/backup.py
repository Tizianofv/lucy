"""Copia de seguridad de la base de Lucy (pilar #40).

Vuelca cada tabla del esquema public a un JSON comprimido, con marca de tiempo,
en la carpeta de backups. Restaurar = crear las tablas con db/schema.sql y
recargar las filas de este archivo.

Se eligió un dump propio en Python, y no pg_dump, por dos razones: no depende
de tener instalado el cliente de PostgreSQL (ni de que su versión coincida con
la del servidor), y el formato JSON es legible y portable — un backup que solo
se puede restaurar con la herramienta exacta que lo creó es medio backup.

La carpeta destino vive en Google Drive: así la copia queda FUERA de Railway.
De nada sirve respaldar la base en el mismo lugar que podría caerse con ella.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# En Windows, psycopg async necesita otra política; el backup es sincrónico,
# así que no aplica. Se deja el import de psycopg sincrónico a propósito.

DESTINO = Path(r"G:\My Drive\Lucy\backups")


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


def hacer_backup() -> Path:
    DESTINO.mkdir(parents=True, exist_ok=True)
    sello = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archivo = DESTINO / f"lucy_{sello}.json.gz"

    datos: dict = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "tablas": {},
    }

    with psycopg.connect(_url(), autocommit=True, row_factory=dict_row) as conn:
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

    resumen = {t: len(v) for t, v in datos["tablas"].items()}
    total = sum(resumen.values())
    print(f"OK: {archivo}")
    print(f"    {len(resumen)} tablas, {total} filas, "
          f"{archivo.stat().st_size / 1024:.0f} KB comprimido")
    for t, n in resumen.items():
        print(f"      {t:16} {n}")

    _rotar()
    return archivo


def _rotar(conservar: int = 30) -> None:
    """Deja solo los últimos N backups: una copia infinita llena el Drive."""
    copias = sorted(DESTINO.glob("lucy_*.json.gz"))
    for viejo in copias[:-conservar]:
        viejo.unlink()
        print(f"    (rotación: borré {viejo.name})")


if __name__ == "__main__":
    try:
        hacer_backup()
    except Exception as e:
        print(f"FALLO EL BACKUP: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
