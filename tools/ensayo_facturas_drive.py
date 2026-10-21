"""Arma el ensayo de la migración `db/migrations/2026-10-21_facturas_drive.sql` (el Drive de las facturas, pieza 8) para
correrlo con `psql` contra la base REAL sin dejar nada. NO se conecta a ninguna base: solo imprime SQL (por eso no hay
credenciales ni direcciones aquí, y el repo es PÚBLICO).

Es el hermano de `tools/ensayo_facturas.py`, pero esa migración CREA tablas y esta AGREGA columnas, así que no sirve la cuenta
de tablas de `tools/ensayo_cotizaciones.py`: aquí lo que se cuenta y se comprueba son las columnas. De ese guion sale solo
`texto_del_ensayo` (la migración sin su `COMMIT;` final, en memoria; el archivo no se toca). Dos modos:

  --cuenta  (por omisión) SQL de SOLO LECTURA: dice cuáles de las cinco columnas y de la tabla existen hoy y cuántas filas
            tienen `facturas` y `drive_de_proyectos`.
  --ensayo  El texto de la migración SIN su `COMMIT;`, seguido de las comprobaciones de lo que tiene que dar (cabecera de la
            migración) y de un `ROLLBACK;`, y una comprobación final de que no quedó nada. Cada comprobación sale como una
            fila con `ok` en `t`/`f`.

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy` (o el árbol del trabajo), primero SOLO CUENTA:

    python3 tools/ensayo_facturas_drive.py --cuenta | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

y después el ensayo:

    python3 tools/ensayo_facturas_drive.py --ensayo | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

FRONTERA: este guion NO se corrió contra Postgres al escribirlo (no hay Postgres en la máquina de construcción). Se probó el
texto que arma (`tests/test_facturas_drive.py`).
"""
from __future__ import annotations

import pathlib
import re
import sys

# Como `tools/ensayo_facturas.py`: la raíz del repo va primero en `sys.path`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools.ensayo_cotizaciones import _sin_comentarios, texto_del_ensayo  # noqa: E402,F401

MIGRACION = (pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "2026-10-21_facturas_drive.sql")


def columnas_de(texto: str) -> list[tuple[str, str, str]]:
    """`(tabla, columna, tipo)` de cada `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, sacadas del texto (no tecleadas acá)."""
    return [(t.lower(), c.lower(), k.lower()) for t, c, k in re.findall(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(\w+)", _sin_comentarios(texto), re.I)]


def tablas_nuevas_de(texto: str) -> list[str]:
    return re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", _sin_comentarios(texto), re.I)


def _lista(valores) -> str:
    return ", ".join(f"'{v}'" for v in valores)


def sql_de_la_cuenta(texto: str) -> str:
    """SQL de solo lectura: qué columnas y tablas de la migración existen hoy y cuántas filas hay donde se agregan."""
    columnas, nuevas = columnas_de(texto), tablas_nuevas_de(texto)
    tablas = list(dict.fromkeys(t for t, _, _ in columnas))
    sal = ["BEGIN READ ONLY;",
           "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
           f"WHERE table_schema = 'public' AND (table_name, column_name) IN ({', '.join(f'({chr(39)}{t}{chr(39)}, {chr(39)}{c}{chr(39)})' for t, c, _ in columnas)}) "
           "ORDER BY table_name, column_name;"]
    for t in nuevas:
        sal.append(f"SELECT '{t}' AS tabla, to_regclass('public.{t}') IS NOT NULL AS existe;")
    for t in tablas:
        sal.append(f"SELECT '{t}' AS tabla, count(*) AS filas FROM {t};")
    sal.append("ROLLBACK;")
    return "\n".join(sal) + "\n"


def sql_del_ensayo(texto: str) -> str:
    """El ensayo entero: la migración sin COMMIT, las comprobaciones, el ROLLBACK y la comprobación final de que no quedó nada."""
    columnas, nuevas = columnas_de(texto), tablas_nuevas_de(texto)
    tablas = list(dict.fromkeys(t for t, _, _ in columnas))
    antes = [f"SELECT count(*) AS n_{t} FROM {t} \\gset antes_" for t in tablas]
    pares = ", ".join(f"('{t}', '{c}')" for t, c, _ in columnas)
    comprobaciones = [
        f"SELECT '{t}: filas igual que antes' AS que, count(*) AS dio, :antes_n_{t} AS esperado, "
        f"count(*) = :antes_n_{t} AS ok FROM {t};" for t in tablas]
    comprobaciones.append(
        "SELECT 'columnas agregadas, todas text/bigint, NULL y sin valor por omisión' AS que, count(*) AS dio, "
        f"{len(columnas)} AS esperado, count(*) = {len(columnas)} AS ok FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND (table_name, column_name) IN ({pares}) AND is_nullable = 'YES' "
        "AND column_default IS NULL AND data_type IN ('text', 'bigint');")
    comprobaciones += [f"SELECT '{t}: filas tras el CREATE' AS que, count(*) AS dio, 0 AS esperado, count(*) = 0 AS ok FROM {t};"
                       for t in nuevas]
    final = [f"SELECT '{t}.{c}: tras el ROLLBACK' AS que, "
             f"CASE WHEN count(*) = 0 THEN 'no quedó' ELSE 'QUEDÓ' END AS dio, 'no quedó' AS esperado, count(*) = 0 AS ok "
             f"FROM information_schema.columns WHERE table_schema = 'public' AND table_name = '{t}' AND column_name = '{c}';"
             for t, c, _ in columnas]
    final += [f"SELECT '{t}: tras el ROLLBACK' AS que, "
              f"CASE WHEN to_regclass('public.{t}') IS NULL THEN 'no quedó' ELSE 'QUEDÓ' END AS dio, "
              f"'no quedó' AS esperado, to_regclass('public.{t}') IS NULL AS ok;" for t in nuevas]
    return ("\\set ON_ERROR_STOP on\n"
            "SET lock_timeout = '5s';\n"
            "BEGIN;\n" + "\n".join(antes) + "\nROLLBACK;\n"
            + texto_del_ensayo(texto)
            + "\n".join(comprobaciones) + "\nROLLBACK;\n"
            + "\n".join(final) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or any(a not in ("--cuenta", "--ensayo") for a in args):
        print("Uso: ensayo_facturas_drive.py [--cuenta | --ensayo]", file=sys.stderr)
        return 2
    texto = MIGRACION.read_text(encoding="utf-8")
    sys.stdout.write(sql_del_ensayo(texto) if args == ["--ensayo"] else sql_de_la_cuenta(texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
