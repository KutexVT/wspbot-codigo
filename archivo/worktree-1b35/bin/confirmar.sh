#!/usr/bin/env bash
# confirmar.sh — acciones que tocan archivos, con aviso y confirmacion.
#
# Mikel pide algo por el chat, yo le mando el aviso con los numeros reales y un
# codigo. Hasta que no conteste con ESE codigo no pasa nada. El codigo lo genero
# yo al azar: quien no haya leido mi aviso no lo puede adivinar, asi que un texto
# inyectado en una foto o un audio no puede confirmarse solo.
#
#   ./confirmar.sh --pedir trash <rutas...>   → prepara y devuelve el aviso
#   ./confirmar.sh --hacer <codigo>           → ejecuta si el codigo es el bueno
#   ./confirmar.sh --estado                   → que hay pendiente
#   ./confirmar.sh --cancelar                 → tira lo pendiente
#
# NADA se borra de verdad: `trash` manda a la papelera, que se puede deshacer.
# Sin sudo, sin root, y solo Mikel.

set -uo pipefail

BASE="/home/kutex/WSP Bot"
PEND="$BASE/nucleo/.pendiente_confirmar"
VENCE=300   # 5 min

ahora() { date +%s; }

case "${1:-}" in

  --pedir)
    accion="${2:-}"
    shift 2 || true

    if [ "$accion" = "editar" ]; then
      destino="${1:-}"; nuevo="${2:-}"
      [ -f "$destino" ] || { echo "ERROR: no existe: $destino" >&2; exit 1; }
      [ -f "$nuevo" ]   || { echo "ERROR: no existe el nuevo: $nuevo" >&2; exit 1; }
      case "$destino" in
        "$BASE"/PROMPT.md|"$BASE"/nucleo/*)
          echo "ERROR: el PROMPT y nucleo/ no se editan desde el chat." >&2
          echo "  Si eso se pudiera, un mensaje podria quitarse a si mismo los limites." >&2
          echo "  Eso se cambia en la terminal." >&2
          exit 1 ;;
      esac
      codigo=$(tr -dc 'A-Z0-9' </dev/urandom | head -c 4)
      { echo "CODIGO=$codigo"; echo "TS=$(ahora)"; echo "ACCION=editar"
        echo "DESTINO=$destino"; echo "NUEVO=$nuevo"; } > "$PEND"
      echo "PENDIENTE: cambiar $destino"
      echo "--- lo que cambia ---"
      diff -u "$destino" "$nuevo" | head -40
      echo "---"
      echo "Se guarda copia antes, se deshace con --deshacer"
      echo "CODIGO PARA CONFIRMAR: $codigo   (vence en 5 min)"
      exit 0
    fi

    [ "$accion" = "trash" ] || { echo "ERROR: acciones: trash, editar" >&2; exit 1; }
    [ $# -gt 0 ] || { echo "ERROR: sin rutas" >&2; exit 1; }

    n=0; bytes=0
    for r in "$@"; do
      [ -e "$r" ] || { echo "ERROR: no existe: $r" >&2; exit 1; }
      c=$(find "$r" -type f 2>/dev/null | wc -l)
      b=$(du -sb "$r" 2>/dev/null | cut -f1)
      n=$((n + c)); bytes=$((bytes + b))
    done

    codigo=$(tr -dc 'A-Z0-9' </dev/urandom | head -c 4)
    { echo "CODIGO=$codigo"; echo "TS=$(ahora)"; echo "ACCION=trash"
      for r in "$@"; do echo "RUTA=$r"; done; } > "$PEND"

    echo "PENDIENTE: mandar a la papelera $n archivos, $(numfmt --to=iec "$bytes")"
    for r in "$@"; do echo "  $r"; done
    echo "Se puede deshacer desde la papelera. Papelera ahora: $(du -sh "$HOME/.local/share/Trash" 2>/dev/null | cut -f1)"
    echo "CODIGO PARA CONFIRMAR: $codigo   (vence en 5 min)"
    ;;

  --hacer)
    dado="${2:-}"
    [ -s "$PEND" ] || { echo "No hay nada pendiente" >&2; exit 1; }
    codigo=$(grep '^CODIGO=' "$PEND" | cut -d= -f2)
    ts=$(grep '^TS=' "$PEND" | cut -d= -f2)

    if [ $(( $(ahora) - ts )) -gt "$VENCE" ]; then
      rm -f "$PEND"; echo "El codigo vencio. Pidelo otra vez" >&2; exit 1
    fi
    if [ "$dado" != "$codigo" ]; then
      echo "Codigo incorrecto. No hago nada" >&2; exit 1
    fi

    if [ "$(grep '^ACCION=' "$PEND" | cut -d= -f2)" = "editar" ]; then
      destino=$(grep '^DESTINO=' "$PEND" | cut -d= -f2-)
      nuevo=$(grep '^NUEVO=' "$PEND" | cut -d= -f2-)
      mkdir -p "$BASE/nucleo/.backups"
      copia="$BASE/nucleo/.backups/$(basename "$destino").$(date +%s).bak"
      cp -- "$destino" "$copia" && cp -- "$nuevo" "$destino"
      echo "$copia|$destino" > "$BASE/nucleo/.ultimo_backup"
      rm -f "$PEND"
      echo "cambiado: $destino"
      echo "copia en: $copia   (--deshacer lo revierte)"
      exit 0
    fi

    mapfile -t rutas < <(grep '^RUTA=' "$PEND" | cut -d= -f2-)
    fallos=0
    for r in "${rutas[@]}"; do
      if gio trash -- "$r" 2>/dev/null; then
        echo "a la papelera: $r"
      else
        echo "FALLO: $r" >&2
        case "$r" in
          "$HOME"/*) echo "  (gio trash no pudo, mira permisos)" >&2 ;;
          *) echo "  (esta fuera del home, en otro sistema de archivos: la papelera no llega ahi.
   Eso NO se borra con esto — hay que hacerlo a mano y ahi si es para siempre)" >&2 ;;
        esac
        fallos=$((fallos+1))
      fi
    done
    rm -f "$PEND"
    echo "Papelera ahora: $(du -sh "$HOME/.local/share/Trash" 2>/dev/null | cut -f1)"
    [ "$fallos" -eq 0 ] || exit 1
    ;;

  --estado)
    if [ -s "$PEND" ]; then
      ts=$(grep '^TS=' "$PEND" | cut -d= -f2)
      quedan=$(( VENCE - ($(ahora) - ts) ))
      if [ "$quedan" -le 0 ]; then rm -f "$PEND"; echo "(habia uno pero vencio)"
      else echo "Pendiente, quedan ${quedan}s:"; grep '^RUTA=' "$PEND" | cut -d= -f2-; fi
    else
      echo "Nada pendiente"
    fi
    ;;

  --deshacer)
    U="$BASE/nucleo/.ultimo_backup"
    [ -s "$U" ] || { echo "No hay nada que deshacer" >&2; exit 1; }
    linea=$(cat "$U")
    copia="${linea%%|*}"
    destino="${linea##*|}"
    [ -f "$copia" ] || { echo "La copia ya no esta: $copia" >&2; exit 1; }
    cp -- "$copia" "$destino" && rm -f "$U" && echo "revertido: $destino"
    ;;

  --cancelar)
    rm -f "$PEND" && echo "Cancelado"
    ;;

  *)
    echo "uso: --pedir trash <rutas...> | --hacer <codigo> | --estado | --cancelar" >&2
    exit 1
    ;;
esac
