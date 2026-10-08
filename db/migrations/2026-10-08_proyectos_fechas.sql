-- Fecha de inicio, fecha de entrega y «Termina cuando» de un proyecto (parte 4 de
-- la página completa del proyecto, 8-oct-2026; Tiziano: «Fecha de inicio, fecha de
-- entrega, la seccion de tareas igual como esta ahora con sus fechas»).
--
--   · `proyectos.inicio` (DATE): el día en que arrancó. Los proyectos que ya
--     existen arrancan con su DÍA DE CREACIÓN EN SANTO DOMINGO (no el día UTC:
--     uno creado a las 10 de la noche allá ya es el día siguiente en UTC).
--     No se deja vacío: `crud.PUERTAS` rechaza vaciarlo.
--   · `proyectos.entrega` (DATE): el día en que debe estar listo. NULL = sin fecha.
--   · `proyectos.termina_cuando` (TEXT): lo que tiene que ser verdad para dar el
--     proyecto por terminado, en palabras de quien lo lleva. NULL = sin escribir.
--   · `proyectos_entrega_despues_del_inicio`: la entrega no puede ser antes del
--     inicio (la misma regla que aplica `crud.editar`; esto es el último recurso
--     de la base).
--
-- Todo es ADITIVO: columnas que aceptan NULL y un CHECK que las filas de hoy
-- cumplen (`entrega` nace NULL). No se borra ni se renombra nada que exista.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del
-- repo para todo DDL en producción).
--
-- EL CÓDIGO FUNCIONA IGUAL mientras esta migración no se haya corrido:
--   · la página de proyectos carga (las fechas se leen aparte, `db.fechas_de_
--     proyectos`, que con la columna ausente devuelve «no disponible»: el bloque
--     no dibuja fechas ni el enlace para cambiarlas);
--   · crear un proyecto sigue andando (`db._con_su_inicio` tolera la columna
--     ausente y deja el proyecto sin `inicio`; la migración se lo pone después);
--   · `crud.editar` de `inicio`/`entrega`/`termina_cuando` se rechaza sin escribir
--     («Esa tabla no tiene: …»).
--
-- IDEMPOTENTE, pero no es un no-hacer-nada: ADD COLUMN IF NOT EXISTS no hace
-- nada la segunda vez, el UPDATE solo toca filas con `inicio` NULL (ninguna, si
-- nadie creó un proyecto sin `inicio` entre las dos corridas), y la segunda
-- corrida suelta y vuelve a poner el CHECK, que recorre `proyectos` una vez más y
-- toma por un instante el candado de esa tabla.
--
-- CÓMO SE DESHACE (solo si el código nuevo ya no está desplegado; con el código
-- nuevo vivo, borrar las columnas hace que la página no dibuje fechas y que crear
-- un proyecto siga igual, pero lo que se escribió en ellas se pierde):
--   ALTER TABLE proyectos DROP CONSTRAINT IF EXISTS proyectos_entrega_despues_del_inicio;
--   ALTER TABLE proyectos DROP COLUMN IF EXISTS termina_cuando;
--   ALTER TABLE proyectos DROP COLUMN IF EXISTS entrega;
--   ALTER TABLE proyectos DROP COLUMN IF EXISTS inicio;

BEGIN;

ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS inicio DATE;
ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS entrega DATE;
ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS termina_cuando TEXT;

UPDATE proyectos
   SET inicio = (creado_en AT TIME ZONE 'America/Santo_Domingo')::date
 WHERE inicio IS NULL;

COMMENT ON COLUMN proyectos.inicio IS
  'El día en que arrancó el proyecto (día de Santo Domingo). Al crearlo es el '
  'día de creación; se puede corregir por crud.editar, pero no se deja vacío.';
COMMENT ON COLUMN proyectos.entrega IS
  'El día en que el proyecto debe estar listo. NULL = sin fecha de entrega. '
  'No puede ser anterior a `inicio`.';
COMMENT ON COLUMN proyectos.termina_cuando IS
  'Texto libre: qué tiene que ser verdad para dar el proyecto por terminado. '
  'NULL = sin escribir. Se pinta siempre escapado.';

ALTER TABLE proyectos DROP CONSTRAINT IF EXISTS proyectos_entrega_despues_del_inicio;
ALTER TABLE proyectos ADD CONSTRAINT proyectos_entrega_despues_del_inicio
  CHECK (entrega IS NULL OR inicio IS NULL OR entrega >= inicio);

COMMIT;
