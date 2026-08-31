-- El estado del movimiento: aprobada | declinada | pendiente.
--
-- Los parsers lo calculan desde el primer día —lo usan en clave_dedupe para
-- separar el "aprobado" del "declinado" que el banco manda en el mismo minuto—
-- pero NUNCA se guardaba. Consecuencia: un intento rechazado en un cajero
-- entraba idéntico a una compra real y sumaba a los totales.
--
-- Medido sobre los 466 movimientos del corpus (unos cinco meses):
--     declinada    23 movimientos · DOP 21,386.56  ← dinero que nunca salió
--     pendiente    12 movimientos · DOP  3,693.07  ← retenciones que además se
--                                                    cuentan otra vez cuando
--                                                    llega el cargo de verdad
-- Cerca de RD$60,000 al año de gasto que no existió.
--
-- Default 'aprobada' porque es lo que eran las filas que ya estaban: si el
-- banco hubiera dicho otra cosa, el parser lo habría puesto en la referencia.
BEGIN;

ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'aprobada';

COMMENT ON COLUMN movimientos.estado IS
  'aprobada | declinada | pendiente. Solo las aprobadas cuentan en los totales: '
  'una declinada es dinero que no salió, y una pendiente (retención) se cuenta '
  'cuando llega el cargo real, no antes.';

-- Las que ya están y el parser marcó como rechazadas dejaron rastro en la
-- referencia. Se corrigen con eso, que es el único dato disponible: la columna
-- no existía cuando entraron.
UPDATE movimientos SET estado = 'declinada'
 WHERE referencia ILIKE '%rechazo:%' OR referencia ILIKE '%declinad%';

COMMIT;
