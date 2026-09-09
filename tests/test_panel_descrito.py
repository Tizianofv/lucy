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

Herméticos: se stubea psycopg antes de importar, igual que en
test_esquema_del_modelo.py. No tocan ninguna base y no llaman a ningún modelo.

Correr:  python3 -m pytest tests/test_panel_descrito.py -q
"""
from __future__ import annotations

import ast
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
    bloque = menu.bloque_para_el_prompt(fabricado)
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
        menu.pantallas("<html><body>ni un nav</body></html>")
    with pytest.raises(menu.MenuIlegible):
        menu.pantallas("<nav><span>texto suelto</span></nav>")
    with pytest.raises(menu.MenuIlegible):
        menu.pantallas(f'<nav>{sana}<a href="/x" data-tambien="lo de x">'
                       '</a></nav>')
    with pytest.raises(menu.MenuIlegible):
        menu.pantallas(f'<nav>{sana}<a href="/x">Equis</a></nav>')
    with pytest.raises(menu.MenuIlegible):
        menu.pantallas(f'<nav>{sana}<a href="/x" data-tambien="lo de x"'
                       ' data-nota="dice &quot;hola&quot;">Equis</a></nav>')


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
    pantallas = menu.pantallas(con_externo)
    assert [p.ruta for p in pantallas] == ["/"], (
        f"la frontera no es la que dice el archivo: {pantallas}")


def test_un_menu_roto_no_deja_a_lucy_sin_contestar(monkeypatch, tmp_path,
                                                   caplog):
    """EL RADIO DE DAÑO, medido en las dos direcciones.

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
    roto.write_text("<header><nav><a href='/'>Resumen</a></header>",
                    encoding="utf-8")
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


def test_un_solo_sitio_se_traga_el_menu_ilegible():
    """¿Quiénes son sus hermanos, y lo cumplen todos?

    Bancarse un menú ilegible está bien en UN sitio —el armado del prompt, que
    corre en cada mensaje— y está mal en cualquier otro: el segundo que lo haga
    va a elegir su propio criterio de qué decirle al modelo, y los dos van a
    separarse. Así que se cuentan, y son uno.

    Los archivos se sacan del disco, no de una lista escrita acá: uno nuevo con
    el defecto se pone rojo solo. Y se leen con `ast`, no buscándoles texto: un
    `except` no se reconoce por cómo está escrito.
    """
    culpables = []
    for carpeta, _, archivos in os.walk(RAIZ):
        if ".git" in carpeta.split(os.sep):
            continue
        for archivo in archivos:
            if not archivo.endswith(".py"):
                continue
            ruta = os.path.join(carpeta, archivo)
            arbol = ast.parse(open(ruta, encoding="utf-8").read(), ruta)
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.ExceptHandler) or nodo.type is None:
                    continue
                if "MenuIlegible" in ast.dump(nodo.type):
                    culpables.append(os.path.relpath(ruta, RAIZ))

    assert culpables == ["cerebro/agente.py"], (
        f"el que se traga un menú ilegible tiene que ser uno solo y ser el "
        f"armado del prompt; hoy son {culpables}. Dos sitios que se lo traguen "
        "eligen por su cuenta qué decirle al modelo cuando no hay menú.")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
