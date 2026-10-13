#!/usr/bin/env python3
# banco.py — saca items held-out para el test ciego.
#
# Un item es: el contexto previo del chat + lo que Contacto mando + LO QUE MIKEL CONTESTO
# de verdad. Se le pide al modelo que conteste a lo mismo y se comparan.
#
# LA PARTICION. Los message id de WhatsApp son hex y su ultimo caracter esta repartido
# uniforme, asi que un substr reparte el corpus sin coordinar nada:
#
#   '0','1' -> banco de desarrollo, se re-sortea cada pocas versiones
#   '2','3' -> banco de confirmacion, se toca UNA vez al final
#   'F'     -> calibracion del juez
#   resto   -> lo que el hook de arranque le da al modelo como corpus y ejemplos
#
# El hook estilo-kutex.sh excluye las cuatro primeras de sus consultas. Con eso el modelo
# generador NUNCA ha visto un item con el que luego se le mide, y no hace falta ningun
# archivo de coordinacion que se pueda desincronizar.
#
# El corte por fecha es aparte y tambien importa: antes del 2026-07-20 no existia la
# tulpa, asi que lo que hay ahi es escritura suya sin contaminar.

import json, os, sqlite3, sys, random
sys.path.insert(0, "/home/kutex/WSP Bot/bin")
import estilo

PART = {"dev": ("0","1"), "test": ("2","3"), "calib": ("F",)}

def sacar(particion="dev", n=50, seed=7, max_por_dia=4, contexto=4):
    p = PART[particion]
    c = sqlite3.connect(f"file:{estilo.DB}?mode=ro", uri=True)
    filas = [(r[0],r[1],r[2],r[3]) for r in c.execute("""
        select id, timestamp, case when is_from_me=0 then 'G' else 'M' end, coalesce(content,'')
        from messages where chat_jid=? and coalesce(media_type,'')='' and coalesce(content,'')<>''
          and timestamp < '2026-07-20' order by timestamp""", (estilo.JID,))]
    items, por_dia = [], {}
    for i in range(1, len(filas)):
        mid, ts, rol, txt = filas[i]
        if rol != 'M' or mid[-1] not in p:      # tiene que ser respuesta suya, y de la particion
            continue
        if filas[i-1][2] != 'G':                # y venir justo despues de algo de ella
            continue
        dia = ts[:10]
        if por_dia.get(dia, 0) >= max_por_dia:  # estratificado: no mas de N por dia, para que
            continue                            # los items no esten correlacionados por conversacion
        ctx = [f"{r[2]}: {r[3]}" for r in filas[max(0,i-1-contexto):i-1]]
        items.append({"id": mid, "ts": ts, "contexto": ctx,
                      "estimulo": filas[i-1][3], "real": txt})
        por_dia[dia] = por_dia.get(dia, 0) + 1
    random.Random(seed).shuffle(items)
    return items[:n]

if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else "dev"
    n    = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    it = sacar(part, n, seed)
    os.makedirs(f"{estilo.DIR}/banco", exist_ok=True)
    ruta = f"{estilo.DIR}/banco/{part}-{seed}.jsonl"
    with open(ruta, "w", encoding="utf-8") as f:
        for x in it: f.write(json.dumps(x, ensure_ascii=False)+"\n")
    print(f"{len(it)} items -> {ruta}")
    for x in it[:3]:
        print(f"\n  [{x['ts']}]")
        for l in x["contexto"][-2:]: print(f"     {l}")
        print(f"     G: {x['estimulo'][:70]}")
        print(f"  -> M: {x['real'][:70]}")
