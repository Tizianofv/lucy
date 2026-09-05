"""Ingesta de movimientos bancarios: del buzón a la tabla `movimientos`.

Este es el módulo que hace que todo lo demás sirva. Los parsers de
`cerebro/bancos/` convierten correos en movimientos, pero hasta acá nadie los
llamaba: 461 movimientos parseados eran cero filas en la base.

TRES INVARIANTES QUE NO SE NEGOCIAN
-----------------------------------

1. EL CRUDO SE GUARDA ANTES DE PARSEAR — Y SOLO SI HAY PARSER. Cuando un
   correo calza con un parser, su cuerpo se guarda en `bandeja` ANTES de
   intentar parsearlo: si el banco cambió su plantilla, el parser se arregla
   después contra un dato que no se perdió.

   Lo que este invariante NO dice, porque el código no lo hace: un correo que
   no calza con ningún parser se DESCARTA. No entra en bandeja. Solo se cuenta
   (ver `Resumen.sin_ruta`) y su rastro queda en Gmail, sin marcar y sin
   borrar. Los adjuntos tampoco se guardan nunca: de un correo parseado queda
   el HTML o el texto, no el PDF.

   Esto estuvo declarado como "el correo entero se guarda antes de intentar
   parsearlo" mientras el código hacía otra cosa, y la alerta del canario
   repetía esa promesa a Tiziano ("están guardados, no se perdió nada"). Un
   invariante declarado y no cumplido es peor que no declarar ninguno: se le
   cree. Si algún día se quiere que sea cierto, hay que MOVER el guardado
   arriba del `if parser is None`, no reescribir esta línea.

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
from datetime import date, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime

import cerebro.bancos as bancos
import config
import db.db as db
from cerebro.bancos.contrato import CorreoCrudo, ErrorDeParseo
from cerebro.bancos.categorias import CLAVES, Categorizador
from cerebro.bancos.propios import Propios

log = logging.getLogger("lucy.consumos")

# Último día en que se avisó de cada cosa. La clave es (remitente, señal) para
# que las dos señales de un mismo remitente no se tapen entre ellas. El throttle
# vive en memoria a propósito: un aviso de más tras un redespliegue es barato, y
# una tabla nueva solo para no repetir una advertencia sería más maquinaria de
# la que el problema pide. Lo que NO puede pasar es avisar cada 15 minutos.
_ultimo_aviso: dict = {}

# ── El latido de la cosecha ──────────────────────────────────────────────
#
# Todas las señales del canario son sobre los BANCOS: hablan de correos que
# llegaron. Ninguna dice nada cuando no llega ninguno porque la maquinaria
# nuestra dejó de mirar — credenciales vencidas, IMAP caído, la búsqueda
# fallando, la pasada reventando entera. Eso se ve exactamente igual que "no
# gastaste nada", que es la peor manera de fallar que tiene este proyecto.
#
# Seis horas: la pasada corre cada ~15 minutos, así que seis horas son ~24
# pasadas seguidas sin una sola cosecha buena. Ningún hipo transitorio dura eso.
LATIDO_HORAS = 6

# Cuándo se cosechó bien por última vez, y desde cuándo está vivo este proceso.
# En memoria, igual que el throttle: un redespliegue reinicia la cuenta y a lo
# sumo retrasa el aviso seis horas. La alternativa era una columna nueva en
# producción, que es una decisión de Tiziano y no de este cambio.
_ultima_cosecha = None
_arranque = datetime.now()

SERVIDOR = "imap.gmail.com"

# Tope de correos nuevos por cuenta y pasada. Con cinco bancos y pocas
# transacciones al día, un día normal trae unas decenas; el tope es un cinturón
# contra una ráfaga rara, no un límite operativo.
MAX_POR_PASADA = 200

# Tope por adjunto. Los comprobantes del Popular pesan ~40 KB; 5 MB deja
# muchísimo margen y a la vez impide que un correo con un adjunto enorme se
# lleve la memoria del proceso, que es compartida con el bot.
MAX_ADJUNTO = 5 * 1024 * 1024

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


@dataclass(frozen=True)
class Fallo:
    """Un correo que no se pudo convertir en movimiento, CON su remitente.

    El remitente viaja aparte del texto a propósito. Antes `fallos` era una
    lista de strings y el aviso buscaba el banco por substring dentro de esa
    frase; cuando no casaba, agarraba `fallos[0]` — el primero de la lista, de
    cualquier banco. Pegar el error de otro banco dentro de una alarma es peor
    que no mostrar ninguno: manda a arreglar lo que no está roto.

    `remitente` va vacío cuando el fallo no es de un correo sino de la cuenta
    entera (no se pudo abrir el buzón, no se pudo cosechar).
    """
    remitente: str
    detalle: str

    def __str__(self) -> str:
        return self.detalle


@dataclass
class Resumen:
    """Lo que pasó en una pasada. Alimenta el canario.

    Los cuatro contadores van POR REMITENTE EXACTO, no por dominio. El dominio
    mezclaba cosas que no se parecen: de `popularenlinea.com` salen el buzón de
    notificaciones (puro movimiento) y el de marketing (130 publicidades al año
    y 8 cargos reales), y contarlos juntos hacía gritar al canario casi todos
    los días. El enrutado ya va por remitente exacto; el canario también.

      · `enrutados`  — correos suyos que calzaron con algún parser.
      · `producidos` — de esos, cuántos dieron al menos un movimiento. Un
                       duplicado cuenta: prueba que el parser corrió, calculó
                       la huella y coincidió con una que ya estaba.
      · `reventados` — de esos, cuántos levantaron ErrorDeParseo.
      · `sin_ruta`   — correos suyos que no calzaron con ningún parser.
      · `rechazados` — movimientos que el parser SÍ produjo y la base no
                       aceptó. Es distinto de `reventados`: acá el correo se
                       entendió entero y lo que falla es el acople con el
                       esquema. Ninguna suite hermética puede ver ese fallo —
                       los dobles de conexión aceptan lo que sea—, así que si
                       nadie lo cuenta acá, no lo cuenta nadie.
    """
    vistos: int = 0
    guardados_crudos: int = 0
    extraidos: int = 0
    duplicados: int = 0
    fallos: list = field(default_factory=list)          # list[Fallo]
    enrutados: dict = field(default_factory=dict)
    producidos: dict = field(default_factory=dict)
    reventados: dict = field(default_factory=dict)
    sin_ruta: dict = field(default_factory=dict)
    rechazados: dict = field(default_factory=dict)

    def remitentes_reventados(self) -> list[str]:
        """SEÑAL A: un parser calzó y no pudo leer el correo. Avisa SIEMPRE.

        No mira la clase del remitente ni cuántos otros correos suyos salieron
        bien. Un remitente que parsea diez y revienta en uno no es "mudo" —con
        la señal vieja nadie se enteraba nunca de ese uno—, y cada uno de esos
        es un movimiento que no está en la base.
        """
        return sorted(r for r, n in self.reventados.items() if n > 0)

    def remitentes_rechazados(self) -> list[str]:
        """SEÑAL C: el parser leyó el correo y la BASE no aceptó la fila.

        Avisa SIEMPRE, para cualquier remitente y sin importar cuántos otros
        movimientos suyos entraron bien. Es la señal más callada de las tres:
        el correo se entendió, el canario A no la ve (no hubo ErrorDeParseo) y
        el B tampoco (el remitente enrutó), así que sin esto el movimiento
        simplemente no está en la base y nada lo dice.
        """
        return sorted(r for r, n in self.rechazados.items() if n > 0)

    def remitentes_mudos(self) -> list[str]:
        """SEÑAL B: llegaron correos suyos y NINGUNO calzó con un parser.

        Es la firma de que cambió el asunto, o de que apareció un tipo de aviso
        que todavía no cubrimos. Solo avisa para los remitentes
        `transaccional`: en uno `mixto` esto es el día a día (ver
        CLASES_REMITENTE en cerebro/bancos/contrato.py).

        Pide `enrutados == 0`: si aunque sea uno calzó, al remitente se le
        sigue entendiendo, y lo que no calzó es la publicidad de siempre.
        """
        return sorted(r for r, n in self.sin_ruta.items()
                      if n > 0
                      and self.enrutados.get(r, 0) == 0
                      and bancos.clase_de_remitente(r) == "transaccional")

    def error_de(self, remitente: str) -> str:
        """El error de ESE remitente, o "" si no hay ninguno. Sin fallback."""
        return next((f.detalle for f in self.fallos
                     if f.remitente == remitente), "")


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
        busquedas_rotas = 0
        for rem in remitentes:
            try:
                typ, data = M.uid("search", None, "FROM", f'"{rem}"',
                                  "SINCE", desde)
            except Exception:
                busquedas_rotas += 1
                log.warning("SEARCH falló para %s en %s", rem,
                            cuenta.get("user"), exc_info=True)
                continue
            if data and data[0]:
                uids.update(int(x) for x in data[0].split())

        # Una búsqueda rota se salta, pero TODAS rotas no es "no llegó nada":
        # es un buzón que no se pudo consultar, y devolver cero correos lo
        # haría indistinguible de un día tranquilo — hasta para el latido.
        if remitentes and busquedas_rotas == len(remitentes):
            raise RuntimeError(
                f"las {busquedas_rotas} búsquedas fallaron en "
                f"{cuenta.get('user')}: el buzón no se pudo consultar")

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
    adjuntos: list[tuple[str, bytes]] = []
    for parte in (msg.walk() if msg.is_multipart() else [msg]):
        if parte.get_content_maintype() != "text":
            # Solo PDF, y solo hasta MAX_ADJUNTO. Bajar cualquier adjunto
            # metería en memoria las imágenes de firma y los banners que los
            # bancos mandan en cada correo, que no dicen nada y pesan.
            nombre = parte.get_filename() or ""
            if nombre.lower().endswith(".pdf"):
                try:
                    datos = parte.get_payload(decode=True) or b""
                except Exception:
                    datos = b""
                if 0 < len(datos) <= MAX_ADJUNTO:
                    adjuntos.append((nombre, datos))
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
        fecha_correo=fc, html=html, texto=plano, cuenta=cuenta, uid=str(uid),
        adjuntos=tuple(adjuntos))


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
    `Resumen.remitentes_reventados` y `Resumen.remitentes_mudos`). Esta
    función no manda mensajes a nadie.
    """
    global _ultima_cosecha
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
            res.fallos.append(Fallo("", f"{user}: {type(e).__name__}: {e}"))
            log.warning("No pude cosechar %s.", user, exc_info=True)
            continue

        # El latido se marca acá y no al final: lo que prueba que la
        # maquinaria funciona es haber podido ABRIR el buzón y buscar, no que
        # haya venido algo. Un día sin movimientos es normal; un día sin poder
        # mirar, no.
        _ultima_cosecha = datetime.now()

        # Si el buzón se renumeró, el cursor guardado no puede seguir mandando:
        # se le dice a la base que lo reemplace en vez de quedarse con el mayor.
        renumerado = bool(uidv_previa and uidv_previa != uidvalidity)

        for correo_bytes in correos:
            res.vistos += 1
            crudo = _a_correo_crudo(user, correo_bytes["uid"],
                                    correo_bytes["crudo"])
            if crudo is None:
                res.fallos.append(
                    Fallo("", f"{user}#{correo_bytes['uid']}: sin cuerpo"))
                continue

            # Los contadores del canario van por REMITENTE EXACTO y ANTES de
            # buscar parser. Si un banco cambia el ASUNTO (no la plantilla),
            # buscar_parser deja de calzar; contarlo solo cuando calza dejaría
            # al canario ciego justo cuando dejamos de entender a ese banco.
            rem = crudo.remitente

            parser = bancos.buscar_parser(crudo.remitente, crudo.asunto)
            if parser is None:
                # Publicidad, encuestas, OTP… o el asunto que cambió. Este
                # correo se DESCARTA: no entra en bandeja, solo se cuenta.
                res.sin_ruta[rem] = res.sin_ruta.get(rem, 0) + 1
                continue
            res.enrutados[rem] = res.enrutados.get(rem, 0) + 1

            # INVARIANTE 1: el crudo se guarda ANTES de parsear. Solo se
            # llega acá con un parser en la mano; el correo sin parser ya se
            # descartó arriba, contado y sin pasar por bandeja.
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
                res.reventados[rem] = res.reventados.get(rem, 0) + 1
                res.fallos.append(Fallo(
                    rem, f"{user}#{crudo.uid} [{crudo.asunto[:40]}]: {e}"))
                continue
            if movs:
                # Al menos un movimiento: el parser corrió entero. Cuenta como
                # señal de vida aunque después todos resulten duplicados.
                res.producidos[rem] = res.producidos.get(rem, 0) + 1

            for mov in movs:
                mov = registro.reclasificar(mov)
                # Solo los GASTOS se categorizan. El dinero que entra no se
                # clasifica —decisión de Tiziano— y adivinarle categoría a un
                # ingreso además sale mal: la contraparte de un ingreso es quien
                # paga, no un comercio, así que la red casaría el nombre del
                # banco o de una persona y lo llamaría gasto bancario.
                try:
                    guardado = await db.guardar_movimiento(
                        mov, bandeja_id=bandeja_id,
                        categoria=(cat.categoria_de(mov.contraparte)
                                   if mov.tipo == "gasto" else None))
                except db.MovimientoRechazado as e:
                    # SOLO se atrapa "esta fila está mal". Un fallo de conexión
                    # NO entra acá y sigue subiendo, que es lo correcto: si la
                    # base no responde hay que parar sin guardar el cursor, o el
                    # próximo `desde_uid` se saltaría correos que nunca se
                    # guardaron. La distinción la hace db.guardar_movimiento().
                    res.rechazados[rem] = res.rechazados.get(rem, 0) + 1
                    res.fallos.append(Fallo(
                        rem, f"{user}#{crudo.uid} [{crudo.asunto[:40]}]: {e}"))
                    log.error("La base rechazó un movimiento de %s: %s", rem, e)
                    continue
                if guardado is None:
                    res.duplicados += 1
                else:
                    res.extraidos += 1

        await db.guardar_estado_consumos(user, uidvalidity, tope, desde_fecha,
                                         reiniciar=renumerado)

    log.info("Ingesta: %s vistos, %s movimientos, %s duplicados, %s fallos.",
             res.vistos, res.extraidos, res.duplicados, len(res.fallos))
    return res


def _no_adivines(sintoma: str) -> str:
    """El bloque que cierra TODOS los avisos de este módulo, palabra por palabra.

    Nació de un aviso del 31-ago que conjeturó mal la causa y mandó a arreglar
    lo que no estaba roto. Lo único que cambia entre una alarma y otra es el
    SÍNTOMA que cada una detecta: pegarle a las tres el síntoma de la primera
    sería meter un dato falso dentro del párrafo que existe para no meter datos
    falsos. Con `sintoma="llegaron correos y no salió ningún movimiento"` el
    texto es idéntico, letra por letra, al que ya estaba.
    """
    return ("NO DIGAS POR QUÉ PASÓ. Esto detecta un síntoma —" + sintoma +
            "— y las causas posibles "
            "son varias: que el banco cambiara su plantilla, o que llegara "
            "un tipo de correo que el parser todavía no cubre, o un fallo "
            "en un adjunto. El 31-ago este mismo aviso dijo \"casi seguro "
            "cambiaron el formato\" y era falso: era un pago de cliente de "
            "un tipo nuevo, y el formato estaba intacto. Una conjetura "
            "dentro de una alarma se lee como un hecho y manda a arreglar "
            "lo que no está roto.")

# Lo único cierto que se puede decir del correo que no se pudo leer. La sesión
# de la ingesta es readonly y usa BODY.PEEK: mirar no cambia el buzón.
_SIGUE_EN_GMAIL = ("Los correos siguen en Gmail, sin marcar y sin borrar.")


async def avisar_si_hay_bancos_mudos(res: Resumen) -> int:
    """El canario. Deja un encargo por cada señal que se encendió.

    Tres señales, y son distintas — por eso son tres avisos y no uno:

      A · `reventados > 0` — un parser calzó con el correo y no pudo leerlo.
          Avisa SIEMPRE, para cualquier remitente. Antes esta señal estaba
          ahogada dentro de "el banco quedó mudo": un remitente que parsea diez
          y revienta en uno no está mudo, así que de ese uno no se enteraba
          nadie. Cada uno es un movimiento que no está en la base.

      B · `sin_ruta > 0` y `enrutados == 0` — llegaron correos suyos y ninguno
          calzó con un parser. Solo para remitentes `transaccional`.

      C · `rechazados > 0` — el parser leyó el correo y la BASE no aceptó la
          fila. Avisa SIEMPRE. Es la más callada de las tres: no hay
          ErrorDeParseo y el remitente enrutó, así que ni A ni B la ven. Sin
          este aviso el movimiento no está en la base y nada lo dice.

    Un aviso por remitente, por señal y por día. Devuelve cuántos avisos dejó.

    Esta función NO dice que no se perdió nada, porque no es verdad: el correo
    sin parser se descarta. Ver el INVARIANTE 1 arriba.
    """
    hoy = datetime.now().date()
    avisados = 0

    for rem in res.remitentes_reventados():
        if _ultimo_aviso.get((rem, "reventado")) == hoy:
            continue
        n = res.reventados.get(rem, 0)
        ok = res.producidos.get(rem, 0)
        error = res.error_de(rem)
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=(
                f"AVISO: {n} correo(s) de {rem} calzaron con su parser y no se "
                "pudieron leer en esta revisión"
                + (f" (otros {ok} sí produjeron movimiento)." if ok else ".")
                + "\n\n"
                + (f"El error fue: {error}\n\n" if error
                   else "No tengo el texto del error de ESTE remitente; el "
                        "detalle está en los logs.\n\n")
                + "Decíselo a Tiziano en una línea, sin alarmar: esos "
                "movimientos NO están entrando a la base hasta que se arregle "
                "el parser. El cuerpo de esos correos sí quedó guardado en la "
                "bandeja de Lucy —se guarda antes de parsear—, pero no sus "
                f"adjuntos. {_SIGUE_EN_GMAIL}\n\n"
                + _no_adivines("un correo calzó con su parser y el parser no "
                               "pudo leerlo")),
            chat_id=config.CHAT_ID_DUENO, origen="banco")
        _ultimo_aviso[(rem, "reventado")] = hoy
        avisados += 1
        log.warning("Canario: %s reventó en %s correos (%s con movimiento).",
                    rem, n, ok)

    for rem in res.remitentes_mudos():
        if _ultimo_aviso.get((rem, "sin_ruta")) == hoy:
            continue
        n = res.sin_ruta.get(rem, 0)
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=(
                f"AVISO: dejé de entender los correos de {rem}. Llegaron {n} "
                "correos suyos en esta revisión y ninguno calzó con un parser: "
                "no salió ni un movimiento.\n\n"
                "Decíselo a Tiziano en una línea, sin alarmar: sus movimientos "
                "NO están entrando a la base hasta que se arregle. Y decile la "
                "verdad de lo que pasó con esos correos: se descartaron. "
                f"{_SIGUE_EN_GMAIL} Lucy no guardó copia del cuerpo ni de los "
                "adjuntos, así que cuando esto se arregle hay que ir a "
                "buscarlos al buzón.\n\n"
                "Es importante que lo sepa: un sistema de gastos que se queda "
                "callado parece decir que no gastó nada.\n\n"
                + _no_adivines("llegaron correos y no salió ningún "
                               "movimiento")),
            chat_id=config.CHAT_ID_DUENO, origen="banco")
        _ultimo_aviso[(rem, "sin_ruta")] = hoy
        avisados += 1
        log.warning("Canario: %s mudo (%s correos, ninguno enrutado).", rem, n)

    for rem in res.remitentes_rechazados():
        if _ultimo_aviso.get((rem, "rechazado")) == hoy:
            continue
        n = res.rechazados.get(rem, 0)
        error = res.error_de(rem)
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=(
                f"AVISO: {n} movimiento(s) de {rem} se leyeron bien y la base "
                "NO los aceptó en esta revisión.\n\n"
                + (f"El error fue: {error}\n\n" if error
                   else "No tengo el texto del error de ESTE remitente; el "
                        "detalle está en los logs.\n\n")
                + "Decíselo a Tiziano en una línea, sin alarmar: esos "
                "movimientos NO están en la base y no van a entrar solos — el "
                "correo ya se dio por revisado, así que la próxima pasada no "
                "vuelve a intentarlo. El cuerpo de esos correos sí quedó "
                "guardado en la bandeja de Lucy —se guarda antes de parsear—, "
                f"pero no sus adjuntos. {_SIGUE_EN_GMAIL}\n\n"
                + _no_adivines("el parser leyó el correo y la base rechazó la "
                               "fila")),
            chat_id=config.CHAT_ID_DUENO, origen="banco")
        _ultimo_aviso[(rem, "rechazado")] = hoy
        avisados += 1
        log.warning("Canario: la base rechazó %s movimiento(s) de %s.", n, rem)

    return avisados


async def avisar_si_no_hay_latido() -> int:
    """El latido de la cosecha: ¿hace cuánto que no se puede mirar el buzón?

    El canario de arriba habla de los BANCOS y necesita que lleguen correos
    para decir algo. Este habla de NUESTRA maquinaria y no necesita ninguno:
    credenciales vencidas, IMAP caído, la búsqueda fallando o la pasada
    reventando entera producen exactamente el mismo silencio que un día sin
    gastos, y hasta hoy nada de eso avisaba.

    Un aviso por día. Devuelve 1 si avisó, 0 si no.
    """
    if not config.CORREO_CUENTAS or not list(bancos.remitentes_registrados()):
        return 0                      # nada que cosechar: no hay latido que pedir
    referencia = _ultima_cosecha or _arranque
    silencio = datetime.now() - referencia
    if silencio <= timedelta(hours=LATIDO_HORAS):
        return 0
    hoy = datetime.now().date()
    if _ultimo_aviso.get(("__latido__", "cosecha")) == hoy:
        return 0
    horas = int(silencio.total_seconds() // 3600)
    nunca = _ultima_cosecha is None
    await db.guardar_en_bandeja(
        tipo_entrada="sistema",
        contenido_raw=(
            f"AVISO: hace {horas} horas que no consigo revisar ningún buzón "
            "en busca de movimientos"
            + (" — desde que Lucy arrancó, ni una sola vez." if nunca else ".")
            + f" La revisión corre cada ~15 minutos, así que son unas "
            f"{horas * 4} pasadas seguidas sin poder cosechar.\n\n"
            "Decíselo a Tiziano en una línea, sin alarmar: mientras esto dure, "
            "el panel y el resumen de gastos están al día solo hasta esa hora. "
            "Un cero de estos días significa 'no miré', no 'no gastaste'. "
            f"{_SIGUE_EN_GMAIL}\n\n"
            + _no_adivines("hace horas que ninguna revisión de buzón "
                           "termina bien")),
        chat_id=config.CHAT_ID_DUENO, origen="banco")
    _ultimo_aviso[("__latido__", "cosecha")] = hoy
    log.warning("Latido: %s horas sin cosechar (nunca=%s).", horas, nunca)
    return 1
