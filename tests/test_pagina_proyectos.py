"""La página de proyectos (Lucy 1.0, E4: solo lectura): G4, G10 y G11, y todo lo
que pinta.

Se recorre la ruta REAL (`GET /proyectos`) con la plantilla REAL. La base es
SQLite con el SQL REAL de `db.pagina_de_proyectos` (cuatro lecturas, una con
`UNION ALL`); la frontera es la de `tests/test_grupo_ia.py` y
`tests/test_base_m2.py`:

  · NO hay Postgres en esta máquina. Las tablas salen del `CREATE TABLE` real de
    `db/schema.sql` (proyectos, comentarios_tarea) o de las columnas que declara
    `db.columnas_declaradas()` (tareas, log_acciones), y SQLite imita dos cosas
    de psycopg: devuelve `datetime` con zona en las columnas de fecha y las
    columnas JSON ya leídas.
  · NO hay `node`: la página no lleva ningún `<script>` (se comprueba), así que
    no hay JavaScript que probar. El filtro de la lista es del servidor (`?q=`).
  · Lo que no se ve acá: cómo se ve en un navegador. Para eso está el HTML
    renderizado que se entrega junto al trabajo.

Correr:  python3 -m pytest tests/test_pagina_proyectos.py -q
"""
from __future__ import annotations

import ast
import re
import types
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import test_base_m2 as b2
import test_grupo_ia as g
import acciones.crud as crud
import config
import db.db as db
import web.app as panel
import web.auth as auth

UTC = timezone.utc
HOY = date(2026, 10, 10)
CREADO = datetime(2026, 10, 9, 15, tzinfo=UTC)
AREAS = [{"clave": "CDS", "color": "#0f7c74"}, {"clave": "ACD", "color": "#b5611a"},
         {"clave": "IA", "color": "#8a4a8f"}]


def _dia(n: int, hora: int = 12, minuto: int = 0) -> datetime:
    """Un instante en Santo Domingo, `n` días desde HOY."""
    return datetime.combine(HOY + timedelta(days=n), datetime.min.time(),
                            tzinfo=config.TZ).replace(hour=hora, minute=minuto)


# ═══════════════════════════════════════════════════════════════════════
# Armar el modelo sin base (lo reusan las pruebas viejas de la página)
# ═══════════════════════════════════════════════════════════════════════

def modelo_de_filas(proyectos=(), tareas=(), areas=AREAS, huellas=(),
                    comentarios=(), nombres=None, hoy=HOY) -> dict:
    """`db.armar_pagina` con filas a medias: lo que falte se rellena con lo
    normal (un proyecto activo y vivo, una tarea pendiente y suelta)."""
    proyectos = [{"creado_en": CREADO, "descripcion": None, "estado": "activo",
                  "area": None, "responsable_chat_id": None,
                  "cliente_nombre": None, **p} for p in proyectos]
    tareas = [{"proyecto_id": None, "area": None, "vence_en": None,
               "completado_en": None, "creado_en": CREADO,
               "responsable_chat_id": None, "estado": "pendiente", **t}
              for t in tareas]
    return db.armar_pagina(list(areas), proyectos, tareas, list(huellas),
                           list(comentarios), nombres or {}, hoy)


def pintar_modelo(modelo: dict, areas=AREAS, **consulta) -> str:
    """La página con ESE modelo: la ruta real y la plantilla real, con la base
    reemplazada por el modelo ya armado."""
    async def _pagina(hoy=None):
        return modelo

    async def _areas():
        return list(areas)

    guardado = (db.pagina_de_proyectos, db.areas)
    db.pagina_de_proyectos, db.areas = _pagina, _areas
    try:
        r = _cliente(config.CHAT_ID_DUENO).get("/proyectos", params=consulta)
    finally:
        db.pagina_de_proyectos, db.areas = guardado
    assert r.status_code == 200, r.text[:300]
    return r.text


# ═══════════════════════════════════════════════════════════════════════
# La base de prueba y el cliente HTTP
# ═══════════════════════════════════════════════════════════════════════

class Mundo:
    """La base de SQLite con sus áreas, y los métodos para sembrar."""

    def __init__(self):
        self.con = b2._base()
        self.con.execute("PRAGMA foreign_keys = OFF")   # sembrar en cualquier orden
        for a in AREAS[1:]:
            self.con.execute("INSERT INTO areas (clave, color, orden) VALUES (?, ?, 2)",
                             (a["clave"], a["color"]))
        self.con.execute("UPDATE areas SET color = '#0f7c74' WHERE clave = 'CDS'")
        self._t = 100

    def proyecto(self, id, nombre, area=None, estado="activo", creado=CREADO,
                 borrado=False, cliente=None, responsable=None):
        self.con.execute(
            "INSERT INTO proyectos (id, nombre, estado, area, creado_en, borrado_en, "
            "cliente_noco_id, cliente_nombre, responsable_chat_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (id, nombre, estado, area, creado.isoformat(),
             "2026-09-01T00:00:00+00:00" if borrado else None,
             7 if cliente else None, cliente, responsable))
        return id

    def tarea(self, id, titulo, proyecto=None, area=None, estado="pendiente",
              vence=None, completado=None, borrada=False, responsable=None):
        self.con.execute(
            "INSERT INTO tareas (id, titulo, proyecto_id, area, estado, vence_en, "
            "completado_en, borrado_en, responsable_chat_id, creado_en, bandeja_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (id, titulo, proyecto, area, estado,
             vence.isoformat() if vence else None,
             completado.isoformat() if completado else None,
             "2026-09-01T00:00:00+00:00" if borrada else None, responsable,
             CREADO.isoformat(), 9000 + id))
        return id

    def huella(self, accion, tabla, registro_id, cuando):
        self.con.execute(
            "INSERT INTO log_acciones (actor, accion, tabla, registro_id, ts) "
            "VALUES ('lucy', ?, ?, ?, ?)", (accion, tabla, registro_id, cuando.isoformat()))

    def comentario(self, id, tarea_id, autor, texto, cuando=CREADO, editado=None,
                   borrado=False):
        self.con.execute(
            "INSERT INTO comentarios_tarea (id, tarea_id, autor_chat_id, creado_en, "
            "texto, editado_en, borrado_en) VALUES (?,?,?,?,?,?,?)",
            (id, tarea_id, autor, cuando.isoformat(), texto,
             editado.isoformat() if editado else None,
             "2026-09-02T00:00:00+00:00" if borrado else None))

    def ids_vivos(self) -> set[int]:
        return {f[0] for f in self.con.execute("SELECT id FROM tareas WHERE borrado_en IS NULL")}


@pytest.fixture
def gente():
    permitidos, nombres = config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT
    dueno, rosi = config.CHAT_ID_DUENO, 700100001
    config.NOMBRES_POR_CHAT = {dueno: "Persona Uno", rosi: "Persona Dos"}
    config.CHAT_IDS_PERMITIDOS = (dueno, rosi)
    yield types.SimpleNamespace(dueno=dueno, rosi=rosi)
    config.CHAT_IDS_PERMITIDOS, config.NOMBRES_POR_CHAT = permitidos, nombres


@pytest.fixture
def mundo(monkeypatch, gente):
    m = Mundo()
    monkeypatch.setattr(db, "pool", g._Pool(m.con))
    monkeypatch.setattr(db, "hoy_rd", lambda: HOY)
    return m


def _cliente(chat=None) -> TestClient:
    c = TestClient(panel.app)
    if chat is not None:
        c.cookies.set(panel.COOKIE, auth.crear_token(chat, auth.VIDA_SESION))
    return c


def ver(mundo, **consulta) -> str:
    """`GET /proyectos?...` por la ruta real, con sesión del dueño."""
    r = _cliente(config.CHAT_ID_DUENO).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def titulo_de(html: str):
    """El texto del título (`<h1>`) de la página, o None si no hay. El título
    lleva atributos (`data-dbl`, `title`): se lee el contenido, no la etiqueta."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html.split("<main>", 1)[-1], re.S)
    return m.group(1) if m else None


def tareas_en(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'data-tarea="(\d+)"', html)]


# ═══════════════════════════════════════════════════════════════════════
# G4: cada tarea viva, en exactamente un sitio
# ═══════════════════════════════════════════════════════════════════════

def _sembrar_los_casos(m: Mundo):
    """Los casos del §5.2 y los bordes: con proyecto (de un grupo, sin grupo,
    cerrado), suelta con grupo, suelta sin grupo, de un proyecto en la
    papelera; pendiente, hecha, descartada, y de un estado que nadie declaró."""
    m.proyecto(1, "Remodelación", area="CDS", cliente="Colegio San Juan")
    m.proyecto(2, "Taller", area="ACD")
    m.proyecto(3, "De IA", area="IA", estado="cerrado")
    m.proyecto(4, "Sin grupo", area=None)
    m.proyecto(5, "En la papelera", area="CDS", borrado=True)
    m.tarea(10, "pendiente en CDS", proyecto=1, vence=_dia(2))
    m.tarea(11, "hecha en CDS", proyecto=1, estado="hecha", completado=_dia(-1))
    m.tarea(12, "descartada en CDS", proyecto=1, estado="descartado")
    m.tarea(13, "de estado raro", proyecto=1, estado="pospuesta-rara")
    m.tarea(14, "en ACD", proyecto=2)
    m.tarea(15, "en un proyecto cerrado", proyecto=3, estado="hecha", completado=_dia(-3))
    m.tarea(16, "en un proyecto sin grupo", proyecto=4)
    m.tarea(17, "suelta de CDS", area="CDS", vence=_dia(-1))
    m.tarea(18, "suelta de ACD, hecha", area="ACD", estado="hecha", completado=_dia(-2))
    m.tarea(19, "suelta de IA", area="IA")
    m.tarea(20, "suelta sin grupo", area=None)
    m.tarea(21, "descartada suelta sin grupo", area=None, estado="descartado")
    m.tarea(22, "de un proyecto en la papelera", proyecto=5)
    m.tarea(23, "borrada, de CDS", area="CDS", borrada=True)
    m.tarea(24, "borrada, en un proyecto", proyecto=1, borrada=True)


def _todas_las_vistas(m: Mundo) -> list[dict]:
    """Cada sitio donde la página puede enseñar tareas. LA LISTA SALE DE LA
    BASE (los proyectos vivos, las áreas y «Sin grupo»), no de lo que la página
    pinte en su menú."""
    vistas = [{"p": f[0]} for f in m.con.execute(
        "SELECT id FROM proyectos WHERE borrado_en IS NULL")]
    vistas += [{"g": f[0]} for f in m.con.execute("SELECT clave FROM areas")]
    vistas.append({"sin_grupo": 1})
    return vistas


def test_G4_toda_tarea_viva_aparece_en_exactamente_un_sitio(mundo):
    _sembrar_los_casos(mundo)
    vivas = mundo.ids_vivos()
    por_vista = {}
    for consulta in _todas_las_vistas(mundo):
        por_vista[tuple(consulta.items())] = tareas_en(ver(mundo, **consulta))
    todas = [t for ts in por_vista.values() for t in ts]
    assert sorted(todas) == sorted(vivas), (
        f"faltan {sorted(vivas - set(todas))}; sobran {sorted(set(todas) - vivas)}; "
        f"repetidas {sorted({t for t in todas if todas.count(t) > 1})}")
    for vista, ts in por_vista.items():
        assert len(ts) == len(set(ts)), f"{vista}: una tarea se pinta dos veces"


def test_G4_las_de_la_papelera_no_salen_ni_su_proyecto(mundo):
    _sembrar_los_casos(mundo)
    for consulta in _todas_las_vistas(mundo):
        html = ver(mundo, **consulta)
        assert 'data-tarea="23"' not in html and 'data-tarea="24"' not in html
    assert "En la papelera" not in ver(mundo)          # el proyecto, fuera de la lista


def test_G4_lo_que_no_es_pendiente_va_plegado_y_se_cuenta_bien(mundo):
    _sembrar_los_casos(mundo)
    html = ver(mundo, p=1)
    # Pendiente: 1. Hecha: 1. Con otro estado: la descartada y la rara.
    assert "1 hecha y 2 con otro estado" in html
    plegado = html.split('<details class="plegar">', 1)[1].split("</details>", 1)[0]
    assert {11, 12, 13} == set(tareas_en(plegado))
    assert 10 not in tareas_en(plegado)


def test_G4_una_de_un_proyecto_en_la_papelera_se_ve_en_sin_grupo_y_lo_dice(mundo):
    _sembrar_los_casos(mundo)
    html = ver(mundo, sin_grupo=1)
    assert 22 in tareas_en(html)
    assert "Era de un proyecto que está en la papelera." in html
    # Y el proyecto de «Sin grupo» sale en la lista de la izquierda, con su enlace.
    assert 'href="/proyectos?p=4"' in html


def test_G4_el_modelo_no_pierde_una_tarea_inventada_con_area_que_ya_no_existe():
    """Con entradas que la base no deja hoy (la FK) pero el código no debe
    perder: una tarea suelta con un área que no está en `areas`."""
    modelo = modelo_de_filas(tareas=[{"id": 1, "titulo": "x", "area": "Inventada"}])
    assert [t["id"] for t in modelo["sin_grupo"]["sueltas"]["pendientes"]] == [1]


# ═══════════════════════════════════════════════════════════════════════
# G10: dormido
# ═══════════════════════════════════════════════════════════════════════

def _dormido_en(html: str) -> bool:
    return 'class="aviso-dormido"' in html


def test_G10_los_avisos_automaticos_no_despiertan_un_proyecto(mundo):
    mundo.proyecto(1, "Solo avisos", area="CDS", creado=_dia(-20))
    mundo.tarea(10, "t", proyecto=1)
    mundo.huella("avisar", "tareas", 10, _dia(-1))
    mundo.huella("aviso_atraso_code", "tareas", 10, _dia(0))
    html = ver(mundo, p=1)
    assert _dormido_en(html) and "20 días sin movimiento" in html
    assert "Sin movimiento hace 20 días" in html          # y en la lista de la izquierda


@pytest.mark.parametrize("tabla,accion", [("proyectos", "editar"), ("tareas", "borrar"),
                                           ("tareas", "crear"), ("tareas", "deshacer")])
def test_G10_cualquier_movimiento_de_verdad_lo_despierta(mundo, tabla, accion):
    mundo.proyecto(1, "Con movimiento", area="CDS", creado=_dia(-20))
    mundo.tarea(10, "t", proyecto=1)
    mundo.huella(accion, tabla, 1 if tabla == "proyectos" else 10, _dia(-1))
    html = ver(mundo, p=1)
    assert not _dormido_en(html) and "Último movimiento: ayer" in html


def test_G10_un_comentario_es_un_movimiento(mundo):
    mundo.proyecto(1, "Con comentario", area="CDS", creado=_dia(-20))
    mundo.tarea(10, "t", proyecto=1)
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "hola", cuando=_dia(-2))
    mundo.huella("crear", "comentarios_tarea", 50, _dia(-2))
    assert not _dormido_en(ver(mundo, p=1))


def test_G10_un_cerrado_nunca_esta_dormido(mundo):
    mundo.proyecto(1, "Cerrado viejo", area="CDS", estado="cerrado", creado=_dia(-300))
    html = ver(mundo, p=1)
    assert not _dormido_en(html) and "Cerrado" in html


def test_G10_el_borde_son_cinco_dias_por_dia_de_santo_domingo():
    assert db.DIAS_DORMIDO == 5
    assert not db.esta_dormido("activo", 4) and db.esta_dormido("activo", 5)
    assert not db.esta_dormido("cerrado", 500) and not db.esta_dormido("activo", None)
    # 23:30 de Santo Domingo del día 5 es el día 6 en UTC: cuenta el día de aquí.
    tarde = datetime.combine(HOY - timedelta(days=5), datetime.min.time(),
                             tzinfo=config.TZ).replace(hour=23, minute=30)
    assert db.dias_sin_movimiento(tarde, HOY) == 5


def _acciones_que_escribe_el_codigo():
    """(las `accion` literales, los sitios con `accion` no literal). LA LISTA
    SALE DE RECORRER LOS `.py` del repo: los `accion="..."` de las llamadas
    (`_registrar`) y el segundo valor de cada `INSERT INTO log_acciones ...
    VALUES ('actor', 'accion', ...)`. FRONTERA: una `accion` que llegue por una
    variable no se ve; se cuentan aparte."""
    literales, variables = set(), []
    for archivo in g._archivos_de_texto():
        if archivo.suffix != ".py":
            continue
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        lit, var = _acciones_de(arbol)
        literales |= lit
        variables += [f"{archivo.name}:{n}" for n in var]
    return literales, variables


def _acciones_de(arbol):
    literales, variables = set(), []
    sql = re.compile(r"INSERT\s+INTO\s+log_acciones[\s\S]*?VALUES\s*\(\s*(?:'[^']*'|%s)\s*,"
                     r"\s*(?:'([^']*)'|(%s))", re.I)
    for n in ast.walk(arbol):
        if isinstance(n, ast.keyword) and n.arg == "accion":
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                literales.add(n.value.value)
            else:
                variables.append(n.value.lineno)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            for m in sql.finditer(n.value):
                if m.group(1) is not None:
                    literales.add(m.group(1))
                else:
                    variables.append(n.lineno)
    return literales, variables


def test_G10_toda_accion_que_escribe_el_codigo_esta_clasificada():
    literales, _ = _acciones_que_escribe_el_codigo()
    clasificadas = set(db.ACCIONES_QUE_MUEVEN) | set(db.ACCIONES_AUTOMATICAS)
    assert literales - clasificadas == set(), (
        f"el código escribe acciones que nadie clasificó como movimiento o como "
        f"automática: {sorted(literales - clasificadas)}")
    assert clasificadas - literales == set(), (
        f"hay acciones clasificadas que el código ya no escribe (¿un typo?): "
        f"{sorted(clasificadas - literales)}")
    assert not set(db.ACCIONES_QUE_MUEVEN) & set(db.ACCIONES_AUTOMATICAS)


def test_G10_el_censo_ve_una_accion_inventada():
    arbol = ast.parse(
        "async def a(c):\n"
        "    await _registrar(c, accion='inventada_uno', tabla='x')\n"
        "    await c.execute(\"INSERT INTO log_acciones (actor, accion) VALUES ('panel', 'inventada_dos')\")\n"
        "    await c.execute('INSERT INTO log_acciones (actor, accion) VALUES (%s, %s)')\n"
        "    await _registrar(c, accion=otra_variable)\n")
    literales, variables = _acciones_de(arbol)
    assert literales == {"inventada_uno", "inventada_dos"} and len(variables) == 2


# ═══════════════════════════════════════════════════════════════════════
# G11: «vencida» es la de `grupo_de_tarea`, por día de Santo Domingo
# ═══════════════════════════════════════════════════════════════════════

def _vencidas(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'class="tarea [^"]*vencida[^"]*" id="tarea-(\d+)"', html)]


def test_G11_la_que_vence_hoy_a_las_9_no_esta_vencida_a_las_10(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "vence hoy a las 9", proyecto=1, vence=_dia(0, 9))
    mundo.tarea(11, "venció ayer", proyecto=1, vence=_dia(-1, 9))
    mundo.tarea(12, "vence mañana", proyecto=1, vence=_dia(1, 9))
    mundo.tarea(13, "sin fecha", proyecto=1)
    html = ver(mundo, p=1)
    assert _vencidas(html) == [11]
    assert "Venció el 9 oct" in html and "1 vencida" in html


def test_G11_el_dia_se_cuenta_en_santo_domingo_no_en_utc(mundo):
    mundo.proyecto(1, "P", area="CDS")
    # Hoy a las 11:30 p. m. en Santo Domingo ya es MAÑANA en UTC: no está vencida.
    mundo.tarea(10, "hoy de noche", proyecto=1, vence=_dia(0, 23, 30))
    # Ayer a las 8 p. m. en Santo Domingo es HOY a las 00:00 en UTC: sí está vencida.
    mundo.tarea(11, "ayer de noche", proyecto=1, vence=_dia(-1, 20))
    assert _vencidas(ver(mundo, p=1)) == [11]


def test_G11_una_hecha_vieja_no_esta_vencida(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "hecha tarde", proyecto=1, estado="hecha", vence=_dia(-5), completado=_dia(-4))
    assert _vencidas(ver(mundo, p=1)) == []


def test_G11_la_pagina_usa_la_misma_definicion_que_tareas_por_grupo():
    for estado in ("pendiente", "hecha", "descartado"):
        for n in (-3, -1, 0, 1, None):
            vence = _dia(n, 9) if n is not None else None
            modelo = modelo_de_filas(tareas=[{"id": 1, "titulo": "x", "estado": estado,
                                              "vence_en": vence, "area": "CDS"}])
            t = (modelo["grupos"][0]["sueltas"]["pendientes"]
                 + modelo["grupos"][0]["sueltas"]["otras"])[0]
            assert t["vencida"] == (db.grupo_de_tarea(estado, vence, HOY) == "atrasadas"), (estado, n)


# ═══════════════════════════════════════════════════════════════════════
# Lo que se pinta
# ═══════════════════════════════════════════════════════════════════════

def test_el_orden_de_las_pendientes_es_vencidas_primero_luego_fecha_y_al_final_sin_fecha(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "sin fecha", proyecto=1)
    mundo.tarea(11, "en 5 días", proyecto=1, vence=_dia(5))
    mundo.tarea(12, "venció hace 1", proyecto=1, vence=_dia(-1))
    mundo.tarea(13, "en 1 día", proyecto=1, vence=_dia(1))
    mundo.tarea(14, "venció hace 4", proyecto=1, vence=_dia(-4))
    assert tareas_en(ver(mundo, p=1)) == [14, 12, 13, 11, 10]


def test_el_estado_se_calcula(mundo):
    mundo.proyecto(1, "Nuevo", area="CDS")
    mundo.proyecto(2, "Empezado", area="CDS")
    mundo.proyecto(3, "Pausado", area="CDS", estado="pausado")
    mundo.proyecto(4, "Cerrado", area="CDS", estado="cerrado")
    mundo.tarea(10, "a", proyecto=2, estado="hecha", completado=_dia(-1))
    mundo.tarea(11, "b", proyecto=2)
    esperado = {1: "Sin empezar", 2: "En curso", 3: "Pausado", 4: "Cerrado"}
    for pid, texto in esperado.items():
        assert f'<span class="pastilla">{texto}</span>' in ver(mundo, p=pid), (pid, texto)


def test_los_contadores_de_vencidas_de_la_lista_son_los_de_la_pagina_del_proyecto(mundo):
    """Una sola definición de «vencida» para la lista, las pastillas y las filas."""
    _sembrar_los_casos(mundo)
    mundo.tarea(30, "otra vencida", proyecto=1, vence=_dia(-9))
    lista = ver(mundo)
    for pid in (1, 2, 3, 4):
        filas = len(_vencidas(ver(mundo, p=pid)))
        en_lista = re.search(r'href="/proyectos\?p=%d"[^>]*>.*?<span class="cuenta">(.*?)</span>' % pid,
                             lista, re.S).group(1)
        n = re.search(r"<b>(\d+) vencida", en_lista)
        assert (int(n.group(1)) if n else 0) == filas, (pid, en_lista, filas)


def test_el_avance_y_los_contadores(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "h1", proyecto=1, estado="hecha", completado=_dia(-1))
    mundo.tarea(11, "p1", proyecto=1, vence=_dia(-2))
    mundo.tarea(12, "p2", proyecto=1)
    mundo.tarea(13, "p3", proyecto=1)
    html = ver(mundo, p=1)
    assert "1 de 4 tareas hechas" in html and 'style="width:25%"' in html
    assert "3 pendientes" in html and "1 vencida" in html
    assert "<b>1 vencida</b> · 1/4" in html          # la lista de la izquierda


def test_un_proyecto_sin_cliente_ni_responsable_no_pinta_guiones(mundo):
    mundo.proyecto(1, "Interno", area="IA")
    html = ver(mundo, p=1)
    assert "Cliente:" not in html
    assert "—" not in html and "Sin cliente" not in html
    # Sin responsable no se inventa uno: el desplegable pide que se escoja.
    assert '<option value="" selected disabled>Escoge…</option>' in html


def test_la_descripcion_del_proyecto_se_ve_si_la_hay_y_sale_escapada(mundo):
    """La página vieja la enseñaba («Estado: activo · descripción»): no se pierde."""
    mundo.proyecto(1, "Con nota", area="CDS")
    mundo.proyecto(2, "Sin nota", area="CDS")
    mundo.con.execute("UPDATE proyectos SET descripcion = ? WHERE id = 1",
                      ("Lo que se acordó <b>con el cliente</b>",))
    assert 'class="descripcion">Lo que se acordó &lt;b&gt;con el cliente&lt;/b&gt;</p>' in ver(mundo, p=1)
    assert 'class="descripcion"' not in ver(mundo, p=2)


def test_si_falta_uno_de_los_dos_no_se_pinta_su_guion(mundo, gente):
    mundo.proyecto(1, "Solo responsable", area="CDS", responsable=gente.rosi)
    mundo.proyecto(2, "Solo cliente", area="CDS", cliente="Colegio")
    solo_responsable, solo_cliente = ver(mundo, p=1), ver(mundo, p=2)
    assert '<option value="Persona Dos" selected>' in solo_responsable
    assert "Cliente:" not in solo_responsable and "—" not in solo_responsable
    assert "Cliente: <b>Colegio</b>" in solo_cliente
    assert 'selected disabled>Escoge…' in solo_cliente and "—" not in solo_cliente


def test_cliente_y_responsable_salen_por_nombre_y_nunca_por_numero(mundo, gente):
    mundo.proyecto(1, "Con todo", area="CDS", cliente="Colegio San Juan", responsable=gente.rosi)
    mundo.proyecto(2, "Responsable desconocido", area="CDS", responsable=555000111)
    mundo.tarea(10, "t", proyecto=1, responsable=gente.dueno)
    mundo.tarea(11, "de Code", proyecto=1, responsable=config.CHAT_ID_CODE)
    mundo.tarea(12, "de alguien sin nombre", proyecto=1, responsable=555000111)
    html = ver(mundo, p=1)
    assert "Cliente: <b>Colegio San Juan</b>" in html and '<option value="Persona Dos" selected>' in html
    assert 'title="Responsable: Persona Uno"' in html and 'title="Responsable: Code"' in html
    for pid in (1, 2):
        pagina = ver(mundo, p=pid)
        for numero in (str(gente.rosi), str(gente.dueno), "555000111"):
            assert numero not in pagina, f"salió el número {numero} en /proyectos?p={pid}"
    assert "selected disabled>Escoge…" in ver(mundo, p=2)   # sin nombre conocido, no se pinta


def test_la_lista_cuenta_cada_grupo_con_su_color_y_sus_proyectos(mundo):
    mundo.proyecto(1, "Beta", area="CDS", cliente="Zeta")
    mundo.proyecto(2, "alfa", area="CDS")
    mundo.proyecto(3, "Cerrado", area="CDS", estado="cerrado")
    html = ver(mundo)
    assert html.index('href="/proyectos?p=2"') < html.index('href="/proyectos?p=1"'), (
        "los proyectos van por nombre sin importar mayúsculas")
    assert 'style="--claro:#0f7c74;--oscuro:%s"' % db.color_oscuro_de_grupo("#0f7c74") in html
    assert "1 cerrado" in html
    assert html.count("Sin proyectos abiertos.") == 2          # ACD e IA


def test_las_hechas_salen_plegadas_y_no_en_la_lista_principal(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "pendiente", proyecto=1)
    mundo.tarea(11, "hecha", proyecto=1, estado="hecha", completado=_dia(-1))
    html = ver(mundo, p=1)
    principal, plegado = html.split('<details class="plegar">')
    assert tareas_en(principal) == [10] and tareas_en(plegado) == [11]
    assert "1 hecha<" in html or "1 hecha</summary>" in html


def test_una_pendiente_lleva_a_su_pantalla_y_lo_nuevo_es_un_enlace(mundo):
    mundo.proyecto(1, "Abierto", area="CDS")
    mundo.proyecto(2, "Cerrado", area="CDS", estado="cerrado")
    mundo.tarea(10, "mi tarea", proyecto=1)
    html = ver(mundo, p=1)
    assert 'href="/tareas/10"' not in html                  # el detalle está cerrado
    assert 'href="/tareas/10"' in ver(mundo, p=1, t=10)     # y de ahí se abre su pantalla
    assert 'href="/tareas/nueva?proyecto=1"' in html
    cerrado = ver(mundo, p=2)
    assert 'href="/tareas/nueva?proyecto=2"' not in cerrado
    assert "Proyecto cerrado: no se le agregan tareas." in cerrado


_JS_QUE_DECIDIRIA = re.compile(
    r"fetch\(|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|\.submit\(|"
    r"location\s*[.=]|\.action|FormData|innerHTML|eval\(")


def test_la_pagina_no_promete_nada_que_no_hace(mundo):
    """Cada escritura es un `<form method="post">` a una de las rutas del
    proyecto o de la tarea; no hay casillas `<input type="checkbox">`. Y el
    JavaScript NO decide nada de negocio: es un solo `<script>` que solo muestra
    o esconde un formulario de edición (no manda ni calcula nada)."""
    _sembrar_los_casos(mundo)
    mundo.comentario(50, 10, config.CHAT_ID_DUENO, "un comentario")
    # TODA escritura es una de estas rutas (la lista EXACTA de cada vista está
    # en `tests/test_escrituras_proyecto.py` y `tests/test_escrituras_tarea.py`).
    permitidas = re.compile(
        r"/proyectos/(nuevo|\d+/(nombre|area|responsable|estado|tareas)|"
        r"tarea/\d+/(hecha|reabrir|titulo|borrar|responsable|comentar|comentario/\d+/editar))")
    for consulta in ({"p": 1}, {"p": 3}, {"g": "CDS"}, {"sin_grupo": 1}, {"nuevo": "CDS"}, {},
                     {"p": 1, "t": 10}):
        html = ver(mundo, **consulta)
        for prohibido in ('type="checkbox"', "borrar-x\" type", "data-hecha"):
            assert prohibido not in html, (consulta, prohibido)
        guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        assert len(guiones) == 1 and not _JS_QUE_DECIDIRIA.search(guiones[0]), (consulta, guiones)
        for form in re.findall(r"<form[^>]*>", html):
            if 'method="post"' in form:
                assert permitidas.fullmatch(re.search(r'action="([^"]*)"', form).group(1)), (consulta, form)
            else:
                assert 'action="/proyectos"' in form and 'method="get"' in form, (consulta, form)


def test_se_siguen_pudiendo_cambiar_el_nombre_y_el_grupo_con_su_ruta_de_siempre(mundo):
    mundo.proyecto(1, "Mi proyecto", area="CDS")
    html = ver(mundo, p=1)
    assert 'action="/proyectos/1/nombre"' in html and 'action="/proyectos/1/area"' in html
    assert f'maxlength="{db.LARGO_NOMBRE_PROYECTO}"' in html
    for grupo in ("CDS", "ACD", "IA"):
        assert f'<option value="{grupo}"' in html
    assert re.search(r'<option value="CDS" selected>', html)


def test_los_comentarios_se_leen_con_nombre_fecha_y_marca_de_editado(mundo, gente):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "con comentarios", proyecto=1)
    mundo.comentario(50, 10, gente.rosi, "Le escribí a Luis", cuando=_dia(-1, 15))
    mundo.comentario(51, 10, gente.dueno, "Segundo <b>comentario</b>", cuando=_dia(0, 9),
                     editado=_dia(0, 10))
    mundo.comentario(52, 10, gente.dueno, "borrado, no sale", borrado=True)
    cerrado = ver(mundo, p=1)
    assert "2 comentarios" in cerrado and "Le escribí a Luis" not in cerrado   # el detalle está cerrado
    html = ver(mundo, p=1, t=10)
    assert "2 comentarios" in html
    assert "<b>Persona Dos</b>" in html and "Le escribí a Luis" in html
    assert "borrado, no sale" not in html
    assert "Segundo <b>comentario</b>" not in html and "Segundo &lt;b&gt;comentario&lt;/b&gt;" in html
    assert html.count(" · editado") == 1


def test_un_autor_de_comentario_sin_nombre_se_pinta_alguien_y_nunca_su_numero(mundo):
    mundo.proyecto(1, "P", area="CDS")
    mundo.tarea(10, "con comentario", proyecto=1)
    mundo.comentario(50, 10, 555000222, "de alguien sin nombre")
    html = ver(mundo, p=1, t=10)
    assert "<b>Alguien</b>" in html and "de alguien sin nombre" in html
    assert "555000222" not in html


def test_lo_que_escribe_una_persona_sale_escapado(mundo):
    peligro = '"><script>alert(1)</script>'
    mundo.proyecto(1, peligro, area="CDS", cliente=peligro)
    mundo.tarea(10, peligro, proyecto=1)
    mundo.tarea(11, peligro, area="CDS")
    for consulta in ({"p": 1}, {"g": "CDS"}, {}, {"q": peligro}):
        html = ver(mundo, **consulta)
        assert "<script>alert(1)" not in html, consulta


def test_un_color_que_no_es_un_hex_no_llega_al_estilo():
    assert db.color_de_grupo("#0f7c74") == "#0f7c74"
    for malo in (None, "", "red", '#fff;background:url(x)', "#12", 'x"onload="1'):
        assert db.color_de_grupo(malo) == db.COLOR_SIN_GRUPO, malo


def test_los_avisos_con_los_que_vuelven_las_rutas_que_escriben(mundo):
    mundo.proyecto(1, "P", area="CDS")
    for clave in ("area", "proyecto", "nombre_vacio", "nombre_largo", "nombre_repetido",
                  "nombre_igual", "nombre_invalido"):
        assert 'class="aviso"' in ver(mundo, error=clave, p=1), clave
    assert 'class="aviso"' not in ver(mundo, error="inventada")
    assert "Nombre guardado" in ver(mundo, nombre_guardado=1)
    assert "Grupo guardado" in ver(mundo, area_guardada=1)
    assert "Proyecto creado (#1)" in ver(mundo, creado=1)


# ── Qué se enseña a la derecha ──────────────────────────────────────────

def test_sin_decir_nada_se_enseña_el_primer_proyecto_de_la_lista(mundo):
    mundo.proyecto(1, "Zeta", area="ACD")
    mundo.proyecto(2, "Alfa", area="CDS")
    assert titulo_de(ver(mundo)) == "Alfa"          # CDS va antes que ACD en la lista


def test_p_elige_el_proyecto_y_uno_que_no_existe_cae_al_primero(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.proyecto(2, "Dos", area="CDS")
    assert titulo_de(ver(mundo, p=1)) == "Uno" and titulo_de(ver(mundo, p=2)) == "Dos"
    assert titulo_de(ver(mundo, p=999)) == "Dos"        # «Dos» va primero por nombre
    assert 'aria-current="true"' in ver(mundo, p=2)


def test_las_rutas_que_vuelven_con_un_aviso_enseñan_el_proyecto_del_que_hablan(mundo):
    mundo.proyecto(1, "Alfa", area="CDS")             # el que se enseña si nada elige
    mundo.proyecto(2, "Zeta", area="CDS")
    mundo.tarea(20, "de Zeta", proyecto=2)
    assert titulo_de(ver(mundo)) == "Alfa"
    for consulta in ({"nombre_guardado": 2}, {"area_guardada": 2}, {"creado": 2},
                     {"tarea_creada": 20}):
        assert titulo_de(ver(mundo, **consulta)) == "Zeta", consulta


def test_las_tareas_sueltas_de_un_grupo_y_de_sin_grupo(mundo):
    _sembrar_los_casos(mundo)
    html = ver(mundo, g="CDS")
    assert "Tareas sin proyecto" in html and tareas_en(html) == [17]
    assert tareas_en(ver(mundo, g="ACD")) == [18]
    assert sorted(tareas_en(ver(mundo, sin_grupo=1))) == [20, 21, 22]


def test_un_grupo_sin_tareas_sueltas_no_ofrece_el_apartado(mundo):
    mundo.proyecto(1, "P", area="CDS")
    html = ver(mundo)
    assert "Tareas sin proyecto" not in html


def test_sin_nada_que_enseñar_lo_dice(mundo):
    assert "Todavía no hay proyectos ni tareas sueltas" in ver(mundo)


# ── El buscador ─────────────────────────────────────────────────────────

def test_el_buscador_filtra_por_proyecto_o_cliente_sin_tildes_ni_mayusculas(mundo):
    mundo.proyecto(1, "Remodelación de la Sala", area="CDS", cliente="Colegio San Juan")
    mundo.proyecto(2, "Grabación del EP", area="CDS", cliente="Banda Los del Patio")
    mundo.proyecto(3, "Taller de verano", area="ACD", cliente="Colegio San Juan")
    por_nombre = ver(mundo, q="REMODELACION")
    # En el HTML el `&` de la URL va como `&amp;`, que el navegador lee como `&`.
    assert 'href="/proyectos?p=1&amp;q=REMODELACION"' in por_nombre
    assert "p=2" not in por_nombre and "p=3" not in por_nombre
    por_cliente = ver(mundo, q="colegio")
    assert "p=1&amp;" in por_cliente and "p=3&amp;" in por_cliente and "p=2&amp;" not in por_cliente
    assert titulo_de(por_cliente) == "Remodelación de la Sala"     # el primero que casa


def test_el_buscador_esconde_los_grupos_vacios_y_los_apartados_de_sueltas(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    mundo.tarea(10, "suelta", area="CDS")
    html = ver(mundo, q="uno")
    lista = html.split("<aside>", 1)[1].split("</aside>", 1)[0]
    assert "ACD" not in lista and "Tareas sin proyecto" not in lista
    assert "Quitar el filtro" in html


def test_sin_coincidencias_lo_dice_y_no_enseña_un_proyecto_cualquiera(mundo):
    mundo.proyecto(1, "Uno", area="CDS")
    html = ver(mundo, q="zzz")
    assert "Ningún proyecto ni cliente con «zzz»" in html and titulo_de(html) is None


def test_lo_que_se_busca_se_escapa_y_se_conserva_en_los_enlaces(mundo):
    mundo.proyecto(1, "Uno <x>", area="CDS")
    html = ver(mundo, q='<x> "y"')
    assert "&lt;x&gt;" in html and "<x>" not in html


# ── La barra y el logo ──────────────────────────────────────────────────

def test_la_barra_lleva_el_logo_y_proyectos_y_los_enlaces_del_menu(mundo):
    html = ver(mundo)
    assert '<img class="logo" src="/logo-cds.png"' in html
    assert '<span class="barra-tag">PROYECTOS</span>' in html
    import web.menu as menu
    for pantalla in menu.pantallas():
        assert f'href="{pantalla.ruta}"' in html, pantalla


def test_el_logo_se_sirve_con_sesion_y_es_el_archivo_del_repo(mundo):
    r = _cliente(config.CHAT_ID_DUENO).get("/logo-cds.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == panel.LOGO_CDS.read_bytes() and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert _cliente(None).get("/logo-cds.png").status_code == 401


def test_sin_sesion_la_pagina_no_se_abre(mundo):
    assert _cliente(None).get("/proyectos").status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# El modelo, sin base
# ═══════════════════════════════════════════════════════════════════════

def test_el_modelo_reparte_cada_tarea_bajo_su_proyecto():
    modelo = modelo_de_filas(
        proyectos=[{"id": 1, "nombre": "A", "area": "CDS"}, {"id": 2, "nombre": "B", "area": "CDS"}],
        tareas=[{"id": 10, "titulo": "de A", "proyecto_id": 1},
                {"id": 11, "titulo": "de B", "proyecto_id": 2}])
    por_nombre = {m["nombre"]: [t["titulo"] for t in m["pendientes"]]
                  for m in modelo["proyectos"].values()}
    assert por_nombre == {"A": ["de A"], "B": ["de B"]}


def test_ultimo_movimiento_es_el_mas_reciente_entre_la_creacion_y_las_huellas():
    creado = datetime(2026, 9, 1, tzinfo=UTC)
    reciente = datetime(2026, 10, 5, tzinfo=UTC)
    assert db.ultimo_movimiento(creado, [reciente, creado]) == reciente
    assert db.ultimo_movimiento(creado, []) == creado
    # Un instante sin zona se lee como UTC y no revienta al compararlo.
    assert db.ultimo_movimiento(creado, [datetime(2026, 10, 6)]) == datetime(2026, 10, 6)


# ── Los hermanos de la página: quien lleva a /proyectos ─────────────────

def test_todo_enlace_o_redireccion_a_un_proyecto_dice_cual_proyecto_es():
    """La página enseña un proyecto a la vez y lo elige con la URL. Un enlace a
    `/proyectos#proyecto-N` (como los de antes) abriría el primero de la lista y
    no el N. LA LISTA SALE DE LOS ARCHIVOS: todo texto de `web/` (plantillas y
    rutas) que lleve `/proyectos` y `#proyecto-`. Los parámetros que eligen el
    proyecto son los que lee `_vista_de_proyectos` (frontera: esa lista es
    de este archivo)."""
    eligen = ("p=", "nombre_guardado=", "area_guardada=", "creado=", "tarea_creada=")
    hallados = []
    for archivo in sorted(a for a in g._archivos_de_texto()
                          if a.relative_to(g._ROOT).parts[0] == "web"):
        if archivo.suffix == ".html":
            for n, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
                hallados += [(archivo.name, n, m.group(0))
                             for m in re.finditer(r"/proyectos[^\"']*#proyecto-", linea)]
        elif archivo.suffix == ".py":
            # Con `ast`: las cadenas pegadas (`f"/proyectos?x"  f"#proyecto-{pid}"`)
            # son UN solo texto para el parser, y un `{...}` queda como `{}`.
            for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
                if isinstance(nodo, ast.JoinedStr):
                    texto = "".join(v.value if isinstance(v, ast.Constant) else "{}"
                                    for v in nodo.values)
                elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                    texto = nodo.value
                else:
                    continue
                if "/proyectos" in texto and "#proyecto-" in texto:
                    hallados.append((archivo.name, nodo.lineno, texto))
    assert len(hallados) >= 8, f"el barrido encontró {len(hallados)} enlaces: ¿se rompió?"
    sin_proyecto = [h for h in hallados if not any(e in h[2] for e in eligen)]
    assert sin_proyecto == [], sin_proyecto
