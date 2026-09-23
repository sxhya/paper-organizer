"""Duplicate detection.

Two distinct notions are in play:

* **byte duplicates** - identical CRC32 (and size). The extra copies are
  dropped silently; only one file is ever placed in the repository.
* **bibliographic duplicates** - different bytes, same bibliographic entry
  (a preprint and the published version, a rescan, a different journal
  offprint). Those need a human decision, so they are collected and handed
  to the prompt layer.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .record import BibRecord, DocFeatures, DocKind
from .util import norm


@dataclass
class Item:
    """One input file as it travels through the pipeline."""
    feat: DocFeatures
    record: BibRecord | None = None
    feat_kind: DocKind = DocKind.UNKNOWN     # classifier verdict
    kind_conf: float = 0.0
    kind_reason: str = ""
    candidates: list = field(default_factory=list)
    # pending | placed | byte-duplicate | duplicate (same entry) | skipped
    status: str = "pending"
    note: str = ""
    placement: object | None = None
    duplicates: list[Path] = field(default_factory=list)   # byte-identical twins
    lookup_failed: list[str] = field(default_factory=list) # hosts that did not answer

    @property
    def path(self) -> Path:
        return self.feat.path


def group_byte_duplicates(items: list[Item]) -> tuple[list[Item], int]:
    """Collapse byte-identical files, keeping the one with the best path.

    "Best" means the shortest directory depth and then the most informative
    file name, which in this corpus reliably picks the curated copy over the
    one buried in ``unsorted/``.
    """
    by_crc: dict[tuple[str, int], list[Item]] = defaultdict(list)
    for it in items:
        by_crc[(it.feat.crc32, it.feat.size)].append(it)

    kept: list[Item] = []
    dropped = 0
    for group in by_crc.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=_copy_rank)
        winner, rest = group[0], group[1:]
        winner.duplicates = [i.path for i in rest]
        for extra in rest:
            extra.status = "byte-duplicate"
            extra.note = f"byte-identical to {winner.path.name}"
        dropped += len(rest)
        kept.append(winner)
    return kept, dropped


def _copy_rank(item: Item) -> tuple:
    p = item.path
    name = p.stem
    has_authors = 0 if item.feat.file_authors else 1
    unsorted = 1 if any(h in ("unsorted", "tmp", "temp", "new", "inbox")
                        for h in item.feat.dir_hints) else 0
    return (unsorted, has_authors, len(p.parts), -len(name), str(p))


def group_bib_duplicates(items: list[Item]) -> dict[str, list[Item]]:
    """Group *resolved* items that share a bibliographic identity."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if it.status != "pending" or not it.record:
            continue
        key = it.record.identifier
        if not key:
            first = it.record.first_author()
            who = norm(first.family if first else "")
            key = f"title:{norm(it.record.title)}|{who}"
        groups[key].append(it)
    return {k: v for k, v in groups.items() if len(v) > 1}


def rank_versions(items: list[Item]) -> list[Item]:
    """Order bibliographic duplicates best-first, to offer a sane default.

    Preference: a real text layer over a scan, then more pages (offprints are
    sometimes truncated), then the larger file, then the tidier name.
    """
    def key(it: Item) -> tuple:
        searchable = 0 if (it.feat.text_chars > 800 and not it.feat.ocr_used) else 1
        return (searchable, -it.feat.pages, -it.feat.size, _copy_rank(it))
    return sorted(items, key=key)
