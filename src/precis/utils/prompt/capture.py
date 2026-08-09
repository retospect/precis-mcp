"""Persist the assembled prompt's INPUT alongside the LLM's output.

Prompt assembler's :func:`~precis.utils.prompt.assembler.assemble` builds the full
input context for every agentic dispatch site (the planner tick, the
structural/deep-tree reviewers); only the *output* stream was durable —
``meta.transcript`` in :mod:`precis.workers.executors.claude_inproc`. This
module is the input-side twin: :func:`persist_assembled_context` writes the
assembled :class:`~precis.utils.prompt.model.Block` list onto a ref's
``meta`` so a debugging surface can render "what the LLM actually saw last
time" next to what it said.

Contract (a separate web surface renders this): ``meta.assembled_context``
is a JSON array of ``{"id", "layer", "text"}`` objects in assembled order,
plus a sibling ``meta.assembled_context_at`` ISO timestamp.

Never-fatal by design, mirroring the transcript capture: a capture failure
is logged and swallowed, never raised — these ticks/passes run unattended
across the fleet, and a debug artifact must never be able to sink the real
work riding alongside it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.utils.prompt.model import Block

log = logging.getLogger(__name__)

#: Cap on the total serialized block text — mirrors the ``claude_inproc``
#: transcript cap (``_TRANSCRIPT_CAP``) so a runaway assembly (an oversized
#: doc_context table, a long skill index) can't bloat ``refs.meta`` without
#: bound.
_ASSEMBLED_CONTEXT_CAP = 1_000_000

#: Floor under which a block is left alone during truncation — shrinking
#: targets the few large blocks, not every small one.
_MIN_BLOCK_KEEP = 200

_TRUNCATION_SUFFIX = "\n…(truncated)"


def _capped_entries(blocks: Sequence[Block]) -> list[dict[str, str]]:
    """Render ``blocks`` to the contract shape, truncating the largest
    blocks first when the total exceeds :data:`_ASSEMBLED_CONTEXT_CAP`."""
    entries = [{"id": b.id, "layer": str(b.layer), "text": b.text} for b in blocks]
    total = sum(len(e["text"]) for e in entries)
    over = total - _ASSEMBLED_CONTEXT_CAP
    if over <= 0:
        return entries
    order = sorted(
        range(len(entries)), key=lambda i: len(entries[i]["text"]), reverse=True
    )
    for i in order:
        if over <= 0:
            break
        text = entries[i]["text"]
        if len(text) <= _MIN_BLOCK_KEEP:
            continue
        # Target the block's FINAL size (kept prefix + the suffix itself) at
        # ``len(text) - over``, floored at ``_MIN_BLOCK_KEEP`` — subtracting
        # the suffix length up front guarantees a strict shrink (naively
        # reusing ``over`` as the *kept-prefix* length would let the
        # re-appended suffix eat the savings back, or even grow the block).
        target_final_len = max(_MIN_BLOCK_KEEP, len(text) - over)
        if target_final_len >= len(text):
            continue
        keep = max(0, target_final_len - len(_TRUNCATION_SUFFIX))
        trimmed = text[:keep] + _TRUNCATION_SUFFIX
        if len(trimmed) >= len(text):
            continue
        over -= len(text) - len(trimmed)
        entries[i]["text"] = trimmed
    return entries


def _write(conn: Connection, ref_id: int, meta: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
        (Jsonb(meta), ref_id),
    )


def persist_assembled_context(
    conn_or_store: Any, ref_id: int, blocks: Sequence[Block]
) -> None:
    """Write ``blocks`` — the full assembled prompt input — onto ``ref_id``'s meta.

    ``conn_or_store`` accepts either an already-open :class:`~psycopg.Connection`
    (folded into the caller's transaction — the caller commits) or a
    :class:`~precis.store.Store` (opens + commits its own short-lived
    connection) — callers pass whichever they already have in hand.

    A falsy/empty ``blocks`` is a silent no-op (nothing to capture — e.g. a
    bare stand-in prompt object in a test). Any other failure (malformed
    blocks, a DB hiccup) is logged and swallowed: capture must never be able
    to sink the tick/pass it's riding alongside (these run unattended across
    the fleet).
    """
    try:
        if not blocks:
            return
        meta = {
            "assembled_context": _capped_entries(blocks),
            "assembled_context_at": datetime.now(UTC).isoformat(),
        }
        if hasattr(conn_or_store, "pool"):
            with conn_or_store.pool.connection() as conn:
                _write(conn, ref_id, meta)
                conn.commit()
        else:
            _write(conn_or_store, ref_id, meta)
    except Exception:
        log.exception(
            "persist_assembled_context: failed to capture assembled context "
            "for ref_id=%s",
            ref_id,
        )


__all__ = ["persist_assembled_context"]
