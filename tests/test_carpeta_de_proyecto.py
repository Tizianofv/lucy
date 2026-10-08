"""La carpeta del proyecto (`proyectos.carpeta`): parte 5 del diseño de la página completa del
proyecto (8-oct-2026; Tiziano: «Lo archivos seria un enlace al folder en el drive o en la compu donde
vive el proyecto»).

LA GARANTÍA CENTRAL: **solo un valor que empieza por `http://` o `https://` llega a un `href`**;
todo lo demás (una ruta de computadora, `file:`, `javascript:`, `smb:`, lo que sea) sale como texto
escapado. La decide UNA función, `db.enlace_de_carpeta`, mirando el VALOR, y la plantilla la llama
como filtro con el texto guardado (nunca recibe esa decisión ya tomada en el modelo).

Qué se vigila, por la ruta REAL (`POST /proyectos/{pid}/carpeta`), la plantilla REAL,
`crud.editar` / `crud.deshacer` REALES y SQL que se ejecuta de verdad (SQLite con el
`CREATE TABLE proyectos` de `db/schema.sql`):

  1. la puerta del `href` con entradas INVENTADAS (un generador con semilla fija que arma textos de
     trozos raros) contra un oráculo independiente («qué esquema leería un navegador»), no solo con
     una lista corta tecleada; y que el lado estricto es alcanzable;
  2. la puerta de escribir: qué vale, el largo, lo que no es texto, lo que `crud.editar` hace con un
     texto que parece fecha (`_adaptar`);
  3. la página REAL con valores hostiles, en la sesión de la casa y en la de solo ver: ningún atributo
     de ninguna etiqueta, salvo el del enlace de la carpeta, lleva el valor; el enlace solo existe si la
     puerta lo deja pasar y abre aparte sin `opener` ni `referer`; las etiquetas de la página con un
     valor hostil son las mismas que con uno inocente (no se colaron etiquetas nuevas);
  4. quién ve y quién escribe: la casa ve y escribe; solo ver ve y no escribe (ni «Copiar», ni guion);
     sin sesión no se ve ni se escribe;
  5. la ruta: guardar, quitar, «ya estaba así», rechazos con su clave sin escribir nada y sin que lo
     escrito viaje en la dirección, la base que falla, un POST sin el campo;
  6. la página con la base SIN migrar (SQLSTATE 42703 de verdad en el doble), también con las fechas
     ya migradas y esta columna no;
  7. el guion de «Copiar» EJECUTADO (JavaScriptCore con `osascript`), con un `document` y un
     `navigator` de mentira cuya conducta está dicha abajo;
  8. la migración EJECUTADA (traducida a SQLite) y sus promesas escritas;
  9. los escritores de la columna, sacados del código con una sonda, y lo que Lucy por Telegram lee.

FRONTERA, dicha una vez:
  · NO es un navegador. «Abre aparte sin opener» se prueba por los atributos `target="_blank"` y
    `rel="noopener noreferrer"`, no abriendo nada. Ni Safari ni un teléfono.
  · El oráculo de la puerta (`_esquema_que_leeria_un_navegador`) es mi lectura de la regla de la
    especificación de URL (quitar los caracteres de control y el espacio de los extremos, quitar
    tabulaciones y saltos de línea de adentro, tomar el esquema antes de los dos puntos): está escrito
    aquí, no sacado de un navegador.
  · El doble del guion: `navigator.clipboard.writeText` devuelve un objeto con `then(ok, fallo)` que
    llama a uno de los dos enseguida (un `Promise` real es asíncrono; el guion solo usa `.then(a, b)`,
    que es lo que ambos ofrecen); `textContent` del `<code>` es el texto del HTML ya sin escapes, que es
    lo que un navegador entrega. La copia al portapapeles de verdad no se probó.
  · Un `TEXT` de SQLite no impone el largo ni el carácter; la migración corre en SQLite con las mismas
    traducciones declaradas de `tests/test_fechas_de_proyecto.py::_aplicar_migracion`.

Correr:  python3 -m pytest tests/test_carpeta_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import json
import random
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from collections import Counter
from html import unescape
from html.parser import HTMLParser

import pytest
from markupsafe import escape

import test_base_m2 as b2
import test_grupo_ia as g
from test_grupo_ia import _ROOT
from test_pagina_proyectos import gente, mundo, ver, ver_r  # noqa: F401
from test_fechas_de_proyecto import (  # noqa: F401  (los ayudantes de la parte 4, y el fixture `uno`)
    GENERICOS_MEDIDOS, _PoolPg, _aplicar_migracion, _correr, _fila, _huella_de_edicion, _huellas,
    _poner, _py_del_repo, _texto_de, pagina_ver, uno)
import acciones.crud as crud
import config
import db.db as db
from _navegador import Navegador
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO
LARGO = db.LARGO_CARPETA_PROYECTO
DRIVE = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz?usp=sharing"
RUTA = "/Users/estudio/Proyectos/Disco Uno"


def mandar(pid, campos, *, chat="dueno", files=None):
    """Un POST al panel; `chat=None` sin sesión; `chat='ver'` con la sesión de solo ver."""
    c = Navegador(panel.app, base_url="https://testserver")
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c.post(f"/proyectos/{pid}/carpeta", data=campos, files=files, follow_redirects=False)


# ═══════════════════════════════════════════════════════════════════════
# 1. LA PUERTA DEL `href`
# ═══════════════════════════════════════════════════════════════════════

def _esquema_que_leeria_un_navegador(valor: str):
    """Oráculo INDEPENDIENTE de la puerta: con qué esquema trataría un navegador esta cadena puesta en
    un `href` (regla de la especificación de URL, leída por mí): se quitan los caracteres de control y
    el espacio de los dos extremos, se quitan tabulaciones y saltos de línea de adentro, y el esquema
    es lo que hay antes de los primeros dos puntos si empieza con letra y sigue con letras, cifras,
    `+`, `-` o `.`. `None` = no tiene esquema: es una dirección RELATIVA y se resuelve contra la
    página de Lucy."""
    t = re.sub(r"^[\x00-\x20]+|[\x00-\x20]+$", "", valor)
    t = re.sub(r"[\t\n\r]", "", t)
    m = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", t)
    return m.group(1).lower() if m else None


def _id(v):
    """Un id estable para `parametrize` (el `repr` de un `object()` cambia entre procesos y `-n 6` no
    se pone de acuerdo en qué pruebas hay)."""
    return repr(v)[:60] if isinstance(v, (str, int, float, bytes, list, tuple, dict, type(None))) else type(v).__name__


_ENLACES_QUE_VALEN = [
    DRIVE,
    "http://servidor.local/proyectos/disco-uno",
    "https://x.com",
    "HTTPS://Drive.Example.Test/Carpeta",
    "HtTp://x.test/a",
    "https://x.test/a?b=c&d=e#f",
    "https://x.test/a%20b/c%2Fd",
    "https://usuario@x.test/a",
    "https://x.test:8443/a",
    "https://x.test:/a:b:c",                 # puerto vacío: un navegador lo acepta
    "http://192.168.1.20/compartida",
    "http://[::1]/a",
    "https://x.test/carpeta/canción/日本",
    "https://x.test/" + "a" * 100,
]

_PREFIJOS_RAROS = [" ", "  ", "\t", "\n", "\r\n", "\x00", "\x01", "\x1f", "\x7f", "\x80", "\u00a0", "\u1680", "\u2003",
                   "\u200b", "\u200e", "\u202e", "\u2028", "\u2029", "\u3000", "\ufeff", "\ufffe", "\U000e0001"]

_NO_ENLACES = (
    ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "JAVASCRIPT:alert(1)", "java\tscript:alert(1)",
     "java\nscript:alert(1)", "javascript://x.test/%0aalert(1)", "data:text/html,<script>alert(1)</script>",
     "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==", "vbscript:msgbox(1)", "blob:https://x.test/uuid",
     "file:///Users/estudio/Proyectos", "file://servidor/compartida", "FILE:///C:/x", "smb://servidor/compartida",
     "afp://servidor/x", "ftp://servidor/x", "ssh://x.test/a", "mailto:a@x.test", "tel:+18095550000",
     "about:blank", "chrome://settings", "x-apple.systempreferences:", "ws://x.test/a", "wss://x.test/a",
     "//x.test/carpeta", "///x.test/carpeta", "\\\\servidor\\compartida", "\\\\?\\C:\\x",
     "/Users/estudio/Proyectos/Disco Uno", "/", "~/Documentos/Disco", "C:\\Proyectos\\Disco Uno", "D:/Proyectos",
     "../x", "./x", "x.test/a", "drive.google.com/drive/folders/abc", "Proyectos/Disco Uno",
     "http:/x.test", "https:/x.test", "http:x.test", "https:x.test", "http//x.test", "https//x.test", "http:\\\\x.test",
     "https:\\\\x.test", "http:", "https:", "http://", "https://", "http:///x", "https:///x", "http://:80", "https://@",
     "https://@/", "http://?a", "https://#a", "http://[::1", "http://::1]/", "http://[x]/", "http://host:99999/",
     "http://host:abc/", "http://host:-1/", "ｈｔｔｐ://x.test", "http：//x.test", "ｈｔｔｐｓ://x.test",
     "xhttp://x.test", "http://x.test\\@evil.test", "https://x.test/a\\b", "https://x.test/a b", "https://x.test/a\tb",
     "https://x.test/a\nb", "https://x.test/a\rb", "https://x.test/a\x00b", "https://x.test/a\x7fb", "https://x.test/a\u00a0b",
     "https://x.test/a\u200bb", "https://x.test/a\u202eb", "https://x.test/a\u2028b", "https://x.test/a\ufeffb",
     "https://x.test/a\"b", "https://x.test/a'b", "https://x.test/a<b", "https://x.test/a>b", "https://x.test/a`b",
     "https://x.test/\"><img src=x onerror=alert(1)>", "https://x.test/' onmouseover='alert(1)",
     "https://x.test/ onmouseover=alert(1)", "https://x.test/\njavascript:alert(1)", "https://x.test/</a><script>alert(1)</script>",
     "https://" + "a" * LARGO,                      # más larga de lo que la puerta de escribir deja
     "", " ", "\n"]
    + [p + "https://x.test/a" for p in _PREFIJOS_RAROS]
    + [p + "http://x.test/a" for p in _PREFIJOS_RAROS]
    + ["https://x.test/a" + p for p in _PREFIJOS_RAROS]
    + [p + "javascript:alert(1)" for p in _PREFIJOS_RAROS]
    + [None, 0, 1, True, False, 1.5, b"https://x.test", ["https://x.test"], ("https://x.test",), {"a": 1}, object()]
)


@pytest.mark.parametrize("valor", _ENLACES_QUE_VALEN)
def test_la_puerta_del_href_deja_pasar_una_direccion_http_o_https_entera(valor):
    assert db.enlace_de_carpeta(valor) == valor              # tal cual: no arregla ni recorta nada


@pytest.mark.parametrize("valor", _NO_ENLACES, ids=_id)
def test_la_puerta_del_href_rechaza_todo_lo_demas(valor):
    assert db.enlace_de_carpeta(valor) is None


def test_un_enlace_de_exactamente_el_largo_maximo_pasa_y_uno_mas_no():
    base = "https://x.test/"
    assert db.enlace_de_carpeta(base + "a" * (LARGO - len(base))) is not None
    assert db.enlace_de_carpeta(base + "a" * (LARGO - len(base) + 1)) is None


def test_cada_caracter_de_control_o_de_espacio_en_cualquier_sitio_cae_del_lado_estricto():
    """Los 0x110000 caracteres de Unicode, uno por uno, puestos al final y en medio de una dirección
    que sin él es buena: si es de las categorías C* o Z*, o es una comilla, un ángulo, un acento grave
    o una barra invertida, la puerta dice que no. (Recorre TODO Unicode: no es una lista tecleada.)"""
    buenos = de_mas = 0
    for cp in range(0x110000):
        c = chr(cp)
        malo = unicodedata.category(c)[0] in ("C", "Z") or c in "\"'<>`\\"
        buenos += not malo
        for v in ("https://x.test/a" + c, "https://x.test/a" + c + "b"):
            if malo:
                assert db.enlace_de_carpeta(v) is None, (hex(cp), v)
            else:
                de_mas += db.enlace_de_carpeta(v) is not None
    # y el resto sí pasa, los DOS sitios de cada uno: la puerta no está cerrada de más por otra razón
    assert buenos > 100_000 and de_mas == 2 * buenos, (buenos, de_mas)


_TROZOS = ["http://", "https://", "HTTP://", "HtTpS://", "javascript:", "data:text/html,", "file:///", "smb://", "//", "/", "\\",
           "~/", "C:\\", " ", "  ", "\t", "\n", "\r", "\x00", "\x1f", "\x7f", "\u00a0", "\u200b", "\ufeff", "\u2028", "\u202e",
           "x", "drive.google.com", "/folders/abc", "?a=b&c=d", "#frag", "\"", "'", "<", ">", "`", "%0a", "%20", ":80", "@",
           "[::1]", "[::1", ":99999", ":abc", "é", "日本", "a.b", "..", "-", "_", ".", ";", ",", "(", ")", "=", "&"]


def _inventados(n: int, semilla: int = 20261008):
    azar = random.Random(semilla)
    for _ in range(n):
        trozos = [azar.choice(_TROZOS) for _ in range(azar.randint(1, 7))]
        if azar.random() < 0.5:                                  # la mitad empieza como una dirección de verdad
            trozos.insert(0, azar.choice(["http://", "https://", "HTTP://", "HtTpS://"]))
        yield "".join(trozos)


def test_la_puerta_del_href_con_entradas_inventadas_contra_un_oraculo_independiente():
    """Para 40 000 textos armados con trozos raros: (1) lo que pasa es el mismo texto, empieza por
    `http(s)://`, no tiene un solo carácter de control, espacio ni invisible, y el analizador de
    direcciones lo ve como http(s) con servidor; (2) lo que un navegador trataría con OTRO esquema, o
    sin esquema (o sea, contra la página de Lucy), NO pasa. Y las dos salidas ocurren (el lado
    estricto es alcanzable, y la puerta no está cerrada de más)."""
    from urllib.parse import urlsplit
    pasan = rechazan = relativos_o_raros = 0
    for texto in _inventados(40_000):
        r = db.enlace_de_carpeta(texto)
        esquema = _esquema_que_leeria_un_navegador(texto)
        if r is None:
            rechazan += 1
            continue
        pasan += 1
        assert r == texto
        assert re.match(r"https?://", texto, re.I), texto
        assert esquema in ("http", "https"), (texto, esquema)
        assert not any(c.isspace() or unicodedata.category(c)[0] in "CZ" for c in texto), texto
        partes = urlsplit(texto)
        assert partes.scheme in ("http", "https") and partes.hostname, texto
        assert not any(c in texto for c in "\"'<>`\\"), texto
    for texto in _inventados(40_000, semilla=7):
        if _esquema_que_leeria_un_navegador(texto) not in ("http", "https"):
            relativos_o_raros += 1
            assert db.enlace_de_carpeta(texto) is None, texto
    assert pasan > 500 and rechazan > 5000 and relativos_o_raros > 5000, (pasan, rechazan, relativos_o_raros)


def test_lo_que_la_puerta_no_sabe_clasificar_cae_del_lado_estricto(monkeypatch):
    """El cubo estricto es alcanzable con una entrada que la puerta no sabe clasificar: si el analizador
    de direcciones falla con un error (o dice un esquema raro), el resultado es `None`, nunca el valor."""
    import db.db as modulo

    def falla(_):
        raise ValueError("no sé")
    monkeypatch.setattr(modulo, "urlsplit", falla)
    assert db.enlace_de_carpeta(DRIVE) is None

    class Raro:
        scheme, netloc, hostname = "javascript", "x", "x"
        port = 80
    monkeypatch.setattr(modulo, "urlsplit", lambda _: Raro())
    assert db.enlace_de_carpeta(DRIVE) is None


# ═══════════════════════════════════════════════════════════════════════
# 2. LA PUERTA DE ESCRIBIR
# ═══════════════════════════════════════════════════════════════════════

_QUE_VALEN = [
    (DRIVE, DRIVE), (RUTA, RUTA), ("  " + RUTA + "  ", RUTA), (RUTA + "\n", RUTA), ("\t" + RUTA, RUTA),
    ("C:\\Proyectos\\Disco Uno", "C:\\Proyectos\\Disco Uno"), ("\\\\servidor\\compartida\\x", "\\\\servidor\\compartida\\x"),
    ("smb://servidor/compartida", "smb://servidor/compartida"), ("~/Disco", "~/Disco"), ("file:///Users/x", "file:///Users/x"),
    ("javascript:alert(1)", "javascript:alert(1)"),                    # se GUARDA: lo que no se hace es pintarlo como enlace
    ("2026-10-08", "2026-10-08"), ("日本のフォルダ", "日本のフォルダ"), ("a" * LARGO, "a" * LARGO),
    (None, None), ("", None), ("   ", None), ("\n", None), ("\u00a0", None),
]
_QUE_NO = [
    ("a" * (LARGO + 1), "largo"), ("\n".join(["x"] * 3), "caracteres"), ("a\nb", "caracteres"), ("a\rb", "caracteres"),
    ("a\tb", "caracteres"), ("a\x00b", "caracteres"), ("a\x1fb", "caracteres"), ("a\x7fb", "caracteres"),
    ("\ud800", "caracteres"), ("a\ud800b", "caracteres"),
    (20261008, "tipo"), (True, "tipo"), (False, "tipo"), (1.5, "tipo"), (b"/Users/x", "tipo"), (["/Users/x"], "tipo"),
    ({"a": 1}, "tipo"), (("/Users/x",), "tipo"),
]


@pytest.mark.parametrize("valor,esperado", _QUE_VALEN, ids=_id)
def test_la_puerta_de_escribir_deja_pasar_texto_de_una_linea(valor, esperado):
    assert db.carpeta_de_proyecto_que_vale(valor) == esperado


@pytest.mark.parametrize("valor,clave", _QUE_NO, ids=_id)
def test_la_puerta_de_escribir_rechaza_con_su_clave(valor, clave):
    with pytest.raises(db.CarpetaDeProyectoNoVale) as e:
        db.carpeta_de_proyecto_que_vale(valor)
    assert e.value.clave == clave
    if isinstance(valor, str) and len(valor) > 3:
        assert valor not in str(e.value)            # el mensaje no repite lo que se pidió


def test_la_puerta_de_escribir_con_entradas_inventadas_es_idempotente_y_no_deja_nada_raro():
    buenos = malos = 0
    for texto in _inventados(20_000, semilla=99):
        try:
            r = db.carpeta_de_proyecto_que_vale(texto)
        except db.CarpetaDeProyectoNoVale as e:
            malos += 1
            assert e.clave in ("caracteres", "largo", "tipo")
            continue
        buenos += 1
        assert r is None or (len(r) <= LARGO and r == r.strip() and "\n" not in r and "\x00" not in r)
        assert db.carpeta_de_proyecto_que_vale(r) == r
    assert buenos > 1000 and malos > 1000


def test_la_columna_tiene_puerta_y_frase_de_deshacer():
    assert crud.PUERTAS["proyectos"]["carpeta"] is db.carpeta_de_proyecto_que_vale
    assert ("proyectos", "carpeta") in crud._VUELVE_A
    assert all((t, c) in crud._VUELVE_A for t, cols in crud.PUERTAS.items() for c in cols)


# ── Por `crud.editar`, que es por donde entran el panel y Telegram ─────────────────

def test_editar_guarda_la_carpeta_limpia_y_deja_la_huella(uno):
    _correr(crud.editar("proyectos", 1, {"carpeta": "  " + RUTA + "\n"}, motivo="x", actor="panel"))
    assert _fila(uno)["carpeta"] == RUTA
    h, = _huellas(uno)
    assert h["accion"] == "editar" and h["actor"] == "panel"


@pytest.mark.parametrize("valor,clave", _QUE_NO, ids=_id)
def test_editar_rechaza_sin_escribir_nada_ni_dejar_huella(uno, valor, clave):
    antes = _fila(uno)
    with pytest.raises(ValueError) as e:
        _correr(crud.editar("proyectos", 1, {"carpeta": valor}, motivo="x"))
    assert isinstance(e.value.__cause__, db.CarpetaDeProyectoNoVale) and e.value.__cause__.clave == clave
    assert _fila(uno) == antes and _huellas(uno) == []


@pytest.mark.parametrize("texto", ["2026-10-08", "2026-10-08T10:00", "2026-10-08 ventas", "2026-10-08T10:00:00+00:00"])
def test_un_texto_que_parece_fecha_se_guarda_como_texto(uno, texto):
    """`crud._adaptar` convierte en `datetime` el texto que arranca como fecha ISO; la carpeta lo
    esquiva (`_COLUMNAS_DE_TEXTO`): sin eso, una carpeta llamada «2026-10-08» se rechazaría como `tipo`."""
    _correr(crud.editar("proyectos", 1, {"carpeta": texto}, motivo="x"))
    assert _fila(uno)["carpeta"] == texto


def test_lo_que_ya_estaba_igual_no_escribe_ni_deja_huella(uno):
    _poner(uno, carpeta=RUTA)
    for pedido in (RUTA, "  " + RUTA + " ", RUTA + "\n"):
        despues, log_id = _correr(crud.editar("proyectos", 1, {"carpeta": pedido}, motivo="x"))
        assert log_id is None and despues["carpeta"] == RUTA
    assert _huellas(uno) == []


def test_pedir_la_carpeta_y_otra_columna_escribe_las_dos_y_una_mala_no_escribe_ninguna(uno):
    _correr(crud.editar("proyectos", 1, {"carpeta": RUTA, "nombre": "Otro nombre"}, motivo="x"))
    f = _fila(uno)
    assert (f["carpeta"], f["nombre"]) == (RUTA, "Otro nombre")
    antes = _fila(uno)
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"carpeta": "a" * (LARGO + 1), "nombre": "Tercero"}, motivo="x"))
    assert _fila(uno) == antes


def test_se_puede_quitar_la_carpeta(uno):
    _poner(uno, carpeta=DRIVE)
    despues, log_id = _correr(crud.editar("proyectos", 1, {"carpeta": ""}, motivo="x"))
    assert log_id is not None and _fila(uno)["carpeta"] is None and despues["carpeta"] is None


def test_telegram_editar_pasa_por_la_misma_puerta(uno):
    """`cerebro/agente.py` llama `crud.editar` con lo que el modelo mandó (texto): la misma puerta."""
    _correr(crud.editar("proyectos", 1, {"carpeta": DRIVE}, motivo="Tiziano lo pidió por Telegram"))
    assert _fila(uno)["carpeta"] == DRIVE
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"carpeta": "x\ny"}, motivo="x"))
    assert _fila(uno)["carpeta"] == DRIVE


def test_deshacer_rechaza_una_carpeta_que_la_puerta_no_deja(uno):
    """Volver atrás una edición cuyo `antes` ya no vale NO escribe (el éxito de `deshacer` usa
    `jsonb_populate_record`, que SQLite no tiene: aquí solo se ejercen sus rechazos)."""
    antes = _fila(uno)
    for malo in ("a" * (LARGO + 1), "a\nb", 12345):
        log_id = _huella_de_edicion(uno, {"carpeta": malo}, {"carpeta": RUTA})
        with pytest.raises(ValueError, match="la carpeta que tenía"):
            _correr(crud.deshacer(log_id))
    assert _fila(uno) == antes


def test_deshacer_solo_devuelve_la_carpeta_si_esa_edicion_la_cambio(uno):
    """Una columna con puerta vuelve atrás solo si ESA edición la cambió: una edición del nombre, cuyo
    `antes` trae una carpeta mala por casualidad, no la toca (ni la rechaza por ella)."""
    log_id = _huella_de_edicion(uno, {"nombre": "Viejo", "carpeta": "a\nb"}, {"nombre": "Disco de prueba", "carpeta": "a\nb"})
    # La carpeta no cambió en esa edición: no se valida ni se devuelve. (Que `deshacer` llegue más lejos
    # y falle por otra cosa de SQLite no es lo que se mide: lo que se mide es que no se queje de la carpeta.)
    try:
        _correr(crud.deshacer(log_id))
    except Exception as e:                                               # noqa: BLE001
        assert "la carpeta que tenía" not in str(e)


# ═══════════════════════════════════════════════════════════════════════
# 3. LA PÁGINA REAL CON VALORES HOSTILES
# ═══════════════════════════════════════════════════════════════════════

_ATRIBUTOS_QUE_NAVEGAN = {"href", "src", "action", "formaction", "data", "poster", "cite", "ping", "srcset", "background",
                          "xlink:href", "manifest", "longdesc", "profile", "usemap", "codebase", "archive"}


class _Etiquetas(HTMLParser):
    """Todas las etiquetas de abrir de la página, con sus atributos (`&amp;` ya deshecho)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.etiquetas: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.etiquetas.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def _etiquetas(html: str) -> list[tuple[str, dict]]:
    p = _Etiquetas()
    p.feed(html)
    p.close()
    return p.etiquetas


def _esquema_de_etiquetas(html: str) -> Counter:
    """Qué etiquetas trae la página (con su clase, sin los valores): si un valor hostil colara una
    etiqueta o un atributo nuevos, esta cuenta cambia respecto a la de un valor inocente."""
    return Counter((t, a.get("class", ""), tuple(sorted(a))) for t, a in _etiquetas(html))


def _el_valor_en_atributos(html: str, valor: str):
    """Las etiquetas que llevan `valor` (o parte de él, ya sin escapes) en CUALQUIER atributo."""
    chico = valor.strip()[:12] or valor
    return [(t, a) for t, a in _etiquetas(html) if any(chico and chico in v for v in a.values())]


_HOSTILES = [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "\tjavascript:alert(1)", "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///Users/estudio/Proyectos", "smb://servidor/x",
    "//evil.test/carpeta", "\\\\evil.test\\x", "/Users/estudio/Proyectos/Disco Uno", "~/Disco", "C:\\Proyectos\\Disco",
    "evil.test/carpeta", "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "' onfocus='alert(1)",
    "</code><script>alert(1)</script>", "</textarea><script>alert(2)</script>", "{{ 7*7 }}", "{% raw %}x{% endraw %}",
    "<svg/onload=alert(1)>", "&lt;ya escapado&gt; & más", "a\"b'c<d>e&f",
    "https://x.test/\"><img src=x onerror=alert(1)>", "https://x.test/' onmouseover='alert(1)",
    "https://x.test/ onmouseover=alert(1)", "https://x.test/\njavascript:alert(1)", "https://x.test/</a><script>alert(1)</script>",
    "https://x.test\\@evil.test", "http://x.test/a b", "https://", "http://[::1", "\u202ehttps://x.test/a", "\ufeffhttps://x.test/a",
    "https://x.test/a\u200bb", "https://x.test/a`b",
] + list(dict.fromkeys(t for t in _inventados(60, semilla=3) if t.strip()))

_BUENOS = [DRIVE, "https://x.test/a?b=c&d=e#f", "HTTPS://Drive.Example.Test/Carpeta", "http://192.168.1.20/compartida",
           "https://x.test/carpeta/canción/日本", "https://x.test/a%20b"]


def _pintar(m, valor, quien="casa", **consulta):
    """La página REAL de un proyecto cuya columna `carpeta` ya vale `valor` (puesta directo en la base:
    sin pasar por la puerta de escribir, para que la de PINTAR se mire sola)."""
    _poner(m, carpeta=valor)
    return ver(m, p=1, **consulta) if quien == "casa" else pagina_ver(m, p=1, **consulta)


def _atributos(html: str) -> Counter:
    """Cada atributo de cada etiqueta de la página como (etiqueta, atributo, valor)."""
    return Counter((t, k, v) for t, a in _etiquetas(html) for k, v in a.items())


@pytest.mark.parametrize("quien", ["casa", "ver"])
@pytest.mark.parametrize("valor", _HOSTILES + _BUENOS, ids=_id)
def test_en_la_pagina_real_solo_el_enlace_de_la_carpeta_lleva_el_valor_y_solo_si_la_puerta_lo_deja(uno, valor, quien):
    """La página con el valor, contra la MISMA página con un valor inocente del mismo tipo (un enlace de
    verdad, o una ruta sencilla): lo único que cambia en TODOS los atributos de TODAS las etiquetas es,
    con un enlace, el `href` del enlace de la carpeta; sin enlace, nada. Así no hace falta buscar el valor
    dentro de los atributos (un valor corto como «(» está en cualquier sitio)."""
    html = _pintar(uno, valor, quien)
    es_enlace = db.enlace_de_carpeta(valor) is not None
    inocente = _pintar(uno, "https://x.test/a" if es_enlace else "/ruta/simple", quien)
    ahora, antes = _atributos(html), _atributos(inocente)
    if es_enlace:
        assert ahora - antes == Counter({("a", "href", valor): 1}) or valor == "https://x.test/a"
        assert antes - ahora == Counter({("a", "href", "https://x.test/a"): 1}) or valor == "https://x.test/a"
        enlaces = [a for t, a in _etiquetas(html) if a.get("class") == "carpeta-enlace"]
        assert len(enlaces) == 1 and enlaces[0]["href"] == valor
        assert enlaces[0]["target"] == "_blank" and enlaces[0]["rel"] == "noopener noreferrer"
    else:
        assert ahora == antes, (ahora - antes, antes - ahora)
        assert 'class="carpeta-enlace"' not in html
    # Los enlaces de esa clase pasan por la puerta; nadie tiene un manejador de eventos; las mismas etiquetas.
    for tag, attrs in _etiquetas(html):
        if attrs.get("class") == "carpeta-enlace":
            assert es_enlace and db.enlace_de_carpeta(attrs["href"]) == attrs["href"]
        assert not [k for k in attrs if k.startswith("on")], (tag, attrs)
    assert _esquema_de_etiquetas(html) == _esquema_de_etiquetas(inocente)
    # Sin enlace sale como TEXTO escapado, en la casa y en ver (y lo que un navegador leería ahí es el texto).
    seccion = _seccion(html)
    if not es_enlace and valor:
        assert f'<code class="carpeta-texto">{escape(valor)}</code>' in seccion
        assert unescape(re.search(r'<code class="carpeta-texto">(.*?)</code>', seccion, re.S).group(1)) == valor
    # El guion de «Copiar» solo existe en la casa y solo con texto (no enlace).
    assert ('id="guion-copiar-carpeta"' in html) == (quien == "casa" and not es_enlace and bool(valor.strip()))
    if quien == "ver":
        assert "<script" not in html and "data-copiar-carpeta" not in html


def _seccion(html: str) -> str:
    """Solo el bloque de la carpeta (el CSS de la página también dice «carpeta-texto» y «copiar»)."""
    m = re.search(r'<section class="bloque carpeta-p" id="carpeta-del-proyecto">.*?</section>', html, re.S)
    return m.group(0) if m else ""


@pytest.mark.parametrize("valor", _HOSTILES, ids=_id)
def test_el_formulario_de_la_carpeta_lleva_el_valor_solo_en_su_value(uno, valor):
    html = _pintar(uno, valor, "casa", editar="carpeta")
    inocente = _pintar(uno, "/ruta/simple", "casa", editar="carpeta")
    campos = [a for t, a in _etiquetas(html) if t == "input" and a.get("name") == "carpeta"]
    assert len(campos) == 1 and campos[0]["value"] == valor and campos[0]["maxlength"] == str(LARGO)
    nuevo = _atributos(html) - _atributos(inocente)
    assert nuevo == Counter({("input", "value", valor): 1}) or valor == "/ruta/simple"
    assert _esquema_de_etiquetas(html) == _esquema_de_etiquetas(inocente)


def test_la_plantilla_decide_con_el_filtro_y_no_con_el_modelo(uno):
    """El modelo (`armar_pagina`) entrega el TEXTO guardado y nada que diga «esto es un enlace»; la
    decisión está en la plantilla, llamando a `db.enlace_de_carpeta`."""
    assert panel.plantillas.env.filters["enlace_de_carpeta"] is db.enlace_de_carpeta
    modelo = _correr(db.pagina_de_proyectos())
    llaves = set(modelo["proyectos"][1])
    assert "carpeta" in llaves and "carpeta_disponible" in llaves
    assert not [k for k in llaves if "enlace" in k or "href" in k or "url" in k]


def test_un_enlace_malo_que_el_modelo_dijera_bueno_no_llega_al_href(uno, monkeypatch):
    """Aunque alguien le pusiera al modelo una clave de enlace inventada, la plantilla no la lee:
    solo mira `m.carpeta` por el filtro."""
    original = db.pagina_de_proyectos

    async def con_clave_inventada(*a, **k):
        modelo = await original(*a, **k)
        for m in modelo["proyectos"].values():
            m["carpeta_enlace"] = m["enlace_carpeta"] = m["carpeta_href"] = "javascript:alert(1)"
        return modelo
    monkeypatch.setattr(db, "pagina_de_proyectos", con_clave_inventada)
    html = _pintar(uno, RUTA)
    assert "javascript:" not in html and 'class="carpeta-enlace"' not in html


def test_la_plantilla_sola_dibuja_el_enlace_solo_con_lo_que_pasa_el_filtro():
    """Se renderiza el bloque con el entorno de Jinja REAL de la página y valores sueltos, sin la ruta."""
    plantilla = panel.plantillas.env.from_string(
        "{% set enlace_carpeta = carpeta|enlace_de_carpeta %}"
        "{% if enlace_carpeta %}<a href=\"{{ enlace_carpeta }}\">E</a>{% else %}<code>{{ carpeta }}</code>{% endif %}")
    assert plantilla.render(carpeta=DRIVE) == f'<a href="{escape(DRIVE)}">E</a>'
    assert plantilla.render(carpeta="javascript:alert(1)") == "<code>javascript:alert(1)</code>"
    assert plantilla.render(carpeta=None) == "<code>None</code>"


# ── El censo de la plantilla y de todo el código que toca la carpeta ───────────────

def test_en_ninguna_plantilla_la_carpeta_llega_a_un_href_sin_pasar_por_el_filtro():
    """Todo atributo de toda plantilla que lleve una expresión `{{ … }}` con «carpeta» adentro: el único
    que puede ir en un `href` es `{{ enlace_carpeta }}` (lo que devolvió el filtro). Las demás
    expresiones con «carpeta» (el `value` del campo, el texto, el largo) no están en atributos que navegan."""
    con_expresion = []
    for ruta in sorted((_ROOT / "web" / "plantillas").glob("*.html")):
        texto = ruta.read_text(encoding="utf-8")
        if ruta.name != "proyectos.html":
            assert "carpeta" not in texto.lower(), ruta.name        # ninguna otra plantilla pinta este dato
        for atributo in re.finditer(r"\b([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')", re.sub(r"\{#.*?#\}", "", texto, flags=re.S)):
            for expresion in re.findall(r"\{\{\s*([^}]*?)\s*\}\}", atributo.group(2)):
                if re.search(r"carpeta", expresion):
                    con_expresion.append((atributo.group(1), expresion))
    navegan = [(k, e) for k, e in con_expresion if k in _ATRIBUTOS_QUE_NAVEGAN]
    assert navegan == [("href", "enlace_carpeta")], navegan
    assert sorted(set(con_expresion)) == sorted([("href", "enlace_carpeta"), ("value", "m.carpeta or ''"),
                                                 ("maxlength", "largo_carpeta")]), con_expresion
    fuente = (_ROOT / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    assigns = re.findall(r"\{%-?\s*set\s+enlace_carpeta\s*=\s*([^%]*?)\s*-?%\}", fuente)
    assert assigns == ["m.carpeta|enlace_de_carpeta"]
    # y dónde más se escribe algo con «carpeta» dentro de `{{ }}`: cada expresión es una conocida.
    expresiones = re.findall(r"\{\{\s*([^}]*?carpeta[^}]*?)\s*\}\}", re.sub(r"\{#.*?#\}", "", fuente, flags=re.S))
    permitidas = {"largo_carpeta", "m.carpeta or ''", "m.carpeta", "enlace_carpeta", "'Cambiar' if m.carpeta else 'Poner'"}
    assert {e for e in expresiones if "m.id" not in e} <= permitidas, expresiones
    # `m.carpeta` suelto (el texto) solo aparece dentro de `<code class="carpeta-texto">`, y `m.carpeta or ''`
    # solo en el `value` del campo del formulario.
    assert re.findall(r'(<code class="carpeta-texto">)\{\{ m\.carpeta \}\}', fuente) == ['<code class="carpeta-texto">']
    assert len(re.findall(r"\{\{ m\.carpeta \}\}", fuente)) == 1
    assert re.findall(r'(value=")\{\{ m\.carpeta or \'\' \}\}', fuente) == ['value="']
    assert len(re.findall(r"\{\{ m\.carpeta or '' \}\}", fuente)) == 1


def test_los_archivos_de_codigo_que_nombran_la_columna_son_estos():
    """Quién nombra la COLUMNA (no la palabra «carpeta», que otros archivos usan por otra cosa): el texto
    `proyectos.carpeta`, la cadena «carpeta» sola (como clave o nombre de columna), `.carpeta` o las
    funciones de este trabajo. Un archivo más que lo haga tiene que declararse aquí."""
    patron = re.compile(r"proyectos\.carpeta|[\"']carpeta[\"']|\.carpeta\b|carpeta_de_proyecto|carpeta_guardada|"
                        r"carpetas_de_proyectos|enlace_de_carpeta|LARGO_CARPETA_PROYECTO")
    nombran = {str(a.relative_to(_ROOT)) for a in _py_del_repo() if patron.search(a.read_text(encoding="utf-8"))}
    # `db/db.py` (las dos puertas y la lectura), `acciones/crud.py` (la puerta en PUERTAS y el trato del
    # texto), `web/app.py` (la ruta y el filtro), `cerebro/consultar.py` (la nota para Lucy por Telegram).
    assert nombran == {"db/db.py", "acciones/crud.py", "web/app.py", "cerebro/consultar.py"}, nombran


def test_ningun_sql_escrito_a_mano_nombra_la_columna_y_los_genericos_son_los_medidos():
    escritores = set()
    genericos = set()
    insertan = set()
    for archivo in _py_del_repo():
        partes = archivo.relative_to(_ROOT).parts
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                texto = _texto_de(n)
                if not texto:
                    continue
                for m in re.finditer(r"UPDATE\s+(proyectos\b|\{\})\s*(?:\w+\s+)?SET\s+(.*?)(?:WHERE|$)", texto, re.I | re.S):
                    if m.group(1) == "{}":
                        genericos.add(f"{'/'.join(partes)}::{fn.name}")
                    if re.search(r"\bcarpeta\s*=", m.group(2)):
                        escritores.add(fn.name)
                for m in re.finditer(r"INSERT\s+INTO\s+(proyectos\b|\{\})\s*\(([^)]*)\)", texto, re.I | re.S):
                    insertan.add(f"{'/'.join(partes)}::{fn.name}")
                    assert "carpeta" not in m.group(2), (partes, fn.name)
    assert escritores == set()
    # Los que arman `UPDATE {tabla}` al vuelo son los mismos de la parte 4: o validan por las puertas
    # (`editar`, `deshacer`: ejercidas arriba con una carpeta mala) o escriben una columna fija que no es esta.
    assert genericos == GENERICOS_MEDIDOS, genericos
    assert {"db/db.py::crear_proyecto", "db/db.py::convertir_tarea_en_proyecto", "db/db.py::_buscar_o_crear"} <= insertan


def test_un_proyecto_nuevo_nace_sin_carpeta(uno):
    # `uno` ya es un proyecto insertado por la base de prueba; los tres sitios reales de creación ni nombran la columna.
    assert _fila(uno)["carpeta"] is None


def test_lucy_por_telegram_ve_la_columna_y_sabe_que_es_texto_a_mano():
    from cerebro import consultar
    bloque = " ".join(consultar.BLOQUES["proyectos"].split())
    assert "carpeta" in bloque
    assert ("proyectos", "carpeta") in consultar.NOTAS_DE_COLUMNA
    assert "proyectos" in consultar.TABLAS_DE_TIZIANO
    assert "carpeta" in db.columnas_declaradas()["proyectos"]


# ═══════════════════════════════════════════════════════════════════════
# 4. QUIÉN VE Y QUIÉN ESCRIBE
# ═══════════════════════════════════════════════════════════════════════

def test_la_casa_ve_el_bloque_aunque_no_haya_carpeta_y_puede_ponerla(uno):
    html = ver(uno, p=1)
    assert '<h2>Carpeta del proyecto</h2>' in html and "Todavía no hay carpeta." in html
    assert 'href="/proyectos?p=1&amp;editar=carpeta#carpeta-del-proyecto"' in html and ">Poner</a>" in html
    form = ver(uno, p=1, editar="carpeta")
    assert 'action="/proyectos/1/carpeta"' in form and 'name="carpeta"' in form and f'maxlength="{LARGO}"' in form
    assert "Pega la dirección de la carpeta en Drive" in form
    assert "Cancelar" in form and "&amp;editar=carpeta" not in form.split('class="carpeta-editar"', 1)[1]


def test_la_casa_ve_un_enlace_con_cambiar_y_sin_copiar(uno):
    html = _pintar(uno, DRIVE)
    assert (f'<a class="carpeta-enlace" href="{escape(DRIVE)}" target="_blank" rel="noopener noreferrer">'
            'Abrir la carpeta</a>') in html
    seccion = _seccion(html)
    assert ">Cambiar</a>" in seccion and "Copiar" not in seccion and "carpeta-texto" not in seccion
    assert "solo sirve en la computadora" not in seccion and "<script" in html and html.count("<script") == 1


def test_la_casa_ve_una_ruta_como_texto_con_copiar_y_la_advertencia(uno):
    html = _pintar(uno, RUTA)
    assert f'<code class="carpeta-texto">{RUTA}</code>' in html
    assert "<button type=\"button\" class=\"btn-linea copiar\" data-copiar-carpeta>Copiar</button>" in html
    assert "Esta ruta solo sirve en la computadora donde existe la carpeta." in html
    assert 'class="carpeta-enlace"' not in _seccion(html) and "Abrir la carpeta" not in html
    assert ">Cambiar</a>" in _seccion(html)


def test_solo_ver_ve_el_enlace_o_el_texto_y_ningun_control_ni_guion(uno):
    html = pagina_ver(uno, p=1)
    assert _seccion(html) == ""                                        # sin carpeta no hay nada que leer
    _poner(uno, carpeta=DRIVE)
    html = pagina_ver(uno, p=1)
    seccion = _seccion(html)
    assert f'href="{escape(DRIVE)}"' in seccion and 'target="_blank" rel="noopener noreferrer"' in seccion
    assert "Cambiar" not in seccion and "<form" not in seccion and "<button" not in seccion
    _poner(uno, carpeta=RUTA)
    html = pagina_ver(uno, p=1)
    seccion = _seccion(html)
    assert f'<code class="carpeta-texto">{RUTA}</code>' in seccion and "Esta ruta solo sirve en la computadora" in seccion
    assert "Copiar" not in seccion and "<button" not in seccion and "<form" not in seccion
    assert "<script" not in html
    # La dirección escrita a mano no abre el formulario.
    html = pagina_ver(uno, p=1, editar="carpeta")
    assert 'name="carpeta"' not in html and "/proyectos/1/carpeta" not in html


def test_la_plantilla_sola_tampoco_dibuja_controles_de_la_carpeta_en_solo_ver(uno, monkeypatch):
    """La ruta ya apaga `editar` en solo ver; esta prueba le quita ese apoyo: la plantilla recibe
    `editar='carpeta'` con `solo_ver=True` y no puede dibujar formulario, campo, «Cambiar» ni «Copiar»."""
    original = panel.plantillas.TemplateResponse

    def con_editar(request, nombre, contexto=None, *a, **k):
        if nombre == "proyectos.html":
            assert contexto["solo_ver"] is True
            contexto = {**contexto, "editar": "carpeta"}
        return original(request, nombre, contexto, *a, **k)

    monkeypatch.setattr(panel.plantillas, "TemplateResponse", con_editar)
    for valor in (RUTA, DRIVE):
        _poner(uno, carpeta=valor)
        html = pagina_ver(uno, p=1)
        assert 'name="carpeta"' not in html and "/proyectos/1/carpeta" not in html
        assert "editar=carpeta" not in html and "Copiar" not in html and "data-copiar-carpeta" not in html
        assert "guion-copiar-carpeta" not in html


def test_solo_ver_y_sin_sesion_no_escriben(uno):
    antes = _fila(uno)
    for chat in ("ver", None):
        r = mandar(1, {"carpeta": "hackeado"}, chat=chat)
        assert r.status_code in (401, 403), (chat, r.status_code)
        assert _fila(uno) == antes and _huellas(uno) == []


def test_sin_sesion_la_pagina_no_se_ve(uno):
    _poner(uno, carpeta=DRIVE)
    c = Navegador(panel.app, base_url="https://testserver")
    r = c.get("/proyectos", params={"p": 1}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403) and DRIVE not in r.text


# ═══════════════════════════════════════════════════════════════════════
# 5. LA RUTA
# ═══════════════════════════════════════════════════════════════════════

def test_la_ruta_guarda_un_enlace_y_dice_guardada(uno):
    r = mandar(1, {"carpeta": "  " + DRIVE + " "})
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?hecho=carpeta&p=1#carpeta-del-proyecto"
    assert _fila(uno)["carpeta"] == DRIVE
    h, = _huellas(uno)
    assert h["actor"] == "panel" and h["accion"] == "editar"
    assert "Carpeta guardada." in ver_r(uno, p=1, hecho="carpeta")


def test_la_ruta_guarda_una_ruta_de_computadora(uno):
    r = mandar(1, {"carpeta": RUTA})
    assert "hecho=carpeta&" in r.headers["location"] and _fila(uno)["carpeta"] == RUTA


def test_la_ruta_quita_la_carpeta_con_el_campo_vacio_y_lo_dice(uno):
    _poner(uno, carpeta=DRIVE)
    r = mandar(1, {"carpeta": ""})
    assert r.headers["location"] == "/proyectos?hecho=carpeta_quitada&p=1#carpeta-del-proyecto"
    assert _fila(uno)["carpeta"] is None and len(_huellas(uno)) == 1
    assert "Carpeta quitada: el proyecto queda sin carpeta." in ver_r(uno, p=1, hecho="carpeta_quitada")
    r = mandar(1, {"carpeta": "   "})
    assert "error=carpeta_igual" in r.headers["location"]            # ya no había: no dice «quitada»


def test_la_ruta_no_dice_guardada_si_nada_cambio(uno):
    _poner(uno, carpeta=DRIVE)
    r = mandar(1, {"carpeta": DRIVE})
    assert "error=carpeta_igual" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _huellas(uno) == []
    assert "La carpeta ya estaba así" in ver_r(uno, p=1, error="carpeta_igual")


_RECHAZOS = [("a" * (LARGO + 1), "carpeta_largo"), ("a\nb", "carpeta_caracteres"), ("a\x00b", "carpeta_caracteres"),
             ("a\tb", "carpeta_caracteres"), ("a\rb", "carpeta_caracteres")]


@pytest.mark.parametrize("valor,clave", _RECHAZOS, ids=_id)
def test_la_ruta_rechaza_con_su_clave_y_no_escribe_nada(uno, valor, clave):
    _poner(uno, carpeta=RUTA)
    antes = _fila(uno)
    r = mandar(1, {"carpeta": valor})
    assert r.status_code == 303
    destino = r.headers["location"]
    assert f"error={clave}&" in destino and "editar=carpeta" in destino and "hecho=" not in destino
    assert _fila(uno) == antes and _huellas(uno) == []
    assert valor.strip() not in destino and valor not in destino                  # lo escrito nunca viaja
    assert "NO se guardó" in ver_r(uno, p=1, error=clave, editar="carpeta")


def test_la_ruta_rechaza_un_archivo_subido_en_vez_de_un_texto(uno):
    antes = _fila(uno)
    r = mandar(1, {}, files={"carpeta": ("c.txt", b"/Users/x", "text/plain")})
    assert "error=carpeta_" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _fila(uno) == antes and _huellas(uno) == []


def test_un_post_sin_el_campo_no_quita_la_carpeta(uno):
    _poner(uno, carpeta=DRIVE)
    antes = _fila(uno)
    r = mandar(1, {"otro": "x"})
    assert "error=carpeta_invalida&" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _fila(uno) == antes and _huellas(uno) == []
    assert "Esa carpeta no se puede guardar: NO se guardó nada." in ver_r(uno, p=1, error="carpeta_invalida")


def test_la_ruta_de_un_proyecto_que_no_esta_no_dice_guardada(uno):
    r = mandar(99, {"carpeta": DRIVE})
    assert r.headers["location"] == "/proyectos?error=proyecto"
    uno.con.execute("UPDATE proyectos SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id = 1")
    assert mandar(1, {"carpeta": DRIVE}).headers["location"] == "/proyectos?error=proyecto"
    assert _fila(uno)["carpeta"] is None


def test_si_la_base_falla_al_guardar_la_ruta_no_dice_ni_guardada_ni_no_se_guardo(uno, monkeypatch):
    class _Falla(g._Conn):
        async def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("UPDATE PROYECTOS"):
                raise RuntimeError("se cayó la base")
            return await super().execute(sql, params)

    class _P(g._Pool):
        def connection(self):
            con = self.con

            class CM:
                async def __aenter__(s):
                    return _Falla(con)

                async def __aexit__(s, *e):
                    return False
            return CM()
    monkeypatch.setattr(db, "pool", _P(uno.con))
    r = mandar(1, {"carpeta": DRIVE})
    assert "error=carpeta_base&" in r.headers["location"] and "hecho=" not in r.headers["location"]
    texto = ver_r(uno, p=1, error="carpeta_base", editar="carpeta")
    assert "no se pudo confirmar" in texto and "Carpeta guardada" not in texto and "NO se guardó" not in texto


def test_los_avisos_de_la_carpeta_no_salen_con_una_direccion_escrita_a_mano(uno):
    """Sin el recibo de un POST de verdad (la puerta de `web/avisos.py`) ni «guardada» ni un error salen."""
    from test_pagina_proyectos import ver as ver_sin_recibo
    for hecho in ("carpeta", "carpeta_quitada"):
        assert "Carpeta guardada" not in ver_sin_recibo(uno, p=1, hecho=hecho)
        assert "Carpeta quitada" not in ver_sin_recibo(uno, p=1, hecho=hecho)
    assert "ya estaba así" not in ver_sin_recibo(uno, p=1, error="carpeta_igual")


# ═══════════════════════════════════════════════════════════════════════
# 6. LA PÁGINA CON LA BASE SIN MIGRAR
# ═══════════════════════════════════════════════════════════════════════

def _ddl_sin_la_carpeta() -> str:
    ddl = b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "proyectos")[0]
    ddl = re.sub(r"\n\s*carpeta\s+TEXT,", "", ddl)
    assert "carpeta" not in ddl, ddl
    return ddl


def _sin_migrar(m, monkeypatch):
    m.con.execute("DROP TABLE proyectos")
    m.con.execute(_ddl_sin_la_carpeta())
    monkeypatch.setattr(db, "pool", _PoolPg(m.con))


def test_la_pagina_carga_con_la_base_sin_migrar_y_no_dibuja_la_carpeta(uno, monkeypatch):
    _sin_migrar(uno, monkeypatch)
    uno.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    for consulta in ({"p": 1}, {"p": 1, "editar": "carpeta"}, {}, {"g": "CDS"}):
        html = ver(uno, **consulta)
        assert "Carpeta del proyecto" not in html and "/proyectos/1/carpeta" not in html
        assert "editar=carpeta" not in html and "carpeta-p" not in html.split("</style>", 1)[1]
    assert 'id="de-que-se-trata"' in ver(uno, p=1)                    # el resto de la página sigue
    assert "Empezó" in ver(uno, p=1)                                    # y las fechas (migración anterior) también


def test_con_la_carpeta_migrada_y_las_fechas_no_la_pagina_ensena_la_carpeta(uno, monkeypatch):
    """La otra mitad de la independencia de las dos lecturas: si faltaran las columnas de las fechas (la
    migración de la parte 4) y estuviera la de la carpeta, la carpeta se ve igual y las fechas no."""
    from test_fechas_de_proyecto import _ddl_sin_las_fechas
    uno.con.execute("DROP TABLE proyectos")
    uno.con.execute(_ddl_sin_las_fechas())
    monkeypatch.setattr(db, "pool", _PoolPg(uno.con))
    uno.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    _poner(uno, carpeta=DRIVE)
    html = ver(uno, p=1)
    assert 'class="carpeta-enlace"' in html and f'href="{escape(DRIVE)}"' in html
    assert "Empezó" not in html and "Cambiar las fechas" not in html


def test_sin_migrar_la_lectura_dice_no_disponible_y_otro_error_no_se_traga(uno, monkeypatch):
    assert _correr(db.carpetas_de_proyectos()) == {1: None}
    _sin_migrar(uno, monkeypatch)
    assert _correr(db.carpetas_de_proyectos()) is None
    from _doble_postgres import ErrorSQL
    for estado in ("08006", "57014", "23505"):
        class _Cur:
            async def execute(self, *a):
                raise ErrorSQL(estado)

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cur()

        class _P:
            def connection(self):
                class CM:
                    async def __aenter__(s):
                        return _Conn()

                    async def __aexit__(s, *e):
                        return False
                return CM()
        monkeypatch.setattr(db, "pool", _P())
        with pytest.raises(ErrorSQL):
            _correr(db.carpetas_de_proyectos())
    class _Sin:                                      # un error que no trae `sqlstate` tampoco se traga
        def cursor(self, row_factory=None):
            class C:
                async def execute(self, *a):
                    raise RuntimeError("otra cosa")
            return C()

    class _P2:
        def connection(self):
            class CM:
                async def __aenter__(s):
                    return _Sin()

                async def __aexit__(s, *e):
                    return False
            return CM()
    monkeypatch.setattr(db, "pool", _P2())
    with pytest.raises(RuntimeError):
        _correr(db.carpetas_de_proyectos())


def test_sin_migrar_una_escritura_a_mano_se_rechaza_diciendo_que_falta_actualizar(uno, monkeypatch):
    _sin_migrar(uno, monkeypatch)
    uno.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    r = mandar(1, {"carpeta": DRIVE})
    assert r.status_code == 303 and "error=carpeta_sin_columna" in r.headers["location"]
    assert "hecho=" not in r.headers["location"] and _huellas(uno) == []
    assert "falta actualizar la base de datos" in ver_r(uno, p=1, error="carpeta_sin_columna")


def test_armar_pagina_sin_carpetas_marca_no_disponible_aunque_el_proyecto_exista():
    proyecto = dict(id=1, nombre="Uno", descripcion=None, estado="activo", area=None, creado_en=_creado(),
                    responsable_chat_id=None, cliente_nombre=None)
    for carpetas in (None, {}, {2: "x"}):
        m = db.armar_pagina([], [proyecto], [], [], [], {}, _hoy(), carpetas=carpetas)["proyectos"][1]
        assert m["carpeta_disponible"] is False and m["carpeta"] is None
    m = db.armar_pagina([], [proyecto], [], [], [], {}, _hoy(), carpetas={1: None})["proyectos"][1]
    assert m["carpeta_disponible"] is True and m["carpeta"] is None


def _creado():
    from test_pagina_proyectos import CREADO
    return CREADO


def _hoy():
    from test_pagina_proyectos import HOY
    return HOY


# ═══════════════════════════════════════════════════════════════════════
# 7. EL GUION DE «COPIAR», EJECUTADO
# ═══════════════════════════════════════════════════════════════════════

hay_osascript = pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="no hay osascript (JavaScriptCore de macOS): el guion de «Copiar» no se ejecuta aquí")

# El `document`, el `window` y el `navigator` de mentira. Qué afirman (y de dónde lo saqué):
#  · `document.addEventListener(tipo, f)` guarda UN oyente por tipo: así lo usa el guion.
#  · el evento trae `target.closest(selector)`: devuelve el botón si el selector es el del botón, y
#    nada si no (así lo hace `Element.closest`).
#  · `boton.parentNode.querySelector("code.carpeta-texto")` devuelve el `<code>`; su `textContent` es
#    el texto del HTML ya SIN escapes (lo que un navegador entrega), que la prueba saca del HTML servido.
#  · `navigator.clipboard.writeText(t)` devuelve algo con `then(ok, fallo)`; el doble llama a `ok` o a
#    `fallo` enseguida. Un `Promise` real lo hace después, sin importar para este guion.
_ARNES_COPIAR = """
var __t = []; var __llamadas = {escribio: [], rangos: 0, selecciones: [], quitadas: 0};
function setTimeout(f, ms) { __t.push({f: f, ms: ms}); return __t.length; }
function correrTemporizadores() { var cola = __t; __t = []; for (var i = 0; i < cola.length; i++) cola[i].f(); }
var __oyentes = {};
var __texto = {textContent: %(TEXTO)s};
var __cuerpo = {querySelector: function (s) { return s === "code.carpeta-texto" ? __texto : null; }};
var __boton = {textContent: "Copiar", parentNode: __cuerpo};
function __evento(sobreElBoton) {
  return {target: {closest: function (s) { return (sobreElBoton && s === "[data-copiar-carpeta]") ? __boton : null; }}};
}
var __seleccion = {removeAllRanges: function () { __llamadas.quitadas++; },
                   addRange: function (r) { __llamadas.selecciones.push(r.nodo === __texto); }};
var window = {getSelection: function () { return __seleccion; }};
var document = {
  documentElement: {classList: {puestas: [], add: function (c) { this.puestas.push(c); }}},
  addEventListener: function (tipo, f) { __oyentes[tipo] = f; },
  createRange: function () { __llamadas.rangos++; return {nodo: null, selectNodeContents: function (n) { this.nodo = n; }}; }
};
%(NAVEGADOR)s
"""

_PORTAPAPELES_OK = ("var navigator = {clipboard: {writeText: function (t) { __llamadas.escribio.push(t);"
                    " return {then: function (ok, fallo) { ok(); }}; }}};")
_PORTAPAPELES_RECHAZA = ("var navigator = {clipboard: {writeText: function (t) { __llamadas.escribio.push(t);"
                         " return {then: function (ok, fallo) { fallo(); }}; }}};")
_PORTAPAPELES_LANZA = ("var navigator = {clipboard: {writeText: function (t) { __llamadas.escribio.push(t);"
                       " throw new Error('sin permiso'); }}};")
_SIN_PORTAPAPELES = "var navigator = {};"
_FOTO = ("JSON.stringify({boton: __boton.textContent, escribio: __llamadas.escribio, rangos: __llamadas.rangos,"
         " selecciones: __llamadas.selecciones, quitadas: __llamadas.quitadas, clases: document.documentElement.classList.puestas,"
         " temporizadores: __t.length, tipos: Object.keys(__oyentes)})")


def _guion_y_texto(m, valor):
    html = _pintar(m, valor)
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert len(guiones) == 2, len(guiones)                 # el de siempre y el de «Copiar», que es aparte
    assert 'id="guion-copiar-carpeta"' in html.split("</script>", 1)[1]
    texto = unescape(re.search(r'<code class="carpeta-texto">(.*?)</code>', html, re.S).group(1))
    assert texto == valor.strip()
    return guiones[1], texto


def _jxa(guion, navegador, texto, escenario):
    arnes = (_ARNES_COPIAR % {"TEXTO": json.dumps(texto), "NAVEGADOR": navegador}) + guion + "\n" + escenario
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", arnes], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


_RUTAS_DEL_GUION = [RUTA, "C:\\Proyectos\\Disco Uno", "/Users/a & b/<x> \"y\" 'z'", "\\\\servidor\\compartida", "~/Música/Año 2026", "javascript:alert(1)"]


@hay_osascript
@pytest.mark.parametrize("valor", _RUTAS_DEL_GUION)
def test_js_el_clic_copia_el_texto_exacto_y_dice_copiado_solo_si_se_copio(uno, valor):
    guion, texto = _guion_y_texto(uno, valor)
    r = _jxa(guion, _PORTAPAPELES_OK, texto, "__oyentes.click(__evento(true));" + _FOTO)
    assert r["escribio"] == [texto] and r["boton"] == "Copiado" and r["rangos"] == 0 and r["selecciones"] == []
    assert r["clases"] == ["con-js"] and r["tipos"] == ["click"] and r["temporizadores"] == 1
    # y pasado el tiempo el botón vuelve a decir «Copiar»
    r = _jxa(guion, _PORTAPAPELES_OK, texto, "__oyentes.click(__evento(true)); correrTemporizadores();" + _FOTO)
    assert r["boton"] == "Copiar"


@hay_osascript
@pytest.mark.parametrize("navegador", [_PORTAPAPELES_RECHAZA, _PORTAPAPELES_LANZA, _SIN_PORTAPAPELES],
                         ids=["rechaza", "lanza", "no_hay"])
def test_js_si_el_navegador_no_copia_nunca_dice_copiado_y_deja_el_texto_seleccionado(uno, navegador):
    guion, texto = _guion_y_texto(uno, RUTA)
    r = _jxa(guion, navegador, texto, "__oyentes.click(__evento(true));" + _FOTO)
    assert r["boton"] == "Cópialo a mano" and "Copiado" not in json.dumps(r)
    assert r["rangos"] == 1 and r["selecciones"] == [True] and r["quitadas"] == 1


@hay_osascript
def test_js_un_clic_en_otro_sitio_no_hace_nada(uno):
    guion, texto = _guion_y_texto(uno, RUTA)
    r = _jxa(guion, _PORTAPAPELES_OK, texto, "__oyentes.click(__evento(false));" + _FOTO)
    assert r["escribio"] == [] and r["boton"] == "Copiar" and r["temporizadores"] == 0
    r = _jxa(guion, _PORTAPAPELES_OK, texto, "__oyentes.click({target: null}); __oyentes.click({target: {}});" + _FOTO)
    assert r["escribio"] == [] and r["boton"] == "Copiar"


def test_el_guion_de_copiar_no_manda_nada_ni_toca_formularios_ni_la_direccion(uno):
    guion = re.sub(r"/\*.*?\*/", "", _guion_y_texto(uno, RUTA)[0], flags=re.S)
    assert re.search(r"fetch\(|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|\.submit\(|requestSubmit|"
                     r"location\s*[.=]|\.action|FormData|innerHTML|eval\(|document\.write|\.href", guion) is None
    assert re.findall(r'addEventListener\("(\w+)"', guion) == ["click"]


def test_el_guion_de_marcar_tareas_sigue_siendo_el_primero_y_el_unico_de_siempre(uno):
    html = _pintar(uno, RUTA)
    guiones = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert "marcarSinSaltar" in guiones[0] and "copiar" not in guiones[0].lower().replace("copiar el", "")
    assert "marcarSinSaltar" not in guiones[1]
    # Con un enlace, o sin carpeta, la página lleva el guion de siempre y nada más.
    assert len(re.findall(r"<script", _pintar(uno, DRIVE))) == 1
    assert len(re.findall(r"<script", _pintar(uno, None))) == 1


def test_el_boton_de_copiar_esta_fuera_de_los_formularios_y_sin_js_no_se_ve(uno):
    html = _pintar(uno, RUTA)
    assert "<form" not in html.split('id="carpeta-del-proyecto"', 1)[1].split("</section>", 1)[0]
    assert re.search(r"\.carpeta-p \.copiar\{display:none\}", html) and re.search(r"\.con-js \.carpeta-p \.copiar\{display:inline-block\}", html)


# ═══════════════════════════════════════════════════════════════════════
# 8. LA MIGRACIÓN, EJECUTADA
# ═══════════════════════════════════════════════════════════════════════

_MIGRACION = _ROOT / "db" / "migrations" / "2026-10-08_proyectos_carpeta.sql"


def _tabla_vieja() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER)")
    con.execute(_ddl_sin_la_carpeta())
    for pid, borrado in ((1, None), (2, None), (3, "2026-04-01T00:00:00+00:00")):
        con.execute("INSERT INTO proyectos (id, nombre, creado_en, borrado_en) VALUES (?,?,?,?)",
                    (pid, f"p{pid}", "2026-03-01T12:00:00+00:00", borrado))
    return con


def test_la_migracion_se_ejecuta_y_no_toca_ninguna_fila():
    con = _tabla_vieja()
    antes = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    saltadas = _aplicar_migracion(con, _MIGRACION.read_text(encoding="utf-8"))
    columnas = [c[1] for c in con.execute("PRAGMA table_info(proyectos)")]
    assert columnas[-1] == "carpeta" and columnas.count("carpeta") == 1
    despues = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    assert [f[:-1] for f in despues] == antes and all(f[-1] is None for f in despues)
    assert con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 3        # el conteo del ensayo
    assert con.execute("SELECT count(carpeta) FROM proyectos").fetchone()[0] == 0
    assert [s.split()[0].upper() for s in saltadas] == ["BEGIN", "COMMENT", "COMMIT"]


def test_la_migracion_corrida_dos_veces_deja_lo_mismo():
    con = _tabla_vieja()
    texto = _MIGRACION.read_text(encoding="utf-8")
    _aplicar_migracion(con, texto)
    con.execute("UPDATE proyectos SET carpeta = ? WHERE id = 1", (DRIVE,))
    primera = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    _aplicar_migracion(con, texto)
    assert [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")] == primera


def test_la_tabla_migrada_es_la_del_esquema():
    con = _tabla_vieja()
    _aplicar_migracion(con, _MIGRACION.read_text(encoding="utf-8"))
    tipos = lambda c: {f[1]: f[2].upper() for f in c.execute("PRAGMA table_info(proyectos)")}   # noqa: E731
    assert tipos(con) == tipos(b2._base())


def test_la_migracion_es_aditiva_y_dice_lo_que_hay_que_decir():
    texto = _MIGRACION.read_text(encoding="utf-8")
    sentencias = [" ".join(s.split()) for s in g._sentencias(texto)]
    assert sentencias[0].upper() == "BEGIN" and sentencias[-1].upper() == "COMMIT"
    assert [s for s in sentencias if s.upper() not in ("BEGIN", "COMMIT")][0] == "ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS carpeta TEXT"
    sin_comentarios = "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith("--"))
    for prohibido in ("DROP ", "RENAME", "DELETE", "TRUNCATE", "NOT NULL", "DEFAULT", "CHECK", "UPDATE ", "CREATE INDEX"):
        assert prohibido not in sin_comentarios.upper(), prohibido
    assert "NO SE APLICA ACÁ" in texto and "CÓMO SE DESHACE" in texto and "db/backup.py" in texto
    assert "DROP COLUMN IF EXISTS carpeta" in texto and "lock_timeout" in texto and "count(carpeta)" in texto
    assert "465f769" in texto                                # dice con qué código viejo se probó que convive


def test_el_esquema_declara_la_columna_y_nada_mas_de_la_tabla_cambio_de_forma():
    esquema = b2._SCHEMA.read_text(encoding="utf-8")
    ddl = " ".join(b2._ddl(esquema, "proyectos")[0].split())
    assert re.search(r"carpeta TEXT,", ddl) and ddl.count("carpeta") == 1
    assert "carpeta" in db.columnas_declaradas()["proyectos"]
