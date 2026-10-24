"""UN PROYECTO NO SE CREA SIN CLIENTE: pieza 1, «la puerta» (Tiziano, 18-oct-2026).

Las reglas de Tiziano que esta pieza sostiene (`disenos/lucy-proyecto-con-cliente/DISENO.md`):

  · cliente obligatorio en CDS, ACD y sin grupo; IA queda libre;
  · en CDS y ACD el nombre del proyecto es el nombre del cliente;
  · UN SOLO proyecto ACTIVO por cliente (un pausado y un cerrado no cuentan: reabrir o reactivar uno se rechaza si el
    cliente ya tiene uno activo);
  · los cinco proyectos que hoy no tienen cliente se pueden seguir viendo, editando y cerrando como hoy.

Garantías (la numeración es la del diseño, §2.8, pieza 1):

  G1   ningún camino alcanzable inserta en `proyectos` sin las dos columnas del cliente (censo con `ast`, sacado de lo
       real y con entradas inventadas);
  G2   Noco sin contestar: 0 filas en `proyectos` y en `log_acciones` (CDS, ACD y sin grupo, por los tres creadores);
  G3   ficha inexistente / Id que no es un número / bool / negativo: 0 filas;
  G4   el nombre del cliente que se guarda es el de Noco;           G5  la huella `despues` trae el cliente;
  G6   llamar sin `cliente_noco_id` / `leer_persona` es `TypeError`; G7  IA sin cliente: nace y no lee Noco;
  G7b  `area=None` o un grupo desconocido exige cliente;            G7c `poner_cliente(None)`;
  G7d  cambiar el grupo de un proyecto sin cliente;                 G7e restaurar de la papelera uno sin cliente;
  G7f  todos llaman a la MISMA `cliente_es_obligatorio`;            G7g el nombre en CDS/ACD es el del cliente;
  G7h  `editar` nombre;  G7i  `poner_cliente` con la ficha de otro nombre;
  G7j  `proyecto_vivo_del_cliente` es la única que decide «un solo activo»;  G7k pausados y cerrados no cuentan;
  G7k2 las nueve transiciones antes/después;  G7l IA ni cuenta ni es contado;  G7m el mismo cliente en su proyecto;
  G7n  los hermanos reaccionan juntos;  G7p el nombre repetido y la búsqueda por nombre;  G7q reactivar con nombre.

FRONTERA, dicha una vez:

  · NO hay Postgres: todo corre sobre SQLite con el texto de las sentencias reales. Por eso el índice único y el CHECK
    (pieza 6) NO se prueban aquí, ni G7o (la carrera de dos pedidos a la vez, que solo el índice cierra).
  · `crud.deshacer` de una EDICIÓN usa `jsonb_populate_record`, que SQLite no tiene: de esa rama se prueba el RECHAZO
    (que ocurre antes del SQL) y que, sin la comprobación, el camino llega al SQL de Postgres (y por eso revienta en
    SQLite). El éxito de esa rama no se prueba aquí. La rama `borrar` (restaurar) sí corre entera.
  · El doble de Noco afirma solo la forma `{id, nombre}` o `None` que fija el diseño de Lucy 1.0 (§4); no es una captura.
  · Lo que no se ve con `ast`: un SQL armado por partes en tiempo de ejecución.

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_crear_proyecto_telegram as _tct
import test_grupo_ia as g
from test_grupo_ia import _ROOT
from psycopg.rows import dict_row
from test_pagina_proyectos import Mundo, gente, mundo, ver, HOY  # noqa: F401
import acciones.crud as crud
import config
import db.db as db
import noco_lectura

pytestmark = pytest.mark.usefixtures("drive_aparte")      # el alta no habla con Drive aquí (ver conftest.py)

DUENO = config.CHAT_ID_DUENO


# ═══════════════════════════════════════════════════════════════════════
# Piezas
# ═══════════════════════════════════════════════════════════════════════

def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


class Noco:
    """El doble de Noco: `persona(id)`. Cuenta los pedidos; `cae=True` es Noco sin contestar."""

    def __init__(self, fichas=None):
        self.fichas = dict(fichas or {})
        self.pedidos: list = []
        self.cae = False

    async def persona(self, noco_id):
        self.pedidos.append(noco_id)
        if self.cae:
            raise noco_lectura.NocoNoContesta("Noco de mentira: no contesta")
        return self.fichas.get(noco_id)


def _ficha(i, nombre=None):
    return {"id": i, "nombre": nombre if nombre is not None else f"Cliente {i}"}


@pytest.fixture
def noco(monkeypatch):
    n = Noco({i: _ficha(i) for i in (101, 102, 103, 104)})
    monkeypatch.setattr(noco_lectura, "persona", n.persona)
    return n


def _p(m, id, nombre, area="CDS", estado="activo", cliente=None, cliente_nombre=None, borrado=False):
    """Un proyecto sembrado a mano (directo a la base: no pasa por la puerta, como los que ya existían)."""
    m.con.execute(
        "INSERT INTO proyectos (id, nombre, estado, area, creado_en, borrado_en, cliente_noco_id, cliente_nombre, "
        "responsable_chat_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (id, nombre, estado, area, "2026-10-01T00:00:00+00:00",
         "2026-09-01T00:00:00+00:00" if borrado else None, cliente,
         cliente_nombre if cliente_nombre is not None else (f"Cliente {cliente}" if cliente else None), DUENO))
    m.con.commit()
    return id


def _foto(m) -> dict:
    """TODO lo que las puertas pueden escribir: filas completas de las tres tablas."""
    return {t: [tuple(f) for f in m.con.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("proyectos", "log_acciones", "tareas")}


def _fila(m, pid) -> dict:
    f = m.con.execute("SELECT * FROM proyectos WHERE id = ?", (pid,)).fetchone()
    return dict(f) if f is not None else None


def _crear(m, nombre, area, cliente, leer, **kw):
    return _correr(db.crear_proyecto(nombre, area, DUENO, cliente_noco_id=cliente, leer_persona=leer, **kw))


def _convertir(m, tid, cliente, leer):
    return _correr(db.convertir_tarea_en_proyecto(tid, cliente_noco_id=cliente, leer_persona=leer))


def _editar(pid, cambios, **kw):
    return _correr(crud.editar("proyectos", pid, cambios, motivo="prueba", actor=kw.pop("actor", "panel"), **kw))


# ═══════════════════════════════════════════════════════════════════════
# G1: el censo de quién inserta en `proyectos`, de lo real y con entradas inventadas
# ═══════════════════════════════════════════════════════════════════════

_COLUMNAS_DEL_CLIENTE = ("cliente_noco_id", "cliente_nombre")


def _creadores_sin_cliente(raiz) -> list:
    """Quiénes pueden INSERTAR un proyecto sin las dos columnas del cliente: (a) funciones con un `INSERT INTO
    proyectos (…)` cuyas columnas no nombran las dos, y (b) los que lo hacen por el escritor GENÉRICO
    (`INSERT INTO {tabla}`, que no puede traer cliente) o por un envoltorio que le pasa el literal «proyectos».
    La lista sale de `test_crear_proyecto_telegram._creadores_de_proyectos` (ast, f-strings rearmados)."""
    creadores, _ = _tct._creadores_de_proyectos(raiz)
    malos = []
    for rel, arbol in _tct._modulos(raiz):
        for fn in _tct._funciones(arbol):
            if (rel, fn.name) not in creadores:
                continue
            directas = [m for t in _tct._sql_de_una_funcion(fn)
                        for m in _tct._INSERT.finditer(t) if m.group(1).lower() == "proyectos"]
            if not directas:
                malos.append((rel, fn.name))            # lo crea por el genérico o por un envoltorio: sin cliente
                continue
            for t in _tct._sql_de_una_funcion(fn):
                for m in re.finditer(r"INSERT\s+INTO\s+proyectos\s*\(([^)]*)\)", t, re.I):
                    if not all(c in m.group(1) for c in _COLUMNAS_DEL_CLIENTE):
                        malos.append((rel, fn.name))
    return sorted(set(malos))


def test_G1_ningun_creador_de_proyectos_inserta_sin_las_dos_columnas_del_cliente():
    creadores, _ = _tct._creadores_de_proyectos(_ROOT)
    assert sorted(n for _, n in creadores) == ["convertir_tarea_en_proyecto", "crear_proyecto"], sorted(creadores)
    assert _creadores_sin_cliente(_ROOT) == []


@pytest.mark.parametrize("codigo", [
    # el mismo INSERT sin cliente escrito como literal en una función nueva…
    "async def nueva(c):\n    await c.execute('INSERT INTO proyectos (nombre, area) VALUES (1, 2)')\n",
    # …y como `INSERT INTO {tabla}` con tabla="proyectos" (la PAREJA de la puerta única)
    "async def generico(c, tabla):\n    await c.execute(f'INSERT INTO {tabla} (nombre) VALUES (1)')\n"
    "async def envoltorio(c):\n    await generico(c, 'proyectos')\n",
    # solo una de las dos columnas
    "async def a_medias(c):\n    await c.execute('INSERT INTO proyectos (nombre, cliente_noco_id) VALUES (1, 2)')\n",
])
def test_G1_el_censo_ve_creadores_inventados_sin_cliente(tmp_path, codigo):
    (tmp_path / "falso.py").write_text(codigo, encoding="utf-8")
    assert _creadores_sin_cliente(tmp_path) != []


def test_G1_el_censo_acepta_un_creador_inventado_que_si_trae_el_cliente(tmp_path):
    (tmp_path / "falso.py").write_text(
        "async def bien(c):\n    await c.execute("
        "'INSERT INTO proyectos (nombre, cliente_noco_id, cliente_nombre) VALUES (1, 2, 3)')\n", encoding="utf-8")
    assert _creadores_sin_cliente(tmp_path) == []


def test_G1_el_escritor_generico_ya_no_puede_crear_proyectos(mundo):
    """No es solo el censo: el genérico (`_buscar_o_crear`) rechaza cualquier tabla que no sea `personas`."""
    for tabla in ("proyectos", "tareas", "personas; DROP"):
        if tabla == "personas; DROP":
            continue
        with pytest.raises(ValueError):
            _correr(db._buscar_o_crear(tabla, "X"))
    assert not hasattr(db, "buscar_o_crear_proyecto")
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0


# ═══════════════════════════════════════════════════════════════════════
# G2, G3: Noco sin contestar / ficha que no vale → 0 filas (por los TRES creadores)
# ═══════════════════════════════════════════════════════════════════════

def _sembrar_tarea(m, camino, area):
    """La tarea suelta que el camino «convertir» va a convertir (los otros dos no la usan). `area=None` es «sin grupo»,
    que solo existe al convertir."""
    if camino == "db.convertir_tarea_en_proyecto":
        m.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Tarea suelta', ?, 9090)", (area,))
        m.con.commit()


CAMINOS = ["db.crear_proyecto", "crud.crear_proyecto", "db.convertir_tarea_en_proyecto"]


def _intentar(m, camino, area, cliente):
    """Intenta crear por ese camino; devuelve lo que levantó (o None si creó)."""
    try:
        if camino == "db.crear_proyecto":
            _crear(m, None, area, cliente, noco_lectura.persona)
        elif camino == "crud.crear_proyecto":
            _correr(crud.crear_proyecto(None, area, DUENO, cliente))
        else:
            _convertir(m, 90, cliente, noco_lectura.persona)
    except Exception as e:      # noqa: BLE001  (se mira QUÉ levantó)
        return e
    return None


@pytest.mark.parametrize("camino", CAMINOS)
@pytest.mark.parametrize("area", ["CDS", "ACD"])
def test_G2_si_noco_no_contesta_no_se_escribe_nada(mundo, gente, noco, camino, area):
    noco.cae = True
    _sembrar_tarea(mundo, camino, area)
    antes = _foto(mundo)
    e = _intentar(mundo, camino, area, 101)
    assert e is not None and (isinstance(e, (noco_lectura.NocoNoContesta, crud.NoDeNegocio))), repr(e)
    assert _foto(mundo) == antes, "escribió algo aunque Noco no contestó"


def test_G2_convertir_una_tarea_sin_grupo_con_noco_caido_no_escribe_nada(mundo, gente, noco):
    noco.cae = True
    antes = None
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Suelta', NULL, 9090)")
    mundo.con.commit()
    antes = _foto(mundo)
    with pytest.raises(noco_lectura.NocoNoContesta):
        _convertir(mundo, 90, 101, noco_lectura.persona)
    assert _foto(mundo) == antes


@pytest.mark.parametrize("camino", CAMINOS)
@pytest.mark.parametrize("cliente", [0, -3, True, False, "101", 101.0, [101]])
def test_G3_un_id_que_no_es_un_numero_positivo_no_se_le_pregunta_a_noco_ni_escribe(mundo, gente, noco, camino, cliente):
    _sembrar_tarea(mundo, camino, "CDS")
    antes = _foto(mundo)
    e = _intentar(mundo, camino, "CDS", cliente)
    if camino == "crud.crear_proyecto" and cliente == "101":
        # (Telegram: el modelo manda el Id como texto de dígitos; `crud` lo vuelve número y la ficha SÍ se pide.)
        assert e is None and noco.pedidos == [101]
        return
    assert isinstance(e, (db.ProyectoNoSeCrea, crud.NoDeNegocio)), repr(e)
    assert noco.pedidos == [] and _foto(mundo) == antes


@pytest.mark.parametrize("camino", CAMINOS)
@pytest.mark.parametrize("ficha", [None, {}, {"id": 999, "nombre": "De otro Id"}, {"id": 101, "nombre": ""},
                                   {"id": 101, "nombre": "   "}, {"id": 101, "nombre": None}, {"id": 101}])
def test_G3_una_ficha_que_no_vale_no_escribe_nada(mundo, gente, noco, camino, ficha):
    noco.fichas[101] = ficha
    _sembrar_tarea(mundo, camino, "CDS")
    antes = _foto(mundo)
    e = _intentar(mundo, camino, "CDS", 101)
    assert isinstance(e, (db.ProyectoNoSeCrea, crud.NoDeNegocio)), repr(e)
    assert _foto(mundo) == antes


# ═══════════════════════════════════════════════════════════════════════
# G4, G5, G7g: el nombre del cliente es el de Noco y es el nombre del proyecto; la huella lo trae
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("area", ["CDS", "ACD"])
def test_G4_G5_G7g_crear_guarda_el_nombre_de_noco_en_cliente_y_en_nombre_y_la_huella_lo_trae(mundo, gente, noco, area):
    noco.fichas[101] = _ficha(101, "  Nombre Desde Noco ")
    nuevo = _crear(mundo, None, area, 101, noco_lectura.persona)
    f = _fila(mundo, nuevo["id"])
    assert (f["cliente_noco_id"], f["cliente_nombre"], f["nombre"]) == (101, "Nombre Desde Noco", "Nombre Desde Noco")
    h = [x for x in mundo.con.execute("SELECT * FROM log_acciones WHERE tabla = 'proyectos'")]
    assert len(h) == 1 and h[0]["accion"] == "crear"
    despues = json.loads(h[0]["despues"])
    assert (despues["cliente_noco_id"], despues["cliente_nombre"], despues["nombre"]) == (
        101, "Nombre Desde Noco", "Nombre Desde Noco")


@pytest.mark.parametrize("area", ["CDS", "ACD"])
@pytest.mark.parametrize("pedido", ["Un nombre escrito a mano", "cliente 101", "", 5])
def test_G7g_un_nombre_distinto_del_cliente_se_rechaza_y_no_se_ignora_en_silencio(mundo, gente, noco, area, pedido):
    antes = _foto(mundo)
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        _crear(mundo, pedido, area, 101, noco_lectura.persona)
    assert e.value.clave == "nombre" and _foto(mundo) == antes


@pytest.mark.parametrize("pedido", [None, "Cliente 101", "  Cliente 101  "])
def test_G7g_el_nombre_pedido_igual_al_del_cliente_vale(mundo, gente, noco, pedido):
    nuevo = _crear(mundo, pedido, "CDS", 101, noco_lectura.persona)
    assert nuevo["nombre"] == "Cliente 101"


def test_G7g_convertir_pone_el_nombre_del_cliente_y_el_titulo_pasa_a_la_descripcion(mundo, gente, noco):
    mundo.con.execute("INSERT INTO tareas (id, titulo, detalle, area, bandeja_id) "
                      "VALUES (90, 'Grabar el disco', 'con la banda completa', 'CDS', 9090)")
    mundo.con.commit()
    r = _convertir(mundo, 90, 101, noco_lectura.persona)
    f = _fila(mundo, r["proyecto_id"])
    assert (f["nombre"], f["cliente_noco_id"], f["cliente_nombre"], f["area"]) == ("Cliente 101", 101, "Cliente 101", "CDS")
    assert f["descripcion"] == "Grabar el disco\ncon la banda completa"
    # la tarea queda archivada y recuperable, y las dos huellas de hoy siguen
    assert mundo.con.execute("SELECT borrado_en FROM tareas WHERE id = 90").fetchone()[0] is not None
    assert sorted(x["tabla"] + "/" + x["accion"] for x in mundo.con.execute("SELECT * FROM log_acciones")) == [
        "proyectos/crear", "tareas/borrar"]


def test_G7g_poner_cliente_cambia_tambien_el_nombre_del_proyecto_en_la_misma_escritura(mundo, gente, noco):
    _p(mundo, 1, "Viejo", cliente=101)
    assert _correr(db.poner_cliente(1, 102, leer_persona=noco.persona)) is True
    f = _fila(mundo, 1)
    assert (f["cliente_noco_id"], f["cliente_nombre"], f["nombre"]) == (102, "Cliente 102", "Cliente 102")
    h = mundo.con.execute("SELECT * FROM log_acciones").fetchone()
    assert json.loads(h["despues"]) == {"cliente_noco_id": 102, "cliente_nombre": "Cliente 102", "nombre": "Cliente 102"}


# ═══════════════════════════════════════════════════════════════════════
# G6: sin cliente_noco_id / leer_persona no se llama
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("funcion", [db.crear_proyecto, db.convertir_tarea_en_proyecto, db.poner_cliente])
def test_G6_los_parametros_del_cliente_son_obligatorios_y_solo_por_nombre(funcion):
    p = inspect.signature(funcion).parameters
    for nombre in ("cliente_noco_id", "leer_persona"):
        if funcion is db.poner_cliente and nombre == "cliente_noco_id":
            continue
        assert p[nombre].kind is inspect.Parameter.KEYWORD_ONLY and p[nombre].default is inspect.Parameter.empty, (
            funcion.__name__, nombre)


def test_G6_llamar_sin_el_cliente_es_un_error_de_programacion(mundo, gente):
    with pytest.raises(TypeError):
        _correr(db.crear_proyecto("X", "IA", DUENO))
    with pytest.raises(TypeError):
        _correr(db.crear_proyecto("X", "IA", DUENO, cliente_noco_id=None))
    with pytest.raises(TypeError):
        _correr(db.convertir_tarea_en_proyecto(1))
    assert mundo.con.execute("SELECT count(*) FROM proyectos").fetchone()[0] == 0


# ═══════════════════════════════════════════════════════════════════════
# G7, G7b, G7f: IA libre; lo que no se sabe clasificar exige cliente; todos preguntan a la MISMA función
# ═══════════════════════════════════════════════════════════════════════

def test_G7_en_ia_sin_cliente_nace_y_no_lee_noco(mundo, gente, noco):
    assert _crear(mundo, "Mejorar a Lucy", "IA", None, noco_lectura.persona)["nombre"] == "Mejorar a Lucy"
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Algo de IA', 'IA', 9090)")
    mundo.con.commit()
    r = _convertir(mundo, 90, None, noco_lectura.persona)
    assert r["proyecto_nombre"] == "Algo de IA" and noco.pedidos == []
    assert _fila(mundo, r["proyecto_id"])["cliente_noco_id"] is None


def test_G7_en_ia_con_cliente_se_lee_y_se_guarda_pero_el_nombre_es_el_escrito(mundo, gente, noco):
    nuevo = _crear(mundo, "Proyecto interno", "IA", 101, noco_lectura.persona)
    assert (nuevo["nombre"], nuevo["cliente_noco_id"], nuevo["cliente_nombre"]) == ("Proyecto interno", 101, "Cliente 101")
    assert noco.pedidos == [101]


@pytest.mark.parametrize("area", [None, "", "cds", "Inventada", 5, "IA ", "ia"])
def test_G7b_lo_que_no_se_sabe_clasificar_exige_cliente(area):
    assert db.cliente_es_obligatorio(area) is True


def test_G7b_solo_ia_no_lo_exige():
    assert db.cliente_es_obligatorio(db.AREA_TECNICA) is False
    assert [a for a in ("CDS", "ACD", "Hogar", None) if not db.cliente_es_obligatorio(a)] == []


def test_G7b_una_tarea_sin_area_no_se_convierte_sin_cliente_y_con_cliente_si(mundo, gente, noco):
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Suelta', NULL, 9090)")
    mundo.con.commit()
    antes = _foto(mundo)
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        _convertir(mundo, 90, None, noco_lectura.persona)
    assert e.value.clave == "cliente" and _foto(mundo) == antes and noco.pedidos == []
    r = _convertir(mundo, 90, 101, noco_lectura.persona)
    f = _fila(mundo, r["proyecto_id"])
    assert (f["area"], f["nombre"], f["cliente_noco_id"]) == (None, "Cliente 101", 101)


@pytest.mark.parametrize("area", ["CDS", "ACD"])
def test_G7b_crear_en_cds_o_acd_sin_cliente_no_crea(mundo, gente, noco, area):
    antes = _foto(mundo)
    with pytest.raises(db.ProyectoNoSeCrea) as e:
        _crear(mundo, "Con nombre", area, None, noco_lectura.persona)
    assert e.value.clave == "cliente" and _foto(mundo) == antes and noco.pedidos == []


# ═══════════════════════════════════════════════════════════════════════
# G7c: quitar el cliente solo en IA
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("area", ["CDS", "ACD", None])
def test_G7c_poner_cliente_none_se_rechaza_fuera_de_ia_y_no_escribe(mundo, gente, noco, area):
    _p(mundo, 1, "Cliente 101", area=area, cliente=101)
    antes = _foto(mundo)
    with pytest.raises(db.ClienteDelProyectoNoVale) as e:
        _correr(db.poner_cliente(1, None, leer_persona=noco.persona))
    assert e.value.clave == "obligatorio" and _foto(mundo) == antes


def test_G7c_en_ia_si_se_quita(mundo, gente, noco):
    _p(mundo, 1, "Interno", area="IA", cliente=101)
    assert _correr(db.poner_cliente(1, None, leer_persona=noco.persona)) is True
    assert (_fila(mundo, 1)["cliente_noco_id"], _fila(mundo, 1)["nombre"]) == (None, "Interno")


# ═══════════════════════════════════════════════════════════════════════
# G7d: cambiar el grupo
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("destino", ["CDS", "ACD"])
def test_G7d_un_proyecto_de_ia_sin_cliente_no_pasa_a_un_grupo_que_lo_exige(mundo, gente, destino):
    _p(mundo, 1, "Interno", area="IA")
    antes = _foto(mundo)
    with pytest.raises(ValueError, match="necesita cliente"):
        _editar(1, {"area": destino})
    assert _foto(mundo) == antes


def test_G7d_un_proyecto_de_ia_con_cliente_pasa_y_toma_el_nombre_del_cliente_en_la_misma_escritura(mundo, gente):
    _p(mundo, 1, "Interno", area="IA", cliente=101)
    despues, _ = _editar(1, {"area": "ACD"})
    f = _fila(mundo, 1)
    assert (f["area"], f["nombre"]) == ("ACD", "Cliente 101") and "nombre" in despues.escritas


def test_G7d_pasar_a_ia_no_toca_el_nombre_y_un_proyecto_sin_cliente_de_cds_puede_cambiar_de_grupo(mundo, gente):
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)
    _p(mundo, 2, "Interno viejo", area="CDS")                 # de los cinco: sin cliente
    _editar(1, {"area": "IA"})
    assert (_fila(mundo, 1)["area"], _fila(mundo, 1)["nombre"]) == ("IA", "Cliente 101")
    _editar(2, {"area": "ACD"})                               # CDS → ACD: no es una exigencia nueva
    _editar(2, {"area": "IA"})                                # y a IA, siempre
    assert _fila(mundo, 2)["area"] == "IA"


# ═══════════════════════════════════════════════════════════════════════
# G7e: restaurar de la papelera
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("area", ["CDS", "ACD"])
def test_G7e_restaurar_uno_sin_cliente_fuera_de_ia_se_rechaza_con_motivo_y_no_escribe(mundo, gente, area):
    _p(mundo, 1, "Sin cliente", area=area)
    _correr(crud.borrar("proyectos", 1, "x", actor="panel"))
    antes = _foto(mundo)
    with pytest.raises(ValueError, match="necesita cliente"):
        _correr(crud.deshacer_borrado("proyectos", 1))
    assert _foto(mundo) == antes and _fila(mundo, 1)["borrado_en"] is not None


def test_G7e_restaurar_uno_de_ia_o_uno_con_cliente_si_vuelve(mundo, gente):
    _p(mundo, 1, "Interno", area="IA")
    _p(mundo, 2, "Cliente 101", area="CDS", cliente=101)
    for pid in (1, 2):
        _correr(crud.borrar("proyectos", pid, "x", actor="panel"))
        _correr(crud.deshacer_borrado("proyectos", pid))
        assert _fila(mundo, pid)["borrado_en"] is None


# ═══════════════════════════════════════════════════════════════════════
# G7h: el nombre a mano
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("area", ["CDS", "ACD", None])
def test_G7h_renombrar_a_mano_en_cds_acd_o_sin_grupo_se_rechaza(mundo, gente, area):
    _p(mundo, 1, "Cliente 101", area=area, cliente=101)
    antes = _foto(mundo)
    with pytest.raises(ValueError, match="nombre del proyecto es el del cliente") as e:
        _editar(1, {"nombre": "Otro nombre"})
    assert isinstance(e.value.__cause__, db.NombreDeProyectoNoVale) and e.value.__cause__.clave == "cliente"
    assert _foto(mundo) == antes


def test_G7h_en_ia_renombrar_funciona(mundo, gente):
    _p(mundo, 1, "Interno", area="IA")
    _editar(1, {"nombre": "Interno 2"})
    assert _fila(mundo, 1)["nombre"] == "Interno 2"


def test_G7h_deshacer_un_renombre_viejo_tampoco_deja_un_nombre_a_mano_en_cds(mundo, gente):
    """Hermano de `editar`: `deshacer` es otro camino que escribe `nombre`."""
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)
    antes = {"id": 1, "nombre": "Un nombre viejo", "estado": "activo", "area": "CDS"}
    mundo.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) "
                      "VALUES ('lucy', 'editar', 'proyectos', 1, ?, ?)",
                      (json.dumps(antes), json.dumps({**antes, "nombre": "Cliente 101"})))
    mundo.con.commit()
    log_id = mundo.con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]
    foto = _foto(mundo)
    with pytest.raises(ValueError, match="nombre del proyecto es el del cliente"):
        _correr(crud.deshacer(log_id))
    assert _foto(mundo) == foto


# ═══════════════════════════════════════════════════════════════════════
# G7i: la ficha cambió de nombre en Noco
# ═══════════════════════════════════════════════════════════════════════

def test_G7i_escoger_otra_vez_al_mismo_cliente_con_la_ficha_renombrada_actualiza_los_dos_nombres(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)
    noco.fichas[101] = _ficha(101, "Nombre Nuevo De La Ficha")
    assert _correr(db.poner_cliente(1, 101, leer_persona=noco.persona)) is True
    f = _fila(mundo, 1)
    assert (f["cliente_nombre"], f["nombre"]) == ("Nombre Nuevo De La Ficha", "Nombre Nuevo De La Ficha")
    # y repetirlo ya no cambia nada ni deja otra huella
    n = mundo.con.execute("SELECT count(*) FROM log_acciones").fetchone()[0]
    assert _correr(db.poner_cliente(1, 101, leer_persona=noco.persona)) is False
    assert mundo.con.execute("SELECT count(*) FROM log_acciones").fetchone()[0] == n


def test_G7i_un_nombre_viejo_distinto_del_cliente_se_arregla_aunque_la_ficha_no_cambio(mundo, gente, noco):
    _p(mundo, 1, "Nombre viejo a mano", area="CDS", cliente=101, cliente_nombre="Cliente 101")
    assert _correr(db.poner_cliente(1, 101, leer_persona=noco.persona)) is True
    assert _fila(mundo, 1)["nombre"] == "Cliente 101"


# ═══════════════════════════════════════════════════════════════════════
# G7j, G7k, G7k2, G7l, G7m: UN SOLO PROYECTO ACTIVO POR CLIENTE
# ═══════════════════════════════════════════════════════════════════════

def _con_un_activo_del_cliente(m, area_del_activo="CDS"):
    _p(m, 1, "Cliente 101", area=area_del_activo, cliente=101)       # el activo que ocupa al cliente
    return 1


def test_G7j_crear_se_rechaza_si_el_cliente_ya_tiene_otro_activo_y_el_aviso_trae_id_y_nombre(mundo, gente, noco):
    _con_un_activo_del_cliente(mundo)
    antes = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto) as e:
        _crear(mundo, None, "ACD", 101, noco_lectura.persona)           # entre CDS y ACD juntos
    assert (e.value.proyecto_id, e.value.nombre, e.value.estado) == (1, "Cliente 101", "activo")
    assert "Cliente 101" not in str(e.value) and "#1" in str(e.value)    # el mensaje no lleva el nombre (los logs)
    assert _foto(mundo) == antes


def test_G7j_convertir_se_rechaza_igual_y_la_tarea_queda_pendiente(mundo, gente, noco):
    _con_un_activo_del_cliente(mundo)
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Suelta', 'ACD', 9090)")
    mundo.con.commit()
    antes = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto):
        _convertir(mundo, 90, 101, noco_lectura.persona)
    assert _foto(mundo) == antes


def test_G7j_por_telegram_el_rechazo_dice_el_proyecto_y_que_no_se_pueden_activar_dos(mundo, gente, noco):
    _con_un_activo_del_cliente(mundo)
    antes = _foto(mundo)
    with pytest.raises(crud.NoDeNegocio) as e:
        _correr(crud.crear_proyecto(None, "CDS", DUENO, 101))
    assert "#1" in str(e.value) and "Cliente 101" in str(e.value) and "no se pueden activar 2" in str(e.value)
    assert "no se pueden activar 2" in crud.lo_que_lee_la_persona(e.value)
    assert _foto(mundo) == antes


def test_G7j_poner_cliente_a_uno_activo_se_rechaza_y_no_cambia_nada(mundo, gente, noco):
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Cliente 102", area="CDS", cliente=102)
    antes = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto) as e:
        _correr(db.poner_cliente(2, 101, leer_persona=noco.persona))
    assert e.value.proyecto_id == 1 and _foto(mundo) == antes


def test_G7j_cambiar_de_ia_a_cds_con_el_cliente_ocupado_se_rechaza(mundo, gente):
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Interno", area="IA", cliente=101)
    antes = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto):
        _editar(2, {"area": "CDS"})
    assert _foto(mundo) == antes


@pytest.mark.parametrize("viejo", ["cerrado", "pausado"])
def test_G7j_reabrir_o_reactivar_por_editar_se_rechaza_si_el_cliente_tiene_otro_activo(mundo, gente, viejo):
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Cliente 101", area="CDS", estado=viejo, cliente=101)
    antes = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto) as e:
        _editar(2, {"estado": "activo"}, actor="lucy")                   # Telegram
    assert e.value.proyecto_id == 1
    assert _foto(mundo) == antes


def test_G7j_reabrir_por_la_ruta_del_panel_se_rechaza_igual(mundo, gente):
    from _navegador import Navegador
    import web.app as panel
    import web.auth as auth
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Cliente 101", area="CDS", estado="cerrado", cliente=101)
    antes = _foto(mundo)
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    r = c.post("/proyectos/2/estado", data={"estado": "activo"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert _foto(mundo) == antes


def test_G7j_deshacer_un_cierre_o_una_pausa_se_rechaza_igual_antes_de_tocar_la_base(mundo, gente):
    """Rama `editar` de `deshacer`: el rechazo ocurre ANTES del SQL de Postgres (frontera dicha arriba)."""
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Cliente 101", area="CDS", estado="cerrado", cliente=101)
    antes = {"id": 2, "nombre": "Cliente 101", "estado": "activo", "area": "CDS"}
    mundo.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) "
                      "VALUES ('lucy', 'editar', 'proyectos', 2, ?, ?)",
                      (json.dumps(antes), json.dumps({**antes, "estado": "cerrado"})))
    mundo.con.commit()
    log_id = mundo.con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]
    foto = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto):
        _correr(crud.deshacer(log_id))
    assert _foto(mundo) == foto


def test_G7j_restaurar_de_la_papelera_se_rechaza_si_vuelve_activo_y_el_cliente_ya_tiene_otro(mundo, gente):
    _p(mundo, 2, "Cliente 101", area="CDS", cliente=101)
    _correr(crud.borrar("proyectos", 2, "x", actor="panel"))
    _con_un_activo_del_cliente(mundo)
    foto = _foto(mundo)
    with pytest.raises(db.ClienteConProyecto):
        _correr(crud.deshacer_borrado("proyectos", 2))
    assert _foto(mundo) == foto and _fila(mundo, 2)["borrado_en"] is not None


@pytest.mark.parametrize("estado", ["pausado", "cerrado"])
def test_G7j_restaurar_uno_que_estaba_pausado_o_cerrado_vuelve_asi_y_no_choca(mundo, gente, estado):
    _p(mundo, 2, "Cliente 101", area="CDS", estado=estado, cliente=101)
    _correr(crud.borrar("proyectos", 2, "x", actor="panel"))
    _con_un_activo_del_cliente(mundo)
    _correr(crud.deshacer_borrado("proyectos", 2))
    assert _fila(mundo, 2)["borrado_en"] is None and _fila(mundo, 2)["estado"] == estado


@pytest.mark.parametrize("estado", ["pausado", "cerrado"])
def test_G7k_un_pausado_o_un_cerrado_del_mismo_cliente_no_impide_crear_otro_y_queda_como_estaba(
        mundo, gente, noco, estado):
    _p(mundo, 1, "Cliente 101", area="CDS", estado=estado, cliente=101)
    antes = _fila(mundo, 1)
    nuevo = _crear(mundo, None, "CDS", 101, noco_lectura.persona)
    assert nuevo["nombre"] == "Cliente 101" and nuevo["id"] != 1, "chocó por nombre con el viejo"
    assert _fila(mundo, 1) == antes, "el proyecto viejo cambió"
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Suelta', 'CDS', 9090)")
    mundo.con.commit()
    with pytest.raises(db.ClienteConProyecto):                  # y ahora el nuevo ocupa al cliente
        _convertir(mundo, 90, 101, noco_lectura.persona)


def test_G7k_pausar_o_cerrar_el_activo_deja_abrir_otro_y_nunca_se_rechaza(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)
    _editar(1, {"estado": "pausado"})
    _crear(mundo, None, "ACD", 101, noco_lectura.persona)
    _editar(1, {"estado": "cerrado"})                           # pausado → cerrado tampoco se rechaza
    assert _fila(mundo, 1)["estado"] == "cerrado"


def _combinaciones():
    """Las 9 combinaciones antes/después, SACADAS de `db.ESTADOS_PROYECTO` (no tecleadas)."""
    return [(a, d) for a in db.ESTADOS_PROYECTO for d in db.ESTADOS_PROYECTO]


def test_G7k2_hay_nueve_combinaciones_y_salen_del_vocabulario():
    assert len(_combinaciones()) == len(db.ESTADOS_PROYECTO) ** 2 == 9


@pytest.mark.parametrize("antes,despues", _combinaciones())
def test_G7k2_solo_se_consulta_al_pasar_a_activo_desde_uno_que_no_lo_era(mundo, gente, antes, despues):
    """Un proyecto del cliente 101 en `antes`, y OTRO activo del mismo cliente. Pasa a `despues` por la puerta de
    `editar`. Se rechaza SOLO si no era activo y pasa a serlo ('activo' es el único estado del vocabulario que
    cuenta; los otros dos son los que Tiziano dijo que no cuentan)."""
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)                    # el otro activo
    # El proyecto que se mueve es de IA con el mismo cliente, para que SU estado de partida no choque con el del otro
    # (dos activos del mismo cliente no existen en CDS/ACD): lo que se prueba es la transición.
    _p(mundo, 2, "Otro nombre", area="IA", estado=antes, cliente=101)
    _correr(crud.editar("proyectos", 2, {"area": "CDS"}, motivo="x", actor="panel")) if False else None
    mundo.con.execute("UPDATE proyectos SET area = 'CDS', nombre = 'Cliente 101 bis' WHERE id = 2")   # ya en CDS (sembrado)
    mundo.con.commit()
    debe_rechazar = antes != "activo" and despues == "activo"
    foto = _foto(mundo)
    if debe_rechazar:
        with pytest.raises(db.ClienteConProyecto):
            _editar(2, {"estado": despues})
        assert _foto(mundo) == foto
    else:
        _editar(2, {"estado": despues})
        assert _fila(mundo, 2)["estado"] == despues


def test_G7k2_un_estado_fuera_del_vocabulario_cuenta_como_activo(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", area="CDS", estado="estado-que-nadie-conoce", cliente=101)
    assert db.cuenta_como_activo("estado-que-nadie-conoce") is True and db.cuenta_como_activo(None) is True
    with pytest.raises(db.ClienteConProyecto):
        _crear(mundo, None, "CDS", 101, noco_lectura.persona)


def test_G7l_ia_ni_cuenta_ni_es_contada(mundo, gente, noco):
    _p(mundo, 1, "Interno", area="IA", cliente=101)                          # IA activo con el cliente
    _crear(mundo, None, "CDS", 101, noco_lectura.persona)                    # no impide abrir uno en CDS
    _crear(mundo, "Otro interno", "IA", 101, noco_lectura.persona)           # y el CDS no impide otro de IA
    assert db.cliente_es_obligatorio("IA") is False


def test_G7m_volver_a_escoger_al_mismo_cliente_en_su_propio_proyecto_no_es_choque(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101)
    assert _correr(db.poner_cliente(1, 101, leer_persona=noco.persona)) is False         # nada cambió: no choca consigo


@pytest.mark.parametrize("estado", ["pausado", "cerrado"])
def test_G7m_un_pausado_o_cerrado_puede_tomar_cualquier_cliente(mundo, gente, noco, estado):
    _con_un_activo_del_cliente(mundo)
    _p(mundo, 2, "Cliente 102", area="CDS", estado=estado, cliente=102)
    assert _correr(db.poner_cliente(2, 101, leer_persona=noco.persona)) is True
    assert _fila(mundo, 2)["cliente_noco_id"] == 101


def test_G7_los_papeleras_no_cuentan(mundo, gente, noco):
    _p(mundo, 1, "Cliente 101", area="CDS", cliente=101, borrado=True)
    _crear(mundo, None, "CDS", 101, noco_lectura.persona)


# ═══════════════════════════════════════════════════════════════════════
# G7n: los hermanos. Cambiar la función compartida los pone a TODOS en otra
# ═══════════════════════════════════════════════════════════════════════

def _sembrar_los_caminos(m):
    """Sembrado: 1 = el activo del cliente 101; 2 = cerrado; 3 = pausado; 4 = IA con cliente; 5 = en la papelera (con su
    huella `borrar`); 7 = de otro cliente; y una tarea de CDS. (El activo tiene un nombre VIEJO distinto del cliente
    —como 5 de los 10 de producción el 18-oct—: así el rechazo de cada camino es solo por «un solo activo por cliente»
    y no lo tapa el chequeo del nombre repetido.)"""
    _p(m, 1, "Nombre viejo del activo", area="CDS", cliente=101, cliente_nombre="Cliente 101")
    _p(m, 2, "Cerrado 101", area="CDS", estado="cerrado", cliente=101)
    _p(m, 3, "Pausado 101", area="CDS", estado="pausado", cliente=101)
    _p(m, 4, "Interno 101", area="IA", cliente=101)
    _p(m, 5, "Borrado 101", area="CDS", cliente=101, borrado=True)
    _p(m, 7, "Cliente 102", area="CDS", cliente=102)
    m.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'Suelta', 'CDS', 9090)")
    m.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes) VALUES "
                  "('lucy', 'borrar', 'proyectos', 5, ?)", (json.dumps(_fila(m, 5), default=str),))
    m.con.commit()


def _camino(nombre, m, noco):
    return {
        "crear": lambda: _crear(m, None, "ACD", 101, noco.persona),
        "convertir": lambda: _convertir(m, 90, 101, noco.persona),
        "poner_cliente": lambda: _correr(db.poner_cliente(7, 101, leer_persona=noco.persona)),
        "cambiar_grupo": lambda: _editar(4, {"area": "CDS"}),
        "reabrir_cerrado": lambda: _editar(2, {"estado": "activo"}),
        "reactivar_pausado": lambda: _editar(3, {"estado": "activo"}),
        "restaurar": lambda: _correr(crud.deshacer_borrado("proyectos", 5)),
    }[nombre]


LOS_CAMINOS = ["crear", "convertir", "poner_cliente", "cambiar_grupo", "reabrir_cerrado", "reactivar_pausado",
               "restaurar"]


def _correr_camino(nombre, noco, monkeypatch):
    """Cada camino en SU mundo (si no, el que crea un proyecto cambia lo que ven los siguientes)."""
    m = Mundo()
    monkeypatch.setattr(db, "pool", g._Pool(m.con))
    monkeypatch.setattr(db, "hoy_rd", lambda: HOY)
    _sembrar_los_caminos(m)
    try:
        _camino(nombre, m, noco)()
    except Exception as e:      # noqa: BLE001
        return e
    return None


@pytest.mark.parametrize("camino", LOS_CAMINOS)
def test_G7n_cada_camino_rechaza_con_la_funcion_compartida(mundo, gente, noco, monkeypatch, camino):
    e = _correr_camino(camino, noco, monkeypatch)
    assert isinstance(e, db.ClienteConProyecto) and e.proyecto_id == 1, repr(e)


@pytest.mark.parametrize("camino", LOS_CAMINOS)
def test_G7n_si_se_cambia_la_funcion_compartida_por_nadie_ocupa_a_nadie_TODOS_dejan_pasar(
        mundo, gente, noco, monkeypatch, camino):
    async def nadie(cur, cliente_noco_id, excluir_id=None):
        return None
    monkeypatch.setattr(db, "proyecto_vivo_del_cliente", nadie)
    assert _correr_camino(camino, noco, monkeypatch) is None


def test_G7n_deshacer_un_cierre_tambien_depende_de_la_funcion_compartida(mundo, gente, monkeypatch):
    """El octavo camino, `crud.deshacer` de una EDICIÓN: su éxito usa `jsonb_populate_record` (no existe en SQLite),
    así que sin la comprobación el camino sigue hasta el SQL de Postgres y revienta AHÍ (`sqlite3.OperationalError`), no con
    `ClienteConProyecto`. Con la función real, se rechaza antes."""
    _p(mundo, 1, "Nombre viejo del activo", area="CDS", cliente=101, cliente_nombre="Cliente 101")   # (sin chocar por nombre)
    _p(mundo, 2, "Cliente 101", area="CDS", estado="cerrado", cliente=101)
    antes = {"id": 2, "nombre": "Cliente 101", "estado": "activo", "area": "CDS"}
    mundo.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) "
                      "VALUES ('lucy', 'editar', 'proyectos', 2, ?, ?)",
                      (json.dumps(antes), json.dumps({**antes, "estado": "cerrado"})))
    mundo.con.commit()
    log_id = mundo.con.execute("SELECT max(id) FROM log_acciones").fetchone()[0]
    with pytest.raises(db.ClienteConProyecto):
        _correr(crud.deshacer(log_id))

    async def nadie(cur, cliente_noco_id, excluir_id=None):
        return None
    monkeypatch.setattr(db, "proyecto_vivo_del_cliente", nadie)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):          # el SQL de Postgres (`UPDATE … FROM jsonb_populate_record`)
        _correr(crud.deshacer(log_id))


def test_G7n_la_comprobacion_vive_en_una_sola_funcion_y_todos_los_escritores_la_llaman():
    llamadas = {}
    for rel, arbol in _tct._modulos(_ROOT):
        for fn in _tct._funciones(arbol):
            vistas = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                      for n in ast.walk(fn) if isinstance(n, ast.Call)}
            for nombre in ("proyecto_puede_quedar_activo", "proyecto_vivo_del_cliente", "cliente_es_obligatorio"):
                if nombre in vistas:
                    llamadas.setdefault(nombre, set()).add((rel, fn.name))
    # LA decisión de «un solo activo» la toma `proyecto_vivo_del_cliente` y la llama solo la función compartida.
    # Lucy 4 (24-oct-2026) agrega UNA LECTURA: `crear_cotizacion_de_natalia` busca el proyecto abierto del cliente para colgar
    # la formal de él. No decide si se abre otro proyecto (no escribe `proyectos`).
    assert llamadas["proyecto_vivo_del_cliente"] == {("db/db.py", "proyecto_puede_quedar_activo"),
                                                     ("db/db.py", "crear_cotizacion_de_natalia")}
    # y la función compartida la llaman LOS CINCO escritores (editar y deshacer son los dos genéricos de crud)
    assert llamadas["proyecto_puede_quedar_activo"] == {
        ("db/db.py", "crear_proyecto"), ("db/db.py", "convertir_tarea_en_proyecto"), ("db/db.py", "poner_cliente"),
        ("acciones/crud.py", "editar"), ("acciones/crud.py", "deshacer")}
    # `cliente_es_obligatorio` (G7f) la llaman todos los que deciden por el grupo
    assert {("db/db.py", "crear_proyecto"), ("db/db.py", "convertir_tarea_en_proyecto"), ("db/db.py", "poner_cliente"),
            ("db/db.py", "nombre_que_toca"), ("acciones/crud.py", "editar"), ("acciones/crud.py", "deshacer"),
            ("web/app.py", "crear_proyecto_nuevo")} <= llamadas["cliente_es_obligatorio"]


_ACTIVA = re.compile(r"(INSERT\s+INTO\s+proyectos\b"
                     r"|UPDATE\s+(proyectos|\{…\})\s*(?:\w+\s+)?SET\s+[^;]*?\b(estado|area|cliente_noco_id)\s*="
                     r"|UPDATE\s+(proyectos|\{…\})\s+SET\s+borrado_en\s*=\s*NULL)", re.I | re.S)

# Escritores de `proyectos` que NO pasan por la función compartida y por qué (frontera dicha): `borrar_grupo` solo pone
# `area = NULL` a proyectos de un grupo que se borra, y todos los grupos que se pueden borrar exigen cliente igual que
# «sin grupo» (IA es el grupo fijo y no se borra): no hay transición que comprobar.
_SIN_LA_FUNCION_Y_POR_QUE = {("acciones/crud.py", "borrar_grupo")}


def _escritores_que_pueden_activar(raiz) -> set:
    salida = set()
    for rel, arbol in _tct._modulos(raiz):
        for fn in _tct._funciones(arbol):
            if any(_ACTIVA.search(t) for t in _tct._sql_de_una_funcion(fn)):
                llamadas = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                            for n in ast.walk(fn) if isinstance(n, ast.Call)}
                if "proyecto_puede_quedar_activo" not in llamadas:
                    salida.add((rel, fn.name))
    return salida


def test_G7n_ningun_otro_escritor_del_estado_el_grupo_el_cliente_o_la_papelera_se_salta_la_funcion():
    sueltos = _escritores_que_pueden_activar(_ROOT)
    assert sueltos - _SIN_LA_FUNCION_Y_POR_QUE == set(), (
        f"estos escriben estado/grupo/cliente/papelera de un proyecto sin la comprobación: "
        f"{sorted(sueltos - _SIN_LA_FUNCION_Y_POR_QUE)}")


def test_G7n_el_censo_ve_un_septimo_camino_inventado(tmp_path):
    (tmp_path / "falso.py").write_text(
        "async def reactivar_a_escondidas(c):\n"
        "    await c.execute('UPDATE proyectos SET estado = %s WHERE id = %s')\n"
        "async def generico(c, tabla):\n"
        "    await c.execute(f'UPDATE {tabla} SET estado = %s WHERE id = %s')\n"
        "async def restaurar_a_escondidas(c):\n"
        "    await c.execute('UPDATE proyectos SET borrado_en = NULL WHERE id = 3')\n", encoding="utf-8")
    assert {n for _, n in _escritores_que_pueden_activar(tmp_path)} == {
        "reactivar_a_escondidas", "generico", "restaurar_a_escondidas"}


@pytest.mark.parametrize("valor", [True, False])
def test_G7f_cambiar_cliente_es_obligatorio_por_una_constante_pone_a_todos_los_sitios_en_otra(
        mundo, gente, noco, monkeypatch, valor):
    """Si la función compartida decide «ninguno exige cliente» (o «todos»), los cinco sitios que la usan reaccionan: nadie
    tiene su propia comparación con 'IA'."""
    monkeypatch.setattr(db, "cliente_es_obligatorio", lambda area: valor)
    _p(mundo, 1, "Interno", area="IA")
    _p(mundo, 2, "Cliente 101", area="CDS", cliente=101)
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (90, 'T', 'CDS', 9090)")
    mundo.con.execute("INSERT INTO tareas (id, titulo, area, bandeja_id) VALUES (91, 'T2', 'IA', 9091)")
    _p(mundo, 4, "Para restaurar", area="CDS", borrado=True)
    mundo.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes) VALUES "
                      "('lucy', 'borrar', 'proyectos', 4, ?)", (json.dumps(_fila(mundo, 4), default=str),))
    mundo.con.commit()

    def rechaza(f, tipo=ValueError):
        try:
            f()
        except tipo:
            return True
        return False
    # crear sin cliente: se rechaza si «todos exigen» y pasa si «ninguno exige»
    assert rechaza(lambda: _crear(mundo, "Sin cliente en CDS", "CDS", None, noco.persona)) is valor
    # convertir una tarea sin cliente
    assert rechaza(lambda: _convertir(mundo, 90, None, noco.persona)) is valor
    # quitar el cliente
    assert rechaza(lambda: _correr(db.poner_cliente(2, None, leer_persona=noco.persona))) is valor
    # renombrar a mano
    assert rechaza(lambda: _editar(2, {"nombre": "Otro a mano"})) is valor
    # restaurar uno sin cliente
    assert rechaza(lambda: _correr(crud.deshacer_borrado("proyectos", 4))) is valor
    # pasar de IA sin cliente a CDS: es una TRANSICIÓN entre «no exige» y «exige». Con una constante no hay transición:
    # no rechaza con ninguna de las dos (con la función real sí, ver G7d).
    assert rechaza(lambda: _editar(1, {"area": "CDS"})) is False


# ═══════════════════════════════════════════════════════════════════════
# G7p, G7q: el nombre repetido y la búsqueda por nombre
# ═══════════════════════════════════════════════════════════════════════

def test_G7p_proyecto_vivo_con_nombre_ignora_pausados_y_cerrados(mundo, gente):
    async def buscar(nombre, excluir=None):
        async with db.pool.connection() as conn:
            return await db.proyecto_vivo_con_nombre(conn.cursor(row_factory=dict_row), nombre, excluir)
    _p(mundo, 1, "Mismo", area="IA", estado="pausado")
    _p(mundo, 2, "Mismo", area="IA", estado="cerrado")
    assert _correr(buscar("mismo")) is None
    _p(mundo, 3, "Mismo", area="IA", estado="activo")
    assert _correr(buscar("MISMO")) == 3 and _correr(buscar("mismo", excluir=3)) is None


def _por_nombre(nombre):
    return _correr(db.proyecto_vivo_por_nombre(nombre))


def test_G7p_la_busqueda_por_nombre_prefiere_al_activo_aunque_otro_tenga_menor_id(mundo, gente):
    _p(mundo, 1, "Cliente 101", estado="cerrado", cliente=101)
    _p(mundo, 2, "Cliente 101", estado="pausado", cliente=101)
    _p(mundo, 3, "Cliente 101", estado="activo", cliente=101)
    assert _por_nombre("cliente 101") == 3


def test_G7p_sin_activo_elige_al_pausado_y_sin_pausado_al_cerrado_de_menor_id(mundo, gente):
    _p(mundo, 1, "Cliente 101", estado="cerrado", cliente=101)
    _p(mundo, 2, "Cliente 101", estado="cerrado", cliente=101)
    assert _por_nombre("Cliente 101") == 1                       # con dos cerrados no hace falta preguntar
    _p(mundo, 3, "Cliente 101", estado="pausado", cliente=101)
    assert _por_nombre("Cliente 101") == 3
    assert _por_nombre("Nadie") is None and _por_nombre("   ") is None


def test_G7p_con_dos_pausados_y_ningun_activo_pregunta_en_vez_de_elegir(mundo, gente):
    _p(mundo, 1, "Cliente 101", estado="pausado", cliente=101)
    _p(mundo, 2, "Cliente 101", estado="pausado", cliente=101)
    _p(mundo, 3, "Cliente 101", estado="cerrado", cliente=101)
    with pytest.raises(db.NombreEnVarios) as e:
        _por_nombre("Cliente 101")
    assert e.value.ids == (1, 2)
    _p(mundo, 4, "Cliente 101", estado="activo", cliente=101)    # con un activo ya no es ambiguo
    assert _por_nombre("Cliente 101") == 4


def test_G7p_el_perfil_encuentra_a_un_proyecto_que_solo_esta_pausado_o_cerrado(mundo, gente):
    """`crud.perfil` usa la misma búsqueda que Telegram: no dice «un proyecto no nace por acá» de uno que existe."""
    _p(mundo, 1, "Solo cerrado", area="IA", estado="cerrado")
    r, _ = _correr(crud.perfil("proyecto", "solo cerrado", descripcion="una descripción"))
    assert r.startswith("OK") and _fila(mundo, 1)["descripcion"] == "una descripción"
    _p(mundo, 2, "Doble", area="IA", estado="pausado")
    _p(mundo, 3, "Doble", area="IA", estado="pausado")
    with pytest.raises(crud.NoDeNegocio, match="varios proyectos pausados"):
        _correr(crud.perfil("proyecto", "Doble", descripcion="x"))


def test_G7p_crear_un_proyecto_de_ia_no_choca_con_el_nombre_de_uno_pausado_o_cerrado(mundo, gente, noco):
    _p(mundo, 1, "Mismo nombre", area="IA", estado="pausado")
    _p(mundo, 2, "Mismo nombre", area="IA", estado="cerrado")
    nuevo = _crear(mundo, "mismo nombre", "IA", None, noco.persona)
    assert nuevo["nombre"] == "mismo nombre"
    with pytest.raises(db.NombreDeProyectoNoVale) as e:           # ahora sí hay un activo con ese nombre
        _crear(mundo, "MISMO NOMBRE", "IA", None, noco.persona)
    assert e.value.clave == "repetido"


@pytest.mark.parametrize("viejo", ["pausado", "cerrado"])
def test_G7q_reactivar_uno_de_ia_con_el_nombre_de_un_activo_se_rechaza(mundo, gente, viejo):
    _p(mundo, 1, "Mismo nombre", area="IA", estado=viejo)
    _p(mundo, 2, "Mismo nombre", area="IA", estado="activo")
    foto = _foto(mundo)
    with pytest.raises(ValueError, match="ya hay otro proyecto con ese nombre"):
        _editar(1, {"estado": "activo"})
    assert _foto(mundo) == foto


def test_G7q_pausar_o_cerrar_a_uno_de_ia_si_puede_repetir_el_nombre_de_un_activo(mundo, gente):
    _p(mundo, 1, "Mismo nombre", area="IA", estado="activo")
    _p(mundo, 2, "Otro", area="IA", estado="activo")
    _editar(2, {"nombre": "Mismo nombre", "estado": "pausado"})          # renombrar al mismo tiempo que se pausa
    assert (_fila(mundo, 2)["nombre"], _fila(mundo, 2)["estado"]) == ("Mismo nombre", "pausado")


def test_G7q_la_papelera_dice_que_choca_solo_al_que_volveria_activo_con_el_nombre_de_un_activo(mundo, gente):
    """La bandera `choca` de la papelera es la MISMA decisión que `deshacer` (restaurar): un pausado o un cerrado que vuelve
    así no choca por nombre con un activo; uno que vuelve activo, sí."""
    _p(mundo, 1, "Mismo nombre", area="IA", estado="activo")
    _p(mundo, 2, "Mismo nombre", area="IA", estado="pausado", borrado=True)
    _p(mundo, 3, "Mismo nombre", area="IA", estado="cerrado", borrado=True)
    _p(mundo, 4, "Mismo nombre", area="IA", estado="activo", borrado=True)
    for pid in (2, 3, 4):
        mundo.con.execute("INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes) VALUES "
                          "('lucy', 'borrar', 'proyectos', ?, ?)", (pid, json.dumps(_fila(mundo, pid), default=str)))
    mundo.con.commit()
    papelera = {p["id"]: p["choca"] for p in _correr(db.papelera_de_proyectos_y_tareas())["proyectos"]}
    assert papelera == {2: False, 3: False, 4: True}
    # y restaurar hace LO MISMO que la bandera
    _correr(crud.deshacer_borrado("proyectos", 2))
    _correr(crud.deshacer_borrado("proyectos", 3))
    with pytest.raises(ValueError, match="ya hay otro proyecto vivo con ese nombre"):
        _correr(crud.deshacer_borrado("proyectos", 4))


# ═══════════════════════════════════════════════════════════════════════
# Telegram: `crud.crear_proyecto` y la herramienta
# ═══════════════════════════════════════════════════════════════════════

def test_telegram_crea_en_cds_con_el_cliente_y_el_proyecto_toma_su_nombre(mundo, gente, noco):
    nuevo, log_id = _correr(crud.crear_proyecto(None, "CDS", DUENO, 101))
    assert (nuevo["nombre"], nuevo["cliente_noco_id"], nuevo["area"]) == ("Cliente 101", 101, "CDS") and log_id
    otro, _ = _correr(crud.crear_proyecto(None, "ACD", DUENO, "102"))            # el Id como texto de dígitos
    assert otro["cliente_noco_id"] == 102


def test_telegram_la_herramienta_pasa_el_cliente_y_no_manda_nombre_vacio(mundo, gente, noco):
    import cerebro.agente as agente
    acciones: list = []
    r = _correr(agente._ejecutar_herramienta(
        "crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 101}, 1, acciones))
    assert r.startswith("OK") and "Cliente 101" in r, r
    r2 = _correr(agente._ejecutar_herramienta(
        "crear_proyecto", {"nombre": "", "grupo": "ACD", "responsable": "Persona Dos", "cliente_id": 102}, 1, acciones))
    assert r2.startswith("OK"), r2


@pytest.mark.parametrize("args,trozo", [
    ({"grupo": "CDS", "responsable": "Persona Dos"}, "cliente"),
    ({"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 999}, "cliente"),
    ({"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 101, "nombre": "A mano"}, "toma el del cliente"),
])
def test_telegram_sin_cliente_o_con_uno_inventado_o_con_nombre_a_mano_no_crea(mundo, gente, noco, args, trozo):
    import cerebro.agente as agente
    antes = _foto(mundo)
    r = _correr(agente._ejecutar_herramienta("crear_proyecto", args, 1, []))
    assert r.startswith("ERROR") and trozo in r, r
    assert _foto(mundo) == antes


def test_telegram_con_noco_caido_dice_que_no_creo_y_no_escribe(mundo, gente, noco):
    noco.cae = True
    antes = _foto(mundo)
    with pytest.raises(crud.NoDeNegocio, match="Noco no contesta"):
        _correr(crud.crear_proyecto(None, "CDS", DUENO, 101))
    assert _foto(mundo) == antes


def test_telegram_editar_sigue_sin_poder_cambiar_el_cliente(mundo, gente):
    _p(mundo, 1, "Interno", area="IA", cliente=101)
    for columna, valor in (("cliente_nombre", "Hacker"), ("cliente_noco_id", 5)):
        with pytest.raises(ValueError):
            _editar(1, {columna: valor}, actor="lucy")
    assert (_fila(mundo, 1)["cliente_noco_id"], _fila(mundo, 1)["cliente_nombre"]) == (101, "Cliente 101")


# ═══════════════════════════════════════════════════════════════════════
# Los cinco que hoy no tienen cliente: seguir viéndolos, editándolos y cerrándolos como hoy
# ═══════════════════════════════════════════════════════════════════════

def _los_cinco(m):
    """Cinco proyectos con la forma de los que hoy no tienen cliente (medidos por la sala el 18-oct-2026: tres de ACD y
    CDS cerrados o activos, sin cliente). Nombres y números genéricos."""
    return [_p(m, 16, "Interno uno", area="CDS"),
            _p(m, 5, "Interno dos", area="CDS", estado="cerrado"),
            _p(m, 21, "Interno tres", area="ACD"),
            _p(m, 8, "Interno cuatro", area="ACD", estado="cerrado"),
            _p(m, 9, "Interno cinco", area="ACD", estado="cerrado")]


def test_los_cinco_sin_cliente_se_siguen_viendo_en_la_pagina(mundo, gente):
    ids = _los_cinco(mundo)
    for pid in ids:
        html = ver(mundo, p=pid)
        assert _fila(mundo, pid)["nombre"] in html, pid


@pytest.mark.parametrize("pid", [16, 5, 21, 8, 9])
def test_los_cinco_sin_cliente_se_siguen_pudiendo_editar_en_lo_que_no_es_nombre_ni_grupo_nuevo(mundo, gente, pid):
    _los_cinco(mundo)
    _editar(pid, {"descripcion": "Una nota nueva"})
    _editar(pid, {"responsable_chat_id": DUENO})
    _editar(pid, {"carpeta": "Sesiones/Una carpeta"})
    _editar(pid, {"inicio": "2026-10-01", "entrega": "2026-10-30"})
    f = _fila(mundo, pid)
    assert (f["descripcion"], f["carpeta"], f["inicio"]) == ("Una nota nueva", "Sesiones/Una carpeta", "2026-10-01")
    _editar(pid, {"area": "IA"})                                       # y pasarlo a IA, que es lo que Tiziano decide
    assert _fila(mundo, pid)["area"] == "IA"
    _editar(pid, {"nombre": "Ahora se renombra en IA"})
    assert _fila(mundo, pid)["nombre"] == "Ahora se renombra en IA"


@pytest.mark.parametrize("pid,estado", [(16, "activo"), (5, "cerrado"), (21, "activo"), (8, "cerrado"), (9, "cerrado")])
def test_los_cinco_sin_cliente_se_cierran_y_se_reabren_como_hoy(mundo, gente, pid, estado):
    _los_cinco(mundo)
    for nuevo in ("cerrado", "pausado", "activo", "cerrado"):
        _editar(pid, {"estado": nuevo})                                 # reabrir uno sin cliente no choca con nadie
        assert _fila(mundo, pid)["estado"] == nuevo


def test_los_cinco_sin_cliente_se_cierran_tambien_por_la_ruta_del_panel(mundo, gente):
    from _navegador import Navegador
    import web.app as panel
    import web.auth as auth
    _los_cinco(mundo)
    c = Navegador(panel.app, base_url="https://testserver")
    c.cookies.set(panel.COOKIE, auth.crear_token(DUENO, auth.VIDA_SESION))
    r = c.post("/proyectos/16/estado", data={"estado": "cerrado"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" not in r.headers["location"] and _fila(mundo, 16)["estado"] == "cerrado"
    r = c.post("/proyectos/5/estado", data={"estado": "activo"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" not in r.headers["location"] and _fila(mundo, 5)["estado"] == "activo"


def test_los_cinco_sin_cliente_Tiziano_les_pone_cliente_y_toman_su_nombre(mundo, gente, noco):
    """Es como los resuelve él: un cliente por el panel (o pasarlos a IA)."""
    _los_cinco(mundo)
    assert _correr(db.poner_cliente(16, 101, leer_persona=noco.persona)) is True
    assert (_fila(mundo, 16)["nombre"], _fila(mundo, 16)["cliente_noco_id"]) == ("Cliente 101", 101)
    # un cerrado sin cliente toma un cliente que ya tiene un activo: puede (no cuenta)
    assert _correr(db.poner_cliente(5, 101, leer_persona=noco.persona)) is True


def test_los_cinco_sin_cliente_lo_unico_que_no_pueden_hacer_es_lo_que_la_regla_prohibe_a_proposito(mundo, gente):
    """Prohibido a propósito (Tiziano): renombrar a mano en CDS/ACD y volver de la papelera sin cliente."""
    _los_cinco(mundo)
    with pytest.raises(ValueError, match="nombre del proyecto es el del cliente"):
        _editar(16, {"nombre": "A mano"})
    _correr(crud.borrar("proyectos", 21, "x", actor="panel"))
    with pytest.raises(ValueError, match="necesita cliente"):
        _correr(crud.deshacer_borrado("proyectos", 21))
