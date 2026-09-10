# -*- coding: utf-8 -*-
"""Escribir una tarea a mano desde el panel.

QUÉ SE PRUEBA CON MÁS SAÑA, y por qué son ésas y no otras:

  · QUE «QUIÉN LA ANOTÓ» NO MIENTA. Esa columna del panel no es un campo de
    `tareas`: sale de `tareas.bandeja_id → bandeja.chat_id`. Medido acá abajo
    (`test_medido_una_tarea_sin_bandeja_no_registra_a_nadie`): con `bandeja_id`
    nulo la tarea sale igual —bien— pero la columna dice «—», o sea que no
    queda registrado quién la escribió. Por eso el alta crea la fila de bandeja
    con el chat de la SESIÓN y la tarea cuelga de ella.

  · QUE EL DÍA SEA EL DE SANTO DOMINGO. `vence_en` es TIMESTAMPTZ, un
    INSTANTE. El formulario da un DÍA. Armar ese instante en UTC hace que una
    tarea escrita de noche nazca en el día equivocado, y no habría forma de
    verlo mirando la pantalla hasta pasadas las 8 de la noche.

  · QUE LA TAREA NUEVA SE VEA. La prueba de punta a punta manda el formulario y
    después PINTA /tareas con la misma base falsa: si la tarea no aparece en su
    grupo, se pone roja. Un guardado que no se ve obliga a confiar.

  · QUE SIN TÍTULO NO SE CREE Y QUE SIN SESIÓN NO SE ESCRIBA. Cada ruta nueva
    es una puerta nueva, y ésta ESCRIBE.

LO QUE ESTOS TESTS NO PUEDEN VER, medido y no supuesto: `psycopg` está
reemplazado por un módulo falso, así que nada de acá habla con Postgres. La
base de mentira de este archivo SÍ guarda lo que se le inserta y lo devuelve en
el SELECT del panel —por eso la prueba de punta a punta significa algo— pero
sigue sin poder ver lo que Postgres haría con esas filas. Que las columnas
existan de verdad lo cubre `tools/humo.py`, que necesita DATABASE_URL; acá se
comparan contra `db/schema.sql`, que describe la base pero no ES la base.

Correr:  python3 -m pytest tests/test_tarea_a_mano.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import types
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO
OTRO = DUENO + 7
UTC = timezone.utc
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Una base de mentira que SÍ se acuerda de lo que se le escribió ────────
#
# La del panel de tareas devuelve filas preparadas de antemano. Ésta guarda lo
# que se inserta, porque la pregunta central de este archivo —«¿la tarea recién
# escrita aparece en la pantalla?»— no se puede contestar con filas puestas a
# mano: eso probaría que la plantilla pinta lo que se le da, no que lo que se
# guardó es lo que se pinta.

class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        c = self._conn
        c.sql.append((s, params))
        self._filas = []

        if s.startswith("INSERT INTO bandeja"):
            c.sig_bandeja += 1
            fila = {"id": c.sig_bandeja, "chat_id": params[0]}
            c.bandeja.append(fila)
            self._filas = [fila]

        elif s.startswith("INSERT INTO tareas"):
            c.sig_tarea += 1
            bandeja_id, titulo, vence_en, anticipos = params
            fila = {"id": c.sig_tarea, "bandeja_id": bandeja_id,
                    "titulo": titulo, "vence_en": vence_en,
                    "anticipos_min": anticipos, "estado": "pendiente",
                    "creado_en": datetime(2026, 9, 9, 12, tzinfo=UTC),
                    "completado_en": None, "borrado_en": None}
            c.tareas.append(fila)
            self._filas = [fila]

        elif s.startswith("INSERT INTO log_acciones"):
            c.log.append((s, params))

        elif s.startswith("SELECT t.id, t.titulo"):
            # El LEFT JOIN contra bandeja, a mano: `quien` es el chat de la
            # fila de bandeja, o None si la tarea no cuelga de ninguna.
            chats = {b["id"]: b["chat_id"] for b in c.bandeja}
            self._filas = [
                dict(t, quien=chats.get(t.get("bandeja_id")))
                for t in c.tareas if t.get("borrado_en") is None]
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _Conn:
    def __init__(self, tareas=None, bandeja=None):
        self.tareas = list(tareas or [])
        self.bandeja = list(bandeja or [])
        self.log: list = []
        self.sql: list = []
        self.sig_bandeja = 900
        self.sig_tarea = 10

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def execute(self, sql, params=None):
        return await _Cursor(self).execute(sql, params)


class _CM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _CM(self._conn)


def _con_base(conn, fn):
    """Corre una corutina con la base falseada. Devuelve el resultado."""
    guardado = db.pool
    db.pool = _Pool(conn)
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(fn())
        finally:
            bucle.close()
    finally:
        db.pool = guardado


# ── Las peticiones ───────────────────────────────────────────────────────

def _cabeceras(con_sesion: bool, chat: int, extra=()):
    cs = [(b"host", b"t")] + list(extra)
    if con_sesion:
        galleta = f"{panel.COOKIE}={auth.crear_token(chat, auth.VIDA_SESION)}"
        cs.append((b"cookie", galleta.encode()))
    return cs


def _post(campos: dict, con_sesion: bool = True, chat: int = DUENO):
    from starlette.requests import Request

    cuerpo = urlencode(campos).encode()
    extra = [(b"content-type", b"application/x-www-form-urlencoded"),
             (b"content-length", str(len(cuerpo)).encode())]

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "https", "server": ("t", 443),
                    "path": "/tareas/nueva", "root_path": "",
                    "query_string": b"",
                    "headers": _cabeceras(con_sesion, chat, extra),
                    "app": panel.app}, recibir)


def _get(ruta: str, con_sesion: bool = True, chat: int = DUENO):
    from starlette.requests import Request
    return Request({"type": "http", "http_version": "1.1", "method": "GET",
                    "scheme": "https", "server": ("t", 443), "path": ruta,
                    "root_path": "", "query_string": b"",
                    "headers": _cabeceras(con_sesion, chat),
                    "app": panel.app})


# ── El campo de la fecha ─────────────────────────────────────────────────

def test_la_fecha_se_arma_en_santo_domingo_y_no_en_utc():
    """`vence_en` es TIMESTAMPTZ —un instante— y el formulario da un DÍA.

    Si el instante se arma en UTC, el día que el panel muestra deja de ser el
    que la persona eligió: el 00:00 de cualquier día en UTC cae en el día
    ANTERIOR en Santo Domingo. Esta prueba se pone roja si alguien cambia la
    zona por `timezone.utc`, y no habría forma de verlo mirando la pantalla.
    """
    ok, v = panel._vence_valido("2026-09-10")
    assert ok and v is not None
    # 1. El día que se lee de vuelta es el que se escribió.
    assert db.dia_rd(v) == date(2026, 9, 10), (
        f"la tarea nace en otro día: {db.dia_rd(v)}")
    # 2. Y el instante está armado EN SANTO DOMINGO. Sin esto, un 23:59 armado
    #    en UTC pasaría la comprobación de arriba (23:59 UTC son las 19:59 del
    #    mismo día en RD) y aun así sería el instante equivocado.
    assert v.utcoffset() == timedelta(hours=-4), (
        f"el instante no está en hora de Santo Domingo: {v.utcoffset()}")
    # 3. Y la zona sale de config.TZ, la misma de la que la lee `db.dia_rd`.
    #    Dos copias de una zona horaria se desincronizan como cualquier otra.
    assert v.tzinfo is config.TZ


def test_el_dia_se_guarda_entero_y_no_a_medias():
    """«Vence el jueves» quiere decir que el jueves entero todavía sirve. El
    instante que representa ese día es su FINAL, no su comienzo: con 00:00 la
    tarea se leería como vencida durante todo el día en que vence."""
    _, v = panel._vence_valido("2026-09-10")
    assert (v.hour, v.minute) == (23, 59), f"la hora quedó en {v.hour}:{v.minute}"


def test_sin_fecha_se_acepta_y_no_se_inventa_ninguna():
    """El campo es opcional. Vacío NO cae a hoy: en este panel «sin fecha» es
    un grupo con nombre propio, y las 4 tareas sin `vence_en` de producción
    llevaban meses invisibles justamente por no tenerlo."""
    for vacio in ("", "   ", None):
        ok, v = panel._vence_valido(vacio)
        assert ok is True, f"rechazó una fecha vacía: {vacio!r}"
        assert v is None, "le inventó una fecha a una tarea que no tiene"


def test_una_fecha_que_no_es_fecha_se_rechaza():
    for basura in ("abc", "2026-13-45", "2026-02-30", "10/09/2026", "0026-09-10"):
        ok, v = panel._vence_valido(basura)
        assert ok is False, f"aceptó «{basura}» como fecha"
        assert v is None


def test_el_futuro_se_acepta_porque_una_tarea_no_es_un_gasto():
    """/efectivo rechaza el futuro: un gasto en efectivo es plata que YA salió.
    Una tarea que vence el mes que viene es el caso normal de una tarea. Mismo
    tipo de campo, preguntas opuestas."""
    dentro_de_un_ano = (panel._hoy() + timedelta(days=365)).isoformat()
    ok, v = panel._vence_valido(dentro_de_un_ano)
    assert ok is True and v is not None, "rechazó una tarea a futuro"


# ── Sin título no hay tarea ──────────────────────────────────────────────

def _mandar(campos: dict, con_sesion: bool = True, chat: int = DUENO,
            conn: _Conn | None = None):
    """Manda el formulario. Devuelve (respuesta, conn)."""
    conn = conn if conn is not None else _Conn()
    r = _con_base(conn, lambda: panel.crear_tarea(
        _post(campos, con_sesion, chat)))
    return r, conn


def test_sin_titulo_no_se_crea_y_se_dice_por_que():
    for vacio in ({}, {"titulo": ""}, {"titulo": "    "},
                  {"titulo": "", "vence": "2026-09-10"}):
        r, conn = _mandar(vacio)
        assert r.status_code == 303
        assert r.headers["location"] == "/tareas/nueva?error=titulo", (
            f"no dijo por qué: {r.headers['location']}")
        assert conn.tareas == [], f"creó una tarea sin título: {vacio!r}"
        assert conn.bandeja == [], "dejó una fila de bandeja huérfana"


def test_un_titulo_larguisimo_se_rechaza_antes_de_la_base():
    """`tareas.titulo` es TEXT y no tiene tope, así que el tope lo pone acá o
    no lo pone nadie. Es la misma vara que `concepto` en POST /efectivo."""
    r, conn = _mandar({"titulo": "x" * (panel.LARGO_TITULO + 1)})
    assert r.headers["location"] == "/tareas/nueva?error=titulo"
    assert conn.tareas == []
    # Y justo en el largo permitido SÍ entra: un tope que rechaza el borde es
    # un tope distinto del que dice la pantalla en su `maxlength`.
    r, conn = _mandar({"titulo": "x" * panel.LARGO_TITULO})
    assert r.status_code == 303 and conn.tareas, "rechazó el largo que permite"


def test_una_fecha_ilegible_no_crea_la_tarea_a_medias():
    r, conn = _mandar({"titulo": "algo", "vence": "2026-99-99"})
    assert r.headers["location"] == "/tareas/nueva?error=fecha"
    assert conn.tareas == [], "creó la tarea igual, con la fecha tirada"
    assert conn.bandeja == [], "dejó una fila de bandeja huérfana"


# ── Las dos puertas piden sesión ─────────────────────────────────────────

def test_la_ruta_que_escribe_exige_sesion():
    """Sin esto, un POST de cualquiera escribe en la lista de esta casa."""
    r, conn = _mandar({"titulo": "tarea de un extraño"}, con_sesion=False)
    assert r.status_code == 401, f"entró sin cookie: {r.status_code}"
    assert conn.tareas == [], "escribió sin sesión"
    assert conn.bandeja == [], "escribió sin sesión"


def test_un_chat_ajeno_bien_firmado_tampoco_escribe():
    """Un token válido y un chat permitido son dos preguntas distintas."""
    ajeno = DUENO + 100000
    assert ajeno not in config.CHAT_IDS_PERMITIDOS
    r, conn = _mandar({"titulo": "tarea ajena"}, chat=ajeno)
    assert r.status_code == 401, f"entró un chat ajeno: {r.status_code}"
    assert conn.tareas == []


def test_el_formulario_exige_sesion():
    bucle = asyncio.new_event_loop()
    try:
        r = bucle.run_until_complete(
            panel.tarea_nueva(_get("/tareas/nueva", con_sesion=False)))
        assert r.status_code == 401, f"entró sin cookie: {r.status_code}"
    finally:
        bucle.close()


# ── De quién queda constancia ────────────────────────────────────────────

def test_medido_una_tarea_sin_bandeja_no_registra_a_nadie():
    """LA MEDICIÓN QUE JUSTIFICA EL DISEÑO, y por eso está escrita como prueba
    y no en un comentario.

    Una tarea con `bandeja_id` NULO no deja constancia de NADIE: `bandeja` es
    el único sitio de este sistema donde queda escrito de dónde salió una fila.
    Por eso `crear_tarea_desde_el_panel` escribe dos filas y no una.

    LO QUE CAMBIÓ EL 10-SEP-2026, y hay que decirlo o esta prueba se lee mal:
    esa constancia YA NO SE PINTA en el panel. La columna «Quién la anotó»
    salía de `tareas.bandeja_id → bandeja.chat_id` y Tiziano la sacó —«nno es
    relevante quien la anoto»—; lo que se ve en su lugar es el RESPONSABLE, que
    es otra pregunta. La constancia sigue haciendo falta igual: es la
    trazabilidad de la fila y el `bandeja_id` que viaja en cada huella de
    `log_acciones`.

    Así que lo que se mide acá es lo que quedó siendo verdad: la fila SALE en
    el panel aunque le falte —esconderla sería peor—, y sin `bandeja_id` no hay
    de dónde sacar de quién es.
    """
    conn = _Conn(tareas=[{"id": 1, "titulo": "sin dueño", "estado": "pendiente",
                          "vence_en": None, "bandeja_id": None,
                          "responsable_chat_id": None,
                          "creado_en": datetime(2026, 9, 1, tzinfo=UTC),
                          "borrado_en": None}])
    datos = _con_base(conn, lambda: db.tareas_por_grupo(hoy=date(2026, 9, 9)))
    filas = [f for g in datos["grupos"] for f in g["filas"]]
    assert len(filas) == 1, "la tarea sin bandeja desapareció del panel"
    assert filas[0]["bandeja_id"] is None, (
        "sin bandeja_id no hay de dónde sacar de quién salió la tarea")

    html = _con_base(conn, lambda: panel.tareas(_get("/tareas"))).body.decode()
    assert "sin dueño" in html
    assert "Quién la anotó" not in html, (
        "la columna vieja volvió a la pantalla; se cambió por Responsable, no "
        "se conservan las dos")


def test_la_tarea_a_mano_queda_a_nombre_de_quien_entro():
    """El chat sale de la SESIÓN —el dato que ya se comprobó para dejar
    entrar—, no de un campo del formulario. Un formulario que preguntara quién
    sos aceptaría la respuesta que le den."""
    r, conn = _mandar({"titulo": "comprar cinta"})
    assert r.status_code == 303
    assert len(conn.bandeja) == 1, "no dejó constancia de quién la anotó"
    assert conn.bandeja[0]["chat_id"] == DUENO
    assert conn.tareas[0]["bandeja_id"] == conn.bandeja[0]["id"], (
        "la tarea no cuelga de la fila que dice quién la anotó")

    # Y el formulario NO puede decidir a nombre de quién queda.
    r, conn = _mandar({"titulo": "otra", "chat_id": str(OTRO),
                       "quien": str(OTRO), "bandeja_id": "1"})
    assert conn.bandeja[0]["chat_id"] == DUENO, (
        "un campo del formulario cambió a nombre de quién quedó la tarea")


def test_la_fila_de_bandeja_no_le_pone_palabras_a_nadie():
    """La fila de bandeja va MUDA: sin `contenido_raw`, sin `transcripcion` y
    sin `respuesta_lucy`.

    Es lo que la deja fuera de las dos consultas que arman la memoria del
    agente: las dos exigen `coalesce(transcripcion, contenido_raw) IS NOT NULL
    OR respuesta_lucy IS NOT NULL`. Guardar ahí el título metería en la
    conversación una frase que nadie dijo por Telegram, y el sistema
    recordaría una voz que no existió.

    Las dos puntas se comprueban contra el CÓDIGO de las dos consultas, no
    contra una copia de su texto escrita acá.
    """
    import cerebro.memoria as memoria
    fuente = inspect.getsource(db.crear_tarea_desde_el_panel)
    insert = re.search(r"INSERT INTO bandeja\s*\(([^)]*)\)", fuente)
    columnas = {c.strip() for c in insert.group(1).split(",") if c.strip()}
    mudas = {"contenido_raw", "transcripcion", "respuesta_lucy"}
    assert not (columnas & mudas), (
        f"la fila de bandeja habla por alguien: {columnas & mudas}")

    filtro = "coalesce(transcripcion, contenido_raw) IS NOT NULL"
    for fn in (db.ultimos_intercambios, memoria.indexar_pendientes):
        assert filtro in " ".join(inspect.getsource(fn).split()), (
            f"{fn.__name__} ya no descarta las filas mudas: la de esta tarea "
            "empezaría a entrar en la memoria del agente")


# ── La huella ────────────────────────────────────────────────────────────

def test_la_escritura_deja_huella_en_log_acciones():
    """Toda escritura del panel es auditable y reversible. Sin la fila de
    log_acciones no hay deshacer, porque el deshacer de este proyecto ES
    log_acciones."""
    r, conn = _mandar({"titulo": "pagar la luz", "vence": "2026-09-10"})
    assert len(conn.log) == 1, f"escribió sin dejar huella: {len(conn.log)}"
    sql, params = conn.log[0]
    assert "INSERT INTO log_acciones" in sql
    assert "'panel'" in sql, "la huella tiene que decir que fue el panel"
    assert "'crear'" in sql, (
        "`deshacer()` revierte la rama 'crear': con otra palabra no se deshace")
    assert "'tareas'" in sql
    registro_id, despues, bandeja_id = params
    assert registro_id == conn.tareas[0]["id"]
    assert bandeja_id == conn.bandeja[0]["id"], (
        "la huella no dice de qué fila de bandeja salió")
    guardado = json.loads(despues)
    assert guardado["titulo"] == "pagar la luz"
    assert guardado["id"] == conn.tareas[0]["id"], (
        "`despues` tiene que ser lo que de verdad quedó guardado")


def test_la_tarea_nace_muda_y_no_dispara_ningun_telegram():
    """El formulario pide un DÍA, no una hora. Con `anticipos_min` en su
    default de la tabla ('{0}') el despertador mandaría un Telegram al instante
    que guardamos, que es un instante que nadie eligió.

    Las dos puntas: que el alta escriba una lista VACÍA, y que la consulta del
    despertador siga exigiendo `cardinality(anticipos_min) > 0` — que es lo que
    hace que una lista vacía no avise nunca. Si mañana esa consulta cambia,
    esto se pone rojo antes de que salga el primer aviso a medianoche.
    """
    import cerebro.despertador as despertador
    _, conn = _mandar({"titulo": "algo", "vence": "2026-09-10"})
    assert conn.tareas[0]["anticipos_min"] == [], (
        f"la tarea nació con avisos: {conn.tareas[0]['anticipos_min']}")
    assert db.SIN_ANTICIPOS == []

    fuente = " ".join(inspect.getsource(despertador).split())
    assert "cardinality(anticipos_min) > 0" in fuente, (
        "el despertador ya no exime a las filas mudas: una tarea escrita en el "
        "panel empezaría a avisar a una hora que nadie eligió")


def test_no_hay_guarda_de_duplicados_y_es_una_decision_dicha():
    """DOS VECES EL MISMO TÍTULO CREA DOS TAREAS, a propósito.

    La guarda de `acciones/crud.py:_duplicado_pendiente` existe porque EL
    AGENTE re-crea lo que acaba de crear. Una persona escribiendo en un
    formulario sabe lo que escribe. Y su coincidencia es
    `titulo = %s AND vence_en IS NOT DISTINCT FROM %s`: acá la fecha es
    opcional, así que dos tareas SIN FECHA con el mismo título chocarían
    siempre — «llamar al banco» de la semana pasada se comería la de hoy.

    Peor todavía: aquella función devuelve el id de la vieja, o sea que el
    panel diría «creada» sobre algo que no creó.
    """
    conn = _Conn()
    _mandar({"titulo": "llamar al banco"}, conn=conn)
    _mandar({"titulo": "llamar al banco"}, conn=conn)
    assert len(conn.tareas) == 2, (
        f"se fusionaron dos tareas que una persona escribió aparte: "
        f"{len(conn.tareas)}")
    assert conn.tareas[0]["id"] != conn.tareas[1]["id"]
    assert len(conn.log) == 2, "una de las dos quedó sin huella"


# ── De punta a punta: se escribe y SE VE ─────────────────────────────────

def _crear_y_pintar(campos: dict):
    """Manda el formulario y DESPUÉS SIGUE EL REDIRECT, con la misma base.

    El `creada` que se le pasa a la pantalla sale del Location de la respuesta,
    no de lo que esta prueba sepa: así, un redirect que no lleve el número
    también se pone rojo.
    """
    conn = _Conn()
    r = _con_base(conn, lambda: panel.crear_tarea(_post(campos)))
    assert r.status_code == 303, f"el alta devolvió {r.status_code}"
    destino = r.headers["location"]
    m = re.search(r"creada=(\d+)", destino)
    creada = int(m.group(1)) if m else 0
    html = _con_base(conn, lambda: panel.tareas(
        _get("/tareas"), creada=creada)).body.decode()
    return destino, html, conn


def test_la_tarea_escrita_aparece_en_la_pantalla_y_en_su_grupo():
    """LA PRUEBA QUE VALE POR TODAS: se manda el formulario y después se pinta
    /tareas contra la MISMA base, con lo que quedó guardado de verdad. Si la
    tarea no sale, o sale en otro grupo, se pone roja."""
    hoy = panel._hoy()
    destino, html, conn = _crear_y_pintar(
        {"titulo": "cambiar las cuerdas", "vence": hoy.isoformat()})

    assert destino == f"/tareas?creada={conn.tareas[0]['id']}", (
        f"no vuelve a la lista con el aviso: {destino}")
    assert "cambiar las cuerdas" in html, "la tarea escrita no se ve"
    assert "Hoy" in html, "no se pintó el grupo de hoy"
    # Y en el GRUPO correcto: el reparto lo hace `db.grupo_de_tarea`, no esta
    # prueba, así que se le pregunta a él con la fila que de verdad se guardó.
    assert db.grupo_de_tarea(conn.tareas[0]["estado"],
                             conn.tareas[0]["vence_en"], hoy) == "hoy", (
        "la tarea nació en el día equivocado")
    # Y NACE SIN RESPONSABLE, que es lo normal: una tarea recién escrita es
    # una tarea que nadie tomó todavía. Quién la anotó no se pinta desde el
    # 10-sep-2026; asignarle un responsable por el hecho de haberla escrito
    # sería confundir las dos preguntas otra vez.
    columnas = re.search(r"INSERT INTO tareas\s*\(([^)]*)\)",
                         inspect.getsource(db.crear_tarea_desde_el_panel))
    assert "responsable" not in columnas.group(1), (
        "el alta a mano le pone un responsable que nadie pidió: una tarea "
        "recién escrita es una tarea que nadie tomó todavía")
    assert 'name="resp_' in html, "no se puede asignar el responsable"


def test_la_tarea_sin_fecha_cae_en_su_grupo_con_nombre_propio():
    _, html, conn = _crear_y_pintar({"titulo": "buscar el micrófono"})
    assert conn.tareas[0]["vence_en"] is None
    assert "buscar el micrófono" in html
    assert "Sin fecha" in html, (
        "la tarea sin fecha no cayó en el grupo que existe para ella")


def test_el_aviso_de_creada_se_pinta_con_el_numero():
    """El éxito y el «no pasó nada» no pueden devolver la misma pantalla."""
    _, html, conn = _crear_y_pintar({"titulo": "afinar el piano"})
    assert f"#{conn.tareas[0]['id']}" in html, (
        "la pantalla no dice qué se creó")


# ── La pantalla del formulario ───────────────────────────────────────────

def _pintar_formulario(error: str = ""):
    bucle = asyncio.new_event_loop()
    try:
        r = bucle.run_until_complete(
            panel.tarea_nueva(_get("/tareas/nueva"), error=error))
        assert r.status_code == 200, f"/tareas/nueva devolvió {r.status_code}"
        return r.body.decode()
    finally:
        bucle.close()


def test_el_formulario_se_pinta_de_verdad():
    """No se mira el HTML con grep desde afuera: se llama a la ruta y se exige
    que la plantilla se RENDERICE. Una variable que la plantilla usa y la ruta
    no manda se cae acá — el fallo que dejó la portada en Internal Server Error
    con los tests en verde."""
    html = _pintar_formulario()
    assert 'action="/tareas/nueva"' in html, "el formulario no manda a ningún lado"
    assert 'name="titulo"' in html and "required" in html
    assert 'name="vence"' in html and 'type="date"' in html
    # El `min` del campo sale del MISMO valor que valida el servidor: una
    # pantalla que ofrece lo que la ruta rechaza es una trampa.
    assert f'min="{panel.PISO_FECHA.isoformat()}"' in html
    assert f'maxlength="{panel.LARGO_TITULO}"' in html


def test_los_rechazos_se_explican_con_palabras():
    """El servidor manda una clave por la URL; la pantalla la traduce. Una
    clave sin traducir es un error que no se entiende, y un error que no se
    entiende se repite."""
    # `class="aviso"` y no «aviso» a secas: la hoja de estilo de base.html trae
    # la palabra suelta, así que buscarla sin comillas da verde siempre.
    for clave in ("titulo", "fecha"):
        html = _pintar_formulario(clave)
        assert 'class="aviso"' in html, f"el rechazo por «{clave}» no se ve"
    # Y una clave inventada no pinta un aviso en blanco ni revienta.
    assert 'class="aviso"' not in _pintar_formulario("inventada")


def test_se_llega_al_formulario_desde_la_lista_incluso_vacia():
    """Una pantalla a la que no se llega no existe. Y el enlace tiene que verse
    CON LA LISTA VACÍA, que es justo cuando hace falta escribir la primera."""
    vacia = _con_base(_Conn(), lambda: panel.tareas(_get("/tareas")))
    html = vacia.body.decode()
    assert 'href="/tareas/nueva"' in html, (
        "con la lista vacía no hay forma de agregar la primera tarea")

    _, con_tareas, _ = _crear_y_pintar({"titulo": "algo"})
    assert 'href="/tareas/nueva"' in con_tareas


def test_la_lista_sigue_teniendo_un_solo_formulario():
    """El alta se agregó como un ENLACE y no como un segundo <form>. Con dos
    formularios, el de cerrar varias de una vez deja de tener sentido —cada
    envío recarga y se lleva puesto lo demás marcado— y un <form> dentro de
    otro es HTML inválido: el navegador descarta el de adentro y el botón deja
    de hacer nada, EN SILENCIO."""
    html = open(os.path.join(RAIZ, "web", "plantillas", "tareas.html"),
                encoding="utf-8").read()
    assert html.count("<form") == 1, "hay más de un formulario en /tareas"
    nuevo = open(os.path.join(RAIZ, "web", "plantillas", "tarea_nueva.html"),
                 encoding="utf-8").read()
    assert nuevo.count("<form") == 1


# ── Las columnas salen del esquema, no de la memoria de nadie ────────────

def test_el_alta_solo_toca_columnas_que_el_esquema_declara():
    """Las tablas y las columnas se sacan del CÓDIGO de la función y se
    comparan contra `db/schema.sql`. Ninguna de las dos listas se teclea acá:
    una lista escrita a mano y la realidad se separan desde el día que se
    escribe.

    Esto NO reemplaza a `tools/humo.py` —schema.sql describe la base, no ES la
    base— pero agarra el typo sin necesidad de DATABASE_URL.
    """
    fuente = inspect.getsource(db.crear_tarea_desde_el_panel)
    declaradas = db.columnas_declaradas()
    tablas = re.findall(r"INSERT INTO (\w+)\s*\(([^)]*)\)", fuente)
    assert len(tablas) == 3, (
        f"se esperaban tres INSERT (bandeja, tareas, log_acciones): {tablas}")
    for tabla, crudo in tablas:
        columnas = {c.strip() for c in crudo.split(",") if c.strip()}
        assert columnas, f"no se leyó ninguna columna del INSERT de {tabla}"
        faltan = columnas - set(declaradas.get(tabla, ()))
        assert not faltan, (
            f"{tabla}: columnas que el esquema no declara: {faltan}")


def test_el_alta_no_toca_ninguna_tabla_de_mas():
    """Tres tablas y ni una más: la tarea, la fila que dice quién la anotó, y
    la huella. Cualquier otra escritura que aparezca acá es un efecto que este
    encargo no pidió."""
    fuente = inspect.getsource(db.crear_tarea_desde_el_panel)
    tocadas = set(re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM) (\w+)", fuente))
    assert tocadas == {"bandeja", "tareas", "log_acciones"}, (
        f"toca tablas que no le tocan: {tocadas}")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
