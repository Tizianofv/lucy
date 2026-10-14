"""Humo de la herramienta de PDF (fpdf2): se importa, arma un PDF de una página
en memoria y lo que se escribió se puede leer de vuelta. No escribe a disco.

Lo que NO prueba: ninguna hoja real de Lucy (todavía no existe el escritor).
Dicho para el que construya el PDF de verdad: con la letra de serie `helvetica`
los acentos y la «ñ» salen, pero «—» no (levanta FPDFUnicodeEncodingException).
"""
import io
import re
from importlib.metadata import requires, version
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader

RAIZ = Path(__file__).resolve().parent.parent


def test_arma_un_pdf_de_una_pagina_en_memoria():
    pdf = FPDF(format="Letter")
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Cotización Ñandú RD$ 62,500.00")
    crudo = bytes(pdf.output())
    assert crudo.startswith(b"%PDF-")
    lector = PdfReader(io.BytesIO(crudo))
    assert len(lector.pages) == 1
    assert lector.pages[0].extract_text().strip() == "Cotización Ñandú RD$ 62,500.00"


def _pines():
    """{nombre normalizado: versión} de las líneas `nombre==versión` de requirements.txt."""
    pines = {}
    for linea in (RAIZ / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(\S+)\s*$", linea.split("#", 1)[0])
        if m:
            pines[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pines


def test_lo_instalado_es_la_version_fijada_en_requirements():
    """fpdf2 y TODO lo que fpdf2 arrastra (sacado de su Requires-Dist, no tecleado)
    está fijado en requirements.txt a la versión que hay instalada y se probó."""
    pines = _pines()
    nombres = ["fpdf2"] + [re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9._-]+", r).group(0)).lower()
                           for r in requires("fpdf2") if "extra ==" not in r]
    assert len(nombres) > 1
    for n in nombres:
        assert n in pines, f"{n} no está fijada con == en requirements.txt"
        assert version(n) == pines[n], f"{n}: fijada {pines[n]}, instalada {version(n)}"
