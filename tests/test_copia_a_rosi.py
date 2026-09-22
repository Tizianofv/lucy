"""Copia a Rosi: lo que Lucy le manda al dueño también le llega a ella.

Diseño aprobado: `disenos/lucy-copia-a-rosi/DISENO.md` (21-sep-2026).
Decisiones de Tiziano ahí adentro: alcance = TODO lo que le llega al dueño;
el enlace del panel NO se copia; lo copiado SÍ queda en la memoria de la
conversación de la persona copiada.

LA PUERTA que se prueba acá es `telegram.Bot.send_message`
(`cerebro/copia_dueno.py`), no una lista de funciones de Lucy que le hablan
al dueño. Por eso la prueba de fondo (`test_un_bot_propio_tambien_copia`)
arma un `telegram.Bot(token=...)` a mano — la "tercera forma" que
`tests/test_cerrar_varias.py::test_ninguna_salida_de_atender_esquiva_la_puerta`
deja medido que esquiva cualquier vigilancia atada a un nombre de función o a
una instancia concreta — y comprueba que TAMBIÉN copia, porque el parche vive
en la CLASE.

Herméticos: nada de esto sale a la red. `telegram.Bot.send_message` se
reemplaza por un doble ANTES de instalar el parche de `copia_dueno`, así que
"la función real" que el parche cree estar envolviendo es, en esta suite, el
doble — nunca hay un POST HTTP de verdad.

Correr:  python3 -m pytest tests/test_copia_a_rosi.py
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "111")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import telegram  # noqa: E402

import captura.telegram as captura_telegram  # noqa: E402
import cerebro.agente as agente  # noqa: E402
import cerebro.copia_dueno as copia_dueno  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO   # 111, del entorno de arriba.
ROSI = 222
OTRO_CHAT = 333


# ── El arnés: `telegram.Bot.send_message` real, doblado, sin red ─────────
#
# `copia_dueno.instalar()` guarda `telegram.Bot.send_message` como "la
# función real". Acá se lo dobla ANTES de instalar, así que ese "real" es
# el doble, y ninguna llamada —ni la del dueño ni la de la copia— sale a
# internet. `bot` es una instancia REAL de `telegram.Bot` (no un objeto de
# mentira con su propio `send_message`): es lo que hace que esta prueba mida
# la puerta de verdad y no una imitación de ella.
@pytest.fixture
def puerta():
    pristino = telegram.Bot.send_message
    enviados: list[dict] = []

    async def _doble(self, chat_id, text, **kwargs):
        enviados.append({"self": self, "chat_id": chat_id, "text": text, **kwargs})
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


def _permitir(nombre_de_copia: str, chats_con_nombre: dict[int, str]):
    """Deja el sistema como si `chats_con_nombre` fueran quienes pueden
    entrar y `nombre_de_copia` fuera lo que dice COPIAS_DEL_DUENO.

    Toca atributos de `config` directamente — el fixture autouse
    `_devolver_los_modulos_a_su_sitio` de conftest.py los repone solo al
    terminar la prueba, es el mismo patrón que usa el resto de la suite.
    """
    config.NOMBRES_POR_CHAT = dict(chats_con_nombre)
    config.CHAT_IDS_PERMITIDOS = (DUENO,) + tuple(
        c for c in chats_con_nombre if c != DUENO)
    config._NOMBRES_DE_COPIA = tuple(
        n.strip() for n in nombre_de_copia.replace(";", ",").split(",") if n.strip())


# ── 1) La variable: vacía, con un nombre que no existe, con uno que sí ────

def test_variable_vacia_no_copia_a_nadie():
    _permitir("", {DUENO: "Tiziano", ROSI: "Rosi"})
    assert config.chats_de_copia() == ()


def test_nombre_que_no_existe_no_copia_a_nadie():
    _permitir("Alguien Que No Existe", {DUENO: "Tiziano", ROSI: "Rosi"})
    assert config.chats_de_copia() == ()


def test_copiarle_a_alguien_sin_acceso_no_copia_a_nadie():
    """"Rosi" está en NOMBRES_POR_CHAT pero NO en CHAT_IDS_PERMITIDOS: no
    puede entrar, así que `personas_del_panel()` no la lista y su nombre no
    resuelve — la misma regla que si el nombre estuviera mal escrito."""
    config.NOMBRES_POR_CHAT = {DUENO: "Tiziano", ROSI: "Rosi"}
    config.CHAT_IDS_PERMITIDOS = (DUENO,)   # Rosi NO puede entrar
    config._NOMBRES_DE_COPIA = ("Rosi",)
    assert config.chats_de_copia() == ()


def test_nombre_que_resuelve_copia_a_ese_chat():
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    assert config.chats_de_copia() == (ROSI,)


# ── 2) El envío al dueño sale dos veces, con el mismo texto ──────────────

async def test_un_mensaje_al_dueno_sale_dos_veces_mismo_texto(puerta, bot):
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    await bot.send_message(chat_id=DUENO, text="Hola, esto te lo mando a vos")

    assert len(puerta) == 2, f"tenían que ser 2 envíos, salieron {len(puerta)}"
    assert puerta[0]["chat_id"] == DUENO
    assert puerta[1]["chat_id"] == ROSI
    assert puerta[0]["text"] == puerta[1]["text"] == "Hola, esto te lo mando a vos"


async def test_la_copia_no_lleva_boton_ni_cita(puerta, bot):
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    markup = object()   # cualquier objeto sirve: no se inspecciona su forma.
    await bot.send_message(chat_id=DUENO, text="Con botón",
                            reply_markup=markup, reply_to_message_id=999)

    copia = puerta[1]
    assert copia["chat_id"] == ROSI
    assert "reply_markup" not in copia, "la copia no debería llevar el botón"
    assert "reply_to_message_id" not in copia, "la copia no debería citar nada"
    # Al dueño sí le llegaron completos.
    assert puerta[0]["reply_markup"] is markup
    assert puerta[0]["reply_to_message_id"] == 999


async def test_un_mensaje_a_otro_chat_no_copia(puerta, bot):
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    await bot.send_message(chat_id=OTRO_CHAT, text="Esto no es para el dueño")

    assert len(puerta) == 1, "un mensaje a otro chat no tiene que copiarse"
    assert puerta[0]["chat_id"] == OTRO_CHAT


async def test_sin_copias_configuradas_el_dueno_recibe_una_sola_vez(puerta, bot):
    _permitir("", {DUENO: "Tiziano", ROSI: "Rosi"})
    await bot.send_message(chat_id=DUENO, text="Nadie más está en la lista")
    assert len(puerta) == 1


# ── 3) Si falla la copia, al dueño le llegó igual ─────────────────────────

async def test_si_falla_la_copia_el_envio_al_dueno_no_falla(puerta, bot):
    """OJO con esta prueba: tiene que seguir pasando por `_send_message_con_copia`
    (lo que `puerta` deja instalado en `telegram.Bot.send_message`). Si acá se
    reasignara TAMBIÉN `telegram.Bot.send_message`, se estaría reemplazando el
    parche entero y la prueba dejaría de medir su try/except — mide otra cosa
    y pasa igual. Por eso se toca únicamente `copia_dueno._original_send_message`,
    que es la función que el parche llama por dentro."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})

    async def _rompe_con_rosi(self, chat_id, text, **kwargs):
        if chat_id == ROSI:
            raise RuntimeError("Telegram rechazó el envío a Rosi")
        puerta.append({"self": self, "chat_id": chat_id, "text": text, **kwargs})
        return types.SimpleNamespace(message_id=1)

    copia_dueno._original_send_message = _rompe_con_rosi

    # No debe propagar la excepción: al dueño ya le llegó el suyo.
    resultado = await bot.send_message(chat_id=DUENO, text="Este sí llega")
    assert resultado is not None
    assert len(puerta) == 1, "el envío al dueño quedó registrado"
    assert puerta[0]["chat_id"] == DUENO


async def test_si_falla_el_envio_al_dueno_no_hay_copia(puerta, bot):
    """Al revés: si Telegram rechaza el mensaje AL DUEÑO, la excepción se
    propaga (como hoy) y no se manda nada a nadie más — no hay de dónde
    copiar un mensaje que nunca salió. Igual que arriba: solo se toca
    `copia_dueno._original_send_message`, no `telegram.Bot.send_message`."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})

    async def _rompe_siempre(self, chat_id, text, **kwargs):
        raise telegram.error.BadRequest("mensaje inválido")

    copia_dueno._original_send_message = _rompe_siempre

    with pytest.raises(telegram.error.BadRequest):
        await bot.send_message(chat_id=DUENO, text="Este falla")
    assert puerta == []


# ── 4) El «✅ Recibí» sale por reply_text, y también pasa por la puerta ───

async def test_el_recibi_tambien_copia(puerta, monkeypatch):
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})

    async def _guardar_en_bandeja(**kw):
        return 42

    monkeypatch.setattr(db, "guardar_en_bandeja", _guardar_en_bandeja)

    chat = telegram.Chat(id=DUENO, type="private")
    msg = telegram.Message(message_id=7, date=datetime.now(timezone.utc),
                            chat=chat, text="hola Lucy")
    real_bot = telegram.Bot(token="123456:token-de-prueba")
    msg.set_bot(real_bot)

    await captura_telegram._capturar(msg, tipo_entrada="texto")

    assert len(puerta) == 2, ("el ✅ Recibí tenía que copiarse: "
                              f"salieron {len(puerta)} envíos")
    assert puerta[0]["chat_id"] == DUENO
    assert puerta[0]["text"] == "✅ Recibí (#42)"
    assert puerta[1]["chat_id"] == ROSI
    assert puerta[1]["text"] == "✅ Recibí (#42)"


# ── 5) El enlace del panel NO se copia ────────────────────────────────────

async def test_sin_copiar_suprime_la_copia(puerta, bot):
    """El mecanismo que usa la herramienta "panel": dentro del bloque, un
    envío al dueño no se copia."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    with copia_dueno.sin_copiar():
        await bot.send_message(chat_id=DUENO, text="Acá está el panel — vence en 10 minutos")

    assert len(puerta) == 1, "el enlace del panel no debía copiarse"
    assert puerta[0]["chat_id"] == DUENO


async def test_fuera_del_bloque_sin_copiar_vuelve_a_copiar(puerta, bot):
    """Que `sin_copiar()` no deje el interruptor pegado: un envío posterior,
    fuera del bloque, copia de nuevo."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    with copia_dueno.sin_copiar():
        await bot.send_message(chat_id=DUENO, text="uno")
    await bot.send_message(chat_id=DUENO, text="dos")

    assert len(puerta) == 3, f"esperaba 1 (sin copiar) + 2 (con copia) = 3, dio {len(puerta)}"
    assert [p["chat_id"] for p in puerta] == [DUENO, DUENO, ROSI]


async def test_la_herramienta_panel_no_copia_el_enlace(monkeypatch, puerta):
    """De punta a punta: `atender()` con la herramienta "panel" no copia el
    enlace, aunque COPIAS_DEL_DUENO esté puesta."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    monkeypatch.setattr(config, "PANEL_URL", "https://panel.ejemplo.test")

    class _AuthFalso:
        @staticmethod
        def puede_entrar(chat_id):
            return True

        @staticmethod
        def crear_token(chat_id):
            return "tok-123"

    sys.modules["web.auth"] = _AuthFalso

    async def _nada(*a, **k):
        return None

    async def _historial_vacio(*a, **k):
        return []

    async def _sin_pendiente(*a, **k):
        return None

    async def _sin_preferencias():
        return []

    async def _sin_areas():
        return []

    async def _sin_proyectos():
        return []

    monkeypatch.setattr(db, "buscar_esperando_respuesta", _sin_pendiente)
    monkeypatch.setattr(db, "ultimos_intercambios", _historial_vacio)
    monkeypatch.setattr(db, "listar_preferencias", _sin_preferencias)
    # `atender()` también trae la lista de áreas (encargo 4) con `db.areas()`;
    # se stubea vacía por el mismo motivo que `listar_preferencias`.
    monkeypatch.setattr(db, "areas", _sin_areas)
    # Y la de proyectos vivos (encargo 5), con `db.proyectos_vivos()`.
    monkeypatch.setattr(db, "proyectos_vivos", _sin_proyectos)
    monkeypatch.setattr(db, "guardar_respuesta", _nada)
    monkeypatch.setattr(db, "guardar_interpretacion", _nada)
    monkeypatch.setattr(db, "cambiar_estado", _nada)

    class _Completions:
        async def create(self, **kw):
            contenido = '{"herramienta": "panel", "argumentos": {}}'
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=contenido))])

    class _Chat:
        completions = _Completions()

    class _Cliente:
        chat = _Chat()

    monkeypatch.setattr(agente.motor, "cliente", _Cliente())

    real_bot = telegram.Bot(token="123456:token-de-prueba")
    fila = {"id": 1, "chat_id": DUENO, "tipo_entrada": "texto"}
    await agente.atender(fila, "dame el panel", real_bot)

    assert len(puerta) == 1, ("el enlace del panel tenía que salir UNA sola "
                              f"vez (sin copia); salieron {len(puerta)}")
    assert puerta[0]["chat_id"] == DUENO
    assert "panel.ejemplo.test" in puerta[0]["text"]
    del sys.modules["web.auth"]


# ── 6) La memoria: lo copiado queda anotado para la persona copiada ──────

async def test_la_copia_queda_anotada_en_la_memoria_de_rosi(puerta, bot, monkeypatch):
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    anotados = []

    async def _registrar_aviso(chat_id, texto, origen="despertador"):
        anotados.append({"chat_id": chat_id, "texto": texto, "origen": origen})
        return len(anotados)

    monkeypatch.setattr(db, "registrar_aviso", _registrar_aviso)

    await bot.send_message(chat_id=DUENO, text="Reunión a las 5")

    assert len(anotados) == 1, "la copia tiene que anotarse en la bandeja de Rosi"
    fila = anotados[0]
    assert fila["chat_id"] == ROSI
    assert fila["origen"] == "copia_dueno"
    # El texto GUARDADO lleva la marca (para que Lucy sepa que es una copia);
    # el texto ENVIADO por Telegram no la lleva (Tiziano: "nada cambiado").
    assert "Reunión a las 5" in fila["texto"]
    assert fila["texto"] != "Reunión a las 5", "en memoria tiene que ir marcado"
    assert puerta[1]["text"] == "Reunión a las 5", "por Telegram, sin marcar"


async def test_si_falla_la_copia_no_se_anota_nada_en_memoria(puerta, monkeypatch):
    """Si el envío a Rosi falla, no hay copia que recordar: no se llama a
    registrar_aviso para ella."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})
    anotados = []

    async def _registrar_aviso(chat_id, texto, origen="despertador"):
        anotados.append(chat_id)
        return 1

    monkeypatch.setattr(db, "registrar_aviso", _registrar_aviso)

    async def _rompe_con_rosi(self, chat_id, text, **kwargs):
        if chat_id == ROSI:
            raise RuntimeError("falló el envío a Rosi")
        return types.SimpleNamespace(message_id=1)

    copia_dueno._original_send_message = _rompe_con_rosi
    bot_real = telegram.Bot(token="123456:token-de-prueba")

    await bot_real.send_message(chat_id=DUENO, text="hola")
    assert anotados == [], "no hay nada que anotar si la copia no salió"


# ── 7) LA PRUEBA DE FONDO: un bot armado a mano también copia ────────────

async def test_un_bot_propio_tambien_copia(puerta):
    """La "tercera forma" de `test_cerrar_varias.py`: un `telegram.Bot`
    armado a mano, sin pasar por `main.py` ni por ningún nombre que una
    vigilancia por función pudiera reconocer. Si esto copia, es porque el
    parche vive en la CLASE `telegram.Bot`, no en un objeto ni en una
    lista de sitios conocidos — que es la garantía que pide el encargo:
    "la séptima llamada nace copiada"."""
    _permitir("Rosi", {DUENO: "Tiziano", ROSI: "Rosi"})

    mensajero = telegram.Bot(token=config.TELEGRAM_TOKEN)
    await mensajero.send_message(chat_id=DUENO, text="Un cliente cualquiera")

    assert len(puerta) == 2, (
        "un telegram.Bot armado a mano tenía que copiar igual que el de "
        f"main.py; salieron {len(puerta)}")
    assert puerta[1]["chat_id"] == ROSI


async def test_la_frontera_un_httpx_a_pelo_no_copia(puerta):
    """LA FRONTERA, medida y no solo dicha: algo que le hable a la API de
    Telegram SIN pasar por `telegram.Bot` —simulado acá con un objeto que
    NO es instancia de `telegram.Bot`— no tiene ningún `send_message` que
    parchar, y por lo tanto no copia. Esto no es un defecto de la puerta:
    es su límite, y este archivo lo mide en vez de prometer que no existe."""
    class _ClienteQueNoUsaLaLibreria:
        def __init__(self):
            self.llamadas = []

        async def post_directo(self, chat_id, text):
            self.llamadas.append((chat_id, text))

    cliente_ajeno = _ClienteQueNoUsaLaLibreria()
    await cliente_ajeno.post_directo(DUENO, "esto no pasó por telegram.Bot")

    assert cliente_ajeno.llamadas == [(DUENO, "esto no pasó por telegram.Bot")]
    assert puerta == [], "un camino que no usa telegram.Bot no puede copiar"
