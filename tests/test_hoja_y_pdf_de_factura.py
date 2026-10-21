"""La hoja de una factura, su vista previa y su PDF (pieza 3 del diseño «Facturas», 21-oct-2026):
`facturas.hoja_de_factura`, `facturas.pdf_de_factura`, la macro `web/plantillas/_hoja_de_factura.html` y la ruta
`GET /facturas/{fid}/pdf`.

Qué se vigila:
  1. la hoja (función pura): «FACTURA» y su número, para quién (RNC o cédula solo si lo lleva), fecha en palabras,
     NCF y su tipo SOLO si los lleva (sin ellos no queda hueco ni texto), subtotal e ITBIS solo con ITBIS y el
     total siempre; el borrador dice «BORRADOR» y NUNCA un número (aunque el diccionario trajera uno); la anulada
     dice «ANULADA» y el motivo; un estado que no se conoce sale como borrador;
  2. los montos de la hoja son los de `db.totales_de_factura` (la hoja no recalcula: escribe lo que trae);
  3. el texto del PDF, leído de vuelta con `pypdf`, es el de la hoja sin blancos y en su orden; la marca en diagonal
     sale en CADA página (borrador y anulada) y en ninguna de una emitida; nada se sale de la carta con todo al tope
     de lo que acepta la puerta de las facturas;
  4. la vista previa HTML trae los mismos textos, escapados;
  5. HERMANOS: la factura y la cotización dibujan con las mismas piezas (la primera página, el pie, la marca) y
     escriben el dinero con la misma función;
  6. la ruta, sobre la lectura REAL (`db.factura_por_id`) con SQLite: solo la casa (solo ver, sin sesión, cookie
     falsa y una sesión válida de alguien que no entra al panel: 401), `application/pdf` con `attachment`, el número
     como nombre (el borrador no lo tiene), 404 con la que no existe o sin la tabla, y no escribe nada.

FRONTERA, dicha una vez:
  · Se lee el TEXTO del PDF, no su aspecto: colores, tamaños y posiciones no los comprueba ninguna prueba (los PDF
    de ejemplo se miran a ojo). `pypdf` puede juntar o partir blancos, por eso se compara sin blancos.
  · La macro se prueba renderizada con el motor de plantillas de la aplicación; el navegador no interviene.
  · SQLite en vez de Postgres (ver `tests/test_guardar_factura.py`).

Correr:  python3 -m pytest tests/test_hoja_y_pdf_de_factura.py -q
"""
from __future__ import annotations

import copy
import io
import re
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse y después el cliente ya no carga)
from pypdf import PdfReader

import test_guardar_factura as tgf          # primero: deja puestas las variables de entorno que pide `config`
from test_guardar_factura import mundo  # noqa: F401  (la prueba usa la fixture)
import test_hoja_y_pdf_de_cotizacion as thc
from test_hoja_y_pdf_de_cotizacion import espia_del_borde, _lo_que_se_sale  # noqa: F401  (fixture y ayuda)

import config
import cotizaciones as cz
import db.db as db
import facturas as fa
import web.app as panel
import web.auth as auth
from _navegador import Navegador


# ── Una factura de muestra, con la forma que devuelve `db.factura_por_id` ───────────────────────────

def _f(estado="emitida", *, itbis="18", ncf=True, rnc="131234567", renglones=None, motivo=None, codigo=None, **cambios):
    renglones = renglones or [{"orden": 1, "descripcion": "Grabación de bajo y güira", "detalle": "Incluye mezcla.", "monto": Decimal("62500.00")},
                              {"orden": 2, "descripcion": "Video musical", "detalle": "Una toma.\nSegunda línea.", "monto": Decimal("15250.50")}]
    pct = None if itbis is None else Decimal(itbis)
    base = {
        "id": 7, "anio": 2026, "numero": 1, "estado": estado,
        "codigo": codigo if codigo is not None else ("FAC-2026-001" if estado in ("emitida", "anulada") else None),
        "para_nombre": "Cliente de prueba", "rnc_cedula": rnc, "fecha": "2026-10-14",
        "ncf": "B0100000001" if ncf else None, "ncf_tipo": "Crédito fiscal" if ncf else None,
        "itbis_pct": pct, "motivo_anulacion": motivo, "renglones": renglones,
    }
    base.update(cambios)
    base.update(db.totales_de_factura([r["monto"] for r in renglones], pct))
    return base


def _sin_blancos(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _texto_del_pdf(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _sin_las_ultimas(texto: str, palabra: str, cuantas: int) -> str:
    for _ in range(cuantas):
        antes, esta, despues = texto.rpartition(palabra)
        assert esta, f"faltan marcas {palabra}"
        texto = antes + despues
    return texto


def _paginas(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def _pdf(f: dict) -> bytes:
    return fa.pdf_de_factura(fa.hoja_de_factura(f))


def _cuantas(palabra: str, pdf: bytes) -> int:
    return _texto_del_pdf(pdf).count(palabra)


# ── 1. La hoja, función pura ────────────────────────────────────────────────────────────────────────

def test_la_emitida_con_ncf_e_itbis_trae_todo_y_ninguna_marca():
    h = fa.hoja_de_factura(_f())
    assert (h["titulo"], h["codigo"], h["marca"]) == ("FACTURA", "FAC-2026-001", None)
    assert h["para"] == [("PARA", "Cliente de prueba"), ("RNC / CÉDULA", "131234567"), ("FECHA", "14 de octubre de 2026"),
                         ("NCF", "B0100000001"), ("TIPO DE NCF", "Crédito fiscal")]
    assert h["encabezados"] == ("DESCRIPCIÓN", "MONTO")
    assert [(r["descripcion"], r["detalle"], r["monto"]) for r in h["renglones"]] == [
        ("Grabación de bajo y güira", "Incluye mezcla.", "RD$62,500"),
        ("Video musical", "Una toma.\nSegunda línea.", "RD$15,250.50")]
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [
        ("Subtotal", "RD$77,750.50"), ("ITBIS 18%", "RD$13,995.09"), ("TOTAL", "RD$91,745.59")]
    assert h["pie"] == cz.PIE_DEL_ESTUDIO


def test_sin_ncf_sin_itbis_y_sin_rnc_no_queda_ningun_hueco_ni_texto():
    h = fa.hoja_de_factura(_f(itbis=None, ncf=False, rnc=None))
    textos = " | ".join(fa.textos_de_la_factura(h))
    for palabra in ("NCF", "ITBIS", "Subtotal", "RNC", "CÉDULA", "None", "TIPO"):
        assert palabra not in textos, palabra
    assert [p[0] for p in h["para"]] == ["PARA", "FECHA"]
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [("TOTAL", "RD$77,750.50")]


def test_el_ncf_y_el_itbis_son_cosas_distintas():
    solo_ncf = fa.textos_de_la_factura(fa.hoja_de_factura(_f(itbis=None)))
    assert "NCF" in solo_ncf and not any("ITBIS" in t for t in solo_ncf)
    solo_itbis = fa.textos_de_la_factura(fa.hoja_de_factura(_f(ncf=False)))
    assert "ITBIS 18%" in solo_itbis and "NCF" not in solo_itbis


def test_el_borrador_dice_borrador_y_no_tiene_numero():
    h = fa.hoja_de_factura(_f("borrador"))
    assert (h["titulo"], h["codigo"], h["marca"]) == ("FACTURA", "BORRADOR", "BORRADOR")
    assert not any("FAC-" in t for t in fa.textos_de_la_factura(h))


def test_un_borrador_con_un_numero_en_el_diccionario_tampoco_lo_muestra():
    h = fa.hoja_de_factura(_f("borrador", codigo="FAC-2026-099"))
    assert h["codigo"] == "BORRADOR" and not any("FAC-" in t for t in fa.textos_de_la_factura(h))


def test_la_anulada_dice_anulada_con_el_motivo_y_conserva_su_numero():
    h = fa.hoja_de_factura(_f("anulada", motivo="Se facturó dos veces"))
    assert (h["codigo"], h["marca"]) == ("FAC-2026-001", "ANULADA")
    assert ("ESTADO", "ANULADA") in h["para"] and ("MOTIVO DE LA ANULACIÓN", "Se facturó dos veces") in h["para"]


def test_el_nombre_del_archivo_de_un_borrador_no_lleva_numero_aunque_el_diccionario_lo_traiga():
    assert fa.nombre_del_archivo(_f()) == "FAC-2026-001.pdf"
    assert fa.nombre_del_archivo(_f("anulada", motivo="x")) == "FAC-2026-001.pdf"
    assert fa.nombre_del_archivo(_f("borrador")) == "Factura-borrador-7.pdf"
    assert fa.nombre_del_archivo(_f("borrador", codigo="FAC-2026-099")) == "Factura-borrador-7.pdf"
    assert fa.nombre_del_archivo(_f("quien-sabe", codigo="FAC-2026-099")) == "Factura-borrador-7.pdf"


def test_la_emitida_no_dice_ni_borrador_ni_anulada():
    textos = " ".join(fa.textos_de_la_factura(fa.hoja_de_factura(_f())))
    assert "BORRADOR" not in textos and "ANULADA" not in textos and "MOTIVO" not in textos


@pytest.mark.parametrize("estado", ["descartada", "", "EMITIDA", None])
def test_un_estado_que_no_se_conoce_sale_como_borrador(estado):
    h = fa.hoja_de_factura(_f(estado, codigo="FAC-2026-005"))
    assert (h["codigo"], h["marca"]) == ("BORRADOR", "BORRADOR")


def test_la_hoja_es_pura_no_toca_lo_que_recibe():
    f = _f("anulada", motivo="x")
    antes = copy.deepcopy(f)
    fa.hoja_de_factura(f)
    assert f == antes


# ── 2. Los montos son los de `db.totales_de_factura` ────────────────────────────────────────────────

@pytest.mark.parametrize("montos,pct", [
    (["62500.00", "15250.50"], "18"), (["0.25"], "18"), (["0.03"], "18"), (["99999999.99", "99999999.99"], "18"),
    (["100"], None), (["0.01", "0.01", "0.01"], "18"), (["1234.56"], "18")])
def test_subtotal_itbis_y_total_de_la_hoja_son_los_de_totales_de_factura(montos, pct):
    renglones = [{"orden": i, "descripcion": f"R{i}", "detalle": "", "monto": Decimal(m)} for i, m in enumerate(montos, 1)]
    h = fa.hoja_de_factura(_f(renglones=renglones, itbis=pct))
    esperado = db.totales_de_factura([Decimal(m) for m in montos], None if pct is None else Decimal(pct))
    por_etiqueta = {t["etiqueta"]: t["total"] for t in h["totales"]}
    assert por_etiqueta["TOTAL"] == cz.dinero_en_pesos(esperado["total"])
    if pct is None:
        assert list(por_etiqueta) == ["TOTAL"]
    else:
        assert por_etiqueta["Subtotal"] == cz.dinero_en_pesos(esperado["subtotal"])
        assert por_etiqueta["ITBIS 18%"] == cz.dinero_en_pesos(esperado["itbis"])


def test_la_hoja_no_recalcula_escribe_lo_que_trae_la_lectura():
    f = _f()
    f.update(subtotal=Decimal("1.00"), itbis=Decimal("2.00"), total=Decimal("3.00"))
    h = fa.hoja_de_factura(f)
    assert [(t["etiqueta"], t["total"]) for t in h["totales"]] == [("Subtotal", "RD$1"), ("ITBIS 18%", "RD$2"), ("TOTAL", "RD$3")]


def test_la_hoja_de_lo_que_lee_la_base_lleva_los_totales_de_la_base(mundo):
    f = tgf.crear(itbis_pct=18, renglones=[{"descripcion": "A", "monto": "0.25"}, {"descripcion": "B", "monto": "10.10"}])
    leida = tgf.leer(f)
    h = fa.hoja_de_factura(leida)
    esperado = db.totales_de_factura([r["monto"] for r in leida["renglones"]], leida["itbis_pct"])
    assert [t["total"] for t in h["totales"]] == [cz.dinero_en_pesos(esperado[k]) for k in ("subtotal", "itbis", "total")]


# ── 3. El PDF ───────────────────────────────────────────────────────────────────────────────────────

CASOS = {
    "emitida": lambda: _f(),
    "emitida sin nada": lambda: _f(itbis=None, ncf=False, rnc=None),
    "borrador": lambda: _f("borrador"),
    "anulada": lambda: _f("anulada", motivo="Se facturó dos veces; ver FAC-2026-002"),
}


@pytest.mark.parametrize("caso", list(CASOS))
def test_el_texto_del_pdf_es_el_de_la_hoja_y_en_su_orden(caso):
    f = CASOS[caso]()
    pdf = _pdf(f)
    assert pdf.startswith(b"%PDF")
    esperado = _sin_blancos("".join(fa.textos_de_la_factura(fa.hoja_de_factura(f))))
    leido = _sin_blancos(_texto_del_pdf(pdf))
    marca = fa.marca_de_la_factura(f["estado"])
    if marca:       # la marca se dibuja al final de cada página: se quitan las últimas `páginas` apariciones
        leido = _sin_las_ultimas(leido, marca, _paginas(pdf))
    assert leido == esperado


def _muchos(n=45):
    return [{"orden": i, "descripcion": f"Servicio número {i}", "detalle": "Detalle largo. " * 8, "monto": Decimal("1500.25")}
            for i in range(1, n + 1)]


def test_la_marca_de_borrador_sale_en_cada_pagina_y_en_la_emitida_en_ninguna():
    pdf = _pdf(_f("borrador", renglones=_muchos()))
    paginas = _paginas(pdf)
    assert paginas >= 3
    assert _cuantas("BORRADOR", pdf) == 1 + paginas        # el lugar del número + una marca por página
    assert not _cuantas("FAC-", pdf)
    sin = _pdf(_f(renglones=_muchos()))
    assert _paginas(sin) >= 3 and not _cuantas("BORRADOR", sin) and not _cuantas("ANULADA", sin)


def test_la_marca_de_anulada_sale_en_cada_pagina_con_el_motivo_y_el_numero():
    pdf = _pdf(_f("anulada", motivo="Cliente cancelado", renglones=_muchos()))
    paginas = _paginas(pdf)
    assert paginas >= 3
    assert _cuantas("ANULADA", pdf) == 1 + paginas         # el «ESTADO» + una marca por página
    leido = _sin_blancos(_texto_del_pdf(pdf))
    assert "Clientecancelado" in leido and "FAC-2026-001" in leido
    assert not _cuantas("BORRADOR", pdf)


def test_el_pdf_con_muchos_renglones_no_pierde_texto_y_los_totales_quedan_al_final():
    f = _f(renglones=_muchos(60))
    pdf = _pdf(f)
    leido = _sin_blancos(_texto_del_pdf(pdf))
    paginas = _paginas(pdf)
    assert paginas >= 4
    cabecera = _sin_blancos(" ".join(fa.ENCABEZADOS_DE_LA_FACTURA))
    assert leido.count(cabecera) == paginas          # cada página repite la cabecera de la tabla
    esperado = _sin_blancos("".join(fa.textos_de_la_factura(fa.hoja_de_factura(f))))
    assert leido.replace(cabecera, "") == esperado.replace(cabecera, "")
    assert leido.index("Servicionúmero60") < leido.index("Subtotal") < leido.index("ITBIS18%") < leido.rindex("TOTAL")


def _factura_al_tope_de_la_puerta(letra):
    return _f(para_nombre=letra * 200, ncf_tipo=letra * 20, rnc="9" * 11,
              renglones=[{"orden": 1, "descripcion": letra * 300, "detalle": letra * 2000, "monto": Decimal("99999999.99")},
                         {"orden": 2, "descripcion": "Otro", "detalle": "", "monto": Decimal("99999999.99")}])


@pytest.mark.parametrize("letra", ["W", "i", "Ñ"])
def test_nada_se_sale_del_borde_de_la_carta_con_todo_al_tope_de_la_puerta(espia_del_borde, letra):
    f = _factura_al_tope_de_la_puerta(letra)
    f["estado"], f["codigo"], f["motivo_anulacion"] = "anulada", "FAC-2026-001", letra * 500
    _pdf(f)
    assert len(espia_del_borde) > 15
    assert _lo_que_se_sale(espia_del_borde) == []


def test_el_nombre_largo_sale_entero_y_la_columna_crece_para_el_total_mas_grande():
    f = _factura_al_tope_de_la_puerta("W")
    leido = _sin_blancos(_texto_del_pdf(_pdf(f)))
    assert "W" * 200 in leido and "W" * 300 in leido
    assert "RD$199,999,999.98" in leido and "RD$36,000,000" in leido        # el subtotal y el ITBIS (18 %, redondeado) de dos montos tope


def test_la_letra_de_fabrica_no_dibuja_la_raya_larga():
    """La frontera que explica por qué la puerta de las facturas cambia las rayas antes de guardar (`_texto_de_factura`)."""
    with pytest.raises(Exception):
        _pdf(_f(para_nombre="Raya — larga"))


# ── 4. La vista previa HTML ─────────────────────────────────────────────────────────────────────────

class _Textos(HTMLParser):
    def __init__(self):
        super().__init__()
        self.textos: list[str] = []
        self.etiquetas: list[tuple] = []
        self._dentro_de_style = False

    def handle_starttag(self, tag, attrs):
        self.etiquetas.append((tag, dict(attrs)))
        self._dentro_de_style = tag == "style"

    def handle_endtag(self, tag):
        self._dentro_de_style = False

    def handle_data(self, data):
        if not self._dentro_de_style and data.strip():
            self.textos.append(data)


def _html(h: dict) -> str:
    return str(panel.plantillas.env.get_template("_hoja_de_factura.html").module.hoja_de_factura(h))


@pytest.mark.parametrize("caso", list(CASOS))
def test_la_vista_previa_trae_los_mismos_textos_que_la_hoja(caso):
    h = fa.hoja_de_factura(CASOS[caso]())
    p = _Textos()
    p.feed(_html(h))
    leido = _sin_blancos("".join(p.textos))
    esperado = _sin_blancos("".join(fa.textos_de_la_factura(h)))
    if h["marca"]:
        esperado = _sin_blancos("".join(fa.textos_de_la_factura(h)[:2] + [h["marca"]] + fa.textos_de_la_factura(h)[2:]))
    assert leido == esperado
    assert any(t == "img" and a.get("src") == "/logo-cds.png" for t, a in p.etiquetas)


def test_la_vista_previa_escapa_lo_que_se_escribio():
    h = fa.hoja_de_factura(_f(para_nombre="<script>alert(1)</script> & <b>x</b>"))
    html = _html(h)
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in html


def test_la_vista_previa_no_pone_marca_en_la_emitida_y_si_en_las_otras():
    assert "marca-f" not in _html(fa.hoja_de_factura(_f()))
    assert "marca-f" in _html(fa.hoja_de_factura(_f("borrador"))) and "marca-f" in _html(fa.hoja_de_factura(_f("anulada", motivo="x")))


# ── 5. Hermanos: la factura y la cotización ─────────────────────────────────────────────────────────

def test_hermanos_la_marca_la_dibuja_la_misma_funcion_y_con_su_palabra(monkeypatch):
    llamadas = []
    real = cz._marcar_de_borrador
    monkeypatch.setattr(cz, "_marcar_de_borrador", lambda pdf, marca=cz.MARCA_DE_BORRADOR: (llamadas.append(marca), real(pdf, marca))[1])
    cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(thc._cot()), borrador=True)
    _pdf(_f("borrador"))
    _pdf(_f("anulada", motivo="x"))
    _pdf(_f())
    cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(thc._cot()))
    assert llamadas == ["BORRADOR", "BORRADOR", "ANULADA"]


def test_hermanos_la_primera_pagina_y_el_pie_son_los_mismos():
    cot = _texto_del_pdf(cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(thc._cot())))
    fac = _texto_del_pdf(_pdf(_f()))
    for linea in cz.PIE_DEL_ESTUDIO:
        assert _sin_blancos(linea) in _sin_blancos(cot) and _sin_blancos(linea) in _sin_blancos(fac)
    # las dos usan la misma primera página: el logo y el título/código al centro (las llama el mismo escritor)
    llamadas = []
    real = cz._abrir_la_hoja
    import unittest.mock as mock
    with mock.patch.object(cz, "_abrir_la_hoja", side_effect=lambda h, l: (llamadas.append(h["titulo"]), real(h, l))[1]):
        cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(thc._cot()))
        _pdf(_f())
    assert llamadas == ["COTIZACIÓN DE SERVICIOS", "FACTURA"]


def test_hermanos_el_dinero_se_escribe_con_la_misma_funcion():
    for valor in ("62500.00", "62500.50", "0.05", "1234567.89"):
        c = thc._cot()
        c["contenido"]["opciones"][0]["renglones"][0]["total"] = valor
        f = _f(renglones=[{"orden": 1, "descripcion": "x", "detalle": "", "monto": Decimal(valor)}], itbis=None)
        assert fa.hoja_de_factura(f)["renglones"][0]["monto"] == cz.hoja_de_cotizacion(c)["opciones"][0]["renglones"][0]["total"]


def test_la_marca_de_la_cotizacion_sigue_en_cada_pagina():
    c = thc._cot()
    c["contenido"]["opciones"] = [{"nombre": "A", "renglones": [{"descripcion": f"Renglón {i}", "detalle": "Texto. " * 30, "total": "10.00"} for i in range(40)]}]
    pdf = cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(c), borrador=True)
    assert _paginas(pdf) >= 3 and _cuantas("BORRADOR", pdf) == _paginas(pdf)


# ── 6. La ruta, sobre la lectura real con SQLite ────────────────────────────────────────────────────

def _cliente(sesion):
    c = Navegador(panel.app, base_url="https://testserver")
    if sesion == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    elif sesion == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    elif sesion == "ajena":
        c.cookies.set(panel.COOKIE, auth.crear_token(tgf.EXTRANO, auth.VIDA_SESION))     # bien firmada, de alguien que no entra
    elif sesion == "falsa":
        c.cookies.set(panel.COOKIE_VER, "ver.9999999999.firma-falsa")
        c.cookies.set(panel.COOKIE, "9999999999.firma-falsa")
    return c


def _url(fid):
    return f"/facturas/{fid}/pdf"


def test_la_casa_baja_la_emitida_con_sus_cabeceras(mundo):
    f = tgf.emitir(tgf.crear(itbis_pct=18, ncf="B0100000001", ncf_tipo="Crédito fiscal", rnc_cedula="131-23456-7"))
    resp = _cliente("casa").get(_url(f["id"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="FAC-2026-001.pdf"'
    assert "private" in resp.headers["cache-control"] and "no-store" in resp.headers["cache-control"]
    assert resp.content.startswith(b"%PDF")
    leido = _sin_blancos(_texto_del_pdf(resp.content))
    assert "FAC-2026-001" in leido and "B0100000001" in leido and "131234567" in leido and "ITBIS18%" in leido
    assert "BORRADOR" not in leido and "ANULADA" not in leido


def test_el_pdf_de_la_ruta_es_el_de_la_hoja_de_lo_que_lee_la_base(mundo):
    f = tgf.emitir(tgf.crear(itbis_pct=18))
    resp = _cliente("casa").get(_url(f["id"]))
    esperado = _sin_blancos("".join(fa.textos_de_la_factura(fa.hoja_de_factura(tgf.leer(f)))))
    assert _sin_blancos(_texto_del_pdf(resp.content)) == esperado


def test_el_borrador_se_baja_con_su_marca_sin_numero_y_con_otro_nombre(mundo):
    f = tgf.crear()
    resp = _cliente("casa").get(_url(f["id"]))
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == f'attachment; filename="Factura-borrador-{f["id"]}.pdf"'
    assert _cuantas("BORRADOR", resp.content) == 1 + _paginas(resp.content) and not _cuantas("FAC-", resp.content)


def test_la_anulada_se_baja_con_su_marca_y_su_motivo(mundo):
    f = tgf.emitir(tgf.crear(ncf="B0100000009", ncf_tipo="Consumo"))
    tgf.correr(db.anular_factura(f["id"], tgf.CASA, "Se facturó dos veces"))
    resp = _cliente("casa").get(_url(f["id"]))
    assert resp.status_code == 200 and 'filename="FAC-2026-001.pdf"' in resp.headers["content-disposition"]
    leido = _sin_blancos(_texto_del_pdf(resp.content))
    assert "Sefacturódosveces" in leido and _cuantas("ANULADA", resp.content) == 1 + _paginas(resp.content)
    assert "B0100000009" in leido


@pytest.mark.parametrize("sesion", [None, "ver", "ajena", "falsa"])
def test_solo_la_casa_baja_el_pdf_todo_lo_demas_es_401(mundo, sesion):
    f = tgf.emitir(tgf.crear())
    resp = _cliente(sesion).get(_url(f["id"]))
    assert resp.status_code == 401
    assert not resp.content.startswith(b"%PDF") and "attachment" not in resp.headers.get("content-disposition", "")


def test_la_ruta_esta_declarada_solo_para_la_casa():
    rutas = [r for r in panel.app.routes if getattr(r, "path", None) == "/facturas/{fid}/pdf"]
    assert len(rutas) == 1 and rutas[0].endpoint.puerta == auth.PUERTA_SIEMPRE and rutas[0].methods == {"GET"}


def test_la_que_no_existe_es_404_y_sin_la_tabla_tambien(mundo):
    f = tgf.emitir(tgf.crear())
    c = _cliente("casa")
    assert c.get(_url(999)).status_code == 404
    for t in ("factura_envios", "factura_ligas", "factura_renglones", "facturas"):
        mundo.con.execute(f"DROP TABLE {t}")
    assert c.get(_url(f["id"])).status_code == 404


def test_la_ruta_es_de_lectura_no_escribe_nada(mundo):
    f = tgf.emitir(tgf.crear(itbis_pct=18))
    antes = tgf.foto(mundo)
    _cliente("casa").get(_url(f["id"]))
    _cliente("ver").get(_url(f["id"]))
    assert tgf.foto(mundo) == antes
