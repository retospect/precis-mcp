"""Lanes — the scheduler slots ``run_slice`` launches a slice on.

Spec: ``docs/backlog/dark-factory.md`` § "Lanes: capacity shapes". A lane
is an adapter plus a credential pool plus a concurrency cap (Reto
2026-10-30, td541982). Model, effort and temperature are per-job choices
inside a lane; the lane itself is a field on the job (``meta.lane``),
never prose on a todo.

The table lives in code until the lane registry (step 3, a ``lanes``
table seeded from me538174) lands; :data:`LANES` is its seed. Only
``claude-sub`` is enabled: the others are declared with their adapters
named so the pick, the work view and the tests already see them, and
:func:`pick_lane` never returns a disabled lane.

Pick (:func:`pick_lane`): fit for the task class first, then the fit lane
with the fewest live ``run_slice`` jobs carrying its ``meta.lane``, under
its cap. Pacing (the lane most behind its window target) replaces the
live-count order once the registry carries the window numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Matches every task class in :attr:`Lane.task_classes`.
ANY_CLASS = "*"

#: The job type whose live rows count against a lane's cap.
RUN_SLICE = "run_slice"

#: Job statuses that no longer hold a lane slot (mirrors
#: ``precis.workers.executors._common.TERMINAL``; restated so this
#: package stays below the workers layer).
_TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")

#: Repo paths a slice session may not write (spec § "7. Alerts as slices":
#: a fixer never touches the rule that fired, the factory never lands its
#: own controller; Reto's ruling on td542236). Posix globs, repo-relative;
#: ``**`` spans directories. ``run_slice`` passes them to the session as
#: deny rules and fails the attempt when the branch touches one anyway.
FACTORY_DENY_WRITE_PATHS: tuple[str, ...] = (
    # the ship harness
    "scripts/ship",
    "scripts/test",
    "scripts/lib/**",
    ".github/**",
    # the factory's own modules
    "src/precis/plans/**",
    "src/precis/workers/job_types/run_slice.py",
    "src/precis/workers/job_types/plan_dispatch.py",
    "src/precis/workers/job_types/gripe_validate.py",
    # the dark release pass: the approval guard, the ops and the script
    # (named singly although plans/** already spans two of them: the
    # release path must stay denied if the glob above is ever narrowed)
    "src/precis/plans/release.py",
    "src/precis/plans/lanes.py",
    "src/precis/workers/job_types/release_cut.py",
    "src/precis/workers/job_types/ci_verdict_poll.py",
    "src/precis/workers/job_types/release_deploy.py",
    "src/precis/workers/job_types/land_slice.py",
    "scripts/release",
    # alert rules, detectors and monitors
    "src/precis/alert_ids.py",
    "src/precis/alerts.py",
    "src/precis/workers/nursery.py",
    "deploy/roles/monitoring/**",
)


@dataclass(frozen=True)
class Lane:
    """One scheduler slot: adapter + credential pool + concurrency cap.

    ``adapter`` names how a slice is launched (``claude_p`` is
    ``claude -p`` in a worktree; ``codex_exec``, ``kimi_tree``,
    ``devin_tree`` and ``api`` are declared, not built). ``task_classes``
    is the fit set; :data:`ANY_CLASS` fits every class.
    ``deny_write_paths`` are the repo globs a slice on this lane may not
    write (:data:`FACTORY_DENY_WRITE_PATHS` for every seed lane).
    ``vendor`` is the model vendor behind the lane (``''`` = unknown); a
    review is picked with ``exclude_vendor`` = the builder lane's vendor.
    """

    name: str
    adapter: str
    credential_pool: str
    cap: int
    task_classes: frozenset[str]
    enabled: bool = True
    deny_write_paths: tuple[str, ...] = FACTORY_DENY_WRITE_PATHS
    vendor: str = ""

    def fits(self, task_class: str) -> bool:
        """``True`` when this lane takes ``task_class`` work."""
        return ANY_CLASS in self.task_classes or task_class in self.task_classes


#: Seed table, in preference order (ties in :func:`pick_lane` keep it).
LANES: tuple[Lane, ...] = (
    Lane(
        name="claude-sub",
        adapter="claude_p",
        credential_pool="subscription",
        cap=2,
        task_classes=frozenset({ANY_CLASS}),
        vendor="anthropic",
    ),
    Lane(
        name="codex",
        adapter="codex_exec",
        credential_pool="codex",
        cap=1,
        task_classes=frozenset({"code", "review"}),
        enabled=False,
        vendor="openai",
    ),
    Lane(
        name="kimi",
        adapter="kimi_tree",
        credential_pool="kimi",
        cap=1,
        task_classes=frozenset({"code", "docs"}),
        enabled=False,
        vendor="moonshot",
    ),
    Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="devin",
        cap=1,
        task_classes=frozenset({"code"}),
        enabled=False,
        vendor="cognition",
    ),
    Lane(
        name="metered",
        adapter="api",
        credential_pool="metered",
        cap=1,
        task_classes=frozenset({ANY_CLASS}),
        enabled=False,
        # OpenRouter / API keys: the vendor is per model; the registry
        # (step 3) splits this lane per vendor before it is enabled.
        vendor="openrouter",
    ),
)


def get_lane(name: str) -> Lane | None:
    """The seed-table lane called ``name``, or ``None``."""
    for lane in LANES:
        if lane.name == name:
            return lane
    return None


def live_lane_counts(store: Any) -> dict[str, int]:
    """``{lane name: live run_slice jobs carrying it}`` from the DB.

    Live = a ``run_slice`` job, not retired, with ``meta.lane`` set and no
    terminal ``STATUS``. A job stamps ``meta.lane`` at submit, so a job
    still queued with no lane yet does not hold a slot.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.meta->>'lane', count(*)
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %s
               AND r.meta ? 'lane'
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                   )
             GROUP BY 1
            """,
            (RUN_SLICE, list(_TERMINAL_STATUSES)),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows if r[0] is not None}


def pick_lane(
    store: Any,
    task_class: str,
    *,
    lanes: tuple[Lane, ...] = LANES,
    counts: Mapping[str, int] | None = None,
    exclude_vendor: str | None = None,
) -> Lane | None:
    """The lane a ``task_class`` slice launches on, or ``None`` when every
    fit lane is full (or none fits).

    Fit first (enabled lanes whose ``task_classes`` take the class), then
    the fewest live ``run_slice`` jobs carrying that ``meta.lane``, only
    lanes under their cap; ties keep table order. ``counts`` skips the DB
    read (tests, or a caller that already has them).

    ``exclude_vendor`` (the review rule: the reviewer never comes from the
    builder's vendor) drops fit lanes of that vendor. When no fit lane of
    another vendor is enabled the pick falls back to the full fit set --
    the caller sees ``lane.vendor == exclude_vendor`` and says so; once a
    second vendor's lane is enabled the rule holds (a full one is waited
    for, not bypassed).
    """
    fit = [lane for lane in lanes if lane.enabled and lane.fits(task_class)]
    if exclude_vendor:
        other = [lane for lane in fit if lane.vendor != exclude_vendor]
        fit = other or fit
    if not fit:
        return None
    live = dict(counts) if counts is not None else live_lane_counts(store)
    open_lanes = [lane for lane in fit if live.get(lane.name, 0) < lane.cap]
    if not open_lanes:
        return None
    # ``min`` keeps the first of equal keys, so ties keep table order.
    return min(open_lanes, key=lambda lane: live.get(lane.name, 0))


__all__ = [
    "ANY_CLASS",
    "FACTORY_DENY_WRITE_PATHS",
    "LANES",
    "RUN_SLICE",
    "Lane",
    "get_lane",
    "live_lane_counts",
    "pick_lane",
]
