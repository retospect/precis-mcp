"""Structural reviewer — Slice 3 of ``docs/design/todo-tree-plan.md``.

The middle tier between the hourly nursery and the weekly deep
review. Runs every 6 hours and asks an opus-class model to look at
the tree's *shape* (drift, sibling contradictions, missing
outcomes, fanout warnings).

This module is a thin shim around :mod:`precis.workers.review` —
the dispatch / gate / dedup / digest-write plumbing is shared
between every reviewer; only the context-gathering SQL + the
prompt template live here.
"""

from __future__ import annotations

from precis.handlers._todo_guards import todo_root_sql
from precis.store import Store
from precis.utils.llm.router import Tier, resolve_model
from precis.utils.prompt import AssemblyContext, Layer, Module
from precis.workers.review import (
    _SHARED_TRAILING_MODULES,
    Reviewer,
    _mcp_config_path,
    run_review_pass,
)
from precis.workers.review import (
    _build_prompt as _review_build_prompt,
)
from precis.workers.review import (
    _gate_enabled as _review_gate_enabled,
)
from precis.workers.review import (
    _recent_digest_exists as _review_recent_digest_exists,
)
from precis.workers.review import (
    _write_digest as _review_write_digest,
)
from precis.workers.runner import BatchResult

# ── reviewer-specific config ─────────────────────────────────────


#: Default min hours between digests. Plan cadence is 6h; 5h
#: tolerates RunAtLoad double-fires while still rejecting a manual
#: rerun in the same window.
MIN_INTERVAL_HOURS = 5


def _strategic_layer_snapshot(store: Store) -> str:
    """Render strategic roots + their tactical children with leaf counts."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT s.ref_id AS strategic_id,
                   s.title AS strategic_title,
                   t.ref_id AS tactical_id,
                   t.title AS tactical_title,
                   (SELECT count(*) FROM refs c
                     WHERE c.parent_id = t.ref_id
                       AND c.deleted_at IS NULL) AS direct_children
              FROM refs s
              LEFT JOIN refs t ON t.parent_id = s.ref_id
                              AND t.kind = 'todo'
                              AND t.deleted_at IS NULL
             WHERE s.kind = 'todo' AND s.deleted_at IS NULL
               AND {todo_root_sql("s")}
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags tg ON tg.tag_id = rt.tag_id
                    WHERE rt.ref_id = s.ref_id
                      AND tg.namespace = 'OPEN'
                      AND tg.value = 'level:strategic'
               )
             ORDER BY s.ref_id, t.ref_id NULLS FIRST
            """,
        ).fetchall()
    if not rows:
        return "(no strategic todos yet)"
    lines: list[str] = []
    last_strategic_id: int | None = None
    for s_id, s_title, t_id, t_title, t_children in rows:
        s_id = int(s_id)
        if s_id != last_strategic_id:
            lines.append("")
            lines.append(f"#{s_id} {(s_title or '').splitlines()[0]}")
            last_strategic_id = s_id
        if t_id is not None:
            lines.append(
                f"  └─ #{int(t_id)} {(t_title or '').splitlines()[0]} "
                f"({int(t_children or 0)} direct children)"
            )
    return "\n".join(lines).lstrip("\n")


def _recent_nursery_excerpt(store: Store) -> str:
    """Compact list of currently-open ``nursery:*`` alerts, else a placeholder.

    Nursery now raises a ``kind='alert'`` per condition (see
    :mod:`precis.alerts`) instead of writing a digest memory, so the
    reviewer reads the live open set rather than the latest digest. One
    line per alert, capped at ~2KB so the prompt stays bounded; the
    model can drill in via MCP if it needs more.
    """
    from precis.alerts import list_open_alerts

    nursery = [
        a for a in list_open_alerts(store) if (a["source"] or "").startswith("nursery:")
    ]
    if not nursery:
        return "(no open nursery alerts)"
    lines = [
        f"- [{a['severity']}] {a['source']}: {a['title']}"
        + (f" (seen {a['seen_count']}×)" if a["seen_count"] > 1 else "")
        for a in nursery
    ]
    return "\n".join(lines)[:2000]


def _structural_context(store: Store) -> dict[str, str]:
    return {
        "strategic_layer": _strategic_layer_snapshot(store),
        "nursery_excerpt": _recent_nursery_excerpt(store),
    }


def _structural_body(ctx: AssemblyContext) -> str:
    """The structural-reviewer-specific body (ADR 0038 step 3).

    Everything up to the shared abbreviations + footer blocks: the header,
    the drill-in note, both live-data sections (read from ``ctx.extras``),
    the "what to look for" checklist, and the output-format spec.
    """
    today = ctx.extras["today"]
    strategic_layer = ctx.extras["strategic_layer"]
    nursery_excerpt = ctx.extras["nursery_excerpt"]
    return f"""STRUCTURAL REVIEW — {today}

You are reviewing the asa todo tree for *structural* problems that
SQL can't detect. Below is a snapshot of the strategic + tactical
layer and the currently-open nursery alerts. If you need to drill in,
use the `precis` MCP tool (`get(kind='todo', id=N, view='tree')` to
read a subtree; `search(kind='todo', view='doable')` for next
actions).

## Strategic + tactical layer (snapshot)

{strategic_layer}

## Open nursery alerts

{nursery_excerpt}

## What to look for

Look for issues that need semantic judgment, not the rules-based
checks the nursery already runs:

1. **Branches missing an outcome line.** A node with children
   should read as "what does done look like" on its first line.
   If the first line reads as an action verb ("Draft the…",
   "Write the…", "Fix the…"), call it out — the branch is a
   project mis-labelled as an action.
2. **Drift between branch outcome and child actions.** The
   children should plausibly ladder to the outcome. If a branch
   says "Submitted to JCP, all figs camera-ready" but its
   children are about "set up the lab notebook", flag the drift.
3. **Sibling contradictions.** Two children whose work undoes
   each other (one renames X, the other depends on the old
   name), or two that compete for the same artifact without one
   blocking the other.
4. **Depth/fanout warnings.** Tactical branches with 8+ direct
   subtasks (probably under-decomposed) or three-level pillars of
   single-child branches (probably over-decomposed). Plan cap is
   depth 10; flag anything approaching it.

## Output format

Write a markdown digest. Start with a one-line summary. Then a
section per problem type (only those with findings). Each finding
references the ref by id. Be specific — name what's wrong and
suggest the next move. If the tree looks clean, say so explicitly
("No structural issues this pass"); we still write the digest so
the audit log shows the review ran."""


#: The structural prompt as an ordered module list (ADR 0038 step 3):
#: the reviewer-specific body, then the two SHARED trailing blocks
#: (abbreviations + only-put-is-a-gripe footer) authored once in
#: :mod:`precis.workers.review`.
_STRUCTURAL_MODULES: list[Module] = [
    Module(
        id="structural.body",
        layer=Layer.VARIABLE,
        build=_structural_body,
        required=True,
    ),
    *_SHARED_TRAILING_MODULES,
]


STRUCTURAL = Reviewer(
    name="structural",
    tier_tag="tier:structural",
    gate_env="PRECIS_STRUCTURAL_REVIEW",
    meta_prefix="structural_",
    # Cloud reasoning tier (opus-4.8) via the router; a per-pass
    # ``PRECIS_STRUCTURAL_MODEL`` pin still wins in ``run_review_pass``.
    tier=Tier.CLOUD_SUPER,
    model=resolve_model(Tier.CLOUD_SUPER),
    max_turns=30,
    timeout_s=900,
    min_interval_hours=MIN_INTERVAL_HOURS,
    context_builder=_structural_context,
    modules=_STRUCTURAL_MODULES,
)


# ── public entry + back-compat shims for tests ───────────────────


def run_structural_pass(
    store: Store, *, min_interval_hours: float | None = None
) -> BatchResult:
    """Run one structural pass via the unified review driver.

    ``min_interval_hours`` exists for symmetry with the original
    surface; pass a custom value to override the reviewer's default
    dedup window (e.g. in tests).
    """
    from dataclasses import replace

    reviewer = (
        STRUCTURAL
        if min_interval_hours is None
        else replace(STRUCTURAL, min_interval_hours=min_interval_hours)
    )
    return run_review_pass(reviewer, store)


# The helpers below preserve the test-visible API. After the
# refactor they delegate to :mod:`precis.workers.review`, holding
# the reviewer-specific config so tests can keep their existing
# call shapes (`_gate_enabled()`, `_recent_digest_exists(store, hours)`,
# `_write_digest(store, body, cost_usd)`, `_build_prompt(store)`).


def _gate_enabled() -> bool:
    return _review_gate_enabled(STRUCTURAL.gate_env)


def _recent_digest_exists(store: Store, hours: float) -> bool:
    return _review_recent_digest_exists(store, STRUCTURAL.tier_tag, hours)


def _write_digest(store: Store, body: str, cost_usd: float | None) -> int:
    return _review_write_digest(store, STRUCTURAL, body, cost_usd)


def _build_prompt(store: Store) -> str:
    return _review_build_prompt(STRUCTURAL, store)


# The reviewer routes through the LLM seam (ADR 0046 unit 4b), so tests
# patch ``precis.utils.llm.router.call_claude_agent`` (the wrapper the
# provider calls) rather than this module — no late re-export needed.

# Silence unused-import lints on the things we re-export for tests
# but don't reference here.
_ = (_mcp_config_path,)


__all__ = [
    "MIN_INTERVAL_HOURS",
    "STRUCTURAL",
    "run_structural_pass",
]
