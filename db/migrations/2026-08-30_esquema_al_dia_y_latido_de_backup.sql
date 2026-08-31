-- El esquema del repo se pone al día con producción, y el backup gana latido
-- (30-ago-2026).
--
-- ── Parte 1: la deriva ──────────────────────────────────────────────────────
-- `correo_reportado` y `correo_estado.ultimo_reporte` nacieron en producción
-- con el Nivel 4 (vigilancia de correo) y nunca bajaron a db/schema.sql. Al
-- 30-ago-2026 producción tenía 329 filas en una tabla que el repo no sabía que
-- existía. La consecuencia no era teórica: el README manda instalar con
--     psql "$DATABASE_URL" -f db/schema.sql
-- y esa base arrancaba ROTA — el reporte matinal revienta en la primera
-- consulta a correo_reportado, y `ultimo_reporte` faltando hace que el reporte
-- del día salga de nuevo en cada redespliegue.
--
-- Un esquema versionado que no describe la base real es peor que no tenerlo:
-- se le cree.
--
-- Este bloque es un NO-OP en producción (ya las tiene) y el rescate para
-- cualquier base creada con el schema.sql viejo. Todo con IF NOT EXISTS: se
-- puede correr dos veces sin consecuencias.

CREATE TABLE IF NOT EXISTS correo_reportado (
  cuenta       TEXT NOT NULL,
  uid          BIGINT NOT NULL,
  reportado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  nivel        TEXT,
  ambito       TEXT,
  area         TEXT,
  asunto       TEXT,
  bandeja_id   BIGINT REFERENCES bandeja(id),
  leido_en     TIMESTAMPTZ,
  PRIMARY KEY (cuenta, uid)
);

ALTER TABLE correo_estado ADD COLUMN IF NOT EXISTS ultimo_reporte DATE;

-- ── Parte 2: el latido ──────────────────────────────────────────────────────
-- Los backups corrían a las 20:00 desde la PC de Tiziano (la ruta destino era
-- `G:\My Drive\Lucy\backups`, una letra de unidad de Windows). El 29-jul esa
-- PC dejó de correrlos; hubo dos manuales el 3 y el 5 de agosto, y después
-- nada. Nadie se enteró durante 25 días porque la única evidencia de que un
-- backup había ocurrido era un archivo en una carpeta que Railway no ve.
--
-- Esta tabla mueve esa evidencia ADENTRO de la base. `backup.py` escribe una
-- fila al terminar bien; el despertador la mira y avisa por Telegram si pasan
-- más de 48 horas sin ninguna. Ahora el silencio tiene quien lo denuncie.
--
-- ⚠️ Correr ANTES de desplegar el código nuevo: despertador.revisar_backup()
-- consulta esta tabla en cada vuelta. (Si no existiera, no tumba a Lucy —el
-- bucle atrapa la excepción— pero la alerta quedaría muda, que es justo lo
-- que se vino a arreglar.)

CREATE TABLE IF NOT EXISTS backups (
  id       BIGSERIAL PRIMARY KEY,
  hecho_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  archivo  TEXT NOT NULL,
  bytes    BIGINT NOT NULL,
  tablas   INT NOT NULL,
  filas    INT NOT NULL,
  esquema  TEXT,
  origen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_backups_hecho_en ON backups(hecho_en DESC);

-- La tabla nace VACÍA a propósito. Sembrarla con el backup del 5 de agosto
-- sería escribir que hay respaldo reciente cuando no lo hay: la primera vuelta
-- del despertador tiene que gritar, porque hoy de verdad no hay respaldo.
-- Sin ninguna fila, `ultimo_backup()` devuelve NULL y eso se lee como "nunca"
-- — que es la lectura honesta.
