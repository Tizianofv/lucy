"""Lo que Lucy promete del panel tiene que ser lo que el panel tiene.

EL FALLO QUE MOTIVA ESTE ARCHIVO, medido el 9-sep-2026 sobre 399fe2d:

Se publicó la pantalla `/tareas` y el menú de `web/plantillas/base.html` la
lista segunda. La descripción de la herramienta `panel` en `cerebro/agente.py`
—lo único que el modelo lee para decidir si manda el enlace— seguía diciendo
«gastos por mes, lo que falta clasificar y el detalle». La suite entera quedó
en verde: nada en el repositorio relacionaba las dos cosas.

Consecuencia concreta: para el modelo el panel era de plata, así que «muéstrame
los pendientes» o «las tareas» no tenían por qué llevar al enlace. Y la pantalla
de tareas es justo la que Rosi va a usar todos los días.

EL SEGUNDO FALLO, EL DE LA TARDE DEL MISMO DÍA, medido sobre `ca2c421`:

La lista de pantallas ya se derivaba del menú, pero LAS PALABRAS CON LAS QUE SE
PIDEN seguían tecleadas en el prompt. Sacando `/tareas` del `<nav>` y sin tocar
nada más:

    · la lista derivada perdió Tareas, correctamente
    · el prompt siguió diciendo «los pendientes», «las tareas», «qué hay que
      hacer» y «la pantalla de tareas es de las DOS personas de la casa»
    · este archivo: 7 passed

O sea que Lucy seguía invitando a pedir una pantalla que el panel ya no tenía:
manda el enlace, la persona entra, y no está. Cuesta más que no haberlo mandado,
porque además la manda a buscar. Una lista tecleada al lado de una derivada.

LO QUE SE PRUEBA ACÁ NO ES QUE HOY DIGA «TAREAS». Eso se arregla una vez y se
vuelve a romper con la pantalla siguiente. Lo que se prueba es que TODO lo que
el bloque del panel dice de una pantalla —su nombre, su ruta, las palabras con
las que se la pide y su nota— salga del menú, y que ninguna pantalla pueda
entrar o salir en silencio. Acá no hay ningún nombre de pantalla ni ninguna
frase tecleada como referencia, porque una lista tecleada dentro del test que
persigue listas tecleadas tiene el mismo defecto.

Y EL TERCER FALLO, EL DEL RADIO DE DAÑO. `_sistema()` se arma en CADA mensaje,
no solo cuando piden el panel. Como leía el menú sin red, un `</nav>` mal
cerrado hacía que cualquier mensaje —«¿qué hora es?»— terminara en «Fallo
definitivo» con una repregunta genérica. Un typo en el menú del panel dejaba a
Lucy sin contestar nada. Eso se fija abajo en las dos direcciones: que siga
contestando, y que el aviso salga igual de fuerte.

Y EL CUARTO, EL DE LA NOCHE DEL 9-sep-2026: EL MISMO, POR OTRA PUERTA.

El arreglo del radio de daño blindó el HTML mal formado y dejó afuera el
archivo ilegible, que llega por la misma llamada. `pantallas()` atrapaba
`OSError` y `UnicodeDecodeError` NO es un `OSError` —es `ValueError`—, así que
un `base.html` con la codificación cambiada (un pegado desde Windows-1252, un
merge mal resuelto) se escapaba de las dos redes y volvía a dejar a Lucy sin
contestar nada. Medido antes del arreglo, llamando al código real:

    · base.html con un byte 0xff  → UnicodeDecodeError SIN ATRAPAR
    · el archivo no está          → aviso y Lucy sigue contestando
    · una carpeta en su lugar     → aviso y Lucy sigue contestando

Y la prueba de «un solo sitio» tenía la misma especie de defecto un centímetro
más abajo: buscaba el TEXTO «MenuIlegible» en el `ast.dump` del `except`, así
que un segundo sitio que la importara con alias se le escapaba. Medido: con la
prueba vieja, `from web.menu import MenuIlegible as ME … except ME:` daba
1 passed. Ahora se resuelve el símbolo contra los imports del archivo.

Las dos veces el diagnóstico fue el mismo —el criterio bueno aplicado a un
tramo y no a su hermano— y las dos veces el arreglo fue quitarle la lista: la
lectura del archivo se blinda por el ALCANCE del `try` y no por los tipos, y el
`except` se reconoce por la CLASE y no por su nombre escrito.

Y EL QUINTO, EL MISMO DIAGNÓSTICO CON LOS DOS TRAMOS DENTRO DE UNA FUNCIÓN:
LAS PRUEBAS MEDÍAN LA COSTURA Y DABAN POR CUBIERTO EL CAMINO REAL.

`pantallas(fuente=None)` tiene dos ramas: con `fuente` usa el HTML que le pasan
—la costura que existe para poder probar sin tocar el disco— y sin `fuente` lee
`web/plantillas/base.html`. **Producción entra SIEMPRE por la segunda**
(`cerebro/agente.py` llama `bloque_para_el_prompt()` sin argumento), y casi
todas las pruebas de acá entraban por la primera. Medido el 9-sep-2026 sobre
`2d29bab`, mutando el código real y corriendo la suite entera:

    · el parseo del `<nav>` metido dentro del `try` SOLO en la rama que lee
      el archivo                                    → 503 passed / 18 passed
    · `data-nota` leído con `_atributo` en vez de por la puerta `_leer`
                                                    → 503 passed / 18 passed
    · la rama que lee el archivo borrando en silencio los `<a>` sin
      `data-tambien`                                → 503 passed / 18 passed

Las tres son la garantía que este archivo se atribuye, rota en el único camino
que corre en producción, y las tres pasaban en verde. La primera es literal el
defecto que motivó el cuarto fallo, entrando por la rama que nadie probaba.

EL ARREGLO NO ES DUPLICAR CADA PRUEBA —una por rama—, porque mañana la cuarta
se escribe por un solo lado. Es que **la costura deje de ser una forma de medir
y pase a ser una sola puerta**: `_por_los_dos_caminos()` corre el menú fabricado
por las DOS ramas y exige que den lo mismo, y `test_ninguna_prueba_mide_el_menu
_solo_por_la_costura` —que saca su lista del AST de los archivos de prueba que
hay en disco, no de un inventario— se pone roja si alguien vuelve a llamar a
`pantallas(html)` por fuera. Una prueba escrita por la costura ya no puede dar
por cubierto el camino real, y la que se escriba mañana lo cubre sin que nadie
se acuerde.

Herméticos: se stubea psycopg antes de importar, igual que en
test_esquema_del_modelo.py. No tocan ninguna base y no llaman a ningún modelo.

Correr:  python3 -m pytest tests/test_panel_descrito.py -q
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

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

import pytest  # noqa: E402

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


def _entrecomillado(texto: str) -> set[str]:
    """Todo lo que va entre comillas dobles en un texto."""
    return set(re.findall(r'"([^"]*)"', texto))


# ── LA PUERTA ÚNICA para medir un menú fabricado ─────────────────────────

def _por_los_dos_caminos(html: str, funcion=None):
    """El menú fabricado, por la costura Y por el camino que corre en producción.

    POR QUÉ EXISTE, medido el 9-sep-2026 sobre `2d29bab`:

    `pantallas(fuente)` tiene dos ramas —el HTML que le pasan, o el archivo que
    lee— y PRODUCCIÓN ENTRA SIEMPRE POR LA SEGUNDA: `cerebro/agente.py` llama
    `bloque_para_el_prompt()` sin argumento. Las pruebas de menús fabricados
    entraban por la primera, así que las garantías más caras de `web/menu.py`
    estaban protegidas en la rama que nadie ejecuta. Tres mutaciones del código
    real —el parseo dentro del `try` solo en la rama que lee el archivo, un
    atributo saltándose la puerta `_leer`, y esa misma rama borrando en silencio
    los enlaces sin `data-tambien`— daban las tres 503 passed / 18 passed.

    Acá el mismo HTML se corre por las dos ramas y se exige que den EXACTAMENTE
    lo mismo: el mismo valor, o el mismo tipo de excepción con el mismo mensaje.
    No hay que escribir la prueba dos veces, y una rama que se desvíe sale roja
    aunque la prueba se haya escrito pensando solo en la otra.

    La ruta del menú se normaliza antes de comparar los mensajes —la costura
    nombra `web/plantillas/base.html` y el camino real nombra el archivo
    temporal— porque lo que se compara es el comportamiento, no dónde vivía el
    archivo.

    Devuelve lo que devolvieron, y si reventaron re-lanza la excepción, así que
    se usa igual que la función de siempre, con `pytest.raises` incluido.

    LO QUE QUEDA FUERA, dicho para que nadie lo dé por cubierto: el camino real
    se corre contra un archivo temporal, no contra `web/plantillas/base.html`.
    Es el mismo código —leer del disco y parsear— y por eso vale; lo que NO
    prueba es el contenido del menú de verdad, y de eso hablan las pruebas que
    llaman sin fabricar nada (`test_el_prompt_nombra_todas_las_pantallas_del
    _menu` y sus hermanas). Y si las dos ramas se rompen IGUAL, acá coinciden y
    esto se calla: lo que atrapa esa mitad es la propia prueba que llama.
    """
    funcion = menu.pantallas if funcion is None else funcion

    def _correr(llamar):
        try:
            return ("devolvió", llamar())
        except Exception as e:      # se compara y se re-lanza más abajo
            return ("reventó", e)

    por_la_costura = _correr(lambda: funcion(html))

    carpeta = tempfile.mkdtemp(prefix="menu-camino-real-")
    falso = Path(carpeta) / "base.html"
    falso.write_text(html, encoding="utf-8")
    original = menu.BASE
    menu.BASE = falso
    try:
        por_el_camino_real = _correr(lambda: funcion())
    finally:
        menu.BASE = original
        shutil.rmtree(carpeta, ignore_errors=True)

    def _comparable(salida, base):
        que, valor = salida
        texto = (repr(valor) if que == "devolvió"
                 else f"{type(valor).__name__}: {valor}")
        return que, texto.replace(str(base), "<el archivo del menú>")

    costura = _comparable(por_la_costura, original)
    real = _comparable(por_el_camino_real, falso)
    assert costura == real, (
        "el mismo menú da cosas distintas según por dónde entre, y producción "
        f"entra SIEMPRE por el camino real:\n"
        f"  por la costura   ({funcion.__name__}(html)) → {costura}\n"
        f"  camino real      ({funcion.__name__}())     → {real}\n"
        "Una garantía que solo vale en la rama de las pruebas no protege nada.")

    que, valor = por_la_costura
    if que == "reventó":
        raise valor
    return valor


# ── La lista sale del menú, no de la memoria de nadie ────────────────────

def test_el_prompt_nombra_todas_las_pantallas_del_menu():
    """Ni una pantalla del panel puede faltarle al modelo.

    Éste es el test que habría dado rojo el día que se publicó `/tareas` sin
    tocar el prompt.
    """
    bloque = _bloque_del_panel(agente.herramientas_del_prompt())
    faltan = [f"{p.nombre} ({p.ruta})"
              for p in menu.pantallas()
              if p.nombre not in bloque or p.ruta not in bloque]
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
    reales = {p.ruta for p in menu.pantallas()}
    nombradas = set(re.findall(r"(?<![\w/])(/[a-z0-9][a-z0-9-]*)", bloque))
    inventadas = sorted(nombradas - reales)
    assert not inventadas, (
        f"el prompt nombra rutas del panel que el menú no tiene: "
        f"{inventadas}. Lucy va a mandar a alguien a una pantalla que no "
        "existe.")


def test_las_frases_del_bloque_son_exactamente_las_que_declara_el_menu():
    """LO QUE EL MODELO ESCUCHA SALE DEL MENÚ, Y DE NINGÚN OTRO SITIO.

    Ésta es la prueba del segundo fallo. Las palabras con las que una persona
    pide una pantalla —«los pendientes» no contiene «Tareas»— no se pueden
    derivar del nombre, así que viajan en el propio `<a>` del menú
    (`data-tambien`) y son parte de la pantalla: si la pantalla se va del menú,
    sus frases se van con ella.

    La comparación es en las DOS direcciones y contra el menú, nunca contra una
    lista escrita acá:

      · una frase en el bloque que el menú no declara  → alguien la tecleó, y
        va a sobrevivir a la pantalla que la justificaba. Es el fallo entero.
      · una frase que el menú declara y no llegó al bloque → la inyección se
        perdió algo y Lucy no manda el enlace a quien la pide así.

    Y es total porque el texto del prompt no tiene ni una comilla propia; eso lo
    fija test_ninguna_pantalla_ni_frase_esta_escrita_a_mano_en_el_prompt.
    """
    bloque = _bloque_del_panel(agente.herramientas_del_prompt())
    en_el_bloque = _entrecomillado(bloque)
    en_el_menu = menu.frases_declaradas()

    tecleadas = sorted(en_el_bloque - en_el_menu)
    assert not tecleadas, (
        f"el bloque del panel le pide al modelo que escuche frases que el menú "
        f"no declara: {tecleadas}. Ésas quedan aunque la pantalla se vaya, y "
        "entonces Lucy invita a pedir algo que el panel ya no tiene.")

    perdidas = sorted(en_el_menu - en_el_bloque)
    assert not perdidas, (
        f"el menú declara frases que no llegaron al prompt: {perdidas}. Quien "
        "pida la pantalla con esas palabras no recibe el enlace.")


def test_ninguna_pantalla_ni_frase_esta_escrita_a_mano_en_el_prompt():
    """El marcador tiene que seguir siendo un marcador.

    Si alguien «arregla» esto pegando los nombres o las frases en el texto, los
    tests de arriba quedan verdes para siempre y la separación empieza de nuevo.
    Mismo criterio que las categorías
    (test_crud_dedup::test_el_agente_conoce_el_codigo_y_las_categorias_del_codigo).

    La regla de la comilla es la que hace TOTAL a la comparación de frases, y se
    puede decir en una línea: en el bloque del panel el texto explica el
    mecanismo y NADA MÁS; todo lo que el modelo tiene que escuchar entra por el
    marcador. Así, cualquier ejemplo tecleado —exista o no la pantalla— aparece
    como una comilla que el menú no declara.
    """
    fuente = open(os.path.join(RAIZ, "cerebro", "agente.py"),
                  encoding="utf-8").read()
    assert "{PANTALLAS_DEL_PANEL}" in fuente, (
        "el marcador de pantallas desapareció del prompt")
    assert "bloque_para_el_prompt(" in fuente, (
        "las pantallas ya no se inyectan desde el menú")

    bloque = _bloque_del_panel(fuente)

    assert '"' not in bloque.replace("{}", ""), (
        "el bloque del panel tiene texto entrecomillado propio: "
        f"{sorted(_entrecomillado(bloque))}. Ahí no va ni un ejemplo tecleado; "
        "todo lo que el modelo tiene que escuchar entra por "
        "{PANTALLAS_DEL_PANEL}, que sale del menú.")

    # La ruta del resumen es "/" y aparece en cualquier texto: solo se mira
    # cuando dice algo. El nombre sí distingue, y se mira tal cual —con su
    # mayúscula—, que es como saldría de una copia del menú.
    a_mano = [f"{p.nombre} ({p.ruta})"
              for p in menu.pantallas()
              if p.nombre in bloque or (len(p.ruta) > 1 and p.ruta in bloque)]
    assert not a_mano, (
        f"pantallas copiadas a mano en el prompt: {a_mano}. Esa copia y el "
        "menú se separan el día que se escriben.")

    frases = sorted(f for f in menu.frases_declaradas() if f in bloque)
    assert not frases, (
        f"frases del menú copiadas a mano en el prompt: {frases}. El día que "
        "esa pantalla se vaya del menú, la frase se queda y Lucy sigue "
        "invitando a pedirla.")


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
    el bloque, con sus frases, sin haber editado una línea del prompt. Si esto
    se rompe, la lista volvió a ser tecleada.
    """
    fabricado = (
        '<nav>'
        '<a href="/" class="on" data-tambien="ver mis gastos">Resumen</a>'
        '<a href="/tareas" data-tambien="los pendientes">Tareas</a>'
        '<a href="/presupuesto" data-tambien="cómo voy con el presupuesto,'
        ' cuánto me queda" data-nota="es del mes en curso">Presupuesto</a>'
        '</nav>')
    bloque = _por_los_dos_caminos(fabricado, menu.bloque_para_el_prompt)
    assert "Presupuesto" in bloque and "/presupuesto" in bloque, bloque
    assert '"cuánto me queda"' in bloque, (
        f"la frase nueva no llegó al prompt: {bloque}")
    assert "es del mes en curso" in bloque, (
        f"la nota nueva no llegó al prompt: {bloque}")
    assert bloque.index("Resumen") < bloque.index("Tareas") \
        < bloque.index("Presupuesto"), "se perdió el orden del menú"


def test_cada_pantalla_del_menu_declara_como_se_la_pide():
    """Una pantalla nueva no puede nacer muda.

    El nombre del menú casi nunca es como la pide una persona: nadie dice
    «Tareas», dicen «los pendientes». Sin esa declaración la pantalla llega al
    prompt como un nombre suelto y Lucy no manda el enlace a quien la pide con
    sus palabras de todos los días — que es exactamente el 9-sep-2026 otra vez.

    La lista de pantallas que se recorre sale del menú de verdad; acá no hay
    ninguna tecleada.
    """
    mudas = [f"{p.nombre} ({p.ruta})" for p in menu.pantallas() if not p.frases]
    assert not mudas, f"pantallas sin data-tambien: {mudas}"


# ── Lo que se hace cuando el menú no se puede leer ───────────────────────

def test_un_menu_ilegible_revienta_en_vez_de_quedarse_callado():
    """Sin menú no hay lista, y una lista vacía dejaría todo en verde.

    Las cinco formas de no poder leerlo, y las cinco son MenuIlegible: no hay
    `<nav>`, el `<nav>` no tiene enlaces al panel, un enlace sin texto visible,
    un enlace sin `data-tambien`, y una nota con comillas (que rompería la
    comparación de frases contra el menú).

    LAS TRES ÚLTIMAS VAN CON UNA PANTALLA SANA AL LADO, y el motivo se midió:
    con el enlace malo SOLO en el menú, `pantallas()` puede salir roja por el
    `if not salida` del final —o sea por el motivo equivocado— y una versión que
    se salte el enlace malo en silencio pasa igual. Medido el 9-sep-2026: con el
    `raise` de `data-tambien` cambiado por un `continue`, este archivo daba
    12 passed. Con la pantalla sana al lado, da rojo.
    """
    sana = '<a href="/" data-tambien="el arranque">Portada</a>'
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos("<html><body>ni un nav</body></html>")
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos("<nav><span>texto suelto</span></nav>")
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos(f'<nav>{sana}<a href="/x" data-tambien="lo de x">'
                             '</a></nav>')
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos(f'<nav>{sana}<a href="/x">Equis</a></nav>')
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos(f'<nav>{sana}<a href="/x" data-tambien="lo de x"'
                             ' data-nota="dice &quot;hola&quot;">Equis</a></nav>')


def test_no_poder_leer_el_archivo_es_menu_ilegible_se_llame_como_se_llame():
    """EL FONDO: acá no puede haber una lista de tipos de fallo.

    El `except` de la lectura decía `OSError`, y `UnicodeDecodeError` NO es un
    OSError. Un `base.html` con la codificación cambiada se escapaba de
    `pantallas()`, se escapaba del `except MenuIlegible` de `cerebro/agente.py`
    y terminaba en «Fallo definitivo»: Lucy sin contestar nada.

    Lo que se prueba acá NO es que atrape `UnicodeDecodeError` —eso sería la
    lista de mañana, con un elemento más—. El tipo del fallo LO INVENTA ESTA
    PRUEBA EN EL MOMENTO, así que ninguna lista escrita en ninguna parte puede
    contenerlo: si alguien vuelve a enumerar tipos, esto se pone rojo sin que
    nadie tenga que acordarse de agregar el caso nuevo.
    """
    class _FalloQueNadiePuedeTenerEnUnaLista(Exception):
        pass

    class _RutaQueNoSeDejaLeer:
        def read_text(self, *a, **k):
            raise _FalloQueNadiePuedeTenerEnUnaLista("ni OSError ni Unicode")

        def __str__(self):
            return "<una ruta de prueba>"

    original = menu.BASE
    menu.BASE = _RutaQueNoSeDejaLeer()
    try:
        with pytest.raises(menu.MenuIlegible) as e:
            menu.pantallas()
    finally:
        menu.BASE = original

    assert "_FalloQueNadiePuedeTenerEnUnaLista" in str(e.value), (
        f"el motivo real no llega al mensaje: {e.value}. Sin el tipo adentro, "
        "el que lea el log no sabe qué le pasó al menú.")
    assert isinstance(e.value.__cause__, _FalloQueNadiePuedeTenerEnUnaLista), (
        "se perdió la excepción original: sin `from e` el rastro no lleva al "
        "fallo de verdad")


def test_un_fallo_del_parseo_no_se_disfraza_de_menu_ilegible():
    """EL OTRO EXTREMO, y lo fija el ALCANCE, no una lista.

    Tragarse cualquier cosa tampoco sirve: si el `try` de la lectura creciera
    hasta cubrir el parseo, un error de programación de ahí abajo saldría
    disfrazado de «no pude leer el menú», `cerebro/agente.py` se lo tragaría y
    Lucy seguiría contestando con un prompt al que le falta algo, sin que nadie
    se entere.

    Se rompe algo que corre DESPUÉS de la lectura y se exige que salga entero.

    Y SE EXIGE EN LAS DOS RAMAS, que es lo que faltaba. Esto llamaba
    `menu.pantallas("<nav></nav>")` —con `fuente`—, o sea que corría por la rama
    de la costura y NUNCA por la que lee el archivo, que es la única que usa
    `cerebro/agente.py`. Medido el 9-sep-2026 sobre `2d29bab`: metiendo el
    parseo del `<nav>` dentro del `try` SOLO en la rama que lee el archivo, la
    suite entera daba 503 passed y este archivo 18 passed, con el agujero vivo
    en producción — un fallo de programación disfrazado de «no pude leer el
    menú», tragado por `agente.py`, y Lucy contestando con un prompt al que le
    falta el panel sin que nadie se entere.
    """
    class _NavRoto:
        def findall(self, *a, **k):
            raise ValueError("esto no es un fallo de lectura")

    original = menu._NAV
    menu._NAV = _NavRoto()
    try:
        with pytest.raises(ValueError):
            _por_los_dos_caminos("<nav></nav>")
    finally:
        menu._NAV = original


def test_una_pantalla_no_se_evapora_por_como_estan_escritas_las_comillas():
    """El mismo defecto, un centímetro más abajo: el atributo por su forma.

    `_atributo` leía solo `nombre="valor"`. Con `href='/tareas'` —HTML
    perfectamente válido— el href salía None, la ruta salía "", no empezaba por
    "/" y LA PANTALLA SE SALTABA EN SILENCIO. Su hermano `data-tambien`, con las
    mismas comillas simples, sí caía del lado seguro. Medido el 9-sep-2026 sobre
    el mismo menú, cambiando solo las comillas:

        href="/tareas"   → ['/', '/tareas']
        href='/tareas'   → ['/']            ← la pantalla se evaporó

    Acá se exige que las tres formas válidas de HTML den la MISMA pantalla, y
    que un atributo que está y no se puede leer reviente en vez de saltarse.
    """
    sana = '<a href="/" data-tambien="el arranque">Portada</a>'
    formas = [
        '<a href="/tareas" data-tambien="los pendientes">Tareas</a>',
        "<a href='/tareas' data-tambien='los pendientes'>Tareas</a>",
        "<a href=/tareas data-tambien='los pendientes'>Tareas</a>",
    ]
    for forma in formas:
        pantallas = _por_los_dos_caminos(f"<nav>{sana}{forma}</nav>")
        assert [p.ruta for p in pantallas] == ["/", "/tareas"], (
            f"la pantalla cambió según cómo se escribió el atributo: {forma} "
            f"→ {[p.ruta for p in pantallas]}")
        assert pantallas[1].frases == ("los pendientes",), (
            f"las frases cambiaron según las comillas: {forma} → {pantallas}")

    # Y el atributo que está pero no se puede leer no se salta en silencio.
    # Acá va solo el `href`, que es el que se evaporaba; los TRES atributos —y
    # los que haya mañana— los recorre
    # test_un_atributo_que_esta_y_no_se_deja_leer_revienta_sea_cual_sea, que
    # saca la lista del AST de `web/menu.py` en vez de teclearla.
    with pytest.raises(menu.MenuIlegible):
        _por_los_dos_caminos(f'<nav>{sana}<a href data-tambien="lo de x">Equis'
                             '</a></nav>')


def test_un_enlace_de_fuera_del_panel_no_es_una_pantalla():
    """La única exención, y se puede predecir sin correr nada.

    Un `<a>` del menú que no apunta a una ruta del propio panel no es una
    pantalla del panel, así que no tiene que declarar nada. Se dice en una línea
    y se comprueba corriendo, que es lo que le falta a una exención escrita solo
    en un comentario.
    """
    con_externo = (
        '<nav>'
        '<a href="/" data-tambien="ver mis gastos">Resumen</a>'
        '<a href="https://railway.app">Railway</a>'
        '<a href="mailto:tiziano@example.com">Escribir</a>'
        '</nav>')
    pantallas = _por_los_dos_caminos(con_externo)
    assert [p.ruta for p in pantallas] == ["/"], (
        f"la frontera no es la que dice el archivo: {pantallas}")


# ── Los atributos del enlace, y que TODOS pasen por la misma puerta ──────

def _atributos_del_enlace():
    """Qué atributos lee `web/menu.py` del `<a>`, y por qué puerta. Del AST.

    DE DÓNDE SALE LA LISTA, porque una comprobación vale lo que valga su lista:
    de los literales que el propio código le pasa a `_leer` y a `_atributo`,
    leídos del árbol de `web/menu.py`. Acá no hay ningún nombre de atributo
    tecleado, así que un cuarto atributo que alguien agregue mañana entra solo.

    Los nombres de las dos funciones tampoco se teclean: salen de los objetos
    reales (`menu._leer.__name__`), así que un renombre no abre el agujero.

    Devuelve tres cosas, y la tercera es la que le pone fondo:
      · los que pasan por la puerta (`_leer`)
      · los que van directo al crudo (`_atributo`) — tienen que ser cero
      · las llamadas que NO se pueden resolver leyendo. Lo que no se sabe
        cuenta como rojo; la única que se descuenta es la que `_leer` le hace
        a `_atributo`, que es la puerta llamando a lo que envuelve, y se
        reconoce por estar DENTRO de su definición, no por su nombre.
    """
    with open(os.path.join(RAIZ, "web", "menu.py"), encoding="utf-8") as f:
        arbol = ast.parse(f.read(), "web/menu.py")

    puerta, crudo = menu._leer.__name__, menu._atributo.__name__
    dentro_de_la_puerta = {
        id(x)
        for n in ast.walk(arbol)
        if isinstance(n, ast.FunctionDef) and n.name == puerta
        for x in ast.walk(n)}

    por_la_puerta, sin_puerta, sin_resolver = set(), set(), []
    for n in ast.walk(arbol):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in (puerta, crudo)):
            continue
        if id(n) in dentro_de_la_puerta and n.func.id == crudo:
            continue
        arg = n.args[1] if len(n.args) > 1 else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            (por_la_puerta if n.func.id == puerta else sin_puerta).add(arg.value)
        else:
            sin_resolver.append(f"{n.func.id}(…) en la línea {n.lineno}")
    return por_la_puerta, sin_puerta, sin_resolver


# Un valor válido por atributo, para poder fabricar el enlace de la prueba de
# abajo. NO es la lista de atributos —ésa sale del AST—: es material de prueba,
# y la prueba exige que cubra exactamente lo que el AST encontró. Al lado
# indulgente no se llega por olvido: un atributo nuevo sin valor acá sale rojo
# pidiendo el valor, no se salta en silencio.
_VALOR_SANO = {
    "href": "/x",
    "data-tambien": "lo de x",
    "data-nota": "una nota",
}


def test_los_atributos_del_enlace_pasan_todos_por_la_misma_puerta():
    """¿Quiénes son sus hermanos, y lo cumplen todos? Leído, no recordado.

    `_leer` existe para que ningún atributo tenga su propio criterio con un
    valor que no se entiende. Que hoy los tres pasen por ahí se sabe leyendo el
    archivo; esta prueba lo sabe CORRIENDO, así que el cuarto atributo no puede
    nacer con el defecto.

    Y hacen falta las dos mitades: ésta mira la fuente compartida, y
    test_un_atributo_que_esta_y_no_se_deja_leer_revienta_sea_cual_sea mira el
    comportamiento. Una prueba de comportamiento sola no ve que dos hermanos
    usen criterios distintos que hoy dan el mismo resultado.

    Medido el 9-sep-2026 sobre `2d29bab`: con `data-nota` leído por `_atributo`
    en vez de por `_leer`, la suite entera daba 503 passed y este archivo
    18 passed. El `ILEGIBLE` crudo llegaba a `.strip()` y salía un
    `AttributeError` que `cerebro/agente.py` no atrapa —solo atrapa
    `MenuIlegible`—, o sea `_sistema()` tumbado en CADA mensaje.
    """
    por_la_puerta, sin_puerta, sin_resolver = _atributos_del_enlace()

    assert por_la_puerta, (
        "no encontré ni un atributo leído por la puerta en web/menu.py: o el "
        "archivo cambió de forma o esta prueba dejó de ver lo que mira, y una "
        "prueba que no ve nada pasa siempre")
    assert not sin_puerta, (
        f"atributos que se saltan la puerta {menu._leer.__name__} y leen el "
        f"crudo directo: {sorted(sin_puerta)}. Ése es el criterio propio que "
        f"{menu._leer.__name__} vino a quitar: el valor ILEGIBLE sale sin "
        "envolver, no es MenuIlegible, y cerebro/agente.py no lo atrapa.")
    assert not sin_resolver, (
        f"no puedo resolver leyendo estas lecturas de atributo: {sin_resolver}. "
        "Lo que no se sabe cuenta como rojo: si el nombre del atributo se arma "
        "al vuelo, esta prueba ya no puede afirmar que todos pasan por la "
        "puerta, y un PASA ahí sería inventado.")


def test_un_atributo_que_esta_y_no_se_deja_leer_revienta_sea_cual_sea():
    """La otra mitad: el comportamiento, atributo por atributo y en las DOS ramas.

    «El atributo está y no se pudo leer» es distinto de «no está»: lo primero es
    MenuIlegible, lo segundo puede ser la exención declarada. Sin esto, un
    atributo escrito sin valor se salta en silencio y la pantalla desaparece del
    prompt sin un rojo en ninguna parte.

    Se recorren TODOS los atributos que el AST encontró —no una lista de acá— y
    cada uno se mide por la costura y por el camino real a la vez.

    Y SE EXIGE QUE HAYA REVENTADO EN LA PUERTA, no solo que haya reventado.
    Con `data-tambien` escrito sin valor, una versión que se lo saltara en
    silencio saldría igual de MenuIlegible —por el «no declara data-tambien» de
    más abajo—, o sea VERDE por el motivo equivocado. El sitio del que salió se
    le pregunta al rastro de la excepción, no al texto del mensaje: un cambio de
    redacción no puede aflojar esto.
    """
    por_la_puerta, _, _ = _atributos_del_enlace()
    assert set(_VALOR_SANO) == por_la_puerta, (
        f"web/menu.py lee {sorted(por_la_puerta)} y acá hay valores de prueba "
        f"para {sorted(_VALOR_SANO)}. No se puede fabricar el enlace sin un "
        "valor válido para cada uno: agrégalo, o esta prueba estaría dando por "
        "cubierto un atributo que nunca ejercita.")

    sana = '<a href="/" data-tambien="el arranque">Portada</a>'
    for roto in sorted(por_la_puerta):
        atributos = " ".join(
            n if n == roto else f'{n}="{_VALOR_SANO[n]}"'
            for n in sorted(por_la_puerta))
        html = f'<nav>{sana}<a {atributos}>Equis</a></nav>'
        with pytest.raises(menu.MenuIlegible) as e:
            _por_los_dos_caminos(html)

        rastro = e.value.__traceback__
        marcos = set()
        while rastro is not None:
            marcos.add(rastro.tb_frame.f_code.co_name)
            rastro = rastro.tb_next
        assert menu._leer.__name__ in marcos, (
            f"{roto} escrito sin valor sí revienta, pero no en "
            f"{menu._leer.__name__}: salió de {sorted(marcos)}. O sea que ese "
            "atributo no pasa por la puerta y hoy está verde de casualidad, "
            f"por otra comprobación. Mensaje: {e.value}")


@pytest.mark.parametrize("romper, motivo", [
    (lambda p: p.write_text("<header><nav><a href='/'>Resumen</a></header>",
                            encoding="utf-8"),
     "un </nav> sin cerrar"),
    (lambda p: p.write_bytes(b'<nav><a href="/" data-tambien="algo">'
                             b"Resumen \xff</a></nav>"),
     "un byte que no es UTF-8 valido"),
    (lambda p: None, "el archivo del menu no esta"),
    (lambda p: p.mkdir(), "donde iba el menu hay una carpeta"),
])
def test_un_menu_roto_no_deja_a_lucy_sin_contestar(monkeypatch, tmp_path,
                                                   caplog, romper, motivo):
    """EL RADIO DE DAÑO, medido en las dos direcciones.

    LAS CUATRO FORMAS DE ROMPERLO ENTRAN POR CAMINOS DISTINTOS a propósito: el
    `</nav>` sin cerrar revienta en el parseo, el byte que no es UTF-8 revienta
    en la decodificación (`UnicodeDecodeError`, que NO es un `OSError`) y los
    otros dos en el sistema de archivos. El del byte es el que se escapaba: el
    9-sep-2026 salía de `pantallas()` sin atrapar, se escapaba del
    `except MenuIlegible` de `cerebro/agente.py` y terminaba en «Fallo
    definitivo».

    `_sistema()` se arma en CADA mensaje. Antes de este arreglo un `</nav>` mal
    cerrado lo hacía lanzar, y `cerebro/interpretar.py::_procesar` mandaba la
    fila a `_fallo` → «Fallo definitivo» → repregunta genérica. O sea: un typo
    en el menú del panel y Lucy no contestaba nada, ni «¿qué hora es?».

    Lo que se exige acá:
      · que `_sistema()` devuelva un prompt entero, con las demás herramientas
        intactas — la conversación que no tiene nada que ver con el panel sigue;
      · que NO se calle: sale un ERROR en el log con el motivo;
      · y que lo que se le dice al modelo sea la verdad —que hoy no sabemos qué
        pantallas hay—, sin inventar ninguna ni colar una ruta.
    """
    # El prompt con el menú de verdad, ANTES de romper nada: es contra él que
    # se compara. La lista de herramientas no se teclea acá —se saca del propio
    # prompt sano—, así que el día que se agregue una herramienta esta prueba la
    # exige sola.
    sano = agente._sistema()
    herramientas = {l for l in sano.splitlines() if l.startswith("· ")}
    assert len(herramientas) > 5, f"no reconocí las herramientas: {herramientas}"

    roto = tmp_path / "base.html"
    romper(roto)
    monkeypatch.setattr(menu, "BASE", roto)

    with caplog.at_level("ERROR"):
        prompt = agente._sistema()

    perdidas = herramientas - {l for l in prompt.splitlines()
                               if l.startswith("· ")}
    assert not perdidas, (
        f"el prompt se quedó a medias: un menú roto se llevó puesto {perdidas}. "
        "Una conversación que no tiene nada que ver con el panel tiene que "
        "seguir igual.")

    errores = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errores, ("el menú roto no dejó ni un ERROR en el log: se arregló "
                     "el radio de daño a cambio de que nadie se entere")

    # Y lo que se le dice al modelo es la verdad, no una lista inventada ni una
    # vacía disfrazada de lista. Se mira el texto de reemplazo en sí —no el
    # bloque entero— para que este test hable solo del radio de daño: si el
    # prompt tuviera algo tecleado, eso es asunto de
    # test_ninguna_pantalla_ni_frase_esta_escrita_a_mano_en_el_prompt.
    assert menu.SIN_MENU in _bloque_del_panel(prompt), (
        "sin menú, el bloque del panel no dice que no sabemos qué pantallas "
        "hay; sin eso Lucy puede afirmar que algo está en el panel")
    assert not re.findall(r"(?<![\w/])(/[a-z0-9][a-z0-9-]*)", menu.SIN_MENU), (
        f"el texto de sin-menú se inventó una ruta: {menu.SIN_MENU}")
    assert not _entrecomillado(menu.SIN_MENU), (
        f"el texto de sin-menú se inventó una frase: {menu.SIN_MENU}")


def test_un_fallo_que_no_es_del_menu_no_se_traga(monkeypatch):
    """Al lado indulgente no se llega por olvido.

    El `except` es de `MenuIlegible` y de nada más. Un `except Exception` en el
    armado del prompt dejaría en silencio los errores de verdad — y el prompt
    seguiría saliendo, sin que nadie sepa que le falta algo.
    """
    def _revienta(*a, **k):
        raise ValueError("esto no es un menú ilegible")

    monkeypatch.setattr(menu, "bloque_para_el_prompt", _revienta)
    with pytest.raises(ValueError):
        agente.herramientas_del_prompt()


# ── Quién puede atrapar la excepción del menú, resuelto por IDENTIDAD ────
#
# POR QUÉ NO ALCANZA CON BUSCARLE EL NOMBRE, medido el 9-sep-2026:
#
# Esto miraba si el texto "MenuIlegible" aparecía en el `ast.dump` del tipo del
# `except`. Un segundo sitio que la importara con alias —`from web.menu import
# MenuIlegible as ME` … `except ME:`— se lo saltaba entero: el defecto era
# exactamente el que persigue el resto de este archivo, comprobar por el nombre
# escrito en vez de por lo que la cosa ES.
#
# Acá el módulo y el nombre de la clase se le preguntan al objeto real, y los
# `except` se resuelven contra los imports de cada archivo. Da igual el alias,
# da igual si mañana la clase se llama distinto.
_MOD_OBJETIVO = menu.MenuIlegible.__module__      # derivado, no tecleado
_NOM_OBJETIVO = menu.MenuIlegible.__name__        # derivado, no tecleado


def _archivos_del_repo():
    """Los `.py` que hay en disco. Uno nuevo con el defecto se pone rojo solo."""
    for carpeta, _, archivos in os.walk(RAIZ):
        if ".git" in carpeta.split(os.sep):
            continue
        for archivo in sorted(archivos):
            if archivo.endswith(".py"):
                yield os.path.join(carpeta, archivo)


def _nombres_de_modulo(ruta: str) -> set[str]:
    """Con qué nombres se puede importar este archivo.

    Son dos porque el repo se importa de dos maneras: `cerebro.agente` desde la
    raíz y `test_x` a secas —pytest mete `tests/` en el path—. Con uno solo, un
    re-export entre archivos de pruebas quedaría invisible.
    """
    rel = os.path.relpath(ruta, RAIZ)[: -len(".py")]
    partes = [p for p in rel.split(os.sep) if p != "__init__"]
    return {".".join(partes), partes[-1]} if partes else set()


def _punteado(nodo) -> str | None:
    """`a.b.c` como texto, o None si la expresión no es un nombre punteado.

    None es «no lo sé», y más abajo eso cuenta como rojo. Un `except` cuyo tipo
    sale de una llamada o de un subíndice no se puede resolver leyendo, así que
    no se da por bueno.
    """
    partes = []
    while isinstance(nodo, ast.Attribute):
        partes.append(nodo.attr)
        nodo = nodo.value
    if not isinstance(nodo, ast.Name):
        return None
    partes.append(nodo.id)
    return ".".join(reversed(partes))


def _es_la_clase(nodo, clases: set[str], modulos: dict[str, str],
                 expone: dict[str, set[str]]) -> bool:
    """¿Esta expresión nombra la clase del menú, con el alias que sea?"""
    if isinstance(nodo, ast.Name):
        return nodo.id in clases
    punteado = _punteado(nodo)
    if punteado is None or "." not in punteado:
        return False
    prefijo, _, atributo = punteado.rpartition(".")
    return atributo in expone.get(modulos.get(prefijo, prefijo), set())


def _como_la_llama(arbol, paquete: str, expone: dict[str, set[str]]):
    """(nombres de la clase, alias de módulos) según los imports de ESE archivo.

    Se sobre-aproxima a propósito: se miran también los imports que están dentro
    de una función —`cerebro/agente.py` importa `web.menu` ahí adentro— y eso
    puede exponer de más. De más es el lado seguro; de menos es el agujero.
    """
    clases: set[str] = set()
    modulos: dict[str, str] = {}
    for _ in range(4):          # un alias del alias necesita otra vuelta
        antes = (len(clases), len(modulos))
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                for a in n.names:
                    modulos[a.asname or a.name] = a.name
                    if a.asname is None:
                        modulos[a.name] = a.name   # `import web.menu` → web.menu.X
            elif isinstance(n, ast.ImportFrom):
                origen = n.module or ""
                if n.level:                        # import relativo
                    base = paquete.split(".") if paquete else []
                    base = base[: len(base) - (n.level - 1)]
                    origen = ".".join([p for p in base if p] +
                                      ([origen] if origen else []))
                for a in n.names:
                    local = a.asname or a.name
                    if a.name in expone.get(origen, set()):
                        clases.add(local)
                    modulos[local] = f"{origen}.{a.name}" if origen else a.name
            elif (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and _es_la_clase(n.value, clases, modulos, expone)):
                clases.add(n.targets[0].id)        # `OTRA = ME`
        if (len(clases), len(modulos)) == antes:
            break
    return clases, modulos


def _mapa_del_repo():
    """Cada archivo con su árbol, y qué módulo expone la clase, a punto fijo.

    El punto fijo es lo que le pone FONDO: si `a.py` la re-exporta y `b.py` la
    trae de `a`, `b` queda igual de vigilado que si la importara del original.
    """
    arboles = {}
    for ruta in _archivos_del_repo():
        with open(ruta, encoding="utf-8") as f:
            arboles[ruta] = (ast.parse(f.read(), ruta), _nombres_de_modulo(ruta))

    expone: dict[str, set[str]] = {_MOD_OBJETIVO: {_NOM_OBJETIVO}}
    while True:
        crecio = False
        for ruta, (arbol, nombres) in arboles.items():
            paquete = sorted(nombres, key=len)[-1].rpartition(".")[0]
            clases, _ = _como_la_llama(arbol, paquete, expone)
            for m in nombres:
                if not clases <= expone.get(m, set()):
                    expone.setdefault(m, set()).update(clases)
                    crecio = True
        if not crecio:
            return arboles, expone


def test_un_solo_sitio_se_traga_el_menu_ilegible():
    """¿Quiénes son sus hermanos, y lo cumplen todos?

    Bancarse un menú ilegible está bien en UN sitio —el armado del prompt, que
    corre en cada mensaje— y está mal en cualquier otro: el segundo que lo haga
    va a elegir su propio criterio de qué decirle al modelo, y los dos van a
    separarse. Así que se cuentan, y son uno.

    LA FRONTERA DE ESTA PRUEBA, en una línea para que se pueda predecir sin
    correrla: es culpable todo `except` cuyo tipo RESUELVE a la clase real del
    menú siguiendo los imports del archivo —con el alias que sea, y aunque la
    clase se llame distinto mañana—, y lo que no se pueda resolver, en un
    archivo que la tenga a mano, también.

    Y lo que queda FUERA, dicho para que nadie lo dé por cubierto: quien la
    atrape por herencia (`except Exception`, `except RuntimeError`) no se cuenta
    acá; de eso habla `test_un_fallo_que_no_es_del_menu_no_se_traga`. Y quien se
    la consiga por un import armado al vuelo tampoco: eso ya no se puede leer.

    DE DÓNDE SALEN LAS DOS LISTAS, porque una comprobación vale lo que valga su
    lista: los archivos son los `.py` que hay en disco, no un inventario escrito
    acá; y los nombres de la clase salen del objeto real y de los imports de
    cada archivo, no de un prefijo ni de un texto.
    """
    arboles, expone = _mapa_del_repo()
    culpables, dudosos = set(), []

    for ruta, (arbol, nombres) in arboles.items():
        paquete = sorted(nombres, key=len)[-1].rpartition(".")[0]
        clases, modulos = _como_la_llama(arbol, paquete, expone)
        # ¿Este archivo tiene la clase al alcance de la mano? Si no la tiene, no
        # puede nombrarla, y un `except` raro suyo no habla del menú.
        alcanza = bool(clases) or any(m in expone for m in modulos.values())
        rel = os.path.relpath(ruta, RAIZ)

        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.ExceptHandler) or nodo.type is None:
                continue
            partes = (nodo.type.elts if isinstance(nodo.type, ast.Tuple)
                      else [nodo.type])
            for parte in partes:
                if _es_la_clase(parte, clases, modulos, expone):
                    culpables.add(rel)
                elif _punteado(parte) is None and alcanza:
                    dudosos.append(f"{rel}:{parte.lineno}")

    assert not dudosos, (
        f"hay {len(dudosos)} `except` que no puedo resolver en archivos que "
        f"tienen la excepción del menú a mano: {dudosos}. Lo que no sé leer "
        "cuenta como rojo: escribí el tipo con su nombre y esto se apaga.")

    assert sorted(culpables) == ["cerebro/agente.py"], (
        f"el que se traga un menú ilegible tiene que ser uno solo y ser el "
        f"armado del prompt; hoy son {sorted(culpables)}. Dos sitios que se lo "
        "traguen eligen por su cuenta qué decirle al modelo cuando no hay menú.")


# ── Y que nadie vuelva a medir el menú solo por la costura ───────────────

# Las funciones del menú que aceptan un HTML inyectado, o sea las que tienen
# costura. La lista sale de la FIRMA de cada una, no de un inventario acá: una
# función nueva con `fuente` queda vigilada sin que nadie la agregue.
_CON_COSTURA = {
    nombre
    for nombre, objeto in vars(menu).items()
    if inspect.isfunction(objeto) and objeto.__module__ == menu.__name__
    and "fuente" in inspect.signature(objeto).parameters}


def _archivos_de_prueba():
    """Los archivos de prueba que hay EN DISCO, no un inventario tecleado.

    LA FRONTERA, en una línea para poder predecirla sin correr nada: es un
    archivo de prueba todo `.py` bajo `tests/`, todo `conftest.py` esté donde
    esté, y todo `.py` cuyo nombre empiece por `test_`.

    `conftest.py` va nombrado A PROPÓSITO: no empieza por `test_` y en pytest es
    justo el sitio donde uno pone lo compartido, o sea el primero donde alguien
    escribiría una medición del menú. Un barrido de `test_*.py` lo dejaría
    afuera, que es el agujero que apareció el 5-sep-2026 en dos proyectos.

    El fondo del barrido —no meterse en entornos virtuales ni en cachés— se lo
    pide a `test_buzon_que_no_se_ve._py_en_disco`, que le pregunta a cada
    carpeta qué es (`pyvenv.cfg`, `CACHEDIR.TAG`) en vez de mirarle el nombre.
    Se reusa esa y no se copia el criterio: dos copias de un criterio se
    separan, que es la regla entera de este archivo.
    """
    import test_buzon_que_no_se_ve as barrido

    for ruta in barrido._py_en_disco(Path(RAIZ)):
        rel = ruta.relative_to(RAIZ)
        if (rel.parts[0] == "tests" or ruta.name == "conftest.py"
                or ruta.name.startswith("test_")):
            yield ruta


def _nombres_con_costura_en(arbol) -> set[str]:
    """Con qué nombres puede llamarse en ESTE archivo a una función con costura.

    Se sobre-aproxima a propósito: `menu.pantallas(...)`, `m.pantallas(...)` y
    `pantallas(...)` traído con `from web.menu import pantallas` cuentan todos.
    De más es el lado seguro; de menos es el agujero.
    """
    locales = set(_CON_COSTURA)
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom) and (n.module or "") == menu.__name__:
            for a in n.names:
                if a.name in _CON_COSTURA:
                    locales.add(a.asname or a.name)
    return locales


def test_ninguna_prueba_mide_el_menu_solo_por_la_costura():
    """LO QUE CIERRA LA SERIE: la costura deja de ser una forma de medir.

    `pantallas(fuente)` acepta un HTML inyectado para poder probar sin tocar el
    disco. Está bien que exista. Lo que no está bien es que una prueba escrita
    por ahí DÉ POR CUBIERTO el camino real, que es el único que corre en
    producción (`cerebro/agente.py` llama sin argumento).

    Medido el 9-sep-2026 sobre `2d29bab`, mutando el código real: tres garantías
    de `web/menu.py` rotas SOLO en la rama que lee el archivo daban las tres
    503 passed / 18 passed. Arreglarlo duplicando cada prueba —una por rama— deja
    plantado el caso de mañana, porque la cuarta prueba se escribe por un lado
    solo. Así que hay UNA puerta, `_por_los_dos_caminos`, que corre las dos
    ramas, y acá se exige que sea la única.

    DE DÓNDE SALEN LAS DOS LISTAS: los archivos son los de prueba que hay en
    disco (ver `_archivos_de_prueba`), y las funciones vigiladas salen de la
    FIRMA de las de `web/menu.py` que aceptan `fuente`, no de nombres tecleados.

    LO QUE QUEDA FUERA, dicho para que nadie lo dé por cubierto: quien consiga
    la función por un nombre armado al vuelo —`getattr(menu, "pant" + "allas")`—
    se escapa; eso ya no se puede leer. Es la misma frontera declarada de
    test_un_solo_sitio_se_traga_el_menu_ilegible.
    """
    assert _CON_COSTURA, (
        "no encontré ninguna función del menú con `fuente`: o la costura "
        "desapareció o esta prueba dejó de verla, y entonces pasa siempre")

    puerta = _por_los_dos_caminos.__name__
    por_fuera = []
    mirados = list(_archivos_de_prueba())

    # Una guarda que no mira nada pasa siempre. Que este mismo archivo —el que
    # tiene TODAS las llamadas al menú— esté en la lista es la comprobación más
    # barata de que el barrido llegó a algún lado.
    assert Path(os.path.abspath(__file__)) in mirados, (
        f"el barrido no llegó ni a este archivo: {len(mirados)} mirados. "
        "Una comprobación sobre una lista vacía está verde por no mirar.")

    for ruta in mirados:
        with open(ruta, encoding="utf-8") as f:
            arbol = ast.parse(f.read(), str(ruta))
        nombres = _nombres_con_costura_en(arbol)

        dentro_de_la_puerta = {
            id(x)
            for n in ast.walk(arbol)
            if isinstance(n, ast.FunctionDef) and n.name == puerta
            for x in ast.walk(n)}

        for n in ast.walk(arbol):
            if not isinstance(n, ast.Call) or id(n) in dentro_de_la_puerta:
                continue
            llamada = (n.func.attr if isinstance(n.func, ast.Attribute)
                       else n.func.id if isinstance(n.func, ast.Name) else None)
            if llamada not in nombres:
                continue
            if n.args or any(k.arg == "fuente" for k in n.keywords):
                rel = os.path.relpath(ruta, RAIZ)
                por_fuera.append(f"{rel}:{n.lineno} → {llamada}(…)")

    assert not por_fuera, (
        f"{len(por_fuera)} mediciones del menú por la costura sin pasar por "
        f"{puerta}(): {por_fuera}. Esa rama NO es la que corre en producción: "
        "una garantía probada solo ahí puede estar rota en el camino real y "
        f"toda la suite en verde. Cambia la llamada por {puerta}(html) y la "
        "misma prueba cubre las dos ramas.")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
