# -*- coding: utf-8 -*-
"""El resumen del correo, directo a los dos (encargo 3, 22-sep-2026).

Diseño: `disenos/lucy-rosi-independiente/DISENO.md`, "Encargo 3". Decisión de
Tiziano, textual: el buzón del estudio también le llega a Rosi; el suyo (el
mixto) NO. Él sigue recibiendo el suyo igual que hoy, con los dos buzones.

HERMANOS, A PROPÓSITO NI TIZIANO NI ROSI -- el segundo destino de este
archivo se llama BETA, igual que en `tests/test_briefing_por_persona.py` y
`tests/test_recordatorios_por_responsable.py`, para probar que el reparto
sale de `reporte_a` (una LISTA de chats, ahora) y no de un nombre escrito en
el código.

LA GARANTÍA CENTRAL (punto 4 del encargo): "que mandarle a Rosi no le robe a
Tiziano ningún correo de su resumen, ni al revés". Antes de este encargo,
`correo_reportado` tenía PRIMARY KEY (cuenta, uid) -- una fila por correo,
sin importar a quién se le informó. La prueba de fondo de este archivo
(`test_lo_que_ya_vio_el_dueno_no_le_roba_la_novedad_a_beta`) corre contra
SQLite DE VERDAD -- no un set de Python -- porque la corrección depende de
que la consulta relacional filtre por LOS TRES campos (cuenta, uid, destino)
a la vez, y un doble en memoria puede esconder un error de ese tipo (por
ejemplo, filtrar solo por cuenta y uid, que es exactamente el defecto que
esto reemplaza).

LA FRONTERA, dicha: este archivo prueba `captura/correo.py::reporte_diario`
y `_pendientes_de` (el reparto por destino), `config.destinos_del_reporte`
(el vocabulario de `reporte_a`), y la extensión de
`cerebro/interpretar.py::_es_encargo_propio_del_dueno` al origen 'correo'.
NO vuelve a probar el candado de "una vez al día" en sí mismo -- eso ya lo
cubre `test_reporte_una_vez_al_dia.py` -- ni el filtro de bancos ni el cupo
de clasificación -- eso ya lo cubren `test_reporte_sin_bancos.py` y
`test_correo_no_descarta_callado.py`.

Herméticos: sin Postgres ni red -- mismos stubs que el resto de la suite. La
única base real que toca este archivo es un SQLite en memoria, propio,
descartado al terminar cada prueba.

Correr:  python3 -m pytest tests/test_correo_directo_a_los_dos.py -q
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import types
from datetime import datetime

os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "1001")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")


class _Cualquiera:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Cualquiera()

    def __call__(self, *a, **k):
        return _Cualquiera()


for _n, _attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object}),
                   ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                   ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera}),
                   ("httpx", {"HTTPError": type("H", (Exception,), {})})):
    _m = types.ModuleType(_n)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    _m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[_n] = _m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import captura.correo as correo  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

DUENO = config.CHAT_ID_DUENO
BETA = 2002
TZ = config.TZ

PERSONAL = "personal@ejemplo.com"   # el buzón mixto, solo del dueño
ESTUDIO = "estudio@ejemplo.com"     # el buzón que ahora también ve BETA


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _clasificar_por_asunto(c, reglas=""):
    """Como el clasificador real: el asunto_corto sale del correo de VERDAD,
    no de un valor fijo -- si no, "del estudio"/"personal" no aparecerían
    nunca en el texto del encargo y las pruebas de contenido no probarían
    nada."""
    async def _f():
        return {"ambito": "laboral", "area": "cds_clientes", "nivel": "accion",
                "asunto_corto": c["subject"][:120], "motivo": ""}
    return _f()


# ---------------------------------------------------------------------------
# 1) config.destinos_del_reporte: el vocabulario de `reporte_a`, con lista
# ---------------------------------------------------------------------------
def test_sin_el_campo_va_solo_al_dueno():
    assert config.destinos_del_reporte({"user": PERSONAL}) == (DUENO,)


def test_reporte_a_0_es_a_nadie():
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte_a": 0}) == ()
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte": False}) == ()


def test_reporte_a_un_entero_es_compatible():
    assert config.destinos_del_reporte({"user": PERSONAL, "reporte_a": 555}) == (555,)


def test_reporte_a_una_lista_da_varios_destinos():
    """El caso nuevo: el buzón del estudio, a los dos."""
    assert config.destinos_del_reporte(
        {"user": ESTUDIO, "reporte_a": [DUENO, BETA]}) == (DUENO, BETA)


def test_reporte_a_invalido_cae_al_dueno():
    assert config.destinos_del_reporte(
        {"user": PERSONAL, "reporte_a": "no-es-un-numero"}) == (DUENO,)
    assert config.destinos_del_reporte(
        {"user": PERSONAL, "reporte_a": [DUENO, "x"]}) == (DUENO,)


def test_destino_del_reporte_compat_es_el_primero():
    """El código de antes de este encargo sigue andando: `destino_del_reporte`
    (singular) es el PRIMERO de la lista."""
    assert correo.destino_del_reporte(
        {"user": ESTUDIO, "reporte_a": [DUENO, BETA]}) == DUENO
    assert correo.destino_del_reporte({"user": PERSONAL, "reporte_a": 0}) == 0


# ---------------------------------------------------------------------------
# 2) La dedupe POR DESTINO, contra SQLite de verdad
# ---------------------------------------------------------------------------
class _BaseSQLite:
    """`correos_ya_reportados`/`marcar_correo_reportado` contra una tabla
    SQLite real -- el resto (bandeja, candado) sigue siendo una lista en
    memoria, como en el resto de la suite. Lo único que necesita SQL de
    verdad es la garantía central de este encargo: la dedupe por
    (cuenta, uid, destino)."""

    def __init__(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            """
            CREATE TABLE correo_reportado (
                cuenta TEXT NOT NULL,
                uid INTEGER NOT NULL,
                destino_chat_id INTEGER NOT NULL,
                bandeja_id INTEGER,
                UNIQUE(cuenta, uid, destino_chat_id)
            )
            """
        )
        self.filas: list[dict] = []

    async def correos_ya_reportados(self, cuenta, uids, destino=None):
        if not uids:
            return set()
        marcas = ",".join("?" * len(uids))
        cur = self.con.execute(
            f"SELECT uid FROM correo_reportado "
            f"WHERE cuenta = ? AND destino_chat_id = ? AND uid IN ({marcas})",
            (cuenta, destino, *uids))
        return {r[0] for r in cur.fetchall()}

    async def marcar_correo_reportado(self, cuenta, uid, *, destino=None,
                                      bandeja_id=None, **kw):
        self.con.execute(
            "INSERT OR IGNORE INTO correo_reportado "
            "(cuenta, uid, destino_chat_id, bandeja_id) VALUES (?, ?, ?, ?)",
            (cuenta, uid, destino, bandeja_id))
        self.con.commit()

    async def guardar_en_bandeja(self, **kw):
        self.filas.append(dict(kw))
        return len(self.filas)

    async def destinos_con_encargo_hoy(self, origen, prefijo, desde):
        return {f["chat_id"] for f in self.filas
                if f.get("origen") == origen
                and (f.get("contenido_raw") or "").startswith(prefijo)
                and f.get("creado_en", desde) >= desde
                and f.get("chat_id") is not None}

    async def listar_preferencias(self):
        return []

    def encargo_de(self, chat_id):
        for f in self.filas:
            if f.get("chat_id") == chat_id and f.get("origen") == "correo":
                return f
        return None


class _Reloj:
    def __init__(self, ahora):
        self.ahora = ahora

    def now(self, tz=None):
        return self.ahora if tz is None else self.ahora.astimezone(tz)


def _uno(uid, asunto):
    return {"uid": uid, "from": "Jorge <jorge@ejemplo.com>", "subject": asunto,
            "snippet": "hola", "ruido_barato": None}


def _montar(base: _BaseSQLite, correo_de: dict, hora=7, minuto=10):
    reloj = _Reloj(datetime(2026, 9, 22, hora, minuto, tzinfo=TZ))
    for f in base.filas:
        f["creado_en"] = f.get("creado_en", reloj.ahora)
    correo.datetime = reloj
    for n in ("guardar_en_bandeja", "destinos_con_encargo_hoy",
              "marcar_correo_reportado", "correos_ya_reportados",
              "listar_preferencias"):
        setattr(db, n, getattr(base, n))
    correo._sin_leer_sync = lambda cuenta, dias, **kw: [
        dict(c, cuenta=cuenta["user"]) for c in correo_de.get(cuenta["user"], [])]
    correo.clasificar = _clasificar_por_asunto
    return reloj


def test_tiziano_recibe_los_dos_buzones_beta_solo_el_del_estudio():
    base = _BaseSQLite()
    config.CORREO_CUENTAS = [
        {"user": PERSONAL, "pass": "x"},
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    _montar(base, {PERSONAL: [_uno(1, "personal")],
                   ESTUDIO: [_uno(2, "del estudio")]})
    total = _correr(correo.reporte_diario())
    assert total == 3, (
        "3 pares (correo, destino): el del personal solo al dueño, el del "
        f"estudio a los dos -- salió {total}")

    del_dueno = base.encargo_de(DUENO)
    del_beta = base.encargo_de(BETA)
    assert del_dueno is not None and del_beta is not None

    assert "personal" in del_dueno["contenido_raw"]
    assert "del estudio" in del_dueno["contenido_raw"], (
        "el dueño tiene que seguir viendo el buzón del estudio, sin cambio")
    assert "informaste a Tiziano" in del_dueno["contenido_raw"]

    assert "del estudio" in del_beta["contenido_raw"]
    assert "personal" not in del_beta["contenido_raw"], (
        "ninguna línea del buzón personal de Tiziano puede llegarle a Beta")
    assert "informaste a Tiziano" not in del_beta["contenido_raw"], (
        "el encargo de Beta no puede decir que es un reporte para Tiziano")

    # Y el candado quedó por destino: una segunda pasada no repite ninguno.
    assert _correr(correo.reporte_diario()) == 0


def test_lo_que_ya_vio_el_dueno_no_le_roba_la_novedad_a_beta():
    """LA GARANTÍA CENTRAL, contra SQLite real: un correo del buzón del
    estudio que YA se le informó al dueño (un día anterior, digamos) sigue
    siendo NUEVO para Beta -- y viceversa. Antes de este encargo era
    imposible: la PK vieja (cuenta, uid) solo dejaba una fila por correo."""
    base = _BaseSQLite()
    # Simula que AYER el correo #2 ya se le informó al dueño, pero nunca a
    # Beta -- exactamente el estado que deja "Beta se sumó como destino
    # después de que el dueño ya viera algunos correos de ese buzón".
    base.con.execute(
        "INSERT INTO correo_reportado (cuenta, uid, destino_chat_id) "
        "VALUES (?, ?, ?)", (ESTUDIO, 2, DUENO))
    base.con.commit()

    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    _montar(base, {ESTUDIO: [_uno(2, "del estudio, viejo para el dueño")]})
    total = _correr(correo.reporte_diario())
    assert total == 1, f"solo Beta tenía que recibir este correo: salieron {total}"

    assert base.encargo_de(DUENO) is None, (
        "el dueño ya lo había visto: no puede volver a aparecerle")
    del_beta = base.encargo_de(BETA)
    assert del_beta is not None, "Beta nunca lo había visto: tenía que llegarle"
    assert "del estudio" in del_beta["contenido_raw"]

    # Y AL REVÉS, en la misma corrida no hace falta otra prueba: la fila del
    # dueño para uid=2 sigue existiendo tal cual (no se reescribió), y ahora
    # además hay una fila nueva para Beta -- las DOS conviven.
    filas = base.con.execute(
        "SELECT destino_chat_id FROM correo_reportado WHERE cuenta=? AND uid=2",
        (ESTUDIO,)).fetchall()
    assert {DUENO, BETA} == {f[0] for f in filas}


def test_un_correo_ya_visto_por_los_dos_no_vuelve_a_ninguno():
    base = _BaseSQLite()
    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": [DUENO, BETA]},
    ]
    _montar(base, {ESTUDIO: [_uno(5, "único")]})
    # 2, no 1: el mismo correo cuenta una vez POR DESTINO (dueño y Beta) --
    # `reporte_diario` documenta esto explícitamente ("pendientes es solo
    # para CONTAR, no para deduplicar entre destinos").
    assert _correr(correo.reporte_diario()) == 2
    # Un día después: candado reabierto (otro `hoy_arranca`), pero el correo
    # sigue siendo el mismo -- ya informado a los dos.
    reloj = _montar(base, {ESTUDIO: [_uno(5, "único")]}, hora=7, minuto=10)
    reloj.ahora = datetime(2026, 9, 23, 7, 10, tzinfo=TZ)
    assert _correr(correo.reporte_diario()) == 0


def test_reporte_a_0_en_el_buzon_del_estudio_no_le_llega_a_nadie():
    """La frontera del campo sigue viva con lista o sin ella: si el buzón
    dice que no informa a nadie, ni el dueño lo ve."""
    base = _BaseSQLite()
    config.CORREO_CUENTAS = [
        {"user": ESTUDIO, "pass": "x", "reporte_a": 0},
    ]
    _montar(base, {ESTUDIO: [_uno(9, "no debería salir")]})
    assert _correr(correo.reporte_diario()) == 0
    assert base.filas == []


# ---------------------------------------------------------------------------
# 3) La copia general: el reporte del dueño no se copia a Beta
# ---------------------------------------------------------------------------
def test_interpretar_reconoce_el_reporte_de_correo_del_dueno():
    fila = {"origen": "correo", "chat_id": DUENO,
            "contenido_raw": correo.MARCA_ENCARGO + " Estos son los 3..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is True


def test_el_reporte_de_beta_no_es_el_del_dueno():
    """Hermano por tipo (mismo origen, misma marca) pero de OTRO chat: no
    tiene que activar la supresión -- va directo a Beta, la copia general
    ni se dispara para ese chat_id."""
    fila = {"origen": "correo", "chat_id": BETA,
            "contenido_raw": correo.MARCA_ENCARGO + " Estos son los 1..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is False


def test_la_alerta_911_no_se_confunde_con_el_reporte():
    """Hermano por chat (mismo origen 'correo', mismo chat_id=dueño) pero
    OTRO tipo de mensaje -- la 911 no tiene camino propio en este encargo,
    así que sigue copiándose como antes."""
    fila = {"origen": "correo", "chat_id": DUENO,
            "contenido_raw": "ALERTA DE INFRAESTRUCTURA por correo..."}
    assert interpretar._es_encargo_propio_del_dueno(fila) is False


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
