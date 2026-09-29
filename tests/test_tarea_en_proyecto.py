# -*- coding: utf-8 -*-
"""Agregar tareas con todas sus características desde la pantalla de un
proyecto (pieza 2 del diseño «proyectos», pedido de Tiziano del 29-sep-2026).

CÓMO SE PRUEBA:
  · Las rutas reales (`panel.tarea_nueva`, `panel.crear_tarea`, `panel.proyectos`,
    `panel.guardar_tareas`) con SUS PLANTILLAS de verdad, y las funciones reales
    (`db.crear_tarea_desde_el_panel`, `db.cerrar_y_derivar`,
    `crud.crear_desde_interpretacion`) contra una base sqlite que EJECUTA el SQL
    del repositorio, con el CHECK `tareas_area_no_con_proyecto` creado igual que
    en `db/schema.sql`. Límites dichos: sqlite no es Postgres, no modela las FK,
    y la consulta que lista las tareas de cada proyecto (`= ANY(%s)`) no corre en
    sqlite: la pantalla `/proyectos` se pinta con la lista que arma esta prueba
    A PARTIR de lo que el escritor de verdad dejó en la base.
  · La puerta del proyecto (`db.proyecto_admite_tareas`) se prueba en sus DOS
    capas (la ruta y el escritor), y quien crea tareas con proyecto sale de un
    censo del AST del repositorio, no de una lista.

Correr:  python3 -m pytest tests/test_tarea_en_proyecto.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_alta_con_responsable as A  # noqa: E402
import test_nombre_de_proyecto as N  # noqa: E402
from test_alta_con_responsable import base  # noqa: E402
import test_responsable as tr  # noqa: E402
import test_buzon_que_no_se_ve as barrido  # noqa: E402

import config  # noqa: E402
import db.db as db  # noqa: E402
import acciones.crud as crud  # noqa: E402
import web.app as panel  # noqa: E402

DUENO, OTRA = A.DUENO, A.OTRA
CODE = config.CHAT_ID_CODE
TEC = db.AREA_TECNICA


# ── La base: el esquema real de `tareas`, con su CHECK ───────────────────

class Mundo(N.Base):
    def __init__(self):
        super().__init__()
        self.con.executescript("""
            DROP TABLE tareas;
            CREATE TABLE tareas (
              id INTEGER PRIMARY KEY, bandeja_id,
              creado_en DEFAULT CURRENT_TIMESTAMP, titulo TEXT NOT NULL,
              detalle, vence_en, recurrencia, prioridad, proyecto_id,
              persona_id, responsable_chat_id INTEGER,
              estado TEXT NOT NULL DEFAULT 'pendiente', anticipos_min, area,
              borrado_en, primero_id, deriva_de_id, tomada_en, completado_en,
              CHECK (proyecto_id IS NULL OR area IS NULL),
              CHECK (primero_id IS NULL OR primero_id <> id));
            CREATE TABLE bandeja (id INTEGER PRIMARY KEY, origen, tipo_entrada,
              chat_id, estado, procesado_en);
            CREATE TABLE personas (id INTEGER PRIMARY KEY, nombre, borrado_en);
            CREATE TABLE areas (clave TEXT PRIMARY KEY, color, orden INT DEFAULT 0);
        """)
        self.con.execute("INSERT INTO areas (clave, color) VALUES (?, '#111')", (TEC,))
        self.con.execute("INSERT INTO areas (clave, color) VALUES ('CDS', '#222')")

    def proyecto(self, nombre, area=None, estado="activo", borrado=False):
        c = self.con.execute(
            "INSERT INTO proyectos (nombre, area, estado, borrado_en) VALUES (?,?,?,?)",
            (nombre, area, estado, "2026-09-01" if borrado else None))
        return c.lastrowid

    def persona(self, nombre, borrada=False):
        return self.con.execute(
            "INSERT INTO personas (nombre, borrado_en) VALUES (?, ?)",
            (nombre, "2026-09-01" if borrada else None)).lastrowid

    def tarea(self, titulo, borrada=False, proyecto=None, estado="pendiente"):
        return self.con.execute(
            "INSERT INTO tareas (titulo, borrado_en, proyecto_id, estado) VALUES (?,?,?,?)",
            (titulo, "2026-09-01" if borrada else None, proyecto, estado)).lastrowid

    def filas(self):
        self.con.row_factory = sqlite3.Row
        try:
            return [dict(f) for f in self.con.execute("SELECT * FROM tareas ORDER BY id")]
        finally:
            self.con.row_factory = None

    def n_tareas(self):
        return self.con.execute("SELECT count(*) FROM tareas").fetchone()[0]

    def n_bandeja(self):
        return self.con.execute("SELECT count(*) FROM bandeja").fetchone()[0]


def _crear(m, campos):
    return N._correr(m, lambda: panel.crear_tarea(base._post(campos)))


def _alta(m, **kw):
    html = N._correr(m, lambda: panel.tarea_nueva(
        base._get("/tareas/nueva"), **kw)).body.decode()
    return " ".join(html.split())    # los saltos de línea de la plantilla no cuentan


def _destino(r):
    return r.headers["location"]


def _uno():
    A._casa()
    m = Mundo()
    return m, m.proyecto("Disco", area="CDS")


# ── El camino feliz: todas las características ───────────────────────────

def test_una_tarea_con_todas_sus_caracteristicas_queda_en_el_proyecto():
    m, pid = _uno()
    otra = m.tarea("una anterior")
    ana = m.persona("Ana")
    r = _crear(m, {"titulo": "mezclar", "vence": base.panel._hoy().isoformat(),
                   "proyecto": str(pid), "responsable": str(OTRA),
                   "primero_id": str(otra), "persona": str(ana),
                   "detalle": "linea 1\r\nlinea 2"})
    fila = m.filas()[-1]
    assert _destino(r) == f"/proyectos?tarea_creada={fila['id']}#proyecto-{pid}", (
        "al guardar no vuelve al proyecto con el aviso de lo creado")
    assert (fila["proyecto_id"], fila["area"], fila["responsable_chat_id"],
            fila["primero_id"], fila["persona_id"], fila["estado"]) == (
        pid, None, OTRA, otra, ana, "pendiente")
    assert fila["detalle"] == "linea 1\nlinea 2", "el detalle no se normalizó"
    assert json.loads(fila["anticipos_min"]) == [], "nació con avisos de Telegram"
    (huella,) = [h for h in m.huellas() if h[2] == "tareas"]
    assert huella[1] == "crear" and huella[0] == "panel"
    assert json.loads(huella[5])["proyecto_id"] == pid


def test_con_proyecto_el_area_que_llegue_se_ignora_y_no_revienta_el_check():
    m, pid = _uno()
    _crear(m, {"titulo": "x", "proyecto": str(pid), "area": "CDS"})
    assert m.filas()[-1]["area"] is None and m.filas()[-1]["proyecto_id"] == pid


def test_sin_proyecto_el_alta_es_la_de_siempre():
    m, _ = _uno()
    r = _crear(m, {"titulo": "suelta", "area": "CDS"})
    fila = m.filas()[-1]
    assert (fila["proyecto_id"], fila["area"]) == (None, "CDS")
    assert _destino(r) == f"/tareas?creada={fila['id']}"


def test_un_proyecto_pausado_si_recibe_tareas():
    A._casa()
    m = Mundo()
    pid = m.proyecto("En pausa", estado="pausado")
    _crear(m, {"titulo": "x", "proyecto": str(pid)})
    assert m.filas()[-1]["proyecto_id"] == pid


# ── LA PUERTA DEL PROYECTO, en sus dos capas ─────────────────────────────

def _invalidos(m):
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    borrado = m.proyecto("Borrado", borrado=True)
    return [(str(cerrado), "proyecto_cerrado"), (str(borrado), "proyecto"),
            ("9999", "proyecto"), ("abc", "proyecto"), ("0", "proyecto"),
            ("-3", "proyecto"), ("1e3", "proyecto"), ("+2", "proyecto"),
            ("02", "proyecto"), ("1_0", "proyecto")]


def test_la_ruta_rechaza_un_proyecto_que_no_vale_y_no_escribe_nada():
    m, _ = _uno()
    for crudo, clave in _invalidos(m):
        r = _crear(m, {"titulo": "x", "proyecto": crudo})
        assert _destino(r) == f"/tareas/nueva?error={clave}", (crudo, _destino(r))
    assert m.n_tareas() == 0 and m.n_bandeja() == 0, "escribió con un proyecto que no vale"


def test_la_ruta_mira_el_proyecto_ANTES_que_lo_demas():
    """Con el proyecto malo Y el título vacío, el motivo que se dice es el del
    proyecto: es la primera puerta. (Quita la de la ruta y esto se pone roja;
    la del escritor, en cambio, la cubre la prueba de abajo.)"""
    m, _ = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    r = _crear(m, {"titulo": "", "proyecto": str(cerrado)})
    assert _destino(r) == "/tareas/nueva?error=proyecto_cerrado"


def test_el_escritor_revalida_el_proyecto_por_su_cuenta_y_no_escribe():
    m, _ = _uno()
    malos = [int(c) for c, _ in _invalidos(m) if c.isdigit() and int(c) > 0]
    for pid in malos + [True, "3", 1.5]:
        e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
            DUENO, "x", None, proyecto_id=pid))
        assert isinstance(e, db.ProyectoNoAdmiteTareas), f"aceptó {pid!r}"
    assert m.n_tareas() == 0 and m.n_bandeja() == 0


def test_proyecto_cerrado_da_su_clave_y_los_otros_no_existe():
    m, _ = _uno()
    cerrado = m.proyecto("C", estado="cerrado")
    e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, proyecto_id=cerrado))
    assert e.clave == "cerrado" and "cerrado" in str(e)
    e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, proyecto_id=4242))
    assert e.clave == "no_existe"


# ── Persona, Primero y Detalle: entradas inventadas ──────────────────────

def test_persona_que_no_vale_se_rechaza_sin_escribir():
    m, pid = _uno()
    borrada = m.persona("Vieja", borrada=True)
    for crudo in (str(borrada), "9999", "abc", "-1", "0", "1.5"):
        r = _crear(m, {"titulo": "x", "proyecto": str(pid), "persona": crudo})
        assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=persona", crudo
    assert m.n_tareas() == 0 and m.n_bandeja() == 0
    e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, proyecto_id=pid, persona_id=borrada))
    assert isinstance(e, ValueError) and m.n_tareas() == 0


def test_primero_que_no_vale_se_rechaza_pero_uno_de_otro_proyecto_si_vale():
    m, pid = _uno()
    borrada = m.tarea("borrada", borrada=True)
    for crudo in (str(borrada), "9999", "abc", "-1"):
        r = _crear(m, {"titulo": "x", "proyecto": str(pid), "primero_id": crudo})
        assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=primero", crudo
    assert m.n_tareas() == 1 and m.n_bandeja() == 0     # solo la «borrada» de arriba
    # Una tarea de OTRO proyecto (o suelta) sí puede ser el «Primero:»: es lo
    # que ya deja hacer Telegram; se dice acá en vez de suponerlo.
    ajeno = m.proyecto("Otro proyecto")
    de_otro = m.tarea("de otro proyecto", proyecto=ajeno)
    _crear(m, {"titulo": "y", "proyecto": str(pid), "primero_id": str(de_otro)})
    assert m.filas()[-1]["primero_id"] == de_otro


def test_detalle_con_tope_y_vacio_es_ninguno():
    m, pid = _uno()
    largo = "d" * (panel.LARGO_DETALLE + 1)
    r = _crear(m, {"titulo": "x", "proyecto": str(pid), "detalle": largo})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=detalle"
    assert m.n_tareas() == 0
    _crear(m, {"titulo": "x", "proyecto": str(pid), "detalle": "d" * 2000})
    assert len(m.filas()[-1]["detalle"]) == 2000
    _crear(m, {"titulo": "y", "proyecto": str(pid), "detalle": "   \r\n "})
    assert m.filas()[-1]["detalle"] is None


def test_los_rechazos_del_proyecto_vuelven_al_formulario_con_su_proyecto():
    m, pid = _uno()
    r = _crear(m, {"titulo": "", "proyecto": str(pid)})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=titulo"


# ── Responsable por la lista única; Code y la sala ───────────────────────

def test_responsable_que_no_vale_se_rechaza_tambien_dentro_del_proyecto():
    m, pid = _uno()
    for crudo in (str(A.AJENO), "abc", "0" + str(OTRA)):
        r = _crear(m, {"titulo": "x", "proyecto": str(pid), "responsable": crudo})
        assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=responsable"
    assert m.n_tareas() == 0
    e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, responsable_chat_id=A.AJENO, proyecto_id=pid))
    assert isinstance(e, ValueError) and m.n_tareas() == 0


def test_code_en_un_proyecto_no_tecnico_se_permite_y_el_aviso_es_verdad():
    """La tarea se guarda con `area = NULL` (nunca el área técnica: el CHECK lo
    prohíbe con proyecto), y el aviso «la sala NO la verá» tiene que ser cierto:
    se contrasta con la consulta REAL de la sala ejecutada en sqlite."""
    A._casa()
    m = Mundo()
    cds = m.proyecto("De CDS", area="CDS")
    tec = m.proyecto("Técnico", area=TEC)
    sin = m.proyecto("Sin área", area=None)
    avisos = {}
    for nombre, pid in (("cds", cds), ("tec", tec), ("sin", sin)):
        r = _crear(m, {"titulo": nombre, "proyecto": str(pid),
                       "responsable": str(CODE)})
        fila = m.filas()[-1]
        assert (fila["responsable_chat_id"], fila["area"]) == (CODE, None)
        avisos[nombre] = "sala_no=1" in _destino(r)
    assert avisos == {"cds": True, "tec": False, "sin": True}, avisos
    # La sala, de verdad:
    vistas = {t["titulo"] for t in N._correr(m, db.tareas_de_code_pendientes)}
    assert vistas == {nombre for nombre, dice in avisos.items() if not dice}, (
        f"el aviso no coincide con lo que la sala ve: {vistas}")


def test_sala_ve_coincide_con_la_consulta_de_la_sala_para_toda_combinacion():
    A._casa()
    m = Mundo()
    esperado = {}
    for resp in (None, OTRA, CODE):
        for area in (None, "CDS", TEC):
            pid = m.proyecto(f"p-{resp}-{area}", area=area)
            tid = m.con.execute(
                "INSERT INTO tareas (titulo, proyecto_id, responsable_chat_id) "
                "VALUES (?,?,?)", (f"t-{resp}-{area}", pid, resp)).lastrowid
            esperado[tid] = db.sala_ve(resp, area)
    m.con.execute("ALTER TABLE tareas ADD COLUMN _x")
    vistas = {t["id"] for t in N._correr(m, db.tareas_de_code_pendientes)}
    assert vistas == {t for t, v in esperado.items() if v}, vistas


def test_code_sin_proyecto_sigue_poniendo_el_area_tecnica():
    m, _ = _uno()
    _crear(m, {"titulo": "suelta de Code", "responsable": str(CODE)})
    assert m.filas()[-1]["area"] == TEC


# ── LAS PANTALLAS, con su plantilla ──────────────────────────────────────

def test_la_pantalla_de_alta_en_proyecto_trae_todos_los_campos_y_no_el_area():
    A._casa()
    m = Mundo()
    pid = m.proyecto('Disco "><script>x</script>', area="CDS")
    m.persona("Ana")
    m.tarea("primera")
    html = _alta(m, proyecto=str(pid))
    assert "<script>x</script>" not in html, "el nombre salió sin escapar"
    assert "Nueva tarea en «Disco" in html
    assert f'name="proyecto" value="{pid}"' in html
    assert 'name="area"' not in html, "ofrece elegir un área con proyecto"
    assert "hereda la del proyecto" in html and "CDS" in html
    for campo in ("titulo", "vence", "responsable", "primero_id", "persona",
                  "detalle"):
        assert f'name="{campo}"' in html, f"falta el campo {campo}"
    assert ">primera (pendiente)<" in html, "«Primero» no lista las tareas reales"
    assert ">Ana<" in html, "«De quién trata» no lista las personas reales"
    assert "prioridad, repetición ni avisos por Telegram" in html, (
        "no dice lo que esta pantalla no pone")
    assert f'href="/proyectos#proyecto-{pid}"' in html
    assert html.count("<form") == 1


def test_sin_proyecto_la_pantalla_de_alta_es_la_de_siempre():
    A._casa()
    m = Mundo()
    html = _alta(m)
    assert 'name="area"' in html and 'name="proyecto"' not in html
    assert 'name="primero_id"' not in html and 'name="detalle"' not in html
    assert "Nueva tarea en" not in html


def test_un_proyecto_que_no_vale_abre_la_pantalla_sin_proyecto_y_lo_dice():
    A._casa()
    m = Mundo()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    for crudo, dice in ((str(cerrado), "está cerrado"), ("9999", "no existe"),
                        ("abc", "no existe")):
        html = _alta(m, proyecto=crudo)
        assert dice in html and 'name="proyecto"' not in html, crudo
        assert "SIN proyecto" in html


def test_el_aviso_de_la_sala_sale_solo_si_de_verdad_no_la_verá():
    A._casa()
    m = Mundo()
    cds = m.proyecto("De CDS", area="CDS")
    tec = m.proyecto("Técnico", area=TEC)
    sin = m.proyecto("Sin área")
    assert "NO verá esta tarea" in _alta(m, proyecto=str(cds))
    assert "NO verá esta tarea" in _alta(m, proyecto=str(sin))
    assert "la verá" in _alta(m, proyecto=str(tec))
    assert "NO verá esta tarea" not in _alta(m, proyecto=str(tec))


def test_cada_clave_de_rechazo_del_alta_tiene_su_aviso_y_una_inventada_no():
    """Las claves salen del CÓDIGO de la ruta (`_vuelta("…")` y los redirects
    `error=proyecto…`), no de una lista escrita acá."""
    fuente = Path(base.RAIZ, "web", "app.py").read_text(encoding="utf-8")
    ruta = next(n for n in ast.walk(ast.parse(fuente))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "crear_tarea")
    claves = {c.args[0].value for c in ast.walk(ruta)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "_vuelta" and c.args
              and isinstance(c.args[0], ast.Constant)}
    claves |= {"proyecto", "proyecto_cerrado"}
    assert {"titulo", "fecha", "area", "responsable", "primero", "persona",
            "detalle"} <= claves, claves
    A._casa()
    m = Mundo()
    for clave in claves:
        assert 'class="aviso"' in _alta(m, error=clave), f"{clave} sin aviso"
    assert 'class="aviso"' not in _alta(m, error="inventada")


def _lista_de_proyectos(m):
    """Lo que devolvería `db.proyectos_con_tareas`, armado con lo que de verdad
    hay en la base (esa consulta usa `= ANY(%s)`, que sqlite no tiene)."""
    m.con.row_factory = sqlite3.Row
    try:
        proyectos = [dict(f) for f in m.con.execute(
            "SELECT * FROM proyectos WHERE borrado_en IS NULL ORDER BY id")]
        for p in proyectos:
            p["color"] = None
            p["tareas"] = [dict(f) for f in m.con.execute(
                "SELECT * FROM tareas WHERE proyecto_id = ? AND borrado_en IS NULL",
                (p["id"],))]
    finally:
        m.con.row_factory = None
    return proyectos


def _pintar_proyectos(m, **kw):
    lista = _lista_de_proyectos(m)

    async def _con_tareas():
        return lista

    async def _areas():
        return []
    g = (db.proyectos_con_tareas, db.areas)
    db.proyectos_con_tareas, db.areas = _con_tareas, _areas
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(
                panel.proyectos(base._get("/proyectos"), **kw)).body.decode()
        finally:
            bucle.close()
    finally:
        db.proyectos_con_tareas, db.areas = g


def test_cada_proyecto_tiene_su_enlace_y_el_cerrado_dice_por_que_no():
    A._casa()
    m = Mundo()
    activo = m.proyecto("Activo")
    pausado = m.proyecto("Pausado", estado="pausado")
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    html = _pintar_proyectos(m)
    for pid in (activo, pausado):
        assert f'href="/tareas/nueva?proyecto={pid}"' in html, pid
    assert f'href="/tareas/nueva?proyecto={cerrado}"' not in html
    assert "Proyecto cerrado: no se le agregan tareas." in html
    # De punta a punta: cada enlace lleva a la pantalla de ESE proyecto.
    for pid, nombre in ((activo, "Activo"), (pausado, "Pausado")):
        href = re.search(r'href="(/tareas/nueva\?proyecto=%d)"' % pid, html).group(1)
        destino = parse_qs(urlparse(href).query)["proyecto"][0]
        assert f"Nueva tarea en «{nombre}»" in _alta(m, proyecto=destino)


def test_lo_creado_se_ve_en_el_proyecto_y_el_aviso_dice_lo_que_paso():
    A._casa()
    m = Mundo()
    pid = m.proyecto("Disco", area="CDS")
    r = _crear(m, {"titulo": "mezclar el bajo", "proyecto": str(pid),
                   "responsable": str(CODE)})
    q = parse_qs(urlparse(_destino(r)).query)
    tid = int(q["tarea_creada"][0])
    html = _pintar_proyectos(m, tarea_creada=tid, sala_no=int(q["sala_no"][0]))
    assert f"Tarea creada (#{tid})" in html and "mezclar el bajo" in html
    assert "la sala de control NO la verá" in html and "Code" in html
    sin_sala = _pintar_proyectos(m, tarea_creada=tid)
    assert "NO la verá" not in sin_sala


# ── LOS HERMANOS: todo lo que crea tareas con proyecto ───────────────────

_PUERTAS = {"proyecto_admite_tareas", "proyecto_para_tareas"}
_INSERTA_CON_PROYECTO = re.compile(r"INSERT\s+INTO\s+tareas", re.I)


def _llamadas(nodo) -> set:
    salida = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                salida.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                salida.add(n.func.attr)
    return salida


def test_todo_lo_que_crea_tareas_con_proyecto_llama_a_la_puerta_del_proyecto():
    """El censo mira todo `execute` del repo (misma frontera que el de
    `test_responsable.py`, cuyas piezas usa): SQL legible que hace
    `INSERT INTO tareas` y nombra `proyecto_id`. Cada función encontrada tiene
    que LLAMAR a la puerta (nombrarla no basta). FRONTERA: no ve una llamada en
    una rama que nunca corre; eso lo miden las pruebas de comportamiento de
    abajo, una por escritor."""
    raiz = Path(base.RAIZ).resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    encontrados = {}
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        rel = real.relative_to(raiz).as_posix()
        modulo = ast.parse(real.read_text(encoding="utf-8"), str(real))
        globales = tr._asignaciones(modulo)
        for funcion, llamada in tr._llamadas_a_execute(modulo):
            sql = tr._sql_de(llamada)
            if sql is None or funcion is None:
                continue
            piezas = tr._piezas(sql, tr._asignaciones(funcion), globales,
                                tr._parametros(funcion))
            legible, texto = tr._legible(piezas)
            if (legible and _INSERTA_CON_PROYECTO.search(texto)
                    and re.search(r"\bproyecto_id\b", texto)):
                encontrados[f"{rel}::{funcion.name}"] = funcion
    for fn in (db.crear_tarea_desde_el_panel, db.cerrar_y_derivar,
               crud.crear_desde_interpretacion):
        assert tr._id_de(fn) in encontrados, (
            f"el censo no ve a {tr._id_de(fn)}: dejó de ver")
    sin_puerta = sorted(q for q, f in encontrados.items()
                        if not (_PUERTAS & _llamadas(f)))
    assert not sin_puerta, f"crean tareas con proyecto sin llamar a la puerta: {sin_puerta}"


def _mundo_con_madre(estado_proyecto):
    A._casa()
    m = Mundo()
    pid = m.proyecto("P", area="CDS", estado=estado_proyecto)
    madre = m.tarea("madre", proyecto=pid)
    return m, pid, madre


def test_derivadas_en_proyecto_cerrado_no_cierran_ni_crean_nada():
    m, pid, madre = _mundo_con_madre("cerrado")
    e = N._rechazo(m, lambda: db.cerrar_y_derivar(
        DUENO, madre, [{"titulo": "hija", "vence_en": None, "area": None,
                        "responsable_chat_id": None}]))
    assert isinstance(e, db.ProyectoNoAdmiteTareas)
    assert m.n_tareas() == 1 and m.filas()[0]["estado"] == "pendiente"
    # Sin derivadas, cerrar una tarea de un proyecto cerrado SÍ se puede.
    res = N._correr(m, lambda: db.cerrar_y_derivar(DUENO, madre, []))
    assert res == (True, []) and m.filas()[0]["estado"] == "hecha"


def test_derivadas_en_proyecto_pausado_si_se_crean_y_heredan_el_proyecto():
    m, pid, madre = _mundo_con_madre("pausado")
    _, hijas = N._correr(m, lambda: db.cerrar_y_derivar(
        DUENO, madre, [{"titulo": "hija", "vence_en": None, "area": "CDS",
                        "responsable_chat_id": None}]))
    hija = m.filas()[-1]
    assert (hija["id"], hija["proyecto_id"], hija["area"]) == (hijas[0], pid, None)


def test_la_ruta_de_guardar_dice_sin_efecto_y_no_cierra_con_derivada_en_cerrado():
    m, pid, madre = _mundo_con_madre("cerrado")
    r = N._correr(m, lambda: panel.guardar_tareas(base._post({
        f"prev_{madre}": "pendiente", f"hecha_{madre}": "1",
        f"deriva_titulo_{madre}_1": "hija"})))
    assert r.status_code == 303 and "guardadas=0" in _destino(r), _destino(r)
    assert "derivadas=0" in _destino(r)
    assert m.filas()[0]["estado"] == "pendiente" and m.n_tareas() == 1


def _crear_por_telegram(m, proyecto):
    async def f():
        return await crud.crear_desde_interpretacion(
            1, {"clasificacion": "tarea", "titulo": "por telegram",
                "proyecto": proyecto})
    return N._rechazo(m, f)


def test_telegram_no_crea_tareas_en_un_proyecto_cerrado_y_lo_dice():
    A._casa()
    m = Mundo()
    m.proyecto("Cerrado", estado="cerrado")
    e = _crear_por_telegram(m, "cerrado")
    assert isinstance(e, ValueError) and str(e).startswith("No creé la tarea:")
    assert "cerrado" in str(e)
    assert m.n_tareas() == 0, "creó la tarea, suelta o no"


def test_telegram_si_crea_en_un_proyecto_pausado_con_area_nula():
    A._casa()
    m = Mundo()
    pid = m.proyecto("En pausa", area="CDS", estado="pausado")
    assert _crear_por_telegram(m, "en pausa") is None
    fila = m.filas()[-1]
    assert (fila["proyecto_id"], fila["area"]) == (pid, None)


def test_convertir_una_tarea_no_agrega_tareas_a_ningun_proyecto():
    """`convertir_tarea_en_proyecto` crea un proyecto y archiva la tarea: no
    inserta ninguna tarea, así que no pasa por la puerta (y el censo de arriba no
    lo encuentra: solo mira los `INSERT INTO tareas`)."""
    A._casa()
    m = Mundo()
    t = m.tarea("Nuevo proyecto")
    antes = m.n_tareas()
    N._correr(m, lambda: db.convertir_tarea_en_proyecto(t))
    assert m.n_tareas() == antes


def test_el_insert_de_respaldo_sin_columna_area_tambien_guarda_todo():
    """Si `tareas.area` no existe (migración sin aplicar, SQLSTATE 42703) el
    escritor cae al segundo INSERT: tiene que llevar también el proyecto, el
    «Primero:», el detalle y la persona (sqlite: la tabla sin `area`)."""
    A._casa()
    m = Mundo()
    m.con.executescript("""
        DROP TABLE tareas;
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, bandeja_id, creado_en DEFAULT CURRENT_TIMESTAMP,
          titulo TEXT NOT NULL, detalle, vence_en, proyecto_id, persona_id,
          responsable_chat_id INTEGER, estado TEXT NOT NULL DEFAULT 'pendiente',
          anticipos_min, borrado_en, primero_id);
    """)
    pid = m.proyecto("P", area="CDS")
    ana = m.persona("Ana")
    otra = m.tarea("otra")
    tid = N._correr(m, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, None, OTRA, proyecto_id=pid, primero_id=otra,
        detalle="d", persona_id=ana))
    fila = m.filas()[-1]
    assert (fila["id"], fila["proyecto_id"], fila["primero_id"], fila["detalle"],
            fila["persona_id"], fila["responsable_chat_id"]) == (
        tid, pid, otra, "d", ana, OTRA)


def test_el_escritor_por_su_cuenta_deja_area_nula_con_proyecto_pida_lo_que_pida():
    """Llamado directo (sin la ruta), con un área pedida y aun con Code: el
    CHECK de la base prohíbe «proyecto + área», así que el escritor guarda NULL."""
    m, pid = _uno()
    for area, resp in (("CDS", None), (TEC, None), ("CDS", CODE), (None, CODE)):
        tid = N._correr(m, lambda: db.crear_tarea_desde_el_panel(
            DUENO, "x", None, area, resp, proyecto_id=pid))
        fila = [f for f in m.filas() if f["id"] == tid][0]
        assert (fila["proyecto_id"], fila["area"], fila["responsable_chat_id"]) == (
            pid, None, resp), (area, resp)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
