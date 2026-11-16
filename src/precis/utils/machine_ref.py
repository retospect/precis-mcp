"""Machine-ref identity + auto-creation helpers (gr592809).

The ``machine`` kind's slug addressing and the heartbeat-side census
maintenance. Lives in ``utils`` (not ``handlers.machine``) because the
heartbeat reporter (:mod:`precis.workers.heartbeat`) must reach it and
workers may not import handlers (import-linter contract); the handler
re-exports :func:`slug_for` / :func:`host_for` for compat.

gr592809 slice b: a beat from an unseen, non-retired host creates its
``ma_<host>`` ref — the fleet census maintains itself instead of waiting
for a hand-run ``put(kind='machine', mode='sync')``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

from precis.store.types import ChunkInsert

_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")
_SLUG_PREFIX = "ma_"

#: meta keys a manual put / sync may carry — the stable facts the ref
#: body is allowed to hold (gr549585 ruling: platform, role, GPU; host
#: is identity; release rides with platform as collector-reported).
FACT_KEYS = ("platform", "release", "role", "gpu")


def slug_for(host: str) -> str:
    """``<host>`` → ``ma_<host>`` — the machine ref's public id.

    Lowercase, one sanitising pass over anything outside
    ``[a-z0-9._-]`` so a heartbeat-reported hostname lands a clean slug.
    Returns ``""`` for an empty/unsalvageable host — callers treat that
    as a bad input, never mint.
    """
    norm = _SLUG_BAD.sub("-", host.strip().lower()).strip("-.")
    return f"{_SLUG_PREFIX}{norm}" if norm else ""


def host_for(id: str) -> str:
    """``ma_<host>`` or a bare ``<host>`` → the bare host name."""
    s = id.strip()
    return s[len(_SLUG_PREFIX) :] if s.startswith(_SLUG_PREFIX) else s


def card_text(host: str, meta: dict[str, Any]) -> str:
    """The embedded card — stable facts ONLY.

    Live fields (load, temp_c, utilisation means) must never reach this
    text: it lands in ``chunks`` and ``chunk_embeddings``, where a
    one-minute collector would churn the graph. Facts come from ``meta``
    alone.
    """
    lines = [f"machine {host}"]
    for k in FACT_KEYS:
        v = meta.get(k)
        if v not in (None, ""):
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _host_retired(store: Store, host: str) -> bool:
    """The current ``host_heartbeat`` row's retirement marker, if any."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'retired' IS NOT NULL FROM host_heartbeat WHERE host = %s",
            (host,),
        ).fetchone()
    return bool(row and row[0])


def ensure_machine_ref(store: Store, host: str, *, ephemeral: bool = False) -> bool:
    """Create the ``ma_<host>`` ref (+ its card chunk) when absent.

    Called by the heartbeat reporter each beat so the machine census
    maintains itself (gr592809 slice b — no manual ``mode='sync'``).
    Returns True when a ref was created, False otherwise. Facts
    (platform/release) are NOT written here — a factless auto-create
    never clobbers a hand-set value, and the next ``mode='sync'``
    refreshes facts as before.

    Never creates when:

    - the host name yields no usable slug;
    - the beat is ``ephemeral`` (gr306275 — an unset Docker container ID
      would mint a throwaway ref nobody ever retires);
    - the current ``host_heartbeat`` row carries the retirement marker
      (``precis heartbeat --retire``). Call BEFORE the caller's beat
      upsert: the upsert clears the marker (a returning host is not
      retired), so reading after it would resurrect a decommissioned
      machine on its first beat back. As ordered, the ref reappears on
      the host's SECOND beat.
    - the ref already exists (one indexed read per beat, then nothing).
    """
    host = host.strip()
    slug = slug_for(host)
    if not slug or ephemeral:
        return False
    if _host_retired(store, host):
        return False
    if store.get_ref(kind="machine", id=slug) is not None:
        return False
    meta: dict[str, Any] = {"host": host}
    card_block = ChunkInsert(
        ord=-1,
        text=card_text(host, meta),
        meta={"chunk_kind": "card_combined"},
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="machine", slug=slug, title=host, meta=meta, conn=conn
        )
        store.chunks.insert_chunks(ref.id, [card_block], replace=True, conn=conn)
    return True


__all__ = ["FACT_KEYS", "card_text", "ensure_machine_ref", "host_for", "slug_for"]
