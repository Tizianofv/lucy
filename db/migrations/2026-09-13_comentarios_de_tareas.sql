-- Los comentarios de una tarea, escritos desde el panel (13-sep-2026).
--
-- Pedido de Tiziano: «en el panel de Lucia quiero poder editar las fechas de
-- vencimiento, poder agregar comentarios.» Lo que decidió sobre los comentarios:
--   · se guardan APARTE, cada uno con quién y cuándo, y nadie —ni Lucy— pisa
--     el de otro;
--   · comentan los dos que entran al panel;
--   · Lucy los lee cuando le preguntan por esa tarea;
--   · cualquiera de los dos puede borrar cualquier comentario.
--
-- POR QUÉ UNA TABLA Y NO `tareas.detalle`: `detalle` es un solo texto, sin
-- autor ni hora, y Lucy lo reescribe por el chat. Un comentario guardado ahí lo
-- podía borrar Lucy la próxima vez que editara la tarea.
--
-- QUIÉN LO ESCRIBIÓ es un chat de Telegram (`autor_chat_id`), igual que
-- `tareas.responsable_chat_id`: el chat con el que esa persona entró al panel,
-- sacado de su sesión firmada y nunca de un campo del formulario. El nombre sale
-- de la variable NOMBRES_POR_CHAT, no de esta tabla. Sin FK a un chat porque no
-- existe tabla de chats.
--
-- BORRAR ES MARCAR, como en el resto de la base: `borrado_en` y
-- `borrado_por_chat_id`. El texto no se toca nunca. En el código no hay ninguna
-- escritura que cambie `texto`, y `acciones.crud` no puede escribir esta tabla
-- (no está en `crud.TABLAS`).
--
-- SOBRE LO QUE PASÓ EL 10-sep-2026 (`cached plan must not change result type`,
-- al agregarle una columna a `tareas` con la app viva). Ese error sale cuando
-- cambia la FORMA DEL RESULTADO de una consulta ya preparada: una columna más en
-- una tabla que un `SELECT *` preparado lee. Este archivo no agrega, quita ni
-- cambia ninguna columna de ninguna tabla existente: crea una tabla nueva, que
-- ninguna consulta vieja nombra. Lo que sí toca a `tareas` es la FK
-- (`tarea_id → tareas.id`): al crearla, Postgres toma un candado SHARE ROW
-- EXCLUSIVE sobre `tareas` durante el CREATE (bloquea escrituras a `tareas`, no
-- lecturas) y le agrega los disparadores internos de la FK. Eso obliga a
-- replanificar las consultas preparadas sobre `tareas`, pero no cambia la forma
-- de su resultado. Como la tabla nace vacía, no hay filas que validar y el
-- candado dura lo que dura el CREATE.
--   ESTO NO ESTÁ MEDIDO: en la máquina donde se escribió no hay servidor
--   Postgres. Es lo que dice el mecanismo, no una prueba. El orden seguro sigue
--   siendo el de siempre: respaldo antes (`python3 db/backup.py`), aplicar
--   justo antes de publicar, y mirar los registros del contenedor viejo en esa
--   ventana.
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

BEGIN;

CREATE TABLE IF NOT EXISTS comentarios_tarea (
  id                  BIGSERIAL PRIMARY KEY,
  tarea_id            BIGINT NOT NULL REFERENCES tareas(id),
  autor_chat_id       BIGINT NOT NULL,
  creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
  texto               TEXT NOT NULL,
  borrado_en          TIMESTAMPTZ,
  borrado_por_chat_id BIGINT
);

CREATE INDEX IF NOT EXISTS idx_comentarios_tarea_tarea
  ON comentarios_tarea(tarea_id);

COMMIT;
