-- Los eventos espejados de Google Calendar dejan de avisar (13-ago-2026).
--
-- Pedido de Tiziano: "está bien que Lucy VEA los calendarios, pero no necesito
-- recordatorios del calendario — ya el mismo calendario me da recordatorios".
-- Eran 2 a 8 avisos por día, casi todos sesiones de las salas del estudio,
-- todos duplicando el recordatorio que Google ya manda.
--
-- El corte es por el ORIGEN del evento (gcal_id NOT NULL = espejo de Google),
-- NO por la maquinaria: las tareas de Tiziano y las citas que él le pide a
-- Lucy por Telegram (gcal_id NULL) conservan su '{0}' y siguen sonando igual.
-- El default de la columna NO se toca por eso mismo.
--
-- El `AND anticipos_min = '{0}'` es la parte importante: solo apaga lo que
-- está en el default, o sea lo que nadie pidió. Si Tiziano alguna vez pidió
-- "recordame 30 min antes de esa reunión" sobre una cita de Google, esa fila
-- tiene otro valor y este UPDATE la deja intacta. (Al correrlo eran 0 filas
-- así de 48, pero la guarda queda escrita: la próxima vez puede no ser 0.)
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

UPDATE eventos
   SET anticipos_min = '{}'
 WHERE gcal_id IS NOT NULL
   AND anticipos_min = '{0}';
