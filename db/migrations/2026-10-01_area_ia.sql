-- El grupo «IA» (Lucy 1.0, encargo E1, 1-oct-2026) -- PASO 1 de 2, antes de
-- desplegar.
--
-- «🛠️ Técnico» pasa a llamarse «IA». Un solo `UPDATE areas SET clave = 'IA'`
-- no se puede: `proyectos.area` y `tareas.area` apuntan a `areas(clave)` con
-- una FK sin `ON UPDATE CASCADE`. Y cambiar `db.AREA_TECNICA` y la base en el
-- mismo instante tampoco: entre la migración y el despliegue,
-- `db.crear_o_reusar_alerta_tecnica` insertaría un área que no existe y la FK
-- reventaría. Por eso son dos pasos y el caso no existe:
--
--   1. ESTE ARCHIVO (antes de desplegar): crea el área «IA». No rompe nada del
--      código viejo; el desplegable del panel ofrece un área más por unos
--      minutos.
--   2. Se despliega el código con `db.AREA_TECNICA = "IA"`.
--   3. `2026-10-01b_area_ia_mover_y_borrar.sql` (después de verificar el
--      despliegue): mueve a «IA» lo que estaba en «🛠️ Técnico» y en «🏠 Personal»
--      y borra las dos áreas viejas.
--
-- Los colores son los de la maqueta aprobada de Proyectos: así `/tareas` y la
-- página nueva usan el mismo, y sale de `areas.color` sin estar escrito en
-- ninguna plantilla. CDS y ACD cambian de color en este mismo paso.
--
-- TRAMPA: `2026-09-22_areas.sql` siembra «🛠️ Técnico» y «🏠 Personal» si alguien
-- lo vuelve a correr sobre una base nueva. Este archivo y el siguiente se
-- corren DESPUÉS de ese, en orden de nombre; `tests/test_grupo_ia.py` aplica
-- todas las migraciones en orden y exige que queden CDS, ACD e IA.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla
-- del repo para todo DDL en producción). Idempotente.

BEGIN;

INSERT INTO areas (clave, color, orden) VALUES
  ('IA', '#8a4a8f', 3)
ON CONFLICT (clave) DO NOTHING;

UPDATE areas SET color = '#0f7c74' WHERE clave = 'CDS';
UPDATE areas SET color = '#b5611a' WHERE clave = 'ACD';

COMMIT;
