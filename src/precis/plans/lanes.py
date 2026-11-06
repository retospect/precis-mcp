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

The table derives from ``llm_account`` rows at call time (td568401 S1c:
:func:`load_lanes` → :func:`derive_lanes`; ``lane_rank`` carries the
preference order the code seed held by position). ``devin``, ``codex``
and ``claude-sub`` are enabled (Reto 2026-11-03: "ok both devin and codex
are ok. Lets make them go."; devin on once its CLI login on the slice
host was confirmed, 2026-11-06, td562568); the others stay account rows —
an account on an adapter the builder cannot launch (``kimi_tree``,
``api``; :data:`LAUNCHABLE_ADAPTERS`) never becomes a lane, so enabling
an api-adapter account can never mint a lane a submit would die on at
``get_adapter``. :func:`pick_lane` never returns a disabled lane.

Pick (:func:`pick_lane`): fit for the task class first, then the fit lane
with the fewest live ``run_slice`` jobs carrying its ``meta.lane``, under
its cap. Pacing (the lane most behind its window target) replaces the
live-count order once the registry carries the window numbers.

Backoff (:func:`lane_backoffs`): a launch the lane's host refuses (the
adapter preflight — binary, login, trust seed) stamps
``meta.host_refusal = {lane, until, why}`` on the failed job and backs the
lane off :data:`HOST_REFUSAL_BACKOFF` (gr571395). A lane is backed off
while its newest ``run_slice`` job carries a refusal ``until`` in the
future; ``pick_lane`` skips it, the first submit after the window is the
probe, and a probe job without the mark clears the lane by being newer.
A broken host then costs one failed probe per window instead of a
retry-cap ask per todo.

Retry (:func:`ladder`): the table in order is also the escalation chain
``plan_dispatch`` climbs when a slice fails — each enabled fit lane's
models from its default upward, free vendor lanes before the Claude
subscription (Reto td559457).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: Matches every task class in :attr:`Lane.task_classes`.
ANY_CLASS = "*"

#: The job type whose live rows count against a lane's cap.
RUN_SLICE = "run_slice"

#: How long a lane stays out of the fit set after its host refused a
#: launch (gr571395): long enough that a broken host costs one failed
#: probe per window, short enough that a fixed login is picked up the
#: same hour.
HOST_REFUSAL_BACKOFF = timedelta(minutes=30)

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
    "src/precis/workers/job_types/ci_red_digest.py",
    "src/precis/workers/job_types/release_poll.py",
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
    is the fit set; :data:`ANY_CLASS` fits every class. An MCP-less lane
    takes ``code``, plus ``review`` and ``validate`` whose one verdict rides
    the session's final message (``handoff: review: …``, ``validated: …``;
    td564019 cut 1); the templates class every node that mints or posts
    through the precis MCP by its step (``triage``, ``brief``, ``pcb``;
    :mod:`precis.plans.templates`) so it never lands on such a lane.
    ``deny_write_paths`` are the repo globs a slice on this lane may not
    write (:data:`FACTORY_DENY_WRITE_PATHS` for every seed lane).
    ``vendor`` is the model vendor behind the lane (``''`` = unknown); a
    review is picked with ``exclude_vendor`` = the builder lane's vendor.
    ``models`` are the model ids the lane's adapter accepts, cheapest
    first (empty while the adapter is unbuilt). ``default_model`` is what
    an unpinned ``run_slice`` job on this lane gets; ``None`` falls to
    the router's frontier pin. ``model_prefix`` widens :meth:`accepts_model`
    past ``models``: the ``claude -p`` CLI takes any ``claude-`` id, so a
    triage pin like ``claude-sonnet-5-5`` still routes to the Claude lane
    (``''`` = ``models`` is exhaustive; a lane with neither accepts no pin,
    so a pinned model never lands on a lane that cannot run it).
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
    model_prefix: str = ""

    def fits(self, task_class: str) -> bool:
        """``True`` when this lane takes ``task_class`` work."""
        return ANY_CLASS in self.task_classes or task_class in self.task_classes

    def accepts_model(self, model: str) -> bool:
        """``True`` when a slice pinned to ``model`` may launch here: the id
        is in ``models``, or starts with ``model_prefix``."""
        if model in self.models:
            return True
        return bool(self.model_prefix) and model.startswith(self.model_prefix)


#: ``model_prefix`` per adapter — the one fact the code seed carried that
#: no ``llm_account`` column holds: ``claude -p`` accepts every
#: ``claude-`` id, so a triage pin outside ``models`` still routes to the
#: lane.
_ADAPTER_MODEL_PREFIX: dict[str, str] = {"claude_p": "claude-"}

#: Adapters a ``run_slice`` submit can launch — mirrors
#: ``precis.workers.job_types._slice_adapters.ADAPTERS`` (restated so
#: this package stays below the workers layer, like
#: ``_TERMINAL_STATUSES``; a drift test pins the two equal). An account
#: on any other adapter — ``api`` (metered), ``kimi_tree`` (declared,
#: unbuilt) — derives to no lane, whatever its ``enabled`` flag.
LAUNCHABLE_ADAPTERS: frozenset[str] = frozenset(
    {"claude_p", "codex_exec", "devin_tree"}
)


def get_lane(name: str, store: Any) -> Lane | None:
    """The account-derived lane called ``name``, or ``None``."""
    for lane in load_lanes(store):
        if lane.name == name:
            return lane
    return None


def derive_lanes(rows: Iterable[Any]) -> tuple[Lane, ...]:
    """The lane table from ``llm_account`` rows (td568401 S1c).

    Order is ``(lane_rank, name)`` — the column carries the preference
    order the seed tuple held by position (:func:`list_accounts` is
    name-ordered), which :func:`pick_lane` ties and :func:`ladder` keep.
    ``concurrency`` is the cap, ``plan`` the credential_pool (unread —
    kept because removing the field churns test constructors),
    ``task_classes`` the fit set (``*`` = :data:`ANY_CLASS`),
    ``model_prefix`` the adapter's (:data:`_ADAPTER_MODEL_PREFIX`).
    Rows on an adapter outside :data:`LAUNCHABLE_ADAPTERS` derive to
    nothing — an enabled api-adapter account is never a builder lane."""
    return tuple(
        Lane(
            name=a.name,
            adapter=a.adapter,
            credential_pool=a.plan,
            cap=a.concurrency,
            task_classes=frozenset(a.task_classes),
            enabled=a.enabled,
            vendor=a.vendor,
            models=tuple(a.models),
            default_model=a.default_model,
            model_prefix=_ADAPTER_MODEL_PREFIX.get(a.adapter, ""),
        )
        for a in sorted(rows, key=lambda a: (a.lane_rank, a.name))
        if a.adapter in LAUNCHABLE_ADAPTERS
    )


def load_lanes(store: Any) -> tuple[Lane, ...]:
    """The lane table read from ``llm_account`` at call time.

    The accounts module is imported lazily: ``precis.utils.llm`` is the
    routing layer and pulling it in at module top would sit the whole
    router under every caller of this leaf. ``accounts.list_accounts``
    goes through the module attribute so tests patch the seam instead
    of needing a DB."""
    from precis.utils.llm import accounts

    return derive_lanes(accounts.list_accounts(store))


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


def lane_backoffs(store: Any, now: datetime | None = None) -> dict[str, datetime]:
    """``{lane name: backoff end}`` for lanes backed off by a host refusal.

    A lane is backed off while its NEWEST ``run_slice`` job (by ref id,
    per ``meta.lane``) carries ``meta.host_refusal.until`` in the future
    (gr571395): a later job on the lane without the mark — a probe that
    launched — clears the backoff by being newer, so no second write
    path is needed.
    """
    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (r.meta->>'lane')
                   r.meta->>'lane', r.meta->'host_refusal'->>'until'
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %s
               AND r.meta ? 'lane'
             ORDER BY r.meta->>'lane', r.ref_id DESC
            """,
            (RUN_SLICE,),
        ).fetchall()
    out: dict[str, datetime] = {}
    for lane, raw in rows:
        if not raw:
            continue
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if until > now:
            out[str(lane)] = until
    return out


def pick_lane(
    store: Any,
    task_class: str,
    *,
    lanes: tuple[Lane, ...] | None = None,
    counts: Mapping[str, int] | None = None,
    exclude_vendor: str | None = None,
    model: str | None = None,
    backoff: Mapping[str, datetime] | None = None,
) -> Lane | None:
    """The lane a ``task_class`` slice launches on, or ``None`` when every
    fit lane is full (or none fits).

    Fit first (enabled lanes whose ``task_classes`` take the class and,
    with ``model`` -- the slice's ``params.model`` pin -- whose
    :meth:`Lane.accepts_model` takes it: a triage-pinned claude id never
    lands on the codex lane, which cannot run it), then the fewest live
    ``run_slice`` jobs carrying that ``meta.lane``, only lanes under their
    cap; ties keep table order. ``lanes`` defaults to the ``llm_account``
    table read at call time (like :func:`ladder`), so a row edit is seen;
    ``counts`` skips the live-count DB read (tests, or a caller that
    already has them). Lanes
    backed off by a host refusal leave the fit set: ``backoff`` carries
    them explicitly, else they are read with :func:`lane_backoffs` — only
    when ``counts`` is ``None`` (the same DB rule as ``counts``, so a
    DB-free caller stays DB-free).

    ``exclude_vendor`` (the review rule: the reviewer never comes from the
    builder's vendor) drops fit lanes of that vendor. When no fit lane of
    another vendor is enabled the pick falls back to the full fit set --
    the caller sees ``lane.vendor == exclude_vendor`` and says so; once a
    second vendor's lane is enabled the rule holds (a full one is waited
    for, not bypassed).
    """
    table = load_lanes(store) if lanes is None else lanes
    fit = [lane for lane in table if lane.enabled and lane.fits(task_class)]
    if model:
        fit = [lane for lane in fit if lane.accepts_model(model)]
    backed = (
        {name for name, end in backoff.items() if end > datetime.now(UTC)}
        if backoff is not None
        else (set(lane_backoffs(store)) if counts is None else set())
    )
    if backed:
        fit = [lane for lane in fit if lane.name not in backed]
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
    task_class: str,
    *,
    store: Any = None,
    lanes: tuple[Lane, ...] | None = None,
) -> tuple[Rung, ...]:
    """The cross-lane retry ladder for ``task_class`` (Reto td559457: a
    failed slice climbs before the cap asks): every enabled fit lane in
    table order, each contributing its ``models`` from ``default_model``
    upward, or one model-less rung when it declares none. With today's
    table that is devin high → devin max → codex → sonnet → opus. The
    table is ``lanes`` or the ``llm_account`` read — one of them is
    required."""
    if lanes is None:
        if store is None:
            raise TypeError("lanes.ladder: pass store or lanes")
        lanes = load_lanes(store)
    out: list[Rung] = []
    for lane in lanes:
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
    "HOST_REFUSAL_BACKOFF",
    "LAUNCHABLE_ADAPTERS",
    "RUN_SLICE",
    "Lane",
    "Rung",
    "derive_lanes",
    "get_lane",
    "ladder",
    "lane_backoffs",
    "live_lane_counts",
    "load_lanes",
    "pick_lane",
    "rung_index",
]
