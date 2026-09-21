"""The end-to-end run: scan -> analyse -> classify -> resolve -> place -> bib."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import bibtex
from .cache import Cache, NullCache
from .classify import classify
from .config import Config, DJVU_EXTS, PDF_EXTS
from .dedupe import Item, group_bib_duplicates, group_byte_duplicates, rank_versions
from .extract import (EXTRACT_VERSION, analyse, extract_school,
                      name_stem, sniff_format)
from .match import Resolver, parse_author_list
from .people import (AuthorIndex, plausible_local_title,
                     plausible_person, prefer_richer)
from .prompts import BOLD, CYAN, DIM, GREEN, RESET, YELLOW, Prompter
from .record import BibRecord, DocFeatures, DocKind
from .repo import RepoWriter, plan_placement
from .sources import Arxiv, Crossref, Http, OpenLibrary, Zbmath


class Progress:
    """One-line progress that behaves on a pipe as well as a terminal."""

    def __init__(self, total: int, label: str, enabled: bool = True) -> None:
        self.total, self.label = total, label
        self.n, self.t0 = 0, time.time()
        self.enabled = enabled and total > 0
        self.tty = sys.stderr.isatty()
        self.last_print = 0.0

    def step(self, extra: str = "") -> None:
        if not self.enabled:
            return
        self.n += 1
        if not self.tty:
            # On a pipe, report on a timer as well as a counter: a slow phase
            # should still show that it is moving.
            due = (self.n % 50 == 0 or self.n == self.total
                   or time.time() - self.last_print > 30)
            if not due:
                return
            self.last_print = time.time()
        el = time.time() - self.t0
        rate = self.n / el if el > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        msg = (f"  {self.label}: {self.n}/{self.total} "
               f"({100*self.n/self.total:.0f}%) {rate:.1f}/s eta {eta:5.0f}s  {extra[:46]}")
        end = "\n" if not self.tty else ""
        print(f"\r{msg:<118}"[:160], end=end, file=sys.stderr, flush=True)

    def done(self) -> None:
        if self.enabled and self.tty:
            print(file=sys.stderr)


class Organizer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Opening the cache creates <output>/.paperorg, so the "does the
        # output folder already exist?" question has to be answered first.
        self.output_existed = cfg.output_dir.exists()
        self.cache = NullCache() if cfg.cache_path is None else Cache(cfg.cache_path)
        http = Http(cfg, self.cache)
        self.resolver = Resolver(cfg, Crossref(http, cfg), Arxiv(http, cfg),
                                 Zbmath(http, cfg), OpenLibrary(http, cfg))
        self.prompter = Prompter(cfg, self.cache)
        self.writer = RepoWriter(cfg)
        self.stats: dict[str, int] = {}
        self.skipped_files = 0
        self.sniffed = 0

    # -------------------------------------------------------------- 1. scan
    def scan(self) -> list[Path]:
        """Collect candidate documents, by extension or by content.

        Files whose name lost its extension are common in an unsorted pile,
        so anything the name does not classify is sniffed for a PDF/DjVu
        header before being discarded.
        """
        exts = set(PDF_EXTS)
        if self.cfg.djvu:
            exts |= DJVU_EXTS
        found: list[Path] = []
        self.skipped_files = 0
        sniffed: set[Path] = set()
        for p in sorted(self.cfg.input_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            if self.cfg.only and not any(s.lower() in str(p).lower() for s in self.cfg.only):
                continue
            if p.suffix.lower() not in exts:
                fmt = sniff_format(p)
                if not fmt or (fmt == "djvu" and not self.cfg.djvu):
                    self.skipped_files += 1
                    continue
                sniffed.add(p)
            found.append(p)
        if self.cfg.sample and self.cfg.sample < len(found):
            import random
            found = sorted(random.Random(self.cfg.seed).sample(found, self.cfg.sample))
        if self.cfg.limit:
            found = found[: self.cfg.limit]
        # Report only what survived --sample/--limit, so the counts add up.
        self.sniffed = sum(1 for p in found if p in sniffed)
        return found

    # ----------------------------------------------------------- 2. analyse
    def analyse_all(self, paths: list[Path]) -> list[Item]:
        items: list[Item] = []
        prog = Progress(len(paths), "reading files")

        def work(path: Path) -> DocFeatures:
            from .util import crc32_file
            try:
                crc = crc32_file(path)
            except OSError as exc:
                f = DocFeatures(path=path, error=str(exc))
                return f
            crc_key = f"{crc}:v{EXTRACT_VERSION}"
            cached = self.cache.get_doc(crc_key)
            if cached:
                cached = dict(cached)
                cached["path"] = path
                cached.setdefault("text", "")
                try:
                    feat = DocFeatures(**cached)
                except TypeError:
                    feat = None
                if feat is not None:
                    if feat.text:
                        return feat
                    if not feat.ocr_used:
                        # Cheap and deterministic: just read it again.
                        from .extract import pdf_text, djvu_text
                        feat.text = (djvu_text(path) if path.suffix.lower() in DJVU_EXTS
                                     else pdf_text(path))
                        return feat
                    # An OCR'd scan with no cached text would classify
                    # differently from a fresh run, so redo the analysis.
            feat = analyse(path, self.cfg)
            self.cache.put_doc(crc_key, feat.to_json())
            return feat

        with ThreadPoolExecutor(max_workers=self.cfg.jobs) as pool:
            futures = {pool.submit(work, p): p for p in paths}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    feat = fut.result()
                except Exception as exc:                       # pragma: no cover
                    feat = DocFeatures(path=path, error=f"analysis failed: {exc}")
                items.append(Item(feat=feat))
                prog.step(path.name)
        prog.done()
        items.sort(key=lambda i: str(i.path))
        return items

    # ---------------------------------------------------------- 3. classify
    def classify_all(self, items: list[Item]) -> None:
        for it in items:
            if it.status != "pending":
                continue
            if it.feat.error:
                it.status, it.note = "skipped", it.feat.error
                continue
            kind, conf, reason = classify(it.feat)
            it.feat_kind, it.kind_conf = kind, conf
            it.kind_reason = reason

    # ----------------------------------------------------------- 4. resolve
    def resolve_all(self, items: list[Item]) -> None:
        todo = [i for i in items
                if i.status == "pending" and i.feat_kind is not DocKind.OTHER]
        prog = Progress(len(todo), "looking up metadata")

        def work(it: Item):
            return self.resolver.resolve(it.feat, it.feat_kind)

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.crossref_workers)) as pool:
            futures = {pool.submit(work, it): it for it in todo}
            for fut in as_completed(futures):
                it = futures[fut]
                try:
                    rec, cands = fut.result()
                except Exception as exc:                       # pragma: no cover
                    rec, cands = None, []
                    it.note = f"lookup failed: {exc}"
                it.record, it.candidates = rec, cands
                prog.step(it.path.name)
        prog.done()

    # ------------------------------------------------- 5. human decisions
    def decide(self, items: list[Item]) -> None:
        # 5a. things we could not classify as scholarly output
        for it in items:
            if it.status != "pending":
                continue
            if it.feat_kind is DocKind.OTHER or (it.record is None and not it.candidates
                                                 and it.feat_kind is DocKind.UNKNOWN):
                action = self.prompter.classify_unknown(it, it.kind_reason)
                if action == "ignore":
                    it.status, it.note = "skipped", "ignored by user"
                elif action == "unsorted":
                    it.record = self._fallback_record(it, DocKind.OTHER)
                else:
                    kind = {"paper": DocKind.PAPER, "book": DocKind.BOOK,
                            "thesis": DocKind.THESIS}[action]
                    it.feat_kind = kind
                    rec, cands = self.resolver.resolve(it.feat, kind)
                    it.record, it.candidates = rec, cands
                    if not it.record:
                        it.record = self.prompter.confirm_match(it, cands) \
                            or self._fallback_record(it, kind)

        # 5b. uncertain matches
        for it in items:
            if it.status != "pending" or it.record is not None:
                continue
            chosen = self.prompter.confirm_match(it, it.candidates)
            it.record = chosen or self._fallback_record(it, it.feat_kind)
            if not chosen:
                it.note = it.note or "no registry match; metadata read from the file"

        # 5c. several files, one bibliographic entry
        for key, group in group_bib_duplicates(items).items():
            ordered = rank_versions(group)
            pick = self.prompter.choose_version(ordered[0].record, ordered)
            if pick is None:
                for it in ordered:
                    it.status, it.note = "skipped", "duplicate entry, skipped by user"
            elif pick == -1:
                continue                     # keep all; unique_path adds suffixes
            else:
                for i, it in enumerate(ordered):
                    if i != pick:
                        it.status = "duplicate"
                        it.note = f"same entry as {ordered[pick].path.name}"

    def _fallback_record(self, it: Item, kind: DocKind) -> BibRecord:
        """Build an entry from the file alone when no registry could help."""
        feat = it.feat
        rec = BibRecord(kind=kind, source="local", confidence=0.3)
        candidates = [t for t in feat.title_candidates() if plausible_local_title(t)]
        rec.title = candidates[0] if candidates else name_stem(feat.path)
        # File name order + title-page spelling: "A. Suslin" -> "Andrei Suslin".
        rec.authors = [a for a in prefer_richer(
            parse_author_list(feat.file_authors),
            parse_author_list(feat.page_authors or feat.meta_author))
            if plausible_person(a)]
        rec.arxiv, rec.doi = feat.arxiv_id, feat.doi
        rec.mrnumber, rec.isbn = feat.mr, feat.isbn
        import re
        m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", feat.text[:2000])
        rec.year = m.group(1) if m else ""
        rec.note = ""
        return rec

    # ---------------------------------------------------- 5c-bis. thesis school
    def fill_thesis_schools(self, items: list[Item]) -> int:
        """A thesis names its university on the title page; registries do not."""
        filled = 0
        for it in items:
            rec = it.record
            if it.status != "pending" or not rec or rec.kind is not DocKind.THESIS:
                continue
            if rec.school:
                continue
            school = extract_school(it.feat.text)
            if school:
                rec.school = school
                filled += 1
        return filled

    # ------------------------------------------------- 5d. author identities
    def unify_authors(self, items: list[Item]) -> dict:
        """Give every person one folder, whatever spelling a source used."""
        records = [i.record for i in items if i.status == "pending" and i.record]
        index = AuthorIndex()
        index.add_records(records)
        index.build()
        index.apply(records)
        return index.stats()

    # -------------------------------------------------------------- 6. place
    def materialise(self, items: list[Item]) -> list[str]:
        placeable = [i for i in items if i.status == "pending" and i.record]
        if not self.cfg.dry_run:
            self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)

        entries: list[str] = []
        keys: set[str] = set()
        prog = Progress(len(placeable), "building repository")
        for it in placeable:
            rec = it.record
            placement = plan_placement(rec, it.feat.suffix)
            try:
                actual = self.writer.place(it.path, placement, crc=it.feat.crc32)
            except OSError as exc:
                it.status, it.note = "skipped", f"copy failed: {exc}"
                prog.step(it.path.name)
                continue
            it.placement = actual
            it.status = "placed"
            if it.duplicates:
                it.note = (it.note + "; " if it.note else "") + \
                    f"{len(it.duplicates)} byte-identical copy/copies collapsed"
            key = bibtex.cite_key(rec, keys)
            entries.append(bibtex.format_entry(rec, key, str(actual.primary)))
            prog.step(it.path.name)
        prog.done()
        return entries

    # -------------------------------------------------------------- 7. report
    def write_outputs(self, items: list[Item], entries: list[str]) -> None:
        # A dry run still writes the plan: inspecting report.csv before
        # committing is the whole point of running one.
        self._write_report(items)
        if self.cfg.dry_run:
            return
        placed = sum(1 for i in items if i.status == "placed")
        header = (f"input:  {self.cfg.input_dir}\n"
                  f"placed: {placed} documents, {len(self.writer.linked)} co-author symlinks")
        bibtex.write_bib(self.cfg.bib_path, entries, header=header)

    def _write_report(self, items: list[Item]) -> None:
        import csv
        path = self.cfg.state_dir / "report.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["status", "kind", "confidence", "source", "identifier",
                        "title", "authors", "year", "placed_at", "input_path", "note"])
            for it in items:
                rec = it.record
                w.writerow([
                    it.status,
                    (rec.kind.value if rec else getattr(it, "feat_kind", DocKind.UNKNOWN).value),
                    f"{rec.confidence:.2f}" if rec else "",
                    rec.source if rec else "",
                    rec.identifier if rec else "",
                    rec.title if rec else "",
                    "; ".join(a.full for a in rec.authors) if rec else "",
                    rec.year if rec else "",
                    str(it.placement.primary) if it.placement else "",
                    str(it.path.relative_to(self.cfg.input_dir)),
                    it.note,
                ])

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        cfg = self.cfg
        print(f"{BOLD}paper-organizer{RESET}  {cfg.input_dir}  ->  {cfg.output_dir}")
        if cfg.dry_run:
            print(f"  {YELLOW}dry run: no document is copied; the plan goes to "
                  f"{cfg.state_dir.name}/report.csv{RESET}")

        paths = self.scan()
        extra = ""
        if self.sniffed:
            extra += f", {self.sniffed} found by content (broken/missing extension)"
        print(f"  found {BOLD}{len(paths)}{RESET} documents{extra}")
        if self.skipped_files:
            where = " in the input tree" if (self.cfg.sample or self.cfg.limit) else ""
            print(f"  {DIM}ignored {self.skipped_files} file(s){where} that are "
                  f"neither PDF nor DjVu (.ps, .dvi, .tex, ...){RESET}")
        if not paths:
            return {"found": 0}

        if not cfg.dry_run and self.output_existed and \
                not self.prompter.confirm_overwrite(cfg.output_dir, len(paths)):
            print("  aborted.")
            return {"aborted": True}

        all_items = self.analyse_all(paths)
        items, byte_dupes = group_byte_duplicates(all_items)
        if byte_dupes:
            print(f"  {DIM}dropped {byte_dupes} byte-identical duplicate(s){RESET}")
        self.classify_all(items)

        kinds: dict[str, int] = {}
        for it in items:
            if it.status == "pending":
                kinds[it.feat_kind.value] = kinds.get(it.feat_kind.value, 0) + 1
        print("  classified: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))

        if cfg.online:
            self.resolve_all(items)
        self.decide(items)
        schools = self.fill_thesis_schools(items)
        if schools:
            print(f"  {DIM}read the awarding institution off {schools} thesis "
                  f"title page(s){RESET}")
        people = self.unify_authors(items)
        if people["merged_variants"]:
            print(f"  {DIM}merged {people['merged_variants']} author name "
                  f"variant(s) into one folder{RESET}")
        entries = self.materialise(items)
        # Report on every input file, including the byte-identical copies that
        # were set aside before classification.
        self.write_outputs(all_items, entries)
        removed = self.writer.prune_empty()

        return self._summarise(all_items, entries, byte_dupes, removed)

    def _summarise(self, items, entries, byte_dupes, pruned) -> dict:
        placed = [i for i in items if i.status == "placed"]
        by_kind: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for i in placed:
            by_kind[i.record.kind.value] = by_kind.get(i.record.kind.value, 0) + 1
            src = i.record.source or "?"
            by_source[src] = by_source.get(src, 0) + 1
        stats = {
            "found": len(items),
            "byte_duplicates": byte_dupes,
            "placed": len(placed),
            "entry_duplicates": sum(1 for i in items if i.status == "duplicate"),
            "byte_duplicates_seen": sum(1 for i in items
                                        if i.status == "byte-duplicate"),
            "skipped": sum(1 for i in items if i.status == "skipped"),
            "symlinks": len(self.writer.linked),
            "reused": len(self.writer.reused),
            "identified": sum(1 for i in placed if i.record.identifier),
            "by_kind": by_kind,
            "by_source": by_source,
            "entries": len(entries),
        }
        self.stats = stats
        return stats


def print_summary(cfg: Config, stats: dict) -> None:
    if not stats or stats.get("found") == 0:
        print("  nothing to do.")
        return
    if stats.get("aborted"):
        return
    ident = stats["identified"]
    placed = max(stats["placed"], 1)
    print(f"\n{BOLD}Summary{RESET}")
    print(f"  input documents      {stats['found']}")
    print(f"  placed in repository {GREEN}{stats['placed']}{RESET}"
          f"  ({stats['symlinks']} co-author symlinks)")
    print(f"  with a DOI/arXiv id  {ident} ({100*ident/placed:.0f}% of placed)")
    print(f"  byte duplicates      {stats['byte_duplicates']}")
    print(f"  same-entry duplicates{stats['entry_duplicates']:>4}")
    print(f"  skipped              {stats['skipped']}")
    if stats["by_kind"]:
        print("  kinds                " + ", ".join(f"{v} {k}" for k, v in
                                                    sorted(stats["by_kind"].items())))
    if stats["by_source"]:
        print("  metadata from        " + ", ".join(f"{v}x {k}" for k, v in
                                                    sorted(stats["by_source"].items(),
                                                           key=lambda x: -x[1])))
    if not cfg.dry_run:
        print(f"\n  bibliography  {CYAN}{cfg.bib_path}{RESET}")
    print(f"  run report    {CYAN}{cfg.state_dir / 'report.csv'}{RESET}")
