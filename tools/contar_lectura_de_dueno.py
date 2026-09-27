"""Cuenta, contra la base REAL, cuántas filas trae cada lectura de la
parte A (`db/lectura_dueno.py`, §A.1-A.3, 27-sep-2026) y cuántas deja
fuera por ser de otro.

SOLO CONTEOS, NUNCA CONTENIDO: no imprime `contenido_raw`, `contraparte`,
`titulo` ni ningún otro texto de una fila -- solo números. El repo es
PÚBLICO y este script puede correr pegado en una terminal compartida.

Corre de SOLO LECTURA: usa el mismo literal `SET TRANSACTION READ ONLY`
que `db/lectura_dueno.py`, en su propia conexión corta (no toca el pool
async de la app).

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy`:

    railway run -s Postgres -- python3 \\
      "/Users/controlroom/Documents/IA CDS/lucy-trabajos/code-lectura/tools/contar_lectura_de_dueno.py"

(usa `DATABASE_URL`/`DATABASE_PUBLIC_URL` que Railway inyecta; si el
entorno ya trae `DATABASE_URL` puesta a mano, sirve igual con
`python3 tools/contar_lectura_de_dueno.py` desde este árbol).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import por el EFECTO: deja `prepare_threshold=None` puesto para el
# proceso entero (ver db/sin_preparadas.py).
import db.sin_preparadas  # noqa: E402,F401

import psycopg  # noqa: E402

from config import CHAT_ID_DUENO  # noqa: E402
from db.lectura_dueno import _condicion_de_dueno  # noqa: E402

# (tabla, filtro adicional adentro del WHERE adicional a `borrado_en`)
_TABLAS = ("bandeja", "notas", "movimientos", "eventos")
_TIENE_BORRADO_EN = {"bandeja": False, "notas": True, "movimientos": True,
                     "eventos": True}


def main() -> int:
    url = (os.environ.get("DATABASE_PUBLIC_URL")
           or os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        print("Falta DATABASE_URL (o DATABASE_PUBLIC_URL).", file=sys.stderr)
        return 2

    with psycopg.connect(url) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            print(f"{'tabla':<12} {'total':>8} {'de Tiziano':>12} {'de otro':>10}")
            print("-" * 46)
            for tabla in _TABLAS:
                borrado = " AND borrado_en IS NULL" if _TIENE_BORRADO_EN[tabla] else ""
                condicion = _condicion_de_dueno(tabla)

                total = conn.execute(
                    f"SELECT count(*) FROM {tabla} WHERE true{borrado}"
                ).fetchone()[0]
                de_dueno = conn.execute(
                    f"SELECT count(*) FROM {tabla} WHERE {condicion}{borrado}",
                    (CHAT_ID_DUENO,)
                ).fetchone()[0]
                de_otro = total - de_dueno
                print(f"{tabla:<12} {total:>8} {de_dueno:>12} {de_otro:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
