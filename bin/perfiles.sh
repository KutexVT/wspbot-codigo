#!/usr/bin/env bash
# perfiles.sh — comprueba que los dos perfiles siguen siendo legibles por quien los lee.
#
#   ./bin/perfiles.sh --verificar     0 limpio · 1 warns · 2 errors
#
# Existe por un fallo que estuvo semanas activo sin que nadie lo viera. `contacto.md`
# se lee POR SECCIONES, con el awk de la linea 68 de este mismo archivo:
#
#     awk -v t="## Salud y bienestar" \
#         'index($0,t)==1 && length($0)==length(t) {f=1; next} f && /^## / {exit} f' \
#         memoria/contacto.md
#
# Un titulo cambiado NO da error: ese awk devuelve vacio y el bot concluye que el dato no
# existe.
#
# OJO — la version corta que parece equivalente esta ROTA y no se usa en ningun lado:
#
#     awk '/^## Salud y bienestar/,/^## /'      # devuelve el titulo y CERO contenido
#
# El rango de awk se cierra en su propia linea de apertura, porque `/^## /` matchea
# tambien la linea que abrio el rango. Estuvo documentada en PROMPT.md, PROMPT_MEMORIA.md
# e INDICE.md hasta el 2026-08-24, asi que el bot leia vacio mientras este script — que
# usa la buena — daba luz verde. Corregido en los tres. Y como las correcciones se escribian al final del archivo en vez de arreglar la
# linea, la seccion decia "Le gusta el picante: Si" mientras 300 lineas mas abajo estaba
# desmentido — y el que leia la seccion se llevaba lo viejo.
#
# Ahora el bot escribe DENTRO de las secciones, que es mas util y mas fragil. Esto es la
# unica red que hay. Corre al final del pase de memoria y del PASO 6, nunca en pulso.sh.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"

GINGER="$BASE/memoria/contacto.md"
ESTILO="$BASE/estilo/user_messaging_style.md"
CAMBIOS_G="$BASE/memoria/ginger_cambios.md"
CAMBIOS_E="$BASE/estilo/cambios.md"
TOPE_ESTILO="${WSP_TOPE_ESTILO:-18000}"

ERR=0; WARN=0
err()  { echo "ERROR  $*"; ERR=$((ERR+1)); }
warn() { echo "WARN   $*"; WARN=$((WARN+1)); }
ok()   { echo "ok     $*"; }

# Las secciones de la ficha de ella. Esta lista es el contrato: si una desaparece o cambia
# de nombre, el awk que la lee empieza a devolver vacio en silencio.
SECCIONES_GINGER=(
  "Datos básicos" "Personalidad y forma de ser" "Gustos — Comida y bebida"
  "Gustos — Entretenimiento" "Gustos — Música" "Gustos — Estética y moda"
  "Gustos — Hobbies y actividades" "Gustos — Naturaleza, viajes y ambiente"
  "Redes sociales y tecnología" "Relación con el usuario"
  "Jerga y forma de hablar (su huella digital)" "Chistes internos y bromas recurrentes"
  "Familia y entorno" "Historial de relaciones (contexto para no meter la pata)"
  "Salud y bienestar" "Educación" "Creencias y supersticiones"
  "Detalles pequeños que valen oro" "Manual práctico — qué funciona con ella"
)
# Del perfil de estilo solo se fija el titulo que citan el hook y siempre.md como autoridad.
SECCIONES_ESTILO=( "Cómo aplicar este estilo — reglas de naturalidad" )

echo "--- secciones ---"
falta=0
for s in "${SECCIONES_GINGER[@]}"; do
  n=$(grep -cxF "## $s" "$GINGER" 2>/dev/null || echo 0)
  [ "$n" -eq 1 ] || { err "contacto.md: '## $s' aparece $n veces (debe ser 1)"; falta=1; }
done
[ "$falta" -eq 0 ] && ok "las ${#SECCIONES_GINGER[@]} secciones de contacto.md, cada una una vez"
for s in "${SECCIONES_ESTILO[@]}"; do
  n=$(grep -cxF "## $s" "$ESTILO" 2>/dev/null || echo 0)
  [ "$n" -eq 1 ] && ok "user_messaging_style.md: '## $s'" \
                 || err "user_messaging_style.md: '## $s' aparece $n veces (lo citan el hook y siempre.md)"
done

# No basta con que el titulo exista: hay que correr el awk de verdad, que es lo que corre
# el bot. Es la diferencia entre comprobar el riesgo y comprobar una aproximacion de el.
echo "--- los awk de produccion devuelven contenido ---"
vacias=0
for s in "${SECCIONES_GINGER[@]}"; do
  # Comparacion literal, no regex: dos titulos llevan parentesis ("Jerga y forma de hablar
  # (su huella digital)") y como patron se leerian como grupo de captura, devolviendo vacio.
  n=$(awk -v t="## $s" 'index($0,t)==1 && length($0)==length(t) {f=1; next} f && /^## / {exit} f' "$GINGER" | grep -c . || true)
  [ "${n:-0}" -ge 2 ] || { err "awk '## $s' devuelve $n lineas de contenido"; vacias=1; }
done
[ "$vacias" -eq 0 ] && ok "las ${#SECCIONES_GINGER[@]} secciones devuelven contenido"

echo "--- el perfil es guia, no registro ---"
for f in "$GINGER" "$ESTILO"; do
  b=$(basename "$f")
  n=$(grep -cE '\(20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$f" || true)
  [ "${n:-0}" -eq 0 ] && ok "$b: sin fechas" || err "$b: $n fechas — el registro va al archivo de cambios"
  n=$(grep -c '^## Aprendido en automatico' "$f" || true)
  [ "${n:-0}" -eq 0 ] && ok "$b: sin zona de aprendido automatico" \
                      || err "$b: reaparecio '## Aprendido en automatico'"
done
n=$(grep -ciE 'visto [0-9]+ vec|correccion directa suya|corrección directa suya' "$ESTILO" || true)
[ "${n:-0}" -eq 0 ] && ok "user_messaging_style.md: sin conteos ni atribuciones" \
                    || err "user_messaging_style.md: $n conteos/atribuciones"

echo "--- frontmatter ---"
for f in "$GINGER" "$ESTILO"; do
  b=$(basename "$f")
  claves=$(awk 'NR==1&&/^---$/{f=1;next} f&&/^---$/{exit} f&&/^[a-zA-Z_]+:/{print $1}' "$f" | tr -d ':' | tr '\n' ' ')
  [ "$claves" = "name description " ] && ok "$b: name + description" \
                                      || warn "$b: frontmatter con '$claves' (se espera 'name description')"
done

echo "--- los archivos de cambios solo crecen ---"
for f in "$CAMBIOS_E" "$CAMBIOS_G"; do
  b=$(basename "$f")
  [ -f "$f" ] || { err "falta $b"; continue; }
  marca="$BASE/nucleo/.perfiles_$(basename "$f" .md)"
  n=$(wc -l < "$f")
  if [ -f "$marca" ] && [ "$n" -lt "$(cat "$marca")" ]; then
    err "$b encogio: $(cat "$marca") -> $n lineas. Es append-only"
  else
    ok "$b: $n lineas"
  fi
  echo "$n" > "$marca"
done

echo "--- tamaño ---"
n=$(wc -c < "$ESTILO")
[ "$n" -le "$TOPE_ESTILO" ] && ok "user_messaging_style.md: $n B (tope $TOPE_ESTILO)" \
  || warn "user_messaging_style.md: $n B, por encima de $TOPE_ESTILO. Se lee entero en cada vuelta: mira si volvio a entrar registro"

echo
[ "$ERR" -gt 0 ] && { echo "$ERR errores, $WARN warns"; exit 2; }
[ "$WARN" -gt 0 ] && { echo "0 errores, $WARN warns"; exit 1; }
echo "todo en orden"; exit 0
