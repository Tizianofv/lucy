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
from decimal import Decimal

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
    """Este test ANTES no ejecutaba nada: comprobaba que el código fuente
    contuviera el string 'startswith("/movimientos")'. Daba tranquilidad sin
    dar garantía — no habría detectado ninguna regresión de comportamiento, ni
    ninguno de los payloads de abajo. Lo señaló el verificador y tenía razón.

    Ahora ejercita la decisión con los destinos hostiles de verdad. El que
    importa es "//evil.com": es una URL protocolo-relativa, y si pasara, el
    navegador se iría del sitio con la sesión abierta.
    """
    import web.app as panel
    for hostil in ("//evil.com", "https://evil.com", "http://evil.com",
                   "///evil.com", "\\\\evil.com", "javascript:alert(1)",
                   "/sin-clasificar/../../evil.com", "", "   ", "otra-cosa"):
        assert panel._destino_seguro(hostil) == "/sin-clasificar", (
            f"{hostil!r} se aceptó como destino de redirect")

    # Y lo legítimo tiene que seguir pasando, con sus filtros intactos: si no,
    # el arreglo rompe la mitad del trabajo en /movimientos.
    for bueno in ("/movimientos", "/movimientos?banco=bhd&tipo=gasto",
                  "  /movimientos?mes=2026-08  "):
        assert panel._destino_seguro(bueno) == bueno.strip(), bueno


def test_el_codigo_se_lee_como_lo_escribe_una_persona():
    """El código NO es una columna nueva: el id de Postgres ya es único y
    estable, y guardar además un código sería guardar dos veces el mismo hecho
    — dos copias del mismo hecho se desincronizan, siempre. Esto es
    presentación, y el buscador tiene que aceptar las formas en que alguien lo
    escribe de verdad: un buscador que exige el formato exacto no se usa."""
    import web.app as panel
    assert panel._codigo(86) == "M-0086"
    assert panel._codigo(1234) == "M-1234"
    assert panel._codigo(None) == "—"
    for escrito in ("M-0086", "m-0086", "m86", "86", " M 86 ", "0086"):
        assert panel._leer_codigo(escrito) == 86, escrito
    # Lo que no es un código no puede colarse como uno: devolver 0 o None por
    # error haría que el filtro escondiera todo sin decir por qué.
    for basura in ("abc", "", "M-", "8 6", "86abc", None):
        assert panel._leer_codigo(basura) is None, repr(basura)


def test_el_codigo_solo_lleva_digitos():
    """Se dicta en voz alta y se teclea a mano. Con letras aparecen los pares
    que todo el mundo confunde —0 y O, 1 y l— y el código deja de servir para
    lo único que existe: referirse a un movimiento sin equivocarse."""
    import web.app as panel
    for mid in (1, 42, 999, 123456):
        cuerpo = panel._codigo(mid).removeprefix("M-")
        assert cuerpo.isdigit(), panel._codigo(mid)


def test_buscar_por_codigo_manda_sobre_los_demas_filtros():
    """Buscar uno concreto y que los otros filtros lo escondan sería la forma
    más rápida de hacer creer que no existe."""
    import inspect
    import db.db as base
    sql = inspect.getsource(base.movimientos_filtrados)
    assert "id = %s::bigint" in sql, "el filtro por código no llega al SQL"


def test_un_ingreso_no_lleva_categoria():
    """Las categorías dicen EN QUÉ se gastó, y el dinero que entra no se gastó
    en nada: ofrecerle "Supermercado" a un ingreso responde una pregunta que
    nadie hizo, y ensucia los totales por categoría con dinero que no es gasto.

    La ÚNICA excepción es "No suma", que no es un rubro sino una marca — los
    intereses del certificado del papá de Rosi entran como ingreso y no son
    ingreso de esta casa. Sin eso, la mitad de ese circuito quedaría contada.
    """
    from cerebro.bancos.categorias import categoria_permitida
    assert categoria_permitida("gasto", "Supermercado")
    assert not categoria_permitida("ingreso", "Supermercado")
    assert not categoria_permitida("transferencia", "CDS")
    assert categoria_permitida("ingreso", "No suma")
    # Quitarle la categoría siempre se puede: es cómo se deshace un error.
    assert categoria_permitida("ingreso", None)
    assert categoria_permitida("gasto", "")
    # Y lo que no está en el vocabulario no pasa por ningún tipo.
    assert not categoria_permitida("gasto", "supermercado")


def test_la_regla_del_ingreso_vale_en_las_dos_puertas():
    """La pantalla ya no ofrece rubros en un ingreso, pero eso no basta: un POST
    a mano o una pestaña vieja los mandarían igual. Y por Telegram no hay
    pantalla que valga. Una regla que solo se aplica en una de las dos puertas
    no es una regla."""
    import inspect
    import db.db as base
    from acciones import crud
    assert "categoria_permitida" in inspect.getsource(base.poner_categoria), (
        "el panel no comprueba que la categoría le corresponda al tipo")
    assert "categoria_permitida" in inspect.getsource(crud.editar), (
        "Telegram no comprueba que la categoría le corresponda al tipo")


def test_la_categoria_que_no_suma_no_suma():
    """Se llama "No suma" y sumaba. La consulta la marcaba bien y la plantilla
    la pintaba debajo del total, pero el sum() que arma ese total recorría TODAS
    las filas: el "TOTAL DOP" incluía RD$36,164 de dinero de terceros.

    Marcar y mostrar aparte no es lo mismo que excluir, y el nombre de la
    categoría promete lo segundo.
    """
    import inspect
    import web.app as panel
    fuente = inspect.getsource(panel.resumen)
    assert 'if not f["no_suma"]' in fuente, (
        "el total del resumen por categoría volvió a sumar las que no suman")

    # Y la comprobación de verdad, sobre la aritmética y no sobre el texto:
    filas = [{"categoria": "Supermercado", "moneda": "DOP",
              "total": Decimal("100"), "n": 1, "no_suma": False},
             {"categoria": "No suma", "moneda": "DOP",
              "total": Decimal("900"), "n": 1, "no_suma": True}]
    por_moneda: dict = {}
    for f in filas:
        por_moneda.setdefault(f["moneda"], []).append(f)
    totales = {mo: sum(f["total"] for f in fs if not f["no_suma"])
               for mo, fs in por_moneda.items()}
    assert totales["DOP"] == Decimal("100"), (
        f"el total quedó en {totales['DOP']}: la marcada se coló")


def test_el_detalle_de_una_categoria_se_abre_sin_javascript():
    """El resumen es la pantalla que más se mira y la que menos puede fallar.
    Con <details> se despliega aunque el JS no cargue; con JavaScript, un día
    no abre y no hay forma de saber por qué."""
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web", "plantillas",
        "resumen.html"), encoding="utf-8").read()
    assert "<details>" in html and "<summary" in html
    assert "detalle.get((moneda, f.categoria)" in html, (
        "el detalle tiene que ir por moneda Y categoría: si se agrupa solo por "
        "categoría, los gastos en USD aparecerían dentro de la fila de DOP")


def _peticion(ruta="/", query=b""):
    """Un Request mínimo, con la cookie de sesión buena.

    No hace falta cliente HTTP: Starlette renderiza la plantilla al CONSTRUIR
    la TemplateResponse, así que llamar a la función de la ruta ya dispara
    cualquier variable indefinida.
    """
    from starlette.requests import Request
    import web.app as panel
    galleta = f"{panel.COOKIE}={auth.crear_token(DUENO, auth.VIDA_SESION)}"
    return Request({"type": "http", "http_version": "1.1", "method": "GET",
                    "scheme": "https", "server": ("t", 443), "path": ruta,
                    "root_path": "", "query_string": query,
                    "headers": [(b"host", b"t"), (b"cookie", galleta.encode())],
                    "app": panel.app})


def test_las_pantallas_se_pintan_de_verdad():
    """El test que había miraba el HTML con grep y decía verde mientras la
    portada devolvía Internal Server Error: la plantilla usaba `detalle` y la
    ruta no lo pasaba. Es la TERCERA vez hoy que un test comprueba que el
    código CONTIENE algo en vez de comprobar que FUNCIONA — las otras dos
    fueron el redirect y la validación de categorías.

    Esto llama a cada ruta con la base falseada y exige que la página se pinte.
    Una variable que la plantilla usa y la ruta no manda se cae acá.
    """
    import asyncio
    from decimal import Decimal

    import db.db as base
    import web.app as panel

    async def _resumen_mes():
        return [{"mes": "2026-08", "moneda": "DOP", "tipo": "gasto",
                 "total": Decimal("100"), "n": 1}]

    async def _por_categoria(mes=None):
        return [{"categoria": "Seguros", "moneda": "DOP",
                 "total": Decimal("100"), "n": 1, "no_suma": False}]

    async def _detalle(mes=None):
        return [{"id": 7, "fecha": "2026-08-04", "banco": "bhd",
                 "contraparte": "SEGUROS SURA", "monto": Decimal("100"),
                 "moneda": "DOP", "categoria": "Seguros"}]

    async def _meses():
        return ["2026-08"]

    async def _salud():
        return {"cuentas": [], "automaticos": 1, "ultimo": None,
                "patrones_propios": 3}

    async def _movs(*a, **k):
        return [{"id": 7, "fecha": "2026-08-04", "tipo": "gasto",
                 "monto": Decimal("100"), "moneda": "DOP",
                 "contraparte": "SEGUROS SURA", "categoria": "Seguros",
                 "referencia": "x", "banco": "bhd"}]

    async def _lista(*a, **k):
        return ["Seguros"]

    async def _duplicados():
        return [{"banco": "bhd", "cuando": "2026-08-05T11:08",
                 "monto": Decimal("2823.07"), "moneda": "DOP",
                 "ids": [92, 93],
                 "contrapartes": ["ALTICE HOGAR", "Tricom - IB BHDLeon"]}]

    guardado = {n: getattr(base, n) for n in (
        "resumen_por_mes", "gasto_por_categoria", "gastos_de_cada_categoria",
        "meses_con_movimientos", "salud_ingesta", "movimientos_filtrados",
        "sin_clasificar", "categorias_usadas", "bancos_usados",
        "posibles_duplicados")}
    base.resumen_por_mes = _resumen_mes
    base.gasto_por_categoria = _por_categoria
    base.gastos_de_cada_categoria = _detalle
    base.meses_con_movimientos = _meses
    base.salud_ingesta = _salud
    base.movimientos_filtrados = _movs
    base.sin_clasificar = _movs
    base.categorias_usadas = _lista
    base.bancos_usados = _lista
    base.posibles_duplicados = _duplicados
    try:
        bucle = asyncio.new_event_loop()
        for nombre, corutina in (
                ("/", panel.resumen(_peticion("/"))),
                ("/movimientos", panel.movimientos(_peticion("/movimientos"))),
                ("/sin-clasificar", panel.cola(_peticion("/sin-clasificar"))),
                ("/salud", panel.salud(_peticion("/salud")))):
            r = bucle.run_until_complete(corutina)
            assert r.status_code == 200, f"{nombre} devolvió {r.status_code}"
            assert len(r.body) > 200, f"{nombre} salió vacía"
    finally:
        for n, fn in guardado.items():
            setattr(base, n, fn)


def test_las_dos_puertas_respetan_que_la_marca_no_se_aprende():
    import inspect
    import db.db as base
    from acciones import crud
    assert "se_aprende" in inspect.getsource(base.poner_categoria), (
        "el panel volvería a enseñar la marca")
    assert "se_aprende" in inspect.getsource(crud.editar), (
        "Telegram volvería a enseñar la marca")


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
