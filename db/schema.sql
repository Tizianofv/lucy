-- ═══════════════════════════════════════════════════════════════════════
-- Lucy · esquema v2 · TZ de referencia: America/Santo_Domingo (UTC-4, sin DST)
-- Correr una sola vez sobre la base de Postgres recién creada.
-- ═══════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector: si esto falla, la imagen de
                                        -- Postgres no sirve para Nivel 3. Avisar a Claude.

-- ═══ Columna vertebral: todo cae aquí crudo, ANTES de que la IA lo toque ═══
CREATE TABLE bandeja (
  id               BIGSERIAL PRIMARY KEY,
  creado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),
  origen           TEXT NOT NULL DEFAULT 'telegram',  -- telegram | email(futuro) | ...
  tipo_entrada     TEXT NOT NULL,                     -- texto | audio | foto
  contenido_raw    TEXT,                              -- texto o caption
  archivo_id       TEXT,                              -- file_id de Telegram
  chat_id          BIGINT,                            -- para responder/editar el msg exacto
  telegram_msg_id  BIGINT,                            --   y base de "muévelo a las 6"
  hash_contenido   TEXT,                              -- dedup futuro (req 20)
  transcripcion    TEXT,                              -- audio → texto, foto → texto leído
  respuesta_lucy   TEXT,                              -- lo que Lucy contestó: la mitad
                                                      --   suya de la conversación (req 11)
  embedding        vector(1536),                      -- memoria de largo plazo (req 13):
                                                      --   se indexa dicho+respuesta al
                                                      --   cerrarse cada intercambio
  estado           TEXT NOT NULL DEFAULT 'sin_procesar',
    -- sin_procesar | procesando | esperando_confirmacion | esperando_respuesta
    -- | procesado | descartado | error
    -- esperando_respuesta = Lucy preguntó algo por Telegram y la conversación
    -- sigue cuando Tiziano conteste (la ventana del agente)
  clasificacion    TEXT,          -- tarea|cita|nota|idea|gasto|pregunta
  interpretacion   JSONB,         -- extracción estructurada completa
  procesado_en     TIMESTAMPTZ,
  error_detalle    TEXT,

  -- Cola de reintentos. Un fallo pasajero (cuota de la IA, un timeout) no
  -- puede condenar un mensaje: vuelve a 'sin_procesar' con una espera que se
  -- va duplicando. Solo tras agotar los intentos pasa a 'error' de verdad.
  intentos           INT NOT NULL DEFAULT 0,
  reintentar_despues TIMESTAMPTZ,

  -- Idempotencia: Telegram reentrega el mismo mensaje si no le confirmamos a
  -- tiempo (deploy, timeout, base lenta). Sin esto una reentrega duplica la
  -- fila. Misma lección que el dedupe de wamid en Natalia.
  -- Ojo: en Postgres los NULL no chocan entre sí, así que las filas de otros
  -- orígenes (email, etc.) sin telegram_msg_id conviven sin problema.
  CONSTRAINT bandeja_msg_unico UNIQUE (chat_id, telegram_msg_id)
);
CREATE INDEX idx_bandeja_estado ON bandeja(estado);
CREATE INDEX idx_bandeja_embedding ON bandeja USING hnsw (embedding vector_cosine_ops);

-- ═══ Vínculos reales desde el día 1 (req 16) ═══
CREATE TABLE personas (
  id         BIGSERIAL PRIMARY KEY,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
  nombre     TEXT NOT NULL,
  alias      TEXT[] DEFAULT '{}',   -- "Ana", "ana la del gym" → misma persona
  relacion   TEXT,                  -- cliente | familia | amigo | proveedor...
  notas      TEXT,                  -- semilla del "perfil vivo" (req 12)
  borrado_en TIMESTAMPTZ
);

CREATE TABLE proyectos (
  id          BIGSERIAL PRIMARY KEY,
  creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
  nombre      TEXT NOT NULL,
  descripcion TEXT,
  estado      TEXT NOT NULL DEFAULT 'activo',  -- activo | pausado | cerrado
  borrado_en  TIMESTAMPTZ
);

-- ═══ Entidades (bandeja_id = trazabilidad, borrado_en = reversibilidad) ═══
CREATE TABLE tareas (
  id              BIGSERIAL PRIMARY KEY,
  bandeja_id      BIGINT REFERENCES bandeja(id),
  creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
  titulo          TEXT NOT NULL,
  detalle         TEXT,
  vence_en        TIMESTAMPTZ,
  prioridad       TEXT,                            -- baja | media | alta
  proyecto_id     BIGINT REFERENCES proyectos(id),
  persona_id      BIGINT REFERENCES personas(id),  -- "preguntarle a Pedro por el presupuesto"
  estado          TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | hecha | pospuesta
  pospuesta_veces INT NOT NULL DEFAULT 0,          -- alimenta "bolas que se caen" (req 28)
  completado_en   TIMESTAMPTZ,
  avisado_en      TIMESTAMPTZ,                     -- el despertador ya avisó (1 sola vez)
  borrado_en      TIMESTAMPTZ
);

CREATE TABLE eventos (
  id           BIGSERIAL PRIMARY KEY,
  bandeja_id   BIGINT REFERENCES bandeja(id),
  creado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
  titulo       TEXT NOT NULL,
  inicia_en    TIMESTAMPTZ NOT NULL,
  termina_en   TIMESTAMPTZ,
  lugar        TEXT,
  persona_id   BIGINT REFERENCES personas(id),   -- "¿cuándo vi a Ana por última vez?" = 1 query
  proyecto_id  BIGINT REFERENCES proyectos(id),
  notas        TEXT,
  avisado_en   TIMESTAMPTZ,                      -- el despertador ya avisó (1 sola vez)
  borrado_en   TIMESTAMPTZ
);

CREATE TABLE notas (
  id          BIGSERIAL PRIMARY KEY,
  bandeja_id  BIGINT REFERENCES bandeja(id),
  creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
  contenido   TEXT NOT NULL,
  etiquetas   TEXT[] DEFAULT '{}',
  proyecto_id BIGINT REFERENCES proyectos(id),
  persona_id  BIGINT REFERENCES personas(id),
  borrado_en  TIMESTAMPTZ
);

-- Todo lo que mueve plata, salga o entre. Una tabla y no dos porque "¿cuánto
-- gasté?" y "¿cuánto entró?" son la misma consulta con otro filtro, y el
-- balance es restarlas. Separarlas obligaría a unir dos tablas cada vez que
-- Tiziano pregunte algo sobre su plata.
CREATE TABLE movimientos (
  id          BIGSERIAL PRIMARY KEY,
  bandeja_id  BIGINT REFERENCES bandeja(id),   -- la foto del ticket, vinculada
  creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
  tipo        TEXT NOT NULL DEFAULT 'gasto',   -- gasto | ingreso | transferencia
  fecha       DATE NOT NULL,
  monto       NUMERIC(12,2) NOT NULL,          -- SIEMPRE positivo: el signo lo da `tipo`
  moneda      TEXT NOT NULL DEFAULT 'DOP',
  contraparte TEXT,                            -- el comercio si sale, quién pagó si entra
  categoria   TEXT,
  referencia  TEXT,                            -- No. de confirmación / comprobante
  persona_id  BIGINT REFERENCES personas(id),  -- "¿cuánto le pagué a Juan?"
  proyecto_id BIGINT REFERENCES proyectos(id), -- "¿cuánto llevo gastado en X?"
  notas       TEXT,
  borrado_en  TIMESTAMPTZ
);
CREATE INDEX idx_movimientos_fecha ON movimientos(fecha);

-- ═══ Todo lo que Lucy hace, queda escrito (pilares + Nivel 7 desde el día 1) ═══
CREATE TABLE log_acciones (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor       TEXT NOT NULL,        -- 'lucy' | 'tiziano'
  accion      TEXT NOT NULL,        -- crear | editar | borrar | restaurar | clasificar
  tabla       TEXT NOT NULL,
  registro_id BIGINT NOT NULL,
  antes       JSONB,                -- estado previo → esto ES el "deshacer"
  despues     JSONB,
  motivo      TEXT,                 -- la explicación de Lucy (req 36, gratis desde hoy)
  bandeja_id  BIGINT REFERENCES bandeja(id)
);
