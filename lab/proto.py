#!/usr/bin/env python3
"""Prototype: filename/text -> title+authors -> Crossref/arXiv -> id. Measure hit rate."""
import os,re,sys,json,time,subprocess,unicodedata,difflib,urllib.parse
import requests

ROOT="/home/sergey/Documents/paper-organizer/input"
UA="paper-organizer/0.1 (mailto:organizer@example.org)"
S=requests.Session(); S.headers["User-Agent"]=UA

def norm(s):
    s=unicodedata.normalize("NFKD",s or "")
    s="".join(c for c in s if not unicodedata.combining(c)).lower()
    s=re.sub(r'[^a-z0-9 ]',' ',s)
    return re.sub(r'\s+',' ',s).strip()

def sim(a,b): return difflib.SequenceMatcher(None,norm(a),norm(b)).ratio()

INITIALS=re.compile(r'^(?:[A-ZА-Я]\.\s*)+[-A-Za-zА-Яа-яёЁ\'’]+$')
def parse_filename(base):
    name=re.sub(r'\.(pdf|djvu|ps)$','',base,flags=re.I)
    name=re.sub(r'\s*\[[^\]]*\]\s*$','',name).strip()
    if " - " not in name: return None,name
    left,right=name.split(" - ",1)
    # left looks like an author list?
    parts=re.split(r',| and | & ',left)
    parts=[p.strip() for p in parts if p.strip()]
    looks=sum(1 for p in parts if INITIALS.match(p) or re.match(r'^[A-ZА-Я][a-zа-я]+$',p) or 'et al' in p.lower())
    if parts and looks>=max(1,len(parts)-1) and len(left)<90:
        return left,right
    return None,name

def first_text(path,pages=2):
    try:
        r=subprocess.run(["pdftotext","-f","1","-l",str(pages),"-q",path,"-"],capture_output=True,timeout=40)
        return r.stdout.decode("utf-8","replace")
    except Exception: return ""

def crossref(query,rows=5):
    try:
        r=S.get("https://api.crossref.org/works",params={"query.bibliographic":query,"rows":rows,
                 "select":"DOI,title,author,issued,container-title,type,page,volume,issue,publisher"},timeout=25)
        if r.status_code!=200: return []
        return r.json()["message"]["items"]
    except Exception: return []

def arxiv(title,rows=4):
    q='ti:"%s"'%re.sub(r'["\\]','',title)
    try:
        r=S.get("https://export.arxiv.org/api/query",params={"search_query":q,"max_results":rows},timeout=25)
        if r.status_code!=200: return []
    except Exception: return []
    out=[]
    for m in re.finditer(r'<entry>(.*?)</entry>',r.text,re.S):
        e=m.group(1)
        tid=re.search(r'<id>http://arxiv.org/abs/([^<]+)</id>',e)
        ti=re.search(r'<title>(.*?)</title>',e,re.S)
        if tid and ti: out.append((re.sub(r'v\d+$','',tid.group(1)),' '.join(ti.group(1).split())))
    return out

def run(files):
    res=[]
    for p in files:
        base=os.path.basename(p); rel=os.path.relpath(p,ROOT)
        auth,title=parse_filename(base)
        txt=first_text(p)
        ax=re.search(r'arXiv[:\s]*([a-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})',txt,re.I)
        doi=re.search(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+?)(?:[.,;)\s]|$)',txt)
        src=None;ident=None;score=None;found=None
        if ax: src,ident="arxiv-embed",ax.group(1)
        elif doi: src,ident="doi-embed",doi.group(1)
        else:
            q=(title+" "+(auth or "")).strip()
            best=None
            for it in crossref(q):
                t=(it.get("title") or [""])[0]
                s=sim(title,t)
                if best is None or s>best[0]: best=(s,it.get("DOI"),t)
            axr=arxiv(title)
            axbest=None
            for aid,t in axr:
                s=sim(title,t)
                if axbest is None or s>axbest[0]: axbest=(s,aid,t)
            cand=[]
            if best: cand.append(("crossref",)+best)
            if axbest: cand.append(("arxiv",)+axbest)
            cand.sort(key=lambda c:-c[1])
            if cand and cand[0][1]>=0.82:
                src,score,ident,found=cand[0][0],round(cand[0][1],3),cand[0][2],cand[0][3]
        res.append(dict(file=rel,fauth=auth,ftitle=title,src=src,id=ident,score=score,found=found))
        st="OK " if src else "MISS"
        print(f'{st} {src or "-":12s} {str(ident)[:34]:34s} {str(score or ""):5s} | {rel[:70]}')
        if found and score and score<0.95: print(f'        want: {title[:80]}\n        got : {found[:80]}')
    json.dump(res,open("/home/sergey/Documents/paper-organizer/lab/proto.json","w"),indent=1)
    ok=sum(1 for r in res if r["src"])
    print(f"\n=== {ok}/{len(res)} resolved ({100*ok/len(res):.0f}%) ===")
    from collections import Counter
    print(Counter(r["src"] for r in res))

if __name__=="__main__":
    import random
    files=[]
    for dp,_,fns in os.walk(ROOT):
        for fn in fns:
            if fn.lower().endswith(".pdf"): files.append(os.path.join(dp,fn))
    random.seed(11); run(random.sample(files,int(sys.argv[1]) if len(sys.argv)>1 else 30))
