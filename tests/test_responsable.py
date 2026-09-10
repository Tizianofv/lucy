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

  · QUE NO SE PUEDA ASIGNAR A CUALQUIERA. Una tarea que queda con alguien que
    no puede abrir el panel es un pendiente que esa persona no va a ver nunca.
    La puerta es `config.puede_ser_responsable`, y hay una prueba que recorre
    `db/db.py` buscando QUIÉN MÁS escribe esa columna y exige que también la
    nombre — para que el segundo camino que alguien escriba mañana no pueda
    nacer sin puerta.

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
import os
import re
import sys
import textwrap
import types
from datetime import datetime, timezone

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
    """LA PRUEBA QUE IMPIDE QUE NAZCA UN SEGUNDO CAMINO SIN PUERTA.

    Hoy `db.asignar_responsable` es la única función que escribe
    `responsable_chat_id`, y valida con `config.puede_ser_responsable`. Eso se
    sabe leyendo el archivo; el problema es que mañana alguien escriba otra que
    también la toque y se olvide de la puerta — y leer el archivo es justo lo
    que nadie va a volver a hacer.

    LA LISTA DE FUNCIONES NO ESTÁ TECLEADA ACÁ: sale de recorrer `db/db.py` con
    el árbol de sintaxis y quedarse con las que escriben esa columna. La que
    alguien agregue mañana entra sola en el recorrido, y si no nombra la puerta
    esto se pone rojo sin que nadie venga a añadirla a ninguna parte.
    """
    columna = _columna_del_codigo()
    puerta = config.puede_ser_responsable.__name__

    arbol = ast.parse(open(os.path.join(RAIZ, "db", "db.py"),
                           encoding="utf-8").read())
    escriben, con_puerta = [], []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        textos = " ".join(
            n.value for n in ast.walk(nodo)
            if isinstance(n, ast.Constant) and isinstance(n.value, str))
        if not re.search(rf"(?:UPDATE|INSERT).*{columna}", textos, re.S | re.I):
            continue
        escriben.append(nodo.name)
        nombres = {n.id for n in ast.walk(nodo) if isinstance(n, ast.Name)}
        nombres |= {n.attr for n in ast.walk(nodo) if isinstance(n, ast.Attribute)}
        if puerta in nombres:
            con_puerta.append(nodo.name)

    assert escriben, (
        f"no se encontró ninguna función que escriba {columna} en db/db.py: "
        "esta guarda estaría verde sin haber mirado nada")
    sin_puerta = sorted(set(escriben) - set(con_puerta))
    assert not sin_puerta, (
        f"estas funciones escriben {columna} sin pasar por {puerta}: "
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
    tecleadas: son las de web/app.py que llaman a `asignar_responsable`. Cada
    una tiene que llegar a la puerta, ella misma o a través de una función del
    mismo módulo a la que llame — un nivel, que es la forma que tiene hoy
    (`guardar_tareas` → `_responsable_pedido` → la puerta). La que alguien
    escriba mañana entra sola en el recorrido.
    """
    puerta = config.puede_ser_responsable.__name__
    escritura = db.asignar_responsable.__name__
    arbol = ast.parse(open(os.path.join(RAIZ, "web", "app.py"),
                           encoding="utf-8").read())

    funciones = {n.name: n for n in arbol.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _nombres(nodo):
        # Solo CÓDIGO: nombres y atributos. Un docstring es un ast.Constant y
        # no entra, así que la prosa no puede hacer pasar esta prueba.
        return ({n.id for n in ast.walk(nodo) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(nodo)
                   if isinstance(n, ast.Attribute)})

    escriben = [f for f in funciones.values() if escritura in _nombres(f)]
    assert escriben, (
        f"ninguna función de web/app.py llama a {escritura}: esta guarda "
        "estaría verde sin haber mirado nada")

    sin_puerta = []
    for f in escriben:
        propios = _nombres(f)
        alcanza = puerta in propios or any(
            puerta in _nombres(funciones[otro])
            for otro in propios if otro in funciones and otro != f.name)
        if not alcanza:
            sin_puerta.append(f.name)
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
