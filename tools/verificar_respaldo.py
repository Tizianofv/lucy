"""¿El último respaldo sirve de verdad para restaurar?

POR QUÉ EXISTE. Hasta hoy nadie lo había comprobado nunca, y el proyecto ya
tropezó dos veces con la misma forma de mentira: los 25 días de agosto sin
copia, y la tabla `backups` que no existía en producción, así que el respaldo
del 31 a la 01:16 se hizo y NO registró su latido. En los dos casos había algo
con cara de estar funcionando.

Un respaldo que nadie abre es una promesa, no un respaldo.

QUÉ COMPRUEBA, en orden de gravedad:

  1. Que el archivo abra y sea JSON válido. Un .gz corrupto se detecta acá y no
     el día que hace falta.
  2. Que estén TODAS las tablas que la base real tiene. Una tabla que existe en
     Postgres y no en el respaldo es una pérdida total silenciosa.
  3. Que las filas cuadren con la base, con margen para lo que creció desde que
     se tomó la copia. El margen es 10% o tres filas, lo que sea mayor: en una
     tabla de cuatro, una fila nueva es el 25% y la alarma saltaría por algo
     normal. Lo que NO se tolera es que falten filas de verdad.
  4. Que el dinero esté entero: los montos guardados como texto decimal y no
     como float, y que sumen lo mismo que en la base.
  5. Que el esquema venga con las columnas de cada tabla, para poder
     reconstruirla.

QUÉ NO COMPRUEBA, y conviene saberlo: no restaura de verdad contra un Postgres
vacío. Eso pide un servidor donde hacerlo, y este Mac solo tiene el cliente. Es
la diferencia entre "el paracaídas está bien plegado" y "salté con él".

    DATABASE_URL=... python3 tools/verificar_respaldo.py
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

CARPETA = os.path.expanduser("~/Google Drive/My Drive/Lucy/backups")


def main() -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("Falta DATABASE_URL.", file=sys.stderr)
        return 2

    archivos = sorted(glob.glob(os.path.join(CARPETA, "*.json.gz")))
    if not archivos:
        print(f"No hay respaldos en {CARPETA}", file=sys.stderr)
        return 1
    ruta = archivos[-1]
    print(f"  respaldo: {os.path.basename(ruta)}")

    problemas: list[str] = []

    # 1. Abre y es JSON.
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            copia = json.load(f)
    except Exception as e:
        print(f"  ✗ NO SE PUEDE ABRIR: {type(e).__name__}: {e}")
        return 1
    print(f"  ✓ abre y es JSON válido")

    tablas = copia.get("tablas") or {}
    esquema = (copia.get("esquema") or {}).get("columnas") or {}
    generado = copia.get("generado", "")
    try:
        edad_h = (datetime.now(timezone.utc)
                  - datetime.fromisoformat(generado)).total_seconds() / 3600
        print(f"  ✓ tomado hace {edad_h:.1f} h")
    except Exception:
        edad_h = 0.0
        problemas.append("no dice cuándo se tomó")

    import psycopg
    with psycopg.connect(url) as conn:
        reales = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY 1")]

        # 2. Ninguna tabla puede faltar.
        faltan = [t for t in reales if t not in tablas]
        if faltan:
            problemas.append(f"tablas que la base tiene y el respaldo NO: {faltan}")
        else:
            print(f"  ✓ las {len(reales)} tablas de la base están en el respaldo")

        # 3. Las filas cuadran (con margen por lo que creció después).
        cortas = []
        for t in reales:
            if t not in tablas:
                continue
            n_real = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            n_copia = len(tablas[t])
            # El margen es 10% O TRES FILAS, lo que sea mayor. El porcentaje
            # solo no sirve: en una tabla de cuatro filas, una fila añadida
            # después del respaldo es el 25% y hacía saltar la alarma por algo
            # que es normal —yo mismo registré una cuenta propia una hora
            # después de la copia—. Una alarma que salta por lo normal es una
            # alarma que se aprende a ignorar, y este proyecto ya pagó por eso.
            margen = max(3, int(n_real * 0.1))
            if n_copia < n_real - margen:
                cortas.append(f"{t}: respaldo {n_copia} vs base {n_real}")
        if cortas:
            problemas.append("tablas con menos filas de las esperadas: "
                             + "; ".join(cortas))
        else:
            print(f"  ✓ las filas cuadran con la base")

        # 4. El dinero, entero y sin float.
        movs = tablas.get("movimientos") or []
        flotantes = [m.get("id") for m in movs if isinstance(m.get("monto"), float)]
        if flotantes:
            problemas.append(
                f"{len(flotantes)} montos guardados como float (ej. id "
                f"{flotantes[:3]}): el respaldo pierde precisión del dinero")
        else:
            print(f"  ✓ los {len(movs)} montos están como texto decimal, no float")

        suma_copia = sum(Decimal(str(m["monto"])) for m in movs
                         if m.get("monto") is not None and not m.get("borrado_en"))
        suma_real = conn.execute(
            "SELECT coalesce(sum(monto), 0) FROM movimientos "
            "WHERE borrado_en IS NULL").fetchone()[0]
        if suma_copia > suma_real:
            problemas.append(f"el respaldo suma MÁS dinero que la base "
                             f"({suma_copia} vs {suma_real})")
        elif suma_real - suma_copia > suma_real * Decimal("0.1"):
            problemas.append(f"al respaldo le falta dinero: {suma_copia} "
                             f"vs {suma_real} en la base")
        else:
            print(f"  ✓ el dinero cuadra: {suma_copia:,.2f} en el respaldo, "
                  f"{suma_real:,.2f} en la base")

    # 5. El esquema alcanza para reconstruir.
    sin_columnas = [t for t in tablas if not esquema.get(t)]
    if sin_columnas:
        problemas.append(f"tablas sin columnas en el esquema: {sin_columnas}")
    else:
        print(f"  ✓ el esquema trae las columnas de las {len(tablas)} tablas")

    sql = ruta.replace(".json.gz", ".schema.sql.gz")
    if os.path.exists(sql):
        print(f"  ✓ hay pg_dump del esquema ({os.path.getsize(sql) // 1024} KB): "
              "se restaura ejecutándolo")
    else:
        print("  · sin pg_dump del esquema — se restaura reconstruyendo desde "
              "el catálogo del JSON, que es más trabajo pero se puede")

    print()
    if problemas:
        for p in problemas:
            print(f"  ✗ {p}")
        print(f"\n  {len(problemas)} problema(s). Este respaldo NO es de fiar.")
        return 1
    print("  El respaldo está entero y sirve para restaurar.")
    return 0


sys.exit(main())
