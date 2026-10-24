"""La tabla de cuatro columnas de la factura después de moverla a `cotizaciones.py` (pieza 1 de «Cotización con
detalle», 24-oct-2026). La factura tiene que salir IGUAL que antes del movimiento.

Compara, para siete facturas de muestra, contra el volcado de la referencia `75fc17d`
(`tests/fixtures/factura_pieza1_75fc17d.json`, generado con `git archive 75fc17d` y las mismas muestras de abajo):
  · la hoja (el diccionario que alimenta la macro HTML),
  · el número de páginas,
  · el flujo de dibujo de cada página (el contenido del PDF, con un hash por página),
  · el texto de cada página.

Qué vigila, por caso: el tope de la columna de la cantidad (`_TOPE_DE_LA_CANTIDAD`: la cantidad larga se parte en dos
líneas; lo vigilan `cantidades_largas` y `muchos_renglones`), la negrita cursiva de «Neto a pagar» (la rama
`ESTILO_DEL_NETO` de `_fila_de_un_total`; la vigilan las facturas con NCF) y la repetición de la cabecera al saltar de
página (`muchos_renglones`).

FRONTERA: compara el dibujo por su flujo y el texto, no por la imagen. Si la factura cambia a propósito, se regenera el
volcado con `git archive` de la referencia buena y se cambia el fixture en el mismo commit; no se afloja la prueba.
"""
from __future__ import annotations

import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader

import test_guardar_factura as tgf  # noqa: F401  (primero: deja puestas las variables de entorno que pide `config`)
import db.db as db
import facturas as fa

REFERENCIA = Path(__file__).parent / "fixtures" / "factura_pieza1_75fc17d.json"


def _renglon(i, desc, det, cant, unidad, precio):
    return {"orden": i, "descripcion": desc, "detalle": det, "cantidad": Decimal(cant), "unidad": unidad,
            "precio_unitario": Decimal(precio), "monto": Decimal(cant) * Decimal(precio)}


def _factura(estado="emitida", *, itbis="18", ncf=True, rnc="131234567", renglones=None, motivo=None, codigo=None,
             **cambios):
    renglones = renglones or [
        _renglon(1, "Grabación de bajo y güira", "Incluye mezcla.", "2", "horas", "31250.00"),
        _renglon(2, "Video musical", "Una toma.\nSegunda línea.", "1", "video", "15250.50")]
    pct = None if itbis is None else Decimal(itbis)
    base = {"id": 7, "anio": 2026, "numero": 1, "estado": estado,
            "codigo": codigo if codigo is not None else ("FAC-2026-001" if estado in ("emitida", "anulada") else None),
            "para_nombre": "Cliente de prueba", "rnc_cedula": rnc, "fecha": "2026-10-14",
            "ncf": "B0100000001" if ncf else None, "ncf_tipo": "Crédito fiscal" if ncf else None,
            "itbis_pct": pct, "motivo_anulacion": motivo, "renglones": renglones}
    base.update(cambios)
    base.update(db.totales_de_factura([r["monto"] for r in renglones], pct, con_ncf=bool(ncf)))
    return base


_VIEJO = {"orden": 1, "descripcion": "Renglón de antes del detalle", "detalle": None, "monto": Decimal("4000.00")}

MUESTRAS = {
    "emitida_ncf_itbis": lambda: _factura(),
    "borrador_sin_nada": lambda: _factura("borrador", itbis=None, ncf=False, rnc=None),
    "anulada_motivo": lambda: _factura("anulada", motivo="Error de monto"),
    "emitida_sin_ncf_con_descuento": lambda: _factura(itbis="18", ncf=False, descuento=Decimal("500.00")),
    "renglon_de_antes": lambda: _factura(renglones=[_VIEJO]),
    "cantidades_largas": lambda: _factura(renglones=[
        _renglon(1, "Servicio con un nombre bastante largo para forzar el salto de la columna", None,
                 "99999999.99", "unidades", "99999999.99")]),
    "muchos_renglones": lambda: _factura(renglones=[
        _renglon(i, f"Renglón número {i}", "detalle " * (i % 4), str(i), "horas", "1250.25") for i in range(1, 46)]),
}


def _referencia() -> dict:
    return json.loads(REFERENCIA.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _sin_emisor_en_el_entorno(monkeypatch):
    # La referencia se hizo sin las variables del emisor: el nombre y el RNC no deben colarse desde el entorno.
    monkeypatch.delenv("FACTURA_EMISOR_NOMBRE", raising=False)
    monkeypatch.delenv("FACTURA_EMISOR_RNC", raising=False)


@pytest.mark.parametrize("caso", sorted(MUESTRAS))
def test_la_factura_sale_igual_que_en_la_referencia(caso):
    ref = _referencia()[caso]
    hoja = fa.hoja_de_factura(MUESTRAS[caso]())
    hoja_json = json.loads(json.dumps(hoja, sort_keys=True, default=str))
    assert hoja_json == ref["hoja"], f"{caso}: la hoja cambió"

    lector = PdfReader(io.BytesIO(fa.pdf_de_factura(hoja)))
    assert len(lector.pages) == ref["paginas"], f"{caso}: número de páginas"
    flujos = [hashlib.sha256(p.get_contents().get_data()).hexdigest() for p in lector.pages]
    assert flujos == ref["flujos_sha256"], f"{caso}: el dibujo del PDF cambió (tabla, franja o neto)"
    assert [p.extract_text() for p in lector.pages] == ref["texto"], f"{caso}: el texto del PDF cambió"
