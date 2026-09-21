#!/usr/bin/env python3
"""v2: font-aware title/author extraction + scored Crossref/arXiv matching."""
import os,re,sys,json,subprocess,unicodedata,difflib,random,time
import xml.etree.ElementTree as ET
import requests

ROOT="/home/sergey/Documents/paper-organizer/input"
XH="{http://www.w3.org/1999/xhtml}"
S=requests.Session(); S.headers["User-Agent"]="paper-organizer/0.2 (mailto:organizer@example.org)"

def norm(s):
    s=unicodedata.normalize("NFKD",s or "")
    s="".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9 ]',' ',s)).strip()
def sim(a,b):
    a,b=norm(a),norm(b)
    if not a or not b: return 0.0
    return difflib.SequenceMatcher(None,a,b).ratio()

# ---------- filename parsing ----------
PARTICLE=r'(?:van|von|der|den|de|di|da|dos|del|della|la|le|el|al|ibn|bin|ten|ter|op)'
NAME=rf'(?:[A-ZА-ЯЁ]\.?\s*)*(?:{PARTICLE}\s+)*[A-ZА-ЯЁ][-\'’A-Za-zА-Яа-яёЁ]+'
AUTH_RE=re.compile(rf'^{NAME}$')
def parse_filename(base):
    name=re.sub(r'\.(pdf|djvu|ps|dvi)$','',base,flags=re.I)
    name=re.sub(r'\s*\[[^\]]*\]\s*','',name).strip()
    name=re.sub(r'\s*\((?:\d{4}|copy|\d)\)\s*$','',name,flags=re.I).strip()
    m=re.split(r'\s+[-–—]\s+',name,1)
    if len(m)!=2: return None,name
    left,right=m[0].strip(),m[1].strip()
    if len(left)>95 or not right: return None,name
    parts=[p.strip() for p in re.split(r',| and | & |;',left) if p.strip()]
    if not parts: return None,name
    good=sum(1 for p in parts if AUTH_RE.match(p) or re.match(r'(?i)^et\.? ?al\.?$',p))
    if good>=len(parts)-(1 if len(parts)>1 else 0) and good>=1:
        return left,right
    return None,name

# ---------- pdf structure ----------
def page_lines(path,page=1):
    try:
        r=subprocess.run(["pdftotext","-f",str(page),"-l",str(page),"-bbox-layout","-q",path,"-"],
                         capture_output=True,timeout=60)
        root=ET.fromstring(r.stdout.decode("utf-8","replace"))
    except Exception: return []
    out=[]
    for ln in root.iter(XH+"line"):
        txt=" ".join((w.text or "").strip() for w in ln.iter(XH+"word") if (w.text or "").strip())
        if not txt: continue
        h=float(ln.get("yMax",0))-float(ln.get("yMin",0))
        out.append(dict(h=round(h,2),y=float(ln.get("yMin",0)),x=float(ln.get("xMin",0)),t=txt))
    return out

def plain_text(path,pages=2):
    try:
        r=subprocess.run(["pdftotext","-f","1","-l",str(pages),"-q",path,"-"],capture_output=True,timeout=60)
        return r.stdout.decode("utf-8","replace")
    except Exception: return ""

JUNK=re.compile(r'(?i)^(abstract|introduction|contents|arxiv|journal|vol\.?|volume|no\.?\s*\d|pp?\.|copyright|©|received|communicated|doi|issn|isbn|printed in|all rights|www\.|http|[\d\s.,;:()\[\]-]+)$')
def extract_title(lines):
    if not lines: return None
    top=[l for l in lines if l["y"]<max(l["y"] for l in lines)*0.62] or lines
    mx=max(l["h"] for l in top)
    band=[l for l in top if l["h"]>=mx*0.92]
    # group contiguous lines of that size (multi-line titles)
    chunks,cur=[],[]
    for l in top:
        if l["h"]>=mx*0.92 and not JUNK.match(l["t"].strip()):
            if cur and abs(l["y"]-cur[-1]["y"])>3.2*l["h"]: chunks.append(cur); cur=[]
            cur.append(l)
        elif cur: chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    chunks=[c for c in chunks if c]
    if not chunks: return None
    best=max(chunks,key=lambda c:sum(len(l["t"]) for l in c))
    t=" ".join(l["t"] for l in best)
    t=re.sub(r'\s*[\*†‡§¶]+\s*$','',t).strip()
    t=re.sub(r'\s+',' ',t)
    if len(t)<6 or len(t)>300: return None
    return t

# ---------- identifiers ----------
DOI_RE=re.compile(r'\b(10\.\d{4,9}/[^\s"<>,;]{3,})',re.I)
AX_NEW=re.compile(r'arXiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?',re.I)
AX_OLD=re.compile(r'arXiv[:\s]*([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?',re.I)
def clean_doi(d):
    d=d.rstrip('.,;:)]}\'"')
    d=re.sub(r'(?i)\.(pdf|html?|xml)$','',d)
    for stop in ('http','www.','arXiv'):
        i=d.find(stop)
        if i>0: d=d[:i]
    return d.rstrip('.,;:)]}')
def embedded_ids(txt):
    ax=AX_OLD.search(txt) or AX_NEW.search(txt)
    doi=DOI_RE.search(txt)
    return (ax.group(1) if ax else None, clean_doi(doi.group(1)) if doi else None)

# ---------- api ----------
def crossref_query(q,rows=5):
    try:
        r=S.get("https://api.crossref.org/works",params={"query.bibliographic":q,"rows":rows,
            "select":"DOI,title,author,issued,container-title,type,page,volume,issue,publisher,subtitle"},timeout=25)
        return r.json()["message"]["items"] if r.status_code==200 else []
    except Exception: return []
def arxiv_title(title,rows=4):
    try:
        r=S.get("https://export.arxiv.org/api/query",
                params={"search_query":'ti:"%s"'%re.sub(r'["\\]','',title),"max_results":rows},timeout=25)
        if r.status_code!=200: return []
    except Exception: return []
    out=[]
    for m in re.finditer(r'<entry>(.*?)</entry>',r.text,re.S):
        e=m.group(1)
        i=re.search(r'<id>http://arxiv\.org/abs/([^<]+)</id>',e); t=re.search(r'<title>(.*?)</title>',e,re.S)
        a=[x.group(1).strip() for x in re.finditer(r'<name>(.*?)</name>',e,re.S)]
        if i and t: out.append(dict(id=re.sub(r'v\d+$','',i.group(1)),title=' '.join(t.group(1).split()),authors=a))
    return out

def author_last(a):
    if isinstance(a,dict): return norm(a.get("family") or a.get("name") or "")
    return norm(a.split()[-1]) if a.split() else ""
def score(cand_title,cand_authors,want_title,want_authors):
    s=sim(want_title,cand_title)
    wa={norm(x) for x in re.findall(r'[A-ZА-ЯЁ][-\'’A-Za-zА-Яа-яёЁ]{2,}',want_authors or '')}
    ca={author_last(a) for a in (cand_authors or [])}
    ca={c for c in ca if c}
    bonus=0.0
    if wa and ca: bonus=0.12*len(wa&ca)/max(1,min(len(wa),len(ca)))
    return min(1.0,s+bonus),s

def resolve(path):
    base=os.path.basename(path)
    fauth,ftitle=parse_filename(base)
    lines=page_lines(path)
    txt=plain_text(path)
    ptitle=extract_title(lines)
    ax,doi=embedded_ids(txt)
    rec=dict(file=os.path.relpath(path,ROOT),fauth=fauth,ftitle=ftitle,ptitle=ptitle,
             nchars=len(txt.strip()),src=None,id=None,score=None,matched=None)
    if ax: rec.update(src="arxiv-embed",id=ax); return rec
    if doi and re.match(r'10\.\d{4,9}/\S{4,}',doi): rec.update(src="doi-embed",id=doi); return rec
    cands=[]
    titles=[t for t in [ftitle,ptitle] if t]
    seen=set()
    for t in titles:
        if norm(t) in seen: continue
        seen.add(norm(t))
        for it in crossref_query((t+" "+(fauth or "")).strip()):
            ct=" ".join((it.get("title") or [""]))
            sc,raw=score(ct,it.get("author"),t,fauth or "")
            cands.append(("crossref",sc,raw,it.get("DOI"),ct))
        for e in arxiv_title(t):
            sc,raw=score(e["title"],e["authors"],t,fauth or "")
            cands.append(("arxiv",sc,raw,e["id"],e["title"]))
    if cands:
        cands.sort(key=lambda c:-c[1])
        src,sc,raw,ident,ct=cands[0]
        if sc>=0.80: rec.update(src=src,id=ident,score=round(sc,3),matched=ct)
        else: rec.update(score=round(sc,3),matched=ct)
    return rec

if __name__=="__main__":
    files=[]
    for dp,_,fns in os.walk(ROOT):
        for fn in fns:
            if fn.lower().endswith(".pdf"): files.append(os.path.join(dp,fn))
    random.seed(11)
    sample=random.sample(files,int(sys.argv[1]) if len(sys.argv)>1 else 30)
    res=[]
    for p in sample:
        r=resolve(p); res.append(r)
        st="OK  " if r["src"] else "MISS"
        print(f'{st} {r["src"] or "-":12s} {str(r["id"])[:32]:32s} {str(r["score"] or ""):5s} | {r["file"][:66]}')
        if not r["src"]:
            print(f'        fname-title: {r["ftitle"][:78]}')
            print(f'        page-title : {str(r["ptitle"])[:78]}   (chars={r["nchars"]})')
            if r["matched"]: print(f'        best-match : {r["matched"][:78]}')
    json.dump(res,open("/home/sergey/Documents/paper-organizer/lab/proto2.json","w"),indent=1)
    ok=sum(1 for r in res if r["src"])
    print(f'\n=== {ok}/{len(res)} resolved ({100*ok/len(res):.0f}%) ===')
    from collections import Counter; print(Counter(r["src"] for r in res))
