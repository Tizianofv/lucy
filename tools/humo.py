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


async def _derivaciones_sin_tolerancia():
    """El mismo SELECT que `db.derivaciones()`, pero SIN su tolerancia a
    SQLSTATE 42703 (columna ausente).

    `db.derivaciones()` TRAGA ese error a propósito -- es la tolerancia de
    migración que hace que el panel siga andando mientras la sala no haya
    corrido `db/migrations/2026-09-25_deriva_de.sql` -- así que llamarla acá
    tal cual no serviría de nada: saldría en VERDE tanto con la migración
    aplicada como sin ella, exactamente el defecto que este archivo existe
    para agarrar (ver la cabecera). Por eso este archivo corre el mismo
    texto DIRECTO, sin ningún `try` alrededor.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=db.dict_row)
        await cur.execute(
            "SELECT id, deriva_de_id FROM tareas "
            "WHERE deriva_de_id IS NOT NULL AND borrado_en IS NULL")
        return await cur.fetchall()


async def _tablas_de_cotizaciones():
    """Los dos SELECT de las tablas de las cotizaciones (pieza 2, 14-oct-2026), SIN tolerancia.

    El contador (`db.siguiente_numero_de_cotizacion`) escribe en `serie_cotizaciones` con un
    `INSERT ... ON CONFLICT (anio)`: sin la tabla, o sin su llave, la primera cotización no guardaría
    nada y la suite (hermética, SQLite) no lo vería. Contra una base sin la migración
    `db/migrations/2026-10-14_cotizaciones.sql`, esta línea revienta con «relation ... does not exist».
    Son LECTURAS (`count(*)`); el contador no se ejecuta acá, porque humo.py no escribe en producción.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=db.dict_row)
        filas = {}
        await cur.execute("SELECT count(*) AS n FROM serie_cotizaciones")
        filas["serie_cotizaciones"] = (await cur.fetchone())["n"]
        await cur.execute("SELECT count(*) AS n FROM cotizaciones")
        filas["cotizaciones"] = (await cur.fetchone())["n"]
        return filas


async def _tablas_de_drive_de_las_cotizaciones():
    """Los SELECT de lo que la pieza 8 agregó (16-oct-2026), SIN tolerancia: las tres columnas de Drive de
    `cotizaciones` y la tabla `drive_de_proyectos`. La lectura de las cotizaciones (`cotizaciones_de_proyectos`) TOLERA
    que falten (devuelve `None` y la página sale sin el bloque), así que esta es la línea que dice si la migración
    `db/migrations/2026-10-16_cotizaciones_drive.sql` no se aplicó. Son LECTURAS; la subida a Drive no se ejerce acá.
    """
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=db.dict_row)
        filas = {}
        await cur.execute("SELECT count(drive_archivo_id) AS n, count(drive_version) AS v, count(drive_error) AS e "
                          "FROM cotizaciones")
        filas["cotizaciones con Drive"] = dict(await cur.fetchone())
        await cur.execute("SELECT count(*) AS n FROM drive_de_proyectos")
        filas["drive_de_proyectos"] = (await cur.fetchone())["n"]
        return filas


async def _columna_de_audio_sin_tolerancia():
    """El SELECT de la carpeta de audio (pieza 4 de «Carpetas del proyecto», 18-oct-2026), SIN tolerancia.
    `db.carpetas_de_audio()` TOLERA que la columna falte (devuelve `None` y la página sale sin la línea de audio),
    así que `pagina_de_proyectos` sigue en verde sin la migración `db/migrations/2026-10-18_proyectos_carpeta_audio.sql`.
    Esta línea es la que lo dice. Es una LECTURA."""
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=db.dict_row)
        await cur.execute("SELECT count(carpeta_audio) AS n FROM proyectos")
        return {"proyectos con carpeta_audio": (await cur.fetchone())["n"]}


async def _consultas_de_natalia_sin_tolerancia():
    """Los SELECT de las consultas de Natalia (pieza L3, 18-oct-2026), SIN tolerancia: `count(*)` de la tabla y
    las columnas que la lectura y el escritor de estado nombran. `db.consultas_de_natalia_de_cliente` y
    `db.abrir_consulta_de_natalia` TOLERAN que la tabla falte (lanzan `ConsultasSinTabla` y las rutas contestan
    503), así que esas líneas solas no dirían si la migración `db/migrations/2026-10-18_consultas_de_natalia.sql`
    no se aplicó. Esta sí. Son LECTURAS: ni abrir una consulta ni cambiarle el estado se ejerce acá (humo.py no
    escribe en producción)."""
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=db.dict_row)
        await cur.execute(
            "SELECT count(*) AS n, count(tarjeta_msg_id) AS tarjetas, count(tarea_resultante_id) AS nacidas, "
            "count(vence_vencida_en) AS vencidas, count(contestada_por_chat_id) AS contestadas "
            "FROM consultas_de_natalia")
        return dict(await cur.fetchone())


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
        # La consulta del panel de tareas, sobre una tabla que hasta que se
        # escribió este panel NINGUNA función de db/db.py consultaba: o sea que
        # es exactamente la familia de error de acople que este archivo existe
        # para agarrar.
        #
        # Y desde el 10-sep-2026 toca `tareas.responsable_chat_id`, una columna
        # que la migración de ese día agrega y que NO EXISTE hasta que alguien
        # la aplique. Ésta es la corrida que lo dice: contra una base sin la
        # migración, esta línea es la que se pone roja —la suite entera sale
        # verde igual, porque es hermética y su conexión es de mentira—. Si
        # revienta acá con «column ... does not exist», falta correr
        # db/migrations/2026-09-10_responsable_de_la_tarea.sql.
        #
        # Es una LECTURA; las escrituras (marcar_tarea_hecha,
        # asignar_responsable) no se prueban acá, porque humo.py no escribe en
        # producción.
        ("tareas_por_grupo", lambda: db.tareas_por_grupo()),
        # La tabla de comentarios nace con la migración del 13-sep-2026
        # (db/migrations/2026-09-13_comentarios_de_tareas.sql). Contra una base
        # sin esa migración, esta línea revienta con «relation ... does not
        # exist», y es la única que lo dice: la suite es hermética y sale verde
        # igual. Es una LECTURA (tarea 0, que no existe); las escrituras
        # (comentar_tarea, borrar_comentario) no se prueban acá.
        ("comentarios_de_tarea", lambda: db.comentarios_de_tarea(0)),
        # La tarea derivada (25-sep-2026): `tareas_por_grupo` de arriba TOLERA
        # que `deriva_de_id` no exista todavía (cae a la consulta sin ella,
        # SQLSTATE 42703) -- así que si la migración no se aplicó, esa línea
        # sigue en VERDE y no dice nada. Y `db.derivaciones()` TAMBIÉN tolera
        # la columna ausente (devuelve `{}`), así que llamarla tal cual acá
        # tampoco serviría. Ésta es la que sí lo dice: `_derivaciones_sin_
        # tolerancia`, el mismo SELECT, SIN el `try`. Si revienta acá con
        # «column tareas.deriva_de_id does not exist», falta correr
        # db/migrations/2026-09-25_deriva_de.sql. Es una LECTURA; la escritura
        # (`cerrar_y_derivar`) no se prueba acá, porque humo.py no escribe en
        # producción.
        ("derivaciones (sin tolerancia)", _derivaciones_sin_tolerancia),
        # La página de proyectos (Lucy 1.0, E4): cuatro lecturas, una con
        # `UNION ALL` sobre `log_acciones` y las columnas que agregó la migración
        # `2026-10-02_proyectos_responsable_cliente_participantes.sql`
        # (`proyectos.responsable_chat_id`, `cliente_nombre`,
        # `comentarios_tarea.editado_en`) y la lectura de `participantes` (las
        # personas de cada proyecto y tarea, E7). Sin tolerancia a propósito: si
        # esa migración no está, esta línea es la que lo dice. Es una LECTURA.
        ("pagina_de_proyectos", lambda: db.pagina_de_proyectos()),
        # Lo que Natalia lee de los proyectos de un cliente (pieza L1, 18-oct-2026): una LECTURA con una ficha
        # cualquiera. OJO: si esa ficha no tiene proyectos, `proyectos_de_cliente` sale despues de la PRIMERA consulta y
        # la de las tareas (el JOIN) no corre; solo prueba la segunda si la ficha 1 existe en la base.
        ("proyectos_de_cliente", lambda: db.proyectos_de_cliente(1)),
        # Las tablas de las cotizaciones (pieza 2): ver `_tablas_de_cotizaciones`. Una LECTURA.
        ("tablas de cotizaciones (sin tolerancia)", _tablas_de_cotizaciones),
        # Lo que la pieza 8 agregó para Drive: ver `_tablas_de_drive_de_las_cotizaciones`. Una LECTURA.
        ("columnas y tabla de Drive de las cotizaciones (sin tolerancia)", _tablas_de_drive_de_las_cotizaciones),
        # La carpeta de audio (pieza 4 de «Carpetas del proyecto»): ver `_columna_de_audio_sin_tolerancia`. Una LECTURA.
        ("columna carpeta_audio de proyectos (sin tolerancia)", _columna_de_audio_sin_tolerancia),
        # Las consultas de Natalia (pieza L3): ver `_consultas_de_natalia_sin_tolerancia`. Una LECTURA.
        ("consultas_de_natalia (sin tolerancia)", _consultas_de_natalia_sin_tolerancia),
        # La lectura de las consultas por ficha: corre el SQL de verdad (la ficha va en la consulta) con ids que no
        # existen. TOLERA la tabla ausente (503 en la ruta), por eso la línea de arriba es la que lo dice. LECTURA.
        ("consultas_de_natalia_de_cliente", lambda: db.consultas_de_natalia_de_cliente(1, [1, 2])),
        # La lectura de las cotizaciones (pieza 3): TOLERA que la tabla no exista (devuelve `None`), así
        # que la línea de arriba es la que dice si falta; ésta corre el SELECT con sus columnas y el JOIN
        # contra la base de verdad. Es una LECTURA; `guardar_cotizacion` escribe y no se prueba acá.
        ("cotizaciones_de_proyectos", lambda: db.cotizaciones_de_proyectos()),
        # El número que la ventana de «Nueva cotización» enseña (pieza 5): un SELECT de la serie. LECTURA.
        ("numero_que_sigue_de_cotizacion", lambda: db.numero_que_sigue_de_cotizacion()),
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
