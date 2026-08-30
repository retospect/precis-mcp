"""Text-cleanup helpers shared across handlers.

Three near-identical ``_excerpt`` implementations used to live in
``handlers/paper.py``, ``handlers/markdown.py``, and
``handlers/patent.py``, each with a slightly different default limit
and tail-handling rule. They're consolidated here so adjustments
(elision character, whitespace policy, word-boundary handling) only
need to change in one place.

Pure — no DB, no IO, no logging.
"""

from __future__ import annotations

__all__ = ["excerpt"]


def excerpt(
    text: str,
    *,
    limit: int = 240,
    ellipsis: str = "…",
    collapse_whitespace: bool = True,
) -> str:
    """Trim ``text`` to roughly ``limit`` chars, word-boundary-aware.

    Trimming snaps to the last whitespace boundary inside ``[:limit]``
    when one exists, which is friendlier for prose previews — search
    headlines, list rows, and TOC blurbs all benefit. When the slice
    has no internal whitespace (a long URL, a hash, a single token)
    we fall back to the hard-cut form to preserve the head of the
    string rather than collapse to a bare ellipsis.

    The ``ellipsis`` is appended only when the input was actually
    shortened. Returning an unchanged short string avoids the
    "every preview ends in …" smell that the older paper-side
    helper had.

    ``collapse_whitespace`` (default ``True``) flattens all runs of
    whitespace — including newlines — to a single space before
    trimming; the usual policy for a one-line preview. Pass ``False``
    to trim in place instead, preserving embedded newlines/paragraph
    breaks (a multi-paragraph abstract, say) — only the ``[:limit]``
    cut point still snaps to the last space.

    The function is idempotent on already-collapsed input and never
    raises — pass it whatever the upstream produced.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split()) if collapse_whitespace else text
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[:limit]
    last_space = head.rfind(" ")
    if last_space > 0:
        head = head[:last_space]
    return f"{head.rstrip()}{ellipsis}"
