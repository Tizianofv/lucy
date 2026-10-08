"""Las notas y decisiones del proyecto (`notas.proyecto_id`, `notas.autor_chat_id`): parte 6 del diseño de
la página completa del proyecto (8-oct-2026).

Qué se vigila, por la ruta REAL (`POST /proyectos/{pid}/notas[/{nid}/editar|borrar]`), la plantilla REAL,
`db.crear_nota_de_proyecto` / `editar_nota_de_proyecto` / `borrar_nota_de_proyecto` / `crud.deshacer`
REALES y SQL que se ejecuta de verdad (SQLite con el `CREATE TABLE notas` y `bandeja` de `db/schema.sql`):

  1. la página carga con la base SIN migrar (SQLSTATE 42703 de verdad en el doble) y no dibuja el bloque;
     las tres rutas contestan que NO se guardó;
  2. qué texto vale (entradas inventadas con semilla y valores fijos), y que un rechazo no escribe nada;
  3. solo salen las notas VIVAS de ESTE proyecto VIVO (valores fijos, no el resultado de la función);
  4. el autor sale de la sesión (nunca del formulario), una nota de Telegram lo dice por su bandeja, una
     sin autor conocido lo dice, y la página no escribe un número de chat;
  5. quién ve y quién puede: la casa ve y escribe; solo ver ve y no escribe; sin sesión ni se ve ni se
     escribe; editar y borrar, solo quien la escribió (`db.puede_tocar_nota`);
  6. los tres hermanos (crear, editar, borrar) cumplen lo mismo: sesión, proyecto vivo, huella, aviso
     verdadero; una nota de OTRO proyecto, o de ninguno, no se toca por la ruta de este;
  7. escribir una nota cuenta como movimiento del proyecto (la consulta REAL del modelo), y todo sitio
     que calcula «último movimiento» (sacado del código) cuenta las notas;
  8. borrar deja la fila y su huella; `crud.deshacer` la devuelve; borrar y restaurar un proyecto no pierde
     sus notas;
  9. lo que se pinta sale escapado;
 10. el texto de la migración se EJECUTA (traducido a SQLite), el `INSERT` del proceso viejo anda con la
     tabla antes y después del cambio, y los escritores de `notas` salen del código con una sonda;
 11. lo que ya hacía una nota fuera de un proyecto no cambia: la lectura de lo de Tiziano para Code, el
     `INSERT` de Telegram, lo que Lucy ve por Telegram.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. La migración corre en SQLite con las traducciones declaradas de
    `test_fechas_de_proyecto._aplicar_migracion` (`ADD COLUMN IF NOT EXISTS` se emula mirando las
    columnas; `COMMENT ON` no corre). Que el `ALTER` pida por un instante el candado de `notas` y espere
    detrás de una consulta larga NO se ejerció aquí.
  · SQLite no hace que una excepción deshaga lo escrito antes (el doble de conexión no tiene
    transacciones): cada función de `db` valida TODO antes de la primera escritura, y esta prueba mide
    que después de un rechazo no queda nada.
  · La carrera de verdad entre dos conexiones se imita con un gancho que escribe justo antes del
    `UPDATE` (`_Intercala`); el éxito de `crud.deshacer` sobre una EDICIÓN usa `jsonb_populate_record` y no
    corre en SQLite (sí el de un borrado y el de una creación).
  · No es un navegador: el `textarea`, el guardado al salir del campo y cómo se ve el bloque no se ejercen.

Correr:  python3 -m pytest tests/test_notas_de_proyecto.py -q
"""
from __future__ import annotations

import ast
import json
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import test_base_m2 as b2
import test_crear_proyecto_telegram as _tct
import test_grupo_ia as g
from test_grupo_ia import _ROOT
from test_pagina_proyectos import (Mundo, gente, mundo, ver, ver_r, HOY, CREADO)  # noqa: F401
from test_fechas_de_proyecto import _correr
from _doble_postgres import ErrorSQL
import acciones.crud as crud
import config
import db.db as db
from _navegador import Navegador
import web.app as panel
import web.auth as auth

DUENO = config.CHAT_ID_DUENO
ROSI = 700100001
AJENO = 700100999
SIN_NOMBRE = 555000333          # un chat que escribió una nota y no tiene nombre en NOMBRES_POR_CHAT
LARGO = db.LARGO_NOTA_PROYECTO


# ═══════════════════════════════════════════════════════════════════════
# El mundo de prueba y las ayudas
# ═══════════════════════════════════════════════════════════════════════

def _bandeja(m, id, chat, origen="telegram"):
    m.con.execute("INSERT INTO bandeja (id, tipo_entrada, chat_id, origen) VALUES (?,?,?,?)",
                  (id, "texto", chat, origen))


def _nota(m, id, proyecto, texto, *, autor=None, bandeja=None, borrada=False, cuando=CREADO):
    m.con.execute(
        "INSERT INTO notas (id, bandeja_id, creado_en, contenido, proyecto_id, autor_chat_id, borrado_en) "
        "VALUES (?,?,?,?,?,?,?)",
        (id, bandeja, cuando.isoformat(), texto, proyecto, autor,
         "2026-09-02T00:00:00+00:00" if borrada else None))
    return id


def _filas(m):
    return [dict(f) for f in m.con.execute("SELECT * FROM notas ORDER BY id")]


def _nota_fila(m, id):
    f = m.con.execute("SELECT * FROM notas WHERE id = ?", (id,)).fetchone()
    return dict(f) if f is not None else None


def _huellas(m):
    return [dict(f) for f in m.con.execute("SELECT * FROM log_acciones ORDER BY id")]


@pytest.fixture
def uno(mundo):
    """Dos proyectos de la casa y uno cerrado."""
    mundo.proyecto(1, "Disco de prueba", area="CDS", responsable=DUENO)
    mundo.proyecto(2, "Otro proyecto", area="CDS", responsable=DUENO)
    return mundo


def _cliente(chat):
    c = Navegador(panel.app, base_url="https://testserver")
    if chat == "dueno":
        c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    elif chat == "rosi":
        c.cookies.set(panel.COOKIE, auth.crear_token(ROSI, auth.VIDA_SESION))
    elif chat == "ajeno":
        c.cookies.set(panel.COOKIE, auth.crear_token(AJENO, auth.VIDA_SESION))
    elif chat == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    return c


def crear(pid, campos, *, chat="dueno", files=None):
    return _cliente(chat).post(f"/proyectos/{pid}/notas", data=campos, files=files, follow_redirects=False)


def editar(pid, nid, campos, *, chat="dueno", files=None):
    return _cliente(chat).post(f"/proyectos/{pid}/notas/{nid}/editar", data=campos, files=files,
                               follow_redirects=False)


def borrar(pid, nid, *, chat="dueno"):
    return _cliente(chat).post(f"/proyectos/{pid}/notas/{nid}/borrar", data={}, follow_redirects=False)


def _donde(r) -> str:
    return r.headers["location"]


def pagina(m, chat="dueno", **consulta):
    r = _cliente(chat).get("/proyectos", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _bloque(html: str) -> str:
    """El bloque «Notas y decisiones» (desde su `id` hasta el cierre de su `section`), o ''."""
    if 'id="notas-del-proyecto"' not in html:
        return ""
    return html.split('id="notas-del-proyecto"', 1)[1].split("</section>", 1)[0]


def _lo_pintado(html: str) -> list[tuple[int, str, str]]:
    """`[(id, 'autor', 'texto')]` de cada nota del bloque, en el orden en que salen."""
    b = _bloque(html)
    salida = []
    for m in re.finditer(r'<div class="nota-item" id="nota-(\d+)">\s*<small>(.*?) · .*?</small>(.*?)(?=<div class="nota-item"|$)',
                         b, re.S):
        texto = re.search(r'<p class="texto">(.*?)</p>', m.group(3), re.S)
        salida.append((int(m.group(1)), m.group(2), texto.group(1) if texto else None))
    return salida


# ═══════════════════════════════════════════════════════════════════════
# 1. La página con la base SIN migrar
# ═══════════════════════════════════════════════════════════════════════

class _CurPg(g._Cur):
    """Un cursor de SQLite que, ante una columna que no existe, lanza lo que lanzaría psycopg contra
    Postgres: un error con `.sqlstate == '42703'`. SQLite lo dice de dos maneras: `no such column` al leer
    y `has no column named` al insertar."""

    async def execute(self, sql, params=()):
        try:
            return await super().execute(sql, params)
        except sqlite3.OperationalError as e:
            if "no such column" in str(e) or "has no column named" in str(e):
                raise ErrorSQL("42703", str(e)) from e
            raise


class _ConnPg(g._Conn):
    def cursor(self, row_factory=None):
        return _CurPg(self.con, como_dict=row_factory is not None)

    async def execute(self, sql, params=()):
        return await _CurPg(self.con, como_dict=False).execute(sql, params)


class _PoolPg(g._Pool):
    def connection(self):
        con = self.con

        class _CM:
            async def __aenter__(self_):
                return _ConnPg(con)

            async def __aexit__(self_, *e):
                return False
        return _CM()


def _sin_migrar(m, monkeypatch):
    m.con.execute("ALTER TABLE notas DROP COLUMN autor_chat_id")
    monkeypatch.setattr(db, "pool", _PoolPg(m.con))


def test_la_pagina_carga_con_la_base_sin_migrar_y_no_dibuja_el_bloque(uno, monkeypatch):
    _nota(uno, 1, 1, "una nota de Telegram de antes", bandeja=None)
    _sin_migrar(uno, monkeypatch)
    for consulta in ({"p": 1}, {"p": 1, "editar_nota": 1}, {"p": 1, "borrar_nota": 1}, {}, {"g": "CDS"}):
        html = ver(uno, **consulta)
        assert "Notas y decisiones" not in html and "/proyectos/1/notas" not in html
        assert "editar_nota=" not in html and "borrar_nota=" not in html
    assert 'id="de-que-se-trata"' in ver(uno, p=1)          # el resto de la página sigue


def test_sin_migrar_las_tres_rutas_dicen_que_no_se_guardo_nada_y_no_escriben(uno, monkeypatch):
    _nota(uno, 1, 1, "de antes", autor=None)
    _sin_migrar(uno, monkeypatch)
    antes = _filas(uno)
    for r in (crear(1, {"texto": "nueva"}), editar(1, 1, {"texto": "cambiada"}), borrar(1, 1)):
        assert r.status_code == 303 and "error=nota_sin_columna" in _donde(r), _donde(r)
        assert "hecho=" not in _donde(r)
    assert _filas(uno) == antes and _huellas(uno) == []
    assert "falta actualizar la base de datos" in ver_r(uno, p=1, error="nota_sin_columna")


def test_sin_migrar_la_lectura_dice_no_disponible_y_otro_error_no_se_traga(uno, monkeypatch):
    assert _correr(db.notas_de_proyectos()) == {}
    _sin_migrar(uno, monkeypatch)
    assert _correr(db.notas_de_proyectos()) is None
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
            _correr(db.notas_de_proyectos())


def test_armar_pagina_sin_notas_marca_no_disponible_aunque_el_proyecto_exista():
    m = db.armar_pagina([], [dict(id=1, nombre="Uno", descripcion=None, estado="activo", area=None,
                                  creado_en=CREADO, responsable_chat_id=None, cliente_nombre=None)],
                        [], [], [], {}, HOY)
    assert m["proyectos"][1]["notas_disponibles"] is False and m["proyectos"][1]["notas"] == []
    m = db.armar_pagina([], [dict(id=1, nombre="Uno", descripcion=None, estado="activo", area=None,
                                  creado_en=CREADO, responsable_chat_id=None, cliente_nombre=None)],
                        [], [], [], {}, HOY, notas={})
    assert m["proyectos"][1]["notas_disponibles"] is True and m["proyectos"][1]["notas"] == []


# ═══════════════════════════════════════════════════════════════════════
# 2. La puerta del texto
# ═══════════════════════════════════════════════════════════════════════

_QUE_VALEN = [
    ("Se aprobó grabar las voces en una sesión", "Se aprobó grabar las voces en una sesión"),
    ("  con espacios  ", "con espacios"), ("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("a\n\nb", "a\n\nb"),
    ("x" * LARGO, "x" * LARGO), ("  " + "x" * LARGO + "  ", "x" * LARGO), ("ñandú — «ok» 🙂", "ñandú — «ok» 🙂"),
    ("<b>no es HTML</b>", "<b>no es HTML</b>"),
]
_QUE_NO = [
    ("", "vacio"), ("   ", "vacio"), ("\n\n", "vacio"), ("\r\n \r", "vacio"),
    ("x" * (LARGO + 1), "largo"), ("  " + "x" * (LARGO + 1), "largo"),
    ("a\x00b", "caracteres"), ("\x00", "caracteres"), ("a\ud800b", "caracteres"),
    (None, "tipo"), (5, "tipo"), (True, "tipo"), (["a"], "tipo"), (b"a", "tipo"),
]


@pytest.mark.parametrize("valor,esperado", _QUE_VALEN)
def test_la_puerta_del_texto_deja_pasar_texto(valor, esperado):
    assert db.texto_de_nota_que_vale(valor) == esperado


@pytest.mark.parametrize("valor,clave", _QUE_NO)
def test_la_puerta_del_texto_rechaza_lo_demas_diciendo_por_que(valor, clave):
    with pytest.raises(db.NotaNoVale) as e:
        db.texto_de_nota_que_vale(valor)
    assert e.value.clave == clave
    assert "x" * 20 not in str(e.value)            # el mensaje nunca repite lo pedido


def test_la_puerta_con_entradas_inventadas_con_semilla():
    """Texto armado al azar (semilla fija) con espacios, saltos, NUL y símbolos: lo que sale de la puerta
    nunca lleva NUL, ni espacios de alrededor, ni `\\r`, y mide a lo sumo el tope; y lo que lleva un NUL se
    rechaza siempre."""
    azar = random.Random(20261008)
    alfabeto = list("abc xyz\n\r\t<>\"'&ñ🙂-") + ["\x00"]
    for _ in range(600):
        t = "".join(azar.choice(alfabeto) for _ in range(azar.randint(0, 40)))
        if "\x00" in t:
            with pytest.raises(db.NotaNoVale):
                db.texto_de_nota_que_vale(t)
            continue
        try:
            limpio = db.texto_de_nota_que_vale(t)
        except db.NotaNoVale as e:
            assert e.clave == "vacio" and not t.strip()
            continue
        assert limpio == limpio.strip() and "\r" not in limpio and 0 < len(limpio) <= LARGO


def test_el_tope_es_el_de_los_otros_textos_libres_de_la_casa():
    """De dónde sale el 2000 (no se midió producción): es el de los comentarios y la descripción."""
    assert LARGO == 2000 == db.LARGO_COMENTARIO == db.LARGO_DESCRIPCION_PROYECTO


# ═══════════════════════════════════════════════════════════════════════
# 3. Solo salen las notas VIVAS de ESTE proyecto vivo
# ═══════════════════════════════════════════════════════════════════════

def test_solo_salen_las_notas_vivas_de_este_proyecto(uno):
    uno.proyecto(3, "En la papelera", area="CDS", borrado=True)
    _nota(uno, 10, 1, "viva de uno", autor=DUENO, cuando=CREADO)
    _nota(uno, 11, 1, "borrada de uno", autor=DUENO, borrada=True)
    _nota(uno, 12, 2, "viva de dos", autor=DUENO)
    _nota(uno, 13, None, "no es de ningún proyecto", autor=DUENO)
    _nota(uno, 14, 3, "de un proyecto en la papelera", autor=DUENO)
    html1, html2 = ver(uno, p=1), ver(uno, p=2)
    assert [n[0] for n in _lo_pintado(html1)] == [10]
    assert [n[0] for n in _lo_pintado(html2)] == [12]
    for texto in ("borrada de uno", "viva de dos", "no es de ningún proyecto", "de un proyecto en la papelera"):
        assert texto not in _bloque(html1), texto
    for pagina_ in (ver(uno), ver(uno, g="CDS"), ver(uno, sin_grupo=1)):
        assert "no es de ningún proyecto" not in pagina_


def test_la_lectura_trae_solo_lo_vivo_de_proyectos_vivos_la_mas_nueva_primero(uno):
    uno.proyecto(3, "En la papelera", area="CDS", borrado=True)
    ayer, hoy = CREADO - timedelta(days=1), CREADO
    _nota(uno, 1, 1, "vieja", autor=DUENO, cuando=ayer)
    _nota(uno, 2, 1, "nueva", autor=DUENO, cuando=hoy)
    _nota(uno, 3, 1, "misma hora, id mayor", autor=DUENO, cuando=hoy)
    _nota(uno, 4, 1, "borrada", autor=DUENO, borrada=True)
    _nota(uno, 5, 3, "del proyecto borrado", autor=DUENO)
    _nota(uno, 6, None, "sin proyecto", autor=DUENO)
    leidas = _correr(db.notas_de_proyectos())
    assert {k: [f["id"] for f in v] for k, v in leidas.items()} == {1: [3, 2, 1]}


def test_una_nota_de_proyecto_cerrado_tambien_sale(uno):
    uno.proyecto(4, "Cerrado", area="CDS", estado="cerrado")
    _nota(uno, 1, 4, "la lección de este cierre", autor=DUENO)
    html = ver(uno, p=4)
    assert [n[2] for n in _lo_pintado(html)] == ["la lección de este cierre"]


def test_el_bloque_sale_con_su_formulario_aunque_el_proyecto_no_tenga_notas(uno):
    html = ver(uno, p=1)
    assert "Notas y decisiones" in html and "Todavía no hay notas." in html
    assert 'action="/proyectos/1/notas"' in html


# ═══════════════════════════════════════════════════════════════════════
# 4. El autor: de la sesión, nunca del formulario; el nombre, nunca el número
# ═══════════════════════════════════════════════════════════════════════

def test_la_pagina_dice_el_nombre_del_autor_y_nunca_su_numero(uno):
    _bandeja(uno, 1, ROSI, "telegram")
    _bandeja(uno, 2, DUENO, "banco")             # el chat de un correo del banco: NO dice quién lo escribió
    _bandeja(uno, 3, DUENO, "correo")            # a quién iba el aviso, no quién escribió el texto
    _bandeja(uno, 4, SIN_NOMBRE, "telegram")
    _nota(uno, 10, 1, "del panel, del dueño", autor=DUENO)
    _nota(uno, 11, 1, "de Telegram, de Rosi", bandeja=1)
    _nota(uno, 12, 1, "de la bandeja de un banco", bandeja=2)
    _nota(uno, 13, 1, "de la bandeja de un correo", bandeja=3)
    _nota(uno, 14, 1, "sin bandeja ni autor")
    _nota(uno, 15, 1, "de Telegram de un chat sin nombre", bandeja=4)
    _nota(uno, 16, 1, "del panel de un chat sin nombre", autor=SIN_NOMBRE)
    _nota(uno, 17, 1, "el panel manda sobre la bandeja", autor=ROSI, bandeja=2)
    html = ver(uno, p=1)
    assert {i: a for i, a, _ in _lo_pintado(html)} == {
        10: "Persona Uno", 11: "Persona Dos", 12: "Autor desconocido", 13: "Autor desconocido",
        14: "Autor desconocido", 15: "Autor desconocido", 16: "Autor desconocido", 17: "Persona Dos"}
    for numero in (str(DUENO), str(ROSI), str(SIN_NOMBRE)):
        assert numero not in html, numero


def test_el_autor_es_la_sesion_aunque_el_formulario_diga_otra_cosa(uno):
    r = crear(1, {"texto": "de Rosi", "autor_chat_id": str(DUENO), "autor": "Persona Uno", "chat": str(DUENO)},
              chat="rosi")
    assert r.status_code == 303 and _donde(r) == "/proyectos?hecho=nota&p=1#notas-del-proyecto"
    f, = _filas(uno)
    assert f["autor_chat_id"] == ROSI and f["contenido"] == "de Rosi" and f["proyecto_id"] == 1
    assert f["bandeja_id"] is None and f["borrado_en"] is None
    assert [a for _, a, _ in _lo_pintado(ver(uno, p=1))] == ["Persona Dos"]


def test_ninguna_ruta_ni_funcion_de_notas_lee_el_autor_del_formulario():
    """Sonda sobre el código: las tres rutas leen del formulario solo `texto`; el autor es `_sesion(request)`."""
    arbol = ast.parse((_ROOT / "web" / "app.py").read_text(encoding="utf-8"))
    nombres = {"escribir_nota_de_proyecto", "editar_nota_de_proyecto", "borrar_nota_de_proyecto"}
    vistas = 0
    for f in ast.walk(arbol):
        if isinstance(f, ast.AsyncFunctionDef) and f.name in nombres:
            vistas += 1
            leidas = {n.args[0].value for n in ast.walk(f)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "get" and n.args and isinstance(n.args[0], ast.Constant)}
            assert leidas <= {"texto"}, (f.name, leidas)
            fuente = ast.unparse(f)
            assert "_sesion(request)" in fuente
    assert vistas == 3


# ═══════════════════════════════════════════════════════════════════════
# 5. Quién ve y quién puede
# ═══════════════════════════════════════════════════════════════════════

def _solo_ver(uno):
    _nota(uno, 10, 1, "una nota del panel", autor=DUENO)
    return pagina(uno, chat="ver", p=1)


def test_solo_ver_lee_las_notas_y_no_tiene_ningun_control(uno):
    html = _solo_ver(uno)
    assert [n[2] for n in _lo_pintado(html)] == ["una nota del panel"]
    b = _bloque(html)
    for control in ("<form", "<textarea", "<button", "<input", "Cambiar", "Borrar", "editar_nota=", "borrar_nota=",
                    "/proyectos/1/notas"):
        assert control not in b, control
    assert "<script" not in html


def test_solo_ver_sin_notas_no_dibuja_el_bloque(uno):
    html = pagina(uno, chat="ver", p=1)
    assert "Notas y decisiones" not in html and "<textarea" not in html


@pytest.mark.parametrize("consulta", [{"editar_nota": 10}, {"borrar_nota": 10}, {"editar_nota": 10, "borrar_nota": 10}])
def test_solo_ver_con_la_direccion_escrita_a_mano_no_abre_nada(uno, consulta):
    _nota(uno, 10, 1, "una nota del panel", autor=DUENO)
    html = pagina(uno, chat="ver", p=1, **consulta)
    assert "<form" not in _bloque(html) and "Sí, borrar la nota" not in html and "<textarea" not in html


@pytest.mark.parametrize("chat", [None, "ver", "ajeno"])
def test_sin_sesion_de_la_casa_ninguna_ruta_escribe(uno, chat):
    _nota(uno, 10, 1, "viva", autor=DUENO)
    antes = _filas(uno)
    for r in (crear(1, {"texto": "x"}, chat=chat), editar(1, 10, {"texto": "x"}, chat=chat), borrar(1, 10, chat=chat)):
        assert r.status_code == 401, r.status_code
    assert _filas(uno) == antes and _huellas(uno) == []


def test_sin_sesion_la_pagina_no_se_ve(uno):
    _nota(uno, 10, 1, "viva", autor=DUENO)
    r = _cliente(None).get("/proyectos", params={"p": 1})
    assert r.status_code == 401 and "una nota" not in r.text and "viva" not in r.text


def test_la_casa_ofrece_cambiar_y_borrar_solo_en_las_notas_que_escribio(uno):
    _bandeja(uno, 1, DUENO, "telegram")
    _bandeja(uno, 2, ROSI, "telegram")
    _nota(uno, 10, 1, "del dueño por el panel", autor=DUENO)
    _nota(uno, 11, 1, "del dueño por Telegram", bandeja=1)
    _nota(uno, 12, 1, "de Rosi por el panel", autor=ROSI)
    _nota(uno, 13, 1, "de Rosi por Telegram", bandeja=2)
    _nota(uno, 14, 1, "sin autor conocido")
    for chat, suyas in (("dueno", {10, 11}), ("rosi", {12, 13})):
        html = pagina(uno, chat=chat, p=1)
        con_cambiar = set(int(x) for x in re.findall(r"editar_nota=(\d+)#nota-", html))
        con_borrar = set(int(x) for x in re.findall(r"borrar_nota=(\d+)#nota-", html))
        assert con_cambiar == suyas and con_borrar == suyas, (chat, con_cambiar, con_borrar)


def test_el_formulario_y_la_pregunta_solo_se_dibujan_para_la_nota_propia(uno):
    _nota(uno, 10, 1, "mía", autor=DUENO)
    _nota(uno, 11, 1, "ajena", autor=ROSI)
    mia = pagina(uno, p=1, editar_nota=10)
    assert 'action="/proyectos/1/notas/10/editar"' in mia and ">mía</textarea>" in mia
    ajena = pagina(uno, p=1, editar_nota=11)
    assert "/notas/11/editar" not in ajena and ">ajena</textarea>" not in ajena and '<p class="texto">ajena</p>' in ajena
    assert 'action="/proyectos/1/notas/10/borrar"' in pagina(uno, p=1, borrar_nota=10)
    assert "/notas/11/borrar" not in pagina(uno, p=1, borrar_nota=11)


# ═══════════════════════════════════════════════════════════════════════
# 6. Crear, editar y borrar: los tres hermanos cumplen lo mismo
# ═══════════════════════════════════════════════════════════════════════

def test_crear_escribe_con_huella_de_panel_y_la_pagina_lo_dice_una_vez(uno):
    r = crear(1, {"texto": "  La banda aprobó\r\nuna sola sesión  "})
    assert r.status_code == 303 and _donde(r) == "/proyectos?hecho=nota&p=1#notas-del-proyecto"
    f, = _filas(uno)
    assert f["contenido"] == "La banda aprobó\nuna sola sesión" and f["autor_chat_id"] == DUENO
    h, = _huellas(uno)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "crear", "notas", f["id"])
    assert h["antes"] is None and json.loads(h["despues"])["contenido"] == "La banda aprobó\nuna sola sesión"
    assert "Nota guardada." in ver_r(uno, hecho="nota", p=1)


def test_editar_escribe_con_huella_con_el_antes_y_el_despues(uno):
    _nota(uno, 10, 1, "antes", autor=DUENO)
    r = editar(1, 10, {"texto": " después "})
    assert r.status_code == 303 and _donde(r) == "/proyectos?hecho=nota_editada&p=1#nota-10"
    assert _nota_fila(uno, 10)["contenido"] == "después"
    h, = _huellas(uno)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "editar", "notas", 10)
    assert json.loads(h["antes"])["contenido"] == "antes" and json.loads(h["despues"])["contenido"] == "después"
    assert json.loads(h["antes"])["autor_chat_id"] == DUENO == json.loads(h["despues"])["autor_chat_id"]
    assert "Nota cambiada." in ver_r(uno, hecho="nota_editada", p=1)


def test_una_direccion_escrita_a_mano_no_dice_que_se_guardo_ninguna_nota(uno):
    """La puerta de avisos (`web/avisos.py`): sin el recibo de un POST de verdad, ningún aviso de nota sale."""
    for hecho in ("nota", "nota_editada", "nota_borrada"):
        assert 'class="aviso ok"' not in ver(uno, hecho=hecho, p=1), hecho
    assert 'class="aviso"' not in ver(uno, error="nota_ajena", p=1)


def test_borrar_marca_la_fila_deja_la_huella_y_no_la_borra_de_verdad(uno):
    _nota(uno, 10, 1, "para borrar", autor=DUENO)
    r = borrar(1, 10)
    assert r.status_code == 303 and _donde(r) == "/proyectos?hecho=nota_borrada&p=1#notas-del-proyecto"
    f = _nota_fila(uno, 10)
    assert f is not None and f["borrado_en"] is not None and f["contenido"] == "para borrar"
    h, = _huellas(uno)
    assert (h["actor"], h["accion"], h["tabla"], h["registro_id"]) == ("panel", "borrar", "notas", 10)
    assert json.loads(h["antes"])["contenido"] == "para borrar" and h["despues"] is None
    assert _lo_pintado(ver(uno, p=1)) == []
    assert "Nota borrada." in ver_r(uno, hecho="nota_borrada", p=1)


def test_una_nota_que_no_es_de_este_proyecto_no_se_toca_por_la_ruta_de_este(uno):
    _nota(uno, 20, 2, "de otro proyecto", autor=DUENO)
    _nota(uno, 21, None, "de ningún proyecto", autor=DUENO)
    _nota(uno, 22, 1, "ya borrada", autor=DUENO, borrada=True)
    antes = _filas(uno)
    for nid in (20, 21, 22, 999):
        for r in (editar(1, nid, {"texto": "cambiada"}), borrar(1, nid)):
            assert "error=nota_no_esta" in _donde(r), (nid, _donde(r))
    assert _filas(uno) == antes and _huellas(uno) == []
    # y la misma nota, por la ruta de SU proyecto, sí
    assert "hecho=nota_editada" in _donde(editar(2, 20, {"texto": "cambiada"}))


def test_solo_quien_la_escribio_edita_o_borra_y_el_otro_ve_que_no_se_cambio_nada(uno):
    _bandeja(uno, 1, ROSI, "telegram")
    _nota(uno, 10, 1, "del dueño", autor=DUENO)
    _nota(uno, 11, 1, "de Rosi por Telegram", bandeja=1)
    _nota(uno, 12, 1, "sin autor conocido")
    antes = _filas(uno)
    for nid, intruso in ((10, "rosi"), (11, "dueno"), (12, "dueno"), (12, "rosi")):
        for r in (editar(1, nid, {"texto": "pisada"}, chat=intruso), borrar(1, nid, chat=intruso)):
            assert "error=nota_ajena" in _donde(r) and "hecho=" not in _donde(r), (nid, intruso, _donde(r))
    assert _filas(uno) == antes and _huellas(uno) == []
    assert "Solo quien escribió una nota puede cambiarla o borrarla: NO se cambió nada." in ver_r(uno, error="nota_ajena", p=1)
    # el dueño de la de Telegram de Rosi no es el dueño: ella sí la toca
    assert "hecho=nota_editada" in _donde(editar(1, 11, {"texto": "mejor"}, chat="rosi"))


def test_un_texto_que_no_vale_no_escribe_ni_deja_huella(uno):
    _nota(uno, 10, 1, "intacta", autor=DUENO)
    antes = _filas(uno)
    casos = [("", "nota_vacio"), ("   ", "nota_vacio"), ("x" * (LARGO + 1), "nota_largo"), ("a\x00b", "nota_caracteres")]
    for texto, error in casos:
        r = crear(1, {"texto": texto})
        assert f"error={error}" in _donde(r) and "hecho=" not in _donde(r), (texto[:5], _donde(r))
        r = editar(1, 10, {"texto": texto})
        assert f"error={error}" in _donde(r) and "editar_nota=10" in _donde(r), (texto[:5], _donde(r))
    assert _filas(uno) == antes and _huellas(uno) == []
    # el texto nunca viaja en la dirección
    assert "xxxx" not in _donde(crear(1, {"texto": "x" * (LARGO + 1)}))


def test_un_post_sin_el_campo_o_con_un_archivo_en_vez_de_texto_no_escribe(uno):
    _nota(uno, 10, 1, "intacta", autor=DUENO)
    antes = _filas(uno)
    assert "error=nota_invalida" in _donde(crear(1, {}))
    assert "error=nota_invalida" in _donde(editar(1, 10, {}))
    r = crear(1, {}, files={"texto": ("n.txt", b"un archivo", "text/plain")})
    assert "error=nota_tipo" in _donde(r)
    r = editar(1, 10, {}, files={"texto": ("n.txt", b"un archivo", "text/plain")})
    assert "error=nota_tipo" in _donde(r)
    assert _filas(uno) == antes and _huellas(uno) == []


def test_igual_al_que_habia_no_escribe_ni_dice_guardado(uno):
    _nota(uno, 10, 1, "igual", autor=DUENO)
    r = editar(1, 10, {"texto": "  igual \r\n"})
    assert "error=nota_igual" in _donde(r) and "hecho=" not in _donde(r)
    assert _huellas(uno) == []
    assert "La nota ya decía eso: no cambió nada." in ver_r(uno, error="nota_igual", p=1)


def test_un_proyecto_que_no_existe_o_esta_en_la_papelera_no_recibe_notas(uno):
    uno.proyecto(3, "En la papelera", area="CDS", borrado=True)
    _nota(uno, 10, 3, "de un proyecto borrado", autor=DUENO)
    antes = _filas(uno)
    for pid in (3, 999):
        assert "error=nota_no_esta" in _donde(crear(pid, {"texto": "x"})), pid
        assert "error=nota_no_esta" in _donde(editar(pid, 10, {"texto": "x"})), pid
        assert "error=nota_no_esta" in _donde(borrar(pid, 10)), pid
    assert _filas(uno) == antes and _huellas(uno) == []


def test_un_proyecto_cerrado_si_recibe_notas_como_las_de_telegram(uno):
    uno.proyecto(4, "Cerrado", area="CDS", estado="cerrado")
    assert "hecho=nota" in _donde(crear(4, {"texto": "lo que aprendimos"}))
    assert [f["proyecto_id"] for f in _filas(uno)] == [4]


class _Intercala:
    """Una conexión que, justo ANTES de un `UPDATE notas SET contenido`, deja que otra persona escriba en la
    misma fila: la carrera de verdad entre dos conexiones, imitada con un gancho."""

    def __init__(self, con, antes_del_update):
        self.con, self.gancho, self.hecho = con, antes_del_update, False

    def conexion(self, base):
        yo = self
        original = base.execute

        async def execute(sql, params=()):
            if not yo.hecho and sql.lstrip().startswith("UPDATE notas SET contenido"):
                yo.hecho = True
                yo.gancho(yo.con)
            return await original(sql, params)
        base.execute = execute
        return base


def test_si_otro_cambia_la_nota_en_el_medio_no_se_escribe_nada_y_se_dice(uno, monkeypatch):
    _nota(uno, 10, 1, "texto leído", autor=DUENO)
    inter = _Intercala(uno.con, lambda con: con.execute("UPDATE notas SET contenido = 'lo que puso otro' WHERE id = 10"))
    pool = g._Pool(uno.con)
    original = pool.connection

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def conexion():
        async with original() as c:
            yield inter.conexion(c)
    pool.connection = conexion
    monkeypatch.setattr(db, "pool", pool)
    r = editar(1, 10, {"texto": "lo que yo quería"})
    assert inter.hecho and "error=nota_cambio" in _donde(r) and "editar_nota=10" in _donde(r)
    assert _nota_fila(uno, 10)["contenido"] == "lo que puso otro" and _huellas(uno) == []
    assert "La nota cambió mientras la editabas: lo que escribiste NO se guardó." in ver_r(uno, error="nota_cambio", p=1)


# Las tres funciones de `db` son LA puerta (la ruta solo traduce): llamadas directo, sin pasar por la ruta,
# cada una rechaza a quien no entra al panel y no escribe nada.

@pytest.mark.parametrize("chat", [None, AJENO, SIN_NOMBRE])
def test_las_tres_funciones_de_db_rechazan_por_si_solas_a_quien_no_entra_al_panel(uno, chat):
    _nota(uno, 10, 1, "de alguien que no entra", autor=AJENO)
    _nota(uno, 11, 1, "de otro que no entra", autor=SIN_NOMBRE)
    antes = _filas(uno)
    with pytest.raises(db.NotaAjena):
        _correr(db.crear_nota_de_proyecto(1, chat, "x"))
    for nid in (10, 11):
        with pytest.raises(db.NotaAjena):
            _correr(db.editar_nota_de_proyecto(nid, 1, chat, "x"))
        with pytest.raises(db.NotaAjena):
            _correr(db.borrar_nota_de_proyecto(nid, 1, chat))
    assert _filas(uno) == antes and _huellas(uno) == []


def test_las_tres_funciones_de_db_dicen_la_verdad_de_lo_que_no_esta(uno):
    uno.proyecto(3, "En la papelera", area="CDS", borrado=True)
    _nota(uno, 10, 3, "de un proyecto borrado", autor=DUENO)
    _nota(uno, 11, 2, "de otro proyecto", autor=DUENO)
    with pytest.raises(db.NotaNoEsta):
        _correr(db.crear_nota_de_proyecto(3, DUENO, "x"))
    for pid, nid in ((3, 10), (1, 11), (1, 999)):
        with pytest.raises(db.NotaNoEsta):
            _correr(db.editar_nota_de_proyecto(nid, pid, DUENO, "x"))
        with pytest.raises(db.NotaNoEsta):
            _correr(db.borrar_nota_de_proyecto(nid, pid, DUENO))
    assert _huellas(uno) == []


def test_una_nota_de_telegram_mas_larga_que_el_tope_se_ve_entera_y_no_se_puede_guardar_sin_recortarla(uno):
    """La frontera: Telegram no limita el largo de una nota, el panel sí (`LARGO_NOTA_PROYECTO`)."""
    _bandeja(uno, 1, DUENO, "telegram")
    larga = "z" * (LARGO + 500)
    _nota(uno, 10, 1, larga, bandeja=1)
    assert larga in ver(uno, p=1)
    r = editar(1, 10, {"texto": larga})
    assert "error=nota_largo" in _donde(r) and _nota_fila(uno, 10)["contenido"] == larga and _huellas(uno) == []
    assert "hecho=nota_editada" in _donde(editar(1, 10, {"texto": larga[:LARGO]}))


# ═══════════════════════════════════════════════════════════════════════
# 7. Escribir una nota es un movimiento del proyecto
# ═══════════════════════════════════════════════════════════════════════

def _ultimo(m, pid):
    return _correr(db.pagina_de_proyectos(HOY))["proyectos"][pid]["ultimo"]


def test_una_nota_cuenta_como_movimiento_del_proyecto_con_la_consulta_real(uno):
    uno.proyecto(5, "Dormido", area="CDS", creado=CREADO - timedelta(days=30))
    base = _ultimo(uno, 5)
    reciente = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    nid = _nota(uno, 10, 5, "una nota", autor=DUENO)
    uno.huella("crear", "notas", nid, reciente)
    assert _ultimo(uno, 5) == reciente != base
    assert _correr(db.pagina_de_proyectos(HOY))["proyectos"][5]["dormido"] is False


def test_las_huellas_de_telegram_borrar_y_editar_de_una_nota_tambien_cuentan(uno):
    uno.proyecto(5, "Dormido", area="CDS", creado=CREADO - timedelta(days=30))
    _nota(uno, 10, 5, "de Telegram", autor=None, borrada=True)
    for accion, dia in (("crear", 2), ("editar", 3), ("borrar", 4), ("deshacer", 5)):
        cuando = datetime(2026, 10, dia, 12, 0, tzinfo=timezone.utc)
        uno.huella(accion, "notas", 10, cuando)
        assert _ultimo(uno, 5) == cuando, accion


def test_la_huella_de_una_nota_sin_proyecto_o_de_otro_proyecto_no_mueve_a_este(uno):
    uno.proyecto(5, "Quieto", area="CDS", creado=CREADO - timedelta(days=30))
    antes = _ultimo(uno, 5)
    _nota(uno, 10, None, "sin proyecto", autor=DUENO)
    _nota(uno, 11, 1, "de otro", autor=DUENO)
    reciente = CREADO + timedelta(hours=3)
    uno.huella("crear", "notas", 10, reciente)
    uno.huella("crear", "notas", 11, reciente)
    assert _ultimo(uno, 5) == antes
    assert _ultimo(uno, 1) == reciente


def test_las_acciones_automaticas_de_una_nota_no_mueven(uno):
    uno.proyecto(5, "Quieto", area="CDS", creado=CREADO - timedelta(days=30))
    antes = _ultimo(uno, 5)
    _nota(uno, 10, 5, "x", autor=DUENO)
    uno.huella("avisar", "notas", 10, datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))
    assert _ultimo(uno, 5) == antes


def test_todo_sitio_que_calcula_el_ultimo_movimiento_cuenta_las_notas():
    """Hermanos, sacados del código: cada cadena de SQL que mira `log_acciones` Y los comentarios de las
    tareas (la firma de «qué es moverse») tiene que mirar también las notas. Hoy es una sola
    (`pagina_de_proyectos`); un segundo sitio que copie el cálculo sin las notas pone roja esta prueba."""
    sitios = []
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            for texto in _tct._sql_de_una_funcion(fn):
                if "log_acciones" in texto and "comentarios_tarea" in texto and "UNION" in texto.upper():
                    sitios.append((rel, fn.name, "'notas'" in texto and "notas n" in texto))
    assert sitios == [("db/db.py", "pagina_de_proyectos", True)], sitios


def test_la_sonda_del_ultimo_movimiento_ve_un_sitio_inventado():
    arbol = ast.parse("async def inventado(c):\n    await c.execute('SELECT 1 FROM log_acciones l "
                      "JOIN comentarios_tarea c ON 1 UNION ALL SELECT 2')\n")
    f = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef))
    textos = _tct._sql_de_una_funcion(f)
    assert any("log_acciones" in t and "comentarios_tarea" in t and "UNION" in t.upper() for t in textos)
    assert not any("'notas'" in t for t in textos)


# ═══════════════════════════════════════════════════════════════════════
# 8. Borrar y deshacer; el proyecto borrado y restaurado
# ═══════════════════════════════════════════════════════════════════════

def test_deshacer_la_huella_de_borrar_devuelve_la_nota_a_la_pagina(uno):
    _nota(uno, 10, 1, "para borrar y volver", autor=DUENO)
    borrar(1, 10)
    assert _lo_pintado(ver(uno, p=1)) == []
    log_id = _huellas(uno)[0]["id"]
    assert _correr(crud.deshacer(log_id)) == "lo que había archivado"
    f = _nota_fila(uno, 10)
    assert f["borrado_en"] is None and f["autor_chat_id"] == DUENO and f["contenido"] == "para borrar y volver"
    assert [n[2] for n in _lo_pintado(ver(uno, p=1))] == ["para borrar y volver"]


def test_deshacer_la_huella_de_crear_saca_la_nota(uno):
    crear(1, {"texto": "una de más"})
    log_id = _huellas(uno)[0]["id"]
    _correr(crud.deshacer(log_id))
    assert _lo_pintado(ver(uno, p=1)) == [] and _filas(uno)[0]["borrado_en"] is not None


def test_borrar_el_proyecto_no_toca_las_notas_y_restaurarlo_las_devuelve(uno):
    _nota(uno, 10, 1, "sobrevive", autor=DUENO)
    _correr(crud.borrar("proyectos", 1, "prueba", actor="panel"))
    assert _nota_fila(uno, 10)["borrado_en"] is None            # la nota no se tocó
    assert "sobrevive" not in ver(uno, p=2) and _lo_pintado(ver(uno)) == []
    _correr(crud.deshacer_borrado("proyectos", 1))              # lo que hace «Restaurar» de la Papelera
    assert [n[2] for n in _lo_pintado(ver(uno, p=1))] == ["sobrevive"]


def test_la_papelera_de_la_pagina_no_lista_notas_y_sigue_andando(uno):
    """Dicho tal cual: borrar una nota deja `borrado_en` y su huella (se devuelve con `crud.deshacer`), pero
    la pantalla Papelera de proyectos y tareas NO la lista ni la restaura (`crud.deshacer_borrado` solo
    sabe de proyectos y tareas). Esta prueba fija ese límite y que lo de siempre sigue andando."""
    _nota(uno, 10, 1, "borrada", autor=DUENO)
    borrar(1, 10)
    lo_borrado = _correr(db.papelera_de_proyectos_y_tareas())
    assert set(lo_borrado) == {"proyectos", "tareas"} and lo_borrado["proyectos"] == [] and lo_borrado["tareas"] == []
    with pytest.raises(ValueError):
        _correr(crud.deshacer_borrado("notas", 10))


# ═══════════════════════════════════════════════════════════════════════
# 9. Lo que se pinta sale escapado
# ═══════════════════════════════════════════════════════════════════════

def test_el_texto_de_una_nota_sale_escapado_venga_del_panel_o_de_telegram(uno):
    peligro = '"><script>alert(1)</script><img src=x onerror=alert(2)>&amp; \'comilla\''
    _bandeja(uno, 1, DUENO, "telegram")
    _nota(uno, 10, 1, peligro, autor=DUENO)
    _nota(uno, 11, 1, peligro, bandeja=1)
    for consulta in ({"p": 1}, {"p": 1, "editar_nota": 10}, {"p": 1, "borrar_nota": 10}):
        html = pagina(uno, **consulta)
        assert "<script>alert(1)" not in html and "<img src=x" not in html, consulta
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&amp;amp;" in html, consulta
    assert "<script>alert(1)" not in pagina(uno, chat="ver", p=1)


def test_entradas_inventadas_con_semilla_nunca_rompen_el_html_ni_se_pierden(uno):
    azar = random.Random(8102026)
    alfabeto = list("ab <>\"'&;/\n=ñ")
    for i in range(40):
        t = "".join(azar.choice(alfabeto) for _ in range(azar.randint(1, 30))).strip() or "x"
        limpio = db.texto_de_nota_que_vale(t)
        _nota(uno, 100 + i, 1, limpio, autor=DUENO)
    html = ver(uno, p=1)
    assert html.count('class="nota-item"') == 40
    assert "<script" not in _bloque(html) and "onerror" not in _bloque(html)


# ═══════════════════════════════════════════════════════════════════════
# 10. La migración, el proceso viejo y los escritores
# ═══════════════════════════════════════════════════════════════════════

_MIGRACION = _ROOT / "db" / "migrations" / "2026-10-08_notas_autor.sql"


def _notas_vieja() -> sqlite3.Connection:
    """`notas` como era antes de la migración, con filas de Telegram dentro."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.create_function("now", 0, lambda: "2026-10-02T12:00:00+00:00")
    ddl = b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "notas")[0]
    ddl = re.sub(r",\s*autor_chat_id BIGINT", "", ddl)
    assert "autor_chat_id" not in ddl, ddl
    con.execute(ddl)
    for i in range(1, 4):
        con.execute("INSERT INTO notas (id, contenido, proyecto_id) VALUES (?,?,?)", (i, f"n{i}", i if i < 3 else None))
    return con


def test_la_migracion_se_ejecuta_deja_el_autor_en_null_y_es_idempotente():
    con = _notas_vieja()
    antes = [tuple(f) for f in con.execute("SELECT * FROM notas ORDER BY id")]
    texto = _MIGRACION.read_text(encoding="utf-8")
    saltadas = _aplicar_migracion_de_notas(con, texto)
    columnas = [c[1] for c in con.execute("PRAGMA table_info(notas)")]
    assert columnas[-1] == "autor_chat_id" and columnas.count("autor_chat_id") == 1
    assert con.execute("SELECT count(*) FROM notas").fetchone()[0] == len(antes) == 3
    assert con.execute("SELECT count(autor_chat_id) FROM notas").fetchone()[0] == 0
    assert [tuple(f)[:-1] for f in con.execute("SELECT * FROM notas ORDER BY id")] == antes
    assert sum(s.upper().startswith("COMMENT ON") for s in saltadas) == 1
    _aplicar_migracion_de_notas(con, texto)                     # la segunda vez no hace nada
    assert [c[1] for c in con.execute("PRAGMA table_info(notas)")] == columnas


def _aplicar_migracion_de_notas(con, texto):
    """La misma emulación declarada de `test_fechas_de_proyecto._aplicar_migracion`, para `notas`."""
    saltadas = []
    for s in g._sentencias(texto):
        s = " ".join(s.split())
        if s.upper() in ("BEGIN", "COMMIT") or s.upper().startswith("COMMENT ON"):
            saltadas.append(s)
        elif m := re.fullmatch(r"ALTER TABLE notas ADD COLUMN IF NOT EXISTS (\w+) (\w+)", s, re.I):
            if m.group(1) not in [c[1] for c in con.execute("PRAGMA table_info(notas)")]:
                con.execute(f"ALTER TABLE notas ADD COLUMN {m.group(1)} {m.group(2)}")
        else:
            raise AssertionError(f"sentencia de la migración que la prueba no sabe ejecutar: {s[:100]}")
    return saltadas


def test_la_migracion_es_aditiva_y_no_toca_nada_que_exista():
    """Lo que dice el archivo, leído por una prueba: una sola sentencia que cambia la tabla, sin DROP,
    DELETE, UPDATE, RENAME, NOT NULL ni DEFAULT; con su BEGIN/COMMIT."""
    sentencias = [" ".join(s.split()).upper() for s in g._sentencias(_MIGRACION.read_text(encoding="utf-8"))]
    assert sentencias[0] == "BEGIN" and sentencias[-1] == "COMMIT"
    cambia = [s for s in sentencias if s.startswith("ALTER")]
    assert cambia == ["ALTER TABLE NOTAS ADD COLUMN IF NOT EXISTS AUTOR_CHAT_ID BIGINT"]
    for prohibido in ("DROP", "DELETE", "UPDATE", "RENAME", "NOT NULL", "DEFAULT", "TRUNCATE"):
        assert not any(prohibido in s for s in sentencias), prohibido


def test_el_esquema_declara_la_misma_columna_que_la_migracion():
    declaradas = db.columnas_declaradas()["notas"]
    assert "autor_chat_id" in declaradas
    texto = _MIGRACION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS autor_chat_id BIGINT" in texto
    assert re.search(r"autor_chat_id\s+BIGINT\s*(--.*)?\n?\)", b2._ddl(b2._SCHEMA.read_text(encoding="utf-8"), "notas")[0], re.S)


def _insert_viejo_de_telegram() -> str:
    """El `INSERT INTO notas` que escribe Telegram (`crud.crear_desde_interpretacion`), sacado del código."""
    for rel, arbol in _tct._modulos(_ROOT):
        if rel != "acciones/crud.py":
            continue
        for fn in _tct._funciones(arbol):
            if fn.name != "crear_desde_interpretacion":
                continue
            textos = [t for t in _tct._sql_de_una_funcion(fn) if re.search(r"INSERT\s+INTO\s+notas\b", t, re.I)]
            assert len(textos) == 1, textos
            return textos[0]
    raise AssertionError("no está el INSERT de Telegram")


def test_el_insert_de_telegram_del_proceso_viejo_anda_con_la_tabla_antes_y_despues_del_cambio():
    sql = g._hacia_sqlite(_insert_viejo_de_telegram())
    assert "autor_chat_id" not in sql                       # el proceso viejo no conoce la columna
    for migrada in (False, True):
        con = _notas_vieja()
        if migrada:
            _aplicar_migracion_de_notas(con, _MIGRACION.read_text(encoding="utf-8"))
        con.execute(sql.replace(" RETURNING id", ""), (None, "nota de Telegram", "[]", 1, None))
        f = dict(con.execute("SELECT * FROM notas ORDER BY id DESC LIMIT 1").fetchone())
        assert f["contenido"] == "nota de Telegram" and f["proyecto_id"] == 1 and f.get("autor_chat_id") is None


def test_el_codigo_viejo_lee_notas_con_select_estrella_por_nombre_y_no_se_rompe_con_una_columna_de_mas(uno):
    """`crud.editar`/`borrar`/`deshacer` leen la fila con `SELECT *` por nombre: con la columna nueva siguen
    andando (Telegram edita y borra una nota del panel)."""
    _nota(uno, 10, 1, "del panel", autor=DUENO)
    _correr(crud.editar("notas", 10, {"contenido": "editada por Telegram"}, "prueba"))
    assert _nota_fila(uno, 10)["contenido"] == "editada por Telegram" and _nota_fila(uno, 10)["autor_chat_id"] == DUENO
    _correr(crud.borrar("notas", 10, "prueba"))
    assert _nota_fila(uno, 10)["borrado_en"] is not None


_SQL_ESCRIBE = re.compile(r"(INSERT\s+INTO|UPDATE)\s+(notas\b|\{…\})(.*)", re.I | re.S)


def test_sonda_quien_escribe_notas_con_el_sql_a_mano_y_quien_arma_la_tabla_al_vuelo():
    """Con los f-strings rearmados (donde interpolan, `…`): un `grep` no ve a quien arma la tabla al vuelo.
    A mano, con el nombre `notas`: Telegram y las tres funciones de la página. Al vuelo: los mismos seis
    escritores genéricos de siempre (ninguno es nuevo: `notas` no recibió ninguno)."""
    a_mano, al_vuelo = set(), set()
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            for texto in _tct._sql_de_una_funcion(fn):
                for m in _SQL_ESCRIBE.finditer(texto):
                    (a_mano if m.group(2).lower() == "notas" else al_vuelo).add((rel, fn.name))
    assert a_mano == {("acciones/crud.py", "crear_desde_interpretacion"),
                      ("db/db.py", "crear_nota_de_proyecto"), ("db/db.py", "editar_nota_de_proyecto"),
                      ("db/db.py", "borrar_nota_de_proyecto")}, sorted(a_mano)
    assert al_vuelo == {("acciones/crud.py", "borrar"), ("acciones/crud.py", "deshacer"),
                        ("acciones/crud.py", "editar"), ("cerebro/despertador.py", "revisar"),
                        ("db/db.py", "_buscar_o_crear"), ("tools/rellenar_duenos.py", "main")}, sorted(al_vuelo)


def test_sonda_quien_lee_notas_con_el_nombre_a_mano_y_quien_la_pide_por_argumento():
    """Los que leen `notas` por su nombre en el SQL, y los que la piden como argumento (`leer_notas_de_dueno`)."""
    lectores = set()
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            for texto in _tct._sql_de_una_funcion(fn):
                if re.search(r"(FROM|JOIN)\s+notas\b", texto, re.I):
                    lectores.add((rel, fn.name))
    assert lectores == {("db/db.py", "notas_de_proyectos"), ("db/db.py", "_nota_viva_de"),
                        ("db/db.py", "editar_nota_de_proyecto"),
                        ("db/db.py", "pagina_de_proyectos")}, sorted(lectores)   # (esta última, la consulta de «movimiento»)


def test_nadie_mas_que_la_sesion_escribe_el_autor():
    """`autor_chat_id` lo nombra el SQL a mano solo en `crear_nota_de_proyecto`; `editar` y `deshacer` no la
    pueden escribir (`es_editable` y `deshacer_la_devuelve` dicen que no, con valores fijos)."""
    quien = set()
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            for texto in _tct._sql_de_una_funcion(fn):
                if re.search(r"(INSERT\s+INTO|UPDATE)\s+notas\b[^;]*\bautor_chat_id\b", texto, re.I | re.S):
                    quien.add((rel, fn.name))
    assert quien == {("db/db.py", "crear_nota_de_proyecto")}
    assert crud.es_editable("notas", "autor_chat_id") is False
    assert crud.es_editable("notas", "contenido") is True
    antes = {"id": 1, "contenido": "a", "autor_chat_id": DUENO}
    despues = {"id": 1, "contenido": "b", "autor_chat_id": ROSI}
    assert crud.deshacer_la_devuelve("notas", "autor_chat_id", antes, despues) is False
    assert crud.deshacer_la_devuelve("notas", "contenido", antes, despues) is True


def test_telegram_editar_no_puede_cambiar_el_autor_de_una_nota(uno):
    _nota(uno, 10, 1, "del panel", autor=DUENO)
    with pytest.raises(ValueError, match="No hay nada que cambiar"):
        _correr(crud.editar("notas", 10, {"autor_chat_id": ROSI}, "prueba"))
    _correr(crud.editar("notas", 10, {"autor_chat_id": ROSI, "contenido": "nuevo"}, "prueba"))
    f = _nota_fila(uno, 10)
    assert f["autor_chat_id"] == DUENO and f["contenido"] == "nuevo"


# ═══════════════════════════════════════════════════════════════════════
# 11. Lo que ya hacía una nota fuera de un proyecto no cambia
# ═══════════════════════════════════════════════════════════════════════

def test_una_nota_del_panel_no_sale_en_la_lectura_de_lo_de_tiziano_para_code(uno):
    """`db/lectura_dueno.py` decide «de Tiziano» por la bandeja: una nota sin `bandeja_id` (todas las del
    panel) NUNCA sale; una de Telegram de su chat, sí. La puerta se ejecuta de verdad (su SQL, en SQLite)."""
    from db import lectura_dueno
    _bandeja(uno, 1, DUENO, "telegram")
    _bandeja(uno, 2, ROSI, "telegram")
    _nota(uno, 10, 1, "del panel del dueño", autor=DUENO)
    _nota(uno, 11, 1, "de Telegram del dueño", bandeja=1)
    _nota(uno, 12, 1, "de Telegram de Rosi", bandeja=2)
    cond = g._hacia_sqlite(lectura_dueno._condicion_de_dueno("notas"))
    ids = {f[0] for f in uno.con.execute(f"SELECT id FROM notas WHERE borrado_en IS NULL AND {cond}", (DUENO,))}
    assert ids == {11}


def test_lucy_por_telegram_ve_la_columna_nueva_y_sabe_que_la_escribe_el_panel():
    from cerebro import consultar
    assert ("notas", "autor_chat_id") in consultar.NOTAS_DE_COLUMNA
    assert "autor_chat_id" in consultar.BLOQUES["notas"]
    assert "notas" in consultar.TABLAS_DE_TIZIANO
    assert "NULL = no la escribió el panel" in consultar.NOTAS_DE_COLUMNA[("notas", "autor_chat_id")]


# ═══════════════════════════════════════════════════════════════════════
# La puerta única de quién toca una nota, con valores fijos
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("chat,fila,toca", [
    (DUENO, {"autor_chat_id": DUENO}, True),
    (ROSI, {"autor_chat_id": DUENO}, False),
    (DUENO, {"autor_chat_id": ROSI}, False),
    (DUENO, {"autor_chat_id": None, "bandeja_origen": "telegram", "bandeja_chat_id": DUENO}, True),
    (ROSI, {"autor_chat_id": None, "bandeja_origen": "telegram", "bandeja_chat_id": DUENO}, False),
    (DUENO, {"autor_chat_id": None, "bandeja_origen": "banco", "bandeja_chat_id": DUENO}, False),
    (DUENO, {"autor_chat_id": None, "bandeja_origen": "correo", "bandeja_chat_id": DUENO}, False),
    (DUENO, {"autor_chat_id": None, "bandeja_origen": None, "bandeja_chat_id": None}, False),
    (DUENO, {}, False),
    (None, {"autor_chat_id": None, "bandeja_origen": "telegram", "bandeja_chat_id": None}, False),
    (None, {"autor_chat_id": DUENO}, False),
    (AJENO, {"autor_chat_id": AJENO}, False),            # tiene nombre pero no entra al panel
    (SIN_NOMBRE, {"autor_chat_id": SIN_NOMBRE}, False),
])
def test_quien_puede_tocar_una_nota(gente, chat, fila, toca):
    assert db.puede_tocar_nota(chat, fila) is toca
