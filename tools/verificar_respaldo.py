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
  3. Que las filas cuadren, contando solo las que YA EXISTÍAN cuando se tomó la
     copia (por `creado_en`). Así la comparación es exacta y no hace falta
     tolerancia: lo que había, tiene que estar. Las pocas tablas sin `creado_en`
     no se pueden acotar por fecha y ahí queda una tolerancia aproximada.
  4. Que el dinero esté entero: los montos como texto decimal y no como float,
     y que sumen EXACTO lo mismo que en la base — por moneda separada, y solo
     sobre las filas que ya existían cuando se tomó la copia. Sin tolerancia:
     acotando por fecha de creación no hace falta ninguna, y una tolerancia
     porcentual sobre un total que crece salta por lo normal.
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

        # 3. Las filas cuadran. Mismo principio que el dinero: se cuentan solo
        # las que YA EXISTÍAN cuando se tomó la copia, usando `creado_en`. Con
        # un simple "respaldo vs base" la alarma saltaba por lo normal —
        # Tiziano clasificó cuatro comercios después del respaldo y
        # `categorias_aprendidas` dio 24 vs 28—, y una alarma que salta por lo
        # normal se aprende a ignorar. Las tablas SIN `creado_en` no se pueden
        # acotar por fecha, así que ahí sí queda la tolerancia de antes.
        con_fecha = {t for (t,) in conn.execute(
            "SELECT DISTINCT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'creado_en'")}
        cortas = []
        for t in reales:
            if t not in tablas:
                continue
            if t in con_fecha:
                n_real = conn.execute(
                    f'SELECT count(*) FROM "{t}" WHERE creado_en <= %s',
                    (generado,)).fetchone()[0]
                n_copia = sum(1 for f in tablas[t]
                              if f.get("creado_en")
                              and str(f["creado_en"]) <= generado)
                if n_copia != n_real:      # exacto: lo que había tiene que estar
                    cortas.append(f"{t}: respaldo {n_copia} vs base {n_real}")
            else:
                n_real = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                n_copia = len(tablas[t])
                if n_copia < n_real - max(3, int(n_real * 0.1)):
                    cortas.append(f"{t}: respaldo {n_copia} vs base {n_real} "
                                  "(sin creado_en, comparación aproximada)")
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

        # El dinero se compara SOLO contra las filas que ya existían cuando se
        # tomó la copia, y SEPARADO POR MONEDA. Las dos cosas son correcciones
        # de algo que escribí mal hace unas horas:
        #
        #   · Comparar el total del respaldo contra el total de la base es
        #     comparar contra un blanco móvil: la base crece, y con un 10% de
        #     tolerancia bastaba que entrara un 11% de movimientos nuevos para
        #     que gritara "este respaldo NO es de fiar" sobre uno sano. Una
        #     alarma que salta por lo normal se aprende a ignorar. Acotando por
        #     `creado_en <= generado` la comparación es EXACTA y no hace falta
        #     ninguna tolerancia: lo que había, tiene que estar.
        #   · Sumaba DOP con USD en una sola cifra — el error que este proyecto
        #     entero existe para no cometer, cometido en la herramienta que
        #     vigila que el dinero esté entero.
        #
        # No se filtra por borrado_en: una fila borrada DESPUÉS del respaldo
        # sigue estando en él, y tiene que seguir estando.
        por_moneda_copia: dict = {}
        for m in movs:
            if m.get("monto") is None or not m.get("creado_en"):
                continue
            if str(m["creado_en"]) > generado:
                continue                     # entró después: no le toca estar
            por_moneda_copia[m.get("moneda") or "?"] = (
                por_moneda_copia.get(m.get("moneda") or "?", Decimal(0))
                + Decimal(str(m["monto"])))

        cur = conn.execute(
            "SELECT moneda, sum(monto) FROM movimientos "
            "WHERE creado_en <= %s GROUP BY 1 ORDER BY 1", (generado,))
        descuadres = []
        for moneda, suma_real_m in cur.fetchall():
            suma_copia_m = por_moneda_copia.get(moneda, Decimal(0))
            if suma_copia_m != suma_real_m:
                descuadres.append(
                    f"{moneda}: respaldo {suma_copia_m:,.2f} vs base "
                    f"{suma_real_m:,.2f} (faltan {suma_real_m - suma_copia_m:,.2f})")
        if descuadres:
            problemas.append("el dinero no cuadra — " + "; ".join(descuadres))
        else:
            cuadre = " · ".join(f"{mo} {v:,.2f}"
                                for mo, v in sorted(por_moneda_copia.items()))
            print(f"  ✓ el dinero cuadra EXACTO por moneda: {cuadre}")

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
