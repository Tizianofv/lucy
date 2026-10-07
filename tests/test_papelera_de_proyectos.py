"""La Papelera de proyectos y tareas, y restaurar desde la página (Tiziano, 7-oct-2026: A «1 y 2» —
Telegram sigue valiendo y además una papelera en la página con «Restaurar»— y B «2»: un proyecto
borrado se lleva sus tareas y vuelve con ellas).

DÓNDE VIVE: en la pantalla «Papelera» que ya existe (la de los gastos), con dos secciones nuevas
arriba —«Proyectos» y «Tareas»— dibujadas con el mismo estilo de tabla y el mismo botón «Restaurar».
Una sola puerta en el menú, y el aviso «está en la Papelera» tiene adónde mandar.

RESTAURAR no es otra forma de des-borrar: `crud.deshacer_borrado(tabla, id)` busca la huella `borrar`
más nueva de esa fila y la pasa por `crud.deshacer`, la misma función que Telegram llama con el número
de la huella. Estas pruebas exigen que las dos vías dejen EXACTAMENTE lo mismo (tablas y huellas), y
que lo que la pantalla ofrece restaurar sea lo que `deshacer` de verdad deja restaurar.

LO FINGIDO, declarado: la base es SQLite con el SQL de verdad (llaves foráneas encendidas); `db.papelera()`
(la de los gastos, que lee `movimientos`, tabla que esa base no tiene) devuelve vacío. NO se ejercita
Postgres. El botón de Telegram (`acciones/botones.py::al_pulsar`) NO se corre aquí (necesita el bot):
solo se comprueba por el texto del código que su remate lee `crud.tareas_que_se_fueron`, la misma pieza
que sí se prueba."""
from __future__ import annotations

import ast
import re

import pytest

import test_grupos as tg  # pone el entorno antes de importar `config`
from test_grupos import base  # noqa: F401
from test_pagina_proyectos import Mundo, _cliente, _dia, gente, mundo, ver  # noqa: F401
from test_borrar_proyecto_y_grupo import (ESPERADO_HOGAR, ESPERADO_P1, _avisos, _borrada, _casa, _corre, _filas,
                                          _huellas, _json, _sembrar, _texto, _va_a)
from test_grupo_ia import _ROOT
import config
import db.db as db
import test_grupo_ia as g
from acciones import crud


@pytest.fixture
def pap(base, monkeypatch):
    """El mundo de `base` + `db.papelera()` (gastos) vacía + un proyecto de cada clase en la papelera."""
    async def _sin_gastos():
        return []
    monkeypatch.setattr(db, "papelera", _sin_gastos)
    return base


def _papelera(m, **consulta):
    r = _casa(m).get("/papelera", params=consulta)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _foto(m):
    return (_filas(m, "proyectos"), _filas(m, "tareas"),
            [(h["accion"], h["tabla"], h["registro_id"], h["actor"], h["motivo"]) for h in _huellas(m)])


# ── La pantalla ─────────────────────────────────────────────────────────

async def test_la_papelera_lista_proyectos_con_lo_que_se_fue_con_ellos_y_las_tareas_sueltas(pap):
    _sembrar(pap)
    await crud.borrar("proyectos", 1, "x", actor="panel")
    lo = await db.papelera_de_proyectos_y_tareas()
    assert [(p["id"], p["tareas"], p["choca"], p["huella_id"] is not None) for p in lo["proyectos"]] == [
        (1, 3, False, True), (3, 0, False, True)]
    # Las que se fueron CON el proyecto (10, 11, 12) no salen aparte: vuelven con él.
    assert sorted(t["id"] for t in lo["tareas"]) == [13, 17, 18]
    por_que = {t["id"]: t["por_que_no"] for t in lo["tareas"]}
    assert por_que == {13: "proyecto_borrado", 17: "sin_huella", 18: None}
    html = _papelera(pap)
    t = _texto(html)
    assert "Proyectos · 2" in t and "Tareas · 3" in t
    assert "Casa nueva" in t and "3 tareas" in t and "ninguna registrada" in t
    assert t.count("Restaurar") >= 3
    assert re.search(r'name="tabla" value="proyectos"><input type="hidden" name="id" value="1"', html)
    assert "su proyecto está en la papelera: restaura primero el proyecto" in t
    assert "sin registro de cómo se borró: no se restaura desde aquí" in t
    assert "Las que se fueron junto con su proyecto no salen aquí: van con él." in t


def test_la_papelera_dice_que_esto_no_se_vacia_solo_y_es_verdad():
    """La frase «esto no se vacía solo» es cierta: lo único que borra de verdad por antigüedad es
    `db.vaciar_papelera`, y toca solo `movimientos` (y `tools/vaciar_papelera.py` solo llama a esa)."""
    fuente = (_ROOT / "db" / "db.py").read_text(encoding="utf-8")
    vaciar = next(f for f in ast.walk(ast.parse(fuente)) if isinstance(f, ast.AsyncFunctionDef) and f.name == "vaciar_papelera")
    tablas = {m.group(1) for n in ast.walk(vaciar) if isinstance(n, ast.Constant) and isinstance(n.value, str)
              for m in re.finditer(r"DELETE\s+FROM\s+(\w+)", n.value, re.I)}
    assert tablas == {"movimientos"}
    herramienta = (_ROOT / "tools" / "vaciar_papelera.py").read_text(encoding="utf-8")
    assert not re.search(r"DELETE\s+FROM\s+(?!movimientos)", herramienta, re.I)
    assert "esto no se vacía solo" in re.sub(r"\s+", " ", (_ROOT / "web" / "plantillas" / "papelera.html").read_text(encoding="utf-8"))


def test_sin_nada_borrado_la_papelera_sigue_diciendo_que_esta_vacia(pap):
    assert "La papelera está vacía." in _texto(_papelera(pap))


# ── Restaurar: la página y Telegram dejan lo mismo ──────────────────────

def _dos_mundos(monkeypatch):
    """Dos mundos iguales e independientes (SQLite, llaves encendidas), para hacer lo mismo por dos vías."""
    mundos = []
    for _ in range(2):
        m = Mundo()
        m.con.create_function("pg_advisory_xact_lock", 1, lambda x: None)
        m.con.create_function("hashtextextended", 2, lambda a, b: 0)
        m.con.execute("INSERT INTO areas (clave, color, orden) VALUES ('Hogar', '#2f6fb3', 4)")
        m.con.execute("INSERT OR IGNORE INTO areas (clave, color, orden) VALUES ('IA', '#8a4a8f', 3)")
        m.con.commit()
        m.con.execute("PRAGMA foreign_keys = ON")
        mundos.append(m)
    return mundos


@pytest.mark.parametrize("tabla,rid,antes", [("proyectos", 1, None), ("tareas", 15, None), ("tareas", 18, None)])
def test_restaurar_por_la_pagina_y_por_telegram_deja_exactamente_lo_mismo(monkeypatch, gente, tabla, rid, antes):
    a, b = _dos_mundos(monkeypatch)
    async def _sin_gastos():
        return []
    monkeypatch.setattr(db, "papelera", _sin_gastos)
    for m in (a, b):
        monkeypatch.setattr(db, "pool", g._Pool(m.con))
        _sembrar(m)
        _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
        _corre(crud.borrar("tareas", 15, "x", actor="panel"))
    # La vía de la página: POST /papelera/restaurar.
    monkeypatch.setattr(db, "pool", g._Pool(a.con))
    r = _casa(a).post("/papelera/restaurar", data={"tabla": tabla, "id": rid}, follow_redirects=False)
    assert _va_a(r)["hecho"] in ("proyecto_restaurado", "tarea_restaurada")
    # La vía de Telegram: `deshacer` con el número de la huella `borrar` de esa fila.
    monkeypatch.setattr(db, "pool", g._Pool(b.con))
    huella = max(h["id"] for h in _huellas(b, tabla=tabla, registro_id=rid, accion="borrar"))
    _corre(crud.deshacer(huella))
    assert _foto(a) == _foto(b)
    assert not _borrada(a, tabla, rid)


def test_restaurar_un_proyecto_trae_sus_tareas_y_solo_esas_y_el_aviso_dice_lo_comprobado(pap):
    _sembrar(pap)
    _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
    # Una dirección escrita a mano NO fuerza el aviso: el proyecto sigue en la papelera.
    assert not [a for a in _avisos(_papelera(pap, hecho="proyecto_restaurado", id=1)) if "de vuelta" in a]
    q = _va_a(_casa(pap).post("/papelera/restaurar", data={"tabla": "proyectos", "id": 1}, follow_redirects=False))
    assert q == {"ruta": "/papelera", "hecho": "proyecto_restaurado", "id": "1"}
    assert not _borrada(pap, "proyectos", 1) and all(not _borrada(pap, "tareas", t) for t in (10, 11, 12))
    assert _borrada(pap, "tareas", 13), "la que ya estaba borrada de antes no vuelve"
    html = _papelera(pap, hecho="proyecto_restaurado", id=1)
    assert _avisos(html)[0] == ("El proyecto «Casa nueva» ya está de vuelta en Proyectos, con 3 tareas de las que se fueron con él. "
                                "Si su grupo ya no existe, está en «Sin grupo».")
    # y el id de otro proyecto que sigue borrado no dice «de vuelta»
    assert not [a for a in _avisos(_papelera(pap, hecho="proyecto_restaurado", id=3)) if "de vuelta" in a]


def test_restaurar_una_tarea_suelta_y_su_aviso(pap):
    _sembrar(pap)
    _corre(crud.borrar("tareas", 15, "x", actor="panel"))
    assert not [a for a in _avisos(_papelera(pap, hecho="tarea_restaurada", id=15)) if "de vuelta" in a]
    _va_a(_casa(pap).post("/papelera/restaurar", data={"tabla": "tareas", "id": 15}, follow_redirects=False))
    assert not _borrada(pap, "tareas", 15)
    assert _avisos(_papelera(pap, hecho="tarea_restaurada", id=15))[0] == "La tarea «suelta pendiente» ya está de vuelta en Tareas."


def test_lo_que_no_se_puede_restaurar_no_se_restaura_y_se_dice(pap):
    _sembrar(pap)
    _corre(crud.borrar("proyectos", 1, "x", actor="panel"))
    # un proyecto vivo con el mismo nombre del que está en la papelera
    pap.proyecto(30, "casa NUEVA", area="CDS")
    pap.con.commit()
    antes = _foto(pap)
    c = _casa(pap)
    assert _va_a(c.post("/papelera/restaurar", data={"tabla": "proyectos", "id": 1}, follow_redirects=False))["error"] == "restaurar_no_se_pudo"
    # una tarea cuyo proyecto sigue en la papelera
    assert _va_a(c.post("/papelera/restaurar", data={"tabla": "tareas", "id": 13}, follow_redirects=False))["error"] == "restaurar_no_se_pudo"
    # sin huella, no está borrada, no existe, o datos que no valen
    assert _va_a(c.post("/papelera/restaurar", data={"tabla": "tareas", "id": 17}, follow_redirects=False))["error"] == "restaurar_no_esta"
    assert _va_a(c.post("/papelera/restaurar", data={"tabla": "tareas", "id": 15}, follow_redirects=False))["error"] == "restaurar_no_esta"
    for datos in ({"tabla": "tareas", "id": 9999}, {"tabla": "areas", "id": 1}, {"tabla": "tareas", "id": "x"}, {}):
        assert _va_a(c.post("/papelera/restaurar", data=datos, follow_redirects=False))["error"] == "restaurar_no_esta", datos
    assert _foto(pap) == antes                                               # y nada se escribió
    html = _papelera(pap)
    assert "ya hay otro proyecto vivo con ese nombre" in _texto(html)
    assert "No se restauró: sigue en la papelera." in _avisos(_papelera(pap, error="restaurar_no_se_pudo"))[0]


async def test_lo_que_la_pantalla_ofrece_restaurar_es_lo_que_deshacer_deja_restaurar(pap):
    """HERMANOS, de lo real: para CADA fila que la pantalla lista, «tiene botón» ⇔ `deshacer_borrado`
    la restaura. La lista sale de `db.papelera_de_proyectos_y_tareas()`, no de una lista escrita aquí; el
    mundo trae uno de cada motivo para no restaurar (sin huella, proyecto borrado, proyecto cerrado,
    nombre repetido)."""
    _sembrar(pap)
    await crud.borrar("proyectos", 1, "x", actor="panel")
    await crud.borrar("tareas", 14, "x", actor="panel")           # tarea de un proyecto CERRADO
    await crud.borrar("tareas", 20, "x", actor="panel")           # tarea de un proyecto vivo y abierto
    await crud.borrar("tareas", 15, "x", actor="panel")           # suelta
    pap.proyecto(30, "Casa nueva", area="CDS")                    # le quita a «Casa nueva» el derecho de volver
    pap.con.commit()
    lo = await db.papelera_de_proyectos_y_tareas()
    motivos = {t["por_que_no"] for t in lo["tareas"]}
    assert motivos == {None, "sin_huella", "proyecto_borrado", "proyecto_cerrado"}, motivos
    assert any(p["choca"] for p in lo["proyectos"]) and any(p["huella_id"] is not None and not p["choca"] for p in lo["proyectos"])
    for t in lo["tareas"]:
        try:
            await crud.deshacer_borrado("tareas", t["id"])
            restaurada = True
        except ValueError:
            restaurada = False
        assert restaurada == (t["por_que_no"] is None), (t["id"], t["por_que_no"], restaurada)
    for p in lo["proyectos"]:
        try:
            await crud.deshacer_borrado("proyectos", p["id"])
            restaurado = True
        except ValueError:
            restaurado = False
        assert restaurado == (p["huella_id"] is not None and not p["choca"]), (p["id"], restaurado)


# ── Los avisos de borrar dicen la verdad: la tarea que se borra está de verdad en la Papelera ──

def test_una_tarea_borrada_desde_proyectos_sale_en_la_papelera_y_se_restaura_desde_ahi(pap):
    _sembrar(pap)
    c = _casa(pap)
    r = c.post("/proyectos/tarea/20/borrar", follow_redirects=False)
    assert r.status_code == 303 and "hecho=tarea_borrada&borrada=20" in r.headers["location"]
    assert 'está en la <a href="/papelera">Papelera</a>' in ver(pap, p=4, hecho="tarea_borrada", borrada=20)
    html = _papelera(pap)
    assert "de otro proyecto" in _texto(html)
    assert re.search(r'name="tabla" value="tareas"><input type="hidden" name="id" value="20"', html)
    c.post("/papelera/restaurar", data={"tabla": "tareas", "id": 20}, follow_redirects=False)
    assert not _borrada(pap, "tareas", 20)


# ── Telegram y los botones ──────────────────────────────────────────────

def test_el_bot_archiva_un_proyecto_con_sus_tareas_y_lo_dice_con_el_numero_de_la_huella():
    import test_nombre_de_proyecto as t
    b = t.Base()
    p = b.proyecto("Casa")
    b.con.execute("INSERT INTO tareas (id, titulo, proyecto_id) VALUES (1, 'a', ?), (2, 'b', ?)", (p, p))
    r, _ = t._herramienta(b, "archivar", {"tabla": "proyectos", "id": p})
    assert re.fullmatch(r"OK: archivado \(acción #\d+, reversible\)\. Se fueron con él 2 tareas; deshacer lo trae de vuelta con ellas\.", r), r
    assert [f[0] for f in b.con.execute("SELECT borrado_en IS NOT NULL FROM tareas ORDER BY id")] == [1, 1]
    sin = b.proyecto("Vacío")
    r, _ = t._herramienta(b, "archivar", {"tabla": "proyectos", "id": sin})
    assert r.endswith("reversible). No tenía tareas."), r
    b.con.execute("INSERT INTO tareas (id, titulo) VALUES (9, 'suelta')")
    r, _ = t._herramienta(b, "archivar", {"tabla": "tareas", "id": 9})
    assert re.fullmatch(r"OK: archivado \(acción #\d+, reversible\)\.", r), r


def test_deshacer_por_telegram_trae_el_proyecto_con_sus_tareas():
    import test_nombre_de_proyecto as t
    b = t.Base()
    p = b.proyecto("Casa")
    b.con.execute("INSERT INTO tareas (id, titulo, proyecto_id) VALUES (1, 'a', ?), (2, 'b', ?)", (p, p))
    r, _ = t._herramienta(b, "archivar", {"tabla": "proyectos", "id": p})
    log_id = int(re.search(r"acción #(\d+)", r).group(1))
    r, _ = t._herramienta(b, "deshacer", {"accion": log_id})
    assert "2 tareas" in r, r
    assert [f[0] for f in b.con.execute("SELECT borrado_en IS NULL FROM tareas ORDER BY id")] == [1, 1]


def test_el_boton_de_telegram_dice_cuantas_tareas_se_fueron_con_la_misma_pieza():
    fuente = (_ROOT / "acciones" / "botones.py").read_text(encoding="utf-8")
    assert "crud.tareas_que_se_fueron(log_id)" in fuente and 'plan["tabla"] == "proyectos"' in fuente
