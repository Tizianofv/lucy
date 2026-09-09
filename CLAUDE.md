# Lucy — lo que hay que saber antes de tocar este repositorio

Esto es conocimiento del proyecto: qué puede hacer daño, cómo se comprueba el
trabajo y qué decisiones ya están tomadas. Las instrucciones de trabajo del
agente vienen de su encargo, no de aquí.

Cada afirmación de abajo lleva de dónde salió. Lo que no se pudo comprobar se
dice como lo que es.

## Lo que puede hacer daño

* **Publicar en `main` despliega a Railway.** Es la acción consecuente de este
  repositorio: un push es un despliegue a producción. (`README.md`, sección
  «Deploy»: «Push a `main` → Railway redeploya solo».)
* **Nunca mostrar contraseñas reales**, ni las de Railway ni las de los buzones.
  Los scripts las leen del `.env` o se las piden al CLI de Railway en el
  momento, y viven en una variable de entorno que muere con el proceso
  (`tools/respaldo_diario.sh`, cabecera). No se copian a un reporte, a un log ni
  a un archivo.
* **DDL en producción pide respaldo antes:** `python3 db/backup.py`. Cada
  corrida además compara la base viva contra `db/schema.sql` y avisa si
  difieren; si se queja, el esquema se reconcilia, no se ignora (`README.md`,
  «El esquema del repo tiene que describir la base real»).
* **Antes de desplegar algo que toque SQL**, correr
  `DATABASE_URL=... python3 tools/humo.py`. Las suites son herméticas —dobles de
  conexión, sin red, sin Postgres— y por construcción no ven los errores de
  acople con la base: el 31-ago-2026 se publicaron tres de esa familia con todo
  en verde y uno tumbó la portada del panel (`tools/humo.py`, cabecera).

## Cómo se comprueba el trabajo

* **La suite entera, con `pytest`.** La configuración vive en `pytest.ini` y ya
  trae `asyncio_mode = auto`; sin esa línea las pruebas `async def` se saltan en
  silencio. En esta Mac no hay `pytest` en el PATH: se nombra el intérprete del
  entorno virtual con su ruta completa. Medido el 8-sep-2026 sobre `68e6aa7`, en
  una máquina sin el corpus de correos: **462 passed, 30 skipped**.
* **`.github/workflows/pruebas.yml` corre la suite en cada push y cada pull
  request**, sobre 3.12 y con las versiones fijadas. Lo que ese freno NO cubre
  es `tools/humo.py`, que necesita `DATABASE_URL` de producción y sigue siendo
  un paso a mano.
* **Regresión obligatoria en código compartido.** Si el cambio toca un archivo
  del que dependen otros módulos, se corren TODAS las suites que dependen de él,
  no solo la de la tarea. La dependencia se comprueba con `grep` sobre los
  imports, no de memoria.
* **Números, no adjetivos.** Nada de «salió bien» ni «funcionó correctamente»:
  va el comando, la salida y las cifras. Si algo falló, la salida real pegada.

## Decisiones y hechos del proyecto

* **El respaldo lo dispara launchd desde `tools/respaldo_diario.sh`**, no una
  persona (cabecera del script), y deja registro en
  `~/Library/Logs/lucy-respaldo.log` (variable `REGISTRO` del mismo script). El
  nombre del trabajo de launchd —`com.lucy.respaldo`— y la hora —21:00— no
  aparecen en ninguna parte del repositorio: vienen de una nota vieja y **no
  están comprobados**.
* **La copia desde la que corre el respaldo vive dentro de Google Drive a
  propósito.** Tiziano lo decidió sabiendo el riesgo de que la sincronización
  corrompa `.git`. No re-litigar. La ruta está en la variable `REPO` de
  `tools/respaldo_diario.sh`. Ojo: **no es la única copia del repositorio**; el
  clon desde el que estés trabajando puede ser otro. Se comprueba con
  `git rev-parse --git-common-dir`, no de memoria.
* **El dinero es `Decimal`, nunca `float`**, y los vocabularios son cerrados: se
  lanza excepción en vez de elegir un valor por defecto
  (`cerebro/bancos/categorias.py:230`, `acciones/crud.py:473`).
* **Las categorías se inyectan al prompt del agente desde `CATEGORIAS`**, no
  copiadas a mano (`cerebro/agente.py:411`). Hay una prueba que falla si alguna
  aparece escrita en el prompt:
  `tests/test_crud_dedup.py::test_el_agente_conoce_el_codigo_y_las_categorias_del_codigo`.
* **`captura/` no importa nada de `cerebro/`.** El mensaje se guarda crudo antes
  de que la IA lo toque, así nada se pierde aunque la IA falle (`README.md`,
  «Regla de oro»).
* **El corpus de correos bancarios está fuera de git a propósito**
  —`tests/fixtures/**/*.eml`, ver `.gitignore:23`—: son movimientos reales de
  Tiziano y de Rosi.
  Donde no está, las pruebas que lo necesitan salen SALTADAS con su motivo,
  nunca en verde.
