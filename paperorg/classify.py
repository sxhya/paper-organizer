"""Decide what a document *is* before spending network calls on it.

The point of classifying early is twofold: a book or a thesis needs a
different lookup (and a different BibTeX type) than an article, and slides /
posters / lecture notes / bank statements should never be looked up at all.
"""
from __future__ import annotations

import re

from .record import DocFeatures, DocKind

THESIS_RE = re.compile(
    r"(?i)\b(ph\.?\s?d\.?\s+thesis|doctoral\s+(?:thesis|dissertation)|"
    r"master'?s?\s+thesis|diploma\s+thesis|habilitation(?:sschrift)?|"
    r"thesis\s+submitted|dissertation\s+submitted|in\s+partial\s+fulfil?lment|"
    r"zur\s+erlangung\s+des\s+(?:akademischen\s+)?grades|"
    r"th[eè]se\s+de\s+doctorat|proefschrift|диссертация|"
    r"диссертация\s+на\s+соискание|автореферат)\b"
)
DEGREE_RE = re.compile(
    r"(?i)\b(doctor\s+of\s+philosophy|dr\.\s?rer\.\s?nat|"
    r"submitted\s+(?:to|for)\s+the\s+(?:faculty|department|university)|"
    r"defended|supervisor|advisor|promotor|betreuer)\b"
)
# A bare publisher name is weak evidence: it appears in the front matter of
# journal articles too. Book *series* and book front matter are strong.
BOOK_STRONG_RE = re.compile(
    r"(?i)\b(graduate\s+texts\s+in\s+mathematics|lecture\s+notes\s+in\s+mathematics|"
    r"universitext|grundlehren|ergebnisse\s+der\s+mathematik|"
    r"encyclopaedia\s+of\s+mathematical|cambridge\s+studies\s+in|"
    r"table\s+of\s+contents|\bpreface\b|монография)\b"
)
BOOK_WEAK_RE = re.compile(
    r"(?i)\b(springer(?:-verlag)?|cambridge\s+university\s+press|"
    r"oxford\s+university\s+press|academic\s+press|birkh[aä]user|world\s+scientific|"
    r"de\s+gruyter|elsevier\s+science\s+publishers|"
    r"издательство)\b"
)
SLIDES_RE = re.compile(
    r"(?i)\b(beamer|powerpoint|keynote|prosper|foiltex|pdfscreen|slideshow|impress)\b"
)
NOTPAPER_RE = re.compile(
    r"(?i)\b(curriculum\s+vitae|list\s+of\s+publications|publication\s+list|"
    r"invoice|rechnung|lastschrift|sepa|steuer|tax\s+return|bank\s+statement|"
    r"boarding\s+pass|ticket|certificate|anmeldung|antrag|versicherung|"
    r"mietvertrag|arbeitsvertrag|timetable|schedule|programme?\s+of\s+the\s+conference|"
    r"abstracts?\s+of\s+talks|poster\s+session|call\s+for\s+papers)\b"
)

_DIR_SLIDES = ("slides", "talks", "presentations", "beamer", "vortrag")
_DIR_THESIS = ("thesis", "theses", "dissertation", "dissertations", "phd")
_DIR_BOOKS = ("books", "book", "monographs", "lecture notes", "textbooks")
_DIR_OTHER = ("publication lists", "publications", "cv", "admin", "posters",
              "abstracts", "programme", "program")


def _dir_hint(feat: DocFeatures) -> str | None:
    joined = " ".join(feat.dir_hints)
    for key, kind in (
        (_DIR_THESIS, DocKind.THESIS),
        (_DIR_BOOKS, DocKind.BOOK),
        (_DIR_SLIDES, DocKind.OTHER),
        (_DIR_OTHER, DocKind.OTHER),
    ):
        if any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", joined) for k in key):
            return kind
    return None


def classify(feat: DocFeatures) -> tuple[DocKind, float, str]:
    """Return (kind, confidence 0..1, human-readable reason)."""
    head = feat.text[:6000]
    name = feat.path.name.lower()
    pages = feat.pages
    hint = _dir_hint(feat)

    # --- slides and other non-documents -------------------------------------
    if feat.landscape and pages >= 3:
        return DocKind.OTHER, 0.92, f"landscape {feat.width:.0f}x{feat.height:.0f} ({pages}p): slides"
    if SLIDES_RE.search(feat.producer + " " + feat.creator):
        return DocKind.OTHER, 0.88, f"presentation tool: {feat.producer or feat.creator}"
    if NOTPAPER_RE.search(head[:2500]) or NOTPAPER_RE.search(name):
        return DocKind.OTHER, 0.80, "matches non-scholarly document pattern"
    if hint is DocKind.OTHER:
        return DocKind.OTHER, 0.70, f"directory hint: {'/'.join(feat.dir_hints[-2:])}"
    if (pages and pages <= 2 and feat.text_chars < 1200
            and not (feat.file_authors and feat.file_title)):
        # A one-page file with no text and an uninformative name is a cover
        # sheet or a scan fragment. With "Authors - Title.pdf" it is a note.
        return DocKind.OTHER, 0.65, f"only {pages} page(s) with little text"

    # --- thesis --------------------------------------------------------------
    # Only the title page counts. A paper that cites someone's PhD thesis, or
    # an obituary that mentions one, is not itself a thesis - and no thesis is
    # eight pages long.
    title_page = feat.text[:1400]
    long_enough = pages == 0 or pages >= 30
    if long_enough and (THESIS_RE.search(title_page) or THESIS_RE.search(name)):
        conf = 0.90 if pages >= 40 else 0.72
        return DocKind.THESIS, conf, "thesis wording on the title page"
    if hint is DocKind.THESIS and (pages == 0 or pages >= 40):
        return DocKind.THESIS, 0.75, f"directory hint: {'/'.join(feat.dir_hints[-2:])}"
    thesis_hits = len(THESIS_RE.findall(title_page)) * 2 + len(DEGREE_RE.findall(title_page))
    if thesis_hits >= 3 and pages >= 60:
        return DocKind.THESIS, 0.70, "degree/supervisor wording with book-like length"

    # --- book ----------------------------------------------------------------
    if feat.isbn:
        return DocKind.BOOK, 0.88, f"ISBN {feat.isbn}"
    if hint is DocKind.BOOK:
        return DocKind.BOOK, 0.78, f"directory hint: {'/'.join(feat.dir_hints[-2:])}"
    if pages >= 150:
        strong = BOOK_STRONG_RE.search(head)
        return DocKind.BOOK, 0.82 if strong else 0.70, f"{pages} pages"
    if pages >= 90 and BOOK_STRONG_RE.search(head):
        return DocKind.BOOK, 0.68, f"{pages} pages with book series/front matter"
    if pages >= 120 and BOOK_WEAK_RE.search(head):
        return DocKind.BOOK, 0.60, f"{pages} pages with a book publisher named"

    # --- paper ---------------------------------------------------------------
    if feat.arxiv_id or feat.doi:
        return DocKind.PAPER, 0.95, "carries an arXiv id / DOI"
    if pages and 1 <= pages <= 120:
        return DocKind.PAPER, 0.75, f"{pages} pages, article-like"
    if feat.text_chars > 1500:
        return DocKind.PAPER, 0.55, "substantial text, no book/thesis markers"
    if feat.file_authors and feat.file_title:
        # Nothing readable inside (a DjVu without djvulibre, an image-only
        # scan with OCR off), but the file name is a clean "Authors - Title".
        return DocKind.PAPER, 0.45, "unreadable content; file name looks like a paper"
    return DocKind.UNKNOWN, 0.30, "not enough signal"


def refine_with_metadata(kind: DocKind, crossref_type: str,
                         has_journal: bool = False) -> DocKind:
    """Trust the registry's own type when it disagrees with our guess.

    The local signals are heuristics over a title page; a registry record is
    evidence. In particular a paper whose front matter grew out of the
    author's dissertation reads as a thesis locally, but if Crossref says it
    is a journal article with a container title, it is a journal article.
    """
    t = (crossref_type or "").lower()
    if t in ("book", "monograph", "reference-book", "edited-book"):
        return DocKind.BOOK
    if t == "dissertation":
        return DocKind.THESIS
    if t in ("journal-article", "proceedings-article"):
        return DocKind.PAPER if has_journal else (
            DocKind.PAPER if kind in (DocKind.UNKNOWN, DocKind.OTHER) else kind)
    if t in ("posted-content", "book-chapter", "report", "other"):
        # A chapter of a book is still filed as a paper: it has its own authors.
        return DocKind.PAPER if kind in (DocKind.UNKNOWN, DocKind.OTHER) else kind
    return kind
