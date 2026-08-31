-- Corrige el backfill de la migración anterior, que solo recuperaba las
-- DECLINADAS y dejaba las PENDIENTES como aprobadas.
--
-- El error fue buscar el estado en el TEXTO de la referencia ("rechazo:",
-- "declinad"). El parser escribe ahí el motivo de un rechazo, pero no escribe
-- nada cuando una transferencia queda en proceso o cuando el banco retiene
-- fondos — así que las pendientes no casaban con nada y se quedaban en el
-- DEFAULT. Lo encontró el testigo.
--
-- El estado real SÍ estaba guardado, en un sitio que yo no miré:
-- `clave_dedupe()` lo pone como último campo de `hash_contenido`, separado por
-- "|", y esa huella no está cifrada. O sea que el dato nunca se perdió — se
-- recupera sin volver a leer un solo correo.
--
--     bhd|2026-08-05T11:08|2823.07|DOP|ALTICE HOGAR|aprobada
--                                                   ^^^^^^^^
BEGIN;

UPDATE movimientos
   SET estado = split_part(hash_contenido, '|', 6)
 WHERE hash_contenido IS NOT NULL
   AND split_part(hash_contenido, '|', 6) IN ('aprobada', 'declinada', 'pendiente')
   AND estado <> split_part(hash_contenido, '|', 6);

-- Y el vocabulario cerrado, cerrado también en la base. Hasta ahora solo lo
-- aplicaba normalizar_estado() en Python: un INSERT desde cualquier otro
-- cliente podía escribir lo que quisiera. Este proyecto trata los vocabularios
-- abiertos como el origen de los totales que no cuadran.
ALTER TABLE movimientos DROP CONSTRAINT IF EXISTS movimientos_estado_valido;
ALTER TABLE movimientos ADD CONSTRAINT movimientos_estado_valido
  CHECK (estado IN ('aprobada', 'declinada', 'pendiente'));

COMMIT;
