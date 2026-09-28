"""The end-to-end run: scan -> analyse -> classify -> resolve -> place -> bib."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import aliases, bibtex, overrides
from .cache import Cache, NullCache
from .classify import classify
from .config import Config, DJVU_EXTS, PDF_EXTS, PS_EXTS, UNSORTED_DIR
from .dedupe import Item, group_bib_duplicates, group_byte_duplicates, rank_versions
from .extract import (EXTRACT_VERSION, analyse, detect_format, extract_school, have,
                      name_stem, readable_copy, sniff_format)
from .arxivguard import Lexicon
from .arxivplan import ArxivPlan
from .match import Resolver, parse_author_list
from .people import (AuthorIndex, plausible_local_title,
                     plausible_person, prefer_richer)
from .prompts import BOLD, CYAN, DIM, GREEN, RESET, YELLOW, Prompter
from .record import BibRecord, DocFeatures, DocKind
from .repo import RepoWriter, plan_placement
from .util import crc32_file, is_cyrillic, removesuffix
from .sources import Arxiv, Crossref, Http, MathNet, OpenLibrary, Zbmath


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
        http = self.http = Http(cfg, self.cache)
        self.plan = ArxivPlan.load(cfg.arxiv_plan_path if cfg.online else None)
        self.resolver = Resolver(cfg, Crossref(http, cfg), Arxiv(http, cfg),
                                 Zbmath(http, cfg), OpenLibrary(http, cfg),
                                 MathNet(http, cfg), plan=self.plan)
        self.prompter = Prompter(cfg, self.cache)
        self.writer = RepoWriter(cfg)
        self.stats: dict[str, int] = {}
        self.ignored: list[Path] = []      # neither PDF, DjVu nor PostScript
        self.sniffed = 0
        # PostScript or gzipped input -> the PDF/DjVu made from it, which is
        # what gets read and placed in the repository.
        self.converted: dict[Path, Path] = {}

    @property
    def skipped_files(self) -> int:
        return len(self.ignored)

    # -------------------------------------------------------------- 1. scan
    def scan(self) -> list[Path]:
        """Collect candidate documents, by extension or by content.

        Files whose name lost its extension are common in an unsorted pile,
        so anything the name does not classify is sniffed for a PDF/DjVu/PS
        header before being discarded.
        """
        exts = set(PDF_EXTS)
        if self.cfg.djvu:
            exts |= DJVU_EXTS
        if self.cfg.ps:
            exts |= PS_EXTS
        found: list[Path] = []
        self.ignored = []
        sniffed: set[Path] = set()
        for p in sorted(self.cfg.input_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            if self.cfg.only and not any(s.lower() in str(p).lower() for s in self.cfg.only):
                continue
            # "._Name.pdf" is the macOS resource fork of "Name.pdf", not a copy.
            if p.name.startswith("._"):
                self.ignored.append(p)
                continue
            if p.suffix.lower() not in exts:
                fmt = removesuffix(sniff_format(p), ".gz")
                if not fmt or (fmt == "djvu" and not self.cfg.djvu) \
                        or (fmt == "ps" and not self.cfg.ps):
                    self.ignored.append(p)
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
            try:
                crc = crc32_file(path)
            except OSError as exc:
                f = DocFeatures(path=path, error=str(exc))
                return f
            doc, err = readable_copy(path, detect_format(path),
                                     self.cfg.state_dir / "converted", crc,
                                     ps_timeout=self.cfg.ps_timeout)
            if err:
                return DocFeatures(path=path, error=err)
            if doc:
                self.converted[path] = doc
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
                if feat is not None and feat.fmt == "djvu" and not feat.pages \
                        and have("djvused"):
                    feat = None     # read before djvulibre was installed
                if feat is not None:
                    if feat.text:
                        return feat
                    if not feat.ocr_used:
                        # Cheap and deterministic: just read it again.
                        from .extract import pdf_text, djvu_text
                        src = doc or path
                        feat.text = (djvu_text(src) if feat.fmt == "djvu"
                                     else pdf_text(src))
                        return feat
                    # An OCR'd scan with no cached text would classify
                    # differently from a fresh run, so redo the analysis.
            feat = analyse(path, self.cfg, doc)
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
    def resolve_all(self, items: list[Item]) -> bool:
        """Look every document up. False when the run has to stop because a
        registry is refusing to answer (and --on-rate-limit is "stop")."""
        todo = [i for i in items
                if i.status == "pending" and i.feat_kind is not DocKind.OTHER]
        stopping = self.cfg.on_rate_limit == "stop"
        self.resolver.lexicon = Lexicon.from_cache(self.cache.iter_http())
        # Identifiers printed in the documents, fifty to an arXiv request
        # instead of one request each.
        ids = [i.feat.arxiv_id for i in todo if i.feat.arxiv_id]
        if ids:
            sent = self.resolver.arxiv.prefetch(ids)
            if sent:
                print(f"  {DIM}looked up {len(set(ids))} arXiv ids in {sent} "
                      f"request(s){RESET}")
        prog = Progress(len(todo), "looking up metadata")

        # A refusing host is not asked again (Http.blocked), but the lookup
        # carries on: cached answers and the other registries still count,
        # and all of that is progress the next run does not repeat.
        def work(it: Item):
            self.http.begin_item()
            rec, cands = self.resolver.resolve(it.feat, it.feat_kind)
            return rec, cands, sorted(self.http.failed_hosts())

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.crossref_workers)) as pool:
            futures = {pool.submit(work, it): it for it in todo}
            for fut in as_completed(futures):
                it = futures[fut]
                try:
                    rec, cands, it.lookup_failed = fut.result()
                except Exception as exc:                       # pragma: no cover
                    rec, cands = None, []
                    it.note = f"lookup failed: {exc}"
                it.record, it.candidates = rec, cands
                prog.step(it.path.name)
        prog.done()
        self.plan.save()
        if stopping and self.http.blocked:
            return False
        if self.http.blocked:
            print(f"  {YELLOW}{self.http.refusals()} refused this machine; it was "
                  f"not asked again this run. Re-run later to finish those "
                  f"lookups.{RESET}")
            for host in sorted(self.http.blocked):
                print(f"  {self.http.retry_after_note(host)}.")
        return True

    @property
    def resume_path(self) -> Path:
        return self.cfg.state_dir / "RESUME.txt"

    def _write_resume(self, items: list[Item]) -> None:
        """Say why the run stopped and how to pick it up again."""
        import shlex
        from datetime import datetime
        todo = [i for i in items
                if i.status == "pending" and i.feat_kind is not DocKind.OTHER]
        done = sum(1 for i in todo if not i.lookup_failed)
        hosts = ", ".join(sorted(self.http.blocked))
        notes = "".join(f"  {self.http.retry_after_note(h)}.\n"
                        for h in sorted(self.http.blocked))
        cmd = shlex.join(sys.argv) if sys.argv and sys.argv[0] else "./organize.py ..."
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.resume_path.write_text(
            f"paper-organizer stopped at {datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"{self.http.refusals()} started refusing requests.\n"
            f"{notes}"
            f"Looked up {done} of {len(todo)} documents before stopping; nothing\n"
            f"was copied and no questions were asked.\n\n"
            f"Everything done so far is cached. To continue, run the same command\n"
            f"again once the block has lifted (usually an hour or more):\n\n"
            f"    {cmd}\n\n"
            f"To finish now without {hosts}, add  --on-rate-limit skip ; the documents\n"
            f"it could not check are tagged [lookup incomplete] in report.txt, and\n"
            f"a later run fills them in.\n", encoding="utf-8")

    def _write_review_note(self, pending: list[str]) -> None:
        """The run stops so that the arXiv title searches can be checked."""
        import shlex
        from datetime import datetime
        counts = self.plan.counts()
        cmd = shlex.join(sys.argv) if sys.argv and sys.argv[0] else "./organize.py ..."
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.resume_path.write_text(
            f"paper-organizer stopped at {datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"{len(pending)} arXiv title searches wait for review in\n\n"
            f"    {self.cfg.arxiv_plan_path}\n\n"
            f"({counts.get('y?', 0)} suggested to send, {counts.get('n?', 0)} suggested "
            f"not to; {counts.get('n', 0)} certain skips are listed too.)\n\n"
            f"In the first column, replace each y? or n? with y (send) or n (do not).\n"
            f"The query, title and authors columns can be edited; an empty query is\n"
            f"rebuilt from title and authors. The scholar column links to a Google\n"
            f"Scholar search for checking by hand. Then run the same command again:\n\n"
            f"    {cmd}\n\n"
            f"Everything else is looked up and cached; nothing was copied and no\n"
            f"questions were asked. To send the suggestions unchecked, add\n"
            f"--arxiv-titles auto; to leave arXiv title search out, --arxiv-titles off.\n",
            encoding="utf-8")

    # ------------------------------------------------- 5. human decisions
    def apply_overrides(self, items: list[Item]) -> int:
        """Pin the records listed in overrides.tsv; return how many applied."""
        rows = overrides.load(self.cfg.overrides_path)
        if not rows:
            return 0
        applied = 0
        for it in items:
            row = rows.pop(str(it.path.relative_to(self.cfg.input_dir)), None)
            if row is None or it.status != "pending":
                continue
            if row.get("kind", "").lower() == "ignore":
                it.status, it.note = "skipped", "ignored in overrides.tsv"
                applied += 1
                continue
            fetched = None
            if self.cfg.online and row.get("doi"):
                fetched = self.resolver.crossref.by_doi(row["doi"])
            elif self.cfg.online and row.get("arxiv"):
                fetched = self.resolver.arxiv.by_id(row["arxiv"])
            if fetched:
                self.resolver.finish(fetched, it.feat_kind)
            # The row's own fields (kind included) have the last word.
            rec = overrides.build(row, fetched, parse_author_list)
            it.record, it.candidates = rec, []
            it.feat_kind, it.override = rec.kind, True
            it.note = "record set in overrides.tsv"
            applied += 1
        for rel in rows:
            print(f"  {YELLOW}overrides.tsv: no pending document at {rel}{RESET}")
        return applied

    def decide(self, items: list[Item]) -> None:
        # 5a. things we could not classify as scholarly output
        for it in items:
            if it.status != "pending" or it.override:
                continue
            if it.feat_kind is DocKind.OTHER or (it.record is None and not it.candidates
                                                 and it.feat_kind is DocKind.UNKNOWN):
                action = self.prompter.classify_unknown(it, it.kind_reason)
                if action == "ignore":
                    it.status = "skipped"
                    it.note = ("not recognised; left alone" if not self.cfg.interactive
                               else "not recognised; ignored by user")
                elif action == "unsorted":
                    it.record = self._fallback_record(it, DocKind.OTHER)
                else:
                    kind = {"paper": DocKind.PAPER, "book": DocKind.BOOK,
                            "thesis": DocKind.THESIS}[action]
                    it.feat_kind = kind
                    rec, cands = self.resolver.resolve(it.feat, kind)
                    it.record, it.candidates = rec, cands
                    if not it.record:
                        chosen = self.prompter.confirm_match(it, cands)
                        if chosen:
                            self.resolver.finish(chosen, kind)
                        it.record = chosen or self._fallback_record(it, kind)

        # 5b. uncertain matches
        for it in items:
            if it.status != "pending" or it.record is not None or it.override:
                continue
            chosen = self.prompter.confirm_match(it, it.candidates)
            if chosen:
                self.resolver.finish(chosen, it.feat_kind)
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
        stats = index.stats()
        # Hand-kept aliases last: they name the folders the merge produced.
        stats["aliased"] = aliases.apply(aliases.load(self.cfg.aliases_path), records)
        return stats

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
            source = self.converted.get(it.path, it.path)
            try:
                # The repository holds the converted PDF, so a resumed run has
                # to recognise that file, not the PostScript original.
                crc = crc32_file(source) if source != it.path else it.feat.crc32
                actual = self.writer.place(source, placement, crc=crc)
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
        self._write_text_report(items)
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

    def _write_text_report(self, items: list[Item]) -> None:
        """What the run did *not* do, for a human to read: files it left
        behind, files it could not place, and files it placed without being
        sure what they are."""
        from datetime import datetime
        cfg = self.cfg

        def rel(p: Path) -> str:
            try:
                return str(p.relative_to(cfg.input_dir))
            except ValueError:
                return str(p)

        # Russian-language material is what the registries cover worst, so it
        # is flagged to show how much of each section it accounts for.
        def russian(it: Item) -> bool:
            return is_cyrillic(it.feat.text[:4000]) or is_cyrillic(it.path.name)

        def head(it: Item) -> str:
            tags = (["Cyrillic"] if russian(it) else []) + \
                [f"lookup incomplete: {h}" for h in it.lookup_failed]
            return rel(it.path) + "".join(f"   [{t}]" for t in tags)

        placed = [i for i in items if i.status == "placed"]
        vague = (DocKind.OTHER, DocKind.UNKNOWN)
        unsorted = [i for i in placed if i.record.kind in vague]
        local = [i for i in placed
                 if i.record.source == "local" and i.record.kind not in vague]
        skipped = [i for i in items if i.status == "skipped"]
        dupes = [i for i in items if i.status in ("duplicate", "byte-duplicate")]
        converted = [i for i in placed if i.path in self.converted]

        by_ext: dict[str, list[Path]] = {}
        for p in self.ignored:
            key = ("._*  (macOS resource forks)" if p.name.startswith("._")
                   else p.suffix.lower() or "(no extension)")
            by_ext.setdefault(key, []).append(p)

        verb = "would be " if cfg.dry_run else ""
        out = [
            "paper-organizer run report",
            "",
            f"input:   {cfg.input_dir}",
            f"output:  {cfg.output_dir}",
            f"date:    {datetime.now():%Y-%m-%d %H:%M}"
            + ("  (dry run: nothing was copied)" if cfg.dry_run else ""),
            "",
            f"  {len(placed):6}  {verb}placed in the repository",
            f"  {len(unsorted):6}    of them not recognised ({verb}filed in Unsorted/)",
            f"  {len(local):6}    of them with metadata read from the file only",
            f"  {len(converted):6}    of them converted (PostScript, gzip)",
            f"  {len(skipped):6}  skipped",
            f"  {len(dupes):6}  duplicates, not placed",
            f"  {len(self.ignored):6}  not a document, left where it is",
        ]

        def section(title: str, blurb: str, its: list[Item], details) -> None:
            if not its:
                return
            ru = sum(1 for i in its if russian(i))
            short = sum(1 for i in its if i.lookup_failed)
            count = ", ".join([str(len(its))]
                              + ([f"{ru} in Cyrillic"] if ru else [])
                              + ([f"{short} with lookups incomplete"] if short else []))
            out.extend(["", "", f"== {title} ({count}) ==", blurb, ""])
            # Russian items together, so the size of that block is visible.
            for it in sorted(its, key=lambda i: (not russian(i), rel(i.path))):
                out.append(f"  {head(it)}")
                out.extend(f"      {d}" for d in details(it) if d)

        section("Skipped",
                "Read errors, failed conversions, and files you chose not to file.",
                skipped, lambda i: [i.note])
        section("Not recognised",
                "Not identified as a paper, book or thesis; "
                f"{verb}filed in {UNSORTED_DIR}/ under a name guessed from the file.",
                unsorted,
                lambda i: [f"-> {i.placement.primary}",
                           f"why: {i.kind_reason}" if i.kind_reason else ""])
        section("No registry match",
                "Recognised, but title and authors come from the file itself; "
                "worth checking.",
                local,
                lambda i: [f"-> {i.placement.primary}  [{i.record.kind.value}]",
                           f"title read: {i.record.title[:100]}"])
        section("Duplicates",
                "Another copy of the same document was placed instead.",
                dupes, lambda i: [i.note])
        if by_ext:
            n = len(self.ignored)
            out.extend(["", "", f"== Not a document ({n}) ==",
                        "Neither PDF, DjVu nor PostScript"
                        + (" (DjVu is off: --no-djvu)" if not cfg.djvu else "")
                        + (" (PostScript is off: --no-ps)" if not cfg.ps else "")
                        + "; left in the input folder.", ""])
            for ext, paths in sorted(by_ext.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                out.append(f"  {ext}  ({len(paths)})")
                out.extend(f"      {rel(p)}" for p in paths)

        path = cfg.state_dir / "report.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        cfg = self.cfg
        print(f"{BOLD}paper-organizer{RESET}  {cfg.input_dir}  ->  {cfg.output_dir}")
        if cfg.dry_run:
            print(f"  {YELLOW}dry run: no document is copied; the plan goes to "
                  f"{cfg.state_dir.name}/report.txt and report.csv{RESET}")

        paths = self.scan()
        extra = ""
        if self.sniffed:
            extra += f", {self.sniffed} found by content (broken/missing extension)"
        print(f"  found {BOLD}{len(paths)}{RESET} documents{extra}")
        if self.skipped_files:
            where = " in the input tree" if (self.cfg.sample or self.cfg.limit) else ""
            print(f"  {DIM}ignored {self.skipped_files} file(s){where} that are "
                  f"neither PDF, DjVu nor PostScript (.dvi, .tex, ...){RESET}")
        if not paths:
            if self.ignored:
                self._write_text_report([])
                print(f"  report        {CYAN}{cfg.state_dir / 'report.txt'}{RESET}")
            return {"found": 0}

        resuming = self.resume_path.exists()
        if resuming:
            print(f"  {CYAN}resuming the run that stopped earlier "
                  f"({self.resume_path.relative_to(cfg.output_dir)}){RESET}")
        if not cfg.dry_run and self.output_existed and not resuming and \
                not self.prompter.confirm_overwrite(cfg.output_dir, len(paths)):
            print("  aborted.")
            return {"aborted": True}

        all_items = self.analyse_all(paths)
        if self.converted:
            print(f"  {DIM}converted {len(self.converted)} PostScript/gzipped "
                  f"file(s) to PDF/DjVu{RESET}")
        items, byte_dupes = group_byte_duplicates(all_items)
        if byte_dupes:
            print(f"  {DIM}dropped {byte_dupes} byte-identical duplicate(s){RESET}")
        self.classify_all(items)

        kinds: dict[str, int] = {}
        for it in items:
            if it.status == "pending":
                kinds[it.feat_kind.value] = kinds.get(it.feat_kind.value, 0) + 1
        print("  classified: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))

        if cfg.online and not self.resolve_all(items):
            self._write_resume(items)
            hosts = ", ".join(sorted(self.http.blocked))
            print(f"\n  {YELLOW}{self.http.refusals()} is refusing requests, so the run "
                  f"stops here.{RESET}")
            for host in sorted(self.http.blocked):
                print(f"  {self.http.retry_after_note(host)}.")
            print(f"  Progress is cached; see {self.resume_path} for how to continue.")
            return {"interrupted": hosts}
        pending = self.plan.pending() if cfg.online and cfg.arxiv_titles == "review" else []
        if pending:
            self._write_review_note(pending)
            print(f"\n  {YELLOW}{len(pending)} arXiv title search(es) wait for your "
                  f"review, so the run stops here.{RESET}")
            print(f"  Mark them y or n in {CYAN}{cfg.arxiv_plan_path}{RESET}, then run "
                  f"the same command again (details in {self.resume_path.name}).")
            return {"interrupted": "arXiv review"}
        pinned = self.apply_overrides(items)
        if pinned:
            print(f"  {DIM}took {pinned} record(s) from "
                  f"{cfg.overrides_path.name}{RESET}")
        self.decide(items)
        schools = self.fill_thesis_schools(items)
        if schools:
            print(f"  {DIM}read the awarding institution off {schools} thesis "
                  f"title page(s){RESET}")
        people = self.unify_authors(items)
        if people["merged_variants"]:
            print(f"  {DIM}merged {people['merged_variants']} author name "
                  f"variant(s) into one folder{RESET}")
        if people["aliased"]:
            print(f"  {DIM}renamed {people['aliased']} author name(s) from "
                  f"{cfg.aliases_path.name}{RESET}")
        entries = self.materialise(items)
        # Report on every input file, including the byte-identical copies that
        # were set aside before classification.
        self.write_outputs(all_items, entries)
        self.plan.save()
        removed = self.writer.prune_empty()
        self.resume_path.unlink(missing_ok=True)

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
    if stats.get("aborted") or stats.get("interrupted"):
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
    print(f"  run report    {CYAN}{cfg.state_dir / 'report.txt'}{RESET}"
          f"  {DIM}(every file: report.csv){RESET}")
