"""Test de t-13: cada buzón informa a quien le corresponde.

Es la línea que separa "Lucy lee el correo de Rosi para sacar sus movimientos"
de "Lucy le cuenta a Tiziano lo que le escriben a Rosi". El sistema tiene que
poder hacer lo primero sin lo segundo, y por defecto no debe cambiar nada de lo
que ya funcionaba.

Correr:  python3 tests/test_reporte_destino.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "777")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")


class _Any:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


for n, attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object}),
                 ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                 ("openai", {"AsyncOpenAI": _Any, "OpenAI": _Any}),
                 ("httpx", {"HTTPError": type("H", (Exception,), {})})):
    m = types.ModuleType(n)
    for k, v in attrs.items():
        setattr(m, k, v)
    m.__getattr__ = lambda name: _Any()
    sys.modules[n] = m

import captura.correo as correo  # noqa: E402
import config  # noqa: E402

DUENO = config.CHAT_ID_DUENO


def test_sin_el_campo_va_al_dueno():
    """El comportamiento de antes. Un cambio que rompa esto rompe el reporte de
    todos los días."""
    assert correo.destino_del_reporte({"user": "a@b.com"}) == DUENO


def test_reporte_a_manda_a_otro_chat():
    assert correo.destino_del_reporte(
        {"user": "rosi@x.com", "reporte_a": 12345}) == 12345


def test_cero_significa_no_informar_a_nadie():
    """El caso de Rosi mientras no tenga su propio reporte: su buzón se escanea
    para bancos y su correspondencia no aparece en el briefing de nadie."""
    assert correo.destino_del_reporte({"user": "r@x.com", "reporte_a": 0}) == 0
    assert correo.destino_del_reporte({"user": "r@x.com", "reporte": False}) == 0


def test_un_valor_roto_no_deja_el_correo_sin_destino():
    """Ante un valor inválido va al dueño, no al vacío: perder el reporte en
    silencio sería peor que mandarlo a quien ya lo recibía."""
    assert correo.destino_del_reporte(
        {"user": "x@y.com", "reporte_a": "ninguno"}) == DUENO


def test_el_encargo_se_arma_por_destino_no_en_un_monton():
    """Juntar los correos de varios buzones en un solo encargo mandaría el
    correo de una persona al chat de otra."""
    import inspect
    fuente = inspect.getsource(correo.reporte_diario)
    assert "por_destino" in fuente
    assert "chat_id=destino" in fuente, (
        "el encargo sigue yendo a un chat fijo en vez de al destino del buzón")


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
