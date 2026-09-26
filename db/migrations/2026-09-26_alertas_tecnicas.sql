-- Alarmas técnicas convertidas en tareas de Code (26-sep-2026, diseño «Code
-- como responsable», §B — parte 4 del plan de construcción
-- disenos/lucy-code/DISENO.md). Las cinco alarmas técnicas de Lucy
-- (el respaldo, las tres señales del canario de bancos, el latido de la
-- cosecha de correo) dejan de mandarle a Tiziano un mensaje directo por
-- Telegram y en su lugar crean -- o reusan -- una tarea Técnica con
-- responsable Code, vía `db.crear_o_reusar_alerta_tecnica`.
--
-- `clave_tecnica` identifica DE QUÉ PROBLEMA se trata ("backup",
-- "latido_cosecha", "canario:<remitente>:<señal>") y es la puerta del
-- dedupe: la misma falla vista cada ~15 minutos reusa la tarea abierta con
-- esa clave en vez de crear una nueva. `ultima_alarma_en` es cuándo se vio
-- esa falla por última vez, y es el reloj del aviso de las 6 horas
-- (`cerebro/despertador.py::revisar_alertas_tecnicas_sin_tomar`): si la
-- sala no toma la tarea (`tomada_en`, §D, ya en producción desde
-- `79943d1`) en las 6 horas siguientes a la última señal real, Tiziano se
-- entera como antes.
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py`
-- antes (regla del repo para todo DDL en producción) y DESPUÉS de que
-- `db/sin_preparadas.py` ya esté desplegado -- mismo requisito que las
-- migraciones de `primero_id`, `deriva_de_id` y `tomada_en`: un plan
-- cacheado de `SELECT *` sobre `tareas` revienta al cambiarle la forma
-- mientras el proceso viejo sigue vivo.
--
-- EL CÓDIGO DE ESTA PARTE FUNCIONA IGUAL mientras esta migración no se haya
-- corrido: `db.crear_o_reusar_alerta_tecnica` devuelve `None` si la columna
-- no existe (SQLSTATE 42703), y las cinco alarmas, al recibir `None`, caen
-- a mandar el aviso exactamente como lo hacían ANTES de esta parte --
-- directo a Tiziano por Telegram (el respaldo) o por la bandeja (el
-- canario y el latido) -- nunca se pierde un aviso por el cambio.
-- `revisar_alertas_tecnicas_sin_tomar` hace lo mismo: sin la columna,
-- no revisa nada y devuelve 0, en vez de reventar.
--
-- Idempotente: correrlo dos veces no hace nada la segunda -- ADD COLUMN
-- IF NOT EXISTS para las columnas, CREATE INDEX IF NOT EXISTS para el
-- índice.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS clave_tecnica TEXT;
ALTER TABLE tareas ADD COLUMN IF NOT EXISTS ultima_alarma_en TIMESTAMPTZ;

COMMENT ON COLUMN tareas.clave_tecnica IS
  'Identifica de qué problema técnico se trata ("backup", '
  '"latido_cosecha", "canario:<remitente>:<señal>"). NULL en toda tarea '
  'que no sea una alarma técnica de Lucy. La pone/actualiza solo '
  'db.crear_o_reusar_alerta_tecnica -- es la clave del dedupe.';
COMMENT ON COLUMN tareas.ultima_alarma_en IS
  'Cuándo se vio esta falla técnica por última vez, mientras la tarea '
  'sigue abierta. Reloj del aviso de 6 horas en '
  'cerebro/despertador.py::revisar_alertas_tecnicas_sin_tomar.';

CREATE INDEX IF NOT EXISTS idx_tareas_clave_tecnica ON tareas(clave_tecnica)
  WHERE clave_tecnica IS NOT NULL;

COMMIT;
