-- El correo y el RNC o la cédula de la formal (24-oct-2026). Tiziano, textual: «Debe llevar Nombre, correo, teléfono y RNC o
-- cédula si es con comprobante». El ejemplo es `IA CDS/disenos/lucy-cotizacion-con-detalle/ejemplo-tiziano.pdf`.
--
--   · `cotizaciones.para_correo` (TEXT, opcional): el correo de a quien va la formal. Mismo largo de siempre para un texto de
--     cabecera: hasta 254 caracteres (el máximo de un correo). Sin chequeo de formato (solo el largo, como el teléfono).
--   · `cotizaciones.para_rnc_cedula` (TEXT, opcional): el RNC (9 cifras) o la cédula (11), solo cuando la formal es con
--     comprobante. La validación es la de las facturas (`db.rnc_o_cedula_que_vale`); aquí queda el mismo CHECK de largo que
--     `facturas.rnc_cedula` (2026-10-20_facturas.sql).
--
-- Todo es ADITIVO: no se reescribe ninguna fila (las formales que ya existen quedan con los dos en NULL) y no se borra nada.
-- IDEMPOTENTE: `IF NOT EXISTS` en todo lo que agrega.
-- REQUIERE `2026-10-14_cotizaciones.sql` (la tabla `cotizaciones`).
--
-- NO SE APLICA ACÁ. La aplica la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en producción) y
-- `python3 tools/humo.py` con `DATABASE_URL` después. Mientras no esté aplicada, la lectura de cotizaciones de la página
-- de Proyectos (`db.cotizaciones_de_proyectos`) tolera el error 42703 y saca el bloque sin avisar: aplicarla ANTES del deploy.
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado):
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS para_rnc_cedula;
--   ALTER TABLE cotizaciones DROP COLUMN IF EXISTS para_correo;

BEGIN;

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS para_correo TEXT
  CONSTRAINT cotizaciones_correo_valido CHECK (para_correo IS NULL OR length(para_correo) <= 254);

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS para_rnc_cedula TEXT
  CONSTRAINT cotizaciones_rnc_cedula_valido CHECK (para_rnc_cedula IS NULL OR length(trim(para_rnc_cedula)) BETWEEN 1 AND 20);

COMMIT;
