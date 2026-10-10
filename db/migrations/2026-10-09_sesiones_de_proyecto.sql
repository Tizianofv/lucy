-- Las decisiones de la casa sobre las sesiones de un proyecto (partes 11 y 13 de la página
-- completa del proyecto, 9-oct-2026; Tiziano: «las sesiones se puede borrar y buscar para
-- agregar» y, a qué es borrar, «Si es quitarla del proyecto»).
--
--   · `sesiones_de_proyecto`: UNA fila por decisión a mano de la casa sobre la lista que el
--     registro del estudio le manda a Lucy para un proyecto.
--       - `proyecto_id` (BIGINT): el proyecto de Lucy.
--       - `sesion_ref` (TEXT): CON QUÉ SE IDENTIFICA LA SESIÓN DEL REGISTRO, tal como lo manda la
--         App (`ref`: el Id de su fila). Lucy no lo interpreta: lo guarda y lo compara como texto.
--       - `codigo` (TEXT, NULL = la App no lo mandó): el código que la sesión tenía al decidir;
--         se guarda, y se pinta en el aviso de una agregada que la App ya no devuelve o devolvió
--         cancelada (parte 13). No se liga por él.
--       - `modo` (TEXT): `quitada` o `agregada` (el vocabulario entero lo fija el CHECK).
--       - `creado_en`, `creado_por_chat_id` (BIGINT): cuándo y desde qué sesión de la casa.
--         Sin llave foránea, igual que `participantes.creado_por_chat_id`: un chat no es una fila.
--       - `borrado_en` (TIMESTAMPTZ, NULL = viva): deshacer la decisión (devolver una quitada, o
--         sacar una agregada).
--     El índice único es PARCIAL (solo entre las vivas), igual que
--     `participantes_una_vez_por_proyecto`: una sola decisión viva por proyecto y sesión.
--     No se guarda nada más: ni fecha, ni sala, ni nombre, ni un solo monto (diseño 3.3).
--
-- NO SE APLICA ACÁ. Lo corre la sala, con `python3 db/backup.py` antes (regla del repo para todo
-- DDL en producción). La tabla es NUEVA: no se reescribe ninguna fila, no se toca ninguna tabla
-- que exista y ningún proyecto queda con decisiones.
--
-- EL PROCESO VIEJO (el código de `1e63a58`, que no conoce la tabla) SIGUE ANDANDO con la base
-- cambiada: no nombra `sesiones_de_proyecto` en ningún SQL y sus consultas preparadas están
-- apagadas para todo el proceso (`db/sin_preparadas.py`), así que una tabla de más no lo rompe.
-- Y el código nuevo anda con la tabla AUSENTE: la página carga con el bloque de sesiones como
-- hoy y sin ningún control (`db.sesiones_quitadas_de_proyectos` devuelve «no disponible» con
-- SQLSTATE 42P01), y las rutas de las sesiones contestan que NO se cambió nada.
--
-- IDEMPOTENTE: `CREATE TABLE IF NOT EXISTS` y `CREATE UNIQUE INDEX IF NOT EXISTS` no hacen nada
-- la segunda vez.
--
-- QUÉ TIENE QUE DAR EL ENSAYO (dentro de una transacción con ROLLBACK): después del `CREATE`,
-- `SELECT count(*) FROM sesiones_de_proyecto` = 0 (nace vacía: nada se copia ni se reescribe),
-- `information_schema.tables` con UNA fila `sesiones_de_proyecto`, y el índice
-- `sesiones_de_proyecto_una_vez` con `indisunique` e `indpredicate` no nulo (es parcial).
--
-- CÓMO SE DESHACE (solo si el código nuevo ya no está desplegado; con el código nuevo vivo, la
-- página vuelve a salir sin controles, pero las decisiones que se guardaron se pierden):
--   DROP TABLE IF EXISTS sesiones_de_proyecto;

BEGIN;

CREATE TABLE IF NOT EXISTS sesiones_de_proyecto (
  id                 BIGSERIAL PRIMARY KEY,
  proyecto_id        BIGINT NOT NULL REFERENCES proyectos(id),
  sesion_ref         TEXT NOT NULL,
  codigo             TEXT,
  modo               TEXT NOT NULL,
  creado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
  creado_por_chat_id BIGINT NOT NULL,
  borrado_en         TIMESTAMPTZ,
  CONSTRAINT sesiones_de_proyecto_modo_valido
    CHECK (modo IN ('quitada', 'agregada'))
);

CREATE UNIQUE INDEX IF NOT EXISTS sesiones_de_proyecto_una_vez
  ON sesiones_de_proyecto (proyecto_id, sesion_ref)
  WHERE borrado_en IS NULL;

COMMENT ON TABLE sesiones_de_proyecto IS
  'Las decisiones a mano de la casa sobre las sesiones que el registro le manda a Lucy para un '
  'proyecto: quitada o agregada. Una viva por proyecto y sesión. Lucy no copia el '
  'dato de la sesión: guarda su identificador estable (sesion_ref) y el código que la sesión '
  'tenía al decidir (codigo), que se pinta en el aviso de una agregada que la App ya no devuelve.';

COMMENT ON COLUMN sesiones_de_proyecto.sesion_ref IS
  'Con qué se identifica la sesión del registro: el `ref` que manda la App (el Id de su fila), '
  'guardado como texto. No se interpreta ni se liga por él.';

COMMIT;
