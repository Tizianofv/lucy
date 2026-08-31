"""Pone categoría a los movimientos que ya están en la base y no la tienen.

POR QUÉ EXISTE: la red de palabras clave se aplica cuando el movimiento ENTRA.
Los que ya entraron antes de que la red existiera se quedarían sin categoría
para siempre, y son justo los que llenan la cola del panel el primer día. Una
cola de 38 no se corrige; una de 7, sí.

NO PISA NADA. Solo toca filas con categoría nula o vacía: una corrección hecha
a mano vale más que cualquier adivinanza de la red, y este script no tiene
permiso para discutirle a Tiziano.

Todo queda en log_acciones con actor='relleno', así que se puede ver qué tocó y
deshacerlo. En seco por defecto:

    python3 tools/rellenar_categorias.py            # muestra y no toca
    python3 tools/rellenar_categorias.py --aplicar  # escribe
"""
from __future__ import annotations

import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.categorias import (  # noqa: E402
    CLAVES, Categorizador, normalizar_comercio)


def main() -> int:
    aplicar = "--aplicar" in sys.argv
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("Falta DATABASE_URL en el entorno.", file=sys.stderr)
        return 2

    with psycopg.connect(url) as conn:
        aprendidas = {r[0]: r[1] for r in conn.execute(
            "SELECT comercio, categoria FROM categorias_aprendidas "
            "WHERE borrado_en IS NULL")}
        cat = Categorizador(aprendidas, CLAVES)

        filas = conn.execute(
            "SELECT id, contraparte, monto, moneda FROM movimientos "
            "WHERE borrado_en IS NULL AND (categoria IS NULL OR categoria = '') "
            "ORDER BY monto DESC").fetchall()

        tocados, quedan = [], []
        for mid, cp, monto, moneda in filas:
            c = cat.categoria_de(cp or "")
            (tocados if c else quedan).append((mid, cp, monto, moneda, c))

        for _, cp, monto, moneda, c in tocados:
            print(f"  {monto:>10,.2f} {moneda}  {c:<26} {normalizar_comercio(cp or '')[:38]}")
        print(f"\n  {len(tocados)} con categoría · {len(quedan)} quedan para la cola")
        for _, cp, monto, moneda, _ in quedan:
            print(f"     cola: {monto:>9,.2f} {moneda}  {normalizar_comercio(cp or '')[:38]}")

        if not aplicar:
            print("\nEn seco. Con --aplicar escribe.")
            return 0

        for mid, cp, _, _, c in tocados:
            conn.execute("UPDATE movimientos SET categoria = %s WHERE id = %s",
                         (c, mid))
            conn.execute(
                """
                INSERT INTO log_acciones
                  (actor, accion, tabla, registro_id, antes, despues, motivo)
                VALUES ('relleno', 'editar', 'movimientos', %s, %s, %s,
                        'categoría puesta por la red de palabras clave')
                """,
                (mid, json.dumps({"categoria": None}),
                 json.dumps({"categoria": c}, ensure_ascii=False)))
        conn.commit()
        print(f"\n  Escritos {len(tocados)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
