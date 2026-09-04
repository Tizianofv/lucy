#!/usr/bin/env python3
"""Deja escrito, fuera del log, QUÉ FALLÓ y QUÉ NO SE PUDO PROBAR en el runner.

Hace dos cosas, y las dos existen porque el log del job NO SE PUEDE LEER.

1) DICE QUÉ SE ROMPIÓ, SIN PEDIR PERMISOS DE ADMINISTRADOR.
   El log completo de una corrida se baja por
   `GET /repos/:o/:r/actions/jobs/:id/logs`, y esa ruta contesta
   `403 Must have admin rights to Repository` aunque el repo sea público —
   comprobado el 4-sep-2026 contra la corrida 33890369787. Con el freno recién
   puesto, la primera corrida salió roja y desde fuera lo único legible era
   «Process completed with exit code 1»: un freno que muerde y no dice dónde.

   La salida que SÍ es pública son las ANOTACIONES del check run. Mismo día,
   misma corrida, sin autenticar:
       GET /repos/Tizianofv/lucy/check-runs/101080280600/annotations  → 200
   Devuelve `path`, `start_line`, `annotation_level` y `message` de cada una.
   También salen dibujadas en la página de la corrida y en el commit.

   Así que cada prueba en rojo se imprime como `::error file=…,line=…::…`, que
   es como se crea una anotación desde un workflow. GitHub solo dibuja las
   primeras 10 de nivel error por paso; si hay más, la última anotación nombra
   las que quedaron fuera.

2) DICE QUÉ NO CORRIÓ.
   Un freno que solo dice «verde» o «rojo» esconde la tercera respuesta: hay
   pruebas que en GitHub no se pueden correr porque necesitan el corpus de
   correos bancarios reales (`tests/fixtures/**/*.eml`), que son movimientos de
   Tiziano y de Rosi y están fuera de git por `.gitignore:23`.

   Esas salen SALTADAS, nunca en verde. Se listan con nombre y motivo, y además
   con un techo: si mañana el número de saltadas sube, el job falla aunque
   ninguna prueba esté en rojo. Sin ese techo, alguien puede apagar cobertura
   sin querer y el freno seguiría en verde — que es exactamente la falla que
   este trabajo vino a arreglar.

El código de salida de ESTE script sigue significando una sola cosa: «se perdió
cobertura». Que haya pruebas en rojo ya lo dice el paso «Suite», y repetirlo acá
solo duplicaría el aviso.

Uso:  python .github/resumen_pruebas.py informe.xml
"""
from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET

# Medido el 4-sep-2026 en un entorno sin el corpus y con el árbol limpio (solo
# los archivos que git tiene, sin el venv adentro): 325 pasadas, 30 saltadas,
# 0 fallos.
#
# La medición anterior decía 323 pasadas contra 6f7d4da y estaba tomada en una
# carpeta con el venv dentro del repo, donde el aislamiento del conftest tapaba
# 8 fallos. El número de saltadas era el mismo; el de pasadas, no.
#
# Subir este número es una decisión, no un trámite: significa que hay más código
# que GitHub ya no comprueba. Si sube porque de verdad hicieron falta más pruebas
# con corpus, se actualiza acá y se dice por qué en el commit.
TECHO_DE_SALTADAS = 30

# GitHub dibuja como mucho 10 anotaciones de nivel error por paso. Pasarse no
# rompe nada, pero las de más no se ven: mejor gastar la última en decir cuáles
# quedaron fuera que perderlas en silencio.
ANOTACIONES_MAX = 10

# La línea con la que pytest cierra cada fallo en el XML:
#   tests/test_reporte_una_vez_al_dia.py:200: AssertionError
# La ruta ya viene relativa a la raíz del repo, que es lo que pide la anotación.
# No se lee de los atributos del <testcase> porque pytest 9.1.1 no escribe ahí
# `file` ni `line` (comprobado el 4-sep-2026 sobre un informe.xml en rojo).
_UBICACION = re.compile(r"^(?P<archivo>[^\s:]+\.py):(?P<linea>\d+):", re.M)


def _escapar(texto: str, propiedad: bool = False) -> str:
    """Escapa un texto para meterlo en un comando de workflow (`::error …`)."""
    t = texto.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if propiedad:                      # dentro de file=/line=/title= además:
        t = t.replace(":", "%3A").replace(",", "%2C")
    return t


def _donde(clase: str, detalle: str) -> tuple[str, str]:
    """Archivo y línea del fallo. Si el rastro no los dice, el archivo de la prueba."""
    ubicaciones = _UBICACION.findall(detalle or "")
    if ubicaciones:
        archivo, linea = ubicaciones[-1]
        return archivo, linea
    return clase.replace(".", "/") + ".py", ""


def _porque(mensaje: str, detalle: str) -> str:
    """El motivo del rojo, corto.

    Se prefieren las líneas `E …` del rastro, que son las que pytest marca como
    la evidencia del fallo; el atributo `message` es esas mismas líneas sin el
    prefijo, así que pegar los dos duplicaba el texto entero.
    """
    evidencia = [l[2:].rstrip() for l in (detalle or "").splitlines()
                 if l.startswith("E ")]
    texto = "\n".join(evidencia[-8:]).strip() or (mensaje or "").strip()
    return texto[:2500] or "sin detalle en el informe"


def main(ruta: str) -> int:
    if not os.path.exists(ruta):
        print(f"::error::no se generó {ruta}: pytest no llegó a escribir el informe")
        return 1

    raiz = ET.parse(ruta).getroot()
    suites = [raiz] if raiz.tag == "testsuite" else list(raiz)

    saltadas: list[tuple[str, str]] = []
    rojas: list[tuple[str, str, str, str]] = []   # nombre, archivo, línea, motivo
    total = fallos = errores = 0
    for suite in suites:
        total += int(suite.get("tests", 0))
        fallos += int(suite.get("failures", 0))
        errores += int(suite.get("errors", 0))
        for caso in suite.iter("testcase"):
            clase = caso.get("classname", "")
            nombre = f"{clase}::{caso.get('name', '')}"
            for s in caso.findall("skipped"):
                saltadas.append((nombre, (s.get("message") or "").strip()))
            for r in list(caso.findall("failure")) + list(caso.findall("error")):
                archivo, linea = _donde(clase, r.text or "")
                rojas.append((nombre, archivo, linea,
                              _porque(r.get("message") or "", r.text or "")))

    pasadas = total - fallos - errores - len(saltadas)

    # Las anotaciones primero: son lo único de todo esto que se lee desde fuera
    # sin permisos de administrador (ver la cabecera del archivo).
    for nombre, archivo, linea, motivo in rojas[:ANOTACIONES_MAX]:
        campos = [f"file={_escapar(archivo, True)}"]
        if linea:
            campos.append(f"line={linea}")
        campos.append(f"title={_escapar('prueba en rojo: ' + nombre, True)}")
        print(f"::error {','.join(campos)}::{_escapar(motivo)}")
    if len(rojas) > ANOTACIONES_MAX:
        resto = ", ".join(n for n, _, _, _ in rojas[ANOTACIONES_MAX:])
        print(f"::error::y {len(rojas) - ANOTACIONES_MAX} pruebas más en rojo "
              f"que no caben en las anotaciones: {_escapar(resto)}")

    lineas = [
        "## Pruebas de Lucy",
        "",
        f"- pasadas: **{pasadas}**",
        f"- fallos: **{fallos}**   errores: **{errores}**",
        f"- saltadas: **{len(saltadas)}** (techo: {TECHO_DE_SALTADAS})",
        "",
    ]

    if rojas:
        lineas += [
            "### Lo que salió en rojo",
            "",
        ]
        for nombre, archivo, linea, motivo in rojas:
            donde = f"{archivo}:{linea}" if linea else archivo
            primera = motivo.splitlines()[0] if motivo else ""
            lineas.append(f"- `{nombre}` — {donde} — {primera}")
        lineas.append("")

    if saltadas:
        lineas += [
            "### Lo que este runner NO pudo probar",
            "",
            "No es que estén bien: es que no corrieron. El corpus de correos "
            "bancarios reales vive fuera de git (`.gitignore:23`) porque son "
            "movimientos reales, y en un runner limpio no existe.",
            "",
        ]
        for nombre, motivo in saltadas:
            lineas.append(f"- `{nombre}` — {motivo}")
        lineas.append("")

    resumen = "\n".join(lineas)
    print(resumen)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(resumen + "\n")

    if len(saltadas) > TECHO_DE_SALTADAS:
        print(
            f"::error::hay {len(saltadas)} pruebas saltadas y el techo es "
            f"{TECHO_DE_SALTADAS}. Se perdió cobertura: o se arregla, o se sube "
            f"el techo a mano en .github/resumen_pruebas.py diciendo por qué."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "informe.xml"))
