-- El resumen del correo, directo a los dos (encargo 3 del diseño "Rosi
-- independiente", 22-sep-2026).
--
-- Decisión de Tiziano, textual: el buzón del estudio también le llega a
-- Rosi; el buzón mixto (el suyo) NO. Él sigue recibiendo el suyo igual que
-- hoy, con los dos buzones juntos.
--
-- POR QUÉ ESTO TOCA LA BASE. Hasta este encargo, `correo_reportado` tenía
-- PRIMARY KEY (cuenta, uid) -- UNA fila por correo, sin importar a quién se
-- le informó. Si el mismo correo del buzón del estudio tiene que aparecer en
-- el resumen de Tiziano Y en el de Rosi, "ya informado" deja de ser un hecho
-- del correo: es un hecho del PAR (correo, destinatario). Sin esto, el
-- primero de los dos que reciba su resumen le "roba" el correo al otro --
-- exactamente lo que el encargo prohíbe ("que mandarle a Rosi no le robe a
-- Tiziano ningún correo de su resumen, ni al revés").
--
-- LA COLUMNA NUEVA: destino_chat_id. NULL en las filas que ya existían antes
-- de este encargo -- son correos informados cuando sólo existía UN destino
-- por buzón (el dueño), así que el código las trata como "informadas al
-- dueño" sin que esta migración tenga que escribir ningún chat_id (los
-- chat_id no van en el repositorio, que es público -- ver config.py, «el
-- número de chat de una persona no es material de pantalla»). Un correo
-- nuevo, informado a Rosi, es una fila NUEVA con su propio destino_chat_id:
-- nunca pisa la fila del dueño.
--
-- LA CLAVE: se cae la PRIMARY KEY vieja (cuenta, uid) -- ya no puede ser
-- única sin el destino, o nunca podría existir una segunda fila para el
-- mismo correo -- y la reemplaza un ÍNDICE ÚNICO con nombre,
-- idx_correo_reportado_destino, sobre (cuenta, uid, destino_chat_id). Mismo
-- nombre en la migración y en db/schema.sql -- test_esquema_reproduce_la_
-- base.py exige que coincidan, para que una base nueva (armada solo con
-- schema.sql) y ésta terminen con la MISMA clave.
--
-- NO SE APLICA ACÁ. Esto lo corre la sala, con `python3 db/backup.py`
-- antes (regla del repo para todo DDL en producción). El código de este
-- encargo tolera que la columna todavía no exista (SQLSTATE 42703) y cae a
-- como se comportaba antes -- ver `db.correos_ya_reportados` y
-- `db.marcar_correo_reportado` -- mismo patrón que ya usa
-- `cerebro/despertador.py::revisar` con `primero_id`.
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

BEGIN;

ALTER TABLE correo_reportado ADD COLUMN IF NOT EXISTS destino_chat_id BIGINT;

-- La PK vieja no tiene nombre propio en el repo (Postgres se lo inventa:
-- casi seguro "correo_reportado_pkey", el patrón <tabla>_pkey). Se cae con
-- IF EXISTS para no reventar si el nombre real difiere o si esto ya corrió.
ALTER TABLE correo_reportado DROP CONSTRAINT IF EXISTS correo_reportado_pkey;

CREATE UNIQUE INDEX IF NOT EXISTS idx_correo_reportado_destino
  ON correo_reportado (cuenta, uid, destino_chat_id);

COMMIT;
