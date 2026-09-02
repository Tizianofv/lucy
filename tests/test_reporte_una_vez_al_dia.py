# -*- coding: utf-8 -*-
"""El reporte de correo sale UNA vez al día. Test del candado, ejecutándolo.

Por qué existe este archivo: el defecto no podía salir en rojo. Ninguna suite
llamaba a `reporte_diario()`, así que el candado de "hoy ya salió" nunca se
ejecutaba en los tests — y llevaba meses roto en producción. La condición pedía
una fila en `correo_estado` por CADA cuenta, el único código que escribía esa
fila solo sabía ACTUALIZAR filas ya existentes, y el bloque que las creaba se
había borrado en julio. Sin fila, el candado no cerraba nunca: el bucle llama a
esto cada ~3 minutos y la ventana va de 7 a 12, así que el reporte salía ~100
veces por mañana.

El candado nuevo es el del briefing matinal: la marca de "hoy ya salió" es el
PROPIO encargo que el reporte deja en la bandeja. Lee lo mismo que escribe, así
que no hay nada que inicializar.

Y hay un candado POR DESTINATARIO, no uno global. La primera versión de este
arreglo preguntaba "¿alguien recibió reporte hoy?", y con dos buzones que
informan a chats distintos (`reporte_a`) eso deja al segundo destino sin nada
hasta mañana. Ver la sección "Dos buzones, dos destinos".

Su riesgo conocido —depende del TEXTO del encargo— está atado acá abajo con
`test_el_texto_del_encargo_y_el_del_candado_son_el_mismo`.

Correr:  python3 tests/test_reporte_una_vez_al_dia.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://t/t")
os.environ.setdefault("CHAT_ID_DUENO", "777")
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

import captura.correo as correo  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

TZ = config.TZ


# ── El doble de la base: la bandeja de verdad es una lista ────────────────

class _Bandeja:
    """Guarda las filas que se escriben y responde el candado LEYENDO ESAS
    MISMAS filas, con la misma condición que el SQL de
    `destinos_con_encargo_hoy`: origen, tipo_entrada='sistema', prefijo del
    contenido, creado_en del día — y el chat_id, que es lo que hace que el
    candado sea de cada destinatario y no de todos a la vez.
    """

    def __init__(self, reloj):
        self.filas: list[dict] = []
        self.reportados: list[tuple] = []
        self.reloj = reloj

    async def guardar_en_bandeja(self, **kw):
        self.filas.append({"origen": kw.get("origen"),
                           "tipo_entrada": kw.get("tipo_entrada"),
                           "contenido_raw": kw.get("contenido_raw") or "",
                           "chat_id": kw.get("chat_id"),
                           "creado_en": self.reloj.ahora})
        return len(self.filas)

    def _encargos_de_hoy(self, origen, prefijo, desde):
        return [f for f in self.filas
                if f["origen"] == origen
                and f["tipo_entrada"] == "sistema"
                and f["contenido_raw"].startswith(prefijo)
                and f["creado_en"] >= desde]

    async def destinos_con_encargo_hoy(self, origen, prefijo, desde):
        """Misma condición que el SQL, más el DISTINCT chat_id."""
        return {f["chat_id"] for f in self._encargos_de_hoy(origen, prefijo, desde)
                if f["chat_id"] is not None}

    # El candado GLOBAL, el de la primera versión de este arreglo. Se deja por
    # lo mismo que `leer_estado_correo`: si alguien vuelve a atar el reporte a
    # un booleano sin destino, esto responde igual que respondía y el test de
    # los dos destinos se pone en rojo por el motivo correcto —el destino 999
    # se queda sin su reporte— en vez de reventar con AttributeError.
    async def ya_hubo_encargo_hoy(self, origen, prefijo, desde):
        return bool(self._encargos_de_hoy(origen, prefijo, desde))

    async def marcar_correo_reportado(self, cuenta, uid, **kw):
        self.reportados.append((cuenta, uid))

    async def correos_ya_reportados(self, cuenta, uids):
        ya = {u for c, u in self.reportados if c == cuenta}
        return {u for u in uids if u in ya}

    async def listar_preferencias(self):
        return []

    # El candado viejo. Se deja para que el test siga siendo honesto: si
    # alguien vuelve a atar el reporte a `correo_estado`, esto devuelve None
    # (que es lo que devuelve la base real: no hay fila para ninguna cuenta) y
    # los tests de abajo se ponen en rojo, igual que estaba producción.
    async def leer_estado_correo(self, cuenta):
        return None

    async def guardar_estado_correo(self, *a, **k):
        return None

    @property
    def encargos(self):
        return [f for f in self.filas
                if f["contenido_raw"].startswith(correo.MARCA_ENCARGO)]


class _Reloj:
    """Un reloj fijo, para poder pararse a las 7:10 de la mañana."""

    def __init__(self, ahora):
        self.ahora = ahora

    def now(self, tz=None):
        return self.ahora if tz is None else self.ahora.astimezone(tz)


def _montar(hora=7, minuto=10, crudos=None):
    reloj = _Reloj(datetime(2026, 9, 2, hora, minuto, tzinfo=TZ))
    bandeja = _Bandeja(reloj)
    correo.datetime = reloj                      # solo se usa para .now(TZ)
    for n in ("guardar_en_bandeja", "ya_hubo_encargo_hoy",
              "destinos_con_encargo_hoy",
              "marcar_correo_reportado", "correos_ya_reportados",
              "listar_preferencias", "leer_estado_correo",
              "guardar_estado_correo"):
        setattr(db, n, getattr(bandeja, n))
    config.CORREO_CUENTAS = [{"user": "tizianofv@gmail.com", "pass": "x"},
                             {"user": "cds@ejemplo.com", "pass": "x"}]
    _servir(crudos or [])
    correo.clasificar = lambda c, r="": _hecho(
        {"ambito": "laboral", "area": "cds_clientes", "nivel": "accion",
         "asunto_corto": c["subject"][:120], "motivo": ""})
    return bandeja, reloj


BUZON_CON_CORREO = "tizianofv@gmail.com"


def _servir(crudos):
    """Los correos llegan a UN solo buzón. El otro queda vacío a propósito: el
    candado viejo exigía una fila por cada cuenta configurada, y ese "cada" era
    la mitad del defecto."""
    correo._sin_leer_sync = lambda cuenta, dias, limite: (
        [dict(c, cuenta=cuenta["user"]) for c in crudos]
        if cuenta["user"] == BUZON_CON_CORREO else [])


async def _hecho(v):
    return v


def _uno(uid=1, asunto="cotización del disco"):
    return {"uid": uid, "from": "Jorge <jorge@ejemplo.com>", "subject": asunto,
            "snippet": "hola", "ruido_barato": None}


def _correr(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── El candado, ejecutado de verdad ──────────────────────────────────────

def test_el_reporte_sale_una_sola_vez_aunque_el_bucle_llame_cien_veces():
    """EL test que faltaba. El bucle llama cada ~3 min de 7 a 12: ~100 veces.
    Sin candado que cierre, son ~100 reportes — que es lo que pasó de verdad."""
    bandeja, _ = _montar(crudos=[_uno()])
    primero = _correr(correo.reporte_diario())
    assert primero == 1, f"el primer reporte no salió: devolvió {primero}"
    for _ in range(99):
        assert _correr(correo.reporte_diario()) == 0
    assert len(bandeja.encargos) == 1, (
        f"el reporte dejó {len(bandeja.encargos)} encargos en la bandeja en una "
        "sola mañana; tenía que dejar 1")


def test_el_candado_cierra_sin_ninguna_fila_previa_en_la_base():
    """El defecto exacto: `correo_estado` está vacía y NADIE crea la primera
    fila. Un candado que necesita que alguien lo inicialice no es un candado."""
    bandeja, _ = _montar(crudos=[_uno()])
    assert _correr(db.leer_estado_correo("tizianofv@gmail.com")) is None, (
        "el montaje ya no reproduce el caso real: aquí no hay fila previa")
    assert _correr(correo.reporte_diario()) == 1
    # Y con correo NUEVO en el buzón, que es cuando el candado viejo se caía:
    # sin fila en `correo_estado` la condición nunca se cumplía y volvía a
    # salir un reporte entero en la vuelta siguiente, tres minutos después.
    _servir([_uno(2, "otro que llegó")])
    assert _correr(correo.reporte_diario()) == 0, (
        "el reporte volvió a salir: el candado quedó abierto sin fila previa")
    assert len(bandeja.encargos) == 1


def test_un_correo_que_llega_despues_espera_a_manana():
    """Lo que NO cambia. Ya salió el reporte de hoy: lo que entre después no
    lo reabre, aunque siga siendo por la mañana."""
    bandeja, reloj = _montar(crudos=[_uno()])
    assert _correr(correo.reporte_diario()) == 1
    _servir([_uno(2, "llegué tarde")])
    reloj.ahora = datetime(2026, 9, 2, 9, 30, tzinfo=TZ)
    assert _correr(correo.reporte_diario()) == 0, (
        "un correo de las 9:30 disparó un segundo reporte el mismo día")
    assert len(bandeja.encargos) == 1


def test_manana_vuelve_a_salir():
    """Un candado que no se abre nunca más es peor que el defecto que arregla."""
    bandeja, reloj = _montar(crudos=[_uno()])
    assert _correr(correo.reporte_diario()) == 1
    reloj.ahora = datetime(2026, 9, 3, 7, 10, tzinfo=TZ)
    _servir([_uno(2, "cosa de mañana")])
    assert _correr(correo.reporte_diario()) == 1, "no salió el reporte del día siguiente"
    assert len(bandeja.encargos) == 2


def test_el_texto_del_encargo_y_el_del_candado_son_el_mismo():
    """El riesgo conocido de este candado: reconoce el encargo por su primera
    frase. Si alguien la reescribe en `_encargo()` y no toca `MARCA_ENCARGO`,
    el candado se abre y volvemos a los ~100 reportes — sin que nada falle.
    Este test es lo único que ata las dos puntas."""
    _montar(crudos=[_uno()])
    texto = correo._encargo([
        {"from": "Jorge <jorge@ejemplo.com>", "cuenta": "x@y.com", "uid": 1,
         "snippet": "hola",
         "clasificacion": {"nivel": "accion", "area": "cds_clientes",
                           "asunto_corto": "cotización"}}])
    assert texto.startswith(correo.MARCA_ENCARGO), (
        "el encargo ya no empieza con MARCA_ENCARGO: el candado no lo va a "
        f"reconocer.\n  encargo: {texto[:80]!r}\n  marca:   {correo.MARCA_ENCARGO!r}")
    import inspect
    fuente = inspect.getsource(correo.reporte_diario)
    assert "MARCA_ENCARGO" in fuente, (
        "el candado busca un literal suelto en vez de la constante que escribe "
        "el encargo: se van a desincronizar")


def test_el_candado_se_consulta_antes_de_abrir_gmail():
    """Si el candado se comprobara después de mirar el buzón, las ~100 vueltas
    de la mañana seguirían abriendo IMAP 100 veces aunque no se duplicara el
    reporte."""
    bandeja, _ = _montar(crudos=[_uno()])
    _correr(correo.reporte_diario())
    aperturas = []
    correo._sin_leer_sync = lambda cuenta, dias, limite: (
        aperturas.append(cuenta["user"]) or [])
    _correr(correo.reporte_diario())
    assert aperturas == [], f"abrió el buzón con el candado cerrado: {aperturas}"


# ── Dos buzones, dos destinos: un reporte por CADA destinatario ──────────
#
# Lo encontró el verificador de este mismo arreglo. El candado de la primera
# versión era global por `origen`: bastaba con que ALGUIEN hubiera recibido su
# reporte hoy para que se cerrara para todos. Con dos cuentas que informan a
# chats distintos —cosa que `reporte_a` permite y `destino_del_reporte` ya
# implementa— la segunda se quedaba sin reporte hasta mañana, en silencio.
# Peor que el defecto original: aquel repetía de más, este pierde un destino.

OTRO_DESTINO = 999


def _montar_dos_destinos(hora=7, minuto=10, correo_de=None):
    """Dos cuentas que informan a chats DISTINTOS.

    `correo_de`: {user: [crudos]} — qué buzón tiene correo en este momento.
    """
    bandeja, reloj = _montar(hora=hora, minuto=minuto)
    config.CORREO_CUENTAS = [
        {"user": "tizianofv@gmail.com", "pass": "x"},              # → dueño (777)
        {"user": "rosi@x.com", "pass": "x", "reporte_a": OTRO_DESTINO},
    ]
    _servir_por_cuenta(correo_de or {})
    return bandeja, reloj


def _servir_por_cuenta(mapa):
    correo._sin_leer_sync = lambda cuenta, dias, limite: [
        dict(c, cuenta=cuenta["user"]) for c in mapa.get(cuenta["user"], [])]


def _destinos(bandeja):
    return sorted(f["chat_id"] for f in bandeja.encargos)


def test_el_segundo_destino_recibe_su_reporte_aunque_el_primero_ya_haya_salido():
    """EL caso del verificador, con sus horas y sus números.

    7:10 — solo el buzón del dueño tiene correo: sale su reporte al 777.
    9:00 — le llega correo a la cuenta de Rosi, que informa al 999.

    Con el candado global, la segunda llamada devolvía 0 y el 999 no recibía
    nada en todo el día. Cada destinatario tiene su propio candado.
    """
    bandeja, reloj = _montar_dos_destinos(
        correo_de={"tizianofv@gmail.com": [_uno(1, "cotización del disco")]})
    assert _correr(correo.reporte_diario()) == 1, "no salió el reporte de las 7:10"
    assert _destinos(bandeja) == [config.CHAT_ID_DUENO], (
        f"a las 7:10 el reporte tenía que ir solo al dueño: {_destinos(bandeja)}")

    reloj.ahora = datetime(2026, 9, 2, 9, 0, tzinfo=TZ)
    _servir_por_cuenta({"rosi@x.com": [_uno(2, "algo para Rosi")]})
    assert _correr(correo.reporte_diario()) == 1, (
        "el destino 999 se quedó sin su reporte: el candado de OTRO destinatario "
        "lo dejó fuera")
    assert _destinos(bandeja) == [config.CHAT_ID_DUENO, OTRO_DESTINO], (
        f"los encargos del día no llegaron a los dos destinos: {_destinos(bandeja)}")


def test_cada_destino_recibe_uno_solo_aunque_el_bucle_llame_cien_veces():
    """Lo que el candado por destinatario NO puede aflojar: el objetivo
    original. Cien vueltas, dos destinos, dos encargos."""
    bandeja, reloj = _montar_dos_destinos(correo_de={
        "tizianofv@gmail.com": [_uno(1, "para Tiziano")],
        "rosi@x.com": [_uno(2, "para Rosi")]})
    assert _correr(correo.reporte_diario()) == 2
    for _ in range(99):
        assert _correr(correo.reporte_diario()) == 0
    assert _destinos(bandeja) == [config.CHAT_ID_DUENO, OTRO_DESTINO], (
        f"{len(bandeja.encargos)} encargos en una mañana para dos destinos: "
        f"{_destinos(bandeja)}")


def test_el_destino_que_ya_reporto_no_vuelve_a_abrir_su_buzon():
    """El candado por destinatario tampoco puede volverse caro: la cuenta que ya
    informó no se vuelve a mirar por IMAP en las ~100 vueltas que quedan."""
    bandeja, reloj = _montar_dos_destinos(
        correo_de={"tizianofv@gmail.com": [_uno(1, "para Tiziano")]})
    assert _correr(correo.reporte_diario()) == 1
    reloj.ahora = datetime(2026, 9, 2, 9, 0, tzinfo=TZ)
    aperturas = []
    correo._sin_leer_sync = lambda cuenta, dias, limite: (
        aperturas.append(cuenta["user"]) or [])
    _correr(correo.reporte_diario())
    assert aperturas == ["rosi@x.com"], (
        f"buzones abiertos con el dueño ya reportado: {aperturas}")


def test_un_buzon_sin_destino_no_cuenta_como_destinatario():
    """`reporte_a: 0` = este buzón se lee para bancos y no informa a nadie. No
    puede quedar como un destino eternamente pendiente que impida el atajo."""
    bandeja, reloj = _montar_dos_destinos(
        correo_de={"tizianofv@gmail.com": [_uno(1, "para Tiziano")]})
    config.CORREO_CUENTAS = [{"user": "tizianofv@gmail.com", "pass": "x"},
                             {"user": "banco@x.com", "pass": "x", "reporte_a": 0}]
    assert _correr(correo.reporte_diario()) == 1
    aperturas = []
    correo._sin_leer_sync = lambda cuenta, dias, limite: (
        aperturas.append(cuenta["user"]) or [])
    assert _correr(correo.reporte_diario()) == 0
    assert aperturas == [], (
        f"con el único destinatario ya reportado abrió buzones igual: {aperturas}")


# ── Lo que no cambia ─────────────────────────────────────────────────────

def test_fuera_de_la_ventana_matinal_no_sale():
    """De 7 a 12, decisión de Tiziano. A las 4 PM el correo de la mañana ya no
    es un reporte matinal: espera al de mañana."""
    for hora in (6, 12, 16, 23):
        bandeja, _ = _montar(hora=hora, crudos=[_uno()])
        assert _correr(correo.reporte_diario()) == 0, f"salió a las {hora}"
        assert bandeja.encargos == [], f"dejó encargo a las {hora}"


def test_la_ventana_de_recuperacion_llega_hasta_el_mediodia():
    for hora in (7, 8, 9, 10, 11):
        bandeja, _ = _montar(hora=hora, crudos=[_uno()])
        assert _correr(correo.reporte_diario()) == 1, f"no salió a las {hora}"


def test_una_manana_sin_correo_no_deja_marca_y_el_primero_que_llegue_dispara():
    """Consecuencia de que la marca sea el encargo, escrita a propósito: si no
    hubo nada que reportar, no hay encargo y el reporte del día sigue
    pendiente. El primero que llegue antes del mediodía lo dispara, y a partir
    de ahí el candado ya está cerrado."""
    bandeja, reloj = _montar(crudos=[])
    assert _correr(correo.reporte_diario()) == 0
    assert bandeja.encargos == []
    reloj.ahora = datetime(2026, 9, 2, 9, 0, tzinfo=TZ)
    _servir([_uno(3, "el primero del día")])
    assert _correr(correo.reporte_diario()) == 1
    assert _correr(correo.reporte_diario()) == 0
    assert len(bandeja.encargos) == 1


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
