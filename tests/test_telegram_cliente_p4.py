"""UN PROYECTO NO SE CREA SIN CLIENTE: pieza 4, Telegram (Tiziano, 18-oct-2026).

Lo que se vigila (numeración del diseño, `disenos/lucy-proyecto-con-cliente/DISENO.md` §2.8, pieza 4):

  G15  `crear_proyecto` en CDS/ACD sin `cliente_id`, o con uno que Noco no devuelve: «No creé el proyecto…» y 0 filas;
  G16  Noco sin contestar: lo mismo, con su causa, y `buscar_cliente` NO lo disfraza de «0 fichas»;
  G17  `buscar_cliente` solo lee (todo pedido que sale es un GET, y no se escribe ni una fila);
  G18  el prompt ya no dice «no mandes cliente», lista la herramienta y dice qué sí hacer;
  G19  `editar` sigue rechazando `cliente_*` (la prueba está en `test_proyecto_con_cliente.py`, aquí se repite por la
       herramienta real).

CAMINO DE PRODUCCIÓN: la herramienta real (`agente._ejecutar_herramienta`) → `crud` → `db` → `noco_lectura` de verdad,
con la base SQLite que ejecuta el texto real de las sentencias y un Noco de mentira que es un cliente HTTP de verdad con
transporte falso (el `Noco` de `test_noco_lectura.py`, con `filtrar=True`: contesta solo las fichas que cumplen el
`where`). El MODELO no corre: sus pasos (buscar, escoger, crear) los hace la prueba con la forma real de lo que devuelven las
herramientas (un JSON de `{id, nombre}`); que el modelo los siga es comportamiento de IA y se vigila con el prompt y con
que la puerta relee la ficha, no con una guarda de lo que escribe.

FRONTERA, dicha una vez: la sintaxis del `where` es la del doble (no una captura de Noco real); SQLite no es Postgres.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otras pruebas cambian `httpx` al importarse)

import test_tarea_a_mano  # noqa: F401  (deja el entorno de pruebas puesto antes de importar `config`)
import acciones.crud as crud
import cerebro.agente as agente
import config
from test_pagina_proyectos import Mundo, gente, mundo  # noqa: F401
from test_noco_lectura import HTTPX, Noco, _apuntar_a_noco

pytestmark = pytest.mark.usefixtures("drive_aparte")      # el alta no habla con Drive aquí (ver conftest.py)

DUENO = config.CHAT_ID_DUENO

# Fichas de Personas de mentira (nombres genéricos). Dos se llaman igual a propósito.
FICHAS = [{"Id": 11, "nombre": "Ana Uno"}, {"Id": 12, "nombre": "Ana Dos"}, {"Id": 13, "nombre": "Beto Tres"},
          {"Id": 14, "nombre": "Cleo Igual"}, {"Id": 15, "nombre": "Cleo Igual"}]


def _correr(coro):
    bucle = asyncio.new_event_loop()
    try:
        return bucle.run_until_complete(coro)
    finally:
        bucle.close()


@pytest.fixture
def noco(monkeypatch):
    return _apuntar_a_noco(monkeypatch, Noco(filas=FICHAS, filtrar=True))


def _herr(nombre, args):
    acciones: list = []
    return _correr(agente._ejecutar_herramienta(nombre, args, 1, acciones)), acciones


def _foto(m) -> dict:
    return {t: [tuple(f) for f in m.con.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("proyectos", "log_acciones", "tareas")}


def _todo_get(noco):
    assert noco.pedidos, "no salió ningún pedido a Noco: la prueba no ejerció nada"
    assert {p.method for p in noco.pedidos} == {"GET"}, [p.method for p in noco.pedidos]


# ═══════════════════════════════════════════════════════════════════════
# G17: buscar_cliente
# ═══════════════════════════════════════════════════════════════════════

def test_G17_buscar_cliente_devuelve_los_candidatos_y_nada_mas(mundo, gente, noco):
    antes = _foto(mundo)
    r, acciones = _herr("buscar_cliente", {"texto": "ana"})
    assert json.loads(r) == [{"id": 11, "nombre": "Ana Uno"}, {"id": 12, "nombre": "Ana Dos"}], r
    assert acciones == [] and _foto(mundo) == antes          # no escribe, no deja huella, no entra al parte
    _todo_get(noco)


def test_H2_buscar_cliente_corta_en_el_tope_y_el_prompt_dice_la_misma_cifra(mundo, gente, monkeypatch):
    import noco_lectura
    muchas = [{"Id": 100 + i, "nombre": f"Mismo {i}"} for i in range(noco_lectura.TOPE_BUSQUEDA)]
    _apuntar_a_noco(monkeypatch, Noco(filas=muchas, filtrar=True))
    assert crud.TOPE_CANDIDATOS_CLIENTE < noco_lectura.TOPE_BUSQUEDA      # la prueba ejerce el corte de verdad
    r, _ = _herr("buscar_cliente", {"texto": "mismo"})
    assert [f["id"] for f in json.loads(r)] == [100 + i for i in range(crud.TOPE_CANDIDATOS_CLIENTE)], r
    assert f"hasta {crud.TOPE_CANDIDATOS_CLIENTE} fichas" in agente.herramientas_del_prompt([])
    assert "{TOPE_CLIENTES}" not in agente.herramientas_del_prompt([])
    monkeypatch.setattr(crud, "TOPE_CANDIDATOS_CLIENTE", 3)
    assert "hasta 3 fichas" in agente.herramientas_del_prompt([])
    assert len(json.loads(_herr("buscar_cliente", {"texto": "mismo"})[0])) == 3


def test_G17_buscar_cliente_sin_coincidencias_no_dice_que_no_existe(mundo, gente, noco):
    r, _ = _herr("buscar_cliente", {"texto": "zzzz"})
    assert r.startswith("0 fichas") and "no prueba que el cliente no exista" in r, r
    assert not r.startswith("ERROR") and r != "[]"
    r2, _ = _herr("buscar_cliente", {})                      # sin texto: tampoco es «hay 0»
    assert r2.startswith("0 fichas"), r2


def test_G16_buscar_cliente_con_noco_caido_lo_dice_y_no_devuelve_cero_fichas(mundo, gente, monkeypatch):
    _apuntar_a_noco(monkeypatch, Noco(falla=HTTPX.ConnectError("sin red")))
    r, _ = _herr("buscar_cliente", {"texto": "ana"})
    assert r.startswith("ERROR") and "Noco no contesta" in r, r
    assert "0 fichas" not in r and not r.strip().startswith("[")


# ═══════════════════════════════════════════════════════════════════════
# G15: crear_proyecto con el Id que devolvió buscar_cliente
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("grupo", ["CDS", "ACD"])
def test_G15_de_punta_a_punta_busca_escoge_y_crea_con_el_nombre_del_cliente(mundo, gente, noco, grupo):
    candidatos = json.loads(_herr("buscar_cliente", {"texto": "beto"})[0])
    assert candidatos == [{"id": 13, "nombre": "Beto Tres"}]
    r, acciones = _herr("crear_proyecto", {"grupo": grupo, "responsable": "Persona Dos",
                                           "cliente_id": candidatos[0]["id"]})
    assert r.startswith("OK") and "«Beto Tres»" in r, r              # la respuesta dice el nombre que quedó
    f, = [dict(x) for x in mundo.con.execute("SELECT * FROM proyectos")]
    assert (f["nombre"], f["cliente_noco_id"], f["cliente_nombre"], f["area"]) == ("Beto Tres", 13, "Beto Tres", grupo)
    assert len(acciones) == 1
    _todo_get(noco)


def test_G15_el_id_puede_venir_como_texto_de_digitos(mundo, gente, noco):
    r, _ = _herr("crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": " 12 "})
    assert r.startswith("OK") and "Ana Dos" in r, r


@pytest.mark.parametrize("grupo", ["CDS", "ACD"])
@pytest.mark.parametrize("args_cliente", [{}, {"cliente_id": None}, {"cliente_id": ""}, {"cliente_id": "   "}])
def test_G15_sin_cliente_no_crea_y_dice_que_falta_y_por_donde_buscarlo(mundo, gente, noco, grupo, args_cliente):
    antes = _foto(mundo)
    r, acciones = _herr("crear_proyecto", {"grupo": grupo, "responsable": "Persona Dos", **args_cliente})
    assert r.startswith("ERROR: No creé el proyecto: falta el cliente") and "buscar_cliente" in r, r
    assert _foto(mundo) == antes and acciones == []


@pytest.mark.parametrize("cliente_id", [999, "999", "abc", True, 0, -3, 12.5])
def test_G15_un_id_que_noco_no_devuelve_no_crea_y_no_dice_que_falta(mundo, gente, noco, cliente_id):
    antes = _foto(mundo)
    r, acciones = _herr("crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": cliente_id})
    assert r.startswith("ERROR: No creé el proyecto: ese `cliente_id` no es una ficha") and "buscar_cliente" in r, r
    assert "falta el cliente" not in r
    assert _foto(mundo) == antes and acciones == []


def test_G16_noco_caido_al_crear_no_crea_y_dice_que_es_noco(mundo, gente, monkeypatch):
    _apuntar_a_noco(monkeypatch, Noco(falla=HTTPX.ConnectError("sin red")))
    antes = _foto(mundo)
    r, acciones = _herr("crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 11})
    assert r == "ERROR: No creé el proyecto: Noco no contesta, inténtalo luego.", r
    assert "falta el cliente" not in r and _foto(mundo) == antes and acciones == []


def test_G15_ia_nace_sin_cliente_con_nombre_libre_y_sin_tocar_noco(mundo, gente, noco):
    r, _ = _herr("crear_proyecto", {"nombre": "Algo interno", "grupo": "IA", "responsable": "Persona Dos"})
    assert r.startswith("OK") and "«Algo interno»" in r, r
    f, = [dict(x) for x in mundo.con.execute("SELECT * FROM proyectos")]
    assert (f["cliente_noco_id"], f["cliente_nombre"]) == (None, None)
    assert noco.pedidos == [], "IA no necesita cliente: no tenía por qué preguntarle a Noco"


def test_G15_un_nombre_a_mano_en_cds_se_rechaza_y_dice_que_el_proyecto_toma_el_del_cliente(mundo, gente, noco):
    antes = _foto(mundo)
    r, _ = _herr("crear_proyecto", {"nombre": "A mano", "grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 11})
    assert r.startswith("ERROR") and "toma el del cliente" in r, r
    assert _foto(mundo) == antes


def test_G15_cliente_con_proyecto_activo_no_crea_otro_y_ofrece_agregar_la_tarea(mundo, gente, noco):
    primero, _ = _herr("crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 13})
    assert primero.startswith("OK"), primero
    antes = _foto(mundo)
    r, acciones = _herr("crear_proyecto", {"grupo": "ACD", "responsable": "Persona Dos", "cliente_id": 13})
    assert r.startswith("ERROR: No creé el proyecto: el cliente tiene el proyecto #") and "«Beto Tres»" in r, r
    assert "créala dentro de ese proyecto" in r, r
    assert _foto(mundo) == antes and acciones == []


def test_G19_editar_sigue_sin_poder_cambiar_el_cliente_por_la_herramienta(mundo, gente, noco):
    r, _ = _herr("crear_proyecto", {"grupo": "CDS", "responsable": "Persona Dos", "cliente_id": 11})
    assert r.startswith("OK"), r
    pid, = [x[0] for x in mundo.con.execute("SELECT id FROM proyectos")]
    antes = _foto(mundo)
    for columna, valor in (("cliente_nombre", "Otro"), ("cliente_noco_id", 12)):
        r2, _ = _herr("editar", {"tabla": "proyectos", "id": pid, "cambios": {columna: valor}})
        assert r2.startswith("ERROR"), (columna, r2)
    assert _foto(mundo) == antes


# ═══════════════════════════════════════════════════════════════════════
# G18: el prompt
# ═══════════════════════════════════════════════════════════════════════

def _bloque(nombre: str, texto: str | None = None) -> str:
    texto = agente.HERRAMIENTAS if texto is None else texto
    inicio = texto.index(f"\n· {nombre}  ") + 3
    resto = texto[inicio:]
    fin = resto.find("\n· ")
    return resto if fin < 0 else resto[:fin]


def test_G18_el_prompt_ya_no_prohibe_el_cliente_y_dice_que_si_hacer():
    texto = agente.herramientas_del_prompt([{"clave": "CDS", "color": "#1"}, {"clave": "IA", "color": "#2"}])
    assert "No mandes \"cliente\"" not in texto and "no mandes \"cliente\"" not in texto
    crear = _bloque("crear_proyecto", texto)
    assert '"cliente_id"' in crear and "buscar_cliente" in crear
    assert "IA" in crear and "agregar la tarea" in crear         # IA con nombre libre; cliente ocupado: agregar la tarea
    buscar = _bloque("buscar_cliente", texto)
    assert '"texto"' in buscar and "numerada" in buscar and "igual" in buscar    # 2+ fichas: lista numerada; homónimos


def test_G18_el_grupo_sin_cliente_del_prompt_sale_de_la_fuente_y_no_esta_tecleado(monkeypatch):
    """Entrada inventada: con otro grupo técnico, el prompt dice ése y ya no habla de «IA» en el bloque."""
    import db.db as db
    monkeypatch.setattr(db, "AREA_TECNICA", "Laboratorio")
    crear = _bloque("crear_proyecto", agente.herramientas_del_prompt([{"clave": "Laboratorio", "color": "#1"}]))
    assert '"Laboratorio"' in crear and "IA" not in crear.replace("PREGUNTÁNTASELO", ""), crear
    assert "CDS" not in crear and "ACD" not in crear
    texto = agente.herramientas_del_prompt([{"clave": "Laboratorio", "color": "#1"}])
    assert '"Laboratorio"' in texto and "{GRUPO_SIN_CLIENTE}" not in texto


def test_G18_toda_herramienta_que_el_despachador_atiende_esta_descrita_en_el_prompt():
    """Hermanos, sacados de lo real: la lista de la que sale el mensaje «no existe la herramienta» (que otra prueba,
    `test_herramientas.py`, obliga a coincidir con el despachador). Cada una tiene su renglón `· nombre  ` en el prompt."""
    for nombre in agente.HERRAMIENTAS_QUE_HAY:
        assert re.search(rf"\n· {nombre}  ", agente.HERRAMIENTAS), f"{nombre} no está descrita en el prompt"
    assert "buscar_cliente" in agente.HERRAMIENTAS_QUE_HAY


# ═══════════════════════════════════════════════════════════════════════
# G17 (por la guarda general): buscar_cliente sale de la parte pública del lector
# ═══════════════════════════════════════════════════════════════════════

def test_G17_buscar_cliente_solo_usa_lo_publico_del_lector_de_noco():
    import ast
    import inspect
    arbol = ast.parse(inspect.getsource(crud.buscar_cliente).lstrip())
    usos = {n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "noco_lectura"}
    assert usos == {"buscar_personas", "NocoNoContesta"}, usos
