-- 001 · Guardia anti-duplicado en `movimientos`
--
-- POR QUÉ: la tabla no tiene ningún UNIQUE. Con ingesta automática que
-- reintenta, eso es un duplicado esperando fecha. Y no es hipotético: Banco
-- Popular manda la MISMA transacción dos veces con 1 y 6 segundos de
-- diferencia — de sus 16 correos parseables solo 13 son movimientos distintos.
--
-- CÓMO: el mismo patrón que ya usa `bandeja` con `hash_contenido`. La huella
-- la calcula `Movimiento.clave_dedupe()` en cerebro/bancos/contrato.py:
--   banco | fecha con hora | monto | moneda | contraparte sin acentos
--
-- La hora entra en la huella a propósito, porque `movimientos.fecha` es DATE y
-- la pierde: sin ella dos cafés del mismo día en el mismo sitio serían
-- indistinguibles y el segundo se descartaría como duplicado.
--
-- El índice es PARCIAL (WHERE hash_contenido IS NOT NULL) para no estorbar a
-- los movimientos que se siguen creando a mano desde Telegram, que no tienen
-- huella y no deben tenerla: si Tiziano dice dos veces "gasté 500 en el super",
-- puede que de verdad hayan sido dos.
--
-- APLICAR CUANDO: los backups vuelvan a correr. Al 30-ago-2026 el último es del
-- 5-ago. Esto es DDL sobre producción y no hay red debajo.
--   psql "$DATABASE_URL" -f db/migraciones/001_movimientos_dedupe.sql

BEGIN;

ALTER TABLE movimientos
  ADD COLUMN IF NOT EXISTS hash_contenido TEXT;

COMMENT ON COLUMN movimientos.hash_contenido IS
  'Huella de Movimiento.clave_dedupe(); NULL en los creados a mano';

CREATE UNIQUE INDEX IF NOT EXISTS idx_movimientos_hash
  ON movimientos (hash_contenido)
  WHERE hash_contenido IS NOT NULL;

COMMIT;
