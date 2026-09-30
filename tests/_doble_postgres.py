# -*- coding: utf-8 -*-
"""Doble FIEL de una conexión Postgres real, para las pruebas de "sin la
migración no revienta" y de "el camino feliz no comitea antes de tiempo"
de todo el repo (§E, hallazgos del testigo sobre `cbc2726` y `1c4acf0`,
NO PASA, 27-sep-2026).

DOS DEFECTOS DISTINTOS, DOS COSAS QUE ESTE DOBLE MODELA:

1. TRANSACCIÓN ABORTADA (`cbc2726`). Una sentencia que falla dentro de una
   transacción implícita (autocommit=False, sin `conn.transaction()` de
   por medio) deja la transacción ABORTADA en Postgres real -- CUALQUIER
   sentencia posterior en esa misma transacción revienta con SQLSTATE
   '25P02' (`psycopg.errors.InFailedSqlTransaction`, "current transaction
   is aborted, commands ignored until end of transaction block";
   documentado en la propia librería y comportamiento estándar del
   protocolo de Postgres) hasta un `ROLLBACK` explícito o el fin de un
   `SAVEPOINT` que la contenga. El doble SQLite que usaba antes `tests/
   test_duenos.py` no lo modelaba.

2. COMMIT PREMATURO (`1c4acf0`). `async with conn.transaction():` NO
   siempre es un SAVEPOINT: según el código real de psycopg3
   (`psycopg/transaction.py::_push_savepoint`), `_outer_transaction =
   self.pgconn.transaction_status == IDLE` -- si es la PRIMERA sentencia
   de transacción sobre una conexión recién salida del pool (`IDLE`), es
   la transacción OUTER, y salir sin excepción hace un `COMMIT` DE
   VERDAD, no un `RELEASE SAVEPOINT`. Eso selló `guardar_preferencia`
   ANTES de que `_registrar` escribiera `log_acciones`: dos transacciones
   separadas, no una. Este doble reproduce esa MISMA regla mirando el
   estado real de la conexión (`en_transaccion`, que empieza en `False` --
   "IDLE" -- y se pone en `True` con CUALQUIER sentencia ejecutada),
   exactamente como hace psycopg.

CÓMO SE USA: se registran REGLAS -- `(fragmento_en_mayusculas, resultado)`
-- en el orden en que hay que probarlas contra cada sentencia. `resultado`
es:
  · `FALLA_42703` -- simula que la columna todavía no existe: marca la
    conexión ABORTADA y lanza el error.
  · cualquier otra cosa (una fila, o `None`) -- la sentencia "funciona" y
    ese es el resultado que devuelve `fetchone()`/`fetchall()`.

`conexion.eventos` guarda la secuencia real de comandos de transacción --
`BEGIN`/`SAVEPOINT`/`COMMIT`/`RELEASE SAVEPOINT`/`ROLLBACK[...]` -- y
`EXEC <sql>` por cada sentencia. Una prueba de "camino feliz" arma las
reglas para que TODO salga bien a la primera y comprueba que ningún
`COMMIT`/`ROLLBACK` (el cierre de la transacción OUTER) aparece ANTES del
último `EXEC` de la acción -- si aparece, la acción quedó partida en dos
transacciones separadas.
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
        # CUALQUIER sentencia manda un comando al server -- lo pone
        # `INTRANS` (regla real de psycopg: cualquier `execute` sin
        # transacción explícita ABRE una transacción implícita).
        self._conexion.en_transaccion = True
        for fragmento, resultado in self._conexion.reglas:
            if fragmento in normal:
                self._conexion.eventos.append(f"EXEC {normal[:60]}")
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
    """`async with conn.transaction():`. MISMA REGLA que psycopg3 real
    (`psycopg/transaction.py::_push_savepoint`): `outer = not conexion.
    en_transaccion` (conexión `IDLE` == nunca se ejecutó nada todavía en
    ella). Si es outer: `BEGIN`/`COMMIT`/`ROLLBACK` de verdad, y la
    conexión vuelve a `IDLE` al salir. Si NO es outer (ya había algo
    ejecutado antes, la conexión está `INTRANS`): `SAVEPOINT`/`RELEASE
    SAVEPOINT`/`ROLLBACK TO SAVEPOINT`, y la conexión sigue `INTRANS`
    para lo que venga después en la MISMA transacción de fondo."""
    def __init__(self, conexion):
        self._conexion = conexion

    async def __aenter__(self):
        self.outer = not self._conexion.en_transaccion
        self._conexion.en_transaccion = True
        self._conexion.eventos.append("BEGIN" if self.outer else "SAVEPOINT")
        return self

    async def __aexit__(self, tipo, valor, tb):
        if tipo is None:
            if self.outer:
                self._conexion.eventos.append("COMMIT")
                self._conexion.en_transaccion = False  # vuelve a IDLE
            else:
                self._conexion.eventos.append("RELEASE SAVEPOINT")
            return False
        # Con excepción: rollback (completo si es outer, a savepoint si
        # no), y la conexión queda SANA para lo que siga -- eso es lo que
        # arregla el 42703: el error NO deja la conexión abortada más
        # allá de este bloque.
        self._conexion.eventos.append(
            "ROLLBACK" if self.outer else "ROLLBACK TO SAVEPOINT")
        if self.outer:
            self._conexion.en_transaccion = False
        self._conexion.abortada = False
        return False


class ConexionPostgresFiel:
    def __init__(self, reglas):
        self.reglas = reglas
        self.abortada = False
        self.en_transaccion = False  # IDLE al salir del pool
        self.ejecutadas: list[str] = []
        self.eventos: list[str] = []

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
        # `pool.connection()` real comitea (o hace rollback, si sale por
        # una excepción) lo que haya quedado abierto SIN un `conn.
        # transaction()` explícito -- una sentencia suelta abre una
        # transacción implícita que igual hay que cerrar al devolver la
        # conexión al pool.
        if self.en_transaccion:
            self.eventos.append("COMMIT (cierre del pool.connection())")
            self.en_transaccion = False
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
                return await conexion.__aexit__(*e)
        return _CM()


def indice_del_primer(eventos: list[str], *nombres: str) -> int | None:
    """El índice del primer evento cuyo texto es exactamente uno de
    `nombres`, o `None` si no aparece ninguno."""
    for i, ev in enumerate(eventos):
        if ev in nombres:
            return i
    return None


def indice_del_ultimo_que_contiene(eventos: list[str], subtexto: str) -> int | None:
    for i in range(len(eventos) - 1, -1, -1):
        if subtexto in eventos[i]:
            return i
    return None
