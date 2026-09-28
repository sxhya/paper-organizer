"""Hand-kept author aliases: ``.paperorg/authors.tsv``.

The tool merges the spellings of one person by itself (``people.AuthorIndex``),
but some variants are beyond any rule: another script (Вавилов / Vavilov), a
romanisation (Victor / Viktor), a middle name used as the first name (Skip /
Ryan Skip), a typo in a registry. A row here maps one spelling to the name the
library should use instead. It is applied to every record after the automatic
merge, so a variant is written exactly as its folder was named.

Columns (tab-separated, the header line names them, ``#`` starts a comment):

  variant    the spelling to replace, as a folder name: ``Vavilov, N. A``
  canonical  the name to use instead: ``Vavilov, Nikolai``

An alias applies to every document, so list only true variants of one person.
A junk "author" that stands for different people in different files belongs
in overrides.tsv, one row per file.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .record import Author, BibRecord
from .repo import author_folder


def parse_name(text: str) -> Author:
    """``Family, Given`` -> Author; a name without a comma stays literal."""
    family, comma, given = text.partition(",")
    if not comma:
        return Author(literal=text.strip())
    return Author(family=family.strip(), given=given.strip())


def load(path: Path) -> dict[str, Author]:
    """Canonical authors keyed by the folder name of each variant."""
    if not path.exists():
        return {}
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    aliases: dict[str, Author] = {}
    for row in csv.DictReader(lines, delimiter="\t", quoting=csv.QUOTE_NONE):
        variant = (row.get("variant") or "").strip()
        canonical = (row.get("canonical") or "").strip()
        if variant and canonical:
            aliases[author_folder(parse_name(variant))] = parse_name(canonical)
    return aliases


def apply(aliases: dict[str, Author], records: list[BibRecord]) -> int:
    """Rename aliased authors in place; return how many names changed."""
    changed = 0
    for rec in records:
        authors: list[Author] = []
        seen: set[str] = set()
        for a in rec.authors:
            canonical = aliases.get(author_folder(a))
            if canonical is not None:
                a = Author(canonical.family, canonical.given, canonical.literal)
                changed += 1
            # A variant and its canonical form on one paper are one person.
            if author_folder(a) not in seen:
                seen.add(author_folder(a))
                authors.append(a)
        rec.authors = authors
    return changed
