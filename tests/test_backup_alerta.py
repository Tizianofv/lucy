"""Tests del 30-ago-2026: un respaldo que falta tiene que GRITAR.

El agujero que esto cubre: los backups de la base corrían a las 20:00 hasta el
29-jul y después dejaron de correr. Pasaron 25 días sin una sola copia y sin
una sola alerta, porque la única prueba de que un backup había ocurrido era un
archivo en una carpeta de Google Drive que el proceso de Railway no puede ver.

Lo que hay que proteger acá son tres cosas, y la tercera es la que más fácil se
rompe sin querer al "mejorar" el aviso:
  1. Que 48 horas sin respaldo disparen el aviso.
  2. Que NUNCA haber tenido respaldo (tabla vacía) grite igual de fuerte. Es el
     caso real de hoy, y el que un `if ultimo is None: return` silenciaría.
  3. Que el aviso NO se repita en cada vuelta del bucle. Un aviso cada 5 minutos
     es un aviso que se ignora, y ahí volvemos al silencio por la puerta de al
     lado.

Y una cuarta, la del backup mismo: que la fila del latido se escriba DESPUÉS de
cerrar el archivo. Si se escribiera antes, un backup que revienta a la mitad
dejaría constancia de un respaldo que no existe — que es peor que no tener nada,
porque además apaga la alarma.

Herméticos: sin Postgres ni red. Mismo patrón de stubs que test_anticipos.py.

Correr:  python3 tests/test_backup_alerta.py
(o con pytest: pytest tests/test_backup_alerta.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 1) Entorno + stubs ANTES de importar el código real.
# ---------------------------------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("CHAT_ID_DUENO", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("GOOGLE_SA_KEY", "")

_psycopg = types.ModuleType("psycopg")
_psycopg_rows = types.ModuleType("psycopg.rows")
_psycopg_rows.dict_row = object()
_psycopg.rows = _psycopg_rows
sys.modules.setdefault("psycopg", _psycopg)
sys.modules.setdefault("psycopg.rows", _psycopg_rows)

_psycopg_pool = types.ModuleType("psycopg_pool")


class _StubPool:
    def __init__(self, *a, **k):
        pass


_psycopg_pool.AsyncConnectionPool = _StubPool
sys.modules.setdefault("psycopg_pool", _psycopg_pool)

_openai = types.ModuleType("openai")


class _StubAsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _StubAsyncOpenAI
sys.modules.setdefault("openai", _openai)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db.db as db  # noqa: E402
from cerebro import despertador  # noqa: E402

AHORA = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 2) La decisión pura: _hay_que_avisar. Sin base, sin Telegram, sin reloj.
# ---------------------------------------------------------------------------
def test_backup_reciente_no_avisa():
    """Un respaldo de anoche es un sistema sano. Callarse es lo correcto."""
    assert not despertador._hay_que_avisar(
        AHORA - timedelta(hours=12), None, AHORA)


def test_a_las_47_horas_todavia_calla():
    """La PC apagada una noche no es una emergencia: avisar sería enseñar a ignorar."""
    assert not despertador._hay_que_avisar(
        AHORA - timedelta(hours=47), None, AHORA)


def test_a_las_49_horas_avisa():
    """Dos días sin copia ya no es un tropiezo, es una tendencia."""
    assert despertador._hay_que_avisar(
        AHORA - timedelta(hours=49), None, AHORA)


def test_nunca_hubo_respaldo_tambien_avisa():
    """El caso REAL de hoy, y el que un `if ultimo is None: return` silenciaría.

    'No hay ninguno registrado' no es 'todavía no sabemos': es la peor noticia
    posible sobre el respaldo, y tiene que salir por la misma puerta.
    """
    assert despertador._hay_que_avisar(None, None, AHORA)


def test_el_aviso_no_se_repite_cada_vuelta():
    """Ya avisó hace 2 horas: el problema sigue, pero repetirlo lo vuelve ruido.

    Sin esto, el chequeo cada ~10 min mandaría 144 mensajes por día — y al
    tercero Tiziano dejaría de leerlos, que es exactamente el silencio que esto
    vino a romper.
    """
    assert not despertador._hay_que_avisar(
        None, AHORA - timedelta(hours=2), AHORA)


def test_al_dia_siguiente_vuelve_a_recordar():
    """El recordatorio diario: espaciado para que no se ignore, pero no se rinde."""
    assert despertador._hay_que_avisar(
        None, AHORA - timedelta(hours=25), AHORA)


def test_un_backup_nuevo_apaga_el_aviso_aunque_haya_avisado_recien():
    """Se respaldó hace una hora: el problema se arregló, el aviso se calla YA.

    El orden de las guardas importa. Si se mirara primero el último aviso, Lucy
    seguiría gritando 'no hay respaldo' durante 24 horas después de que Tiziano
    corriera el backup — mintiéndole justo cuando le hizo caso.
    """
    assert not despertador._hay_que_avisar(
        AHORA - timedelta(hours=1), AHORA - timedelta(minutes=5), AHORA)


# ---------------------------------------------------------------------------
# 3) El texto: tiene que decir la verdad y decir qué hacer.
# ---------------------------------------------------------------------------
def test_el_texto_arranca_con_la_firma_que_usa_el_dedupe():
    """La firma es a la vez lo que se lee y la clave del dedupe en la bandeja.

    Si alguien cambia el texto del aviso y se lleva puesto el prefijo,
    `ultimo_aviso_de_backup()` deja de encontrar el aviso anterior y el
    recordatorio diario se convierte en uno cada 10 minutos. El dedupe se
    rompería en silencio: por eso se prueba el acople.
    """
    for ultimo in (None, AHORA - timedelta(days=25)):
        texto = despertador._texto_backup(ultimo, AHORA)
        assert texto.startswith(db.AVISO_BACKUP_PREFIJO), (
            f"el aviso ya no arranca con la firma del dedupe: {texto[:40]!r}")


def test_el_texto_dice_cuantos_dias_y_como_arreglarlo():
    """Un grito sin salida es ruido: el aviso trae el comando para respaldar."""
    texto = despertador._texto_backup(AHORA - timedelta(days=25), AHORA)
    assert "25 día" in texto, f"no dice hace cuánto: {texto!r}"
    assert "db/backup.py" in texto, "no dice cómo respaldar"


def test_el_texto_sin_respaldo_no_finge_una_fecha():
    """Cuando no hay ninguno, el aviso lo dice así — no inventa un 'hace 0 días'."""
    texto = despertador._texto_backup(None, AHORA)
    assert "NINGÚN" in texto, f"suaviza el 'nunca': {texto!r}"
    assert "hace 0" not in texto


# ---------------------------------------------------------------------------
# 4) revisar_backup de punta a punta, con la base y el bot falsos.
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self):
        self.mensajes: list[str] = []

    async def send_message(self, chat_id, text):
        self.mensajes.append(text)


def _con_base(ultimo_backup, ultimo_aviso, bot):
    """Reemplaza los dos accesos a Postgres y el registro del aviso en la bandeja."""
    registrados: list[str] = []

    async def _ultimo_backup():
        return {"hecho_en": ultimo_backup} if ultimo_backup else None

    async def _ultimo_aviso():
        return ultimo_aviso

    async def _registrar_aviso(chat_id, texto):
        registrados.append(texto)
        return 1

    despertador.db.ultimo_backup = _ultimo_backup
    despertador.db.ultimo_aviso_de_backup = _ultimo_aviso
    despertador.db.registrar_aviso = _registrar_aviso
    return registrados


async def test_revisar_backup_manda_y_deja_constancia():
    """Manda por Telegram Y lo escribe en la bandeja: si no se registra, el
    dedupe no tiene de dónde acordarse y el aviso se repite para siempre."""
    bot = FakeBot()
    registrados = _con_base(None, None, bot)
    n = await despertador.revisar_backup(bot)
    assert n == 1, "no avisó sin ningún respaldo registrado"
    assert len(bot.mensajes) == 1, f"mensajes enviados: {len(bot.mensajes)}"
    assert bot.mensajes[0].startswith(db.AVISO_BACKUP_PREFIJO)
    assert registrados == bot.mensajes, (
        "el aviso salió por Telegram pero no quedó en la bandeja: el próximo "
        "chequeo no va a saber que ya avisó")


async def test_revisar_backup_calla_con_respaldo_fresco():
    bot = FakeBot()
    _con_base(AHORA - timedelta(hours=3), None, bot)
    assert await despertador.revisar_backup(bot) == 0
    assert bot.mensajes == [], "avisó teniendo un respaldo de hace 3 horas"


# ---------------------------------------------------------------------------
# 5) El latido del backup: la fila se escribe DESPUÉS del archivo.
# ---------------------------------------------------------------------------
def test_el_registro_va_despues_de_cerrar_el_archivo():
    """Una fila escrita antes del gzip sería constancia de un backup inexistente.

    Peor que no tener respaldo: además apaga la alarma que avisaría que falta.
    Se chequea sobre el texto porque el orden es la garantía entera, y es de las
    cosas que un refactor bienintencionado reordena sin notar.
    """
    fuente = open(os.path.join(_ROOT, "db", "backup.py"), encoding="utf-8").read()
    cuerpo = fuente.split("def hacer_backup()", 1)[1]
    escritura = cuerpo.index("gzip.open(archivo")
    registro = cuerpo.index("_registrar(conn")
    assert escritura < registro, (
        "_registrar() quedó ANTES de cerrar el .json.gz: dejaría constancia de "
        "un respaldo que puede no existir")


def test_la_rotacion_se_lleva_el_esquema_con_su_json():
    """Separarlos dejaría datos viejos junto a un esquema nuevo — el modo más
    silencioso de que una restauración salga mal."""
    fuente = open(os.path.join(_ROOT, "db", "backup.py"), encoding="utf-8").read()
    rotar = fuente.split("def _rotar(", 1)[1]
    assert "schema.sql.gz" in rotar, (
        "_rotar() borra el .json.gz y deja el .schema.sql.gz huérfano")


def test_la_clave_de_la_base_no_va_en_la_linea_de_comandos():
    """pg_dump recibe la URL como argumento, y argv lo lee cualquier `ps`.

    Dejar la contraseña adentro sería publicar la credencial de toda la base en
    cada corrida del backup. Se prueba también el caso percent-encodeado, que
    es el que se rompe callado: libpq espera PGPASSWORD ya decodificada, así
    que un `%40` mal tratado da un "authentication failed" que parece otra cosa.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bk", os.path.join(_ROOT, "db", "backup.py"))
    bk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bk)

    url = "postgresql://lucy:s3cr%40et@monorail.proxy.rlwy.net:12345/railway"
    limpia, entorno = bk._sin_clave(url)
    assert "s3cr" not in limpia, f"la clave sigue en argv: {limpia}"
    assert entorno["PGPASSWORD"] == "s3cr@et", "PGPASSWORD quedó percent-encodeada"
    assert "lucy@monorail.proxy.rlwy.net:12345" in limpia, (
        f"se perdió el usuario o el host al sacar la clave: {limpia}")

    # Sin contraseña no se toca nada ni se inventa un PGPASSWORD vacío.
    sin, entorno2 = bk._sin_clave("postgresql://host:5432/railway")
    assert sin == "postgresql://host:5432/railway"
    assert entorno2 == {}


# ---------------------------------------------------------------------------
# 6) La deriva del esquema: lo que dejó a schema.sql mintiendo por meses.
# ---------------------------------------------------------------------------
def test_schema_sql_declara_lo_que_el_codigo_usa():
    """`psql -f db/schema.sql` tiene que dar una base que Lucy pueda usar.

    Hasta hoy no la daba: `correo_reportado` (329 filas en producción) y
    `correo_estado.ultimo_reporte` existían en la base real y no en el archivo
    del repo, así que una instalación nueva reventaba en el primer reporte de
    correo. Un esquema versionado que no describe la base real es peor que no
    tener ninguno: se le cree.
    """
    esquema = open(os.path.join(_ROOT, "db", "schema.sql"), encoding="utf-8").read()
    for necesario in ("correo_reportado", "ultimo_reporte", "backups"):
        assert necesario in esquema, (
            f"'{necesario}' se usa en el código y no está en db/schema.sql")


_TESTS = [
    test_backup_reciente_no_avisa,
    test_a_las_47_horas_todavia_calla,
    test_a_las_49_horas_avisa,
    test_nunca_hubo_respaldo_tambien_avisa,
    test_el_aviso_no_se_repite_cada_vuelta,
    test_al_dia_siguiente_vuelve_a_recordar,
    test_un_backup_nuevo_apaga_el_aviso_aunque_haya_avisado_recien,
    test_el_texto_arranca_con_la_firma_que_usa_el_dedupe,
    test_el_texto_dice_cuantos_dias_y_como_arreglarlo,
    test_el_texto_sin_respaldo_no_finge_una_fecha,
    test_revisar_backup_manda_y_deja_constancia,
    test_revisar_backup_calla_con_respaldo_fresco,
    test_el_registro_va_despues_de_cerrar_el_archivo,
    test_la_rotacion_se_lleva_el_esquema_con_su_json,
    test_la_clave_de_la_base_no_va_en_la_linea_de_comandos,
    test_schema_sql_declara_lo_que_el_codigo_usa,
]


async def _main():
    fallos = 0
    for t in _TESTS:
        try:
            r = t()
            if asyncio.iscoroutine(r):
                await r
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            fallos += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fallos += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - fallos}/{len(_TESTS)} en verde")
    return fallos


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
