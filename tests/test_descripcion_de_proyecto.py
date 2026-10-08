"""«De qué se trata» (`proyectos.descripcion`) se escribe desde la página: parte 3 del
diseño de la página completa del proyecto (8-oct-2026).

Qué se vigila, por la ruta REAL (`POST /proyectos/{pid}/descripcion`), con la plantilla
REAL, `crud.editar`/`crud.perfil`/`crud.deshacer` REALES y SQL que se ejecuta de verdad
(la base de SQLite con el esquema de `db/schema.sql` que usan las pruebas de esta página):

  1. la puerta: qué texto vale (limpieza, largo, tipo, NUL), con entradas inventadas;
  2. quién puede escribir: solo la sesión de la casa (401 en solo ver y sin sesión);
  3. lo que se dice es lo que pasó: guardada / quitada / «ya decía eso» / «cambió» /
     «ya no está» / «NO se guardó», y que el texto nunca viaja en la dirección;
  4. lo que Telegram agrega con `perfil` NO se pierde al abrir y guardar sin cambios, y
     tampoco si llegó MIENTRAS se escribía (la huella `antes`);
  5. lo que se pinta sale escapado;
  6. los demás escritores de la columna: `crud.editar` por Telegram, `crud.perfil`,
     `crud.deshacer`, y quién más (sonda sobre el código, no un `grep`).

FRONTERA, dicha una vez:
  · SQLite no es Postgres: el ÉXITO de `crud.deshacer` sobre una EDICIÓN usa
    `jsonb_populate_record` y no corre aquí; sí corre su rechazo (la puerta), que va antes.
  · No es un navegador. Lo que un navegador hace con un `textarea` (quitar el primer salto
    de línea del contenido, mandar `\\r\\n`) se imita en `_como_lo_manda_un_navegador`, con
    las dos reglas de la especificación de HTML, y NO se comprobó en un navegador de verdad.
  · Dos guardados exactamente simultáneos no se prueban: la comparación de `antes` y el
    UPDATE son dos pasos dentro de la misma conexión, sin `SELECT … FOR UPDATE`.

Correr:  python3 -m pytest tests/test_descripcion_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import random
import re
from html.parser import HTMLParser

import pytest

from test_grupo_ia import _ROOT
from test_pagina_proyectos import (_cliente, gente, mundo, ver, ver_r, HOY, CREADO)  # noqa: F401
import test_crear_proyecto_telegram as _tct
import acciones.crud as crud
import config
import db.db as db
from _navegador import Navegador
import web.app as panel
import web.auth as auth

RUTA = "/proyectos/{}/descripcion"
LARGO = db.LARGO_DESCRIPCION_PROYECTO


def mandar(pid, campos, *, chat="dueno"):
    """Un POST al panel; `chat=None` sin sesión; `chat='ver'` con la sesión de solo ver."""
    c = Navegador(panel.app, base_url="https://testserver")
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c.post(RUTA.format(pid), data=campos, follow_redirects=False)


def _fila(mundo, pid=1):
    f = mundo.con.execute("SELECT * FROM proyectos WHERE id = ?", (pid,)).fetchone()
    return dict(f) if f is not None else None


def _huellas(mundo):
    return [dict(f) for f in mundo.con.execute("SELECT * FROM log_acciones ORDER BY id")]


def _poner(mundo, texto, pid=1):
    mundo.con.execute("UPDATE proyectos SET descripcion = ? WHERE id = ?", (texto, pid))


def _donde(r):
    return r.headers["location"]


def _correr(coro):
    import asyncio
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


# ── Lo que un navegador hace con el formulario ────────────────────────────

class _LeerFormulario(HTMLParser):
    """El `textarea` de la descripción y el `antes` escondido, del HTML servido."""

    def __init__(self):
        super().__init__()                      # convert_charrefs=True: `&lt;` llega como `<`
        self.texto = None
        self.antes = None
        self._en = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "textarea" and a.get("name") == "descripcion":
            self._en, self.texto = True, ""
        elif tag == "input" and a.get("name") == "antes":
            self.antes = a.get("value")

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._en = False

    def handle_data(self, d):
        if self._en:
            self.texto += d


def _como_lo_manda_un_navegador(html: str) -> dict:
    """Lo que enviaría el navegador al guardar SIN tocar nada: (1) el analizador de HTML quita
    UN salto de línea que siga a `<textarea>`; (2) al enviar, los saltos de línea del valor
    viajan como `\\r\\n`. Son las dos reglas de la especificación; no se comprobaron en un
    navegador de verdad."""
    lector = _LeerFormulario()
    lector.feed(html)
    assert lector.texto is not None and lector.antes is not None, "el formulario no está en la página"
    valor = lector.texto
    if valor.startswith("\n"):
        valor = valor[1:]
    return {"descripcion": valor.replace("\r\n", "\n").replace("\n", "\r\n"), "antes": lector.antes}


def _abrir(mundo, pid=1):
    """La página con el formulario abierto, tal como se sirve a la sesión de la casa."""
    return ver(mundo, p=pid, editar="descripcion")


@pytest.fixture
def uno(mundo):
    mundo.proyecto(1, "Disco de prueba", area="CDS", responsable=config.CHAT_ID_DUENO)
    return mundo


# ═══════════════════════════════════════════════════════════════════════
# 1. La puerta
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pedido,queda", [
    ("Grabar un EP", "Grabar un EP"),
    ("  con espacios \n", "con espacios"),
    ("uno\r\ndos\rtres\ncuatro", "uno\ndos\ntres\ncuatro"),
    ("a\n\n\nb", "a\n\n\nb"),                         # los renglones vacíos de en medio se respetan
    ("línea  \n  otra", "línea  \n  otra"),            # los de alrededor de cada renglón, también
    ("", None), ("   \n\t ", None), (None, None),
    ("ñandú — «sí» 🎵", "ñandú — «sí» 🎵"),
    ("x" * LARGO, "x" * LARGO),
    (("y" * (LARGO - 1)) + "\r\n", "y" * (LARGO - 1)),      # el `\r\n` del final no cuenta: se limpia ANTES de medir
    ("a\r\n" * 1000 + "z", ("a\n" * 1000) + "z"),             # 2001 con \r\n; 2001 caracteres limpios, tampoco entra...
])
def test_la_puerta_deja_el_texto_limpio(pedido, queda):
    if queda is not None and len(queda) > LARGO:
        with pytest.raises(db.DescripcionDeProyectoNoVale) as e:
            crud.PUERTAS["proyectos"]["descripcion"](pedido)
        assert e.value.clave == "largo"
    else:
        assert crud.PUERTAS["proyectos"]["descripcion"](pedido) == queda


@pytest.mark.parametrize("pedido,clave", [
    ("x" * (LARGO + 1), "largo"),
    ("é" * (LARGO + 1), "largo"),                   # cuenta caracteres, no bytes
    (5, "tipo"), (3.5, "tipo"), (b"bytes", "tipo"), (["a"], "tipo"), ({"a": 1}, "tipo"), (True, "tipo"),
    ("con\x00nulo", "caracteres"), ("\x00", "caracteres"),
    ("suelto\ud800roto", "caracteres"),             # un sustituto suelto no se codifica en UTF-8
])
def test_la_puerta_rechaza_con_su_clave_y_el_mensaje_no_repite_lo_pedido(pedido, clave):
    with pytest.raises(db.DescripcionDeProyectoNoVale) as e:
        crud.PUERTAS["proyectos"]["descripcion"](pedido)
    assert e.value.clave == clave and isinstance(e.value, ValueError)
    assert "bytes" not in str(e.value) and "roto" not in str(e.value) and "nulo" not in str(e.value)
    assert str(pedido)[:30] not in str(e.value) or len(str(pedido)) < 3


def test_la_puerta_con_entradas_inventadas_nunca_deja_pasar_algo_que_no_vale():
    """Textos al azar (con semilla) hechos de pedazos de los que rompen cosas: la puerta o rechaza, o
    devuelve algo limpio, corto y estable (pasarla otra vez no cambia nada)."""
    azar = random.Random(20261008)
    pedazos = ["a", "Z", "é", "ñ", " ", "  ", "\t", "\n", "\r\n", "\r", "<b>", "&amp;", "\"", "'", "🎵",
               " ", " ", "x" * 50, "· [08/10/2026] ", "\x00", "\x0b", "​"]
    vistos = {"rechazado": 0, "ok": 0, "vacio": 0}
    puerta = crud.PUERTAS["proyectos"]["descripcion"]
    for _ in range(600):
        texto = "".join(azar.choice(pedazos) for _ in range(azar.randint(0, 120)))
        if azar.random() < 0.15:
            texto *= azar.randint(10, 60)
        try:
            salida = puerta(texto)
        except db.DescripcionDeProyectoNoVale as e:
            assert e.clave in ("largo", "caracteres")
            vistos["rechazado"] += 1
            continue
        if salida is None:
            assert texto.strip() == "" or "\x00" not in texto
            vistos["vacio"] += 1
            continue
        vistos["ok"] += 1
        assert 0 < len(salida) <= LARGO and "\r" not in salida and "\x00" not in salida
        assert salida == salida.strip() and puerta(salida) == salida
    assert min(vistos.values()) > 0, vistos           # el generador ejerce las tres salidas


def test_la_huella_ignora_lo_que_la_puerta_ignora_y_distingue_el_resto():
    h = db.huella_de_descripcion
    assert h(None) == h("") == h("  \n ")
    assert h("a\nb") == h("a\r\nb") == h("  a\nb\n")
    assert h("a\nb") != h("a\n\nb") != h("a b")
    assert len(h("x")) == 64 and re.fullmatch(r"[0-9a-f]{64}", h("x"))


# ═══════════════════════════════════════════════════════════════════════
# 2. Quién puede escribir
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("quien", [None, "ver"])
def test_sin_sesion_o_en_solo_ver_la_ruta_da_401_y_no_escribe_nada(uno, quien):
    antes = _fila(uno)
    r = mandar(1, {"descripcion": "Me la cuelo", "antes": db.huella_de_descripcion(None)}, chat=quien)
    assert r.status_code == 401
    assert _fila(uno) == antes and _huellas(uno) == []


def test_la_sesion_de_la_casa_si_escribe(uno):
    r = mandar(1, {"descripcion": "Grabar un EP", "antes": db.huella_de_descripcion(None)})
    assert r.status_code == 303 and _fila(uno)["descripcion"] == "Grabar un EP"


def test_en_solo_ver_se_lee_la_descripcion_y_no_hay_ningun_control(uno):
    _poner(uno, "Grabar un EP\nPara Banda Ejemplo")
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    for consulta in ({"p": 1}, {"p": 1, "editar": "descripcion"}):
        html = c.get("/proyectos", params=consulta).text
        assert '<p class="descripcion">Grabar un EP\nPara Banda Ejemplo</p>' in html, consulta
        assert "De qué se trata" in html
        assert "/descripcion" not in html and "<textarea" not in html and "editar=descripcion" not in html
        assert ">Cambiar<" not in html and ">Escribir<" not in html


@pytest.mark.parametrize("con_texto", [True, False])
def test_la_plantilla_sola_tampoco_dibuja_nada_en_solo_ver_aunque_le_llegue_editar(uno, monkeypatch, con_texto):
    """La ruta ya apaga `editar` en solo ver (`web/app.py::proyectos`); esta prueba le quita ese apoyo: la
    plantilla recibe `editar='descripcion'` con `solo_ver=True` y no puede dibujar formulario, campo ni enlace."""
    if con_texto:
        _poner(uno, "Texto que sí se lee")
    original = panel.plantillas.TemplateResponse

    def con_editar(request, nombre, contexto=None, *a, **k):
        if nombre == "proyectos.html":
            assert contexto["solo_ver"] is True
            contexto = {**contexto, "editar": "descripcion"}
        return original(request, nombre, contexto, *a, **k)

    monkeypatch.setattr(panel.plantillas, "TemplateResponse", con_editar)
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    html = c.get("/proyectos", params={"p": 1}).text
    assert "<textarea" not in html and "/descripcion" not in html and 'name="antes"' not in html
    assert "editar=descripcion" not in html and ">Cambiar<" not in html and ">Escribir<" not in html
    assert ("Texto que sí se lee" in html) is con_texto


def test_en_solo_ver_sin_descripcion_el_bloque_no_sale(uno):
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    html = c.get("/proyectos", params={"p": 1, "editar": "descripcion"}).text
    assert "De qué se trata" not in html and "<textarea" not in html and "Todavía no hay nada escrito" not in html


def test_la_casa_ve_el_bloque_con_su_enlace_segun_haya_o_no_texto(uno):
    html = ver(uno, p=1)
    assert "De qué se trata" in html and "Todavía no hay nada escrito." in html
    assert 'href="/proyectos?p=1&amp;editar=descripcion">Escribir</a>' in html
    assert 'class="descripcion"' not in html
    assert "<textarea" not in html.split('id="notas-del-proyecto"')[0]     # (el de «Nueva nota» es otro bloque)
    _poner(uno, "Algo")
    html = ver(uno, p=1)
    assert 'href="/proyectos?p=1&amp;editar=descripcion">Cambiar</a>' in html
    assert '<p class="descripcion">Algo</p>' in html and "Todavía no hay nada escrito" not in html


# ═══════════════════════════════════════════════════════════════════════
# 3. Lo que se dice es lo que pasó
# ═══════════════════════════════════════════════════════════════════════

def test_guardar_escribe_deja_huella_de_panel_y_la_pagina_lo_dice_una_vez(uno):
    r = mandar(1, {"descripcion": "  Grabar un EP\r\nPara Banda Ejemplo  ", "antes": db.huella_de_descripcion(None)})
    assert r.status_code == 303 and _donde(r) == "/proyectos?hecho=descripcion&p=1#de-que-se-trata"
    assert _fila(uno)["descripcion"] == "Grabar un EP\nPara Banda Ejemplo"
    h, = _huellas(uno)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "proyectos", 1)
    assert h["motivo"] == "Descripción cambiada desde el panel"
    # La página que sigue a la redirección (con el recibo de la puerta de avisos) lo dice; la misma
    # dirección escrita a mano (sin recibo), no.
    html = ver(uno, hecho="descripcion", p=1)
    assert "Descripción guardada." in html
    assert '<p class="descripcion">Grabar un EP\nPara Banda Ejemplo</p>' in html
    assert "Descripción guardada." not in ver(uno, hecho="descripcion", p=1)       # el recibo se gastó


def test_una_direccion_escrita_a_mano_no_dice_que_se_guardo(uno):
    for consulta in ({"hecho": "descripcion"}, {"hecho": "descripcion_quitada"}, {"error": "descripcion_cambio"},
                     {"error": "descripcion_largo"}, {"error": "descripcion_igual"}, {"error": "descripcion_base"}):
        html = ver(uno, p=1, **consulta)
        assert "Descripción guardada" not in html and "Descripción quitada" not in html, consulta
        assert "NO se guardó" not in html and "ya decía eso" not in html and "no se pudo confirmar" not in html, consulta


def test_vaciar_la_descripcion_la_quita_y_se_dice_que_se_quito(uno):
    _poner(uno, "Algo escrito")
    r = mandar(1, {"descripcion": " \r\n ", "antes": db.huella_de_descripcion("Algo escrito")})
    assert _donde(r) == "/proyectos?hecho=descripcion_quitada&p=1#de-que-se-trata"
    assert _fila(uno)["descripcion"] is None
    assert len(_huellas(uno)) == 1
    html = ver_r(uno, hecho="descripcion_quitada", p=1)
    assert "Descripción quitada" in html and "Todavía no hay nada escrito." in html


def test_guardar_lo_mismo_no_escribe_ni_deja_huella_ni_dice_guardada(uno):
    _poner(uno, "Igual que antes")
    r = mandar(1, {"descripcion": "  Igual que antes\r\n", "antes": db.huella_de_descripcion("Igual que antes")})
    assert _donde(r) == "/proyectos?error=descripcion_igual&p=1#de-que-se-trata"
    assert _fila(uno)["descripcion"] == "Igual que antes" and _huellas(uno) == []
    html = ver_r(uno, error="descripcion_igual", p=1)
    assert "ya decía eso" in html and "Descripción guardada" not in html


def test_vaciar_lo_que_ya_estaba_vacio_tampoco_escribe(uno):
    r = mandar(1, {"descripcion": "   ", "antes": db.huella_de_descripcion(None)})
    assert _donde(r) == "/proyectos?error=descripcion_igual&p=1#de-que-se-trata"
    assert _fila(uno)["descripcion"] is None and _huellas(uno) == []


@pytest.mark.parametrize("texto,clave", [("x" * (LARGO + 1), "largo"), ("con\x00nulo", "caracteres")], ids=["largo", "caracteres"])
def test_un_texto_que_no_vale_no_se_guarda_vuelve_al_formulario_y_no_viaja_en_la_direccion(uno, texto, clave):
    _poner(uno, "Lo que ya había")
    r = mandar(1, {"descripcion": texto, "antes": db.huella_de_descripcion("Lo que ya había")})
    assert _donde(r) == f"/proyectos?error=descripcion_{clave}&p=1&editar=descripcion#de-que-se-trata"
    assert texto[:10] not in _donde(r)
    assert _fila(uno)["descripcion"] == "Lo que ya había" and _huellas(uno) == []
    html = ver_r(uno, error=f"descripcion_{clave}", p=1, editar="descripcion")
    assert "NO se guardó" in html and "Descripción guardada" not in html
    assert _como_lo_manda_un_navegador(html)["descripcion"] == "Lo que ya había"       # el formulario abre con lo que hay


def test_el_tope_exacto_entra_y_uno_mas_no(uno):
    ok = mandar(1, {"descripcion": "z" * LARGO, "antes": db.huella_de_descripcion(None)})
    assert "hecho=descripcion" in _donde(ok) and len(_fila(uno)["descripcion"]) == LARGO
    pasado = mandar(1, {"descripcion": "z" * (LARGO + 1), "antes": db.huella_de_descripcion("z" * LARGO)})
    assert "error=descripcion_largo" in _donde(pasado) and len(_fila(uno)["descripcion"]) == LARGO


def test_un_campo_que_no_es_texto_se_rechaza(uno):
    """Un POST multipart con un archivo en `descripcion`: la puerta pide un texto."""
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION))
    r = c.post(RUTA.format(1), files={"descripcion": ("x.txt", b"contenido de un archivo")},
               data={"antes": db.huella_de_descripcion(None)}, follow_redirects=False)
    assert r.status_code == 303 and "error=descripcion_tipo" in _donde(r)
    assert _fila(uno)["descripcion"] is None and _huellas(uno) == []


@pytest.mark.parametrize("estado", ["papelera", "no_existe"])
def test_un_proyecto_que_ya_no_esta_no_recibe_nada_y_se_dice(mundo, estado):
    if estado == "papelera":
        mundo.proyecto(1, "Borrado", area="CDS", borrado=True)
    r = mandar(1, {"descripcion": "Para nadie", "antes": db.huella_de_descripcion(None)})
    assert _donde(r) == "/proyectos?error=proyecto"
    assert (_fila(mundo) is None or _fila(mundo)["descripcion"] is None) and _huellas(mundo) == []
    assert "Ese proyecto ya no está." in ver_r(mundo, error="proyecto")


def test_si_la_base_falla_el_aviso_no_dice_guardada_ni_deja_una_pagina_de_error_pelada(uno, monkeypatch):
    """Una falla de la base en el guardado ya no sale como una página de error pelada: vuelve a la
    descripción con un aviso que no afirma nada que no se sepa (no se sabe si llegó a confirmar)."""
    async def revienta(*a, **k):
        raise RuntimeError("la base no contesta")
    original = crud.editar
    monkeypatch.setattr(crud, "editar", revienta)
    r = mandar(1, {"descripcion": "Algo", "antes": db.huella_de_descripcion(None)})
    assert r.status_code == 303 and _donde(r) == "/proyectos?error=descripcion_base&p=1&editar=descripcion#de-que-se-trata"
    assert "Algo" not in _donde(r)
    assert _fila(uno)["descripcion"] is None and _huellas(uno) == []
    monkeypatch.setattr(crud, "editar", original)
    html = ver_r(uno, error="descripcion_base", p=1, editar="descripcion")
    assert "no se pudo confirmar" in html and "Descripción guardada" not in html
    assert "<textarea" in html                                        # el formulario vuelve abierto, con lo que hay


# ═══════════════════════════════════════════════════════════════════════
# 4. Lo que agregó Telegram con `perfil` no se pierde
# ═══════════════════════════════════════════════════════════════════════

NOTAS = ["Para Banda Ejemplo, su mánager contesta por WhatsApp.",
         "Ojo: <b>no</b> mezclar antes del viernes & confirmar con \"el cliente\".",
         "Línea con espacios al final   ",
         "Con\ttabulador y ñ, é, 🎵"]


def _perfil(uno, nota, **k):
    return _correr(crud.perfil("proyecto", "Disco de prueba", nota=nota, **k))


def test_perfil_deja_renglones_con_fecha_y_el_panel_los_lee_igual_que_los_guarda(uno):
    """Se corre `perfil` de verdad (varias veces, con saltos de línea y de todo) y se ve qué queda."""
    for n in NOTAS:
        res, log_id = _perfil(uno, n)
        assert res.startswith("OK: perfil de") and log_id is not None
    guardado = _fila(uno)["descripcion"]
    lineas = guardado.split("\n")
    assert len(lineas) == len(NOTAS) and all(re.match(r"· \[\d\d/\d\d/\d{4}\] ", x) for x in lineas)


def test_abrir_y_guardar_sin_cambios_no_toca_lo_que_agrego_telegram(uno):
    for n in NOTAS:
        _perfil(uno, n)
    guardado = _fila(uno)["descripcion"]
    huellas_antes = len(_huellas(uno))
    html = _abrir(uno)
    envio = _como_lo_manda_un_navegador(html)
    assert envio["descripcion"].replace("\r\n", "\n").strip() == guardado.strip()      # el navegador manda lo que se sirvió
    r = mandar(1, envio)
    assert _donde(r) == "/proyectos?error=descripcion_igual&p=1#de-que-se-trata"
    assert _fila(uno)["descripcion"] == guardado                                       # byte por byte
    assert len(_huellas(uno)) == huellas_antes                                         # ni una huella nueva


def test_abrir_y_guardar_sin_cambios_con_texto_que_empieza_con_saltos_y_espacios(uno):
    """Texto que dejó otro camino (el detalle de una tarea convertida, Telegram `editar`) con espacios y
    saltos de línea al principio y al final: el formulario lo trae y guardar sin tocar no lo cambia."""
    crudo = "\n\n  Con sangría al principio\r\n\r\ny un renglón vacío  \n\n"
    _poner(uno, crudo)
    envio = _como_lo_manda_un_navegador(_abrir(uno))
    r = mandar(1, envio)
    assert "error=descripcion_igual" in _donde(r), _donde(r)
    assert _fila(uno)["descripcion"] == crudo and _huellas(uno) == []


def test_lo_que_llega_por_telegram_entre_abrir_el_formulario_y_guardar_no_se_pisa_EN_SECUENCIA(uno):
    """EN SECUENCIA (cada paso termina antes del siguiente; el intercalado de verdad está en la sección 7).
    Se abre el formulario, Telegram agrega un renglón con `perfil`, y se guarda lo que se veía: NO se
    escribe encima; la página vuelve con el formulario abierto y el texto como está AHORA."""
    _perfil(uno, "Primer renglón")
    abierto = _como_lo_manda_un_navegador(_abrir(uno))
    _perfil(uno, "Renglón que llegó por Telegram")
    con_los_dos = _fila(uno)["descripcion"]
    huellas_antes = _huellas(uno)
    r = mandar(1, {**abierto, "descripcion": abierto["descripcion"] + "\r\nLo que escribí yo"})
    assert _donde(r) == "/proyectos?error=descripcion_cambio&p=1&editar=descripcion#de-que-se-trata"
    assert _fila(uno)["descripcion"] == con_los_dos and _huellas(uno) == huellas_antes
    html = ver_r(uno, error="descripcion_cambio", p=1, editar="descripcion")
    assert "lo que escribiste NO se guardó" in html and "Renglón que llegó por Telegram" in html
    # Con la huella nueva (la del formulario que vuelve abierto) ya se puede guardar.
    nuevo = _como_lo_manda_un_navegador(html)
    r = mandar(1, {**nuevo, "descripcion": nuevo["descripcion"] + "\r\nLo que escribí yo"})
    assert "hecho=descripcion" in _donde(r)
    assert _fila(uno)["descripcion"].endswith("Renglón que llegó por Telegram\nLo que escribí yo")


def test_dos_formularios_abiertos_con_la_misma_huella_el_segundo_en_llegar_no_pisa_EN_SECUENCIA(uno):
    abierto = _como_lo_manda_un_navegador(_abrir(uno))
    primera = mandar(1, {**abierto, "descripcion": "Escribió la primera"})
    segunda = mandar(1, {**abierto, "descripcion": "Escribió la segunda"})
    assert "hecho=descripcion" in _donde(primera) and "error=descripcion_cambio" in _donde(segunda)
    assert _fila(uno)["descripcion"] == "Escribió la primera" and len(_huellas(uno)) == 1


@pytest.mark.parametrize("campos", [{"descripcion": "Sin el campo antes"},
                                    {"descripcion": "Con antes vacío", "antes": ""},
                                    {"descripcion": "Con antes inventado", "antes": "0" * 64}])
def test_un_post_que_no_trae_la_huella_buena_no_escribe(uno, campos):
    _poner(uno, "Lo que había")
    r = mandar(1, campos)
    assert "error=descripcion_cambio" in _donde(r)
    assert _fila(uno)["descripcion"] == "Lo que había" and _huellas(uno) == []


def test_el_formulario_de_un_proyecto_sin_descripcion_trae_la_huella_de_nada(uno):
    envio = _como_lo_manda_un_navegador(_abrir(uno))
    assert envio["descripcion"] == "" and envio["antes"] == db.huella_de_descripcion(None)


def test_telegram_puede_seguir_agregando_despues_de_que_el_panel_escribio(uno):
    mandar(1, {"descripcion": "Escrito en el panel", "antes": db.huella_de_descripcion(None)})
    _perfil(uno, "Y esto por Telegram")
    d = _fila(uno)["descripcion"]
    assert d.startswith("Escrito en el panel\n· [") and d.endswith("] Y esto por Telegram")


# ═══════════════════════════════════════════════════════════════════════
# 5. Lo que se pinta sale escapado
# ═══════════════════════════════════════════════════════════════════════

INYECCIONES = ["<script>alert(1)</script>", "</textarea><script>alert(2)</script>", "\" onfocus=\"alert(3)",
               "<img src=x onerror=alert(4)>", "&lt;b&gt;ya escapado&lt;/b&gt;", "'; DROP TABLE proyectos; --",
               "{{ 7*7 }} {% if 1 %}x{% endif %}", "</p><h1>otro título</h1>"]


@pytest.mark.parametrize("texto", INYECCIONES)
def test_el_texto_sale_escapado_en_la_lectura_y_en_el_formulario(uno, texto):
    _poner(uno, texto)
    for consulta in ({"p": 1}, {"p": 1, "editar": "descripcion"}):
        html = ver(uno, **consulta)
        for malo in ("<script>alert", "<img src=x", "onfocus=\"alert", "<h1>otro", "</textarea><script"):
            assert malo not in html, (consulta, malo)
    # y el formulario devuelve EXACTAMENTE lo guardado (escapar no cambia el texto)
    assert _como_lo_manda_un_navegador(ver(uno, p=1, editar="descripcion"))["descripcion"] == texto.replace("\n", "\r\n")
    assert "49" not in re.sub(r"[^0-9]", " ", ver(uno, p=1).split('id="de-que-se-trata"')[1].split("</section>")[0]).split()


def test_guardar_un_texto_hostil_lo_guarda_tal_cual_y_no_lo_ejecuta(uno):
    hostil = "</textarea><script>alert(9)</script> & <b>x</b>"
    mandar(1, {"descripcion": hostil, "antes": db.huella_de_descripcion(None)})
    assert _fila(uno)["descripcion"] == hostil
    assert "<script>alert(9)" not in ver(uno, p=1)


def test_la_huella_escondida_es_solo_hexadecimal(uno):
    _poner(uno, "<script>")
    lector = _LeerFormulario()
    lector.feed(_abrir(uno))
    assert re.fullmatch(r"[0-9a-f]{64}", lector.antes)


# ═══════════════════════════════════════════════════════════════════════
# 6. Los demás escritores de la columna
# ═══════════════════════════════════════════════════════════════════════

def test_telegram_editar_pasa_por_la_misma_puerta_del_largo(uno):
    with pytest.raises(ValueError) as e:
        _correr(crud.editar("proyectos", 1, {"descripcion": "x" * (LARGO + 1)}, motivo="Telegram"))
    assert str(e.value) == f"No cambié nada: la descripción del proyecto no puede pasar de {LARGO} caracteres."
    assert isinstance(e.value.__cause__, db.DescripcionDeProyectoNoVale) and e.value.__cause__.clave == "largo"
    assert _fila(uno)["descripcion"] is None and _huellas(uno) == []
    despues, log_id = _correr(crud.editar("proyectos", 1, {"descripcion": "  ok \r\n"}, motivo="Telegram"))
    assert despues["descripcion"] == "ok" and log_id is not None and despues.escritas == {"descripcion"}


def test_telegram_editar_con_lo_mismo_no_escribe_y_con_espacios_la_quita(uno):
    _poner(uno, "Algo")
    despues, log_id = _correr(crud.editar("proyectos", 1, {"descripcion": " Algo "}, motivo="Telegram"))
    assert log_id is None and _huellas(uno) == []                       # sin huella: el agente dice «SIN CAMBIOS»
    despues, log_id = _correr(crud.editar("proyectos", 1, {"descripcion": "  "}, motivo="Telegram"))
    assert despues["descripcion"] is None and log_id is not None


def test_telegram_editar_con_lo_mismo_y_otro_campo_solo_escribe_el_otro(uno):
    _poner(uno, "Algo")
    despues, log_id = _correr(crud.editar("proyectos", 1, {"descripcion": "Algo", "estado": "pausado"}, motivo="T"))
    assert despues.escritas == {"estado"} and _fila(uno)["estado"] == "pausado" and log_id is not None


def test_perfil_no_deja_pasar_el_largo_ni_con_la_nota_ni_con_la_descripcion(uno):
    _poner(uno, "a" * (LARGO - 10))
    with pytest.raises(ValueError, match="No cambié nada: la descripción del proyecto no puede pasar de"):
        _perfil(uno, "una nota que ya no cabe en lo que queda")
    assert _fila(uno)["descripcion"] == "a" * (LARGO - 10) and _huellas(uno) == []
    with pytest.raises(ValueError, match="no puede pasar de"):
        _perfil(uno, None, descripcion="b" * (LARGO + 1))
    assert _fila(uno)["descripcion"] == "a" * (LARGO - 10)
    _poner(uno, "a" * (LARGO - 60))
    res, log_id = _perfil(uno, "cabe")               # y si cabe, entra
    assert log_id is not None and _fila(uno)["descripcion"].endswith("] cabe")
    assert len(_fila(uno)["descripcion"]) <= LARGO


def test_perfil_con_la_descripcion_entera_la_reemplaza_por_la_puerta(uno):
    _poner(uno, "vieja")
    _perfil(uno, None, descripcion="  nueva \r\n con salto ")
    assert _fila(uno)["descripcion"] == "nueva \n con salto"


def test_deshacer_no_devuelve_una_descripcion_que_la_puerta_no_deja_pasar(uno):
    """El rechazo de `deshacer` (la puerta corre ANTES del UPDATE y sí corre en SQLite). El éxito de
    deshacer una edición usa `jsonb_populate_record`: es de Postgres y no se prueba aquí."""
    larga = "q" * (LARGO + 500)                                    # lo que dejó, p. ej., una tarea convertida
    _poner(uno, "Corta")
    uno.con.execute(
        "INSERT INTO log_acciones (id, actor, accion, tabla, registro_id, antes, despues, motivo) "
        "VALUES (900, 'panel', 'editar', 'proyectos', 1, ?, ?, 'x')",
        (__import__("json").dumps({"id": 1, "descripcion": larga}),
         __import__("json").dumps({"id": 1, "descripcion": "Corta"})))
    with pytest.raises(ValueError) as e:
        _correr(crud.deshacer(900))
    assert str(e.value) == ("No lo deshice: el proyecto volvería a tener la descripción que tenía, y la "
                            f"descripción del proyecto no puede pasar de {LARGO} caracteres.")
    assert _fila(uno)["descripcion"] == "Corta"


def test_la_edicion_guarda_el_antes_completo_para_poder_deshacer(uno):
    """Lo que lee `deshacer` es la fila entera de `antes`: la huella la trae con la descripción de antes."""
    import json
    _poner(uno, "Antes")
    mandar(1, {"descripcion": "Después", "antes": db.huella_de_descripcion("Antes")})
    h, = _huellas(uno)
    assert json.loads(h["antes"])["descripcion"] == "Antes" and json.loads(h["despues"])["descripcion"] == "Después"


def test_convertir_una_tarea_en_proyecto_copia_el_detalle_sin_la_puerta_FRONTERA(mundo):
    """FRONTERA declarada, no una garantía: `db.convertir_tarea_en_proyecto` copia `tareas.detalle` con su
    propio INSERT y NO pasa por `crud.PUERTAS`, así que un detalle de más de `LARGO` caracteres (el que
    deja Telegram, que no lo limita: `acciones/crud.py` lo lee con `str(...).strip()`) queda como
    descripción. Si algún día se le pone la puerta, esta prueba cambia a propósito."""
    mundo.tarea(50, "Una tarea larga")
    mundo.con.execute("UPDATE tareas SET detalle = ? WHERE id = 50", ("d" * (LARGO + 100),))
    _correr(db.convertir_tarea_en_proyecto(50))
    nuevo = mundo.con.execute("SELECT descripcion FROM proyectos").fetchone()[0]
    assert nuevo == "d" * (LARGO + 100)
    # y el panel con ese texto: se enseña entero, y guardar (con o sin cambios) dice que NO se guardó
    # por el tope --la puerta corre antes de mirar si cambió--: no se pierde nada, hay que recortarlo.
    pid = mundo.con.execute("SELECT id FROM proyectos").fetchone()[0]
    huella = db.huella_de_descripcion(nuevo)
    assert "d" * (LARGO + 100) in ver(mundo, p=pid)
    for texto in (nuevo, nuevo + "!"):
        r = mandar(pid, {"descripcion": texto, "antes": huella})
        assert "error=descripcion_largo" in _donde(r) and "editar=descripcion" in _donde(r)
    assert mundo.con.execute("SELECT descripcion FROM proyectos").fetchone()[0] == nuevo
    assert mundo.con.execute("SELECT count(*) FROM log_acciones WHERE tabla = 'proyectos' AND accion = 'editar'").fetchone()[0] == 0
    recortado = mandar(pid, {"descripcion": "d" * LARGO, "antes": huella})
    assert "hecho=descripcion" in _donde(recortado)


# ── La sonda: quién más escribe `proyectos.descripcion` ───────────────────

_SQL = re.compile(r"(INSERT\s+INTO|UPDATE)\s+([\w{}…]+)(.*)", re.I | re.S)


def _escritores_de_sql():
    """{(archivo, función): [(verbo, tabla, texto)]} de TODO el SQL de escritura que hay fuera de `tests/`:
    cadenas y f-strings rearmados (donde interpolan, `…`). Lo mismo que usa el censo de creadores de
    `tests/test_crear_proyecto_telegram.py`: un f-string no es un `ast.Constant`, y por eso un `grep` no
    ve a quien arma la tabla al vuelo."""
    salida = {}
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            for texto in _tct._sql_de_una_funcion(fn):
                for m in _SQL.finditer(texto):
                    salida.setdefault((rel, fn.name), []).append(
                        (m.group(1).upper().split()[0], m.group(2), m.group(3)))
    return salida


def test_sonda_los_escritores_con_la_tabla_al_vuelo_son_estos_y_solo_dos_pueden_escribir_la_descripcion():
    """Los escritores cuyo SQL interpola la tabla (los que un `grep` de `UPDATE proyectos` no ve) son
    estos seis. Cuatro escriben UNA columna fija que no es `descripcion`; `editar` y `deshacer` escriben
    las que les pidan, y por eso se prueban arriba contra la puerta. Un séptimo escritor genérico pone
    roja esta prueba hasta que alguien lo clasifique."""
    genericos = {k: v for k, v in _escritores_de_sql().items() if any("…" in t for _, t, _ in v)}
    assert set(genericos) == {
        ("acciones/crud.py", "borrar"), ("acciones/crud.py", "deshacer"), ("acciones/crud.py", "editar"),
        ("cerebro/despertador.py", "revisar"), ("db/db.py", "_buscar_o_crear"),
        ("tools/rellenar_duenos.py", "main")}, sorted(genericos)
    fijos = {("acciones/crud.py", "borrar"): "borrado_en",
             ("cerebro/despertador.py", "revisar"): "avisos_enviados",
             ("tools/rellenar_duenos.py", "main"): "bandeja_id"}
    for clave, columna in fijos.items():
        resto = " ".join(r for _, t, r in genericos[clave] if "…" in t)
        assert re.search(rf"\bSET\s+{columna}\b", resto), (clave, resto[:80])
        assert "descripcion" not in resto
    insert = " ".join(r for _, t, r in genericos[("db/db.py", "_buscar_o_crear")] if "…" in t)
    assert re.match(r"\s*\(nombre\)", insert) and "descripcion" not in insert
    # `borrar` también tiene un UPDATE de `borrado_en` por tabla; ninguno de los fijos nombra la columna.
    # Los dos libres: ¿llaman a la puerta? (el árbol de llamadas, no un nombre)
    fuente = ast.parse((_ROOT / "acciones" / "crud.py").read_text(encoding="utf-8"))
    for nombre in ("editar", "deshacer"):
        f = next(n for n in ast.walk(fuente) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == nombre)
        llamadas = {n.func.id for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_por_las_puertas" in llamadas, nombre


def test_sonda_el_unico_sql_a_mano_que_escribe_la_descripcion_es_convertir_tarea_en_proyecto():
    quien = set()
    for (rel, fn), sentencias in _escritores_de_sql().items():
        for verbo, tabla, resto in sentencias:
            if tabla.lower() == "proyectos" and re.search(r"\bdescripcion\b", resto.split("VALUES")[0].split("WHERE")[0]):
                quien.add((rel, fn))
    assert quien == {("db/db.py", "convertir_tarea_en_proyecto")}


def test_sonda_la_sonda_ve_un_escritor_inventado(tmp_path):
    texto = ("async def inventado(c, tabla):\n"
             "    await c.execute(f'UPDATE {tabla} SET descripcion = %s WHERE id = 1')\n")
    arbol = ast.parse(texto)
    f = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef))
    sql = _tct._sql_de_una_funcion(f)
    assert any(_SQL.search(s) and "…" in _SQL.search(s).group(2) and "descripcion" in s for s in sql), sql


def test_sonda_en_marcha_todo_lo_que_cambia_la_descripcion_deja_huella_con_el_antes(uno):
    """La sonda de ejecución: con un registro de TODO el SQL que corre contra la base de prueba, se
    ejercen los tres caminos que escriben (panel, Telegram `editar`, `perfil`) y se mira qué
    sentencias tocaron `proyectos`: solo el `UPDATE proyectos SET descripcion` de `editar`, cada una
    con su huella `editar` en la misma conexión."""
    visto = []
    uno.con.set_trace_callback(visto.append)
    try:
        mandar(1, {"descripcion": "Panel", "antes": db.huella_de_descripcion(None)})
        _correr(crud.editar("proyectos", 1, {"descripcion": "Telegram editar"}, motivo="t"))
        _perfil(uno, "perfil")
    finally:
        uno.con.set_trace_callback(None)
    escrituras = [s for s in visto if re.match(r"\s*UPDATE\s+proyectos\s+SET\s+descripcion\s*=", s, re.I)]
    assert len(escrituras) == 3, escrituras
    assert len(_huellas(uno)) == 3 and {h["accion"] for h in _huellas(uno)} == {"editar"}


# ═══════════════════════════════════════════════════════════════════════
# 7. El intercalado de verdad: leer y escribir no se pueden separar por una escritura ajena
# ═══════════════════════════════════════════════════════════════════════
#
# LO QUE SE VIGILA: el candado de `crud.editar` (`UPDATE … AND descripcion IS NOT DISTINCT FROM <lo que
# editar leyó>`) cubre solo la ventana entre el `SELECT` y el `UPDATE` de `editar`; la huella
# (`si_sigue_igual`) cubre la ventana anterior, y solo la pasan el panel y `perfil` (ésta, con la de la
# fila sobre la que calculó su renglón). Telegram `editar` no pasa huella: NO está cubierto contra una
# escritura ajena anterior a su lectura (su prueba de abajo mide solo la ventana del candado).
# `deshacer` se niega si la descripción cambió después de esa edición.
#
# CÓMO SE INTERCALA: el pool de prueba deja correr UNA escritura ajena (un `UPDATE` hecho como si lo
# confirmara otra conexión) justo antes de que `editar` ejecute su `UPDATE proyectos SET descripcion`, es decir
# entre su lectura y su escritura. El SQL es el de verdad, sobre SQLite.
# FRONTERA: SQLite tiene una sola conexión y un solo hilo aquí; que, en Postgres, un `UPDATE … WHERE col IS NOT
# DISTINCT FROM x` vuelva a evaluar la condición contra la fila que otra transacción acaba de confirmar (READ
# COMMITTED) no corre en estas pruebas. Para medirlo hace falta una base Postgres real y dos conexiones
# concurrentes (p. ej. un guion de `tools/` contra una base de pruebas, nunca la de producción); `tools/humo.py`
# hoy solo ejerce lecturas de la página (`pagina_de_proyectos`), no esta escritura.

import test_grupo_ia as _g


class _PoolConGancho(_g._Pool):
    """Como `_Pool`, pero antes del `UPDATE proyectos SET descripcion` de `editar` ejecuta `gancho(con)` (una
    vez, o siempre con `siempre=True`): la escritura que otro confirmó entre la lectura y la escritura."""

    def __init__(self, con, gancho, siempre=False):
        super().__init__(con)
        self.gancho, self.siempre, self.veces = gancho, siempre, 0

    def connection(self):
        pool = self
        base = super().connection()

        class _Cm:
            async def __aenter__(s):
                conn = await base.__aenter__()
                original = conn.execute

                async def execute(sql, params=()):
                    if pool.gancho and str(sql).startswith("UPDATE proyectos SET descripcion"):
                        pool.veces += 1
                        gancho = pool.gancho
                        if not pool.siempre:
                            pool.gancho = None
                        gancho(pool.con)
                    return await original(sql, params)
                conn.execute = execute
                return conn

            async def __aexit__(s, *e):
                return await base.__aexit__(*e)
        return _Cm()


def _ajeno(texto):
    def escribir(con):
        con.execute("UPDATE proyectos SET descripcion = ? WHERE id = 1", (texto,))
    return escribir


def _con_gancho(monkeypatch, uno, gancho, siempre=False):
    pool = _PoolConGancho(uno.con, gancho, siempre)
    monkeypatch.setattr(db, "pool", pool)
    return pool


def test_el_panel_no_pisa_una_escritura_que_cayo_entre_su_lectura_y_su_escritura(uno, monkeypatch):
    """Dos guardados casi simultáneos del panel que PASAN los dos la comparación de la huella: el segundo
    no escribe, no dice «guardada» y no deja huella."""
    _poner(uno, "Base")
    abierto = db.huella_de_descripcion("Base")
    pool = _con_gancho(monkeypatch, uno, _ajeno("Base\nEscrito por la otra sesión"))
    r = mandar(1, {"descripcion": "Base\nEscrito por mí", "antes": abierto})
    assert pool.veces == 1                                              # el intercalado ocurrió de verdad
    assert _donde(r) == "/proyectos?error=descripcion_cambio&p=1&editar=descripcion#de-que-se-trata"
    assert _fila(uno)["descripcion"] == "Base\nEscrito por la otra sesión" and _huellas(uno) == []


def test_telegram_editar_no_pisa_una_escritura_que_cayo_entre_su_lectura_y_su_escritura(uno, monkeypatch):
    _poner(uno, "Base")
    _con_gancho(monkeypatch, uno, _ajeno("Base\nOtro"))
    with pytest.raises(crud.CambioAlEditar):
        _correr(crud.editar("proyectos", 1, {"descripcion": "Lo mío"}, motivo="t"))
    assert _fila(uno)["descripcion"] == "Base\nOtro" and _huellas(uno) == []


def test_perfil_no_pierde_un_guardado_del_panel_que_cae_entre_su_lectura_y_su_escritura(uno, monkeypatch):
    """La carrera del testigo (H1): el guardado del panel cae entre la lectura de `perfil` y su llamada a
    `editar`. `perfil` reintenta sobre lo nuevo y quedan los DOS textos."""
    _poner(uno, "Lo que había")
    abierto = _como_lo_manda_un_navegador(_abrir(uno))
    original = crud.editar
    pasos = {"n": 0}

    async def editar_con_carrera(tabla, rid, cambios, motivo, **kw):
        if tabla == "proyectos" and kw.get("actor", "lucy") == "lucy" and pasos["n"] == 0:
            pasos["n"] = 1
            await original("proyectos", rid, {"descripcion": "Texto del panel"}, motivo="panel", actor="panel",
                           si_sigue_igual={"descripcion": abierto["antes"]})
        return await original(tabla, rid, cambios, motivo, **kw)

    monkeypatch.setattr(crud, "editar", editar_con_carrera)
    res, log_id = _perfil(uno, "Renglón de Telegram")
    final = _fila(uno)["descripcion"]
    assert log_id is not None and res.startswith("OK: perfil de")
    assert final.startswith("Texto del panel\n· [") and final.endswith("] Renglón de Telegram"), final
    assert "Lo que había" not in final                    # el panel había reemplazado ese texto; perfil sumó el suyo encima


def test_perfil_no_pierde_la_escritura_ajena_que_cae_justo_antes_del_update(uno, monkeypatch):
    """Lo mismo en el último instante (entre el `SELECT` de `editar` y su `UPDATE`): el candado del UPDATE
    lo ataja y `perfil` vuelve a leer."""
    _poner(uno, "Base")
    pool = _con_gancho(monkeypatch, uno, _ajeno("Base\nLlegó en el medio"))
    res, log_id = _perfil(uno, "Mi renglón")
    assert pool.veces == 1 and log_id is not None             # la escritura ajena cayó una vez, de verdad
    final = _fila(uno)["descripcion"]
    assert final.startswith("Base\nLlegó en el medio\n· [") and final.endswith("] Mi renglón"), final
    assert len(_huellas(uno)) == 1                        # la tentativa fallida no dejó huella


def test_perfil_se_rinde_diciendolo_si_la_descripcion_no_deja_de_cambiar(uno, monkeypatch):
    _poner(uno, "Base")
    cuenta = {"n": 0}

    def siempre_otra(con):
        cuenta["n"] += 1
        con.execute("UPDATE proyectos SET descripcion = ? WHERE id = 1", (f"Base {cuenta['n']}",))
    _con_gancho(monkeypatch, uno, siempre_otra, siempre=True)
    with pytest.raises(ValueError, match="No anoté nada: la descripción del proyecto sigue cambiando"):
        _perfil(uno, "Nunca entra")
    assert cuenta["n"] == 3 and "Nunca entra" not in _fila(uno)["descripcion"] and _huellas(uno) == []


def test_el_candado_solo_esta_en_la_descripcion_y_el_resto_de_las_ediciones_escribe_como_antes(uno, monkeypatch):
    """HERMANOS: las demás columnas y tablas escriben con el UPDATE de siempre (`SET … WHERE id = N`, sin
    condición sobre el valor). La lista de columnas con candado es esta y solo esta."""
    assert crud._COLUMNAS_CON_CANDADO == {("proyectos", "descripcion")}
    uno.tarea(7, "una tarea", proyecto=1)
    visto = []
    uno.con.set_trace_callback(visto.append)
    try:
        for tabla, rid, cambios in (
                ("proyectos", 1, {"nombre": "Otro nombre"}), ("proyectos", 1, {"estado": "pausado"}),
                ("proyectos", 1, {"area": "ACD"}), ("proyectos", 1, {"responsable_chat_id": config.CHAT_ID_DUENO}),
                ("tareas", 7, {"titulo": "Otro"}), ("tareas", 7, {"detalle": "texto"}),
                ("proyectos", 1, {"descripcion": "Con candado"})):
            _correr(crud.editar(tabla, rid, cambios, motivo="t"))
    finally:
        uno.con.set_trace_callback(None)
    updates = [q for q in visto if re.match(r"\s*UPDATE\s+(proyectos|tareas)\s+SET", q)]
    assert len(updates) == 7, updates
    for q in updates[:-1]:
        assert re.fullmatch(r"UPDATE (proyectos|tareas) SET \w+ = .+ WHERE id = \d+", q) and "DISTINCT" not in q, q
    assert updates[-1].endswith("AND descripcion IS NOT DISTINCT FROM NULL"), updates[-1]


# ── deshacer ──────────────────────────────────────────────────────────────

import test_nombre_de_proyecto as _tn


def test_deshacer_una_edicion_de_la_descripcion_la_devuelve_si_nadie_la_toco_despues():
    b = _tn.Base()
    pid = b.proyecto("P")
    b.con.execute("UPDATE proyectos SET descripcion = 'Antes' WHERE id = ?", (pid,))
    _, l1 = _tn._correr(b, lambda: crud.editar("proyectos", pid, {"descripcion": "Después"}, motivo="uno"))
    que = _tn._correr(b, lambda: crud.deshacer(l1))
    assert que == "el cambio" and b.con.execute("SELECT descripcion FROM proyectos").fetchone()[0] == "Antes"
    sql = [q for q in b.sql if "jsonb_populate_record" in q][-1]
    assert sql.endswith("WHERE t.id = %s AND t.descripcion IS NOT DISTINCT FROM %s"), sql


def test_deshacer_se_niega_si_la_descripcion_cambio_despues_y_no_borra_lo_escrito():
    """H2: deshacer un guardado viejo no borra los renglones que llegaron después."""
    b = _tn.Base()
    pid = b.proyecto("P")
    b.con.execute("UPDATE proyectos SET descripcion = 'Antes' WHERE id = ?", (pid,))
    _, l1 = _tn._correr(b, lambda: crud.editar("proyectos", pid, {"descripcion": "Panel"}, motivo="uno"))
    _tn._correr(b, lambda: crud.perfil("proyecto", "P", nota="Llegó después por Telegram"))
    antes_de_deshacer = b.con.execute("SELECT descripcion FROM proyectos").fetchone()[0]
    assert "Llegó después por Telegram" in antes_de_deshacer
    huellas = len(b.huellas())
    with pytest.raises(ValueError) as e:
        _tn._correr(b, lambda: crud.deshacer(l1))
    assert str(e.value) == crud.DESHACER_PISARIA_LA_DESCRIPCION == (
        "No lo deshice: la descripción cambió después de esa edición y volver atrás borraría lo escrito "
        "después. Si quieres la de antes, escríbela de nuevo.")
    assert b.con.execute("SELECT descripcion FROM proyectos").fetchone()[0] == antes_de_deshacer
    assert len(b.huellas()) == huellas                                # ni huella de deshacer


def test_deshacer_tambien_se_niega_si_el_cambio_cae_entre_su_lectura_y_su_escritura(monkeypatch):
    """El UPDATE de `deshacer` lleva el candado; si no tocó ninguna fila, se niega con la misma frase.
    (El emulador de `jsonb_populate_record` de esta base de prueba no aplica la condición: aquí se
    simula el resultado «0 filas» y se comprueba la rama; la condición misma se lee en el SQL.)"""
    b = _tn.Base()
    pid = b.proyecto("P")
    b.con.execute("UPDATE proyectos SET descripcion = 'Antes' WHERE id = ?", (pid,))
    _, l1 = _tn._correr(b, lambda: crud.editar("proyectos", pid, {"descripcion": "Después"}, motivo="uno"))

    def cero_filas(self, sql, params):
        if not re.search(r"UPDATE (\w+) t SET (.+?) FROM jsonb_populate_record", " ".join(sql.split())):
            return None
        return self._b.con.execute("UPDATE proyectos SET descripcion = descripcion WHERE 1 = 0")
    monkeypatch.setattr(_tn._Cur, "_emular_deshacer", cero_filas)
    with pytest.raises(ValueError, match="volver atrás borraría lo escrito después"):
        _tn._correr(b, lambda: crud.deshacer(l1))
    assert b.con.execute("SELECT descripcion FROM proyectos").fetchone()[0] == "Después"


def test_deshacer_de_las_otras_columnas_sigue_como_en_06037e2_sin_mirar_el_valor_actual():
    """HERMANOS (no es una garantía deseable: es lo que ya hacía y este trabajo no cambia): deshacer una edición
    de `estado` devuelve el `antes` aunque el estado haya cambiado después, y su UPDATE no lleva condición."""
    b = _tn.Base()
    pid = b.proyecto("P")
    _, l1 = _tn._correr(b, lambda: crud.editar("proyectos", pid, {"estado": "pausado"}, motivo="uno"))
    _tn._correr(b, lambda: crud.editar("proyectos", pid, {"estado": "cerrado"}, motivo="dos"))
    _tn._correr(b, lambda: crud.deshacer(l1))
    assert b.con.execute("SELECT estado FROM proyectos").fetchone()[0] == "activo"
    sql = [q for q in b.sql if "jsonb_populate_record" in q][-1]
    assert "DISTINCT" not in sql and sql.endswith("WHERE t.id = %s")
