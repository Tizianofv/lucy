# -*- coding: utf-8 -*-
"""Elegir el responsable al escribir una tarea a mano (tarea 146 de Tiziano,
aprobada el 28-sep-2026).

LO QUE SE VIGILA, y con qué camino:

  · Sin elegir nada, la tarea nace sin responsable. Por la RUTA real
    (`panel.crear_tarea`) y la base de mentira de `test_tarea_a_mano.py`.
  · Elegido, queda escrito en los DOS INSERT de `tareas` (`con_area` y
    `sin_area`). Estas pruebas EJECUTAN el texto real de cada INSERT en sqlite
    (con los parámetros reales que arma `db.crear_tarea_desde_el_panel`): un
    parámetro desalineado o una columna quitada de uno de los dos revienta o
    guarda otra cosa. Límite dicho: sqlite no es Postgres (no tiene la FK de
    `tareas.area`, ni tipos estrictos); lo que comprueba es que el texto y sus
    parámetros dicen lo mismo, no lo que Postgres haría con ellos.
  · La puerta: un responsable que no vale se rechaza en la ruta Y en la
    función que escribe, cada una por su cuenta (una pareja de mutaciones: se
    quita cada una y la prueba de su capa se pone roja).
  · Code → el área se pone sola en `db.AREA_TECNICA`, con la ruta y con la
    función directa.
  · Los tres desplegables de responsable (fila de la tabla, renglón derivado
    y alta) ofrecen las MISMAS opciones, que salen de una sola función.
  · `?responsable=<nombre>` preescoge, y solo preescoge: pasa por la misma
    puerta y ningún número de chat sale como texto.

Correr:  python3 -m pytest tests/test_alta_con_responsable.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tarea_a_mano as base  # noqa: E402  (deja `psycopg` falseado)

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402

DUENO = config.CHAT_ID_DUENO
OTRA = 700000001          # la segunda persona de la casa (inventada)
TERCERA = 700000002       # una tercera que podría entrar mañana (inventada)
AJENO = 700000999         # uno que NO entra al panel
AREAS = [{"clave": db.AREA_TECNICA, "color": "#111111"},
         {"clave": "CDS", "color": "#2b6cb0"}]


def _casa(nombres=None, permitidos=None):
    """Instala una casa de mentira en `config` (el conftest la devuelve)."""
    if nombres is None:
        nombres = {DUENO: "Zutana", OTRA: "Mengano"}
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = tuple(
        permitidos if permitidos is not None else nombres)


def _mandar(campos, areas=AREAS):
    conn = base._Conn(areas=areas)
    r = base._con_base(conn, lambda: panel.crear_tarea(base._post(campos)))
    return r, conn


def _pintar_alta(**kw):
    conn = base._Conn(areas=AREAS)
    r = base._con_base(conn, lambda: panel.tarea_nueva(
        base._get("/tareas/nueva"), **kw))
    assert r.status_code == 200
    return r.body.decode()


# ── Sin elegir: sin responsable ──────────────────────────────────────────

def test_sin_elegir_responsable_la_tarea_nace_sin_responsable():
    _casa()
    for campos in ({"titulo": "a"}, {"titulo": "a", "responsable": ""},
                   {"titulo": "a", "responsable": "   "}):
        r, conn = _mandar(campos)
        assert r.status_code == 303 and "creada=" in r.headers["location"]
        assert conn.tareas[0]["responsable_chat_id"] is None, (
            f"le puso un responsable a {campos!r}")


def test_el_formulario_sale_en_sin_responsable():
    """Tiziano (28-sep): al crear sale «Sin responsable»."""
    _casa()
    html = _pintar_alta()
    seleccionadas = re.findall(r'<option value="([^"]*)"\s*selected', html)
    assert seleccionadas == [""], (
        f"el alta no arranca en «sin responsable»: {seleccionadas}")


# ── Elegir ───────────────────────────────────────────────────────────────

def test_elegir_una_persona_la_deja_escrita_y_no_toca_el_area():
    _casa()
    r, conn = _mandar({"titulo": "a", "area": "CDS", "responsable": str(OTRA)})
    assert r.status_code == 303 and "creada=" in r.headers["location"]
    assert conn.tareas[0]["responsable_chat_id"] == OTRA
    assert conn.tareas[0]["area"] == "CDS", (
        "una persona (no Code) no debe cambiar el área elegida")


def test_elegir_code_pone_el_area_tecnica_sola_pida_lo_que_pida():
    _casa()
    for area in (None, "CDS"):
        campos = {"titulo": "a", "responsable": str(config.CHAT_ID_CODE)}
        if area:
            campos["area"] = area
        r, conn = _mandar(campos)
        assert r.status_code == 303 and "creada=" in r.headers["location"]
        assert conn.tareas[0]["responsable_chat_id"] == config.CHAT_ID_CODE
        assert conn.tareas[0]["area"] == db.AREA_TECNICA, (
            f"con Code y área {area!r} no quedó en Técnico: "
            f"{conn.tareas[0]['area']!r}")


def test_code_sin_el_area_tecnica_en_la_lista_se_rechaza_y_no_escribe():
    """Si `areas()` no trae el área técnica, ponérsela sola reventaría la FK
    de la base en un 500: la ruta lo rechaza como cualquier área que no existe."""
    _casa()
    r, conn = _mandar({"titulo": "a", "responsable": str(config.CHAT_ID_CODE)},
                      areas=[{"clave": "CDS", "color": "#2b6cb0"}])
    assert r.headers["location"] == "/tareas/nueva?error=area"
    assert conn.tareas == [] and conn.bandeja == []


def test_la_funcion_que_escribe_pone_el_area_tecnica_aunque_la_llamen_directo():
    """La regla vive en el escritor, no solo en la ruta."""
    _casa()
    conn = base._Conn(areas=AREAS)
    base._con_base(conn, lambda: db.crear_tarea_desde_el_panel(
        DUENO, "x", None, "CDS", config.CHAT_ID_CODE))
    assert conn.tareas[0]["area"] == db.AREA_TECNICA


# ── La puerta, en las dos capas ──────────────────────────────────────────

MALOS = [str(AJENO), "abc", "0" + str(OTRA), "+" + str(OTRA), "-5", "1_000",
         "Mengano"]


def test_la_ruta_rechaza_un_responsable_que_no_vale_y_no_escribe_nada():
    _casa()
    for malo in MALOS:
        r, conn = _mandar({"titulo": "a", "responsable": malo})
        assert r.status_code == 303
        assert r.headers["location"] == "/tareas/nueva?error=responsable", (
            f"{malo!r}: {r.headers['location']}")
        assert conn.tareas == [] and conn.bandeja == [], (
            f"{malo!r}: escribió algo")
        assert not [q for q, _ in conn.sql if q.startswith("INSERT")], (
            f"{malo!r}: intentó un INSERT")


def test_alguien_con_nombre_que_ya_no_entra_al_panel_tampoco_vale():
    _casa({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    r, conn = _mandar({"titulo": "a", "responsable": str(OTRA)})
    assert r.headers["location"] == "/tareas/nueva?error=responsable"
    assert conn.tareas == []


def test_la_funcion_que_escribe_revalida_por_su_cuenta_y_no_abre_conexion():
    _casa()
    for malo in (AJENO, 12345):
        conn = base._Conn(areas=AREAS)
        try:
            base._con_base(conn, lambda: db.crear_tarea_desde_el_panel(
                DUENO, "x", None, None, malo))
        except ValueError:
            pass
        else:
            raise AssertionError(f"aceptó a {malo}")
        assert conn.sql == [] and conn.tareas == [], "tocó la base"


def test_el_rechazo_se_explica_con_palabras():
    _casa()
    assert 'class="aviso"' in _pintar_alta(error="responsable")


# ── Los DOS INSERT, ejecutados de verdad (sqlite) ────────────────────────

class _SqliteCur:
    def __init__(self, c):
        self._c = c
        self._cur = None

    async def execute(self, sql, params=None):
        s = sql.replace("%s", "?").replace("now()", "CURRENT_TIMESTAMP")
        conv = []
        for p in (params or ()):
            if isinstance(p, list):
                p = repr(p)
            elif isinstance(p, datetime):
                p = p.isoformat()
            conv.append(p)
        try:
            self._cur = self._c.execute(s, conv)
        except sqlite3.OperationalError as e:
            # Lo que haría Postgres con una columna que no existe.
            if "no column named" in str(e):
                e.sqlstate = "42703"
            raise
        return self

    async def fetchone(self):
        f = self._cur.fetchone()
        if f is None:
            return None
        return dict(zip([d[0] for d in self._cur.description], f))


class _SqliteConn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _SqliteCur(self._con)

    async def execute(self, sql, params=None):
        return await _SqliteCur(self._con).execute(sql, params)

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *e):
                return False
        return _T()


def _sqlite(con_area: bool):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("CREATE TABLE bandeja (id INTEGER PRIMARY KEY, origen, "
                "tipo_entrada, chat_id, estado, procesado_en)")
    columna_area = "area TEXT," if con_area else ""
    con.execute("CREATE TABLE tareas (id INTEGER PRIMARY KEY, bandeja_id, "
                "titulo, vence_en, anticipos_min, " + columna_area +
                "responsable_chat_id INTEGER, estado TEXT DEFAULT 'pendiente')")
    con.execute("CREATE TABLE log_acciones (id INTEGER PRIMARY KEY, actor, "
                "accion, tabla, registro_id, antes, despues, motivo, "
                "bandeja_id)")
    return con


def _crear_en_sqlite(con_area, responsable, area="CDS"):
    con = _sqlite(con_area)

    class _P:
        def connection(self):
            class _CM:
                async def __aenter__(s):
                    return _SqliteConn(con)

                async def __aexit__(s, *e):
                    return False
            return _CM()
    guardado, db.pool = db.pool, _P()
    try:
        bucle = asyncio.new_event_loop()
        try:
            tid = bucle.run_until_complete(db.crear_tarea_desde_el_panel(
                DUENO, "x", None, area, responsable))
        finally:
            bucle.close()
    finally:
        db.pool = guardado
    fila = con.execute(
        "SELECT titulo, responsable_chat_id" + (", area" if con_area else "") +
        " FROM tareas WHERE id = ?", (tid,)).fetchone()
    log = con.execute("SELECT despues FROM log_acciones").fetchone()[0]
    return fila, log


def test_el_insert_con_area_guarda_el_responsable_de_verdad():
    _casa()
    fila, log = _crear_en_sqlite(True, OTRA, area="CDS")
    assert fila == ("x", OTRA, "CDS"), f"el INSERT con área guardó {fila}"
    assert f'"responsable_chat_id": {OTRA}' in log.replace("'", '"'), (
        "la huella no lleva el responsable")


def test_el_insert_sin_area_guarda_el_responsable_de_verdad():
    """La columna `area` no existe (migración sin aplicar): cae al segundo
    INSERT, que también tiene que llevar el responsable."""
    _casa()
    fila, _ = _crear_en_sqlite(False, OTRA)
    assert fila == ("x", OTRA), f"el INSERT sin área guardó {fila}"


def test_los_dos_insert_sin_responsable_guardan_null():
    _casa()
    assert _crear_en_sqlite(True, None)[0] == ("x", None, "CDS")
    assert _crear_en_sqlite(False, None)[0] == ("x", None)


def test_code_por_la_funcion_real_guarda_menos_uno_y_el_area_tecnica():
    _casa()
    fila, _ = _crear_en_sqlite(True, config.CHAT_ID_CODE, area="CDS")
    assert fila == ("x", config.CHAT_ID_CODE, db.AREA_TECNICA)


# ── Una sola lista de opciones para los tres desplegables ────────────────

def _valores(html, nombre_select):
    m = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>'
                  % re.escape(nombre_select), html, re.S)
    assert m, f"no hay <select name={nombre_select}> en la pantalla"
    return re.findall(r'<option value="([^"]*)"', m.group(1))


def _tabla_con_una_pendiente():
    conn = base._Conn(tareas=[{
        "id": 1, "titulo": "t", "estado": "pendiente", "vence_en": None,
        "creado_en": datetime(2026, 9, 9, tzinfo=timezone.utc),
        "bandeja_id": 5, "responsable_chat_id": None, "completado_en": None,
        "borrado_en": None}], areas=AREAS)
    return base._con_base(conn, lambda: panel.tareas(
        base._get("/tareas"))).body.decode()


def test_los_tres_desplegables_ofrecen_las_mismas_opciones_de_una_sola_funcion():
    # Una tercera persona INVENTADA: aparece en los tres sin tocar código.
    _casa({DUENO: "Zutana", OTRA: "Mengano", TERCERA: "Fulanita"})
    esperado = [v for v, _ in config.opciones_de_responsable()]
    assert esperado == ["", str(DUENO), str(OTRA), str(TERCERA),
                        str(config.CHAT_ID_CODE)], esperado
    tabla = _tabla_con_una_pendiente()
    assert _valores(tabla, "resp_1") == esperado
    assert _valores(tabla, "deriva_resp_1_1") == esperado
    assert _valores(_pintar_alta(), "responsable") == esperado


def test_sin_nadie_en_la_variable_siguen_estando_sin_responsable_y_code():
    _casa({})
    assert [v for v, _ in config.opciones_de_responsable()] == [
        "", str(config.CHAT_ID_CODE)]


# ── ?responsable=<nombre> preescoge, y solo preescoge ────────────────────

def _elegida(html):
    return re.findall(r'<option value="([^"]*)"\s*selected', html)


def test_el_parametro_preescoge_por_nombre_sin_mayusculas_ni_tildes():
    _casa()
    assert _elegida(_pintar_alta(responsable="Mengano")) == [str(OTRA)]
    assert _elegida(_pintar_alta(responsable=" mengano ")) == [str(OTRA)]
    assert _elegida(_pintar_alta(responsable="CODE")) == [
        str(config.CHAT_ID_CODE)]


def test_lo_que_no_es_un_nombre_valido_abre_en_sin_responsable():
    _casa()
    for crudo in ("", "_sin", "_otros", "Nadie", str(OTRA), str(AJENO),
                  "0" + str(OTRA), '"><script>alert(1)</script>'):
        html = _pintar_alta(responsable=crudo)
        assert _elegida(html) == [""], f"{crudo!r} preescogió {_elegida(html)}"
        assert "<script>alert(1)" not in html, "reflejó el parámetro sin escapar"


def test_un_nombre_repetido_no_se_desempata():
    _casa({DUENO: "Igual", OTRA: "igual"})
    assert _elegida(_pintar_alta(responsable="igual")) == [""]


def test_alguien_con_nombre_que_ya_no_entra_no_se_preescoge():
    _casa({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    assert _elegida(_pintar_alta(responsable="Mengano")) == [""]


# ── Lo que la pantalla no debe enseñar ───────────────────────────────────

def test_el_alta_no_enseña_ningun_numero_de_chat_como_texto():
    _casa()
    html = _pintar_alta(responsable="Mengano")
    sin_valores = re.sub(r'value="[^"]*"', 'value=""', html)
    for chat in (DUENO, OTRA, config.CHAT_ID_CODE):
        assert str(chat) not in sin_valores, f"se ve un chat: {chat}"


def test_el_aviso_de_code_nombra_el_area_desde_la_constante_y_no_escrita():
    _casa()
    assert db.AREA_TECNICA in _pintar_alta()
    ruta = os.path.join(base.RAIZ, "web", "plantillas", "tarea_nueva.html")
    plantilla = open(ruta, encoding="utf-8").read()
    assert "Técnico" not in plantilla, (
        "el nombre del área está escrito a mano en la plantilla")


def test_el_alta_sigue_teniendo_un_solo_formulario():
    ruta = os.path.join(base.RAIZ, "web", "plantillas", "tarea_nueva.html")
    assert open(ruta, encoding="utf-8").read().count("<form") == 1


# ── LA OPCIÓN CORRECTA ES LA ÚNICA MARCADA, en los tres desplegables ─────
#
# Hallazgo del testigo sobre a4fd282: al pasar la fila de la tabla y el renglón
# derivado a la lista única, la lógica de `selected` quedó sin vigilar. Si una
# persona o Code deja de salir marcada, el navegador manda la PRIMERA opción
# («sin responsable»), `prev_resp_<id>` dice otra cosa y el siguiente «Guardar
# los cambios» le borra el responsable a cada tarea. Estas pruebas leen la
# pantalla como la lee un navegador (una opción marcada; sin ninguna, la
# primera) y cierran el ciclo: pintar → enviar tal cual → leer la base.

from html.parser import HTMLParser  # noqa: E402


class _Formulario(HTMLParser):
    """Los campos que un navegador enviaría al tocar «Guardar» sin cambiar
    nada. `campos`: {nombre: valor} tal como viajaría; `marcadas`: {nombre de
    select: [valores con `selected` escrito en el HTML]}."""

    def __init__(self):
        super().__init__()
        self.campos, self.marcadas, self.opciones = {}, {}, {}
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            tipo = a.get("type", "text")
            if tipo in ("checkbox", "radio", "submit", "button") or "disabled" in a:
                return
            if a.get("name"):
                self.campos[a["name"]] = a.get("value", "")
        elif tag == "select":
            self._select = a.get("name")
            self.opciones[self._select] = []
            self.marcadas[self._select] = []
        elif tag == "option" and self._select is not None:
            self.opciones[self._select].append(a.get("value", ""))
            if "selected" in a:
                self.marcadas[self._select].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "select":
            n = self._select
            # Lo que manda un navegador: la marcada, o la primera si ninguna.
            self.campos[n] = (self.marcadas[n][0] if self.marcadas[n]
                              else self.opciones[n][0])
            self._select = None


def _leer(html):
    f = _Formulario()
    f.feed(html)
    return f


class _CursorE(base._Cursor):
    """La base de mentira del alta, más lo que necesita `db.asignar_responsable`
    (leer la fila y escribir el responsable) para que guardar de verdad se lea
    de vuelta desde la base."""

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        c = self._conn
        if s.startswith("SELECT id, titulo, estado, vence_en, responsable_chat_id"):
            c.sql.append((s, params))
            self._filas = [dict(t) for t in c.tareas
                           if t["id"] == params[0] and not t.get("borrado_en")]
            return self
        if s.startswith("UPDATE tareas SET responsable_chat_id"):
            c.sql.append((s, params))
            for t in c.tareas:
                if t["id"] == params[1]:
                    t["responsable_chat_id"] = params[0]
            self._filas = []
            return self
        return await super().execute(sql, params)


class _ConnE(base._Conn):
    def cursor(self, row_factory=None):
        return _CursorE(self)

    async def execute(self, sql, params=None):
        return await _CursorE(self).execute(sql, params)


EXPULSADO = 700000555      # ya no entra al panel y no tiene nombre
JUBILADA = 700000556       # ya no entra al panel pero SÍ tiene nombre


def _fila(id, resp, estado="pendiente"):
    return {"id": id, "titulo": f"tarea {id}", "estado": estado,
            "vence_en": None,
            "creado_en": datetime(2026, 9, 9, tzinfo=timezone.utc),
            "bandeja_id": 500 + id, "responsable_chat_id": resp,
            "completado_en": (datetime.now(timezone.utc)
                              if estado == "hecha" else None),
            "borrado_en": None}


def _mundo():
    """Una tarea por cada clase de responsable posible, hoy y mañana."""
    _casa({DUENO: "Zutana", OTRA: "Mengano", JUBILADA: "Jubilada"},
          permitidos=(DUENO, OTRA))
    filas = [_fila(1, DUENO), _fila(2, OTRA), _fila(3, config.CHAT_ID_CODE),
             _fila(4, None), _fila(5, EXPULSADO), _fila(6, JUBILADA),
             _fila(7, OTRA, "hecha")]
    return _ConnE(tareas=filas, areas=AREAS)


def _pantalla(conn):
    return base._con_base(conn, lambda: panel.tareas(
        base._get("/tareas"))).body.decode()


def test_la_opcion_correcta_es_la_unica_marcada_en_la_fila_y_en_el_renglon():
    conn = _mundo()
    f = _leer(_pantalla(conn))
    esperado = {1: str(DUENO), 2: str(OTRA), 3: str(config.CHAT_ID_CODE),
                4: "", 5: str(EXPULSADO), 6: str(JUBILADA), 7: str(OTRA)}
    for tid, valor in esperado.items():
        assert f.marcadas[f"resp_{tid}"] == [valor], (
            f"fila {tid}: marcadas {f.marcadas[f'resp_{tid}']}, esperaba "
            f"{[valor]}")
    # El renglón derivado (solo en las pendientes) copia el responsable de la
    # madre. Quien ya no se puede asignar no se copia: sin marca, que es lo
    # que hace hoy la pantalla, y el navegador manda «sin responsable».
    for tid in (1, 2, 3, 4):
        for n in range(1, panel.MAX_DERIVADAS + 1):
            assert f.marcadas[f"deriva_resp_{tid}_{n}"] == [esperado[tid]], (
                f"renglón {tid}/{n}: {f.marcadas[f'deriva_resp_{tid}_{n}']}")
    for tid in (5, 6):
        assert f.marcadas[f"deriva_resp_{tid}_1"] == []
    assert "deriva_resp_7_1" not in f.marcadas, "una hecha no lleva renglón"


def test_el_alta_preescogida_marca_solo_la_correcta():
    _mundo()
    for parametro, valor in (("Zutana", str(DUENO)), ("Mengano", str(OTRA)),
                             ("Code", str(config.CHAT_ID_CODE)),
                             ("_sin", ""), ("", "")):
        marcadas = _leer(_pintar_alta(responsable=parametro)).marcadas
        assert marcadas["responsable"] == [valor], (
            f"?responsable={parametro!r}: {marcadas['responsable']}")


def test_guardar_sin_tocar_nada_no_cambia_ningun_responsable_de_punta_a_punta():
    """Pinta la pantalla, manda el formulario tal como un navegador lo mandaría
    sin que nadie toque nada, y lee la base: ningún responsable cambió y no se
    escribió ninguna huella de cambio de responsable."""
    conn = _mundo()
    antes = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    f = _leer(_pantalla(conn))
    assert f.campos["resp_1"] == str(DUENO), "el formulario no manda a Zutana"
    assert all(f"prev_resp_{i}" in f.campos for i in antes), (
        "falta el valor previo de alguna fila")
    r = base._con_base(conn, lambda: panel.guardar_tareas(
        base._post(dict(f.campos))))
    assert r.status_code == 303
    assert "asignadas=0" in r.headers["location"], r.headers["location"]
    despues = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    assert despues == antes, f"guardar sin tocar cambió: {antes} → {despues}"
    assert not [q for q, _ in conn.sql if q.startswith("UPDATE tareas")], (
        "escribió una tarea sin que nadie tocara nada")
    assert conn.log == [], "dejó una huella sin que nadie cambiara nada"


def test_cambiar_un_desplegable_si_se_guarda_de_punta_a_punta():
    """El contrapeso de la anterior: la prueba de «no cambia nada» no se cumple
    por no escribir jamás. Tocar UNA fila cambia esa y solo esa."""
    conn = _mundo()
    f = _leer(_pantalla(conn))
    campos = dict(f.campos)
    campos["resp_4"] = str(OTRA)              # la que estaba sin responsable
    campos["resp_1"] = ""                     # y a Zutana se la quitamos
    antes = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    r = base._con_base(conn, lambda: panel.guardar_tareas(base._post(campos)))
    assert "asignadas=2" in r.headers["location"], r.headers["location"]
    despues = {t["id"]: t["responsable_chat_id"] for t in conn.tareas}
    assert despues == {**antes, 4: OTRA, 1: None}, despues


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
