#!/usr/bin/env bash
# generar.sh — pide al modelo que conteste a los items del banco, con un perfil concreto.
#
#   estilo/bin/generar.sh <banco.jsonl> <perfil.md> <salida.jsonl> [paralelas]
#
# Corre con el stack de produccion ENTERO (--setting-sources user), no aislado: los hooks
# son parte de lo que se esta midiendo. Lo unico que cambia entre corridas es KUTEX_PERFIL,
# que estilo-kutex.sh lee para saber que perfil volcar — asi se hace A/B sin intercambiar
# el archivo vivo, que es la receta para perder la pista de que se midio.
set -uo pipefail
BANCO="$1"; PERFIL="$2"; SALIDA="$3"; PAR="${4:-8}"
CLAUDE="/home/kutex/.local/bin/claude"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

python3 - "$BANCO" "$TMP" <<'PY'
import json, sys
banco, tmp = sys.argv[1], sys.argv[2]
for i, ln in enumerate(open(banco, encoding="utf-8")):
    it = json.loads(ln)
    ctx = "\n".join(it["contexto"] + [f"G: {it['estimulo']}"])
    open(f"{tmp}/{i:04d}.prompt", "w", encoding="utf-8").write(
f"""Estas en el chat de WhatsApp de Mikel con su novia Contacto. Abajo va el final de la
conversacion. Responde EXACTAMENTE como responderia Mikel: una linea por cada mensaje
que mandaria.

No expliques nada, no pongas comillas ni prefijos ni guiones. Solo los mensajes, uno por
linea.

--- conversacion ---
{ctx}
--- fin ---

Tus mensajes:""")
    open(f"{tmp}/{i:04d}.id", "w").write(it["id"])
PY

ls "$TMP"/*.prompt | xargs -P "$PAR" -I{} sh -c '
  KUTEX_PERFIL="'"$PERFIL"'" "'"$CLAUDE"'" -p --model opus --setting-sources user \
    --tools "" --no-session-persistence < "{}" > "{}.out" 2>/dev/null
'

python3 - "$BANCO" "$TMP" "$SALIDA" <<'PY'
import json, os, sys
banco, tmp, salida = sys.argv[1], sys.argv[2], sys.argv[3]
items = [json.loads(l) for l in open(banco, encoding="utf-8")]
n_ok = 0
with open(salida, "w", encoding="utf-8") as f:
    for i, it in enumerate(items):
        p = f"{tmp}/{i:04d}.prompt.out"
        gen = open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""
        if gen: n_ok += 1
        it["generado"] = gen
        f.write(json.dumps(it, ensure_ascii=False) + "\n")
print(f"{n_ok}/{len(items)} generados -> {salida}")
PY
