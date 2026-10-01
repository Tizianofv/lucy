"""La página de proyectos contra la maqueta aprobada (1-oct-2026, versión 19):
las diferencias que se cerraron, cada una con lo que se puede comprobar de ella.
(La parte de las tres columnas, el menú, la lista y la cabecera de la versión 19
está en `tests/test_pagina_proyectos_v19.py`.)

  1. Los comentarios llevan la bolita con la inicial y el globo con borde.
  2. «Fecha límite» queda alineada con las fechas, con el margen de la ×.
  3. En modo oscuro el color de cada grupo se aclara, y sale de `areas.color`.
  4. El texto de «Tareas» ya no promete que el título lleva a otra pantalla.
  5. «+ Proyecto en X» abre una ventanita encima de la página; sin JavaScript
     sigue siendo la página aparte (`?nuevo=`).

FRONTERA, dicha una vez: NO es un navegador. Se lee el HTML y el CSS que se
sirven (no cómo se pintan), y el script corre con el `document` de mentira de
`tests/test_escrituras_proyecto.py` (JavaScriptCore de macOS). Que la ventanita
se vea encima de la página, o que un color se vea claro sobre el fondo oscuro,
no lo prueba nadie aquí: eso es mirar el HTML renderizado que se entrega junto
al trabajo. Lo que SÍ se vigila: que el servidor escriba la ventanita completa y
pegada a su enlace, que el guion solo abra y cierre, que el color oscuro salga
de la tabla por una regla y llegue a todo sitio que usa el color del grupo, y
que ninguna de las dos formas de abrir el formulario deje de enviar al mismo
sitio.

Correr:  python3 -m pytest tests/test_pagina_proyectos_maqueta.py -q
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from test_escrituras_proyecto import (_correr_en_jxa, _formularios_de, _lo_que_manda_el_navegador,
                                      _primera_habilitada, _problemas_de_html_simple, hay_osascript)
from test_grupo_ia import _ROOT
from test_pagina_proyectos import AREAS, _cliente, _dia, gente, mundo, ver  # noqa: F401
import config
import db.db as db


# ═══════════════════════════════════════════════════════════════════════
# Un lector de árbol y de CSS, lo justo
# ═══════════════════════════════════════════════════════════════════════

_VACIOS = {"input", "br", "hr", "img", "meta", "link"}


class _Nodo:
    def __init__(self, tag, attrs, padre):
        self.tag, self.attrs, self.padre, self.hijos, self.texto = tag, dict(attrs), padre, [], ""

    @property
    def clases(self):
        return (self.attrs.get("class") or "").split()

    def elementos(self):
        for h in self.hijos:
            yield h
            yield from h.elementos()

    def buscar(self, tag=None, clase=None):
        return [n for n in self.elementos()
                if (tag is None or n.tag == tag) and (clase is None or clase in n.clases)]

    def hermano_siguiente(self):
        h = self.padre.hijos
        i = h.index(self)
        return h[i + 1] if i + 1 < len(h) else None

    def ancestro_clase(self, clase):
        n = self.padre
        while n is not None:
            if clase in n.clases:
                return n
            n = n.padre
        return None

    def ancestro(self, tag):
        n = self.padre
        while n is not None:
            if n.tag == tag:
                return n
            n = n.padre
        return None

    def todo_el_texto(self):
        return self.texto + "".join(h.todo_el_texto() for h in self.hijos)


class _Arbol(HTMLParser):
    def __init__(self):
        super().__init__()
        self.raiz = _Nodo("#raiz", [], None)
        self._n = self.raiz

    def handle_starttag(self, tag, attrs):
        n = _Nodo(tag, attrs, self._n)
        self._n.hijos.append(n)
        if tag not in _VACIOS:
            self._n = n

    def handle_endtag(self, tag):
        n = self._n
        while n is not None and n.tag != tag:
            n = n.padre
        if n is not None and n.padre is not None:
            self._n = n.padre

    def handle_data(self, d):
        self._n.texto += d


def arbol(html: str) -> _Nodo:
    a = _Arbol()
    a.feed(html)
    a.close()
    return a.raiz


def _css(html: str) -> str:
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declaraciones(cuerpo: str) -> dict:
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in cuerpo.split(";") if ":" in d)}


def _regla(html: str, selector: str) -> dict:
    """Las declaraciones de la regla `selector{...}` de la hoja (una sola vez)."""
    hallazgos = re.findall(r"(?:^|[};])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", _css(html), re.M)
    assert len(hallazgos) == 1, (selector, len(hallazgos))
    return _declaraciones(hallazgos[0])


def _vistas(mundo) -> list[dict]:
    mundo.proyecto(1, "Uno", area="CDS", responsable=config.CHAT_ID_DUENO)
    mundo.proyecto(2, "Cerrado", area="ACD", estado="cerrado")
    mundo.proyecto(3, "Sin grupo", area=None)
    mundo.tarea(10, "pendiente", proyecto=1)
    mundo.tarea(30, "suelta", area="CDS")
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "primero")
    return [{}, {"p": 1}, {"p": 1, "t": 10}, {"p": 2}, {"p": 3}, {"g": "CDS"}, {"sin_grupo": 1},
            {"nuevo": "ACD"}, {"q": "uno"}, {"p": 1, "confirmar": "cerrar"}]


# ═══════════════════════════════════════════════════════════════════════
# 1. Los comentarios: la bolita con la inicial y el globo
# ═══════════════════════════════════════════════════════════════════════

def test_cada_comentario_lleva_su_bolita_con_la_inicial_y_su_globo(mundo, gente):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "con comentarios", proyecto=1)
    mundo.comentario(50, 10, gente.rosi, "Le escribí a Luis", cuando=_dia(-1, 15))
    mundo.comentario(51, 10, gente.dueno, "Segundo", cuando=_dia(0, 9), editado=_dia(0, 10))
    mundo.comentario(52, 10, 555000222, "de alguien sin nombre", cuando=_dia(0, 11))
    html = ver(mundo, p=1, t=10)
    comentarios = arbol(html).buscar("div", "comentario")
    assert len(comentarios) == 3
    esperado = [("Persona Dos", "PD"), ("Persona Uno", "PU"), ("Alguien", "A")]
    for c, (autor, inicial) in zip(comentarios, esperado):
        hijos = [h for h in c.hijos if h.tag in ("span", "div")]
        # Primero la bolita, luego el globo: como en la maqueta.
        assert [h.tag for h in hijos] == ["span", "div"], autor
        bolita, globo = hijos
        assert bolita.clases == ["ini"] and bolita.todo_el_texto() == inicial, (autor, bolita.todo_el_texto())
        assert bolita.attrs.get("title") == autor
        assert globo.clases == ["globo"]
        # Dentro del globo: quién y cuándo, y el texto (y el cuadro de editarlo).
        assert [b.todo_el_texto() for b in globo.buscar("b")] == [autor]
        assert len(globo.buscar("p")) == 1 and len(globo.buscar("form", "renombrar")) == 1
        assert not bolita.buscar() and "555000222" not in c.todo_el_texto()


def test_el_globo_se_ve_con_borde_y_fondo_como_en_la_maqueta(mundo):
    mundo.proyecto(1, "P", area="CDS")
    html = ver(mundo, p=1)
    globo = _regla(html, ".comentario .globo")
    assert globo["background"] == "var(--papel)"
    assert globo["border"] == "1px solid var(--linea)"
    assert globo["border-radius"] == "10px" and globo["flex"] == "1" and globo["min-width"] == "0"
    fila = _regla(html, ".comentario")
    assert fila["display"] == "flex" and fila["align-items"] == "flex-start"
    bolita = _regla(html, ".comentario .ini")
    assert (bolita["width"], bolita["height"]) == ("1.6rem", "1.6rem")


# ═══════════════════════════════════════════════════════════════════════
# 2. «Fecha límite», alineada con las fechas
# ═══════════════════════════════════════════════════════════════════════

def _padding_derecho(decl: dict) -> str:
    if "padding-right" in decl:
        return decl["padding-right"].replace("!important", "").strip()
    v = decl["padding"].replace("!important", "").split()
    return {1: v[0], 2: v[1], 3: v[1], 4: v[1]}[len(v)]


def test_fecha_limite_deja_sitio_a_la_x_y_comparte_la_columna_con_las_fechas(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "con fecha", proyecto=1, vence=_dia(3))
    html = ver(mundo, p=1)
    titulo = _regla(html, ".col-fecha")
    assert titulo["justify-content"] == "flex-end"
    assert _padding_derecho(titulo) == "1.9rem"
    # La columna de las fechas y la del título miden y se alinean igual.
    titulo_span, fecha = _regla(html, ".col-fecha span"), _regla(html, ".vence")
    assert titulo_span["min-width"] == fecha["min-width"] == "6.5rem"
    assert titulo_span["text-align"] == "right" and fecha["align-items"] == "flex-end"
    # El título de columna va justo antes de la lista, una sola vez, con ese texto.
    assert html.count('<div class="col-fecha"><span>Fecha límite</span></div>\n<div class="tareas">') == 1


# ═══════════════════════════════════════════════════════════════════════
# 3. El color de cada grupo en modo oscuro
# ═══════════════════════════════════════════════════════════════════════

# Los tres colores de la maqueta aprobada, versión 19 (`disenos/lucy-proyectos/
# maqueta-v19.html`, líneas 9 y 15), copiados: color del grupo -> el suyo para el
# fondo oscuro.
MAQUETA_OSCURO = {"#c8102e": "#ff6b6b", "#ef6c00": "#ffa04a", "#8a4a8f": "#c98ccf"}
FONDO_OSCURO = "#18201d"                      # `--papel` en modo oscuro (la hoja)


def _rgb(h: str) -> tuple:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _luminancia(h: str) -> float:
    def lineal(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lineal(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contraste(a: str, b: str) -> float:
    x, y = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


@pytest.mark.parametrize("claro,maqueta", sorted(MAQUETA_OSCURO.items()))
def test_la_regla_aclara_los_colores_de_la_maqueta_casi_igual_que_ella(claro, maqueta):
    oscuro = db.color_oscuro_de_grupo(claro)
    assert max(abs(a - b) for a, b in zip(_rgb(oscuro), _rgb(maqueta))) <= 6, (claro, oscuro, maqueta)
    assert oscuro != claro


def test_la_regla_no_es_una_tabla_de_tres_colores():
    """Un color de grupo que la maqueta no tiene también se aclara, sin que nadie
    lo escriba en ningún sitio."""
    for nuevo in ("#2255aa", "#aa2244", "#55781a"):
        oscuro = db.color_oscuro_de_grupo(nuevo)
        assert oscuro not in MAQUETA_OSCURO.values() and oscuro != nuevo
        assert _luminancia(oscuro) > _luminancia(nuevo)


def test_la_regla_deja_legible_cada_color_sobre_el_fondo_oscuro():
    """Una rejilla de colores (36 tonos x 6 claridades x 4 saturaciones = 864):
    todos quedan con contraste de lectura (4.5) sobre el fondo oscuro de la
    página y con la forma de un hex de seis dígitos."""
    import colorsys
    n = 0
    for h in range(0, 360, 10):
        for l in (.2, .3, .4, .5, .6, .7):
            for s in (.2, .5, .8, 1):
                c = "#%02x%02x%02x" % tuple(round(v * 255) for v in colorsys.hls_to_rgb(h / 360, l, s))
                o = db.color_oscuro_de_grupo(c)
                assert re.fullmatch(r"#[0-9a-f]{6}", o), (c, o)
                assert _contraste(o, FONDO_OSCURO) >= 4.5, (c, o)
                n += 1
    assert n == 864


def test_los_bordes_de_la_regla_un_gris_sigue_gris_y_lo_que_no_es_color_no_rompe():
    gris = db.color_oscuro_de_grupo("#6b6b6b")
    assert len(set(_rgb(gris))) == 1 and _luminancia(gris) > _luminancia("#6b6b6b")
    assert db.color_oscuro_de_grupo("#fff") == "#ffffff"
    assert db.color_oscuro_de_grupo("#abc") == db.color_oscuro_de_grupo("#aabbcc")
    assert db.color_oscuro_de_grupo("#aabbcc80") == db.color_oscuro_de_grupo("#aabbcc")
    assert db.color_oscuro_de_grupo("#12345") == "#12345"           # largo que no se sabe leer
    for malo in (None, "", "red", '#fff;background:url(x)', 'x"onload="1'):
        assert db.color_oscuro_de_grupo(malo) == db.color_oscuro_de_grupo(db.COLOR_SIN_GRUPO), malo
        assert re.fullmatch(r"#[0-9a-f]{6}", db.color_oscuro_de_grupo(malo))


def _con_estilo(raiz: _Nodo) -> list[_Nodo]:
    return [n for n in raiz.elementos() if "style" in n.attrs]


def test_cada_sitio_que_usa_el_color_del_grupo_lleva_el_claro_y_el_oscuro_de_la_tabla(mundo):
    """Hermanos: la lista sale del HTML de todas las vistas. Todo elemento con
    `style` lleva `--claro` y `--oscuro` (nunca un `--color` suelto, que no
    cambiaría con el modo) y la clase `gc`; y los dos valores son los de la
    tabla `areas`, el segundo por la regla."""
    vistas = _vistas(mundo)
    por_clave = {a["clave"]: a["color"] for a in AREAS}
    esperado = {(c, db.color_oscuro_de_grupo(c)) for c in [*por_clave.values(), db.COLOR_SIN_GRUPO]}
    vistos = set()
    for consulta in vistas:
        html = ver(mundo, **consulta)
        sitios = _con_estilo(arbol(html))
        assert sitios, consulta
        for n in sitios:
            if "--" not in n.attrs["style"]:
                continue                       # un ancho o un relleno, no un color de grupo
            m = re.fullmatch(r"(?:display:contents;)?--claro:(#[0-9a-f]{3,8});--oscuro:(#[0-9a-f]{3,8})",
                             n.attrs["style"])
            assert m is not None, (consulta, n.tag, n.attrs["style"])
            assert "gc" in n.clases, (consulta, n.tag, n.attrs["style"])
            vistos.add((m.group(1), m.group(2)))
        assert "--color:#" not in html.split("<style>", 1)[1].split("</style>", 1)[1], consulta
    assert vistos <= esperado and {c for c, _ in vistos} >= {"#0f7c74", "#b5611a", "#8a4a8f"}


def test_el_color_oscuro_sigue_a_la_tabla_cuando_cambia_un_color(mundo):
    mundo.proyecto(1, "P", area="IA")
    mundo.con.execute("UPDATE areas SET color = '#2255aa' WHERE clave = 'IA'")
    html = ver(mundo, p=1)
    assert "--claro:#2255aa;--oscuro:" + db.color_oscuro_de_grupo("#2255aa") in html
    assert "#8a4a8f" not in html and db.color_oscuro_de_grupo("#8a4a8f") not in html


def test_ninguna_plantilla_ni_la_hoja_teclea_el_color_oscuro_de_un_grupo():
    fuente = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    for tono in MAQUETA_OSCURO.values():
        assert tono not in fuente, tono


def test_la_hoja_cambia_de_color_con_el_modo_y_la_regla_oscura_va_despues(mundo):
    mundo.proyecto(1, "P", area="CDS")
    css = _css(ver(mundo, p=1))
    claro = css.index(".gc{--color:var(--claro)}")
    oscuro = css.index(".gc{--color:var(--oscuro)}")
    modo = css.index("@media (prefers-color-scheme: dark)")
    # La clara va antes del bloque oscuro y la oscura DENTRO de él: la oscura gana.
    assert claro < modo < oscuro
    assert css.index("}}", oscuro) > oscuro and css.count(".gc{--color:") == 2
    assert css[modo:oscuro].count("{") - css[modo:oscuro].count("}") == 1       # sigue abierto el @media


# ═══════════════════════════════════════════════════════════════════════
# 4. El texto de «Tareas»
# ═══════════════════════════════════════════════════════════════════════

def test_el_texto_de_tareas_dice_lo_que_hace_el_titulo(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "una", proyecto=1)
    html = ver(mundo, p=1)
    nota = [n.todo_el_texto() for n in arbol(html).buscar("span", "nota") if "vencidas arriba" in n.todo_el_texto()]
    assert nota == ["Las vencidas arriba. Toca el título de una tarea para ver su detalle y sus comentarios"]
    assert "pantalla" not in nota[0]
    # Y es verdad: el título es el enlace que abre el detalle de ESA tarea.
    titulo = arbol(html).buscar("a", "titulo")[0]
    assert titulo.attrs["href"].endswith("&t=10#tarea-10") and titulo.todo_el_texto() == "una"
    # El detalle abierto de verdad trae los comentarios que el texto promete.
    assert "<h4>Comentarios</h4>" in ver(mundo, p=1, t=10)


def test_la_plantilla_ya_no_dice_que_el_titulo_lleva_a_otra_pantalla():
    fuente = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    assert "lleva a su pantalla" not in fuente


# ═══════════════════════════════════════════════════════════════════════
# 5. «Nuevo proyecto» como ventanita
# ═══════════════════════════════════════════════════════════════════════

def _enlaces_de_nuevo(raiz: _Nodo) -> list[_Nodo]:
    return raiz.buscar("a", "nuevo-proy")


def _ventana_de(enlace: _Nodo) -> _Nodo:
    """La ventanita de un «+»: la única `<dialog>` del MISMO grupo (es donde la
    busca el script: `closest(".grupo").querySelector("dialog.ventana")`)."""
    grupo = enlace.ancestro_clase("grupo")
    ventanas = grupo.buscar("dialog", "ventana")
    assert len(ventanas) == 1
    return ventanas[0]


def test_cada_mas_de_proyecto_nuevo_tiene_su_ventanita_en_su_grupo_con_el_formulario_de_su_grupo(mundo, gente):
    for consulta in _vistas(mundo):
        raiz = arbol(ver(mundo, **consulta))
        enlaces = _enlaces_de_nuevo(raiz)
        if "q" in consulta:                         # buscando, no hay «+ Proyecto» ni ventanita
            assert enlaces == [] and raiz.buscar("dialog") == [], consulta
            continue
        assert [e.attrs["href"] for e in enlaces] == [f"/proyectos?nuevo={a['clave']}" for a in AREAS], consulta
        for enlace, area in zip(enlaces, AREAS):
            # El «+» vive en el nombre del grupo, con su texto y su nombre para quien lee en voz alta.
            assert enlace.clases == ["nuevo-proy", "mas"] and enlace.todo_el_texto() == "+", consulta
            assert enlace.padre.tag == "h3" and enlace.attrs["aria-label"] == f"Proyecto nuevo en {area['clave']}"
            ventana = _ventana_de(enlace)
            assert ventana.tag == "dialog" and ventana.clases == ["ventana"], consulta
            assert "open" not in ventana.attrs and "style" not in ventana.attrs
            assert ventana.attrs["aria-label"] == f"Proyecto nuevo en {area['clave']}"
            assert [h.todo_el_texto() for h in ventana.buscar("h2")] == [f"Proyecto nuevo en {area['clave']}"]
            forms = ventana.buscar("form")
            assert len(forms) == 1 and forms[0].attrs["action"] == "/proyectos/nuevo"
            assert forms[0].attrs["method"] == "post" and forms[0].clases == ["nuevo"]
            campos = {c.attrs.get("name"): c for c in forms[0].buscar() if c.attrs.get("name")}
            assert sorted(campos) == ["area", "nombre", "responsable"], consulta
            assert campos["area"].attrs["value"] == area["clave"]            # el grupo de SU enlace
            assert campos["nombre"].attrs["maxlength"] == str(db.LARGO_NOMBRE_PROYECTO)
            opciones = [o.attrs.get("value") for o in campos["responsable"].buscar("option")]
            assert opciones == ["", "Persona Uno", "Persona Dos", "Code"], opciones
            assert "Cliente" not in ventana.todo_el_texto()                   # el cliente llega con otro trabajo


def test_sin_grupo_no_ofrece_ventanita_ni_enlace(mundo):
    mundo.proyecto(1, "Suelto", area=None)
    raiz = arbol(ver(mundo))
    assert len(raiz.buscar("dialog")) == len(AREAS)
    sin_grupo = [g for g in raiz.buscar("div", "grupo")
                 if any(h.todo_el_texto() == "Sin grupo" for h in g.buscar("h3"))]
    assert len(sin_grupo) == 1 and sin_grupo[0].buscar("dialog") == [] and _enlaces_de_nuevo(sin_grupo[0]) == []


def test_sin_javascript_el_enlace_sigue_llevando_a_la_pagina_aparte(mundo, gente):
    """El enlace es un enlace de verdad con su `href`, y la página aparte sigue
    ahí con su formulario. Ninguno de los dos depende del script."""
    mundo.proyecto(1, "Uno", area="CDS")
    html = ver(mundo)
    assert ('<a class="nuevo-proy mas" href="/proyectos?nuevo=ACD" aria-label="Proyecto nuevo en ACD" '
            'title="Proyecto nuevo en ACD">+</a>') in html
    aparte = ver(mundo, nuevo="ACD")
    assert "<h1>Proyecto nuevo en ACD</h1>" in aparte
    forms = [f for f in arbol(aparte).buscar("form", "nuevo") if f.ancestro("dialog") is None]
    assert len(forms) == 1 and forms[0].attrs["action"] == "/proyectos/nuevo"
    campos = {c.attrs.get("name"): c.attrs.get("value") for c in forms[0].buscar("input")}
    assert campos["area"] == "ACD"
    # El `<dialog>` cerrado no se pinta: sin script no hay nada que lo abra, y la
    # hoja no lo fuerza visible.
    css = _css(html)
    assert not re.search(r"dialog[^{]*\{[^}]*display\s*:\s*(block|flex|grid)", css)


def test_la_ventanita_y_la_pagina_aparte_envian_lo_mismo_a_la_misma_ruta(mundo, gente):
    mundo.proyecto(1, "Uno", area="CDS")
    html = ver(mundo, nuevo="ACD")
    formularios = [f for f in _formularios_de(html) if f["accion"] == "/proyectos/nuevo"]
    assert len(formularios) == len(AREAS) + 1
    enviados = []
    for f in formularios:
        datos = _lo_que_manda_el_navegador(f, lambda c: "Un proyecto", _primera_habilitada)
        enviados.append(tuple(sorted(datos)))
        assert datos["nombre"] == "Un proyecto" and datos["responsable"] == "Persona Uno"
    assert set(enviados) == {("area", "nombre", "responsable")}
    assert sorted(_lo_que_manda_el_navegador(f, lambda c: "x", _primera_habilitada)["area"]
                  for f in formularios) == sorted([a["clave"] for a in AREAS] + ["ACD"])


def test_enviar_cada_ventanita_crea_el_proyecto_en_su_grupo(mundo, gente):
    for area in AREAS:
        raiz = arbol(ver(mundo))
        enlace = next(e for e in _enlaces_de_nuevo(raiz) if e.attrs["href"].endswith("=" + area["clave"]))
        form = _ventana_de(enlace).buscar("form")[0]
        datos = {c.attrs["name"]: c.attrs.get("value", "") for c in form.buscar("input")}
        datos["nombre"] = "Desde la ventanita " + area["clave"]
        datos["responsable"] = "Persona Dos"
        r = _cliente(config.CHAT_ID_DUENO).post(form.attrs["action"], data=datos, follow_redirects=False)
        assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]
        fila = mundo.con.execute("SELECT area, responsable_chat_id FROM proyectos WHERE nombre = ?",
                                 ("Desde la ventanita " + area["clave"],)).fetchone()
        assert tuple(fila) == (area["clave"], gente.rosi)


def test_cada_ventanita_cumple_la_regla_de_html_simple_y_su_cancelar_es_un_enlace(mundo):
    for consulta in _vistas(mundo):
        html = ver(mundo, **consulta)
        assert _problemas_de_html_simple(html) == [], consulta
        for ventana in arbol(html).buscar("dialog"):
            cancelar = ventana.buscar("a", "cancelar")
            assert len(cancelar) == 1 and cancelar[0].attrs["href"] == "/proyectos"
            assert cancelar[0].ancestro("dialog") is ventana
            assert [b.todo_el_texto() for b in ventana.buscar("button")] == ["Crear proyecto"]


def test_la_hoja_de_la_ventanita_es_la_de_la_maqueta(mundo):
    html = ver(mundo)
    v = _regla(html, "dialog.ventana")
    assert v["border"] == "1px solid var(--linea)" and v["border-radius"] == "14px"
    assert v["background"] == "var(--papel)" and v["width"] == "min(26rem,calc(100vw - 32px))"
    assert _regla(html, "dialog.ventana::backdrop")["background"] == "rgb(0 0 0 / .35)"
    assert _regla(html, "dialog.ventana .botones")["justify-content"] == "flex-end"


# ── El guion: abre y cierra, nada más ──────────────────────────────────────

_FALSOS = """
/* El grupo del «+»: solo sabe contestar `querySelector("dialog.ventana")`. */
function grupoDeMentira(ventana) {
  return {querySelector: function (s) { return s === "dialog.ventana" ? ventana : null; }};
}
function enlaceDeMentira(clase, grupo, ventana) {
  return {closest: function (s) {
            if (s === "a." + clase) return this;
            if (s === ".grupo") return grupo || null;
            if (s === "dialog") return ventana || null;
            return null; }};
}
function ventanaDeMentira(conSoporte) {
  var v = {abiertas: 0, cerradas: 0};
  if (conSoporte) {
    v.showModal = function () { this.abiertas++; };
    v.close = function () { this.cerradas++; };
  }
  return v;
}
"""


def _guion_de(mundo) -> str:
    """El `<script>` de la página servida (la sin proyectos también lo lleva)."""
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", ver(mundo), re.S)
    assert len(guiones) == 1
    return guiones[0]


def _jxa(mundo, escenario: str) -> dict:
    return _correr_en_jxa(_guion_de(mundo), _FALSOS + escenario)


@hay_osascript
def test_js_el_clic_en_proyecto_nuevo_abre_la_ventanita_y_no_sigue_el_enlace(mundo):
    r = _jxa(mundo, "var V = ventanaDeMentira(true); var L = enlaceDeMentira('nuevo-proy', grupoDeMentira(V));"
                    "oyentes.click(ev(L));"
                    "JSON.stringify({abiertas: V.abiertas, cerradas: V.cerradas, evitado: evitado,"
                    " esperando: cuantosTemporizadores()})")
    assert r == {"abiertas": 1, "cerradas": 0, "evitado": 1, "esperando": 0}


@hay_osascript
def test_js_si_el_navegador_no_sabe_de_dialog_el_enlace_sigue_a_la_pagina_aparte(mundo):
    r = _jxa(mundo, "var V = ventanaDeMentira(false); var L = enlaceDeMentira('nuevo-proy', grupoDeMentira(V));"
                    "oyentes.click(ev(L)); var N = enlaceDeMentira('nuevo-proy', grupoDeMentira(null));"
                    "oyentes.click(ev(N)); var M = enlaceDeMentira('nuevo-proy', null);"
                    "oyentes.click(ev(M));"
                    "JSON.stringify({abiertas: V.abiertas, evitado: evitado})")
    assert r == {"abiertas": 0, "evitado": 0}


@hay_osascript
def test_js_cancelar_cierra_la_ventanita_y_nada_mas(mundo):
    r = _jxa(mundo, "var V = ventanaDeMentira(true); var C = enlaceDeMentira('cancelar', null, V);"
                    "oyentes.click(ev(C));"
                    "JSON.stringify({abiertas: V.abiertas, cerradas: V.cerradas, evitado: evitado,"
                    " esperando: cuantosTemporizadores()})")
    assert r == {"abiertas": 0, "cerradas": 1, "evitado": 1, "esperando": 0}
    sin = _jxa(mundo, "var V = ventanaDeMentira(false); var C = enlaceDeMentira('cancelar', null, V);"
                      "oyentes.click(ev(C)); JSON.stringify({evitado: evitado})")
    assert sin == {"evitado": 0}


@hay_osascript
def test_js_el_clic_en_el_titulo_de_una_tarea_sigue_funcionando_con_lo_nuevo(mundo):
    """El clic del título espera al segundo (lo prueban los de
    `test_escrituras_proyecto.py`); acá, que un clic en un enlace de la ventanita
    NO deja un temporizador del título y que el del título no abre ninguna."""
    r = _jxa(mundo, "var V = ventanaDeMentira(true);"
                    "var T = {closest: function (s) { return (s === 'a[data-dbl]') ? this : null; }};"
                    "oyentes.click(ev(T));"
                    "JSON.stringify({abiertas: V.abiertas, esperando: cuantosTemporizadores()})")
    assert r == {"abiertas": 0, "esperando": 1}


@hay_osascript
def test_js_con_la_estructura_de_la_pagina_cada_enlace_abre_su_ventanita(mundo):
    """Los enlaces y lo que hay pegado a cada uno salen del HTML servido: si el
    servidor dejara un enlace sin su ventanita al lado, el clic no abriría
    nada (y el enlace llevaría a la página aparte)."""
    casos = []
    for consulta in _vistas(mundo):
        for enlace in _enlaces_de_nuevo(arbol(ver(mundo, **consulta))):
            grupo = enlace.ancestro_clase("grupo")
            casos.append({"href": enlace.attrs["href"], "dialog": len(grupo.buscar("dialog", "ventana")) == 1})
    assert len(casos) >= len(AREAS) * 5 and all(c["dialog"] for c in casos), casos
    escenario = ("var casos = " + json.dumps(casos) + ";"
                 "JSON.stringify(casos.map(function (c) {"
                 "  var V = c.dialog ? ventanaDeMentira(true) : {};"
                 "  var L = enlaceDeMentira('nuevo-proy', grupoDeMentira(c.dialog ? V : null));"
                 "  oyentes.click(ev(L));"
                 "  return {href: c.href, abiertas: V.abiertas || 0};}))")
    for dicho in _jxa(mundo, escenario):
        assert dicho["abiertas"] == 1, dicho


def test_el_guion_de_la_ventanita_no_escribe_ni_decide_nada(mundo):
    guion = re.sub(r"/\*.*?\*/", "", _guion_de(mundo), flags=re.S)
    assert "showModal()" in guion and ".close()" in guion
    assert re.search(r"fetch\(|XMLHttpRequest|\.submit\(|FormData|localStorage|\.action", guion) is None
    # Sigue habiendo un oyente por evento: la ventanita se cuelga del clic que ya había.
    assert sorted(re.findall(r'addEventListener\("(\w+)"', guion)) == [
        "change", "click", "dblclick", "focusout", "keydown", "load", "submit"]
