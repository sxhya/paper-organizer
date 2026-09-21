"""Author identity: one folder per person, not one per spelling.

Registries return the same mathematician in several shapes - ``Suslin, A.``
from an old journal record, ``Suslin, Andrei`` from a newer one, and
``SUSLIN, ANDREI`` from an OCR'd title page. Filing those separately produces
three sibling folders for one person, which defeats the point of the
repository. This module canonicalises names and merges compatible variants.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .record import Author, BibRecord
from .util import norm, strip_accents

_PARTICLES = {"van", "von", "der", "den", "de", "di", "da", "dos", "del", "della",
              "la", "le", "el", "al", "ibn", "bin", "ten", "ter", "op", "zu"}


def _fix_caps(name: str) -> str:
    """``YOU'AN CAO`` -> ``You'an Cao``; leave normal capitalisation alone."""
    if not name or not name.isupper():
        return name
    out = []
    for word in name.split():
        if word.lower() in _PARTICLES:
            out.append(word.lower())
            continue
        # Capitalise after hyphens and apostrophes too: O'NEILL -> O'Neill.
        fixed = re.sub(r"[A-ZА-ЯЁ][A-ZА-ЯЁ'’-]*",
                       lambda m: _cap_word(m.group(0)), word)
        out.append(fixed)
    return " ".join(out)


def _cap_word(w: str) -> str:
    """Capitalise across hyphens, and across an apostrophe only when the
    prefix is a single letter: ``O'NEILL`` -> ``O'Neill``, ``YOU'AN`` ->
    ``You'an`` (the pinyin convention)."""
    parts = re.split("([-'\u2019])", w.lower())
    out: list[str] = []
    for i, part in enumerate(parts):
        if part in "-'\u2019":
            out.append(part)
        elif i == 0 or parts[i - 1] == "-" or (i >= 2 and len(parts[i - 2]) == 1):
            out.append(part.capitalize())
        else:
            out.append(part)
    return "".join(out)


def normalise_author(a: Author) -> Author:
    """Tidy one author in place-safe fashion (returns a new object)."""
    family = _fix_caps(re.sub(r"\s+", " ", (a.family or "").strip(" ,.")))
    given = _fix_caps(re.sub(r"\s+", " ", (a.given or "").strip(" ,")))
    literal = _fix_caps(re.sub(r"\s+", " ", (a.literal or "").strip(" ,")))
    # Some records put everything in `family`.
    if family and not given and not literal and "," in family:
        fam, _, giv = family.partition(",")
        family, given = fam.strip(), giv.strip()
    return Author(family=family, given=given, literal=literal)


def _initials(given: str) -> str:
    """``Jean-Pierre`` -> ``JP``; ``A.`` -> ``A``."""
    return "".join(w[0].upper() for w in re.split(r"[\s.\-’']+", given) if w)


def _key(a: Author) -> str:
    base = a.family or a.literal
    return norm(strip_accents(base))


def _spelled_out(given: str) -> str:
    """The given-name words that are more than a bare initial."""
    parts = [w.strip(".") for w in re.split(r"[\s.]+", given or "") if w.strip(".")]
    return " ".join(norm(w) for w in parts if len(w) > 1)


def _compatible(given_a: str, given_b: str) -> bool:
    """Do two given-name spellings plausibly denote the same person?

    An initial may stand for any name starting with that letter, but two
    *spelled-out* names that differ are two different people: Rabeya Basu and
    Rikmantra Basu both abbreviate to "R. Basu" and must not be merged.
    """
    if not given_a or not given_b:
        return True                       # one side is simply less specific
    full_a, full_b = _spelled_out(given_a), _spelled_out(given_b)
    if full_a and full_b and full_a != full_b:
        return full_a.startswith(full_b) or full_b.startswith(full_a)
    ia, ib = _initials(given_a), _initials(given_b)
    if not ia or not ib:
        return True
    # "A." vs "A. V." - a prefix of the initials is still the same person.
    return ia == ib or ia.startswith(ib) or ib.startswith(ia)


def _richness(a: Author) -> tuple:
    """Prefer the most informative spelling as the canonical one."""
    given = a.given or ""
    spelled = sum(1 for w in given.split() if len(w.strip(".")) > 1)
    return (spelled, len(given), len(a.family or a.literal))


class AuthorIndex:
    """Collects every author seen in a run and hands out canonical spellings."""

    def __init__(self) -> None:
        self._variants: dict[str, list[Author]] = defaultdict(list)
        self._exact: dict[tuple[str, str], Author] = {}
        self._reps: dict[str, list[Author]] = {}
        self._merged = 0

    # ------------------------------------------------------------- building
    def add(self, author: Author) -> None:
        a = normalise_author(author)
        if not (a.family or a.literal):
            return
        self._variants[_key(a)].append(a)

    def add_records(self, records: list[BibRecord]) -> None:
        for rec in records:
            for a in rec.authors:
                self.add(a)

    def build(self) -> None:
        """Cluster the variants of each surname and pick a canonical form."""
        for key, variants in self._variants.items():
            clusters: list[list[Author]] = []
            # Richest spellings first, so that by the time a bare initial is
            # considered every spelled-out name already has its cluster.
            for a in sorted(variants, key=_richness, reverse=True):
                fits = [c for c in clusters
                        if all(_compatible(a.given, b.given) for b in c)]
                if len(fits) == 1:
                    fits[0].append(a)
                else:
                    # No home, or several equally plausible ones: keep it apart
                    # rather than assign it to whichever came first.
                    clusters.append([a])
            reps = []
            for cluster in clusters:
                best = max(cluster, key=_richness)
                reps.append(best)
                for a in cluster:
                    self._exact[(key, _variant(a))] = best
                    if _variant(a) != _variant(best):
                        self._merged += 1
            self._reps[key] = reps

    # ------------------------------------------------------------- applying
    def canonical(self, author: Author) -> Author:
        """The agreed spelling for *author*, or the author unchanged.

        An initials-only form is only upgraded when exactly one person with
        that surname fits: with both Rabeya Basu and Rikmantra Basu in the
        library, a bare "R. Basu" stays "R. Basu" rather than guessing.
        """
        a = normalise_author(author)
        key = _key(a)
        hit = self._exact.get((key, _variant(a)))
        if hit is not None:
            return hit
        fits = [r for r in self._reps.get(key, []) if _compatible(a.given, r.given)]
        return fits[0] if len(fits) == 1 else a

    def apply(self, records: list[BibRecord]) -> None:
        for rec in records:
            rec.authors = [self.canonical(a) for a in rec.authors]

    def stats(self) -> dict:
        return {"distinct_surnames": len(self._variants),
                "merged_variants": self._merged,
                "people": sum(len(r) for r in self._reps.values())}


def _variant(a: Author) -> str:
    """Identity of one *spelling* of a name."""
    return norm(strip_accents(a.given or ""))


def prefer_richer(primary: list[Author], secondary: list[Author]) -> list[Author]:
    """Upgrade each name in *primary* with a compatible, fuller spelling.

    The file name usually gives the reliable author *order* but only initials
    ("A. Suslin"), while the title page spells the name out ("Andrei Suslin").
    Taking the order from one and the spelling from the other gives the best
    of both without trusting page extraction to get the order right.
    """
    if not primary:
        return [normalise_author(a) for a in secondary]
    if not secondary:
        return [normalise_author(a) for a in primary]
    out: list[Author] = []
    for a in primary:
        a = normalise_author(a)
        best = a
        for b in secondary:
            b = normalise_author(b)
            if _key(a) != _key(b) or not _compatible(a.given, b.given):
                continue
            if _richness(b) > _richness(best):
                best = b
        out.append(best)
    return out


# --------------------------------------------------------- sanity filtering

_JUNK_CHARS = re.compile(r"[\\{}^~|<>£§©†‡]")
_SENTENCE = re.compile(r"(?i)\b(the|and|of|for|with|that|this|such|stands|"
                       r"submitted|foundation|rights|reserved|droits)\b")


def plausible_person(a: Author) -> bool:
    """Would a librarian accept this as somebody's name?

    Locally-derived records are built from OCR output, which happily offers
    "Here \"E\" stands" or a whole copyright notice as an author.
    """
    full = (a.literal or f"{a.given} {a.family}").strip()
    if not full or len(full) > 60:
        return False
    if _JUNK_CHARS.search(full) or any(c.isdigit() for c in full):
        return False
    words = full.split()
    if not (1 <= len(words) <= 5):
        return False
    if _SENTENCE.search(full):
        return False
    # A surname of one letter, or none at all, is not a name.
    surname = (a.family or a.literal or "").strip()
    if len(surname) < 2:
        return False
    # Surnames are capitalised; "теоремы" ("theorems") is not a person. The
    # exception is a nobiliary particle: "van der Kallen", "de Bruijn".
    first = surname.split()[0]
    return first[:1].isupper() or first.lower() in _PARTICLES


def plausible_local_title(title: str) -> bool:
    """Reject OCR debris that happens to sit where a title should be."""
    t = (title or "").strip()
    if not (6 <= len(t) <= 300):
        return False
    if _JUNK_CHARS.search(t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters < 0.6 * len(t):
        return False
    words = t.split()
    if not words:
        return False
    # "C OMPOSITIO M ATHEMATICA" and friends: mostly one-character tokens.
    singles = sum(1 for w in words if len(w.strip(".,;:()")) == 1)
    return singles <= max(1, len(words) // 4)
