"""Ref-level claim-time attempt lease — the ref-pass analog of ``chunk_claims``.

Background (OPEN-ITEMS "Unbraked LLM-pass cluster"): a chunk-level pass
(``classify.py``, the chunk-level path of ``axis_pass.py``) leases each chunk
in ``chunk_claims`` BEFORE the LLM call, so a raise (or a hard crash mid-call)
still leaves the row braked — it can't be re-claimed and re-billed every
sweep. A **ref-level** pass (``classify_topics``, the ref-level path of
``axis_pass``, ``paper_glossary``) has no such lease table; its only "done"
signal is a SUCCESS-written marker (a ref tag, or a marker chunk), so a
persistently-failing ref (dead endpoint, breaker refusal, unparseable JSON)
got re-fetched and re-LLM'd every sweep, unbounded.

Fix: a ``ref_tags`` row with a TTL (``expires_at`` — migration 0010),
written to the ref BEFORE the LLM call runs, that a pass's own candidate
query excludes while unexpired:

* **failure** — the lease survives (nothing clears it), braking the ref for
  :data:`ATTEMPT_COOLDOWN_MIN` regardless of outcome — bounded retries
  instead of every sweep.
* **success** — the pass's own success-write clears the lease in the SAME
  transaction (:func:`clear_attempt`), so an unrelated re-trigger (a
  version bump, a toggled enabled-topic set) is never blocked by a stale
  lease left over from an earlier successful run.

``hub_refine.py`` needs a variant of this same idea but tied into its own
due-set predicate (not a flat candidate query), so it implements its own
claim-time lease rather than importing this module — see its docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.store.types import Tag

#: How long a claim-time attempt lease brakes a failing ref before it's
#: reclaimable again. These passes are batched, infrequent corpus-wide
#: backfills (not a hot per-minute loop), so a generous window bounds $
#: burn without needing a separate reaper — and a successful retry clears
#: the lease immediately anyway (see module docstring).
ATTEMPT_COOLDOWN_MIN = 60


def attempt_ns(marker_ns: str) -> str:
    """The attempt-lease tag namespace for a pass's own marker namespace."""
    return f"{marker_ns}ATTEMPT"


def stamp_attempt(store: Any, ref_id: int, marker_ns: str, *, conn: Any) -> None:
    """Write/refresh the claim-time attempt lease for ``ref_id``.

    Call BEFORE the LLM call, in its own short committed transaction ahead
    of the risky call (mirrors ``chunk_claims``' claim-then-release-the-lock
    ordering) — so a raise, or a hard crash mid-call, still leaves the brake
    in place. Idempotent: a re-stamp within the cooldown just refreshes
    ``expires_at``.
    """
    store.add_tag(
        ref_id,
        Tag.closed(attempt_ns(marker_ns), "1"),
        set_by="agent",
        expires_at=datetime.now(UTC) + timedelta(minutes=ATTEMPT_COOLDOWN_MIN),
        conn=conn,
    )


def clear_attempt(store: Any, ref_id: int, marker_ns: str, *, conn: Any) -> None:
    """Drop the attempt lease — call as part of the pass's own success write
    (same transaction) so a later legitimate re-trigger (version bump,
    toggled enabled-set) is never blocked by a stale lease left over from an
    earlier successful run. A no-op if no lease is present."""
    store.remove_tag(ref_id, Tag.closed(attempt_ns(marker_ns), "1"), conn=conn)


def exclude_clause(ref_col: str, param: str) -> str:
    """``AND NOT EXISTS (...)`` SQL excluding a ref currently under an
    unexpired attempt lease. ``ref_col`` is the ref_id column reference in
    the caller's query (e.g. ``"r.ref_id"``); ``param`` is the bind-param
    name the caller will supply the lease's namespace under (typically
    ``attempt_ns(marker_ns)``)."""
    return (
        "AND NOT EXISTS (SELECT 1 FROM ref_tags rlt JOIN tags rlta "
        "ON rlta.tag_id = rlt.tag_id "
        f"WHERE rlt.ref_id = {ref_col} AND rlta.namespace = %({param})s "
        "AND (rlt.expires_at IS NULL OR rlt.expires_at > now()))"
    )


__all__ = [
    "ATTEMPT_COOLDOWN_MIN",
    "attempt_ns",
    "clear_attempt",
    "exclude_clause",
    "stamp_attempt",
]
