"""Órdenes: cambiar algo que ya existe (req 15 — el CRUD completo).

EL CINTURÓN ES LA PREGUNTA. Tiziano lo dijo mejor que la metáfora original:
un cinturón no te frena mientras manejás, actúa en el momento del golpe. Un
bloqueo permanente no es un cinturón, es un limitador soldado al motor.

Así que acá no hay nada prohibido de antemano. Lucy puede planear cualquier
cambio sobre cualquier fila. Lo que la separa de ejecutarlo es una pregunta:
propone en criollo qué va a hacer y espera el ✅. Si duda de CUÁL registro,
tampoco adivina — muestra los candidatos y pregunta.

Ese diseño se sostiene sobre algo que ya estaba: en Lucy borrar no es
destructivo. Es soft-delete con el 'antes' completo en log_acciones, así que
todo cambio se puede deshacer. La reversibilidad vive en el modelo de datos,
no en una lista de operaciones permitidas.

La búsqueda del registro usa el mismo camino de SOLO LECTURA que consultar.py:
buscar nunca puede modificar. El cambio ocurre después, por acciones/crud.py,
y solo con la confirmación de Tiziano.
"""
from __future__ import annotations

import json
import logging

from cerebro.consultar import ESQUEMA, _ejecutar, _validar
from cerebro.deepseek import MODELO, TZ, _ahora_txt, cliente

log = logging.getLogger("lucy.ordenar")

# Qué acciones puede llegar a proponer. 'borrar' está escrito y probado, pero
# apagado a pedido de Tiziano (2026-07-21: "ahora no quiero que borre nada
# pero es seguro que mañana sí voy a querer"). Encenderlo es agregar la
# palabra a este conjunto — no hay nada más que desarmar. Justamente ese era
# el punto: que mañana sea una línea y no un rediseño.
ACCIONES = {"completar", "editar"}
TODAS_LAS_ACCIONES = {"completar", "editar", "borrar"}

# Cuántos candidatos se ofrecen cuando la orden es ambigua. Más de esto en un
# teclado de Telegram se vuelve una pared de botones.
MAX_CANDIDATOS = 5

INSTRUCCIONES = """\
Sos la parte de Lucy que interpreta ÓRDENES: pedidos de cambiar algo que ya
existe en la base de Tiziano.

Ahora es {ahora} (zona {zona}, UTC-4, sin horario de verano).

{esquema}

Devolvés SOLO un objeto JSON con estas claves:
  accion: una de {acciones}
     · completar → marcar una tarea como hecha
     · editar → cambiar uno o más campos (mover de hora, corregir un monto,
                cambiar un título, reasignar proyecto...)
  tabla: 'tareas' | 'eventos' | 'notas' | 'movimientos'
  sql_buscar: un SELECT que encuentre el/los registros a los que se refiere.
     DEBE devolver la columna id y algunas columnas legibles para que Tiziano
     reconozca de cuál se trata (titulo, fecha, monto...). Filtrá SIEMPRE por
     borrado_en IS NULL. Traé como mucho {max} filas.
  cambios: objeto {{columna: valor}} con lo que hay que escribir.
     · Para 'completar' en tareas: {{"estado": "hecha", "completado_en": "<ISO ahora>"}}
     · Las fechas van en ISO 8601 con offset.
     · Solo las columnas que cambian. Nunca id, bandeja_id ni creado_en.
  resumen: una frase en criollo de qué se va a hacer, para mostrársela y que
     la apruebe. Ej: "Mover 'Llamar al contador' de las 10:00 a las 18:00".
  aclaracion: si la orden no se entiende o pide algo que no podés hacer,
     escribí acá qué le preguntarías o por qué no se puede. "" si todo bien.

REGLAS:
· NO adivines cuál registro es si hay varios que encajan: devolvé el SELECT
  que los traiga a todos y Lucy le va a preguntar a Tiziano cuál.
· Si la orden pide una acción que no está en {acciones}, dejá accion en "" y
  explicá en `aclaracion` qué te pidió y que todavía no lo tenés habilitado.
· El sql_buscar es SOLO de lectura. El cambio no lo hacés vos: lo hace Lucy
  después de que Tiziano apruebe.
· Sé conservador con `cambios`: tocá lo mínimo que cumpla lo que pidió. Si
  dice "movelo a las 6", cambiá la hora y nada más.\
"""


async def planear(instruccion: str) -> dict:
    """Orden en criollo → plan concreto + candidatos.

    Devuelve {'plan': {...}, 'candidatos': [...], 'aclaracion': str}. No
    ejecuta NADA: solo mira y propone.
    """
    plan = json.loads((await cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCCIONES.format(
                ahora=_ahora_txt(), zona=TZ.key, esquema=ESQUEMA,
                acciones=sorted(ACCIONES), max=MAX_CANDIDATOS)},
            {"role": "user", "content": instruccion},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )).choices[0].message.content)

    accion = str(plan.get("accion") or "").strip().lower()
    aclaracion = str(plan.get("aclaracion") or "").strip()

    if accion in TODAS_LAS_ACCIONES and accion not in ACCIONES:
        # La sabe hacer, está apagada. Se lo decimos con todas las letras en
        # vez de fingir que no se entendió: es su propia decisión, no un límite
        # técnico, y tiene que poder cambiarla sabiendo que existe.
        return {"plan": None, "candidatos": [],
                "aclaracion": f"Puedo hacerlo, pero todavía no me habilitaste a "
                              f"{accion}. Avisame y lo enciendo."}

    if accion not in ACCIONES:
        return {"plan": None, "candidatos": [],
                "aclaracion": aclaracion or "No entendí qué querés que cambie."}

    sql = str(plan.get("sql_buscar") or "").strip()
    if not sql:
        return {"plan": None, "candidatos": [],
                "aclaracion": aclaracion or "No supe dónde buscar eso."}

    candidatos = await _ejecutar(_validar(sql))
    log.info("Orden '%s' sobre %s: %s candidatos",
             accion, plan.get("tabla"), len(candidatos))

    return {
        "plan": {
            "accion": accion,
            "tabla": str(plan.get("tabla") or ""),
            "cambios": plan.get("cambios") or {},
            "resumen": str(plan.get("resumen") or "").strip(),
        },
        "candidatos": candidatos[:MAX_CANDIDATOS],
        "aclaracion": aclaracion,
    }


def describir(fila: dict) -> str:
    """Etiqueta corta de un candidato, para el botón. Sin el id, que no le
    dice nada a un humano."""
    partes = [
        str(v)[:38] for k, v in fila.items()
        if k != "id" and v is not None and str(v).strip()
    ]
    return " · ".join(partes[:2]) or f"#{fila.get('id')}"
