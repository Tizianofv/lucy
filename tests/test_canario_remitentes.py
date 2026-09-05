# -*- coding: utf-8 -*-
"""El canario cuenta por REMITENTE y tiene dos señales, no una.

Lo que este archivo fija, y por qué:

  · El canario contaba por DOMINIO. De `popularenlinea.com` salen tres
    remitentes distintos: el de notificaciones (puro movimiento) y el de
    marketing, que manda ~130 publicidades al año y 8 cargos reales. Juntos en
    un contador, el canario gritaba "dejé de entender al Popular" casi todos
    los días. Una alarma que grita en falso se aprende a ignorar, y entonces no
    sirve el día que acierta.

  · La señal única ("llegaron correos y no salió NINGÚN movimiento") ahogaba el
    caso más claro de todos: un parser que calza con el correo y revienta al
    leerlo. Un remitente que parsea diez y revienta en uno no está mudo, así
    que de ese uno no se enteraba nadie nunca.

  · El aviso prometía "están guardados, no se perdió nada". Es falso: el correo
    que no calza con ningún parser se descarta sin pasar por la bandeja.

  · Ninguna señal miraba NUESTRA maquinaria. Credenciales vencidas o IMAP caído
    producen el mismo silencio que un día sin gastos.

Correr:  python3 tests/test_canario_remitentes.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import types
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules["psycopg"] = _psycopg
sys.modules["psycopg.rows"] = _rows
sys.modules["psycopg_pool"] = _pool

# Varios tests provocan a propósito un fallo que el código registra con
# `exc_info=True`. El traceback es ruido acá: lo que se comprueba es el aviso.
logging.disable(logging.CRITICAL)

import captura.consumos as consumos  # noqa: E402
import cerebro.bancos as bancos  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

# Los dos remitentes que hacen la diferencia, tal como están registrados.
MARKETING = "popularteinforma@popularenlinea.com"   # mixto: publicidad + 8 cargos
TRANSACCIONAL = "alertas@bhd.com.do"                # solo movimientos


# ── Dobles ───────────────────────────────────────────────────────────────

class _IMAPFalso:
    instancias: list = []
    correos_a_servir: list = []
    romper_busqueda = False

    def __init__(self, *a, **k):
        self.comandos: list[str] = []
        self.correos = list(_IMAPFalso.correos_a_servir)
        _IMAPFalso.instancias.append(self)

    def login(self, u, p):
        self.comandos.append("login")

    def select(self, box, readonly=False):
        self.comandos.append(f"select readonly={readonly}")

    def response(self, k):
        return (k, [b"1"])

    def uid(self, cmd, *args):
        self.comandos.append(f"uid {cmd}")
        if cmd == "search":
            if _IMAPFalso.romper_busqueda:
                raise OSError("conexión caída a mitad de la búsqueda")
            return ("OK", [b" ".join(str(u).encode() for u, _ in self.correos)])
        if cmd == "fetch":
            objetivo = int(args[0])
            for u, crudo in self.correos:
                if u == objetivo:
                    return ("OK", [(b"", crudo)])
        return ("OK", [None])

    def logout(self):
        self.comandos.append("logout")


class _Registro:
    def __init__(self):
        self.avisos: list[str] = []

    async def guardar_en_bandeja(self, **kw):
        self.avisos.append(kw.get("contenido_raw") or "")
        return len(self.avisos)

    async def guardar_movimiento(self, mov, bandeja_id=None, categoria=None):
        return 1

    async def leer_estado_consumos(self, cuenta):
        return None

    async def guardar_estado_consumos(self, *a, **k):
        return None

    async def listar_cuentas_propias(self):
        return []


def _montar(correos=()):
    reg = _Registro()
    _IMAPFalso.correos_a_servir = list(correos)
    _IMAPFalso.instancias = []
    _IMAPFalso.romper_busqueda = False
    consumos.imaplib = types.SimpleNamespace(IMAP4_SSL=_IMAPFalso)
    for n in ("guardar_en_bandeja", "guardar_movimiento", "leer_estado_consumos",
              "guardar_estado_consumos", "listar_cuentas_propias"):
        setattr(db, n, getattr(reg, n))
    config.CORREO_CUENTAS = [{"user": "tizianofv@gmail.com", "pass": "x"}]
    consumos._ultimo_aviso.clear()
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now()
    return reg


def _eml(remitente: str, asunto: str, cuerpo: str = "hola") -> bytes:
    """Un correo mínimo pero real: `email.message_from_bytes` lo tiene que
    desarmar igual que a uno de un banco."""
    return (f"From: Banco <{remitente}>\r\n"
            f"Subject: {asunto}\r\n"
            f"Date: Wed, 02 Sep 2026 08:00:00 -0400\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{cuerpo}\r\n").encode()


def _correr(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── EL caso que pedía el encargo: publicidad no, asunto cambiado sí ───────

def test_la_publicidad_del_popular_no_dispara_la_alerta():
    """El falso positivo que hizo falta arreglar. `popularteinforma@` manda
    ~130 publicidades al año por 8 cargos reales: que ninguno de sus correos de
    hoy calce con un parser es su estado NORMAL, no una avería."""
    reg = _montar([(1, _eml(MARKETING, "Descubre las promociones de septiembre")),
                   (2, _eml(MARKETING, "Tu resumen de beneficios"))])
    res = _correr(consumos.revisar())
    assert res.sin_ruta.get(MARKETING) == 2, (
        f"el conteo por remitente no cuadra: {res.sin_ruta}")
    assert res.remitentes_mudos() == [], (
        f"gritó por la publicidad del Popular: {res.remitentes_mudos()}")
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 0
    assert reg.avisos == [], reg.avisos


def test_un_asunto_cambiado_en_un_remitente_transaccional_si_dispara():
    """La otra mitad del mismo test: la alarma tiene que seguir sonando donde
    importa. De `alertas@bhd.com.do` solo llegan movimientos, así que un correo
    suyo que no calza con ningún parser es que cambió el asunto o apareció un
    tipo de aviso que no cubrimos."""
    reg = _montar([(1, _eml(TRANSACCIONAL, "Asunto que nadie ha visto nunca"))])
    res = _correr(consumos.revisar())
    assert res.remitentes_mudos() == [TRANSACCIONAL], (
        f"no vio el asunto cambiado: sin_ruta={res.sin_ruta} "
        f"enrutados={res.enrutados}")
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1
    assert TRANSACCIONAL in reg.avisos[0]


def test_los_dos_a_la_vez_avisan_por_uno_solo():
    """El mismo día, en la misma pasada: publicidad del Popular y un asunto
    cambiado del BHD. Un canario por dominio los habría mezclado."""
    reg = _montar([(1, _eml(MARKETING, "Promociones de septiembre")),
                   (2, _eml(TRANSACCIONAL, "Asunto que nadie ha visto nunca"))])
    res = _correr(consumos.revisar())
    assert res.remitentes_mudos() == [TRANSACCIONAL]
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1
    assert MARKETING not in reg.avisos[0]


# ── Señal A: un parser que revienta avisa SIEMPRE ────────────────────────

def test_un_parser_que_revienta_avisa_aunque_los_demas_correos_salgan_bien():
    """La señal que estaba ahogada. Con la regla vieja —"llegaron correos y no
    salió NINGÚN movimiento"— un remitente que parsea diez y revienta en uno no
    era mudo, y de ese uno no se enteraba nadie jamás."""
    reg = _montar()
    res = consumos.Resumen()
    res.enrutados = {TRANSACCIONAL: 11}
    res.producidos = {TRANSACCIONAL: 10}
    res.reventados = {TRANSACCIONAL: 1}
    res.fallos = [consumos.Fallo(TRANSACCIONAL, "buzón#12 [Consumo]: no encontré <tbody>")]
    assert res.remitentes_mudos() == [], "no está mudo: diez salieron bien"
    assert res.remitentes_reventados() == [TRANSACCIONAL]
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1
    assert "no encontré <tbody>" in reg.avisos[0]


def test_la_senal_de_reventado_no_mira_la_clase_del_remitente():
    """Marcar un remitente `mixto` lo calla para "no entendí nada", NO para "tu
    parser reventó". Si no fuera así, marcar mixto a `popularteinforma@`
    perdería los 8 cargos reales al año en silencio — que es exactamente lo que
    la decisión de no desregistrarlo existe para evitar."""
    reg = _montar()
    res = consumos.Resumen()
    res.enrutados = {MARKETING: 1}
    res.reventados = {MARKETING: 1}
    res.fallos = [consumos.Fallo(MARKETING, "buzón#9 [cargo comisión]: monto ambiguo")]
    assert bancos.clase_de_remitente(MARKETING) == "mixto"
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1, (
        "un cargo real del Popular que revienta tiene que avisar igual")
    assert "monto ambiguo" in reg.avisos[0]


def test_cada_senal_tiene_su_propio_candado_diario():
    """Un aviso por remitente, por señal y por día. Si las dos señales
    compartieran candado, la primera taparía a la otra hasta mañana."""
    reg = _montar()
    res = consumos.Resumen()
    res.sin_ruta = {TRANSACCIONAL: 3}
    res.reventados = {"notificaciones@banesco.com.do": 1}
    res.enrutados = {"notificaciones@banesco.com.do": 1}
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 2
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 0, "avisó dos veces"


# ── 2.6 La atribución del error ──────────────────────────────────────────

def test_el_error_se_elige_por_igualdad_de_remitente():
    reg = _montar()
    res = consumos.Resumen()
    res.enrutados = {TRANSACCIONAL: 1}
    res.reventados = {TRANSACCIONAL: 1}
    res.fallos = [
        consumos.Fallo("notificaciones@banesco.com.do", "ERROR DE BANESCO"),
        consumos.Fallo(TRANSACCIONAL, "ERROR DEL BHD"),
    ]
    _correr(consumos.avisar_si_hay_bancos_mudos(res))
    assert "ERROR DEL BHD" in reg.avisos[0]
    assert "ERROR DE BANESCO" not in reg.avisos[0], (
        "pegó el error de otro banco en la alarma de este")


def test_sin_fallo_propio_no_se_pega_el_de_otro():
    """El fallback viejo agarraba `fallos[0]`, de cualquier banco. Pegar el
    error de otro banco es peor que no mostrar ninguno: manda a arreglar lo que
    no está roto. Si no hay error para este remitente, eso ya es el dato."""
    reg = _montar()
    res = consumos.Resumen()
    res.sin_ruta = {TRANSACCIONAL: 2}
    res.fallos = [consumos.Fallo("notificaciones@banesco.com.do", "ERROR DE BANESCO")]
    _correr(consumos.avisar_si_hay_bancos_mudos(res))
    assert "ERROR DE BANESCO" not in reg.avisos[0], (
        "el fallback volvió: la alarma de un banco lleva el error de otro")
    assert res.error_de(TRANSACCIONAL) == ""


# ── 2.3 El texto que mentía ──────────────────────────────────────────────

def test_el_aviso_no_promete_que_no_se_perdio_nada():
    """Era falso en las tres partes: el correo sin parser no entra en bandeja,
    no se guarda el cuerpo y no se guardan los adjuntos."""
    reg = _montar()
    res = consumos.Resumen()
    res.sin_ruta = {TRANSACCIONAL: 4}
    _correr(consumos.avisar_si_hay_bancos_mudos(res))
    texto = reg.avisos[0].lower()
    for mentira in ("no se perdió nada", "están guardados"):
        assert mentira not in texto, f"volvió la promesa falsa: {mentira!r}"
    assert "se descartaron" in texto
    assert "siguen en gmail, sin marcar y sin borrar" in texto


def test_todos_los_avisos_conservan_el_bloque_de_no_adivinar():
    """Nació de un aviso del 31-ago que conjeturó mal la causa. Vale para las
    tres alarmas de este módulo, no solo para la que ya existía."""
    reg = _montar()
    res = consumos.Resumen()
    res.sin_ruta = {TRANSACCIONAL: 1}
    res.reventados = {"notificaciones@banesco.com.do": 1}
    res.enrutados = {"notificaciones@banesco.com.do": 1}
    _correr(consumos.avisar_si_hay_bancos_mudos(res))
    consumos._ultima_cosecha = datetime.now() - timedelta(hours=99)
    _correr(consumos.avisar_si_no_hay_latido())
    assert len(reg.avisos) == 3, f"esperaba 3 avisos, hay {len(reg.avisos)}"
    for aviso in reg.avisos:
        assert "NO DIGAS POR QUÉ PASÓ" in aviso, aviso[:120]
        for conjetura in ("casi seguro cambiaron", "seguramente"):
            assert conjetura not in aviso.split("NO DIGAS POR QUÉ PASÓ")[0]


# ── 2.4 El latido de la cosecha ──────────────────────────────────────────

def test_el_latido_calla_mientras_la_cosecha_corre():
    _montar()
    consumos._ultima_cosecha = datetime.now() - timedelta(hours=consumos.LATIDO_HORAS - 1)
    assert _correr(consumos.avisar_si_no_hay_latido()) == 0


def test_el_latido_avisa_a_las_seis_horas_sin_cosechar():
    """Ninguna otra señal cubre esto: si no se puede abrir el buzón no llega
    ningún correo, y "no llegó nada" se lee igual que "no gastaste nada"."""
    reg = _montar()
    consumos._ultima_cosecha = datetime.now() - timedelta(
        hours=consumos.LATIDO_HORAS, minutes=1)
    assert _correr(consumos.avisar_si_no_hay_latido()) == 1
    assert _correr(consumos.avisar_si_no_hay_latido()) == 0, "avisó dos veces el mismo día"
    assert "no consigo revisar ningún buzón" in reg.avisos[0]


def test_el_latido_tambien_cubre_no_haber_cosechado_nunca():
    """Credenciales mal puestas en un despliegue nuevo: nunca hubo una cosecha
    buena, así que no hay 'última' contra la que medir. Se mide contra el
    arranque, o el aviso no saldría jamás."""
    reg = _montar()
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now() - timedelta(hours=consumos.LATIDO_HORAS + 1)
    assert _correr(consumos.avisar_si_no_hay_latido()) == 1
    assert "ni una sola vez" in reg.avisos[0]


def test_una_cosecha_buena_reinicia_el_latido():
    _montar([(1, _eml(TRANSACCIONAL, "Asunto cualquiera"))])
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now() - timedelta(hours=99)
    _correr(consumos.revisar())
    assert consumos._ultima_cosecha is not None, "la cosecha buena no marcó latido"
    assert _correr(consumos.avisar_si_no_hay_latido()) == 0


def test_una_busqueda_rota_en_todos_los_remitentes_no_pasa_por_dia_tranquilo():
    """Con la búsqueda rota se devolvían cero correos y la pasada se daba por
    buena: el latido se marcaba, el canario no veía nada, y el buzón quedaba
    ciego sin que nada lo dijera."""
    _montar([(1, _eml(TRANSACCIONAL, "Consumo"))])
    _IMAPFalso.romper_busqueda = True
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now() - timedelta(hours=99)
    res = _correr(consumos.revisar())
    assert res.fallos, "una búsqueda rota en todos los remitentes no dejó fallo"
    assert consumos._ultima_cosecha is None, (
        "marcó latido con el buzón que no se pudo consultar")
    assert _correr(consumos.avisar_si_no_hay_latido()) == 1


def test_sin_cuentas_configuradas_el_latido_no_grita():
    """Un entorno sin buzones no tiene nada que cosechar. Pedirle latido sería
    la tercera alarma que grita en falso."""
    _montar()
    config.CORREO_CUENTAS = []
    consumos._ultima_cosecha = None
    consumos._arranque = datetime.now() - timedelta(hours=99)
    assert _correr(consumos.avisar_si_no_hay_latido()) == 0


# ── 2.2 La marca por remitente ───────────────────────────────────────────

def test_solo_popularteinforma_esta_marcado_mixto():
    """El único verificado, y el que causaba el falso positivo. Marcar mal un
    remitente lo deja mudo para siempre: eso no se hace por parecido."""
    mixtos = [r for r in bancos.remitentes_registrados()
              if bancos.clase_de_remitente(r) == "mixto"]
    assert mixtos == [MARKETING], f"clases mal repartidas: {mixtos}"


def test_los_otros_dos_del_popular_siguen_transaccionales():
    for rem in ("notificaciones@popularenlinea.com",
                "pagoselectronicos@popularenlinea.com"):
        assert bancos.clase_de_remitente(rem) == "transaccional", rem


def test_popularteinforma_sigue_registrado():
    """Desregistrarlo callaría la alarma al precio de perder ocho cargos reales
    al año. La decisión fue marcarlo, no sacarlo."""
    assert MARKETING in list(bancos.remitentes_registrados())
    assert bancos.buscar_parser(
        MARKETING, "Aviso cargo comisión por bajo balance") is not None


def test_un_remitente_sin_marcar_avisa():
    """El default es el lado seguro. Quien agregue un banco nuevo y no piense en
    esto se queda con el remitente que AVISA, no con el mudo."""
    assert bancos.clase_de_remitente("nuevo@bancodelfuturo.com") == "transaccional"


def test_una_clase_inventada_revienta_en_vez_de_elegir_por_defecto():
    """Vocabulario cerrado, como los demás de contrato.py. Caer en un default
    silencioso acá es exactamente cómo un remitente se queda mudo sin que nadie
    lo haya decidido."""
    try:
        bancos.registrar("x@y.com", lambda c: [], clase="semi-publicitario")
    except bancos.ErrorDeParseo:
        return
    raise AssertionError("aceptó una clase que no existe")


# ── Señal C: la base rechazó la fila ─────────────────────────────────────
#
# La más callada de las tres. El correo se entendió (no hay ErrorDeParseo, así
# que la señal A no la ve) y el remitente enrutó (así que la B tampoco). Hasta
# el 4-sep-2026 el rechazo subía crudo hasta el `except Exception` del bucle de
# cerebro/interpretar.py: un `log.warning` en Railway y nada más, sin llegar
# nunca a `guardar_estado_consumos` — o sea con el cursor de UID de ese buzón
# sin avanzar, releyendo el mismo correo malo cada 15 minutos para siempre.

BANRESERVAS = "notificaciones@banreservas.com"
CONSUMO_BANRESERVAS = (
    "Notificación de Consumo Su tarjeta VISA PLATINUM ••8110 presenta un "
    "consumo. Monto: DOP 254.90 Estado: APROBADO Comercio: SM NACIONAL "
    "MAXIMO GOM SANTO DOMINGODO Fecha de transacción: 17/04/2026 10:28 AM "
    "Número de aprobación: 299209 Recibido por los valores indicados")


def test_una_fila_rechazada_por_la_base_no_mata_la_pasada_y_avisa():
    """Lo que cierra el agujero: el movimiento que no se puede guardar deja
    rastro y avisa, en vez de desaparecer."""
    reg = _montar([(1, _eml(BANRESERVAS, "Notificaciones Banreservas",
                            CONSUMO_BANRESERVAS))])

    async def _rechaza(mov, bandeja_id=None, categoria=None):
        raise db.MovimientoRechazado(
            "la base rechazó el movimiento de banreservas "
            "(tipo=gasto, estado=reversada): CheckViolation: violates check "
            'constraint "movimientos_estado_valido"')

    db.guardar_movimiento = _rechaza

    res = _correr(consumos.revisar())          # no revienta
    assert res.enrutados.get(BANRESERVAS) == 1, res.enrutados
    assert res.reventados.get(BANRESERVAS) is None, (
        "no es un fallo de parseo: el correo se entendió entero")
    assert res.rechazados.get(BANRESERVAS) == 1, res.rechazados
    assert res.remitentes_rechazados() == [BANRESERVAS]
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1
    # avisos[0] es el CUERPO CRUDO del correo: se guarda antes de parsear
    # (INVARIANTE 1), así que el dato no se pierde aunque la fila no entre. El
    # aviso del canario es el último.
    assert "VISA PLATINUM" in reg.avisos[0], (
        "el crudo tiene que quedar guardado igual: es de donde se recupera")
    assert BANRESERVAS in reg.avisos[-1], [a[:60] for a in reg.avisos]
    assert "movimientos_estado_valido" in reg.avisos[-1], (
        "el aviso tiene que llevar el error real, no una conjetura")


def test_una_fila_rechazada_no_impide_que_avance_el_cursor():
    """La pasada sigue: las demás filas entran y el cursor de UID se guarda. Un
    correo malo no puede dejar la cuenta entera sin ingerir."""
    _montar([(7, _eml(BANRESERVAS, "Notificaciones Banreservas",
                      CONSUMO_BANRESERVAS))])
    guardado = {}

    async def _rechaza(mov, bandeja_id=None, categoria=None):
        raise db.MovimientoRechazado("la base rechazó el movimiento")

    async def _cursor(cuenta, uidv, uid, desde, reiniciar=False):
        guardado[cuenta] = uid

    db.guardar_movimiento = _rechaza
    db.guardar_estado_consumos = _cursor
    _correr(consumos.revisar())
    assert guardado.get("tizianofv@gmail.com") == 7, guardado


def test_un_fallo_de_conexion_no_se_disfraza_de_fila_mala():
    """Lo contrario, y es lo que hace que el anterior valga. Si la base no
    responde hay que PARAR sin guardar el cursor: darlo por bueno se saltaría
    correos que nunca se guardaron."""
    _montar([(1, _eml(BANRESERVAS, "Notificaciones Banreservas",
                      CONSUMO_BANRESERVAS))])

    async def _caida(mov, bandeja_id=None, categoria=None):
        raise OSError("conexión caída")

    db.guardar_movimiento = _caida
    try:
        _correr(consumos.revisar())
    except OSError:
        return
    raise AssertionError("la ingesta se tragó un fallo de conexión")


if __name__ == "__main__":
    fallidos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {nombre}")
        except AssertionError as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {e or 'assert falló'}")
        except Exception as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {type(e).__name__}: {e}")
    print(f"\n{'FALLARON ' + str(fallidos) if fallidos else 'Todo verde'}")
    sys.exit(1 if fallidos else 0)
