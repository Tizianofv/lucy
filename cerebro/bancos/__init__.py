"""Parsers de correo bancario.

Un módulo por banco, todos cumpliendo el mismo contrato (ver `contrato.py`).
Importar este paquete registra todos los parsers disponibles: los módulos de
banco llaman a `registrar()` al importarse, así que el registro se arma solo
y nadie tiene que mantener una lista a mano.

Uso:
    from cerebro.bancos import parsear, CorreoCrudo
    movimientos = parsear(correo)      # [] si ningún parser aplica
"""
from cerebro.bancos.contrato import (  # noqa: F401
    ASUNTOS_IGNORADOS,
    CANALES,
    ESTADOS,
    MONEDAS,
    TIPOS,
    CorreoCrudo,
    ErrorDeParseo,
    Movimiento,
    buscar_parser,
    normalizar_estado,
    normalizar_fecha,
    normalizar_monto,
    normalizar_moneda,
    parsear,
    registrar,
    remitentes_registrados,
)

# Los parsers por banco se importan acá abajo a medida que existan. El import
# es el que los registra; sin él, `parsear()` no los encuentra.
# from cerebro.bancos import bhd, banesco, banreservas, apap  # noqa: F401
