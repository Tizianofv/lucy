-- El panel de tareas dice QUIÉN TIENE PENDIENTE cada tarea (10-sep-2026).
--
-- Pedido de Tiziano: «la columna "quien la anoto" vamos a cambiarla a
-- Responsable y lo que indica es quien tiene esa tarea pendiente», y
-- «nno es relevante quien la anoto». La columna vieja se va del panel; ésta la
-- reemplaza.
--
-- POR QUÉ UNA COLUMNA NUEVA Y NO `persona_id`, que es lo primero que uno
-- intentaría. `tareas.persona_id` ya existe y significa DE QUIÉN TRATA la
-- tarea —"preguntarle a Pedro por el presupuesto"—, no quién la tiene que
-- hacer. Medido contra producción el 10-sep-2026: de las 57 tareas vivas, 30
-- tienen `persona_id` puesto. Reciclarla habría pisado ese dato en más de la
-- mitad de las filas, y las dos preguntas volverían a ser indistinguibles.
--
-- POR QUÉ UN CHAT DE TELEGRAM Y NO UN NOMBRE NI UNA FK. En este sistema no hay
-- de dónde sacar el nombre de una persona a partir de su chat: medido sobre las
-- 16 tablas y las 150 columnas del esquema, ninguna tiene a la vez un chat y un
-- nombre, y en los `.py` del repo no hay ningún mapa ni ninguna llamada a
-- Telegram que pida un perfil. Los nombres viven en la variable de entorno
-- NOMBRES_POR_CHAT para que Tiziano pueda cambiarlos sin desplegar.
--
-- Lo que se guarda acá es la IDENTIDAD —el chat—, no la etiqueta. Así, cambiar
-- «Rosi» por «Rosa» en la variable no toca una sola fila de esta tabla.
--
-- NULL ES LO NORMAL, no un error: las 57 tareas vivas nacen sin responsable y
-- se asignan desde el panel. Por eso no hay NOT NULL, no hay DEFAULT y este
-- archivo no hace ningún UPDATE — no habría con qué llenarlo, y llenarlo
-- adivinando sería inventarle a alguien un pendiente que nadie le dio.
--
-- Sin FK porque no existe ninguna tabla de chats a la que apuntar. Quién vale
-- como responsable lo decide `config.puede_ser_responsable()`, que lo deriva de
-- quién puede ENTRAR al panel (`CHAT_IDS_PERMITIDOS`) y de quién tiene nombre.
--
-- Idempotente: correrlo dos veces no hace nada la segunda.

BEGIN;

ALTER TABLE tareas ADD COLUMN IF NOT EXISTS responsable_chat_id BIGINT;

COMMENT ON COLUMN tareas.responsable_chat_id IS
  'Quién tiene pendiente la tarea: su chat de Telegram, el mismo con el que '
  'entra al panel. NULL = sin responsable, que es lo normal hasta que alguien '
  'la toma. No confundir con persona_id, que es de quién TRATA la tarea.';

COMMIT;
