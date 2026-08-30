"""Descubrimiento de correo bancario — paso previo a escribir cualquier parser.

Antes de escribir un parser hay que saber QUÉ hay: cuáles bancos escriben de
verdad, a qué buzón, con qué remitente exacto y en qué volumen. Un banco que
manda tres correos al año no merece un parser; uno que manda cuatro al día es
el 80% del valor.

Hace dos pasadas, en este orden:

  1. DESCUBRIR (por defecto) — busca en cada buzón por los dominios de los
     bancos conocidos, y además explora términos genéricos ("banco", "alerta",
     "consumo"…) para sacar a la luz remitentes que no estén en la lista.
     Reporta remitente exacto + conteo + un asunto de muestra. No baja cuerpos.

  2. VOLCAR (--volcar DOMINIO) — de un remitente ya confirmado, baja los
     mensajes CRUDOS a tests/fixtures/<banco>/. Eso es el set de pruebas del
     parser y, de paso, un respaldo de los datos fuente.

REGLAS DE SEGURIDAD DE ESTE SCRIPT:
  · La sesión IMAP es SIEMPRE readonly y usa BODY.PEEK — nunca marca un correo
    como leído. Mirar no puede cambiar el buzón.
  · No toca la base de datos. No importa nada de captura/ ni de db/.
  · No llama a ninguna IA. Todo es búsqueda IMAP y conteo local.
  · Lee las credenciales de CORREO_CUENTAS (la misma variable que usa Lucy en
    Railway). No las pide por parámetro ni las imprime.

USO:
    export CORREO_CUENTAS='[{"user":"...","pass":"..."}, ...]'   # o un .env
    python tools/descubrir_bancos.py
    python tools/descubrir_bancos.py --volcar bhd.com.do --banco bhd
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

SERVIDOR = "imap.gmail.com"

# Los bancos que Tiziano nombró. El dominio es la ÚNICA llave confiable para
# enrutar: "popular" como substring es ambiguo entre Banco Popular Dominicano y
# APAP (Asociación Popular de Ahorros y Préstamos), que son instituciones
# distintas. Por eso acá se busca por dominio y nunca por nombre suelto.
BANCOS_CONOCIDOS = {
    "bhd":         ["bhd.com.do"],
    "banreservas": ["banreservas.com", "banreservas.com.do"],
    "banesco":     ["banesco.com.do"],
    "popular":     ["popularenlinea.com", "bpd.com.do", "popular.com.do"],
    "apap":        ["apap.com.do"],
}

# Red de arrastre para remitentes que no estén arriba. Tiziano dijo "pueden
# haber más", así que esto existe para encontrar lo que él mismo no recuerda.
# Van contra el campo FROM salvo los que dicen ASUNTO.
EXPLORATORIOS_FROM = ["banco", "banca", "alerta", "notifica", "tarjeta",
                      "scotiabank", "santacruz", "promerica", "lafise",
                      "caribe", "ademi", "vimenca", "bancamerica"]
EXPLORATORIOS_ASUNTO = ["consumo", "transacc", "tarjeta de credito",
                        "transferencia", "notificacion de"]

# Cuánto historial MIRA este script. Ojo con la distinción, que Tiziano marcó
# el 30-ago: el sistema en producción arranca en septiembre y no importa el
# pasado a `movimientos`. Pero este script no importa nada — lee para aprender
# el formato del banco, que es distinto. Los casos que rompen un parser (un
# consumo en USD, un correo con dos transacciones, una declinada, un reverso,
# un monto de más de mil) aparecen una vez cada varios meses: con una semana de
# muestra no se ven, y el parser sale mal probado. Un año de historial es el
# manual de formato que el banco no publica.
VENTANA_DIAS = 365

_MESES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fecha_imap(dt: datetime) -> str:
    return f"{dt.day:02d}-{_MESES[dt.month - 1]}-{dt.year}"


def _texto(v: str | None) -> str:
    """Cabecera MIME (=?utf-8?...) → texto legible."""
    if not v:
        return ""
    return "".join(
        (p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p)
        for p, enc in decode_header(v)
    )


def _cuentas() -> list[dict]:
    """Las cuentas desde el entorno. Igual que config.py, pero sin importarlo:
    este script tiene que correr sin el resto de las variables de Lucy."""
    crudo = os.environ.get("CORREO_CUENTAS", "")
    if not crudo:
        # Cortesía: si hay un .env al lado, lo lee sin traer dependencias.
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for linea in env.read_text(encoding="utf-8").splitlines():
                if linea.strip().startswith("CORREO_CUENTAS="):
                    crudo = linea.split("=", 1)[1].strip().strip("'\"")
                    break
    if not crudo:
        sys.exit("Falta CORREO_CUENTAS (variable de entorno o .env en la raíz).")
    try:
        cuentas = json.loads(crudo)
    except ValueError as e:
        sys.exit(f"CORREO_CUENTAS no es JSON válido: {e}")
    if not isinstance(cuentas, list) or not cuentas:
        sys.exit("CORREO_CUENTAS debe ser una lista no vacía.")
    return cuentas


def _conectar(cuenta: dict) -> imaplib.IMAP4_SSL:
    M = imaplib.IMAP4_SSL(SERVIDOR, 993)
    M.login(cuenta["user"], cuenta["pass"])
    M.select("INBOX", readonly=True)   # readonly: mirar no cambia el buzón
    return M


def _buscar(M, campo: str, termino: str, desde: str) -> list[bytes]:
    """UID SEARCH tolerante: un fallo devuelve vacío en vez de tumbar el barrido."""
    try:
        typ, data = M.uid("search", None, campo, f'"{termino}"', "SINCE", desde)
        return data[0].split() if data and data[0] else []
    except Exception as e:
        print(f"      (búsqueda {campo}:{termino} falló: {e})")
        return []


def _cabeceras(M, uid: bytes) -> tuple[str, str, str]:
    """From, Subject y Date de un uid. PEEK: no marca leído."""
    try:
        d = M.uid("fetch", uid.decode(),
                  "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")[1]
        if not d or not d[0]:
            return "", "", ""
        msg = email.message_from_bytes(d[0][1])
        return (_texto(msg.get("From")), _texto(msg.get("Subject")),
                _texto(msg.get("Date")))
    except Exception:
        return "", "", ""


def _dominio(remitente: str) -> str:
    """'BHD <Alertas@bhd.com.do>' → 'bhd.com.do'."""
    r = remitente
    if "<" in r:
        r = r.split("<", 1)[1].split(">", 1)[0]
    return r.split("@")[-1].strip("> ").lower() if "@" in r else "(sin dominio)"


def descubrir() -> None:
    desde = _fecha_imap(datetime.now() - timedelta(days=VENTANA_DIAS))
    print(f"Barrido de los últimos {VENTANA_DIAS} días (desde {desde}).\n")

    # dominio → {cuenta: conteo}, y una muestra de asunto por dominio
    hallazgos: dict[str, Counter] = {}
    muestras: dict[str, tuple[str, str]] = {}
    remitentes: dict[str, Counter] = {}

    for cuenta in _cuentas():
        user = cuenta.get("user", "?")
        print(f"── {user}")
        try:
            M = _conectar(cuenta)
        except Exception as e:
            print(f"   NO PUDE ENTRAR: {type(e).__name__}: {e}\n")
            continue
        try:
            uids: set[bytes] = set()
            for dominios in BANCOS_CONOCIDOS.values():
                for d in dominios:
                    uids |= set(_buscar(M, "FROM", d, desde))
            for t in EXPLORATORIOS_FROM:
                uids |= set(_buscar(M, "FROM", t, desde))
            for t in EXPLORATORIOS_ASUNTO:
                uids |= set(_buscar(M, "SUBJECT", t, desde))

            print(f"   {len(uids)} correos candidatos; leyendo cabeceras…")
            for uid in uids:
                frm, asunto, fecha = _cabeceras(M, uid)
                if not frm:
                    continue
                dom = _dominio(frm)
                hallazgos.setdefault(dom, Counter())[user] += 1
                remitentes.setdefault(dom, Counter())[frm.strip()] += 1
                muestras.setdefault(dom, (asunto, fecha))
        finally:
            try:
                M.logout()
            except Exception:
                pass
        print()

    if not hallazgos:
        print("Nada encontrado. Revisa que las credenciales sean correctas.")
        return

    print("═" * 72)
    print("RESULTADO — dominios ordenados por volumen\n")
    orden = sorted(hallazgos.items(), key=lambda kv: -sum(kv[1].values()))
    for dom, porcuenta in orden:
        total = sum(porcuenta.values())
        banco = next((b for b, ds in BANCOS_CONOCIDOS.items()
                      if any(d in dom for d in ds)), None)
        etiqueta = f"[{banco}]" if banco else "[desconocido]"
        print(f"{total:>5}  {dom:<32} {etiqueta}")
        for u, n in porcuenta.most_common():
            print(f"       · {n:>4} en {u}")
        for rem, n in remitentes[dom].most_common(3):
            print(f"       remitente: {rem}  ({n})")
        asunto, fecha = muestras[dom]
        print(f"       muestra:   {asunto[:70]}")
        print()

    print("Siguiente paso: confirma cuáles son bancos de verdad y volcá uno:")
    print("  python tools/descubrir_bancos.py --volcar <dominio> --banco <slug>")


def volcar(dominio: str, banco: str, limite: int) -> None:
    """Baja los mensajes CRUDOS de un remitente a tests/fixtures/<banco>/."""
    destino = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / banco
    destino.mkdir(parents=True, exist_ok=True)
    desde = _fecha_imap(datetime.now() - timedelta(days=VENTANA_DIAS))
    total = 0

    for cuenta in _cuentas():
        user = cuenta.get("user", "?")
        try:
            M = _conectar(cuenta)
        except Exception as e:
            print(f"── {user}: no pude entrar ({e})")
            continue
        try:
            uids = _buscar(M, "FROM", dominio, desde)[-limite:]
            print(f"── {user}: {len(uids)} mensajes")
            for uid in uids:
                d = M.uid("fetch", uid.decode(), "(BODY.PEEK[])")[1]
                if not d or not d[0]:
                    continue
                # El nombre lleva cuenta y uid: así el fixture dice de dónde
                # salió, y dos cuentas no se pisan el mismo número.
                slug = user.split("@")[0]
                (destino / f"{slug}_{uid.decode()}.eml").write_bytes(d[0][1])
                total += 1
        finally:
            try:
                M.logout()
            except Exception:
                pass

    print(f"\n{total} mensajes crudos en {destino}")
    print("OJO: son correos reales con datos financieros. Ya están cubiertos por")
    print("el .gitignore que te dejé — verifica con `git status` antes de commitear.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--volcar", metavar="DOMINIO",
                   help="baja los mensajes crudos de este dominio")
    p.add_argument("--banco", metavar="SLUG",
                   help="carpeta destino en tests/fixtures/ (con --volcar)")
    p.add_argument("--limite", type=int, default=200,
                   help="máximo de mensajes a volcar por cuenta (def. 200)")
    p.add_argument("--desde", type=int, default=VENTANA_DIAS, metavar="DIAS",
                   help=f"cuántos días de historial mirar (def. {VENTANA_DIAS}). "
                        "Acortarlo NO cambia qué se importa a la base —este "
                        "script no escribe en la base— solo achica la muestra "
                        "con la que se prueba el parser.")
    a = p.parse_args()

    VENTANA_DIAS = a.desde
    if a.volcar:
        if not a.banco:
            sys.exit("--volcar necesita también --banco (ej: --banco bhd)")
        volcar(a.volcar, a.banco, a.limite)
    else:
        descubrir()
