# Lucy

Asistente personal de Tiziano por Telegram. Captura todo (texto, audio, fotos),
lo entiende, lo recuerda y —con el tiempo— se vuelve proactiva.

Roadmap completo y decisiones de arquitectura: [`docs/roadmap.md`](docs/roadmap.md).

## Dónde vive cada cosa

| Qué | Dónde |
|-----|-------|
| Código + documentación | **GitHub** (este repo) |
| Lucy corriendo + datos | **Railway** (servicio Python + Postgres) |
| Backups de la base | **Google Drive** (`Lucy/backups`) — ver [Respaldos](#respaldos) |
| Copia de trabajo | La compu donde estés (desechable, `git pull`) |

## Stack

Telegram → Python (`python-telegram-bot`, long-polling) → Postgres (+ pgvector) → NocoDB para ver los datos.
IA: **Gemini Flash** (texto + audio + visión, capa gratuita).

## Estructura

```
lucy/
├── main.py            arranque; conecta Telegram y despacha
├── config.py          zona horaria (Santo Domingo) + secretos desde entorno
├── captura/           texto/audio/foto → bandeja + "recibí ✅" (NUNCA llama IA)
├── cerebro/           Gemini: clasificar, transcribir, leer imágenes (Nivel 2+)
├── acciones/          CRUD con soft-delete + log_acciones (Nivel 2+)
├── db/                schema.sql (esquema v2) + acceso a Postgres
└── docs/              roadmap y decisiones
```

**Regla de oro:** `captura/` no importa nada de `cerebro/`. El mensaje se guarda
crudo *antes* de que la IA lo toque, así nada se pierde aunque la IA falle.

## Correr en local

0. Python **3.12** — lo dice `.python-version` y es lo que corre en Railway. Con
   3.9 los pines de `requirements.txt` no se instalan siquiera
   (`psycopg[binary]==3.2.3` no tiene rueda para 3.9). Ver
   [Correr las pruebas](#correr-las-pruebas) si hace falta bajar un 3.12 aislado.
1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -r requirements.txt`
3. Copiar `.env.example` a `.env` y completar los valores.
4. Crear el esquema una vez: `psql "$DATABASE_URL" -f db/schema.sql`
5. Sobre una base que YA existía, aplicar las migraciones pendientes en orden
   de fecha: `psql "$DATABASE_URL" -f db/migrations/<fecha>_<nombre>.sql`
   (son idempotentes: correr una dos veces no hace nada la segunda).
6. `python main.py`

## Correr las pruebas

Un comando, y no hace falta acordarse de ninguna bandera:

```bash
pytest
```

Eso es todo. La configuración vive en `pytest.ini` y ya trae `asyncio_mode = auto`,
que es lo que hace que las pruebas `async def` **corran**. Sin esa línea
`pytest` se las salta en silencio y la suite se ve más verde de lo que es:
medido el 4-sep-2026 contra `6f7d4da`, sin ella eran 300 pasadas y con ella 327.
Veintisiete pruebas que nadie había corrido nunca.

Las dependencias de prueba están fijadas aparte, en `requirements-dev.txt`, y no
se instalan en Railway:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

**Sobre Python 3.12, que es lo que dice `.python-version` y lo que corre en
Railway.** No es un detalle: `psycopg[binary]==3.2.3` no tiene rueda para 3.9, así
que bajo 3.9 los pines de `requirements.txt` ni siquiera se instalan, y un verde
sacado de un entorno con otras versiones no dice nada sobre lo que va a pasar en
producción. Si en tu máquina no hay un 3.12, `uv` baja uno aislado:

```bash
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install -r requirements.txt -r requirements-dev.txt
```

### Lo que no siempre se puede probar

El corpus de correos bancarios (`tests/fixtures/**/*.eml`) son movimientos reales
de Tiziano y de Rosi, y está fuera de git a propósito (`.gitignore:23`). Donde no
está —un runner limpio de GitHub, una compu recién clonada— las pruebas que lo
necesitan salen **SALTADAS con su motivo**, nunca en verde y nunca en rojo: se
dice que no corrieron. Son 30 de 353. En la máquina que sí tiene el corpus, corren
todas y `pytest.ini` no cambia nada.

Cada archivo sigue teniendo su bloque `__main__`, así que `python3 tests/test_x.py`
también funciona. Pero ése no carga `conftest.py`: sin el corpus, las pruebas que
lo necesitan revientan en vez de saltarse. Para la suite completa, `pytest`.

### El freno

`.github/workflows/pruebas.yml` corre todo esto en cada push y en cada pull
request, sobre 3.12 y con las versiones fijadas. El resumen del job lista una por
una las pruebas que no se pudieron correr, y falla si ese número sube.

**Lo que el freno NO cubre:** `tools/humo.py`, que es el que ve los errores de
acople con la base. Necesita `DATABASE_URL` de producción y sigue siendo un paso a
mano antes de desplegar cualquier cosa que toque SQL. Las suites son herméticas y
por construcción no ven esos errores.

## Deploy

Push a `main` → Railway redeploya solo. Los secretos viven en las Variables del
servicio de Railway (NO en el repo).

## Respaldos

`db/backup.py` vuelca la base entera —datos **y** esquema— a Google Drive, o
sea FUERA de Railway. De nada sirve respaldar la base en el mismo lugar que
podría caerse con ella.

```bash
python db/backup.py
```

Cada corrida deja dos archivos en `Lucy/backups/`:

| Archivo | Qué es |
|---------|--------|
| `lucy_<sello>.json.gz` | los datos de todas las tablas, más un catálogo del esquema (columnas, tipos, índices, constraints) leído por SQL |
| `lucy_<sello>.schema.sql.gz` | el `pg_dump --schema-only`, cuando hay un `pg_dump` utilizable — es el que restaura solo |

Sin `pg_dump` instalado el backup igual sale: el catálogo adentro del JSON
alcanza para reconstruir la estructura a mano. Con `pg_dump` la restauración es
un comando. Se conservan los últimos 30 pares; la rotación borra el esquema
junto con su JSON.

El destino se detecta solo (`G:\My Drive` en Windows, `~/Library/CloudStorage/…`
en macOS). Si Drive está montado en otro lado:

```bash
LUCY_BACKUP_DIR='/ruta/a/Lucy/backups' python db/backup.py
```

### Quién lo corre, y qué pasa si nadie lo hace

**Esto no corre solo dentro de Lucy.** No hay cron en Railway ni tarea en el
bucle: el backup escribe en una carpeta de Google Drive sincronizada por el
cliente de escritorio, y el contenedor de Railway no la ve. Hoy lo dispara una
tarea programada en la máquina de Tiziano.

Esa dependencia ya falló una vez: los backups corrían a las 20:00 hasta el
**29-jul-2026**, hubo dos manuales el 3 y el 5 de agosto, y después nada
durante 25 días. No falló ningún job — simplemente nadie lo ejecutó, y el
sistema no tenía cómo notarlo.

Por eso ahora cada backup exitoso deja una fila en la tabla `backups`, y el
despertador la mira: **si pasan más de 48 horas sin respaldo, Lucy avisa por
Telegram**, y lo repite una vez por día mientras siga faltando. La alerta no
depende de la máquina que corre el backup, así que cubre las tres formas de
fallar: la PC apagada, el script reventado y la tarea desprogramada.

Un respaldo que falla en silencio es peor que no tener respaldo: el que no lo
tiene lo sabe y actúa; el que cree tenerlo se entera el único día en que ya no
se puede hacer nada.

### El esquema del repo tiene que describir la base real

Cada corrida de `backup.py` compara la base viva contra `db/schema.sql` y
avisa por consola si difieren. Existe por lo que pasó: `correo_reportado` (329
filas) y `correo_estado.ultimo_reporte` vivieron meses en producción sin estar
en el esquema del repo, así que el `psql -f db/schema.sql` del paso 4 creaba
una base **rota** —el reporte matinal de correo reventaba en la primera
consulta— y nadie lo notaba porque nada lo miraba. Si esa comparación se queja,
el esquema se reconcilia; no se ignora.
