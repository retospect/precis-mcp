"""Locator normalization + the ``searchSnip`` contract.

The published provenance graph carries **universal anchors only**: DOI +
``pdf_sha256`` + verbatim quote + a short normalized snip that locates
the passage in *any* copy of the paper (chunking is our segmentation,
never a fact about the paper). The snip doubles as the PDF deep-link
query (``/pdf/<sha256>?search=<snip>``), so it must survive any URL
encoding: **lowercase ASCII tokens, letters/digits/hyphens only.**

Normalization (:func:`normalize_text`) is the chunk-navigation anchoring
trick from the spec: casefold, strip soft hyphens, unfold ligatures,
collapse whitespace. Both sides of every comparison — quote-in-chunk
containment and snip uniqueness — run on this normalized form, so PDF
extraction artifacts (line-break hyphenation, ﬁ-ligatures, case) don't
break verbatim checks.

Uniqueness (:func:`count_matches`) is validated against the paper's
stored chunk text **at mint time**: no unique match → mint fails
("no source, no atom"). A wrong snip is a locator inconvenience; a wrong
*quote* is an integrity issue — hence the quote is part of what the
signature covers and the snip is merely re-derivable.
"""

from __future__ import annotations

import re
import unicodedata

_SOFT_HYPHEN = "­"
#: Ligature unfolding beyond NFKD (NFKD handles ﬁﬂﬀﬃﬄ already; œ/æ do
#: not decompose under NFKD, and both appear in PDF text extractions).
_LIGATURES = {
    "œ": "oe",
    "Œ": "OE",
    "æ": "ae",
    "Æ": "AE",
}
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: Simple markup :func:`fold_markup` strips on both sides of every match —
#: a chunk's ``**2350**`` and a quote's plain ``2350`` fold to the same
#: thing. Conservative on purpose (only shapes seen in stored chunk text;
#: lifted from ``taproot/reground.py``, which imports them from here):
#: ``<sup>X</sup>``/``<sub>X</sub>`` -> Unicode super/subscript ``X``, ``[text](url-or-#anchor)`` ->
#: ``text`` (the link text may hold backslash-escaped brackets, as in the
#: ``[\[22\]](#page-11-0)`` citation links marker-ingest emits), and
#: ``**x``/``*x*`` emphasis dropped. A bare ``[...]`` not followed by ``(...)``
#: is left alone.
SUP_SUB_RE = re.compile(r"(?is)<(sup|sub)>(.*?)</\1>")
MD_LINK_RE = re.compile(r"\[((?:\\.|[^\]\\])*)\]\([^)]*\)")
_ADJ_SUP_RE = re.compile(r"(?i)</sup><sup>")
_ADJ_SUB_RE = re.compile(r"(?i)</sub><sub>")
_ESCAPED_BRACKET_RE = re.compile(r"\\([\[\]])")
#: Markdown emphasis only: ``**x**`` / ``*x*`` wrapping non-space text and
#: standing apart from word characters. A bare ``*`` between numbers
#: (``2 * 3``, ``2*3``) or a footnote asterisk (``value*``) is meaning, kept.
_EMPHASIS_RE = re.compile(r"(?<![\w*])(\*{1,3})(?=[^\s*])(.+?)(?<=[^\s*])\1(?![\w*])")

_SUP_MAP = dict(
    zip(
        "0123456789+\u2212-\u2013=()aboidtnhmpskvlxwycefgjruz",
        "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207b\u207b\u207c\u207d\u207e"
        "\u1d43\u1d47\u1d52\u2071\u1d48\u1d57\u207f\u02b0\u1d50\u1d56\u02e2\u1d4f\u1d5b\u02e1\u02e3\u02b7\u02b8\u1d9c\u1d49\u1da0\u1d4d\u02b2\u02b3\u1d58\u1dbb",
        strict=True,
    )
)
_SUB_MAP = dict(
    zip(
        "0123456789+\u2212-\u2013=()aehijklmnoprstvx",
        "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208b\u208b\u208c\u208d\u208e"
        "\u2090\u2091\u2095\u1d62\u2c7c\u2096\u2097\u2098\u2099\u2092\u209a\u1d63\u209b\u209c\u1d65\u2093",
        strict=True,
    )
)
#: Private-use stand-ins for the mapped script characters, so the NFKD in
#: :func:`normalize_text` cannot flatten ``10\u207b\u2074`` back to ``10\u22124``.
_SCRIPT_CHARS = sorted(set(_SUP_MAP.values()) | set(_SUB_MAP.values()))
_PROTECT = {c: chr(0xE000 + i) for i, c in enumerate(_SCRIPT_CHARS)}
_PROTECT_TABLE = str.maketrans(_PROTECT)


def merge_adjacent_scripts(text: str) -> str:
    """Merge directly adjacent ``</sup><sup>`` / ``</sub><sub>`` runs, so the
    split exponent ``10<sup>\u2212</sup><sup>4</sup>`` reads as one
    ``<sup>\u22124</sup>``. Whitespace between tags is NOT bridged."""
    return _ADJ_SUB_RE.sub("", _ADJ_SUP_RE.sub("", text))


def _script_body(kind: str, body: str, tag_sep: str | None) -> str:
    if tag_sep is not None:
        return f"{tag_sep}{body}{tag_sep}"
    table = _SUP_MAP if kind.lower() == "sup" else _SUB_MAP
    if body and all(ch in table for ch in body):
        return "".join(table[ch] for ch in body)
    return f" {body} "  # unmappable: keep a boundary, never glue


def fold_markup(text: str, *, tag_sep: str | None = None) -> str:
    """The one markup fold: sup/sub runs (merged) -> Unicode super/subscript
    characters (``10<sup>\u2212</sup><sup>4</sup>`` -> ``10\u207b\u2074``; a body with
    no mapping keeps a boundary space), markdown links -> link text,
    ``\\[``/``\\]`` unescaped, emphasis ``*`` dropped (a bare or footnote
    ``*`` is kept). ``tag_sep`` (:func:`tokens` passes a space) instead emits
    the raw body between separators, so a tag is a token boundary."""
    text = merge_adjacent_scripts(text)
    text = SUP_SUB_RE.sub(lambda m: _script_body(m.group(1), m.group(2), tag_sep), text)
    text = MD_LINK_RE.sub(lambda m: m.group(1), text)
    text = _ESCAPED_BRACKET_RE.sub(lambda m: m.group(1), text)
    return _EMPHASIS_RE.sub(lambda m: m.group(2), text)


def normalize_text(text: str) -> str:
    """Casefold + strip soft hyphens + unfold ligatures + collapse
    whitespace. Applied to BOTH sides of every locator comparison."""
    out = text.replace(_SOFT_HYPHEN, "")
    for lig, plain in _LIGATURES.items():
        out = out.replace(lig, plain)
    out = unicodedata.normalize("NFKD", out)
    out = out.casefold()
    return _WS.sub(" ", out).strip()


def tokens(text: str, *, legacy: bool = False) -> list[str]:
    """The lowercase-ASCII token stream of ``text`` (markup-folded, then
    normalized; ``legacy=True`` skips the fold — the pre-fold stream that
    already-approved payloads were validated against).

    Non-ASCII letters are dropped by tokenization after NFKD strips
    combining marks — the snip alphabet is deliberately the URL-safe
    subset."""
    normalized = normalize_text(text if legacy else fold_markup(text, tag_sep=" "))
    ascii_only = normalized.encode("ascii", errors="ignore").decode("ascii")
    return _TOKEN.findall(ascii_only)


def make_snip(quote: str, *, max_tokens: int = 8) -> str:
    """A snip candidate from a verbatim quote: its first ``max_tokens``
    tokens, space-joined. Callers must still validate uniqueness — a
    generic opening may need a hand-picked snip instead."""
    return " ".join(tokens(quote)[:max_tokens])


def is_valid_snip(snip: str) -> bool:
    """The published-form contract: non-empty, single-spaced, lowercase
    ASCII letters/digits/hyphens tokens only."""
    if not snip:
        return False
    parts = snip.split(" ")
    return all(part and _TOKEN.fullmatch(part) for part in parts)


def canonical_snip(raw: str) -> str:
    """The published form of a snip: its token stream, single-spaced."""
    return " ".join(tokens(raw))


#: Shortest token run two neighbouring chunks must share to count as the
#: chunker's overlap rather than coincidence.
_MIN_OVERLAP = 3


def _overlap(prev: list[str], nxt: list[str], floor: int) -> int:
    """Length of the longest PARTIAL seam overlap: a suffix of ``prev`` that
    is a prefix of ``nxt``, shorter than both chunks (0 when shorter than
    ``floor``). Identical or wholly-contained neighbours are not a seam."""
    for k in range(min(len(prev), len(nxt)) - 1, floor - 1, -1):
        if prev[-k:] == nxt[:k]:
            return k
    return 0


def count_matches(needle: str, haystacks: list[str], *, legacy: bool = False) -> int:
    """Occurrences of ``needle``'s token sequence across ``haystacks``
    (each normalized+tokenized; token-boundary matching, so ``ratio 400``
    never matches inside ``ratio 4000``). Uniqueness = exactly 1 across
    the paper's chunks. ``haystacks`` are reading-order chunks: a match
    lying wholly inside the overlap shared with the previous chunk is the
    same passage seen twice and is counted once (a partial seam only:
    identical or nested neighbours count separately; ``legacy`` never
    de-duplicates)."""
    seq = tokens(needle, legacy=legacy)
    if not seq:
        return 0
    total = 0
    n = len(seq)
    prev: list[str] = []
    for haystack in haystacks:
        hs = tokens(haystack, legacy=legacy)
        skip = 0 if legacy or not prev else _overlap(prev, hs, max(n, _MIN_OVERLAP))
        total += sum(
            1 for i in range(len(hs) - n + 1) if hs[i : i + n] == seq and i + n > skip
        )
        prev = hs
    return total


def contains_verbatim(quote: str, chunk_text: str, *, legacy: bool = False) -> bool:
    """Normalized-form containment: is ``quote`` verbatim inside
    ``chunk_text``? Both sides go through :func:`fold_markup` (unless
    ``legacy``) then :func:`normalize_text` — punctuation KEPT (tokenizing
    here would let "2.30 GPa" satisfy a "2\u201330 GPa" quote; the
    normalization folds only extraction artifacts, never characters that
    carry meaning)."""
    if legacy:
        q, c = normalize_text(quote), normalize_text(chunk_text)
    else:
        # protect script characters from NFKD (10\u207b\u2074 != 10\u22124)
        q = normalize_text(fold_markup(quote).translate(_PROTECT_TABLE))
        c = normalize_text(fold_markup(chunk_text).translate(_PROTECT_TABLE))
    return bool(q) and q in c
