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

-- Lo que Lucy aprende de CÓMO Tiziano quiere que trabaje (req 35). Cada fila es
-- una regla en lenguaje natural; TODAS las activas se inyectan en el prompt del
-- agente en cada mensaje, así las aplica sin que él las repita. borrado_en =
-- "olvidá esa regla" (reversible con el deshacer genérico, como todo lo demás).
CREATE TABLE preferencias (
  id         BIGSERIAL PRIMARY KEY,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
  texto      TEXT NOT NULL,                    -- "no me recuerdes trabajo los domingos"
  contexto   TEXT,                             -- opcional: 'agenda'|'recordatorios'|'personas'…
  borrado_en TIMESTAMPTZ
);

-- Puntero de lectura por cuenta de correo (Nivel 4). Guarda hasta qué UID se
-- miró, para que 'vigilar' sea mirar lo nuevo y no releer decenas de miles.
-- uidvalidity: si Gmail lo cambia, el puntero se resetea (los UID ya no son
-- los mismos) y se vuelve a fijar la línea de corte sin procesar el backlog.
CREATE TABLE correo_estado (
  cuenta         TEXT PRIMARY KEY,
  uidvalidity    BIGINT,
  ultimo_uid     BIGINT NOT NULL DEFAULT 0,
  actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Fecha del último reporte matinal ya emitido. Vive en la base y no en
  -- memoria a propósito: un redespliegue a media mañana no puede hacer que el
  -- reporte del día salga dos veces.
  ultimo_reporte DATE
);

-- Memoria de qué correos ya se le informaron a Tiziano, con la clasificación
-- que se les dio. Es lo que permite mirar los SIN LEER en vez de consumir un
-- puntero: sin esta tabla, un correo que él no marque leído reaparecería cada
-- mañana hasta el fin de los tiempos. Informado una vez, informado.
--
-- Guardar el nivel/ámbito/área no es adorno: es lo que después deja contestar
-- "¿por qué no me avisaste de esto?" con datos en la mano.
--
-- leido_en se llena recién cuando el reporte LLEGÓ de verdad (ver
-- captura/correo.py::confirmar_leidos): marcar leído antes sería escribirle
-- una mentira en su propio buzón.
CREATE TABLE correo_reportado (
  cuenta       TEXT NOT NULL,
  uid          BIGINT NOT NULL,
  reportado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  nivel        TEXT,                          -- 911 | accion | enterarte | mencion
  ambito       TEXT,                          -- laboral | personal
  area         TEXT,                          -- infraestructura | cds_clientes | ...
  asunto       TEXT,
  bandeja_id   BIGINT REFERENCES bandeja(id), -- el encargo del reporte que lo mencionó
  leido_en     TIMESTAMPTZ,                   -- NULL = informado pero aún sin marcar en Gmail
  -- (cuenta, uid) es la clave del ON CONFLICT de db.marcar_correo_reportado.
  PRIMARY KEY (cuenta, uid)
);

-- ═══ Entidades (bandeja_id = trazabilidad, borrado_en = reversibilidad) ═══
CREATE TABLE tareas (
  id              BIGSERIAL PRIMARY KEY,
  bandeja_id      BIGINT REFERENCES bandeja(id),
  creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
  titulo          TEXT NOT NULL,
  detalle         TEXT,
  vence_en        TIMESTAMPTZ,
  recurrencia     TEXT,                            -- NULL = una vez. 'cada 8 horas' |
                                                   --   'diaria' | 'cada 3 días' | 'semanal' |
                                                   --   'cada 2 semanas' | 'mensual' | 'cada lunes'…
                                                   --   El despertador reprograma la MISMA fila al
                                                   --   marcarse hecha: 1 fila por tarea recurrente,
                                                   --   no 1 por ocurrencia.
  prioridad       TEXT,                            -- baja | media | alta
  proyecto_id     BIGINT REFERENCES proyectos(id),
  persona_id      BIGINT REFERENCES personas(id),  -- "preguntarle a Pedro por el presupuesto"
  estado          TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | hecha | pospuesta
  pospuesta_veces INT NOT NULL DEFAULT 0,          -- alimenta "bolas que se caen" (req 28)
  completado_en   TIMESTAMPTZ,
  avisos_enviados INT[] NOT NULL DEFAULT '{}',      -- minutos-antes ya avisados: {30,0} = avisó a -30 y a la hora
  anticipos_min   INT[] NOT NULL DEFAULT '{0}',     -- minutos-antes a avisar (por fila): {0}=solo a la hora; {30,0}=30' antes y a la hora
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
  avisos_enviados INT[] NOT NULL DEFAULT '{}',   -- minutos-antes ya avisados: {30,0} = avisó a -30 y a la hora
  anticipos_min INT[] NOT NULL DEFAULT '{0}',    -- minutos-antes a avisar (por fila): {0}=solo a la hora; {30,0}=30' antes y a la hora
  preaviso_en  TIMESTAMPTZ,                      -- HUÉRFANA desde el 13-ago-2026: era la marca del
                                                 --   encargo de salida, que se eliminó entero. Ya
                                                 --   no se lee ni se escribe. Queda para DROP junto
                                                 --   con avisado_en; 18 filas la tienen puesta, y
                                                 --   es estado de maquinaria, no dato de Tiziano.
  -- Nivel 4: espejo de Google Calendar. NULL = cita nativa de Lucy (Telegram).
  gcal_id       TEXT,                            -- id del evento en Google
  gcal_cal_id   TEXT,                            -- id del calendario (clave + push)
  gcal_calendar TEXT,                            -- nombre legible ('CDS Sala P'…)
  borrado_en   TIMESTAMPTZ
);
-- Upsert del sync: un evento de Google es único por (calendario, id).
CREATE UNIQUE INDEX idx_eventos_gcal ON eventos (gcal_cal_id, gcal_id)
  WHERE gcal_id IS NOT NULL;

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
  -- Las dos siguientes llegaron con la ingesta bancaria y vivieron un tiempo
  -- solo en la migración: la base real las tenía y este archivo no.
  hash_contenido TEXT,                         -- clave de dedupe del correo
  -- aprobada | declinada | pendiente.
  --
  -- NO cuentan las DECLINADAS: ese dinero no salió.
  --
  -- Las PENDIENTES sí cuentan, y esto se midió antes de decidirlo. Parecía que
  -- una retención debía esperar al cargo real para no contarse dos veces —
  -- pero sobre los 461 movimientos del corpus, 10 de 12 retenciones NUNCA se
  -- liquidan en un segundo correo. Para las tarjetas en dólares el aviso de
  -- retención es el ÚNICO registro que manda el banco: Railway, Amazon Prime y
  -- Anthropic llegan así todos los meses. Excluirlas borraba gastos reales.
  --
  -- Si alguna se duplicara, se ve: sale en "posibles duplicados" de /salud.
  -- Contar de más se nota; contar de menos, no.
  estado      TEXT NOT NULL DEFAULT 'aprobada'
              CHECK (estado IN ('aprobada', 'declinada', 'pendiente')),
  banco       TEXT,                            -- BHD | Banreservas | ... | NULL a mano
  borrado_en  TIMESTAMPTZ
);
CREATE INDEX idx_movimientos_fecha ON movimientos(fecha);

-- Los lugares con nombre de su vida ("CDS", "el estudio", "casa"): son lo que
-- convierte unas coordenadas en un dato con significado.
CREATE TABLE lugares (
  id         BIGSERIAL PRIMARY KEY,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
  nombre     TEXT NOT NULL,
  lat        DOUBLE PRECISION NOT NULL,
  lon        DOUBLE PRECISION NOT NULL,
  radio_m    INT NOT NULL DEFAULT 300,      -- dentro de este radio "está ahí"
  borrado_en TIMESTAMPTZ
);

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

-- ═══ El latido del respaldo (pilar #40) ═══
-- Una fila por backup que TERMINÓ bien. No es contabilidad: es la única forma
-- de que Lucy sepa si todavía tiene respaldo. Hasta el 30-ago-2026 esa verdad
-- vivía solo en los nombres de archivo de una carpeta de Google Drive que el
-- proceso de Railway no puede ver — así que nadie adentro del sistema podía
-- notar que hacía 25 días que no se respaldaba nada.
--
-- Se escribe DESPUÉS de cerrar el archivo, nunca antes: la fila significa "el
-- archivo existe y está completo". Un backup que revienta a la mitad no deja
-- fila, y a las 48 horas el despertador avisa. Un respaldo que falla callado
-- es peor que no tener respaldo: da la confianza sin dar la copia.
CREATE TABLE backups (
  id       BIGSERIAL PRIMARY KEY,
  hecho_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  archivo  TEXT NOT NULL,        -- nombre del .json.gz (sin ruta: la ruta cambia por máquina)
  bytes    BIGINT NOT NULL,      -- tamaño comprimido; si se desploma, algo se rompió
  tablas   INT NOT NULL,
  filas    INT NOT NULL,
  esquema  TEXT,                 -- 'pg_dump' | 'catalogo' — cómo se guardó la estructura
  origen   TEXT                  -- qué máquina lo corrió: delata que dependemos de una sola
);
CREATE INDEX idx_backups_hecho_en ON backups(hecho_en DESC);

-- ── Ingesta bancaria (migración 2026-08-30) ─────────────────────────
-- Vivieron un tiempo solo en la migración: la base real las tenía y este
-- archivo no, así que `psql -f db/schema.sql` daba una base que no podía
-- ingerir un solo correo. Un esquema versionado que no describe la base
-- real es peor que no tener ninguno, porque se le cree.
CREATE TABLE IF NOT EXISTS consumos_estado (
  cuenta         TEXT PRIMARY KEY,
  uidvalidity    BIGINT,
  ultimo_uid     BIGINT NOT NULL DEFAULT 0,
  desde_fecha    DATE NOT NULL DEFAULT DATE '2026-09-01',
  actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE consumos_estado IS
  'Cursor de la ingesta de movimientos. Separado de correo_estado a propósito: '
  'el reporte matinal y la ingesta recorren el buzón con criterios distintos y '
  'compartir puntero deja ciego al que avanza más lento.';
COMMENT ON COLUMN consumos_estado.uidvalidity IS
  'Si el servidor lo cambia, los UID viejos dejan de significar nada y hay que '
  'reiniciar el cursor. Sin esta comprobación la ingesta se saltaría correos en '
  'silencio tras una migración del buzón.';

CREATE TABLE IF NOT EXISTS categorias_aprendidas (
  comercio    TEXT PRIMARY KEY,
  categoria   TEXT NOT NULL,
  creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
  borrado_en  TIMESTAMPTZ
);
COMMENT ON COLUMN categorias_aprendidas.comercio IS
  'Comercio NORMALIZADO (cerebro/bancos/categorias.py): sin acentos, sin '
  'sucursal, sin sufijo de ciudad. Guardar el nombre crudo obligaría a corregir '
  'el mismo sitio una vez por cada forma en que el banco lo escribe.';

-- Registro de cuentas propias de la casa. Estaba en la base real y no acá: es
-- la tabla que separa un traspaso entre cuentas de un gasto de verdad, o sea
-- la que evita volver a contar RD$657,400 al año que no se gastaron.
CREATE TABLE IF NOT EXISTS cuentas_propias (
  id         BIGSERIAL PRIMARY KEY,
  creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
  patron     TEXT NOT NULL,        -- nombre o dígitos de cuenta, normalizado
  clase      TEXT NOT NULL,        -- 'nombre' | 'cuenta'
  banco      TEXT,
  notas      TEXT,
  borrado_en TIMESTAMPTZ
);
