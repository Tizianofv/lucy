# Lucy

Asistente personal de Tiziano por Telegram. Captura todo (texto, audio, fotos),
lo entiende, lo recuerda y —con el tiempo— se vuelve proactiva.

Roadmap completo y decisiones de arquitectura: [`docs/roadmap.md`](docs/roadmap.md).

## Dónde vive cada cosa

| Qué | Dónde |
|-----|-------|
| Código + documentación | **GitHub** (este repo) |
| Lucy corriendo + datos | **Railway** (servicio Python + Postgres) |
| Backups de la base | **Google Drive** (`Lucy/backups`) |
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
4. Correr el esquema una vez: `psql "$DATABASE_URL" -f db/schema.sql`
5. `python main.py`

## Deploy

Push a `main` → Railway redeploya solo. Los secretos viven en las Variables del
servicio de Railway (NO en el repo).
