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

1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -r requirements.txt`
3. Copiar `.env.example` a `.env` y completar los valores.
4. Crear el esquema una vez: `psql "$DATABASE_URL" -f db/schema.sql`
5. Sobre una base que YA existía, aplicar las migraciones pendientes en orden
   de fecha: `psql "$DATABASE_URL" -f db/migrations/<fecha>_<nombre>.sql`
   (son idempotentes: correr una dos veces no hace nada la segunda).
6. `python main.py`

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
