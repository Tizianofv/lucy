# -*- coding: utf-8 -*-
"""«Code» como responsable de una tarea (26-sep-2026, diseño aprobado por
Tiziano: disenos/lucy-code/DISENO.md, §1 + §2 + §4).

Parte 1 del plan de construcción, y SOLO esa: `CHAT_ID_CODE`/`NOMBRE_CODE`,
la separación `puede_ser_responsable` / `puede_recibir_telegram`, la
resolución del nombre «Code» por Telegram, el desplegable del panel, y el
cierre `db.cerrar_tarea_de_la_sala`. NADA de puerta HTTP, «tomada», alarmas
técnicas ni dueños en `personas`/`preferencias` — eso son otras partes.

NINGÚN chat_id de este archivo es real (regla del repo: es PÚBLICO). `OTRA`
y `AJENO` son constantes inventadas, igual que en `tests/test_responsable.py`
y `tests/test_recordatorios_por_responsable.py`.

Herméticos: sin Postgres ni red — mismos stubs que el resto de la suite.

Correr:  python3 -m pytest tests/test_code_responsable.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sys
import textwrap
import types
from datetime import datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-code")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
os.environ.setdefault("DEEPSEEK_API_KEY", "")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object()
_psycopg.rows = _rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)

_pool_mod = types.ModuleType("psycopg_pool")


class _StubPool:
    def __init__(self, *a, **k):
        pass


_pool_mod.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _pool_mod)

_openai = types.ModuleType("openai")


class _StubAsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _StubAsyncOpenAI
sys.modules.setdefault("openai", _openai)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import acciones.crud as crud  # noqa: E402
import cerebro.agente as agente  # noqa: E402
import db.db as db  # noqa: E402
from cerebro import despertador  # noqa: E402

UTC = timezone.utc
DUENO = config.CHAT_ID_DUENO
OTRA = 700100001          # una persona real de la casa, INVENTADA
AJENO = 700100999         # no entra al panel, INVENTADO


def _con_gente(nombres, permitidos=None):
    """Mismo patrón que `tests/test_responsable.py::_con_gente`: se tocan
    los dos globales de `config` que las funciones leen EN CADA LLAMADA, no
    el entorno — así no importa qué otro archivo de la suite importó
    `config` primero. `conftest.py` no restaura nada de esto solo, así que
    cada prueba que lo use lo deja como lo encontró."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = tuple(
        permitidos if permitidos is not None else nombres)

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


# ═══════════════════════════════════════════════════════════════════════
# §1 — `CHAT_ID_CODE`, `NOMBRE_CODE`, y las dos puertas
# ═══════════════════════════════════════════════════════════════════════

def test_code_es_negativo_y_nunca_puede_chocar_con_un_chat_real():
    """Un chat PRIVADO de Telegram (una persona, nunca un grupo) es SIEMPRE
    positivo — es la premisa entera de reservar un negativo para Code. Si
    esto deja de ser cierto, todo el diseño de §1 se apoya en algo falso."""
    assert config.CHAT_ID_CODE < 0
    assert config.NOMBRE_CODE == "Code"


def test_code_puede_quedar_como_responsable_sin_estar_en_la_casa():
    """LA GARANTÍA CENTRAL de §1: Code pasa la puerta de asignación aunque
    NO esté en `personas_del_panel()` — nunca entra al panel, y no tiene por
    qué para poder QUEDAR como responsable."""
    restaurar = _con_gente({}, permitidos=())
    try:
        assert config.personas_del_panel() == (), (
            "la casa no está vacía: la prueba no comprueba lo que dice")
        assert config.puede_ser_responsable(config.CHAT_ID_CODE) is True
    finally:
        restaurar()


def test_un_chat_inventado_que_no_es_code_sigue_rechazado():
    """La excepción es UNA comparación (`== CHAT_ID_CODE`), no una puerta que
    se abrió de más: un negativo cualquiera que no sea Code sigue sin
    poder ser responsable."""
    restaurar = _con_gente({}, permitidos=())
    try:
        assert config.puede_ser_responsable(config.CHAT_ID_CODE - 1) is False
        assert config.puede_ser_responsable(-999) is False
    finally:
        restaurar()


def test_puede_recibir_telegram_excluye_a_code_pero_no_a_la_casa():
    """La SEGUNDA puerta: Code puede QUEDAR asignado (de arriba) pero NUNCA
    se le puede escribir por Telegram — no tiene chat. Para cualquier
    persona real de la casa, las dos puertas dan la MISMA respuesta."""
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        assert config.puede_recibir_telegram(config.CHAT_ID_CODE) is False
        assert config.puede_ser_responsable(config.CHAT_ID_CODE) is True, (
            "la MUTACIÓN de este archivo (ver tabla del reporte) rompe esta "
            "línea si alguien funde las dos puertas en una sola otra vez")
        assert config.puede_recibir_telegram(OTRA) is True
        assert config.puede_recibir_telegram(AJENO) is False
    finally:
        restaurar()


def test_nombres_con_code_agrega_sin_pisar_ni_mutar_el_original():
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        antes = dict(config.NOMBRES_POR_CHAT)
        salida = config.nombres_con_code()
        assert salida[config.CHAT_ID_CODE] == "Code"
        assert salida[OTRA] == "Mengano"
        assert config.NOMBRES_POR_CHAT == antes, (
            "nombres_con_code() mutó NOMBRES_POR_CHAT en vez de devolver uno "
            "nuevo")
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# §2a — Asignación por Telegram: `_chat_del_nombre("Code")`
# ═══════════════════════════════════════════════════════════════════════

def test_chat_del_nombre_resuelve_code_sin_importar_mayusculas_ni_espacios():
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        for variante in ("Code", "code", "CODE", "  Code  ", "cÓdE"):
            chat, motivo = crud._chat_del_nombre(variante)
            assert chat == config.CHAT_ID_CODE, (
                f"{variante!r} no resolvió a Code: motivo={motivo!r}")
    finally:
        restaurar()


def test_chat_del_nombre_no_confunde_codigo_con_code():
    """La clave se compara ENTERA, no por prefijo: "código" no es "code".
    Si esto se rompiera con un `.startswith`, cualquier palabra que empiece
    con las mismas letras resolvería a Code por accidente."""
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        for parecido in ("codigo", "código", "cod", "co"):
            chat, motivo = crud._chat_del_nombre(parecido)
            assert chat is None, f"{parecido!r} resolvió a un chat: {chat}"
    finally:
        restaurar()


def test_editar_por_telegram_acepta_code_como_responsable():
    """El camino completo de Telegram: `crud._responsable_que_vale("Code")`
    -- la puerta que usan `crear`/`editar` -- devuelve `CHAT_ID_CODE`."""
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        assert crud._responsable_que_vale("Code") == config.CHAT_ID_CODE
        assert crud._responsable_que_vale("code") == config.CHAT_ID_CODE
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# §2a — El prompt: Code es responsable válido de TAREA, nunca dueño de CITA
# ═══════════════════════════════════════════════════════════════════════

def _bloque(prompt: str, marca: str) -> str:
    i = prompt.index(marca)
    return prompt[i:prompt.index("\n\n", i)]


def test_el_prompt_ofrece_code_como_responsable_de_tarea():
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        prompt = agente.herramientas_del_prompt()
        assert "Code" in _bloque(prompt, "RESPONSABLE (solo tareas, opcional)")
        assert "Code" in _bloque(prompt, "RESPONSABLE DE UNA TAREA")
    finally:
        restaurar()


def test_el_prompt_NUNCA_ofrece_code_como_dueno_de_cita():
    """Code no atiende citas: la lista de dueños de cita es la MISMA
    `personas_del_panel()` de siempre, sin Code sumado — a propósito, para
    que nadie le arme una cita a la sala de control por error."""
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        prompt = agente.herramientas_del_prompt()
        assert "Code" not in _bloque(prompt, "DUEÑO(S) (solo citas, opcional)")
        assert "Code" not in _bloque(prompt, "DUEÑO(S) DE UNA CITA")
    finally:
        restaurar()


def test_el_prompt_ofrece_code_aunque_no_haya_ninguna_persona_de_la_casa():
    """A diferencia de las personas —que dependen de NOMBRES_POR_CHAT—, Code
    sigue disponible como responsable aunque esa variable esté vacía."""
    restaurar = _con_gente({}, permitidos=())
    try:
        prompt = agente.herramientas_del_prompt()
        assert "Code" in _bloque(prompt, "RESPONSABLE (solo tareas, opcional)")
    finally:
        restaurar()


def test_duenos_que_valen_sigue_rechazando_a_code_como_dueno_de_cita():
    """Aunque alguien intentara pasarle "Code" a `_duenos_que_valen`
    (compartida con `_responsable_que_vale`), esta prueba deja registrado
    que HOY la puerta de abajo SÍ lo aceptaría -- ver la nota de diseño en
    `cerebro/despertador.py` sobre por qué el filtro de ENVÍO
    (`puede_recibir_telegram`) es el que de verdad evita el problema, no
    esta puerta de asignación."""
    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        # Documentado, no deseado: la puerta de asignación es una sola para
        # tareas y citas (`_responsable_que_vale`), así que acepta a Code acá
        # también. Lo que impide el daño real -- mandarle un Telegram a un
        # chat que no existe -- es que `despertador.py` arma el destino de
        # avisos de cita con `puede_recibir_telegram`, no con esta puerta.
        assert crud._duenos_que_valen("Code") == [config.CHAT_ID_CODE]
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# §2b — El panel: el selector ofrece «Code» sin meterlo en `personas_del_panel`
# ═══════════════════════════════════════════════════════════════════════

def test_web_app_ofrece_code_aparte_de_personas_del_panel():
    """No se prueba HTML acá (eso ya lo cubre `tests/test_responsable.py`
    para el resto del desplegable) — se prueba que la RUTA le pasa a la
    plantilla lo que hace falta para pintar a Code aparte, sin ensuciar
    `personas`/`asignables` con el significado de "puede entrar al panel"."""
    import web.app as panel

    restaurar = _con_gente({OTRA: "Mengano"}, permitidos=(DUENO, OTRA))
    try:
        fuente = inspect.getsource(panel.tareas)
        assert '"chat_id_code": config.CHAT_ID_CODE' in fuente
        assert '"nombre_code": config.NOMBRE_CODE' in fuente
        assert 'config.nombres_con_code()' in fuente, (
            "la ruta sigue pintando 'nombres' desde NOMBRES_POR_CHAT a "
            "secas: una tarea de Code se vería 'sin nombre'")
        assert "config.CHAT_ID_CODE" in fuente.split('"asignables":')[1][:120], (
            "'asignables' no suma CHAT_ID_CODE: una tarea de Code se vería "
            "'ya no se le puede asignar'")
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# Hermanos — censo AST: NADA en `cerebro/despertador.py` decide un ENVÍO de
# Telegram preguntándole a la puerta de asignación (`puede_ser_responsable`)
# ═══════════════════════════════════════════════════════════════════════

def test_despertador_no_llama_a_puede_ser_responsable_ni_una_vez():
    """`cerebro/despertador.py` es el ÚNICO archivo de producción que arma un
    destino de Telegram a partir de un `responsable_chat_id`/`duenos_chat_id`
    guardado (medido con `grep` de `_avisar(`/`bot.send_message` sobre todo
    el repo el 26-sep-2026: los únicos otros usos responden al chat de la
    conversación en curso, no a un responsable guardado). Por eso el censo
    es sobre ESTE archivo, no sobre el repo entero -- lo mismo que
    `tests/test_responsable.py` acota su censo a quién ESCRIBE la columna.

    Se mira el ÁRBOL DE SINTAXIS, no el texto: un comentario que mencione
    `puede_ser_responsable` (y este archivo tiene varios, a propósito, para
    explicar por qué NO se usa) no puede hacer fallar ni salvar esta prueba.
    """
    fuente = inspect.getsource(despertador)
    arbol = ast.parse(fuente)
    llamadas_prohibidas = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        f = nodo.func
        nombre = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else None)
        if nombre == "puede_ser_responsable":
            llamadas_prohibidas.append(nodo.lineno)
    assert not llamadas_prohibidas, (
        f"cerebro/despertador.py llama a puede_ser_responsable en las líneas "
        f"{llamadas_prohibidas} -- tiene que ser puede_recibir_telegram para "
        "decidir un envío de Telegram, o Code recibiría uno")


def test_despertador_SI_llama_a_puede_recibir_telegram_al_menos_dos_veces():
    """Lo contrario de la prueba de arriba: que el censo no esté vacío
    porque la guarda no vigila nada. Los DOS sitios son el recordatorio
    puntual de una tarea/cita (`revisar`) y el reparto de
    briefing/semanal (`_destinatarios_de_tareas`)."""
    fuente = inspect.getsource(despertador)
    arbol = ast.parse(fuente)
    n = sum(
        1 for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "puede_recibir_telegram")
    assert n >= 2, (
        f"solo {n} llamada(s) a puede_recibir_telegram en despertador.py: "
        "el censo de arriba estaría verde sin haber vigilado nada")


# ═══════════════════════════════════════════════════════════════════════
# Comportamiento real de `despertador.revisar()`: una tarea de Code nunca
# le manda Telegram a -1; el recordatorio cae al dueño, como si no hubiera
# responsable válido.
# ═══════════════════════════════════════════════════════════════════════

class _Cur:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows

    async def execute(self, sql, params=None):
        return self


class _Transaccion:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class _FakeConn:
    def __init__(self, filas):
        self.sqls: list[tuple[str, tuple]] = []
        self._filas = filas
        self._servidas = False

    def _norm(self, sql):
        return " ".join(sql.split())

    async def execute(self, sql, params=None):
        self.sqls.append((self._norm(sql), params or ()))
        return _Cur()

    def cursor(self, row_factory=None):
        filas, self._servidas = ([] if self._servidas else self._filas), True
        return _CursorConFilas(self, filas)

    def transaction(self):
        return _Transaccion(self)


class _CursorConFilas(_Cur):
    def __init__(self, conn, filas):
        super().__init__(filas)
        self._conn = conn

    async def execute(self, sql, params=None):
        self._conn.sqls.append((self._conn._norm(sql), params or ()))
        return self


class _PoolCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _PoolCM(self._conn)


class _BotFalso:
    def __init__(self):
        self.enviados: list[dict] = []

    async def send_message(self, **kw):
        self.enviados.append(kw)


def _fila_tarea(id_, titulo, cuando, responsable=None):
    return {"tabla": "tareas", "id": id_, "titulo": titulo, "cuando": cuando,
            "avisos_enviados": [], "anticipos_min": [0],
            "responsable_chat_id": responsable}


def _instalar(conn):
    db.pool = _FakePool(conn)
    conn.avisos_registrados: list[tuple[int, str]] = []

    async def _registrar_aviso(chat_id, texto):
        conn.avisos_registrados.append((chat_id, texto))

    async def _cero():
        return 0

    async def _registrar(*a, **k):
        return None

    db.registrar_aviso = _registrar_aviso
    despertador._briefing = _cero
    despertador._semanal = _cero
    despertador._reprogramar_recurrentes = _cero
    crud._registrar = _registrar


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


def test_lucy_intenta_mandarle_telegram_a_code_la_prueba_se_pone_roja():
    """LA GARANTÍA PEDIDA EXPLÍCITAMENTE: si `despertador.revisar()` alguna
    vez intentara `bot.send_message(chat_id=CHAT_ID_CODE, ...)`, esta prueba
    lo agarra. Hoy NO pasa -- se demuestra corriendo `revisar()` de verdad,
    sin ningún doble que decida el resultado por su cuenta -- y por eso el
    recordatorio de una tarea técnica de Code cae al dueño, exactamente como
    si el responsable no fuera válido."""
    ahora = datetime.now(UTC)
    conn = _FakeConn([_fila_tarea(1, "Arreglar el parser de BHD", ahora,
                                  config.CHAT_ID_CODE)])
    _instalar(conn)
    bot = _BotFalso()
    avisos = _correr(despertador.revisar(bot))
    assert avisos == 1
    assert len(bot.enviados) == 1
    assert bot.enviados[0]["chat_id"] != config.CHAT_ID_CODE, (
        "¡Lucy le mandó Telegram a Code! chat_id=-1 no existe en Telegram")
    assert bot.enviados[0]["chat_id"] == config.CHAT_ID_DUENO, (
        f"el recordatorio de una tarea de Code tiene que caer al dueño, no "
        f"a {bot.enviados[0]['chat_id']}")


# ═══════════════════════════════════════════════════════════════════════
# §4 — `db.cerrar_tarea_de_la_sala`: la guarda vive en el SQL
# ═══════════════════════════════════════════════════════════════════════

def _extraer_consulta_elegible() -> str:
    """El texto REAL de `consulta_elegible`, sacado del árbol de sintaxis de
    `db.cerrar_tarea_de_la_sala` -- no copiado a mano (mismo patrón que
    `tests/test_primero.py::_extraer_sql`, para que un cambio futuro del SQL
    no deje esta prueba corriendo un texto viejo sin que nadie se entere)."""
    arbol = ast.parse(textwrap.dedent(inspect.getsource(db.cerrar_tarea_de_la_sala)))
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Assign) and len(nodo.targets) == 1
                and isinstance(nodo.targets[0], ast.Name)
                and nodo.targets[0].id == "consulta_elegible"
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)):
            return nodo.value.value
    raise AssertionError("no se encontró 'consulta_elegible' como string")


def _sqlite_con_tareas(filas: list[dict]):
    """Una base sqlite en memoria con el mismo shape que `tareas`/`proyectos`
    (subconjunto de columnas -- las que la consulta real nombra), sembrada
    con `filas`. Cada fila: id, estado, responsable_chat_id, area,
    proyecto_id, area_proyecto, borrado_en."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE tareas (
          id INTEGER PRIMARY KEY, titulo TEXT, estado TEXT, vence_en TEXT,
          completado_en TEXT, bandeja_id INTEGER, responsable_chat_id INTEGER,
          area TEXT, proyecto_id INTEGER, borrado_en TEXT
        )""")
    conn.execute("""
        CREATE TABLE proyectos (id INTEGER PRIMARY KEY, area TEXT)""")
    conn.execute("INSERT INTO proyectos (id, area) VALUES (900, 'CDS')")
    conn.execute(
        "INSERT INTO proyectos (id, area) VALUES (901, ?)", (db.AREA_TECNICA,))
    for f in filas:
        conn.execute(
            "INSERT INTO tareas (id, titulo, estado, vence_en, completado_en, "
            "bandeja_id, responsable_chat_id, area, proyecto_id, borrado_en) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
            (f["id"], f.get("titulo", f"tarea {f['id']}"), f["estado"],
             f.get("bandeja_id", 900 + f["id"]), f.get("responsable_chat_id"),
             f.get("area"), f.get("proyecto_id"), f.get("borrado_en")))
    conn.commit()
    return conn


def _corre_consulta_elegible(conn, tarea_id: int) -> dict | None:
    sql = _extraer_consulta_elegible().replace("%s", "?")
    cur = conn.execute(sql, (tarea_id, db.ESTADO_HECHA, db.CHAT_ID_CODE,
                             db.AREA_TECNICA))
    fila = cur.fetchone()
    return dict(fila) if fila else None


def test_la_consulta_elegible_trae_una_tarea_tecnica_de_code_pendiente():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA}])
    assert _corre_consulta_elegible(conn, 1) is not None


def test_la_consulta_elegible_NO_trae_una_tarea_que_no_es_de_code():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: cerrar una tarea que no es de Code no
    toca nada -- corrida contra SQL real, no un doble."""
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": None,
         "area": db.AREA_TECNICA},
        {"id": 2, "estado": "pendiente", "responsable_chat_id": 700200002,
         "area": db.AREA_TECNICA}])
    assert _corre_consulta_elegible(conn, 1) is None, (
        "trajo una tarea SIN responsable como si fuera de Code")
    assert _corre_consulta_elegible(conn, 2) is None, (
        "trajo una tarea de OTRA persona como si fuera de Code")


def test_la_consulta_elegible_NO_trae_una_tarea_tecnica_de_code_ya_hecha():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "hecha", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA}])
    assert _corre_consulta_elegible(conn, 1) is None


def test_la_consulta_elegible_NO_trae_una_tarea_de_code_de_otra_area():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": "CDS"}])
    assert _corre_consulta_elegible(conn, 1) is None


def test_la_consulta_elegible_hereda_el_area_del_proyecto():
    """Una tarea de Code SIN área propia pero con proyecto Técnico SÍ cuenta
    -- `COALESCE(t.area, p.area)`, el mismo criterio que ya usa el panel."""
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": None, "proyecto_id": 901}])
    assert _corre_consulta_elegible(conn, 1) is not None


def test_la_consulta_elegible_NO_trae_una_tarea_borrada():
    conn = _sqlite_con_tareas([
        {"id": 1, "estado": "pendiente", "responsable_chat_id": db.CHAT_ID_CODE,
         "area": db.AREA_TECNICA, "borrado_en": "2026-09-01"}])
    assert _corre_consulta_elegible(conn, 1) is None


# ── El comportamiento de la FUNCIÓN completa (guarda + escritura + rastro) ──

class _CurCierre:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if "SELECT t.id, t.titulo, t.estado" in s:
            elegible = self._conn.elegible
            self._filas = [elegible] if elegible is not None else []
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnCierre:
    """`elegible=None` simula que `consulta_elegible` no trajo nada -- la
    prueba no reimplementa el SQL, solo controla si la fila "existe" del
    lado de la base para poder ver qué hace la función CON esa respuesta.
    El SQL de verdad ya se comprobó arriba, contra sqlite."""
    def __init__(self, elegible):
        self.elegible = elegible
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CurCierre(self)

    async def execute(self, sql, params=None):
        return await _CurCierre(self).execute(sql, params)


class _PoolCierre:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        class _CM:
            async def __aenter__(s):
                return self._conn

            async def __aexit__(s, *e):
                return False
        return _CM()


def test_cerrar_tarea_de_la_sala_no_toca_nada_si_no_es_elegible():
    conn = _ConnCierre(elegible=None)
    guardado = db.pool
    db.pool = _PoolCierre(conn)
    try:
        ok = _correr(db.cerrar_tarea_de_la_sala(999))
    finally:
        db.pool = guardado
    assert ok is False
    escribio = any(
        s.upper().startswith("UPDATE") or s.upper().startswith("INSERT")
        for s, _ in conn.sql)
    assert not escribio, "escribió algo sin que la consulta trajera nada"


def test_cerrar_tarea_de_la_sala_cierra_y_deja_rastro_de_sala():
    fila = {"id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
            "vence_en": None, "completado_en": None, "bandeja_id": 905,
            "responsable_chat_id": config.CHAT_ID_CODE,
            "area_efectiva": db.AREA_TECNICA}
    conn = _ConnCierre(elegible=fila)
    guardado = db.pool
    db.pool = _PoolCierre(conn)
    try:
        ok = _correr(db.cerrar_tarea_de_la_sala(5))
    finally:
        db.pool = guardado
    assert ok is True
    updates = [(s, p) for s, p in conn.sql if s.upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert updates[0][1] == (db.ESTADO_HECHA, 5)
    inserts = [(s, p) for s, p in conn.sql
              if s.upper().startswith("INSERT INTO LOG_ACCIONES")]
    assert len(inserts) == 1
    sql_log, params_log = inserts[0]
    assert "'sala'" in sql_log, (
        "el actor no está fijo en 'sala' -- ver la garantía del rastro")
    assert "Code" in sql_log, "el motivo no nombra a Code"
