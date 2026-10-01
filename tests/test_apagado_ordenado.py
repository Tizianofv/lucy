"""El apagado de Lucy al recibir SIGTERM (un redespliegue en Railway).

Medido el 1-oct-2026: con el panel (uvicorn) en el mismo proceso, SIGTERM no
apagaba en orden -- ver `main._servidor_del_panel`. Estas pruebas corren
`main.main()` de verdad en un proceso aparte (ver `tests/_arnes_apagado.py`
para qué es real y qué es doble) y le mandan SIGTERM.

Cada prueba usa su propio proceso y su propio puerto: no chocan en paralelo.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
ARNES = RAIZ / "tests" / "_arnes_apagado.py"


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _correr(escenario: str):
    env = dict(os.environ, PORT=str(_puerto_libre()), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(ARNES), escenario], cwd=RAIZ, env=env,
                       capture_output=True, text=True, timeout=60)
    lineas = [x for x in r.stdout.splitlines() if x.startswith("RESULTADO ")]
    assert lineas, f"sin RESULTADO (rc={r.returncode}); stderr:\n{r.stderr[-3000:]}"
    return json.loads(lineas[-1][len("RESULTADO "):]), r


def _ordenado(res, r):
    """El apagado ordenado, visto desde afuera: nada destruido, y corrieron
    Application.stop (espera a los handlers) y db.cerrar (el final de
    `_al_apagar`)."""
    assert "Task was destroyed" not in r.stderr, r.stderr[-3000:]
    assert "Event loop is closed" not in r.stderr, r.stderr[-3000:]
    assert "Application.stop" in res["eventos"], res["eventos"]
    assert "db.cerrar" in res["eventos"], res["eventos"]


def test_sigterm_con_el_panel_activo_apaga_en_orden():
    res, r = _correr("turno")
    _ordenado(res, r)


def test_fila_a_medias_vuelve_a_sin_procesar_sin_sumar_intentos():
    res, r = _correr("turno")
    _ordenado(res, r)
    assert res["filas"] == {"1": ["sin_procesar", 0]}, res["filas"]
    assert "SQL devolver_reclamadas" in res["eventos"]
    # y se devolvió ANTES de cerrar la base
    assert (res["eventos"].index("SQL devolver_reclamadas")
            < res["eventos"].index("db.cerrar"))


def test_update_ya_confirmado_con_handler_en_vuelo_queda_en_la_bandeja():
    res, r = _correr("guardar")
    _ordenado(res, r)
    assert "1" in res["filas"], res["filas"]
    # Telegram lo dio por entregado (offset 1001): sin esto, estaría perdido.
    assert 1001 in res["offsets"], res["offsets"]


def test_dos_updates_en_la_misma_tanda_no_se_pierden():
    res, r = _correr("dos")
    _ordenado(res, r)
    assert sorted(res["filas"]) == ["1", "2"], res["filas"]
    assert 1002 in res["offsets"], res["offsets"]


def test_fila_terminada_no_se_revive_al_apagar():
    res, r = _correr("fin")
    _ordenado(res, r)
    assert res["filas"] == {"1": ["procesado", 0]}, res["filas"]
    # y el bucle ya la había soltado: no hay nada que devolver, ni se consulta
    assert "SQL devolver_reclamadas" not in res["eventos"], res["eventos"]


def test_control_sin_sigterm_no_se_toca_nada():
    res, r = _correr("quieto")
    assert "SQL devolver_reclamadas" not in res["eventos"]
    assert "db.cerrar" not in res["eventos"]
    assert res["filas"] == {"1": ["procesando", 0]}, res["filas"]


# ── el SQL de devolver_reclamadas, ejecutado de verdad (sqlite) ─────────────
# Mismo texto que corre en Postgres, salvo `= ANY(%s)` -> `IN (?, ...)` y `%s`
# -> `?`. No es Postgres: el parecido que importa aquí es el filtro de estado y
# qué columnas se tocan.
import asyncio  # noqa: E402
import sqlite3  # noqa: E402

for _k, _v in (("TELEGRAM_TOKEN", "1:fake"), ("DATABASE_URL", "postgresql://x/x"),
               ("CHAT_ID_DUENO", "111")):
    os.environ.setdefault(_k, _v)   # config.py las exige al importarse

import db.db as base  # noqa: E402


def _con_sqlite(monkeypatch, filas):
    cx = sqlite3.connect(":memory:")
    cx.execute("CREATE TABLE bandeja (id INTEGER PRIMARY KEY, estado TEXT, "
               "intentos INT, reintentar_despues TEXT, error_detalle TEXT)")
    cx.executemany("INSERT INTO bandeja VALUES (?,?,?,NULL,NULL)", filas)

    class _Conn:
        async def execute(self, sql, params=()):
            ids = list(params[0])
            sql = sql.replace("= ANY(%s)", "IN (" + ",".join("?" * len(ids)) + ")")
            filas_ = cx.execute(sql, ids).fetchall()

            class _Cur:
                async def fetchall(self):
                    return filas_
            return _Cur()

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _Ctx()

    monkeypatch.setattr(base, "pool", _Pool())
    return cx


def test_sql_devuelve_procesando_sin_tocar_intentos_ni_espera(monkeypatch):
    cx = _con_sqlite(monkeypatch, [(1, "procesando", 2), (2, "procesando", 0)])
    n = asyncio.run(base.devolver_reclamadas([1, 2]))
    assert n == 2
    assert cx.execute("SELECT id, estado, intentos, reintentar_despues, "
                      "error_detalle FROM bandeja ORDER BY id").fetchall() == [
        (1, "sin_procesar", 2, None, None), (2, "sin_procesar", 0, None, None)]


def test_sql_no_revive_una_fila_ya_cerrada(monkeypatch):
    cx = _con_sqlite(monkeypatch, [(1, "procesado", 0), (2, "error", 1),
                                   (3, "esperando_confirmacion", 0),
                                   (4, "procesando", 0), (5, "procesando", 0)])
    n = asyncio.run(base.devolver_reclamadas([1, 2, 3, 4]))
    assert n == 1
    assert cx.execute("SELECT id, estado FROM bandeja ORDER BY id").fetchall() == [
        (1, "procesado"), (2, "error"), (3, "esperando_confirmacion"),
        (4, "sin_procesar"), (5, "procesando")]   # la 5 no era de esta instancia


def test_sin_ids_no_toca_la_base(monkeypatch):
    class _Boom:
        def connection(self):
            raise AssertionError("no debía abrir conexión")
    monkeypatch.setattr(base, "pool", _Boom())
    assert asyncio.run(base.devolver_reclamadas([])) == 0
