# -*- coding: utf-8 -*-
"""La columna RESPONSABLE del panel de tareas: quién tiene pendiente cada cosa.

Pedido de Tiziano el 10-sep-2026: «la columna "quien la anoto" vamos a
cambiarla a Responsable y lo que indica es quien tiene esa tarea pendiente», y
«nno es relevante quien la anoto». La vieja se va; no se conservan las dos.

QUÉ SE PRUEBA CON MÁS SAÑA, y por qué son ésas:

  · QUE LA LISTA DE PERSONAS NO ESTÉ TECLEADA EN NINGUNA PARTE. Los nombres
    viven en la variable de entorno `NOMBRES_POR_CHAT` justamente para que
    Tiziano pueda cambiar uno, o dar de alta a una tercera persona, sin que
    nadie despliegue nada. Si esa lista se copiara al código —o a una prueba—,
    la variable dejaría de servir para lo único que existe. Hay una prueba que
    mete una tercera persona y exige que aparezca sola.

  · QUE NO SE PUEDA ASIGNAR A CUALQUIERA, POR NINGÚN CAMINO. Una tarea que
    queda con alguien que no puede abrir el panel es un pendiente que esa
    persona no va a ver nunca. La puerta es `config.puede_ser_responsable`.
    Quién escribe la columna sale de un censo de cada `execute` del
    repositorio, no de buscar el nombre de la columna en el texto: `editar` y
    `deshacer` la escriben sin nombrarla nunca. A los que la nombran se les
    exige la puerta; a los que arman la columna al vuelo, una sonda que CORRE y
    demuestra que un chat que no vale no se escribe. Hasta dónde llega: LA
    FRONTERA, más abajo, y sólo ahí.

  · QUE SIN RESPONSABLE SEA NORMAL Y NO UN ERROR. Las 57 tareas vivas de
    producción nacieron sin responsable; tratarlo como un caso a manejar habría
    hecho ruido en todas.

  · QUE NO SE PIERDA UN RESPONSABLE YA GUARDADO. Si a alguien le quitan el
    nombre o el acceso, ninguna opción del desplegable lo representa y el
    siguiente envío lo borraría en silencio. Se le pinta su propia opción.

LO QUE ESTAS PRUEBAS NO PUEDEN VER, medido y no supuesto: `psycopg` está
reemplazado por un módulo falso, así que nada de acá habla con Postgres. La
conexión devuelve las filas que se le preparan, así que ninguna prueba de
comportamiento de este archivo puede ver si la COLUMNA existe de verdad en la
base. Eso lo ve `tools/humo.py`, que necesita DATABASE_URL, y hasta que alguien
aplique `db/migrations/2026-09-10_responsable_de_la_tarea.sql` la columna no
existe. Lo que sí se comprueba sin base delante es que la migración y el código
nombren la MISMA columna, que es el typo que se paga en producción.

LOS NÚMEROS DE CHAT DE ESTE ARCHIVO SON INVENTADOS. `CHAT_ID_DUENO` se fija
arriba con un valor de prueba y los demás salen de sumarle. Ninguno es un chat
real de nadie, y ninguna prueba lee la variable de entorno de verdad.

Correr:  python3 -m pytest tests/test_responsable.py
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import json
import os
import re
import sys
import textwrap
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import config  # noqa: E402
import db.db as db  # noqa: E402
import acciones.crud as crud  # noqa: E402
import web.app as panel  # noqa: E402
import web.auth as auth  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = timezone.utc

# Chats INVENTADOS, y ninguno es el de nadie.
#
# `DUENO` sale de `CHAT_ID_DUENO` porque la cookie de sesión se firma para él y
# tiene que poder entrar. NO se le puede poner un valor propio: el `setdefault`
# de arriba solo gana si este archivo es el primero de la suite en importar
# `config`, y cuál corre primero depende del orden de recolección de pytest.
#
# Los otros tres son constantes GRANDES y sueltas, no `DUENO + n`, y eso es una
# cicatriz: atados al dueño valían 1, 2 y 999 cuando otro archivo de la suite
# ganaba el `setdefault` con `CHAT_ID_DUENO=1`. Entonces «el número no se ve en
# la pantalla» daba rojo porque "1" aparece en «tarea 1» y en `prev_1`, o sea
# que la prueba medía el orden de la suite en vez de medir el panel. Sueltos y
# de nueve cifras —el largo de un chat de Telegram de verdad— buscarlos en el
# HTML significa lo que dice que significa.
DUENO = config.CHAT_ID_DUENO
OTRA = 700000001          # la segunda persona de la casa
TERCERA = 700000002       # la que Tiziano podría dar de alta mañana
AJENO = 700000999         # uno que NO entra al panel


# ── Una base de mentira ──────────────────────────────────────────────────
#
# Anota todo el SQL que le llega y devuelve las filas que se le preparan. Es la
# misma forma que usan las otras suites del panel, y tiene el mismo límite
# dicho: no ve nada de lo que el SQL hace con las filas.

class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._filas: list = []

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sql.append((s, params))
        if s.startswith("SELECT t.id, t.titulo"):
            self._filas = list(self._conn.tareas)
        elif s.startswith("SELECT id, titulo, estado"):
            self._filas = [f for f in self._conn.tareas if f["id"] == params[0]]
        else:
            self._filas = []
        return self

    async def fetchall(self):
        return self._filas

    async def fetchone(self):
        return self._filas[0] if self._filas else None


class _Conn:
    def __init__(self, filas):
        self.tareas = filas
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def execute(self, sql, params=None):
        return await _Cursor(self).execute(sql, params)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        class _CM:
            async def __aenter__(s):
                return self._conn

            async def __aexit__(s, *e):
                return False
        return _CM()


def _fila(id, estado="pendiente", vence_en=None, titulo=None,
          responsable=None, bandeja_id=None):
    return {"id": id, "titulo": titulo or f"tarea {id}", "estado": estado,
            "vence_en": vence_en, "creado_en": datetime(2026, 8, 1, tzinfo=UTC),
            "bandeja_id": bandeja_id if bandeja_id is not None else 900 + id,
            "responsable_chat_id": responsable, "completado_en": None}


def _con_base(filas, fn):
    conn = _Conn(filas)
    guardado = db.pool
    db.pool = _Pool(conn)
    try:
        bucle = asyncio.new_event_loop()
        try:
            return bucle.run_until_complete(fn()), conn
        finally:
            bucle.close()
    finally:
        db.pool = guardado


def _con_gente(nombres, permitidos=None):
    """Instala una casa de mentira: quién entra y cómo se llama cada uno.

    Se escriben los dos atributos de `config` que las funciones leen EN CADA
    LLAMADA. El `conftest.py` de este repo devuelve los módulos a su sitio
    después de cada prueba, así que esto no se le escapa a la siguiente.

    Se toca `config` y no ningún archivo del repositorio: acá no se muta código
    real, se le cambia el entorno.
    """
    config.NOMBRES_POR_CHAT = dict(nombres)
    config.CHAT_IDS_PERMITIDOS = tuple(
        permitidos if permitidos is not None else nombres)


# Nombres INVENTADOS, y a propósito distintos de los de las personas reales:
# el repositorio está lleno de prosa que los nombra —son de quien es este
# proyecto— y una prueba que buscara «Tiziano» en un archivo mediría los
# comentarios en vez de medir la lista de personas.
LA_CASA = {DUENO: "Zutana", OTRA: "Mengano"}


# ── Cómo se lee la variable ──────────────────────────────────────────────

def test_la_variable_se_lee_en_pares_y_el_orden_no_significa_nada():
    """`chat:nombre`, separados por coma o por punto y coma —la misma tolerancia
    que CHAT_IDS_CASA—, y con los espacios sobrantes recortados.

    ES UNA LISTA DE PARES Y NO DOS LISTAS PARALELAS a propósito: con dos
    listas, reordenar una le pone a una persona el nombre de la otra y no hay
    forma de notarlo. Acá cada nombre viaja pegado a su chat, así que el mismo
    contenido en otro orden da exactamente lo mismo.
    """
    nombres, malos = config._leer_nombres("11:Tiziano, 22:Rosi")
    assert nombres == {11: "Tiziano", 22: "Rosi"}
    assert malos == 0

    al_reves, _ = config._leer_nombres("22:Rosi;11:Tiziano")
    assert al_reves == nombres, "el orden de la variable cambió quién es quién"

    con_espacios, _ = config._leer_nombres("  11 : Tiziano  ,  22:Rosi  ")
    assert con_espacios == nombres

    # Un nombre puede llevar dos puntos: se parte por el PRIMERO.
    raro, _ = config._leer_nombres("11:Tiziano: el jefe")
    assert raro == {11: "Tiziano: el jefe"}


def test_lo_que_no_se_entiende_se_descarta_y_SE_CUENTA():
    """Descartar callado es cómo una persona desaparece de la lista sin que
    nadie sepa por qué. Cada entrada que no se entiende se cuenta, y el panel
    dice cuántas hay.

    Las cuatro formas de escribirla mal —sin `:`, sin chat, sin nombre, con un
    chat que no es número— caen todas del mismo lado. No se adivina ninguna:
    inventarle el nombre a un chat es exactamente lo que Tiziano descartó.
    """
    nombres, malos = config._leer_nombres(
        "11:Tiziano, Rosi, 22:, :nadie, abc:Ana, 33:Ana")
    assert nombres == {11: "Tiziano", 33: "Ana"}
    assert malos == 4, f"no se contaron las mal escritas: {malos}"

    # Y la variable vacía no es un error: es "nadie tiene nombre todavía".
    assert config._leer_nombres("") == ({}, 0)
    assert config._leer_nombres("   ,  ; ") == ({}, 0)


def test_el_ultimo_gana_cuando_el_mismo_chat_aparece_dos_veces():
    """Es lo que hace cualquiera al corregir una línea sin borrar la anterior.
    Lo que importa no es cuál gane, sino que sea SIEMPRE el mismo: una regla
    que depende del orden de un diccionario da una casa distinta en cada
    arranque."""
    nombres, malos = config._leer_nombres("11:Rosi, 11:Tiziano")
    assert nombres == {11: "Tiziano"}
    assert malos == 0


# ── Quién puede ser responsable ──────────────────────────────────────────

def test_solo_los_que_entran_al_panel_y_tienen_nombre():
    """LAS DOS CONDICIONES, y las dos salen de lo real.

    Entrar al panel, porque una tarea que queda con alguien que no puede abrir
    la pantalla es un pendiente que no va a ver nunca. Y tener nombre, porque
    sin él no hay nada honesto que pintar: inventarlo sería mentir y enseñar el
    número de chat lo descartó Tiziano.
    """
    _con_gente({DUENO: "Zutana", OTRA: "Mengano", AJENO: "Un Ajeno"},
               permitidos=(DUENO, OTRA))
    assert config.personas_del_panel() == ((DUENO, "Zutana"), (OTRA, "Mengano"))
    assert config.puede_ser_responsable(DUENO)
    assert config.puede_ser_responsable(OTRA)
    # Tiene nombre y NO entra: no puede quedarse con una tarea.
    assert not config.puede_ser_responsable(AJENO)
    assert not config.puede_ser_responsable(None)

    # Entra y NO tiene nombre: tampoco, y el panel lo dice (ver más abajo).
    _con_gente({DUENO: "Zutana"}, permitidos=(DUENO, OTRA))
    assert config.personas_del_panel() == ((DUENO, "Zutana"),)
    assert not config.puede_ser_responsable(OTRA)
    assert config.chats_sin_nombre() == 1


def test_quien_puede_ser_responsable_no_se_puede_separar_de_quien_entra():
    """Las dos preguntas se responden con la MISMA tupla, no con dos listas que
    dicen lo mismo hoy. Si mañana alguien deja de poder entrar al panel, deja
    de poder ser responsable el mismo día y sin que nadie se acuerde.

    Se comprueba sobre la casa entera, sin nombrar a nadie: todo el que puede
    ser responsable tiene que poder entrar.
    """
    _con_gente({DUENO: "Zutana", OTRA: "Mengano", TERCERA: "Perengana"})
    for chat, _ in config.personas_del_panel():
        assert auth.puede_entrar(chat), (
            "hay alguien que puede quedarse con una tarea y no puede abrir el "
            "panel para verla")


def test_una_tercera_persona_aparece_sola_sin_tocar_codigo():
    """LA PRUEBA QUE JUSTIFICA QUE LOS NOMBRES VIVAN EN UNA VARIABLE.

    Tiziano tiene que poder dar de alta a alguien desde Railway, sin que nadie
    despliegue. Acá se agrega una tercera persona SOLO cambiando el entorno —ni
    un archivo del repositorio se toca— y tiene que aparecer en el desplegable
    del panel y poder quedarse con una tarea.

    Si esto se pusiera rojo, sería porque la lista de personas quedó tecleada
    en algún sitio del código.
    """
    _con_gente(LA_CASA)
    assert not config.puede_ser_responsable(TERCERA)
    antes = _pintar([_fila(1)])
    assert "Perengana" not in antes

    _con_gente({**LA_CASA, TERCERA: "Perengana"})
    assert config.puede_ser_responsable(TERCERA), (
        "la tercera persona no entró: hay una lista de personas escrita a mano")
    despues = _pintar([_fila(1)])
    assert ">Perengana</option>" in despues, (
        "la tercera persona no aparece en el desplegable del panel")
    assert config.chats_sin_nombre() == 0


def test_sin_la_variable_no_hay_NADIE_a_quien_asignar():
    """LA CONTRACARA DE LA DE ARRIBA, y la que agarra el atajo.

    Alguien con prisa escribe la lista de personas en el código «por ahora» y
    todo se ve bien: el panel enseña los dos nombres. La variable deja de
    servir para lo único que existe, y nadie se entera hasta que Tiziano cambia
    un nombre en Railway y el panel sigue diciendo el viejo.

    Acá se vacía la variable entera dejando a las dos personas con acceso. Si
    quedara una sola lista escrita en cualquier parte del camino —config, la
    ruta o la plantilla—, alguien seguiría apareciendo en el desplegable. Tiene
    que no quedar nadie, y el panel tiene que DECIRLO en vez de callarse: sin
    nombres no se puede asignar, y un desplegable vacío sin explicación deja a
    Tiziano buscando qué se rompió.

    Y no se inventa ni se miente: no hay ningún nombre que enseñar y el número
    de chat Tiziano lo descartó, así que lo único honesto es no ofrecer a
    nadie y decir por qué.
    """
    _con_gente({}, permitidos=(DUENO, OTRA))
    assert config.personas_del_panel() == (), (
        "hay personas donde la variable no dice ninguna: la lista está "
        "escrita a mano en algún sitio")
    assert not config.puede_ser_responsable(DUENO)
    assert not config.puede_ser_responsable(OTRA)
    assert config.chats_sin_nombre() == 2

    html = _pintar([_fila(1, titulo="afinar el piano")])
    assert "afinar el piano" in html, "la tarea desapareció por falta de nombres"
    # La ÚNICA opción del desplegable es «sin responsable»: se cuentan las que
    # hay, no se busca un nombre que habría que teclear acá para buscarlo.
    fila = html[html.index('name="resp_1"'):]
    fila = fila[:fila.index("</select>")]
    assert fila.count("<option") == 1, (
        f"el desplegable ofrece {fila.count('<option')} personas y la variable "
        "no declara ninguna")
    assert "sin responsable" in fila
    assert "NOMBRES_POR_CHAT" in html, (
        "el panel se calla que no puede asignar nada, y no hay forma de saber "
        "por qué el desplegable está vacío")


# ── La escritura: la puerta ──────────────────────────────────────────────

def _asignar(tid, chat, filas):
    return _con_base(filas, lambda: db.asignar_responsable(tid, chat))


def test_no_se_le_puede_asignar_a_quien_no_entra_al_panel():
    """Ni se abre la conexión: un chat que no puede entrar al panel no puede
    quedarse con una tarea, y no hay nada que escribir ni que registrar."""
    _con_gente(LA_CASA)
    ok, conn = _asignar(1, AJENO, [_fila(1)])
    assert ok is False, "le asignó la tarea a alguien que no entra al panel"
    assert conn.sql == [], "tocó la base para una asignación que no vale"

    # Y uno que tiene nombre pero perdió el acceso, tampoco.
    _con_gente({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    ok, conn = _asignar(1, OTRA, [_fila(1)])
    assert ok is False
    assert conn.sql == []


def test_toda_escritura_de_la_columna_pasa_por_la_misma_puerta():
    """LOS QUE NOMBRAN LA COLUMNA EN SU SQL tienen que nombrar la puerta.

    Es el cubo 1 del censo (ver LA FRONTERA). Los que la escriben sin nombrarla
    —el cubo 3— los juzga `test_todo_escritor_generico_tiene_sonda_y_la_sonda_
    corre`, corriendo.

    LO QUE ESTA PRUEBA MIRABA ANTES, Y POR QUÉ NO SERVÍA, medido el 10-sep-2026
    sobre 9a12b0f: recorría SOLO `db/db.py` y buscaba el nombre de la columna en
    todos los textos de cada función, docstring incluido. `crud.editar` le
    escribía la columna a un chat que la puerta rechazaba y esto seguía verde:
    estaba en otro archivo, y el nombre de la columna no aparece en ningún
    texto de ese archivo. Ahora los archivos salen del disco y lo que se lee es
    el SQL de cada `execute`, no la prosa.
    """
    censo = _censo()
    columna = _columna_del_codigo()
    assert censo["sitios"], (
        "el censo no encontró ni un `execute` en el repositorio: esta guarda "
        "estaría verde sin haber mirado nada")
    escriben = censo["legibles_que_escriben"]
    assert escriben, (
        f"no se encontró ninguna función que escriba {columna} con un SQL que "
        "la nombre: esta guarda estaría verde sin haber mirado nada")
    puertas = {_PUERTA, _PUERTA_DE_CRUD}
    sin_puerta = sorted(
        quien for quien, funcion in escriben.items()
        if funcion is None or not (puertas & _nombres_de_codigo(funcion)))
    assert not sin_puerta, (
        f"estas funciones escriben {columna} sin pasar por la puerta: "
        f"{sin_puerta}. Cualquiera de ellas puede dejarle una tarea a alguien "
        "que no puede abrir el panel para verla")


def test_la_ruta_tambien_pregunta_por_la_puerta():
    """La validación del formulario y la de la escritura son la MISMA función,
    no dos criterios que hoy dicen lo mismo. Si la ruta dejara de preguntarle a
    la puerta, el rechazo dependería solo de la capa de abajo.

    LO QUE ESTA PRUEBA MIRABA ANTES, Y POR QUÉ NO SERVÍA, medido el 10-sep-2026
    en memoria: buscaba el nombre `puede_ser_responsable` en el TEXTO de
    web/app.py. Quitándole a la ruta la llamada, el nombre seguía ahí —en el
    docstring de `_responsable_pedido`, que lo menciona al explicarse— y la
    prueba seguía verde con CERO referencias de código a la puerta. Medía la
    prosa, que es exactamente el defecto que este trabajo le quitó a las
    guardas de `tests/test_panel_tareas.py`.

    Ahora se mira el árbol de sintaxis. Las funciones que se revisan NO están
    tecleadas: son las que llaman a `asignar_responsable`. Cada una tiene que
    llegar a la puerta, ella misma o a través de una función del mismo módulo a
    la que llame — un nivel, que es la forma que tiene hoy (`guardar_tareas` →
    `_responsable_pedido` → la puerta). La que alguien escriba mañana entra
    sola en el recorrido.

    Y LOS ARCHIVOS TAMPOCO ESTÁN TECLEADOS, desde el 10-sep-2026. Hasta ese día
    esto leía sólo `web/app.py`: una pantalla nueva en otro archivo que llamara
    a la escritura sin preguntarle a la puerta entraba sin que nadie la mirara.
    Ahora son todos los .py que hay en disco fuera de `testpaths`, por la misma
    puerta de recorrido que usa el censo.
    """
    import test_buzon_que_no_se_ve as barrido

    puerta = config.puede_ser_responsable.__name__
    escritura = db.asignar_responsable.__name__
    raiz = Path(RAIZ)
    pruebas = barrido._testpaths(raiz)

    escriben, sin_puerta = [], []
    for py in barrido._py_en_disco(raiz):
        if any(c == py or c in py.parents for c in pruebas):
            continue
        arbol = ast.parse(py.read_text(encoding="utf-8"), str(py))
        todas = [n for n in ast.walk(arbol)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # Por nombre, pero sin perder a ninguna: dos funciones que se llamen
        # igual (dos métodos, una anidada) se miran las dos.
        por_nombre: dict = {}
        for f in todas:
            por_nombre.setdefault(f.name, []).append(f)
        for f in todas:
            propios = _nombres_de_codigo(f)
            if escritura not in propios:
                continue
            donde = f"{py.relative_to(raiz).as_posix()}::{f.name}"
            escriben.append(donde)
            alcanza = puerta in propios or any(
                puerta in _nombres_de_codigo(otra)
                for nombre in propios if nombre != f.name
                for otra in por_nombre.get(nombre, ()))
            if not alcanza:
                sin_puerta.append(donde)

    assert escriben, (
        f"ninguna función del repositorio llama a {escritura}: esta guarda "
        "estaría verde sin haber mirado nada")
    assert not sin_puerta, (
        f"estas rutas escriben el responsable sin preguntarle a {puerta}: "
        f"{sin_puerta}. El formulario aceptaría a cualquiera y solo la capa de "
        "abajo lo pararía")


# ── La escritura: lo que deja escrito ────────────────────────────────────

def test_asignar_escribe_la_columna_y_deja_huella():
    """Toda escritura del panel es auditable y reversible: sin la fila de
    `log_acciones` no hay deshacer, porque el deshacer de este proyecto ES
    `log_acciones`.

    Y la huella guarda la fila ENTERA en `antes`, no solo la columna que
    cambia: la rama de 'editar' de `acciones.crud.deshacer` arma la vuelta
    atrás con las columnas que encuentre ahí.
    """
    _con_gente(LA_CASA)
    ok, conn = _asignar(1, OTRA, [_fila(1, bandeja_id=77)])
    assert ok is True
    escrito = " | ".join(s for s, _ in conn.sql)
    assert "UPDATE tareas SET responsable_chat_id" in escrito
    assert "INSERT INTO log_acciones" in escrito, "escribió sin dejar huella"
    assert "'panel'" in escrito, "la huella tiene que decir que fue el panel"
    assert "'editar'" in escrito, (
        "'editar' es la única acción que `deshacer` sabe revertir con `antes`")

    huella = [p for s, p in conn.sql if "log_acciones" in s][0]
    assert huella[0] == 1, "la huella no apunta a la tarea que cambió"
    assert "responsable_chat_id" in huella[1], (
        "el `antes` no guarda la columna que cambió: no habría con qué deshacer")
    assert huella[3] == 77, "la huella perdió el bandeja_id de la tarea"


def test_lo_que_ya_decia_eso_no_se_vuelve_a_escribir():
    """La pantalla que lleva diez minutos abierta no puede dejar una huella de
    una edición que no pasó. Es la misma decisión que `marcar_tarea_hecha`, y
    vale en las dos direcciones: reasignarle la misma persona, y volver a
    dejar sin responsable una que ya estaba sin nadie."""
    _con_gente(LA_CASA)
    for chat, filas in ((OTRA, [_fila(1, responsable=OTRA)]),
                        (None, [_fila(1)])):
        ok, conn = _asignar(1, chat, filas)
        assert ok is False, f"reescribió una tarea que ya decía eso ({chat})"
        assert not any("UPDATE" in s for s, _ in conn.sql)
        assert not any("log_acciones" in s for s, _ in conn.sql), (
            "dejó una huella de una edición que no pasó")


def test_la_tarea_que_no_esta_no_se_escribe():
    """No existe o está en la papelera: en los dos casos no se escribe nada. El
    SELECT filtra por `borrado_en IS NULL`, así que una tarea archivada no
    vuelve a la vida por asignarle un responsable."""
    _con_gente(LA_CASA)
    ok, conn = _asignar(999, OTRA, [_fila(1)])
    assert ok is False
    assert not any("UPDATE" in s for s, _ in conn.sql)
    sql = " ".join(s for s, _ in conn.sql)
    assert "borrado_en IS NULL" in sql, (
        "sin ese filtro se le puede asignar un responsable a una tarea que "
        "está en la papelera")


def test_dejar_sin_responsable_es_una_operacion_de_primera():
    """SIN RESPONSABLE ES LO NORMAL, no un error ni un hueco que haya que
    rellenar: las 57 tareas vivas de producción nacieron así. Quitarlo se
    escribe igual que ponerlo, deja su huella igual, y no pasa por la puerta —
    no hay a quién validar."""
    _con_gente(LA_CASA)
    ok, conn = _asignar(1, None, [_fila(1, responsable=OTRA)])
    assert ok is True, "no se puede devolver una tarea a «sin responsable»"
    escrito = " | ".join(s for s, _ in conn.sql)
    assert "UPDATE tareas SET responsable_chat_id" in escrito
    assert "INSERT INTO log_acciones" in escrito, (
        "quitar el responsable también es una edición y también se registra")
    valores = [p for s, p in conn.sql if s.startswith("UPDATE")][0]
    assert valores[0] is None, "no guardó NULL, guardó otra cosa"


def test_la_escritura_solo_toca_columnas_declaradas():
    """La columna que el código escribe sale del código y se busca en el
    esquema. Ninguna de las dos listas se teclea acá."""
    fuente = inspect.getsource(db.asignar_responsable)
    escritas = set(re.findall(r"SET ([a-z_]+) =", fuente))
    assert escritas, "no se encontró ninguna columna escrita"
    faltan = escritas - set(db.columnas_declaradas()["tareas"])
    assert not faltan, f"escribe columnas que el esquema no declara: {faltan}"


# ── La migración y el código no se pueden separar ────────────────────────

def _columna_del_codigo() -> str:
    """La columna que `db.asignar_responsable` escribe, leída de su SQL.

    Sale del árbol de sintaxis y no de `inspect.getsource` para que la prosa
    del docstring —que nombra la columna varias veces al explicarla— no cuente
    como si fuera SQL.
    """
    arbol = ast.parse(textwrap.dedent(inspect.getsource(db.asignar_responsable)))
    cuerpo = arbol.body[0].body[1:]          # sin el docstring
    sql = " ".join(
        n.value for nodo in cuerpo for n in ast.walk(nodo)
        if isinstance(n, ast.Constant) and isinstance(n.value, str))
    escritas = set(re.findall(r"SET ([a-z_]+) =", " ".join(sql.split())))
    assert len(escritas) == 1, f"se esperaba una sola columna escrita: {escritas}"
    return escritas.pop()


def test_la_migracion_declara_LA_MISMA_columna_que_escribe_el_codigo():
    """ÉSTA ES LA QUE IMPIDE QUE EL CÓDIGO Y LA BASE SE SEPAREN, y es lo único
    que se puede comprobar sin DATABASE_URL.

    La suite es hermética: su conexión es de mentira y acepta cualquier nombre
    de columna, así que un typo en la migración —o en el UPDATE— sale VERDE acá
    y revienta la primera vez que alguien toque la pantalla en producción.

    Los dos lados salen de sus archivos: la columna, del SQL que ejecuta el
    código; los nombres declarados, de recorrer `db/migrations/` entero. NO se
    nombra el archivo de la migración: si mañana se renombra, o si la columna
    se declara en otra, esto sigue valiendo.
    """
    columna = _columna_del_codigo()

    migraciones = os.path.join(RAIZ, "db", "migrations")
    declaradas = set()
    for nombre in sorted(os.listdir(migraciones)):
        if not nombre.endswith(".sql"):
            continue
        texto = open(os.path.join(migraciones, nombre), encoding="utf-8").read()
        declaradas |= {m.group(1).lower() for m in re.finditer(
            r"ALTER TABLE\s+tareas\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+(\w+)",
            db._sin_comentarios(texto), re.I)}

    assert columna in declaradas, (
        f"el código escribe `tareas.{columna}` y ninguna migración declara esa "
        f"columna; las migraciones declaran {sorted(declaradas)}. La suite es "
        "hermética y no lo puede ver de ninguna otra forma: en producción, la "
        "primera asignación revienta")


def test_el_esquema_del_repo_tambien_la_declara():
    """Una base creada de cero corre `db/schema.sql`, no las migraciones — es
    el paso 4 del README. Una columna que viva SOLO en la migración deja rota
    cualquier instalación nueva, que es exactamente cómo se perdió
    `idx_movimientos_hash` el 5-sep-2026.

    Se lee el bloque `CREATE TABLE tareas` de schema.sql con la misma expresión
    que usa `db.columnas_declaradas`, y no con `columnas_declaradas()` a secas
    —aquella junta el archivo CON las migraciones, así que no sabría distinguir
    una cosa de la otra.
    """
    columna = _columna_del_codigo()
    crudo = open(os.path.join(RAIZ, "db", "schema.sql"), encoding="utf-8").read()
    bloques = {m.group(1).lower(): m.group(2) for m in db._RE_TABLA.finditer(crudo)}
    assert "tareas" in bloques, "db/schema.sql ya no declara la tabla tareas"
    assert re.search(rf"^\s*{columna}\b", bloques["tareas"], re.M), (
        f"db/schema.sql no declara `tareas.{columna}`: una base creada de cero "
        "con el paso 4 del README nace sin ella y el panel de tareas no puede "
        "asignar nada")


def test_la_columna_nueva_no_es_persona_id():
    """`tareas.persona_id` ya existe y significa DE QUIÉN TRATA la tarea
    —"preguntarle a Pedro por el presupuesto"—, no quién la tiene que hacer.
    Medido contra producción el 10-sep-2026: de las 57 tareas vivas, 30 la
    tienen puesta. Reciclarla habría pisado ese dato en más de la mitad.

    Las dos columnas tienen que seguir existiendo y siendo distintas."""
    columna = _columna_del_codigo()
    declaradas = db.columnas_declaradas()["tareas"]
    assert columna != "persona_id", (
        "el responsable se guardó en persona_id y se pisó de quién trata la "
        "tarea en 30 de las 57 filas vivas")
    assert "persona_id" in declaradas, "persona_id desapareció del esquema"
    assert columna in declaradas


# ═════════════════════════════════════════════════════════════════════════
# QUIÉN ESCRIBE LA COLUMNA: el censo, y las sondas que CORREN
# ═════════════════════════════════════════════════════════════════════════
#
# POR QUÉ EXISTE, medido el 10-sep-2026 sobre 9a12b0f: la guarda de antes
# buscaba el nombre de la columna en los textos de `db/db.py`, y `crud.editar`
# —el que usan el agente de Telegram y los botones— escribía la columna con un
# chat que la puerta rechazaba:
#
#     crud.editar("tareas", 1, {"responsable_chat_id": <un chat que no vale>})
#     -> UPDATE tareas SET responsable_chat_id = %s WHERE id = %s
#
# El nombre de la columna no está en ningún texto de `acciones/crud.py`: sale
# de los datos y se arma al vuelo. Buscarlo en el texto del repositorio entero
# tampoco lo habría encontrado. Lo mismo `crud.deshacer`, con el `antes` de una
# huella.
#
# ── LA FRONTERA: lo que ve ───────────────────────────────────────────────
#
#   Toda llamada `<algo>.execute(...)` o `<algo>.executemany(...)` escrita en
#   un .py del repositorio fuera de `testpaths` —los archivos los da
#   `test_buzon_que_no_se_ve._py_en_disco`, la única puerta para recorrer el
#   repositorio—, repartida por su SQL en tres cubos:
#
#     1. SQL LEGIBLE. Un literal, o armado al vuelo SOLO en el nombre de la
#        tabla: el hueco va justo detrás de UPDATE, INTO, FROM, JOIN o `null::`.
#        Sus columnas se leen, y si escribe la columna la función tiene que
#        nombrar la puerta.
#     2. SOLO LECTURA. La misma función ejecuta el literal
#        `SET TRANSACTION READ ONLY`, y el candado lo pone Postgres.
#     3. ESCRITOR GENÉRICO: todo lo demás. Un hueco en cualquier otro sitio, o un
#        SQL que no se puede reconstruir desde el código. Tiene que tener sonda
#        en `SONDAS`, y la sonda corre.
#
#   Lo que no se sabe clasificar cae en el 3, que es el estricto. Al 1 y al 2
#   no se llega por olvido: hay que cumplir su regla.
#
#   Cómo se reconstruye el SQL: un hueco que sólo puede valer textos escritos en
#   el código (`{"a" if x else "b"}`) cuenta como esos textos; una variable
#   cuenta como lo que se le asigna en la misma función —o en el módulo, si en
#   la función no se le asigna nada— siempre que todo eso sea texto. Un
#   parámetro no se puede reconstruir.
#
# ── LA FRONTERA: lo que NO ve ────────────────────────────────────────────
#
#   1. SQL que llega a Postgres sin una llamada escrita `.execute` o
#      `.executemany`: `getattr(con, "execute")`, `copy`, un subproceso con
#      `psql`, los `.sql` de `db/migrations`, la consola de Railway.
#   2. Un hueco detrás de UPDATE, INTO, FROM, JOIN o `null::` se toma por un
#      nombre de tabla. Si alguien mete ahí «tareas SET responsable_chat_id»,
#      pasa.
#   3. Una sonda ve el SQL que saldría hacia la base con una conexión de
#      mentira. No ve lo que Postgres hace con él.
#   4. En el cubo 1 basta con que la función NOMBRE la puerta. Que respete la
#      respuesta sólo lo prueban, corriendo, las pruebas de
#      `db.asignar_responsable` de este archivo.
#   5. Del cubo 2 se ve que la línea está, no que Postgres la cumpla.
#   6. El código bajo `testpaths`.

_PUERTA = config.puede_ser_responsable.__name__
_PUERTA_DE_CRUD = crud._por_las_puertas.__name__
# Se compara contra el texto de antes del hueco YA SIN ESPACIOS AL FINAL, así
# que detrás de la palabra no se exige ninguno: sólo, si acaso, una comilla.
_HUECO_DE_TABLA = re.compile(r'(?:\b(?:UPDATE|INTO|FROM|JOIN)\s*"?|null::)$',
                             re.I)
_SOLO_LECTURA = "SET TRANSACTION READ ONLY"
_EJECUTAN = ("execute", "executemany")


def _nombres_de_codigo(nodo) -> set:
    """Los nombres y atributos que un nodo USA. Sólo código: un docstring es un
    ast.Constant y no entra, así que la prosa no puede hacer pasar nada."""
    return ({n.id for n in ast.walk(nodo) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(nodo) if isinstance(n, ast.Attribute)})


def _textos_posibles(nodo):
    """Los textos que puede valer una expresión, si SÓLO puede valer textos
    escritos en el código. None si puede valer otra cosa."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return [nodo.value]
    if isinstance(nodo, ast.IfExp):
        a, b = _textos_posibles(nodo.body), _textos_posibles(nodo.orelse)
        return None if a is None or b is None else a + b
    return None


def _asignaciones(ambito) -> dict:
    """Lo que se le asigna a cada nombre dentro de un ámbito."""
    nodos = ambito.body if isinstance(ambito, ast.Module) else ast.walk(ambito)
    salida: dict = {}
    for n in nodos:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    salida.setdefault(t.id, []).append(n.value)
        elif (isinstance(n, (ast.AugAssign, ast.AnnAssign))
              and isinstance(n.target, ast.Name) and n.value is not None):
            salida.setdefault(n.target.id, []).append(n.value)
    return salida


def _parametros(funcion) -> set:
    if funcion is None:
        return set()
    a = funcion.args
    todos = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
    return {p.arg for p in todos if p is not None}


def _piezas(expr, locales, globales, parametros):
    """El SQL como [(es_texto, texto)], o None si no se puede reconstruir."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [(True, expr.value)]
    if isinstance(expr, ast.JoinedStr):
        piezas = []
        for v in expr.values:
            if isinstance(v, ast.Constant):
                piezas.append((True, v.value))
                continue
            textos = _textos_posibles(v.value)
            piezas.append((True, " ".join(textos)) if textos is not None
                          else (False, ""))
        return piezas
    if isinstance(expr, ast.Name) and expr.id not in parametros:
        valores = locales.get(expr.id) or globales.get(expr.id)
        if not valores:
            return None
        piezas = []
        for v in valores:
            parte = _piezas(v, {}, {}, set())
            if parte is None:
                return None
            piezas += parte + [(True, " ")]
        return piezas
    return None


def _legible(piezas):
    """(True, texto) si todo hueco es un nombre de tabla; (False, None) si no."""
    if piezas is None:
        return False, None
    texto = ""
    for es_texto, trozo in piezas:
        if not es_texto:
            if not _HUECO_DE_TABLA.search(texto.rstrip()):
                return False, None
            trozo = " <tabla> "
        texto += trozo
    return True, texto


def _llamadas_a_execute(nodo, funcion=None):
    """(función que la contiene o None, llamada) por cada `.execute(...)`."""
    for hijo in ast.iter_child_nodes(nodo):
        dentro = (hijo if isinstance(hijo, (ast.FunctionDef,
                                            ast.AsyncFunctionDef))
                  else funcion)
        if (isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
                and hijo.func.attr in _EJECUTAN):
            yield funcion, hijo
        yield from _llamadas_a_execute(hijo, dentro)


def _sql_de(llamada):
    if llamada.args:
        return llamada.args[0]
    return next((k.value for k in llamada.keywords if k.arg == "query"), None)


def _ejecuta_solo_lectura(funcion) -> bool:
    return any(
        isinstance(_sql_de(llamada), ast.Constant)
        and isinstance(_sql_de(llamada).value, str)
        and " ".join(_sql_de(llamada).value.split()).upper() == _SOLO_LECTURA
        for _, llamada in _llamadas_a_execute(funcion, funcion))


def _censo() -> dict:
    """Cada `execute` del repositorio, en su cubo. Ver LA FRONTERA."""
    import test_buzon_que_no_se_ve as barrido

    raiz = Path(RAIZ).resolve()
    pruebas = [p.resolve() for p in barrido._testpaths(raiz)]
    columna = _columna_del_codigo()
    escribe = re.compile(r"\b(?:UPDATE|INSERT|COPY)\b", re.I)
    nombra = re.compile(rf"\b{re.escape(columna)}\b")

    censo = {"sitios": 0, "legibles_que_escriben": {}, "genericos": {},
             "solo_lectura": {}}
    for py in barrido._py_en_disco(raiz):
        real = py.resolve()
        if any(c == real or c in real.parents for c in pruebas):
            continue
        rel = real.relative_to(raiz).as_posix()
        modulo = ast.parse(real.read_text(encoding="utf-8"), str(real))
        globales = _asignaciones(modulo)
        for funcion, llamada in _llamadas_a_execute(modulo):
            censo["sitios"] += 1
            quien = f"{rel}::{funcion.name if funcion else '<módulo>'}"
            sql = _sql_de(llamada)
            piezas = (None if sql is None else _piezas(
                sql, _asignaciones(funcion) if funcion else {}, globales,
                _parametros(funcion)))
            legible, texto = _legible(piezas)
            if not legible:
                cubo = ("solo_lectura" if funcion is not None
                        and _ejecuta_solo_lectura(funcion) else "genericos")
                censo[cubo].setdefault(quien, []).append(llamada.lineno)
            elif escribe.search(texto) and nombra.search(texto):
                censo["legibles_que_escriben"].setdefault(quien, funcion)
    return censo


def _id_de(fn) -> str:
    """El mismo `archivo::función` que usa el censo, sacado del objeto."""
    raiz = Path(RAIZ).resolve()
    archivo = Path(inspect.getsourcefile(fn)).resolve()
    return f"{archivo.relative_to(raiz).as_posix()}::{fn.__name__}"


# ── Una base de mentira para `acciones.crud` ─────────────────────────────
#
# Anota todo el SQL y sirve lo justo: la fila de la tarea para `editar` y la
# huella para `deshacer`. Mismo límite dicho arriba: ve el SQL que saldría, no
# lo que Postgres haría con él.

class _CursorDeCrud:
    def __init__(self, base):
        self._base = base
        self._fila = None

    async def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._base.sql.append((s, params))
        if s.startswith("SELECT") and "FROM log_acciones" in s:
            self._fila = self._base.huella
        elif s.startswith("SELECT"):
            self._fila = dict(self._base.fila) if self._base.fila else None
        elif "RETURNING" in s:
            self._fila = (5000,)
        else:
            self._fila = None
        return self

    async def fetchone(self):
        return self._fila

    async def fetchall(self):
        return [self._fila] if self._fila else []


class _BaseDeCrud:
    def __init__(self, fila=None, huella=None):
        self.fila = fila
        self.huella = huella
        self.sql: list = []

    def cursor(self, row_factory=None):
        return _CursorDeCrud(self)

    async def execute(self, sql, params=None):
        return await _CursorDeCrud(self).execute(sql, params)


def _correr(base, fn):
    """Corre `fn` contra `base`. Devuelve el ValueError si lo hubo, o None.

    Sólo se atrapa ValueError, que es como `crud` dice «no»: cualquier otra
    excepción revienta la prueba con su traza, en vez de contar como rechazo.
    """
    guardado = db.pool
    db.pool = _Pool(base)
    bucle = asyncio.new_event_loop()
    try:
        bucle.run_until_complete(fn())
        return None
    except ValueError as e:
        return e
    finally:
        bucle.close()
        db.pool = guardado


def _escrituras(base) -> list:
    """Las escrituras a la tabla de la entidad, sin la huella del log."""
    return [(s, p) for s, p in base.sql
            if re.match(r"(?:UPDATE|INSERT|DELETE)\b", s)
            and "log_acciones" not in s]


def _escribio(base, columna) -> bool:
    return any(re.search(rf"\b{re.escape(columna)}\b", s)
               for s, _ in _escrituras(base))


def _escribio_algo(base) -> bool:
    return any(re.match(r"(?:UPDATE|INSERT|DELETE)\b", s) for s, _ in base.sql)


def _sonda_editar(fn, chat):
    """Pide escribir `chat` como responsable por el camino de Telegram."""
    base = _BaseDeCrud(fila=_fila(1))
    return base, _correr(base, lambda: fn(
        "tareas", 1, {_columna_del_codigo(): chat}, motivo="sonda"))


def _sonda_deshacer(fn, chat):
    """Deshace una edición que CAMBIÓ el responsable y lo tenía en `chat`."""
    columna = _columna_del_codigo()
    otro = DUENO if chat != DUENO else OTRA
    base = _BaseDeCrud(huella={
        "accion": "editar", "tabla": "tareas", "registro_id": 1,
        "antes": {**_fila(1), columna: chat},
        "despues": {**_fila(1), columna: otro}})
    return base, _correr(base, lambda: fn(77))


# Una sonda por escritor genérico del censo. Las claves son los OBJETOS, no
# nombres: si mañana uno se renombra o se muda, deja de coincidir con el censo
# y se pone rojo, en vez de quedarse acá probando algo que ya no existe.
SONDAS = {crud.editar: _sonda_editar, crud.deshacer: _sonda_deshacer}


def test_todo_escritor_generico_tiene_sonda_y_la_sonda_corre():
    """LOS HERMANOS, Y QUE LO CUMPLAN TODOS.

    Los escritores genéricos salen del censo (cubo 3 de LA FRONTERA), no de una
    lista. Se exige que el conjunto sea EXACTAMENTE el de `SONDAS`: uno nuevo
    sin sonda se pone rojo, y una sonda de uno que ya no está, también.

    Y cada sonda corre dos veces. Con un chat que no entra al panel: tiene que
    decir que no y no escribir la columna. Con uno que sí: tiene que escribirla,
    porque una sonda que nunca llega a escribir no está midiendo nada.
    """
    _con_gente(LA_CASA)
    censo = _censo()
    columna = _columna_del_codigo()
    hallados = set(censo["genericos"])
    assert hallados, (
        "el censo no encontró ningún escritor genérico, y `crud.editar` lo es: "
        "el censo dejó de ver, no se arregló el problema")
    con_sonda = {_id_de(fn) for fn in SONDAS}
    assert hallados == con_sonda, (
        f"escritores genéricos SIN sonda: {sorted(hallados - con_sonda)}; "
        f"sondas de escritores que el censo ya no ve: "
        f"{sorted(con_sonda - hallados)}. Uno que arma el SQL al vuelo puede "
        f"escribir {columna} sin nombrarla, y leer su texto no lo va a ver")

    for fn, sonda in SONDAS.items():
        base, error = sonda(fn, AJENO)
        assert isinstance(error, ValueError), (
            f"{fn.__name__} aceptó un responsable que no entra al panel")
        assert not _escribio(base, columna), (
            f"{fn.__name__} escribió {columna} con un chat que no vale: "
            f"{_escrituras(base)}")

        base, error = sonda(fn, OTRA)
        assert error is None, f"{fn.__name__} rechazó a quien sí puede: {error}"
        assert _escribio(base, columna), (
            f"la sonda de {fn.__name__} no llegó a escribir {columna} ni con un "
            "chat que vale: no está midiendo nada")


def test_la_puerta_de_crud_es_la_columna_que_escribe_el_panel():
    """`crud` le pone la puerta a la columna por nombre, y ese nombre tiene que
    ser el que escribe `db.asignar_responsable` y declara la migración. Un typo
    en `crud.PUERTAS` dejaría a `editar` escribiendo la columna de verdad sin
    ninguna puerta."""
    assert _columna_del_codigo() in crud.PUERTAS.get("tareas", {}), (
        f"crud no le pone puerta a tareas.{_columna_del_codigo()}: "
        f"{crud.PUERTAS}")


# ── Por Telegram: `crud.editar` ──────────────────────────────────────────

def test_editar_no_escribe_un_responsable_que_no_vale_y_dice_por_que():
    """Decisión de Tiziano (10-sep-2026): por Telegram se puede asignar, pero
    sólo a quien entra al panel, y un chat que no vale se RECHAZA.

    Rechazar quiere decir las dos cosas: no se escribe nada, y quien lo pidió
    se entera de por qué. El motivo nombra a quién SÍ se le puede dejar —por
    nombre, sacado de la misma casa que usa la puerta— y nunca el número.
    """
    _con_gente(LA_CASA)
    for valor in (AJENO, str(AJENO), "cualquier cosa", True, 1.5, -1, 0):
        base, error = _sonda_editar(crud.editar, valor)
        assert isinstance(error, ValueError), f"editar aceptó {valor!r}"
        assert not _escribio_algo(base), (
            f"editar escribió algo con {valor!r}: {base.sql}")
        texto = str(error)
        assert str(AJENO) not in texto, "el rechazo enseña un número de chat"
        for _, nombre in config.personas_del_panel():
            assert nombre in texto, (
                f"el rechazo no dice a quién sí se le puede dejar: {texto!r}")

    # Tiene nombre y perdió el acceso al panel: tampoco.
    _con_gente({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    base, error = _sonda_editar(crud.editar, OTRA)
    assert isinstance(error, ValueError)
    assert not _escribio_algo(base)


def test_editar_asigna_a_quien_si_puede_y_deja_sin_responsable():
    """Los controles: lo que vale se escribe. Y «sin responsable» es de primera
    clase, venga como null o como texto vacío."""
    _con_gente(LA_CASA)
    columna = _columna_del_codigo()
    for valor, queda in ((OTRA, OTRA), (str(OTRA), OTRA),
                         (None, None), ("", None)):
        base, error = _sonda_editar(crud.editar, valor)
        assert error is None, f"editar rechazó {valor!r}: {error}"
        escrito = _escrituras(base)
        assert len(escrito) == 1 and columna in escrito[0][0], escrito
        assert escrito[0][1][0] == queda, (
            f"con {valor!r} guardó {escrito[0][1][0]!r} y tenía que quedar "
            f"{queda!r}")


def test_editar_otras_columnas_sigue_igual():
    """La puerta no le cambia nada a quien no la toca: el mismo UPDATE, con los
    mismos valores, que emitía `editar` antes de que existiera. Y una tabla sin
    puerta no pasa por ninguna aunque le manden una columna con ese nombre."""
    _con_gente(LA_CASA)
    base = _BaseDeCrud(fila=_fila(1))
    error = _correr(base, lambda: crud.editar(
        "tareas", 1, {"titulo": "otro", "estado": "hecha"}, motivo="t"))
    assert error is None
    assert _escrituras(base) == [
        ("UPDATE tareas SET titulo = %s, estado = %s WHERE id = %s",
         ("otro", "hecha", 1))]
    columna = _columna_del_codigo()
    assert crud._por_las_puertas("notas", {columna: AJENO}) == {columna: AJENO}


# ── Deshacer ─────────────────────────────────────────────────────────────

def _huella(antes, despues):
    return {"accion": "editar", "tabla": "tareas", "registro_id": 1,
            "antes": antes, "despues": despues}


def test_deshacer_no_deja_escrito_un_responsable_que_no_vale():
    """Una edición le cambió el responsable a alguien que HOY ya no puede
    tenerla. Deshacerla lo volvería a escribir: no se hace, y se dice por qué.
    No se deshace la mitad: la edición entera se queda como está."""
    _con_gente(LA_CASA)
    base, error = _sonda_deshacer(crud.deshacer, AJENO)
    assert isinstance(error, ValueError), "deshacer dejó escrito a quien no vale"
    assert not _escribio_algo(base), f"deshacer escribió algo: {base.sql}"
    assert str(AJENO) not in str(error)
    for _, nombre in config.personas_del_panel():
        assert nombre in str(error)

    # Lo tenía alguien con nombre que después perdió el acceso.
    _con_gente({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    base, error = _sonda_deshacer(crud.deshacer, OTRA)
    assert isinstance(error, ValueError)
    assert not _escribio_algo(base)


def test_deshacer_un_cambio_de_titulo_no_toca_el_responsable():
    """EL CASO DEL TÍTULO. Se cambia el título por Telegram, después alguien le
    pone responsable en el panel, y luego se deshace lo del título. Deshacer
    reescribía TODAS las columnas del `antes`, así que le devolvía a la tarea
    el responsable de aquel momento y pisaba el nuevo sin preguntarle a nadie.

    Ahora la columna con puerta vuelve atrás sólo si ESA edición la cambió. Se
    prueba con los tres responsables posibles en el `antes`: ninguno, uno que
    vale y uno que ya no vale —que tampoco puede bloquear deshacer un título—.
    Y con una huella que no dice qué quedó: si no se sabe, no se toca.
    """
    _con_gente(LA_CASA)
    columna = _columna_del_codigo()
    for resp in (None, OTRA, AJENO):
        for despues in (_fila(1, titulo="nuevo", responsable=resp), None):
            base = _BaseDeCrud(huella=_huella(
                _fila(1, titulo="viejo", responsable=resp), despues))
            error = _correr(base, lambda: crud.deshacer(77))
            assert error is None, f"no deshizo el título ({resp}): {error}"
            escrito = _escrituras(base)
            assert len(escrito) == 1 and "titulo" in escrito[0][0], escrito
            assert not re.search(rf"\b{columna}\b", escrito[0][0]), (
                f"deshacer un cambio de título reescribió {columna}: "
                f"{escrito[0][0]}")


def test_deshacer_devuelve_el_responsable_que_esa_edicion_cambio():
    """El control: si la edición SÍ cambió el responsable, deshacerla lo
    devuelve — a una persona que vale, o a «sin responsable»."""
    _con_gente(LA_CASA)
    columna = _columna_del_codigo()
    for antes, despues in ((OTRA, DUENO), (None, OTRA)):
        base = _BaseDeCrud(huella=_huella(_fila(1, responsable=antes),
                                          _fila(1, responsable=despues)))
        error = _correr(base, lambda: crud.deshacer(77))
        assert error is None, f"no devolvió {antes!r}: {error}"
        assert _escribio(base, columna), _escrituras(base)


def test_la_huella_que_deja_el_panel_se_deshace_por_la_misma_puerta():
    """La huella no es inventada: es la que escribe `db.asignar_responsable`,
    tal cual la manda a `log_acciones`. Deshacerla devuelve el responsable de
    antes; y si ése ya no puede tenerla, no se deshace."""
    _con_gente(LA_CASA)
    columna = _columna_del_codigo()
    ok, conn = _asignar(1, OTRA, [_fila(1, responsable=DUENO)])
    assert ok is True
    params = [p for s, p in conn.sql if "log_acciones" in s][0]
    huella = {"accion": "editar", "tabla": "tareas", "registro_id": params[0],
              "antes": json.loads(params[1]), "despues": json.loads(params[2])}

    base = _BaseDeCrud(huella=huella)
    assert _correr(base, lambda: crud.deshacer(77)) is None
    assert _escribio(base, columna), _escrituras(base)

    _con_gente({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(OTRA,))
    base = _BaseDeCrud(huella=huella)
    assert isinstance(_correr(base, lambda: crud.deshacer(77)), ValueError)
    assert not _escribio_algo(base)


# ── La pantalla ──────────────────────────────────────────────────────────

def _pedir(ruta="/tareas", con_sesion=True, chat=None):
    from starlette.requests import Request
    cabeceras = [(b"host", b"t")]
    if con_sesion:
        token = auth.crear_token(chat if chat is not None else DUENO,
                                 auth.VIDA_SESION)
        cabeceras.append((b"cookie", f"{panel.COOKIE}={token}".encode()))
    return Request({"type": "http", "http_version": "1.1", "method": "GET",
                    "scheme": "https", "server": ("t", 443), "path": ruta,
                    "root_path": "", "query_string": b"",
                    "headers": cabeceras, "app": panel.app})


def _junto(html: str) -> str:
    """El HTML con los espacios colapsados.

    La plantilla parte los atributos en varias líneas para que se lean; una
    comprobación sobre el texto crudo estaría midiendo dónde cayó el salto de
    línea en vez de qué dice el atributo.
    """
    return " ".join(html.split())


def _pintar(filas, **kw):
    r, _ = _con_base(filas, lambda: panel.tareas(_pedir(), **kw))
    assert r.status_code == 200, f"/tareas devolvió {r.status_code}"
    return r.body.decode()


def test_la_columna_vieja_ya_no_esta_y_la_nueva_si():
    """«La columna vieja se VA. No se conservan las dos.» Y se comprueba
    llamando a la ruta y renderizando la plantilla de verdad: una variable que
    la plantilla usa y la ruta no manda se cae acá, que es el fallo que dejó la
    portada en Internal Server Error con los tests en verde."""
    _con_gente(LA_CASA)
    html = _pintar([_fila(1), _fila(2, responsable=OTRA)])
    assert "Quién la anotó" not in html, "la columna vieja sigue ahí"
    assert "<th>Responsable</th>" in html, "no se pintó la columna nueva"
    assert ">Mengano</option>" in html and ">Zutana</option>" in html, (
        "el desplegable no trae a las personas de la casa")


def _opciones(html: str, tid: int = 1) -> list[str]:
    """Los textos que se LEEN en el desplegable de una fila.

    Se recorta el <select> de esa fila y se sacan los textos de sus <option>,
    sin atributos. Es lo que ve una persona; el `value=` queda fuera a
    propósito, porque ahí sí va el chat.
    """
    trozo = html[html.index(f'name="resp_{tid}"'):]
    trozo = trozo[:trozo.index("</select>")]
    textos = []
    for pedazo in trozo.split("<option")[1:]:
        # Lo que queda después del `>` que cierra la etiqueta: o sea el texto,
        # ya sin `value=` ni `selected`, que es justo lo que no se lee.
        cuerpo = pedazo.split(">", 1)[1]
        textos.append(" ".join(re.sub(r"<[^>]*>", " ", cuerpo).split()))
    return textos


def test_la_pantalla_no_enseña_ningun_numero_de_chat_como_texto():
    """Tiziano descartó enseñar el número: «no es relevante». La columna vieja
    lo imprimía cuando la tarea era de la otra persona, y eso no vuelve.

    Lo que se mira es el TEXTO que una persona LEE, no el HTML entero: el
    `value=` del desplegable sí es el chat, porque la identidad de una persona
    es su chat y no su nombre —un nombre se puede cambiar en Railway entre que
    se pinta la pantalla y se envía—. Lo que no puede pasar es que alguien LEA
    un número donde tendría que leer un nombre.

    La regla es «ni un dígito en lo que se lee del desplegable», y no «que no
    aparezca este número concreto»: así también agarra el día que a alguien se
    le ocurra pintar «Mengano (700000001)» como ayuda.
    """
    _con_gente(LA_CASA)
    html = _pintar([_fila(1, responsable=OTRA), _fila(2, responsable=DUENO)])
    for tid in (1, 2):
        for texto in _opciones(html, tid):
            assert not re.search(r"\d", texto), (
                f"el desplegable enseña un número donde se lee un nombre: "
                f"{texto!r}")
    assert set(_opciones(html)) == {"sin responsable"} | set(LA_CASA.values()), (
        f"el desplegable no ofrece la casa: {_opciones(html)}")


def test_la_tarea_sin_responsable_se_pinta_y_no_es_un_error():
    """Es el caso más común —las 57 vivas nacieron así— y tiene que verse como
    lo que es: una tarea que nadie tomó todavía. Ni un aviso, ni un hueco, ni
    una fila escondida."""
    _con_gente(LA_CASA)
    html = _pintar([_fila(1, titulo="afinar el piano")])
    assert "afinar el piano" in html, "la tarea sin responsable desapareció"
    assert 'value="" selected>sin responsable' in html, (
        "el desplegable no cae en «sin responsable» cuando no hay ninguno")
    assert 'name="prev_resp_1" value=""' in _junto(html), (
        "el valor previo de una tarea sin responsable tiene que ir vacío, o el "
        "primer envío la reescribiría sola")


def test_un_responsable_que_ya_no_se_puede_asignar_no_se_pierde():
    """EL CASO QUE BORRA UN DATO EN SILENCIO SI NADIE LO MIRA.

    A alguien le quitan el nombre —o el acceso— y las tareas que tenía siguen
    guardadas con su chat. Si el desplegable no trae ninguna opción que lo
    represente, el navegador cae en la primera —«sin responsable»—, el
    `prev_resp_` dice otra cosa, y el siguiente «Guardar los cambios» borra el
    responsable de todas esas tareas sin que nadie lo haya pedido.

    Así que se le pinta su propia opción, ya seleccionada: con su nombre si
    todavía se sabe, y «sin nombre» si ni eso. Nunca el número.
    """
    # Tiene nombre pero perdió el acceso: se sigue leyendo su nombre.
    _con_gente({DUENO: "Zutana", OTRA: "Mengano"}, permitidos=(DUENO,))
    html = _pintar([_fila(1, responsable=OTRA)])
    assert "Mengano" in html, "se perdió de vista quién tiene la tarea"
    assert f'value="{OTRA}" selected' in html, (
        "el desplegable no representa al responsable guardado: el próximo "
        "envío lo borraría solo")

    # Y sin nombre ni acceso: se dice que no se sabe, no se inventa ni se
    # enseña el número.
    _con_gente({DUENO: "Zutana"}, permitidos=(DUENO,))
    html = _pintar([_fila(1, responsable=OTRA)])
    assert "sin nombre" in html, (
        "un responsable del que no se sabe el nombre tiene que decirse, no "
        "desaparecer")
    assert f'value="{OTRA}" selected' in html
    for texto in _opciones(html):
        assert not re.search(r"\d", texto), (
            f"enseñó el número de chat de quien ya no se puede asignar: {texto!r}")


def test_el_panel_dice_a_quien_le_falta_el_nombre_sin_decir_su_numero():
    """Si a alguien que entra al panel le falta el nombre, no aparece en el
    desplegable. Sin este aviso, Tiziano buscaría un nombre que no puede estar
    y no habría ni una pista de por qué.

    Va una CUENTA y nunca un chat: para arreglar la variable no hace falta que
    el panel recite el número de Telegram de nadie.
    """
    _con_gente({DUENO: "Zutana"}, permitidos=(DUENO, OTRA))
    html = _pintar([_fila(1)])
    assert "NOMBRES_POR_CHAT" in html, (
        "el panel no dice que falta un nombre: el desplegable queda corto y "
        "nadie sabe por qué")
    visible = " ".join(re.sub(r"<[^>]*>", " ", html).split())
    assert str(OTRA) not in visible, "el aviso enseñó el número de chat"

    # Con todos los nombres puestos, el aviso NO sale: un panel que avisa
    # siempre es un panel que nadie lee.
    _con_gente(LA_CASA)
    assert "NOMBRES_POR_CHAT" not in _pintar([_fila(1)])


def test_el_panel_dice_cuantas_entradas_de_la_variable_no_se_entendieron():
    """La otra mitad del aviso. Una entrada mal escrita se descarta —no se
    adivina— y por eso hay que decir que se descartó."""
    _con_gente(LA_CASA)
    config.NOMBRES_MAL_ESCRITOS = 2
    html = _pintar([_fila(1)])
    assert "NOMBRES_POR_CHAT" in html and "chat:nombre" in html, (
        "el panel se calla las entradas que no entendió")

    config.NOMBRES_MAL_ESCRITOS = 0
    assert "chat:nombre" not in _pintar([_fila(1)])


def test_sigue_habiendo_UN_SOLO_formulario_en_la_pantalla():
    """El desplegable vive DENTRO del formulario que ya había. Con un <form>
    por fila, guardar uno recarga y se lleva puesto todo lo demás marcado; y un
    <form> dentro de otro es HTML inválido — el navegador descarta el de
    adentro y el botón deja de hacer nada, EN SILENCIO."""
    ruta = os.path.join(RAIZ, "web", "plantillas", "tareas.html")
    html = open(ruta, encoding="utf-8").read()
    assert html.count("<form") == 1 and html.count("</form>") == 1
    assert 'name="resp_' in html, "el desplegable no está en la plantilla"


# ── Guardar desde la pantalla ────────────────────────────────────────────

def _post(campos: dict, con_sesion: bool = True):
    from urllib.parse import urlencode
    from starlette.requests import Request

    cuerpo = urlencode(campos).encode()
    cabeceras = [(b"host", b"t"),
                 (b"content-type", b"application/x-www-form-urlencoded"),
                 (b"content-length", str(len(cuerpo)).encode())]
    if con_sesion:
        token = auth.crear_token(DUENO, auth.VIDA_SESION)
        cabeceras.append((b"cookie", f"{panel.COOKIE}={token}".encode()))

    async def recibir():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    return Request({"type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "https", "server": ("t", 443), "path": "/tareas",
                    "root_path": "", "query_string": b"",
                    "headers": cabeceras, "app": panel.app}, recibir)


def _guardar(campos: dict, con_sesion: bool = True):
    """Manda el formulario con las dos escrituras ESPIADAS.

    Devuelve (respuesta, cerradas, asignaciones). Los espías se ponen sobre el
    módulo `db` y el `conftest.py` los devuelve a su sitio al terminar.
    """
    cerradas: list = []
    asignaciones: list = []

    async def _cerrar(tid):
        cerradas.append(tid)
        return True

    async def _asignar_espia(tid, chat):
        asignaciones.append((tid, chat))
        return True

    db.marcar_tarea_hecha = _cerrar
    db.asignar_responsable = _asignar_espia
    bucle = asyncio.new_event_loop()
    try:
        r = bucle.run_until_complete(
            panel.guardar_tareas(_post(campos, con_sesion)))
    finally:
        bucle.close()
    return r, cerradas, asignaciones


def test_la_ruta_que_escribe_exige_sesion():
    """ESCRIBE, así que es una puerta: sin sesión, un POST de cualquiera
    reasignaría las tareas de esta casa."""
    _con_gente(LA_CASA)
    r, cerradas, asignadas = _guardar(
        {"prev_resp_1": "", "resp_1": str(OTRA)}, con_sesion=False)
    assert r.status_code == 401, f"entró sin cookie: {r.status_code}"
    assert asignadas == [], "escribió sin sesión"
    assert cerradas == []


def test_se_asigna_y_se_reasigna_y_se_deja_sin_responsable():
    """Los tres movimientos que la pantalla tiene que permitir, en un solo
    envío. El tercero —dejarla sin nadie— es tan legítimo como los otros dos."""
    _con_gente(LA_CASA)
    r, _, asignadas = _guardar({
        "prev_resp_1": "", "resp_1": str(OTRA),          # asignar
        "prev_resp_2": str(OTRA), "resp_2": str(DUENO),  # reasignar
        "prev_resp_3": str(DUENO), "resp_3": "",         # dejar sin nadie
    })
    assert r.status_code == 303
    assert sorted(asignadas) == sorted(
        [(1, OTRA), (2, DUENO), (3, None)]), asignadas
    assert "asignadas=3" in r.headers["location"]


def test_el_desplegable_que_nadie_toco_no_reescribe_nada():
    """Un <select> SIEMPRE viaja, tocado o no. Sin `prev_resp_`, cada envío
    reescribiría el responsable de las cuarenta filas de la pantalla y llenaría
    `log_acciones` de ediciones que nadie pidió."""
    _con_gente(LA_CASA)
    r, _, asignadas = _guardar({
        "prev_resp_1": str(OTRA), "resp_1": str(OTRA),
        "prev_resp_2": "", "resp_2": "",
        "prev_resp_3": "", "resp_3": str(DUENO),
    })
    assert asignadas == [(3, DUENO)], (
        f"reescribió filas que nadie tocó: {asignadas}")
    assert "asignadas=1" in r.headers["location"]


def test_un_responsable_que_no_entra_al_panel_no_se_escribe():
    """La ruta rechaza antes de llamar a la base. Y con cualquier basura
    también: un `resp_` con texto, o con el número de alguien que no entra."""
    _con_gente(LA_CASA)
    for valor in (str(AJENO), "cualquier cosa", "0", "-1"):
        r, _, asignadas = _guardar({"prev_resp_1": "", "resp_1": valor})
        assert r.status_code == 303, f"reventó la pantalla con {valor!r}"
        assert asignadas == [], (
            f"escribió un responsable que no entra al panel: {valor!r}")


def test_cerrar_y_reasignar_viajan_en_el_MISMO_envio():
    """Es un solo gesto en la vida real —cerrar dos y pasarle la tercera a la
    otra persona— y por eso el desplegable vive dentro del formulario que ya
    cerraba varias. Los dos contadores vuelven por separado: un solo número no
    diría cuál de las dos cosas pasó."""
    _con_gente(LA_CASA)
    r, cerradas, asignadas = _guardar({
        "prev_1": "pendiente", "hecha_1": "1",
        "prev_resp_1": "", "resp_1": "",
        "prev_2": "pendiente", "prev_resp_2": "", "resp_2": str(OTRA),
    })
    assert cerradas == [1], cerradas
    assert asignadas == [(2, OTRA)], asignadas
    assert "guardadas=1" in r.headers["location"]
    assert "asignadas=1" in r.headers["location"]


def test_un_campo_con_basura_no_revienta_la_pantalla():
    _con_gente(LA_CASA)
    r, _, asignadas = _guardar({
        "resp_abc": str(OTRA), "resp_": str(OTRA),
        "prev_resp_5": "", "resp_5": str(OTRA),
        "volver": "//evil.com"})
    assert r.status_code == 303
    assert asignadas == [(5, OTRA)]
    # No hay destino elegible: de acá se vuelve siempre a /tareas.
    assert r.headers["location"].startswith("/tareas?")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
