"""El contrato que todos los parsers de banco tienen que cumplir.

Esta es la pieza que NO se puede paralelizar: si cada banco inventara su
propia forma de salida, juntarlos después sería reescribirlos. Un parser
recibe un correo ya normalizado y devuelve una LISTA de Movimiento —lista y
no uno solo, porque un correo puede traer varias transacciones—. Nada más.
No toca la base, no llama a la IA, no sabe qué es Lucy.

Las tres normalizaciones viven acá a propósito, porque son donde se esconden
los errores caros:

  · MONEDA — BHD dice "RD" y "US", otro banco dirá "RD$" o "DOP". Si cada
    parser emite su propio string, sumar gastos mezcla pesos con dólares y
    el total no significa nada. Sale ISO o no sale. (En la muestra de BHD,
    33 de 161 consumos son en USD: esto no es hipotético.)

  · MONTO — el error de 1000×. "$2,500.00" es dos mil quinientos, pero
    "2.500" podría ser dos mil quinientos O dos con cincuenta según el país.
    Ante un caso genuinamente ambiguo esto REVIENTA en vez de adivinar: un
    parser que falla se arregla, uno que calla multiplica por mil en
    silencio y nadie se entera hasta que los totales no cuadran.

  · FECHA — hora local de Santo Domingo. La tabla `movimientos` guarda DATE
    (pierde la hora), así que la hora exacta viaja en `referencia` para que
    el dedupe pueda distinguir dos compras iguales del mismo día.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

# ── Vocabularios cerrados ────────────────────────────────────────────────
# Cerrados a propósito: un valor fuera de estos conjuntos es un parser mal
# escrito, y vale más que reviente en los tests que que ensucie la base.

MONEDAS = ("DOP", "USD")

# `tipo` es el de la tabla `movimientos` (gasto | ingreso | transferencia),
# con el monto SIEMPRE positivo y el signo dado por el tipo. `transferencia`
# es el movimiento que no es ni gasto ni ingreso: mover plata entre cuentas
# propias. Esa distinción es la que evita contar doble — pagar la tarjeta no
# es un gasto nuevo, el gasto ya se contó cuando se pasó la tarjeta.
TIPOS = ("gasto", "ingreso", "transferencia")

# El canal es más fino que el tipo y no va a la columna `tipo`: sirve para
# saber de dónde salió el dato y para depurar. Viaja en `referencia`.
CANALES = ("tarjeta", "transferencia", "traspaso", "nomina", "servicio", "interes")

ESTADOS = ("aprobada", "declinada", "reversada", "pendiente")


class ErrorDeParseo(ValueError):
    """Un correo que no se pudo convertir en movimiento.

    Se usa para TODO lo que sale mal, y el mensaje tiene que decir qué campo
    y con qué texto falló: es lo que después alimenta la alerta de "el banco
    cambió el formato".
    """


@dataclass(frozen=True)
class Movimiento:
    """Una transacción, ya normalizada y lista para insertarse.

    Se valida sola al construirse: si un parser produce basura, revienta acá
    y no tres capas más abajo cuando ya está a medio guardar.
    """
    banco: str          # bhd | banesco | banreservas | apap
    canal: str          # uno de CANALES
    tipo: str           # uno de TIPOS — el de la columna `movimientos.tipo`
    fecha: datetime     # hora local de Santo Domingo
    monto: Decimal      # SIEMPRE positivo
    moneda: str         # DOP | USD
    contraparte: str    # el comercio si sale, quién pagó si entra
    estado: str         # uno de ESTADOS
    referencia: str     # últimos dígitos, ref del banco, hora exacta

    def __post_init__(self) -> None:
        if self.moneda not in MONEDAS:
            raise ErrorDeParseo(
                f"moneda '{self.moneda}' no es ISO; usá normalizar_moneda()")
        if self.tipo not in TIPOS:
            raise ErrorDeParseo(f"tipo '{self.tipo}' no está en {TIPOS}")
        if self.canal not in CANALES:
            raise ErrorDeParseo(f"canal '{self.canal}' no está en {CANALES}")
        if self.estado not in ESTADOS:
            raise ErrorDeParseo(f"estado '{self.estado}' no está en {ESTADOS}")
        if not isinstance(self.monto, Decimal):
            raise ErrorDeParseo("monto tiene que ser Decimal, no float")
        if self.monto <= 0:
            raise ErrorDeParseo(
                f"monto {self.monto} no es positivo; el signo lo da `tipo`")
        if not self.contraparte.strip():
            raise ErrorDeParseo("contraparte vacía")

    def clave_dedupe(self) -> str:
        """Huella del contenido, para detectar el mismo movimiento llegando
        dos veces (un reenvío del banco, o la misma alerta a dos buzones).

        Incluye el banco porque dos bancos podrían producir por casualidad
        la misma tripleta fecha+monto+comercio. Incluye la hora exacta
        porque `movimientos.fecha` es DATE y sin ella dos cafés del mismo
        día serían indistinguibles.
        """
        return "|".join((
            self.banco, self.fecha.isoformat(timespec="minutes"),
            str(self.monto), self.moneda,
            _sin_acentos(self.contraparte).upper(),
        ))


# ── Normalizaciones ──────────────────────────────────────────────────────

def _sin_acentos(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", v)
                   if not unicodedata.combining(c))


# Las notaciones que mandan los bancos de verdad. Banreservas usa TRES distintas
# dentro del mismo remitente y el mismo asunto: "DOP 254.90" en los consumos,
# "RD$ 1,500.00" en las transferencias recibidas y "DOP$ 12,267.85" en la nómina.
# Esa última —código ISO pegado al símbolo— es la que nadie inventaría de cabeza.
_MONEDAS_CONOCIDAS = {
    "RD": "DOP", "RD$": "DOP", "DOP": "DOP", "DOP$": "DOP", "PESOS": "DOP",
    "$RD": "DOP", "RD$$": "DOP",
    "US": "USD", "US$": "USD", "USD": "USD", "USD$": "USD", "DOLARES": "USD",
    "$US": "USD",
}


def normalizar_moneda(texto: str) -> str:
    """'RD' → 'DOP', 'US$' → 'USD'. Revienta si no la conoce.

    Deliberadamente NO tiene un valor por defecto. Asumir pesos cuando el
    banco dijo otra cosa es el error más caro posible acá: convierte 100
    dólares en 100 pesos y nadie lo nota.
    """
    limpio = _sin_acentos((texto or "").strip().upper()).replace(" ", "")
    if limpio in _MONEDAS_CONOCIDAS:
        return _MONEDAS_CONOCIDAS[limpio]
    raise ErrorDeParseo(
        f"moneda desconocida: {texto!r}. Agregala a _MONEDAS_CONOCIDAS si es "
        "legítima — no la asumas.")


def normalizar_monto(texto: str) -> Decimal:
    """'$2,500.00' → Decimal('2500.00'). Ante la duda, revienta.

    El separador DECIMAL es el último que aparece: en "2,500.00" manda el
    punto, en "2.500,00" manda la coma. Con un solo separador hay un caso
    genuinamente ambiguo —"2.500" puede ser 2500 o 2.50— y ahí esto NO
    adivina: si hay exactamente 3 dígitos después, es separador de miles
    (convención universal); con 1 o 2 dígitos es decimal; con cualquier otra
    cantidad, error. Preferimos un parser que falla a uno que multiplica por
    mil sin avisar.
    """
    crudo = (texto or "").strip()
    limpio = re.sub(r"[^\d.,]", "", crudo)
    if not limpio or not any(c.isdigit() for c in limpio):
        raise ErrorDeParseo(f"no encontré ningún número en {crudo!r}")

    # Un número TIENE que terminar en dígito. Sin esta guarda, "254.90." entraba
    # por la rama de "miles repetido" (porque hay un punto también en la parte
    # entera), se le borraban todos los separadores y salía 25490: un ×100 en
    # silencio. Y con la asimetría más fea posible — los montos de cuatro cifras
    # sí reventaban, así que fallaba justo en los cotidianos y se portaba bien en
    # los grandes. Lo encontró un subagente-testigo el 30-ago sobre este mismo
    # módulo, cuyo docstring prometía exactamente lo contrario.
    if not limpio[-1].isdigit():
        raise ErrorDeParseo(
            f"{crudo!r} no termina en dígito. Casi siempre es que el patrón que "
            "lo capturó se llevó puntuación de la frase; ancla el monto a "
            r"[\d.,]*\d en vez de [\d.,]+.")

    ult_coma, ult_punto = limpio.rfind(","), limpio.rfind(".")

    if ult_coma >= 0 and ult_punto >= 0:
        # Los dos presentes: el último manda como decimal, el otro es miles.
        if ult_coma > ult_punto:
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif ult_coma >= 0 or ult_punto >= 0:
        sep = "," if ult_coma >= 0 else "."
        entero, _, resto = limpio.rpartition(sep)
        if sep in entero:                      # "1.234.567" → miles repetido
            limpio = limpio.replace(sep, "")
        elif len(resto) == 3:                  # "2.500" → dos mil quinientos
            limpio = limpio.replace(sep, "")
        elif len(resto) in (1, 2):             # "2.5" / "2.50" → decimal
            limpio = f"{entero}.{resto}"
        else:
            raise ErrorDeParseo(
                f"monto ambiguo {crudo!r}: {len(resto)} dígitos después de "
                f"'{sep}' no es ni decimal ni miles. No lo voy a adivinar.")

    try:
        valor = Decimal(limpio)
    except InvalidOperation as e:
        raise ErrorDeParseo(f"no pude convertir {crudo!r} a número") from e
    if valor <= 0:
        raise ErrorDeParseo(f"monto {valor} no es positivo (de {crudo!r})")
    return valor


_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def normalizar_fecha(texto: str) -> datetime:
    """Fecha del banco → datetime local. Acepta los formatos vistos hasta hoy.

    Todo lo que llega es hora de Santo Domingo (los bancos son locales), así
    que el datetime sale naive y se interpreta en TZ local. No inventamos un
    offset que el banco no mandó.
    """
    crudo = (texto or "").strip()

    # "08/07/2026 04:07 pm" (BHD, Banreservas) y "06/05/26" (Banesco, año de
    # dos dígitos). El año corto se expande a 20xx: son correos de alertas
    # bancarias, no registros históricos — no hay ningún 1926 posible acá.
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)"
                  r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap])\.?m\.?)?",
                  crudo, re.I)
    if m:
        dd, mm, yyyy, hh, mi, ss, ampm = m.groups()
        if len(yyyy) == 2:
            yyyy = f"20{yyyy}"
        hora = int(hh) if hh else 0
        if ampm:
            if ampm.lower() == "p" and hora != 12:
                hora += 12
            elif ampm.lower() == "a" and hora == 12:
                hora = 0
        return datetime(int(yyyy), int(mm), int(dd), hora,
                        int(mi or 0), int(ss or 0))

    # Mes en letras: "8 de julio de 2026", "08-jul-2026" y el formato de la App
    # de Banreservas, "02 de Marzo 2026 - 09:03 PM" (sin "de" antes del año, y
    # con la hora colgando de un guion).
    # "de"/"del" antes del año, y la marca de tarde con espacios y puntos
    # sueltos: los comprobantes de sucursal de Banreservas escriben
    # "16 DE ABRIL DEL 2026 - 12:30 P. M.".
    m = re.search(r"(\d{1,2})\s*(?:del?\s+|-)([a-z]+)\.?\s*(?:del?\s+|-|\s)\s*(\d{4})"
                  r"(?:\s*-\s*(\d{1,2}):(\d{2})\s*([ap])\s*\.?\s*m\s*\.?)?",
                  _sin_acentos(crudo).lower())
    if m:
        dd, mes_txt, yyyy, hh, mi, ampm = m.groups()
        for nombre, num in _MESES_ES.items():
            if _sin_acentos(nombre).startswith(mes_txt[:3]):
                hora = int(hh) if hh else 0
                if ampm:
                    if ampm == "p" and hora != 12:
                        hora += 12
                    elif ampm == "a" and hora == 12:
                        hora = 0
                return datetime(int(yyyy), num, int(dd), hora, int(mi or 0))

    raise ErrorDeParseo(f"no reconozco la fecha {crudo!r}")


def normalizar_estado(texto: str) -> str:
    """'Aprobada' → 'aprobada'. Lo desconocido revienta, no cae en 'aprobada'.

    Importa: dar por aprobada una transacción declinada infla los gastos con
    plata que nunca salió.
    """
    t = _sin_acentos((texto or "").strip().lower())
    if not t:
        raise ErrorDeParseo("estado vacío")
    if t.startswith("aprob") or t in ("exitosa", "completada", "procesada"):
        return "aprobada"
    if t.startswith("declin") or t.startswith("rechaz") or t.startswith("denegad"):
        return "declinada"
    if t.startswith("revers") or t.startswith("anulad") or t.startswith("devuelt"):
        return "reversada"
    if t.startswith("pendien") or t.startswith("en proceso"):
        return "pendiente"
    raise ErrorDeParseo(f"estado desconocido: {texto!r}")


# ── El correo que recibe un parser ───────────────────────────────────────

@dataclass(frozen=True)
class CorreoCrudo:
    """Un correo ya desarmado, para que el parser no repita ese trabajo.

    Trae HTML *y* texto plano a propósito: algunos bancos mandan tabla HTML
    y otros solo texto, y algunos ponen el monto en el asunto. Firmar esto
    como "HTML entra" nos obligaría a romper el contrato en el segundo banco.
    """
    remitente: str      # dirección sola, en minúsculas
    asunto: str
    fecha_correo: datetime
    html: str
    texto: str
    cuenta: str         # buzón donde llegó — sirve para atribuir a CDS/ACD
    uid: str


# ── Registro de parsers ──────────────────────────────────────────────────
#
# El enrutamiento va por REMITENTE COMPLETO, nunca por dominio ni por
# substring del nombre. Dos razones que salieron de los correos reales:
#
#   · bhd.com.do tiene tres remitentes y solo `alertas@` es transaccional:
#     `info@` es publicidad y `infopb@` es el Puesto de Bolsa.
#   · APAP usa `no-reply@` para transacciones y `noreply@` para publicidad.
#     Un enrutador que normalizara el guión los mezclaría.

Parser = Callable[[CorreoCrudo], list[Movimiento]]
_REGISTRO: list[tuple[str, re.Pattern | None, Parser]] = []

# Asuntos que traen un monto pero NO son un movimiento. Se listan explícito
# para que nadie los parsee por accidente: un código de validación de compra
# lleva el monto de una compra que todavía no ocurrió.
ASUNTOS_IGNORADOS = (
    re.compile(r"c[oó]digo de validaci[oó]n", re.I),
    re.compile(r"afiliaci[oó]n nuevo beneficiario", re.I),
    re.compile(r"estado de cuenta", re.I),
    re.compile(r"cambio de contrase[nñ]a|nueva contrase[nñ]a", re.I),
)


def registrar(remitente: str, parser: Parser, asunto: str | None = None) -> None:
    """Ata un parser a un remitente exacto, opcionalmente filtrando por asunto.

    Un mismo remitente puede tener varios parsers: `alertas@bhd.com.do` manda
    consumos de tarjeta Y traspasos entre cuentas propias, con formatos y
    significados distintos. El orden de registro NO importa (ver buscar_parser).
    """
    patron = re.compile(asunto, re.I) if asunto else None
    _REGISTRO.append((remitente.strip().lower(), patron, parser))


def buscar_parser(remitente: str, asunto: str) -> Parser | None:
    """El parser que corresponde, o None si este correo no se parsea.

    Lo ESPECÍFICO gana sobre lo general, sin depender del orden de registro:
    primero se buscan los parsers que además exigen un asunto, y solo si
    ninguno calza se cae al parser general del remitente. Resolverlo por orden
    de registro haría que el comportamiento dependiera de en qué orden se
    importan los módulos de banco — un footgun que este proyecto no necesita.
    """
    if any(p.search(asunto or "") for p in ASUNTOS_IGNORADOS):
        return None
    rem = (remitente or "").strip().lower()
    general = None
    for esperado, patron, parser in _REGISTRO:
        if rem != esperado:
            continue
        if patron is not None:
            if patron.search(asunto or ""):
                return parser
        elif general is None:
            general = parser
    return general


def parsear(correo: CorreoCrudo) -> list[Movimiento]:
    """Convierte un correo en movimientos. Lista vacía = no aplica ningún parser.

    Un ErrorDeParseo sube tal cual: quien llama decide si lo registra como
    fallo (y alimenta la alerta de cambio de formato) o si reintenta. Tragarlo
    acá convertiría "el banco cambió la plantilla" en "hoy no hubo gastos".
    """
    parser = buscar_parser(correo.remitente, correo.asunto)
    if parser is None:
        return []
    return parser(correo)


def remitentes_registrados() -> Iterable[str]:
    """Para la búsqueda IMAP: a quién hay que ir a buscar."""
    return sorted({rem for rem, _, _ in _REGISTRO})
