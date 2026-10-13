#!/usr/bin/env python3
# estilo-metricas.py — cuanto se parece un conjunto de mensajes a como escribe Mikel,
# en numeros y sin ningun juez de por medio.
#
# LA IDEA CENTRAL ES EL SUELO. Comparar un candidato contra su corpus y decir "KS=0.29"
# no significa nada por si solo: hace falta saber cuanto sale cuando se comparan mensajes
# SUYOS contra su propio corpus. Ese es el suelo de ruido, y es lo que convierte un numero
# en un umbral. Sin el, cualquier objetivo que se ponga es inventado — y perseguir "que la
# divergencia baje a 0" es perseguir un imposible, porque su propia cola de vocabulario
# no deja bajar de ~0,17 con muestras de este tamaño.
#
# El suelo se remuestrea EN CADA CORRIDA y con el tamaño del candidato, nunca se cachea:
# depende de n, y un suelo de otro n compara peras con manzanas.
#
# Se usa la h de Cohen y no una diferencia en puntos porcentuales porque la h es
# independiente del tamaño de muestra: un banco de 200 y uno de 400 dan la misma h. Con
# diferencias crudas, subir n empeora el numero aunque el estilo mejore, y se acaba
# eligiendo n para que salga bonito.
#
# El titular es el PEOR de los rasgos, nunca el promedio: promediar esconde justo el
# defecto que hay que arreglar (hoy la coma, con h=1,03, mientras el promedio sale comodo).

import json, math, os, random, re, sqlite3, statistics, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estilo

def ks(a, b):
    """Kolmogorov-Smirnov entre dos muestras: la maxima distancia entre sus acumuladas."""
    a, b = sorted(a), sorted(b)
    if not a or not b: return 1.0
    todos = sorted(set(a) | set(b))
    d = 0.0
    ia = ib = 0
    for x in todos:
        while ia < len(a) and a[ia] <= x: ia += 1
        while ib < len(b) and b[ib] <= x: ib += 1
        d = max(d, abs(ia/len(a) - ib/len(b)))
    return d

def js(ca, cb):
    """Divergencia de Jensen-Shannon entre dos distribuciones de unigramas."""
    na, nb = sum(ca.values()), sum(cb.values())
    if not na or not nb: return 1.0
    d = 0.0
    for w in set(ca) | set(cb):
        p, q = ca[w]/na, cb[w]/nb
        m = (p+q)/2
        if p: d += 0.5*p*math.log2(p/m)
        if q: d += 0.5*q*math.log2(q/m)
    return d

def h_cohen(p1, p2):
    """h de Cohen: diferencia de proporciones independiente del tamaño de muestra."""
    f = lambda p: 2*math.asin(math.sqrt(min(max(p, 0.0), 1.0)))
    return abs(f(p1) - f(p2))

TOKS = lambda ms: Counter(w for m in ms for w in estilo.TOK.findall(m.lower()))

def perfil(ms):
    n = len(ms)
    tot = Counter()
    for m in ms:
        for k, v in estilo.rasgos(m).items():
            if v: tot[k] += 1
    return {k: tot[k]/n for k in estilo.rasgos("")}, \
           [len(m) for m in ms], [len(m.split()) for m in ms], TOKS(ms)

def tilde_por_largo(ms):
    """Tasa de tildes por tramo de longitud.

    Medirla en crudo enganya: la tasa sube del 4% en mensajes de una palabra al 75% en
    los de mas de quince, porque es el autocorrector y no una decision. Si el candidato
    escribe mas corto, su tasa cruda baja aunque acentue igual de bien. Hay que comparar
    a igual longitud."""
    b = {}
    for m in ms:
        k = len(m.split())
        key = "1" if k == 1 else "2-3" if k <= 3 else "4-7" if k <= 7 else "8-15" if k <= 15 else "16+"
        b.setdefault(key, []).append(bool(re.search(r"[áéíóúüñÁÉÍÓÚÑ]", m)))
    return {k: (sum(v)/len(v), len(v)) for k, v in b.items()}

def comparar(cand, ref, etiqueta="candidato", reps=12, seed=7):
    """cand vs ref, con el suelo remuestreado de ref contra si misma al tamaño de cand."""
    pc, lc, wc, tc = perfil(cand)
    pr, lr, wr, tr = perfil(ref)
    res = {"etiqueta": etiqueta, "n_candidato": len(cand), "n_referencia": len(ref)}
    res["ks_chars"]    = ks(lc, lr)
    res["ks_palabras"] = ks(wc, wr)
    res["js_unigramas"] = js(tc, tr)
    rasgos = {k: {"candidato": pc[k], "real": pr[k], "h": h_cohen(pc[k], pr[k])} for k in pc}
    res["rasgos"] = rasgos
    peor = max(rasgos.items(), key=lambda kv: kv[1]["h"])
    res["max_h"] = peor[1]["h"]; res["rasgo_peor"] = peor[0]
    res["tilde_por_largo"] = {"candidato": tilde_por_largo(cand), "real": tilde_por_largo(ref)}

    # --- EL SUELO: ref contra si misma, al tamaño del candidato ---
    rnd = random.Random(seed)
    sk, sw, sj, sh = [], [], [], []
    for _ in range(reps):
        sub = rnd.sample(ref, min(len(cand), len(ref)//2))
        resto = ref  # el corpus entero es la referencia, igual que para el candidato
        ps, ls, ws, ts = perfil(sub)
        sk.append(ks(ls, lr)); sw.append(ks(ws, wr)); sj.append(js(ts, tr))
        sh.append(max(h_cohen(ps[k], pr[k]) for k in ps))
    res["suelo"] = {"ks_chars": statistics.mean(sk), "ks_chars_max": max(sk),
                    "ks_palabras": statistics.mean(sw), "ks_palabras_max": max(sw),
                    "js_unigramas": statistics.mean(sj), "js_unigramas_max": max(sj),
                    "max_h": statistics.mean(sh), "max_h_max": max(sh), "reps": reps}
    return res

def imprimir(r):
    s = r["suelo"]
    print(f"\n{'='*78}\n{r['etiqueta']}   n={r['n_candidato']}   referencia n={r['n_referencia']}\n{'='*78}")
    print(f"{'metrica':<20}{'candidato':>12}{'suelo (el vs el)':>20}{'factor':>10}   veredicto")
    print("-"*78)
    for k, sk_, smax in (("ks_chars","ks_chars","ks_chars_max"),
                         ("ks_palabras","ks_palabras","ks_palabras_max"),
                         ("js_unigramas","js_unigramas","js_unigramas_max"),
                         ("max_h","max_h","max_h_max")):
        v, base, tope = r[k], s[sk_], s[smax]
        f = v/base if base else 0
        ok = "dentro" if v <= tope else f"{f:.1f}x el suelo"
        print(f"{k:<20}{v:>12.3f}{base:>13.3f} (max {tope:.3f}){f:>9.1f}x   {ok}")
    print(f"\n  rasgo peor: {r['rasgo_peor']}  (h={r['max_h']:.3f})")
    t = r.get("tilde_por_largo")
    if t:
        print("\n  tildes A IGUAL LONGITUD (comparar la tasa cruda enganya):")
        orden = ["1","2-3","4-7","8-15","16+"]
        print("    " + "".join(f"{k:>12}" for k in orden if k in t["real"]))
        for et2, d in (("candidato", t["candidato"]), ("real", t["real"])):
            print(f"    {et2:<9}" + "".join(
                f"{d[k][0]*100:>10.1f}%" if k in d else f"{'—':>11}" for k in orden if k in t["real"]))
    print(f"\n{'rasgo':<16}{'candidato':>12}{'real':>10}{'h Cohen':>10}")
    print("-"*48)
    for k, v in sorted(r["rasgos"].items(), key=lambda kv: -kv[1]["h"]):
        print(f"{k:<16}{v['candidato']*100:>11.1f}%{v['real']*100:>9.1f}%{v['h']:>10.3f}")

if __name__ == "__main__":
    estilo.cargar()
    c = sqlite3.connect(f"file:{estilo.DB}?mode=ro", uri=True)
    q = lambda cond: [r[0] for r in c.execute(
        "select content from messages where chat_jid=? and coalesce(media_type,'')='' "
        "and coalesce(content,'')<>'' and " + cond, (estilo.JID,))]
    real  = q("is_from_me=1 and sender<>'__tulpa__'")
    tulpa = q("sender='__tulpa__'")
    r = comparar(tulpa, real, "v0-observado — los mensajes que la tulpa mando DE VERDAD")
    imprimir(r)
    os.makedirs(f"{estilo.DIR}/corridas", exist_ok=True)
    json.dump(r, open(f"{estilo.DIR}/corridas/v0-observado.json","w"), indent=1, ensure_ascii=False)
    print(f"\nguardado en estilo/corridas/v0-observado.json")
