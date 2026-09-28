"""Materialise the new repository: folders, file placement, symlinks."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import BOOKS_DIR, Config, THESIS_DIR, UNSORTED_DIR
from .record import Author, BibRecord, DocKind
from .util import crc32_file, safe_filename, unique_path


@dataclass
class Placement:
    """Where one document ends up, and which links point at it."""
    primary: Path                       # real file, relative to the repo root
    links: list[Path] = field(default_factory=list)
    kind: DocKind = DocKind.PAPER


def author_folder(author: Author | None) -> str:
    """``Family, Given`` - sorts nicely and keeps namesakes apart."""
    if author is None:
        return "Unknown"
    return safe_filename(author.folder or "Unknown", max_bytes=120)


def _author_suffix(rec: BibRecord, limit: int = 3) -> str:
    """``(J. Milnor, D. Husemoller)`` for book/thesis file names."""
    names = []
    for a in rec.authors[:limit]:
        if a.literal:
            names.append(a.literal)
        else:
            initials = " ".join(f"{p[0]}." for p in a.given.split() if p)
            names.append(f"{initials} {a.family}".strip())
    if len(rec.authors) > limit:
        names.append("et al.")
    return ", ".join(n for n in names if n)


def target_name(rec: BibRecord, suffix: str) -> str:
    """File name (without folder) for a record."""
    title = rec.title + ((": " + rec.subtitle) if rec.subtitle else "")
    if rec.kind in (DocKind.BOOK, DocKind.THESIS):
        who = _author_suffix(rec)
        base = f"{title} ({who})" if who else title
    else:
        base = title
    if rec.year and rec.kind in (DocKind.BOOK, DocKind.THESIS):
        base = f"{base} [{rec.year}]"
    return safe_filename(base) + suffix


def plan_placement(rec: BibRecord, suffix: str) -> Placement:
    """Decide the repo-relative path of the real file and of each symlink."""
    name = target_name(rec, suffix)
    if rec.kind is DocKind.BOOK:
        return Placement(Path(BOOKS_DIR) / name, [], rec.kind)
    if rec.kind is DocKind.THESIS:
        return Placement(Path(THESIS_DIR) / name, [], rec.kind)
    if rec.kind is DocKind.OTHER or rec.kind is DocKind.UNKNOWN:
        return Placement(Path(UNSORTED_DIR) / name, [], rec.kind)

    authors = rec.authors or []
    if not authors:
        return Placement(Path("Unknown") / name, [], rec.kind)
    primary = Path(author_folder(authors[0])) / name
    links = []
    seen = {author_folder(authors[0])}
    for a in authors[1:]:
        folder = author_folder(a)
        if folder in seen:
            continue
        seen.add(folder)
        links.append(Path(folder) / name)
    return Placement(primary, links, rec.kind)


class RepoWriter:
    """Performs (or simulates) the file system side effects."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.output_dir
        self.created_dirs: set[Path] = set()
        self.written: list[Path] = []
        self.linked: list[Path] = []
        self.reused: list[Path] = []

    # ------------------------------------------------------------------ util
    def _ensure_dir(self, d: Path) -> None:
        if d in self.created_dirs:
            return
        if not self.cfg.dry_run:
            d.mkdir(parents=True, exist_ok=True)
        self.created_dirs.add(d)

    # ----------------------------------------------------------------- write
    def place(self, source: Path, placement: Placement, *,
              move: bool = False, crc: str = "") -> Placement:
        """Copy/move *source* into the repo and create the author symlinks.

        Returns a placement with the *actual* paths used. A name clash gets a
        ``(2)`` suffix and the links follow the renamed target - except when
        the file already sitting there *is* this document, which happens when
        an interrupted run is resumed; then it is left alone.
        """
        dest = self.root / placement.primary
        self._ensure_dir(dest.parent)
        reused = False
        if not self.cfg.dry_run:
            if crc and self._same_document(dest, crc):
                reused = True
            elif crc and (twin := self._numbered_twin(dest, crc)):
                # "Title.pdf" belongs to another document; an earlier run put
                # this one at "Title (3).pdf", so that is where it still is.
                dest, reused = twin, True
            else:
                dest = unique_path(dest)
        primary_rel = dest.relative_to(self.root)

        if not self.cfg.dry_run and not reused:
            if move:
                shutil.move(str(source), str(dest))
            else:
                shutil.copy2(source, dest)
        if reused:
            self.reused.append(primary_rel)
        self.written.append(primary_rel)

        links: list[Path] = []
        if self.cfg.link_mode != "none":
            for link_rel in placement.links:
                link_path = self.root / link_rel.parent / dest.name
                self._ensure_dir(link_path.parent)
                if not self.cfg.dry_run:
                    # A link left by an earlier, interrupted run already points
                    # where we want it; replacing it in place beats adding a
                    # "(2)" twin next to it.
                    if not self._points_at(link_path, dest):
                        # The same, for a link numbered because its name was
                        # taken: find it rather than add "(3)" beside "(2)".
                        twin = next((q for q in self._numbered_siblings(link_path)
                                     if self._points_at(q, dest)), None)
                        link_path = twin or unique_path(link_path)
                    self._make_link(dest, link_path)
                links.append(link_path.relative_to(self.root))
                self.linked.append(link_path.relative_to(self.root))
        return Placement(primary_rel, links, placement.kind)

    @staticmethod
    def _points_at(link: Path, target: Path) -> bool:
        if not link.is_symlink():
            return False
        try:
            return link.resolve() == target.resolve()
        except OSError:
            return False

    @staticmethod
    def _same_document(dest: Path, crc: str) -> bool:
        """Is the file already at *dest* byte-identical to the one we place?"""
        if not dest.is_file() or dest.is_symlink():
            return False
        try:
            return crc32_file(dest) == crc
        except OSError:
            return False

    @staticmethod
    def _numbered_siblings(path: Path) -> list[Path]:
        """``path (2)``, ``path (3)``, ... as they exist on disk, in order.

        Empty while *path* itself is free: a free clean name is always
        preferred to a numbered one.
        """
        if not (path.exists() or path.is_symlink()) or not path.parent.is_dir():
            return []
        pattern = re.compile(re.escape(path.stem) + r" \((\d+)\)" + re.escape(path.suffix))
        found = sorted((int(m.group(1)), path.parent / m.group(0))
                       for m in map(pattern.fullmatch, os.listdir(path.parent)) if m)
        return [p for _, p in found]

    @classmethod
    def _numbered_twin(cls, dest: Path, crc: str) -> Path | None:
        """The ``dest (N)`` sibling already holding this document, if any."""
        return next((p for p in cls._numbered_siblings(dest)
                     if cls._same_document(p, crc)), None)

    def _make_link(self, target: Path, link: Path) -> None:
        """Relative symlink so the whole repository stays movable."""
        rel = os.path.relpath(target, link.parent)
        if link.is_symlink() or link.exists():
            link.unlink()
        if self.cfg.link_mode == "copy":
            shutil.copy2(target, link)
            return
        try:
            link.symlink_to(rel)
        except OSError:
            shutil.copy2(target, link)     # e.g. a filesystem without symlinks

    # --------------------------------------------------------------- cleanup
    def prune_empty(self) -> int:
        """Remove folders that ended up empty (dry runs create nothing)."""
        if self.cfg.dry_run or not self.root.exists():
            return 0
        removed = 0
        for d in sorted((p for p in self.root.rglob("*") if p.is_dir()),
                        key=lambda p: -len(p.parts)):
            if d.name == ".paperorg":
                continue
            try:
                next(d.iterdir())
            except StopIteration:
                d.rmdir()
                removed += 1
            except OSError:
                pass
        return removed
