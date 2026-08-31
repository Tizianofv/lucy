"""Tests de la guardia anti-duplicado de `movimientos` (db.guardar_movimiento).

Herméticos: se stubea psycopg igual que en test_crud_dedup.py, con una conexión
de mentira que modela el índice único parcial de la migración 001. No toca
ninguna base — de producción, menos.

Lo que se prueba no es que Postgres sepa hacer ON CONFLICT: es que la huella
que se le pasa sea la correcta, que el monto no pase por float, y que un
duplicado devuelva None en vez de reventar.

El caso real que motiva todo esto: Banco Popular manda la misma transacción dos
veces con 1 y 6 segundos de diferencia.

Correr:  python3 tests/test_movimientos_dedupe.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import types
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

# ── Stubs de psycopg ANTES de importar db.db ─────────────────────────────
_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")


class _FalsoPool:
    def __init__(self, *a, **k):
        pass


_pool.AsyncConnectionPool = _FalsoPool
sys.modules["psycopg"] = _psycopg
sys.modules["psycopg.rows"] = _rows
sys.modules["psycopg_pool"] = _pool

import db.db as db  # noqa: E402
from cerebro.bancos.contrato import Movimiento  # noqa: E402


class _Cursor:
    def __init__(self, fila):
        self._fila = fila

    async def fetchone(self):
        return self._fila


class _Conn:
    """Modela el índice único parcial: guarda las huellas ya insertadas."""

    def __init__(self, huellas):
        self.huellas = huellas
        self.sql_visto = ""
        self.args_vistos = ()

    async def execute(self, sql, args):
        self.sql_visto = " ".join(sql.split())
        self.args_vistos = args
        # La huella se localiza por su posición en la lista de COLUMNAS del
        # INSERT, no por ser el último parámetro. Asumir "el último" hizo que
        # este doble se rompiera al añadir la columna `banco`, y un doble que se
        # rompe al crecer el esquema esconde el fallo real detrás de ruido.
        cols = sql.split("(", 1)[1].split(")", 1)[0]
        cols = [c.strip() for c in cols.replace("\n", " ").split(",")]
        huella = args[cols.index("hash_contenido")]
        if huella is not None and huella in self.huellas:
            return _Cursor(None)          # ON CONFLICT DO NOTHING → sin RETURNING
        self.huellas.add(huella)
        return _Cursor((len(self.huellas),))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self):
        self.huellas = set()
        self.ultima = None

    def connection(self):
        self.ultima = _Conn(self.huellas)
        return self.ultima


def _mov(**kw):
    base = dict(banco="popular", canal="transferencia", tipo="ingreso",
                fecha=datetime(2026, 8, 10, 0, 0), monto=Decimal("17000.00"),
                moneda="DOP", contraparte="(Popular no informa quién envió)",
                estado="aprobada", referencia="Popular · cuenta ••9142")
    base.update(kw)
    return Movimiento(**base)


def _correr(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── Tests ────────────────────────────────────────────────────────────────

def test_primero_entra_segundo_no():
    """El caso real de Popular: la misma transacción notificada dos veces."""
    db.pool = _Pool()
    m = _mov()
    primero = _correr(db.guardar_movimiento(m))
    segundo = _correr(db.guardar_movimiento(m))
    assert primero is not None, "el primero tiene que guardarse"
    assert segundo is None, "el duplicado devuelve None, no revienta"


def test_dos_compras_distintas_el_mismo_dia_entran_las_dos():
    """La hora está en la huella justamente para esto: dos cafés del mismo día
    en el mismo sitio son dos gastos, no un duplicado."""
    db.pool = _Pool()
    a = _mov(fecha=datetime(2026, 8, 10, 9, 0), contraparte="CAFETERIA X",
             monto=Decimal("150.00"), tipo="gasto")
    b = _mov(fecha=datetime(2026, 8, 10, 16, 30), contraparte="CAFETERIA X",
             monto=Decimal("150.00"), tipo="gasto")
    assert _correr(db.guardar_movimiento(a)) is not None
    assert _correr(db.guardar_movimiento(b)) is not None, (
        "misma fecha y monto pero distinta hora: son dos movimientos")


def test_el_monto_no_pasa_por_float():
    """La columna es NUMERIC(12,2). Convertir a float en el borde es el único
    sitio donde este sistema podría perder centavos."""
    db.pool = _Pool()
    _correr(db.guardar_movimiento(_mov(monto=Decimal("0.10"))))
    args = db.pool.ultima.args_vistos
    sql = db.pool.ultima.sql_visto
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    monto = args[cols.index("monto")]
    assert isinstance(monto, str), f"llegó como {type(monto).__name__}"
    assert monto == "0.10"


def test_la_huella_es_la_del_contrato():
    db.pool = _Pool()
    m = _mov()
    _correr(db.guardar_movimiento(m))
    args = db.pool.ultima.args_vistos
    sql = db.pool.ultima.sql_visto
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert args[cols.index("hash_contenido")] == m.clave_dedupe()


def test_deja_pasar_los_creados_a_mano():
    """El índice es parcial (WHERE hash_contenido IS NOT NULL): si Tiziano dice
    dos veces "gasté 500 en el super", puede que hayan sido dos veces."""
    db.pool = _Pool()
    _correr(db.guardar_movimiento(_mov()))
    assert "hash_contenido IS NOT NULL" in db.pool.ultima.sql_visto


def test_el_insert_usa_on_conflict_no_un_select_previo():
    """Con un SELECT antes del INSERT, dos ingestas simultáneas se colarían las
    dos. Decide el índice, no una consulta."""
    db.pool = _Pool()
    _correr(db.guardar_movimiento(_mov()))
    sql = db.pool.ultima.sql_visto
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert "SELECT" not in sql.upper().split("RETURNING")[0]


# ── La capa que faltaba: parsers reales contra clave_dedupe ──────────────

def test_ningun_movimiento_real_colisiona_con_otro_distinto():
    """LA prueba que faltaba, y la que destapó el fallo: los seis tests de
    arriba construyen Movimiento a mano, así que no pueden ver una colisión que
    nace de cómo los PARSERS llenan los campos.

    Un falso duplicado es el peor fallo posible de esta guardia: no revienta,
    devuelve None —que significa "ya visto"— y el gasto desaparece."""
    fixtures = pathlib.Path(__file__).parent / "fixtures"
    if not fixtures.exists():
        print("     (sin fixtures en disco — capa saltada)")
        return

    import cerebro.bancos as B
    from email.header import decode_header
    from email.utils import parsedate_to_datetime
    import email as _email

    def _cab(v):
        if not v:
            return ""
        return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes)
                       else p for p, e in decode_header(v))

    por_clave = {}
    for f in sorted(fixtures.rglob("*.eml")):
        msg = _email.message_from_bytes(f.read_bytes())
        frm = _cab(msg.get("From"))
        addr = frm.split("<")[-1].strip("> ").lower() if "<" in frm else frm.lower()
        asunto = _cab(msg.get("Subject")).strip()
        fn = B.buscar_parser(addr, asunto)
        if fn is None:
            continue
        plano = html = ""
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            if p.get_content_maintype() != "text":
                continue
            d = (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/plain" and not plano:
                plano = d
            elif p.get_content_type() == "text/html" and not html:
                html = d
        try:
            fc = parsedate_to_datetime(msg.get("Date")).replace(tzinfo=None)
        except Exception:
            fc = datetime(2026, 1, 1)
        c = B.CorreoCrudo(remitente=addr, asunto=asunto, fecha_correo=fc,
                          html=html, texto=plano, cuenta="x@y.com", uid=f.stem)
        try:
            movs = fn(c)
        except Exception:
            continue
        for mv in movs:
            por_clave.setdefault(mv.clave_dedupe(), []).append((f.name, fc))

    # Una clave con varios correos es legítima si el banco RENOTIFICÓ la misma
    # transacción. El umbral sale de los datos, no de la intuición: el duplicado
    # real más separado del corpus son 20 horas (Popular renotifica un reverso
    # de RD 0.60 al día siguiente), y los falsos duplicados que destapó el
    # verificador estaban a 29 y 59 días. Siete días separa los dos mundos con
    # margen por los dos lados.
    falsos = []
    for clave, correos in por_clave.items():
        if len(correos) < 2:
            continue
        fechas = sorted(fc for _, fc in correos)
        if (fechas[-1] - fechas[0]).days > 7:
            falsos.append((clave, [n for n, _ in correos],
                           f"{(fechas[-1] - fechas[0]).days} días"))

    for clave, nombres, sep in falsos[:5]:
        print(f"     COLISIÓN ({sep}): {nombres}")
        print(f"        {clave}")
    assert not falsos, (
        f"{len(falsos)} claves agrupan correos separados por horas o días: son "
        "movimientos distintos que la guardia descartaría como duplicados")


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
