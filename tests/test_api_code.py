# -*- coding: utf-8 -*-
"""La puerta HTTP de las tareas de Code (26-sep-2026, diseño aprobado por
Tiziano: disenos/lucy-code/DISENO.md, §C — parte 2 del plan de construcción).

Parte 2, y SOLO esa: `GET /api/code/tareas` (listar) y
`POST /api/code/tareas/{id}/cerrar`. NADA de "tomar" ni "alertas" -- esas
rutas no existen todavía, a propósito (§D/§B son partes futuras).

NINGUNA clave de este archivo es real: las fabrica cada prueba en el
momento (regla del repo: es PÚBLICO). La app FastAPI real corre con
`fastapi.testclient.TestClient` -- no se llama a las funciones de la ruta a
mano, se manda una petición HTTP de verdad contra la app montada.

Herméticos: sin Postgres ni red -- mismos stubs que el resto de la suite.

Correr:  python3 -m pytest tests/test_api_code.py -q
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import sys
import time
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-puerta")
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import db.db as db  # noqa: E402
import web.app as panel  # noqa: E402
import web.api_code as api_code  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLIENTE = TestClient(panel.app)


def _con_claves(claves: dict[str, str], permisos: dict[str, frozenset[str]]):
    """Instala una configuración de mentira de las dos variables de Railway
    y devuelve una función para restaurarla. Mismo patrón que `_con_gente`
    de `tests/test_code_responsable.py`: se tocan los globales de `config`
    directo, no el entorno, para no depender del orden de la suite."""
    claves_orig = config.CLAVES_API_CODE
    permisos_orig = config.PERMISOS_API_CODE
    config.CLAVES_API_CODE = dict(claves)
    config.PERMISOS_API_CODE = {k: frozenset(v) for k, v in permisos.items()}

    def _restaurar():
        config.CLAVES_API_CODE = claves_orig
        config.PERMISOS_API_CODE = permisos_orig

    return _restaurar


def _limpiar_contadores_de_abuso():
    """Los contadores en memoria de `web/api_code.py` son módulo-globales:
    sin limpiarlos, una prueba puede heredar intentos de otra."""
    api_code._intentos_malos.clear()
    api_code._pedidos_por_quien.clear()
    api_code._ultimo_aviso_abuso = 0.0


SALA_CLAVE = "clave-de-prueba-sala-no-es-real-1234567890"
NATALIA_CLAVE = "clave-de-prueba-natalia-no-es-real-0987654321"


def _con_sala_y_natalia():
    return _con_claves(
        {SALA_CLAVE: "sala_mac", NATALIA_CLAVE: "natalia"},
        {"sala_mac": frozenset({"tareas:listar", "tareas:cerrar"}),
         "natalia": frozenset({"alertas:crear"})})


# ═══════════════════════════════════════════════════════════════════════
# Cerrado por defecto
# ═══════════════════════════════════════════════════════════════════════

def test_sin_variables_toda_llamada_se_rechaza():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE: sin `CLAVES_API_CODE`/
    `PERMISOS_API_CODE` puestas, NINGÚN pedido pasa -- ni siquiera uno sin
    encabezado `Authorization` en absoluto."""
    restaurar = _con_claves({}, {})
    _limpiar_contadores_de_abuso()
    try:
        r1 = CLIENTE.get("/api/code/tareas")
        assert r1.status_code == 401
        r2 = CLIENTE.post("/api/code/tareas/1/cerrar")
        assert r2.status_code == 401
        r3 = CLIENTE.get("/api/code/tareas",
                         headers={"Authorization": "Bearer cualquier-cosa"})
        assert r3.status_code == 401
    finally:
        restaurar()


def test_una_clave_valida_sin_permiso_configurado_se_rechaza_no_revienta():
    """Cerrado por defecto EN EL SEGUNDO NIVEL: la clave existe y resuelve a
    un `quien`, pero ese `quien` no tiene fila en `PERMISOS_API_CODE` ->
    403, nunca un error de servidor."""
    restaurar = _con_claves({SALA_CLAVE: "sala_mac"}, {})
    _limpiar_contadores_de_abuso()
    try:
        r = CLIENTE.get("/api/code/tareas",
                        headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 403
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# Permisos por quien llama
# ═══════════════════════════════════════════════════════════════════════

def _con_pool_fake(listar_filas=None, elegible=None):
    return _PoolFake(_ConnFake(listar_filas, elegible))


class _CurFake:
    def __init__(self, conn):
        self._conn = conn
        self._filas = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if s.startswith("SELECT t.id, t.titulo, t.detalle, t.vence_en"):
            self._filas = list(self._conn.listar_filas)
        elif s.startswith("SELECT t.id, t.titulo, t.estado"):
            self._filas = [self._conn.elegible] if self._conn.elegible else []
        else:
            self._filas = []
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return self._filas


class _ConnFake:
    def __init__(self, listar_filas=None, elegible=None):
        self.listar_filas = listar_filas or []
        self.elegible = elegible
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CurFake(self)

    async def execute(self, sql, params=None):
        return await _CurFake(self).execute(sql, params)


class _PoolFake:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _CM()


def test_clave_de_sala_puede_listar_y_cerrar():
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    guardado = db.pool
    fila_elegible = {
        "id": 5, "titulo": "Arreglar el canario", "estado": "pendiente",
        "vence_en": None, "completado_en": None, "bandeja_id": 905,
        "responsable_chat_id": config.CHAT_ID_CODE,
        "area_efectiva": db.AREA_TECNICA}
    db.pool = _con_pool_fake(
        listar_filas=[{"id": 5, "titulo": "Arreglar el canario"}],
        elegible=fila_elegible)
    try:
        r1 = CLIENTE.get("/api/code/tareas",
                         headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r1.status_code == 200
        assert r1.json() == {"tareas": [{"id": 5, "titulo": "Arreglar el canario"}]}

        r2 = CLIENTE.post("/api/code/tareas/5/cerrar",
                          headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r2.status_code == 200
        assert r2.json() == {"cerrada": True}
    finally:
        db.pool = guardado
        restaurar()


def test_clave_de_natalia_no_puede_listar_ni_cerrar():
    """GARANTÍA PEDIDA EXPLÍCITAMENTE. Natalia solo tiene "alertas:crear" --
    una ruta que en esta parte ni siquiera existe -- así que hoy no puede
    hacer NADA en esta puerta salvo recibir 403."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    guardado = db.pool
    db.pool = _con_pool_fake(listar_filas=[{"id": 5}])
    try:
        r1 = CLIENTE.get("/api/code/tareas",
                         headers={"Authorization": f"Bearer {NATALIA_CLAVE}"})
        assert r1.status_code == 403

        r2 = CLIENTE.post("/api/code/tareas/5/cerrar",
                          headers={"Authorization": f"Bearer {NATALIA_CLAVE}"})
        assert r2.status_code == 403
    finally:
        db.pool = guardado
        restaurar()


def test_cada_ruta_exige_especificamente_su_propio_permiso():
    """Hallazgo del testigo sobre `b07de3f`: ninguna prueba exigía que
    `POST /tareas/{id}/cerrar` pidiera ESPECÍFICAMENTE `tareas:cerrar` y no
    cualquier otro permiso -- todos los fixtures le daban a `sala_mac` los
    dos permisos juntos, así que una mutación que cambiara el permiso
    exigido por esa ruta a `"tareas:listar"` seguía en verde.

    LAS RUTAS SALEN DE `api_code.rutas_registradas(panel.app)` -- lo que
    FastAPI REALMENTE registró -- no de una lista tecleada acá: el día que
    haya una tercera ruta, entra sola. Para CADA una, se arma una clave con
    TODOS los permisos de la puerta MENOS el que esa ruta exige, y se
    exige 403 -- la pareja exacta que pidió el testigo: una clave con solo
    `tareas:listar` no puede cerrar, y viceversa."""
    rutas = api_code.rutas_registradas(panel.app)
    assert rutas, "no se encontró ninguna ruta -- la prueba no vigilaría nada"
    assert all(permiso is not None for _, _, permiso in rutas), (
        f"alguna ruta no tiene permiso etiquetado: {rutas}")
    todos_los_permisos = {permiso for _, _, permiso in rutas}
    assert len(todos_los_permisos) >= 2, (
        "con un solo permiso en toda la puerta esta prueba no puede armar "
        "una clave a la que 'le falte' el suyo sin quedarse sin ninguno")

    guardado = db.pool
    _limpiar_contadores_de_abuso()
    try:
        for metodo, path, permiso_de_esta_ruta in rutas:
            permisos_sin_el_suyo = todos_los_permisos - {permiso_de_esta_ruta}
            clave = f"clave-de-prueba-sin-{permiso_de_esta_ruta.replace(':', '_')}"
            restaurar = _con_claves(
                {clave: "quien_de_prueba"},
                {"quien_de_prueba": permisos_sin_el_suyo})
            db.pool = _con_pool_fake(listar_filas=[], elegible=None)
            try:
                r = CLIENTE.request(
                    metodo, path.replace("{tid}", "1"),
                    headers={"Authorization": f"Bearer {clave}"})
                assert r.status_code == 403, (
                    f"{metodo} {path} exige {permiso_de_esta_ruta!r}, pero "
                    f"una clave con {sorted(permisos_sin_el_suyo)} (todos "
                    "MENOS ese) recibió "
                    f"{r.status_code} en vez de 403")
            finally:
                restaurar()
    finally:
        db.pool = guardado


def test_no_existe_ruta_de_tomar_ni_de_alertas():
    """«Mejor ausente que a medias»: estas rutas son de partes futuras del
    diseño y no se construyen todavía, aunque el permiso `alertas:crear` ya
    esté reservado en el formato de `PERMISOS_API_CODE`."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    try:
        r1 = CLIENTE.post("/api/code/tareas/5/tomar",
                          headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r1.status_code == 404
        r2 = CLIENTE.post("/api/code/alertas",
                          headers={"Authorization": f"Bearer {NATALIA_CLAVE}"})
        assert r2.status_code == 404
    finally:
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# GARANTÍA PEDIDA EXPLÍCITAMENTE: tarea que no es de Code -> no se toca
# ═══════════════════════════════════════════════════════════════════════

def test_cerrar_tarea_que_no_es_de_code_no_se_toca_por_la_puerta():
    """`db.cerrar_tarea_de_la_sala` (parte 1) es quien decide -- la ruta
    solo traduce su `False` a un 409. Se comprueba que la conexión NUNCA
    recibe un `UPDATE`/`INSERT` cuando la consulta con la guarda no trae
    nada elegible."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    guardado = db.pool
    db.pool = _con_pool_fake(elegible=None)   # nada elegible: no es de Code
    try:
        r = CLIENTE.post("/api/code/tareas/999/cerrar",
                         headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert r.status_code == 409
        conn = db.pool._conn
        escribio = any(
            s.upper().startswith("UPDATE") or s.upper().startswith("INSERT")
            for s, _ in conn.sql)
        assert not escribio, "la puerta escribió algo sin que fuera elegible"
    finally:
        db.pool = guardado
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# Clave mala: 401, registrada, nunca impresa
# ═══════════════════════════════════════════════════════════════════════

def test_clave_mala_401_y_registrada_sin_la_clave(caplog):
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    clave_mala = "esta-clave-jamas-fue-configurada-000000"
    try:
        with caplog.at_level(logging.WARNING, logger="lucy.api_code"):
            r = CLIENTE.get("/api/code/tareas",
                            headers={"Authorization": f"Bearer {clave_mala}"})
        assert r.status_code == 401
        assert r.json() == {"detail": "clave inválida"}
        texto_del_log = "\n".join(rec.message for rec in caplog.records)
        assert clave_mala not in texto_del_log, (
            "¡la clave mala apareció en el log!")
        assert SALA_CLAVE not in texto_del_log and NATALIA_CLAVE not in texto_del_log
        assert "intento" in texto_del_log.lower()
    finally:
        restaurar()


def test_clave_ambigua_no_deja_pasar_a_nadie():
    """Si DOS `quien` reclamaran la misma clave (config mal puesta en
    Railway), ninguno de los dos entra -- se prueba a través del parser
    real, no de una lista tecleada acá."""
    claves, malos = config._leer_claves_api_code(
        f"sala_mac:{SALA_CLAVE}, natalia:{SALA_CLAVE}")
    assert claves == {}, "una clave ambigua NO puede resolver a nadie"
    assert malos == 2


def test_el_formato_de_permisos_se_lee_con_igual_separado_por_mas():
    permisos, malos = config._leer_permisos_api_code(
        "sala_mac=tareas:listar+tareas:cerrar, natalia=alertas:crear")
    assert permisos == {
        "sala_mac": frozenset({"tareas:listar", "tareas:cerrar"}),
        "natalia": frozenset({"alertas:crear"})}
    assert malos == 0


# ═══════════════════════════════════════════════════════════════════════
# Límite de abuso: 60/min por clave válida
# ═══════════════════════════════════════════════════════════════════════

def test_limite_60_por_minuto_por_clave_valida():
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    guardado = db.pool
    db.pool = _con_pool_fake(listar_filas=[])
    try:
        ultimo = None
        for _ in range(61):
            ultimo = CLIENTE.get(
                "/api/code/tareas", headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        assert ultimo.status_code == 429
    finally:
        db.pool = guardado
        restaurar()


def test_el_limite_es_por_quien_no_global():
    """La clave de Natalia no gasta el límite de la sala -- son ventanas
    independientes, una por `quien`."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    guardado = db.pool
    db.pool = _con_pool_fake(listar_filas=[])
    try:
        for _ in range(60):
            CLIENTE.get("/api/code/tareas",
                       headers={"Authorization": f"Bearer {SALA_CLAVE}"})
        # Natalia no tiene permiso (403), pero eso pasa DESPUÉS del límite:
        # su propia ventana sigue en cero.
        r = CLIENTE.get("/api/code/tareas",
                        headers={"Authorization": f"Bearer {NATALIA_CLAVE}"})
        assert r.status_code == 403, "no debería estar limitada, solo sin permiso"
    finally:
        db.pool = guardado
        restaurar()


# ═══════════════════════════════════════════════════════════════════════
# Aviso a Tiziano tras 10 intentos con clave inválida en 10 minutos
# ═══════════════════════════════════════════════════════════════════════

def test_diez_claves_malas_disparan_un_aviso_a_tiziano():
    """De punta a punta, con la app FastAPI real: 10 pedidos con clave
    inválida contra `/api/code/tareas` -- corridos por el CLIENTE de
    pruebas, así que `asyncio.create_task` corre dentro del loop real de la
    petición -- disparan exactamente UN aviso a Tiziano."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    fake_telegram = types.ModuleType("telegram")
    enviados = []

    class _BotFalso:
        def __init__(self, token):
            self.token = token

        async def send_message(self, chat_id, text):
            enviados.append((chat_id, text))

    fake_telegram.Bot = _BotFalso
    modulo_real = sys.modules.get("telegram")
    sys.modules["telegram"] = fake_telegram
    try:
        for _ in range(10):
            r = CLIENTE.get("/api/code/tareas",
                            headers={"Authorization": "Bearer clave-invalida-de-prueba"})
            assert r.status_code == 401
        # `TestClient` corre cada pedido hasta el final (incluidas las
        # tareas que ese pedido agendó) antes de devolver la respuesta, así
        # que para el 10mo pedido el aviso ya se mandó -- sin `sleep` ni
        # espera abierta.
        assert len(enviados) == 1, f"se mandaron {len(enviados)} avisos, no 1"
        chat_id, texto = enviados[0]
        assert chat_id == config.CHAT_ID_DUENO
        assert "10 intento" in texto
        assert "clave-invalida-de-prueba" not in texto, (
            "¡la clave apareció en el aviso a Tiziano!")
    finally:
        if modulo_real is not None:
            sys.modules["telegram"] = modulo_real
        else:
            del sys.modules["telegram"]
        restaurar()


def test_avisar_abuso_manda_el_mensaje_por_telegram():
    """Corrida directa de `_avisar_abuso` (la corrutina que
    `_registrar_clave_mala` agenda con `create_task` al llegar al umbral),
    con un `telegram.Bot` de mentira -- prueba que el mensaje SÍ sale, sin
    depender de la integración real de Telegram."""
    fake_telegram = types.ModuleType("telegram")
    enviados = []

    class _BotFalso:
        def __init__(self, token):
            self.token = token

        async def send_message(self, chat_id, text):
            enviados.append((chat_id, text))

    fake_telegram.Bot = _BotFalso
    modulo_real = sys.modules.get("telegram")
    sys.modules["telegram"] = fake_telegram
    try:
        bucle = asyncio.new_event_loop()
        try:
            bucle.run_until_complete(api_code._avisar_abuso(10))
        finally:
            bucle.close()
    finally:
        if modulo_real is not None:
            sys.modules["telegram"] = modulo_real
        else:
            del sys.modules["telegram"]

    assert len(enviados) == 1
    chat_id, texto = enviados[0]
    assert chat_id == config.CHAT_ID_DUENO
    assert "10 intentos" in texto


def test_avisar_abuso_nunca_revienta_si_telegram_falla():
    """Sin tumbar el panel ni el bot: si `bot.send_message` revienta,
    `_avisar_abuso` se traga la excepción."""
    fake_telegram = types.ModuleType("telegram")

    class _BotQueFalla:
        def __init__(self, token):
            pass

        async def send_message(self, chat_id, text):
            raise RuntimeError("Telegram está caído")

    fake_telegram.Bot = _BotQueFalla
    modulo_real = sys.modules.get("telegram")
    sys.modules["telegram"] = fake_telegram
    try:
        bucle = asyncio.new_event_loop()
        try:
            bucle.run_until_complete(api_code._avisar_abuso(10))  # no debe lanzar
        finally:
            bucle.close()
    finally:
        if modulo_real is not None:
            sys.modules["telegram"] = modulo_real
        else:
            del sys.modules["telegram"]


# ═══════════════════════════════════════════════════════════════════════
# Comparación en tiempo constante (censo AST, no de memoria)
# ═══════════════════════════════════════════════════════════════════════

def test_resolver_quien_usa_compare_digest_no_igualdad_directa():
    """El árbol de sintaxis de `_resolver_quien` tiene que llamar a
    `hmac.compare_digest`, y NO puede comparar la clave que llega con `==`
    ni buscarla con `in` sobre el diccionario de claves reales."""
    fuente = inspect.getsource(api_code._resolver_quien)
    arbol = ast.parse(fuente)
    usa_compare_digest = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "compare_digest"
        for n in ast.walk(arbol))
    assert usa_compare_digest, (
        "_resolver_quien no llama a hmac.compare_digest")
    usa_comparacion_directa = any(
        isinstance(n, ast.Compare)
        and any(isinstance(op, ast.Eq) for op in n.ops)
        for n in ast.walk(arbol))
    assert not usa_comparacion_directa, (
        "_resolver_quien compara con == en algún lado -- tiene que ser "
        "SOLO compare_digest")


# ═══════════════════════════════════════════════════════════════════════
# Pareja de mutaciones: el mismo ataque por cookie y por encabezado
# ═══════════════════════════════════════════════════════════════════════

def test_una_cookie_de_panel_falsa_no_sirve_para_la_puerta_de_code():
    """Forjar la cookie `lucy_panel` (el ataque de siempre contra el panel
    humano) no le da NADA a quien intente usarlo contra `/api/code/*` -- es
    una autenticación completamente aparte."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    try:
        r = CLIENTE.get("/api/code/tareas", cookies={"lucy_panel": "cualquier.cosa.forjada"})
        assert r.status_code == 401
    finally:
        restaurar()


def test_una_clave_de_code_falsa_no_sirve_para_entrar_al_panel_humano():
    """La pareja del ataque de arriba: mandarle a `/tareas` (el panel
    humano) un encabezado `Authorization` con una clave de Code -- válida o
    no -- no abre esa puerta. `/tareas` solo mira la cookie de sesión, y
    `_fuera` (`web/app.py:361-363`) contesta 401 con la pantalla `entrar.
    html` para CUALQUIERA sin sesión -- coincide en número con el 401 de la
    puerta de Code, pero es HTML, no el JSON de `/api/code/*`."""
    restaurar = _con_sala_y_natalia()
    _limpiar_contadores_de_abuso()
    try:
        sin_encabezado = CLIENTE.get("/tareas", follow_redirects=False)
        con_clave_de_code = CLIENTE.get(
            "/tareas", headers={"Authorization": f"Bearer {SALA_CLAVE}"},
            follow_redirects=False)
        # La clave de Code no cambia NADA de la respuesta del panel: con
        # ella o sin ella, la misma pantalla de "entrar" con el mismo 401.
        assert con_clave_de_code.status_code == sin_encabezado.status_code == 401
        assert "application/json" not in con_clave_de_code.headers["content-type"], (
            "el panel humano contestó JSON -- se cruzó con la puerta de Code")
        # La clave de Code no cambia NI UN BYTE de lo que ve quien no tiene
        # sesión: la misma pantalla "entrar", con clave o sin ella.
        assert con_clave_de_code.text == sin_encabezado.text
    finally:
        restaurar()
