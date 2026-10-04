-- Alertas técnicas GRAVES (4-oct-2026, Tiziano: «los graves debe mandarlo a
-- telegram y a code con un aviso de grave para que lo atienda primero»).
--
-- `tareas.grave`: la alerta técnica que le dejó Natalia a Code dijo que es
-- grave. Solo la sube `db.crear_o_reusar_alerta_tecnica` (con `grave=True`) y
-- NUNCA la baja: una falla que ya se declaró grave no deja de serlo porque
-- la siguiente repetición venga marcada como leve. `GET /api/code/tareas`
-- ordena las graves primero y `/tareas` las pinta con su etiqueta.
-- Toda tarea que no sea una alerta grave queda en `false`.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla
-- del repo para todo DDL en producción) y con el mismo cuidado de las
-- migraciones de `tomada_en` y `clave_tecnica` (`SELECT *` preparado sobre
-- `tareas` con la app viva).
--
-- EL CÓDIGO FUNCIONA IGUAL mientras esta migración no se haya corrido:
--   · `GET /api/code/tareas` cae a la consulta de antes y cada fila sale con
--     `grave: false`.
--   · `/tareas` no pinta la etiqueta ni reordena.
--   · `crear_o_reusar_alerta_tecnica(grave=False)` -- lo que usan las cinco
--     alarmas de Lucy -- no nombra la columna y no se entera.
--   · `crear_o_reusar_alerta_tecnica(grave=True)` SIN la columna devuelve
--     `None` y `POST /api/code/alertas` contesta 503: no guarda una alerta
--     grave sin poder decir que es grave.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS grave BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tareas.grave IS
  'true = la alerta técnica que originó esta tarea se declaró grave (hoy: '
  'las que manda Natalia por POST /api/code/alertas). Solo sube, nunca baja. '
  'La sala atiende primero las graves.';

COMMIT;
