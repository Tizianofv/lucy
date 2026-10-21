-- La puerta de Natalia para las facturas (pieza 9 del diseño «Facturas», aprobado por Tiziano el 20-oct-2026): Natalia
-- hace la factura de un cliente, pide que se apruebe mandarla por WhatsApp o por correo, Rosi la revisa en Telegram
-- (la tarjeta lleva el PDF y los botones Aprobar y Rechazar) y Natalia anota si salió. Los envíos viven en
-- `factura_envios` (pieza 1); esto solo le agrega lo que hace falta para la tarjeta:
--
--   · `factura_envios.tarjeta_msg_id` (BIGINT): el mensaje de Telegram de la tarjeta que se le mandó a quien revisa.
--     NULL = la tarjeta todavía no salió (el despertador la manda y la anota DESPUÉS de que Telegram la devolvió).
--   · `factura_envios.visto_cambiado_en` (TIMESTAMPTZ): el `cambiado_en` de la factura que tenía el PDF de la tarjeta.
--     Aprobar emite el borrador con ESE candado: si alguien lo corrigió después de mandar la tarjeta, la aprobación
--     no emite una factura distinta de la que se vio. NULL mientras no salió la tarjeta.
--   · `factura_envios.tarjeta_enviada_en` (TIMESTAMPTZ): cuándo salió la tarjeta (de ahí se cuentan los 30 minutos del recordatorio).
--   · `factura_envios.recordada_en` (TIMESTAMPTZ): cuándo se le recordó a quien revisa. NULL = todavía no. El recordatorio se
--     RECLAMA con un `UPDATE … WHERE recordada_en IS NULL` ANTES de enviarlo, así que dos contenedores no lo mandan dos veces.
--
-- Todo es ADITIVO: cuatro columnas que aceptan NULL, sin valor por omisión y sin CHECK. No se reescribe ninguna fila. Hasta
-- esta pieza ningún código crea envíos, así que la tabla está vacía en producción (según el diseño de la pieza 1; la
-- sala lo cuenta con `python3 tools/ensayo_facturas_envios.py --cuenta` antes de aplicar).
-- REQUIERE `2026-10-20_facturas.sql` (la tabla `factura_envios`).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción), con `lock_timeout` y antes de eso, en ensayo, con `python3 tools/ensayo_facturas_envios.py --ensayo`
-- (arma el SQL para `psql`, sin conectarse).
--
-- EL PROCESO VIEJO (el código que no conoce estas columnas) SIGUE ANDANDO con la base cambiada: no las nombra en ningún
-- SQL. Y el código nuevo con la migración SIN aplicar: la tarjeta no sale (el despertador lo registra y lo reintenta),
-- pedir un envío por la puerta de Natalia revienta con 503 sin escribir, y la página de una factura sigue leyendo los
-- envíos (solo nombra las columnas de siempre); `tools/humo.py` dice que falta.
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM factura_envios` igual que antes, `information_schema.columns` con CUATRO filas de
-- `factura_envios` (`tarjeta_msg_id` bigint; `visto_cambiado_en`, `tarjeta_enviada_en` y `recordada_en` timestamp with time zone), las cuatro con
-- `is_nullable = YES` y sin valor por omisión, y con el ROLLBACK ninguna queda.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado):
--   ALTER TABLE factura_envios DROP COLUMN IF EXISTS tarjeta_msg_id;
--   ALTER TABLE factura_envios DROP COLUMN IF EXISTS visto_cambiado_en;
--   ALTER TABLE factura_envios DROP COLUMN IF EXISTS tarjeta_enviada_en;
--   ALTER TABLE factura_envios DROP COLUMN IF EXISTS recordada_en;

BEGIN;

ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS tarjeta_msg_id BIGINT;
ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS visto_cambiado_en TIMESTAMPTZ;
ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS tarjeta_enviada_en TIMESTAMPTZ;
ALTER TABLE factura_envios ADD COLUMN IF NOT EXISTS recordada_en TIMESTAMPTZ;

COMMENT ON COLUMN factura_envios.tarjeta_msg_id IS
  'El mensaje de Telegram con la tarjeta de aprobacion. NULL = la tarjeta no ha salido.';
COMMENT ON COLUMN factura_envios.visto_cambiado_en IS
  'El cambiado_en de la factura que tenia el PDF de la tarjeta: aprobar emite con ese candado.';

COMMIT;
