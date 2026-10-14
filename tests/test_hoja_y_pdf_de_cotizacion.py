"""La hoja de una cotización, su vista previa y su PDF (pieza 4 del diseño «Cotizaciones en Proyectos»,
14-oct-2026): `cotizaciones.hoja_de_cotizacion`, `cotizaciones.pdf_de_cotizacion`, la macro
`web/plantillas/_hoja_de_cotizacion.html` y la ruta `GET /proyectos/{pid}/cotizaciones/{cid}/pdf`.

Qué se vigila:
  1. la hoja (función pura): textos, dinero («RD$62,500», con centavos «RD$62,500.50»), fecha en palabras,
     «Total de la opción» solo con más de un renglón, SOLO las condiciones marcadas, pie del estudio;
  2. el texto del PDF, leído de vuelta con `pypdf`, es el de `hoja_de_cotizacion` (sin blancos, en su orden),
     sin las condiciones desmarcadas; varias páginas si no caben, sin perder texto;
  3. todo carácter del plano básico (U+0020 a U+FFFF, sin sustitutos) que la puerta del contenido deja pasar
     se dibuja con la letra de fábrica y se lee de vuelta en TODOS los campos (la lista sale de preguntarle a
     la puerta carácter por carácter); fuera de ese plano solo se prueban tres tecleados (emoji, «☺», «😀»);
     «—» y las comillas tipográficas llegan al PDF ya cambiadas por su forma simple;
  3-bis. nada de lo que se dibuja se sale de la carta: nombre del cliente (tope de la puerta, 200), título de
     la opción (tope, 120), teléfono, descripción, detalle, condición y los totales más grandes se ajustan en
     varias líneas o ensanchan su columna (se mide cada `cell` y `multi_cell` que pinta el escritor);
  4. la vista previa HTML trae los mismos textos que la hoja, escapados;
  5. la ruta: `application/pdf` con `attachment` y el código como nombre; la bajan la casa y solo ver; sin
     sesión 401; 404 con la cotización de otro proyecto, una que no existe, una borrada o sin la tabla.
     La ruta corre sobre la lectura REAL (`db.cotizaciones_de_proyectos`) con SQLite.

FRONTERA, dicha una vez:
  · Se lee el texto del PDF, no su aspecto: colores, tamaños y posiciones no los comprueba ninguna prueba (el
    PDF de ejemplo se mira a ojo). `pypdf` puede juntar o partir blancos, por eso se compara sin blancos.
  · Los caracteres de espacio y de formato (U+00A0, U+00AD) se dibujan sin error pero no se comparan al
    leerlos de vuelta (la extracción de `pypdf` los trata como quiere).
  · La macro se prueba renderizada con el motor de plantillas de la aplicación; el navegador no interviene.
  · SQLite en vez de Postgres (ver `tests/test_guardar_cotizacion.py`).

Correr:  python3 -m pytest tests/test_hoja_y_pdf_de_cotizacion.py -q
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import re
import unicodedata
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse y después el cliente ya no carga)
from pypdf import PdfReader

import test_guardar_cotizacion as tg      # primero: deja puestas las variables de entorno que pide `config`
from test_guardar_cotizacion import con  # noqa: F401  (la prueba usa la fixture)

import config
import cotizaciones as cz
import db.db as db
import web.app as panel
import web.auth as auth
from _navegador import Navegador


# ── Una cotización de muestra, con la forma que devuelve `db.cotizaciones_de_proyectos` ─────────────

def _cot(**cambios):
    base = {
        "id": 1, "proyecto_id": 1, "codigo": "COT-2026-009", "para_nombre": "Cliente de prueba",
        "para_telefono": "809-555-0101", "fecha": "2026-10-14", "validez_dias": 15,
        "contenido": {
            "opciones": [
                {"nombre": "Músicos formados", "renglones": [
                    {"descripcion": "Grabación de bajo y güira", "detalle": "Incluye mezcla y máster.", "total": "62500.00"}]},
                {"nombre": "Músicos graduados", "renglones": [
                    {"descripcion": "Grabación completa", "detalle": "", "total": "72500.00"},
                    {"descripcion": "Video musical", "detalle": "Una toma.\nSegunda línea.", "total": "15250.50"}]}],
            "condiciones": [
                {"clave": "validez", "texto": "Esta cotización tiene validez de 15 días a partir de la fecha de emisión.", "marcada": True},
                {"clave": "deposito", "texto": "Se requiere un depósito del 50%.", "marcada": True},
                {"clave": "impuestos", "texto": "TEXTO-DE-LA-CONDICION-DESMARCADA", "marcada": False},
                {"clave": None, "texto": "Una propia.", "marcada": True}]},
    }
    base.update(cambios)
    return base


def _sin_blancos(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _texto_del_pdf(pdf: bytes) -> str:
    lector = PdfReader(io.BytesIO(pdf))
    return "\n".join(p.extract_text() for p in lector.pages)


def _paginas(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


# ── 1. La hoja, función pura ────────────────────────────────────────────────────────────────────────

def test_la_hoja_trae_los_textos_de_la_maqueta():
    h = cz.hoja_de_cotizacion(_cot())
    assert h["titulo"] == "COTIZACIÓN DE SERVICIOS" and h["codigo"] == "COT-2026-009"
    assert h["para"] == [("PARA", "Cliente de prueba"), ("TELÉFONO", "809-555-0101"),
                         ("FECHA", "14 de octubre de 2026"), ("VALIDEZ", "15 días")]
    assert h["encabezados"] == ("DESCRIPCIÓN", "TOTAL")
    assert [o["titulo"] for o in h["opciones"]] == ["Opción 1 - Músicos formados", "Opción 2 - Músicos graduados"]
    assert h["opciones"][0]["renglones"] == [
        {"descripcion": "Grabación de bajo y güira", "detalle": "Incluye mezcla y máster.", "total": "RD$62,500"}]
    assert h["pie"] == ("Santo Domingo, República Dominicana", "(809) 687-9641 · caribbeandreamstudios@gmail.com")
    assert h["condiciones_titulo"] == "CONDICIONES"


def test_total_de_la_opcion_solo_con_mas_de_un_renglon_y_es_la_suma():
    h = cz.hoja_de_cotizacion(_cot())
    assert h["opciones"][0]["total"] is None
    assert h["opciones"][1]["total"] == {"etiqueta": "Total de la opción", "total": "RD$87,750.50"}


def test_solo_salen_las_condiciones_marcadas_y_sin_ninguna_no_sale_el_titulo():
    h = cz.hoja_de_cotizacion(_cot())
    assert h["condiciones"] == ["Esta cotización tiene validez de 15 días a partir de la fecha de emisión.",
                                "Se requiere un depósito del 50%.", "Una propia."]
    c = _cot()
    for x in c["contenido"]["condiciones"]:
        x["marcada"] = False
    assert cz.hoja_de_cotizacion(c)["condiciones"] == []
    assert "CONDICIONES" not in cz.textos_de_la_hoja(cz.hoja_de_cotizacion(c))


@pytest.mark.parametrize("valor,esperado", [
    ("62500.00", "RD$62,500"), ("62500.50", "RD$62,500.50"), ("1000000.00", "RD$1,000,000"),
    ("0.05", "RD$0.05"), ("99999999.99", "RD$99,999,999.99"), ("5.10", "RD$5.10"), ("999.00", "RD$999")])
def test_el_dinero_se_escribe_con_miles_y_centavos_solo_si_hay(valor, esperado):
    assert cz.dinero_en_pesos(valor) == esperado


@pytest.mark.parametrize("mes,palabra", list(enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
     "noviembre", "diciembre"], start=1)))
def test_la_fecha_en_palabras_de_cada_mes(mes, palabra):
    assert cz.fecha_en_palabras(f"2026-{mes:02d}-05") == f"5 de {palabra} de 2026"


def test_sin_telefono_no_sale_el_renglon_y_la_hoja_no_toca_lo_que_recibe():
    c = _cot(para_telefono=None)
    antes = copy.deepcopy(c)
    h = cz.hoja_de_cotizacion(c)
    assert [e for e, _ in h["para"]] == ["PARA", "FECHA", "VALIDEZ"]
    assert c == antes
    assert cz.hoja_de_cotizacion(_cot(para_telefono=""))["para"][1][0] == "FECHA"


# ── 2. El texto del PDF es el de la hoja ────────────────────────────────────────────────────────────

def test_el_texto_del_pdf_es_exactamente_el_de_la_hoja_y_en_su_orden():
    h = cz.hoja_de_cotizacion(_cot())
    pdf = cz.pdf_de_cotizacion(h)
    assert pdf.startswith(b"%PDF") and _paginas(pdf) == 1
    assert _sin_blancos(_texto_del_pdf(pdf)) == _sin_blancos("".join(cz.textos_de_la_hoja(h)))


def test_cada_texto_esta_y_en_el_orden_en_que_se_dibuja():
    h = cz.hoja_de_cotizacion(_cot())
    leido = _sin_blancos(_texto_del_pdf(cz.pdf_de_cotizacion(h)))
    desde = 0
    for t in cz.textos_de_la_hoja(h):
        i = leido.find(_sin_blancos(t), desde)
        assert i >= 0, f"{t!r} no está en el PDF después de lo anterior"
        desde = i + len(_sin_blancos(t))


def test_el_pdf_trae_la_ultima_opcion_con_sus_numeros():
    pdf = _sin_blancos(_texto_del_pdf(cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(_cot()))))
    for esperado in ("Opción1-Músicosformados", "Opción2-Músicosgraduados", "RD$62,500", "RD$72,500",
                     "RD$15,250.50", "Totaldelaopción", "RD$87,750.50", "14deoctubrede2026", "15días",
                     "809-555-0101", "(809)687-9641·caribbeandreamstudios@gmail.com"):
        assert _sin_blancos(esperado) in pdf, esperado


def test_el_pdf_no_trae_las_condiciones_desmarcadas_ni_en_la_hoja():
    c = _cot()
    h = cz.hoja_de_cotizacion(c)
    assert "TEXTO-DE-LA-CONDICION-DESMARCADA" not in "".join(cz.textos_de_la_hoja(h))
    leido = _sin_blancos(_texto_del_pdf(cz.pdf_de_cotizacion(h)))
    assert "DESMARCADA" not in leido
    for t in ("Se requiere un depósito del 50%.", "Una propia."):
        assert _sin_blancos(t) in leido


def test_con_muchas_opciones_y_renglones_hay_varias_paginas_y_no_se_pierde_texto():
    ops = [{"nombre": f"Opción larga {i}", "renglones": [
        {"descripcion": f"Trabajo {i}.{j}", "detalle": "Detalle largo " * 40 + f"fin{i}x{j}", "total": f"{1000 + j}.00"}
        for j in range(8)]} for i in range(1, 7)]
    c = _cot()
    c["contenido"]["opciones"] = ops
    h = cz.hoja_de_cotizacion(c)
    pdf = cz.pdf_de_cotizacion(h)
    assert _paginas(pdf) >= 4
    leido = _sin_blancos(_texto_del_pdf(pdf))
    desde = 0
    for t in cz.textos_de_la_hoja(h):         # la cabecera de la tabla se repite en cada página: se salta
        if t in h["encabezados"]:
            continue
        i = leido.find(_sin_blancos(t), desde)
        assert i >= 0, t[:60]
        desde = i + len(_sin_blancos(t))
    assert leido.count("fin6x7") == 1


# ── 3. La letra de fábrica y lo que la puerta deja pasar ────────────────────────────────────────────

def _caracteres_que_la_puerta_acepta() -> list[str]:
    """Todo carácter de U+0020 a U+FFFF (menos los sustitutos, que ni se codifican) que la PUERTA deja pasar,
    preguntándole a ella uno por uno en un nombre de opción y en una descripción. Fuera del plano básico no
    se recorre: se agregan tres caracteres tecleados que la puerta debe rechazar o simplificar."""
    candidatos = [chr(i) for i in range(0x20, 0x10000) if not 0xD800 <= i <= 0xDFFF] + list("☺😀€")
    aceptados = []
    for c in candidatos:
        try:
            db.contenido_de_cotizacion_que_vale(
                {"opciones": [{"nombre": f"a{c}b", "renglones": [{"descripcion": f"a{c}b", "total": "1"}]}]})
        except db.CotizacionNoVale:
            continue
        aceptados.append(c)
    return aceptados


def _visibles(texto: str) -> str:
    return "".join(ch for ch in texto if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Zs"))


def test_todo_lo_que_la_puerta_deja_pasar_se_dibuja_con_la_letra_de_fabrica_en_todos_los_campos():
    aceptados = _caracteres_que_la_puerta_acepta()
    assert len(aceptados) >= 180, len(aceptados)        # Latin-1 imprimible más las formas que se simplifican
    todo = "".join(aceptados)
    comparados = 0
    for i in range(0, len(todo), 40):
        trozo = todo[i:i + 40]
        contenido = {"opciones": [{"nombre": trozo, "renglones": [
            {"descripcion": trozo, "detalle": trozo, "total": "1500"}]}],
            "condiciones": [{"clave": None, "texto": trozo, "marcada": True}]}
        guardado = json.loads(db.condiciones_con_sus_textos_al_dia(db.contenido_de_cotizacion_que_vale(contenido), 15))
        nombre = db._texto_de_cotizacion(trozo, "para_nombre", 200, minimo=0) or "x"    # como lo deja `guardar_cotizacion`
        pdf = cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(_cot(para_nombre=nombre, contenido=guardado)))
        assert pdf.startswith(b"%PDF")
        leido = _sin_blancos(_texto_del_pdf(pdf))
        op, r, cond = guardado["opciones"][0], guardado["opciones"][0]["renglones"][0], guardado["condiciones"][0]
        for campo, valor in (("para_nombre", nombre), ("nombre de la opción", op["nombre"]),
                             ("descripción", r["descripcion"]), ("detalle", r["detalle"]),
                             ("condición", cond["texto"])):
            visibles = _visibles(valor)
            assert visibles, f"el trozo {i} dejó vacío el campo {campo}: la prueba no compara nada"
            assert _sin_blancos(visibles) in leido, f"{campo} (trozo {i}) no se lee de vuelta del PDF"
            comparados += 1
    assert comparados >= 5 * 4


def test_la_puerta_cambia_las_formas_tipograficas_y_el_pdf_las_lleva_ya_cambiadas():
    crudo = {"opciones": [{"nombre": "Uno — dos “tres” ‘cuatro’ cinco… seis", "renglones": [
        {"descripcion": "A – B", "detalle": "x — y", "total": "10"}]}]}
    guardado = json.loads(db.contenido_de_cotizacion_que_vale(crudo))
    h = cz.hoja_de_cotizacion(_cot(contenido=guardado))
    leido = _sin_blancos(_texto_del_pdf(cz.pdf_de_cotizacion(h)))
    assert _sin_blancos('Opción 1 - Uno - dos "tres" \'cuatro\' cinco... seis') in leido
    assert _sin_blancos("A - B") in leido and _sin_blancos("x - y") in leido
    assert not any(c in leido for c in "—–“”‘’…")


def test_la_letra_de_fabrica_no_dibuja_la_raya_larga_y_por_eso_la_puerta_la_cambia():
    """La frontera que justifica la regla de la puerta, medida con la biblioteca instalada: si un día esto
    deja de reventar, la regla de las rayas se puede aflojar."""
    c = _cot()
    c["contenido"]["opciones"][0]["nombre"] = "Raya — larga"
    with pytest.raises(Exception) as e:
        cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(c))
    assert "latin-1" in str(e.value).lower() or "encode" in type(e.value).__name__.lower() or "Unicode" in type(e.value).__name__


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


def _html_de_la_hoja(h: dict) -> str:
    plantilla = panel.plantillas.env.get_template("_hoja_de_cotizacion.html")
    return str(plantilla.module.hoja_de_cotizacion(h))


def test_la_vista_previa_trae_los_mismos_textos_que_la_hoja():
    h = cz.hoja_de_cotizacion(_cot())
    p = _Textos()
    p.feed(_html_de_la_hoja(h))
    assert _sin_blancos("".join(p.textos)) == _sin_blancos("".join(cz.textos_de_la_hoja(h)))
    assert any(t == "img" and a.get("src") == "/logo-cds.png" for t, a in p.etiquetas)
    assert "TEXTO-DE-LA-CONDICION-DESMARCADA" not in "".join(p.textos)


def test_la_vista_previa_escapa_lo_que_se_escribio():
    h = cz.hoja_de_cotizacion(_cot(para_nombre="<script>alert(1)</script> & <b>x</b>"))
    html = _html_de_la_hoja(h)
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in html


def test_la_vista_previa_no_pone_titulo_de_condiciones_si_no_hay_ninguna():
    c = _cot()
    for x in c["contenido"]["condiciones"]:
        x["marcada"] = False
    html = _html_de_la_hoja(cz.hoja_de_cotizacion(c))
    assert "CONDICIONES" not in html and "<ul>" not in html


def test_el_estilo_de_la_hoja_lleva_el_rojo_de_la_maqueta():
    plantilla = panel.plantillas.env.get_template("_hoja_de_cotizacion.html")
    estilo = str(plantilla.module.estilo_de_la_hoja())
    assert "#c0272d" in estilo and ".hoja" in estilo


# ── 5. La ruta del PDF, sobre la lectura real con SQLite ────────────────────────────────────────────

def _cliente(con_sesion):
    c = Navegador(panel.app, base_url="https://testserver")
    if con_sesion == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    elif con_sesion == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    return c


def _guardada(con, **kw):
    return tg._guardar(**kw)


def _url(pid, cid):
    return f"/proyectos/{pid}/cotizaciones/{cid}/pdf"


@pytest.mark.parametrize("sesion", ["casa", "ver"])
def test_la_casa_y_solo_ver_bajan_el_pdf_con_sus_cabeceras(con, sesion):
    r = _guardada(con, accion="enviar")
    resp = _cliente(sesion).get(_url(1, r["id"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="COT-2026-001.pdf"'
    assert "private" in resp.headers["cache-control"]
    assert resp.content.startswith(b"%PDF")
    leido = _sin_blancos(_texto_del_pdf(resp.content))
    assert "COT-2026-001" in leido and _sin_blancos("Cliente de la ficha") in leido


def test_el_pdf_de_la_ruta_es_el_de_la_hoja_de_lo_guardado(con):
    r = _guardada(con, accion="enviar", para_telefono="809-555-0101")
    resp = _cliente("casa").get(_url(1, r["id"]))
    cot = asyncio.run(db.cotizaciones_de_proyectos())[1][0]
    esperado = _sin_blancos("".join(cz.textos_de_la_hoja(cz.hoja_de_cotizacion(cot))))
    assert _sin_blancos(_texto_del_pdf(resp.content)) == esperado


def test_un_borrador_tambien_se_baja(con):
    r = _guardada(con)
    assert _cliente("casa").get(_url(1, r["id"])).status_code == 200


def test_sin_sesion_es_401_y_no_hay_pdf(con):
    r = _guardada(con, accion="enviar")
    resp = _cliente(None).get(_url(1, r["id"]))
    assert resp.status_code == 401
    assert not resp.content.startswith(b"%PDF") and "attachment" not in resp.headers.get("content-disposition", "")


def test_una_cookie_falsa_tampoco_baja_el_pdf(con):
    r = _guardada(con, accion="enviar")
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, "ver.9999999999.firma-falsa")
    assert c.get(_url(1, r["id"])).status_code == 401


@pytest.mark.parametrize("sesion", ["casa", "ver"])
def test_la_cotizacion_de_otro_proyecto_es_404(con, sesion):
    r = _guardada(con, accion="enviar", proyecto=1)
    otra = _guardada(con, accion="enviar", proyecto=2)
    c = _cliente(sesion)
    assert c.get(_url(2, r["id"])).status_code == 404       # el cid de 1 pedido por 2
    assert c.get(_url(1, otra["id"])).status_code == 404    # y al revés
    assert c.get(_url(2, otra["id"])).status_code == 200


def test_una_que_no_existe_o_borrada_o_sin_tabla_es_404(con):
    r = _guardada(con, accion="enviar")
    c = _cliente("casa")
    assert c.get(_url(1, 999)).status_code == 404 and c.get(_url(99, r["id"])).status_code == 404
    con.execute("UPDATE cotizaciones SET borrado_en = '2026-10-15 00:00:00'")
    assert c.get(_url(1, r["id"])).status_code == 404
    con.execute("DROP TABLE cotizaciones")
    assert c.get(_url(1, r["id"])).status_code == 404


def test_la_ruta_es_de_lectura_no_escribe_nada(con):
    r = _guardada(con, accion="enviar")
    antes = (tg._fila(con), tg._cuantas(con, "log_acciones"), tg._cuantas(con, "serie_cotizaciones"))
    _cliente("ver").get(_url(1, r["id"]))
    _cliente("casa").get(_url(1, r["id"]))
    assert (tg._fila(con), tg._cuantas(con, "log_acciones"), tg._cuantas(con, "serie_cotizaciones")) == antes


# ── 3-bis. Nada se sale de la carta ─────────────────────────────────────────────────────────────────

@pytest.fixture
def espia_del_borde(monkeypatch):
    """Anota cada `cell` y `multi_cell` que pinta el escritor y lo que le falta (o le sobra) al borde derecho."""
    from fpdf import FPDF
    dibujados: list[tuple] = []
    cell, multi = FPDF.cell, FPDF.multi_cell

    def espiar_cell(self, w=None, h=None, text="", *a, **k):
        limite = self.w - self.r_margin
        x = self.get_x()
        ancho = (w if w else limite - x)
        dibujados.append(("cell", text, x + ancho - limite, self.get_string_width(text) + 2 * self.c_margin - ancho))
        return cell(self, w, h, text, *a, **k)

    def espiar_multi(self, w, h=None, text="", *a, **k):
        limite = self.w - self.r_margin
        x = self.get_x()
        ancho = (w if w else limite - x)
        if not k.get("dry_run"):
            dibujados.append(("multi_cell", text, x + ancho - limite, 0.0))
        return multi(self, w, h, text, *a, **k)
    monkeypatch.setattr(FPDF, "cell", espiar_cell)
    monkeypatch.setattr(FPDF, "multi_cell", espiar_multi)
    return dibujados


def _lo_que_se_sale(dibujados) -> list:
    # `[2]`: cuánto pasa el borde derecho de la carta la caja de la celda; `[3]`: cuánto pasa el texto de su caja
    return [(t, texto[:40], round(a, 2), round(b, 2)) for t, texto, a, b in dibujados if a > 1e-6 or b > 1e-6]


def _cot_al_tope_de_la_puerta(letra="W"):
    nombre = letra * 200
    c = _cot(para_nombre=nombre, para_telefono="9" * 40)
    c["contenido"]["opciones"] = [
        {"nombre": letra * 120, "renglones": [
            {"descripcion": letra * 200, "detalle": letra * 1000, "total": "99999999.99"},
            {"descripcion": "Otro", "detalle": "", "total": "99999999.99"}]},
        {"nombre": ("Palabra " * 15).strip(), "renglones": [{"descripcion": "Dos " * 50, "detalle": "x y " * 250, "total": "5"}]}]
    c["contenido"]["condiciones"] = [{"clave": None, "texto": letra * 300, "marcada": True},
                                     {"clave": None, "texto": "Una condición " * 21, "marcada": True}]
    return c


@pytest.mark.parametrize("letra", ["W", "i", "Ñ"])
def test_nada_de_lo_que_se_dibuja_se_sale_del_borde_de_la_carta_con_todo_al_tope_de_la_puerta(espia_del_borde, letra):
    c = _cot_al_tope_de_la_puerta(letra)
    # lo que ya acepta la puerta (nombre 200, opción 120, descripción 200, detalle 1000, condición 300)
    db.contenido_de_cotizacion_que_vale(c["contenido"])
    cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(c))
    assert len(espia_del_borde) > 20
    assert _lo_que_se_sale(espia_del_borde) == []


def test_el_nombre_y_el_titulo_largos_salen_enteros_en_el_pdf_y_en_varias_lineas():
    c = _cot_al_tope_de_la_puerta("W")
    pdf = cz.pdf_de_cotizacion(cz.hoja_de_cotizacion(c))
    leido = _sin_blancos(_texto_del_pdf(pdf))
    assert "W" * 200 in leido and "Opción1-" + "W" * 120 in leido
    assert "RD$199,999,999.98" in leido           # el total de la opción 1 es la suma de dos tope: la columna creció


def test_la_prueba_del_borde_ve_lo_que_se_sale(espia_del_borde):
    """Entrada inventada para el espía: una celda de una línea con un texto largo SÍ se marca."""
    from fpdf import FPDF
    pdf = FPDF(unit="mm", format="Letter")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "W" * 120)
    assert _lo_que_se_sale(espia_del_borde) != []
