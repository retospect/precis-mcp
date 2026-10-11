"""Render orchestration: a computed `figure` chunk → a PNG in `chunk_blobs`.

The glue between the document model and the sandboxed engine:
pull a figure's render recipe (`meta.render.src`) and the `meta.table` data of
every chunk it `plots`, run the code in the phase-1 sandbox
(:func:`precis.render.sandbox.render_python`), and on success write the image
into the figure's `chunk_blobs` row (regenerable) and stamp its invalidation
key. Pure orchestration — the isolation lives in the engine, the storage in the
store; this never executes chunk code in-process.

The render code receives ``data = {'tables': [<meta.table>, ...]}`` in plotted
order, plus ``out`` (the PNG path) — see the engine's contract.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from precis.render.sandbox import DEFAULT_TIMEOUT_S, render_python

log = logging.getLogger(__name__)

#: mime of the rendered artifact (the engine always emits PNG).
RENDER_MIME = "image/png"


@dataclass(frozen=True)
class RenderOutcome:
    """Result of rendering one figure chunk.

    ``ok`` means a PNG was produced and written to the figure's blob.
    ``error`` is a short tag on failure — ``"not-a-graph"`` (no render
    recipe), ``"no-data"`` (no plotted tables), or the engine's own tag
    (``"timeout"`` / ``"exit:n"`` / ``"no-output"``); ``detail`` carries the
    child stderr for the failure bubble.
    """

    ok: bool
    error: str | None = None
    detail: str = ""
    cached_key: str | None = None


def invalidation_key(input_shas: list[str]) -> str:
    """`hash(render_src_sha, sorted(plotted_data_shas))` — the
    content-addressed key. Any plotted table's `content_sha` change (or a
    recipe edit) flips it, marking the figure stale."""
    joined = "\n".join(sorted(input_shas))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def render_figure_chunk(
    store: Any,
    figure_chunk_id: int,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RenderOutcome:
    """Render one computed `figure` chunk and write its PNG to `chunk_blobs`.

    No-op-safe: returns ``ok=False`` (never raises) for a plain image figure
    or a render failure, so a caller (the render pass / export barrier) can
    bubble the reason without crashing the worker."""
    bundle = store.drafts.figure_render_bundle(figure_chunk_id)
    if bundle is None:
        return RenderOutcome(ok=False, error="not-a-graph")
    tables = bundle["tables"]
    if not tables:
        return RenderOutcome(ok=False, error="no-data")

    result = render_python(
        str(bundle["render"]["src"]),
        inputs={"tables": tables},
        timeout_s=timeout_s,
    )
    if not result.ok or result.png is None:
        return RenderOutcome(ok=False, error=result.error, detail=result.stderr)

    store.drafts.upsert_chunk_blob(figure_chunk_id, result.png, RENDER_MIME)
    key = invalidation_key(bundle["input_shas"])
    store.drafts.stamp_render_key(figure_chunk_id, key)
    return RenderOutcome(ok=True, cached_key=key)


def needs_render(store: Any, chunk: Any) -> bool:
    """True when a draft graph figure must be (re)rendered before use: it is
    still ``meta.figure.render_pending``, or its stamped ``meta.render.cached_key``
    no longer matches the key of the recipe + plotted tables' current
    ``content_sha`` (the plotted data changed since the last render). A graph
    whose plotted tables are all gone keeps its last image."""
    meta = getattr(chunk, "meta", None) or {}
    if (meta.get("figure") or {}).get("render_pending"):
        return True
    if not isinstance(meta.get("render"), dict):
        return False
    bundle = store.drafts.figure_render_bundle(chunk.chunk_id)
    if bundle is None or not bundle["tables"]:
        return False
    return meta["render"].get("cached_key") != invalidation_key(bundle["input_shas"])


def ensure_rendered(store: Any, chunk: Any) -> str | None:
    """Render a draft graph figure that is pending or stale (see
    :func:`needs_render`).

    ``DraftHandler._add_graph_figure`` writes a placeholder PNG flagged
    ``meta.figure.render_pending``; the real image is rendered here, lazily, at
    the moment it is consumed (export, the reader's blob route). A figure whose
    plotted data was edited after its last render is re-rendered the same way.
    The result is persisted — ``chunk_blobs`` is the regenerable render cache,
    so the next consumer finds the current image and the flag cleared.

    Returns ``None`` when nothing was needed or the render succeeded, else a
    human-readable failure the caller must surface (the flag/stale key stays,
    so the next consume retries)."""
    if not needs_render(store, chunk):
        return None
    outcome = render_figure_chunk(store, chunk.chunk_id)
    if not outcome.ok:
        tail = outcome.detail.strip().splitlines()[-1:] or [""]
        return f"graph render failed ({outcome.error})" + (
            f": {tail[0]}" if tail[0] else ""
        )
    store.drafts.clear_render_pending(chunk.chunk_id)
    return None
