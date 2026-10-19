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


TEXTO_DE_PERSONA, DATO_DEL_MODELO, CONTROL = "texto de persona", "dato del modelo que no es texto", "control de presentación"
# LA TABLA CERRADA de lo que puede haber entre llaves en la rama de la vista: texto normalizado EXACTO → clase.
# Para añadir algo a la vista hay que declararlo aquí (y esta tabla se revisa en el testigo): no se deduce de palabras.
TABLA_DE_LA_VISTA = {
    "{{ q }}": TEXTO_DE_PERSONA,
    "{{ x.clave if x.clave else 'Sin grupo' }}": TEXTO_DE_PERSONA,
    "{{ m.nombre }}": TEXTO_DE_PERSONA,
    "{{ m.cliente }}": TEXTO_DE_PERSONA,
    "{{ m.proxima.titulo }}": TEXTO_DE_PERSONA,
    "{{ loop.index }}": DATO_DEL_MODELO,
    "{{ m.id }}": DATO_DEL_MODELO,
    "{{ m.estado_calculado }}": DATO_DEL_MODELO,
    "{{ m.n_pendientes }}": DATO_DEL_MODELO,
    "{{ m.proxima.vence|dia_largo }}": DATO_DEL_MODELO,
    "{{ 'vencida' if m.proxima.vencida }}": CONTROL,
    "{{ 'venció el' if m.proxima.vencida else 'vence el' }}": CONTROL,
    "{{ 's' if m.n_pendientes != 1 }}": CONTROL,
    "{% set grupos_con = [] %}": CONTROL,
    "{% for x in visibles.grupos + [visibles.sin_grupo] %}": CONTROL,
    "{% set _ = grupos_con.append(x) %}": CONTROL,
    "{% for x in grupos_con %}": CONTROL,
    "{% for m in x.abiertos + x.cerrados %}": CONTROL,
    "{% if x.abiertos or x.cerrados %}": CONTROL,
    "{% if q %}": CONTROL,
    "{% if m.cliente %}": CONTROL,
    # La marca de Natalia (19-oct-2026): pinta un `<span>` fijo (icono y texto de `web/marca_natalia.py`), sin texto de
    # nadie; `tests/test_marca_natalia.py` vigila dónde sale y con qué campo. Las cuatro llamadas del renglón.
    "{{ marca_natalia('nombre', m) }}": CONTROL,
    "{{ marca_natalia('estado', m) }}": CONTROL,
    "{{ marca_natalia('siguiente.titulo', m) }}": CONTROL,
    "{{ marca_natalia('siguiente.fecha', m) }}": CONTROL,
    "{% if m.proxima %}": CONTROL,
    "{% if m.proxima.vence %}": CONTROL,
    "{% if m.proxima.grave %}": CONTROL,
    "{% elif m.cerrado %}": CONTROL,
    "{% if not grupos_con %}": CONTROL,
    "{% else %}": CONTROL,
    "{% endif %}": CONTROL,
    "{% endfor %}": CONTROL,
}


def llaves_de(bloque: str) -> list:
    """Cada `{{ … }}` y cada `{% … %}` del bloque (sin los comentarios `{# … #}`, que no se pintan), con el texto
    normalizado: sin los guiones de espacio (`{%-`, `-%}`) y con los blancos colapsados."""
    sin_comentarios = re.sub(r"{#.*?#}", "", bloque, flags=re.S)
    salida = []
    for m in re.finditer(r"{{.*?}}|{%.*?%}", sin_comentarios, re.S):
        t = m.group(0)
        t = re.sub(r"^({[{%])-", r"\1", t)
        t = re.sub(r"-([}%]})$", r"\1", t)
        salida.append(re.sub(r"\s+", " ", t).strip())
    return salida


def no_declaradas(bloque: str) -> list:
    """Lo que hay entre llaves y NO es, por igualdad exacta, una entrada de `TABLA_DE_LA_VISTA`."""
    return [t for t in llaves_de(bloque) if t not in TABLA_DE_LA_VISTA]


def test_E2_la_tabla_cerrada_de_la_vista_cae_del_lado_estricto_con_una_entrada_inventada_de_cada_clase():
    """QUÉ EXIGE: cada `{{ … }}` y cada `{% … %}` de la rama de la vista es, por igualdad exacta de texto normalizado, una entrada de la tabla cerrada `TABLA_DE_LA_VISTA` (con su clase: texto de persona, dato del modelo que no es texto, control de presentación) y, con `estado`, las funciones (atributos llamables) que la ruta pide directamente a un módulo envuelto son solo las que `LO_QUE_PIDE_LA_RUTA_CON_ESTADO` declara; lo no declarado cae en rojo. Los módulos envueltos son los globales de `web.app` que son módulos del proyecto o de `LIBRERIAS_DE_RED`; los demás módulos de `web.app` (p. ej. `asyncio`, `re`, `logging`) no se envuelven.
    QUÉ NO PROMETE: no vigila que la tabla declarada sea «correcta» (quien añada una entrada a la tabla puede añadir una decisión: es una lista que obliga a declarar, no a acertar) ni lo que la ruta pida a través de un OBJETO de un módulo (p. ej. `db.pool.<método>()`: el espía solo anota atributos llamables y `db.pool` no lo es), ni lo que pida a módulos que no son del proyecto ni de `LIBRERIAS_DE_RED` (p. ej. `asyncio`), ni las lecturas por caminos que el espía no envuelve (funciones traídas con `from … import`, lo que haga un hilo o una tarea aparte). Tampoco vigila que la prueba de escape declarada para cada texto de persona de la tabla escape de verdad ese texto: la tabla solo obliga a declarar una prueba y a que exista; que escape lo vigila cada prueba de escape por su cuenta."""
    # todo lo declarado, junto, pasa; es lo que hay que poder escribir
    assert no_declaradas(" ".join(TABLA_DE_LA_VISTA)) == []
    # una entrada inventada de CADA clase cae, con la forma de las que el testigo usó
    inventadas = {
        TEXTO_DE_PERSONA: ["{{ m.descripcion }}", "{{ '' if q else '' }}", "{{ m.nombre|safe }}", "{{ q|upper }}"],
        DATO_DEL_MODELO: ["{{ m.n_hechas }}", "{{ m.n_otras }}", "{{ m.estado_calculado|upper }}"],
        CONTROL: ["{% if m.n_pendientes > 1000 %}", "{% if m.n_hechas > 1000 %}", "{% for t in m.pendientes %}", "{% set z = 1 %}"],
    }
    for clase, textos in inventadas.items():
        for t in textos:
            assert no_declaradas(f"antes {t} después") == [t], (clase, t)
    # los guiones de espacio y los blancos no cambian la igualdad exacta, pero las palabras sí
    assert no_declaradas("{%- if   q -%}  {{- q -}}  {%- endif -%}") == []
    assert no_declaradas("{% if q and q %}") == ["{% if q and q %}"]


def test_E2_la_plantilla_real_de_la_vista_solo_tiene_lo_declarado_y_la_tabla_no_tiene_sobrantes():
    bloque = bloque_de_la_vista((RAIZ / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8"))
    assert no_declaradas(bloque) == []
    assert set(TABLA_DE_LA_VISTA) <= set(llaves_de(bloque)), set(TABLA_DE_LA_VISTA) - set(llaves_de(bloque))
    assert "{{ m.estado_calculado }}" in llaves_de(bloque) and any("m.proxima" in t for t in llaves_de(bloque))


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
    assert f[6] == "Cerrado con pendiente Cerrado Archivado: sin próxima tarea 1 pendiente"
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


# Los textos de persona de la tabla (`TABLA_DE_LA_VISTA`) y la prueba de este archivo que cada uno DECLARA como su prueba de escape.
# Solo se comprueba que esté declarada y exista; que de verdad escape ese texto lo vigila cada una por su cuenta.
LO_QUE_CUBREN_LAS_PRUEBAS_DE_ESCAPE = {
    "{{ q }}": "test_E5_el_eco_de_la_busqueda_se_escapa_en_los_dos_sitios",
    "{{ x.clave if x.clave else 'Sin grupo' }}": "test_E5_el_grupo_escrito_por_una_persona_se_escapa",
    "{{ m.nombre }}": "test_E5_el_titulo_y_el_nombre_se_pintan_como_texto",
    "{{ m.cliente }}": "test_E5_el_titulo_y_el_nombre_se_pintan_como_texto",
    "{{ m.proxima.titulo }}": "test_E5_el_titulo_y_el_nombre_se_pintan_como_texto",
}


def test_E5_cada_texto_de_persona_de_la_tabla_tiene_declarada_una_prueba_de_escape_que_existe():
    de_persona = {t for t, c in TABLA_DE_LA_VISTA.items() if c == TEXTO_DE_PERSONA}
    assert de_persona == set(LO_QUE_CUBREN_LAS_PRUEBAS_DE_ESCAPE)
    for prueba in set(LO_QUE_CUBREN_LAS_PRUEBAS_DE_ESCAPE.values()):
        assert prueba in globals() and callable(globals()[prueba]), prueba


# ═══════════════════════════════════════════════════════════════════════
# E6: solo lee
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("con", ["casa", "ver"])
def test_E6_la_vista_no_ofrece_nada_que_escriba(con):
    h = pintar(con, estado=1)
    assert sv.controles_que_sobran(h, "") == [] or con == "casa"
    vista = centro(h)
    assert "<form" not in vista and "<button" not in vista and 'method="post"' not in vista
    if con == "ver":     # el único guion de solo ver es el de la pieza (lo exige `controles_que_sobran`)
        assert h.count("<script") == 1 and h.count("fetch(") == 1


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


class _Modulo:
    """Un módulo envuelto: anota cada función (o clase) que `web.app` le pide y la deja pasar a la de verdad."""

    def __init__(self, nombre, real, anotar):
        self._nombre, self._real, self._anotar = nombre, real, anotar

    def __getattr__(self, atributo):
        valor = getattr(self._real, atributo)
        if not atributo.startswith("__") and callable(valor):
            self._anotar(f"{self._nombre}.{atributo}")
        return valor


# Librerías de red que `web.app` puede traer y que NO son del proyecto (frontera declarada: una librería
# de red que no esté en esta lista y no sea del proyecto no la envuelve el espía).
LIBRERIAS_DE_RED = ("httpx", "requests", "urllib", "aiohttp", "socket", "http")


def modulos_por_los_que_la_ruta_puede_leer_hacia_fuera() -> dict:
    """{nombre en `web.app`: módulo}. SALEN DEL MÓDULO REAL: cada global de `web.app` que es un módulo y o
    es del proyecto (su archivo cuelga de la raíz del repositorio: `db`, `config`, `noco_lectura`,
    `registro_lectura`, `auth`, `_menu`, `avisos_mod`, `crud`…) o es una librería de red de
    `LIBRERIAS_DE_RED` (`httpx`). Las funciones de cada uno no se teclean: son sus atributos llamables."""
    import types
    encontrados = {}
    for nombre, valor in vars(panel).items():
        if not isinstance(valor, types.ModuleType):
            continue
        archivo = getattr(valor, "__file__", None)
        del_proyecto = bool(archivo) and Path(archivo).resolve().is_relative_to(RAIZ)
        if del_proyecto or valor.__name__.split(".")[0] in LIBRERIAS_DE_RED:
            encontrados[nombre] = valor
    return encontrados


def espiar_la_ruta(monkeypatch) -> list:
    pedidos = []
    for nombre, modulo in modulos_por_los_que_la_ruta_puede_leer_hacia_fuera().items():
        monkeypatch.setattr(panel, nombre, _Modulo(nombre, modulo, pedidos.append))
    return pedidos


PARAMETROS_QUE_ABREN_OTRAS_COSAS = {
    "p": 1, "g": "CDS", "sin_grupo": 1, "nuevo": "CDS", "confirmar": "cerrar", "editar": "nombre", "t": 10,
    "editar_tarea": 10, "confirmar_borrar": 10, "editar_comentario": 100, "derivar": 10, "pq": "ab",
    "pdonde": "cliente", "quitar_grupo": "CDS", "borrar_proyecto": 1, "hecho": "proyecto_borrado", "borrado": 1,
    "borrada": 10, "grupo": "CDS", "editar_nota": 700, "borrar_nota": 701, "sq": "ana", "filtro": "mias",
}


# Lo que la ruta le pide a esos módulos cuando la vista es la de estado, SIN contar lo que hace en cualquier vista
# dentro del proceso (el menú, la sesión, los nombres de responsable): lo único que lee hacia fuera es la base,
# y solo la lista de proyectos y la de grupos. Declarado leyendo la ruta; si la ruta pide algo más, rojo.
LO_QUE_PIDE_LA_RUTA_CON_ESTADO = {
    "db.pagina_de_proyectos", "db.areas",                                  # la base: lo único que la vista usa
    "auth.puede_entrar", "auth.validar", "_menu.pantallas", "config.opciones_de_responsable",   # en el proceso
}


def _sin_red_de_verdad(monkeypatch):
    """Si algún camino llegara a la red, que sea un doble que cuenta (y no la red)."""
    import noco_lectura
    import registro_lectura
    llamadas = []

    async def _noco(*a, **k):
        llamadas.append("noco")
        return []

    async def _registro(*a, **k):
        llamadas.append("registro")
        return {}
    for nombre in ("buscar_personas", "persona", "contacto"):
        monkeypatch.setattr(noco_lectura, nombre, _noco)
    monkeypatch.setattr(registro_lectura, "buscar_sesiones", _registro)       # (`sesiones_de_cliente` sin cliente no sale a la red)
    return llamadas


def test_E8_con_la_vista_la_ruta_no_pide_a_ningun_modulo_nada_que_la_vista_no_use(mundo, monkeypatch):
    """QUÉ EXIGE: cada `{{ … }}` y cada `{% … %}` de la rama de la vista es, por igualdad exacta de texto normalizado, una entrada de la tabla cerrada `TABLA_DE_LA_VISTA` (con su clase: texto de persona, dato del modelo que no es texto, control de presentación) y, con `estado`, las funciones (atributos llamables) que la ruta pide directamente a un módulo envuelto son solo las que `LO_QUE_PIDE_LA_RUTA_CON_ESTADO` declara; lo no declarado cae en rojo. Los módulos envueltos son los globales de `web.app` que son módulos del proyecto o de `LIBRERIAS_DE_RED`; los demás módulos de `web.app` (p. ej. `asyncio`, `re`, `logging`) no se envuelven.
    QUÉ NO PROMETE: no vigila que la tabla declarada sea «correcta» (quien añada una entrada a la tabla puede añadir una decisión: es una lista que obliga a declarar, no a acertar) ni lo que la ruta pida a través de un OBJETO de un módulo (p. ej. `db.pool.<método>()`: el espía solo anota atributos llamables y `db.pool` no lo es), ni lo que pida a módulos que no son del proyecto ni de `LIBRERIAS_DE_RED` (p. ej. `asyncio`), ni las lecturas por caminos que el espía no envuelve (funciones traídas con `from … import`, lo que haga un hilo o una tarea aparte). Tampoco vigila que la prueba de escape declarada para cada texto de persona de la tabla escape de verdad ese texto: la tabla solo obliga a declarar una prueba y a que exista; que escape lo vigila cada prueba de escape por su cuenta.

    Los módulos envueltos SALEN de `web.app` (`modulos_por_los_que_la_ruta_puede_leer_hacia_fuera`): sus globales que
    son módulos del proyecto o de `LIBRERIAS_DE_RED`; las funciones de cada uno, de sus atributos llamables."""
    _sembrar_casos(mundo)
    assert {"db", "noco_lectura", "registro_lectura", "config", "auth", "_menu", "httpx"} <= set(
        modulos_por_los_que_la_ruta_puede_leer_hacia_fuera())
    red = _sin_red_de_verdad(monkeypatch)
    pedidos = espiar_la_ruta(monkeypatch)
    h = ver(mundo, estado=1, **PARAMETROS_QUE_ABREN_OTRAS_COSAS)
    assert 'class="estados"' in centro(h)
    assert set(pedidos) == LO_QUE_PIDE_LA_RUTA_CON_ESTADO, sorted(set(pedidos) ^ LO_QUE_PIDE_LA_RUTA_CON_ESTADO)
    assert red == []


def test_E8_el_espia_ve_lo_que_sin_la_vista_esos_parametros_si_hacen_leer(mundo, monkeypatch):
    """Control: sin `estado`, los mismos parámetros SÍ llevan a Noco, a las cuentas de borrar, etc. (si el espía no
    viera nada, la prueba de arriba no probaría)."""
    _sembrar_casos(mundo)
    red = _sin_red_de_verdad(monkeypatch)
    pedidos = espiar_la_ruta(monkeypatch)
    ver(mundo, p=1, pq="ab", pdonde="cliente", quitar_grupo="CDS", borrar_proyecto=1)
    assert {"db.contenido_para_borrar_grupo", "db.contenido_de_proyecto", "noco_lectura.buscar_personas"} <= set(pedidos)
    assert red and set(red) == {"noco"}      # la búsqueda de personas (y, con cliente, el contacto)


def test_E8_el_parametro_gana_a_los_demas_y_no_los_dibuja():
    h = pintar("casa", estado=1, p=1, nuevo="CDS", borrar_proyecto=1)
    assert 'class="estados"' in centro(h) and "Proyecto nuevo" not in centro(h) and "Borrar" not in centro(h)


# ═══════════════════════════════════════════════════════════════════════
# La guarda de solo ver acepta el campo escondido de la vista, y solo ese
# ═══════════════════════════════════════════════════════════════════════

_BUSCADOR = '<form class="buscador" method="get" action="/proyectos"><input type="search" name="q" value=""> {extra}</form>'


@pytest.mark.parametrize("extra,sobra", [
    ('<input type="hidden" name="estado" value="1">', False),       # el de la vista
    ('<input type="hidden" name="estado" value="2">', True),        # otro valor
    ('<input type="hidden" name="estado" value="">', True),
    ('<input type="hidden" name="estado">', True),                  # sin valor
    ('<input name="estado" value="1">', True),                      # sin type
    ('<input type="text" name="estado" value="1">', True),          # no escondido
    ('<input type="hidden" name="p" value="1">', True),             # otro campo
    ('<input type="hidden" name="estado" value="1"><input type="hidden" name="nuevo" value="CDS">', True),
])
def test_la_guarda_de_solo_ver_acepta_exactamente_el_campo_escondido_de_la_vista(extra, sobra):
    sobran = sv.controles_que_sobran(_BUSCADOR.format(extra=extra), "")
    assert bool(sobran) is sobra, (extra, sobran)
