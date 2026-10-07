"""Borrar cualquier proyecto o grupo, con confirmación (Tiziano, 7-oct-2026: «ponlo poder
borrar cualquier proyecto o grupo, con confirmación»; B2: «se van con él y vuelven con él»;
C: «sus proyectos y tareas se borran igual que si los hubieras borrado a mano, y el grupo
desaparece. Lo que restaures después vuelve a «Sin grupo»»).

QUÉ SIGNIFICA «BORRAR» (medido y leído el 7-oct-2026):
  · UN PROYECTO: `borrado_en` puesto + sus tareas VIVAS con él, cada una con su huella
    `borrar`; la huella del proyecto guarda en `despues.tareas_con_el` cuáles se fueron en
    ESE acto. Es la misma función (`crud.borrar`) para el panel, Telegram y los botones.
  · UN GRUPO: lo vivo (proyectos con sus tareas, tareas sueltas) se borra igual; a todo lo que
    nombra el grupo (también lo ya borrado) se le pone `area = NULL`; se borra la fila de
    `areas`; una huella `borrar` de tabla `areas` guarda nombre, color y orden.
  · RESTAURAR: `crud.deshacer` (Telegram) y la Papelera de la página (`crud.deshacer_borrado`,
    que busca la huella y llama a `deshacer`) dejan EXACTAMENTE lo mismo.

QUÉ VIGILA ESTE ARCHIVO, con el SQL ejecutándose de verdad en SQLite (llaves foráneas
encendidas, como `tests/test_grupos.py`) y las rutas y la página reales: el borrado y la
restauración; el «Sí» atado a la pregunta (cada cuenta de la pregunta, una por una); que nada
quede apuntando a un grupo borrado y que una tabla nueva que apunte a uno lo rechace; que los
avisos salgan de la base; que quien solo ve o no entró no llame a las rutas.

LO FINGIDO, declarado (igual que en `crear_grupo`/`quitar_grupo`): (1) `pg_advisory_xact_lock` y
`hashtextextended` son funciones vacías: se prueba el ORDEN (el bloqueo es lo primero que
corre), no que Postgres ponga a dos pedidos en fila; (2) SQLite de prueba NO deshace la
transacción cuando una excepción sale (psycopg sí): lo que se prueba es que sale la excepción
correcta y que, al deshacer a mano lo escrito, no queda nada; (3) la carrera «otro pedido le
mete algo al grupo/proyecto entre la pregunta y el borrado» se imita con un gancho, no con dos
conexiones; (4) el SQLSTATE «23503» y el punto de guardado se prueban con dobles que lanzan lo
mismo que psycopg (`tests/test_grupos.py` comprueba el reconocimiento con las clases reales).
NO ejercita Postgres: nadie ha comprobado aquí el bloqueo, las llaves ni el SQLSTATE en una
base de verdad."""
from __future__ import annotations

import ast
import asyncio
import json
import re
from urllib.parse import parse_qsl, urlsplit

import pytest

import test_grupos as tg  # pone el entorno antes de importar `config`
from test_grupos import _ViolacionDeLlaveForanea, LOCK, base  # noqa: F401
from test_pagina_proyectos import _cliente, _dia, gente, mundo  # noqa: F401
from test_proyectos_solo_ver import cliente as cliente_de_sesion
from test_grupo_ia import _ROOT
import config
import db.db as db
from acciones import crud


def _huella_de_antes(m, tabla: str, rid: int, accion="borrar", despues=None):
    """La huella que dejaría un borrado hecho a mano antes de este cambio."""
    fila = dict(m.con.execute(f"SELECT * FROM {tabla} WHERE id = ?", (rid,)).fetchone())
    m.con.execute(
        "INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, despues) VALUES ('lucy', ?, ?, ?, ?, ?)",
        (accion, tabla, rid, json.dumps(fila, default=str), json.dumps(despues) if despues else None))


def _sembrar(m):
    """Un grupo «Hogar» con de todo, y otro grupo con lo suyo (que no se debe tocar)."""
    m.proyecto(1, "Casa nueva", area="Hogar")
    m.proyecto(2, "Viejo", area="Hogar", estado="cerrado")
    m.proyecto(3, "Ya borrado", area="Hogar", borrado=True)
    m.proyecto(4, "De otro grupo", area="CDS")
    m.tarea(10, "pendiente en 1", proyecto=1)
    m.tarea(11, "hecha en 1", proyecto=1, estado="hecha", completado=_dia(-1))
    m.tarea(12, "otro estado en 1", proyecto=1, estado="descartado")
    m.tarea(13, "borrada de antes en 1", proyecto=1, borrada=True)
    m.tarea(14, "en el cerrado", proyecto=2, estado="hecha", completado=_dia(-1))
    m.tarea(15, "suelta pendiente", area="Hogar")
    m.tarea(16, "suelta hecha", area="Hogar", estado="hecha", completado=_dia(-1))
    m.tarea(17, "suelta borrada de antes, sin huella", area="Hogar", borrada=True)
    m.tarea(18, "suelta borrada de antes, con huella", area="Hogar", borrada=True)
    m.tarea(20, "de otro proyecto", proyecto=4)
    m.tarea(21, "suelta de CDS", area="CDS")
    m.con.commit()
    _huella_de_antes(m, "tareas", 13)
    _huella_de_antes(m, "tareas", 18)
    _huella_de_antes(m, "proyectos", 3)
    m.con.commit()


def _filas(m, tabla):
    return {f["id"]: dict(f) for f in m.con.execute(f"SELECT * FROM {tabla} ORDER BY id")}


def _borrada(m, tabla, rid):
    return _filas(m, tabla)[rid]["borrado_en"] is not None


def _huellas(m, **filtro):
    sql, params = "SELECT * FROM log_acciones", []
    if filtro:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in filtro)
        params = list(filtro.values())
    return [dict(f) for f in m.con.execute(sql + " ORDER BY id", params)]


def _json(x):
    return db._json_de(x)


def _corre(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════════
# 1. Borrar un proyecto: se lleva sus tareas, y vuelve con ellas
# ═══════════════════════════════════════════════════════════════════════

async def test_borrar_un_proyecto_se_lleva_sus_tareas_vivas_y_solo_esas(base):
    _sembrar(base)
    antes_t = _filas(base, "tareas")
    log_id = await crud.borrar("proyectos", 1, "prueba", actor="panel")
    assert _borrada(base, "proyectos", 1)
    # Las vivas de ese proyecto (10, 11, 12) se fueron con él, de cualquier estado…
    assert all(_borrada(base, "tareas", t) for t in (10, 11, 12))
    # …la que ya estaba borrada de antes NO cambia (ni su fecha ni su huella), y nada más se mueve.
    ahora_t = _filas(base, "tareas")
    assert ahora_t[13] == antes_t[13]
    for t in (14, 15, 16, 17, 18, 20, 21):
        assert ahora_t[t] == antes_t[t], t
    assert not _borrada(base, "proyectos", 4) and not _borrada(base, "proyectos", 2)
    # Huellas: una por tarea que se fue (con su antes) y la del proyecto, al final, con la lista.
    h = _huellas(base, accion="borrar", actor="panel")
    assert [(x["tabla"], x["registro_id"]) for x in h] == [("tareas", 10), ("tareas", 11), ("tareas", 12), ("proyectos", 1)]
    assert h[-1]["id"] == log_id and _json(h[-1]["despues"]) == {"tareas_con_el": [10, 11, 12]}
    assert _json(h[0]["antes"])["titulo"] == "pendiente en 1"
    assert await crud.tareas_que_se_fueron(log_id) == 3


async def test_un_proyecto_cerrado_o_sin_tareas_tambien_se_borra(base):
    _sembrar(base)
    assert await crud.borrar("proyectos", 2, "x", actor="panel") is not None        # cerrado, con una tarea
    base.proyecto(9, "Vacío", area="Hogar")
    base.con.commit()
    log = await crud.borrar("proyectos", 9, "x", actor="panel")
    assert _borrada(base, "proyectos", 9) and _json(_huellas(base, id=log)[0]["despues"]) == {"tareas_con_el": []}
    assert await crud.tareas_que_se_fueron(log) == 0


async def test_deshacer_el_proyecto_devuelve_sus_tareas_y_solo_esas(base):
    _sembrar(base)
    log_id = await crud.borrar("proyectos", 1, "prueba", actor="panel")
    que = await crud.deshacer(log_id)
    assert que == "lo que había archivado y 3 tareas que se fueron con él"
    assert not _borrada(base, "proyectos", 1)
    assert all(not _borrada(base, "tareas", t) for t in (10, 11, 12))
    assert _borrada(base, "tareas", 13), "la que estaba borrada de antes NO vuelve"
    assert _borrada(base, "tareas", 17) and _borrada(base, "tareas", 18)
    # cada una que volvió deja su huella `deshacer`
    assert sorted(h["registro_id"] for h in _huellas(base, accion="deshacer", tabla="tareas")) == [10, 11, 12]


async def test_un_deshacer_viejo_no_trae_lo_que_se_borro_aparte_despues(base):
    """La huella del proyecto devuelve las tareas que se fueron EN ESE ACTO. Si después la persona
    borra una a mano (huella más nueva), repetir el deshacer viejo NO la trae de vuelta."""
    _sembrar(base)
    log_id = await crud.borrar("proyectos", 1, "x", actor="panel")
    await crud.deshacer(log_id)
    await crud.borrar("tareas", 10, "a mano", actor="panel")
    que = await crud.deshacer(log_id)
    assert _borrada(base, "tareas", 10), "la que se borró aparte, después, no vuelve con un deshacer viejo"
    assert not _borrada(base, "proyectos", 1) and que == "lo que había archivado"


async def test_restaurar_una_tarea_cuyo_proyecto_sigue_borrado_se_rechaza_y_lo_dice(base):
    _sembrar(base)
    await crud.borrar("proyectos", 1, "x", actor="panel")
    huella_tarea = _huellas(base, tabla="tareas", registro_id=10, accion="borrar")[0]["id"]
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"))
    with pytest.raises(ValueError) as e:
        await crud.deshacer(huella_tarea)
    assert str(e.value) == ("No lo deshice: su proyecto está en la papelera. Restaura primero el proyecto: "
                            "con él vuelven sus tareas.")
    assert (_filas(base, "tareas"), _filas(base, "proyectos")) == antes            # nada se escribió


async def test_telegram_y_el_panel_borran_un_proyecto_igual(base, monkeypatch):
    """Una sola función: el bot (`actor='lucy'`, sin `esperado`) y el panel (`actor='panel'`,
    con `esperado`) dejan lo mismo, salvo quién lo hizo."""
    _sembrar(base)
    base.proyecto(5, "Gemelo", area="Hogar")
    base.tarea(50, "t", proyecto=5)
    base.tarea(51, "u", proyecto=5, estado="hecha", completado=_dia(-1))
    base.proyecto(6, "Gemelo B", area="Hogar")
    base.tarea(60, "t", proyecto=6)
    base.tarea(61, "u", proyecto=6, estado="hecha", completado=_dia(-1))
    base.con.commit()
    a = await crud.borrar("proyectos", 5, "por el bot")
    b = await crud.borrar("proyectos", 6, "por el panel", actor="panel",
                          esperado={"tareas_pendientes": 1, "tareas_hechas": 1, "tareas_otras": 0})
    ha, hb = _huellas(base, id=a)[0], _huellas(base, id=b)[0]
    assert (ha["actor"], hb["actor"]) == ("lucy", "panel")
    assert len(_json(ha["despues"])["tareas_con_el"]) == len(_json(hb["despues"])["tareas_con_el"]) == 2
    assert all(_borrada(base, "tareas", t) for t in (50, 51, 60, 61))


def test_toda_via_que_borra_un_proyecto_pasa_por_crud_borrar():
    """Hermanos, sacados del código: quién escribe `borrado_en` de `proyectos`. Ningún `.py`
    fuera de `crud._borrar_proyecto_en` lo pone (el bot y los botones llaman a `crud.borrar`)."""
    escritores = set()
    from test_buzon_que_no_se_ve import _py_en_disco      # LA puerta de los barridos del repo
    for archivo in _py_en_disco(_ROOT):
        if "tests" in archivo.relative_to(_ROOT).parts:
            continue
        for fn in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(
                        r"UPDATE\s+proyectos\s+SET\s+borrado_en", n.value, re.I):
                    escritores.add(fn.name)
    assert escritores == {"_borrar_proyecto_en"}, escritores
    # y `crud.borrar`, genérico, manda los proyectos allí (no hay un segundo camino)
    cuerpo = ast.get_source_segment((_ROOT / "acciones" / "crud.py").read_text(encoding="utf-8"),
                                    next(f for f in ast.walk(ast.parse((_ROOT / "acciones" / "crud.py").read_text(encoding="utf-8")))
                                         if isinstance(f, ast.AsyncFunctionDef) and f.name == "borrar"))
    assert 'if tabla == "proyectos":' in cuerpo and "_borrar_proyecto_en(" in cuerpo


# ── El «Sí» atado a la pregunta (proyecto) ─────────────────────────────

ESPERADO_P1 = {"tareas_pendientes": 1, "tareas_hechas": 1, "tareas_otras": 1}


async def test_el_si_vale_solo_para_lo_que_la_pregunta_dijo_proyecto(base):
    _sembrar(base)
    medido = await db.contenido_de_proyecto(1)
    assert {k: medido[k] for k in db.CLAVES_DE_CONTENIDO_DE_PROYECTO} == ESPERADO_P1
    assert await crud.borrar("proyectos", 1, "x", actor="panel", esperado=dict(ESPERADO_P1)) is not None


@pytest.mark.parametrize("clave", db.CLAVES_DE_CONTENIDO_DE_PROYECTO)
@pytest.mark.parametrize("cambio", [1, -1, None])
async def test_cada_cuenta_de_la_pregunta_esta_atada_al_si_proyecto(base, clave, cambio):
    """Una por una: si CUALQUIER cuenta de la pregunta no es la de ahora (o falta), no se borra
    nada y sale `CambioAlBorrar` con lo de ahora."""
    _sembrar(base)
    pedido = dict(ESPERADO_P1)
    pedido[clave] = None if cambio is None else pedido[clave] + cambio
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base))
    with pytest.raises(crud.CambioAlBorrar) as e:
        await crud.borrar("proyectos", 1, "x", actor="panel", esperado=pedido)
    assert {k: e.value.contenido[k] for k in db.CLAVES_DE_CONTENIDO_DE_PROYECTO} == ESPERADO_P1
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base)) == antes


async def test_si_entre_la_pregunta_y_el_si_aparece_una_tarea_no_se_borra(base):
    _sembrar(base)
    pregunta = {k: (await db.contenido_de_proyecto(1))[k] for k in db.CLAVES_DE_CONTENIDO_DE_PROYECTO}
    base.tarea(99, "apareció después de la pregunta", proyecto=1)
    base.con.commit()
    with pytest.raises(crud.CambioAlBorrar):
        await crud.borrar("proyectos", 1, "x", actor="panel", esperado=pregunta)
    assert not _borrada(base, "proyectos", 1) and not _borrada(base, "tareas", 99)


async def test_dos_si_sobre_el_mismo_proyecto_borran_una_vez(base):
    _sembrar(base)
    primero = await crud.borrar("proyectos", 1, "x", actor="panel", esperado=dict(ESPERADO_P1))
    segundo = await crud.borrar("proyectos", 1, "x", actor="panel", esperado=dict(ESPERADO_P1))
    assert primero is not None and segundo is None
    assert len(_huellas(base, tabla="proyectos", accion="borrar", actor="panel")) == 1


async def test_el_update_del_proyecto_es_el_que_decide_quien_borra(base):
    """Dos pedidos que leyeron el proyecto vivo: el segundo `UPDATE ... borrado_en IS NULL` no
    encuentra nada y no escribe ninguna huella (la guarda no es el `SELECT` de antes)."""
    _sembrar(base)
    fila = dict(base.con.execute("SELECT * FROM proyectos WHERE id = 1").fetchone())
    async with db.pool.connection() as conn:
        cur = conn.cursor(row_factory=object)
        uno = await crud._borrar_proyecto_en(conn, cur, fila, motivo="a", bandeja_id=None, actor="panel")
        dos = await crud._borrar_proyecto_en(conn, cur, fila, motivo="b", bandeja_id=None, actor="panel")
    assert uno is not None and dos is None
    # la huella vieja de «Ya borrado» (3) más UNA del proyecto 1: la segunda lectura no escribió otra
    assert [(h["actor"], h["registro_id"]) for h in _huellas(base, tabla="proyectos", accion="borrar")] == [("lucy", 3), ("panel", 1)]


async def test_una_tarea_que_entra_mientras_se_borra_el_proyecto_se_va_y_queda_en_la_huella(base, monkeypatch):
    """Frontera dicha: lo que otro pedido meta en el proyecto ENTRE la medida de la pregunta y el
    borrado se va también (recuperable) y la huella la lista; nunca queda viva dentro de un proyecto
    borrado sin que se sepa."""
    _sembrar(base)
    original = crud._registrar
    metida = {"ya": False}

    async def _registrar_y_cuela(conn, **k):
        if not metida["ya"] and k.get("tabla") == "tareas":
            metida["ya"] = True
            base.con.execute("INSERT INTO tareas (id, titulo, estado, proyecto_id, bandeja_id) "
                             "VALUES (77, 'colada', 'pendiente', 1, 9077)")
        return await original(conn, **k)
    monkeypatch.setattr(crud, "_registrar", _registrar_y_cuela)
    log = await crud.borrar("proyectos", 1, "x", actor="panel")
    assert _borrada(base, "tareas", 77)
    assert 77 in _json(_huellas(base, id=log)[0]["despues"])["tareas_con_el"]


# ═══════════════════════════════════════════════════════════════════════
# 2. Borrar un grupo con cosas
# ═══════════════════════════════════════════════════════════════════════

ESPERADO_HOGAR = {"proyectos_abiertos": 1, "proyectos_cerrados": 1, "proyectos_papelera": 1,
                  "tareas_pendientes": 1, "tareas_hechas": 1, "tareas_otras": 0, "tareas_papelera": 2,
                  "tareas_en_proyectos_pendientes": 1, "tareas_en_proyectos_hechas": 2,
                  "tareas_en_proyectos_otras": 1}


async def test_la_pregunta_del_grupo_mide_todo_lo_que_se_iria(base):
    _sembrar(base)
    c = await db.contenido_para_borrar_grupo("Hogar")
    assert {k: c[k] for k in db.CLAVES_DE_CONTENIDO_DE_GRUPO} == ESPERADO_HOGAR
    assert c["total"] == sum(ESPERADO_HOGAR.values())
    assert await db.contenido_para_borrar_grupo("No existe") is None


async def test_borrar_un_grupo_con_cosas_borra_lo_vivo_como_a_mano_y_el_grupo_desaparece(base):
    _sembrar(base)
    otros = {t: _filas(base, "tareas")[t] for t in (20, 21)}
    grupo_antes = dict(base.con.execute("SELECT * FROM areas WHERE clave = 'Hogar'").fetchone())
    r = await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    assert r == {"proyectos": 2, "tareas": 6, "ya_borradas": 3}
    assert "Hogar" not in [f[0] for f in base.con.execute("SELECT clave FROM areas")]
    # lo vivo, borrado (proyectos 1 y 2; sus tareas 10, 11, 12, 14; las sueltas 15 y 16)…
    for t in (10, 11, 12, 14, 15, 16):
        assert _borrada(base, "tareas", t), t
    assert _borrada(base, "proyectos", 1) and _borrada(base, "proyectos", 2)
    # …lo ya borrado de antes sigue borrado (misma fecha), y NADA queda nombrando el grupo
    assert _filas(base, "tareas")[13]["borrado_en"] == "2026-09-01T00:00:00+00:00"
    assert all(_filas(base, tabla)[i]["area"] is None
               for tabla, ids in (("proyectos", (1, 2, 3)), ("tareas", (15, 16, 17, 18))) for i in ids)
    assert base.con.execute("SELECT count(*) FROM proyectos WHERE area = 'Hogar'").fetchone()[0] == 0
    assert base.con.execute("SELECT count(*) FROM tareas WHERE area = 'Hogar'").fetchone()[0] == 0
    # lo de otro grupo no se tocó
    assert {t: _filas(base, "tareas")[t] for t in (20, 21)} == otros
    assert not _borrada(base, "proyectos", 4) and _filas(base, "proyectos")[4]["area"] == "CDS"
    # La huella de cada cosa borrada ahora guarda de qué grupo venía (su `antes`)…
    for h in _huellas(base, accion="borrar", tabla="proyectos"):
        if h["registro_id"] in (1, 2):
            assert _json(h["antes"])["area"] == "Hogar"
    sueltas = {h["registro_id"]: _json(h["antes"])["area"] for h in _huellas(base, accion="borrar", tabla="tareas")
               if h["registro_id"] in (15, 16)}
    assert sueltas == {15: "Hogar", 16: "Hogar"}
    # …lo que ya estaba borrado deja una huella `editar` (su grupo se fue) y no una `borrar` nueva…
    editadas = {(h["tabla"], h["registro_id"]) for h in _huellas(base, accion="editar")}
    # (la 13 estaba borrada pero DENTRO del proyecto 1, sin grupo propio: no nombraba el grupo)
    assert editadas == {("proyectos", 3), ("tareas", 17), ("tareas", 18)}
    # …y el grupo, con nombre, color y orden, y los ids que se fueron
    h, = _huellas(base, tabla="areas")
    assert (h["accion"], h["actor"], h["registro_id"]) == ("borrar", "panel", 0)
    assert _json(h["antes"]) == grupo_antes
    d = _json(h["despues"])
    assert d["proyectos"] == [1, 2] and sorted(d["tareas"]) == [10, 11, 12, 14, 15, 16]
    assert d["ya_en_la_papelera"] == {"proyectos": [3], "tareas": [17, 18]}


async def test_lo_que_se_restaura_de_un_grupo_borrado_vuelve_a_sin_grupo(base):
    _sembrar(base)
    await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    log_p = _huellas(base, tabla="proyectos", registro_id=1, accion="borrar")[0]["id"]
    log_s = _huellas(base, tabla="tareas", registro_id=15, accion="borrar")[0]["id"]
    await crud.deshacer(log_p)
    await crud.deshacer(log_s)
    p = _filas(base, "proyectos")[1]
    assert p["borrado_en"] is None and p["area"] is None                       # vivo, en «Sin grupo»
    assert _filas(base, "tareas")[15]["area"] is None and not _borrada(base, "tareas", 15)
    assert all(not _borrada(base, "tareas", t) for t in (10, 11, 12))          # y sus tareas con él
    from test_pagina_proyectos import ver
    html = ver(base, sin_grupo=1)
    assert "suelta pendiente" in html
    assert re.search(r'data-g="sin-grupo"[\s\S]*?Casa nueva', ver(base))


def test_la_huella_de_lo_que_ya_estaba_borrado_se_rechaza_al_deshacer_con_el_grupo_que_no_existe(base):
    """La huella `editar` que `borrar_grupo` deja para lo que ya estaba en la papelera, repetida
    tal cual en el mundo de `deshacer` de `tests/test_grupos.py` (SQLite con el `UPDATE ...
    jsonb_populate_record` emulado, la llave foránea encendida): `deshacer` dice con verdad
    que el grupo ya no existe y no escribe nada."""
    _sembrar(base)
    _corre(crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel"))
    mia = _huellas(base, tabla="proyectos", registro_id=3, accion="editar")[0]
    assert _json(mia["antes"]) == {"area": "Hogar"} and _json(mia["despues"]) == {"area": None}
    t, b = tg._mundo_de_deshacer()
    b.con.execute("INSERT INTO log_acciones (id, actor, accion, tabla, registro_id, antes, despues) "
                  "VALUES (8, 'panel', 'editar', 'proyectos', 1, ?, ?)", (mia["antes"], mia["despues"]))
    b.con.execute("UPDATE proyectos SET area = NULL")
    t._correr(b, lambda: db.quitar_grupo("Hogar"))
    e = t._rechazo(b, lambda: crud.deshacer(8))
    assert e is not None and str(e) == "No lo deshice: el grupo «Hogar» ya no existe."
    assert b.con.execute("SELECT area FROM proyectos WHERE id = 1").fetchone()[0] is None


async def test_el_grupo_fijo_no_se_borra_ni_con_cosas(base):
    base.proyecto(7, "De IA", area="IA")
    base.tarea(70, "suelta de IA", area="IA")
    base.con.commit()
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"), [f[0] for f in base.con.execute("SELECT clave FROM areas")])
    for esperado in (None, {}):
        with pytest.raises(db.GrupoNoSeQuita) as e:
            await crud.borrar_grupo(db.AREA_TECNICA, esperado, actor="panel")
        assert e.value.clave == "fijo"
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), [f[0] for f in base.con.execute("SELECT clave FROM areas")]) == antes
    assert _huellas(base) == []
    # …pero SUS proyectos sí se pueden borrar, uno por uno
    assert await crud.borrar("proyectos", 7, "x", actor="panel") is not None


async def test_un_grupo_que_no_existe_se_dice(base):
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await crud.borrar_grupo("Fantasma", {}, actor="panel")
    assert e.value.clave == "no_existe"


@pytest.mark.parametrize("clave", db.CLAVES_DE_CONTENIDO_DE_GRUPO)
@pytest.mark.parametrize("cambio", [1, None])
async def test_cada_cuenta_de_la_pregunta_esta_atada_al_si_grupo(base, clave, cambio):
    _sembrar(base)
    pedido = dict(ESPERADO_HOGAR)
    pedido[clave] = None if cambio is None else pedido[clave] + cambio
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
             [f[0] for f in base.con.execute("SELECT clave FROM areas")])
    with pytest.raises(crud.CambioAlBorrar) as e:
        await crud.borrar_grupo("Hogar", pedido, actor="panel")
    assert {k: e.value.contenido[k] for k in db.CLAVES_DE_CONTENIDO_DE_GRUPO} == ESPERADO_HOGAR
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
            [f[0] for f in base.con.execute("SELECT clave FROM areas")]) == antes


async def test_si_entre_la_pregunta_y_el_si_cambia_el_grupo_no_se_borra_y_se_vuelve_a_preguntar(base):
    _sembrar(base)
    pregunta = {k: (await db.contenido_para_borrar_grupo("Hogar"))[k] for k in db.CLAVES_DE_CONTENIDO_DE_GRUPO}
    base.proyecto(8, "Llegó después", area="Hogar")
    base.con.commit()
    with pytest.raises(crud.CambioAlBorrar) as e:
        await crud.borrar_grupo("Hogar", pregunta, actor="panel")
    assert e.value.contenido["proyectos_abiertos"] == 2
    assert "Hogar" in [f[0] for f in base.con.execute("SELECT clave FROM areas")] and not _borrada(base, "proyectos", 8)


async def test_dos_si_sobre_el_mismo_grupo_borran_una_vez(base):
    _sembrar(base)
    await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    with pytest.raises(db.GrupoNoSeQuita) as e:
        await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    assert e.value.clave == "no_existe"
    assert len(_huellas(base, tabla="areas")) == 1


async def test_si_a_mitad_del_borrado_alguien_mete_algo_vivo_al_grupo_se_vuelve_a_preguntar(base, monkeypatch):
    """El gancho mete un proyecto vivo al grupo DESPUÉS de leer y ANTES de la revisión final: sale
    `CambioAlBorrar`. (SQLite no deshace la transacción al salir la excepción —psycopg sí—: se
    deshace a mano con un punto de guardado, y lo que se comprueba es que NO quedó nada escrito.)"""
    _sembrar(base)
    base.con.execute("SAVEPOINT antes_de_borrar")
    original = crud._borrar_proyecto_en
    visto = {"n": 0}

    async def _con_gancho(conn, cur, antes, **k):
        r = await original(conn, cur, antes, **k)
        visto["n"] += 1
        if visto["n"] == 2:                      # al terminar el último proyecto del grupo
            base.con.execute("INSERT INTO proyectos (id, nombre, estado, area) VALUES (88, 'Colado', 'activo', 'Hogar')")
        return r
    monkeypatch.setattr(crud, "_borrar_proyecto_en", _con_gancho)
    foto = (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
            [f[0] for f in base.con.execute("SELECT clave FROM areas")])
    with pytest.raises(crud.CambioAlBorrar):
        await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    base.con.execute("ROLLBACK TO antes_de_borrar")       # lo que hace Postgres al salir la excepción
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
            [f[0] for f in base.con.execute("SELECT clave FROM areas")]) == foto


async def test_una_tabla_que_apunte_al_grupo_y_el_borrado_no_conozca_lo_rechaza_en_vez_de_dejarla_huerfana(base):
    """ENTRADA INVENTADA: una tabla nueva con llave foránea a `areas`. Borrar el grupo no puede
    dejarla apuntando a algo que no existe: la llave lo rechaza y se vuelve a preguntar."""
    _sembrar(base)
    base.con.execute("CREATE TABLE inventada (id INTEGER PRIMARY KEY, area TEXT REFERENCES areas(clave))")
    base.con.execute("INSERT INTO inventada (area) VALUES ('Hogar')")
    base.con.commit()
    base.con.execute("SAVEPOINT s")
    with pytest.raises(crud.CambioAlBorrar):
        await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    base.con.execute("ROLLBACK TO s")
    assert "Hogar" in [f[0] for f in base.con.execute("SELECT clave FROM areas")]


# ── Hermanos: lo que cuelga de un proyecto y de un grupo, del esquema real ───

# Cómo trata el borrado cada tabla que el ESQUEMA dice que apunta a un proyecto, a una tarea o a
# un grupo. La lista de tablas sale de `db/schema.sql`; esto solo dice qué se hace con cada una.
LO_QUE_APUNTA_A_UN_PROYECTO = {
    "tareas": "se van con él (cada una con su huella) y vuelven con él",
    "eventos": "no se tocan: siguen nombrando al proyecto en la papelera, no se pierde nada",
    "notas": "no se tocan: siguen nombrando al proyecto en la papelera, no se pierde nada",
    "movimientos": "no se tocan: siguen nombrando al proyecto en la papelera, no se pierde nada",
    "participantes": "no se tocan: siguen nombrando al proyecto en la papelera, no se pierde nada",
}
LO_QUE_APUNTA_A_UN_GRUPO = {
    "proyectos": "su `area` pasa a NULL (lo vivo, antes, se borra)",
    "tareas": "su `area` pasa a NULL (lo vivo, antes, se borra)",
}


def _quien_apunta(esquema: str, destino: str) -> set[str]:
    """Las tablas de `esquema` con una columna `REFERENCES <destino>(…)`."""
    salida = set()
    for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", esquema, re.S | re.I):
        if re.search(rf"REFERENCES\s+{destino}\s*\(", m.group(2), re.I):
            salida.add(m.group(1))
    return salida


def test_toda_tabla_que_apunta_a_un_proyecto_o_a_un_grupo_esta_declarada():
    """Si aparece una tabla o llave nueva que apunte a un proyecto o a un grupo, esta prueba se
    pone roja hasta que alguien decida qué hace el borrado con ella."""
    esquema = (_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert _quien_apunta(esquema, "proyectos") == set(LO_QUE_APUNTA_A_UN_PROYECTO)
    assert _quien_apunta(esquema, "areas") == set(LO_QUE_APUNTA_A_UN_GRUPO)


def test_el_censo_de_llaves_ve_una_tabla_inventada():
    inventado = ("CREATE TABLE nueva (\n  id BIGSERIAL PRIMARY KEY,\n  proyecto_id BIGINT REFERENCES proyectos(id),\n"
                 "  area TEXT REFERENCES areas(clave)\n);")
    assert _quien_apunta(inventado, "proyectos") == {"nueva"} and _quien_apunta(inventado, "areas") == {"nueva"}


async def test_lo_que_el_borrado_declara_que_no_toca_de_verdad_no_se_toca(base):
    _sembrar(base)
    base.con.execute("INSERT INTO participantes (proyecto_id, noco_id, nombre, rol, creado_por_chat_id) "
                     "VALUES (1, 5, 'Ana', 'x', 1)")
    base.con.commit()
    antes = [tuple(f) for f in base.con.execute("SELECT * FROM participantes")]
    await crud.borrar("proyectos", 1, "x", actor="panel")
    assert [tuple(f) for f in base.con.execute("SELECT * FROM participantes")] == antes


async def test_despues_de_borrar_un_grupo_ninguna_tabla_declarada_lo_nombra(base):
    _sembrar(base)
    await crud.borrar_grupo("Hogar", dict(ESPERADO_HOGAR), actor="panel")
    for tabla in LO_QUE_APUNTA_A_UN_GRUPO:
        assert base.con.execute(f"SELECT count(*) FROM {tabla} WHERE area = 'Hogar'").fetchone()[0] == 0, tabla


# ── Postgres, lo que se puede vigilar sin él: el orden y los errores ───────

class _Reg:
    """Un `pool` de mentira que RECUERDA cada sentencia con la profundidad de transacción en que
    corrió (`conn.transaction()` anidado = punto de guardado en psycopg), con grupo sin nada
    adentro. Misma idea que `_Registro` de `tests/test_grupos.py`, pero las listas salen vacías."""

    def __init__(self, falla_en, error):
        self.falla_en, self.error = falla_en, error
        self.hechos, self.profundidad = [], 0

    def connection(self):
        reg = self

        class _CM:
            async def __aenter__(s):
                return _C(reg)

            async def __aexit__(s, *e):
                return False
        return _CM()


class _C:
    def __init__(self, r):
        self.r = r

    def transaction(self):
        r = self.r

        class _T:
            async def __aenter__(s):
                r.profundidad += 1

            async def __aexit__(s, *e):
                r.profundidad -= 1
                return False
        return _T()

    async def execute(self, sql, params=()):
        return await _K(self.r).execute(sql, params)

    def cursor(self, row_factory=None):
        return _K(self.r)


class _K:
    def __init__(self, r):
        self.r, self.sql = r, ""

    async def execute(self, sql, params=()):
        self.sql = " ".join(sql.split())
        self.r.hechos.append((self.r.profundidad, self.sql, tuple(params)))
        if self.sql.startswith(self.r.falla_en):
            raise self.r.error()
        return self

    async def fetchone(self):
        if self.sql.startswith("SELECT clave, color, orden FROM areas WHERE"):
            return {"clave": "G", "color": "#1", "orden": 9}
        if self.sql.startswith("SELECT (SELECT count"):
            return {"n": 0}
        return {"x": 1}

    async def fetchall(self):
        return []


def _con_cero_de_todo(monkeypatch):
    async def _cero(cur, clave):
        return {k: 0 for k in db.CLAVES_DE_CONTENIDO_DE_GRUPO} | {"total": 0}
    monkeypatch.setattr(db, "_contenido_para_borrar_grupo", _cero)
    return {k: 0 for k in db.CLAVES_DE_CONTENIDO_DE_GRUPO}


async def test_borrar_grupo_pone_el_bloqueo_primero_y_el_delete_dentro_de_su_punto_de_guardado(monkeypatch):
    r = _Reg("DELETE FROM areas", _ViolacionDeLlaveForanea)
    monkeypatch.setattr(db, "pool", r)
    esperado = _con_cero_de_todo(monkeypatch)
    with pytest.raises(crud.CambioAlBorrar):
        await crud.borrar_grupo("G", esperado, actor="panel")           # la llave foránea rechaza el DELETE
    assert r.hechos[0] == (1, LOCK, ("grupos",))                         # el bloqueo, lo primero, en la transacción
    delete = [h for h in r.hechos if h[1].startswith("DELETE FROM areas")]
    assert [h[0] for h in delete] == [2]                                 # el DELETE, un nivel más adentro (punto de guardado)
    assert all(h[0] == 1 for h in r.hechos if h[1].startswith(("UPDATE proyectos SET area", "UPDATE tareas SET area")))
    assert r.profundidad == 0


async def test_borrar_grupo_no_se_traga_otros_errores_de_la_base(monkeypatch):
    class _Otro(Exception):
        sqlstate = "40001"
    r = _Reg("DELETE FROM areas", _Otro)
    monkeypatch.setattr(db, "pool", r)
    esperado = _con_cero_de_todo(monkeypatch)
    with pytest.raises(_Otro):
        await crud.borrar_grupo("G", esperado, actor="panel")


# ═══════════════════════════════════════════════════════════════════════
# 3. La página: preguntas, avisos y rutas
# ═══════════════════════════════════════════════════════════════════════

def _casa(m):
    return _cliente(config.CHAT_ID_DUENO)


def _va_a(r) -> dict:
    assert r.status_code == 303, (r.status_code, r.text[:200])
    d = urlsplit(r.headers["location"])
    return {"ruta": d.path, **dict(parse_qsl(d.query))}


def _avisos(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a)).strip()
            for a in re.findall(r'<p class="aviso[^"]*">(.*?)</p>', html, re.S)]


def _texto(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


def test_la_pagina_del_proyecto_ofrece_borrar_abierto_y_cerrado_y_pregunta_con_las_cuentas(base):
    from test_pagina_proyectos import ver
    _sembrar(base)
    abierto, cerrado = ver(base, p=1), ver(base, p=2)
    for html, pid in ((abierto, 1), (cerrado, 2)):
        assert f'href="/proyectos?p={pid}&amp;borrar_proyecto={pid}"' in html and ">Borrar proyecto<" in html
    pregunta = _texto(ver(base, p=1, borrar_proyecto=1))
    assert ("¿Borrar el proyecto «Casa nueva»? Se van con él 3 tareas (1 pendiente, 1 hecha, 1 con otro estado). "
            "Todo va a la Papelera; de ahí lo restauras y el proyecto vuelve con esas tareas.") in pregunta
    html = ver(base, p=1, borrar_proyecto=1)
    assert 'action="/proyectos/1/borrar"' in html and ">Sí, borrar el proyecto<" in html
    for k, v in ESPERADO_P1.items():
        assert f'name="{k}" value="{v}"' in html
    base.proyecto(9, "Sin tareas", area="CDS")
    base.con.commit()
    assert "¿Borrar el proyecto «Sin tareas»? No tiene tareas." in _texto(ver(base, p=9, borrar_proyecto=9))


def test_borrar_un_proyecto_por_la_ruta_lo_manda_a_la_papelera_y_el_aviso_sale_de_la_base(base):
    from test_pagina_proyectos import ver
    _sembrar(base)
    r = _casa(base).post("/proyectos/1/borrar", data=ESPERADO_P1, follow_redirects=False)
    q = _va_a(r)
    assert q == {"ruta": "/proyectos", "hecho": "proyecto_borrado", "borrado": "1"}
    assert _borrada(base, "proyectos", 1) and all(_borrada(base, "tareas", t) for t in (10, 11, 12))
    html = ver(base, **{k: v for k, v in q.items() if k != "ruta"})
    assert _avisos(html)[0] == "El proyecto «Casa nueva» está en la Papelera, con 3 tareas que se fueron con él. Se restaura desde ahí."
    lista = html.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert "Casa nueva" not in _texto(lista) and "De otro grupo" in _texto(lista)
    h, = _huellas(base, tabla="proyectos", accion="borrar", actor="panel")
    assert h["registro_id"] == 1


@pytest.mark.parametrize("consulta", [
    {"hecho": "proyecto_borrado", "borrado": 1}, {"hecho": "proyecto_borrado"}, {"hecho": "proyecto_borrado", "borrado": 999}], ids=str)
def test_una_direccion_escrita_a_mano_no_hace_decir_borrado_si_no_esta_en_la_papelera(base, consulta):
    from test_pagina_proyectos import ver
    _sembrar(base)
    html = ver(base, **consulta)
    assert not [a for a in _avisos(html) if "Papelera" in a], _avisos(html)


def test_un_si_sin_cuentas_o_con_cuentas_viejas_no_borra_y_vuelve_a_preguntar_con_lo_de_ahora(base):
    from test_pagina_proyectos import ver
    _sembrar(base)
    cliente = _casa(base)
    for datos in ({}, {"tareas_pendientes": 1}, {**ESPERADO_P1, "tareas_hechas": 0}, {**ESPERADO_P1, "tareas_otras": "x"}):
        q = _va_a(cliente.post("/proyectos/1/borrar", data=datos, follow_redirects=False))
        assert q == {"ruta": "/proyectos", "p": "1", "borrar_proyecto": "1", "error": "borrar_cambio"}
        assert not _borrada(base, "proyectos", 1) and not _borrada(base, "tareas", 10)
    # Apareció una tarea después de la pregunta: el aviso lo dice y la pregunta trae el número de AHORA.
    base.tarea(99, "nueva", proyecto=1)
    base.con.commit()
    q = _va_a(cliente.post("/proyectos/1/borrar", data=ESPERADO_P1, follow_redirects=False))
    assert q["error"] == "borrar_cambio" and not _borrada(base, "proyectos", 1)
    html = ver(base, **{k: v for k, v in q.items() if k != "ruta"})
    assert _avisos(html)[0] == ("El proyecto «Casa nueva» sigue aquí: NO se borró nada. Lo que tiene ahora está abajo; "
                                "confirma otra vez solo si es lo que quieres borrar.")
    assert "Se van con él 4 tareas (2 pendientes, 1 hecha, 1 con otro estado)" in _texto(html)


def test_dos_si_o_un_proyecto_que_ya_no_esta_lo_dicen(base):
    _sembrar(base)
    c = _casa(base)
    assert _va_a(c.post("/proyectos/1/borrar", data=ESPERADO_P1, follow_redirects=False))["hecho"] == "proyecto_borrado"
    assert _va_a(c.post("/proyectos/1/borrar", data=ESPERADO_P1, follow_redirects=False)) == {"ruta": "/proyectos", "error": "proyecto"}
    assert _va_a(c.post("/proyectos/999/borrar", data=ESPERADO_P1, follow_redirects=False))["error"] == "proyecto"
    assert len(_huellas(base, tabla="proyectos", accion="borrar", actor="panel")) == 1


def test_borrar_un_grupo_por_la_ruta_pregunta_borra_y_el_aviso_sale_de_la_huella(base):
    from test_pagina_proyectos import ver
    _sembrar(base)
    base.con.commit()
    c = _casa(base)
    datos = {"clave": "Hogar", **{k: v for k, v in ESPERADO_HOGAR.items()}}
    q = _va_a(c.post("/proyectos/grupos/borrar", data=datos, follow_redirects=False))
    assert q == {"ruta": "/proyectos", "hecho": "grupo_borrado", "grupo": "Hogar"}
    assert "Hogar" not in [f[0] for f in base.con.execute("SELECT clave FROM areas")]
    html = ver(base, hecho="grupo_borrado", grupo="Hogar")
    assert _avisos(html)[0] == ("El grupo «Hogar» ya no está en la lista de la izquierda. Lo que tenía se fue a la Papelera: "
                                "2 proyectos y 6 tareas. Lo que restaures vuelve a «Sin grupo».")
    # una dirección escrita a mano con el nombre de un grupo que SÍ existe no dice «borrado»
    assert not [a for a in _avisos(ver(base, hecho="grupo_borrado", grupo="CDS")) if "ya no está" in a]
    assert not [a for a in _avisos(ver(base, hecho="grupo_borrado", grupo="Nunca existió")) if "Papelera" in a]


def test_un_si_del_grupo_con_cuentas_viejas_no_borra_y_la_pagina_vuelve_a_preguntar(base):
    from test_pagina_proyectos import ver
    _sembrar(base)
    c = _casa(base)
    base.proyecto(8, "Llegó después", area="Hogar")
    base.con.commit()
    q = _va_a(c.post("/proyectos/grupos/borrar", data={"clave": "Hogar", **ESPERADO_HOGAR}, follow_redirects=False))
    assert q == {"ruta": "/proyectos", "error": "grupo_cambio", "quitar_grupo": "Hogar"}
    assert "Hogar" in [f[0] for f in base.con.execute("SELECT clave FROM areas")] and not _borrada(base, "proyectos", 1)
    html = ver(base, **{k: v for k, v in q.items() if k != "ruta"})
    assert _avisos(html)[0] == ("El grupo «Hogar» sigue aquí: NO se borró nada. Lo que tiene ahora está a la izquierda; "
                                "confirma otra vez solo si es lo que quieres borrar.")
    assert "Tiene 2 proyectos abiertos" in _texto(html)
    sin = _va_a(c.post("/proyectos/grupos/borrar", data={"clave": "Hogar"}, follow_redirects=False))
    assert sin["error"] == "grupo_cambio" and not _borrada(base, "proyectos", 1)


def test_el_grupo_fijo_no_se_borra_por_la_ruta(base):
    base.proyecto(7, "De IA", area="IA")
    base.con.commit()
    q = _va_a(_casa(base).post("/proyectos/grupos/borrar", data={"clave": "IA"}, follow_redirects=False))
    assert q["error"] == "grupo_fijo" and "IA" in [f[0] for f in base.con.execute("SELECT clave FROM areas")]
    assert not _borrada(base, "proyectos", 7)


# ── solo_ver y sin sesión ───────────────────────────────────────────────────

RUTAS_QUE_BORRAN = [("/proyectos/1/borrar", ESPERADO_P1),
                    ("/proyectos/grupos/borrar", {"clave": "Hogar", **ESPERADO_HOGAR}),
                    ("/papelera/restaurar", {"tabla": "proyectos", "id": 3})]


@pytest.mark.parametrize("ruta,datos", RUTAS_QUE_BORRAN)
@pytest.mark.parametrize("quien", ["ver", None])
def test_quien_solo_ve_o_no_entro_no_llama_a_las_rutas_de_borrar_ni_de_restaurar(base, ruta, datos, quien):
    _sembrar(base)
    antes = (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
             [f[0] for f in base.con.execute("SELECT clave FROM areas")])
    r = cliente_de_sesion(quien).post(ruta, data=datos, follow_redirects=False)
    assert r.status_code == 401, (quien, ruta, r.status_code)
    assert (_filas(base, "tareas"), _filas(base, "proyectos"), _huellas(base),
            [f[0] for f in base.con.execute("SELECT clave FROM areas")]) == antes


def test_en_solo_ver_ni_los_controles_ni_los_avisos_de_borrar(base):
    _sembrar(base)
    c = cliente_de_sesion("ver")
    for consulta in ({"p": 1}, {"p": 1, "borrar_proyecto": 1}, {"quitar_grupo": "Hogar"},
                     {"hecho": "proyecto_borrado", "borrado": 1}, {"hecho": "grupo_borrado", "grupo": "Hogar"}, {"error": "grupo_cambio"}):
        html = c.get("/proyectos", params=consulta).text
        assert "/borrar" not in html and "Borrar proyecto" not in html and "Sí, borrar" not in html, consulta
        assert not [a for a in _avisos(html) if "Papelera" in a or "NO se borró" in a], consulta


def test_la_papelera_no_la_ve_ni_la_toca_quien_no_entro(base):
    _sembrar(base)
    assert cliente_de_sesion(None).get("/papelera", follow_redirects=False).status_code in (303, 401, 307)


# ── Lo nuevo NO pasa por la pieza que escribe por JavaScript ──────────────

def test_los_formularios_de_borrar_y_restaurar_no_son_form_marcar_ni_hay_guion_en_la_papelera():
    """`marcarSinSaltar` (`_marcar_sin_saltar.html`) es la ÚNICA excepción a las prohibiciones del
    guion: solo engancha `form.marcar`. Los formularios de borrar y de restaurar (de las plantillas
    reales) son envíos normales —el servidor decide qué se ve—, y la Papelera no trae ningún guion."""
    for nombre in ("proyectos.html", "papelera.html"):
        texto = (_ROOT / "web" / "plantillas" / nombre).read_text(encoding="utf-8")
        hallados = [e for e, _ in re.findall(r"<form\b([^>]*)>(.*?)</form>", texto, re.S)
                    if re.search(r'action="[^"]*/(?:borrar|restaurar)"', e)]
        assert len(hallados) >= (2 if nombre == "proyectos.html" else 2), (nombre, hallados)
        assert not [e for e in hallados if re.search(r'\bclass="[^"]*\bmarcar\b', e)], (nombre, hallados)
    assert "<script" not in (_ROOT / "web" / "plantillas" / "papelera.html").read_text(encoding="utf-8")
