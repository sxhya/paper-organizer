"""Emit a BibTeX file that JabRef and bibtex/biber both accept."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .record import BibRecord, DocKind
from .util import norm, strip_accents, translit

# BibTeX entry type per document kind.
ENTRY_TYPE = {
    DocKind.PAPER: "article",
    DocKind.BOOK: "book",
    DocKind.THESIS: "phdthesis",
    DocKind.OTHER: "misc",
    DocKind.UNKNOWN: "misc",
}

# Braces are NOT escaped as \{ and \}: BibTeX counts those toward brace
# depth, so a single stray brace in an OCR'd title unbalances the whole entry
# and the parser swallows everything up to the next @. The text-mode macros
# are brace-balanced and render the same.
_SPECIALS = {
    "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "$": r"\$",
    "{": r"\textbraceleft{}", "}": r"\textbraceright{}",
}
# Latin-1 supplement and friends that plain bibtex cannot digest.
_ACCENTS = {
    "ä": r'\"a', "ö": r'\"o', "ü": r'\"u', "Ä": r'\"A', "Ö": r'\"O', "Ü": r'\"U',
    "ß": r"\ss{}", "é": r"\'e", "è": r"\`e", "ê": r"\^e", "ë": r'\"e',
    "á": r"\'a", "à": r"\`a", "â": r"\^a", "å": r"\aa{}", "Å": r"\AA{}",
    "í": r"\'i", "ì": r"\`i", "î": r"\^i", "ï": r'\"i',
    "ó": r"\'o", "ò": r"\`o", "ô": r"\^o", "ø": r"\o{}", "Ø": r"\O{}",
    "ú": r"\'u", "ù": r"\`u", "û": r"\^u", "ñ": r"\~n", "ç": r"\c{c}",
    "č": r"\v{c}", "š": r"\v{s}", "ž": r"\v{z}", "ř": r"\v{r}", "ě": r"\v{e}",
    "ł": r"\l{}", "Ł": r"\L{}", "ą": r"\k{a}", "ę": r"\k{e}", "ń": r"\'n",
    "ś": r"\'s", "ź": r"\'z", "ż": r"\.z", "ő": r"\H{o}", "ű": r"\H{u}",
    "æ": r"\ae{}", "Æ": r"\AE{}", "œ": r"\oe{}",
}


def escape(value: str, *, math_ok: bool = True) -> str:
    """Escape a field value for BibTeX.

    Mathematical titles legitimately contain ``$...$``; when *math_ok* is set
    we leave balanced dollar-delimited spans alone and only escape outside.
    """
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    if math_ok and value.count("$") >= 2 and value.count("$") % 2 == 0:
        parts = value.split("$")
        return "$".join(p if i % 2 else _escape_plain(p) for i, p in enumerate(parts))
    return _escape_plain(value)


def _escape_plain(s: str) -> str:
    out = []
    for ch in s:
        if ch in _SPECIALS:
            out.append(_SPECIALS[ch])
        elif ch in _ACCENTS:
            out.append(_ACCENTS[ch])
        elif ch == "\\":
            out.append(r"\textbackslash{}")
        elif ch == "~":
            out.append(r"\textasciitilde{}")
        elif ch == "^":
            out.append(r"\textasciicircum{}")
        else:
            out.append(ch)
    return "".join(out)


def protect_caps(title: str) -> str:
    """Brace words BibTeX styles would otherwise lowercase.

    Crossref hands out sentence-cased titles, where an interior capital almost
    always marks a proper noun or an acronym (``Neumann``, ``K1-functors``,
    ``A1-homotopy``). In an already title-cased string every word is capital,
    so bracing them all would be noise - there we only protect the unambiguous
    cases: acronyms, interior capitals and capital+digit compounds.
    """
    words = title.split(" ")
    if len(words) < 2:
        return title

    def core_of(w: str) -> str:
        return w.strip(".,;:()[]?!\u2019'\"")

    tail = [core_of(w) for w in words[1:]]
    capitalised = [w for w in tail if w[:1].isupper()]
    alpha = [w for w in tail if w[:1].isalpha()]
    # Sentence case if only a minority of the remaining words are capitalised.
    sentence_case = bool(alpha) and len(capitalised) / len(alpha) < 0.4

    def protect(word: str) -> str:
        if not word or word.startswith("{") or "$" in word or "\\" in word:
            return word
        core = core_of(word)
        if not core:
            return word
        if len(core) == 1:
            # A lone capital ("Property T", "the group G") is always a symbol,
            # and bracing it costs nothing in an already title-cased string.
            return "{" + word + "}" if core.isupper() else word
        if core.isupper():                                  # NSF, GL, SL2
            return "{" + word + "}"
        if any(c.isupper() for c in core[1:]):              # McLane, GLn, dgAlgebra
            return "{" + word + "}"
        if core[:1].isupper() and re.search(r"[\d-]", core):   # K1-functors, A1-homotopy
            return "{" + word + "}"
        if sentence_case and core[:1].isupper():            # proper nouns
            return "{" + word + "}"
        return word

    return " ".join([words[0]] + [protect(w) for w in words[1:]])


def cite_key(rec: BibRecord, taken: set[str]) -> str:
    """``Surname:2004:firstword`` style key, disambiguated with a/b/c."""
    a = rec.first_author()
    who = ""
    if a:
        who = strip_accents(a.family or a.literal or "").replace(" ", "")
    # Keep "vanderKallen" readable; only fold names that are not ASCII already.
    if not who.isascii():
        who = translit(who).title()
    who = re.sub(r"[^A-Za-z]", "", who) or "Anon"
    year = rec.year or "nd"
    words = [w for w in norm(rec.title).split() if len(w) > 3]
    tail = re.sub(r"[^a-z0-9]", "", translit(words[0])) if words else ""
    base = f"{who}{year}{(tail or 'untitled')[:12]}"
    key = base
    suffix = ord("a")
    while key in taken:
        key = f"{base}{chr(suffix)}"
        suffix += 1
    taken.add(key)
    return key


_INSTITUTION = re.compile(
    r"(?i)\b(universit|institut|college|school|academy|polytech|"
    r"hochschule|\beth\b|\btu\b|ecole|\bens\b)")


def _institution(publisher: str) -> str:
    """A thesis is granted by a university, not by JSTOR or Springer."""
    return publisher if publisher and _INSTITUTION.search(publisher) else ""


def etype_is_preprint(rec: BibRecord) -> bool:
    """An arXiv posting that has not (yet) appeared in a journal."""
    return bool(rec.arxiv) and not rec.journal and not rec.doi


def _fields_for(rec: BibRecord, file_rel: str | None) -> list[tuple[str, str]]:
    f: list[tuple[str, str]] = []
    add = f.append

    title = rec.title + ((": " + rec.subtitle) if rec.subtitle else "")
    add(("author", " and ".join(a.bibtex for a in rec.authors) or "{Unknown}"))
    add(("title", protect_caps(escape(title))))
    add(("year", rec.year))

    if rec.kind is DocKind.PAPER:
        add(("journal", escape(rec.journal)))
        add(("booktitle", escape(rec.booktitle)))
        add(("volume", rec.volume))
        add(("number", rec.number))
        add(("pages", rec.pages))
    elif rec.kind is DocKind.BOOK:
        add(("publisher", escape(rec.publisher)))
        add(("series", escape(rec.journal)))
        add(("volume", rec.volume))
        add(("isbn", rec.isbn))
    elif rec.kind is DocKind.THESIS:
        add(("school", escape(rec.school or _institution(rec.publisher))))
        add(("type", escape(rec.thesis_type)))
    else:
        add(("howpublished", escape(rec.journal or rec.publisher)))

    if etype_is_preprint(rec):
        add(("howpublished", f"arXiv:{rec.arxiv}"))

    add(("doi", rec.doi))
    if rec.arxiv:
        add(("eprint", rec.arxiv))
        add(("archiveprefix", "arXiv"))
        add(("primaryclass", rec.eprint_class))
    add(("mrnumber", rec.mrnumber))
    add(("zbl", rec.zbl))
    add(("issn", rec.issn))
    if rec.kind is not DocKind.BOOK:
        add(("isbn", rec.isbn))
    add(("url", rec.url))
    if rec.msc:
        # MathSciNet's form, "20G35 (14L15 13D15)": primary, then secondary.
        rest = f" ({' '.join(rec.msc[1:])})" if rec.msc[1:] else ""
        add(("mrclass", rec.msc[0] + rest))
        add(("keywords", "MSC: " + ", ".join(rec.msc)))
    add(("language", rec.language))
    add(("note", escape(rec.bib_note)))
    if file_rel:
        # JabRef "file" field: description:path:type
        add(("file", f":{_jabref_escape(file_rel)}:PDF"))
    return [(k, v) for k, v in f if v]


def _jabref_escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", r"\:").replace(";", r"\;")


def format_entry(rec: BibRecord, key: str, file_rel: str | None = None) -> str:
    etype = ENTRY_TYPE.get(rec.kind, "misc")
    if rec.kind is DocKind.PAPER and not rec.journal and not rec.booktitle:
        etype = "misc" if rec.arxiv and not rec.doi else "article"
    fields = _fields_for(rec, file_rel)
    width = max((len(k) for k, _ in fields), default=8)
    body = ",\n".join(f"  {k.ljust(width)} = {{{v}}}" for k, v in fields)
    return f"@{etype}{{{key},\n{body}\n}}\n"


def write_bib(path: Path, entries: list[str], *, header: str = "") -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    preamble = (
        f"% Encoding: UTF-8\n"
        f"% Generated by paper-organizer on {stamp}\n"
        f"% {len(entries)} entries. Paths in 'file' fields are relative to this file.\n"
    )
    if header:
        preamble += "".join(f"% {line}\n" for line in header.splitlines())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preamble + "\n" + "\n".join(entries), encoding="utf-8")
