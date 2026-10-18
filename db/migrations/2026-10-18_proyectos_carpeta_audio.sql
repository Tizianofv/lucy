-- La carpeta de audio del proyecto (pieza 4 de «Carpetas del proyecto», 18-oct-2026; diseño en
-- `disenos/lucy-carpetas-proyecto/DISENO.md` §3.3).
--
--   · `proyectos.carpeta_audio` (TEXT): la ruta, escrita a mano por quien lleva el proyecto, de su carpeta en
--     el disco «Sesiones» del estudio (grabaciones, mezclas, archivos del DAW). NULL = sin escribir.
--     LUCY NO VE ESE DISCO: la columna es una nota. No dice que la carpeta exista, que el disco esté montado
--     ni qué hay dentro. Nunca se pinta como enlace. El largo y los caracteres los impone
--     `crud.PUERTAS["proyectos"]["carpeta_audio"]` (la misma función que la carpeta de Drive), no la base.
--
-- Todo es ADITIVO: una columna que acepta NULL, sin valor por omisión y sin CHECK. No se reescribe ninguna
-- fila ni se borra o renombra nada que exista; los proyectos de hoy quedan con `carpeta_audio` NULL.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción) y con `lock_timeout` (el ALTER pide por un instante el candado más fuerte de la tabla).
--
-- EL PROCESO VIEJO (el que no conoce la columna) SIGUE ANDANDO con la tabla cambiada: lee `proyectos` con
-- `SELECT *` por nombre de columna y las consultas preparadas están apagadas (`db/sin_preparadas.py`), así que
-- una columna de más no lo rompe. Y el código nuevo anda con la columna AUSENTE: la página carga sin la
-- línea de audio (`db.carpetas_de_audio` devuelve «no disponible» con SQLSTATE 42703; la carpeta de Drive no
-- depende de esa lectura) y `crud.editar` de `carpeta_audio` se rechaza sin escribir («Esa tabla no tiene:
-- carpeta_audio»).
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez (el comentario se vuelve a poner igual).
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después del `ALTER`,
-- `SELECT count(*) FROM proyectos` igual que antes, `SELECT count(carpeta_audio) FROM proyectos` = 0 y
-- `information_schema.columns` con UNA fila `proyectos.carpeta_audio` de tipo `text`, `is_nullable = YES`, sin
-- valor por omisión.
--
-- CÓMO SE DESHACE (solo si el código nuevo ya no está desplegado; con él vivo, borrar la columna hace que la
-- página no dibuje la línea de audio, pero lo escrito en ella se pierde):
--   ALTER TABLE proyectos DROP COLUMN IF EXISTS carpeta_audio;

BEGIN;

ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS carpeta_audio TEXT;

COMMENT ON COLUMN proyectos.carpeta_audio IS
  'Ruta, escrita a mano, de la carpeta de audio del proyecto en el disco Sesiones del estudio. Lucy NO ve ese '
  'disco: no sabe si la carpeta existe. NULL = sin escribir. Nunca se pinta como enlace.';

COMMIT;
