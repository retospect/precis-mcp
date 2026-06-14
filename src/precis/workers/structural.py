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

from precis.store import Store
from precis.workers.review import (
    Reviewer,
    _build_prompt as _review_build_prompt,
    _gate_enabled as _review_gate_enabled,
    _mcp_config_path,
    _recent_digest_exists as _review_recent_digest_exists,
    _write_digest as _review_write_digest,
    run_review_pass,
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
            """
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
               AND s.parent_id IS NULL
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
    """Body of the most recent ``tier:nursery`` memory if any, else placeholder.

    Capped at ~2KB so the prompt stays bounded; the model can drill
    in via MCP if it needs more.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.title
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = 'tier:nursery'
               AND r.created_at > now() - interval '24 hours'
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
        ).fetchone()
    if row is None:
        return "(none in the last 24h)"
    return (row[0] or "")[:2000]


def _structural_context(store: Store) -> dict[str, str]:
    return {
        "strategic_layer": _strategic_layer_snapshot(store),
        "nursery_excerpt": _recent_nursery_excerpt(store),
    }


_STRUCTURAL_TEMPLATE = """STRUCTURAL REVIEW — {today}

You are reviewing the asa todo tree for *structural* problems that
SQL can't detect. Below is a snapshot of the strategic + tactical
layer and the most recent nursery digest. If you need to drill in,
use the `precis` MCP tool (`get(kind='todo', id=N, view='tree')` to
read a subtree; `search(kind='todo', view='doable')` for next
actions).

## Strategic + tactical layer (snapshot)

{strategic_layer}

## Recent nursery digest

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
the audit log shows the review ran.

Do not address anyone. Do not use the precis MCP `put` tool to
write a memory directly — the worker will write your output as a
memory tagged `tier:structural` after you finish. Your final stdout
IS the digest body.
"""


STRUCTURAL = Reviewer(
    name="structural",
    tier_tag="tier:structural",
    gate_env="PRECIS_STRUCTURAL_REVIEW",
    meta_prefix="structural_",
    model="claude-opus-4-7",
    max_turns=30,
    timeout_s=900,
    min_interval_hours=MIN_INTERVAL_HOURS,
    context_builder=_structural_context,
    prompt_template=_STRUCTURAL_TEMPLATE,
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


# Tests `monkeypatch.setattr("precis.workers.structural.call_claude_agent", ...)`
# — keep that name resolvable from this module.
from precis.utils.claude_agent import call_claude_agent  # noqa: E402,F401


# Silence unused-import lints on the things we re-export for tests
# but don't reference here.
_ = (_mcp_config_path,)


__all__ = [
    "MIN_INTERVAL_HOURS",
    "STRUCTURAL",
    "run_structural_pass",
]
