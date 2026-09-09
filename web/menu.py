"""Las pantallas del panel, tal como las declara el menú de verdad.

POR QUÉ EXISTE, medido el 9-sep-2026 sobre 399fe2d:

Se publicó `/tareas` —el panel dejó de ser solo de plata— y la descripción de
la herramienta `panel` que lee Lucy siguió diciendo «gastos por mes, lo que
falta clasificar y el detalle». Nada se puso rojo. Consecuencia: quien pedía
«los pendientes» o «las tareas» no tenía por qué recibir el enlace, porque para
el modelo ese panel era de plata.

Arreglarlo escribiendo «Tareas» en el prompt tapa el caso de hoy y deja
plantado el de mañana: una lista tecleada y el menú empiezan a separarse el
mismo día que se escriben. Así que acá NO se teclea ninguna lista. Se lee del
único sitio donde las pantallas existen de verdad —el `<nav>` de
`web/plantillas/base.html`, que es exactamente lo que ve quien abre el panel—.
Una pantalla nueva en el menú llega sola al prompt, sin que nadie se acuerde
de nada, y una que se quita deja de mencionarse igual de sola.

Es el mismo principio que las categorías (`CATEGORIAS` inyectadas en
`cerebro/agente.py`, no copiadas al texto): una sola fuente, y el prompt se
arma desde ella.

LO QUE SE AGREGÓ EL 9-sep-2026 POR LA TARDE, y el motivo, medido:

La lista de pantallas se derivaba, pero LAS PALABRAS CON LAS QUE SE PIDEN
seguían tecleadas en el prompt —«los pendientes», «las tareas», «qué hay que
hacer»— junto con una frase suelta sobre la pantalla de tareas. Medido sobre
`ca2c421`, sacando `/tareas` del `<nav>` y sin tocar nada más:

    · la lista derivada perdió Tareas, correctamente
    · el prompt siguió diciendo «los pendientes», «las tareas», «qué hay que
      hacer» y «la pantalla de tareas es de las DOS personas de la casa»
    · tests/test_panel_descrito.py: 7 passed

O sea: Lucy seguía invitando a pedir una pantalla que el panel ya no tenía —
manda el enlace, la persona entra, y no está—. Una lista tecleada al lado de
una derivada: el mismo defecto, un centímetro más abajo.

El arreglo NO es enumerar más frases en ninguna parte. Las frases dejan de ser
una lista aparte y pasan a ser PARTE DE LA PANTALLA: viajan en el propio `<a>`
del menú, en `data-tambien`. Si la pantalla se va del menú, sus frases se van
con ella, porque son la misma cosa. No hay nada que mantener sincronizado.

Y para que una pantalla nueva no pueda nacer muda, `data-tambien` es
OBLIGATORIO en cada enlace del menú: quien agrega una pantalla tiene que decir
en ese momento cómo se la va a pedir. Al lado indulgente no se llega por olvido.

LA FRONTERA DE ESTE ARCHIVO, dicha en una línea para que se pueda predecir sin
probar: es una pantalla del panel todo `<a>` dentro de un `<nav>` cuyo `href`
empieza por `/`. Lo que apunta afuera (`http…`, `mailto:`, un ancla) no es una
pantalla y no se mira. Todo lo demás que esté dentro de esa frontera y no se
pueda leer entero —sin nombre visible, sin `data-tambien`, con una nota que
lleve comillas— es MenuIlegible, no un caso que se salta.

Lo que NO se hace acá: adivinar. Un fallback silencioso devolvería una lista
vacía, el prompt quedaría sin pantallas y todo seguiría en verde —que es justo
la forma de fallar que este archivo viene a cerrar—. Quién se banca el
MenuIlegible y qué hace con él es decisión de quien llama; acá se revienta con
el motivo. El único sitio que se lo traga hoy es `cerebro/agente.py`, y hay una
prueba que se pone roja si mañana son dos
(`tests/test_panel_descrito.py::test_un_solo_sitio_se_traga_el_menu_ilegible`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

# Independiente del directorio de trabajo a propósito: `web/app.py` monta las
# plantillas con una ruta relativa ("web/plantillas") y eso solo funciona si el
# proceso arranca en la raíz. El prompt se arma también desde las pruebas y
# desde tools/, así que acá se ancla al propio archivo.
BASE = Path(__file__).resolve().parent / "plantillas" / "base.html"

_NAV = re.compile(r"<nav\b[^>]*>(.*?)</nav>", re.S | re.I)
_ENLACE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_MARCAS = re.compile(r"<[^>]+>")


def _atributo(atributos: str, nombre: str) -> str | None:
    """El valor de un atributo del `<a>`, o None si no está.

    Se busca por nombre y no por posición: el orden de los atributos en el HTML
    no es asunto de nadie, y una guarda que dependa de él se rompe el día que
    alguien reordene la línea.
    """
    m = re.search(rf'\b{re.escape(nombre)}="([^"]*)"', atributos, re.I)
    return None if m is None else m.group(1)


class MenuIlegible(RuntimeError):
    """No se pudo sacar del menú la lista de pantallas, y no se inventa.

    Es `RuntimeError` para que quien ya la esperaba la siga viendo, y es un tipo
    PROPIO para que el que decida seguir adelante pueda tragarse exactamente
    esto y nada más. Un `except Exception` alrededor del armado del prompt se
    tragaría también los errores de verdad y los dejaría en silencio.
    """


class Pantalla(NamedTuple):
    """Una pantalla del panel, con todo lo que el menú declara de ella."""

    nombre: str          # lo que se lee en el menú: "Tareas"
    ruta: str            # el href: "/tareas"
    frases: tuple[str, ...]   # cómo la pide una persona: "los pendientes", …
    nota: str            # lo que hay que saber de ella, o "" si no hay nada


# Lo que se le dice al modelo cuando el menú no se pudo leer. No nombra ninguna
# pantalla y no lleva ni una ruta ni una comilla a propósito: es lo único que se
# puede afirmar sin haber leído el menú, y así no ensucia las dos guardas que
# comparan el bloque del panel contra el menú.
SIN_MENU = (
    "   · (hoy no pude leer el menú del panel, así que NO sé qué pantallas\n"
    "     tiene. El enlace sigue sirviendo igual: si lo piden, mandalo. Lo\n"
    "     único que no podés hacer es decir que algo está —o que no está— en\n"
    "     el panel, porque eso ahora mismo no lo sabés.)")


def pantallas(fuente: str | None = None) -> list[Pantalla]:
    """Las pantallas del panel, en el mismo orden en que salen en el menú.

    `fuente` es el HTML ya leído; sirve para probar con un menú fabricado sin
    tocar la plantilla real. Sin argumento lee `web/plantillas/base.html`.
    """
    if fuente is None:
        try:
            html = BASE.read_text(encoding="utf-8")
        except OSError as e:
            raise MenuIlegible(
                f"{BASE}: no pude leer el archivo del menú "
                f"({type(e).__name__}: {e}). Sin él no sé qué pantallas tiene "
                "el panel y no me invento una lista.") from e
    else:
        html = fuente

    bloques = _NAV.findall(html)
    if not bloques:
        raise MenuIlegible(
            f"{BASE}: no encontré ningún <nav>. El menú es la lista de "
            "pantallas del panel; sin él no sé qué tiene el panel y no me "
            "invento una lista.")

    salida: list[Pantalla] = []
    for bloque in bloques:
        for atributos, texto in _ENLACE.findall(bloque):
            ruta = _atributo(atributos, "href") or ""
            # La frontera, y es la única exención que hay: un enlace del menú
            # que no apunta a una ruta del propio panel no es una pantalla del
            # panel. Se puede decir en una línea y se puede predecir sin correr
            # nada, que es lo que se le pide a una exención.
            if not ruta.startswith("/"):
                continue

            nombre = " ".join(_MARCAS.sub(" ", texto).split())
            if not nombre:
                raise MenuIlegible(
                    f"{BASE}: el enlace a {ruta} no tiene texto visible. Sin "
                    "nombre no se lo puedo nombrar al modelo, y saltármelo en "
                    "silencio es justo cómo desaparece una pantalla sin que "
                    "nadie se entere.")

            crudo = _atributo(atributos, "data-tambien")
            frases = tuple(f for f in (t.strip() for t in (crudo or "").split(","))
                           if f)
            if not frases:
                raise MenuIlegible(
                    f'{BASE}: el enlace a {ruta} ({nombre}) no declara '
                    'data-tambien. Ahí van las palabras con las que una '
                    'persona pide esta pantalla —"los pendientes" no contiene '
                    '"Tareas"—, y sin ellas Lucy no manda el enlace a quien la '
                    "pide por su nombre de todos los días. Es obligatorio: una "
                    "pantalla nueva no puede nacer muda.")

            nota = (_atributo(atributos, "data-nota") or "").strip()
            if '"' in nota or "&quot;" in nota:
                raise MenuIlegible(
                    f"{BASE}: la data-nota de {ruta} lleva comillas. Las "
                    "comillas del bloque del panel se comparan una a una "
                    "contra las frases del menú, así que una nota entrecomillada "
                    "rompería esa comparación. Decila sin comillas.")

            salida.append(Pantalla(nombre, ruta, frases, nota))

    if not salida:
        raise MenuIlegible(
            f"{BASE}: el <nav> no tiene ningún enlace a una ruta del panel. "
            "Devolver una lista vacía dejaría el prompt sin pantallas y todo "
            "en verde.")
    return salida


def frases_declaradas(fuente: str | None = None) -> set[str]:
    """Todas las formas de pedir una pantalla que el menú declara HOY.

    Es la lista contra la que se comparan las comillas del bloque del panel:
    lo que el prompt le pide al modelo que escuche tiene que salir de acá y de
    ningún otro sitio.
    """
    return {frase for p in pantallas(fuente) for frase in p.frases}


def bloque_para_el_prompt(fuente: str | None = None) -> str:
    """Las pantallas como se las decimos al modelo, una por línea.

    Cada línea lleva TODO lo que el modelo necesita saber de esa pantalla —el
    nombre, la ruta, cómo se la piden y la nota si la hay—, para que el texto
    del prompt no tenga que decir nada de ninguna pantalla en particular. Ése es
    el punto: en `cerebro/agente.py` no queda ni un nombre ni una frase tecleada
    que pueda sobrevivir a la pantalla que la justificaba.

    El sangrado (tres espacios y `·`) es el de las demás sublistas de
    `HERRAMIENTAS` en `cerebro/agente.py`.
    """
    lineas = []
    for p in pantallas(fuente):
        pedidos = ", ".join(f'"{f}"' for f in p.frases)
        lineas.append(f"   · {p.nombre}  ({p.ruta}) — te la piden diciendo: "
                      f"{pedidos}")
        if p.nota:
            lineas.append(f"     Ojo: {p.nota}")
    return "\n".join(lineas)
