# paper-organizer

Turns a pile of unsorted mathematical PDFs into a tidy local repository with a
BibTeX index that JabRef (or `bibtex`/`biber`) can open directly.

```
input/                                 NEW_REPO/
  003 - K-theory/                        library.bib
    vdk-stability.pdf                    van der Kallen, Wilberd/
  scans/                                   Injective stability for K2.pdf
    deodhar82.pdf                        Stein, Michael R./
  A. Bak - Powers.pdf        ------>       Injective stability for K2.pdf  -> symlink
  thesis_reich.pdf                       Bak, Anthony/
  slides/ICTP.pdf                          Presenting powers of augmentation ideals.pdf
                                         Books/
                                           The Finite Simple Groups (R. A. Wilson) [2009].pdf
                                         Thesis/
                                           Group von Neumann algebras (H. Reich) [1998].pdf
                                         Unsorted/
                                           ICTP.pdf
```

## Quick start

```bash
sudo apt install poppler-utils                 # required
sudo apt install tesseract-ocr djvulibre-bin   # optional: scans and DjVu
pip install requests                           # optional: faster HTTP than urllib

./organize.py input/ library/ --sample 50 -n   # dry run on 50 random files
./organize.py input/ library/                  # the real thing, interactive
```

Nothing but `poppler-utils` is mandatory, and the only Python dependency is
`requests` (it falls back to `urllib` if absent). A dry run writes no
documents — only `library/.paperorg/report.csv`, the plan, one row per file.

## How a document is identified

The cascade stops at the first step that succeeds, cheapest first.

| # | Step | Signal | Measured on the corpus |
|---|------|--------|-------------------------------|
| 1 | Embedded identifier | `arXiv:1601.06642`, `doi:10.1007/...` printed on pages 1–3 | present in ~38% of files |
| 2 | File name | `A. Bak, N. Vavilov - Presenting powers.pdf` → Crossref / arXiv title search | ~93% of names carry this shape |
| 3 | Layout-aware title | largest type in the upper part of page 1 (`pdftotext -bbox-layout`), authors from the lines below | rescues opaque names (`deodhar82.pdf`) |
| 4 | OCR | `pdftoppm` + `tesseract` (eng+rus) for scans with no text layer | ~9% of files have no usable text layer |
| 5 | Local fallback | whatever the file itself says | keeps Russian-language and unindexed items usable |

Before any of that, each file is **classified** — paper, book, thesis, or
something else — from page geometry, page count, producer metadata, title-page
wording and the folder it sits in. This is what keeps slides and posters out of
the author folders, and it also picks the right lookup: books are searched as
books, and a registry that says "journal-article with a container title" beats
a local guess of "thesis".

Candidate records are scored on title similarity plus author-surname overlap
and a year check; anything at or above `--accept` (default 0.86) is taken
automatically, anything between that and 0.70 is offered to you, and the rest
falls through to step 5.

Metadata sources: **Crossref** (DOI, journal, volume, pages), **arXiv**
(preprints), **zbMATH** (Zbl number, MSC codes), **OpenLibrary** (books).

> **MR numbers.** MathSciNet has no free API, so `mrnumber` is filled only when
> the number is printed in the document itself or appears in Crossref's
> `alternative-id`. zbMATH's `zbl` field and MSC codes are always attempted and
> serve the same purpose for most workflows.

## Measured on the real corpus

A complete run over the 1196 documents in `input/`:

| | |
|---|---|
| placed in the repository | 1101 (485 co-author symlinks, none broken) |
| resolved to a DOI or arXiv id | **805 — 73%** |
| metadata from a registry | 70% (Crossref 43%, arXiv 17%, DOI lookup 10%) |
| metadata read from the file alone | 30% |
| byte-identical duplicates dropped | 36 |
| different files, one entry (resolved) | 59 |
| author name variants merged | 306 |
| classified | 965 papers, 41 theses, 24 books, 71 other |

The bibliography passes `bibtex` with **zero errors**, parses with zero failed
blocks, and all 1101 `file` fields resolve to a real file. The remaining
`bibtex` warnings are all "empty journal/year" on items no registry indexes.

That 30% is not a set of matching failures to be tuned away. Spot-checking
shows Russian-language originals and preprints Crossref genuinely does not
carry; for those the best available candidate scores 0.42–0.73, comfortably
under the 0.86 accept threshold, so the tool keeps the title and authors it
read from the document instead of inventing a citation. A missing journal
field is recoverable; a wrong DOI is not. Lower the bar with `--accept` if you
disagree.

## What the repository looks like

* **Papers** → `NEW_REPO/Family, Given/<paper title>.pdf`.
  The physical file lives in the **first author's** folder; every co-author
  gets a **relative symlink** to it, so the whole tree stays movable.
* **Books** → `NEW_REPO/Books/<title> (A. Author, B. Author) [year].pdf`
* **Theses** → `NEW_REPO/Thesis/<title> (A. Author) [year].pdf`
* **Anything else** (slides, posters, lecture notes, bank statements) → you are
  asked whether to ignore it or drop it in `NEW_REPO/Unsorted/`.
* `library.bib` sits in the repository root; every entry carries a JabRef
  `file = {:relative/path.pdf:PDF}` field pointing at the copy in the tree.

One person gets one folder even when sources disagree on the spelling:
`Suslin, A.`, `Suslin, Andrei` and `SUSLIN, A. A.` are merged into the richest
form (306 such merges on this corpus), while `Stein, Michael R.` and
`Stein, Yakov` stay apart. An initial that fits two different people is left
alone rather than guessed: with both `Basu, Rabeya` and `Basu, Rikmantra` in
the library, a bare `Basu, R.` keeps its own folder.

## Robustness on a real pile

Things the sample corpus actually contained, and what happens to them:

* **PDFs whose name lost its extension** (`...root systemspdf`, or no extension
  at all) — detected by their `%PDF` header and filed normally; 8 of them here.
  The name is also parsed without letting `Path.stem` cut at the wrong dot.
* **Non-documents** (`.ps`, `.dvi`, `.tex`, `.ppt`) — counted and reported, never
  silently dropped.
* **ALL-CAPS titles and names** from OCR and older journals — restored to normal
  case (`YOU'AN CAO` → `Cao, You'an`, keeping `O'Neill` correct).
* **Letter-spaced running heads and formula fragments** that look like an author
  line (`U NIVERSITA DI P ADOVA`, `factor group $G/G^{0}$`) — rejected.
* **Russian and transliterated titles** that no registry indexes — kept with
  metadata read from the file rather than discarded.
* **An interrupted run** — re-running recognises the files already in place by
  checksum instead of writing `Title (2).pdf` next to each one.

## Duplicates

* **Byte-identical** files (CRC32 over the contents, name ignored) are detected
  and only one copy is placed. The copy with the most informative path wins.
* **Different bytes, same bibliographic entry** — a preprint and the published
  version, or two scans — you are shown both with page count, size and whether
  they have a real text layer, and you choose. The default offered first is the
  one with a genuine text layer and the most pages.

## Options

```
--dry-run          analyse and report, write nothing
--yes              never prompt; take the default for every decision
--unknown ignore   with --yes: leave non-papers alone instead of Unsorted/
--sample N         work on a random sample (--seed for reproducibility)
--only SUBSTR      restrict to paths containing SUBSTR (repeatable)
--offline          no network; metadata comes from the files alone
--no-ocr           never shell out to tesseract
--ocr-pages 1      OCR only the first page (about twice as fast)
--links copy|none  give co-authors a real copy, or no link at all
--accept 0.90      be stricter about automatic matches
--review 0.80      only ask about candidates this good or better
-j N               parallel file readers (default 8)
--no-cache         ignore the cache for this run
```

At every prompt, suffixing your answer with `!` (for example `i!`) applies that
choice to all remaining cases of the same kind.

On a first run over a large pile, OCR dominates the wall clock: roughly 9% of
this corpus has no text layer, and each of those files is rendered at 300 dpi
and passed through tesseract. `--ocr-pages 1` halves that work, `--no-ocr` removes
it entirely (those files then fall back to their names). Everything is cached,
so a second run skips all of it.

arXiv asks callers to leave three seconds between queries, so it is consulted
only when Crossref has not already produced an acceptable match (the Crossref
record is the richer one anyway), and at most once per document.

Every run writes `NEW_REPO/.paperorg/report.csv` (one row per input file:
status, kind, confidence, source, identifier, destination) and caches API
responses and per-file analysis in `NEW_REPO/.paperorg/cache.sqlite3`, so
re-running after an interruption is cheap and makes no repeat network calls.

## DjVu

DjVu is supported when `djvulibre-bin` is installed: `djvutxt` for the text
layer, `ddjvu` + `tesseract` for scans.

It was worth asking whether it pays for itself, as the brief suggested. On this
corpus it does not amount to much — 9 files out of 1196 — so it is deliberately
cheap: the two extra tools are optional, and without them DjVu files are still
collected, classified and filed from their names rather than skipped or
crashing the run. That costs about thirty lines. Building a DjVu-specific
layout parser to match the PDF one would not have been worth it for 0.7% of a
library. `--no-djvu` skips them entirely.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q      # 184 tests
```

The suite passes both with `requests` installed and without it (the urllib
fallback), since both go through one `Http.fetch` seam.

`tests/test_integration.py` builds real PDFs with ghostscript and runs the
whole pipeline offline, so the layout, the symlinks, the deduplication and the
`.bib` are checked against an actual repository on disk rather than mocks.
Several tests are named for the bug they pin down: the directory-hint leak that
made two thirds of the corpus look like slides, the section heading matched as
a title, and the two mathematicians who share the initials `R. Basu`.
