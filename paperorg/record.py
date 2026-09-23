"""Normalised bibliographic record and document descriptors."""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


class DocKind(str, enum.Enum):
    PAPER = "paper"
    BOOK = "book"
    THESIS = "thesis"
    OTHER = "other"        # slides, posters, notes, admin documents, ...
    UNKNOWN = "unknown"


@dataclass
class Author:
    family: str = ""
    given: str = ""
    literal: str = ""      # used when we could not split the name

    @property
    def full(self) -> str:
        if self.literal:
            return self.literal
        return f"{self.given} {self.family}".strip()

    @property
    def bibtex(self) -> str:
        """``Family, Given`` - the unambiguous BibTeX form."""
        if self.literal:
            return f"{{{self.literal}}}" if " " not in self.literal else self.literal
        if self.family and self.given:
            return f"{self.family}, {self.given}"
        return self.family or self.given

    @property
    def folder(self) -> str:
        """Folder name for this author: ``Family, Given`` without noise."""
        if self.literal:
            return self.literal
        if self.family and self.given:
            return f"{self.family}, {self.given}"
        return self.family or self.given or "Unknown"

    def to_json(self) -> dict[str, str]:
        return {"family": self.family, "given": self.given, "literal": self.literal}

    @classmethod
    def from_json(cls, d: dict[str, str]) -> "Author":
        return cls(d.get("family", ""), d.get("given", ""), d.get("literal", ""))


@dataclass
class BibRecord:
    """Everything we know bibliographically about one document."""
    kind: DocKind = DocKind.UNKNOWN
    title: str = ""
    subtitle: str = ""
    authors: list[Author] = field(default_factory=list)
    year: str = ""
    journal: str = ""          # container title / book series
    booktitle: str = ""
    publisher: str = ""
    school: str = ""           # thesis-granting institution
    thesis_type: str = ""      # "PhD thesis", "Master's thesis", ...
    volume: str = ""
    number: str = ""
    pages: str = ""
    doi: str = ""
    arxiv: str = ""
    eprint_class: str = ""     # primary arXiv category
    mrnumber: str = ""
    zbl: str = ""
    isbn: str = ""
    issn: str = ""
    url: str = ""
    msc: list[str] = field(default_factory=list)
    language: str = ""
    note: str = ""             # the registry's own type ("journal-article", ...)
    bib_note: str = ""         # free text for the BibTeX note field
    source: str = ""           # which resolver produced this record
    confidence: float = 0.0

    # ------------------------------------------------------------------ misc
    @property
    def identifier(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.arxiv:
            return f"arxiv:{self.arxiv.lower()}"
        return ""

    @property
    def display(self) -> str:
        who = ", ".join(a.full for a in self.authors[:3]) or "?"
        if len(self.authors) > 3:
            who += " et al."
        return f"{who} — {self.title or '?'} ({self.year or 'n.d.'})"

    def first_author(self) -> Author | None:
        return self.authors[0] if self.authors else None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["authors"] = [a.to_json() for a in self.authors]
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "BibRecord":
        d = dict(d)
        d["kind"] = DocKind(d.get("kind", "unknown"))
        d["authors"] = [Author.from_json(a) for a in d.get("authors", [])]
        d.setdefault("msc", [])
        return cls(**d)


def _looks_like_a_title(s: str) -> bool:
    """Prose, not a slug: at least two words and mostly letters."""
    s = (s or "").strip()
    if len(s) < 6 or " " not in s:
        return False
    letters = sum(1 for c in s if c.isalpha())
    return letters >= 0.6 * len(s)


# What converters and word processors leave in the Title field: the source
# file's name ("thesis.dvi", "Microsoft Word - talk.doc"), not a title.
_FILENAME_TITLE = re.compile(
    r"(?i)^(microsoft (word|powerpoint) - .*|.*\.(dvi|tex|ps|pdf|docx?|pptx?|rtf|odt))$")


@dataclass
class DocFeatures:
    """What we managed to read out of the file itself, before any lookup."""
    path: Path
    crc32: str = ""
    fmt: str = ""              # "pdf" or "djvu", from the content not the name
    size: int = 0
    pages: int = 0
    width: float = 0.0
    height: float = 0.0
    producer: str = ""
    creator: str = ""
    meta_title: str = ""
    meta_author: str = ""
    text: str = ""             # first pages, plain
    text_chars: int = 0
    ocr_used: bool = False
    page_title: str = ""       # largest-font title line(s) on page 1
    page_authors: str = ""     # line(s) right below the title
    file_title: str = ""       # from the file name
    file_authors: str = ""     # from the file name
    dir_hints: list[str] = field(default_factory=list)
    arxiv_id: str = ""
    doi: str = ""
    mr: str = ""
    isbn: str = ""
    error: str = ""

    @property
    def suffix(self) -> str:
        """Canonical extension for the repository copy."""
        return {"pdf": ".pdf", "djvu": ".djvu"}.get(self.fmt, ".pdf")

    @property
    def landscape(self) -> bool:
        return self.height > 0 and self.width / self.height > 1.2

    def title_candidates(self) -> list[str]:
        """Best-first list of plausible titles.

        A file name that parsed as ``Authors - Title`` was written by a human
        and is the best source. A bare stem (``vdk-v2``, ``deodhar82``) was
        not, so there the document's own typography comes first.
        """
        named = bool(self.file_authors) and _looks_like_a_title(self.file_title)
        meta = self.meta_title.strip()
        meta = "" if _FILENAME_TITLE.match(meta) else meta
        order = ((self.file_title, self.page_title, meta) if named
                 else (self.page_title, meta, self.file_title))
        out: list[str] = []
        for t in order:
            t = (t or "").strip()
            if len(t) >= 6 and t not in out:
                out.append(t)
        return out

    def author_hint(self) -> str:
        return self.file_authors or self.page_authors or self.meta_author or ""

    # Enough of the front matter for classification and identifier hunting,
    # without storing whole documents in the cache.
    CACHED_TEXT = 20000

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        d["text"] = self.text[:self.CACHED_TEXT]
        return d
