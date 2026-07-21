"""Convertir un fallo en una pregunta.

Tiziano, cerrando la idea del cinturón: "es ahí precisamente donde el cinturón
debe actuar, si no sabe —como ella misma dijo— debería preguntar".

Tiene razón y era el hueco que quedaba. Le enseñamos a Lucy a repreguntar
cuando duda ENTENDIENDO algo, pero cuando se rompía la plomería —un SQL que no
corre, una respuesta malformada, un error inesperado— caía en un mensaje
enlatado. Y un "me tropecé, probá de otra forma" es un callejón sin salida:
no le dice a Tiziano qué reformular, así que la conversación se muere ahí.

Una pregunta concreta, en cambio, la deja viva. "¿Te referís al balance de tus
movimientos o a otra cosa?" se contesta en cinco palabras.

Este módulo es la última red antes de darse por vencida: recibe qué pedía
Tiziano y qué falló, y devuelve algo que él pueda contestar.
"""
from __future__ import annotations

import logging

from cerebro.deepseek import MODELO, cliente

log = logging.getLogger("lucy.preguntar")

INSTRUCCIONES = """\
Sos Lucy, la asistente personal de Tiziano. Algo te salió mal y tenés que
decírselo PREGUNTANDO, no informando un error.

Devolvés SOLO el texto que le vas a mandar: una o dos frases, en su mismo
registro (español dominicano informal), que reconozcan brevemente qué no
pudiste y le pidan el dato concreto que te destrabaría.

REGLAS:
· Nada de jerga técnica, ni nombres de error, ni números de fila. "Falló la
  consulta SQL" no le sirve de nada a nadie.
· Preguntá algo que se conteste en una línea. Ofrecé opciones si ayuda:
  "¿te referís a X o a Y?".
· No te disculpes de más. Una disculpa larga es ruido; la pregunta es lo útil.
· Si de verdad no hay nada que preguntar porque es algo que todavía no sabés
  hacer, decilo en criollo y sin vueltas.
· Texto plano, sin markdown ni HTML.\
"""

# Lo que se dice si hasta la repregunta falla. Nunca quedarse mudo.
RESPALDO = ("Me quedé trabada con eso y no supe cómo resolverlo. "
            "¿Me lo decís de otra forma?")


async def repreguntar(mensaje: str, problema: str) -> str:
    """Qué pedía Tiziano + qué falló → una pregunta que él pueda contestar.

    Si esto también falla, devuelve el respaldo. Es la última red: acá ya no
    se puede lanzar nada más, porque más arriba solo queda el silencio.
    """
    try:
        texto = (await cliente.chat.completions.create(
            model=MODELO,
            messages=[
                {"role": "system", "content": INSTRUCCIONES},
                {"role": "user", "content":
                    f'Tiziano te dijo: "{mensaje}"\n\n'
                    f"Lo que falló de tu lado: {problema}"},
            ],
            temperature=0.3,
        )).choices[0].message.content
        return (texto or "").strip() or RESPALDO
    except Exception:
        log.exception("Tampoco pude armar la repregunta")
        return RESPALDO
