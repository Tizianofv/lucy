"""Tránsito real: cuánto se tarda AHORA, según Google Routes API.

Antes de esto, Lucy sabía cuánto tarda una ruta porque se lo preguntó a
Tiziano una vez ("De CDS al estudio: 45 min") — un asistente humano nuevo.
Con esto pasa a saberlo como un asistente con celular: consultando el
tráfico del momento. Las rutas aprendidas quedan como respaldo: si Maps
falla o la key no está, Lucy degrada a lo que sabía, no a la nada.

El resultado es un TEXTO para el agente, no una excepción: los errores son
información y él decide — usar la ruta guardada, preguntar, o avisar sin
precisión. Mismo contrato que todas sus herramientas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

import db.db as db
from config import GOOGLE_MAPS_API_KEY

log = logging.getLogger("lucy.viaje")

URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Solo pedimos lo que usamos: la máscara de campos es obligatoria en Routes
# API y de paso evita pagar (en bytes y en latencia) por datos que no leemos.
MASCARA = "routes.duration,routes.distanceMeters"


async def _punto(nombre_o_direccion: str) -> dict | None:
    """Un destino puede ser un lugar con nombre ("el estudio") o una dirección.

    Primero se busca en los lugares de Tiziano: sus nombres le ganan a
    cualquier geocodificador ("el estudio" no significa nada para Google,
    pero para Lucy son coordenadas exactas).
    """
    fila = await db.lugar_por_nombre(nombre_o_direccion)
    if fila:
        return {"location": {"latLng": {
            "latitude": fila["lat"], "longitude": fila["lon"]}}}
    if nombre_o_direccion.strip():
        return {"address": nombre_o_direccion.strip()}
    return None


async def calcular(destino: str, desde: str | None = None) -> str:
    """Minutos con tráfico de ahora, de donde está Tiziano hasta el destino.

    `desde` es opcional: sin él se usa la última ubicación compartida. Con él
    ("desde CDS") se resuelve contra los lugares con nombre.
    """
    if not GOOGLE_MAPS_API_KEY:
        return ("ERROR: no hay GOOGLE_MAPS_API_KEY configurada. Usá la ruta "
                "guardada en notas (etiqueta 'ruta') o preguntale a Tiziano "
                "cuánto tarda.")

    # ── Origen ───────────────────────────────────────────────────────────
    if desde:
        origen = await _punto(desde)
        if origen is None:
            return f"ERROR: no conozco ningún lugar llamado '{desde}'."
    else:
        u = await db.ultima_ubicacion()
        if u is None:
            return ("ERROR: no sé dónde está Tiziano (nunca compartió "
                    "ubicación). Pedile un pin o desde dónde sale.")
        origen = {"location": {"latLng": {
            "latitude": u["lat"], "longitude": u["lon"]}}}
        if u["hace_min"] > 90:
            # Se calcula igual, pero el agente tiene que saber que el punto
            # de partida es dudoso: mejor un dato con advertencia que una
            # certeza falsa.
            desde = f"su última ubicación (de hace {u['hace_min']} min, OJO)"

    destino_punto = await _punto(destino)
    if destino_punto is None:
        return "ERROR: el destino vino vacío."

    # ── Routes API ───────────────────────────────────────────────────────
    # TRAFFIC_AWARE_OPTIMAL + departureTime es lo que usa la app de Maps: el
    # modelo de tráfico en vivo de verdad, no la versión liviana. La diferencia
    # se nota en hora pico. departureTime va ~1 min al futuro porque Google lo
    # exige presente-o-futuro y la latencia de red podría dejarlo en el pasado.
    salida = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    cuerpo = {
        "origin": origen,
        "destination": destino_punto,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": salida,
    }
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(
            URL,
            json=cuerpo,
            headers={
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": MASCARA,
            },
        )

    if r.status_code != 200:
        # El motivo de Google viaja en el cuerpo. Un "403" pelado no le dice
        # nada a nadie; "403: facturación deshabilitada" le dice a Tiziano
        # exactamente qué botón le faltó tocar en Google Cloud.
        try:
            motivo = (r.json().get("error") or {}).get("message") or r.text[:150]
        except Exception:
            motivo = r.text[:150]
        log.warning("Routes API %s: %s", r.status_code, motivo)
        return (f"ERROR: Maps respondió {r.status_code}: {motivo[:200]} — "
                f"usá la ruta guardada en notas o preguntale a Tiziano.")

    rutas = (r.json() or {}).get("routes") or []
    if not rutas:
        return (f"ERROR: Maps no encontró ruta hasta '{destino}'. ¿La "
                f"dirección está completa? Probá con más detalle o preguntá.")

    seg = int(str(rutas[0].get("duration", "0s")).rstrip("s") or 0)
    km = (rutas[0].get("distanceMeters") or 0) / 1000
    minutos = max(1, round(seg / 60))

    origen_txt = f" desde {desde}" if desde else ""
    return (f"OK: ~{minutos} min con el tráfico de ahora ({km:.1f} km)"
            f"{origen_txt} hasta {destino}.")
