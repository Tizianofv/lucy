-- Lucy 1.0, encargo E2 «la base» (diseño §3.2, M2): lo que el proyecto no tiene.
--
--   · `proyectos.responsable_chat_id`: quién lleva el proyecto (Rosi, Tiziano o
--     Code). NULL = sin responsable, que se ve «—». Sin FK, igual que
--     `tareas.responsable_chat_id`: la lista de quién vale sale de
--     `config.puede_ser_responsable`, no de una tabla.
--   · `proyectos.cliente_noco_id` y `proyectos.cliente_nombre`: la ficha del
--     cliente en el Noco de CDS y el nombre que tenía al elegirla. Se guarda el
--     nombre junto al Id porque (1) la página se pinta aunque Noco esté caído,
--     (2) buscar por cliente filtra sin llamar a Noco y (3) los Id de Noco se
--     borran cuando Natalia une fichas. El cliente es OPCIONAL (Tiziano, 1-oct:
--     «No, no todos los proyectos tienen cliente»): las dos columnas NULL. El
--     CHECK impide que quede una sola de las dos.
--   · `comentarios_tarea.editado_en`: NULL = el comentario nunca se editó.
--   · `participantes`: las «personas» de un proyecto o de una tarea, con «qué
--     hace aquí». UNA sola tabla para las dos cosas: el CHECK impide una persona
--     que no esté en ningún sitio o que esté en los dos, y los dos índices
--     únicos impiden la misma persona dos veces en el mismo sitio (mientras no
--     esté borrada). `rol` no vacío y de a lo sumo 80 caracteres.
--
-- Todo es ADITIVO: columnas que aceptan NULL, una tabla nueva, y un CHECK que
-- las filas de hoy cumplen (las dos columnas del cliente nacen NULL). No se
-- borra ni se renombra nada que exista. Con la app viva no rompe ningún
-- `SELECT *` (las consultas preparadas están apagadas: `db/sin_preparadas.py`).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla
-- del repo para todo DDL en producción) y su guion `m2_proyectos.py`.
-- Idempotente: correrlo dos veces no hace nada la segunda.

BEGIN;

ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS responsable_chat_id BIGINT;
ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS cliente_noco_id BIGINT;
ALTER TABLE proyectos ADD COLUMN IF NOT EXISTS cliente_nombre TEXT;

COMMENT ON COLUMN proyectos.responsable_chat_id IS
  'Quién lleva el proyecto: el chat de Telegram de Rosi, de Tiziano o de Code. '
  'NULL = sin responsable. La puerta es crud.PUERTAS (config.puede_ser_responsable).';
COMMENT ON COLUMN proyectos.cliente_noco_id IS
  'El Id de la ficha del cliente en el Noco de CDS. NULL = sin cliente (es '
  'opcional). Lo escribe SOLO db.poner_cliente, que vuelve a leer la ficha de Noco.';
COMMENT ON COLUMN proyectos.cliente_nombre IS
  'El nombre que tenía la ficha al elegirla. Puede quedar desactualizado si lo '
  'cambian en Noco: está aceptado. Siempre junto con cliente_noco_id.';

ALTER TABLE proyectos DROP CONSTRAINT IF EXISTS proyectos_cliente_entero;
ALTER TABLE proyectos ADD CONSTRAINT proyectos_cliente_entero
  CHECK ((cliente_noco_id IS NULL) = (cliente_nombre IS NULL));

ALTER TABLE comentarios_tarea ADD COLUMN IF NOT EXISTS editado_en TIMESTAMPTZ;

COMMENT ON COLUMN comentarios_tarea.editado_en IS
  'NULL = el comentario nunca se editó. Lo pone db.editar_comentario.';

CREATE TABLE IF NOT EXISTS participantes (
  id                  BIGSERIAL PRIMARY KEY,
  proyecto_id         BIGINT REFERENCES proyectos(id),
  tarea_id            BIGINT REFERENCES tareas(id),
  noco_id             BIGINT NOT NULL,
  nombre              TEXT NOT NULL,
  rol                 TEXT NOT NULL,
  creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
  creado_por_chat_id  BIGINT NOT NULL,
  borrado_en          TIMESTAMPTZ,
  borrado_por_chat_id BIGINT,
  CONSTRAINT participantes_en_un_solo_sitio
    CHECK ((proyecto_id IS NULL) <> (tarea_id IS NULL)),
  CONSTRAINT participantes_rol_valido
    CHECK (length(trim(rol)) BETWEEN 1 AND 80)
);

COMMENT ON TABLE participantes IS
  'Las personas de un proyecto o de una tarea, con su rol (qué hace aquí). Una '
  'persona por sitio; el borrado es blando (borrado_en).';

CREATE UNIQUE INDEX IF NOT EXISTS participantes_una_vez_por_proyecto
  ON participantes (proyecto_id, noco_id)
  WHERE borrado_en IS NULL AND proyecto_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS participantes_una_vez_por_tarea
  ON participantes (tarea_id, noco_id)
  WHERE borrado_en IS NULL AND tarea_id IS NOT NULL;

COMMIT;
