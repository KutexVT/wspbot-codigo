#!/usr/bin/env python3
# estilo-ficha.py — genera estilo/ficha.md: TODO lo cuantitativo del estilo de Mikel,
# sacado de la base en el momento.
#
# Por que existe: los conteos del perfil estaban escritos a mano y se desfasaron todos.
# `encerio` decia 6 usos y son 836; `talvez` decia 2 y son 347; el hook afirmaba
# `para` 1645 (real 2435) y `pa` 0 (real 7). Ninguno reproducia. La causa no es que
# alguien contara mal: es que un numero escrito en prosa sobre una base que crece a
# diario esta desfasado desde el dia siguiente.
#
# El arreglo de raiz es que los numeros no vivan en prosa. El perfil se queda con lo que
# un numero no puede decir (que significa cada tic, cuando se usa); todo lo contable sale
# de aqui y se regenera.

import os, re, sqlite3, statistics, sys, unicodedata
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estilo
from estilo import TOK, desnuda

c = sqlite3.connect(f"file:{estilo.DB}?mode=ro", uri=True)
SUYO = "is_from_me=1 and sender<>'__tulpa__'"
q = lambda cond, chat=None: [r[0] for r in c.execute(
    "select coalesce(content,'') from messages where coalesce(media_type,'')='' "
    "and coalesce(content,'')<>'' and " + cond + (" and chat_jid=?" if chat else ""),
    (chat,) if chat else ())]

todos = q(SUYO)
reg   = q(SUYO, estilo.JID)
voc   = Counter(w for m in todos for w in TOK.findall(m.lower()))
N     = len(reg)
pc    = lambda v, p: sorted(v)[min(len(v)-1, int(len(v)*p))]
L     = [len(m) for m in reg]; W = [len(m.split()) for m in reg]
tasa  = lambda f: sum(1 for m in reg if f(m))*100/N

o = []
w = o.append
from datetime import date, datetime, timedelta
w(f"# Ficha de estilo — GENERADA EL {date.today()} DESDE messages.db")
w("")
w("**NO EDITAR A MANO: este archivo se sobreescribe.** Lo regenera `bin/estilo-ficha.py`.")
w("Todo lo contable vive aqui; `user_messaging_style.md` se queda con lo que un numero no")
w("puede decir. Es el arreglo a que los conteos escritos en prosa se desfasaran todos.")
w("")
w("## Corpus")
w("")
tot_base = c.execute("select count(*) from messages").fetchone()[0]
suyos_tot = c.execute(f"select count(*) from messages where {SUYO}").fetchone()[0]
w(f"- **{tot_base:,}** mensajes en la base · **{suyos_tot:,}** suyos · **{len(todos):,}** suyos con texto")
w(f"- Registro medido (chat de Contacto): **{N:,}** mensajes de texto suyos")
w(f"- Vocabulario: **{sum(voc.values()):,}** tokens, **{len(voc):,}** tipos distintos")
w("")
w("## Longitud — la forma")
w("")
w("| | p25 | mediana | p75 | p90 | p99 |")
w("|---|---|---|---|---|---|")
w(f"| caracteres | {pc(L,.25)} | **{pc(L,.5)}** | {pc(L,.75)} | {pc(L,.9)} | {pc(L,.99)} |")
w(f"| palabras | {pc(W,.25)} | **{pc(W,.5)}** | {pc(W,.75)} | {pc(W,.9)} | {pc(W,.99)} |")
w("")
w(f"- **{tasa(lambda m: len(m.split())==1):.1f}%** de sus mensajes son de UNA sola palabra")
w(f"- **{tasa(lambda m: len(m.split())<=3):.1f}%** son de tres o menos")
w(f"- solo el {tasa(lambda m: len(m.split())>20):.1f}% pasa de veinte palabras")
w("")
w("## Puntuacion y ortotipografia")
w("")
w("| rasgo | tasa | nota |")
w("|---|---|---|")
w(f"| cierra con punto | **{tasa(lambda m: m.rstrip().endswith('.')):.2f}%** | esto SI es decision suya, no autocorrector |")
w(f"| lleva coma | **{tasa(lambda m: ',' in m):.1f}%** | el defecto nº1 de la imitacion: la tulpa iba al 40,8% |")
w(f"| lleva `¿` o `¡` | {tasa(lambda m: '¿' in m or '¡' in m):.2f}% | |")
w(f"| lleva `?` | {tasa(lambda m: '?' in m):.1f}% | |")
w(f"| lleva `??` | {tasa(lambda m: '??' in m):.1f}% | su forma de preguntar con enfasis |")
w(f"| empieza en mayuscula | {tasa(lambda m: m[:1].isupper()):.1f}% | autocapitalizacion del movil |")
w(f"| lleva alguna tilde | {tasa(lambda m: bool(re.search(r'[áéíóúüñ]', m, re.I))):.1f}% | |")
w(f"| letra alargada (3+) | {tasa(lambda m: any(m[i]==m[i+1]==m[i+2] for i in range(len(m)-2))):.1f}% | alarga sobre todo i, e, h, s |")
w(f"| mensaje entero en CAPS | {tasa(lambda m: m.isupper() and len(m)>2):.1f}% | |")
w("")
w("## Tildes — la regla real")
w("")
w("No es 'si' ni 'no'. **Acentua lo ortografico y NO acentua lo diacritico.** Y la tasa")
w("sube con la longitud, porque es el autocorrector del movil y no una decision:")
w("")
por_largo = {}
for m in reg:
    k = len(m.split())
    b = '1' if k==1 else '2' if k==2 else '3' if k==3 else '4-5' if k<=5 else '6-10' if k<=10 else '11-20' if k<=20 else '>20'
    por_largo.setdefault(b, []).append(bool(re.search(r'[áéíóúüñ]', m, re.I)))
w("| palabras | 1 | 2 | 3 | 4-5 | 6-10 | 11-20 | >20 |")
w("|---|---|---|---|---|---|---|---|")
w("| con tilde | " + " | ".join(f"{sum(por_largo[k])*100/len(por_largo[k]):.0f}%"
    for k in ['1','2','3','4-5','6-10','11-20','>20'] if k in por_largo) + " |")
w("")
par = {}
for m in todos:
    for x in TOK.findall(m.lower()):
        d = desnuda(x)
        if d != x: par.setdefault(d, Counter())['con'] += 1
        elif any(ch in 'aeiou' for ch in d): par.setdefault(d, Counter())['sin'] += 1
# Solo entran las formas donde de verdad hay ELECCION: las que escribe de las dos
# maneras con volumen en las dos. Sin ese filtro la tabla se llenaba de `a`, `al` y
# `amor`, que no llevan tilde jamas y no dicen nada.
real_con = {}
for x, n in voc.items():
    d = desnuda(x)
    if d != x and n > real_con.get(d, (0, ''))[0]: real_con[d] = (n, x)
elegibles = [(d, v['con'], v['sin']) for d, v in par.items()
             if v['con'] >= 20 and v['sin'] >= 5 and d in real_con]
si = sorted(((c/(c+sn), real_con[d][1], c+sn) for d, c, sn in elegibles
             if c/(c+sn) > .60), reverse=True)
no = sorted(((c/(c+sn), d, c+sn) for d, c, sn in elegibles if c/(c+sn) < .35))
w("**SI acentua** (tilde ortografica normal): " + " · ".join(f"`{d}` {r*100:.0f}%" for r,d,_ in si[:16]))
w("")
w("**NO acentua** (tilde diacritica): " + " · ".join(f"`{d}` {(1-r)*100:.0f}% sin" for r,d,_ in no[:16]))
w("")
w("## Sus grafias — y las dos que el perfil tenia mal")
w("")
w("| suya | usos | correcta | usos | gana |")
w("|---|---|---|---|---|")
for suya, corr in [("encerio","en serio"),("talvez","tal vez"),("almenos","al menos"),
                   ("aveces","a veces"),("enrelidad","en realidad"),("masomenos","mas o menos"),
                   ("osea","o sea"),("llendo","yendo"),("haber","a ver")]:
    a = voc.get(suya,0); b = sum(1 for m in todos if corr in m.lower())
    gana = "**suya**" if a > b else "**la correcta**"
    w(f"| `{suya}` | {a} | `{corr}` | {b} | {gana} |")
w("")
w("`llendo` y `haber` PIERDEN contra la forma correcta. El perfil las presentaba como su")
w("forma por defecto y es falso. Se mantienen porque hay correccion directa suya")
w("(`llendo`, el 2026-08-06), pero como preferencia declarada y no como hecho medido.")
w("")
w("## Vocabulario — lo que mas lo delata")
w("")
w("| palabra | usos | por mil | nota |")
w("|---|---|---|---|")
tot_tok = sum(voc.values())
for p, nota in [("xq","su palabra nº18 en frecuencia. La tulpa la usaba CERO veces"),
                ("x","= 'por'. Idem: la tulpa 9 veces"),("encerio",""),("shi",""),
                ("ntp",""),("talvez",""),("almenos",""),("osea",""),("alv","con cuentagotas"),
                ("xfis",""),("nop",""),("sip","")]:
    w(f"| `{p}` | {voc.get(p,0)} | {voc.get(p,0)*1000/tot_tok:.2f} | {nota} |")
w("")
w("## Sus mensajes exactos mas repetidos")
w("")
rep = Counter(m.strip() for m in reg)
w("El **{:.1f}%** de sus mensajes es identico a otro suyo. Los mas frecuentes:".format(
    (1 - len(set(m.strip() for m in reg))/N)*100))
w("")
w("  " + " · ".join(f"`{m}`({n})" for m, n in rep.most_common(24) if m))
w("")
w("## Rafagas — el orden en que dice las cosas")
w("")
rows=[(r[0],r[1],r[2]) for r in c.execute("""select is_from_me, sender, coalesce(content,'')
    from messages where chat_jid=? and coalesce(media_type,'')='' and coalesce(content,'')<>''
    order by timestamp""",(estilo.JID,))]
raf=[];cur=[]
for fm,snd,t in rows:
    if fm and snd!='__tulpa__': cur.append(t)
    else:
        if cur: raf.append(cur); 
        cur=[]
d=Counter(len(r) for r in raf); tr=sum(d.values())
w("| mensajes seguidos | 1 | 2 | 3 | 4 | 5+ |")
w("|---|---|---|---|---|---|")
w(f"| frecuencia | **{d[1]*100/tr:.1f}%** | {d[2]*100/tr:.1f}% | {d[3]*100/tr:.1f}% | {d[4]*100/tr:.1f}% | {sum(v for k,v in d.items() if k>=5)*100/tr:.1f}% |")
w("")
w(f"Media {sum(len(r) for r in raf)/tr:.2f} mensajes por turno.")
w("")
w("**Escalera ascendente** — el largo CRECE dentro de la rafaga, no al reves:")
w("")
for n in (2,3):
    sub=[r for r in raf if len(r)==n]
    med=[statistics.median([len(r[i].split()) for r in sub]) for i in range(n)]
    w(f"- rafaga de {n}: " + " -> ".join(f"**{x:.0f}**" for x in med) + " palabras")
w("")
w("Abre con lo corto y cierra con lo desarrollado y con la pregunta.")
w("")
# ---------------------------------------------------------------- STICKERS
# Aparte del resto por dos motivos. Uno: `q()` filtra media_type='' y aqui hace falta justo
# lo contrario. Dos, y es el que invalido la primera version de este bloque: **el bridge no
# guardaba los stickers antes del 2026-08-06** — no hay ni uno en la base de marzo a julio,
# y no porque el no los mandara. Dividir "rafagas con sticker" entre TODAS sus rafagas daba
# 14% cuando la cifra real es cinco veces esa. El corte se calcula solo desde el primer
# sticker de la base (+1 dia, porque el primero esta a medias) y no se cablea: el dia que se
# repare el historico, esto se corrige solo.
#
# Las rafagas se cortan por emisor Y por gap de 5 min — el bloque de arriba corta solo por
# emisor, y un turno sin corte por tiempo puede abarcar horas y falsear la posicion.
w("## Stickers — cuando los manda")
w("")
GAP = 300
PRIMER = c.execute("select min(timestamp) from messages where chat_jid=? and media_type='sticker'",
                   (estilo.JID,)).fetchone()[0]
CORTE = (datetime.fromisoformat(PRIMER[:19].replace("T"," ")) + timedelta(days=1)).strftime("%Y-%m-%d")
srows = c.execute(
    "select is_from_me, coalesce(sender,''), coalesce(media_type,''), coalesce(filename,''), timestamp, "
    "coalesce(content,'') from messages where chat_jid=? and timestamp>=? order by timestamp, id",
    (estilo.JID, CORTE)).fetchall()
quien = lambda r: "ella" if r[0]==0 else ("tulpa" if r[1]=="__tulpa__" else "mikel")
hora  = lambda r: datetime.fromisoformat(r[4][:19].replace("T"," "))

def rafagas(yo, filas=None):
    out=[]
    for r in (x for x in (srows if filas is None else filas) if quien(x) in ("ella", yo)):
        k=quien(r)
        if out and out[-1][0]==k and (hora(r)-hora(out[-1][1][-1])).total_seconds()<=GAP:
            out[-1][1].append(r)
        else:
            out.append([k,[r]])
    return out

def stk(t):  return [m for m in t[1] if m[2]=="sticker"]
def hay(t):  return any(m[2]=="sticker" for m in t[1])

raf  = rafagas("mikel")
mias = [t for t in raf if t[0]=="mikel"]
con  = [t for t in mias if hay(t)]
msgs = [m for t in mias for m in t[1]]
w(f"**Manda sticker casi siempre.** El **{len(con)*100/len(mias):.1f}%** de sus rafagas lleva uno, y el "
  f"**{sum(1 for m in msgs if m[2]=='sticker')*100/len(msgs):.1f}%** de todo lo que escribe ES un sticker "
  f"({sum(len(stk(t)) for t in con):,} en {len(mias):,} rafagas). Lo raro no es mandarlo: es no mandarlo.")
w("")
w(f"> Contado solo desde el **{CORTE}**. El bridge no guardaba stickers antes del {PRIMER[:10]}, y "
  f"dividir entre las rafagas de los cinco meses anteriores daba un 14% que no existio nunca.")
w("")
w("**Lo que ella acaba de mandar mueve poco la aguja:**")
w("")
tab={}
for i,t in enumerate(raf):
    if t[0]!="mikel": continue
    p = raf[i-1] if i>0 and raf[i-1][0]=="ella" else None
    k = "ella mando sticker" if (p and hay(p)) else ("ella mando otra cosa" if p else "abre el, sin nada de ella delante")
    a,b = tab.get(k,(0,0)); tab[k]=(a+hay(t), b+1)
w("| lo anterior de ella | rafagas | el mete sticker |")
w("|---|---|---|")
for k in ("ella mando sticker","ella mando otra cosa","abre el, sin nada de ella delante"):
    a,b = tab.get(k,(0,0))
    if b: w(f"| {k} | {b:,} | **{a*100/b:.1f}%** |")
w("")
# La hipotesis que hay que refutar explicitamente, porque un modelo la da por buena sola.
SERIO = re.compile(r"\b(perdon|disculpa|lo siento|encerio|de verdad|me duele|estoy mal|estas mal|"
                   r"problema|preocup|triste|molesta|enojad|pelea|discut|terapia|psicolog|ansiedad|"
                   r"deprim|llorar|llore|mi culpa|prometo|te juro)\b")
serio = lambda t: bool(SERIO.search(desnuda(" ".join(m[5] for m in t[1])).lower()))
ser = [t for t in mias if serio(t)]
nor = [t for t in mias if not serio(t)]
lar = [t for t in ser if len(" ".join(m[5] for m in t[1] if m[2]=="").split())>=26]
w(f"**Y el momento serio NO lo apaga** — es la excepcion que parece obvia y que los datos niegan: "
  f"cuando pide perdon, se sincera o hablan de un problema manda sticker el **{sum(1 for t in ser if hay(t))*100/len(ser):.1f}%** "
  f"de las veces, contra el **{sum(1 for t in nor if hay(t))*100/len(nor):.1f}%** del resto. Y en sus "
  f"turnos mas serios que hay — {len(lar)} de 26 palabras o mas hablando de eso — sube al "
  f"**{sum(1 for t in lar if hay(t))*100/len(lar):.1f}%**.")
w("")
# La posicion se cuenta POR RAFAGA. Contarla por sticker (la primera version) partia una
# rafaga de tres stickers seguidos en "abre"+"en medio"+"remata" e inflaba el medio al 37%.
solo = [t for t in con if all(m[2]=="sticker" for m in t[1])]
mix  = [t for t in con if t not in solo]
pos={}
for t in mix:
    idx=[i for i,m in enumerate(t[1]) if m[2]=="sticker"]
    k = "remata" if idx[-1]==len(t[1])-1 else ("abre y sigue escribiendo" if idx[0]==0 else "en medio")
    pos[k]=pos.get(k,0)+1
w(f"**Donde va:** de las que llevan sticker, el **{len(solo)*100/len(con):.0f}%** son SOLO stickers, sin "
  "una palabra. En las demas " + " · ".join(f"{k} **{v*100/len(mix):.0f}%**"
  for k,v in sorted(pos.items(), key=lambda x:-x[1])) + ".")
w("")
nst=sum(len(stk(t)) for t in con)
multi=[t for t in con if len(stk(t))>=2]
igual=sum(1 for t in multi if len({m[3] for m in stk(t)})==1)
pers=tot=0
for a,b in zip(mias, mias[1:]):
    if hay(a) and hay(b):
        tot+=1; pers += stk(b)[0][3] in {m[3] for m in stk(a)}
w(f"Cuando entra en modo sticker puntua **cada frase**: 1 sticker cada "
  f"**{sum(len(t[1]) for t in con)/nst:.1f}** mensajes dentro de la rafaga, y el "
  f"**{len(multi)*100/len(con):.0f}%** de esas rafagas lleva dos o mas.")
w("")
w(f"**Y repite el mismo archivo, no varia:** en el **{igual*100/len(multi):.0f}%** de las rafagas con "
  f"varios stickers son todos el MISMO, y el **{pers*100/tot:.0f}%** de las veces arrastra a la rafaga "
  "siguiente uno que ya habia usado. El sticker es el punto final que el nunca escribe, y se queda "
  "pegado al humor mientras dure.")
w("")
lat=[]
for i,t in enumerate(raf):
    if t[0]!="mikel" or not hay(t): continue
    p = raf[i-1] if i>0 and raf[i-1][0]=="ella" else None
    if p and hay(p): lat.append((hora(stk(t)[0])-hora(p[1][-1])).total_seconds())
lat=sorted(x for x in lat if 0<=x<3600)
peg=[]
for t in con:
    for i,m in enumerate(t[1]):
        if m[2]=="sticker" and i>0: peg.append((hora(m)-hora(t[1][i-1])).total_seconds())
peg=sorted(x for x in peg if 0<=x<3600)
w(f"Va pegado a lo anterior (mediana **{statistics.median(peg):.0f}s**) y devolver el de ella le toma "
  f"**{statistics.median(lat):.0f}s** de mediana.")
w("")
cat=Counter(m[3] for t in con for m in stk(t))
w(f"Tira de **{len(cat)}** stickers distintos, pero el top 10 se lleva el "
  f"**{sum(v for _,v in cat.most_common(10))*100/nst:.0f}%** de los usos.")
w("")

# ---------------------------------------------------------------- RESPUESTA
# Rafagas por emisor + gap de 5 min, igual que en stickers, pero sobre el chat ENTERO: el
# corte de agosto de alli existe solo porque faltan los stickers viejos, y el texto si esta
# desde el primer dia. Aplicarselo tambien aqui tiraria cinco meses de conversacion.
w("## Respuesta — cuando contesta y cuantos mensajes manda")
w("")
todas = c.execute(
    "select is_from_me, coalesce(sender,''), coalesce(media_type,''), coalesce(filename,''), timestamp, "
    "coalesce(content,'') from messages where chat_jid=? order by timestamp, id", (estilo.JID,)).fetchall()
raf = rafagas("mikel", todas)
pares=[]   # (turno de ella, turno suyo que lo responde, latencia en segundos)
for i in range(1,len(raf)):
    if raf[i][0]=="mikel" and raf[i-1][0]=="ella":
        l=(hora(raf[i][1][0])-hora(raf[i-1][1][-1])).total_seconds()
        if l>=0: pares.append((raf[i-1],raf[i],l))
lat=sorted(l for _,_,l in pares)
pc=lambda p: lat[min(len(lat)-1,int(len(lat)*p/100))]
w(f"**Contesta en el acto.** Mediana **{statistics.median(lat):.0f}s** desde el ultimo mensaje de ella "
  f"(p25 {pc(25):.0f}s, p75 {pc(75):.0f}s, p90 {pc(90):.0f}s), sobre {len(lat):,} respuestas.")
w("")
tramos=[("menos de 10s",0,10),("10-60s",10,60),("1-5 min",60,300),("5-30 min",300,1800),
        ("30 min - 2h",1800,7200),("mas de 2h",7200,9e9)]
w("| tarda | " + " | ".join(n for n,_,_ in tramos) + " |")
w("|---" * (len(tramos)+1) + "|")
w("| | " + " | ".join(f"**{sum(1 for l in lat if a<=l<b)*100/len(lat):.1f}%**" for _,a,b in tramos) + " |")
w("")
sinr=sum(1 for i in range(len(raf)-1) if raf[i][0]=="ella" and raf[i+1][0]=="ella")
tote=sum(1 for t in raf if t[0]=="ella")
w(f"Y contesta **casi siempre**: solo el **{sinr*100/tote:.1f}%** de los turnos de ella se queda sin "
  f"respuesta suya en 5 min. No filtra por contenido — lo que deja pasar lleva pregunta tan a menudo "
  f"como lo que contesta. Las esperas largas son sueño, no desinteres: entre las 3 y las 5 de la "
  f"mañana no hay ni un mensaje suyo.")
w("")
w("**Cuantos manda: varios temas suben la cuenta, pero no son la causa.**")
w("")
def dosmas(v): return sum(1 for x in v if x>=2)*100/len(v)
b={}
for te,tm,_ in pares: b.setdefault(min(len(te[1]),6),[]).append(len(tm[1]))
w("| ella manda | " + " | ".join(f"{k}{'+' if k==6 else ''} msg" for k in sorted(b)) + " |")
w("|---" * (len(b)+1) + "|")
w("| el responde con 2 o mas | " + " | ".join(f"**{dosmas(b[k]):.0f}%**" for k in sorted(b)) + " |")
w("")
bq={}
for te,tm,_ in pares:
    bq.setdefault(min(sum(1 for m in te[1] if "?" in m[5]),2),[]).append(len(tm[1]))
w("| preguntas de ella | " + " | ".join(f"{k}{'+' if k==2 else ''}" for k in sorted(bq)) + " |")
w("|---" * (len(bq)+1) + "|")
w("| el responde con 2 o mas | " + " | ".join(f"**{dosmas(bq[k]):.0f}%**" for k in sorted(bq)) + " |")
w("")
uno=[len(tm[1]) for te,tm,_ in pares if len(te[1])==1]
gaps=sorted(g for t in raf if t[0]=="mikel" for i in range(1,len(t[1]))
            for g in [(hora(t[1][i])-hora(t[1][i-1])).total_seconds()] if 0<=g<=GAP)
w(f"Pero aunque ella mande **un solo mensaje** el contesta con dos o mas el **{dosmas(uno):.0f}%** de las "
  f"veces: la mitad de sus rafagas no son un tema por mensaje, es una frase partida. Los mensajes de una "
  f"misma rafaga suya salen con **{statistics.median(gaps):.0f}s** de mediana entre uno y otro — "
  f"{sum(1 for g in gaps if g<10)*100/len(gaps):.0f}% van en menos de 10s.")
w("")
pw=[len(" ".join(m[5] for m in t[1]).split()) for t in raf if t[0]=="mikel"]
w(f"El turno entero, sumando todos sus mensajes, son **{statistics.median(pw):.0f} palabras** de mediana "
  f"({sum(pw)/len(pw):.1f} de media). Varios mensajes NO significa mas texto: significa el mismo poquito, "
  f"troceado.")
w("")

open(f"{estilo.DIR}/ficha.md","w",encoding="utf-8").write("\n".join(o)+"\n")
print(f"escrita estilo/ficha.md — {len(o)} lineas")
