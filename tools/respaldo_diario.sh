#!/bin/zsh
# Respaldo diario de la base de Lucy. Lo dispara launchd, no una persona.
#
# POR QUÉ EN ESTE MAC Y NO EN RAILWAY: el respaldo se guarda en la carpeta de
# Google Drive, y el contenedor de Railway no la ve. Eso también es lo que
# causó los 25 días de agosto sin copia: la única prueba de que un respaldo
# había ocurrido era un archivo que el proceso de Railway no podía mirar. Por
# eso backup.py escribe además una fila en la tabla `backups`, y el despertador
# grita a las 48 horas sin ninguna. Este script puede fallar; lo que no puede
# pasar es que falle en silencio.
#
# PATH EXPLÍCITO: launchd no corre un shell de login, así que no lee ~/.zshrc y
# no hereda nada. Todo lo que haga falta se nombra acá.
#
# La contraseña de la base NO se guarda en ningún archivo: se le pide al CLI de
# Railway en el momento, y vive en una variable de entorno que muere con el
# proceso.

export PATH="/opt/homebrew/opt/libpq/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO="$HOME/Library/CloudStorage/GoogleDrive-caribbeandreamstudios@gmail.com/My Drive/Organizacion economica Familiar"
REGISTRO="$HOME/Library/Logs/lucy-respaldo.log"

cd "$REPO" || { echo "$(date '+%F %T') · no encuentro el repo en $REPO" >> "$REGISTRO"; exit 1 }

DATABASE_URL="$(railway variables --service Postgres --kv 2>/dev/null | grep '^DATABASE_PUBLIC_URL=' | cut -d= -f2-)"
if [[ -z "$DATABASE_URL" ]]; then
  # Casi siempre es que la sesión del CLI de Railway venció. Se dice cuál es el
  # arreglo: un registro que solo dice "falló" obliga a investigar de nuevo cada
  # vez que pasa.
  echo "$(date '+%F %T') · sin DATABASE_URL. Probá 'railway login' — la sesión del CLI vence." >> "$REGISTRO"
  exit 1
fi
export DATABASE_URL

echo "$(date '+%F %T') · arranca" >> "$REGISTRO"
python3 db/backup.py >> "$REGISTRO" 2>&1
CODIGO=$?
echo "$(date '+%F %T') · verifico el respaldo" >> "$REGISTRO"
python3 tools/verificar_respaldo.py >> "$REGISTRO" 2>&1
VER=$?
if [[ $VER -ne 0 ]]; then
  # Un respaldo roto que nadie mira es peor que no tener respaldo: apaga la
  # alarma de las 48 horas sin proteger nada. Se marca como fallo de la tarea
  # entera para que quede en el log de launchd y no solo en este archivo.
  echo "$(date '+%F %T') · EL RESPALDO NO PASÓ LA VERIFICACIÓN" >> "$REGISTRO"
  CODIGO=$VER
fi

# La papelera se vacía DESPUÉS del respaldo y SOLO si se verificó. Ese orden es
# lo único que hace aceptable un DELETE real en este proyecto: lo que se
# destruye ya está dentro de una copia buena tomada hace segundos.
if [[ $CODIGO -eq 0 ]]; then
  echo "$(date '+%F %T') · vacío la papelera (>30 días)" >> "$REGISTRO"
  python3 tools/vaciar_papelera.py --aplicar >> "$REGISTRO" 2>&1
else
  echo "$(date '+%F %T') · NO vacío la papelera: el respaldo no está bien" >> "$REGISTRO"
fi

echo "$(date '+%F %T') · termina con código $CODIGO" >> "$REGISTRO"
exit $CODIGO
