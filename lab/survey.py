#!/usr/bin/env python3
"""Survey the input corpus: text layer, embedded ids, filename shape."""
import os, re, subprocess, sys, random, json, collections

ROOT = "/home/sergey/Documents/paper-organizer/input"

ARXIV_NEW = re.compile(r'(?:arXiv[:\s]*)?(\d{4}\.\d{4,5})(?:v\d+)?', re.I)
ARXIV_OLD = re.compile(r'arXiv[:\s]*([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', re.I)
DOI = re.compile(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b')
MR  = re.compile(r'\bMR\s?(\d{6,7})\b')

def text_of(path, pages=2):
    try:
        out = subprocess.run(["pdftotext","-f","1","-l",str(pages),"-q",path,"-"],
                             capture_output=True, timeout=40)
        return out.stdout.decode("utf-8","replace")
    except Exception:
        return ""

def main():
    files = []
    for dp,_,fns in os.walk(ROOT):
        for fn in fns:
            if fn.lower().endswith(".pdf"):
                files.append(os.path.join(dp,fn))
    random.seed(7)
    sample = random.sample(files, min(int(sys.argv[1]) if len(sys.argv)>1 else 120, len(files)))
    stats = collections.Counter()
    rows = []
    for p in sample:
        t = text_of(p)
        n = len(t.strip())
        stats["total"] += 1
        kind = "empty" if n < 60 else ("thin" if n < 700 else "text")
        stats["layer:"+kind] += 1
        ax = ARXIV_OLD.search(t) or (ARXIV_NEW.search(t) if re.search(r'arXiv', t, re.I) else None)
        doi = DOI.search(t)
        mr  = MR.search(t)
        if ax:  stats["has_arxiv"] += 1
        if doi: stats["has_doi"] += 1
        if mr:  stats["has_mr"] += 1
        if ax or doi: stats["has_any_id"] += 1
        base = os.path.basename(p)
        if " - " in base: stats["fname_dash"] += 1
        rows.append(dict(f=os.path.relpath(p,ROOT), chars=n, kind=kind,
                         arxiv=ax.group(1) if ax else None,
                         doi=doi.group(1)[:60] if doi else None,
                         mr=mr.group(1) if mr else None))
    print(json.dumps(stats, indent=1, sort_keys=True))
    json.dump(rows, open("/home/sergey/Documents/paper-organizer/lab/survey.json","w"), indent=1)
    print("\n--- no-text-layer examples ---")
    for r in rows:
        if r["kind"]!="text": print(f'  [{r["chars"]:5d}] {r["f"][:110]}')

main()
