# -*- coding: utf-8 -*-
"""El estado que la base acepta, y el que el contrato dice que existe.

EL DEFECTO QUE ESTE ARCHIVO FIJA (medido el 4-sep-2026 contra producción):

  · `cerebro/bancos/contrato.py` declaraba CUATRO estados posibles.
  · la restricción real `movimientos_estado_valido` acepta TRES.
  · el que sobra es `reversada`, y `normalizar_estado()` lo devuelve cuando el
    aviso dice "reversada", "anulada" o "devuelta".
  · BHD lo convertía antes de guardar. Banesco y Banreservas NO: pasaban el
    valor derecho a la base.

O sea que el día que Banesco o Banreservas mandara el aviso de una transacción
anulada, el INSERT violaba el CHECK y ese movimiento no quedaba en ningún lado.
Contra producción, ese día todavía no había llegado: de los 178 correos de
banco en bandeja, los 3 con palabra de reverso eran de BHD y entraron bien.

Y hay un defecto DE FONDO debajo, que es el que de verdad importa: las dos
listas —la del contrato y la de la restricción— podían separarse sin que nadie
se enterara. Por eso acá no solo se prueba el caso: se ATAN las dos listas.

Los tests son herméticos. El de "llega hasta la base" usa una conexión de
mentira que aplica el CHECK DE VERDAD, leído de db/schema.sql en el momento —
no una copia de la lista escrita a mano acá, que se desincronizaría igual que
las otras dos.

Correr:  python3 -m pytest tests/test_estado_guardable.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
import types
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")

# ── Stubs de psycopg ANTES de importar db.db (igual que test_movimientos_dedupe)
_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules["psycopg"] = _psycopg
sys.modules["psycopg.rows"] = _rows
sys.modules["psycopg_pool"] = _pool

import db.db as db  # noqa: E402
from cerebro.bancos.banesco import parsear_consumo as banesco_consumo  # noqa: E402
from cerebro.bancos.banreservas import parsear as banreservas_parsear  # noqa: E402
from cerebro.bancos.bhd import parsear_consumo as bhd_consumo  # noqa: E402
from cerebro.bancos.contrato import (  # noqa: E402
    ESTADOS,
    ESTADOS_GUARDABLES,
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    asentar_reverso,
    normalizar_estado,
)

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = RAIZ / "db" / "schema.sql"
MIGRACIONES = RAIZ / "db" / "migrations"


def _correr(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── El CHECK de verdad, leído del SQL ────────────────────────────────────

_CHECK = re.compile(
    r"CHECK\s*\(\s*estado\s+IN\s*\(([^)]*)\)", re.I)


def _estados_del_sql(texto: str) -> tuple[str, ...]:
    """Los valores que un `CHECK (estado IN (...))` acepta, en orden."""
    m = _CHECK.search(texto)
    assert m, "no encontré ningún CHECK (estado IN (...)) en ese SQL"
    return tuple(re.findall(r"'([^']+)'", m.group(1)))


def _sql_de_la_restriccion() -> str:
    """El último SQL de db/migrations que redefine movimientos_estado_valido."""
    tocan = sorted(p for p in MIGRACIONES.glob("*.sql")
                   if "movimientos_estado_valido" in p.read_text())
    assert tocan, ("ninguna migración define movimientos_estado_valido. Si la "
                   "restricción solo vive en la base real o en schema.sql, una "
                   "base armada desde las migraciones queda distinta a la de "
                   "producción.")
    return tocan[-1].read_text()


# ── 1. Las dos listas atadas ─────────────────────────────────────────────

def test_el_contrato_y_la_restriccion_dicen_lo_mismo():
    """EL TEST DE FONDO.

    Si mañana alguien agrega un quinto estado a ESTADOS_GUARDABLES y no lo
    agrega a la restricción —o al revés—, esto se pone rojo. Es lo único que
    impide que las dos listas se separen en silencio, que es exactamente lo que
    pasó con `reversada`.
    """
    del_schema = _estados_del_sql(SCHEMA.read_text())
    de_la_migracion = _estados_del_sql(_sql_de_la_restriccion())

    assert set(ESTADOS_GUARDABLES) == set(del_schema), (
        f"contrato={ESTADOS_GUARDABLES} vs db/schema.sql={del_schema}")
    assert set(del_schema) == set(de_la_migracion), (
        f"db/schema.sql={del_schema} vs la migración={de_la_migracion}. Una "
        "base armada desde las migraciones no sería igual a una armada desde "
        "el esquema.")


def test_la_restriccion_del_schema_lleva_el_nombre_de_produccion():
    """Sin nombre, Postgres la bautiza `movimientos_estado_check` y una base
    armada desde el esquema y después migrada termina con las DOS. El nombre de
    producción es `movimientos_estado_valido` (comprobado el 4-sep-2026)."""
    assert "CONSTRAINT movimientos_estado_valido" in SCHEMA.read_text()


def test_todo_estado_del_contrato_o_se_guarda_o_se_asienta():
    """Un estado que el banco puede DECIR y la base no acepta tiene que tener
    una conversión. Hoy el único es `reversada`. Si alguien agrega un quinto a
    ESTADOS sin decir qué se hace con él, esto se pone rojo."""
    sin_guardar = set(ESTADOS) - set(ESTADOS_GUARDABLES)
    assert sin_guardar == {"reversada"}, (
        f"estados que la base no acepta y nadie convierte: {sin_guardar}")
    for estado in sin_guardar:
        for tipo in ("gasto", "ingreso", "transferencia"):
            _, resuelto = asentar_reverso(tipo, estado)
            assert resuelto in ESTADOS_GUARDABLES, (
                f"asentar_reverso({tipo!r}, {estado!r}) devolvió {resuelto!r}, "
                f"que la base tampoco acepta")


def test_normalizar_estado_sigue_devolviendo_reversada():
    """`reversada` NO se borra del vocabulario: es lo que el banco dijo, y
    perderlo sería tratar una anulación como una aprobación."""
    assert normalizar_estado("Reversada") == "reversada"
    assert normalizar_estado("ANULADA") == "reversada"
    assert normalizar_estado("Devuelta") == "reversada"


# ── 2. El Movimiento no se deja construir con un estado que no se guarda ──

def _mov(**kw):
    base = dict(banco="banesco", canal="tarjeta", tipo="gasto",
                fecha=datetime(2026, 9, 4, 10, 0), monto=Decimal("100.00"),
                moneda="DOP", contraparte="COMERCIO X",
                estado="aprobada", referencia="ref")
    base.update(kw)
    return Movimiento(**base)


def test_movimiento_rechaza_un_estado_que_la_base_no_acepta():
    """La guarda estructural: `reversada` deja de poder existir como Movimiento,
    así que ya no hay forma de que llegue a la base desde NINGÚN parser.

    Y revienta con ErrorDeParseo, que es la excepción que la ingesta ya cuenta
    (`reventados`) y por la que el canario ya avisa — o sea que un parser nuevo
    que se olvide de asentar el reverso falla RUIDOSO."""
    try:
        _mov(estado="reversada")
    except ErrorDeParseo as e:
        assert "asentar_reverso" in str(e)
    else:
        raise AssertionError("el Movimiento aceptó un estado que la base no "
                             "acepta")


# ── 3. El caso real: un consumo anulado que llega hasta la base ──────────

class _Cursor:
    def __init__(self, fila):
        self._fila = fila

    async def fetchone(self):
        return self._fila


class _ViolacionDelCheck(Exception):
    """Lo que Postgres tiraría: psycopg.errors.CheckViolation."""


class _Conn:
    """Conexión de mentira que aplica el CHECK DE VERDAD de db/schema.sql."""

    def __init__(self, guardados):
        self.guardados = guardados
        self.validos = _estados_del_sql(SCHEMA.read_text())

    async def execute(self, sql, args):
        cols = sql.split("(", 1)[1].split(")", 1)[0]
        cols = [c.strip() for c in cols.replace("\n", " ").split(",")]
        estado = args[cols.index("estado")]
        if estado not in self.validos:
            raise _ViolacionDelCheck(
                'new row for relation "movimientos" violates check constraint '
                f'"movimientos_estado_valido" · estado={estado!r}')
        self.guardados.append(dict(zip(cols, args)))
        return _Cursor((len(self.guardados),))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self):
        self.guardados: list[dict] = []

    def connection(self):
        return _Conn(self.guardados)


def _con_base_de_mentira():
    """Monta el pool falso y hace que la violación del CHECK sea 'fila mala'."""
    db.pool = _Pool()
    db._ERRORES_DE_FILA = (_ViolacionDelCheck,)
    return db.pool


BANESCO_ANULADA = (
    "Estimado(a) ROSILIS ROMERO, te notificamos que tu tarjeta VISA CLASICA "
    "SUPERCASHBACK Banesco terminada en 9639, en fecha 06/05/26, presenta un "
    "consumo de RD$ 3,183.38, en SM NACIONAL MAXIMO GOM y su estado es "
    "anulada. Dispones de RD$ 663,903.86.")

BANRESERVAS_ANULADA = (
    "Notificación de Consumo Su tarjeta VISA PLATINUM ••8110 presenta un "
    "consumo. Monto: DOP 254.90 Estado: ANULADA Comercio: SM NACIONAL "
    "MAXIMO GOM SANTO DOMINGODO Fecha de transacción: 17/04/2026 10:28 AM "
    "Número de aprobación: 299209 Recibido por los valores indicados")


def _correo(remitente: str, asunto: str, texto: str) -> CorreoCrudo:
    return CorreoCrudo(remitente=remitente, asunto=asunto,
                       fecha_correo=datetime(2026, 9, 4, 10, 0), html="",
                       texto=texto, cuenta="rosilisr04@gmail.com", uid="1")


def test_banesco_anulada_llega_a_la_base():
    """EL TEST QUE ESTABA EN ROJO. Antes: normalizar_estado devolvía
    'reversada', el Movimiento lo aceptaba, y el INSERT violaba el CHECK."""
    pool = _con_base_de_mentira()
    mov = banesco_consumo(_correo("notificaciones@banesco.com.do",
                                  "Alerta de Consumo Banesco RD",
                                  BANESCO_ANULADA))[0]
    assert mov.estado == "aprobada", "el reverso sí ocurrió; debe contar"
    assert mov.tipo == "ingreso", "es plata que vuelve, no un gasto"
    assert _correr(db.guardar_movimiento(mov)) == 1
    assert pool.guardados[0]["estado"] == "aprobada"
    assert pool.guardados[0]["tipo"] == "ingreso"


def test_banreservas_anulada_llega_a_la_base():
    """El mismo caso por el otro banco que no convertía."""
    pool = _con_base_de_mentira()
    mov = banreservas_parsear(_correo("notificaciones@banreservas.com",
                                      "Notificaciones Banreservas",
                                      BANRESERVAS_ANULADA))[0]
    assert mov.estado == "aprobada"
    assert mov.tipo == "ingreso"
    assert mov.monto == Decimal("254.90")
    assert _correr(db.guardar_movimiento(mov)) == 1
    assert pool.guardados[0]["estado"] == "aprobada"


def test_bhd_sigue_asentando_el_reverso_igual_que_antes():
    """BHD ya lo hacía bien y tiene que seguir haciéndolo idéntico: lo único
    que cambió es de dónde sale la regla."""
    html = ("<table><tbody><tr>"
            "<td>08/07/2026 11:14 am</td><td>US</td><td>$1.19</td>"
            "<td>APPLE.COM/BILL</td><td>Reversada</td><td>Compra</td>"
            "</tr></tbody></table>")
    correo = CorreoCrudo(remitente="alertas@bhd.com.do",
                         asunto="Notificación de Transacciones",
                         fecha_correo=datetime(2026, 7, 8), html=html,
                         texto="", cuenta="tizianofv@gmail.com", uid="1")
    m = bhd_consumo(correo)[0]
    assert m.tipo == "ingreso" and m.estado == "aprobada"


def test_una_retencion_revertida_no_queda_pendiente():
    """El `estado_forzado` de BHD se anula en un reverso: una "Reserva de Fondos
    (Hold)" revertida ya no está pendiente de nada, se resolvió."""
    html = ("<table><tbody><tr>"
            "<td>08/07/2026 11:14 am</td><td>RD</td><td>$500.00</td>"
            "<td>HOTEL X</td><td>Reversada</td><td>Reserva de Fondos (Hold)</td>"
            "</tr></tbody></table>")
    correo = CorreoCrudo(remitente="alertas@bhd.com.do",
                         asunto="Notificación de Transacciones",
                         fecha_correo=datetime(2026, 7, 8), html=html,
                         texto="", cuenta="tizianofv@gmail.com", uid="1")
    m = bhd_consumo(correo)[0]
    assert m.estado == "aprobada" and m.tipo == "ingreso"


def test_un_traspaso_revertido_sigue_siendo_traspaso():
    """La plata volvió a moverse entre cuentas propias: ni entró ni salió del
    conjunto. Invertirlo a 'gasto' inventaría un gasto que no existió."""
    assert asentar_reverso("transferencia", "reversada") == (
        "transferencia", "aprobada")


def test_lo_que_no_es_reverso_no_se_toca():
    for tipo in ("gasto", "ingreso", "transferencia"):
        for estado in ESTADOS_GUARDABLES:
            assert asentar_reverso(tipo, estado) == (tipo, estado)


# ── 4. Un movimiento que no se puede guardar deja rastro ─────────────────

def test_la_base_que_rechaza_una_fila_no_sube_como_error_cualquiera():
    """`guardar_movimiento` tiene que distinguir "esta fila está mal" de "la
    base no responde": lo primero se sigue y se cuenta, lo segundo para la
    pasada sin avanzar el cursor. Antes las dos subían idénticas."""
    _con_base_de_mentira()
    # Se salta la guarda del dataclass a propósito: lo que se prueba es la capa
    # de base, no el contrato. `object.__setattr__` porque Movimiento es frozen.
    mov = _mov()
    object.__setattr__(mov, "estado", "reversada")
    try:
        _correr(db.guardar_movimiento(mov))
    except db.MovimientoRechazado as e:
        assert "banesco" in str(e) and "reversada" in str(e), str(e)
    else:
        raise AssertionError("la base rechazó la fila y guardar_movimiento no "
                             "lo dijo")


def test_un_fallo_de_conexion_sigue_subiendo():
    """Lo contrario del anterior, y es lo que hace que valga: si la base no
    responde hay que PARAR. Atraparlo como 'fila mala' haría que la ingesta
    avanzara el cursor y se saltara correos que nunca se guardaron."""
    class _ConnCaida:
        async def execute(self, sql, args):
            raise OSError("conexión caída")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    db.pool = types.SimpleNamespace(connection=lambda: _ConnCaida())
    db._ERRORES_DE_FILA = (_ViolacionDelCheck,)
    try:
        _correr(db.guardar_movimiento(_mov()))
    except db.MovimientoRechazado:
        raise AssertionError("un fallo de conexión se disfrazó de fila mala")
    except OSError:
        pass
