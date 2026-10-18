-- Las consultas de Natalia (pieza L3 del diseño «Natalia y los Proyectos», 18-oct-2026;
-- disenos/natalia-tareas-en-proyectos/DISENO.md §5.1).
--
-- Cuando un cliente necesita algo, Natalia NO escribe una tarea en Lucy: abre una CONSULTA
-- (`POST /api/code/clientes/{noco_id}/consultas`) y Lucy es la que le pregunta al responsable por
-- Telegram. Esta tabla es esa consulta. Hay dos tipos:
--   · `tarea_nueva`: el cliente pidió algo; si el responsable dice «Sí, se puede», Lucy anota la tarea.
--   · `nueva_fecha`: la tarea que sigue en el proyecto del cliente ya pasó su fecha; se le pregunta al
--     responsable por una fecha nueva.
--
-- Columnas (todas las de §5.1):
--   · `clave` ÚNICA: la arma el código de Natalia; la misma clave devuelve la misma consulta, así un
--     reintento no duplica.
--   · `tipo` y `estado` con CHECK (vocabulario cerrado), y un CHECK que ata cada tipo a SUS estados:
--     `tarea_nueva` termina en `anotada`, `rechazada` o `no_se_pudo`; `nueva_fecha` en `resuelta`.
--   · `cliente_noco_id`: la ficha de NocoDB del cliente; `proyecto_id`: su proyecto (FK).
--   · `tarea_nueva`: `titulo`, `pedido` (lo que dijo el cliente), `vence_en` propuesta (instante, como
--     `tareas.vence_en`; NULL = sin fecha). `nueva_fecha`: `tarea_id` y `vence_vencida_en`, la fecha que
--     ya había vencido. Un CHECK exige que cada tipo traiga lo suyo y nada del otro.
--   · `responsable_chat_id` (BIGINT, sin FK, como `tareas.responsable_chat_id`): a quién se le pregunta.
--   · `creada_en`, `tarjeta_msg_id`, `enviada_en`, `recordada_en`, `contestada_por_chat_id`,
--     `contestada_en`, `tarea_resultante_id` (FK; la tarea que nació del «Sí») y `motivo` (si no se pudo):
--     los escribe L4/L5 por `db.pasar_consulta_de_natalia`, la única función que cambia el estado.
--   · Índice único PARCIAL: UNA `nueva_fecha` viva (`abierta` o `preguntada`) por tarea.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción), JUSTO ANTES de desplegar, y con el ensayo con ROLLBACK primero (cabecera de
-- `tools/ensayo_cotizaciones.py` para el patrón; el texto lo arma `sql_del_ensayo` sobre este archivo).
-- La tabla es NUEVA: no se reescribe ninguna fila ni se toca ninguna tabla que exista.
--
-- EL CÓDIGO NUEVO TOLERA LA BASE SIN MIGRAR: las dos rutas de consultas contestan 503 («falta una
-- migración») si la tabla no existe (SQLSTATE 42P01), sin guardar nada; el proceso viejo no nombra esta
-- tabla en ningún SQL, así que una tabla de más no lo rompe.
--
-- IDEMPOTENTE: `CREATE TABLE IF NOT EXISTS` y `CREATE UNIQUE INDEX IF NOT EXISTS` no hacen nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de los `CREATE`,
-- `SELECT count(*) FROM consultas_de_natalia` = 0 (nace vacía), `information_schema.tables` con UNA fila
-- (`consultas_de_natalia`) y en `pg_constraint` las restricciones con nombre de esta tabla (las que dice este
-- mismo archivo: el guion las saca del texto). Al terminar, con el ROLLBACK, `to_regclass` da NULL.
--
-- CÓMO SE DESHACE (solo si el código que la usa ya no está desplegado; con él vivo se pierden las consultas):
--   DROP TABLE IF EXISTS consultas_de_natalia;

BEGIN;

CREATE TABLE IF NOT EXISTS consultas_de_natalia (
  id                     BIGSERIAL PRIMARY KEY,
  clave                  TEXT NOT NULL,
  tipo                   TEXT NOT NULL,
  estado                 TEXT NOT NULL DEFAULT 'abierta',
  cliente_noco_id        BIGINT NOT NULL,
  proyecto_id            BIGINT NOT NULL REFERENCES proyectos(id),
  titulo                 TEXT,
  pedido                 TEXT,
  vence_en               TIMESTAMPTZ,
  tarea_id               BIGINT REFERENCES tareas(id),
  vence_vencida_en       TIMESTAMPTZ,
  responsable_chat_id    BIGINT NOT NULL,
  creada_en              TIMESTAMPTZ NOT NULL DEFAULT now(),
  tarjeta_msg_id         BIGINT,
  enviada_en             TIMESTAMPTZ,
  recordada_en           TIMESTAMPTZ,
  contestada_por_chat_id BIGINT,
  contestada_en          TIMESTAMPTZ,
  tarea_resultante_id    BIGINT REFERENCES tareas(id),
  motivo                 TEXT,
  CONSTRAINT consultas_de_natalia_clave_unica
    UNIQUE (clave),
  CONSTRAINT consultas_de_natalia_tipo_valido
    CHECK (tipo IN ('tarea_nueva', 'nueva_fecha')),
  CONSTRAINT consultas_de_natalia_estado_valido
    CHECK (estado IN ('abierta', 'preguntada', 'anotada', 'rechazada', 'no_se_pudo', 'resuelta')),
  CONSTRAINT consultas_de_natalia_estado_del_tipo
    CHECK ((tipo = 'tarea_nueva' AND estado IN ('abierta', 'preguntada', 'anotada', 'rechazada', 'no_se_pudo')) OR
           (tipo = 'nueva_fecha' AND estado IN ('abierta', 'preguntada', 'resuelta'))),
  CONSTRAINT consultas_de_natalia_campos_del_tipo
    CHECK ((tipo = 'tarea_nueva' AND titulo IS NOT NULL AND pedido IS NOT NULL AND
            tarea_id IS NULL AND vence_vencida_en IS NULL) OR
           (tipo = 'nueva_fecha' AND tarea_id IS NOT NULL AND titulo IS NULL AND pedido IS NULL AND
            vence_en IS NULL)),
  CONSTRAINT consultas_de_natalia_clave_valida
    CHECK (length(clave) BETWEEN 1 AND 120)
);

-- UNA consulta de nueva fecha viva por tarea: la última puerta contra dos aperturas a la vez.
CREATE UNIQUE INDEX IF NOT EXISTS idx_consultas_de_natalia_nueva_fecha_viva
  ON consultas_de_natalia (tarea_id)
  WHERE tipo = 'nueva_fecha' AND estado IN ('abierta', 'preguntada');

-- Lo que lee el despertador (L4) y la lectura de Natalia: por estado, y por ficha.
CREATE INDEX IF NOT EXISTS idx_consultas_de_natalia_estado
  ON consultas_de_natalia (estado)
  WHERE estado IN ('abierta', 'preguntada');
CREATE INDEX IF NOT EXISTS idx_consultas_de_natalia_cliente
  ON consultas_de_natalia (cliente_noco_id);

COMMIT;
