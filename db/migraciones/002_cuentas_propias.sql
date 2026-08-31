-- 002 · Registro de titulares y cuentas de la casa
--
-- POR QUÉ: un banco no sabe qué cuentas son tuyas. Dice "transferencia a ROSILIS
-- YANELY ROMERO JIMENEZ" igual si le pagaste a un proveedor que si moviste plata
-- entre dos cuentas tuyas. Sin este registro lo segundo se anota como gasto, y la
-- entrada correspondiente en el otro banco como ingreso: la misma plata contada
-- dos veces con el signo cambiado.
--
-- Medido sobre los 461 movimientos de los fixtures (30-ago-2026): 31 movimientos
-- cambian de tipo con el registro puesto, incluidos 2 pagos de clientes al
-- estudio que hoy figuran como gasto en vez de ingreso.
--
-- QUÉ SE GUARDA: patrones distintivos, no nombres completos. Los bancos escriben
-- al mismo titular de cinco maneras — "ROSILIS YANELY ROMERO JIMENEZ",
-- "ROSILISYANELY ROMERO JIMENEZ" (sin espacio), "SRA ROSILIS Y ROMERO",
-- "Rosilis Romero". El matcher normaliza (sin acentos, sin espacios, mayúsculas)
-- y busca el patrón como subcadena.
--
-- APLICAR CUANDO: los backups vuelvan a correr. Al 30-ago-2026 el último es del
-- 5-ago. Esto es DDL sobre producción y no hay red debajo.
--   psql "$DATABASE_URL" -f db/migraciones/002_cuentas_propias.sql

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

-- Deliberadamente SIN semilla. Los patrones son nombres de personas reales y
-- números de cuenta: los carga Tiziano, no un archivo del repo.

COMMIT;
