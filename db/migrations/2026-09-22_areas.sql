-- Las áreas del panel (encargo 4, 22-sep-2026).
--
-- «Las áreas viven en una tabla, no escritas en el código» (diseño, sección
-- 3). De esa tabla salen las 4 opciones del panel, sus colores y lo que se le
-- explica a Lucy. Si mañana hay una quinta área, es una fila, y aparece sola
-- en los tres sitios — nada que desplegar.
--
-- LA CLAVE ES EL NOMBRE QUE SE VE, no un id sintético: 'CDS', 'ACD',
-- '🛠️ Técnico', '🏠 Personal'. Mismo principio que ya usan las categorías de
-- gastos (`cerebro/bancos/categorias.py`) y `correo_estado.cuenta` — un
-- vocabulario cerrado se guarda por su valor, no por un número que solo
-- significa algo si se lo traduce primero. Así, lo que Lucy escribe por
-- Telegram, lo que el panel guarda y lo que hay en la base son la MISMA
-- cadena, sin tabla de traducción en el medio.
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py` antes
-- (regla del repo para todo DDL en producción). El código de este encargo
-- tiene que funcionar igual mientras esto no se haya corrido — ver
-- `db.areas()`, `db.tareas_por_grupo()`: las dos toleran que la tabla o las
-- columnas todavía no existan (SQLSTATE 42P01 / 42703) y siguen andando sin
-- áreas, en vez de romper el panel entero.
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

BEGIN;

CREATE TABLE IF NOT EXISTS areas (
  clave  TEXT PRIMARY KEY,       -- 'CDS' | 'ACD' | '🛠️ Técnico' | '🏠 Personal'
  color  TEXT NOT NULL,          -- para la etiqueta del panel, un hex
  orden  INT NOT NULL DEFAULT 0  -- el orden en que se pintan
);

COMMENT ON TABLE areas IS
  'Vocabulario cerrado de áreas. La clave ES el nombre que se ve -- no hay '
  'traducción por medio. Panel, Lucy y la base leen esta tabla, nadie la '
  'copia a mano.';

-- Las 4 áreas de hoy. ON CONFLICT DO NOTHING: si alguien ya las insertó a
-- mano, o si esto se corre dos veces, no pisa el color ni el orden que ya
-- estén puestos.
INSERT INTO areas (clave, color, orden) VALUES
  ('CDS',          '#2b6cb0', 1),
  ('ACD',          '#805ad5', 2),
  ('🛠️ Técnico',   '#dd6b20', 3),
  ('🏠 Personal',   '#38a169', 4)
ON CONFLICT (clave) DO NOTHING;

ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS area TEXT REFERENCES areas(clave);
ALTER TABLE tareas    ADD COLUMN IF NOT EXISTS area TEXT REFERENCES areas(clave);

COMMENT ON COLUMN proyectos.area IS
  'El área del proyecto. NULL = sin área todavía, que se ve con su propia '
  'etiqueta gris en el panel -- no se esconde.';
COMMENT ON COLUMN tareas.area IS
  'El área de una tarea SUELTA (sin proyecto). Una tarea CON proyecto no '
  'guarda la suya acá -- la hereda del proyecto, y la restricción '
  'tareas_area_no_con_proyecto lo impone en la base, no en cada escritura. '
  'NULL = sin área, que también se ve y no se esconde.';

-- «Una tarea dentro de un proyecto nunca tiene un área propia distinta»
-- (decisión de Tiziano, "No, toma la del proyecto"). Se hace que el caso NO
-- PUEDA EXISTIR, no que se valide en cada escritura: con `proyecto_id`
-- puesto, `area` tiene que quedar en NULL, lo escriba el panel, lo escriba
-- Lucy por Telegram o lo escriba cualquier cosa que hable con esta base
-- directamente. Mismo patrón DROP...IF EXISTS + ADD que ya usa la
-- restricción de estado de movimientos (migración 2026-08-31b), para que
-- correr esto dos veces no reviente.
ALTER TABLE tareas DROP CONSTRAINT IF EXISTS tareas_area_no_con_proyecto;
ALTER TABLE tareas ADD CONSTRAINT tareas_area_no_con_proyecto
  CHECK (proyecto_id IS NULL OR area IS NULL);

COMMIT;
