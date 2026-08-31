"""Tests de la ingesta de movimientos bancarios (captura/consumos.py).

Herméticos: se stubea psycopg, imaplib y config. Lo que se prueba no es que
IMAP funcione, sino los TRES INVARIANTES que este módulo no puede romper:

  1. El crudo se guarda ANTES de parsear.
  2. No se marca ningún correo como leído.
  3. El cursor es propio y nunca retrocede.

Y se alimenta con correos REALES del corpus, no con texto inventado: la ingesta
solo sirve si funciona sobre lo que los bancos mandan de verdad.

Correr:  python3 tests/test_consumos.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import types
from datetime import date

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

import captura.consumos as consumos  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ── Doble de la sesión IMAP: registra TODO lo que se le pide ─────────────

class _IMAPFalso:
    """Anota cada comando para poder afirmar qué NO se hizo."""

    instancias: list = []

    def __init__(self, *a, **k):
        self.comandos: list[str] = []
        self.readonly = None
        self.correos: list[tuple[int, bytes]] = list(_IMAPFalso.correos_a_servir)
        _IMAPFalso.instancias.append(self)

    correos_a_servir: list = []

    def login(self, u, p):
        self.comandos.append("login")

    def select(self, box, readonly=False):
        self.readonly = readonly
        self.comandos.append(f"select readonly={readonly}")

    def response(self, k):
        return (k, [b"1"])

    def uid(self, cmd, *args):
        self.comandos.append(f"uid {cmd} {' '.join(str(a) for a in args)}")
        if cmd == "search":
            return ("OK", [b" ".join(str(u).encode() for u, _ in self.correos)])
        if cmd == "fetch":
            objetivo = int(args[0])
            for u, crudo in self.correos:
                if u == objetivo:
                    return ("OK", [(b"", crudo)])
        if cmd == "store":
            raise AssertionError("¡marcó un correo! la ingesta es de solo lectura")
        return ("OK", [None])

    def logout(self):
        self.comandos.append("logout")


class _Registro:
    """Doble de la base: anota el ORDEN de las escrituras."""

    def __init__(self):
        self.orden: list[str] = []
        self.movimientos: list = []
        self.estado: dict = {}
        self.huellas: set = set()

    async def guardar_en_bandeja(self, **kw):
        self.orden.append(f"bandeja:{kw.get('origen')}")
        return len(self.orden)

    async def guardar_movimiento(self, mov, bandeja_id=None, categoria=None):
        self.orden.append("movimiento")
        h = mov.clave_dedupe()
        if h in self.huellas:
            return None
        self.huellas.add(h)
        self.movimientos.append(mov)
        return len(self.movimientos)

    async def leer_estado_consumos(self, cuenta):
        return self.estado.get(cuenta)

    async def guardar_estado_consumos(self, cuenta, uidv, uid, desde,
                                      reiniciar=False):
        prev = self.estado.get(cuenta, {}).get("ultimo_uid", 0)
        self.estado[cuenta] = {"uidvalidity": uidv,
                               "ultimo_uid": uid if reiniciar else max(prev, uid),
                               "desde_fecha": desde}

    async def listar_cuentas_propias(self):
        return []


def _montar(correos):
    reg = _Registro()
    _IMAPFalso.correos_a_servir = correos
    _IMAPFalso.instancias = []
    consumos.imaplib = types.SimpleNamespace(IMAP4_SSL=_IMAPFalso)
    for n in ("guardar_en_bandeja", "guardar_movimiento", "leer_estado_consumos",
              "guardar_estado_consumos", "listar_cuentas_propias"):
        setattr(db, n, getattr(reg, n))
    config.CORREO_CUENTAS = [{"user": "rosilisr04@gmail.com", "pass": "x"}]
    return reg


def _correos_reales(n=6):
    """Correos del corpus que SÍ tienen parser registrado."""
    import email as _e
    from email.header import decode_header
    import cerebro.bancos as B

    def cab(v):
        if not v:
            return ""
        return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes)
                       else p for p, e in decode_header(v))
    salida, uid = [], 100
    for f in sorted(FIXTURES.rglob("*.eml")):
        crudo = f.read_bytes()
        m = _e.message_from_bytes(crudo)
        frm = cab(m.get("From"))
        addr = frm.split("<")[-1].strip("> ").lower() if "<" in frm else frm.lower()
        if B.buscar_parser(addr, cab(m.get("Subject")).strip()) is None:
            continue
        salida.append((uid, crudo))
        uid += 1
        if len(salida) >= n:
            break
    return salida


def _correr(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── Invariante 1: crudo antes de parsear ─────────────────────────────────

def test_el_crudo_se_guarda_antes_del_movimiento():
    """La regla de oro del proyecto. Si un banco cambia su plantilla, el correo
    ya está en bandeja y el parser se arregla contra un dato que no se perdió."""
    if not FIXTURES.exists():
        return
    reg = _montar(_correos_reales(3))
    _correr(consumos.revisar())
    assert reg.orden, "no escribió nada"
    assert reg.orden[0].startswith("bandeja:"), reg.orden[:3]
    for i, x in enumerate(reg.orden):
        if x == "movimiento":
            assert reg.orden[i - 1].startswith("bandeja:"), (
                f"guardó un movimiento sin bandeja delante: {reg.orden[:i+1]}")


def test_el_crudo_se_guarda_aunque_el_parser_reviente():
    """El caso que justifica el invariante: parser roto, correo a salvo."""
    if not FIXTURES.exists():
        return
    reg = _montar(_correos_reales(2))
    import cerebro.bancos as B
    original = B.buscar_parser

    def _roto(rem, asunto):
        p = original(rem, asunto)
        if p is None:
            return None
        def _revienta(correo):
            from cerebro.bancos.contrato import ErrorDeParseo
            raise ErrorDeParseo("el banco cambió la plantilla")
        return _revienta
    consumos.bancos.buscar_parser = _roto
    try:
        res = _correr(consumos.revisar())
    finally:
        consumos.bancos.buscar_parser = original
    assert res.guardados_crudos >= 2, "no guardó el crudo"
    assert res.extraidos == 0 and len(res.fallos) >= 2


# ── Invariante 2: no se marca nada como leído ────────────────────────────

def test_la_sesion_es_readonly_y_usa_peek():
    if not FIXTURES.exists():
        return
    _montar(_correos_reales(2))
    _correr(consumos.revisar())
    ses = _IMAPFalso.instancias[0]
    assert ses.readonly is True, "la sesión IMAP no es de solo lectura"
    assert any("BODY.PEEK" in c for c in ses.comandos), ses.comandos
    assert not any(c.startswith("uid store") for c in ses.comandos)


# ── Invariante 3: el cursor es propio y no retrocede ─────────────────────

def test_el_cursor_avanza_y_no_retrocede():
    if not FIXTURES.exists():
        return
    reg = _montar(_correos_reales(3))
    _correr(consumos.revisar())
    alto = reg.estado["rosilisr04@gmail.com"]["ultimo_uid"]
    assert alto >= 102
    _correr(reg.guardar_estado_consumos("rosilisr04@gmail.com", 1, 5,
                                        date(2026, 9, 1)))
    assert reg.estado["rosilisr04@gmail.com"]["ultimo_uid"] == alto, (
        "el cursor retrocedió")


def test_arranca_en_septiembre():
    """Decisión de Tiziano: esto es a futuro, no al pasado."""
    if not FIXTURES.exists():
        return
    _montar(_correos_reales(1))
    _correr(consumos.revisar())
    busquedas = [c for c in _IMAPFalso.instancias[0].comandos
                 if c.startswith("uid search")]
    assert busquedas, "no buscó nada"
    assert all("01-Sep-2026" in c for c in busquedas), busquedas[0]


# ── Regresiones: lo que encontró el verificador el 30-ago-2026 ───────────

def test_el_cursor_no_pasa_de_lo_que_llego_a_bandeja():
    """EL invariante que faltaba. `tope` se calculaba sobre TODOS los correos
    nuevos, pero solo se piden MAX_POR_PASADA. Con GREATEST en el UPDATE, los
    que quedaban fuera del corte se saltaban PARA SIEMPRE: no entraban en
    bandeja, no daban fallo, y no contaban en el canario. Cincuenta consumos
    desaparecían sin rastro en ninguna de las tres señales."""
    if not FIXTURES.exists():
        return
    base = _correos_reales(1)[0][1]
    muchos = [(1000 + i, base) for i in range(consumos.MAX_POR_PASADA + 50)]
    reg = _montar(muchos)
    res = _correr(consumos.revisar())
    cursor = reg.estado["rosilisr04@gmail.com"]["ultimo_uid"]
    tope_cosechado = 1000 + consumos.MAX_POR_PASADA - 1
    assert cursor <= tope_cosechado, (
        f"el cursor quedó en {cursor} pero solo se cosechó hasta "
        f"{tope_cosechado}: {cursor - tope_cosechado} correos saltados")
    # Y la pasada siguiente tiene que volver por ellos.
    res2 = _correr(consumos.revisar())
    assert res2.vistos > 0, "la segunda pasada no volvió por los que faltaban"


def test_uidvalidity_nueva_reinicia_el_cursor_de_verdad():
    """El buzón se renumeró: los UID viejos no significan nada. Se asignaba
    desde_uid=0 DESPUÉS de cosechar, así que no se usaba; y se persistía el
    uidvalidity nuevo, con lo que la rama no volvía a dispararse jamás. La
    cuenta quedaba ciega de forma permanente y silenciosa."""
    if not FIXTURES.exists():
        return
    base = _correos_reales(1)[0][1]
    reg = _montar([(7, base), (8, base), (9, base)])
    reg.estado["rosilisr04@gmail.com"] = {
        "uidvalidity": 999, "ultimo_uid": 5000, "desde_fecha": date(2026, 9, 1)}
    res = _correr(consumos.revisar())
    assert res.vistos >= 1, (
        "con el buzón renumerado no vio ningún correo: la cuenta quedó ciega")
    assert reg.estado["rosilisr04@gmail.com"]["ultimo_uid"] <= 9, (
        "el cursor se quedó en el UID viejo, que ya no significa nada")


def test_el_canario_ve_los_correos_sin_parser():
    """Si un banco cambia el ASUNTO (no la plantilla), buscar_parser devuelve
    None, el banco desaparece de por_banco_vistos y bancos_mudos() da []. El
    canario está para detectar que dejamos de entender a un banco: tiene que
    contar los correos de un remitente registrado aunque no calce el asunto."""
    if not FIXTURES.exists():
        return
    base = _correos_reales(1)[0][1]
    reg = _montar([(500, base)])
    original = consumos.bancos.buscar_parser
    consumos.bancos.buscar_parser = lambda rem, asunto: None
    try:
        res = _correr(consumos.revisar())
    finally:
        consumos.bancos.buscar_parser = original
    assert res.bancos_mudos(), (
        "un remitente registrado cuyos correos ya no calzan ningún asunto tiene "
        "que salir en el canario")


# ── Comportamiento sobre correos reales ──────────────────────────────────

def test_extrae_movimientos_de_correos_reales():
    if not FIXTURES.exists():
        return
    reg = _montar(_correos_reales(8))
    res = _correr(consumos.revisar())
    print(f"     ({res.vistos} vistos, {res.extraidos} movimientos, "
          f"{res.duplicados} duplicados, {len(res.fallos)} fallos)")
    assert res.extraidos >= 6, f"solo extrajo {res.extraidos} de {res.vistos}"
    assert not res.fallos, res.fallos[:3]


def test_el_canario_detecta_un_banco_mudo():
    """vistos > 0 y extraidos == 0 para un banco es la señal de que cambió su
    plantilla. Es lo único que separa "no gastaste nada" de "dejé de entender
    los correos"."""
    res = consumos.Resumen()
    res.por_banco_vistos = {"bhd.com.do": 12, "apap.com.do": 3}
    res.por_banco_extraidos = {"apap.com.do": 3}
    assert res.bancos_mudos() == ["bhd.com.do"]
    res.por_banco_extraidos["bhd.com.do"] = 12
    assert res.bancos_mudos() == []


# ── El canario avisa ─────────────────────────────────────────────────────

def test_el_canario_avisa_una_vez_por_dia():
    """Un banco mudo tiene que avisar, y avisar UNA vez: el bucle corre cada 15
    minutos, así que sin throttle serían 96 avisos al día del mismo banco."""
    reg = _montar([])
    consumos._ultimo_aviso.clear()
    res = consumos.Resumen()
    res.por_banco_vistos = {"bhd.com.do": 12}
    res.por_banco_extraidos = {}
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 1
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 0, "avisó dos veces"
    assert len(reg.orden) == 1 and reg.orden[0] == "bandeja:banco"


def test_el_canario_callado_cuando_todo_va_bien():
    """Un canario que avisa sin motivo se aprende a ignorar."""
    _montar([])
    consumos._ultimo_aviso.clear()
    res = consumos.Resumen()
    res.por_banco_vistos = {"bhd.com.do": 12}
    res.por_banco_extraidos = {"bhd.com.do": 12}
    assert _correr(consumos.avisar_si_hay_bancos_mudos(res)) == 0


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
