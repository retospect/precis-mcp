"""Lanes — the scheduler slots ``run_slice`` launches a slice on.

Spec: ``docs/backlog/dark-factory.md`` § "Lanes: capacity shapes". A lane
is an adapter plus a credential pool plus a concurrency cap (Reto
2026-10-30, td541982). Model, effort and temperature are per-job choices
inside a lane; the lane itself is a field on the job (``meta.lane``),
never prose on a todo. The lane pins the model ids its adapter accepts
(``Lane.models``, cheapest first — the list a generator's brief names so
the pick lands in ``meta.op.params.model``, gr549046) and the model an
unpinned job gets (``Lane.default_model``: the cheapest of ``models``
that can still write code, so an unpinned child never drifts to the
frontier pin).

The table lives in code until the lane registry (step 3, a ``lanes``
table seeded from me538174) lands; :data:`LANES` is its seed. ``codex``
and ``claude-sub`` are enabled (Reto 2026-11-03: "ok both devin and codex
are ok. Lets make them go."); the others are declared with their adapters
named so the pick, the work view and the tests already see them, and
:func:`pick_lane` never returns a disabled lane. ``devin`` flips on once
the Devin CLI is installed and logged in on the slice host (td561545,
gr562902).

Pick (:func:`pick_lane`): fit for the task class first, then the fit lane
with the fewest live ``run_slice`` jobs carrying its ``meta.lane``, under
its cap. Pacing (the lane most behind its window target) replaces the
live-count order once the registry carries the window numbers.

Retry (:func:`ladder`): the table in order is also the escalation chain
``plan_dispatch`` climbs when a slice fails — each enabled fit lane's
models from its default upward, free vendor lanes before the Claude
subscription (Reto td559457).
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
    ``models`` are the model ids the lane's adapter accepts, cheapest
    first (empty while the adapter is unbuilt). ``default_model`` is what
    an unpinned ``run_slice`` job on this lane gets; ``None`` falls to
    the router's frontier pin.
    """

    name: str
    adapter: str
    credential_pool: str
    cap: int
    task_classes: frozenset[str]
    enabled: bool = True
    deny_write_paths: tuple[str, ...] = FACTORY_DENY_WRITE_PATHS
    vendor: str = ""
    models: tuple[str, ...] = ()
    default_model: str | None = None

    def fits(self, task_class: str) -> bool:
        """``True`` when this lane takes ``task_class`` work."""
        return ANY_CLASS in self.task_classes or task_class in self.task_classes


#: Seed table, in preference order (ties in :func:`pick_lane` keep it, and
#: :func:`ladder` climbs it): the free vendor lanes first, the Claude
#: subscription after them (Reto td559457, 2026-11-03: "devin → haiku →
#: sonnet → opus"; "both devin and codex are ok").
LANES: tuple[Lane, ...] = (
    Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="devin",
        cap=1,
        task_classes=frozenset({"code"}),
        # The devin_tree adapter is in (td561543); flips on once the Devin
        # CLI is installed and logged in on the slice host under the vendor
        # account (td561545, gr562902).
        enabled=False,
        vendor="cognition",
        # `devin models list` 2026-11-03: every SWE-2 tier is free; the
        # "-lightning" variants and every other family are paid and never
        # launched unattended (Reto: never buy overage).
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
    ),
    Lane(
        name="codex",
        adapter="codex_exec",
        credential_pool="codex",
        cap=1,
        # Code only (Reto 2026-10-26); reviews stay with the Claude lane
        # until Codex has a review track record.
        task_classes=frozenset({"code"}),
        # On since td562568: the codex_exec adapter (td561543) is in and
        # deploy's Codex login on the slice host answered a headless
        # ``codex exec`` 2026-11-04 (td561545; the dedicated vendor account
        # is gr562902). No model list: the adapter passes no model flag and
        # the CLI's account default runs.
        enabled=True,
        vendor="openai",
    ),
    Lane(
        name="claude-sub",
        adapter="claude_p",
        credential_pool="subscription",
        cap=2,
        task_classes=frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=(
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5",
            "claude-opus-4-8",
        ),
        # Sonnet-class: the cheapest accepted id that still writes code.
        default_model="claude-sonnet-5",
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
    lanes: tuple[Lane, ...] | None = None,
    counts: Mapping[str, int] | None = None,
    exclude_vendor: str | None = None,
) -> Lane | None:
    """The lane a ``task_class`` slice launches on, or ``None`` when every
    fit lane is full (or none fits).

    Fit first (enabled lanes whose ``task_classes`` take the class), then
    the fewest live ``run_slice`` jobs carrying that ``meta.lane``, only
    lanes under their cap; ties keep table order. ``lanes`` defaults to
    :data:`LANES` read at call time (like :func:`ladder`), so a table swap
    is seen; ``counts`` skips the DB read (tests, or a caller that already
    has them).

    ``exclude_vendor`` (the review rule: the reviewer never comes from the
    builder's vendor) drops fit lanes of that vendor. When no fit lane of
    another vendor is enabled the pick falls back to the full fit set --
    the caller sees ``lane.vendor == exclude_vendor`` and says so; once a
    second vendor's lane is enabled the rule holds (a full one is waited
    for, not bypassed).
    """
    table = LANES if lanes is None else lanes
    fit = [lane for lane in table if lane.enabled and lane.fits(task_class)]
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


@dataclass(frozen=True)
class Rung:
    """One step of the retry ladder: a lane and the model pinned on it
    (``None`` = the lane declares no models; its adapter runs the
    account default)."""

    lane: str
    model: str | None


def ladder(
    task_class: str, *, lanes: tuple[Lane, ...] | None = None
) -> tuple[Rung, ...]:
    """The cross-lane retry ladder for ``task_class`` (Reto td559457: a
    failed slice climbs before the cap asks): every enabled fit lane in
    table order, each contributing its ``models`` from ``default_model``
    upward, or one model-less rung when it declares none. With today's
    table that is codex → sonnet → opus; devin high → devin max joins at
    the foot once the devin lane is on."""
    out: list[Rung] = []
    for lane in LANES if lanes is None else lanes:
        if not lane.enabled or not lane.fits(task_class):
            continue
        if not lane.models:
            out.append(Rung(lane.name, None))
            continue
        start = (
            lane.models.index(lane.default_model)
            if lane.default_model in lane.models
            else 0
        )
        out.extend(Rung(lane.name, m) for m in lane.models[start:])
    return tuple(out)


def rung_index(
    rungs: tuple[Rung, ...], lane: str | None, model: str | None
) -> int | None:
    """Where a run on ``lane`` with ``model`` sits on ``rungs``: the exact
    (lane, model) rung, else the lane's model-less rung, else the lane's
    first rung (a model the lane does not list, e.g. a resolver fallback),
    else ``None``."""
    if not lane:
        return None
    exact = next(
        (i for i, r in enumerate(rungs) if r.lane == lane and r.model == model), None
    )
    if exact is not None:
        return exact
    return next((i for i, r in enumerate(rungs) if r.lane == lane), None)


__all__ = [
    "ANY_CLASS",
    "FACTORY_DENY_WRITE_PATHS",
    "LANES",
    "RUN_SLICE",
    "Lane",
    "Rung",
    "get_lane",
    "ladder",
    "live_lane_counts",
    "pick_lane",
    "rung_index",
]
