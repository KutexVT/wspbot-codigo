#!/usr/bin/env bash
# responder.sh — manda un mensaje al chat, citandolo o no.
#
#   ./bin/responder.sh "texto"                      suelto, como siempre
#   ./bin/responder.sh --citar 3 "texto"            citando la linea #3 de esta vuelta
#   ./bin/responder.sh --citar ACDF003F... "texto"  citando por Message ID (media)
#   ./bin/responder.sh --dry --citar 3 "texto"      imprime lo que mandaria y NO manda
#   ./bin/responder.sh --voz "texto"               nota de voz con OmniVoice
#   ./bin/responder.sh --texto "texto"             fuerza texto; por defecto voz ocasional
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
VOZ="auto"
MEDIA=""
VOZ_CONFIG="${WSP_VOZ_CONFIG:-$BASE/media/voz/config.json}"

while [ $# -gt 0 ]; do
  case "$1" in
    --citar) [ $# -ge 2 ] || morir "--citar necesita una referencia"; CITAR="$2"; shift 2 ;;
    --dry)   DRY=1;          shift   ;;
    --voz)   VOZ="voz";      shift   ;;
    --texto) VOZ="texto";    shift   ;;
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

# Validar aca evita una ronda separada del modelo. Los WARN son deliberadamente
# informativos: el propio linter dice que una palabra nueva puede ser un mensaje real
# suyo. Los ERROR y cualquier fallo del linter cierran antes de tocar citas o el bridge.
ESTILO_OUT="$("$BASE/bin/estilo.sh" --json "$TEXTO" 2>&1)"
ESTILO_RC=$?
if ! python3 -c 'import json,sys; raise SystemExit(not isinstance(json.load(sys.stdin), list))' \
  <<< "$ESTILO_OUT" 2>/dev/null; then
  morir "fallo el validador de estilo (${ESTILO_RC}): ${ESTILO_OUT:-sin detalle}"
fi
if [ "$ESTILO_RC" -ge 2 ]; then
  morir "estilo rechazo el mensaje (${ESTILO_RC}): ${ESTILO_OUT:-sin detalle}"
fi
if [ "$ESTILO_RC" -eq 1 ]; then
  printf '%s\n' "$ESTILO_OUT" >&2
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

# La generación es local y opcional. El lock cubre también el HTTP: dos llamadas
# simultáneas no pueden saltarse la pausa consultando ambas el historial anterior.
if [ "$DRY" -eq 0 ] && [ "$VOZ" != "texto" ]; then
  VOZ_LIBRE=1
  if [ -f "$VOZ_CONFIG" ]; then
    if { exec 8>"$VOZ_CONFIG.lock"; } 2>/dev/null; then
      flock -n 8 || VOZ_LIBRE=0
    else
      VOZ_LIBRE=0
    fi
  fi
  if [ "$VOZ_LIBRE" -eq 0 ]; then
    [ "$VOZ" != "voz" ] || morir "no pude tomar el lock de voz; puede haber otro audio en preparación"
  elif ! MEDIA="$(python3 "$BASE/bin/voz.py" --config "$VOZ_CONFIG" --modo "$VOZ" --texto "$TEXTO")"; then
    [ "$VOZ" != "voz" ] || morir "no pude generar la nota de voz"
    MEDIA=""
    echo "WARN: OmniVoice no pudo generar; se enviará el texto" >&2
  fi
fi

# --- armar el JSON ---
CUERPO="$(python3 -c '
import json, sys
d = {"recipient": sys.argv[1], "message": sys.argv[2]}
if sys.argv[3]:
    d["quoted_id"] = sys.argv[3]
if sys.argv[4]:
    d["media_path"] = sys.argv[4]
print(json.dumps(d))
' "$JID" "$TEXTO" "$MID" "$MEDIA")" || morir "no pude armar el JSON"

if [ "$DRY" -eq 1 ]; then
  echo "DRY — no se manda ni genera nada. voz=$VOZ. Iria esto a $ENVIO_URL:"
  echo "$CUERPO"
  exit 0
fi

# MUDO pudo aparecer mientras se resolvia una cita o se armaba el JSON. Esta segunda
# lectura cierra esa ventana justo antes de tocar el bridge.
if [ -f "$ARCH_MUDO" ]; then
  morir "el bot esta MUDO (existe nucleo/MUDO). No se manda nada."
fi

ENVIO_TIMEOUT=10
[ -z "$MEDIA" ] || ENVIO_TIMEOUT=60
RESP="$(curl -s -m "$ENVIO_TIMEOUT" -w $'\n%{http_code}' -X POST "$ENVIO_URL" \
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
FORMATO="texto"
if [ -n "$MEDIA" ]; then
  FORMATO="audio"
  # El bridge guarda el texto como content, pero pulso/transcribir leen esta caché.
  # Un fallo local después del envío no debe provocar que el agente lo repita.
  CACHE_VOZ="${WSP_TRANSCRIPCIONES:-$TRANSCRIPCIONES}"
  if [[ "$MSG_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    if ! mkdir -p "$CACHE_VOZ" || ! cp -- "${MEDIA%.ogg}.txt" "$CACHE_VOZ/$MSG_ID.txt"; then
      echo "WARN: audio enviado pero no pude guardar su transcripción" >&2
    fi
  fi
fi
echo "OK message_id=${MSG_ID:-?} citando=${MID:--} formato=$FORMATO"
