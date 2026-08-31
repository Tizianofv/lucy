"""Tests de la categorización que aprende (cerebro/bancos/categorias.py).

Lo que decide si esto sirve o no es LA NORMALIZACIÓN. Si dos formas de escribir
el mismo comercio caen en claves distintas, hay que corregir el mismo sitio
veinte veces — y ahí es cuando la gente deja de corregir y el sistema se queda
sin aprender nada.

Los comercios de estos tests son literales de los 963 correos reales.

Correr:  python3 tests/test_categorias.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cerebro.bancos.categorias import (  # noqa: E402
    CATEGORIAS, CLAVES,
    Categorizador,
    normalizar_comercio,
)


# ── Normalización: las variantes reales del mismo sitio ──────────────────

def test_las_variantes_del_supermercado_caen_en_la_misma_clave():
    """Los tres primeros son literales del corpus."""
    base = normalizar_comercio("SM NACIONAL MAXIMO GOM")
    for v in ("SM NACIONAL MAXIMO GOM SANTO DOMINGODO",
              "SM NACIONAL MAXIMO GOM  ",
              "sm nacional maximo gom SDQ"):
        assert normalizar_comercio(v) == base, f"{v!r} → {normalizar_comercio(v)!r}"


def test_quita_sucursal_y_prefijo_de_red():
    assert normalizar_comercio("SUPERMERCADO NACIONAL #12 SDQ") == \
           normalizar_comercio("SUPERMERCADO NACIONAL")
    # "*BNS CCN MAXIMO GOMEZ DIS" es literal de un correo de Banesco.
    assert normalizar_comercio("*BNS CCN MAXIMO GOMEZ DIS").startswith("MAXIMO")


def test_no_destruye_nombres_cortos():
    """Normalizar de más es tan malo como no normalizar: si dos comercios
    distintos caen en la misma clave, uno queda mal categorizado para siempre."""
    assert normalizar_comercio("APPLE.COM/BILL") != normalizar_comercio("NETFLIX.COM")
    assert normalizar_comercio("CLUB NACO") != normalizar_comercio("DRINK 2 GO")
    assert normalizar_comercio("APPLE.COM/BILL")


def test_comercio_vacio_no_produce_clave():
    for x in ("", "   ", None, "###"):
        assert not normalizar_comercio(x)


# ── La decisión ──────────────────────────────────────────────────────────

def test_lo_aprendido_gana_a_la_palabra_clave():
    """Una corrección explícita vale más que una regla general: es la única
    señal que viene de alguien que sabe qué es ese comercio."""
    c = Categorizador(aprendidas={"CLUB NACO CABAMAR": "Restaurantes"},
                      claves={"CLUB": "Ocio"})
    assert c.categoria_de("CLUB NACO CABAMAR GUAYACANES DO") == "Restaurantes"


def test_la_palabra_clave_es_la_red_de_seguridad():
    c = Categorizador(claves={"SM NACIONAL": "Supermercado"})
    assert c.categoria_de("SM NACIONAL MAXIMO GOM SDQ") == "Supermercado"


def test_la_clave_mas_larga_gana():
    """"SUPER" casaría con "SUPERCASHBACK" y con media ciudad. Ordenar por
    longitud evita tener que pensar en el orden al escribir las reglas."""
    c = Categorizador(claves={"SUPER": "Otros",
                              "SUPERMERCADO NACIONAL": "Supermercado"})
    assert c.categoria_de("SUPERMERCADO NACIONAL #7") == "Supermercado"


def test_lo_desconocido_no_se_inventa():
    """Sin categoría va a la cola del panel, que es donde se convierte en una
    corrección. Adivinar mal es peor que dejarlo en blanco: una categoría
    equivocada nadie la revisa."""
    assert Categorizador().categoria_de("FERRETERIA DON JOSE") is None


def test_corregir_una_variante_clasifica_a_las_demas():
    """El corazón de esto: se aprende del comercio normalizado, así que una
    corrección cubre todas las formas en que el banco escribe ese sitio."""
    c = Categorizador()
    c.aprender("SM NACIONAL MAXIMO GOM SANTO DOMINGODO", "Supermercado")
    assert c.categoria_de("SM NACIONAL MAXIMO GOM") == "Supermercado"
    assert c.categoria_de("sm nacional maximo gom SDQ") == "Supermercado"


# ── Cuánto cubriría sobre los comercios reales ───────────────────────────

def test_cuantos_comercios_distintos_hay_de_verdad():
    """La pregunta que decide si esto converge: ¿cuántas correcciones harían
    falta para cubrir el gasto? Si cada compra fuera un comercio nuevo, no
    convergería nunca."""
    import email
    import pathlib
    from collections import Counter
    from datetime import datetime
    from email.header import decode_header
    fx = pathlib.Path(__file__).parent / "fixtures"
    if not fx.exists():
        print("     (sin fixtures — capa saltada)")
        return
    import cerebro.bancos as B

    def cab(v):
        if not v:
            return ""
        return "".join(p.decode(e or "utf-8", "replace") if isinstance(p, bytes)
                       else p for p, e in decode_header(v))
    crudos, normalizados, todos = Counter(), Counter(), []
    for f in sorted(fx.rglob("*.eml")):
        m = email.message_from_bytes(f.read_bytes())
        frm = cab(m.get("From"))
        addr = frm.split("<")[-1].strip("> ").lower() if "<" in frm else frm.lower()
        fn = B.buscar_parser(addr, cab(m.get("Subject")).strip())
        if fn is None:
            continue
        pl = ht = ""
        for p in (m.walk() if m.is_multipart() else [m]):
            if p.get_content_maintype() != "text":
                continue
            d = (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/plain" and not pl:
                pl = d
            elif p.get_content_type() == "text/html" and not ht:
                ht = d
        try:
            movs = fn(B.CorreoCrudo(remitente=addr, asunto=cab(m.get("Subject")),
                                    fecha_correo=datetime(2026, 1, 1), html=ht,
                                    texto=pl, cuenta="x", uid=f.stem))
        except Exception:
            continue
        for mv in movs:
            if mv.canal != "tarjeta":
                continue
            crudos[mv.contraparte] += 1
            normalizados[normalizar_comercio(mv.contraparte)] += 1
            todos.append(mv.contraparte)

    ahorro = len(crudos) - len(normalizados)
    cubre_10 = sum(n for _, n in normalizados.most_common(10))
    print(f"     ({sum(crudos.values())} consumos · {len(crudos)} comercios crudos "
          f"→ {len(normalizados)} normalizados · los 10 más frecuentes cubren "
          f"{cubre_10} consumos)")
    assert ahorro > 0, "la normalización no unificó ni una variante"
    assert len(normalizados) < sum(crudos.values()) / 2, (
        "hay casi tantos comercios como compras: no convergería nunca")

    # Cuántas correcciones harían falta de verdad, usando la búsqueda por
    # prefijo. Es la cifra que decide si esto sirve, y va con cota para que no
    # se degrade en silencio si alguien "mejora" la normalización.
    c = Categorizador()
    faltan = [x for x in todos]
    correcciones = 0
    while faltan and correcciones < 20:
        pend = Counter(normalizar_comercio(x) for x in faltan)
        c.aprender(pend.most_common(1)[0][0], "X")
        correcciones += 1
        faltan = [x for x in faltan if c.categoria_de(x) is None]
    cubierto = len(todos) - len(faltan)
    pct = 100 * cubierto // len(todos)
    print(f"     (20 correcciones cubrirían {cubierto}/{len(todos)} consumos = {pct}%)")
    assert pct >= 35, (
        f"20 correcciones solo cubren {pct}%: la normalización empeoró y "
        "corregir a mano dejó de valer la pena")


# ── El vocabulario y la red de palabras clave ────────────────────────────

def test_la_clave_casa_en_inicio_de_palabra_y_no_en_cualquier_parte():
    """La trampa que costó reescribir el casamiento: con subcadena cruda,
    "UBER" casa dentro de "TUBERIA" y una compra de plomería queda contada como
    transporte. Nadie lo nota, porque un movimiento MAL categorizado no cae en
    ninguna cola: se va derecho al total."""
    cat = Categorizador(claves=CLAVES)
    # "TUBERIAS Y CONEXIONES" ahora SÍ tiene categoría, y está bien: desde que
    # existe "Reparaciones del hogar", "TUBERIA" es una clave legítima que casa
    # al principio de la palabra. Lo que este test vigila no es que no tenga
    # categoría, es que no tenga LA EQUIVOCADA: con subcadena cruda, "UBER"
    # casaba dentro de "TUBERIA" y una compra de plomería se contaba como
    # transporte. Afirmar `is None` confundía el síntoma con el fallo, y al
    # agregar una clave buena el test se puso rojo sin que nada se rompiera.
    assert cat.categoria_de("TUBERIAS Y CONEXIONES") != "Transporte", (
        "'UBER' volvió a casar por dentro de 'TUBERIA'")
    for texto in ("COLEGIO SANTA ANA", "CARBONELL SRL", "ABONOS DEL CARIBE"):
        assert cat.categoria_de(texto) is None, (
            f"{texto!r} casó con una clave por adentro de una palabra")


def test_la_clave_si_deja_pasar_el_plural_y_el_sufijo():
    """Anclar al inicio de palabra no puede volverse coincidencia exacta: los
    bancos escriben el mismo sitio de varias formas, y "SUPERMERCADO" tiene que
    seguir cubriendo "SUPERMERCADOS BRAVO"."""
    cat = Categorizador(claves=CLAVES)
    assert cat.categoria_de("SUPERMERCADOS BRAVO") == "Supermercado"
    assert cat.categoria_de("SUPERMERCADO NACIONAL #12 SDQ") == "Supermercado"


def test_ninguna_clave_apunta_a_una_categoria_inventada():
    """Una clave que devuelve una categoría fuera de la lista mete un valor que
    el desplegable no ofrece: el movimiento queda con una categoría que nadie
    puede volver a elegir, y el filtro del panel nunca la encuentra."""
    fuera = sorted({v for v in CLAVES.values() if v not in CATEGORIAS})
    assert not fuera, f"categorías fuera del vocabulario: {fuera}"


def test_el_vocabulario_no_tiene_duplicados_ni_variantes_de_caja():
    bajas = [c.lower() for c in CATEGORIAS]
    assert len(bajas) == len(set(bajas)), "hay categorías repetidas"


def test_la_normalizacion_no_se_come_el_final_de_la_palabra():
    """_COLAS quita el sufijo de ciudad —"SDQ", "SANTO DOMINGO", "RD"— pero sin
    frontera de palabra se comía las últimas letras de cualquier cosa terminada
    en DO, RD o US: "SUPERMERCADO" quedaba en "SUPERMERCA" y "BONUS" en "BON".
    Y como las CLAVES también se normalizan, eso convertía claves largas y
    seguras en muñones de tres letras — justo lo que el comentario prohíbe."""
    for entero in ("SUPERMERCADO", "BONUS", "PESCADO", "HELADO", "ALTUS"):
        assert normalizar_comercio(entero) == entero, (
            f"la normalización mutiló {entero!r} → {normalizar_comercio(entero)!r}")
    # Y sigue quitando lo que sí es cola de ciudad:
    assert normalizar_comercio("SM NACIONAL MAXIMO GOM SANTO DOMINGODO") == \
        "SM NACIONAL MAXIMO GOM"


def test_ningun_nombre_de_pila_hace_de_clave_de_comercio():
    """"WENDY" capturaba 8 movimientos del corpus y 7 eran transferencias a una
    señora que se llama Wendy: 87% de error. Un nombre de pila no identifica un
    comercio, y peor, se come justo las transferencias a personas que el módulo
    dice mandar a la cola a propósito."""
    cat = Categorizador(claves=CLAVES)
    assert cat.categoria_de(
        "ROSILIS YANELY ROMERO JIMENEZ → WENDY MARISOL CANELA CRUZ") is None
    assert cat.categoria_de("WENDY'S TIRADENTES") == "Restaurantes"


def test_mencionar_un_banco_no_es_una_comision_bancaria():
    """"BANCO" capturaba 10 movimientos y significaba "el texto nombra un
    banco", no "esto es un cargo del banco": convertía transferencias entre
    personas en gasto bancario. La clave tiene que nombrar el CARGO."""
    cat = Categorizador(claves=CLAVES)
    for texto in ("BANCO RESERVAS R.D 010",
                  "ROSILIS YANELY ROMERO JIMENEZ · BANCO MULTIPLE BHD S.A.",
                  "BANCO POPULAR DOMINICANO, C. POR A."):
        assert cat.categoria_de(texto) is None, f"{texto!r} → gasto bancario"
    assert cat.categoria_de("COMISION POR MANEJO") == "Banco y comisiones"


def test_la_comida_a_domicilio_no_es_transporte():
    cat = Categorizador(claves=CLAVES)
    assert cat.categoria_de("UBER * EATS PENDING") == "Restaurantes"
    assert cat.categoria_de("UBER*RIDES") == "Transporte"


def test_la_marca_no_suma_nunca_se_aprende_del_comercio():
    """Costó caro descubrirlo. "No suma" no dice nada del COMERCIO: dice algo de
    ese movimiento —que ese dinero solo pasaba por la cuenta—. Aprenderlo del
    comercio es una generalización falsa.

    El caso real: los dos pagos de EDESUR del 4-ago son idénticos salvo el
    monto; uno es la luz de esta casa y el otro la del papá de Rosi. Marcar el
    segundo enseñó `EDESUR PAGA TODO ONLINE → No suma`, así que a partir de ahí
    TODOS los pagos de EDESUR —el propio incluido— habrían entrado marcados y
    desaparecido de los totales sin que nadie lo viera.
    """
    from cerebro.bancos.categorias import NO_SUMAN, se_aprende
    for marca in NO_SUMAN:
        assert not se_aprende(marca), f"{marca!r} se estaría aprendiendo"
    # Un rubro sí: "SM NACIONAL es Supermercado" vale para siempre.
    assert se_aprende("Supermercado")
    assert not se_aprende("") and not se_aprende(None)


if __name__ == "__main__":
    fallidos = 0
    for nombre, fn in sorted(globals().items()):
        if not nombre.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {nombre}")
        except AssertionError as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {e or 'assert falló'}")
        except Exception as e:
            fallidos += 1
            print(f"  ✗ {nombre}  — {type(e).__name__}: {e}")
    print(f"\n{'FALLARON ' + str(fallidos) if fallidos else 'Todo verde'}")
    sys.exit(1 if fallidos else 0)
