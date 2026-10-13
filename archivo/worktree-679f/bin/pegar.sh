#!/usr/bin/env bash
# pegar.sh — convierte una imagen cualquiera en un sticker de WhatsApp valido.
#
#   ./bin/pegar.sh gato.png                    # deja media/stickers/gato.webp
#   ./bin/pegar.sh risa.gif                    # animado, tambien vale
#   ./bin/pegar.sh foto.jpg /tmp/prueba.webp   # salida a mano
#
# WhatsApp solo acepta WebP de 512x512. Cualquier otra cosa sale recortada, con marco
# blanco o directamente no se pinta. Esto lo deja en formato: escala sin deformar y
# rellena hasta el cuadrado con transparente, asi que la imagen entra entera y centrada.
#
# Se usa ffmpeg y no cwebp porque cwebp no esta instalado en esta maquina, y ffmpeg ya
# trae libwebp y libwebp_anim de serie.
#
# ===========================================================================
# ESTO NO SE USA NUNCA SOBRE UNA FOTO DE GINGER.
# Lo pidio en serio el 2026-08-04: "me haces un stiker y lloro literalmente.
# Por si acaso". Sus fotos son terreno minado y no es material de broma.
# Tampoco se toca el sticker de Ximena. Ver REGLAS DURAS DE STICKERS en el PROMPT.
# ===========================================================================

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

ORIGEN="${1:-}"
[ -n "$ORIGEN" ] || morir "uso: pegar.sh <imagen> [salida.webp]"
[ -f "$ORIGEN" ] || morir "no existe: $ORIGEN"
command -v ffmpeg  >/dev/null 2>&1 || morir "falta ffmpeg"
command -v ffprobe >/dev/null 2>&1 || morir "falta ffprobe"

STICKERS="$BASE/media/stickers"
if [ -n "${2:-}" ]; then
  SALIDA="$2"
else
  nombre="$(basename "$ORIGEN")"
  SALIDA="$STICKERS/${nombre%.*}.webp"
fi
mkdir -p "$(dirname "$SALIDA")"

# Los limites practicos de WhatsApp. Pasarse no da error al enviar: el sticker llega y
# el otro telefono no lo pinta, que es peor porque no se entera nadie.
MAX_KB_ESTATICO=100
MAX_KB_ANIMADO=500

# Mide un .webp leyendo su cabecera, porque ffprobe NO sabe con los animados: el
# decodificador webp de ffmpeg solo entiende los de un frame y contesta "0,0" al resto.
# Formatos posibles del chunk que va en el byte 12:
#   VP8X (extendido, el de los animados)  ancho-1 en 24 bits LE en el 24, alto-1 en el 27
#   VP8  (con perdida)                    14 bits en el 26 y en el 28
#   VP8L (sin perdida)                    14+14 bits empaquetados desde el 21
# Devuelve "ancho,alto", o vacio si no lo reconoce.
dim_webp() {
  local f="$1" chunk b
  [ -s "$f" ] || return 0
  chunk="$(dd if="$f" bs=1 skip=12 count=4 2>/dev/null)"
  case "$chunk" in
    VP8X)
      b=($(od -An -tu1 -j24 -N6 "$f" 2>/dev/null))
      [ "${#b[@]}" -eq 6 ] || return 0
      echo "$(( (b[0] | b[1]<<8 | b[2]<<16) + 1 )),$(( (b[3] | b[4]<<8 | b[5]<<16) + 1 ))"
      ;;
    "VP8 ")
      b=($(od -An -tu1 -j26 -N4 "$f" 2>/dev/null))
      [ "${#b[@]}" -eq 4 ] || return 0
      echo "$(( (b[0] | b[1]<<8) & 0x3FFF )),$(( (b[2] | b[3]<<8) & 0x3FFF ))"
      ;;
    *)
      ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
        -of csv=p=0 "$f" 2>/dev/null | head -1
      ;;
  esac
}

# Un .webp que ya viene con la medida buena no se toca. Ademas de ahorrarse el trabajo,
# es que NO SE PUEDE: ffmpeg escribe webp animados pero no sabe leerlos, asi que intentar
# reconvertir un sticker que ella mando fallaria con "image data not found". Devolverle
# su propio sticker tiene que funcionar siempre.
if [ "$(echo "${ORIGEN##*.}" | tr 'A-Z' 'a-z')" = "webp" ]; then
  d="$(dim_webp "$ORIGEN")"
  kb=$(( $(stat -c%s "$ORIGEN") / 1024 ))
  if [ "$d" = "512,512" ]; then
    if [ "$SALIDA" != "$ORIGEN" ]; then
      cp -f "$ORIGEN" "$SALIDA"
      chmod 644 "$SALIDA"
    fi
    echo "$SALIDA"
    echo "  512,512 · ${kb} KB · ya era un sticker valido, copiado tal cual"
    exit 0
  fi
fi

# Un solo frame es una imagen; varios, una animacion. -read_intervals corta el sondeo
# para que un gif largo no se lea entero solo para contar.
FRAMES="$(ffprobe -v error -select_streams v:0 -count_frames -read_intervals '%+#2' \
            -show_entries stream=nb_read_frames -of csv=p=0 "$ORIGEN" 2>/dev/null | head -1)"
[ "${FRAMES:-1}" -gt 1 ] 2>/dev/null && ANIMADO=1 || ANIMADO=0

# scale con force_original_aspect_ratio=decrease mete la imagen dentro del cuadro sin
# deformarla, y pad la centra rellenando con transparente. El format=rgba va antes del
# pad o el relleno sale negro en vez de transparente.
FILTRO="scale=512:512:force_original_aspect_ratio=decrease,format=rgba,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=#00000000"

TMP="$(mktemp "$SALIDA.XXXXXX.webp")"
trap 'rm -f "$TMP"' EXIT

if [ "$ANIMADO" -eq 1 ]; then
  # -t 6 y -r 15: WhatsApp corta los stickers animados a unos segundos, y bajar los
  # fotogramas es lo que mas peso quita sin que se note.
  ffmpeg -nostdin -v error -y -i "$ORIGEN" -vf "$FILTRO" \
    -c:v libwebp_anim -loop 0 -t 6 -r 15 -q:v 75 -an "$TMP" 2>/dev/null
  TOPE_KB="$MAX_KB_ANIMADO"
else
  # Se baja la calidad por pasos hasta que entre. Empezar bajo para todos seria peor:
  # la mayoria de los stickers caben de sobra al 80.
  TOPE_KB="$MAX_KB_ESTATICO"
  for q in 80 70 60 50 40; do
    ffmpeg -nostdin -v error -y -i "$ORIGEN" -vf "$FILTRO" \
      -frames:v 1 -c:v libwebp -q:v "$q" -an "$TMP" 2>/dev/null
    [ -s "$TMP" ] || break
    [ "$(( $(stat -c%s "$TMP") / 1024 ))" -le "$TOPE_KB" ] && break
  done
fi

if [ ! -s "$TMP" ]; then
  if [ "$(echo "${ORIGEN##*.}" | tr 'A-Z' 'a-z')" = "webp" ]; then
    morir "ffmpeg no sabe leer webp animados. Si ya es un sticker, usalo tal cual; si no, pasa el gif o el mp4 original."
  fi
  morir "ffmpeg no pudo convertir $ORIGEN"
fi

KB=$(( $(stat -c%s "$TMP") / 1024 ))
DIM="$(dim_webp "$TMP")"

chmod 644 "$TMP"
mv -f "$TMP" "$SALIDA"
trap - EXIT

echo "$SALIDA"
echo "  ${DIM:-?} · ${KB} KB · $([ "$ANIMADO" -eq 1 ] && echo animado || echo estatico)"
if [ "$KB" -gt "$TOPE_KB" ]; then
  echo "  OJO: pasa de ${TOPE_KB} KB. Puede que el otro telefono no lo pinte." >&2
fi
