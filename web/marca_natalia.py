"""La marca de Natalia en la página de Proyectos (19-oct-2026, pedido de Tiziano: «podemos usar esto como un
pequeñito icono al lado de lo que Natalia lee?»).

Qué dice: al lado de cada dato de un proyecto que Natalia le puede contar al cliente, la página pinta un icono
pequeño (la «N» roja sobre negro que dio Tiziano). Este módulo es la ÚNICA lista de qué lee Natalia; la plantilla
no la repite: pregunta aquí.

QUÉ LEE NATALIA: de lo que manda `GET /api/code/clientes/{noco_id}/proyectos` (`web/api_code.py::proyectos_del_cliente`),
lo que llega al cliente es el nombre del proyecto, su estado y la tarea que sigue (título y fecha). Eso lo dice el mapa
de Natalia, que NO se leyó desde Lucy (es otro proyecto): es lo dicho, sin comprobar por quien lo escribió aquí.
Lo que Lucy manda y Natalia no le pasa al cliente no lleva marca. `tests/test_marca_natalia.py` exige que `LEE` y
`NO_LEE` juntos sean EXACTAMENTE los campos que la ruta real manda: un campo nuevo en la ruta obliga a decidir aquí
si se marca.

Los nombres son los caminos del JSON de la ruta (`siguiente.titulo` = el campo `titulo` de `siguiente`).
"""
from __future__ import annotations

TEXTO = "Natalia se lo puede contar al cliente"

LEE = frozenset({"nombre", "estado", "siguiente.titulo", "siguiente.fecha"})
NO_LEE = frozenset({"id", "admite_tareas", "responsable", "siguiente.tarea_id", "siguiente.vencida", "responsables"})

# La «N» de Natalia, 64x64 PNG (2972 bytes), dentro del código: sin pedir nada afuera.
_PNG_BASE64 = (
    "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"
)

ICONO = "data:image/png;base64," + _PNG_BASE64
