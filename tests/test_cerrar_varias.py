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

Las dos últimas pruebas son las que impiden que esto vuelva. No comprueban un
caso: comprueban que no se pueda añadir una salida nueva —ni una herramienta
nueva que escriba— que se olvide del parte. Y no lo hacen con una lista de
nombres escrita acá, que se separaría del código el día que se escribe: la
primera recorre el árbol de sintaxis de `atender`, y la segunda le pregunta al
propio `acciones/crud.py` cuáles de sus funciones escriben una huella.

Correr:  python3 -m pytest tests/test_cerrar_varias.py
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import sys
import types

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


def _montar(turnos: list[dict]) -> tuple[_BotFalso, dict]:
    estado = {"editadas": [], "consumidos": 0, "interpretaciones": []}

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
    `mandar = _enviar; await mandar(...)` salía VERDE. Mirando los nombres, para
    hablarle a Tiziano fuera de la puerta hay que nombrar alguno de los dos, y
    los dos están vigilados.

    LO QUE ESTO NO VE, dicho para que no se descubra tarde: `atender` no es el
    único sitio del proyecto que le manda mensajes a Telegram — el despertador
    tiene los suyos. Esta guarda habla de las salidas de UN turno, no de todo lo
    que Lucy dice.
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

    HASTA DÓNDE LLEGA, medido y no tapado: la guarda reconoce el módulo por el
    nombre con que se le llama (`db.`). Un `import db.db as base` dentro de la
    rama y un `base.a_la_papelera(...)` salen VERDES — probado el 8-sep-2026.
    Perseguir los alias no tiene fondo (después vienen `getattr`, un envoltorio,
    una clausura), así que se dice hasta dónde llega en vez de prometer todo:
    esto atrapa al que escribe de la forma normal, que es como se escribe.
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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
