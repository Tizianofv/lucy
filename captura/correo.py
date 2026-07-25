"""Vigía de correo (Nivel 4, req 17): mira los buzones, filtra con criterio, y
deja en la bandeja SOLO lo que amerita la atención de Tiziano.

Dos filtros en cascada, para no malgastar IA ni exponer de más:
  1. Barato, por cabeceras: descarta el grueso (no-reply, listas, newsletters)
     sin gastar un token. En el correo real de Tiziano tumba ~80%.
  2. La IA juzga lo que sobrevive: ¿esto merece que lo interrumpan? Solo lo que
     pasa acá cae en la bandeja.

Arranca desde AHORA. La primera vuelta de cada cuenta guarda el UID más alto
SIN procesar nada: el backlog histórico (decenas de miles) no se toca. Vigilar
es mirar lo que llega, no releer el pasado.

imaplib es síncrono; toda la sesión IMAP corre en un hilo (asyncio.to_thread)
para no congelar el bucle del agente mientras habla con Gmail.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
from email.header import decode_header

import cerebro.deepseek as motor
import config
import db.db as db

log = logging.getLogger("lucy.correo")

SERVIDOR = "imap.gmail.com"

# Cuántos correos nuevos procesar por cuenta y vuelta. Un tope para que una
# ráfaga (volvió el internet tras horas) no dispare cientos de llamadas a la IA
# de golpe; lo que no entre esta vuelta entra en la próxima.
MAX_POR_VUELTA = 25

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
        for uid in uids[:MAX_POR_VUELTA]:
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


async def _procesar_cuenta(cuenta: dict) -> int:
    """Vigila una cuenta. Devuelve cuántos correos relevantes dejó en la bandeja."""
    estado = await db.leer_estado_correo(cuenta["user"])

    uidvalidity, top, candidatos = await asyncio.to_thread(
        _cosechar, cuenta, estado["ultimo_uid"] if estado else 0)

    # Primera vez, o Gmail renumeró (cambió UIDVALIDITY): fijamos la línea de
    # corte en el tope actual y NO procesamos el backlog. Vigilamos desde acá.
    if estado is None or estado["uidvalidity"] != uidvalidity:
        await db.guardar_estado_correo(cuenta["user"], uidvalidity, top)
        log.info("Correo %s: línea de corte en UID %s (backlog ignorado).",
                 cuenta["user"], top)
        return 0

    relevantes = 0
    for cand in candidatos:
        try:
            veredicto = await _relevante(cand)
        except Exception:
            log.warning("No pude juzgar un correo de %s; lo salteo (sigue en "
                        "el buzón).", cuenta["user"], exc_info=True)
            continue
        if not veredicto.get("relevante"):
            continue
        contenido = (
            f"[correo de {cand['from']} → {cuenta['user']}]\n"
            f"Asunto: {cand['subject']}\n\n{cand['snippet']}"
        )
        await db.guardar_en_bandeja(
            tipo_entrada="email",
            contenido_raw=contenido,
            chat_id=config.CHAT_ID_DUENO,
            origen="email",
        )
        relevantes += 1
        log.info("Correo relevante de %s: %s (%s)", cand["from"][:40],
                 cand["subject"][:50], veredicto.get("motivo", ""))

    await db.guardar_estado_correo(cuenta["user"], uidvalidity, top)
    return relevantes


async def vigilar() -> int:
    """Recorre todas las cuentas. Devuelve el total de relevantes encolados.

    Una cuenta que falla (Gmail caído, credencial revocada) no frena a las
    otras ni al bucle: se loguea y se sigue. El correo no es el latido de Lucy.
    """
    total = 0
    for cuenta in config.CORREO_CUENTAS:
        try:
            total += await _procesar_cuenta(cuenta)
        except Exception:
            log.warning("Falló la vigilancia de %s; reintento en la próxima.",
                        cuenta.get("user", "?"), exc_info=True)
    return total
