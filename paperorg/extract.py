"""Read whatever we can out of a document file, without any network access.

Everything here is driven by command line tools that ship with poppler /
mupdf / tesseract / djvulibre, so the package itself has no binary
dependencies beyond ``requests``.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import Config, DJVU_EXTS, OTHER_DOC_EXTS, PDF_EXTS, PS_EXTS
from .record import DocFeatures
from .util import crc32_file, removesuffix

XHTML = "{http://www.w3.org/1999/xhtml}"

# Bump when the extracted feature set changes, so that cached
# analyses from an older build are recomputed instead of reused.
EXTRACT_VERSION = 2


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# An unsorted pile contains files whose name lost its extension, or never had
# one ("...root systemspdf", "R. Swan - On stably free modules"). The content
# is what decides, so unrecognised names are sniffed rather than skipped.
_MAGIC_PDF = b"%PDF"
_MAGIC_DJVU = b"AT&TFORM"
_MAGIC_PS = b"%!PS"
_MAGIC_GZIP = b"\x1f\x8b"


def sniff_format(path: Path) -> str:
    """Return "pdf", "djvu", "ps" or "" by looking at the first bytes of the file.

    A gzipped document gives the inner format plus ".gz" ("ps.gz"); a gzipped
    anything-else gives "".
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if head.startswith(_MAGIC_GZIP):
            with gzip.open(path, "rb") as fh:
                inner = _sniff_head(fh.read(24))
            return f"{inner}.gz" if inner else ""
    except (OSError, EOFError):
        return ""
    return _sniff_head(head)


def _sniff_head(head: bytes) -> str:
    if head.startswith(_MAGIC_PDF):
        return "pdf"
    if head.startswith(_MAGIC_DJVU) and head[12:16] in (b"DJVU", b"DJVM", b"DJVI"):
        return "djvu"
    if head.startswith(_MAGIC_PS):
        return "ps"
    return ""


_DOC_EXTS = PDF_EXTS | DJVU_EXTS | PS_EXTS | OTHER_DOC_EXTS


def name_stem(path: Path) -> str:
    """The file name without a *real* extension.

    ``Path.stem`` splits on the last dot, which mangles the many names in an
    unsorted pile that have no extension at all: for
    ``G. Garkusha, I. Panin - Framed motives...`` it would return
    ``G. Garkusha, I``.
    """
    if path.suffix.lower() == ".gz":
        return name_stem(path.with_suffix(""))
    return path.stem if path.suffix.lower() in _DOC_EXTS else path.name


def detect_format(path: Path) -> str:
    """Extension first (cheap), falling back to the file's magic bytes."""
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DJVU_EXTS:
        return "djvu"
    if ext in PS_EXTS:
        return "ps"
    return sniff_format(path)


def readable_copy(path: Path, fmt: str, workdir: Path, key: str,
                  ps_timeout: int = 180) -> tuple[Path | None, str]:
    """A PDF or DjVu that the extractors can read, standing in for *path*.

    Gzipped documents are unpacked and PostScript goes through ps2pdf; the
    results land in *workdir* named by *key* (the input's CRC), so a later run
    finds them there. Returns ``(None, "")`` when *path* is readable as it
    is, and ``(None, reason)`` when it cannot be made readable.
    """
    inner = removesuffix(fmt, ".gz")
    if fmt == inner and inner != "ps":
        return None, ""
    final = workdir / f"{key}.{'pdf' if inner == 'ps' else inner}"
    if final.is_file() and final.stat().st_size > 0:
        return final, ""
    src = path
    if fmt != inner:
        src = workdir / f"{key}.{inner}"
        err = _gunzip(path, src)
        if err:
            return None, err
    if inner == "ps":
        # Bitmap PostScript (scans exported from DjVu) runs to tens of MB and
        # converts slowly, so the budget grows with the size.
        mb = src.stat().st_size >> 20
        err = ps_to_pdf(src, final, timeout=ps_timeout + 20 * mb)
        if src != path:
            src.unlink(missing_ok=True)      # the unpacked .ps was only a step
        if err:
            return None, err
    return final, ""


def _gunzip(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with gzip.open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    except (OSError, EOFError) as exc:
        tmp.unlink(missing_ok=True)
        return f"could not unpack: {exc}"
    os.replace(tmp, dest)
    return ""


def ps_to_pdf(src: Path, dest: Path, timeout: int = 180) -> str:
    """Convert PostScript to PDF with ps2pdf; return "" or an error message.

    The PDF is written next to *dest* first and renamed into place, so an
    interrupted conversion never leaves a truncated file that a later run
    would take for a finished one.
    """
    if dest.is_file() and dest.stat().st_size > 0:
        return ""
    if not have("ps2pdf"):
        return "ps2pdf is not installed"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        p = subprocess.run(["ps2pdf", str(src), str(tmp)], capture_output=True,
                           timeout=timeout, env=_CHILD_ENV)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return f"ps2pdf timed out after {timeout}s"
    except OSError as exc:
        return f"ps2pdf failed: {exc}"
    if p.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        why = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return "ps2pdf failed" + (f": {why[-1]}" if why else "")
    os.replace(tmp, dest)
    return ""


# tesseract parallelises internally with OpenMP. With one worker that is a
# win; with a pool of them every process grabs every core and they spend their
# time fighting each other, so each invocation is pinned to a single thread.
_CHILD_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"}


def _run(cmd: list[str], timeout: int = 60) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           env=_CHILD_ENV)
        return p.stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return ""


# --------------------------------------------------------------- identifiers

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>,;]{3,})", re.I)
ARXIV_OLD = re.compile(r"arXiv[:\s]*([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
ARXIV_NEW = re.compile(r"arXiv[:\s]*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
ARXIV_BARE = re.compile(r"\b(\d{4}\.\d{4,5})v\d+\b")
MR_RE = re.compile(r"\bMR\s?#?\s?(\d{6,7})\b")
ISBN_RE = re.compile(r"\bISBN[:\s]*((?:97[89][-\s]?)?[\d][\d\-\s]{8,15}[\dXx])\b")

_DOI_TAIL = re.compile(r"(?i)\.(pdf|html?|xml|full|abstract)$")


def clean_doi(raw: str) -> str:
    """DOIs extracted from a text layer pick up trailing punctuation and glue."""
    d = raw.strip().rstrip(".,;:)]}'\"")
    # A DOI never contains these; text extraction often concatenates them.
    for stop in ("http", "www.", "arXiv", "  "):
        i = d.find(stop)
        if i > 0:
            d = d[:i]
    d = _DOI_TAIL.sub("", d)
    d = d.rstrip(".,;:)]}'\"")
    # Reject obvious truncation such as "10.1016/j".
    if not re.match(r"^10\.\d{4,9}/\S{3,}$", d):
        return ""
    return d


def find_identifiers(text: str) -> tuple[str, str, str, str]:
    """Return (arxiv_id, doi, mr, isbn) found in *text*."""
    arxiv = ""
    m = ARXIV_OLD.search(text) or ARXIV_NEW.search(text)
    if m:
        arxiv = m.group(1)
    elif (m := ARXIV_BARE.search(text)):
        arxiv = m.group(1)

    doi = ""
    for cand in DOI_RE.finditer(text):
        doi = clean_doi(cand.group(1))
        if doi:
            break

    mr = (m.group(1) if (m := MR_RE.search(text)) else "")
    isbn = ""
    if (m := ISBN_RE.search(text)):
        isbn = re.sub(r"[-\s]", "", m.group(1))
    return arxiv, doi, mr, isbn


# ------------------------------------------------------------ file name parse

PARTICLES = r"(?:van|von|der|den|de|di|da|dos|del|della|la|le|el|al|ibn|bin|ten|ter|op|zu|of)"
_SURNAME = r"[A-ZА-ЯЁ][-'\u2019A-Za-zА-Яа-яёЁ]+"
_GIVEN = (
    r"(?:[A-ZА-ЯЁ]\.?[-\u2010\u2013]?\s*"      # initials: "A.", "J.-P."
    r"|[A-ZА-ЯЁ][a-zа-яё\u2019']+\.?\s+)*"      # or spelled out: "Jean-Pierre"
)
AUTHOR_TOKEN = re.compile(rf"^{_GIVEN}(?:{PARTICLES}\s+)*{_SURNAME}$")
ETAL = re.compile(r"(?i)^et\.?\s?al\.?$")
_ETAL_SUFFIX = re.compile(r"(?i)\s+et\.?\s?al\.?$")

# Spelled-out given names mean a title-cased title could masquerade as an
# author list ("Symmetric Bilinear Forms - notes"). Two structural rules keep
# that out without maintaining a vocabulary of mathematical nouns:
#   * a name written without initials has at most two words ("Shingo Murakami");
#   * lower-case words are never tolerated as a stray token.
_INITIAL = re.compile(r"\b[A-ZА-ЯЁ]\.")
_PARTICLE_WORD = re.compile(rf"(?i)^{PARTICLES}$")


def _name_shape_ok(token: str) -> bool:
    """Reject "Symmetric Bilinear Forms" while keeping "Shingo Murakami"."""
    words = [w for w in token.split() if not _PARTICLE_WORD.match(w)]
    if len(words) > 4:
        return False
    if not _INITIAL.search(token) and len(words) > 2:
        return False
    return True
# "Vavilov- Relative Subgroups" (no space before the dash) is common too.
_SEP = re.compile(
    r"\s+[-\u2013\u2014]\s+"                      # " - "
    r"|\s+[-\u2013\u2014](?=[A-ZА-ЯЁ])"            # " -Title"
    r"|(?<=[a-zа-яё\u2019'])[-\u2013\u2014]\s+(?=[A-ZА-ЯЁ])"   # "Name- Title"
)
# A glued "pdf"/"djvu" is safe to strip; a glued "ps" is not - plenty of
# titles end in "groups".
_TRAILING_EXT = re.compile(r"(?i)(?:\.(?:pdf|djvu|djv|ps|dvi)|pdf|djvu)$")


def parse_filename(stem: str) -> tuple[str, str]:
    """Split ``A. Bak, N. Vavilov - Presenting powers`` into (authors, title).

    Returns ``("", title)`` when the stem does not look like an author list
    followed by a title.
    """
    name = re.sub(r"\s*\[[^\]]*\]\s*", " ", stem)               # [slides], [2004]
    name = re.sub(r"(?i)\s*\((?:copy|\d{1,2}|\d{4})\)\s*$", "", name)
    # Strip a trailing "(final)" / "draft" / "v2", but only when something
    # substantial is left: "vdk-v2" must not collapse to "vdk-".
    trimmed = re.sub(r"(?i)[-_\s]*\(?(?:final|draft|preprint|v\d+)\)?\s*$", "", name)
    if len(trimmed) >= 6:
        name = trimmed
    name = name.strip(" -_")
    name = _TRAILING_EXT.sub("", name)                            # "...pdf.pdf"
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    parts = _SEP.split(name, 1)
    if len(parts) != 2:
        return "", name
    left, right = parts[0].strip(" ,"), _TRAILING_EXT.sub("", parts[1].strip())
    if not right or len(left) > 95 or len(right) < 4:
        return "", name
    tokens = [t.strip() for t in re.split(r",| and | & |;", left) if t.strip()]
    if not tokens or len(tokens) > 8:
        return "", name
    # "A. Rapinchuk et al" arrives as one token; judge the name part alone.
    stripped = [_ETAL_SUFFIX.sub("", t) for t in tokens]
    if not all(_name_shape_ok(t) for t in stripped if t):
        return "", name
    good, odd = 0, 0
    for t in stripped:
        if not t or AUTHOR_TOKEN.match(t) or ETAL.match(t):
            good += 1
        elif t[:1].isupper():
            odd += 1            # a typo or an unusual name: tolerate one
        else:
            return "", name     # a lower-case word is never part of a name
    if good >= 1 and odd <= (1 if len(tokens) > 1 else 0):
        return left, right
    return "", name


# ------------------------------------------------------------- pdf structure

_HEADER_JUNK = re.compile(
    r"(?i)^\s*(?:\u00a7\s*)?(?:\d+(?:\.\d+)*[.)]?\s+)?"       # "1.", "§ 2.3"
    r"(abstract|introduction|contents|table of contents|references|"
    r"bibliography|acknowledge?ments?|preliminaries|notation|"
    r"arxiv[:\s]|journal|vol\.?\s*\d|volume\s*\d|"
    r"no\.?\s*\d|pp?\.\s*\d|copyright|\u00a9|received|revised|communicated|"
    r"doi[:\s]|issn|isbn|printed in|all rights|www\.|https?:|preprint|"
    r"to appear|submitted|draft|chapter\s+\d|section\s+\d|[\d\s.,;:()\[\]/-]+)\s*$"
)


def page_lines(path: Path, page: int = 1, timeout: int = 60) -> list[dict]:
    """Lines of one page with their height (a proxy for font size)."""
    out = _run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-bbox-layout", "-q", str(path), "-"],
        timeout=timeout,
    )
    if not out.strip():
        return []
    try:
        root = ET.fromstring(out)
    except ET.ParseError:
        return []
    lines: list[dict] = []
    for ln in root.iter(XHTML + "line"):
        words = [(w.text or "").strip() for w in ln.iter(XHTML + "word")]
        text = " ".join(w for w in words if w)
        if not text:
            continue
        try:
            y0, y1 = float(ln.get("yMin", 0)), float(ln.get("yMax", 0))
            x0 = float(ln.get("xMin", 0))
        except ValueError:
            continue
        lines.append({"h": round(y1 - y0, 2), "y": y0, "x": x0, "t": text})
    return lines


def extract_title_authors(lines: list[dict]) -> tuple[str, str]:
    """Pick the title (largest type in the upper page) and the line(s) under it."""
    if not lines:
        return "", ""
    page_bottom = max(l["y"] for l in lines) or 1.0
    top = [l for l in lines if l["y"] <= page_bottom * 0.66] or lines
    biggest = max(l["h"] for l in top)
    if biggest <= 0:
        return "", ""

    # Group vertically adjacent lines set in (nearly) the largest type.
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for l in top:
        big = l["h"] >= biggest * 0.92
        junk = _HEADER_JUNK.match(l["t"])
        if big and not junk:
            if cur and abs(l["y"] - cur[-1]["y"]) > 3.0 * max(l["h"], 1.0):
                chunks.append(cur)
                cur = []
            cur.append(l)
        elif cur:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    chunks = [c for c in chunks if c]
    if not chunks:
        return "", ""

    best = max(chunks, key=lambda c: sum(len(l["t"]) for l in c))
    title = " ".join(l["t"] for l in best)
    # Strip footnote marks, but never trailing digits: "Injective stability
    # for K2" and "Chevalley groups I" end in meaningful characters.
    title = re.sub("[\\*\u2020\u2021\u00a7\u00b6\u2217]+\\s*$", "", title).strip()
    title = re.sub(r"\s+", " ", title)
    if not (6 <= len(title) <= 300):
        return "", ""

    # Authors: the next few lines below the title block, before the abstract.
    y_end = max(l["y"] for l in best)
    # Search the whole page below the title: on a sparse title page the "top
    # band" used to find the title can end above the author line.
    below = [l for l in lines if l["y"] > y_end][:6]
    authors: list[str] = []
    for l in below:
        t = l["t"].strip()
        if _ABSTRACT_START.match(t):
            break
        t = re.sub(r"(?i)^(by|von|par)\s+", "", t)
        if not plausible_author_line(t):
            continue
        authors.append(t)
        if len(" ".join(authors)) > 90:
            break
    return title, " ".join(authors)[:200]


_ABSTRACT_START = re.compile(
    "(?i)^(abstract|r[\u00e9\u00e8]sum[\u00e9\u00e8]|zusammenfassung|introduction|"
    "contents|keywords|1[.\\s]|\u00a7|\u0430\u043d\u043d\u043e\u0442\u0430\u0446\u0438\u044f)")
_AFFILIATION = re.compile(
    "(?i)(universit|institut|department|dipartimento|e-?mail|@|street|strasse|"
    "academy|laborator|facult|cnrs|p\\.o\\. ?box|school of|college|centre|center|"
    "\\bvia\\b|\\bstr\\.|subject classification|key ?words|"
    "received|accepted|revised|copyright|all rights|proceedings of|"
    "\u0443\u043d\u0438\u0432\u0435\u0440\u0441\u0438\u0442\u0435\u0442|"
    "\u0438\u043d\u0441\u0442\u0438\u0442\u0443\u0442|\u043a\u0430\u0444\u0435\u0434\u0440\u0430)")


def plausible_author_line(t: str) -> bool:
    """Reject running heads, affiliations and OCR debris posing as authors.

    The false positives this rules out were seen on the sample corpus:
    letter-spaced journal headers ("U NIVERSITA DI P ADOVA Y U C HEN") and
    stray formula fragments ("factor group $G/G^{0}$; . . the type of").
    """
    if not (3 <= len(t) <= 160):
        return False
    if _AFFILIATION.search(t):
        return False
    if re.search(r"[${}\\^_|<>]|\.\s*\.", t):          # maths, or dot leaders
        return False
    words = t.split()
    if not words or len(words) > 14:
        return False
    # Letter-spaced OCR output produces many one-character "words".
    if sum(1 for w in words if len(w.strip(".")) == 1) > max(2, len(words) // 3):
        return False
    if sum(1 for c in t if c.isdigit()) > len(t) * 0.2:
        return False
    # At least one properly capitalised word that is not a lone initial.
    return any(len(w) > 1 and w[:1].isupper() and w[1:2].islower() for w in words)


def pdf_info(path: Path, timeout: int = 40) -> dict:
    out = _run(["pdfinfo", str(path)], timeout=timeout)
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip().lower()] = v.strip()
    res = {
        "pages": int(info["pages"]) if info.get("pages", "").isdigit() else 0,
        "producer": info.get("producer", ""),
        "creator": info.get("creator", ""),
        "meta_title": info.get("title", ""),
        "meta_author": info.get("author", ""),
        "width": 0.0,
        "height": 0.0,
    }
    if (m := re.match(r"([\d.]+) x ([\d.]+)", info.get("page size", ""))):
        res["width"], res["height"] = float(m.group(1)), float(m.group(2))
    return res


def pdf_text(path: Path, pages: int = 3, timeout: int = 60) -> str:
    return _run(["pdftotext", "-f", "1", "-l", str(pages), "-q", str(path), "-"], timeout=timeout)


def ocr_pdf(path: Path, cfg: Config) -> str:
    """OCR the first pages of a scan. Returns "" when tesseract is unavailable."""
    if not (cfg.ocr and have("tesseract") and have("pdftoppm")):
        return ""
    with tempfile.TemporaryDirectory(prefix="paperorg-ocr-") as tmp:
        prefix = os.path.join(tmp, "pg")
        _run(["pdftoppm", "-r", str(cfg.ocr_dpi), "-gray", "-f", "1",
              "-l", str(cfg.ocr_max_pages), "-png", str(path), prefix],
             timeout=cfg.ocr_timeout)
        chunks = []
        for img in sorted(Path(tmp).glob("pg*.png")):
            txt = _run(["tesseract", str(img), "stdout", "-l", "eng+rus", "--psm", "1"],
                       timeout=cfg.ocr_timeout)
            chunks.append(txt)
        return "\n".join(chunks)


# ----------------------------------------------------------------- djvu path

def djvu_text(path: Path, timeout: int = 60) -> str:
    if have("djvutxt"):
        return _run(["djvutxt", "--page=1-3", str(path)], timeout=timeout)
    return ""


def djvu_info(path: Path, timeout: int = 40) -> dict:
    res = {"pages": 0, "producer": "djvu", "creator": "", "meta_title": "",
           "meta_author": "", "width": 0.0, "height": 0.0}
    if not have("djvused"):
        return res
    out = _run(["djvused", "-e", "n", str(path)], timeout=timeout).strip()
    if out.isdigit():
        res["pages"] = int(out)
    return res


def djvu_ocr(path: Path, cfg: Config) -> str:
    """Render DjVu pages to images and OCR them (needs ddjvu + tesseract)."""
    if not (cfg.ocr and have("ddjvu") and have("tesseract")):
        return ""
    with tempfile.TemporaryDirectory(prefix="paperorg-djvu-") as tmp:
        chunks = []
        for pg in range(1, cfg.ocr_max_pages + 1):
            img = os.path.join(tmp, f"p{pg}.pgm")
            _run(["ddjvu", "-format=pgm", f"-page={pg}",
                  f"-scale={cfg.ocr_dpi}", str(path), img],
                 timeout=cfg.ocr_timeout)
            if os.path.exists(img):
                chunks.append(_run(["tesseract", img, "stdout", "-l", "eng+rus", "--psm", "1"],
                                   timeout=cfg.ocr_timeout))
        return "\n".join(chunks)


# --------------------------------------------------------------- entry point

def analyse(path: Path, cfg: Config, doc: Path | None = None) -> DocFeatures:
    """Collect every local signal we can get from one file.

    *doc* is the file actually read when it differs from the input file (the
    PDF converted from a PostScript original); the name and folder hints
    still come from *path*.
    """
    feat = DocFeatures(path=path)
    try:
        feat.size = path.stat().st_size
        feat.crc32 = crc32_file(path)
    except OSError as exc:
        feat.error = f"unreadable: {exc}"
        return feat

    src = doc or path
    feat.fmt = detect_format(src)
    is_djvu = feat.fmt == "djvu"

    try:
        info = djvu_info(src) if is_djvu else pdf_info(src)
    except Exception as exc:                                    # pragma: no cover
        info, feat.error = {}, f"info failed: {exc}"
    for k, v in (info or {}).items():
        setattr(feat, k, v)

    # 1. text layer
    text = djvu_text(src) if is_djvu else pdf_text(src)
    # 2. fall back to OCR for scans
    if len(text.strip()) < 220:
        ocr = djvu_ocr(src, cfg) if is_djvu else ocr_pdf(src, cfg)
        if len(ocr.strip()) > len(text.strip()):
            text, feat.ocr_used = ocr, True
    feat.text = text
    feat.text_chars = len(text.strip())

    # 3. layout-aware title/authors (PDF only - DjVu has no bbox extractor)
    if not is_djvu:
        try:
            feat.page_title, feat.page_authors = extract_title_authors(page_lines(src))
        except Exception:
            pass
    if not feat.page_title and feat.ocr_used:
        feat.page_title, feat.page_authors = _title_from_plain(text)
    if is_djvu and text:
        feat.page_title, feat.page_authors = _title_from_plain(text)

    # 4. file name and directory hints
    feat.file_authors, feat.file_title = parse_filename(name_stem(path))
    # Only folders *inside* the input tree say anything about the document;
    # the absolute prefix (/home/<user>/Documents/...) must never leak in.
    try:
        rel_parts = path.parent.relative_to(cfg.input_dir).parts
    except ValueError:
        rel_parts = path.parent.parts[-2:]
    feat.dir_hints = [p.lower() for p in rel_parts[-4:]]

    # 5. identifiers printed in the document
    feat.arxiv_id, feat.doi, feat.mr, feat.isbn = find_identifiers(text)
    return feat


def _title_from_plain(text: str) -> tuple[str, str]:
    """Crude title guess for OCR/DjVu output that carries no layout data."""
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and not _HEADER_JUNK.match(l)]
    for i, l in enumerate(lines[:14]):
        words = l.split()
        if 3 <= len(words) <= 22 and len(l) >= 14 and not l.endswith((".", ",")):
            authors = " ".join(lines[i + 1:i + 3])[:120] if i + 1 < len(lines) else ""
            return l, authors
    return "", ""


# ------------------------------------------------------------ thesis school

_INSTITUTION_WORD = re.compile(
    "(?i)(universit|hochschule|\u00e9cole|ecole|politecnico|polytechnic|"
    "institute of technology|state university|academy of sciences)")
_INSTITUTION_WEAK = re.compile(
    "(?i)(institut|college|school of mathematic|faculty of|department of)")


def extract_school(text: str) -> str:
    """Name the degree-granting institution from a thesis title page.

    Theses rarely reach a registry, so their ``school`` field would otherwise
    stay empty - and a publisher is not a school. Title pages put the
    institution on a line of its own, so the line *is* the answer; trying to
    parse the name out of the surrounding prose is less reliable, not more.
    """
    strong, weak = "", ""
    for raw in text[:4000].splitlines():
        line = " ".join(raw.split()).strip(" ,.;:-\u2013\u2014")
        if not (6 <= len(line) <= 80):
            continue
        if sum(1 for c in line if c.isdigit()) > 2:
            continue
        letters = sum(1 for c in line if c.isalpha())
        if letters < 0.7 * len(line):
            continue
        if len(line.split()) > 8:
            continue
        if _INSTITUTION_WORD.search(line):
            if not strong or len(line) > len(strong):
                strong = line
        elif _INSTITUTION_WEAK.search(line) and not weak:
            weak = line
    return strong or weak
