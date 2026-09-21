"""Interactive decision paths: duplicate choice and unknown-document handling."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from paperorg.cache import NullCache
from paperorg.config import Config
from paperorg.dedupe import Item, rank_versions
from paperorg.match import Candidate
from paperorg.prompts import Prompter
from paperorg.record import Author, BibRecord, DocFeatures


@pytest.fixture
def cfg(tmp_path):
    return Config(input_dir=tmp_path / "in", output_dir=tmp_path / "out")


@pytest.fixture
def prompter(cfg):
    p = Prompter(cfg, NullCache())
    p.enabled = True                      # pretend we are on a terminal
    return p


def _answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))


def _item(tmp_path, name, *, pages=10, size=1000, ocr=False, chars=5000):
    f = DocFeatures(path=tmp_path / name, crc32=f"{abs(hash(name)) % 2**32:08x}",
                    size=size, pages=pages, ocr_used=ocr, text_chars=chars)
    return Item(feat=f)


# ------------------------------------------------------- requirement 5

def test_choose_version_returns_selected_index(prompter, tmp_path, monkeypatch):
    a = _item(tmp_path, "scan.pdf", ocr=True)
    b = _item(tmp_path, "clean.pdf")
    _answers(monkeypatch, "2")
    rec = BibRecord(title="T", authors=[Author("A", "B")], doi="10.1/x")
    assert prompter.choose_version(rec, [a, b]) == 1


def test_choose_version_default_is_the_ranked_best(prompter, tmp_path, monkeypatch):
    scan = _item(tmp_path, "scan.pdf", ocr=True, chars=600)
    clean = _item(tmp_path, "clean.pdf", chars=40000)
    ordered = rank_versions([scan, clean])
    assert ordered[0] is clean            # text layer beats a scan
    _answers(monkeypatch, "")             # user just hits enter
    rec = BibRecord(title="T", authors=[Author("A", "B")])
    assert prompter.choose_version(rec, ordered) == 0


def test_choose_version_keep_all(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "a")
    assert prompter.choose_version(BibRecord(title="T"),
                                   [_item(tmp_path, "x.pdf"),
                                    _item(tmp_path, "y.pdf")]) == -1


def test_choose_version_skip_all(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "s")
    assert prompter.choose_version(BibRecord(title="T"),
                                   [_item(tmp_path, "x.pdf"),
                                    _item(tmp_path, "y.pdf")]) is None


def test_choose_version_rejects_nonsense_then_accepts(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "zzz", "1")
    assert prompter.choose_version(BibRecord(title="T"),
                                   [_item(tmp_path, "x.pdf"),
                                    _item(tmp_path, "y.pdf")]) == 0


def test_choose_version_non_interactive_takes_the_best(cfg, tmp_path):
    p = Prompter(cfg, NullCache())
    p.enabled = False
    assert p.choose_version(BibRecord(title="T"), [_item(tmp_path, "x.pdf")]) == 0


# ------------------------------------------------------- requirement 7

@pytest.mark.parametrize("reply,expected", [
    ("i", "ignore"), ("u", "unsorted"), ("p", "paper"),
    ("b", "book"), ("t", "thesis"), ("", "unsorted"),
])
def test_classify_unknown_maps_every_answer(prompter, tmp_path, monkeypatch,
                                            reply, expected):
    _answers(monkeypatch, reply)
    item = _item(tmp_path, "poster.pdf", pages=1)
    item.feat.path = prompter.cfg.input_dir / "poster.pdf"
    assert prompter.classify_unknown(item, "1 page, little text") == expected


def test_classify_unknown_apply_to_all_is_remembered(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "i!")           # trailing '!' = apply to everything
    first = _item(tmp_path, "a.pdf")
    first.feat.path = prompter.cfg.input_dir / "a.pdf"
    assert prompter.classify_unknown(first, "x") == "ignore"
    second = _item(tmp_path, "b.pdf")
    second.feat.path = prompter.cfg.input_dir / "b.pdf"
    # no further input is offered; the remembered answer must be reused
    assert prompter.classify_unknown(second, "x") == "ignore"


def test_classify_unknown_honours_default_when_not_interactive(cfg, tmp_path):
    cfg.default_unknown_action = "ignore"
    p = Prompter(cfg, NullCache())
    p.enabled = False
    assert p.classify_unknown(_item(tmp_path, "a.pdf"), "x") == "ignore"


def test_eof_falls_back_to_the_default(prompter, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    item = _item(tmp_path, "a.pdf")
    item.feat.path = prompter.cfg.input_dir / "a.pdf"
    assert prompter.classify_unknown(item, "x") == "unsorted"


# --------------------------------------------------- uncertain match review

def _cand(title, score):
    rec = BibRecord(title=title, authors=[Author("Suslin", "Andrei")], year="1977",
                    doi="10.1/" + title[:3])
    return Candidate(rec, score, score, 0.0, f"title {score:.2f}")


def test_confirm_match_picks_a_candidate(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "2")
    item = _item(tmp_path, "x.pdf")
    cands = [_cand("Wrong paper", 0.72), _cand("Right paper", 0.71)]
    got = prompter.confirm_match(item, cands)
    assert got is not None and got.title == "Right paper"
    assert "manual" in got.source


def test_confirm_match_none_of_these(prompter, tmp_path, monkeypatch):
    _answers(monkeypatch, "n")
    assert prompter.confirm_match(_item(tmp_path, "x.pdf"), [_cand("A", 0.7)]) is None


def test_confirm_match_without_candidates(prompter, tmp_path):
    assert prompter.confirm_match(_item(tmp_path, "x.pdf"), []) is None


def test_decisions_are_memoised_across_runs(cfg, tmp_path, monkeypatch):
    from paperorg.cache import Cache
    cache = Cache(tmp_path / "c.sqlite3")
    p1 = Prompter(cfg, cache)
    p1.enabled = True
    _answers(monkeypatch, "2")
    rec = BibRecord(title="T", doi="10.1/x")
    assert p1.choose_version(rec, [_item(tmp_path, "a.pdf"),
                                   _item(tmp_path, "b.pdf")]) == 1
    # A second run must not ask again.
    p2 = Prompter(cfg, cache)
    p2.enabled = True
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("asked twice"))
    assert p2.choose_version(rec, [_item(tmp_path, "a.pdf"),
                                   _item(tmp_path, "b.pdf")]) == 1


def test_colour_is_disabled_when_not_a_terminal(monkeypatch):
    """Piped output must not be littered with escape codes."""
    import importlib, sys as _sys
    import paperorg.prompts as pr
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    reloaded = importlib.reload(pr)
    assert reloaded.BOLD == "" and reloaded.RESET == ""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True, raising=False)
    reloaded = importlib.reload(pr)
    assert reloaded.CYAN == ""
    importlib.reload(pr)
