-- El Drive de las facturas (pieza 8 del diseño «Facturas», aprobado por Tiziano el 20-oct-2026): el PDF de una
-- factura EMITIDA se sube a Drive, a la subcarpeta `facturas` de la carpeta de su proyecto (hermana de
-- `cotizaciones`) o, si no tiene proyecto, a una carpeta «Facturas» que Lucy crea en la raíz de Mi unidad. Al
-- anularla se sube además el PDF «ANULADA», con su propio nombre; el de la emitida no se toca.
--
--   · `facturas.drive_archivo_id` (TEXT): el id del PDF de la EMITIDA en Drive.
--   · `facturas.drive_anulada_archivo_id` (TEXT): el id del PDF «ANULADA» en Drive.
--   · `facturas.drive_proyecto_id` (BIGINT): el proyecto que la factura tenía cuando se subió el PDF (NULL = se subió
--     a «Facturas», sin proyecto). Sirve para decir la verdad si después le ponen o le quitan el proyecto: el PDF
--     se queda donde se subió (Lucy no mueve ni borra nada en Drive) y la pantalla lo dice. NO es llave foránea:
--     es una nota de dónde quedó el archivo, no una liga viva.
--   · `facturas.drive_error` (TEXT): la causa, en una frase fija (nunca texto de Google), del último intento que falló.
--   · `drive_de_proyectos.carpeta_facturas_id` (TEXT): la subcarpeta `facturas` de cada proyecto, por id; NULL hasta
--     que se sube la primera factura de ese proyecto.
--   · `drive_carpetas_sueltas`: carpetas de Drive que no son de ningún proyecto, por clave (hoy solo `facturas`, la
--     carpeta «Facturas»). Una fila por clave.
--
-- Todo es ADITIVO: cinco columnas que aceptan NULL, sin valor por omisión y sin CHECK, y una tabla nueva. No se
-- reescribe ninguna fila. Las facturas de hoy quedan con todo en NULL (= «No está en Drive»).
-- REQUIERE las migraciones `2026-10-16_cotizaciones_drive.sql` (la tabla `drive_de_proyectos`) y
-- `2026-10-20_facturas.sql` (la tabla `facturas`).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción), con `lock_timeout` (cada ALTER pide por un instante el candado más fuerte de su tabla) y antes de
-- eso, en ensayo, con `python3 tools/ensayo_facturas_drive.py --ensayo` (arma el SQL para `psql`, sin conectarse).
--
-- EL PROCESO VIEJO (el código que no conoce estas columnas) SIGUE ANDANDO con la base cambiada: las lecturas de las
-- facturas y de las carpetas de los proyectos nombran sus columnas una por una y sus consultas preparadas están
-- apagadas para todo el proceso (`db/sin_preparadas.py`). Y el código nuevo con la migración SIN aplicar: la lectura
-- del estado de Drive de una factura devuelve `None` (SQLSTATE 42703 o 42P01) y la página sale sin el bloque de
-- Drive; subir una factura revienta y se anota como que no llegó; `tools/humo.py` dice que falta.
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` y `CREATE TABLE IF NOT EXISTS` no hacen nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de la migración,
-- `SELECT count(*) FROM facturas` igual que antes, `SELECT count(*) FROM drive_carpetas_sueltas` = 0,
-- `information_schema.columns` con CUATRO filas de `facturas` (`drive_archivo_id` text, `drive_anulada_archivo_id`
-- text, `drive_proyecto_id` bigint, `drive_error` text) y UNA de `drive_de_proyectos` (`carpeta_facturas_id` text),
-- cinco en total, todas con `is_nullable = YES` y sin valor por omisión, y con el ROLLBACK ninguna queda.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado; con él vivo se pierde qué factura está en
-- Drive):
--   ALTER TABLE facturas DROP COLUMN IF EXISTS drive_archivo_id;
--   ALTER TABLE facturas DROP COLUMN IF EXISTS drive_anulada_archivo_id;
--   ALTER TABLE facturas DROP COLUMN IF EXISTS drive_proyecto_id;
--   ALTER TABLE facturas DROP COLUMN IF EXISTS drive_error;
--   ALTER TABLE drive_de_proyectos DROP COLUMN IF EXISTS carpeta_facturas_id;
--   DROP TABLE IF EXISTS drive_carpetas_sueltas;

BEGIN;

ALTER TABLE facturas ADD COLUMN IF NOT EXISTS drive_archivo_id TEXT;
ALTER TABLE facturas ADD COLUMN IF NOT EXISTS drive_anulada_archivo_id TEXT;
ALTER TABLE facturas ADD COLUMN IF NOT EXISTS drive_proyecto_id BIGINT;
ALTER TABLE facturas ADD COLUMN IF NOT EXISTS drive_error TEXT;

ALTER TABLE drive_de_proyectos ADD COLUMN IF NOT EXISTS carpeta_facturas_id TEXT;

CREATE TABLE IF NOT EXISTS drive_carpetas_sueltas (
  clave      TEXT PRIMARY KEY,
  carpeta_id TEXT NOT NULL,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN facturas.drive_proyecto_id IS
  'El proyecto que la factura tenia cuando se subio su PDF a Drive (NULL = a la carpeta Facturas). No es llave.';

COMMENT ON TABLE drive_carpetas_sueltas IS
  'Carpetas de Drive que no son de ningun proyecto, por clave. Hoy solo facturas: la carpeta Facturas.';

COMMIT;
