"""Google Calendar: Lucy lee la agenda real y la vuelca a la tabla `eventos`.

Nivel 4 (omnipresencia), primera pieza. Hasta ahora las citas de Lucy vivían
solo en su Postgres; esto las une con la vida real de Tiziano: sus dos mundos,
el personal (tizianofv) y el del estudio (caribbeandreamstudios), que hoy son
diez calendarios de Google.

Cómo entra sin OAuth que caduque: una CUENTA DE SERVICIO (la lección de
Natalia — el OAuth de Google se vencía cada 7 días y tumbaba medio sistema).
Tiziano COMPARTIÓ cada calendario con la dirección de esa cuenta, como se
comparte con una persona: el personal con permiso de escritura, los del
estudio en solo lectura. La cuenta no caduca nunca.

Por qué los calendarios se listan por ID y NO se autodescubren: compartir un
calendario con una cuenta de servicio le da acceso a sus eventos, pero —a
diferencia de una persona— NO lo agrega a la lista de calendarios de la
cuenta (`calendarList` queda vacía; es un límite conocido de Google, no un
error de la compartición). Así que la cuenta puede LEER cada calendario por
su ID, pero hay que decirle cuáles. Por eso viven en CALENDARIOS. Sumar uno
nuevo = compartirlo con la cuenta + agregar su línea acá.

Esto es LECTURA (los eventos de Google se espejan en `eventos`, marcados con
su gcal_id). Un evento nativo de Lucy —una cita que ella creó por Telegram—
tiene gcal_id NULL y este módulo no lo toca. Así el briefing, el plan semanal,
los choques y las salidas ven UNA sola agenda: la de Google más la de Lucy.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import db.db as db
from config import GOOGLE_SA_KEY, TZ

log = logging.getLogger("lucy.calendario")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
API = "https://www.googleapis.com/calendar/v3"

# Los calendarios que Tiziano compartió con la cuenta de servicio. Se listan
# por ID a propósito (ver el docstring): Google no los autodescubre para una
# cuenta de servicio. `ambito` distingue su mundo personal del estudio, para
# que el agente sepa de dónde viene cada cita.
CALENDARIOS = [
    {"id": "tizianofv@gmail.com",
     "nombre": "Tiziano (personal)", "ambito": "personal"},
    {"id": "caribbeandreamstudios@gmail.com",
     "nombre": "CDS (principal)", "ambito": "estudio"},
    {"id": "3244683b95cf9e097bad11c306a0cddacf9307a46fbe510b1993f3d61080bc29@group.calendar.google.com",
     "nombre": "Bloqueos CDS", "ambito": "estudio"},
    {"id": "c4a8e661ac3d6db4e1c6c7a583f04d21f85c1d5db2aab2458b99396b9dca6d5b@group.calendar.google.com",
     "nombre": "Rosilis", "ambito": "estudio"},
    {"id": "dbpn9pdc8qgc675gnlqlmeg1d0@group.calendar.google.com",
     "nombre": "Calendario Tiziano (estudio)", "ambito": "estudio"},
    {"id": "460c6e48147b09eaa2cd81d5f75725420a502cada46bcdea3cadda661595b27b@group.calendar.google.com",
     "nombre": "CDS GRABACIONES", "ambito": "estudio"},
    {"id": "onauqbbgkqkd4gp1l7dl58rh9k@group.calendar.google.com",
     "nombre": "CDS Sala P", "ambito": "estudio"},
    {"id": "c6f02a737eae3212f6f5299184286777cec4f6f78137418c24c21d9e80fcd6da@group.calendar.google.com",
     "nombre": "CDS Sala R", "ambito": "estudio"},
    {"id": "cd12e934b0d7b88dfc06539cde755ee3efcb6bc4000e4bad776f674451284569@group.calendar.google.com",
     "nombre": "Sala K", "ambito": "estudio"},
    {"id": "66e0c93e0fbe5bc633632c863ed6750abdd98161ae4ee8a728c94f3224821e02@group.calendar.google.com",
     "nombre": "Pasantías", "ambito": "estudio"},
]

# Ventana de sincronización: desde ayer (para no perder algo que empezó y aún
# corre) hasta 60 días adelante. Más lejos no alimenta ni el briefing ni el
# plan semanal, y ensancharía cada sync sin ganar nada.
DIAS_ATRAS = 1
DIAS_ADELANTE = 60

# La cuenta de servicio, cacheada. El objeto refresca su propio access token
# (~1h de vida) por dentro; lo construimos una vez.
_creds = None


def _cargar_creds():
    global _creds
    if _creds is None:
        # Import perezoso: si google-auth no estuviera instalado, que reviente
        # acá (donde se usa) y no al importar el módulo — el resto de Lucy no
        # tiene por qué caerse porque el calendario falte.
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_SA_KEY)
        _creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES)
    return _creds


async def _token() -> str:
    """Un access token válido. Refresca solo si venció.

    El refresh de google-auth es sincrónico (usa requests); va en un hilo para
    no congelar el bucle async ~200ms cada hora.
    """
    creds = _cargar_creds()
    if not creds.valid:
        from google.auth.transport.requests import Request
        await asyncio.to_thread(creds.refresh, Request())
    return creds.token


async def _get(client: httpx.AsyncClient, url: str, token: str,
               params: dict | None = None) -> dict:
    r = await client.get(
        url, params=params, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def _parse_dt(campo: dict | None) -> datetime | None:
    """start/end de Google → datetime. Maneja hora exacta y todo-el-día."""
    if not campo:
        return None
    if campo.get("dateTime"):
        # Trae su propio offset ("...-04:00"); fromisoformat lo respeta.
        return datetime.fromisoformat(campo["dateTime"])
    if campo.get("date"):
        # Evento de todo el día: se ancla a medianoche local de Tiziano.
        return datetime.fromisoformat(campo["date"]).replace(tzinfo=TZ)
    return None


async def _eventos_de(client: httpx.AsyncClient, token: str,
                      cal_id: str) -> list[dict]:
    """Los eventos de un calendario en la ventana, recurrentes ya expandidos."""
    ahora = datetime.now(timezone.utc)
    params = {
        "timeMin": (ahora - timedelta(days=DIAS_ATRAS)).isoformat(),
        "timeMax": (ahora + timedelta(days=DIAS_ADELANTE)).isoformat(),
        "singleEvents": "true",   # expande las recurrencias en instancias reales
        "orderBy": "startTime",
        "maxResults": 250,
        "showDeleted": "true",    # para enterarnos de las cancelaciones
    }
    url = f"{API}/calendars/{quote(cal_id, safe='')}/events"
    items: list[dict] = []
    while True:
        datos = await _get(client, url, token, params)
        items.extend(datos.get("items", []))
        siguiente = datos.get("nextPageToken")
        if not siguiente:
            return items
        params["pageToken"] = siguiente


async def _guardar(cal: dict, ev: dict) -> None:
    """Espeja un evento de Google en `eventos` (upsert por gcal_id).

    Un evento cancelado en Google se archiva acá (soft-delete): la agenda de
    Lucy tiene que reflejar que ya no está, pero sin perder el rastro.
    """
    gcal_id = ev["id"]
    inicia = _parse_dt(ev.get("start"))

    if ev.get("status") == "cancelled" or inicia is None:
        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE eventos SET borrado_en = now() "
                "WHERE gcal_cal_id = %s AND gcal_id = %s AND borrado_en IS NULL",
                (cal["id"], gcal_id))
        return

    titulo = (ev.get("summary") or "(sin título)").strip()
    termina = _parse_dt(ev.get("end"))
    lugar = ev.get("location")

    async with db.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO eventos
              (titulo, inicia_en, termina_en, lugar,
               gcal_id, gcal_cal_id, gcal_calendar)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (gcal_cal_id, gcal_id) WHERE gcal_id IS NOT NULL
            DO UPDATE SET
              titulo = EXCLUDED.titulo, inicia_en = EXCLUDED.inicia_en,
              termina_en = EXCLUDED.termina_en, lugar = EXCLUDED.lugar,
              gcal_calendar = EXCLUDED.gcal_calendar, borrado_en = NULL
            """,
            (titulo, inicia, termina, lugar, gcal_id, cal["id"], cal["nombre"]))


async def sincronizar() -> dict:
    """Jala los eventos de todos los calendarios visibles. Devuelve {cal: N}.

    Rama lateral del bucle: si un calendario falla, se salta y sigue con los
    otros. Un calendario caído no puede llevarse puesta la agenda entera.
    """
    if not GOOGLE_SA_KEY:
        return {}
    token = await _token()
    resumen: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for cal in CALENDARIOS:
            try:
                eventos = await _eventos_de(client, token, cal["id"])
                for ev in eventos:
                    await _guardar(cal, ev)
                resumen[cal["nombre"]] = len(eventos)
            except Exception:
                log.warning("No pude sincronizar '%s'.", cal["nombre"],
                            exc_info=True)
    return resumen


async def verificar() -> list[str]:
    """Al arrancar: confirma que la key sirve Y que cada calendario responde.

    Como el chequeo de DeepSeek: una llamada real, no una promesa. Prueba el
    acceso a cada calendario con una consulta mínima y devuelve los que
    respondieron; si uno falla, se sabe acá y no cuando el briefing salga
    incompleto. Un calendario mudo casi siempre es una compartición que quedó
    a medias.
    """
    if not GOOGLE_SA_KEY:
        raise RuntimeError("GOOGLE_SA_KEY vacía: Lucy no puede leer el calendario.")
    token = await _token()
    vivos = []
    async with httpx.AsyncClient(timeout=30) as client:
        for cal in CALENDARIOS:
            try:
                url = f"{API}/calendars/{quote(cal['id'], safe='')}/events"
                await _get(client, url, token, {"maxResults": 1})
                vivos.append(cal["nombre"])
            except Exception:
                log.warning("Sin acceso al calendario '%s' (¿compartición a "
                            "medias?).", cal["nombre"], exc_info=True)
    if not vivos:
        raise RuntimeError(
            "La cuenta de servicio no pudo leer NINGÚN calendario: revisar que "
            "estén compartidos con lucy-calendar@…gserviceaccount.com")
    return vivos
