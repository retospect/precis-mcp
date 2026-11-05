"""Shared scope predicate for embedding work orders.

An ``embed_batch`` with a ``params.memory_target`` key can only process its
pinned memory revision. Key presence, including a null or malformed value,
selects targeted dispatch; validation failure never falls back to bulk work.
Such jobs cannot cover corpus backlog, coalesce a fresh-finding nudge, or
start or clear the bulk failure cooldown. Corpus-backlog diagnostics also
exclude their activity and outcomes. Missing/null params remain bulk.
"""

from __future__ import annotations


def targeted_embed_job_sql(meta: str = "r.meta") -> str:
    """SQL predicate over a trusted metadata column expression, never user input."""
    return (
        f"(COALESCE({meta}->>'job_type' = 'embed_batch', false) "
        f"AND COALESCE({meta}->'params' ? 'memory_target', false))"
    )
