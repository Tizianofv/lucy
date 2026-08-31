-- 003 · Cursor propio para la ingesta de movimientos bancarios
--
-- POR QUÉ UNO PROPIO Y NO `correo_estado`: esa tabla es del camino del reporte
-- matinal. Compartirla acoplaría dos recorridos que avanzan a ritmos distintos —
-- el reporte mira lo SIN LEER de los últimos 7 días, la ingesta mira TODO lo
-- nuevo de unos remitentes concretos— y el primero que avanzara el puntero
-- dejaría ciego al otro. Ese fue exactamente el fallo que `_sin_leer_sync`
-- documenta haber tenido en el reporte: "el puntero SE CONSUMÍA".
--
-- ARRANQUE EN SEPTIEMBRE: `desde_uid` empieza en el UID más alto que exista al
-- instalarse, y `desde_fecha` en 2026-09-01. El histórico no se importa —
-- decisión de Tiziano el 30-ago: "esto es a futuro, no al pasado". Los 963
-- correos viejos ya cumplieron su función como fixtures de los parsers.
--
-- APLICAR CUANDO: los backups vuelvan a correr. Al 30-ago-2026 el último es del
-- 5-ago. Esto es DDL sobre producción y no hay red debajo.
--   psql "$DATABASE_URL" -f db/migraciones/003_consumos_estado.sql

BEGIN;

CREATE TABLE IF NOT EXISTS consumos_estado (
  cuenta         TEXT PRIMARY KEY,
  uidvalidity    BIGINT,
  ultimo_uid     BIGINT NOT NULL DEFAULT 0,
  desde_fecha    DATE NOT NULL DEFAULT DATE '2026-09-01',
  actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE consumos_estado IS
  'Cursor de la ingesta de movimientos. Separado de correo_estado a propósito: '
  'el reporte matinal y la ingesta recorren el buzón con criterios distintos y '
  'compartir puntero deja ciego al que avanza más lento.';

COMMENT ON COLUMN consumos_estado.uidvalidity IS
  'Si el servidor lo cambia, los UID viejos dejan de significar nada y hay que '
  'reiniciar el cursor. Sin esta comprobación la ingesta se saltaría correos en '
  'silencio tras una migración del buzón.';

COMMIT;
