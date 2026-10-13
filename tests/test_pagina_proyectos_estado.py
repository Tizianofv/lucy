"""La vista «Estado de los proyectos» de la página de Proyectos (`GET /proyectos?estado=1`): la lista
de TODOS los proyectos con su estado y su próxima tarea. Pieza 2 del diseño
`disenos/natalia-estado-de-proyecto/DISENO-modulo-proyectos.md` (13-oct-2026).

Qué vigila:
  E1  Se llega por un enlace de la izquierda («Estado de los proyectos» → `/proyectos?estado=1`), en las
      dos sesiones; sin sesión no se ve nada (401).
  E2  La vista no calcula nada: el estado y la próxima son los de `db.situacion_de_proyecto`.
  E3  Lo que dice de cada caso: con próxima (con fecha, vencida, sin fecha, grave), en pausa, cerrado,
      sin tareas, sin pendientes; el estado raro se trata como abierto.
  E4  Salen TODOS los proyectos vivos (la lista sale de la base de SQLite con el SQL real) y ninguno de la
      papelera, cada uno una vez y con su enlace.
  E5  El título de una tarea y el nombre de un proyecto se pintan como texto.
  E6  La vista solo lee: ningún formulario ni botón que escriba y ningún INSERT/UPDATE/DELETE al pintarla.
  E7  Hermanos: cada clase de vista que la ruta puede devolver (sacada del código) la pinta alguna
      consulta de la lista que recorren las guardas de solo ver.
  E8  La página de siempre no cambia: sin `?estado=` no sale la vista.

Frontera: es la ruta real con la plantilla real. No hay navegador: el ancho de 400 px no se mide aquí.

Correr:  python3 -m pytest tests/test_pagina_proyectos_estado.py -q
"""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

import _proyectos_de_prueba as pp
import config
import db.db as db
import test_pagina_proyectos as tp
from test_pagina_proyectos import _dia, gente, mundo, ver  # noqa: F401
import test_proyectos_solo_ver as sv
from test_escrituras_proyecto import _ARNES, hay_osascript
from test_pagina_proyectos_v19 import _dom_de_mentira
import web.avisos  # noqa: F401
import web.app as panel

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _el_dueno_tiene_nombre(monkeypatch):
    monkeypatch.setattr(config, "NOMBRES_POR_CHAT", {config.CHAT_ID_DUENO: "Dueño"})


def centro(html: str) -> str:
    """Lo que hay en `<main data-region="centro">`."""
    return html.split('<main data-region="centro">', 1)[1].split("</main>", 1)[0]


def renglones(html: str) -> dict[int, str]:
    """{id del proyecto: texto del renglón} de la vista, sin etiquetas."""
    salida = {}
    for m in re.finditer(r'<li>\s*<a class="nombre" href="/proyectos\?p=(\d+)">(.*?)</li>', centro(html), re.S):
        texto = re.sub(r"<[^>]+>", " ", m.group(2))
        salida[int(m.group(1))] = re.sub(r"\s+", " ", texto).strip()
    return salida


def pintar(con, **consulta):
    with pp.pagina_sin_base():
        return sv._pintar(con, **consulta)


# ═══════════════════════════════════════════════════════════════════════
# E1: cómo se llega, y quién
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("con", ["casa", "ver"])
def test_E1_el_enlace_de_la_izquierda_lleva_a_la_vista_en_las_dos_sesiones(con):
    html = pintar(con)
    assert '<a class="ver-estado" href="/proyectos?estado=1" aria-current="false">Estado de los proyectos</a>' in html
    assert html.index('class="ver-estado"') < html.index('<nav class="grupos"')      # arriba de la lista
    con_vista = pintar(con, estado=1)
    assert 'aria-current="true">Estado de los proyectos</a>' in con_vista
    assert "<h1>Estado de los proyectos</h1>" in con_vista


def test_E1_sin_sesion_no_se_ve_nada():
    with pp.pagina_sin_base():
        r = sv.cliente(None).get("/proyectos", params={"estado": 1})
    assert r.status_code == 401
    assert "Disco Uno" not in r.text and "Estado de los proyectos" not in r.text


def test_E1_la_casa_y_el_solo_ver_ven_el_mismo_contenido_de_la_vista():
    assert centro(pintar("casa", estado=1)) == centro(pintar("ver", estado=1))


# ═══════════════════════════════════════════════════════════════════════
# E2: no calcula nada; sale de la función única
# ═══════════════════════════════════════════════════════════════════════

def test_E2_la_vista_dice_lo_que_dice_la_funcion(mundo, monkeypatch):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, "una", proyecto=1)
    real = db.situacion_de_proyecto

    def cambiada(estado_guardado, filas):
        s = real(estado_guardado, filas)
        s["estado"] = "PALABRA-DE-LA-FUNCION"
        if s["proxima"]:
            s["proxima"] = {**s["proxima"], "titulo": "TITULO-DE-LA-FUNCION"}
        return s

    monkeypatch.setattr(db, "situacion_de_proyecto", cambiada)
    fila = renglones(ver(mundo, estado=1))[1]
    assert "PALABRA-DE-LA-FUNCION" in fila and "Próxima: TITULO-DE-LA-FUNCION" in fila
    assert "una" not in fila.replace("TITULO-DE-LA-FUNCION", "")


_ABRE = ("if", "for", "macro", "call", "block", "filter", "with", "set_bloque")


def bloque_de_la_vista(plantilla: str) -> str:
    """El texto de la rama `{% elif vista.tipo == 'estado' %}` ENTERA: desde su etiqueta hasta la
    etiqueta `else`/`elif`/`endif` que cierra ESA rama, contando los `if`/`for` que se abren y se
    cierran dentro. No depende de dónde caiga un `{% else %}` interior."""
    inicio = plantilla.index("{% elif vista.tipo == 'estado' %}")
    resto = plantilla[inicio + len("{% elif vista.tipo == 'estado' %}"):]
    profundidad = 0
    for m in re.finditer(r"{%-?\s*(\w+)(.*?)-?%}", resto, re.S):
        palabra = m.group(1)
        if palabra in ("if", "for", "macro", "call", "block", "filter", "with"):
            profundidad += 1
        elif palabra.startswith("end"):
            if profundidad == 0:
                return resto[:m.start()]
            profundidad -= 1
        elif palabra in ("else", "elif") and profundidad == 0:
            return resto[:m.start()]
    raise AssertionError("la rama de la vista no se cierra")


def test_E2_el_delimitador_del_bloque_no_se_queda_corto_con_entradas_inventadas():
    inventada = ("{% if vista.tipo == 'a' %}A{% elif vista.tipo == 'estado' %}"
                 "PRIMERO {% if x %}uno{% else %}dos{% endif %} MEDIO "
                 "{% for i in y %}{% if i %}a{% else %}b{% endif %}{% else %}vacio{% endfor %} FIN"
                 "{% elif vista.tipo == 'otra' %}OTRA{% else %}ELSE-DE-AFUERA{% endif %}")
    b = bloque_de_la_vista(inventada)
    assert b.strip().startswith("PRIMERO") and b.strip().endswith("FIN")
    assert "OTRA" not in b and "ELSE-DE-AFUERA" not in b
    # sin ramas siguientes: cierra con el `endif` de afuera
    assert bloque_de_la_vista("{% if q %}{% elif vista.tipo == 'estado' %}X{% if z %}y{% else %}w{% endif %}Z{% endif %}").endswith("Z")


def test_E2_el_bloque_real_se_toma_entero():
    html = (RAIZ / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    b = bloque_de_la_vista(html)
    for extremo in ("Estado de los proyectos</h1>", "Nada pendiente", "Todavía no hay proyectos."):
        assert extremo in b, extremo            # desde el título hasta el último mensaje: no se corta en el primer else
    assert "Proyecto nuevo" not in b and "Tareas sin proyecto" not in b      # y no se pasa a la rama que sigue


def test_E2_la_plantilla_de_la_vista_no_decide_el_estado_ni_cuenta():
    bloque = bloque_de_la_vista((RAIZ / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8"))
    assert "m.estado_calculado" in bloque and "m.proxima" in bloque
    for prohibido in ("n_hechas", "n_total", "n_vencidas", "'En curso'", "'Sin empezar'", "'Pausado'", "'Cerrado'",
                      "pendientes[", "pendientes|", "|sort", "|first", "|length", "|count", "|sum", "estado ==", "estado !="):
        assert prohibido not in bloque, prohibido


# ═══════════════════════════════════════════════════════════════════════
# E3: lo que dice de cada caso (SQL real sobre SQLite)
# ═══════════════════════════════════════════════════════════════════════

def _sembrar_casos(m):
    m.proyecto(1, "Con fecha", area="CDS", cliente="Cliente A")
    m.tarea(10, "Mezclar el tema", proyecto=1, vence=_dia(5))
    m.proyecto(2, "Vencida", area="CDS")
    m.tarea(20, "Mandar la letra", proyecto=2, vence=_dia(-2))
    m.proyecto(3, "Sin fecha", area="CDS")
    m.tarea(30, "Pensar el título", proyecto=3)
    m.proyecto(4, "Con alerta", area="ACD")
    m.tarea(40, "Revisar la alerta", proyecto=4)
    m.con.execute("UPDATE tareas SET grave = 1, responsable_chat_id = ? WHERE id = 40", (config.CHAT_ID_CODE,))   # grave = de Code (`db.graves_de`)
    m.proyecto(5, "En pausa", area="ACD", estado="pausado")
    m.tarea(50, "Esperar al cliente", proyecto=5, vence=_dia(1))
    m.proyecto(6, "Cerrado con pendiente", area="IA", estado="cerrado")
    m.tarea(60, "Quedó abierta", proyecto=6)
    m.proyecto(7, "Sin tareas", area="IA")
    m.proyecto(8, "Todo hecho", area="IA")
    m.tarea(80, "Hecha", proyecto=8, estado="hecha", completado=_dia(-1))
    m.proyecto(9, "Sin grupo", area=None)
    m.tarea(90, "De sin grupo", proyecto=9)
    m.proyecto(10, "Estado raro", area="IA", estado="en revision")
    m.proyecto(11, "En la papelera", area="CDS", borrado=True)
    m.tarea(110, "De la papelera", proyecto=11)


def test_E3_cada_caso_dice_lo_que_dice(mundo):
    _sembrar_casos(mundo)
    f = renglones(ver(mundo, estado=1))
    assert f[1] == "Con fecha Cliente A Sin empezar Próxima: Mezclar el tema · vence el 15 oct 2026 1 pendiente"
    assert f[2] == "Vencida Sin empezar Próxima: Mandar la letra · venció el 8 oct 2026 1 pendiente"
    assert f[3] == "Sin fecha Sin empezar Próxima: Pensar el título · sin fecha 1 pendiente"
    assert f[4] == "Con alerta Sin empezar Próxima: Revisar la alerta · sin fecha Grave 1 pendiente"
    assert f[5] == "En pausa Pausado Próxima: Esperar al cliente · vence el 11 oct 2026 1 pendiente"
    assert f[6] == "Cerrado con pendiente Cerrado Cerrado: sin próxima tarea 1 pendiente"
    assert f[7] == "Sin tareas Sin empezar Nada pendiente 0 pendientes"
    assert f[8] == "Todo hecho En curso Nada pendiente 0 pendientes"
    assert f[9] == "Sin grupo Sin empezar Próxima: De sin grupo · sin fecha 1 pendiente"
    assert f[10] == "Estado raro Sin empezar Nada pendiente 0 pendientes"


def test_E3_los_grupos_salen_con_su_nombre_y_sin_grupo_al_final(mundo):
    _sembrar_casos(mundo)
    h = centro(ver(mundo, estado=1))
    titulos = re.findall(r"<h2>(.*?)</h2>", h)
    assert titulos == ["CDS", "ACD", "IA", "Sin grupo"]


# ═══════════════════════════════════════════════════════════════════════
# E4: todos los vivos, una vez, con su enlace
# ═══════════════════════════════════════════════════════════════════════

def test_E4_salen_todos_los_proyectos_vivos_y_ninguno_de_la_papelera(mundo):
    _sembrar_casos(mundo)
    vivos = {fila[0] for fila in mundo.con.execute("SELECT id FROM proyectos WHERE borrado_en IS NULL")}
    enpapelera = {fila[0] for fila in mundo.con.execute("SELECT id FROM proyectos WHERE borrado_en IS NOT NULL")}
    html = ver(mundo, estado=1)
    ids = [int(x) for x in re.findall(r'<a class="nombre" href="/proyectos\?p=(\d+)">', centro(html))]
    assert sorted(ids) == sorted(vivos) and len(ids) == len(set(ids)) and enpapelera
    assert not set(ids) & enpapelera
    assert "En la papelera" not in centro(html)


def test_E4_la_busqueda_acorta_la_vista_igual_que_la_lista():
    f = renglones(pintar("casa", estado=1, q="disco"))
    assert list(f) == [1]
    assert "Solo los de «disco»" in centro(pintar("casa", estado=1, q="disco"))
    vacio = centro(pintar("casa", estado=1, q="zzz"))
    assert "Ningún proyecto ni cliente con «zzz»." in vacio and renglones(pintar("casa", estado=1, q="zzz")) == {}


def _lo_que_envia_el_buscador(html: str) -> dict:
    """Los campos que el navegador mandaría al enviar el formulario `buscador` de la izquierda, sacados del HTML
    servido (nombre y valor de cada `<input>`); no una lista tecleada."""
    forma = re.search(r'<form class="buscador"[^>]*>(.*?)</form>', html, re.S).group(1)
    campos = {}
    for tag in re.findall(r"<input[^>]*>", forma):
        nombre = re.search(r'name="([^"]*)"', tag)
        if nombre:
            campos[nombre.group(1)] = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
    return campos


def test_E4_buscar_estando_en_la_vista_te_deja_en_la_vista_con_la_lista_acortada():
    for con in ("casa", "ver"):
        campos = _lo_que_envia_el_buscador(pintar(con, estado=1))
        assert campos == {"q": "", "estado": "1"}, campos
        h = pintar(con, **{**campos, "q": "disco"})
        assert list(renglones(h)) == [1] and "Solo los de «disco»" in centro(h)
        assert 'href="/proyectos?estado=1">Quitar el filtro' in h


def test_E4_buscar_desde_la_pagina_normal_sigue_llevando_a_la_pagina_normal():
    for con in ("casa", "ver"):
        campos = _lo_que_envia_el_buscador(pintar(con))
        assert campos == {"q": ""}, campos
        h = pintar(con, **{**campos, "q": "disco"})
        assert 'class="estados"' not in h and '<h1>Estado de los proyectos</h1>' not in h
        assert 'href="/proyectos">Quitar el filtro' in h


@hay_osascript
def test_E4_con_javascript_enter_en_la_caja_envia_el_formulario_en_la_vista_y_sigue_sin_recargar_fuera(mundo):
    def enter(html, con_estado):
        guion = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]
        form = ("{querySelector: function (s) { return s === 'input[name=\"estado\"]' ? {} : null; }}"
                if con_estado else "undefined")
        escenario = (_dom_de_mentira(html) + f"caja.form = {form}; oyentes.keydown(ev(caja, {{key: 'Enter'}}));"
                     " JSON.stringify({evitado: evitado})")
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ARNES + guion + "\n" + escenario],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout.strip())["evitado"]
    mundo.proyecto(1, "Uno", area="CDS")
    en_la_vista, fuera = ver(mundo, estado=1), ver(mundo)
    assert enter(en_la_vista, True) == 0       # Enter envía el formulario: el servidor acorta la vista
    assert enter(fuera, False) == 1            # en la página normal Enter no recarga, como siempre
    assert enter(fuera, True) == 0             # (la señal es el campo escondido, no la página)


def test_E4_sin_proyectos_lo_dice(mundo):
    h = centro(ver(mundo, estado=1))
    assert "Todavía no hay proyectos." in h and renglones(ver(mundo, estado=1)) == {}


# ═══════════════════════════════════════════════════════════════════════
# E5: texto, no HTML
# ═══════════════════════════════════════════════════════════════════════

def test_E5_el_titulo_y_el_nombre_se_pintan_como_texto(mundo):
    mundo.proyecto(1, "<u>Proy</u> & co", area="CDS", cliente="<i>Cli</i>")
    mundo.tarea(10, "<script>alert(1)</script> \"x\" & y", proyecto=1)
    h = centro(ver(mundo, estado=1))
    assert "<script" not in h and "<u>Proy" not in h and "<i>Cli" not in h
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h and "&lt;u&gt;Proy&lt;/u&gt; &amp; co" in h


def test_E5_el_eco_de_la_busqueda_se_escapa_en_los_dos_sitios(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    q = '<script>alert(1)</script>"&'
    html = ver(mundo, estado=1, q=q)
    assert "<script>alert(1)</script>" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 2        # «Solo los de…» / «Ningún proyecto…»
    assert "Solo los de «&lt;script&gt;" in centro(html)


def test_E5_el_grupo_escrito_por_una_persona_se_escapa(mundo):
    mundo.con.execute("INSERT INTO areas (clave, color, orden) VALUES ('<i>Grupo</i>', '#123456', 9)")
    mundo.proyecto(1, "Uno", area="<i>Grupo</i>")
    h = centro(ver(mundo, estado=1))
    assert "<i>Grupo" not in h and "&lt;i&gt;Grupo&lt;/i&gt;" in h


# Lo que la vista pinta que NO es un texto escrito por una persona (lo declaro uno por uno; lo que
# no esté aquí y no repita un texto de persona hace fallar la prueba de abajo: cae del lado estricto).
EXPRESIONES_QUE_NO_SON_TEXTO_DE_PERSONA = {
    "loop.index", "m.id", "m.estado_calculado", "m.n_pendientes", "'s' if m.n_pendientes != 1",
    "m.proxima.vence|dia_largo", "'vencida' if m.proxima.vencida",
    "'venció el' if m.proxima.vencida else 'vence el'",
}
# Los orígenes de texto de persona que la vista repite: nombre de proyecto, cliente, título de la próxima,
# nombre del grupo, y la búsqueda.
ORIGENES_DE_TEXTO = (r"m\.nombre", r"m\.cliente", r"m\.proxima\.titulo", r"x\.clave", r"\bq\b")


def test_E5_cada_expresion_de_la_vista_o_es_conocida_o_repite_un_texto_de_persona_y_se_escapa():
    bloque = bloque_de_la_vista((RAIZ / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8"))
    expresiones = [e.strip() for e in re.findall(r"{{(.*?)}}", bloque, re.S)]
    assert expresiones
    sin_clasificar = []
    for e in expresiones:
        de_persona = any(re.search(o, e) for o in ORIGENES_DE_TEXTO)
        assert not re.search(r"\|\s*(safe|tojson|urlencode)\b|Markup|autoescape", e), f"{e}: sale sin escapar"
        if not de_persona and e.replace("-", "").strip() not in EXPRESIONES_QUE_NO_SON_TEXTO_DE_PERSONA \
                and e.strip("- ") not in EXPRESIONES_QUE_NO_SON_TEXTO_DE_PERSONA:
            sin_clasificar.append(e)
    assert sin_clasificar == [], f"expresiones de la vista que no sé si son texto de una persona: {sin_clasificar}"
    # y cada origen de texto que aparece en la vista tiene su prueba de escape de arriba
    vistos = {o for o in ORIGENES_DE_TEXTO if any(re.search(o, e) for e in expresiones)}
    assert vistos == set(ORIGENES_DE_TEXTO), set(ORIGENES_DE_TEXTO) - vistos
    assert "autoescape" not in (RAIZ / "web" / "app.py").read_text(encoding="utf-8").split("plantillas = Jinja2Templates")[1][:300]


# ═══════════════════════════════════════════════════════════════════════
# E6: solo lee
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("con", ["casa", "ver"])
def test_E6_la_vista_no_ofrece_nada_que_escriba(con):
    h = pintar(con, estado=1)
    assert sv.controles_que_sobran(h, "") == [] or con == "casa"
    vista = centro(h)
    assert "<form" not in vista and "<button" not in vista and 'method="post"' not in vista
    assert "<script" not in h and "fetch(" not in h if con == "ver" else True


def test_E6_pintarla_no_escribe_en_la_base(mundo):
    _sembrar_casos(mundo)
    vistos = []
    mundo.con.set_trace_callback(vistos.append)
    ver(mundo, estado=1)
    escribe = [x for x in vistos if re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE)", x, re.I)]
    assert vistos and escribe == []


# ═══════════════════════════════════════════════════════════════════════
# E7: los hermanos — la lista de clases de vista sale del código
# ═══════════════════════════════════════════════════════════════════════

def _tipos_de_vista_del_codigo() -> set:
    """Todo `"tipo": "<x>"` que construye `_vista_de_proyectos`, sacado del código con `ast`."""
    arbol = ast.parse((RAIZ / "web" / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "_vista_de_proyectos")
    tipos = set()
    for d in ast.walk(fn):
        if isinstance(d, ast.Dict):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and k.value == "tipo" and isinstance(v, ast.Constant):
                    tipos.add(v.value)
                elif isinstance(k, ast.Constant) and k.value == "tipo":
                    tipos.add("<computado>")
    return tipos


def test_E7_cada_clase_de_vista_la_pinta_alguna_consulta_de_las_guardas_de_solo_ver(monkeypatch):
    esperados = _tipos_de_vista_del_codigo()
    assert {"estado", "proyecto", "nuevo", "grupo", "sin_grupo"} <= esperados, esperados
    vistos = set()
    original = panel._vista_de_proyectos

    def espia(*a, **k):
        v = original(*a, **k)
        vistos.add(v["tipo"])
        return v

    monkeypatch.setattr(panel, "_vista_de_proyectos", espia)
    for consulta in sv.VISTAS + list(sv.A_MANO.values()):
        for con in ("casa", "ver"):         # «nuevo» solo existe en la casa: en solo ver se apaga a propósito
            pintar(con, **consulta)
    faltan = {t for t in esperados if t not in ("<computado>",)} - vistos
    assert faltan == set(), f"clases de vista que ninguna consulta de las guardas pinta: {faltan}"


def test_E7_la_vista_esta_en_las_listas_de_las_guardas():
    assert {"estado": 1} in sv.VISTAS and {"estado": 1, "q": "disco"} in sv.VISTAS


# ═══════════════════════════════════════════════════════════════════════
# E8: la página de siempre no cambia
# ═══════════════════════════════════════════════════════════════════════

def test_E8_sin_el_parametro_no_sale_la_vista():
    for consulta in ({}, {"p": 1}, {"g": "CDS"}, {"sin_grupo": 1}, {"q": "disco"}, {"nuevo": "CDS"}):
        h = pintar("casa", **consulta)
        assert 'class="estados"' not in h and "<h1>Estado de los proyectos</h1>" not in h, consulta


class _Espia:
    """Reemplaza `web.app.db`: anota cada cosa que la ruta le pide y la deja pasar a la de verdad."""

    def __init__(self, real):
        self._real, self.pedidos = real, []

    def __getattr__(self, nombre):
        if not nombre.isupper():
            self.pedidos.append(nombre)
        return getattr(self._real, nombre)


PARAMETROS_QUE_ABREN_OTRAS_COSAS = {
    "p": 1, "g": "CDS", "sin_grupo": 1, "nuevo": "CDS", "confirmar": "cerrar", "editar": "nombre", "t": 10,
    "editar_tarea": 10, "confirmar_borrar": 10, "editar_comentario": 100, "derivar": 10, "pq": "ab",
    "pdonde": "cliente", "quitar_grupo": "CDS", "borrar_proyecto": 1, "hecho": "proyecto_borrado", "borrado": 1,
    "borrada": 10, "grupo": "CDS", "editar_nota": 700, "borrar_nota": 701, "sq": "ana", "filtro": "mias",
}


def test_E8_con_la_vista_no_se_lee_nada_que_la_vista_no_use(mundo, monkeypatch):
    """La ruta, con `estado=1` y los parámetros que en otra vista harían leer la base o Noco, pide a `db`
    EXACTAMENTE lo que la lista necesita y no hace ninguna búsqueda en Noco. (La lista de lo que pide sale de
    lo que la ruta de verdad toca, no de una lista tecleada.)"""
    _sembrar_casos(mundo)
    espia = _Espia(db)
    monkeypatch.setattr(panel, "db", espia)
    noco = []

    async def _noco(texto):
        noco.append(texto)
        return []
    monkeypatch.setattr(panel.noco_lectura, "buscar_personas", _noco)
    h = ver(mundo, estado=1, **PARAMETROS_QUE_ABREN_OTRAS_COSAS)
    assert 'class="estados"' in centro(h)
    assert set(espia.pedidos) == {"pagina_de_proyectos", "areas"}, sorted(set(espia.pedidos))
    assert noco == []


def test_E8_el_espia_sirve_los_mismos_parametros_sin_la_vista_si_hacen_leer(mundo, monkeypatch):
    """Control: sin `estado`, esos parámetros SÍ hacen leer (si el espía no viera nada, la prueba de arriba no probaría)."""
    _sembrar_casos(mundo)
    espia = _Espia(db)
    monkeypatch.setattr(panel, "db", espia)
    noco = []

    async def _noco(texto):
        noco.append(texto)
        return []
    monkeypatch.setattr(panel.noco_lectura, "buscar_personas", _noco)
    ver(mundo, p=1, pq="ab", pdonde="cliente", quitar_grupo="CDS", borrar_proyecto=1)
    assert {"contenido_para_borrar_grupo", "contenido_de_proyecto"} <= set(espia.pedidos) and noco == ["ab"]


def test_E8_el_parametro_gana_a_los_demas_y_no_los_dibuja():
    h = pintar("casa", estado=1, p=1, nuevo="CDS", borrar_proyecto=1)
    assert 'class="estados"' in centro(h) and "Proyecto nuevo" not in centro(h) and "Borrar" not in centro(h)
