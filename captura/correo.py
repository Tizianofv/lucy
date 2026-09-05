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

# ACOTAR SIN DESCARTAR: EL CUPO LIMITA EL TRABAJO CARO, NO LA LISTA DE CORREOS.
#
# Hasta el 5-sep-2026 acá vivía `MAX_POR_DIA = 60`, puesto el 24-jul-2026
# (commit 6395ec7, que subió el viejo `MAX_POR_VUELTA = 25`) con este motivo
# escrito al lado: "el tope es un cinturón contra una ráfaga rara". Ese tope
# hacía DOS cosas a la vez y solo una estaba mal:
#
#   · ACOTABA el trabajo de una vuelta. Eso hacía falta, y sigue haciendo falta.
#   · DESCARTABA lo que no cabía. Eso estaba mal, y es lo que se fue.
#
# Se aplicaba en `_sin_leer_sync`, sobre la lista de UIDs recién buscada, y se
# quedaba con los 60 MÁS NUEVOS:
#
#   1. Cortaba ANTES de descontar los que ya se habían informado, así que el
#      presupuesto se gastaba en correos que después se tiraban igual.
#   2. Lo que caía del corte no dejaba rastro: ni un log, ni un aviso. Un correo
#      viejo sin leer podía salirse de la ventana de 7 días sin que a nadie le
#      constara que existió.
#
# Y mordió de verdad: en la base de producción los encargos 171 y 172 (los dos
# del 28-jul-2026) tienen 60 correos clavados cada uno para la misma cuenta —
# medido el 5-sep-2026 con `SELECT bandeja_id, cuenta, count(*) FROM
# correo_reportado GROUP BY 1,2`, que devuelve 2 grupos de exactamente 60 y
# ninguno entre 31 y 59.
#
# El 5-sep-2026 se quitó entero, y quitarlo entero fue quitar de más: sin cupo,
# un buzón con 2.000 sin leer en la ventana disparaba 2.000 descargas de cuerpo
# y 2.000 llamadas a DeepSeek, secuenciales y sin límite de tiempo — medido con
# un arnés de IMAP falso: N=2000 → 2000 cuerpos, 2000 llamadas, y un encargo de
# 812.564 caracteres. Y como `reporte_diario()` se llama con `await` directo
# desde `cerebro/interpretar.py::bucle`, mientras eso corre Lucy no vuelve a
# mirar los mensajes de Tiziano.
#
# LA FORMA QUE QUEDÓ. El cupo de abajo NO decide qué correos entran al reporte:
# entran TODOS, siempre. Decide en cuántos se gasta lo caro — una llamada a
# DeepSeek y una descarga de cuerpo por correo. Lo que no entra en el cupo se
# informa igual, agrupado por remitente y contado, diciendo con todas las
# letras que Lucy no alcanzó a mirarlo con criterio. O sea: se pierde JUICIO
# sobre esos correos, nunca el correo.
#
# Por qué no se aplazan a la vuelta siguiente, que era la otra forma: la ventana
# son 7 días. Con 2.000 atrasados y 60 por mañana harían falta 34 días, así que
# lo aplazado se caería igual de la ventana — el mismo descarte de antes, solo
# que más lento y con un log. Informar de todo hoy borra ese caso en vez de
# manejarlo: ningún correo queda esperando un turno que no va a llegar.
#
# El orden sí importa, y es el que el tope viejo tenía al revés: LO MÁS VIEJO
# PRIMERO. Es lo que lleva más esperando y lo más cerca de caerse de la ventana,
# así que es lo que se gana el cupo.
#
# 60 es el mismo número que había, a propósito: es el techo de gasto que este
# reporte ya tenía y que nadie objetó. La diferencia es que antes esos 60 se
# gastaban ANTES de descontar informados y bancos —o sea, en correos que se
# tiraban igual— y ahora son 60 clasificaciones que sirven.
MAX_CLASIFICA_POR_VUELTA = 60

# El nivel de lo que entró al reporte sin pasar por el clasificador. No está en
# `NIVELES` a propósito: `NIVELES` es el vocabulario con que se valida lo que
# devuelve el modelo, y el modelo nunca puede devolver esto — lo pone Lucy
# cuando se le acabó el cupo del día.
SIN_CLASIFICAR = "sin_clasificar"

# Los niveles que llevan extracto del cuerpo en el encargo. Vive acá arriba y en
# un solo sitio porque decide DOS cosas que tienen que coincidir: qué correos se
# muestran con extracto (`_linea`) y de cuáles se baja el cuerpo por IMAP
# (`_pendientes_de`). Con dos listas escritas a mano, el día que alguien agregue
# un nivel se baja un cuerpo que no se usa, o se muestra un extracto vacío.
NIVELES_CON_EXTRACTO = ("911", "accion", "dudoso")

# La primera frase del encargo que el reporte deja en la bandeja, y a la vez la
# MARCA de "hoy ya salió": el candado de una vez al día busca ESTA constante en
# la bandeja (ver reporte_diario). Por eso vive en un solo sitio y `_encargo()`
# la usa para armar el texto — si alguien reescribe la frase en el encargo sin
# tocar el candado, el candado se abre y el reporte vuelve a salir cien veces.
# `tests/test_reporte_una_vez_al_dia.py` ata las dos puntas.
MARCA_ENCARGO = "Reporte de correo de la mañana."

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
    """Cabecera MIME (=?utf-8?...) → texto legible.

    Un charset que Python no conoce NO puede tumbar esto. El caso que pasa de
    verdad es `unknown-8bit`: es la etiqueta que le pone el propio `email`
    cuando la cabecera trae bytes crudos de más de 7 bits sin declarar
    codificación, y la manda cualquier remitente mal configurado — un asunto
    con una tilde alcanza. Python no tiene ese códec, así que `p.decode(...)`
    lanzaba `LookupError` desde dentro de `_sin_leer_sync`, y arriba
    `reporte_diario` se lo comía con un `log.warning` y seguía con la cuenta
    siguiente: UN asunto mal formado dejaba el buzón ENTERO fuera del reporte
    del día y nadie se enteraba. Un carácter ilegible es mejor que un buzón
    mudo.
    """
    if not v:
        return ""
    partes = []
    for p, enc in decode_header(v):
        if not isinstance(p, bytes):
            partes.append(p)
            continue
        try:
            partes.append(p.decode(enc or "utf-8", "replace"))
        except LookupError:
            partes.append(p.decode("utf-8", "replace"))
    return "".join(partes)


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


def _ficha(cuenta: dict, uid: bytes, crudo: bytes, con_cuerpo: bool) -> dict:
    """El diccionario con el que trabaja el resto del módulo, desde el correo
    crudo que devolvió IMAP."""
    msg = email.message_from_bytes(crudo)
    return {
        "cuenta": cuenta["user"],
        "uid": int(uid),
        "from": _texto(msg.get("From")),
        "subject": _texto(msg.get("Subject")),
        "fecha": _texto(msg.get("Date")),
        # Sin cuerpo no hay extracto. `_es_ruido` sí sale entero de las
        # cabeceras, así que se calcula en los dos modos.
        "snippet": _snippet(msg) if con_cuerpo else "",
        "ruido_barato": _es_ruido(msg),  # se informa igual, pero baja solo
    }


def _sin_leer_sync(cuenta: dict, dias: int, *,
                   con_cuerpo: bool = True) -> list[dict]:
    """SÍNCRONO (en un hilo). TODOS los correos SIN LEER de los últimos `dias`.

    Este reemplaza al puntero como fuente del reporte, y es el arreglo de fondo
    del sistema: el puntero SE CONSUMÍA —cualquier revisión lo adelantaba— así
    que el reporte de la mañana podía encontrar cero y quedarse mudo mientras
    había correos pendientes de verdad. "Sin leer" no se consume solo, coincide
    con lo que Tiziano ve en su Gmail, y sobrevive a que Lucy esté caída.

    TODOS quiere decir todos: acá no se descarta nada, y acá no vive ningún
    tope. El cupo del reporte se aplica más arriba, sobre el trabajo caro y
    después de descontar lo que ya se informó (ver `MAX_CLASIFICA_POR_VUELTA`).

    `con_cuerpo=False` pide solo cabeceras. Sirve para decidir barato quién
    merece que le bajemos el cuerpo entero — es lo que hace la vigilancia 911,
    que corre cada pocos minutos las 24 horas y solo necesita remitente y
    asunto para saber si mirar más.
    """
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        desde = _fecha_imap(datetime.now(TZ) - timedelta(days=dias))
        typ, data = M.uid("search", None, "UNSEEN", "SINCE", desde)
        uids = data[0].split() if data and data[0] else []
        # Los más nuevos primero. Antes este orden decidía qué se salvaba del
        # tope; ahora solo decide en qué orden se mira, porque no se cae nada.
        uids = uids[::-1]
        pieza = "(BODY.PEEK[])" if con_cuerpo else "(BODY.PEEK[HEADER])"
        salida = []
        for uid in uids:
            d = M.uid("fetch", uid.decode(), pieza)[1]
            if not d or not d[0]:
                continue
            salida.append(_ficha(cuenta, uid, d[0][1], con_cuerpo))
        return salida
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _traer_sync(cuenta: dict, uids: list[int]) -> list[dict]:
    """SÍNCRONO (en un hilo). El cuerpo de UNOS uids concretos, y nada más.

    Es la segunda mitad del par barato/caro: primero se mira con cabeceras
    quién importa (`_sin_leer_sync(..., con_cuerpo=False)`) y después se baja
    entero solo eso. Sin esto, la única forma de tener el extracto de un correo
    era bajarlos todos.
    """
    if not uids:
        return []
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    try:
        M.login(cuenta["user"], cuenta["pass"])
        M.select("INBOX", readonly=True)
        salida = []
        for uid in uids:
            d = M.uid("fetch", str(uid), "(BODY.PEEK[])")[1]
            if not d or not d[0]:
                continue
            salida.append(_ficha(cuenta, str(uid).encode(), d[0][1], True))
        return salida
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


def destino_del_reporte(cuenta: dict) -> int:
    """A qué chat va el reporte de ESTE buzón.

    Sin el campo `reporte_a`, va al dueño — que es como se comportaba antes y
    por eso no rompe nada existente. Con él, el buzón se puede escanear para
    bancos sin que su correspondencia aparezca en el briefing de otra persona.

    Es la línea que separa "Lucy lee el correo de Rosi para sacar sus
    movimientos" de "Lucy le cuenta a Tiziano lo que le escriben a Rosi". Son
    cosas distintas y el sistema tiene que poder hacer la primera sin la
    segunda.

    `reporte_a: 0` (o false) = este buzón NO genera reporte para nadie.
    """
    v = cuenta.get("reporte_a", cuenta.get("reporte", True))
    if v is True:
        return config.CHAT_ID_DUENO
    if v is False or v == 0:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        log.warning("reporte_a inválido en %s (%r): mando al dueño.",
                    cuenta.get("user"), v)
        return config.CHAT_ID_DUENO


async def _pendientes_de(cuenta: dict, reglas: str = "") -> list[dict]:
    """Los correos SIN LEER de esta cuenta que todavía no se informaron, ya
    clasificados. Es la materia prima del reporte.

    No toca el puntero ni marca nada: solo mira. Lo que se informa y lo que se
    marca leído se decide después, cuando el reporte de verdad haya salido.

    Barato primero, caro después, igual que `vigilar_911`: se traen SOLO las
    cabeceras de todo lo sin leer, se descuenta lo que ya se informó y lo que
    lee la ingesta bancaria, y recién entonces se gasta —clasificar con DeepSeek
    y bajar el cuerpo— y solo hasta `MAX_CLASIFICA_POR_VUELTA`. Bajar el cuerpo
    de todos, como se hacía hasta el 5-sep-2026, pagaba lo caro por correos que
    dos líneas después se tiraban.
    """
    crudos = await asyncio.to_thread(
        _sin_leer_sync, cuenta, VENTANA_DIAS, con_cuerpo=False)
    if not crudos:
        return []

    ya = await db.correos_ya_reportados(
        cuenta["user"], [c["uid"] for c in crudos])
    nuevos = [c for c in crudos if c["uid"] not in ya]

    # Los correos de los bancos que la ingesta ya sabe leer NO entran al
    # reporte. Cada consumo con tarjeta llega como un correo y el clasificador
    # lo marcaría "accion" —hay una regla explícita para eso en
    # SISTEMA_CLASIFICA—, así que con varias compras al día el briefing matinal
    # se convertiría en una lista de compras que Tiziano ya puede ver mejor en
    # el panel. Y cada uno costaría una llamada a DeepSeek.
    #
    # Se filtra por REMITENTE REGISTRADO, no por una lista aparte: así, cuando
    # se añada un banco nuevo, deja de ensuciar el reporte solo, sin que nadie
    # se acuerde de tocar este archivo.
    if nuevos:
        try:
            import cerebro.bancos as _bancos
            de_bancos = set(_bancos.remitentes_registrados())
        except Exception:
            de_bancos = set()
        if de_bancos:
            def _dir(remitente: str) -> str:
                r = remitente or ""
                if "<" in r:
                    r = r.split("<")[-1].split(">")[0]
                return r.strip().lower()
            antes = len(nuevos)
            nuevos = [c for c in nuevos if _dir(c["from"]) not in de_bancos]
            if antes != len(nuevos):
                log.info("Reporte: %s correos bancarios fuera (los lee la "
                         "ingesta).", antes - len(nuevos))
    if not nuevos:
        return []

    # LO MÁS VIEJO PRIMERO. El uid de IMAP crece con la llegada al buzón, así
    # que ordenar por uid es ordenar por antigüedad. Es lo que lleva más
    # esperando y lo más cerca de caerse de la ventana de 7 días: si el cupo no
    # alcanza para todos, que se lo lleve eso y no lo que llegó hace una hora.
    nuevos.sort(key=lambda c: c["uid"])

    # El filtro barato ya no descarta: solo ahorra una llamada a la IA en lo
    # que es ruido evidente. El correo se informa igual, como mención — que
    # es justo la regla que puso Tiziano: nada desaparece en silencio. Y como no
    # gasta cupo, el cupo entero queda para lo que sí hay que juzgar.
    for c in nuevos:
        if c.get("ruido_barato"):
            c["clasificacion"] = {
                "ambito": "", "area": "publicidad_laboral", "nivel": "mencion",
                "asunto_corto": c["subject"][:120],
                "motivo": f"filtro: {c['ruido_barato']}",
            }

    a_juzgar = [c for c in nuevos if "clasificacion" not in c]
    con_cupo = a_juzgar[:MAX_CLASIFICA_POR_VUELTA]
    sin_cupo = a_juzgar[MAX_CLASIFICA_POR_VUELTA:]

    for c in con_cupo:
        c["clasificacion"] = await clasificar(c, reglas)

    # Los que no entraron en el cupo NO se caen: entran al reporte con lo que se
    # sabe de ellos sin gastar nada —quién escribió y qué asunto puso—, y
    # `_encargo` los cuenta y los nombra aparte, diciendo que Lucy no los miró
    # con criterio. Nada desaparece en silencio; lo que se pierde es el juicio
    # sobre ellos, no el correo.
    for c in sin_cupo:
        c["clasificacion"] = {
            "ambito": "", "area": "", "nivel": SIN_CLASIFICAR,
            "asunto_corto": c["subject"][:120],
            "motivo": f"no alcancé a mirarlo hoy (cupo de {MAX_CLASIFICA_POR_VUELTA})",
        }
    if sin_cupo:
        log.warning(
            "Reporte de %s: %s correos entran SIN clasificar (cupo %s de %s por "
            "juzgar). Se informan igual, agrupados.",
            cuenta.get("user", "?"), len(sin_cupo), MAX_CLASIFICA_POR_VUELTA,
            len(a_juzgar))

    # El cuerpo, solo de los que de verdad lo muestran. Es el otro tramo caro:
    # antes se bajaban los N cuerpos de la ventana entera, y el extracto solo lo
    # usan los niveles de `NIVELES_CON_EXTRACTO`.
    # `not c.get("snippet")` no es una guarda de más: es "no pidas lo que ya
    # tenés". Sin ella, un camino que llegue acá con los cuerpos ya en la mano
    # abre una segunda sesión IMAP para volver a bajar lo mismo.
    caros = [c for c in con_cupo
             if c["clasificacion"]["nivel"] in NIVELES_CON_EXTRACTO
             and not c.get("snippet")]
    if caros:
        try:
            cuerpos = await asyncio.to_thread(
                _traer_sync, cuenta, [c["uid"] for c in caros])
        except Exception:
            # Un cuerpo que no se pudo bajar no cancela el correo: sale sin
            # extracto, igual que en la vigilancia 911.
            log.warning("No pude bajar los cuerpos en %s; el reporte sale sin "
                        "extractos.", cuenta.get("user", "?"), exc_info=True)
            cuerpos = []
        extractos = {c["uid"]: c.get("snippet", "") for c in cuerpos}
        for c in caros:
            c["snippet"] = extractos.get(c["uid"], "")
    return nuevos


def _linea(c: dict) -> str:
    """Una línea de correo para el encargo, con lo justo según su nivel."""
    cl = c["clasificacion"]
    base = f"[{cl['nivel']}|{cl.get('area') or '?'}] De {c['from']} → {cl['asunto_corto']}"
    if cl["nivel"] in NIVELES_CON_EXTRACTO:
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


def _por_remitente(correos: list[dict], top: int = 25) -> str:
    """'Pinterest (12), Amazon (3), Canva, y 40 remitentes más'.

    Cómo se nombra en el encargo un montón de correos que no llevan detalle. El
    número total sale del largo de la lista, no de un contador aparte: así no
    puede decir 12 cuando hay 13.
    """
    cuenta_por: dict[str, int] = {}
    for c in correos:
        n = _nombre(c["from"])
        cuenta_por[n] = cuenta_por.get(n, 0) + 1
    ordenados = sorted(cuenta_por.items(), key=lambda x: -x[1])
    listado = ", ".join(f"{n} ({k})" if k > 1 else n
                        for n, k in ordenados[:top])
    if len(ordenados) > top:
        listado += f", y {len(ordenados) - top} remitentes más"
    return listado


def _encargo(pendientes: list[dict]) -> str:
    """El encargo que se le deja al agente para que redacte el reporte.

    Igual que el briefing: acá se juntan y clasifican los datos, y el AGENTE
    los convierte en un mensaje humano. Lo que este texto sí fija es la
    política que Tiziano definió: qué nivel lleva cuánto detalle, y que nada
    —ni la publicidad— puede quedar sin mencionarse.
    """
    orden = {"911": 0, "accion": 1, "dudoso": 2, "enterarte": 3, "mencion": 4,
             SIN_CLASIFICAR: 5}
    pendientes = sorted(pendientes, key=lambda c: orden.get(
        c["clasificacion"]["nivel"], 9))

    # Las menciones y los que no entraron en el cupo van AGRUPADOS por remitente
    # y contados. Cumplen la regla de Tiziano (aparecen, él se entera de que
    # llegaron) sin convertir el reporte en un muro: en el ensayo eran 69 líneas
    # de Pinterest, ofertas y newsletters, y un día de atasco serían miles.
    total = len(pendientes)
    menciones = [c for c in pendientes if c["clasificacion"]["nivel"] == "mencion"]
    sin_juzgar = [c for c in pendientes
                  if c["clasificacion"]["nivel"] == SIN_CLASIFICAR]
    detalle = [c for c in pendientes
               if c["clasificacion"]["nivel"] not in ("mencion", SIN_CLASIFICAR)]
    lineas = "\n".join(_linea(c) for c in detalle)
    if menciones:
        lineas += (f"\n\n[mencion] {len(menciones)} correos sin importancia, "
                   f"de: {_por_remitente(menciones)}")
    if sin_juzgar:
        # Esto es lo que evita que un día de atasco se convierta en un descarte
        # callado: llegaron, se dicen, y se dice que no se miraron con criterio.
        lineas += (
            f"\n\n[{SIN_CLASIFICAR}] {len(sin_juzgar)} correos que NO alcancé a "
            f"mirar hoy (el cupo del día es {MAX_CLASIFICA_POR_VUELTA} y hoy "
            f"llegaron muchos más). No sé de qué van: solo sé que llegaron, y "
            f"son de: {_por_remitente(sin_juzgar)}")
    return (
        f"{MARCA_ENCARGO} Estos son los {total} correos SIN LEER que "
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
        "sin tema y sin detalle. Ej: «De publicidad: Amazon, Canva, Fiverr».\n"
        f"· {SIN_CLASIFICAR.upper()}: si hay, decíselo derecho y al final — "
        "cuántos son y de quiénes. Que quede claro que llegaron y que NO los "
        "leíste, para que él decida si quiere mirarlos. No inventes de qué van "
        "ni les pongas un nivel: no lo sabés.\n\n"
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

    Mira TODO lo sin leer del último día, no una parte. Antes cortaba en los 30
    más nuevos, y en el camino de las emergencias eso es lo peor que se puede
    hacer: un día con 31 correos sin leer dejaba el "deploy failed" más viejo
    fuera para siempre y sin una línea de log. Que ahora pueda mirarlos todos
    sin volverse caro es por el par de abajo — cabeceras para decidir, cuerpo
    solo para los que hay que contar. Como esto corre cada pocos minutos las 24
    horas, es además MÁS barato que antes: lo normal es cero sospechosos y ni
    un cuerpo bajado.
    """
    if not config.CORREO_CUENTAS:
        return 0
    avisados = 0
    for cuenta in config.CORREO_CUENTAS:
        try:
            cabeceras = await asyncio.to_thread(
                _sin_leer_sync, cuenta, 1, con_cuerpo=False)  # solo el último día
        except Exception:
            log.warning("Vigilancia 911: no pude mirar %s.",
                        cuenta.get("user", "?"), exc_info=True)
            continue

        sospechosos = [c for c in cabeceras if _huele_a_911(c)]
        if not sospechosos:
            continue
        ya = await db.correos_ya_reportados(
            cuenta["user"], [c["uid"] for c in sospechosos])
        nuevos = [c for c in sospechosos if c["uid"] not in ya]
        if not nuevos:
            continue
        # Recién acá se baja el cuerpo, y solo de estos: el extracto es lo que
        # deja que el aviso diga qué pasó y no solo que pasó algo.
        try:
            con_cuerpo = await asyncio.to_thread(
                _traer_sync, cuenta, [c["uid"] for c in nuevos])
        except Exception:
            log.warning("Vigilancia 911: no pude bajar el cuerpo en %s; aviso "
                        "con lo que tengo.", cuenta.get("user", "?"),
                        exc_info=True)
            con_cuerpo = []
        # Un cuerpo que no se pudo bajar NO cancela el aviso: se avisa igual,
        # sin extracto. Callar una alerta de infraestructura porque falló el
        # segundo fetch sería cambiar un aviso incompleto por ninguno.
        extractos = {c["uid"]: c.get("snippet", "") for c in con_cuerpo}
        for c in nuevos:
            snippet = extractos.get(c["uid"], "")
            texto = (f"🚨 {c['from']}\n{c['subject']}\n\n{snippet[:400]}")
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

    UNA VEZ AL DÍA de verdad: la marca de que el reporte ya salió es el propio
    encargo en la bandeja (ver el candado más abajo). El bucle llama a esto
    cada ~3 minutos, así que de 7 a 12 son ~100 llamadas: todas menos la que
    deja el encargo tienen que salir por el candado.

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
    # EL CANDADO DE "HOY YA SALIÓ", UNO POR DESTINATARIO. La marca es el PROPIO
    # encargo que este reporte deja en la bandeja, igual que el briefing matinal
    # (cerebro/despertador.py::_briefing). No una tabla aparte.
    #
    # Antes la marca era `correo_estado.ultimo_reporte`, y no cerraba nunca: la
    # condición pedía una fila por CADA cuenta, el único sitio que escribía esa
    # fila era el `if e:` de más abajo —que solo actualiza filas que ya
    # existen— y el bloque que las creaba se había borrado. Sin fila, el
    # candado quedaba abierto y el reporte entraba en cada vuelta del bucle:
    # ~100 salidas por mañana en vez de una. Un candado que lee lo mismo que
    # escribe no se puede romper por falta de inicialización.
    #
    # Y se pregunta POR DESTINO, no en global. Cada buzón informa al chat que
    # dice su `reporte_a`, así que "¿ya salió el reporte de hoy?" sin decir de
    # quién tiene la respuesta equivocada: si el dueño recibió el suyo a las
    # 7:10, un candado global deja al destino de las 9:00 sin nada hasta mañana
    # y sin rastro. Un destino, un candado, un reporte por día.
    hoy_arranca = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    ya_reportados = await db.destinos_con_encargo_hoy(
        "correo", MARCA_ENCARGO, hoy_arranca)
    destinos = {d for d in map(destino_del_reporte, config.CORREO_CUENTAS) if d}
    if not destinos - ya_reportados:
        # Todos los que informan a alguien ya informaron. Se sale ANTES de
        # abrir IMAP: son ~100 vueltas por mañana.
        return 0

    reglas = await _reglas()
    # Los correos se agrupan POR DESTINO, no en un solo montón: cada buzón
    # informa a quien le corresponde. Hoy todos van al dueño porque ninguna
    # cuenta declara `reporte_a`, pero la estructura ya no lo obliga.
    por_destino: dict[int, list[dict]] = {}
    for cuenta in config.CORREO_CUENTAS:
        destino = destino_del_reporte(cuenta)
        if not destino:
            continue          # buzón que se lee para bancos y nada más
        if destino in ya_reportados:
            continue          # ese chat ya tuvo su reporte hoy
        try:
            por_destino.setdefault(destino, []).extend(
                await _pendientes_de(cuenta, reglas))
        except Exception:
            log.warning("Falló la revisión de %s; sigo con las demás.",
                        cuenta.get("user", "?"), exc_info=True)
    pendientes = [c for lista in por_destino.values() for c in lista]

    if not pendientes:
        # Sin encargo no hay marca, así que una mañana sin correo se vuelve a
        # mirar en la próxima vuelta. Es barato —mirar no clasifica, y sin
        # correos nuevos no hay ni una llamada a DeepSeek— y es la consecuencia
        # de que la marca sea el encargo: no hubo reporte, así que el reporte
        # del día sigue pendiente. El primero que llegue antes del mediodía lo
        # dispara; lo que llegue DESPUÉS de ese ya espera a mañana.
        log.info("Reporte de correo: nada sin leer que no se haya informado.")
        return 0

    # Un encargo POR DESTINO. Juntarlos mandaría el correo de una persona al
    # chat de otra, que es exactamente lo que este cambio existe para impedir.
    #
    # Y cada correo se ata AL SUYO, dentro del mismo bucle que lo creó. Hasta el
    # 5-sep-2026 esto se hacía en dos pasos: primero se creaban todos los
    # encargos y se guardaba UN `bandeja_id` —el del dueño, o el primero que se
    # hubiera creado—, y después se marcaban TODOS los correos con ese único id.
    # O sea: una relación que es de varios se guardaba como si fuera de uno, y
    # el desempate lo resolvía una regla ("prefiero el del dueño") en vez de un
    # dato. Con dos destinos, los correos de uno quedaban colgando del encargo
    # del otro.
    #
    # No es cosmético: `db.correos_por_marcar_leidos` y
    # `db.olvidar_reportados_fallidos` deciden POR ESE id. Con el id cruzado,
    # el encargo de una persona que sale bien marcaba como leídos —"ya te
    # informé"— correos de otra persona cuyo reporte nunca llegó.
    #
    # Hoy en producción no hay ni una fila mal atada, porque hay un solo
    # destino: las 994 filas de `correo_reportado` cuelgan de encargos al chat
    # del dueño (medido el 5-sep-2026). El defecto estaba esperando al segundo
    # `reporte_a`, que es justamente para lo que existe ese campo.
    #
    # Marcar dentro del bucle no adelanta nada respecto de "mandar primero,
    # marcar después": lo que se marca es que el ENCARGO quedó escrito en la
    # bandeja, y quien decide si de verdad llegó sigue siendo
    # `confirmar_leidos`, que espera a que la bandeja diga 'procesado'.
    encargos: dict[int, int] = {}      # destino → id del encargo que le tocó
    for destino, lista in por_destino.items():
        if not lista:
            continue
        bandeja_id = await db.guardar_en_bandeja(
            tipo_entrada="sistema", contenido_raw=_encargo(lista),
            chat_id=destino, origen="correo")
        encargos[destino] = bandeja_id
        for c in lista:
            cl = c["clasificacion"]
            await db.marcar_correo_reportado(
                c["cuenta"], c["uid"], nivel=cl["nivel"],
                ambito=cl.get("ambito", ""), area=cl.get("area", ""),
                asunto=c["subject"], bandeja_id=bandeja_id)

    niveles = {}
    for c in pendientes:
        n = c["clasificacion"]["nivel"]
        niveles[n] = niveles.get(n, 0) + 1
    # El log nombra TODOS los encargos, uno por destino. Con "→ encargo #N" en
    # singular, un día con dos destinos dejaba la mitad sin rastro en el log.
    log.info("Reporte de correo: %s correos (%s) → encargos %s.",
             len(pendientes), niveles,
             ", ".join(f"#{b} (chat {d})" for d, b in encargos.items()))
    return len(pendientes)
