"""La carpeta de Drive en la página del proyecto: su estado y el botón «Crear la carpeta en Drive» (pieza 2 del diseño
«Carpetas del proyecto», 18-oct-2026; garantías A7 y A8, la parte de la página).

CÓMO. El mismo camino de producción de la pieza 1 (`tests/test_carpeta_nace_con_el_proyecto.py`): base SQLite con las
sentencias REALES de `db`/`crud`, Drive de mentira con memoria (`Nube`) detrás del cliente de verdad `drive.py`, y la
página y las rutas REALES (`GET /proyectos`, `POST /proyectos/{pid}/drive/crear`). Ni `preparar_la_carpeta` ni
`estado_de_la_carpeta_de_drive` se reemplazan (salvo la prueba que dice que hace reventar a la primera).

QUÉ SE VIGILA
  A7  el botón solo lo usa la casa: sin sesión, solo ver y una sesión válida de un chat fuera de `CHAT_IDS_PERMITIDOS` dan
      401 y a Drive no le llega NADA ni cambia una fila; la página no dibuja el botón ni dice que falta en solo ver; y si la
      lectura de «¿hay fila?» falla (la tabla no está, la base revienta) no se afirma «sin carpeta» ni sale botón. Los
      hermanos (toda ruta POST que pide la carpeta) piden sesión, sacados del árbol de `web/app.py`;
  A8  lo que la página dice después de pulsar el botón sale de `frase_de_la_carpeta` y coincide con lo que de verdad pasó,
      en cada resultado posible (los de `preparar_la_carpeta`, sacados de su árbol), y ninguna frase de esas se escribe a
      mano en otro sitio (la guarda de la pieza 1, ahora también con las palabras de la página y del botón).

FRONTERA, dicha una vez: SQLite, no Postgres; el Drive de mentira afirma lo que afirma `tests/test_drive.py`; los dos
clics seguidos corren en un solo hilo; «Crear la carpeta en Drive» no se probó en un navegador (es un `<form>` de un botón).

Correr con la suite entera:  python3 -m pytest -q -p no:cacheprovider -n 6
"""
from __future__ import annotations

import ast
import html as _html
import re

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (primero: otros módulos de prueba cambian `httpx` al importarse)

from test_carpeta_nace_con_el_proyecto import (  # noqa: F401  (fixtures y ayudas de la pieza 1)
    FALLAS_DE_DRIVE, _carpetas, _filas, _frases_a_mano, _fragmentos_de_las_frases, _funciones, _modulos, _nombre_llamado,
    _preparar, _proyecto, _resultados_que_preparar_puede_dar, espia, gente, google, m, mundo, nube)
from test_cotizaciones_drive import MARCA_DE_GOOGLE
from test_cotizaciones_en_la_pagina import _casa, _pagina, _ver
from test_cotizaciones_ia_rutas import _ajeno, _sin_sesion
from test_grupo_ia import _ROOT

import cotizaciones_drive as cd
import db.db as db

UN_PROYECTO_SIN_CARPETA = 2        # el mundo `m`: el 1 trae una carpeta escrita a mano, el 2 y el 3 no
UN_PROYECTO_CON_CARPETA = 1
RUTA = "/proyectos/{}/drive/crear"


def _html_de(r) -> str:
    return _html.unescape(r.text)


def _formularios_de_drive(pagina: str) -> list[str]:
    return re.findall(r'<form class="carpeta-drive"[^>]*action="([^"]*)"', pagina)


def _pulsar(c, pid: int):
    """Lo que hace el navegador: abre la página del proyecto, toma el `action` del formulario que ella misma dibuja, lo
    envía vacío (el botón no lleva campos) y sigue la redirección. `(respuesta del POST, página que sigue)`."""
    acciones = _formularios_de_drive(c.get(f"/proyectos?p={pid}").text)
    assert acciones == [RUTA.format(pid)], acciones
    r = c.post(acciones[0], follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r, _html_de(c.get(r.headers["location"].split("#")[0]))


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# La página: título, línea «Documentos (Drive)», frase y botón
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_el_bloque_se_llama_carpetas_del_proyecto_y_tiene_la_linea_de_documentos_para_la_casa_y_para_solo_ver(m, nube):
    for cliente in (_casa(), _ver()):
        pagina = _html_de(cliente.get(f"/proyectos?p={UN_PROYECTO_CON_CARPETA}"))
        assert "<h2>Carpetas del proyecto</h2>" in pagina and "Documentos (Drive)" in pagina
        assert "Carpeta del proyecto</h2>" not in pagina


def test_sin_fila_la_casa_ve_la_frase_y_el_boton_y_con_fila_no(m, nube):
    esperado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    for pid in (UN_PROYECTO_SIN_CARPETA, UN_PROYECTO_CON_CARPETA):
        pagina = _html_de(_casa().get(f"/proyectos?p={pid}"))
        assert esperado["frase"] in pagina and f">{esperado['boton']}</button>" in pagina
        assert _formularios_de_drive(pagina) == [RUTA.format(pid)]
    assert _preparar(UN_PROYECTO_SIN_CARPETA)["resultado"] == "lista"
    pagina = _html_de(_casa().get(f"/proyectos?p={UN_PROYECTO_SIN_CARPETA}"))
    assert esperado["frase"] not in pagina and esperado["boton"] not in pagina and "drive/crear" not in pagina
    assert "Abrir la carpeta" in pagina                       # y ya se ve el enlace que Lucy puso


def test_en_solo_ver_ni_el_boton_ni_la_frase_de_que_falta_con_fila_o_sin_ella(m, nube):
    esperado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    for con_fila in (False, True):
        if con_fila:
            assert _preparar(UN_PROYECTO_CON_CARPETA)["resultado"] == "lista"
        for pid in (UN_PROYECTO_SIN_CARPETA, UN_PROYECTO_CON_CARPETA):
            pagina = _html_de(_ver().get(f"/proyectos?p={pid}"))
            assert "drive/crear" not in pagina and esperado["frase"] not in pagina and esperado["boton"] not in pagina
            assert _formularios_de_drive(pagina) == []


def _lectura_que_revienta(mp, m_):
    async def revienta(pid):
        raise RuntimeError(MARCA_DE_GOOGLE)
    mp.setattr(db, "carpetas_de_drive", revienta)


def _tabla_que_falta(mp, m_):
    m_.con.execute("DROP TABLE drive_de_proyectos")
    m_.con.commit()


@pytest.mark.parametrize("rompe", [_lectura_que_revienta, _tabla_que_falta], ids=["la_lectura_revienta", "falta_la_tabla"])
@pytest.mark.parametrize("pid", [UN_PROYECTO_SIN_CARPETA, UN_PROYECTO_CON_CARPETA])
def test_si_no_se_pudo_leer_si_hay_fila_la_pagina_no_afirma_que_falta_ni_ofrece_el_boton(m, nube, monkeypatch, rompe, pid):
    esperado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    rompe(monkeypatch, m)
    r = _casa().get(f"/proyectos?p={pid}")
    pagina = _html_de(r)
    assert r.status_code == 200 and "<h2>Carpetas del proyecto</h2>" in pagina       # la página carga, el bloque sigue
    assert esperado["frase"] not in pagina and esperado["boton"] not in pagina and "drive/crear" not in pagina
    assert MARCA_DE_GOOGLE not in pagina


def test_la_puerta_del_estado_distingue_los_tres_casos_y_solo_cuando_falta_habla(m):
    falta = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    hay = cd.estado_de_la_carpeta_de_drive({"carpeta_proyecto_id": "x"}, se_pudo_leer=True)
    no_se_sabe = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=False)
    assert (falta["estado"], hay["estado"], no_se_sabe["estado"]) == ("falta", "hay", "no_se_sabe")
    assert falta["frase"] and falta["boton"]
    assert hay["frase"] is None and hay["boton"] is None and no_se_sabe["frase"] is None and no_se_sabe["boton"] is None
    # una lectura que no contestó manda sobre lo que "devolvió": nunca se afirma que falta
    assert cd.estado_de_la_carpeta_de_drive({"carpeta_proyecto_id": "x"}, se_pudo_leer=False)["estado"] == "no_se_sabe"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A7. Solo la casa pulsa el botón
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("quien", [_sin_sesion, _ver, _ajeno], ids=["sin_sesion", "solo_ver", "chat_ajeno_con_sesion_valida"])
@pytest.mark.parametrize("pid", [UN_PROYECTO_SIN_CARPETA, 9999])
def test_sin_la_casa_la_ruta_da_401_y_a_drive_no_le_llega_nada_ni_cambia_la_base(m, nube, quien, pid):
    antes = list(m.con.iterdump())
    r = quien().post(RUTA.format(pid), follow_redirects=False)
    assert r.status_code == 401, r.status_code
    assert nube.pasos == [] and nube.google.pedidos == []
    assert list(m.con.iterdump()) == antes and _carpetas(m, UN_PROYECTO_SIN_CARPETA) is None


def test_la_casa_pulsa_el_boton_y_la_carpeta_nace(m, nube):
    r, pagina = _pulsar(_casa(), UN_PROYECTO_SIN_CARPETA)
    assert r.headers["location"] == f"/proyectos?hecho=carpeta_drive&p={UN_PROYECTO_SIN_CARPETA}#carpeta-del-proyecto"
    assert cd.frase_de_la_carpeta({"resultado": "lista", "causa": None}) in pagina
    carpetas = _carpetas(m, UN_PROYECTO_SIN_CARPETA)
    assert carpetas is not None and nube.carpetas_de_proyectos() == [carpetas["carpeta_proyecto_id"]]
    assert _proyecto(m, UN_PROYECTO_SIN_CARPETA)["carpeta"].endswith(carpetas["carpeta_proyecto_id"])
    assert "drive/crear" not in pagina and "Abrir la carpeta" in pagina


def test_pulsarlo_dos_veces_no_crea_otra_y_la_segunda_pagina_dice_que_ya_estaba(m, nube):
    c = _casa()
    _pulsar(c, UN_PROYECTO_SIN_CARPETA)
    pasos = list(nube.pasos)
    r = c.post(RUTA.format(UN_PROYECTO_SIN_CARPETA), follow_redirects=False)         # un segundo clic (otra pestaña, otro dedo)
    assert r.headers["location"] == f"/proyectos?hecho=carpeta_drive_ya&p={UN_PROYECTO_SIN_CARPETA}#carpeta-del-proyecto"
    pagina = _html_de(c.get(r.headers["location"].split("#")[0]))
    assert cd.frase_de_la_carpeta({"resultado": "ya_estaba", "causa": None}) in pagina
    assert nube.pasos == pasos and len(nube.carpetas_de_proyectos()) == 1 and len(_filas(m, "SELECT * FROM drive_de_proyectos")) == 1


def test_con_una_carpeta_escrita_a_mano_la_nueva_no_la_pisa_y_la_pagina_lo_dice(m, nube):
    antes = _proyecto(m, UN_PROYECTO_CON_CARPETA)["carpeta"]
    r, pagina = _pulsar(_casa(), UN_PROYECTO_CON_CARPETA)
    assert r.headers["location"] == f"/proyectos?hecho=carpeta_drive_otra&p={UN_PROYECTO_CON_CARPETA}#carpeta-del-proyecto"
    assert "El proyecto ya tenía otra carpeta escrita y no se cambió." in pagina
    assert _proyecto(m, UN_PROYECTO_CON_CARPETA)["carpeta"] == antes and _carpetas(m, UN_PROYECTO_CON_CARPETA) is not None


def test_un_proyecto_que_no_existe_se_dice_y_no_toca_drive(m, nube):
    r = _casa().post(RUTA.format(9999), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/proyectos?error=carpeta_drive_no_esta&p=9999#carpeta-del-proyecto"
    assert nube.pasos == [] and nube.google.pedidos == []
    c = _casa()
    c.post(RUTA.format(9999))          # (el recibo del aviso es de la sesión que pulsó)
    assert cd.frase_de_la_carpeta({"resultado": "no_esta"}) in _html_de(c.get("/proyectos?error=carpeta_drive_no_esta&p=9999"))


def test_si_preparar_la_carpeta_reventara_la_ruta_contesta_con_el_aviso_de_que_no_se_pudo(m, nube, monkeypatch):
    async def revienta(*a, **k):
        raise RuntimeError(MARCA_DE_GOOGLE)
    monkeypatch.setattr(cd, "preparar_la_carpeta", revienta)
    r = _casa().post(RUTA.format(UN_PROYECTO_SIN_CARPETA), follow_redirects=False)
    assert r.status_code == 303 and "error=carpeta_drive_fallo" in r.headers["location"] and MARCA_DE_GOOGLE not in r.headers["location"]


def _rutas_post_que_piden_la_carpeta(arbol) -> dict:
    """`{nombre de la ruta: ¿pide sesión de la casa (`puede_entrar`)?}` de toda función de `web/app.py` decorada con
    `app.post(...)` que pide la carpeta de Drive: llama a `preparar_la_carpeta` o a un ayudante del módulo que la llama."""
    ayudantes = {f.name for f in _funciones(arbol)
                 if any(isinstance(n, ast.Call) and _nombre_llamado(n) == "preparar_la_carpeta" for n in ast.walk(f))}
    salida = {}
    for f in _funciones(arbol):
        es_post = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
                      for d in f.decorator_list)
        llamadas = [n for n in ast.walk(f) if isinstance(n, ast.Call)]
        pide = any(_nombre_llamado(n) == "preparar_la_carpeta" or _nombre_llamado(n) in ayudantes for n in llamadas)
        if es_post and pide:
            salida[f.name] = any(_nombre_llamado(n) == "puede_entrar" for n in llamadas)
    return salida


def test_toda_ruta_post_que_pide_la_carpeta_pide_sesion_de_la_casa_los_hermanos_sacados_del_arbol():
    arbol = dict(_modulos(_ROOT))["web/app.py"]
    rutas = _rutas_post_que_piden_la_carpeta(arbol)
    assert {"crear_proyecto_nuevo", "convertir_en_proyecto", "crear_carpeta_de_drive_del_proyecto"} <= set(rutas), rutas
    assert [n for n, con_sesion in rutas.items() if not con_sesion] == [], rutas


def test_la_guarda_de_hermanos_ve_una_ruta_inventada_sin_sesion():
    fuente = ("@app.post('/x')\nasync def sin_puerta(request):\n    await _ayuda(1)\n"
              "async def _ayuda(p):\n    await cd.preparar_la_carpeta(p)\n"
              "@app.post('/y')\nasync def con_puerta(request):\n    if not auth.puede_entrar(1):\n        return 1\n    await _ayuda(1)\n")
    assert _rutas_post_que_piden_la_carpeta(ast.parse(fuente)) == {"sin_puerta": False, "con_puerta": True}


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# A8. La página dice lo que pasó, de una sola fuente
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def _sin_causa(r: dict) -> dict:
    return {**r, "causa": None}


@pytest.mark.parametrize("falla", sorted(FALLAS_DE_DRIVE))
def test_con_cada_falla_de_drive_la_pagina_que_sigue_al_boton_dice_lo_que_la_funcion_contesto(m, nube, monkeypatch, espia, falla):
    FALLAS_DE_DRIVE[falla][0](monkeypatch, nube, m)
    c = _casa()
    r = c.post(RUTA.format(UN_PROYECTO_SIN_CARPETA), follow_redirects=False)
    assert espia[-1]["resultado"] == "fallo"
    assert r.headers["location"] == f"/proyectos?error=carpeta_drive_fallo&p={UN_PROYECTO_SIN_CARPETA}#carpeta-del-proyecto"
    pagina = _html_de(c.get(r.headers["location"].split("#")[0]))
    assert cd.frase_de_la_carpeta(_sin_causa(espia[-1])) in pagina and "creada." not in pagina.split("aviso", 1)[-1][:400]
    assert MARCA_DE_GOOGLE not in pagina and MARCA_DE_GOOGLE not in r.headers["location"]
    assert _proyecto(m, UN_PROYECTO_SIN_CARPETA)["carpeta"] is None


def test_cuando_el_enlace_no_se_pudo_poner_la_pagina_lo_dice_y_no_dice_que_quedo(m, nube, monkeypatch, espia):
    async def sin_lectura():
        return None                        # como la columna sin migrar: no se puede comprobar que quedó escrito
    monkeypatch.setattr(db, "carpetas_de_proyectos", sin_lectura)
    c = _casa()
    r = c.post(RUTA.format(UN_PROYECTO_SIN_CARPETA), follow_redirects=False)
    assert espia[-1] == {"resultado": "lista", "causa": None, "carpeta": cd.ENLACE_NO_SE_PUDO}
    assert r.headers["location"] == f"/proyectos?error=carpeta_drive_enlace&p={UN_PROYECTO_SIN_CARPETA}#carpeta-del-proyecto"
    assert cd.frase_de_la_carpeta(espia[-1]) in _html_de(c.get(r.headers["location"].split("#")[0]))


def test_para_cada_resultado_posible_la_clave_lleva_a_una_frase_igual_a_la_real_sin_la_causa():
    resultados = _resultados_que_preparar_puede_dar()
    assert len(resultados) >= 12
    alcanzadas = set()
    for r in resultados:
        tipo, clave = cd.aviso_de_la_carpeta(r)
        tabla = {"hecho": cd.HECHOS_DE_LA_CARPETA, "error": cd.AVISOS_DE_LA_CARPETA}[tipo]
        assert clave in tabla, (r, tipo, clave)
        assert cd.frase_de_la_carpeta(tabla[clave]) == cd.frase_de_la_carpeta(_sin_causa(r)), (r, clave)
        alcanzadas.add((tipo, clave))
    # y el reparto es limpio: cada clave de las dos tablas la alcanza algún resultado, y ninguna está en las dos
    assert alcanzadas == {("hecho", k) for k in cd.HECHOS_DE_LA_CARPETA} | {("error", k) for k in cd.AVISOS_DE_LA_CARPETA}
    assert not set(cd.HECHOS_DE_LA_CARPETA) & set(cd.AVISOS_DE_LA_CARPETA)
    # lo que la función no entiende cae del lado de «no se pudo»
    for raro in ({}, {"resultado": "otra"}, {"resultado": None}, None, "x"):
        assert cd.aviso_de_la_carpeta(raro) == ("error", "carpeta_drive_fallo"), raro


@pytest.mark.parametrize("clave", sorted(cd.HECHOS_DE_LA_CARPETA))
def test_la_pagina_pinta_la_frase_de_cada_clave_de_hecho_solo_con_recibo(m, nube, clave):
    frase = cd.frase_de_la_carpeta(cd.HECHOS_DE_LA_CARPETA[clave])
    consulta = {"hecho": clave, "p": UN_PROYECTO_CON_CARPETA}
    assert frase in _html.unescape(_pagina(_casa(), con_recibo=True, **consulta))
    assert frase not in _html.unescape(_pagina(_casa(), **consulta))          # una dirección escrita a mano no cuenta nada
    assert frase not in _html.unescape(_pagina(_ver(), **consulta))


def test_ninguna_pieza_escribe_a_mano_las_palabras_de_la_pagina_ni_las_del_boton():
    """Las frases de la pieza 1 más las de esta (`FRASE_SIN_CARPETA_EN_DRIVE`, la etiqueta del botón): se sacan de la
    función que las dice, no se teclean aquí. El guardián es el mismo de la pieza 1 (`_frases_a_mano`)."""
    estado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    palabras = {" ".join(estado[k].split()[:7]) for k in ("frase", "boton")}
    fragmentos = _fragmentos_de_las_frases()
    assert palabras <= fragmentos, (palabras, fragmentos)           # la guarda de la pieza 1 ya las trae
    assert _frases_a_mano(_ROOT, palabras) == []


def test_la_guarda_ve_el_boton_y_la_frase_escritos_a_mano_en_un_archivo_inventado(tmp_path):
    estado = cd.estado_de_la_carpeta_de_drive(None, se_pudo_leer=True)
    palabras = {" ".join(estado[k].split()[:7]) for k in ("frase", "boton")}
    (tmp_path / "web" / "plantillas").mkdir(parents=True)
    (tmp_path / "web" / "plantillas" / "a.html").write_text(f"<button>{estado['boton']}</button>", encoding="utf-8")
    (tmp_path / "web" / "plantillas" / "b.html").write_text(f"<p>{estado['frase']}</p>", encoding="utf-8")
    (tmp_path / "web" / "plantillas" / "limpia.html").write_text("<p>{{ carpeta_drive.frase }}</p>", encoding="utf-8")
    hallados = {r for r, _ in _frases_a_mano(tmp_path, palabras)}
    assert hallados == {"web/plantillas/a.html", "web/plantillas/b.html"}, hallados
