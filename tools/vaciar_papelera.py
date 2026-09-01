"""Borra DE VERDAD lo que lleva más de 30 días en la papelera.

CORRE DESPUÉS DEL RESPALDO, Y SOLO SI EL RESPALDO SE VERIFICÓ. Ese orden es la
única razón por la que este script puede existir sin contradecir el pilar del
proyecto —"borrar es marcar borrado_en, nunca DELETE real"—: lo que se destruye
acá ya está dentro de una copia buena tomada hace segundos.

Al revés sería la única forma de perder algo de verdad en todo el sistema.

Lo que NO se toca es `log_acciones`: ahí queda el `antes` completo en JSON, así
que después del DELETE todavía hay rastro de qué había y quién lo borró. Lo que
se pierde es la fila viva, no la memoria.

    DATABASE_URL=... python3 tools/vaciar_papelera.py [--aplicar]
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.db as db  # noqa: E402


async def main() -> int:
    if not os.environ.get("DATABASE_URL", "").strip():
        print("Falta DATABASE_URL.", file=sys.stderr)
        return 2
    aplicar = "--aplicar" in sys.argv
    await db.abrir()
    try:
        pendientes = [m for m in await db.papelera() if m["dias"] <= 0]
        for m in pendientes:
            print(f"  M-{m['id']:04d}  {m['fecha']}  {m['monto']:>10,.2f} "
                  f"{m['moneda']}  {(m['contraparte'] or '')[:34]}")
        if not aplicar:
            print(f"\n  {len(pendientes)} para borrar. En seco; con --aplicar borra.")
            return 0
        n = await db.vaciar_papelera()
        print(f"\n  Borrados definitivamente: {n}")
    finally:
        await db.cerrar()
    return 0


sys.exit(asyncio.run(main()))
