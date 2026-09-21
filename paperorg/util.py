"""Small shared helpers: text normalisation, similarity, filenames, checksums."""
from __future__ import annotations

import difflib
import re
import unicodedata
import zlib
from pathlib import Path

# ---------------------------------------------------------------- text utils

_COMBINING = dict.fromkeys(
    i for i in range(0x110000) if unicodedata.combining(chr(i))
)

# TeX-isms and typographic noise that show up in extracted titles.
_TEX_MACROS = re.compile(r"\\[a-zA-Z]+\s*|[{}$]")
_WS = re.compile(r"\s+")


def strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).translate(_COMBINING)


def norm(s: str | None) -> str:
    """Aggressive normalisation used for comparing titles/names."""
    if not s:
        return ""
    s = strip_accents(unicodedata.normalize("NFKC", s)).lower()
    s = _TEX_MACROS.sub(" ", s)
    s = re.sub(r"[^a-z0-9\u0400-\u04ff ]+", " ", s)
    return _WS.sub(" ", s).strip()


def similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity between two titles, robust to subtitle/punctuation drift."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    # A short candidate fully contained in a longer one (subtitle dropped, or
    # "Title, I" vs "Title") should not be punished too harshly.
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 12 and short in long:
        ratio = max(ratio, 0.90 + 0.10 * len(short) / len(long))
    return ratio


def token_set_ratio(a: str, b: str) -> float:
    """Order-insensitive word overlap, useful for author lists."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_SMALL_WORDS = {
    "a", "an", "the", "and", "but", "or", "nor", "for", "of", "on", "in", "to",
    "from", "by", "at", "as", "with", "over", "under", "into", "via", "vs",
    "is", "are", "be", "its", "their", "some", "not",
}


def smart_title_case(s: str) -> str:
    """Rescue an ALL-CAPS title (common in OCR and older journals).

    Title case rather than sentence case, because it capitalises the proper
    nouns that pepper mathematical titles - Lie, Chevalley, Hopf - which
    sentence case would silently destroy.
    """
    words = s.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        core = low.strip(".,;:()[]")
        if i and core in _SMALL_WORDS and i != len(words) - 1:
            out.append(low)
        else:
            out.append("-".join(p[:1].upper() + p[1:] for p in low.split("-")))
    return " ".join(out)


def is_shouting(s: str) -> bool:
    """True when a string is (almost) entirely upper case."""
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 8:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.9


def titlecase_preserving(s: str) -> str:
    """Title-case a string but keep existing ALLCAPS acronyms and math tokens."""
    out = []
    for w in s.split():
        if w.isupper() and len(w) > 1:
            out.append(w)
        elif any(c.isdigit() for c in w) or "_" in w or "^" in w:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


# ------------------------------------------------------------ filename utils

# Characters that are illegal or merely painful in file names.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOTS = re.compile(r"\.+$")


def safe_filename(name: str, max_bytes: int = 180) -> str:
    """Turn an arbitrary title into a portable, readable file name.

    Keeps Unicode (these are mathematicians' names), drops path separators and
    control characters, collapses whitespace and truncates on a word boundary
    so the result stays below the ext4/HFS 255-byte component limit even after
    an extension and a ``(2)`` disambiguator are appended.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("/", "-").replace("\\", "-")
    # A colon is legal on ext4 but trips up JabRef paths, Windows shares and
    # rsync-to-exFAT backups, so spell it out rather than dropping it.
    name = name.replace(": ", " - ").replace(":", "-")
    name = _ILLEGAL.sub("", name)
    name = _WS.sub(" ", name).strip(" .")
    name = _DOTS.sub("", name)
    if not name:
        name = "untitled"
    while len(name.encode("utf-8")) > max_bytes:
        cut = name.rsplit(" ", 1)[0]
        name = cut if cut and cut != name else name.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return name.strip(" .") or "untitled"


# Cyrillic -> Latin, good enough for BibTeX keys (which must be ASCII to be
# portable across bibtex/biber and the styles that mangle non-ASCII).
_CYR = {
    "\u0430": "a", "\u0431": "b", "\u0432": "v", "\u0433": "g", "\u0434": "d",
    "\u0435": "e", "\u0451": "e", "\u0436": "zh", "\u0437": "z", "\u0438": "i",
    "\u0439": "i", "\u043a": "k", "\u043b": "l", "\u043c": "m", "\u043d": "n",
    "\u043e": "o", "\u043f": "p", "\u0440": "r", "\u0441": "s", "\u0442": "t",
    "\u0443": "u", "\u0444": "f", "\u0445": "kh", "\u0446": "ts", "\u0447": "ch",
    "\u0448": "sh", "\u0449": "shch", "\u044a": "", "\u044b": "y", "\u044c": "",
    "\u044d": "e", "\u044e": "yu", "\u044f": "ya",
    "\u0456": "i", "\u0457": "i", "\u0454": "ie", "\u0491": "g",
}


def translit(s: str) -> str:
    """Fold a string to ASCII letters, transliterating Cyrillic."""
    out = []
    for ch in strip_accents(s or "").lower():
        out.append(_CYR.get(ch, ch))
    return "".join(c for c in "".join(out) if c.isascii())


# Some registries sentence-case titles so aggressively that eponyms come back
# in lower case ("on adjoint chevalley groups ... euclidean lie algebras").
# In a mathematics library that reads as an error, so a curated list of names
# common in this subject is restored. Extend it freely; nothing depends on it
# being complete.
_EPONYMS = {
    "abelian", "artinian", "azumaya", "banach", "beilinson", "bloch", "borel",
    "bruhat", "brauer", "cartan", "chevalley", "chow", "clifford", "coxeter",
    "dedekind", "dynkin", "euclidean", "frobenius", "galois", "gersten",
    "grothendieck", "hausdorff", "heisenberg", "hermitian", "hilbert", "hopf",
    "jacobian", "jordan", "karoubi", "kazhdan", "laurent", "lefschetz", "levi",
    "milnor", "minkowski", "morita", "nisnevich", "noetherian", "picard",
    "pfister", "poincare", "quillen", "riemann", "schur", "segal", "serre",
    "steinberg", "suslin", "swan", "sylow", "tits", "vaserstein", "voevodsky",
    "waldhausen", "weil", "weyl", "witt", "zariski",
}
# "lie" and "bass" are ordinary words too, so they need a following noun.
_CONDITIONAL_EPONYMS = {
    "lie": {"algebra", "algebras", "group", "groups", "ring", "rings",
            "theory", "bracket", "subalgebra", "subalgebras", "superalgebra",
            "superalgebras", "ideal", "ideals"},
    "bass": {"conjecture", "theorem", "number", "numbers", "modules"},
}


def restore_eponyms(title: str) -> str:
    """Undo a registry's over-eager sentence casing of proper names."""
    out = title.split(" ")
    for i, w in enumerate(out):
        core = w.strip(".,;:()[]?!'\u2019")
        low = core.lower()
        if not core or not core.islower():
            continue
        nxt = ""
        if i + 1 < len(out):
            nxt = out[i + 1].strip(".,;:()[]?!'\u2019").lower()
        hit = low in _EPONYMS or nxt in _CONDITIONAL_EPONYMS.get(low, set())
        if hit:
            out[i] = w.replace(core, core[:1].upper() + core[1:], 1)
    return " ".join(out)



def crc32_file(path: Path, chunk: int = 1 << 20) -> str:
    """CRC32 of file *contents* (name and mtime deliberately excluded)."""
    acc = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            acc = zlib.crc32(block, acc)
    return f"{acc & 0xFFFFFFFF:08x}"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def unique_path(path: Path) -> Path:
    """Return *path* or the first free ``name (2).ext`` style variant."""
    if not path.exists() and not path.is_symlink():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(2, 1000):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists() and not cand.is_symlink():
            return cand
    raise RuntimeError(f"cannot find a free name near {path}")
