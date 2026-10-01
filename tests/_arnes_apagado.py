"""Arnés de `tests/test_apagado_ordenado.py`. NO es una prueba: es un guion que
corre en un proceso aparte (las señales hay que mandárselas a un proceso, y
`main.main()` cierra el bucle de eventos al terminar).

Corre `main.main()` REAL: PTB real (`Application.run_polling`, con su manejo de
SIGTERM), uvicorn real sirviendo el panel real, `captura.telegram` real,
`cerebro.interpretar.bucle/_procesar` reales y `db.devolver_reclamadas` real.
Los dobles están en el borde y son solo estos:
  · HTTP de Telegram: `ExtBot._do_post` (getMe, getUpdates, sendMessage). El
    contrato de `getUpdates` que importa aquí -- el `offset` del siguiente
    pedido confirma lo entregado -- lo hace PTB real (`_updater.py`).
  · `db.db`: estado en memoria que copia las transiciones del SQL de
    `db/db.py` (`guardar_en_bandeja`, `tomar_pendientes`) y un `pool` falso que
    ejecuta el UPDATE REAL de `devolver_reclamadas` interpretando sus
    parámetros. NO hay Postgres en la suite: ese SQL no se ejecuta contra uno.
  · `agente.atender`: duerme 30 s (un turno largo de DeepSeek) o termina al
    instante (escenario `fin`).
Uso: python _arnes_apagado.py ESCENARIO   con la raíz del repo como cwd.
ESCENARIOS: turno | guardar | dos | fin | quieto | lote
Imprime una línea `RESULTADO {json}`.
"""
import asyncio
import json
import os
import signal
import sys
import threading
import time

ESC = sys.argv[1]
os.environ.update(TELEGRAM_TOKEN="1:fake", DATABASE_URL="postgresql://x/x",
                  CHAT_ID_DUENO="111")
sys.path.insert(0, os.getcwd())

EVENTOS: list[str] = []
FILAS: dict[int, dict] = {}


def ev(txt):
    EVENTOS.append(txt)


import telegram.ext  # noqa: E402
from telegram.ext import ExtBot  # noqa: E402

import cerebro.agente as agente  # noqa: E402
import cerebro.calendario as calendario  # noqa: E402
import cerebro.deepseek as motor  # noqa: E402
import cerebro.interpretar as interpretar  # noqa: E402
import cerebro.vision as vision  # noqa: E402
import cerebro.whisper as whisper  # noqa: E402
import db.db as db  # noqa: E402

interpretar.INTERVALO_S = 1   # que el despertador (vuelta % 6) no llegue a correr

GATILLO = threading.Event()


class _Conn:
    async def execute(self, sql, params=()):
        # El UPDATE real de devolver_reclamadas. Se aplica leyendo su texto:
        # si alguien le agrega `intentos = ...` o quita el filtro de estado,
        # esto lo refleja o se niega.
        assert "UPDATE bandeja" in sql and "= ANY(%s)" in sql, sql
        ev("SQL devolver_reclamadas")
        quitar_filtro = "estado = 'procesando'" not in sql
        suma = "intentos" in sql
        devueltas = []
        for i in params[0]:
            f = FILAS.get(i)
            if f and (quitar_filtro or f["estado"] == "procesando"):
                f["estado"] = "sin_procesar"
                if suma:
                    f["intentos"] += 1
                devueltas.append((i,))

        class _Cur:
            async def fetchall(self_inner):
                return devueltas
        return _Cur()


class _Pool:
    def connection(self):
        class _Ctx:
            async def __aenter__(self_c):
                return _Conn()

            async def __aexit__(self_c, *a):
                return False
        return _Ctx()


async def abrir():
    ev("db.abrir")


async def cerrar():
    ev("db.cerrar")


async def rescatar_procesando(minutos=10, max_intentos=3):
    return 0


async def vacio():
    return []


async def guardar_en_bandeja(*, tipo_entrada, contenido_raw=None, archivo_id=None,
                             chat_id=None, telegram_msg_id=None, origen="telegram"):
    if ESC in ("guardar", "dos"):
        GATILLO.set()
        await asyncio.sleep(3)          # base lenta: el handler sigue en vuelo
    for i, f in FILAS.items():
        if (f["chat_id"], f["msg"]) == (chat_id, telegram_msg_id):
            return i
    i = len(FILAS) + 1
    FILAS[i] = dict(estado="sin_procesar", intentos=0, chat_id=chat_id,
                    msg=telegram_msg_id, tipo_entrada=tipo_entrada,
                    contenido_raw=contenido_raw, transcripcion=None)
    return i


async def tomar_pendientes(tipos=(), limite=5):
    out = []
    for i, f in sorted(FILAS.items()):
        if f["estado"] == "sin_procesar" and len(out) < limite:
            f["estado"] = "procesando"
            out.append(dict(id=i, archivo_id=None, telegram_msg_id=f["msg"],
                            **{k: f[k] for k in ("tipo_entrada", "contenido_raw",
                                                 "chat_id", "intentos",
                                                 "transcripcion")}))
    return out


db.pool = _Pool()
db.abrir, db.cerrar = abrir, cerrar
db.rescatar_procesando = rescatar_procesando
db.tablas_que_faltan = db.columnas_que_faltan = db.objetos_que_faltan = vacio
db.guardar_en_bandeja, db.tomar_pendientes = guardar_en_bandeja, tomar_pendientes


async def nada():
    return None


async def sin_calendarios():
    return []

motor.verificar_modelo = whisper.verificar = vision.verificar = nada
calendario.verificar = sin_calendarios


async def atender(fila, texto, bot):
    if ESC == "fin":
        FILAS[fila["id"]]["estado"] = "procesado"
        GATILLO.set()
        return
    GATILLO.set()
    await asyncio.sleep(30)
    FILAS[fila["id"]]["estado"] = "procesado"

agente.atender = atender

OFFSETS: list = []
_ahora = int(time.time())


def _upd(uid, mid):
    return {"update_id": uid, "message": {
        "message_id": mid, "date": _ahora, "chat": {"id": 111, "type": "private"},
        "from": {"id": 111, "is_bot": False, "first_name": "T"}, "text": "x"}}


_cola = [[_upd(1000, 7), _upd(1001, 8)] if ESC == "dos" else
         [] if ESC == "lote" else [_upd(1000, 7)]]
if ESC == "lote":   # tres filas ya en la bandeja: tomar_pendientes las reclama juntas
    for _i in (1, 2, 3):
        FILAS[_i] = dict(estado="sin_procesar", intentos=0, chat_id=111, msg=_i,
                         tipo_entrada="texto", contenido_raw="x", transcripcion=None)


async def _do_post(self, endpoint, data, request_data=None, **kw):
    if endpoint == "getMe":
        return {"id": 1, "is_bot": True, "first_name": "L", "username": "lucy_bot"}
    if endpoint == "getUpdates":
        OFFSETS.append(data.get("offset"))
        if _cola:
            return _cola.pop(0)
        await asyncio.sleep(1)
        return []
    if endpoint == "sendMessage":
        return {"message_id": 99, "date": _ahora,
                "chat": {"id": 111, "type": "private"}}
    return True

ExtBot._do_post = _do_post

_stop = telegram.ext.Application.stop
_orig_stop = _stop


async def _stop_marcado(self):
    ev("Application.stop")
    await _orig_stop(self)

telegram.ext.Application.stop = _stop_marcado


def _disparar():
    GATILLO.wait()
    time.sleep(1.0 if ESC in ("fin", "quieto") else 0.5)
    if ESC == "quieto":          # control: ninguna señal; se corta sin apagar
        print("RESULTADO " + json.dumps(_resultado()), flush=True)
        os._exit(0)
    os.kill(os.getpid(), signal.SIGTERM)


def _resultado():
    return {"eventos": EVENTOS,
            "filas": {str(i): [f["estado"], f["intentos"]] for i, f in FILAS.items()},
            "offsets": OFFSETS}


# Marcas de ORDEN del apagado: cuándo terminan de verdad el panel y el bucle.
import uvicorn  # noqa: E402

_serve = uvicorn.Server.serve


async def _serve_marcado(self, *a, **k):
    try:
        return await _serve(self, *a, **k)
    finally:
        ev("panel termino")

uvicorn.Server.serve = _serve_marcado
_bucle = interpretar.bucle


async def _bucle_marcado(bot):
    try:
        return await _bucle(bot)
    finally:
        ev("bucle termino")

interpretar.bucle = _bucle_marcado

threading.Thread(target=_disparar, daemon=True).start()
import main as lucy_main  # noqa: E402

lucy_main.main()
print("RESULTADO " + json.dumps(_resultado()), flush=True)
