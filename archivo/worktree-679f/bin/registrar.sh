#!/usr/bin/env bash
# registrar.sh — cierra la iteracion: mueve los cursores y escribe el log, de una vez.
#
#   ./bin/registrar.sh --log "NO RESPONDI — sin mensajes nuevos"
#   ./bin/registrar.sh --ts "2026-07-30 21:15:02" --log "RESPONDI — despedida"
#   ./bin/registrar.sh --ts visto --log "MUDO — 3 mensajes, ningun /on"
#
#   --ts   timestamp del ultimo mensaje de Contacto ya ATENDIDO. Sin esto, ese cursor no se
#          mueve (para las vueltas en las que no se respondio nada).
#          La palabra literal `visto` vale por "el ultimo suyo que me mostro pulso.sh":
#          lo resuelve este script leyendo el traspaso, asi no hay que copiar a mano un
#          timestamp — copiarlo mal era una forma silenciosa de perder mensajes.
#   --log  una linea de auditoria. Se le antepone la hora sola.
#
# LAST_VISTO_TS ("hasta aqui ya mire") se mueve SIEMPRE y solo, sin bandera, con lo que
# dejo pulso.sh en el traspaso. Es lo que impide que el volcado crezca sin fin en las
# vueltas en las que no se contesta nada.
#
# LAST_LEIDO_TS ("hasta aqui le marque el doble check azul") va igual: sin bandera, con lo
# que dejo pulso.sh. Solo aparece en el traspaso cuando el acuse de lectura salio de
# verdad, asi que un fallo de red deja el cursor quieto y la vuelta siguiente reintenta.
#
# Ver comun.sh para por que son tres cursores y no uno.
#
# Por que existe este script: antes esto eran de tres a seis ediciones sueltas del
# modelo, y entre una y otra el chat seguia moviendose. Si la iteracion siguiente
# arrancaba en medio, leia un cursor a medio escribir. Aca todas las escrituras van bajo
# el mismo flock y el cursor se reemplaza entero de golpe, asi que o se ve el estado
# viejo o el nuevo, nunca uno partido.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

TS=""
LINEA_LOG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ts)  TS="${2:-}";        shift 2 ;;
    --log) LINEA_LOG="${2:-}"; shift 2 ;;
    *) morir "opcion desconocida: $1" ;;
  esac
done

[ -n "$LINEA_LOG" ] || morir "hace falta --log"

# --- lo que dejo pulso.sh ---
# Si no hay traspaso, esta vuelta no llego a correr pulso.sh (o lo corto el bridge
# caido). Entonces no hay nada que dar por visto y los cursores no se mueven: se escribe
# el log y ya. Es la proteccion estructural contra cerrar una vuelta a ciegas.
V_NUEVO=""
G_TRASPASO=""
L_NUEVO=""
if [ -f "$TRASPASO" ]; then
  V_NUEVO="$(sed -n 's/^VISTO=//p' "$TRASPASO" | head -1)"
  G_TRASPASO="$(sed -n 's/^VISTO_GINGER=//p' "$TRASPASO" | head -1)"
  # LEIDO solo aparece cuando pulso.sh consiguio mandar el acuse de lectura. Si el
  # marcado se salto (mudo) o fallo (el curl, o un bridge sin /api/markread), la clave
  # no esta y el cursor se queda quieto: la vuelta siguiente reintenta los mismos IDs.
  L_NUEVO="$(sed -n 's/^LEIDO=//p' "$TRASPASO" | head -1)"
fi

if [ "$TS" = "visto" ]; then
  [ -n "$G_TRASPASO" ] || morir "--ts visto pero pulso.sh no mostro ningun mensaje de ella"
  TS="$G_TRASPASO"
fi

# Validar el formato antes de escribir nada: un cursor con basura es peor que uno viejo.
if [ -n "$TS" ]; then
  date -d "$TS" >/dev/null 2>&1 || morir "timestamp invalido: $TS"
fi

LOG="$LOGS/$(date +%F).txt"
mkdir -p "$LOGS"
AHORA="$(date +%H:%M)"

# Que lineas se pueden agrupar cuando se repiten. La lista es BLANCA y no negra a
# proposito: una accion nueva que nadie previo se guarda entera por defecto, que es el
# lado seguro del error. RESPONDI, APRENDI, /on, /off y NO PUDE RESPONDER no se agrupan
# jamas — cada una de esas es un hecho distinto aunque el texto coincida.
es_colapsable() {
  case "$1" in
    "MUDO"*|"NO HICE NADA"*|"NO ME METI"*|"NO SAQUE TEMA"*|"NO RESPONDI"*|"BRIDGE CAIDO"*) return 0 ;;
    *) return 1 ;;
  esac
}

exec 9>"$LOCK"
flock 9 || morir "no se pudo tomar el lock"

# --- CURSORES ---
G_FINAL="$(leer_cursor LAST_GINGER_TS)"
[ -n "$TS" ] && G_FINAL="$TS"
V_FINAL="$(leer_visto)"
[ -n "$V_NUEVO" ] && V_FINAL="$V_NUEVO"
L_FINAL="$(leer_cursor LAST_LEIDO_TS)"
[ -n "$L_NUEVO" ] && L_FINAL="$L_NUEVO"

# El cursor se reescribe entero de una sola vez, con tmp+mv porque el rename es atomico:
# la vuelta siguiente ve el estado viejo o el nuevo, nunca uno partido. Las dos claves
# van arriba y TODO lo demas del archivo se conserva en orden — ahi viven
# TULPA_EN_DB_DESDE, ESTRENO_MEDIA y el historico congelado de mensajes del bot.
TMP="$(mktemp "$CURSOR.XXXXXX")"
{
  [ -n "$G_FINAL" ] && printf 'LAST_GINGER_TS=%s\n' "$G_FINAL"
  [ -n "$V_FINAL" ] && printf 'LAST_VISTO_TS=%s\n'  "$V_FINAL"
  [ -n "$L_FINAL" ] && printf 'LAST_LEIDO_TS=%s\n'  "$L_FINAL"
  [ -f "$CURSOR" ] && grep -v -e '^LAST_GINGER_TS=' -e '^LAST_VISTO_TS=' -e '^LAST_LEIDO_TS=' "$CURSOR"
} > "$TMP"
chmod 644 "$TMP"
mv -f "$TMP" "$CURSOR"

# --- LOG, agrupando repeticiones ---
# El log era 76% ruido: una linea por minuto pasara lo que pasara. De 1003 lineas, 516
# eran "MUDO" y 118 "NO ME METI" identicas y seguidas. Asi no se puede auditar nada.
#
# Si esta vuelta repite EXACTAMENTE la misma linea que la anterior, en vez de añadir otra
# se reescribe la ultima como un rango con contador:
#   [15:03-15:36] NO ME METI — sigue en pie la disculpa de Mikel ×34
#
# Se compara la linea ENTERA y no solo la accion: si el motivo cambio, es un hecho nuevo
# y merece su propia linea. El 2026-08-06 el motivo paso de "sigue en pie la disculpa" a
# "tema de ellos" sin cambiar la accion, y esa diferencia es informacion.
ULTIMA=""
[ -s "$LOG" ] && ULTIMA="$(tail -n 1 "$LOG")"

CUERPO_ULT="${ULTIMA#*] }"
MARCA_ULT="${ULTIMA%%]*}"
MARCA_ULT="${MARCA_ULT#\[}"
INI_ULT="${MARCA_ULT%%-*}"
N_ULT=1
if [[ "$CUERPO_ULT" =~ ^(.*)\ ×([0-9]+)$ ]]; then
  CUERPO_ULT="${BASH_REMATCH[1]}"
  N_ULT="${BASH_REMATCH[2]}"
fi

if [ -n "$ULTIMA" ] && [ "$CUERPO_ULT" = "$LINEA_LOG" ] && es_colapsable "$LINEA_LOG"; then
  # Mismo tmp+mv que el cursor: un sed -i a mitad de camino deja el log partido, y esto
  # corre bajo el mismo flock que las demas escrituras.
  TMPLOG="$(mktemp "$LOG.XXXXXX")"
  head -n -1 "$LOG" > "$TMPLOG"
  # Varias vueltas dentro del mismo minuto no llevan rango: "[15:03-15:03]" no dice nada
  # que no diga "[15:03]".
  if [ "$INI_ULT" = "$AHORA" ]; then
    printf '[%s] %s ×%d\n' "$AHORA" "$LINEA_LOG" "$((N_ULT + 1))" >> "$TMPLOG"
  else
    printf '[%s-%s] %s ×%d\n' "$INI_ULT" "$AHORA" "$LINEA_LOG" "$((N_ULT + 1))" >> "$TMPLOG"
  fi
  chmod 644 "$TMPLOG"
  mv -f "$TMPLOG" "$LOG"
else
  printf '[%s] %s\n' "$AHORA" "$LINEA_LOG" >> "$LOG"
fi

flock -u 9
exec 9>&-

echo "registrado en $(basename "$LOG")${TS:+ — respondido hasta $TS}${V_NUEVO:+ — visto hasta $V_NUEVO}${L_NUEVO:+ — leido hasta $L_NUEVO}"
