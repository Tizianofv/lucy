# -*- coding: utf-8 -*-
"""Mover la fecha de una tarea desde el panel.

LO QUE DECIDIÓ TIZIANO el 13-sep-2026, y lo que cada prueba de acá fija:

  F1 · Mover la fecha desde el panel NO vuelve a armar el aviso de Telegram,
       igual que cuando se mueve por el chat.
  F2 · El panel pide DÍA Y HORA, no solo el día.
  F3 · Mover para MÁS TARDE desde el panel cuenta como posponer, igual que por
       el chat.
  F4 · Se le puede QUITAR la fecha a una tarea desde el panel.
  F5 · Mueven fechas los dos que entran al panel. No hay nada que probar
       aparte: la ruta pide la misma sesión que el resto del panel.

Y lo que no es decisión sino cuidado: que lo hecho desde el panel NO quede
firmado como si lo hubiera hecho Lucy.

LO QUE ESTAS PRUEBAS NO VEN: `psycopg` es de mentira. La base de acá guarda lo
que se le escribe, pero no es Postgres. Que las columnas existan de verdad lo
compara `test_lo_que_escribe_existe_en_el_esquema` contra db/schema.sql, que
describe la base pero no ES la base.

Correr:  python3 -m pytest tests/test_fechas_del_panel.py
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import sys
import textwrap
import types
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import acciones.crud as crud  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO
UTC = timezone.utc
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Una base de mentira que APLICA lo que se le escribe ──────────────────
#
# Aplica el UPDATE a la fila guardada. Así la prueba del aviso (F1) puede mirar
# la fila COMO QUEDÓ y preguntarle al despertador si sonaría, en vez de buscar
# palabras en el SQL.

class _Cursor:
    def __init__(self, base):
        self._base = base
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        b = self._base
        b.sql.append((s, params))
        self._filas = []
        if s.startswith("SELECT") and "FROM tareas" in s:
            tid = params[0]
            fila = b.tareas.get(tid)
            if fila is not None and fila.get("borrado_en") is None:
                self._filas = [dict(fila)]
        elif s.startswith("UPDATE tareas SET"):
            m = re.match(r"UPDATE tareas SET (.*) WHERE id = %s", s)
            columnas = [c.split("=")[0].strip() for c in m.group(1).split(",")]
            fila = b.tareas[params[-1]]
            for col, valor in zip(columnas, params[:-1]):
                fila[col] = valor
        elif s.startswith("INSERT INTO log_acciones"):
            b.log.append((s, params))
            self._filas = [(5000,)]
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _Base:
    def __init__(self, *filas):
        self.tareas = {f["id"]: dict(f) for f in filas}
        self.sql: list = []
        self.log: list = []

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def execute(self, sql, params=None):
        return await _Cursor(self).execute(sql, params)


class _CM:
    def __init__(self, base):
        self._base = base

    async def __aenter__(self):
        return self._base

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, base):
        self._base = base

    def connection(self):
        return _CM(self._base)


def _correr(base, fn):
    guardado = db.pool
    db.pool = _Pool(base)
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(fn())
    finally:
        bucle.close()
        db.pool = guardado


def _tarea(id=1, estado="pendiente", vence_en=None, pospuesta_veces=0,
           avisos_enviados=(), anticipos_min=(0,)):
    """Una fila de `tareas` con todas las columnas que tocan este trabajo."""
    return {"id": id, "titulo": f"tarea {id}", "detalle": None,
            "estado": estado, "vence_en": vence_en,
            "pospuesta_veces": pospuesta_veces,
            "avisos_enviados": list(avisos_enviados),
            "anticipos_min": list(anticipos_min), "recurrencia": None,
            "responsable_chat_id": None, "bandeja_id": 900 + id,
            "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "completado_en": None, "borrado_en": None}


def _escrituras(base):
    return [(s, p) for s, p in base.sql if s.startswith("UPDATE")]


# ═════════════════════════════════════════════════════════════════════════
# F2 · El campo pide DÍA Y HORA, y la hora es la de Santo Domingo
# ═════════════════════════════════════════════════════════════════════════

def test_el_campo_pide_dia_y_hora_y_la_hora_es_de_santo_domingo():
    """Las 3:30 de la tarde del 20-sep en Santo Domingo son las 19:30 en UTC.
    Leída en UTC, la tarea sonaría cuatro horas antes de lo que se eligió."""
    ok, instante = panel._vence_con_hora_valido("2026-09-20T15:30")
    assert ok is True
    assert instante.tzinfo is not None, "guardó una hora sin zona"
    assert instante.astimezone(UTC) == datetime(2026, 9, 20, 19, 30, tzinfo=UTC)
    # La zona sale de config.TZ, la misma con la que el panel reparte grupos.
    assert db.dia_rd(instante) == date(2026, 9, 20)


def test_un_dia_suelto_se_rechaza_porque_la_hora_no_se_inventa():
    """F2: se pide día Y hora. Aceptar «2026-09-20» obligaría a inventar una
    hora, y la hora es la que decide cuándo suena el aviso."""
    assert panel._vence_con_hora_valido("2026-09-20") == (False, None)


def test_vacio_es_quitar_la_fecha():
    """F4: vacío no es un error, es «sin fecha»."""
    assert panel._vence_con_hora_valido("") == (True, None)
    assert panel._vence_con_hora_valido("   ") == (True, None)


def test_lo_que_no_es_el_campo_del_navegador_se_rechaza():
    for malo in ("abc", "2026-13-45T10:00", "2026-02-30T10:00",
                 "2026-09-20T25:00", "2026-09-20 15:30",
                 "2026-09-20T15:30-04:00", "2026-09-20T15:30Z",
                 "0026-09-20T15:30", "1999-12-31T23:59"):
        assert panel._vence_con_hora_valido(malo) == (False, None), malo
    # Con segundos sí: algunos navegadores los mandan.
    assert panel._vence_con_hora_valido("2026-09-20T15:30:00")[0] is True


def test_lo_que_se_pinta_es_lo_que_se_vuelve_a_leer():
    """El valor pintado viaja también como `prev_vence_`. Si pintar y leer
    usaran zonas distintas, una fila que nadie tocó volvería con otra hora y
    se reescribiría en cada envío."""
    for instante in (datetime(2026, 9, 8, 3, 0, tzinfo=UTC),      # 7-sep 23:00 RD
                     datetime(2026, 9, 20, 19, 30, tzinfo=UTC),
                     datetime(2026, 12, 31, 16, 59, tzinfo=UTC)):
        pintado = panel._para_el_campo(instante)
        ok, leido = panel._vence_con_hora_valido(pintado)
        assert ok and leido == instante, (instante, pintado, leido)
    assert panel._para_el_campo(None) == ""
    # Sin zona se lee como UTC, igual que db.dia_rd.
    assert panel._para_el_campo(datetime(2026, 9, 8, 3, 0)) == "2026-09-07T23:00"


# ═════════════════════════════════════════════════════════════════════════
# La escritura
# ═════════════════════════════════════════════════════════════════════════

LUNES = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
MARTES = LUNES + timedelta(days=1)
DOMINGO = LUNES - timedelta(days=1)


def test_F3_mover_para_mas_tarde_cuenta_como_posponer():
    base = _Base(_tarea(vence_en=LUNES, pospuesta_veces=1))
    assert _correr(base, lambda: db.mover_vence(1, MARTES)) is True
    fila = base.tareas[1]
    assert fila["vence_en"] == MARTES
    assert fila["pospuesta_veces"] == 2, (
        "mover para más tarde desde el panel no contó como posponer")


def test_adelantar_poner_fecha_o_quitarla_no_es_posponer():
    casos = ((LUNES, DOMINGO, "adelantarla"),
             (None, MARTES, "ponerle fecha a una que no tenía"),
             (LUNES, None, "quitarle la fecha"))
    for vieja, nueva, que in casos:
        base = _Base(_tarea(vence_en=vieja, pospuesta_veces=3))
        assert _correr(base, lambda: db.mover_vence(1, nueva)) is True, que
        assert base.tareas[1]["pospuesta_veces"] == 3, f"contó {que} como posponer"
        assert base.tareas[1]["vence_en"] == nueva, que


def test_F4_quitar_la_fecha_la_deja_sin_fecha_y_con_su_huella():
    base = _Base(_tarea(vence_en=LUNES))
    assert _correr(base, lambda: db.mover_vence(1, None)) is True
    assert base.tareas[1]["vence_en"] is None
    assert db.grupo_de_tarea("pendiente", base.tareas[1]["vence_en"],
                             date(2026, 9, 13)) == "sin_fecha"
    (s, p), = base.log
    assert p[3] == "fecha quitada desde el panel de tareas"


def test_F1_mover_la_fecha_no_vuelve_a_armar_un_aviso_que_ya_sono():
    """La tarea avisó a la hora ({0}). Se mueve para mañana. A la hora nueva,
    el despertador NO la vuelve a sonar.

    Se pregunta al despertador de verdad (`_campanadas`, la decisión pura de
    qué suena) sobre la fila COMO QUEDÓ, no se buscan palabras en el SQL. Si
    la escritura vaciara `avisos_enviados`, esto se pone rojo.
    """
    import cerebro.despertador as despertador

    base = _Base(_tarea(vence_en=LUNES, avisos_enviados=[0], anticipos_min=[0]))
    assert _correr(base, lambda: db.mover_vence(1, MARTES)) is True
    fila = base.tareas[1]
    a_la_hora_nueva = despertador._campanadas(
        fila["anticipos_min"], set(fila["avisos_enviados"]), 0)
    assert a_la_hora_nueva == set(), (
        f"mover la fecha desde el panel volvió a armar el aviso: {fila}")
    assert fila["anticipos_min"] == [0], "cambió a qué minutos avisa la tarea"


def test_F1_es_lo_mismo_que_hace_el_chat():
    """«Igual que pasa hoy por el chat»: la misma tarea, movida por
    `crud.editar`, queda con las mismas columnas de aviso que movida por el
    panel. Si uno de los dos caminos empezara a rearmar el aviso, se separan."""
    import cerebro.despertador as despertador

    por_el_chat = _Base(_tarea(vence_en=LUNES, avisos_enviados=[0]))
    _correr(por_el_chat, lambda: crud.editar(
        "tareas", 1, {"vence_en": MARTES.isoformat()}, motivo="prueba"))
    por_el_panel = _Base(_tarea(vence_en=LUNES, avisos_enviados=[0]))
    _correr(por_el_panel, lambda: db.mover_vence(1, MARTES))

    for base in (por_el_chat, por_el_panel):
        f = base.tareas[1]
        assert despertador._campanadas(
            f["anticipos_min"], set(f["avisos_enviados"]), 0) == set()
    columnas = ("vence_en", "avisos_enviados", "anticipos_min", "pospuesta_veces")
    assert ({c: por_el_chat.tareas[1][c] for c in columnas}
            == {c: por_el_panel.tareas[1][c] for c in columnas}), (
        "el chat y el panel dejan la tarea distinta al mover la misma fecha")


def test_los_dos_caminos_le_preguntan_a_LA_MISMA_funcion_si_es_posponer():
    """LOS HERMANOS. Se cambia la fuente compartida y se exige que los DOS
    reaccionen. Una prueba de comportamiento sola no vería que uno de los dos
    tiene su propia copia del criterio mientras las dos copias coincidan."""
    original = db.cuenta_como_posposicion
    try:
        for respuesta, esperado in ((True, 1), (False, 0)):
            db.cuenta_como_posposicion = lambda *a, _r=respuesta: _r
            # Un cambio que el criterio verdadero NO cuenta (adelantar), y otro
            # que SÍ cuenta (atrasar): con la fuente cambiada, los dos caminos
            # tienen que obedecerla a ella y no a una copia propia.
            for nueva in (DOMINGO, MARTES):
                chat = _Base(_tarea(vence_en=LUNES))
                _correr(chat, lambda: crud.editar(
                    "tareas", 1, {"vence_en": nueva.isoformat()}, motivo="p"))
                web = _Base(_tarea(vence_en=LUNES))
                _correr(web, lambda: db.mover_vence(1, nueva))
                assert chat.tareas[1]["pospuesta_veces"] == esperado, (
                    "crud.editar no le preguntó a db.cuenta_como_posposicion")
                assert web.tareas[1]["pospuesta_veces"] == esperado, (
                    "db.mover_vence no le preguntó a db.cuenta_como_posposicion")
    finally:
        db.cuenta_como_posposicion = original


def test_el_criterio_de_posponer_entero():
    c = db.cuenta_como_posposicion
    assert c("pendiente", LUNES, "pendiente", MARTES) is True
    assert c("pendiente", LUNES, "pendiente", DOMINGO) is False
    assert c("pendiente", LUNES, "pendiente", LUNES) is False
    assert c("pendiente", None, "pendiente", MARTES) is False
    assert c("pendiente", LUNES, "pendiente", None) is False
    assert c("hecha", LUNES, "pendiente", MARTES) is False
    assert c("pendiente", LUNES, "hecha", MARTES) is False
    # Una con zona y otra sin zona no se pueden comparar: no cuenta, no revienta.
    assert c("pendiente", LUNES, "pendiente", datetime(2026, 9, 20)) is False


def test_solo_se_mueven_pendientes_y_lo_igual_no_se_reescribe():
    for fila, que in ((_tarea(estado="hecha", vence_en=LUNES), "una hecha"),
                      (_tarea(estado="descartado", vence_en=LUNES), "otro estado"),
                      (_tarea(vence_en=MARTES), "la misma fecha"),
                      (_tarea(vence_en=None), "sin fecha a sin fecha")):
        base = _Base(fila)
        nueva = None if fila["vence_en"] is None else MARTES
        assert _correr(base, lambda: db.mover_vence(1, nueva)) is False, que
        assert not _escrituras(base), f"escribió {que}"
        assert not base.log, f"dejó huella de {que}"

    # No existe, o está en la papelera: el SELECT filtra `borrado_en IS NULL`.
    borrada = _tarea(vence_en=LUNES)
    borrada["borrado_en"] = LUNES
    base = _Base(borrada)
    assert _correr(base, lambda: db.mover_vence(1, MARTES)) is False
    assert "borrado_en IS NULL" in base.sql[0][0]
    assert _correr(_Base(), lambda: db.mover_vence(999, MARTES)) is False


def test_la_huella_dice_panel_y_no_lucy():
    """Lo que hace una persona en el panel no puede quedar firmado como si lo
    hubiera hecho Lucy. `crud._registrar` firma 'lucy'; por eso no se usa."""
    base = _Base(_tarea(vence_en=LUNES))
    _correr(base, lambda: db.mover_vence(1, MARTES))
    (s, p), = base.log
    assert "VALUES ('panel', 'editar', 'tareas'" in s, s
    assert "'lucy'" not in s
    assert p[0] == 1 and p[4] == 901, "la huella no apunta a la tarea o a su bandeja"
    assert "crud" not in _nombres_de_codigo(db.mover_vence), (
        "mover_vence escribe por acciones.crud, que firma 'lucy'")


def test_deshacer_la_huella_solo_devuelve_lo_que_este_cambio_toco():
    """`crud.deshacer` devuelve TODAS las columnas del `antes`. Con la fila
    entera, deshacer un cambio de fecha podía devolverle también el estado o el
    título que tenía la tarea en ese momento. Se corre el deshacer de verdad
    sobre la huella y se mira qué columnas escribe."""
    base = _Base(_tarea(vence_en=LUNES, pospuesta_veces=1))
    _correr(base, lambda: db.mover_vence(1, MARTES))
    (s, p), = base.log
    antes = json.loads(p[1])
    assert set(antes) - crud.NO_EDITABLES == {"vence_en", "pospuesta_veces"}, antes

    huella = {"accion": "editar", "tabla": "tareas", "registro_id": 1,
              "antes": antes, "despues": json.loads(p[2])}

    class _CursorDeshacer(_Cursor):
        """Sirve la huella; anota el UPDATE de vuelta sin interpretarlo."""

        async def execute(self, sql, params=None):
            s2 = " ".join(sql.split())
            if "FROM log_acciones" in s2 or s2.startswith("UPDATE tareas t SET"):
                self._base.sql.append((s2, params))
                self._filas = [huella] if "FROM log_acciones" in s2 else []
                return self
            return await super().execute(sql, params)

    class _BaseDeshacer(_Base):
        def cursor(self, row_factory=None):
            return _CursorDeshacer(self)

        async def execute(self, sql, params=None):
            return await _CursorDeshacer(self).execute(sql, params)

    h = _BaseDeshacer(_tarea(vence_en=MARTES, pospuesta_veces=2))
    _correr(h, lambda: crud.deshacer(77))
    vuelta = [s2 for s2, _ in h.sql if s2.startswith("UPDATE tareas t SET")]
    assert vuelta, "deshacer no escribió nada"
    escritas = set(re.findall(r"(\w+) = r\.\w+", vuelta[0]))
    assert escritas == {"vence_en", "pospuesta_veces"}, escritas


def _nombres_de_codigo(fn) -> set:
    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return ({n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)})


def test_lo_que_escribe_existe_en_el_esquema():
    """Las columnas del UPDATE salen del SQL de la función (sin su docstring) y
    se buscan en db/schema.sql. La suite es hermética: un typo acá sale verde
    y revienta la primera vez que alguien mueva una fecha en producción."""
    arbol = ast.parse(textwrap.dedent(inspect.getsource(db.mover_vence)))
    cuerpo = arbol.body[0].body[1:]
    sql = " ".join(n.value for nodo in cuerpo for n in ast.walk(nodo)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    sql = " ".join(sql.split())
    sets = re.findall(r"UPDATE tareas SET (.*?) WHERE", sql)
    assert len(sets) == 1, f"se esperaba un solo UPDATE de tareas: {sets}"
    escritas = set(re.findall(r"\b([a-z_]+) = %s", sets[0]))
    leidas = set(re.findall(r"SELECT (.*?) FROM tareas", sql)[0].replace(" ", "")
                 .split(","))
    assert escritas == {"vence_en", "pospuesta_veces"}, escritas
    faltan = (escritas | leidas) - set(db.columnas_declaradas()["tareas"])
    assert not faltan, f"columnas que db/schema.sql no declara: {faltan}"


# ═════════════════════════════════════════════════════════════════════════
# La ruta y la pantalla
# ═════════════════════════════════════════════════════════════════════════

def _post(campos: dict, con_sesion: bool = True, chat: int = DUENO):
    from starlette.requests import Request

    cuerpo = urlencode(campos).encode()
    cabeceras = [(b"host", b"t"),
                 (b"content-type", b"application/x-www-form-urlencoded"),
                 (b"content-length", str(len(cuerpo)).encode())]
    if con_sesion:
        galleta = f"{panel.COOKIE}={auth.crear_token(chat, auth.VIDA_SESION)}"
        cabeceras.append((b"cookie", galleta.encode()))

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "https", "server": ("t", 443), "path": "/tareas",
                    "root_path": "", "query_string": b"", "headers": cabeceras,
                    "app": panel.app}, recibir)


def _guardar(campos, con_sesion=True):
    """Manda el formulario con las escrituras ESPIADAS, en el orden en que
    llegan. Devuelve (respuesta, [(qué, id, valor)])."""
    llamadas: list = []

    async def _mover(tid, vence):
        llamadas.append(("mover", tid, vence))
        return True

    async def _hecha(tid):
        llamadas.append(("hecha", tid, None))
        return True

    guardados = db.mover_vence, db.marcar_tarea_hecha
    db.mover_vence, db.marcar_tarea_hecha = _mover, _hecha
    bucle = asyncio.new_event_loop()
    try:
        r = bucle.run_until_complete(panel.guardar_tareas(_post(campos, con_sesion)))
    finally:
        bucle.close()
        db.mover_vence, db.marcar_tarea_hecha = guardados
    return r, llamadas


def test_la_ruta_mueve_solo_lo_que_cambio_y_lo_lee_en_santo_domingo():
    r, llamadas = _guardar({
        "prev_vence_1": "2026-09-14T10:00", "vence_1": "2026-09-15T16:45",
        "prev_vence_2": "2026-09-14T10:00", "vence_2": "2026-09-14T10:00",
        "prev_vence_3": "", "vence_3": "",
    })
    assert r.status_code == 303
    assert llamadas == [("mover", 1, datetime(2026, 9, 15, 16, 45,
                                               tzinfo=config.TZ))], llamadas
    assert "movidas=1" in r.headers["location"]


def test_la_ruta_quita_la_fecha_cuando_el_campo_llega_vacio():
    r, llamadas = _guardar({"prev_vence_4": "2026-09-14T10:00", "vence_4": ""})
    assert llamadas == [("mover", 4, None)]


def test_una_fecha_que_no_se_entiende_no_escribe_nada():
    r, llamadas = _guardar({"prev_vence_1": "2026-09-14T10:00",
                            "vence_1": "2026-09-15",
                            "prev_vence_2": "", "vence_2": "mañana",
                            "vence_abc": "2026-09-15T10:00"})
    assert r.status_code == 303
    assert llamadas == [], f"escribió una fecha que no vale: {llamadas}"
    assert "movidas=0" in r.headers["location"]


def test_mover_y_cerrar_en_el_mismo_envio_guarda_las_dos_cosas():
    """Las fechas van ANTES que las casillas. Al revés, la tarea ya estaría
    hecha cuando llegara su fecha, mover_vence la rechazaría y el cambio que
    la persona escribió se perdería."""
    r, llamadas = _guardar({"prev_1": "pendiente", "hecha_1": "1",
                            "prev_vence_1": "2026-09-14T10:00",
                            "vence_1": "2026-09-16T10:00"})
    assert [q for q, _, _ in llamadas] == ["mover", "hecha"], llamadas


def test_sin_sesion_no_se_mueve_nada():
    r, llamadas = _guardar({"prev_vence_1": "", "vence_1": "2026-09-16T10:00"},
                           con_sesion=False)
    assert r.status_code == 401
    assert llamadas == []


def test_la_pantalla_ofrece_dia_y_hora_solo_en_las_pendientes():
    """Se llama a la ruta y se exige que la plantilla se RENDERICE: una
    variable que la plantilla usa y la ruta no manda se cae acá."""
    from starlette.requests import Request

    import test_panel_tareas as tp

    manana_rd = datetime.now(config.TZ).date() + timedelta(days=1)
    vence = datetime(manana_rd.year, manana_rd.month, manana_rd.day, 15, 30,
                     tzinfo=config.TZ)
    filas = [tp._fila(1, vence_en=vence.astimezone(UTC)),
             tp._fila(2, estado="hecha", vence_en=vence.astimezone(UTC)),
             tp._fila(3)]
    galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
    peticion = Request({"type": "http", "http_version": "1.1", "method": "GET",
                        "scheme": "https", "server": ("t", 443),
                        "path": "/tareas", "root_path": "", "query_string": b"",
                        "headers": [(b"host", b"t"), (b"cookie", galleta.encode())],
                        "app": panel.app})
    r, _ = tp._con_base(filas, lambda: panel.tareas(peticion, movidas=2))
    assert r.status_code == 200
    html = r.body.decode()

    valor = vence.strftime("%Y-%m-%dT%H:%M")
    assert re.search(rf'type="datetime-local" name="vence_1"\s+value="{valor}"',
                     html), "la pendiente no tiene su campo de día y hora en hora RD"
    assert f'name="prev_vence_1"\n               value="{valor}"' in html
    assert 'name="vence_3"' in html and 'name="prev_vence_3"' in html, (
        "una pendiente sin fecha también tiene que poder recibir una")
    assert 'name="vence_2"' not in html, "ofrece mover la fecha de una tarea hecha"
    assert "Fecha cambiada en 2" in html
    assert html.count("<form") == 1, "la lista tiene que seguir con UN formulario"


def test_la_nota_del_panel_que_lee_lucy_dice_que_ahi_se_mueven_fechas():
    """Lucy decide si algo «está en el panel» mirando el menú. Sin esto, a la
    pregunta «¿puedo cambiar la fecha en el panel?» diría que no."""
    import web.menu as menu
    tareas = [p for p in menu.pantallas() if p.ruta == "/tareas"]
    assert tareas and "fecha" in tareas[0].nota, tareas


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
