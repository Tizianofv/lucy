"""Test de t-12: los correos bancarios no entran al reporte matinal.

Cada consumo con tarjeta llega como un correo, y `SISTEMA_CLASIFICA` tiene una
regla explícita que manda clasificar los movimientos del banco como "accion".
Sin este filtro, con varias compras al día el briefing matinal se convertiría en
una lista de compras — que Tiziano ya puede ver mejor en el panel — y cada una
costaría una llamada a DeepSeek.

Lo que se protege es que el filtro use los REMITENTES REGISTRADOS y no una lista
aparte: una lista aparte se olvida de actualizar cuando se añade un banco.

Correr:  python3 tests/test_reporte_sin_bancos.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "x")

class _Cualquiera:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _Cualquiera()

    def __call__(self, *a, **k):
        return _Cualquiera()


for n, attrs in (("psycopg", {}), ("psycopg.rows", {"dict_row": object}),
                 ("psycopg_pool", {"AsyncConnectionPool": lambda *a, **k: None}),
                 ("openai", {"AsyncOpenAI": _Cualquiera, "OpenAI": _Cualquiera}),
                 ("httpx", {"HTTPError": type("H", (Exception,), {})})):
    m = types.ModuleType(n)
    for k, v in attrs.items():
        setattr(m, k, v)
    m.__getattr__ = lambda name: _Cualquiera()
    sys.modules[n] = m

import captura.correo as correo  # noqa: E402
import cerebro.bancos as bancos  # noqa: E402
import db.db as db  # noqa: E402


def _correr(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def _montar(crudos):
    correo.asyncio.to_thread = lambda fn, *a, **k: _hecho(crudos)

    async def _ya(cuenta, uids):
        return set()
    db.correos_ya_reportados = _ya


async def _hecho(v):
    return v


def test_los_bancos_registrados_no_llegan_al_reporte():
    reg = list(bancos.remitentes_registrados())
    assert reg, "no hay parsers registrados; el test no probaría nada"
    crudos = [{"uid": 1, "from": f"BHD <{reg[0]}>", "subject": "consumo",
               "snippet": "", "cuenta": "x@y.com", "ruido_barato": None},
              {"uid": 2, "from": "Jorge <jorge@ejemplo.com>",
               "subject": "cotización", "snippet": "", "cuenta": "x@y.com",
               "ruido_barato": None}]
    _montar(crudos)
    correo.clasificar = lambda c, r="": _hecho(
        {"ambito": "", "area": "", "nivel": "accion",
         "asunto_corto": c["subject"], "motivo": ""})
    salida = _correr(correo._pendientes_de({"user": "x@y.com"}, ""))
    remitentes = [c["from"] for c in salida]
    assert len(salida) == 1, f"esperaba 1 correo, llegaron {len(salida)}"
    assert "jorge@ejemplo.com" in remitentes[0]


def test_el_filtro_sale_del_registro_y_no_de_una_lista_aparte():
    """Si mañana se añade un banco, tiene que salir del reporte solo. Una lista
    duplicada en este archivo se olvidaría de actualizar."""
    import inspect
    fuente = inspect.getsource(correo._pendientes_de)
    assert "remitentes_registrados" in fuente, (
        "el filtro no usa el registro de parsers: se va a desincronizar")


def test_un_correo_normal_del_mismo_dominio_si_pasa():
    """De bhd.com.do solo `alertas@` es transaccional. Un correo de una persona
    del banco tiene que seguir llegando al reporte."""
    crudos = [{"uid": 3, "from": "Ana <ana@bhd.com.do>", "subject": "tu préstamo",
               "snippet": "", "cuenta": "x@y.com", "ruido_barato": None}]
    _montar(crudos)
    correo.clasificar = lambda c, r="": _hecho(
        {"ambito": "", "area": "", "nivel": "accion",
         "asunto_corto": c["subject"], "motivo": ""})
    salida = _correr(correo._pendientes_de({"user": "x@y.com"}, ""))
    assert len(salida) == 1, "un humano del banco no es una alerta automática"


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
