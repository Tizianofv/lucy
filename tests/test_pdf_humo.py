"""Humo de la herramienta de PDF (fpdf2): se importa, arma un PDF de una página
en memoria y lo que se escribió se puede leer de vuelta. No escribe a disco.

Lo que NO prueba: ninguna hoja real de Lucy (todavía no existe el escritor).
Dicho para el que construya el PDF de verdad: con la letra de serie `helvetica`
los acentos y la «ñ» salen, pero «—» no (levanta FPDFUnicodeEncodingException).
"""
import io
import re
from importlib.metadata import version
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


def test_lo_instalado_es_la_version_fijada_en_requirements():
    fijada = None
    for linea in (RAIZ / "requirements.txt").read_text().splitlines():
        m = re.match(r"^fpdf2==(\S+)\s*$", linea.split("#", 1)[0])
        if m:
            fijada = m.group(1)
    assert fijada, "fpdf2 no está fijada con == en requirements.txt"
    assert version("fpdf2") == fijada
