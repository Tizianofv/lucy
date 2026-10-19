# -*- coding: utf-8 -*-
"""L-1: la herramienta `consultar_crm` (diseño `max-natalia-lucy`, pieza L-1).

Una persona de la casa le pregunta a Lucy algo del registro de clientes. Lucy no contesta:
manda el texto original y el `chat_id` a un módulo aparte, lleva la respuesta TAL CUAL al
chat que preguntó y, en su propia base y en lo que ve su modelo, deja solo una marca.

Cada garantía de abajo lleva el nombre de la del diseño (L-1.1 … L-1.10):

  L-1.1  el modelo de Lucy no ve la respuesta (el doble del modelo graba TODO lo que recibe,
         en este turno y en el siguiente, que es donde entra la historia);
  L-1.2  la respuesta no está en NINGUNA columna de la base de prueba (la lista de tablas y de
         columnas sale del esquema, `db.columnas_declaradas()`; el SQL se ejecuta de verdad
         en sqlite) y no va a embeddings (se corre `memoria.indexar_pendientes` de verdad);
  L-1.3  va solo al chat que preguntó, sin copia a Rosi (la puerta real: `copia_dueno`
         parchando `telegram.Bot.send_message`);
  L-1.4  el bucle de Lucy no se frena con el módulo colgado (se corre `interpretar.bucle`);
  L-1.5  tope y errores → la frase fija, nunca una respuesta inventada;
  L-1.6  solo desde un chat permitido, y se manda ESE chat;
  L-1.7  la llave va en la cabecera y no se siguen redirecciones;
  L-1.8  al módulo llega el texto de la persona, no uno que arme el modelo;
  L-1.9  Lucy no importa `cds_max` ni nombra ids de tablas (guarda por AST);
  L-1.10 el censo de llamadas al modelo no ganó entradas (lo vigila la prueba que ya existía,
         `tests/test_responsable.py::test_toda_llamada_al_modelo_del_repositorio_esta_clasificada`;
         acá se comprueba además que `cerebro/crm.py` no llama a ningún modelo).

EL MÓDULO ESTÁ DOBLADO EN LA RED, no en Python: el cliente es el `httpx` DE VERDAD con
transporte de mentira (`httpx.MockTransport`), así que los argumentos con que producción crea
el cliente (`follow_redirects`, `timeout`) se ejecutan de verdad. El contrato que dobla es el del
diseño, sección 9: `POST /lucy/consulta` con `{"chat_id", "texto"}` → 200 `{"texto", "propuesta"}`.

LA FRONTERA, DICHA:
  · sqlite no es Postgres: el SQL del repo se ejecuta con el mismo texto (`%s` → `?`, `= ANY(…)` →
    `IN (SELECT value FROM json_each(…))`, `::vector` fuera), pero lo que Postgres haría con él
    no se ve. La columna `embedding` vive en la base de prueba como una columna cualquiera.
  · Telegram no se toca: `telegram.Bot.send_message` es un doble DEBAJO de la puerta de copia.
  · El módulo de verdad no existe todavía en esta rama (se construye aparte): el doble se ajusta
    al contrato escrito, no a una captura del servicio. Que el nombre de la cabecera de la llave
    sea `X-Lucy-Llave` es una decisión de este lado que el módulo tiene que cumplir.

Correr:  <intérprete> -m pytest tests/test_consultar_crm.py
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_tarea_a_mano as base  # noqa: E402,F401  (deja `psycopg` falseado)

import telegram  # noqa: E402

import captura.correo as correo  # noqa: E402
import cerebro.agente as agente  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import cerebro.crm as crm  # noqa: E402
import cerebro.deepseek as motor  # noqa: E402
import cerebro.despertador as despertador  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import cerebro.memoria as memoria  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parent.parent
DUENO = config.CHAT_ID_DUENO
ROSI = DUENO + 7
AJENO = DUENO + 999

LLAVE = "llave-de-mentira-del-modulo-xyz"
URL_MODULO = "http://modulo-de-mentira.invalido"
# Cada texto lleva una marca que no puede salir de otro sitio: si aparece, vino de aquí.
RESPUESTA = "RESPUESTA-DEL-CRM-9d41c2: el telefono de la clienta de prueba es 000-000-0000"
PREGUNTA = "cual es el telefono de la clienta de prueba"
REESCRITA = "TEXTO-REESCRITO-POR-EL-MODELO-77aa"


# ═══════════════════════════════════════════════════════════════════════════
# El httpx de verdad (varias pruebas pisan `sys.modules["httpx"]` al importarse)
# ═══════════════════════════════════════════════════════════════════════════

def _httpx_de_verdad():
    """Ver `tests/test_noco_lectura.py::_httpx_de_verdad`: el de verdad o esto lo dice."""
    modulo = crm.httpx
    if inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                        ).endswith("httpx/__init__.py"):
        return modulo
    import starlette.testclient as testclient
    modulo = getattr(testclient, "httpx", None)
    assert inspect.ismodule(modulo) and str(getattr(modulo, "__file__", "")
                                            ).endswith("httpx/__init__.py"), (
        "no encontré el httpx de verdad: sin él esta prueba no dice nada sobre lo que "
        "Lucy le pide al módulo por la red.")
    return modulo


HTTPX = _httpx_de_verdad()
_CLIENTE_REAL = HTTPX.AsyncClient


class _ConDoble:
    """El módulo httpx de verdad con UNA pieza cambiada: `AsyncClient`."""

    def __init__(self, real, cliente):
        self._real = real
        self.AsyncClient = cliente

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)


class Modulo:
    """El módulo del CRM, doblado en la red según el contrato del diseño (sección 9).

    `responder` es una función `pedido -> Response` (o corrutina, o que lanza). Por defecto
    contesta 200 `{"texto": RESPUESTA, "propuesta": None}`.
    """

    def __init__(self):
        self.pedidos: list = []
        self.responder = None
        self.otros_destinos: list = []     # pedidos a cualquier otro host (una redirección seguida)
        self.cliente_args: list = []       # con qué argumentos creó Lucy el cliente

    def cliente(self, *args, **kwargs):
        self.cliente_args.append(dict(kwargs))
        kwargs["transport"] = HTTPX.MockTransport(self._manejar)
        return _CLIENTE_REAL(*args, **kwargs)

    async def _manejar(self, p):
        if p.url.host != HTTPX.URL(URL_MODULO).host:
            self.otros_destinos.append(p)
            return HTTPX.Response(200, json={"texto": "OTRO-SERVIDOR"})
        self.pedidos.append(p)
        if self.responder is not None:
            r = self.responder(p)
            return await r if inspect.isawaitable(r) else r
        return HTTPX.Response(200, json={"texto": RESPUESTA, "propuesta": None})

    @staticmethod
    def cuerpo(p) -> dict:
        return json.loads(p.content.decode("utf-8"))


@pytest.fixture
def modulo(monkeypatch):
    m = Modulo()
    monkeypatch.setattr(config, "CRM_MODULO_URL", URL_MODULO)
    monkeypatch.setattr(config, "CRM_MODULO_LLAVE", LLAVE)
    monkeypatch.setattr(crm, "httpx", _ConDoble(HTTPX, m.cliente))
    return m


# ═══════════════════════════════════════════════════════════════════════════
# La base de prueba: sqlite que ejecuta el SQL del repo, con TODAS las tablas del esquema
# ═══════════════════════════════════════════════════════════════════════════

def _sql_para_sqlite(sql: str) -> str:
    s = str(sql).replace("%s", "?")
    s = re.sub(r"(\w+)\s*=\s*ANY\(\?\)", r"\1 IN (SELECT value FROM json_each(?))", s)
    return s.replace("::vector", "")


def _param(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, tuple, dict)) else v


class _Cur:
    def __init__(self, con, como_dict: bool):
        self._con, self._dict, self._cur = con, como_dict, None

    async def execute(self, sql, params=None):
        self._cur = self._con.execute(_sql_para_sqlite(sql),
                                      tuple(_param(v) for v in (params or ())))
        return self

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _fila(self, f):
        if f is None or not self._dict:
            return f
        return dict(zip([d[0] for d in self._cur.description], f))

    async def fetchone(self):
        return self._fila(self._cur.fetchone())

    async def fetchall(self):
        return [self._fila(f) for f in self._cur.fetchall()]


class _Conn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _Cur(self._con, row_factory is not None)

    async def execute(self, sql, params=None):
        return await _Cur(self._con, False).execute(sql, params)

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *e):
                return False
        return _T()


class _Pool:
    def __init__(self, con):
        self._con = con

    def connection(self):
        con = self._con

        class _Ctx:
            async def __aenter__(s):
                return _Conn(con)

            async def __aexit__(s, *e):
                return False
        return _Ctx()


# Los valores por omisión que `db/schema.sql` declara para `bandeja` y que el código da por hechos.
_OMISIONES_DE_BANDEJA = {"estado": "'sin_procesar'", "origen": "'telegram'", "intentos": "0"}


class Mundo:
    """Toda la base: una tabla por cada tabla que declara el esquema, con sus columnas.

    Las tablas y las columnas salen de `db.columnas_declaradas()` (el esquema y sus
    migraciones), no de una lista de aquí. Las columnas no llevan tipo: sqlite guarda lo que se
    le dé, y justo eso es lo que se necesita para buscar un texto en TODAS.
    """

    def __init__(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.create_function("now", 0, lambda: "2026-10-19T12:00:00+00:00")
        self.tablas = db.columnas_declaradas()
        for tabla, columnas in self.tablas.items():
            defs = []
            for c in columnas:
                if c == "id":
                    defs.append('"id" INTEGER PRIMARY KEY')
                elif tabla == "bandeja" and c in _OMISIONES_DE_BANDEJA:
                    defs.append(f'"{c}" DEFAULT {_OMISIONES_DE_BANDEJA[c]}')
                else:
                    defs.append(f'"{c}"')
            self.con.execute(f'CREATE TABLE "{tabla}" ({", ".join(defs)})')
        self.con.commit()

    def pool(self):
        return _Pool(self.con)

    def fila(self, id: int, chat_id: int, texto: str, tipo: str = "texto", **extra):
        campos = {"id": id, "chat_id": chat_id, "telegram_msg_id": 100 + id,
                  "tipo_entrada": tipo, "contenido_raw": texto, "estado": "procesando", **extra}
        self.con.execute(
            f'INSERT INTO bandeja ({", ".join(campos)}) VALUES ({", ".join("?" for _ in campos)})',
            tuple(campos.values()))
        self.con.commit()
        return {"id": id, "chat_id": chat_id, "telegram_msg_id": 100 + id, "tipo_entrada": tipo,
                "contenido_raw": texto, "origen": "telegram", **extra}

    def valor(self, id: int, columna: str):
        f = self.con.execute(f'SELECT "{columna}" FROM bandeja WHERE id = ?', (id,)).fetchone()
        return f[0] if f else None

    def volcado(self) -> str:
        """TODO lo que hay escrito, en todas las tablas y todas las columnas."""
        partes = []
        for (t,) in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for f in self.con.execute(f'SELECT * FROM "{t}"'):
                partes.extend(str(v) for v in f if v is not None)
        return "\n".join(partes)

    def tablas_volcadas(self) -> set:
        return {t for (t,) in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.fixture
def mundo(monkeypatch):
    m = Mundo()
    monkeypatch.setattr(db, "pool", m.pool())
    return m


# ═══════════════════════════════════════════════════════════════════════════
# Telegram (debajo de la puerta de copia), el modelo y las embeddings, doblados
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def telegram_falso():
    """`telegram.Bot.send_message` doblado POR DEBAJO del parche de `copia_dueno`, que se
    instala de verdad: lo que sale de aquí es lo que saldría hacia Telegram, copias incluidas."""
    pristino = telegram.Bot.send_message
    enviados: list[dict] = []

    async def _doble(self, chat_id, text, **kwargs):
        enviados.append({"chat_id": chat_id, "text": text, **kwargs})
        return types.SimpleNamespace(message_id=len(enviados))

    telegram.Bot.send_message = _doble
    copia_dueno.instalar()
    try:
        yield enviados
    finally:
        copia_dueno.desinstalar()
        telegram.Bot.send_message = pristino


@pytest.fixture
def bot():
    return telegram.Bot(token="123456:token-de-prueba")


def _casa(copia_a_rosi: bool = True):
    """El dueño y Rosi pueden entrar; Rosi recibe copia de lo que se le manda al dueño."""
    config.NOMBRES_POR_CHAT = {DUENO: "Tiziano", ROSI: "Rosi"}
    config.CHAT_IDS_PERMITIDOS = (DUENO, ROSI)
    config._NOMBRES_DE_COPIA = ("Rosi",) if copia_a_rosi else ()


class Modelo:
    """El modelo de Lucy, doblado: graba TODO lo que recibe (`pedidos`) y contesta con `guion`.

    `guion(mensajes) -> dict` ve lo que el modelo está viendo en ese momento.
    """

    def __init__(self, guion=None):
        self.pedidos: list[list[dict]] = []
        self.guion = guion or self.guion_normal

    @staticmethod
    def guion_normal(mensajes):
        """«teléfono» → consulta al registro (con un argumento reescrito a propósito); si ya hay
        un resultado, o es otra cosa, responde."""
        ultimo = mensajes[-1]["content"]
        if not ultimo.startswith("[resultado]") and "telefono" in ultimo:
            return {"herramienta": "consultar_crm", "argumentos": {"texto": REESCRITA}}
        return {"herramienta": "responder",
                "argumentos": {"texto": "Listo.", "clasificacion": "charla"}}

    async def create(self, **kw):
        self.pedidos.append([dict(m) for m in kw["messages"]])
        paso = self.guion(kw["messages"])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(paso, ensure_ascii=False)))])

    def todo_lo_que_vio(self) -> str:
        return "\n".join(m["content"] for pedido in self.pedidos for m in pedido)


@pytest.fixture
def modelo(monkeypatch):
    m = Modelo()
    monkeypatch.setattr(motor, "cliente", types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=m)))
    return m


class Embeddings:
    def __init__(self):
        self.textos: list[str] = []

    async def create(self, model=None, input=None, **kw):
        self.textos.extend(input)
        return types.SimpleNamespace(data=[
            types.SimpleNamespace(index=i, embedding=[0.1, 0.2]) for i in range(len(input))])


@pytest.fixture
def embeddings(monkeypatch):
    e = Embeddings()
    monkeypatch.setattr(memoria, "cliente", types.SimpleNamespace(embeddings=e))
    return e


async def _hasta_que_terminen_las_consultas():
    pendientes = list(crm._en_vuelo)
    if pendientes:
        await asyncio.wait_for(asyncio.gather(*pendientes), timeout=10)


def _a(enviados, chat_id):
    return [e["text"] for e in enviados if e["chat_id"] == chat_id]


async def _preguntar(mundo, bot, chat_id=DUENO, texto=PREGUNTA, id=1, tipo="texto", **extra):
    fila = mundo.fila(id, chat_id, texto, tipo, **extra)
    await agente.atender(fila, texto, bot)
    await _hasta_que_terminen_las_consultas()
    return fila


# ═══════════════════════════════════════════════════════════════════════════
# El camino feliz, medido de punta a punta
# ═══════════════════════════════════════════════════════════════════════════

async def test_la_pregunta_llega_al_modulo_y_la_respuesta_al_chat_tal_cual(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot)

    assert len(modulo.pedidos) == 1, f"el módulo recibió {len(modulo.pedidos)} pedidos"
    p = modulo.pedidos[0]
    assert (p.method, p.url.path, p.url.query) == ("POST", "/lucy/consulta", b"")
    assert Modulo.cuerpo(p) == {"chat_id": DUENO, "texto": PREGUNTA}
    # Al chat salen DOS cosas, en orden: la espera y la respuesta, esta última sin tocar.
    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, RESPUESTA]
    # El modelo se llamó UNA vez (eligió la herramienta) y no volvió a hablar.
    assert len(modelo.pedidos) == 1
    assert len(telegram_falso) == 2


async def test_una_respuesta_larga_sale_entera_en_varios_mensajes(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    larga = "\n".join(f"renglon {i:04d} " + "x" * 60 for i in range(200))
    assert len(larga) > 3 * crm.LARGO_MAX
    modulo.responder = lambda p: HTTPX.Response(200, json={"texto": larga})
    await _preguntar(mundo, bot)

    salio = _a(telegram_falso, DUENO)
    assert salio[0] == crm.FRASE_BUSCANDO
    pedazos = salio[1:]
    assert len(pedazos) >= 4
    assert all(len(p) <= crm.LARGO_MAX for p in pedazos)
    # Pegados dan el texto: nada recortado (los bordes en blanco los quita `_enviar`).
    assert "".join(p.strip("\n") for p in pedazos).replace("\n", "") == larga.replace("\n", "")


def test_pedazos_nunca_pasan_el_tope_ni_pierden_texto():
    for texto in ["corto", "a" * 4000, "a" * 4001, "ñ" * 9000, "😀" * 3000,
                  ("linea\n" * 2000), "\n".join("b" * 1500 for _ in range(7))]:
        partes = crm.pedazos(texto)
        assert "".join(partes) == texto, texto[:20]
        assert all(len(p.encode("utf-16-le")) // 2 <= crm.LARGO_MAX for p in partes), texto[:20]
    assert crm.pedazos("   \n  ") == []


# ═══════════════════════════════════════════════════════════════════════════
# L-1.1  el modelo de Lucy no ve la respuesta
# ═══════════════════════════════════════════════════════════════════════════

async def test_L11_el_modelo_no_ve_la_respuesta_ni_en_este_turno_ni_en_el_siguiente(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot, id=1)
    # El turno siguiente, de la misma persona: la historia (memoria corta) entra al modelo.
    siguiente = mundo.fila(2, DUENO, "gracias")
    await agente.atender(siguiente, "gracias", bot)
    await _hasta_que_terminen_las_consultas()

    visto = modelo.todo_lo_que_vio()
    assert len(modelo.pedidos) == 2, "esperaba una llamada por turno"
    assert "RESPUESTA-DEL-CRM" not in visto, "el modelo vio la respuesta del CRM"
    assert "000-000-0000" not in visto
    # No vacío: lo que el modelo SÍ ve del primer turno es la pregunta y la marca.
    assert PREGUNTA in visto
    assert crm.MARCA_CONTESTO in "".join(m["content"] for m in modelo.pedidos[1]), (
        "la marca no llegó a la historia del turno siguiente")


# ═══════════════════════════════════════════════════════════════════════════
# L-1.2  no se guarda en Lucy
# ═══════════════════════════════════════════════════════════════════════════

async def test_L12_la_respuesta_no_esta_en_ninguna_columna_de_ninguna_tabla(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    fila = await _preguntar(mundo, bot)
    await agente.atender(mundo.fila(2, DUENO, "gracias"), "gracias", bot)  # un turno más

    # La cobertura: el volcado recorrió TODAS las tablas del esquema, ninguna quedó fuera.
    assert mundo.tablas_volcadas() == set(db.columnas_declaradas()), (
        "el volcado no cubre todo el esquema")
    volcado = mundo.volcado()
    assert PREGUNTA in volcado, "el volcado no ve ni lo que sí se guarda: no está mirando"
    assert "RESPUESTA-DEL-CRM" not in volcado, "la respuesta del CRM quedó escrita en la base"
    assert "000-000-0000" not in volcado
    # Lo que queda en la fila es la marca, y la pregunta queda como todo lo que le escriben.
    assert mundo.valor(fila["id"], "respuesta_lucy") == crm.MARCA_CONTESTO
    assert mundo.valor(fila["id"], "contenido_raw") == PREGUNTA
    assert mundo.valor(fila["id"], "clasificacion") == "pregunta"
    assert mundo.valor(fila["id"], "estado") == "procesado"


async def test_L12_la_memoria_de_largo_plazo_no_recibe_la_respuesta(
        mundo, modulo, modelo, telegram_falso, bot, embeddings):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot)
    n = await memoria.indexar_pendientes()     # la rama del bucle, de verdad, sobre la base

    assert n >= 1 and embeddings.textos, "no se indexó nada: la prueba no está mirando"
    mandado = "\n".join(embeddings.textos)
    assert PREGUNTA in mandado and crm.MARCA_CONTESTO in mandado
    assert "RESPUESTA-DEL-CRM" not in mandado, "la respuesta se mandó a embeddings"


async def test_L12_cuando_el_modulo_no_contesta_queda_la_frase_y_no_un_texto_del_modulo(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    modulo.responder = lambda p: HTTPX.Response(
        500, json={"texto": "RESPUESTA-DEL-CRM-DE-UN-500", "causa": "boom"})
    fila = await _preguntar(mundo, bot)
    assert mundo.valor(fila["id"], "respuesta_lucy") == crm.FRASE_NO_CONTESTO
    assert "RESPUESTA-DEL-CRM" not in mundo.volcado()


# ═══════════════════════════════════════════════════════════════════════════
# L-1.3  solo al chat que preguntó, sin copia a Rosi
# ═══════════════════════════════════════════════════════════════════════════

async def test_L13_lo_que_pregunta_el_dueno_no_se_copia_a_rosi(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=True)
    await _preguntar(mundo, bot, chat_id=DUENO)
    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, RESPUESTA]
    assert _a(telegram_falso, ROSI) == [], (
        f"a Rosi le llegó algo de lo que preguntó el dueño: {_a(telegram_falso, ROSI)}")

    # El control: la puerta de copia SÍ está puesta. Un mensaje cualquiera al dueño se copia.
    await bot.send_message(chat_id=DUENO, text="un mensaje cualquiera")
    assert _a(telegram_falso, ROSI) == ["un mensaje cualquiera"]


async def test_L13_lo_que_pregunta_rosi_le_llega_a_rosi_y_a_nadie_mas(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=True)
    await _preguntar(mundo, bot, chat_id=ROSI)
    assert _a(telegram_falso, ROSI) == [crm.FRASE_BUSCANDO, RESPUESTA]
    assert _a(telegram_falso, DUENO) == []


# ═══════════════════════════════════════════════════════════════════════════
# L-1.4  el bucle no se frena
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def bucle_aislado(monkeypatch):
    """`interpretar.bucle` real, con todas sus ramas laterales anuladas menos el despertador,
    que se cuenta. Cada vuelta dura milisegundos."""
    monkeypatch.setattr(interpretar, "INTERVALO_S", 0.005)
    cuenta = {"despertador": 0, "meta": None, "alcanzada": asyncio.Event()}

    async def _despertador(bot):
        cuenta["despertador"] += 1
        if cuenta["meta"] is not None and cuenta["despertador"] >= cuenta["meta"]:
            cuenta["alcanzada"].set()

    async def _nada(*a, **k):
        return None

    monkeypatch.setattr(despertador, "revisar", _despertador)
    monkeypatch.setattr(despertador, "revisar_consultas_de_natalia", _nada)
    monkeypatch.setattr(despertador, "revisar_backup", _nada)
    monkeypatch.setattr(despertador, "revisar_alertas_tecnicas_sin_tomar", _nada)
    monkeypatch.setattr(memoria, "indexar_pendientes", _nada)
    monkeypatch.setattr(correo, "reporte_diario", _nada)
    monkeypatch.setattr(correo, "confirmar_leidos", _nada)
    monkeypatch.setattr(correo, "vigilar_911", _nada)
    monkeypatch.setattr(interpretar.consumos, "revisar", _nada)
    monkeypatch.setattr(interpretar.consumos, "avisar_si_hay_bancos_mudos", _nada)
    monkeypatch.setattr(interpretar.consumos, "avisar_si_no_hay_latido", _nada)
    monkeypatch.setattr(interpretar.calendario, "sincronizar", _nada)
    monkeypatch.setattr(db, "rescatar_procesando", _nada)
    return cuenta


async def test_L14_con_el_modulo_colgado_el_bucle_sigue_y_otro_mensaje_se_atiende(
        mundo, modulo, modelo, telegram_falso, bot, bucle_aislado, monkeypatch):
    _casa(copia_a_rosi=False)
    colgado = asyncio.Event()
    llego_al_modulo = asyncio.Event()

    async def _se_cuelga(p):
        llego_al_modulo.set()
        await colgado.wait()
        return HTTPX.Response(200, json={"texto": RESPUESTA})

    modulo.responder = _se_cuelga
    primera = mundo.fila(1, DUENO, PREGUNTA)
    segunda = mundo.fila(2, DUENO, "anota esto")
    lotes = [[primera], [segunda]]

    async def _tomar_pendientes():
        return lotes.pop(0) if lotes else []

    monkeypatch.setattr(db, "tomar_pendientes", _tomar_pendientes)
    segunda_atendida = asyncio.Event()
    original = agente.atender

    async def _atender_y_avisar(fila, texto, bot_):
        await original(fila, texto, bot_)
        if fila["id"] == 2:
            segunda_atendida.set()

    monkeypatch.setattr(agente, "atender", _atender_y_avisar)

    tarea = asyncio.create_task(interpretar.bucle(bot))
    try:
        await asyncio.wait_for(llego_al_modulo.wait(), timeout=10)
        # Con el módulo COLGADO: el otro mensaje se atiende y el despertador sigue sonando.
        try:
            await asyncio.wait_for(segunda_atendida.wait(), timeout=5)
        except asyncio.TimeoutError:
            pytest.fail("el bucle se frenó: con el módulo colgado no se atendió el otro mensaje")
        bucle_aislado["meta"] = bucle_aislado["despertador"] + 2
        try:
            await asyncio.wait_for(bucle_aislado["alcanzada"].wait(), timeout=5)
        except asyncio.TimeoutError:
            pytest.fail("el despertador dejó de sonar mientras el módulo estaba colgado")
        assert _a(telegram_falso, DUENO).count(RESPUESTA) == 0, "la respuesta salió antes de tiempo"
        assert len(crm._en_vuelo) == 1, "la consulta no está viva en su tarea"
        assert "Listo." in _a(telegram_falso, DUENO), "el otro mensaje no recibió su respuesta"
    finally:
        colgado.set()
        await _hasta_que_terminen_las_consultas()
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass
    # Al soltar al módulo, la respuesta llega igual, al chat que preguntó.
    assert RESPUESTA in _a(telegram_falso, DUENO)
    assert crm._en_vuelo == set()


async def test_L14_la_consulta_vive_en_una_tarea_aparte_que_el_turno_no_espera(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    colgado = asyncio.Event()

    async def _se_cuelga(p):
        await colgado.wait()
        return HTTPX.Response(200, json={"texto": RESPUESTA})

    modulo.responder = _se_cuelga
    fila = mundo.fila(1, DUENO, PREGUNTA)
    # El turno entero vuelve aunque el módulo no haya contestado.
    await asyncio.wait_for(agente.atender(fila, PREGUNTA, bot), timeout=5)
    assert mundo.valor(1, "estado") == "procesado"
    assert mundo.valor(1, "respuesta_lucy") == crm.MARCA_CONSULTANDO
    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO]
    assert len(crm._en_vuelo) == 1
    colgado.set()
    await _hasta_que_terminen_las_consultas()
    assert mundo.valor(1, "respuesta_lucy") == crm.MARCA_CONTESTO


# ═══════════════════════════════════════════════════════════════════════════
# L-1.5  tope y errores: la frase fija, nunca una respuesta inventada
# ═══════════════════════════════════════════════════════════════════════════

def _lanza(exc):
    def _f(p):
        raise exc
    return _f


_FALLAS = {
    "401": lambda p: HTTPX.Response(401, json={"texto": "SECRETO-DEL-MODULO"}),
    "403": lambda p: HTTPX.Response(403, text="SECRETO-DEL-MODULO"),
    "422": lambda p: HTTPX.Response(422, json={"causa": "SECRETO-DEL-MODULO"}),
    "500": lambda p: HTTPX.Response(500, text="SECRETO-DEL-MODULO"),
    "502": lambda p: HTTPX.Response(502, json={"causa": "SECRETO-DEL-MODULO"}),
    "503": lambda p: HTTPX.Response(503, text="SECRETO-DEL-MODULO"),
    "504": lambda p: HTTPX.Response(504, json={"causa": "SECRETO-DEL-MODULO"}),
    "red_caida": _lanza(HTTPX.ConnectError("no hay red")),
    "red_cortada": _lanza(HTTPX.ReadError("se cortó")),
    "no_es_json": lambda p: HTTPX.Response(200, text="<html>SECRETO-DEL-MODULO</html>"),
    "json_de_otra_forma": lambda p: HTTPX.Response(200, json=["SECRETO-DEL-MODULO"]),
    "sin_texto": lambda p: HTTPX.Response(200, json={"propuesta": None}),
    "texto_vacio": lambda p: HTTPX.Response(200, json={"texto": "   "}),
    "texto_que_no_es_texto": lambda p: HTTPX.Response(200, json={"texto": {"x": 1}}),
    "redireccion": lambda p: HTTPX.Response(
        302, headers={"location": "http://otro-servidor.invalido/lucy/consulta"}),
}


@pytest.mark.parametrize("falla", sorted(_FALLAS))
async def test_L15_ante_cualquier_falla_sale_la_frase_fija_y_nada_mas(
        falla, mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    modulo.responder = _FALLAS[falla]
    await _preguntar(mundo, bot)

    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, crm.FRASE_NO_CONTESTO]
    assert len(modelo.pedidos) == 1, "el modelo habló otra vez: contestó por su cuenta"
    todo = " ".join(e["text"] for e in telegram_falso) + mundo.volcado()
    assert "SECRETO-DEL-MODULO" not in todo
    assert modulo.otros_destinos == [], "se siguió una redirección"


async def test_L15_el_tope_de_lucy_corta_a_un_modulo_colgado(
        mundo, modulo, modelo, telegram_falso, bot, monkeypatch):
    _casa(copia_a_rosi=False)
    monkeypatch.setattr(crm, "TOPE_S", 0.2)
    nunca = asyncio.Event()

    async def _se_cuelga(p):
        await nunca.wait()

    modulo.responder = _se_cuelga
    # El pase se mide con su propio reloj de 5 s: si el tope no corta, esto no termina.
    fila = mundo.fila(1, DUENO, PREGUNTA)
    await asyncio.wait_for(agente.atender(fila, PREGUNTA, bot), timeout=5)
    try:
        await asyncio.wait_for(_hasta_que_terminen_las_consultas(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail("el tope de Lucy no cortó la llamada colgada")
    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, crm.FRASE_NO_CONTESTO]


async def test_L15_sin_direccion_o_sin_llave_sale_la_frase_y_no_se_llama_a_nadie(
        mundo, modulo, modelo, telegram_falso, bot, monkeypatch):
    _casa(copia_a_rosi=False)
    for var in ("CRM_MODULO_URL", "CRM_MODULO_LLAVE"):
        telegram_falso.clear()
        monkeypatch.setattr(config, var, "")
        await _preguntar(mundo, bot, id={"CRM_MODULO_URL": 11, "CRM_MODULO_LLAVE": 12}[var])
        assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, crm.FRASE_NO_CONTESTO], var
        assert modulo.pedidos == [], var
        monkeypatch.setattr(config, var, URL_MODULO if var == "CRM_MODULO_URL" else LLAVE)


# ═══════════════════════════════════════════════════════════════════════════
# L-1.6  solo desde un chat permitido, y se manda ESE chat
# ═══════════════════════════════════════════════════════════════════════════

async def test_L16_se_manda_el_chat_de_quien_pregunto_y_a_el_vuelve_la_respuesta(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot, chat_id=ROSI)
    assert Modulo.cuerpo(modulo.pedidos[0])["chat_id"] == ROSI, (
        "al módulo no llegó el chat de quien preguntó")
    assert _a(telegram_falso, ROSI) == [crm.FRASE_BUSCANDO, RESPUESTA]
    assert _a(telegram_falso, DUENO) == []


async def test_L16_un_chat_fuera_de_la_lista_no_llega_al_modulo(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot, chat_id=AJENO)
    assert modulo.pedidos == [], "un chat que no está en la lista llegó al módulo"
    # El modelo recibió el ERROR y contestó por su cuenta (sin datos del registro).
    assert any("no puedo consultar el registro" in m["content"]
               for p in modelo.pedidos for m in p), "el modelo no recibió el ERROR"
    assert "RESPUESTA-DEL-CRM" not in " ".join(e["text"] for e in telegram_falso)


@pytest.mark.parametrize("tipo", ["foto", "email", "sistema"])
async def test_L16_un_turno_que_no_es_de_una_persona_con_texto_o_voz_no_llega_al_modulo(
        tipo, mundo, modulo, modelo, telegram_falso, bot):
    """Una foto es lo que otro modelo leyó; un correo o un encargo de la maquinaria no son
    preguntas de nadie: por ahí no se abre el registro de clientes."""
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot, chat_id=DUENO, tipo=tipo)
    assert modulo.pedidos == [], f"un turno '{tipo}' llegó al módulo"


def test_L16_se_puede_lee_la_lista_de_ahora_y_no_la_de_cuando_se_importo():
    permitidos = config.CHAT_IDS_PERMITIDOS
    try:
        config.CHAT_IDS_PERMITIDOS = (1, 2)
        assert crm.se_puede(2, "texto") and crm.se_puede(2, "audio")
        assert not crm.se_puede(3, "texto")
        assert not crm.se_puede(2, "foto")
        config.CHAT_IDS_PERMITIDOS = (3,)
        assert crm.se_puede(3, "texto") and not crm.se_puede(2, "texto")
    finally:
        config.CHAT_IDS_PERMITIDOS = permitidos


# ═══════════════════════════════════════════════════════════════════════════
# L-1.7  la llave en la cabecera; ninguna redirección
# ═══════════════════════════════════════════════════════════════════════════

async def test_L17_la_llave_va_en_la_cabecera_y_en_ningun_otro_sitio(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot)
    p = modulo.pedidos[0]
    assert p.headers[crm.CABECERA_LLAVE] == LLAVE
    assert LLAVE not in str(p.url), "la llave viaja en la dirección"
    assert LLAVE not in p.content.decode("utf-8"), "la llave viaja en el cuerpo"
    # La pregunta tampoco va en la dirección.
    assert PREGUNTA not in str(p.url) and str(DUENO) not in str(p.url)
    assert all(LLAVE not in v for k, v in p.headers.items() if k.lower() != crm.CABECERA_LLAVE.lower())


async def test_L17_no_se_sigue_una_redireccion_y_la_llave_no_llega_a_otro_servidor(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    modulo.responder = _FALLAS["redireccion"]
    await _preguntar(mundo, bot)
    assert modulo.otros_destinos == [], "Lucy siguió la redirección (y le mandó la llave a otro)"
    # El cliente se creó como producción lo crea: sin seguir redirecciones.
    assert modulo.cliente_args and all(
        a.get("follow_redirects") is False for a in modulo.cliente_args), modulo.cliente_args


async def test_L17_ni_la_llave_ni_los_textos_llegan_al_registro_de_lucy(
        mundo, modulo, modelo, telegram_falso, bot, caplog):
    _casa(copia_a_rosi=False)
    caplog.set_level(logging.DEBUG)
    await _preguntar(mundo, bot, id=1)                     # camino feliz
    modulo.responder = _FALLAS["500"]
    await _preguntar(mundo, bot, id=2)                     # y uno que falla
    modulo.responder = _lanza(HTTPX.ConnectError(f"no pude con {LLAVE} {PREGUNTA}"))
    await _preguntar(mundo, bot, id=3)                     # y una red caída con datos en el error
    texto = "\n".join(r.getMessage() + str(r.exc_info and r.exc_text or "")
                      for r in caplog.records if r.name.startswith("lucy.crm"))
    assert texto, "no se registró nada: la prueba no está mirando"
    for secreto in (LLAVE, PREGUNTA, "RESPUESTA-DEL-CRM", "SECRETO-DEL-MODULO"):
        assert secreto not in texto, f"el registro de Lucy tiene «{secreto}»"


# ═══════════════════════════════════════════════════════════════════════════
# L-1.8  al módulo llega lo que escribió la persona
# ═══════════════════════════════════════════════════════════════════════════

async def test_L18_al_modulo_llega_el_texto_de_la_persona_y_no_el_argumento_del_modelo(
        mundo, modulo, modelo, telegram_falso, bot):
    _casa(copia_a_rosi=False)
    await _preguntar(mundo, bot)
    enviado = Modulo.cuerpo(modulo.pedidos[0])["texto"]
    assert enviado == PREGUNTA
    assert REESCRITA not in json.dumps(Modulo.cuerpo(modulo.pedidos[0])), (
        "al módulo llegó lo que armó el modelo")
    # No vacío: el modelo SÍ armó un argumento reescrito (la prueba lo provoca) y quedó en su turno.
    assert REESCRITA in json.dumps(mundo.valor(1, "interpretacion"))


async def test_L18_una_nota_de_voz_llega_como_su_transcripcion(
        mundo, modulo, modelo, telegram_falso, bot, monkeypatch):
    """Por el camino de producción: `interpretar._procesar` → `_obtener_texto` → `atender`."""
    _casa(copia_a_rosi=False)
    transcripcion = "cual es el telefono de la clienta de prueba, dicho en voz"
    fila = mundo.fila(1, DUENO, None, tipo="audio", transcripcion=transcripcion,
                      archivo_id="archivo-de-prueba")
    fila["transcripcion"] = transcripcion
    fila["archivo_id"] = "archivo-de-prueba"
    fila["contenido_raw"] = None
    await interpretar._procesar(fila, bot)
    await _hasta_que_terminen_las_consultas()
    assert Modulo.cuerpo(modulo.pedidos[0])["texto"] == transcripcion
    assert _a(telegram_falso, DUENO) == [crm.FRASE_BUSCANDO, RESPUESTA]


# ═══════════════════════════════════════════════════════════════════════════
# L-1.9  Lucy no importa `cds_max` ni nombra ids de tablas (guarda por AST)
# ═══════════════════════════════════════════════════════════════════════════

# El id de una tabla de NocoDB es una «m» y 14 letras o dígitos. FRONTERA: ese formato es el de
# NocoDB en general; no se comprobó contra la instalación del estudio (el id real no se
# escribe aquí: el repositorio es público).
_ID_DE_TABLA = re.compile(r"^m[a-z0-9]{14}$")
_PAQUETE_PROHIBIDO = "cds_max"


def _lo_que_nombra(codigo: str) -> list[str]:
    """Lo que ese código nombra y no debe: un import de `cds_max` (de cualquier forma), un texto
    que lo menciona (`importlib.import_module("cds_max.x")`, `__import__("cds_max")`) o un
    literal con forma de id de tabla. Es una regla sobre el ÁRBOL, no sobre cómo está escrito."""
    arbol = ast.parse(codigo)
    hallado = []
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            hallado += [a.name for a in n.names if a.name.split(".")[0] == _PAQUETE_PROHIBIDO]
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] == _PAQUETE_PROHIBIDO:
                hallado.append(n.module)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if _PAQUETE_PROHIBIDO in n.value or _ID_DE_TABLA.match(n.value.strip()):
                hallado.append(n.value[:40])
    return hallado


def _archivos_de_lucy():
    """Todo `.py` del repositorio que no es una prueba, pedido a LA puerta de los barridos
    (`test_buzon_que_no_se_ve._py_en_disco`): lo saca del disco y no entra en entornos virtuales."""
    from test_buzon_que_no_se_ve import _py_en_disco
    for ruta in sorted(_py_en_disco(RAIZ)):
        if ruta.relative_to(RAIZ).parts[0] != "tests":
            yield ruta


def test_L19_ningun_archivo_de_lucy_importa_cds_max_ni_nombra_un_id_de_tabla():
    archivos = list(_archivos_de_lucy())
    assert len(archivos) > 50 and (RAIZ / "cerebro" / "crm.py") in archivos, (
        f"el barrido no encontró los archivos de Lucy ({len(archivos)})")
    culpables = {str(r.relative_to(RAIZ)): _lo_que_nombra(r.read_text(encoding="utf-8"))
                 for r in archivos}
    culpables = {k: v for k, v in culpables.items() if v}
    assert not culpables, f"código de Lucy que nombra a cds_max o a un id de tabla: {culpables}"


@pytest.mark.parametrize("codigo", [
    "import cds_max",
    "import cds_max.nocodb as nc",
    "from cds_max import config",
    "from cds_max.nocodb import get_all",
    "import importlib\nimportlib.import_module('cds_max.chat')",
    "__import__('cds_max')",
    "RUTA = 'cds_max/config.py'",
    "TABLA = 'mabcdefghij1234'",
    "def f():\n    return {'t': 'm0123456789abcd'}",
])
def test_L19_la_guarda_ve_el_codigo_inventado(codigo):
    assert _lo_que_nombra(codigo), f"la guarda no vio: {codigo!r}"


@pytest.mark.parametrize("codigo", [
    "import json",
    "from cerebro import crm",
    "X = 'mensaje normal'",
    "Y = 'm1234'",           # no tiene la forma de un id
])
def test_L19_la_guarda_no_acusa_a_codigo_normal(codigo):
    assert _lo_que_nombra(codigo) == []


# ═══════════════════════════════════════════════════════════════════════════
# L-1.10  el censo de llamadas al modelo no gana entradas
# ═══════════════════════════════════════════════════════════════════════════

def test_L110_crm_no_llama_a_ningun_modelo():
    """El censo (`test_responsable.py`) sigue verde sin entradas nuevas si este archivo no
    llama a `…completions.create`. Acá se mira lo mismo, y además `embeddings`."""
    arbol = ast.parse((RAIZ / "cerebro" / "crm.py").read_text(encoding="utf-8"))
    llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "create"]
    assert llamadas == []
    importados = {a.name for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    importados |= {n.module for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}
    assert not {"openai", "cerebro.deepseek", "cerebro.memoria", "cerebro.vision",
                "cerebro.whisper"} & importados, importados


# ═══════════════════════════════════════════════════════════════════════════
# La herramienta, como la ve el modelo
# ═══════════════════════════════════════════════════════════════════════════

def test_el_prompt_ofrece_la_herramienta_sin_nombrar_a_cds_max_ni_ids():
    texto = agente.herramientas_del_prompt()
    assert "consultar_crm" in texto
    assert "consultar_crm" in agente.HERRAMIENTAS_QUE_HAY
    assert "cds_max" not in texto.lower() and "max" not in re.findall(r"\bMax\b", texto)
    assert _lo_que_nombra("X = " + repr(texto)) == []


def test_las_variables_nuevas_nacen_vacias_y_solo_se_nombran():
    fuente = (RAIZ / "config.py").read_text(encoding="utf-8")
    assert 'os.environ.get("CRM_MODULO_URL", "")' in fuente
    assert 'os.environ.get("CRM_MODULO_LLAVE", "")' in fuente
