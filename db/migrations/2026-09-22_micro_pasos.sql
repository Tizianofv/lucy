-- Micro-pasos (encargo 7, 22-sep-2026): lista de chequeo dentro de una
-- tarea. «No, es una lista de chequeo» (Tiziano) -- sin fecha, sin
-- responsable, sin aviso propio, no suena el despertador.
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py`
-- antes (regla del repo para todo DDL en producción). El código de este
-- encargo tiene que funcionar IGUAL mientras esta tabla no exista -- ver
-- `db.pasos_de_tarea`, `db.conteo_pasos` y `db.tarea_con_comentarios`, que
-- toleran SQLSTATE 42P01 (undefined_table) y devuelven "sin pasos" en vez
-- de reventar, y `acciones/crud.py::crear_pasos`, que hace lo mismo antes
-- de escribir.
--
-- El porqué de cada decisión de diseño (por qué `hecho` es BOOLEAN y no un
-- tercer estado, por qué `orden` es un entero simple, qué pasa con los
-- pasos de una tarea que se marca hecha / se borra / va al Historial / se
-- convierte en proyecto) está en `db/schema.sql`, al lado de esta misma
-- tabla -- no se repite acá para que no se separen las dos explicaciones.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- CREATE TABLE
-- IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.

BEGIN;

CREATE TABLE IF NOT EXISTS micro_pasos (
  id         BIGSERIAL PRIMARY KEY,
  tarea_id   BIGINT NOT NULL REFERENCES tareas(id),
  texto      TEXT NOT NULL,
  hecho      BOOLEAN NOT NULL DEFAULT false,
  orden      INT NOT NULL DEFAULT 0,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
  borrado_en TIMESTAMPTZ
);

COMMENT ON TABLE micro_pasos IS
  'Lista de chequeo dentro de una tarea (encargo 7). No es una tarea: sin '
  'fecha, sin responsable, sin aviso propio. Ver db/schema.sql para el '
  'porqué completo de cada decisión.';

CREATE INDEX IF NOT EXISTS idx_micro_pasos_tarea ON micro_pasos(tarea_id)
  WHERE borrado_en IS NULL;

COMMIT;
