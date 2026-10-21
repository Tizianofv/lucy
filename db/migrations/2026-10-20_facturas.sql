-- Las facturas (pieza 1 del diseño «Facturas», aprobado por Tiziano el 20-oct-2026: «Si, aprobado, dale»).
-- Solo las tablas y el contador: guardar, leer, la pantalla, el PDF, Drive y la puerta de Natalia son las
-- piezas 2 a 10.
--
--   · `facturas`: UNA fila por factura.
--       - `anio`, `numero`: el código que se ve es `FAC-<anio>-<numero con 3 cifras>`; se calcula, no se guarda
--         como texto. Los dos van juntos o ninguno (`facturas_anio_y_numero_juntos`) y `UNIQUE (anio, numero)`
--         es la última puerta contra un número repetido. Un borrador PUEDE no tener número todavía y una
--         emitida o anulada SIEMPRE lo tiene (`facturas_emitida_con_numero`): la base no obliga a asignarlo al
--         crear ni al emitir, lo decide la pieza 2 (el contador es `db.siguiente_numero_de_factura`).
--       - `estado`: `borrador`, `emitida` o `anulada` (el vocabulario entero lo fija el CHECK). Una emitida no
--         se corrige: se anula (con motivo) y se hace otra. `emitida_en` es NULL si y solo si es borrador;
--         `anulada_en` y `motivo_anulacion` existen si y solo si está anulada.
--       - `cliente_noco_id`: la ficha del cliente en NocoDB (Natalia busca por ahí). `para_nombre`: a quien va,
--         copiado de la ficha al hacerla.
--       - `proyecto_id`, `cotizacion_id`, `cotizacion_opcion`: ligas OPCIONALES que se pueden poner o quitar
--         antes, durante o después. La opción (desde 1) no existe sin cotización.
--       - `itbis_pct`: NULL si la factura no suma ITBIS, el porcentaje (mayor que 0, hasta 100) si lo suma. El
--         subtotal, el ITBIS y el total NO se guardan: salen de los renglones y de este porcentaje, así no hay
--         dos sitios que puedan decir cifras distintas.
--       - `ncf`, `ncf_tipo`, `rnc_cedula`: opcionales. El NCF lo escribe una persona a mano. `UNIQUE (ncf)`
--         cuenta TODAS las filas, anuladas incluidas: el NCF de una anulada no se vuelve a usar. El NCF solo
--         puede tener letras mayúsculas y números, de 1 a 20 (`facturas_ncf_normalizado`: ni espacios, ni tabuladores,
--         ni saltos de línea, ni guiones) para que la misma cifra no entre dos veces escrita de dos formas. El NCF y su tipo van juntos o ninguno.
--       - `hecha_por`: `casa` (una persona, con su `hecha_por_chat_id`) o `natalia` (sin chat).
--       - NO hay columnas de pago: el pago se anota en la App de registro y la factura lo lee.
--   · `factura_renglones`: los renglones de la factura (descripción, detalle y monto de la línea, mayor que 0).
--   · `factura_ligas`: a qué sesiones o trabajos del Registro (la App) pertenece la factura. Se guarda la
--     referencia como texto (`ref`) y el código que tenía (`codigo`), igual que `sesiones_de_proyecto`; el
--     dato de la sesión no se copia. Desligar es ponerle `desligada_en` (nunca un DELETE). El índice único
--     parcial deja UNA liga viva por factura, tipo y referencia.
--       QUÉ LA BASE NO PUEDE EXIGIR: que toda factura tenga al menos una liga viva (es una regla entre dos
--       tablas y este repo no usa disparadores). La exige la pieza 2 (crear la factura y su primera liga en la
--       misma transacción y no dejar desligar la última) y la vigila `tools/humo.py`, que cuenta las
--       facturas (de cualquier estado, borradores incluidos) sin ninguna liga viva.
--   · `factura_envios`: por dónde salió la factura. Es también el lugar donde queda la APROBACIÓN del equipo
--     (`aprobado_por_chat_id`, `aprobado_en`) que se pide antes de mandar una factura al cliente. `canal`:
--     `whatsapp`, `correo` o `mano`. Por `whatsapp` o `correo` no se llega a `aprobado` ni a `enviado` sin
--     aprobación; `mano` (la persona la manda ella misma) no la pide. `enviado` y solo `enviado` lleva
--     `enviado_en`; `rechazado` y `no_se_pudo` llevan su motivo.
--   · `serie_facturas`: el contador por año (`anio`, `ultimo`). NACE VACÍA: la primera que haga Lucy es
--     FAC-2026-001.
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo DDL en
-- producción) y antes de eso, en ensayo, con `python3 tools/ensayo_facturas.py --ensayo` (arma el SQL para
-- `psql`, sin conectarse). Las cinco tablas son NUEVAS: no se reescribe ninguna fila ni se toca ninguna tabla
-- que exista (solo se miran `proyectos` y `cotizaciones` para las llaves foráneas).
--
-- EL PROCESO VIEJO (el código que no conoce estas tablas) SIGUE ANDANDO con la base cambiada: no las nombra
-- en ningún SQL y sus consultas preparadas están apagadas para todo el proceso (`db/sin_preparadas.py`),
-- así que una tabla de más no lo rompe.
--
-- IDEMPOTENTE: `CREATE TABLE IF NOT EXISTS` y `CREATE INDEX IF NOT EXISTS` no hacen nada la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después de los `CREATE`,
-- `count(*)` de cada tabla = 0 (nacen vacías), `information_schema.tables` con CINCO filas y en
-- `pg_constraint` todas las restricciones con nombre de las cinco tablas (el guion las saca de este texto).
-- Al terminar, con el ROLLBACK, `to_regclass` de las cinco da NULL (no quedó nada).
--
-- CÓMO SE DESHACE (solo si el código que las usa ya no está desplegado, porque con él vivo se pierden las
-- facturas guardadas), en este orden por las llaves foráneas:
--   DROP TABLE IF EXISTS factura_envios;
--   DROP TABLE IF EXISTS factura_ligas;
--   DROP TABLE IF EXISTS factura_renglones;
--   DROP TABLE IF EXISTS facturas;
--   DROP TABLE IF EXISTS serie_facturas;

BEGIN;

CREATE TABLE IF NOT EXISTS facturas (
  id                   BIGSERIAL PRIMARY KEY,
  anio                 SMALLINT,
  numero               INT,
  estado               TEXT NOT NULL DEFAULT 'borrador',
  cliente_noco_id      BIGINT NOT NULL,
  para_nombre          TEXT NOT NULL,
  fecha                DATE NOT NULL,
  proyecto_id          BIGINT REFERENCES proyectos(id),
  cotizacion_id        BIGINT REFERENCES cotizaciones(id),
  cotizacion_opcion    SMALLINT,
  itbis_pct            NUMERIC(5,2),
  ncf                  TEXT,
  ncf_tipo             TEXT,
  rnc_cedula           TEXT,
  hecha_por            TEXT NOT NULL,
  hecha_por_chat_id    BIGINT,
  creado_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
  cambiado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
  emitida_en           TIMESTAMPTZ,
  anulada_en           TIMESTAMPTZ,
  anulada_por_chat_id  BIGINT,
  motivo_anulacion     TEXT,
  CONSTRAINT facturas_numero_unico
    UNIQUE (anio, numero),
  CONSTRAINT facturas_ncf_unico
    UNIQUE (ncf),
  CONSTRAINT facturas_numero_positivo
    CHECK (numero IS NULL OR numero >= 1),
  CONSTRAINT facturas_anio_y_numero_juntos
    CHECK ((anio IS NULL) = (numero IS NULL)),
  CONSTRAINT facturas_estado_valido
    CHECK (estado IN ('borrador', 'emitida', 'anulada')),
  CONSTRAINT facturas_emitida_con_numero
    CHECK (estado = 'borrador' OR numero IS NOT NULL),
  CONSTRAINT facturas_emitida_con_fecha
    CHECK ((estado = 'borrador') = (emitida_en IS NULL)),
  CONSTRAINT facturas_anulada_con_motivo
    CHECK ((estado = 'anulada' AND anulada_en IS NOT NULL AND motivo_anulacion IS NOT NULL AND
            length(trim(motivo_anulacion)) BETWEEN 1 AND 500) OR
           (estado <> 'anulada' AND anulada_en IS NULL AND anulada_por_chat_id IS NULL AND
            motivo_anulacion IS NULL)),
  CONSTRAINT facturas_nombre_valido
    CHECK (length(trim(para_nombre)) BETWEEN 1 AND 200),
  CONSTRAINT facturas_opcion_de_cotizacion
    CHECK (cotizacion_opcion IS NULL OR (cotizacion_id IS NOT NULL AND cotizacion_opcion >= 1)),
  CONSTRAINT facturas_itbis_valido
    CHECK (itbis_pct IS NULL OR (itbis_pct > 0 AND itbis_pct <= 100)),
  CONSTRAINT facturas_ncf_completo
    CHECK ((ncf IS NULL) = (ncf_tipo IS NULL)),
  CONSTRAINT facturas_ncf_normalizado
    CHECK (ncf IS NULL OR (ncf ~ '^[A-Z0-9]+$' AND length(ncf) BETWEEN 1 AND 20)),
  CONSTRAINT facturas_ncf_tipo_valido
    CHECK (ncf_tipo IS NULL OR length(trim(ncf_tipo)) BETWEEN 1 AND 20),
  CONSTRAINT facturas_rnc_cedula_valido
    CHECK (rnc_cedula IS NULL OR length(trim(rnc_cedula)) BETWEEN 1 AND 20),
  CONSTRAINT facturas_hecha_por_valida
    CHECK (hecha_por IN ('casa', 'natalia')),
  CONSTRAINT facturas_hecha_por_con_persona
    CHECK ((hecha_por = 'casa') = (hecha_por_chat_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_facturas_cliente
  ON facturas (cliente_noco_id);
CREATE INDEX IF NOT EXISTS idx_facturas_proyecto
  ON facturas (proyecto_id)
  WHERE proyecto_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS factura_renglones (
  id          BIGSERIAL PRIMARY KEY,
  factura_id  BIGINT NOT NULL REFERENCES facturas(id),
  orden       SMALLINT NOT NULL,
  descripcion TEXT NOT NULL,
  detalle     TEXT,
  monto       NUMERIC(14,2) NOT NULL,
  CONSTRAINT factura_renglones_orden_unico
    UNIQUE (factura_id, orden),
  CONSTRAINT factura_renglones_orden_positivo
    CHECK (orden >= 1),
  CONSTRAINT factura_renglones_descripcion_valida
    CHECK (length(trim(descripcion)) BETWEEN 1 AND 300),
  CONSTRAINT factura_renglones_detalle_valido
    CHECK (detalle IS NULL OR length(detalle) <= 2000),
  CONSTRAINT factura_renglones_monto_positivo
    CHECK (monto > 0)
);

CREATE TABLE IF NOT EXISTS factura_ligas (
  id                 BIGSERIAL PRIMARY KEY,
  factura_id         BIGINT NOT NULL REFERENCES facturas(id),
  tipo               TEXT NOT NULL,
  ref                TEXT NOT NULL,
  codigo             TEXT,
  creado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
  creado_por_chat_id BIGINT,
  desligada_en       TIMESTAMPTZ,
  CONSTRAINT factura_ligas_tipo_valido
    CHECK (tipo IN ('sesion', 'trabajo')),
  CONSTRAINT factura_ligas_ref_valida
    CHECK (length(ref) BETWEEN 1 AND 100 AND ref = trim(ref)),
  CONSTRAINT factura_ligas_codigo_valido
    CHECK (codigo IS NULL OR length(codigo) <= 100)
);

CREATE UNIQUE INDEX IF NOT EXISTS factura_ligas_una_viva
  ON factura_ligas (factura_id, tipo, ref)
  WHERE desligada_en IS NULL;
CREATE INDEX IF NOT EXISTS idx_factura_ligas_de_la_sesion
  ON factura_ligas (tipo, ref)
  WHERE desligada_en IS NULL;

CREATE TABLE IF NOT EXISTS factura_envios (
  id                   BIGSERIAL PRIMARY KEY,
  factura_id           BIGINT NOT NULL REFERENCES facturas(id),
  canal                TEXT NOT NULL,
  estado               TEXT NOT NULL DEFAULT 'pendiente',
  creado_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
  aprobado_por_chat_id BIGINT,
  aprobado_en          TIMESTAMPTZ,
  enviado_en           TIMESTAMPTZ,
  motivo               TEXT,
  CONSTRAINT factura_envios_canal_valido
    CHECK (canal IN ('whatsapp', 'correo', 'mano')),
  CONSTRAINT factura_envios_estado_valido
    CHECK (estado IN ('pendiente', 'aprobado', 'rechazado', 'enviado', 'no_se_pudo')),
  CONSTRAINT factura_envios_aprobacion_completa
    CHECK ((aprobado_por_chat_id IS NULL) = (aprobado_en IS NULL)),
  CONSTRAINT factura_envios_no_sale_sin_aprobar
    CHECK (canal = 'mano' OR estado NOT IN ('aprobado', 'enviado') OR aprobado_en IS NOT NULL),
  CONSTRAINT factura_envios_enviado_con_fecha
    CHECK ((estado = 'enviado') = (enviado_en IS NOT NULL)),
  CONSTRAINT factura_envios_motivo_si_falla
    CHECK (estado NOT IN ('rechazado', 'no_se_pudo') OR
           (motivo IS NOT NULL AND length(trim(motivo)) BETWEEN 1 AND 500))
);

CREATE INDEX IF NOT EXISTS idx_factura_envios_factura
  ON factura_envios (factura_id);

CREATE TABLE IF NOT EXISTS serie_facturas (
  anio   SMALLINT PRIMARY KEY,
  ultimo INT NOT NULL,
  CONSTRAINT serie_facturas_ultimo_valido
    CHECK (ultimo >= 0)
);

COMMENT ON TABLE facturas IS
  'Las facturas. El código que se ve es FAC-<anio>-<numero con 3 cifras>. Estados: borrador, emitida, anulada. '
  'No guarda el pago (se lee de la App de registro) ni los totales (salen de los renglones y de itbis_pct).';

COMMENT ON TABLE factura_renglones IS
  'Los renglones de una factura: descripción, detalle y monto de la línea.';

COMMENT ON TABLE factura_ligas IS
  'A qué sesiones o trabajos del Registro pertenece una factura. ref es el identificador estable que manda la App, '
  'guardado como texto. Desligar es ponerle desligada_en. La regla de que toda factura tenga una liga viva la '
  'exige el código, no la base.';

COMMENT ON TABLE factura_envios IS
  'Por dónde salió una factura y la aprobación del equipo que se pide antes de mandarla al cliente. '
  'El canal mano no pide aprobación.';

COMMENT ON TABLE serie_facturas IS
  'El contador de números de factura, una fila por año: ultimo es el último número dado. '
  'Nace vacía: la serie empieza con el sistema.';

COMMIT;
