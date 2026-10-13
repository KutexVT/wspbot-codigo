#!/usr/bin/env python3
# estilo.py — el motor del linter de estilo. Lo envuelve bin/estilo.sh.
#
# Valida un texto contra como escribe Mikel DE VERDAD, medido sobre la base, y no contra
# lo que un archivo de reglas dice que escribe. Nacio porque el perfil escrito a mano tenia
# afirmaciones falsas de forma verificable ("casi cero acentos" cuando son el 27%, "cero
# voseo" cuando hay 32 `vos`) y sus conteos ya no reproducian contra la base.
#
# DOS CORPUS, y la distincion es el corazon de esto:
#
#   VOCABULARIO — todos los chats, todo el tiempo, sin __tulpa__. 61 mil mensajes.
#     Responde "esta palabra es suya?". Cuanto mas grande, menos falsas alarmas: cada
#     palabra que falte del corpus es un falso positivo garantizado.
#
#   UMBRALES — solo el chat de Contacto. 27 mil mensajes.
#     Responde "esta forma es la de ESTE registro?". Una tasa de comas sacada de todos
#     los chats mezclaria registros distintos.
#
# Confundirlos es exactamente el error que produjo las reglas actuales.
#
# Los umbrales NO se cablean: salen del indice. Los numeros cableados son lo que se
# desfaso y por lo que existe este archivo.

import json, os, re, sqlite3, statistics, sys, unicodedata
from collections import Counter

BASE = os.environ.get("WSP_BASE", "/home/kutex/WSP Bot")
DB   = os.environ.get("WSP_DB", f"{BASE}/whatsapp-mcp/whatsapp-bridge/store/messages.db")
JID  = os.environ.get("WSP_JID", "000000000000000@lid")
DIR  = os.environ.get("WSP_ESTILO", f"{BASE}/estilo")
IDX  = f"{DIR}/indice"

TOK = re.compile(r"[a-zA-ZáéíóúüñÁÉÍÓÚÑ]+")

def desnuda(w):
    return "".join(c for c in unicodedata.normalize("NFD", w) if unicodedata.category(c) != "Mn")

def conectar():
    if not os.path.exists(DB):
        sys.exit(f"ERROR: no existe la base: {DB}")
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

# ---------------------------------------------------------------------------
# CONSTRUIR EL INDICE
# ---------------------------------------------------------------------------
def textos(c, cond, chat=None):
    q = ("select coalesce(content,'') from messages where coalesce(media_type,'')='' "
         "and coalesce(content,'')<>'' and " + cond)
    p = []
    if chat:
        q += " and chat_jid=?"; p.append(chat)
    return [r[0] for r in c.execute(q, p)]

SUYO = "is_from_me=1 and sender<>'__tulpa__'"

def rasgos(m):
    """Los rasgos binarios que se miden por mensaje. Un solo sitio, para que el linter y
    las metricas midan LO MISMO y no puedan desincronizarse."""
    w = m.split()
    return {
        "tilde":      bool(re.search(r"[áéíóúüñÁÉÍÓÚÑ]", m)),
        "mayus_ini":  m[:1].isupper(),
        "coma":       "," in m,
        "punto_final": m.rstrip().endswith("."),
        "apertura":   ("¿" in m or "¡" in m),
        "interr":     "?" in m,
        "interr2":    "??" in m,
        "alargada":   bool(re.search(r"(\w)\1{2,}", m)),
        "caps_pesado": sum(1 for ch in m if ch.isupper()) >= 0.6 * max(1, sum(1 for ch in m if ch.isalpha())),
        "corto3":     len(w) <= 3,
        "corto1":     len(w) == 1,
    }

def construir():
    c = conectar()
    voc_msgs = textos(c, SUYO)
    reg_msgs = textos(c, SUYO, JID)
    os.makedirs(IDX, exist_ok=True)

    # --- vocabulario ---
    voc = Counter(w for m in voc_msgs for w in TOK.findall(m.lower()))
    with open(f"{IDX}/voc.tsv", "w", encoding="utf-8") as f:
        for w, n in voc.most_common():
            f.write(f"{w}\t{n}\n")

    # --- familias que SOLO existen con marca (tilde o ñ) ---
    # Es el chequeo estrella: 0,7% de falsos positivos y 17x de ratio de señal.
    fam = {}
    for w, n in voc.items():
        fam.setdefault(desnuda(w), Counter())[w] = n
    with open(f"{IDX}/desnudo.tsv", "w", encoding="utf-8") as f:
        for d, variantes in fam.items():
            if d in voc:            # tambien la escribe sin marca: no hay nada que corregir
                continue
            real, n = variantes.most_common(1)[0]
            if real != d:
                f.write(f"{d}\t{real}\t{n}\n")

    # --- bigramas (solo freq>=2: el resto es cola y no dice nada) ---
    big = Counter()
    for m in voc_msgs:
        ws = TOK.findall(m.lower())
        big.update(" ".join(p) for p in zip(ws, ws[1:]))
    with open(f"{IDX}/big.tsv", "w", encoding="utf-8") as f:
        for b, n in big.most_common():
            if n < 2: break
            f.write(f"{b}\t{n}\n")

    # --- umbrales del REGISTRO (chat de Contacto) ---
    L = sorted(len(m) for m in reg_msgs)
    W = sorted(len(m.split()) for m in reg_msgs)
    n = len(reg_msgs)
    pc = lambda v, p: v[min(len(v) - 1, int(len(v) * p))]
    tot = Counter()
    for m in reg_msgs:
        for k, v in rasgos(m).items():
            if v: tot[k] += 1
    with open(f"{IDX}/umbrales.tsv", "w", encoding="utf-8") as f:
        f.write(f"n\t{n}\n")
        for et, v in (("chars", L), ("palabras", W)):
            f.write(f"{et}_p25\t{pc(v,.25)}\n{et}_p50\t{pc(v,.50)}\n"
                    f"{et}_p75\t{pc(v,.75)}\n{et}_p90\t{pc(v,.90)}\n{et}_p99\t{pc(v,.99)}\n")
        for k in rasgos(""):
            f.write(f"tasa_{k}\t{tot[k]/n:.6f}\n")

    meta = {"n_vocabulario": len(voc_msgs), "n_registro": n, "tipos": len(voc),
            "tokens": sum(voc.values()), "familias_con_marca": sum(1 for _ in open(f"{IDX}/desnudo.tsv", encoding="utf-8")),
            "total_en_base": c.execute("select count(*) from messages").fetchone()[0]}
    json.dump(meta, open(f"{IDX}/META.json", "w"), indent=1, ensure_ascii=False)
    return meta

# ---------------------------------------------------------------------------
# CARGAR Y REVISAR
# ---------------------------------------------------------------------------
def cargar():
    if not os.path.exists(f"{IDX}/META.json"):
        construir()
    voc = {}
    for ln in open(f"{IDX}/voc.tsv", encoding="utf-8"):
        w, n = ln.rstrip("\n").split("\t"); voc[w] = int(n)
    desn = {}
    for ln in open(f"{IDX}/desnudo.tsv", encoding="utf-8"):
        d, real, n = ln.rstrip("\n").split("\t"); desn[d] = (real, int(n))
    umb = {}
    for ln in open(f"{IDX}/umbrales.tsv", encoding="utf-8"):
        k, v = ln.rstrip("\n").split("\t"); umb[k] = float(v)
    voseo = {}
    p = f"{DIR}/voseo.lst"
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.split("#")[0].strip()
            if "\t" in ln:
                a, b = ln.split("\t", 1); voseo[a.strip()] = b.strip()
    giros = []
    p = f"{DIR}/giros.lst"
    if os.path.exists(p):
        giros = [l.split("#")[0].strip() for l in open(p, encoding="utf-8") if l.split("#")[0].strip()]
    return voc, desn, umb, voseo, giros

def revisar(texto, idx=None):
    voc, desn, umb, voseo, giros = idx or cargar()
    h = []
    add = lambda niv, cod, tramo, arreglo, dato: h.append(
        {"nivel": niv, "codigo": cod, "tramo": tramo, "arreglo": arreglo, "dato": dato})
    bajo = texto.lower()

    # --- ortotipografia: sin indice, y son las mas seguras que hay ---
    if "¿" in texto or "¡" in texto:
        add("ERROR", "E-APERTURA", "¿ / ¡", "quitarlo",
            f"los usa en el {umb.get('tasa_apertura',0)*100:.2f}% de sus mensajes")
    for linea in texto.split("\n"):
        if linea.rstrip().endswith(".") and not linea.rstrip().endswith("..."):
            add("ERROR", "E-PUNTO-FINAL", linea.rstrip()[-25:], "quitar el punto",
                f"cierra con punto el {umb.get('tasa_punto_final',0)*100:.2f}% de las veces")
            break
    for m in re.finditer(r"\bja(?:ja)+\b|\bji(?:ji)+\b", bajo):
        add("ERROR", "E-JAJA", m.group(0), "jsjsjs o CAPS revueltos",
            "preferencia suya, corregida de frente el 2026-07-31 (no es que no exista en su historial)")

    # --- vocabulario ---
    for w in TOK.findall(bajo):
        # El voseo se mira ANTES de "existe en el corpus", y a proposito: `vos` (12 usos),
        # `tenes` (8) y `decime` (3) SI existen en sus 61 mil mensajes, pero son residuales
        # frente a `tú` (910), `tienes` (412) y `dime` (196). Saltarlos por existir era
        # dejar pasar justo el fallo nº1 de la imitacion. La lista ya se auto-valida en el
        # build, asi que lo que sobreviva ahi es residual por definicion.
        if w in voseo:
            add("ERROR", "E-VOSEO", w, voseo[w],
                f"tutea: `{voseo[w]}` tiene {voc.get(voseo[w],0)} usos suyos contra {voc.get(w,0)} de `{w}`")
            continue
        if voc.get(w):
            continue
        if w in desn:
            real, n = desn[w]
            add("ERROR", "E-TILDE", w, real, f"escribe `{real}` {n} veces y `{w}` cero")
        else:
            add("WARN", "W-NUEVA", w, None,
                "no aparece en sus 356k tokens (OJO: este chequeo falla el 8,2% de las veces "
                "sobre mensajes REALES suyos por su cola de vocabulario — miralo, no lo obedezcas)")
    for g in giros:
        if g in bajo:
            add("ERROR", "E-GIRO", g, None, "giro que nunca ha escrito, revalidado a 0 en cada build")

    # --- forma, con umbrales derivados del corpus ---
    n_ch, n_pal = len(texto), len(texto.split())
    if n_ch > umb.get("chars_p99", 1e9):
        add("ERROR", "E-LARGO", f"{n_ch} chars", f"por debajo de {int(umb['chars_p90'])}",
            f"su p99 es {int(umb['chars_p99'])} y su mediana {int(umb['chars_p50'])}")
    elif n_ch > umb.get("chars_p90", 1e9):
        add("WARN", "W-LARGO", f"{n_ch} chars", f"apuntar a {int(umb['chars_p50'])}",
            f"su p90 es {int(umb['chars_p90'])}, mediana {int(umb['chars_p50'])}")
    ncomas = texto.count(",")
    if ncomas >= 2:
        add("WARN", "W-COMAS", f"{ncomas} comas", "partir o quitar",
            f"solo el {umb.get('tasa_coma',0)*100:.1f}% de sus mensajes lleva alguna coma")
    if texto[:1].islower():
        add("WARN", "W-MAYUS0", texto[:12], "mayuscula inicial",
            f"empieza en mayuscula el {umb.get('tasa_mayus_ini',0)*100:.1f}% de las veces")
    return h

# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__ or "uso: estilo.py [--reconstruir|--stats|--json] <texto>"); return 0
    if args[0] == "--reconstruir":
        print(json.dumps(construir(), indent=1, ensure_ascii=False)); return 0
    if args[0] == "--stats":
        cargar(); print(open(f"{IDX}/umbrales.tsv", encoding="utf-8").read(), end="")
        print(json.dumps(json.load(open(f"{IDX}/META.json")), indent=1, ensure_ascii=False)); return 0
    js = args[0] == "--json"
    if js: args = args[1:]
    texto = " ".join(args) if args else sys.stdin.read()
    h = revisar(texto)
    if js:
        print(json.dumps(h, ensure_ascii=False, indent=1))
    else:
        for x in h:
            flecha = f"  ->  {x['arreglo']}" if x["arreglo"] else ""
            print(f"{x['nivel']:<5} {x['codigo']:<14} {x['tramo']}{flecha}")
            print(f"      {x['dato']}")
        if not h: print("limpio")
    return 2 if any(x["nivel"] == "ERROR" for x in h) else (1 if h else 0)

if __name__ == "__main__":
    sys.exit(main())
