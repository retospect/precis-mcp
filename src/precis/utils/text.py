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

import re
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

__all__ = ["clip", "clip_total", "esc", "esc_quoted", "excerpt", "fmt_num", "slugify"]

_SLUG_RUN_RE = re.compile(r"[^a-z0-9]+")


def esc(value: Any, *, quote: bool = False) -> str:
    """XML/SVG-escape ``value`` (``&`` ``<`` ``>``); ``None`` -> ``""``.

    ``quote=True`` also escapes ``"`` as ``&quot;`` (for attribute values).
    """
    text = "" if value is None else str(value)
    return _xml_escape(text, {'"': "&quot;"} if quote else {})


def esc_quoted(value: Any) -> str:
    """Backslash-escape ``\\`` and ``"`` for a double-quoted DSN-style string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def fmt_num(v: float) -> str:
    """A float without a trailing ``.0`` (``256.0`` -> ``256``)."""
    return str(int(v)) if v == int(v) else str(v)


def clip(text: str | None, n: int = 160) -> str:
    """Collapse whitespace, cut at ``n`` chars and append ``…`` if shortened.

    The result can be ``n + 1`` long (the ellipsis sits past the cut); use
    :func:`clip_total` when the ellipsis must fit inside the limit.
    """
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def clip_total(text: str, limit: int) -> str:
    """Strip ``text`` and trim it so the result, ellipsis included, is <= ``limit``."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def slugify(text: str | None, *, max_len: int | None = None, default: str = "") -> str:
    """Lowercase, runs of non-``[a-z0-9]`` to one hyphen, ends trimmed.

    No diacritic folding (``café`` -> ``caf``); use
    :func:`precis.utils.slug.slug_from_text` when folding is wanted.
    ``max_len`` truncates after slugging; ``default`` replaces an empty
    result.
    """
    s = _SLUG_RUN_RE.sub("-", (text or "").lower()).strip("-")
    if max_len is not None:
        s = s[:max_len]
    return s or default


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
