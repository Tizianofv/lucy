"""Crea la carpeta de Drive de los proyectos que ya existían (Carpetas del proyecto, pieza 3: el arreglo de una vez).

POR QUÉ EXISTE. Desde la pieza 1 la carpeta nace con el proyecto, pero los proyectos creados ANTES no la tienen.
Este guion se las da, UNO por UNO, por la MISMA puerta que usan las rutas y el botón (`preparar_la_carpeta`):
no habla con Drive por su cuenta, no escribe en la base por su cuenta y no arma frases. Lo que dice de cada proyecto
es `frase_de_la_carpeta` de ese resultado. Lo corre la SALA, no un agente de construcción.

A QUIÉN LE TOCA (Tiziano, 18-oct-2026: «solo activos» y «si pero solo si también la agregas al proyecto»):
  · SOLO los proyectos vivos (no están en la papelera) con `estado = 'activo'`. Pausados y cerrados no se tocan.
  · Con la carpeta del proyecto (`proyectos.carpeta`) VACÍA: el enlace tiene que poder quedar puesto en el proyecto.
    Uno que ya trae algo escrito NO recibe otra carpeta (lectura de la sala): se cuenta aparte y no se toca.
  · Uno que ya tiene fila en `drive_de_proyectos` pero la carpeta del proyecto vacía (la creación anterior se cayó antes
    de escribir el enlace) entra: `preparar_la_carpeta` solo le escribe el enlace y NO le pide nada nuevo a Drive.

MODOS. Por omisión `--contar`: solo lee la base y NO llama a Drive. `--crear --maximo N` hace lo mismo y además crea,
en serie, a lo más N proyectos (`--maximo` es obligatorio: no hay «crear todos» sin decir cuántos). El primer paso real
es `--crear --maximo 1`. Se detiene en el primer fallo de Drive y no insiste con los demás. Segunda corrida: todo
queda en «completos» y no hay ni una llamada de escritura a Drive.

LA SALIDA lleva ids de proyecto y cuentas, nunca nombres ni clientes, y de las variables de Drive solo si existen
(nunca sus valores). Código de salida: 0 si lo pedido quedó hecho (con `--crear`: cada proyecto tiene su fila en
`drive_de_proyectos` y su `proyectos.carpeta` RELEÍDA de la base es el enlace de esa carpeta); 1 si algo quedó sin
hacer o no se pudo leer; 2 si el uso o el entorno están mal (no se hizo nada).

LA SALA, desde `/Users/controlroom/Documents/IA CDS/lucy` (el servicio `lucy` trae las variables de Drive y el resto
de la configuración; la base es la pública de Postgres):

    railway run -s Postgres -- sh -c 'PUB="$DATABASE_PUBLIC_URL" railway run -s lucy -- sh -c "DATABASE_URL=\\"\\$PUB\\" \\
      <python3 de la sala> <ruta del árbol>/tools/crear_carpetas_de_drive.py --contar"'

FRONTERA, dicha una vez: este guion se probó con una base SQLite (las sentencias reales de `db`) y con un Drive de mentira
(`httpx.MockTransport`); NUNCA corrió contra Postgres ni contra Google. Que `railway run` anidado le pase al guion las
variables correctas no se ejerció aquí. Los nombres repetidos que cuenta son los de la base: con el permiso `drive.file`
Lucy no ve carpetas hechas a mano en «Proyectos», así que un choque con una de ellas no se puede contar.
"""
from __future__ import annotations

import os
import sys

if __name__ == "__main__" and not os.environ.get("DATABASE_URL", "").strip():
    # Antes de importar nada: `config` revienta sin esta variable.
    print("Falta DATABASE_URL en el entorno.", file=sys.stderr)
    sys.exit(2)

import argparse  # noqa: E402
import asyncio  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cotizaciones_drive as cd  # noqa: E402
import db.db as db  # noqa: E402
import drive  # noqa: E402

# A quién le toca: el estado que Tiziano llamó «activos». Una prueba exige que esté en el vocabulario de `db`.
ESTADO_QUE_ENTRA = "activo"

# Por qué se escribió `proyectos.carpeta` (queda en la huella): el origen es este arreglo, no un proyecto nuevo.
MOTIVO = "La carpeta del proyecto en Drive (la creó Lucy en el arreglo de una vez para los proyectos que ya existían)"

# Las cuatro variables de Drive que Lucy necesita para crear carpetas; los nombres salen de `drive.py`.
VARIABLES_DE_DRIVE = (*drive.VARIABLES, drive.VARIABLE_CARPETA_PROYECTOS)

# Los grupos en que cae cada proyecto activo (excluyentes), de lo que dice la base.
COMPLETO = "completos"          # fila en drive_de_proyectos Y algo escrito en proyectos.carpeta: nada que hacer
SOLO_ENLACE = "solo_enlace"     # fila en drive_de_proyectos y proyectos.carpeta vacía: solo falta escribir el enlace
POR_CREAR = "por_crear"         # sin fila y proyectos.carpeta vacía: falta la carpeta de Drive
CARPETA_AJENA = "carpeta_ajena"  # sin fila y proyectos.carpeta ya trae algo: no se le crea otra, se cuenta aparte
GRUPOS = (COMPLETO, SOLO_ENLACE, POR_CREAR, CARPETA_AJENA)
# De estos dos sale el trabajo. Primero los que necesitan a Drive: el primer paso real prueba justo eso.
POR_HACER = (POR_CREAR, SOLO_ENLACE)


class NoSePudoLeer(Exception):
    """La base no contestó lo que hacía falta (la columna o la tabla no están, o la conexión falló). Lleva el motivo
    en una frase fija; nunca el texto de la excepción original, que puede traer direcciones."""


def _escrita(valor) -> bool:
    """¿`proyectos.carpeta` trae algo? Lo que `preparar_la_carpeta` no pisa: lo mismo que ella decide, por la misma
    función de `db` (un valor que no vale cuenta como escrito: ella tampoco lo toca)."""
    try:
        return db.carpeta_guardada(valor) is not None
    except db.CarpetaDeProyectoNoVale:
        return True


async def leer_el_plano() -> dict:
    """Lo que hay hoy, solo leyendo, por las lecturas de `db`: los proyectos vivos por estado, y los activos repartidos
    en `GRUPOS`. No escribe nada ni llama a Drive."""
    try:
        vivos = await db.proyectos_vivos()
        carpetas = await db.carpetas_de_proyectos()
    except Exception as e:
        raise NoSePudoLeer(f"no se pudo leer la lista de proyectos ({type(e).__name__})") from None
    if carpetas is None:
        raise NoSePudoLeer("la columna proyectos.carpeta no existe en esta base (migración sin aplicar)")
    por_estado: dict[str, int] = {}
    for p in vivos:
        clave = p["estado"] if p["estado"] in db.ESTADOS_PROYECTO else "otro"
        por_estado[clave] = por_estado.get(clave, 0) + 1
    grupos: dict[str, list[int]] = {g: [] for g in GRUPOS}
    for p in sorted((p for p in vivos if p["estado"] == ESTADO_QUE_ENTRA), key=lambda p: p["id"]):
        try:
            fila = await db.carpetas_de_drive(p["id"])
        except Exception as e:
            raise NoSePudoLeer(f"no se pudo leer drive_de_proyectos ({type(e).__name__})") from None
        escrita = _escrita(carpetas.get(p["id"]))
        if fila is not None:
            grupos[COMPLETO if escrita else SOLO_ENLACE].append(p["id"])
        else:
            grupos[CARPETA_AJENA if escrita else POR_CREAR].append(p["id"])
    nombres: dict[str, list[int]] = {}
    for p in vivos:
        nombres.setdefault((p["nombre"] or "").strip(), []).append(p["id"])
    repetidos = sorted(i for ids in nombres.values() if len(ids) > 1 for i in ids)
    return {"vivos": len(vivos), "por_estado": por_estado, "grupos": grupos, "repetidos": repetidos}


def variables_que_faltan() -> list[str]:
    """Los nombres (nunca los valores) de las variables de Drive que no están o están en blanco. Cada una se lee por su
    nombre escrito (la guarda del entorno del repositorio no deja leer el entorno por una clave que no sea constante);
    una prueba exige que sean EXACTAMENTE las de `VARIABLES_DE_DRIVE`, que sale de `drive.py`."""
    puestas = {
        "LUCY_DRIVE_CLIENT_ID": os.environ.get("LUCY_DRIVE_CLIENT_ID", ""),
        "LUCY_DRIVE_CLIENT_SECRET": os.environ.get("LUCY_DRIVE_CLIENT_SECRET", ""),
        "LUCY_DRIVE_REFRESH_TOKEN": os.environ.get("LUCY_DRIVE_REFRESH_TOKEN", ""),
        "LUCY_DRIVE_CARPETA_PROYECTOS": os.environ.get("LUCY_DRIVE_CARPETA_PROYECTOS", ""),
    }
    return [n for n in VARIABLES_DE_DRIVE if not puestas.get(n, "").strip()]


def _ids(ids) -> str:
    return ", ".join(f"#{i}" for i in ids) if ids else "ninguno"


def imprimir_el_plano(plano: dict) -> None:
    g = plano["grupos"]
    estados = [*db.ESTADOS_PROYECTO, "otro"]
    print(f"Proyectos vivos (fuera de la papelera): {plano['vivos']}")
    print("  por estado: " + " · ".join(f"{e} {plano['por_estado'][e]}" for e in estados if e in plano["por_estado"]))
    print(f"Solo se miran los que están en estado «{ESTADO_QUE_ENTRA}»: {sum(len(v) for v in g.values())}")
    print(f"  {len(g[COMPLETO]):>4}  ya completos: fila en drive_de_proyectos y algo escrito en proyectos.carpeta (nada que hacer)")
    print(f"  {len(g[SOLO_ENLACE]):>4}  con carpeta en Drive y proyectos.carpeta vacía (solo se escribiría el enlace; Drive no se toca)")
    print(f"        ids: {_ids(g[SOLO_ENLACE])}")
    print(f"  {len(g[POR_CREAR]):>4}  sin carpeta en Drive y proyectos.carpeta vacía (se crearía su carpeta)")
    print(f"        ids: {_ids(g[POR_CREAR])}")
    print(f"  {len(g[CARPETA_AJENA]):>4}  sin carpeta en Drive pero con algo ya escrito en proyectos.carpeta (NO se tocan)")
    print(f"        ids: {_ids(g[CARPETA_AJENA])}")
    por_hacer = sum(len(g[k]) for k in POR_HACER)
    print(f"Por hacer con --crear: {por_hacer}")
    print(f"Ids que comparten nombre exacto con otro proyecto vivo (la segunda carpeta llevaría «· #id»): {_ids(plano['repetidos'])}")


def imprimir_las_variables() -> None:
    faltan = set(variables_que_faltan())
    print("Variables de Drive (solo si existen, nunca su valor):")
    for n in VARIABLES_DE_DRIVE:
        print(f"  {n}: {'FALTA' if n in faltan else 'sí'}")


async def _comprobar(proyecto_id: int) -> bool:
    """Lo que se exige para dar un proyecto por hecho, releído de la base DESPUÉS de crear: tiene su fila en
    `drive_de_proyectos` y su `proyectos.carpeta` es exactamente el enlace de esa carpeta."""
    fila = await db.carpetas_de_drive(proyecto_id)
    if fila is None:
        return False
    esperado = cd.enlace_de_carpeta_de_drive(fila["carpeta_proyecto_id"])
    actuales = await db.carpetas_de_proyectos()
    if esperado is None or actuales is None:
        return False
    try:
        return db.carpeta_guardada(actuales.get(proyecto_id)) == esperado
    except db.CarpetaDeProyectoNoVale:
        return False


async def crear(plano: dict, maximo: int) -> dict:
    """Pide la carpeta, de a una, de los primeros `maximo` proyectos por hacer. Se detiene en el primer fallo de Drive.
    Devuelve las cuentas: `resultados` por clase de `preparar_la_carpeta`, `hechos`, `no_intentados`, `fuera_del_maximo`."""
    g = plano["grupos"]
    cola = [i for k in POR_HACER for i in g[k]]
    elegidos, fuera = cola[:maximo], cola[maximo:]
    resultados: dict[str, int] = {}
    hechos = 0
    no_intentados = list(elegidos)
    for i in elegidos:
        no_intentados.remove(i)
        r = await cd.preparar_la_carpeta(i, motivo=MOTIVO)
        clase = r.get("resultado") if isinstance(r, dict) else None
        resultados[str(clase)] = resultados.get(str(clase), 0) + 1
        hecho = clase in ("lista", "ya_estaba") and await _comprobar(i)
        hechos += bool(hecho)
        print(f"  #{i}: {cd.frase_de_la_carpeta(r)}  [fila y enlace releídos de la base: {'sí' if hecho else 'NO'}]")
        if clase == "fallo":
            print("  Se detiene aquí: Drive falló y no se insiste con los demás.")
            break
    else:
        no_intentados = []
    return {"resultados": resultados, "hechos": hechos, "elegidos": len(elegidos),
            "no_intentados": no_intentados, "fuera_del_maximo": fuera}


def _argumentos(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="crear_carpetas_de_drive.py",
        description="Crea la carpeta de Drive de los proyectos activos que no la tienen. Por omisión solo cuenta.")
    modo = p.add_mutually_exclusive_group()
    modo.add_argument("--contar", action="store_true", help="solo cuenta, no escribe nada (por omisión)")
    modo.add_argument("--crear", action="store_true", help="crea las carpetas; pide --maximo")
    p.add_argument("--maximo", type=int, help="cuántos proyectos crear como mucho (obligatorio con --crear)")
    a = p.parse_args(argv)
    if a.crear and (a.maximo is None or a.maximo < 1):
        p.error("--crear necesita --maximo N, con N de 1 en adelante")
    if a.maximo is not None and not a.crear:
        p.error("--maximo solo va con --crear")
    return a


async def correr(argv: list[str]) -> int:
    a = _argumentos(argv)
    if a.crear:
        faltan = variables_que_faltan()
        if faltan:
            print("No se hizo nada: faltan variables de Drive en el entorno: " + ", ".join(faltan), file=sys.stderr)
            return 2
    await db.abrir()
    try:
        try:
            plano = await leer_el_plano()
        except NoSePudoLeer as e:
            print(f"No se pudo contar: {e}.", file=sys.stderr)
            return 1
        imprimir_el_plano(plano)
        if not a.crear:
            imprimir_las_variables()
            print("\nModo que solo cuenta: no se escribió nada en la base ni se llamó a Drive.")
            return 0
        print(f"\nCreando como mucho {a.maximo}:")
        hecho = await crear(plano, a.maximo)
        print("\nResultado: " + (" · ".join(f"{k} {v}" for k, v in sorted(hecho["resultados"].items())) or "nada que hacer"))
        print(f"  hechos y releídos: {hecho['hechos']} de {hecho['elegidos']} pedidos")
        if hecho["no_intentados"]:
            print(f"  sin intentar por el fallo: {_ids(hecho['no_intentados'])}")
        if hecho["fuera_del_maximo"]:
            print(f"  quedan fuera del máximo: {_ids(hecho['fuera_del_maximo'])}")
        try:
            despues = await leer_el_plano()
        except NoSePudoLeer as e:
            print(f"No se pudo releer el estado final: {e}.", file=sys.stderr)
            return 1
        print("Después de esta corrida:")
        imprimir_el_plano(despues)
        return 0 if hecho["hechos"] == hecho["elegidos"] else 1
    finally:
        await db.cerrar()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(correr(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
