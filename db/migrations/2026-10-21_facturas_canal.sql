-- El cambio de canal de una factura ya aprobada (pieza 9b del diseño «Facturas», 21-oct-2026). Tiziano, textual:
-- «Si no se puede por Whatsapp que la envié por correo». Natalia pidió mandar la factura por un canal, quien revisa la
-- aprobó, y el envío no salió (la ventana de 24 h de WhatsApp cerrada, por ejemplo): Natalia pide el envío por el otro
-- canal y NO tiene que hacer que se apruebe dos veces la misma factura — una factura emitida no se corrige, así que es
-- la misma que ya se aprobó.
--
--   · `factura_envios.viene_de_envio_id` (BIGINT): de qué envío APROBADO que no salió viene este. NULL en todos los
--     envíos normales (los pendientes que pide Natalia y los `mano` de la casa). El envío nuevo lleva también la
--     persona y la hora de la aprobación de aquél (`aprobado_por_chat_id`, `aprobado_en`) y su tarjeta no se manda.
--
-- Todo es ADITIVO: una columna que acepta NULL, sin valor por omisión y sin CHECK. No se reescribe ninguna fila. El
-- proceso VIEJO (el código que no conoce la columna) sigue andando con la base cambiada: no la nombra en ningún SQL.
-- El código NUEVO con la migración SIN aplicar: el cambio de canal revienta con 503 sin escribir (`EnviosSinMigrar`) y
-- todo lo demás de los envíos sigue igual; `tools/humo.py` dice que falta.
-- REQUIERE `2026-10-20_facturas.sql` (la tabla `factura_envios`).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en producción),
-- con `lock_timeout` y antes de eso, en ensayo, con `python3 tools/ensayo_facturas_canal.py --ensayo` (arma el SQL para
-- `psql`, sin conectarse).
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM factura_envios` igual que antes, `information_schema.columns` con UNA fila de `factura_envios`
-- (`viene_de_envio_id` bigint) con `is_nullable = YES` y sin valor por omisión, y con el ROLLBACK no queda.
--
-- CÓMO SE DESHACE (solo si el código que la usa ya no está desplegado):
--   ALTER TABLE factura_envios DROP COLUMN IF EXISTS viene_de_envio_id;

BEGIN;

ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS viene_de_envio_id BIGINT;

COMMENT ON COLUMN factura_envios.viene_de_envio_id IS
  'El envio aprobado que no salio y del que viene este cambio de canal. NULL = no viene de ninguno.';

COMMIT;
