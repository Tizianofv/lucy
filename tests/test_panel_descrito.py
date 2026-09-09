"""Lo que Lucy sabe del panel tiene que ser lo que el panel tiene.

EL FALLO QUE MOTIVA ESTE ARCHIVO, medido el 9-sep-2026 sobre 399fe2d:

Se publicó la pantalla `/tareas` y el menú de `web/plantillas/base.html` la
lista segunda. La descripción de la herramienta `panel` en `cerebro/agente.py`
—lo único que el modelo lee para decidir si manda el enlace— seguía diciendo
«gastos por mes, lo que falta clasificar y el detalle». La suite entera quedó
en verde: nada en el repositorio relacionaba las dos cosas.

Consecuencia concreta: para el modelo el panel era de plata, así que «muéstrame
los pendientes» o «las tareas» no tenían por qué llevar al enlace. Y la pantalla
de tareas es justo la que Rosi va a usar todos los días.

LO QUE SE PRUEBA ACÁ NO ES QUE HOY DIGA «TAREAS». Eso se arregla una vez y se
vuelve a romper con la pantalla siguiente. Lo que se prueba es que la lista SE
DERIVE del menú real, y que ninguna pantalla pueda entrar o salir en silencio.
La lista de pantallas de estos tests sale de `web/plantillas/base.html`: acá no
hay ningún nombre de pantalla tecleado como referencia, porque una lista
tecleada dentro del test que persigue listas tecleadas tiene el mismo defecto.

Herméticos: se stubea psycopg antes de importar, igual que en
test_esquema_del_modelo.py. No tocan ninguna base y no llaman a ningún modelo.

Correr:  python3 -m pytest tests/test_panel_descrito.py -q
"""
from __future__ import annotations

import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

# ── Stubs de psycopg ANTES de importar nada que abra el pool ─────────────
_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")


class _FalsoPool:
    def __init__(self, *a, **k):
        pass


_pool.AsyncConnectionPool = _FalsoPool
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _rows)
sys.modules.setdefault("psycopg_pool", _pool)

import cerebro.agente as agente  # noqa: E402
import web.menu as menu  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bloque_del_panel(texto: str) -> str:
    """El trozo del prompt que describe la herramienta `panel`, y solo ése.

    Mirar el prompt entero no serviría para lo que importa: «Movimientos» y
    «Salud» aparecen en otras partes del texto, así que el bloque del panel
    podría haberlos perdido y el test seguiría en verde. Es el mismo motivo por
    el que test_esquema_del_modelo mira el bloque de cada tabla y no el prompt
    completo.
    """
    inicio = texto.index("· panel  {}")
    resto = texto[inicio + 1:]
    fin = resto.find("\n· ")
    return texto[inicio:] if fin < 0 else texto[inicio:inicio + 1 + fin]


# ── La lista sale del menú, no de la memoria de nadie ────────────────────

def test_el_prompt_nombra_todas_las_pantallas_del_menu():
    """Ni una pantalla del panel puede faltarle al modelo.

    Éste es el test que habría dado rojo el día que se publicó `/tareas` sin
    tocar el prompt.
    """
    bloque = _bloque_del_panel(agente.herramientas_del_prompt())
    faltan = [f"{nombre} ({ruta})"
              for nombre, ruta in menu.pantallas()
              if nombre not in bloque or ruta not in bloque]
    assert not faltan, (
        f"{len(faltan)} pantallas que el panel tiene y Lucy no sabe que "
        f"existen: {faltan}. Por una pantalla que no conoce no manda el "
        "enlace, y quien la pide se queda sin respuesta.")


def test_el_prompt_no_nombra_pantallas_que_el_panel_no_tiene():
    """La dirección contraria, y también es roja.

    Una pantalla nombrada en el prompt que el menú no tiene es una promesa que
    el panel no cumple: Lucy manda el enlace diciendo que ahí está lo que
    pidieron, la persona entra, y no está. Cuesta más que no haberlo mandado,
    porque además la manda a buscar.

    Las rutas no se comparan contra una lista tecleada: se sacan del propio
    texto del prompt (cualquier `/loquesea`) y se cruzan contra el menú.
    """
    bloque = _bloque_del_panel(agente.herramientas_del_prompt())
    reales = {ruta for _, ruta in menu.pantallas()}
    nombradas = set(re.findall(r"(?<![\w/])(/[a-z0-9][a-z0-9-]*)", bloque))
    inventadas = sorted(nombradas - reales)
    assert not inventadas, (
        f"el prompt nombra rutas del panel que el menú no tiene: "
        f"{inventadas}. Lucy va a mandar a alguien a una pantalla que no "
        "existe.")


def test_ninguna_pantalla_esta_escrita_a_mano_en_el_prompt():
    """El marcador tiene que seguir siendo un marcador.

    Si alguien «arregla» esto pegando los nombres de las pantallas en el texto,
    los dos tests de arriba quedan verdes para siempre y la separación empieza
    de nuevo. Mismo criterio que las categorías
    (test_crud_dedup::test_el_agente_conoce_el_codigo_y_las_categorias_del_codigo).
    """
    fuente = open(os.path.join(RAIZ, "cerebro", "agente.py"),
                  encoding="utf-8").read()
    assert "{PANTALLAS_DEL_PANEL}" in fuente, (
        "el marcador de pantallas desapareció del prompt")
    assert "bloque_para_el_prompt(" in fuente, (
        "las pantallas ya no se inyectan desde el menú")

    bloque = _bloque_del_panel(fuente)
    # La ruta del resumen es "/" y aparece en cualquier texto: solo se mira
    # cuando dice algo. El nombre sí distingue, y se mira tal cual —con su
    # mayúscula—, que es como saldría de una copia del menú.
    a_mano = [f"{nombre} ({ruta})"
              for nombre, ruta in menu.pantallas()
              if nombre in bloque or (len(ruta) > 1 and ruta in bloque)]
    assert not a_mano, (
        f"pantallas copiadas a mano en el prompt: {a_mano}. Esa copia y el "
        "menú se separan el día que se escriben.")


def test_no_queda_ningun_marcador_sin_reemplazar():
    """Un `{...}` que llega crudo al modelo es una lista que no se inyectó."""
    armado = agente.herramientas_del_prompt()
    for marcador in ("{CATEGORIAS}", "{PANTALLAS_DEL_PANEL}"):
        assert marcador not in armado, (
            f"{marcador} llegó sin reemplazar al prompt")


def test_una_pantalla_nueva_en_el_menu_llega_sola_al_prompt():
    """El fondo: que no haga falta que nadie se acuerde.

    Se le da al parser un menú FABRICADO —no se toca la plantilla real— con una
    pantalla que no existe en ninguna parte del repositorio. Tiene que salir en
    el bloque sin haber editado una línea del prompt. Si esto se rompe, la
    lista volvió a ser tecleada.
    """
    fabricado = (
        '<nav>'
        '<a href="/" class="on">Resumen</a>'
        '<a href="/tareas">Tareas</a>'
        '<a href="/presupuesto">Presupuesto</a>'
        '</nav>')
    bloque = menu.bloque_para_el_prompt(fabricado)
    assert "Presupuesto" in bloque and "/presupuesto" in bloque, bloque
    assert bloque.index("Resumen") < bloque.index("Tareas") \
        < bloque.index("Presupuesto"), "se perdió el orden del menú"


def test_un_menu_ilegible_revienta_en_vez_de_quedarse_callado():
    """Sin menú no hay lista, y una lista vacía dejaría todo en verde."""
    import pytest
    with pytest.raises(RuntimeError):
        menu.pantallas("<html><body>ni un nav</body></html>")
    with pytest.raises(RuntimeError):
        menu.pantallas("<nav><span>texto suelto</span></nav>")


# ── El caso concreto del 9-sep-2026, fijado ─────────────────────────────

def test_las_palabras_con_las_que_se_piden_los_pendientes():
    """Las formas naturales de pedir la pantalla de tareas.

    Esto SÍ es una lista escrita a mano, y a propósito: es el caso de hoy
    clavado para que no vuelva callado, no la guarda general —esa es la de
    arriba, que se deriva del menú—. Mismo papel que
    test_esquema_del_modelo::test_las_tres_columnas_del_fallo_estan.

    Salen de cómo lo pidió Tiziano el 9-sep-2026 («el panel de pendientes») y
    de cómo lo va a pedir Rosi todos los días.
    """
    bloque = _bloque_del_panel(agente.herramientas_del_prompt()).lower()
    faltan = [p for p in ("pendientes", "tareas", "qué hay que hacer")
              if p not in bloque]
    assert not faltan, (
        f"el prompt no relaciona el panel con {faltan}: quien lo pida así "
        "no recibe el enlace.")
