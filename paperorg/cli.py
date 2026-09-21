"""Command line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .extract import have
from .pipeline import Organizer, print_summary
from .prompts import DIM, RESET, YELLOW


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper-organizer",
        description="Turn a pile of mathematical PDFs into a tidy, "
                    "BibTeX-indexed repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  organize.py input/ library/
  organize.py input/ library/ --dry-run --limit 50
  organize.py input/ library/ --yes --unknown ignore      # unattended
  organize.py input/ library/ --offline                   # no network lookups
  organize.py input/ library/ --only "van der Kallen"     # a subset
""")
    p.add_argument("input", type=Path, help="folder with the unsorted documents")
    p.add_argument("output", type=Path, help="folder for the new repository")

    g = p.add_argument_group("selection")
    g.add_argument("--limit", type=int, help="process the first N files")
    g.add_argument("--sample", type=int, metavar="N",
                   help="process a random sample of N files (good for a trial run)")
    g.add_argument("--seed", type=int, default=0, help="seed for --sample")
    g.add_argument("--only", action="append", default=[],
                   help="only paths containing this substring (repeatable)")
    g.add_argument("--no-djvu", action="store_true", help="skip .djvu files")

    g = p.add_argument_group("behaviour")
    g.add_argument("-n", "--dry-run", action="store_true",
                   help="analyse and report, but write nothing")
    g.add_argument("-y", "--yes", action="store_true",
                   help="never prompt; use the defaults for every decision")
    g.add_argument("--unknown", choices=("unsorted", "ignore"), default="unsorted",
                   help="what to do with non-papers when not prompting "
                        "(default: unsorted)")
    g.add_argument("--links", choices=("symlink", "copy", "none"), default="symlink",
                   help="how co-authors get their copy (default: symlink)")
    g.add_argument("--offline", action="store_true",
                   help="no network; metadata is taken from the files alone")
    g.add_argument("--no-ocr", action="store_true", help="never run OCR on scans")
    g.add_argument("--ocr-pages", type=int, default=2, metavar="N",
                   help="how many leading pages to OCR (default: 2; 1 is twice "
                        "as fast and usually enough for the title)")
    g.add_argument("--ocr-dpi", type=int, default=300, metavar="D",
                   help="render resolution for OCR (default: 300)")
    g.add_argument("--no-zbmath", action="store_true",
                   help="skip the zbMATH enrichment (Zbl number, MSC codes)")

    g = p.add_argument_group("tuning")
    g.add_argument("-j", "--jobs", type=int, default=8,
                   help="parallel file readers (default: 8)")
    g.add_argument("--accept", type=float, default=0.86, metavar="S",
                   help="auto-accept a match at this score (default: 0.86)")
    g.add_argument("--review", type=float, default=0.70, metavar="S",
                   help="ask about matches down to this score (default: 0.70)")
    g.add_argument("--mailto", default="paper-organizer@example.org",
                   help="contact address sent to Crossref's polite pool")
    g.add_argument("--no-cache", action="store_true", help="ignore the cache")
    g.add_argument("--cache", type=Path, help="cache file location")
    g.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"paper-organizer {__version__}")
    return p


def check_tools(cfg: Config) -> None:
    """Warn about missing helpers instead of failing deep inside a run."""
    required = {"pdftotext": "reading PDF text (poppler-utils)",
                "pdfinfo": "reading PDF page counts (poppler-utils)"}
    optional = {"tesseract": "OCR for scanned papers",
                "pdftoppm": "rendering pages for OCR (poppler-utils)",
                "djvutxt": "reading DjVu text (djvulibre-bin)",
                "ddjvu": "rendering DjVu for OCR (djvulibre-bin)"}
    missing = [f"{t} - {why}" for t, why in required.items() if not have(t)]
    if missing:
        print(f"{YELLOW}missing required tools:{RESET}", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        print("  on Debian/Ubuntu: sudo apt install poppler-utils", file=sys.stderr)
        sys.exit(2)
    absent = [f"{t} ({why})" for t, why in optional.items() if not have(t)]
    if absent and cfg.verbose:
        print(f"  {DIM}optional tools not found: {', '.join(absent)}{RESET}")
    if cfg.djvu and not have("djvutxt"):
        print(f"  {DIM}note: djvulibre-bin is not installed, so DjVu files are "
              f"filed from their name alone{RESET}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.exists():
        print(f"input folder does not exist: {args.input}", file=sys.stderr)
        return 2

    cfg = Config(
        input_dir=args.input,
        output_dir=args.output,
        jobs=max(1, args.jobs),
        interactive=not args.yes,
        dry_run=args.dry_run,
        ocr=not args.no_ocr,
        ocr_max_pages=max(1, args.ocr_pages),
        ocr_dpi=args.ocr_dpi,
        djvu=not args.no_djvu,
        online=not args.offline,
        zbmath=not args.no_zbmath,
        accept_score=args.accept,
        review_score=args.review,
        mailto=args.mailto,
        default_unknown_action=args.unknown,
        link_mode=args.links,
        limit=args.limit,
        sample=args.sample,
        seed=args.seed,
        only=args.only,
        verbose=args.verbose,
    )
    if args.cache:
        cfg.cache_path = Path(args.cache)
    if args.no_cache:
        cfg.cache_path = None
    if cfg.output_dir == cfg.input_dir or cfg.output_dir in cfg.input_dir.parents:
        print("output folder must not contain the input folder", file=sys.stderr)
        return 2

    check_tools(cfg)
    org = Organizer(cfg)
    try:
        stats = org.run()
    except KeyboardInterrupt:
        print("\ninterrupted - progress is cached, re-run to continue.", file=sys.stderr)
        return 130
    print_summary(cfg, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
