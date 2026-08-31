"""Categorizar comercios aprendiendo de las correcciones, no adivinando.

La forma barata de hacer esto es una lista de palabras clave, y funciona mal
para siempre: el comercio nuevo nunca está, y el que está casa por accidente.
La forma cara es preguntarle a un modelo cada vez, y eso cuesta dinero y no es
determinista.

Acá se hace la tercera: el sistema recuerda lo que ya se corrigió. Tiziano
arregla una categoría en el panel y ese comercio queda clasificado para siempre.

CUÁNTO CUBRE, medido sobre los 323 consumos reales del corpus, corrigiendo
siempre el comercio más frecuente que quede sin clasificar:

     1 corrección  →   7%      20 correcciones →  44%
     5             →  20%      30              →  54%
    10             →  29%      50              →  67%

O sea: es una cola larga, no una curva que se cierra sola. Veinte correcciones
—un rato una vez— cubren casi la mitad del gasto, y de ahí en adelante rinde
cada vez menos. Por eso las palabras clave siguen existiendo como red: para la
cola, corregir a mano nunca va a alcanzar. Conviene saberlo antes de prometerse
que "el sistema aprende solo".

LA CLAVE ES LA NORMALIZACIÓN. Los bancos escriben el mismo comercio de maneras
distintas según la sucursal, el terminal y el día:

    SM NACIONAL MAXIMO GOM SANTO DOMINGODO
    SM NACIONAL MAXIMO GOM
    SUPERMERCADO NACIONAL #12 SDQ
    *BNS CCN MAXIMO GOMEZ DIS

Sin normalizar, cada variante sería un comercio nuevo y habría que corregir el
mismo sitio veinte veces — que es exactamente cuando la gente deja de corregir.
Se quitan acentos, números de sucursal y terminal, sufijos de ciudad y país, y
los prefijos que meten las redes de pago.

ORDEN DE DECISIÓN, y la primera que acierta gana:
  1. Lo aprendido, por comercio normalizado exacto.
  2. Palabras clave, como red de seguridad para lo que nunca se ha corregido.
  3. Sin categoría — y a la cola del panel, que es donde se convierte en (1).
"""
from __future__ import annotations

import re
import unicodedata

# Sufijos de ciudad y país que los bancos pegan al final del comercio.
_COLAS = re.compile(
    r"\s*(SANTO\s*DOMINGO(DO)?|SANTIAGO|SDQ|STO\s*DGO|DO|RD|US|USA|"
    r"REP\s*DOM(INICANA)?)\s*$", re.I)
# Prefijos de las redes de pago: "*BNS ", "CCN ", "TPV ", "POS ".
_CABEZAS = re.compile(r"^\s*[*#]?\s*(BNS|CCN|TPV|POS|PDV|VTA)\s+", re.I)
# Números de sucursal y terminal: "#12", "No. 7", " 007", "-15".
_SUCURSAL = re.compile(r"\s*[#Nn][oO]?\.?\s*\d+\s*|\s*[-–]\s*\d+\s*$|\s+\d{1,5}\s*$")


def normalizar_comercio(texto: str) -> str:
    """'SUPERMERCADO NACIONAL #12 SDQ' → 'SUPERMERCADO NACIONAL'.

    El objetivo no es un nombre bonito: es que todas las variantes del mismo
    sitio caigan en la MISMA clave, para que corregirlo una vez alcance.
    """
    t = "".join(c for c in unicodedata.normalize("NFD", texto or "")
                if not unicodedata.combining(c)).upper()
    t = t.replace("&", " Y ")
    for _ in range(3):          # los prefijos vienen apilados: "*BNS CCN ..."
        nuevo = _CABEZAS.sub("", t)
        if nuevo == t:
            break
        t = nuevo
    for _ in range(3):          # las colas se apilan: "... SANTO DOMINGO DO"
        nuevo = _COLAS.sub("", t)
        if nuevo == t:
            break
        t = nuevo
    t = _SUCURSAL.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


class Categorizador:
    """Decide la categoría de un comercio. Sin base de datos: lógica pura.

    `aprendidas` es {comercio_normalizado: categoria} y sale de la tabla de
    correcciones. `claves` es {palabra: categoria}, la red de seguridad.
    """

    def __init__(self, aprendidas: dict | None = None,
                 claves: dict | None = None):
        self.aprendidas = {normalizar_comercio(k): v
                           for k, v in (aprendidas or {}).items()}
        # Las palabras clave se prueban de MÁS LARGA a más corta: "SUPERMERCADO
        # NACIONAL" tiene que ganarle a "SUPER", que casaría también con
        # "SUPERCASHBACK" y con media ciudad. Ordenar por longitud evita tener
        # que pensar en el orden al escribirlas.
        self.claves = sorted(
            ((normalizar_comercio(k), v) for k, v in (claves or {}).items()),
            key=lambda kv: -len(kv[0]))

    def categoria_de(self, comercio: str) -> str | None:
        """Exacto primero, después por PREFIJO, y al final las palabras clave.

        El prefijo no es un adorno: los bancos pegan al final del comercio el
        barrio, la ciudad o el terminal, y esa cola varía entre compras del
        mismo sitio — "CLUB NACO CABAMAR" y "CLUB NACO CABAMAR GUAYACANES" son
        el mismo restaurante. Enumerar todos los barrios de Santo Domingo no es
        una opción; que una corrección cubra sus variantes por delante, sí.

        Se prueba de más largo a más corto para que "SM NACIONAL MAXIMO" le gane
        a "SM NACIONAL" cuando las dos estén aprendidas.
        """
        norm = normalizar_comercio(comercio)
        if not norm:
            return None
        if norm in self.aprendidas:
            return self.aprendidas[norm]
        for clave in sorted(self.aprendidas, key=len, reverse=True):
            # Prefijo por PALABRAS, no por caracteres: "CLUB NA" no debe casar
            # con "CLUB NACIONAL". El espacio final es lo que fuerza el corte.
            if norm.startswith(clave + " ") or clave.startswith(norm + " "):
                return self.aprendidas[clave]
        for clave, categoria in self.claves:
            if clave and clave in norm:
                return categoria
        return None

    def aprender(self, comercio: str, categoria: str) -> str:
        """Registra una corrección. Devuelve la clave con la que quedó guardada.

        Se aprende del comercio NORMALIZADO, así que corregir
        "SM NACIONAL MAXIMO GOM SDQ" también clasifica
        "SM NACIONAL MAXIMO GOM" la próxima vez.
        """
        norm = normalizar_comercio(comercio)
        if norm:
            self.aprendidas[norm] = categoria
        return norm
