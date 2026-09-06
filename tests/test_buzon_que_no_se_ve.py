# -*- coding: utf-8 -*-
"""Un buzón con `reporte_a: 0` no se le enseña a Tiziano por NINGÚN camino.

EL DEFECTO, medido el 5-sep-2026. `reporte_a` existe para que Lucy pueda leer
un buzón ajeno —sacarle los movimientos bancarios— sin contarle a Tiziano lo
que ahí le escriben a otra persona. `reporte_diario` respetaba el campo. Los
otros cuatro caminos que enseñan correo, no:

    · revisar_ahora()  — "revisá el correo", el pedido a mano
    · buscar()         — "¿me escribió Jorge?"
    · leer()           — abrir un correo suelto por cuenta + uid
    · vigilar_911()    — la alerta de infraestructura, con 400 caracteres
                         de cuerpo al chat del dueño

O sea: el reporte automático de las 7:00 respetaba el campo y todo lo que él
pedía a mano se lo saltaba. El buzón marcado tenía 34 correos sin leer en la
ventana el día que se midió.

POR QUÉ ESTAS PRUEBAS NO MIRAN CUATRO FILTROS. El arreglo no le puso un filtro
a cada camino: les quitó a todos la capacidad de conseguir un buzón sin decir
para qué lo quieren. `config.cuentas_de_correo(para=...)` es el único sitio de
los que SALEN DEL MÓDULO CONFIG, y `test_nadie_lee_la_lista_cruda` —que recorre
los .py que hay EN DISCO, no una lista escrita acá, y los lee con `ast.parse`
en vez de buscarles texto— se pone rojo si algún archivo puede alcanzar la
lista cruda por ahí, lo escriba como lo escriba. Un camino nuevo no puede
olvidarse de filtrar: no puede conseguir el buzón. El porqué de leer el árbol y
no el texto está entero arriba de la guarda, más abajo.

Y HASTA DÓNDE LLEGA ESTA GUARDA, dicho para que nadie la lea de más. Vigila el
camino que pasa por el módulo `config`, y dentro de ese camino tiene dos
fronteras declaradas y medidas, las dos escritas donde viven:

  · Los siete objetos de `_PELIGROSOS` —`getattr` y familia, `eval`, `exec`,
    `compile`, `__import__`— se reconocen POR IDENTIDAD DE OBJETO, así que
    ningún alias los esconde; pero conseguir uno de ellos sin nombrarlo, por
    ejemplo `builtins.__dict__["get" + "attr"]`, no se reconoce como tal. Ese
    camino tampoco llega solo a la lista: hace falta además el módulo `config`.
  · Un ayudante genérico `def leer(mod, nombre): return getattr(mod, nombre)`
    sale rojo aunque nunca toque la lista prohibida. Es el precio de «lo que no
    se puede clasificar es rojo», y hoy no lo paga nadie: cero sitios de Lucy lo
    escriben. Lo cuenta `test_el_ayudante_generico_de_getattr_es_rojo_y_cuanto_
    cuesta_hoy`, que se pone rojo el día que empiece a costar.

Y NO vigila el otro sitio del que hoy
salen buzones con credenciales: `tools/descubrir_bancos.py::_cuentas()` lee
`os.environ["CORREO_CUENTAS"]` —y, si no está, el `.env` de la raíz— sin tocar
config, y se queda con TODAS las cuentas, la marcada incluida. Está hecho a
propósito («Igual que config.py, pero sin importarlo: este script tiene que
correr sin el resto de las variables de Lucy») y es un script de mano, no un
camino del bot. Se deja dicho acá porque una guarda que promete más de lo que
cubre es peor que no tenerla; si eso tiene que cambiar, es decisión de Tiziano
y no de esta prueba.

Y LA OTRA MITAD, que tiene que seguir igual: BARRER NO ES MOSTRAR. El buzón
marcado se sigue leyendo entero para sacar sus movimientos bancarios. Si estas
pruebas pasaran dejando de barrerlo, el arreglo estaría mal.

Correr:  python3 tests/test_buzon_que_no_se_ve.py
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import configparser
import json
import os
import sys
import types
from collections.abc import Mapping
from pathlib import Path

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

import captura.consumos as consumos  # noqa: E402
import captura.correo as correo  # noqa: E402
import config  # noqa: E402
import db.db as db  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]

# Los dos buzones de todas las pruebas de abajo. El segundo es el caso de Rosi:
# se lee para bancos y su correspondencia no aparece en el briefing de nadie.
SE_VE = {"user": "tizianofv@gmail.com", "pass": "x"}
NO_SE_VE = {"user": "rosi@ejemplo.com", "pass": "x", "reporte_a": 0}


# ── Un Gmail de mentira, con DOS buzones distintos ────────────────────────

def _eml(de: str, asunto: str, cuerpo: str = "el cuerpo del correo") -> bytes:
    return (f"From: {de}\r\n"
            f"Subject: {asunto}\r\n"
            f"Date: Fri, 05 Sep 2026 08:00:00 -0400\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{cuerpo}\r\n").encode()


class _GmailFalso:
    """Un IMAP de mentira que sirve un buzón DISTINTO según quién se loguea.

    Es lo que hace que estas pruebas midan algo: con un solo buzón compartido,
    una fuga entre cuentas no se vería. Acá cada correo lleva el nombre de su
    dueño, así que un correo de Rosi que aparezca en la salida es una fuga
    señalada con el dedo.

    Anota además CADA login, que es lo que mide el barrido: si `cosechar` deja
    de entrar al buzón de Rosi, su usuario no está en `logins` y la prueba del
    barrido se pone roja.
    """

    buzones: dict[str, list] = {}
    logins: list[str] = []

    def __init__(self, *a, **k):
        self.user = None

    def login(self, usuario, clave):
        self.user = usuario
        _GmailFalso.logins.append(usuario)
        return ("OK", [b"logueado"])

    def select(self, carpeta, readonly=False):
        assert readonly, "esta prueba no admite abrir el buzón para escribir"
        return ("OK", [b"0"])

    def _buzon(self):
        return _GmailFalso.buzones.get(self.user, [])

    def uid(self, orden, *args):
        if orden == "search":
            return ("OK", [b" ".join(str(u).encode() for u, _ in self._buzon())])
        if orden == "fetch":
            uid, pieza = int(args[0]), args[1]
            for u, crudo in self._buzon():
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
    def __init__(self):
        self.encargos: list[dict] = []
        self.marcados: list[dict] = []

    async def guardar_en_bandeja(self, **kw):
        self.encargos.append(dict(kw))
        return len(self.encargos)

    async def marcar_correo_reportado(self, cuenta, uid, *, bandeja_id=None, **kw):
        self.marcados.append({"cuenta": cuenta, "uid": uid})

    async def correos_ya_reportados(self, cuenta, uids):
        return set()

    async def listar_preferencias(self):
        return []

    async def destinos_con_encargo_hoy(self, origen, prefijo, desde):
        return set()

    async def leer_estado_consumos(self, cuenta):
        return None

    async def guardar_estado_consumos(self, *a, **k):
        return None

    async def listar_cuentas_propias(self):
        return []

    async def categorias_aprendidas(self):
        return []


def _montar(buzones, cuentas=(SE_VE, NO_SE_VE)):
    """Pone el Gmail falso con un buzón por cuenta y la base falsa."""
    _GmailFalso.buzones = dict(buzones)
    _GmailFalso.logins = []
    falso = types.SimpleNamespace(IMAP4_SSL=_GmailFalso)
    # Se le escribe encima al atributo `imaplib` DE los módulos de Lucy, que el
    # conftest devuelve a su sitio al terminar; tocar `imaplib` en sí sería
    # escribirle encima a la biblioteca estándar para toda la sesión.
    correo.imaplib = falso
    consumos.imaplib = falso
    base = _BaseFalsa()
    for n in ("guardar_en_bandeja", "marcar_correo_reportado",
              "correos_ya_reportados", "listar_preferencias",
              "destinos_con_encargo_hoy", "leer_estado_consumos",
              "guardar_estado_consumos", "listar_cuentas_propias",
              "categorias_aprendidas"):
        setattr(db, n, getattr(base, n))

    def _clasificar(c, r=""):
        return _hecho({"ambito": "laboral", "area": "cds_clientes",
                       "nivel": "accion", "asunto_corto": c["subject"][:120],
                       "motivo": ""})

    correo.clasificar = _clasificar
    config.CORREO_CUENTAS = list(cuentas)
    return base


async def _hecho(v):
    return v


def _correr(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def _dos_buzones():
    """Un correo en cada buzón, cada uno con el nombre de su dueño encima."""
    return {
        SE_VE["user"]: [(1, _eml("Jorge <jorge@ejemplo.com>",
                                 "cotizacion para Tiziano"))],
        NO_SE_VE["user"]: [(1, _eml("Clinica <citas@clinica.com>",
                                    "resultado de Rosi"))],
    }


# ── 1. El defecto del encargo: el pedido a mano ───────────────────────────

def test_revisar_a_mano_no_ensena_el_buzon_que_no_se_ve():
    """EL defecto, exacto. `revisar_ahora` recorría TODAS las cuentas.

    Pedir "revisá el correo" le ponía delante los correos de un buzón que el
    reporte de las 7:00 sí escondía. Un mismo buzón no puede ser privado a las
    7:00 y público a las 15:00.
    """
    _montar(_dos_buzones())
    salida = _correr(correo.revisar_ahora())

    cuentas = sorted({c["cuenta"] for c in salida})
    assert cuentas == [SE_VE["user"]], (
        f"la revisión a mano devolvió correo de {cuentas}, y el único buzón "
        f"que se le puede enseñar es {SE_VE['user']}")
    asuntos = [c["subject"] for c in salida]
    assert "resultado de Rosi" not in asuntos, (
        f"se filtró el correo del buzón marcado: {asuntos}")
    # Y el control: no está pasando por devolver vacío.
    assert asuntos == ["cotizacion para Tiziano"], (
        f"el buzón que SÍ se ve tenía que llegar entero: {asuntos}")


def test_buscar_no_ensena_el_buzon_que_no_se_ve():
    """"¿Me escribió la clínica?" no puede contestarse con el buzón de Rosi."""
    _montar(_dos_buzones())
    salida = _correr(correo.buscar(texto="resultado"))

    cuentas = sorted({c["cuenta"] for c in salida})
    assert NO_SE_VE["user"] not in cuentas, (
        f"la búsqueda entró al buzón marcado: devolvió {salida}")


def test_buscar_sigue_encontrando_en_el_buzon_que_si_se_ve():
    """El control de la de arriba: el arreglo no puede apagar la búsqueda."""
    _montar(_dos_buzones())
    salida = _correr(correo.buscar(texto="cotizacion"))
    assert [c["cuenta"] for c in salida] == [SE_VE["user"]], (
        f"la búsqueda dejó de encontrar en el buzón normal: {salida}")


def test_leer_no_abre_un_correo_del_buzon_que_no_se_ve():
    """`leer` recibe la cuenta como TEXTO, de quien sea que la haya llamado.

    Antes se buscaba en la lista cruda, así que bastaba con nombrar el buzón
    para que devolviera el cuerpo entero. Ahora se busca en los que se pueden
    enseñar: el nombre no alcanza.
    """
    _montar(_dos_buzones())
    assert _correr(correo.leer(NO_SE_VE["user"], "1")) is None, (
        "nombrando el buzón marcado se consiguió el cuerpo del correo")
    # Control: por el buzón normal sí se lee, así que el None de arriba es el
    # filtro y no que `leer` esté rota.
    abierto = _correr(correo.leer(SE_VE["user"], "1"))
    assert abierto and abierto["asunto"] == "cotizacion para Tiziano", (
        f"leer dejó de funcionar en el buzón normal: {abierto}")


def test_la_911_no_avisa_desde_el_buzon_que_no_se_ve():
    """La alerta 911 manda remitente, asunto y 400 caracteres de cuerpo al chat
    del dueño. Eso es enseñar correo como cualquier otro camino."""
    alerta = _eml("Railway <team@railway.app>", "Deploy failed")
    base = _montar({SE_VE["user"]: [], NO_SE_VE["user"]: [(1, alerta)]})
    avisados = _correr(correo.vigilar_911(None))
    assert avisados == 0, (
        f"se avisaron {avisados} alertas desde el buzón marcado; el aviso "
        f"lleva el cuerpo del correo al chat del dueño: {base.encargos}")


def test_la_911_sigue_avisando_desde_el_buzon_que_si_se_ve():
    """El control de la de arriba: la vigilancia no se apagó entera."""
    alerta = _eml("Railway <team@railway.app>", "Deploy failed")
    base = _montar({SE_VE["user"]: [(1, alerta)], NO_SE_VE["user"]: []})
    avisados = _correr(correo.vigilar_911(None))
    assert avisados == 1, (
        f"la alerta estaba en el buzón normal y se avisó {avisados} vez/veces")
    assert "Deploy failed" in base.encargos[0]["contenido_raw"]


def test_el_reporte_diario_sigue_escondiendo_el_buzon_marcado():
    """Lo único que ya funcionaba. Un arreglo que lo rompa no es un arreglo."""
    from datetime import datetime
    base = _montar(_dos_buzones())
    correo.datetime = _Reloj(datetime(2026, 9, 2, 7, 10, tzinfo=config.TZ))
    config.es_horario_caro_deepseek = lambda ahora: False

    cuantos = _correr(correo.reporte_diario())
    assert cuantos == 1, (
        f"entraron {cuantos} correos al reporte y solo 1 buzón se informa")
    assert [m["cuenta"] for m in base.marcados] == [SE_VE["user"]]
    assert [e["chat_id"] for e in base.encargos] == [config.CHAT_ID_DUENO]


class _Reloj:
    def __init__(self, ahora):
        self.ahora = ahora

    def now(self, tz=None):
        return self.ahora if tz is None else self.ahora.astimezone(tz)


# ── 2. La otra mitad: barrer NO es mostrar ────────────────────────────────

def test_el_barrido_de_bancos_sigue_entrando_al_buzon_que_no_se_ve():
    """La mitad que NO cambia, y la razón de que `reporte_a` exista.

    El buzón marcado se lee entero para sacarle los movimientos bancarios. Lo
    que se mide es el LOGIN: si `consumos.revisar` deja de entrar a ese buzón,
    su usuario no aparece en `logins` y esto se pone rojo. No hace falta un
    correo de banco de verdad — hace falta que la puerta se abra.
    """
    _montar({SE_VE["user"]: [], NO_SE_VE["user"]: []})
    _correr(consumos.revisar())
    assert NO_SE_VE["user"] in _GmailFalso.logins, (
        f"la ingesta bancaria dejó de entrar al buzón marcado: entró a "
        f"{_GmailFalso.logins}. Barrerlo es justo para lo que existe el campo")
    assert SE_VE["user"] in _GmailFalso.logins, (
        f"la ingesta dejó de entrar al buzón normal: {_GmailFalso.logins}")


def test_barrer_devuelve_todos_los_buzones_y_mostrar_solo_los_visibles():
    """Las dos vistas, sobre la misma lista cruda."""
    config.CORREO_CUENTAS = [SE_VE, NO_SE_VE]
    assert [c["user"] for c in config.cuentas_de_correo("barrer")] == [
        SE_VE["user"], NO_SE_VE["user"]]
    assert [c["user"] for c in config.cuentas_de_correo("mostrar")] == [
        SE_VE["user"]]


def test_marcar_leidos_usa_la_lista_entera_y_no_deja_correos_colgados():
    """`confirmar_leidos` escribe \\Seen sobre lo que YA se informó: no enseña
    nada. Va por "barrer" a propósito.

    Si fuera por "mostrar", ponerle `reporte_a: 0` a un buzón dejaría sus
    correos ya reportados sin marcar para siempre — pendientes de una
    confirmación que nunca puede llegar.
    """
    import inspect
    fuente = inspect.getsource(correo.confirmar_leidos)
    assert 'cuentas_de_correo("barrer")' in fuente, (
        "confirmar_leidos dejó de usar la lista entera: los correos ya "
        "informados de un buzón que se apague se quedan sin marcar leídos")


# ── 3. La puerta: vocabulario cerrado y nadie la esquiva ──────────────────

def test_para_es_obligatorio_y_su_vocabulario_es_cerrado():
    """Sin valor por defecto, a propósito.

    Es lo que sostiene todo el arreglo: si `cuentas_de_correo()` eligiera
    "todos" cuando no le dicen nada, el camino nuevo que se olvide de pensar
    heredaría la fuga sin que nadie la escriba. Que reviente obliga a elegir.
    """
    fallo = False
    try:
        config.cuentas_de_correo()          # type: ignore[call-arg]
    except TypeError:
        fallo = True
    assert fallo, "cuentas_de_correo() aceptó que no le dijeran para qué"

    for malo in ("todos", "", "leer", None, True):
        reventado = False
        try:
            config.cuentas_de_correo(malo)  # type: ignore[arg-type]
        except ValueError:
            reventado = True
        assert reventado, (
            f"cuentas_de_correo({malo!r}) devolvió buzones en vez de reventar")


# ── LA GUARDA: se lee el ÁRBOL DE SINTAXIS, nunca el texto ────────────────
#
# POR QUÉ NO SE BUSCA TEXTO. La primera versión de esta guarda recorría los .py
# del disco —eso estaba bien— pero decidía con `"config.CORREO_CUENTAS" in ln`.
# Medido el 5-sep-2026 con tres archivos nuevos que hacían LO MISMO
# (`return [c for c in <la lista cruda>]`), en sintaxis normal de Python:
#
#     config.CORREO_CUENTAS                          →  1 failed   la agarra
#     from config import CORREO_CUENTAS as CUENTAS   → 13 passed   se le escapa
#     getattr(config, "CORREO_" + "CUENTAS")         → 13 passed   se le escapa
#
# Dos de cada tres. Y el arreglo NO puede ser añadirle esas dos formas al
# patrón: mañana llega por `vars(config)`, por `config.__dict__`, por
# `import config as cfg`, por `importlib`, o por un nombre que arma una
# función. Una guarda que enumera formas de escribir algo siempre tiene una
# forma más que no vio, y mientras tanto da tranquilidad falsa.
#
# LO MISMO PASÓ CON LA SEGUNDA VERSIÓN, y por la misma razón. La versión sobre
# `ast` arregló el texto pero dejó DOS listas tecleadas a mano adentro, y las
# dos tenían un agujero, medido el 5-sep-2026:
#
#   · `_FABRICAS_DE_MODULOS = {"importlib", "__import__", "eval", ...}` no
#     incluía `sys`. Un archivo con `getattr(sys, "modules")["config"]` daba
#     16 passed, cero infracciones, y devolvía las credenciales del buzón
#     marcado.
#   · La exención decía `rel.name in ("config.py", "conftest.py")`, o sea POR
#     NOMBRE DE ARCHIVO. El mismo código, byte por byte, daba
#     `captura/_utilidades.py → 1 failed` y `captura/conftest.py → 16 passed`.
#     Bastaba con ponerle a un archivo de producción el nombre `conftest.py`.
#
# O sea: el criterio bueno («lo que no sé cuenta como rojo») estaba aplicado a
# UN tramo del camino, y los otros dos tramos —de dónde sale un módulo, y quién
# queda fuera de la vigilancia— seguían decidiéndose con una lista escrita a
# mano. Blindar un tramo y dejar los demás da la sensación de haberlo resuelto.
#
# Y PASÓ UNA TERCERA VEZ, medido el 6-sep-2026, con la misma especie otra vez:
# reconocer una cosa por el NOMBRE ESCRITO en vez de resolver qué es. Quedaban
# dos sitios comparando el texto de un identificador contra `"getattr"` —el
# motivo (c) y `es_espacio`—, así que UNA línea los apagaba los dos a la vez:
#
#     ga = getattr
#     def cuentas():
#         import sys
#         return ga(ga(sys, "mod" + "ules")["config"], "CORREO_" + "CUENTAS")
#
# Puesto DENTRO de `captura/consumos.py` —un archivo que ya existía, para que el
# rojo no pudiera venir de haber agregado uno— la suite daba 25 passed, y
# ejecutado con un valor centinela la función devolvía la lista cruda entera,
# con el buzón `reporte_a: 0` y sus credenciales. `from builtins import getattr
# as ga` hacía lo mismo.
#
# Y con una segunda forma, de otra familia: `eval("config.CORREO_CUENTAS")`
# dejaba la guarda en verde, igual que `exec`. Ésa no tiene arreglo por el lado
# de mirar el texto —el código que va a correr no existe hasta que corre— y por
# eso se le puso un fondo en vez de perseguirla. Ver `_PELIGROSOS`.
#
# DE DÓNDE SACA AHORA LO QUE COMPARA. De cuatro sitios, los cuatro reales, y
# ninguno tecleado:
#
#   1. `ast.parse` del archivo. El árbol ve igual `from config import
#      CORREO_CUENTAS as CUENTAS` que `config.CORREO_CUENTAS`, porque el nombre
#      está en el nodo y no en cómo se escribió. Los alias, los espacios, los
#      paréntesis y los comentarios desaparecen antes de que se compare nada.
#   2. `vars(config)` y `config.__name__`. Los atributos PERMITIDOS son los
#      nombres públicos que el módulo config de verdad tiene hoy, menos el
#      prohibido; y el nombre del módulo sale del módulo. Nadie los teclea acá:
#      si config gana un nombre, entra solo; si pierde el prohibido, la guarda
#      revienta en vez de quedarse verde vigilando un fantasma.
#   3. `sys.modules`, para preguntarle a los objetos DE VERDAD si son un
#      espacio del que puede salir un módulo. `sys.modules` es peligroso porque
#      hoy tiene módulos dentro, no porque alguien escribiera su nombre en una
#      lista; y `os.environ` no lo es porque hoy solo tiene strings. Eso lo
#      contesta el objeto, no la guarda. No se importa nada nuevo: solo se mira
#      lo que ya está cargado, así que no hay efectos de import.
#   4. `pytest.ini`, `railway.json` y `config.__file__`, para saber quién queda
#      fuera de la vigilancia. Un archivo es andamio de pruebas por DÓNDE VIVE
#      y por QUIÉN LO CARGA, nunca por cómo se llama.
#   5. `builtins`, para saber a QUÉ FUNCIÓN llama una llamada. No se compara el
#      texto del identificador: se resuelve el nombre al objeto que hay hoy en
#      memoria y se pregunta si ES el objeto `getattr` —o `eval`, o `exec`—.
#      `ga` resuelve al mismo objeto que `getattr`, así que da igual el nombre.
#      Ver `llama_a`, y ver `_PELIGROSOS` para el fondo que cubre los alias que
#      no se pueden seguir hasta su asignación.
#
# Y EL CRITERIO ES «LO QUE NO SÉ CUENTA COMO ROJO», aplicado al trayecto entero
# y no a un tramo:
#
#   · El módulo `config` solo se puede usar para UNA cosa: leer uno de sus
#     atributos permitidos, escrito como atributo o pedido por `getattr` con un
#     literal. Cualquier otro uso del objeto módulo —pasarlo, guardarlo,
#     `vars`-earlo, abrirle el `__dict__`— no se puede clasificar, y lo que no
#     se puede clasificar es rojo.
#   · De un espacio del que puede salir un módulo solo se puede sacar un nombre
#     ESCRITO. Si la guarda no puede enumerar exactamente qué nombres se piden,
#     es rojo. Por eso `getattr(sys, "modules")["config"]` cae sin que nadie
#     haya tenido que apuntar `sys` en ningún sitio.
#   · Un `getattr` con un nombre de atributo que la guarda no puede enumerar es
#     rojo sobre CUALQUIER cosa, sea o no un módulo: ahí no hay nada que
#     clasificar. Y CUÁL llamada es un `getattr` se decide resolviendo la
#     función al objeto, no leyendo cómo se llama.
#   · Convertir un texto en código —`eval`, `exec`, `compile`, `__import__`— es
#     rojo por sí solo, sin mirar qué lleva el texto dentro. No es que la guarda
#     no quiera mirar: es que ahí no hay nada que mirar hasta que corre.
#   · Un archivo que no se pueda clasificar como andamio de pruebas queda
#     DENTRO de la vigilancia, nunca fuera. Y si no se puede determinar con qué
#     arranca producción, no se exenta a nadie.
#
# Por eso una forma que nadie previó cae del lado rojo: no hay que reconocerla,
# hay que fallar en reconocerla.

_PROHIBIDO = "CORREO_CUENTAS"

# El nombre con el que config vive en la tabla de módulos, sacado del módulo y
# no tecleado: si mañana se renombra, esto lo sigue.
_MODULO_CONFIG = config.__name__


def _atributos_que_config_ofrece() -> set[str]:
    """Los nombres públicos que `config` DE VERDAD tiene, menos el prohibido."""
    publicos = {n for n in vars(config) if not n.startswith("_")}
    assert _PROHIBIDO in publicos, (
        f"config ya no define {_PROHIBIDO}. Esta guarda quedaría vigilando un "
        "nombre que no existe, o sea verde sin haber mirado nada: si la lista "
        "cruda cambió de nombre, hay que cambiárselo también acá")
    return publicos - {_PROHIBIDO}


# ── Tramo 1: qué nombres puede pedir una expresión ────────────────────────
#
# Enumerar los strings que una expresión puede valer es lo que convierte
# `getattr(config, "CORREO_" + "CUENTAS")` en el mismo nodo que
# `config.CORREO_CUENTAS`. Y es también lo que deja pasar sin ruido el
# `getattr(psycopg, n, None)` de `db/db.py`, donde `n` recorre una tupla de dos
# literales: ahí sí se sabe qué se pide. `None` no significa "ningún string":
# significa "no lo sé", que es la condición que dispara el rojo.
#
# Los nombres se miran POR ÁMBITO, no de corrido. Un archivo de 1.500 líneas
# usa `n` en veinte funciones distintas; si se mezclaran todas, el `n` de
# `db/db.py` valdría "no se sabe" por culpa de otra función que no tiene nada
# que ver, y la guarda rojearía un uso legítimo.

class _Ambito(dict):
    """Los nombres de UN ámbito, encadenado al de afuera."""

    def __init__(self, padre=None):
        super().__init__()
        self.padre = padre

    def buscar(self, nombre, defecto=None):
        amb = self
        while amb is not None:
            if dict.__contains__(amb, nombre):
                return dict.__getitem__(amb, nombre)
            amb = amb.padre
        return defecto


_ABREN_AMBITO = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                 ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp,
                 ast.GeneratorExp)


def _cadenas(nodo, ambitos: dict) -> frozenset | None:
    """Los strings que esta expresión puede valer. None = no se sabe."""
    if isinstance(nodo, ast.Constant):
        return frozenset({nodo.value}) if isinstance(nodo.value, str) \
            else frozenset()
    if isinstance(nodo, ast.Name):
        amb = ambitos.get(id(nodo))
        return amb.buscar(nodo.id) if amb is not None else None
    if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
        izq = _cadenas(nodo.left, ambitos)
        der = _cadenas(nodo.right, ambitos)
        if izq is None or der is None:
            return None
        return frozenset(a + b for a in izq for b in der)
    if isinstance(nodo, (ast.Tuple, ast.List, ast.Set)):
        partes = [_cadenas(e, ambitos) for e in nodo.elts]
        if any(p is None for p in partes):
            return None
        return frozenset().union(*partes) if partes else frozenset()
    if isinstance(nodo, ast.JoinedStr):
        partes = [_cadenas(v, ambitos) for v in nodo.values]
        if any(p is None or len(p) != 1 for p in partes):
            return None
        return frozenset({"".join(next(iter(p)) for p in partes)})
    if isinstance(nodo, ast.Starred):
        return _cadenas(nodo.value, ambitos)
    if isinstance(nodo, ast.Slice):
        # `x[:32]` corta, no pide un nombre. No hay ningún string en juego.
        return frozenset()
    return None


def _repartir_ambitos(arbol) -> dict:
    """A qué ámbito pertenece cada nodo del árbol."""
    ambitos: dict = {}

    def visitar(nodo, amb):
        ambitos[id(nodo)] = amb
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo, _Ambito(amb) if isinstance(hijo, _ABREN_AMBITO)
                    else amb)

    visitar(arbol, _Ambito())
    return ambitos


def _atar_cadenas(arbol, ambitos: dict) -> None:
    """Ata cada nombre a los strings que puede valer, ámbito por ámbito.

    Se repite hasta que deje de crecer, para que el orden en que aparecen las
    asignaciones en el archivo no cambie el resultado.
    """
    def atar(destino, valor, amb):
        if not isinstance(destino, ast.Name):
            for hijo in ast.walk(destino):
                if isinstance(hijo, ast.Name):
                    amb[hijo.id] = None
            return
        anterior = amb.get(destino.id, "sin atar")   # solo ESTE ámbito
        if anterior is None:
            return
        if valor is None:
            amb[destino.id] = None
        elif anterior == "sin atar":
            amb[destino.id] = valor
        else:
            amb[destino.id] = anterior | valor

    for _ in range(4):
        for n in ast.walk(arbol):
            amb = ambitos[id(n)]
            if isinstance(n, ast.Assign):
                for d in n.targets:
                    atar(d, _cadenas(n.value, ambitos), amb)
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                atar(n.target, _cadenas(n.value, ambitos), amb)
            elif isinstance(n, ast.AugAssign):
                atar(n.target, None, amb)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                atar(n.target, _cadenas(n.iter, ambitos), amb)
            elif isinstance(n, ast.comprehension):
                atar(n.target, _cadenas(n.iter, ambitos), amb)
            elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                atar(n.optional_vars, None, amb)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda)):
                for a in (n.args.posonlyargs + n.args.args + n.args.kwonlyargs
                          + ([n.args.vararg] if n.args.vararg else [])
                          + ([n.args.kwarg] if n.args.kwarg else [])):
                    amb[a.arg] = None
            elif isinstance(n, ast.ExceptHandler) and n.name:
                amb[n.name] = None


# ── Tramo 2: de dónde sale un objeto módulo ───────────────────────────────
#
# Antes había una lista de "fábricas de módulos" tecleada a mano y le faltaba
# `sys`; con `getattr(sys, "modules")["config"]` se sacaban las credenciales
# del buzón marcado con la guarda en verde. Ahora la pregunta va al revés.
#
# Un objeto módulo entra a un archivo por UN solo camino clasificable: un
# `import`. De ahí en adelante:
#
#   · Lo que se saca de dentro de un módulo por un nombre ESCRITO se resuelve
#     al objeto que hay hoy en memoria, y se le pregunta a ÉL si es un espacio
#     del que puede salir otro módulo. `sys.modules` lo es porque está lleno de
#     módulos; `os.environ` no lo es porque está lleno de strings; `config.TZ`
#     tampoco. Eso lo contesta el objeto, no una lista.
#   · Lo que no se pudo resolver cuenta como que SÍ lo es.
#   · De un espacio así solo se puede pedir un nombre que la guarda pueda
#     enumerar. Si no puede, es rojo — y por eso `sys` no hace falta apuntarlo
#     en ningún sitio.

def _es_espacio_de_modulos(obj) -> bool:
    """¿De este objeto se puede sacar un MÓDULO pidiéndole un nombre?"""
    if isinstance(obj, types.ModuleType):
        return True
    if isinstance(obj, Mapping):
        try:
            valores = list(obj.values())
        except Exception:
            return True                       # no se pudo mirar → rojo
        if any(isinstance(v, types.ModuleType) for v in valores):
            return True
        # El `__dict__` de un módulo es su espacio de nombres, aunque hoy no
        # tenga ningún módulo adentro.
        for m in list(sys.modules.values()):
            if m is not None and getattr(m, "__dict__", None) is obj:
                return True
    return False


def _objeto_ya_cargado(punteado: str):
    """`(encontrado, objeto)` para un camino punteado, sin importar nada nuevo.

    Solo mira lo que ya está en `sys.modules`: una guarda que importa módulos
    para decidir dispara los efectos de import de código que no le toca correr.
    """
    partes = punteado.split(".")
    for corte in range(len(partes), 0, -1):
        base = sys.modules.get(".".join(partes[:corte]))
        if base is None:
            continue
        obj = base
        for p in partes[corte:]:
            try:
                obj = getattr(obj, p)
            except Exception:
                return False, None
        return True, obj
    return False, None


def _punteado(nodo) -> str | None:
    """`sys.modules` → "sys.modules"; cualquier otra forma → None."""
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        base = _punteado(nodo.value)
        return f"{base}.{nodo.attr}" if base else None
    return None


def _nombre_llamado(nodo) -> str | None:
    """El nombre al que se llama, si se llama a un nombre suelto."""
    return nodo.func.id if isinstance(nodo, ast.Call) and \
        isinstance(nodo.func, ast.Name) else None


# ── Tramo 2-bis: a QUÉ FUNCIÓN se llama, resuelta al objeto ───────────────
#
# EL TERCER AGUJERO DE ESTE ARCHIVO, medido el 6-sep-2026, y de la misma
# especie que los dos anteriores: reconocer una cosa por el NOMBRE ESCRITO en
# vez de resolver qué es. `getattr` se reconocía comparando el texto del
# identificador, en dos sitios a la vez, así que una sola línea lo apagaba
# todo:
#
#     ga = getattr
#     def _fuga():
#         import sys
#         modulo = ga(sys, "mod" + "ules")["config"]
#         return ga(modulo, "CORREO_" + "CUENTAS")
#
# Puesto DENTRO de `captura/consumos.py` —un archivo que ya existía, para que
# el rojo no pudiera venir del conteo de archivos— la suite daba 25 passed. El
# mismo código con `getattr` escrito daba 2 failed. Ejecutado con un valor
# centinela, la función devolvía la lista cruda entera, con el buzón
# `reporte_a: 0` y sus credenciales dentro. `from builtins import getattr as
# ga` hacía lo mismo, y `builtins.getattr(...)` también, porque un `func` que
# es `ast.Attribute` no tiene "nombre suelto" que comparar.
#
# EL ARREGLO ES EL MISMO QUE YA SE HIZO CON `config`: no se pregunta cómo se
# llama el identificador, se pregunta A QUÉ OBJETO RESUELVE HOY EN MEMORIA.
# `ga` resuelve al mismo objeto que `getattr`, y por eso un alias que nadie
# previó cae igual: no hay que reconocer el nombre, hay que resolver el objeto.

_DESCONOCIDO = object()          # «no se pudo resolver», que no es «no es»

# Las tres funciones que piden un atributo POR SU NOMBRE en tiempo de ejecución.
# Son OBJETOS, no textos: da igual con qué identificador se llamen. Se pueden
# usar, llamándolas, y entonces valen las reglas (c) y (f).
_ATRIBUTO_POR_NOMBRE = (builtins.getattr, builtins.setattr, builtins.delattr)

# Y las cuatro que convierten un TEXTO en código o en un módulo. Éstas no se
# pueden usar de ninguna forma. Ver `_infracciones`, motivo (g).
_TEXTO_A_CODIGO = (builtins.eval, builtins.exec, builtins.compile,
                   builtins.__import__)

# EL FONDO, dicho en una línea: en un archivo vigilado, estos siete objetos SOLO
# pueden aparecer siendo llamados —y los de `_TEXTO_A_CODIGO` ni eso—. Nombrar
# uno sin llamarlo es rojo, se le llame como se le llame.
#
# POR QUÉ HACE FALTA UN FONDO Y NO OTRA RONDA DE PARCHES. `ga = getattr` apagaba
# las dos comprobaciones de `getattr` a la vez porque las dos comparaban el
# TEXTO del identificador. Resolver el nombre al objeto (ver `llama_a`) arregla
# los alias que se pueden seguir, pero el espacio de formas de ponerle otro
# nombre a una función NO TIENE FONDO: una tupla, un diccionario, una clausura,
# un `from builtins import getattr as ga`, el valor por defecto de un parámetro.
# Perseguirlas una por una es la carrera que la sala ya perdió tres veces en
# este mismo archivo. Así que la regla no persigue formas: dice DÓNDE puede
# aparecer el objeto, y todo lo demás cae fuera por consecuencia.
#
# LA FRONTERA, medida el 6-sep-2026 sobre los 37 archivos vigilados de este
# repo: 0 menciones fuera de una llamada, y 0 llamadas a `eval`, `exec`,
# `compile` o `__import__`. O sea: el fondo cuesta CERO hoy.
#
# Y LO QUE QUEDA FUERA, dicho para que nadie lea esto de más: la lista es de
# objetos concretos, así que un camino que consiga el objeto `getattr` sin
# nombrarlo —`builtins.__dict__["get" + "attr"]`— no se reconoce como tal. Ese
# camino no llega solo a la lista cruda: para sacarla hay que además conseguir
# el módulo `config`, y eso cae por los motivos (d), (e) y (f).
_PELIGROSOS = _ATRIBUTO_POR_NOMBRE + _TEXTO_A_CODIGO


def _cual_peligroso(objetos) -> object | None:
    """El objeto peligroso que hay en este conjunto, comparado por IDENTIDAD."""
    for f in _PELIGROSOS:
        if any(o is f for o in objetos):
            return f
    return None


def _meter(conj: set, obj) -> None:
    """Mete un objeto en el conjunto; si no se puede ni guardar, no lo sé."""
    try:
        conj.add(obj)
    except TypeError:                        # inhashable → no se puede seguir
        conj.add(_DESCONOCIDO)


class _Contexto:
    """Lo que la guarda sabe de los nombres de UN archivo."""

    def __init__(self, arbol):
        self.ambitos = _repartir_ambitos(arbol)
        _atar_cadenas(arbol, self.ambitos)
        self.importados: dict[str, str] = {}   # nombre local → camino punteado
        self.atados: set[str] = set()          # todo nombre que el archivo ata
        self.locales_config: set[str] = set()  # nombres que SON el módulo
        self.derivados: set[str] = set()       # nombres atados desde un espacio
        self.objetos: dict[str, set] = {}      # nombre local → objetos de hoy
        self._leer_ataduras(arbol)
        self._propagar(arbol)
        self._resolver_nombres(arbol)

    def _leer_ataduras(self, arbol) -> None:
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                for a in n.names:
                    local = a.asname or a.name.split(".")[0]
                    self.importados[local] = a.name if a.asname else \
                        a.name.split(".")[0]
                    self.atados.add(local)
                    # `import config` e `import config.algo` atan el módulo.
                    if a.name == _MODULO_CONFIG or \
                            a.name.startswith(_MODULO_CONFIG + "."):
                        self.locales_config.add(local)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "*":
                        continue
                    local = a.asname or a.name
                    self.importados[local] = f"{n.module}.{a.name}" \
                        if n.module else a.name
                    self.atados.add(local)
                    # `from paquete import config` ata el módulo; `from config
                    # import TZ` ata un atributo suyo, que no es lo mismo.
                    if a.name == _MODULO_CONFIG:
                        self.locales_config.add(local)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.atados.add(n.name)
                for a in (n.args.posonlyargs + n.args.args + n.args.kwonlyargs
                          + ([n.args.vararg] if n.args.vararg else [])
                          + ([n.args.kwarg] if n.args.kwarg else [])):
                    self.atados.add(a.arg)
            elif isinstance(n, ast.Lambda):
                for a in (n.args.posonlyargs + n.args.args + n.args.kwonlyargs
                          + ([n.args.vararg] if n.args.vararg else [])
                          + ([n.args.kwarg] if n.args.kwarg else [])):
                    self.atados.add(a.arg)
            elif isinstance(n, ast.ClassDef):
                self.atados.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store,
                                                                ast.Del)):
                self.atados.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                self.atados.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                self.atados.update(n.names)

    def _propagar(self, arbol) -> None:
        """Un nombre atado desde un espacio de módulos también lo es.

        Se repite hasta que deje de crecer, para seguir cadenas como
        `a = sys.modules`, `b = a`, `c = b`.
        """
        for _ in range(10):
            antes = len(self.derivados)
            for n in ast.walk(arbol):
                destino = valor = None
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    destino, valor = n.targets[0], n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    destino, valor = n.target, n.value
                elif isinstance(n, (ast.For, ast.AsyncFor)):
                    destino, valor = n.target, n.iter
                if isinstance(destino, ast.Name) and valor is not None \
                        and self.es_espacio(valor):
                    self.derivados.add(destino.id)
            if len(self.derivados) == antes:
                return

    # ── A qué objeto resuelve un nombre ──────────────────────────────────

    def _resolver_nombres(self, arbol) -> None:
        """Ata cada nombre a los objetos que puede valer HOY.

        Solo asignaciones simples (`ga = getattr`, `f = ga`, `h = builtins.exec`)
        y se repite hasta que deje de crecer, para que el orden de las líneas no
        cambie el resultado. Se UNEN todas las asignaciones de un mismo nombre:
        si alguna de ellas es `getattr`, el nombre cuenta como `getattr`. Eso es
        el lado seguro — `ga = getattr` seguido de `ga = otra_cosa` sigue rojo.
        """
        for _ in range(4):
            antes = {k: set(v) for k, v in self.objetos.items()}
            for n in ast.walk(arbol):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                        isinstance(n.targets[0], ast.Name):
                    self.objetos.setdefault(n.targets[0].id, set()).update(
                        self.resuelve(n.value))
            if self.objetos == antes:
                return

    def resuelve(self, nodo) -> set:
        """Los objetos a los que esta expresión puede resolver HOY en memoria.

        Un `_DESCONOCIDO` dentro del conjunto significa que alguna rama no se
        pudo resolver. Nunca importa nada nuevo: mira `sys.modules` (a través de
        `_objeto_ya_cargado`) y `builtins`, y nada más.
        """
        if isinstance(nodo, ast.Name):
            if nodo.id in self.objetos:
                return set(self.objetos[nodo.id])
            if nodo.id in self.importados:
                hay, obj = _objeto_ya_cargado(self.importados[nodo.id])
                salida: set = set()
                _meter(salida, obj if hay else _DESCONOCIDO)
                return salida
            # Un nombre que este archivo no ata y que ES un builtin resuelve al
            # builtin. Si el archivo lo ata (un `def getattr` propio, un
            # parámetro), ya no se sabe qué es.
            if nodo.id not in self.atados and hasattr(builtins, nodo.id):
                salida = set()
                _meter(salida, getattr(builtins, nodo.id))
                return salida
            return {_DESCONOCIDO}
        if isinstance(nodo, ast.Attribute):
            punteado = _punteado(nodo)
            if punteado:
                raiz = punteado.split(".")[0]
                if raiz in self.importados:
                    real = self.importados[raiz] + punteado[len(raiz):]
                    hay, obj = _objeto_ya_cargado(real)
                    if hay:
                        salida = set()
                        _meter(salida, obj)
                        return salida
            salida = set()
            for base in self.resuelve(nodo.value):
                if base is _DESCONOCIDO:
                    salida.add(_DESCONOCIDO)
                    continue
                try:
                    _meter(salida, getattr(base, nodo.attr))
                except Exception:
                    salida.add(_DESCONOCIDO)
            return salida or {_DESCONOCIDO}
        return {_DESCONOCIDO}

    def llama_a(self, nodo, funciones: tuple):
        """La función de `funciones` a la que llama este `ast.Call`, o None.

        Compara por IDENTIDAD DE OBJETO, no por nombre: `getattr(...)`,
        `ga(...)` con `ga = getattr`, `builtins.getattr(...)` y
        `from builtins import getattr as ga` dan todos el mismo veredicto.
        """
        if not isinstance(nodo, ast.Call):
            return None
        posibles = self.resuelve(nodo.func)
        for f in funciones:
            if any(o is f for o in posibles):
                return f
        return None

    def es_espacio(self, nodo) -> bool:
        """¿De esta expresión puede salir un módulo si se le pide un nombre?"""
        if isinstance(nodo, ast.Name):
            if nodo.id in self.derivados or nodo.id in self.locales_config:
                return True
            if nodo.id in self.importados:
                hay, obj = _objeto_ya_cargado(self.importados[nodo.id])
                return _es_espacio_de_modulos(obj) if hay else True
            # Un nombre que este archivo nunca ata y que tampoco es un builtin
            # viene de fuera y no se puede clasificar.
            return nodo.id not in self.atados and not hasattr(builtins, nodo.id)
        if isinstance(nodo, ast.Attribute):
            if not self.es_espacio(nodo.value):
                return False
            punteado = _punteado(nodo)
            if punteado:
                raiz = punteado.split(".")[0]
                if raiz in self.importados:
                    real = self.importados[raiz] + punteado[len(raiz):]
                    hay, obj = _objeto_ya_cargado(real)
                    if hay:
                        return _es_espacio_de_modulos(obj)
            return True                      # no se pudo resolver → rojo
        if isinstance(nodo, ast.Subscript):
            return self.es_espacio(nodo.value)
        if isinstance(nodo, ast.Call):
            # Sacarle un atributo a un espacio devuelve lo que ese espacio
            # tenga dentro, o sea otro espacio. Cuál es la función se decide
            # RESOLVIÉNDOLA al objeto, nunca por su nombre: `ga(sys, "modules")`
            # con `ga = getattr` es exactamente `getattr(sys, "modules")`.
            if self.llama_a(nodo, _ATRIBUTO_POR_NOMBRE) is not None and \
                    nodo.args:
                return self.es_espacio(nodo.args[0])
            libre = _nombre_llamado(nodo)
            # Llamar SIN argumentos a un nombre que este archivo no ata es la
            # forma de pedir un espacio de nombres entero: `globals()`,
            # `locals()`, `vars()`. De ahí sale cualquier cosa que el archivo
            # tenga a mano, módulos incluidos.
            if libre and libre not in self.atados and not nodo.args \
                    and not nodo.keywords:
                return True
            # Y pasarle un espacio a CUALQUIER llamada devuelve otra vista del
            # mismo espacio: `vars(mod)`, `list(sys.modules.values())`,
            # `ga(sys, "modules")`. Antes esto solo valía para las llamadas a un
            # nombre suelto que el archivo no atara, así que bastaba con atar el
            # nombre —`ga = getattr`— para que la rama no se ejecutara.
            #
            # Lo que NO se hereda es el resultado de una llamada cuyos
            # argumentos no son espacios: `hmac.new(token, carga, sha256)`
            # devuelve un objeto HMAC, no el módulo `hmac`. Tratarlo como
            # espacio rojeaba `...hexdigest()[:32]` en web/auth.py sin que
            # hubiera por dónde llegar a un módulo.
            return any(self.es_espacio(a) for a in nodo.args)
        return False


def _es_el_modulo_config(nodo, locales: set[str]) -> bool:
    """¿Esta expresión ES el módulo config?

    Dos formas, y las dos se ven en el árbol: un nombre que un `import` ató al
    módulo (con alias o sin él), o el atributo `.config` de cualquier otra cosa
    — que es como se llega a config a través de un módulo que ya lo importó.
    """
    if isinstance(nodo, ast.Name) and nodo.id in locales:
        return True
    return isinstance(nodo, ast.Attribute) and nodo.attr == _MODULO_CONFIG


def _infracciones(fuente, permitidos: set[str]) -> list[str]:
    """Todo lo que en este archivo alcanza (o podría alcanzar) la lista cruda.

    Devuelve motivos con número de línea. Lista vacía = el archivo no tiene por
    dónde conseguir un buzón sin pasar por `cuentas_de_correo(para=...)`.
    """
    try:
        arbol = ast.parse(fuente)
    except SyntaxError as e:
        return [f"línea {e.lineno}: no se pudo leer como Python ({e.msg}); "
                "sin árbol no hay nada que clasificar, y sin clasificar es rojo"]

    for padre in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(padre):
            hijo._padre = padre                      # type: ignore[attr-defined]

    ctx = _Contexto(arbol)
    malas: list[str] = []

    def pedidos(nodo):
        return _cadenas(nodo, ctx.ambitos)

    for n in ast.walk(arbol):
        # (a) El nombre prohibido escrito como atributo de lo que sea. Cubre
        #     `config.X`, `cfg.X` y `otro_modulo.config.X` de una sola vez,
        #     porque el árbol no distingue con qué alias se llegó.
        if isinstance(n, ast.Attribute) and n.attr == _PROHIBIDO:
            malas.append(f"línea {n.lineno}: lee el atributo .{_PROHIBIDO}")

        # (b) El nombre prohibido traído por un import, de donde sea y con el
        #     alias que sea — el alias vive en el nodo, no en el texto.
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == _PROHIBIDO:
                    como = f" y lo llama {a.asname}" if a.asname else ""
                    malas.append(
                        f"línea {n.lineno}: importa {_PROHIBIDO} de "
                        f"{n.module}{como}")
                elif a.name == "*":
                    malas.append(
                        f"línea {n.lineno}: `from {n.module} import *` trae "
                        "nombres que no puedo enumerar")

        # (i) Un import que TRAE uno de los siete objetos peligrosos, del módulo
        #     que sea y con el alias que sea. `from builtins import getattr as
        #     ga` no nombra a `getattr` en ninguna expresión, así que el motivo
        #     (h) no lo ve: el objeto entra por la puerta del import. Quién
        #     entra se decide resolviendo el camino punteado AL OBJETO de hoy,
        #     no leyendo el alias.
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name == "*":
                    continue
                local = a.asname or a.name
                if local not in ctx.importados:
                    continue
                hay, obj = _objeto_ya_cargado(ctx.importados[local])
                # Una LISTA, no un conjunto: lo que devuelve un import puede
                # ser inhashable y meterlo en un `set` reventaría la guarda.
                cual = _cual_peligroso([obj]) if hay else None
                if cual is not None:
                    malas.append(
                        f"línea {n.lineno}: importa {cual.__name__} y lo llama "
                        f"{local!r}; ponerle otro nombre es justo lo que apaga "
                        "las comprobaciones que lo reconocen")

        # (c) Un atributo pedido por su nombre en tiempo de ejecución. Si la
        #     guarda no puede enumerar qué nombres se piden no hay nada que
        #     clasificar, y eso vale sobre cualquier objeto, sea módulo o no.
        #
        #     CUÁL es la función se decide resolviéndola AL OBJETO. Antes se
        #     comparaba el texto del identificador contra
        #     `("getattr", "setattr", "delattr")`, así que `ga = getattr` apagaba
        #     esta comprobación entera con una línea.
        pide = ctx.llama_a(n, _ATRIBUTO_POR_NOMBRE)
        if pide is not None and len(n.args) >= 2:
            comose = ast.unparse(n.func)
            quiere = pedidos(n.args[1])
            if quiere is None:
                malas.append(
                    f"línea {n.lineno}: {comose} es {pide.__name__} y pide un "
                    "nombre de atributo que no puedo enumerar; sin saber qué "
                    "pide no hay nada que clasificar")
            elif _PROHIBIDO in quiere:
                malas.append(
                    f"línea {n.lineno}: pide el atributo {_PROHIBIDO} por "
                    f"{comose}, que es {pide.__name__}")

        # (g) Convertir un TEXTO en código o en un módulo. Acá no se mira qué
        #     lleva dentro el string a propósito: el código que va a correr no
        #     existe hasta que corre, así que no hay NADA que analizar y
        #     perseguirlo es una carrera sin fondo. Usarlas es rojo por sí solo.
        #     Medido el 6-sep-2026: ningún archivo vigilado usa ninguna.
        hace = ctx.llama_a(n, _TEXTO_A_CODIGO)
        if hace is not None:
            malas.append(
                f"línea {n.lineno}: llama a {hace.__name__}, que convierte un "
                "texto en código; lo que corra ahí no existe hasta que corre y "
                "no hay forma de saber si alcanza la lista cruda")

        # (h) EL FONDO: uno de los siete objetos peligrosos NOMBRADO sin
        #     llamarlo. Ahí es donde se fabrica un alias —una tupla, un dict,
        #     una clausura, el valor por defecto de un parámetro— y es la única
        #     comprobación que no depende de poder seguir la asignación.
        if isinstance(n, (ast.Name, ast.Attribute)) and \
                isinstance(getattr(n, "ctx", None), ast.Load):
            padre = getattr(n, "_padre", None)
            if not (isinstance(padre, ast.Call) and padre.func is n):
                cual = _cual_peligroso(ctx.resuelve(n))
                if cual is not None:
                    malas.append(
                        f"línea {n.lineno}: nombra a {cual.__name__} sin "
                        f"llamarlo ({ast.unparse(n)}); así se le pone otro "
                        "nombre y las comprobaciones dejan de reconocerlo")

        # (j) Abrirle a algo su espacio de nombres y pedirle una clave que no
        #     puedo enumerar. Es la tercera forma de sacarle un atributo a un
        #     módulo, después del punto y de `getattr`, y la única que se
        #     escapaba de las dos: `vars(importlib.import_module(n))[k]`.
        if isinstance(n, ast.Subscript):
            base = n.value
            abre = (ctx.llama_a(base, (builtins.vars,)) is not None
                    or (isinstance(base, ast.Attribute)
                        and base.attr == "__dict__"))
            if abre and pedidos(n.slice) is None:
                malas.append(
                    f"línea {n.lineno}: le pide al espacio de nombres de algo "
                    "una clave que no puedo enumerar; de ahí sale cualquier "
                    "atributo de cualquier módulo, la lista cruda incluida")

        # (d) Pedir por su nombre el MÓDULO config: `sys.modules["config"]`,
        #     `importlib.import_module("config")`, `globals()["config"]`. El
        #     nombre sale de `config.__name__`, no de acá.
        if isinstance(n, ast.Call):
            for arg in list(n.args) + [k.value for k in n.keywords]:
                trae = pedidos(arg)
                if trae and _MODULO_CONFIG in trae:
                    malas.append(
                        f"línea {n.lineno}: le pasa el nombre "
                        f"{_MODULO_CONFIG!r} a una llamada; así se consigue el "
                        "módulo sin nombrarlo en un import")

        # (e) Un nombre sacado de un espacio del que puede salir un módulo. De
        #     ahí solo se puede pedir un nombre que la guarda pueda enumerar:
        #     si no puede, o si es uno de los dos prohibidos, es rojo. Acá cae
        #     `sys.modules["config"]` y `getattr(sys, "modules")[...]` sin que
        #     `sys` esté apuntado en ninguna lista.
        if isinstance(n, ast.Subscript) and ctx.es_espacio(n.value):
            claves = pedidos(n.slice)
            donde = _punteado(n.value) or "un espacio de nombres"
            prohibidas = {_PROHIBIDO, _MODULO_CONFIG}
            if claves is None:
                malas.append(
                    f"línea {n.lineno}: saca de {donde} un nombre que no puedo "
                    "enumerar; de ahí puede salir cualquier módulo")
            elif claves & prohibidas:
                cual = sorted(claves & prohibidas)
                malas.append(
                    f"línea {n.lineno}: saca {cual} de {donde}, que es "
                    "exactamente la lista cruda o el módulo que la tiene")

        # (f) El objeto módulo config usado para CUALQUIER otra cosa que no sea
        #     leer uno de sus atributos permitidos. Acá caen `vars(config)`,
        #     `config.__dict__`, `otro = config` y todo lo que todavía no se le
        #     ocurrió a nadie.
        if _es_el_modulo_config(n, ctx.locales_config):
            padre = getattr(n, "_padre", None)
            bien = (isinstance(padre, ast.Attribute)
                    and padre.value is n
                    and padre.attr in permitidos)
            # Pedirle un atributo permitido por `getattr` con un literal es
            # exactamente igual de clasificable que escribirlo con un punto: se
            # sabe qué nombre se pide y se sabe que no es el prohibido.
            if not bien and isinstance(padre, ast.Call) and \
                    ctx.llama_a(padre, (builtins.getattr,)) is not None and \
                    len(padre.args) >= 2 and padre.args[0] is n:
                quiere = pedidos(padre.args[1])
                bien = bool(quiere) and quiere is not None and \
                    quiere <= permitidos
            if not bien:
                como = (f".{padre.attr}" if isinstance(padre, ast.Attribute)
                        else type(padre).__name__ if padre else "suelto")
                malas.append(
                    f"línea {n.lineno}: usa el módulo config de una forma que "
                    f"no puedo clasificar ({como}); lo único permitido es "
                    "leerle un atributo suyo que no sea la lista cruda")

    return sorted(set(malas))


# ── Tramo 3: quién queda fuera de la vigilancia ───────────────────────────
#
# Antes era `rel.name in ("config.py", "conftest.py")`, o sea POR NOMBRE DE
# ARCHIVO: bastaba con llamar `conftest.py` a un archivo de producción, en la
# carpeta que fuera, para que la guarda dejara de mirarlo. Ahora la exención
# sale de hechos comprobables sobre el archivo, y cada hecho se lee de un
# artefacto real del repo:
#
#   · DÓNDE VIVE  → `pytest.ini`, campo `testpaths`, que es lo que pytest
#     recoge de verdad. Y el `conftest.py` de la rootdir de pytest (la carpeta
#     donde está `pytest.ini`), que es el otro sitio del que pytest carga
#     andamio en este repo.
#   · QUIÉN LO CARGA → el cierre de imports del `startCommand` de
#     `railway.json`. Si un archivo se alcanza importando desde ahí, corre en
#     producción y no puede ser andamio de pruebas, se llame como se llame.
#   · Y aparte, el archivo que DEFINE la lista, que sale de `config.__file__`
#     — del objeto módulo, no de un nombre tecleado.
#
# Un archivo que no encaje en ninguno de esos hechos queda DENTRO de la
# vigilancia. `captura/conftest.py` no está bajo `testpaths`, no es el conftest
# de la rootdir y no es config.py: se mira igual que `captura/_utilidades.py`.

def _pytest_ini(raiz: Path, campo: str) -> list[str]:
    """Un campo de `pytest.ini`, partido en palabras. Sin archivo, nada."""
    ini = raiz / "pytest.ini"
    if not ini.is_file():
        return []
    cfg = configparser.ConfigParser(allow_no_value=True,
                                    comment_prefixes=(";", "#"))
    try:
        cfg.read_string(ini.read_text(encoding="utf-8"))
    except configparser.Error:
        return []
    return cfg.get("pytest", campo, fallback="").split()


def _testpaths(raiz: Path) -> list[Path]:
    """Las carpetas que pytest recoge de verdad, leídas de `pytest.ini`."""
    return [raiz / p for p in _pytest_ini(raiz, "testpaths")
            if (raiz / p).is_dir()]


def _carpetas_que_no_son_del_repo(raiz: Path) -> set[str]:
    """Las carpetas que ni pytest ni la guarda recorren.

    Sale de `norecursedirs` de `pytest.ini` —el venv, los cachés, `.git`— y no
    de una lista escrita acá. Era la TERCERA lista tecleada de este archivo: si
    mañana aparece otra carpeta de herramientas, se añade en pytest.ini una vez
    y las dos cosas se enteran. Sin `pytest.ini` no se salta nada, que es el
    lado seguro: más archivos mirados, no menos.
    """
    return set(_pytest_ini(raiz, "norecursedirs"))


def _entradas_de_produccion(raiz: Path) -> list[Path]:
    """Con qué archivo arranca Lucy en Railway, leído de `railway.json`.

    Se entienden las DOS formas normales de arrancar Python, porque el arreglo
    de una no puede ser esperar que nadie escriba la otra:

        "python main.py"    → el archivo, tal cual
        "python -m main"    → el módulo, resuelto a `main.py` o `main/__init__.py`

    Devolver la lista VACÍA significa «no pude determinar con qué arranca
    producción», y quien lo llama tiene que tratarlo como tal — ver
    `_archivos_exentos`. Antes esto se filtraba con `t.endswith(".py")` a secas:
    un `startCommand` con `-m` daba una lista vacía EN SILENCIO, el cierre de
    imports salía vacío, y con él todo lo que vive bajo `testpaths` quedaba
    exento aunque producción lo cargara. Verificado ejecutando el 6-sep-2026.
    """
    conf = raiz / "railway.json"
    if not conf.exists():
        return []
    try:
        datos = json.loads(conf.read_text(encoding="utf-8"))
    except ValueError:
        return []
    trozos = str(datos.get("deploy", {}).get("startCommand", "")).split()
    entradas: list[Path] = []
    for i, t in enumerate(trozos):
        if t.endswith(".py") and (raiz / t).is_file():
            entradas.append(raiz / t)
        elif t == "-m" and i + 1 < len(trozos):
            partes = trozos[i + 1].split(".")
            for cand in (raiz.joinpath(*partes).with_suffix(".py"),
                         raiz.joinpath(*partes, "__init__.py")):
                if cand.is_file():
                    entradas.append(cand)
                    break
    return entradas


def _cierre_de_imports(raiz: Path, entradas: list[Path]) -> set[Path]:
    """Los .py del repo a los que se llega importando desde `entradas`."""
    visto: set[Path] = set()
    pila = list(entradas)
    while pila:
        py = pila.pop()
        py = py.resolve()
        if py in visto or not py.is_file():
            continue
        visto.add(py)
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        punteados: set[str] = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                punteados.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                punteados.add(n.module)
                punteados.update(f"{n.module}.{a.name}" for a in n.names
                                 if a.name != "*")
        for p in punteados:
            trozos = p.split(".")
            for cand in (raiz.joinpath(*trozos).with_suffix(".py"),
                         raiz.joinpath(*trozos, "__init__.py")):
                if cand.is_file():
                    pila.append(cand)
    return visto


def _donde_vive_config() -> Path | None:
    """Dónde vive `config.py` DENTRO del repo, preguntándole al módulo.

    Sale de `config.__file__`, o sea del objeto módulo que estas pruebas ya
    tienen importado, y se devuelve relativo a la raíz del repo. Si mañana
    config se mueve de carpeta, esto se mueve con él sin que nadie lo edite.
    """
    donde = getattr(config, "__file__", None)
    if not donde:
        return None
    try:
        return Path(donde).resolve().relative_to(RAIZ)
    except ValueError:
        return None


def _archivos_exentos(raiz: Path) -> set[Path]:
    """Quién queda fuera de la vigilancia, y por qué hecho comprobable.

    Nada de esto es un nombre de archivo tecleado: sale de `config.__file__`,
    de `pytest.ini` y de `railway.json`. Y lo que no encaje se queda DENTRO.
    """
    entradas = _entradas_de_produccion(raiz)
    produccion = _cierre_de_imports(raiz, entradas)
    exentos: set[Path] = set()

    # El archivo que DEFINE la lista. Dónde vive lo dice el módulo de verdad,
    # no un nombre tecleado; se guarda como camino RELATIVO al repo para que
    # esto siga valiendo sobre una copia del árbol sacada a otra carpeta.
    definidor = _donde_vive_config()
    if definidor is not None and (raiz / definidor).is_file():
        exentos.add((raiz / definidor).resolve())

    # Andamio de pruebas: pytest lo carga Y no corre en producción. Los dos
    # hechos a la vez; con uno solo no alcanza.
    #
    # Y SI NO SE SUPO CON QUÉ ARRANCA PRODUCCIÓN, no se exenta a nadie. El
    # segundo hecho —«no corre en producción»— es el que separa un archivo de
    # pruebas de uno que produccion carga, y sin las entradas ese hecho no se
    # puede comprobar: darlo por cierto convertiría «no lo sé» en «no corre», o
    # sea lo desconocido del lado verde. Cuesta más archivos mirados, nunca
    # menos, que es el lado seguro del error.
    if not entradas:
        return exentos
    candidatos: set[Path] = set()
    for carpeta in _testpaths(raiz):
        candidatos.update(p.resolve() for p in carpeta.rglob("*.py"))
    if (raiz / "pytest.ini").is_file() and (raiz / "conftest.py").is_file():
        candidatos.add((raiz / "conftest.py").resolve())
    exentos |= {p for p in candidatos if p not in produccion}
    return exentos


def _archivos_vigilados(raiz: Path) -> list[Path]:
    """Los .py que la guarda mira de verdad: ni saltados ni exentos.

    Está acá, y no repetido dentro de cada prueba, porque varias necesitan
    contar sobre EXACTAMENTE el mismo conjunto que se vigila. Dos copias de este
    filtro se separarían, y entonces «cero falsos positivos sobre 37 archivos»
    y «7 getattr sobre 37 archivos» dejarían de hablar del mismo 37.
    """
    fuera = _carpetas_que_no_son_del_repo(raiz)
    exentos = _archivos_exentos(raiz)
    return [p for p in raiz.rglob("*.py")
            if not any(x in fuera for x in p.relative_to(raiz).parts)
            and p.resolve() not in exentos]


def _quienes_leen_la_lista_cruda(raiz: Path) -> dict[str, list[str]]:
    """Recorre los .py que hay EN DISCO bajo `raiz` y los clasifica.

    Ningún archivo se nombra acá. Qué carpetas no se recorren sale de
    `norecursedirs` de pytest.ini; quién queda exento, de `_archivos_exentos`,
    que lo deriva de `config.__file__`, de `testpaths` y del cierre de imports
    del `startCommand` de railway.json. Lo que no encaje en esos hechos se
    queda DENTRO de la vigilancia, que es el lado seguro del error.

    Medido el 6-sep-2026 sobre este repo: 66 archivos .py, 29 exentos
    (config.py, el conftest de la rootdir y los 27 de `tests/`), 37 vigilados,
    0 culpables. Las cuatro cifras las vuelve a medir al correr
    `test_cuantos_falsos_positivos_hay_hoy_sobre_los_archivos_reales`, para que
    ninguna quede acá afirmada sin que nada la compruebe.
    """
    permitidos = _atributos_que_config_ofrece()
    exentos = _archivos_exentos(raiz)
    fuera = _carpetas_que_no_son_del_repo(raiz)
    culpables: dict[str, list[str]] = {}
    for py in sorted(raiz.rglob("*.py")):
        rel = py.relative_to(raiz)
        if any(p in fuera for p in rel.parts):
            continue
        if py.resolve() in exentos:
            continue
        motivos = _infracciones(py.read_bytes(), permitidos)
        if motivos:
            culpables[str(rel)] = motivos
    return culpables


def test_nadie_lee_la_lista_cruda():
    """LA guarda del arreglo, y lo que hace que valga para el camino que
    todavía no existe.

    Recorre los .py que hay EN DISCO —no una lista de archivos escrita acá— y
    exige que nadie fuera de `config.py` pueda alcanzar la lista cruda. Un
    archivo nuevo que se saltee la puerta pone esto rojo solo, sin que nadie se
    acuerde de venir a añadirlo, y sin que importe cómo lo escriba.
    """
    culpables = _quienes_leen_la_lista_cruda(RAIZ)
    assert not culpables, (
        "estos archivos alcanzan la lista cruda de buzones en vez de pedirla "
        f"por config.cuentas_de_correo(para=...): {culpables}. Esa lista trae "
        "TODOS los buzones, incluidos los que no se le enseñan a Tiziano")


# Caminos, todos haciendo LO MISMO: devolver la lista cruda. Cuántos son lo
# dice `len(_ESQUIVES)` allí donde hace falta el número —
# `test_la_guarda_muerde_todos_los_caminos_a_la_lista_cruda` lo cuenta al
# correr—, y por eso acá no va ninguna cifra escrita: la que había decía
# «Trece» con catorce entradas debajo, y una cifra tecleada que nadie vuelve a
# medir se separa de la realidad exactamente igual que una lista tecleada.
#
#   · Los tres primeros son los que la guarda de TEXTO midió el 5-sep-2026
#     (uno rojo, dos verdes).
#   · Del cuarto al octavo, las formas siguientes — las que se le habrían
#     escapado a un parche que solo añadiera las dos primeras al patrón.
#   · El noveno es EL AGUJERO que un testigo encontró en la guarda de `ast`:
#     con `getattr(sys, "modules")` el nombre del atributo es un `ast.Constant`
#     y no un `ast.Attribute`, así que la comprobación de `sys.modules` no
#     disparaba, y `sys` no estaba en la lista de fábricas de módulos. Puesto
#     como archivo real en `captura/`: 16 passed, cero infracciones, y la
#     función devolvía las credenciales del buzón con `reporte_a: 0`.
#   · Los cuatro últimos los buscó el agente que arregló el agujero, para no
#     dar por bueno el arreglo con las mismas pruebas que lo motivaron.
_ESQUIVES = {
    "el nombre escrito entero": """
import config


def cuentas():
    return [c for c in config.CORREO_CUENTAS]
""",
    "importado con alias": """
from config import CORREO_CUENTAS as CUENTAS


def cuentas():
    return [c for c in CUENTAS]
""",
    "el nombre partido en dos y pegado en getattr": """
import config


def cuentas():
    return [c for c in getattr(config, "CORREO_" + "CUENTAS")]
""",
    "por vars() del módulo": """
import config


def cuentas():
    return [c for c in vars(config)["CORREO_CUENTAS"]]
""",
    "por el __dict__ del módulo": """
import config


def cuentas():
    return [c for c in config.__dict__["CORREO_CUENTAS"]]
""",
    "el módulo importado con otro nombre": """
import config as cfg


def cuentas():
    return [c for c in cfg.CORREO_CUENTAS]
""",
    "el módulo traído por importlib": """
import importlib


def cuentas():
    return [c for c in importlib.import_module("config").CORREO_CUENTAS]
""",
    "el nombre armado por una función": """
import config


def _nombre():
    return "".join(["CORREO", "_", "CUENTAS"])


def cuentas():
    return [c for c in getattr(config, _nombre())]
""",
    "por la tabla de módulos, pedida con getattr": """
import sys

def cuentas():
    modulo = getattr(sys, "modules")["config"]
    return [c for c in getattr(modulo, "CORREO_CUENTAS")]
""",
    "la tabla de módulos guardada en una variable, con la clave armada": """
import sys


def _n():
    return "con" + "fig"


def cuentas():
    tabla = sys.modules
    return getattr(tabla[_n()], "CORREO_" + "CUENTAS")
""",
    "el módulo guardado como atributo de una clase": """
import config


class Caja:
    mod = config


def cuentas():
    return [c for c in Caja.mod.CORREO_CUENTAS]
""",
    "el módulo pasado como valor por defecto de un parámetro": """
import config


def cuentas(_m=config):
    return [c for c in _m.CORREO_CUENTAS]
""",
    "sys.modules.get() en vez del subíndice": """
import sys


def cuentas():
    return [c for c in sys.modules.get("config").CORREO_CUENTAS]
""",
    # Éste cae por UN solo motivo: el nombre 'config' viajando dentro de una
    # llamada. Ni el atributo se escribe, ni el módulo se ata a un nombre que
    # la guarda reconozca. Si esa regla se cayera, esta forma volvería a pasar.
    "traído por importlib y abierto con vars()": """
import importlib


def cuentas():
    mod = importlib.import_module("config")
    return [c for c in vars(mod)["CORREO_CUENTAS"]]
""",
    # ── Los seis siguientes son el TERCER agujero, medido el 6-sep-2026. Los
    #    cinco primeros son la misma causa: `getattr` se reconocía comparando el
    #    TEXTO del identificador, en dos sitios a la vez, así que ponerle otro
    #    nombre apagaba las dos comprobaciones con una sola línea.
    "getattr con otro nombre, atado en una asignación": """
ga = getattr


def cuentas():
    import sys
    modulo = ga(sys, "mod" + "ules")["config"]
    return ga(modulo, "CORREO_" + "CUENTAS")
""",
    "getattr con otro nombre, traído por un import": """
from builtins import getattr as ga


def cuentas():
    import sys
    modulo = ga(sys, "mod" + "ules")["config"]
    return ga(modulo, "CORREO_" + "CUENTAS")
""",
    # Éste es el residuo que ninguna resolución de asignaciones alcanza: el
    # alias nace dentro de una tupla. Cae por el FONDO —nombrar a `getattr` sin
    # llamarlo— y no por haber podido seguir la asignación.
    "getattr con otro nombre, sacado de una tupla": """
_HERRAMIENTAS = (getattr, len)


def cuentas():
    import sys
    ga = _HERRAMIENTAS[0]
    return ga(ga(sys, "modules")["config"], "CORREO_CUENTAS")
""",
    "getattr con otro nombre, devuelto por una función": """
def _dame():
    return getattr


def cuentas():
    import sys
    ga = _dame()
    return ga(ga(sys, "modules")["config"], "CORREO_CUENTAS")
""",
    "getattr escrito por su módulo": """
import builtins
import sys


def cuentas():
    modulo = builtins.getattr(sys, "modules")["config"]
    return builtins.getattr(modulo, "CORREO_CUENTAS")
""",
    # Y la segunda forma, de la misma familia y con otra causa: el código que
    # `eval` va a correr NO EXISTE hasta que corre, así que no hay nada que
    # analizar. No se persigue lo que lleva dentro el texto; usarla es rojo.
    "por eval, con el nombre dentro de un texto": """
import config


def cuentas():
    return eval("config.CORREO_CUENTAS")
""",
    "por exec, con el nombre dentro de un texto": """
import config


def cuentas():
    d = {}
    exec("x = config.CORREO_CUENTAS", {"config": config}, d)
    return d["x"]
""",
    # Y la fábrica de módulos con el nombre en una variable: ni el atributo se
    # escribe, ni el nombre 'config' viaja como literal, así que los motivos (a)
    # y (d) no lo ven. Cae por pedirle una clave no enumerable al espacio de
    # nombres de algo.
    "importlib con el nombre en una variable, abierto con vars()": """
import importlib


def cuentas(nombre, clave):
    return vars(importlib.import_module(nombre))[clave]
""",
    # `vars(x)` y `x.__dict__` son la misma puerta escrita de dos formas, así
    # que las dos tienen que estar cerradas. Ésta existe para que la mitad
    # `__dict__` del motivo (j) no se pueda borrar sin que nada se ponga rojo:
    # medido el 6-sep-2026, quitándola las 30 pruebas seguían en verde.
    "importlib con el nombre en una variable, abierto por __dict__": """
import importlib


def cuentas(nombre, clave):
    return importlib.import_module(nombre).__dict__[clave]
""",
}

# Y lo que TIENE que seguir en verde: los usos legítimos que hay hoy en el
# repo, escritos igual que en `cerebro/`, `db/` y `captura/`.
_LEGITIMOS = {
    "import config y un atributo suyo": """
import config


def a_quien():
    return config.CHAT_ID_DUENO
""",
    "from config import de nombres normales": """
from config import TZ, OPENAI_API_KEY


def zona():
    return TZ, OPENAI_API_KEY
""",
    "la puerta, que es como se pide un buzón": """
import config


def cuentas():
    return [c["user"] for c in config.cuentas_de_correo("mostrar")]
""",
    "un atributo permitido pedido por getattr con literal": """
import config


def zona():
    return getattr(config, "TZ", None)
""",
    "getattr sobre un módulo que no es config, con un nombre enumerable": """
import psycopg


ERRORES = tuple(getattr(psycopg, n, None)
                for n in ("IntegrityError", "DataError"))
""",
    "cortar el resultado de un método": """
import hashlib
import hmac

import config


def firmar(carga: str) -> str:
    return hmac.new(config.TELEGRAM_TOKEN.encode(), carga.encode(),
                    hashlib.sha256).hexdigest()[:32]
""",
}


def test_la_guarda_muerde_todos_los_caminos_a_la_lista_cruda():
    """Cada forma de arriba tiene que poner la guarda ROJA.

    Se corre el archivo de verdad —`_quienes_leen_la_lista_cruda`, el mismo que
    usa `test_nadie_lee_la_lista_cruda`— sobre una carpeta temporal FUERA del
    repositorio, con un .py por forma. No se escribe nada dentro del repo.
    """
    import tempfile

    permitidos = _atributos_que_config_ofrece()
    escapados = [nombre for nombre, fuente in _ESQUIVES.items()
                 if not _infracciones(fuente, permitidos)]
    assert not escapados, (
        f"estas formas de leer la lista cruda se le escapan a la guarda: "
        f"{escapados}. Todas devuelven los buzones enteros, incluido el que "
        "tiene reporte_a: 0")

    # Y el recorrido del disco entero, no solo el clasificador: un archivo con
    # cualquiera de las formas, puesto en una carpeta como la del repo, tiene
    # que aparecer en los culpables.
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        (raiz / "tests").mkdir()
        for i, fuente in enumerate(_ESQUIVES.values()):
            (raiz / f"camino_nuevo_{i}.py").write_text(fuente, encoding="utf-8")
        culpables = _quienes_leen_la_lista_cruda(raiz)
    assert len(culpables) == len(_ESQUIVES), (
        f"el recorrido del disco solo señaló {sorted(culpables)} de "
        f"{len(_ESQUIVES)} archivos culpables")


def test_la_guarda_muerde_el_camino_por_la_tabla_de_modulos():
    """EL agujero de la versión anterior, con su código exacto.

    `getattr(sys, "modules")` esquivaba las dos comprobaciones a la vez: el
    nombre del atributo viajaba como `ast.Constant` en vez de `ast.Attribute`,
    y `sys` no estaba en la lista tecleada de fábricas de módulos. Medido el
    5-sep-2026 puesto en `captura/`: 16 passed, cero infracciones, y la función
    devolvía de verdad los buzones con `reporte_a: 0` y sus credenciales.

    Ahora cae por DOS motivos independientes, y ninguno de los dos nombra a
    `sys`: sacar el nombre 'config' de un espacio del que pueden salir módulos,
    y pedir el atributo prohibido por `getattr`.
    """
    fuente = _ESQUIVES["por la tabla de módulos, pedida con getattr"]
    motivos = _infracciones(fuente, _atributos_que_config_ofrece())
    assert len(motivos) >= 2, (
        f"el camino por la tabla de módulos solo cayó por {motivos}; se espera "
        "que caiga por el nombre del módulo y por el atributo pedido")
    assert any(_MODULO_CONFIG in m for m in motivos), (
        f"nadie vio que se estaba sacando el módulo {_MODULO_CONFIG!r} de un "
        f"espacio de nombres: {motivos}")
    assert any(_PROHIBIDO in m for m in motivos), (
        f"nadie vio que se estaba pidiendo {_PROHIBIDO}: {motivos}")


# ── Que un archivo esté exento NO puede depender de cómo se llame ─────────

def _repo_de_mentira(raiz: Path) -> None:
    """Un repo con la forma del de Lucy, para medir la exención sin tocar el
    repo de verdad: `pytest.ini` con sus testpaths, `railway.json` con su
    startCommand, un `main.py` que importa producción, y las carpetas."""
    (raiz / "tests").mkdir()
    (raiz / "captura").mkdir()
    (raiz / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\nnorecursedirs = .venv __pycache__\n",
        encoding="utf-8")
    (raiz / "railway.json").write_text(
        '{"deploy": {"startCommand": "python main.py"}}', encoding="utf-8")
    (raiz / "main.py").write_text(
        "import captura.correo\n", encoding="utf-8")
    (raiz / "captura" / "__init__.py").write_text("", encoding="utf-8")
    (raiz / "captura" / "correo.py").write_text("", encoding="utf-8")
    (raiz / "conftest.py").write_text("import sys\n", encoding="utf-8")
    (raiz / "tests" / "test_algo.py").write_text("", encoding="utf-8")


def test_la_exencion_no_mira_el_nombre_del_archivo():
    """El SEGUNDO agujero, medido: el mismo código, byte por byte, daba
    `captura/_utilidades.py → 1 failed` y `captura/conftest.py → 16 passed`.

    Bastaba con ponerle a un archivo de producción el nombre `conftest.py`, en
    cualquier carpeta del repo, para que la guarda dejara de mirarlo — porque
    la exención se decidía con `rel.name in ("config.py", "conftest.py")`, o
    sea por el NOMBRE.

    Ahora un archivo es andamio de pruebas por DÓNDE VIVE (bajo un `testpaths`
    de pytest.ini, o el conftest de la rootdir) y por QUIÉN LO CARGA (que no
    esté en el cierre de imports del `startCommand` de railway.json). El nombre
    no entra en la cuenta, así que los dos archivos dan el MISMO veredicto.
    """
    import tempfile

    fuga = _ESQUIVES["el nombre escrito entero"]
    veredictos = {}
    for donde in ("captura/_utilidades.py", "captura/conftest.py",
                  "captura/config.py", "cerebro/conftest.py", "conftest2.py"):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _repo_de_mentira(raiz)
            destino = raiz / donde
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(fuga, encoding="utf-8")
            veredictos[donde] = donde in _quienes_leen_la_lista_cruda(raiz)

    assert all(veredictos.values()), (
        f"la guarda dejó de mirar algún archivo por cómo se llama: "
        f"{veredictos}. Los cinco tienen el MISMO código y ninguno es andamio "
        "de pruebas: los cinco tienen que salir culpables")


def test_el_andamio_de_pruebas_de_verdad_si_queda_exento():
    """El control de la de arriba: si la exención dejara de existir, aquélla
    pasaría igual y esto se pondría rojo. Las dos juntas fijan la línea.

    El `conftest.py` de la rootdir y lo que vive bajo `testpaths` SÍ quedan
    fuera: pytest los carga y no están en el cierre de imports de producción.
    Es lo que deja que el conftest use `sys.modules` para aislar las pruebas.
    """
    import tempfile

    fuga = _ESQUIVES["el nombre escrito entero"]
    for donde in ("conftest.py", "tests/test_algo.py", "tests/hondo/aux.py"):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _repo_de_mentira(raiz)
            destino = raiz / donde
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(fuga, encoding="utf-8")
            culpables = _quienes_leen_la_lista_cruda(raiz)
            assert donde not in culpables, (
                f"{donde} es andamio de pruebas y la guarda lo señaló igual: "
                f"{culpables}. Eso convierte la guarda en un impuesto")


def test_un_conftest_que_SI_corre_en_produccion_no_queda_exento():
    """Los dos hechos, y hacen falta los dos.

    Un `conftest.py` en la rootdir que además esté en el cierre de imports de
    producción no es andamio: alguien lo importó desde `main.py`. Ahí la
    exención se cae sola, sin que nadie tenga que acordarse de quitarlo de una
    lista.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        _repo_de_mentira(raiz)
        (raiz / "main.py").write_text("import conftest\n", encoding="utf-8")
        (raiz / "conftest.py").write_text(
            _ESQUIVES["el nombre escrito entero"], encoding="utf-8")
        culpables = _quienes_leen_la_lista_cruda(raiz)
    assert "conftest.py" in culpables, (
        "un conftest.py importado desde el arranque de producción siguió "
        f"exento: {culpables}")


def test_las_exenciones_salen_de_artefactos_que_existen():
    """Una exención derivada de un archivo que no está es una exención muda.

    Si mañana desaparece `pytest.ini` o `railway.json`, o config se muda, esto
    se pone rojo en vez de dejar de vigilar en silencio.
    """
    assert (RAIZ / "pytest.ini").is_file(), "sin pytest.ini no sé qué recoge pytest"
    assert (RAIZ / "railway.json").is_file(), (
        "sin railway.json no sé con qué arranca producción")
    assert _testpaths(RAIZ) == [RAIZ / "tests"], (
        f"testpaths de pytest.ini dejó de ser 'tests': {_testpaths(RAIZ)}")
    assert _entradas_de_produccion(RAIZ) == [RAIZ / "main.py"], (
        f"el startCommand de railway.json dejó de arrancar main.py: "
        f"{_entradas_de_produccion(RAIZ)}")
    assert _donde_vive_config() == Path("config.py"), (
        f"config se mudó a {_donde_vive_config()}; la exención lo sigue sola, "
        "pero conviene enterarse")

    produccion = _cierre_de_imports(RAIZ, _entradas_de_produccion(RAIZ))
    assert (RAIZ / "captura" / "correo.py").resolve() in produccion, (
        "el cierre de imports no llegó a captura/correo.py, que sí corre en "
        "producción: si el cierre se queda corto, la exención se ensancha")
    assert (RAIZ / "conftest.py").resolve() not in produccion, (
        "conftest.py aparece en el cierre de imports de producción")

    exentos = _archivos_exentos(RAIZ)
    assert (RAIZ / "config.py").resolve() in exentos
    assert (RAIZ / "conftest.py").resolve() in exentos
    assert (RAIZ / "captura" / "correo.py").resolve() not in exentos


def test_un_getattr_con_nombre_desconocido_es_rojo_sobre_lo_que_sea():
    """La regla que sostiene «lo que no sé cuenta como rojo» en su forma pura.

    `getattr(obj, nombre)` con `nombre` sin enumerar no se puede clasificar: la
    guarda no sabe si `obj` es config ni qué atributo se le pide. No hace falta
    que sea sospechoso — hace falta que no se pueda descartar.

    Es el único motivo por el que cae este archivo, así que si la regla se
    quitara, esto se pondría rojo. Medido el 5-sep-2026: cuesta CERO falsos
    positivos sobre los .py del repo: los 7 `getattr` que hay hoy en los
    archivos vigilados (4 en main.py, 2 en cerebro/interpretar.py, 1 en
    db/db.py) piden todos un nombre que sí se puede enumerar.
    """
    fuente = "def leer(obj, nombre):\n    return getattr(obj, nombre)\n"
    motivos = _infracciones(fuente, _atributos_que_config_ofrece())
    assert motivos, (
        "un getattr con el nombre del atributo en una variable pasó limpio; "
        "ahí no hay nada que clasificar y por eso tiene que ser rojo")


def test_las_carpetas_saltadas_se_leen_de_pytest_ini_de_verdad():
    """Que la lista de carpetas saltadas SALGA de pytest.ini, no que coincida.

    Con la lista tecleada a mano —`(".venv", "venv", "__pycache__")`, que es lo
    que había— el resultado de hoy es el mismo, así que comprobar los nombres
    no distingue una cosa de la otra. Esto sí: se monta un repo cuyo
    `norecursedirs` nombra una carpeta que ninguna lista tecleada tendría, y se
    exige que la guarda la respete.
    """
    import tempfile

    fuga = _ESQUIVES["el nombre escrito entero"]
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        _repo_de_mentira(raiz)
        (raiz / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\nnorecursedirs = trastero\n",
            encoding="utf-8")
        (raiz / "trastero").mkdir()
        (raiz / "trastero" / "x.py").write_text(fuga, encoding="utf-8")
        (raiz / "captura" / "y.py").write_text(fuga, encoding="utf-8")
        culpables = _quienes_leen_la_lista_cruda(raiz)

    assert "captura/y.py" in culpables, (
        f"la guarda dejó de mirar código normal del repo: {culpables}")
    assert "trastero/x.py" not in culpables, (
        "la guarda entró en una carpeta que pytest.ini dice no recorrer: la "
        f"lista de carpetas saltadas no sale de pytest.ini. Culpables: "
        f"{culpables}")


def test_las_carpetas_que_no_se_recorren_salen_de_pytest_ini():
    """La TERCERA lista tecleada que tenía este archivo, ya derivada.

    Decía `(".venv", "venv", "__pycache__")` a mano. Ahora sale de
    `norecursedirs` de pytest.ini, o sea del mismo sitio del que lo saca
    pytest: una carpeta de herramientas nueva se añade una vez y las dos cosas
    se enteran.
    """
    fuera = _carpetas_que_no_son_del_repo(RAIZ)
    assert ".venv" in fuera and "__pycache__" in fuera, (
        f"norecursedirs de pytest.ini ya no cubre el venv ni los cachés: "
        f"{sorted(fuera)}")
    assert "captura" not in fuera and "cerebro" not in fuera, (
        f"norecursedirs se comió una carpeta de código de Lucy: {sorted(fuera)}")


def test_cuantos_falsos_positivos_hay_hoy_sobre_los_archivos_reales():
    """El número, no el adjetivo.

    `test_nadie_lee_la_lista_cruda` dice que no hay ninguno; esto dice sobre
    CUÁNTOS archivos se midió, para que "cero falsos positivos" signifique algo
    y para que se note si mañana la guarda deja de mirar medio repo.
    """
    fuera = _carpetas_que_no_son_del_repo(RAIZ)
    todos = [p for p in RAIZ.rglob("*.py")
             if not any(x in fuera for x in p.relative_to(RAIZ).parts)]
    mirados = _archivos_vigilados(RAIZ)
    medido = {"en disco": len(todos), "exentos": len(todos) - len(mirados),
              "vigilados": len(mirados)}
    assert medido == {"en disco": 66, "exentos": 29, "vigilados": 37}, (
        f"el reparto de archivos cambió: {medido}, y el 6-sep-2026 era "
        "{'en disco': 66, 'exentos': 29, 'vigilados': 37}. Si los vigilados "
        "bajaron, algo se está saltando de más y «cero falsos positivos» dejó "
        "de significar lo que decía; si subieron, hay código nuevo que mirar")
    assert not _quienes_leen_la_lista_cruda(RAIZ), (
        "hay falsos positivos sobre los archivos reales de hoy")


def test_la_guarda_no_rojea_a_quien_usa_config_como_se_debe():
    """Cero falsos positivos: la guarda no puede volverse un impuesto.

    El repo entero ya se mide en `test_nadie_lee_la_lista_cruda` (hoy: 0
    culpables sobre los .py que hay en disco). Acá van además los tres usos
    legítimos escritos a la vista, para que un endurecimiento futuro que los
    rompa se vea acá y no en un archivo cualquiera.
    """
    permitidos = _atributos_que_config_ofrece()
    rojos = {n: _infracciones(f, permitidos)
             for n, f in _LEGITIMOS.items() if _infracciones(f, permitidos)}
    assert not rojos, f"la guarda rojeó usos legítimos de config: {rojos}"


def test_la_guarda_se_cae_si_la_lista_cruda_cambia_de_nombre():
    """Una guarda que vigila un nombre que ya no existe está verde sin mirar.

    `_atributos_que_config_ofrece` deriva los atributos permitidos de
    `vars(config)` y exige que el prohibido siga estando ahí. Si mañana alguien
    renombra la lista, esto revienta en vez de dejar de vigilar en silencio.
    """
    guardado = config.CORREO_CUENTAS
    try:
        del config.CORREO_CUENTAS
        reventado = False
        try:
            _atributos_que_config_ofrece()
        except AssertionError:
            reventado = True
        assert reventado, (
            "la guarda siguió tan tranquila con config sin CORREO_CUENTAS: "
            "eso es estar verde sin haber mirado nada")
    finally:
        config.CORREO_CUENTAS = guardado


def test_getattr_se_reconoce_por_el_objeto_y_no_por_como_se_llame():
    """EL TERCER AGUJERO de este archivo, medido el 6-sep-2026.

    `getattr` se reconocía comparando el TEXTO del identificador, en dos sitios
    a la vez —el motivo (c) y `es_espacio`—, así que ponerle otro nombre apagaba
    las dos comprobaciones con UNA sola línea. Metido dentro de
    `captura/consumos.py` —un archivo que YA EXISTÍA, para que el rojo no
    pudiera venir de haber agregado un archivo— la suite daba 25 passed, y la
    función devolvía la lista cruda entera con el buzón `reporte_a: 0` y sus
    credenciales dentro.

    Ahora la pregunta no es cómo se llama el identificador sino A QUÉ OBJETO
    resuelve, que es lo mismo que ya se hacía con el módulo `config`. Por eso un
    alias que nadie previó cae igual: no hay que reconocer el nombre nuevo, hay
    que resolver el objeto, y `ga` resuelve al mismo objeto que `getattr`.
    """
    permitidos = _atributos_que_config_ofrece()
    for nombre in ("getattr con otro nombre, atado en una asignación",
                   "getattr con otro nombre, traído por un import",
                   "getattr con otro nombre, sacado de una tupla",
                   "getattr con otro nombre, devuelto por una función",
                   "getattr escrito por su módulo"):
        motivos = _infracciones(_ESQUIVES[nombre], permitidos)
        assert motivos, (
            f"«{nombre}» pasó limpio; es la lista cruda entera, con buzón "
            "marcado y contraseñas, devuelta por un archivo de producción")

    # Y el corazón del asunto: el MISMO código con `getattr` escrito y con
    # `getattr` renombrado tiene que dar el mismo veredicto. Si un día vuelven a
    # compararse textos, esto se pone rojo y aquéllas de arriba también.
    literal = _infracciones("""
import sys


def cuentas():
    modulo = getattr(sys, "mod" + "ules")["config"]
    return getattr(modulo, "CORREO_" + "CUENTAS")
""", permitidos)
    alias = _infracciones(_ESQUIVES[
        "getattr con otro nombre, atado en una asignación"], permitidos)
    assert len(alias) >= len(literal), (
        f"con `getattr` escrito caen {len(literal)} motivos y renombrándolo "
        f"solo {len(alias)}: el nombre todavía cambia el veredicto.\n"
        f"  literal: {literal}\n  alias:   {alias}")

    # LOS TRES MOTIVOS, EXIGIDOS UNO POR UNO, y ésta es la parte que costó.
    #
    # El alias cae por tres caminos independientes, y al medirlo con mutaciones
    # el 6-sep-2026 resultó que se tapaban entre sí: apagando CUALQUIERA de los
    # tres, las 30 pruebas seguían en verde porque los otros dos sostenían el
    # rojo. Una pieza que nadie puede demostrar que hace algo es una pieza que
    # mañana se borra sin que se entere nadie — y entonces quedan dos, después
    # una, y después ninguna. Así que se exigen los tres por separado.
    def hay(trozo, motivos):
        return any(trozo in m for m in motivos)

    assert hay("pide el atributo CORREO_CUENTAS por", alias), (
        f"nadie vio que `ga(modulo, ...)` estaba pidiendo {_PROHIBIDO}: la "
        f"llamada dejó de resolverse al objeto `getattr`. Motivos: {alias}")
    assert hay("sin llamarlo", alias), (
        f"nadie vio que se estaba nombrando a getattr sin llamarlo. Ése es EL "
        f"FONDO, y es lo único que agarra un alias fabricado de una forma que "
        f"no se puede seguir —una tupla, una clausura—. Motivos: {alias}")
    assert hay("de un espacio de nombres", alias), (
        f"nadie vio que de `ga(sys, 'modules')` sale un espacio del que puede "
        f"salir un módulo: `es_espacio` volvió a dar False para una llamada a "
        f"un nombre que el archivo ata. Motivos: {alias}")

    # Y el residuo, donde el FONDO es lo ÚNICO que queda: el alias nace dentro
    # de una tupla, así que ninguna resolución de asignaciones lo alcanza y el
    # motivo de arriba no puede dispararse.
    tupla = _infracciones(
        _ESQUIVES["getattr con otro nombre, sacado de una tupla"], permitidos)
    assert hay("sin llamarlo", tupla), (
        f"un alias fabricado dentro de una tupla se le escapó al fondo: "
        f"{tupla}")
    # Y acá `ga` no resuelve a nada —sale de un subíndice—, así que la única
    # forma de ver que de `ga(sys, "modules")` sale un espacio es la regla
    # general: de una llamada a la que se le pasa un espacio, sale un espacio.
    # Antes esa regla solo valía si el nombre llamado NO estaba atado por el
    # archivo, o sea que `ga = ...` la apagaba.
    assert hay("de un espacio de nombres", tupla), (
        f"de una llamada a la que se le pasa `sys` dejó de salir un espacio "
        f"cuando el nombre llamado lo ata el propio archivo: {tupla}")


def test_convertir_texto_en_codigo_es_rojo_sin_mirar_lo_que_lleva_dentro():
    """La segunda mitad del agujero, y por qué acá no se analiza el string.

    `eval("config.CORREO_CUENTAS")` dejaba `test_nadie_lee_la_lista_cruda` en
    VERDE, igual que `exec`. Y no tiene arreglo por el lado de mirar el texto:
    el código que va a correr NO EXISTE hasta que corre, así que
    `eval("config." + parte_que_viene_de_la_base)` no se puede analizar ni en
    principio. Perseguirlo es una carrera sin fondo.

    El fondo es que usarlas sea rojo por sí solo, sin mirar qué llevan dentro.
    Y hoy eso cuesta CERO: esta misma prueba cuenta los usos que hay en los
    archivos vigilados en vez de afirmar el número.
    """
    permitidos = _atributos_que_config_ofrece()
    for fuente in ('def f(t):\n    return eval(t)\n',
                   'def f(t):\n    exec(t)\n',
                   'def f(t):\n    return compile(t, "x", "eval")\n',
                   'def f(n):\n    return __import__(n)\n'):
        motivos = _infracciones(fuente, permitidos)
        assert motivos, (
            f"esto pasó limpio y no se puede clasificar ni en principio:\n"
            f"{fuente}")

    # El precio de la regla, contado y no afirmado.
    usan = {}
    for py in sorted(_archivos_vigilados(RAIZ)):
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        ctx = _Contexto(arbol)
        cuales = sorted({f.__name__ for n in ast.walk(arbol)
                         if (f := ctx.llama_a(n, _TEXTO_A_CODIGO)) is not None})
        if cuales:
            usan[str(py.relative_to(RAIZ))] = cuales
    assert not usan, (
        f"la regla dejó de costar cero: estos archivos de Lucy usan eval, "
        f"exec, compile o __import__ y ahora salen rojos: {usan}. Antes de "
        "aflojar la regla hay que mirar si ese uso es legítimo")


def test_las_siete_formas_de_maquinaria_dinamica_una_por_una():
    """El veredicto MEDIDO de cada una, y la frontera dicha donde está.

    Un reporte anterior afirmó que las siete eran «rojas por sí solas». Medido
    el 6-sep-2026 sobre la guarda de entonces, cuatro no lo eran: `importlib`,
    `__import__`, `eval` y `exec` pasaban limpias. Esta prueba fija el veredicto
    de hoy para que la próxima afirmación se pueda comprobar sin creerle a
    nadie.

    Y donde dice False, no dice «se puede sacar la lista»: dice que conseguir un
    módulo NO es por sí solo la infracción. Sacarle la lista sí lo es, y eso lo
    cubren los motivos (a), (c) y (j) — ver los esquives de `importlib`.
    """
    permitidos = _atributos_que_config_ofrece()
    desnudas = {
        "importlib.import_module": (
            'import importlib\ndef f(n):\n    return importlib.import_module(n)\n',
            False),
        "__import__": ('def f(n):\n    return __import__(n)\n', True),
        "eval": ('def f(t):\n    return eval(t)\n', True),
        "exec": ('def f(t):\n    exec(t)\n', True),
        "globals": ('def f(n):\n    return globals()[n]\n', True),
        "locals": ('def f(n):\n    return locals()[n]\n', True),
        "sys.modules": (
            'import sys\ndef f(n):\n    return sys.modules[n]\n', True),
    }
    medido = {k: bool(_infracciones(src, permitidos))
              for k, (src, _) in desnudas.items()}
    esperado = {k: roja for k, (_, roja) in desnudas.items()}
    assert medido == esperado, (
        f"el veredicto de la maquinaria dinámica cambió.\n  medido:   {medido}"
        f"\n  esperado: {esperado}")

    # Y la mitad que de verdad importa: usada para LLEGAR a la lista cruda, la
    # siete son rojas. `importlib` sale de la frontera en cuanto se le pide algo.
    llegando = {
        "importlib": 'import importlib\ndef f():\n    return importlib.import_module("config").CORREO_CUENTAS\n',
        "importlib con nombre variable": 'import importlib\ndef f(n, k):\n    return vars(importlib.import_module(n))[k]\n',
        "__import__": 'def f():\n    return __import__("config").CORREO_CUENTAS\n',
        "eval": 'import config\ndef f():\n    return eval("config.CORREO_CUENTAS")\n',
        "exec": 'import config\ndef f():\n    d = {}\n    exec("x = config.CORREO_CUENTAS", {"config": config}, d)\n    return d["x"]\n',
        "globals": 'import config\ndef f():\n    return globals()["config"].CORREO_CUENTAS\n',
        "locals": 'def f():\n    import config\n    return locals()["config"].CORREO_CUENTAS\n',
        "sys.modules": 'import sys\ndef f():\n    return sys.modules["config"].CORREO_CUENTAS\n',
    }
    escapados = [k for k, src in llegando.items()
                 if not _infracciones(src, permitidos)]
    assert not escapados, (
        f"estas formas alcanzan la lista cruda y pasaron limpias: {escapados}")


def test_la_exencion_no_se_apaga_si_cambia_la_forma_de_arrancar():
    """`"python -m main"` es una forma perfectamente normal de arrancar.

    Con el filtro anterior —`t.endswith(".py")` sobre el `startCommand`— eso
    daba una lista de entradas VACÍA en silencio, el cierre de imports salía
    vacío, y con él todo lo que vive bajo `testpaths` quedaba exento aunque
    producción lo cargara. Verificado ejecutando el 6-sep-2026; hoy el
    startCommand dice `python main.py`, así que no se disparaba.

    Ahora se entienden las dos formas, y lo que NO se pueda determinar cae del
    lado rojo: sin entradas no se exenta a nadie.
    """
    import tempfile

    fuga = _ESQUIVES["el nombre escrito entero"]

    def montar(orden: str) -> tuple[list, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _repo_de_mentira(raiz)
            (raiz / "railway.json").write_text(
                json.dumps({"deploy": {"startCommand": orden}}),
                encoding="utf-8")
            # Un archivo bajo `testpaths` que producción SÍ carga. Es andamio
            # por dónde vive y NO lo es por quién lo carga: el segundo hecho es
            # el que decide, y sin saber con qué arranca no se puede comprobar.
            (raiz / "main.py").write_text(
                "import tests.compartido\n", encoding="utf-8")
            (raiz / "tests" / "compartido.py").write_text(fuga,
                                                          encoding="utf-8")
            return (_entradas_de_produccion(raiz),
                    _quienes_leen_la_lista_cruda(raiz))

    entradas, culpables = montar("python main.py")
    assert [p.name for p in entradas] == ["main.py"], (
        f"no se entendió el arranque con el archivo suelto: {entradas}")
    assert "tests/compartido.py" in culpables, (
        f"un archivo bajo testpaths que producción importa quedó exento: "
        f"{culpables}")

    entradas, culpables = montar("python -m main")
    assert [p.name for p in entradas] == ["main.py"], (
        f"`python -m main` no se entendió como arranque: {entradas}. Ésa es "
        "la forma que dejaba la lista de entradas vacía en silencio")
    assert "tests/compartido.py" in culpables, (
        f"con `-m` la exención volvió a tragarse un archivo de producción: "
        f"{culpables}")

    # Y lo que NO se puede determinar cae del lado rojo: sin entradas no se
    # exenta a nadie, ni siquiera al andamio de pruebas de verdad.
    entradas, culpables = montar("gunicorn web.app:app")
    assert entradas == [], (
        f"un arranque que no nombra ningún módulo de Lucy dio entradas: "
        f"{entradas}")
    assert "tests/compartido.py" in culpables, (
        f"sin saber con qué arranca producción se siguió exentando: "
        f"{culpables}. Lo que no se puede determinar va del lado rojo")


def test_el_ayudante_generico_de_getattr_es_rojo_y_cuanto_cuesta_hoy():
    """UN LÍMITE DECLARADO, con su número, en vez de una excepción silenciosa.

    Un ayudante genérico `def leer(mod, nombre): return getattr(mod, nombre)`,
    usado como `leer(config, "TZ")` —que nunca toca la lista prohibida— sale
    ROJO. Es coherente con «lo que no se puede clasificar es rojo»: la guarda no
    puede saber qué nombre le van a pasar, y por ese mismo agujero se saca
    `CORREO_CUENTAS`. Pero una guarda que rojea a quien hace lo correcto se
    apaga sola, así que el precio se cuenta en vez de suponerse.

    LO MEDIDO, el 6-sep-2026: CERO sitios de Lucy escriben ese patrón. Los 7
    `getattr` que hay en los 37 archivos vigilados piden todos un nombre que sí
    se puede enumerar (4 en main.py, 2 en cerebro/interpretar.py, 1 en db/db.py
    recorriendo una tupla de dos literales). O sea, el límite no le cuesta nada
    a nadie hoy, y el día que le cueste será esta prueba la que lo diga.
    """
    permitidos = _atributos_que_config_ofrece()
    assert _infracciones(
        "def leer(mod, nombre):\n    return getattr(mod, nombre)\n",
        permitidos), (
        "el ayudante genérico pasó limpio; por ese mismo agujero se saca "
        "CORREO_CUENTAS y la guarda no tiene cómo distinguirlo")

    cuantos, opacos = 0, []
    for py in sorted(_archivos_vigilados(RAIZ)):
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        ctx = _Contexto(arbol)
        for n in ast.walk(arbol):
            if ctx.llama_a(n, _ATRIBUTO_POR_NOMBRE) is None or len(n.args) < 2:
                continue
            cuantos += 1
            if _cadenas(n.args[1], ctx.ambitos) is None:
                opacos.append(f"{py.relative_to(RAIZ)}:{n.lineno}")
    assert cuantos == 7, (
        f"los archivos vigilados tienen {cuantos} llamadas a getattr y el "
        "6-sep-2026 eran 7. Si el número se movió, hay que volver a mirar "
        "cuánto cuesta este límite antes de darlo por gratis")
    assert not opacos, (
        f"el límite dejó de costar cero: {opacos} piden un nombre de atributo "
        "que no se puede enumerar. Eso no es un falso positivo que ignorar — o "
        "el sitio se reescribe con el nombre a la vista, o el límite se "
        "renegocia con Tiziano, pero no se afloja la aserción")


def test_la_puerta_es_de_verdad_el_unico_sitio_donde_se_decide():
    """Que el filtro esté escrito UNA vez. Si mañana alguien copia el
    `if destino:` dentro de un camino, esto no lo ve — pero la de arriba sí ve
    al que se saltee la puerta, y entre las dos no queda hueco."""
    import inspect
    fuente = inspect.getsource(config.cuentas_de_correo)
    assert "destino_del_reporte(c)" in fuente, (
        "la vista 'mostrar' dejó de decidirse con destino_del_reporte")


if __name__ == "__main__":
    fallidos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {nombre}")
        except AssertionError as e:
            fallidos += 1
            print(f"FALLA {nombre}\n      {e}")
    print(f"\n{fallidos} fallos")
    sys.exit(1 if fallidos else 0)
