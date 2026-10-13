#!/usr/bin/env bash
# buscar.sh — busca un termino en TODA la memoria de la tulpa.
#
#   ./buscar.sh matcha
#   ./buscar.sh "tipo de sangre"
#   ./buscar.sh alnst
#
# Ignora mayusculas y acentos: "musica" encuentra "música", "cumpleanos" encuentra
# "cumpleaños". Busca en orden de vigencia: primero lo que rige ahora, al final el
# historial crudo. Cada resultado sale como  archivo:linea: texto original.

set -uo pipefail

BASE="/home/kutex/WSP Bot"
# La ficha de Contacto y el perfil de estilo vivian en la memoria de Claude; desde el
# 2026-08-24 viven aqui, en el repo, y alla quedan symlinks. Un solo archivo real.
FICHA_GINGER="$BASE/memoria/contacto.md"
PERFIL_ESTILO="$BASE/estilo/user_messaging_style.md"

if [ $# -eq 0 ]; then
  echo "uso: buscar.sh <termino>" >&2
  exit 1
fi
PATRON="$*"

# Quita acentos de un texto para que la busqueda no dependa de ellos.
sin_acentos() {
  sed 'y/áéíóúüñÁÉÍÓÚÜÑ/aeiouunAEIOUUN/'
}
PATRON_N="$(printf '%s' "$PATRON" | sin_acentos)"

# Acorta la ruta para que el resultado se lea. Los archivos de la memoria permanente
# viven fuera de BASE, asi que llevan su propio prefijo.
ruta_corta() {
  local p="$1"
  case "$p" in
    "$BASE"/*)      printf '%s' "${p#$BASE/}" ;;
    "$CLAUDE_MEM"/*) printf 'memoria-claude/%s' "${p#$CLAUDE_MEM/}" ;;
    *)              printf '%s' "$p" ;;
  esac
}

# Busca en un archivo comparando sin acentos, pero imprimiendo la linea original.
# -F: el termino es texto literal, no una expresion regular. Sin esto, buscar algo con
# parentesis o interrogacion daba error o resultados falsos.
buscar_en() {
  local archivo="$1"
  [ -f "$archivo" ] || return 0
  local lineas
  lineas="$(sin_acentos < "$archivo" | grep -inF -- "$PATRON_N" | cut -d: -f1)"
  [ -n "$lineas" ] || return 0
  local n
  while read -r n; do
    [ -n "$n" ] || continue
    printf '%s:%s: %s\n' "$(ruta_corta "$archivo")" "$n" \
      "$(sed -n "${n}p" "$archivo" | sed 's/^[[:space:]]*//')"
  done <<< "$lineas"
}

seccion() {
  local titulo="$1"; shift
  local salida=""
  for f in "$@"; do
    salida+="$(buscar_en "$f")"$'\n'
  done
  salida="$(printf '%s' "$salida" | grep -v '^$' || true)"
  [ -n "$salida" ] || return 0
  printf '\n=== %s ===\n%s\n' "$titulo" "$salida"
}

echo "buscando: $PATRON"

# 1. Lo que rige ahora mismo.
seccion "VIGENTE (nucleo)" "$BASE/nucleo/siempre.md" "$BASE/nucleo/cursor.txt"
seccion "VIGENTE (memoria actual)" "$BASE"/memoria/actual/*.md

# 2. Memoria permanente curada por Mikel.
seccion "PERMANENTE (ficha de ella + perfil de estilo)" "$FICHA_GINGER" "$PERFIL_ESTILO"

# 3. Historial semanal, de la semana mas reciente hacia atras.
mapfile -t semanas < <(ls -1r "$BASE"/memoria/semanas/*.md 2>/dev/null || true)
[ ${#semanas[@]} -gt 0 ] && seccion "SEMANAS (reciente → antiguo)" "${semanas[@]}"

# 4. Transcripciones crudas: la fuente de verdad, pero la mas ruidosa.
mapfile -t chats < <(ls -1r "$BASE"/chats/*.md 2>/dev/null | grep -v FORMATO || true)
[ ${#chats[@]} -gt 0 ] && seccion "CHATS (reciente → antiguo)" "${chats[@]}"

# 5. Notas de voz transcritas. El nombre del archivo es el message_id, que sirve para
#    volver a bajar el audio original con download_media si hiciera falta.
mapfile -t audios < <(ls -1t "$BASE"/media/transcripciones/*.txt 2>/dev/null || true)
[ ${#audios[@]} -gt 0 ] && seccion "AUDIOS TRANSCRITOS (reciente → antiguo)" "${audios[@]}"

# 6. Auditoria del bot. Solo si se busca que hizo el, no que dijo ella.
mapfile -t logs < <(ls -1r "$BASE"/logs/*.txt 2>/dev/null || true)
[ ${#logs[@]} -gt 0 ] && seccion "LOGS (reciente → antiguo)" "${logs[@]}"

# 7. EL CHAT ENTERO, desde la base. Los chats/*.md solo cubren los dias con el bot
#    encendido; la base tiene los 130 mil mensajes de siempre. Es lo ultimo porque es
#    lo mas ruidoso, pero es donde de verdad esta todo lo que se dijeron alguna vez.
DB="$BASE/whatsapp-mcp/whatsapp-bridge/store/messages.db"
JID="${WSP_JID:-000000000000000@lid}"
TOPE_DB="${WSP_TOPE_DB:-25}"

if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  PATRON_SQL="${PATRON//\'/\'\'}"
  hits="$(sqlite3 "$DB" "
    select substr(timestamp,1,16) || '  ' ||
           case when is_from_me = 0 then 'GINGER' when sender='__tulpa__' then 'TULPA '
                else 'MIKEL ' end || '  ' ||
           replace(replace(content, char(10), ' '), char(9), ' ')
    from messages
    where chat_jid = '$JID' and content is not null
      and lower(content) like lower('%$PATRON_SQL%')
    order by timestamp desc limit $TOPE_DB;
  " 2>/dev/null)"
  if [ -n "$hits" ]; then
    total_db="$(sqlite3 "$DB" "
      select count(*) from messages
      where chat_jid = '$JID' and content is not null
        and lower(content) like lower('%$PATRON_SQL%');" 2>/dev/null)"
    printf '\n=== EL CHAT ENTERO (%s coincidencias, se muestran las %s mas recientes) ===\n%s\n' \
      "$total_db" "$TOPE_DB" "$hits"
  fi
fi

echo
echo "--- fin. si no aparecio nada, proba con una palabra mas corta o la raiz del termino ---"
