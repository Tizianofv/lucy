-- Recordatorios configurables por fila (30-jul-2026).
--
-- Cambia el default de aviso de DOBLE (−30' y a la hora) a UNO SOLO a la hora
-- exacta. El anticipado pasa a ser opt-in por recordatorio, guardado en esta
-- columna: {0} = solo a la hora; {30,0} = 30' antes y a la hora; {1440,0} = el
-- día antes y a la hora.
--
-- Aditiva y segura: sin backfill. Las filas viejas quedan en {0} (el default),
-- o sea un único aviso a la hora — el nuevo comportamiento por defecto.
--
-- ⚠️ Correr ANTES de desplegar el código nuevo: el despertador y crud.py leen
-- esta columna al arrancar; si no existe, revientan.

ALTER TABLE tareas  ADD COLUMN IF NOT EXISTS anticipos_min INT[] NOT NULL DEFAULT '{0}';
ALTER TABLE eventos ADD COLUMN IF NOT EXISTS anticipos_min INT[] NOT NULL DEFAULT '{0}';
