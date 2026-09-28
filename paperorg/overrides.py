"""Per-file corrections the user writes by hand: ``.paperorg/overrides.tsv``.

The resolver is sometimes confidently wrong: a thesis matched to the author's
paper of a similar title, part II matched to part I, an identifier *cited* on
the first page taken for the document's own. The version prompt cannot fix
that - it can only keep or drop files - so a row here pins the record of one
input file instead. That file is then never asked about, and it is filed
under what the row says.

Columns (tab-separated, the header line names them, ``#`` starts a comment):

  file      path relative to the input folder (required)
  kind      paper | book | thesis | other
  doi       fetch the record by DOI (Crossref)
  arxiv     fetch the record by arXiv id
  title, authors, year, journal, volume, number, pages, school, thesis_type,
  language, note
            set these fields; they win over a fetched record. Separate
            authors with ";" ("Viktor Petrov; Anastasia Stavrova").

A row with neither ``doi`` nor ``arxiv`` builds the record from its own
fields alone, so a document no registry holds can still be filed correctly.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .record import BibRecord, DocKind

COLUMNS = ("file", "kind", "doi", "arxiv", "title", "authors", "year", "journal",
           "volume", "number", "pages", "school", "thesis_type", "language", "note")
_FIELDS = ("title", "year", "journal", "volume", "number", "pages", "school",
           "thesis_type", "language")


def load(path: Path) -> dict[str, dict[str, str]]:
    """Rows keyed by input-relative path; an absent file means no overrides."""
    if not path.exists():
        return {}
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    rows: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(lines, delimiter="\t", quoting=csv.QUOTE_NONE):
        rel = (row.get("file") or "").strip()
        if not rel:
            continue
        rows[rel] = {k: (v or "").strip() for k, v in row.items() if k in COLUMNS}
    return rows


def build(row: dict[str, str], fetched: BibRecord | None,
          parse_authors) -> BibRecord:
    """The record a row describes: the fetched one (if any) with its fields."""
    rec = fetched or BibRecord(doi=row.get("doi", ""), arxiv=row.get("arxiv", ""))
    rec.source = (rec.source + "+override") if fetched else "override"
    rec.confidence = 1.0
    for name in _FIELDS:
        if row.get(name):
            setattr(rec, name, row[name])
    if row.get("authors"):
        rec.authors = [a for blob in row["authors"].split(";")
                       for a in parse_authors(blob.strip())[:1]]
    if row.get("note"):
        rec.bib_note = row["note"]
    if row.get("kind"):
        rec.kind = DocKind(row["kind"].lower())
    elif rec.kind is DocKind.UNKNOWN:
        rec.kind = DocKind.PAPER
    return rec
