"""``reground_claim`` job_type — dispatch glue for the taproot reground
pass (``docs/backlog/taproot-reground.md``).

**Thin on purpose.** Every line of the actual mechanism lives in
:mod:`precis.workers.hub_refine` (audit, strict judge, deeper
re-discovery, the add-first applier) and :mod:`precis.taproot.hub` (the
edge-removal door). This module only resolves a hub scope, loops, and
checkpoints — the same shape ``taproot_backfill.py`` has. The DRY
invariant the spec exists to protect is that there is exactly ONE
mechanism that improves an existing claim hub; a job_type that started
judging or writing evidence itself would be the parallel producer it
forbids.

Two modes:

* ``mode='reground'`` (default) — run the pass per hub. The ``hub_ids``
  override bypasses the due-set entirely, so a draft's whole hub set can
  be regrounded on demand rather than waiting for the trigger pass.
* ``mode='verify'`` — the **intent-vs-committed diff**, promoted from the
  one-off recovery script that found the original 173020 damage (10
  missing adds + 8 stale edges in one wave, none of which any error
  string mentioned). Read-only by default; ``repair=true`` applies the
  delta adds-first. No LLM spend either way.

Serial and **checkpointed**: each finished hub id is appended to
``refs.meta.done_hub_ids``, so a re-claimed job resumes rather than
re-paying for hubs it already processed. A single hub's failure is
isolated to a ``job_event`` — one bad hub never fails the scope.

**Dark.** ``mode='reground'`` does nothing destructive unless ``prune``
is passed explicitly **and** the host's rubric-eval interlock is open
(``hub_refine.prune_interlock_open`` — the ``slice_refine_eval`` blocker
is enforced in code here, not just by convention, so a job submitter
cannot route around it). ``authorize_retire`` is gated a second time per
hub by the ``TAPROOT_REGROUND_OK`` / ``TAPROOT:reground-ok`` opt-in tag,
so the destructive stage can never fire on a hub nobody vetted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Scope — at least one required (validated in _dispatch, not the
        # schema, so the failure reads as a job event rather than a
        # submit-time schema error a caller can't act on).
        "hub_ids": {"type": "array", "items": {"type": "integer"}},
        "claim_scope": {"type": "string"},  # a tag, "NS:value"
        "draft_scope": {"type": "string"},  # a draft slug or dc<id>/dr<id>
        "mode": {"type": "string", "enum": ["reground", "verify"]},
        # reground-mode stage gates (all default OFF)
        "prune": {"type": "boolean"},
        "external": {"type": "boolean"},
        "authorize_retire": {"type": "boolean"},
        "topk": {"type": "integer"},
        # verify-mode
        "repair": {"type": "boolean"},
        "limit": {"type": "integer"},
    },
    "required": [],
    "additionalProperties": False,
}

#: Hard ceiling on how many hubs one job will touch, whatever the scope
#: resolves to. A reground pass is LLM-bearing per hub, and the serial
#: ``claude_inproc`` lane is shared — an unbounded taproot pass
#: monopolizes it (the spec's own warning about the interim agent runs).
#: Raise it deliberately per job via ``params.limit``, never by default.
_DEFAULT_LIMIT = 200


def _build_embedder(store: Store) -> Any:
    """Real embedder for the discovery step — same construction
    ``taproot_backfill`` uses, since this job dispatches in-worker off its
    own hub rather than the server's runtime."""
    from precis.config import load_config
    from precis.embedder import make_embedder

    cfg = load_config()
    return make_embedder(
        cfg.embedder,
        dim=store.embedding_dim(),
        url=cfg.embedder_url,
        timeout=cfg.embedder_timeout,
        max_retries=cfg.embedder_max_retries,
    )


def _hubs_for_tag(store: Store, tag: str, *, limit: int) -> list[int]:
    """Claim hubs carrying ``tag`` (``"NS:value"``). Deliberately a plain
    tag join rather than a new query surface — ``claim_scope`` exists so a
    cohort can be marked once and regrounded as a set."""
    ns, _, value = tag.partition(":")
    if not ns or not value:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'finding' AND r.retired_at IS NULL
               AND t.namespace = %s AND t.value = %s
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (ns, value, limit),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _hubs_for_draft(store: Store, scope: str, *, limit: int) -> list[int]:
    """Every claim hub cited by a draft's own prose, read out of its
    ``[fi<id>]`` inline markers.

    The markers are the authority, not a link table: a hub is cited by an
    inline marker, and that is exactly why retiring one without editing
    the sentence leaves a dangling cite. Resolving the scope through
    ``DraftHandler._scope_chunks`` reuses the same slug/``dc<id>``
    resolution ``taproot_backfill`` accepts, so the two jobs take the
    same scope strings.
    """
    import re

    from precis.dispatch import Hub
    from precis.handlers.draft import DraftHandler

    handler = DraftHandler(hub=Hub(store=store))
    pairs, _where = handler._scope_chunks(scope, allow_all=False)
    marker_re = re.compile(r"\[fi(\d+)\]")
    found: list[int] = []
    seen: set[int] = set()
    for _slug, c in pairs:
        for m in marker_re.finditer(c.text or ""):
            hub_id = int(m.group(1))
            if hub_id not in seen:
                seen.add(hub_id)
                found.append(hub_id)
    return found[:limit]


def _resolve_scope(store: Store, params: dict[str, Any], *, limit: int) -> list[int]:
    """``hub_ids`` wins outright (the spec's due-set bypass); otherwise a
    draft scope, otherwise a claim tag."""
    raw_ids = params.get("hub_ids")
    if isinstance(raw_ids, list) and raw_ids:
        return [int(x) for x in raw_ids][:limit]
    draft_scope = str(params.get("draft_scope") or "").strip()
    if draft_scope:
        return _hubs_for_draft(store, draft_scope, limit=limit)
    claim_scope = str(params.get("claim_scope") or "").strip()
    if claim_scope:
        return _hubs_for_tag(store, claim_scope, limit=limit)
    return []


def _run_verify_mode(
    ctx: Any, hub_ids: list[int], *, repair_requested: bool, repair_apply: bool
) -> None:
    """Intent-vs-committed diff (and optional adds-first repair) over the
    scope. No embedder, no LLM, no discovery — this mode exists precisely
    so a caller can audit what a previous wave actually committed without
    paying to re-judge anything.

    ``repair_apply`` is the caller's already-interlock-checked decision
    (:func:`~precis.workers.hub_refine.prune_interlock_open`) — this
    function never re-derives it, so there is exactly one place a repair
    request can be downgraded to a dry run. When ``repair_requested`` is
    true but ``repair_apply`` is false, :func:`repair_hub_intent` still
    runs with ``apply=False``: the intent diff is still reported, nothing
    is removed — the caller has already announced the block via
    ``job_event`` before calling in.
    """
    from precis.workers.hub_refine import repair_hub_intent, verify_hub_intent

    n_clean = 0
    n_dirty = 0
    n_no_intent = 0
    for hub_id in hub_ids:
        try:
            diff = (
                repair_hub_intent(ctx.store, hub_id, apply=repair_apply)
                if repair_requested
                else verify_hub_intent(ctx.store, hub_id)
            )
        except Exception as exc:
            n_dirty += 1
            ctx.append_chunk("job_event", f"fi{hub_id}: verify FAILED — {exc}")
            continue
        if not diff.has_intent:
            n_no_intent += 1
            continue
        if diff.clean:
            n_clean += 1
            continue
        n_dirty += 1
        ctx.append_chunk(
            "job_event",
            f"fi{hub_id}: {len(diff.missing_adds)} missing add(s), "
            f"{len(diff.stale_edges)} stale edge(s)"
            + (" (after repair)" if repair_apply else ""),
        )
    ctx.append_chunk(
        "job_summary",
        f"reground verify — {len(hub_ids)} hub(s): {n_clean} clean, "
        f"{n_dirty} with residue, {n_no_intent} with no stored intent"
        + (" [repair applied]" if repair_apply else ""),
    )
    ctx.set_meta(clean=n_clean, dirty=n_dirty, no_intent=n_no_intent)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.
    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.errors import BadInput, NotFound
    from precis.workers.hub_refine import (
        RegroundConfig,
        _reground_deeper_topk,
        prune_interlock_open,
        reground_one_hub,
    )

    params = (ctx.meta or {}).get("params") or {}
    mode = str(params.get("mode") or "reground")
    try:
        limit = int(params.get("limit") or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT

    try:
        hub_ids = _resolve_scope(ctx.store, params, limit=limit)
    except (NotFound, BadInput) as exc:
        ctx.record_failure(f"reground_claim: {exc}")
        return
    if not hub_ids:
        ctx.record_failure(
            "reground_claim: no hubs in scope — pass params.hub_ids, "
            "params.draft_scope, or params.claim_scope"
        )
        return

    done_ids: list[int] = list((ctx.meta or {}).get("done_hub_ids") or [])
    done_set = set(done_ids)
    todo = [h for h in hub_ids if h not in done_set]

    if mode == "verify":
        # The repair param is an opt-IN, not an override: same rubric-eval
        # blocker as the reground-mode prune stage below, checked in code
        # on this path too — repair_hub_intent's apply=True is a hard
        # DELETE through taproot.hub.remove_evidence, so a job submitter
        # cannot route around the interlock by asking for mode='verify'
        # instead of a reground prune. A requested-but-blocked repair is
        # announced, not silently downgraded — the diff is still reported.
        repair_requested = bool(params.get("repair"))
        repair_apply = repair_requested and prune_interlock_open()
        if repair_requested and not repair_apply:
            ctx.append_chunk(
                "job_event",
                "repair requested but the rubric-eval interlock is closed on "
                "this host (PRECIS_TAPROOT_REGROUND_PRUNE) — running "
                "verify-only; the intent diff is reported, no evidence removed",
            )
        _run_verify_mode(
            ctx, todo, repair_requested=repair_requested, repair_apply=repair_apply
        )
        return

    try:
        embedder = _build_embedder(ctx.store)
    except Exception as exc:
        ctx.record_failure(
            f"reground_claim: no embedder configured (PRECIS_EMBEDDER_URL): {exc}"
        )
        return

    # The prune param is an opt-IN, not an override: the rubric-eval
    # blocker is checked in code on this path too (prune_interlock_open),
    # so a job submitter cannot route around it. A requested-but-blocked
    # prune is announced, not silently downgraded.
    prune_requested = bool(params.get("prune"))
    prune = prune_requested and prune_interlock_open()
    if prune_requested and not prune:
        ctx.append_chunk(
            "job_event",
            "prune requested but the rubric-eval interlock is closed on this "
            "host (PRECIS_TAPROOT_REGROUND_PRUNE) — running audit-only; "
            "prune proposals are logged to meta.reground_log",
        )
    cfg = RegroundConfig(
        prune=prune,
        external=bool(params.get("external")),
        authorize_retire=bool(params.get("authorize_retire")),
        deeper_topk=int(params.get("topk") or _reground_deeper_topk()),
    )

    n_ok = 0
    n_failed = 0
    n_added = 0
    n_pruned = 0
    n_withheld = 0
    n_flagged = 0
    for hub_id in todo:
        if ctx.is_cancel_requested():
            ctx.append_chunk("job_event", "cancel requested — stopping between hubs")
            break
        try:
            result = reground_one_hub(ctx.store, hub_id, embedder=embedder, cfg=cfg)
        except Exception as exc:
            n_failed += 1
            ctx.append_chunk("job_event", f"fi{hub_id}: FAILED — {exc}")
        else:
            n_ok += 1
            n_added += result.confirmed_adds
            n_pruned += result.pruned
            n_withheld += result.withheld
            if not result.clean:
                n_flagged += 1
            if result.confirmed_adds or result.pruned or not result.clean:
                ctx.append_chunk("job_event", str(result.as_dict()))
        done_ids.append(hub_id)
        ctx.set_meta(done_hub_ids=list(done_ids))

    summary = (
        f"reground — {n_ok} hub(s) regrounded, {n_failed} failed: "
        f"{n_added} add(s) confirmed, {n_pruned} prune(s) applied, "
        f"{n_withheld} withheld, {n_flagged} hub(s) flagged for review"
    )
    if not cfg.prune:
        summary += " [prune stage OFF — proposals logged only]"
    ctx.append_chunk("job_summary", summary)
    ctx.set_meta(
        regrounded=n_ok,
        failed=n_failed,
        added=n_added,
        pruned=n_pruned,
        withheld=n_withheld,
        flagged=n_flagged,
    )


SPEC = JobTypeSpec(
    name="reground_claim",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),
    description=(
        "Reground taproot claim hubs: audit + prune proxy evidence, "
        "re-discover deeper primaries, add-first applier (serial, "
        "checkpointed). Also mode='verify' for the intent-vs-committed diff."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
