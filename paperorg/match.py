"""Turn local document features into a single best bibliographic record."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .arxivguard import Lexicon, Reroute, decide_arxiv
from .arxivplan import ArxivPlan
from .classify import refine_with_metadata
from .config import Config
from .extract import name_stem
from .record import Author, BibRecord, DocFeatures, DocKind
from .people import plausible_local_title, plausible_person, prefer_richer
from .sources import Arxiv, Crossref, MathNet, OpenLibrary, Zbmath, _split_name
from .util import is_cyrillic, norm, similarity, translit

# Words that a title-only match should not be allowed to hinge on.
_STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "with", "to", "over"}

# Section headings and front-matter stubs. Crossref indexes book chapters
# literally called "Introduction", and matching one of those to a real paper
# is worse than returning nothing.
_GENERIC_TITLES = {
    "introduction", "preface", "foreword", "contents", "table of contents",
    "index", "bibliography", "references", "appendix", "notation",
    "preliminaries", "acknowledgements", "acknowledgments", "front matter",
    "back matter", "editorial", "abstract", "summary", "conclusion",
    "conclusions", "erratum", "corrigendum", "abstracts", "chapter",
    "main results", "notes", "problems", "exercises",
}


def is_generic_title(title: str) -> bool:
    n = norm(title)
    return not n or n in _GENERIC_TITLES or len(n) < 6


@dataclass
class Candidate:
    record: BibRecord
    score: float
    title_score: float
    author_score: float
    why: str


def _surnames(blob: str) -> set[str]:
    """Pull plausible surnames out of a free-form author string."""
    if not blob:
        return set()
    blob = re.sub(r"(?i)\bet\.?\s?al\.?", " ", blob)
    out = set()
    for tok in re.findall(r"[A-ZА-ЯЁ][-'’A-Za-zА-Яа-яёЁ]{2,}", blob):
        n = norm(tok)
        if n and n not in _STOP and len(n) > 2:
            out.add(n)
    return out


def author_agreement(rec: BibRecord, hint: str) -> float:
    """0..1 overlap between record authors and the author hint we extracted.

    Names are compared transliterated, so "N. Vavilov" in a file name agrees
    with "Н. А. Вавилов" on Math-Net.Ru; schemes differ ("Petrov"/"Петров" is
    exact, "Jagodovskij"/"Ягодовский" is not), hence the fuzzy fallback.
    """
    want = {translit(w) for w in _surnames(hint)}
    have = {norm(a.family or a.literal) for a in rec.authors}
    have = {translit(h.split()[-1]) for h in have if h}
    if not want or not have:
        return 0.0
    hits = sum(1 for w in want if w in have or any(similarity(w, h) >= 0.8 for h in have))
    return min(1.0, hits / min(len(want), len(have)))


def _year(value: str) -> int:
    """First four-digit year in *value*, or 0. Records carry "2011-2012" too."""
    m = re.search(r"\d{4}", str(value) or "")
    return int(m.group(0)) if m else 0


def score_candidate(rec: BibRecord, title: str, author_hint: str,
                    year_hint: str = "") -> Candidate:
    if is_generic_title(rec.title) and not rec.subtitle:
        return Candidate(rec, 0.0, 0.0, 0.0, "generic title")
    full = rec.title + ((": " + rec.subtitle) if rec.subtitle else "")
    ts = max(similarity(title, rec.title), similarity(title, full))
    ascore = author_agreement(rec, author_hint)

    score = ts
    if ascore > 0:
        score = min(1.0, score + 0.14 * ascore)
    elif author_hint and rec.authors:
        score -= 0.06                       # we had names and none of them matched
    if year_hint and rec.year:
        if year_hint == rec.year:
            score = min(1.0, score + 0.03)
        elif abs(_year(year_hint) - _year(rec.year)) > 2:
            score -= 0.04
    # A very short title match is easy to hit by accident.
    if len(norm(title).split()) <= 2:
        score -= 0.10
    why = f"title {ts:.2f}" + (f", authors {ascore:.2f}" if ascore else "")
    return Candidate(rec, max(0.0, min(1.0, score)), ts, ascore, why)


class Resolver:
    """Runs the identification cascade for one document."""

    def __init__(self, cfg: Config, crossref: Crossref, arxiv: Arxiv,
                 zbmath: Zbmath, openlib: OpenLibrary,
                 mathnet: MathNet | None = None, plan: ArxivPlan | None = None) -> None:
        self.cfg = cfg
        self.mathnet = mathnet
        # arXiv title searches wait here for a human (see arxivplan).
        self.plan = plan if plan is not None else ArxivPlan()
        # What the registries have said so far; the pipeline fills it in.
        self.lexicon = Lexicon()
        self.crossref = crossref
        self.arxiv = arxiv
        self.zbmath = zbmath
        self.openlib = openlib

    # ------------------------------------------------------------------ main
    def resolve(self, feat: DocFeatures, kind: DocKind) -> tuple[BibRecord | None, list[Candidate]]:
        """Return (best record or None, ranked candidates for the user to pick)."""
        if not self.cfg.online:
            return self._offline_record(feat, kind), []

        # 1. An identifier printed in the document beats any amount of guessing.
        if feat.arxiv_id:
            rec = self.arxiv.by_id(feat.arxiv_id)
            if rec:
                if rec.doi:
                    merged = self.crossref.by_doi(rec.doi)
                    if merged:
                        rec = self._merge(merged, rec)
                rec.confidence = 0.99
                rec.source = "arxiv-id"
                self._finish(rec, kind)
                return rec, []
        if feat.doi:
            rec = self.crossref.by_doi(feat.doi)
            if rec:
                rec.confidence = 0.98
                rec.source = "doi"
                self._finish(rec, kind)
                return rec, []

        # 2. Otherwise search by the titles we extracted.
        cands = self._search_candidates(feat, kind)
        if not cands:
            return None, []
        cands.sort(key=lambda c: -c.score)
        best = cands[0]
        if best.score >= self.cfg.accept_score:
            rec = best.record
            rec.confidence = best.score
            self._finish(rec, kind)
            return rec, cands[:5]
        # Below the accept threshold: only offer the near misses. Showing a
        # user a 0.2-scoring candidate wastes their attention.
        worth_asking = [c for c in cands[:5] if c.score >= self.cfg.review_score]
        return None, worth_asking

    # ------------------------------------------------------------- searching
    def _search_candidates(self, feat: DocFeatures, kind: DocKind) -> list[Candidate]:
        titles = feat.title_candidates()
        hint = feat.author_hint()
        year = self._year_hint(feat)
        seen: set[str] = set()
        out: list[Candidate] = []

        for title in titles[:3]:
            key = norm(title)
            if key in seen or is_generic_title(title):
                continue
            seen.add(key)

            # Russian originals are indexed under their Cyrillic titles only
            # on Math-Net.Ru; Crossref and arXiv hold at best the translation.
            if self.mathnet and is_cyrillic(title):
                for rec in self._mathnet_search(title, hint):
                    out.append(score_candidate(rec, title, hint, year))
                if out and max(c.score for c in out) >= self.cfg.accept_score:
                    break

            if kind is DocKind.BOOK:
                for rec in self.crossref.search(title, hint, rows=4, filters="type:book"):
                    out.append(score_candidate(rec, title, hint, year))
                for rec in self.openlib.search(title, _first_surname(hint), rows=3):
                    out.append(score_candidate(rec, title, hint, year))
            for rec in self.crossref.search(title, hint, rows=5):
                out.append(score_candidate(rec, title, hint, year))
            if self._good(out):
                break

        # arXiv answers one query every half minute, so it comes last, and
        # only with a query that survived `decide_arxiv`. The Crossref record
        # is the richer one anyway (journal, volume, pages).
        if not self._good(out):
            self._beyond_titles(feat, kind, titles, hint, year, out)
        return _dedupe(out)

    def _good(self, cands: list[Candidate]) -> bool:
        return bool(cands) and max(c.score for c in cands) >= self.cfg.accept_score

    # ------------------------------------------------------ arXiv and reroutes
    def _beyond_titles(self, feat: DocFeatures, kind: DocKind, titles: list[str],
                       hint: str, year: str, out: list[Candidate]) -> None:
        dec = decide_arxiv(feat, kind, titles, hint, out, self.lexicon)
        for r in dec.reroutes:
            try:
                out.extend(self._reroute(r, hint, year))
            except Exception:
                continue
            if self._good(out):
                return
        if dec.action != "none":
            self._ask_arxiv(feat, dec, hint, year, out)

    def _reroute(self, r: Reroute, hint: str, year: str) -> list[Candidate]:
        """Evidence that belongs to a registry other than arXiv."""
        d = r.data
        if r.kind == "jstor":
            # JSTOR's DOIs are its stable ids under the prefix 10.2307.
            if d.get("stable"):
                rec = self.crossref.by_doi(f"10.2307/{d['stable']}")
                if rec and similarity(rec.title, d["title"]) >= 0.6:
                    return [Candidate(rec, 0.97, 1.0, 0.0, "JSTOR stable id")]
            return [score_candidate(rec, d["title"], d.get("authors") or hint,
                                    d.get("year") or year)
                    for rec in self.crossref.search(d["title"], d.get("authors") or hint)]
        if r.kind == "pii":
            rec = self.crossref.by_alternative_id(d["pii"]) \
                if hasattr(self.crossref, "by_alternative_id") else None
            # Elsevier's DOIs are their PIIs under 10.1016.
            rec = rec or self.crossref.by_doi("10.1016/" + d["pii"].lower())
            return [Candidate(rec, 0.97, 1.0, 0.0, "Elsevier PII")] if rec else []
        if r.kind == "arxiv-id":
            rec = self.arxiv.by_id(d["id"])
            return [Candidate(rec, 0.97, 1.0, 0.0, "arXiv stamp")] if rec else []
        if r.kind == "citation":
            return [score_citation(rec, d, hint)
                    for rec in self.crossref.search(d["line"], hint, rows=5)]
        if r.kind == "mathnet" and self.mathnet:
            return [score_candidate(rec, d["title"], d.get("author") or hint, year)
                    for rec in self._mathnet_search(d["title"], d.get("author", ""))]
        return []

    def _plan_key(self, feat: DocFeatures) -> str:
        try:
            return str(feat.path.relative_to(self.cfg.input_dir))
        except ValueError:
            return str(feat.path)

    def _ask_arxiv(self, feat: DocFeatures, dec, hint: str, year: str,
                   out: list[Candidate]) -> None:
        """Send the query a human approved (or, with --arxiv-titles auto,
        the one `decide_arxiv` proposed); otherwise just put it on the list."""
        mode = getattr(self.cfg, "arxiv_titles", "review")
        if mode == "off":
            return
        key = self._plan_key(feat)
        row = self.plan.reviewed(key)
        self.plan.propose(key, dec)
        if row is not None:
            if row["send"].lower() not in ("y", "yes"):
                return
            query = self.plan.query_for(row)
            title, who = row["title"] or dec.title, row["authors"] or hint
        elif mode == "auto" and dec.action == "send":
            query, title, who = dec.query, dec.title, hint
        else:
            return
        if not query:
            self.plan.note_result(key, "nothing to send: no query and no title words")
            return
        recs = self.arxiv.search(query, rows=5)
        cands = [score_candidate(rec, title, who, year) for rec in recs]
        out.extend(cands)
        http = getattr(self.arxiv, "http", None)
        if not recs and http is not None and "export.arxiv.org" in http.failed_hosts():
            self.plan.note_result(key, "not answered (arXiv refusing); sent again next run")
        elif not cands:
            self.plan.note_result(key, "no results")
        else:
            best = max(cands, key=lambda c: c.score)
            verdict = "accepted" if best.score >= self.cfg.accept_score else "best only"
            self.plan.note_result(key, f"{verdict} {best.score:.2f}: {best.record.title[:70]}"
                                       f" (arXiv:{best.record.arxiv})")

    def _mathnet_search(self, title: str, hint: str) -> list[BibRecord]:
        """Every word must match on Math-Net.Ru, so one OCR slip or a
        misspelt file name loses the paper; the fallback asks for the
        author's surname and the title's three most distinctive words."""
        words = re.findall(r"[\w'-]+", re.sub(r"\$[^$]*\$", " ", title))
        found = self.mathnet.search(" ".join(words[:10]))
        if found:
            return found[:6]
        surname = _first_surname(hint) if is_cyrillic(hint) else ""
        longest = sorted((w for w in words if len(w) > 4), key=len, reverse=True)[:3]
        if surname and longest:
            return self.mathnet.search(" ".join(longest), surname)[:6]
        return []

    @staticmethod
    def _year_hint(feat: DocFeatures) -> str:
        m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", feat.text[:1500])
        return m.group(1) if m else ""

    # -------------------------------------------------------------- finishing
    def finish(self, rec: BibRecord, kind: DocKind) -> None:
        """Complete an accepted (or user-chosen) record."""
        self._finish(rec, kind)

    def _finish(self, rec: BibRecord, kind: DocKind) -> None:
        translation = ""
        if rec.source == "mathnet" and self.mathnet:
            try:
                translation = self.mathnet.complete(rec)
            except Exception:
                pass
        rec.kind = refine_with_metadata(kind, rec.note,
                                        has_journal=bool(rec.journal))
        if rec.msc and rec.zbl:
            return                  # Math-Net.Ru already gave what zbMATH would
        # zbMATH indexes a Russian paper by its translation's DOI.
        probe = BibRecord(doi=translation, title=rec.title) \
            if translation and not rec.doi else rec
        try:
            self.zbmath.enrich(probe)
        except Exception:
            pass
        if probe is not rec:
            rec.msc = rec.msc or probe.msc
            rec.zbl = rec.zbl or probe.zbl

    @staticmethod
    def _merge(primary: BibRecord, extra: BibRecord) -> BibRecord:
        """Crossref record enriched with whatever only arXiv knows."""
        for field in ("arxiv", "eprint_class"):
            if not getattr(primary, field):
                setattr(primary, field, getattr(extra, field))
        if not primary.authors:
            primary.authors = extra.authors
        if not primary.year:
            primary.year = extra.year
        primary.source = "arxiv+crossref"
        return primary

    def _offline_record(self, feat: DocFeatures, kind: DocKind) -> BibRecord | None:
        """No network: synthesise a record from the file itself."""
        titles = [t for t in feat.title_candidates() if plausible_local_title(t)]
        if not titles:
            titles = [name_stem(feat.path)]
        rec = BibRecord(kind=kind if kind is not DocKind.UNKNOWN else DocKind.PAPER,
                        source="local", confidence=0.4)
        rec.title = titles[0]
        rec.authors = [a for a in prefer_richer(
            parse_author_list(feat.file_authors),
            parse_author_list(feat.page_authors or feat.meta_author))
            if plausible_person(a)]
        rec.arxiv = feat.arxiv_id
        rec.doi = feat.doi
        rec.mrnumber = feat.mr
        rec.isbn = feat.isbn
        m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", feat.text[:2000])
        rec.year = m.group(1) if m else ""
        return rec


def score_citation(rec: BibRecord, cite: dict, author_hint: str = "") -> Candidate:
    """How well *rec* fits a printed reference line: journal, volume and
    first page all agreeing is as good as an identifier."""
    score, why = 0.45, []
    fp = re.split(r"\D+", rec.pages or "")[0] if rec.pages else ""
    if cite.get("volume") and str(rec.volume) == cite["volume"]:
        score += 0.2
        why.append("volume")
    if cite.get("page") and fp == cite["page"]:
        score += 0.2
        why.append("page")
    j, want = norm(rec.journal), norm(cite.get("journal", ""))
    if j and want and (similarity(j, want) >= 0.6 or j in want or want in j):
        score += 0.1
        why.append("journal")
    if cite.get("year") and rec.year == cite["year"]:
        score += 0.05
        why.append("year")
    ascore = author_agreement(rec, author_hint)
    if ascore:
        score += 0.05 * ascore
    if "volume" not in why or "page" not in why:
        score = min(score, 0.75)        # a journal and a year are not enough
    return Candidate(rec, min(1.0, score), 0.0, ascore,
                     "citation line: " + (", ".join(why) or "nothing agrees"))


def _first_surname(hint: str) -> str:
    names = _surnames(hint)
    return sorted(names)[0] if names else ""


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for c in cands:
        key = c.record.identifier or norm(c.record.title)
        if key not in best or c.score > best[key].score:
            best[key] = c
    return list(best.values())


def parse_author_list(blob: str) -> list[Author]:
    """Best-effort parse of ``"A. Bak, N. Vavilov"`` into Author objects."""
    if not blob:
        return []
    blob = re.sub(r"(?i)\bet\.?\s?al\.?", "", blob)
    blob = re.sub(r"\s+", " ", blob).strip(" ,;")
    parts = [p.strip() for p in re.split(r",| and | & |;|\band\b", blob) if p.strip()]
    # "Serre, Jean-Pierre" is one author, not two.
    if len(parts) == 2 and len(parts[1].split()) <= 2 and "." not in parts[1] \
            and parts[1][:1].isupper() and len(parts[0].split()) == 1:
        return [_split_name(f"{parts[1]} {parts[0]}")]
    out = []
    for p in parts:
        if len(p) < 2 or re.match(r"^[\W\d]+$", p):
            continue
        out.append(_split_name(p))
    return out
