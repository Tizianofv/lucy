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
import inspect
import textwrap
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
import cerebro.agente as agente  # noqa: E402
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
_ESCRIBE_TAREAS = re.compile(r"(?:INSERT\s+INTO|UPDATE)\s+tareas\b", re.I)


def _llamadas(nodo) -> set:
    salida = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                salida.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                salida.add(n.func.attr)
    return salida


def _esperadas(nodo) -> set:
    """Los nombres de las funciones que un nodo LLAMA Y ESPERA (`await f(...)`).
    Las puertas son corrutinas: llamarlas sin `await` no las corre (la corrutina
    se descarta) y el «chequeo» no chequea nada."""
    salida = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            f = n.value.func
            if isinstance(f, ast.Name):
                salida.add(f.id)
            elif isinstance(f, ast.Attribute):
                salida.add(f.attr)
    return salida


def test_todo_lo_que_crea_o_mueve_tareas_de_proyecto_espera_a_la_puerta_del_proyecto():
    """El censo mira todo `execute` del repo (misma frontera que el de
    `test_responsable.py`, cuyas piezas usa): SQL legible que hace
    `INSERT INTO tareas` o `UPDATE tareas` y nombra `proyecto_id`, más los
    escritores GENÉRICOS (`crud.editar` y `crud.deshacer`, que arman su UPDATE al
    vuelo: mover una tarea a un proyecto es «recibirla»). Cada uno tiene que
    LLAMAR a la puerta Y ESPERARLA (`await`): nombrarla o llamarla sin `await` no
    la corre. FRONTERA: no ve una llamada en una rama que nunca corre; eso lo
    miden las pruebas de comportamiento, una por escritor."""
    raiz = Path(base.RAIZ).resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    encontrados = {}
    genericos = {}
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
            if not legible:
                if not tr._ejecuta_solo_lectura(funcion):
                    genericos[f"{rel}::{funcion.name}"] = funcion
            elif _ESCRIBE_TAREAS.search(texto) and re.search(r"\bproyecto_id\b", texto):
                encontrados[f"{rel}::{funcion.name}"] = funcion
    for fn in (db.crear_tarea_desde_el_panel, db.cerrar_y_derivar,
               crud.crear_desde_interpretacion):
        assert tr._id_de(fn) in encontrados, (
            f"el censo no ve a {tr._id_de(fn)}: dejó de ver")
    for fn in (crud.editar, crud.deshacer):
        assert tr._id_de(fn) in genericos, f"el censo no ve al genérico {fn.__name__}"
        encontrados[tr._id_de(fn)] = genericos[tr._id_de(fn)]
    # `deshacer` llega a la puerta por su ayudante `_recibir_al_deshacer`, que a
    # su vez tiene que ESPERAR a la puerta (se exige abajo).
    assert _PUERTAS & _esperadas(ast.parse(textwrap.dedent(
        inspect.getsource(crud._recibir_al_deshacer)))), (
        "`_recibir_al_deshacer` no espera a la puerta del proyecto")
    sin_puerta = sorted(
        q for q, f in encontrados.items()
        if not ((_PUERTAS | ({"_recibir_al_deshacer"} if q.endswith("::deshacer")
                             else set())) & _esperadas(f)))
    assert not sin_puerta, (
        f"escriben tareas con proyecto sin ESPERAR a la puerta (llamarla sin "
        f"`await` no la corre): {sin_puerta}")


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


def test_el_escritor_revalida_el_primero_por_su_cuenta():
    m, pid = _uno()
    borrada = m.tarea("borrada", borrada=True)
    antes = m.n_tareas()
    for primero in (borrada, 9999):
        e = N._rechazo(m, lambda: db.crear_tarea_desde_el_panel(
            DUENO, "x", None, proyecto_id=pid, primero_id=primero))
        assert isinstance(e, ValueError), primero
    assert m.n_tareas() == antes and m.n_bandeja() == 0


# ── 1. Mover una tarea a un proyecto es «recibirla» ──────────────────────

def _mover(m, tid, destino):
    return lambda: crud.editar("tareas", tid, {"proyecto_id": destino},
                               motivo="prueba")


def test_editar_no_mueve_una_tarea_a_un_proyecto_que_no_la_recibe():
    m, pid = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    borrado = m.proyecto("Borrado", borrado=True)
    t = m.tarea("suelta")
    for destino in (cerrado, borrado, 9999, str(cerrado), "abc", True):
        e = N._rechazo(m, _mover(m, t, destino))
        assert isinstance(e, ValueError), f"movió a {destino!r}"
        assert str(e).startswith("No cambié nada:"), str(e)
        assert m.filas()[0]["proyecto_id"] is None, destino
    assert not [q for q in m.sql if q.startswith("UPDATE tareas")]


def test_editar_si_mueve_a_un_proyecto_activo_o_pausado_y_deja_sacarla():
    m, pid = _uno()
    pausado = m.proyecto("Pausado", estado="pausado")
    t = m.tarea("suelta")
    N._correr(m, _mover(m, t, str(pid)))               # el id como texto
    assert m.filas()[0]["proyecto_id"] == pid and m.filas()[0]["area"] is None
    N._correr(m, _mover(m, t, pausado))
    assert m.filas()[0]["proyecto_id"] == pausado
    N._correr(m, lambda: crud.editar("tareas", t, {"proyecto_id": None},
                                     motivo="prueba"))
    assert m.filas()[0]["proyecto_id"] is None


def test_una_tarea_que_ya_esta_en_un_proyecto_cerrado_se_sigue_editando():
    """No cambia de proyecto: no «recibe» nada. Ni siquiera reenviando su
    proyecto actual junto con otro cambio."""
    m, pid = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    t = m.tarea("ya estaba", proyecto=cerrado)
    N._correr(m, lambda: crud.editar("tareas", t, {"titulo": "renombrada"},
                                     motivo="prueba"))
    N._correr(m, lambda: crud.editar(
        "tareas", t, {"proyecto_id": cerrado, "titulo": "otra vez"},
        motivo="prueba"))
    assert m.filas()[0]["titulo"] == "otra vez"
    assert m.filas()[0]["proyecto_id"] == cerrado


def test_por_telegram_mover_a_un_proyecto_cerrado_dice_el_motivo():
    m, pid = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    t = m.tarea("suelta")
    r, acciones = N._correr(m, lambda: agente._ejecutar_herramienta(
        "editar", {"tabla": "tareas", "id": t,
                   "cambios": {"proyecto_id": cerrado}}, 1, [])), None
    assert r.startswith("ERROR") and "cerrado" in r, r
    assert m.filas()[0]["proyecto_id"] is None


def test_deshacer_no_devuelve_una_tarea_a_un_proyecto_que_ya_se_cerro():
    m, pid = _uno()
    otro = m.proyecto("Otro")
    t = m.tarea("de disco", proyecto=pid)
    N._correr(m, _mover(m, t, otro))                   # se muda a «Otro»
    log_id = N._ultimo_log(m)
    m.con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = ?", (pid,))
    e = N._rechazo(m, N._deshacer(m, log_id))
    assert isinstance(e, ValueError) and str(e).startswith("No lo deshice:")
    assert "cerrado" in str(e)
    assert m.filas()[0]["proyecto_id"] == otro, "la devolvió a un proyecto cerrado"
    # Con el proyecto abierto, sí la devuelve.
    m.con.execute("UPDATE proyectos SET estado = 'activo' WHERE id = ?", (pid,))
    N._correr(m, N._deshacer(m, log_id))
    assert m.filas()[0]["proyecto_id"] == pid


# ── 2. La pantalla dice cuál derivada no se cerró ────────────────────────

def _tareas_pantalla(m, **kw):
    """`/tareas` con su plantilla; la base es la de `m` (sqlite) para el aviso de
    `sin_cerrar`, que SÍ mira la base. Solo la lista de tareas de la pantalla se
    reemplaza (su consulta es de Postgres) por una vacía."""
    async def _vacia(*a, **k):
        return {"grupos": [], "hay_mas": False}
    guardado = db.tareas_por_grupo
    db.tareas_por_grupo = _vacia
    try:
        html = N._correr(m, lambda: panel.tareas(base._get("/tareas"), **kw)
                         ).body.decode()
    finally:
        db.tareas_por_grupo = guardado
    return " ".join(re.sub(r"<[^>]+>", "", html).split())   # solo el texto que se lee


def test_la_ruta_dice_cual_no_se_cerro_aunque_otras_si():
    A._casa()
    m = Mundo()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    activo = m.proyecto("Activo")
    mala = m.tarea("en cerrado", proyecto=cerrado)
    b1 = m.tarea("libre uno")
    b2 = m.tarea("libre dos")
    r = N._correr(m, lambda: panel.guardar_tareas(base._post({
        f"prev_{mala}": "pendiente", f"hecha_{mala}": "1",
        f"deriva_titulo_{mala}_1": "hija",
        f"prev_{b1}": "pendiente", f"hecha_{b1}": "1",
        f"prev_{b2}": "pendiente", f"hecha_{b2}": "1"})))
    q = parse_qs(urlparse(_destino(r)).query)
    assert q["guardadas"] == ["2"] and q["sin_cerrar"] == [str(mala)], _destino(r)
    estados = {f["id"]: f["estado"] for f in m.filas()}
    assert estados == {mala: "pendiente", b1: "hecha", b2: "hecha"}
    html = _tareas_pantalla(m, guardadas=2, sin_cerrar=q["sin_cerrar"][0])
    assert f"La tarea #{mala} sigue pendiente y su proyecto está cerrado" in html
    assert "tampoco se creó" not in html and "escribiste" not in html, (
        "afirma lo que la base no confirmó: que había una derivada pedida")


def _mundo_de_sin_cerrar():
    A._casa()
    m = Mundo()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    activo = m.proyecto("Activo")
    borrado = m.proyecto("Borrado", estado="cerrado", borrado=True)
    for tid, kw in ((3, {"proyecto": cerrado}), (9, {"proyecto": cerrado}),
                    (20, {"proyecto": activo}),                    # proyecto activo
                    (21, {"proyecto": cerrado, "estado": "hecha"}),  # ya hecha
                    (22, {"proyecto": cerrado, "borrada": True}),    # a la papelera
                    (23, {"proyecto": borrado})):                  # proyecto archivado
        m.con.execute(
            "INSERT INTO tareas (id, titulo, proyecto_id, estado, borrado_en) "
            "VALUES (?,?,?,?,?)",
            (tid, f"t{tid}", kw["proyecto"], kw.get("estado", "pendiente"),
             "2026-09-01" if kw.get("borrada") else None))
    return m


def test_la_pantalla_pluraliza_y_solo_afirma_lo_que_la_base_confirma():
    m = _mundo_de_sin_cerrar()
    assert "Las tareas #3, #9 siguen pendientes y sus proyectos están cerrados" in \
        _tareas_pantalla(m, sin_cerrar="3,9")
    assert "La tarea #3 sigue pendiente y su proyecto está cerrado" in \
        _tareas_pantalla(m, sin_cerrar="3")
    # Lo que la base NO confirma no se afirma: no existe, activo, hecha,
    # papelera, proyecto archivado.
    for crudo in ("999", "0", "20", "21", "22", "23", "20,21,22,23,999"):
        assert "pendiente" not in _tareas_pantalla(m, sin_cerrar=crudo), crudo
    # Mezcla: solo sale el que vale.
    html = _tareas_pantalla(m, sin_cerrar="999,3,20")
    assert "La tarea #3 sigue pendiente" in html and "#999" not in html and "#20" not in html
    # Sin repetidos.
    assert "La tarea #3 sigue pendiente" in _tareas_pantalla(m, sin_cerrar="3,3,3")


def test_la_pantalla_no_pinta_lo_que_no_es_una_lista_de_ids_ascii():
    m = _mundo_de_sin_cerrar()
    for basura in ("", "x", "1;2", "<b>3</b>", "3,", ",3", "3,,9", "-3", "3e0",
                   "9" * 30, "٣", "٣,٩", "３", "3 ,9", " 3"):
        assert "pendiente" not in _tareas_pantalla(m, sin_cerrar=basura), repr(basura)


def test_la_pantalla_pone_tope_a_cuantos_ids_mira():
    A._casa()
    m = Mundo()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    ids = list(range(100, 100 + db.TOPE_SIN_CERRAR + 5))
    for i in ids:
        m.con.execute("INSERT INTO tareas (id, titulo, proyecto_id) VALUES (?,?,?)",
                      (i, f"t{i}", cerrado))
    html = _tareas_pantalla(m, sin_cerrar=",".join(map(str, ids)))
    assert f"#{ids[db.TOPE_SIN_CERRAR - 1]}" in html
    assert f"#{ids[db.TOPE_SIN_CERRAR]}" not in html, "no puso tope"
    assert N._correr(m, lambda: db.tareas_sin_cerrar_por_proyecto_cerrado(
        ids)) == ids[:db.TOPE_SIN_CERRAR]


def test_sin_derivadas_rechazadas_no_hay_aviso_de_sin_cerrar():
    A._casa()
    m = Mundo()
    t = m.tarea("libre")
    r = N._correr(m, lambda: panel.guardar_tareas(base._post({
        f"prev_{t}": "pendiente", f"hecha_{t}": "1"})))
    assert "sin_cerrar" not in _destino(r)


# ── 3. El botón de la tarjeta de Telegram dice el motivo real ────────────

def _apretar_guardar(m, interpretacion):
    import test_botones_rosi as tb
    from acciones import botones
    restaurar = tb._con_gente({DUENO: "Zutana"})
    guardados = (db.cambiar_estado, db.obtener, db.pool)
    estados = []

    async def _estado(bandeja_id, nuevo, **k):
        estados.append(nuevo)
        return True

    async def _obtener(bandeja_id):
        return {"id": bandeja_id, "interpretacion": interpretacion}

    db.cambiar_estado, db.obtener, db.pool = _estado, _obtener, m.pool()
    try:
        q = tb._Q("ok:5", DUENO)
        tb._correr(botones.al_pulsar(tb._update(q), None))
    finally:
        db.cambiar_estado, db.obtener, db.pool = guardados
        restaurar()
    alertas = [a[0][0] for a in q.respuestas if a[0]]
    return q, alertas, estados


def test_el_boton_con_un_proyecto_cerrado_dice_el_motivo_y_no_promete_reintentar():
    A._casa()
    m = Mundo()
    m.proyecto("Cerrado", estado="cerrado")
    q, alertas, estados = _apretar_guardar(m, {
        "clasificacion": "tarea", "titulo": "x", "proyecto": "cerrado"})
    assert alertas, "no le dijo nada"
    assert "cerrado" in alertas[-1] and "No creé la tarea" in alertas[-1], alertas
    assert "prueba de nuevo" not in alertas[-1] and "probá" not in alertas[-1]
    assert estados[-1] == "esperando_confirmacion", "cerró la tarjeta sin crear nada"
    assert m.n_tareas() == 0
    assert "Guardado" not in q.editado.get("texto", "")


def test_el_boton_con_un_proyecto_activo_si_guarda():
    A._casa()
    m = Mundo()
    m.proyecto("Activo", area="CDS")
    q, alertas, estados = _apretar_guardar(m, {
        "clasificacion": "tarea", "titulo": "x", "proyecto": "activo"})
    assert "Guardado" in q.editado.get("texto", ""), (alertas, q.editado)
    assert m.n_tareas() == 1


def test_otro_no_de_crud_tambien_se_muestra_y_lo_inesperado_sigue_igual():
    A._casa()
    m = Mundo()
    q, alertas, _ = _apretar_guardar(m, {
        "clasificacion": "tarea", "titulo": "x", "responsable_chat_id": "nadie"})
    assert alertas and "No creé la tarea" in alertas[-1], alertas
    # Lo que NO es un «no» de crud (una excepción cualquiera) sigue con el
    # aviso genérico.
    from acciones import botones
    import test_botones_rosi as tb
    guardado = crud.crear_desde_interpretacion

    async def _revienta(*a, **k):
        raise RuntimeError("se cayó")
    crud.crear_desde_interpretacion = _revienta
    try:
        q, alertas, _ = _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x"})
    finally:
        crud.crear_desde_interpretacion = guardado
    assert "No pude guardarlo" in alertas[-1], alertas


# ── 5. Carrera: el escritor rechaza lo que la ruta acababa de aceptar ────

def _con_ruta_ciega(m, campos, parches):
    """Corre `crear_tarea` con las puertas de la ruta reemplazadas para que
    digan «sí»: lo que se prueba es el escritor rechazando por su cuenta."""
    guardados = {(mod, k): getattr(mod, k) for (mod, k) in parches}
    try:
        for (mod, k), v in parches.items():
            setattr(mod, k, v)
        return _crear(m, campos)
    finally:
        for (mod, k), v in guardados.items():
            setattr(mod, k, v)


def test_carrera_persona_primero_y_responsable_vuelven_con_su_aviso_no_con_un_500():
    m, pid = _uno()
    borrada = m.persona("Vieja", borrada=True)
    tarea_borrada = m.tarea("borrada", borrada=True)

    async def _si(*a, **k):
        return True

    async def _primero(valor, **k):
        return int(valor)
    r = _con_ruta_ciega(m, {"titulo": "x", "proyecto": str(pid),
                            "persona": str(borrada)}, {(db, "persona_viva"): _si})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=persona"
    r = _con_ruta_ciega(m, {"titulo": "x", "proyecto": str(pid),
                            "primero_id": str(tarea_borrada)},
                        {(crud, "_primero_que_vale"): _primero})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=primero"
    r = _con_ruta_ciega(m, {"titulo": "x", "proyecto": str(pid)},
                        {(panel, "_responsable_pedido"): lambda c: (True, A.AJENO)})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=responsable"
    assert m.n_tareas() == 1 and m.n_bandeja() == 0    # solo la «borrada» de arriba


def test_carrera_del_proyecto_cerrado_en_medio_vuelve_con_su_aviso():
    m, pid = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")

    async def _pasa(valor):
        return {"id": valor, "nombre": "x", "area": None, "estado": "activo"}
    r = _con_ruta_ciega(m, {"titulo": "x", "proyecto": str(cerrado)},
                        {(db, "proyecto_para_tareas"): _pasa})
    assert _destino(r) == "/tareas/nueva?error=proyecto_cerrado"
    assert m.n_tareas() == 0


def test_cualquier_otro_no_del_escritor_dice_que_no_se_guardo_nada():
    m, pid = _uno()

    async def _no(*a, **k):
        raise ValueError("no")
    r = _con_ruta_ciega(m, {"titulo": "x", "proyecto": str(pid)},
                        {(db, "crear_tarea_desde_el_panel"): _no})
    assert _destino(r) == f"/tareas/nueva?proyecto={pid}&error=rechazada"
    assert "No se guardó nada" in _alta(m, proyecto=str(pid), error="rechazada")


# ── Deshacer también «recibe»: restaurar por cualquier camino ────────────

def _editar_tarea(m, tid, cambios):
    N._correr(m, lambda: crud.editar("tareas", tid, cambios, motivo="prueba"))
    return N._ultimo_log(m)


def _proyecto_de(m, tid):
    return [f for f in m.filas() if f["id"] == tid][0]["proyecto_id"]


def test_deshacer_una_edicion_vieja_no_mete_la_tarea_en_un_proyecto_cerrado():
    """Renombrar → mover a otro proyecto → cerrar el viejo → deshacer el
    renombre: esa huella lleva el proyecto de ANTES; restaurarlo devolvería la
    tarea al proyecto cerrado."""
    m, viejo = _uno()
    nuevo = m.proyecto("Nuevo")
    t = m.tarea("original", proyecto=viejo)
    log_renombre = _editar_tarea(m, t, {"titulo": "renombrada"})
    _editar_tarea(m, t, {"proyecto_id": nuevo})
    m.con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = ?", (viejo,))
    e = N._rechazo(m, N._deshacer(m, log_renombre))
    assert isinstance(e, ValueError) and str(e).startswith("No lo deshice:"), e
    assert "cerrado" in str(e)
    assert _proyecto_de(m, t) == nuevo, "la devolvió a un proyecto cerrado"
    assert [f for f in m.filas() if f["id"] == t][0]["titulo"] == "renombrada"
    # Con el proyecto abierto de nuevo, sí.
    m.con.execute("UPDATE proyectos SET estado = 'activo' WHERE id = ?", (viejo,))
    N._correr(m, N._deshacer(m, log_renombre))
    assert _proyecto_de(m, t) == viejo


def test_deshacer_el_borrado_de_una_tarea_no_la_hace_reaparecer_en_un_proyecto_cerrado():
    m, pid = _uno()
    t = m.tarea("de disco", proyecto=pid)
    log_borrado = N._correr(m, lambda: crud.borrar("tareas", t, "prueba"))
    m.con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = ?", (pid,))
    e = N._rechazo(m, N._deshacer(m, log_borrado))
    assert isinstance(e, ValueError) and "No lo deshice:" in str(e) and "cerrado" in str(e)
    assert m.con.execute("SELECT borrado_en FROM tareas WHERE id=?",
                         (t,)).fetchone()[0] is not None, "reapareció"
    # Proyecto archivado: tampoco.
    m.con.execute("UPDATE proyectos SET estado='activo', borrado_en='2026-09-01' "
                  "WHERE id = ?", (pid,))
    assert isinstance(N._rechazo(m, N._deshacer(m, log_borrado)), ValueError)
    # Proyecto abierto: reaparece.
    m.con.execute("UPDATE proyectos SET borrado_en = NULL WHERE id = ?", (pid,))
    N._correr(m, N._deshacer(m, log_borrado))
    assert m.con.execute("SELECT borrado_en FROM tareas WHERE id=?",
                         (t,)).fetchone()[0] is None


def test_deshacer_el_borrado_de_una_tarea_sin_proyecto_sigue_funcionando():
    m, _ = _uno()
    t = m.tarea("suelta")
    log_borrado = N._correr(m, lambda: crud.borrar("tareas", t, "prueba"))
    N._correr(m, N._deshacer(m, log_borrado))
    assert m.con.execute("SELECT borrado_en FROM tareas WHERE id=?",
                         (t,)).fetchone()[0] is None


def test_deshacer_un_cambio_que_no_movio_la_tarea_funciona_en_un_proyecto_cerrado():
    m, _ = _uno()
    cerrado = m.proyecto("Cerrado", estado="cerrado")
    t = m.tarea("original", proyecto=cerrado)
    log_id = _editar_tarea(m, t, {"titulo": "cambiada"})
    N._correr(m, N._deshacer(m, log_id))
    fila = [f for f in m.filas() if f["id"] == t][0]
    assert (fila["titulo"], fila["proyecto_id"]) == ("original", cerrado)


def test_deshacer_un_movimiento_de_sin_proyecto_a_un_proyecto_vuelve_a_sin_proyecto():
    m, pid = _uno()
    t = m.tarea("suelta")
    log_id = _editar_tarea(m, t, {"proyecto_id": pid})
    assert _proyecto_de(m, t) == pid
    # Aunque el proyecto ya se haya cerrado: sacar la tarea de ahí no es recibir.
    m.con.execute("UPDATE proyectos SET estado = 'cerrado' WHERE id = ?", (pid,))
    N._correr(m, N._deshacer(m, log_id))
    assert _proyecto_de(m, t) is None


def test_deshacer_un_movimiento_de_un_proyecto_a_otro_devuelve_si_el_de_antes_admite():
    m, a = _uno()
    b = m.proyecto("B")
    t = m.tarea("x", proyecto=a)
    log_id = _editar_tarea(m, t, {"proyecto_id": b})
    N._correr(m, N._deshacer(m, log_id))
    assert _proyecto_de(m, t) == a


# ── El botón: la promesa solo cuando es verdad; solo el «no» de negocio baja de nivel ──

def test_la_promesa_de_reabrir_solo_sale_si_el_motivo_es_que_esta_cerrado():
    A._casa()
    m = Mundo()
    m.proyecto("Cerrado", estado="cerrado")
    _, alertas, _ = _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x",
                                         "proyecto": "cerrado"})
    assert "vuelve a tocar" in alertas[-1], alertas
    # Mismo callback, otro motivo del proyecto (ya no existe: carrera).
    m2 = Mundo()
    m2.proyecto("Activo")

    async def _no_existe(pid):
        raise db.ProyectoNoAdmiteTareas("no_existe", "ese proyecto no existe o ya no está")
    guardado = db.proyecto_para_tareas
    db.proyecto_para_tareas = _no_existe
    try:
        _, alertas, _ = _apretar_guardar(m2, {"clasificacion": "tarea", "titulo": "x",
                                              "proyecto": "activo"})
    finally:
        db.proyecto_para_tareas = guardado
    assert "No creé la tarea" in alertas[-1] and "vuelve a tocar" not in alertas[-1], alertas
    # Y un «no» de otro tipo (responsable) tampoco promete nada de proyectos.
    _, alertas, _ = _apretar_guardar(Mundo(), {"clasificacion": "tarea", "titulo": "x",
                                               "responsable_chat_id": "nadie"})
    assert "vuelve a tocar" not in alertas[-1]


def test_un_valueerror_de_programacion_conserva_el_traceback_y_el_aviso_generico(caplog):
    import logging
    A._casa()
    m = Mundo()
    guardado = crud.crear_desde_interpretacion

    async def _bug(*a, **k):
        raise ValueError("un fallo de programación cualquiera")
    crud.crear_desde_interpretacion = _bug
    try:
        with caplog.at_level(logging.WARNING, logger="lucy.botones"):
            _, alertas, _ = _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x"})
    finally:
        crud.crear_desde_interpretacion = guardado
    assert "No pude guardarlo" in alertas[-1], alertas
    assert "programación" not in alertas[-1], "enseñó un ValueError que no es de negocio"
    regs = [r for r in caplog.records if r.name == "lucy.botones"]
    assert any(r.levelno == logging.ERROR and r.exc_info for r in regs), (
        "perdió el traceback")


def test_el_no_de_negocio_baja_a_warning_sin_traceback(caplog):
    import logging
    A._casa()
    m = Mundo()
    m.proyecto("Cerrado", estado="cerrado")
    with caplog.at_level(logging.WARNING, logger="lucy.botones"):
        _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x",
                             "proyecto": "cerrado"})
    regs = [r for r in caplog.records if r.name == "lucy.botones"]
    assert regs and all(r.levelno == logging.WARNING and not r.exc_info for r in regs), regs


def test_los_no_de_negocio_de_crud_son_de_su_clase_y_un_valueerror_suelto_no():
    """AYUDA, NO GARANTÍA. Busca en el AST el literal `ValueError` lanzado dentro
    de `crear_desde_interpretacion`. NO VE un alias (`_VE = ValueError`), una
    clase creada al vuelo, ni un `raise` dentro de un ayudante que la función
    llame: es una regla detectada por un literal de texto. La garantía de verdad
    es de COMPORTAMIENTO y está en las pruebas de abajo que aprietan el botón real
    con cada «no» de `crear_desde_interpretacion`."""
    fuente = inspect.getsource(crud.crear_desde_interpretacion)
    arbol = ast.parse(textwrap.dedent(fuente))
    planos = [n for n in ast.walk(arbol) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name)
              and n.exc.func.id == "ValueError"]
    assert not planos, (
        f"crear_desde_interpretacion lanza ValueError sueltos: "
        f"{[n.lineno for n in planos]}")
    assert issubclass(crud.NoDeNegocio, ValueError)


# ── LA GARANTÍA, POR COMPORTAMIENTO: cada «no» de crear, por el botón real ──

def _con_eventos(m, con_duenos=True):
    """La tabla `eventos` (solo lo que `crear` cita necesita). Sin
    `duenos_chat_id` es la base con la migración de «citas con dueño» sin aplicar."""
    duenos = ", duenos_chat_id" if con_duenos else ""
    m.con.execute(
        "CREATE TABLE eventos (id INTEGER PRIMARY KEY, bandeja_id, titulo, "
        "inicia_en, termina_en, lugar, persona_id, proyecto_id, notas, "
        f"anticipos_min, borrado_en{duenos})")


def _boton_con_no(caplog, m, interpretacion):
    import logging
    with caplog.at_level(logging.WARNING, logger="lucy.botones"):
        q, alertas, estados = _apretar_guardar(m, interpretacion)
    regs = [r for r in caplog.records if r.name == "lucy.botones"]
    return q, alertas, estados, regs


def _casos_de_no():
    """(nombre, cómo armar el mundo, interpretación, trozo del motivo que se
    tiene que ver). Uno por cada `raise NoDeNegocio` de
    `crud.crear_desde_interpretacion`, más el nombre de proyecto."""
    def base_(m):
        return m

    def cerrado(m):
        m.proyecto("Cerrado", estado="cerrado")
        return m

    def duplicado_de_otro(m):
        m.con.execute("INSERT INTO tareas (titulo, responsable_chat_id, estado) "
                      "VALUES ('Llamar al banco', ?, 'pendiente')", (OTRA,))
        return m

    def sin_columna_de_duenos(m):
        _con_eventos(m, con_duenos=False)
        return m

    def con_eventos(m):
        _con_eventos(m)
        return m

    cita = {"clasificacion": "cita", "titulo": "reunión",
            "cuando": "2030-01-01T10:00:00"}
    return [
        ("clasificacion_que_no_crea_nada", base_,
         {"clasificacion": "otra", "titulo": "x"}, "no crea ninguna entidad"),
        ("responsable_que_no_vale", base_,
         {"clasificacion": "tarea", "titulo": "x", "responsable_chat_id": "nadie"},
         "No creé la tarea"),
        ("area_que_no_vale", base_,
         {"clasificacion": "tarea", "titulo": "x", "area": "Marketing"},
         "No creé la tarea"),
        ("primero_que_no_existe", base_,
         {"clasificacion": "tarea", "titulo": "x", "primero_id": 9999},
         "No creé la tarea"),
        ("proyecto_cerrado", cerrado,
         {"clasificacion": "tarea", "titulo": "x", "proyecto": "cerrado"},
         "ese proyecto está cerrado"),
        ("nombre_de_proyecto_demasiado_largo", base_,
         {"clasificacion": "tarea", "titulo": "x", "proyecto": "p" * 201},
         "200 caracteres"),
        ("tarea_que_ya_existia_de_otro", duplicado_de_otro,
         {"clasificacion": "tarea", "titulo": "Llamar al banco",
          "responsable_chat_id": "Zutana"}, "ya existía"),
        ("dueno_de_cita_que_no_vale", con_eventos,
         {**cita, "duenos_chat_id": "nadie"}, "No creé la cita"),
        ("cita_con_dueno_sin_la_migracion", sin_columna_de_duenos,
         {**cita, "duenos_chat_id": "Zutana"}, "migración"),
    ]


def test_el_boton_real_dice_el_motivo_de_cada_no_de_crear_a_nivel_warning(caplog):
    import logging
    for nombre, armar, interpretacion, trozo in _casos_de_no():
        caplog.clear()
        A._casa()
        m = armar(Mundo())
        q, alertas, estados, regs = _boton_con_no(caplog, m, interpretacion)
        assert alertas and trozo in alertas[-1], (nombre, alertas)
        assert "No pude guardarlo" not in alertas[-1], (
            f"{nombre}: dio el aviso genérico en vez del motivo")
        assert estados[-1] == "esperando_confirmacion", nombre
        assert regs and all(r.levelno == logging.WARNING and not r.exc_info
                            for r in regs), (nombre, regs)
        assert "Guardado" not in q.editado.get("texto", ""), nombre
        assert m.n_tareas() == (1 if nombre == "tarea_que_ya_existia_de_otro" else 0), nombre


def test_los_casos_de_no_cubren_cada_raise_de_no_de_negocio_de_crear():
    """Cada `raise NoDeNegocio(...)` de `crear_desde_interpretacion` (contados en
    el AST) tiene al menos un caso arriba. No prueba QUÉ raise dispara cada caso
    (eso lo hace el comportamiento de la prueba de arriba); solo evita que un
    `raise` nuevo quede sin caso."""
    arbol = ast.parse(textwrap.dedent(inspect.getsource(crud.crear_desde_interpretacion)))
    raises = [n for n in ast.walk(arbol) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name)
              and n.exc.func.id == "NoDeNegocio"]
    # Los 4 «No creé la tarea: {e}» de responsable, área, «Primero:» y proyecto,
    # el «ya existía», la clasificación, el dueño de cita y la migración de citas.
    assert len(raises) == 8, (
        f"cambió el número de `raise NoDeNegocio` ({len(raises)}): agrega su "
        f"caso a `_casos_de_no`")
    assert len(_casos_de_no()) >= len(raises)


def test_un_motivo_largo_recorta_el_motivo_y_no_la_promesa():
    A._casa()
    m = Mundo()
    m.proyecto("Activo")

    async def _cerrado_con_motivo_largo(pid):
        raise db.ProyectoNoAdmiteTareas("cerrado", "x" * 400)
    guardado = db.proyecto_para_tareas
    db.proyecto_para_tareas = _cerrado_con_motivo_largo
    try:
        _, alertas, _ = _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x",
                                             "proyecto": "activo"})
    finally:
        db.proyecto_para_tareas = guardado
    aviso = alertas[-1]
    assert len(aviso) <= 190, len(aviso)
    assert aviso.endswith("vuelve a tocar ✅."), "se cortó la promesa"
    assert "…" in aviso and aviso.startswith("No creé la tarea"), aviso
    # Sin promesa: el motivo largo se recorta a 190 también.
    async def _no_existe_largo(pid):
        raise db.ProyectoNoAdmiteTareas("no_existe", "y" * 400)
    db.proyecto_para_tareas = _no_existe_largo
    try:
        _, alertas, _ = _apretar_guardar(m, {"clasificacion": "tarea", "titulo": "x",
                                             "proyecto": "activo"})
    finally:
        db.proyecto_para_tareas = guardado
    assert len(alertas[-1]) <= 190 and alertas[-1].endswith("…")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
