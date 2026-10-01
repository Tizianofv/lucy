-- El grupo «IA» (Lucy 1.0, encargo E1, 1-oct-2026) -- PASO 2 de 2, DESPUÉS de
-- desplegar y verificar el código con `db.AREA_TECNICA = "IA"`. Explicación
-- completa en `2026-10-01_area_ia.sql`.
--
-- En una sola transacción:
--   1. Los proyectos VIVOS de «🏠 Personal» que no tienen ninguna tarea viva
--      van a la papelera, como lo hace `crud.borrar`: `borrado_en = now()` y
--      una huella `accion = 'borrar'` en `log_acciones` con el `antes`, para
--      que `crud.deshacer` los pueda recuperar. (Decisión de Tiziano,
--      1-oct-2026: el grupo «🏠 Personal» desaparece y el proyecto «Casa», que
--      está vacío, va a la papelera.) Se escoge por estar vacío, no por el
--      nombre; uno de ese grupo con tareas vivas NO se borra: pasa a «IA».
--   2. Todos los proyectos de «🛠️ Técnico» y de «🏠 Personal» pasan a «IA»
--      (también los de la papelera: la FK no deja borrar un área que alguien
--      todavía apunta).
--   3. Lo mismo para las tareas con área propia (las que tienen proyecto no
--      guardan la suya: `tareas_area_no_con_proyecto`).
--   4. Se borran las dos áreas viejas.
--
-- Falla fuerte si el área «IA» no existe (la FK revienta y nada se aplica):
-- el paso 1 (`2026-10-01_area_ia.sql`) tiene que estar corrido. Idempotente:
-- la segunda vez no encuentra nada que mover.
--
-- Sin datos de tareas en este archivo: solo SQL. Para correrlo con un conteo
-- previo, la sala usa su guion `m1b_grupo_ia.py` (por defecto solo cuenta).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes.

BEGIN;

INSERT INTO log_acciones (actor, accion, tabla, registro_id, antes, motivo)
SELECT 'sala', 'borrar', 'proyectos', p.id,
       jsonb_build_object('id', p.id, 'creado_en', p.creado_en,
                          'nombre', p.nombre, 'descripcion', p.descripcion,
                          'estado', p.estado, 'borrado_en', p.borrado_en,
                          'area', p.area),
       'el grupo 🏠 Personal desaparece (Lucy 1.0): el proyecto vacío va a la papelera'
  FROM proyectos p
 WHERE p.area = '🏠 Personal'
   AND p.borrado_en IS NULL
   AND NOT EXISTS (SELECT 1 FROM tareas t
                    WHERE t.proyecto_id = p.id AND t.borrado_en IS NULL);

UPDATE proyectos SET borrado_en = now()
 WHERE area = '🏠 Personal'
   AND borrado_en IS NULL
   AND NOT EXISTS (SELECT 1 FROM tareas t
                    WHERE t.proyecto_id = proyectos.id AND t.borrado_en IS NULL);

UPDATE proyectos SET area = 'IA'
 WHERE area IN ('🛠️ Técnico', '🏠 Personal');

UPDATE tareas SET area = 'IA'
 WHERE area IN ('🛠️ Técnico', '🏠 Personal');

DELETE FROM areas WHERE clave IN ('🛠️ Técnico', '🏠 Personal');

COMMIT;
