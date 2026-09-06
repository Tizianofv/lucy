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
lista cruda por ahí. Un camino nuevo escrito por DESCUIDO no puede olvidarse de
filtrar: no puede conseguir el buzón. Hasta dónde llega eso —y hasta dónde no—
está dicho entero en la frontera de aquí abajo, y el porqué de leer el árbol y
no el texto está arriba de la guarda, más abajo.

LA FRONTERA DE ESTA GUARDA, corregida DOS VECES el 6-sep-2026 porque las dos
anteriores prometían de más:

    La guarda ve lo que un archivo NOMBRA, sobre algo que ella puede RESOLVER,
    y escrito EN LA MISMA OPERACIÓN con la que lo toca. UN SALTO, ni uno más.

    No ve lo que un archivo BUSCA mientras corre; no ve lo que le pide a un
    objeto que no pudo resolver; y no ve lo que nombra un salto más allá —sobre
    lo que devolvió esa operación, o dentro de otra función.

EL FONDO, QUE ES LA TERCERA CONDICIÓN Y LA QUE SE PUSO HOY. Abierto el espacio
de nombres de algo —`vars(m)`, `globals()`, `locals()`, `m.__dict__`—, la guarda
mira los nombres que viajan a la operación que TOCA ESE ESPACIO, y ni uno más
allá. El motivo (j) explica el mecanismo; el fondo se fija corriendo, en las
dos direcciones, en `test_el_fondo_del_espacio_abierto_es_un_salto`.

Y ANTES DE PONERLE EL FONDO SE MIDIÓ QUE HACÍA FALTA. Seis formas, pasadas una
por una por la guarda y ejecutadas después contra el `config` de verdad:

    vars(mod)["CORREO_CUENTAS"]                        → rojo   un salto
    dict(vars(mod))["CORREO_CUENTAS"]                  → VERDE  dos saltos
    vars(mod).copy()["CORREO_CUENTAS"]                 → VERDE  dos saltos
    mod.__dict__.copy().get("CORREO_CUENTAS")          → VERDE  dos saltos
    d = {}; d.update(vars(mod)); d["CORREO_CUENTAS"]   → VERDE  dos saltos
    operator.itemgetter("CORREO_CUENTAS")(vars(mod))   → VERDE  dos saltos
    _leer(vars(mod))   # el nombre, en la otra función → VERDE  dos saltos

Las seis verdes devuelven el objeto `config.CORREO_CUENTAS` en persona —la
lista cruda, sin filtrar por `reporte_a`—, y sus gemelas de un salto menos
también. Están escritas enteras en `_FUERA_DE_LA_FRONTERA`, y las ejecuta las
doce, comparando por identidad y sin imprimir nada,
`test_el_fondo_del_espacio_abierto_es_un_salto`.

Las seis nombran `CORREO_CUENTAS` con todas sus letras sobre un módulo que la
guarda SÍ resuelve, o sea que caían DENTRO de lo que la frontera anterior
prometía. Perseguir el segundo salto obliga a perseguir el tercero —guardar el
espacio en un atributo, pasarlo por dos funciones— y eso es análisis de flujo de
datos, que no tiene fondo. Así que en vez de la séptima forma se le puso fondo
al barrido y las seis se declararon, con su código exacto y con su gemela de un
salto menos, en `_FUERA_DE_LA_FRONTERA`.

QUÉ TENÍA DE FALSO LA PRIMERA FRONTERA, y por qué se escribe así el arreglo.
Decía «la guarda ve lo que un archivo NOMBRA», a secas, con este criterio:
tacha `CORREO_CUENTAS` del texto y, si el código deja de encontrar su objetivo,
cae. La sala midió que no era cierto, con la diferencia más pequeña que hay:

    vars(m)["CORREO_CUENTAS"]      → 1 failed   la agarra
    vars(m).get("CORREO_CUENTAS")  → 1 passed   INVISIBLE

Corchetes o punto. El nombre estaba escrito entero en las dos, así que por el
propio criterio las dos tenían que caer. La causa era que el motivo (j) miraba
LA FORMA DE LA OPERACIÓN —solo `ast.Subscript`— en vez del espacio de nombres;
está arreglado y medido, y el motivo (j) explica de dónde sale ahora la
pregunta. Lo que quedó fuera y no se pudo cerrar son las otras dos mitades de
la frontera de arriba —lo que se le pide a un objeto sin resolver, y lo que se
nombra un salto más allá—, y está declarado con su código exacto en
`_FUERA_DE_LA_FRONTERA`, entrada por entrada.

Y UN DEFECTO DEL MISMO DÍA QUE SÍ SE ARREGLÓ, porque no era ofuscación sino un
descuido corriente y tiraba hacia VERDE: un parámetro llamado `getattr` en
cualquier función del archivo apagaba el motivo (c) para el archivo ENTERO
—`_Contexto.atados` es plano y no tiene ámbitos—, así que `getattr(mod, n)`
dejaba de verse. No se le dieron ámbitos a `atados`: se aplicó la doctrina de
este archivo, «si el nombre está pisado no puedo resolverlo, y lo que no puedo
clasificar es rojo». Es el motivo (k); vale para TODOS los nombres peligrosos
—que salen de los objetos, no de una lista tecleada— y para las formas de pisar
uno que `_Contexto.atados` recoge: un parámetro, un `def` propio, una
asignación, un alias de import, el destino de un `for`. La que no recoge —el
`case` de un `match`— sale roja igual, por el otro lado: no verla significa que
la guarda sigue resolviendo el nombre al builtin, y entonces muerde el motivo
(c). Todo eso, y lo que cuesta, se mide corriendo en
`test_pisar_un_nombre_peligroso_es_rojo_y_cuanto_cuesta_hoy`.

EL CRITERIO PARA SABER DE QUÉ LADO CAE ALGO NUEVO, sin tener que probarlo.
Tacha del texto del archivo tres cosas: el nombre `config`, el nombre
`CORREO_CUENTAS`, y los nombres de los siete objetos de `_PELIGROSOS`. Tacha
también los que estén partidos en trozos de string y los alias que se puedan
seguir hasta un `import` o una asignación, porque la guarda los reconstruye.

  · Si tachando eso el código DEJA DE ENCONTRAR su objetivo, Y el objeto al que
    se lo pide llega por un camino que la guarda resuelve —un `import`, un
    espacio de nombres abierto con `vars`/`globals`/`locals`/`__dict__`, o una
    función que ella resuelve al objeto—, Y el nombre está escrito en LA MISMA
    OPERACIÓN con la que se toca ese objeto o su espacio → la guarda lo atrapa.
    Da igual cómo esté escrito: no hay que reconocer la forma, hay que fallar
    en reconocerla.
  · Si lo SIGUE ENCONTRANDO —porque no dice qué quiere, sino que recorre una
    colección en tiempo de ejecución y elige comparando valores— → cae fuera, y
    la guarda no lo va a ver nunca. No hay parche que lo cambie: en el árbol de
    sintaxis no hay nada que resolver.
  · Y si lo NOMBRA pero se lo pide a un objeto que la guarda no puede resolver
    —`m.__getattribute__("CORREO_CUENTAS")` sobre un `m` que vino de la primera
    rama— → también cae fuera. Para tener ese `m` hay que haber conseguido el
    módulo por una puerta que ya está declarada fuera; en cuanto se consigue
    por una que la guarda ve, se pone rojo. Las dos direcciones están medidas.
  · Y si lo NOMBRA sobre algo que la guarda sí resuelve, pero UN SALTO MÁS
    ALLÁ —el nombre se le pide a lo que devolvió la operación anterior
    (`dict(vars(m))[...]`, `vars(m).copy()[...]`, `m.__dict__.copy().get(...)`,
    un `update` a otro diccionario), o está escrito dentro de otra función a la
    que se le entregó el espacio— → también cae fuera. Para saber de qué lado
    está algo no hace falta probarlo: mira si el nombre y el `vars`/`__dict__`
    se tocan en la MISMA operación. Si sí, la guarda lo ve, sea cual sea el
    método o la función; si entre los dos hay una operación más, no. Asignarle
    el espacio a un nombre tal cual (`d = vars(m)`, `e = d`) NO es un salto: es
    el mismo objeto, y la guarda lo sigue. Las seis formas están escritas
    enteras en `_FUERA_DE_LA_FRONTERA`, cada una con su gemela de un salto
    menos, que sale roja.
  · Y UNA QUE NO DEPENDE DE TACHAR NADA: si el archivo le pone su propio
    significado a uno de esos nombres —un parámetro, un `def`, una asignación,
    un alias de import, el destino de un `for`— la guarda ya no puede resolver
    ese nombre en NINGUNA parte del archivo, y todo el archivo sale rojo. Es el
    motivo (k).

POR QUÉ SE CORTÓ LA PERSECUCIÓN AHÍ, y no en la forma siguiente. Son DOS los
espacios sin fondo con los que se topa esta guarda, y los dos se cierran igual
—poniéndoles un fondo dicho en una línea—, nunca enumerando formas:

  · las maneras de BUSCAR un objeto en tiempo de ejecución sin nombrarlo. El
    fondo es el criterio de arriba: si no escribe el nombre, no hay nada en el
    árbol que mirar.
  · las maneras de MOVER un valor de un sitio a otro antes de pedirle el
    nombre —copiarlo, volcarlo en otro diccionario, pasarlo por un parámetro,
    guardarlo en un atributo—. Ése es el fondo del salto, y seguirlo es
    análisis de flujo de datos.

Lo que esta guarda sí puede prometer es lo otro: que un camino nuevo escrito
POR DESCUIDO —el que se olvida de pedir el buzón por la puerta— cae. Para eso
funciona, y eso está medido forma por forma en `_ESQUIVES`. Nadie escribe por
descuido un bucle sobre los módulos cargados comparando un nombre partido en
dos, ni copia el espacio de nombres de un módulo antes de leerlo.

Y LA DIFERENCIA ENTRE «UNA FORMA MÁS» Y «LA PROMESA SIN CUMPLIR», que es lo
que decidió qué se arreglaba el 6-sep-2026 y qué no. La pregunta ante la
siguiente forma no es «¿es rebuscada?»: es si CAE DENTRO DE LO QUE LA FRONTERA
PROMETE, y la frontera son las tres condiciones de arriba a la vez.

  · `vars(m).get(...)` nombraba la lista cruda, sobre un objeto resoluble, en
    la misma operación. Caía dentro de lo prometido. Eso no se declara, se
    arregla — y se arregló.
  · Un parámetro llamado `getattr` apagaba el motivo (c) del archivo entero.
    Caía dentro de lo prometido, y además era un descuido corriente y tiraba
    hacia verde. Se arregló: motivo (k).
  · Recorrer los módulos cargados comparando un nombre partido NO nombra nada;
    `m.__getattribute__(...)` sobre un `m` sin resolver no se puede resolver; y
    `dict(vars(m))[...]` nombra pero un salto más allá. Las tres caen FUERA de
    lo prometido, y por eso se declaran en vez de perseguirse.

Una forma nueva se arregla si cumple las tres condiciones y aun así se escapa.
Si falla alguna, se declara — y la declaración es la que hace que la frontera
siga siendo cierta.

LO QUE QUEDA FUERA, DECLARADO CON SU CÓDIGO EXACTO, para que nadie lo
redescubra creyendo que es un agujero nuevo: `_FUERA_DE_LA_FRONTERA`, medido
entrada por entrada en `test_la_frontera_declarada_esta_medida_forma_por_forma`.
Los números —cuántas formas atrapa y cuántas están declaradas fuera— los cuenta
esa prueba al correr; acá no va ninguna cifra, que ya se separó de la realidad
dos veces en este mismo archivo.

Y DOS PRECIOS, que no son agujeros sino lo contrario. Los dos son lo que cuesta
«lo que no se puede clasificar es rojo», y los dos se cuentan al correr en vez
de afirmarse acá:

  · un ayudante genérico `def leer(mod, nombre): return getattr(mod, nombre)`
    sale ROJO aunque nunca toque la lista prohibida. Lo cuenta
    `test_el_ayudante_generico_de_getattr_es_rojo_y_cuanto_cuesta_hoy`.
  · un archivo que use uno de los nombres peligrosos para otra cosa —el caso
    corriente es un parámetro llamado `vars` o `getattr`— sale ROJO entero, y
    el arreglo es renombrar la variable. Lo cuenta
    `test_pisar_un_nombre_peligroso_es_rojo_y_cuanto_cuesta_hoy`.

Los dos exigen que su precio sea CERO sobre los archivos vigilados de hoy, y si
deja de serlo se ponen rojos en vez de aflojarse.

LA SALIDA DE VERDAD, QUE NO SE HIZO HOY Y HAY QUE DEJAR ESCRITA. Nada de esto
haría falta si `config.CORREO_CUENTAS` no existiera como atributo alcanzable.
Si las credenciales se leyeran DENTRO de `cuentas_de_correo(para=...)` y no
quedaran guardadas en ningún atributo del módulo, no habría nada que encontrar
por mucho que se recorran los módulos cargados: la fuga de
`_FUERA_DE_LA_FRONTERA` volvería con las manos vacías. Es la Regla 11 —borrar el
caso en vez de manejarlo— y es el arreglo definitivo.

Una vuelta anterior lo midió y lo descartó por un motivo real, y sigue en pie:
`config.CORREO_CUENTAS = ...` se asigna 15 veces en 5 archivos de prueba
(medido el 6-sep-2026 con `grep -rn "config\\.CORREO_CUENTAS *=" tests/`), y esos
5 archivos colectan 100 pruebas (`pytest --collect-only`). Ésa es hoy la única
forma que tiene la suite de instalar buzones falsos, y con la lista metida en un
cierre el `conftest` no la restaura. O sea: el día que alguien rehaga el
aislamiento de la suite, ahí está el arreglo que borra este archivo entero.

Y NO VIGILA el otro sitio del que hoy salen buzones con credenciales, que no es
un agujero sino una decisión: `tools/descubrir_bancos.py::_cuentas()` lee
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
import importlib
import importlib.machinery
import importlib.util
import inspect
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

# LOS CONTADORES, y por qué llevan una marca. Varias pruebas de acá cuentan lo
# que un límite CUESTA hoy —cuántos getattr hay, cuántos archivos se vigilan—
# para que «no cuesta nada» sea un número y no un adjetivo. Está bien que
# existan; lo que no puede pasar es que su rojo se confunda con el rojo que
# importa.
#
# PASÓ TRES VECES EL 6-sep-2026, y la última fue la peor: la sala metió en el
# repo una fuga que devolvía las credenciales del buzón marcado, y el ÚNICO
# rojo de la suite fue «tienen 8 llamadas a getattr y el 6-sep eran 7». Un
# contador que salta primero hace creer que la guarda vio algo cuando no vio
# nada, y de paso esconde que la prueba de fondo está en verde.
#
# Dos reglas, y las dos se comprueban corriendo en
# `test_los_contadores_no_pueden_tapar_el_rojo_que_importa`:
#
#   1. El mensaje de un contador EMPIEZA con esta marca, así que su rojo dice
#      de sí mismo que es un contador.
#   2. Un contador nunca es la PRIMERA aserción de su prueba. La aserción de
#      fondo va antes, siempre, para que el contador no pueda abortar la prueba
#      antes de que lo que importa se haya comprobado.
_MARCA_CONTADOR = "CONTADOR (no es una fuga): "

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

# Las tres funciones que DEVUELVEN EL ESPACIO DE NOMBRES de algo. También son
# OBJETOS, así que `v = vars` no apaga nada. La cuarta forma de abrir un espacio
# no es una función sino el atributo `__dict__`, que es el nombre que Python le
# pone al espacio de nombres de un objeto y que no se puede cambiar por otro.
# Ver `_Contexto.abre_un_espacio` y `_infracciones`, motivo (j).
_ABREN_UN_ESPACIO = (builtins.vars, builtins.globals, builtins.locals)

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
# nombrarlo no se reconoce como tal. Son AL MENOS TRES formas; el 6-sep-2026
# había escrita una, después dos, y un testigo encontró la tercera:
#
#     builtins.__dict__["get" + "attr"]                  # pide `import builtins`
#     sys.modules["builtins"].__dict__["get" + "attr"]   # no pide nada
#     vars(builtins)["get" + "attr"]                     # pide `import builtins`
#
# La tercera se le escapa por el mismo sitio que las otras dos: el motivo (h)
# mira nodos `ast.Name` y `ast.Attribute`, y el resultado de un `Subscript` no
# es ninguno de los dos. Y se dice «AL MENOS TRES» a propósito: éste es el
# espacio sin fondo del que habla la cabecera, así que la cuenta no se cierra —
# lo que se cierra es el criterio con el que se decide de qué lado cae una
# cuarta, y ése está arriba, en una línea.
#
# Las tres, sueltas, salen VERDES — medido, no supuesto, en
# `test_la_frontera_declarada_esta_medida_forma_por_forma`. Y las tres dejan de
# importar en cuanto se usan para algo: para sacar la lista cruda hace falta
# además el módulo `config`, y conseguirlo cae por los motivos (d), (e) y (f).
# Esa segunda mitad también está medida allí, con las tres formas.
_PELIGROSOS = _ATRIBUTO_POR_NOMBRE + _TEXTO_A_CODIGO

# Y CÓMO SE LLAMAN HOY ESOS OBJETOS, sacado de los objetos y no tecleado. Hace
# falta para el motivo (k): si un archivo ATA uno de estos nombres —un `def`, un
# parámetro, una asignación, un import— entonces la guarda ya no puede resolver
# ese nombre al builtin, y «no puedo resolver» es rojo.
#
# EL DEFECTO QUE LO MOTIVA, medido el 6-sep-2026 con tres casos:
#
#     def _otra(getattr):        # un parámetro llamado igual, en OTRA función
#         return getattr
#     def robar(mod, n):
#         return getattr(mod, n)
#
#     sin nada que pise el nombre               → 1 failed   la agarra
#     con un PARÁMETRO llamado getattr          → 1 passed   INVISIBLE
#     con un parámetro de otro nombre (control) → 1 failed   la agarra
#
# `_Contexto.atados` es PLANO —no tiene ámbitos—, así que un parámetro llamado
# `getattr` en cualquier función del archivo apaga `resuelve` para el archivo
# ENTERO: el nombre deja de resolver al builtin, `llama_a` no reconoce nada y el
# motivo (c) se va en silencio. No es ofuscación, es un descuido corriente —usar
# el nombre de un builtin como parámetro se escribe sin pensar—, y tira hacia
# VERDE, que es el lado que no avisa.
#
# NO SE ARREGLA DÁNDOLE ÁMBITOS A `atados`: eso es rehacer el Tramo 2 entero. Se
# arregla con la doctrina del propio archivo: si el nombre está pisado, la
# guarda no puede resolver con seguridad, y lo que no se puede clasificar es
# rojo. Lo que cuesta —cuántos archivos vigilados atan hoy alguno de estos
# nombres— no va escrito acá: lo cuenta y lo exige al correr
# `test_pisar_un_nombre_peligroso_es_rojo_y_cuanto_cuesta_hoy`.
_NOMBRES_PELIGROSOS = frozenset(
    f.__name__ for f in _PELIGROSOS + _ABREN_UN_ESPACIO)


def _cual_peligroso(objetos) -> object | None:
    """El objeto peligroso que hay en este conjunto, comparado por IDENTIDAD."""
    for f in _PELIGROSOS:
        if any(o is f for o in objetos):
            return f
    return None


# ── Tramo 2-ter: qué llamadas pueden DEVOLVER un módulo ───────────────────
#
# EL CUARTO AGUJERO DE ESTE ARCHIVO, medido el 6-sep-2026, y de la misma
# especie que los tres anteriores — con una vuelta de tuerca: acá la regla
# buena YA ESTABA ESCRITA, aplicada por cinco motivos, y a uno se le olvidó.
#
# De los seis motivos que buscan `config`, cinco tratan «no puedo enumerarlo»
# como ROJO y lo dicen: «si es None: rojo». El motivo (d) preguntaba
# `if trae and _MODULO_CONFIG in trae`, o sea que un nombre que no se podía
# resolver daba `None`, la condición salía False en silencio, y no pasaba nada:
#
#     import importlib as _il
#     def _cual():
#         import os
#         return os.environ["MODULO_OBJETIVO_SALA"]
#     def _fuga_importlib():
#         mod = _il.import_module(_cual())
#         return vars(mod)["CORREO_CUENTAS"]
#
# Puesto DENTRO de `captura/consumos.py` —un archivo que ya existía, para que
# el rojo no pudiera venir del conteo de archivos— la guarda daba 30 passed. El
# MISMO código con `import_module("config")` daba 2 failed. Ejecutado con
# credenciales centinela, la función devolvía la lista cruda entera, con el
# buzón `reporte_a: 0` dentro. Lo único que cambiaba era si el nombre del
# módulo era un literal o se armaba al vuelo.
#
# POR QUÉ EL ARREGLO NO ES «ARGUMENTO NO ENUMERABLE ⇒ ROJO» A SECAS. La mayoría
# de las llamadas de cualquier archivo reciben algo que no es un string
# literal, así que la regla a secas rojea a casi todo el repo. Una guarda que
# rojea a quien hace lo correcto se apaga sola: ese no es el arreglo, es el
# mismo defecto por el otro lado. Acá NO va la cifra —se separaría de la
# realidad como cualquier número tecleado—: la mide al correr
# `test_cuanto_costaria_la_regla_a_secas_y_cuanto_cuesta_la_de_verdad`.
#
# LA PUERTA ESTÁ EN EL CALLEE, NO EN EL ARGUMENTO. `f(x)` con `x` desconocido
# no dice nada; `import_module(x)` con `x` desconocido devuelve CUALQUIER
# módulo. Así que la pregunta es a qué objeto resuelve la función llamada, que
# es exactamente lo que este archivo ya hace con `getattr` y su familia.
#
# LA LISTA SE SACA DEL SISTEMA DE IMPORTS, Y CON FONDO. Un
# `_TEXTO_A_MODULO = (importlib.import_module,)` escrito a mano es la especie de
# lista que ya falló tres veces acá: le faltó `sys`, le faltó `getattr` por
# alias, le faltó `conftest.py`. Así que se barre `importlib`.
#
# PERO UN BARRIDO SIN FONDO ES LA MISMA LISTA QUE SE SEPARA DE LA REALIDAD, solo
# que automática, y este archivo se lo comió en el primer intento. Barrer «todo
# submódulo de `importlib` que esté en `sys.modules`» daba 28 objetos corriendo
# el archivo suelto y 120 corriendo bajo pytest, que carga `importlib.metadata`
# y `importlib.resources`. Entre esos 120 entraban `str`, `Mapping`,
# `suppress`, `cast` y `filterfalse`: RE-EXPORTS de módulos que no tienen nada
# que ver, colados por ser atributos públicos. Con `str` adentro el motivo (d)
# rojeaba decenas de llamadas legítimas en un tercio de los archivos vigilados
# —la cifra exacta cambiaba de corrida a corrida, que es justo el problema—, o
# sea que el veredicto de la guarda pasaba a depender de qué había importado
# quien la corriera.
#
# EL FONDO, en una línea: se barren los módulos del sistema de imports que ESTE
# archivo importa arriba —ni uno más, así que no depende de quién cargó qué— y
# de ellos se guarda solo lo que el sistema de imports DEFINE, no lo que
# re-exporta. «Definir» se decide con `__module__`, y las raíces se leen de
# objetos reales (`ModuleSpec.__module__` es `_frozen_importlib`), no tecleadas.

_MODULOS_DEL_SISTEMA_DE_IMPORTS = (importlib, importlib.util,
                                   importlib.machinery)

# De dónde sale «lo que el sistema de imports define». Las dos raíces de
# implementación se leen de objetos suyos, no se escriben a mano.
_RAICES_DE_IMPORTS = (
    importlib.__name__,                                   # "importlib"
    importlib.machinery.ModuleSpec.__module__,            # "_frozen_importlib"
    importlib.machinery.SourceFileLoader.__module__,      # "..._external"
)


def _funciones_que_traen_modulos(modulos=_MODULOS_DEL_SISTEMA_DE_IMPORTS
                                 ) -> tuple:
    """Los callables que el sistema de imports DEFINE, sacados de él.

    Son OBJETOS, no textos: da igual con qué identificador se llamen, igual que
    con `getattr`. `builtins.__import__` se suma aparte porque es la puerta de
    abajo de todas ellas y vive en `builtins`, no en `importlib`.

    `modulos` se puede pasar para poder MEDIR el filtro con un módulo de
    mentira, en vez de tener que creerse que filtra.
    """
    salida: list = []

    def meter(obj) -> None:
        if not any(obj is x for x in salida):
            salida.append(obj)

    for mod in modulos:
        for atrib in dir(mod):
            if atrib.startswith("_"):
                continue
            try:
                obj = getattr(mod, atrib)
            except Exception:
                continue
            if not callable(obj) or isinstance(obj, types.ModuleType):
                continue
            duena = getattr(obj, "__module__", None)
            if not isinstance(duena, str):
                continue
            if any(duena == r or duena.startswith(r + ".")
                   for r in _RAICES_DE_IMPORTS):
                meter(obj)
    meter(builtins.__import__)
    return tuple(salida)


_TRAEN_MODULOS = _funciones_que_traen_modulos()

# El barrido tiene que haber encontrado la función que motivó todo esto. Si
# `importlib` cambia de forma y el barrido se queda vacío, esto se pone rojo en
# vez de dejar el motivo (d) mirando una tupla sin nada dentro — que sería
# verde sin haber comprobado nada.
assert any(o is importlib.import_module for o in _TRAEN_MODULOS), (
    "el barrido de `importlib` no encontró `import_module`; el motivo (d) "
    "quedaría sin puerta y la fuga que lo motivó volvería a ser invisible")
assert any(o is builtins.__import__ for o in _TRAEN_MODULOS)


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
        self.espacios: set[str] = set()        # nombres que SON un espacio ya abierto
        self._leer_ataduras(arbol)
        self._propagar(arbol)
        self._resolver_nombres(arbol)
        self._propagar_espacios(arbol)

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

    def _propagar_espacios(self, arbol) -> None:
        """`d = vars(m)` deja a `d` SIENDO el espacio de nombres de algo.

        Sin esto, guardar el espacio en una variable apaga el motivo (j) entero
        con una línea, que es exactamente la especie de fuga que este archivo ya
        cerró tres veces por el lado de `getattr`. Se repite hasta que deje de
        crecer, para seguir `d = vars(m)`, `e = d`.
        """
        for _ in range(10):
            antes = len(self.espacios)
            for n in ast.walk(arbol):
                destino = valor = None
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    destino, valor = n.targets[0], n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    destino, valor = n.target, n.value
                elif isinstance(n, (ast.For, ast.AsyncFor)):
                    destino, valor = n.target, n.iter
                if isinstance(destino, ast.Name) and valor is not None \
                        and self.abre_un_espacio(valor):
                    self.espacios.add(destino.id)
            if len(self.espacios) == antes:
                return

    def abre_un_espacio(self, nodo) -> bool:
        """¿Esta expresión ES el espacio de nombres de algo?

        Tres formas, y ninguna se reconoce por el texto:

          · una llamada que RESUELVE AL OBJETO `vars`, `globals` o `locals`
            (`_ABREN_UN_ESPACIO`), igual que `llama_a` hace con `getattr`;
          · el atributo `__dict__`, que es como Python llama al espacio de
            nombres de un objeto y no admite otro nombre;
          · un nombre al que se le asignó cualquiera de las dos, que es lo que
            propaga `_propagar_espacios`.
        """
        if isinstance(nodo, ast.Name) and nodo.id in self.espacios:
            return True
        if isinstance(nodo, ast.Attribute) and nodo.attr == "__dict__":
            return True
        return self.llama_a(nodo, _ABREN_UN_ESPACIO) is not None

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

    # (k) UN NOMBRE PELIGROSO PISADO EN CUALQUIER PARTE DEL ARCHIVO. `atados`
    #     es plano —no tiene ámbitos—, así que un `def _otra(getattr)` en una
    #     función cualquiera hace que `resuelve` deje de resolver `getattr` al
    #     builtin en TODO el archivo, y el motivo (c) se va en silencio. Medido
    #     el 6-sep-2026: el mismo `getattr(mod, n)` daba rojo sin el parámetro y
    #     VERDE con él.
    #
    #     No se arregla dándole ámbitos a `atados` —eso es rehacer el Tramo 2—
    #     sino con la doctrina de este archivo: si el nombre está pisado, la
    #     guarda no puede resolverlo con seguridad, y lo que no se puede
    #     clasificar es rojo. Los nombres salen de los OBJETOS peligrosos, no
    #     de una lista tecleada. Y va antes que todo lo demás porque, cuando
    #     pasa, todo lo demás está decidido sobre nombres mal resueltos.
    pisados = sorted(ctx.atados & _NOMBRES_PELIGROSOS)
    if pisados:
        malas.append(
            f"línea 1: este archivo le pone su propio significado a {pisados}, "
            "que son los nombres de los objetos peligrosos; con el nombre "
            "pisado no puedo resolver a qué función se llama en ninguna parte "
            "del archivo, y sin poder resolverlo no hay nada que clasificar")

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
        if pide is not None and len(n.args) < 2:
            # `getattr(*par)` y `getattr(mod, **kw)` no tienen un segundo
            # argumento que mirar: el nombre pedido está dentro de algo que se
            # desparrama al llamar. No poder ni localizarlo es un caso más de
            # «no lo sé», y valía verde porque este `if` pedía `>= 2` y se iba
            # en silencio.
            malas.append(
                f"línea {n.lineno}: {ast.unparse(n.func)} es {pide.__name__} y "
                "se le pasan los argumentos desparramados; no puedo ni "
                "localizar qué nombre de atributo pide, así que no hay nada "
                "que clasificar")
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

        # (j) EL ESPACIO DE NOMBRES DE ALGO, ABIERTO. `vars(x)`, `globals()`,
        #     `locals()` y `x.__dict__` son la tercera forma de sacarle un
        #     atributo a un módulo, después del punto y de `getattr`.
        #
        #     DE DÓNDE SALE «esto le está pidiendo una clave a un espacio de
        #     nombres», que es la pregunta entera. Hasta el 6-sep-2026 la
        #     respuesta era LA FORMA DE LA OPERACIÓN: solo se miraba un
        #     `ast.Subscript`. Así que `vars(m)["CORREO_CUENTAS"]` salía rojo y
        #     `vars(m).get("CORREO_CUENTAS")` salía VERDE devolviendo el objeto
        #     `config.CORREO_CUENTAS` en persona, con la única diferencia de
        #     cambiar corchetes por punto. Lo mismo `m.__dict__.get(...)` y
        #     `dict.get(vars(m), ...)`.
        #
        #     Y la respuesta NO PUEDE SER EL NOMBRE DEL MÉTODO. Añadir `get` a
        #     una lista deja fuera `pop`, `setdefault`, `__getitem__` llamado a
        #     mano, `operator.getitem`, `dict.get` desatado y el que se invente
        #     mañana: es la misma lista tecleada que este archivo ya quitó de
        #     `getattr` y de las fábricas de módulos.
        #
        #     LA RESPUESTA, y el fondo: no se mira la operación, se mira EL
        #     ESPACIO. `abre_un_espacio` resuelve `vars`/`globals`/`locals` AL
        #     OBJETO —así `v = vars; v(m)` cuenta igual— y sigue `__dict__`, que
        #     es el nombre que Python le da al espacio de nombres. Abierto el
        #     espacio, TODO NOMBRE QUE VIAJE a la operación que se le hace tiene
        #     que ser enumerable y no prohibido, sea cual sea esa operación. Un
        #     método que nadie previó cae del lado correcto porque la guarda
        #     nunca le pregunta cómo se llama: le pregunta qué le entregan.
        #
        #     Y SU FONDO, dicho en una línea porque el espacio de formas de
        #     mover un valor tampoco tiene fondo: UN SALTO. Se miran los nombres
        #     que viajan a la operación que toca EL ESPACIO —las tres ramas de
        #     `viajan`, que son las tres formas que tiene Python de juntar un
        #     objeto y una clave en una expresión—, y ni uno más allá. Lo que
        #     esa operación DEVUELVE ya no es el espacio: `dict(vars(m))`,
        #     `vars(m).copy()` y `d = {}; d.update(vars(m))` son diccionarios
        #     nuevos, y un espacio entregado a otra función llega allí como un
        #     parámetro, que es un nombre atado. El espacio sí se sigue de
        #     nombre en nombre mientras se le asigne TAL CUAL —eso lo hace
        #     `_propagar_espacios`— porque ahí sigue siendo el mismo objeto.
        #
        #     Y lo que se queda fuera, a propósito y por lo mismo: una operación
        #     que NO LLEVA NINGÚN NOMBRE —`vars(m).values()`, `vars(m).items()`—
        #     no nombra nada, así que no hay nada que clasificar. Ésa y las seis
        #     del salto están declaradas en `_FUERA_DE_LA_FRONTERA` con su
        #     código exacto, y el fondo se fija corriendo, en las dos
        #     direcciones, en `test_el_fondo_del_espacio_abierto_es_un_salto`.
        if ctx.abre_un_espacio(n):
            padre = getattr(n, "_padre", None)
            abuelo = getattr(padre, "_padre", None)
            if isinstance(padre, ast.Subscript) and padre.value is n:
                # `vars(m)[k]`
                viajan = [padre.slice]
            elif isinstance(padre, ast.Attribute) and padre.value is n and \
                    isinstance(abuelo, ast.Call) and abuelo.func is padre:
                # `vars(m).loquesea(k, ...)` — el método da igual
                viajan = list(abuelo.args) + [k.value for k in abuelo.keywords]
            elif isinstance(padre, ast.Call) and any(a is n for a in padre.args):
                # `dict.get(vars(m), k)`, `operator.getitem(vars(m), k)`
                viajan = [a for a in padre.args if a is not n] + \
                    [k.value for k in padre.keywords]
            else:
                viajan = []
            prohibidas = {_PROHIBIDO, _MODULO_CONFIG}
            for viaja in viajan:
                claves = pedidos(viaja)
                if claves is None:
                    malas.append(
                        f"línea {n.lineno}: le pide al espacio de nombres de "
                        "algo una clave que no puedo enumerar; de ahí sale "
                        "cualquier atributo de cualquier módulo, la lista "
                        "cruda incluida")
                elif claves & prohibidas:
                    cual = sorted(claves & prohibidas)
                    malas.append(
                        f"línea {n.lineno}: le pide {cual} al espacio de "
                        "nombres de algo, que es exactamente la lista cruda o "
                        "el módulo que la tiene")

        # (d) Pedir por su nombre el MÓDULO config: `sys.modules["config"]`,
        #     `importlib.import_module("config")`, `globals()["config"]`. El
        #     nombre sale de `config.__name__`, no de acá.
        #
        #     Y si la llamada es a una función que TRAE MÓDULOS POR SU NOMBRE,
        #     un argumento que no se puede enumerar es rojo, igual que en (c),
        #     (e), (f) y (j): sin saber qué nombre se pide, no se sabe si el
        #     módulo que sale es `config`. La puerta está en el callee y no en
        #     el argumento porque «argumento no enumerable ⇒ rojo» a secas
        #     rojea 1.828 de las 3.198 llamadas de los archivos vigilados; ver
        #     `_funciones_que_traen_modulos`, donde está la medida entera.
        if isinstance(n, ast.Call):
            trae_modulos = ctx.llama_a(n, _TRAEN_MODULOS)
            for arg in list(n.args) + [k.value for k in n.keywords]:
                trae = pedidos(arg)
                if trae is None:
                    if trae_modulos is not None:
                        malas.append(
                            f"línea {n.lineno}: le pasa a "
                            f"{trae_modulos.__name__}, que trae módulos por su "
                            "nombre, un nombre que no puedo enumerar; de ahí "
                            f"puede salir cualquier módulo, {_MODULO_CONFIG!r} "
                            "incluido")
                elif _MODULO_CONFIG in trae:
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

    OJO CON ESTA FUNCIÓN: mudar la lista a pytest.ini no la dejó de ser una
    lista TECLEADA, solo la mudó de fichero — y el 6-sep-2026 falló como falla
    cualquier lista tecleada. Decía `.venv` (con punto) y el árbol desde el que
    se publica tiene una carpeta `venv` (sin punto). Por eso hoy esto NO es el
    fondo del barrido, sino un cinturón: el fondo está en `_py_en_disco`, que
    le pregunta a cada carpeta qué es en vez de mirarle el nombre.
    """
    return set(_pytest_ini(raiz, "norecursedirs"))


def _es_entorno_virtual(carpeta: Path) -> bool:
    """¿Esta carpeta es un entorno virtual? Se lo pregunta a la carpeta.

    Un entorno virtual no es un NOMBRE: PEP 405 dice que es una carpeta con un
    `pyvenv.cfg` dentro, y ése es el archivo que el propio Python busca para
    saber que está corriendo dentro de uno. Así que `venv`, `.venv`, `env`,
    `virtualenv` o `entorno` dan todos el mismo resultado y no hay ninguna
    lista que mantener ni que se pueda quedar vieja.

    POR QUÉ EXISTE, medido el 6-sep-2026: `norecursedirs` decía `.venv` y el
    árbol desde el que se publica tiene una carpeta `venv`, así que la guarda
    se metía dentro y parseaba los paquetes instalados. Reproducido sobre una
    copia del árbol sacada con `git archive` y un venv poblado dentro (9664
    .py bajo `venv/`, 66 fuera): UNA sola pasada de
    `_quienes_leen_la_lista_cruda` tardó 175,13 s y contó 9701 archivos
    vigilados en vez de 37. La suite entera llama a esto decenas de veces.

    Y NO ES UNA INVENCIÓN DE ESTA GUARDA: es literalmente la comprobación que
    hace el propio pytest para no coleccionar dentro de un entorno virtual
    (`_pytest.main._in_venv`, leída del pytest 9.1.1 de este entorno el
    6-sep-2026): `pyvenv.cfg`, más `conda-meta/history` para los entornos de
    conda, que todavía no traen `pyvenv.cfg`. Se copian las DOS, porque copiar
    media comprobación es cómo se hacen los agujeros.
    """
    try:
        return ((carpeta / "pyvenv.cfg").is_file()
                or (carpeta / "conda-meta" / "history").is_file())
    except OSError:
        return False


# La firma que la Cache Directory Tagging Specification exige en la primera
# línea de un `CACHEDIR.TAG`. Es una constante del estándar, no una elección de
# este repo: por eso se compara el CONTENIDO y no el nombre del archivo.
_FIRMA_DE_CACHE = b"Signature: 8a477f597d28d172789f06886806bc55"


def _es_cache_de_herramienta(carpeta: Path) -> bool:
    """¿Esta carpeta es el caché de una herramienta? También se lo pregunta.

    Misma idea que `_es_entorno_virtual`, con el otro estándar que existe para
    esto: una carpeta de caché se marca poniéndole dentro un `CACHEDIR.TAG`
    que empieza por una firma fija. pytest lo escribe —comprobado en este repo
    el 6-sep-2026 en `.pytest_cache/CACHEDIR.TAG`— y uv lo escribe en su caché.
    El nombre de la carpeta vuelve a dar igual.
    """
    tag = carpeta / "CACHEDIR.TAG"
    try:
        if not tag.is_file():
            return False
        with tag.open("rb") as f:
            return f.read(len(_FIRMA_DE_CACHE)) == _FIRMA_DE_CACHE
    except OSError:
        return False


def _py_en_disco(raiz: Path) -> list[Path]:
    """Los `.py` que hay bajo `raiz`, sin entrar en lo que no es del repo.

    EL FONDO DEL BARRIDO, dicho en una línea: se entra en toda carpeta menos
    en las que cumplen uno de estos tres hechos.

      · tiene dentro un `pyvenv.cfg`   → es un entorno virtual (PEP 405)
      · tiene dentro un `CACHEDIR.TAG` → es un caché de herramienta (la spec)
      · `norecursedirs` de pytest.ini la nombra → pytest tampoco entra ahí

    Los dos primeros se le preguntan A LA CARPETA y por eso no se pueden
    quedar viejos: cubren `venv`, `.venv`, `env` y cualquier nombre que alguien
    invente mañana, sin que nadie teclee nada. El tercero es el que YA se quedó
    viejo, y se queda solo como cinturón.

    Se usa `os.walk` y no `rglob` por dos razones: poda la rama entera
    (`subs[:] = ...`), o sea que ni siquiera se enumeran los miles de archivos
    de un venv; y no sigue enlaces simbólicos a carpetas, que `rglob` sí
    seguía. Hoy no cambia nada: los 66 .py que hay en disco son los 66 que git
    tiene registrados, y no hay ningún enlace.

    UNA DIFERENCIA DECLARADA con pytest, para que no sorprenda: pytest lee
    `norecursedirs` como PATRONES (`fnmatch`) y acá se leen como NOMBRES
    EXACTOS. Un `.*` o un `venv*` escritos ahí saltarían carpetas para pytest
    y no para esta guarda. Se deja así a propósito —la lectura estricta mira
    MÁS archivos, no menos, que es el lado seguro del error—, y se puede dejar
    porque el fondo del barrido ya no depende de esa línea.

    FRONTERA DECLARADA: una carpeta de código ajeno que no sea un venv, no
    lleve `CACHEDIR.TAG` y no esté nombrada en `norecursedirs` SÍ se recorre.
    El caso concreto que se conoce es `.uv-python` —los intérpretes que baja
    uv—: hoy la tapa `norecursedirs`, y si alguien la saca de ahí el barrido
    vuelve a crecer. No está comprobado desde acá si uv le pone `CACHEDIR.TAG`
    a esa carpeta; la del caché sí lo lleva según su documentación.
    """
    nombres_fuera = _carpetas_que_no_son_del_repo(raiz)
    encontrados: list[Path] = []
    for carpeta, subs, ficheros in os.walk(raiz):
        aqui = Path(carpeta)
        subs[:] = [d for d in subs
                   if d not in nombres_fuera
                   and not _es_entorno_virtual(aqui / d)
                   and not _es_cache_de_herramienta(aqui / d)]
        encontrados.extend(aqui / f for f in ficheros if f.endswith(".py"))
    return sorted(encontrados)


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
    # Los .py bajo `testpaths` salen del MISMO barrido podado que usa todo lo
    # demás (`_py_en_disco`), no de un `rglob` aparte: si mañana alguien deja
    # un entorno virtual dentro de `tests/`, dos barridos distintos darían dos
    # conjuntos distintos y las cifras dejarían de hablar de lo mismo.
    carpetas = _testpaths(raiz)
    candidatos: set[Path] = {
        p.resolve() for p in _py_en_disco(raiz)
        if any(c == p or c in p.parents for c in carpetas)}
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
    exentos = _archivos_exentos(raiz)
    return [p for p in _py_en_disco(raiz) if p.resolve() not in exentos]


def _quienes_leen_la_lista_cruda(raiz: Path) -> dict[str, list[str]]:
    """Recorre los .py que hay EN DISCO bajo `raiz` y los clasifica.

    Ningún archivo se nombra acá. Hasta dónde llega el barrido lo decide
    `_py_en_disco`, que le pregunta a cada carpeta si es un entorno virtual
    (`pyvenv.cfg`, PEP 405) o un caché (`CACHEDIR.TAG`) en vez de mirarle el
    nombre. Quién queda exento sale de `_archivos_exentos`, que lo deriva de
    `config.__file__`, de `testpaths` y del cierre de imports del
    `startCommand` de railway.json. Lo que no encaje en esos hechos se queda
    DENTRO de la vigilancia, que es el lado seguro del error.

    ACÁ NO VA NINGUNA CIFRA. La que había —«66 archivos .py, 29 exentos, 37
    vigilados»— decía «medido sobre este repo» y era cierta en un árbol de
    trabajo y falsa en el árbol desde el que se publica, donde hay un venv
    dentro y son miles. Quien las mide, y contra el árbol en el que de verdad
    esté corriendo, es
    `test_cuantos_falsos_positivos_hay_hoy_sobre_los_archivos_reales`.
    """
    permitidos = _atributos_que_config_ofrece()
    exentos = _archivos_exentos(raiz)
    culpables: dict[str, list[str]] = {}
    for py in _py_en_disco(raiz):
        rel = py.relative_to(raiz)
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
    # EL AGUJERO DEL 6-sep-2026, y por qué las dos de arriba no lo tapaban: las
    # dos piden una CLAVE no enumerable, y el motivo (j) cubría eso. Ésta pide
    # la clave con un literal —`"CORREO_CUENTAS"` escrito entero— y arma el
    # NOMBRE DEL MÓDULO al vuelo. Así quedaba fuera de las dos ramas a la vez:
    # (j) veía una clave enumerable y se callaba, y (d) veía un nombre de módulo
    # no enumerable y también se callaba. Metida en `captura/consumos.py` —un
    # archivo que ya existía, para que el rojo no pudiera venir del conteo—:
    # 30 passed. La misma con `import_module("config")`: 2 failed. Ejecutada con
    # credenciales centinela devolvía la lista cruda entera, buzón
    # `reporte_a: 0` incluido.
    "importlib con el nombre al vuelo y la clave escrita entera": """
import importlib as _il


def _cual():
    import os
    return os.environ["MODULO_OBJETIVO"]


def cuentas():
    mod = _il.import_module(_cual())
    return vars(mod)["CORREO_CUENTAS"]
""",
    # Y la mitad de arriba por separado, para que ninguna de las dos ramas
    # nuevas se pueda borrar sin que algo se ponga rojo. Ésta no importa nada:
    # le pide la lista cruda por su nombre al espacio de nombres de lo que le
    # pasen. Medido el 6-sep-2026 sobre la guarda de antes: VERDE.
    "la lista cruda pedida por su nombre al espacio de nombres de algo": """
def cuentas(mod):
    return vars(mod)["CORREO_CUENTAS"]
""",
    "la lista cruda pedida por su nombre al __dict__ de algo": """
def cuentas(mod):
    return mod.__dict__["CORREO_CUENTAS"]
""",
    # LAS MISMAS DOS DE ARRIBA CON UN PUNTO EN VEZ DE CORCHETES. Medido por la
    # sala el 6-sep-2026 sobre la guarda anterior, metiendo las dos formas en un
    # archivo que YA EXISTÍA: `vars(m)["CORREO_CUENTAS"]` daba 1 failed y
    # `vars(m).get("CORREO_CUENTAS")` daba 1 passed devolviendo el objeto
    # `config.CORREO_CUENTAS` en persona. El motivo (j) solo miraba
    # `ast.Subscript`, y un `.get(...)` es un `ast.Call`.
    #
    # Las cinco están acá y no una sola a propósito: si la respuesta a esto
    # hubiera sido añadir `get` a una lista de métodos, las otras cuatro
    # seguirían verdes. Ahora ninguna de las cinco menciona el método en la
    # guarda — cae el espacio de nombres, no la operación.
    "la lista cruda pedida con .get al espacio de nombres de algo": """
def cuentas(mod):
    return vars(mod).get("CORREO_CUENTAS")
""",
    "la lista cruda pedida con .get al __dict__ de algo": """
def cuentas(mod):
    return mod.__dict__.get("CORREO_CUENTAS")
""",
    "la lista cruda pedida con dict.get desatado": """
def cuentas(mod):
    return dict.get(vars(mod), "CORREO_CUENTAS")
""",
    "la lista cruda sacada con un método que nadie previó": """
def cuentas(mod):
    return vars(mod).setdefault("CORREO_CUENTAS", [])
""",
    "el espacio de nombres guardado en una variable primero": """
def cuentas(mod):
    espacio = vars(mod)
    return espacio.get("CORREO_CUENTAS")
""",
    # `globals()` y `locals()` son la misma puerta que `vars()`: los tres son
    # objetos de builtins que devuelven un espacio de nombres. Con corchetes
    # caen por el motivo (e), así que estas dos están escritas con `.get` —
    # medido el 6-sep-2026: quitando `globals` y `locals` de `_ABREN_UN_ESPACIO`
    # las 38 pruebas seguían en VERDE, o sea que esa mitad del arreglo no la
    # comprobaba nadie.
    "la lista cruda pedida con .get al espacio global": """
def cuentas():
    return globals().get("CORREO_CUENTAS")
""",
    "la lista cruda pedida con .get al espacio local": """
def cuentas(CORREO_CUENTAS=None):
    # se llama como `cuentas(**vars(config))`, desde otro archivo
    return locals().get("CORREO_CUENTAS")
""",
    # Y la tercera del mismo día: `getattr` con los argumentos desparramados.
    # El motivo (c) pedía `len(n.args) >= 2` para poder mirar el segundo, así
    # que una llamada sin segundo argumento POSICIONAL se iba en silencio — que
    # es justo el caso en el que menos se sabe qué se está pidiendo. Medido
    # sobre la guarda de antes: VERDE las dos.
    "getattr con los argumentos desparramados": """
def cuentas(mod, nombre):
    par = (mod, nombre)
    return getattr(*par)
""",
    "getattr con el nombre metido en kwargs": """
def cuentas(mod, nombre):
    return getattr(mod, **{"name": nombre})
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
    # Los dos motivos concretos PRIMERO: son lo que se exige. El conteo va
    # después, para que no aborte la prueba antes de decir CUÁL de los dos
    # falta, que es el dato con el que se arregla.
    assert any(_MODULO_CONFIG in m for m in motivos), (
        f"nadie vio que se estaba sacando el módulo {_MODULO_CONFIG!r} de un "
        f"espacio de nombres: {motivos}")
    assert any(_PROHIBIDO in m for m in motivos), (
        f"nadie vio que se estaba pidiendo {_PROHIBIDO}: {motivos}")
    assert len(motivos) >= 2, (
        f"{_MARCA_CONTADOR}el camino por la tabla de módulos solo cayó por "
        f"{motivos}. Los dos motivos que se exigen YA CORRIERON arriba y "
        "quedaron verdes, así que esto NO es una fuga: es que los dos se "
        "fundieron en un solo mensaje")


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


def _repo_con_carpeta_ajena(raiz: Path, nombre: str, marca: str,
                            contenido: bytes, cuantos: int = 1) -> None:
    """Un repo de mentira con una carpeta que trae `marca` dentro y `cuantos`
    archivos que SÍ leen la lista cruda, para ver si el barrido entra o no."""
    _repo_de_mentira(raiz)
    ajena = raiz / nombre
    ajena.mkdir()
    if marca:
        (ajena / marca).parent.mkdir(parents=True, exist_ok=True)
        (ajena / marca).write_bytes(contenido)
    for i in range(cuantos):
        (ajena / f"paquete{i}.py").write_text(
            _ESQUIVES["el nombre escrito entero"], encoding="utf-8")


def test_el_barrido_no_entra_en_un_entorno_virtual_se_llame_como_se_llame():
    """EL ARREGLO del 6-sep-2026, y la razón por la que no mira nombres.

    `norecursedirs` decía `.venv` (con punto) y el árbol desde el que se
    publica tiene una carpeta `venv` (sin punto). Una lista tecleada falló
    exactamente como fallan las listas tecleadas, y nadie se enteró en todo el
    día porque los árboles de trabajo no tienen esa carpeta.

    Acá se prueban CUATRO nombres, y ninguno está en el `norecursedirs` del
    repo de mentira (que dice `.venv __pycache__`): lo único que decide es si
    la carpeta tiene un `pyvenv.cfg` dentro, que es lo que PEP 405 dice que es
    un entorno virtual. El cuarto —`parece_venv`, sin `pyvenv.cfg`— comprueba
    la otra mitad: que no se pierde vigilancia sobre una carpeta normal por
    tener nombre sospechoso.
    """
    import tempfile

    veredictos = {}
    for nombre, marca in (("venv", "pyvenv.cfg"), ("env", "pyvenv.cfg"),
                          ("entorno_raro", "pyvenv.cfg"),
                          ("entorno_conda", "conda-meta/history"),
                          ("parece_venv", "")):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _repo_con_carpeta_ajena(raiz, nombre, marca, b"home = /usr\n")
            assert nombre not in _carpetas_que_no_son_del_repo(raiz), (
                f"{nombre} está en norecursedirs; entonces esta prueba no "
                "mide el pyvenv.cfg sino la lista tecleada")
            veredictos[nombre] = f"{nombre}/paquete0.py" in \
                _quienes_leen_la_lista_cruda(raiz)

    assert veredictos == {"venv": False, "env": False, "entorno_raro": False,
                          "entorno_conda": False, "parece_venv": True}, (
        f"el barrido no está decidiendo por el pyvenv.cfg: {veredictos}. "
        "Con pyvenv.cfg dentro la carpeta es un entorno virtual (PEP 405) y "
        "no se recorre; sin él es código normal del repo y se mira")


def test_un_venv_dentro_de_testpaths_tampoco_entra_por_la_puerta_de_atras():
    """El mismo barrido para las dos mitades, no solo para la vigilada.

    `_archivos_exentos` también recorre disco: junta los .py que hay bajo
    `testpaths` para exentar el andamio de pruebas. Si ese recorrido fuera un
    `rglob` aparte, un entorno virtual dejado dentro de `tests/` se colaría por
    ahí —no como vigilado, sino como EXENTO— y las cifras de
    `test_cuantos_falsos_positivos_hay_hoy_sobre_los_archivos_reales`
    empezarían a contar miles de archivos que nadie miró.

    Es la Regla de los hermanos: el criterio bueno aplicado a un tramo y no al
    de al lado. Acá se exige que los dos recorridos usen el mismo fondo.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        _repo_de_mentira(raiz)
        sin_venv = len(_archivos_exentos(raiz))
        (raiz / "tests" / "venv").mkdir()
        (raiz / "tests" / "venv" / "pyvenv.cfg").write_text(
            "home = /usr\n", encoding="utf-8")
        (raiz / "tests" / "venv" / "paq.py").write_text(
            _ESQUIVES["el nombre escrito entero"], encoding="utf-8")
        con_venv = len(_archivos_exentos(raiz))
        vigilados = [str(p.relative_to(raiz)) for p in _archivos_vigilados(raiz)]

    assert con_venv == sin_venv, (
        f"un venv dentro de tests/ metió {con_venv - sin_venv} archivos en la "
        "lista de EXENTOS: el recorrido de la exención no usa el mismo fondo "
        "que el de la vigilancia")
    assert "tests/venv/paq.py" not in vigilados


def test_el_barrido_no_entra_en_un_cache_marcado_con_cachedir_tag():
    """La otra carpeta ajena que se reconoce sin nombrarla.

    Un caché se marca con un `CACHEDIR.TAG` que empieza por una firma fija —la
    Cache Directory Tagging Specification—, y pytest y uv lo escriben. Se
    comprueba que decide la FIRMA y no el nombre del archivo: una carpeta con
    un `CACHEDIR.TAG` que no lleva la firma se sigue mirando.
    """
    import tempfile

    veredictos = {}
    for nombre, contenido in (("un_cache", _FIRMA_DE_CACHE + b"\n"),
                              ("cache_falso", b"esto no es la firma\n")):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _repo_con_carpeta_ajena(raiz, nombre, "CACHEDIR.TAG", contenido)
            veredictos[nombre] = f"{nombre}/paquete0.py" in \
                _quienes_leen_la_lista_cruda(raiz)

    assert veredictos == {"un_cache": False, "cache_falso": True}, (
        f"el barrido no está mirando la firma del CACHEDIR.TAG: {veredictos}")


def test_el_fondo_del_barrido_no_depende_de_que_pytest_ini_este_bien():
    """Que el arreglo NO se apoye en el fichero que se quedó viejo.

    `norecursedirs` se queda como cinturón, no como fondo. Acá el repo de
    mentira no tiene `norecursedirs` NINGUNO —o sea el peor caso: alguien
    borra la línea, o la escribe mal como el 6-sep-2026— y aun así el barrido
    se detiene en el entorno virtual, porque el hecho que lo detiene se lo
    pregunta a la carpeta.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        _repo_con_carpeta_ajena(raiz, "venv", "pyvenv.cfg", b"home = /usr\n")
        (raiz / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\n", encoding="utf-8")
        assert _carpetas_que_no_son_del_repo(raiz) == set(), (
            "esta prueba solo vale si no queda ninguna carpeta saltada por "
            "nombre; si no, no mide el fondo sino el cinturón")
        culpables = _quienes_leen_la_lista_cruda(raiz)

    assert "venv/paquete0.py" not in culpables, (
        f"sin norecursedirs el barrido se metió en el entorno virtual: "
        f"{culpables}. El fondo del barrido está dependiendo de pytest.ini")
    assert "captura/correo.py" not in culpables


def test_un_venv_dentro_del_arbol_no_mete_ni_un_archivo_en_el_barrido():
    """El número, no el adjetivo: con un venv dentro se miran los MISMOS
    archivos que sin él, uno por uno y no solo la cuenta.

    Medido el 6-sep-2026 sobre una copia del árbol sacada con `git archive`
    con un venv poblado dentro (9665 .py bajo `venv/`, 66 fuera): antes, una
    sola pasada de `_quienes_leen_la_lista_cruda` tardó 175,13 s y contó 9701
    vigilados; después, 0,81 s y 37, y la suite entera pasó de no terminar a
    6,43 s.

    Lo que esta prueba NO mide: si la rama se poda (`os.walk` con
    `subs[:] = ...`) o si se enumera y se descarta al final. Las dos dan la
    misma lista; la diferencia son los 0,4 s de enumerar 9.6 mil archivos, no
    los 175 s de parsearlos. Se poda porque sale gratis, no porque esto lo
    exija.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        _repo_de_mentira(raiz)
        sin_venv = [str(p.relative_to(raiz)) for p in _py_en_disco(raiz)]

        venv = raiz / "venv"
        (venv / "lib" / "site-packages" / "hondo").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        for i in range(300):
            (venv / "lib" / "site-packages" / "hondo" / f"m{i}.py").write_text(
                _ESQUIVES["el nombre escrito entero"], encoding="utf-8")
        con_venv = [str(p.relative_to(raiz)) for p in _py_en_disco(raiz)]

    assert con_venv == sin_venv, (
        f"el venv metió {len(con_venv) - len(sin_venv)} archivos en el "
        "barrido; la rama no se está podando")


def test_cuantos_falsos_positivos_hay_hoy_sobre_los_archivos_reales():
    """El número, no el adjetivo.

    `test_nadie_lee_la_lista_cruda` dice que no hay ninguno; esto dice sobre
    CUÁNTOS archivos se midió, para que "cero falsos positivos" signifique algo
    y para que se note si mañana la guarda deja de mirar medio repo.
    """
    # PRIMERO lo que importa. Antes el contador de abajo iba delante, así que
    # añadir un archivo al repo abortaba la prueba y esto no llegaba a correr.
    assert not _quienes_leen_la_lista_cruda(RAIZ), (
        "hay falsos positivos sobre los archivos reales de hoy")

    todos = _py_en_disco(RAIZ)
    mirados = _archivos_vigilados(RAIZ)
    medido = {"en disco": len(todos), "exentos": len(todos) - len(mirados),
              "vigilados": len(mirados)}
    assert medido == {"en disco": 66, "exentos": 29, "vigilados": 37}, (
        f"{_MARCA_CONTADOR}el reparto de archivos cambió: {medido}, y el "
        "6-sep-2026 era {'en disco': 66, 'exentos': 29, 'vigilados': 37}. La "
        "aserción de fondo —cero archivos alcanzan la lista cruda— YA CORRIÓ "
        "arriba y quedó verde, así que esto NO es una fuga. Si los vigilados "
        "bajaron, algo se está saltando de más y «cero falsos positivos» dejó "
        "de significar lo que decía; si subieron, hay código nuevo que mirar")


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

    Y CAMBIÓ UNA, el 6-sep-2026, y a más rojo: `importlib.import_module(n)` con
    `n` sin enumerar era False y ahora es True. Era el agujero — el motivo (d)
    trataba «no puedo enumerar el nombre» como verde, y por ahí salía la lista
    cruda entera sin que nada se pusiera rojo. Ver `_funciones_que_traen_modulos`.

    Y donde dice False, no dice «se puede sacar la lista»: dice que conseguir un
    módulo NO es por sí solo la infracción. Sacarle la lista sí lo es, y eso lo
    cubren los motivos (a), (c) y (j) — ver los esquives de `importlib`.
    """
    permitidos = _atributos_que_config_ofrece()
    desnudas = {
        "importlib.import_module": (
            'import importlib\ndef f(n):\n    return importlib.import_module(n)\n',
            True),
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


def _arbol_de_este_archivo():
    """El árbol del ARCHIVO ENTERO, con cada nodo apuntando a su padre.

    Antes esto parseaba solo `inspect.getsource(_infracciones)`, y ahí estaba el
    agujero: un motivo escrito en una FUNCIÓN AUXILIAR de fuera, llamada desde
    el bucle de `_infracciones` con una línea, quedaba fuera del texto mirado y
    la prueba de la serie seguía verde. Medido el 6-sep-2026: un motivo con el
    mismo defecto de las seis vueltas, puesto en un ayudante, daba 35 passed.

    El archivo se lee de `sys.modules[__name__]`, no de una ruta escrita acá.

    UN solo árbol por llamada, y quien lo use tiene que usar SIEMPRE el mismo:
    parsear dos veces da dos juegos de nodos distintos, y entonces los padres de
    uno no se encuentran en los índices del otro. Eso ya rompió esta prueba una
    vez, y de la forma peligrosa: daba rojo cuando el código estaba bien.
    """
    return _con_padres(inspect.getsource(sys.modules[__name__]))


def _con_padres(fuente: str):
    """Un árbol con cada nodo apuntando a su padre en `_p`.

    Está aparte de `_arbol_de_este_archivo` para que las pruebas puedan medir
    los cuidados sobre un texto INVENTADO. Sin eso, «este cuidado no se pasa con
    un gesto» sería una afirmación de comentario: la única forma de comprobarla
    es escribir el gesto y ver que sale rojo.
    """
    arbol = ast.parse(fuente)
    for padre in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(padre):
            hijo._p = padre                          # type: ignore[attr-defined]
    return arbol


def _alias_del_enumerador(arbol) -> set[str]:
    """Con qué nombres se llama al enumerador en este archivo.

    El enumerador es `_cadenas` —su nombre sale del objeto, no está tecleado— y
    un alias es una función que no hace más que devolver lo que él devuelve
    (`def pedidos(nodo): return _cadenas(nodo, ctx.ambitos)`) o un nombre atado
    directamente a él. Se repite hasta que deje de crecer, para que un alias de
    un alias también entre.
    """
    nombres = {_cadenas.__name__}
    for _ in range(4):
        antes = set(nombres)
        for n in ast.walk(arbol):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    len(n.body) == 1 and isinstance(n.body[0], ast.Return) and \
                    isinstance(n.body[0].value, ast.Call) and \
                    isinstance(n.body[0].value.func, ast.Name) and \
                    n.body[0].value.func.id in nombres:
                nombres.add(n.name)
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                    isinstance(n.targets[0], ast.Name) and \
                    isinstance(n.value, ast.Name) and n.value.id in nombres:
                nombres.add(n.targets[0].id)
        if nombres == antes:
            break
    return nombres


def _sitios_que_enumeran_nombres(arbol) -> list[ast.Call]:
    """TODA llamada al enumerador que hay en el archivo, esté donde esté.

    Menos una: la línea que DEFINE un alias (`def pedidos(nodo): return
    _cadenas(...)`). Ahí no hay nada que comprobar —el alias devuelve tal cual
    lo que le den, incluido el `None`— y la obligación pasa a quien lo llame,
    que es justo lo que esta lista recoge.
    """
    alias = _alias_del_enumerador(arbol)
    definiciones = set()
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                n.name in alias and len(n.body) == 1 and \
                isinstance(n.body[0], ast.Return) and \
                isinstance(n.body[0].value, ast.Call):
            definiciones.add(id(n.body[0].value))
    def se_llama(f) -> bool:
        # Un nombre suelto (`pedidos(x)`) o punteado (`T._cadenas(x)`). Lo
        # segundo no lo escribe nadie hoy; se mira igual porque no cuesta, y
        # porque «no lo miro» es como se escapó la fuga de las seis vueltas.
        return (isinstance(f, ast.Name) and f.id in alias) or \
            (isinstance(f, ast.Attribute) and f.attr in alias)

    return [n for n in ast.walk(arbol)
            if isinstance(n, ast.Call) and se_llama(n.func)
            and id(n) not in definiciones]


def _cuerpo_de_infracciones(arbol):
    """El nodo `FunctionDef` de `_infracciones` dentro del árbol del archivo."""
    for n in ast.walk(arbol):
        if isinstance(n, ast.FunctionDef) and n.name == _infracciones.__name__:
            return n
    raise AssertionError(
        f"no se encontró `{_infracciones.__name__}` en el árbol del archivo; "
        "sin él estas pruebas estarían verdes sin mirar nada")


def _motivos_que_enumeran_nombres(arbol) -> dict[str, list[ast.Call]]:
    """Qué motivo de `_infracciones` llama al enumerador, leído de su código.

    Los motivos se marcan con un comentario `# (x)` en el cuerpo de la función.
    Ni las letras ni los números de línea se escriben acá: se sacan del archivo,
    así que un motivo nuevo aparece solo. Solo cuentan las marcas que caen
    DENTRO de `_infracciones`, y ese rango sale del propio nodo.
    """
    cuerpo = _cuerpo_de_infracciones(arbol)
    desde, hasta = cuerpo.lineno, cuerpo.end_lineno or cuerpo.lineno

    marcas: list[tuple[int, str]] = []
    for i, linea in enumerate(
            inspect.getsource(sys.modules[__name__]).splitlines(), 1):
        if not desde <= i <= hasta:
            continue
        limpia = linea.strip()
        if limpia.startswith("# (") and ")" in limpia:
            letra = limpia[3:limpia.index(")")]
            if len(letra) == 1 and letra.isalpha():
                marcas.append((i, letra))

    salida: dict[str, list[ast.Call]] = {}
    for n in _sitios_que_enumeran_nombres(arbol):
        if not desde <= n.lineno <= hasta:
            continue
        cuales = [m for m in marcas if m[0] <= n.lineno]
        letra = cuales[-1][1] if cuales else "?"
        salida.setdefault(letra, []).append(n)
    return salida


# Lo que una rama tiene que CONTENER para que se acepte que hace algo. La lista
# es sobre la gramática de sentencias de Python, que sí es finita y enumerable,
# y va por el lado seguro: una clase de nodo que no esté acá NO cuenta como
# consecuencia, así que una forma nueva de «hacer algo» se pone roja y se ve, en
# vez de colarse en verde. Ver `_juez_de_cuidados`.
_HACEN_ALGO = (ast.Return, ast.Raise, ast.Assert, ast.Break, ast.Continue,
               ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete,
               ast.Call, ast.Yield, ast.YieldFrom, ast.Await,
               ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)


def _juez_de_cuidados(arbol):
    """Devuelve `como_se_cuida(llamada) -> letra | None` sobre ESTE árbol.

    Está fuera de la prueba para que se pueda pasar un árbol INVENTADO y medir
    cada cuidado con un caso escrito, en vez de afirmar en un comentario que no
    se pasan con un gesto. Lo mide
    `test_los_cuidados_no_se_pasan_con_un_gesto_que_no_hace_nada`.

    LOS CINCO CUIDADOS: (A) el resultado se compara contra `None` ahí mismo;
    (B) se asigna a un nombre que se compara contra `None` después y en el mismo
    bloque; (C) se devuelve tal cual, con lo que la obligación pasa a quien
    llame; (D) se recoge en una comprensión cuyos elementos se comparan contra
    `None`; (E) se pasa como argumento a una función de este archivo que compara
    ESE parámetro contra `None`.

    Y LA CONDICIÓN QUE LES FALTABA A CUATRO DE LOS CINCO, del 6-sep-2026: que la
    comparación TENGA CONSECUENCIA. Un testigo metió en una copia un motivo
    defectuoso pasando el valor por este ayudante:

        def _log_si_desconocido(nombre_atributo):
            if nombre_atributo is None:
                pass                  # un "diagnóstico" que no diagnostica nada
            return nombre_atributo

    y el cuidado (E) lo aceptó —36 passed— porque solo preguntaba si el
    parámetro se comparaba contra `None` en alguna parte del cuerpo, sin mirar
    si esa comparación hacía algo. Con esa copia, un `setattr` dinámico sobre
    `config` quedaba verde y funcionaba. (A), (B) y (D) tenían el mismo defecto:
    les bastaba que la comparación existiera. El único sano era (C), porque
    devolver un valor no se puede fingir: el valor sale de verdad y la
    obligación cae en quien llama, que también está en la lista.

    QUÉ CUENTA COMO CONSECUENCIA, y por qué así. Se sube desde la comparación
    hasta la SENTENCIA que la contiene:

      · Si esa sentencia es una expresión suelta (`ast.Expr`), el valor de la
        comparación se tira a la basura: no hay consecuencia. Es el único
        enunciado de Python que evalúa algo y descarta el resultado, así que
        esta rama no es una lista adivinada sino el complemento de un hecho.
      · Si es un `if`/`while` cuyo test contiene la comparación, la consecuencia
        vive en las ramas: alguna tiene que CONTENER algo de `_HACEN_ALGO`.
      · Si es cualquier otra sentencia (`return`, una asignación, un `assert`,
        un argumento de llamada, el iterable de un `for`), el valor SALE de
        donde está y eso cuenta.

    SU FONDO, dicho para que no prometa de más: comprueba que la rama NO ESTÉ
    VACÍA, no que lo que hay dentro sea lo correcto. `if x is None: _ = 1` sigue
    pasando, y eso está medido en la prueba de los gestos. Que el `is None`
    dispare el `malas.append` que toca lo mide, corriendo y motivo por motivo,
    `test_lo_indeterminable_es_rojo_en_cada_motivo_por_separado`.
    """
    def rama_hace_algo(cuerpo) -> bool:
        return any(isinstance(x, _HACEN_ALGO)
                   for s in cuerpo for x in ast.walk(s))

    def tiene_consecuencia(cmp) -> bool:
        hijo, arriba = cmp, getattr(cmp, "_p", None)
        while arriba is not None and not isinstance(arriba, ast.stmt):
            hijo, arriba = arriba, getattr(arriba, "_p", None)
        if arriba is None or isinstance(arriba, ast.Expr):
            return False
        if isinstance(arriba, (ast.If, ast.While)) and arriba.test is hijo:
            return rama_hace_algo(arriba.body) or rama_hace_algo(arriba.orelse)
        return True

    def compara_con_none(nodo) -> bool:
        return isinstance(nodo, ast.Compare) and \
            any(isinstance(o, (ast.Is, ast.IsNot)) for o in nodo.ops) and \
            any(isinstance(c, ast.Constant) and c.value is None
                for c in nodo.comparators) and \
            tiene_consecuencia(nodo)

    # DÓNDE vive cada sentencia: su bloque y su posición dentro de él. Hace
    # falta para no darse por satisfecho con que el nombre se compare «en
    # alguna parte de la función»: `claves` lo usan (e) y (j), y `quiere` (c) y
    # (f), así que un motivo nuevo que reutilizara el nombre y NO lo comparara
    # pasaría gratis colgado de la comprobación del vecino. La comparación
    # tiene que estar DESPUÉS de la asignación y en su mismo bloque.
    sitio: dict[int, tuple[list, int]] = {}
    for n in ast.walk(arbol):
        for campo in ("body", "orelse", "finalbody"):
            bloque = getattr(n, campo, None)
            if isinstance(bloque, list):
                for i, s in enumerate(bloque):
                    if isinstance(s, ast.stmt):
                        sitio[id(s)] = (bloque, i)

    def se_compara_despues(asignacion, nombre: str) -> str | None:
        """(B) o (D): el nombre asignado se compara contra None más adelante."""
        donde = sitio.get(id(asignacion))
        if donde is None:
            return None
        bloque, i = donde
        for posterior in bloque[i + 1:]:
            for n in ast.walk(posterior):
                if not compara_con_none(n) or not isinstance(n.left, ast.Name):
                    continue
                if n.left.id == nombre:
                    return "B"
                # (D) `partes = [pedidos(e) for e in ...]` seguido de
                # `any(p is None for p in partes)`: lo comparado es el elemento
                # y no la lista, y hay que atarlo a ESA lista para que la
                # comprobación no valga colgada de la de otra variable.
                for c in ast.walk(posterior):
                    if isinstance(c, ast.comprehension) and \
                            isinstance(c.target, ast.Name) and \
                            c.target.id == n.left.id and \
                            isinstance(c.iter, ast.Name) and \
                            c.iter.id == nombre:
                        return "D"
        return None

    def sentencia_de(nodo):
        while nodo is not None and not isinstance(nodo, ast.stmt):
            nodo = getattr(nodo, "_p", None)
        return nodo

    # (E) Qué funciones de este archivo comparan contra None el parámetro que
    # está en una posición dada. Se saca del árbol, no se teclea.
    def parametro_comparado(nombre_fn: str, pos: int) -> bool:
        for n in ast.walk(arbol):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or \
                    n.name != nombre_fn:
                continue
            args = n.args.posonlyargs + n.args.args
            if pos >= len(args):
                continue
            param = args[pos].arg
            for d in ast.walk(n):
                if compara_con_none(d) and isinstance(d.left, ast.Name) and \
                        d.left.id == param:
                    return True
        return False

    def como_se_cuida(c: ast.Call) -> str | None:
        padre = getattr(c, "_p", None)
        if compara_con_none(padre):
            return "A"
        if isinstance(padre, ast.Return):
            return "C"                       # la obligación pasa a quien llame
        if isinstance(padre, ast.Assign) and len(padre.targets) == 1 and \
                isinstance(padre.targets[0], ast.Name):
            visto = se_compara_despues(padre, padre.targets[0].id)
            if visto:
                return visto
        st = sentencia_de(c)                 # (D): dentro de una comprensión
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and \
                isinstance(st.targets[0], ast.Name):
            visto = se_compara_despues(st, st.targets[0].id)
            if visto:
                return visto
        if isinstance(padre, ast.Call) and isinstance(padre.func, ast.Name):
            for i, a in enumerate(padre.args):
                if a is c and parametro_comparado(padre.func.id, i):
                    return "E"
        return None

    return como_se_cuida


def test_ningun_sitio_del_archivo_deja_pasar_lo_que_no_puede_enumerar():
    """LA regla, exigida a TODO EL ARCHIVO y no leyéndolo uno a uno.

    EL AGUJERO DEL 6-sep-2026 no fue una clase nueva de fuga: fue la regla de
    esta guarda sin aplicar a una rama. De los motivos que preguntan qué nombres
    puede valer una expresión, todos menos uno trataban «no lo sé» como ROJO. El
    motivo (d) preguntaba `if trae and _MODULO_CONFIG in trae`, así que un
    `None` —que significa «no se puede enumerar»— hacía la condición False en
    silencio. Por ahí salía la lista cruda entera.

    Y EL AGUJERO DE ESTA PRUEBA, del mismo día: miraba solo
    `inspect.getsource(_infracciones)`. Un testigo escribió un motivo con
    exactamente ese defecto en una FUNCIÓN AUXILIAR de fuera, llamada desde el
    bucle con una línea, y la suite dio 35 passed. La prueba prometía «el
    séptimo motivo no puede nacer con este defecto» y solo miraba un trozo del
    archivo.

    SU ALCANCE, AHORA, dicho con precisión y sin prometer de más:

      · MIRA el archivo entero —`sys.modules[__name__]`, no una ruta escrita
        acá— y dentro de él TODA llamada al enumerador `_cadenas`, se le llame
        como se le llame: los alias se derivan del árbol (una función que no
        hace más que devolver lo que él devuelve), no hay ninguno tecleado.
      · EXIGE que el resultado de cada una haga una de estas cinco cosas, y
        nada más: (A) compararse contra `None` ahí mismo; (B) asignarse a un
        nombre que se compara contra `None` después y en el mismo bloque; (C)
        devolverse tal cual, con lo que la obligación pasa a quien llame —y ese
        sitio también está en esta lista—; (D) recogerse en una comprensión
        cuyos elementos se comparan contra `None`; (E) pasarse como argumento a
        una función de este archivo que compara ESE parámetro contra `None`.
        Los cinco están en `_juez_de_cuidados`, con la condición que les faltaba
        a cuatro: que la comparación TENGA CONSECUENCIA. Hasta el 6-sep-2026
        bastaba con que estuviera escrita, y un `if x is None: pass` pasaba por
        cuidado — medido, y ahora medido al revés en
        `test_los_cuidados_no_se_pasan_con_un_gesto_que_no_hace_nada`.
      · NO MIRA lo que la guarda hace con el resultado una vez comparado, ni si
        lo que hay dentro de la rama sirve: solo que la rama no esté vacía. Que
        `claves is None` dispare el `malas.append` correcto lo mide
        `test_lo_indeterminable_es_rojo_en_cada_motivo_por_separado`, motivo por
        motivo y corriendo. Entre las dos no queda hueco: ésta ve la forma en
        todo el archivo, la otra ve el veredicto en cada motivo.
      · Y UN HUECO QUE SÍ QUEDA, dicho para que no prometa de más: la otra mide
        por LETRA de motivo, no por sitio. Dos llamadas bajo la misma letra, una
        de ellas defectuosa, se cubren la una a la otra. Esta prueba lo tapa por
        la forma —el sitio flojo sale igual— pero no por el veredicto.

    No hay ninguna lista de motivos ni de sitios escrita acá.
    """
    arbol = _arbol_de_este_archivo()
    como_se_cuida = _juez_de_cuidados(arbol)

    porletra = _motivos_que_enumeran_nombres(arbol)
    assert porletra, (
        "no se encontró ni un solo motivo de `_infracciones` que llame al "
        "enumerador. O se renombró y esta prueba quedó mirando al vacío —o sea "
        "verde sin comprobar nada—, o los motivos dejaron de enumerar nombres")

    sitios = _sitios_que_enumeran_nombres(arbol)
    assert len(sitios) >= sum(len(v) for v in porletra.values()), (
        "el barrido del archivo entero encontró MENOS sitios que el de "
        "`_infracciones` solo. Eso es imposible salvo que el barrido esté roto, "
        "y un barrido roto es verde sin haber mirado")

    cuerpo = _cuerpo_de_infracciones(arbol)
    dentro = range(cuerpo.lineno, (cuerpo.end_lineno or cuerpo.lineno) + 1)

    flojos: dict[str, list[int]] = {}
    for c in sitios:
        if como_se_cuida(c) is not None:
            continue
        donde = "en un motivo de _infracciones" if c.lineno in dentro \
            else "FUERA de _infracciones"
        flojos.setdefault(donde, []).append(c.lineno)

    assert not flojos, (
        f"estos sitios enumeran nombres y NO tratan «no lo sé» como rojo: "
        f"{flojos} (líneas del archivo). Un `pedidos(...)` cuyo resultado no se "
        "compara contra None deja pasar en silencio justo el caso en el que "
        "menos se sabe qué se está pidiendo, que es como se escapó la fuga del "
        "6-sep-2026. Y si el sitio está FUERA de `_infracciones`, es además la "
        "forma exacta con la que un testigo burló esta prueba el mismo día: un "
        "motivo escrito en una función auxiliar")


# Los cinco cuidados, cada uno escrito BIEN y escrito como GESTO. El gesto es
# la misma forma con la comparación vaciada: existe, se lee igual, y no cambia
# nada. Ninguna letra va tecleada en las claves de este diccionario — la letra
# la dice el juez al correr, y por eso un cuidado que se renombre o desaparezca
# se ve acá en vez de quedar en verde.
_CUIDADOS_A_MEDIR = {
    "(A) comparar el resultado ahí mismo": {
        "bien": 'def motivo(n, amb):\n'
                '    if _cadenas(n, amb) is None:\n'
                '        malas.append("rojo")\n',
        "gestos": [
            # la comparación como sentencia suelta: su valor se tira
            'def motivo(n, amb):\n'
            '    _cadenas(n, amb) is None\n',
            # y la comparación con una rama que no hace nada
            'def motivo(n, amb):\n'
            '    if _cadenas(n, amb) is None:\n'
            '        pass\n',
        ],
    },
    "(B) asignar y comparar después, en el mismo bloque": {
        "bien": 'def motivo(n, amb):\n'
                '    claves = _cadenas(n, amb)\n'
                '    if claves is None:\n'
                '        malas.append("rojo")\n',
        "gestos": [
            'def motivo(n, amb):\n'
            '    claves = _cadenas(n, amb)\n'
            '    if claves is None:\n'
            '        pass\n',
            'def motivo(n, amb):\n'
            '    claves = _cadenas(n, amb)\n'
            '    claves is None\n',
        ],
    },
    "(C) devolverlo tal cual": {
        "bien": 'def motivo(n, amb):\n'
                '    if n is None:\n'
                '        return None\n'
                '    return _cadenas(n, amb)\n',
        # (C) NO tiene gesto, y no es un descuido de esta lista: devolver un
        # valor no se puede fingir. El `None` sale de verdad de la función y la
        # obligación cae entera en quien llama, que también está en la lista de
        # sitios. Los otros cuatro se cuidan CON UNA COMPARACIÓN, y una
        # comparación sí se puede escribir sin que sirva para nada.
        "gestos": [],
    },
    "(D) recogerlo en una comprensión y comparar los elementos": {
        "bien": 'def motivo(elts, amb):\n'
                '    partes = [_cadenas(e, amb) for e in elts]\n'
                '    if any(p is None for p in partes):\n'
                '        return None\n'
                '    return partes\n',
        "gestos": [
            'def motivo(elts, amb):\n'
            '    partes = [_cadenas(e, amb) for e in elts]\n'
            '    any(p is None for p in partes)\n'
            '    return partes\n',
            'def motivo(elts, amb):\n'
            '    partes = [_cadenas(e, amb) for e in elts]\n'
            '    if any(p is None for p in partes):\n'
            '        pass\n'
            '    return partes\n',
        ],
    },
    "(E) pasarlo a una función que compara ESE parámetro": {
        "bien": 'def _cuidar(valor):\n'
                '    if valor is None:\n'
                '        raise AssertionError("rojo")\n'
                '    return valor\n'
                '\n'
                'def motivo(n, amb):\n'
                '    return _cuidar(_cadenas(n, amb))\n',
        "gestos": [
            # EL GESTO QUE ENCONTRÓ UN TESTIGO EL 6-sep-2026, textual: un
            # "diagnóstico" que no diagnostica nada. Con esto en una copia, un
            # `setattr` dinámico sobre `config` quedaba verde y funcionaba.
            'def _log_si_desconocido(nombre_atributo):\n'
            '    if nombre_atributo is None:\n'
            '        pass\n'
            '    return nombre_atributo\n'
            '\n'
            'def motivo(n, amb):\n'
            '    return _log_si_desconocido(_cadenas(n, amb))\n',
        ],
    },
}


def test_los_cuidados_no_se_pasan_con_un_gesto_que_no_hace_nada():
    """Los cinco cuidados, medidos con un caso bueno y uno vaciado por dentro.

    POR QUÉ EXISTE. Hasta el 6-sep-2026 los cuidados (A), (B), (D) y (E) solo
    preguntaban si la comparación contra `None` ESTABA ESCRITA. Un testigo
    escribió el ayudante de `(E) gestos[0]` —`if x is None: pass`, con el
    comentario «un diagnóstico»— y `test_ningun_sitio_del_archivo_deja_pasar_lo
    _que_no_puede_enumerar` dio 36 passed sobre una copia en la que un `setattr`
    dinámico sobre `config` salía verde y devolvía la lista cruda.

    Y por qué se mide en vez de afirmarse: la promesa «este cuidado no se pasa
    con un gesto» solo se puede comprobar escribiendo el gesto. Un comentario
    que lo diga es exactamente la clase de cifra tecleada que este archivo ya
    tuvo que quitar dos veces.

    EL FONDO DE LA COMPROBACIÓN, dicho acá para que no prometa de más: el juez
    exige que la rama NO ESTÉ VACÍA, no que lo que hay dentro sirva. Eso se mide
    abajo con su caso: `if x is None: _ = 1` sigue pasando. Lo que cierra ese
    resto es la otra prueba, la que corre cada motivo y mira el veredicto.
    """
    for nombre, caso in _CUIDADOS_A_MEDIR.items():
        arbol = _con_padres(caso["bien"])
        sitios = _sitios_que_enumeran_nombres(arbol)
        assert len(sitios) == 1, (
            f"{nombre}: el caso bueno tiene {len(sitios)} llamadas al "
            "enumerador y tiene que tener exactamente una; si tiene cero, este "
            "caso está midiendo el vacío y pasa sin comprobar nada")
        letra = _juez_de_cuidados(arbol)(sitios[0])
        assert letra == nombre[1], (
            f"{nombre}: el caso escrito BIEN ya no se acepta con su cuidado — "
            f"el juez dice {letra!r}. O el cuidado desapareció, o el caso dejó "
            "de ser un ejemplo suyo; en cualquiera de los dos casos lo que este "
            "diccionario dice medir no es lo que mide")

    colados = []
    for nombre, caso in _CUIDADOS_A_MEDIR.items():
        for i, fuente in enumerate(caso["gestos"]):
            arbol = _con_padres(fuente)
            sitios = _sitios_que_enumeran_nombres(arbol)
            assert len(sitios) == 1, (
                f"{nombre} gesto {i}: {len(sitios)} llamadas al enumerador en "
                "vez de una; un gesto que no se ve no mide nada")
            letra = _juez_de_cuidados(arbol)(sitios[0])
            if letra is not None:
                colados.append(f"{nombre} gesto {i} → aceptado como {letra!r}")

    assert not colados, (
        f"estos gestos pasan por cuidado sin cuidar nada: {colados}. Una "
        "comparación contra `None` que no cambia nada es un trámite, y el "
        "cuidado que la acepta convierte esta guarda entera en un trámite: fue "
        "así como un motivo defectuoso salió 36 passed el 6-sep-2026")

    # ── EL FONDO, medido y no prometido: hasta dónde llega esto y hasta dónde
    # no. Se comprueba que la rama HACE ALGO, no que lo que hace sirva. Este
    # caso pasa a propósito, y está escrito para que nadie lea la prueba de
    # arriba como si cerrara más de lo que cierra.
    con_fondo = ('def _cuidar(valor):\n'
                 '    if valor is None:\n'
                 '        _ = 1\n'
                 '    return valor\n'
                 '\n'
                 'def motivo(n, amb):\n'
                 '    return _cuidar(_cadenas(n, amb))\n')
    arbol = _con_padres(con_fondo)
    fondo = _juez_de_cuidados(arbol)(_sitios_que_enumeran_nombres(arbol)[0])
    assert fondo == "E", (
        "el fondo declarado se movió: una rama que asigna algo inútil ya no "
        "pasa. Es mejor de lo declarado, y hay que reescribir la declaración — "
        "una frontera que promete de menos hace que nadie vuelva a mirar")


def test_cuanto_cuesta_mirar_el_espacio_de_nombres_y_no_el_metodo():
    """El precio del motivo (j), contado sobre los archivos reales.

    LA TENTACIÓN que había que medir antes de escribir nada: `.get("algo")` es
    de lo más común que hay en Python. Si la respuesta a
    `vars(m).get("CORREO_CUENTAS")` hubiera sido «un `.get` con un argumento que
    no puedo enumerar es rojo», la guarda habría empezado a rojear código que no
    tiene nada que ver con buzones; una guarda que estorba se termina apagando,
    y entonces no vigila nada.

    LAS DOS CIFRAS, contadas acá y no escritas en ningún comentario:

      · lo que costaría la regla a secas — llamadas a un método `get` con algún
        argumento que la guarda no puede enumerar;
      · lo que cuesta la que se quedó — sitios donde un archivo ABRE el espacio
        de nombres de algo (`vars`, `globals`, `locals`, `__dict__`), que es lo
        único que el motivo (j) mira.

    La segunda es la que decide, y es la que tiene aserción: mientras sea cero,
    la regla no le cuesta nada a nadie. La primera va detrás y marcada como
    contador, para que su movimiento no aborte nunca lo que importa.
    """
    llamadas = get_opacos = aperturas = sin_resolver = 0
    rojos: dict[str, list[str]] = {}
    permitidos = _atributos_que_config_ofrece()
    for py in _archivos_vigilados(RAIZ):
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        for padre in ast.walk(arbol):
            for hijo in ast.iter_child_nodes(padre):
                hijo._padre = padre              # type: ignore[attr-defined]
        ctx = _Contexto(arbol)
        for n in ast.walk(arbol):
            if isinstance(n, ast.Call):
                llamadas += 1
                if all(o is _DESCONOCIDO for o in ctx.resuelve(n.func)):
                    sin_resolver += 1
                if isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                        and any(_cadenas(a, ctx.ambitos) is None
                                for a in list(n.args)
                                + [k.value for k in n.keywords]):
                    get_opacos += 1
            if not ctx.abre_un_espacio(n):
                continue
            aperturas += 1
            motivos = [m for m in _infracciones(py.read_bytes(), permitidos)
                       if m.startswith(f"línea {n.lineno}:")
                       and "espacio de nombres" in m]
            if motivos:
                rojos.setdefault(str(py.relative_to(RAIZ)), []).extend(motivos)

    # PRIMERO lo que importa: la regla que se quedó no rojea a nadie.
    assert not rojos, (
        f"el motivo (j) empezó a costar: {rojos}. Un archivo real abre el "
        "espacio de nombres de algo y le pide un nombre que la guarda no puede "
        "clasificar. Eso no es un falso positivo que ignorar — o el sitio se "
        "reescribe pidiendo el buzón por config.cuentas_de_correo(para=...), o "
        "la frontera se renegocia con Tiziano, pero no se afloja la regla")

    print(f"\nCOSTO DEL MOTIVO (j), medido al correr sobre "
          f"{len(_archivos_vigilados(RAIZ))} archivos vigilados y "
          f"{llamadas} llamadas: la regla que se quedó mira {aperturas} "
          f"sitios (los que abren un espacio de nombres) y rojea {len(rojos)}. "
          f"La regla a secas —«un `.get` con un argumento que no puedo "
          f"enumerar es rojo»— habría rojeado {get_opacos}. Y la que cerraría "
          f"la entrada `__getattribute__` de `_FUERA_DE_LA_FRONTERA` —«toda "
          f"llamada cuyo callee no se resuelve es roja»— tocaría "
          f"{sin_resolver}.")

    assert get_opacos > aperturas, (
        _MARCA_CONTADOR + f"la regla a secas costaría {get_opacos} y la que se "
        f"quedó mira {aperturas} sitios. Si la primera ya no es mayor, el "
        "motivo por el que se eligió mirar el espacio y no el método dejó de "
        "ser cierto y hay que volver a mirarlo")


def test_lo_indeterminable_es_rojo_en_cada_motivo_por_separado():
    """La otra mitad: no que esté escrito, sino que MUERDA — motivo por motivo.

    La prueba de arriba mira la forma del código; ésta mira el veredicto. Cada
    caso es la misma idea escrita para un motivo distinto: pedir algo con un
    nombre que la guarda no puede enumerar. Los seis tienen que salir rojos.

    El motivo (a) no aparece porque no enumera nada: el nombre de un atributo
    escrito con un punto es siempre un literal en el árbol, así que no tiene
    versión indeterminable — cuando el nombre se arma al vuelo, eso YA es (c).
    """
    permitidos = _atributos_que_config_ofrece()
    casos = {
        "(c) getattr con un nombre que no se puede enumerar":
            'def f(mod, n):\n    return getattr(mod, n)\n',
        "(d) un módulo traído por un nombre que no se puede enumerar":
            'import importlib\ndef f(n):\n    return importlib.import_module(n)\n',
        "(e) un nombre sacado de un espacio de módulos, sin enumerar":
            'import sys\ndef f(n):\n    return sys.modules[n]\n',
        "(f) el módulo config usado de una forma que no se puede clasificar":
            'import config\ndef f(n):\n    return getattr(config, n)\n',
        "(j) una clave pedida al espacio de nombres de algo, sin enumerar":
            'def f(mod, k):\n    return vars(mod)[k]\n',
    }
    verdes = [k for k, src in casos.items()
              if not _infracciones(src, permitidos)]
    assert not verdes, (
        f"estos motivos dejaron pasar un nombre que no se puede enumerar: "
        f"{verdes}")

    # Y que la prueba de arriba no se quede corta: cada motivo que enumera
    # nombres tiene que tener acá un caso suyo. Las letras salen del código de
    # `_infracciones`, no de una lista escrita a mano, así que un motivo nuevo
    # que enumere nombres y no traiga su caso pone esto rojo.
    letras = set(_motivos_que_enumeran_nombres(
        _arbol_de_este_archivo()))
    cubiertas = {k[1] for k in casos}
    assert letras <= cubiertas, (
        f"estos motivos enumeran nombres y no tienen un caso medido acá: "
        f"{sorted(letras - cubiertas)}. Sin caso, nadie comprueba corriendo "
        "que traten «no lo sé» como rojo")


def test_cuanto_costaria_la_regla_a_secas_y_cuanto_cuesta_la_de_verdad():
    """El precio del motivo (d), contado sobre los archivos reales.

    «Argumento no enumerable ⇒ rojo» aplicado a TODA llamada es la lectura
    literal de la regla, y es inviable: cuenta acá cuántas llamadas legítimas de
    los archivos vigilados caerían. La versión que se quedó pone la puerta en el
    CALLEE —solo las llamadas a algo que trae módulos por su nombre— y tiene que
    costar CERO.

    Las dos cifras se miden al correr y no se afirman en ningún comentario: una
    cifra que nadie puede reproducir es peor que ninguna, porque el que la lee
    la da por buena.
    """
    vigilados = _archivos_vigilados(RAIZ)
    llamadas = a_secas = con_puerta = 0
    archivos_a_secas: set[str] = set()
    archivos_con_puerta: dict[str, list[int]] = {}

    for py in vigilados:
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        ctx = _Contexto(arbol)
        rel = str(py.relative_to(RAIZ))
        for n in ast.walk(arbol):
            if not isinstance(n, ast.Call):
                continue
            llamadas += 1
            args = list(n.args) + [k.value for k in n.keywords]
            sin_enumerar = [a for a in args
                            if _cadenas(a, ctx.ambitos) is None]
            if sin_enumerar:
                a_secas += 1
                archivos_a_secas.add(rel)
                if ctx.llama_a(n, _TRAEN_MODULOS) is not None:
                    con_puerta += 1
                    archivos_con_puerta.setdefault(rel, []).append(n.lineno)

    # Lo que de verdad se exige: la regla que se quedó no rojea a nadie.
    assert con_puerta == 0, (
        f"el motivo (d) empezó a costar: {con_puerta} llamadas legítimas en "
        f"{archivos_con_puerta} le pasan un nombre no enumerable a algo que "
        "trae módulos. Antes de aflojar la regla hay que mirar si ese uso es "
        "legítimo — y si lo es, la frontera se declara acá con su medida")

    # Y la constancia de por qué la puerta está en el callee y no en el
    # argumento. Si esto dejara de ser cierto, la regla a secas sería viable y
    # habría que preferirla, porque no depende de reconocer ninguna función.
    assert a_secas > llamadas // 4, (
        f"{_MARCA_CONTADOR}la regla «argumento no enumerable ⇒ rojo» a secas ya "
        f"solo costaría {a_secas} de {llamadas} llamadas en "
        f"{len(archivos_a_secas)} de {len(vigilados)} archivos. La aserción de "
        "fondo —la regla que se quedó no rojea a nadie— YA CORRIÓ arriba y "
        "quedó verde, así que esto NO es una fuga. Si de verdad bajó tanto, "
        "conviene revisarla: sería un fondo mejor que la puerta en el callee")


# ── LO QUE QUEDA FUERA DE LA FRONTERA, con su código exacto ───────────────
#
# Cada entrada es una forma que la guarda NO atrapa, escrita entera para que
# nadie la redescubra creyendo que encontró un agujero nuevo. Caen fuera por uno
# de los TRES motivos que dice la cabecera, y cada entrada dice por cuál:
#
#   · no NOMBRAN lo que quieren (buscan en ejecución comparando valores);
#   · lo nombran sobre un objeto que la guarda no puede RESOLVER;
#   · o lo nombran A MÁS DE UN SALTO del sitio que la guarda mira.
#
# Los campos:
#   · `fuente`      el código, tal cual. Tiene que salir VERDE.
#   · `llega_sola`  ¿consigue ella sola la lista cruda? Si es False, hace falta
#                   además el módulo `config`, y conseguirlo cae por (d)/(e)/(f).
#   · `y_llegando`  la misma forma usada para alcanzar la lista, una entrada por
#                   cada puerta distinta por la que se consiga `config`. Todas
#                   tienen que salir ROJAS. Vacía cuando `llega_sola` es True:
#                   ahí no hay nada que la atrape, y eso es lo que se declara.
#   · `con_un_salto` OPCIONAL, y solo en las que caen fuera POR EL SALTO: la
#                   misma forma con el salto de más quitado, que tiene que salir
#                   ROJA. Es lo que fija el fondo en las dos direcciones: si el
#                   barrido se ensancha, `fuente` deja de ser verde; si se
#                   estrecha, ésta deja de ser roja. Lo mide
#                   `test_el_fondo_del_espacio_abierto_es_un_salto`, que además
#                   EJECUTA las dos y comprueba que las dos sacan la lista.
#
# Que estén acá NO afloja ninguna aserción: cada una está fijada por una prueba
# que se pone roja si cambia de lado, en cualquiera de las dos direcciones.
_FUERA_DE_LA_FRONTERA = {
    # LA fuga que cerró la persecución, medida por la sala el 6-sep-2026. No
    # nombra `config` ni `CORREO_CUENTAS` en ninguna parte: recorre los módulos
    # ya cargados y le pregunta a cada uno cómo se llama. El único rojo que
    # produjo fue un CONTADOR de costo, y por eso se arreglaron los contadores.
    "el bucle sobre los módulos cargados, eligiendo por el nombre partido": {
        "fuente":
            'def _es_el_que_busco(m):\n'
            '    return getattr(m, "__name__", "") == "con" + "fig"\n'
            '\n'
            'def _robar():\n'
            '    import sys\n'
            '    salida = []\n'
            '    for m in sys.modules.values():\n'
            '        if _es_el_que_busco(m):\n'
            '            salida.extend(vars(m).values())\n'
            '    return salida\n',
        "llega_sola": True,
        "y_llegando": [],
        "por_que":
            "el nombre del módulo se compara en tiempo de ejecución contra un "
            "string armado al vuelo, y los valores salen por `.values()`, que "
            "no es un `Subscript`: no hay un solo nodo del árbol donde "
            "aparezca qué se quiere",
    },
    # Las tres formas de conseguir el objeto `getattr` sin nombrarlo. El motivo
    # (h) mira nodos `ast.Name` y `ast.Attribute`; el resultado de un
    # `Subscript` no es ninguno de los dos.
    "getattr sacado del __dict__ de builtins importado": {
        "fuente":
            'import builtins\n'
            'def f(mod, n):\n'
            '    ga = builtins.__dict__["get" + "attr"]\n'
            '    return ga\n',
        "llega_sola": False,
        "y_llegando": [
            'import builtins\n'
            'import sys\n'
            'def f():\n'
            '    ga = builtins.__dict__["get" + "attr"]\n'
            '    return ga(ga(sys, "modules")["config"], "CORREO_CUENTAS")\n',
        ],
        "por_que": "`_PELIGROSOS` compara por identidad de objeto, y acá el "
                   "objeto se consigue sin que su nombre esté en el árbol",
    },
    "getattr sacado de la tabla de módulos, sin importar builtins": {
        "fuente":
            'import sys\n'
            'def f(mod, n):\n'
            '    ga = sys.modules["builtins"].__dict__["get" + "attr"]\n'
            '    return ga\n',
        "llega_sola": False,
        "y_llegando": [
            'import sys\n'
            'def f():\n'
            '    ga = sys.modules["builtins"].__dict__["get" + "attr"]\n'
            '    return ga(ga(sys, "modules")["config"], "CORREO_CUENTAS")\n',
            # Y la misma forma consiguiendo `config` por otra puerta: el nombre
            # del módulo armado al vuelo y pasado a `import_module`. Cae por
            # (d), no por (e), así que mide otra cosa.
            'import sys\n'
            'import importlib\n'
            'def f(n):\n'
            '    ga = sys.modules["builtins"].__dict__["get" + "attr"]\n'
            '    return ga(importlib.import_module(n), "CORREO_CUENTAS")\n',
        ],
        "por_que": "igual que la anterior, y además sin necesitar `import "
                   "builtins`",
    },
    # LA TERCERA, que el archivo no declaraba: encontrada el 6-sep-2026.
    # Ejecutada, `vars(builtins)["get" + "attr"] is builtins.getattr` da True.
    "getattr sacado de vars(builtins)": {
        "fuente":
            'import builtins\n'
            'def f(mod, n):\n'
            '    ga = vars(builtins)["get" + "attr"]\n'
            '    return ga\n',
        "llega_sola": False,
        "y_llegando": [
            'import builtins\n'
            'import sys\n'
            'def f():\n'
            '    ga = vars(builtins)["get" + "attr"]\n'
            '    return ga(ga(sys, "modules")["config"], "CORREO_CUENTAS")\n',
        ],
        "por_que": "el archivo declaraba DOS formas de conseguir `getattr` sin "
                   "nombrarlo y hay al menos tres; el motivo (j) rojea la "
                   "clave indeterminable, pero acá la clave SÍ se enumera y no "
                   "es ninguno de los dos nombres prohibidos",
    },
    # EL MOTIVO (i) Y EL (c), APAGADOS POR UN NOMBRE LOCAL. Comprobado con un
    # caso ejecutable el 6-sep-2026, y es peor de lo que se creía: `ga` no
    # queda «sin resolver», queda resuelto AL OBJETO EQUIVOCADO, así que ni
    # siquiera el nombre de atributo escrito entero lo salva.
    "un import local que le roba el nombre a un alias peligroso": {
        "fuente":
            'from builtins import getattr as ga\n'
            'def _prepara():\n'
            '    import os as ga\n'
            '    return ga.getcwd()\n'
            'def robar(mod):\n'
            '    return ga(mod, "CORREO_" + "CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            'from builtins import getattr as ga\n'
            'import sys\n'
            'def _prepara():\n'
            '    import os as ga\n'
            '    return ga.getcwd()\n'
            'def robar():\n'
            '    return ga(sys.modules["config"], "CORREO_" + "CUENTAS")\n',
        ],
        "por_que":
            "`_Contexto.importados` es PLANO: no tiene ámbitos. El `import os "
            "as ga` de dentro de una función pisa el `ga` de todo el archivo, "
            "así que (i) y (c) resuelven `ga` al módulo `os` cuando en tiempo "
            "de ejecución es `builtins.getattr`. Es la especie de «no lo sé ⇒ "
            "verde» que ya se arregló en (c), (d) y (j), pero por el lado de "
            "«lo resolví MAL ⇒ verde». Arreglarlo pide darle ámbitos a "
            "`importados`, o sea rehacer el Tramo 2 entero: es otra vuelta, y "
            "por eso hoy se declara en vez de perseguirse",
    },
    # LA CUARTA, encontrada el 6-sep-2026 al cerrar el motivo (j) y declarada
    # el mismo día porque es la MITAD QUE FALTABA DE LA FRONTERA. El criterio
    # de la cabecera decía «tacha `CORREO_CUENTAS`; si el código deja de
    # encontrar su objetivo, la guarda lo atrapa», y acá el nombre está escrito
    # entero y aun así no cae: `__getattribute__` es el `getattr` que vive
    # DENTRO del objeto, y el motivo (c) reconoce a `getattr` resolviéndolo al
    # objeto — sobre un `m` que la guarda no sabe qué es no hay nada que
    # resolver. Ejecutada devuelve `config.CORREO_CUENTAS` en persona
    # (`resultado is config.CORREO_CUENTAS` → True, medido).
    #
    # Por eso la frontera de la cabecera dice ahora «lo que un archivo nombra
    # SOBRE UN OBJETO QUE LA GUARDA PUEDE RESOLVER»: para tener ese `m` hay que
    # haber conseguido el módulo por una puerta que ya está fuera de la
    # frontera, y en cuanto se consigue por una que la guarda ve, cae — es
    # justo lo que miden las dos formas de `y_llegando`.
    "el getattr que vive dentro del objeto, sobre un módulo sin resolver": {
        "fuente":
            'def robar(m):\n'
            '    return m.__getattribute__("CORREO_CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            # el mismo `__getattribute__` sobre el config que se ve: cae por (f)
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return config.__getattribute__("CORREO_CUENTAS")\n',
            # y consiguiéndolo por la tabla de módulos: cae por (e)
            'import sys\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return sys.modules["config"].__getattribute__(\n'
            '        "CORREO_CUENTAS")\n',
        ],
        "por_que":
            "el motivo (c) decide qué es `getattr` RESOLVIÉNDOLO al objeto, y "
            "un método buscado sobre un valor que la guarda no puede resolver "
            "no resuelve a nada. Cerrarlo pediría rojear toda llamada cuyo "
            "callee no se resuelve, y eso es la mayoría de las llamadas de los "
            "archivos vigilados —lo cuenta al correr "
            "`test_cuanto_cuesta_mirar_el_espacio_de_nombres_y_no_el_metodo`, "
            "acá no va ninguna cifra tecleada—, o sea apagar la guarda por "
            "exceso de ruido. Se declara en vez de perseguirse",
    },

    # ── LAS SEIS DEL SALTO, medidas por la sala el 6-sep-2026 y declaradas el
    # mismo día. Las seis NOMBRAN `CORREO_CUENTAS` con todas sus letras y
    # operan sobre un módulo que la guarda SÍ resuelve, así que no caen por
    # ninguno de los dos motivos anteriores: caen por el TERCERO, el fondo del
    # motivo (j). El nombre está escrito UN SALTO más allá del sitio que la
    # guarda mira, y ahí ya no mira.
    #
    # POR QUÉ SE DECLARAN EN VEZ DE PERSEGUIRSE, que es la pregunta que decide
    # de qué lado va cada hallazgo. Seguir el segundo salto es seguir el
    # tercero: después de `.copy()` viene `d = {}; d.update(...)`, después
    # guardarlo en un atributo de una clase, después pasarlo por dos funciones.
    # Eso es análisis de flujo de datos, y el espacio NO TIENE FONDO — es
    # exactamente el caso en que enumerar formas no termina nunca. Así que en
    # vez de la séptima forma se le puso FONDO al barrido, dicho en una línea
    # en la cabecera, y las seis se escriben acá con su código exacto.
    #
    # Las seis, EJECUTADAS con el `config` de verdad, devuelven el objeto
    # `config.CORREO_CUENTAS` en persona —comparado por identidad, sin imprimir
    # ninguna credencial— y lo mismo su versión de un salto. Eso lo comprueba
    # `test_el_fondo_del_espacio_abierto_es_un_salto`, para que ni la forma
    # declarada fuera ni la que la fija sean paja: las dos funcionan.
    "el espacio copiado con dict() antes de pedirle el nombre": {
        "fuente":
            'def robar(mod):\n'
            '    return dict(vars(mod))["CORREO_CUENTAS"]\n',
        "con_un_salto":
            'def robar(mod):\n'
            '    return vars(mod)["CORREO_CUENTAS"]\n',
        "llega_sola": False,
        "y_llegando": [
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return dict(vars(config))["CORREO_CUENTAS"]\n',
        ],
        "por_que":
            "el espacio abierto es argumento de `dict(...)`, y a esa llamada "
            "no viaja ningún nombre: el nombre se le pide a lo que `dict` "
            "DEVOLVIÓ, que ya no es el espacio",
    },
    "el espacio copiado con .copy() antes de pedirle el nombre": {
        "fuente":
            'def robar(mod):\n'
            '    return vars(mod).copy()["CORREO_CUENTAS"]\n',
        "con_un_salto":
            'def robar(mod):\n'
            '    return vars(mod).get("CORREO_CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return vars(config).copy()["CORREO_CUENTAS"]\n',
        ],
        "por_que":
            "el método llamado sobre el espacio SÍ se mira —y da igual cómo se "
            "llame, por eso la versión de un salto usa `.get`—, pero a `.copy()` "
            "no viaja ningún nombre y el subíndice cae sobre su resultado",
    },
    "el __dict__ copiado, y el nombre pedido a la copia": {
        "fuente":
            'def robar(mod):\n'
            '    return mod.__dict__.copy().get("CORREO_CUENTAS")\n',
        "con_un_salto":
            'def robar(mod):\n'
            '    return mod.__dict__.get("CORREO_CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return config.__dict__.copy().get("CORREO_CUENTAS")\n',
        ],
        "por_que":
            "lo mismo que la anterior por la puerta del `__dict__`, que la "
            "guarda sigue igual que a `vars`: el salto de más es el `.copy()`",
    },
    "el espacio volcado en otro diccionario con update()": {
        "fuente":
            'def robar(mod):\n'
            '    d = {}\n'
            '    d.update(vars(mod))\n'
            '    return d["CORREO_CUENTAS"]\n',
        "con_un_salto":
            'def robar(mod):\n'
            '    d = vars(mod)\n'
            '    return d["CORREO_CUENTAS"]\n',
        "llega_sola": False,
        "y_llegando": [
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    d = {}\n'
            '    d.update(vars(config))\n'
            '    return d["CORREO_CUENTAS"]\n',
        ],
        "por_que":
            "`_propagar_espacios` sigue el espacio mientras se le asigne TAL "
            "CUAL a un nombre —por eso la versión de un salto, con `d = "
            "vars(mod)`, sale roja—, pero `d = {}` seguido de un `update` deja "
            "a `d` siendo otro diccionario, y a `update` no viaja ningún nombre",
    },
    "el nombre escrito en otra llamada, y el espacio entregado a lo que devolvió": {
        "fuente":
            'import operator\n'
            '\n'
            '\n'
            'def robar(mod):\n'
            '    return operator.itemgetter("CORREO_CUENTAS")(vars(mod))\n',
        "con_un_salto":
            'import operator\n'
            '\n'
            '\n'
            'def robar(mod):\n'
            '    return operator.getitem(vars(mod), "CORREO_CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            'import operator\n'
            'import config\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return operator.itemgetter("CORREO_CUENTAS")(vars(config))\n',
        ],
        "por_que":
            "cuando el espacio y el nombre están en la MISMA llamada la guarda "
            "los ve, y da igual qué función sea —por eso `operator.getitem` "
            "sale rojo—; acá el nombre está en la llamada de ARRIBA y al "
            "espacio solo lo recibe lo que ésa devolvió",
    },
    "el espacio pasado a otra función, que es la que escribe el nombre": {
        "fuente":
            'def _leer(d):\n'
            '    return d["CORREO_CUENTAS"]\n'
            '\n'
            '\n'
            'def robar(mod):\n'
            '    return _leer(vars(mod))\n',
        "con_un_salto":
            'def _leer(d, clave):\n'
            '    return d[clave]\n'
            '\n'
            '\n'
            'def robar(mod):\n'
            '    return _leer(vars(mod), "CORREO_CUENTAS")\n',
        "llega_sola": False,
        "y_llegando": [
            'import config\n'
            '\n'
            '\n'
            'def _leer(d):\n'
            '    return d["CORREO_CUENTAS"]\n'
            '\n'
            '\n'
            'def robar():\n'
            '    return _leer(vars(config))\n',
        ],
        "por_que":
            "si el nombre viaja a la misma llamada que el espacio, la guarda "
            "lo ve —por eso la versión de un salto sale roja—; escrito DENTRO "
            "del otro lado de la llamada, el parámetro `d` es un nombre atado y "
            "la guarda no lo trata como espacio",
    },
}


def test_la_frontera_declarada_esta_medida_forma_por_forma():
    """LA FRONTERA, medida por sus dos mitades — no afirmada en un comentario.

    Mitad A: las formas de DESCUIDO caen todas. Son `_ESQUIVES`, y lo mide
    `test_la_guarda_muerde_todos_los_caminos_a_la_lista_cruda`; acá se cuenta
    cuántas son, para que el número de la cabecera no sea una cifra tecleada.

    Mitad B: las formas DELIBERADAS de `_FUERA_DE_LA_FRONTERA` no caen, y están
    escritas con su código exacto. Cada una se comprueba en las dos
    direcciones:

      · suelta → VERDE. Si un día sale roja, mejor: hay que venir y reescribir
        la declaración. Lo que no puede pasar es que la declaración diga una
        cosa y la guarda haga otra.
      · usada para llegar a la lista cruda → ROJA, y una vez por cada puerta
        distinta por la que se consiga `config`, no una sola. La excepción es
        la declarada `llega_sola`: ésa no la atrapa nadie, y por eso su lista
        de formas de llegar está vacía.

    Declarar una forma fuera NO es quitar la prueba que la cubría: es
    escribirla donde se vea y fijarla con una aserción que se pone roja si se
    mueve, en cualquiera de las dos direcciones.
    """
    permitidos = _atributos_que_config_ofrece()

    # ── Mitad B, primero lo que importa: cada forma declarada está donde dice
    rojas = {k: _infracciones(d["fuente"], permitidos)
             for k, d in _FUERA_DE_LA_FRONTERA.items()
             if _infracciones(d["fuente"], permitidos)}
    assert not rojas, (
        f"la frontera se movió y ahora la guarda SÍ atrapa esto: {rojas}. Es "
        "una buena noticia, pero la cabecera y `_FUERA_DE_LA_FRONTERA` dicen "
        "lo contrario y hay que corregirlos: una frontera que promete de menos "
        "hace que nadie vuelva a mirar")

    escapados = [f"{k} [{i}]" for k, d in _FUERA_DE_LA_FRONTERA.items()
                 for i, src in enumerate(d["y_llegando"])
                 if not _infracciones(src, permitidos)]
    assert not escapados, (
        f"estas formas están declaradas como «no llegan solas» y sin embargo "
        f"llegaron a la lista cruda sin que nadie las viera: {escapados}. O se "
        "arregla la guarda, o la entrada pasa a `llega_sola: True` y eso es un "
        "agujero nuevo que hay que contarle a Tiziano")

    # Y la coherencia de la declaración: `llega_sola` y `y_llegando` son las
    # dos caras de lo mismo, y una entrada que diga las dos cosas a la vez
    # dejaría sin medir justo lo que declara.
    incoherentes = [k for k, d in _FUERA_DE_LA_FRONTERA.items()
                    if bool(d["llega_sola"]) == bool(d["y_llegando"])]
    assert not incoherentes, (
        f"estas entradas se declaran a sí mismas de dos formas a la vez: "
        f"{incoherentes}. `llega_sola: True` significa que nada la atrapa, y "
        "entonces no hay versión roja que medir; `False` significa que hace "
        "falta además el módulo config, y entonces esa versión tiene que estar "
        "escrita para poder comprobarla")

    # ── Y LA QUE DE VERDAD LLEGA SOLA: que no sea una declaración de papel.
    # Se ejecuta contra el `config` de verdad, con un centinela puesto y
    # quitado acá mismo. No se imprime ninguna credencial: lo que se comprueba
    # es que el objeto `CORREO_CUENTAS` sale por ahí, comparado por identidad.
    solas = [k for k, d in _FUERA_DE_LA_FRONTERA.items() if d["llega_sola"]]
    assert solas, (
        "no queda ninguna forma declarada como «llega sola». Si la guarda "
        "cerró la fuga del bucle sobre los módulos cargados, esto es una gran "
        "noticia y hay que reescribir la cabecera entera")

    centinela = object()
    config.CENTINELA_DE_LA_FRONTERA = centinela
    try:
        espacio: dict = {}
        exec(compile(_FUERA_DE_LA_FRONTERA[solas[0]]["fuente"],
                     "<fuga declarada>", "exec"), espacio)
        salida = espacio["_robar"]()
    finally:
        del config.CENTINELA_DE_LA_FRONTERA

    assert any(v is centinela for v in salida), (
        "la fuga declarada ya no alcanza el espacio de nombres de config. Si "
        "de verdad dejó de funcionar, la declaración sobra y hay que quitarla: "
        "declarar como agujero algo que no lo es asusta sin motivo")
    assert any(v is config.CORREO_CUENTAS for v in salida), (
        "la fuga declarada alcanza config pero ya no saca la lista cruda. Eso "
        "sería el arreglo de la Regla 11 hecho a medias: comprobar si "
        "`CORREO_CUENTAS` dejó de ser un atributo del módulo y, si es así, "
        "reescribir la cabecera")

    # ── LOS NÚMEROS, contados al correr y no escritos en ningún comentario.
    # No hay aserción sobre las cifras a propósito: un contador congelado acá
    # se pondría rojo cada vez que alguien añade una forma, tapando el rojo que
    # importa. Ver `test_los_contadores_dicen_que_son_contadores`.
    llegando = sum(len(d["y_llegando"])
                   for d in _FUERA_DE_LA_FRONTERA.values())
    print(f"\nFRONTERA (medida al correr): la guarda atrapa "
          f"{len(_ESQUIVES)} formas de descuido y tiene "
          f"{len(_FUERA_DE_LA_FRONTERA)} declaradas fuera, de las cuales "
          f"{len(solas)} llega(n) a la lista cruda sin ayuda de nadie. De las "
          f"otras {len(_FUERA_DE_LA_FRONTERA) - len(solas)} se miden "
          f"{llegando} formas de llegar, y las {llegando} salen rojas.")


def test_el_fondo_del_espacio_abierto_es_un_salto():
    """EL FONDO DEL MOTIVO (j), fijado en las dos direcciones y ejecutado.

    EL FONDO, EN UNA LÍNEA — la misma que está en la cabecera:

        Abierto el espacio de nombres de algo, la guarda mira los nombres que
        viajan a LA OPERACIÓN QUE TOCA EL ESPACIO, y ni uno más allá.

    EL CRITERIO PARA PREDECIR SIN PROBAR: mira dónde está escrito el nombre y
    dónde está el `vars(...)` / `globals()` / `locals()` / `.__dict__`. Si los
    dos están en la MISMA operación —subíndice sobre el espacio, método llamado
    sobre el espacio, o los dos como argumentos de una misma llamada— la guarda
    lo ve, sea cual sea el método o la función. Si entre los dos hay una
    operación más —una copia, un `dict(...)`, un `update`, el resultado de otra
    llamada— o el nombre está escrito dentro de otra función, cae fuera. El
    espacio sí se sigue de nombre en nombre mientras se le asigne TAL CUAL
    (`d = vars(m)`, `e = d`): eso no es un salto, es el mismo objeto.

    POR QUÉ EL FONDO ESTÁ AHÍ Y NO UN SALTO MÁS ALLÁ. Seguir el segundo salto
    obliga a seguir el tercero, y el espacio de formas de mover un valor de un
    sitio a otro no tiene fondo: es análisis de flujo de datos. Esta sala ya
    perdió esa carrera tres veces en este archivo. Cuando el espacio no tiene
    fondo, el arreglo no es tapar la forma nueva: es ponerle fondo al barrido y
    decirlo en una línea que cualquiera pueda aplicar sin probar. Lo que queda
    fuera se declara, con su código exacto, en `_FUERA_DE_LA_FRONTERA`.

    LO QUE FIJA ESTA PRUEBA, y por eso se cae si alguien mueve el barrido sin
    mover el fondo:

      · cada forma declarada fuera POR EL SALTO tiene su gemela de un salto
        menos, y ésa tiene que salir ROJA. Si el barrido se estrecha, se pone
        verde y esto se cae.
      · (la otra dirección —que la forma de dos saltos siga verde— la fija
        `test_la_frontera_declarada_esta_medida_forma_por_forma`, que rojea si
        el barrido se ensancha y la declaración se queda vieja.)
      · y las DOS se ejecutan contra el `config` de verdad: las dos tienen que
        devolver el objeto `config.CORREO_CUENTAS` en persona. Sin esto, la
        declaración podría ser un susto de papel y la gemela un espantapájaros
        que no saca nada. No se imprime ninguna credencial: se compara por
        identidad.
    """
    permitidos = _atributos_que_config_ofrece()

    delsalto = {k: d for k, d in _FUERA_DE_LA_FRONTERA.items()
                if "con_un_salto" in d}
    assert delsalto, (
        "no queda ninguna forma declarada fuera POR EL SALTO. Si el motivo (j) "
        "aprendió a seguir el segundo salto, es una gran noticia y hay que "
        "reescribir el fondo de la cabecera; lo que no puede pasar es que esta "
        "prueba se quede verde sin medir nada")

    # ── Lo que importa: la gemela de un salto menos MUERDE.
    blandas = [k for k, d in delsalto.items()
               if not _infracciones(d["con_un_salto"], permitidos)]
    assert not blandas, (
        f"el barrido del motivo (j) se estrechó: {blandas} ya no se ven ni a "
        "UN salto, que es justo el fondo que la cabecera promete. Eso no es "
        "declarar una frontera, es haberla perdido")

    # ── Y que ninguna de las dos sea paja: las dos sacan la lista cruda.
    def _saca_la_lista(fuente: str):
        espacio: dict = {}
        exec(compile(fuente, "<forma del salto>", "exec"), espacio)
        return espacio["robar"](config)

    pajas = []
    for k, d in delsalto.items():
        for cual in ("fuente", "con_un_salto"):
            try:
                if _saca_la_lista(d[cual]) is not config.CORREO_CUENTAS:
                    pajas.append(f"{k} [{cual}]")
            except Exception as e:                       # noqa: BLE001
                pajas.append(f"{k} [{cual}] reventó: {type(e).__name__}")
    assert not pajas, (
        f"estas formas no sacan la lista cruda al ejecutarlas: {pajas}. Una "
        "forma declarada fuera que no funciona asusta sin motivo, y una gemela "
        "que no funciona hace creer que el fondo está medido cuando lo que se "
        "midió fue un espantapájaros")

    print(f"\nFONDO DEL ESPACIO (medido al correr): {len(delsalto)} formas "
          f"declaradas fuera por el salto; sus {len(delsalto)} gemelas de un "
          f"salto menos salen rojas, y las {2 * len(delsalto)} sacan "
          "`config.CORREO_CUENTAS` al ejecutarse.")


def test_las_funciones_que_traen_modulos_no_dependen_de_quien_importo_que():
    """El fondo del barrido de `importlib`, y por qué hizo falta ponérselo.

    El primer intento de este arreglo barría «todo submódulo de `importlib` que
    esté en `sys.modules`». Medido el 6-sep-2026: 28 objetos corriendo el
    archivo suelto y 120 corriendo bajo pytest, que carga `importlib.metadata` y
    `importlib.resources`. Entre esos 120 entraban `str`, `Mapping`, `suppress`,
    `cast` y `filterfalse` —re-exports de módulos que no tienen nada que ver— y
    con `str` adentro el motivo (d) rojeaba 80 llamadas legítimas en 13 de los
    37 archivos vigilados.

    O sea: una lista DERIVADA sin fondo se separa de la realidad igual que una
    tecleada, solo que sin que nadie la haya escrito. El fondo es que se barren
    los módulos que este archivo importa —ni uno más— y que solo se guarda lo
    que el sistema de imports DEFINE, no lo que re-exporta.
    """
    coladas = [o for o in _TRAEN_MODULOS
               if getattr(o, "__module__", None) == builtins.__name__
               and o is not builtins.__import__]
    assert not coladas, (
        f"el barrido se coló objetos de `builtins` que no son `__import__`: "
        f"{[getattr(o, '__name__', o) for o in coladas]}. Con `str` ahí "
        "adentro el motivo (d) rojea a medio repo y la guarda se apaga sola")

    # Y que siga trayendo lo que tiene que traer.
    assert any(o is importlib.import_module for o in _TRAEN_MODULOS)
    assert any(o is importlib.reload for o in _TRAEN_MODULOS)
    assert any(o is builtins.__import__ for o in _TRAEN_MODULOS)

    # EL FILTRO, medido y no supuesto. Un módulo de mentira con tres atributos
    # públicos: uno que el sistema de imports define y dos re-exportados de
    # otro sitio. Solo el primero puede entrar. Sin esta medida, quitarle el
    # filtro al barrido no ponía nada rojo.
    import contextlib

    falso = types.ModuleType("importlib.de_mentira")
    falso.import_module = importlib.import_module      # sí: lo define importlib
    falso.str = str                                    # no: re-export de builtins
    falso.suppress = contextlib.suppress               # no: re-export de contextlib
    salida = _funciones_que_traen_modulos((falso,))
    assert any(o is importlib.import_module for o in salida)
    colados = [getattr(o, "__name__", o) for o in salida
               if o is str or o is contextlib.suppress]
    assert not colados, (
        f"el filtro por `__module__` dejó pasar re-exports: {colados}. Es "
        "exactamente lo que metió `str` en la lista y rojeó 80 llamadas "
        "legítimas de 13 archivos vigilados")

    # Y EL ALCANCE, también medido. El barrido mira los módulos que este
    # archivo importa, no `sys.modules`: si mirara `sys.modules`, un submódulo
    # de `importlib` cargado por cualquier otro cambiaría el veredicto de la
    # guarda. Se comprueba metiendo uno y viendo que no cambia nada.
    intruso = types.ModuleType("importlib.intruso_de_prueba")

    def traeria_modulos(nombre):                      # __module__ = este test
        raise AssertionError("no se debe llamar")

    traeria_modulos.__module__ = "importlib.intruso_de_prueba"
    intruso.traeria_modulos = traeria_modulos
    sys.modules["importlib.intruso_de_prueba"] = intruso
    try:
        despues = _funciones_que_traen_modulos()
        colado = [o for o in despues if o is traeria_modulos]
        assert not colado, (
            "el barrido volvió a mirar `sys.modules`: un submódulo de "
            "`importlib` cargado por otro se le coló. Así es como pasó de 28 "
            "objetos a 120 según quién hubiera importado qué")
        assert len(despues) == len(_TRAEN_MODULOS)
    finally:
        del sys.modules["importlib.intruso_de_prueba"]


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

    # PRIMERO lo que importa. ESTE ES EL ORDEN QUE FALLÓ: el 6-sep-2026 la sala
    # metió en el repo una fuga que devolvía las credenciales del buzón marcado
    # y el único rojo de la suite fue el contador de abajo, que iba delante y
    # abortaba la prueba antes de llegar acá.
    assert not opacos, (
        f"el límite dejó de costar cero: {opacos} piden un nombre de atributo "
        "que no se puede enumerar. Eso no es un falso positivo que ignorar — o "
        "el sitio se reescribe con el nombre a la vista, o el límite se "
        "renegocia con Tiziano, pero no se afloja la aserción")

    assert cuantos == 7, (
        f"{_MARCA_CONTADOR}los archivos vigilados tienen {cuantos} llamadas a "
        "getattr y el 6-sep-2026 eran 7. La aserción de fondo —ninguna de esas "
        "llamadas pide un nombre que no se pueda enumerar— YA CORRIÓ arriba y "
        "quedó verde, así que esto NO es una fuga: es el precio del límite, que "
        "se movió. Hay que volver a mirar cuánto cuesta antes de darlo por "
        "gratis, y actualizar el número")


def test_pisar_un_nombre_peligroso_es_rojo_y_cuanto_cuesta_hoy():
    """EL DEFECTO DE `atados`, arreglado el 6-sep-2026, medido corriendo.

    LO QUE PASABA. `_Contexto.atados` es PLANO: junta en un solo conjunto todo
    nombre que el archivo ate, venga de donde venga. Y `resuelve` lo usa para
    decidir si un nombre suelto es el builtin: `if nodo.id not in self.atados
    and hasattr(builtins, nodo.id)`. O sea que UN parámetro llamado `getattr`,
    en cualquier función del archivo, apagaba el motivo (c) para el archivo
    entero. Medido con estos tres casos:

        sin nada que pise el nombre               → rojo   la agarra
        con un PARÁMETRO llamado getattr          → VERDE  invisible
        con un parámetro de otro nombre (control) → rojo   la agarra

    POR QUÉ ESTO SE ARREGLA Y NO SE DECLARA, que es la pregunta que decide de
    qué lado va cada hallazgo en este archivo. Dos motivos, y los dos importan:

      · No es ofuscación, es un descuido corriente. Usar el nombre de un
        builtin como parámetro se escribe sin pensar, y esta guarda existe
        justamente para los descuidos.
      · Tira hacia VERDE, que es el lado que no avisa. Todo lo demás que está
        declarado en `_FUERA_DE_LA_FRONTERA` tira hacia rojo o exige mala fe.

    LO QUE COMPRUEBA ESTA PRUEBA, y en este orden:

      1. Los tres casos de arriba, tal cual, con el arreglo puesto.
      2. Que valga para LOS DIEZ nombres peligrosos y no solo para `getattr`.
         Los nombres salen de los objetos (`f.__name__`), no de una lista
         tecleada, así que un objeto peligroso nuevo entra solo.
      3. Que valga para las CINCO formas de pisar un nombre, no solo para un
         parámetro: un `def` propio, una asignación, un alias de import y el
         destino de un `for` atan igual que un parámetro.
      4. Y lo que cuesta hoy, contado sobre los archivos de verdad.
    """
    permitidos = _atributos_que_config_ofrece()

    # ── 1. Los tres casos de la sala, tal cual
    llamada = 'def robar(mod, n):\n    return getattr(mod, n)\n'
    tres = {
        "sin nada que pise el nombre": llamada,
        "con un PARÁMETRO llamado getattr":
            'def _otra(getattr):\n    return getattr\n\n\n' + llamada,
        "control: un parámetro de otro nombre":
            'def _otra(gatito):\n    return gatito\n\n\n' + llamada,
    }
    verdes = [k for k, src in tres.items() if not _infracciones(src, permitidos)]
    assert not verdes, (
        f"estos casos salieron VERDES: {verdes}. El del medio es el defecto "
        "que motivó el motivo (k): un parámetro llamado `getattr` en otra "
        "función apaga la resolución del nombre en todo el archivo, y el "
        "`getattr(mod, n)` de abajo deja de verse. El de arriba y el control "
        "tienen que seguir rojos por el motivo (c), o esta prueba estaría "
        "midiendo el vacío")

    # ── 2. Los DIEZ nombres, uno por uno, aislados. Cada caso no hace más que
    # pisar el nombre: sin el motivo (k) los diez son verdes, porque `resuelve`
    # devuelve «no lo sé» y «no lo sé» no era ningún objeto peligroso.
    sueltos = {n: f'def _otra({n}):\n    return {n}\n'
               for n in sorted(_NOMBRES_PELIGROSOS)}
    sin_morder = [n for n, src in sueltos.items()
                  if not _infracciones(src, permitidos)]
    assert not sin_morder, (
        f"pisar estos nombres peligrosos salió verde: {sin_morder}. Con el "
        "nombre pisado la guarda no puede resolver a qué función se llama en "
        "ninguna parte del archivo, y eso es «no lo sé», que en esta guarda es "
        "rojo")

    # Y el control del control: la MISMA forma con un nombre que no es de
    # nadie tiene que salir verde. Sin esto, la comprobación de arriba estaría
    # igual de contenta con una guarda que rojea todo lo que se le ponga
    # delante, que es la otra forma de no comprobar nada.
    assert not _infracciones('def _otra(gatito):\n    return gatito\n',
                             permitidos), (
        "la misma forma con un nombre inocente también sale roja, así que lo "
        "de arriba no está midiendo el nombre sino la forma: la guarda rojea "
        "cualquier cosa y eso no es vigilar, es apagarse")

    # ── 3. Las cinco formas de pisar un nombre. Un parámetro no es la única.
    formas = {
        "un parámetro": 'def _otra(getattr):\n    return getattr\n',
        "una función propia": 'def getattr(o, n):\n    return None\n',
        "una asignación": 'getattr = None\n',
        "un alias de import": 'import os as vars\n',
        "el destino de un for": 'for getattr in (1, 2):\n    pass\n',
    }
    escapadas = [k for k, src in formas.items()
                 if not _infracciones(src, permitidos)]
    assert not escapadas, (
        f"estas formas de pisar un nombre peligroso no se ven: {escapadas}. "
        "`_Contexto.atados` las recoge todas, así que si alguna sale verde es "
        "que el motivo (k) dejó de mirar `atados`")

    # ── 3-bis. Y LA QUE `atados` NO RECOGE, dicha acá para no prometer de más:
    # el `case` de un `match` ata su nombre en tiempo de ejecución y
    # `_leer_ataduras` no lo mira —`ast.MatchAs` guarda el nombre en un campo,
    # no en un nodo `ast.Name`—. No es un agujero: NO verlo significa que la
    # guarda sigue resolviendo el nombre al builtin, así que el `getattr(mod,
    # n)` de abajo cae por el motivo (c) igual. La dirección del error es la
    # segura, y se comprueba corriendo en vez de razonarse.
    con_match = (
        'def _otra(x):\n'
        '    match x:\n'
        '        case getattr:\n'
        '            return getattr\n'
        '\n'
        '\n' + llamada)
    assert _infracciones(con_match, permitidos), (
        "un `case getattr:` dejó el archivo verde. `atados` no ve los nombres "
        "que ata un `match`, y hasta hoy eso caía del lado seguro —el nombre "
        "seguía resolviendo al builtin y el motivo (c) mordía—; si ya no "
        "muerde, la forma pasó al lado que no avisa y hay que recogerla en "
        "`_leer_ataduras`")

    # ── 4. LO QUE CUESTA, contado sobre los archivos reales y no afirmado.
    pisan: dict[str, list[str]] = {}
    for py in sorted(_archivos_vigilados(RAIZ)):
        try:
            arbol = ast.parse(py.read_bytes())
        except SyntaxError:
            continue
        choque = sorted(_Contexto(arbol).atados & _NOMBRES_PELIGROSOS)
        if choque:
            pisan[str(py.relative_to(RAIZ))] = choque

    assert not pisan, (
        f"el motivo (k) dejó de costar cero: {pisan} atan un nombre que es el "
        "de un objeto peligroso. Eso NO es un falso positivo que ignorar: "
        "mientras ese nombre esté pisado la guarda no puede resolver ninguna "
        "llamada de ese archivo, así que el arreglo es renombrar la variable, "
        "no aflojar la regla")

    print(f"\nMOTIVO (k) (medido al correr): {len(_NOMBRES_PELIGROSOS)} nombres "
          f"peligrosos, derivados de los objetos; {len(pisan)} de "
          f"{len(_archivos_vigilados(RAIZ))} archivos vigilados atan alguno.")


def test_los_contadores_no_pueden_tapar_el_rojo_que_importa():
    """El 6-sep-2026 un contador tapó una fuga de credenciales. Que no repita.

    LO QUE PASÓ, medido: la sala metió en el repo una fuga que devolvía la
    lista cruda entera con el buzón `reporte_a: 0` y sus credenciales. La
    prueba de fondo —`test_nadie_lee_la_lista_cruda`— se quedó VERDE, porque la
    fuga cae fuera de la frontera. Y el único rojo de toda la suite fue
    «tienen 8 llamadas a getattr y el 6-sep eran 7»: un contador de costo, que
    además iba PRIMERO en su prueba y la abortaba antes de que la aserción de
    fondo de esa misma prueba llegara a correr.

    Un rojo así engaña dos veces: hace creer que la guarda vio algo, y esconde
    que lo que de verdad mira quedó verde.

    LAS DOS REGLAS, comprobadas acá sobre el árbol de este archivo y no sobre
    una lista de pruebas escrita a mano:

      1. Todo `assert` cuyo mensaje empiece por `_MARCA_CONTADOR` dice de sí
         mismo que es un contador. La marca sale de la constante, no está
         tecleada acá.
      2. Después de un contador NO va ninguna aserción sin marca. O sea: los
         contadores son lo ÚLTIMO de su prueba, y por eso su rojo no puede
         abortar nada que importe — no queda nada detrás que abortar.

    POR QUÉ ASÍ Y NO «QUE NO SEA LA PRIMERA», que fue el primer intento y era
    más débil que el fallo que dice prevenir: en la prueba que falló el
    6-sep-2026 el contador YA tenía delante otra aserción —una que comprobaba
    otra cosa— y aun así tapó a la que venía detrás. Exigir que no vaya primero
    la habría dejado pasar. Comprobado moviendo el contador a su sitio viejo: la
    regla vieja daba 1 passed, ésta da rojo.

    Lo que esta prueba NO mira, dicho para que no prometa de más:

      · Solo este archivo. Los contadores de las demás suites no los ve nadie;
        acá estaban los tres que taparon algo el 6-sep-2026.
      · No juzga si una aserción sin marca es importante, ni si un contador
        está bien marcado como tal. Un contador SIN la marca se le escapa: eso
        no se puede leer del árbol, y por eso la regla 1 se escribe en el
        comentario de `_MARCA_CONTADOR` para quien añada el siguiente.

    Lo que sí garantiza, y es lo que falló: que ningún contador marcado corre
    antes de nada.
    """
    arbol = _arbol_de_este_archivo()

    # Con qué identificadores se escribe la marca en este archivo, sacado de
    # los objetos y no tecleado: si mañana la constante se renombra, esto la
    # sigue en vez de quedarse verde buscando un nombre que ya no existe.
    apodos = {k for k, v in globals().items()
              if isinstance(v, str) and v == _MARCA_CONTADOR}
    assert apodos, (
        "no hay ningún nombre de módulo atado a la marca de contador; sin él "
        "esta prueba no puede reconocer ni uno solo y quedaría verde sin mirar")

    def marcado(a: ast.Assert) -> bool:
        """¿El mensaje EMPIEZA por la marca? Que esté en medio no vale: el rojo
        tiene que decir lo que es en su primera palabra."""
        msg = a.msg
        if isinstance(msg, ast.JoinedStr) and msg.values:
            primero = msg.values[0]
            return isinstance(primero, ast.FormattedValue) and \
                isinstance(primero.value, ast.Name) and \
                primero.value.id in apodos
        return isinstance(msg, ast.Constant) and isinstance(msg.value, str) \
            and msg.value.startswith(_MARCA_CONTADOR)

    contadores: list[tuple[str, int]] = []
    tapan: dict[str, list[int]] = {}
    for fn in ast.walk(arbol):
        # TODA función del archivo, no solo las que empiezan por `test_`. El
        # prefijo es una convención tecleada, y un contador puesto en un
        # ayudante taparía exactamente igual lo que venga detrás.
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        afirmaciones = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        afirmaciones.sort(key=lambda n: n.lineno)
        primer_contador = None
        for a in afirmaciones:
            if marcado(a):
                contadores.append((fn.name, a.lineno))
                if primer_contador is None:
                    primer_contador = a.lineno
            elif primer_contador is not None:
                tapan.setdefault(fn.name, []).append(a.lineno)

    assert not tapan, (
        f"estas aserciones corren DESPUÉS de un contador de su misma prueba, "
        f"así que el rojo del contador las aborta y nunca llegan a correr: "
        f"{tapan} (líneas del archivo). Ése es el orden exacto que el "
        "6-sep-2026 hizo que una fuga de credenciales se viera como «el número "
        "de getattr se movió». Los contadores van al final, después de todo lo "
        "que importa")

    assert contadores, (
        "no se encontró ni un solo `assert` marcado como contador. O las "
        "pruebas dejaron de medir lo que cuestan los límites —y entonces «no "
        "cuesta nada» volvió a ser un adjetivo—, o alguien quitó la marca y "
        "esta prueba quedó verde sin comprobar nada")


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
