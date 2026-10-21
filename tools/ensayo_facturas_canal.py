"""Arma el ensayo de la migración `db/migrations/2026-10-21_facturas_canal.sql` (el cambio de canal de las facturas,
pieza 9b: la columna `viene_de_envio_id` de `factura_envios`) para correrlo con `psql` contra la base REAL sin dejar nada.
NO se conecta a ninguna base: solo imprime SQL (por eso no hay credenciales ni direcciones aquí, y el repo es PÚBLICO).

Es el hermano de `tools/ensayo_facturas_envios.py` (el de la pieza 9) y reusa de él lo mismo que él reusa de
`tools/ensayo_facturas_drive.py`: sacar las columnas del texto de la migración y la cuenta de solo lectura. Lo que cambia
es la comprobación del tipo: aquí la única columna es `bigint`. Dos modos:

  --cuenta  (por omisión) SQL de SOLO LECTURA: dice si la columna existe hoy y cuántas filas tiene `factura_envios`.
  --ensayo  El texto de la migración SIN su `COMMIT;`, seguido de las comprobaciones de lo que tiene que dar (cabecera de
            la migración) y de un `ROLLBACK;`, y una comprobación final de que no quedó nada. Cada comprobación sale como
            una fila con `ok` en `t`/`f`.

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy` (o el árbol del trabajo), primero SOLO CUENTA:

    python3 tools/ensayo_facturas_canal.py --cuenta | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

y después el ensayo:

    python3 tools/ensayo_facturas_canal.py --ensayo | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

FRONTERA: este guion NO se corrió contra Postgres al escribirlo (no hay Postgres en la máquina de construcción). Se probó
el texto que arma (`tests/test_facturas_natalia.py`).
"""
from __future__ import annotations

import pathlib
import sys

# Como `tools/ensayo_facturas_envios.py`: la raíz del repo va primero en `sys.path`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools.ensayo_cotizaciones import texto_del_ensayo  # noqa: E402
from tools.ensayo_facturas_drive import columnas_de, sql_de_la_cuenta  # noqa: E402

MIGRACION = (pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "2026-10-21_facturas_canal.sql")

# Cómo `information_schema.columns.data_type` llama a cada tipo que la migración escribe.
_TIPO_EN_POSTGRES = {"bigint": "bigint"}


def sql_del_ensayo(texto: str) -> str:
    """El ensayo entero: la migración sin COMMIT, las comprobaciones, el ROLLBACK y la comprobación final de que no quedó nada."""
    columnas = columnas_de(texto)
    tablas = list(dict.fromkeys(t for t, _, _ in columnas))
    antes = [f"SELECT count(*) AS n_{t} FROM {t} \\gset antes_" for t in tablas]
    comprobaciones = [
        f"SELECT '{t}: filas igual que antes' AS que, count(*) AS dio, :antes_n_{t} AS esperado, "
        f"count(*) = :antes_n_{t} AS ok FROM {t};" for t in tablas]
    for t, c, tipo in columnas:
        comprobaciones.append(
            f"SELECT '{t}.{c}: {_TIPO_EN_POSTGRES[tipo]}, NULL y sin valor por omisión' AS que, count(*) AS dio, 1 AS esperado, "
            f"count(*) = 1 AS ok FROM information_schema.columns WHERE table_schema = 'public' AND table_name = '{t}' "
            f"AND column_name = '{c}' AND is_nullable = 'YES' AND column_default IS NULL "
            f"AND data_type = '{_TIPO_EN_POSTGRES[tipo]}';")
    final = [f"SELECT '{t}.{c}: tras el ROLLBACK' AS que, "
             f"CASE WHEN count(*) = 0 THEN 'no quedó' ELSE 'QUEDÓ' END AS dio, 'no quedó' AS esperado, count(*) = 0 AS ok "
             f"FROM information_schema.columns WHERE table_schema = 'public' AND table_name = '{t}' AND column_name = '{c}';"
             for t, c, _ in columnas]
    return ("\\set ON_ERROR_STOP on\n"
            "SET lock_timeout = '5s';\n"
            "BEGIN;\n" + "\n".join(antes) + "\nROLLBACK;\n"
            + texto_del_ensayo(texto)
            + "\n".join(comprobaciones) + "\nROLLBACK;\n"
            + "\n".join(final) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or any(a not in ("--cuenta", "--ensayo") for a in args):
        print("Uso: ensayo_facturas_canal.py [--cuenta | --ensayo]", file=sys.stderr)
        return 2
    texto = MIGRACION.read_text(encoding="utf-8")
    sys.stdout.write(sql_del_ensayo(texto) if args == ["--ensayo"] else sql_de_la_cuenta(texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
