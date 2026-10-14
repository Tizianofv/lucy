-- Las cotizaciones de un proyecto (pieza 1 del diseño «Cotizaciones en Proyectos», 14-oct-2026;
-- Tiziano, textual, a las tres preguntas del diseño: «1) Comienza hoy con el codigo 2) SI 3) Si»).
--
--   · `cotizaciones`: UNA fila por cotización de un proyecto.
--       - `proyecto_id` (BIGINT): el proyecto de Lucy. Sin `ON DELETE`: los proyectos no se borran de verdad.
--       - `anio`, `numero`: el código que se ve es `COT-<anio>-<numero con 3 cifras>`; se calcula, no se
--         guarda como texto. `UNIQUE (anio, numero)`: la última puerta contra un número repetido.
--       - `estado`: `borrador` o `enviada` (el vocabulario entero lo fija el CHECK). Una enviada SE PUEDE
--         corregir y volver a bajar con el mismo número (respuesta 2 de Tiziano): el esquema no lo impide;
--         el estado queda para saber cuál se mandó.
--       - `para_nombre`, `para_telefono`: copia al guardar (lo que se mandó queda como se mandó).
--       - `fecha`, `validez_dias` (7, 15 o 30).
--       - `contenido` (TEXT con JSON: opciones, renglones y condiciones): lo valida UNA puerta de código,
--         `db.contenido_de_cotizacion_que_vale`, antes de cualquier escritura. TEXT y no JSONB para que la
--         misma sentencia corra en SQLite en las pruebas; Lucy nunca busca adentro.
--       - `creado_en`, `creado_por_chat_id`, `cambiado_en` (candado de edición), `enviada_en`, `borrado_en`.
--         `enviada_en` es NULL si y solo si el estado es `borrador`. Ninguna ruta escribe `borrado_en`
--         todavía.
--   · `serie_cotizaciones`: el contador por año (`anio`, `ultimo`). NACE VACÍA: la serie no se siembra
--     desde las cotizaciones hechas a mano (respuesta 1 de Tiziano); la primera que haga Lucy es
--     COT-2026-001.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción) y antes de eso, en ensayo, con `python3 tools/ensayo_cotizaciones.py --ensayo` (arma el SQL para `psql`; las
-- órdenes completas están en la cabecera de ese archivo). Las dos tablas
-- son NUEVAS: no se reescribe ninguna fila ni se toca ninguna tabla que exista.
--
-- EL PROCESO VIEJO (el código que no conoce estas tablas) SIGUE ANDANDO con la base cambiada: no las nombra
-- en ningún SQL y sus consultas preparadas están apagadas para todo el proceso (`db/sin_preparadas.py`),
-- así que una tabla de más no lo rompe.
--
-- IDEMPOTENTE: `CREATE TABLE IF NOT EXISTS` no hace nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de los `CREATE`,
-- `SELECT count(*) FROM cotizaciones` = 0 y `SELECT count(*) FROM serie_cotizaciones` = 0 (nacen vacías),
-- `information_schema.tables` con DOS filas (`cotizaciones` y `serie_cotizaciones`), y en `pg_constraint`
-- las restricciones con nombre de las dos tablas (las que dice este mismo archivo: siete en
-- `cotizaciones` y una en `serie_cotizaciones`; el guion las saca del texto). Al terminar, con el
-- ROLLBACK, `to_regclass` de las dos da NULL (no quedó nada).
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado; con él vivo se pierden las
-- cotizaciones guardadas):
--   DROP TABLE IF EXISTS cotizaciones;
--   DROP TABLE IF EXISTS serie_cotizaciones;

BEGIN;

CREATE TABLE IF NOT EXISTS cotizaciones (
  id                 BIGSERIAL PRIMARY KEY,
  proyecto_id        BIGINT NOT NULL REFERENCES proyectos(id),
  anio               SMALLINT NOT NULL,
  numero             INT NOT NULL,
  estado             TEXT NOT NULL DEFAULT 'borrador',
  para_nombre        TEXT NOT NULL,
  para_telefono      TEXT,
  fecha              DATE NOT NULL,
  validez_dias       SMALLINT NOT NULL,
  contenido          TEXT NOT NULL,
  creado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
  creado_por_chat_id BIGINT,
  cambiado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),
  enviada_en         TIMESTAMPTZ,
  borrado_en         TIMESTAMPTZ,
  CONSTRAINT cotizaciones_numero_unico
    UNIQUE (anio, numero),
  CONSTRAINT cotizaciones_numero_positivo
    CHECK (numero >= 1),
  CONSTRAINT cotizaciones_estado_valido
    CHECK (estado IN ('borrador', 'enviada')),
  CONSTRAINT cotizaciones_nombre_valido
    CHECK (length(trim(para_nombre)) BETWEEN 1 AND 200),
  CONSTRAINT cotizaciones_telefono_valido
    CHECK (para_telefono IS NULL OR length(para_telefono) <= 40),
  CONSTRAINT cotizaciones_validez_valida
    CHECK (validez_dias IN (7, 15, 30)),
  CONSTRAINT cotizaciones_enviada_con_fecha
    CHECK ((estado = 'enviada') = (enviada_en IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS serie_cotizaciones (
  anio   SMALLINT PRIMARY KEY,
  ultimo INT NOT NULL,
  CONSTRAINT serie_cotizaciones_ultimo_valido
    CHECK (ultimo >= 0)
);

COMMENT ON TABLE cotizaciones IS
  'Las cotizaciones de un proyecto. El código que se ve es COT-<anio>-<numero con 3 cifras>. '
  'contenido es JSON en texto (opciones, renglones, condiciones) validado por '
  'db.contenido_de_cotizacion_que_vale. Una enviada se puede corregir con el mismo número.';

COMMENT ON TABLE serie_cotizaciones IS
  'El contador de números de cotización, una fila por año; ultimo es el último número dado. '
  'Nace vacía: la serie empieza con el sistema.';

COMMIT;
