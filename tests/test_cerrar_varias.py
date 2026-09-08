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
import inspect
import json
import os
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
#   · para que una escritura llegue a Postgres hay que pedirle una conexión al
#     pool. `db.pool` es el único de este proceso — 57 usos en `db/db.py`, 10 en
#     `acciones/crud.py`, y todo `cerebro/*` va por él. Con un doble ahí se ve
#     CADA sentencia que se ejecuta, con su texto ya armado, se llame como se
#     llame quien la mandó.
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

        # Y un pool hecho a mano tampoco se escapa: la fábrica devuelve un
        # espía atado al libro que esté midiendo. Sin esto, una herramienta que
        # se armara su propio `AsyncConnectionPool(DATABASE_URL)` escribiría en
        # producción sin pasar por `db.pool` y ninguna medición la vería.
        self._reemplazar(sys.modules["psycopg_pool"], "AsyncConnectionPool",
                         lambda *a, **k: _PoolEspia(_LIBRO_ACTIVO or _Libro()))

        # Ni una conexión suelta, sin pool de por medio.
        class _ConexionSuelta:
            @classmethod
            async def connect(cls, *a, **k):
                libro = _LIBRO_ACTIVO or _Libro()
                return _ConexionEspia(libro, 9000 + len(libro.sentencias))

        self._reemplazar(sys.modules["psycopg"], "AsyncConnection",
                         _ConexionSuelta)

    def _reemplazar(self, obj, nombre, nuevo):
        self._guardado.append((obj, nombre, getattr(obj, nombre, _NO_ESTABA)))
        setattr(obj, nombre, nuevo)

    def levantar(self):
        for obj, nombre, viejo in reversed(self._guardado):
            if viejo is _NO_ESTABA:
                delattr(obj, nombre)
            else:
                setattr(obj, nombre, viejo)
        self._guardado.clear()


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
      · La red cubre `db.pool`, un `AsyncConnectionPool` hecho a mano y un
        `psycopg.AsyncConnection.connect` suelto — probadas las tres. Lo que
        queda fuera es escribir sin usar Python (un `psql` por subproceso, por
        ejemplo).
      · El «mismo bloque de conexión» es la unidad. Una escritura y su huella
        repartidas en dos bloques distintos saldrían rojas aunque el efecto
        final fuera correcto; hoy `crud` no lo hace nunca.
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
