# lab — the measurements behind the design

Throwaway scripts used to decide how documents should be identified, kept
because the numbers in the main README came from them.

* `survey.py` — samples the corpus and reports how many files have a text
  layer, an embedded arXiv id or DOI, and an `Authors - Title` file name.
  Result: 91% have text, 38% carry an identifier, 93% of names parse.
* `proto.py` — first identification attempt: file name → Crossref/arXiv.
  67% resolved.
* `proto2.py` — adds layout-aware title extraction via
  `pdftotext -bbox-layout`. 73% resolved, and every remaining miss turned out
  to be a slide deck, a poster, a book or a Russian-language original — which
  is what led to classifying documents *before* looking them up.

Run them against `../input` with `python3 <script>.py [N]`.
