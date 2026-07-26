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

SISTEMA_RELEVANCIA = (
    "Sos el filtro de correo de Lucy, la asistente personal de Tiziano. Decidís "
    "si un correo AMERITA su atención personal. Relevante: escrito por una "
    "persona real para él, algo que pide acción o respuesta, una cita, una "
    "factura o pago, algo personal o de sus negocios (CDS, estudio de "
    "grabación) que no puede ignorar. NO relevante: promociones, newsletters, "
    "avisos automáticos, redes sociales, cosas de sistema. Ante la duda, es NO: "
    "molestarlo con ruido es peor que dejar pasar un correo tibio. Devolvé JSON "
    '{"relevante": true/false, "motivo": "en pocas palabras"}.'
)


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
    if any(x in frm for x in ("no-reply", "noreply", "no_reply", "donotreply",
                              "notifications@", "notification@", "mailer@",
                              "newsletter", "bounce", "mailer-daemon")):
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
    return " ".join(texto.split())[:limite]


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


async def _relevante(cand: dict) -> dict:
    """La IA decide si el correo amerita atención. From+Subject alcanza y expone
    menos que mandar el cuerpo entero."""
    r = await motor.cliente.chat.completions.create(
        model=motor.MODELO,
        messages=[
            {"role": "system", "content": SISTEMA_RELEVANCIA},
            {"role": "user",
             "content": f"De: {cand['from']}\nAsunto: {cand['subject']}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(r.choices[0].message.content)


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


async def _relevantes_de(cuenta: dict, hoy) -> list[dict]:
    """Lee lo nuevo de una cuenta y devuelve solo los relevantes. Avanza el
    puntero. Si `hoy` no es None, marca el reporte de ese día (para no repetir
    el matinal); on-demand pasa None y no lo toca."""
    estado = await db.leer_estado_correo(cuenta["user"])

    uidvalidity, top, candidatos = await asyncio.to_thread(
        _cosechar, cuenta, estado["ultimo_uid"] if estado else 0)

    # Primera vez, o Gmail renumeró (cambió UIDVALIDITY): fijamos la línea de
    # corte en el tope actual y NO procesamos el backlog. Miramos desde acá.
    if estado is None or estado["uidvalidity"] != uidvalidity:
        await db.guardar_estado_correo(cuenta["user"], uidvalidity, top, hoy)
        log.info("Correo %s: línea de corte en UID %s (backlog ignorado).",
                 cuenta["user"], top)
        return []

    relevantes = []
    for cand in candidatos:
        try:
            veredicto = await _relevante(cand)
        except Exception:
            log.warning("No pude juzgar un correo de %s; lo salteo (sigue en "
                        "el buzón).", cuenta["user"], exc_info=True)
            continue
        if veredicto.get("relevante"):
            cand["cuenta"] = cuenta["user"]
            relevantes.append(cand)
            log.info("Correo relevante de %s: %s (%s)", cand["from"][:40],
                     cand["subject"][:50], veredicto.get("motivo", ""))

    await db.guardar_estado_correo(cuenta["user"], uidvalidity, top, hoy)
    return relevantes


def _encargo(relevantes: list[dict]) -> str:
    """Arma el encargo que se le deja al agente para que redacte el reporte.

    Igual que el briefing: el despertador/vigía junta los datos, el agente los
    convierte en un mensaje humano y acciona lo que corresponda. Acá no se
    escribe el reporte — armarlo sin el criterio del agente sería opinar sin él.
    """
    lineas = []
    for i, r in enumerate(relevantes, 1):
        lineas.append(f"{i}. De {r['from']} (a {r['cuenta']})\n"
                      f"   Asunto: {r['subject']}\n   {r['snippet'][:300]}")
    return (
        "Reporte de correo de la mañana. Llegaron estos correos relevantes desde "
        "tu última revisión:\n\n" + "\n\n".join(lineas) + "\n\n"
        "Dale a Tiziano UN resumen corto y ordenado: de quién, de qué, y qué "
        "requiere de él. Accioná lo que claramente corresponda —si alguno trae "
        "una cita, creála; si es una factura, registrala; si pide respuesta con "
        "fecha, anotá la tarea— y decile qué hiciste. Lo dudoso, proponelo. No "
        "respondas ningún correo ni inventes lo que no está en el texto."
    )


async def reporte_diario() -> int:
    """Una vez al día, en la mañana: junta el correo relevante y deja el encargo
    del reporte en la bandeja. Devuelve cuántos relevantes encontró.

    El guard es barato (una query por cuenta): el IMAP solo se abre cuando de
    verdad toca, una vez al día. Fuera de la ventana, o si ya reportó hoy, sale
    enseguida sin tocar Gmail.
    """
    if not config.CORREO_CUENTAS:
        return 0
    ahora = datetime.now(TZ)
    if not (REPORTE_DESDE <= ahora.hour < REPORTE_HASTA):
        return 0
    hoy = ahora.date()

    # ¿Ya reportó hoy? Si TODAS las cuentas tienen ultimo_reporte == hoy, listo.
    estados = [await db.leer_estado_correo(c["user"]) for c in config.CORREO_CUENTAS]
    if estados and all(e and e.get("ultimo_reporte") == hoy for e in estados):
        return 0

    relevantes = []
    for cuenta in config.CORREO_CUENTAS:
        try:
            relevantes += await _relevantes_de(cuenta, hoy)
        except Exception:
            log.warning("Falló la revisión de %s; sigo con las demás.",
                        cuenta.get("user", "?"), exc_info=True)

    if relevantes:
        await db.guardar_en_bandeja(
            tipo_entrada="sistema",
            contenido_raw=_encargo(relevantes),
            chat_id=config.CHAT_ID_DUENO,
            origen="correo",
        )
        log.info("Reporte de correo: %s relevante(s) → encargo en la bandeja.",
                 len(relevantes))
    return len(relevantes)
