"""Ingesta de movimientos bancarios: del buzón a la tabla `movimientos`.

Este es el módulo que hace que todo lo demás sirva. Los parsers de
`cerebro/bancos/` convierten correos en movimientos, pero hasta acá nadie los
llamaba: 461 movimientos parseados eran cero filas en la base.

TRES INVARIANTES QUE NO SE NEGOCIAN
-----------------------------------

1. CRUDO ANTES QUE NADA. El correo entero se guarda en `bandeja` antes de
   intentar parsearlo. Es la regla de oro del proyecto —la misma que hace que
   `captura/` no importe `cerebro/`— y acá vale doble: si un banco cambia su
   plantilla, el correo ya está guardado y el parser se arregla después contra
   un dato que no se perdió. Un parser que falla sobre un correo guardado es
   una tarde de trabajo; sobre un correo que nunca se guardó, es un movimiento
   que no existió nunca.

2. NO SE MARCA NADA COMO LEÍDO. La sesión IMAP es readonly y todo se pide con
   BODY.PEEK. El reporte matinal define "leído" como "ya te lo conté a vos", y
   esta ingesta no le cuenta nada a nadie: si marcara, le robaría correos al
   reporte y Tiziano dejaría de enterarse de cosas que sí quiere ver.

3. CURSOR PROPIO. `consumos_estado`, no `correo_estado`. El reporte mira lo sin
   leer de 7 días; esto mira todo lo nuevo de unos remitentes concretos.
   Compartir puntero deja ciego al que avanza más lento — el fallo que el propio
   `correo.py` documenta haber tenido.

QUÉ NO HACE
-----------
No clasifica por categoría, no avisa por Telegram, no decide si un movimiento
es traspaso propio. Eso último lo hace `cerebro/bancos/propios.py` y se aplica
acá al vuelo, pero el registro de titulares se carga de la base: si está vacío,
los movimientos entran tal como los dejó el parser y nadie inventa nada.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

import cerebro.bancos as bancos
import config
import db.db as db
from cerebro.bancos.contrato import CorreoCrudo, ErrorDeParseo
from cerebro.bancos.categorias import CLAVES, Categorizador
from cerebro.bancos.propios import Propios

log = logging.getLogger("lucy.consumos")

# Último día en que se avisó de un banco mudo. El throttle vive en memoria a
# propósito: un aviso de más tras un redespliegue es barato, y una tabla nueva
# solo para no repetir una advertencia sería más maquinaria de la que el
# problema pide. Lo que NO puede pasar es avisar cada 15 minutos.
_ultimo_aviso: dict = {}

SERVIDOR = "imap.gmail.com"

# Tope de correos nuevos por cuenta y pasada. Con cinco bancos y pocas
# transacciones al día, un día normal trae unas decenas; el tope es un cinturón
# contra una ráfaga rara, no un límite operativo.
MAX_POR_PASADA = 200

_MESES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fecha_imap(d: date) -> str:
    return f"{d.day:02d}-{_MESES[d.month - 1]}-{d.year}"


def _texto(v: str | None) -> str:
    if not v:
        return ""
    return "".join(
        (p.decode(e or "utf-8", "replace") if isinstance(p, bytes) else p)
        for p, e in decode_header(v))


def _direccion(remitente: str) -> str:
    r = remitente or ""
    if "<" in r:
        r = r.split("<")[-1].split(">")[0]
    return r.strip().lower()


@dataclass
class Resumen:
    """Lo que pasó en una pasada. Alimenta el canario de cambio de formato.

    `vistos` vs `extraidos` es la señal: si un banco cambia su plantilla, los
    correos siguen llegando (vistos sube) pero dejan de producir movimientos
    (extraidos se queda en cero). Ese par, por banco, es lo que hay que vigilar.
    """
    vistos: int = 0
    guardados_crudos: int = 0
    extraidos: int = 0
    duplicados: int = 0
    fallos: list[str] = field(default_factory=list)
    por_banco_vistos: dict = field(default_factory=dict)
    por_banco_extraidos: dict = field(default_factory=dict)
    por_banco_duplicados: dict = field(default_factory=dict)

    def bancos_mudos(self) -> list[str]:
        """Bancos cuyos correos llegaron y no produjeron NINGÚN movimiento.

        Un duplicado cuenta como señal de vida: prueba que el parser funcionó —
        parseó, calculó la huella y coincidió con una que ya estaba. Contarlo
        como cero hacía gritar al canario justo cuando el banco funcionaba bien.
        Y el caso no es raro: cuando un buzón se renumera se recosecha todo, y
        entonces los cinco bancos avisarían a la vez de haber cambiado su
        plantilla el mismo día. Eso entrena a ignorar el aviso.
        """
        return [b for b, n in self.por_banco_vistos.items()
                if n > 0
                and self.por_banco_extraidos.get(b, 0) == 0
                and self.por_banco_duplicados.get(b, 0) == 0]


def _cosechar_sync(cuenta: dict, remitentes: list[str], desde_uid: int,
                   desde_fecha: date,
                   uidvalidity_previa: int | None = None
                   ) -> tuple[int, int, list[dict]]:
    """SÍNCRONO (en un hilo). Trae los correos nuevos de los remitentes dados.

    Devuelve (uidvalidity, uid_mas_alto_COSECHADO, correos). La sesión es
    readonly y todo se pide con BODY.PEEK: mirar no puede cambiar el buzón.

    El UIDVALIDITY se comprueba ACÁ DENTRO, antes de filtrar. Comprobarlo fuera
    llegaba tarde: la cosecha ya se había hecho con el puntero viejo, el
    `desde_uid = 0` de después no lo leía nadie, y el uidvalidity nuevo se
    persistía igual — así que la rama de reinicio no volvía a dispararse nunca
    y la cuenta quedaba ciega de forma permanente.
    """
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        uidvalidity = int(M.response("UIDVALIDITY")[1][0])
        if uidvalidity_previa is not None and uidvalidity_previa != uidvalidity:
            log.warning("UIDVALIDITY cambió en %s (%s → %s): los UID viejos ya "
                        "no significan nada, reinicio el cursor.",
                        cuenta.get("user"), uidvalidity_previa, uidvalidity)
            desde_uid = 0

        uids: set[int] = set()
        desde = _fecha_imap(desde_fecha)
        for rem in remitentes:
            try:
                typ, data = M.uid("search", None, "FROM", f'"{rem}"',
                                  "SINCE", desde)
            except Exception:
                log.warning("SEARCH falló para %s en %s", rem,
                            cuenta.get("user"), exc_info=True)
                continue
            if data and data[0]:
                uids.update(int(x) for x in data[0].split())

        nuevos = sorted(u for u in uids if u > desde_uid)
        if not nuevos:
            return uidvalidity, desde_uid, []

        # El tope es el UID más alto que se llega a COSECHAR, no el más alto que
        # existe. Con max(nuevos) el cursor saltaba por encima de los que caían
        # fuera de MAX_POR_PASADA: no se pedían, no entraban en bandeja, no
        # daban fallo, no contaban en el canario — y GREATEST lo hacía
        # permanente. Cincuenta consumos podían desaparecer sin rastro.
        #
        # Avanzar hasta lo cosechado sí es correcto aunque el parseo falle
        # después: ese correo YA está en bandeja y en el buzón, así que no se
        # pierde; reprocesarlo en bucle sí sería un problema.
        a_pedir = nuevos[:MAX_POR_PASADA]
        correos = []
        for uid in a_pedir:
            typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[])")
            if not d or not d[0]:
                continue
            correos.append({"uid": uid, "crudo": d[0][1]})
        if len(nuevos) > len(a_pedir):
            log.info("%s: quedan %s correos para la próxima pasada.",
                     cuenta.get("user"), len(nuevos) - len(a_pedir))
        return uidvalidity, a_pedir[-1], correos
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _a_correo_crudo(cuenta: str, uid: int, crudo: bytes) -> CorreoCrudo | None:
    """Bytes de un correo → CorreoCrudo, o None si no tiene cuerpo legible."""
    msg = email.message_from_bytes(crudo)
    plano = html = ""
    for parte in (msg.walk() if msg.is_multipart() else [msg]):
        if parte.get_content_maintype() != "text":
            continue
        try:
            d = (parte.get_payload(decode=True) or b"").decode(
                parte.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if parte.get_content_type() == "text/plain" and not plano:
            plano = d
        elif parte.get_content_type() == "text/html" and not html:
            html = d
    if not plano and not html:
        return None
    try:
        fc = parsedate_to_datetime(msg.get("Date")).replace(tzinfo=None)
    except Exception:
        fc = datetime.now()
    return CorreoCrudo(
        remitente=_direccion(_texto(msg.get("From"))),
        asunto=_texto(msg.get("Subject")).strip(),
        fecha_correo=fc, html=html, texto=plano, cuenta=cuenta, uid=str(uid))


async def _propios() -> Propios:
    """El registro de titulares de la casa, desde la base.

    Si la tabla no existe todavía o está vacía, devuelve un registro vacío: los
    movimientos entran tal como los dejó el parser. Preferimos un traspaso
    contado como gasto —que se corrige cargando la tabla— a inventar una
    reclasificación con datos que nadie puso.
    """
    try:
        filas = await db.listar_cuentas_propias()
    except Exception:
        log.info("Sin registro de cuentas propias; no se reclasifica nada.")
        return Propios()
    reg = Propios()
    for f in filas:
        try:
            reg.agregar(f["patron"] if isinstance(f, dict) else f[0])
        except ValueError as e:
            log.warning("Patrón inválido en cuentas_propias: %s", e)
    return reg


async def revisar() -> Resumen:
    """Una pasada completa: buzones → bandeja → movimientos.

    Devuelve el Resumen para que quien la llame decida si avisar (ver
    `Resumen.bancos_mudos`). Esta función no manda mensajes a nadie.
    """
    res = Resumen()
    if not config.CORREO_CUENTAS:
        return res

    remitentes = list(bancos.remitentes_registrados())
    if not remitentes:
        log.warning("Ningún parser de banco registrado; no hay a quién buscar.")
        return res

    registro = await _propios()
    try:
        cat = Categorizador(await db.categorias_aprendidas(), CLAVES)
    except Exception:
        # Sin tabla de aprendidas todavía: se sigue con la red de palabras
        # clave, que no depende de la base. Lo que la red no sepa entra sin
        # categoría y va a la cola del panel, que es de donde salen las
        # correcciones. No inventamos.
        cat = Categorizador(claves=CLAVES)

    for cuenta in config.CORREO_CUENTAS:
        user = cuenta.get("user", "?")
        estado = await db.leer_estado_consumos(user)
        desde_uid = estado["ultimo_uid"] if estado else 0
        desde_fecha = (estado["desde_fecha"] if estado
                       else date(2026, 9, 1))
        uidv_previa = estado.get("uidvalidity") if estado else None
        try:
            uidvalidity, tope, correos = await asyncio.to_thread(
                _cosechar_sync, cuenta, remitentes, desde_uid, desde_fecha,
                uidv_previa)
        except Exception as e:
            res.fallos.append(f"{user}: {type(e).__name__}: {e}")
            log.warning("No pude cosechar %s.", user, exc_info=True)
            continue

        # Si el buzón se renumeró, el cursor guardado no puede seguir mandando:
        # se le dice a la base que lo reemplace en vez de quedarse con el mayor.
        renumerado = bool(uidv_previa and uidv_previa != uidvalidity)

        for correo_bytes in correos:
            res.vistos += 1
            crudo = _a_correo_crudo(user, correo_bytes["uid"],
                                    correo_bytes["crudo"])
            if crudo is None:
                res.fallos.append(f"{user}#{correo_bytes['uid']}: sin cuerpo")
                continue

            # El conteo del canario va ANTES de buscar parser. Si un banco
            # cambia el ASUNTO (no la plantilla), buscar_parser deja de calzar y
            # el banco desaparecía de por_banco_vistos: bancos_mudos() devolvía
            # [] justo cuando había dejado de entenderse a ese banco.
            banco = crudo.remitente.split("@")[-1]
            res.por_banco_vistos[banco] = res.por_banco_vistos.get(banco, 0) + 1

            parser = bancos.buscar_parser(crudo.remitente, crudo.asunto)
            if parser is None:
                continue          # publicidad, encuestas, OTP: no son dinero

            # INVARIANTE 1: el crudo se guarda ANTES de parsear.
            #
            # tipo_entrada="banco" y NO "sistema". `tomar_pendientes` reclama
            # ("texto","audio","foto","sistema","email"), así que con "sistema"
            # cada correo bancario se habría convertido en un turno completo del
            # agente: el HTML entero a DeepSeek y un mensaje a Telegram por
            # correo, hasta MAX_POR_PASADA por pasada. Y peor: siendo "sistema",
            # el guardarraíl SOLO_A_MANO de agente.py solo bloquea `archivar` y
            # `preferencia` — `crear` y `editar` quedaban abiertas a texto que
            # escribe el banco. Esto es archivo, no encargo: nadie lo procesa.
            bandeja_id = await db.guardar_en_bandeja(
                tipo_entrada="banco", contenido_raw=crudo.html or crudo.texto,
                chat_id=config.CHAT_ID_DUENO, origen="banco")
            res.guardados_crudos += 1

            try:
                movs = parser(crudo)
            except ErrorDeParseo as e:
                res.fallos.append(f"{user}#{crudo.uid} [{crudo.asunto[:40]}]: {e}")
                continue

            for mov in movs:
                mov = registro.reclasificar(mov)
                # Solo los GASTOS se categorizan. El dinero que entra no se
                # clasifica —decisión de Tiziano— y adivinarle categoría a un
                # ingreso además sale mal: la contraparte de un ingreso es quien
                # paga, no un comercio, así que la red casaría el nombre del
                # banco o de una persona y lo llamaría gasto bancario.
                guardado = await db.guardar_movimiento(
                    mov, bandeja_id=bandeja_id,
                    categoria=(cat.categoria_de(mov.contraparte)
                               if mov.tipo == "gasto" else None))
                if guardado is None:
                    res.duplicados += 1
                    res.por_banco_duplicados[banco] = \
                        res.por_banco_duplicados.get(banco, 0) + 1
                else:
                    res.extraidos += 1
                    res.por_banco_extraidos[banco] = \
                        res.por_banco_extraidos.get(banco, 0) + 1

        await db.guardar_estado_consumos(user, uidvalidity, tope, desde_fecha,
                                         reiniciar=renumerado)

    log.info("Ingesta: %s vistos, %s movimientos, %s duplicados, %s fallos.",
             res.vistos, res.extraidos, res.duplicados, len(res.fallos))
    return res


async def avisar_si_hay_bancos_mudos(res: Resumen) -> int:
    """El canario. Deja un encargo si algún banco dejó de producir movimientos.

    Un banco mudo —correos que llegan pero no producen ni un movimiento— es la
    firma de que cambió su plantilla. Sin este aviso el sistema no se rompe: se
    queda callado, y un sistema de gastos callado se lee como "no gastaste
    nada". Esa es la peor manera de fallar que tiene este proyecto, y la que
    lleva toda la conversación intentando evitarse.

    Un aviso por banco y por día. Devuelve cuántos avisos dejó.
    """
    mudos = res.bancos_mudos()
    if not mudos:
        return 0
    hoy = datetime.now().date()
    avisados = 0
    for banco in mudos:
        if _ultimo_aviso.get(banco) == hoy:
            continue
        vistos = res.por_banco_vistos.get(banco, 0)
        # El fallo se formatea con la cuenta de GMAIL delante, no con el
        # dominio del banco, así que buscar el dominio no casaba nunca y el
        # aviso salía siempre sin el único dato accionable. Se busca en todo el
        # texto del fallo, que sí incluye el asunto y el mensaje del error.
        muestra = next((f for f in res.fallos
                        if banco.split(".")[0].lower() in f.lower()), "")
        if not muestra and res.fallos:
            muestra = res.fallos[0]
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=(
                f"AVISO: dejé de entender los correos de {banco}. Llegaron "
                f"{vistos} correos suyos en esta revisión y no salió ni un "
                f"movimiento — casi seguro cambiaron el formato del correo.\n\n"
                + (f"El error fue: {muestra}\n\n" if muestra else "")
                + "Decíselo a Tiziano en una línea, sin alarmar: los correos "
                "están guardados y no se perdió nada, pero sus gastos de ese "
                "banco NO están entrando a la base hasta que se arregle el "
                "parser. Es importante que lo sepa: un sistema de gastos que se "
                "queda callado parece decir que no gastó nada."),
            chat_id=config.CHAT_ID_DUENO, origen="banco")
        _ultimo_aviso[banco] = hoy
        avisados += 1
        log.warning("Canario: %s mudo (%s correos, 0 movimientos).",
                    banco, vistos)
    return avisados
