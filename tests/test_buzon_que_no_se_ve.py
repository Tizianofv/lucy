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
para qué lo quieren. `config.cuentas_de_correo(para=...)` es el único sitio del
que sale un buzón con credenciales, y `test_nadie_lee_la_lista_cruda` —que
recorre los .py que hay EN DISCO, no una lista escrita acá, y los lee con
`ast.parse` en vez de buscarles texto— se pone rojo si algún archivo puede
alcanzar la lista cruda por su cuenta, lo escriba como lo escriba. Un camino
nuevo no puede olvidarse de filtrar: no puede conseguir el buzón. El porqué de
leer el árbol y no el texto está entero arriba de la guarda, más abajo.

Y LA OTRA MITAD, que tiene que seguir igual: BARRER NO ES MOSTRAR. El buzón
marcado se sigue leyendo entero para sacar sus movimientos bancarios. Si estas
pruebas pasaran dejando de barrerlo, el arreglo estaría mal.

Correr:  python3 tests/test_buzon_que_no_se_ve.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types
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
# DE DÓNDE SACA AHORA LO QUE COMPARA. De dos sitios, los dos reales:
#
#   1. `ast.parse` del archivo. El árbol ve igual `from config import
#      CORREO_CUENTAS as CUENTAS` que `config.CORREO_CUENTAS`, porque el nombre
#      está en el nodo y no en cómo se escribió. Los alias, los espacios, los
#      paréntesis y los comentarios desaparecen antes de que se compare nada.
#   2. `vars(config)`. Los atributos PERMITIDOS son los nombres públicos que el
#      módulo config de verdad tiene hoy, menos el prohibido. Nadie los teclea
#      acá: si config gana un nombre, entra solo; si pierde el prohibido, la
#      guarda revienta en vez de quedarse verde vigilando un fantasma.
#
# Y EL CRITERIO ES «LO QUE NO SÉ CUENTA COMO ROJO», aplicado al trayecto
# entero y no a un tramo. El módulo `config` solo se puede usar para UNA cosa:
# leer uno de sus atributos permitidos, escrito como atributo. Cualquier otro
# uso del objeto módulo —pasarlo, guardarlo, `getattr`-earlo, `vars`-earlo,
# abrirle el `__dict__`— no se puede clasificar, y lo que no se puede
# clasificar es rojo. Por eso una forma que nadie previó cae del lado rojo: no
# hay que reconocerla, hay que fallar en reconocerla.

_PROHIBIDO = "CORREO_CUENTAS"

# Maquinaria que fabrica un objeto módulo en tiempo de ejecución. No es una
# lista de trucos: es el juego completo de puertas que el lenguaje ofrece para
# conseguir un módulo sin nombrarlo, y da igual qué string se les pase. El
# código de Lucy no importa módulos a mano en ningún sitio (medido: cero usos
# fuera de tests/ y conftest.py), así que exigirlo no cuesta nada y cierra el
# hueco que el árbol de sintaxis solo no puede ver.
_FABRICAS_DE_MODULOS = frozenset({
    "importlib", "__import__", "eval", "exec", "globals", "locals"})


def _atributos_que_config_ofrece() -> set[str]:
    """Los nombres públicos que `config` DE VERDAD tiene, menos el prohibido."""
    publicos = {n for n in vars(config) if not n.startswith("_")}
    assert _PROHIBIDO in publicos, (
        f"config ya no define {_PROHIBIDO}. Esta guarda quedaría vigilando un "
        "nombre que no existe, o sea verde sin haber mirado nada: si la lista "
        "cruda cambió de nombre, hay que cambiárselo también acá")
    return publicos - {_PROHIBIDO}


def _es_el_modulo_config(nodo, nombres_locales: set[str]) -> bool:
    """¿Esta expresión ES el módulo config?

    Dos formas, y las dos se ven en el árbol: un nombre que un `import` ató al
    módulo (con alias o sin él), o el atributo `.config` de cualquier otra cosa
    — que es como se llega a config a través de un módulo que ya lo importó.
    """
    if isinstance(nodo, ast.Name) and nodo.id in nombres_locales:
        return True
    return isinstance(nodo, ast.Attribute) and nodo.attr == "config"


def _nombres_locales_del_modulo_config(arbol) -> set[str]:
    """Con qué nombre conoce ESTE archivo al módulo config."""
    nombres: set[str] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "config" or a.name.startswith("config."):
                    nombres.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "config":
                    nombres.add(a.asname or a.name)
    return nombres


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

    locales = _nombres_locales_del_modulo_config(arbol)
    malas: list[str] = []

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

        # (c) Maquinaria que fabrica módulos: no se puede saber qué consigue.
        if isinstance(n, ast.Name) and n.id in _FABRICAS_DE_MODULOS:
            malas.append(f"línea {n.lineno}: usa {n.id}, que puede devolver "
                         "cualquier módulo y no se puede clasificar")
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            raiz_mod = (n.module or "").split(".")[0] if isinstance(
                n, ast.ImportFrom) else ""
            modulos = [raiz_mod] if raiz_mod else [
                a.name.split(".")[0] for a in n.names]
            for m in modulos:
                if m in _FABRICAS_DE_MODULOS:
                    malas.append(f"línea {n.lineno}: importa {m}, que fabrica "
                                 "objetos módulo que no se pueden clasificar")
        if isinstance(n, ast.Attribute) and n.attr == "modules":
            malas.append(f"línea {n.lineno}: toca la tabla de módulos; de ahí "
                         "sale config sin nombrarlo")

        # (d) El objeto módulo usado para CUALQUIER otra cosa que no sea leer
        #     uno de sus atributos permitidos. Acá caen `getattr(config, ...)`,
        #     `vars(config)`, `config.__dict__`, `otro = config` y todo lo que
        #     todavía no se le ocurrió a nadie.
        if _es_el_modulo_config(n, locales):
            padre = getattr(n, "_padre", None)
            bien = (isinstance(padre, ast.Attribute)
                    and padre.value is n
                    and padre.attr in permitidos)
            if not bien:
                como = (f".{padre.attr}" if isinstance(padre, ast.Attribute)
                        else type(padre).__name__ if padre else "suelto")
                malas.append(
                    f"línea {n.lineno}: usa el módulo config de una forma que "
                    f"no puedo clasificar ({como}); lo único permitido es "
                    "leerle un atributo suyo que no sea la lista cruda")

    return sorted(set(malas))


def _quienes_leen_la_lista_cruda(raiz: Path) -> dict[str, list[str]]:
    """Recorre los .py que hay EN DISCO bajo `raiz` y los clasifica.

    Las exclusiones son por ROL, no por nombre:
      · `config.py` es donde la lista se define y donde vive la puerta.
      · `tests/` le escribe encima para montar buzones de mentira; es lo que
        hace este mismo archivo unas funciones más arriba.
      · `conftest.py` es andamio de pruebas del mismo rol que `tests/`: no
        corre en producción y necesita `sys.modules` para aislar las pruebas
        entre sí.
    """
    permitidos = _atributos_que_config_ofrece()
    culpables: dict[str, list[str]] = {}
    for py in sorted(raiz.rglob("*.py")):
        rel = py.relative_to(raiz)
        if rel.parts[0] == "tests" or rel.name in ("config.py", "conftest.py"):
            continue
        if any(p in (".venv", "venv", "__pycache__") for p in rel.parts):
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


# Ocho caminos nuevos, todos haciendo LO MISMO: devolver la lista cruda. Los
# tres primeros son los que la guarda vieja midió el 5-sep-2026 (uno rojo, dos
# verdes). Los cinco de abajo son las formas siguientes, las que se le habrían
# escapado a un parche que solo añadiera las dos primeras al patrón.
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
}

# Y lo que TIENE que seguir en verde: los tres usos legítimos que hay hoy en el
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
}


def test_la_guarda_muerde_los_ocho_caminos_a_la_lista_cruda():
    """Las ocho formas de arriba tienen que poner la guarda ROJA.

    Se corre el archivo de verdad —`_quienes_leen_la_lista_cruda`, el mismo que
    usa la prueba de arriba— sobre una carpeta temporal FUERA del repositorio,
    con un .py por forma. No se escribe nada dentro del repo.
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
    # cualquiera de las ocho formas, puesto en una carpeta como la del repo,
    # tiene que aparecer en los culpables.
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        (raiz / "tests").mkdir()
        for i, fuente in enumerate(_ESQUIVES.values()):
            (raiz / f"camino_nuevo_{i}.py").write_text(fuente, encoding="utf-8")
        culpables = _quienes_leen_la_lista_cruda(raiz)
    assert len(culpables) == len(_ESQUIVES), (
        f"el recorrido del disco solo señaló {sorted(culpables)} de "
        f"{len(_ESQUIVES)} archivos culpables")


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
