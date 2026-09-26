# -*- coding: utf-8 -*-
"""`main.py::_al_fallar` (el manejador de errores de última red del bot,
pilar #39) sigue yendo directo a quien mandó el mensaje -- NUNCA crea una
tarea de Code (§B.4 del diseño, tabla de censo: «`_al_fallar` (B.1-bis)
sigue yendo directo a Tiziano»).

Hallazgo del testigo (NO PASA sobre 00eb9e6, parte 4): ninguna prueba en
todo `tests/` cubría esto, y la parte 4 solo pedía comprobarlo con censo de
texto (Regla 18: un censo de texto se burla con un alias o un hermano que
delega). Acá se corre `_al_fallar` DE VERDAD -- la función real de
`main.py`, sin doblar -- con un `update`/`context` de mentira y un
`msg.reply_text` espía, y se comprueba que el aviso le llega a quien
disparó el error (comportamiento, no texto) y que la puerta de Code
(`db.crear_o_reusar_alerta_tecnica`) nunca se toca.

Correr:  python3 -m pytest tests/test_al_fallar_no_va_a_code.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-al-fallar")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "111")
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
                   ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera})):
    _m = types.ModuleType(_n)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    _m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[_n] = _m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from telegram.error import Conflict  # noqa: E402

import db.db as db  # noqa: E402
import main  # noqa: E402


def _correr(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _MensajeEspia:
    def __init__(self):
        self.respuestas: list[str] = []

    async def reply_text(self, texto):
        self.respuestas.append(texto)


class _UpdateFalso:
    def __init__(self, msg):
        self.effective_message = msg


class _ContextoFalso:
    def __init__(self, error):
        self.error = error


def test_al_fallar_le_responde_a_quien_disparo_el_error_y_no_toca_code():
    """LA GARANTÍA CENTRAL: un fallo real (no `Conflict`, que tiene su
    propio camino silencioso) le llega de vuelta al chat que lo disparó y
    NUNCA pasa por `db.crear_o_reusar_alerta_tecnica`."""
    llamadas_a_code = []

    async def _crear_o_reusar_espia(clave, titulo, detalle):
        llamadas_a_code.append(clave)
        return 999

    guardado_puerta = db.crear_o_reusar_alerta_tecnica
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_espia
    msg = _MensajeEspia()
    update = _UpdateFalso(msg)
    contexto = _ContextoFalso(RuntimeError("la base no respondió"))
    try:
        _correr(main._al_fallar(update, contexto))
        assert len(msg.respuestas) == 1, (
            f"esperaba UNA respuesta directa al chat, salieron {len(msg.respuestas)}")
        assert "no pude guardarlo" in msg.respuestas[0].lower()
        assert llamadas_a_code == [], (
            f"_al_fallar llamó a la puerta de Code con {llamadas_a_code}; "
            "tiene que seguir yendo directo al chat que disparó el error")
    finally:
        db.crear_o_reusar_alerta_tecnica = guardado_puerta


def test_al_fallar_con_conflict_no_responde_ni_toca_code():
    """El `Conflict` (dos instancias peleando el long-polling en un
    redespliegue) es esperable: no le contesta a nadie, y tampoco crea nada
    en Code."""
    llamadas_a_code = []

    async def _crear_o_reusar_espia(clave, titulo, detalle):
        llamadas_a_code.append(clave)
        return 999

    guardado_puerta = db.crear_o_reusar_alerta_tecnica
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_espia
    msg = _MensajeEspia()
    update = _UpdateFalso(msg)
    contexto = _ContextoFalso(Conflict("dos instancias"))
    try:
        _correr(main._al_fallar(update, contexto))
        assert msg.respuestas == []
        assert llamadas_a_code == []
    finally:
        db.crear_o_reusar_alerta_tecnica = guardado_puerta


def test_al_fallar_sin_mensaje_asociado_no_revienta_ni_toca_code():
    """Un fallo sin `effective_message` (p. ej. de red) solo queda en el
    log -- no revienta, y tampoco se desvía hacia Code."""
    llamadas_a_code = []

    async def _crear_o_reusar_espia(clave, titulo, detalle):
        llamadas_a_code.append(clave)
        return 999

    guardado_puerta = db.crear_o_reusar_alerta_tecnica
    db.crear_o_reusar_alerta_tecnica = _crear_o_reusar_espia
    update = _UpdateFalso(None)
    contexto = _ContextoFalso(RuntimeError("fallo de red"))
    try:
        _correr(main._al_fallar(update, contexto))
        assert llamadas_a_code == []
    finally:
        db.crear_o_reusar_alerta_tecnica = guardado_puerta


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
