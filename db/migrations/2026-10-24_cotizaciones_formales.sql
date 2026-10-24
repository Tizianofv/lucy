-- La base de las formales que hace Natalia (pieza 3 del diseño «Cotización formal que hace Natalia», 24-oct-2026;
-- `IA CDS/disenos/lucy-cotizacion-formal-natalia/DISENO.md`, sección «Qué escribe cada pieza en la base»).
--
--   · `cotizaciones.hecha_por` (TEXT, NOT NULL, por omisión 'equipo'): quién hizo la formal. 'equipo' = la hizo el equipo
--     desde el panel (las que ya existen quedan así por el DEFAULT); 'natalia' = la hizo Natalia desde Lucy. Dos CHECK con
--     nombre: `cotizaciones_hecha_por_valida` (solo esos dos valores) y `cotizaciones_natalia_sin_chat` (una hecha por
--     Natalia no tiene chat de quien la creó: `creado_por_chat_id` IS NULL).
--   · `cotizacion_envios`: por dónde salió cada formal y la aprobación de Rosi que se pide antes de mandarla. Es el mismo
--     mecanismo que `factura_envios` (`db/migrations/2026-10-20_facturas.sql`), con los mismos CHECK (nombres propios),
--     sin `viene_de_envio_id`, y con las cuatro columnas de la tarjeta de Telegram (pieza 5) que el diseño pone en esta
--     misma tabla. Sin índice único: igual que `factura_envios`, un envío vivo por formal lo cuida el código.
--
-- Todo es ADITIVO: no se reescribe ninguna fila (las que hay quedan 'equipo') y no se borra nada.
-- IDEMPOTENTE: `IF NOT EXISTS` en todo lo que crea o agrega.
-- REQUIERE `2026-10-14_cotizaciones.sql` (la tabla `cotizaciones`).
--
-- NO SE APLICA ACÁ. La aplica la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en producción) y
-- `python3 tools/humo.py` con `DATABASE_URL` después.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado):
--   DROP TABLE IF EXISTS cotizacion_envios;
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS hecha_por;

BEGIN;

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS hecha_por TEXT NOT NULL DEFAULT 'equipo'
  CONSTRAINT cotizaciones_hecha_por_valida CHECK (hecha_por IN ('equipo', 'natalia'))
  CONSTRAINT cotizaciones_natalia_sin_chat CHECK (hecha_por = 'equipo' OR creado_por_chat_id IS NULL);

CREATE TABLE IF NOT EXISTS cotizacion_envios (
  id                   BIGSERIAL PRIMARY KEY,
  cotizacion_id        BIGINT NOT NULL REFERENCES cotizaciones(id),
  canal                TEXT NOT NULL,
  estado               TEXT NOT NULL DEFAULT 'pendiente',
  creado_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
  aprobado_por_chat_id BIGINT,
  aprobado_en          TIMESTAMPTZ,
  enviado_en           TIMESTAMPTZ,
  motivo               TEXT,
  tarjeta_msg_id       BIGINT,
  visto_cambiado_en    TIMESTAMPTZ,
  tarjeta_enviada_en   TIMESTAMPTZ,
  recordada_en         TIMESTAMPTZ,
  CONSTRAINT cotizacion_envios_canal_valido
    CHECK (canal IN ('whatsapp', 'correo', 'mano')),
  CONSTRAINT cotizacion_envios_estado_valido
    CHECK (estado IN ('pendiente', 'aprobado', 'rechazado', 'enviado', 'no_se_pudo')),
  CONSTRAINT cotizacion_envios_aprobacion_completa
    CHECK ((aprobado_por_chat_id IS NULL) = (aprobado_en IS NULL)),
  CONSTRAINT cotizacion_envios_no_sale_sin_aprobar
    CHECK (canal = 'mano' OR estado NOT IN ('aprobado', 'enviado') OR aprobado_en IS NOT NULL),
  CONSTRAINT cotizacion_envios_enviado_con_fecha
    CHECK ((estado = 'enviado') = (enviado_en IS NOT NULL)),
  CONSTRAINT cotizacion_envios_motivo_si_falla
    CHECK (estado NOT IN ('rechazado', 'no_se_pudo') OR
           (motivo IS NOT NULL AND length(trim(motivo)) BETWEEN 1 AND 500))
);

CREATE INDEX IF NOT EXISTS idx_cotizacion_envios_cotizacion
  ON cotizacion_envios (cotizacion_id);

COMMENT ON COLUMN cotizaciones.hecha_por IS
  'Quién hizo la formal: equipo (desde el panel) o natalia (desde la puerta de Natalia). Las que ya existían quedan en equipo.';
COMMENT ON TABLE cotizacion_envios IS
  'Por dónde salió una cotización formal y la aprobación de Rosi que se pide antes de mandarla al cliente. '
  'El canal mano no pide aprobación.';

COMMIT;
