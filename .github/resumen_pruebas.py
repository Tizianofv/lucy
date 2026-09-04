#!/usr/bin/env python3
"""Deja escrito en el resumen del job QUÉ NO SE PUDO PROBAR en el runner.

Un freno que solo dice «verde» o «rojo» esconde la tercera respuesta, que acá es
la importante: hay pruebas que en GitHub no se pueden correr porque necesitan el
corpus de correos bancarios reales (`tests/fixtures/**/*.eml`), que son
movimientos de Tiziano y de Rosi y están fuera de git por `.gitignore:23`.

Esas salen SALTADAS, nunca en verde. Este script las lista con nombre y motivo, y
además pone un techo: si mañana el número de saltadas sube, el job falla aunque
ninguna prueba esté en rojo. Sin ese techo, alguien puede apagar cobertura sin
querer y el freno seguiría en verde — que es exactamente la falla que este
trabajo vino a arreglar.

Uso:  python .github/resumen_pruebas.py informe.xml
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

# Medido el 4-sep-2026 contra 6f7d4da, en un entorno sin el corpus:
# 323 pasadas, 30 saltadas, 0 fallos.
#
# Subir este número es una decisión, no un trámite: significa que hay más código
# que GitHub ya no comprueba. Si sube porque de verdad hicieron falta más pruebas
# con corpus, se actualiza acá y se dice por qué en el commit.
TECHO_DE_SALTADAS = 30


def main(ruta: str) -> int:
    if not os.path.exists(ruta):
        print(f"::error::no se generó {ruta}: pytest no llegó a escribir el informe")
        return 1

    raiz = ET.parse(ruta).getroot()
    suites = [raiz] if raiz.tag == "testsuite" else list(raiz)

    saltadas: list[tuple[str, str]] = []
    total = fallos = errores = 0
    for suite in suites:
        total += int(suite.get("tests", 0))
        fallos += int(suite.get("failures", 0))
        errores += int(suite.get("errors", 0))
        for caso in suite.iter("testcase"):
            for s in caso.findall("skipped"):
                nombre = f"{caso.get('classname', '')}::{caso.get('name', '')}"
                saltadas.append((nombre, (s.get("message") or "").strip()))

    pasadas = total - fallos - errores - len(saltadas)

    lineas = [
        "## Pruebas de Lucy",
        "",
        f"- pasadas: **{pasadas}**",
        f"- fallos: **{fallos}**   errores: **{errores}**",
        f"- saltadas: **{len(saltadas)}** (techo: {TECHO_DE_SALTADAS})",
        "",
    ]

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
