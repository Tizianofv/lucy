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


class _AsyncioConHilo:
    """Un `asyncio` que solo cambia `to_thread`; todo lo demás es el de verdad.

    Se pone sobre `correo.asyncio` —un atributo de un módulo DE LUCY, que el
    conftest devuelve a su sitio al terminar cada prueba— y NO sobre el módulo
    `asyncio` del proceso.

    La versión anterior hacía `correo.asyncio.to_thread = ...`, que es escribirle
    encima a la biblioteca estándar: quedaba puesto para el resto de la sesión y
    nadie lo devolvía. Toda prueba posterior que leyera correo recibía ESTOS dos
    crudos en vez de los suyos. El 4-sep-2026 eso dejó 8 pruebas de
    tests/test_reporte_una_vez_al_dia.py en rojo en la primera corrida del freno
    en GitHub (commit 347ff14) —`reporte_diario` devolvía 2 donde se esperaba 1—
    mientras en la Mac salían verdes, porque ahí el venv vive dentro del repo y
    el conftest limpiaba `asyncio` por accidente.
    """

    def __init__(self, crudos):
        self._crudos = crudos

    def to_thread(self, fn, *a, **k):
        return _hecho(self._crudos)

    def __getattr__(self, nombre):
        return getattr(asyncio, nombre)


def _montar(crudos):
    correo.asyncio = _AsyncioConHilo(crudos)

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


def test_el_canario_no_adivina_la_causa():
    """El 31-ago el aviso dijo "casi seguro cambiaron el formato del correo" y
    era falso: había llegado un pago de cliente de un tipo que el parser no
    cubría, con el formato del banco intacto. La alarma acertó el SÍNTOMA y se
    inventó la CAUSA, y una conjetura dentro de una alarma se lee como un hecho
    — manda a arreglar lo que no está roto.

    Lo que el canario sabe es que llegaron correos y no salió ningún
    movimiento. Eso es lo que puede decir.
    """
    import asyncio

    import db.db as base
    from captura import consumos

    capturado = []

    async def _falso(**k):
        capturado.append(k.get("contenido_raw", ""))
        return 1

    real = base.guardar_en_bandeja
    base.guardar_en_bandeja = _falso
    try:
        res = consumos.Resumen()
        res.sin_ruta = {"notificaciones@popularenlinea.com": 1}
        res.fallos = [consumos.Fallo(
            "notificaciones@popularenlinea.com",
            "x#1 [asunto]: no encuentro a quién se le transfirió.")]
        consumos._ultimo_aviso.clear()
        asyncio.new_event_loop().run_until_complete(
            consumos.avisar_si_hay_bancos_mudos(res))
    finally:
        base.guardar_en_bandeja = real

    assert capturado, "el canario no dejó ningún aviso"
    texto = capturado[0]
    for conjetura in ("casi seguro", "seguramente", "cambiaron el formato del correo"):
        assert conjetura not in texto.split("NO DIGAS POR QUÉ PASÓ")[0], (
            f"el aviso vuelve a adivinar la causa: {conjetura!r}")
    # Y sigue diciendo lo que sí sabe, que es lo que lo hace útil.
    assert "no salió ni un movimiento" in texto

    # Lo que ya NO dice, porque era falso en las tres partes: el correo sin
    # parser no se guarda en bandeja, no se conserva el cuerpo y no se
    # conservan los adjuntos. "Están guardados, no se perdió nada" mandaba a
    # Tiziano a no ir a buscarlos, que es lo único que había que hacer.
    for mentira in ("no se perdió nada", "están guardados"):
        assert mentira not in texto.lower(), (
            f"el aviso vuelve a prometer algo que el código no hace: {mentira!r}")
    # Lo único cierto que sí puede decir: la sesión de la ingesta es readonly.
    assert "siguen en Gmail, sin marcar y sin borrar" in texto


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
