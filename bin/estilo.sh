#!/usr/bin/env bash
# estilo.sh — envoltorio del linter de estilo. El motor esta en estilo.py.
#
#   ./bin/estilo.sh "texto"           0 limpio · 1 warns · 2 errors
#   ./bin/estilo.sh --json "texto"    salida JSON, la que consume el harness
#   ./bin/estilo.sh --stats           el perfil numerico actual, derivado de la base
#   ./bin/estilo.sh --reconstruir     regenera el indice (0,5s)
#
# Existe como .sh y no se llama al .py directo para heredar las rutas de comun.sh: asi
# WSP_DB y WSP_JID desvian el linter igual que desvian a pulso.sh y registrar.sh, y se
# puede probar contra una base de juguete sin tocar la real.
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/comun.sh"
export WSP_BASE="$BASE" WSP_DB="$DB" WSP_JID="$JID"
exec python3 "$(dirname "$(readlink -f "$0")")/estilo.py" "$@"
