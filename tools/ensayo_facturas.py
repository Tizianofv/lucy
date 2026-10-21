"""Arma el ensayo de la migración `db/migrations/2026-10-20_facturas.sql` para correrlo con `psql` contra la
base REAL sin dejar nada. NO se conecta a ninguna base: solo imprime SQL (por eso no hay credenciales ni
direcciones aquí, y el repo es PÚBLICO).

Es el mismo guion de `tools/ensayo_cotizaciones.py` (de ahí salen `tablas_de`, `restricciones_de`,
`texto_del_ensayo`, `sql_de_la_cuenta` y `sql_del_ensayo`, que reciben el texto de la migración) apuntado a la
migración de las facturas. Dos modos:

  --cuenta  (por omisión) SQL de SOLO LECTURA: dice si las cinco tablas existen hoy y, si existen, cuántas filas
            tienen.
  --ensayo  El texto de la migración SIN su `COMMIT;` final (en memoria; el archivo no se toca), seguido de
            las comprobaciones de lo que tiene que dar el ensayo (cabecera de la migración) y de un
            `ROLLBACK;`. Cada comprobación sale como una fila con `ok` en `t`/`f`.

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy` (o el árbol del trabajo), primero SOLO CUENTA:

    python3 tools/ensayo_facturas.py --cuenta | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

y después el ensayo:

    python3 tools/ensayo_facturas.py --ensayo | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

FRONTERA: este guion NO se corrió contra Postgres al escribirlo (no hay Postgres en la máquina de
construcción). Se probó el texto que arma (`tests/test_facturas_tablas.py`).
"""
from __future__ import annotations

import pathlib
import sys

# Como `tools/humo.py`: la raíz del repo va primero en `sys.path` y el guion hermano se importa con su ruta de
# paquete (`tools.ensayo_cotizaciones`), corra este archivo como guion o lo cargue otro por su ruta.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools.ensayo_cotizaciones import (restricciones_de, sql_de_la_cuenta, sql_del_ensayo,  # noqa: E402,F401
                                       tablas_de, texto_del_ensayo)

MIGRACION = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "2026-10-20_facturas.sql"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or any(a not in ("--cuenta", "--ensayo") for a in args):
        print("Uso: ensayo_facturas.py [--cuenta | --ensayo]", file=sys.stderr)
        return 2
    texto = MIGRACION.read_text(encoding="utf-8")
    sys.stdout.write(sql_del_ensayo(texto) if args == ["--ensayo"] else sql_de_la_cuenta(texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
