"""End-to-end: build a real repository on disk from synthetic PDFs.

Runs entirely offline, so the assertions are about layout, symlinks,
deduplication and the BibTeX file rather than about registry content.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from paperorg.config import Config
from paperorg.pipeline import Organizer

pytestmark = pytest.mark.skipif(
    not (shutil.which("gs") and shutil.which("pdftotext")),
    reason="needs ghostscript and poppler to build sample PDFs",
)


def make_pdf(path: Path, title: str, authors: str, body: str = "", pages: int = 1) -> Path:
    """Render a minimal but genuine PDF with a title page."""
    def esc(s):
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    ps = ["%!PS"]
    for pg in range(pages):
        if pg == 0:
            ps += ["/Helvetica findfont 18 scalefont setfont",
                   f"72 700 moveto ({esc(title)}) show",
                   "/Helvetica findfont 11 scalefont setfont",
                   f"72 670 moveto ({esc(authors)}) show"]
            filler = ("Abstract. We prove a stability theorem and derive "
                      "several corollaries for the functor under study.")
            for i, line in enumerate((body or filler).split("\n")):
                ps.append(f"72 {640 - 14 * i} moveto ({esc(line)}) show")
        else:
            ps += ["/Helvetica findfont 11 scalefont setfont",
                   f"72 700 moveto (page {pg + 1}) show"]
        ps.append("showpage")
    path.parent.mkdir(parents=True, exist_ok=True)
    src = path.with_suffix(".ps")
    src.write_text("\n".join(ps))
    subprocess.run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
                    f"-sOutputFile={path}", str(src)], check=True,
                   capture_output=True)
    src.unlink()
    return path


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    inp, out = root / "input", root / "repo"

    make_pdf(inp / "W. van der Kallen, M. Stein - Injective stability for K2.pdf",
             "Injective stability for K2",
             "Wilberd van der Kallen and Michael R. Stein", pages=8)
    # byte-identical copy, buried in a worse path
    (inp / "unsorted").mkdir(parents=True, exist_ok=True)
    shutil.copy2(inp / "W. van der Kallen, M. Stein - Injective stability for K2.pdf",
                 inp / "unsorted" / "scan001.pdf")
    # same entry, different bytes (a second scan with an extra page)
    make_pdf(inp / "unsorted" / "vdk-stein-k2-v2.pdf",
             "Injective stability for K2",
             "Wilberd van der Kallen and Michael R. Stein", pages=11)
    # a single-author paper
    make_pdf(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
             "Projective modules over polynomial rings", "Andrei Suslin", pages=6)
    # a thesis
    make_pdf(inp / "theses" / "H. Reich - Group von Neumann algebras.pdf",
             "Group von Neumann algebras",
             "Holger Reich",
             "A thesis submitted in partial fulfilment of the requirements\n"
             "for the degree of Doctor of Philosophy", pages=45)
    # a PDF whose name lost its extension
    make_pdf(inp / "tmp.pdf", "Framed motives of relative motivic spheres",
             "Grigory Garkusha and Ivan Panin", pages=5)
    os.rename(inp / "tmp.pdf",
              inp / "G. Garkusha, I. Panin - Framed motives of relative motivic spherespdf")
    # something that is not scholarly output at all
    make_pdf(inp / "SEPA_Lastschriftmandat.pdf", "SEPA Lastschriftmandat",
             "Bank", "Gläubiger-Identifikationsnummer")

    cfg = Config(input_dir=inp, output_dir=out, interactive=False, online=False,
                 ocr=False, jobs=2, default_unknown_action="unsorted")
    stats = Organizer(cfg).run()
    return cfg, stats


def test_finds_the_pdf_with_a_broken_extension(built):
    _, stats = built
    assert stats["found"] == 7          # 6 named .pdf + 1 with a mangled name


def test_byte_identical_copy_is_dropped(built):
    _, stats = built
    assert stats["byte_duplicates"] == 1


def test_same_entry_different_bytes_is_resolved_not_placed_twice(built):
    cfg, stats = built
    assert stats["entry_duplicates"] == 1
    hits = list(cfg.output_dir.rglob("Injective stability for K2*.pdf"))
    reals = [p for p in hits if not p.is_symlink()]
    assert len(reals) == 1


def test_first_author_holds_the_file(built):
    cfg, _ = built
    target = cfg.output_dir / "van der Kallen, Wilberd" / "Injective stability for K2.pdf"
    assert target.is_file() and not target.is_symlink()


def test_coauthor_gets_a_relative_symlink_that_resolves(built):
    cfg, _ = built
    link = cfg.output_dir / "Stein, Michael R" / "Injective stability for K2.pdf"
    assert link.is_symlink(), "co-author must get a symlink"
    assert not os.path.isabs(os.readlink(link)), "symlink must be relative"
    assert link.resolve().is_file()
    assert link.resolve().parent.name == "van der Kallen, Wilberd"


def test_repository_stays_movable(built, tmp_path):
    """A relative symlink must survive the whole tree being moved."""
    cfg, _ = built
    moved = tmp_path / "moved-repo"
    shutil.copytree(cfg.output_dir, moved, symlinks=True)
    link = moved / "Stein, Michael R" / "Injective stability for K2.pdf"
    assert link.resolve().is_file()
    assert str(moved) in str(link.resolve())


def test_paper_filename_is_exactly_the_title(built):
    cfg, _ = built
    assert (cfg.output_dir / "Suslin, Andrei" /
            "Projective modules over polynomial rings.pdf").is_file()


def test_thesis_goes_to_its_own_folder_with_authors_in_the_name(built):
    cfg, _ = built
    found = list((cfg.output_dir / "Thesis").glob("*.pdf"))
    assert len(found) == 1
    assert "Group von Neumann algebras" in found[0].name
    assert "Reich" in found[0].name


def test_non_scholarly_file_goes_to_unsorted(built):
    cfg, _ = built
    assert list((cfg.output_dir / "Unsorted").glob("*.pdf"))


def test_bib_file_is_at_the_repository_root(built):
    cfg, _ = built
    assert cfg.bib_path.is_file()
    assert cfg.bib_path.parent == cfg.output_dir


def test_every_bib_file_field_points_at_a_real_file(built):
    cfg, _ = built
    text = cfg.bib_path.read_text(encoding="utf-8")
    paths = re.findall(r"file\s+=\s+\{:(.+?):PDF\}", text)
    assert paths, "entries must carry a file field"
    for rel in paths:
        rel = rel.replace(r"\:", ":")
        assert (cfg.output_dir / rel).exists(), f"dangling file field: {rel}"


def test_bib_entry_count_matches_placed_documents(built):
    cfg, stats = built
    text = cfg.bib_path.read_text(encoding="utf-8")
    assert len(re.findall(r"^@\w+\{", text, re.M)) == stats["placed"]


def test_bib_cite_keys_are_unique(built):
    cfg, _ = built
    keys = re.findall(r"^@\w+\{([^,]+),", cfg.bib_path.read_text(encoding="utf-8"), re.M)
    assert len(keys) == len(set(keys))


def test_report_lists_every_input_file(built):
    import csv
    cfg, stats = built
    rows = list(csv.DictReader(open(cfg.state_dir / "report.csv", encoding="utf-8")))
    assert len(rows) == stats["found"]
    assert {"status", "kind", "title", "placed_at", "input_path"} <= set(rows[0])


def test_rerun_is_idempotent_enough_to_not_duplicate_entries(built, tmp_path):
    """Running again into a fresh output folder must give the same counts."""
    cfg, stats = built
    again = Config(input_dir=cfg.input_dir, output_dir=tmp_path / "repo2",
                   interactive=False, online=False, ocr=False, jobs=2)
    stats2 = Organizer(again).run()
    assert stats2["placed"] == stats["placed"]
    assert stats2["byte_duplicates"] == stats["byte_duplicates"]


def test_resuming_into_an_existing_repository_does_not_duplicate_files(built, tmp_path):
    """An interrupted run leaves files in place; running again must recognise
    them instead of writing 'Title (2).pdf' beside each one."""
    cfg, stats = built
    again = Config(input_dir=cfg.input_dir, output_dir=tmp_path / "resumed",
                   interactive=False, online=False, ocr=False, jobs=2)
    first = Organizer(again).run()
    second = Organizer(again).run()
    assert second["placed"] == first["placed"]
    assert second["reused"] == second["placed"], "every file should be recognised"
    assert not list(again.output_dir.rglob("* (2).pdf"))


def test_byte_and_entry_duplicates_are_counted_separately(built):
    """A byte-identical copy and a different scan of the same paper are two
    different situations and must not be reported as one number."""
    _, stats = built
    assert stats["byte_duplicates"] == 1        # the identical copy
    assert stats["byte_duplicates_seen"] == 1
    assert stats["entry_duplicates"] == 1       # the second scan


def test_cached_features_round_trip_with_their_text(tmp_path):
    """A cached analysis must carry its text, or a second run classifies the
    same document differently from the first."""
    from paperorg.extract import analyse
    from paperorg.record import DocFeatures

    inp = tmp_path / "in"
    f = make_pdf(inp / "H. Reich - Group von Neumann algebras.pdf",
                 "Group von Neumann algebras", "Holger Reich",
                 "A thesis submitted in partial fulfilment of the requirements\n"
                 "for the degree of Doctor of Philosophy", pages=45)
    cfg = Config(input_dir=inp, output_dir=tmp_path / "out", ocr=False)
    fresh = analyse(f, cfg)
    assert fresh.text_chars > 0

    restored = DocFeatures(**{**fresh.to_json(), "path": f})
    assert restored.text, "the cached record must keep its text"
    from paperorg.classify import classify
    assert classify(restored)[0] is classify(fresh)[0]
