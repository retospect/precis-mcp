"""Deterministic, read-only offering decisions for shadow routing.

Only ``ledger_from_store`` performs I/O. Decisions never reserve capacity or
change the legacy dispatch chain; their explanation is call-log telemetry.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.budget.work_class import resolve as resolve_work_class

# ``router`` stays a module reference (call-time ``resolve_model`` lookup):
# ``decide`` is first imported inside a ``route()`` call, so a from-import
# bound while a test monkeypatches the name would keep the fake (policy.py).
from precis.utils.llm import accounts, admit, policy, recipes, router
from precis.utils.llm.accounts import Pacing
from precis.utils.llm.policy import Requirement
from precis.utils.llm.router import LlmRequest, Rung, Tier, Transport

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Offering:
    key: str
    model: str
    provider: str = ""
    account: str | None = None
    adapter: str = ""
    variant: str | None = None
    billing: str = "metered"
    capacity_class: str = "metered"
    price_in: float | None = None
    price_out: float | None = None
    locality: str = "unknown"
    window: int | None = None
    tools: bool | None = None
    structured: bool | None = None
    live: bool | None = None
    pool_keys: tuple[str, ...] = ()
    lane: str | None = None
    capability: Mapping[str, int] = field(default_factory=dict)
    enabled: bool = True
    #: The card's ``tier_floor`` (``policy._tier_of``), as the tier value
    #: string — the smallest demand the offering can serve. ``None`` = the
    #: card doesn't say; the capability filter then rejects any demand
    #: above SMALL (the offering can't prove it can serve it), passing
    #: only SMALL demands.
    tier_floor: str | None = None


@dataclass(frozen=True)
class SliceRequest:
    task_kind: str
    needs_mcp: bool = False
    code_only: bool = False
    exclude_vendor: str | None = None
    consequence: str = "medium"
    privacy_class: str = "default"


@dataclass(frozen=True)
class Ledger:
    offerings: tuple[Offering, ...]
    accounts: Mapping[str, accounts.Account]
    pools: Mapping[str, Pacing]
    unavailable_transports: frozenset[str]
    now: datetime
    legacy_adapter: str | None = None
    #: The model the legacy ladder's rung 0 resolved to — a rank tie-break
    #: (:func:`decide`), so an otherwise-equal offering matching the lane the
    #: legacy path actually ran wins and ``compare`` agrees. ``None`` leaves
    #: the ranked order unchanged.
    legacy_model: str | None = None
    #: ``(model, adapter) -> (calls, ok)`` over :data:`HEALTH_WINDOW_MIN`
    #: minutes of ``llm_call_log`` — the offering-level failure signal the
    #: rank demotion reads (td568209). ``budget``-class rows are excluded
    #: (a spend-cap pause is not the offering's fault); gated rows count.
    #: Refreshed with the 15-second ledger entry, never per call.
    health: Mapping[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    #: ``model -> (capacity, free)`` summed over the ``resource_slots``
    #: ``llm:<model>`` rows — the local pool's live concurrency ceiling
    #: and headroom the rank demotion reads (gr576274). Refreshed with
    #: the 15-second ledger entry, never per call.
    slots: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: ``task_kind -> offered concurrency`` in slot-equivalents over
    #: :data:`RATE_WINDOW_MIN` minutes of ``llm_call_log`` — arrivals ×
    #: mean served duration / window (Little's law). A kind whose demand
    #: meets a local offering's ceiling can't be absorbed there, so the
    #: offering sorts after the next rung *before* the call, not after a
    #: refusal (gr576274).
    rates: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Ranked:
    offering: Offering
    band: str
    capacity_class: str
    pool_state: str
    reason: str


@dataclass(frozen=True)
class Rejected:
    offering_key: str
    filter: str
    reason: str


#: Capability rank by tier value — SMALL < MEDIUM < BIG < FRONTIER — for the
#: tier-floor capability filter in :func:`_rejection`. A tier string absent
#: here (a Requirement field type can't produce one, but a hand-built one
#: could) reads as no demand floor: nothing is rejected on it.
_TIER_ORDER = {
    tier.value: rank
    for rank, tier in enumerate((Tier.SMALL, Tier.MEDIUM, Tier.BIG, Tier.FRONTIER))
}

#: The health guard's window and sample floor (td568209): an offering
#: whose ``llm_call_log`` rows over the window show at least this many
#: calls and zero successes sorts after every healthy one.
HEALTH_WINDOW_MIN = 30
HEALTH_MIN_CALLS = 20

#: The kind-rate window (gr576274): long enough to estimate a mean served
#: duration from, short enough that a rate flip shows within a couple of
#: ledger rebuilds. Rides the same 15 s cache entry, never per call.
RATE_WINDOW_MIN = 15


def _unhealthy_calls(o: Offering, ledger: Ledger) -> int:
    """Calls behind an all-failing offering's demotion, ``0`` when it
    ranks normally — below the sample floor, or one success seen."""
    calls, ok = ledger.health.get((o.model, o.adapter), (0, 0))
    return calls if calls >= HEALTH_MIN_CALLS and ok == 0 else 0


def _local_overload(o: Offering, ledger: Ledger, kind: str | None) -> str | None:
    """The demotion reason for a saturated local rung, ``None`` when it
    ranks normally (gr576274). Two signals, both read off the pool's own
    ``resource_slots`` rows: the live ``free`` count is spent — the pool
    is saturated *now* — or the deciding kind's offered load meets or
    exceeds the pool's ceiling, so its calls would queue past the slot
    wait and the next rung should win *before* the call, not after a
    refusal. A local operating point with no ``llm:<model>`` pool row
    has no ceiling to read and keeps its rank."""
    if o.billing != "local":
        return None
    capacity, free = ledger.slots.get(o.model, (0, 0))
    if capacity <= 0:
        return None
    if free <= 0:
        return f"saturated: 0 of {capacity} local slots free"
    load = ledger.rates.get(kind or "", 0.0)
    if load >= capacity:
        return f"over-ceiling: {kind} at {load:.1f} of {capacity} local slots"
    return None


#: Fields a logged decision keeps per offering — identity, lane and price.
#: The full dataclass (capability map, pool keys, window) made one
#: /decisions row ~6.7 KB; this subset covers every consumer.
_OFFERING_LOG_FIELDS = (
    "key",
    "model",
    "adapter",
    "account",
    "billing",
    "price_in",
    "price_out",
)


def compact_offering(o: Offering) -> dict[str, Any]:
    """The log form of an offering (:data:`_OFFERING_LOG_FIELDS` only)."""
    return {k: getattr(o, k) for k in _OFFERING_LOG_FIELDS}


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class Decision:
    mode: str
    chosen: Offering | None
    pinned: bool
    ranked: tuple[Ranked, ...]
    rejected: tuple[Rejected, ...]
    rejected_counts: Mapping[str, int]
    pools: Mapping[str, str]
    recipe: Any
    requirement: Requirement
    classifier: str = "none"
    fallback_model: str | None = None
    reason: str = "ranked"

    def explain(self) -> dict[str, Any]:
        """Bounded JSON-safe evidence; totals retain every rejection."""
        result = asdict(self)
        result["chosen"] = (
            compact_offering(self.chosen) if self.chosen is not None else None
        )
        result["ranked"] = [
            {
                **compact_offering(r.offering),
                "band": r.band,
                "capacity_class": r.capacity_class,
                "pool_state": r.pool_state,
                "reason": r.reason,
            }
            for r in self.ranked[:5]
        ]
        result["rejected"] = [asdict(r) for r in self.rejected[:10]]
        return dict(_json(result))


def requirement_for(req: LlmRequest) -> Requirement:
    """Derive demand from request text and declared recipe, without a judge."""
    from precis.utils.llm.router import _content_text

    recipe = recipes.lookup(req.source)
    declared = recipe.requirement if recipe else {}
    chars = len(req.prompt or "") + sum(
        len(_content_text(m.get("content", ""))) for m in req.messages or []
    )
    return Requirement(
        tier_floor=req.tier,
        axis=declared.get("axis"),
        min_ordinal=int(declared.get("min_ordinal", 1)),
        max_input=admit.estimate_tokens(chars),
        needs_tools=req.tools_needed,
        needs_structured=False,
        transport="local" if req.placement == "local" else None,
    )


def _pool_state(o: Offering, ledger: Ledger) -> tuple[str, float]:
    pools = [ledger.pools[k] for k in o.pool_keys if k in ledger.pools]
    order = {"unknown": 0, "behind": 1, "on-pace": 2, "ahead": 3, "exhausted": 4}
    state = max(
        (p.state for p in pools), key=lambda s: order.get(s, 0), default="unknown"
    )
    deficit = min(
        (1 - p.fraction for p in pools if p.fraction is not None), default=0.0
    )
    return state, deficit


def _rejection(
    o: Offering, req: LlmRequest | SliceRequest, r: Requirement, ledger: Ledger
) -> tuple[str, str] | None:
    account = ledger.accounts.get(o.account or "")
    if not o.enabled or (account is not None and not account.enabled):
        return "entitlement", "offering or account disabled"
    if o.billing == "subscription" and account is None:
        return "entitlement", "subscription account missing"
    if account is not None:
        model = account.entitlement.get("models", {}).get(o.model, {})
        if isinstance(model, dict) and model.get("allowed") is False:
            return "entitlement", "model not allowed by account"
    if o.billing == "local" and o.live is not True:
        return "entitlement", "local slot is not live"
    if req.privacy_class == "local-only" and o.locality != "local":
        return "privacy", "local-only demand requires local offering"
    if (
        account is not None
        and req.privacy_class != "default"
        and account.privacy.get("training_use") != "no"
    ):
        return "privacy", "account may train on prompts or has no stance recorded"
    if r.transport == "local" and o.locality != "local":
        return "privacy", "local placement requires local offering"
    if (
        r.max_input is not None
        and o.window is not None
        and not admit.admit(r.max_input, o.window).fits
    ):
        return "capability", "input exceeds offering window"
    if r.needs_tools and o.tools is False:
        return "capability", "tools unsupported"
    if r.needs_structured and o.structured is False:
        return "capability", "structured output unsupported"
    demand = _TIER_ORDER.get(r.tier_floor, 0)
    if o.tier_floor is None:
        if demand > 0:
            return "capability", "tier floor unset"
    elif (_TIER_ORDER.get(o.tier_floor) or 0) < demand:
        return "capability", "offering tier below demanded tier floor"
    ordinal = o.capability.get(r.axis, 0) if r.axis else 0
    if ordinal and ordinal < r.min_ordinal - 1:
        return "capability", "axis ordinal below marginal"
    if ordinal == r.min_ordinal - 1 and ordinal and req.consequence != "low":
        return "capability", "marginal requires low consequence"
    if o.adapter in ledger.unavailable_transports:
        return "availability", "transport unavailable"
    state, _ = _pool_state(o, ledger)
    if state == "exhausted":
        return "availability", "pool exhausted"
    if (
        state == "ahead"
        and resolve_work_class(getattr(req, "work_class", None)).value == "background"
    ):
        return "availability", "pool ahead for background work"
    return None


def decide(
    request: LlmRequest | SliceRequest,
    requirement: Requirement,
    ledger: Ledger,
    mode: str,
) -> Decision:
    """Filter and rank offerings, retaining the free ranking beside a pin."""
    kind = getattr(request, "task_kind", None)
    ranked: list[Ranked] = []
    rejected: list[Rejected] = []
    for o in ledger.offerings:
        try:
            failure = _rejection(o, request, requirement, ledger)
            ordinal = o.capability.get(requirement.axis, 0) if requirement.axis else 0
            if not isinstance(ordinal, int):
                raise ValueError("capability ordinal must be an integer")
            if o.price_in is not None and not isinstance(o.price_in, (float, int)):
                raise ValueError("price must be numeric")
        except Exception:
            log.debug(
                "decide: malformed offering %s skipped",
                getattr(o, "key", "unknown"),
                exc_info=True,
            )
            continue
        if failure:
            rejected.append(Rejected(o.key, *failure))
            continue
        band = (
            "unknown"
            if not ordinal
            else "strong"
            if ordinal >= requirement.min_ordinal + 2
            else "adequate"
            if ordinal >= requirement.min_ordinal
            else "marginal"
        )
        state, _ = _pool_state(o, ledger)
        sick = _unhealthy_calls(o, ledger)
        overload = _local_overload(o, ledger, kind)
        ranked.append(
            Ranked(
                o,
                band,
                o.capacity_class,
                state,
                f"{band}; {o.billing}; {state}"
                + (
                    f"; unhealthy: 0 of {sick} calls ok in {HEALTH_WINDOW_MIN} min"
                    if sick
                    else ""
                )
                + (f"; {overload}" if overload else ""),
            )
        )
    if any(r.band in {"strong", "adequate"} for r in ranked):
        for r in ranked:
            if r.band == "marginal":
                rejected.append(
                    Rejected(
                        r.offering.key, "capability", "adequate alternative survives"
                    )
                )
        ranked = [r for r in ranked if r.band != "marginal"]

    def rank(r: Ranked) -> tuple[int, int, int, float, float, int, str]:
        o = r.offering
        state, deficit = _pool_state(o, ledger)
        capacity = (
            0
            if o.billing == "free" and state == "behind"
            else 1
            if o.billing == "local"
            else 2
            if o.billing == "subscription" and state != "ahead"
            else 3
            if o.billing == "subscription"
            else 4
        )
        # A dead-even pick (band, capacity class, deficit, price) lands on
        # the lane the legacy ladder actually ran — shadow telemetry then
        # measures real disagreement, not an alphabetical coin flip.
        legacy_match = (
            0
            if (
                ledger.legacy_model is not None
                and ledger.legacy_adapter is not None
                and o.model == ledger.legacy_model
                and _ADAPTER_FAMILY.get(o.adapter, o.adapter)
                == _ADAPTER_FAMILY.get(ledger.legacy_adapter, ledger.legacy_adapter)
            )
            else 1
        )
        return (
            1
            if _unhealthy_calls(o, ledger)
            or _local_overload(o, ledger, kind) is not None
            else 0,
            {"strong": 0, "adequate": 1, "unknown": 2, "marginal": 3}[r.band],
            capacity,
            -deficit if capacity in {0, 2} else 0,
            o.price_in if o.price_in is not None else math.inf,
            legacy_match,
            o.key,
        )

    ranked.sort(key=rank)
    pin = getattr(request, "model", None)
    chosen = ranked[0].offering if ranked else None
    if pin:
        candidates = [o for o in ledger.offerings if o.model == pin]
        candidates.sort(key=lambda o: (o.adapter != ledger.legacy_adapter, o.key))
        chosen = (
            candidates[0]
            if candidates
            else Offering(f"{pin}@pinned/none", pin, adapter="pinned")
        )
    return Decision(
        mode,
        chosen,
        bool(pin),
        tuple(ranked),
        tuple(rejected),
        dict(Counter(r.filter for r in rejected)),
        {k: p.state for k, p in ledger.pools.items()},
        getattr(request, "task_kind", None),
        requirement,
        fallback_model=router.resolve_model(requirement.tier_floor)
        if chosen is None
        else None,
        reason="pinned"
        if pin
        else "ranked"
        if chosen
        else "no offering survives; using tier floor",
    )


def offering_key(
    model: str, adapter: str, account: str | None, variant: str | None = None
) -> str:
    """The one offering identity — ``model@adapter/account-or-none[/variant]``
    — shared by the catalog builder and the legacy rung synthesis so a key
    means the same operating point on both sides of a comparison."""
    return f"{model}@{adapter}/{account or 'none'}" + (f"/{variant}" if variant else "")


def rung_key(rung: Rung, model: str) -> str:
    """Stable offering identity for a legacy rung."""
    if rung.transport in {Transport.CLAUDE_P, Transport.CLAUDE_AGENT}:
        account = "anthropic-api" if rung.bare else "claude-sub"
    elif rung.transport == Transport.CODEX_EXEC:
        account = "codex"
    elif rung.transport == Transport.LOCAL:
        account = "local"
    else:
        account = rung.profile or "metered"
    return offering_key(rung.model or model, rung.transport.value, account)


def rung_for(offering: Offering, accts: Mapping[str, accounts.Account]) -> Rung | None:
    """The dispatch rung for a chosen offering — the apply path's inverse
    of :func:`rung_key` (td568209).

    ``None`` when the offering's adapter names no :class:`Transport` —
    the caller falls back ``unresolved``. The ``anthropic-api`` account
    rides ``--bare`` (per-token billing, mirror of :func:`rung_key`); an
    ``openai_compat`` account offering carries its account's ``profile``;
    the label mirrors ``locality`` so
    :func:`~precis.utils.llm.router._rung_is_cloud` classifies the lane
    the offering described."""
    try:
        transport = Transport(offering.adapter)
    except ValueError:
        return None
    account = accts.get(offering.account or "")
    return Rung(
        transport,
        model=offering.model,
        label="local" if offering.locality == "local" else "cloud",
        bare=offering.account == "anthropic-api",
        profile=(
            account.profile
            if account is not None and account.adapter == "openai_compat"
            else None
        ),
    )


def legacy_decision(ladder: Sequence[Rung], model: str, ledger: Ledger) -> Decision:
    from precis.utils.llm.router import _rung_is_cloud

    ranked: list[Ranked] = []
    for rung in ladder:
        key = rung_key(rung, model)
        account = key.rsplit("/", 1)[1]
        rung_model = rung.model or model
        # The catalog key carries account-or-none and often a variant the rung
        # can't know — bind by (model, adapter), preferring the rung's own
        # account, so the legacy lane inherits the catalog price and flags.
        candidates = [
            o
            for o in ledger.offerings
            if o.model == rung_model and o.adapter == rung.transport.value
        ]
        o = next((c for c in candidates if c.account == account), None)
        if o is None and candidates:
            o = candidates[0]
        if o is None:
            local = not _rung_is_cloud(rung)
            pools = (
                ledger.accounts[account].pools
                if account in ledger.accounts
                else accounts.CLAUDE_SUB_POOLS
                if account == "claude-sub"
                else ()
            )
            o = Offering(
                key,
                rung_model,
                account=account,
                adapter=rung.transport.value,
                billing="local"
                if local
                else "subscription"
                if account in {"claude-sub", "codex"}
                else "metered",
                price_in=0.0 if local else None,
                price_out=0.0 if local else None,
                locality="local" if local else "cloud",
                pool_keys=pools,
            )
        ranked.append(
            Ranked(
                o, "unknown", o.capacity_class, _pool_state(o, ledger)[0], "legacy rung"
            )
        )
    return Decision(
        "legacy",
        ranked[0].offering if ranked else None,
        False,
        tuple(ranked),
        (),
        {},
        {k: p.state for k, p in ledger.pools.items()},
        None,
        Requirement(Tier.SMALL),
    )


#: Adapters that serve the same lane — a legacy claude_p rung and a shadow
#: claude_agent pick on the same model agree; the openai wires likewise.
_ADAPTER_FAMILY = {
    "claude_p": "claude",
    "claude_agent": "claude",
    "openai_compat": "openai",
    "openai_tools": "openai",
}


def compare(shadow: Decision, legacy: Decision) -> dict[str, Any]:
    def claude(d: Decision) -> bool:
        return bool(
            d.chosen
            and any(k.startswith("llm:claude-sub:") for k in d.chosen.pool_keys)
        )

    def lane(d: Decision) -> tuple[str, str, str | None] | None:
        # Account (and thus bare/profile, both derived from it in
        # rung_for) must match too — same model/adapter family on a
        # different account is a different thing to execute (e.g. the
        # legacy-sub account vs the chosen metered one), not a lane the
        # ladder already covers.
        if d.chosen is None:
            return None
        return (
            d.chosen.model,
            _ADAPTER_FAMILY.get(d.chosen.adapter, d.chosen.adapter),
            d.chosen.account,
        )

    return {
        "agree": bool(shadow.chosen and legacy.chosen and lane(shadow) == lane(legacy)),
        "claude_window": {"legacy": claude(legacy), "shadow": claude(shadow)},
    }


_bad_modes: set[str] = set()

#: ``task kind -> (expiry_monotonic, mode)``. The memo stores the final
#: validated mode, so the default legacy hot path is one dict lookup —
#: no ``app_settings`` read per route() (gr572666; settings misses are
#: never cached, so ``precis.settings.get_str`` would query every call).
_mode_memo: dict[str, tuple[float, str]] = {}
_MODE_MEMO_TTL_S = 15.0


def reset_mode_cache() -> None:
    """Drop the per-kind mode memo (tests, and the decide-mode CLI write)."""
    _mode_memo.clear()


def mode_for(task_kind: str | None) -> str:
    """The decide mode for one task kind.

    Layered switches, first set value wins: the exact-kind row
    ``llm.decide.kind.<task_kind>``, then the prefix row
    ``llm.decide.<prefix>`` (the segment before the first ``.``/``:``),
    then the global ``llm.decide.mode``, then ``PRECIS_LLM_DECIDE``, else
    ``legacy``. An invalid value at the winning layer fails safe to
    legacy — it does not fall through to the next layer."""
    from precis.utils.llm import live_config

    kind = task_kind or "unknown"
    stamp = time.monotonic()
    hit = _mode_memo.get(kind)
    if hit is not None and hit[0] > stamp:
        return hit[1]
    mode = live_config.decide_kind_override(kind)
    if mode is None:
        prefix = re.split(r"[.:]", kind, maxsplit=1)[0]
        mode = live_config.decide_override(prefix)
    if mode is None:
        mode = live_config.decide_default()
    if mode is None:
        mode = (os.environ.get("PRECIS_LLM_DECIDE") or "").strip() or None
    if mode is None:
        mode = "legacy"
    if mode not in {"legacy", "shadow", "on"}:
        if mode not in _bad_modes:
            _bad_modes.add(mode)
            log.warning("invalid llm.decide mode %r; using legacy", mode)
        mode = "legacy"
    _mode_memo[kind] = (stamp + _MODE_MEMO_TTL_S, mode)
    return mode


#: ``id(store) -> (stamp, accounts, pools, offerings, health, slots, rates)``.
#: Offerings, the llm_call_log health/rate windows and the resource_slots
#: pool read ride the same 15 s entry as the account/pool snapshot — a
#: rebuild is the only time the catalog is walked, a heartbeat is probed
#: or a table is grouped; per call stays a dict lookup plus a pure
#: :func:`accounts.pacing` per pool.
_ledger_cache: dict[
    int,
    tuple[
        float,
        list[accounts.Account],
        list[accounts.Pool],
        tuple[Offering, ...],
        dict[tuple[str, str], tuple[int, int]],
        dict[str, tuple[int, int]],
        dict[str, float],
    ],
] = {}
_loader_error_at = -math.inf

#: Transports that ride a subscription credential — an offering on one of
#: these with no explicit ``billing`` is a subscription operating point,
#: never the metered default (prod cards carry no billing key, td568207).
_SUBSCRIPTION_ADAPTERS = frozenset({"claude_p", "claude_agent", "codex_exec"})

#: Adapter a subscription offering's account lookup uses when it differs
#: from the wire adapter — claude_agent runs on the claude_p credential.
_ACCOUNT_ADAPTER = {"claude_agent": "claude_p"}


def _account_offerings(
    accts: list[accounts.Account], catalog: Mapping[str, dict[str, Any]]
) -> tuple[Offering, ...]:
    """One offering per model on each enabled ``openai_compat`` account
    that names a profile — the metered/free side of the owner door
    (td573668).

    Price: the account's ``entitlement.models.<id>`` price wins; absent an
    entry, the card's ``facts_openrouter`` price — ``0``/``0`` reads
    ``free`` under the same rule the catalog loop uses, while an unknown
    price stays ``metered`` and unpriced (a ``free`` plan never implies
    free). Window, tier floor and capability come from the model's card
    when one exists, expressed exactly as the catalog loop does.
    """
    offerings: list[Offering] = []
    for acct in accts:
        if not acct.enabled or acct.adapter != "openai_compat" or not acct.profile:
            continue
        ent_models = (acct.entitlement or {}).get("models") or {}
        for model in acct.models:
            try:
                entry = ent_models.get(model)
                if isinstance(entry, dict) and entry.get("allowed") is False:
                    continue
                price_in = price_out = None
                meta = catalog.get(model) or {}
                if (
                    isinstance(entry, dict)
                    and entry.get("price_in") is not None
                    and entry.get("price_out") is not None
                ):
                    price_in = float(entry["price_in"])
                    price_out = float(entry["price_out"])
                elif meta:
                    facts = meta.get("facts_openrouter") or {}
                    if (
                        facts.get("price_in") is not None
                        and facts.get("price_out") is not None
                    ):
                        price_in = float(facts["price_in"])
                        price_out = float(facts["price_out"])
                billing = "free" if price_in == 0 and price_out == 0 else "metered"
                floor_tier = policy._tier_of(meta)
                offerings.append(
                    Offering(
                        offering_key(model, "openai_compat", acct.name),
                        model,
                        acct.vendor,
                        acct.name,
                        "openai_compat",
                        None,
                        billing,
                        billing,
                        price_in,
                        price_out,
                        "cloud",
                        admit.window_for(meta, "openai_compat") if meta else None,
                        None,
                        None,
                        None,
                        acct.pools,
                        capability={
                            axis: policy._axis_ordinal(meta, axis)
                            for axis in set(meta.get("capability") or {})
                        },
                        tier_floor=floor_tier.value if floor_tier else None,
                    )
                )
            except Exception:
                log.debug(
                    "decide: malformed account offering %s/%s skipped",
                    acct.name,
                    model,
                    exc_info=True,
                )
    return tuple(offerings)


def _build_offerings(
    store: Store, accts: list[accounts.Account]
) -> tuple[Offering, ...]:
    """Catalog cards → offerings; host heartbeats only for a card that
    carries a local operating point. Enabled ``openai_compat`` accounts
    append one offering per model (:func:`_account_offerings`)."""
    offerings: list[Offering] = []
    catalog = admit._catalog(store)
    for model, meta in catalog.items():
        try:
            if not policy._runnable(meta):
                continue
            curated = meta.get("offerings") or []
            endpoints = meta.get("endpoints") or []
            entries = curated or endpoints or [meta.get("facts_openrouter") or {}]
            live_hosts = (
                policy._live_served_hosts(store, meta)
                if any(
                    isinstance(e, dict) and policy._is_local_offering(e)
                    for e in entries
                )
                else frozenset()
            )
            floor_tier = policy._tier_of(meta)
            for entry in entries:
                adapter = str(entry.get("transport") or "openai_compat")
                local = policy._is_local_offering(entry)
                billing = (
                    "local"
                    if local
                    else str(entry["billing"])
                    if entry.get("billing") is not None
                    else "subscription"
                    if adapter in _SUBSCRIPTION_ADAPTERS
                    else "metered"
                )
                account = None
                if billing == "subscription":
                    account_adapter = _ACCOUNT_ADAPTER.get(adapter, adapter)
                    account = (
                        next(
                            (
                                a
                                for a in accts
                                if a.enabled and a.adapter == account_adapter
                            ),
                            None,
                        )
                        or next((a for a in accts if model in a.models), None)
                        or next(
                            (a for a in accts if a.adapter == account_adapter),
                            None,
                        )
                    )
                price_in = (
                    float(entry["price_in"])
                    if entry.get("price_in") is not None
                    else None
                )
                price_out = (
                    float(entry["price_out"])
                    if entry.get("price_out") is not None
                    else None
                )
                # A local operating point spends no dollars: absent a card
                # price it books at 0 so projected cost and the metered sort
                # both see the real number. Subscription stays unpriced.
                if billing == "local":
                    if price_in is None:
                        price_in = 0.0
                    if price_out is None:
                        price_out = 0.0
                if billing == "metered" and price_in == 0 and price_out == 0:
                    billing = "free"
                variant = (
                    str(entry.get("effort") or "")
                    or (
                        "/".join(
                            str(entry[k]) for k in ("provider", "quant") if entry.get(k)
                        )
                        if not curated
                        else ""
                    )
                    or None
                )
                account_name = account.name if account else "local" if local else None
                key = offering_key(model, adapter, account_name, variant)
                params = entry.get("supported_parameters")
                tools_params = params
                if tools_params is None and entry.get("tools") is None:
                    tools_params = (meta.get("facts_openrouter") or {}).get(
                        "supported_parameters"
                    )
                capabilities = {
                    axis: policy._axis_ordinal(meta, axis, entry)
                    for axis in set(meta.get("capability") or {})
                    | set(entry.get("capability") or {})
                }
                offerings.append(
                    Offering(
                        key,
                        model,
                        str(entry.get("provider") or entry.get("endpoint") or ""),
                        account_name,
                        adapter,
                        variant,
                        billing,
                        billing,
                        price_in,
                        price_out,
                        "local" if local else "cloud",
                        int(entry["max_input"])
                        if entry.get("max_input")
                        else admit.window_for(meta, adapter),
                        entry.get("tools")
                        if tools_params is None
                        else "tools" in tools_params,
                        entry.get("structured")
                        if params is None
                        else bool(
                            {"structured_outputs", "response_format"} & set(params)
                        ),
                        policy._offering_live(entry, meta, live_hosts)
                        if local
                        else None,
                        account.pools if account else (),
                        capability=capabilities,
                        tier_floor=floor_tier.value if floor_tier else None,
                    )
                )
        except Exception:
            log.debug("decide: malformed catalog card %s skipped", model, exc_info=True)
    return tuple(offerings) + _account_offerings(accts, catalog)


def _health_from_store(store: Store) -> dict[tuple[str, str], tuple[int, int]]:
    """``(model, transport) -> (calls, ok)`` over :data:`HEALTH_WINDOW_MIN`
    minutes of ``llm_call_log`` — the offering-level failure signal the
    rank demotion reads (td568209; the router keeps no in-memory signal,
    and the window query rides the 15 s ledger entry so it never runs per
    call). ``budget``-class rows are excluded — a spend-cap pause is not
    an offering fault; gated rows count (a refused rung IS the observed
    fault). A failed read yields an empty mapping, never an exception."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT model, transport, count(*), "
                "count(*) FILTER (WHERE NOT errored) FROM llm_call_log "
                f"WHERE ts > now() - interval '{HEALTH_WINDOW_MIN} minutes' "
                "AND reason_class IS DISTINCT FROM 'budget' GROUP BY 1, 2"
            ).fetchall()
    except Exception:
        log.debug("decide: health read failed", exc_info=True)
        return {}
    return {
        (str(model), str(transport)): (int(calls), int(ok))
        for model, transport, calls, ok in rows
        if model is not None and transport is not None
    }


def _slots_from_store(store: Store) -> dict[str, tuple[int, int]]:
    """``model -> (capacity, free)`` summed over the ``llm:<model>``
    ``resource_slots`` rows — the serving pool's live ceiling and
    headroom the rank demotion reads (gr576274). Capacity and free are
    fleet-wide sums across serving hosts, matching what
    :func:`~precis.utils.llm.local_serving.acquire` can reach (the local
    row plus LAN-routable remote candidates). Rides the 15 s ledger
    entry, never per call; a failed read yields an empty map — no
    ceiling is read, the pre-fix behaviour — never an exception."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT resource, sum(capacity), sum(free) FROM resource_slots "
                "WHERE resource LIKE 'llm:%' GROUP BY 1"
            ).fetchall()
    except Exception:
        log.debug("decide: slot read failed", exc_info=True)
        return {}
    return {
        str(resource)[4:]: (int(capacity), int(free))
        for resource, capacity, free in rows
        if resource is not None and capacity is not None
    }


def _rates_from_store(store: Store) -> dict[str, float]:
    """``task_kind -> offered concurrency`` (slot-equivalents) over
    :data:`RATE_WINDOW_MIN` minutes of ``llm_call_log``: arrivals × mean
    *served* duration / window — Little's law, the demand a local pool
    must absorb for that kind (gr576274). A gated row counts as an
    arrival but not toward the service-time mean — its duration is slot
    wait, not serving. A kind whose rows all lack a measured duration
    proves no holding time and gets no row (the live ``free`` signal
    still covers a pool it fills). A failed read yields an empty map,
    never an exception."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT features->>'task_kind', count(*), avg(duration_ms) "
                "FILTER (WHERE features->>'gated' IS DISTINCT FROM 'true') "
                "FROM llm_call_log "
                f"WHERE ts > now() - interval '{RATE_WINDOW_MIN} minutes' "
                "AND features->>'task_kind' IS NOT NULL GROUP BY 1"
            ).fetchall()
    except Exception:
        log.debug("decide: rate read failed", exc_info=True)
        return {}
    window_ms = RATE_WINDOW_MIN * 60_000
    return {
        str(kind): int(calls) * float(mean_ms) / window_ms
        for kind, calls, mean_ms in rows
        if kind is not None and mean_ms is not None
    }


def ledger_from_store(
    store: Store, *, now: datetime, unavailable_transports: frozenset[str] = frozenset()
) -> Ledger | None:
    """Load catalog operating points and a 15-second account/pool snapshot."""
    global _loader_error_at
    try:
        stamp = time.monotonic()
        cached = _ledger_cache.get(id(store))
        if cached is None or stamp - cached[0] >= 15:
            accts = accounts.list_accounts(store)
            pools = accounts.list_pools(store)
            cached = (
                stamp,
                accts,
                pools,
                _build_offerings(store, accts),
                _health_from_store(store),
                _slots_from_store(store),
                _rates_from_store(store),
            )
            _ledger_cache[id(store)] = cached
        account_map = {a.name: a for a in cached[1]}
        pool_map = {p.provider: accounts.pacing(p, now) for p in cached[2]}
        return Ledger(
            cached[3],
            account_map,
            pool_map,
            unavailable_transports,
            now,
            health=cached[4],
            slots=cached[5],
            rates=cached[6],
        )
    except Exception:
        stamp = time.monotonic()
        if stamp - _loader_error_at >= 60:
            _loader_error_at = stamp
            log.debug("decide: ledger unavailable", exc_info=True)
        return None
