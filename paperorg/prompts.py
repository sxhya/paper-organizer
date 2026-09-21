"""All human interaction lives here, so the rest of the pipeline stays pure."""
from __future__ import annotations

import sys
from pathlib import Path

from .config import Config
from .record import BibRecord
from .util import human_size

def _use_colour() -> bool:
    """Colour only for a real terminal, and never when NO_COLOR is set."""
    import os
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty()


_C = _use_colour()
BOLD = "\033[1m" if _C else ""
DIM = "\033[2m" if _C else ""
RESET = "\033[0m" if _C else ""
CYAN = "\033[36m" if _C else ""
YELLOW = "\033[33m" if _C else ""
GREEN = "\033[32m" if _C else ""


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class Prompter:
    """Asks the user; remembers answers so a resumed run stays quiet."""

    _UNKNOWN_Q = "What should happen to this file?"

    def __init__(self, cfg: Config, cache) -> None:
        self.cfg = cfg
        self.cache = cache
        self.enabled = cfg.interactive and _tty()
        self.apply_all: dict[str, str] = {}

    # ------------------------------------------------------------------ core
    def _ask(self, question: str, options: list[tuple[str, str]],
             default: str, *, memo_key: str | None = None,
             allow_all: bool = True) -> str:
        if memo_key:
            remembered = self.cache.get_decision(memo_key)
            if remembered:
                return remembered
        if not self.enabled:
            return default

        keys = {o[0] for o in options}
        print(question)
        for key, label in options:
            mark = " (default)" if key == default else ""
            print(f"    {BOLD}[{key}]{RESET} {label}{mark}")
        if allow_all:
            print(f"    {DIM}suffix '!' applies your choice to all later cases"
                  f" (e.g. '{default}!'){RESET}")
        while True:
            try:
                raw = input(f"  choice [{default}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            if not raw:
                choice = default
            else:
                choice = raw.rstrip("!").lower()
                if raw.endswith("!") and choice in keys:
                    self.apply_all[question.split("\n")[0][:40]] = choice
            if choice in keys:
                if memo_key:
                    self.cache.put_decision(memo_key, choice)
                return choice
            print(f"  {YELLOW}please answer one of: {', '.join(sorted(keys))}{RESET}")

    # -------------------------------------------------- bibliographic clashes
    def choose_version(self, record: BibRecord, items: list) -> int | None:
        """Several files map to one entry. Return the index to keep, or None."""
        head = (f"\n{BOLD}Several files match one bibliographic entry:{RESET}\n"
                f"  {CYAN}{record.display}{RESET}")
        lines = []
        for i, it in enumerate(items, 1):
            flags = []
            if it.feat.ocr_used:
                flags.append("scanned/OCR")
            elif it.feat.text_chars > 800:
                flags.append("text layer")
            else:
                flags.append("little text")
            flags.append(f"{it.feat.pages}p")
            flags.append(human_size(it.feat.size))
            rel = it.path
            lines.append(f"  {BOLD}[{i}]{RESET} {rel.name}\n"
                         f"      {DIM}{'; '.join(flags)} - crc {it.feat.crc32}{RESET}\n"
                         f"      {DIM}{rel.parent}{RESET}")
        print(head)
        print("\n".join(lines))
        if not self.enabled:
            return 0                      # the pre-ranked best candidate
        options = [(str(i), f"keep {items[i-1].path.name}") for i in range(1, len(items) + 1)]
        options.append(("a", "keep all of them (names get a numeric suffix)"))
        options.append(("s", "skip all of them"))
        memo = f"version:{record.identifier or record.title}"
        ans = self._ask("  Which file should the entry point to?", options, "1",
                        memo_key=memo, allow_all=False)
        if ans == "a":
            return -1
        if ans == "s":
            return None
        return int(ans) - 1

    # ------------------------------------------------------ unknown documents
    UNKNOWN_ACTIONS = {"u": "unsorted", "i": "ignore", "p": "paper",
                       "b": "book", "t": "thesis"}

    def classify_unknown(self, item, reason: str) -> str:
        """Return one of: unsorted | ignore | paper | book | thesis."""
        blanket = self.apply_all.get(self._UNKNOWN_Q[:40])
        if blanket:
            return self.UNKNOWN_ACTIONS[blanket]
        if not self.enabled:
            return self.cfg.default_unknown_action

        feat = item.feat
        print(f"\n{BOLD}Not recognised as a paper, book or thesis:{RESET}")
        print(f"  {CYAN}{feat.path.relative_to(self.cfg.input_dir)}{RESET}")
        print(f"  {DIM}{feat.pages}p, {human_size(feat.size)}, "
              f"{feat.text_chars} chars of text - {reason}{RESET}")
        for label, value in (("title guess", feat.page_title or feat.file_title),
                             ("authors guess", feat.author_hint())):
            if value:
                print(f"  {DIM}{label}: {value[:96]}{RESET}")
        ans = self._ask(self._UNKNOWN_Q,
                        [("u", f"move to {self.cfg.output_dir.name}/Unsorted"),
                         ("i", "ignore (leave it where it is)"),
                         ("p", "treat as a paper and look it up"),
                         ("b", "treat as a book"),
                         ("t", "treat as a thesis")],
                        "u")
        return self.UNKNOWN_ACTIONS[ans]

    # ------------------------------------------------------ uncertain matches
    def confirm_match(self, item, candidates) -> BibRecord | None:
        """Offer the near-misses when nothing crossed the accept threshold."""
        if not candidates:
            return None
        blanket = self.apply_all.get("Which record describes this file?"[:40])
        if blanket == "n":
            return None
        if not self.enabled:
            return None

        feat = item.feat
        print(f"\n{BOLD}Uncertain match:{RESET} {CYAN}{feat.path.name}{RESET}")
        if feat.file_title:
            print(f"  {DIM}from file name : {feat.file_authors or '?'} - {feat.file_title}{RESET}")
        if feat.page_title:
            print(f"  {DIM}from first page: {feat.page_title[:96]}{RESET}")
        options = []
        for i, c in enumerate(candidates[:5], 1):
            print(f"  {BOLD}[{i}]{RESET} {c.record.display}")
            print(f"      {DIM}{c.record.journal or c.record.publisher or '-'} - "
                  f"{c.record.identifier or 'no id'} - score {c.score:.2f} ({c.why}){RESET}")
            options.append((str(i), f"use candidate {i}"))
        options.append(("n", "none of these"))
        ans = self._ask("Which record describes this file?", options, "n")
        if ans == "n":
            return None
        chosen = candidates[int(ans) - 1]
        rec = chosen.record
        rec.confidence = chosen.score
        rec.source += "+manual"
        return rec

    # -------------------------------------------------------------- overwrite
    def confirm_overwrite(self, path: Path, n_files: int) -> bool:
        if not path.exists():
            return True
        contents = sum(1 for p in path.rglob("*")
                       if ".paperorg" not in p.relative_to(path).parts)
        if not contents:
            return True
        print(f"\n{YELLOW}The output folder already exists:{RESET} {path}")
        print(f"  {DIM}it currently holds {contents} entries; "
              f"{n_files} documents are about to be added{RESET}")
        if not self.enabled:
            return True
        return self._ask("Continue?", [("y", "continue, merging into it"),
                                       ("n", "abort")], "y", allow_all=False) == "y"
