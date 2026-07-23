"""Per-kind item presenter — the ``ItemPresenter`` contract
(``docs/proposals/unified-item-view.md``).

One presenter renders a cross-kind search hit into a row view-model for
the unified ``/items`` list: a name, the matching-chunk preview, a
richer hover peek, an optional thumbnail, kind-specific actions, and the
click-through URL. The default covers *every* kind through the generic
``/refs/<kind>/<id>`` detail route; a kind with a richer reader overrides
``open_url`` via :data:`_OPEN_URL_OVERRIDES`.

The full method contract from the proposal is now present
(``name``/``open_url``/``preview``/``hover_preview``/``thumbnail``/
``state``/``actions``/``links``), each with a generic default so every
kind renders without a subclass. **Not yet promoted to
``@abstractmethod``** — that check-time-totality guarantee (per the
proposal's decisions log) requires a dedicated presenter for every
source/artifact kind, which is a separate per-kind pass (tracked in
``OPEN-ITEMS.md``), not a mechanical follow-on to this module. A kind
needing a richer peek registers a subclass in
:data:`_PRESENTER_CLASSES` (seeded here with ``youtube``'s thumbnail).
"""

from __future__ import annotations

import re
from typing import Any

from precis_web.paper_links import doi_url, scholar_url, uol_url

#: Max characters of the matching chunk shown as the row preview.
_PREVIEW_CHARS = 240

#: Max characters of the richer hover-popover peek.
_HOVER_CHARS = 600

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: Kinds with a richer detail view than the generic ``/refs`` browser.
#: ``{id}`` / ``{slug}`` are filled from the ref. Every other kind falls
#: back to ``/refs/<kind>/<id>`` (which exists for all kinds), so the map
#: only needs the exceptions — grow it as kinds gain dedicated readers.
_OPEN_URL_OVERRIDES: dict[str, str] = {
    "paper": "/papers/{id}",
    "datasheet": "/datasheets/{id}",
}

#: Kinds whose ingest runs a fetch→PDF→chunk pipeline, so the
#: stub-vs-ingested distinction is meaningful. Other kinds (web,
#: wikipedia, perplexity, …) are always chunked on arrival — a "chunks"
#: badge on every row would be noise, so state markers are scoped here.
_PIPELINE_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "datasheet", "cfp", "pres"}
)

#: Namespaces hidden from the per-row tag chips — machine/control tags
#: the operator doesn't browse by.
_TAG_HIDE_NS: frozenset[str] = frozenset(
    {"STATUS", "DREAM", "PRIO", "SRC", "CACHE", "EMBED", "LLM", "ROLE3", "CLASSIFY"}
)

#: Flag values shown as toggle buttons, not repeated as tag chips.
_TAG_HIDE_VALUES: frozenset[str] = frozenset({"read-later", "must-read", "skim"})


def _display_tags(raw: list[tuple[str, str]] | None) -> list[dict[str, str]]:
    """Per-row tag chips: what this item was tagged with, minus the
    machine namespaces and the reading-intent flags (which have buttons).
    ``OPEN`` tags render bare; others as ``namespace:value``; each links
    to the ``/tags/refs`` pivot.
    """
    out: list[dict[str, str]] = []
    for ns, val in raw or []:
        if ns in _TAG_HIDE_NS:
            continue
        if ns == "OPEN" and val in _TAG_HIDE_VALUES:
            continue
        label = val if ns == "OPEN" else f"{ns}:{val}"
        out.append({"label": label, "href": f"/tags/refs?namespace={ns}&value={val}"})
    return out


class ItemPresenter:
    """Default renderer for one kind's search hit → row view-model."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def name(self, ref: Any) -> str:
        title = (getattr(ref, "title", None) or "").strip()
        return title or f"{self.kind} #{getattr(ref, 'id', '?')}"

    def open_url(self, ref: Any) -> str:
        tmpl = _OPEN_URL_OVERRIDES.get(self.kind)
        if tmpl:
            return tmpl.format(
                id=getattr(ref, "id", ""),
                slug=getattr(ref, "slug", None) or getattr(ref, "id", ""),
            )
        return f"/refs/{self.kind}/{getattr(ref, 'id', '')}"

    def preview(self, block: Any) -> str:
        text = (getattr(block, "text", None) or "").strip()
        if len(text) <= _PREVIEW_CHARS:
            return text
        return text[: _PREVIEW_CHARS - 1].rstrip() + "…"

    def _abstract(self, ref: Any) -> str:
        """Publisher abstract off ``refs.meta['abstract']``, tag-stripped —
        present on the paper-family kinds, empty (harmlessly) elsewhere."""
        meta = getattr(ref, "meta", None) or {}
        raw = meta.get("abstract")
        if not raw:
            return ""
        return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(raw))).strip()

    def hover_preview(self, ref: Any, block: Any) -> str:
        """The richer hover-popover peek: abstract (if any) + the matching
        chunk, wider than the row preview. Falls back to the row preview
        when there's neither an abstract nor a chunk (a recent-list row
        with no query has no ``block``)."""
        abstract = self._abstract(ref)
        text = (getattr(block, "text", None) or "").strip()
        combined = "\n\n".join(p for p in (abstract, text) if p) or self.preview(block)
        if len(combined) <= _HOVER_CHARS:
            return combined
        return combined[: _HOVER_CHARS - 1].rstrip() + "…"

    def thumbnail(self, ref: Any) -> str | None:
        """Cached-still image URL, or ``None`` when there isn't one.

        Visual-kind thumbnails (structure/cad/pcb) are a deferred render
        + cache pass (see the proposal's open question); the default is
        always ``None`` here. A kind that already has a cheap image (e.g.
        the youtube per-video screenshot) overrides this."""
        return None

    def actions(self, ref: Any) -> list[dict[str, str]]:
        """Kind-specific actions beyond the universal flag buttons (e.g.
        papers-needed's "re-chase stub", cad's "apply proposal" — per the
        proposal). None are wired yet; the seam is here so a future
        subclass has somewhere to put them without leaking back onto a
        bespoke page."""
        return []

    def state(self, ref: Any, *, has_chunks: bool) -> list[dict[str, str]]:
        """Pipeline-state badges for the row (paper-family kinds only).

        ``stub`` — a corpus doc still awaiting the fetcher (no PDF yet);
        ``chunks`` — ingested, has body chunks (searchable). Mirrors the
        Papers-tab vocabulary. Non-pipeline kinds get no badges.
        """
        if self.kind not in _PIPELINE_KINDS:
            return []
        badges: list[dict[str, str]] = []
        if getattr(ref, "pdf_sha256", None) is None and not has_chunks:
            badges.append(
                {
                    "label": "stub",
                    "cls": "bg-slate-200 text-slate-500",
                    "title": "awaiting fetch — no PDF yet",
                }
            )
        if has_chunks:
            badges.append(
                {
                    "label": "chunks",
                    "cls": "bg-sky-100 text-sky-700",
                    "title": "ingested — has body chunks",
                }
            )
        return badges

    def links(self, identifier: str | None) -> list[dict[str, str]]:
        """Off-site "go find it" links from a paper's external identifier —
        the publisher/arXiv page, University of Limerick Primo, and Google
        Scholar. Empty when there's no identifier (non-paper rows)."""
        if not identifier:
            return []
        out: list[dict[str, str]] = []
        pub = doi_url(identifier)
        if pub:
            out.append(
                {
                    "label": "arXiv" if identifier.startswith("arxiv:") else "DOI",
                    "href": pub,
                }
            )
        u = uol_url(identifier)
        if u:
            out.append({"label": "UoL", "href": u})
        s = scholar_url(identifier)
        if s:
            out.append({"label": "Scholar", "href": s})
        return out


class YoutubePresenter(ItemPresenter):
    """A video already has a free thumbnail — YouTube's stable per-video
    still. Mirrors the fallback in ``routes/refs.py``'s ``_youtube_meta``
    (``video_id`` defaults to the ref's slug when there's no scraped
    cache row), so this needs no store round-trip."""

    def thumbnail(self, ref: Any) -> str | None:
        video_id = getattr(ref, "slug", None)
        if not video_id:
            return None
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


#: Per-kind presenter overrides — the registry seam for a kind whose
#: hover/thumbnail/actions need more than the generic default. Grow this
#: as kinds adopt a richer presenter; see the module docstring for why
#: this isn't yet a total ``@abstractmethod`` mapping over every kind.
_PRESENTER_CLASSES: dict[str, type[ItemPresenter]] = {
    "youtube": YoutubePresenter,
}


def presenter_for(kind: str) -> ItemPresenter:
    """Return the presenter for ``kind`` — a registered override
    (:data:`_PRESENTER_CLASSES`) or the generic default."""
    cls = _PRESENTER_CLASSES.get(kind, ItemPresenter)
    return cls(kind)


#: Kinds declared ``role='artifact'`` that fall back to when the live hub
#: isn't reachable (mirrors ``routes/drive.py``'s ``_artifact_kinds``
#: fallback — kept in sync by hand since both are small, static lists).
_ARTIFACT_KIND_FALLBACK: tuple[str, ...] = ("draft", "structure", "cad", "todo")


def artifact_kinds(hub: Any) -> list[str]:
    """Kinds declared ``role='artifact'`` in this build (minus ``folder``)
    — the "Author" facet on ``/items`` (source vs. authored, per the
    proposal's "author/source split is a facet of ``KindSpec.role``").
    Reads the live hub so a future placeable kind joins by declaration,
    with no route edit; falls back to a static list when the hub isn't
    wired (e.g. a test double with ``hub=None``)."""
    if hub is None:
        return list(_ARTIFACT_KIND_FALLBACK)
    try:
        out = []
        for k in sorted(hub.kinds):
            handler = hub.handler_for(k)
            spec = getattr(handler, "spec", None)
            if spec is not None and getattr(spec, "role", None) == "artifact":
                if k != "folder":
                    out.append(k)
        return out
    except Exception:
        return list(_ARTIFACT_KIND_FALLBACK)


def item_row(
    ref: Any,
    block: Any,
    score: float,
    flags: set[str],
    *,
    has_chunks: bool = False,
    tags: list[tuple[str, str]] | None = None,
    identifier: str | None = None,
) -> dict[str, Any]:
    """Build one unified-list row view-model from a search hit.

    ``flags`` is the ref's active reading-intent flag values (for the
    toggle buttons). ``preview`` is the chunk that made the ref match.
    ``has_chunks`` drives the stub/ingested state badges (a search hit
    matched a chunk, so it's ``True``; a recent-list ref is probed).
    ``tags`` are the ref's raw ``(namespace, value)`` tags → the per-row
    chips.
    """
    p = presenter_for(getattr(ref, "kind", ""))
    return {
        "id": getattr(ref, "id", None),
        "kind": getattr(ref, "kind", ""),
        "title": p.name(ref),
        "open_url": p.open_url(ref),
        "preview": p.preview(block),
        "hover_preview": p.hover_preview(ref, block),
        "thumbnail": p.thumbnail(ref),
        "actions": p.actions(ref),
        "created_at": getattr(ref, "created_at", None),
        "state": p.state(ref, has_chunks=has_chunks),
        "tags": _display_tags(tags),
        "links": p.links(identifier),
        "score": score,
        "flags": flags,
    }
