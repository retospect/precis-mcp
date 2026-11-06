"""Deterministic, read-only offering decisions for shadow routing.

Only ``ledger_from_store`` performs I/O. Decisions never reserve capacity or
change the legacy dispatch chain; their explanation is call-log telemetry.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.budget.work_class import resolve as resolve_work_class
from precis.utils.llm import accounts, admit, policy, recipes
from precis.utils.llm.accounts import Pacing
from precis.utils.llm.policy import Requirement
from precis.utils.llm.router import LlmRequest, Rung, Tier, Transport, resolve_model

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
        result["ranked"] = [asdict(r) for r in self.ranked[:5]]
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
        ranked.append(
            Ranked(o, band, o.capacity_class, state, f"{band}; {o.billing}; {state}")
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

    def rank(r: Ranked) -> tuple[int, int, float, float, str]:
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
        return (
            {"strong": 0, "adequate": 1, "unknown": 2, "marginal": 3}[r.band],
            capacity,
            -deficit if capacity in {0, 2} else 0,
            o.price_in if o.price_in is not None else math.inf,
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
        fallback_model=resolve_model(requirement.tier_floor)
        if chosen is None
        else None,
        reason="pinned"
        if pin
        else "ranked"
        if chosen
        else "no offering survives; using tier floor",
    )


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
    return f"{rung.model or model}@{rung.transport.value}/{account}"


def legacy_decision(ladder: Sequence[Rung], model: str, ledger: Ledger) -> Decision:
    from precis.utils.llm.router import _rung_is_cloud

    ranked: list[Ranked] = []
    for rung in ladder:
        key = rung_key(rung, model)
        o = next((o for o in ledger.offerings if o.key == key), None)
        if o is None:
            account = key.rsplit("/", 1)[1]
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
                rung.model or model,
                account=account,
                adapter=rung.transport.value,
                billing="local"
                if local
                else "subscription"
                if account in {"claude-sub", "codex"}
                else "metered",
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


def compare(shadow: Decision, legacy: Decision) -> dict[str, Any]:
    def claude(d: Decision) -> bool:
        return bool(
            d.chosen
            and any(k.startswith("llm:claude-sub:") for k in d.chosen.pool_keys)
        )

    return {
        "agree": bool(
            shadow.chosen
            and legacy.chosen
            and (shadow.chosen.model, shadow.chosen.adapter)
            == (legacy.chosen.model, legacy.chosen.adapter)
        ),
        "claude_window": {"legacy": claude(legacy), "shadow": claude(shadow)},
    }


_bad_modes: set[str] = set()


def mode_for(task_kind: str | None) -> str:
    from precis import settings
    from precis.utils.llm import live_config

    prefix = re.split(r"[.:]", task_kind or "unknown", maxsplit=1)[0]
    override = live_config.decide_override(prefix)
    mode = (
        override
        if override is not None
        else settings.get_str("llm.decide.mode", default="legacy")
    )
    if mode is None:
        mode = "legacy"
    if mode not in {"legacy", "shadow", "on"}:
        if mode not in _bad_modes:
            _bad_modes.add(mode)
            log.warning("invalid llm.decide mode %r; using legacy", mode)
        return "legacy"
    return mode


_ledger_cache: dict[int, tuple[float, list[accounts.Account], list[accounts.Pool]]] = {}
_loader_error_at = -math.inf


def ledger_from_store(
    store: Store, *, now: datetime, unavailable_transports: frozenset[str] = frozenset()
) -> Ledger | None:
    """Load catalog operating points and a 15-second account/pool snapshot."""
    global _loader_error_at
    try:
        stamp = time.monotonic()
        cached = _ledger_cache.get(id(store))
        if cached is None or stamp - cached[0] >= 15:
            cached = (stamp, accounts.list_accounts(store), accounts.list_pools(store))
            _ledger_cache[id(store)] = cached
        account_map = {a.name: a for a in cached[1]}
        pool_map = {p.provider: accounts.pacing(p, now) for p in cached[2]}
        offerings: list[Offering] = []
        for model, meta in admit._catalog(store).items():
            try:
                if not policy._runnable(meta):
                    continue
                live_hosts = policy._live_served_hosts(store, meta)
                curated = meta.get("offerings") or []
                endpoints = meta.get("endpoints") or []
                entries = curated or endpoints or [meta.get("facts_openrouter") or {}]
                for entry in entries:
                    adapter = str(entry.get("transport") or "openai_compat")
                    local = policy._is_local_offering(entry)
                    billing = (
                        "local" if local else str(entry.get("billing") or "metered")
                    )
                    account = None
                    if billing == "subscription":
                        account = (
                            next(
                                (
                                    a
                                    for a in cached[1]
                                    if a.enabled and a.adapter == adapter
                                ),
                                None,
                            )
                            or next((a for a in cached[1] if model in a.models), None)
                            or next(
                                (a for a in cached[1] if a.adapter == adapter), None
                            )
                        )
                    elif not local:
                        account = account_map.get("metered")
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
                    if billing == "metered" and price_in == 0 and price_out == 0:
                        billing = "free"
                    variant = (
                        str(entry.get("effort") or "")
                        or (
                            "/".join(
                                str(entry[k])
                                for k in ("provider", "quant")
                                if entry.get(k)
                            )
                            if not curated
                            else ""
                        )
                        or None
                    )
                    account_name = (
                        account.name if account else "local" if local else None
                    )
                    key = f"{model}@{adapter}/{account_name or 'none'}" + (
                        f"/{variant}" if variant else ""
                    )
                    params = entry.get("supported_parameters")
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
                            entry.get("tools") if params is None else "tools" in params,
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
                        )
                    )
            except Exception:
                log.debug(
                    "decide: malformed catalog card %s skipped", model, exc_info=True
                )
        return Ledger(
            tuple(offerings), account_map, pool_map, unavailable_transports, now
        )
    except Exception:
        stamp = time.monotonic()
        if stamp - _loader_error_at >= 60:
            _loader_error_at = stamp
            log.debug("decide: ledger unavailable", exc_info=True)
        return None
