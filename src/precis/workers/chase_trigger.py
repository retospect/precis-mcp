"""chase_trigger — mark taproot claim hubs due when a near paper chunk lands.

Build ticket: taproot chase incremental trigger, Phase 1. ``hub_refine``
(``workers/hub_refine.py``) enriches existing claim hubs, but on a blind
weekly rescan of every hub — expensive and slow to notice a freshly-embedded
corroborator sitting right next to a hub's claim sentence. This pass closes
that gap with a **reverse ANN**: instead of searching the (huge) chunk corpus
per hub, it indexes the (tiny, ~1.2k) claim-hub set and probes *that* index
per newly-embedded chunk, marking any near claim ``TAPROOT_DUE`` so
``hub_refine``'s claim query picks it up promptly instead of waiting out the
weekly-turned-90d backstop.

One pass, four steps. Step (a) is its own committed unit; steps (b)+(c)+(d)
run in ONE transaction (all-or-nothing — a mid-batch failure rolls the whole
sweep back and it re-claims next pass, so a chunk is never marked swept
without having been matched):

(a) **Refresh claim_embeddings** — upsert a vector (embedded from the claim
    sentence, i.e. the hub's ``title``) for every canonical claim hub whose
    stored ``claim_embeddings`` row is missing or whose ``claim_sha`` no
    longer matches the live title (an edited claim). Shape mirrors
    ``tag_embeddings`` (``migration 0101``'s own docstring): one row per
    ``(claim_ref_id, embedder)``, ``claim_sha`` gating re-embed the same way
    ``hub_refine`` gates a reopen off the same hash (:func:`taproot.canon.claim_sha`
    — the two passes share the helper so "changed" means the same thing to
    both).

(b) **Claim a chunk batch** — up to ``batch_size`` embedded ``paper``/
    ``patent`` body chunks that don't yet carry a ``CHASETRIG:<version>``
    chunk tag (the classify/classify_topics done-marker idiom — no lease
    table, existence of the marker tag IS the claim).

(c) **Match + mark** — one set-based query crosses the claimed chunks'
    vectors against ``claim_embeddings`` (a flat scan — the table is small
    enough per migration 0101's own comment) for any pair within
    :func:`_min_sim_default`'s cosine-distance floor. Every distinct near
    claim (excluding a claim hub matching its own source chunk) gets a
    closed ``TAPROOT_DUE`` ref tag — idempotent, popped by ``hub_refine``
    when it claims the hub.

**Compound exclusion** (docs/backlog/taproot-atomic-claims.md): both (a)'s
hub query and (c)'s probe query exclude compound claim hubs (a live inbound
``conjunct-of`` edge from a live finding) — same predicate
``hub_refine._is_compound_hub``/``_claim_hubs_due_for_refine`` apply,
deliberately re-derived rather than shared (the "cross-task seam"
precedent: ``seniority._is_claim_hub`` mirrors ``hub._is_claim_hub``). A
compound excluded from ``hub_refine``'s own due-set query must never be
embedded/probed/marked ``TAPROOT_DUE`` here either — a due-mark that
``hub_refine`` would never claim (it's excluded there too) would just
accumulate on the hub forever, an unpopped tag with no consumer. Existing
``claim_embeddings`` rows for a hub that later becomes compound are left in
place (no deletion pass) — harmless once (a) stops refreshing them and (c)
stops probing against them.

(d) **Mark every claimed chunk swept** — matched or not, in the same
    transaction as (b)+(c). This is what drains the queue and guarantees
    convergence: a chunk that matched nothing is still marked so it's never
    re-probed (a chunk whose batch *failed* is NOT, so it retries). Bump
    :data:`CHASETRIG_VERSION` to force a lazy re-sweep of the whole corpus
    (e.g. after a claim-embedding scheme change).

Ship dark: :func:`chase_trigger_enabled` defaults OFF
(``PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED``), like every other taproot flag.
No embedder wired -> the whole pass degrades to a logged no-op (mirrors
``hub_refine``'s own embedder-unavailable degrade).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from psycopg import Connection

from precis.store.types import Tag
from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE, claim_sha
from precis.utils.embed_query import embed_query
from precis.workers.hub_refine import _STATUS_CANONICAL, _STATUS_NAMESPACE

log = logging.getLogger(__name__)

#: Bump to force a lazy re-sweep of the whole corpus (every chunk re-probed
#: against the current claim-embedding index).
CHASETRIG_VERSION = "2"
_CHASETRIG_NS = "CHASETRIG"

#: A due claim hub carries this closed ref tag until ``hub_refine`` pops it
#: at claim time.
_DUE_NS = "TAPROOT_DUE"
_DUE_VALUE = "1"

#: The ref kinds a corroborator can come from -- bounds the sweep to the
#: same universe hub_refine's own discovery draws candidates from.
_CORROBORATOR_KINDS = ["paper", "patent"]


def chase_trigger_enabled() -> bool:
    """``PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`` -- default OFF (dark, like
    every other taproot flag)."""
    return bool(int(os.environ.get("PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED", "0") or "0"))


def _min_sim_default() -> float:
    """``PRECIS_TAPROOT_TRIGGER_MIN_SIM`` -- cosine-distance floor (max
    distance) for a chunk<->claim match.

    Default **0.45**: this is the loose-biased MVP default -- over-triggering
    is cheap (``hub_refine`` still prechecks attached/memoed before any LLM
    spend), under-triggering silently misses corroborators, so bias loose.
    TODO(Phase 2): tune against the slice harness
    (``taproot/slice_refine_eval.py``).
    """
    raw = os.environ.get("PRECIS_TAPROOT_TRIGGER_MIN_SIM")
    if raw is None or not raw.strip():
        return 0.45
    try:
        return float(raw)
    except ValueError:
        return 0.45


def _batch_size_default() -> int:
    try:
        return int(os.environ.get("PRECIS_TAPROOT_CHASE_TRIGGER_BATCH_SIZE", "200"))
    except ValueError:
        return 200


def _claim_refresh_limit_default() -> int:
    try:
        return int(
            os.environ.get("PRECIS_TAPROOT_CHASE_TRIGGER_CLAIM_REFRESH_LIMIT", "64")
        )
    except ValueError:
        return 64


# ── (a) claim_embeddings refresh ──────────────────────────────────────


def _refresh_claim_embeddings(
    conn: Connection, embedder: Any, embedder_model: str, *, limit: int
) -> int:
    """Upsert a fresh vector for every canonical claim hub whose stored
    ``claim_embeddings`` row is missing or stale (``claim_sha`` mismatch).

    Flat scan over the whole (tiny) claim-hub set, filtered/capped in
    Python -- the sha comparison isn't SQL-computable (see
    :func:`taproot.canon.claim_sha`), and migration 0101's own comment
    already sizes this table (~1.2k rows) as trivial to scan whole.

    Excludes **compound** claim hubs (docs/backlog/taproot-atomic-claims.md
    -- see the module docstring's "Compound exclusion" note): a compound's
    ``claim_embeddings`` row is never refreshed, so it never becomes a probe
    target for :func:`_near_claims` either.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, ce.claim_sha
          FROM refs r
          LEFT JOIN claim_embeddings ce
            ON ce.claim_ref_id = r.ref_id AND ce.embedder = %(embedder)s
         WHERE r.kind = 'finding'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(taproot_ns)s
                    AND t.value = %(taproot_claim)s
               )
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(status_ns)s
                    AND t.value = %(status_canonical)s
               )
           AND NOT EXISTS (
                 SELECT 1 FROM links l
                  JOIN refs a ON a.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = r.ref_id
                   AND l.relation = 'conjunct-of'
                   AND a.kind = 'finding'
                   AND a.deleted_at IS NULL
               )
         ORDER BY r.ref_id
        """,
        {
            "embedder": embedder_model,
            "taproot_ns": TAPROOT_NAMESPACE,
            "taproot_claim": TAPROOT_CLAIM,
            "status_ns": _STATUS_NAMESPACE,
            "status_canonical": _STATUS_CANONICAL,
        },
    ).fetchall()

    refreshed = 0
    for ref_id, title, stored_sha in rows:
        if refreshed >= limit:
            break
        title_str = str(title or "").strip()
        if not title_str:
            continue
        sha = claim_sha(title_str)
        if stored_sha == sha:
            continue  # already current -- nothing to do
        vec = embed_query(embedder, title_str)
        if vec is None:
            continue
        conn.execute(
            """
            INSERT INTO claim_embeddings (claim_ref_id, embedder, claim_sha, vector)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (claim_ref_id, embedder) DO UPDATE
              SET claim_sha = EXCLUDED.claim_sha,
                  vector = EXCLUDED.vector,
                  embedded_at = now()
            """,
            (int(ref_id), embedder_model, sha, vec),
        )
        refreshed += 1
    return refreshed


# ── (b) claim a chunk batch to sweep ──────────────────────────────────


def _claim_chunks_to_sweep(
    conn: Connection, *, embedder_model: str, batch_size: int
) -> list[tuple[int, int, int]]:
    """Up to ``batch_size`` ``(chunk_id, ref_id, ord)`` rows for embedded
    body chunks of a ``paper``/``patent`` ref not yet swept at the current
    :data:`CHASETRIG_VERSION`.

    No lease table -- the ``CHASETRIG:<version>`` chunk tag written in step
    (d) IS the durable done-marker (mirrors ``classify``/``classify_topics``).
    ``FOR UPDATE OF c SKIP LOCKED`` keeps concurrent trigger instances (the
    "system" worker runs on every node) from selecting the same batch within
    a pass; the caller holds this lock through match + mark + commit, so the
    marker lands before the lock releases.
    """
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.ref_id, c.ord
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
          JOIN chunk_embeddings ce
            ON ce.chunk_id = c.chunk_id
           AND ce.embedder = %(embedder)s
           AND ce.status = 'ok'
         WHERE r.kind = ANY(%(kinds)s)
           AND r.deleted_at IS NULL
           AND c.ord >= 0
           AND c.retired_at IS NULL
           AND NOT EXISTS (
                 SELECT 1 FROM chunk_tags ct JOIN tags t USING (tag_id)
                  WHERE ct.chunk_id = c.chunk_id
                    AND t.namespace = %(chase_ns)s
                    AND t.value = %(chase_val)s
               )
         ORDER BY c.chunk_id
         LIMIT %(limit)s
           FOR UPDATE OF c SKIP LOCKED
        """,
        {
            "embedder": embedder_model,
            "kinds": _CORROBORATOR_KINDS,
            "chase_ns": _CHASETRIG_NS,
            "chase_val": CHASETRIG_VERSION,
            "limit": batch_size,
        },
    ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


# ── (c) match + mark ───────────────────────────────────────────────────


def _near_claims(
    conn: Connection,
    chunk_ids: list[int],
    *,
    embedder_model: str,
    floor: float,
    chunk_ref_map: dict[int, int],
) -> set[int]:
    """Distinct claim-hub ``ref_id``s within ``floor`` cosine distance of any
    chunk in ``chunk_ids``, excluding a claim matching its own source chunk
    (a claim hub's own card surfacing in its own sweep).

    Excludes **compound** claim hubs the same way :func:`_refresh_claim_
    embeddings` does upstream (docs/backlog/taproot-atomic-claims.md) --
    belt-and-suspenders: (a) already stops refreshing a compound's row, so
    this is normally a no-op filter, but a hub already carrying a stale row
    when it *becomes* compound (a decomposition landing between passes)
    must still never surface here, since ``hub_refine`` would never claim
    it to pop the due-mark this function would otherwise write.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ce.chunk_id, cl.claim_ref_id
          FROM chunk_embeddings ce
          JOIN claim_embeddings cl
            ON cl.embedder = %(embedder)s
           AND cl.vector IS NOT NULL
           AND (cl.vector <=> ce.vector) <= %(floor)s
         WHERE ce.chunk_id = ANY(%(chunk_ids)s)
           AND ce.embedder = %(embedder)s
           AND ce.status = 'ok'
           AND NOT EXISTS (
                 SELECT 1 FROM links l
                  JOIN refs a ON a.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = cl.claim_ref_id
                   AND l.relation = 'conjunct-of'
                   AND a.kind = 'finding'
                   AND a.deleted_at IS NULL
               )
        """,
        {"floor": floor, "chunk_ids": chunk_ids, "embedder": embedder_model},
    ).fetchall()
    near: set[int] = set()
    for chunk_id, claim_ref_id in rows:
        chunk_id = int(chunk_id)
        claim_ref_id = int(claim_ref_id)
        if chunk_ref_map.get(chunk_id) == claim_ref_id:
            continue
        near.add(claim_ref_id)
    return near


# ── runner ─────────────────────────────────────────────────────────────


def run_chase_trigger_pass(
    store: Any,
    *,
    embedder: Any | None,
    batch_size: int | None = None,
    min_sim: float | None = None,
    claim_refresh_limit: int | None = None,
) -> dict[str, int]:
    """One pass: refresh claim embeddings, sweep a chunk batch, mark near
    claims due, mark every swept chunk done.

    Every keyword defaults to its ``PRECIS_TAPROOT_CHASE_TRIGGER_*`` /
    ``PRECIS_TAPROOT_TRIGGER_*`` env knob when omitted -- tests pass them
    explicitly to stay independent of the process environment (mirrors
    ``hub_refine``'s own convention).

    ``embedder=None`` degrades the whole pass to a logged no-op, same
    degrade as ``hub_refine`` -- there is nothing to embed the claim
    sentences or compare against without one.

    Returns ``{claim_embeds, chunks_swept, due_marked, failed}``.
    """
    if embedder is None:
        log.warning("chase_trigger: no embedder available -- pass degrades to a no-op")
        return {"claim_embeds": 0, "chunks_swept": 0, "due_marked": 0, "failed": 0}

    resolved_batch_size = (
        batch_size if batch_size is not None else _batch_size_default()
    )
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()
    resolved_claim_refresh_limit = (
        claim_refresh_limit
        if claim_refresh_limit is not None
        else _claim_refresh_limit_default()
    )
    embedder_model = str(embedder.model)

    claim_embeds = 0
    failed = 0

    # (a) refresh stale/missing claim embeddings -- its own committed unit.
    try:
        with store.pool.connection() as conn:
            claim_embeds = _refresh_claim_embeddings(
                conn, embedder, embedder_model, limit=resolved_claim_refresh_limit
            )
            conn.commit()
    except Exception:
        log.warning("chase_trigger: claim-embedding refresh failed", exc_info=True)
        failed += 1

    # (b)+(c)+(d) sweep a chunk batch in ONE transaction: claim (FOR UPDATE
    # SKIP LOCKED, so concurrent instances never double-sweep a batch), match
    # near claims, mark them due, mark every claimed chunk swept, commit.
    # All-or-nothing on purpose: if any step raises, the whole batch rolls
    # back -- nothing marked due, nothing marked swept -- and is simply
    # re-claimed next pass. That's what keeps a transient failure (a DB blip,
    # a deadlock) from leaving chunks marked swept-but-never-matched, which
    # the CHASETRIG marker would otherwise make a *permanent* silent skip.
    # The lock is held across match+mark so the marker lands before release.
    due_marked = 0
    chunks_swept = 0
    try:
        with store.pool.connection() as conn:
            claimed = _claim_chunks_to_sweep(
                conn, embedder_model=embedder_model, batch_size=resolved_batch_size
            )
            if claimed:
                chunk_ref_map = {cid: rid for cid, rid, _ord in claimed}
                near = _near_claims(
                    conn,
                    list(chunk_ref_map),
                    embedder_model=embedder_model,
                    floor=resolved_min_sim,
                    chunk_ref_map=chunk_ref_map,
                )
                for claim_ref_id in near:
                    store.add_tag(
                        claim_ref_id,
                        Tag.closed(_DUE_NS, _DUE_VALUE),
                        set_by="system",
                        conn=conn,
                    )
                    due_marked += 1
                for _chunk_id, ref_id, ord_ in claimed:
                    store.add_tag(
                        ref_id,
                        Tag.closed(_CHASETRIG_NS, CHASETRIG_VERSION),
                        set_by="system",
                        pos=ord_,
                        conn=conn,
                    )
                    chunks_swept += 1
                conn.commit()
    except Exception:
        log.warning(
            "chase_trigger: sweep batch failed -- rolled back, retried next pass",
            exc_info=True,
        )
        failed += 1
        due_marked = 0
        chunks_swept = 0

    return {
        "claim_embeds": claim_embeds,
        "chunks_swept": chunks_swept,
        "due_marked": due_marked,
        "failed": failed,
    }


__all__ = ["chase_trigger_enabled", "run_chase_trigger_pass"]
