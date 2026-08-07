"""One paper/chunk identity header for every preview surface.

Historically each hover/card built its own "which paper is this?" line: the
generic ``/preview/{kind}`` route (``routes/preview.py``) showed a bare
title; the chunk hover (``routes/drafts.py::preview_chunk``) added a
``First … Last`` byline; the claim evidence rows (``claim_render.py``)
printed ``handle · title (year)``; the ``/drive`` card
(``item_view.py::title_meta``) had ``title / journal / authors / year`` —
four field vocabularies for the same question. This module is the single
builder they now share: :func:`paper_head` turns a ``refs`` row into a
:class:`PaperHead`, rendered by ``templates/_paper_head.html.j2``.

The header is two lines::

    <year> <title>
    <journal> <first author> … <last author>

Colour follows the same held-vs-external language the inline citation
markers already use (``linkify._cite_style``: sky ``§`` for a paper we
hold, amber ``↗`` for one we don't): a paper whose full text is in the
corpus reads sky, a stub / external reference reads amber. ``held`` is the
caller's call — it knows whether body chunks exist; this module only
formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from precis.utils.authors import author_names

#: The kinds whose ``/preview`` hover leads with a paper identity header
#: (title + byline + venue), rather than the generic kind-chip + title.
#: A ``cfp`` / ``patent`` carries the same year/venue/author shape as a
#: ``paper``. Single-sourced here so ``routes/preview.py`` and
#: ``routes/drafts.py`` agree on the set.
PAPER_IDENT_KINDS: frozenset[str] = frozenset({"paper", "cfp", "patent"})

#: Venue clamp — the journal is a second-line detail, not the headline; a
#: long "Journal of the American Chemical Society" would push the authors
#: off the card, so it's clipped (no abbreviation table exists in the
#: tree, so this is the full name, truncated).
_JOURNAL_MAX = 24

#: Strip JATS/HTML tags and collapse whitespace out of a stored abstract
#: (the publisher copy in ``meta['abstract']`` is often markup-wrapped).
#: Kept in step with the identical pair in ``routes/papers.py`` — which
#: now delegates here so the two never diverge.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PaperHead:
    """The shared two-line paper identity, ready for ``_paper_head.html.j2``."""

    ref_id: int
    handle: str
    title: str
    year: int | None
    journal: str
    first_author: str
    last_author: str
    cite_key: str | None
    held: bool

    @property
    def multi_author(self) -> bool:
        """True when there's a distinct last author — the template then
        prints ``first … last``; a single author prints once."""
        return bool(self.last_author) and self.last_author != self.first_author

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe mapping for surfaces that serialize the row before
        rendering — the finding References panel dumps its rows via Jinja's
        ``tojson``, which a dataclass would break. ``multi_author`` is
        materialised (it's a computed property, not a field) so the macro
        reads it identically whether handed the object or this dict — Jinja
        resolves ``h.multi_author`` as an attribute or a key transparently."""
        return {
            "ref_id": self.ref_id,
            "handle": self.handle,
            "title": self.title,
            "year": self.year,
            "journal": self.journal,
            "first_author": self.first_author,
            "last_author": self.last_author,
            "cite_key": self.cite_key,
            "held": self.held,
            "multi_author": self.multi_author,
        }


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def paper_head(ref: Any, *, held: bool, handle: str = "") -> PaperHead:
    """Build the shared identity header from a ``refs`` row.

    ``held`` — does the corpus hold the full text (body chunks)? Drives the
    sky/amber colour, matching the inline ``§``/``↗`` cite convention.
    ``handle`` — the display handle (``pa123`` / cite key) when the caller
    has one; blank otherwise (the hover card hides it — a kind chip already
    labels it — while the list rows pass it as the click target).
    """
    meta = getattr(ref, "meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    journal = _clip(str(meta.get("journal") or ""), _JOURNAL_MAX)
    names = author_names(getattr(ref, "authors", None))
    first = names[0] if names else ""
    last = names[-1] if names else ""
    title = (getattr(ref, "title", "") or "").split("\n", 1)[0].strip()
    return PaperHead(
        ref_id=int(getattr(ref, "id", 0) or 0),
        handle=handle,
        title=title or "(untitled)",
        year=getattr(ref, "year", None),
        journal=journal,
        first_author=first,
        last_author=last,
        cite_key=getattr(ref, "slug", None),
        held=held,
    )


def paper_head_from_facts(
    *,
    ref_id: int,
    title: str,
    year: int | None,
    handle: str = "",
    held: bool = True,
) -> PaperHead:
    """A :class:`PaperHead` from the bare facts a caller already has
    (title + year), when the full ``refs`` row isn't on hand — e.g. a claim
    evidence edge whose paper ref wasn't in the batch. Venue / authors come
    back empty and the macro degrades to the one available line."""
    return PaperHead(
        ref_id=ref_id,
        handle=handle,
        title=(title or "").split("\n", 1)[0].strip() or "(untitled)",
        year=year,
        journal="",
        first_author="",
        last_author="",
        cite_key=None,
        held=held,
    )


def paper_abstract(ref: Any, *, max_chars: int | None = None) -> str:
    """Tag-stripped, whitespace-collapsed publisher abstract (``""`` when
    absent). Single-sourced here so the hover card and the paper Meta form
    strip identically. ``max_chars`` clamps with an ellipsis."""
    meta = getattr(ref, "meta", None) or {}
    if not isinstance(meta, dict):
        return ""
    raw = meta.get("abstract")
    if not raw:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", str(raw))).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text
