#!/usr/bin/env bash
# comun.sh — rutas y helpers que comparten pulso.sh, registrar.sh y transcribir.sh.
# No se ejecuta solo: se hace `source` desde los otros scripts.

BASE="${WSP_BASE:-/home/kutex/WSP Bot}"
DB="${WSP_DB:-$BASE/whatsapp-mcp/whatsapp-bridge/store/messages.db}"
JID="${WSP_JID:-000000000000000@lid}"
ROL_CONTACTO="${WSP_ROL_CONTACTO:-GINGER}"
case "$ROL_CONTACTO" in GINGER|AMIGO) ;; *) echo "ERROR: rol de contacto invalido" >&2; exit 1 ;; esac

# Las rutas de estado se pueden desviar por entorno para probar los scripts sin tocar
# nada real. Antes la unica forma de probar registrar.sh era dejarlo escribir en el
# cursor y el log de produccion, asi que en la practica no se probaba:
#   WSP_CURSOR=/tmp/wsp/cursor.txt WSP_LOGS=/tmp/wsp WSP_TRASPASO=/tmp/wsp/.traspaso \
#     ./bin/registrar.sh --log "NO ME METI — prueba"
CURSOR="${WSP_CURSOR:-$BASE/nucleo/cursor.txt}"
LOCK="${WSP_LOCK:-$BASE/nucleo/.lock}"
LOGS="${WSP_LOGS:-$BASE/logs}"

# Traspaso de pulso.sh a registrar.sh. Aqui queda el timestamp del ultimo mensaje que
# pulso.sh ALCANZO A IMPRIMIR, y de ahi lo lee registrar.sh para mover LAST_VISTO_TS.
#
# Por que un archivo y no consultar la base al cerrar: entre que pulso.sh imprime y el
# modelo termina la vuelta pasan segundos, y en ese hueco entran mensajes que no vio
# nadie. Si registrar.sh diera por visto "el ultimo de la base", se los comeria.
TRASPASO="${WSP_TRASPASO:-$BASE/nucleo/.traspaso}"

# Mapa de numeros de cita a message_id, reescrito entero por pulso.sh en cada vuelta y
# leido por responder.sh. Existe para que el modelo escriba `--citar 3` en vez de copiar
# 32 caracteres hexadecimales a mano: copiar uno mal cita el mensaje equivocado, y eso se
# ve en el telefono de ella sin que aqui salte nada. Mismo razonamiento que `--ts visto`.
#
# Lleva EPOCA= y caduca (ver WSP_CITA_TTL en responder.sh): un numero resuelto contra un
# mapa de hace media hora apunta a otra conversacion.
CITAS="${WSP_CITAS:-$BASE/nucleo/.citas}"

# Endpoint de salud del bridge. Ver /api/health en whatsapp-bridge/main.go.
SALUD_URL="${WSP_SALUD_URL:-http://127.0.0.1:8080/api/health}"

TRANSCRIPCIONES="$BASE/media/transcripciones"
DESCRIPCIONES="$BASE/media/descripciones"

# Marca de agua del bot en la base. El bridge guarda con este sender los mensajes que
# se envian por la API, que es lo que separa a la TULPA del MIKEL real sin depender de
# comparar textos. Ver whatsapp-bridge/main.go, handler /api/send.
SENDER_TULPA="__tulpa__"

# Los timestamps de la base son TEXT con offset fijo de Costa Rica
# ("2026-07-29 20:49:16-06:00"). substr(...,1,19) los deja en la misma forma que usa el
# cursor y ya vienen en hora local: NO se convierten ni se les resta nada.
TS_SQL="substr(timestamp,1,19)"

# Quien escribio cada mensaje, resuelto en SQL para que nadie tenga que deducirlo:
#   GINGER = no es nuestro
#   TULPA  = lo mandamos por la API (sender __tulpa__)
#   MIKEL  = nuestro pero no de la API, o sea escrito por el desde el telefono
# Estaba copiado en cuatro consultas entre pulso.sh y transcribir.sh; aqui se cambia
# en un sitio si algun dia aparece un cuarto rol.
ROL_SQL="case
      when is_from_me = 0 then '$ROL_CONTACTO'
      when sender = '$SENDER_TULPA' then 'TULPA'
      else 'MIKEL'
    end"

morir() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$DB" ] || morir "no existe la base: $DB"
command -v sqlite3 >/dev/null 2>&1 || morir "falta sqlite3"

# Lee un campo del cursor. Uso: leer_cursor LAST_GINGER_TS
leer_cursor() {
  local clave="$1"
  [ -f "$CURSOR" ] || return 0
  sed -n "s/^${clave}=//p" "$CURSOR" | head -1
}

# Separador de campos para las consultas. Tiene que ser un caracter que NO sea espacio
# en blanco: con IFS de tabulador, `read` colapsa los tabs seguidos y un campo vacio en
# medio (media_type, por ejemplo) corre todas las columnas siguientes. \x1f no aparece
# nunca en un chat.
SEP=$'\x1f'

# Consulta a la base, con separador de campo explicito.
consulta() {
  sqlite3 -separator "$SEP" "$DB" "$1"
}

# Pasa un timestamp de la base a segundos. Devuelve 0 si no se entiende, para que las
# comparaciones nunca revienten con un cursor a medio escribir o editado a mano.
a_segundos() {
  date -d "$1" +%s 2>/dev/null || echo 0
}

# --- LOS DOS CURSORES, y por que son dos ---
#
#   LAST_VISTO_TS  — hasta aqui YA MIRE. Avanza SIEMPRE, cada vuelta, se conteste o no.
#   LAST_GINGER_TS — hasta aqui ya le RESPONDI. Avanza solo cuando se responde.
#
# Con uno solo, las vueltas de "NO ME METI" no movian nada y el volcado de pulso.sh
# crecia hasta chocar con el TOPE; a partir de ahi solo salian los mas recientes y los
# viejos se caian de la ventana para no volver. Paso el 2026-08-06: el cursor clavado en
# 13:38:41 durante 115 vueltas y el backlog llegando a 60 con el tope en 40. Si entre los
# que se cayeron hay un mensaje de ella sin responder, no lo ve nadie nunca.
#
# leer_visto devuelve el de "ya mire". Dos salvaguardas:
#   - Un cursor.txt viejo no tiene LAST_VISTO_TS: mientras no exista vale el de Contacto,
#     asi que la primera vuelta tras el cambio se comporta igual que antes de el y no hay
#     migracion que hacer.
#   - Nunca puede quedar por detras del de Contacto. Si alguien edita el archivo a mano y
#     los deja incoherentes, gana el mas nuevo de los dos.
leer_visto() {
  local v g
  v="$(leer_cursor LAST_VISTO_TS)"
  g="$(leer_cursor LAST_GINGER_TS)"
  [ -n "$v" ] || v="$g"
  [ -n "$v" ] || v="1970-01-01 00:00:00"
  if [ -n "$g" ] && [ "$(a_segundos "$g")" -gt "$(a_segundos "$v")" ]; then
    v="$g"
  fi
  printf '%s' "$v"
}

# --- EL TERCER CURSOR ---
#
#   LAST_LEIDO_TS — hasta aqui le MARQUE sus mensajes como leidos (el doble check azul).
#
# Hace falta separado y no se puede colgar de LAST_VISTO_TS por el mudo: estando en /off
# el de "ya mire" sigue avanzando (`--ts visto`), asi que si el marcado dependiera de el,
# todo lo que entre durante un /off de dos horas no se marcaria nunca — y al volver ella
# veria lo nuevo en azul y lo viejo en gris.
#
# El fallback a leer_visto no es cosmetico: sin el, la primera vuelta despues de estrenar
# esto intentaria marcar como leidos los 73 mil mensajes historicos del chat.
leer_leido() {
  local l
  l="$(leer_cursor LAST_LEIDO_TS)"
  [ -n "$l" ] || l="$(leer_visto)"
  printf '%s' "$l"
}
