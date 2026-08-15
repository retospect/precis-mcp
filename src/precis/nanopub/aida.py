"""Canonicalised AIDA URIs — one sentence, one content address.

An AIDA sentence (Atomic, Independent, Declarative, Absolute) *is* its
URI: ``http://purl.org/aida/<percent-encoded sentence>``. Content-
addressed convergence is the entire benefit — two agents asserting the
same sentence must land on the same URI — and the live corpus defeats
itself here: both ``%20`` and ``+`` appear for spaces, minting different
URIs for identical sentences. So: **parse leniently, mint strictly.**

Canonical form (:func:`canonical_sentence`): whitespace stripped and
collapsed, and a terminal ``.`` ensured when the sentence carries no
terminal punctuation — grammar is otherwise untouched (rewording is a
new identity by design; only encoding/whitespace variance is folded).
Hedges are never stripped here: per the hypothesis-type decision the
sentence must arrive declarative and unhedged, the TYPE carries
epistemic status.

:func:`aida_uri` percent-encodes with ``%20`` (never ``+``) and
uppercase hex via :func:`urllib.parse.quote`. :func:`parse_aida_uri`
accepts either encoding and any hex case, then re-canonicalises — so
matching against external corpus URIs converges.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote_plus

#: The community AIDA namespace (observed in the live registry corpus).
AIDA_NS = "http://purl.org/aida/"

_WS = re.compile(r"\s+")
_TERMINAL_PUNCT = (".", "!", "?")


def canonical_sentence(text: str) -> str:
    """Fold whitespace variance and ensure terminal punctuation.

    Deterministic and idempotent; raises ``ValueError`` on an empty
    sentence (an empty claim has no identity to mint).
    """
    folded = _WS.sub(" ", text).strip()
    if not folded:
        raise ValueError("empty claim sentence")
    if not folded.endswith(_TERMINAL_PUNCT):
        folded += "."
    return folded


def aida_uri(sentence: str) -> str:
    """The canonical AIDA URI for ``sentence`` (canonicalised first)."""
    return AIDA_NS + quote(canonical_sentence(sentence), safe="")


def parse_aida_uri(uri: str) -> str | None:
    """Lenient inverse: the canonical sentence for an AIDA URI, or
    ``None`` for a non-AIDA URI. Accepts ``+`` for space and any hex
    case (both live in the corpus)."""
    if not uri.startswith(AIDA_NS):
        return None
    decoded = unquote_plus(uri[len(AIDA_NS) :])
    if not decoded.strip():
        return None
    return canonical_sentence(decoded)
