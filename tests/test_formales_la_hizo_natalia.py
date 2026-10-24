"""«La hizo Natalia» en la pantalla de Proyectos (pieza 6 de «Cotización formal que hace Natalia», 24-oct-2026).

Qué se vigila (todo por la ruta REAL `GET /proyectos` y la plantilla real, sobre la base SQLite de la suite):
  1. en la lista, la etiqueta sale SOLO en la formal con `cotizaciones.hecha_por = 'natalia'`; una de `equipo`
     no pinta nada nuevo, y la etiqueta sale también en solo ver;
  2. en la ventana de la formal (borrador, corregir o hoja) sale la misma etiqueta, y no sale en la de `equipo`;
  3. la hoja y el PDF que van al cliente NO dicen «Natalia» (los de una formal de Natalia);
  4. el texto que escribió la persona se escapa también en la ventana de una formal de Natalia;
  5. la lectura de la página (`db.cotizaciones_de_proyectos`) trae `hecha_por` de cada formal.

Las formales de Natalia se siembran con su forma real en la base: la fila se guarda como `equipo` por la ruta de
la casa y luego se cambia a `natalia` sin chat (lo que la restricción `cotizaciones_natalia_sin_chat` exige).

FRONTERA: no hay navegador aquí. Lo que se ve en Chrome lo mira la sala en el HTML que escribe
`test_html_para_la_sala` (solo si se pasa la variable `LUCY_SALA_HTML`).

Correr:  python3 -m pytest tests/test_formales_la_hizo_natalia.py -q
"""
from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

from pypdf import PdfReader

import test_guardar_cotizacion  # noqa: F401  (primero: deja puestas las variables de entorno que pide `config`)
from test_pagina_proyectos import gente, mundo  # noqa: F401  (fixtures)
from test_cotizaciones_en_la_pagina import (  # noqa: F401  (fixtures y helpers de la misma página)
    _casa, _contenido, _guardar, _pagina, _seccion_de_cotizaciones, _ver, m,
)
import db.db as db

ETIQUETA = "La hizo Natalia"
CLASE = 'class="sub cz-natalia"'


def _hecha_por_natalia(mundo, cid: int) -> None:
    """Pasa una formal de la casa a `natalia`, con la forma que la base exige (sin chat)."""
    mundo.con.execute("UPDATE cotizaciones SET hecha_por = 'natalia', creado_por_chat_id = NULL WHERE id = ?", (cid,))
    mundo.con.commit()


def _velo(html: str) -> str:
    """La ventana abierta (el velo), sin lo que está fuera de ella."""
    desde = html.index('class="velo"')
    return html[desde:html.index("</section>", desde)]


def _filas(seccion: str) -> list[str]:
    return seccion.split("<li>")[1:]


def _fila_de(seccion: str, codigo: str) -> str:
    return next(f for f in _filas(seccion) if codigo in f)


def test_la_lista_marca_solo_la_formal_que_hizo_natalia(m):
    equipo = _guardar("borrador", contenido=_contenido(2))
    natalia = _guardar("borrador", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    seccion = _seccion_de_cotizaciones(_pagina(_casa(), p=1))
    assert seccion.count(ETIQUETA) == 1
    assert ETIQUETA in _fila_de(seccion, natalia["codigo"])
    fila_equipo = _fila_de(seccion, equipo["codigo"])
    assert ETIQUETA not in fila_equipo and "cz-natalia" not in fila_equipo


def test_en_solo_ver_la_lista_tambien_marca_la_formal_de_natalia(m):
    natalia = _guardar("enviar", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    seccion = _seccion_de_cotizaciones(_pagina(_ver(), p=1))
    assert ETIQUETA in _fila_de(seccion, natalia["codigo"])


def test_la_ventana_de_la_formal_de_natalia_lo_dice_y_la_de_equipo_no(m):
    equipo = _guardar("borrador", contenido=_contenido(1))
    natalia = _guardar("borrador", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    con_natalia = _velo(_pagina(_casa(), p=1, cotizacion=str(natalia["id"])))
    assert CLASE in con_natalia and ETIQUETA in con_natalia
    con_equipo = _velo(_pagina(_casa(), p=1, cotizacion=str(equipo["id"])))
    assert ETIQUETA not in con_equipo and "cz-natalia" not in con_equipo


def test_la_ventana_de_una_enviada_de_natalia_lo_dice_en_la_hoja(m):
    natalia = _guardar("enviar", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    velo = _velo(_pagina(_casa(), p=1, cotizacion=str(natalia["id"])))
    assert ETIQUETA in velo and 'class="previa"' in velo


def test_la_hoja_y_el_pdf_de_natalia_no_dicen_natalia(m):
    natalia = _guardar("enviar", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    velo = _velo(_pagina(_casa(), p=1, cotizacion=str(natalia["id"])))
    sin_etiqueta = velo.replace(f'<span class="sub cz-natalia">{ETIQUETA}</span>', "")
    assert ETIQUETA not in sin_etiqueta and "natalia" not in sin_etiqueta.lower()
    r = _casa().get(f"/proyectos/1/cotizaciones/{natalia['id']}/pdf")
    assert r.status_code == 200
    texto = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)
    assert texto.strip(), "el PDF no trae texto: la prueba no estaría midiendo nada"
    assert "natalia" not in texto.lower()


def test_el_texto_que_escribio_la_persona_se_escapa_en_la_formal_de_natalia(m):
    natalia = _guardar("borrador", contenido=_contenido(1))
    m.con.execute("UPDATE cotizaciones SET para_nombre = '<b>Ana</b>' WHERE id = ?", (natalia["id"],))
    m.con.commit()
    _hecha_por_natalia(m, natalia["id"])
    velo = _velo(_pagina(_casa(), p=1, cotizacion=str(natalia["id"])))
    assert ETIQUETA in velo
    assert "<b>Ana</b>" not in velo
    assert "&lt;b&gt;Ana&lt;/b&gt;" in velo


def test_la_lectura_de_proyectos_trae_quien_la_hizo(m):
    equipo = _guardar("borrador", contenido=_contenido(1))
    natalia = _guardar("borrador", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia["id"])
    filas = asyncio.run(db.cotizaciones_de_proyectos())[1]
    por_id = {f["id"]: f for f in filas}
    assert por_id[natalia["id"]]["hecha_por"] == "natalia"
    assert por_id[equipo["id"]]["hecha_por"] == "equipo"


def test_html_para_la_sala(m):
    """Escribe el HTML que mira la sala en Chrome, solo si `LUCY_SALA_HTML` apunta a una carpeta.
    Sin la variable no escribe nada y pasa: la prueba de arriba es la que vigila."""
    destino = os.environ.get("LUCY_SALA_HTML")
    if not destino:
        return
    carpeta = Path(destino)
    carpeta.mkdir(parents=True, exist_ok=True)
    equipo_b = _guardar("borrador", contenido=_contenido(1))
    equipo_e = _guardar("enviar", contenido=_contenido(2))
    natalia_b = _guardar("borrador", contenido=_contenido(1))
    natalia_e = _guardar("enviar", contenido=_contenido(1))
    _hecha_por_natalia(m, natalia_b["id"])
    _hecha_por_natalia(m, natalia_e["id"])
    (carpeta / "lista.html").write_text(_pagina(_casa(), p=1), encoding="utf-8")
    (carpeta / "ventana_natalia_borrador.html").write_text(
        _pagina(_casa(), p=1, cotizacion=str(natalia_b["id"])), encoding="utf-8")
    (carpeta / "ventana_natalia_enviada.html").write_text(
        _pagina(_casa(), p=1, cotizacion=str(natalia_e["id"])), encoding="utf-8")
    (carpeta / "ventana_equipo_enviada.html").write_text(
        _pagina(_casa(), p=1, cotizacion=str(equipo_e["id"])), encoding="utf-8")
    assert equipo_b["codigo"] and equipo_e["codigo"]
