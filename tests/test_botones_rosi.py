# -*- coding: utf-8 -*-
"""Los botones de Lucy, también para Rosi (encargo 8, 22-sep-2026).

Decisión de Tiziano, textual: «Sí, que le funcionen». Hasta este encargo,
`acciones/botones.py::al_pulsar` cortaba con `!= config.CHAT_ID_DUENO` --
Rosi apretaba y no pasaba nada, aunque ya pudiera entrar al panel y
escribirle a Lucy por su cuenta.

Herméticos: se stubea `psycopg` antes de importar, igual que el resto de la
suite. Se corre `acciones.botones.al_pulsar` DE VERDAD -- lo único de
mentira es lo que está fuera del proceso: `crud`/`db` (espiados, no la
base) y el objeto `CallbackQuery` de Telegram.

Correr:  python3 -m pytest tests/test_botones_rosi.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

_psycopg = types.ModuleType("psycopg")
_psycopg_rows = types.ModuleType("psycopg.rows")
_psycopg_rows.dict_row = object()
_psycopg.rows = _psycopg_rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _psycopg_rows)

_psycopg_pool = types.ModuleType("psycopg_pool")


class _StubPool:
    def __init__(self, *a, **k):
        pass


_psycopg_pool.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _psycopg_pool)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import acciones.botones as botones  # noqa: E402
import acciones.crud as crud  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
ROSI = 555
AJENO = 999999  # ni el dueño, ni Rosi: nadie de la casa


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


def _con_gente(nombres: dict[int, str]):
    """`config.CHAT_IDS_PERMITIDOS` y `NOMBRES_POR_CHAT` de mentira, para
    esta prueba. Devuelve una función que restaura los originales -- se
    llama SIEMPRE en un `finally`, como el resto de esta suite (no hay
    fixture autouse en este repo que lo haga por su cuenta: se comprobó
    con `grep -rn autouse tests/*.py conftest.py`, cero resultados)."""
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = tuple(nombres)
    config.NOMBRES_POR_CHAT = dict(nombres)

    def _restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    return _restaurar


class _Q:
    """Un `CallbackQuery` de mentira: lo justo que `al_pulsar` toca."""

    def __init__(self, data: str, chat_id: int):
        self.data = data
        self.message = types.SimpleNamespace(
            chat_id=chat_id, text_html="La tarjeta.", reply_text=None)
        self.respuestas: list = []
        self.editado: dict = {}
        self._reply_markup_enviado = None

    async def answer(self, *a, **k):
        self.respuestas.append((a, k))

    async def edit_message_text(self, text, **kw):
        self.editado["texto"] = text

    async def _reply_text(self, texto, **kw):
        self._reply_markup_enviado = kw.get("reply_markup")
        return types.SimpleNamespace(message_id=1)


def _update(q: _Q):
    q.message.reply_text = q._reply_text
    return types.SimpleNamespace(callback_query=q)


class _Espia:
    """Reemplaza TODAS las funciones de `crud`/`db` que `al_pulsar` puede
    llamar, y anota cada llamada. Sirve para comprobar, para cualquier
    `accion`, que un chat sin acceso no dispara NINGUNA."""

    def __init__(self):
        self.llamadas: list[tuple[str, tuple]] = []

    def instalar(self):
        guardados = {}
        for nombre, mod in (
                ("deshacer", crud), ("deshacer_varias", crud),
                ("borrar", crud), ("editar", crud),
                ("crear_desde_interpretacion", crud),
                ("cambiar_estado", db), ("obtener", db)):
            guardados[(nombre, mod)] = getattr(mod, nombre)

            async def _espia(*a, _nombre=nombre, **k):
                self.llamadas.append((_nombre, a))
                if _nombre == "cambiar_estado":
                    return True  # "sí, reclamé la fila" -- para no frenar antes de tiempo
                if _nombre == "obtener":
                    return {"id": a[0] if a else None, "chat_id": DUENO,
                            "interpretacion": {"hecho": [1, 2]}}
                if _nombre in ("crear_desde_interpretacion",):
                    return ("tareas", 1, 100)
                if _nombre in ("editar",):
                    return ({"id": 1}, 100)
                if _nombre == "deshacer_varias":
                    return (0, [])
                return 100

            setattr(mod, nombre, _espia)
        self._guardados = guardados
        return self

    def restaurar(self):
        for (nombre, mod), fn in self._guardados.items():
            setattr(mod, nombre, fn)


def _acciones_reales() -> list[str]:
    """Los valores de `accion` que `al_pulsar` distingue de verdad, sacados
    del árbol de sintaxis -- no tecleados acá. Recorre los `if accion ==
    "..."`, el `if accion == "und"`/`"undt"` sueltos, y el
    `if accion not in ("ok", "alt")` que cierra la lista."""
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(botones.al_pulsar)))
    vistas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Compare):
            continue
        if not (isinstance(nodo.left, ast.Name) and nodo.left.id == "accion"):
            continue
        for op, comp in zip(nodo.ops, nodo.comparators):
            if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                if comp.value not in vistas:
                    vistas.append(comp.value)
            elif isinstance(op, ast.NotIn) and isinstance(comp, ast.Tuple):
                for el in comp.elts:
                    if isinstance(el, ast.Constant) and el.value not in vistas:
                        vistas.append(el.value)
    return vistas


# ═══════════════════════════════════════════════════════════════════════
# 1) Los botones que existen de verdad, y la lista de acceso.
# ═══════════════════════════════════════════════════════════════════════

def test_la_lista_de_botones_sale_del_codigo_real():
    """Documenta, midiéndolo, cuáles son los botones que existen HOY --
    para que el encargo (y este archivo) hablen de los mismos seis:
    "ok" (✅ Dale), "alt" (mejor la otra clasificación), "no" (🗑
    Descartar), "acc" (aplicar una orden sobre un candidato), "und"
    (deshacer UNA acción), "undt" (deshacer TODAS las de un mensaje).
    "Marcar hecha" y "posponer" -- que el encargo nombra -- NO son botones
    de Telegram en este código: son acciones del PANEL
    (`db.marcar_tarea_hecha`, `db.mover_vence`), sin manejador acá."""
    assert set(_acciones_reales()) == {"ok", "alt", "no", "acc", "und", "undt"}


def test_todo_manejador_de_boton_pasa_por_la_misma_puerta():
    """LA ÚNICA PUERTA: `web.auth.puede_entrar`, la MISMA lista que ya usan
    el panel (`web/auth.py::puede_entrar`) y la copia
    (`config.chats_de_copia`, derivada de `config.CHAT_IDS_PERMITIDOS`).

    Se mide CORRIENDO, no leyendo: para CADA `accion` real (sacada de
    `_acciones_reales()`, no tecleada), un chat AJENO (ni dueño, ni Rosi)
    no dispara NINGUNA llamada a `crud`/`db` -- ni siquiera para acciones
    que hoy no procesan más que un `q.answer()`. Si mañana alguien agrega
    una rama nueva y la escribe ANTES del candado (o en un `elif` que lo
    esquive), esta prueba se pone roja porque `_acciones_reales()` la
    encuentra y el espía ve una llamada que no debería haber pasado."""
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    for accion in _acciones_reales():
        espia = _Espia().instalar()
        try:
            data = f"{accion}:1:1" if accion == "acc" else f"{accion}:1"
            q = _Q(data, AJENO)
            _correr(botones.al_pulsar(_update(q), None))
            assert espia.llamadas == [], (
                f"'{accion}' disparó {espia.llamadas} para un chat sin acceso")
            assert q.editado == {}, f"'{accion}' editó la tarjeta sin acceso"
        finally:
            espia.restaurar()
    restaurar()


def test_la_puerta_esta_antes_del_despacho_de_acciones():
    """Comprobación estructural: la llamada a `puede_entrar` aparece ANTES,
    en el texto fuente, que el primer `if accion ==` -- así que ninguna
    rama es alcanzable sin pasar por ella. No reemplaza a la prueba de
    arriba (que lo mide corriendo): la complementa mostrando POR QUÉ es
    imposible saltarla."""
    import inspect

    fuente = inspect.getsource(botones.al_pulsar)
    pos_puerta = fuente.index("puede_entrar(")
    pos_primera_rama = fuente.index('if accion ==')
    assert pos_puerta < pos_primera_rama, (
        "la puerta tiene que estar ANTES del despacho de acciones")


# ═══════════════════════════════════════════════════════════════════════
# 2) A Rosi le funcionan -- de punta a punta, con `crud`/`db` espiados.
# ═══════════════════════════════════════════════════════════════════════

def test_descartar_le_funciona_a_rosi():
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    llamadas = []

    async def _cambiar_estado(bandeja_id, estado, desde=None):
        llamadas.append((bandeja_id, estado, desde))
        return True

    guardado = db.cambiar_estado
    db.cambiar_estado = _cambiar_estado
    try:
        q = _Q("no:5", ROSI)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado = guardado
        restaurar()
    assert llamadas == [(5, "descartado", "esperando_confirmacion")], (
        "Rosi tiene que poder descartar una tarjeta, igual que el dueño")
    assert "Descartado" in q.editado.get("texto", "")


def test_deshacer_le_funciona_a_rosi():
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    llamadas = []

    async def _deshacer(log_id):
        llamadas.append(log_id)
        return "el cambio"

    guardado = crud.deshacer
    crud.deshacer = _deshacer
    try:
        q = _Q("und:42", ROSI)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        crud.deshacer = guardado
        restaurar()
    assert llamadas == [42], "Rosi tiene que poder deshacer, igual que el dueño"
    assert "Deshecho" in q.editado.get("texto", "")


def test_un_chat_ajeno_sigue_sin_poder_nada():
    """Ni dueño ni Rosi: alguien fuera de `CHAT_IDS_PERMITIDOS` sigue sin
    poder tocar nada -- la puerta no se abrió de más."""
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    llamadas = []

    async def _cambiar_estado(*a, **k):
        llamadas.append(a)
        return True

    guardado = db.cambiar_estado
    db.cambiar_estado = _cambiar_estado
    try:
        q = _Q("no:5", AJENO)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado = guardado
        restaurar()
    assert llamadas == []
    assert q.editado == {}


# ═══════════════════════════════════════════════════════════════════════
# 3) La huella dice QUIÉN apretó -- no siempre "Tiziano".
# ═══════════════════════════════════════════════════════════════════════

def test_el_motivo_de_acc_dice_quien_aprieta():
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    motivos = []

    async def _cambiar_estado(*a, **k):
        return True

    async def _obtener(bandeja_id):
        return {"id": bandeja_id,
                "interpretacion": {"plan": {"accion": "editar", "tabla": "tareas",
                                            "cambios": {"estado": "hecha"},
                                            "resumen": "marcar hecha"}}}

    async def _editar(tabla, registro_id, cambios, motivo):
        motivos.append(motivo)
        return {"id": registro_id}, 77

    guardado = (db.cambiar_estado, db.obtener, crud.editar)
    db.cambiar_estado, db.obtener, crud.editar = _cambiar_estado, _obtener, _editar
    try:
        q = _Q("acc:5:9", ROSI)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado, db.obtener, crud.editar = guardado
        restaurar()
    assert motivos and "Rosi" in motivos[0], motivos
    assert "Tiziano" not in motivos[0], motivos


def test_el_motivo_de_ok_dice_quien_aprieta():
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    motivos = []

    async def _cambiar_estado(*a, **k):
        return True

    async def _obtener(bandeja_id):
        return {"id": bandeja_id,
                "interpretacion": {"clasificacion": "tarea", "titulo": "algo"}}

    async def _crear(bandeja_id, interpretacion, motivo=None):
        motivos.append(motivo)
        return "tareas", 1, 88

    guardado = (db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion)
    db.cambiar_estado, db.obtener = _cambiar_estado, _obtener
    crud.crear_desde_interpretacion = _crear
    try:
        q = _Q("ok:5", ROSI)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion = guardado
        restaurar()
    assert motivos and "Rosi" in motivos[0], motivos


def test_el_motivo_de_ok_dice_tiziano_cuando_el_que_aprieta_es_el():
    """Control: la MISMA tarjeta, apretada por el dueño, sigue diciendo
    su nombre -- no rompió el caso de siempre."""
    restaurar = _con_gente({DUENO: "Tiziano", ROSI: "Rosi"})
    motivos = []

    async def _cambiar_estado(*a, **k):
        return True

    async def _obtener(bandeja_id):
        return {"id": bandeja_id,
                "interpretacion": {"clasificacion": "tarea", "titulo": "algo"}}

    async def _crear(bandeja_id, interpretacion, motivo=None):
        motivos.append(motivo)
        return "tareas", 1, 88

    guardado = (db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion)
    db.cambiar_estado, db.obtener = _cambiar_estado, _obtener
    crud.crear_desde_interpretacion = _crear
    try:
        q = _Q("ok:5", DUENO)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion = guardado
        restaurar()
    assert motivos and "Tiziano" in motivos[0], motivos


def test_el_motivo_sin_nombre_puesto_no_inventa_uno():
    """Un chat en `CHAT_IDS_PERMITIDOS` pero SIN nombre en
    `NOMBRES_POR_CHAT` (el caso que el panel ya declara: "sin_nombre") no
    hace que el motivo mienta con un nombre inventado."""
    # No se usa `_con_gente`: acá hace falta que 777 esté en
    # CHAT_IDS_PERMITIDOS pero AUSENTE de NOMBRES_POR_CHAT -- justo lo que
    # el panel ya declara como "sin_nombre" (`config.chats_sin_nombre`).
    permitidos_orig = config.CHAT_IDS_PERMITIDOS
    nombres_orig = config.NOMBRES_POR_CHAT
    config.CHAT_IDS_PERMITIDOS = (DUENO, 777)
    config.NOMBRES_POR_CHAT = {DUENO: "Tiziano"}

    def restaurar():
        config.CHAT_IDS_PERMITIDOS = permitidos_orig
        config.NOMBRES_POR_CHAT = nombres_orig

    motivos = []

    async def _cambiar_estado(*a, **k):
        return True

    async def _obtener(bandeja_id):
        return {"id": bandeja_id,
                "interpretacion": {"clasificacion": "tarea", "titulo": "algo"}}

    async def _crear(bandeja_id, interpretacion, motivo=None):
        motivos.append(motivo)
        return "tareas", 1, 88

    guardado = (db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion)
    db.cambiar_estado, db.obtener = _cambiar_estado, _obtener
    crud.crear_desde_interpretacion = _crear
    try:
        q = _Q("ok:5", 777)
        _correr(botones.al_pulsar(_update(q), None))
    finally:
        db.cambiar_estado, db.obtener, crud.crear_desde_interpretacion = guardado
        restaurar()
    assert motivos and "alguien sin nombre" in motivos[0], motivos


# ═══════════════════════════════════════════════════════════════════════
# 4) Requisito 3: "que Rosi no pueda apretar un botón que actúe sobre algo
#    que no le toca". Evidencia de por qué NO APLICA hoy -- no un texto
#    prometido, medido contra el código real.
# ═══════════════════════════════════════════════════════════════════════

def test_una_tarjeta_con_botones_solo_llega_al_chat_que_la_origino():
    """`cerebro/agente.py::atender` arma `responder_kw` con el `chat_id` de
    LA PROPIA fila de bandeja (`fila["chat_id"]`), nunca con una constante
    ni con el de otra persona -- así que un botón (`teclado_deshacer_todo`,
    el único que `atender()` manda) nace SIEMPRE en el chat de quien
    escribió el mensaje que lo generó.

    Y el botón "und" que arma `al_pulsar` en su propia rama "acc"
    (`teclado_deshacer`) se manda con `q.message.reply_text(...)`, que
    Telegram responde EN EL MISMO chat que `q.message` -- nunca en otro.

    Entre los dos, hoy NO EXISTE un camino que le entregue a un chat un
    botón que actúe sobre la fila de OTRO chat -- así que el candado de
    pertenencia que pide el encargo (punto 3) no tiene sobre qué actuar
    TODAVÍA. La única puerta que sí cruza de chat es la copia
    (`cerebro/copia_dueno.py`), y le saca los botones a propósito --
    medido en `tests/test_copia_a_rosi.py` (`assert "reply_markup" not in
    copia`). El día que el encargo 2 (si Tiziano lo aprueba) le ponga
    botones a la copia, ESTA prueba es la que hay que romper primero -- y
    ahí sí hace falta el candado de pertenencia que este archivo no
    escribe.
    """
    import inspect

    fuente = inspect.getsource(__import__("cerebro.agente", fromlist=["atender"]).atender)
    assert 'chat_id = fila["chat_id"]' in fuente, (
        "atender() ya no arma responder_kw con el chat_id de la propia "
        "bandeja -- si esto cambió, revisar si ahora SÍ hace falta un "
        "candado de pertenencia acá")
    assert "responder_kw = dict(chat_id=chat_id" in fuente
