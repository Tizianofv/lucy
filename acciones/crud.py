"""CRUD sobre las entidades (tareas, eventos, notas, gastos).

Dos reglas que TODA operación debe respetar — son pilares, no opcionales:
  · Borrar = marcar borrado_en (soft-delete). Nunca DELETE real. → reversibilidad
  · Toda operación escribe una fila en log_acciones con antes/después.
    → auditoría + autoexplicación + el "deshacer" sale gratis de ahí.

Estado: andamiaje. Se implementa junto con Nivel 2 (cuando haya qué crear).
"""

# TODO: crear(tabla, datos, motivo, bandeja_id) → INSERT + log_acciones
# TODO: editar(tabla, id, cambios, motivo)      → captura 'antes', UPDATE, log
# TODO: borrar(tabla, id, motivo)               → set borrado_en, log
# TODO: restaurar(tabla, id)                    → copia 'antes' del log, log
