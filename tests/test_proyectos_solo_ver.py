"""La sesión de «solo ver»: G2-1, G2-2 y G2-10 del diseño aprobado el 1-oct-2026,
y que la página de Proyectos, vista con ella, no ofrece nada que cambie algo.

QUÉ SE VIGILA, EN TRES CAPAS (Tiziano: «solo miran: no ven botones y, si lo
intentan a mano, Lucy lo rechaza»):
  1. EL SERVIDOR RECHAZA. Con la cookie de ver, TODA ruta registrada en la
     aplicación responde 401, salvo las que declaran `PUERTA_VER`, que
     responden 200. La lista sale de `panel.app.routes`, no de una lista escrita
     a mano, y cada ruta DECLARA su puerta (`@auth.puerta`): lo no declarado cae
     en rojo, también una ruta inventada por la propia prueba.
  2. LA PÁGINA NO OFRECE. Se pinta con la cookie de ver (la ruta real, la
     plantilla real) y se mira lo pintado: ni un formulario que escriba, ni un
     campo, ni un botón, ni un guion, ni un enlace a algo que no sea de ver. La
     lista de rutas que escriben sale de lo registrado.
  3. LA PÁGINA NO MIENTE. Con direcciones armadas a mano que harían decir
     «guardado», «creado», «hecha»… no sale ningún aviso de esos.

LA FRONTERA DE LA CAPA 2: el detector mira el HTML ya pintado (etiquetas,
atributos y enlaces). No ejecuta JavaScript; por eso la página de ver NO lleva
ningún `<script>` y eso también se exige. Un control que se fabricara con
JavaScript queda fuera de lo que esto ve.

Correr:  python3 -m pytest tests/test_proyectos_solo_ver.py -q
"""
from __future__ import annotations

import html as html_lib
import os
import re
from html.parser import HTMLParser

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import _proyectos_de_prueba as pp  # noqa: E402
import config  # noqa: E402
import web.api_code as api_code  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO


@pytest.fixture(autouse=True)
def _sin_contar_claves_malas_de_code(monkeypatch):
    """Estas pruebas le tocan a TODA ruta, también a las de `/api/code`, sin clave:
    cada una cuenta como «clave inválida» y a los 10 intentos esa puerta le manda
    un aviso por Telegram al dueño. Se apaga el conteo (ni contadores globales
    sucios para otras pruebas, ni un mensaje que salga de verdad)."""
    monkeypatch.setattr(api_code, "_registrar_clave_mala", lambda: None)


def cliente(con: str | None = "ver") -> TestClient:
    c = TestClient(panel.app, base_url="https://testserver")
    if con == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    elif con == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    return c


# ═══════════════════════════════════════════════════════════════════════
# Las puertas de cada ruta (lo registrado, no una lista tecleada)
# ═══════════════════════════════════════════════════════════════════════

def _metodos(ruta) -> list[str]:
    return sorted((getattr(ruta, "methods", None) or set()) - {"HEAD"})


def _puerta_de(ruta) -> str | None:
    """La puerta declarada: `@auth.puerta` en el endpoint, o `code` si es una
    ruta de `/api/code` que de verdad exige un permiso (lo que `requiere()` le
    cuelga como dependencia). Nada más cuenta."""
    declarada = getattr(getattr(ruta, "endpoint", None), "puerta", None)
    if declarada in (auth.PUERTA_SIEMPRE, auth.PUERTA_VER, auth.PUERTA_ENTRADA):
        return declarada
    if getattr(ruta, "path", "").startswith(api_code.router.prefix):
        permisos = {p for m, path, p in api_code.rutas_registradas(panel.app)
                    if path == ruta.path}
        if permisos and None not in permisos:
            return auth.PUERTA_CODE
    return None


def _url_de(ruta) -> str:
    return re.sub(r"\{[^}]+\}", "1", ruta.path)


def revisar_puertas(app) -> list[str]:
    """Recorre `app.routes` y devuelve todo lo que está mal. Vacío = bien."""
    mal: list[str] = []
    for ruta in app.routes:
        if not hasattr(ruta, "endpoint"):
            mal.append(f"{getattr(ruta, 'path', '?')}: ruta que no es de función (¿un montaje?)")
            continue
        puerta = _puerta_de(ruta)
        metodos = _metodos(ruta)
        if puerta is None:
            mal.append(f"{ruta.path} {metodos}: SIN DECLARAR su puerta")
            continue
        if puerta == auth.PUERTA_VER and metodos != ["GET"]:
            mal.append(f"{ruta.path} {metodos}: una ruta de ver solo puede ser GET")
            continue
        # El comportamiento con la cookie de solo ver, método por método.
        for metodo in metodos:
            esperado = 200 if puerta == auth.PUERTA_VER else 401
            r = cliente("ver").request(metodo, _url_de(ruta), json={} if metodo == "POST" else None,
                                       follow_redirects=False)
            if r.status_code != esperado:
                mal.append(f"{metodo} {ruta.path}: declara «{puerta}» y con la cookie de ver "
                           f"contestó {r.status_code} (esperado {esperado})")
    return mal


def test_G2_1_y_G2_10_cada_ruta_declara_su_puerta_y_se_porta_como_declara():
    with pp.pagina_sin_base():
        assert revisar_puertas(panel.app) == []


def test_G2_1_la_lista_de_rutas_no_es_vacia_y_trae_los_tres_tipos():
    """Para que la prueba de arriba no sea verde por no mirar nada."""
    puertas = {_puerta_de(r) for r in panel.app.routes if hasattr(r, "endpoint")}
    assert puertas == {auth.PUERTA_SIEMPRE, auth.PUERTA_VER, auth.PUERTA_ENTRADA, auth.PUERTA_CODE}
    rutas_ver = sorted(r.path for r in panel.app.routes if _puerta_de(r) == auth.PUERTA_VER)
    assert rutas_ver == ["/logo-cds.png", "/proyectos"]
    escribe = [r for r in panel.app.routes if "POST" in _metodos(r)]
    assert len(escribe) >= 30


def _con_ruta_inventada(path, fn, metodos, **opciones):
    """La ruta se agrega a la aplicación REAL y se quita al terminar."""
    panel.app.add_api_route(path, fn, methods=metodos, **opciones)
    nueva = panel.app.router.routes[-1]
    try:
        with pp.pagina_sin_base():
            return revisar_puertas(panel.app)
    finally:
        panel.app.router.routes.remove(nueva)


def test_G2_10_una_ruta_inventada_sin_declarar_cae_en_rojo():
    async def inventada():
        return JSONResponse({"hola": 1})
    mal = _con_ruta_inventada("/inventada-por-la-prueba", inventada, ["GET"])
    assert any("/inventada-por-la-prueba" in m and "SIN DECLARAR" in m for m in mal), mal


def test_G2_10_una_ruta_inventada_que_declara_siempre_pero_no_guarda_la_puerta_cae_en_rojo():
    @auth.puerta(auth.PUERTA_SIEMPRE)
    async def abierta():
        return JSONResponse({"hola": 1})              # no mira ninguna sesión
    mal = _con_ruta_inventada("/inventada-abierta", abierta, ["POST"])
    assert any("/inventada-abierta" in m and "200" in m for m in mal), mal


def test_G2_10_una_ruta_que_declara_ver_pero_solo_acepta_la_de_la_casa_cae_en_rojo():
    @auth.puerta(auth.PUERTA_VER)
    async def estrecha(request: Request):
        if not auth.puede_entrar(panel._sesion(request)):
            return panel._fuera(request)
        return JSONResponse({})
    mal = _con_ruta_inventada("/inventada-estrecha", estrecha, ["GET"])
    assert any("/inventada-estrecha" in m and "401" in m for m in mal), mal


def test_G2_10_una_ruta_de_ver_que_escribe_cae_en_rojo_aunque_la_deje_pasar():
    @auth.puerta(auth.PUERTA_VER)
    async def escribe(request: Request):
        return JSONResponse({})
    mal = _con_ruta_inventada("/inventada-escribe", escribe, ["POST"])
    assert any("/inventada-escribe" in m and "solo puede ser GET" in m for m in mal), mal


def test_G2_10_una_ruta_de_code_inventada_sin_permiso_cae_en_rojo():
    async def sin_permiso():
        return JSONResponse({})
    mal = _con_ruta_inventada(api_code.router.prefix + "/inventada", sin_permiso, ["GET"])
    assert any("/inventada" in m and "SIN DECLARAR" in m for m in mal), mal


def test_G2_10_una_puerta_desconocida_no_se_puede_declarar():
    with pytest.raises(ValueError):
        auth.puerta("casi-siempre")
    with pytest.raises(ValueError):
        auth.puerta(auth.PUERTA_CODE)       # la de Code la da `requiere()`, no un marcador


# ═══════════════════════════════════════════════════════════════════════
# G2-2 — la cookie de ver nunca es una sesión de la casa
# ═══════════════════════════════════════════════════════════════════════

def test_G2_2_la_sesion_de_la_casa_ignora_la_cookie_de_ver():
    """`_sesion` es lo que mira `puede_entrar` en todas las rutas de finanzas."""
    from starlette.requests import Request

    def pedido(cookie: str):
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                        "headers": [(b"cookie", cookie.encode())]})
    ver = auth.crear_token_ver()
    assert panel._sesion(pedido(f"{panel.COOKIE_VER}={ver}")) is None
    assert not auth.puede_entrar(panel._sesion(pedido(f"{panel.COOKIE_VER}={ver}")))
    # Y el mismo `_sesion` sí lee la de la casa (la prueba no es verde por leer nada).
    casa = auth.crear_token(DUENO, auth.VIDA_SESION)
    assert panel._sesion(pedido(f"{panel.COOKIE}={casa}")) == DUENO


def test_G2_2_con_la_cookie_de_ver_las_finanzas_cierran_con_401():
    """Las rutas de finanzas, sacadas de lo registrado (declaradas `siempre` y de lectura)."""
    rutas = [r for r in panel.app.routes if _puerta_de(r) == auth.PUERTA_SIEMPRE]
    assert {"/", "/movimientos", "/salud", "/sin-clasificar", "/papelera", "/tareas"} <= {
        r.path for r in rutas}
    with pp.pagina_sin_base():
        for r in rutas:
            for m in _metodos(r):
                resp = cliente("ver").request(m, _url_de(r), json={} if m == "POST" else None,
                                              follow_redirects=False)
                assert resp.status_code == 401, (m, r.path, resp.status_code)


def test_si_la_persona_tiene_las_dos_cookies_manda_la_de_la_casa():
    with pp.pagina_sin_base():
        c = cliente("casa")
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
        r = c.get("/proyectos", params={"p": 1})
    assert r.status_code == 200 and 'method="post"' in r.text


# ═══════════════════════════════════════════════════════════════════════
# La página de Proyectos con la cookie de ver
# ═══════════════════════════════════════════════════════════════════════

# Qué es un control que cambia algo, en lo que la página escribe en HTML:
_CAMPOS = {"input", "textarea", "select", "option", "button", "dialog", "script", "noscript",
           "iframe", "object", "embed", "form"}


class _Mirador(HTMLParser):
    """Recorre el HTML pintado y junta todo lo que, en una página de solo ver,
    sobra. Una sola puerta: lo que no está en `permitido` es un control."""

    def __init__(self, rutas_de_ver: set[str], enlace_a_la_app: str):
        super().__init__(convert_charrefs=True)
        self.rutas_de_ver = rutas_de_ver
        self.enlace_a_la_app = enlace_a_la_app
        self.sobran: list[str] = []
        self._form_permitido = False

    def _destino_ok(self, valor: str) -> bool:
        if valor.startswith("#") or valor == self.enlace_a_la_app:
            return True
        ruta = re.split(r"[?#]", valor, 1)[0]
        return ruta in self.rutas_de_ver

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            # La búsqueda de la lista es una LECTURA: GET a /proyectos con un solo campo, `q`.
            es_busqueda = ((a.get("method") or "get").lower() == "get"
                           and (a.get("action") or "") == "/proyectos")
            self._form_permitido = es_busqueda
            if not es_busqueda:
                self.sobran.append(f"<form method={a.get('method')!r} action={a.get('action')!r}>")
            return
        if tag in _CAMPOS:
            if tag in ("input", "button") and self._form_permitido:
                if tag == "input" and a.get("name") != "q":
                    self.sobran.append(f"<input name={a.get('name')!r}> en la búsqueda")
                return
            self.sobran.append(f"<{tag}>")
        for k in a:
            if k.startswith("on") or k in ("contenteditable", "formaction", "data-dbl",
                                           "data-auto"):
                self.sobran.append(f"{tag} con {k}")
        for atributo in ("href", "src", "action", "formaction"):
            if tag in ("link", "meta"):      # tipografías y metadatos: no navegan a nada de Lucy
                continue
            v = a.get(atributo)
            if v is not None and not self._destino_ok(v):
                self.sobran.append(f"<{tag} {atributo}={v!r}>")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form_permitido = False


def controles_que_sobran(html: str, enlace_a_la_app: str = "") -> list[str]:
    rutas_de_ver = {r.path for r in panel.app.routes if _puerta_de(r) == auth.PUERTA_VER}
    m = _Mirador(rutas_de_ver, enlace_a_la_app)
    m.feed(html)
    m.close()
    return m.sobran


# Consultas de lectura reales de la página y las armadas a mano para hacerla
# dibujar formularios, confirmaciones o avisos.
VISTAS = [
    {}, {"p": 1}, {"p": 2}, {"p": 3}, {"p": 4}, {"g": "CDS"}, {"sin_grupo": 1}, {"q": "disco"}, {"q": "zzz"},
    {"p": 1, "t": 10}, {"p": 1, "t": 11},
    {"g": "CDS", "t": 12}, {"sin_grupo": 1, "t": 13},      # el detalle de una tarea suelta («Meter en un proyecto»)
]
A_MANO = {
    "nuevo=CDS": {"nuevo": "CDS"},
    "confirmar=cerrar": {"p": 1, "confirmar": "cerrar"},
    "editar=nombre": {"p": 1, "editar": "nombre"},
    "editar_tarea": {"p": 1, "t": 10, "editar_tarea": 10},
    "confirmar_borrar": {"p": 1, "t": 10, "confirmar_borrar": 10},
    "editar_comentario": {"p": 1, "t": 10, "editar_comentario": 100},
    "derivar": {"p": 1, "t": 10, "derivar": 10, "derivadas": "5,6"},
    "busqueda de personas": {"p": 1, "t": 10, "pq": "ab", "pdonde": "cliente"},
    "todo junto": {"p": 1, "t": 10, "nuevo": "CDS", "confirmar": "cerrar", "editar": "nombre",
                   "editar_tarea": 10, "confirmar_borrar": 10, "editar_comentario": 100,
                   "derivar": 10, "pq": "a", "pdonde": "tarea-10"},
}


def _pintar(con, **consulta):
    with pp.pagina_sin_base():
        r = cliente(con).get("/proyectos", params=consulta)
    assert r.status_code == 200, (consulta, r.status_code)
    return r.text


@pytest.mark.parametrize("consulta", VISTAS + list(A_MANO.values()),
                         ids=[str(v) for v in VISTAS] + list(A_MANO))
def test_la_pagina_de_ver_no_ofrece_nada_que_cambie_algo(consulta, monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", "https://registro.example.test")
    html = _pintar("ver", **consulta)
    assert controles_que_sobran(html, "https://registro.example.test/") == []
    assert "<script" not in html and "fetch(" not in html


def test_la_pagina_de_ver_dice_lo_que_hay_pero_no_lo_que_se_puede_hacer(monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", "https://registro.example.test")
    html = _pintar("ver", p=1, t=10)
    for visible in ("Disco Uno", "Pendiente A", "Hecha B", "hola comentario", "Cliente X",
                    "Volver al inicio", 'href="https://registro.example.test/"'):
        assert visible in html, visible
    # Y no el menú de las finanzas ni los enlaces a lo que ver no abre.
    for href in ("/movimientos", "/tareas", "/salud", "/sin-clasificar", "/papelera"):
        assert f'href="{href}' not in html, href


def test_sin_direccion_de_la_App_no_se_dibuja_un_enlace_roto(monkeypatch):
    monkeypatch.setattr(config, "REGISTRO_URL", "")
    html = _pintar("ver", p=1)
    assert "Volver al inicio" not in html
    assert controles_que_sobran(html, "") == []


def test_el_detector_ve_los_controles_de_la_pagina_completa():
    """Para que el verde de arriba signifique algo: la MISMA página, con la sesión
    de la casa, trae muchos controles y el detector los encuentra."""
    html = _pintar("casa", p=1, t=10)
    sobran = controles_que_sobran(html)
    assert len(sobran) >= 15, sobran
    for esperado in ("<textarea>", "<select>", "<script>", "<form method='post'"):
        assert any(s.startswith(esperado) for s in sobran), (esperado, sobran[:8])


def test_el_detector_ve_entradas_inventadas():
    """Entradas que nadie escribió en la plantilla: cada una tiene que saltar."""
    inventadas = [
        '<form method="post" action="/proyectos/1/estado"><button>x</button></form>',
        '<form action="/proyectos/1/estado"><input name="q"></form>',      # GET a otra ruta
        '<form method="get" action="/proyectos"><input name="estado"></form>',  # campo que no es q
        '<a href="/tareas/nueva">crear</a>',
        '<a href="/proyectos/tarea/1/hecha">hecha</a>',                   # ruta que escribe
        '<img src="/otra-imagen.png">',
        '<div contenteditable>x</div>',
        '<p onclick="x()">x</p>',
        '<textarea></textarea>', '<select></select>', '<input name="q">',
        '<button>x</button>', '<dialog>x</dialog>', '<script>1</script>',
        '<noscript>x</noscript>', '<iframe src="/proyectos"></iframe>',
        '<p data-dbl="texto">x</p>',
    ]
    for trozo in inventadas:
        assert controles_que_sobran(trozo), trozo
    # Y lo permitido no salta: la búsqueda, el logo y un enlace a la lista.
    ok = ('<form method="get" action="/proyectos"><input type="search" name="q">'
          '<button>Buscar</button></form><img src="/logo-cds.png"><a href="/proyectos?p=1#x">a</a>')
    assert controles_que_sobran(ok) == []


def test_las_rutas_que_escriben_salen_de_lo_registrado_y_ningun_enlace_de_la_pagina_va_a_una():
    """Capa 2 con la lista de verdad: toda ruta con POST de la aplicación, con su
    forma de ruta, contra todo enlace y acción de la página de ver."""
    plantillas = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$")
                  for r in panel.app.routes if "POST" in _metodos(r)]
    assert len(plantillas) >= 30
    for consulta in VISTAS + list(A_MANO.values()):
        html = _pintar("ver", **consulta)
        destinos = re.findall(r'(?:href|action|formaction|src)="([^"]*)"', html)
        for d in destinos:
            ruta = re.split(r"[?#]", d, 1)[0]
            for p in plantillas:
                assert not p.match(ruta), (consulta, d)


# — La página no miente —

def _avisos_de_exito(html: str) -> list[str]:
    """Los textos de aviso («Proyecto creado.», «Tarea marcada como hecha.»…)
    que la página dibuja cuando la ruta de escribir vuelve con éxito."""
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(
        r'<p class="aviso(?: ok)?">(.*?)</p>', html, flags=re.S)]


AVISOS_A_MANO = {"hecho": "tarea_hecha", "creado": 5, "nombre_guardado": 1, "area_guardada": 1,
                 "tarea_creada": 3, "sala_no": 1, "derivadas": "5,6", "error": "area"}


def test_la_pagina_de_ver_no_dice_que_se_guardo_algo_que_no_se_guardo():
    """Las direcciones que, con la sesión de la casa, hacen decir «guardado»,
    «creado», «hecha»… con la de ver no dicen nada de eso. Los textos se sacan de
    la página completa, no de una lista escrita aquí."""
    de_la_casa = []
    for k, v in AVISOS_A_MANO.items():
        de_la_casa += _avisos_de_exito(_pintar("casa", p=1, **{k: v}))
    de_la_casa = sorted(set(de_la_casa))
    assert len(de_la_casa) >= 5, de_la_casa          # la prueba ve avisos de verdad
    for k, v in AVISOS_A_MANO.items():
        assert _avisos_de_exito(_pintar("ver", p=1, **{k: v})) == [], k
    todo = _pintar("ver", p=1, **AVISOS_A_MANO)
    for texto in de_la_casa:
        assert texto not in re.sub(r"<[^>]+>", "", todo), texto


def test_la_pagina_de_ver_no_promete_cambios_que_no_ofrece():
    """Los títulos y ayudas que explican CÓMO se cambia algo («doble clic para
    cambiar el nombre»…) están en la página completa y NO en la de ver: decirlo
    sin poder hacerlo es una frase que promete más de lo que la página hace."""
    assert "oble clic" in _pintar("casa", p=1, t=10)         # la prueba ve la frase de verdad
    assert "Al cerrarlo o reabrirlo" in _pintar("casa", p=4)
    for consulta in VISTAS + list(A_MANO.values()):
        html = _pintar("ver", **consulta)
        assert "oble clic" not in html, consulta
        for frase in ("Cambiar el nombre", "Cambiar el título", "Editar el comentario",
                      "Escribe un comentario", "Nueva tarea", "Cerrar proyecto", "Borrar",
                      "Al cerrarlo o reabrirlo"):
            assert frase not in html, (consulta, frase)


# ═══════════════════════════════════════════════════════════════════════
# El modelo de prueba hace correr TODOS los controles de la plantilla
# ═══════════════════════════════════════════════════════════════════════
#
# El hueco que esto cierra (lo encontró un testigo): el modelo de prueba no traía
# personas, así que las macros que dibujan la ✕ de quitar y «Agregar» NUNCA
# corrían, y «la página de ver no ofrece nada» salía verde sin haberlas visto. Una
# prueba que mira lo pintado solo vale por las ramas que el modelo hace pintar.
#
# CÓMO SE MIDE: de la plantilla REAL (`proyectos.html`, sin comentarios ni
# etiquetas de Jinja) se sacan todas las etiquetas que son un control o un
# destino (`form`, `a`, `button`, `input`, `textarea`, `select`, `dialog`), cada
# una con su atributo identificador (`action`, `href`, `name` o `class`, con los
# huecos `{{ … }}` como comodín). Con la sesión de la CASA se pinta la página en
# todas las vistas y consultas conocidas, y cada etiqueta de la plantilla tiene
# que aparecer en alguna página pintada. Una que no aparece = el modelo no la
# hace correr = rojo.
#
# LA FRONTERA, dicha: se compara por etiqueta + sus atributos identificadores
# (`action`, `href`, `name`, `class`), no por la línea de la plantilla. Dos sitios
# con la MISMA etiqueta y los MISMOS atributos (p. ej. el formulario de «proyecto
# nuevo» de la ventanita y el de la página aparte, ambos a `/proyectos/nuevo`) se
# dan por cubiertos si aparece uno. Un control cuyos atributos son todo comodín y
# no tiene clase se da por cubierto con cualquiera de su etiqueta. Lo que sí cae
# seguro: formulario, enlace o campo con un destino, un nombre o una clase que
# ninguna página pintada trae. Solo ve etiquetas de la lista `_ETIQUETAS_DE_CONTROL`.

_ETIQUETAS_DE_CONTROL = ("form", "a", "button", "input", "textarea", "select", "dialog")
_HUECO = "\x00"


def _fuente_sin_jinja() -> str:
    fuente = open("web/plantillas/proyectos.html", encoding="utf-8").read()
    fuente = re.sub(r"\{#.*?#\}", "", fuente, flags=re.S)
    fuente = re.sub(r"\{%.*?%\}", "", fuente, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", _HUECO, fuente, flags=re.S)


_ATRIBUTOS_QUE_IDENTIFICAN = ("action", "href", "name", "class")


def _sitios(fuente: str) -> list[tuple[str, dict]]:
    """(etiqueta, {atributo: valor con huecos}) de cada control de la plantilla,
    con los atributos que lo identifican."""
    sitios = []
    for m in re.finditer(r"<(%s)\b([^>]*)>" % "|".join(_ETIQUETAS_DE_CONTROL), fuente, flags=re.S):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(2)))
        attrs = {k: html_lib.unescape(v) for k, v in attrs.items()}     # `&amp;` → `&`, como lo lee el navegador
        sitios.append((m.group(1), {k: v for k, v in attrs.items()
                                    if k in _ATRIBUTOS_QUE_IDENTIFICAN}))
    return sitios


class _Pintado(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.elementos: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.elementos.append((tag, {k: (v or "") for k, v in attrs}))


def _regex_de(valor: str):
    return re.compile(".*".join(re.escape(x) for x in valor.split(_HUECO)), re.S)


def sitios_sin_cubrir(fuente: str, paginas: list[str]) -> list[str]:
    """Los controles de la plantilla que NINGUNA de las páginas pintadas trae: mismo
    tipo de etiqueta y TODOS sus atributos identificadores (los que no son puro
    hueco) iguales."""
    elementos: list[tuple[str, dict]] = []
    for html in paginas:
        p = _Pintado()
        p.feed(html)
        elementos += p.elementos
    sin = []
    for tag, attrs in _sitios(fuente):
        reglas = {k: _regex_de(v.strip()) for k, v in attrs.items()
                  if k != "class" and v.replace(_HUECO, "").strip()}
        # La clase se compara por PALABRAS (`quitar` no es `quitar-cliente`); las
        # palabras con hueco adentro (`{{ … }}`) no cuentan.
        clases = {w for w in attrs.get("class", "").split() if _HUECO not in w}

        def _es(t, a):
            return (t == tag and clases <= set(a.get("class", "").split())
                    and all(k in a and rx.search(a[k].strip()) for k, rx in reglas.items()))
        if not any(_es(t, a) for t, a in elementos):
            sin.append(f"<{tag} {attrs}>")
    return sorted(set(sin))


# Consultas para pintar TODO lo que la plantilla puede dibujar con la sesión de la casa.
COBERTURA = (VISTAS + list(A_MANO.values()) + [
    {"p": 1, "t": 11}, {"p": 1, "t": 10, "pq": "ab", "pdonde": "tarea-10"},
    {"p": 1, "pq": "ab", "pdonde": "proyecto"}, {"p": 1, "pq": "ab", "pdonde": "cliente"},
    {"p": 2, "confirmar": "cerrar"}, {"g": "CDS", "t": 12}, {"sin_grupo": 1, "t": 13},
])


def _paginas_de_la_casa(consultas=COBERTURA) -> list[str]:
    return [_pintar("casa", **c) for c in consultas]


def test_el_modelo_de_prueba_hace_correr_todos_los_controles_de_la_plantilla():
    fuente = _fuente_sin_jinja()
    assert len(_sitios(fuente)) >= 40, "el extractor no está viendo la plantilla"
    sin = sitios_sin_cubrir(fuente, _paginas_de_la_casa())
    assert sin == [], ("la plantilla dibuja controles que ninguna página de prueba pinta: "
                       "el modelo no los hace correr, y la prueba de solo ver no los ve: " + str(sin))


def test_el_extractor_ve_los_controles_y_el_hueco_del_testigo(monkeypatch):
    """Que el verde de arriba signifique algo: sin personas en el modelo (como estaba)
    la ✕ de quitar cae en rojo; y un control inventado en la plantilla
    cae también."""
    fuente = _fuente_sin_jinja()
    original = pp.modelo
    monkeypatch.setattr(pp, "modelo", lambda: original(con_personas=False))
    sin = sitios_sin_cubrir(fuente, _paginas_de_la_casa())
    monkeypatch.undo()
    assert any("quitar" in s for s in sin), sin
    assert len(sin) == 1, sin       # solo la ✕: «Agregar» corre aunque no haya personas
    paginas = _paginas_de_la_casa()
    for inventado in ('<form method="post" action="/proyectos/algo-nuevo"><button>x</button></form>',
                      '<a href="/proyectos/otra-cosa-nueva">x</a>',
                      '<input name="campo_nuevo">', '<textarea name="otro_campo_nuevo"></textarea>',
                      '<select name="elige_nuevo"></select>',
                      '<button class="boton-que-nadie-pinta">x</button>'):
        assert sitios_sin_cubrir(fuente + inventado, paginas), inventado
    assert sitios_sin_cubrir(fuente, paginas) == []
