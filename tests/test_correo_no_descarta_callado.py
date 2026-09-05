# -*- coding: utf-8 -*-
"""En el camino del correo no se cae nada en silencio, y cada correo queda
atado al encargo que de verdad lo informó.

Dos defectos, medidos contra producción el 5-sep-2026, que eran el mismo:
correos que se pierden de vista.

1. EL TOPE DESCARTABA CALLADO. `_sin_leer_sync` cortaba en los 60 UIDs más
   nuevos, y lo hacía ANTES de que nadie pudiera descontar los que ya se habían
   informado. Con más de 60 sin leer en la ventana de 7 días, lo viejo
   desaparecía sin log y sin aviso; y si esos 60 más nuevos ya estaban
   informados, el reporte salía vacío teniendo correos pendientes debajo.
   Mordió: los encargos 171 y 172 (28-jul-2026) tienen 60 correos clavados cada
   uno — `SELECT bandeja_id, cuenta, count(*) FROM correo_reportado GROUP BY 1,2`
   devuelve 2 grupos de exactamente 60 y ninguno entre 31 y 59.
   La vigilancia 911 tenía el mismo corte en 30, que es el peor sitio posible
   para tenerlo: ahí lo que se cae es un "deploy failed".

2. UNA RELACIÓN DE VARIOS GUARDADA COMO SI FUERA DE UNA. `reporte_diario`
   creaba un encargo por destino pero se quedaba con UN solo `bandeja_id` —el
   del dueño, o el primero creado— y marcaba TODOS los correos con ése. El
   desempate lo hacía una regla en vez de un dato. No es cosmético:
   `correos_por_marcar_leidos` y `olvidar_reportados_fallidos` deciden por ese
   id, así que el reporte de una persona que sale bien marcaba como leídos
   —"ya te informé"— correos de otra cuyo reporte nunca llegó.

Estas pruebas manejan el `_sin_leer_sync` DE VERDAD contra un IMAP de mentira,
en vez de sustituirlo por un doble. Es a propósito: un tope que vuelva a
aparecer dentro de esa función tiene que ponerlas rojas, y un doble puesto en
su lugar no lo vería nunca. Lo que se compara sale del buzón falso —cuántos
correos tiene, cuáles— y no de un número escrito acá.

Correr:  python3 tests/test_correo_no_descarta_callado.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

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


# ── Un Gmail de mentira ───────────────────────────────────────────────────

def _eml(de: str, asunto: str, cuerpo: str = "el cuerpo del correo") -> bytes:
    return (f"From: {de}\r\n"
            f"Subject: {asunto}\r\n"
            f"Date: Fri, 05 Sep 2026 08:00:00 -0400\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{cuerpo}\r\n").encode()


class _IMAPFalso:
    """Responde SEARCH y FETCH como `imaplib.IMAP4_SSL`, sobre un buzón en
    memoria ordenado del más viejo al más nuevo — que es el orden en que Gmail
    devuelve los UIDs.

    Solo lee: no tiene STORE. Si algún día este camino intenta escribir un
    flag, revienta acá con AttributeError en vez de hacerlo en el buzón real.
    """

    ultimo: "_IMAPFalso | None" = None

    def __init__(self, buzon, rompe_al_traer_cuerpo=False):
        self.buzon = list(buzon)          # [(uid:int, crudo:bytes)]
        self.rompe_al_traer_cuerpo = rompe_al_traer_cuerpo
        self.pedidos: list[tuple] = []    # (uid, pieza) de cada FETCH
        _IMAPFalso.ultimo = self

    # imaplib
    def login(self, usuario, clave):
        return ("OK", [b"logueado"])

    def select(self, carpeta, readonly=False):
        assert readonly, "esta prueba no admite abrir el buzón para escribir"
        return ("OK", [str(len(self.buzon)).encode()])

    def uid(self, orden, *args):
        if orden == "search":
            return ("OK", [b" ".join(str(u).encode() for u, _ in self.buzon)])
        if orden == "fetch":
            uid, pieza = int(args[0]), args[1]
            self.pedidos.append((uid, pieza))
            if self.rompe_al_traer_cuerpo and "HEADER" not in pieza:
                raise OSError("Gmail cortó la conexión al bajar el cuerpo")
            for u, crudo in self.buzon:
                if u != uid:
                    continue
                if "HEADER" in pieza:
                    crudo = crudo.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                return ("OK", [(b"1 (UID x {n})", crudo)])
            return ("OK", [None])
        raise AssertionError(f"orden IMAP inesperada: {orden}")

    def logout(self):
        return ("BYE", [b"chao"])


class _BaseFalsa:
    """Lo justo de `db.db` para este camino. Guarda el `bandeja_id` con el que
    se marcó cada correo, que es el dato del segundo defecto."""

    def __init__(self, ya_reportados=()):
        self.encargos: list[dict] = []            # id implícito = índice + 1
        self.marcados: list[dict] = []
        self._ya = set(ya_reportados)

    async def guardar_en_bandeja(self, **kw):
        self.encargos.append(dict(kw))
        return len(self.encargos)

    async def marcar_correo_reportado(self, cuenta, uid, *, bandeja_id=None, **kw):
        self.marcados.append({"cuenta": cuenta, "uid": uid,
                              "bandeja_id": bandeja_id})
        self._ya.add((cuenta, uid))

    async def correos_ya_reportados(self, cuenta, uids):
        return {u for u in uids if (cuenta, u) in self._ya}

    async def listar_preferencias(self):
        return []

    async def destinos_con_encargo_hoy(self, origen, prefijo, desde):
        return {e["chat_id"] for e in self.encargos
                if e.get("origen") == origen
                and (e.get("contenido_raw") or "").startswith(prefijo)
                and e.get("chat_id") is not None}

    def encargo_de(self, chat_id):
        """El id del encargo que se le dejó a ese chat, o None."""
        for i, e in enumerate(self.encargos, start=1):
            if e.get("chat_id") == chat_id and e.get("origen") == "correo":
                return i
        return None


class _Reloj:
    def __init__(self, ahora):
        self.ahora = ahora

    def now(self, tz=None):
        return self.ahora if tz is None else self.ahora.astimezone(tz)


def _montar(buzon, ya_reportados=(), rompe_al_traer_cuerpo=False):
    """Pone el IMAP falso y la base falsa. Devuelve la base."""
    imap = types.SimpleNamespace(
        IMAP4_SSL=lambda servidor, puerto: _IMAPFalso(
            buzon, rompe_al_traer_cuerpo))
    # Se le escribe encima al atributo `imaplib` DE captura.correo, que es un
    # módulo de Lucy y el conftest lo devuelve a su sitio al terminar la
    # prueba. Tocar `imaplib` en sí sería escribirle encima a la biblioteca
    # estándar y dejarlo puesto para toda la sesión.
    correo.imaplib = imap
    base = _BaseFalsa(ya_reportados)
    for n in ("guardar_en_bandeja", "marcar_correo_reportado",
              "correos_ya_reportados", "listar_preferencias",
              "destinos_con_encargo_hoy"):
        setattr(db, n, getattr(base, n))
    correo.clasificar = lambda c, r="": _hecho(
        {"ambito": "laboral", "area": "cds_clientes", "nivel": "accion",
         "asunto_corto": c["subject"][:120], "motivo": ""})
    return base


async def _hecho(v):
    return v


def _correr(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


CUENTA = {"user": "tizianofv@gmail.com", "pass": "x"}


# ── 1. El tope ya no descarta ─────────────────────────────────────────────

def test_ningun_correo_sin_leer_se_queda_afuera_por_un_tope():
    """Todo lo que el buzón dice que está sin leer tiene que llegar al reporte.

    150 es a propósito más del doble del viejo tope de 60: con el tope puesto
    esto devolvía 60 y los otros 90 se perdían sin dejar rastro.
    """
    buzon = [(i, _eml(f"Persona {i} <p{i}@ejemplo.com>", f"asunto {i}"))
             for i in range(1, 151)]
    _montar(buzon)
    salida = _correr(correo._pendientes_de(CUENTA, ""))
    assert len(salida) == len(buzon), (
        f"el buzón tiene {len(buzon)} correos sin leer y al reporte llegaron "
        f"{len(salida)}: hay algo descartando en silencio")


def test_lo_viejo_sin_informar_no_se_pierde_detras_de_lo_nuevo_ya_informado():
    """EL defecto, exacto. El corte se hacía ANTES de descontar los informados.

    80 sin leer; los 60 más nuevos ya se informaron; los 20 más viejos no. Con
    el tope, `_sin_leer_sync` devolvía justo esos 60 ya informados, el filtro
    los tumbaba todos y el reporte salía vacío — con 20 correos pendientes
    debajo, que se iban a caer de la ventana de 7 días sin que nadie supiera
    que existieron.
    """
    buzon = [(i, _eml(f"Persona {i} <p{i}@ejemplo.com>", f"asunto {i}"))
             for i in range(1, 81)]
    viejos = [u for u, _ in buzon[:20]]
    ya = [(CUENTA["user"], u) for u, _ in buzon[20:]]
    _montar(buzon, ya_reportados=ya)
    salida = _correr(correo._pendientes_de(CUENTA, ""))
    assert sorted(c["uid"] for c in salida) == viejos, (
        f"esperaba los {len(viejos)} sin informar {viejos[:3]}…, "
        f"llegaron {sorted(c['uid'] for c in salida)[:5]}… "
        f"({len(salida)} correos)")


def test_la_vigilancia_911_no_pierde_la_alerta_detras_de_un_tope():
    """El corte en 30 de la vigilancia 911 dejaba fuera lo más viejo del día.

    El buzón trae 41 sin leer y la alerta de infraestructura es la MÁS VIEJA.
    Con el tope, los 30 más nuevos no la incluían y la caída de producción no
    se avisaba nunca — sin una línea de log que dijera que se había mirado a
    medias.
    """
    alerta = _eml("Railway <team@railway.app>", "Build failed for lucy")
    buzon = [(1, alerta)] + [
        (i, _eml(f"Persona {i} <p{i}@ejemplo.com>", f"asunto {i}"))
        for i in range(2, 42)]
    base = _montar(buzon)
    config.CORREO_CUENTAS = [CUENTA]
    avisados = _correr(correo.vigilar_911(None))
    assert avisados == 1, (
        f"la alerta de Railway estaba en el buzón y se avisó {avisados} vez/veces")
    assert base.marcados[0]["uid"] == 1
    texto = base.encargos[0]["contenido_raw"]
    assert "Build failed for lucy" in texto
    assert "el cuerpo del correo" in texto, (
        "el aviso salió sin el extracto: no se bajó el cuerpo del sospechoso")


def test_la_vigilancia_911_solo_baja_el_cuerpo_de_los_sospechosos():
    """La 911 corre cada pocos minutos las 24 horas. Mirarlo todo solo es
    sostenible si lo caro se pide únicamente para lo que hay que contar."""
    alerta = _eml("Railway <team@railway.app>", "Deploy failed")
    buzon = [(i, _eml(f"Persona {i} <p{i}@ejemplo.com>", f"asunto {i}"))
             for i in range(1, 41)] + [(41, alerta)]
    _montar(buzon)
    config.CORREO_CUENTAS = [CUENTA]
    _correr(correo.vigilar_911(None))
    cuerpos = [uid for uid, pieza in _IMAPFalso.ultimo.pedidos
               if "HEADER" not in pieza]
    assert cuerpos == [41], (
        f"bajó el cuerpo de {len(cuerpos)} correos y solo 1 era sospechoso: "
        f"{cuerpos[:5]}")


def test_la_911_avisa_aunque_no_consiga_bajar_el_cuerpo():
    """Un fetch que falla no puede convertir una alerta de infraestructura en
    silencio. Se avisa igual, sin extracto."""
    buzon = [(1, _eml("Railway <team@railway.app>", "Deploy failed"))]
    base = _montar(buzon, rompe_al_traer_cuerpo=True)
    config.CORREO_CUENTAS = [CUENTA]
    avisados = _correr(correo.vigilar_911(None))
    assert avisados == 1, (
        f"se avisó {avisados} veces: el fallo al bajar el cuerpo se comió la alerta")
    assert "Deploy failed" in base.encargos[0]["contenido_raw"]


# ── 2. Cada correo, atado a SU encargo ────────────────────────────────────

def test_cada_correo_queda_atado_al_encargo_de_su_propio_destino():
    """Con dos buzones que informan a chats distintos, el correo de cada uno
    tiene que colgar del encargo que fue a SU chat.

    Lo que se compara sale de la base falsa —qué encargo se le dejó a cada
    chat— y no de un id escrito acá: si mañana cambia el orden en que se crean
    los encargos, la prueba sigue diciendo lo mismo.
    """
    from datetime import datetime
    dueno, otro = config.CHAT_ID_DUENO, 999
    cuentas = [{"user": "tizianofv@gmail.com", "pass": "x"},
               {"user": "rosi@ejemplo.com", "pass": "x", "reporte_a": otro}]
    base = _montar([(1, _eml("Jorge <jorge@ejemplo.com>", "cotizacion del disco"))])
    config.CORREO_CUENTAS = cuentas
    correo.datetime = _Reloj(datetime(2026, 9, 2, 7, 10, tzinfo=config.TZ))
    config.es_horario_caro_deepseek = lambda ahora: False

    cuantos = _correr(correo.reporte_diario())
    assert cuantos == 2, f"esperaba 1 correo por buzón, entraron {cuantos}"
    assert len(base.encargos) == 2, (
        f"esperaba un encargo por destino, hay {len(base.encargos)}")

    por_cuenta = {c["user"]: correo.destino_del_reporte(c) for c in cuentas}
    for m in base.marcados:
        destino = por_cuenta[m["cuenta"]]
        assert m["bandeja_id"] == base.encargo_de(destino), (
            f"el correo de {m['cuenta']} (que informa al chat {destino}) quedó "
            f"colgando del encargo #{m['bandeja_id']}, y el suyo es el "
            f"#{base.encargo_de(destino)}")
    assert base.encargo_de(dueno) != base.encargo_de(otro)


# ── 3. Un asunto mal formado no puede callar un buzón entero ──────────────

def test_un_asunto_con_bytes_crudos_no_deja_el_buzon_entero_afuera():
    """Salió manejando el código real en la prueba de los dos destinos.

    "cotización" en el asunto sin codificar en MIME —bytes crudos, que es lo
    que manda cualquier remitente mal configurado— hace que `email` etiquete la
    cabecera como `unknown-8bit`. Python no tiene ese códec, así que `_texto`
    lanzaba `LookupError` desde dentro de `_sin_leer_sync`; arriba,
    `reporte_diario` lo atrapaba con un `log.warning` y seguía con la cuenta
    siguiente. Un solo asunto así dejaba el buzón COMPLETO fuera del reporte
    del día, en silencio.

    El buzón trae el correo torcido y dos sanos: tienen que llegar los tres.
    """
    buzon = [(1, _eml("Jorge <jorge@ejemplo.com>", "cotización del disco")),
             (2, _eml("Ana <ana@ejemplo.com>", "sin tildes")),
             (3, _eml("Luis <luis@ejemplo.com>", "reunión del jueves"))]
    _montar(buzon)
    salida = _correr(correo._pendientes_de(CUENTA, ""))
    assert len(salida) == len(buzon), (
        f"el buzón tiene {len(buzon)} correos y llegaron {len(salida)}: una "
        "cabecera mal formada se llevó el buzón por delante")
    asuntos = {c["uid"]: c["subject"] for c in salida}
    assert "cotizaci" in asuntos[1], (
        f"el asunto torcido llegó ilegible del todo: {asuntos[1]!r}")


def test_un_solo_destino_sigue_atando_todo_a_su_unico_encargo():
    """El caso de hoy en producción: un solo destino. No puede cambiar nada."""
    from datetime import datetime
    base = _montar([(1, _eml("Jorge <jorge@ejemplo.com>", "cotizacion del disco"))])
    config.CORREO_CUENTAS = [{"user": "tizianofv@gmail.com", "pass": "x"}]
    correo.datetime = _Reloj(datetime(2026, 9, 2, 7, 10, tzinfo=config.TZ))
    config.es_horario_caro_deepseek = lambda ahora: False

    assert _correr(correo.reporte_diario()) == 1
    assert len(base.encargos) == 1
    unico = base.encargo_de(config.CHAT_ID_DUENO)
    assert [m["bandeja_id"] for m in base.marcados] == [unico]


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
