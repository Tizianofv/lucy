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

Y LA REGLA DE LA QUE SALE ESA FRONTERA, agregada el 9-sep-2026 después de que
el mismo defecto apareciera dos veces más: LO QUE NO SE PUEDE LEER ES
MenuIlegible, NO UNA LISTA DE MOTIVOS.

    · leyendo el ARCHIVO: no hay lista de excepciones. La frontera es el
      alcance del `try`, y adentro está solo la lectura, así que cualquier
      fallo de ahí —`OSError`, `UnicodeDecodeError`, o el que inventen
      mañana— es «no pude leer el menú» por construcción.
    · leyendo un ATRIBUTO: no hay lista de formas de escribirlo. Se leen las
      tres válidas de HTML, y un atributo que está y no se entiende revienta
      en vez de saltarse — los tres atributos por la misma puerta (`_leer`).

Las dos veces el defecto tuvo la misma forma —el criterio bueno en un tramo y
no en su hermano— y las dos veces el arreglo fue el mismo: que lo que no se
sabe clasificar caiga del lado seguro SOLO, sin que nadie lo agregue a una
lista.

Y LA TERCERA VEZ, LA NOCHE DEL 9-sep-2026, LOS DOS TRAMOS ERAN LAS DOS RAMAS
DE `pantallas()`: la costura que existe para poder probar, y el camino real.

`pantallas(fuente)` acepta el HTML ya leído para probar con un menú fabricado.
Está bien que exista. Lo que estaba mal es que las pruebas de las garantías de
arriba entraban TODAS por ahí, y producción entra siempre por la otra rama
—`cerebro/agente.py` llama `bloque_para_el_prompt()` sin argumento—. Medido
sobre `2d29bab`, rompiendo cada garantía SOLO en la rama que lee el archivo:

    · el parseo del <nav> metido dentro del `try`   → 503 passed, todo verde
    · un atributo saltándose la puerta `_leer`      → 503 passed, todo verde
    · los <a> sin data-tambien borrados en silencio → 503 passed, todo verde

Ninguna era un defecto vivo; las tres eran un cambio futuro razonable a un paso
de serlo. El arreglo no es duplicar pruebas —una por rama— sino que la costura
deje de ser una forma de medir: `tests/test_panel_descrito.py` corre todo menú
fabricado por LAS DOS ramas (`_por_los_dos_caminos`) y se pone rojo si alguien
vuelve a llamar a `pantallas(html)` por fuera. Si se toca este archivo, la
garantía que se agregue se prueba por ahí, no llamando con `fuente`.

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


# Los tres modos válidos de escribir un valor en HTML: entre comillas dobles,
# entre simples, y suelto hasta el primer espacio. Se reconocen los tres porque
# un atributo es lo que es —no cómo lo escribió quien editó la plantilla—, y
# porque el editor de cualquiera puede cambiar unas por otras sin avisar.
_VALOR = r'"([^"]*)"|\'([^\']*)\'|([^\s>]+)'

# «El atributo está y no se pudo leer» es distinto de «no está»: lo primero es
# MenuIlegible, lo segundo puede ser la exención declarada. Sin un valor propio
# para el primer caso, los dos se confunden en None y el ilegible se salta en
# silencio, que es justo la forma de fallar que este archivo cierra.
ILEGIBLE = object()


def _atributo(atributos: str, nombre: str) -> str | None | object:
    """El valor de un atributo del `<a>`: None si no está, ILEGIBLE si no se lee.

    Se busca por nombre y no por posición: el orden de los atributos en el HTML
    no es asunto de nadie, y una guarda que dependa de él se rompe el día que
    alguien reordene la línea.

    POR QUÉ MIRA LAS TRES FORMAS DE ESCRIBIR UN VALOR, medido el 9-sep-2026:

    Acá decía `nombre="([^"]*)"` —solo comillas dobles—. Con `href='/tareas'`,
    que es HTML perfectamente válido, `href` salía None, la ruta salía "", no
    empezaba por "/" y la pantalla SE SALTABA EN SILENCIO: desaparecía del
    prompt sin un rojo en ninguna parte. Medido sobre el mismo menú, cambiando
    solo las comillas del href:

        href="/tareas"   → ['/', '/tareas']
        href='/tareas'   → ['/']          ← la pantalla se evaporó
        data-tambien='…' → MenuIlegible   ← el hermano sí caía del lado seguro

    O sea: el mismo criterio aplicado a un atributo y no a su hermano, y el que
    se lo saltaba era justo el que decide si algo es una pantalla. Reconocer un
    atributo por CÓMO ESTÁ ESCRITO es la misma especie de defecto que este
    archivo persigue en el prompt.
    """
    limite = rf'(?<![-\w]){re.escape(nombre)}(?![-\w])'
    m = re.search(rf"{limite}\s*=\s*(?:{_VALOR})", atributos, re.I)
    if m is not None:
        return next(g for g in m.groups() if g is not None)
    # Está escrito pero sin un valor que se pueda leer. No se adivina y no se
    # salta: quien llama decide, y hoy eso es MenuIlegible.
    return ILEGIBLE if re.search(limite, atributos, re.I) else None


class MenuIlegible(RuntimeError):
    """No se pudo sacar del menú la lista de pantallas, y no se inventa.

    Es `RuntimeError` para que quien ya la esperaba la siga viendo, y es un tipo
    PROPIO para que el que decida seguir adelante pueda tragarse exactamente
    esto y nada más. Un `except Exception` alrededor del armado del prompt se
    tragaría también los errores de verdad y los dejaría en silencio.
    """


def _leer(atributos: str, nombre: str) -> str | None:
    """UNA sola puerta para los tres atributos del enlace.

    Los tres —`href`, `data-tambien`, `data-nota`— pasan por acá, así que
    ninguno puede tener su propio criterio para un atributo escrito de una
    forma que no se entiende. Ése fue el defecto del 9-sep-2026: `data-tambien`
    ilegible reventaba y `href` ilegible se saltaba en silencio.
    """
    valor = _atributo(atributos, nombre)
    if valor is ILEGIBLE:
        raise MenuIlegible(
            f"{BASE}: el enlace <a {' '.join(atributos.split())}> declara "
            f"{nombre} y no le puedo leer el valor. Un atributo que está y no "
            "se entiende no se adivina ni se salta: saltarlo en silencio es "
            "exactamente cómo desaparece una pantalla sin que nadie se entere.")
    return valor


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
        # POR QUÉ ESTE `except` NO ES UNA LISTA DE TIPOS, medido el 9-sep-2026:
        #
        # Acá decía `except OSError`, y `UnicodeDecodeError` NO es un OSError
        # (es ValueError). O sea que un `base.html` guardado en otra
        # codificación —un pegado desde Windows-1252, un merge mal resuelto—
        # se escapaba de acá, se escapaba del `except MenuIlegible` de
        # `cerebro/agente.py`, y terminaba en «Fallo definitivo»: Lucy sin
        # contestar NADA, ni la hora. Exactamente el fallo que este archivo
        # existe para cerrar, entrando por otra puerta.
        #
        # Agregarle `UnicodeDecodeError` a la lista tapa el de hoy y deja
        # plantado el de mañana, que es la misma forma de fallar que persigue
        # todo lo demás de este archivo. Así que la frontera NO es el tipo:
        # es el ALCANCE del `try`, y adentro hay UNA sola operación —leer el
        # archivo del menú—. Todo lo que salga de ahí es, por construcción, «no
        # pude leer el menú», se llame como se llame y lo invente quien lo
        # invente. Un tipo nuevo cae del lado seguro sin que nadie lo agregue a
        # ninguna parte.
        #
        # Y el otro extremo queda cerrado por el mismo alcance: el parseo de
        # abajo está FUERA del `try`, así que un error de verdad ahí sigue
        # saliendo entero. `BaseException` también queda afuera a propósito —un
        # Ctrl-C no es un menú ilegible—.
        try:
            html = BASE.read_text(encoding="utf-8")
        except Exception as e:
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
            ruta = _leer(atributos, "href") or ""
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

            crudo = _leer(atributos, "data-tambien")
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

            nota = (_leer(atributos, "data-nota") or "").strip()
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
