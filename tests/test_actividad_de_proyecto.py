"""La Actividad del proyecto, SIN NOMBRES (parte 7 del diseño «la página de un proyecto, completa»,
8-oct-2026).

Qué se vigila, por la ruta REAL, la plantilla REAL, `crud` REAL y SQL que se ejecuta de verdad (SQLite
con el `CREATE TABLE` de `db/schema.sql` y las huellas que dejan las acciones de verdad):

  1. la frase de cada combinación de tabla y acción que el código escribe —la lista sale de recorrer
     el código, no de una lista tecleada—, con textos FIJOS esperados;
  2. una combinación que nadie declaró sale con la frase general, no se esconde;
  3. NINGUNA frase lleva un nombre de persona ni un número de chat, tampoco en comentarios ni en
     notas, ni con valores inventados;
  4. solo las huellas de ESTE proyecto: nada de otra tarea, ni de una tarea suelta, ni de otro
     proyecto; la Actividad y «Último movimiento» salen de la MISMA consulta;
  5. los avisos automáticos (`ACCIONES_AUTOMATICAS`) no salen;
  6. el orden (lo más nuevo primero) y el tope de renglones (`TOPE_ACTIVIDAD`), por proyecto;
  7. lo ve todo el que ve el proyecto, también la sesión de solo ver, y no hay ningún control;
  8. el camino de producción: las acciones de verdad dejan las frases que se esperan, escapadas.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. El `row_number() OVER (PARTITION BY …)` corre en SQLite (3.50.4);
    que Postgres ordene dos huellas del MISMO instante igual que SQLite (por `id`) no se ejerció.
  · SQLite guarda los `timestamptz` como texto: el orden por fecha de renglones escritos con zonas
    distintas es una imitación declarada (`tests/test_grupo_ia.py::_fila`). En estas pruebas las
    huellas se siembran con una sola zona (o empatan, y manda el `id`).
  · No es un navegador: cómo se ve el bloque (alto, teléfono) no se ejerce.

Correr:  python3 -m pytest tests/test_actividad_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import asyncio
import re

import pytest

import test_grupo_ia as g
import test_pagina_proyectos as tp
from test_pagina_proyectos import (CREADO, HOY, _dia, gente, mundo, ver,  # noqa: F401
                                   ver_r)
from _navegador import Navegador
import acciones.crud as crud
import config
import db.db as db
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO
ROSI = 700100001
TITULO = "Grabar voces"


# ═══════════════════════════════════════════════════════════════════════
# Ayudas
# ═══════════════════════════════════════════════════════════════════════

def _cliente(chat="dueno") -> Navegador:
    c = Navegador(panel.app)
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def post(ruta, campos=None, chat="dueno"):
    return _cliente(chat).post(ruta, data=campos or {}, follow_redirects=False)


def correr(coro):
    """Corre una corrutina de `db`/`crud` contra el mundo de prueba (pool de SQLite)."""
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


def _bloque(html: str) -> str:
    """El bloque «Actividad» (desde su `id` hasta el cierre de su `section`), o ''."""
    if 'id="actividad-del-proyecto"' not in html:
        return ""
    return html.split('id="actividad-del-proyecto"', 1)[1].split("</section>", 1)[0]


def _renglones(html: str) -> list[tuple[str, str]]:
    """`[(frase, cuándo)]` de cada renglón del bloque, en el orden en que salen."""
    return re.findall(r"<li><b>(.*?)</b><span>(.*?)</span></li>", _bloque(html), re.S)


def _frases(html: str) -> list[str]:
    return [f for f, _ in _renglones(html)]


def _ficha(nombre):
    """Un lector de fichas de Noco, de mentira, que devuelve ese nombre."""
    async def _leer(noco_id):
        return {"id": noco_id, "nombre": nombre}
    return _leer


# ═══════════════════════════════════════════════════════════════════════
# El bloque
# ═══════════════════════════════════════════════════════════════════════

def test_el_bloque_sale_con_su_titulo_aunque_no_haya_nada(mundo):
    mundo.proyecto(1, "Sin historia", area="CDS")
    html = ver(mundo, p=1)
    b = _bloque(html)
    assert "<h2>Actividad</h2>" in b
    assert "Todavía no hay movimiento." in b and _renglones(html) == []


def test_el_bloque_sale_en_un_proyecto_cerrado(mundo):
    """Cerrado no esconde el historial: lo que pasó, pasó."""
    mundo.proyecto(1, "Uno", area="CDS", estado="cerrado")
    mundo.huella("editar", "proyectos", 1, _dia(-1))
    assert _frases(ver(mundo, p=1)) == ["Se cambió el proyecto."]


# ═══════════════════════════════════════════════════════════════════════
# 1 y 2. Una frase por combinación; la que nadie declaró, la general
# ═══════════════════════════════════════════════════════════════════════

# LOS TEXTOS, FIJOS Y ESCRITOS A MANO: no se comparan contra la misma función que vigilan.
_FIJAS = {
    ("proyectos", "crear"): "Se creó el proyecto.",
    ("proyectos", "borrar"): "Se mandó el proyecto a la papelera.",
    ("proyectos", "deshacer"): "Se deshizo un cambio del proyecto.",
    ("tareas", "crear"): f"Se agregó la tarea «{TITULO}».",
    ("tareas", "borrar"): f"Se borró la tarea «{TITULO}».",
    ("tareas", "deshacer"): f"Se deshizo un cambio en la tarea «{TITULO}».",
    ("comentarios_tarea", "crear"): f"Nuevo comentario en «{TITULO}».",
    ("comentarios_tarea", "editar"): f"Se editó un comentario en «{TITULO}».",
    ("comentarios_tarea", "borrar"): f"Se borró un comentario en «{TITULO}».",
    ("comentarios_tarea", "deshacer"): f"Se deshizo un cambio en un comentario de «{TITULO}».",
    ("notas", "crear"): "Nueva nota en el proyecto.",
    ("notas", "editar"): "Se editó una nota.",
    ("notas", "borrar"): "Se borró una nota.",
    ("notas", "deshacer"): "Se deshizo un cambio en una nota.",
}

# Las dos que miran QUÉ cambió. La huella trae a veces la fila ENTERA de después (`crud.editar`) y a
# veces SOLO lo que cambió (las funciones de `db`): las dos formas tienen que decir lo mismo, y lo que
# NO cambió no se puede afirmar (el `cliente` está en todas las filas de un proyecto).
_ROWS = {"id": 1, "nombre": "Uno", "cliente_nombre": None, "cliente_noco_id": None,
         "responsable_chat_id": None, "estado": "activo", "area": "CDS", "descripcion": None,
         "carpeta": None, "inicio": None, "entrega": None, "termina_cuando": None}
_CAMBIOS = [
    (("proyectos", "editar"), {"estado": "activo"}, {"estado": "cerrado"}, "Se cerró el proyecto."),
    (("proyectos", "editar"), {"estado": "cerrado"}, {"estado": "activo"}, "Se reabrió el proyecto."),
    (("proyectos", "editar"), {"estado": "activo"}, {"estado": "pausado"},
     "Se cambió el estado del proyecto."),
    (("proyectos", "editar"), {"cliente_noco_id": None, "cliente_nombre": None},
     {"cliente_noco_id": 7, "cliente_nombre": "Banda"}, "Se cambió el cliente del proyecto."),
    (("proyectos", "editar"), {"responsable_chat_id": None}, {"responsable_chat_id": DUENO},
     "Se cambió el responsable del proyecto."),
    (("proyectos", "editar"), {"nombre": "Uno"}, {"nombre": "Otro"},
     "Se le cambió el nombre al proyecto."),
    (("proyectos", "editar"), {"descripcion": None}, {"descripcion": "texto"},
     "Se escribió de qué se trata el proyecto."),
    (("proyectos", "editar"), {"carpeta": None}, {"carpeta": "https://x.test"},
     "Se cambió la carpeta del proyecto."),
    (("proyectos", "editar"), {"area": "CDS"}, {"area": "ACD"}, "Se cambió el proyecto de grupo."),
    (("proyectos", "editar"), {"entrega": None}, {"entrega": "2026-11-01"},
     "Se cambiaron las fechas del proyecto."),
    # SOLO cambió el nombre, pero la huella trae la fila entera (con su cliente y su estado): la frase
    # no puede decir que cambió el cliente ni que se cerró.
    (("proyectos", "editar"), {**_ROWS, "cliente_nombre": "Banda", "estado": "cerrado"},
     {**_ROWS, "nombre": "Otro", "cliente_nombre": "Banda", "estado": "cerrado"},
     "Se le cambió el nombre al proyecto."),
    (("proyectos", "editar"), {**_ROWS, "cliente_nombre": "Banda"}, {**_ROWS, "cliente_nombre": "Banda"},
     "Se cambió el proyecto."),
    (("tareas", "editar"), {"estado": "pendiente"}, {"estado": "hecha"}, f"Se marcó hecha «{TITULO}»."),
    (("tareas", "editar"), {"estado": "hecha"},
     {"estado": "pendiente", "completado_en": None}, f"Se reabrió «{TITULO}»."),
    (("tareas", "editar"), {"estado": "pendiente"}, {"estado": "descartado"},
     f"Se le cambió el estado a «{TITULO}»."),
    (("tareas", "editar"), {"responsable_chat_id": None}, {"responsable_chat_id": ROSI},
     f"Se cambió el responsable de «{TITULO}»."),
    (("tareas", "editar"), {"titulo": TITULO}, {"titulo": "Otro título"},
     "Se le cambió el nombre a una tarea."),
    (("tareas", "editar"), {"vence_en": None}, {"vence_en": "2026-11-01"},
     f"Se le cambió la fecha a «{TITULO}»."),
    (("tareas", "editar"), {"proyecto_id": 1}, {"proyecto_id": 2},
     f"Se movió «{TITULO}» a otro proyecto."),
    (("tareas", "editar"), {"area": "CDS"}, {"area": "ACD"}, f"Se cambió «{TITULO}» de grupo."),
    # La fila entera de una tarea con el estado cambiado a hecha: se dice que se marcó hecha.
    (("tareas", "editar"), {"estado": "pendiente", "vence_en": None},
     {"estado": "hecha", "vence_en": None, "completado_en": "2026-10-02 12:00:00+00:00"},
     f"Se marcó hecha «{TITULO}»."),
    (("tareas", "editar"), {"estado": "pendiente"}, {"estado": "pendiente", "detalle": "nuevo"},
     f"Se cambió «{TITULO}»."),
]


@pytest.mark.parametrize("par,esperada", sorted(_FIJAS.items()))
def test_cada_combinacion_declarada_tiene_su_frase_fija(par, esperada):
    assert db.frase_de_actividad(*par, titulo=TITULO) == esperada


@pytest.mark.parametrize("par,antes,despues,esperada", _CAMBIOS,
                         ids=[f"{p[0]}-{p[1]}-{i}" for i, (p, _, _, _) in enumerate(_CAMBIOS)])
def test_lo_que_cambio_se_dice_por_su_papel_nunca_por_su_nombre(par, antes, despues, esperada):
    assert db.frase_de_actividad(*par, antes=antes, despues=despues, titulo=TITULO) == esperada


def test_sin_lo_que_cambio_o_sin_titulo_la_frase_sigue_leyendose():
    """Una huella sin `despues` (vieja, o la de un `deshacer`) o de una tarea que ya no está no deja
    la frase a medias; y sin `antes` con qué comparar NO se afirma qué cambió."""
    assert db.frase_de_actividad("tareas", "crear") == "Se agregó una tarea."
    assert db.frase_de_actividad("tareas", "borrar", titulo="") == "Se borró una tarea."
    assert db.frase_de_actividad("tareas", "editar", antes={"estado": "pendiente"},
                                 despues={"estado": "hecha"}) == "Se marcó hecha una tarea."
    assert db.frase_de_actividad("tareas", "editar") == "Se cambió una tarea."
    assert db.frase_de_actividad("proyectos", "editar") == "Se cambió el proyecto."
    assert db.frase_de_actividad("comentarios_tarea", "crear", titulo="   ") == "Nuevo comentario en una tarea."
    assert "«»" not in db.frase_de_actividad("tareas", "editar")
    # SIN `antes` no se afirma qué cambió: la fila entera no dice qué columna se tocó.
    assert db.frase_de_actividad("proyectos", "editar", despues={"estado": "activo"}) == "Se cambió el proyecto."
    assert db.frase_de_actividad("tareas", "editar", despues={"estado": "hecha"}) == "Se cambió una tarea."
    assert db.frase_de_actividad("proyectos", "editar", antes={}, despues={}) == "Se cambió el proyecto."


def test_una_combinacion_que_nadie_declaro_sale_con_la_frase_general():
    """EL CUBO ESTRICTO ES ALCANZABLE: se le dan entradas que la guarda no sabe clasificar."""
    otra = db.ACTIVIDAD_OTRA
    assert db.frase_de_actividad("eventos", "editar") == otra       # una tabla que no llega a un proyecto
    assert db.frase_de_actividad("tareas", "accion-que-nadie-declaro") == otra
    assert db.frase_de_actividad("tabla-inventada", "crear") == otra
    assert db.frase_de_actividad(None, None) == otra
    assert db.frase_de_actividad("tareas", 7) == otra
    assert otra == "Hubo un cambio en el proyecto."


def test_la_frase_general_no_se_usa_para_ninguna_combinacion_declarada():
    for par in db.COMBINACIONES_DE_ACTIVIDAD:
        assert db.frase_de_actividad(*par, antes={"estado": "pendiente"},
                                     despues={"estado": "hecha"}, titulo=TITULO) != db.ACTIVIDAD_OTRA


# ═══════════════════════════════════════════════════════════════════════
# La lista de combinaciones SALE DEL CÓDIGO
# ═══════════════════════════════════════════════════════════════════════

def _funcion(nombre: str):
    fuente = (g._ROOT / "db" / "db.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    return next(n for n in ast.walk(arbol)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == nombre)


def _sql_de_la_actividad() -> str:
    """El texto de la consulta de la Actividad, sacado del árbol (no de un `split` del archivo)."""
    for n in ast.walk(_funcion("pagina_de_proyectos")):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "row_number() OVER" in n.value):
            return n.value
    raise AssertionError("no encontré la consulta de la actividad en pagina_de_proyectos")


def _tablas_de_la_actividad() -> set[str]:
    """Las tablas del `UNION` de esa consulta, tal como están escritas ahí."""
    return set(re.findall(r"l\.tabla = '(\w+)'", _sql_de_la_actividad()))


def test_las_tablas_salen_de_la_consulta_y_cubren_lo_que_el_codigo_escribe():
    tablas = _tablas_de_la_actividad()
    assert tablas == {"proyectos", "tareas", "comentarios_tarea", "notas"}
    declaradas = {t for t, _ in db.COMBINACIONES_DE_ACTIVIDAD}
    assert declaradas == tablas, (
        "una tabla que llega a un proyecto sin frase (o al revés): "
        f"{sorted(declaradas ^ tablas)}")


def test_las_personas_gastos_citas_y_micro_pasos_no_llegan_a_la_actividad():
    """Lo que NO entra, con su motivo: las personas no cuentan como movimiento (diseño §5.4 y su
    prueba), y los gastos, las citas y los micro-pasos no se ven en esta página."""
    assert "participantes" not in _sql_de_la_actividad()
    assert "movimientos" not in _sql_de_la_actividad()
    assert "eventos" not in _sql_de_la_actividad()
    assert "micro_pasos" not in _sql_de_la_actividad()


def test_una_frase_por_cada_accion_que_mueve_y_solo_por_esas():
    acciones = {a for _, a in db.COMBINACIONES_DE_ACTIVIDAD}
    assert acciones == set(db.ACCIONES_QUE_MUEVEN)
    assert not acciones & set(db.ACCIONES_AUTOMATICAS)


def test_los_huecos_del_sql_son_los_de_las_acciones_automaticas():
    """El `NOT IN (%s, %s)` va escrito a mano: si mañana `ACCIONES_AUTOMATICAS` cambia de largo, esto
    se pone rojo ANTES de que la consulta reviente con «too many parameters»."""
    sql = _sql_de_la_actividad()
    assert "y.accion NOT IN (%s, %s)" in sql
    assert sql.count("%s") == len(db.ACCIONES_AUTOMATICAS) + 1     # + el tope


_ESCRITURA = re.compile(r"INSERT\s+INTO\s+log_acciones[\s\S]*?VALUES\s*\(\s*(?:'[^']*'|%s)\s*,"
                        r"\s*(?:'([^']*)'|(%s))\s*,\s*(?:'([^']*)'|(%s))", re.I)


def _pares_que_escribe_el_codigo() -> tuple[set, list]:
    """(pares `(tabla, acción)` LITERALES que el código escribe en `log_acciones`, sitios donde la
    tabla o la acción van por variable). Sale de recorrer los `.py` del repo, no de una lista.

    Ve: los `INSERT INTO log_acciones … VALUES (actor, 'accion', 'tabla', …)` escritos a mano y las
    llamadas `_registrar(accion=…, tabla=…)` con las dos cosas literales. FRONTERA: los escritores
    genéricos (`crud.editar`, `crud.borrar`, `crud.deshacer`, `crear_desde_interpretacion`) escriben
    la acción fija y la tabla que les pasa el llamador: sus pares salen del producto cartesiano
    (acción del escritor × tabla del `UNION`), que es lo que declara `COMBINACIONES_DE_ACTIVIDAD`."""
    literales, variables = set(), []
    for archivo in g._archivos_de_texto():
        if archivo.suffix != ".py":
            continue
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call):
                kw = {k.arg: k.value for k in n.keywords}
                acc, tab = kw.get("accion"), kw.get("tabla")
                if acc is None or tab is None:
                    continue
                if isinstance(acc, ast.Constant) and isinstance(tab, ast.Constant):
                    literales.add((tab.value, acc.value))
                else:
                    variables.append(f"{archivo.name}:{n.lineno}")
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                for m in _ESCRITURA.finditer(n.value):
                    if m.group(1) and m.group(3):
                        literales.add((m.group(3), m.group(1)))
                    elif m.group(2) or m.group(4):
                        variables.append(f"{archivo.name}:{n.lineno}")
    return literales, variables


def test_toda_combinacion_que_el_codigo_escribe_sobre_un_proyecto_tiene_su_frase():
    literales, _ = _pares_que_escribe_el_codigo()
    tablas = _tablas_de_la_actividad()
    del_proyecto = {p for p in literales if p[0] in tablas}
    assert del_proyecto, "la sonda no vio ni un escritor: estaría verde sin mirar"
    # Lo que el código puede escribir sobre algo que cuelga de un proyecto: las cuatro acciones que
    # mueven (con su frase) o los avisos automáticos (que NO salen, a propósito).
    esperados = set(db.COMBINACIONES_DE_ACTIVIDAD) | {
        (t, a) for t in tablas for a in db.ACCIONES_AUTOMATICAS}
    faltan = del_proyecto - esperados
    assert not faltan, f"el código escribe combinaciones sin frase: {sorted(faltan)}"


def test_la_sonda_ve_los_pares_escritos_a_mano():
    """La sonda mira lo que dice mirar: un par inventado, y los huecos por variable."""
    m = _ESCRITURA.search("INSERT INTO log_acciones (actor, accion, tabla) "
                          "VALUES ('panel', 'inventada', 'tareas', 1)")
    assert (m.group(3), m.group(1)) == ("tareas", "inventada")
    m2 = _ESCRITURA.search("INSERT INTO log_acciones (actor, accion, tabla) VALUES (%s, %s, %s)")
    assert m2.group(2) and m2.group(4) and not m2.group(1) and not m2.group(3)
    # Y la lista de pares, de verdad: cada tabla del proyecto tiene la acción `crear` escrita a mano
    # en algún sitio (comentarios y proyectos incluidos), y `participantes` NO entra a la lista.
    literales, _ = _pares_que_escribe_el_codigo()
    assert ("proyectos", "crear") in literales
    assert ("comentarios_tarea", "borrar") in literales
    assert not {p for p in literales if p[0] == "participantes" and p[0] in _tablas_de_la_actividad()}


# ═══════════════════════════════════════════════════════════════════════
# 3. SIN NOMBRES: la garantía central, mirando lo que SALE
# ═══════════════════════════════════════════════════════════════════════

# Todo lo que puede ser un nombre de persona en estos mundos.
_NOMBRES = ("Persona Uno", "Persona Dos", "Fulano de Tal", "Mengana de Noco",
            "Doña Perengana", "Banda Fulana", "Juan Pérez")


async def test_ninguna_frase_lleva_un_nombre_de_persona_ni_un_numero_de_chat(mundo):
    """EL MUNDO CON NOMBRES EN TODAS PARTE, con las acciones de verdad por las rutas."""
    # `gente` (fixture de la suite) nombra a los chats: Persona Uno es el dueño del panel.
    mundo.proyecto(1, "Disco con nombres", area="CDS", cliente="Banda Fulana", responsable=DUENO)
    mundo.tarea(10, TITULO, proyecto=1, responsable=DUENO)
    mundo.comentario(100, 10, DUENO, "Fulano de Tal dijo que sí; Doña Perengana no")
    # Comentar (el nombre va DENTRO del texto), escribir una nota y agregar una persona de Noco.
    assert post("/proyectos/tarea/10/comentar", {"texto": "Lo dijo Fulano de Tal"}).status_code == 303
    assert post("/proyectos/1/notas", {"texto": "Hablar con Mengana de Noco"}).status_code == 303
    persona = await db.agregar_participante(("proyecto", 1), 501, "productor", DUENO,
                                            leer_persona=_ficha("Mengana de Noco"))
    # Cambiar el cliente (su nombre de Noco es un nombre de persona), el nombre del proyecto y el
    # responsable de la tarea (por la ruta del panel).
    await db.poner_cliente(1, 7, leer_persona=_ficha("Banda Fulana"))
    assert post("/proyectos/1/nombre", {"nombre": "Doña Perengana"}).status_code == 303
    assert post("/proyectos/tarea/10/responsable", {"responsable": "Persona Dos"}).status_code == 303
    assert post("/proyectos/1/estado", {"estado": "cerrado"}).status_code == 303
    html = ver(mundo, p=1)
    b = _bloque(html)
    assert _renglones(html), "el bloque salió vacío: la prueba no estaría midiendo nada"
    for nombre in _NOMBRES:
        assert nombre not in b, f"la Actividad dice el nombre {nombre!r}"
    for numero in (str(DUENO), str(ROSI), "700100999"):
        assert numero not in b, f"la Actividad dice el número de chat {numero!r}"
    assert str(501) not in b, "la Actividad dice el Id de la ficha de Noco"
    assert str(persona["id"]) not in b
    assert TITULO in b, "el título de la tarea SÍ sale: lo escribe la casa"


def test_la_actividad_no_lleva_nombres_con_valores_inventados(mundo):
    """Valores inventados en TODAS las claves que la frase mira: ninguna se imprime."""
    mundo.proyecto(1, "Uno", area="CDS")
    filas = [
        {"pid": 1, "tabla": "proyectos", "accion": "editar", "registro_id": 1, "ts": CREADO,
         "antes": {"cliente_nombre": "Doña Perengana", "responsable_chat_id": 42},
         "despues": {"cliente_nombre": "Banda Fulana", "responsable_chat_id": 43}, "titulo": None},
        {"pid": 1, "tabla": "proyectos", "accion": "editar", "registro_id": 1, "ts": CREADO,
         "antes": {"nombre": "Uno", "cliente_nombre": "Banda Fulana"},
         "despues": {"nombre": "Juan Pérez", "cliente_nombre": "Banda Fulana"}, "titulo": None},
        {"pid": 1, "tabla": "tareas", "accion": "editar", "registro_id": 10, "ts": CREADO,
         "antes": {"responsable_chat_id": 41}, "despues": {"responsable_chat_id": 44},
         "titulo": TITULO},
        {"pid": 1, "tabla": "notas", "accion": "crear", "registro_id": 700, "ts": CREADO,
         "antes": None, "despues": {"contenido": "Mengana de Noco"}, "titulo": None},
    ]
    modelo = db.armar_pagina(
        list(tp.AREAS),
        [{"id": 1, "nombre": "Uno", "area": "CDS", "creado_en": CREADO, "descripcion": None,
          "estado": "activo", "responsable_chat_id": None, "cliente_nombre": None}],
        [], [], [], {}, HOY, actividad={1: filas})
    texto = " ".join(a["frase"] for a in modelo["proyectos"][1]["actividad"])
    for nombre in _NOMBRES:
        assert nombre not in texto
    for numero in ("42", "43", "44", "700"):
        assert numero not in texto
    assert TITULO in texto        # el TÍTULO de la tarea es lo único que puede salir


def test_la_frase_no_mira_el_actor_de_la_huella(mundo):
    """El `actor` no viaja en la consulta: una huella firmada con un nombre no puede filtrarlo."""
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, ts, despues) "
        "VALUES (?, 'crear', 'proyectos', 1, ?, NULL)", ("Persona Uno", CREADO.isoformat()))
    b = _bloque(ver(mundo, p=1))
    assert "Se creó el proyecto." in b
    assert "Persona Uno" not in b


# ═══════════════════════════════════════════════════════════════════════
# 4. Solo las de ESTE proyecto; una sola fuente con «Último movimiento»
# ═══════════════════════════════════════════════════════════════════════

def _sembrar_dos(m) -> None:
    m.proyecto(1, "Uno", area="CDS", responsable=DUENO)
    m.proyecto(2, "Dos", area="CDS", responsable=DUENO)
    m.tarea(10, "De Uno", proyecto=1)
    m.tarea(20, "De Dos", proyecto=2)
    m.tarea(30, "Suelta", area="CDS")
    m.comentario(100, 10, DUENO, "un comentario")
    m.comentario(200, 20, DUENO, "otro comentario")
    m.comentario(300, 30, DUENO, "de una suelta")


def test_nada_de_otro_proyecto_ni_de_una_tarea_suelta(mundo):
    _sembrar_dos(mundo)
    for accion, tabla, rid in [("crear", "tareas", 10), ("crear", "tareas", 20),
                               ("crear", "tareas", 30), ("crear", "comentarios_tarea", 100),
                               ("crear", "comentarios_tarea", 200), ("crear", "comentarios_tarea", 300),
                               ("crear", "proyectos", 2)]:
        mundo.huella(accion, tabla, rid, _dia(-1))
    # (Todo el mismo día: manda el `id`, el más nuevo primero. Se mira QUIÉN entra, no el orden.)
    assert sorted(_frases(ver(mundo, p=1))) == sorted(["Se agregó la tarea «De Uno».",
                                                       "Nuevo comentario en «De Uno»."])
    assert sorted(_frases(ver(mundo, p=2))) == sorted(["Se agregó la tarea «De Dos».",
                                                       "Nuevo comentario en «De Dos».",
                                                       "Se creó el proyecto."])
    # Lo de la tarea SUELTA («Suelta») no aparece en el proyecto de nadie.
    assert not any("Suelta" in f for p in (1, 2) for f in _frases(ver(mundo, p=p)))


def test_una_tarea_que_se_mueve_de_proyecto_lleva_sus_huellas_al_nuevo(mundo):
    """El dato que decide es `tareas.proyecto_id` de AHORA: así lo dice la consulta, y por eso la
    Actividad y «Último movimiento» cuentan lo mismo."""
    _sembrar_dos(mundo)
    mundo.huella("crear", "tareas", 10, _dia(-1))
    assert _frases(ver(mundo, p=1)) == ["Se agregó la tarea «De Uno»."]
    assert _frases(ver(mundo, p=2)) == []
    correr(crud.editar("tareas", 10, {"proyecto_id": 2}, motivo="sonda", actor="panel"))
    assert "Se agregó la tarea «De Uno»." in _frases(ver(mundo, p=2))
    assert _frases(ver(mundo, p=1)) == []


def test_la_actividad_y_el_ultimo_movimiento_cuentan_lo_mismo(mundo):
    """UNA SOLA FUENTE: la fecha más nueva de la Actividad es el «último movimiento» del proyecto."""
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, TITULO, proyecto=1)
    mundo.huella("crear", "proyectos", 1, _dia(-2))
    mundo.huella("crear", "tareas", 10, _dia(-1))
    mundo.huella("avisar", "tareas", 10, _dia(0))          # automática: no cuenta, aunque sea la más nueva
    m = correr(db.pagina_de_proyectos())["proyectos"][1]
    assert m["ultimo"] == _dia(-1) and m["ultimo"] != _dia(0)
    assert max(a["cuando"] for a in m["actividad"]) == m["ultimo"]
    assert sorted(a["cuando"] for a in m["actividad"]) == [_dia(-2), _dia(-1)]


# ═══════════════════════════════════════════════════════════════════════
# 5 y 6. Avisos automáticos, orden y tope
# ═══════════════════════════════════════════════════════════════════════

def test_los_avisos_automaticos_no_salen(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, TITULO, proyecto=1)
    mundo.huella("avisar", "tareas", 10, _dia(0))
    mundo.huella("aviso_atraso_code", "tareas", 10, _dia(0))
    mundo.huella("crear", "tareas", 10, _dia(-1))
    assert _frases(ver(mundo, p=1)) == [f"Se agregó la tarea «{TITULO}»."]


def test_el_orden_es_lo_mas_nuevo_primero(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    for i, titulo in ((10, "Primera"), (11, "Segunda"), (12, "Tercera")):
        mundo.tarea(i, titulo, proyecto=1)
    mundo.huella("crear", "tareas", 10, _dia(-3))
    mundo.huella("crear", "tareas", 11, _dia(-2))
    mundo.huella("crear", "tareas", 12, _dia(-1))
    assert _frases(ver(mundo, p=1)) == ["Se agregó la tarea «Tercera».",
                                        "Se agregó la tarea «Segunda».",
                                        "Se agregó la tarea «Primera»."]


def test_el_tope_es_por_proyecto_y_no_crece_con_el_historial(mundo):
    """Con más huellas que el tope, se pintan `TOPE_ACTIVIDAD`, las más nuevas — y cada proyecto tiene
    las suyas (no es un tope global): la consulta trae solo lo que se pinta."""
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.proyecto(2, "Dos", area="CDS")
    for pid in (1, 2):
        mundo.tarea(pid * 100, f"De {pid}", proyecto=pid)
        for i in range(db.TOPE_ACTIVIDAD + 5):
            mundo.huella("editar", "tareas", pid * 100, _dia(-i - 1))
    assert len(_renglones(ver(mundo, p=1))) == db.TOPE_ACTIVIDAD
    assert len(_renglones(ver(mundo, p=2))) == db.TOPE_ACTIVIDAD
    assert db.TOPE_ACTIVIDAD <= 30, "un tope así ya no es «los últimos renglones»"


def test_la_fecha_sale_en_hora_de_santo_domingo(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, TITULO, proyecto=1)
    # 20:30 en Santo Domingo es el día siguiente a las 00:30 UTC: se pinta el día de aquí.
    mundo.huella("crear", "tareas", 10, _dia(-1, 20, 30))
    (_, cuando), = _renglones(ver(mundo, p=1))
    assert cuando == "09/10/2026 20:30", cuando


# ═══════════════════════════════════════════════════════════════════════
# 7. Quién lo ve
# ═══════════════════════════════════════════════════════════════════════

def test_solo_ver_ve_la_actividad_y_no_hay_ningun_control(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, TITULO, proyecto=1)
    mundo.huella("crear", "tareas", 10, _dia(-1))
    r = _cliente("ver").get("/proyectos", params={"p": 1})
    assert r.status_code == 200
    b = _bloque(r.text)
    assert f"Se agregó la tarea «{TITULO}»." in b
    for prohibido in ("<form", "<input", "<button", "<script", "fetch("):
        assert prohibido not in b, prohibido


# ═══════════════════════════════════════════════════════════════════════
# 8. El camino de producción: acciones de verdad, frases de verdad
# ═══════════════════════════════════════════════════════════════════════

def test_las_acciones_de_verdad_dejan_las_frases_que_se_esperan(mundo):
    mundo.proyecto(1, "Uno", area="CDS", responsable=DUENO)
    assert post("/proyectos/1/tareas", {"titulo": TITULO}).status_code == 303
    tid = mundo.con.execute("SELECT id FROM tareas WHERE titulo = ?", (TITULO,)).fetchone()[0]
    assert post(f"/proyectos/tarea/{tid}/hecha", {}).status_code == 303
    assert post(f"/proyectos/tarea/{tid}/reabrir", {}).status_code == 303
    assert post(f"/proyectos/tarea/{tid}/comentar", {"texto": "hola"}).status_code == 303
    assert post("/proyectos/1/notas", {"texto": "una nota"}).status_code == 303
    assert post("/proyectos/1/nombre", {"nombre": "Uno bis"}).status_code == 303
    assert post("/proyectos/1/estado", {"estado": "cerrado"}).status_code == 303
    assert _frases(ver(mundo, p=1)) == [
        "Se cerró el proyecto.",
        "Se le cambió el nombre al proyecto.",
        "Nueva nota en el proyecto.",
        f"Nuevo comentario en «{TITULO}».",
        f"Se reabrió «{TITULO}».",
        f"Se marcó hecha «{TITULO}».",
        f"Se agregó la tarea «{TITULO}».",
    ]


def test_el_titulo_de_la_tarea_sale_escapado(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, "<img src=x onerror=alert(1)>", proyecto=1)
    mundo.huella("crear", "tareas", 10, _dia(-1))
    b = _bloque(ver(mundo, p=1))
    assert "<img src=x" not in b
    assert "&lt;img src=x onerror=alert(1)&gt;" in b


def test_deshacer_una_creacion_dice_lo_mismo_que_deshacer_un_borrado(mundo):
    """`deshacer` sirve para las DOS cosas —devolver algo borrado, o deshacer una creación (que la manda
    a la papelera)— y su huella no dice cuál: la frase no puede prometer que algo «volvió»."""
    mundo.proyecto(1, "Uno", area="CDS", responsable=DUENO)
    assert post("/proyectos/1/notas", {"texto": "una nota"}).status_code == 303
    nid = mundo.con.execute("SELECT id FROM notas").fetchone()[0]
    crear = mundo.con.execute(
        "SELECT id FROM log_acciones WHERE tabla = 'notas' AND accion = 'crear'").fetchone()[0]
    correr(crud.deshacer(crear))
    borrada = mundo.con.execute("SELECT borrado_en FROM notas WHERE id = ?", (nid,)).fetchone()[0]
    assert borrada is not None, "deshacer una creación tiene que mandar la nota a la papelera"
    assert _frases(ver(mundo, p=1)) == ["Se deshizo un cambio en una nota.",
                                        "Nueva nota en el proyecto."]


def test_borrar_una_nota_deja_su_frase_y_un_proyecto_en_la_papelera_no_sale(mundo):
    mundo.proyecto(1, "Uno", area="CDS", responsable=DUENO)
    assert post("/proyectos/1/notas", {"texto": "una nota"}).status_code == 303
    nid = mundo.con.execute("SELECT id FROM notas").fetchone()[0]
    assert post(f"/proyectos/1/notas/{nid}/borrar", {}).status_code == 303
    assert _frases(ver(mundo, p=1)) == ["Se borró una nota.", "Nueva nota en el proyecto."]
    # Borrar el proyecto POR LA MISMA PUERTA que la ruta (`crud.borrar`): el «Sí» del panel trae las
    # cuentas de la pregunta, y sin ellas la ruta vuelve a preguntar (no borra).
    correr(crud.borrar("proyectos", 1, "sonda", actor="panel"))
    assert 'id="actividad-del-proyecto"' not in ver(mundo)
