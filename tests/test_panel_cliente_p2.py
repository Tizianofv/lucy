"""UN PROYECTO NO SE CREA SIN CLIENTE: piezas 2 y 3, «la pantalla» (Tiziano, 18-oct-2026).

Pieza 2: el alta del panel (la ventanita «Proyecto nuevo en X» y la página aparte `?nuevo=`). Pieza 3: convertir una
tarea en proyecto, que fuera de IA son dos pasos. Más lo que el testigo de la pieza 1 dejó para estas piezas: el aviso
de la caja «Cliente» de la cabecera decía una causa falsa («la ficha ya no está en Noco o el proyecto ya no está») para
casos que eran otros, y «Quitar el cliente» se ofrecía donde la puerta lo rechaza.

Reglas de Tiziano que la pantalla sostiene (`disenos/lucy-proyecto-con-cliente/DISENO.md`): en CDS y ACD el campo
Cliente es obligatorio y el «Nombre» no se pide (el proyecto se llama como su cliente); en IA todo sigue como estaba; un
solo proyecto ACTIVO por cliente (el aviso nombra al que lo ocupa y lleva «Abrir»).

Garantías (la numeración es la del diseño, §2.8; las claves de aviso son las del texto):

  G8   POST sin cliente / vacío / no numérico en un grupo que lo exige -> 303 `error=cliente`, 0 filas, ni Noco ni Drive
       (en IA crea);
  G9   Noco sin contestar -> `error=cliente_noco`, 0 filas, 0 llamadas a Drive;
  G10  con cliente: nace con él y con su nombre (el de Noco), y la carpeta de Drive se pide DESPUÉS de guardar;
  G11  el HTML pide el cliente en la ventanita y en `?nuevo=` exactamente en los grupos que `cliente_es_obligatorio`
       dice (sacados de los grupos de la base, con uno inventado), y sin JavaScript la página aparte escoge al cliente;
  G12  convertir sin cliente una tarea de un grupo que lo exige (o sin área): no convierte nada;
  G13  Noco sin contestar al convertir: igual;
  G14  convertir con cliente: proyecto con cliente + tarea archivada + las dos huellas de siempre;
  A    cada aviso dice lo que pasó DE VERDAD (sale de la clase con que la puerta rechazó, no del texto) y el de cliente
       ocupado nombra al proyecto y enlaza «Abrir» solo si ese proyecto existe, sigue activo y exige cliente;
  Q    «Quitar el cliente» solo en los grupos que no exigen cliente.

FRONTERA, dicha una vez:

  · NO hay Postgres ni navegador: SQLite con el texto de las sentencias reales, y la plantilla de verdad leída como
    HTML. Que un navegador deje o no enviar el formulario sin cliente (`required` sobre la caja que se ve) no se probó:
    se comprueba el atributo, no el comportamiento del navegador. El `required` del campo escondido NO frena nada en un
    navegador (un campo `hidden` no se valida); lo que decide es la ruta.
  · El doble de Noco afirma solo `{id, nombre}` o `None` para una ficha y `[{id, nombre}]` para la búsqueda.
  · Los avisos viajan por la dirección con su recibo (`web/avisos.py`): aquí se sigue la redirección REAL de cada POST
    con el mismo navegador, así que el aviso que se lee es el que la acción dejó.

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import html as _html
import re

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_grupo_ia  # noqa: F401  (pone el entorno de prueba: va antes que todo lo que importe `config`)
from test_pagina_proyectos import AREAS, gente, mundo, _cliente  # noqa: F401
from test_proyecto_con_cliente import _p
import config
import cotizaciones_drive
import db.db as db
import noco_lectura

DUENO = config.CHAT_ID_DUENO
EXIGEN = [a["clave"] for a in AREAS if db.cliente_es_obligatorio(a["clave"])]        # CDS, ACD
LIBRES = [a["clave"] for a in AREAS if not db.cliente_es_obligatorio(a["clave"])]    # IA


# ═══════════════════════════════════════════════════════════════════════
# Piezas
# ═══════════════════════════════════════════════════════════════════════

class Noco:
    """El doble de Noco: `persona(id)` y `buscar_personas(texto)`. `cae=True` es Noco sin contestar."""

    def __init__(self):
        self.fichas = {i: {"id": i, "nombre": f"Cliente {i}"} for i in (101, 102, 103, 104)}
        self.pedidos: list = []
        self.busquedas: list = []
        self.cae = False

    async def persona(self, noco_id):
        self.pedidos.append(noco_id)
        if self.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        return self.fichas.get(noco_id)

    async def buscar(self, texto):
        self.busquedas.append(texto)
        if self.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        return [dict(f) for f in self.fichas.values() if texto.lower() in f["nombre"].lower()]


@pytest.fixture
def noco(monkeypatch):
    n = Noco()
    monkeypatch.setattr(noco_lectura, "persona", n.persona)
    monkeypatch.setattr(noco_lectura, "buscar_personas", n.buscar)
    return n


@pytest.fixture
def drive(monkeypatch, mundo):
    """La carpeta de Drive del proyecto nuevo, registrada. Cada llamada anota si, EN ESE MOMENTO, el proyecto ya estaba
    guardado y con qué cliente: así se ve que Drive se pide DESPUÉS de la puerta y no antes."""
    llamadas: list = []

    async def preparar(proyecto_id, **kw):
        f = mundo.con.execute("SELECT * FROM proyectos WHERE id = ?", (proyecto_id,)).fetchone()
        llamadas.append({"id": proyecto_id, "existe": f is not None,
                         "cliente": None if f is None else f["cliente_noco_id"]})
        return {"resultado": "lista", "causa": None, "carpeta": cotizaciones_drive.ENLACE_PUESTO}
    monkeypatch.setattr(cotizaciones_drive, "preparar_la_carpeta", preparar)
    return llamadas


def _foto(m) -> dict:
    """TODO lo que estas rutas pueden escribir."""
    return {t: [tuple(f) for f in m.con.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("proyectos", "log_acciones", "tareas")}


def _post(ruta, campos):
    c = _cliente(DUENO)
    return c, c.post(ruta, data=campos, follow_redirects=False)


def _pagina_de(c, r) -> str:
    """La página a la que el POST mandó, con el recibo que el mismo POST dejó (un navegador de verdad)."""
    assert r.status_code == 303, r.text[:300]
    g = c.get(r.headers["location"].split("#")[0])
    assert g.status_code == 200, g.text[:300]
    return _html.unescape(g.text)


def _avisos(html: str) -> list[str]:
    """El texto de cada `<p class="aviso">` (sin etiquetas)."""
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()
            for t in re.findall(r'<p class="aviso[^"]*">(.*?)</p>', html, re.S)]


def _nuevo(area, **campos):
    datos = {"area": area, "responsable": "Persona Dos"}
    if area not in EXIGEN:
        datos["nombre"] = "Proyecto de la prueba"
    datos.update(campos)
    return datos


# ═══════════════════════════════════════════════════════════════════════
# G8 · G9 · G10: crear desde el panel
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("area", EXIGEN)
@pytest.mark.parametrize("campos", [{}, {"cliente": ""}, {"cliente": "   "}, {"cliente": "abc"}, {"cliente": "-5"},
                                    {"cliente": "1.5"}, {"cliente": "101 OR 1=1"}])
def test_G8_sin_cliente_o_con_algo_que_no_es_un_numero_no_se_crea_nada(mundo, gente, noco, drive, area, campos):
    antes = _foto(mundo)
    c, r = _post("/proyectos/nuevo", _nuevo(area, **campos))
    assert r.headers["location"] == f"/proyectos?nuevo={area}&error=cliente"
    assert _foto(mundo) == antes, "escribió algo aunque no había cliente"
    assert noco.pedidos == [] and drive == [], "habló con Noco o con Drive sin tener un cliente que valga"
    # y el aviso dice lo que pasó: falta escoger al cliente, no que una ficha falle
    avisos = _avisos(_pagina_de(c, r))
    assert any("Falta escoger al cliente" in a for a in avisos), avisos
    assert not any("ficha" in a.lower() or "Noco no contestó" in a for a in avisos), avisos


@pytest.mark.parametrize("area", LIBRES)
def test_G8_en_ia_sin_cliente_si_se_crea_con_el_nombre_escrito_y_sin_leer_noco(mundo, gente, noco, drive, area):
    c, r = _post("/proyectos/nuevo", _nuevo(area))
    f = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(f) == 1 and f[0]["nombre"] == "Proyecto de la prueba" and f[0]["cliente_noco_id"] is None
    assert r.headers["location"].startswith("/proyectos?hecho=proyecto_nuevo") and noco.pedidos == []
    assert len(drive) == 1


@pytest.mark.parametrize("area", EXIGEN)
def test_G9_si_noco_no_contesta_no_hay_proyecto_ni_huella_ni_llamada_a_drive(mundo, gente, noco, drive, area):
    noco.cae = True
    antes = _foto(mundo)
    c, r = _post("/proyectos/nuevo", _nuevo(area, cliente="101"))
    assert r.headers["location"] == f"/proyectos?nuevo={area}&error=cliente_noco"
    assert _foto(mundo) == antes and drive == []
    avisos = _avisos(_pagina_de(c, r))
    assert any("Noco no contestó" in a and "no se guardó nada" in a for a in avisos), avisos
    assert not any("Falta escoger" in a or "ficha no está" in a for a in avisos), avisos


@pytest.mark.parametrize("area", EXIGEN)
def test_A_una_ficha_que_noco_no_conoce_se_dice_distinto_de_no_haber_escogido_a_nadie(mundo, gente, noco, drive, area):
    antes = _foto(mundo)
    c, r = _post("/proyectos/nuevo", _nuevo(area, cliente="999"))
    assert r.headers["location"] == f"/proyectos?nuevo={area}&error=cliente_ficha"
    assert _foto(mundo) == antes and drive == [] and noco.pedidos == [999]
    avisos = _avisos(_pagina_de(c, r))
    assert any("Esa ficha no está en Noco" in a for a in avisos), avisos
    assert not any("Falta escoger" in a for a in avisos), avisos


@pytest.mark.parametrize("area", EXIGEN)
def test_G10_con_cliente_nace_con_el_y_con_su_nombre_y_drive_se_pide_despues(mundo, gente, noco, drive, area):
    # (aunque el navegador mandara un nombre, el proyecto se llama como el cliente que Noco devuelve)
    c, r = _post("/proyectos/nuevo", _nuevo(area, cliente="101", nombre="Un nombre que no se usa"))
    f = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(f) == 1
    assert (f[0]["nombre"], f[0]["cliente_noco_id"], f[0]["cliente_nombre"], f[0]["area"]) == (
        "Cliente 101", 101, "Cliente 101", area)
    assert r.headers["location"] == f"/proyectos?hecho=proyecto_nuevo&p={f[0]['id']}"
    assert drive == [{"id": f[0]["id"], "existe": True, "cliente": 101}], drive
    huella = mundo.con.execute("SELECT * FROM log_acciones WHERE tabla = 'proyectos'").fetchall()
    assert len(huella) == 1 and huella[0]["accion"] == "crear" and '"cliente_noco_id": 101' in huella[0]["despues"]


def test_G10_si_el_campo_llega_dos_veces_vale_el_que_trae_algo(mundo, gente, noco, drive):
    """La página aparte, sin JavaScript, manda el campo escondido (vacío) y el botón de la ficha (con su Id): no
    depende de cuál de los dos gane en el servidor."""
    c, r = _post("/proyectos/nuevo", {"area": "CDS", "responsable": "Persona Dos", "cliente": ["", "101"]})
    assert mundo.con.execute("SELECT cliente_noco_id FROM proyectos").fetchone()[0] == 101
    c, r = _post("/proyectos/nuevo", {"area": "ACD", "responsable": "Persona Dos", "cliente": ["102", ""]})
    assert sorted(f[0] for f in mundo.con.execute("SELECT cliente_noco_id FROM proyectos")) == [101, 102]


# ═══════════════════════════════════════════════════════════════════════
# A: el aviso de cliente ocupado nombra al proyecto y enlaza «Abrir» solo si dice la verdad
# ═══════════════════════════════════════════════════════════════════════

def test_A_cliente_ocupado_al_crear_nombra_al_proyecto_activo_y_lleva_abrir(mundo, gente, noco, drive):
    _p(mundo, 7, "Cliente 101", "CDS", cliente=101)
    antes = _foto(mundo)
    c, r = _post("/proyectos/nuevo", _nuevo("ACD", cliente="101"))
    assert r.headers["location"] == "/proyectos?nuevo=ACD&error=cliente_ocupado&ocupa=7"
    assert _foto(mundo) == antes and drive == []
    pagina = _pagina_de(c, r)
    aviso = next(a for a in _avisos(pagina) if "proyecto activo" in a)
    assert "(«Cliente 101»)" in aviso and "no se pueden activar 2" in aviso and aviso.endswith("Abrir")
    assert '<a href="/proyectos?p=7">Abrir</a>' in pagina


@pytest.mark.parametrize("otro", [
    dict(id=8, nombre="Cerrado", area="CDS", estado="cerrado", cliente=101),
    dict(id=8, nombre="Pausado", area="CDS", estado="pausado", cliente=101),
    dict(id=8, nombre="De IA", area="IA", estado="activo", cliente=101),
    dict(id=8, nombre="Borrado", area="CDS", estado="activo", cliente=101, borrado=True),
], ids=["cerrado", "pausado", "de IA", "en la papelera"])
def test_A_el_aviso_no_nombra_ni_enlaza_a_un_proyecto_que_ya_no_ocupa_al_cliente(mundo, gente, noco, otro):
    """`ocupa` viaja por la dirección: el nombre sale de la base AL PINTAR y solo si ese proyecto de verdad ocupa
    (existe, activo, de un grupo que exige cliente). Si no, el aviso se pinta sin nombre y sin enlace."""
    _p(mundo, otro.pop("id"), otro.pop("nombre"), otro.pop("area"), **otro)
    from _navegador import dar_recibo
    dar_recibo("/proyectos", nuevo="ACD", error="cliente_ocupado", ocupa=8)
    pagina = _html.unescape(_cliente(DUENO).get("/proyectos", params={
        "nuevo": "ACD", "error": "cliente_ocupado", "ocupa": 8}).text)
    aviso = next(a for a in _avisos(pagina) if "proyecto activo" in a)
    assert "(«" not in aviso and "Abrir" not in aviso and "/proyectos?p=8" not in pagina.split("</header>")[1].split("<aside")[0]


def test_A_una_direccion_escrita_a_mano_no_pinta_el_aviso_ni_el_nombre(mundo, gente, noco):
    _p(mundo, 7, "Cliente 101", "CDS", cliente=101)
    pagina = _html.unescape(_cliente(DUENO).get("/proyectos", params={
        "nuevo": "ACD", "error": "cliente_ocupado", "ocupa": 7}).text)
    assert not any("proyecto activo" in a for a in _avisos(pagina))


# ═══════════════════════════════════════════════════════════════════════
# A: la caja «Cliente» de la cabecera dice la causa que fue (hallazgo 1 del testigo de la pieza 1)
# ═══════════════════════════════════════════════════════════════════════

FALSA = "la ficha ya no está en Noco o el proyecto ya no está"


def _cabecera(mundo, noco_id, pid=1):
    c, r = _post(f"/proyectos/{pid}/cliente", {"noco_id": noco_id})
    return r.headers["location"], _avisos(_pagina_de(c, r))


def test_A_cabecera_cliente_ocupado_nombra_al_otro_proyecto_y_lleva_abrir(mundo, gente, noco):
    _p(mundo, 1, "Cliente 102", "CDS", cliente=102)
    _p(mundo, 2, "Cliente 101", "CDS", cliente=101)
    loc, avisos = _cabecera(mundo, "101", pid=1)
    assert loc == "/proyectos?error=cliente_ocupado&p=1&ocupa=2"
    assert any("(«Cliente 101»)" in a and a.endswith("Abrir") for a in avisos) and not any(FALSA in a for a in avisos)
    assert mundo.con.execute("SELECT cliente_noco_id FROM proyectos WHERE id = 1").fetchone()[0] == 102


@pytest.mark.parametrize("area", EXIGEN)
def test_A_cabecera_quitar_el_cliente_en_un_grupo_que_lo_exige_dice_que_no_se_puede(mundo, gente, noco, area):
    _p(mundo, 1, "Cliente 101", area, cliente=101)
    loc, avisos = _cabecera(mundo, "")
    assert loc == "/proyectos?error=cliente_obligatorio&p=1"
    assert any("no puede quedarse sin cliente" in a for a in avisos) and not any(FALSA in a for a in avisos), avisos
    assert mundo.con.execute("SELECT cliente_noco_id FROM proyectos WHERE id = 1").fetchone()[0] == 101


def test_A_cabecera_ficha_que_no_existe_proyecto_que_no_esta_y_nombre_repetido_son_tres_avisos_distintos(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", "CDS", cliente=101)
    _p(mundo, 2, "Cliente 103", "CDS", cliente=103)
    noco.fichas[102] = {"id": 102, "nombre": "Cliente 101"}              # otra ficha que se llama igual
    ficha, a_ficha = _cabecera(mundo, "999", pid=1)
    falta, a_falta = _cabecera(mundo, "101", pid=77)
    repetido, a_repe = _cabecera(mundo, "102", pid=2)
    assert (ficha, falta, repetido) == ("/proyectos?error=cliente_ficha&p=1", "/proyectos?error=proyecto&p=77",
                                        "/proyectos?error=nombre_repetido&p=2")
    assert any("Esa ficha no está en Noco" in a for a in a_ficha)
    assert any("Ese proyecto ya no está" in a for a in a_falta)
    assert any("Ya hay otro proyecto con ese nombre" in a for a in a_repe)
    for avisos in (a_ficha, a_falta, a_repe):
        assert not any(FALSA in a for a in avisos), avisos


def test_A_cabecera_si_la_puerta_rechaza_por_algo_que_no_se_nombra_no_se_afirma_ninguna_causa(mundo, gente, noco, monkeypatch):
    async def rara(*a, **k):
        raise ValueError("algo que la ruta no sabe nombrar")
    monkeypatch.setattr(db, "poner_cliente", rara)
    _p(mundo, 1, "Cliente 101", "CDS", cliente=101)
    loc, avisos = _cabecera(mundo, "102")
    assert loc == "/proyectos?error=cliente_no_se_pudo&p=1"
    assert any(a.startswith("No se pudo cambiar el cliente") for a in avisos) and not any(FALSA in a for a in avisos)


def test_A_cada_causa_tiene_su_propio_texto_y_ninguno_dice_la_causa_falsa(mundo, gente, noco):
    """Los avisos del cliente (sin el de Drive): cada clave se pinta, con texto, y los textos son todos distintos."""
    from _navegador import dar_recibo
    _p(mundo, 1, "Cliente 101", "CDS", cliente=101)
    claves = ["cliente", "cliente_ficha", "cliente_noco", "cliente_obligatorio", "cliente_no_se_pudo", "cliente_ocupado",
              "cliente_igual"]
    textos = {}
    for clave in claves:
        dar_recibo("/proyectos", error=clave, p=1)
        pagina = _html.unescape(_cliente(DUENO).get("/proyectos", params={"error": clave, "p": 1}).text)
        centro = pagina.split('<main data-region="centro">', 1)[1]
        avisos = [a for a in _avisos(centro)]
        assert len(avisos) == 1 and avisos[0], (clave, avisos)
        textos[clave] = avisos[0]
        assert FALSA not in avisos[0]
    assert len(set(textos.values())) == len(claves), textos


# ═══════════════════════════════════════════════════════════════════════
# Q: «Quitar el cliente» solo donde la puerta lo deja
# ═══════════════════════════════════════════════════════════════════════

def test_Q_quitar_el_cliente_se_ofrece_exactamente_en_los_grupos_que_no_lo_exigen(mundo, gente, noco):
    """Los grupos salen de los de la base (más uno inventado, que cae del lado estricto), y la decisión de cada uno la
    toma `db.cliente_es_obligatorio`: lo que la plantilla ofrece tiene que coincidir con lo que la puerta deja."""
    mundo.con.execute("INSERT INTO areas (clave, color, orden) VALUES ('Nueva', '#123456', 9)")
    grupos = [a["clave"] for a in AREAS] + ["Nueva"]
    for i, g in enumerate(grupos, start=1):
        _p(mundo, i, f"Proyecto {g}", g, cliente=100 + i)
        html = _cliente(DUENO).get("/proyectos", params={"p": i}).text
        ofrece = 'class="btn-linea quitar-cliente"' in html          # el botón (el texto también sale en un comentario del guion)
        assert ofrece == (not db.cliente_es_obligatorio(g)), (g, ofrece)
    assert [g for g in grupos if not db.cliente_es_obligatorio(g)] == ["IA"]
    # y la puerta lo confirma: en los que no se ofrece, quitarlo se rechaza
    import asyncio
    for i, g in enumerate(grupos, start=1):
        quitar = asyncio.run(db.poner_cliente(i, None, leer_persona=noco_lectura.persona)) if g == "IA" else None
        if g != "IA":
            with pytest.raises(db.ClienteDelProyectoNoVale) as e:
                asyncio.run(db.poner_cliente(i, None, leer_persona=noco_lectura.persona))
            assert e.value.clave == "obligatorio"
        else:
            assert quitar is True


# ═══════════════════════════════════════════════════════════════════════
# G11: el HTML pide el cliente donde la puerta lo exige
# ═══════════════════════════════════════════════════════════════════════

def _ventanitas(html: str) -> dict:
    """`{grupo: HTML de su ventanita}`."""
    return {m.group(1): m.group(2) for m in re.finditer(
        r'<dialog class="ventana" aria-label="Proyecto nuevo en ([^"]+)">(.*?)</dialog>', html, re.S)}


def _aparte(html: str) -> str:
    return html.split("<h1>Proyecto nuevo en", 1)[1].split("</section>", 1)[0]


def _campos_del_alta(trozo: str) -> dict:
    """Lo que pide el formulario `/proyectos/nuevo` de ese trozo: `{nombre?, cliente_visible?, cliente_escondido}`."""
    post = trozo.split('action="/proyectos/nuevo"', 1)[1]
    visible = re.search(r'<input type="text"[^>]*data-buscar-persona="elegir"[^>]*>', trozo)
    return {"nombre": 'name="nombre"' in post, "visible": visible.group(0) if visible else "",
            "escondido": re.search(r'<input type="hidden" name="cliente"[^>]*>', post).group(0)}


def test_G11_la_ventanita_y_la_pagina_aparte_piden_el_cliente_en_los_grupos_que_lo_exigen_y_no_el_nombre(mundo, gente, noco):
    mundo.con.execute("INSERT INTO areas (clave, color, orden) VALUES ('Nueva', '#123456', 9)")
    todos = [a["clave"] for a in AREAS] + ["Nueva"]
    html = _cliente(DUENO).get("/proyectos").text
    ventanitas = _ventanitas(html)
    assert sorted(ventanitas) == sorted(todos)
    for g in todos:
        exige = db.cliente_es_obligatorio(g)
        for donde, trozo in (("ventanita", ventanitas[g]),
                             ("aparte", _aparte(_cliente(DUENO).get("/proyectos", params={"nuevo": g}).text))):
            c = _campos_del_alta(trozo)
            assert c["nombre"] == (not exige), (g, donde)
            # la caja que SE VE frena el envío vacío (en la ventanita; la página aparte no la usa con JavaScript apagado)
            assert (" required" in c["visible"]) == (exige and donde == "ventanita"), (g, donde, c["visible"])
            assert (" required" in c["escondido"]) == exige, (g, donde)
            assert ("Cliente (opcional)" in trozo) == (not exige), (g, donde)
            assert ("El proyecto se llama como su cliente." in trozo) == exige, (g, donde)
    assert [g for g in todos if not db.cliente_es_obligatorio(g)] == ["IA"]


def test_G11_sin_javascript_la_pagina_aparte_busca_en_noco_y_cada_ficha_es_un_boton_que_envia_el_alta(mundo, gente, noco, drive):
    html = _cliente(DUENO).get("/proyectos", params={"nuevo": "CDS", "pq": "Cliente 10", "pdonde": "cliente-nuevo"}).text
    assert noco.busquedas == ["Cliente 10"]
    post = _aparte(html).split('action="/proyectos/nuevo"', 1)[1].split("</form>", 1)[0]
    botones = re.findall(r'<button class="sugerencia" name="([^"]+)" value="([^"]+)"', post)
    assert botones == [("cliente", str(i)) for i in (101, 102, 103, 104)], botones
    # tocar una ficha envía el alta: el formulario real, con el campo escondido (vacío) Y el botón
    c, r = _post("/proyectos/nuevo", {"area": "CDS", "responsable": "Persona Dos", "cliente": ["", "103"]})
    f = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(f) == 1 and f[0]["cliente_noco_id"] == 103 and f[0]["nombre"] == "Cliente 103"


def test_G11_la_busqueda_de_la_pagina_aparte_dice_cuando_noco_no_contesta_o_nadie_coincide(mundo, gente, noco):
    h = _cliente(DUENO).get("/proyectos", params={"nuevo": "ACD", "pq": "zzz", "pdonde": "cliente-nuevo"}).text
    assert "Nadie con «zzz» en Noco." in _aparte(h)
    noco.cae = True
    h = _cliente(DUENO).get("/proyectos", params={"nuevo": "ACD", "pq": "zzz", "pdonde": "cliente-nuevo"}).text
    assert "Noco de mentira: no contesta" in _aparte(h) and "Nadie con" not in _aparte(h)


# ═══════════════════════════════════════════════════════════════════════
# Pieza 3: convertir una tarea en proyecto
# ═══════════════════════════════════════════════════════════════════════

GRUPOS_DE_TAREA = [a["clave"] for a in AREAS] + [None]      # None: tarea sin área (cuenta como CDS/ACD)


@pytest.fixture(autouse=True)
def leer_la_tarea(monkeypatch, mundo):
    """La lectura de UNA tarea para su página (`db.tarea_con_comentarios`) usa `= ANY(%s)`, que SQLite no tiene: se
    reemplaza por una que lee la fila de la tarea de la misma base. FINGIDO, declarado: solo esa lectura (y la lista de
    candidatas de «Primero:», vacía); la ruta, la plantilla, la puerta de convertir y la base son las de verdad."""
    async def tarea_con_comentarios(tid):
        f = mundo.con.execute("SELECT * FROM tareas WHERE id = ? AND borrado_en IS NULL", (tid,)).fetchone()
        if f is None:
            return None
        return {"tarea": dict(dict(f), proyecto_nombre=None, primero_id=None, primero_titulo=None,
                              primero_esperando=False, tomada_en=None, grave=False),
                "comentarios": [], "pasos": []}

    async def candidatas(tid):
        return []
    monkeypatch.setattr(db, "tarea_con_comentarios", tarea_con_comentarios)
    monkeypatch.setattr(db, "tareas_para_elegir_primero", candidatas)


def _tarea(m, tid, area, titulo="Una tarea suelta", estado="pendiente"):
    m.tarea(tid, titulo, area=area, estado=estado)
    m.con.commit()
    return tid


def _directo(html: str, tid: int) -> bool:
    """¿La página de la tarea ofrece el clic único (un POST de convertir sin cliente)?"""
    return f'action="/tareas/{tid}/convertir-en-proyecto"' in html and "Convertir en proyecto</button>" in html


@pytest.mark.parametrize("area", GRUPOS_DE_TAREA, ids=lambda a: str(a))
def test_la_pagina_de_la_tarea_ofrece_el_clic_unico_solo_donde_la_puerta_no_pide_cliente(mundo, gente, noco, area):
    _tarea(mundo, 50, area)
    html = _cliente(DUENO).get("/tareas/50").text
    exige = db.cliente_es_obligatorio(area)
    assert _directo(html, 50) == (not exige)
    assert ("/tareas/50?convertir=1" in html) == exige


@pytest.mark.parametrize("area", GRUPOS_DE_TAREA, ids=lambda a: str(a))
def test_el_segundo_paso_busca_en_noco_y_cada_ficha_envia_la_conversion_con_su_id(mundo, gente, noco, area):
    _tarea(mundo, 50, area)
    html = _cliente(DUENO).get("/tareas/50", params={"convertir": 1, "pq": "Cliente 10"}).text
    if not db.cliente_es_obligatorio(area):
        assert noco.busquedas == [] and 'name="cliente"' not in html           # IA: no hay segundo paso
        return
    assert noco.busquedas == ["Cliente 10"]
    post = html.split('action="/tareas/50/convertir-en-proyecto"', 1)[1].split("</form>", 1)[0]
    assert re.findall(r'<button name="cliente" value="(\d+)"', post) == ["101", "102", "103", "104"]
    assert "Cancelar" in html and "paso 2 de 2" in html


@pytest.mark.parametrize("estado,proyecto", [("hecha", None), ("pendiente", 3)], ids=["hecha", "ya en un proyecto"])
def test_el_segundo_paso_no_se_dibuja_para_una_tarea_que_no_se_puede_convertir(mundo, gente, noco, estado, proyecto):
    _p(mundo, 3, "Otro", "CDS", cliente=104)
    mundo.tarea(50, "Una tarea", proyecto=proyecto, area="CDS", estado=estado)
    mundo.con.commit()
    html = _cliente(DUENO).get("/tareas/50", params={"convertir": 1, "pq": "Cliente"}).text
    assert noco.busquedas == [] and "paso 2 de 2" not in html and 'name="cliente"' not in html


@pytest.mark.parametrize("area", [a for a in GRUPOS_DE_TAREA if db.cliente_es_obligatorio(a)], ids=lambda a: str(a))
@pytest.mark.parametrize("campos", [{}, {"cliente": ""}, {"cliente": "abc"}])
def test_G12_el_clic_viejo_no_convierte_una_tarea_que_exige_cliente(mundo, gente, noco, drive, area, campos):
    _tarea(mundo, 50, area)
    antes = _foto(mundo)
    c, r = _post("/tareas/50/convertir-en-proyecto", campos)
    assert r.headers["location"] == "/tareas/50?error=convertir_cliente"
    assert _foto(mundo) == antes and noco.pedidos == [] and drive == []
    avisos = _avisos(_pagina_de(c, r))
    assert any("falta escoger al cliente" in a for a in avisos), avisos
    assert mundo.con.execute("SELECT estado, borrado_en FROM tareas WHERE id = 50").fetchone()[:] == ("pendiente", None)


@pytest.mark.parametrize("area", [a for a in GRUPOS_DE_TAREA if db.cliente_es_obligatorio(a)], ids=lambda a: str(a))
def test_G13_si_noco_no_contesta_no_convierte_nada(mundo, gente, noco, drive, area):
    noco.cae = True
    _tarea(mundo, 50, area)
    antes = _foto(mundo)
    c, r = _post("/tareas/50/convertir-en-proyecto", {"cliente": "101"})
    assert r.headers["location"] == "/tareas/50?error=convertir_noco"
    assert _foto(mundo) == antes and drive == []
    assert any("Noco no contestó" in a for a in _avisos(_pagina_de(c, r)))


def test_A_convertir_con_una_ficha_que_noco_no_conoce_lo_dice_y_no_convierte(mundo, gente, noco, drive):
    _tarea(mundo, 50, "CDS")
    antes = _foto(mundo)
    c, r = _post("/tareas/50/convertir-en-proyecto", {"cliente": "999"})
    assert r.headers["location"] == "/tareas/50?error=convertir_ficha"
    assert _foto(mundo) == antes and drive == []
    avisos = _avisos(_pagina_de(c, r))
    assert any("esa ficha no está en Noco" in a for a in avisos) and not any("falta escoger" in a for a in avisos)


def test_A_convertir_con_el_cliente_ocupado_nombra_al_proyecto_activo_y_lleva_abrir(mundo, gente, noco, drive):
    _p(mundo, 7, "Cliente 101", "ACD", cliente=101)
    _tarea(mundo, 50, "CDS")
    antes = _foto(mundo)
    c, r = _post("/tareas/50/convertir-en-proyecto", {"cliente": "101"})
    assert r.headers["location"] == "/tareas/50?error=convertir_ocupado&ocupa=7"
    assert _foto(mundo) == antes and drive == []
    pagina = _pagina_de(c, r)
    aviso = next(a for a in _avisos(pagina) if "proyecto activo" in a)
    assert "(«Cliente 101»)" in aviso and aviso.endswith("Abrir") and '<a href="/proyectos?p=7">Abrir</a>' in pagina
    # una dirección a mano o un proyecto que ya no ocupa: sin nombre ni enlace
    mundo.con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = 7")
    mundo.con.commit()
    from _navegador import dar_recibo
    dar_recibo("/tareas/50", error="convertir_ocupado", ocupa=7)
    h = _html.unescape(_cliente(DUENO).get("/tareas/50", params={"error": "convertir_ocupado", "ocupa": 7}).text)
    aviso = next(a for a in _avisos(h) if "proyecto activo" in a)
    assert "(«" not in aviso and "Abrir" not in aviso


@pytest.mark.parametrize("area", [a for a in GRUPOS_DE_TAREA if db.cliente_es_obligatorio(a)], ids=lambda a: str(a))
def test_G14_convertir_con_cliente_crea_el_proyecto_archiva_la_tarea_y_deja_las_dos_huellas(mundo, gente, noco, drive, area):
    _tarea(mundo, 50, area, titulo="Título de la tarea")
    c, r = _post("/tareas/50/convertir-en-proyecto", {"cliente": "103"})
    p = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(p) == 1 and (p[0]["cliente_noco_id"], p[0]["cliente_nombre"], p[0]["nombre"]) == (103, "Cliente 103", "Cliente 103")
    assert p[0]["area"] == area and (p[0]["descripcion"] or "").startswith("Título de la tarea")
    t = mundo.con.execute("SELECT estado, borrado_en FROM tareas WHERE id = 50").fetchone()
    assert t["borrado_en"] is not None
    huellas = sorted((h["tabla"], h["accion"]) for h in mundo.con.execute("SELECT * FROM log_acciones"))
    assert huellas == [("proyectos", "crear"), ("tareas", "borrar")], huellas
    assert r.headers["location"] == f"/proyectos?creado={p[0]['id']}#proyecto-{p[0]['id']}"
    assert drive == [{"id": p[0]["id"], "existe": True, "cliente": 103}]


def test_G14_en_ia_el_clic_unico_sigue_como_estaba(mundo, gente, noco, drive):
    _tarea(mundo, 50, "IA", titulo="Mejorar el prompt")
    c, r = _post("/tareas/50/convertir-en-proyecto", {})
    p = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(p) == 1 and p[0]["nombre"] == "Mejorar el prompt" and p[0]["cliente_noco_id"] is None
    assert noco.pedidos == [] and len(drive) == 1


def test_G14_el_segundo_paso_de_punta_a_punta_buscar_tocar_la_ficha_y_convertir(mundo, gente, noco, drive):
    _tarea(mundo, 50, None, titulo="Tarea sin área")
    html = _cliente(DUENO).get("/tareas/50", params={"convertir": 1, "pq": "104"}).text
    valor = re.search(r'<button name="cliente" value="(\d+)"', html).group(1)
    assert valor == "104"
    c, r = _post("/tareas/50/convertir-en-proyecto", {"cliente": valor})
    p = mundo.con.execute("SELECT * FROM proyectos").fetchall()
    assert len(p) == 1 and p[0]["cliente_noco_id"] == 104 and p[0]["area"] is None
