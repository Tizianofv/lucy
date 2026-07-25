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

Los calendarios NO se hardcodean: se descubren con calendarList (lo que la
cuenta puede ver = lo que Tiziano compartió). Si mañana comparte uno nuevo,
entra solo; si deja de compartir otro, desaparece. La casa se adapta a lo que
él decide, no al revés.

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


async def _calendarios(client: httpx.AsyncClient, token: str) -> list[dict]:
    """Los calendarios que la cuenta ve = los que Tiziano compartió."""
    datos = await _get(client, f"{API}/users/me/calendarList", token)
    cals = []
    for c in datos.get("items", []):
        cals.append({
            "id": c["id"],
            "nombre": c.get("summaryOverride") or c.get("summary") or c["id"],
            "acceso": c.get("accessRole"),  # owner | writer | reader
        })
    return cals


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
        for cal in await _calendarios(client, token):
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
    """Al arrancar: confirma que la key sirve y lista los calendarios visibles.

    Como el chequeo de DeepSeek: una llamada real, no una promesa. Si la cuenta
    no puede ver ningún calendario, algo se rompió en la compartición y hay que
    saberlo ahora, no cuando el briefing salga vacío.
    """
    if not GOOGLE_SA_KEY:
        raise RuntimeError("GOOGLE_SA_KEY vacía: Lucy no puede leer el calendario.")
    token = await _token()
    async with httpx.AsyncClient(timeout=30) as client:
        cals = await _calendarios(client, token)
    return [c["nombre"] for c in cals]
