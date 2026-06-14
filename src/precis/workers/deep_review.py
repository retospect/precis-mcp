"""Deep reviewer — Slice 3 of ``docs/design/todo-tree-plan.md``.

Weekly Sunday-night tier. Where structural runs every 6h with a
relatively narrow lens, deep_review does the full Allen-review:
archive candidates, pruning, decomposition-budget warnings,
rotation rebalancing, long waits, plus a qualitative paragraph.

Thin shim around :mod:`precis.workers.review` — see the structural
shim for the same pattern.
"""

from __future__ import annotations

from precis.store import Store
from precis.workers.review import (
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


#: 6-day dedup window. The LaunchDaemon fires Sunday 23:00; this
#: window catches a Tuesday manual rerun while still allowing the
#: next legitimate Sunday cycle.
MIN_INTERVAL_HOURS = 144


def _strategic_dashboard(store: Store) -> str:
    """Compact strategic dashboard: one line per strategic with 7d picks."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE
              strat AS (
                SELECT r.ref_id, r.title
                  FROM refs r
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                   AND r.parent_id IS NULL
                   AND EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN'
                          AND t.value = 'level:strategic'
                   )
              ),
              subtree AS (
                SELECT s.ref_id AS ref_id, s.ref_id AS strategic_id FROM strat s
                UNION ALL
                SELECT r.ref_id, st.strategic_id
                  FROM refs r JOIN subtree st ON r.parent_id = st.ref_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
              )
            SELECT s.ref_id, s.title,
                   (SELECT count(*) FROM subtree st WHERE st.strategic_id = s.ref_id) AS subtree_size,
                   (SELECT count(*) FROM ref_events e
                     WHERE e.ref_id IN (SELECT st.ref_id FROM subtree st WHERE st.strategic_id = s.ref_id)
                       AND e.event = 'status:done'
                       AND e.ts >= now() - interval '7 days') AS picks_7d
              FROM strat s
             ORDER BY s.ref_id
            """,
        ).fetchall()
    if not rows:
        return "(no strategic todos yet)"
    lines: list[str] = []
    for s_id, title, size, picks in rows:
        first = (title or "").splitlines()[0]
        lines.append(
            f"#{int(s_id)} {first}  ({int(size or 0)} descendants, "
            f"{int(picks or 0)} picks in 7d)"
        )
    return "\n".join(lines)


def _recent_review_summary(store: Store) -> str:
    """One line per recent nursery/structural digest in the last 7d."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT t.value AS tier, r.created_at, r.title
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value IN ('tier:nursery', 'tier:structural')
               AND r.created_at > now() - interval '7 days'
             ORDER BY r.created_at DESC
             LIMIT 30
            """,
        ).fetchall()
    if not rows:
        return "(no nursery / structural digests in the last 7 days)"
    lines: list[str] = []
    for tier, ts, title in rows:
        head = (title or "").splitlines()[0][:120]
        lines.append(f"- [{tier}] {ts.date().isoformat()}: {head}")
    return "\n".join(lines)


def _deep_context(store: Store) -> dict[str, str]:
    return {
        "strategic_dashboard": _strategic_dashboard(store),
        "recent_review_summary": _recent_review_summary(store),
    }


_DEEP_TEMPLATE = """DEEP REVIEW — {today}

You are reviewing the asa todo tree at the weekly cadence (Allen's
"deep review" tier). Below is the strategic dashboard with 7d
picks accounting and a summary of the last week's nursery +
structural digests. Use the `precis` MCP tool to drill into any
subtree you need to look at (`get(kind='todo', id=N, view='tree')`).

## Strategic dashboard

{strategic_dashboard}

## Recent review summary (last 7 days)

{recent_review_summary}

## What to do

Produce a markdown digest organised into five sections (skip any
section with no recommendations):

1. **Archive candidates** — strategics whose work is functionally
   done. Name the strategic id and the evidence (last done leaf,
   no open descendants, etc.). Suggest the operator close them
   with `tag(id=N, add=['STATUS:done'])`.

2. **Pruning candidates** — branches whose subtree is stale,
   irrelevant, or duplicates work elsewhere. Suggest soft-deletes
   with reasoning.

3. **Decomposition budget warnings** — strategics approaching the
   soft cap of 30 descendants (knob #5 in the plan). Suggest
   which subtrees could be pruned, archived, or split out into
   their own strategic.

4. **Rotation rebalancing** — strategics that have drifted from
   their 1/N share (very few or very many picks vs expected).
   Note whether the imbalance is workload-driven (legitimate) or
   crowding-out (pause / re-PRIO).

5. **Long-running waits** — `waiting-for:*` leaves > 7d that
   probably need the dependency replaced or the wait converted to
   an asking-reto leaf.

End with one or two paragraphs of qualitative narrative — what's
the tree telling you about how the week went? Use this for
continuity; asa-bot's preamble surfaces recent memories so a good
narrative gets quoted back in chat.

Do not address anyone. Do not use the precis MCP `put` tool to
write a memory directly — the worker will write your output as a
memory tagged `tier:deep` after you finish. Your final stdout IS
the digest body.
"""


DEEP_REVIEW = Reviewer(
    name="deep_review",
    tier_tag="tier:deep",
    gate_env="PRECIS_DEEP_REVIEW",
    meta_prefix="deep_review_",
    model="claude-opus-4-7",
    max_turns=60,
    timeout_s=1800,
    min_interval_hours=MIN_INTERVAL_HOURS,
    context_builder=_deep_context,
    prompt_template=_DEEP_TEMPLATE,
)


# ── public entry + back-compat shims for tests ───────────────────


def run_deep_review_pass(
    store: Store, *, min_interval_hours: float | None = None
) -> BatchResult:
    from dataclasses import replace

    reviewer = (
        DEEP_REVIEW
        if min_interval_hours is None
        else replace(DEEP_REVIEW, min_interval_hours=min_interval_hours)
    )
    return run_review_pass(reviewer, store)


def _gate_enabled() -> bool:
    return _review_gate_enabled(DEEP_REVIEW.gate_env)


def _recent_digest_exists(store: Store, hours: float) -> bool:
    return _review_recent_digest_exists(store, DEEP_REVIEW.tier_tag, hours)


def _write_digest(store: Store, body: str, cost_usd: float | None) -> int:
    return _review_write_digest(store, DEEP_REVIEW, body, cost_usd)


def _build_prompt(store: Store) -> str:
    return _review_build_prompt(DEEP_REVIEW, store)


# After the reviewer refactor, tests patch
# ``precis.workers.review.call_claude_agent`` (the actual call site)
# rather than this module — no late re-export needed.

_ = (_mcp_config_path,)

__all__ = [
    "DEEP_REVIEW",
    "MIN_INTERVAL_HOURS",
    "run_deep_review_pass",
]
