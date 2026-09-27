"""Rellena `personas.bandeja_id`/`preferencias.bandeja_id` en las filas que
ya existen (§E, 27-sep-2026, parte E del plan de construcción "Code como
responsable de tareas técnicas").

POR QUÉ EXISTE: la migración `2026-09-27_dueno_personas_preferencias.sql`
agrega la columna, pero toda fila creada ANTES de que el código de esta
parte se desplegara nace con `bandeja_id = NULL` -- "sin dueño", aunque en
muchos casos SÍ hay evidencia de quién la creó, guardada en
`log_acciones` (accion='crear', el mismo `bandeja_id` que ya viajaba como
parámetro de `crud.guardar_preferencia`/`crud.perfil` desde antes de esta
parte, solo que sin guardarse en la fila).

SIN ADIVINAR, EN NINGÚN PASO: si `log_acciones` no tiene una fila de
'crear' con `bandeja_id` puesto para ese registro, la fila queda SIN
dueño. Si la `bandeja` de ese `bandeja_id` tiene un origen NO confiable
(la MISMA lista de `db/lectura_dueno.py::_ORIGENES_CONFIABLES_DE_BANDEJA`,
no una nueva -- lo aprendido en la parte A: `origen='banco'` pone el chat
del dueño sobre correo de cualquier buzón, así que no prueba nada), queda
sin dueño. Si el `chat_id` no es de nadie que `config.NOMBRES_POR_CHAT`
reconozca, se cuenta como "de otro chat" -- ni Tiziano ni Rosi ni nadie
conocido -- y TAMPOCO se le pone `bandeja_id` (no hay necesidad: la
lectura de la parte A solo busca `chat_id = CHAT_ID_DUENO`, así que
ponerle un `bandeja_id` que apunta a un chat ajeno no cambiaría nada de
lo que Code puede leer, y sí dejaría una FK apuntando a algo que nadie
pidió).

MODO QUE SOLO CUENTA POR OMISIÓN -- nunca escribe sin que se lo pidan:

    python3 tools/rellenar_duenos.py             # cuenta, no toca
    python3 tools/rellenar_duenos.py --escribir  # escribe de verdad

Cada fila que se rellena de verdad deja su huella en `log_acciones`
(`actor='relleno'`, igual que `tools/rellenar_categorias.py`), así que se
puede ver qué tocó y deshacerlo.

Para la sala, desde `/Users/controlroom/Documents/IA CDS/lucy`, DESPUÉS de
aplicar la migración de §E:

    railway run -s Postgres -- python3 \\
      "/Users/controlroom/Documents/IA CDS/lucy-trabajos/code-duenos/tools/rellenar_duenos.py"
    # y con --escribir al final para que escriba de verdad.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import por el EFECTO: deja `prepare_threshold=None` puesto para el
# proceso entero (ver db/sin_preparadas.py).
import db.sin_preparadas  # noqa: E402,F401

import psycopg  # noqa: E402

import config  # noqa: E402
from db.lectura_dueno import _ORIGENES_CONFIABLES_DE_BANDEJA  # noqa: E402

_TABLAS = ("personas", "preferencias")


def _candidato_bandeja_id(conn, tabla: str, registro_id: int) -> int | None:
    """El `bandeja_id` con el que se creó esta fila, según `log_acciones`
    -- o `None` si no hay ninguna huella (personas creadas por `db.
    buscar_o_crear_persona` ANTES de esta parte no llevaban huella en
    absoluto: "personas NO lleva huella", `db/db.py::_buscar_o_crear`)."""
    fila = conn.execute(
        "SELECT bandeja_id FROM log_acciones "
        "WHERE tabla = %s AND accion = 'crear' AND registro_id = %s "
        "AND bandeja_id IS NOT NULL "
        "ORDER BY id LIMIT 1",
        (tabla, registro_id)).fetchone()
    return fila[0] if fila else None


def _clasificar(conn, bandeja_id: int | None,
                nombres_por_chat: dict[int, str]) -> tuple[int | None, str]:
    """(chat_id o None, la etiqueta para el conteo). `chat_id is None`
    quiere decir "sin dueño" -- no se escribe nada para esa fila."""
    if bandeja_id is None:
        return None, "sin dueño (sin huella en log_acciones)"
    fila = conn.execute(
        "SELECT chat_id, origen FROM bandeja WHERE id = %s", (bandeja_id,)
    ).fetchone()
    if fila is None:
        return None, "sin dueño (la bandeja de la huella ya no existe)"
    chat_id, origen = fila
    if origen not in _ORIGENES_CONFIABLES_DE_BANDEJA:
        return None, f"sin dueño (origen no confiable: {origen!r})"
    if chat_id is None:
        return None, "sin dueño (la bandeja no tiene chat_id)"
    nombre = nombres_por_chat.get(chat_id)
    if nombre is None:
        return None, "de otro chat (no reconocido en NOMBRES_POR_CHAT)"
    return chat_id, nombre


def main() -> int:
    escribir = "--escribir" in sys.argv
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("Falta DATABASE_URL en el entorno.", file=sys.stderr)
        return 2

    nombres_por_chat = dict(config.NOMBRES_POR_CHAT)

    with psycopg.connect(url) as conn:
        total_escritas = 0
        for tabla in _TABLAS:
            filas = conn.execute(
                f"SELECT id FROM {tabla} "
                "WHERE bandeja_id IS NULL AND borrado_en IS NULL ORDER BY id"
            ).fetchall()

            conteos: dict[str, int] = {}
            asignaciones: list[tuple[int, int]] = []  # (id, bandeja_id)
            for (rid,) in filas:
                candidato = _candidato_bandeja_id(conn, tabla, rid)
                chat_id, etiqueta = _clasificar(conn, candidato, nombres_por_chat)
                conteos[etiqueta] = conteos.get(etiqueta, 0) + 1
                if chat_id is not None:
                    asignaciones.append((rid, candidato))

            print(f"\n{tabla} -- {len(filas)} filas sin dueño hoy:")
            for etiqueta, n in sorted(conteos.items(), key=lambda kv: -kv[1]):
                print(f"  {n:>5}  {etiqueta}")
            print(f"  → {len(asignaciones)} se rellenarían con dueño conocido")

            if escribir:
                for rid, bandeja_id in asignaciones:
                    conn.execute(
                        f"UPDATE {tabla} SET bandeja_id = %s WHERE id = %s",
                        (bandeja_id, rid))
                    conn.execute(
                        """
                        INSERT INTO log_acciones
                          (actor, accion, tabla, registro_id, antes, despues, motivo)
                        VALUES ('relleno', 'editar', %s, %s, %s, %s,
                                'bandeja_id rellenado desde log_acciones (parte E, 27-sep-2026)')
                        """,
                        (tabla, rid, json.dumps({"bandeja_id": None}),
                         json.dumps({"bandeja_id": bandeja_id})))
                total_escritas += len(asignaciones)

        if escribir:
            conn.commit()
            print(f"\nEscritas {total_escritas} filas en total.")
        else:
            print("\nEn seco. Con --escribir escribe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
