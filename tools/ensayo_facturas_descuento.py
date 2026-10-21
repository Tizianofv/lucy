"""Arma el ensayo de la migración `db/migrations/2026-10-21_facturas_descuento.sql` (el descuento de una factura,
pieza D3) para correrlo con `psql` contra la base REAL sin dejar nada. NO se conecta a ninguna base: solo imprime SQL
(por eso no hay credenciales ni direcciones aquí, y el repo es PÚBLICO).

La columna no es como las de las otras piezas: `descuento` es `NOT NULL DEFAULT 0` y lleva un CHECK. La cuenta de
solo lectura sirve la de `tools/ensayo_facturas_drive.py` (las columnas del texto y las filas de `facturas`); el
ensayo es propio porque las comprobaciones son otras. Dos modos:

  --cuenta  (por omisión) SQL de SOLO LECTURA: dice si `facturas.descuento` existe hoy y cuántas filas tiene
            `facturas`.
  --ensayo  El texto de la migración SIN su `COMMIT;`, seguido de las comprobaciones de lo que tiene que dar (cabecera
            de la migración) y de un `ROLLBACK;`, y una comprobación final de que no quedó nada. Cada comprobación sale
            como una fila con `ok` en `t`/`f`: la columna es numeric, NO acepta NULL, su valor por omisión es 0, todas
            las facturas de hoy quedan en 0 (no se reescribe ninguna fila con otro valor) y la restricción del CHECK
            está puesta y dice que el descuento no es negativo.

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy` (o el árbol del trabajo), primero SOLO CUENTA:

    python3 tools/ensayo_facturas_descuento.py --cuenta | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

y después el ensayo:

    python3 tools/ensayo_facturas_descuento.py --ensayo | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

FRONTERA: este guion NO se corrió contra Postgres al escribirlo (no hay Postgres en la máquina de construcción). Se
probó el texto que arma (`tests/test_facturas_el_descuento.py`).
"""
from __future__ import annotations

import pathlib
import sys

# Como `tools/ensayo_facturas_canal.py`: la raíz del repo va primero en `sys.path`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools.ensayo_cotizaciones import texto_del_ensayo  # noqa: E402
from tools.ensayo_facturas_drive import sql_de_la_cuenta  # noqa: E402

MIGRACION = (pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "2026-10-21_facturas_descuento.sql")


# El CHECK se mira en el catálogo, sin escribir ninguna fila: la restricción de `facturas` que nombra `descuento` tiene
# que decir que no es negativo. (Que la base lo haga cumplir se ejerce en `tests/test_facturas_el_descuento.py` contra
# SQLite; aquí solo se comprueba que la migración la dejó puesta con esa condición.)
_LA_RESTRICCION = (
    "SELECT 'la restriccion del descuento dice que no es negativo' AS que, count(*) AS dio, 1 AS esperado, "
    "count(*) = 1 AS ok FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' AND t.relname = 'facturas' AND c.contype = 'c' "
    "AND pg_get_constraintdef(c.oid) LIKE '%descuento >= 0%';\n")


def sql_del_ensayo(texto: str) -> str:
    """El ensayo entero: la migración sin COMMIT, las comprobaciones, el ROLLBACK y la comprobación final de que no
    quedó nada."""
    vuelta = ("SELECT 'facturas.descuento: tras el ROLLBACK' AS que, "
              "CASE WHEN count(*) = 0 THEN 'no quedó' ELSE 'QUEDÓ' END AS dio, 'no quedó' AS esperado, "
              "count(*) = 0 AS ok FROM information_schema.columns "
              "WHERE table_schema = 'public' AND table_name = 'facturas' AND column_name = 'descuento';")
    return ("\\set ON_ERROR_STOP on\n"
            "SET lock_timeout = '5s';\n"
            "BEGIN;\n"
            "SELECT count(*) AS n_facturas FROM facturas \\gset antes_\n"
            + texto_del_ensayo(texto)
            + "SELECT 'facturas: filas igual que antes' AS que, count(*) AS dio, :antes_n_facturas AS esperado, "
              "count(*) = :antes_n_facturas AS ok FROM facturas;\n"
              "SELECT 'facturas.descuento: numeric, NOT NULL y con valor por omision 0' AS que, count(*) AS dio, "
              "1 AS esperado, count(*) = 1 AS ok FROM information_schema.columns "
              "WHERE table_schema = 'public' AND table_name = 'facturas' AND column_name = 'descuento' "
              "AND data_type = 'numeric' AND is_nullable = 'NO' AND column_default = '0';\n"
              "SELECT 'todas las facturas de hoy quedan en 0' AS que, count(*) AS dio, 0 AS esperado, "
              "count(*) = 0 AS ok FROM facturas WHERE descuento <> 0;\n"
            + _LA_RESTRICCION
            + "ROLLBACK;\n"
            + vuelta + "\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or any(a not in ("--cuenta", "--ensayo") for a in args):
        print("Uso: ensayo_facturas_descuento.py [--cuenta | --ensayo]", file=sys.stderr)
        return 2
    texto = MIGRACION.read_text(encoding="utf-8")
    sys.stdout.write(sql_del_ensayo(texto) if args == ["--ensayo"] else sql_de_la_cuenta(texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
