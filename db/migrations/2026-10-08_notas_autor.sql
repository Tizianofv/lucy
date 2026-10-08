-- Quién escribió una nota desde la página del proyecto (parte 6 de la página completa del
-- proyecto, 8-oct-2026; Tiziano: «Notas y decisiones», y el diseño aprobado dice que la nota
-- SÍ dice quién la escribió, como los comentarios de las tareas).
--
--   · `notas.autor_chat_id` (BIGINT): el chat de la SESIÓN del panel que escribió la nota
--     (`db.crear_nota_de_proyecto`), nunca un campo del formulario. NULL = no la escribió el panel:
--     las notas de siempre (las de Telegram) dicen quién las mandó por `notas.bandeja_id` ->
--     `bandeja.chat_id`, y una nota sin ninguna de las dos no tiene autor conocido. Sin llave
--     foránea, igual que `comentarios_tarea.autor_chat_id` y `proyectos.responsable_chat_id`: un
--     chat no es una fila de ninguna tabla.
--
-- Todo es ADITIVO: una columna que acepta NULL, sin valor por omisión y sin CHECK. No se reescribe
-- ninguna fila, no se borra ni se renombra nada que exista y las notas de hoy quedan con
-- `autor_chat_id` NULL (no se inventa un autor para ellas).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo
-- DDL en producción) y con `lock_timeout` (el ALTER pide por un instante el candado más fuerte de
-- la tabla y espera detrás de cualquier consulta larga que la esté leyendo).
--
-- EL PROCESO VIEJO (el código de `9fb5f0c`, que no conoce la columna) SIGUE ANDANDO con la tabla
-- cambiada, también si Telegram escribe una nota justo en ese momento:
--   · su único `INSERT INTO notas` (`crud.crear_desde_interpretacion`) nombra las columnas una por
--     una y no nombra la nueva: entra con NULL. Si llega mientras el `ALTER` tiene el candado, espera
--     ese instante y entra, antes o después del cambio, con el mismo resultado;
--   · `crud.editar`, `crud.borrar` y `crud.deshacer` leen la fila con `SELECT *` por nombre de columna
--     (`dict_row`) y las consultas preparadas están apagadas para todo el proceso
--     (`db/sin_preparadas.py`), así que una columna de más no los rompe;
--   · `db/lectura_dueno.py` nombra sus columnas.
-- Y el código nuevo anda con la columna AUSENTE: la página carga sin el bloque de notas
-- (`db.notas_de_proyectos` devuelve «no disponible» con SQLSTATE 42703) y las tres rutas de notas
-- contestan que NO se guardó nada.
--
-- IDEMPOTENTE: `ADD COLUMN IF NOT EXISTS` no hace nada la segunda vez (el comentario se vuelve a
-- poner igual).
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después del `ALTER`,
-- `SELECT count(*) FROM notas` igual que antes, `SELECT count(autor_chat_id) FROM notas` = 0 (ninguna
-- fila trae autor) y `information_schema.columns` con UNA fila `notas.autor_chat_id` de tipo
-- `bigint`, `is_nullable = YES`, sin valor por omisión.
--
-- CÓMO SE DESHACE (solo si el código nuevo ya no está desplegado; con el código nuevo vivo, borrar
-- la columna hace que la página no dibuje el bloque de notas, pero los autores que se escribieron en
-- ella se pierden: las notas siguen, sin autor):
--   ALTER TABLE notas DROP COLUMN IF EXISTS autor_chat_id;

BEGIN;

ALTER TABLE notas ADD COLUMN IF NOT EXISTS autor_chat_id BIGINT;

COMMENT ON COLUMN notas.autor_chat_id IS
  'Quién escribió la nota desde el panel: el chat de la sesión. NULL = no la escribió el panel '
  '(las de Telegram dicen quién por bandeja_id -> bandeja.chat_id).';

COMMIT;
