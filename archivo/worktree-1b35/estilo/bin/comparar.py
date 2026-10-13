#!/usr/bin/env python3
# comparar.py — pone lado a lado lo que dijo Contacto, lo que contesto Mikel de verdad, y
# lo que contesto el bot. Y despues mide la distancia en numeros.
#
# El lado a lado es para mirarlo con los ojos; las metricas son para que "mejoro" no sea
# una impresion. Las dos cosas hacen falta: el ojo ve defectos que ninguna metrica coge,
# y la metrica coge desviaciones que el ojo perdona.

import json, sys, os
sys.path.insert(0, "/home/kutex/WSP Bot/bin")
import sqlite3
import estilo
from estilo_metricas import comparar, imprimir

def cargar(p):
    return [json.loads(l) for l in open(p, encoding="utf-8")]

def lado_a_lado(items, n=12, etiqueta="BOT"):
    for it in items[:n]:
        print("\n" + "-"*76)
        for l in it["contexto"][-2:]:
            quien = "GINGER" if l.startswith("G:") else "MIKEL "
            print(f"  {quien} | {l[3:][:66]}")
        print(f"  GINGER | {it['estimulo'][:66]}")
        print(f"  {'':6} |")
        print(f"  MIKEL  | {' / '.join(it['real'].split(chr(10)))[:66]}   <- real")
        g = ' / '.join(x for x in it['generado'].split('\n') if x.strip())
        print(f"  {etiqueta:<6} | {g[:66]}")
        if len(g) > 66: print(f"  {'':6} | {g[66:132]}")

if __name__ == "__main__":
    ruta = sys.argv[1]
    et   = sys.argv[2] if len(sys.argv) > 2 else "BOT"
    items = cargar(ruta)
    items = [x for x in items if x.get("generado")]
    print(f"\n{'='*76}\n{ruta}  —  {len(items)} items\n{'='*76}")
    lado_a_lado(items, 12, et)

    # metricas: cada mensaje generado por separado, igual que cuenta la base
    gen  = [l for it in items for l in it["generado"].split("\n") if l.strip()]
    estilo.cargar()
    c = sqlite3.connect(f"file:{estilo.DB}?mode=ro", uri=True)
    real = [r[0] for r in c.execute("""select content from messages where chat_jid=?
        and is_from_me=1 and sender<>'__tulpa__' and coalesce(media_type,'')=''
        and coalesce(content,'')<>''""", (estilo.JID,))]
    r = comparar(gen, real, f"{et} — {len(gen)} mensajes generados")
    imprimir(r)
    # y el linter, que es gratis y determinista
    idx = estilo.cargar()
    err = sum(1 for m in gen if any(x["nivel"]=="ERROR" for x in estilo.revisar(m, idx)))
    print(f"\n  linter: {err}/{len(gen)} mensajes con ERROR ({err*100/len(gen):.1f}%)"
          f"   — sobre mensajes reales suyos da 1,2%")
    json.dump(r, open(ruta.replace(".jsonl", "-metricas.json"), "w"), indent=1, ensure_ascii=False)
