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


def make_ps(path: Path, title: str, authors: str, body: str = "", pages: int = 1) -> Path:
    """Write a minimal PostScript document with a title page."""
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
    path.write_text("\n".join(ps))
    return path


def make_pdf(path: Path, title: str, authors: str, body: str = "", pages: int = 1) -> Path:
    """Render a minimal but genuine PDF with a title page."""
    src = make_ps(path.with_suffix(".ps"), title, authors, body, pages)
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


@pytest.mark.skipif(not shutil.which("ps2pdf"), reason="needs ps2pdf")
def test_postscript_is_converted_and_the_pdf_is_placed(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "repo"
    make_ps(inp / "A. Suslin - Projective modules over polynomial rings.ps",
            "Projective modules over polynomial rings", "Andrei Suslin", pages=6)
    cfg = Config(input_dir=inp, output_dir=out, interactive=False, online=False,
                 ocr=False, jobs=2)
    stats = Organizer(cfg).run()
    assert stats["placed"] == 1
    placed = out / "Suslin, Andrei" / "Projective modules over polynomial rings.pdf"
    assert placed.is_file()
    assert placed.read_bytes().startswith(b"%PDF")
    assert not list(out.rglob("*.ps"))
    assert str(placed.relative_to(out)) in cfg.bib_path.read_text(encoding="utf-8")

    # A resumed run recognises the converted PDF already in place.
    again = Organizer(cfg).run()
    assert again["reused"] == again["placed"] == 1
    assert not list(out.rglob("* (2).pdf"))


def test_postscript_is_skipped_when_disabled(tmp_path):
    inp = tmp_path / "in"
    make_ps(inp / "A. Suslin - Projective modules.ps", "Projective modules",
            "Andrei Suslin")
    cfg = Config(input_dir=inp, output_dir=tmp_path / "repo", interactive=False,
                 online=False, ocr=False, ps=False)
    org = Organizer(cfg)
    assert org.scan() == [] and org.skipped_files == 1


def test_text_report_lists_what_was_not_filed_normally(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "repo"
    make_pdf(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
             "Projective modules over polynomial rings", "Andrei Suslin", pages=6)
    shutil.copy2(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
                 inp / "copy.pdf")
    make_pdf(inp / "SEPA_Lastschriftmandat.pdf", "SEPA Lastschriftmandat",
             "Bank", "Gläubiger-Identifikationsnummer")
    (inp / "notes").mkdir()
    (inp / "notes" / "draft.tex").write_text("\\documentclass{article}")
    (inp / "broken.pdf").write_bytes(b"%PDF-1.4\ngarbage")
    cfg = Config(input_dir=inp, output_dir=out, interactive=False, online=False,
                 ocr=False, jobs=2)
    Organizer(cfg).run()

    report = (cfg.state_dir / "report.txt").read_text(encoding="utf-8")
    sections = dict(re.findall(r"== (.+?) \([^)]*\) ==\n(.*?)(?=\n\n\n==|\Z)",
                               report, re.S))
    assert "notes/draft.tex" in sections["Not a document"]
    assert "SEPA_Lastschriftmandat.pdf" in sections["Not recognised"]
    assert "-> Unsorted/" in sections["Not recognised"]
    assert "copy.pdf" in sections["Duplicates"]
    assert "broken.pdf" in sections["Not recognised"]
    # Offline, nothing can come from a registry.
    assert "-> Suslin, Andrei/" in sections["No registry match"]


def test_text_report_is_written_when_nothing_is_a_document(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "slides.ppt").write_bytes(b"not a pdf")
    cfg = Config(input_dir=inp, output_dir=tmp_path / "repo", interactive=False,
                 online=False, ocr=False)
    assert Organizer(cfg).run() == {"found": 0}
    assert "slides.ppt" in (cfg.state_dir / "report.txt").read_text(encoding="utf-8")


def test_macos_resource_forks_are_not_documents(tmp_path):
    inp = tmp_path / "in"
    make_pdf(inp / "A. Suslin - Projective modules.pdf", "Projective modules",
             "Andrei Suslin")
    (inp / "._A. Suslin - Projective modules.pdf").write_bytes(b"\x00\x05\x16\x07" + b"\0" * 60)
    cfg = Config(input_dir=inp, output_dir=tmp_path / "repo", interactive=False,
                 online=False, ocr=False)
    org = Organizer(cfg)
    assert [p.name for p in org.scan()] == ["A. Suslin - Projective modules.pdf"]
    assert org.skipped_files == 1


@pytest.mark.skipif(not shutil.which("ps2pdf"), reason="needs ps2pdf")
def test_gzipped_postscript_and_pdf_are_unpacked_and_placed(tmp_path):
    import gzip
    inp, out = tmp_path / "in", tmp_path / "repo"
    ps = make_ps(tmp_path / "src.ps", "Projective modules over polynomial rings",
                 "Andrei Suslin", pages=3)
    (inp / "sub").mkdir(parents=True)
    (inp / "A. Suslin - Projective modules over polynomial rings.ps.gz") \
        .write_bytes(gzip.compress(ps.read_bytes()))
    pdf = make_pdf(tmp_path / "src.pdf", "Stability in algebraic K-theory",
                   "Leonid Vaserstein", pages=3)
    # no ".pdf" before ".gz": found by looking inside
    (inp / "sub" / "L. Vaserstein - Stability in algebraic K-theory.gz") \
        .write_bytes(gzip.compress(pdf.read_bytes()))
    (inp / "notes.tex.gz").write_bytes(gzip.compress(b"\\documentclass{article}"))

    cfg = Config(input_dir=inp, output_dir=out, interactive=False, online=False,
                 ocr=False, jobs=2)
    stats = Organizer(cfg).run()
    assert stats["found"] == 2 and stats["placed"] == 2
    for p in (out / "Suslin, Andrei" / "Projective modules over polynomial rings.pdf",
              out / "Vaserstein, Leonid" / "Stability in algebraic K-theory.pdf"):
        assert p.read_bytes().startswith(b"%PDF"), p
    assert not list(out.rglob("*.gz"))
    assert "notes.tex.gz" in (cfg.state_dir / "report.txt").read_text(encoding="utf-8")


def test_a_rate_limited_run_stops_and_resumes(tmp_path, monkeypatch):
    """When a registry starts refusing, stop before any prompt or copy, say
    how to continue, and let the same command pick the run up again."""
    inp, out = tmp_path / "in", tmp_path / "repo"
    make_pdf(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
             "Projective modules over polynomial rings", "Andrei Suslin")
    make_pdf(inp / "L. Vaserstein - Stability in algebraic K-theory.pdf",
             "Stability in algebraic K-theory", "Leonid Vaserstein")
    from paperorg import sources
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = Config(input_dir=inp, output_dir=out, interactive=False, ocr=False,
                 jobs=1, crossref_workers=1, retries=3, arxiv_titles="auto")

    org = Organizer(cfg)
    org.http.fetch = lambda url, accept, form=None: (429, "")
    stats = org.run()
    assert "api.crossref.org" in stats["interrupted"]
    resume = (cfg.state_dir / "RESUME.txt").read_text(encoding="utf-8")
    assert "api.crossref.org" in resume and "--on-rate-limit skip" in resume
    assert not cfg.bib_path.exists() and not list(out.rglob("*.pdf"))

    again = Organizer(cfg)                  # the block has lifted
    again.http.fetch = lambda url, accept, form=None: (404, "")
    assert again.run()["placed"] == 2       # no "output exists" question either
    assert not (cfg.state_dir / "RESUME.txt").exists()


def test_skip_mode_finishes_without_the_refusing_registry(tmp_path, monkeypatch):
    inp, out = tmp_path / "in", tmp_path / "repo"
    make_pdf(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
             "Projective modules over polynomial rings", "Andrei Suslin")
    from paperorg import sources
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = Config(input_dir=inp, output_dir=out, interactive=False, ocr=False,
                 retries=3, on_rate_limit="skip", arxiv_titles="auto")
    org = Organizer(cfg)
    org.http.fetch = lambda url, accept, form=None: (429, "")
    assert org.run()["placed"] == 1
    assert "lookup incomplete: api.crossref.org" in \
        (cfg.state_dir / "report.txt").read_text(encoding="utf-8")


def test_arxiv_title_searches_wait_for_review_then_go_out(tmp_path, monkeypatch):
    """The first run lists the arXiv searches and stops; once a human marks
    them, the same command sends exactly those and finishes."""
    import csv
    inp, out = tmp_path / "in", tmp_path / "repo"
    make_pdf(inp / "M. Rost - Norm varieties and algebraic cobordism.pdf",
             "Norm varieties and algebraic cobordism", "Markus Rost")
    make_pdf(inp / "A. Suslin - Projective modules over polynomial rings.pdf",
             "Projective modules over polynomial rings", "Andrei Suslin")
    from paperorg import sources
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = Config(input_dir=inp, output_dir=out, interactive=False, ocr=False,
                 jobs=1, crossref_workers=1, retries=1, arxiv_delay=0)
    asked = []

    def fetch(url, accept, form=None):
        if "export.arxiv.org" in url:
            asked.append(url)
        return 404, ""

    org = Organizer(cfg)
    org.http.fetch = fetch
    stats = org.run()
    assert stats["interrupted"] == "arXiv review"
    assert asked == []
    assert "arxiv-queries.tsv" in (cfg.state_dir / "RESUME.txt").read_text(encoding="utf-8")
    assert not list(out.rglob("*.pdf"))

    path = cfg.arxiv_plan_path
    body = [l for l in path.read_text(encoding="utf-8").splitlines(True) if not l.startswith("#")]
    rows = list(csv.DictReader(body, dialect="excel-tab"))
    assert {r["send"] for r in rows} == {"y?"}
    assert any(r["query"].startswith("au:Rost AND ti:") for r in rows)
    for r in rows:
        r["send"] = "y" if "Rost" in r["file"] else "n"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), dialect="excel-tab")
        w.writeheader()
        w.writerows(rows)

    again = Organizer(cfg)
    again.http.fetch = fetch
    assert again.run()["placed"] == 2
    assert len(asked) == 1 and "au%3ARost" in asked[0]
    assert not (cfg.state_dir / "RESUME.txt").exists()
