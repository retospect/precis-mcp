"""Shadow lane decisions for the dark factory (td572552 S4b).

Follows td568207 S4, which ran :func:`precis.utils.llm.decide.decide` in
shadow beside :func:`~precis.utils.llm.router.route`'s ladder. This
module is the lane half: it maps the factory's lane table — the
``llm_account`` rows :func:`precis.plans.lanes.load_lanes` derives —
onto :class:`~precis.utils.llm.decide.Offering`, asks ``decide()`` for
its pick beside the legacy one (:func:`precis.plans.lanes.pick_lane`
in ``run_slice.submit`` and ``plan_dispatch._review_lane``;
:func:`~precis.plans.lanes.ladder`'s chain in ``plan_dispatch._escalate``)
and returns the job-meta ``decision`` record — the ``explain()`` dict,
the chain the legacy path actually ran and the agreement verdict, the
same shape the router's call-log stamp carries.

The shadow is telemetry only. It never reserves capacity, never changes
which lane launches and never fails a dispatch: every failure inside
:func:`shadow_stamp` reads as ``None`` — a miss logged, not an error —
the same fail-open the router's ``_decision_probe`` keeps. Mode is
:func:`~precis.utils.llm.decide.mode_for` on ``"slice"``; the
``llm.decide.*`` settings rows and ``PRECIS_LLM_DECIDE`` light it, and
``legacy`` (the default) computes nothing.

Mapping: one offering per declared model per lane — a model-less lane
contributes one ``""`` offering (the account default, its
``Rung(None)`` rung). ``adapter`` is the lane's adapter, ``provider``
its vendor, ``account`` and ``lane`` its name (the account row it
derived from; ``credential_pool`` is the account's ``plan`` —
descriptive — while ``account`` must key :data:`Ledger.accounts` for the
entitlement checks). ``pool_keys`` are the account's own ``llm:``
pools plus a synthetic ``lane:<name>:cap`` pool that carries the
capacity signal the real pick reads: a lane at its ``cap``, inside a
host-refusal backoff or health-parked paces ``exhausted``, which the
decision's availability filter rejects and its rank demotes with no
lane-specific branches. A lane declares its fit in ``capability``:
``task:*`` or one ``task:<class>`` key per class, and the adapter flags
``run_slice._lane_offers`` reads (``mcp``, ``read_mcp``,
``budget_capable``, ``budget_watch``). The :class:`SliceRequest`
carries the pick's own constraints so the ``rejected`` list records why
each skipped lane was out, and ``cost_class`` becomes ``billing`` so
the decision's free-before-paid capacity order mirrors
:data:`~precis.plans.lanes.COST_ORDER`. The executed lane's adapter and
resolved model anchor the ``legacy_model``/``legacy_adapter`` tie-break
— a dead-even shadow pick lands on the lane the chain ran, so ``agree``
measures real disagreement.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.plans import lanes as _lanes
from precis.workers.job_types._slice_adapters import ADAPTERS

if TYPE_CHECKING:
    from precis.plans.lanes import Lane, Rung
    from precis.utils.llm import decide

log = logging.getLogger(__name__)


def _cap_key(lane: str) -> str:
    """The synthetic pool key carrying one lane's capacity state."""
    return f"lane:{lane}:cap"


def offerings_for(
    table: Sequence[Lane],
    accts: Mapping[str, Any],
    *,
    pin: str | None = None,
) -> tuple[decide.Offering, ...]:
    """The ``decide()`` view of a lane table.

    One offering per model in :attr:`Lane.models` (``()`` models = one
    account-default offering), plus a synthetic one for ``pin`` on each
    lane whose :meth:`~precis.plans.lanes.Lane.accepts_model` takes it
    — a ``model_prefix`` pin names no declared model but still runs.
    Disabled lanes stay in the table (enabled=False) so the rejection
    list records them; parked/at-cap/backed-off state rides the pool,
    not the flag — the pick treats those as temporary holds, not the
    human switch.
    """
    from precis.utils.llm import decide

    out: list[decide.Offering] = []
    for lane in table:
        adapter = ADAPTERS.get(lane.adapter)
        capability: dict[str, int] = {}
        if adapter is not None:
            capability = {
                "mcp": int(adapter.mcp),
                "read_mcp": int(adapter.read_mcp),
                "budget_capable": int(adapter.budget_capable),
                "budget_watch": int(adapter.budget_watch),
            }
        for cls in sorted(lane.task_classes):
            capability[f"task:{cls}"] = 1
        acct = accts.get(lane.name)
        pool_keys = (tuple(acct.pools) if acct is not None else ()) + (
            _cap_key(lane.name),
        )
        models: tuple[str, ...] = lane.models or ("",)
        if pin and pin not in models and lane.accepts_model(pin):
            models = (*models, pin)
        for model in models:
            out.append(
                decide.Offering(
                    decide.offering_key(model, lane.adapter, lane.name),
                    model,
                    provider=lane.vendor,
                    account=lane.name,
                    adapter=lane.adapter,
                    billing=lane.cost_class,
                    capacity_class=lane.cost_class,
                    locality="cloud",
                    pool_keys=pool_keys,
                    lane=lane.name,
                    capability=capability,
                    enabled=lane.enabled,
                )
            )
    return tuple(out)


def _legacy_ladder(
    pairs: Sequence[tuple[str, str | None]],
    offerings: tuple[decide.Offering, ...],
    ledger: decide.Ledger,
    chosen_rung: int,
) -> decide.Decision:
    """The chain the legacy path ran as a Decision — the ``ladder()``
    rungs or the single ``pick_lane`` result — so
    :func:`decide.compare` measures the shadow against it the way
    ``route()`` measures its own rung chain. A rung naming no offering
    (the resolved model is not one the lane declares) gets a bare
    identity so the comparison still names the executed lane."""
    from precis.utils.llm import decide
    from precis.utils.llm.policy import Requirement
    from precis.utils.llm.router import Tier

    by_key = {(o.lane, o.model): o for o in offerings}
    ranked: list[decide.Ranked] = []
    for lane_name, model in pairs:
        o = by_key.get((lane_name, model or ""))
        if o is None:
            o = decide.Offering(
                decide.offering_key(model or "", "", lane_name),
                model or "",
                lane=lane_name,
            )
        ranked.append(
            decide.Ranked(o, "unknown", o.capacity_class, "unknown", "legacy rung")
        )
    idx = min(chosen_rung, len(ranked) - 1)
    return decide.Decision(
        "legacy",
        ranked[idx].offering if idx >= 0 else None,
        False,
        tuple(ranked),
        (),
        {},
        {k: p.state for k, p in ledger.pools.items()},
        None,
        Requirement(Tier.SMALL),
    )


def shadow_stamp(
    store: Any,
    *,
    task_class: str,
    table: Sequence[Lane],
    site: str,
    picked: tuple[str, str | None],
    rungs: Sequence[Rung] = (),
    chosen_rung: int = 0,
    counts: Mapping[str, int] | None = None,
    backoffs: Mapping[str, datetime] | None = None,
    needs_mcp: bool = False,
    code_only: bool = False,
    exclude_vendor: str | Sequence[str] | None = None,
    required_capability: str | None = None,
    model_pin: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The job-meta ``decision`` record for one legacy lane pick.

    ``site`` names the pick site (``submit``/``review``/``escalate``);
    ``picked`` the ``(lane, model)`` the legacy path executed;
    ``rungs``+``chosen_rung`` the ladder chain an escalation climbed
    (else the executed pick alone is the chain). ``table`` is the FULL
    lane table — the request's constraints, not a pre-shrunk pick set,
    carry the exclusions so the rejection list records them.
    ``counts``/``backoffs`` let the caller pass what it already read;
    ``None`` reads them fresh. ``None`` under the default ``legacy``
    mode and on any failure — the shadow never touches dispatch.
    """
    from precis.utils.llm import accounts, decide
    from precis.utils.llm.policy import Requirement
    from precis.utils.llm.router import Tier

    try:
        mode = decide.mode_for("slice")
        if mode == "legacy":
            return None
        now = now or datetime.now(UTC)
        try:
            accts = {a.name: a for a in accounts.list_accounts(store)}
        except Exception:
            accts = {}
        try:
            pools = {
                p.provider: accounts.pacing(p, now) for p in accounts.list_pools(store)
            }
        except Exception:
            pools = {}
        if counts is None:
            try:
                counts = _lanes.live_lane_counts(store)
            except Exception:
                counts = {}
        if backoffs is None:
            try:
                backoffs = _lanes.lane_backoffs(store)
            except Exception:
                backoffs = {}
        for lane in table:
            live = counts.get(lane.name, 0)
            backed = (backoffs.get(lane.name) or datetime.min.replace(tzinfo=UTC)) > now
            held = lane.parked or backed or live >= lane.cap
            pools[_cap_key(lane.name)] = accounts.Pacing(
                "exhausted" if held else "behind",
                min(1.0, live / lane.cap) if lane.cap else 1.0,
                1.0,
                None,
                backoffs.get(lane.name),
            )
        picked_lane, picked_model = picked
        lane_by_name = {lane.name: lane for lane in table}
        executed = lane_by_name.get(picked_lane)
        offerings = offerings_for(table, accts, pin=model_pin)
        ledger = decide.Ledger(
            offerings,
            accts,
            pools,
            frozenset(),
            now,
            legacy_adapter=executed.adapter if executed is not None else None,
            legacy_model=picked_model,
        )
        req = decide.SliceRequest(
            task_class,
            needs_mcp=needs_mcp,
            code_only=code_only,
            exclude_vendor=exclude_vendor,
            required_capability=required_capability,
            model=model_pin,
            work_class="background",
        )
        shadow = decide.decide(req, Requirement(Tier.SMALL), ledger, mode)
        pairs: list[tuple[str, str | None]] = [(r.lane, r.model) for r in rungs]
        legacy = _legacy_ladder(pairs or [picked], offerings, ledger, chosen_rung)
        return {
            "site": site,
            **shadow.explain(),
            "legacy": {
                "chosen": decide.compact_offering(legacy.chosen)
                if legacy.chosen
                else None,
                "keys": [r.offering.key for r in legacy.ranked],
            },
            **decide.compare(shadow, legacy),
            "lane_match": bool(
                shadow.chosen is not None and shadow.chosen.lane == picked_lane
            ),
            "executed": "legacy",
        }
    except Exception:
        log.debug("lane_decide: shadow stamp failed", exc_info=True)
        return None
