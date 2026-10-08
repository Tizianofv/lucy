-- La carpeta del proyecto (parte 5 de la página completa del proyecto, 8-oct-2026;
-- Tiziano: «Lo archivos seria un enlace al folder en el drive o en la compu donde
-- vive el proyecto»).
--
--   · `proyectos.carpeta` (TEXT): dónde vive el proyecto, escrito por quien lo lleva:
--     la dirección de una carpeta de Drive (`https://…`) o la ruta de una carpeta de
--     una computadora. NULL = sin carpeta. La columna guarda texto y no sabe cuál de
--     las dos es: si se pinta como enlace lo decide `db.enlace_de_carpeta`, mirando
--     el valor, y todo lo que no sea una dirección `http://` o `https://` sale como
--     texto escapado. El largo y los caracteres los impone `crud.PUERTAS`, no la base.
--
-- Todo es ADITIVO: una columna que acepta NULL, sin valor por omisión y sin CHECK.
-- No se reescribe ninguna fila, no se borra ni se renombra nada que exista y los
-- proyectos de hoy quedan con `carpeta` NULL.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del
-- repo para todo DDL en producción) y con `lock_timeout` (el ALTER pide por un
-- instante el candado más fuerte de la tabla y espera detrás de cualquier consulta
-- larga que la esté leyendo).
--
-- EL PROCESO VIEJO (el código de `465f769`, que no conoce la columna) SIGUE ANDANDO con la
-- tabla cambiada: lee `proyectos` con `SELECT *` por nombre de columna (`dict_row`) y las
-- consultas preparadas están apagadas para todo el proceso (`db/sin_preparadas.py`), así
-- que una columna de más no lo rompe. Y el código nuevo anda con la columna AUSENTE:
-- la página carga sin el bloque de la carpeta (`db.carpetas_de_proyectos` devuelve
-- «no disponible» con SQLSTATE 42703) y `crud.editar` de `carpeta` se rechaza sin
-- escribir («Esa tabla no tiene: carpeta»).
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez (el comentario
-- se vuelve a poner igual).
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después del
-- `ALTER`, `SELECT count(*) FROM proyectos` igual que antes, `SELECT count(carpeta)
-- FROM proyectos` = 0 (ninguna fila trae carpeta) y `information_schema.columns` con
-- UNA fila `proyectos.carpeta` de tipo `text`, `is_nullable = YES`, sin valor por omisión.
--
-- CÓMO SE DESHACE (solo si el código nuevo ya no está desplegado; con el código nuevo
-- vivo, borrar la columna hace que la página no dibuje el bloque de la carpeta, pero
-- lo que se escribió en ella se pierde):
--   ALTER TABLE proyectos DROP COLUMN IF EXISTS carpeta;

BEGIN;

ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS carpeta TEXT;

COMMENT ON COLUMN proyectos.carpeta IS
  'Dónde vive el proyecto: una dirección http(s) de Drive o la ruta de una carpeta de '
  'una computadora. NULL = sin carpeta. Solo una dirección http:// o https:// se pinta '
  'como enlace (db.enlace_de_carpeta); lo demás, como texto.';

COMMIT;
