#!/usr/bin/env bash
# transcribir.sh — arma chats/YYYY-MM-DD.md desde la base, ordenado de verdad.
#
#   ./bin/transcribir.sh              # hoy
#   ./bin/transcribir.sh 2026-07-30   # un dia concreto
#   ./bin/transcribir.sh 2026-07-30 --forzar   # sobrescribe uno escrito a mano
#
# Antes esta transcripcion la iba escribiendo el modelo a mano, añadiendo al final los
# mensajes que acababa de leer. El problema es que mientras escribia seguian entrando
# mensajes con hora anterior, asi que el archivo quedaba desordenado (paso el 2026-07-29:
# tres lineas fuera de secuencia, y el intento de arreglarlas a mano tampoco quedo).
# Generarlo desde la base lo vuelve imposible: el ORDER BY no se equivoca.
#
# Lo unico que la base no sabe es que se ve en una foto o que dice un audio. Eso lo sigue
# poniendo el modelo, en un archivo por message_id:
#   media/transcripciones/<id>.txt  → lo que dice el audio  (lo escribe oir.sh)
#   media/descripciones/<id>.txt    → que se ve en la foto  (lo escribe el modelo)
# Este script los inyecta al generar. Si todavia no existen, deja el marcador con el id
# para que se pueda resolver despues y regenerar.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

DIA="$(date +%F)"
FORZAR=0
for arg in "$@"; do
  case "$arg" in
    --forzar) FORZAR=1 ;;
    *) DIA="$arg" ;;
  esac
done

date -d "$DIA" >/dev/null 2>&1 || morir "fecha invalida: $DIA"
DIA="$(date -d "$DIA" +%F)"

SALIDA="$BASE/chats/$DIA.md"
MARCA="<!-- generado por bin/transcribir.sh -->"

# Las transcripciones viejas se escribieron a mano. No se pisan sin permiso explicito.
if [ -f "$SALIDA" ] && [ "$FORZAR" -eq 0 ] && ! head -3 "$SALIDA" | grep -qF "$MARCA"; then
  morir "$DIA.md se escribio a mano. Revisalo y usa --forzar si de verdad queres regenerarlo."
fi

mkdir -p "$BASE/chats" "$DESCRIPCIONES" "$TRANSCRIPCIONES"

FILAS="$(consulta "
  select
    $TS_SQL,
    $ROL_SQL,
    coalesce(media_type,''),
    coalesce(id,''),
    coalesce(filename,''),
    replace(replace(coalesce(content,''), char(10), ' '), char(9), ' ')
  from messages
  where chat_jid = '$JID' and substr(timestamp,1,10) = '$DIA'
  order by timestamp asc, id asc;
")"

if [ -z "$FILAS" ]; then
  morir "no hay mensajes de $DIA en la base"
fi

TMP="$(mktemp "$SALIDA.XXXXXX")"
{
  echo "# $DIA"
  echo
  echo "$MARCA"
  echo
} > "$TMP"

n=0
sin_resolver=0
while IFS="$SEP" read -r ts rol media id arch texto; do
  [ -n "$ts" ] || continue
  hora="${ts:11:8}"

  case "$media" in
    audio)
      if [ -s "$TRANSCRIPCIONES/$id.txt" ]; then
        cuerpo="<audio: \"$(tr -d '\n' < "$TRANSCRIPCIONES/$id.txt")\">"
      else
        cuerpo="<audio: sin transcribir - Message ID: $id>"
        sin_resolver=$((sin_resolver + 1))
      fi
      ;;
    image)
      if [ -s "$DESCRIPCIONES/$id.txt" ]; then
        cuerpo="<foto: $(tr -d '\n' < "$DESCRIPCIONES/$id.txt")>"
      else
        cuerpo="<foto: sin describir - Message ID: $id>"
        sin_resolver=$((sin_resolver + 1))
      fi
      ;;
    sticker)
      # Los stickers se describen por NOMBRE DE ARCHIVO, no por message_id: el nombre
      # sale del hash del sticker, asi que el mismo sticker mandado veinte veces se
      # describe una sola vez y las otras diecinueve se resuelven de la cache.
      clave="${arch%.webp}"
      if [ -n "$clave" ] && [ -s "$DESCRIPCIONES/$clave.txt" ]; then
        cuerpo="<sticker: $(tr -d '\n' < "$DESCRIPCIONES/$clave.txt")>"
      else
        cuerpo="<sticker: sin describir - Message ID: $id>"
        sin_resolver=$((sin_resolver + 1))
      fi
      ;;
    video)    cuerpo="<video>" ;;          # no se ven, queda vacio a proposito
    document) cuerpo="<documento>" ;;
    "")       cuerpo="$texto" ;;
    *)        cuerpo="<$media>" ;;
  esac

  # "GINGER:" mide 7 y "MIKEL:"/"TULPA:" miden 6, asi que el relleno va DESPUES de los
  # dos puntos y las tres columnas quedan alineadas igual que en FORMATO.md.
  printf '[%s] %-7s %s\n' "$hora" "$rol:" "$cuerpo" >> "$TMP"
  n=$((n + 1))
done <<< "$FILAS"

chmod 644 "$TMP"
mv -f "$TMP" "$SALIDA"

echo "$DIA.md: $n mensajes"
[ "$sin_resolver" -gt 0 ] && echo "  ojo: $sin_resolver media sin resolver (falta la transcripcion o la descripcion). Se puede regenerar despues."
exit 0
