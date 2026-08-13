-- El 0 vuelve a las filas a las que `editar` se lo robó (13-ago-2026).
--
-- crud._anticipos() existe para garantizar una sola cosa: que la campanada a la
-- hora exacta (el 0) esté SIEMPRE en anticipos_min. crear_desde_interpretacion
-- la aplicaba; crud.editar NO. Y editar es el camino más transitado de los dos:
-- "recordámelo 30 minutos antes" sobre algo que YA existe es una edición, no
-- una creación. Llegaba {"anticipos_min": [30]} y se guardaba tal cual, así que
-- esa fila avisaba 30' antes y nunca a la hora — justo el aviso que importa.
--
-- Al escribir esto había 1 fila así viva en producción: la tarea #69 ("Cambiar
-- el bombillo de la cocina del estudio", vencía el 7-ago 14:00 UTC), con
-- anticipos_min = {30} y avisos_enviados = {30}. Ese par es la prueba del
-- agujero: la campanada anticipada sonó, la de la hora no sonó nunca.
--
-- ⚠️ `cardinality(anticipos_min) > 0` es la guarda que NO se puede quitar. La
-- lista VACÍA significa "esta fila no avisa nunca", y así entran los 48 eventos
-- espejados de Google Calendar desde el cambio de esta misma mañana
-- (2026-08-13_calendario_no_avisa.sql). Convertir un '{}' en '{0}' volvería a
-- encender exactamente lo que Tiziano pidió apagar.
--
-- No dispara avisos retroactivos: el despertador solo mira hasta GRACIA_MIN
-- (120 minutos) después de la hora, y lo que esto toca venció hace días. Lo que
-- arregla es el FUTURO de esas filas: si a la #69 le mueven la fecha —o si
-- fuera recurrente— ahora sí suena a la hora.
--
-- El `m >= 0` copia el descarte de negativos del helper, para que la fila que
-- queda en la base sea la misma que habría escrito el código.
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

UPDATE tareas
   SET anticipos_min = ARRAY(
         SELECT DISTINCT m FROM unnest(anticipos_min || 0) m
          WHERE m >= 0 ORDER BY m DESC)
 WHERE borrado_en IS NULL
   AND cardinality(anticipos_min) > 0
   AND NOT (anticipos_min @> '{0}');

UPDATE eventos
   SET anticipos_min = ARRAY(
         SELECT DISTINCT m FROM unnest(anticipos_min || 0) m
          WHERE m >= 0 ORDER BY m DESC)
 WHERE borrado_en IS NULL
   AND cardinality(anticipos_min) > 0
   AND NOT (anticipos_min @> '{0}');
