"""Marcar o desmarcar una tarea en Proyectos no mueve lo que la persona está viendo
(Tiziano, 6-oct-2026: «cuando seleccione una tarea como realizada la pagina se me
mueve»).

LA CAUSA, medida en el navegador: el formulario del círculo hacía POST, el servidor
contestaba 303 a la página entera con `#tarea-N`, y el navegador la cargaba de cero:
el centro (`.envoltura > main`, con su propio desplazamiento) y la ventana quedaban
donde los dejara el ancla, no donde estaba la persona.

EL ARREGLO: el guion de la página (`marcarSinSaltar`, hoy en la pieza única
`_marcar_sin_saltar.html` que también usan las pantallas de Tareas y, desde el 13-oct-2026, todo enlace
y formulario de Proyectos; ver `tests/test_tareas_sin_saltar.py` y `tests/test_pagina_sin_saltos.py`) envía EL
MISMO formulario (mismo POST, misma ruta). El navegador sigue la redirección del
servidor y baja la página que el servidor decidió mostrar (la de antes, con sus
avisos y sin los estados viejos de la URL); con ESA página, sin pedir nada más, se
cambian la columna de la izquierda y el centro y se devuelve el desplazamiento. Si el
POST no llega o el servidor contesta un error, envía el formulario de la forma de
siempre; si el POST contestó bien pero la página no se puede poner (ya se guardó),
va a esa página con un enlace, sin reenviar el POST.

QUÉ VIGILA ESTE ARCHIVO Y QUÉ NO. Vigila: (1) la ruta real marca y la página que se
redirige a una página con SOLO el aviso nuevo y lo que se ve (tarea, cuenta); (2) todos los formularios que
marcan o desmarcan, sacados del HTML real, son los que el guion intercepta; (3) la
función del guion tiene las MISMAS prohibiciones que el resto del guion, salvo un
POST declarado; (4) el guion, corrido en JavaScriptCore con un DOM de mentira,
impide el envío normal, hace UN POST y ningún otro pedido, pinta lo que trajo el
servidor, restaura el desplazamiento de las dos columnas y de la ventana, cae al
envío normal si el POST falla y va a la página (sin reenviar) si guardó y no pudo
pintarla. NO vigila el alto ni el desplazamiento reales: eso es de un navegador (se midió
a mano; los números van en el reporte del trabajo)."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_escrituras_proyecto import _script_de_la_pagina, hay_osascript
from _sin_saltar import correr, lo_prohibido_en_la_pieza  # noqa: F401
from test_escrituras_tarea import mt, post, tarea  # noqa: F401
from test_grupo_ia import _ROOT
from _navegador import dar_recibo
from test_pagina_proyectos import _cliente, gente, mundo  # noqa: F401
import config

CLAVES_DEL_AVISO = ("hecho", "error", "derivadas", "derivar")
MARCAR = re.compile(r'<form class="marcar" method="post" action="(/proyectos/tarea/(\d+)/(hecha|reabrir)(?:\?[^"]*)?)"')


def _pagina(**consulta) -> str:
    r = _cliente(config.CHAT_ID_DUENO).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:200]
    return r.text


def _clase_de(html: str, tid: int) -> list[str]:
    return re.search(r'<div class="(tarea[^"]*)" id="tarea-%d"' % tid, html).group(1).split()


# (consulta que la persona está viendo, la tarea que toca): la lista de un
# proyecto, el mismo con un detalle abierto, las sueltas de un grupo y las de
# «sin grupo».
SITIOS = [({"p": 2}, 10), ({"p": 2, "t": 12}, 10), ({"g": "CDS"}, 30), ({"sin_grupo": 1}, 31)]


ESTADOS_VIEJOS = ("tarea_creada", "sala_no", "confirmar_borrar", "editar_tarea", "editar_comentario", "t",
                  "creado", "area_guardada", "nombre_guardado", "confirmar")


def _avisos(html: str) -> list[str]:
    return re.findall(r'<p class="aviso[^"]*">(.*?)</p>', html)


@pytest.mark.parametrize("donde,tid", SITIOS, ids=lambda x: str(x))
def test_la_ruta_marca_y_desmarca_y_la_pagina_a_la_que_redirige_es_la_que_se_pinta(mt, donde, tid):
    """Camino de producción de punta a punta: el POST que envía el formulario y la
    página a la que redirige el servidor (la que el guion pinta tal cual, sin pedir
    otra). Lo que cambia en pantalla al marcar (la tarea hecha, UN aviso y la cuenta
    del avance) sale de ahí, y la dirección no trae ningún estado viejo."""
    antes = _pagina(**donde)
    assert "hecha" not in _clase_de(antes, tid)
    r = post(f"/proyectos/tarea/{tid}/hecha")
    assert r.status_code == 303
    destino = urlsplit(r.headers["location"])
    claves = dict(parse_qsl(destino.query))
    assert claves.pop("hecho") == "tarea_hecha" and not (set(claves) & set(ESTADOS_VIEJOS)), claves
    despues = _pagina(**dict(parse_qsl(destino.query)))
    assert "hecha" in _clase_de(despues, tid)
    assert _avisos(despues) == ["Tarea marcada como hecha."]
    if "p" in donde:     # la cuenta del avance sube en uno, como con la recarga
        def n(h):
            return int(re.search(r"(\d+) de \d+ tareas hechas", h).group(1))
        assert n(despues) == n(antes) + 1
    # Y desmarcar: vuelve a pendiente, con SU aviso (y solo ese).
    r = post(f"/proyectos/tarea/{tid}/reabrir")
    otra = _pagina(**dict(parse_qsl(urlsplit(r.headers["location"]).query)))
    assert "hecha" not in _clase_de(otra, tid)
    assert len(_avisos(otra)) == 1 and "Tarea desmarcada" in _avisos(otra)[0]
    assert tarea(mt, tid)["estado"] == "pendiente"


def test_el_servidor_descarta_los_estados_viejos_al_marcar(mt):
    """El caso del testigo: la persona está en `?p=2&tarea_creada=..&confirmar_borrar=..`
    y marca otra tarea. Lo que el guion pinta es la página de la redirección: no
    hereda ese aviso ni el «¿Borrar?»; la dirección vieja SÍ los pintaría (por eso
    el guion no la vuelve a pedir). Desde `web/avisos.py` la dirección vieja solo pinta el
    aviso mientras conserve SU recibo (aquí se le da con `dar_recibo`); sin él, a mano, no."""
    assert not any("Tarea creada" in a for a in _avisos(_pagina(p=2, tarea_creada=12, confirmar_borrar=10)))
    dar_recibo("/proyectos", tarea_creada=12)
    vieja = _pagina(p=2, tarea_creada=12, confirmar_borrar=10)
    assert any("Tarea creada" in a for a in _avisos(vieja))
    r = post("/proyectos/tarea/10/hecha")
    nueva = _pagina(**dict(parse_qsl(urlsplit(r.headers["location"]).query)))
    assert _avisos(nueva) == ["Tarea marcada como hecha."]


def test_todos_los_formularios_que_marcan_son_los_que_intercepta_el_guion(mt):
    """Hermanos: cada formulario de la página que marca o desmarca una tarea (de
    TODAS las vistas, sacados del HTML real) es un `form.marcar`, que es lo que
    engancha el guion; y la plantilla no los escribe de otra forma en otro sitio."""
    vistas = [{"p": 1}, {"p": 2}, {"p": 2, "t": 12}, {"p": 3}, {"p": 4}, {"g": "CDS"}, {"g": "ACD"},
              {"sin_grupo": 1}, {}]
    vistos = set()
    for v in vistas:
        html = _pagina(**v)
        marcan = re.findall(r'<form\b[^>]*action="(/proyectos/tarea/\d+/(?:hecha|reabrir)(?:\?[^"]*)?)"[^>]*>', html)
        enganchables = [m.group(1) for m in MARCAR.finditer(html)]
        assert sorted(marcan) == sorted(enganchables), (v, marcan, enganchables)
        vistos |= set(marcan)
    assert any(x.endswith("/hecha") for x in vistos) and any(x.endswith("/reabrir") for x in vistos), vistos
    fuente = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    assert len(re.findall(r'action="/proyectos/tarea/\{\{ t\.id \}\}/(?:hecha|reabrir)(?:\{\{ consulta_filtro \}\})?"', fuente)) == 2
    assert len(re.findall(r'<form class="marcar" method="post" action="/proyectos/tarea/\{\{ t\.id \}\}/(?:hecha|reabrir)(?:\{\{ consulta_filtro \}\})?"', fuente)) == 2


# LAS PROHIBICIONES de la pieza (las mismas que las del resto del guion, con lo permitido declarado
# uno por uno) viven en `tests/test_pagina_sin_saltos.py`, sección 3: ahora la pieza hace más de un
# pedido (un enlace, un formulario, «atrás») y se vigila ENTERA, no una sola función.


# ── El guion corrido de verdad (JavaScriptCore) con el navegador de mentira de `tests/_sin_saltar.py` ──
# FRONTERA: no es un navegador; no mide pixeles ni alto. Comprueba lo que el guion HACE con lo que ve:
# impedir el envío normal, UN POST y ningún otro pedido, pintar lo que trajo el servidor (cada nodo
# nuevo nace de LA página que contestó), devolver el desplazamiento y qué hace si algo falla.

def _marcar(escenario_extra: str = "") -> str:
    return ('var f = formulario("/proyectos/tarea/10/hecha", "post", []); f.clases = ["marcar"]; __e = enviar(f);'
            + escenario_extra)


URL_FINAL = "http://x.test/proyectos?p=2&hecho=tarea_hecha"


@hay_osascript
def test_js_marcar_pinta_lo_que_trajo_el_servidor_sin_un_segundo_pedido(mundo):
    r = correr(_script_de_la_pagina(mundo), _marcar('responder(0, "PAGINA-DEL-SERVIDOR", {url: %r});' % URL_FINAL))
    assert r["pedidos"] == [["POST", "/proyectos/tarea/10/hecha", ""]] and r["prevenido"] is True
    # lo pintado nació de LA página que contestó el servidor (no de la vieja)…
    assert r["izq"] == "PAGINA-DEL-SERVIDOR" and r["cen"] == "PAGINA-DEL-SERVIDOR"
    # …el desplazamiento (de las dos columnas y de la ventana) es el de antes…
    assert r["izqTop"] == 300 and r["cenTop"] == 1200 and r["ventana"] == [[0, 50]]
    # …la dirección es la verdadera y marcar no suma una entrada al historial…
    assert r["historia"] == [["replace", None, "/proyectos?p=2&hecho=tarea_hecha"]]
    # …y no se recarga ni se va a ningún sitio.
    assert r["salidas"] == []


@hay_osascript
@pytest.mark.parametrize("modo", ["red", "500", "401"])
def test_js_si_el_post_no_llega_o_falla_nunca_se_reenvia_un_guardado_que_pudo_llegar(mundo, modo):
    """Sin respuesta (red) o con 5xx el POST pudo llegar: NO se reenvía, se lee un aviso. Con 4xx (401: la sesión
    venció) el servidor no escribió nada y se envía el formulario de siempre (con su botón)."""
    cierre = {"red": "sinRed(0);", "500": 'responder(0, "ERROR", {ok: false, status: 500});',
              "401": 'responder(0, "ENTRAR", {ok: false, status: 401});'}[modo]
    r = correr(_script_de_la_pagina(mundo), _marcar(cierre))
    assert [p[0] for p in r["pedidos"]] == ["POST"]
    assert r["izq"] == "VIEJA" and r["cen"] == "VIEJA" and r["historia"] == []        # nada se pintó
    if modo == "401":
        assert r["salidas"] == [["requestSubmit", "/proyectos/tarea/10/hecha", None]]
    else:
        assert [x[0] for x in r["salidas"]] == ["aviso"]                              # ni submit ni requestSubmit
    assert r["escritos"] == []


@hay_osascript
@pytest.mark.parametrize("modo", ["otra-pantalla", "importar-falla"])
def test_js_si_guardo_y_no_se_puede_pintar_va_a_la_pagina_sin_reenviar_el_post(mundo, modo):
    """El POST contestó bien (ya se guardó): no se reenvía (daría «ya estaba»), se va a la MISMA página que
    contestó el servidor, con un enlace."""
    previo = 'paginas["PAG"] = {regiones: ["cuerpo"]};' if modo == "otra-pantalla" else "reemplazos.__importar_falla = true;"
    r = correr(_script_de_la_pagina(mundo), previo + _marcar('responder(0, "PAG", {url: %r});' % URL_FINAL))
    assert len(r["pedidos"]) == 1 and r["salidas"] == [["ir", URL_FINAL]]


@hay_osascript
def test_js_un_segundo_envio_del_mismo_formulario_no_manda_otro_post(mundo):
    r = correr(_script_de_la_pagina(mundo), 'var f = formulario("/proyectos/tarea/10/hecha", "post", []); f.clases = ["marcar"];'
                                            ' enviar(f); __e = enviar(f);')
    assert len(r["pedidos"]) == 1 and r["prevenido"] is True
