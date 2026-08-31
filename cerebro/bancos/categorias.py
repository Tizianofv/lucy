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
# El \b antes del grupo NO es adorno: sin él, "DO|RD|US" casan dentro de la
# última palabra y "SUPERMERCADO" se normaliza a "SUPERMERCA", "BONUS" a "BON"
# y "PESCADO" a "PESCA". Como las claves también pasan por acá, eso convertía
# claves largas y seguras en muñones cortos y peligrosos.
_COLAS = re.compile(
    r"\s*\b(SANTO\s*DOMINGO(DO)?|SANTIAGO|SDQ|STO\s*DGO|DO|RD|US|USA|"
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
    # El apóstrofo se BORRA, no se cambia por espacio: "WENDY'S" tiene que dar
    # "WENDYS" y no "WENDY S", que parte el nombre en dos y hace que la clave
    # del comercio deje de casar mientras el nombre de pila suelto empieza a
    # casar solo. Vale para todas las comillas que meten los bancos.
    t = re.sub(r"[\u2019\u02bc'`]", "", t)
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


# ── El vocabulario ───────────────────────────────────────────────────────
#
# Lista CERRADA, como las otras de este proyecto (moneda, estado, tipo). No es
# rigidez por gusto: si el panel deja escribir la categoría a mano, en tres
# meses hay "Supermercado", "supermercado", "Super" y "Comida/super" y ningún
# total sirve. Se agrega una categoría editando esta lista, a propósito.
#
# El orden es el del desplegable, y está puesto por frecuencia de uso esperada
# —no alfabético— porque esto se maneja con el pulgar en el celular y lo que
# más se toca tiene que estar arriba.
CATEGORIAS = [
    "Supermercado",
    "Colmado",
    "Restaurantes",
    "Combustible",
    "Vehículo",
    "Transporte",
    "Servicios del hogar",
    "Salud",
    "Seguros",
    "Cuidado personal",
    "Hogar",
    "Ropa y hogar",
    "Reparaciones del hogar",
    "Equipos y tecnología",
    "Educación",
    "Software y suscripciones",
    "Entretenimiento",
    "Banco y comisiones",
    "Impuestos",
    "Regalos y donaciones",
    "Mascotas",
    # Un proyecto concreto, no un rubro. Va cerca del final porque se toca
    # menos que el supermercado, pero por encima de "Otros" porque cuando se
    # usa se usa a propósito: la gracia es poder filtrar /movimientos por él y
    # ver cuánto lleva costando, que es una pregunta que ninguna categoría de
    # rubro puede contestar. Sin palabras clave: un proyecto lo decide una
    # persona, no el nombre del comercio.
    "Proyecto Inés",
    # Acá NO hay categorías de ingreso, y es una decisión de Tiziano: el
    # dinero que entra no hace falta clasificarlo. La cola solo trae gastos
    # (db.sin_clasificar), así que un ingreso nunca pide categoría — antes sí
    # las había, y lo único que hacían era empujar hacia abajo lo que de verdad
    # hay que corregir.
    "Otros",
    # Va al final porque no es una categoría de gasto: es una MARCA. Y se llama
    # "No suma" a secas por pedido de Tiziano, que tiene razón — el nombre tiene
    # que decir qué HACE, no de dónde viene el dinero. "Dinero de terceros"
    # obligaba a acordarse de que además no contaba; así se lee en el
    # desplegable y ya está dicho. Ver NO_SUMAN.
    "No suma",
]

# Las categorías que NO entran en ningún total.
#
# Nace de un circuito real: el papá de Rosi tiene un certificado en APAP; los
# intereses caen en la cuenta de la casa, de ahí se paga la luz de SU casa y el
# resto se le transfiere. Tres movimientos —RD$43,312 de "ingreso", RD$30,518 y
# ~RD$11,000 de "gasto"— que no son ni ingreso ni gasto de nadie de esta casa.
# Se compensaban entre sí, así que el neto quedaba casi bien por casualidad y
# cada cifra por separado era falsa. "Servicios del hogar" figuraba como la
# segunda categoría del mes en buena parte por una factura ajena.
#
# Por qué una categoría y no una tabla de terceros: la marca vive donde ya se
# mira y se corrige, sin inventar una pantalla nueva ni un registro que alguien
# tenga que mantener. El movimiento SIGUE VISIBLE en /movimientos —el circuito
# se puede auditar entero— y solo se cae de los totales.
NO_SUMAN = ("No suma",)

# ── La red de seguridad ──────────────────────────────────────────────────
#
# Palabras clave → categoría, para lo que nunca se ha corregido. Se prueban de
# MÁS LARGA a más corta (lo hace el constructor), así que una clave específica
# le gana a una genérica sin que haya que pensar en el orden acá.
#
# DOS REGLAS al agregar claves, las dos aprendidas rompiendo algo:
#
#   1. La clave tiene que empezar donde empieza una palabra del comercio, y de
#      eso se encarga `categoria_de`. Por eso "UBER" y "DGII" pueden ser cortas
#      sin ensuciar: no van a casar dentro de "TUBERIA" ni de nada. Lo que sí
#      sigue prohibido es la palabra corta y GENÉRICA —"BON", "OLE"— que sí
#      empieza palabras ajenas de verdad.
#   2. Ante la duda, NO poner la clave. Un movimiento sin categoría cae en la
#      cola del panel y se corrige una vez y para siempre; uno mal categorizado
#      no cae en ninguna cola y desvía el total sin que nadie lo note. El costo
#      de faltar es un minuto; el de sobrar es un número equivocado.
#
# Por eso acá no están las transferencias a personas: van a la cola, que es su
# sitio. "ENFOQUE DIGITAL" sí está, pero no por deducción — estaba en la cola
# hasta que Tiziano dijo que es una tienda de equipos de foto y video. Eso es
# exactamente el circuito que el panel existe para cerrar.
CLAVES = {
    # Supermercados dominicanos
    "SM NACIONAL": "Supermercado", "SUPERMERCADO": "Supermercado",
    "PLAZA LAMA": "Supermercado", "JUMBO": "Supermercado",
    "SUPERMERCADOS BRAVO": "Supermercado", "LA SIRENA": "Supermercado",
    "PRICESMART": "Supermercado",
    # "MAXIMO GOMEZ DIS" es la red de Nacional en la avenida, no un comercio
    # distinto; sale así en los correos del BHD.
    "MAXIMO GOMEZ DIS": "Supermercado",

    # El colmado es categoría aparte del supermercado a propósito: son la misma
    # compra hecha en otro sitio y con otra frecuencia —diaria y chica contra
    # semanal y grande— y juntarlas esconde exactamente el gasto que uno quiere
    # ver. "COLMADON" entra solo, porque la clave casa en inicio de palabra.
    "COLMADO": "Colmado",

    "CLUB NACO": "Restaurantes", "RESTAURANT": "Restaurantes",
    "CAFETERIA": "Restaurantes", "PIZZA": "Restaurantes",
    "HELADOS BON": "Restaurantes", "GRAN MURALLA": "Restaurantes",
    "SAZON": "Restaurantes",
    "MCDONALD": "Restaurantes", "BURGER": "Restaurantes",
    "WENDYS": "Restaurantes", "DOMINO": "Restaurantes",
    "ADRIAN TROPICAL": "Restaurantes", "PANADERIA": "Restaurantes",

    "SHELL": "Combustible", "TEXACO": "Combustible",
    "TOTAL BELLA": "Combustible", "SIGMA": "Combustible",
    "ESTACION": "Combustible",

    # Vehículo es el gasto del carro que NO es la gasolina: lavado, goma,
    # mantenimiento, repuestos. "LA ALTANERA" estaba de restaurante porque yo
    # deduje el rubro del nombre, que es exactamente lo que el módulo dice no
    # hacer; es un carwash, y lo dijo Tiziano.
    "ALTANERA": "Vehículo", "CAR WASH": "Vehículo", "CARWASH": "Vehículo",
    "AUTO LAVADO": "Vehículo", "AUTOLAVADO": "Vehículo",
    "LAVADO": "Vehículo", "GOMERA": "Vehículo", "REPUESTOS": "Vehículo",
    "TALLER": "Vehículo",

    "UBER EATS": "Restaurantes", "UBER *EATS": "Restaurantes",
    "UBER": "Transporte", "RDVIAL": "Transporte", "PEAJE": "Transporte",
    "PARQUEO": "Transporte",

    "EDESUR": "Servicios del hogar", "EDEESTE": "Servicios del hogar",
    "EDENORTE": "Servicios del hogar", "CLARO": "Servicios del hogar",
    "ALTICE": "Servicios del hogar", "CAASD": "Servicios del hogar",
    "INAPA": "Servicios del hogar",

    "FARMACIA": "Salud", "FARMACONSUMO": "Salud", "CLINICA": "Salud",
    "LABORATORIO": "Salud", "HOSPITAL": "Salud", "CEDIMAT": "Salud",

    "SEGUROS": "Seguros", "MAPFRE": "Seguros", "HUMANO": "Seguros",

    # Reparaciones: la ferretería, el plomero, la pintura. Va aparte de "Ropa
    # y hogar" —que es lo que se compra para la casa— porque una gotera y un
    # juego de sábanas no se miran con el mismo ojo: uno es imprevisto y el
    # otro es gusto. Nada de apellidos acá: "OCHOA" es una ferretería conocida
    # Y un apellido dominicano corriente, y las transferencias a personas
    # llevan nombre; casaría la mitad de ellas.
    "FERRETERIA": "Reparaciones del hogar",
    "TUBERIA": "Reparaciones del hogar",
    "PLOMER": "Reparaciones del hogar",
    "CERRAJER": "Reparaciones del hogar",
    "PINTURAS": "Reparaciones del hogar",
    "SHERWIN": "Reparaciones del hogar",
    "MATERIALES DE CONST": "Reparaciones del hogar",

    "SALON": "Cuidado personal", "BARBER": "Cuidado personal",
    "PELUQUERIA": "Cuidado personal",

    "CANVA": "Software y suscripciones", "NETFLIX": "Software y suscripciones",
    "SPOTIFY": "Software y suscripciones", "GOOGLE": "Software y suscripciones",
    "MICROSOFT": "Software y suscripciones", "ADOBE": "Software y suscripciones",
    "OPENAI": "Software y suscripciones", "ANTHROPIC": "Software y suscripciones",
    "GITHUB": "Software y suscripciones", "DROPBOX": "Software y suscripciones",
    "AMAZON": "Software y suscripciones",

    "CINEMA": "Entretenimiento", "CARIBBEAN CINEMAS": "Entretenimiento",

    # Solo el cargo, nunca el nombre del banco. "BANCO" y "BANRESERVAS" casaban
    # 10 movimientos del corpus y su significado real era "el texto menciona un
    # banco", no "esto es una comisión": convertían transferencias entre
    # personas en gasto bancario. Un nombre de pila como clave —"WENDY", que
    # capturaba 7 transferencias a una señora y 1 restaurante— es el mismo error.
    "COMISION": "Banco y comisiones", "SOBREGIRO": "Banco y comisiones",

    "DGII": "Impuestos", "IMPUESTO": "Impuestos",

    "ENFOQUE DIGITAL": "Equipos y tecnología",

    "VETERINARIA": "Mascotas", "PETSHOP": "Mascotas",
}


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
            # Casa en INICIO DE PALABRA, no en cualquier parte. La diferencia no
            # es cosmética: con subcadena cruda, "UBER" casa dentro de "TUBERIA"
            # y "OLE" dentro de "COLEGIO", y una compra de plomería termina
            # contada como transporte sin que nadie lo vea. Anclar al principio
            # de la palabra deja pasar el plural y el sufijo —"SUPERMERCADO"
            # sigue casando con "SUPERMERCADOS"— que es justo lo que sí se
            # quiere, porque los bancos escriben el mismo sitio de varias formas.
            if clave and re.search(r"\b" + re.escape(clave), norm):
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
