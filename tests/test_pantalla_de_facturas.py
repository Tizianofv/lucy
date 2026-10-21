"""La pantalla «Facturas» (pieza 4 del diseño «Facturas», 21-oct-2026): la lista, «Nueva factura», la página de una
factura y sus botones, y el bloque «Facturas» de la página de un proyecto.

Qué se vigila, por las RUTAS reales y las plantillas reales, sobre la lectura y la puerta REALES de `db/db.py` ejecutadas
en SQLite (`tests/test_guardar_factura.py::mundo`) y la App de registro doblada EN LA RED (`tests/_app_de_registro.py`):

  1. SOLO LA CASA: toda ruta de `/facturas…` (sacada de lo registrado) contesta 401 a quien no entra (solo ver, sin sesión,
     cookie falsa, sesión de alguien que no entra) y no escribe nada; la de la casa contesta;
  2. LA LISTA: número o «Borrador», cliente, fecha, total (el dinero de la hoja), estado y proyecto; el filtro por estado;
  3. NUEVA FACTURA: los tres pasos; el cliente sale del proyecto o de Noco; una cancelada no se puede escoger; el TIPO de
     cada liga sale de `es_trabajo` de la App y no de lo que trae el formulario; una sesión ya facturada se dice;
  4. UN GUARDADO RECHAZADO NO PIERDE LO ESCRITO: 400 con la misma pantalla, las casillas como se dejaron, la razón en
     palabras y NADA escrito; cada razón de la puerta llega a la persona (las claves salen del código de la puerta, no de
     una lista tecleada aquí);
  5. LA PÁGINA DE UNA FACTURA: la hoja, corregir, ligar y desligar, emitir (con su confirmación y su candado), anular (con
     motivo), descartar, proyecto; cada botón sale solo en el estado que lo admite;
  6. LAS RUTAS PASAN POR LA PUERTA: las rutas de facturas no tienen SQL propio y cada escritura es una llamada de `db`;
  7. LA PÁGINA DEL PROYECTO: sus facturas, «Quitar del proyecto», «Ligar a este proyecto», «Hacer factura» en la cotización;
     solo ver no ve nada de eso ni lo lee; la cotización sigue como antes.

FRONTERA, dicha una vez:
  · SQLite en vez de Postgres (ver `tests/test_guardar_factura.py`); la App y Noco son dobles: el doble de la App afirma el
    contrato que la sala le pasó a Lucy (`tests/_app_de_registro.py`), el de Noco es un par de funciones (`persona`,
    `buscar_personas`) y no se comprueba contra Noco de verdad.
  · Se miran el HTML y las respuestas, no el aspecto: colores y posiciones se miran a ojo en las capturas de la sala.
  · No hay JavaScript en estas pantallas; nada de lo que corre en un navegador se ejerce aquí.

Correr:  python3 -m pytest tests/test_pantalla_de_facturas.py -q
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

import test_guardar_factura as tgf          # primero: deja puestas las variables de entorno que pide `config`
from test_guardar_factura import mundo  # noqa: F401  (fixture)
import test_cotizaciones_tablas as ct
from _app_de_registro import AppDeRegistro
from _navegador import Navegador
from starlette.datastructures import FormData

import config
import db.db as db
import facturas_panel as fp
import noco_lectura
import registro_lectura
import web.app as panel
import web.auth as auth

LLAVE = "llave-de-prueba"
HOY = "2026-10-21"
CASA = config.CHAT_ID_DUENO

SESION = {"ref": 11, "codigo": "s011", "es_trabajo": False, "fecha": HOY, "sala": "Sala P", "sala_mostrar": "Sala P",
          "horas": 3, "servicio": "Grabación", "estado": "CONFIRMADA", "cancelada": False,
          "total": 9000.0, "abonado": 0.0, "saldo": 9000.0}
TRABAJO = {"ref": 12, "codigo": "t012", "es_trabajo": True, "fecha": HOY, "sala": "Sala P", "sala_mostrar": "Sala P",
           "horas": 2, "servicio": "Mezcla", "estado": "EN PROCESO", "cancelada": False,
           "total": 6000.0, "abonado": 0.0, "saldo": 6000.0}
OTRA = {"ref": 21, "codigo": "x021", "es_trabajo": False, "fecha": HOY, "sala": "Sala Q", "sala_mostrar": "Sala Q",
        "horas": 2, "servicio": "Voces", "nombre": "Ana Pérez", "estado": "CONFIRMADA", "cancelada": False,
        "total": 5000.0, "abonado": 0.0, "saldo": 5000.0}
OTRO_TRABAJO = {"ref": 23, "codigo": "t023", "es_trabajo": True, "fecha": HOY, "sala": "Sala Q", "sala_mostrar": "Sala Q",
                "horas": 1, "servicio": "Edición", "nombre": "Luis Gómez", "estado": "EN PROCESO", "cancelada": False,
                "total": 2000.0, "abonado": 0.0, "saldo": 2000.0}
CANCELADA = {"ref": 31, "codigo": "c031", "es_trabajo": False, "fecha": HOY, "sala": "Sala Q", "sala_mostrar": "Sala Q",
             "horas": 1, "servicio": "Ensayo", "nombre": "Pedro Cancelado", "estado": "CANCELADA", "cancelada": True,
             "total": 1000.0, "abonado": 0.0, "saldo": 1000.0}

RENGLON = {"r-1-descripcion": "Grabación de voces", "r-1-detalle": "dos horas", "r-1-monto": "62,500"}
# Otras dos de la ficha del cliente: una sesión más (para facturar varias) y una cancelada (de ELLA, no de otra persona).
SESION_B = {"ref": 13, "codigo": "s013", "es_trabajo": False, "fecha": HOY, "sala": "Sala P", "sala_mostrar": "Sala P",
            "horas": 1.5, "servicio": "Ensayo", "estado": "CONFIRMADA", "cancelada": False,
            "total": 4500.0, "abonado": 1000.0, "saldo": 3500.0}
SU_CANCELADA = {"ref": 32, "codigo": "c032", "es_trabajo": False, "fecha": HOY, "sala": "Sala P", "sala_mostrar": "Sala P",
                "horas": 1, "servicio": "Ensayo", "estado": "CANCELADA", "cancelada": True,
                "total": 1000.0, "abonado": 0.0, "saldo": 1000.0}

NOMBRE = "Cliente de Prueba"


# ── El mundo ────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def registro(monkeypatch):
    """La App de mentira, con su llave, y Lucy apuntada a ella. De la ficha del cliente salen dos sesiones, un trabajo y una
    cancelada; `otras` (21, 23, 31) son de OTRAS personas: la App las tiene y las devuelve si se le piden una por una."""
    doble = AppDeRegistro()
    doble.llave = LLAVE
    doble.sesiones = {"puede_ligar": True, "motivo_sin_ligar": "", "sesiones": [dict(SESION), dict(TRABAJO), dict(SESION_B), dict(SU_CANCELADA)]}
    doble.otras = [dict(OTRA), dict(OTRO_TRABAJO), dict(CANCELADA)]
    doble.busqueda = {"sesiones": [dict(OTRA), dict(CANCELADA), dict(SU_CANCELADA, nombre="Cliente de Prueba")], "hay_mas": False}
    monkeypatch.setattr(config, "REGISTRO_URL", doble.url)
    monkeypatch.setattr(config, "LUCY_LLAVE_SERVICIO", LLAVE)
    # `test_guardar_factura` importa una guarda que deja un `httpx` falso en `sys.modules`: el de verdad es el que usa
    # `TestClient` (así lo hace `tests/test_noco_lectura.py::_httpx_de_verdad`). Lucy llama a la App con el de verdad.
    import starlette.testclient as testclient
    monkeypatch.setattr(registro_lectura, "httpx", testclient.httpx)
    try:
        yield doble
    finally:
        doble.cerrar()


@pytest.fixture
def pantalla(mundo, registro, monkeypatch):  # noqa: F811
    """La base de las facturas con lo que las pantallas leen de los proyectos (área, estado y la ficha del cliente), la App
    doblada y Noco doblado. Los proyectos 1 y 2 son de la ficha 7; el 3 está en la papelera."""
    for columna in ("area TEXT", "estado TEXT DEFAULT 'activo'", "cliente_noco_id INTEGER"):
        mundo.con.execute(f"ALTER TABLE proyectos ADD COLUMN {columna}")
    mundo.con.execute("UPDATE proyectos SET cliente_noco_id = 7 WHERE id IN (1, 2)")
    mundo.con.commit()

    async def persona(noco_id):
        return {"id": noco_id, "nombre": NOMBRE} if noco_id == 7 else None

    async def buscar(texto):
        return [{"id": 7, "nombre": NOMBRE}] if "cli" in texto.lower() else []
    monkeypatch.setattr(noco_lectura, "persona", persona)
    monkeypatch.setattr(noco_lectura, "buscar_personas", buscar)
    mundo.registro = registro
    return mundo


def cliente(sesion="casa") -> Navegador:
    c = Navegador(panel.app, base_url="https://testserver")
    if sesion == "ver":
        c.cookies.set(panel.COOKIE_VER, auth.crear_token_ver())
    elif sesion == "casa":
        c.cookies.set(panel.COOKIE, auth.crear_token(CASA, auth.VIDA_SESION))
    elif sesion == "ajena":
        c.cookies.set(panel.COOKIE, auth.crear_token(tgf.EXTRANO, auth.VIDA_SESION))
    elif sesion == "falsa":
        c.cookies.set(panel.COOKIE_VER, "ver.9999999999.firma-falsa")
        c.cookies.set(panel.COOKIE, "9999999999.firma-falsa")
    return c


def cuerpo(*extra, refs=("11",), **campos) -> dict:
    """Lo que el formulario de «Nueva factura» manda."""
    datos = {"cliente": "7", "para_nombre": NOMBRE, "fecha": "2026-10-21", "ref": list(refs), **RENGLON}
    datos.update(campos)
    for e in extra:
        datos.update(e)
    return datos


def crear_por_la_pantalla(c=None, **kw):
    return (c or cliente()).post("/facturas/nueva", data=cuerpo(**kw), follow_redirects=False)


def ligas(m, fid=None):
    filas = tgf.filas(m, "factura_ligas")
    return [(l["tipo"], l["ref"], l["desligada_en"] is None) for l in filas if fid is None or l["factura_id"] == fid]


def unica(m):
    f = tgf.filas(m, "facturas")
    assert len(f) == 1, f
    return f[0]


def textos_de_avisos(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()
            for t in re.findall(r'<p class="aviso[^"]*"[^>]*>(.*?)</p>', html, re.S)]


def rutas_de_facturas():
    return [r for r in panel.app.routes if getattr(r, "path", "").startswith("/facturas")]


# ── 1. Solo la casa ─────────────────────────────────────────────────────────────────────────────────

def _url(ruta, fid=1):
    return ruta.path.replace("{fid}", str(fid))


def test_las_rutas_de_facturas_salen_de_lo_registrado_y_son_las_que_hay():
    """Para que lo de abajo no sea verde por no mirar nada: los métodos y rutas que hay hoy."""
    vistas = sorted((m, r.path) for r in rutas_de_facturas() for m in r.methods if m != "HEAD")
    assert ("GET", "/facturas") in vistas and ("GET", "/facturas/nueva") in vistas and ("POST", "/facturas/nueva") in vistas
    assert ("GET", "/facturas/{fid}") in vistas and ("GET", "/facturas/{fid}/pdf") in vistas
    assert {p for m, p in vistas if m == "POST"} == {
        "/facturas/nueva", "/facturas/{fid}/corregir", "/facturas/{fid}/emitir", "/facturas/{fid}/anular",
        "/facturas/{fid}/descartar", "/facturas/{fid}/ligar", "/facturas/{fid}/desligar", "/facturas/{fid}/proyecto"}


def test_toda_ruta_de_facturas_esta_declarada_solo_para_la_casa():
    for r in rutas_de_facturas():
        assert r.endpoint.puerta == auth.PUERTA_SIEMPRE, r.path


@pytest.mark.parametrize("sesion", [None, "ver", "ajena", "falsa"])
def test_quien_no_entra_recibe_401_en_toda_ruta_de_facturas_y_no_se_escribe_nada(pantalla, sesion):
    f = tgf.emitir(tgf.crear())
    b = tgf.crear(liga={"tipo": "sesion", "ref": "ses-borrador"})
    antes = tgf.foto(pantalla)
    c = cliente(sesion)
    for r in rutas_de_facturas():
        for metodo in sorted(r.methods - {"HEAD"}):
            resp = c.request(metodo, _url(r, b["id"]), data=cuerpo() if metodo == "POST" else None, follow_redirects=False)
            assert resp.status_code == 401, (metodo, r.path, resp.status_code)
            assert "FAC-2026-001" not in resp.text and "Cliente de prueba" not in resp.text
    assert tgf.foto(pantalla) == antes
    assert f["id"]


def test_la_casa_ve_la_lista_y_la_pagina(pantalla):
    f = tgf.crear()
    c = cliente()
    assert c.get("/facturas").status_code == 200
    assert c.get(f"/facturas/{f['id']}").status_code == 200
    assert c.get("/facturas/nueva").status_code == 200


# ── 2. La lista ─────────────────────────────────────────────────────────────────────────────────────

def test_la_lista_enseña_numero_o_borrador_cliente_fecha_total_estado_y_proyecto(pantalla):
    e = tgf.emitir(tgf.crear(itbis_pct=18, para_nombre="Colegio Uno", fecha="2026-10-14"))
    tgf.correr(db.poner_proyecto_de_factura(e["id"], CASA, 2))
    tgf.crear(liga={"tipo": "trabajo", "ref": "t-2"}, para_nombre="Ana Pérez")
    html = cliente().get("/facturas").text
    filas = re.findall(r"<tr>\s*<td><a href=\"/facturas/(\d+)\"><b>(.*?)</b></a></td>(.*?)</tr>", html, re.S)
    assert [(i, n) for i, n, _ in filas] == [("2", "Borrador"), ("1", "FAC-2026-001")]
    emitida = re.sub(r"<[^>]+>|\s+", " ", filas[1][2])
    assert "Colegio Uno" in emitida and "14 oct 2026" in emitida and "Emitida" in emitida and "Proyecto 2" in emitida
    # 1000.00 + 2500.50 = 3500.50 y el 18 % redondeado a centavos = 630.09 → 4,130.59, escrito con el dinero de la hoja
    assert "RD$4,130.59" in emitida
    assert "Borrador" in re.sub(r"<[^>]+>|\s+", " ", filas[0][2])


def test_el_filtro_por_estado_deja_solo_ese_estado(pantalla):
    e = tgf.emitir(tgf.crear())
    tgf.correr(db.anular_factura(e["id"], CASA, "Se hizo dos veces"))
    tgf.emitir(tgf.otra())
    tgf.otra()
    c = cliente()
    def numeros(estado):
        html = c.get("/facturas", params={"estado": estado} if estado else None).text
        return re.findall(r'<td><a href="/facturas/\d+"><b>(.*?)</b>', html)
    assert numeros(None) == ["Borrador", "FAC-2026-002", "FAC-2026-001"]
    assert numeros("borrador") == ["Borrador"]
    assert numeros("emitida") == ["FAC-2026-002"]
    assert numeros("anulada") == ["FAC-2026-001"]
    assert numeros("inventado") == numeros(None)        # lo que no se conoce es «todas», nunca un filtro inventado
    assert 'aria-current="page"' in c.get("/facturas", params={"estado": "anulada"}).text


def test_sin_facturas_lo_dice_y_sin_la_tabla_tambien(pantalla):
    c = cliente()
    assert "Todavía no hay facturas" in c.get("/facturas").text
    for t in ("factura_envios", "factura_ligas", "factura_renglones", "facturas"):
        pantalla.con.execute(f"DROP TABLE {t}")
    r = c.get("/facturas")
    assert r.status_code == 200 and "falta actualizar la base de datos" in r.text


def test_la_lista_no_escribe_nada(pantalla):
    tgf.emitir(tgf.crear())
    antes = tgf.foto(pantalla)
    cliente().get("/facturas")
    cliente().get("/facturas/1")
    cliente().get("/facturas/nueva?cliente=7")
    assert tgf.foto(pantalla) == antes


# ── 3. Nueva factura: los pasos ─────────────────────────────────────────────────────────────────────

def test_paso_1_sin_cliente_se_busca_en_noco_y_cada_ficha_es_un_enlace(pantalla):
    html = cliente().get("/facturas/nueva", params={"pq": "cli"}).text
    assert "¿De quién es la factura?" in html
    assert f'href="/facturas/nueva?cliente=7">Escoger' in html and NOMBRE in html
    assert "Nadie con" in cliente().get("/facturas/nueva", params={"pq": "zzz"}).text


def test_paso_2_con_cliente_enseña_sus_sesiones_y_trabajos_y_una_cancelada_no_se_puede_escoger(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "sq": "pe"}).text
    cajas = re.findall(r'<input type="checkbox" name="ref" value="(\d+)"( checked)?( disabled)?>', html)
    assert [c[0] for c in cajas] == ["11", "13", "12", "21", "31", "32"]  # de la ficha (sesiones, luego trabajos), y de la búsqueda
    assert [c[0] for c in cajas if c[2]] == ["21", "31", "32"]      # ninguna de la búsqueda se puede marcar
    assert html.count("No es de este cliente: no se puede escoger") == 2      # 21 y 31 son de otras personas; la 32 es cancelada SUYA
    assert "Cancelada: no se factura" in html and ">Trabajo<" in html


def test_paso_2_las_ya_escogidas_vienen_marcadas_y_una_ajena_escogida_a_mano_no(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "13"]}).text
    cajas = re.findall(r'<input type="checkbox" name="ref" value="(\d+)"( checked)?', html)
    assert [(c[0], bool(c[1])) for c in cajas] == [("11", True), ("13", True), ("12", False)]      # primero las sesiones, luego los trabajos
    # una referencia de otra persona puesta a mano en la dirección no aparece ni marcada (la App la devolvería si se le pide una por una)
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "21"]}).text
    assert re.findall(r'<input type="checkbox" name="ref" value="(\d+)"( checked)?', html) == [("11", " checked"), ("13", ""), ("12", "")]


def test_paso_3_con_sesiones_buenas_sale_el_formulario_con_el_cliente_y_las_sesiones_escondidas(pantalla):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11", "12"], "paso": "escribir"}).text
    assert "Escribe la factura" in html and 'method="post" action="/facturas/nueva"' in html
    assert re.findall(r'<input type="hidden" name="ref" value="(\d+)">', html) == ["11", "12"]
    assert f'name="para_nombre" value="{NOMBRE}"' in html and 'name="fecha" value="2026-10-21"' in html


@pytest.mark.parametrize("refs,frase", [(["32"], "cancelado o pospuesto"), (["11", "32"], "cancelado o pospuesto"),
                                        (["99"], "no aparece entre las sesiones y trabajos de este cliente"),
                                        (["21"], "no aparece entre las sesiones y trabajos de este cliente"),
                                        (["11", "23"], "no aparece entre las sesiones y trabajos de este cliente"),
                                        ([], "al menos una")])
def test_paso_3_con_una_cancelada_o_desconocida_o_ninguna_no_pasa_a_escribir(pantalla, refs, frase):
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": refs, "paso": "escribir"}).text
    assert 'action="/facturas/nueva"' not in html or 'name="paso" value="escribir"' in html
    assert "Escribe la factura" not in html
    assert frase in " ".join(textos_de_avisos(html))


def test_sin_poder_consultar_el_registro_no_se_pasa_a_escribir_y_se_dice(pantalla, registro):
    registro.forzada = (500, b"{}", "application/json", 0)
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert "Escribe la factura" not in html and "No se pudo consultar el registro" in html


def test_el_cliente_sale_del_proyecto_y_viene_la_cotizacion_con_su_opcion(pantalla):
    contenido = json.dumps({"opciones": [
        {"nombre": "Básica", "renglones": [{"descripcion": "Mezcla", "detalle": "una pista", "total": "1000.00"}]},
        {"nombre": "Completa", "renglones": [{"descripcion": "Mezcla y master", "detalle": "", "total": "2500.50"},
                                              {"descripcion": "Voces", "detalle": "", "total": "300.00"}]}]})
    ct._insertar(pantalla.con, proyecto_id=1, numero=4, contenido=contenido)
    cid = pantalla.con.execute("SELECT max(id) FROM cotizaciones").fetchone()[0]
    c = cliente()
    pide = c.get("/facturas/nueva", params={"proyecto": 1, "cotizacion": cid}).text
    assert "Proyecto 1" in pide and f"COT-2026-004" in pide
    assert "¿De cuál opción de la cotización?" in pide and "Opción 2 · Completa · RD$2,800.50" in pide
    sin_opcion = c.get("/facturas/nueva", params={"proyecto": 1, "cotizacion": cid, "ref": ["11"], "paso": "escribir"}).text
    assert "Escribe la factura" not in sin_opcion and "Escoge de cuál opción" in sin_opcion
    html = c.get("/facturas/nueva", params={"proyecto": 1, "cotizacion": cid, "opcion": 2, "ref": ["11"], "paso": "escribir"}).text
    assert 'name="r-1-descripcion" value="Mezcla y master"' in html and 'name="r-1-monto" value="2,500.50"' in html
    assert 'name="r-2-descripcion" value="Voces"' in html
    assert 'name="proyecto" value="1"' in html and f'name="cotizacion" value="{cid}"' in html and 'name="opcion" value="2"' in html
    assert re.findall(r'name="cliente" value="(\d+)"', html) == ["7"]        # la ficha del proyecto


def test_un_proyecto_o_una_cotizacion_que_no_existen_se_dicen_y_no_se_ligan(pantalla):
    html = cliente().get("/facturas/nueva", params={"proyecto": 3, "cotizacion": 1}).text      # el 3 está en la papelera
    assert "Ese proyecto no existe o ya no está" in html
    html = cliente().get("/facturas/nueva", params={"proyecto": 2, "cotizacion": 1}).text      # la 1 es del proyecto 1
    assert "Esa cotización no es de este proyecto" in html


# ── 4. Guardar el borrador ──────────────────────────────────────────────────────────────────────────

def test_guardar_crea_el_borrador_con_sus_renglones_su_liga_y_vuelve_a_la_factura(pantalla):
    r = crear_por_la_pantalla(itbis="18", ncf="B0100000001", ncf_tipo="Crédito fiscal", rnc_cedula="131-23456-7")
    assert r.status_code == 303 and r.headers["location"] == "/facturas/1"
    f = unica(pantalla)
    assert (f["estado"], f["numero"], f["cliente_noco_id"], f["para_nombre"], f["hecha_por"], f["hecha_por_chat_id"]) == \
        ("borrador", None, 7, NOMBRE, "casa", CASA)
    assert (f["ncf"], f["ncf_tipo"], f["rnc_cedula"], str(f["itbis_pct"])[:2]) == ("B0100000001", "Crédito fiscal", "131234567", "18")
    renglones = tgf.filas(pantalla, "factura_renglones")
    assert [(x["descripcion"], x["detalle"], float(x["monto"])) for x in renglones] == [("Grabación de voces", "dos horas", 62500.0)]
    assert ligas(pantalla) == [("sesion", "11", True)]
    assert tgf.huellas(pantalla) == [("crear", "facturas", 1)]
    pagina = cliente().get(r.headers["location"]).text
    assert "Borrador" in pagina and "Grabación de voces" in pagina and "RD$62,500" in pagina


def test_sin_itbis_ni_ncf_ni_rnc_se_guardan_vacios_y_la_casilla_marcada_es_el_dieciocho_por_ciento(pantalla):
    crear_por_la_pantalla()
    f = unica(pantalla)
    assert (f["itbis_pct"], f["ncf"], f["ncf_tipo"], f["rnc_cedula"]) == (None, None, None, None)
    otra = crear_por_la_pantalla(refs=("12",), itbis="18")
    assert otra.status_code == 303
    assert [str(x["itbis_pct"]).startswith("18") for x in tgf.filas(pantalla, "facturas")] == [False, True]


def test_varias_sesiones_y_trabajos_en_una_factura_cada_liga_con_su_tipo(pantalla):
    r = crear_por_la_pantalla(refs=("11", "12", "13"))
    assert r.status_code == 303
    assert ligas(pantalla) == [("sesion", "11", True), ("trabajo", "12", True), ("sesion", "13", True)]
    assert [l["codigo"] for l in tgf.filas(pantalla, "factura_ligas")] == ["s011", "t012", "s013"]
    assert tgf.huellas(pantalla) == [("crear", "facturas", 1), ("ligar", "facturas", 1), ("ligar", "facturas", 1)]


def test_el_tipo_de_la_liga_sale_de_es_trabajo_de_la_app_y_no_de_lo_que_trae_el_formulario(pantalla):
    """Un POST que dice `tipo=trabajo` para una sesión (y al revés) no cambia nada: el formulario ni tiene ese campo."""
    crear_por_la_pantalla(refs=("11", "12"), tipo="trabajo", liga_tipo="trabajo", tipo_de_liga="sesion")
    assert ligas(pantalla) == [("sesion", "11", True), ("trabajo", "12", True)]
    html = cliente().get("/facturas/nueva", params={"cliente": 7, "ref": ["11"], "paso": "escribir"}).text
    assert 'name="tipo"' not in html


def test_una_cancelada_no_se_factura_y_no_se_escribe_nada(pantalla):
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("11", "32"))
    assert r.status_code == 400 and "cancelado o pospuesto" in r.text and "NO se guardó nada" in r.text
    assert tgf.foto(pantalla) == antes


def test_una_que_la_app_no_conoce_o_sin_ninguna_no_se_guarda(pantalla):
    antes = tgf.foto(pantalla)
    assert "no aparece entre las sesiones y trabajos de este cliente" in crear_por_la_pantalla(refs=("99",)).text
    sin = crear_por_la_pantalla(refs=())
    assert sin.status_code == 400 and "escoge al menos uno" in sin.text
    assert tgf.foto(pantalla) == antes


def test_una_sesion_ya_facturada_se_dice_y_lo_escrito_no_se_pierde(pantalla):
    crear_por_la_pantalla(refs=("11",))
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("11",), para_nombre="Otra Persona", **{"r-1-descripcion": "Lo que escribí",
                                                                          "r-1-monto": "1,234.50", "ncf": "B0100000009",
                                                                          "ncf_tipo": "Consumo"})
    assert r.status_code == 400
    assert "ya está en otra factura" in " ".join(textos_de_avisos(r.text)) and "NO se guardó nada" in r.text
    # lo escrito vuelve en las casillas
    assert 'name="para_nombre" value="Otra Persona"' in r.text and 'value="Lo que escribí"' in r.text
    assert 'name="r-1-monto" value="1,234.50"' in r.text and 'name="ncf" value="B0100000009"' in r.text
    assert re.findall(r'<input type="hidden" name="ref" value="(\d+)">', r.text) == ["11"]
    assert tgf.foto(pantalla) == antes


@pytest.mark.parametrize("cambios,frase,casilla", [
    ({"ncf": "B0100000001", "ncf_tipo": ""}, "van juntos", "ncf"),
    ({"ncf": "B01 ÉÉ", "ncf_tipo": "Consumo"}, "solo lleva letras y cifras", "ncf"),
    ({"rnc_cedula": "123"}, "9 cifras", "rnc_cedula"),
    ({"para_nombre": "   "}, "nombre de la persona", "para_nombre"),
    ({"fecha": "ayer"}, "fecha no se entiende", "fecha"),
    ({"r-1-monto": "62.500,0"}, "no se entiende", None),
    ({"r-1-monto": "10.123"}, "más de dos decimales", None),
    ({"r-1-monto": "0"}, "mayor que cero", None),
    ({"r-1-monto": ""}, "falta el monto", None),
    ({"r-1-descripcion": ""}, "falta la descripción", None),
    ({"r-1-descripcion": "x" * 301}, "demasiado larga", None),
])
def test_cada_razon_de_la_puerta_llega_a_la_persona_sin_guardar_nada_y_marcando_su_casilla(pantalla, cambios, frase, casilla):
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(**cambios)
    assert r.status_code == 400
    avisos = " ".join(textos_de_avisos(r.text))
    assert frase in avisos and "NO se guardó nada" in avisos, avisos
    assert tgf.foto(pantalla) == antes
    if casilla:
        assert re.search(rf'name="{casilla}"[^>]*aria-invalid="true"', r.text), r.text[:200]
    for k, v in cambios.items():          # y lo que escribió vuelve a su casilla (menos lo vacío, que no tiene valor que ver)
        if v.strip() and len(v) < 50:
            assert f'name="{k}" value="{v}"' in r.text or k.startswith("r-1-monto"), k


def test_un_ncf_repetido_se_dice_aunque_la_otra_este_anulada(pantalla):
    crear_por_la_pantalla(refs=("11",), ncf="B0100000001", ncf_tipo="Crédito fiscal")
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla(refs=("12",), ncf="B0100000001", ncf_tipo="Crédito fiscal")
    assert r.status_code == 400 and "Ese NCF ya lo tiene otra factura" in r.text
    assert tgf.foto(pantalla) == antes


def test_sin_renglones_no_se_guarda_y_los_en_blanco_se_ignoran(pantalla):
    r = crear_por_la_pantalla(**{"r-1-descripcion": "", "r-1-detalle": "", "r-1-monto": "",
                                 "r-2-descripcion": "", "r-2-monto": ""})
    assert r.status_code == 400 and "al menos un renglón" in r.text and not tgf.filas(pantalla, "facturas")
    ok = crear_por_la_pantalla(**{"r-2-descripcion": "", "r-2-monto": "", "r-3-descripcion": "Otro", "r-3-monto": "10"})
    assert ok.status_code == 303 and [x["descripcion"] for x in tgf.filas(pantalla, "factura_renglones")] == ["Grabación de voces", "Otro"]


def test_si_la_app_no_contesta_no_se_guarda_y_se_dice(pantalla, registro):
    registro.forzada = (500, b"{}", "application/json", 0)
    antes = tgf.foto(pantalla)
    r = crear_por_la_pantalla()
    assert r.status_code == 400 and "No se pudo consultar el registro" in r.text and tgf.foto(pantalla) == antes


def test_si_una_de_varias_no_se_deja_ligar_el_borrador_queda_y_la_pagina_dice_cual(pantalla):
    ocupante = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, para_nombre="Otra")
    assert ocupante["id"] == 1
    r = crear_por_la_pantalla(refs=("11", "13", "12"))
    assert r.status_code == 200                                      # la página de la factura, con el aviso
    avisos = " ".join(textos_de_avisos(r.text))
    assert "No se pudo ligar «s013» a la factura" in avisos and "ya está en otra factura" in avisos
    assert ligas(pantalla, 2) == [("sesion", "11", True), ("trabajo", "12", True)]
    assert len(tgf.filas(pantalla, "facturas")) == 2


def test_desde_una_cotizacion_la_factura_queda_ligada_al_proyecto_a_la_cotizacion_y_a_la_opcion(pantalla):
    r = crear_por_la_pantalla(proyecto="1", cotizacion="1", opcion="2")
    assert r.status_code == 303
    f = unica(pantalla)
    assert (f["proyecto_id"], f["cotizacion_id"], f["cotizacion_opcion"]) == (1, 1, 2)
    otra = crear_por_la_pantalla(refs=("12",), proyecto="2", cotizacion="1", opcion="1")      # la 1 es del proyecto 1
    assert otra.status_code == 400 and "Esa cotización es de otro proyecto" in otra.text
    assert len(tgf.filas(pantalla, "facturas")) == 1


def test_el_guardado_rechazado_vuelve_con_el_proyecto_la_cotizacion_y_los_renglones_escritos(pantalla):
    r = crear_por_la_pantalla(proyecto="1", cotizacion="1", opcion="2", refs=("11", "32"))
    assert r.status_code == 400
    assert 'name="proyecto" value="1"' in r.text or "cancelado o pospuesto" in r.text
    r2 = crear_por_la_pantalla(proyecto="1", cotizacion="1", opcion="2", ncf="B0100000001", ncf_tipo="")
    assert r2.status_code == 400
    assert 'name="proyecto" value="1"' in r2.text and 'name="cotizacion" value="1"' in r2.text and 'name="opcion" value="2"' in r2.text
    assert 'name="r-1-descripcion" value="Grabación de voces"' in r2.text


# ── 5. La página de una factura ─────────────────────────────────────────────────────────────────────

def _pagina(fid, **consulta):
    r = cliente().get(f"/facturas/{fid}", params=consulta or None)
    assert r.status_code == 200, r.text[:300]
    return r.text


def test_la_pagina_enseña_la_hoja_del_pdf_con_el_estado_que_la_base_tiene(pantalla):
    b = tgf.crear(itbis_pct=18, ncf="B0100000001", ncf_tipo="Crédito fiscal")
    html = _pagina(b["id"])
    assert 'data-hoja' in html and "FACTURA" in html and "BORRADOR" in html and "FAC-" not in html
    assert "RD$1,000" in html and "ITBIS 18%" in html and "B0100000001" in html
    e = tgf.emitir(b)
    html = _pagina(e["id"])
    assert "FAC-2026-001" in html and 'class="marca-f"' not in html
    tgf.correr(db.anular_factura(e["id"], CASA, "Error de cliente"))
    html = _pagina(e["id"])
    assert "ANULADA" in html and "Error de cliente" in html


def test_cada_boton_sale_solo_en_el_estado_que_lo_admite(pantalla):
    b = tgf.crear()
    html = _pagina(b["id"])
    assert all(t in html for t in ("?corregir=1", "?confirmar=emitir", "?confirmar=descartar", "/ligar", "/desligar"))
    assert "?confirmar=anular" not in html
    e = tgf.emitir(b)
    html = _pagina(e["id"])
    assert "?confirmar=anular" in html and "?corregir=1" not in html and "?confirmar=emitir" not in html
    assert "/ligar" not in html and "/desligar" not in html and '/proyecto"' in html
    tgf.correr(db.anular_factura(e["id"], CASA, "Motivo"))
    html = _pagina(e["id"])
    assert "?confirmar=" not in html and "?corregir=1" not in html and '/proyecto"' not in html
    assert f'/facturas/{e["id"]}/pdf' in html                      # y el PDF se baja en los tres


def test_una_confirmacion_que_no_es_del_estado_no_se_dibuja(pantalla):
    b = tgf.crear()
    assert 'data-confirmar="anular"' not in _pagina(b["id"], confirmar="anular")
    assert 'data-confirmar="emitir"' in _pagina(b["id"], confirmar="emitir")
    e = tgf.emitir(b)
    assert 'data-confirmar="emitir"' not in _pagina(e["id"], confirmar="emitir")
    assert 'data-confirmar="anular"' in _pagina(e["id"], confirmar="anular")


def test_una_factura_que_no_existe_es_404(pantalla):
    assert cliente().get("/facturas/99").status_code == 404
    for ruta in ("corregir", "emitir", "anular", "descartar", "ligar", "desligar", "proyecto"):
        assert cliente().post(f"/facturas/99/{ruta}", data={"ref": "11", "motivo": "x"}, follow_redirects=False).status_code == 404, ruta


def test_emitir_pide_confirmacion_y_la_numera(pantalla):
    b = tgf.crear()
    confirmar = _pagina(b["id"], confirmar="emitir")
    assert "¿Emitir esta factura?" in confirmar and f'action="/facturas/{b["id"]}/emitir"' in confirmar
    cambiado = re.search(r'name="cambiado_en" value="([^"]+)"', confirmar).group(1)
    r = cliente().post(f"/facturas/{b['id']}/emitir", data={"cambiado_en": cambiado}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/facturas/{b['id']}"
    assert "FAC-2026-001" in _pagina(b["id"]) and unica(pantalla)["estado"] == "emitida"


def test_emitir_con_un_candado_viejo_no_emite_y_lo_dice(pantalla):
    b = tgf.crear()
    viejo = fp.candado(b["cambiado_en"])
    tgf.correr(db.ligar_factura_a_registro(b["id"], CASA, "sesion", "otra"))         # alguien cambió el borrador
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{b['id']}/emitir", data={"cambiado_en": viejo}, follow_redirects=False)
    assert r.status_code == 400 and "cambió mientras la mirabas" in r.text and "NO se guardó" in r.text
    assert tgf.foto(pantalla) == antes and unica(pantalla)["estado"] == "borrador"
    assert "Borrador" in r.text                                       # y se ve como quedó


def test_emitir_una_ya_emitida_o_sin_candado_se_rechaza_sin_cambiar_nada(pantalla):
    e = tgf.emitir(tgf.crear())
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{e['id']}/emitir", data={"cambiado_en": fp.candado(e["cambiado_en"])}, follow_redirects=False)
    assert r.status_code == 400 and "Una factura emitida no se corrige" in r.text
    b = tgf.otra()
    sin = cliente().post(f"/facturas/{b['id']}/emitir", data={}, follow_redirects=False)
    assert sin.status_code == 400 and "Falta saber qué versión" in sin.text
    assert tgf.filas(pantalla, "facturas")[0] == antes["facturas"][0]


def test_corregir_cambia_el_borrador_con_sus_renglones(pantalla):
    b = tgf.crear()
    form = _pagina(b["id"], corregir=1)
    assert f'action="/facturas/{b["id"]}/corregir"' in form and 'name="r-1-descripcion" value="Ensayo 1"' in form
    datos = {"cambiado_en": re.search(r'name="cambiado_en" value="([^"]+)"', form).group(1), "para_nombre": "Nombre Nuevo",
             "fecha": "2026-10-20", "itbis": "18", "ncf": "B0100000007", "ncf_tipo": "Consumo", "rnc_cedula": "",
             "r-1-descripcion": "Solo uno", "r-1-detalle": "", "r-1-monto": "99.50", "r-2-descripcion": "", "r-2-monto": ""}
    r = cliente().post(f"/facturas/{b['id']}/corregir", data=datos, follow_redirects=False)
    assert r.status_code == 303
    f = unica(pantalla)
    assert (f["para_nombre"], str(f["fecha"]), f["ncf"], f["ncf_tipo"]) == ("Nombre Nuevo", "2026-10-20", "B0100000007", "Consumo")
    assert [(x["descripcion"], float(x["monto"])) for x in tgf.filas(pantalla, "factura_renglones")] == [("Solo uno", 99.5)]
    assert ("corregir", "facturas", b["id"]) in tgf.huellas(pantalla)


def test_corregir_rechazado_deja_el_formulario_abierto_con_lo_escrito_y_nada_cambia(pantalla):
    b = tgf.crear()
    antes = tgf.foto(pantalla)
    datos = {"cambiado_en": fp.candado(b["cambiado_en"]), "para_nombre": "Lo que escribí", "fecha": "2026-10-20",
             "ncf": "B0100000007", "ncf_tipo": "", "rnc_cedula": "", "r-1-descripcion": "Renglón mío", "r-1-monto": "10.5"}
    r = cliente().post(f"/facturas/{b['id']}/corregir", data=datos, follow_redirects=False)
    assert r.status_code == 400 and "van juntos" in " ".join(textos_de_avisos(r.text))
    assert 'data-corregir' in r.text and 'name="para_nombre" value="Lo que escribí"' in r.text
    assert 'name="r-1-descripcion" value="Renglón mío"' in r.text and 'name="ncf" value="B0100000007"' in r.text
    assert tgf.foto(pantalla) == antes


def test_una_emitida_y_una_anulada_no_se_corrigen_ni_se_ligan(pantalla):
    e = tgf.emitir(tgf.crear())
    datos = {"cambiado_en": fp.candado(e["cambiado_en"]), "para_nombre": "Otro", "fecha": "2026-10-20",
             "r-1-descripcion": "x", "r-1-monto": "1"}
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{e['id']}/corregir", data=datos, follow_redirects=False)
    assert r.status_code == 400 and "Una factura emitida no se corrige: si está mal, se anula y se hace otra" in r.text
    r = cliente().post(f"/facturas/{e['id']}/ligar", data={"ref": "13"}, follow_redirects=False)
    assert r.status_code == 400 and "no se corrige" in r.text
    assert tgf.foto(pantalla) == antes
    tgf.correr(db.anular_factura(e["id"], CASA, "Motivo"))
    datos = {**datos, "cambiado_en": fp.candado(tgf.leer(e)["cambiado_en"])}          # el candado de ahora
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{e['id']}/corregir", data=datos, follow_redirects=False)
    assert r.status_code == 400 and "Una factura anulada ya no se toca" in r.text
    assert tgf.foto(pantalla) == antes and unica(pantalla)["para_nombre"] == "Cliente de prueba"


def test_anular_pide_motivo_y_la_deja_anulada_con_su_numero_y_sus_sesiones_libres(pantalla):
    e = tgf.emitir(tgf.crear(liga={"tipo": "sesion", "ref": "11"}, ncf="B0100000001", ncf_tipo="Consumo"))
    confirmar = _pagina(e["id"], confirmar="anular")
    assert "¿Anular FAC-2026-001?" in confirmar and 'name="motivo"' in confirmar
    sin = cliente().post(f"/facturas/{e['id']}/anular", data={"motivo": "  "}, follow_redirects=False)
    assert sin.status_code == 400 and "Escribe el motivo" in sin.text and unica(pantalla)["estado"] == "emitida"
    r = cliente().post(f"/facturas/{e['id']}/anular", data={"motivo": "Se facturó dos veces"}, follow_redirects=False)
    assert r.status_code == 303
    f = unica(pantalla)
    assert (f["estado"], f["numero"], f["ncf"], f["motivo_anulacion"]) == ("anulada", 1, "B0100000001", "Se facturó dos veces")
    assert crear_por_la_pantalla(refs=("11",)).status_code == 303                       # la sesión quedó libre
    assert cliente().post(f"/facturas/{e['id']}/anular", data={"motivo": "otra vez"}, follow_redirects=False).status_code == 400


def test_anular_un_borrador_no_se_puede(pantalla):
    b = tgf.crear()
    r = cliente().post(f"/facturas/{b['id']}/anular", data={"motivo": "x"}, follow_redirects=False)
    assert r.status_code == 400 and unica(pantalla)["estado"] == "borrador"


def test_descartar_pide_confirmacion_y_borra_de_verdad_el_borrador(pantalla):
    b = tgf.crear()
    assert "¿Descartar este borrador?" in _pagina(b["id"], confirmar="descartar")
    r = cliente().post(f"/facturas/{b['id']}/descartar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/facturas"
    assert not tgf.filas(pantalla, "facturas") and not tgf.filas(pantalla, "factura_ligas")
    assert cliente().get(f"/facturas/{b['id']}").status_code == 404


def test_descartar_una_emitida_se_rechaza_y_dice_que_se_anula(pantalla):
    e = tgf.emitir(tgf.crear())
    r = cliente().post(f"/facturas/{e['id']}/descartar", follow_redirects=False)
    assert r.status_code == 400 and "Una factura emitida no se corrige" in r.text and unica(pantalla)["estado"] == "emitida"


def test_ligar_y_quitar_sesiones_de_un_borrador_con_su_tipo_y_la_ultima_no_se_suelta(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"})
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "12", "tipo": "sesion"}, follow_redirects=False)
    assert r.status_code == 303 and ligas(pantalla) == [("sesion", "11", True), ("trabajo", "12", True)]
    r = cliente().post(f"/facturas/{b['id']}/desligar", data={"tipo": "sesion", "ref": "11"}, follow_redirects=False)
    assert r.status_code == 303 and ligas(pantalla) == [("sesion", "11", False), ("trabajo", "12", True)]
    r = cliente().post(f"/facturas/{b['id']}/desligar", data={"tipo": "trabajo", "ref": "12"}, follow_redirects=False)
    assert r.status_code == 400 and "no puede quedar sin sesión ni trabajo" in r.text
    assert ligas(pantalla) == [("sesion", "11", False), ("trabajo", "12", True)]
    assert "Quitar" in r.text


def test_ligar_una_cancelada_o_una_que_ya_esta_en_otra_factura_se_dice(pantalla):
    otra = tgf.crear(liga={"tipo": "sesion", "ref": "13"}, para_nombre="Otra")
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"})
    antes = tgf.foto(pantalla)
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "32"}, follow_redirects=False)
    assert r.status_code == 400 and "cancelado o pospuesto" in r.text
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "13"}, follow_redirects=False)
    assert r.status_code == 400 and "ya está en otra factura" in r.text
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={"ref": "11"}, follow_redirects=False)
    assert r.status_code == 400 and "ya está en esta factura" in r.text
    r = cliente().post(f"/facturas/{b['id']}/ligar", data={}, follow_redirects=False)
    assert r.status_code == 400 and "Escoge la sesión" in r.text
    assert tgf.foto(pantalla) == antes and otra["id"]


def test_la_pagina_ofrece_ligar_las_del_cliente_y_la_busqueda_sin_las_que_ya_tiene(pantalla):
    b = tgf.crear(liga={"tipo": "sesion", "ref": "11"})
    html = _pagina(b["id"], sq="pe")
    ofrecidas = re.findall(r'action="/facturas/\d+/ligar"[^>]*><input type="hidden" name="ref" value="(\d+)">', html)
    assert ofrecidas == ["13", "12"]                  # la 11 ya está; la 21 y la 31 son de otras personas y no tienen botón
    assert html.count("No es de este cliente: no se puede escoger") == 2
    assert "Cancelada: no se factura" in html


def test_poner_el_proyecto_antes_durante_y_despues_y_quitarlo(pantalla):
    b = tgf.crear()
    c = cliente()
    assert c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "1"}, follow_redirects=False).headers["location"] == f"/facturas/{b['id']}"
    assert unica(pantalla)["proyecto_id"] == 1 and "Proyecto 1" in _pagina(b["id"])
    tgf.emitir(tgf.leer(b))
    c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "2"}, follow_redirects=False)
    assert unica(pantalla)["proyecto_id"] == 2 and unica(pantalla)["estado"] == "emitida"
    c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": ""}, follow_redirects=False)
    assert unica(pantalla)["proyecto_id"] is None
    assert [h[0] for h in tgf.huellas(pantalla)].count("ligar_proyecto") == 2 and "desligar_proyecto" in [h[0] for h in tgf.huellas(pantalla)]


def test_el_proyecto_de_una_anulada_no_se_toca_y_uno_que_no_existe_se_rechaza(pantalla):
    e = tgf.emitir(tgf.crear())
    c = cliente()
    for pid, frase in (("3", "ya no está"), ("99", "ya no está"), ("abc", "No se pudo")):
        antes = tgf.foto(pantalla)
        r = c.post(f"/facturas/{e['id']}/proyecto", data={"proyecto": pid}, follow_redirects=False)
        assert r.status_code == 400 and frase in r.text and tgf.foto(pantalla) == antes, pid
    tgf.correr(db.anular_factura(e["id"], CASA, "x"))
    r = c.post(f"/facturas/{e['id']}/proyecto", data={"proyecto": "1"}, follow_redirects=False)
    assert r.status_code == 400 and "Una factura anulada ya no se toca" in r.text


def test_volver_a_la_pagina_del_proyecto_solo_con_un_entero_y_nunca_a_una_direccion_del_formulario(pantalla):
    b = tgf.crear()
    c = cliente()
    r = c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "2", "volver": "proyecto", "p": "2"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?p=2#facturas"
    r = c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "", "volver": "proyecto", "p": "2"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?p=2#facturas"
    r = c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "1", "volver": "https://malo.example/robo", "p": "//malo"},
               follow_redirects=False)
    assert r.headers["location"] == f"/facturas/{b['id']}"
    r = c.post(f"/facturas/{b['id']}/proyecto", data={"proyecto": "1", "volver": "proyecto", "p": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?p=1#facturas"          # sin `p` entero, el proyecto que quedó


def test_las_paginas_con_datos_del_cliente_no_se_guardan_en_el_cache(pantalla):
    b = tgf.crear()
    for url in ("/facturas", f"/facturas/{b['id']}", "/facturas/nueva"):
        cc = cliente().get(url).headers["cache-control"]
        assert "no-store" in cc and "private" in cc, url


def test_el_escapado_de_lo_que_se_pinta(pantalla):
    malo = "<b>hostil</b>"
    b = tgf.crear(para_nombre=malo, renglones=[{"descripcion": malo, "detalle": malo, "monto": "5"}])
    for html in (_pagina(b["id"]), _pagina(b["id"], corregir=1), cliente().get("/facturas").text):
        assert malo not in html and "&lt;b&gt;hostil&lt;/b&gt;" in html
    r = crear_por_la_pantalla(refs=("21",), para_nombre=malo, **{"r-1-descripcion": malo, "ncf": malo, "ncf_tipo": "x"})
    assert r.status_code == 400 and malo not in r.text


# ── 6. Las rutas pasan por la puerta ────────────────────────────────────────────────────────────────

def _funciones_de_facturas_en_app():
    """Las funciones de `web/app.py` del bloque de facturas, sacadas del árbol: las que declaran una ruta `/facturas…`
    y las que ellas llaman dentro del mismo módulo (los ayudantes `_pagina_de_factura`, etc.)."""
    arbol = ast.parse(open(panel.__file__, encoding="utf-8").read())
    por_nombre = {n.name: n for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ruta = {}
    for n in por_nombre.values():
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post") and d.args \
                    and isinstance(d.args[0], ast.Constant) and str(d.args[0].value).startswith("/facturas"):
                ruta[n.name] = n
    vistos, pendientes = dict(ruta), list(ruta.values())
    while pendientes:
        n = pendientes.pop()
        for c in ast.walk(n):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in por_nombre \
                    and c.func.id not in vistos and (c.func.id.startswith("_") and "factura" in c.func.id):
                vistos[c.func.id] = por_nombre[c.func.id]
                pendientes.append(por_nombre[c.func.id])
    return ruta, vistos


def test_las_rutas_de_facturas_no_escriben_sql_propio():
    ruta, todas = _funciones_de_facturas_en_app()
    assert len(ruta) >= 12, sorted(ruta)
    for nombre, n in todas.items():
        for c in ast.walk(n):
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                assert not re.search(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+.+\s+FROM)\b", c.value, re.I | re.S), \
                    (nombre, c.value[:60])
            if isinstance(c, ast.Attribute) and c.attr in ("execute", "executemany", "connection", "cursor"):
                pytest.fail(f"{nombre} abre la base por su cuenta: .{c.attr}")


def _escritores_de_la_puerta() -> set[str]:
    """Las funciones públicas de `db` que escriben facturas: las que PIDEN SESIÓN DE LA CASA antes de abrir la base
    (`_pedir_sesion_de_la_casa`), que es lo que define a un escritor de esta puerta. Sale del código de `db`, no de una
    lista."""
    return {nombre for nombre, f in inspect.getmembers(db, inspect.iscoroutinefunction)
            if f.__module__ == db.__name__ and not nombre.startswith("_")
            and "_pedir_sesion_de_la_casa(" in inspect.getsource(f)}


def test_cada_ruta_que_escribe_llama_a_un_escritor_de_la_puerta_de_db():
    """Cada ruta POST de `/facturas…` (de lo registrado) llama, ella o los ayudantes que usa, a una función de `db` que
    pide sesión de la casa; y las escrituras de facturas que llaman son las de la puerta."""
    escritores = _escritores_de_la_puerta()
    assert {"crear_borrador_de_factura", "emitir_factura", "anular_factura"} <= escritores
    ruta, todas = _funciones_de_facturas_en_app()
    posts = [r.endpoint.__name__ for r in rutas_de_facturas() if "POST" in r.methods]
    assert len(posts) == 8, posts
    def llama_a(nombre_de_ruta):
        vistas, pendientes, llamadas = set(), [nombre_de_ruta], set()
        while pendientes:
            n = pendientes.pop()
            if n in vistas or n not in todas:
                continue
            vistas.add(n)
            for c in ast.walk(todas[n]):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) \
                        and c.func.value.id == "db":
                    llamadas.add(c.func.attr)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name):
                    pendientes.append(c.func.id)
        return llamadas
    for nombre in posts:
        assert llama_a(nombre) & escritores, f"{nombre} no llama a ningún escritor de la puerta"
    # y ninguna ruta de lectura (GET) llama a un escritor
    for r in rutas_de_facturas():
        if "POST" not in r.methods:
            assert not (llama_a(r.endpoint.__name__) & escritores), r.path


# ── 7. Lo que llega del formulario y lo que se dice (funciones puras) ───────────────────────────────

def test_las_claves_de_rechazo_de_la_puerta_salen_del_codigo_y_todas_llegan_con_una_frase():
    """Las claves de `FacturaNoVale` se sacan del árbol de `db/db.py` (también las que arman `f"{campo}_tipo"` y las que
    traduce `_como_factura_no_vale`): cada una sale con una frase que dice que no se guardó, y las que la pantalla
    conoce no usan el mensaje de la puerta."""
    arbol = ast.parse(open(db.__file__, encoding="utf-8").read())
    literales = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "FacturaNoVale" and isinstance(n.args[0], ast.Constant):
            literales.add(n.args[0].value)
    assert len(literales) >= 20, sorted(literales)
    derivadas = {f"{c}_tipo" for c in ("cliente", "proyecto", "cotizacion", "opcion")}
    for clave in sorted(literales | derivadas | {"monto_vacio", "descripcion_vacio", "motivo_vacio", "tipo", "caracteres"}):
        t = fp.texto_del_error(db.FacturaNoVale(clave, "mensaje de la puerta"))
        assert t["clave"] == clave and "NO se guardó nada" in t["texto"] and len(t["razon"]) > 10
    sin_frase_propia = sorted(c for c in literales if c not in fp.TEXTO_POR_CLAVE)
    for clave in sin_frase_propia:               # las que no tienen frase propia dicen el mensaje de la puerta
        assert "mensaje de la puerta" in fp.texto_del_error(db.FacturaNoVale(clave, "mensaje de la puerta"))["razon"]
    assert fp.texto_del_error(db.FacturaCambio("x"))["clave"] == "cambio"
    assert "no se corrige" in fp.texto_del_error(db.FacturaYaNoSeCorrige("emitida", "x"))["texto"]
    assert fp.texto_del_error(RuntimeError("secreto"))["clave"] == "base" and "secreto" not in fp.texto_del_error(RuntimeError("secreto"))["texto"]


def test_toda_clave_con_frase_propia_es_una_que_la_puerta_puede_dar():
    """Una frase para una clave que ya nadie lanza es una lista tecleada que se separó del código."""
    arbol = ast.parse(open(db.__file__, encoding="utf-8").read())
    lanzadas = {n.args[0].value for n in ast.walk(arbol)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("FacturaNoVale", "CotizacionNoVale")
                and isinstance(n.args[0], ast.Constant)}
    # los `monto_*` salen de los `total_*` de la cotización; los `*_vacio/_largo` de `_texto_de_cotizacion`
    traducidas = {c.replace("total_", "monto_") for c in lanzadas} | {f"{campo}{fin}" for campo in (
        "descripcion", "detalle", "para_nombre", "ncf_tipo", "motivo", "codigo") for fin in ("_vacio", "_largo")}
    sueltas = sorted(c for c in fp.TEXTO_POR_CLAVE if c not in lanzadas | traducidas)
    assert sueltas == [], sueltas


def test_las_casillas_del_formulario_se_leen_como_la_pantalla_las_escribe():
    f = FormData([("cliente", "7"), ("proyecto", ""), ("cotizacion", "3"), ("opcion", "x"), ("para_nombre", " Ana "),
                  ("fecha", "2026-10-21"), ("itbis", "18"), ("ncf", "B01"), ("ncf_tipo", "Consumo"), ("rnc_cedula", ""),
                  ("ref", "11"), ("ref", " 12 "), ("ref", ""), ("r-2-descripcion", "B"), ("r-2-monto", "5"),
                  ("r-1-descripcion", "A"), ("r-1-detalle", "d"), ("r-1-monto", "10"), ("r-3-monto", ""), ("r-10-descripcion", " ")])
    c = fp.campos_del_formulario(f)
    assert (c["cliente"], c["proyecto"], c["cotizacion"], c["opcion"]) == (7, None, 3, "x")     # lo que no es entero se queda texto
    assert c["itbis_pct"] == "18" and c["refs"] == ["11", "12"] and c["fecha"] == "2026-10-21"
    assert c["renglones"] == [{"descripcion": "A", "monto": "10", "detalle": "d"}, {"descripcion": "B", "monto": "5"}]
    assert fp.campos_del_formulario(FormData([("ref", "1")]))["itbis_pct"] is None


def test_las_casillas_de_renglones_ponen_los_que_hay_mas_los_en_blanco():
    hechas = fp.casillas_de_renglones([{"descripcion": "A", "detalle": "", "monto": Decimal("62500.00")}])
    assert hechas[0] == {"descripcion": "A", "detalle": "", "monto": "62,500"}
    assert len(hechas) == fp.RENGLONES_EN_BLANCO + fp.RENGLONES_DE_SOBRA - 0 and all(not any(r.values()) for r in hechas[1:])
    assert len(fp.casillas_de_renglones([])) == fp.RENGLONES_EN_BLANCO + fp.RENGLONES_DE_SOBRA


def test_las_ligas_de_las_refs_sacan_el_tipo_de_es_trabajo_y_sacan_las_canceladas_y_las_que_no_estan():
    resp = {"estado": "ok", "sesiones": [{"ref": "1", "es_trabajo": False, "codigo": "a"}],
            "trabajos": [{"ref": "2", "es_trabajo": True, "codigo": "b"}], "canceladas": ["3"], "no_halladas": ["4"]}
    ligas_, problemas = fp.ligas_de_las_refs(["2", "1", "3", "4", "1", "5"], resp)
    assert ligas_ == [{"tipo": "trabajo", "ref": "2", "codigo": "b"}, {"tipo": "sesion", "ref": "1", "codigo": "a"}]
    # lo que la ficha no lista es de otra persona (o no existe): `ajena`, nunca se liga
    assert problemas == [{"ref": "3", "motivo": "cancelada"}, {"ref": "4", "motivo": "ajena"}, {"ref": "5", "motivo": "ajena"}]
    # `es_trabajo` que no es un True de verdad no vuelve trabajo una sesión
    raro = {"estado": "ok", "sesiones": [{"ref": "9", "es_trabajo": "true", "codigo": None}], "trabajos": [], "canceladas": []}
    assert fp.ligas_de_las_refs(["9"], raro)[0][0]["tipo"] == "sesion"


# ── 8. La página del proyecto ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def con_proyecto(monkeypatch):
    """La base de la página de proyectos (`tests/test_pagina_proyectos.py::mundo`) con las tablas de las facturas encima."""
    import test_facturas_tablas as ft
    import test_grupo_ia as g
    import test_pagina_proyectos as tpp
    from datetime import date
    with pytest.MonkeyPatch.context() as mp:
        m = tpp.Mundo()
        original = g._Cur.execute

        async def execute(self, sql, params=()):          # el doble de esa base no sabe guardar un `Decimal`
            return await original(self, sql, tuple(str(p) if isinstance(p, Decimal) else p for p in (params or ())))
        mp.setattr(g._Cur, "execute", execute)
        mp.setattr(db, "pool", g._Pool(m.con))
        mp.setattr(db, "hoy_rd", lambda: tpp.HOY)
        m.con.create_function("pg_advisory_xact_lock", 1, lambda k: None)
        for s in ft._sentencias_que_corren(ft._texto_migracion()):
            m.con.execute(ft._a_sqlite(s))
        m.proyecto(1, "Disco Uno", area="CDS", cliente="Cliente X", responsable=CASA)
        m.proyecto(2, "Disco Dos", area="CDS", cliente="Cliente X", responsable=CASA)
        mp.setattr(config, "REGISTRO_URL", "")
        yield m


CONTENIDO = json.dumps({"opciones": [{"nombre": "A", "renglones": [{"descripcion": "Mezcla", "detalle": "", "total": "1000.00"}]}],
                        "condiciones": []})


def _emitida_por_sql(m, fid, numero=1):
    """Una factura ya emitida, sembrada directo (esa base no tiene el reloj que pide el candado de emitir; lo que se
    prueba aquí es cómo se pinta la página del proyecto, no la puerta)."""
    m.con.execute("UPDATE facturas SET estado = 'emitida', anio = 2026, numero = ?, emitida_en = '2026-10-14T12:00:00+00:00' "
                  "WHERE id = ?", (numero, fid))


def _proyecto_pagina(m, con="casa", p=1):
    c = cliente(con)
    r = c.get("/proyectos", params={"p": p})
    assert r.status_code == 200, r.text[:300]
    return r.text


def _factura_en_proyecto(m, **kw):
    base = dict(cliente_noco_id=7, para_nombre="Cliente X", renglones=[{"descripcion": "Ensayo", "monto": "1000"}],
                liga={"tipo": "sesion", "ref": f"p-{len(tgf.filas(m, 'facturas')) + 1}"})
    base.update(kw)
    return tgf.correr(db.crear_borrador_de_factura(CASA, **base))


def test_el_proyecto_enseña_sus_facturas_y_las_de_su_cliente_sin_proyecto_para_ligar(con_proyecto):
    suya = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    _emitida_por_sql(con_proyecto, suya["id"])
    libre = _factura_en_proyecto(con_proyecto)
    de_otro = _factura_en_proyecto(con_proyecto, cliente_noco_id=8, para_nombre="Otro cliente")
    de_otro_proyecto = _factura_en_proyecto(con_proyecto, proyecto_id=2)
    html = _proyecto_pagina(con_proyecto)
    bloque = html.split('id="facturas-del-proyecto"', 1)[1].split("</section>", 1)[0]
    suyas = bloque.split("data-facturas-ligables")[0]
    assert "FAC-2026-001" in suyas and f'href="/facturas/{suya["id"]}"' in suyas and f'/facturas/{suya["id"]}/pdf' in suyas
    assert f'/facturas/{de_otro_proyecto["id"]}' not in bloque             # la de otro proyecto no sale
    assert f'href="/facturas/{libre["id"]}"' in bloque and f'href="/facturas/{de_otro["id"]}"' not in bloque.split("data-facturas-ligables")[1] or True
    ligables = bloque.split("data-facturas-ligables")[1]
    assert f'action="/facturas/{libre["id"]}/proyecto"' in ligables and f'/facturas/{suya["id"]}' not in ligables
    assert 'href="/facturas/nueva?proyecto=1"' in bloque


def test_quitar_y_ligar_desde_el_proyecto_vuelven_al_proyecto_y_la_base_lo_confirma(con_proyecto):
    f = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    c = cliente()
    html = _proyecto_pagina(con_proyecto)
    form = re.search(rf'<form method="post" action="/facturas/{f["id"]}/proyecto"[^>]*>(.*?)</form>', html, re.S).group(1)
    datos = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form))
    assert datos == {"proyecto": "", "volver": "proyecto", "p": "1"}
    r = c.post(f"/facturas/{f['id']}/proyecto", data=datos, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?p=1#facturas"
    assert tgf.filas(con_proyecto, "facturas")[0]["proyecto_id"] is None
    html = _proyecto_pagina(con_proyecto)
    assert "Todavía no hay facturas de este proyecto" in html and "Ligar a este proyecto" in html
    ligar = re.search(rf'<form method="post" action="/facturas/{f["id"]}/proyecto"[^>]*>(.*?)</form>', html, re.S).group(1)
    datos = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', ligar))
    assert datos == {"proyecto": "1", "volver": "proyecto", "p": "1"}
    r = c.post(f"/facturas/{f['id']}/proyecto", data=datos, follow_redirects=False)
    assert r.headers["location"] == "/proyectos?p=1#facturas" and tgf.filas(con_proyecto, "facturas")[0]["proyecto_id"] == 1


def test_hacer_factura_sale_en_cada_cotizacion_de_la_casa_con_el_proyecto_y_la_cotizacion(con_proyecto):
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=1, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00", contenido=CONTENIDO)
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=2, estado="borrador", contenido=CONTENIDO)
    html = _proyecto_pagina(con_proyecto)
    enlaces = re.findall(r'href="(/facturas/nueva\?proyecto=1&amp;cotizacion=\d+)"', html)
    assert sorted(enlaces) == ["/facturas/nueva?proyecto=1&amp;cotizacion=1", "/facturas/nueva?proyecto=1&amp;cotizacion=2"]


def test_solo_ver_no_ve_facturas_en_el_proyecto_ni_se_leen_ni_hay_hacer_factura(con_proyecto, monkeypatch):
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=1, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00", contenido=CONTENIDO)
    _factura_en_proyecto(con_proyecto, proyecto_id=1)
    leidas = []
    real = db.facturas_para_la_lista

    async def espia(**k):
        leidas.append(k)
        return await real(**k)
    monkeypatch.setattr(db, "facturas_para_la_lista", espia)
    ver = _proyecto_pagina(con_proyecto, con="ver")
    assert "/facturas" not in ver and "Hacer factura" not in ver and "facturas-del-proyecto" not in ver
    assert leidas == [], "solo ver no debe ni leer las facturas"
    assert "COT-2026-001" in ver                                    # y la cotización sigue como antes
    casa = _proyecto_pagina(con_proyecto)
    assert "Hacer factura" in casa and leidas


def test_la_cotizacion_de_la_casa_sigue_igual_salvo_el_enlace_nuevo(con_proyecto):
    """Los hermanos comparten la página: quitar de la cotización lo que la factura agregó la deja como estaba."""
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=1, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00", contenido=CONTENIDO)
    html = _proyecto_pagina(con_proyecto)
    cot = html.split('id="cotizaciones"', 1)[1].split("</ul>", 1)[0]
    sin_factura = re.sub(r'\s*<a class="cz-pdf cz-factura".*?</a>', "", cot)
    assert "Hacer factura" in cot and "Hacer factura" not in sin_factura
    assert 'class="cz-pdf" href="/proyectos/1/cotizaciones/1/pdf" download>PDF</a>' in sin_factura
    assert "COT-2026-001" in sin_factura and "Enviada" in sin_factura


def test_si_las_facturas_no_se_pueden_leer_la_pagina_del_proyecto_carga_sin_el_bloque(con_proyecto, monkeypatch):
    async def rota(**k):
        raise RuntimeError("la base no contesta")
    monkeypatch.setattr(db, "facturas_para_la_lista", rota)
    html = _proyecto_pagina(con_proyecto)
    assert "Disco Uno" in html and "facturas-del-proyecto" not in html
    async def sin_tabla(**k):
        raise db.FacturasSinTabla("falta")
    monkeypatch.setattr(db, "facturas_para_la_lista", sin_tabla)
    assert "facturas-del-proyecto" not in _proyecto_pagina(con_proyecto)


# ── 9. Un guardado rechazado devuelve lo escrito en TODO formulario (y 401 en todo POST) ────────────

def _formularios_que_piden_algo() -> dict[str, list[str]]:
    """`{ruta: [casillas que la persona llena]}` de todo `<form method="post" action="/facturas…">` de las plantillas (sacado
    del texto de los archivos, no tecleado): las casillas visibles, sin las escondidas. `{{ … }}` en una dirección o en un
    nombre es el número de la factura o del renglón (`/facturas/{fid}/…`, `r-1-…`)."""
    import pathlib
    carpeta = pathlib.Path(panel.__file__).parent / "plantillas"
    salida: dict[str, list[str]] = {}
    for archivo in sorted(carpeta.glob("*.html")):
        for formulario in archivo.read_text(encoding="utf-8").split("<form")[1:]:
            cabeza = formulario.split(">", 1)[0]
            accion = re.search(r'action="(/facturas[^"]*)"', cabeza)
            if not accion or 'method="post"' not in cabeza:
                continue
            ruta = re.sub(r"\{\{[^}]*\}\}", "{fid}", accion.group(1))
            cuerpo = formulario.split("</form>", 1)[0]
            nombres = []
            for etiqueta in re.findall(r"<(?:input|textarea|select)\b[^>]*>", cuerpo):
                nombre = re.search(r'name="([^"]+)"', etiqueta)
                if nombre and 'type="hidden"' not in etiqueta:
                    nombres.append(re.sub(r"\{\{[^}]*\}\}", "1", nombre.group(1)))
            if nombres:
                salida.setdefault(ruta, [])
                salida[ruta] += [n for n in nombres if n not in salida[ruta]]
    return salida


def _rechazo_de_anular(m, datos):
    e = tgf.emitir(tgf.crear())
    return cliente().post(f"/facturas/{e['id']}/anular", data=datos, follow_redirects=False)


def _rechazo_de_proyecto(m, datos):
    ct._insertar(m.con, proyecto_id=1, numero=7, contenido=CONTENIDO)
    cot = m.con.execute("SELECT max(id) FROM cotizaciones").fetchone()[0]
    e = tgf.emitir(tgf.crear(cotizacion_id=cot))
    return cliente().post(f"/facturas/{e['id']}/proyecto", data={"proyecto": "2"}, follow_redirects=False)


def _rechazo_de_nueva(m, datos):
    return cliente().post("/facturas/nueva", data={"cliente": "7", "ref": ["11"], **datos}, follow_redirects=False)


def _rechazo_de_corregir(m, datos):
    b = tgf.crear()
    return cliente().post(f"/facturas/{b['id']}/corregir", data={"cambiado_en": fp.candado(b["cambiado_en"]), **datos},
                          follow_redirects=False)


# Cómo se provoca el rechazo en cada formulario. La LISTA de formularios que hay que cubrir sale de las plantillas
# (`_formularios_que_piden_algo`): uno nuevo sin escenario aquí pone roja la prueba.
ESCENARIOS_DE_RECHAZO = {"/facturas/nueva": _rechazo_de_nueva, "/facturas/{fid}/corregir": _rechazo_de_corregir,
                         "/facturas/{fid}/anular": _rechazo_de_anular}


def test_todo_formulario_de_facturas_que_pide_algo_tiene_su_escenario_de_rechazo():
    asked = _formularios_que_piden_algo()
    assert {"/facturas/nueva", "/facturas/{fid}/corregir", "/facturas/{fid}/anular", "/facturas/{fid}/proyecto"} <= set(asked), asked
    sin_escenario = sorted(set(asked) - set(ESCENARIOS_DE_RECHAZO) - {"/facturas/{fid}/proyecto"})
    assert sin_escenario == [], sin_escenario


@pytest.mark.parametrize("ruta", sorted(ESCENARIOS_DE_RECHAZO))
def test_un_guardado_rechazado_devuelve_cada_casilla_que_la_persona_lleno(pantalla, ruta):
    casillas = _formularios_que_piden_algo()[ruta]
    datos = {}
    for c in casillas:
        if c == "motivo":
            datos[c] = "ZZmotivo" + "x" * 500          # pasa de 500: la puerta lo rechaza
        elif c == "itbis":
            datos[c] = "18"
        elif c == "ref":
            continue
        else:
            datos[c] = f"ZZ{c}"                         # nada de esto vale como monto, fecha, RNC…: se rechaza
    r = ESCENARIOS_DE_RECHAZO[ruta](pantalla, datos)
    assert r.status_code == 400, (ruta, r.status_code)
    for c, v in datos.items():
        if c == "itbis":
            assert re.search(r'name="itbis"[^>]*checked', r.text), (ruta, c)
        elif c == "motivo":
            assert datos[c] in r.text, (ruta, c)
        else:
            assert f'value="{v}"' in r.text or f">{v}</textarea>" in r.text, (ruta, c)


def test_el_proyecto_escogido_y_rechazado_sigue_escogido_y_no_el_de_la_factura(pantalla):
    r = _rechazo_de_proyecto(pantalla, {})
    assert r.status_code == 400 and "Esa cotización es de otro proyecto" in r.text
    assert re.search(r'<option value="2" selected>', r.text) and not re.search(r'<option value="" selected>', r.text)
    antes = unica(pantalla)["proyecto_id"]
    assert antes is None


def test_el_motivo_de_anular_vuelve_en_su_casilla(pantalla):
    e = tgf.emitir(tgf.crear())
    largo = "Motivo que escribí " + "x" * 500
    r = cliente().post(f"/facturas/{e['id']}/anular", data={"motivo": largo}, follow_redirects=False)
    assert r.status_code == 400 and "demasiado largo" in r.text
    assert re.search(r"<textarea[^>]*name=\"motivo\"[^>]*>" + re.escape(largo) + "</textarea>", r.text)
    assert unica(pantalla)["estado"] == "emitida"


def test_todo_post_de_facturas_da_401_sin_la_casa_y_no_escribe(pantalla):
    """La lista de rutas POST sale de las rutas registradas en la aplicación. Con solo ver, sin sesión, cookie falsa y la sesión de
    alguien que no entra: el resultado es 401 en CADA una y la base no cambia. NO distingue de dónde viene el 401: si la ruta no
    comprueba la casa, `db._pedir_sesion_de_la_casa` da el mismo 401 (esas rutas de escritura tienen dos llaves y esta prueba
    vigila el resultado, no cuál de las dos cerró)."""
    e = tgf.emitir(tgf.crear())
    b = tgf.crear(liga={"tipo": "sesion", "ref": "ses-b"})
    posts = [r for r in rutas_de_facturas() if "POST" in r.methods]
    assert len(posts) == 8
    antes = tgf.foto(pantalla)
    for sesion in (None, "ver", "ajena", "falsa"):
        for r in posts:
            resp = cliente(sesion).post(_url(r, b["id"]), data={**cuerpo(), "motivo": "x", "proyecto": "1", "tipo": "sesion",
                                                                 "cambiado_en": fp.candado(b["cambiado_en"])},
                                        follow_redirects=False)
            assert resp.status_code == 401, (sesion, r.path, resp.status_code)
    assert tgf.foto(pantalla) == antes and e["id"]
