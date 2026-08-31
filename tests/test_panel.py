"""Tests del panel de finanzas (web/).

Lo que se prueba con más saña es QUIÉN PUEDE ENTRAR. El panel muestra las
finanzas completas de la casa: un fallo de autenticación acá no es un bug, es
una filtración. Las pantallas pueden salir feas y se arreglan; la puerta no.

Correr:  python3 tests/test_panel.py
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "token-de-prueba-123")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "424242")

_psycopg = types.ModuleType("psycopg")
_rows = types.ModuleType("psycopg.rows")
_rows.dict_row = object
_psycopg.rows = _rows
_pool = types.ModuleType("psycopg_pool")
_pool.AsyncConnectionPool = lambda *a, **k: None
sys.modules["psycopg"] = _psycopg
sys.modules["psycopg.rows"] = _rows
sys.modules["psycopg_pool"] = _pool

import config  # noqa: E402
import web.auth as auth  # noqa: E402

DUENO = config.CHAT_ID_DUENO


# ── La puerta ────────────────────────────────────────────────────────────

def test_token_valido_deja_entrar():
    assert auth.validar(auth.crear_token(DUENO)) == DUENO
    assert auth.puede_entrar(DUENO)


def test_un_token_vencido_no_sirve():
    """El enlace viaja en la URL, así que va a quedar en historiales y en logs
    de servidores ajenos. Diez minutos es todo lo que tiene que durar."""
    viejo = auth.crear_token(DUENO, vida=-1)
    assert auth.validar(viejo) is None


def test_no_se_puede_falsificar_sin_el_secreto():
    """Lo esencial: sin TELEGRAM_TOKEN no se puede fabricar un token."""
    chat, vence, firma = auth.crear_token(DUENO).split(".")
    for falso in (f"{chat}.{vence}.{'0'*32}",           # firma inventada
                  f"{chat}.{int(time.time())+99999}.{firma}",  # fecha movida
                  f"{DUENO + 1}.{vence}.{firma}"):      # otro chat
        assert auth.validar(falso) is None, falso


def test_otro_chat_no_entra_aunque_su_token_sea_valido():
    """Un token bien firmado para OTRO chat es válido como token y aun así no
    puede entrar. Son dos preguntas distintas: si el token es auténtico, y si
    ese chat tiene permiso. Confundirlas es cómo se abren los paneles."""
    ajeno = auth.crear_token(DUENO + 1)
    assert auth.validar(ajeno) == DUENO + 1
    assert not auth.puede_entrar(auth.validar(ajeno))


def test_basura_no_revienta():
    for x in (None, "", "abc", "a.b", "1.2.3.4", "...", "x" * 500):
        assert auth.validar(x) is None, repr(x)


def test_la_sesion_dura_mas_que_el_enlace():
    """El enlace es de un uso y viaja expuesto; la cookie vive en el navegador
    y nunca aparece en una URL."""
    assert auth.VIDA_SESION > auth.VIDA_ENLACE
    assert auth.VIDA_ENLACE <= 900, "un enlace en una URL no puede durar horas"


# ── Las rutas exigen sesión ──────────────────────────────────────────────

def test_todas_las_rutas_estan_protegidas():
    """Cada ruta nueva es una puerta nueva. Este test recorre la app y falla si
    alguna no comprueba la sesión — sin él, añadir una pantalla y olvidarse del
    guardarraíl no rompería nada visible."""
    import inspect
    import web.app as panel
    sin_guardia = []
    for ruta in panel.app.routes:
        fn = getattr(ruta, "endpoint", None)
        nombre = getattr(fn, "__name__", "")
        if not fn or nombre == "entrar":       # la puerta valida aparte
            continue
        fuente = inspect.getsource(fn)
        if "puede_entrar" not in fuente:
            sin_guardia.append(f"{getattr(ruta,'path','?')} ({nombre})")
    assert not sin_guardia, f"rutas sin comprobar sesión: {sin_guardia}"


def test_la_cookie_no_es_accesible_por_javascript():
    import inspect
    import web.app as panel
    fuente = inspect.getsource(panel.entrar)
    assert "httponly=True" in fuente, "la cookie de sesión tiene que ser httponly"
    assert "secure=True" in fuente, "la cookie no puede viajar en claro"
    assert 'samesite="lax"' in fuente or "samesite='lax'" in fuente


# ── Formato ──────────────────────────────────────────────────────────────

def test_el_dinero_se_formatea_sin_mezclar_monedas():
    from decimal import Decimal
    import web.app as panel
    assert panel._pesos(Decimal("1234.5")) == "1,234.50"
    assert panel._pesos(None) == "—"
    # Sin símbolo: la moneda va en su propia columna, porque juntar DOP y USD
    # en la misma cifra es el error que este panel existe para no cometer.
    assert "$" not in panel._pesos(Decimal("10"))


# ── La cola de corrección ────────────────────────────────────────────────

def test_la_cola_se_guarda_entera_de_una_vez():
    """Un formulario por fila hacía que guardar una recargara la página y se
    llevara puesto lo que ya estaba elegido en las demás: marcar y guardar de
    uno en uno. Con cuarenta movimientos eso no lo hace nadie, y una cola que no
    se corrige no le enseña nada al sistema — el defecto de usabilidad se comía
    la función entera."""
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web", "plantillas",
        "sin_clasificar.html"), encoding="utf-8").read()
    assert html.count("<form") == 1, (
        f"hay {html.count('<form')} formularios: uno por fila vuelve a perder "
        "lo seleccionado en las demás al guardar")
    assert "{% for m in movs %}" in html and "cat_{{ m.id }}" in html, (
        "los campos tienen que ir nombrados por id; emparejar listas paralelas "
        "por posición se rompe en cuanto una fila va vacía")


def test_la_cola_no_pide_clasificar_ingresos():
    """El dinero que entra no se clasifica. Un ingreso en la cola no aportaba
    nada y sí quitaba: empujaba hacia abajo un gasto que sí hay que mirar."""
    import inspect
    import db.db as base
    sql = inspect.getsource(base.sin_clasificar)
    assert "tipo = 'gasto'" in sql, (
        "la cola volvió a traer ingresos o traspasos")


def test_no_hay_categorias_de_ingreso_en_el_vocabulario():
    """Si los ingresos no se clasifican, ofrecer "Salario" o "Intereses" es
    ofrecer opciones que nada puede usar."""
    from cerebro.bancos.categorias import CATEGORIAS
    for prohibida in ("Salario", "Intereses", "Ingresos varios"):
        assert prohibida not in CATEGORIAS, (
            f"'{prohibida}' es categoría de ingreso y los ingresos no se "
            "clasifican")


def test_una_categoria_ya_puesta_se_puede_cambiar():
    """El caso que de verdad importa: una categoría EQUIVOCADA. La cola solo
    trae las que no tienen ninguna, así que sin esta pantalla un error quedaba
    fijo para siempre — y peor, seguía enseñándole lo mismo al sistema en cada
    compra siguiente. Corregirlo tenía que pasar por pedírselo a Claude, o sea
    que cada corrección costaba dinero."""
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web", "plantillas",
        "movimientos.html"), encoding="utf-8").read()
    assert 'action="/categorias"' in html, "la tabla no se puede editar"
    assert "prev_{{ m.id }}" in html, (
        "sin el valor previo no se distingue 'no la toqué' de 'la vacié'")
    assert "'selected' if m.categoria == c" in html, (
        "el desplegable tiene que venir con la categoría actual puesta")


def test_vaciar_una_categoria_tambien_desaprende():
    """Si no, quitar una categoría equivocada duraba hasta la próxima compra en
    el mismo sitio: la regla aprendida seguía viva y volvía a ponerla, esa vez
    sin pasar por ninguna cola."""
    import inspect
    import web.app as panel
    fuente = inspect.getsource(panel.categorias)
    assert "olvidar_categoria" in fuente
    import db.db as base
    assert hasattr(base, "olvidar_categoria")


def test_el_redirect_no_acepta_destinos_de_afuera():
    """`volver` viene del formulario. Un destino que no se comprueba es un
    redirect abierto: basta un POST con volver=https://otro-sitio."""
    import inspect
    import web.app as panel
    fuente = inspect.getsource(panel.categorias)
    assert 'startswith("/movimientos")' in fuente, (
        "el destino del redirect no se está comprobando")


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
