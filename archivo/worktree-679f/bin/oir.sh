#!/usr/bin/env bash
# oir.sh — transcribe una nota de voz de WhatsApp a texto.
#
#   ./oir.sh /ruta/audio_20260729_171052.ogg
#   ./oir.sh /ruta/audio.ogg ACA3B0947C00127F4760C95AAA5CD9CF   # con message_id → cachea
#
# Escribe la transcripcion en stdout y nada mas. Todo lo demas (avisos, errores) va a
# stderr, para que quien lo llame pueda usar la salida directo.
#
# Si le pasas el message_id, guarda el resultado en media/transcripciones/<id>.txt y la
# proxima vez lo devuelve de ahi sin volver a transcribir. Sin eso, el loop que corre cada
# minuto re-transcribiria el mismo audio en cada vuelta.
#
# --- POR QUE ESTE ARCHIVO DESCONFIA TANTO DE SU PROPIO MOTOR ---
#
# El 2026-08-14 a las 23:07 pacman subio ggml de 0.19 a 0.20 y el paquete venia sin
# backends, asi que whisper-cli empezo a abortar. Hasta ahi, un fallo normal. El problema
# fue lo que hizo este script con eso: whisper escupia el backtrace de gdb por **stdout**
# (no por stderr, asi que el 2>/dev/null no lo filtraba), la sustitucion de comando lo
# capturaba como si fuera texto, nadie miraba el codigo de salida — y se cacheaba.
# Resultado: en chats/2026-08-14.md quedo GINGER "diciendo" un volcado de gdb, y como la
# cache manda, ese texto falso se servia para siempre.
#
# De ahi las tres reglas que ya no se tocan:
#   1. Se mira SIEMPRE el codigo de salida del motor.
#   2. La salida se valida ANTES de usarse (por si un dia falla sin devolver error).
#   3. No se escribe cache si la validacion no paso. Mejor sin transcripcion que con una
#      inventada: un hueco se nota, una cita falsa no.

set -uo pipefail

BASE="/home/kutex/WSP Bot"
CACHE="$BASE/media/transcripciones"
ASR="$BASE/bin/asr.py"
PY="$BASE/bin/venv-asr/bin/python"

# Motor por defecto. Los dos estan instalados y se cambia solo aqui:
#   parakeet  → NVIDIA Parakeet TDT 0.6B v3 (ONNX int8). ~30x tiempo real en CPU y no
#               alucina en silencios: su decoder emite un "blank" y el silencio sale vacio.
#   whisper   → faster-whisper. Mas robusto con ruido y acento, pero alucina en las pausas
#               ([Musica] [Musica]...) y es mas lento. Modelos: small, large-v3-turbo.
MOTOR="${WSP_ASR_MOTOR:-parakeet}"
MODELO_WHISPER="${WSP_ASR_MODELO:-large-v3-turbo}"

# Presupuesto de TIEMPO DE RELOJ, no de duracion de audio: lo que importa es que quepa en
# una vuelta del bot, que es de 1 minuto. El motor emite segmentos hasta agotarlo y corta.
PRESUPUESTO="${WSP_ASR_PRESUPUESTO:-90}"

if [ $# -eq 0 ]; then
  echo "uso: oir.sh <archivo.ogg> [message_id]" >&2
  exit 1
fi

AUDIO="$1"
MSG_ID="${2:-}"

if [ ! -f "$AUDIO" ]; then
  echo "ERROR: no existe el archivo: $AUDIO" >&2
  exit 1
fi

# --- cache: si ya lo transcribimos, devolverlo y salir ---
if [ -n "$MSG_ID" ]; then
  CACHE_FILE="$CACHE/$MSG_ID.txt"
  if [ -s "$CACHE_FILE" ]; then
    cat "$CACHE_FILE"
    exit 0
  fi
fi

if [ ! -x "$PY" ]; then
  echo "ERROR: falta el entorno de transcripcion en $PY" >&2
  echo "       recrealo con:  uv venv bin/venv-asr && uv pip install 'onnx-asr[cpu]' faster-whisper" >&2
  exit 2
fi

if [ ! -f "$ASR" ]; then
  echo "ERROR: falta $ASR" >&2
  exit 2
fi

# --- transcribir ---
# La salida va a un fichero, NO directo a una variable: asi se puede mirar el codigo de
# salida y revisar el contenido antes de dar nada por bueno.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SALIDA="$TMP/salida.txt"

if [ "$MOTOR" = "whisper" ]; then
  "$PY" "$ASR" "$AUDIO" --motor whisper --modelo "$MODELO_WHISPER" \
        --presupuesto "$PRESUPUESTO" >"$SALIDA"
else
  "$PY" "$ASR" "$AUDIO" --motor parakeet --presupuesto "$PRESUPUESTO" >"$SALIDA"
fi
ESTADO=$?

# REGLA 1 — el codigo de salida manda. 134 es el SIGABRT que dio el ggml roto.
if [ "$ESTADO" -ne 0 ]; then
  echo "ERROR: el motor de transcripcion ($MOTOR) fallo con codigo $ESTADO. No se cachea nada." >&2
  exit 4
fi

TEXTO="$(tr -s '[:space:]' ' ' < "$SALIDA" | sed 's/^ *//;s/ *$//')"

# REGLA 2 — validar el contenido aunque el codigo diga que todo fue bien.
# Defensa en profundidad: si un motor vuelve a morirse escupiendo un volcado por stdout
# con codigo 0, esto lo caza igual. Son marcas que jamas apareceran en una nota de voz.
case "$TEXTO" in
  *"GDB supports auto-downloading"*|*ggml_*|*libwhisper*|*"Inferior 1"*|*__libc_start_main*|*"Traceback (most recent call last)"*)
    echo "ERROR: el motor devolvio un volcado tecnico en vez de texto. No se cachea nada." >&2
    echo "       primeros 200 caracteres: ${TEXTO:0:200}" >&2
    exit 5 ;;
esac

if [ -z "$TEXTO" ]; then
  # Distinguir "no se entendio" de "fallo el proceso" importa: el bot no debe leer un
  # error como si ella hubiera mandado un audio en silencio. Aqui el motor SI termino
  # bien (codigo 0), asi que el audio esta de verdad mudo.
  echo "[audio sin habla reconocible]"
  exit 0
fi

# REGLA 3 — solo se cachea lo que paso las dos comprobaciones de arriba.
if [ -n "$MSG_ID" ]; then
  mkdir -p "$CACHE"
  printf '%s\n' "$TEXTO" > "$CACHE/$MSG_ID.txt"
fi

printf '%s\n' "$TEXTO"
