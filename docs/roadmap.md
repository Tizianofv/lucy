# Lucy — Roadmap y decisiones de arquitectura

## Visión

Asistente personal que captura todo lo que Tiziano le manda, lo entiende, lo
recuerda y con el tiempo se vuelve proactivo. **Estrategia: pulir Niveles 1-3
antes de tocar lo proactivo (5-7).** Un agente proactivo que se equivoca de
datos es proactivamente molesto.

## Los 7 niveles

**Nivel 1 — Captura perfecta (que nada se pierda)**
1. Recibir texto por Telegram y responder al instante.
2. Aceptar audios y transcribirlos.
3. Aceptar fotos (cartel, ticket, tarjeta) y extraer datos.
4. Guardar notas/tareas/eventos con confirmación clara.
5. Bandeja de entrada universal: todo cae en un solo sitio.

**Nivel 2 — Comprensión profunda**
6. Fechas relativas y ambiguas.
7. Clasificación automática: tarea/cita/nota/idea/gasto/pregunta.
8. Extracción estructurada: quién, qué, cuándo, dónde, duración, proyecto.
9. Manejo de ambigüedad: si falta dato crítico pregunta; si es deducible, lo deduce y avisa.
10. Consultas en lenguaje natural sobre la agenda.

**Nivel 3 — Memoria como la de un buen asistente humano**
11. Memoria conversacional ("muévelo a las 6").
12. Perfil vivo: proyectos, personas clave, horarios, cosas que odia.
13. Memoria a largo plazo consultable (búsqueda semántica → pgvector).
14. Memoria de patrones.
15. CRUD completo.
16. Vínculos entre cosas (tarea↔proyecto↔persona↔mensaje).

**Nivel 4 — Omnipresencia** · correo, calendario, tareas, notas, Telegram unificados; dedup; enriquecimiento.
**Nivel 5 — Proactividad** · briefing matinal, resumen semanal, conflictos, recordatorios contextuales, huecos, bolas que se caen, prep de reuniones.
**Nivel 6 — Autonomía con juicio** · priorización, borradores en su tono, negociación de agenda, micro-decisiones con límites, modos, aprendizaje de correcciones.
**Nivel 7 — Meta-capacidades** · autoexplicación, informe de errores propio, log auditable, degradación elegante, backups.

## Pilares transversales (todos los niveles)

- **Seguridad**: bot cerrado al chat ID de Tiziano; secretos en entorno; cuidado con qué datos van a la API.
- **Confirmación proporcional al riesgo**: borrar/enviar → pregunta; añadir nota → nunca molesta.
- **Reversibilidad**: todo se puede deshacer (soft-delete + log de antes/después).
- **Silencio inteligente**: se gana cada interrupción.

## Decisiones de arquitectura

| Decisión | Elección | Por qué |
|----------|----------|---------|
| Interfaz | Telegram | Pedido; bot personal cerrado a un chat ID |
| Núcleo | **Python en Railway** (no n8n) | Niveles 3-7 son software, no workflows; con Claude escribiendo, Python es más rápido incluso al arranque. Natalia sigue en n8n; Lucy es 100% código |
| Datos | Postgres propio + pgvector | CRUD real + vínculos (FKs) + búsqueda semántica (Nivel 3) |
| UI de datos | NocoDB sobre el mismo Postgres | Ver/editar como planilla |
| IA | **Solo Gemini Flash** | Texto+audio+visión en un modelo; capa gratuita; buen tool-calling para Nivel 6. DeepSeek descartado (solo texto). Plan B documentado |
| Repo | GitHub privado → deploy auto a Railway | Accesible desde cualquier compu; versionado = reversibilidad del propio código |
| Zona horaria | America/Santo_Domingo (UTC-4, sin DST) | Ancla de toda interpretación de fechas |

### La columna vertebral: tabla `bandeja`

Todo mensaje cae crudo en `bandeja` y se confirma recepción **antes** de que la
IA lo toque. Si Gemini/la conexión fallan, el mensaje ya está a salvo y encolado.
Esto hace real el req 5 y el pilar de degradación elegante (req 39). La captura
está desacoplada del cerebro: si algún día el cerebro se muda a otro lado, la
captura y los datos no se tocan.

### Esquema

Ver [`../db/schema.sql`](../db/schema.sql). Tablas: `bandeja`, `tareas`,
`eventos`, `notas`, `gastos`, `personas`, `proyectos`, `log_acciones`.
Principios grabados en el diseño: soft-delete (`borrado_en`) en todo,
`bandeja_id` para trazabilidad, `log_acciones` con antes/después desde el día 1.
