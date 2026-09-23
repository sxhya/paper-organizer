"""Runtime configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Folders inside the new repository that are not author folders.
BOOKS_DIR = "Books"
THESIS_DIR = "Thesis"
UNSORTED_DIR = "Unsorted"
BIB_NAME = "library.bib"

PDF_EXTS = {".pdf"}
DJVU_EXTS = {".djvu", ".djv"}
PS_EXTS = {".ps"}
OTHER_DOC_EXTS = {".dvi"}


@dataclass
class Config:
    input_dir: Path
    output_dir: Path
    jobs: int = 8
    interactive: bool = True
    dry_run: bool = False
    ocr: bool = True
    ocr_max_pages: int = 2
    ocr_dpi: int = 300
    ocr_timeout: int = 120
    djvu: bool = True
    ps: bool = True                     # convert PostScript with ps2pdf
    ps_timeout: int = 180
    online: bool = True
    zbmath: bool = True
    mathnet: bool = True
    # Matching thresholds.
    accept_score: float = 0.86          # auto-accept a metadata match
    review_score: float = 0.70          # below accept: ask the user (if interactive)
    # Politeness / networking.
    mailto: str = "paper-organizer@example.org"
    crossref_workers: int = 6
    arxiv_delay: float = 3.0            # arXiv asks for >=3s between queries
    http_timeout: int = 30
    retries: int = 3
    # Behaviour when running without a human.
    default_unknown_action: str = "unsorted"   # unsorted | ignore
    on_rate_limit: str = "stop"                # stop (and resume later) | skip
    link_mode: str = "symlink"                  # symlink | copy | none
    cache_path: Path | None = None
    limit: int | None = None
    sample: int | None = None
    seed: int = 0
    only: list[str] = field(default_factory=list)
    verbose: bool = False

    def __post_init__(self) -> None:
        self.input_dir = Path(self.input_dir).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if self.cache_path is None:
            self.cache_path = self.output_dir / ".paperorg" / "cache.sqlite3"
        else:
            self.cache_path = Path(self.cache_path).expanduser().resolve()

    @property
    def bib_path(self) -> Path:
        return self.output_dir / BIB_NAME

    @property
    def state_dir(self) -> Path:
        return self.output_dir / ".paperorg"

    @property
    def user_agent(self) -> str:
        from . import __version__
        return f"paper-organizer/{__version__} (mailto:{self.mailto})"
