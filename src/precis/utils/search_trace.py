"""Call-local retrieval evidence; no queries, embeddings, or rendered output.

The dispatcher owns the collector and the single best-effort write. Fan-out
workers inherit it through copy_context; late workers cannot mutate a closed
trace. Stage observers reuse ranks/distances already computed by retrieval.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from precis.utils import handle_registry

log = logging.getLogger(__name__)
MAX_CANDIDATES = 50


@dataclass
class SearchTrace:
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    kinds: list[str] | None = None
    lock: Any = field(default_factory=Lock)
    closed: bool = False

    def snapshot(self) -> tuple[list[dict[str, Any]], int]:
        with self.lock:
            self.closed = True
            return (
                [dict(c) for c in list(self.candidates.values())[:MAX_CANDIDATES]],
                len(self.candidates),
            )


CURRENT: ContextVar[SearchTrace | None] = ContextVar("search_trace", default=None)


def active() -> bool:
    trace = CURRENT.get()
    return trace is not None and not trace.closed


def resolved_kinds(kinds: list[str]) -> None:
    trace = CURRENT.get()
    if trace is not None and not trace.closed:
        trace.kinds = list(kinds)


def ref_handle(ref: Any, block: Any = None) -> str:
    try:
        chunk = block is not None and ref.slug is not None
        ident = block.id if chunk else ref.id
        return handle_registry.try_format(ref.kind, ident, chunk=chunk) or (
            f"{ref.kind}:{ref.id}"
        )
    except Exception:
        log.warning("search trace: handle observation failed", exc_info=True)
        return ""


def observe(
    handle: str,
    *,
    lexical_rank: int | None = None,
    semantic_distance: float | None = None,
    fused_rank: int | None = None,
    cut: str | None = None,
) -> None:
    """Merge an existing stage's evidence without losing earlier leg values."""
    trace = CURRENT.get()
    if trace is None or not handle:
        return
    try:
        with trace.lock:
            if trace.closed:
                return
            candidate = trace.candidates.setdefault(
                handle,
                {
                    "handle": handle,
                    "lexical_rank": None,
                    "semantic_distance": None,
                    "fused_rank": None,
                    "cut": "page",
                    "rendered": False,
                },
            )
            if lexical_rank is not None:
                old = candidate["lexical_rank"]
                candidate["lexical_rank"] = (
                    min(old, lexical_rank) if old else lexical_rank
                )
            if semantic_distance is not None:
                old = candidate["semantic_distance"]
                candidate["semantic_distance"] = (
                    min(old, semantic_distance)
                    if old is not None
                    else semantic_distance
                )
            if fused_rank is not None:
                candidate["fused_rank"] = fused_rank
            if cut is not None:
                candidate["cut"] = cut
    except Exception:
        log.warning("search trace: stage observation failed", exc_info=True)


def page(handles: list[str], *, rendered: bool = False) -> None:
    """Record the actual post-cut ordering/page, never recomputing fusion."""
    trace = CURRENT.get()
    if trace is None:
        return
    try:
        with trace.lock:
            if trace.closed:
                return
            if rendered:
                for existing in trace.candidates.values():
                    existing["rendered"] = False
                    if existing["cut"] is None:
                        existing["cut"] = "page"
            for handle in handles:
                candidate = trace.candidates.get(handle)
                if candidate is None:
                    continue
                candidate["cut"] = None
                candidate["rendered"] = rendered
    except Exception:
        log.warning("search trace: page observation failed", exc_info=True)
