"""El esquema que ve Lucy al escribir SQL tiene que ser el de la tabla real.

EL FALLO QUE MOTIVA ESTE ARCHIVO, medido el 4-sep-2026 sobre b32153d:

`cerebro/consultar.py` le describía las tablas al modelo con una lista de
columnas escrita a mano. Esa lista se había separado de `db/schema.sql` sin que
nadie se enterara: le faltaban 17 columnas de las 9 tablas que describe. Tres
eran de `movimientos` —`estado`, `banco`, `hash_contenido`—, así que Lucy no
sabía que `estado` existe.

Consecuencia: las cuatro consultas del panel filtran `estado <> 'declinada'`
(db/db.py, en resumen_por_mes, gasto_por_categoria, gastos_de_cada_categoria y
sin_clasificar) y el SQL que escribe Lucy no podía filtrarlo. Los dos caminos
contestaban números distintos a "¿cuánto gasté?", y el de Telegram contaba
dinero que el banco había rechazado.

Lo que se prueba acá NO es que hoy estén las tres columnas —eso se arregla una
vez y se vuelve a romper—. Es que la lista SE ARME desde db/schema.sql, y que
ninguna tabla ni ninguna columna pueda entrar o salir en silencio.

Herméticos: se stubea psycopg antes de importar, igual que en
test_movimientos_dedupe.py. No tocan ninguna base.

Correr:  python3 -m pytest tests/test_esquema_del_modelo.py -q
"""
from __future__ import annotations

import inspect
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

# ── Stubs de psycopg ANTES de importar db.db ─────────────────────────────
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

import db.db as base  # noqa: E402
import cerebro.consultar as consultar  # noqa: E402


# ── La lista de columnas sale de la tabla, no de la memoria de nadie ─────

def test_el_esquema_nombra_todas_las_columnas_de_sus_tablas():
    """Ni una columna de las tablas de Tiziano puede faltar en el prompt.

    Se mira EL BLOQUE DE CADA TABLA, no el prompt entero. Con el prompt entero
    este test no sirve para nada en el caso que importa: `estado` existe
    también en bandeja, tareas y proyectos, así que "estado" aparece en el
    texto aunque el bloque de `movimientos` lo haya perdido — que es
    literalmente el fallo del 4-sep-2026. Comprobado mutando: buscando en todo
    el texto, quitarle `estado` a movimientos deja el test en verde.

    Este es el test que habría dado rojo el día que se agregó
    `movimientos.estado` sin tocar el texto.
    """
    import re
    declaradas = base.columnas_declaradas()
    faltan = []
    for tabla in consultar.TABLAS_DE_TIZIANO:
        bloque = consultar.BLOQUES[tabla]
        for col in declaradas[tabla]:
            # Delimitado: `banco` no puede darse por nombrado porque el bloque
            # diga "bancos_usados" o "el banco rechazó".
            if not re.search(rf"(?<!\w){re.escape(col)}(?!\w)", bloque):
                faltan.append(f"{tabla}.{col}")
    assert not faltan, (
        f"{len(faltan)} columnas reales que Lucy no ve al escribir SQL: "
        f"{faltan}. Por una columna que no conoce no puede filtrar, y ahí es "
        "donde su número se separa del panel.")


def test_cada_bloque_llega_entero_al_prompt():
    """Armar bien los bloques no sirve si el prompt final no los lleva."""
    for tabla, bloque in consultar.BLOQUES.items():
        assert bloque in consultar.ESQUEMA, (
            f"el bloque de {tabla} no llegó al prompt")


def test_las_tres_columnas_del_fallo_estan():
    """El caso concreto del 4-sep-2026, fijado para que no vuelva callado."""
    for col in ("estado", "banco", "hash_contenido"):
        assert col in base.columnas_declaradas()["movimientos"], (
            f"movimientos.{col} desapareció de db/schema.sql")
        assert col in consultar.BLOQUES["movimientos"], (
            f"movimientos.{col} volvió a quedarse fuera del prompt")


def test_ninguna_tabla_entra_ni_sale_en_silencio():
    """Una tabla nueva en schema.sql obliga a decidir si Lucy la ve.

    Sin esto, agregar una tabla la deja invisible por omisión —que es
    exactamente cómo `movimientos.estado` pasó desapercibido, un nivel más
    abajo. Acá la omisión cuesta un rojo.
    """
    en_schema = set(base.columnas_declaradas())
    clasificadas = set(consultar.TABLAS_DE_TIZIANO) | set(
        consultar.TABLAS_DE_MAQUINARIA)
    sin_clasificar = en_schema - clasificadas
    assert not sin_clasificar, (
        f"tablas en db/schema.sql que nadie decidió si Lucy ve: "
        f"{sorted(sin_clasificar)}. Ponelas en TABLAS_DE_TIZIANO o en "
        "TABLAS_DE_MAQUINARIA con su motivo.")
    fantasmas = clasificadas - en_schema
    assert not fantasmas, (
        f"tablas clasificadas que ya no existen en db/schema.sql: "
        f"{sorted(fantasmas)}")


def test_no_hay_notas_huerfanas():
    """Una nota sobre una columna que ya no existe delata que la tabla cambió.

    Es la mitad que le faltaba al chequeo de arriba: aquél caza lo que sobra en
    la tabla, éste caza lo que sobra en el texto.
    """
    declaradas = base.columnas_declaradas()
    huerfanas = [f"{t}.{c}" for (t, c) in consultar.NOTAS_DE_COLUMNA
                 if c not in declaradas.get(t, [])]
    assert not huerfanas, (
        f"notas de columnas que db/schema.sql ya no declara: {huerfanas}")
    sin_titulo = [t for t in consultar.TABLAS_DE_TIZIANO
                  if t not in consultar.TITULOS]
    assert not sin_titulo, f"tablas sin título en el prompt: {sin_titulo}"


def test_la_lista_no_esta_copiada_a_mano():
    """El prompt se ARMA; no puede volver a ser una constante escrita.

    Mismo candado que el de las categorías (tests/test_crud_dedup.py:305): si
    mañana alguien "simplifica" pegando el texto ya renderizado, la copia se
    separa de la tabla otra vez y este archivo entero deja de proteger nada.
    """
    fuente = inspect.getsource(consultar._armar_bloques)
    assert "columnas_declaradas()" in fuente, (
        "_armar_bloques ya no lee las columnas de db/schema.sql")
    # Y ESQUEMA tiene que salir de esas funciones, no de un literal.
    texto = inspect.getsource(consultar)
    assert "BLOQUES = _armar_bloques()" in texto, (
        "los bloques volvieron a ser literales escritos a mano")
    assert "ESQUEMA = _armar_esquema()" in texto, (
        "ESQUEMA volvió a ser un literal escrito a mano")


# ── Conocer la columna no es usarla: el prompt tiene que MANDARLO ────────

def test_el_prompt_ordena_excluir_las_declinadas_por_defecto():
    """Que Lucy sepa que `estado` existe no la hace filtrar por él.

    El panel excluye las declinadas SIN que se lo pidan (db/db.py, cuatro
    consultas). Si Lucy solo las excluyera cuando la pregunta lo menciona,
    "¿cuánto gasté?" seguiría dando dos números distintos — que es el problema
    entero. Así que la instrucción tiene que estar escrita, y ser por defecto.
    """
    esquema = consultar.ESQUEMA
    assert "estado <> 'declinada'" in esquema, (
        "el prompt no le dice a Lucy CÓMO excluir las declinadas")
    assert "aunque la pregunta no lo mencione" in esquema, (
        "el prompt no dice que se excluyan POR DEFECTO: sin eso Lucy filtra "
        "solo cuando se lo piden y el panel filtra siempre")
    # Y no puede pasarse de frenada: 'aprobada' a secas borra las retenciones,
    # que sí son gasto real (Railway, Amazon Prime, Anthropic).
    assert "NUNCA uses \"estado = 'aprobada'\"" in esquema, (
        "falta el aviso de no filtrar por 'aprobada': eso borraría las "
        "retenciones, que sí ocurrieron")


def test_lucy_y_el_panel_filtran_lo_mismo():
    """El contrato de este trabajo, en una línea: mismo filtro, mismo número.

    Se compara contra la fuente de las consultas del panel en vez de contra un
    literal repetido acá: si mañana el panel cambia de criterio y el prompt no,
    esto se pone rojo en vez de dejar los dos números separándose otra vez.
    """
    for consulta in (base.resumen_por_mes, base.gasto_por_categoria,
                     base.gastos_de_cada_categoria, base.sin_clasificar):
        filtro_del_panel = "estado <> 'declinada'"
        assert filtro_del_panel in inspect.getsource(consulta), (
            f"{consulta.__name__} cambió de criterio sobre las declinadas")
        assert filtro_del_panel in consultar.ESQUEMA, (
            "el panel filtra las declinadas y el prompt de Lucy no")

    # El otro filtro que separa los números: el dinero de terceros.
    from cerebro.bancos.categorias import NO_SUMAN
    assert "NO_SUMAN" in inspect.getsource(base.resumen_por_mes), (
        "el panel dejó de excluir el dinero de terceros de los totales")
    for categoria in NO_SUMAN:
        assert categoria in consultar.ESQUEMA, (
            f"'{categoria}' no llega al prompt: Lucy sumaría plata de terceros "
            "que el panel no suma")


def test_el_prompt_no_manda_referencias_a_lineas_de_archivos():
    """Un "db/db.py:822" dentro del prompt es ruido que además se pudre.

    El modelo no puede abrir el archivo, así que la referencia no le sirve de
    nada; y el número deja de ser cierto en cuanto alguien agrega líneas más
    arriba. Medido el 5-sep-2026: el prompt decía `db/db.py:822` y el filtro de
    NO_SUMAN estaba en la 952, porque db/db.py había crecido 120 líneas en este
    mismo trabajo. Se nombran funciones, que no se mueven de sitio.
    """
    import re
    sobran = re.findall(r"[\w/]+\.py:\d+", consultar.ESQUEMA)
    assert not sobran, (
        f"el prompt le manda al modelo referencias a líneas de archivo: "
        f"{sobran}. El modelo no las puede abrir y el número se pudre solo.")


def test_las_categorias_que_no_suman_no_estan_copiadas_a_mano():
    """Igual que en el prompt del agente: la lista se inyecta, no se copia."""
    fuente = inspect.getsource(consultar)
    # El MODISMO 7 lleva un hueco {no_suman}, no el texto.
    assert "{no_suman}" in fuente, (
        "el MODISMO del dinero de terceros dejó de inyectar NO_SUMAN")
    assert "no_suman=" in fuente, "NO_SUMAN ya no se le pasa al format()"


def test_la_orden_llega_al_mensaje_que_se_le_manda_al_modelo():
    """Que el texto exista en una constante no es que el modelo lo reciba.

    Se ejercita `responder()` con el cliente stubeado y se mira el system
    message DE VERDAD. Si mañana alguien arma el prompt con otro texto —o le
    manda solo un resumen del esquema—, esto se pone rojo aunque ESQUEMA siga
    perfecto.

    Lo que NO prueba: que DeepSeek OBEDEZCA la orden. Eso no se puede saber
    sin llamar al modelo real, y queda dicho acá para que nadie lea este
    verde como si lo cubriera.
    """
    import asyncio
    import json as _json

    enviados = []

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    async def _falso_create(**kwargs):
        enviados.append(kwargs["messages"][0]["content"])
        if kwargs.get("response_format"):
            # Pide aclaración: así no se ejecuta ningún SQL ni se toca la base.
            return _Resp(_json.dumps({"sql": "", "aclaracion": "¿de qué mes?",
                                      "explicacion": "x"}))
        return _Resp("texto")

    # Se sustituye el CLIENTE ENTERO, no `cliente.chat.completions.create`.
    # Corriendo la suite completa, `cerebro.deepseek` se importa después de que
    # test_anticipos.py (y otros tres) metieran un `openai` de mentira en
    # sys.modules, así que `consultar.cliente` es un stub sin `.chat` y parchear
    # ese atributo revienta con AttributeError. Aislado pasaba; en la suite no.
    # Un doble autosuficiente no depende de con qué openai se importó el módulo.
    falso_cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_falso_create)))

    original = consultar.cliente
    consultar.cliente = falso_cliente
    try:
        r = asyncio.run(consultar.responder("¿cuánto gasté?"))
        # El SEGUNDO camino: cuando Postgres rechaza la consulta, _corregir()
        # la reescribe. Si ahí no fuera el esquema, el SQL corregido —que es el
        # que termina corriendo— podría volver sin el filtro, y el número malo
        # llegaría igual, solo que un intento más tarde.
        asyncio.run(consultar._corregir("¿cuánto gasté?", "SELECT 1", "boom"))
    finally:
        consultar.cliente = original

    assert len(enviados) == 2, (
        f"se esperaban 2 llamadas al modelo (plan y corrección), hubo "
        f"{len(enviados)}")
    for i, sistema_i in enumerate(enviados):
        assert consultar.ESQUEMA in sistema_i, (
            f"la llamada {i} no lleva el esquema completo")

    assert r["texto"] == "¿de qué mes?"
    assert enviados, "no se le mandó ningún system message al modelo"
    sistema = enviados[0]
    assert "estado <> 'declinada'" in sistema, (
        "la orden de excluir las declinadas no llega al modelo")
    assert "aunque la pregunta no lo mencione" in sistema
    assert consultar.BLOQUES["movimientos"] in sistema, (
        "las columnas de movimientos no llegan al modelo")
    # El esquema ENTERO, no un trozo: así un futuro "para ahorrar tokens le
    # mando solo las tablas que parecen relevantes" se pone rojo acá.
    assert consultar.ESQUEMA in sistema, (
        "al modelo no le llega el esquema completo, sino una versión recortada")


def test_el_agente_hereda_la_misma_orden():
    """Hay un TERCER camino, y también le tiene que llegar.

    `cerebro/agente.py:409` mete `consultar.ESQUEMA` entero en su propio prompt
    de sistema, así que el agente de Telegram escribe SQL con las mismas
    reglas. Si mañana alguien le arma ahí un esquema aparte «más corto», ese
    camino vuelve a dar un número distinto al del panel sin que nada se ponga
    rojo. Se comprueba sobre el prompt ARMADO, no sobre el import.
    """
    import cerebro.agente as agente
    sistema = agente._sistema()
    assert "estado <> 'declinada'" in sistema, (
        "el agente no recibe la orden de excluir las declinadas")
    assert "aunque la pregunta no lo mencione" in sistema
    assert consultar.ESQUEMA in sistema, (
        "el agente dejó de usar el esquema de consultar.py y armó el suyo")


# ── El detector del descuadre contra la base REAL ────────────────────────

def test_columnas_que_faltan_mira_las_dos_direcciones():
    """El caso peligroso es el silencioso: columna en la base, no en el archivo.

    Ya ocurrió con `movimientos.banco`. Si este chequeo solo mirara la
    dirección ruidosa (archivo → base), no habría visto nada, y hoy es la
    dirección que decide si Lucy conoce una columna.
    """
    fuente = inspect.getsource(base.columnas_que_faltan)
    assert "set(columnas) - reales[tabla]" in fuente, (
        "no se detecta lo que está en db/schema.sql y no en la base")
    assert "reales[tabla] - set(columnas)" in fuente, (
        "no se detecta lo que está en la base y no en db/schema.sql — la "
        "dirección silenciosa, que es la que deja a Lucy sin saber que una "
        "columna existe")
    assert "information_schema.columns" in fuente


def test_el_humo_se_pone_rojo_con_un_descuadre():
    """Devolver descuadres y salir en verde era el fallo callado de siempre.

    `tablas_que_faltan` corría dentro del bucle de humo.py, que solo mira si la
    consulta REVIENTA. Dos tablas faltantes se imprimían como "✓ ... 2".
    """
    ruta = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "humo.py")
    with open(ruta, encoding="utf-8") as f:
        humo = f.read()
    assert "columnas_que_faltan" in humo, (
        "tools/humo.py no comprueba el descuadre de columnas contra la base")
    assert "descuadres" in humo and "if rojas or descuadres:" in humo, (
        "un descuadre no hace salir a humo.py con código distinto de 0")
    # Y no puede haber vuelto al bucle que solo mira excepciones.
    assert '("tablas_que_faltan", lambda: db.tablas_que_faltan())' not in humo, (
        "tablas_que_faltan volvió al bucle donde su resultado se ignora")


def test_el_arranque_avisa_del_descuadre():
    """Si no sale en el log del arranque, se pierde entre los reintentos."""
    ruta = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    with open(ruta, encoding="utf-8") as f:
        arranque = f.read()
    assert "columnas_que_faltan" in arranque, (
        "main.py no comprueba las columnas al arrancar")
    assert "log.error" in arranque.split("columnas_que_faltan")[1][:400], (
        "el descuadre se avisa por debajo de error: en el log del arranque "
        "eso no lo ve nadie")


# ── El parser de schema.sql, que es de quien depende todo lo de arriba ───

def test_sin_schema_revienta_en_vez_de_armar_un_prompt_vacio(monkeypatch):
    """Un prompt sin columnas no deja a Lucy muda: la deja inventando.

    Es el modo de fallo que este trabajo entero viene a cerrar — contestar un
    número que no cuadra con el panel, sin que nada se ponga rojo. Así que si
    db/schema.sql no se puede leer, se revienta al importar y se ve en el log
    del arranque.
    """
    import pytest
    monkeypatch.setattr(base, "columnas_declaradas", lambda *a, **k: {})
    with pytest.raises(ValueError, match="no declara columnas"):
        consultar._armar_bloques()


def test_el_parser_no_confunde_una_restriccion_con_una_columna():
    """`bandeja` termina con CONSTRAINT bandeja_msg_unico UNIQUE (...).

    Si el parser lo tomara por columna, el prompt le ofrecería a Lucy una
    columna inexistente y su SQL reventaría con UndefinedColumn.
    """
    cols = base.columnas_declaradas()["bandeja"]
    assert "constraint" not in cols and "unique" not in cols
    assert "reintentar_despues" in cols, "se perdió la última columna real"


def test_el_parser_no_se_come_las_comas_de_un_check():
    """`movimientos.estado` lleva CHECK (estado IN ('aprobada', ...)).

    Cortar por todas las comas partiría esa línea y las columnas siguientes
    —`banco`, `borrado_en`— se leerían mal. Son justo las del fallo.
    """
    cols = base.columnas_declaradas()["movimientos"]
    assert cols[-3:] == ["estado", "banco", "borrado_en"], cols
    assert "'aprobada'" not in cols and "in" not in cols


def test_el_parser_lee_las_columnas_que_solo_estan_en_una_migracion(tmp_path):
    """`hash_contenido` vivió un tiempo solo en la migración del 30-ago.

    Es el modo real de este repo: la columna llega por migración y baja a
    schema.sql después. Si el parser no leyera migrations/, habría una ventana
    en la que Lucy no conoce una columna que la base ya tiene.

    Se prueba sobre un esquema de mentira en un directorio temporal —fuera del
    repositorio— y no sobre db/schema.sql: hoy todas las migraciones ya están
    volcadas al archivo, así que contra el repo real este test pasaría aunque
    el parser ignorara migrations/ por completo.
    """
    (tmp_path / "migrations").mkdir()
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE movimientos (\n"
        "  id   BIGSERIAL PRIMARY KEY,\n"
        "  monto NUMERIC(12,2) NOT NULL\n"
        ");\n", encoding="utf-8")
    (tmp_path / "migrations" / "2026-09-04_columna_nueva.sql").write_text(
        "ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS recien_llegada TEXT;\n",
        encoding="utf-8")

    cols = base.columnas_declaradas(str(tmp_path / "schema.sql"))["movimientos"]
    assert cols == ["id", "monto", "recien_llegada"], cols


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
