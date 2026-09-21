"""Clients for the bibliographic registries we query.

Crossref  - DOIs, journals, pages, volumes (the workhorse).
arXiv     - preprints, both by id and by title.
zbMATH    - Zbl numbers and MSC classification (open API).
OpenLibrary - books by title/ISBN, for the Books shelf.

Every response goes through the cache, so a second run costs no traffic.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import urllib.request

try:
    import requests
except ImportError:                                       # pragma: no cover
    requests = None

from .cache import Cache
from .config import Config
from .record import Author, BibRecord, DocKind
from .util import is_shouting, restore_eponyms, smart_title_case

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class Http:
    """Tiny HTTP helper: cache, retries, per-host rate limiting."""

    def __init__(self, cfg: Config, cache: Cache) -> None:
        self.cfg = cfg
        self.cache = cache
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()
        if requests is not None:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = cfg.user_agent
        else:
            self.session = None       # the urllib path below is used instead

    def _throttle(self, host: str, delay: float) -> None:
        if delay <= 0:
            return
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            wait = delay - (time.time() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()

    def fetch(self, url: str, accept: str) -> tuple[int, str]:
        """One HTTP GET. The single seam over requests / urllib, and the one
        place tests need to stub."""
        if self.session is not None:
            r = self.session.get(url, timeout=self.cfg.http_timeout,
                                 headers={"Accept": accept})
            return r.status_code, r.text
        req = urllib.request.Request(
            url, headers={"User-Agent": self.cfg.user_agent, "Accept": accept})
        with urllib.request.urlopen(req, timeout=self.cfg.http_timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    def get(self, url: str, params: dict | None = None, *, delay: float = 0.0,
            accept: str = "application/json", parse: str = "json") -> Any:
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        key = f"{parse}:{full}"
        hit = self.cache.get_http(key)
        if hit is not None:
            return hit if parse == "json" else hit.get("text", "")

        host = urllib.parse.urlsplit(url).netloc
        body: Any = None
        for attempt in range(self.cfg.retries):
            self._throttle(host, delay)
            try:
                status, text = self.fetch(full, accept)
                if status == 200:
                    body = json.loads(text) if parse == "json" else text
                    break
                if status in (404, 400):
                    body = None
                    break
                if status in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1) + delay)
                    continue
                body = None
                break
            except Exception:
                time.sleep(1.0 * (attempt + 1))
        # Only successful, non-empty responses are cached. Storing a failure
        # would turn one flaky moment into a permanent "not found" for the
        # ninety days the cache lives.
        if body:
            self.cache.put_http(key, body if parse == "json" else {"text": body})
        return body


# --------------------------------------------------------------------- utils

# Registries sometimes record a contributor's role in the name field itself:
# arXiv lists "with an appendix by Kay Rulling" as an author.
_ROLE_PREFIX = re.compile(
    r"(?i)^\s*(?:and\s+)?(?:with\s+)?(?:an?\s+|the\s+)?"
    r"(?:appendix|appendices|foreword|preface|introduction|afterword|"
    r"collaboration|assistance|contributions?|help|notes)\s+"
    r"(?:by|of|from|with)\s+")


def _split_name(literal: str) -> Author:
    """Turn ``"Wilberd van der Kallen"`` into family/given parts."""
    s = re.sub(r"\s+", " ", (literal or "").strip().strip(","))
    s = _ROLE_PREFIX.sub("", s).strip()
    if not s:
        return Author()
    if "," in s:
        fam, _, giv = s.partition(",")
        return Author(family=fam.strip(), given=giv.strip())
    words = s.split()
    if len(words) == 1:
        return Author(family=words[0])
    particles = {"van", "von", "der", "den", "de", "di", "da", "dos", "del",
                 "della", "la", "le", "el", "al", "ibn", "bin", "ten", "ter", "op", "zu"}
    i = len(words) - 1
    while i > 0 and words[i - 1].lower() in particles:
        i -= 1
    # "Jean-Pierre Serre" -> family Serre; "W. van der Kallen" -> van der Kallen
    return Author(family=" ".join(words[i:]), given=" ".join(words[:i]))


def _clean_title(t: str) -> str:
    # NFKC folds the mathematical alphanumerics Crossref sometimes emits
    # ("Groups over \U0001d44d") back to plain ASCII letters.
    t = unicodedata.normalize("NFKC", t or "")
    t = re.sub(r"\s+", " ", t.strip())
    t = re.sub(r"</?(i|b|em|strong|sub|sup|scp|mml:[a-z]+)[^>]*>", "", t, flags=re.I)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.strip(" .")
    t = smart_title_case(t) if is_shouting(t) else t
    return restore_eponyms(t)


# ------------------------------------------------------------------ Crossref

class Crossref:
    BASE = "https://api.crossref.org/works"
    SELECT = ("DOI,title,subtitle,author,issued,container-title,type,page,volume,"
              "issue,publisher,ISBN,ISSN,short-container-title,alternative-id,"
              "published-print,published-online,event")

    def __init__(self, http: Http, cfg: Config) -> None:
        self.http, self.cfg = http, cfg

    def by_doi(self, doi: str) -> BibRecord | None:
        data = self.http.get(f"{self.BASE}/{urllib.parse.quote(doi, safe='')}")
        if not data or "message" not in data:
            return None
        return self._to_record(data["message"])

    def search(self, title: str, authors: str = "", rows: int = 5,
               filters: str = "") -> list[BibRecord]:
        params: dict[str, Any] = {
            "query.bibliographic": f"{title} {authors}".strip(),
            "rows": rows, "select": self.SELECT, "mailto": self.cfg.mailto,
        }
        if authors:
            params["query.author"] = authors
        if filters:
            params["filter"] = filters
        data = self.http.get(self.BASE, params)
        items = ((data or {}).get("message") or {}).get("items") or []
        return [r for r in (self._to_record(i) for i in items) if r]

    @staticmethod
    def _to_record(m: dict) -> BibRecord | None:
        title = _clean_title(" ".join(m.get("title") or []))
        if not title:
            return None
        rec = BibRecord(source="crossref")
        rec.title = title
        rec.subtitle = _clean_title(" ".join(m.get("subtitle") or []))
        for a in m.get("author") or []:
            if a.get("family") or a.get("given"):
                rec.authors.append(Author(family=(a.get("family") or "").strip(),
                                          given=(a.get("given") or "").strip()))
            elif a.get("name"):
                rec.authors.append(Author(literal=a["name"].strip()))
        for key in ("issued", "published-print", "published-online"):
            parts = ((m.get(key) or {}).get("date-parts") or [[None]])[0]
            if parts and parts[0]:
                rec.year = str(parts[0])
                break
        container = m.get("container-title") or []
        rec.journal = _clean_title(container[0]) if container else ""
        rec.publisher = (m.get("publisher") or "").strip()
        rec.volume = str(m.get("volume") or "")
        rec.number = str(m.get("issue") or "")
        rec.pages = (m.get("page") or "").replace("-", "--")
        rec.doi = (m.get("DOI") or "").lower()
        rec.isbn = (m.get("ISBN") or [""])[0]
        rec.issn = (m.get("ISSN") or [""])[0]
        rec.url = f"https://doi.org/{rec.doi}" if rec.doi else ""
        for alt in m.get("alternative-id") or []:
            if (mm := re.match(r"(?i)^MR(\d{6,7})$", str(alt).strip())):
                rec.mrnumber = mm.group(1)
        ctype = (m.get("type") or "").lower()
        rec.note = ctype
        if ctype in ("book", "monograph", "reference-book", "edited-book"):
            rec.kind = DocKind.BOOK
        elif ctype == "dissertation":
            rec.kind = DocKind.THESIS
        elif ctype == "book-chapter":
            rec.kind = DocKind.PAPER
            rec.booktitle = rec.journal
        else:
            rec.kind = DocKind.PAPER
        return rec


# --------------------------------------------------------------------- arXiv

class Arxiv:
    BASE = "https://export.arxiv.org/api/query"

    def __init__(self, http: Http, cfg: Config) -> None:
        self.http, self.cfg = http, cfg

    def by_id(self, arxiv_id: str) -> BibRecord | None:
        recs = self._query({"id_list": re.sub(r"v\d+$", "", arxiv_id), "max_results": 1})
        return recs[0] if recs else None

    def by_ids(self, ids: list[str]) -> dict[str, BibRecord]:
        """Batch lookup - arXiv accepts a comma separated id_list."""
        out: dict[str, BibRecord] = {}
        ids = [re.sub(r"v\d+$", "", i) for i in ids if i]
        for chunk_start in range(0, len(ids), 50):
            chunk = ids[chunk_start:chunk_start + 50]
            for rec in self._query({"id_list": ",".join(chunk), "max_results": len(chunk)}):
                if rec.arxiv:
                    out[rec.arxiv] = rec
        return out

    def search_title(self, title: str, rows: int = 4) -> list[BibRecord]:
        clean = re.sub(r'["\\]', " ", title).strip()
        if len(clean) < 6:
            return []
        return self._query({"search_query": f'ti:"{clean}"', "max_results": rows})

    def _query(self, params: dict) -> list[BibRecord]:
        text = self.http.get(self.BASE, params, delay=self.cfg.arxiv_delay,
                             accept="application/atom+xml", parse="text")
        if not text:
            return []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        out = []
        for entry in root.iter(ATOM + "entry"):
            rec = self._to_record(entry)
            if rec:
                out.append(rec)
        return out

    @staticmethod
    def _to_record(entry: ET.Element) -> BibRecord | None:
        idel = entry.find(ATOM + "id")
        tel = entry.find(ATOM + "title")
        if idel is None or tel is None or not (idel.text or "").strip():
            return None
        if (entry.findtext(ATOM + "summary") or "").startswith("Error"):
            return None
        rec = BibRecord(source="arxiv", kind=DocKind.PAPER)
        rec.arxiv = re.sub(r"v\d+$", "", (idel.text or "").rsplit("/abs/", 1)[-1].strip())
        rec.title = _clean_title(tel.text or "")
        for a in entry.iter(ATOM + "author"):
            nm = (a.findtext(ATOM + "name") or "").strip()
            if nm:
                rec.authors.append(_split_name(nm))
        published = entry.findtext(ATOM + "published") or ""
        rec.year = published[:4] if published[:4].isdigit() else ""
        rec.doi = (entry.findtext(ARXIV_NS + "doi") or "").strip().lower()
        jref = (entry.findtext(ARXIV_NS + "journal_ref") or "").strip()
        if jref:
            rec.journal = re.sub(r"\s+", " ", jref)
        prim = entry.find(ARXIV_NS + "primary_category")
        if prim is not None:
            rec.eprint_class = prim.get("term", "")
        rec.url = f"https://arxiv.org/abs/{rec.arxiv}"
        return rec


# -------------------------------------------------------------------- zbMATH

class Zbmath:
    BASE = "https://api.zbmath.org/v1/document/_search"

    def __init__(self, http: Http, cfg: Config) -> None:
        self.http, self.cfg = http, cfg

    def enrich(self, rec: BibRecord) -> None:
        """Add Zbl id and MSC codes in place. Best effort, never fatal."""
        if not self.cfg.zbmath:
            return
        if rec.doi:
            query = f"doi:{rec.doi}"
        elif rec.arxiv:
            query = f"arxiv:{rec.arxiv}"
        elif rec.title:
            query = rec.title
        else:
            return
        data = self.http.get(self.BASE, {"search_string": query,
                                         "results_per_page": 1, "page": 0}, delay=0.4)
        results = (data or {}).get("result") or []
        if not results:
            return
        hit = results[0]
        got_title = ((hit.get("title") or {}).get("title") or "")
        from .util import similarity
        # Only trust a title-based hit when the titles really agree.
        if not (rec.doi or rec.arxiv) and similarity(got_title, rec.title) < 0.90:
            return
        if hit.get("identifier"):
            rec.zbl = str(hit["identifier"])
        codes = [m.get("code") for m in (hit.get("msc") or []) if m.get("code")]
        if codes:
            rec.msc = codes
        if not rec.year and hit.get("year"):
            rec.year = str(hit["year"])


# --------------------------------------------------------------- OpenLibrary

class OpenLibrary:
    BASE = "https://openlibrary.org/search.json"

    def __init__(self, http: Http, cfg: Config) -> None:
        self.http, self.cfg = http, cfg

    def search(self, title: str, authors: str = "", rows: int = 4) -> list[BibRecord]:
        params = {"title": title, "limit": rows,
                  "fields": "title,author_name,first_publish_year,publisher,isbn,number_of_pages_median"}
        if authors:
            params["author"] = authors
        data = self.http.get(self.BASE, params, delay=0.5)
        out = []
        for doc in (data or {}).get("docs", [])[:rows]:
            rec = BibRecord(source="openlibrary", kind=DocKind.BOOK)
            rec.title = _clean_title(doc.get("title", ""))
            if not rec.title:
                continue
            rec.authors = [_split_name(n) for n in (doc.get("author_name") or [])]
            rec.year = str(doc.get("first_publish_year") or "")
            pubs = doc.get("publisher") or []
            rec.publisher = pubs[0] if pubs else ""
            isbns = doc.get("isbn") or []
            rec.isbn = isbns[0] if isbns else ""
            out.append(rec)
        return out
