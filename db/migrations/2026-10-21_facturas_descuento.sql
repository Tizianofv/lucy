-- El descuento de una factura (pieza D3 del diseño «Facturas», 21-oct-2026. Tiziano escogió la opción B: una línea
-- de descuento al final, junto al subtotal).
--
-- UNA cifra por factura, no un renglón negativo ni un descuento por renglón: `facturas.descuento` (NUMERIC(14,2),
-- NOT NULL, por omisión 0). El descuento se resta del subtotal, el ITBIS se calcula sobre lo que queda (el neto) y el
-- total es el neto más el ITBIS. Los tres números NO se guardan: salen de los renglones y del descuento cada vez que
-- se leen (`db.totales_de_factura`, el único sitio de esa cuenta).
--
-- NO PUEDE PASAR DEL SUBTOTAL. La base no puede exigirlo con un CHECK (el subtotal es la suma de otra tabla, y un CHECK
-- no mira otras filas): lo exige la puerta (`db._revisar_el_descuento`, al crear y al corregir un borrador) y lo
-- cuenta el humo contra la base real (`_descuento_de_las_facturas`: ninguna factura con el descuento por encima del
-- subtotal). El CHECK de aquí solo cierra el lado que sí puede ver: que no sea negativo.
--
-- ADITIVA y con valor por omisión 0: las facturas de antes de esta pieza quedan SIN descuento y sus totales salen
-- idénticos, céntimo por céntimo (no se reescribe ninguna fila; el valor por omisión lo pone la base al agregar la
-- columna y las filas de hoy quedan en 0).
--
-- Todo es ADITIVO. REQUIERE `2026-10-20_facturas.sql`.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en producción),
-- con `lock_timeout` (el ALTER pide por un instante el candado más fuerte de la tabla) y antes de eso, en ensayo, con
-- `python3 tools/ensayo_facturas_descuento.py --ensayo` (arma el SQL para `psql`, sin conectarse).
--
-- EL PROCESO VIEJO (el código que no conoce la columna) SIGUE ANDANDO con la base cambiada: sus consultas preparadas
-- están apagadas para todo el proceso (`db/sin_preparadas.py`) y no nombran la columna. El código NUEVO necesita la
-- migración aplicada: `SENTENCIA_DE_LEER_FACTURA` y `SENTENCIA_DE_LA_LISTA_DE_FACTURAS` la nombran y
-- `tools/humo.py` lo dice antes (su línea de la cuenta revienta sin ella).
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez. El CHECK viaja con su columna, así que la
-- segunda vez tampoco se repite.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM facturas` igual que antes, `information_schema.columns` con UNA fila de `facturas.descuento`
-- (`numeric`, `is_nullable = NO`, `column_default = 0`), todas las filas de hoy con 0, un `INSERT` de un descuento
-- negativo rechazado por el CHECK, y con el ROLLBACK no queda ninguna columna.
--
-- CÓMO SE DESHACE (solo si el código que la usa ya no está desplegado):
--   ALTER TABLE facturas DROP COLUMN IF EXISTS descuento;

BEGIN;

ALTER TABLE facturas ADD COLUMN IF NOT EXISTS descuento NUMERIC(14,2) NOT NULL DEFAULT 0
  CHECK (descuento >= 0);

COMMENT ON COLUMN facturas.descuento IS
  'Lo que se rebaja del subtotal antes del ITBIS. 0 = sin descuento. No puede pasar del subtotal (lo exige la puerta).';

COMMIT;
