"""Unit tests for paper-organizer. Run: python3 -m pytest tests/ -q"""
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from paperorg import bibtex
from paperorg.classify import classify
from paperorg.config import Config
from paperorg.dedupe import Item, group_byte_duplicates, group_bib_duplicates, rank_versions
from paperorg.extract import clean_doi, find_identifiers, parse_filename, extract_title_authors
from paperorg.match import parse_author_list, author_agreement, score_candidate
from paperorg.record import Author, BibRecord, DocFeatures, DocKind
from paperorg.repo import plan_placement, target_name, author_folder
from paperorg.sources import _split_name
from paperorg.util import crc32_file, safe_filename, similarity, unique_path


# ------------------------------------------------------------------ filenames

@pytest.mark.parametrize("stem,authors,title", [
    ("A. Bak, N. Vavilov - Presenting powers", "A. Bak, N. Vavilov", "Presenting powers"),
    ("W. van der Kallen - Injective stability", "W. van der Kallen", "Injective stability"),
    ("J.-P. Serre - Trees", "J.-P. Serre", "Trees"),
    ("A. Rapinchuk et al - Bounded generation", "A. Rapinchuk et al", "Bounded generation"),
    ("Н. Вавилов, А. Степанов - Линейные группы", "Н. Вавилов, А. Степанов", "Линейные группы"),
    ("T. Bachmann, Calmes, Fasel - Milnor-Witt motives",
     "T. Bachmann, Calmes, Fasel", "Milnor-Witt motives"),
])
def test_parse_filename_author_title(stem, authors, title):
    assert parse_filename(stem) == (authors, title)


@pytest.mark.parametrize("stem", [
    "deodhar82", "chernousovspin", "arithmetic-undead",
    "An essay on Quillen's work", "Leben_in_Deutschland",
    "Introduction to Python - a tutorial",
])
def test_parse_filename_rejects_non_author_prefix(stem):
    authors, title = parse_filename(stem)
    assert authors == "", f"{stem!r} should not yield an author list, got {authors!r}"
    assert title


def test_parse_filename_strips_decorations():
    assert parse_filename("A. Suslin - K-theory [preprint] (2)")[1] == "K-theory"


# ---------------------------------------------------------------- identifiers

@pytest.mark.parametrize("raw,expected", [
    ("10.1016/j.jalgebra.2014.04.008", "10.1016/j.jalgebra.2014.04.008"),
    ("10.2307/2373586.", "10.2307/2373586"),
    ("10.1007/BF01390008http://x", "10.1007/BF01390008"),
    ("10.1090/memo/0924)", "10.1090/memo/0924"),
    ("10.1016/j", ""),                      # truncated by the text extractor
    ("nonsense", ""),
])
def test_clean_doi(raw, expected):
    assert clean_doi(raw) == expected


def test_find_identifiers():
    text = ("arXiv:1601.06642v3 [math.AG] 25 Jan 2016\n"
            "doi:10.1007/s00222-016-0645-9\nMR 1234567\nISBN: 978-1-84800-987-5")
    arxiv, doi, mr, isbn = find_identifiers(text)
    assert arxiv == "1601.06642"
    assert doi == "10.1007/s00222-016-0645-9"
    assert mr == "1234567"
    assert isbn == "9781848009875"


def test_find_identifiers_old_style_arxiv():
    assert find_identifiers("arXiv:math/0110163v2")[0] == "math/0110163"


# ------------------------------------------------------------------- matching

def test_similarity_tolerates_case_and_math():
    assert similarity("Injective stability for K2", "Injective Stability for $K_2$") > 0.9
    assert similarity("Trees", "Sheaves on manifolds") < 0.5


def test_similarity_subtitle_dropped():
    assert similarity("Algebraic K-theory",
                      "Algebraic K-theory: an introduction") > 0.85


@pytest.mark.parametrize("blob,expected", [
    ("A. Bak, N. Vavilov", ["Bak, A.", "Vavilov, N."]),
    ("W. van der Kallen and M. Stein", ["van der Kallen, W.", "Stein, M."]),
    ("A. Rapinchuk et al", ["Rapinchuk, A."]),
    ("Serre, Jean-Pierre", ["Serre, Jean-Pierre"]),
])
def test_parse_author_list(blob, expected):
    assert [a.bibtex for a in parse_author_list(blob)] == expected


def test_split_name_particles():
    a = _split_name("Wilberd van der Kallen")
    assert (a.family, a.given) == ("van der Kallen", "Wilberd")


def test_author_agreement():
    rec = BibRecord(authors=[Author("van der Kallen", "W."), Author("Stein", "M.")])
    assert author_agreement(rec, "W. van der Kallen, M. Stein") == 1.0
    assert author_agreement(rec, "A. Suslin") == 0.0


def test_score_rewards_author_match_and_punishes_mismatch():
    rec = BibRecord(title="Injective stability for K2",
                    authors=[Author("van der Kallen", "W.")], year="1977")
    good = score_candidate(rec, "Injective Stability for K_2", "W. van der Kallen", "1977")
    bad = score_candidate(rec, "Injective Stability for K_2", "A. Suslin", "1999")
    assert good.score > bad.score
    assert good.score > 0.9


def test_score_penalises_two_word_titles():
    rec = BibRecord(title="Trees", authors=[Author("Serre", "J.-P.")])
    assert score_candidate(rec, "Trees", "").score < 1.0


# ----------------------------------------------------------------- classifier

def _feat(**kw):
    base = dict(path=Path("/tmp/x.pdf"), pages=20, width=595, height=842,
                text="A theorem about groups. " * 80, text_chars=2000)
    base.update(kw)
    f = DocFeatures(**{k: v for k, v in base.items() if k != "text_chars"})
    f.text_chars = len(f.text.strip())
    return f


def test_classify_slides_by_geometry():
    kind, conf, _ = classify(_feat(width=720, height=540, pages=30))
    assert kind is DocKind.OTHER and conf > 0.8


def test_classify_thesis_by_wording():
    kind, _, _ = classify(_feat(pages=120, text="A thesis submitted in partial "
                                                "fulfilment of the requirements"))
    assert kind is DocKind.THESIS


def test_classify_book_by_length_and_isbn():
    assert classify(_feat(pages=300))[0] is DocKind.BOOK
    assert classify(_feat(pages=80, isbn="9781848009875"))[0] is DocKind.BOOK


def test_classify_paper_default():
    assert classify(_feat(pages=18))[0] is DocKind.PAPER


def test_classify_paper_wins_with_identifier():
    kind, conf, _ = classify(_feat(pages=200, arxiv_id="1601.06642"))
    # length still says book; an explicit arXiv id is not enough to override it
    assert kind is DocKind.BOOK


def test_classify_admin_document():
    assert classify(_feat(pages=2, text="SEPA Lastschriftmandat"))[0] is DocKind.OTHER


# --------------------------------------------------------------------- bibtex

def test_escape_specials_and_accents():
    assert bibtex.escape("Gauss & Möbius 50%") == r"Gauss \& M\"obius 50\%"


def test_escape_keeps_math():
    assert bibtex.escape("Stability of $K_2$ & friends") == r"Stability of $K_2$ \& friends"


@pytest.mark.parametrize("title,expected", [
    ("Injective stability for K2", "Injective stability for {K2}"),
    ("Group von Neumann algebras", "Group von {Neumann} algebras"),
    ("Property T for Kac-Moody groups", "Property {T} for {Kac-Moody} groups"),
    ("The Finite Simple Groups", "The Finite Simple Groups"),      # title case: leave alone
])
def test_protect_caps(title, expected):
    assert bibtex.protect_caps(title) == expected


def test_cite_key_is_unique():
    taken = set()
    r = BibRecord(title="Injective stability", authors=[Author("Kallen", "W.")], year="1977")
    k1 = bibtex.cite_key(r, taken)
    k2 = bibtex.cite_key(r, taken)
    assert k1 != k2 and k2.startswith(k1)


def test_entry_has_file_field_and_type():
    r = BibRecord(kind=DocKind.PAPER, title="T", authors=[Author("A", "B")],
                  year="2000", journal="J", doi="10.1/x")
    out = bibtex.format_entry(r, "key", "A, B/T.pdf")
    assert out.startswith("@article{key,")
    assert re.search(r"file\s+= \{:A, B/T\.pdf:PDF\}", out)
    assert "doi" in out


def test_preprint_becomes_misc_with_eprint():
    r = BibRecord(kind=DocKind.PAPER, title="T", authors=[Author("A", "B")],
                  year="2011", arxiv="1111.4664", eprint_class="math.KT")
    out = bibtex.format_entry(r, "k", None)
    assert out.startswith("@misc{")
    assert "eprint" in out and "archiveprefix" in out


def test_thesis_and_book_entry_types():
    t = BibRecord(kind=DocKind.THESIS, title="T", authors=[Author("A", "B")], school="U")
    b = BibRecord(kind=DocKind.BOOK, title="T", authors=[Author("A", "B")], publisher="P")
    assert bibtex.format_entry(t, "k", None).startswith("@phdthesis{")
    assert bibtex.format_entry(b, "k", None).startswith("@book{")


def test_jabref_file_field_escapes_colons():
    r = BibRecord(kind=DocKind.PAPER, title="T", authors=[Author("A", "B")])
    out = bibtex.format_entry(r, "k", "Author/Title: a study.pdf")
    assert r"Title\: a study.pdf" in out


# ----------------------------------------------------------------- repository

def test_placement_first_author_gets_file_others_links():
    rec = BibRecord(kind=DocKind.PAPER, title="On Schur multipliers",
                    authors=[Author("van der Kallen", "W."), Author("Stein", "M.")])
    pl = plan_placement(rec, ".pdf")
    assert pl.primary == Path("van der Kallen, W/On Schur multipliers.pdf")
    assert pl.links == [Path("Stein, M/On Schur multipliers.pdf")]


def test_placement_paper_name_is_just_the_title():
    rec = BibRecord(kind=DocKind.PAPER, title="Trees", year="1980",
                    authors=[Author("Serre", "J.-P.")])
    assert plan_placement(rec, ".pdf").primary.name == "Trees.pdf"


def test_placement_book_name_carries_authors():
    rec = BibRecord(kind=DocKind.BOOK, title="Symmetric Bilinear Forms", year="1973",
                    authors=[Author("Milnor", "John"), Author("Husemoller", "Dale")])
    pl = plan_placement(rec, ".djvu")
    assert pl.primary.parent.name == "Books"
    assert "J. Milnor, D. Husemoller" in pl.primary.name
    assert pl.links == []


def test_placement_thesis_folder():
    rec = BibRecord(kind=DocKind.THESIS, title="Topics in K-theory",
                    authors=[Author("Basu", "Rabeya")], year="2006")
    assert plan_placement(rec, ".pdf").primary.parent.name == "Thesis"


def test_placement_other_goes_to_unsorted():
    rec = BibRecord(kind=DocKind.OTHER, title="ICTP Poster")
    assert plan_placement(rec, ".pdf").primary.parent.name == "Unsorted"


def test_duplicate_coauthor_folder_is_not_linked_twice():
    rec = BibRecord(kind=DocKind.PAPER, title="T",
                    authors=[Author("Bak", "A."), Author("Bak", "A.")])
    assert plan_placement(rec, ".pdf").links == []


def test_safe_filename():
    assert safe_filename('A/B: "C"?') == "A-B - C"
    assert safe_filename("Algebraic K-theory: an introduction") == \
        "Algebraic K-theory - an introduction"
    assert len(safe_filename("x" * 400).encode()) <= 180
    assert safe_filename("") == "untitled"


def test_safe_filename_keeps_unicode():
    assert "Васерштейн" in safe_filename("Л. Васерштейн - Стабильный ранг")


# ------------------------------------------------------------------ dedupe

def _item(tmp_path, name, content, sub=""):
    d = tmp_path / sub if sub else tmp_path
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(content)
    f = DocFeatures(path=p, crc32=crc32_file(p), size=len(content))
    f.file_authors, f.file_title = parse_filename(p.stem)
    f.dir_hints = [x.lower() for x in p.parent.parts[-4:]]
    return Item(feat=f)


def test_byte_duplicates_collapse_and_prefer_named_copy(tmp_path):
    a = _item(tmp_path, "A. Suslin - On K-theory.pdf", b"same")
    b = _item(tmp_path, "scan001.pdf", b"same", sub="unsorted")
    kept, dropped = group_byte_duplicates([a, b])
    assert dropped == 1 and len(kept) == 1
    assert kept[0].path.name == "A. Suslin - On K-theory.pdf"
    assert b.status == "byte-duplicate"


def test_distinct_bytes_are_not_duplicates(tmp_path):
    a = _item(tmp_path, "one.pdf", b"aaa")
    b = _item(tmp_path, "two.pdf", b"bbb")
    kept, dropped = group_byte_duplicates([a, b])
    assert dropped == 0 and len(kept) == 2


def test_bib_duplicates_grouped_by_identifier(tmp_path):
    a = _item(tmp_path, "one.pdf", b"aaa")
    b = _item(tmp_path, "two.pdf", b"bbb")
    for it in (a, b):
        it.record = BibRecord(title="Same paper", doi="10.1/x",
                              authors=[Author("Suslin", "A.")])
    groups = group_bib_duplicates([a, b])
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 2


def test_rank_versions_prefers_text_layer_over_scan(tmp_path):
    scan = _item(tmp_path, "scan.pdf", b"a")
    scan.feat.ocr_used, scan.feat.pages, scan.feat.text_chars = True, 20, 900
    text = _item(tmp_path, "text.pdf", b"bb")
    text.feat.pages, text.feat.text_chars = 20, 30000
    assert rank_versions([scan, text])[0] is text


def test_unique_path(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert unique_path(p).name == "a (2).pdf"


def test_rerun_finds_a_document_at_its_numbered_name(tmp_path):
    # Two papers with one title share a folder: the second is "T (2).pdf".
    # A second run used to copy it again to "T (3).pdf" every time.
    from paperorg.repo import Placement, RepoWriter

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"first paper")
    b.write_bytes(b"second paper")
    where = Placement(Path("Serre, Jean-Pierre") / "Groupes algebriques.pdf")
    for _ in range(2):
        w = RepoWriter(cfg)
        got = [w.place(src, where, crc=crc32_file(src)).primary.name for src in (a, b)]
        assert got == ["Groupes algebriques.pdf", "Groupes algebriques (2).pdf"]
    assert len(list((cfg.output_dir / "Serre, Jean-Pierre").iterdir())) == 2
    assert w.reused == [Path("Serre, Jean-Pierre") / n for n in got]
    # A free clean name still wins over a numbered copy.
    (cfg.output_dir / where.primary).unlink()
    assert RepoWriter(cfg).place(b, where, crc=crc32_file(b)).primary == where.primary


def test_overrides_pin_a_record_and_split_a_false_version_group(tmp_path):
    # The corpus case: a Russian thesis matched to the author's paper of a
    # similar title, so the version prompt could only keep one of the two.
    from paperorg.pipeline import Organizer

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out",
                 online=False, interactive=False)
    paper = _item(cfg.input_dir, "V. Petrov - Overgroups of unitary groups.pdf", b"paper")
    thesis = _item(cfg.input_dir, "thesis.pdf", b"thesis", sub="theses")
    for it in (paper, thesis):
        it.feat_kind = DocKind.PAPER
        it.record = BibRecord(kind=DocKind.PAPER, title="Overgroups of unitary groups",
                              doi="10.1023/B:KTHE.0000006934.95243.91")
    assert len(group_bib_duplicates([paper, thesis])) == 1
    cfg.state_dir.mkdir(parents=True)
    cfg.overrides_path.write_text(
        "# corrections\n"
        "file\tkind\ttitle\tauthors\tyear\tschool\n"
        "theses/thesis.pdf\tthesis\tНадгруппы классических групп\tViktor Petrov\t2005"
        "\tSt. Petersburg State University\n"
        "gone/missing.pdf\tpaper\tNot in the input\t\t\t\n", encoding="utf-8")

    org = Organizer(cfg)
    assert org.apply_overrides([paper, thesis]) == 1
    rec = thesis.record
    assert thesis.override and rec.kind is DocKind.THESIS and rec.source == "override"
    assert (rec.title, rec.year, rec.doi) == ("Надгруппы классических групп", "2005", "")
    assert [a.family for a in rec.authors] == ["Petrov"]
    assert group_bib_duplicates([paper, thesis]) == {}
    org.decide([paper, thesis])                  # never asked, never replaced
    assert thesis.record is rec
    assert plan_placement(rec, ".pdf").primary.parts[0] == "Thesis"


# ------------------------------------------------------------------- config

def test_config_resolves_paths(tmp_path):
    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    assert cfg.bib_path.name == "library.bib"
    assert cfg.bib_path.parent == cfg.output_dir
    assert cfg.cache_path.parent == cfg.output_dir / ".paperorg"


# ------------------------------------------------------- title extraction

def test_extract_title_picks_largest_font_block():
    lines = [
        {"h": 6.4, "y": 60, "x": 100, "t": "JOURNAL OF PURE AND APPLIED ALGEBRA"},
        {"h": 7.1, "y": 98, "x": 100, "t": "Journal of Algebra 105 (1995) 93-105"},
        {"h": 11.9, "y": 134, "x": 100, "t": "Normal subgroups of classical groups"},
        {"h": 8.6, "y": 159, "x": 100, "t": "Leonid N. Vaserstein and Hong You"},
        {"h": 7.9, "y": 174, "x": 100, "t": "Department of Mathematics, Penn State"},
        {"h": 7.7, "y": 238, "x": 100, "t": "Abstract"},
    ]
    title, authors = extract_title_authors(lines)
    assert title == "Normal subgroups of classical groups"
    assert "Vaserstein" in authors
    assert "Department" not in authors


def test_extract_title_handles_empty():
    assert extract_title_authors([]) == ("", "")


# ------------------------------------------------------------ author identity

from paperorg.people import AuthorIndex, _fix_caps, normalise_author
from paperorg.sources import _clean_title
from paperorg.classify import refine_with_metadata
from paperorg.util import is_shouting, smart_title_case


@pytest.mark.parametrize("raw,expected", [
    ("YOU'AN CAO", "You'an Cao"),          # pinyin: lower case after apostrophe
    ("O'NEILL", "O'Neill"),                # but not after a one-letter prefix
    ("JEAN-PIERRE SERRE", "Jean-Pierre Serre"),
    ("VAN DER KALLEN", "van der Kallen"),
    ("Already Fine", "Already Fine"),
])
def test_fix_caps(raw, expected):
    assert _fix_caps(raw) == expected


def _idx(pairs):
    recs = [BibRecord(authors=[Author(f, g)]) for f, g in pairs]
    ix = AuthorIndex()
    ix.add_records(recs)
    ix.build()
    ix.apply(recs)
    return [r.authors[0].folder for r in recs]


def test_author_index_merges_initials_into_full_name():
    assert _idx([("Suslin", "A."), ("Suslin", "Andrei"), ("Suslin", "A. A.")]) == \
        ["Suslin, Andrei"] * 3


def test_author_index_keeps_different_people_apart():
    folders = _idx([("Stein", "Michael R."), ("Stein", "Yakov")])
    assert folders[0] != folders[1]


def test_author_index_prefers_the_richer_spelling():
    assert _idx([("Rao", "Ravi"), ("Rao", "Ravi A.")]) == ["Rao, Ravi A."] * 2


def test_author_index_normalises_shouting():
    assert _idx([("CAO", "YOU'AN"), ("Cao", "You'an")]) == ["Cao, You'an"] * 2


def test_normalise_author_splits_a_comma_in_family():
    a = normalise_author(Author(family="Serre, Jean-Pierre"))
    assert (a.family, a.given) == ("Serre", "Jean-Pierre")


# ---------------------------------------------------- registry title cleaning

def test_clean_title_folds_math_alphanumerics():
    assert _clean_title("Groups over \U0001d44d") == "Groups over Z"


def test_clean_title_rescues_shouting_titles():
    got = _clean_title("AUTOMORPHISMS OF CERTAIN NILPOTENT LIE ALGEBRAS")
    assert got == "Automorphisms of Certain Nilpotent Lie Algebras"


def test_clean_title_leaves_normal_titles_alone():
    assert _clean_title("Injective stability for K2") == "Injective stability for K2"
    assert not is_shouting("Injective stability for K2")


def test_smart_title_case_keeps_small_words_low():
    assert smart_title_case("ON THE STRUCTURE OF GROUPS") == "On the Structure of Groups"


# ------------------------------------------------- registry overrides locals

def test_journal_article_beats_a_local_thesis_guess():
    assert refine_with_metadata(DocKind.THESIS, "journal-article",
                                has_journal=True) is DocKind.PAPER


def test_dissertation_type_wins():
    assert refine_with_metadata(DocKind.PAPER, "dissertation") is DocKind.THESIS


def test_book_type_wins():
    assert refine_with_metadata(DocKind.PAPER, "book") is DocKind.BOOK


def test_thesis_survives_a_typeless_record():
    assert refine_with_metadata(DocKind.THESIS, "") is DocKind.THESIS


def test_school_field_rejects_a_mere_publisher():
    t = BibRecord(kind=DocKind.THESIS, title="T", authors=[Author("A", "B")],
                  publisher="JSTOR")
    assert "school" not in bibtex.format_entry(t, "k", None)
    t.publisher = "University of Göttingen"
    assert "school" in bibtex.format_entry(t, "k", None)


# -------------------------------------------------------- author line filter

from paperorg.extract import plausible_author_line


@pytest.mark.parametrize("line", [
    "Leonid N. Vaserstein and Hong You",
    "Wilberd van der Kallen",
    "A. Bak, N. Vavilov",
])
def test_author_line_accepts_real_names(line):
    assert plausible_author_line(line)


@pytest.mark.parametrize("line", [
    "della U NIVERSITA DI P ADOVA Y U C HEN",   # letter-spaced running head
    "factor group $G/G^{0}$; . . the type of",  # formula fragment
    "Department of Mathematics, Penn State",
    "1991 Mathematics Subject Classification",
    "Received 8 March 1993",
    "E-mail: someone@example.edu",
])
def test_author_line_rejects_debris(line):
    assert not plausible_author_line(line)


# ------------------------------------------------- directory hint isolation

def test_dir_hints_are_relative_to_the_input_root(tmp_path):
    """Regression: an absolute prefix such as /home/x/Documents must not be
    read as a classification hint - it made 65% of a real corpus look like
    'other' because of the folder named ``Documents``."""
    from paperorg.extract import analyse
    root = tmp_path / "Documents" / "posters" / "input"
    sub = root / "003 - K-theory"
    sub.mkdir(parents=True)
    f = sub / "A. Suslin - On K-theory.pdf"
    f.write_bytes(b"%PDF-1.4\n%not a real pdf\n")
    cfg = Config(input_dir=root, output_dir=tmp_path / "out", ocr=False)
    feat = analyse(f, cfg)
    assert feat.dir_hints == ["003 - k-theory"]
    assert "posters" not in feat.dir_hints and "documents" not in feat.dir_hints


def test_unreadable_file_with_a_clean_name_is_treated_as_a_paper():
    """A DjVu read without djvulibre yields no text and no page count, but
    'W. van der Kallen - Injective stability for K2.djvu' is still a paper."""
    f = DocFeatures(path=Path("/tmp/x.djvu"), pages=0, text="", text_chars=0)
    f.file_authors, f.file_title = parse_filename("W. van der Kallen - Injective stability for K2")
    kind, conf, _ = classify(f)
    assert kind is DocKind.PAPER and conf < 0.6


def test_unreadable_file_without_a_useful_name_stays_unknown():
    f = DocFeatures(path=Path("/tmp/scan001.djvu"), pages=0, text="", text_chars=0)
    f.file_authors, f.file_title = parse_filename("scan001")
    assert classify(f)[0] is DocKind.UNKNOWN


# ------------------------------------------- classifier false-positive guards

def test_obituary_mentioning_a_thesis_is_not_a_thesis():
    """Regression: 'In memoriam ...' was filed under Thesis/ because the text
    mentions the subject's PhD thesis."""
    f = _feat(pages=6, text="In memoriam. He defended his PhD thesis in 1973 "
                            "under the supervision of ... " * 4)
    assert classify(f)[0] is not DocKind.THESIS


def test_thesis_wording_deep_in_the_body_is_ignored():
    body = "x" * 3000 + " This thesis submitted in partial fulfilment "
    assert classify(_feat(pages=40, text=body))[0] is not DocKind.THESIS


def test_real_thesis_title_page_still_wins():
    f = _feat(pages=140, text="A thesis submitted in partial fulfilment of the "
                              "requirements for the degree of Doctor of Philosophy")
    assert classify(f)[0] is DocKind.THESIS


def test_long_paper_naming_a_publisher_is_not_a_book():
    """A 100-page JAMS paper names the American Mathematical Society in its
    front matter; that alone must not make it a book."""
    f = _feat(pages=100, text="Journal of the American Mathematical Society "
                              "Springer-Verlag " + "content " * 400)
    assert classify(f)[0] is DocKind.PAPER


def test_book_series_wording_does_make_a_book():
    f = _feat(pages=100, text="Graduate Texts in Mathematics 251\nPreface\n"
                              "Table of contents" + " body" * 300)
    assert classify(f)[0] is DocKind.BOOK


# ------------------------------------------- combining name hints

from paperorg.people import prefer_richer


def test_prefer_richer_upgrades_initials_from_the_title_page():
    """File name gives the order, the title page gives the spelling."""
    out = prefer_richer([Author("Suslin", "A.")], [Author("Suslin", "Andrei")])
    assert [a.folder for a in out] == ["Suslin, Andrei"]


def test_prefer_richer_keeps_the_file_name_order():
    out = prefer_richer([Author("Bak", "A."), Author("Vavilov", "N.")],
                        [Author("Vavilov", "Nikolai"), Author("Bak", "Anthony")])
    assert [a.folder for a in out] == ["Bak, Anthony", "Vavilov, Nikolai"]


def test_prefer_richer_ignores_incompatible_names():
    out = prefer_richer([Author("Suslin", "A.")], [Author("Suslin", "Boris")])
    assert [a.folder for a in out] == ["Suslin, A."]


def test_prefer_richer_falls_back_to_either_side():
    assert [a.folder for a in prefer_richer([], [Author("Serre", "Jean-Pierre")])] == \
        ["Serre, Jean-Pierre"]
    assert [a.folder for a in prefer_richer([Author("Serre", "J.-P.")], [])] == \
        ["Serre, J.-P."]


# ------------------------------------------- names without a real extension

from paperorg.extract import name_stem, sniff_format


@pytest.mark.parametrize("name,stem", [
    # Path.stem would cut at the last dot and return "G. Garkusha, I"
    ("G. Garkusha, I. Panin - Framed motives.pdf", "G. Garkusha, I. Panin - Framed motives"),
    ("G. Garkusha, I. Panin - Framed motives", "G. Garkusha, I. Panin - Framed motives"),
    ("A. Suslin, V. Kopeiko - Quadratic modulespdf",
     "A. Suslin, V. Kopeiko - Quadratic modulespdf"),
    ("J.-P. Serre - Trees.djvu", "J.-P. Serre - Trees"),
])
def test_name_stem(name, stem):
    assert name_stem(Path("/x") / name) == stem


def test_glued_extension_is_stripped_from_the_title():
    authors, title = parse_filename("A. Suslin, V. Kopeiko - Quadratic modules pdf".replace(" pdf", "pdf"))
    assert authors == "A. Suslin, V. Kopeiko"
    assert title == "Quadratic modules"


def test_a_title_ending_in_ps_is_not_truncated():
    """"...Chevalley groups" must not lose its "ps"."""
    assert parse_filename("N. Vavilov - Chevalley groups")[1] == "Chevalley groups"


def test_sniff_format_reads_magic_bytes(tmp_path):
    pdf = tmp_path / "no-extension-at-all"
    pdf.write_bytes(b"%PDF-1.5\nrest")
    assert sniff_format(pdf) == "pdf"
    djvu = tmp_path / "scan"
    djvu.write_bytes(b"AT&TFORM" + b"\x00" * 4 + b"DJVU" + b"rest")
    assert sniff_format(djvu) == "djvu"
    other = tmp_path / "notes.txt"
    other.write_bytes(b"just text")
    assert sniff_format(other) == ""


# ------------------------------------------------ generic-title protection

from paperorg.match import is_generic_title, score_candidate as _score
from paperorg.extract import _HEADER_JUNK


@pytest.mark.parametrize("title", [
    "Introduction", "1. Introduction", "References", "Table of contents",
    "Preface", "Front matter", "",
])
def test_generic_titles_are_recognised(title):
    assert is_generic_title(title.lstrip("1. "))


def test_real_titles_are_not_generic():
    assert not is_generic_title("Injective stability for K2")
    assert not is_generic_title("An introduction to algebraic K-theory")


def test_generic_candidate_cannot_win():
    """Regression: a Crossref book chapter literally titled 'Introduction'
    was matched to 'Local-Global principle for quadratic groups'."""
    rec = BibRecord(title="Introduction", authors=[Author("Basu", "Rikmantra")])
    c = _score(rec, "Local-Global principle for quadratic and hermitian groups",
               "R. Basu")
    assert c.score == 0.0


@pytest.mark.parametrize("line", [
    "1. Introduction", "2.1 Preliminaries", "§ 3. Notation", "Introduction",
    "References", "Contents",
])
def test_header_junk_matches_numbered_section_headings(line):
    assert _HEADER_JUNK.match(line)


def test_header_junk_leaves_real_titles_alone():
    assert not _HEADER_JUNK.match("Injective stability for K2")
    assert not _HEADER_JUNK.match("An introduction to algebraic K-theory")


def test_section_heading_is_not_taken_as_a_title():
    lines = [
        {"h": 9.0, "y": 60, "x": 70, "t": "R. Basu"},
        {"h": 12.0, "y": 120, "x": 70, "t": "1. Introduction"},
        {"h": 9.0, "y": 150, "x": 70, "t": "The vigorous study of general linear groups"},
    ]
    title, _ = extract_title_authors(lines)
    assert "Introduction" not in title


# ------------------------------------------- distinct people stay distinct

def test_two_spelled_out_given_names_are_not_merged():
    """Rabeya Basu and Rikmantra Basu both abbreviate to 'R. Basu'."""
    folders = _idx([("Basu", "Rabeya"), ("Basu", "Rikmantra")])
    assert folders[0] != folders[1]


def test_an_initial_still_merges_into_one_full_name():
    assert _idx([("Basu", "R."), ("Basu", "Rabeya")]) == ["Basu, Rabeya"] * 2


def test_a_longer_form_of_the_same_name_still_merges():
    assert _idx([("Rao", "Ravi"), ("Rao", "Ravi A.")]) == ["Rao, Ravi A."] * 2


# --------------------------------------------------------- ascii cite keys

def test_cite_keys_are_ascii_even_for_cyrillic_records():
    rec = BibRecord(title="Надгруппы полупростых групп", year="2008",
                    authors=[Author("Вавилов", "Н.")])
    key = bibtex.cite_key(rec, set())
    assert key.isascii() and key.startswith("Vavilov2008")


def test_cite_key_keeps_latin_names_readable():
    rec = BibRecord(title="Injective stability for K2", year="1977",
                    authors=[Author("van der Kallen", "W.")])
    assert bibtex.cite_key(rec, set()).startswith("vanderKallen1977")


def test_ambiguous_initials_are_left_alone_when_two_people_share_them():
    """With both Rabeya and Rikmantra Basu present, a bare 'R. Basu' must not
    be silently assigned to either of them."""
    recs = [BibRecord(authors=[Author("Basu", "Rabeya")]),
            BibRecord(authors=[Author("Basu", "Rikmantra")]),
            BibRecord(authors=[Author("Basu", "R.")])]
    ix = AuthorIndex()
    ix.add_records(recs)
    ix.build()
    ix.apply(recs)
    assert [r.authors[0].folder for r in recs] == \
        ["Basu, Rabeya", "Basu, Rikmantra", "Basu, R."]


def test_unambiguous_initials_still_upgrade():
    recs = [BibRecord(authors=[Author("Suslin", "Andrei")]),
            BibRecord(authors=[Author("Suslin", "A.")])]
    ix = AuthorIndex()
    ix.add_records(recs)
    ix.build()
    ix.apply(recs)
    assert [r.authors[0].folder for r in recs] == ["Suslin, Andrei"] * 2


# ------------------------------------------------- title candidate ordering

def _tc(file_authors="", file_title="", page_title="", meta_title=""):
    f = DocFeatures(path=Path("/tmp/x.pdf"))
    f.file_authors, f.file_title = file_authors, file_title
    f.page_title, f.meta_title = page_title, meta_title
    return f.title_candidates()


def test_a_human_written_filename_wins():
    got = _tc("A. Suslin", "On the structure of SL_n", "1. Introduction")
    assert got[0] == "On the structure of SL_n"


def test_a_bare_stem_loses_to_the_page_title():
    """Regression: 'vdk-v2.pdf' was filed under the title 'vdk-v2'."""
    got = _tc("", "vdk-v2", "Injective stability for K2")
    assert got[0] == "Injective stability for K2"


def test_opaque_filename_defers_to_the_document():
    got = _tc("", "deodhar82", "Some characterizations of Coxeter groups")
    assert got[0] == "Some characterizations of Coxeter groups"


def test_pdf_metadata_title_is_used_when_nothing_else_is_there():
    assert _tc("", "scan01", "", "Clifford algebras and spinor norms")[0] == \
        "Clifford algebras and spinor norms"


def test_scoring_survives_a_non_numeric_year():
    rec = BibRecord(title="Injective stability for K2", year="2011-2012",
                    authors=[Author("Kallen", "W.")])
    c = _score(rec, "Injective stability for K2", "W. van der Kallen", "2011")
    assert c.score > 0.9


def test_scoring_survives_an_empty_year():
    rec = BibRecord(title="Injective stability for K2", year="n.d.")
    assert _score(rec, "Injective stability for K2", "", "1977").score > 0.8


def test_author_folder_and_target_name_are_stable():
    a = Author("van der Kallen", "Wilberd")
    assert author_folder(a) == "van der Kallen, Wilberd"
    assert author_folder(None) == "Unknown"
    rec = BibRecord(kind=DocKind.PAPER, title="Injective stability for K2")
    assert target_name(rec, ".pdf") == "Injective stability for K2.pdf"


def test_only_near_misses_are_offered_for_review():
    """Candidates far below the accept threshold are noise, not choices."""
    from paperorg.match import Resolver
    from paperorg.record import DocFeatures

    class FakeCrossref:
        def by_doi(self, doi): return None
        def search(self, title, authors="", rows=5, filters=""):
            return [BibRecord(title="Injective stability for K3",
                              authors=[Author("Kallen", "W.")]),
                    BibRecord(title="Something entirely different",
                              authors=[Author("Nobody", "X.")])]

    class FakeArxiv:
        def by_id(self, i): return None
        def search_title(self, t, rows=4): return []

    class Nothing:
        def enrich(self, rec): pass
        def search(self, *a, **k): return []

    cfg = Config(input_dir=Path("/tmp/in"), output_dir=Path("/tmp/out"),
                 accept_score=0.99, review_score=0.70)
    r = Resolver(cfg, FakeCrossref(), FakeArxiv(), Nothing(), Nothing())
    feat = DocFeatures(path=Path("/tmp/x.pdf"))
    # No author hint, so the near miss scores on title alone (~0.96) and stays
    # under the deliberately strict accept threshold.
    feat.page_title = "Injective stability for K2"
    rec, cands = r.resolve(feat, DocKind.PAPER)
    assert rec is None
    assert len(cands) == 1
    assert cands[0].record.title == "Injective stability for K3"


# -------------------------------------------------------------- http caching

def test_failed_responses_are_not_cached(tmp_path):
    """A flaky network moment must not become a ninety-day 'not found'."""
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out", retries=1)
    cache = Cache(tmp_path / "c.sqlite3")
    http = Http(cfg, cache)

    calls = {"n": 0}

    def flaky(url, accept):
        calls["n"] += 1
        return (500, "") if calls["n"] == 1 else (200, '{"ok": true}')

    http.fetch = flaky
    assert http.get("https://example.org/x") is None
    assert http.get("https://example.org/x") == {"ok": True}      # retried
    assert http.get("https://example.org/x") == {"ok": True}      # now cached
    assert calls["n"] == 2


def test_successful_responses_are_cached(tmp_path):
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    calls = {"n": 0}

    def once(url, accept):
        calls["n"] += 1
        return (200, '{"v": 1}')

    http.fetch = once
    http.get("https://example.org/y")
    http.get("https://example.org/y")
    assert calls["n"] == 1


def test_both_http_backends_are_wired(tmp_path):
    """requests is optional; urllib must be usable whichever is installed."""
    import urllib.request as _ur
    from paperorg import sources
    assert hasattr(sources, "urllib") and hasattr(_ur, "urlopen")
    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = sources.Http(cfg, NullCacheForTest())
    # Either a requests Session or the urllib path; both go through fetch().
    assert callable(http.fetch)


class NullCacheForTest:
    def get_http(self, k): return None
    def put_http(self, k, v): pass


# ------------------------------------------------- eponym capitalisation

from paperorg.util import restore_eponyms


@pytest.mark.parametrize("raw,fixed", [
    ("On adjoint chevalley groups over euclidean lie algebras",
     "On adjoint Chevalley groups over Euclidean Lie algebras"),
    ("homology of classical lie groups", "homology of classical Lie groups"),
    ("a note on the bass conjecture", "a note on the Bass conjecture"),
    ("galois cohomology and brauer groups", "Galois cohomology and Brauer groups"),
])
def test_restore_eponyms(raw, fixed):
    assert restore_eponyms(raw) == fixed


@pytest.mark.parametrize("raw", [
    "Theorems that lie deeper than expected",     # 'lie' the verb
    "The bass line of the argument",              # 'bass' with no maths noun
    "Injective stability for K2",                 # nothing to do
])
def test_restore_eponyms_leaves_ordinary_words(raw):
    assert restore_eponyms(raw) == raw


def test_record_title_and_filename_agree_on_eponyms():
    """The repository file name comes from the record, so the fix has to live
    in the record rather than only in the BibTeX writer."""
    from paperorg.sources import _clean_title
    title = _clean_title("on chevalley groups over commutative rings")
    rec = BibRecord(kind=DocKind.PAPER, title=title, authors=[Author("Vavilov", "N.")])
    assert "Chevalley" in target_name(rec, ".pdf")
    assert "{Chevalley}" in bibtex.format_entry(rec, "k", None)


# ------------------------------------------- OCR debris must not become data

from paperorg.people import plausible_local_title, plausible_person


@pytest.mark.parametrize("author,ok", [
    (Author("Vaserstein", "Leonid"), True),
    (Author("van der Kallen", "W."), True),          # particle, lower case
    (Author("de Bruijn", "N. G."), True),
    (Author("Васерштейн", "Л. Н."), True),
    (Author("Stands", 'Here "E"'), False),           # OCR of a caption
    (Author("теоремы", "Л. Н."), False),             # a noun, not a surname
    (Author("Mathematica", "On normal subgroups of GL 2 over rings "
                           "© Foundation Compositio"), False),
    (Author("", ""), False),
])
def test_plausible_person(author, ok):
    assert plausible_person(author) is ok


@pytest.mark.parametrize("title,ok", [
    ("Injective stability for K2", True),
    ("Стабилизация для классических групп над кольцами", True),
    ("C OMPOSITIO M ATHEMATICA", False),             # letter-spaced masthead
    ("in k such that g~ ι x t {c)g e H.", False),    # formula debris
    ("х £ {A,} 1 ^ г =т^ / ^ г ) и определяющими", False),
])
def test_plausible_local_title(title, ok):
    assert plausible_local_title(title) is ok


# ------------------------------------------------- brace-balanced escaping

def test_escape_keeps_braces_balanced():
    """BibTeX counts \\{ toward brace depth, so an unbalanced brace in an
    OCR'd title used to swallow the rest of the file."""
    out = bibtex.escape("in k such that g~ x t {c)g e H.")
    assert out.count("{") == out.count("}")
    assert "\\{" not in out


def test_entry_with_stray_braces_stays_parseable():
    rec = BibRecord(kind=DocKind.PAPER, title="a }stray{ brace }",
                    authors=[Author("Doe", "J.")], year="1999", journal="J")
    entry = bibtex.format_entry(rec, "k", "Doe, J/x.pdf")
    assert entry.count("{") == entry.count("}")


# --------------------------------------------------------- thesis school

from paperorg.extract import extract_school


@pytest.mark.parametrize("text,expected", [
    ("A thesis submitted to\nThe University of Manchester\nfor the degree of PhD",
     "The University of Manchester"),
    ("Rheinische Friedrich-Wilhelms-Universität Bonn\nMathematisches Institut",
     "Rheinische Friedrich-Wilhelms-Universität Bonn"),
    ("Universiteit Utrecht\nProefschrift", "Universiteit Utrecht"),
    ("Massachusetts Institute of Technology\nSeptember 2004",
     "Massachusetts Institute of Technology"),
    ("École Normale Supérieure\nThèse de doctorat", "École Normale Supérieure"),
])
def test_extract_school(text, expected):
    assert extract_school(text) == expected


def test_extract_school_ignores_a_journal_masthead():
    assert extract_school("Journal of Algebra 105 (1995) 93-105\n"
                          "Normal subgroups of classical groups") == ""


def test_extract_school_prefers_the_university_over_the_institute():
    got = extract_school("Mathematisches Institut\nUniversität Göttingen")
    assert "Universität" in got


# ------------------------------------- contributor roles in the name field

@pytest.mark.parametrize("raw,folder", [
    # arXiv really does list this as an author of "de Rham-Witt sheaves"
    ("with an appendix by Kay Rülling", "Rülling, Kay"),
    ("and an appendix by J. Doe", "Doe, J."),
    ("with the collaboration of A. Smith", "Smith, A."),
    ("Kay Rülling", "Rülling, Kay"),                 # untouched
    ("Wilberd van der Kallen", "van der Kallen, Wilberd"),
])
def test_role_prefixes_are_stripped_from_registry_names(raw, folder):
    assert _split_name(raw).folder == folder


def test_is_cyrillic():
    from paperorg.util import is_cyrillic
    assert is_cyrillic("А. Суслин - О структуре специальной линейной группы.pdf")
    assert not is_cyrillic("A. Suslin - On the structure of the special linear group.pdf")
    assert not is_cyrillic("")


@pytest.mark.parametrize("meta", ["thesis.dvi", "Microsoft Word - talk.doc", "main.tex"])
def test_a_file_name_in_the_title_field_is_not_a_title(meta):
    from paperorg.record import DocFeatures
    f = DocFeatures(path=Path("thesis.ps"), meta_title=meta)
    assert meta not in f.title_candidates()


@pytest.mark.parametrize("name, stem", [
    ("A. Suslin - Title.ps.gz", "A. Suslin - Title"),
    ("B. Kahn - Approaches to SK_1 of CSA.gz", "B. Kahn - Approaches to SK_1 of CSA"),
    ("Subgroups containing a torus. II.ps.gz", "Subgroups containing a torus. II"),
])
def test_name_stem_strips_gz(name, stem):
    from paperorg.extract import name_stem
    assert name_stem(Path(name)) == stem


def test_a_definite_not_found_is_cached(tmp_path):
    """zbMATH answers "no hits" with 404; asking again on every run is waste."""
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    calls = {"n": 0}

    def missing(url, accept):
        calls["n"] += 1
        return (404, "")

    http.fetch = missing
    assert http.get("https://example.org/z") is None
    assert http.get("https://example.org/z") is None
    assert calls["n"] == 1


def test_a_rate_limiting_host_is_left_alone_for_the_rest_of_the_run(tmp_path, monkeypatch):
    from paperorg import sources
    from paperorg.cache import Cache
    from paperorg.sources import Http

    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out", retries=3)
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    calls = {"n": 0}

    def limited(url, accept):
        calls["n"] += 1
        return (429, "")

    http.fetch = limited
    http.begin_item()
    assert http.get("https://export.arxiv.org/api/query?q=1") is None
    assert http.failed_hosts() == {"export.arxiv.org"}
    assert "export.arxiv.org" in http.blocked
    before = calls["n"]
    http.begin_item()
    assert http.get("https://export.arxiv.org/api/query?q=2") is None
    assert calls["n"] == before, "a blocked host must not be asked again"
    assert http.failed_hosts() == {"export.arxiv.org"}


def test_an_outright_block_is_given_up_on_at_once(tmp_path, monkeypatch):
    from paperorg import sources
    from paperorg.cache import Cache
    from paperorg.sources import Http

    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out", retries=3)
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    calls = {"n": 0}

    def banned(url, accept):
        calls["n"] += 1
        return (406, "")

    http.fetch = banned
    http.begin_item()
    assert http.get("https://export.arxiv.org/api/query?q=1") is None
    assert calls["n"] == 1, "a block is not retried"
    assert http.failed_hosts() == {"export.arxiv.org"}, "a block is not 'not found'"
    assert http.refusals() == "export.arxiv.org (HTTP 406 Not Acceptable)"


def test_a_rate_limited_host_gets_one_connection_at_a_time(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    state = {"open": 0, "most": 0}
    guard = threading.Lock()

    def slow(url, accept):          # answers take longer than the delay
        with guard:
            state["open"] += 1
            state["most"] = max(state["most"], state["open"])
        time.sleep(0.05)
        with guard:
            state["open"] -= 1
        return (200, "<feed/>")

    http.fetch = slow
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: http.get(f"https://export.arxiv.org/api/query?q={i}",
                                         delay=0.01, parse="text"), range(8)))
    assert state["most"] == 1


def test_a_definite_answer_is_not_a_failed_lookup(tmp_path):
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    http.fetch = lambda url, accept: (404, "")
    http.begin_item()
    http.get("https://api.zbmath.org/x")
    assert http.failed_hosts() == set()


# --------------------------------------------------------------- Math-Net.Ru

_MN_RESULTS = """<span class=red><b>Найдено публикаций: 1</b></span><table>
<tr><td valign=top>1.</td><td valign=top align=left>
<a class=SLink href="/rus/aa1249">Геометрия корневых элементов в&nbsp;группах типа&nbsp;<nobr>$\\mathrm E_6$</nobr></a><br>И.&nbsp;М.&nbsp;Певзнер<br><i>Алгебра и анализ</i>, 2011<nobr><b>, 23</b>:3</nobr>, &nbsp;<nobr>261&ndash;309</nobr>
</td></tr></table>"""

_MN_RUS = """<a href='https://doi.org/10.1090/S1061-0022-2012-01210-0'>badge</a>
<a href='https://doi.org/10.4213/aa1249'>DOI</a>
<a href='https://mathscinet.ams.org/mathscinet-getitem?mr=2896171'></a>
<a href='https://zbmath.org/?q=an:1285.20048'></a>
<div class='msc'><b>MSC:</b> Primary <a href='https://mathscinet.ams.org/mathscinet/freetools/msc-search?text=20G41'>20G41</a>;
Secondary <a href='https://mathscinet.ams.org/mathscinet/freetools/msc-search?text=17B22'>17B22</a></div>
<b>Английская версия:</b> <i>St. Petersburg Math. J.</i>
DOI: <a href='https://doi.org/10.1090/S1061-0022-2012-01210-0'>x</a>"""

_MN_ENG = """<title>I.&nbsp;M.&nbsp;Pevzner, &#x201C;The geometry of root elements in groups of type
$\\mathrm E_6$&#x201D;, Algebra i Analiz, 23:3 (2011), 261&ndash;309; St. Petersburg Math. J.,
23:3 (2012), 603&ndash;635</title>"""


def test_mathnet_search_rows_are_parsed():
    from paperorg.sources import parse_mathnet_results
    [rec] = parse_mathnet_results(_MN_RESULTS)
    assert rec.title == "Геометрия корневых элементов в группах типа $\\mathrm E_6$"
    assert [(a.given, a.family) for a in rec.authors] == [("И. М.", "Певзнер")]
    assert (rec.journal, rec.year, rec.volume, rec.number, rec.pages) == \
        ("Алгебра и анализ", "2011", "23", "3", "261--309")
    assert rec.url.endswith("/rus/aa1249") and rec.language == "russian"


def test_mathnet_pages_give_ids_msc_latin_names_and_the_translation():
    from paperorg.sources import apply_mathnet_pages, parse_mathnet_results
    [rec] = parse_mathnet_results(_MN_RESULTS)
    translation = apply_mathnet_pages(rec, _MN_RUS, _MN_ENG)
    assert rec.doi == "10.4213/aa1249", "the original's DOI, not the translation's"
    assert translation == "10.1090/S1061-0022-2012-01210-0"
    assert (rec.mrnumber, rec.zbl, rec.msc) == ("2896171", "1285.20048", ["20G41", "17B22"])
    assert [a.family for a in rec.authors] == ["Pevzner"]
    assert rec.bib_note.startswith("English translation: “The geometry of root elements")
    assert "St. Petersburg Math. J., 23:3 (2012), 603–635" in rec.bib_note
    assert "Algebra i Analiz" not in rec.bib_note


def test_transliterated_names_agree():
    from paperorg.match import author_agreement
    from paperorg.record import Author, BibRecord
    rec = BibRecord(authors=[Author(given="Н. А.", family="Вавилов"),
                             Author(given="В. А.", family="Петров")])
    assert author_agreement(rec, "N. Vavilov, V. Petrov") == 1.0
    assert author_agreement(rec, "A. Suslin") == 0.0


def test_msc_goes_to_mrclass_and_keywords():
    from paperorg.record import BibRecord
    rec = BibRecord(kind=DocKind.PAPER, title="T", year="2000", journal="J",
                    msc=["20G35", "14L15", "13D15"])
    text = bibtex.format_entry(rec, "k", "")
    assert re.search(r"mrclass\s*= \{20G35 \(14L15 13D15\)\}", text)
    assert "MSC: 20G35, 14L15, 13D15" in text


def test_retry_after_is_reported_in_words(tmp_path):
    import time as _time
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    host = "export.arxiv.org"
    assert "did not say when to retry" in http.retry_after_note(host)
    http.retry_after[host] = ("3600", _time.time())
    assert "in about 60 min" in http.retry_after_note(host)
    http.retry_after[host] = ("Wed, 21 Oct 2099 07:28:00 GMT", _time.time())
    assert "2099-10-21" in http.retry_after_note(host)
    http.retry_after[host] = ("soon", _time.time())
    assert "unreadable Retry-After" in http.retry_after_note(host)


def test_urllib_refusals_are_statuses_not_exceptions(tmp_path, monkeypatch):
    """Without requests, urllib raises on 4xx; a 429 must still count as a
    refusal (and its Retry-After be kept), not as a network hiccup."""
    import io
    import urllib.error
    import urllib.request
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    http.session = None

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "120"}, io.BytesIO(b"slow down"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert http.fetch("https://export.arxiv.org/api/query?q=1", "text/xml") == (429, "slow down")
    assert "in about 2 min" in http.retry_after_note("export.arxiv.org")


def test_removesuffix_backport():
    from paperorg.util import removesuffix
    assert removesuffix("paper.ps.gz", ".gz") == "paper.ps"
    assert removesuffix("paper.ps", ".gz") == "paper.ps"
    assert removesuffix("x", "") == "x"


def test_crossref_is_paced_and_capped(tmp_path):
    """Crossref's polite pool: 3 requests a second, 3 at a time."""
    from concurrent.futures import ThreadPoolExecutor
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out",
                 crossref_rate=20, crossref_concurrency=3)
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    state = {"open": 0, "most": 0, "starts": []}
    guard = threading.Lock()

    def slow(url, accept, form=None):
        with guard:
            state["open"] += 1
            state["most"] = max(state["most"], state["open"])
            state["starts"].append(time.time())
        time.sleep(0.2)                  # slower than the spacing, so they pile up
        with guard:
            state["open"] -= 1
        return 200, '{"message": {"items": []}}'

    http.fetch = slow
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: http.get("https://api.crossref.org/works", {"q": i}), range(12)))
    starts = sorted(state["starts"])
    assert state["most"] == 3
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.045   # 1/20 s apart


def test_crossref_pace_follows_its_headers(tmp_path):
    from paperorg.cache import Cache
    from paperorg.sources import Http

    cfg = Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out", crossref_rate=5)
    http = Http(cfg, Cache(tmp_path / "c.sqlite3"))
    pace = http.pace["api.crossref.org"]
    http._note_limits("https://api.crossref.org/works?q=x",
                      {"X-Rate-Limit-Limit": "2", "X-Rate-Limit-Interval": "1s",
                       "X-Concurrency-Limit": "1"})
    assert (round(pace.interval, 2), pace.concurrency) == (0.5, 1)
    http._note_limits("https://api.crossref.org/works?q=y",
                      {"X-Rate-Limit-Limit": "50", "X-Rate-Limit-Interval": "1s"})
    assert round(pace.interval, 2) == 0.5          # never loosens
