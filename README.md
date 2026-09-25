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
sudo apt install ghostscript                   # optional: PostScript (ps2pdf)
pip install requests                           # optional: faster HTTP than urllib

./organize.py input/ library/ --sample 50 -n   # dry run on 50 random files
./organize.py input/ library/                  # the real thing, interactive
```

Nothing but `poppler-utils` is mandatory, and the only Python dependency is
`requests` (it falls back to `urllib` if absent). A dry run writes no
documents — only the plan in `library/.paperorg/` (`report.txt` and `report.csv`).

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
(preprints), **zbMATH** (Zbl number, MSC codes), **OpenLibrary** (books),
**Math-Net.Ru** (Russian journals, for Cyrillic titles).

> **MR numbers.** MathSciNet has no free API, so `mrnumber` is filled only when
> the number is printed in the document itself, appears in Crossref's
> `alternative-id`, or is linked from the Math-Net.Ru page. zbMATH's `zbl` field
> and MSC codes are always attempted and serve the same purpose for most
> workflows.

MSC codes go into each entry twice: `mrclass = {20G35 (14L15 13D15)}` in
MathSciNet's form (primary, then secondary), which JabRef and biblatex styles
can sort and filter on, and `keywords = {MSC: 20G35, 14L15, 13D15}`.

### Russian-language papers

Crossref and arXiv hold at best the English *translation* of a Russian paper,
under a title the file does not contain. A document whose title is in Cyrillic
is therefore looked up on [Math-Net.Ru](https://www.mathnet.ru) first, the
Steklov Institute's index of the Russian journals (Izvestiya, Algebra i Analiz,
Zapiski POMI/LOMI, Uspekhi, ...). There is no API: the search form and the
paper pages are parsed, one request per second, all cached. A match gives

* the entry for the **Russian original** (the file is the original): Cyrillic
  title and journal, volume, pages, `language = {russian}`, `doi` only when the
  original has one (Math-Net.Ru's own `10.4213/...`);
* author names in their **Latin** spelling from the English page, so that
  `Н. А. Вавилов` lands in the same folder as `N. A. Vavilov`;
* MR number, Zbl id, MSC codes (from zbMATH through the translation's DOI when
  Math-Net.Ru lists none);
* `note = {English translation: “...”, St. Petersburg Math. J., 23:3 (2012),
  603–635, doi:...}`.

Conference volumes, preprints and books are mostly not on Math-Net.Ru, and a
file name that is a transliteration (`Avtomorfizmy linejnyh grupp...`) is not
turned back into Cyrillic. `--no-mathnet` switches it off.

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
* **PostScript** (`.ps`, or a `%!PS` header under any name) — converted with
  `ps2pdf` into `NEW_REPO/.paperorg/converted/`, read like any other PDF, and
  the PDF is what lands in the repository. `--no-ps` skips them instead.
* **Gzipped documents** (`.ps.gz`, `.pdf.gz`, `.djvu.gz`, or a bare `.gz`
  whose content is one of these) — unpacked into the same folder (and
  PostScript then converted), so the repository gets a plain PDF or DjVu. A
  gzipped `.tex` or anything else is listed as not a document.
* **macOS resource forks** (`._Name.pdf`) — not copies of `Name.pdf`; listed
  as not documents.
* **Non-documents** (`.dvi`, `.tex`, `.ppt`) — listed in `report.txt`, never
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
--no-ps            skip PostScript instead of converting it with ps2pdf
--no-mathnet       do not look Cyrillic titles up on Math-Net.Ru
--on-rate-limit skip   carry on without a registry that refuses requests
                   (default: stop, and resume by running the same command)
--arxiv-titles auto    send arXiv title searches without review first
--arxiv-titles off     never search arXiv by title (identifiers still work)
--arxiv-delay SEC  seconds between arXiv requests (default 30, minimum 3)
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

### arXiv: identifiers in batches, title searches after review

arXiv only answers about one request every half minute without blocking the
address, so it is used sparingly:

* **arXiv ids printed in the documents** are looked up before anything else,
  fifty to a request (a few minutes for hundreds of ids, not hours).
* **Title searches** come last, only for documents Crossref, zbMATH and
  Math-Net.Ru could not identify, and only when the title is worth a query.
  A single rule set (`paperorg/arxivguard.py`) turns away body text taken
  for a title, file-name slugs, journal names and running heads, copyright
  and cover-page stamps, section headings, OCR debris, Russian documents, and
  papers published before 1992 (the year counts only where a paper states
  its own date: journal line, copyright, "Received", the file name, a
  registry record for the same authors; "1991 Mathematics Subject
  Classification" is a scheme, not a date). A query uses the first author's
  surname and the title's most distinctive words, e.g.
  `au:Tate AND ti:symbols AND ti:arithmetic`, not an exact phrase.
* Some rejected "titles" are better evidence elsewhere and are sent there
  instead: a JSTOR cover page (its stable id is a DOI, `10.2307/…`), an
  Elsevier PII, a journal citation line such as `Advances in Mathematics
  109, 108-139 (1994)` (matched on volume and first page), a Russian title
  typed in Latin letters (Math-Net.Ru, transliterated back).

By default (`--arxiv-titles review`) the title searches are not sent at
once. The run looks everything else up, writes every proposed search to
`NEW_REPO/.paperorg/arxiv-queries.tsv`, and stops before copying or asking
anything (exit code 75, as for a rate limit). In the first column, replace
`y?` / `n?` (the suggestions) with `y` or `n`. The query, title and authors
columns can be edited, and an emptied query is rebuilt from title and
authors. `passed_over` says why other title candidates were rejected, and
`scholar` links to a Google Scholar search for checking by hand. Certain
skips are listed as plain `n`, so they can still be overturned. Then run the
same command again: it sends the `y` rows, records each outcome in the
`result` column, and finishes. Rows keep your decisions across runs.

### When a registry says "too many requests"

arXiv's documented limit is one request every three seconds over a single
connection. In practice that pace still drew a block, so the tool leaves 30
seconds (`--arxiv-delay`, never below 3) after the previous answer arrived,
over one connection at a time. Beyond that arXiv blocks an
address outright with HTTP 429 for a cool-off it does not announce (no
`Retry-After`; more than an hour when it happened here), and every further
request keeps the block alive. A longer block comes back as an empty HTTP 406
on every request, from that address only. So after three 429s in a row from one
host, or a single 406 or 403, the tool stops asking it and, by default, **stops
the run** after the lookups already in flight: nothing is copied, no question
is asked, and `NEW_REPO/.paperorg/RESUME.txt` says what happened and gives the
command to run again. If a registry does send `Retry-After`, the message and
`RESUME.txt` say when it asked to be tried again. The exit code is 75 ("try
again later"), for scripts.

The code runs on Python 3.8 and later.

Resuming is running the same command again. File analyses, registry answers
and your answers to prompts are all cached, so it continues where it stopped
(and skips the "output folder exists" question). `--on-rate-limit skip`
finishes without the refusing registry instead; the affected documents are
tagged `[lookup incomplete: export.arxiv.org]` in `report.txt`, and a later run
fills them in. A definite "not found" (HTTP 404) is cached; a refusal never is.

Every run writes two reports into `NEW_REPO/.paperorg/`:

* `report.txt`, for reading: everything that was *not* filed normally. That
  means files skipped (read errors, failed `ps2pdf`, ignored at a prompt),
  documents not recognised and put in `Unsorted/`, documents filed with metadata
  read from the file because no registry matched, duplicates that were not
  placed, and non-documents left in the input folder, grouped by extension.
* `report.csv`, one row per input file (status, kind, confidence, source,
  identifier, destination).

It also caches API
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
