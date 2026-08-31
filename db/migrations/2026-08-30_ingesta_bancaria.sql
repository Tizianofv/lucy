-- Las tres piezas que le faltan a la base para que la ingesta bancaria
-- funcione (30-ago-2026). Es seguro correrlo dos veces: todo va con
-- IF NOT EXISTS.
--
-- 1. movimientos.hash_contenido  → evita guardar dos veces el mismo
--    movimiento. Banco Popular manda la misma transacción dos veces con
--    segundos de diferencia.
-- 2. cuentas_propias             → distingue mover plata entre cuentas
--    tuyas de gastarla de verdad. Sin esto un tercio del gasto en pesos
--    está mal contado.
-- 3. consumos_estado             → por dónde va leyendo el correo.

-- ═══════════════════════════════════════════════════════════
BEGIN;
ALTER TABLE movimientos
  ADD COLUMN IF NOT EXISTS hash_contenido TEXT;
COMMENT ON COLUMN movimientos.hash_contenido IS
  'Huella de Movimiento.clave_dedupe(); NULL en los creados a mano';
CREATE UNIQUE INDEX IF NOT EXISTS idx_movimientos_hash
  ON movimientos (hash_contenido)
  WHERE hash_contenido IS NOT NULL;
COMMIT;

-- ═══════════════════════════════════════════════════════════
BEGIN;
CREATE TABLE IF NOT EXISTS cuentas_propias (
  id          BIGSERIAL PRIMARY KEY,
  creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
  patron      TEXT NOT NULL,          -- trozo distintivo: "ROSILIS", "8354"
  clase       TEXT NOT NULL,          -- titular | cuenta | tarjeta
  banco       TEXT,                   -- opcional, solo para saber de dónde salió
  notas       TEXT,
  borrado_en  TIMESTAMPTZ,
  CONSTRAINT cuentas_propias_patron_unico UNIQUE (patron)
);
COMMENT ON COLUMN cuentas_propias.patron IS
  'Trozo distintivo del nombre o los últimos dígitos. Mínimo 5 caracteres tras '
  'normalizar: uno más corto casaría con nombres ajenos por accidente.';
COMMIT;

-- ═══════════════════════════════════════════════════════════
BEGIN;
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
COMMIT;


-- ═══════════════════════════════════════════════════════════
-- 4. categorias_aprendidas → lo que el sistema aprende cuando corriges una
--    categoría en el panel. Sin esto, corregir el mismo comercio la semana que
--    viene volvería a ser trabajo manual.
BEGIN;
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
COMMIT;
