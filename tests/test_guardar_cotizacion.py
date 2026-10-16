"""Guardar y leer las cotizaciones (pieza 3 del diseño «Cotizaciones en Proyectos», 14-oct-2026).

Qué se vigila, con las sentencias REALES de `db.guardar_cotizacion` y `db.cotizaciones_de_proyectos`
EJECUTADAS en SQLite sobre las tablas que crea la migración real (`db/migrations/2026-10-14_cotizaciones.sql`):

  1. crear (borrador o directo a enviada): número de la serie, año de `hoy_rd()` (no el de `fecha`),
     nombre de la ficha si no se da, huella `crear`;
  2. editar un borrador solo con el candado `cambiado_en` que se leyó; con otro, NO se escribe nada;
  3. enviar; CORREGIR una enviada (respuesta 2 de Tiziano): conserva `anio`, `numero` y `enviada_en`, sube
     `cambiado_en`, deja la huella `corregir`; una enviada no vuelve a borrador;
  4. nombre y teléfono se COPIAN: si la ficha cambia, la cotización y su lectura no cambian;
  5. quien no entra al panel (la cookie de solo ver tampoco llega aquí) no escribe, y no gasta número;
     la lectura no pide sesión;
  6. lo que no vale no gasta número; un fallo después de sacar el número deshace el número;
  7. los textos de `validez` y `alternativas` se rehacen en cada guardado;
  8. la lectura: más nueva primero, copia (no ficha), totales por opción, sin la tabla → `None`;
  9. una sola puerta de escritura (`ast` sobre todo `.py` fuera de `tests/`): ninguna sentencia toca
     `proyecto_id`/`anio`/`numero`/`creado_*`, todas las que cambian exigen el candado, nadie borra filas.

FRONTERA, dicha una vez:
  · NO hay Postgres en esta máquina. SQLite no tiene `timestamptz` (las fechas viajan como texto ISO, que
    es lo que el doble entrega), ni bloqueo de fila: el candado se prueba como condición de la sentencia
    (`WHERE … cambiado_en = …`), no con dos escritores simultáneos de verdad. `psycopg` no se ejerce: el
    cursor es un doble mínimo (`%s` → `?`, filas como dict) que no cambia la sentencia que recibe.
  · `now()` es una función de la prueba que sube un microsegundo por llamada; en Postgres `now()` es la hora
    del inicio de la transacción (dos escrituras en la MISMA transacción darían el mismo valor; aquí nunca
    se escribe dos veces la misma cotización en una transacción).
  · La ruta y la pantalla no existen todavía (pieza 5): «solo ver no escribe» se prueba en la función.

Correr:  python3 -m pytest tests/test_guardar_cotizacion.py -q
"""
from __future__ import annotations

import asyncio
import ast
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import test_buzon_que_no_se_ve as buzon
import test_cotizaciones_tablas as t
import test_grupo_ia as g
import test_numero_de_cotizacion as num
from test_grupo_ia import _ROOT

import config
import db.db as db

CASA = config.CHAT_ID_DUENO
EXTRANO = CASA + 12345        # no entra al panel (así es el de solo ver para esta función)


class _SinTabla(Exception):
    sqlstate = "42P01"


def _a_texto(v):
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="microseconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


class _Cur:
    def __init__(self, con):
        self._con = con
        self._filas: list[dict] = []

    async def execute(self, sql, params=None):
        try:
            cur = self._con.execute(sql.replace("%s", "?"), tuple(_a_texto(p) for p in (params or ())))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise _SinTabla(str(e)) from None
            raise
        self._filas = ([dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()]
                       if cur.description else [])
        return self

    async def fetchone(self):
        return self._filas[0] if self._filas else None

    async def fetchall(self):
        return list(self._filas)


class _Conn:
    def __init__(self, con):
        self._con = con

    def cursor(self, row_factory=None):
        return _Cur(self._con)

    async def execute(self, sql, params=None):
        return await _Cur(self._con).execute(sql, params)


class _Pool:
    """Como el de psycopg: confirma al salir bien y DESHACE todo si algo revienta."""

    def __init__(self, con):
        self.con = con

    def connection(self):
        con = self.con

        class _CM:
            async def __aenter__(self):
                return _Conn(con)

            async def __aexit__(self, tipo, *e):
                con.commit() if tipo is None else con.rollback()
                return False
        return _CM()


def _base():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = None
    contador = iter(range(1, 10**6))
    inicio = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    con.create_function("now", 0, lambda: _a_texto(inicio + timedelta(microseconds=next(contador))))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("CREATE TABLE proyectos (id INTEGER PRIMARY KEY, nombre TEXT, cliente_nombre TEXT, borrado_en TEXT)")
    con.execute("INSERT INTO proyectos (id, nombre, cliente_nombre) VALUES (1, 'Casa de prueba', 'Cliente de la ficha')")
    con.execute("INSERT INTO proyectos (id, nombre, cliente_nombre) VALUES (2, 'Otro proyecto', 'Otro cliente')")
    con.execute("CREATE TABLE log_acciones (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, accion TEXT, "
                "tabla TEXT, registro_id INTEGER, antes TEXT, despues TEXT, motivo TEXT, bandeja_id INTEGER)")
    # la migración de las cotizaciones y la de Drive (pieza 8), una tras otra, como en producción
    for texto in (t._texto_migracion(), t._texto_migracion_de_drive()):
        for s in g._sentencias(texto):
            if not re.match(r"(BEGIN|COMMIT|COMMENT\s+ON)\b", s, re.I):
                con.execute(t._a_sqlite(s))
    con.commit()
    return con


@pytest.fixture
def con(monkeypatch):
    c = _base()
    monkeypatch.setattr(db, "pool", _Pool(c))
    monkeypatch.setattr(db, "hoy_rd", lambda: date(2026, 10, 14))
    assert EXTRANO not in config.CHAT_IDS_PERMITIDOS
    return c


def _contenido(opciones=1, **extra):
    c = {"opciones": [{"nombre": f"Opción {i + 1}", "renglones": [
            {"descripcion": f"Trabajo {i + 1}", "detalle": "", "total": f"{(i + 1) * 1000}"}]}
            for i in range(opciones)],
         "condiciones": [
             {"clave": "validez", "texto": "x", "marcada": True},
             {"clave": "alternativas", "texto": "x", "marcada": True},
             {"clave": "deposito", "texto": "Se requiere un depósito del 50%.", "marcada": True}]}
    c.update(extra)
    return c


def _guardar(accion="borrador", contenido=None, validez=15, chat=CASA, proyecto=1, **kw):
    return asyncio.run(db.guardar_cotizacion(proyecto, chat, accion, contenido or _contenido(), validez, **kw))


def _fila(con, cid=1):
    cur = con.execute("SELECT * FROM cotizaciones WHERE id = ?", (cid,))
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def _cuantas(con, tabla="cotizaciones"):
    return con.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]


def _huellas(con):
    cur = con.execute("SELECT actor, accion, tabla, registro_id, antes, despues FROM log_acciones ORDER BY id")
    return [dict(zip([d[0] for d in cur.description], f)) for f in cur.fetchall()]


# ── 1. Crear ────────────────────────────────────────────────────────────────────────────────────────

def test_la_primera_cotizacion_es_la_cot_2026_001_borrador_con_su_huella(con):
    r = _guardar()
    assert (r["codigo"], r["anio"], r["numero"], r["estado"], r["enviada_en"], r["creada"]) == \
        ("COT-2026-001", 2026, 1, "borrador", None, True)
    assert r["creado_por_chat_id"] == CASA and r["proyecto_id"] == 1
    h = _huellas(con)
    assert [(x["actor"], x["accion"], x["tabla"], x["registro_id"], x["antes"]) for x in h] == \
        [("panel", "crear", "cotizaciones", r["id"], None)]
    assert json.loads(h[0]["despues"])["numero"] == 1


def test_directo_a_enviada_pone_estado_y_enviada_en(con):
    r = _guardar("enviar")
    assert r["estado"] == "enviada" and r["enviada_en"] is not None


def test_dos_cotizaciones_seguidas_llevan_numeros_seguidos_aunque_sean_de_proyectos_distintos(con):
    assert [_guardar(proyecto=p)["codigo"] for p in (1, 2, 1)] == ["COT-2026-001", "COT-2026-002", "COT-2026-003"]


def test_el_anio_del_numero_sale_de_hoy_y_no_de_la_fecha_de_la_cotizacion(con, monkeypatch):
    monkeypatch.setattr(db, "hoy_rd", lambda: date(2026, 12, 31))
    r = _guardar(fecha=date(2025, 1, 15))
    assert (r["anio"], r["fecha"]) == (2026, "2025-01-15")
    monkeypatch.setattr(db, "hoy_rd", lambda: date(2027, 1, 1))
    r2 = _guardar(fecha=date(2026, 12, 31))
    assert (r2["codigo"], r2["fecha"]) == ("COT-2027-001", "2026-12-31")


def test_la_fecha_por_omision_es_hoy_y_acepta_texto_iso(con):
    assert _guardar()["fecha"] == "2026-10-14"
    assert _guardar(fecha="2026-11-02")["fecha"] == "2026-11-02"


# ── 2. El candado de edición ────────────────────────────────────────────────────────────────────────

def test_editar_un_borrador_con_el_candado_que_se_leyo_cambia_y_sube_el_candado(con):
    r = _guardar()
    r2 = _guardar(contenido=_contenido(2), validez=30, cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert (r2["numero"], r2["estado"], r2["creada"], r2["validez_dias"]) == (1, "borrador", False, 30)
    assert r2["cambiado_en"] != r["cambiado_en"]
    assert len(json.loads(_fila(con)["contenido"])["opciones"]) == 2
    assert [x["accion"] for x in _huellas(con)] == ["crear", "editar"]
    assert json.loads(_huellas(con)[1]["antes"])["validez_dias"] == 15


def test_con_un_candado_viejo_no_se_guarda_nada(con):
    r = _guardar()
    _guardar(contenido=_contenido(2), cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])   # otra pestaña
    antes = _fila(con)
    with pytest.raises(db.CotizacionCambio):
        _guardar(contenido=_contenido(3), cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert _fila(con) == antes
    assert [x["accion"] for x in _huellas(con)] == ["crear", "editar"]


def test_el_candado_tambien_vale_al_enviar_y_al_corregir(con):
    r = _guardar()
    _guardar(cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    with pytest.raises(db.CotizacionCambio):
        _guardar("enviar", cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert _fila(con)["estado"] == "borrador"
    e = _guardar("enviar", cotizacion_id=r["id"], cambiado_en=_fila(con)["cambiado_en"])
    with pytest.raises(db.CotizacionCambio):
        _guardar("enviar", cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert _fila(con)["cambiado_en"] == e["cambiado_en"]


def test_el_candado_se_acepta_como_datetime_o_como_texto_y_es_obligatorio(con):
    r = _guardar()
    candado = datetime.fromisoformat(r["cambiado_en"])
    assert _guardar(cotizacion_id=r["id"], cambiado_en=candado)["creada"] is False
    for malo in (None, "ayer", 12345):
        with pytest.raises(db.CotizacionNoVale) as e:
            _guardar(cotizacion_id=r["id"], cambiado_en=malo)
        assert e.value.clave == "candado"


# ── 3. Enviar, corregir una enviada ─────────────────────────────────────────────────────────────────

def test_enviar_un_borrador_lo_deja_enviada_con_su_huella_enviar(con):
    r = _guardar()
    e = _guardar("enviar", cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert (e["estado"], e["numero"]) == ("enviada", 1) and e["enviada_en"] is not None
    assert [x["accion"] for x in _huellas(con)] == ["crear", "enviar"]


def test_corregir_una_enviada_conserva_numero_y_primera_fecha_de_envio_y_queda_registrado(con):
    e = _guardar("enviar")
    c = _guardar("enviar", contenido=_contenido(2), validez=7, cotizacion_id=e["id"], cambiado_en=e["cambiado_en"])
    assert (c["anio"], c["numero"], c["codigo"], c["estado"]) == (2026, 1, "COT-2026-001", "enviada")
    assert c["enviada_en"] == e["enviada_en"]
    assert c["cambiado_en"] != e["cambiado_en"]
    assert _cuantas(con) == 1 and _cuantas(con, "serie_cotizaciones") == 1
    assert con.execute("SELECT ultimo FROM serie_cotizaciones").fetchone()[0] == 1
    h = _huellas(con)[-1]
    assert (h["accion"], h["tabla"], h["registro_id"]) == ("corregir", "cotizaciones", e["id"])
    assert json.loads(h["antes"])["validez_dias"] == 15 and json.loads(h["despues"])["validez_dias"] == 7


def test_una_enviada_no_vuelve_a_borrador_por_ningun_camino(con):
    e = _guardar("enviar")
    antes = _fila(con)
    with pytest.raises(db.CotizacionEnviadaSoloSeEnvia):
        _guardar("borrador", cotizacion_id=e["id"], cambiado_en=e["cambiado_en"])
    assert _fila(con) == antes
    # ni con el candado equivocado ni con ninguna acción rara
    with pytest.raises(db.CotizacionEnviadaSoloSeEnvia):
        _guardar("borrador", cotizacion_id=e["id"], cambiado_en="2020-01-01 00:00:00+00:00")
    with pytest.raises(db.CotizacionNoVale):
        _guardar("devolver", cotizacion_id=e["id"], cambiado_en=e["cambiado_en"])
    assert _fila(con) == antes
    assert [x["accion"] for x in _huellas(con)] == ["crear"]


# ── 4. Nombre y teléfono se copian ──────────────────────────────────────────────────────────────────

def test_sin_nombre_se_copia_el_de_la_ficha_y_con_nombre_se_copia_el_dado(con):
    assert _guardar()["para_nombre"] == "Cliente de la ficha"
    r = _guardar(para_nombre="  Otro Nombre ", para_telefono="809-555-0101")
    assert (r["para_nombre"], r["para_telefono"]) == ("Otro Nombre", "809-555-0101")
    assert _guardar()["para_telefono"] is None


def test_si_la_ficha_cambia_la_cotizacion_y_su_lectura_no_cambian(con):
    r = _guardar(para_telefono="809-555-0101")
    con.execute("UPDATE proyectos SET cliente_nombre = 'Nombre nuevo de la ficha' WHERE id = 1")
    leida = asyncio.run(db.cotizaciones_de_proyectos())[1][0]
    assert (leida["para_nombre"], leida["para_telefono"]) == ("Cliente de la ficha", "809-555-0101")
    # guardar otra vez SIN nombre ni teléfono no los toca (tampoco los lee de la ficha)
    _guardar(cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert (_fila(con)["para_nombre"], _fila(con)["para_telefono"]) == ("Cliente de la ficha", "809-555-0101")
    # con un teléfono vacío se borra
    r3 = _guardar(cotizacion_id=r["id"], cambiado_en=_fila(con)["cambiado_en"], para_telefono="")
    assert r3["para_telefono"] is None


def test_una_nueva_sin_nombre_ni_ficha_con_nombre_no_vale_y_no_gasta_numero(con):
    con.execute("UPDATE proyectos SET cliente_nombre = NULL WHERE id = 1")
    with pytest.raises(db.CotizacionNoVale) as e:
        _guardar()
    assert e.value.clave == "para_nombre_vacio"
    assert _cuantas(con) == 0 and _cuantas(con, "serie_cotizaciones") == 0


# ── 5. Quién escribe y quién lee ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chat", [None, EXTRANO, 0])
def test_quien_no_entra_al_panel_no_escribe_ni_gasta_numero(con, chat):
    with pytest.raises(db.CotizacionSinSesion):
        _guardar(chat=chat)
    assert _cuantas(con) == 0 and _cuantas(con, "serie_cotizaciones") == 0 and _huellas(con) == []


def test_quien_solo_ve_tampoco_corrige_una_existente(con):
    r = _guardar("enviar")
    antes = _fila(con)
    with pytest.raises(db.CotizacionSinSesion):
        _guardar("enviar", chat=EXTRANO, cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert _fila(con) == antes


def test_la_lectura_no_pide_sesion(con):
    _guardar("enviar")
    assert asyncio.run(db.cotizaciones_de_proyectos())[1][0]["codigo"] == "COT-2026-001"


# ── 6. Lo que no vale no gasta número; un fallo deshace el número ───────────────────────────────────

@pytest.mark.parametrize("kw,clave", [
    ({"validez": 20}, "validez"), ({"validez": True}, "validez"), ({"validez": None}, "validez"),
    ({"fecha": "mañana"}, "fecha"), ({"fecha": datetime(2026, 1, 1)}, "fecha"),
    ({"para_nombre": "   "}, "para_nombre_vacio"), ({"para_nombre": "x" * 201}, "para_nombre_largo"),
    ({"para_telefono": "9" * 41}, "para_telefono_largo"),
    ({"contenido": {"opciones": []}}, "sin_opciones"), ({"contenido": "texto"}, "forma"),
    ({"accion": "borrar"}, "accion"),
])
def test_lo_que_no_vale_no_escribe_ni_gasta_numero(con, kw, clave):
    with pytest.raises(db.CotizacionNoVale) as e:
        _guardar(**kw)
    assert e.value.clave == clave
    assert _cuantas(con) == 0 and _cuantas(con, "serie_cotizaciones") == 0 and _huellas(con) == []


def test_un_proyecto_que_no_esta_o_en_la_papelera_no_gasta_numero(con):
    con.execute("UPDATE proyectos SET borrado_en = '2026-10-01 00:00:00' WHERE id = 2")
    for pid in (2, 99):
        with pytest.raises(db.CotizacionNoEsta):
            _guardar(proyecto=pid)
    assert _cuantas(con, "serie_cotizaciones") == 0


def test_una_cotizacion_de_otro_proyecto_o_borrada_no_se_toca(con):
    r = _guardar()
    with pytest.raises(db.CotizacionNoEsta):
        _guardar(proyecto=2, cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    con.execute("UPDATE cotizaciones SET borrado_en = '2026-10-15 00:00:00'")
    with pytest.raises(db.CotizacionNoEsta):
        _guardar(cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    with pytest.raises(db.CotizacionNoEsta):
        _guardar(cotizacion_id=999, cambiado_en=r["cambiado_en"])


def test_si_el_insert_falla_el_numero_sacado_se_deshace(con):
    """El número se saca en la MISMA transacción que el INSERT: un fallo del INSERT (aquí, el UNIQUE)
    deja la serie como estaba y sin huella."""
    f = t._fila(anio=2026, numero=1)
    con.execute(f"INSERT INTO cotizaciones ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", tuple(f.values()))
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _guardar()
    assert _cuantas(con, "serie_cotizaciones") == 0 and _huellas(con) == [] and _cuantas(con) == 1


def test_sin_la_tabla_guardar_dice_cual_falta(con):
    con.execute("DROP TABLE cotizaciones")
    with pytest.raises(db.CotizacionesSinTabla):
        _guardar()
    con.rollback()


# ── 7. Los textos de validez y alternativas se rehacen ──────────────────────────────────────────────

def _condiciones(con, cid=1):
    return {c["clave"]: c for c in json.loads(_fila(con, cid)["contenido"])["condiciones"]}


def test_el_texto_de_la_validez_dice_la_validez_de_este_guardado(con):
    r = _guardar(validez=30)
    assert _condiciones(con)["validez"]["texto"] == "Esta cotización tiene validez de 30 días a partir de la fecha de emisión."
    _guardar(validez=7, cotizacion_id=r["id"], cambiado_en=r["cambiado_en"])
    assert "validez de 7 días" in _condiciones(con)["validez"]["texto"]


def test_alternativas_dice_cuantas_opciones_hay_y_con_una_sola_no_se_ofrece(con):
    _guardar(contenido=_contenido(1))
    assert "alternativas" not in _condiciones(con, 1)
    for n in range(2, db.MAX_OPCIONES_DE_COTIZACION + 1):
        r = _guardar(contenido=_contenido(n))
        assert _condiciones(con, r["id"])["alternativas"]["texto"] == \
            f"Las {db._NUMERO_EN_PALABRAS[n]} opciones son alternativas: se selecciona una sola para el proyecto."


def test_hay_palabra_para_cada_numero_de_opciones_posible():
    assert set(db._NUMERO_EN_PALABRAS) == set(range(2, db.MAX_OPCIONES_DE_COTIZACION + 1))


def test_lo_marcado_y_las_demas_condiciones_no_se_tocan(con):
    c = _contenido(2)
    c["condiciones"][0]["marcada"] = False
    c["condiciones"].append({"clave": None, "texto": "Una propia.", "marcada": True})
    _guardar(contenido=c)
    cs = json.loads(_fila(con)["contenido"])["condiciones"]
    assert [(x["clave"], x["marcada"]) for x in cs] == \
        [("validez", False), ("alternativas", True), ("deposito", True), (None, True)]
    assert cs[2]["texto"] == "Se requiere un depósito del 50%." and cs[3]["texto"] == "Una propia."


# ── 8. La lectura ───────────────────────────────────────────────────────────────────────────────────

def test_la_lectura_trae_la_mas_nueva_primero_con_codigo_totales_y_candado(con):
    a = _guardar(contenido=_contenido(2))
    b = _guardar(proyecto=2)
    c = _guardar()
    leidas = asyncio.run(db.cotizaciones_de_proyectos())
    assert set(leidas) == {1, 2}
    assert [x["codigo"] for x in leidas[1]] == ["COT-2026-003", "COT-2026-001"]
    assert [x["codigo"] for x in leidas[2]] == ["COT-2026-002"]
    primera = leidas[1][1]
    assert primera["totales_por_opcion"] == [Decimal("1000.00"), Decimal("2000.00")]
    assert all(isinstance(x, Decimal) for x in primera["totales_por_opcion"])
    assert primera["cambiado_en"] == a["cambiado_en"] and isinstance(primera["contenido"], dict)
    assert b["id"] != c["id"]


def test_la_lectura_no_trae_borradas_ni_de_proyectos_en_la_papelera(con):
    _guardar()
    r2 = _guardar(proyecto=2)
    con.execute("UPDATE cotizaciones SET borrado_en = '2026-10-15 00:00:00' WHERE id = 1")
    assert list(asyncio.run(db.cotizaciones_de_proyectos())) == [2]
    con.execute("UPDATE proyectos SET borrado_en = '2026-10-15 00:00:00' WHERE id = 2")
    assert asyncio.run(db.cotizaciones_de_proyectos()) == {}
    assert r2["id"] == 2


def test_sin_la_tabla_la_lectura_da_none_y_otro_error_se_propaga(con, monkeypatch):
    con.execute("DROP TABLE cotizaciones")
    assert asyncio.run(db.cotizaciones_de_proyectos()) is None

    class _Raro(Exception):
        sqlstate = "08006"          # la conexión se cayó: no es «falta la tabla» ni «falta una columna» (42P01, 42703)

    async def revienta(self, sql, params=None):
        raise _Raro()
    monkeypatch.setattr(_Cur, "execute", revienta)
    with pytest.raises(_Raro):
        asyncio.run(db.cotizaciones_de_proyectos())


# ── 9. Una sola puerta de escritura ─────────────────────────────────────────────────────────────────

_ESCRIBE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+cotizaciones\b", re.I)


def _sentencias_sobre_cotizaciones(archivos, raiz) -> list[tuple[str, int, str]]:
    """(archivo, línea, texto) de toda constante de texto (o suma de ellas) de un `.py` fuera de `tests/` que
    INSERTA, ACTUALIZA o BORRA en `cotizaciones`. Misma regla y misma frontera que la de la serie
    (`num._textos_que_escriben_la_serie`): no ve el nombre de la tabla en una variable, f-strings ni SQL armado
    en ejecución."""
    hallados = []
    for ruta in archivos:
        rel = ruta.relative_to(raiz).as_posix()
        if rel.startswith("tests/") or ruta.name == "conftest.py":
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(arbol)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        absorbidos: set[int] = set()
        for nodo in ast.walk(arbol):
            if id(nodo) in absorbidos or id(nodo) in docstrings:
                continue
            v = num._texto_fijo(nodo)
            if v is not None:
                absorbidos.update(id(h) for h in ast.walk(nodo))
                if _ESCRIBE.search(v):
                    hallados.append((rel, nodo.lineno, v))
    return hallados


def _lo_que_viola(hallados):
    """Reglas: nadie borra; solo `db/db.py` escribe; un UPDATE no toca las columnas que no cambian y exige
    el candado en su WHERE."""
    malos = []
    for rel, linea, v in hallados:
        if rel != "db/db.py":
            malos.append((rel, linea, "escribe fuera de db/db.py"))
        if re.search(r"\b(DELETE\s+FROM|REPLACE\s+INTO)\b", v, re.I):
            malos.append((rel, linea, "borra"))
        m = re.search(r"\bUPDATE\s+cotizaciones\s+SET\b(.*?)\bWHERE\b(.*)", v, re.I | re.S)
        if re.search(r"\bUPDATE\s+cotizaciones\b", v, re.I):
            if m is None:
                malos.append((rel, linea, "UPDATE sin WHERE"))
                continue
            puesto, donde = m.group(1), m.group(2)
            for col in ("proyecto_id", "anio", "numero", "creado_en", "creado_por_chat_id", "borrado_en", "id"):
                if re.search(rf"\b{col}\s*=", puesto):
                    malos.append((rel, linea, f"cambia {col}"))
            if not re.search(r"\bcambiado_en\s*=\s*%s", donde):
                malos.append((rel, linea, "sin el candado en el WHERE"))
    return malos


def test_las_sentencias_que_escriben_cotizaciones_son_las_de_la_puerta_y_cumplen_las_reglas():
    hallados = _sentencias_sobre_cotizaciones(buzon._py_en_disco(_ROOT), _ROOT)
    inserts = [h for h in hallados if re.search(r"INSERT\s+INTO", h[2], re.I)]
    updates = [h for h in hallados if re.search(r"UPDATE\s+cotizaciones", h[2], re.I)]
    assert len(inserts) == 1 and len(updates) == 5, hallados
    # las UPDATE son las dos sentencias de la puerta (guardar) y las dos de Drive (pieza 8: marcar «en Drive» y
    # anotar el error; y la de soltar los archivos al rehacer la carpeta), que también exigen el candado `cambiado_en` y no tocan las columnas que no cambian
    assert sorted(u[2] for u in updates) == sorted((
        db.SENTENCIA_DE_GUARDAR_BORRADOR, db.SENTENCIA_DE_ENVIAR,
        db.SENTENCIA_DE_MARCAR_EN_DRIVE, db.SENTENCIA_DE_ERROR_DE_DRIVE, db.SENTENCIA_DE_SOLTAR_DRIVE))
    assert _lo_que_viola(hallados) == []


def test_la_guarda_ve_lo_inventado(tmp_path):
    """Entradas inventadas: lo que la guarda tiene que rechazar, escrito de varias formas."""
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "db.py").write_text(
        'A = "UPDATE cotizaciones SET anio = %s WHERE id = %s AND cambiado_en = %s"\n'
        'B = "UPDATE cotizaciones SET " + "numero = 1 WHERE cambiado_en = %s"\n'
        'C = "UPDATE cotizaciones SET para_nombre = %s WHERE id = %s"\n'
        'D = "DELETE FROM cotizaciones WHERE id = 1"\n'
        'E = ("UPDATE cotizaciones SET estado = 1 "\n     "WHERE cambiado_en = %s")\n', encoding="utf-8")
    (tmp_path / "otro.py").write_text('F = "INSERT INTO cotizaciones (a) VALUES (1)"\n', encoding="utf-8")
    hallados = _sentencias_sobre_cotizaciones(
        [tmp_path / "db" / "db.py", tmp_path / "otro.py"], tmp_path)
    razones = sorted(r for _, _, r in _lo_que_viola(hallados))
    assert razones == ["borra", "cambia anio", "cambia numero", "escribe fuera de db/db.py", "sin el candado en el WHERE"]


def test_la_frontera_de_la_guarda_no_lo_ve_a_proposito(tmp_path):
    (tmp_path / "f.py").write_text(
        'T = "cotizaciones"\nA = "UPDATE " + T + " SET numero = 1"\nB = f"DELETE FROM {T}"\n', encoding="utf-8")
    assert _sentencias_sobre_cotizaciones([tmp_path / "f.py"], tmp_path) == []


def test_la_funcion_que_escribe_pide_sesion_de_la_casa_antes_de_tocar_la_base():
    """La puerta de quién escribe: `puede_entrar` es lo PRIMERO que hace `guardar_cotizacion`, antes de
    abrir la conexión (la lista de quién entra no está tecleada acá: es la de `web.auth`)."""
    fuente = ast.parse((_ROOT / "db" / "db.py").read_text(encoding="utf-8"))
    f = next(n for n in ast.walk(fuente) if isinstance(n, ast.AsyncFunctionDef) and n.name == "guardar_cotizacion")
    texto = ast.get_source_segment((_ROOT / "db" / "db.py").read_text(encoding="utf-8"), f)
    assert texto.index("puede_entrar(chat_id)") < texto.index("pool.connection()")


def test_humo_corre_la_lectura_de_las_cotizaciones_contra_la_base_de_verdad():
    """Sus columnas y su JOIN solo se comprueban contra Postgres: `tools/humo.py` tiene que llamarla."""
    arbol = ast.parse((_ROOT / "tools" / "humo.py").read_text(encoding="utf-8"))
    llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "cotizaciones_de_proyectos"]
    assert llamadas, "tools/humo.py no corre db.cotizaciones_de_proyectos()"
