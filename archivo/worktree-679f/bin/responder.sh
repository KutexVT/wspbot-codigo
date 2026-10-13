#!/usr/bin/env bash
# responder.sh — manda un mensaje al chat, citandolo o no.
#
#   ./bin/responder.sh "texto"                      suelto, como siempre
#   ./bin/responder.sh --citar 3 "texto"            citando la linea #3 de esta vuelta
#   ./bin/responder.sh --citar ACDF003F... "texto"  citando por Message ID (media)
#   ./bin/responder.sh --dry --citar 3 "texto"      imprime lo que mandaria y NO manda
#
# Por que existe: `mcp__whatsapp__send_message` no sabe citar y darle ese parametro
# obligaria a tocar el Python del MCP, lo que fuerza reiniciar Claude Code — y eso mata
# la sesion del propio bot. Aqui se le pega directo al bridge por HTTP, que es el mismo
# camino que acaba usando el MCP, asi que el mensaje se guarda en la base igual (como
# __tulpa__) y no hay dos formas distintas de que salga un mensaje.
#
# El numero de cita lo reparte pulso.sh y vive en nucleo/.citas. Es de UNA vuelta: un #3
# resuelto contra un mapa viejo cita el mensaje equivocado, y eso se ve en el telefono de
# ella sin que aqui salte nada. De ahi el TTL de abajo.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

ENVIO_URL="${WSP_ENVIO_URL:-http://127.0.0.1:8080/api/send}"

# Cuanto vale un mapa de citas. pulso.sh corre cada minuto, asi que 15 min es de sobra
# para una vuelta lenta y corto de mas para que el mapa se refiera a otra conversacion.
CITA_TTL="${WSP_CITA_TTL:-900}"

ARCH_MUDO="${WSP_MUDO:-$BASE/nucleo/MUDO}"

CITAR=""
DRY=0
TEXTO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --citar) CITAR="${2:-}"; shift 2 ;;
    --dry)   DRY=1;          shift   ;;
    --)      shift; TEXTO="${1:-}"; break ;;
    -*)      morir "opcion desconocida: $1" ;;
    *)       TEXTO="$1";     shift   ;;
  esac
done

[ -n "$TEXTO" ] || morir "hace falta el texto del mensaje"

# --- el mudo, como proteccion estructural y no como regla que recordar ---
# Es el mismo criterio que el corte por bridge caido de pulso.sh: si estar callado
# dependiera de que el modelo se acuerde, tarde o temprano no se acuerda. Hablar cuando
# tocaba callar no se puede deshacer.
if [ -f "$ARCH_MUDO" ]; then
  morir "el bot esta MUDO (existe nucleo/MUDO). No se manda nada."
fi

# --- resolver el --citar ---
MID=""
if [ -n "$CITAR" ]; then
  if [[ "$CITAR" =~ ^[0-9]{1,4}$ ]]; then
    # Es un numero de cita: hay que resolverlo contra el mapa de esta vuelta.
    [ -f "$CITAS" ] || morir "no hay mapa de citas ($CITAS). Corre pulso.sh antes."

    EPOCA="$(sed -n 's/^EPOCA=//p' "$CITAS" | head -1)"
    [ -n "$EPOCA" ] || morir "el mapa de citas no tiene EPOCA: no me fio, no cito"
    EDAD=$(( $(date +%s) - EPOCA ))
    if [ "$EDAD" -gt "$CITA_TTL" ]; then
      morir "el mapa de citas tiene ${EDAD}s (tope ${CITA_TTL}s). Corre pulso.sh otra vez: el #$CITAR ya no apunta a lo mismo."
    fi

    MID="$(awk -F'\t' -v n="$CITAR" '$1 == n { print $2; exit }' "$CITAS")"
    [ -n "$MID" ] || morir "no hay ninguna linea #$CITAR en el volcado de esta vuelta"
  else
    # Es un Message ID tal cual, del PASO 1.
    MID="$CITAR"
  fi

  # Ultima red: el bridge tambien lo comprueba y devuelve 404, pero fallar aqui da un
  # mensaje que se entiende y no gasta una llamada HTTP.
  EXISTE="$(consulta "select count(*) from messages where id = '${MID//\'/\'\'}' and chat_jid = '$JID';")"
  [ "$EXISTE" = "1" ] || morir "el mensaje $MID no esta en la base para este chat: no lo puedo citar"
fi

# --- armar el JSON ---
CUERPO="$(python3 -c '
import json, sys
d = {"recipient": sys.argv[1], "message": sys.argv[2]}
if sys.argv[3]:
    d["quoted_id"] = sys.argv[3]
print(json.dumps(d))
' "$JID" "$TEXTO" "$MID")" || morir "no pude armar el JSON"

if [ "$DRY" -eq 1 ]; then
  echo "DRY — no se manda nada. Iria esto a $ENVIO_URL:"
  echo "$CUERPO"
  exit 0
fi

RESP="$(curl -s -m 10 -w $'\n%{http_code}' -X POST "$ENVIO_URL" \
  -H 'Content-Type: application/json' -d "$CUERPO" 2>/dev/null)"
COD="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"

if [ "$COD" != "200" ]; then
  morir "el bridge contesto HTTP ${COD:-sin respuesta}: $BODY"
fi

case "$BODY" in
  *'"success":true'*) ;;
  *) morir "el envio fallo: $BODY" ;;
esac

MSG_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("message_id",""))' <<< "$BODY" 2>/dev/null)"
echo "OK message_id=${MSG_ID:-?} citando=${MID:--}"
