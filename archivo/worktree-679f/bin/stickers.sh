#!/usr/bin/env bash
# stickers.sh — que stickers se usan de verdad en el chat, y en que momentos.
#
#   ./bin/stickers.sh                  # los que aparecen 3 o mas veces
#   ./bin/stickers.sh 5                # los que aparecen 5 o mas veces
#   ./bin/stickers.sh --aprender       # candidatos a entrar al catalogo, CON el contexto
#                                      # de cada uso, para poder juzgar cuando los manda
#   ./bin/stickers.sh --adoptar <archivo>   # lo copia a media/stickers/ para poder mandarlo
#
# Al catalogo (media/stickers/INDICE.md) van SOLO los recurrentes, nunca todos: en un
# chat de 140 mil mensajes salen demasiados, y una tabla larga deja de servir para elegir
# — el bot la lee entera cada vez que quiere mandar uno.
#
# Se cuenta por NOMBRE DE ARCHIVO y no por message_id porque el nombre sale del hash del
# sticker (ver stickerFilename en whatsapp-bridge/main.go): el mismo sticker mandado
# veinte veces son veinte mensajes distintos pero un solo archivo.
#
# OJO: hasta el 2026-08-06 el bridge DESCARTABA los stickers entrantes, asi que la base
# arranco de cero ese dia. Las cuentas no valen nada hasta que hayan pasado varios dias
# de chat.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

STICKERS="$BASE/media/stickers"
CATALOGO="$STICKERS/INDICE.md"
DIR_MEDIA="$BASE/whatsapp-mcp/whatsapp-bridge/store/$JID"

# Cuantos usos hacen falta para que un sticker deje de ser casualidad. Tres es el mismo
# criterio que el resto de la memoria del bot: una vez es un hecho, dos puede ser
# coincidencia, tres ya es como habla.
MINIMO_DEF="${WSP_STICKER_MINIMO:-3}"

# Cuantos dias cuentan como "lo que uso ahora". El catalogo se ordena por esto y no por
# el acumulado: Mikel cambia de sticker cada pocas semanas, y un contador desde el primer
# dia deja arriba para siempre al que le hacia gracia en agosto. El total se sigue
# mostrando, pero solo como desempate.
VENTANA_DEF="${WSP_STICKER_VENTANA:-7}"

# SOLO los suyos. La consulta contaba los de los tres — los de ella y los que ya mando la
# tulpa — y eso hacia dos cosas mal: mezclaba el gusto de ella con el suyo, y se
# retroalimentaba, porque cada sticker que mandaba la tulpa subia y por subir se mandaba
# mas. La preferencia que se replica es la de Mikel.
SOLO_SUYOS="is_from_me = 1 and coalesce(sender,'') <> '__tulpa__'"

# Marca donde acaban las filas del catalogo. Existe para poder insertar sin adivinar
# donde termina la tabla en un markdown que alguien puede haber editado a mano.
MARCA_FIN="<!-- fin de las filas -->"

# Copia un sticker a media/stickers/ y devuelve la ruta. Se copia y no se enlaza al cache
# del bridge a proposito: ese directorio es temporal (lo llena downloadMedia segun hace
# falta) y nada garantiza que el archivo siga ahi dentro de un mes. Un sticker del
# catalogo tiene que poder mandarse siempre.
adoptar() {
  local arch="$1" origen destino
  origen="$arch"
  [ -f "$origen" ] || origen="$DIR_MEDIA/$arch"
  [ -f "$origen" ] || origen="$STICKERS/$arch"
  [ -f "$origen" ] || return 1
  mkdir -p "$STICKERS"
  destino="$STICKERS/$(basename "$origen")"
  [ "$origen" = "$destino" ] || cp -f "$origen" "$destino"
  chmod 644 "$destino"
  printf '%s' "$destino"
}

# El ultimo sticker que salio en el chat, venga de quien venga. Es a lo que se refiere
# Mikel cuando escribe `/sticker ...` sin decir cual: al que se acaba de mandar.
#
# Sale con el message_id y si esta descargado o no, porque el caso normal es que NO lo
# este: el bridge solo baja media cuando se le pide, asi que un sticker que mando el desde
# el telefono esta en la base pero no en el disco. Sin el id no se puede bajar.
if [ "${1:-}" = "--ultimo" ]; then
  ULT="$(consulta "
    select coalesce(filename,''), coalesce(id,'')
    from messages
    where chat_jid = '$JID' and media_type = 'sticker'
    order by timestamp desc limit 1;
  ")"
  if [ -z "$ULT" ]; then
    echo "ARCHIVO="
    echo "(no ha salido ningun sticker en este chat todavia)"
    exit 0
  fi
  ARCH="${ULT%%${SEP}*}"
  MID="${ULT##*${SEP}}"
  echo "ARCHIVO=$ARCH"
  echo "MESSAGE_ID=$MID"
  if [ -f "$STICKERS/$ARCH" ] || [ -f "$DIR_MEDIA/$ARCH" ]; then
    echo "DESCARGADO=si"
  else
    echo "DESCARGADO=no"
    echo "(bajalo antes de anotarlo: download_media con ese MESSAGE_ID)"
  fi
  exit 0
fi

if [ "${1:-}" = "--adoptar" ]; then
  [ -n "${2:-}" ] || morir "uso: stickers.sh --adoptar sticker_xxxxxxxx.webp"
  adoptar "$2" || morir "no encuentro ${2} (ni en $DIR_MEDIA)"
  echo
  exit 0
fi

# --- guardar: bajar TODOS los stickers del chat y dejarlos a salvo ---
# El cache del bridge (store/<jid>/) es temporal: lo llena downloadMedia segun hace falta
# y nada garantiza que siga ahi. Un sticker que ella mando hace tres meses puede no estar
# ya en los servidores de WhatsApp, y entonces se pierde para siempre. Pesan 60-80 KB:
# guardarlos todos es barato y no se puede deshacer un olvido.
#
# OJO: guardar no es catalogar. Aqui caen todos; al INDICE solo suben los que se usan.
if [ "${1:-}" = "--guardar" ]; then
  mkdir -p "$STICKERS"
  # El id NO se puede dejar al azar. Con 'group by filename' a secas, SQLite elige una fila
  # cualquiera del grupo, y los mensajes SALIENTES guardan en url el literal
  # "https://a.whatsapp.net" — un placeholder de 22 caracteres, sin path. Con eso
  # extractDirectPathFromURL() del bridge no encuentra ".net/", devuelve la basura entera y
  # whatsmeow acaba pidiendo un host que no existe: "lookup a.whatsapp.net: no such host".
  # No es un fallo de red ni del DNS, es la url de ese mensaje.
  #
  # Como el nombre sale del hash del contenido, CUALQUIER mensaje con el mismo filename
  # sirve para bajar el mismo sticker: basta con quedarse con uno que tenga url de verdad.
  # Medido el 2026-08-16: 40 stickers elegian un id malo y 34 tenian alternativa buena.
  PENDIENTES="$(consulta "
    select filename, id from (
      select coalesce(filename,'') as filename, coalesce(id,'') as id
      from messages
      where chat_jid = '$JID' and media_type = 'sticker'
        and coalesce(filename,'') <> '' and coalesce(url,'') like '%.net/%'
    )
    group by filename;
  ")"
  nuevos=0; ya=0; fallos=0
  while IFS="$SEP" read -r arch mid; do
    [ -n "$arch" ] || continue
    if [ -f "$STICKERS/$arch" ]; then ya=$((ya + 1)); continue; fi

    if [ -f "$DIR_MEDIA/$arch" ]; then
      cp -f "$DIR_MEDIA/$arch" "$STICKERS/$arch"
      chmod 644 "$STICKERS/$arch"
      nuevos=$((nuevos + 1))
      continue
    fi

    # No estaba descargado. Se pide al bridge por su API, que es lo mismo que hace
    # download_media del MCP pero sin depender del modelo.
    resp="$(curl -s -m 20 -X POST "${SALUD_URL%/health}/download" \
              -H 'Content-Type: application/json' \
              -d "{\"message_id\":\"$mid\",\"chat_jid\":\"$JID\"}" 2>/dev/null)"
    case "$resp" in
      *'"success":true'*)
        if [ -f "$DIR_MEDIA/$arch" ]; then
          cp -f "$DIR_MEDIA/$arch" "$STICKERS/$arch"
          chmod 644 "$STICKERS/$arch"
          nuevos=$((nuevos + 1))
        else
          fallos=$((fallos + 1))
        fi ;;
      *) fallos=$((fallos + 1)) ;;
    esac
  done <<< "$PENDIENTES"

  echo "guardados: $nuevos nuevos, $ya ya estaban$([ "$fallos" -gt 0 ] && echo ", $fallos no se pudieron bajar")"
  echo "en $STICKERS ($(ls "$STICKERS"/*.webp 2>/dev/null | wc -l) en total)"
  exit 0
fi

# --- ranking: reordenar el catalogo por cuanto se usa cada uno ---
# La tabla se lee de arriba abajo, asi que el orden ES la preferencia: lo que esta arriba
# se manda mas. Se recuenta contra la base y se reordena, para que los que de verdad
# funcionan suban solos y los que casi no salen se hundan.
if [ "${1:-}" = "--ranking" ]; then
  [ -f "$CATALOGO" ] || morir "no existe el catalogo: $CATALOGO"
  grep -qF "$MARCA_FIN" "$CATALOGO" || morir "al catalogo le falta la marca '$MARCA_FIN'"

  # Una sola consulta para los dos contadores: el bucle de antes lanzaba una por fila.
  CUENTAS="$(consulta "
    select filename,
           sum(case when $TS_SQL >= datetime('now','-$VENTANA_DEF days') then 1 else 0 end),
           count(*)
    from messages
    where chat_jid = '$JID' and media_type = 'sticker' and $SOLO_SUYOS
      and coalesce(filename,'') <> ''
    group by filename;")"

  ORDENADAS="$(
    grep '^| `sticker' "$CATALOGO" | while IFS='|' read -r _ col1 col2 col3 _; do
      arch="$(echo "$col1" | tr -d ' `')"
      [ -n "$arch" ] || continue
      desc="$(echo "$col2" | sed 's/^ *//;s/ *$//')"
      cuando="$(echo "$col3" | sed 's/^ *//;s/ *$//')"
      fila="$(printf '%s\n' "$CUENTAS" | grep -F "$arch$SEP" | head -1)"
      semana="$(printf '%s' "$fila" | cut -d"$SEP" -f2)"
      total="$(printf '%s' "$fila" | cut -d"$SEP" -f3)"
      printf '%06d\t%06d\t| `%s` | %s | %s | %s esta semana · %s en total |\n' \
        "${semana:-0}" "${total:-0}" "$arch" "$desc" "$cuando" "${semana:-0}×" "${total:-0}"
    done | sort -rn -k1,1 -k2,2 | cut -f3-
  )"

  TMP="$(mktemp "$CATALOGO.XXXXXX")"
  awk -v filas="$ORDENADAS" -v fin="$MARCA_FIN" '
    /^\| `sticker/ { next }                       # fuera las viejas, se reponen en orden
    index($0, fin) { if (filas != "") print filas; print; next }
    { print }
  ' "$CATALOGO" > "$TMP"
  chmod 644 "$TMP"
  mv -f "$TMP" "$CATALOGO"
  echo "catalogo reordenado por uso de los ultimos $VENTANA_DEF dias (solo los suyos):"
  grep '^| `sticker' "$CATALOGO" | sed 's/|[^|]*|[^|]*| /  /; s/ |$//' | head -20
  exit 0
fi

# --- anotar: añadir o corregir el "cuando" de un sticker, en una sola pasada ---
# Es lo que hay detras de `/sticker <texto>` en el chat. Tiene que poder llamarse dos
# veces con lo mismo sin duplicar la fila: si el sticker ya estaba, se le cambia el
# "cuando" y se deja el resto igual.
if [ "${1:-}" = "--anotar" ]; then
  ARCH="$(basename "${2:-}")"
  CUANDO="${3:-}"
  [ -n "$ARCH" ] && [ -n "$CUANDO" ] || morir "uso: stickers.sh --anotar <archivo.webp> \"<cuando se manda>\""

  RUTA="$(adoptar "$ARCH")" || morir "no encuentro $ARCH. ¿Esta descargado? Mira en $DIR_MEDIA"

  clave="${ARCH%.webp}"
  DESC="(sin describir)"
  [ -s "$DESCRIPCIONES/$clave.txt" ] && DESC="$(tr -d '\n' < "$DESCRIPCIONES/$clave.txt")"

  [ -f "$CATALOGO" ] || morir "no existe el catalogo: $CATALOGO"
  grep -qF "$MARCA_FIN" "$CATALOGO" || morir "al catalogo le falta la marca '$MARCA_FIN'"

  # Los mismos dos contadores del ranking, y con el mismo filtro: solo los que mando el.
  SEMANA="$(consulta "select count(*) from messages
             where chat_jid = '$JID' and media_type = 'sticker' and $SOLO_SUYOS
               and $TS_SQL >= datetime('now','-$VENTANA_DEF days')
               and filename = '${ARCH//\'/\'\'}';")"
  VECES="$(consulta "select count(*) from messages
             where chat_jid = '$JID' and media_type = 'sticker' and $SOLO_SUYOS
               and filename = '${ARCH//\'/\'\'}';")"
  USOS="${SEMANA:-0}× esta semana · ${VECES:-0} en total"

  TMP="$(mktemp "$CATALOGO.XXXXXX")"
  if grep -qF "\`$ARCH\`" "$CATALOGO"; then
    # Ya estaba: se le cambia solo el "cuando", que es la columna que se corrige.
    awk -v arch="$ARCH" -v desc="$DESC" -v cuando="$CUANDO" -v usos="$USOS" '
      index($0, "`" arch "`") { printf "| `%s` | %s | %s | %s |\n", arch, desc, cuando, usos; next }
      { print }
    ' "$CATALOGO" > "$TMP"
    ACCION="corregido"
  else
    awk -v arch="$ARCH" -v desc="$DESC" -v cuando="$CUANDO" -v usos="$USOS" -v fin="$MARCA_FIN" '
      # La fila de relleno se va en cuanto entra la primera de verdad.
      /^\| _\(vacio/ { next }
      index($0, fin) { printf "| `%s` | %s | %s | %s |\n", arch, desc, cuando, usos; print; next }
      { print }
    ' "$CATALOGO" > "$TMP"
    ACCION="añadido"
  fi
  chmod 644 "$TMP"
  mv -f "$TMP" "$CATALOGO"
  echo "$ACCION al catalogo: $ARCH"
  echo "  se ve:  $DESC"
  echo "  cuando: $CUANDO"
  echo "  listo para mandar desde: $RUTA"
  exit 0
fi

# --- olvidar: sacarlo del catalogo para no volver a mandarlo ---
if [ "${1:-}" = "--olvidar" ]; then
  ARCH="$(basename "${2:-}")"
  [ -n "$ARCH" ] || morir "uso: stickers.sh --olvidar <archivo.webp>"
  [ -f "$CATALOGO" ] || morir "no existe el catalogo: $CATALOGO"
  if ! grep -qF "\`$ARCH\`" "$CATALOGO"; then
    echo "$ARCH no estaba en el catalogo, no habia nada que quitar"
    exit 0
  fi
  TMP="$(mktemp "$CATALOGO.XXXXXX")"
  grep -vF "\`$ARCH\`" "$CATALOGO" > "$TMP"
  chmod 644 "$TMP"
  mv -f "$TMP" "$CATALOGO"
  # El archivo se queda en media/stickers/: quitarlo del catalogo es "no lo uses", no
  # "borralo". Si vuelve a hacer falta, se re-anota sin volver a descargarlo.
  echo "quitado del catalogo: $ARCH (el archivo sigue en media/stickers/)"
  exit 0
fi

MODO="lista"
MINIMO="$MINIMO_DEF"
for arg in "$@"; do
  case "$arg" in
    --aprender) MODO="aprender" ;;
    --md)       MODO="md" ;;
    ''|*[!0-9]*) morir "no entiendo el argumento: $arg" ;;
    *)          MINIMO="$arg" ;;
  esac
done

TOTAL="$(consulta "select count(*) from messages where media_type='sticker' and chat_jid='$JID';")"
DISTINTOS="$(consulta "select count(distinct filename) from messages where media_type='sticker' and chat_jid='$JID';")"

if [ "${TOTAL:-0}" -eq 0 ]; then
  echo "Todavia no hay ni un sticker en la base de este chat."
  echo "El soporte se activo el 2026-08-06; antes se descartaban. Dale unos dias."
  exit 0
fi

FILAS="$(consulta "
  select
    count(*) as veces,
    filename,
    case when sum(case when $SOLO_SUYOS then 1 else 0 end) > 0 then 'yo' else 'ella' end as quien,
    min($TS_SQL) as primera,
    max($TS_SQL) as ultima,
    sum(case when $SOLO_SUYOS and $TS_SQL >= datetime('now','-$VENTANA_DEF days')
             then 1 else 0 end) as semana
  from messages
  where media_type = 'sticker' and chat_jid = '$JID' and coalesce(filename,'') <> ''
  group by filename
  having count(*) >= $MINIMO
  order by semana desc, veces desc, filename;
")"

# Los momentos en que se uso un sticker: para cada aparicion, los tres mensajes de antes
# y el de despues. Es lo que hace falta para contestar "¿cuando lo manda?", que es la
# unica columna del catalogo que no sale de un contador.
contexto_de() {
  local arch="${1//\'/\'\'}" usos ts
  usos="$(consulta "
    select $TS_SQL from messages
    where chat_jid = '$JID' and media_type = 'sticker' and filename = '$arch'
    order by timestamp desc limit 4;
  ")"
  while read -r ts; do
    [ -n "$ts" ] || continue
    local esc="${ts//\'/\'\'}"
    echo "    · ${ts:5:11}"
    consulta "
      select * from (
        select $TS_SQL as t, $ROL_SQL as rol,
          case
            when coalesce(media_type,'') <> '' then '<' || media_type || '>'
            else replace(replace(coalesce(content,''), char(10), ' '), char(9), ' ')
          end as cuerpo
        from messages
        where chat_jid = '$JID' and $TS_SQL <= '$esc'
        order by timestamp desc limit 4
      ) order by t asc;
    " | while IFS="$SEP" read -r t rol cuerpo; do
      [ -n "$t" ] || continue
      printf '      %-7s %s\n' "$rol:" "${cuerpo:0:90}"
    done
    # Que dijo ella justo despues: a veces el sticker es la respuesta y a veces es la
    # entrada a lo que viene.
    consulta "
      select $ROL_SQL, replace(coalesce(content,''), char(10), ' ')
      from messages
      where chat_jid = '$JID' and $TS_SQL > '$esc' and coalesce(content,'') <> ''
      order by timestamp asc limit 1;
    " | while IFS="$SEP" read -r rol cuerpo; do
      [ -n "$rol" ] || continue
      printf '      %-7s %s   <-- despues\n' "$rol:" "${cuerpo:0:90}"
    done
  done <<< "$usos"
}

# --- aprender: solo los que AUN NO estan en el catalogo ---
if [ "$MODO" = "aprender" ]; then
  hay=0
  while IFS="$SEP" read -r veces arch quien primera ultima semana; do
    [ -n "$arch" ] || continue
    # Ya catalogado: no se vuelve a proponer.
    [ -f "$CATALOGO" ] && grep -qF "$arch" "$CATALOGO" && continue

    clave="${arch%.webp}"
    desc="(SIN DESCRIBIR — bajalo con download_media y miralo antes de catalogarlo)"
    [ -s "$DESCRIPCIONES/$clave.txt" ] && desc="$(tr -d '\n' < "$DESCRIPCIONES/$clave.txt")"

    hay=1
    echo "=== $arch  ($veces usos, de $quien — ${semana:-0} suyos en los ultimos $VENTANA_DEF dias) ==="
    echo "  se ve: $desc"
    echo "  del ${primera:0:10} al ${ultima:0:10}"
    [ -f "$DIR_MEDIA/$arch" ] && echo "  archivo: $DIR_MEDIA/$arch" \
                             || echo "  archivo: NO DESCARGADO — bajalo antes de adoptarlo"
    echo "  momentos en que lo mando:"
    contexto_de "$arch"
    echo
  done <<< "$FILAS"

  if [ "$hay" -eq 0 ]; then
    echo "Nada nuevo que catalogar: ningun sticker sin catalogar llega a $MINIMO usos."
    echo "($TOTAL stickers en el chat, $DISTINTOS distintos.)"
  fi
  exit 0
fi

# --- lista / md ---
echo "$TOTAL stickers en el chat, $DISTINTOS distintos. Los que salen $MINIMO veces o mas,"
echo "ordenados por los que MAS USA EL en los ultimos $VENTANA_DEF dias:"
echo

if [ -z "$FILAS" ]; then
  echo "(ninguno llega a $MINIMO todavia)"
  exit 0
fi

while IFS="$SEP" read -r veces arch quien primera ultima semana; do
  [ -n "$arch" ] || continue
  clave="${arch%.webp}"
  desc="(sin describir)"
  [ -s "$DESCRIPCIONES/$clave.txt" ] && desc="$(tr -d '\n' < "$DESCRIPCIONES/$clave.txt")"
  en_catalogo=""
  [ -f "$CATALOGO" ] && grep -qF "$arch" "$CATALOGO" && en_catalogo="  [ya en el catalogo]"

  if [ "$MODO" = "md" ]; then
    printf '| `%s` | %s | _(a mano)_ | %s×, de %s |\n' "$arch" "$desc" "$veces" "$quien"
  else
    printf '%3s× (%3s sem)  %-24s  %-5s %s%s\n' "$veces" "${semana:-0}" "$arch" "$quien" "$desc" "$en_catalogo"
  fi
done <<< "$FILAS"
