#!/usr/bin/env python3
# tabla.py — pone todas las versiones medidas en una tabla, para ver si se mejora o no.
# Es el archivo que hace que esto sea un experimento y no una sensacion.
import json, sys, os, glob, statistics, sqlite3
sys.path.insert(0, "/home/kutex/WSP Bot/bin")
import estilo
from estilo_metricas import comparar

estilo.cargar()
c = sqlite3.connect(f"file:{estilo.DB}?mode=ro", uri=True)
real = [r[0] for r in c.execute("""select content from messages where chat_jid=?
    and is_from_me=1 and sender<>'__tulpa__' and coalesce(media_type,'')=''
    and coalesce(content,'')<>''""", (estilo.JID,))]
idx = estilo.cargar()

filas = []
# v0-observado: lo que la tulpa mando DE VERDAD con el sistema viejo. Baseline gratis.
tulpa = [r[0] for r in c.execute("""select content from messages where chat_jid=?
    and sender='__tulpa__' and coalesce(media_type,'')='' and coalesce(content,'')<>''""",
    (estilo.JID,))]
filas.append(("v0-observado", tulpa, None))

for p in sorted(glob.glob(f"{estilo.DIR}/corridas/v*-dev7.jsonl")):
    et = os.path.basename(p).split("-")[0]
    its = [json.loads(l) for l in open(p, encoding="utf-8") if json.loads(l).get("generado")]
    msgs = [l for i in its for l in i["generado"].split("\n") if l.strip()]
    burb = statistics.mean([len([x for x in i["generado"].split("\n") if x.strip()]) for i in its])
    filas.append((f"{et}-generado", msgs, burb))

print(f"\n{'version':<16}{'n':>6}{'KS ch':>8}{'KS pal':>8}{'max h':>8}{'peor rasgo':>14}"
      f"{'msg/turno':>11}{'ERR lint':>10}")
print("-"*82)
for et, msgs, burb in filas:
    r = comparar(msgs, real, et)
    err = sum(1 for m in msgs if any(x["nivel"]=="ERROR" for x in estilo.revisar(m, idx)))
    b = f"{burb:.2f}" if burb else "—"
    print(f"{et:<16}{len(msgs):>6}{r['ks_chars']:>8.3f}{r['ks_palabras']:>8.3f}"
          f"{r['max_h']:>8.3f}{r['rasgo_peor']:>14}{b:>11}{err*100/len(msgs):>9.1f}%")
    s = r["suelo"]
print("-"*82)
print(f"{'SUELO (el vs el)':<16}{'':>6}{s['ks_chars']:>8.3f}{s['ks_palabras']:>8.3f}"
      f"{s['max_h']:>8.3f}{'—':>14}{'2.35':>11}{'1.2':>9}%")
print("\n  El suelo es lo que sale comparando mensajes SUYOS contra su propio corpus.")
print("  Es el objetivo real: por debajo de eso no se puede bajar.")
