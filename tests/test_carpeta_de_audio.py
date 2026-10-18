"""La carpeta de audio del proyecto (`proyectos.carpeta_audio`): pieza 4 de «Carpetas del proyecto» (18-oct-2026;
diseño en `disenos/lucy-carpetas-proyecto/DISENO.md` §3.3 y garantías B1–B7).

Es la ruta, escrita a mano, de la carpeta del proyecto en el disco «Sesiones» del estudio. LUCY NO VE ESE DISCO: guarda y
enseña lo que se escribió; no la crea, no la abre, no comprueba que exista. Lo que se vigila (cada punto, con la ruta REAL
`POST /proyectos/{pid}/carpeta-audio`, la plantilla REAL, `crud.editar` / `crud.deshacer` REALES y SQL que se ejecuta de
verdad en SQLite con el `CREATE TABLE proyectos` de `db/schema.sql`):

  B1. UNA SOLA PUERTA de escribir, la MISMA función que la carpeta de Drive, y todos los sitios hermanos de `crud.py` que
      nombran `carpeta` nombran también `carpeta_audio` (la lista sale de recorrer el `ast` de `acciones/crud.py`);
  B2. la ruta NUNCA llega a un atributo que navega: siempre texto escapado en su `<code>`, aunque empiece por `https://`;
  B3. quién: la casa lee y escribe; solo ver lee el texto sin controles y la ruta le da 401; sin sesión nada;
  B4. una base sin migrar: la página carga, la línea de audio no sale y la carpeta de Drive sigue; el POST dice que falta
      actualizar la base; el humo tiene su línea sin tolerancia; la migración se ejecuta, es aditiva e idempotente;
  B5. la página y Lucy no prometen lo que no ven: la frase está para la casa y para solo ver, la nota de la columna dice que
      Lucy no ve el disco, y ninguna función de producción que nombra la columna toca el disco;
  B6. Telegram (`crud.editar`) escribe y deshace por la misma puerta;
  B7. «Copiar» copia SU ruta (el guion ejecutado con JavaScriptCore) y la estructura de la página lo permite.

FRONTERA, dicha una vez:
  · NO es un navegador. El portapapeles de verdad no se probó aquí (el doble del guion lo declara abajo); el navegador real
    se midió aparte y está en el reporte de la pieza, no en esta prueba.
  · `cerebro/consultar.py::_ejecutar` necesita Postgres (`SET TRANSACTION READ ONLY`): aquí se prueba lo que el modelo VE
    (el esquema y la nota) y que la columna está declarada, no la ejecución del SELECT del modelo.
  · La sonda de «ninguna función toca el disco» mira funciones que NOMBRAN la columna; no sigue el valor a otra función que
    lo reciba como argumento ni un nombre armado en ejecución.
  · Un `TEXT` de SQLite no impone el largo ni el carácter; la migración corre en SQLite con las mismas traducciones
    declaradas de `tests/test_fechas_de_proyecto.py::_aplicar_migracion`.

Correr:  python3 -m pytest tests/test_carpeta_de_audio.py -q
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import sqlite3
import subprocess
import textwrap
from collections import Counter
from html import unescape
from html.parser import HTMLParser

import pytest

import test_base_m2 as b2
from test_carpeta_de_proyecto import (  # noqa: F401  (los ayudantes de la carpeta de Drive y sus fixtures)
    DRIVE, LARGO, RUTA, _BUENOS, _HOSTILES, _QUE_NO, _QUE_VALEN, _atributos, _ddl_sin_la_carpeta, _esquema_de_etiquetas,
    _etiquetas, _id, _inventados, _seccion, hay_osascript, gente, mundo, uno, ver, ver_r, pagina_ver)
from test_fechas_de_proyecto import (  # noqa: F401
    _PoolPg, _aplicar_migracion, _correr, _fila, _huella_de_edicion, _huellas, _poner, _py_del_repo)
import test_grupo_ia as g
from test_grupo_ia import _ROOT
from _navegador import Navegador
import acciones.crud as crud
import config
import db.db as db
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO
AUDIO = "Sesiones/Cliente Uno"
PLANTILLA = _ROOT / "web" / "plantillas" / "proyectos.html"
MIGRACION = _ROOT / "db" / "migrations" / "2026-10-18_proyectos_carpeta_audio.sql"
FRASE = "Lucy no ve este disco: esta ruta es la que se escribió, y solo sirve en la computadora que tiene el disco Sesiones."


def mandar(pid, campos, *, chat="dueno", files=None):
    """Un POST al panel; `chat=None` sin sesión; `chat='ver'` con la sesión de solo ver."""
    c = Navegador(panel.app, base_url="https://testserver")
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c.post(f"/proyectos/{pid}/carpeta-audio", data=campos, files=files, follow_redirects=False)


def _pintar(m, valor, quien="casa", **consulta):
    """La página REAL de un proyecto cuya columna `carpeta_audio` ya vale `valor` (puesta directo en la base, sin pasar por
    la puerta de escribir, para mirar sola la forma de PINTAR)."""
    _poner(m, carpeta_audio=valor)
    return ver(m, p=1, **consulta) if quien == "casa" else pagina_ver(m, p=1, **consulta)


def _el_bloque_de_audio(html: str) -> str:
    """El `<div class="carpeta-audio">…</div>` de la página (cuenta los `<div>` para hallar su cierre), o ''."""
    inicio = html.find('<div class="carpeta-audio">')
    if inicio < 0:
        return ""
    nivel = 0
    for m in re.finditer(r"<div\b|</div>", html[inicio:]):
        nivel += 1 if m.group(0) != "</div>" else -1
        if nivel == 0:
            return html[inicio:inicio + m.end()]
    raise AssertionError("el bloque de audio no cierra")


# ═══════════════════════════════════════════════════════════════════════
# B1. UNA SOLA PUERTA, Y TODOS LOS SITIOS HERMANOS LA CUMPLEN
# ═══════════════════════════════════════════════════════════════════════

def _nombra_la_columna(n, padres) -> bool:
    """¿Esta cadena es el NOMBRE de una columna de `proyectos` y no otra cosa que se llame igual (la clave `"carpeta"` del
    resultado de Drive, p. ej.)? Lo es si: es el segundo elemento de una tupla `("proyectos", …)`; o la clave de un
    diccionario que es el valor de la clave `"proyectos"` (el de `PUERTAS`); o lo que se busca en un `in` (`"carpeta" in
    campos`); o el índice de un subíndice (`campos["carpeta"]`, `antes["carpeta"]`)."""
    p = padres.get(n)
    if isinstance(p, ast.Tuple) and len(p.elts) == 2 and p.elts[1] is n:
        return isinstance(p.elts[0], ast.Constant) and p.elts[0].value == "proyectos"
    if isinstance(p, ast.Dict) and n in p.keys:
        abuelo = padres.get(p)
        return isinstance(abuelo, ast.Dict) and any(
            v is p and isinstance(k, ast.Constant) and k.value == "proyectos" for k, v in zip(abuelo.keys, abuelo.values))
    if isinstance(p, ast.Compare) and p.left is n and any(isinstance(o, ast.In) for o in p.ops):
        return True
    return isinstance(p, ast.Subscript) and p.slice is n


def _sitios_de_crud(fuente: str) -> dict[str, tuple[int, int]]:
    """Por cada sentencia de primer nivel (o función) de un módulo que nombra la columna `carpeta` o `carpeta_audio` (según
    `_nombra_la_columna`): cuántas veces nombra cada una. Sale del `ast`, no de una lista tecleada."""
    salida = {}
    arbol = ast.parse(fuente)
    padres = {h: p for p in ast.walk(arbol) for h in ast.iter_child_nodes(p)}
    for nodo in arbol.body:
        cuenta = Counter(n.value for n in ast.walk(nodo)
                         if isinstance(n, ast.Constant) and n.value in ("carpeta", "carpeta_audio") and _nombra_la_columna(n, padres))
        if not cuenta:
            continue
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombre = nodo.name
        elif isinstance(nodo, ast.Assign) and isinstance(nodo.targets[0], ast.Name):
            nombre = nodo.targets[0].id
        else:
            nombre = f"sentencia@{nodo.lineno}"
        salida[nombre] = (cuenta["carpeta"], cuenta["carpeta_audio"])
    return salida


def _sitios_sin_hermano(fuente: str) -> list[str]:
    """Los sitios donde se nombra `carpeta` y no `carpeta_audio` el mismo número de veces."""
    return sorted(n for n, (c, a) in _sitios_de_crud(fuente).items() if c != a)


def test_los_sitios_hermanos_de_crud_salen_de_recorrer_el_codigo_y_los_cumplen_todos():
    sitios = _sitios_de_crud((_ROOT / "acciones" / "crud.py").read_text(encoding="utf-8"))
    # La sonda ve los cuatro sitios que el diseño nombra (si no los viera, un «todos cumplen» no diría nada).
    assert {"PUERTAS", "_VUELVE_A", "_COLUMNAS_DE_TEXTO", "editar"} <= set(sitios), sorted(sitios)
    assert _sitios_sin_hermano((_ROOT / "acciones" / "crud.py").read_text(encoding="utf-8")) == []
    assert all(c >= 1 and a >= 1 for c, a in sitios.values()), sitios


def test_la_sonda_de_hermanos_ve_un_sitio_inventado_sin_su_hermano():
    nuevo = textwrap.dedent("""
        OTRA_LISTA = {("proyectos", "carpeta")}
        TRES = {("proyectos", "carpeta"), ("proyectos", "carpeta_audio")}
        PUERTAS = {"proyectos": {"nombre": f, "carpeta": g}}
        BIEN = {"proyectos": {"nombre": f, "carpeta": g, "carpeta_audio": g}}
        async def editar_otra(campos):
            if "carpeta" in campos:
                pass
        async def indice(campos, antes):
            return campos["carpeta"] == antes["carpeta"]
        async def con_los_dos(campos):
            if "carpeta" in campos or "carpeta_audio" in campos:
                pass
        async def otra_cosa_que_se_llama_igual():
            return {"resultado": "fallo", "carpeta": None}
        """)
    # (`TRES`, `BIEN` y `con_los_dos` cuentan uno y uno; `otra_cosa_que_se_llama_igual` no nombra la columna)
    assert _sitios_sin_hermano(nuevo) == ["OTRA_LISTA", "PUERTAS", "editar_otra", "indice"]


def test_la_columna_de_audio_tiene_la_misma_puerta_que_la_carpeta_y_frase_de_deshacer():
    assert crud.PUERTAS["proyectos"]["carpeta_audio"] is crud.PUERTAS["proyectos"]["carpeta"] is db.carpeta_de_proyecto_que_vale
    assert ("proyectos", "carpeta_audio") in crud._VUELVE_A and ("proyectos", "carpeta_audio") in crud._COLUMNAS_DE_TEXTO
    assert all((t, c) in crud._VUELVE_A for t, cols in crud.PUERTAS.items() for c in cols)


def _resultado_de_escribir(m, columna: str, valor):
    """Qué hace `crud.editar` real con ese valor en esa columna: ('guardado', lo que quedó) o ('rechazado', la clave)."""
    try:
        _correr(crud.editar("proyectos", 1, {columna: valor}, motivo="x"))
    except ValueError as e:
        causa = e.__cause__
        return ("rechazado", getattr(causa, "clave", str(e)))
    return ("guardado", _fila(m)[columna])


@pytest.mark.parametrize("valor", [v for v, _ in _QUE_VALEN] + [v for v, _ in _QUE_NO] + _HOSTILES[:30]
                         + ["2026-10-08", "2026-10 sesiones", "a" * (LARGO + 1)], ids=_id)
def test_editar_trata_la_ruta_de_audio_como_trata_la_carpeta_con_el_mismo_criterio(uno, valor):
    con_carpeta = _resultado_de_escribir(uno, "carpeta", valor)
    _poner(uno, carpeta=None)
    con_audio = _resultado_de_escribir(uno, "carpeta_audio", valor)
    assert con_audio == con_carpeta, (valor, con_carpeta, con_audio)


@pytest.mark.parametrize("valor,clave", _QUE_NO, ids=_id)
def test_editar_rechaza_una_ruta_de_audio_mala_sin_escribir_nada_ni_dejar_huella(uno, valor, clave):
    antes = _fila(uno)
    with pytest.raises(ValueError) as e:
        _correr(crud.editar("proyectos", 1, {"carpeta_audio": valor}, motivo="x"))
    assert isinstance(e.value.__cause__, db.CarpetaDeProyectoNoVale) and e.value.__cause__.clave == clave
    assert _fila(uno) == antes and _huellas(uno) == []


def test_la_puerta_es_una_sola_con_entradas_inventadas():
    """Con 3.000 textos inventados la puerta de la columna de audio y la de la carpeta dicen lo mismo."""
    for texto in _inventados(3000, semilla=4):
        def _lo_que_dice(columna):
            try:
                return ("ok", crud.PUERTAS["proyectos"][columna](texto))
            except db.CarpetaDeProyectoNoVale as e:
                return ("no", e.clave)
        assert _lo_que_dice("carpeta_audio") == _lo_que_dice("carpeta")


def test_editar_guarda_la_ruta_limpia_deja_la_huella_y_no_toca_la_carpeta_de_drive(uno):
    _poner(uno, carpeta=DRIVE)
    _correr(crud.editar("proyectos", 1, {"carpeta_audio": "  " + AUDIO + "\n"}, motivo="x", actor="panel"))
    f = _fila(uno)
    assert f["carpeta_audio"] == AUDIO and f["carpeta"] == DRIVE
    h, = _huellas(uno)
    assert h["accion"] == "editar" and h["actor"] == "panel"
    assert json.loads(h["antes"])["carpeta_audio"] is None and json.loads(h["despues"])["carpeta_audio"] == AUDIO


def test_lo_que_ya_estaba_igual_no_escribe_ni_deja_huella(uno):
    _poner(uno, carpeta_audio=AUDIO)
    for pedido in (AUDIO, "  " + AUDIO + " ", AUDIO + "\n"):
        despues, log_id = _correr(crud.editar("proyectos", 1, {"carpeta_audio": pedido}, motivo="x"))
        assert log_id is None and despues["carpeta_audio"] == AUDIO
    assert _huellas(uno) == []


def test_pedir_la_ruta_y_otra_columna_escribe_las_dos_y_una_mala_no_escribe_ninguna(uno):
    _correr(crud.editar("proyectos", 1, {"carpeta_audio": AUDIO, "nombre": "Otro nombre"}, motivo="x"))
    f = _fila(uno)
    assert (f["carpeta_audio"], f["nombre"]) == (AUDIO, "Otro nombre")
    antes = _fila(uno)
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"carpeta_audio": "a" * (LARGO + 1), "nombre": "Tercero"}, motivo="x"))
    assert _fila(uno) == antes


def test_se_puede_quitar_la_ruta_y_pedir_la_misma_otra_vez_no_deja_huella(uno):
    _poner(uno, carpeta_audio=AUDIO)
    despues, log_id = _correr(crud.editar("proyectos", 1, {"carpeta_audio": ""}, motivo="x"))
    assert log_id is not None and _fila(uno)["carpeta_audio"] is None and despues["carpeta_audio"] is None
    despues, log_id = _correr(crud.editar("proyectos", 1, {"carpeta_audio": "   "}, motivo="x"))
    assert log_id is None and len(_huellas(uno)) == 1


def test_deshacer_rechaza_una_ruta_que_la_puerta_no_deja_y_dice_de_cual_se_trata(uno):
    """Volver atrás una edición cuyo `antes` ya no vale NO escribe (el éxito de `deshacer` usa `jsonb_populate_record`,
    que SQLite no tiene: aquí solo se ejercen sus rechazos, igual que con la carpeta)."""
    antes = _fila(uno)
    for malo in ("a" * (LARGO + 1), "a\nb", 12345):
        log_id = _huella_de_edicion(uno, {"carpeta_audio": malo}, {"carpeta_audio": AUDIO})
        with pytest.raises(ValueError, match="la carpeta de audio que tenía"):
            _correr(crud.deshacer(log_id))
    assert _fila(uno) == antes


def test_deshacer_solo_devuelve_la_ruta_si_esa_edicion_la_cambio(uno):
    log_id = _huella_de_edicion(uno, {"nombre": "Viejo", "carpeta_audio": "a\nb"},
                                {"nombre": "Disco de prueba", "carpeta_audio": "a\nb"})
    try:
        _correr(crud.deshacer(log_id))
    except Exception as e:                                               # noqa: BLE001
        assert "la carpeta de audio que tenía" not in str(e)


def test_telegram_editar_escribe_la_ruta_por_la_misma_puerta(uno):
    """`cerebro/agente.py` llama `crud.editar` con lo que el modelo mandó (texto): la misma puerta, con su huella."""
    _correr(crud.editar("proyectos", 1, {"carpeta_audio": "Sesiones/Cliente Dos"}, motivo="Tiziano lo pidió por Telegram"))
    assert _fila(uno)["carpeta_audio"] == "Sesiones/Cliente Dos"
    assert _huellas(uno)[0]["actor"] == "lucy"
    with pytest.raises(ValueError):
        _correr(crud.editar("proyectos", 1, {"carpeta_audio": "x\ny"}, motivo="x"))
    assert _fila(uno)["carpeta_audio"] == "Sesiones/Cliente Dos"


def test_un_proyecto_nuevo_nace_sin_ruta_de_audio(uno):
    assert _fila(uno)["carpeta_audio"] is None


# ═══════════════════════════════════════════════════════════════════════
# B2. LA RUTA DE AUDIO NUNCA LLEGA A UN ATRIBUTO QUE NAVEGA
# ═══════════════════════════════════════════════════════════════════════

_ATRIBUTOS_QUE_NAVEGAN = {"href", "src", "action", "formaction", "data", "poster", "cite", "ping", "srcset", "background",
                          "xlink:href", "manifest", "longdesc", "profile", "usemap", "codebase", "archive"}


def _sin_comentarios(texto: str) -> str:
    return re.sub(r"\{#.*?#\}", "", texto, flags=re.S)


def _usos_malos_del_audio_en_jinja(texto: str) -> list[str]:
    """Todo lo que la plantilla hace con `carpeta_audio` que no sea una de las dos cosas permitidas:
       · `{{ … }}` solo como el texto de `<code class="carpeta-texto">` o como el `value="…"` del campo, sin filtros;
       · `{% … %}` solo condiciones (`if`/`elif`) y el `set` de `ver_audio` (un booleano), sin filtros;
    y `ver_audio` mismo solo en condiciones. Un alias (`{% set c = m.carpeta_audio %}`), un filtro (`|enlace_de_carpeta`,
    `|attr`), un `href="{{ … }}"` o un volcado fuera de esos dos sitios: malo. LO QUE NO VE: un nombre armado en ejecución
    (`m['carpeta' ~ '_audio']`) ni recorrer `m` sin nombrar el dato."""
    texto = _sin_comentarios(texto)
    malos = []
    for m in re.finditer(r"\{\{.*?\}\}|\{%.*?%\}", texto, flags=re.S):
        etiqueta = m.group(0)
        if "carpeta_audio" not in etiqueta and "ver_audio" not in etiqueta:
            continue
        antes = texto[:m.start()]
        if etiqueta.startswith("{{"):
            ok = ("carpeta_audio" in etiqueta and "ver_audio" not in etiqueta and "|" not in etiqueta
                  and (antes.endswith('<code class="carpeta-texto">') or antes.endswith('value="')))
        else:
            cuerpo = etiqueta[2:-2].strip("- \n")
            if re.fullmatch(r"set\s+ver_audio\s*=.*", cuerpo, flags=re.S):
                ok = "|" not in cuerpo
            else:
                ok = bool(re.match(r"(if|elif)\b", cuerpo)) and "|" not in cuerpo and "enlace" not in cuerpo
        if not ok:
            malos.append(" ".join(etiqueta.split()))
    return malos


def test_la_plantilla_solo_pone_la_ruta_de_audio_en_su_code_y_en_el_value_de_su_campo():
    assert _usos_malos_del_audio_en_jinja(PLANTILLA.read_text(encoding="utf-8")) == []
    # y se ve a sí misma: hay de verdad un uso de cada clase permitida en la plantilla real
    sin = _sin_comentarios(PLANTILLA.read_text(encoding="utf-8"))
    assert sin.count('<code class="carpeta-texto">{{ m.carpeta_audio }}</code>') == 1
    assert sin.count('value="{{ m.carpeta_audio or \'\' }}"') == 1 and sin.count("{{ m.carpeta_audio") == 2


def test_la_sonda_del_audio_en_jinja_ve_cada_forma_de_llevarla_a_un_enlace():
    malas = [
        '<a href="{{ m.carpeta_audio }}">x</a>',
        '<a href="{{ m.carpeta_audio|enlace_de_carpeta }}">x</a>',
        '{% set enlace_audio = m.carpeta_audio|enlace_de_carpeta %}',
        '{% set c = m.carpeta_audio %}<a href="{{ c }}">x</a>',
        '{% set ver_audio = m.carpeta_audio|enlace_de_carpeta %}',
        '<code class="carpeta-texto">{{ m.carpeta_audio|enlace_de_carpeta }}</code>',
        '<a title="{{ m.carpeta_audio }}">x</a>',
        '<code class="otra">{{ m.carpeta_audio }}</code>',
        '{{ ver_audio }}', '{% set otra = ver_audio %}', '{{ m|attr("carpeta_audio") }}',
        '<img src="{{ m.carpeta_audio }}">', '<form action="{{ m.carpeta_audio }}">',
    ]
    for mala in malas:
        assert _usos_malos_del_audio_en_jinja(mala), mala
    buenas = ['<code class="carpeta-texto">{{ m.carpeta_audio }}</code>', '<input value="{{ m.carpeta_audio or \'\' }}">',
              '{% set ver_audio = m.carpeta_audio_disponible and (m.carpeta_audio or not solo_ver) %}',
              '{% if ver_audio %}', '{% elif m.carpeta_audio %}', '{# m.carpeta_audio|enlace_de_carpeta #}',
              '{% if ver_carpeta or ver_audio or ver_cotizaciones %}']
    for buena in buenas:
        assert _usos_malos_del_audio_en_jinja(buena) == [], buena


@pytest.mark.parametrize("quien", ["casa", "ver"])
@pytest.mark.parametrize("valor", _HOSTILES + _BUENOS, ids=_id)
def test_en_la_pagina_real_la_ruta_de_audio_sale_solo_como_texto_escapado_en_su_code(uno, valor, quien):
    inocente = _pintar(uno, "Sesiones/Simple", quien)
    html = _pintar(uno, valor, quien)
    bloque = _el_bloque_de_audio(html)
    assert bloque, "la línea de audio no salió"
    # el valor aparece, entero y sin escapes, como el texto de UN `<code class="carpeta-texto">` dentro del bloque
    codigos = re.findall(r'<code class="carpeta-texto">(.*?)</code>', bloque, re.S)
    assert [unescape(c) for c in codigos] == [valor]
    # y ningún atributo de la página lleva el valor, ni cambia una sola etiqueta respecto a una ruta inocente
    assert _atributos(html) - _atributos(inocente) == Counter() or valor == "Sesiones/Simple"
    assert _esquema_de_etiquetas(html) == _esquema_de_etiquetas(inocente)
    assert not [1 for t, a in _etiquetas(bloque) if t == "a" and "carpeta-enlace" in a.get("class", "")]
    assert not [1 for t, a in _etiquetas(bloque) if any(k in _ATRIBUTOS_QUE_NAVEGAN and valor.strip()[:8] in v and valor.strip()
                                                        for k, v in a.items())]


def test_aunque_empiece_por_https_la_ruta_de_audio_no_es_un_enlace(uno):
    for quien in ("casa", "ver"):
        html = _pintar(uno, DRIVE, quien)
        bloque = _el_bloque_de_audio(html)
        assert f'<code class="carpeta-texto">{DRIVE.replace("&", "&amp;")}</code>' in bloque or unescape(
            re.search(r'<code class="carpeta-texto">(.*?)</code>', bloque, re.S).group(1)) == DRIVE
        assert "<a " not in bloque.replace('<a class="btn-linea"', "") and "Abrir" not in bloque
        assert 'class="carpeta-enlace"' not in html and "target=\"_blank\"" not in _seccion(html)


def test_el_filtro_de_enlaces_no_ve_nunca_la_ruta_de_audio(uno, monkeypatch):
    """Se cambia el filtro real por un espía: con la carpeta de Drive vacía y una ruta de audio que parece enlace, lo que
    recibe el filtro (todas las veces que la plantilla lo llama) nunca es la ruta de audio."""
    vistos = []
    original = panel.plantillas.env.filters["enlace_de_carpeta"]

    def espia(valor):
        vistos.append(valor)
        return original(valor)
    monkeypatch.setitem(panel.plantillas.env.filters, "enlace_de_carpeta", espia)
    for quien in ("casa", "ver"):
        _pintar(uno, DRIVE, quien)
    assert vistos, "el espía no se llamó: la prueba no mide nada"
    assert DRIVE not in vistos


def test_el_formulario_de_audio_lleva_el_valor_solo_en_su_value(uno):
    for valor in _HOSTILES[:25]:
        html = _pintar(uno, valor, "casa", editar="carpeta_audio")
        inocente = _pintar(uno, "Sesiones/Simple", "casa", editar="carpeta_audio")
        campos = [a for t, a in _etiquetas(html) if t == "input" and a.get("name") == "carpeta_audio"]
        assert len(campos) == 1 and campos[0]["value"] == valor and campos[0]["maxlength"] == str(LARGO)
        assert _atributos(html) - _atributos(inocente) == Counter({("input", "value", valor): 1}) or valor == "Sesiones/Simple"
        assert _esquema_de_etiquetas(html) == _esquema_de_etiquetas(inocente)
        assert 'action="/proyectos/1/carpeta-audio"' in html


# ═══════════════════════════════════════════════════════════════════════
# B3. QUIÉN
# ═══════════════════════════════════════════════════════════════════════

def test_la_casa_ve_la_linea_aunque_no_haya_ruta_y_puede_ponerla(uno):
    html = ver(uno, p=1)
    bloque = _el_bloque_de_audio(html)
    assert "<h3 class=\"carpeta-linea\">Audio (disco Sesiones)</h3>" in bloque and "Todavía no hay carpeta de audio." in bloque
    assert 'href="/proyectos?p=1&amp;editar=carpeta_audio#carpeta-del-proyecto"' in bloque and ">Poner</a>" in bloque
    assert "Copiar" not in bloque and FRASE not in bloque
    form = ver(uno, p=1, editar="carpeta_audio")
    assert 'action="/proyectos/1/carpeta-audio"' in form and 'name="carpeta_audio"' in form and f'maxlength="{LARGO}"' in form
    assert "Lucy no ve ese disco" in form and "Cancelar" in _el_bloque_de_audio(form)


def test_la_casa_ve_la_ruta_como_texto_con_copiar_cambiar_y_la_frase(uno):
    bloque = _el_bloque_de_audio(_pintar(uno, AUDIO))
    assert f'<code class="carpeta-texto">{AUDIO}</code>' in bloque and FRASE in bloque
    assert '<button type="button" class="btn-linea copiar" data-copiar-carpeta>Copiar</button>' in bloque
    assert ">Cambiar</a>" in bloque and "editar=carpeta_audio" in bloque


def test_solo_ver_ve_la_ruta_y_la_frase_sin_ningun_control_ni_guion(uno):
    html = pagina_ver(uno, p=1)
    assert "Audio (disco Sesiones)" not in html and _seccion(html) == ""          # sin ruta no hay nada que leer
    html = _pintar(uno, AUDIO, "ver")
    bloque = _el_bloque_de_audio(html)
    assert f'<code class="carpeta-texto">{AUDIO}</code>' in bloque and FRASE in bloque
    for sobra in ("<form", "<button", "Copiar", "Cambiar", "Poner", "<a "):
        assert sobra not in bloque, sobra
    assert html.count("<script") == 1 and "data-copiar-carpeta" not in html and 'id="guion-copiar-carpeta"' not in html
    # y la dirección escrita a mano no abre el formulario
    html = pagina_ver(uno, p=1, editar="carpeta_audio")
    assert 'name="carpeta_audio"' not in html and "/proyectos/1/carpeta-audio" not in html


def test_la_plantilla_sola_tampoco_dibuja_controles_de_audio_en_solo_ver(uno, monkeypatch):
    """La ruta ya apaga `editar` en solo ver; aquí se le quita ese apoyo: la plantilla recibe `editar='carpeta_audio'` con
    `solo_ver=True` y no puede dibujar formulario, campo, «Poner/Cambiar» ni «Copiar»."""
    original = panel.plantillas.TemplateResponse

    def con_editar(request, nombre, contexto=None, *a, **k):
        if nombre == "proyectos.html":
            assert contexto["solo_ver"] is True
            contexto = {**contexto, "editar": "carpeta_audio"}
        return original(request, nombre, contexto, *a, **k)
    monkeypatch.setattr(panel.plantillas, "TemplateResponse", con_editar)
    html = _pintar(uno, AUDIO, "ver")
    assert 'name="carpeta_audio"' not in html and "/proyectos/1/carpeta-audio" not in html
    assert "editar=carpeta_audio" not in html and "Copiar" not in html and "data-copiar-carpeta" not in html


def test_solo_ver_y_sin_sesion_no_escriben_la_ruta(uno):
    antes = _fila(uno)
    for chat in ("ver", None):
        r = mandar(1, {"carpeta_audio": "hackeado"}, chat=chat)
        assert r.status_code in (401, 403), (chat, r.status_code)
        assert _fila(uno) == antes and _huellas(uno) == []


def test_sin_sesion_la_pagina_no_deja_ver_la_ruta(uno):
    _poner(uno, carpeta_audio=AUDIO)
    r = Navegador(panel.app, base_url="https://testserver").get("/proyectos", params={"p": 1}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403) and AUDIO not in r.text


# ═══════════════════════════════════════════════════════════════════════
# LA RUTA
# ═══════════════════════════════════════════════════════════════════════

def test_la_ruta_guarda_y_lo_dice_con_su_huella(uno):
    r = mandar(1, {"carpeta_audio": "  " + AUDIO + " "})
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?hecho=carpeta_audio&p=1#carpeta-del-proyecto"
    assert _fila(uno)["carpeta_audio"] == AUDIO and _fila(uno)["carpeta"] is None
    h, = _huellas(uno)
    assert h["actor"] == "panel" and h["accion"] == "editar"
    assert "Carpeta de audio guardada." in ver_r(uno, p=1, hecho="carpeta_audio")


def test_la_ruta_guarda_un_texto_que_parece_fecha_como_texto(uno):
    r = mandar(1, {"carpeta_audio": "2026-10-08"})
    assert "hecho=carpeta_audio&" in r.headers["location"] and _fila(uno)["carpeta_audio"] == "2026-10-08"


def test_la_ruta_quita_con_el_campo_vacio_y_lo_dice(uno):
    _poner(uno, carpeta_audio=AUDIO)
    r = mandar(1, {"carpeta_audio": ""})
    assert r.headers["location"] == "/proyectos?hecho=carpeta_audio_quitada&p=1#carpeta-del-proyecto"
    assert _fila(uno)["carpeta_audio"] is None and len(_huellas(uno)) == 1
    assert "Carpeta de audio quitada" in ver_r(uno, p=1, hecho="carpeta_audio_quitada")
    assert "error=carpeta_audio_igual" in mandar(1, {"carpeta_audio": "   "}).headers["location"]      # ya no había


def test_la_ruta_no_dice_guardada_si_nada_cambio(uno):
    _poner(uno, carpeta_audio=AUDIO)
    r = mandar(1, {"carpeta_audio": AUDIO})
    assert "error=carpeta_audio_igual" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _huellas(uno) == []
    assert "La carpeta de audio ya estaba así" in ver_r(uno, p=1, error="carpeta_audio_igual")


_RECHAZOS = [("a" * (LARGO + 1), "carpeta_audio_largo"), ("a\nb", "carpeta_audio_caracteres"),
             ("a\x00b", "carpeta_audio_caracteres"), ("a\tb", "carpeta_audio_caracteres"), ("a\rb", "carpeta_audio_caracteres")]


@pytest.mark.parametrize("valor,clave", _RECHAZOS, ids=_id)
def test_la_ruta_rechaza_con_su_clave_y_no_escribe_nada(uno, valor, clave):
    _poner(uno, carpeta_audio=AUDIO)
    antes = _fila(uno)
    r = mandar(1, {"carpeta_audio": valor})
    destino = r.headers["location"]
    assert r.status_code == 303 and f"error={clave}&" in destino and "editar=carpeta_audio" in destino and "hecho=" not in destino
    assert _fila(uno) == antes and _huellas(uno) == []
    assert valor.strip() not in destino                                               # lo escrito nunca viaja
    assert "NO se guardó" in ver_r(uno, p=1, error=clave, editar="carpeta_audio")


def test_la_ruta_rechaza_un_archivo_subido_en_vez_de_un_texto(uno):
    antes = _fila(uno)
    r = mandar(1, {}, files={"carpeta_audio": ("c.txt", b"/Volumes/x", "text/plain")})
    assert "error=carpeta_audio_" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _fila(uno) == antes and _huellas(uno) == []


def test_un_post_sin_el_campo_no_quita_la_ruta(uno):
    _poner(uno, carpeta_audio=AUDIO)
    antes = _fila(uno)
    r = mandar(1, {"otro": "x"})
    assert "error=carpeta_audio_invalida&" in r.headers["location"] and "hecho=" not in r.headers["location"]
    assert _fila(uno) == antes and _huellas(uno) == []
    assert "Esa carpeta de audio no se puede guardar: NO se guardó nada." in ver_r(uno, p=1, error="carpeta_audio_invalida")


def test_el_campo_de_la_carpeta_de_drive_no_escribe_la_de_audio_ni_al_reves(uno):
    mandar(1, {"carpeta_audio": AUDIO})
    # el POST de la carpeta de Drive con SU campo no toca la de audio
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    c.post("/proyectos/1/carpeta", data={"carpeta": DRIVE}, follow_redirects=False)
    f = _fila(uno)
    assert (f["carpeta"], f["carpeta_audio"]) == (DRIVE, AUDIO)
    # y el de audio con el campo de la de Drive no escribe nada
    r = mandar(1, {"carpeta": "otra"})
    assert "error=carpeta_audio_invalida" in r.headers["location"] and _fila(uno) == f


def test_la_ruta_de_un_proyecto_que_no_esta_no_dice_guardada(uno):
    assert mandar(99, {"carpeta_audio": AUDIO}).headers["location"] == "/proyectos?error=proyecto"
    uno.con.execute("UPDATE proyectos SET borrado_en = '2026-09-01T00:00:00+00:00' WHERE id = 1")
    assert mandar(1, {"carpeta_audio": AUDIO}).headers["location"] == "/proyectos?error=proyecto"
    assert _fila(uno)["carpeta_audio"] is None


def test_si_la_base_falla_al_guardar_no_dice_ni_guardada_ni_no_se_guardo(uno, monkeypatch):
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
    r = mandar(1, {"carpeta_audio": AUDIO})
    assert "error=carpeta_audio_base&" in r.headers["location"] and "hecho=" not in r.headers["location"]
    texto = ver_r(uno, p=1, error="carpeta_audio_base", editar="carpeta_audio")
    assert "no se pudo confirmar" in texto and "guardada" not in texto and "NO se guardó" not in texto


def test_los_avisos_de_audio_no_salen_con_una_direccion_escrita_a_mano(uno):
    """Sin el recibo de un POST de verdad (la puerta de `web/avisos.py`) ni «guardada» ni un error salen."""
    from test_pagina_proyectos import ver as ver_sin_recibo
    for hecho in ("carpeta_audio", "carpeta_audio_quitada"):
        assert "Carpeta de audio" not in ver_sin_recibo(uno, p=1, hecho=hecho)
    assert "ya estaba así" not in ver_sin_recibo(uno, p=1, error="carpeta_audio_igual")


# ═══════════════════════════════════════════════════════════════════════
# B4. LA BASE SIN MIGRAR
# ═══════════════════════════════════════════════════════════════════════

def _ddl_sin_el_audio() -> str:
    """El `CREATE TABLE proyectos` de `db/schema.sql` como era con la columna `carpeta` y SIN `carpeta_audio`."""
    ddl = b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "proyectos")[0]
    ddl = re.sub(r"\n\s*carpeta_audio\s+TEXT,", "", ddl)
    assert "carpeta_audio" not in ddl and re.search(r"\bcarpeta\s+TEXT,", ddl), ddl
    return ddl


def _sin_el_audio(m, monkeypatch):
    m.con.execute("DROP TABLE proyectos")
    m.con.execute(_ddl_sin_el_audio())
    monkeypatch.setattr(db, "pool", _PoolPg(m.con))
    m.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)


def test_la_pagina_carga_sin_la_columna_de_audio_no_dibuja_la_linea_y_la_carpeta_de_drive_sigue(uno, monkeypatch):
    _sin_el_audio(uno, monkeypatch)
    _poner(uno, carpeta=DRIVE)
    for consulta in ({"p": 1}, {"p": 1, "editar": "carpeta_audio"}, {}, {"g": "CDS"}):
        html = ver(uno, **consulta)
        for sobra in ("Audio (disco Sesiones)", "/carpeta-audio", "editar=carpeta_audio", "Todavía no hay carpeta de audio",
                      "carpeta-audio\">"):
            assert sobra not in html, (consulta, sobra)
    html = ver(uno, p=1)
    assert "Documentos (Drive)" in html and f'href="{DRIVE.replace("&", "&amp;")}"' in html and "<h2>Carpetas del proyecto</h2>" in html
    assert 'id="de-que-se-trata"' in html                                                # el resto de la página sigue
    assert "Audio (disco Sesiones)" not in pagina_ver(uno, p=1)


def test_sin_la_columna_la_lectura_dice_no_disponible_la_de_la_carpeta_sigue_y_otro_error_no_se_traga(uno, monkeypatch):
    assert _correr(db.carpetas_de_audio()) == {1: None}
    _sin_el_audio(uno, monkeypatch)
    assert _correr(db.carpetas_de_audio()) is None
    assert _correr(db.carpetas_de_proyectos()) == {1: None}      # NO está mezclada: la de la carpeta no cae con la de audio
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
            _correr(db.carpetas_de_audio())

    class _SinSqlstate:
        def cursor(self, row_factory=None):
            class C:
                async def execute(self, *a):
                    raise RuntimeError("otra cosa")
            return C()

    class _P2:
        def connection(self):
            class CM:
                async def __aenter__(s):
                    return _SinSqlstate()

                async def __aexit__(s, *e):
                    return False
            return CM()
    monkeypatch.setattr(db, "pool", _P2())
    with pytest.raises(RuntimeError):
        _correr(db.carpetas_de_audio())


def test_sin_la_columna_una_escritura_a_mano_se_rechaza_diciendo_que_falta_actualizar(uno, monkeypatch):
    _sin_el_audio(uno, monkeypatch)
    r = mandar(1, {"carpeta_audio": AUDIO})
    assert r.status_code == 303 and "error=carpeta_audio_sin_columna" in r.headers["location"]
    assert "hecho=" not in r.headers["location"] and _huellas(uno) == []
    assert "falta actualizar la base de datos" in ver_r(uno, p=1, error="carpeta_audio_sin_columna")


def test_la_lectura_de_la_carpeta_no_nombra_la_columna_de_audio():
    """Si `carpetas_de_proyectos` leyera también `carpeta_audio`, una base sin migrar tumbaría el bloque de Drive. Se mira el
    SQL real de cada lectura."""
    fuente = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    sql = {}
    for fn in ast.walk(fuente):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in ("carpetas_de_proyectos", "carpetas_de_audio"):
            sql[fn.name] = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and n.value.startswith("SELECT")]
    assert sql["carpetas_de_proyectos"] == ["SELECT id, carpeta FROM proyectos WHERE borrado_en IS NULL"]
    assert sql["carpetas_de_audio"] == ["SELECT id, carpeta_audio FROM proyectos WHERE borrado_en IS NULL"]


def _humo_sin_tolerancia():
    """La función `_columna_de_audio_sin_tolerancia` de `tools/humo.py`, sacada del archivo (al importarlo el humo arranca y
    se conecta) y ejecutada con el `db` real."""
    arbol = ast.parse((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"))
    fn = next(n for n in arbol.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_columna_de_audio_sin_tolerancia")
    espacio = {"db": db}
    exec(compile(ast.Module([fn], []), "humo.py", "exec"), espacio)
    return arbol, espacio["_columna_de_audio_sin_tolerancia"]


def test_el_humo_lee_la_columna_sin_tolerancia_y_la_tiene_en_su_lista(uno, monkeypatch):
    arbol, funcion = _humo_sin_tolerancia()
    en_la_lista = [e for n in ast.walk(arbol) if isinstance(n, ast.Assign)
                   and any(isinstance(x, ast.Name) and x.id == "pruebas" for x in n.targets)
                   for e in ast.walk(n.value) if isinstance(e, ast.Name) and e.id == "_columna_de_audio_sin_tolerancia"]
    assert en_la_lista, "la función del humo existe pero no está en la lista `pruebas`"
    monkeypatch.setattr(db, "pool", _PoolPg(uno.con))
    _poner(uno, carpeta_audio=AUDIO)
    assert _correr(funcion()) == {"proyectos con carpeta_audio": 1}                  # con la columna: corre y cuenta
    _sin_el_audio(uno, monkeypatch)
    with pytest.raises(Exception) as e:                                                # sin ella: REVIENTA (no se traga)
        _correr(funcion())
    assert getattr(e.value, "sqlstate", None) == "42703"


# ── La migración, ejecutada ─────────────────────────────────────────────────────────────────────

def _tabla_con_la_carpeta_y_sin_el_audio() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE areas (clave TEXT PRIMARY KEY, color TEXT, orden INTEGER)")
    con.execute(_ddl_sin_el_audio())
    for pid, borrado in ((1, None), (2, None), (3, "2026-04-01T00:00:00+00:00")):
        con.execute("INSERT INTO proyectos (id, nombre, creado_en, borrado_en, carpeta) VALUES (?,?,?,?,?)",
                    (pid, f"p{pid}", "2026-03-01T12:00:00+00:00", borrado, DRIVE if pid == 1 else None))
    return con


def test_la_migracion_se_ejecuta_y_no_toca_ninguna_fila():
    con = _tabla_con_la_carpeta_y_sin_el_audio()
    antes = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    saltadas = _aplicar_migracion(con, MIGRACION.read_text(encoding="utf-8"))
    columnas = [c[1] for c in con.execute("PRAGMA table_info(proyectos)")]
    assert columnas[-1] == "carpeta_audio" and columnas.count("carpeta_audio") == 1
    despues = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    assert [f[:-1] for f in despues] == antes and all(f[-1] is None for f in despues)
    assert con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 3            # el conteo del ensayo
    assert con.execute("SELECT count(carpeta_audio) FROM proyectos").fetchone()[0] == 0
    assert [s.split()[0].upper() for s in saltadas] == ["BEGIN", "COMMENT", "COMMIT"]


def test_la_migracion_corrida_dos_veces_deja_lo_mismo():
    con = _tabla_con_la_carpeta_y_sin_el_audio()
    texto = MIGRACION.read_text(encoding="utf-8")
    _aplicar_migracion(con, texto)
    con.execute("UPDATE proyectos SET carpeta_audio = ? WHERE id = 1", (AUDIO,))
    primera = [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")]
    _aplicar_migracion(con, texto)
    assert [tuple(f) for f in con.execute("SELECT * FROM proyectos ORDER BY id")] == primera


def test_la_tabla_migrada_es_la_del_esquema_y_la_columna_esta_declarada():
    con = _tabla_con_la_carpeta_y_sin_el_audio()
    _aplicar_migracion(con, MIGRACION.read_text(encoding="utf-8"))
    tipos = lambda c: {f[1]: f[2].upper() for f in c.execute("PRAGMA table_info(proyectos)")}   # noqa: E731
    assert tipos(con) == tipos(b2._base())
    assert "carpeta_audio" in db.columnas_declaradas()["proyectos"]
    ddl = " ".join(b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "proyectos")[0].split())
    assert len(re.findall(r"\bcarpeta_audio TEXT,", ddl)) == 1


def test_la_migracion_es_aditiva_y_dice_lo_que_hay_que_decir():
    texto = MIGRACION.read_text(encoding="utf-8")
    sentencias = [" ".join(s.split()) for s in g._sentencias(texto)]
    assert sentencias[0].upper() == "BEGIN" and sentencias[-1].upper() == "COMMIT"
    assert [s for s in sentencias if s.upper() not in ("BEGIN", "COMMIT")][0] == \
        "ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS carpeta_audio TEXT"
    assert len([s for s in sentencias if s.upper().startswith("ALTER")]) == 1
    sin_comentarios = "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith("--"))
    for prohibido in ("DROP ", "RENAME", "DELETE", "TRUNCATE", "NOT NULL", "DEFAULT", "CHECK", "UPDATE ", "CREATE INDEX"):
        assert prohibido not in sin_comentarios.upper(), prohibido
    assert "NO SE APLICA ACÁ" in texto and "CÓMO SE DESHACE" in texto and "db/backup.py" in texto
    assert "DROP COLUMN IF EXISTS carpeta_audio" in texto and "lock_timeout" in texto and "count(carpeta_audio)" in texto


# ═══════════════════════════════════════════════════════════════════════
# B5. LA PÁGINA Y LUCY NO PROMETEN LO QUE NO VEN
# ═══════════════════════════════════════════════════════════════════════

def test_la_frase_de_que_lucy_no_ve_el_disco_esta_para_la_casa_y_para_solo_ver(uno):
    for quien in ("casa", "ver"):
        assert FRASE in _el_bloque_de_audio(_pintar(uno, AUDIO, quien)), quien


def test_ninguna_frase_de_la_linea_de_audio_dice_que_la_carpeta_existe_o_se_creo(uno):
    for quien in ("casa", "ver"):
        texto = re.sub(r"<[^>]+>", " ", _el_bloque_de_audio(_pintar(uno, AUDIO, quien))).lower()
        for promesa in ("existe", "creada", "se creó", "encontrad", "montado", "disponible", "abrir", "vacía", "pesa"):
            assert promesa not in texto, (quien, promesa)


def test_lucy_por_telegram_ve_la_columna_y_sabe_que_no_ve_el_disco():
    from cerebro import consultar
    nota = consultar.NOTAS_DE_COLUMNA[("proyectos", "carpeta_audio")]
    assert "NO ve ese disco" in nota and "no sabe si la carpeta existe" in nota and "NULL = sin escribir" in nota
    assert "carpeta_audio" in db.columnas_declaradas()["proyectos"]
    bloque = " ".join(consultar.BLOQUES["proyectos"].split())
    assert f"carpeta_audio ({nota})" in bloque
    consultar._validar("SELECT carpeta_audio FROM proyectos WHERE id = 1")
    # y la nota de la carpeta de Drive ya no habla de rutas de computadora
    assert "computadora" not in consultar.NOTAS_DE_COLUMNA[("proyectos", "carpeta")]


_TOCAN_EL_DISCO = {"os", "shutil", "subprocess", "pathlib", "glob", "tempfile"}


def _funciones_que_nombran_la_columna_y_tocan_el_disco(fuente: str) -> list[str]:
    """Las funciones de un módulo que NOMBRAN la columna de audio (una cadena que la contiene, o un identificador o atributo
    con `carpeta_audio`, `carpeta_de_audio` o `carpetas_de_audio`) y llaman a `open`, `Path`, o a algo de `os`/`shutil`/
    `subprocess`/`pathlib`/`glob`/`tempfile` con algún argumento que NO sea una constante (`os.environ.get("X")` no cuenta;
    `os.path.exists(ruta)` sí, aunque `ruta` sea un alias). FRONTERA: no sigue el valor a otra función ni ve un nombre armado
    en ejecución."""
    nombres = ("carpeta_audio", "carpeta_de_audio", "carpetas_de_audio")
    salida = []
    for fn in ast.walk(ast.parse(fuente)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nombra = tocan = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and any(x in n.value for x in nombres):
                nombra = True
            elif isinstance(n, ast.Name) and any(x in n.id for x in nombres):
                nombra = True
            elif isinstance(n, ast.Attribute) and any(x in n.attr for x in nombres):
                nombra = True
            if isinstance(n, ast.Call):
                f = n.func
                while isinstance(f, ast.Attribute):
                    f = f.value
                con_algo_variable = any(not isinstance(a, ast.Constant) for a in n.args) or any(
                    not isinstance(k.value, ast.Constant) for k in n.keywords)
                if (isinstance(f, ast.Name) and (f.id in _TOCAN_EL_DISCO or f.id in ("open", "Path", "listdir", "scandir"))
                        and con_algo_variable):
                    tocan = True
        if nombra and tocan:
            salida.append(fn.name)
    return sorted(salida)


def test_ninguna_funcion_de_produccion_que_nombra_la_columna_toca_el_disco():
    nombran = {}
    for archivo in _py_del_repo():
        fuente = archivo.read_text(encoding="utf-8")
        if re.search(r"carpeta_audio|carpeta_de_audio|carpetas_de_audio", fuente):
            nombran[str(archivo.relative_to(_ROOT))] = _funciones_que_nombran_la_columna_y_tocan_el_disco(fuente)
    # la sonda ve los archivos que de verdad la nombran, y ninguno toca el disco
    assert {"db/db.py", "acciones/crud.py", "web/app.py", "cerebro/consultar.py", "tools/humo.py"} <= set(nombran), sorted(nombran)
    assert {k: v for k, v in nombran.items() if v} == {}


def test_la_sonda_del_disco_ve_las_formas_de_tocarlo():
    for malo in ("def f(m):\n    return os.path.exists(m['carpeta_audio'])\n",
                 "def f(m):\n    return Path(m.carpeta_audio).is_dir()\n",
                 "def f(m):\n    return open(m['carpeta_audio'])\n",
                 "async def f(carpeta_audio):\n    subprocess.run(['ls', carpeta_audio])\n",
                 "def f(m):\n    return shutil.disk_usage(m['carpeta_audio'])\n",
                 "def f(m):\n    ruta = m['carpeta_audio']\n    return os.path.isdir(ruta)\n"):
        assert _funciones_que_nombran_la_columna_y_tocan_el_disco(malo) == ["f"], malo
    assert _funciones_que_nombran_la_columna_y_tocan_el_disco("def f(m):\n    return os.getcwd()\n") == []
    assert _funciones_que_nombran_la_columna_y_tocan_el_disco(
        "def f(m):\n    x = m['carpeta_audio']\n    return os.environ.get('DATABASE_URL')\n") == []
    assert _funciones_que_nombran_la_columna_y_tocan_el_disco("def f(m):\n    return m['carpeta_audio']\n") == []


# ═══════════════════════════════════════════════════════════════════════
# B7. «COPIAR» COPIA SU PROPIA RUTA
# ═══════════════════════════════════════════════════════════════════════

class _Cajas(HTMLParser):
    """Por cada botón `data-copiar-carpeta`: el texto del `<code class="carpeta-texto">` de la `.carpeta-contenido` más
    cercana que lo envuelve, y cuántos `<code class="carpeta-texto">` tiene esa caja."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila = []                     # [{'es_caja': bool, 'codigos': [str]}] de los <div> abiertos
        self.botones = []                  # [(caja, ...)]
        self._en_code = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            self.pila.append({"caja": "carpeta-contenido" in (a.get("class") or "").split(), "codigos": []})
        elif tag == "code" and "carpeta-texto" in (a.get("class") or "").split():
            self._en_code = []
        elif tag == "button" and "data-copiar-carpeta" in a:
            caja = next((d for d in reversed(self.pila) if d["caja"]), None)
            self.botones.append(caja)

    def handle_data(self, data):
        if self._en_code is not None:
            self._en_code.append(data)

    def handle_endtag(self, tag):
        if tag == "div" and self.pila:
            self.pila.pop()
        elif tag == "code" and self._en_code is not None:
            caja = next((d for d in reversed(self.pila) if d["caja"]), None)
            if caja is not None:
                caja["codigos"].append("".join(self._en_code))
            self._en_code = None


def _cajas_de_los_botones(html: str):
    p = _Cajas()
    p.feed(html)
    return [None if c is None else c["codigos"] for c in p.botones]


def test_cada_boton_de_copiar_esta_en_una_caja_con_un_solo_texto_y_es_el_suyo(uno):
    _poner(uno, carpeta=RUTA, carpeta_audio=AUDIO)
    html = ver(uno, p=1)
    assert _cajas_de_los_botones(html) == [[RUTA], [AUDIO]]
    assert html.count('id="guion-copiar-carpeta"') == 1 and len(re.findall(r"<button[^>]*data-copiar-carpeta", html)) == 2
    _poner(uno, carpeta=DRIVE, carpeta_audio=AUDIO)             # la carpeta es un enlace: solo el de audio
    html = ver(uno, p=1)
    assert _cajas_de_los_botones(html) == [[AUDIO]] and html.count('id="guion-copiar-carpeta"') == 1
    _poner(uno, carpeta=RUTA, carpeta_audio=None)               # al revés: solo el de la carpeta
    html = ver(uno, p=1)
    assert _cajas_de_los_botones(html) == [[RUTA]] and html.count('id="guion-copiar-carpeta"') == 1


def test_el_guion_solo_sale_si_la_pagina_dibuja_algun_boton(uno):
    _poner(uno, carpeta=DRIVE, carpeta_audio=None)
    assert 'id="guion-copiar-carpeta"' not in ver(uno, p=1) and "data-copiar-carpeta" not in ver(uno, p=1)
    _poner(uno, carpeta=RUTA, carpeta_audio=AUDIO)
    assert 'id="guion-copiar-carpeta"' not in pagina_ver(uno, p=1) and "data-copiar-carpeta" not in pagina_ver(uno, p=1)
    html = ver(uno, p=1, editar="carpeta_audio")                 # con el formulario de audio abierto la de audio no hay
    assert _cajas_de_los_botones(html) == [[RUTA]]


def test_el_guion_va_fuera_de_main_y_no_manda_nada(uno):
    _poner(uno, carpeta=RUTA, carpeta_audio=AUDIO)
    html = ver(uno, p=1)
    assert html.index('id="guion-copiar-carpeta"') > html.index("</main>")
    guion = re.sub(r"/\*.*?\*/", "", _guion_de_copiar(html), flags=re.S)
    assert re.search(r"fetch\(|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|\.submit\(|requestSubmit|"
                     r"location\s*[.=]|\.action|FormData|innerHTML|eval\(|document\.write|\.href", guion) is None
    assert re.findall(r'addEventListener\("(\w+)"', guion) == ["click"]


def _guion_de_copiar(html: str) -> str:
    m = re.search(r'<script id="guion-copiar-carpeta">(.*?)</script>', html, re.S)
    assert m, "la página no trae el guion de Copiar"
    return m.group(1)


# El `document`, el `window` y el `navigator` de mentira, con DOS cajas (la de la carpeta y la de audio). Qué afirman (y de
# dónde lo saqué):
#  · `Element.closest(selector)` devuelve el ancestro más cercano que cumple el selector, el propio elemento incluido.
#    Aquí: el botón de la carpeta está directamente en su caja; el de audio está dentro de un `.botones` que está en su caja
#    (como en la página real: la prueba de estructura de arriba lo comprueba sobre el HTML servido).
#  · `parentNode` del botón de audio es ese `.botones`, que NO contiene el `<code>`: `querySelector` ahí da nada.
#  · `document.querySelector(s)` devuelve el PRIMER elemento de toda la página que cumple: el `<code>` de la carpeta.
#  · `navigator.clipboard.writeText(t)` devuelve algo con `then(ok, fallo)` y llama a uno de los dos enseguida.
_ARNES_DOS_CAJAS = """
var __t = []; var __escribio = []; var __selecciones = [];
function setTimeout(f, ms) { __t.push({f: f, ms: ms}); return __t.length; }
var __oyentes = {};
var __codigoA = {textContent: %(A)s}; var __codigoB = {textContent: %(B)s};
var __cajaA = {querySelector: function (s) { return s === "code.carpeta-texto" ? __codigoA : null; }};
var __cajaB = {querySelector: function (s) { return s === "code.carpeta-texto" ? __codigoB : null; }};
var __botones = {querySelector: function (s) { return null; }};
var __botonA = {textContent: "Copiar", parentNode: __cajaA,
                closest: function (s) { return s === ".carpeta-contenido" ? __cajaA : null; }};
var __botonB = {textContent: "Copiar", parentNode: __botones,
                closest: function (s) { return s === ".carpeta-contenido" ? __cajaB : null; }};
function __clic(boton) {
  return {target: {closest: function (s) { return s === "[data-copiar-carpeta]" ? boton : null; }}};
}
var __seleccion = {removeAllRanges: function () {}, addRange: function (r) { __selecciones.push(r.nodo === __codigoB ? "B" : r.nodo === __codigoA ? "A" : "?"); }};
var window = {getSelection: function () { return __seleccion; }};
var document = {
  documentElement: {classList: {add: function (c) {}}},
  addEventListener: function (tipo, f) { __oyentes[tipo] = f; },
  querySelector: function (s) { return s === "code.carpeta-texto" ? __codigoA : null; },
  createRange: function () { return {nodo: null, selectNodeContents: function (n) { this.nodo = n; }}; }
};
%(NAVEGADOR)s
"""
_OK = ("var navigator = {clipboard: {writeText: function (t) { __escribio.push(t);"
       " return {then: function (ok, fallo) { ok(); }}; }}};")
_RECHAZA = ("var navigator = {clipboard: {writeText: function (t) { __escribio.push(t);"
            " return {then: function (ok, fallo) { fallo(); }}; }}};")
_FOTO_DOS = "JSON.stringify({a: __botonA.textContent, b: __botonB.textContent, escribio: __escribio, selecciones: __selecciones})"


def _correr_dos_cajas(guion: str, navegador: str, escenario: str) -> dict:
    arnes = (_ARNES_DOS_CAJAS % {"A": json.dumps(RUTA), "B": json.dumps(AUDIO), "NAVEGADOR": navegador}) + guion + "\n" + escenario
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", arnes], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip() or r.stderr.strip())


@hay_osascript
def test_js_cada_boton_copia_su_propia_ruta_y_solo_el_suyo_dice_copiado(uno):
    _poner(uno, carpeta=RUTA, carpeta_audio=AUDIO)
    guion = _guion_de_copiar(ver(uno, p=1))
    r = _correr_dos_cajas(guion, _OK, "__oyentes.click(__clic(__botonB));" + _FOTO_DOS)
    assert r["escribio"] == [AUDIO] and r["b"] == "Copiado" and r["a"] == "Copiar"
    r = _correr_dos_cajas(guion, _OK, "__oyentes.click(__clic(__botonA));" + _FOTO_DOS)
    assert r["escribio"] == [RUTA] and r["a"] == "Copiado" and r["b"] == "Copiar"
    r = _correr_dos_cajas(guion, _OK, "__oyentes.click(__clic(__botonA)); __oyentes.click(__clic(__botonB));" + _FOTO_DOS)
    assert r["escribio"] == [RUTA, AUDIO] and r["a"] == r["b"] == "Copiado"


@hay_osascript
def test_js_si_el_portapapeles_no_acepta_nunca_dice_copiado_y_deja_seleccionada_su_ruta(uno):
    _poner(uno, carpeta=RUTA, carpeta_audio=AUDIO)
    guion = _guion_de_copiar(ver(uno, p=1))
    r = _correr_dos_cajas(guion, _RECHAZA, "__oyentes.click(__clic(__botonB));" + _FOTO_DOS)
    assert r["b"] == "Cópialo a mano" and "Copiado" not in json.dumps(r) and r["selecciones"] == ["B"] and r["a"] == "Copiar"
