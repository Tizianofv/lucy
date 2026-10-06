"""Agregar y quitar grupos en Proyectos (Tiziano, 6-oct-2026: «quiero tambien
poder agregar y quitar grupos»).

QUÉ ES UN GRUPO (medido el 6-oct-2026): una fila de `areas` (clave = el nombre
que se ve, color, orden). Lo nombran `proyectos.area` y `tareas.area` con llave
foránea SIN `ON DELETE` (`proyectos_area_fkey`, `tareas_area_fkey`, «NO ACTION»,
en producción), así que «vacío» es: NINGUNA fila de esas dos tablas lo nombra, de
cualquier estado y también las de la papelera.

QUÉ VIGILA ESTE ARCHIVO, con el SQL ejecutándose de verdad en SQLite (las llaves
foráneas ENCENDIDAS; `FILTER` y `RETURNING` los acepta SQLite igual que Postgres)
y las rutas y la página reales:
  · crear: nombre vacío/largo/con control/«Sin grupo»/repetido sin distinguir
    mayúsculas, tildes ni espacios no se crea; el bueno queda al final, con color
    que pone el sistema, leído de la base;
  · quitar: solo un grupo vacío; uno con cosas (de cada clase) no se quita y dice
    cuántas; el fijo (`db.AREA_TECNICA`) tampoco; nada de lo demás se mueve;
  · la página: pregunta antes de quitar, avisos de «creado/quitado» solo si es
    verdad (salen de la base, no de la dirección), textos exactos;
  · `solo_ver` y quien no entró no ven los controles ni llaman a las rutas;
  · ningún código fuera de `tests/` da por fijo el nombre de un grupo salvo el
    técnico.
LO FINGIDO, declarado: (1) `pg_advisory_xact_lock` y `hashtextextended` son
funciones vacías (SQLite no tiene bloqueos de Postgres): se prueba la SECUENCIA
(la segunda petición ve lo que dejó la primera), no que Postgres las ponga en fila;
(2) «un pedido le mete algo al grupo entre la lectura y el borrado» se imita con un
gancho que borra SIN la guarda del `NOT EXISTS` (la lectura vieja): lo que se
comprueba es que la llave foránea lo rechaza y que `quitar_grupo` lo cuenta como
«tiene cosas», no la carrera de verdad entre dos conexiones."""
from __future__ import annotations

import ast
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

import test_grupo_ia as g
from test_pagina_proyectos import _cliente, _dia, gente, mundo, ver  # noqa: F401
from test_proyectos_solo_ver import cliente as cliente_de_sesion
from test_grupo_ia import _ROOT
import config
import db.db as db


@pytest.fixture
def base(mundo):
    """El mundo de Proyectos (SQLite con el SQL real) con las llaves foráneas
    encendidas y las dos funciones de bloqueo de Postgres como funciones vacías."""
    mundo.con.create_function("pg_advisory_xact_lock", 1, lambda x: None)
    mundo.con.create_function("hashtextextended", 2, lambda a, b: 0)
    mundo.con.execute("INSERT INTO areas (clave, color, orden) VALUES ('Hogar', '#2f6fb3', 4)")
    mundo.con.execute("INSERT OR IGNORE INTO areas (clave, color, orden) VALUES ('IA', '#8a4a8f', 3)")
    mundo.con.commit()
    mundo.con.execute("PRAGMA foreign_keys = ON")
    return mundo


def _grupos(m) -> list[tuple]:
    return [tuple(r) for r in m.con.execute("SELECT clave, color, orden FROM areas ORDER BY orden, clave")]


def _fotos(m) -> dict:
    return {t: [tuple(r) for r in m.con.execute(f"SELECT id, area FROM {t} ORDER BY id")]
            for t in ("proyectos", "tareas")}


# ── Crear ───────────────────────────────────────────────────────────────────

async def test_crear_agrega_al_final_con_color_del_sistema_y_se_lee_de_la_base(base):
    antes = _grupos(base)
    nuevo = await db.crear_grupo("  Estudio   Dos ")
    assert nuevo["clave"] == "Estudio Dos"
    despues = _grupos(base)
    assert despues[:-1] == antes and despues[-1][0] == "Estudio Dos"
    assert despues[-1][2] == max(a[2] for a in antes) + 1
    assert despues[-1][1] == db.PALETA_DE_GRUPOS[0] and despues[-1][1] not in {a[1] for a in antes}
    assert [a["clave"] for a in await db.areas()][-1] == "Estudio Dos"
    assert (nuevo["clave"], nuevo["color"], nuevo["orden"]) == despues[-1]      # lo devuelto ES lo guardado


async def test_los_colores_no_se_repiten_y_nunca_faltan(base):
    for i in range(len(db.PALETA_DE_GRUPOS) + 4):             # más grupos que colores de la paleta
        await db.crear_grupo(f"G{i}")
    colores = [c for _, c, _ in _grupos(base)]
    assert len(colores) == len(set(colores))
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in colores[4:])


@pytest.mark.parametrize("pedido,clave", [
    ("", "vacio"), ("   ", "vacio"), (None, "vacio"), (5, "vacio"),
    ("x" * (db.LARGO_NOMBRE_GRUPO + 1), "largo"),
    ("con\nsalto", "caracteres"), ("tab\taqui", "caracteres"),
    ("Sin grupo", "reservado"), ("  SIN   GRUPO ", "reservado"), ("sín grupo", "reservado")])
async def test_un_nombre_que_no_vale_no_se_crea_y_dice_por_que(base, pedido, clave):
    antes = _grupos(base)
    with pytest.raises(db.GrupoNoVale) as e:
        await db.crear_grupo(pedido)
    assert e.value.clave == clave
    assert _grupos(base) == antes


@pytest.mark.parametrize("repetido", ["CDS", "cds", " Cds ", "C D S".replace(" ", ""), "hogar", "HOGAR", "  ho gar".replace(" ", "")])
async def test_un_nombre_repetido_sin_distinguir_mayusculas_ni_espacios_no_se_crea(base, repetido):
    antes = _grupos(base)
    with pytest.raises(db.GrupoNoVale) as e:
        await db.crear_grupo(repetido)
    assert e.value.clave == "repetido" and _grupos(base) == antes


async def test_un_nombre_repetido_sin_distinguir_tildes_no_se_crea(base):
    await db.crear_grupo("Café")
    for pedido in ("CAFE", "cafe", "  café ", "CAFÉ", "Café"):        # la última, con tilde combinada
        with pytest.raises(db.GrupoNoVale) as e:
            await db.crear_grupo(pedido)
        assert e.value.clave == "repetido", pedido
    assert [a[0] for a in _grupos(base)].count("Café") == 1


async def test_dos_pedidos_a_la_vez_con_el_mismo_nombre_crean_uno_solo(base):
    import asyncio
    r = await asyncio.gather(db.crear_grupo("Casa"), db.crear_grupo("casa"), return_exceptions=True)
    assert sum(isinstance(x, dict) for x in r) == 1
    assert [type(x).__name__ for x in r if not isinstance(x, dict)] == ["GrupoNoVale"]
    assert len([a for a in _grupos(base) if a[0].lower() == "casa"]) == 1


async def test_si_la_lectura_estaba_vieja_la_llave_primaria_frena_el_nombre_exacto(base, monkeypatch):
    """La lectura de los grupos devuelve lo de antes de que otro pedido crease «Casa»
    (declarado: se imita con un doble de la lectura); el INSERT choca con la llave
    primaria y se dice «repetido», sin dejar dos filas ni un error sin explicar."""
    await db.crear_grupo("Casa")
    original = g._Cur.execute

    async def con_lectura_vieja(self, sql, params=()):
        if sql.startswith("SELECT clave, color, orden FROM areas"):
            self._cur = self.con.execute("SELECT clave, color, orden FROM areas WHERE 0")
            return self
        return await original(self, sql, params)
    monkeypatch.setattr(g._Cur, "execute", con_lectura_vieja)
    with pytest.raises(db.GrupoNoVale) as e:
        await db.crear_grupo("Casa")
    assert e.value.clave == "repetido" and [a[0] for a in _grupos(base)].count("Casa") == 1


# ── Quitar ──────────────────────────────────────────────────────────────────

async def test_quitar_un_grupo_vacio_lo_quita_y_no_mueve_nada_mas(base):
    base.proyecto(1, "Uno", area="CDS")
    base.tarea(10, "suelta", area="ACD")
    base.con.commit()
    antes_g, antes_f = _grupos(base), _fotos(base)
    await db.crear_grupo("Vacío")
    await db.quitar_grupo("Vacío")
    assert _grupos(base) == antes_g and _fotos(base) == antes_f      # nada colgaba, nada cambió de sitio
    assert "Vacío" not in [a["clave"] for a in await db.areas()]


async def test_quitar_uno_del_medio_no_cambia_el_orden_ni_el_color_de_los_otros(base):
    antes = _grupos(base)
    await db.quitar_grupo("ACD")
    assert _grupos(base) == [a for a in antes if a[0] != "ACD"]


# Cada clase de cosa que apunta a un grupo lo hace imposible de quitar, y se cuenta.
CLASES = {
    "proyecto abierto": (lambda m: m.proyecto(1, "P", area="Hogar"), {"proyectos_abiertos": 1}),
    "proyecto cerrado": (lambda m: m.proyecto(1, "P", area="Hogar", estado="cerrado"), {"proyectos_cerrados": 1}),
    "proyecto en la papelera": (lambda m: m.proyecto(1, "P", area="Hogar", borrado=True), {"proyectos_papelera": 1}),
    "tarea pendiente": (lambda m: m.tarea(10, "T", area="Hogar"), {"tareas_pendientes": 1}),
    "tarea hecha": (lambda m: m.tarea(10, "T", area="Hogar", estado="hecha", completado=_dia(-1)), {"tareas_hechas": 1}),
    "tarea de otro estado": (lambda m: m.tarea(10, "T", area="Hogar", estado="descartado"), {"tareas_otras": 1}),
    "tarea en la papelera": (lambda m: m.tarea(10, "T", area="Hogar", borrada=True), {"tareas_papelera": 1}),
}


@pytest.mark.parametrize("clase", CLASES)
async def test_un_grupo_con_cosas_no_se_quita_y_dice_cuantas(base, clase):
    sembrar, esperado = CLASES[clase]
    sembrar(base)
    base.con.commit()
    antes_g, antes_f = _grupos(base), _fotos(base)
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await db.quitar_grupo("Hogar")
    assert e.value.clave == "con_cosas"
    ceros = {k: 0 for k in ("proyectos_abiertos", "proyectos_cerrados", "proyectos_papelera", "tareas_pendientes",
                            "tareas_hechas", "tareas_otras", "tareas_papelera")}
    assert e.value.contenido == {**ceros, **esperado, "total": 1}
    assert await db.contenido_de_grupo("Hogar") == e.value.contenido     # la misma medida que usa la página
    assert _grupos(base) == antes_g and _fotos(base) == antes_f


async def test_el_grupo_fijo_no_se_quita_aunque_este_vacio_y_el_que_no_existe_se_dice(base):
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await db.quitar_grupo(db.AREA_TECNICA)
    assert e.value.clave == "fijo" and db.AREA_TECNICA in [a[0] for a in _grupos(base)]
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await db.quitar_grupo("No existe")
    assert e.value.clave == "no_existe"
    assert await db.contenido_de_grupo("No existe") is None


async def test_si_le_meten_algo_al_grupo_entre_la_lectura_y_el_borrado_la_llave_lo_rechaza(base, monkeypatch):
    """Declarado en la cabecera: el gancho borra SIN la guarda `NOT EXISTS` (lo que haría
    una lectura vieja) cuando ya hay un proyecto en el grupo; la llave foránea de la base
    lo rechaza y `quitar_grupo` lo dice como «tiene cosas», con la cuenta de ese momento."""
    base.proyecto(1, "Llegó justo", area="Hogar")
    base.con.commit()
    original = g._Cur.execute

    async def sin_la_guarda(self, sql, params=()):
        if sql.startswith("DELETE FROM areas"):
            sql = "DELETE FROM areas WHERE clave = %s RETURNING clave"
        return await original(self, sql, params)
    monkeypatch.setattr(g._Cur, "execute", sin_la_guarda)
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await db.quitar_grupo("Hogar")
    assert e.value.clave == "con_cosas" and e.value.contenido["proyectos_abiertos"] == 1
    assert "Hogar" in [a[0] for a in _grupos(base)]


async def test_la_base_no_deja_dejar_huerfano_a_nadie_con_la_guarda_apagada(base):
    """La garantía de fondo: aunque se quitara la guarda de `quitar_grupo`, un `DELETE` de un
    grupo con filas lo rechaza la llave (la prueba de arriba depende de esto)."""
    base.proyecto(1, "P", area="Hogar")
    base.con.commit()
    with pytest.raises(Exception) as e:
        base.con.execute("DELETE FROM areas WHERE clave = 'Hogar'")
    assert type(e.value).__name__ == "IntegrityError"


# ── La página y las rutas ───────────────────────────────────────────────────

def _casa(base):
    return _cliente(config.CHAT_ID_DUENO)


def _va_a(r) -> dict:
    assert r.status_code == 303, (r.status_code, r.text[:200])
    return dict(parse_qsl(urlsplit(r.headers["location"]).query))


def _avisos(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", a).strip() for a in re.findall(r'<p class="aviso[^"]*">(.*?)</p>', html, re.S)]


def _lista(html: str) -> str:
    return html.split("<aside>", 1)[1].split("</aside>", 1)[0]


def test_la_pagina_ofrece_nuevo_grupo_y_crear_lo_pinta_al_final_con_su_aviso(base):
    c = _casa(base)
    assert '<a class="nuevo-grupo" href="/proyectos?nuevo_grupo=1">+ Nuevo grupo</a>' in ver(base)
    form = ver(base, nuevo_grupo=1)
    assert '<form class="nuevo-grupo-form" method="post" action="/proyectos/grupos">' in form
    assert 'name="nombre"' in form and ">Crear grupo</button>" in form
    r = c.post("/proyectos/grupos", data={"nombre": "Taller"}, follow_redirects=False)
    q = _va_a(r)
    assert q == {"hecho": "grupo_creado", "grupo": "Taller"}
    html = ver(base, **q)
    assert _avisos(html)[0] == "Grupo «Taller» creado. Está al final de la lista de la izquierda."
    nombres = re.findall(r'<div class="grupo gc" data-g="([^"]*)"', _lista(html))
    assert nombres[-2:] == ["hogar", "taller"] or nombres[-1] == "taller", nombres
    assert [a["clave"] for a in db_areas(base)][-1] == "Taller"


def db_areas(base):
    return [{"clave": r[0]} for r in base.con.execute("SELECT clave FROM areas ORDER BY orden, clave")]


@pytest.mark.parametrize("pedido,clave,texto", [
    ("", "grupo_vacio", "El nombre del grupo no puede quedar vacío."),
    ("x" * 31, "grupo_largo", "El nombre del grupo no puede pasar de 30 caracteres."),
    ("Sin grupo", "grupo_reservado", "«Sin grupo» ya es el nombre de lo que no tiene grupo: escoge otro nombre."),
    ("cds", "grupo_repetido", "Ya hay un grupo con ese nombre (no cuenta si cambian las mayúsculas, las tildes o los espacios). El grupo NO se creó."),
])
def test_crear_un_nombre_que_no_vale_no_crea_nada_y_el_aviso_lo_dice(base, pedido, clave, texto):
    antes = _grupos(base)
    q = _va_a(_casa(base).post("/proyectos/grupos", data={"nombre": pedido}, follow_redirects=False))
    assert q == {"nuevo_grupo": "1", "error": clave}
    html = ver(base, **q)
    assert _avisos(html)[0] == texto and '<form class="nuevo-grupo-form"' in html
    assert _grupos(base) == antes


def test_quitar_pregunta_primero_y_un_grupo_vacio_se_quita(base):
    c = _casa(base)
    html = ver(base, quitar_grupo="Hogar")
    assert "¿Quitar el grupo «Hogar»? Está vacío." in html
    assert '<form class="confirmar" method="post" action="/proyectos/grupos/quitar">' in html
    assert '<input type="hidden" name="clave" value="Hogar">' in html and ">Sí, quitarlo</button>" in html
    assert "Hogar" in [a[0] for a in _grupos(base)]                     # preguntar no quita nada
    q = _va_a(c.post("/proyectos/grupos/quitar", data={"clave": "Hogar"}, follow_redirects=False))
    assert q == {"hecho": "grupo_quitado", "grupo": "Hogar"}
    assert "Hogar" not in [a[0] for a in _grupos(base)]
    html = ver(base, **q)
    assert _avisos(html)[0] == "Grupo «Hogar» quitado." and 'data-g="hogar"' not in html


def test_un_grupo_con_cosas_dice_cuantas_y_no_ofrece_quitarlo(base):
    base.proyecto(1, "A", area="Hogar")
    base.proyecto(2, "B", area="Hogar")
    base.proyecto(3, "C", area="Hogar", estado="cerrado")
    base.proyecto(4, "D", area="Hogar", borrado=True)
    base.tarea(10, "t1", area="Hogar")
    base.tarea(11, "t2", area="Hogar", estado="hecha", completado=_dia(-1))
    base.tarea(12, "t3", area="Hogar", borrada=True)
    base.con.commit()
    html = ver(base, quitar_grupo="Hogar")
    texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))
    assert ("No se puede quitar «Hogar»: todavía tiene 2 proyectos abiertos, 1 proyecto cerrado, "
            "1 proyecto en la papelera, 1 tarea suelta pendiente, 1 tarea suelta hecha, "
            "1 tarea suelta en la papelera. Un grupo solo se quita cuando no queda nada en él: "
            "mueve esos proyectos y tareas a otro grupo primero.") in texto
    assert 'action="/proyectos/grupos/quitar"' not in html and ">Sí, quitarlo<" not in html
    antes = (_grupos(base), _fotos(base))
    q = _va_a(_casa(base).post("/proyectos/grupos/quitar", data={"clave": "Hogar"}, follow_redirects=False))
    assert q == {"error": "grupo_con_cosas", "quitar_grupo": "Hogar"}
    assert (_grupos(base), _fotos(base)) == antes                       # y nada se movió
    despues = ver(base, **q)
    assert _avisos(despues)[0] == "El grupo NO se quitó: todavía tiene cosas (se dice a la izquierda)."
    assert "2 proyectos abiertos" in despues


def test_el_grupo_fijo_no_ofrece_la_x_ni_se_quita_por_la_ruta(base):
    assert 'href="/proyectos?quitar_grupo=IA"' not in ver(base)
    assert "Ese grupo no se puede quitar: Lucy y Code lo usan." in ver(base, quitar_grupo="IA")
    q = _va_a(_casa(base).post("/proyectos/grupos/quitar", data={"clave": "IA"}, follow_redirects=False))
    assert q["error"] == "grupo_fijo" and "IA" in [a[0] for a in _grupos(base)]
    assert _avisos(ver(base, **q))[0] == "Ese grupo no se puede quitar: Lucy y Code lo usan."


def test_quitar_un_grupo_que_ya_no_esta_lo_dice(base):
    q = _va_a(_casa(base).post("/proyectos/grupos/quitar", data={"clave": "Fantasma"}, follow_redirects=False))
    assert q["error"] == "grupo_no_existe"
    assert _avisos(ver(base, **q))[0] == "Ese grupo ya no está."


def test_los_avisos_de_creado_y_quitado_salen_de_la_base_no_de_la_direccion(base):
    """Una dirección escrita a mano no puede hacer decir «creado» de un grupo que no está
    ni «quitado» de uno que sigue."""
    assert not [a for a in _avisos(ver(base, hecho="grupo_creado", grupo="Inventado")) if "creado" in a]
    assert not [a for a in _avisos(ver(base, hecho="grupo_quitado", grupo="CDS")) if "quitado" in a]
    assert _avisos(ver(base, hecho="grupo_quitado", grupo="Inventado")) == ["Grupo «Inventado» quitado."]   # no está: es verdad
    assert [a for a in _avisos(ver(base, hecho="grupo_creado", grupo="CDS")) if "creado" in a]            # está: es verdad


def test_el_nombre_en_un_aviso_se_escapa(base):
    html = ver(base, hecho="grupo_quitado", grupo="<script>x</script>")
    assert "<script>x" not in html.split("<main>", 1)[1].split("</main>", 1)[0]


def test_con_un_filtro_de_busqueda_no_se_ofrece_agregar_ni_quitar(base):
    lista = _lista(ver(base, q="a"))
    assert "nuevo-grupo" not in lista and "quitar-grupo" not in lista


# ── solo_ver y sin sesión ───────────────────────────────────────────────────

RUTAS_DE_GRUPOS = [("/proyectos/grupos", {"nombre": "Colado"}), ("/proyectos/grupos/quitar", {"clave": "Hogar"})]


@pytest.mark.parametrize("ruta,datos", RUTAS_DE_GRUPOS)
@pytest.mark.parametrize("quien", ["ver", None])
def test_quien_solo_ve_o_no_entro_no_llama_a_las_rutas_de_grupos(base, ruta, datos, quien):
    antes = _grupos(base)
    r = cliente_de_sesion(quien).post(ruta, data=datos, follow_redirects=False)
    assert r.status_code == 401, (quien, ruta, r.status_code)
    assert _grupos(base) == antes


def test_en_solo_ver_ni_los_controles_ni_los_avisos_ni_una_direccion_escrita_a_mano(base):
    c = cliente_de_sesion("ver")
    for consulta in ({}, {"nuevo_grupo": 1}, {"quitar_grupo": "Hogar"}, {"quitar_grupo": "CDS"},
                     {"hecho": "grupo_creado", "grupo": "CDS"}, {"error": "grupo_repetido"}):
        r = c.get("/proyectos", params=consulta)
        assert r.status_code == 200
        cuerpo = re.sub(r"<style>.*?</style>", "", r.text, flags=re.S)     # la hoja de estilo lleva los nombres de las clases
        for prohibido in ("nuevo-grupo", "quitar-grupo", "quitar-aviso", "/proyectos/grupos",
                          "Nuevo grupo", "Quitar el grupo", "Sí, quitarlo", "Ya hay un grupo", "creado."):
            assert prohibido not in cuerpo, (consulta, prohibido)


def test_toda_ruta_que_escribe_en_proyectos_exige_sesion_de_la_casa_por_la_puerta_declarada():
    """Hermanos, de lo REAL (`app.routes`): cada POST bajo `/proyectos` declara la puerta
    `siempre` (la sesión de la casa y nada más), no la de ver."""
    import web.app as panel
    import web.auth as auth
    posts = [r for r in panel.app.routes
             if "POST" in (getattr(r, "methods", None) or set()) and r.path.startswith("/proyectos")]
    assert {"/proyectos/grupos", "/proyectos/grupos/quitar"} <= {r.path for r in posts}
    for r in posts:
        assert r.endpoint.puerta == auth.PUERTA_SIEMPRE, r.path


# ── Nadie da por fijo un grupo (salvo el técnico) ───────────────────────────

# Nombres de grupo que hay hoy (CDS, ACD, IA, Hogar) escritos como texto completo en
# código de producción. Exentos, uno por uno: 'Hogar' como CATEGORÍA de gasto
# (`cerebro/bancos/categorias.py`), que no es un grupo, y 'IA' en `db.AREA_TECNICA`.
# FRONTERA: ve los textos EXACTOS; no ve un nombre dentro de una frase ni armado por partes, ni
# el CSS de la plantilla (`.grupo[data-g="acd"]` solo pinta el ícono de ACD, no lo exige).
_EXENTOS = {("cerebro/bancos/categorias.py", "Hogar"), ("db/db.py", "IA")}


def test_ningun_codigo_de_produccion_da_por_fijo_el_nombre_de_un_grupo_salvo_el_tecnico():
    encontrados = set()
    from test_buzon_que_no_se_ve import _py_en_disco       # LA puerta de los barridos del repo
    for p in _py_en_disco(_ROOT):
        rel = p.relative_to(_ROOT).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/", ".git/")) or "site-packages" in rel:
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and n.value in ("CDS", "ACD", "IA", "Hogar"):
                encontrados.add((rel, n.value))
    assert encontrados == _EXENTOS, encontrados ^ _EXENTOS
    assert db.AREA_TECNICA == "IA"


def test_el_censo_de_nombres_fijos_ve_uno_inventado(tmp_path):
    (tmp_path / "x.py").write_text("GRUPOS = ['CDS']\n")
    visto = {n.value for n in ast.walk(ast.parse((tmp_path / "x.py").read_text())) if isinstance(n, ast.Constant)}
    assert "CDS" in visto
