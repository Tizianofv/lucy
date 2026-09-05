"""Prueba de humo: corre TODAS las consultas contra la base de verdad.

POR QUÉ EXISTE. Las 19 suites de tests son herméticas —dobles de conexión, sin
red, sin Postgres— y eso es correcto: corren en un segundo y no dependen de que
Railway esté vivo. Pero un doble responde lo que uno le diga, así que hay una
familia entera de fallos que NO puede ver, y los tres se publicaron el mismo día:

  · `movimientos.banco` existía en la base y no en db/schema.sql.
  · La tabla `backups` existía en schema.sql y no en la base. Reventaba cada
    diez minutos, dentro de un try, en silencio, desde hacía semanas.
  · `(%s IS NULL OR ...)` sin cast: Postgres no deduce el tipo del parámetro
    cuando llega NULL, y la portada del panel devolvía Internal Server Error.

Los tres son errores de ACOPLE entre el código y una base concreta. Solo se ven
preguntándole a la base.

Esto NO reemplaza a los tests: no comprueba que las respuestas sean correctas,
solo que las consultas CORREN. Es la diferencia entre "esto responde algo" y
"esto responde lo que debe", y hace falta pasar la primera para que la segunda
signifique algo.

    DATABASE_URL=... python3 tools/humo.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.db as db  # noqa: E402


async def main() -> int:
    if not os.environ.get("DATABASE_URL", "").strip():
        print("Falta DATABASE_URL.", file=sys.stderr)
        return 2

    await db.abrir()
    # Cada prueba es (nombre, función sin argumentos). Los argumentos se eligen
    # para pegarle a las ramas que rompen: None donde el SQL hace `IS NULL`
    # sobre un parámetro, que es exactamente lo que tiró la portada.
    pruebas = [
        ("resumen_por_mes", lambda: db.resumen_por_mes()),
        ("gasto_por_categoria (sin mes)", lambda: db.gasto_por_categoria(None)),
        ("gasto_por_categoria (con mes)", lambda: db.gasto_por_categoria("2026-08")),
        ("meses_con_movimientos", lambda: db.meses_con_movimientos()),
        ("sin_clasificar", lambda: db.sin_clasificar()),
        ("categorias_usadas", lambda: db.categorias_usadas()),
        ("categorias_aprendidas", lambda: db.categorias_aprendidas()),
        ("bancos_usados", lambda: db.bancos_usados()),
        ("salud_ingesta", lambda: db.salud_ingesta()),
        ("listar_cuentas_propias", lambda: db.listar_cuentas_propias()),
        ("ultimo_backup", lambda: db.ultimo_backup()),
        ("movimientos_filtrados (todo None)",
         lambda: db.movimientos_filtrados(None, None, None, None, None)),
        ("movimientos_filtrados (con filtros)",
         lambda: db.movimientos_filtrados(
             date(2026, 8, 1), date(2026, 8, 31), "gasto", None, "bhd")),
        ("leer_estado_consumos", lambda: db.leer_estado_consumos("tizianofv@gmail.com")),
        # El candado del reporte de correo: compara un TIMESTAMPTZ contra un
        # datetime con zona, usa LIKE con el prefijo escapado y devuelve los
        # chat_id que ya recibieron lo suyo. Si esta no corre, el reporte
        # matinal sale ~100 veces por mañana.
        ("destinos_con_encargo_hoy",
         lambda: db.destinos_con_encargo_hoy(
             "correo", "Reporte de correo de la mañana.",
             datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                microsecond=0))),
        ("silencio_por_banco", lambda: db.silencio_por_banco()),
        # El filtro que contesta "¿cuánto gasté en efectivo?". No hay columna
        # de método de pago: los gastos en efectivo se marcan con
        # banco='efectivo', así que esta es LA consulta de esa función. Es una
        # LECTURA — humo.py no escribe nada, y menos en producción.
        ("movimientos_filtrados (banco=efectivo)",
         lambda: db.movimientos_filtrados(None, None, None, None, "efectivo")),
    ]

    rojas = []
    descuadres: list[str] = []
    try:
        for nombre, fn in pruebas:
            try:
                r = await fn()
                n = len(r) if hasattr(r, "__len__") else ("None" if r is None else "ok")
                print(f"  ✓ {nombre:<38} {n}")
            except Exception as e:
                rojas.append((nombre, e))
                print(f"  ✗ {nombre:<38} {type(e).__name__}: {e}")

        # Éstas NO se miden por "corrió sin reventar": lo que importa es lo que
        # DEVUELVEN. Devolver una lista con problemas y salir en verde era el
        # fallo callado que este archivo existe para no repetir — así estuvo
        # `tablas_que_faltan` hasta el 4-sep-2026: imprimía "✓ ... 2" y nadie
        # entendía que ese 2 eran dos tablas faltantes.
        for nombre, fn in (("tablas_que_faltan", db.tablas_que_faltan),
                           ("columnas_que_faltan", db.columnas_que_faltan),
                           ("objetos_que_faltan", db.objetos_que_faltan)):
            try:
                hallazgos = await fn()
            except Exception as e:
                rojas.append((nombre, e))
                print(f"  ✗ {nombre:<38} {type(e).__name__}: {e}")
                continue
            if hallazgos:
                descuadres += hallazgos
                print(f"  ✗ {nombre:<38} {len(hallazgos)} descuadre(s)")
                for h in hallazgos:
                    print(f"      · {h}")
            else:
                print(f"  ✓ {nombre:<38} cuadra")
    finally:
        await db.cerrar()

    if rojas or descuadres:
        if rojas:
            print(f"\n  {len(rojas)} consulta(s) NO corren contra la base real.")
        if descuadres:
            # Por qué esto frena un despliegue, en las dos formas que tiene de
            # doler:
            #  · COLUMNAS. El esquema que ve Lucy al escribir SQL se arma desde
            #    db/schema.sql. Si el archivo y la base no cuadran, hay columnas
            #    que Lucy no sabe que existen y por las que no filtra. No
            #    revienta: contesta otro número que el panel.
            #  · ÍNDICES y RESTRICCIONES. Lo que está en el archivo y no en la
            #    base revienta la consulta que lo necesita (un ON CONFLICT sin
            #    su índice no guarda nada); lo que está en la base y no en el
            #    archivo desaparece el día que haya que levantarla de cero.
            print(f"\n  {len(descuadres)} descuadre(s) entre db/schema.sql y la "
                  "base. Mientras sigan, ni el número que contesta Lucy ni una "
                  "base creada desde el repo son de fiar.")
        return 1
    print(f"\n  Las {len(pruebas)} corren contra la base real, y db/schema.sql "
          "cuadra con ella.")
    return 0


sys.exit(asyncio.run(main()))
