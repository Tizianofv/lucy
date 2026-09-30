-- Marca de dueño en `personas` y `preferencias` (27-sep-2026, §E — parte E
-- del plan de construcción disenos/lucy-code/DISENO.md, "Code como
-- responsable"). Para que la lectura de la parte A (`db/lectura_dueno.py`)
-- pueda decidir "esto es de Tiziano" en estas dos tablas, igual que ya lo
-- hace con `notas`/`movimientos` -- por la MISMA puerta, `bandeja_id` hacia
-- la `bandeja` que originó la fila, nunca un chat_id propio nuevo.
--
-- `bandeja_id BIGINT REFERENCES bandeja(id)`, NULLABLE -- mismo patrón que
-- `notas.bandeja_id`/`movimientos.bandeja_id`: una fila SIN `bandeja_id`
-- (todas las que ya existen hoy, y cualquiera que se cree antes de que el
-- código de esta parte se despliegue) queda "sin dueño" -- NUNCA se
-- adivina de quién es. El relleno de las filas viejas es un guion aparte
-- (`tools/rellenar_duenos.py`), que la sala corre después de esta
-- migración, y que tampoco adivina: si no hay de dónde sacarlo, la fila
-- queda sin dueño.
--
-- QUIÉN LAS ESCRIBE, desde este cambio:
--   · `personas`: `db._buscar_o_crear`/`db.buscar_o_crear_persona` (ahora
--     con `bandeja_id`) y `acciones/crud.py::perfil` (ya recibía
--     `bandeja_id`, ahora lo guarda en la fila y no solo en el log).
--   · `preferencias`: `acciones/crud.py::guardar_preferencia` (ya recibía
--     `bandeja_id`, mismo arreglo).
-- Un censo (`tests/test_duenos.py::
-- test_todo_insert_de_personas_y_preferencias_pone_bandeja_id`) recorre el
-- AST del repo entero y exige que TODO `INSERT INTO personas`/`INSERT INTO
-- preferencias` real incluya `bandeja_id` en su lista de columnas.
--
-- EL CÓDIGO DE ESTA PARTE FUNCIONA IGUAL mientras esta migración no se haya
-- corrido (SQLSTATE 42703, mismo patrón que `tomada_en`/`correo_reportado.
-- destino_chat_id`): los tres escritores caen al INSERT de antes -- sin
-- `bandeja_id` -- y la lectura de la parte A devuelve `[]` para estas dos
-- tablas en vez de reventar.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- ADD COLUMN
-- IF NOT EXISTS.

BEGIN;

ALTER TABLE personas ADD COLUMN IF NOT EXISTS bandeja_id BIGINT REFERENCES bandeja(id);
ALTER TABLE preferencias ADD COLUMN IF NOT EXISTS bandeja_id BIGINT REFERENCES bandeja(id);

COMMENT ON COLUMN personas.bandeja_id IS
  'La bandeja que originó esta persona (§E). NULL = sin dueño conocido -- '
  'todas las filas de antes de esta migración, y cualquiera creada antes '
  'de que el código de esta parte se despliegue. La usa db.lectura_dueno '
  'para decidir si esta persona es de Tiziano, con el mismo filtro de '
  'origen confiable que ya usa notas/movimientos.';

COMMENT ON COLUMN preferencias.bandeja_id IS
  'La bandeja que originó esta preferencia (§E). NULL = sin dueño '
  'conocido. Mismo criterio que personas.bandeja_id.';

COMMIT;
