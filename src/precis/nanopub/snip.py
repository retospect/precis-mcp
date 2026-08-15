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


def normalize_text(text: str) -> str:
    """Casefold + strip soft hyphens + unfold ligatures + collapse
    whitespace. Applied to BOTH sides of every locator comparison."""
    out = text.replace(_SOFT_HYPHEN, "")
    for lig, plain in _LIGATURES.items():
        out = out.replace(lig, plain)
    out = unicodedata.normalize("NFKD", out)
    out = out.casefold()
    return _WS.sub(" ", out).strip()


def tokens(text: str) -> list[str]:
    """The lowercase-ASCII token stream of ``text`` (normalized first).

    Non-ASCII letters are dropped by tokenization after NFKD strips
    combining marks — the snip alphabet is deliberately the URL-safe
    subset."""
    normalized = normalize_text(text)
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


def count_matches(needle: str, haystacks: list[str]) -> int:
    """Occurrences of ``needle``'s token sequence across ``haystacks``
    (each normalized+tokenized; token-boundary matching, so ``ratio 400``
    never matches inside ``ratio 4000``). Uniqueness = exactly 1 across
    the paper's chunks."""
    seq = tokens(needle)
    if not seq:
        return 0
    total = 0
    n = len(seq)
    for haystack in haystacks:
        hs = tokens(haystack)
        total += sum(1 for i in range(len(hs) - n + 1) if hs[i : i + n] == seq)
    return total


def contains_verbatim(quote: str, chunk_text: str) -> bool:
    """Normalized-form containment: is ``quote`` verbatim inside
    ``chunk_text``? Compared on :func:`normalize_text` output —
    punctuation KEPT (tokenizing here would let "2.30 GPa" satisfy a
    "2–30 GPa" quote; the normalization folds only extraction artifacts,
    never characters that carry meaning)."""
    q = normalize_text(quote)
    c = normalize_text(chunk_text)
    return bool(q) and q in c
