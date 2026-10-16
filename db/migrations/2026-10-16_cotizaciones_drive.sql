-- El Drive de las cotizaciones (pieza 8 del diseño «Cotizaciones en Proyectos», 16-oct-2026; el diseño lo
-- aprobó Tiziano el 14-oct-2026).
--
--   · `cotizaciones.drive_archivo_id` (TEXT): el id del PDF de la cotización en Drive.
--   · `cotizaciones.drive_version` (TIMESTAMPTZ): el `cambiado_en` de la versión que Drive confirmó. Una
--     cotización está «en Drive» solo si `drive_version = cambiado_en`.
--   · `cotizaciones.drive_error` (TEXT): la causa del último intento que falló, en una frase fija (nunca
--     texto de Google ni datos).
--   · `drive_de_proyectos`: las carpetas de Drive de cada proyecto, por id (la del proyecto y su
--     `cotizaciones`) y el `nombre_puesto` con que se creó la primera. Una fila por proyecto. Tabla aparte
--     para no tocar `proyectos`.
--
-- Todo es ADITIVO: tres columnas que aceptan NULL, sin valor por omisión y sin CHECK, y una tabla nueva. No
-- se reescribe ninguna fila ni se toca nada que exista; las cotizaciones de hoy quedan con las tres en NULL
-- (= «No está en Drive»). REQUIERE que la migración `2026-10-14_cotizaciones.sql` ya esté aplicada.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción) y con `lock_timeout` (el ALTER pide por un instante el candado más fuerte de la tabla).
--
-- EL PROCESO VIEJO (el código que no conoce estas columnas) SIGUE ANDANDO con la base cambiada: lee las
-- cotizaciones nombrando sus columnas una por una y sus consultas preparadas están apagadas para todo el
-- proceso (`db/sin_preparadas.py`). Y el código nuevo con la migración SIN aplicar: `db.cotizaciones_de_proyectos`
-- devuelve `None` (SQLSTATE 42703) y el bloque de cotizaciones no sale; `tools/humo.py` lo dice.
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` y `CREATE TABLE IF NOT EXISTS` no hacen nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM cotizaciones` igual que antes, `SELECT count(drive_archivo_id) FROM cotizaciones` = 0,
-- `SELECT count(*) FROM drive_de_proyectos` = 0, y `information_schema.columns` con TRES filas de
-- `cotizaciones` (`drive_archivo_id` text, `drive_version` timestamp with time zone, `drive_error` text), las
-- tres con `is_nullable = YES` y sin valor por omisión.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado; con él vivo se pierde qué
-- cotización está en Drive):
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS drive_archivo_id;
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS drive_version;
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS drive_error;
--   DROP TABLE IF EXISTS drive_de_proyectos;

BEGIN;

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS drive_archivo_id TEXT;
ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS drive_version TIMESTAMPTZ;
ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS drive_error TEXT;

CREATE TABLE IF NOT EXISTS drive_de_proyectos (
  proyecto_id            BIGINT PRIMARY KEY REFERENCES proyectos(id),
  carpeta_proyecto_id    TEXT NOT NULL,
  carpeta_cotizaciones_id TEXT NOT NULL,
  nombre_puesto          TEXT NOT NULL,
  creado_en              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN cotizaciones.drive_version IS
  'El cambiado_en de la versión que Drive confirmó. En Drive = drive_version igual a cambiado_en.';

COMMENT ON TABLE drive_de_proyectos IS
  'Las carpetas de Drive de cada proyecto, por id: la del proyecto y su subcarpeta cotizaciones. '
  'nombre_puesto es el nombre con que se creó la carpeta del proyecto.';

COMMIT;
