"""Quién es de la casa: el registro que evita contar la misma plata dos veces.

Un banco no sabe qué cuentas son tuyas. Te dice "transferencia a ROSILIS YANELY
ROMERO JIMENEZ" igual si le pagaste a un proveedor que si moviste plata de tu
cuenta de APAP a la tuya de Banreservas. Sin saber quién es de la casa, lo
segundo se registra como gasto — y la entrada correspondiente en el otro banco
como ingreso. La misma plata contada dos veces, con el signo cambiado.

Medido sobre los 461 movimientos de los fixtures reales (30-ago-2026):

    22  APAP, marcados gasto, con Rosi como beneficiaria     → traspaso propio
     3  BHD, marcados gasto, contraparte de la casa          → traspaso propio
     2  Banreservas, marcados ingreso, origen de la casa     → traspaso propio
     1  con los dos lados de la casa (RD$44,000)             → traspaso propio
     2  cliente pagando al estudio, marcados gasto           → INGRESO

Esos dos últimos son el caso inverso y el más caro por unidad: un cliente paga
una sesión de grabación y el sistema lo anota como si el estudio hubiera gastado.

EL MATCHING NO PUEDE SER POR NOMBRE COMPLETO. Los bancos escriben el mismo
titular de cinco maneras: "ROSILIS YANELY ROMERO JIMENEZ", "ROSILISYANELY ROMERO
JIMENEZ" (sin espacio), "SRA ROSILIS Y ROMERO", "Rosilis Romero", "ROSILIS
ROMERO". Por eso el registro guarda PATRONES distintivos, no nombres, y compara
sobre el texto sin acentos, sin espacios y en mayúsculas.
"""
from __future__ import annotations

import unicodedata
from dataclasses import replace

from cerebro.bancos.contrato import Movimiento

# Cómo se separan las dos partes cuando un parser conoce origen y destino.
FLECHA = "→"


def normalizar(texto: str) -> str:
    """'SRA Rosilis Y. Romero' → 'SRAROSILISYROMERO'.

    Sin acentos, sin espacios, sin puntuación y en mayúsculas. Quitar los
    espacios es lo que hace que "ROSILISYANELY" —que Banreservas manda pegado—
    case igual que "ROSILIS YANELY".
    """
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if not unicodedata.combining(c))
    return "".join(c for c in sin_acentos.upper() if c.isalnum())


class Propios:
    """Los patrones que identifican a la casa: titulares y cuentas.

    Un patrón es un trozo distintivo del nombre ("ROSILIS", "FAJARDOVARGAS") o
    los últimos dígitos de una cuenta o tarjeta. Se guarda normalizado y se
    busca como subcadena: es lo único que sobrevive a que cada banco escriba el
    nombre a su manera.

    Los patrones cortos son peligrosos —"ANA" casaría con "BANANA"— así que se
    exige un mínimo de longitud al registrarlos.
    """

    # Los nombres necesitan 5 para no casar por accidente ("ANA" cae dentro de
    # "BANANA"). Los números de cuenta llegan enmascarados a cuatro dígitos en
    # los cinco bancos (***8354, ••9639, "terminada en 9854"), y cuatro dígitos
    # son suficientemente específicos: no hay texto de nombre que los contenga.
    LARGO_MINIMO = 5
    LARGO_MINIMO_DIGITOS = 4

    def __init__(self, patrones: list[str] | None = None):
        self.patrones: list[str] = []
        for p in (patrones or []):
            self.agregar(p)

    def agregar(self, patron: str) -> None:
        norm = normalizar(patron)
        minimo = (self.LARGO_MINIMO_DIGITOS if norm.isdigit()
                  else self.LARGO_MINIMO)
        if len(norm) < minimo:
            raise ValueError(
                f"patrón {patron!r} demasiado corto ({len(norm)} < {minimo}): "
                "casaría con nombres ajenos por accidente")
        if norm not in self.patrones:
            self.patrones.append(norm)

    def es_de_la_casa(self, texto: str) -> bool:
        if not texto or not texto.strip():
            return False
        norm = normalizar(texto)
        return any(p in norm for p in self.patrones)

    # ── La decisión ──────────────────────────────────────────────────────

    def reclasificar(self, mov: Movimiento) -> Movimiento:
        """Devuelve el movimiento con el `tipo` corregido según quién es quién.

        Reglas, en orden:
          · Los DOS lados de la casa → transferencia. No entra ni sale plata del
            conjunto; solo cambia de bolsillo.
          · Solo el ORIGEN de la casa → gasto. Le pagamos a alguien.
          · Solo el DESTINO de la casa → ingreso. Alguien nos pagó.
          · Sin flecha (el parser solo conoce una contraparte) y esa contraparte
            es de la casa → transferencia: el otro extremo somos nosotros por
            definición, porque el correo llegó a un buzón nuestro.

        Lo que NO hace: tocar los movimientos de tarjeta. Un consumo en un
        comercio es un gasto aunque el comercio se llame como alguien de la
        casa, y `canal='tarjeta'` lo dice sin ambigüedad.
        """
        if mov.canal == "tarjeta":
            return mov

        if FLECHA in mov.contraparte:
            origen, _, destino = mov.contraparte.partition(FLECHA)
            o, d = self.es_de_la_casa(origen), self.es_de_la_casa(destino)
            if o and d:
                nuevo = "transferencia"
            elif o:
                nuevo = "gasto"
            elif d:
                nuevo = "ingreso"
            else:
                return mov
        else:
            if not self.es_de_la_casa(mov.contraparte):
                return mov
            nuevo = "transferencia"

        return mov if nuevo == mov.tipo else replace(mov, tipo=nuevo)


def desde_filas(filas) -> Propios:
    """Construye el registro desde las filas de la tabla `cuentas_propias`.

    Se separa de la clase para que el matcher siga siendo lógica pura y
    testeable sin base de datos.
    """
    reg = Propios()
    for f in filas:
        patron = f["patron"] if isinstance(f, dict) else f[0]
        reg.agregar(patron)
    return reg
