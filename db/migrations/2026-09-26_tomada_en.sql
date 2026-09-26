-- «Tomada» (26-sep-2026, diseño «Code como responsable», §D — parte 3 del
-- plan de construcción disenos/lucy-code/DISENO.md). Cuándo la sala de
-- control EMPEZÓ a trabajar una tarea técnica suya, con `POST /api/code/
-- tareas/{id}/tomar` (`web/api_code.py`, permiso `tareas:tomar`), ANTES de
-- cerrarla con `db.cerrar_tarea_de_la_sala` (§4, ya en producción desde
-- `74841c4`).
--
-- NULL = todavía no la tomó nadie -- el estado normal de toda tarea que
-- nace, incluidas las 95 vivas de hoy (medido en el LEVANTAMIENTO del
-- 26-sep-2026, sin re-medir acá). NO es un `estado` nuevo: la tarea sigue
-- `pendiente` mientras la sala la trabaja, y pasa a `hecha` recién al
-- cerrarse -- es ORTOGONAL, mismo patrón incremental que `area`
-- (2026-09-22_areas.sql), `primero_id` (2026-09-22_primero.sql) y
-- `deriva_de_id` (2026-09-25_deriva_de.sql): una columna nullable más que
-- no cambia el significado de ninguna que ya exista.
--
-- QUIÉN LA ESCRIBE: solo `db.tomar_tarea_de_la_sala`, con la MISMA guarda
-- embebida en el SQL que ya usa `cerrar_tarea_de_la_sala` -- área EFECTIVA
-- Técnico, `responsable_chat_id = CHAT_ID_CODE`, `estado = 'pendiente'`, y
-- (nueva acá) `tomada_en IS NULL` todavía. Tomar una tarea que no es de
-- Code, o tomarla una segunda vez, no escribe nada -- la segunda llamada no
-- pisa la hora de la primera, porque la condición del `WHERE` ya la excluye
-- antes de llegar al `UPDATE`.
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py` antes
-- (regla del repo para todo DDL en producción) y DESPUÉS de que
-- `db/sin_preparadas.py` ya esté desplegado -- mismo requisito que las
-- migraciones de `primero_id` y `deriva_de_id`, con el mismo motivo: un
-- plan cacheado de `SELECT *` sobre `tareas` revienta al cambiarle la forma
-- mientras el proceso viejo sigue vivo.
--
-- EL CÓDIGO DE ESTA PARTE FUNCIONA IGUAL mientras esta migración no se haya
-- corrido, en los tres caminos que tocan `tomada_en`:
--   · `db.tareas_de_code_pendientes` (la lista de la puerta, §3): cae a la
--     consulta de ANTES si la columna no existe (SQLSTATE 42703, mismo
--     patrón que `tarea_con_comentarios` con `primero_id`) y cada fila
--     sale con `tomada_en: None` -- la lista sigue sirviendo, solo que sin
--     decir quién está "en curso".
--   · `db.tomar_tarea_de_la_sala` (la escritura, §D): SIN la columna no hay
--     nada sano que escribir -- a diferencia de una tarea derivada, acá no
--     existe un "tomar sin tomada_en" que tenga sentido -- así que devuelve
--     `None` (distinto de `True`/`False`) y lo registra en el log; la ruta
--     `POST /api/code/tareas/{id}/tomar` lo traduce a un 503 con un mensaje
--     claro, nunca un 500 mudo.
--   · El panel (`web/plantillas/tareas.html`, la etiqueta «🔧 en curso
--     desde…»): solo se pinta si `tomada_en` viene con valor: con la
--     migración sin correr, todas las filas la traen en `None` (por el
--     primer punto) y la etiqueta simplemente no aparece -- el panel no
--     revienta, no dice nada falso.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- ADD COLUMN
-- IF NOT EXISTS.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS tomada_en TIMESTAMPTZ;

COMMENT ON COLUMN tareas.tomada_en IS
  'Cuándo la sala de control empezó a trabajar esta tarea técnica (§D). '
  'NULL = todavía no la tomó nadie. Ortogonal a `estado`: sigue pendiente '
  'mientras se trabaja. La pone solo db.tomar_tarea_de_la_sala, con la '
  'misma guarda de valor que db.cerrar_tarea_de_la_sala.';

COMMIT;
