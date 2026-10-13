"""`db.situacion_de_proyecto`: la puerta única del estado de un proyecto y de su próxima tarea.

Qué vigila (cada punto tiene al menos una mutación del constructor, ver el reporte del trabajo):

  G1  Las palabras del estado son las cuatro de siempre, con sus defectos conocidos (decisión de
      Tiziano, 13-oct-2026: se quedan).
  G2  «La próxima» es la primera pendiente del orden de siempre (graves, vencidas, fecha más
      cercana, sin fecha al final, empate por id).
  G3  Un proyecto cerrado no tiene próxima; uno en pausa sí.
  G4  La página (`GET /proyectos`, ruta y plantilla REALES, SQL REAL sobre SQLite) dice lo que dice
      la función: el estado, el orden y la próxima salen de ella y de ningún otro sitio.
  G5  Los hermanos: dentro del código de producción nadie más decide el estado con las palabras ni
      el orden de las pendientes de un proyecto (la lista sale de recorrer los `.py` y las plantillas
      de verdad).
  G6  Lo que se ve no cambió: contra una copia de la lógica de ANTES (7f962ce) sobre todas las
      combinaciones que deciden (estado guardado × estado de la tarea × grave × vencimiento).

Frontera: es Python puro más la ruta real con SQLite; no se mide un navegador ni Postgres. La batería
de «antes contra ahora» sobre el código real de 7f962ce (con `git archive`) se corrió fuera del
repositorio; aquí queda el oráculo escrito a mano de esa lógica (G6).

Correr:  python3 -m pytest tests/test_situacion_de_proyecto.py -q
"""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import ast
import itertools
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import db.db as db
from test_buzon_que_no_se_ve import _py_en_disco
from test_pagina_proyectos import _dia, gente, mundo, ver  # noqa: F401  (fixtures y ayudas de la página)

HOY = date(2026, 10, 10)
RAIZ = Path(__file__).resolve().parent.parent
PLANTILLAS = RAIZ / "web" / "plantillas"


def _dia_rd(n: int, hora: int = 12) -> datetime:
    """Un instante en Santo Domingo, `n` días desde HOY."""
    return datetime.combine(HOY + timedelta(days=n), datetime.min.time(),
                            tzinfo=config.TZ).replace(hour=hora)


def tarea(id, estado="pendiente", vence=None, grave=False, completado=None, titulo=None):
    """Una fila de tarea como la deja `_fila_de_tarea` (la REAL, la misma de la página)."""
    t = {"id": id, "titulo": titulo or f"tarea {id}", "estado": estado, "vence_en": vence,
         "completado_en": completado, "responsable_chat_id": None, "proyecto_id": 1,
         "area": None, "grave": grave}
    return db._fila_de_tarea(t, HOY, {}, {})


def situacion(estado, *filas):
    return db.situacion_de_proyecto(estado, list(filas))


# ═══════════════════════════════════════════════════════════════════════
# G1: las palabras (y sus defectos, dichos)
# ═══════════════════════════════════════════════════════════════════════

CASOS_DE_ESTADO = [
    # (nombre del caso, estado guardado, tareas, palabra esperada)
    ("sin tareas", "activo", [], "Sin empezar"),
    ("una hecha", "activo", [tarea(1, "hecha", completado=_dia_rd(-1))], "En curso"),
    # DEFECTO CONOCIDO 1 (Tiziano lo dejó así): todo hecho y sin cerrar dice «En curso».
    ("todo hecho y sin cerrar", "activo",
     [tarea(1, "hecha", completado=_dia_rd(-1)), tarea(2, "hecha", completado=_dia_rd(-2))], "En curso"),
    # DEFECTO CONOCIDO 2 (idem): pendientes vencidas y nada hecho dice «Sin empezar».
    ("pendientes vencidas y nada hecho", "activo",
     [tarea(1, vence=_dia_rd(-3)), tarea(2, vence=_dia_rd(-1))], "Sin empezar"),
    ("pendiente y una hecha", "activo", [tarea(1), tarea(2, "hecha", completado=_dia_rd(-1))], "En curso"),
    ("solo descartadas", "activo", [tarea(1, "descartado")], "Sin empezar"),
    ("pausado, aunque haya hechas", "pausado", [tarea(1, "hecha", completado=_dia_rd(-1))], "Pausado"),
    ("pausado sin tareas", "pausado", [], "Pausado"),
    ("cerrado con pendientes", "cerrado", [tarea(1, vence=_dia_rd(-1))], "Cerrado"),
    ("cerrado sin tareas", "cerrado", [], "Cerrado"),
    # Un estado guardado fuera del vocabulario se trata como abierto (`estado_conocido` lo avisa aparte).
    ("estado libre sin hechas", "en revision", [tarea(1)], "Sin empezar"),
    ("estado libre con una hecha", "en revision", [tarea(1, "hecha", completado=_dia_rd(-1))], "En curso"),
]


@pytest.mark.parametrize("nombre,estado,filas,palabra", CASOS_DE_ESTADO, ids=[c[0] for c in CASOS_DE_ESTADO])
def test_G1_las_palabras_del_estado(nombre, estado, filas, palabra):
    assert situacion(estado, *filas)["estado"] == palabra


def test_G1_el_vocabulario_son_las_cuatro_palabras_de_siempre():
    """Ninguna palabra nueva («Por cerrar», una frase): lo contestó Tiziano el 13-oct-2026."""
    vistas = {situacion(e, *f)["estado"] for _, e, f, _ in CASOS_DE_ESTADO}
    assert vistas == {"Sin empezar", "En curso", "Pausado", "Cerrado"}


def test_G1_el_docstring_dice_los_dos_defectos_tal_cual():
    """La frase promete lo que el código hace: si algún día cambian las palabras, esto avisa."""
    d = " ".join(db.situacion_de_proyecto.__doc__.split())
    assert "todas sus tareas hechas y sin cerrar dice «En curso»" in d
    assert "ninguna hecha dice «Sin empezar»" in d


# ═══════════════════════════════════════════════════════════════════════
# G2: la próxima es la primera del orden de siempre
# ═══════════════════════════════════════════════════════════════════════

def _los_cinco():
    # {5: sin fecha, 4: vence en 8 días, 3: venció hace 4, 2: vence hoy, 1: sin fecha y grave,
    #  6: hecha, 7: descartada}: la grave, la vencida, la de hoy, la de fecha más lejana, la sin fecha.
    return [tarea(5), tarea(4, vence=_dia_rd(8)), tarea(3, vence=_dia_rd(-4)), tarea(2, vence=_dia_rd(0, 9)),
            tarea(1, grave=True), tarea(6, "hecha", completado=_dia_rd(-1)), tarea(7, "descartado")]


def test_G2_el_orden_de_las_pendientes_y_la_primera():
    s = situacion("activo", *_los_cinco())
    assert [f["id"] for f in s["pendientes"]] == [1, 3, 2, 4, 5]
    assert s["proxima"]["id"] == 1 and s["proxima"]["grave"] is True
    assert {f["id"] for f in s["otras"]} == {6, 7}


def test_G2_la_forma_de_la_proxima():
    s = situacion("activo", tarea(9, vence=_dia_rd(-2, 9), titulo="Llamar a la persona A"))
    assert s["proxima"] == {"id": 9, "titulo": "Llamar a la persona A", "vence": HOY - timedelta(days=2),
                            "vencida": True, "grave": False}
    assert set(s["proxima"]) == {"id", "titulo", "vence", "vencida", "grave"}
    sin_fecha = situacion("activo", tarea(8))["proxima"]
    assert sin_fecha["vence"] is None and sin_fecha["vencida"] is False


def test_G2_una_que_vence_hoy_no_esta_vencida_y_la_de_ayer_si():
    hoy_9, hoy_23 = tarea(1, vence=_dia_rd(0, 9)), tarea(2, vence=_dia_rd(0, 23))
    assert (hoy_9["vencida"], hoy_23["vencida"]) == (False, False)
    assert situacion("activo", hoy_23, tarea(3, vence=_dia_rd(-1, 10)))["proxima"]["id"] == 3
    # entre dos de hoy manda la hora (la de las 9 antes que la de las 23), con vencer ambas en el mismo día
    assert situacion("activo", hoy_23, hoy_9)["proxima"]["id"] == 1


def test_G2_con_fechas_sin_zona_y_con_zona_no_revienta_y_ordena():
    """Entradas inventadas (la guarda no se prueba solo con lo que hay hoy)."""
    sin_zona = tarea(1, vence=datetime(2026, 10, 12, 3))
    con_zona = tarea(2, vence=datetime(2026, 10, 11, 3, tzinfo=timezone.utc))
    assert [f["id"] for f in situacion("activo", sin_zona, con_zona)["pendientes"]] == [2, 1]


def test_G2_empata_por_id():
    s = situacion("activo", tarea(7), tarea(3), tarea(5))
    assert [f["id"] for f in s["pendientes"]] == [3, 5, 7]
    assert s["proxima"]["id"] == 3


def test_G2_sin_pendientes_no_hay_proxima():
    assert situacion("activo", tarea(1, "hecha", completado=_dia_rd(-1)), tarea(2, "descartado"))["proxima"] is None
    assert situacion("activo")["proxima"] is None


# ═══════════════════════════════════════════════════════════════════════
# G3: cerrado no tiene próxima; en pausa sí
# ═══════════════════════════════════════════════════════════════════════

def test_G3_cerrado_con_pendientes_no_tiene_proxima_pero_conserva_su_lista():
    s = situacion("cerrado", tarea(1, vence=_dia_rd(-1)), tarea(2))
    assert s["proxima"] is None
    assert s["estado"] == "Cerrado"
    assert [f["id"] for f in s["pendientes"]] == [1, 2] and s["n_pendientes"] == 2 and s["n_vencidas"] == 1


def test_G3_pausado_si_dice_cual_sigue():
    s = situacion("pausado", tarea(1), tarea(2, vence=_dia_rd(-1)))
    assert s["estado"] == "Pausado" and s["proxima"]["id"] == 2


def test_G3_un_estado_libre_se_trata_como_abierto():
    assert situacion("en revision", tarea(4))["proxima"]["id"] == 4


# ═══════════════════════════════════════════════════════════════════════
# Las cifras siguen siendo las de `_resumen`
# ═══════════════════════════════════════════════════════════════════════

def test_las_cifras_son_las_de_resumen_y_nada_se_pierde():
    filas = _los_cinco()
    s = situacion("activo", *filas)
    pend, otras = db._repartir(filas)
    assert {k: s[k] for k in ("n_total", "n_hechas", "n_otras", "n_pendientes", "n_vencidas", "pct")} == \
        db._resumen(pend, otras)
    assert (s["n_total"], s["n_hechas"], s["n_otras"], s["n_pendientes"], s["n_vencidas"]) == (7, 1, 1, 5, 1)
    assert len(s["pendientes"]) + len(s["otras"]) == len(filas)


# ═══════════════════════════════════════════════════════════════════════
# G4: la página (ruta real + plantilla real + SQL real sobre SQLite) dice lo de la función
# ═══════════════════════════════════════════════════════════════════════

def _sembrar(mundo):
    mundo.proyecto(1, "Nuevo", area="CDS")
    mundo.proyecto(2, "Empezado", area="CDS")
    mundo.proyecto(3, "Pausado", area="CDS", estado="pausado")
    mundo.proyecto(4, "Cerrado", area="CDS", estado="cerrado")
    mundo.proyecto(5, "Todo hecho", area="CDS")
    mundo.tarea(10, "sin fecha", proyecto=1)
    mundo.tarea(11, "vence en 5 días", proyecto=1, vence=_dia(5))
    mundo.tarea(12, "vencida", proyecto=1, vence=_dia(-2))
    mundo.tarea(20, "hecha", proyecto=2, estado="hecha", completado=_dia(-1))
    mundo.tarea(21, "pendiente", proyecto=2)
    mundo.tarea(30, "en pausa", proyecto=3)
    mundo.tarea(40, "de un cerrado", proyecto=4, vence=_dia(-1))
    mundo.tarea(50, "hecha", proyecto=5, estado="hecha", completado=_dia(-1))


def _pastilla(html: str) -> str:
    return re.search(r'<span class="pastilla">(Sin empezar|En curso|Pausado|Cerrado|[^<]*)</span>',
                     html.split('<main data-region="centro">', 1)[-1]).group(1)


def _orden_de_la_pagina(html: str) -> list[int]:
    centro = html.split('<main data-region="centro">', 1)[-1]
    primera_lista = centro.split('<details class="plegar">', 1)[0]
    return [int(x) for x in re.findall(r'data-tarea="(\d+)"', primera_lista)]


def test_G4_la_pagina_muestra_lo_que_calcula_la_funcion(mundo):
    _sembrar(mundo)
    esperado = {1: "Sin empezar", 2: "En curso", 3: "Pausado", 4: "Cerrado", 5: "En curso"}
    for pid, palabra in esperado.items():
        assert _pastilla(ver(mundo, p=pid)) == palabra, pid
    assert _orden_de_la_pagina(ver(mundo, p=1)) == [12, 11, 10]


def test_G4_la_pagina_toma_el_estado_y_el_orden_de_la_funcion_y_de_ninguna_otra_parte(mundo, monkeypatch):
    """Si la página decidiera por su cuenta, una función cambiada no se notaría en ella."""
    _sembrar(mundo)
    real = db.situacion_de_proyecto

    def cambiada(estado_guardado, filas):
        s = real(estado_guardado, filas)
        s["estado"] = "PALABRA-DE-LA-FUNCION"
        s["pendientes"] = list(reversed(s["pendientes"]))
        return s

    monkeypatch.setattr(db, "situacion_de_proyecto", cambiada)
    html = ver(mundo, p=1)
    assert _pastilla(html) == "PALABRA-DE-LA-FUNCION"
    assert _orden_de_la_pagina(html) == [10, 11, 12]


async def test_G4_el_modelo_lleva_la_proxima_de_la_funcion(mundo):
    """`armar_pagina` guarda `proxima` en el modelo de cada proyecto (SQL real sobre SQLite)."""
    _sembrar(mundo)
    p = (await db.pagina_de_proyectos(HOY))["proyectos"]
    assert p[1]["proxima"] == {"id": 12, "titulo": "vencida", "vence": HOY - timedelta(days=2),
                               "vencida": True, "grave": False}
    assert p[2]["proxima"]["id"] == 21
    assert p[3]["proxima"]["id"] == 30                      # en pausa: sí
    assert p[4]["proxima"] is None                          # cerrado con una pendiente: no
    assert p[5]["proxima"] is None                          # nada pendiente


def test_G4_un_cerrado_sigue_enseñando_sus_pendientes_como_hoy(mundo):
    _sembrar(mundo)
    html = ver(mundo, p=4)
    assert _pastilla(html) == "Cerrado" and 40 in _orden_de_la_pagina(html)


# ═══════════════════════════════════════════════════════════════════════
# G5: los hermanos — la lista sale de lo real
# ═══════════════════════════════════════════════════════════════════════

def _archivos_de_produccion(sufijos):
    """Todo el código que no es prueba: los `.py` los da LA puerta de los barridos del repositorio
    (`_py_en_disco`: no entra en entornos virtuales ni cachés) y las plantillas se leen de su carpeta.
    La lista SALE DEL DISCO. Frontera: no hay `.js` ni `.html` fuera de `web/plantillas/` (medido con
    `find` el 13-oct-2026)."""
    if ".py" in sufijos:
        for p in _py_en_disco(RAIZ):
            if p.relative_to(RAIZ).parts[0] != "tests":
                yield p
    if ".html" in sufijos:
        yield from sorted(PLANTILLAS.glob("*.html"))


def _quien_llama(nombre):
    """{(archivo, función que la contiene)} de cada llamada a `nombre` en producción."""
    llamadores = set()
    for p in _archivos_de_produccion({".py"}):
        arbol = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(arbol):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call):
                        f = n.func
                        usado = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                        if usado == nombre:
                            llamadores.add((p.relative_to(RAIZ).as_posix(), fn.name))
    return llamadores


def test_G5_solo_la_funcion_unica_decide_el_estado_con_sus_palabras():
    assert _quien_llama("estado_calculado") == {("db/db.py", "situacion_de_proyecto")}
    # y la función única la llama quien arma la página y nadie más
    assert _quien_llama("situacion_de_proyecto") == {("db/db.py", "armar_pagina")}


def test_G5_solo_repartir_ordena_y_solo_la_funcion_unica_reparte_a_un_proyecto():
    assert _quien_llama("_orden_de_pendientes") == set()      # se pasa como `key=`, no se llama
    llamadores = _quien_llama("_repartir")
    # `armar_pagina` (y su `_grupo`) también reparte las tareas SUELTAS de cada grupo (no son de ningún proyecto: no
    # tienen estado ni «próxima»). Cualquier otro llamador nuevo rompe esta prueba y hay que mirarlo.
    assert llamadores == {("db/db.py", "situacion_de_proyecto"), ("db/db.py", "armar_pagina"),
                          ("db/db.py", "_grupo")}      # `_grupo` vive dentro de `armar_pagina`
    fuente = (RAIZ / "db" / "db.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    armar = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "armar_pagina")
    reparten = [ast.get_source_segment(fuente, n) for n in ast.walk(armar)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_repartir"]
    assert len(reparten) == 2 and all("sueltas" in x or "huerfanas" in x for x in reparten), reparten


def test_G5_las_palabras_del_estado_no_estan_escritas_en_ningun_otro_sitio():
    palabras = ("Sin empezar", "En curso", "Pausado")
    donde = {}
    for p in _archivos_de_produccion({".py", ".html"}):
        texto = p.read_text(encoding="utf-8")
        for w in palabras:
            if f'"{w}"' in texto or f"'{w}'" in texto or f">{w}<" in texto:
                donde.setdefault(p.relative_to(RAIZ).as_posix(), set()).add(w)
    assert donde == {"db/db.py": set(palabras)}, donde


def test_G5_la_plantilla_pinta_el_estado_del_modelo_tal_cual():
    html = (RAIZ / "web" / "plantillas" / "proyectos.html").read_text(encoding="utf-8")
    assert html.count("m.estado_calculado") == 1 and "{{ m.estado_calculado }}" in html


# ═══════════════════════════════════════════════════════════════════════
# G6: lo que se ve no cambió (oráculo = la lógica de 7f962ce, copiada a mano)
# ═══════════════════════════════════════════════════════════════════════

def _estado_de_antes(estado, hechas):
    if estado == "cerrado":
        return "Cerrado"
    if estado == "pausado":
        return "Pausado"
    return "Sin empezar" if hechas == 0 else "En curso"


def _orden_de_antes(t):
    futuro = datetime.max.replace(tzinfo=timezone.utc)
    cuando = t["vence_en"] if isinstance(t["vence_en"], datetime) else futuro
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return (not t["grave"], not t["vencida"], t["vence_en"] is None, cuando, t["id"])


def _clases_de_tarea():
    vences = [None, _dia_rd(-1, 10), _dia_rd(-4), _dia_rd(0, 9), _dia_rd(0, 23), _dia_rd(1), _dia_rd(8),
              datetime(2026, 10, 9, 3), datetime(2026, 10, 12, 3)]
    for estado in ("pendiente", "hecha", "descartado", "rara"):
        for grave in (False, True):
            for v in vences:
                yield estado, grave, v


def test_G6_antes_y_ahora_dicen_lo_mismo_en_todas_las_combinaciones():
    clases = list(_clases_de_tarea())
    n_proyectos = 0
    for k in (0, 1, 2):
        for combo in itertools.combinations(range(len(clases)), k):
            filas = []
            for i, c in enumerate(combo, start=1):
                estado, grave, v = clases[c]
                filas.append(tarea(i, estado, vence=v, grave=grave,
                                   completado=_dia_rd(-2) if estado == "hecha" else None))
            for guardado in ("activo", "pausado", "cerrado", "en revision"):
                s = db.situacion_de_proyecto(guardado, filas)
                antes_pend = sorted((f for f in filas if f["es_pendiente"]), key=_orden_de_antes)
                antes_hechas = sum(1 for f in filas if f["es_hecha"])
                assert s["estado"] == _estado_de_antes(guardado, antes_hechas), (guardado, combo)
                assert [f["id"] for f in s["pendientes"]] == [f["id"] for f in antes_pend], (guardado, combo)
                assert s["n_hechas"] == antes_hechas and s["n_total"] == len(filas)
                n_proyectos += 1
    assert n_proyectos == 4 * (1 + len(clases) + len(clases) * (len(clases) - 1) // 2)


def test_G6_la_proxima_es_siempre_la_primera_de_las_pendientes_de_antes():
    clases = list(_clases_de_tarea())
    for combo in itertools.combinations(range(len(clases)), 2):
        filas = [tarea(i, clases[c][0], vence=clases[c][2], grave=clases[c][1],
                       completado=_dia_rd(-2) if clases[c][0] == "hecha" else None)
                 for i, c in enumerate(combo, start=1)]
        antes_pend = sorted((f for f in filas if f["es_pendiente"]), key=_orden_de_antes)
        for guardado in ("activo", "pausado", "en revision"):
            p = db.situacion_de_proyecto(guardado, filas)["proxima"]
            assert (p["id"] if p else None) == (antes_pend[0]["id"] if antes_pend else None), (guardado, combo)
        assert db.situacion_de_proyecto("cerrado", filas)["proxima"] is None
