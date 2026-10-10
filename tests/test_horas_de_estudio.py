"""«N h de estudio en M sesiones», la cifra de «Cómo va» (parte 14 de la página de
un proyecto, 9-oct-2026): las garantías de la fila 14 del diseño aprobado.

QUÉ SE VIGILA, una prueba por garantía:
  · sale de la MISMA lectura del registro que el bloque de sesiones: abrir el proyecto
    sigue haciendo UN solo pedido a la App;
  · sale de la lista ya con las quitadas fuera y las agregadas dentro, y solo de la
    lista «Sesiones»: ni los trabajos ni las canceladas cuentan;
  · una sesión sin horas cuenta como sesión y no suma;
  · el número sale como el bloque pinta las horas de un renglón («3 h», «1.5 h»);
  · cuando el registro no contestó, no pudo ligar las del cliente o no se le preguntó,
    la cifra NO se pinta (nada de «0 h»); y con el registro contestando y ninguna
    sesión que pintar, tampoco;
  · se ve igual en la sesión de la casa y en la de solo ver;
  · la función que suma, sola, con sus bordes (decimales y horas que no son un número).

CÓMO. Igual que `tests/test_sesiones_de_proyecto.py`: la App se dobla EN LA RED (un
servidor HTTP de verdad en 127.0.0.1) y la página se pinta por la ruta real y la
plantilla real, con la base reemplazada por el modelo de prueba.

Correr:  python3 -m pytest tests/test_horas_de_estudio.py -q
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

import _proyectos_de_prueba as pp  # noqa: E402
import registro_lectura  # noqa: E402
from test_sesiones_de_proyecto import (  # noqa: E402,F401
    CANCELADA, SESION, TRABAJO, _abrir, _forzar, _respuesta, registro)

# Las sesiones de este trabajo: horas que suman, una sin horas, un trabajo con horas, una
# cancelada, una quitada y una agregada a mano. Los `ref` son distintos de los de la sesión
# base para no pisarse con los dobles que ya existen.
CON_HORAS = {**SESION, "ref": 21, "codigo": "s021", "horas": 3}
MEDIA = {**SESION, "ref": 22, "codigo": "s022", "horas": 1.5}
SIN_HORAS = {**SESION, "ref": 23, "codigo": "s023", "horas": None}
TRABAJO_CON_HORAS = {**TRABAJO, "ref": 24, "codigo": "t024", "horas": 2}
AGREGADA = {**SESION, "ref": 25, "codigo": "a025", "horas": 2.5}
QUITADA = "21"
AGREGADA_REF = "25"


def _como_va(html: str) -> str:
    """Lo que hay dentro del bloque «Cómo va», para mirar solo eso."""
    dentro = html.split('<section class="bloque como-va">')[1].split("</section>")[0]
    return " ".join(dentro.split())


def _las_sesiones(html: str) -> str:
    """Lo que hay dentro del bloque de las sesiones, para mirar solo eso."""
    dentro = html.split('id="sesiones-y-trabajos"')[1].split("</section>")[0]
    return " ".join(dentro.split())


def _lista_de(html: str, titulo: str) -> str:
    """Lo que hay dentro de una de las listas del bloque de sesiones («Sesiones», «Trabajos»,
    «Quitadas»): el `<h3>` del título y lo que sigue hasta el próximo `<h3>` o el fin del bloque."""
    bloque = html.split('id="sesiones-y-trabajos"')[1].split("</section>")[0]
    trozo = bloque.split(f"<h3>{titulo}</h3>", 1)[1]
    return " ".join(trozo.split("<h3>")[0].split())


def _con_decisiones(monkeypatch, *, quitadas=(QUITADA,), agregadas=(AGREGADA_REF,)):
    """La casa quitó una sesión de la lista y agregó otra a mano (partes 11 y 13)."""
    monkeypatch.setattr(pp, "QUITADAS_DEL_MODELO",
                        {1: {r: "s0" + r for r in quitadas}} if quitadas else {})
    monkeypatch.setattr(pp, "AGREGADAS_DEL_MODELO",
                        {1: {r: "a0" + r for r in agregadas}} if agregadas else {})


# ═══════════════════════════════════════════════════════════════════════
# Garantía: la cifra y de dónde sale (una sola llamada, la lista pintada)
# ═══════════════════════════════════════════════════════════════════════

def test_la_cifra_sale_de_la_lista_pintada_ni_trabajos_ni_canceladas_ni_quitadas(registro, monkeypatch):
    """Con una lista donde hay de todo, la cifra cuenta y suma solo las sesiones pintadas."""
    registro.sesiones = _respuesta(
        sesiones=(CON_HORAS, MEDIA, SIN_HORAS, TRABAJO_CON_HORAS, CANCELADA))
    registro.otras = [AGREGADA]
    _con_decisiones(monkeypatch)
    html = _abrir(monkeypatch)

    # Pintadas: MEDIA (1.5) + SIN_HORAS (no suma) + AGREGADA (2.5) = 4 h en 3 sesiones.
    assert "4 h de estudio en 3 sesiones" in _como_va(html)
    # Y sale de la lista «Sesiones», que es la que lleva esos tres códigos: el trabajo va en
    # «Trabajos», la quitada en «Quitadas» y la cancelada no sale.
    lista = _lista_de(html, "Sesiones")
    for pinta in ("s022", "s023", "a025"):
        assert pinta in lista, pinta
    for no_va in ("t024", "s021", "c013"):
        assert no_va not in lista, f"{no_va} no debería estar en la lista de sesiones"
    assert "t024" in _lista_de(html, "Trabajos")
    assert "s021" in _lista_de(html, "Quitadas")


def test_abrir_el_proyecto_sigue_haciendo_un_solo_pedido_al_registro(registro, monkeypatch):
    """La cifra no cuesta un pedido más: la misma lectura del bloque."""
    registro.sesiones = _respuesta(sesiones=(MEDIA, SIN_HORAS, TRABAJO_CON_HORAS))
    registro.otras = [AGREGADA]
    _con_decisiones(monkeypatch)
    html = _abrir(monkeypatch)
    assert "4 h de estudio en 3 sesiones" in _como_va(html)
    assert len(registro.pedidos) == 1, (
        f"abrir un proyecto hizo {len(registro.pedidos)} pedido(s): es uno solo")


def test_una_sesion_sin_horas_cuenta_como_sesion_y_no_suma(registro, monkeypatch):
    """Sin horas que sumar pero con sesiones, la cifra se pinta con 0 h (es verdad)."""
    registro.sesiones = _respuesta(sesiones=(SIN_HORAS, {**SIN_HORAS, "ref": 26, "codigo": "s026"}))
    _con_decisiones(monkeypatch, quitadas=(), agregadas=())
    assert "0 h de estudio en 2 sesiones" in _como_va(_abrir(monkeypatch))


def test_una_sola_sesion_lo_dice_en_singular(registro, monkeypatch):
    registro.sesiones = _respuesta(sesiones=(CON_HORAS,))
    _con_decisiones(monkeypatch, quitadas=(), agregadas=())
    assert "3 h de estudio en 1 sesión" in _como_va(_abrir(monkeypatch))


# ═══════════════════════════════════════════════════════════════════════
# Garantía: sin dato no se pinta (y nunca un «0 h»)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("como", ["502", "401"])
def test_sin_la_respuesta_del_registro_no_se_pinta_la_cifra(registro, monkeypatch, como):
    registro.forzada = _forzar(int(como))
    html = _abrir(monkeypatch)
    assert "h de estudio" not in html
    assert "No se pudo consultar el registro." in html


def test_sin_poder_ligar_las_del_cliente_no_se_pinta_aunque_haya_agregadas(registro, monkeypatch):
    """Con la ficha sin ligar, la lista trae las agregadas pero la cifra no se pinta: lo que hay
    es una lista incompleta, no un «0 h»."""
    registro.sesiones = _respuesta(sesiones=(), puede_ligar=False, motivo="sin_telefono")
    registro.otras = [AGREGADA]
    _con_decisiones(monkeypatch, quitadas=())
    html = _abrir(monkeypatch)
    assert "a025" in _las_sesiones(html), "la agregada sí se pinta"
    assert "h de estudio" not in _como_va(html)


def test_sin_cliente_no_se_le_pregunta_al_registro_ni_hay_cifra(registro, monkeypatch):
    html = _abrir(monkeypatch, noco_id=None)
    assert registro.pedidos == []
    assert "h de estudio" not in _como_va(html)


def test_con_el_registro_contestando_y_sin_sesiones_tampoco_se_pinta(registro, monkeypatch):
    """«0 h de estudio en 0 sesiones» sería verdad, pero es ruido."""
    registro.sesiones = _respuesta(sesiones=())
    _con_decisiones(monkeypatch, quitadas=(), agregadas=())
    html = _abrir(monkeypatch)
    assert "El registro no tiene sesiones ni trabajos de este cliente." in _las_sesiones(html)
    assert "h de estudio" not in _como_va(html)


# ═══════════════════════════════════════════════════════════════════════
# Garantía: se ve igual en la casa y en solo ver
# ═══════════════════════════════════════════════════════════════════════

def test_la_cifra_es_la_misma_en_la_casa_y_en_solo_ver(registro, monkeypatch):
    registro.sesiones = _respuesta(sesiones=(MEDIA, SIN_HORAS, TRABAJO_CON_HORAS))
    registro.otras = [AGREGADA]
    _con_decisiones(monkeypatch)
    casa = _como_va(_abrir(monkeypatch, con="casa"))
    ver = _como_va(_abrir(monkeypatch, con="ver"))
    assert "4 h de estudio en 3 sesiones" in casa
    assert casa == ver, (casa, ver)


# ═══════════════════════════════════════════════════════════════════════
# La función que suma, sola, con sus bordes
# ═══════════════════════════════════════════════════════════════════════

def _suma(*horas: str | None, estado: str = "ok"):
    return registro_lectura.horas_de_estudio(
        {"estado": estado, "sesiones": [{"horas": h} for h in horas]})


def test_la_suma_con_decimales_y_sin_ceros_de_cola():
    assert _suma("3", "1.5", "2.25") == {"horas": "6.75", "sesiones": 3}
    assert _suma("3", "1.5") == {"horas": "4.5", "sesiones": 2}
    assert _suma("1.5", "1.5") == {"horas": "3", "sesiones": 2}
    assert _suma("0.1", "0.2") == {"horas": "0.3", "sesiones": 2}
    assert _suma("14") == {"horas": "14", "sesiones": 1}


def test_una_sesion_sin_horas_cuenta_y_no_suma():
    assert _suma("3", None) == {"horas": "3", "sesiones": 2}
    assert _suma(None, None) == {"horas": "0", "sesiones": 2}


@pytest.mark.parametrize("basura", ["muchas", "", "inf", "nan", "-", "1,5"])
def test_horas_que_no_son_un_numero_cuentan_y_no_suman(basura):
    assert _suma(basura, "2") == {"horas": "2", "sesiones": 2}


@pytest.mark.parametrize("estado", ["no_se_pudo", "sin_ligar", "sin_cliente"])
def test_sin_la_respuesta_del_registro_la_funcion_no_devuelve_cifra(estado):
    assert _suma("3", estado=estado) is None


def test_sin_ninguna_sesion_la_funcion_no_devuelve_cifra():
    assert _suma() is None
