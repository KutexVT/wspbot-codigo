#!/usr/bin/env bash
# pulso.sh — ¿hay algo que hacer? Todo el PASO 0 y el PASO 1 en una sola llamada.
#
#   ./bin/pulso.sh
#
# La mayoria de las vueltas del loop no tienen nada que hacer. Antes cada una de esas
# vueltas costaba tres lecturas de archivo mas una consulta al MCP para terminar en
# "NO HICE NADA". Este script contesta lo mismo en una llamada y unos 50 tokens.
#
# NO decide por el bot: da los hechos y dice que reglas del PASO 3 estan en juego.
# Quien elige (y sobre todo, si un mensaje es una despedida) sigue siendo el modelo.
#
# Estados posibles:
#   NADA          — sin mensajes nuevos y sin silencio suficiente. Se corta aqui.
#   NUEVOS        — hay mensajes de Contacto sin responder. Aplican las reglas 2 o 3.
#   REVISAR       — no hay de ella sin responder, pero hay algo que mirar: mensajes
#                   nuevos de Mikel (regla 6) o silencio largo (reglas 4 o 5).
#   BRIDGE_CAIDO  — el bridge no responde. La base esta congelada y todo lo que se
#                   pudiera decir aqui seria mentira. Se corta sin mirar nada mas.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

# Minutos de silencio a partir de los cuales vale la pena plantearse sacar tema (regla 4).
UMBRAL_SILENCIO="${WSP_UMBRAL_SILENCIO:-30}"

# Tope de mensajes nuevos a mostrar. Si el cursor quedo muy atras (el bot estuvo apagado
# un dia), sin tope esto escupe miles de lineas y entierra la iteracion.
#
# Truncar ya no pierde nada: lo de ELLA que se caiga por aqui sigue saliendo en la
# seccion de pendientes hasta que se responda, porque esa se calcula contra el cursor de
# Contacto y no contra el de "ya mire".
TOPE="${WSP_TOPE:-40}"

# Tope de la seccion de pendientes. Se ordena de mas viejo a mas nuevo a proposito: si
# hay que cortar, se corta por el final, que es lo que ya se vio hace un momento.
TOPE_PEND="${WSP_TOPE_PEND:-60}"

# Arranque en frio: cuantos mensajes de contexto leer la primera vuelta de cada encendido,
# y a partir de cuantos minutos sin correr se considera que el bot se acaba de prender.
CONTEXTO_FRIO="${WSP_CONTEXTO_FRIO:-100}"
FRIO_MIN="${WSP_FRIO_MIN:-10}"
MARCA_VUELTA="${WSP_MARCA_VUELTA:-$BASE/nucleo/.ultima_vuelta}"

# Cada cuanto se repite el aviso de escritorio con el bridge caido. Avisar cada minuto
# seria inusable: mako deja las criticas fijas hasta que se cierran a mano.
AVISO_CADA_MIN="${WSP_AVISO_CADA_MIN:-15}"
MARCA_AVISO="${WSP_MARCA_AVISO:-$BASE/nucleo/.aviso_bridge}"

# El interruptor (/on y /off del PROMPT) y la marca de "volvi y todavia no he saludado".
ARCH_MUDO="${WSP_MUDO:-$BASE/nucleo/MUDO}"
MARCA_VOLVI="${WSP_MARCA_VOLVI:-$BASE/nucleo/.volvi_pendiente}"

# EL DOBLE CHECK AZUL. Cada vuelta se le manda un acuse de lectura de lo suyo que aun no
# se habia marcado, para que sus mensajes no se queden en un check gris para siempre.
#
# Va aqui y no en registrar.sh por dos razones estructurales: aqui ya se corto en seco si
# el bridge esta caido, y aqui ya se sabe si el mudo esta puesto. Ademas el momento del
# acuse es justo cuando el bot mira, que es la verdad.
#
# Quien PERSISTE el avance sigue siendo registrar.sh, bajo su flock: este script propone
# en el traspaso, registrar confirma en el cursor. Igual que los otros dos cursores.
#
# Tope de IDs por acuse: van todos en un mismo nodo, asi que sin tope un /off largo
# armaria una stanza enorme. El bridge lo vuelve a capar a 200 por su cuenta.
MARCAR_LEIDO="${WSP_MARCAR_LEIDO:-1}"
TOPE_LEIDO="${WSP_TOPE_LEIDO:-200}"
MARCAR_URL="${WSP_MARCAR_URL:-http://127.0.0.1:8080/api/markread}"

# ---------------------------------------------------------------------------
# SALUD DEL BRIDGE — antes que ninguna otra cosa
# ---------------------------------------------------------------------------
# Se pregunta al bridge por su endpoint y no por la base, porque la base no sabe
# distinguir "no ha escrito nadie" de "no me estan llegando los mensajes". De noche hay
# huecos legitimos de horas en los que no escribe nadie en ninguno de los 931 chats: con
# un umbral de frescura, el bot daria por caido el bridge todas las madrugadas.
#
# Tampoco basta con que el puerto este abierto: el proceso puede estar vivo, escuchando y
# desconectado de WhatsApp. Es justo lo que pasa despues de un "replaced stream", cuando
# otro cliente se lleva la sesion.
#
# -m 2 no es opcional: sin timeout, un puerto que acepta la conexion y no contesta cuelga
# la vuelta entera.
#
# El 404 NO es un fallo, es el binario viejo — el que no tiene /api/health todavia pero
# esta vivo y sirviendo. Asi este script se puede desplegar antes de recompilar el Go.
SALUD_CUERPO=""
salud_bridge() {
  local resp cod cuerpo
  resp="$(curl -s -m 2 -w $'\n%{http_code}' "$SALUD_URL" 2>/dev/null)"
  cod="${resp##*$'\n'}"
  cuerpo="${resp%$'\n'*}"
  SALUD_CUERPO="$cuerpo"
  case "$cod" in
    200)
      case "$cuerpo" in
        *'"logged_in":false'*) echo "CAIDO la sesion esta cerrada: hay que reescanear el QR (reiniciar no sirve)" ;;
        *'"connected":false'*) echo "CAIDO el bridge corre pero esta desconectado de WhatsApp" ;;
        *)                     echo "VIVO" ;;
      esac ;;
    404)    echo "VIVO" ;;
    000|"") echo "CAIDO el puerto 8080 no responde: el bridge no esta corriendo" ;;
    *)      echo "CAIDO el bridge contesta HTTP $cod" ;;
  esac
}

SALUD="$(salud_bridge)"

if [ "${SALUD%% *}" = "CAIDO" ]; then
  MOTIVO="${SALUD#CAIDO }"

  # Sin systemd detras, nadie va a levantar el bridge solo. El aviso lo manda el script y
  # no el modelo, para que salga aunque la vuelta se muera despues de esta linea.
  avisar=1
  if [ -f "$MARCA_AVISO" ]; then
    edad=$(( ( $(date +%s) - $(stat -c %Y "$MARCA_AVISO" 2>/dev/null || echo 0) ) / 60 ))
    [ "$edad" -lt "$AVISO_CADA_MIN" ] && avisar=0
  fi
  if [ "$avisar" -eq 1 ] && command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "WSP Bot — bridge caido" \
      "$MOTIVO"$'\n''Levantalo con:  wspbot' 2>/dev/null
    touch "$MARCA_AVISO"
  fi

  # Corte seco. Con el bridge caido la base esta congelada y TODO lo que diria este
  # script es mentira: "no hay mensajes nuevos" no es un hecho, es que no llegan. Es el
  # fallo que mas veces se repitio en este sistema (transcripciones a medias, dias
  # procesados incompletos), y hasta ahora salia en el log como "NO HICE NADA".
  #
  # Aqui NO se toca la marca de vuelta ni se escribe el traspaso, a proposito:
  #   - sin tocar la marca, al volver el bridge la vuelta cuenta como ARRANQUE FRIO y se
  #     vuelca el contexto que se perdio mientras tanto;
  #   - sin traspaso, aunque el modelo llame a registrar.sh, los cursores no se mueven.
  # La proteccion es estructural y no una regla que se pueda olvidar.
  echo "ESTADO: BRIDGE_CAIDO"
  echo "MOTIVO: $MOTIVO"
  echo "ULTIMO_EN_LA_BASE: $(consulta "select max($TS_SQL) from messages;")"
  echo "REGLAS: ninguna. No escribas al chat, no muevas cursores, no transcribas."
  exit 0
fi

rm -f "$MARCA_AVISO"

# ---------------------------------------------------------------------------
# EL INTERRUPTOR — vencimiento del /off
# ---------------------------------------------------------------------------
# `/off` a secas calla al bot hasta que llegue un `/on`, y eso no cambia. Pero `/off 10
# minutos` tambien tiene que poder cumplirse, y el modelo no puede encargarse: entre vuelta
# y vuelta pierde el contexto, asi que una hora de expiracion que solo viva en su cabeza no
# llega vivo al minuto siguiente. La aritmetica de fechas se hace aqui; el que parsea la
# duracion en castellano ("media hora", "hasta las 9") sigue siendo el modelo, que escribe
# el resultado ya resuelto como `HASTA: YYYY-MM-DD HH:MM:SS` dentro del archivo.
#
# Un MUDO vacio es un mudo indefinido: es como funcionaba esto antes y se queda igual.
#
# Va aqui a proposito, despues del corte del bridge: con la base congelada no se toca nada,
# ni siquiera el interruptor. Y antes de calcular ARRANQUE y ESTADO, porque si el mudo
# acaba de vencer esta vuelta ya cuenta como vuelta trabajando.
MUDO_LINEA="no"
if [ -f "$ARCH_MUDO" ]; then
  MUDO_HASTA="$(sed -n 's/^HASTA:[[:space:]]*//p' "$ARCH_MUDO" | head -1)"
  MUDO_SEG="$(a_segundos "$MUDO_HASTA")"

  if [ -z "$MUDO_HASTA" ]; then
    MUDO_LINEA="si — indefinido (solo /on lo levanta)"

  # a_segundos devuelve 0 cuando no entiende la fecha. Ahi NO se vence: ante un archivo a
  # medio escribir se prefiere seguir callado, que es el error recuperable de los dos —
  # sobra con que Mikel mande /on. Hablar cuando tocaba callar no se puede deshacer.
  elif [ "$MUDO_SEG" -eq 0 ]; then
    MUDO_LINEA="si — indefinido (el HASTA no se entiende: '$MUDO_HASTA')"

  elif [ "$MUDO_SEG" -le "$(date +%s)" ]; then
    rm -f "$ARCH_MUDO"
    # La marca es lo que hace que el saludo sobreviva. Si aqui solo se borrara el MUDO, la
    # vuelta siguiente no tendria como saber que venia de un silencio, y si esta vuelta se
    # muere a la mitad el saludo se perderia igual. Mientras la marca exista el aviso se
    # reintenta solo, vuelta tras vuelta, hasta que el modelo salude y la borre.
    touch "$MARCA_VOLVI"

  else
    MUDO_LINEA="si — hasta $MUDO_HASTA (faltan $(( (MUDO_SEG - $(date +%s)) / 60 )) min)"
  fi
fi

# Se comprueba aparte del bloque de arriba, y no en su `else`, para que el aviso siga
# saliendo en las vueltas siguientes aunque el MUDO ya no exista.
[ -f "$MARCA_VOLVI" ] && \
  MUDO_LINEA="acaba de expirar — toca saludar en el chat y borrar nucleo/.volvi_pendiente"

# ---------------------------------------------------------------------------
# ARRANQUE — solo se llega aqui con el bridge vivo
# ---------------------------------------------------------------------------
ARRANQUE="CALIENTE"
if [ ! -f "$MARCA_VUELTA" ]; then
  ARRANQUE="FRIO"
else
  edad_min=$(( ( $(date +%s) - $(stat -c %Y "$MARCA_VUELTA" 2>/dev/null || echo 0) ) / 60 ))
  [ "$edad_min" -ge "$FRIO_MIN" ] && ARRANQUE="FRIO"
fi
touch "$MARCA_VUELTA"

LAST_VISTO="$(leer_visto)"
LAST_GINGER="$(leer_cursor LAST_GINGER_TS)"
[ -n "$LAST_GINGER" ] || LAST_GINGER="1970-01-01 00:00:00"

ESC_VISTO="${LAST_VISTO//\'/\'\'}"
ESC_GINGER="${LAST_GINGER//\'/\'\'}"

# --- VENTANA 1: lo que entro desde la ultima vuelta ---
# Como LAST_VISTO_TS avanza siempre, en la vuelta normal esto son media docena de lineas
# y no un backlog que crece solo.
TOTAL_NUEVOS="$(consulta "
  select count(*) from messages
  where chat_jid = '$JID' and $TS_SQL > '$ESC_VISTO';
")"

# Se piden los ultimos TOPE y se reordenan ascendente, para quedarse con los mas
# recientes cuando el cursor viene muy atrasado.
FILAS="$(consulta "
  select * from (
    select
      $TS_SQL as ts,
      $ROL_SQL as rol,
      coalesce(media_type,'') as media,
      coalesce(id,'') as mid,
      coalesce(filename,'') as arch,
      replace(replace(coalesce(content,''), char(10), ' '), char(9), ' ') as texto
    from messages
    where chat_jid = '$JID' and $TS_SQL > '$ESC_VISTO'
    order by timestamp desc limit $TOPE
  ) order by ts asc;
")"

# --- VENTANA 2: lo de ELLA que sigue sin responder de vueltas anteriores ---
# Esta es la red de seguridad del sistema entero. Mientras el cursor de Contacto no los
# pase, sus mensajes siguen saliendo aqui aunque la ventana 1 ya haya avanzado por
# encima. Es lo que impide que un mensaje suyo desaparezca en silencio, que es lo que
# pasaba cuando el unico cursor se quedaba clavado y el TOPE se comia lo viejo.
#
# Sale con las mismas columnas que la ventana 1 (el rol es siempre GINGER, pero va igual)
# para que las dos se pinten con la misma funcion y no haya dos formatos que cuadrar.
PENDIENTES="$(consulta "
  select
    $TS_SQL as ts,
    '$ROL_CONTACTO' as rol,
    coalesce(media_type,'') as media,
    coalesce(id,'') as mid,
    coalesce(filename,'') as arch,
    replace(replace(coalesce(content,''), char(10), ' '), char(9), ' ') as texto
  from messages
  where chat_jid = '$JID' and is_from_me = 0
    and $TS_SQL > '$ESC_GINGER' and $TS_SQL <= '$ESC_VISTO'
  order by ts asc limit $TOPE_PEND;
")"

# --- estado general del chat, haya o no mensajes nuevos ---
ULTIMO="$(consulta "
  select $TS_SQL, $ROL_SQL
  from messages where chat_jid = '$JID' order by timestamp desc limit 1;
")"
ULTIMO_TS="${ULTIMO%%${SEP}*}"
ULTIMO_ROL="${ULTIMO##*${SEP}}"

SILENCIO_MIN=0
if [ -n "$ULTIMO_TS" ]; then
  ahora=$(date +%s)
  ult="$(a_segundos "$ULTIMO_TS")"
  [ "$ult" -eq 0 ] && ult=$ahora
  SILENCIO_MIN=$(( (ahora - ult) / 60 ))
fi

hay_ginger=0
hay_mikel=0
if [ -n "$FILAS" ]; then
  grep -q "${SEP}${ROL_CONTACTO}${SEP}" <<< "$FILAS" && hay_ginger=1
  grep -q "${SEP}MIKEL${SEP}"  <<< "$FILAS" && hay_mikel=1
fi

hay_pendientes=0
n_pend=0
if [ -n "$PENDIENTES" ]; then
  hay_pendientes=1
  n_pend="$(grep -c . <<< "$PENDIENTES")"
fi

# Ultimo mensaje MIO en el chat. Si es posterior a todo lo pendiente, esos pendientes ya
# estan contestados y lo unico que quedo sin hacer fue mover el cursor. Decirlo evita que
# el modelo los responda dos veces, que es el riesgo que trae volver a mostrarlos.
YA_CONTESTADOS=0
if [ "$hay_pendientes" -eq 1 ]; then
  ULT_PEND_TS="$(tail -1 <<< "$PENDIENTES" | cut -d"$SEP" -f1)"
  ULT_TULPA_TS="$(consulta "
    select $TS_SQL from messages
    where chat_jid = '$JID' and sender = '$SENDER_TULPA'
    order by timestamp desc limit 1;
  ")"
  if [ -n "$ULT_TULPA_TS" ] && \
     [ "$(a_segundos "$ULT_TULPA_TS")" -gt "$(a_segundos "$ULT_PEND_TS")" ]; then
    YA_CONTESTADOS=1
  fi
fi

# NUEVOS  → hay algo suyo sin responder, sea recien llegado o de antes.
# REVISAR → escribio Mikel, hay silencio, o quedaron pendientes que YA tienen respuesta
#           mia posterior: ahi no se escribe, se pone al dia el cursor y ya.
if [ "$hay_ginger" -eq 1 ] || { [ "$hay_pendientes" -eq 1 ] && [ "$YA_CONTESTADOS" -eq 0 ]; }; then
  ESTADO="NUEVOS"
elif [ "$hay_mikel" -eq 1 ] || [ "$hay_pendientes" -eq 1 ] || [ "$SILENCIO_MIN" -ge "$UMBRAL_SILENCIO" ]; then
  ESTADO="REVISAR"
else
  ESTADO="NADA"
fi

# --- traspaso a registrar.sh ---
# Va el timestamp del ultimo mensaje que este pulso ALCANZO A IMPRIMIR, nunca el ultimo
# de la base: lo que entre despues de las consultas de arriba no lo vio el modelo y tiene
# que volver a salir en la vuelta siguiente.
#
# Con ESTADO NADA las filas no se imprimen, pero el cursor avanza igual: NADA solo puede
# darse cuando lo unico nuevo son mensajes mios, y esos no hace falta enseñarlos.
VISTO_NUEVO="$LAST_VISTO"
[ -n "$FILAS" ] && VISTO_NUEVO="$(tail -1 <<< "$FILAS" | cut -d"$SEP" -f1)"

# Y el ultimo mensaje DE ELLA que se mostro, que es lo que vale `--ts visto`. Si hay
# suyos en la ventana nueva manda el ultimo de esos; si no, el ultimo de los pendientes.
VISTO_GINGER=""
if [ "$hay_ginger" -eq 1 ]; then
  VISTO_GINGER="$(grep "${SEP}${ROL_CONTACTO}${SEP}" <<< "$FILAS" | tail -1 | cut -d"$SEP" -f1)"
elif [ "$hay_pendientes" -eq 1 ]; then
  VISTO_GINGER="$(tail -1 <<< "$PENDIENTES" | cut -d"$SEP" -f1)"
fi

# --- EL DOBLE CHECK AZUL ---
# Se marcan sus mensajes desde LAST_LEIDO_TS hasta ahora. Solo los suyos: marcar como
# leidos los propios no significa nada.
#
# Estando MUDO no se toca. /off es "el bot no toca el chat", y un check azul es tocarlo:
# es visible para ella y afirma "te estoy leyendo". Si ademas no va a haber respuesta,
# es una mentira. Al volver con /on se marca el backlog entero de una.
#
# Si el curl falla, o el bridge es viejo y no tiene /api/markread (404), NO se escribe
# LEIDO= y el cursor no avanza: la vuelta siguiente reintenta los mismos IDs. Eso hace
# que este script se pueda desplegar antes de recompilar el Go, y que un corte de red no
# deje un hueco permanente de mensajes en gris.
LEIDO_NUEVO=""
if [ "$MARCAR_LEIDO" = "1" ] && [ ! -f "$ARCH_MUDO" ]; then
  ESC_LEIDO="$(leer_leido)"; ESC_LEIDO="${ESC_LEIDO//\'/\'\'}"
  POR_MARCAR="$(consulta "
    select coalesce(id,'') from messages
    where chat_jid = '$JID' and is_from_me = 0 and $TS_SQL > '$ESC_LEIDO'
    order by timestamp desc limit $TOPE_LEIDO;
  ")"
  if [ -n "$POR_MARCAR" ]; then
    LEIDO_CANDIDATO="$(consulta "
      select max($TS_SQL) from messages
      where chat_jid = '$JID' and is_from_me = 0 and $TS_SQL > '$ESC_LEIDO';
    ")"
    CUERPO_MARCA="$(python3 -c '
import json, sys
ids = [l for l in sys.stdin.read().split(chr(10)) if l]
print(json.dumps({"chat_jid": sys.argv[1], "message_ids": ids}))
' "$JID" <<< "$POR_MARCAR")"
    RESP_MARCA="$(curl -s -m 3 -X POST "$MARCAR_URL" \
      -H 'Content-Type: application/json' -d "$CUERPO_MARCA" 2>/dev/null)"
    case "$RESP_MARCA" in
      *'"success":true'*) LEIDO_NUEVO="$LEIDO_CANDIDATO" ;;
    esac
  else
    # Nada nuevo suyo que marcar, pero el cursor se pone al dia igual para que no
    # arrastre un hueco si mas tarde se activa el mudo.
    LEIDO_NUEVO="$ESC_LEIDO"
  fi
fi

TMP_T="$(mktemp "$TRASPASO.XXXXXX")"
{
  echo "VISTO=$VISTO_NUEVO"
  echo "VISTO_GINGER=$VISTO_GINGER"
  [ -n "$LEIDO_NUEVO" ] && echo "LEIDO=$LEIDO_NUEVO"
} > "$TMP_T"
chmod 644 "$TMP_T"
mv -f "$TMP_T" "$TRASPASO"

# El mapa de citas se reescribe ENTERO cada vuelta, incluso vacio: si se dejara el de la
# vuelta anterior, un `--citar 3` de esta vuelta apuntaria a otra conversacion. Se llama
# desde los dos puntos de salida del script.
volcar_citas() {
  local tmp
  tmp="$(mktemp "$CITAS.XXXXXX")" || return 0
  {
    printf 'EPOCA=%s\n' "$(date +%s)"
    printf '%s' "${MAPA_CITAS:-}"
  } > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$CITAS"
}

# Por trap y no por llamadas sueltas en cada salida: el traspaso se escribe antes que
# esto, asi que una muerte en medio (un SIGPIPE de un `| head`, sin ir mas lejos) dejaba
# un traspaso nuevo con el mapa de la vuelta ANTERIOR, y un `--citar 3` dentro de los 15
# min del TTL apuntaria a otra conversacion sin que nada avisara.
trap volcar_citas EXIT

# ---------------------------------------------------------------------------
# SALIDA
# ---------------------------------------------------------------------------
echo "ESTADO: $ESTADO"
echo "ARRANQUE: $ARRANQUE"
echo "BRIDGE: vivo"
echo "LAST_GINGER_TS: $LAST_GINGER"
echo "LAST_VISTO_TS: $LAST_VISTO"
echo "ULTIMO_EN_EL_CHAT: ${ULTIMO_ROL:-ninguno} (${ULTIMO_TS:-sin mensajes})"
echo "SILENCIO_MIN: $SILENCIO_MIN"
echo "MUDO: $MUDO_LINEA"

# MarkRead degrada en silencio a read-self cuando la cuenta tiene las confirmaciones de
# lectura apagadas: el check azul no sale nunca y nada da error. Se avisa aqui porque es
# lo unico que el codigo no puede arreglar solo.
case "$SALUD_CUERPO" in
  *'"read_receipts":"none"'*)
    echo "OJO: la cuenta tiene las confirmaciones de lectura APAGADAS. El doble check azul"
    echo "     no va a salir por mas que el bot lo marque. Se arregla en Ajustes > Privacidad." ;;
esac

# Solo mientras la funcion sea nueva. En cuanto se estrene, la marca existe y esta linea
# no vuelve a salir nunca.
if [ ! -f "${WSP_ESTRENO_STICKERS:-$BASE/nucleo/ESTRENO_STICKERS_HECHO}" ] && [ "$ESTADO" != "NADA" ]; then
  echo "ESTRENO_STICKERS: pendiente (ver PASO 4)"
fi

# Pinta una tanda de filas del formato ts|rol|media|id|filename|texto, resolviendo la
# media contra la cache. Las dos ventanas salen con las mismas columnas para poder usar
# esto en las dos sin traducir nada.
# Numero de cita. Es de ESTA vuelta y solo de esta: sirve para el --citar de
# responder.sh y no tiene nada que ver con el Message ID.
#
# El contador vive fuera de la funcion a proposito. pintar_filas se invoca con
# here-string, que no abre subshell, asi que sobrevive entre las dos llamadas y los
# numeros no se repiten entre la seccion de pendientes y la de nuevos.
N_CITA=0
MAPA_CITAS=""

pintar_filas() {
  local ts rol media id arch texto hora cache clave
  while IFS="$SEP" read -r ts rol media id arch texto; do
    [ -n "$ts" ] || continue
    hora="${ts:11:8}"
    N_CITA=$((N_CITA + 1))
    [ -n "$id" ] && MAPA_CITAS+="$N_CITA"$'\t'"$id"$'\n'
    if [ -n "$media" ]; then
      # La media se anuncia con su id para poder bajarla; el contenido se resuelve
      # despues (PASO 1), y si ya se resolvio antes se muestra cacheado.
      #
      # Los stickers se cachean por NOMBRE DE ARCHIVO y no por message_id: el nombre sale
      # del hash del sticker, asi que el mismo sticker mandado veinte veces se describe
      # una sola vez y las otras diecinueve salen de aqui gratis.
      clave="$id"
      [ "$media" = "sticker" ] && [ -n "$arch" ] && clave="${arch%.webp}"
      cache=""
      [ -s "$TRANSCRIPCIONES/$clave.txt" ] && cache="$(head -c 300 "$TRANSCRIPCIONES/$clave.txt")"
      [ -s "$DESCRIPCIONES/$clave.txt" ]   && cache="$(head -c 300 "$DESCRIPCIONES/$clave.txt")"
      # La linea de media conserva su Message ID entero: el PASO 1 lo necesita para
      # download_media y ese contrato no se toca. Queda redundante con el #N y da igual.
      if [ -n "$cache" ]; then
        printf '[%s] %-4s %s: <%s ya resuelto: %s>\n' "$hora" "#$N_CITA" "$rol" "$media" "$cache"
      else
        printf '[%s] %-4s %s: <%s PENDIENTE - Message ID: %s>\n' "$hora" "#$N_CITA" "$rol" "$media" "$id"
      fi
    else
      printf '[%s] %-4s %s: %s\n' "$hora" "#$N_CITA" "$rol" "$texto"
    fi
  done
}

# En el primer pulso de cada encendido se vuelca el chat reciente para no entrar en frio
# a una conversacion que siguio sin la tulpa. Sale una sola vez por encendido: en las
# vueltas siguientes ARRANQUE ya es CALIENTE y esto no se imprime.
if [ "$ARRANQUE" = "FRIO" ]; then
  echo "--- CONTEXTO DE ARRANQUE: ultimos $CONTEXTO_FRIO mensajes ---"
  echo "(el bot estuvo apagado. Leelo entero antes de escribir nada: puede haber pasado"
  echo " cualquier cosa mientras no estabas, y meterte sin saberlo es la peor falla.)"
  consulta "
    select * from (
      select
        $TS_SQL, $ROL_SQL,
        case
          when coalesce(media_type,'') <> '' then '<' || media_type || '>'
          else replace(replace(coalesce(content,''), char(10), ' '), char(9), ' ')
        end
      from messages
      where chat_jid = '$JID'
      order by timestamp desc limit $CONTEXTO_FRIO
    ) order by 1 asc;
  " | while IFS="$SEP" read -r ts rol cuerpo; do
    [ -n "$ts" ] || continue
    printf '[%s %s] %-7s %s\n' "${ts:5:5}" "${ts:11:5}" "$rol:" "$cuerpo"
  done
  echo "--- FIN DEL CONTEXTO ---"
fi

if [ "$ESTADO" = "NADA" ]; then
  echo "REGLAS: 1 o 5 — nada suyo sin responder y el silencio no llega a $UMBRAL_SILENCIO min. No escribas."
  exit 0
fi

# --- que reglas estan en juego ---
if [ "$hay_ginger" -eq 1 ] || { [ "$hay_pendientes" -eq 1 ] && [ "$YA_CONTESTADOS" -eq 0 ]; }; then
  echo "REGLAS: 2 o 3 — hay mensajes suyos sin responder. Decidi vos si el ultimo es una despedida."
elif [ "$hay_pendientes" -eq 1 ]; then
  echo "REGLAS: ninguna — los pendientes de abajo ya tienen respuesta tuya. Solo hay que poner el cursor al dia."
elif [ "$hay_mikel" -eq 1 ]; then
  echo "REGLAS: 6 — solo escribio Mikel. Meterte es opcional; no le pises una disculpa ni un tema suyo."
else
  echo "REGLAS: 4 o 5 — $SILENCIO_MIN min de silencio. Sacar tema solo si no hubo despedida."
fi

if [ "$hay_pendientes" -eq 1 ]; then
  echo "--- DE ELLA, SIN RESPONDER DE ANTES ($n_pend) ---"
  if [ "$YA_CONTESTADOS" -eq 1 ]; then
    echo "(OJO: hay respuesta tuya posterior a estos. Ya los atendiste y solo falto mover"
    echo " el cursor: cierra con --ts visto y NO los vuelvas a responder.)"
  else
    echo "(ya se mostraron en vueltas anteriores y el cursor no los paso. Siguen sin respuesta.)"
  fi
  pintar_filas <<< "$PENDIENTES"
fi

echo "--- NUEVO DESDE LA ULTIMA VUELTA ---"
if [ "${TOTAL_NUEVOS:-0}" -gt "$TOPE" ]; then
  echo "(entraron $TOTAL_NUEVOS; se muestran los $TOPE mas recientes. Lo suyo que se caiga"
  echo " por el tope vuelve a salir arriba mientras siga sin responder.)"
fi
if [ -z "$FILAS" ]; then
  echo "(ninguno)"
else
  pintar_filas con_rol <<< "$FILAS"
fi
echo "--- FIN ---"
