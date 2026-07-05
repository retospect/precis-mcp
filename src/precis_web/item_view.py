"""Per-kind item presenter — the Slice-3 seed of the ``ItemPresenter``
contract (``docs/proposals/unified-item-view.md``).

One presenter renders a cross-kind search hit into a row view-model for
the unified ``/items`` list: a name, the matching-chunk preview, and the
click-through URL. The default covers *every* kind through the generic
``/refs/<kind>/<id>`` detail route; a kind with a richer reader overrides
``open_url`` via :data:`_OPEN_URL_OVERRIDES`.

This is deliberately the minimal seed. It grows — per the proposal — into
the full contract (``preview(query) -> text|image``, ``hover_preview``,
``thumbnail``, ``state``, ``actions``) and gets promoted to
``@abstractmethod`` once every kind adopts it (the check-time-totality
guarantee). For now it is a plain class with a default, so unadopted
kinds still render.
"""

from __future__ import annotations

from typing import Any

#: Max characters of the matching chunk shown as the row preview.
_PREVIEW_CHARS = 240

#: Kinds with a richer detail view than the generic ``/refs`` browser.
#: ``{id}`` / ``{slug}`` are filled from the ref. Every other kind falls
#: back to ``/refs/<kind>/<id>`` (which exists for all kinds), so the map
#: only needs the exceptions — grow it as kinds gain dedicated readers.
_OPEN_URL_OVERRIDES: dict[str, str] = {
    "paper": "/papers/{id}",
}

#: Kinds whose ingest runs a fetch→PDF→chunk pipeline, so the
#: stub-vs-ingested distinction is meaningful. Other kinds (web,
#: wikipedia, perplexity, …) are always chunked on arrival — a "chunks"
#: badge on every row would be noise, so state markers are scoped here.
_PIPELINE_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "datasheet", "cfp", "pres"}
)


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


def presenter_for(kind: str) -> ItemPresenter:
    """Return the presenter for ``kind`` (the default for now — the
    registry seam where per-kind subclasses will land)."""
    return ItemPresenter(kind)


def item_row(
    ref: Any,
    block: Any,
    score: float,
    flags: set[str],
    *,
    has_chunks: bool = False,
) -> dict[str, Any]:
    """Build one unified-list row view-model from a search hit.

    ``flags`` is the ref's active reading-intent flag values (for the
    toggle buttons). ``preview`` is the chunk that made the ref match.
    ``has_chunks`` drives the stub/ingested state badges (a search hit
    matched a chunk, so it's ``True``; a recent-list ref is probed).
    """
    p = presenter_for(getattr(ref, "kind", ""))
    return {
        "id": getattr(ref, "id", None),
        "kind": getattr(ref, "kind", ""),
        "title": p.name(ref),
        "open_url": p.open_url(ref),
        "preview": p.preview(block),
        "created_at": getattr(ref, "created_at", None),
        "state": p.state(ref, has_chunks=has_chunks),
        "score": score,
        "flags": flags,
    }
