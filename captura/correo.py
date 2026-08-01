"""Vigía de correo (Nivel 4, req 17): mira los buzones, filtra con criterio, y
deja en la bandeja SOLO lo que amerita la atención de Tiziano.

Una vez al día, en la mañana, Lucy junta el correo relevante del último día y
le deja UN reporte — no gotea avisos durante el día (pedido de Tiziano, 24-jul:
"con que lo haga una vez al día y me dé el reporte en la mañana estoy contento").

Dos filtros en cascada, para no malgastar IA ni exponer de más:
  1. Barato, por cabeceras: descarta el grueso (no-reply, listas, newsletters)
     sin gastar un token. En el correo real de Tiziano tumba ~80%.
  2. La IA juzga lo que sobrevive: ¿esto merece que lo interrumpan? Solo lo que
     pasa acá entra al reporte.

Arranca desde AHORA. La primera vez de cada cuenta guarda el UID más alto SIN
procesar nada: el backlog histórico (decenas de miles) no se toca. Revisar es
mirar lo que llega, no releer el pasado.

imaplib es síncrono; toda la sesión IMAP corre en un hilo (asyncio.to_thread)
para no congelar el bucle del agente mientras habla con Gmail.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
import unicodedata
from datetime import datetime, timedelta
from email.header import decode_header

import cerebro.deepseek as motor
import config
import db.db as db
from config import TZ

log = logging.getLogger("lucy.correo")

SERVIDOR = "imap.gmail.com"

# La ventana matinal en que sale el reporte, igual que el briefing. Si Lucy
# estuvo caída toda la mañana, el correo de ayer a las 4 PM ya no es un
# "reporte matinal": espera al de mañana.
REPORTE_DESDE = 7   # 7 AM
REPORTE_HASTA = 12  # mediodía

# Tope de correos nuevos a mirar por cuenta y día. Un día normal trae pocos que
# pasen el filtro barato; el tope es un cinturón contra una ráfaga rara.
MAX_POR_DIA = 60

# Hasta dónde mira hacia atrás el reporte. Lo eligió Tiziano el 26-jul: 7 días.
# Lo anterior queda como pasado — en la cuenta del estudio hay ~2.900 sin leer
# acumulados, y arrastrarlos sería empezar la relación con una deuda imposible.
VENTANA_DIAS = 7

# ═══ La política de correo, definida con Tiziano el 26-jul-2026 ═══════════
#
# El filtro viejo era binario (relevante sí/no) y tenía un defecto que él marcó
# enseguida: lo "no relevante" desaparecía en silencio. Su regla: aunque Lucy
# juzgue que algo no vale la pena, IGUAL tiene que decir que llegó. Por eso
# esto ya no filtra: CLASIFICA. Nada baja de "mención".
#
# Tres niveles de decisión: ámbito (su vida personal o su trabajo), área (cuál
# de sus frentes) y —lo que decide cuánto detalle— qué pide de él.

AMBITOS = ("personal", "laboral")

# Las áreas de su mundo, tal como él las ordenó. No son etiquetas inventadas:
# son sus dos frentes de negocio (CDS el estudio, ACD la academia), la docencia
# que da en otros centros, lo transversal a los dos, y su vida personal.
AREAS = (
    # Personal
    "rosi_familia", "tramites_servicios", "dinero_personal", "compras_publicidad",
    # Laboral · CDS (Caribbean Dream Studios)
    "cds_clientes", "cds_pasantes", "cds_proveedores", "cds_dinero",
    # Laboral · ACD (Academia Caribbean Dream)
    "acd_estudiantes", "acd_administracion", "acd_pasantes", "acd_dinero",
    # Laboral · docencia en otros centros (ITLA, UNPHU)
    "docencia_externa",
    # Laboral · transversal a los dos frentes
    "infraestructura", "oficio_conocimiento", "publicidad_laboral",
)

NIVELES = ("911", "accion", "enterarte", "mencion", "dudoso")

SISTEMA_CLASIFICA = """\
Sos el clasificador de correo de Lucy, la asistente personal de Tiziano
Fajardo. Recibís el remitente y el asunto de UN correo y lo clasificás. NO
filtrás: todo correo se clasifica, hasta la publicidad. Nada se descarta.

QUIÉN ES TIZIANO: ingeniero de mezcla, master y productor musical. Dueño de
dos frentes de negocio en República Dominicana — CDS (Caribbean Dream Studios,
estudio de grabación) y ACD (Academia Caribbean Dream). Además da clases en
otros centros (ITLA, UNPHU). Rosi es su pareja y maneja la parte
administrativa de ACD.

Devolvés SOLO un JSON con estas claves:
  ambito: "personal" | "laboral"
  area: una de estas, la que mejor calce:
    PERSONALES:
      rosi_familia ......... Rosi o la familia, en lo personal
      tramites_servicios ... trámites suyos: peajes (Paso Rápido), bancos, seguros
      dinero_personal ...... facturas y pagos de su casa/vida
      compras_publicidad ... compras y promociones personales (Amazon, Pinterest)
    LABORALES · CDS (el estudio):
      cds_clientes ......... clientes, sesiones, reservas del estudio
      cds_pasantes ......... CVs y pasantías para el estudio
      cds_proveedores ...... proveedores, equipos, servicios del estudio
      cds_dinero ........... facturas, comprobantes y pagos DEL ESTUDIO
    LABORALES · ACD (la academia):
      acd_estudiantes ...... estudiantes y docencia de la academia
      acd_administracion ... administración interna de ACD (él y Rosi)
      acd_pasantes ......... CVs y pasantías para la academia
      acd_dinero ........... facturas, comprobantes y pagos DE LA ACADEMIA
    LABORALES · otros:
      docencia_externa ..... ITLA, UNPHU y otros centros donde él da clases
      infraestructura ...... Railway, n8n, Google Cloud, UptimeRobot, dominios,
                             hosting, seguridad de sus sistemas
      oficio_conocimiento .. su oficio: audio, plugins, técnicas, marcas
                             (Audient, Kazrog, Krotos), tecnología que usa
      publicidad_laboral ... promociones de herramientas de trabajo (Canva, Fiverr)
  nivel: "911" | "accion" | "enterarte" | "mencion"
  asunto_corto: de qué va, en menos de 12 palabras, en español
  motivo: por qué le pusiste ese nivel, en pocas palabras

LOS NIVELES — esto decide cuánto se le cuenta:
  911 ....... SOLO infraestructura rota o comprometida: un deploy que falló,
              un servicio caído, una vulnerabilidad crítica. Ahí viven Natalia
              y Lucy, y esperar hasta la mañana empeora la cosa. NADA MÁS es
              911: lo urgente de clientes le llega por WhatsApp, no por correo.
  accion .... alguien real espera algo suyo, o hay algo que hacer.
              ⚠️ TODA FACTURA, comprobante fiscal o cobro es SIEMPRE "accion",
              nunca menos: todas vencen y hay que archivarlas.
  enterarte . nada que hacer, pero le sirve saberlo: novedades de su oficio
              (audio, plugins, técnicas), avisos de seguridad no críticos,
              noticias de las herramientas que usa.
  mencion ... llegó pero no aporta: publicidad, encuestas automáticas,
              newsletters comerciales sin valor profesional.

CRITERIOS FINOS:
· Un boletín de una marca de AUDIO (Audient, Krotos, Waves…) que enseña algo
  del oficio es "enterarte". Si es solo una OFERTA de precio, es "mencion".
· Railway/n8n avisando que algo FALLÓ o de una vulnerabilidad crítica = 911.
  El mismo remitente mandando su newsletter de producto = "enterarte", y un
  reporte rutinario de que todo está bien = "mencion".
· Un CV o una pasantía puede ser de CDS o de ACD: elegí por el contenido. Si
  de verdad no se puede saber, poné el área que te parezca más probable y
  decilo en "motivo".
· Rosi aparece en los dos lados: lo decide el CONTENIDO, no el remitente.
  "¿Compramos algo para la casa?" es rosi_familia; "los pagos de los
  estudiantes" es acd_administracion.
· Ante la duda en el NIVEL, subí un escalón: es peor que se le pase una
  factura que darle una línea de más.
· Un REBOTE (Mail Delivery Subsystem, mailer-daemon, "Delivery Status
  Notification: Failure") es "accion": algo que ÉL mandó no llegó a destino.
· Alertas de su BANCO sobre movimientos o cambios en su cuenta: "accion" si
  es un movimiento o un cambio de datos; "mencion" si es publicidad del banco.
· Alguien pidiendo COTIZACIÓN, alquiler del estudio, o queriendo grabar =
  cds_clientes y "accion", siempre: es plata entrando por la puerta.\
"""


def _texto(v: str | None) -> str:
    """Cabecera MIME (=?utf-8?...) → texto legible."""
    if not v:
        return ""
    return "".join(
        (p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p)
        for p, enc in decode_header(v)
    )


def _es_ruido(msg) -> str | None:
    """Motivo del descarte barato, o None si merece la mirada de la IA."""
    frm = (_texto(msg.get("From")) or "").lower()
    # OJO con los rebotes: mailer-daemon NO es ruido. Un "Delivery Status
    # Notification (Failure)" significa que algo que Tiziano mandó NO llegó, y
    # eso amerita saberlo. Estaba en esta lista y se descartaba solo; salió a
    # la luz en el ensayo del 26-jul con dos rebotes reales en su buzón.
    if any(x in frm for x in ("no-reply", "noreply", "no_reply", "donotreply",
                              "notifications@", "notification@",
                              "newsletter")):
        return "remitente automático"
    if msg.get("List-Unsubscribe") or msg.get("List-Id") or msg.get("List-Post"):
        return "lista/newsletter"
    if (msg.get("Precedence") or "").lower() in ("bulk", "list", "junk"):
        return "precedence bulk"
    if (msg.get("Auto-Submitted") or "").lower().startswith("auto"):
        return "auto-generado"
    return None


def _snippet(msg, limite: int = 400) -> str:
    """Un fragmento de texto plano del cuerpo, para darle contexto al agente."""
    try:
        if msg.is_multipart():
            for parte in msg.walk():
                if parte.get_content_type() == "text/plain":
                    cuerpo = parte.get_payload(decode=True) or b""
                    break
            else:
                cuerpo = b""
        else:
            cuerpo = msg.get_payload(decode=True) or b""
        texto = cuerpo.decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""
    # Fuera las URLs: un correo de Pinterest es media pantalla de link firmado
    # que no le dice nada a nadie y se come el presupuesto de contexto.
    import re as _re
    texto = _re.sub(r"https?://\S+", " ", texto)
    return " ".join(texto.split())[:limite]


def _sin_leer_sync(cuenta: dict, dias: int, limite: int) -> list[dict]:
    """SÍNCRONO (en un hilo). Los correos SIN LEER de los últimos `dias`.

    Este reemplaza al puntero como fuente del reporte, y es el arreglo de fondo
    del sistema: el puntero SE CONSUMÍA —cualquier revisión lo adelantaba— así
    que el reporte de la mañana podía encontrar cero y quedarse mudo mientras
    había correos pendientes de verdad. "Sin leer" no se consume solo, coincide
    con lo que Tiziano ve en su Gmail, y sobrevive a que Lucy esté caída.
    """
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        desde = _fecha_imap(datetime.now(TZ) - timedelta(days=dias))
        typ, data = M.uid("search", None, "UNSEEN", "SINCE", desde)
        uids = data[0].split() if data and data[0] else []
        # Los más nuevos primero: si hay que cortar por el tope, que se caiga
        # lo viejo, no lo de hoy.
        uids = uids[-limite:][::-1]
        salida = []
        for uid in uids:
            d = M.uid("fetch", uid.decode(), "(BODY.PEEK[])")[1]
            if not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            salida.append({
                "cuenta": cuenta["user"],
                "uid": int(uid),
                "from": _texto(msg.get("From")),
                "subject": _texto(msg.get("Subject")),
                "fecha": _texto(msg.get("Date")),
                "snippet": _snippet(msg),
                "ruido_barato": _es_ruido(msg),  # se informa igual, pero baja solo
            })
        return salida
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _cosechar(cuenta: dict, desde_uid: int) -> tuple[int, int, list[dict]]:
    """SÍNCRONO (corre en un hilo). Conecta, lee lo nuevo, filtra barato.

    Devuelve (uidvalidity, uid_mas_alto_visto, candidatos). Los candidatos son
    los que pasaron el filtro barato; el juicio de la IA se hace afuera, en el
    mundo async, para no mezclar el IMAP bloqueante con las llamadas al modelo.
    """
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        uidvalidity = int(M.response("UIDVALIDITY")[1][0])

        # UID > desde_uid = solo lo que llegó después del puntero.
        typ, data = M.uid("search", None, f"UID {desde_uid + 1}:*")
        uids = [int(x) for x in data[0].split()]
        # Gmail devuelve al menos el último aunque no supere el puntero: filtramos.
        uids = sorted(u for u in uids if u > desde_uid)
        if not uids:
            return uidvalidity, desde_uid, []

        top = max(uids)
        candidatos = []
        for uid in uids[:MAX_POR_DIA]:
            typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[])")
            if not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            motivo = _es_ruido(msg)
            if motivo:
                continue  # descartado barato, ni se menciona
            candidatos.append({
                "uid": uid,
                "from": _texto(msg.get("From")),
                "subject": _texto(msg.get("Subject")),
                "snippet": _snippet(msg),
            })
        # Avanzamos el puntero al tope VISTO, no solo al procesado: el correo
        # vive en Gmail, no en la bandeja. Si la IA falla en juzgar uno, no se
        # "pierde" (sigue en el buzón); reprocesarlo en bucle sí sería un
        # problema. Mirar hacia adelante gana.
        return uidvalidity, top, candidatos
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def clasificar(cand: dict, reglas: str = "") -> dict:
    """Clasifica UN correo: ámbito, área y nivel. From+Subject alcanza, y expone
    mucho menos que mandar el cuerpo entero a la IA.

    `reglas` son las preferencias que Tiziano le fue enseñando ("lo de Kazrog
    es ruido", "cualquier cosa de UNPHU avisame completo"). Van arriba del
    prompt para que pesen más que el criterio general: la clasificación se
    afina con el uso, sin que haya que tocar código cada vez.

    Nunca devuelve "descartado": lo peor que le puede pasar a un correo es
    quedar en 'mencion', y si algo sale raro cae en 'dudoso' — que también se
    informa. Nada desaparece en silencio.
    """
    sistema = SISTEMA_CLASIFICA
    if reglas:
        sistema += ("\n\nREGLAS QUE TE DIO TIZIANO (mandan sobre lo de arriba):\n"
                    + reglas)
    try:
        r = await motor.cliente.chat.completions.create(
            model=motor.MODELO,
            messages=[
                {"role": "system", "content": sistema},
                {"role": "user",
                 "content": f"De: {cand['from']}\nAsunto: {cand['subject']}"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        v = json.loads(r.choices[0].message.content)
    except Exception:
        # Si la IA no puede juzgar, el correo NO se pierde: se informa como
        # dudoso y Tiziano decide. Callarse por un fallo propio sería
        # exactamente lo que esta política vino a prohibir.
        log.warning("No pude clasificar «%s»; va como dudoso.",
                    cand.get("subject", "")[:60], exc_info=True)
        return {"ambito": "", "area": "", "nivel": "dudoso",
                "asunto_corto": cand.get("subject", "")[:80],
                "motivo": "no pude clasificarlo"}

    nivel = str(v.get("nivel", "")).strip().lower()
    v["nivel"] = nivel if nivel in NIVELES else "dudoso"
    v["ambito"] = str(v.get("ambito", "")).strip().lower()
    area = str(v.get("area", "")).strip().lower()
    v["area"] = area if area in AREAS else ""
    v["asunto_corto"] = str(v.get("asunto_corto") or cand.get("subject", ""))[:120]
    v["motivo"] = str(v.get("motivo") or "")[:160]
    return v


# Meses en inglés para el formato de fecha de IMAP (SINCE 27-Apr-2026), que no
# depende del locale del contenedor.
_MESES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fecha_imap(dt: datetime) -> str:
    return f"{dt.day:02d}-{_MESES[dt.month - 1]}-{dt.year}"


def _sin_acentos(v: str) -> str:
    """'Paso Rápido' → 'Paso Rapido'. Descompone y tira los diacríticos."""
    return "".join(c for c in unicodedata.normalize("NFD", v)
                   if not unicodedata.combining(c))


def _buscar_uids(M, criterios: list[str]) -> list[bytes]:
    """Corre el SEARCH tolerando acentos. Devuelve los uids (sin repetir).

    Esto existe por un fallo real (26-jul): Tiziano preguntó por "Paso Rápido"
    y la búsqueda entera se cayó con UnicodeEncodeError — el comando IMAP viaja
    en ASCII y la á no cabe. Buscar SOLO sin acentos tampoco alcanza: si la
    cabecera dice "Rápido" de verdad, el servidor no la matchea contra "Rapido".
    Así que se hace las dos búsquedas y se unen:
      1. sin acentos, en ASCII → siempre corre, y pega cuando el remitente
         escribe su nombre sin tilde (el caso de Paso Rapido);
      2. el término tal cual, declarando CHARSET UTF-8 → pega cuando la
         cabecera sí trae el acento. Va en try porque no todo servidor lo
         acepta, y un rechazo acá no puede tumbar la búsqueda.
    """
    vistos: list[bytes] = []

    def _sumar(data) -> None:
        if data and data[0]:
            for u in data[0].split():
                if u not in vistos:
                    vistos.append(u)

    ascii_crit = [_sin_acentos(c) for c in criterios]
    try:
        _sumar(M.uid("search", None, *ascii_crit)[1])
    except Exception:
        log.warning("SEARCH ASCII falló con %s", ascii_crit, exc_info=True)

    if any(c != a for c, a in zip(criterios, ascii_crit)):
        try:
            _sumar(M.uid("search", "CHARSET", "UTF-8",
                         *[c.encode("utf-8") for c in criterios])[1])
        except Exception:
            # El servidor no quiso el UTF-8: nos quedamos con lo del ASCII.
            log.info("SEARCH UTF-8 no aceptado; sigo con el resultado ASCII.")

    return sorted(vistos, key=lambda u: int(u))


def _buscar_sync(cuenta: dict, criterios: list[str], limite: int) -> list[dict]:
    """SÍNCRONO (en un hilo). IMAP SEARCH en el buzón, devuelve coincidencias.

    Trae también el uid (para poder LEER el cuerpo después) y si está sin leer
    —eso es lo que Tiziano suele buscar: lo viejo que quedó pendiente—.
    """
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        # Los valores ya vienen entre comillas para tolerar espacios
        # ("Jorge Taveras"); los acentos los maneja _buscar_uids.
        uids = _buscar_uids(M, criterios)[-limite:][::-1]
        salida = []
        for uid in uids:
            # FLAGS junto con las cabeceras: el preámbulo de la respuesta trae
            # los flags, y ahí miramos si \Seen está o no (sin leer = no está).
            d = M.uid("fetch", uid.decode(),
                      "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")[1]
            if not d or not d[0]:
                continue
            preambulo = d[0][0] if isinstance(d[0][0], bytes) else b""
            msg = email.message_from_bytes(d[0][1])
            salida.append({
                "cuenta": cuenta["user"],
                "uid": uid.decode(),
                "no_leido": b"\\Seen" not in preambulo,
                "de": _texto(msg.get("From")),
                "asunto": _texto(msg.get("Subject")),
                "fecha": _texto(msg.get("Date")),
            })
        return salida
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def buscar(de: str = "", asunto: str = "", texto: str = "",
                 dias: int = 90, solo_no_leidos: bool = False,
                 limite: int = 15) -> list[dict]:
    """Busca en los buzones por remitente/asunto/texto. Para "¿Juan escribió?".

    Mira el HISTORIAL, no solo lo nuevo: la ventana por defecto son los últimos
    90 días (`dias`), que es donde suele estar "el correo viejo sin leer" que
    Tiziano no alcanzó a ver. Con `solo_no_leidos` se limita a los pendientes.
    Devuelve cabeceras + uid + si está sin leer (el cuerpo se pide aparte con
    leer()). Ordena del más nuevo al más viejo.
    """
    # Entre comillas para que un valor con espacios sea UN término IMAP; sin
    # comillas internas, que romperían el comando.
    def _q(v: str) -> str:
        return '"' + v.strip().replace('"', "") + '"'

    criterios: list[str] = []
    if de:
        criterios += ["FROM", _q(de)]
    if asunto:
        criterios += ["SUBJECT", _q(asunto)]
    if texto:
        criterios += ["TEXT", _q(texto)]
    if not criterios:
        return []
    if solo_no_leidos:
        criterios.append("UNSEEN")
    if dias and dias > 0:
        desde = datetime.now(TZ) - timedelta(days=dias)
        criterios += ["SINCE", _fecha_imap(desde)]

    resultados: list[dict] = []
    fallos: list[str] = []
    for cuenta in config.CORREO_CUENTAS:
        try:
            resultados += await asyncio.to_thread(
                _buscar_sync, cuenta, criterios, limite)
        except Exception as e:
            fallos.append(f"{cuenta.get('user', '?')} ({type(e).__name__}: {e})")
            log.warning("Falló la búsqueda en %s.", cuenta.get("user", "?"),
                        exc_info=True)

    # Un fallo NO puede volver como lista vacía. Es la lección más cara de esta
    # función: cuando el acento de "Paso Rápido" rompía el IMAP, el error se
    # tragaba acá, buscar() devolvía [] y Lucy le dijo a Tiziano "no encontré
    # ningún correo" — una respuesta tranquila y falsa sobre correos que SÍ
    # estaban. Vacío y roto se ven igual desde afuera, así que hay que
    # distinguirlos acá: si nadie pudo mirar, esto revienta y el agente recibe
    # un ERROR (que sabe contar como "no pude mirar"), no un "no hay nada".
    if fallos and not resultados:
        raise RuntimeError(
            "no pude buscar en el correo — " + "; ".join(fallos) +
            ". OJO: esto NO significa que no haya correos, significa que la "
            "búsqueda falló. Decíselo así a Tiziano.")
    if fallos:
        log.warning("Búsqueda PARCIAL: falló %s; devuelvo lo de las demás.",
                    ", ".join(fallos))
    return resultados


def _cuerpo(msg, limite: int = 3000) -> str:
    """El texto plano del cuerpo, más largo que el snippet, para LEER el correo.

    Prefiere text/plain; si solo hay HTML, lo desnuda a lo bruto (quita etiquetas)
    para que quede legible sin traer una librería nueva. No es perfecto, pero
    alcanza para 'qué dice el correo'.
    """
    plano, html = "", ""
    try:
        partes = msg.walk() if msg.is_multipart() else [msg]
        for parte in partes:
            ct = parte.get_content_type()
            if ct not in ("text/plain", "text/html"):
                continue
            crudo = parte.get_payload(decode=True) or b""
            txt = crudo.decode(parte.get_content_charset() or "utf-8", "replace")
            if ct == "text/plain" and not plano:
                plano = txt
            elif ct == "text/html" and not html:
                html = txt
    except Exception:
        return ""
    cuerpo = plano
    if not cuerpo and html:
        import re
        sin_tags = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        sin_tags = re.sub(r"(?s)<[^>]+>", " ", sin_tags)
        cuerpo = sin_tags
    return " ".join(cuerpo.split())[:limite]


def _leer_sync(cuenta: dict, uid: str) -> dict | None:
    """SÍNCRONO (en un hilo). Trae el cuerpo completo de UN correo por su uid."""
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)  # readonly = leerlo NO lo marca leído
        d = M.uid("fetch", str(uid), "(BODY.PEEK[])")[1]
        if not d or not d[0]:
            return None
        msg = email.message_from_bytes(d[0][1])
        return {
            "cuenta": cuenta["user"],
            "de": _texto(msg.get("From")),
            "asunto": _texto(msg.get("Subject")),
            "fecha": _texto(msg.get("Date")),
            "cuerpo": _cuerpo(msg),
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def leer(cuenta: str, uid: str) -> dict | None:
    """Lee el cuerpo de un correo concreto (el que Tiziano señaló de una lista).

    `cuenta` y `uid` salen de un buscar() previo. Leerlo por Lucy NO lo marca
    como leído en Gmail (la sesión IMAP es de solo lectura): si él quiere, lo
    abre él mismo.
    """
    cta = next((c for c in config.CORREO_CUENTAS if c["user"] == cuenta), None)
    if cta is None:
        return None
    try:
        return await asyncio.to_thread(_leer_sync, cta, uid)
    except Exception:
        log.warning("No pude leer el correo uid=%s de %s.", uid, cuenta,
                    exc_info=True)
        return None


async def revisar_ahora() -> list[dict]:
    """Revisión on-demand: lo mismo que el reporte matinal, pero cuando Tiziano
    lo pide ("revisá si llegó algo"). Devuelve los relevantes para que el agente
    los resuma en su respuesta; NO deja encargo ni marca el reporte del día."""
    relevantes: list[dict] = []
    fallos: list[str] = []
    for cuenta in config.CORREO_CUENTAS:
        try:
            relevantes += await _relevantes_de(cuenta, None)  # None = no marca reporte
        except Exception as e:
            fallos.append(f"{cuenta.get('user', '?')} ({type(e).__name__}: {e})")
            log.warning("Falló la revisión de %s.", cuenta.get("user", "?"),
                        exc_info=True)
    # Mismo principio que en buscar(): si no se pudo mirar, no se dice "no llegó
    # nada". Un buzón que no se pudo abrir no es un buzón vacío.
    if fallos and not relevantes:
        raise RuntimeError(
            "no pude revisar el correo — " + "; ".join(fallos) +
            ". NO es que no haya llegado nada: la revisión falló.")
    return relevantes


async def _pendientes_de(cuenta: dict, reglas: str = "") -> list[dict]:
    """Los correos SIN LEER de esta cuenta que todavía no se informaron, ya
    clasificados. Es la materia prima del reporte.

    No toca el puntero ni marca nada: solo mira. Lo que se informa y lo que se
    marca leído se decide después, cuando el reporte de verdad haya salido.
    """
    crudos = await asyncio.to_thread(
        _sin_leer_sync, cuenta, VENTANA_DIAS, MAX_POR_DIA)
    if not crudos:
        return []

    ya = await db.correos_ya_reportados(
        cuenta["user"], [c["uid"] for c in crudos])
    nuevos = [c for c in crudos if c["uid"] not in ya]
    if not nuevos:
        return []

    for c in nuevos:
        # El filtro barato ya no descarta: solo ahorra una llamada a la IA en lo
        # que es ruido evidente. El correo se informa igual, como mención — que
        # es justo la regla que puso Tiziano: nada desaparece en silencio.
        if c.get("ruido_barato"):
            c["clasificacion"] = {
                "ambito": "", "area": "publicidad_laboral", "nivel": "mencion",
                "asunto_corto": c["subject"][:120],
                "motivo": f"filtro: {c['ruido_barato']}",
            }
        else:
            c["clasificacion"] = await clasificar(c, reglas)
    return nuevos


def _linea(c: dict) -> str:
    """Una línea de correo para el encargo, con lo justo según su nivel."""
    cl = c["clasificacion"]
    base = f"[{cl['nivel']}|{cl.get('area') or '?'}] De {c['from']} → {cl['asunto_corto']}"
    if cl["nivel"] in ("911", "accion", "dudoso"):
        # Los que piden algo van con extracto: el agente necesita el contenido
        # para decir qué requiere y proponer la tarea.
        return f"{base}\n    (cuenta {c['cuenta']} · uid {c['uid']})\n    {c['snippet'][:280]}"
    return f"{base}  (uid {c['uid']})"


def _nombre(remitente: str) -> str:
    """'Kazrog <list@kazrog.com>' → 'Kazrog'. Para agrupar las menciones."""
    r = (remitente or "").strip()
    if "<" in r:
        r = r.split("<")[0].strip().strip('"')
    if not r and "@" in (remitente or ""):
        r = remitente.split("@")[-1].strip("> ")
    return r or "(sin remitente)"


def _encargo(pendientes: list[dict]) -> str:
    """El encargo que se le deja al agente para que redacte el reporte.

    Igual que el briefing: acá se juntan y clasifican los datos, y el AGENTE
    los convierte en un mensaje humano. Lo que este texto sí fija es la
    política que Tiziano definió: qué nivel lleva cuánto detalle, y que nada
    —ni la publicidad— puede quedar sin mencionarse.
    """
    orden = {"911": 0, "accion": 1, "dudoso": 2, "enterarte": 3, "mencion": 4}
    pendientes = sorted(pendientes, key=lambda c: orden.get(
        c["clasificacion"]["nivel"], 9))

    # Las menciones van AGRUPADAS por remitente y contadas. Cumplen la regla de
    # Tiziano (aparecen, él se entera de que llegaron) sin convertir el reporte
    # en un muro: en el ensayo eran 69 líneas de Pinterest, ofertas y newsletters.
    detalle = [c for c in pendientes if c["clasificacion"]["nivel"] != "mencion"]
    menciones = [c for c in pendientes if c["clasificacion"]["nivel"] == "mencion"]
    lineas = "\n".join(_linea(c) for c in detalle)
    if menciones:
        cuenta_por: dict[str, int] = {}
        for c in menciones:
            n = _nombre(c["from"])
            cuenta_por[n] = cuenta_por.get(n, 0) + 1
        top = sorted(cuenta_por.items(), key=lambda x: -x[1])
        listado = ", ".join(f"{n} ({k})" if k > 1 else n for n, k in top[:25])
        if len(top) > 25:
            listado += f", y {len(top) - 25} remitentes más"
        lineas += (f"\n\n[mencion] {len(menciones)} correos sin importancia, "
                   f"de: {listado}")
    return (
        "Reporte de correo de la mañana. Estos son los correos SIN LEER que "
        "todavía no le informaste a Tiziano, ya clasificados por vos misma "
        f"(nivel|área):\n\n{lineas}\n\n"
        "Armá UN mensaje, en este orden y con este detalle — es la política que "
        "él definió:\n"
        "· ACCION (y 911): lo primero. De quién, qué pide y para cuándo. Creale "
        "la tarea cuando esté claro y decíselo. Las FACTURAS siempre van acá: "
        "toda factura vence y hay que archivarla.\n"
        "· DUDOSO: mostráselos aparte, con remitente y asunto, y que él decida.\n"
        "· ENTERARTE: una o dos líneas cada uno, de qué va. Son cosas de su "
        "oficio (audio, plugins, técnica) o avisos de sus sistemas.\n"
        "· MENCION: SOLO los nombres de quién escribió, juntos en una línea, "
        "sin tema y sin detalle. Ej: «De publicidad: Amazon, Canva, Fiverr».\n\n"
        "REGLA QUE NO SE ROMPE: todo lo que llegó tiene que aparecer, aunque "
        "sea solo el nombre. Él fue explícito: aunque vos creas que no vale la "
        "pena, igual tiene que saber que llegó. No respondas ningún correo ni "
        "inventes nada que no esté en el texto."
    )


async def revisar_ahora() -> list[dict]:
    """Revisión on-demand ("revisá el correo"): mira lo mismo que el reporte
    pero sin informar formalmente — no marca reportado ni leído, así lo que él
    espía a media tarde igual le llega ordenado en el reporte de la mañana."""
    reglas = await _reglas()
    salida: list[dict] = []
    fallos: list[str] = []
    for cuenta in config.CORREO_CUENTAS:
        try:
            salida += await _pendientes_de(cuenta, reglas)
        except Exception as e:
            fallos.append(f"{cuenta.get('user', '?')} ({type(e).__name__}: {e})")
            log.warning("Falló la revisión de %s.", cuenta.get("user", "?"),
                        exc_info=True)
    # Un buzón que no se pudo abrir no es un buzón vacío (lección del 26-jul).
    if fallos and not salida:
        raise RuntimeError(
            "no pude revisar el correo — " + "; ".join(fallos) +
            ". NO es que no haya llegado nada: la revisión falló.")
    return salida


def _marcar_leidos_sync(cuenta: dict, uids: list[int]) -> int:
    """SÍNCRONO (en un hilo). Marca \\Seen en Gmail. Única escritura de Lucy
    sobre el buzón: todo lo demás es readonly."""
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX")  # sin readonly: acá sí escribimos el flag
        hechos = 0
        for uid in uids:
            typ, _ = M.uid("store", str(uid), "+FLAGS", "(\\Seen)")
            if typ == "OK":
                hechos += 1
        return hechos
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def confirmar_leidos() -> int:
    """Marca como leído en Gmail lo que YA se informó y llegó de verdad.

    "Leído = te informé", la definición de Tiziano. Por eso esto corre después
    del reporte y no antes: primero el mensaje llega, después se marca. Es la
    misma regla de oro de todo el proyecto (mandar primero, marcar después),
    acá aplicada a su buzón: un correo marcado leído que él nunca vio sería
    una mentira escrita en un lugar donde no puede desconfiar.
    """
    await db.olvidar_reportados_fallidos()
    filas = await db.correos_por_marcar_leidos()
    if not filas:
        return 0
    por_cuenta: dict[str, list[int]] = {}
    for f in filas:
        por_cuenta.setdefault(f["cuenta"], []).append(f["uid"])

    total = 0
    for user, uids in por_cuenta.items():
        cta = next((c for c in config.CORREO_CUENTAS if c["user"] == user), None)
        if cta is None:
            continue
        try:
            hechos = await asyncio.to_thread(_marcar_leidos_sync, cta, uids)
            for uid in uids:
                await db.confirmar_leido(user, uid)
            total += hechos
            log.info("Marcados %s correos como leídos en %s (ya informados).",
                     hechos, user)
        except Exception:
            log.warning("No pude marcar leídos en %s; reintento después.", user,
                        exc_info=True)
    return total


# ═══ Vigilancia 911: lo único que interrumpe ═════════════════════════════
#
# Tiziano lo acotó y eso simplificó todo: por correo, lo ÚNICO urgente es que
# se rompa la infraestructura donde viven Natalia, Lucy y la App de registro.
# Lo urgente de clientes le llega por WhatsApp con Natalia, o de humano a
# humano. Así que esto no necesita juzgar el mundo entero: mira un puñado de
# remitentes conocidos y si el asunto huele a incendio.

REMITENTES_INFRA = ("railway.app", "railway.com", "n8n.io", "google.com",
                    "googlecloud", "uptimerobot.com", "nocodb", "cloudflare")

ASUNTOS_911 = ("build failed", "deploy failed", "deployment failed", "failed to",
               "is down", "went down", "outage", "incident", "critical",
               "security update", "vulnerability", "suspended", "payment failed",
               "quota exceeded", "se cayó", "caído")


def _huele_a_911(cand: dict) -> bool:
    """¿Vale la pena despertarlo por esto? Barato: remitente + asunto."""
    frm = (cand.get("from") or "").lower()
    asunto = (cand.get("subject") or "").lower()
    if not any(d in frm for d in REMITENTES_INFRA):
        return False
    return any(p in asunto for p in ASUNTOS_911)


async def vigilar_911(bot) -> int:
    """Cada pocos minutos, 24 h: ¿se rompió algo de la infraestructura?

    Solo esto interrumpe. Si encuentra algo, avisa AL MOMENTO y lo deja como
    encargo para que el agente lo cuente con criterio; el resto del correo ni
    se toca — sigue esperando tranquilo al reporte de la mañana.

    EXENTO de la regla de tarifa doble de DeepSeek, a propósito: la detección
    en sí no gasta IA (mira remitente y asunto), y el encargo que sale cuando
    encuentra algo es una emergencia. Aplazar tres horas el aviso de que se
    cayó producción para ahorrar medio centavo es un mal negocio con nombre.
    """
    if not config.CORREO_CUENTAS:
        return 0
    avisados = 0
    for cuenta in config.CORREO_CUENTAS:
        try:
            crudos = await asyncio.to_thread(
                _sin_leer_sync, cuenta, 1, 30)  # solo el último día
        except Exception:
            log.warning("Vigilancia 911: no pude mirar %s.",
                        cuenta.get("user", "?"), exc_info=True)
            continue

        sospechosos = [c for c in crudos if _huele_a_911(c)]
        if not sospechosos:
            continue
        ya = await db.correos_ya_reportados(
            cuenta["user"], [c["uid"] for c in sospechosos])
        for c in sospechosos:
            if c["uid"] in ya:
                continue
            texto = (f"🚨 {c['from']}\n{c['subject']}\n\n{c['snippet'][:400]}")
            bandeja_id = await db.guardar_en_bandeja(
                tipo_entrada="sistema",
                contenido_raw=(
                    "ALERTA DE INFRAESTRUCTURA por correo (esto sí interrumpe, "
                    "es la única clase de correo urgente que definió Tiziano). "
                    f"Llegó esto:\n\n{texto}\n\n"
                    "Avisale YA, corto y claro: qué servicio, qué pasó, y si "
                    "hay algo que él pueda hacer. Si no es grave de verdad, "
                    "decíselo igual en una línea — pero no lo dejes pasar."),
                chat_id=config.CHAT_ID_DUENO,
                origen="correo",
            )
            await db.marcar_correo_reportado(
                cuenta["user"], c["uid"], nivel="911", ambito="laboral",
                area="infraestructura", asunto=c["subject"],
                bandeja_id=bandeja_id)
            avisados += 1
            log.info("911 de correo: %s — %s", c["from"][:40], c["subject"][:60])
    return avisados


async def _reglas() -> str:
    """Las preferencias de Tiziano que aplican al correo, para el clasificador.

    Es lo que hace que la clasificación se afine sola: él dice «lo de Kazrog es
    ruido» y desde mañana Kazrog baja a mención, sin tocar una línea de código.
    """
    try:
        prefs = await db.listar_preferencias()
    except Exception:
        return ""
    utiles = [p for p in prefs
              if not p.get("contexto") or "correo" in str(p["contexto"]).lower()]
    return "\n".join(f"· {p['texto']}" for p in utiles[:20])


async def reporte_diario() -> int:
    """Una vez al día, en la mañana: junta lo sin leer no informado, lo clasifica
    y deja el encargo del reporte. Devuelve cuántos correos entraron.

    Los correos quedan anotados como "reportados" con el id del encargo, pero
    NO se marcan leídos todavía: eso pasa recién cuando el mensaje llegó de
    verdad (ver confirmar_leidos). Leído significa "ya te informé", así que
    marcarlo antes de que él lo lea sería una mentira escrita en su buzón.
    """
    if not config.CORREO_CUENTAS:
        return 0
    ahora = datetime.now(TZ)
    if not (REPORTE_DESDE <= ahora.hour < REPORTE_HASTA):
        return 0
    # Guarda dura de tarifa doble: este reporte clasifica con DeepSeek UNA
    # llamada por correo, así que es el gasto automático más grande del día.
    # Su ventana (7–12) ya es barata, y por eso esto hoy nunca dispara — está
    # justamente para el día en que alguien mueva REPORTE_HASTA sin acordarse
    # de la regla. La ventana dice CUÁNDO conviene; esto dice cuándo no se puede.
    if config.es_horario_caro_deepseek(ahora):
        log.info("Reporte de correo aplazado: tarifa doble de DeepSeek.")
        return 0
    hoy = ahora.date()

    estados = [await db.leer_estado_correo(c["user"]) for c in config.CORREO_CUENTAS]
    if estados and all(e and e.get("ultimo_reporte") == hoy for e in estados):
        return 0

    reglas = await _reglas()
    pendientes: list[dict] = []
    for cuenta in config.CORREO_CUENTAS:
        try:
            pendientes += await _pendientes_de(cuenta, reglas)
        except Exception:
            log.warning("Falló la revisión de %s; sigo con las demás.",
                        cuenta.get("user", "?"), exc_info=True)

    # Marcamos el día aunque no haya nada, para no reintentar toda la mañana.
    for c, e in zip(config.CORREO_CUENTAS, estados):
        if e:
            await db.guardar_estado_correo(
                c["user"], e["uidvalidity"], e["ultimo_uid"], hoy)

    if not pendientes:
        log.info("Reporte de correo: nada sin leer que no se haya informado.")
        return 0

    bandeja_id = await db.guardar_en_bandeja(
        tipo_entrada="sistema",
        contenido_raw=_encargo(pendientes),
        chat_id=config.CHAT_ID_DUENO,
        origen="correo",
    )
    for c in pendientes:
        cl = c["clasificacion"]
        await db.marcar_correo_reportado(
            c["cuenta"], c["uid"], nivel=cl["nivel"], ambito=cl.get("ambito", ""),
            area=cl.get("area", ""), asunto=c["subject"], bandeja_id=bandeja_id)

    niveles = {}
    for c in pendientes:
        n = c["clasificacion"]["nivel"]
        niveles[n] = niveles.get(n, 0) + 1
    log.info("Reporte de correo: %s correos (%s) → encargo #%s.",
             len(pendientes), niveles, bandeja_id)
    return len(pendientes)
