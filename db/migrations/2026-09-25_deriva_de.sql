-- «Sale de» (tarea derivada, 25-sep-2026): al marcar una tarea hecha en el
-- panel /tareas, se puede escribir ahí mismo una o varias tareas nuevas que
-- salen de ella. Pedido de Tiziano, textual: «muchas veces una tarea hecha
-- da como resultado una tarea nueva que se deriva de la completada».
-- Diseño aprobado en disenos/lucy-tarea-derivada/DISENO.md.
--
-- `deriva_de_id` es AL REVÉS de `primero_id`: ahí A espera a que B se marque
-- hecha; acá B (la nueva) nace cuando A se marca hecha, y no hay espera ni
-- aviso apagado de por medio -- por eso es una columna nueva y no una
-- reutilización de `primero_id` (una tarea recurrente vuelve a `pendiente`
-- sola, y con `primero_id` la hija quedaría "esperando" para siempre).
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py` antes
-- (regla del repo para todo DDL en producción) y DESPUÉS de que
-- `db/sin_preparadas.py` ya esté desplegado -- mismo requisito que la
-- migración de `primero_id`, con el mismo motivo: un plan cacheado de
-- `SELECT *` sobre `tareas` revienta al cambiarle la forma.
--
-- El código de este encargo funciona IGUAL mientras esta migración no se
-- haya corrido: `db.cerrar_y_derivar` cae a un INSERT sin `deriva_de_id` si
-- la columna no existe (SQLSTATE 42703, mismo patrón que el área en
-- `db.crear_tarea_desde_el_panel`) y `db.derivaciones()` devuelve `{}`.
--
-- SIN CÍRCULOS: no hace falta impedirlo -- una hija nace SIEMPRE con
-- `estado='pendiente'` y nunca se vuelve a marcar hecha por este camino
-- apuntando hacia atrás; no hay ninguna puerta que reescriba `deriva_de_id`
-- después de creada la fila (está fuera de `acciones/crud.py::editar` a
-- propósito, ver `acciones/crud.py::NO_EDITABLES`). Queda el CHECK de una
-- sola fila, igual que `primero_id`, contra que una tarea "salga de" sí
-- misma.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- ADD COLUMN
-- IF NOT EXISTS para la columna, DROP...IF EXISTS + ADD para la
-- restricción, CREATE INDEX IF NOT EXISTS para el índice.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS deriva_de_id BIGINT REFERENCES tareas(id);

COMMENT ON COLUMN tareas.deriva_de_id IS
  'De qué tarea sale ésta, al marcarse hecha la de antes desde el panel. '
  'NULL = no sale de ninguna. La relación se CALCULA al pintar (ver '
  'db.derivaciones y db.tareas_por_grupo), nunca se guarda "hijos". Cerrada '
  'al agente de Telegram por acciones/crud.py::editar (NO_EDITABLES).';

-- Una tarea no puede «salir de» sí misma. Mismo nombre que db/schema.sql,
-- para que una base armada desde el archivo y una migrada terminen con la
-- restricción UNA sola vez.
ALTER TABLE tareas DROP CONSTRAINT IF EXISTS tareas_deriva_no_de_si_misma;
ALTER TABLE tareas ADD CONSTRAINT tareas_deriva_no_de_si_misma
  CHECK (deriva_de_id IS NULL OR deriva_de_id <> id);

-- Para `db.derivaciones`, que trae todas las filas con `deriva_de_id` no
-- nulo. Parcial: van a ser pocas.
CREATE INDEX IF NOT EXISTS idx_tareas_deriva_de_id ON tareas(deriva_de_id)
  WHERE deriva_de_id IS NOT NULL;

COMMIT;
