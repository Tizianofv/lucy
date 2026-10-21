-- El detalle de una factura (pieza D2 del diseño «Facturas», 21-oct-2026. Tiziano, textual: «Si, las facturas deben
-- salir con los detalles.»). Cada renglón lleva SERVICIO (descripción), CANTIDAD con su unidad, PRECIO UNITARIO y
-- TOTAL, como la cotización con detalle (`disenos/lucy-cotizacion-con-detalle/`).
--
--   · `factura_renglones.cantidad` (NUMERIC(14,3)): cuánto se cobra («2 canciones», «4 horas»).
--   · `factura_renglones.unidad` (TEXT): la palabra que acompaña a la cantidad («horas», «canciones»). NULL = sin
--     unidad (un renglón viejo o uno escrito sin unidad).
--   · `factura_renglones.precio_unitario` (NUMERIC(14,2)): lo que cuesta UNA unidad.
--
-- EL TOTAL DEL RENGLÓN ES `cantidad × precio_unitario` redondeado a centavos, y se sigue guardando en `monto`: la
-- suma de los `monto` es el subtotal de la factura (`db.totales_de_factura`, la lista y el humo) y no hay una
-- segunda forma de contarlo. El CHECK de `precio_unitario` lo exige, así que la base no puede guardar un renglón
-- cuyo `monto` no sea el de su cantidad y su precio.
--
-- LOS RENGLONES QUE YA EXISTEN (los de las facturas de antes de esta pieza) SIGUEN VALIENDO: las tres columnas
-- nacen NULL y el código lee un renglón con `cantidad` NULL como cantidad 1 y precio unitario = su `monto`, sin
-- unidad. No se reescribe NINGUNA fila: la migración es solo de columnas, y el renglón viejo se sigue viendo con
-- su mismo total (1 × monto). Las dos formas son cerradas (las dos columnas del dinero NULL, o las dos con valor y
-- el total cuadrando); la base lo exige con los CHECK (el de `unidad`, que es la última, cierra las dos columnas del
-- dinero a la vez: o las dos o ninguna).
--
-- Todo es ADITIVO: tres columnas que aceptan NULL, sin valor por omisión. REQUIERE `2026-10-20_facturas.sql`.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción), con `lock_timeout` (cada ALTER pide por un instante el candado más fuerte de la tabla) y antes de
-- eso, en ensayo, con `python3 tools/ensayo_facturas_detalle.py --ensayo` (arma el SQL para `psql`, sin conectarse).
--
-- EL PROCESO VIEJO (el código que no conoce las columnas) SIGUE ANDANDO con la base cambiada: sus lecturas de los
-- renglones nombran sus columnas una por una y sus consultas preparadas están apagadas para todo el proceso
-- (`db/sin_preparadas.py`). El código NUEVO necesita la migración aplicada: `SENTENCIA_DE_LOS_RENGLONES` nombra las
-- tres columnas y `tools/humo.py` lo dice antes (la cuenta de `_renglones_de_la_factura` revienta sin ellas).
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez. Los CHECK viajan con su columna, así que la
-- segunda vez tampoco se repiten.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM factura_renglones` igual que antes, `information_schema.columns` con TRES filas de
-- `factura_renglones` (`cantidad` numeric, `unidad` text, `precio_unitario` numeric), las tres con
-- `is_nullable = YES` y sin valor por omisión, los renglones viejos con las tres en NULL, y con el ROLLBACK no
-- queda ninguna.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado; con él vivo se pierde el detalle):
--   ALTER TABLE factura_renglones DROP COLUMN IF EXISTS unidad;
--   ALTER TABLE factura_renglones DROP COLUMN IF EXISTS precio_unitario;
--   ALTER TABLE factura_renglones DROP COLUMN IF EXISTS cantidad;

BEGIN;

ALTER TABLE factura_renglones ADD COLUMN IF NOT EXISTS cantidad NUMERIC(14,3)
  CHECK (cantidad IS NULL OR cantidad > 0);

ALTER TABLE factura_renglones ADD COLUMN IF NOT EXISTS precio_unitario NUMERIC(14,2)
  CHECK (precio_unitario IS NULL OR (precio_unitario > 0 AND cantidad IS NOT NULL AND monto = round(cantidad * precio_unitario, 2)));

ALTER TABLE factura_renglones ADD COLUMN IF NOT EXISTS unidad TEXT
  CHECK (((cantidad IS NULL) = (precio_unitario IS NULL)) AND
         (unidad IS NULL OR (cantidad IS NOT NULL AND length(trim(unidad)) BETWEEN 1 AND 20)));

COMMENT ON COLUMN factura_renglones.cantidad IS
  'Cuanto se cobra en el renglon (2 canciones, 4 horas). NULL = renglon de antes del detalle (vale 1).';
COMMENT ON COLUMN factura_renglones.precio_unitario IS
  'Lo que cuesta una unidad. NULL = renglon de antes del detalle (vale su monto). monto = cantidad x precio.';
COMMENT ON COLUMN factura_renglones.unidad IS
  'La palabra que acompania a la cantidad (horas, canciones). NULL = sin unidad.';

COMMIT;
