"""hub_refine — periodic, converging enrichment of existing taproot claim hubs.

Build ticket: ``docs/proposals/taproot-hub-refine.md``. Closes a gap the
shipped taproot phases leave open: evidence attaches to a claim hub only as
a side effect of chasing a *finding* (the forward bridge,
``workers/chase.py``'s ``_taproot_bridge``) or when a human hands
``precis taproot mint`` a supporter list. Neither ever looks at an
**existing** hub and asks "what else in the corpus supports this claim?" —
so a hub minted off a single draft cite sits at one corroborator forever,
even when the primary source already sits un-attached in the same corpus.

This pass does exactly that, per hub, claimed off a **due-set** rather than
a blind periodic rescan (the incremental-trigger design,
``workers/chase_trigger.py``):

1. **Claim** — ``TAPROOT:claim`` / ``STATUS:canonical`` findings due for
   refine (see :func:`_claim_hubs_due_for_refine` for the full predicate: a
   ``TAPROOT_DUE`` tag from the trigger pass, never-refined, an edited
   claim reopening it, or the long backstop), never-refined first, oldest
   ``last_refined_at`` next, ``SKIP LOCKED``, ``LIMIT`` :func:`_hubs_per_pass`
   — mirrors ``workers/inbound_chase.py``'s claim-query shape.
2. **Discover** — a semantic (embedding-ANN) search over paper body chunks
   for the claim sentence, top-``PRECIS_TAPROOT_REFINE_TOPK``. Note:
   *not* ``taproot.canon.block`` — that ANN is over hub *cards* (dedup
   against other hubs); this needs paper-*chunk* neighbours, the same
   ``store.search_blocks`` engine ``PaperHandler.search`` drives (see
   ``handlers/_paper_search.py``), run in ``mode='semantic'`` so
   ``PRECIS_TAPROOT_REFINE_MIN_SIM`` (an optional cosine-distance floor)
   means what its name says.
3. **Filter** — drop a candidate paper already carrying a ``corroborates``
   edge on this hub (the idempotency precheck, done *before* any LLM
   spend) or already recorded in the hub's rejection memo
   (``meta['taproot_rejected']`` — a ``supports=no`` verdict from an
   earlier pass, judged once, never re-verified).
4. **Verify** — ``workers._chase_llm._verify_support_with_caveats`` per
   surviving candidate.
5. **Write** — ``supports`` in ``{yes, partial}`` → an evidence edge via
   ``taproot.hub.attach_evidence`` (role ``corroborates``, meta carrying
   ``support``/``caveats``/``source_handle``); ``supports == "no"`` →
   append to the rejection memo. Either way the candidate is judged once.
6. **Stamp** — ``meta.last_refined_at`` and ``meta.last_refined_sha`` (the
   claim sentence's :func:`taproot.canon.claim_sha` at refine time) are set
   unconditionally (even an empty pass with zero new candidates), so the
   claim query's ``never-refined`` / ``never-reopened`` conditions hold and
   the hub naturally drains out of the due-set until something re-marks it
   (a new near paper, a title edit, or the backstop). A **sha-reopen** — the
   stored ``last_refined_sha`` no longer matches the live title — clears the
   rejection memo *before* discovery: the claim itself changed, so an old
   ``supports=no`` verdict on the previous wording may no longer hold.

Never a periodic full re-scan: idempotent attach + pre-verify existence
check + rejection memo + due-set claim query together bound the per-run LLM
spend to (at most) ``HUBS_PER_PASS x TOPK`` calls, in practice far less
once memos fill in. See the build ticket's "Non-negotiable: it must
converge" for the full rationale.

Ship dark: ``PRECIS_TAPROOT_REFINE_ENABLED`` (default ``"0"``), like every
other taproot flag. Once claiming work, the pass always verifies with the
LLM — there is no separate ``with_llm`` toggle here (unlike ``chase``): a
hub-refine run that can't verify can't do anything, so reaching this pass
at all already implies paying for it. The one hard dependency is the
embedder (discovery needs a query vector); if none is wired the pass logs
a warning and no-ops for the whole cycle (mirrors the forward bridge's own
embedder-unavailable degrade, ``workers/chase.py``'s ``_taproot_bridge``).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection

from precis.store.types import Tag
from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE, claim_sha
from precis.taproot.hub import HUB_ROLES, attach_evidence
from precis.utils.embed_query import embed_query
from precis.workers._chase_llm import _verify_support_with_caveats

log = logging.getLogger(__name__)

#: Mirrors ``taproot.hub``'s own private ``STATUS:canonical`` marker
#: (``_STATUS_NS`` / ``_STATUS_CANONICAL`` there) — duplicated as plain
#: strings here rather than importing hub.py's underscored names, the
#: same call ``workers/inbound_chase.py`` makes for its own ``INBOUND``
#: namespace constants.
_STATUS_NAMESPACE = "STATUS"
_STATUS_CANONICAL = "canonical"

#: The evidence-edge role hub-refine always attaches with — never
#: ``establishes`` (originator promotion is derived elsewhere, see the
#: module docstring's "Originators are still derived" note in the build
#: ticket).
_ROLE = "corroborates"

#: ``finding.meta`` keys this pass reads/writes.
_META_LAST_REFINED_AT = "last_refined_at"
_META_LAST_REFINED_SHA = "last_refined_sha"
_META_REJECTED = "taproot_rejected"

#: The trigger pass's due-marker (``workers/chase_trigger.py``) — a closed
#: ref tag on a claim hub meaning "a new near paper landed, refresh me".
#: Popped here at claim time (see :func:`_claim_hubs_due_for_refine`).
_DUE_NS = "TAPROOT_DUE"
_DUE_VALUE = "1"


def hub_refine_enabled() -> bool:
    """``PRECIS_TAPROOT_REFINE_ENABLED`` — default OFF (dark, like every
    other taproot flag)."""
    return bool(int(os.environ.get("PRECIS_TAPROOT_REFINE_ENABLED", "0") or "0"))


def _backstop_hours() -> float:
    """``PRECIS_TAPROOT_REFINE_BACKSTOP_H`` — default **2160** (90d).

    Replaces the old ``PRECIS_TAPROOT_REFINE_INTERVAL_H`` weekly cadence
    now that the incremental trigger (``workers/chase_trigger.py``) marks a
    hub due promptly off a real corpus change: this is a LONG backstop so
    nothing is ever permanently stuck if a ``TAPROOT_DUE`` tag is lost to a
    failed pass, not a scheduling cadence.
    """
    try:
        return float(os.environ.get("PRECIS_TAPROOT_REFINE_BACKSTOP_H", "2160"))
    except ValueError:
        return 2160.0


def _hubs_per_pass() -> int:
    try:
        return int(os.environ.get("PRECIS_TAPROOT_REFINE_HUBS_PER_PASS", "8"))
    except ValueError:
        return 8


def _topk_default() -> int:
    try:
        return int(os.environ.get("PRECIS_TAPROOT_REFINE_TOPK", "8"))
    except ValueError:
        return 8


def _min_sim_default() -> float | None:
    """Optional cosine-distance floor — unset by default (no floor)."""
    raw = os.environ.get("PRECIS_TAPROOT_REFINE_MIN_SIM")
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ── claim query ────────────────────────────────────────────────────


def _is_hub_due(
    *,
    is_due_tagged: bool,
    last_refined_at: str | None,
    last_refined_sha: str | None,
    title: str,
    backstop_h: float,
) -> bool:
    """The due predicate — any of:

    1. carries a ``TAPROOT_DUE`` tag (the trigger pass marked a new near
       paper), or
    2. never refined (``last_refined_at`` absent), or
    3. edited since last refine (stored ``last_refined_sha`` absent or no
       longer matches the live title's :func:`taproot.canon.claim_sha` —
       a reopen), or
    4. the long backstop has elapsed (nothing is ever permanently stuck if
       a due-tag is lost to a failed pass).
    """
    if is_due_tagged:
        return True
    if last_refined_at is None:
        return True
    if last_refined_sha is None or last_refined_sha != claim_sha(title):
        return True
    try:
        refined_at = datetime.fromisoformat(last_refined_at)
    except (TypeError, ValueError):
        # Malformed timestamp -- fail open (due) rather than get stuck.
        return True
    if refined_at.tzinfo is None:
        refined_at = refined_at.replace(tzinfo=UTC)
    return refined_at < datetime.now(UTC) - timedelta(hours=backstop_h)


def _claim_hubs_due_for_refine(
    conn: Connection, store: Any, *, limit: int, backstop_h: float
) -> list[int]:
    """Lock and return up to ``limit`` claim-hub ``ref_id``s due for refine.

    Due-set claim query (see :func:`_is_hub_due`), never-refined first,
    oldest ``last_refined_at`` next, ``SKIP LOCKED``. The sha-reopen check
    isn't SQL-computable (:func:`taproot.canon.claim_sha` is Python-side
    blake2b), so this scans the whole (small, ~1.2k) canonical claim-hub
    set, computes due-ness in Python, then re-locks just the winning
    ``limit`` ids with a second ``FOR UPDATE SKIP LOCKED`` — a concurrent
    pass already holding one of those rows drops it from the returned set,
    same concurrency-safety as the old single-query form.

    Pops each claimed hub's ``TAPROOT_DUE`` tag in this same call (the
    work-queue pop) — if a new chunk re-marks the hub mid-processing, it
    simply re-triggers next pass.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, r.meta,
               EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(due_ns)s
                    AND t.value = %(due_value)s
               ) AS is_due_tagged
          FROM refs r
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
        """,
        {
            "due_ns": _DUE_NS,
            "due_value": _DUE_VALUE,
            "taproot_ns": TAPROOT_NAMESPACE,
            "taproot_claim": TAPROOT_CLAIM,
            "status_ns": _STATUS_NAMESPACE,
            "status_canonical": _STATUS_CANONICAL,
        },
    ).fetchall()

    candidates: list[tuple[int, str | None]] = []
    for ref_id, title, meta, is_due_tagged in rows:
        meta = dict(meta or {})
        last_refined_at = meta.get(_META_LAST_REFINED_AT)
        last_refined_sha = meta.get(_META_LAST_REFINED_SHA)
        if _is_hub_due(
            is_due_tagged=bool(is_due_tagged),
            last_refined_at=last_refined_at,
            last_refined_sha=last_refined_sha,
            title=str(title or ""),
            backstop_h=backstop_h,
        ):
            candidates.append((int(ref_id), last_refined_at))

    # never-refined first, then oldest last_refined_at, then ref_id for a
    # deterministic tie-break -- mirrors the old ORDER BY ... NULLS FIRST.
    candidates.sort(key=lambda item: (item[1] is not None, item[1] or "", item[0]))
    candidate_ids = [ref_id for ref_id, _ in candidates[:limit]]
    if not candidate_ids:
        return []

    locked_rows = conn.execute(
        "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) FOR UPDATE SKIP LOCKED",
        (candidate_ids,),
    ).fetchall()
    priority = {ref_id: i for i, ref_id in enumerate(candidate_ids)}
    locked_ids = sorted((int(r[0]) for r in locked_rows), key=lambda rid: priority[rid])

    for ref_id in locked_ids:
        store.remove_tag(ref_id, Tag.closed(_DUE_NS, _DUE_VALUE), conn=conn)

    return locked_ids


def _fetch_hub_info(conn: Connection, ref_id: int) -> tuple[str, dict[str, Any]] | None:
    """``(title, meta)`` for a live hub finding — ``None`` if it's gone."""
    row = conn.execute(
        "SELECT title, meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0] or ""), dict(row[1] or {})


# ── per-hub refine ─────────────────────────────────────────────────


def _attached_paper_ids(conn: Connection, hub_ref_id: int) -> set[int]:
    """Paper ref_ids already carrying an evidence edge (any ``HUB_ROLES``
    role) on this hub.

    Hub-refine dedups at the **paper** level — a supporter is a paper, not a
    passage — so a paper already attached via *any* grounding chunk is
    skipped before verify. This is load-bearing under the chunk-scoped edge
    model (evidence edges now carry ``src_chunk_id``): a per-chunk
    ``_evidence_edge_exists`` check would miss the same paper re-surfaced via
    a *different* chunk on a later pass and re-verify + re-attach it every
    run — breaking convergence. One query per hub also collapses the old
    per-candidate ``_evidence_edge_exists`` connection-per-call N+1.
    """
    rows = conn.execute(
        "SELECT DISTINCT src_ref_id FROM links "
        "WHERE dst_ref_id = %s AND relation = ANY(%s)",
        (hub_ref_id, list(HUB_ROLES)),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _refine_one_hub(
    conn: Connection,
    store: Any,
    hub_ref_id: int,
    *,
    embedder: Any,
    topk: int,
    min_sim: float | None,
) -> None:
    """Discover + verify + attach corroborators for one hub, then stamp it.

    Always writes ``meta.last_refined_at`` + ``meta.last_refined_sha`` on
    the way out (even when the hub's title is blank, or discovery/verify
    finds nothing new) — that unconditional stamp is what makes the claim
    query's due-set conditions (never-refined, sha-match) hold (see
    :func:`_claim_hubs_due_for_refine`).

    A **sha-reopen** — the stored ``last_refined_sha`` no longer matches
    the live title's :func:`taproot.canon.claim_sha` — clears the
    rejection memo *before* discovery/verify run: the claim wording
    changed, so an old ``supports=no`` verdict may no longer hold.
    """
    info = _fetch_hub_info(conn, hub_ref_id)
    if info is None:
        # Ref vanished between claim and processing — nothing to stamp.
        return
    title, meta = info
    claim_sentence = title.strip()
    scope = dict(meta.get("scope") or {})
    new_sha = claim_sha(title)
    stored_sha = meta.get(_META_LAST_REFINED_SHA)
    reopened = stored_sha is not None and stored_sha != new_sha
    rejected: dict[str, Any] = {} if reopened else dict(meta.get(_META_REJECTED) or {})
    attached = _attached_paper_ids(conn, hub_ref_id)

    query_vec = embed_query(embedder, claim_sentence) if claim_sentence else None
    if claim_sentence and query_vec is None:
        log.warning(
            "hub_refine: embed returned no vector for hub #%d -- skipping "
            "discovery this pass rather than silently degrading to lexical",
            hub_ref_id,
        )
    if claim_sentence and query_vec is not None:
        candidates = store.search_blocks(
            q=claim_sentence,
            query_vec=query_vec,
            mode="semantic",
            kind="paper",
            limit=topk,
            max_distance=min_sim,
        )
        seen_papers: set[int] = set()
        for block, ref, _score in candidates:
            paper_ref_id = int(ref.id)
            if paper_ref_id == hub_ref_id or paper_ref_id in seen_papers:
                continue
            seen_papers.add(paper_ref_id)
            # Precheck BEFORE verify (idempotency + rejection memo): a paper
            # already an evidence supporter of this hub (any role, any
            # grounding chunk) or already judged ``no`` must never cost
            # another LLM call. Paper-level, not chunk-level — see
            # _attached_paper_ids.
            if paper_ref_id in attached or str(paper_ref_id) in rejected:
                continue
            verification = _verify_support_with_caveats(
                claim=claim_sentence,
                scope=scope,
                target_cite_key=ref.slug or f"ref:{paper_ref_id}",
                target_chunk_ord=block.pos,
                target_chunk_text=block.text,
            )
            if verification is None:
                # Transient LLM/dispatch failure — no verdict recorded,
                # so this candidate is simply retried next pass (neither
                # attached nor memoed as rejected).
                continue
            supports = verification.get("supports")
            if supports in ("yes", "partial"):
                attach_evidence(
                    store,
                    hub_ref_id=hub_ref_id,
                    paper_ref_id=paper_ref_id,
                    role=_ROLE,
                    meta={
                        "support": supports,
                        "caveats": list(verification.get("caveats") or []),
                        "source_handle": f"pc{block.id}",
                    },
                    set_by="system",
                    conn=conn,
                )
            elif supports == "no":
                rejected[str(paper_ref_id)] = {
                    "at": datetime.now(UTC).isoformat(),
                    "supports": "no",
                }
            else:
                # Verdict outside the {yes,partial,no} enum (missing key or
                # an LLM-schema regression) — neither attach nor memo, so it
                # retries next cadence; log so the regression isn't invisible.
                log.warning(
                    "hub_refine: hub #%d candidate paper #%d got unexpected "
                    "verify verdict %r -- skipped (retries next cadence)",
                    hub_ref_id,
                    paper_ref_id,
                    supports,
                )

    meta_patch: dict[str, Any] = {
        _META_LAST_REFINED_AT: datetime.now(UTC).isoformat(),
        _META_LAST_REFINED_SHA: new_sha,
    }
    if rejected or reopened:
        # Always persist on a reopen, even an emptied memo -- that's the
        # clear taking effect, not just skipped because nothing's pending.
        meta_patch[_META_REJECTED] = rejected
    store.update_ref(hub_ref_id, meta_patch=meta_patch, conn=conn)


# ── runner ─────────────────────────────────────────────────────────


def run_hub_refine_pass(
    store: Any,
    *,
    limit: int | None = None,
    embedder: Any | None = None,
    topk: int | None = None,
    min_sim: float | None = None,
) -> dict[str, int]:
    """One pass: claim due hubs, discover + verify + attach corroborators.

    Every keyword defaults to its ``PRECIS_TAPROOT_REFINE_*`` env knob
    when omitted (see the module-level ``_*_default``/``_*_hours``
    readers) — tests pass them explicitly to stay independent of the
    process environment. The due-set backstop (``PRECIS_TAPROOT_REFINE_
    BACKSTOP_H``, see :func:`_backstop_hours`) has no override kwarg here
    — it's a rarely-hit safety net, not a per-run tuning knob; tests that
    need to force it monkeypatch the env var.

    ``embedder=None`` degrades the whole pass to a logged no-op: no hubs
    are even claimed, since discovery has nothing to search with. This
    mirrors the forward bridge's (``workers/chase.py``) own
    embedder-unavailable degrade rather than silently falling through to
    ``store.search_blocks``'s internal lexical fallback, which would
    quietly turn "ANN over paper chunks" into a much weaker keyword
    match without ever telling the operator.

    Returns the standard ``{claimed, ok, failed}`` shape.
    """
    if embedder is None:
        log.warning("hub_refine: no embedder available -- pass degrades to a no-op")
        return {"claimed": 0, "ok": 0, "failed": 0}

    resolved_limit = limit if limit is not None else _hubs_per_pass()
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_backstop_h = _backstop_hours()
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()

    with store.pool.connection() as conn:
        hub_ids = _claim_hubs_due_for_refine(
            conn, store, limit=resolved_limit, backstop_h=resolved_backstop_h
        )
        conn.commit()

    claimed = len(hub_ids)
    ok = 0
    failed = 0

    for hub_ref_id in hub_ids:
        try:
            with store.pool.connection() as conn:
                _refine_one_hub(
                    conn,
                    store,
                    hub_ref_id,
                    embedder=embedder,
                    topk=resolved_topk,
                    min_sim=resolved_min_sim,
                )
                conn.commit()
            ok += 1
        except Exception:  # pragma: no cover — defensive, mirrors inbound_chase.py
            log.warning(
                "hub_refine: refine failed for hub #%d", hub_ref_id, exc_info=True
            )
            failed += 1

    return {"claimed": claimed, "ok": ok, "failed": failed}


__all__ = [
    "hub_refine_enabled",
    "run_hub_refine_pass",
]
