# -*- coding: utf-8 -*-
"""La tarjeta de Rosi para las formales de Natalia (Lucy 5 del diseño «Cotización formal que hace Natalia», 24-oct-2026).

Espejo de la tarjeta de las facturas (`test_facturas_natalia.py`, sección de la tarjeta), con el MISMO camino de producción:
el despertador REAL (`despertador.revisar_envios_de_formales`), el manejador de botones REAL (`botones.al_pulsar`) y la puerta
REAL de `db/db.py` sobre SQLite con las migraciones reales (fixture `mundo` de `test_cotizaciones_natalia_puerta`).

Lo que se vigila:
  1. la tarjeta va SOLO al chat de quien revisa, con el PDF (sin marca de borrador) y los botones `cfs:` / `cfn:`;
  2. solo las formales de Natalia tienen tarjeta: una formal de la casa con envío pendiente no sale;
  3. canal «mano» no sale;
  4. Aprobar pone el envío `aprobado` con quién y cuándo, y NO cambia la formal (sigue borrador);
  5. Rechazar pone el envío `rechazado` con su motivo;
  6. EL CANDADO: si la formal cambió después de la tarjeta, Aprobar no aprueba la versión vieja (`no_se_pudo`);
  7. un segundo envío aprobado para la misma formal impide aprobar otro;
  8. quien no es Rosi no aprueba; tocar antes de que salga la tarjeta no escribe nada;
  9. el recordatorio sale UNA vez, respondiendo a la tarjeta.

FRONTERA: SQLite, no Postgres (el «primero gana» se vigila por el `WHERE estado` de cada UPDATE, y el bloqueo por el UPDATE
sin cambios); Telegram son dobles (la firma de `send_document` y `send_message` es la de `telegram.Bot`). NINGÚN nombre,
clave ni número de este archivo es real.

Correr:  python3 -m pytest tests/test_cotizaciones_tarjeta_rosi.py -q
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest
import telegram

# Primero: la puerta de formales fija el entorno de las pruebas antes de cargar `config`.
from test_cotizaciones_natalia_puerta import (  # noqa: F401  (fixture y ayudantes de la puerta de formales)
    DUENO, ROSI, _fila, _formal_de_la_casa, _texto_del_pdf, crear, mundo, pedir)

import acciones.botones as botones
import cerebro.despertador as despertador
import config
import db.db as db

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class Red:
    def __init__(self):
        self.documentos: list[dict] = []
        self.mensajes: list[dict] = []
        self.avisos: list[tuple] = []
        self.modo = "bien"                      # bien | sin_mensaje


@pytest.fixture
def tg(mundo, monkeypatch):  # noqa: F811
    red = Red()
    red.con = mundo

    async def _documento(self, chat_id, document, caption=None, reply_markup=None, filename=None, **kw):
        red.documentos.append({"chat_id": chat_id, "document": document, "caption": caption,
                               "reply_markup": reply_markup, "filename": filename, **kw})
        if red.modo == "sin_mensaje":
            return None
        return types.SimpleNamespace(message_id=9000 + len(red.documentos))

    async def _mensaje(self, chat_id, text, **kw):
        red.mensajes.append({"chat_id": chat_id, "text": text, **kw})
        return types.SimpleNamespace(message_id=7000 + len(red.mensajes))

    monkeypatch.setattr(telegram.Bot, "send_document", _documento, raising=False)
    monkeypatch.setattr(telegram.Bot, "send_message", _mensaje, raising=False)
    red.bot = telegram.Bot(token="123456:token-de-prueba-formales")

    async def _aviso(chat, texto):
        red.avisos.append((chat, texto))
    monkeypatch.setattr(db, "registrar_aviso", _aviso)
    monkeypatch.setattr(config, "chat_de_quien_revisa_las_facturas", lambda: ROSI)
    monkeypatch.setattr(config, "puede_recibir_telegram", lambda chat: True)
    return red


def correr(coro):
    return asyncio.run(coro)


def vuelta(red, ahora=T0):
    return correr(despertador.revisar_envios_de_formales(red.bot, ahora))


def botones_de(documento):
    return [b.callback_data for fila in documento["reply_markup"].inline_keyboard for b in fila]


class Toque:
    """Un `CallbackQuery` de mentira de una tarjeta que es un DOCUMENTO con pie."""

    def __init__(self, data, chat_id):
        self.data = data
        self.avisos: list[str] = []
        self.editados: list[dict] = []
        self.message = types.SimpleNamespace(chat_id=chat_id, caption_html="El pie de la tarjeta.", reply_text=self._reply)

    async def answer(self, *a, **k):
        pass

    async def edit_message_caption(self, caption=None, **kw):
        self.editados.append({"caption": caption, **kw})

    async def _reply(self, texto, **kw):
        self.avisos.append(texto)
        return types.SimpleNamespace(message_id=1)

    @property
    def pie_final(self):
        return self.editados[-1]["caption"] if self.editados else None


def tocar(data, chat_id):
    q = Toque(data, chat_id)
    correr(botones.al_pulsar(types.SimpleNamespace(callback_query=q), None))
    return q


def con_tarjeta(red, canal="whatsapp"):
    """Natalia hace la formal y pide la aprobación; el despertador REAL manda la tarjeta. Devuelve `(cid, eid, codigo)`."""
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid, canal).json()["envio"]["envio_id"]
    assert vuelta(red) == 1
    anio, numero = red.con.execute("SELECT anio, numero FROM cotizaciones WHERE id = ?", (cid,)).fetchone()
    return cid, eid, db.codigo_de_cotizacion(anio, numero)


def envio(con, eid):
    return _fila(con, "SELECT estado, aprobado_por_chat_id, motivo, tarjeta_msg_id FROM cotizacion_envios WHERE id = ?", (eid,))


# ── La tarjeta ───────────────────────────────────────────────────────────────────────────────────────

def test_la_tarjeta_va_a_quien_revisa_con_el_pdf_sin_borrador_y_los_botones(mundo, tg):
    cid, eid, codigo = con_tarjeta(tg)
    assert len(tg.documentos) == 1
    d = tg.documentos[0]
    assert d["chat_id"] == ROSI
    assert botones_de(d) == [f"cfs:{eid}", f"cfn:{eid}"]
    assert d["filename"] == f"{codigo}.pdf"
    assert codigo in d["caption"]
    texto = _texto_del_pdf(d["document"])
    assert codigo in texto
    assert "BORRADOR" not in texto
    assert envio(mundo, eid)["tarjeta_msg_id"] == 9001
    assert tg.avisos and tg.avisos[0][0] == ROSI


def test_la_tarjeta_no_se_anota_si_telegram_no_devolvio_mensaje(mundo, tg):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    tg.modo = "sin_mensaje"
    assert vuelta(tg) == 0
    assert envio(mundo, eid)["tarjeta_msg_id"] is None


def test_una_formal_de_la_casa_con_envio_pendiente_no_tiene_tarjeta(mundo, tg):
    """Hermano: solo las de Natalia. La puerta de pedir no deja pedir una de la casa; aquí se mete el envío a mano."""
    cid = _formal_de_la_casa(mundo)
    mundo.execute("INSERT INTO cotizacion_envios (cotizacion_id, canal, estado) VALUES (?, 'whatsapp', 'pendiente')", (cid,))
    mundo.commit()
    assert vuelta(tg) == 0
    assert tg.documentos == []


def test_el_canal_a_mano_no_pide_tarjeta(mundo, tg):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    mundo.execute("INSERT INTO cotizacion_envios (cotizacion_id, canal, estado) VALUES (?, 'mano', 'pendiente')", (cid,))
    mundo.commit()
    assert vuelta(tg) == 0
    assert tg.documentos == []


# ── Los botones ──────────────────────────────────────────────────────────────────────────────────────

def test_aprobar_pone_el_envio_aprobado_y_la_formal_sigue_borrador(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    q = tocar(f"cfs:{eid}", ROSI)
    assert "Aprobada" in q.pie_final
    e = envio(mundo, eid)
    assert e["estado"] == "aprobado" and e["aprobado_por_chat_id"] == ROSI
    assert _fila(mundo, "SELECT estado, enviada_en FROM cotizaciones WHERE id = ?", (cid,)) == {"estado": "borrador", "enviada_en": None}


def test_rechazar_pone_el_envio_rechazado_con_motivo_y_no_toca_la_formal(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    q = tocar(f"cfn:{eid}", ROSI)
    assert "Rechazada" in q.pie_final
    e = envio(mundo, eid)
    assert e["estado"] == "rechazado" and e["motivo"] == "Rechazada por Rosi Inventada"
    assert _fila(mundo, "SELECT estado FROM cotizaciones WHERE id = ?", (cid,))["estado"] == "borrador"


def test_si_la_formal_cambio_despues_de_la_tarjeta_aprobar_no_aprueba_la_version_vieja(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    mundo.execute("UPDATE cotizaciones SET cambiado_en = '2026-10-20T09:00:00+00:00' WHERE id = ?", (cid,))
    mundo.commit()
    q = tocar(f"cfs:{eid}", ROSI)
    assert "No se pudo aprobar" in q.pie_final
    e = envio(mundo, eid)
    assert e["estado"] == "no_se_pudo" and e["motivo"].startswith("La formal cambió")


def test_un_segundo_envio_aprobado_para_la_misma_formal_impide_aprobar_otro(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    mundo.execute("INSERT INTO cotizacion_envios (cotizacion_id, canal, estado, aprobado_por_chat_id, aprobado_en) "
                  "VALUES (?, 'correo', 'aprobado', ?, '2026-10-14T11:00:00+00:00')", (cid, ROSI))
    mundo.commit()
    q = tocar(f"cfs:{eid}", ROSI)
    assert "No se pudo aprobar" in q.pie_final
    assert envio(mundo, eid)["estado"] == "no_se_pudo"


def test_quien_no_es_rosi_no_aprueba_y_no_cambia_nada(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    q = tocar(f"cfs:{eid}", DUENO)
    assert q.avisos == ["Esto le toca a Rosi Inventada."]
    assert envio(mundo, eid)["estado"] == "pendiente"


def test_tocar_antes_de_que_salga_la_tarjeta_no_escribe_nada(mundo, tg):
    cid = crear().json()["cotizacion"]["cotizacion_id"]
    eid = pedir(cid).json()["envio"]["envio_id"]
    q = tocar(f"cfs:{eid}", ROSI)
    assert q.avisos and "todavía se está preparando" in q.avisos[0]
    assert envio(mundo, eid)["estado"] == "pendiente"


def test_un_boton_ya_contestado_dice_que_ya_estaba_y_no_vuelve_a_escribir(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    tocar(f"cfn:{eid}", ROSI)
    q = tocar(f"cfs:{eid}", ROSI)
    assert "Ya estaba contestada" in q.pie_final
    assert envio(mundo, eid)["estado"] == "rechazado"


def test_los_botones_de_la_formal_tienen_prefijos_propios(mundo):
    datos = [b.callback_data for fila in botones.teclado_de_envio_de_formal(5).inline_keyboard for b in fila]
    assert datos == ["cfs:5", "cfn:5"]
    assert not set(datos) & {"fas:5", "fan:5", "cns:5", "cnn:5"}


# ── El recordatorio ──────────────────────────────────────────────────────────────────────────────────

def test_a_los_minutos_sale_un_solo_recordatorio_respondiendo_a_la_tarjeta(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    despues = T0 + timedelta(minutes=despertador.RECORDAR_CONSULTA_MIN)
    assert vuelta(tg, despues) == 1
    assert vuelta(tg, despues) == 0
    assert len(tg.mensajes) == 1
    assert tg.mensajes[0]["chat_id"] == ROSI
    assert tg.mensajes[0]["reply_parameters"].message_id == 9001


def test_una_formal_aprobada_no_se_recuerda(mundo, tg):
    cid, eid, _ = con_tarjeta(tg)
    tocar(f"cfs:{eid}", ROSI)
    assert vuelta(tg, T0 + timedelta(minutes=despertador.RECORDAR_CONSULTA_MIN)) == 0
    assert tg.mensajes == []
