"""Arma el ensayo de la migración `db/migrations/2026-10-14_cotizaciones.sql` para correrlo con `psql` contra
la base REAL sin dejar nada. NO se conecta a ninguna base: solo imprime SQL (por eso no hay credenciales ni
direcciones aquí, y el repo es PÚBLICO).

Dos modos:

  --cuenta  (por omisión) SQL de SOLO LECTURA: dice si `cotizaciones` y `serie_cotizaciones` existen hoy
            y, si existen, cuántas filas tienen.
  --ensayo  El texto de la migración SIN su `COMMIT;` final (en memoria; el archivo no se toca), seguido de
            las comprobaciones de lo que tiene que dar el ensayo (cabecera de la migración) y de un
            `ROLLBACK;`. Cada comprobación sale como una fila con `ok` en `t`/`f`. Al final, otra fila
            por tabla comprueba que no quedó nada (`to_regclass` NULL). Un `lock_timeout` de la sesión
            evita que el ensayo se quede esperando detrás de una consulta larga por el candado de
            `proyectos` (la llave foránea lo pide, como lo pediría la migración de verdad).

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy` (o el árbol del trabajo), primero SOLO CUENTA:

    python3 tools/ensayo_cotizaciones.py --cuenta | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

y después el ensayo:

    python3 tools/ensayo_cotizaciones.py --ensayo | railway run -s Postgres -- sh -c 'psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=1 -X -f -'

Si `psql` falla a medias, la conexión se cierra con la transacción abierta y Postgres la deshace sola.

FRONTERA: este guion NO se corrió contra Postgres al escribirlo (no hay Postgres en la máquina de
construcción). Se probó el texto que arma (`tests/test_cotizaciones_tablas.py`); que `psql` lea el
SQL por la entrada estándar a través de `railway run` y que las consultas a `information_schema` y
`pg_constraint` den lo que se espera no se ejerció aquí.
"""
from __future__ import annotations

import pathlib
import re
import sys

MIGRACION = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "2026-10-14_cotizaciones.sql"


def _sin_comentarios(texto: str) -> str:
    return "\n".join(l.split("--")[0] for l in texto.splitlines())


def tablas_de(texto: str) -> list[str]:
    """Las tablas que la migración crea, sacadas de su texto (no tecleadas acá)."""
    return re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", _sin_comentarios(texto), re.I)


def restricciones_de(texto: str) -> list[str]:
    """Los nombres de las restricciones (`CONSTRAINT <nombre>`) que declara la migración."""
    return re.findall(r"CONSTRAINT\s+(\w+)", _sin_comentarios(texto), re.I)


def texto_del_ensayo(texto: str) -> str:
    """El texto de la migración SIN su `COMMIT;` final: lo demás, idéntico. Si el texto no termina en
    UN `COMMIT;` propio, no hay forma segura de ensayarlo y se rechaza."""
    lineas = texto.rstrip().splitlines()
    commits = [i for i, l in enumerate(lineas) if l.strip().upper() == "COMMIT;"]
    if len(commits) != 1 or commits[0] != len(lineas) - 1:
        raise ValueError("la migración tiene que terminar en un único COMMIT;")
    return "\n".join(lineas[:-1]).rstrip() + "\n"


def sql_de_la_cuenta(texto: str) -> str:
    """SQL de solo lectura: existencia y filas de cada tabla de la migración."""
    sal = ["BEGIN READ ONLY;"]
    for t in tablas_de(texto):
        sal.append(f"SELECT '{t}' AS tabla, to_regclass('public.{t}') IS NOT NULL AS existe;")
    for t in tablas_de(texto):
        # `\gexec` corre el SELECT que arma la consulta, y solo si la tabla existe.
        sal.append(f"SELECT format('SELECT %L AS tabla, count(*) AS filas FROM %I', '{t}', '{t}') "
                   f"WHERE to_regclass('public.{t}') IS NOT NULL \\gexec")
    sal.append("ROLLBACK;")
    return "\n".join(sal) + "\n"


def sql_del_ensayo(texto: str) -> str:
    """El ensayo entero: la migración sin COMMIT, las comprobaciones, el ROLLBACK y la comprobación final
    de que no quedó nada."""
    tablas, restricciones = tablas_de(texto), restricciones_de(texto)
    lista_tablas = ", ".join(f"'{t}'" for t in tablas)
    lista_restr = ", ".join(f"'{r}'" for r in dict.fromkeys(restricciones))
    regclass = ", ".join(f"to_regclass('public.{t}')" for t in tablas)
    comprobaciones = [
        f"SELECT '{t}: filas tras el CREATE' AS que, count(*) AS dio, 0 AS esperado, count(*) = 0 AS ok FROM {t};"
        for t in tablas]
    comprobaciones.append(
        "SELECT 'information_schema.tables' AS que, count(*) AS dio, "
        f"{len(tablas)} AS esperado, count(*) = {len(tablas)} AS ok FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name IN ({lista_tablas});")
    n = len(set(restricciones))
    comprobaciones.append(
        f"SELECT 'restricciones con nombre' AS que, count(*) AS dio, {n} AS esperado, count(*) = {n} AS ok "
        f"FROM pg_constraint WHERE conname IN ({lista_restr}) AND conrelid IN ({regclass});")
    final = [f"SELECT '{t}: tras el ROLLBACK' AS que, "
             f"CASE WHEN to_regclass('public.{t}') IS NULL THEN 'no quedó' ELSE 'QUEDÓ' END AS dio, "
             f"'no quedó' AS esperado, to_regclass('public.{t}') IS NULL AS ok;" for t in tablas]
    return ("\\set ON_ERROR_STOP on\n"
            "SET lock_timeout = '5s';\n"
            + texto_del_ensayo(texto)
            + "\n".join(comprobaciones) + "\nROLLBACK;\n"
            + "\n".join(final) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or any(a not in ("--cuenta", "--ensayo") for a in args):
        print("Uso: ensayo_cotizaciones.py [--cuenta | --ensayo]", file=sys.stderr)
        return 2
    texto = MIGRACION.read_text(encoding="utf-8")
    sys.stdout.write(sql_del_ensayo(texto) if args == ["--ensayo"] else sql_de_la_cuenta(texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
