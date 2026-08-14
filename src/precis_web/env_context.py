"""Assembled-context panel data for the ``/env`` inspector (Part 3B).

Part 3A (:mod:`precis.utils.prompt.capture`) made the planner tick and the
two reviewer passes persist the FULL assembled prompt input onto a ref's
``meta`` (``meta.assembled_context`` + ``meta.assembled_context_at``) — the
input-side twin of ``meta.transcript``. This module is the read side: it
finds the most recent captured ("last real") context per agent, and can
also assemble a fresh preview ("dry-run") with zero LLM spend by reusing
the same builder functions the real passes call.

WHERE the capture lands differs by agent (mirrors Part 3A's contract):

* ``job_claude_inproc`` drains many job_types; only ``plan_tick`` captures
  today, onto its own ``kind='job'`` ref (``meta.job_type = 'plan_tick'``).
* ``structural`` / ``deep_review`` mint no job ref — capture lands on the
  ``kind='memory'`` digest ref the pass writes, found by its ``tier_tag``.
* ``dream_agent`` builds its prompt by hand (no :mod:`precis.utils.prompt`
  assembler on that path) — never captured, and no dry-run is possible.

Every lookup here is best-effort: a DB hiccup or an assembler exception
degrades to a note (:func:`build_panel` never raises), matching the
``_read_file`` "unreadable" convention already on the ``/env`` page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.workers.registry import ServiceSpec

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Shown when an agent has a capture mapping but nothing has landed yet.
_NO_CAPTURE_YET = (
    "no captured context yet — nothing has run since this capture shipped, "
    "or this agent hasn't fired"
)
#: Shown for the one agent with no assembler on its path at all (dream).
_HAND_ROLLED_NOT_CAPTURED = (
    "hand-rolled prompt — not captured (no assembler on this path)"
)
_HAND_ROLLED_NO_DRY_RUN = "hand-rolled prompt — no dry-run available"
_DRY_RUN_UNAVAILABLE = (
    "dry-run unavailable right now (no representative target found, or the "
    "assembler raised — see server logs)"
)


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One rendered assembled-context block — a collapsible ``<details>`` row."""

    id: str
    layer: str
    text: str


@dataclass(frozen=True, slots=True)
class LastReal:
    """The most recently captured real assembled context for an agent."""

    #: ``"job:123"`` / ``"memory:456"`` — the ref that carried the capture.
    handle: str
    captured_at: str | None
    blocks: list[ContextBlock]


@dataclass(frozen=True, slots=True)
class DryRun:
    """A freshly assembled (zero-LLM-call) preview for an agent."""

    #: What the assembly was run against, e.g. ``"todo:42 (representative)"``.
    target_label: str
    blocks: list[ContextBlock]


@dataclass(frozen=True, slots=True)
class AssembledContextPanel:
    """Everything the ``/env`` template needs for one agent's panel."""

    last_real: LastReal | None
    last_real_note: str | None
    dry_run: DryRun | None
    dry_run_note: str | None


def _blocks_from_entries(entries: list[dict[str, Any]]) -> list[ContextBlock]:
    return [
        ContextBlock(
            id=str(e.get("id", "?")),
            layer=str(e.get("layer", "?")),
            text=str(e.get("text", "")),
        )
        for e in entries
    ]


def _blocks_from_assembler(blocks: Any) -> list[ContextBlock]:
    """Project real :class:`precis.utils.prompt.Block` objects to the panel shape."""
    return [ContextBlock(id=b.id, layer=str(b.layer), text=b.text) for b in blocks]


# ── "last real" lookups ──────────────────────────────────────────────


def _last_real_job(store: Store, *, job_type: str) -> LastReal | None:
    """Latest ``kind='job'`` ref of ``job_type`` carrying a capture."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT ref_id, meta
              FROM refs
             WHERE kind = 'job'
               AND deleted_at IS NULL
               AND meta ? 'assembled_context'
               AND meta->>'job_type' = %s
             ORDER BY meta->>'assembled_context_at' DESC NULLS LAST
             LIMIT 1
            """,
            (job_type,),
        ).fetchone()
    if row is None:
        return None
    ref_id, meta = row
    entries = meta.get("assembled_context") or []
    return LastReal(
        handle=f"job:{int(ref_id)}",
        captured_at=meta.get("assembled_context_at"),
        blocks=_blocks_from_entries(entries),
    )


def _last_real_digest(store: Store, *, tier_tag: str) -> LastReal | None:
    """Latest ``kind='memory'`` digest ref tagged ``tier_tag`` carrying a capture."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
               AND r.meta ? 'assembled_context'
             ORDER BY r.meta->>'assembled_context_at' DESC NULLS LAST
             LIMIT 1
            """,
            (tier_tag,),
        ).fetchone()
    if row is None:
        return None
    ref_id, meta = row
    entries = meta.get("assembled_context") or []
    return LastReal(
        handle=f"memory:{int(ref_id)}",
        captured_at=meta.get("assembled_context_at"),
        blocks=_blocks_from_entries(entries),
    )


def load_last_real(store: Store, spec: ServiceSpec) -> LastReal | None:
    """The most recent captured context for ``spec``, or ``None``.

    ``None`` means either "nothing has landed yet" (job_claude_inproc,
    structural, deep_review) or "never captured, ever" (dream_agent) — the
    caller (:func:`build_panel`) tells those apart via the note text.
    """
    if spec.name == "job_claude_inproc":
        # job_claude_inproc drains several job_types (plan_tick, fix_gripe,
        # casts…); only plan_tick captures today (Part 3A), so that's the
        # representative job_type we look for.
        return _last_real_job(store, job_type="plan_tick")
    if spec.name == "structural":
        from precis.workers.structural import STRUCTURAL

        return _last_real_digest(store, tier_tag=STRUCTURAL.tier_tag)
    if spec.name == "deep_review":
        from precis.workers.deep_review import DEEP_REVIEW

        return _last_real_digest(store, tier_tag=DEEP_REVIEW.tier_tag)
    # dream_agent (and any future introspectable agent with no capture
    # mapping yet) — nothing to look up.
    return None


# ── dry-run (zero-LLM-call) assembly ─────────────────────────────────


def _representative_todo_ref_id(store: Store) -> int | None:
    """A recent ``meta.llm_tier``-set todo to assemble the planner dry-run against.

    ``meta.llm_tier`` is a closed-vocabulary field (the §M facet-
    normalized replacement for the old ``LLM:<model>`` tag) — see
    ``precis.workers.dispatch``'s ``llm_tier`` projection for the same
    convention. Deliberately looser than the dispatcher's full doable
    predicate (``_candidate_parent_ids``) — this only needs *some*
    plausible planner-owned todo to preview an assembly against, not the
    exact next-to-fire candidate.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.meta ? 'llm_tier'
             ORDER BY r.updated_at DESC NULLS LAST, r.created_at DESC
             LIMIT 1
            """
        ).fetchone()
    return int(row[0]) if row else None


def _dry_run_planner(store: Store, *, target_ref_id: int | None) -> DryRun | None:
    """Assemble the planner prompt for ``target_ref_id`` (or a representative
    todo when unset) — reuses :func:`build_planner_prompts`, no LLM call."""
    ref_id = (
        target_ref_id
        if target_ref_id is not None
        else _representative_todo_ref_id(store)
    )
    if ref_id is None:
        return None
    from precis.workers.planner_prompt import build_planner_prompts

    prompts = build_planner_prompts(store, ref_id=ref_id, model="opus")
    label = f"todo:{ref_id}" + (
        ""
        if target_ref_id is not None
        else " (representative — most-recently-touched LLM:* todo)"
    )
    return DryRun(target_label=label, blocks=_blocks_from_assembler(prompts.blocks))


def _dry_run_reviewer(store: Store, reviewer: Any, *, label: str) -> DryRun:
    """Assemble a reviewer's prompt against the CURRENT tree — reuses
    :func:`_assemble_reviewer_blocks`, no LLM call."""
    from precis.workers.review import _assemble_reviewer_blocks

    blocks = _assemble_reviewer_blocks(reviewer, store)
    return DryRun(
        target_label=f"{label} — current tree state",
        blocks=_blocks_from_assembler(blocks),
    )


def build_dry_run(
    store: Store, spec: ServiceSpec, *, target_ref_id: int | None = None
) -> DryRun | None:
    """Assemble a fresh, zero-LLM-call preview for ``spec``, or ``None``.

    ``target_ref_id`` scopes the planner dry-run to a specific todo (e.g.
    a draft's owning project) — see the ``/env?target_ref_id=`` query param
    threaded from the draft reader's "assembled context" link. Ignored for
    the reviewers (they assemble over the whole tree, not one ref) and for
    dream (no assembler at all).
    """
    if spec.name == "job_claude_inproc":
        return _dry_run_planner(store, target_ref_id=target_ref_id)
    if spec.name == "structural":
        from precis.workers.structural import STRUCTURAL

        return _dry_run_reviewer(store, STRUCTURAL, label="Structural reviewer")
    if spec.name == "deep_review":
        from precis.workers.deep_review import DEEP_REVIEW

        return _dry_run_reviewer(store, DEEP_REVIEW, label="Deep review")
    # dream_agent: hand-rolled prompt, no assembler to reuse.
    return None


# ── the panel a route renders ────────────────────────────────────────


def build_panel(
    store: Store, spec: ServiceSpec, *, target_ref_id: int | None = None
) -> AssembledContextPanel:
    """Build both sub-sections ("last real" + "dry-run") for ``spec``.

    Never raises: every lookup is wrapped so a DB hiccup or an assembler
    exception degrades to a note instead of 500ing the ``/env`` page.
    """
    last_real: LastReal | None = None
    try:
        last_real = load_last_real(store, spec)
    except Exception:
        log.exception("assembled-context: last-real lookup failed for %s", spec.name)
    last_real_note = None
    if last_real is None:
        last_real_note = (
            _HAND_ROLLED_NOT_CAPTURED if spec.name == "dream_agent" else _NO_CAPTURE_YET
        )

    dry_run: DryRun | None = None
    try:
        dry_run = build_dry_run(store, spec, target_ref_id=target_ref_id)
    except Exception:
        log.exception("assembled-context: dry-run failed for %s", spec.name)
    dry_run_note = None
    if dry_run is None:
        dry_run_note = (
            _HAND_ROLLED_NO_DRY_RUN
            if spec.name == "dream_agent"
            else _DRY_RUN_UNAVAILABLE
        )

    return AssembledContextPanel(
        last_real=last_real,
        last_real_note=last_real_note,
        dry_run=dry_run,
        dry_run_note=dry_run_note,
    )


def empty_panel(reason: str) -> AssembledContextPanel:
    """A degrade-safe panel when even reaching the store failed."""
    return AssembledContextPanel(
        last_real=None, last_real_note=reason, dry_run=None, dry_run_note=reason
    )


__all__ = [
    "AssembledContextPanel",
    "ContextBlock",
    "DryRun",
    "LastReal",
    "build_dry_run",
    "build_panel",
    "empty_panel",
    "load_last_real",
]
