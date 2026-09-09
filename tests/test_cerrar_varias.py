# -*- coding: utf-8 -*-
"""Cerrar VARIAS cosas de un mensaje: que se cuente lo escrito y se pueda deshacer.

EL DEFECTO QUE ESTE ARCHIVO FIJA (medido el 8-sep-2026 contra fa0ca9f, que es
lo que estaba desplegado):

  1 consultar + 10 editar -> cerradas 10 | 1 botón de deshacer | "Listo."
  1 consultar + 11 editar -> cerradas 11 | 0 botones           | "Me enredé
                             tratando de resolver esto y prefiero no adivinar."

Son dos caras de la misma falla. El turno contaba lo que quería hacer en vez de
lo que YA había escrito:

  · con 10, el botón apuntaba a `acciones[-1]` y ofrecía volver atrás UNA de
    diez; el `antes` de las otras nueve seguía en `log_acciones` y no había por
    dónde pedirlo, porque el botón es el único camino de vuelta;
  · con 11 se acababan los pasos (MAX_PASOS=12, y la consulta gasta uno), y el
    mensaje decía que no había podido — con las once escrituras hechas. Ese día
    había exactamente 11 pendientes.

Subir MAX_PASOS no era el arreglo: mueve la pared. Lo que estas pruebas fijan es
la regla que la hace inofensiva:

  ⬛ TODO lo escrito sale nombrado en el mensaje, y el botón cubre TODO lo
     escrito. Salga el turno por donde salga.

Las guardas del final son las que impiden que esto vuelva. No comprueban un
caso: comprueban que no se pueda añadir una salida nueva —ni una herramienta
nueva que escriba— que se olvide del parte.

Y desde el 8-sep-2026 se miden CORRIENDO y no leyendo el archivo, porque leer no
alcanzaba. Las tres guardas de árbol de sintaxis perseguían nombres (`_enviar`,
`bot`, `crud.`, `db.`) y dos formas de escribir lo mismo las dejaban en verde:
un cliente de `telegram` armado a mano, y un `db.pool.connection()` con SQL
crudo. La segunda se saltaba las dos guardas de escritura a la vez y es la forma
más directa que existe. Lo que las cierra ahora es un doble en los sitios por
donde de verdad se sale del proceso —el pool de Postgres y los cuatro caminos de
un mensaje—, más el barrido de todas las herramientas sacadas del propio árbol
de `_ejecutar_herramienta`. El detalle, en el bloque «LA PUERTA DE VERDAD».

Correr:  python3 -m pytest tests/test_cerrar_varias.py
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import os
import pathlib
import re
import socket
import sys
import types

import httpx
import telegram

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

# `psycopg` de mentira, igual que en tests/test_panel.py: acá no se habla con
# Postgres. Lo que se mide es el camino del agente, no la base.
_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import acciones.botones as botones          # noqa: E402
import acciones.crud as crud                # noqa: E402
import cerebro.agente as agente             # noqa: E402
import cerebro.consultar as consultar       # noqa: E402
import cerebro.deepseek as motor            # noqa: E402
import config                               # noqa: E402
import db.db as db                          # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUENTE_AGENTE = open(os.path.join(RAIZ, "cerebro", "agente.py"),
                     encoding="utf-8").read()

TITULOS = ["Llamar al contador", "Pagar la luz", "Comprar café",
           "Mandar la factura de CDS", "Renovar el seguro",
           "Cita con el dentista", "Revisar el presupuesto",
           "Hablar con Rosi", "Depositar el cheque", "Sacar la basura",
           "Confirmar el vuelo", "Pedir la cotización"]


# ── El arnés ─────────────────────────────────────────────────────────────
#
# Corre `atender()` DE VERDAD. Lo único de mentira es lo que está fuera del
# proceso: el modelo, la base y Telegram. El bucle, el techo de pasos, el parte
# y el botón son los reales — que es justo lo que hay que medir.
#
# Los dobles se instalan sobre los módulos y no se devuelven a su sitio a mano:
# de eso se encarga el fixture autouse de conftest.py.

class _BotFalso:
    def __init__(self):
        self.enviados = []

    async def send_message(self, text, **kw):
        self.enviados.append({"text": text, "markup": kw.get("reply_markup")})
        return types.SimpleNamespace(message_id=1)


def _guion(n_editar: int, final: dict | None = None) -> list[dict]:
    """Los turnos que devolverá el modelo: una consulta, N ediciones y el final."""
    turnos = [{"herramienta": "consultar",
               "argumentos": {"sql": "SELECT id FROM tareas"}}]
    turnos += [{"herramienta": "editar",
                "argumentos": {"tabla": "tareas", "id": 100 + i,
                               "cambios": {"estado": "hecha"}}}
               for i in range(n_editar)]
    turnos.append(final or {"herramienta": "responder",
                            "argumentos": {"texto": "Listo.",
                                           "clasificacion": "orden"}})
    return turnos


def _modelo(turnos: list[dict], estado: dict) -> None:
    """Instala el modelo de mentira: devuelve `turnos` en orden y luego cierra.

    Está aparte de `_montar` porque las pruebas del final necesitan el modelo
    falso pero el `crud` REAL, y `_montar` le pone un doble a `crud.editar`.
    """
    class _Completions:
        async def create(self, **kw):
            i = estado["consumidos"]
            estado["consumidos"] += 1
            paso = (turnos[i] if i < len(turnos)
                    else {"herramienta": "responder",
                          "argumentos": {"texto": "Listo."}})
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content=json.dumps(paso, ensure_ascii=False)))])

    motor.cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions()))


def _montar(turnos: list[dict]) -> tuple[_BotFalso, dict]:
    estado = {"editadas": [], "consumidos": 0, "interpretaciones": []}
    _modelo(turnos, estado)

    huella = {"n": 0}

    async def _editar(tabla, registro_id, cambios, motivo=""):
        huella["n"] += 1
        estado["editadas"].append((tabla, registro_id, dict(cambios),
                                   huella["n"]))
        titulo = TITULOS[(registro_id - 100) % len(TITULOS)]
        return ({"id": registro_id, "titulo": titulo, **cambios}, huella["n"])

    crud.editar = _editar

    async def _ejecutar_sql(sql):
        return [{"id": 100 + i, "titulo": TITULOS[i]} for i in range(12)]

    consultar._ejecutar = _ejecutar_sql
    consultar._validar = lambda sql: sql

    async def _nada(*a, **k):
        return None

    async def _vacio(*a, **k):
        return []

    db.buscar_esperando_respuesta = _nada
    db.ultimos_intercambios = _vacio
    db.listar_preferencias = _vacio
    db.cambiar_estado = _nada
    db.guardar_respuesta = _nada

    async def _guardar_interpretacion(bandeja_id, clasificacion, interp,
                                      estado_kw=None, **kw):
        estado["interpretaciones"].append(
            {"clasificacion": clasificacion, "interp": interp,
             "estado": kw.get("estado", estado_kw)})

    db.guardar_interpretacion = _guardar_interpretacion

    return _BotFalso(), estado


def _correr(n_editar: int, final: dict | None = None) -> tuple[dict, dict]:
    """Devuelve (el último mensaje que salió, el estado del arnés)."""
    bot, estado = _montar(_guion(n_editar, final))
    fila = {"id": 77, "chat_id": config.CHAT_ID_DUENO, "telegram_msg_id": 9,
            "tipo_entrada": "texto"}
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        agente.atender(fila, "ya hice todo", bot))
    return bot.enviados[-1], estado


def _botones_de(mensaje: dict) -> list[str]:
    markup = mensaje["markup"]
    if markup is None:
        return []
    return [b.callback_data for fila in markup.inline_keyboard for b in fila]


# ── Lo que le muerde hoy: once cierres ───────────────────────────────────

def test_once_cierres_se_cuentan_todos_y_el_boton_los_cubre():
    """EL CASO DEL 8-sep: 11 pendientes, "ya hice todo", y el bucle se corta.

    Antes: 11 escritas, 0 botones, y un mensaje diciendo que no pudo. Ahora las
    once tienen que estar nombradas y el botón tiene que cubrir las once.
    """
    mensaje, estado = _correr(11)
    assert len(estado["editadas"]) == 11, "el arnés no llegó a las 11"

    for _tabla, _rid, _cambios, _log in estado["editadas"]:
        titulo = TITULOS[(_rid - 100) % len(TITULOS)]
        assert titulo in mensaje["text"], (
            f"«{titulo}» se escribió y no aparece en el mensaje")

    assert _botones_de(mensaje) == ["undt:77"], (
        "el botón no cubre el mensaje entero")

    # Y las huellas son LAS DE VERDAD, una por escritura. Contarlas no alcanza:
    # once asas repetidas apuntando a la misma huella también dan once, y el
    # botón desharía una sola cosa once veces.
    huellas = estado["interpretaciones"][-1]["interp"]["hecho"]
    devueltas = [log for _t, _r, _c, log in estado["editadas"]]
    assert huellas == devueltas, (
        f"las asas guardadas {huellas} no son las que devolvió crud "
        f"{devueltas}")
    assert len(set(huellas)) == 11, (
        f"hay asas repetidas: {huellas}")


def test_quedarse_sin_pasos_no_puede_decir_que_no_pudo_si_ya_escribio():
    """El mensaje viejo —"Me enredé... prefiero no adivinar"— con once
    escrituras hechas es una mentira en la dirección cómoda: las filas quedan
    cambiadas y él cree que no pasó nada."""
    mensaje, _ = _correr(11)
    assert "prefiero no adivinar" not in mensaje["text"]
    assert "Me enredé" not in mensaje["text"]


def test_sin_escribir_nada_el_mensaje_de_enredo_sigue_igual():
    """La otra mitad: si de verdad no hizo nada, decir que se enredó es correcto
    y no se toca. Y sin escrituras no hay botón que ofrecer."""
    turnos = [{"herramienta": "consultar",
               "argumentos": {"sql": "SELECT 1"}}] * 13
    bot, _ = _montar(turnos)
    fila = {"id": 77, "chat_id": config.CHAT_ID_DUENO, "telegram_msg_id": 9,
            "tipo_entrada": "texto"}
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        agente.atender(fila, "algo raro", bot))
    mensaje = bot.enviados[-1]
    assert "Me enredé tratando de resolver esto" in mensaje["text"]
    assert "Ya quedó hecho" not in mensaje["text"]
    assert _botones_de(mensaje) == []


# ── El botón cubre TODO, no solo lo último ───────────────────────────────

def test_el_boton_no_apunta_solo_a_la_ultima_accion():
    """Con diez cierres el turno terminaba bien y ofrecía deshacer UNO.

    `acciones[-1]` era literalmente eso: la última. Las otras nueve quedaban sin
    camino de vuelta aunque su `antes` estuviera guardado.
    """
    mensaje, estado = _correr(10)
    assert len(estado["editadas"]) == 10
    assert _botones_de(mensaje) == ["undt:77"], (
        "el botón sigue apuntando a una sola acción")
    assert len(estado["interpretaciones"][-1]["interp"]["hecho"]) == 10


def test_una_sola_escritura_tambien_se_cuenta_y_en_un_renglon():
    """Es un mensaje de Telegram: con una sola cosa el parte es un renglón, no
    una lista con encabezado."""
    mensaje, _ = _correr(1)
    assert "Ya está hecho" in mensaje["text"]
    assert "Ya quedó hecho" not in mensaje["text"]
    assert TITULOS[0] in mensaje["text"]
    assert _botones_de(mensaje) == ["undt:77"]


def test_preguntar_tambien_lleva_el_parte_y_el_boton():
    """La ventana era la tercera salida sin parte: Lucy escribía tres cosas,
    dudaba, preguntaba — y lo escrito se quedaba sin contar y sin botón."""
    mensaje, estado = _correr(3, final={
        "herramienta": "preguntar",
        "argumentos": {"texto": "¿La de las 3 o la de las 5?"}})
    assert "¿La de las 3 o la de las 5?" in mensaje["text"]
    assert "Ya quedó hecho" in mensaje["text"]
    assert _botones_de(mensaje) == ["undt:77"]
    ultima = estado["interpretaciones"][-1]
    assert ultima["estado"] == "esperando_respuesta", (
        "la ventana dejó de quedar abierta")
    assert len(ultima["interp"]["hecho"]) == 3


# ── Deshacer varias ──────────────────────────────────────────────────────

def test_deshacer_varias_va_de_atras_para_adelante():
    """El orden no es estilo. Dos ediciones sobre la misma fila encadenan sus
    `antes`: la primera guarda A y deja B, la segunda guarda B y deja C.
    Deshaciendo en orden se queda en B; al revés vuelve a A, que es de donde
    salimos."""
    orden = []

    async def _deshacer(log_id):
        orden.append(log_id)
        return "el cambio"

    crud.deshacer = _deshacer
    revertidas, fallos = asyncio.get_event_loop_policy().new_event_loop(
        ).run_until_complete(crud.deshacer_varias([41, 42, 43]))
    assert orden == [43, 42, 41], f"deshizo en el orden {orden}"
    assert (revertidas, fallos) == (3, [])


def test_deshacer_varias_no_se_para_en_el_primer_fallo_y_lo_dice():
    """Quedarse a medias en silencio sería lo peor de las dos cosas."""
    async def _deshacer(log_id):
        if log_id == 42:
            raise ValueError("No encuentro esa acción en el registro.")
        return "el cambio"

    crud.deshacer = _deshacer
    revertidas, fallos = asyncio.get_event_loop_policy().new_event_loop(
        ).run_until_complete(crud.deshacer_varias([41, 42, 43]))
    assert revertidas == 2
    assert len(fallos) == 1 and "#42" in fallos[0]


def test_el_boton_deshace_todas_las_huellas_del_mensaje():
    """De punta a punta: el `callback_data` que sale del turno, metido en el
    manejador de botones, tiene que revertir las once."""
    deshechas = []

    async def _deshacer_varias(log_ids):
        deshechas.extend(log_ids)
        return len(list(log_ids)), []

    crud.deshacer_varias = _deshacer_varias

    async def _obtener(bandeja_id):
        return {"id": bandeja_id,
                "interpretacion": {"hecho": list(range(1, 12))}}

    db.obtener = _obtener

    editado = {}

    class _Q:
        data = "undt:77"
        message = types.SimpleNamespace(
            chat_id=config.CHAT_ID_DUENO, text_html="Listo.",
            reply_text=None)

        async def answer(self, *a, **k):
            editado["alerta"] = a[0] if a else None

        async def edit_message_text(self, text, **kw):
            editado["texto"] = text

    q = _Q()
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        botones.al_pulsar(types.SimpleNamespace(callback_query=q), None))
    assert deshechas == list(range(1, 12)), (
        f"el botón mandó a deshacer {deshechas}")
    assert "11" in editado.get("texto", ""), editado


# ── Las dos guardas: que esto no pueda volver ────────────────────────────

def _nodo(nombre: str) -> ast.AST:
    for n in ast.walk(ast.parse(FUENTE_AGENTE)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == nombre:
            return n
    raise AssertionError(f"no encontré {nombre}() en cerebro/agente.py")


def test_ninguna_salida_de_atender_esquiva_la_puerta():
    """UNA sola puerta de salida, y se comprueba recorriendo el código.

    Esto es lo que impide que el defecto vuelva con otra ropa. Antes había
    cuatro salidas y cada una decidía por su cuenta qué contar de lo escrito:
    dos no contaban nada. Una salida nueva que mande el mensaje por su cuenta
    —exactamente lo que pasó con el bloque del panel en septiembre— se lleva por
    delante el parte y el botón sin que nada se ponga rojo.

    La lista de salidas NO está escrita acá: sale del árbol de sintaxis de
    `atender`, así que cubre también la que alguien escriba mañana.

    No se persigue la LLAMADA a `_enviar` sino el NOMBRE, y además el del propio
    `bot`. Medido: buscando llamadas, una salida nueva que hiciera
    `mandar = _enviar; await mandar(...)` salía VERDE.

    LO QUE ESTO NO VE, medido el 8-sep-2026 y corregido acá porque este mismo
    docstring decía lo contrario. Decía que «para hablarle a Tiziano fuera de la
    puerta hay que nombrar alguno de los dos, y los dos están vigilados». **No es
    cierto: hay una tercera forma**, y no necesita nombrar ninguno de los dos —
    armarse un cliente propio:

        mensajero = telegram.Bot(token=config.TELEGRAM_TOKEN)
        await mensajero.send_message(chat_id=config.CHAT_ID_DUENO, text=texto)

    Metido en una copia limpia sacada con `git archive`, eso dejaba este archivo
    en 13 passed. Perseguir la cuarta forma no tiene fondo, así que la que de
    verdad cierra ese lado no es ésta: es
    `test_un_turno_manda_un_solo_mensaje_y_sale_por_la_puerta`, que mide
    corriendo cuántos mensajes salieron del proceso y por dónde, sin mirar un
    solo nombre. Ésta se queda con lo que aquélla no puede ver —una salida
    escrita y todavía sin llamar— junto con
    `test_en_agente_solo__enviar_nombra_al_cliente_de_telegram`.

    Y `atender` tampoco es el único sitio del proyecto que le manda mensajes a
    Telegram: el despertador tiene los suyos. Esta guarda habla de las salidas de
    UN turno, no de todo lo que Lucy dice.
    """
    atender = _nodo("atender")
    puerta = next(
        (n for n in ast.walk(atender)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "_fin_del_turno"), None)
    assert puerta is not None, "desapareció _fin_del_turno de atender()"

    for quien in ("_enviar", "bot"):
        usos = [n for n in ast.walk(atender)
                if isinstance(n, ast.Name) and n.id == quien]
        assert usos, f"no encontré ni un uso de `{quien}` dentro de atender()"
        fuera = sorted({n.lineno for n in usos
                        if not (puerta.lineno <= n.lineno <= puerta.end_lineno)})
        assert not fuera, (
            f"`{quien}` se usa fuera de _fin_del_turno (líneas {fuera} de "
            f"cerebro/agente.py). Ahí hay una salida que manda el mensaje por "
            f"su cuenta, y lo ya escrito se queda sin parte y sin botón")


def _escribe_y_devuelve_su_asa(nombre: str) -> bool:
    """¿Esa función de crud deja una huella Y devuelve el asa para deshacerla?

    Las dos mitades se le preguntan al propio `acciones/crud.py`, no a una lista
    escrita acá: escribe si su cuerpo llama a `_registrar(`, y devuelve el asa
    si su anotación de retorno nombra un `int`. Por eso `deshacer` —que registra
    su propia huella pero devuelve una frase— queda fuera sola, sin exención por
    nombre.
    """
    fn = getattr(crud, nombre, None)
    if not callable(fn):
        return False
    try:
        cuerpo = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    if "_registrar(" not in cuerpo:
        return False
    retorno = str(inspect.signature(fn).return_annotation)
    return bool(re.search(r"\bint\b", retorno))


def test_toda_herramienta_que_escribe_pasa_por_anotar():
    """Sus hermanos, y que lo cumplan TODOS.

    `_anotar` es el único sitio por donde una escritura entra en el parte, y
    exige la frase en el momento de escribir. Una herramienta nueva que llame a
    `crud` y se guarde el número para sí misma vuelve a dejar trabajo hecho sin
    contar — y hoy eso no rompería nada visible.

    La lista de "las que escriben" sale de crud, no de acá.
    """
    fn = _nodo("_ejecutar_herramienta")
    padres = {}
    for n in ast.walk(fn):
        for hijo in ast.iter_child_nodes(n):
            padres[hijo] = n

    def _if_de(nodo):
        while nodo in padres:
            nodo = padres[nodo]
            if isinstance(nodo, ast.If):
                return nodo
        return fn

    sin_anotar, revisadas = [], []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "crud"):
            continue
        if not _escribe_y_devuelve_su_asa(n.func.attr):
            continue
        revisadas.append(n.func.attr)
        rama = _if_de(n)
        anota = any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                    and x.func.id == "_anotar" for x in ast.walk(rama))
        if not anota:
            sin_anotar.append(f"crud.{n.func.attr} (línea {n.lineno})")

    assert len(revisadas) >= 5, (
        f"la guarda solo halló {revisadas}: si crud dejó de anotar sus huellas "
        f"con `_registrar(`, esta prueba se volvió decorativa")
    assert "deshacer" not in revisadas, (
        "`deshacer` no devuelve un asa nueva: si entró, el criterio se aflojó")
    assert not sin_anotar, (
        f"escriben y no pasan por `_anotar`, así que lo que hagan no se cuenta "
        f"ni se puede deshacer: {sin_anotar}")


def test_ninguna_herramienta_escribe_esquivando_crud():
    """La otra mitad de lo mismo, y hay que decirla o la guarda de arriba miente.

    Vigilar `crud.*` cubre a quien escribe por donde se debe. Medido: una
    herramienta nueva que llamara a `db.a_la_papelera` derecho salía VERDE —
    escribe, no deja huella con asa, y por lo tanto no hay nada que anotar ni
    que deshacer. La guarda de arriba no podía verla porque solo miraba `crud`.

    Así que acá se cierra por el otro lado: `_ejecutar_herramienta` no llama a
    ninguna función de `db.db` que escriba. Cuáles escriben se le pregunta a
    `db/db.py`, no a una lista de acá: es la que tenga un INSERT, un UPDATE o un
    DELETE en su cuerpo.

    HASTA DÓNDE LLEGA, medido el 8-sep-2026 y CORREGIDO acá: la frontera que
    este docstring declaraba se quedaba muy corta. Decía que el hueco era el
    alias (`import db.db as base`; `base.a_la_papelera(...)`) y que «esto atrapa
    al que escribe de la forma normal, que es como se escribe». **Falso, y por
    el lado peor.** No hace falta ningún alias:

        with db.pool.connection() as con:
            con.execute("UPDATE tareas SET borrado_en = now() WHERE id = %s", ...)

    Eso se salta ESTA guarda —el nodo llamado es `db.pool.connection`, no
    `db.<función>`— y también la de arriba, porque no nombra `crud`. Y no es una
    forma rebuscada: es exactamente como escribe `acciones/crud.py`, diez veces.
    Con ese código dentro, este archivo quedaba en 13 passed.

    Estas dos guardas se quedan porque son baratas y miran un eje distinto, pero
    la que de verdad cierra las escrituras es
    `test_ninguna_escritura_llega_a_la_base_sin_su_huella`: pone un doble en
    `db.pool` —el único camino a Postgres que hay en el proceso— y mira las
    sentencias que salieron, se llame como se llame quien las mandó.
    """
    fn = _nodo("_ejecutar_herramienta")

    def _escribe_en_la_base(nombre: str) -> bool:
        f = getattr(db, nombre, None)
        if not callable(f):
            return False
        try:
            cuerpo = inspect.getsource(f).upper()
        except (OSError, TypeError):
            return False
        return any(v in cuerpo for v in ("INSERT ", "UPDATE ", "DELETE "))

    culpables = sorted({
        f"db.{n.func.attr} (línea {n.lineno})"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "db"
        and _escribe_en_la_base(n.func.attr)})

    assert not culpables, (
        f"escriben en la base sin pasar por `crud`, así que no dejan huella con "
        f"asa y no hay forma de contarlo ni de deshacerlo: {culpables}")


def test_el_parte_nunca_esconde_menos_de_lo_que_un_turno_puede_escribir():
    """El techo del parte sale de MAX_PASOS y no de un número tecleado: un turno
    no puede escribir más veces que pasos tiene, así que nada queda elidido — y
    si alguien mueve el techo, el parte lo sigue solo."""
    assert agente.MAX_EN_EL_PARTE >= agente.MAX_PASOS


# ═════════════════════════════════════════════════════════════════════════
# LA PUERTA DE VERDAD: se mide CORRIENDO, no leyendo
# ═════════════════════════════════════════════════════════════════════════
#
# POR QUÉ EXISTE ESTA SECCIÓN (medido el 8-sep-2026 contra 30168c3):
#
# Las tres guardas de arriba persiguen NOMBRES en el texto del archivo, y por
# eso prometen más de lo que entregan. Dos formas de escribir lo mismo, metidas
# en una copia limpia sacada con `git archive`, las dejaban en 13 passed:
#
#   · una salida que no se llama ni `_enviar` ni `bot`:
#         mensajero = telegram.Bot(token=config.TELEGRAM_TOKEN)
#         await mensajero.send_message(chat_id=..., text=texto)
#   · una escritura que no nombra ni `crud.` ni `db.<función>`:
#         with db.pool.connection() as con:
#             con.execute("UPDATE tareas SET borrado_en = now() WHERE id = %s", ...)
#
# La segunda es la peor de las dos: no necesita ningún alias raro —es el patrón
# más directo que hay, y `db.pool.connection()` se usa así dentro del propio
# `acciones/crud.py`— y se salta LAS DOS guardas de escritura a la vez. Una
# herramienta escrita así archiva o edita filas sin dejar `log_acciones`, sin
# entrar en el parte y sin que el botón pueda deshacerla.
#
# LA DIRECCIÓN: perseguir formas de escribir código no tiene fondo. Después del
# `telegram.Bot` viene un envoltorio, después un `getattr`, después una clausura.
# Lo que SÍ tiene fondo es lo que el código HACE:
#
#   · para que un mensaje salga del proceso hay que hacer E/S. Da igual cómo se
#     llame quien la haga: pasa por el objeto `bot` del turno, por un cliente de
#     `telegram` propio, por `httpx`, o por un socket. Los cuatro llevan doble.
#   · para que una escritura llegue a Postgres hay que pasarla por un CURSOR de
#     psycopg. Da igual de dónde salga la conexión: `db.pool`, un pool armado a
#     mano, un `psycopg.connect()` suelto. Con un doble en el cursor se ve CADA
#     sentencia con su texto ya armado, se llame como se llame quien la mandó.
#
# CORRECCIÓN DEL 8-sep-2026 — lo que este bloque decía antes era FALSO y sostuvo
# dos vueltas de trabajo. Decía: «para que una escritura llegue a Postgres hay
# que pedirle una conexión al pool; `db.pool` es el único de este proceso».
# No es cierto, y se midió preguntándole al paquete instalado (recorriendo
# `dir(psycopg)` y `dir(psycopg_pool)`, no una lista escrita acá): hay OCHO
# formas de conseguir una conexión y la guarda de entonces veía DOS.
#
#     psycopg.connect                      NO la veía
#     psycopg.Connection                   NO
#     psycopg.BaseConnection               NO
#     psycopg.AsyncConnection              sí
#     psycopg_pool.ConnectionPool          NO
#     psycopg_pool.NullConnectionPool      NO
#     psycopg_pool.AsyncNullConnectionPool NO
#     psycopg_pool.AsyncConnectionPool     sí
#
# Y se reprodujo: una herramienta que escribía por `psycopg.connect(...)`
# síncrono corrió nueve veces y la suite entera dio 452 passed.
#
# Y las ramas que se recorren no salen de una lista escrita acá: salen del árbol
# de sintaxis de `_ejecutar_herramienta`. Una herramienta nueva entra sola en el
# barrido y se ejecuta de verdad, aunque el guion del modelo nunca la pida.

_VERBOS = re.compile(
    r"^\s*(?:WITH\b.*?\)\s*)?(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
    re.IGNORECASE | re.DOTALL)

# Las tablas que Lucy toca por encargo de Tiziano. Sale de `crud`, no de acá: si
# mañana hay una tabla más, entra sola en la vigilancia.
TABLAS_DE_DOMINIO = frozenset(crud.TABLAS)
TABLA_DE_HUELLAS = "log_acciones"

# ── El cubo indulgente, nombrado uno por uno ─────────────────────────────
#
# Estas funciones escriben en una tabla de dominio Y NO dejan huella. Están acá
# enumeradas a propósito y no deducidas: a lo que perdona no se puede llegar por
# olvido. Lo que no esté en esta lista cae del lado estricto y se pone rojo.
#
# `acciones/crud.py:236` lo dice así: «Personas y proyectos se resuelven fuera
# de la transacción a propósito: crear una persona de más es inofensivo y
# reutilizable». Es una decisión de diseño escrita y no la toco. Pero medida
# tiene consecuencia, y queda dicha acá porque nadie más la iba a ver:
# «anotá llamar a Rosita» crea una fila en `personas` que NO sale en el parte y
# que el botón de deshacer NO revierte.
EXENTAS_DE_HUELLA = ("buscar_o_crear_persona", "buscar_o_crear_proyecto")

# ── El otro cubo indulgente, también nombrado uno por uno ────────────────
#
# Una huella con asa es una acción que Tiziano puede deshacer, y por eso TIENE
# que salir en el parte. La única que no ofrece asa es la del propio deshacer:
# revertir algo se registra —la historia se agrega, no se reescribe— pero ese
# renglón no es una acción nueva que ofrecerle deshacer.
#
# Enumerado y no deducido, por lo mismo de arriba: una acción nueva que nadie
# clasifique cae del lado estricto y exige salir en el parte.
ACCIONES_SIN_ASA = ("deshacer",)


def _accion_de_la_huella(sql: str, args) -> str | None:
    """Qué acción registra ese INSERT en log_acciones, leído del SQL ejecutado.

    Se resuelve la columna `accion` por su POSICIÓN en la lista de columnas del
    propio INSERT, y de ahí se saca el valor: si en VALUES hay un literal, ese;
    si hay un `%s`, el parámetro que le toca. Así da igual que la escriba
    `crud._registrar` (que la pasa como parámetro) o `db.a_la_papelera` (que la
    lleva escrita dentro del SQL).
    """
    m = re.search(r"INSERT\s+INTO\s+log_acciones\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)",
                  sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    columnas = [c.strip().lower() for c in m.group(1).split(",")]
    valores = [v.strip() for v in m.group(2).split(",")]
    if "accion" not in columnas or len(columnas) != len(valores):
        return None
    i = columnas.index("accion")
    bruto = valores[i]
    if bruto == "%s":
        cuantos_antes = sum(1 for v in valores[:i] if v == "%s")
        try:
            return str(list(args)[cuantos_antes])
        except (TypeError, IndexError):
            return None
    return bruto.strip("'\"") or None


def _pila_corta(n: int = 14) -> list[str]:
    """Los nombres de las funciones que llevaron hasta acá.

    Barato a propósito: `inspect.stack()` construye objetos de marco completos y
    esto corre en CADA sentencia del barrido.
    """
    nombres, f = [], sys._getframe(1)
    while f is not None and len(nombres) < n:
        nombres.append(f.f_code.co_name)
        f = f.f_back
    return nombres


def _que_hace(sql: str) -> tuple[str, str] | None:
    """(verbo, tabla) de una sentencia YA ARMADA, o None si no escribe.

    Se lee el texto que de verdad se ejecutó, no el código fuente que lo
    produjo. Ésa es toda la diferencia: `crud` interpola el nombre de la tabla
    en una f-string, y acá llega resuelto.
    """
    m = _VERBOS.match(sql or "")
    if not m:
        return None
    return m.group(1).split()[0].upper(), m.group(2).lower()


class _Libro:
    """Todo lo que se ejecutó contra una conexión, en orden y por bloque."""

    def __init__(self):
        self.sentencias: list[dict] = []
        self.exentas_que_dispararon: set[str] = set()
        self._siguiente_huella = 1000

    def apuntar(self, bloque: int, sql: str, args) -> None:
        hecho = _que_hace(sql)
        pila = _pila_corta()
        self.sentencias.append({
            "bloque": bloque, "sql": " ".join(str(sql).split())[:600],
            "args": args, "pila": pila,
            "verbo": hecho[0] if hecho else None,
            "tabla": hecho[1] if hecho else None,
        })

    def nueva_huella(self) -> int:
        self._siguiente_huella += 1
        return self._siguiente_huella

    def escrituras_de_dominio(self) -> list[dict]:
        return [s for s in self.sentencias
                if s["verbo"] and s["tabla"] in TABLAS_DE_DOMINIO]

    def huellas(self) -> list[dict]:
        return [s for s in self.sentencias
                if s["verbo"] == "INSERT" and s["tabla"] == TABLA_DE_HUELLAS]

    def asas(self) -> list[dict]:
        """Las huellas que SÍ se pueden deshacer, o sea las que deben ir al parte."""
        return [h for h in self.huellas()
                if _accion_de_la_huella(h["sql"], h["args"])
                not in ACCIONES_SIN_ASA]

    def mudas(self) -> list[dict]:
        """Escrituras de dominio sin una huella en su mismo bloque de conexión.

        El bloque es la unidad porque así lo hace `crud`: abre la conexión,
        escribe, y registra en `log_acciones` antes de soltarla. Una escritura
        que sale de un bloque sin huella no se puede contar ni deshacer.

        Lo que emitió cada sentencia se toma de la PILA DE LLAMADAS, no del
        texto del archivo: quien de verdad la mandó, se llame el código como se
        llame en el fuente.
        """
        con_huella = {s["bloque"] for s in self.huellas()}
        mudas = []
        for s in self.escrituras_de_dominio():
            if s["bloque"] in con_huella:
                continue
            exenta = next((e for e in EXENTAS_DE_HUELLA if e in s["pila"]), None)
            if exenta:
                self.exentas_que_dispararon.add(exenta)
                continue
            mudas.append(s)
        return mudas


class _CursorEspia:
    """Cursor de mentira que apunta TODO lo que se le manda.

    `execute` es SÍNCRONO y devuelve un objeto esperable, y eso es la pieza
    clave: un `async def` que nadie espera nunca corre su cuerpo, así que un
    `con.execute("UPDATE ...")` sin `await` no dejaría rastro. Y ésa es
    justamente una de las formas en que se cuela una escritura muda.
    """

    def __init__(self, libro: _Libro, bloque: int, como_dict: bool):
        self._libro, self._bloque = libro, bloque
        self._dict = como_dict
        self._sql = ""
        self._args = None

    def execute(self, sql, args=None, *a, **k):
        self._sql, self._args = str(sql), args
        self._libro.apuntar(self._bloque, self._sql, args)
        return self

    def executemany(self, sql, seq=(), *a, **k):
        for args in list(seq) or [None]:
            self.execute(sql, args)
        return self

    def __await__(self):
        async def _yo():
            return self
        return _yo().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchone(self):
        sql = self._sql.upper()
        if "RETURNING" in sql:
            n = self._libro.nueva_huella()
            return {"id": n} if self._dict else (n,)
        if sql.lstrip().startswith("SELECT") and self._dict:
            return self._fila()
        # Modo tupla y sin RETURNING: no hay fila. Es lo que hace que la
        # deduplicación de `crud.crear` no crea haber visto un duplicado.
        return None

    async def fetchall(self):
        fila = await self.fetchone()
        return [fila] if fila else []

    def _fila(self):
        """Una fila plausible de la tabla que se consultó.

        Las columnas salen de `db/schema.sql` a través de
        `db.columnas_declaradas()`, no de una lista escrita acá: si mañana
        aparece una columna, esta fila la tiene.
        """
        m = re.search(r"\bFROM\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", self._sql,
                      re.IGNORECASE)
        tabla = (m.group(1).lower() if m else "tareas")
        columnas = db.columnas_declaradas().get(tabla) or ["id"]
        rid = next((a for a in (self._args or ()) if isinstance(a, int)), 100)
        fila = {c: None for c in columnas}
        fila.update({k: v for k, v in {
            "id": rid,
            "bandeja_id": 77,
            "estado": "pendiente",
            "titulo": TITULOS[rid % len(TITULOS)],
            "nombre": TITULOS[rid % len(TITULOS)],
            "texto": TITULOS[rid % len(TITULOS)],
            "tipo": "gasto",
            "alias": [],
            # Una huella plausible, para que `crud.deshacer` llegue a escribir
            # en vez de morir leyendo. Sin esto esa rama sale del barrido sin
            # veredicto, que es lo que hay que evitar.
            "accion": "borrar",
            "tabla": "tareas",
            "registro_id": rid,
        }.items() if k in fila})
        return fila


class _ConexionEspia:
    def __init__(self, libro, bloque):
        self._libro, self._bloque = libro, bloque

    def cursor(self, *a, **k):
        # `row_factory=dict_row` es lo que distingue una lectura que espera un
        # diccionario de una que espera una tupla. Se mira si vino, no cuál es.
        return _CursorEspia(self._libro, self._bloque,
                            como_dict=bool(k.get("row_factory")))

    def execute(self, sql, args=None, *a, **k):
        return _CursorEspia(self._libro, self._bloque,
                            como_dict=False).execute(sql, args)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None

    # Una conexión suelta se usa de las cuatro maneras, y las cuatro tienen que
    # caer en el mismo sitio: `await ...connect()`, `with ...connect() as c`
    # (así lo hace `db/backup.py:380`), `async with`, y a pelo.
    def __await__(self):
        async def _yo():
            return self
        return _yo().__await__()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Bloque:
    """`pool.connection()` sirve para `async with` Y para `with`.

    Los dos, a propósito: una escritura muda escrita con el `with` equivocado
    seguiría llegando a Postgres el día que alguien la corrija, y una guarda que
    solo mira la forma buena no la vería venir.
    """

    def __init__(self, libro, bloque):
        self._con = _ConexionEspia(libro, bloque)

    async def __aenter__(self):
        return self._con

    async def __aexit__(self, *a):
        return False

    def __enter__(self):
        return self._con

    def __exit__(self, *a):
        return False


# El libro que está midiendo ahora mismo. Lo usa la fábrica de pools de abajo,
# para que un pool hecho a mano —que no pasa por `db.pool`— también se apunte.
_LIBRO_ACTIVO: _Libro | None = None


class _PoolEspia:
    """El único sitio por donde se consigue hablar con Postgres en este proceso."""

    def __init__(self, libro: _Libro):
        self.libro = libro
        self._bloques = 0

    def connection(self, *a, **k):
        self._bloques += 1
        return _Bloque(self.libro, self._bloques)

    async def open(self, *a, **k):
        return None

    async def close(self, *a, **k):
        return None


# ═════════════════════════════════════════════════════════════════════════
# LA PUERTA DEL CURSOR: una sola, y tiene que PROBAR que está puesta
# ═════════════════════════════════════════════════════════════════════════
#
# LA REGLA, EN UNA LÍNEA: todo lo que este proceso le manda a Postgres por
# psycopg pasa por un cursor, y el doble está en el cursor.
#
# POR QUÉ EL CURSOR ES UN FONDO Y NO OTRA LISTA (medido el 8-sep-2026):
#
#   · los 10 cursores que expone psycopg derivan TODOS de
#     `psycopg.cursor.BaseCursor` — comprobado recorriendo los módulos del
#     paquete y preguntando por el atributo `execute`, no por un prefijo de
#     nombre;
#   · `Connection.execute` y `AsyncConnection.execute` NO hablan con la base:
#     leído en su fuente, construyen un cursor y llaman a su `execute`;
#   · y debajo del cursor ya no hay Python: `psycopg.pq.__impl__` es `binary` y
#     `psycopg_binary.pq.PGconn` es un tipo inmutable —
#     `TypeError: cannot set 'exec_' attribute of immutable type`.
#
# Así, las tres formas que antes salían verdes salen rojas SIN NOMBRARLAS: no se
# las persigue, se mira por dónde salen.
#
# ⬛ Y LA PUERTA TIENE QUE PROBAR QUE ESTÁ PUESTA, O PONERSE ROJA.
#
# Éste es el punto por el que esta serie llevaba vueltas fallando, y no es un
# adorno. Medido dentro de la suite ENTERA:
#
#     sys.modules["psycopg"] = <module 'psycopg' from
#                               <test_reporte_una_vez_al_dia._Cualquiera ...>>
#
# Otros DIEZ archivos de `tests/` plantan un `psycopg` de mentira al importarse
# y no lo devuelven nunca; el `conftest` solo restaura módulos de Lucy. La cifra
# se reproduce así, y por eso se escribe —una cifra que nadie puede repetir es
# peor que ninguna, porque el que la lee la da por buena:
#
#     grep -lE 'sys\.modules\[[^]]*\] *=' tests/*.py | xargs grep -l '"psycopg"'
#
# Da 11 archivos, y uno de los once es éste. Se cuenta con UN comando y no con
# dos: la primera versión de esta cifra usaba dos greps por separado, uno por
# cada forma de plantar el muñeco, y se le escaparon dos archivos porque un par
# de ellos escribe `sys.modules[n]` sin guion bajo. Es la misma trampa que
# persigue este archivo —una lista escrita a mano— metida en el comentario que
# la denuncia.
#
# El peor de los diez es un
# atrapa-todo que responde a CUALQUIER atributo: `hasattr(psycopg.cursor,
# "BaseCursor")` devuelve True siendo falso. O sea que una puerta que se
# conforme con que el nombre exista se pone a sí misma sobre un muñeco y mide
# CERO en verde, sin distinguir «vi todo y no había nada» de «nadie me enseñó
# nada».
#
# Por eso: el módulo real se busca por `isinstance(__file__, str)` —los dos
# tipos de mentira fallan ahí, el atrapa-todo y el `types.ModuleType` pelado— y
# la clase se exige con `isinstance(..., type)`, nunca con `hasattr`. Si no se
# puede DEMOSTRAR, esto sale sin veredicto y en ROJO.


_REALES: dict = {}


def _modulo_de_verdad(nombre: str):
    """`_buscar_modulo_de_verdad` con memoria: el real es siempre el mismo objeto.

    La memoria está porque el rastreo recorre `sys.modules` entero y los
    atributos de cada módulo, y eso corre en cada tendido de red. NO se afirma
    acá cuánto ahorra: se midió la suite entera con y sin ella y la diferencia
    quedó por debajo del ruido de la máquina (tres corridas de cada una, 9.4–12.0 s
    contra 9.7–11.3 s, rangos solapados). Una cifra que no se sostiene es peor
    que ninguna, así que no se pone.
    """
    hallado = _buscar_modulo_de_verdad(nombre)
    if hallado is not None:
        _REALES[nombre] = hallado
    return hallado


def _buscar_modulo_de_verdad(nombre: str):
    """El módulo `nombre` de verdad, o None si en este proceso solo hay muñecos.

    No se pregunta por `sys.modules[nombre]` y ya: durante la suite completa esa
    entrada es un doble. Se busca por una marca que ningún doble de este repo
    tiene —un `__file__` que sea de verdad un `str`— primero en `sys.modules` y
    después entre las referencias que el código de Lucy ya tiene agarradas, que
    es donde sobrevive el real cuando alguien pisa la entrada global.
    """
    def _sirve(m):
        return (isinstance(m, types.ModuleType)
                and getattr(m, "__name__", None) == nombre
                and isinstance(getattr(m, "__file__", None), str))

    if _sirve(_REALES.get(nombre)):
        return _REALES[nombre]

    directo = sys.modules.get(nombre)
    if _sirve(directo):
        return directo
    for mod in list(sys.modules.values()):
        if not isinstance(mod, types.ModuleType):
            continue
        for attr in list(vars(mod).values()) if hasattr(mod, "__dict__") else []:
            if _sirve(attr):
                return attr
    # Ni en `sys.modules` ni agarrado por nadie: en este proceso el real nunca
    # llegó a importarse porque un muñeco ocupaba su sitio desde el principio.
    # Se trae del disco apartando los muñecos, y se los devuelve a su sitio en
    # el acto: quien decide qué queda en `sys.modules` es `poner_la_puerta`,
    # que sabe restaurarlo, y no esta función.
    #
    # Se aparta la FAMILIA entera y no solo el módulo pedido, porque no son
    # independientes: medido, importar el `psycopg_pool` real con un muñeco en
    # `psycopg` da `ImportError: cannot import name 'errors' from 'psycopg'`.
    familia = nombre.split("_")[0]
    apartados = {n: m for n, m in sys.modules.items()
                 if n == familia or n.startswith(familia)}
    for n in apartados:
        del sys.modules[n]
    try:
        real = importlib.import_module(nombre)
        return real if _sirve(real) else None
    except Exception:                                  # noqa: BLE001
        return None
    finally:
        for n, m in apartados.items():
            sys.modules[n] = m


def _vias_de_conexion(*modulos) -> dict:
    """Los SITIOS donde se instala la puerta, derivados del paquete instalado.

    Ojo con el nombre: esto NO es «toda forma de conseguir una conexión», que es
    lo que decía antes y era falso. Son los sitios donde la puerta se pone. Y la
    puerta se pone CAMBIANDO UN ATRIBUTO mientras el programa corre, así que solo
    la ve quien resuelva ese atributo DESPUÉS. Ver `_ESCAPAN_SI_SE_CAPTURAN` y la
    FRONTERA 4, que es el techo del método y no un caso que falte cubrir.

    DE DÓNDE SALE LA LISTA, porque importa: de `dir()` de cada módulo y de
    `issubclass` contra `psycopg.BaseConnection` y `psycopg.cursor.BaseCursor`.
    NO de nombres ni de prefijos. Si mañana psycopg publica una clase de
    conexión o un pool más, entra sola en la puerta y nadie tiene que acordarse.

    Tres formas, y las tres derivadas. Lo que las separa de verdad es DÓNDE se
    pisa el atributo, porque de eso depende a quién le gana la puerta:

      · `clase-de-conexion` (subclase de `BaseConnection`) → se dobla `connect`
        DENTRO DE LA CLASE. Quien tenga la clase agarrada de antes sigue viendo
        el doble, porque el atributo se resuelve en el momento de llamarlo.
      · `pool` (clase con `.connection` que no es cursor ni conexión) → se dobla
        EN EL MÓDULO. Quien tenga la clase agarrada de antes NO ve el doble.
      · `atajo-de-modulo`: un método YA VINCULADO a una clase de conexión → se
        dobla EN EL MÓDULO, y tampoco lo ve quien lo capturó antes.
        `psycopg.connect` es exactamente esto: medido,
        `psycopg.connect == psycopg.Connection.connect`, un classmethod vinculado.
        Por eso doblar la clase no alcanza y hay que pisar también el atajo.

    Medido el 8-sep-2026: devuelve las OCHO del levantamiento y ninguna de más
    — tres en la clase y cinco en el módulo.
    """
    real = _modulo_de_verdad("psycopg")
    if real is None:
        return {}
    base_con = real.BaseConnection
    base_cur = real.cursor.BaseCursor
    fuera: dict[str, tuple] = {}
    for mod in modulos:
        if mod is None:
            continue
        for nom in dir(mod):
            if nom.startswith("__"):
                continue
            try:
                obj = getattr(mod, nom)
            except Exception:                          # noqa: BLE001
                continue
            etiqueta = f"{mod.__name__}.{nom}"
            if inspect.isclass(obj):
                if issubclass(obj, base_cur):          # un cursor no da conexiones
                    continue
                if issubclass(obj, base_con):
                    fuera[etiqueta] = ("clase-de-conexion", mod, nom, obj)
                elif callable(getattr(obj, "connection", None)):
                    fuera[etiqueta] = ("pool", mod, nom, obj)
            elif (inspect.ismethod(obj)
                  and inspect.isclass(getattr(obj, "__self__", None))
                  and issubclass(obj.__self__, base_con)):
                fuera[etiqueta] = ("atajo-de-modulo", mod, nom, obj)
    return fuera


def _escapan_si_se_capturan() -> set[tuple[str, str]]:
    """Los `(módulo, nombre)` a los que la puerta NO le gana si se capturan antes.

    DERIVADO, no tecleado: son exactamente las vías que `_vias_de_conexion`
    clasifica como `pool` o `atajo-de-modulo`, o sea las que se doblan pisando un
    atributo DEL MÓDULO. Quien se quede con el objeto antes tiene el de verdad y
    ya no vuelve a mirar el módulo nunca más.

    Las `clase-de-conexion` NO entran: ésas se doblan dentro de la clase, así que
    capturar la clase con `from psycopg import Connection` es inofensivo — el
    `.connect` se resuelve al llamarlo y ahí ya está el doble.

    Esto responde QUÉ símbolos escapan, y esa mitad sí es derivada: si psycopg
    publica mañana otro pool o otro atajo de módulo, entra solo acá. La otra
    mitad —CÓMO se busca a quién los capturó— no lo es: el barrido de
    `test_la_captura_por_from_import_esta_declarada` solo lee `from … import …`.
    Un `x = psycopg.connect` captura EL MISMO objeto. Medido el 8-sep-2026:
    con `from psycopg import connect as a` y `b = psycopg.connect`, `a is b` →
    **True**; y tras `tender()`, `psycopg.connect is a` → False y
    `psycopg.connect is b` → False, o sea que la puerta no le llega a ninguno de
    los dos. La diferencia entre las dos formas es de escritura, no de efecto —
    y el barrido solo ve la escritura.
    """
    real = _modulo_de_verdad("psycopg")
    real_pool = _modulo_de_verdad("psycopg_pool")
    return {(mod.__name__, nom)
            for (clase, mod, nom, _obj) in _vias_de_conexion(
                real, real_pool).values()
            if clase in ("pool", "atajo-de-modulo")}


# ── La red de los mensajes: los cuatro sitios por donde se puede salir ────

class _Salida(dict):
    pass


# Marca para «este atributo no existía antes»: al levantar la red hay que
# borrarlo, no dejar puesto un None que después parezca un valor.
_NO_ESTABA = object()


class _RedDeMensajes:
    """Apunta todo intento de mandar algo fuera del proceso, venga por donde venga.

    Cuatro capas, y ninguna es un nombre de función de Lucy:
      1. el `bot` que recibe el turno            (la puerta esperada)
      2. `telegram.Bot`                          (un cliente propio)
      3. `httpx.AsyncClient.send` / `Client.send` (HTTP a pelo)
      4. `socket.socket.connect`                 (cualquier otra librería)

    Cada apunte guarda la pila de llamadas, así que además de CUÁNTOS mensajes
    salieron se sabe si salieron por dentro de la puerta.
    """

    def __init__(self):
        self.salidas: list[_Salida] = []
        self._guardado: list[tuple] = []
        self._guardado_modulos: list[tuple] = []
        self.vias_dobladas: dict[str, str] = {}
        self.motivo_sin_puerta: str | None = "la red todavía no se tendió"
        self.base_cursor = None

    def apuntar(self, via, detalle):
        # `_pila_corta` y no `inspect.stack()`: éste último va al disco a buscar
        # el fuente de cada marco, y dentro de la suite completa hay pruebas que
        # le cambian el `open` por debajo. Acá solo hacen falta los nombres.
        self.salidas.append(
            _Salida(via=via, detalle=detalle, pila=_pila_corta(40)))

    # -- la capa 1: el bot del turno --------------------------------------
    def bot(self):
        red = self

        class _Bot:
            def __init__(self):
                self.enviados = []

            async def send_message(self, text="", **kw):
                self.enviados.append({"text": text,
                                      "markup": kw.get("reply_markup")})
                red.apuntar("el bot del turno", text)
                return types.SimpleNamespace(message_id=1)

        return _Bot()

    # -- las capas 2 a 4 ---------------------------------------------------
    def tender(self):
        red = self

        class _ClientePropio:
            def __init__(self, *a, **k):
                pass

            def __getattr__(self, nombre):
                async def _lo_que_sea(*a, **k):
                    red.apuntar(f"un cliente de telegram propio (.{nombre})",
                                k.get("text") or a)
                    return types.SimpleNamespace(message_id=1)
                return _lo_que_sea

        self._reemplazar(telegram, "Bot", _ClientePropio)

        async def _http_async(self, *a, **k):
            red.apuntar("httpx (async)", a[:1])
            raise OSError("httpx bloqueado: la suite no sale a internet")

        def _http_sync(self, *a, **k):
            red.apuntar("httpx (sync)", a[:1])
            raise OSError("httpx bloqueado: la suite no sale a internet")

        self._reemplazar(httpx.AsyncClient, "send", _http_async)
        self._reemplazar(httpx.Client, "send", _http_sync)

        def _conectar(self, direccion, *a, **k):
            red.apuntar("un socket a pelo", direccion)
            raise OSError("socket bloqueado: la suite no sale a internet")

        self._reemplazar(socket.socket, "connect", _conectar)
        self._reemplazar(socket.socket, "connect_ex", _conectar)

        # Y la puerta del cursor, que es la que cubre las OCHO formas de
        # conseguir una conexión sin nombrar ninguna.
        self.poner_la_puerta()

    # -- la puerta del cursor ---------------------------------------------
    def poner_la_puerta(self) -> None:
        """Dobla el cursor y las ocho vías de conseguir una conexión.

        NO dice «todas», y la palabra importa: la puerta se instala CAMBIANDO UN
        ATRIBUTO con el programa ya corriendo, así que solo alcanza a quien
        resuelva ese atributo después. Quien se quedara antes con la función de
        verdad —`from psycopg import connect`, o `x = psycopg.connect`, que
        capturan el mismo objeto— escribe sin que el libro se entere. Eso es el
        techo del método y está declarado como FRONTERA 4. De las dos formas,
        `test_la_captura_por_from_import_esta_declarada` avisa de UNA: la del
        `from … import …`. La otra no la ve nadie y está dicho allá.

        Deja constancia de si pudo o no en `self.motivo_sin_puerta`: quien mida
        algo con la puerta caída tiene que poder ponerse rojo en vez de dar un
        verde que no significa nada.
        """
        self.vias_dobladas: dict[str, str] = {}
        self.motivo_sin_puerta: str | None = None

        real = _modulo_de_verdad("psycopg")
        real_pool = _modulo_de_verdad("psycopg_pool")
        if real is None or real_pool is None:
            falta = "psycopg" if real is None else "psycopg_pool"
            self.motivo_sin_puerta = (
                f"en este proceso no hay un `{falta}` de verdad: "
                f"sys.modules[{falta!r}] = {sys.modules.get(falta)!r}")
            return

        base_cur = getattr(real.cursor, "BaseCursor", None)
        if not isinstance(base_cur, type):
            self.motivo_sin_puerta = (
                f"`psycopg.cursor.BaseCursor` no es una clase sino "
                f"{base_cur!r}: el módulo responde al nombre pero es un muñeco")
            return

        # Los reales vuelven a `sys.modules` MIENTRAS se mide, para que un
        # `import psycopg` dentro de una herramienta obtenga el que está
        # doblado y no el muñeco que dejó otro archivo de pruebas.
        for nombre, mod in (("psycopg", real), ("psycopg_pool", real_pool)):
            self._guardado_modulos.append((nombre, sys.modules.get(nombre),
                                           nombre in sys.modules))
            sys.modules[nombre] = mod

        # ── EL DOBLE EN `BaseCursor`, Y HASTA DÓNDE LLEGA DE VERDAD ──
        # Acá decía «EL FONDO: si alguna vía se escapara y consiguiera un objeto
        # REAL de psycopg, su ejecución cae igual acá; debajo de esto ya no hay
        # Python al que bajarse». Eso era falso y se corrige con la medición.
        #
        # Medido el 8-sep-2026 sobre el psycopg instalado:
        #     'execute' in vars(psycopg.cursor.BaseCursor) ...... False
        #     'execute' in vars(psycopg.Cursor) ................. True
        #     'execute' in vars(psycopg.AsyncCursor) ............ True
        # y tras poner el doble en `BaseCursor`:
        #     psycopg.Cursor.execute.__qualname__ ....... 'Cursor.execute'
        #     psycopg.AsyncCursor.execute.__qualname__ .. 'AsyncCursor.execute'
        #     psycopg.ServerCursor.execute.__qualname__ . 'ServerCursor.execute'
        #     psycopg.cursor.BaseCursor.execute ......... el doble
        #
        # O sea que `execute` NO está definido en `BaseCursor`: ponerlo acá crea
        # un atributo que las clases concretas TAPAN en el MRO. Un cursor REAL de
        # psycopg no pasa por este doble, así que no hay fondo debajo de las ocho
        # vías: lo que las cubre son los espías que ellas devuelven, y nada más.
        #
        # Lo que este doble sí hace: apuntar lo que le llegue por `BaseCursor` y
        # dejar constancia, en la prueba de la puerta, de que se pudo instalar.
        # Medido quitándolo y corriendo la suite entera: 1 rojo, y es el assert
        # que comprueba que está puesto. Ninguna prueba de comportamiento cambia.
        def _execute(cur, sql, args=None, *a, **k):
            libro = _LIBRO_ACTIVO or _Libro()
            libro.apuntar(getattr(cur, "_bloque_espia", 8000), str(sql), args)
            return cur

        def _executemany(cur, sql, seq=(), *a, **k):
            for args in list(seq) or [None]:
                _execute(cur, sql, args)
            return cur

        self._reemplazar(base_cur, "execute", _execute)
        self._reemplazar(base_cur, "executemany", _executemany)
        self.base_cursor = base_cur

        # ── Las vías de conseguir una conexión, DERIVADAS del paquete ──
        def _conexion_suelta(*a, **k):
            libro = _LIBRO_ACTIVO or _Libro()
            return _ConexionEspia(libro, 9000 + len(libro.sentencias))

        for etiqueta, (clase, mod, nom, obj) in _vias_de_conexion(
                real, real_pool).items():
            if clase == "clase-de-conexion":
                self._reemplazar(obj, "connect", classmethod(
                    lambda cls, *a, **k: _conexion_suelta()))
            elif clase == "pool":
                self._reemplazar(mod, nom,
                                 lambda *a, **k: _PoolEspia(_LIBRO_ACTIVO
                                                            or _Libro()))
            else:                                   # atajo-de-modulo
                self._reemplazar(mod, nom, _conexion_suelta)
            self.vias_dobladas[etiqueta] = clase

    def _reemplazar(self, obj, nombre, nuevo):
        # En una CLASE se guarda lo que hay en su `__dict__`, no lo que devuelve
        # `getattr`: para un `classmethod`, `getattr` ya devuelve el método
        # VINCULADO, y restaurar eso dejaría a las subclases heredando un
        # `connect` atado a la clase madre. `vars()` devuelve el descriptor tal
        # cual, que es lo único que restaura sin cambiar la semántica.
        if inspect.isclass(obj):
            viejo = vars(obj).get(nombre, _NO_ESTABA)
        else:
            viejo = getattr(obj, nombre, _NO_ESTABA)
        self._guardado.append((obj, nombre, viejo))
        setattr(obj, nombre, nuevo)

    def levantar(self):
        for obj, nombre, viejo in reversed(self._guardado):
            if viejo is _NO_ESTABA:
                delattr(obj, nombre)
            else:
                setattr(obj, nombre, viejo)
        self._guardado.clear()
        for nombre, viejo, estaba in reversed(self._guardado_modulos):
            if estaba:
                sys.modules[nombre] = viejo
            else:
                sys.modules.pop(nombre, None)
        self._guardado_modulos.clear()


def _montar_sobre_la_base(libro: _Libro):
    """Deja corriendo el `crud` REAL contra el pool espía. Nada de mocks de crud.

    Lo único de mentira es lo que está fuera del proceso: el modelo y Postgres.
    Las LECTURAS de contexto sí llevan doble —devuelven filas con forma propia
    que el espía no sabe inventar—, pero todas las ESCRITURAS son reales y
    llegan al pool, que es lo que acá se mide.
    """
    global _LIBRO_ACTIVO
    _LIBRO_ACTIVO = libro
    db.pool = _PoolEspia(libro)

    async def _nada(*a, **k):
        return None

    async def _vacio(*a, **k):
        return []

    db.buscar_esperando_respuesta = _nada
    db.ultimos_intercambios = _vacio
    db.listar_preferencias = _vacio

    async def _ejecutar_sql(sql):
        return [{"id": 100 + i, "titulo": TITULOS[i]} for i in range(12)]

    consultar._ejecutar = _ejecutar_sql
    consultar._validar = lambda sql: sql


def test_un_turno_manda_un_solo_mensaje_y_sale_por_la_puerta():
    """MEDIDO CORRIENDO: cuántos mensajes salieron del proceso, y por dónde.

    Ésta es la que ve lo que la guarda del árbol de sintaxis no puede ver. Una
    salida nueva no necesita nombrar `_enviar` ni `bot` para hablarle a Tiziano:
    le basta con armarse su propio cliente. Acá da igual cómo se llame, porque
    lo que lleva doble son los cuatro sitios por los que se sale del proceso.

    Lo que se exige es una sola cosa, y no depende de ningún nombre: **de un
    turno sale UN mensaje**. Dos son un turno que habló por fuera de la puerta.

    HASTA DÓNDE LLEGA, medido el 8-sep-2026 probando a burlarla:

      · CAEN: un cliente de `telegram` armado a mano; el mismo con el nombre del
        método construido al vuelo (`getattr(cli, "send_" + "message")`), que la
        guarda estática no ve; `httpx` a pelo contra api.telegram.org; y
        `urllib.request.urlopen`, que termina en `socket.connect`.
      · NO CAE: lo que no sale por la red — dejar el texto en un archivo, en una
        tabla, o lanzarlo con un subproceso. Eso no es «mandarle un mensaje a
        Tiziano» hasta que alguien lo recoja, y quien lo recoja sí pasará por
        acá.
      · Y esto habla del turno de `atender`, no de todo lo que Lucy dice: el
        despertador tiene sus propios envíos y no es asunto de este archivo.
    """
    red = _RedDeMensajes()
    libro = _Libro()
    bot = red.bot()
    _modelo(_guion(3), {"consumidos": 0})   # el bot lo pone la red
    _montar_sobre_la_base(libro)
    fila = {"id": 77, "chat_id": config.CHAT_ID_DUENO, "telegram_msg_id": 9,
            "tipo_entrada": "texto"}
    red.tender()
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            agente.atender(fila, "ya hice todo", bot))
    finally:
        red.levantar()

    assert len(red.salidas) == 1, (
        "de un turno tiene que salir UN mensaje; salieron "
        f"{len(red.salidas)}: "
        + " | ".join(f"{s['via']}: {str(s['detalle'])[:60]}"
                     for s in red.salidas))
    unica = red.salidas[0]
    assert unica["via"] == "el bot del turno", (
        f"el mensaje salió por «{unica['via']}», no por el bot del turno")
    assert "_fin_del_turno" in unica["pila"], (
        f"el mensaje salió sin pasar por la puerta; pila: {unica['pila'][:8]}")


def _resolver(x):
    """El valor, esperándolo si hace falta. Sirve para las vías sync y async."""
    if inspect.isawaitable(x):
        return asyncio.get_event_loop_policy().new_event_loop(
            ).run_until_complete(_esperar(x))
    return x


async def _esperar(x):
    return await x


def test_la_puerta_del_cursor_esta_puesta_y_se_demuestra_mandandole_una_sentencia():
    """⬛ LA PUERTA PRUEBA QUE ESTÁ PUESTA, O ESTO SE PONE ROJO.

    Es la mitad que hace válido el diseño, y la que faltaba en las vueltas
    anteriores. Una guarda que se instala sobre un muñeco mide CERO y lo informa
    como verde: no distingue «vi todo y no había nada» de «nadie me enseñó
    nada». Acá eso no se puede: si la puerta no se puede DEMOSTRAR, sale roja.

    Y no se demuestra leyendo el archivo ni preguntando si un nombre existe
    —medido: dentro de la suite completa `hasattr(psycopg.cursor, "BaseCursor")`
    devuelve True siendo un atrapa-todo—. Se demuestra CORRIENDO: por cada vía
    de conseguir una conexión se manda una sentencia centinela y se exige verla
    aparecer en el libro. La vía que no la enseñe se nombra y pone esto rojo.

    DE DÓNDE SALE LA LISTA DE VÍAS: de `dir()` de `psycopg` y `psycopg_pool` y
    de `issubclass` contra `BaseConnection` / `BaseCursor` — no de nombres ni de
    prefijos. Una clase de conexión o un pool nuevos entran solos.
    """
    global _LIBRO_ACTIVO
    red = _RedDeMensajes()
    libro = _Libro()
    _LIBRO_ACTIVO = libro
    red.tender()
    try:
        assert red.motivo_sin_puerta is None, (
            "la puerta del cursor NO se pudo poner, así que cualquier medición "
            f"de escrituras de este archivo vale cero: {red.motivo_sin_puerta}")

        # 1. El doble está en el cursor, y `BaseCursor` es una CLASE de verdad.
        #    `isinstance(..., type)` y no `hasattr`: un atrapa-todo pasa hasattr.
        assert isinstance(red.base_cursor, type), (
            f"`BaseCursor` no es una clase sino {red.base_cursor!r}")
        assert "_execute" in getattr(
            vars(red.base_cursor).get("execute"), "__name__", ""), (
            "`BaseCursor.execute` no quedó doblado: lo que hay es "
            f"{vars(red.base_cursor).get('execute')!r}")

        # 2. Están las OCHO vías, y ninguna se quedó fuera.
        assert len(red.vias_dobladas) >= 8, (
            f"solo se doblaron {sorted(red.vias_dobladas)}. Si psycopg dejó de "
            f"exponer sus conexiones así, esta prueba dejó de ver las vías y "
            f"hay que rehacerla, no bajar el número")

        # 3. LA DEMOSTRACIÓN: una sentencia por vía, y hay que verla.
        ciegas, ejercitadas = [], []
        for etiqueta, clase in sorted(red.vias_dobladas.items()):
            mod_nom, _, attr = etiqueta.partition(".")
            obj = getattr(sys.modules[mod_nom], attr)
            centinela = f"UPDATE tareas SET centinela = '{etiqueta}'"
            antes = len(libro.sentencias)
            try:
                if clase == "pool":
                    with obj("postgresql://centinela").connection() as con:
                        con.execute(centinela)
                elif clase == "clase-de-conexion":
                    _resolver(obj.connect("postgresql://centinela")
                              ).execute(centinela)
                else:                                    # atajo-de-modulo
                    _resolver(obj("postgresql://centinela")).execute(centinela)
            except Exception as e:                       # noqa: BLE001
                ciegas.append(f"{etiqueta} ({clase}): reventó al usarla — "
                              f"{type(e).__name__}: {e}")
                continue
            visto = [s for s in libro.sentencias[antes:]
                     if etiqueta in str(s["sql"])]
            if not visto:
                ciegas.append(f"{etiqueta} ({clase}): la sentencia salió y el "
                              f"libro no la vio — por ahí se escribe a ciegas")
            else:
                ejercitadas.append(etiqueta)

        assert not ciegas, (
            "hay vías de llegar a Postgres que la puerta NO ve. Todo lo que "
            "este archivo mida sobre escrituras es un verde sin valor mientras "
            "esto siga así:\n  " + "\n  ".join(ciegas))

        assert len(ejercitadas) == len(red.vias_dobladas), (
            f"se doblaron {len(red.vias_dobladas)} vías y solo se demostraron "
            f"{len(ejercitadas)}: {sorted(ejercitadas)}")
    finally:
        red.levantar()
        _LIBRO_ACTIVO = None

    # 4. Y al levantar la red, psycopg queda como estaba. Una puerta que se deja
    #    puesta le cambia el mundo a las 452 pruebas que vienen detrás.
    assert "_execute" not in getattr(
        vars(red.base_cursor).get("execute"), "__name__", ""), (
        "la puerta se quedó puesta después de levantar la red")


def test_ninguna_escritura_llega_a_la_base_sin_su_huella():
    """LA GUARDA DE VERDAD SOBRE LAS ESCRITURAS: un doble en el pool, y a correr.

    `db.pool` es el único camino a Postgres que hay en este proceso. Con un
    espía ahí se ve CADA sentencia con su texto ya armado — se llame como se
    llame quien la mandó, la haya escrito con `crud.`, con `db.`, con un alias,
    o con `db.pool.connection()` a pelo, que es la forma que se saltaba las dos
    guardas de arriba a la vez.

    Y las herramientas que se recorren NO están escritas acá: salen del árbol de
    sintaxis de `_ejecutar_herramienta` (`if nombre == "..."`), y los argumentos
    con que se las llama salen de los `args.get("...")` de cada rama. Una
    herramienta nueva entra sola en el barrido y se ejecuta de verdad, aunque el
    guion del modelo no la pida nunca.

    LO QUE SE EXIGE, dos cosas y las dos medidas corriendo:

      1. toda escritura contra una tabla de `crud.TABLAS` deja una huella en
         `log_acciones` dentro del mismo bloque de conexión — sin huella no hay
         nada que deshacer;
      2. toda huella CON ASA (o sea, las que no son un `deshacer`) sale en el
         parte. Dejar rastro y callárselo también deja a Tiziano sin saber qué
         pasó y al botón sin qué revertir.

    HASTA DÓNDE LLEGA, medido el 8-sep-2026 y no tapado:

      · Una rama a la que los argumentos genéricos no le alcanzan para llegar a
        escribir NO SE MIDE: sale de acá sin veredicto, no aprobada. Ese día el
        barrido halló 12 herramientas y 7 llegaron a escribir; las otras 5
        —consultar, buscar_lugar, viaje, correo, recordar— no escriben en
        ninguna tabla de dominio, así que la cobertura sobre las que escriben
        era completa. El piso de abajo existe para que eso no se degrade en
        silencio.
      · La red se instala en las OCHO formas de conseguir una conexión, derivadas
        del paquete instalado y no de una lista escrita acá, y que la puerta esté
        de verdad puesta lo demuestra corriendo
        `test_la_puerta_del_cursor_esta_puesta_...`, mandando una sentencia
        centinela por cada una. **Cubre las ocho para quien las llame por el
        atributo** —`psycopg.connect(...)`, `psycopg_pool.ConnectionPool(...)`—,
        que es como se escribe. NO cubre a quien se haya quedado con el símbolo
        de antes; eso es la FRONTERA 4 de abajo.
      · El «mismo bloque de conexión» es la unidad. Una escritura y su huella
        repartidas en dos bloques distintos saldrían rojas aunque el efecto
        final fuera correcto; hoy `crud` no lo hace nunca.

    LA FRONTERA, corregida dos veces el 8-sep-2026.

    La PRIMERA corrección quitó un «lo que queda fuera es escribir sin usar
    Python» que era falso: medido, TRES formas en Python puro salían verdes
    —`psycopg.connect` síncrono, el pool síncrono y `psycopg.Connection.connect`—
    y una la usa el propio repo en `db/backup.py:380`. Esas tres ya están
    cubiertas: `guarda vieja → 0 de 3 atrapadas; guarda nueva → 3 de 3`.

    La SEGUNDA corrección es ésta, y no es un caso que faltara: es EL TECHO DEL
    MÉTODO, y por eso se declara en vez de perseguirse. La puerta se instala
    cambiando un atributo con el programa ya corriendo. Cualquiera que haya hecho
    `from psycopg import connect` ANTES se quedó con la función de verdad y le
    gana por llegar primero. Perseguir eso caso por caso no termina nunca; lo
    honesto es decir hasta dónde llega la puerta y poner guardia en la frontera.

    Así que lo que queda fuera son CINCO cosas, y las cuatro primeras NO se
    confían: tienen prueba propia que las pone rojas solas.

      1. UN SUBPROCESO. Hoy hay exactamente uno que habla con Postgres,
         `db/backup.py` (`pg_dump --schema-only`), y solo lee.
         → lo vigila `test_ningun_subproceso_nuevo_le_habla_a_postgres`.
      2. OTRO DRIVER. Hoy no hay ninguno además de psycopg.
         → lo vigila `test_no_entro_otro_driver_de_postgres_por_la_puerta_de_atras`.
      3. LAS TABLAS QUE NO SE VIGILAN. `db/schema.sql` declara 16 tablas y
         `crud.TABLAS` tiene 8: una escritura a las otras 9 se VE pero no se
         juzga. Queda declarado en
         `test_la_frontera_de_las_tablas_vigiladas_esta_declarada`.
      4. UN SÍMBOLO CAPTURADO ANTES DE QUE LA PUERTA SE INSTALE. Medido el
         8-sep-2026 por identidad de objetos: tras `tender()`,
         `psycopg.connect is capturado_antes` → **False**, y
         `capturado_antes.__module__` sigue siendo `psycopg.connection`. Se
         escribió por ahí y el libro anotó **0 sentencias**; la capa de socket de
         `_RedDeMensajes` tampoco lo vio (**0 apuntes**), porque psycopg abre el
         zócalo en C y no pasa por `socket.socket.connect`. O sea que por esa vía
         se escribe a ciegas del todo. Solo escapan los símbolos que la puerta
         pisa EN EL MÓDULO (`pool` y `atajo-de-modulo`, cinco hoy); capturar una
         clase de conexión es inofensivo porque el doble va dentro de la clase.
         Y no es rebuscado: el propio repo importa así en `db/db.py:17`.
         → De esto AVISA UNA FORMA Y NO TODAS, y ésa es la diferencia con las
           tres de arriba. `test_la_captura_por_from_import_esta_declarada` lee
           el árbol de sintaxis buscando `from psycopg import …` /
           `from psycopg_pool import …`, y nada más. Una captura por asignación
           de atributo —`x = psycopg.connect`, `self._c = psycopg.connect`,
           `getattr(psycopg, "connect")`— no la ve, y captura EL MISMO objeto:
           medido, `from psycopg import connect as a` y `b = psycopg.connect`
           dan `a is b` → True. Se dejó así a propósito en la séptima vuelta:
           ampliar el barrido a las asignaciones traería la octava forma
           (`getattr`, un diccionario, un `functools.partial`, un decorador),
           porque el barrido mira CÓMO ESTÁ ESCRITO el código y no lo que hace.
           Lo que queda cubierto es una forma real y frecuente; lo que queda
           fuera está escrito acá para que nadie lo descubra otra vez.
      5. `Copy.write` / `write_row`, que manda datos después de un `COPY` que el
         cursor sí vio. Hoy el repo no usa `copy` fuera de `tests/`.
    """
    fn = _nodo("_ejecutar_herramienta")

    # Las herramientas, sacadas del código: `if nombre == "..."`.
    ramas: dict[str, ast.If] = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        for cmp_ in ast.walk(n.test):
            if (isinstance(cmp_, ast.Compare)
                    and isinstance(cmp_.left, ast.Name)
                    and cmp_.left.id == "nombre"
                    and isinstance(cmp_.ops[0], ast.Eq)
                    and isinstance(cmp_.comparators[0], ast.Constant)
                    and isinstance(cmp_.comparators[0].value, str)):
                ramas.setdefault(cmp_.comparators[0].value, n)

    assert len(ramas) >= 10, (
        f"el barrido solo encontró {sorted(ramas)}: si `_ejecutar_herramienta` "
        f"dejó de despachar con `if nombre == \"...\"`, esta prueba dejó de ver "
        f"las herramientas y hay que rehacerla, no aflojarla")

    def _claves_de(rama: ast.If) -> set[str]:
        """Los argumentos con que se puede llamar a esa rama, según el código.

        Dos fuentes, las dos derivadas y ninguna escrita acá:
          · los `args.get("...")` de la propia rama;
          · los `.get("...")` de las funciones de `crud` que la rama llama —
            hacen falta porque varias ramas le pasan el diccionario entero
            (`dict(args)`) y las claves que de verdad importan se leen allá.
        """
        claves = set()
        for n in ast.walk(rama):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("get", "pop")
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "args"
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                claves.add(n.args[0].value)
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "crud"):
                fn_crud = getattr(crud, n.func.attr, None)
                try:
                    cuerpo = inspect.getsource(fn_crud)
                except (OSError, TypeError):
                    continue
                claves |= set(re.findall(r"\.get\(\"([a-z_]+)\"", cuerpo))
        return claves

    def _envoltorio(rama: ast.If, clave: str) -> str:
        """¿Ese argumento se usa como número, como diccionario, como lista?"""
        for n in ast.walk(rama):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("int", "float", "dict", "list", "str")):
                continue
            for dentro in ast.walk(n):
                if (isinstance(dentro, ast.Call)
                        and isinstance(dentro.func, ast.Attribute)
                        and dentro.func.attr == "get"
                        and dentro.args
                        and isinstance(dentro.args[0], ast.Constant)
                        and dentro.args[0].value == clave):
                    return n.func.id
        return "str"

    # Valores con los que se prueba a llamar cada rama. No son el criterio de la
    # guarda: son la munición. Si una rama nueva no llega a escribir con
    # ninguno, sale del barrido SIN veredicto (y el piso de abajo lo canta).
    CANDIDATOS = [100, "tareas", {"estado": "hecha"}, ["tareas"], "hecha",
                  "persona", "guardar", "tarea"]

    def _bundles(rama: ast.If) -> list[dict]:
        claves = _claves_de(rama)
        if not claves:
            return [{}]
        mixto = {}
        for c in claves:
            env = _envoltorio(rama, c)
            mixto[c] = {"int": 100, "float": 1.0, "dict": {"estado": "hecha"},
                        "list": ["tareas"]}.get(env, "tareas")
        return [mixto] + [{c: v for c in claves} for v in CANDIDATOS]

    red = _RedDeMensajes()
    red.tender()                       # nada de esto sale a internet
    escribieron, mudas, veredicto, exentas = [], [], {}, set()
    sin_parte: list[str] = []
    try:
        for herramienta, rama in sorted(ramas.items()):
            escribio = False
            for args in _bundles(rama):
                libro = _Libro()
                _montar_sobre_la_base(libro)
                acciones: list[dict] = []
                try:
                    asyncio.get_event_loop_policy().new_event_loop(
                        ).run_until_complete(agente._ejecutar_herramienta(
                            herramienta, dict(args), 77, acciones))
                except Exception as e:            # noqa: BLE001
                    veredicto[herramienta] = f"reventó: {type(e).__name__}: {e}"
                    continue
                if libro.escrituras_de_dominio():
                    escribio = True
                for s in libro.mudas():
                    mudas.append(f"{herramienta}: {s['verbo']} {s['tabla']} "
                                 f"— «{s['sql'][:80]}»")
                exentas |= libro.exentas_que_dispararon
                # Dejar huella no alcanza: si el asa no llega al parte, Tiziano
                # no ve lo que se hizo y el botón no lo revierte.
                if len(libro.asas()) != len(acciones):
                    sin_parte.append(
                        f"{herramienta}: dejó {len(libro.asas())} huella(s) con "
                        f"asa y anotó {len(acciones)} en el parte")
            if escribio:
                escribieron.append(herramienta)
    finally:
        red.levantar()

    assert not mudas, (
        "escribieron en la base sin dejar huella en log_acciones, así que lo "
        "que hicieron no se cuenta en el parte ni se puede deshacer con el "
        "botón:\n  " + "\n  ".join(sorted(set(mudas))))

    assert not sin_parte, (
        "dejaron una huella con asa que NO llega al parte, así que Tiziano no "
        "ve lo que se hizo y el botón no lo revierte:\n  "
        + "\n  ".join(sorted(set(sin_parte))))

    assert len(escribieron) >= 7, (
        f"solo {escribieron} llegaron a escribir de verdad en el barrido "
        f"(el 8-sep-2026 eran archivar, crear, deshacer, editar, lugar, "
        f"perfil y preferencia — siete). Si el número baja, la prueba pasa en "
        f"verde sin haber ejercitado nada: hay que arreglar los argumentos "
        f"genéricos, no bajar el piso. Ramas que reventaron: {veredicto}")

    # Una exención que ya no dispara es una exención muerta, y una lista de
    # perdones muertos es cómo se cuela el siguiente. Si esto se pone rojo,
    # se BORRA la línea de EXENTAS_DE_HUELLA, no se afloja la comprobación.
    assert exentas == set(EXENTAS_DE_HUELLA), (
        f"declaradas exentas de dejar huella: {sorted(EXENTAS_DE_HUELLA)}; "
        f"en el barrido solo escribieron {sorted(exentas)}. Las que sobran ya "
        f"no perdonan nada y tienen que salir de la lista")


def test_lo_que_dice_el_parte_es_exactamente_lo_que_llego_a_la_base():
    """De punta a punta, con el `crud` REAL y un espía en el pool.

    Las pruebas de arriba miden el parte con `crud.editar` sustituido por un
    doble: eso comprueba el camino del agente, no el de la base. Acá el `crud`
    es el de verdad y lo único de mentira es Postgres, así que se puede exigir
    la igualdad que sostiene todo el diseño:

        UPDATEs que llegaron a la base == huellas en log_acciones == asas del parte

    Si una herramienta escribe y se guarda el asa para sí misma, los tres
    números dejan de coincidir y esto se pone rojo.
    """
    red = _RedDeMensajes()
    libro = _Libro()
    bot = red.bot()
    _modelo(_guion(11), {"consumidos": 0})
    _montar_sobre_la_base(libro)
    fila = {"id": 77, "chat_id": config.CHAT_ID_DUENO, "telegram_msg_id": 9,
            "tipo_entrada": "texto"}
    red.tender()
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            agente.atender(fila, "ya hice todo", bot))
    finally:
        red.levantar()

    escrituras = libro.escrituras_de_dominio()
    huellas = libro.huellas()
    assert len(escrituras) == 11, (
        f"llegaron {len(escrituras)} escrituras de dominio a la base, no 11: "
        f"{[(s['verbo'], s['tabla']) for s in escrituras]}")
    assert len(huellas) == 11, f"{len(huellas)} huellas para 11 escrituras"
    assert not libro.mudas()

    mensaje = bot.enviados[-1]
    assert _botones_de(mensaje) == ["undt:77"]
    for s in escrituras:
        assert s["tabla"] == "tareas", s


# ═════════════════════════════════════════════════════════════════════════
# LA FRONTERA DE LA PUERTA: lo que queda fuera, declarado y con guardia
# ═════════════════════════════════════════════════════════════════════════
#
# Una guarda que dice honestamente hasta dónde llega vale más que una que
# promete todo y tiene un agujero que nadie ha buscado todavía. Estas cuatro
# prueban las salidas 1, 2, 3 y 4 de la frontera; la 5 (`Copy.write`) se declara
# en el docstring de arriba y hoy no tiene uso en el repo fuera de `tests/`.
#
# La 4 es distinta de las otras tres y conviene decirlo dos veces, porque acá
# estuvo escrito de más:
#
#   · Las tres primeras vigilan algo que HOY NO PASA y que sería un error si
#     pasara. La 4 vigila algo que YA PASA una vez, a propósito y sin arreglo
#     previsto, porque es el techo del método.
#   · Y decía «lo que la prueba impide es que aparezca la SEGUNDA sin que nadie
#     se entere». Eso prometía de más. La prueba de la 4 avisa de la SEGUNDA
#     ESCRITA CON `from … import …`, que es la única forma que su barrido lee.
#     Una segunda escrita `x = psycopg.connect` aparece sin que nadie se entere:
#     medido el 8-sep-2026 sobre una copia, archivo nuevo con esa forma →
#     `tests/test_cerrar_varias.py` da 24 passed, 0 rojos. Sobre la suite entera
#     da 1 rojo, y NO cuenta como detección: es el censo de archivos de
#     `tests/test_buzon_que_no_se_ve.py`, que se pone rojo por CUALQUIER `.py`
#     nuevo — comprobado con un archivo trivial sin psycopg, mismo rojo. Cuenta
#     que el archivo existe, no lo que hace.
#     La forma cubierta es real y frecuente; la que falta está declarada, no
#     tapada.

_RAIZ_REPO = pathlib.Path(__file__).resolve().parent.parent

# LISTA TECLEADA, y se dice: son los ejecutables de PostgreSQL que sirven para
# escribir en la base sin pasar por psycopg. No hay forma de derivarla del
# sistema —depende de qué haya instalado en la máquina, no del repo—, así que
# vale lo que valga la lista. Lo que SÍ es derivado es dónde se busca: todos los
# `.py` que hay en el disco, no un puñado de archivos nombrados a mano.
_HERRAMIENTAS_DE_POSTGRES = ("psql", "pg_dump", "pg_dumpall", "pg_restore",
                             "pgbench", "createdb", "dropdb", "vacuumdb",
                             "reindexdb", "clusterdb", "pg_isready")

# El único subproceso que hoy habla con Postgres, y solo LEE. Enumerado a
# propósito: al cubo indulgente no se puede llegar por olvido.
_SUBPROCESOS_PERMITIDOS = {("db/backup.py", "pg_dump")}


def _py_del_repo() -> list[pathlib.Path]:
    """Todos los `.py` que hay en el disco bajo el repo. Derivado, no tecleado.

    Sin esto la vigilancia sería una lista de archivos escrita a mano, que es
    exactamente el defecto que este archivo existe para quitar: un archivo nuevo
    entraría sin que nadie lo mirara.
    """
    return [p for p in _RAIZ_REPO.rglob("*.py")
            if not any(parte in (".venv", "venv", "site-packages", ".git",
                                 "node_modules", "__pycache__")
                       for parte in p.parts)]


def test_ningun_subproceso_nuevo_le_habla_a_postgres():
    """FRONTERA 1: un subproceso no pasa por el cursor, así que se vigila aparte.

    La puerta del cursor ve todo lo que sale por psycopg. Un `subprocess` que
    llame a `psql` no pasa por ahí y escribiría sin que nadie lo viera. Hoy hay
    exactamente uno y solo lee; si aparece otro, esto se pone rojo y alguien
    tiene que decidir, en vez de enterarse después.
    """
    hallados = set()
    for archivo in _py_del_repo():
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(arbol):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "subprocess"):
                continue
            for dentro in ast.walk(n):
                if not (isinstance(dentro, ast.Constant)
                        and isinstance(dentro.value, str)):
                    continue
                # El nombre del ejecutable, no una frase que lo mencione: se
                # mira el primer trozo del literal, que es como se pasa en una
                # lista de argumentos.
                if dentro.value.split("/")[-1] in _HERRAMIENTAS_DE_POSTGRES:
                    rel = archivo.relative_to(_RAIZ_REPO).as_posix()
                    hallados.add((rel, dentro.value.split("/")[-1]))

    nuevos = hallados - _SUBPROCESOS_PERMITIDOS
    assert not nuevos, (
        "hay subprocesos que le hablan a Postgres y NO están declarados. Un "
        "subproceso no pasa por el cursor, así que lo que escriba no lo ve "
        "ninguna guarda de este archivo:\n  "
        + "\n  ".join(f"{a}: {h}" for a, h in sorted(nuevos))
        + "\n  Si es legítimo y solo lee, va a _SUBPROCESOS_PERMITIDOS con su "
          "motivo. Si escribe, hay que decidir qué se hace, no aflojar esto.")

    muertos = _SUBPROCESOS_PERMITIDOS - hallados
    assert not muertos, (
        f"declarados permitidos pero ya no existen: {sorted(muertos)}. Un "
        f"perdón muerto es cómo se cuela el siguiente: se BORRA la línea.")


def test_no_entro_otro_driver_de_postgres_por_la_puerta_de_atras():
    """FRONTERA 2: la puerta es de psycopg. Otro driver no pasaría por ella.

    Se lee de `requirements.txt`, no de la memoria de nadie. Hoy el archivo
    declara `psycopg[binary]` y `psycopg-pool` y ningún otro driver; el día que
    entre uno, esto se pone rojo — porque la puerta del cursor no lo vería y
    todo lo que este archivo mide dejaría de valer sin avisar.
    """
    # LISTA TECLEADA, y se dice: son los otros drivers de Postgres para Python
    # que se usan hoy. No se puede derivar del repo, porque justamente lo que se
    # busca es algo que todavía no está. Vale lo que valga la lista.
    OTROS_DRIVERS = ("asyncpg", "sqlalchemy", "pg8000", "aiopg", "psycopg2",
                     "psycopg2-binary", "pygresql", "py-postgresql", "pgdb",
                     "postgres", "databases", "peewee", "tortoise-orm")

    req = (_RAIZ_REPO / "requirements.txt").read_text(encoding="utf-8")
    declarados = set()
    for linea in req.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        # El nombre del paquete, sin extras ni versión.
        nombre = re.split(r"[\[<>=!;\s]", linea, 1)[0].strip().lower()
        if nombre:
            declarados.add(nombre)

    intrusos = declarados & set(OTROS_DRIVERS)
    assert not intrusos, (
        f"`requirements.txt` declara {sorted(intrusos)}, que habla con Postgres "
        f"sin pasar por psycopg. La puerta del cursor NO lo ve, así que todo lo "
        f"que este archivo mide sobre escrituras dejó de valer: hay que "
        f"extender la puerta o declarar la frontera de nuevo.")

    assert any(d.startswith("psycopg") for d in declarados), (
        f"`requirements.txt` ya no declara psycopg ({sorted(declarados)}): si "
        f"el driver cambió, esta puerta entera está midiendo otra cosa")


def test_la_frontera_de_las_tablas_vigiladas_esta_declarada():
    """FRONTERA 3: la puerta VE todas las tablas; la guarda solo JUZGA ocho.

    Medido el 8-sep-2026: una herramienta que hace `UPDATE categorias_aprendidas`
    o `DELETE FROM cuentas_propias` sale VERDE. El espía ve la sentencia y la
    prueba no la juzga, porque `_Libro.escrituras_de_dominio` filtra por
    `crud.TABLAS`. Puede estar bien —esas tablas no son acciones que Tiziano
    deshaga con un botón— pero hasta hoy no estaba dicho en ninguna parte, y una
    frontera callada es la que mañana alguien cruza creyendo que estaba cubierta.

    Los dos números salen de lo real: del esquema en disco y de `crud.TABLAS`.
    Si el reparto se mueve, esto se pone rojo para que alguien lo mire.
    """
    declaradas = set(db.columnas_declaradas())
    vigiladas = set(crud.TABLAS)

    assert vigiladas <= declaradas, (
        f"`crud.TABLAS` nombra tablas que el esquema no declara: "
        f"{sorted(vigiladas - declaradas)}")

    # 16 y no 17: `grep -c "CREATE TABLE" db/schema.sql` da 17 porque una de las
    # apariciones está DENTRO de un comentario (línea 353). El número sale de
    # `columnas_declaradas()`, que parsea, no de contar líneas que casan.
    sin_juzgar = declaradas - vigiladas
    assert len(declaradas) == 16 and len(vigiladas) == 8, (
        f"el reparto de tablas cambió: el esquema declara {len(declaradas)} y "
        f"`crud.TABLAS` vigila {len(vigiladas)} (el 8-sep-2026 eran 16 y 8). "
        f"Las que quedan sin juzgar serían {sorted(sin_juzgar)}. No se afloja "
        f"este número: se decide si las nuevas entran en la vigilancia y se "
        f"actualiza la frontera.")

    # Las ocho que la puerta VE y la guarda NO JUZGA, enumeradas. `log_acciones`
    # está acá porque no es una tabla de dominio: es donde viven las huellas, y
    # se la mira aparte (`_Libro.huellas`). `backups` es donde escribe
    # `db/backup.py:360`, que es legítimo y por eso sigue verde.
    assert sin_juzgar == {
        "backups", "bandeja", "categorias_aprendidas", "consumos_estado",
        "correo_estado", "correo_reportado", "cuentas_propias", "log_acciones",
    }, (f"cambió qué tablas quedan fuera del juicio de esta guarda: "
        f"{sorted(sin_juzgar)}. Una escritura a cualquiera de ellas se VE pero "
        f"no se exige que deje huella ni que salga en el parte.")


# Los sitios del repo que se quedan con un símbolo de psycopg ANTES de que la
# puerta se instale, ESCRITO `from psycopg… import …`, y que por eso escriben sin
# que el libro los vea. DECLARADOS UNO POR UNO: al cubo indulgente no se llega
# por olvido, y un sitio que no esté acá pone la prueba de abajo roja aunque sea
# inofensivo.
#
# ⚠️ EL TRINQUETE, dicho donde está la lista porque es donde se usa. Agregar un
# sitio a esta lista es MÁS BARATO que arreglarlo: son UNA línea contra reescribir
# el módulo para que llame por el atributo. Medido el 8-sep-2026 sobre una copia:
# un archivo nuevo con `from psycopg import connect` da 1 rojo; agregándolo acá y
# sin tocar una coma de ese archivo, 24 passed. La escritura a ciegas sigue viva.
# Así que: **quien agregue un sitio a esta lista está decidiendo que ese sitio
# escribe a ciegas** — sin marca en el parte y sin poder deshacerse con el botón.
# No es un trámite para poner verde la suite; es esa decisión, y se hereda a quien
# venga detrás. La forma de NO tomarla es dejar el módulo de por medio: `import
# psycopg` y llamar `psycopg.connect(...)`.
#
# Reproducir esta lista, sin correr la suite y sin creerle a nadie. Fíjate en el
# `isinstance(n, ast.ImportFrom)`: eso, y no otra cosa, es todo lo que se busca.
#
#   "../pruebas-confiables/.venv/bin/python3" -c "$(cat <<'PY'
#   import ast, pathlib
#   for p in sorted(pathlib.Path('.').rglob('*.py')):
#       if {'.venv','venv','site-packages','.git','node_modules','__pycache__'} & set(p.parts):
#           continue
#       for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
#           if isinstance(n, ast.ImportFrom) and (n.module or '').split('.')[0] in ('psycopg','psycopg_pool'):
#               for a in n.names:
#                   print(f'{p}:{n.lineno}: from {n.module} import {a.name}')
#   PY
#   )"
#
# El 8-sep-2026 ese comando da OCHO líneas: siete son `from psycopg.rows import
# dict_row`, que no es una vía de conexión y no escapa de nada, y la octava es la
# única declarada acá.
_CAPTURAS_DECLARADAS = {
    # `db/db.py:17`. Se queda con la clase del pool y con ella construye `db.pool`
    # en la línea 51, a la hora de importar el módulo. En la suite esto no hace
    # daño porque `_montar_sobre_la_base` pisa `db.pool` entero con `_PoolEspia`,
    # que es un nivel más abajo que la puerta; pero el símbolo está capturado y
    # la puerta no le llega. Se declara para que se vea, no porque esté bien.
    ("db/db.py", "psycopg_pool", "AsyncConnectionPool"),
}


def test_la_captura_por_from_import_esta_declarada():
    """UNA FORMA CONCRETA AVISA: `from psycopg… import …` antes de la puerta.

    El nombre dice lo que hace, y antes decía de más. Se llamaba
    `test_la_frontera_de_los_simbolos_capturados_esta_declarada` y se leía como
    si la FRONTERA 4 entera estuviera vigilada. No lo está: lo que esta prueba
    lee es UNA forma de escribir la captura.

    POR QUÉ EXISTE. La puerta se pone cambiando un atributo con el programa ya
    arrancado, así que cualquiera que se quedara antes con el símbolo le gana por
    llegar primero. Eso no es un caso que falte cubrir: es el techo del método.

    QUÉ VE Y QUÉ NO, medido el 8-sep-2026 sobre una copia fuera del repo:

      · VE `from psycopg import connect` en un archivo nuevo → 1 rojo, con el
        archivo y la línea en el mensaje.
      · NO VE `x = psycopg.connect` en un archivo nuevo → 24 passed, 0 rojos en
        este archivo. (Sobre la suite entera sale 1 rojo, pero es el censo de
        archivos de `tests/test_buzon_que_no_se_ve.py`, que salta por cualquier
        `.py` nuevo: comprobado con un archivo trivial sin psycopg, mismo rojo.
        No es una detección de la captura.)
        Y no es un caso distinto: `from psycopg import connect as a` y
        `b = psycopg.connect` capturan EL MISMO objeto (`a is b` → True), así que
        lo que escriba por ahí es igual de invisible para el libro. Tampoco ve
        `getattr(psycopg, "connect")`, `self._c = psycopg.connect`, ni nada que no
        sea un `ast.ImportFrom`.

    POR QUÉ NO SE PERSIGUE LA FORMA QUE FALTA. Ésta es la séptima vuelta sobre la
    misma guarda y cada una encontró una forma nueva. Todas comparten defecto:
    miran CÓMO ESTÁ ESCRITO el código en vez de lo que hace. Ampliar el barrido a
    las asignaciones traería la octava (`getattr`, un diccionario, un
    `functools.partial`, un decorador) y eso no termina. Se decidió dejar de
    prometer en vez de perseguir: la forma cubierta es real y frecuente —el propio
    repo importa así en `db/db.py:17`—, y la que falta queda escrita acá.

    QUÉ SE DERIVA Y QUÉ SE TECLEA, sin adornos:
      · CUÁLES símbolos escapan → DERIVADO. `_escapan_si_se_capturan()`, de
        `dir()` + `issubclass` sobre el paquete instalado. Un pool nuevo de
        psycopg entra solo.
      · EN QUÉ ARCHIVOS se busca → DERIVADO. Todos los `.py` que hay en el disco
        bajo el repo (`_py_del_repo`), no un puñado nombrados a mano.
      · CÓMO se busca → **TECLEADO**, y es lo que limita todo lo demás: un solo
        `isinstance(n, ast.ImportFrom)`. Un símbolo nuevo entra solo en la lista
        de arriba, pero solo se lo encuentra si alguien lo escribió con esa forma.
      · QUÉ SE PERDONA → TECLEADO. `_CAPTURAS_DECLARADAS`, en el sentido estricto
        —lo no declarado se pone rojo—, que es el único reparto al que no se llega
        por olvido. Y agregar un sitio ahí es más barato que arreglarlo: el
        trinquete está declarado arriba, junto a la lista.

    ¿Y EL TECHO ES DE VERDAD UN TECHO? Medido el 8-sep-2026, y la respuesta es
    que NO del todo — pero la salida no es gratis y por eso no se tomó acá:

      · `conftest.py` SÍ corre antes de que los módulos de prueba importen
        `db/db.py`. Comprobado poniendo un centinela en `psycopg_pool` desde
        `conftest.py`: `db/db.py` lo capturó (`db.AsyncConnectionPool` era el
        centinela, y la línea 51 reventó al instanciarlo). O sea que una puerta
        instalada ahí SÍ le ganaría a la captura de `db/db.py:17`.
      · PERO cuesta. Instalar cualquier cosa con forma de psycopg en
        `conftest.py` obliga a tener el psycopg REAL en `sys.modules` para las
        488 que recoge `pytest --collect-only`, y varias suites son herméticas a
        propósito y montan muñecos
        con `sys.modules.setdefault` (este archivo, líneas 68-76). Medido sobre
        una copia: la suite pasó de `456 passed` a `5 failed, 451 passed` — las
        cuatro de `tests/test_buzon_que_no_se_ve.py` más
        `test_la_puerta_del_cursor_esta_puesta_...`, que es la prueba de la
        propia puerta.
      · Y `sitecustomize` correría todavía antes, pero es un archivo que cambia
        el arranque del intérprete para TODO lo que se ejecute en esa máquina,
        no solo para la suite. No se probó.

    Así que hay salida, no es barata, y cuál se toma no es una decisión técnica
    con una respuesta correcta: es de Tiziano. Mientras no la tome, esto se queda
    declarado y con guardia, que es lo honesto.
    """
    escapan = _escapan_si_se_capturan()

    # Lo que no sé cuenta como rojo: si la derivación se quedó vacía o perdió la
    # vía más obvia, esta prueba estaría verde sin haber mirado nada.
    assert ("psycopg", "connect") in escapan, (
        f"la derivación de símbolos que escapan salió sin `psycopg.connect`, que "
        f"es el caso de manual: {sorted(escapan)}. O psycopg cambió de forma o "
        f"`_modulo_de_verdad` devolvió un muñeco — en cualquiera de los dos casos "
        f"esta guarda quedó decorativa y hay que rehacerla, no aflojarla.")
    assert len(escapan) >= 5, (
        f"solo se derivaron {sorted(escapan)} (el 8-sep-2026 eran cinco: "
        f"`psycopg.connect` y los cuatro pools de `psycopg_pool`). Si el número "
        f"baja, el barrido dejó de ver vías y hay que mirarlo.")

    hallados = set()
    for archivo in _py_del_repo():
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(arbol):
            # ⚠️ ACÁ ESTÁ EL TECHO DE ESTA PRUEBA, Y ES ESTA LÍNEA. Solo
            # `ast.ImportFrom`. Una captura por asignación —`x = psycopg.connect`,
            # `self._c = psycopg.connect`, `getattr(psycopg, "connect")`— se lleva
            # EL MISMO objeto y pasa por acá sin que nadie la vea. No es un
            # descuido: ampliar esto a las asignaciones abre la forma siguiente
            # (un diccionario, un `partial`, un decorador), porque lo que se mira
            # es cómo está escrito el código y no lo que hace. Ver el docstring.
            if not isinstance(n, ast.ImportFrom):
                continue
            for alias in n.names:
                if (n.module, alias.name) in escapan:
                    rel = archivo.relative_to(_RAIZ_REPO).as_posix()
                    hallados.add((rel, n.module, alias.name))

    nuevas = hallados - _CAPTURAS_DECLARADAS
    assert not nuevas, (
        "hay sitios que se quedan con un símbolo de psycopg ANTES de que la "
        "puerta se instale. Lo que escriban por ahí NO lo ve el libro, así que "
        "no se cuenta en el parte ni se puede deshacer con el botón:\n  "
        + "\n  ".join(f"{a}: from {m} import {n}" for a, m, n in sorted(nuevas))
        + "\n  La forma que SÍ ve la puerta es dejar el módulo de por medio: "
          "`import psycopg` y llamar `psycopg.connect(...)`, o `import "
          "psycopg_pool` y `psycopg_pool.AsyncConnectionPool(...)`.\n"
          "  Y lo otro que se puede hacer, dicho para que se elija a sabiendas y "
          "no por salir del paso: meter el sitio en `_CAPTURAS_DECLARADAS` pone "
          "esto verde en UNA línea, sin tocar el módulo y con la escritura a "
          "ciegas viva. Eso es más barato que arreglarlo, y por eso hace falta "
          "decirlo: quien lo haga está DECIDIENDO que ese sitio escribe a ciegas, "
          "y esa decisión se hereda. No se hereda por descuido.")

    muertas = _CAPTURAS_DECLARADAS - hallados
    assert not muertas, (
        f"declaradas como capturas que escapan pero ya no existen: "
        f"{sorted(muertas)}. Un perdón muerto es cómo se cuela el siguiente: se "
        f"BORRA la línea, no se deja «por si acaso».")


def test_el_simbolo_capturado_antes_de_la_puerta_de_verdad_se_escapa():
    """FRONTERA 4, la mitad medida: la de arriba declara, ésta lo DEMUESTRA.

    Una frontera declarada y no comprobada es una frase. Acá se captura
    `psycopg.connect` antes de tender la red, se tiende, y se exige que la puerta
    NO le haya llegado. Medido el 8-sep-2026: `real.connect is capturado` → False,
    y `capturado.__module__` sigue siendo `psycopg.connection`.

    ⚠️ Esta prueba está escrita AL REVÉS que las demás del archivo: exige que el
    agujero SIGA ABIERTO. Si se pone roja no es que algo se rompió — es que
    alguien consiguió que la puerta llegue antes que la captura. Entonces la
    FRONTERA 4 dejó de existir y lo que hay que hacer es BORRARLA de la lista de
    arriba y borrar esta prueba, no aflojar el assert.

    No se llega a conectar con nada: solo se comparan identidades de objetos.
    """
    real_antes = _modulo_de_verdad("psycopg")
    assert real_antes is not None, (
        "no hay un `psycopg` de verdad en este proceso: esta medición no vale")
    capturado = real_antes.connect          # el `from psycopg import connect`

    red = _RedDeMensajes()
    red.tender()
    try:
        assert red.motivo_sin_puerta is None, (
            f"la puerta no se pudo poner, así que esto no mide nada: "
            f"{red.motivo_sin_puerta}")
        real = _modulo_de_verdad("psycopg")
        assert real.connect is not capturado, (
            "la puerta SÍ le llegó al símbolo capturado antes de instalarla. "
            "Eso es una buena noticia y significa que la FRONTERA 4 ya no "
            "existe: hay que BORRARLA del docstring de "
            "`test_ninguna_escritura_llega_a_la_base_sin_su_huella`, borrar "
            "`_CAPTURAS_DECLARADAS` y borrar esta prueba. No aflojar esto.")
        assert capturado.__module__ == "psycopg.connection", (
            f"el símbolo capturado ya no es la función real de psycopg sino "
            f"{capturado!r} (módulo {capturado.__module__}): la medición dejó "
            f"de comparar lo que dice comparar")
    finally:
        red.levantar()


# ── Las dos mitades estáticas que el runtime no puede cubrir ─────────────
#
# Una guarda que corre solo ve lo que se ejecuta. Código muerto —una salida
# escrita y todavía sin llamar— no sale en ninguna medición, y mañana alguien la
# llama. Estas dos lo cubren, y las dos derivan su criterio de algo real: una de
# la librería `telegram` instalada, la otra del propio árbol de sintaxis.

def _codigos_por_funcion(fuente: str) -> dict[str, ast.AST]:
    """{nombre de función: su nodo}, incluidas las anidadas."""
    fuera = {}
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fuera[n.name] = n
    return fuera


def test_en_agente_solo__enviar_nombra_al_cliente_de_telegram():
    """La lista de «formas de hablarle a Telegram» sale de la LIBRERÍA, no de acá.

    Lo que se prohíbe es nombrar cualquier método público de `telegram.Bot`, o
    cualquier clase cliente del paquete `telegram`, fuera de `_enviar`. Esos
    nombres se sacan con `dir()` de la clase instalada: son 270 el 8-sep-2026, y
    si la librería crece mañana, la guarda crece con ella. Un rogue puede
    cambiarse de nombre; no puede cambiarle el nombre a `send_message`.

    Esto es lo que atrapa una salida ESCRITA Y TODAVÍA NO LLAMADA, que ninguna
    medición en caliente puede ver.

    HASTA DÓNDE LLEGA, dicho para que no se descubra tarde: un
    `getattr(cliente, "send_" + "message")` no aparece como atributo en el árbol
    y sale de acá en verde — pero en cuanto se ejecute cae en
    `test_un_turno_manda_un_solo_mensaje_y_sale_por_la_puerta`, que no mira
    nombres. Y esto habla de `cerebro/agente.py`: el despertador manda sus
    propios mensajes y no es asunto de este archivo.
    """
    metodos = {n for n in dir(telegram.Bot)
               if not n.startswith("_")
               and callable(getattr(telegram.Bot, n, None))}
    clientes = {n for n in dir(telegram)
                if isinstance(getattr(telegram, n, None), type)
                and issubclass(getattr(telegram, n), telegram.Bot)}
    prohibidos = metodos | clientes

    assert "send_message" in prohibidos and clientes, (
        f"la lista derivada de `telegram` salió sin lo básico ({len(metodos)} "
        f"métodos, clientes={clientes}): la guarda quedó decorativa")

    arbol = ast.parse(FUENTE_AGENTE)
    puerta = next(n for n in ast.walk(arbol)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_enviar")
    dentro = set(range(puerta.lineno, puerta.end_lineno + 1))

    fuera = sorted({
        f"{n.attr if isinstance(n, ast.Attribute) else n.id} "
        f"(línea {n.lineno})"
        for n in ast.walk(arbol)
        if ((isinstance(n, ast.Attribute) and n.attr in prohibidos)
            or (isinstance(n, ast.Name) and n.id in prohibidos))
        and n.lineno not in dentro})

    assert not fuera, (
        f"nombran al cliente de Telegram fuera de `_enviar`, o sea que hay una "
        f"forma de hablarle a Tiziano sin pasar por la puerta del turno: "
        f"{fuera}")


def test_atender_no_tiene_funciones_anidadas_que_nadie_llama():
    """Dentro de `atender` no hay código muerto.

    Una salida escrita y sin llamar no aparece en ninguna medición en caliente,
    y es exactamente la forma en que se prepara la próxima. Acá no se mira qué
    hace ni cómo se llama: se mira si alguien la usa. Si no la usa nadie, o
    sobra o es una puerta trasera a medio abrir; en los dos casos se saca.

    La lista de funciones anidadas sale del árbol de `atender`, no de acá.
    """
    atender = _nodo("atender")
    anidadas = [n for n in ast.walk(atender)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n is not atender]
    usados = {n.id for n in ast.walk(atender) if isinstance(n, ast.Name)}
    huerfanas = sorted(f.name for f in anidadas if f.name not in usados)
    assert not huerfanas, (
        f"están definidas dentro de atender() y no las llama nadie: "
        f"{huerfanas}. O sobran, o es una salida a medio escribir")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
