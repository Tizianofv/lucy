# -*- coding: utf-8 -*-
"""Un mundo de mentira para las pantallas de TAREAS (`/tareas` y `/tareas/{id}`),
con estado que cambia entre un pedido y el siguiente: marcar una tarea o un paso
la cambia de verdad en la lista y la siguiente página lo ve.

Lo usan `tests/test_tareas_sin_saltar.py` (por la ruta real y la página real) y el
servidor local con el que se midió en un navegador. FRONTERA, dicha: las
RUTAS y las PLANTILLAS son las de producción; lo que se finge es la base
(`db.tareas_por_grupo` corre de verdad contra filas preparadas, igual que en
`tests/test_panel_tareas.py`; cerrar una tarea, marcar un paso y leer una tarea
son dobles que cambian estas listas). No ejercita Postgres.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")
_ps = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_ps.rows = _rows
_pl = types.ModuleType("psycopg_pool")
_pl.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _ps)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pl)

import config  # noqa: E402
import db.db as dbreal  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402
from test_panel_tareas import _Conn, _Pool  # noqa: E402

UTC = timezone.utc


class Mundo:
    """Las tareas y los pasos de mentira, y el doble de `db` con ellos."""

    def __init__(self, n_pendientes=40, n_con_pasos=12):
        base = datetime(2026, 8, 1, tzinfo=UTC)
        self.filas = []
        for i in range(1, n_pendientes + 1):
            self.filas.append({
                "id": i, "titulo": f"Tarea de prueba {i}", "estado": "pendiente",
                "vence_en": None, "creado_en": base + timedelta(minutes=i),
                "bandeja_id": None, "responsable_chat_id": None,
                "completado_en": None, "area": None})
        self.pasos = {1: [{"id": 100 + k, "texto": f"Paso {k}", "hecho": False, "orden": k}
                          for k in range(1, n_con_pasos + 1)]}
        self.cerradas: list[int] = []        # ids cerrados por la ruta de guardar
        self.pasos_escritos: list[tuple] = []
        self.escritos = 0

    def fila(self, tid):
        return next(f for f in self.filas if f["id"] == tid)

    def instalar(self, monkeypatch=None):
        """Cambia `panel.db` y `panel.crud` por los dobles. Devuelve lo que hay que
        deshacer (o nada si se pasó `monkeypatch`)."""
        mundo = self

        class Base:
            def __getattr__(self, nombre):
                return getattr(dbreal, nombre)

            async def tareas_por_grupo(self, *a, **k):
                guardado = dbreal.pool
                dbreal.pool = _Pool(_Conn([dict(f) for f in mundo.filas]))
                try:
                    return await dbreal.tareas_por_grupo(*a, **k)
                finally:
                    dbreal.pool = guardado

            async def areas(self):
                return []

            async def tareas_sin_cerrar_por_proyecto_cerrado(self, ids):
                return []

            async def cerrar_y_derivar(self, chat, tid, derivadas):
                try:
                    f = mundo.fila(tid)
                except StopIteration:
                    return None            # como la real: no existe o está en la papelera
                if f["estado"] != "pendiente":
                    return False, []
                f["estado"] = "hecha"
                f["completado_en"] = datetime.now(UTC)
                mundo.cerradas.append(tid)
                mundo.escritos += 1
                return True, []

            async def tarea_con_comentarios(self, tid):
                try:
                    f = mundo.fila(tid)
                except StopIteration:
                    return None
                return {"tarea": dict(f, proyecto_id=None, proyecto_nombre=None,
                                      primero_id=None, primero_titulo=None,
                                      primero_esperando=False, tomada_en=None, grave=False),
                        "comentarios": [], "pasos": [dict(p) for p in mundo.pasos.get(tid, [])]}

            async def tareas_para_elegir_primero(self, tid):
                return []

            async def pertenece_paso(self, tid, pid):
                return any(p["id"] == pid for p in mundo.pasos.get(tid, []))

        class Crud:
            async def editar(self, tabla, pid, cambios, motivo="", actor="lucy"):
                assert tabla == "micro_pasos"
                for ps in mundo.pasos.values():
                    for p in ps:
                        if p["id"] == pid:
                            p["hecho"] = bool(cambios["hecho"])
                            mundo.pasos_escritos.append((pid, p["hecho"]))
                            mundo.escritos += 1
                            return dict(p), 1
                return None, None

        if monkeypatch is not None:
            monkeypatch.setattr(panel, "db", Base())
            monkeypatch.setattr(panel, "crud", Crud())
        else:
            panel.db = Base()
            panel.crud = Crud()
        return self


def galleta() -> str:
    return auth.crear_token(config.CHAT_ID_DUENO, auth.VIDA_SESION)
