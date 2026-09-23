"""Turn local document features into a single best bibliographic record."""
from __future__ import annotations

import re
from dataclasses import dataclass

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
                 mathnet: MathNet | None = None) -> None:
        self.cfg = cfg
        self.mathnet = mathnet
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
        asked_arxiv = False

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

            # arXiv asks callers to wait three seconds between queries, which
            # dominates the run time, so only fall through to it when Crossref
            # has not already produced an acceptable match. The Crossref
            # record is the richer one anyway (journal, volume, pages).
            if out and max(c.score for c in out) >= self.cfg.accept_score:
                break
            # One arXiv query per document, on the best title candidate only:
            # each costs three seconds of enforced politeness, and the weaker
            # candidates almost never turn up something the first one missed.
            if kind in (DocKind.PAPER, DocKind.UNKNOWN) and not asked_arxiv:
                asked_arxiv = True
                for rec in self.arxiv.search_title(title, rows=4):
                    out.append(score_candidate(rec, title, hint, year))
            if out and max(c.score for c in out) >= self.cfg.accept_score:
                break
        return _dedupe(out)

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
