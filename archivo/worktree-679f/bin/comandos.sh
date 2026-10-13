#!/usr/bin/env bash
# comandos.sh — acciones de sistema que Mikel puede disparar desde WhatsApp.
#
# LISTA BLANCA CERRADA. Recibe el NOMBRE de una accion, nunca un comando.
# Lo que venga en el mensaje de WhatsApp jamas se ejecuta: solo sirve para
# elegir una de las ramas de aqui abajo. Si el nombre no esta en la lista,
# no pasa nada.
#
# Todo corre como el usuario normal. NADA de sudo, nada de root.
#
#   ./comandos.sh estado    → uptime, carga, RAM, disco, temperatura
#   ./comandos.sh bot       → si el bridge responde y las ultimas del log
#   ./comandos.sh updates   → paquetes con actualizacion pendiente
#   ./comandos.sh lista     → que acciones existen
#
# Para añadir una accion nueva: otra rama en el case, con el comando escrito
# aqui dentro. No se acepta nada por parametro.

set -uo pipefail

BASE="/home/kutex/WSP Bot"

case "${1:-}" in

  estado)
    echo "UPTIME: $(uptime -p 2>/dev/null || uptime)"
    echo "CARGA:  $(cut -d' ' -f1-3 /proc/loadavg)"
    echo "RAM:    $(free -h | awk '/^Mem:/ {print $3 " usados de " $2}')"
    echo "DISCO:  $(df -h / | awk 'NR==2 {print $3 " usados de " $2 " (" $5 ")"}')"
    t=$(sensors 2>/dev/null | awk '/Tctl|Package id 0/ {print $NF; exit}')
    [ -n "$t" ] && echo "TEMP:   $t"
    ;;

  bot)
    if curl -s -m 3 -o /dev/null http://localhost:8080 2>/dev/null; then
      echo "BRIDGE: vivo (puerto 8080 responde)"
    else
      echo "BRIDGE: CAIDO (el puerto 8080 no responde)"
    fi
    hoy="$BASE/logs/$(date '+%Y-%m-%d').txt"
    if [ -s "$hoy" ]; then
      echo "--- ultimas del log de hoy ---"
      tail -5 "$hoy"
    else
      echo "(sin log hoy)"
    fi
    ;;

  updates)
    n=$(checkupdates 2>/dev/null | wc -l)
    if [ "$n" -eq 0 ]; then
      echo "Nada que actualizar"
    else
      echo "$n paquetes con update:"
      checkupdates 2>/dev/null | head -15
      [ "$n" -gt 15 ] && echo "... y $((n - 15)) mas"
    fi
    echo
    echo "(solo consulta — instalar necesita sudo y lo corres vos)"
    ;;

  bridge)
    # reinicia el whatsapp-client. Es proceso del usuario, no lleva sudo.
    pkill -f 'whatsapp-bridge/whatsapp-client' 2>/dev/null && echo "matado el viejo"
    cd "$BASE/whatsapp-mcp/whatsapp-bridge" || exit 1
    nohup ./whatsapp-client >/dev/null 2>&1 &
    sleep 3
    if curl -s -m 3 -o /dev/null http://localhost:8080 2>/dev/null; then
      echo "BRIDGE: levantado, el 8080 responde"
    else
      echo "BRIDGE: lo lance pero el 8080 aun no responde. Puede pedir QR"
    fi
    ;;

  repos)
    for d in "$HOME/repos"/*/; do
      [ -d "$d/.git" ] || continue
      printf '%s: ' "$(basename "$d")"
      git -C "$d" fetch --quiet 2>/dev/null
      git -C "$d" status -sb 2>/dev/null | head -1
    done
    ;;

  espacio)
    echo "--- discos ---"
    df -h --output=target,used,size,pcent -x tmpfs -x devtmpfs 2>/dev/null | head -8
    echo "--- lo mas gordo en el home ---"
    du -sh "$HOME"/*/ 2>/dev/null | sort -rh | head -8
    ;;

  lista)
    echo "estado   — uptime, carga, RAM, disco, temperatura"
    echo "bot      — si el bridge responde y las ultimas del log"
    echo "updates  — paquetes con actualizacion pendiente (solo mira)"
    echo "bridge   — reinicia el whatsapp-client (solo Mikel)"
    echo "repos    — fetch y estado de cada repo de ~/repos (solo Mikel)"
    echo "espacio  — discos y las carpetas mas gordas del home"
    ;;

  *)
    echo "ERROR: '${1:-}' no esta en la lista blanca." >&2
    echo "Acciones validas: estado, bot, updates, bridge, repos, espacio, lista" >&2
    exit 1
    ;;

esac
