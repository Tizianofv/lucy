-- «Primero:» (encargo 6, 22-sep-2026): una tarea puede esperar a otra.
--
-- «No, calladita hasta que toque» (Tiziano): una tarea con fecha que todavía
-- espera a otra no suena, y en el panel se ve gris, un poco hacia adentro,
-- con «→ Primero: <la de antes>» -- se «enciende» sola en cuanto la anterior
-- deja de estar pendiente. Nada de eso se guarda: se CALCULA en cada
-- lectura, con el mismo `primero_id` de acá, igual que "atrasada" y el
-- Historial no guardan nada tampoco (ver `db.grupo_de_tarea`).
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py` antes
-- (regla del repo para todo DDL en producción) y DESPUÉS de que
-- `db/sin_preparadas.py` (encargo 3) ya esté desplegado -- si no, una
-- consulta repetida de `SELECT * FROM tareas` con un plan cacheado revienta
-- al cambiar el tipo de resultado, el mismo defecto de fondo que motivó el
-- encargo 3. El código de este encargo tiene que funcionar IGUAL mientras
-- esta migración no se haya corrido -- ver `acciones/crud.py::
-- crear_desde_interpretacion` (cae a un INSERT sin `primero_id` si la
-- columna no existe: SQLSTATE 42703) y `db.tareas_por_grupo` (cae a la
-- consulta de antes de este encargo, sin JOIN a la tarea "Primero:").
--
-- SIN CÍRCULOS: esto NO lo impone la base -- ver el comentario largo al
-- lado de `idx_tareas_primero_id` en `db/schema.sql` para el porqué (un
-- disparador lo podría hacer, y ESTE REPO LOS PROHÍBE:
-- `tests/test_fechas_del_panel.py::
-- test_ningun_sql_del_repo_crea_disparadores_ni_reglas_leido_como_texto`).
-- Lo que la base SÍ impone es que una tarea no sea su propia «Primero:»
-- (el CHECK de abajo); la cadena más larga la corta
-- `acciones/crud.py::_primero_que_vale`, la única puerta, para crear y
-- para editar.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- ADD COLUMN
-- IF NOT EXISTS para la columna, DROP...IF EXISTS + ADD para la
-- restricción, CREATE INDEX IF NOT EXISTS para el índice.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS primero_id BIGINT REFERENCES tareas(id);

COMMENT ON COLUMN tareas.primero_id IS
  'Qué otra tarea tiene que estar HECHA antes que ésta. NULL = no espera a '
  'nadie. Se ve gris en el panel mientras la de antes siga pendiente; se '
  'enciende sola -- nada se reescribe acá cuando eso pasa, se calcula al '
  'pintar (ver db.grupo_de_tarea y db.tareas_por_grupo). Sin círculos: lo '
  'impone acciones/crud.py::_primero_que_vale, no la base (ver ahí y en '
  'db/schema.sql por qué no hay un disparador).';

-- Una tarea no puede ser su propia «Primero:». Mismo nombre que
-- db/schema.sql, para que una base armada desde el archivo y una migrada
-- terminen con la restricción UNA sola vez.
ALTER TABLE tareas DROP CONSTRAINT IF EXISTS tareas_primero_no_a_si_misma;
ALTER TABLE tareas ADD CONSTRAINT tareas_primero_no_a_si_misma
  CHECK (primero_id IS NULL OR primero_id <> id);

-- Para la exclusión del despertador (`cerebro/despertador.py::revisar`) y
-- para el panel (`db.tareas_por_grupo`), que hacen un LEFT JOIN de `tareas`
-- contra sí misma por `primero_id`. Parcial: solo indexa las filas que de
-- verdad esperan a otra, que van a ser pocas.
CREATE INDEX IF NOT EXISTS idx_tareas_primero_id ON tareas(primero_id)
  WHERE primero_id IS NOT NULL;

COMMIT;
