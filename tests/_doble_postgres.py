# -*- coding: utf-8 -*-
"""Doble FIEL de una conexión Postgres real, para las pruebas de "sin la
migración no revienta" de todo el repo (§E, hallazgo del testigo sobre
`cbc2726`, NO PASA, 27-sep-2026).

POR QUÉ EXISTE: una sentencia que falla dentro de una transacción
implícita (autocommit=False, sin `conn.transaction()` de por medio) deja
la transacción ABORTADA en Postgres real -- CUALQUIER sentencia posterior
en esa misma transacción revienta con SQLSTATE '25P02'
(`psycopg.errors.InFailedSqlTransaction`, "current transaction is
aborted, commands ignored until end of transaction block"; documentado en
la propia librería y comportamiento estándar del protocolo de Postgres)
hasta un `ROLLBACK` explícito o el fin de un `SAVEPOINT` que la contenga.

El doble SQLite que usaba antes `tests/test_duenos.py` simulaba el
SQLSTATE 42703 (columna ausente) pero SQLite no aborta la conexión
completa cuando una sentencia falla -- así que una prueba contra ESE
doble podía pasar en verde con un código que revienta contra Postgres de
verdad. Pasó exactamente eso con la primera versión de
`guardar_preferencia`/`perfil`/`db._buscar_o_crear` (rama personas):
reintentaban un segundo INSERT en la MISMA transacción sin envolver el
primer intento en `async with conn.transaction():` (un SAVEPOINT).

CÓMO SE USA: se registran REGLAS -- `(fragmento_en_mayusculas, resultado)`
-- en el orden en que hay que probarlas contra cada sentencia. `resultado`
es:
  · `FALLA_42703` -- simula que la columna todavía no existe: marca la
    conexión ABORTADA y lanza el error.
  · cualquier otra cosa (una fila, o `None`) -- la sentencia "funciona" y
    ese es el resultado que devuelve `fetchone()`/`fetchall()`.

Con `async with conn.transaction():` (SAVEPOINT) alrededor de un intento
que falla, la conexión se limpia (`abortada = False`) al salir con una
excepción -- eso es lo que hace un SAVEPOINT de verdad (`ROLLBACK TO
SAVEPOINT`, o un `ROLLBACK` liso si es la única transacción de la
conexión: para lo que estas pruebas miden -- "¿el siguiente INSERT corre
limpio?" -- el efecto es el mismo). SIN esa envoltura, la conexión queda
abortada para siempre (dentro de esta prueba) y la siguiente sentencia
revienta con `InFailedSqlTransaction`, tal cual pasaría contra Postgres
real.
"""
from __future__ import annotations


class ErrorSQL(Exception):
    """Un error con `.sqlstate`, como las excepciones reales de psycopg
    (`psycopg.errors.Error.sqlstate`)."""
    def __init__(self, sqlstate: str, mensaje: str = "error de mentira"):
        self.sqlstate = sqlstate
        super().__init__(mensaje)


class InFailedSqlTransaction(ErrorSQL):
    """`psycopg.errors.InFailedSqlTransaction`, SQLSTATE '25P02'."""
    def __init__(self):
        super().__init__(
            "25P02",
            "current transaction is aborted, commands ignored until end "
            "of transaction block")


class CursorPostgresFiel:
    def __init__(self, conexion):
        self._conexion = conexion
        self._fila = None
        self._filas: list = []

    async def execute(self, sql, params=None):
        if self._conexion.abortada:
            raise InFailedSqlTransaction()
        normal = " ".join(sql.split()).upper()
        for fragmento, resultado in self._conexion.reglas:
            if fragmento in normal:
                if resultado == "FALLA_42703":
                    self._conexion.abortada = True
                    raise ErrorSQL("42703", "columna ausente (de mentira)")
                self._fila = resultado
                self._filas = [resultado] if resultado is not None else []
                self._conexion.ejecutadas.append(normal)
                return self
        raise AssertionError(
            f"SQL no modelado por el doble fiel de Postgres: {sql[:160]!r}")

    async def fetchone(self):
        return self._fila

    async def fetchall(self):
        return self._filas


class _Savepoint:
    """`async with conn.transaction():` -- un SAVEPOINT (o la transacción
    entera, si es la primera de la conexión; el efecto que estas pruebas
    miden es el mismo): si lo de adentro falla, limpia `abortada` al
    salir, ANTES de dejar propagar la excepción."""
    def __init__(self, conexion):
        self._conexion = conexion

    async def __aenter__(self):
        return self

    async def __aexit__(self, tipo, valor, tb):
        if tipo is not None:
            self._conexion.abortada = False
        return False


class ConexionPostgresFiel:
    def __init__(self, reglas):
        self.reglas = reglas
        self.abortada = False
        self.ejecutadas: list[str] = []

    def cursor(self, row_factory=None):
        return CursorPostgresFiel(self)

    async def execute(self, sql, params=None):
        cur = CursorPostgresFiel(self)
        return await cur.execute(sql, params)

    def transaction(self):
        return _Savepoint(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class PoolPostgresFiel:
    def __init__(self, conexion: ConexionPostgresFiel):
        self._conexion = conexion

    def connection(self):
        conexion = self._conexion

        class _CM:
            async def __aenter__(self):
                return conexion

            async def __aexit__(self, *e):
                return False
        return _CM()
