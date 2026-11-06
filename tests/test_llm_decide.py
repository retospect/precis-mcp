"""Shadow decisions keep filtering evidence without executing any provider."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.utils.llm import decide as d
from precis.utils.llm.accounts import Account, Pacing
from precis.utils.llm.policy import Requirement
from precis.utils.llm.router import LlmRequest, Rung, Tier, Transport, resolve_model

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def offering(key: str = "m@api/none", **kwargs: Any) -> d.Offering:
    return d.Offering(
        key,
        kwargs.pop("model", "m"),
        adapter=kwargs.pop("adapter", "api"),
        **kwargs,
    )


def ledger(*items: d.Offering, **kwargs: Any) -> d.Ledger:
    return d.Ledger(
        tuple(items),
        kwargs.pop("accounts", {}),
        kwargs.pop("pools", {}),
        kwargs.pop("unavailable_transports", frozenset()),
        NOW,
        **kwargs,
    )


def account(**kwargs: Any) -> Account:
    return Account(
        "sub",
        "vendor",
        "subscription",
        "api",
        None,
        None,
        1,
        (),
        (),
        None,
        kwargs.pop("enabled", True),
        kwargs.pop("entitlement", {}),
        {},
        None,
        ("p",),
        NOW,
    )


def result(
    o: d.Offering,
    *,
    req: LlmRequest | None = None,
    requirement: Requirement | None = None,
    **kwargs: Any,
) -> d.Decision:
    return d.decide(
        req or LlmRequest(Tier.SMALL),
        requirement or Requirement(Tier.SMALL),
        ledger(o, **kwargs),
        "shadow",
    )


@pytest.mark.parametrize(
    ("changes", "request_values", "requirement", "filter_name"),
    [
        ({"enabled": False}, {}, {}, "entitlement"),
        ({"billing": "subscription"}, {}, {}, "entitlement"),
        ({"billing": "local", "live": False}, {}, {}, "entitlement"),
        ({"locality": "cloud"}, {"privacy_class": "local-only"}, {}, "privacy"),
        ({"locality": "unknown"}, {"privacy_class": "local-only"}, {}, "privacy"),
        ({"window": 10}, {}, {"max_input": 100}, "capability"),
        ({"tools": False}, {}, {"needs_tools": True}, "capability"),
        ({"structured": False}, {}, {"needs_structured": True}, "capability"),
        (
            {"capability": {"code": 1}},
            {},
            {"axis": "code", "min_ordinal": 4},
            "capability",
        ),
        (
            {"capability": {"code": 2}},
            {"consequence": "medium"},
            {"axis": "code", "min_ordinal": 3},
            "capability",
        ),
    ],
)
def test_filters(
    changes: dict[str, Any],
    request_values: dict[str, Any],
    requirement: dict[str, Any],
    filter_name: str,
) -> None:
    decision = result(
        offering(**changes),
        req=LlmRequest(Tier.SMALL, **request_values),
        requirement=Requirement(Tier.SMALL, **requirement),
    )
    assert decision.chosen is None
    assert decision.rejected[0].filter == filter_name
    assert result(offering()).chosen is not None


def test_accounts_and_unknown_capability() -> None:
    o = offering(account="sub", billing="subscription")
    assert (
        result(o, accounts={"sub": account(enabled=False)}).rejected[0].filter
        == "entitlement"
    )
    assert (
        result(
            o,
            accounts={
                "sub": account(entitlement={"models": {"m": {"allowed": False}}})
            },
        )
        .rejected[0]
        .filter
        == "entitlement"
    )
    assert result(o, accounts={"sub": account()}).chosen == o
    assert (
        result(
            offering(),
            requirement=Requirement(
                Tier.SMALL, max_input=100, needs_tools=True, needs_structured=True
            ),
        ).chosen
        is not None
    )
    assert (
        result(
            offering(billing="local", live=True, locality="local"),
            req=LlmRequest(Tier.SMALL, privacy_class="local-only"),
        ).chosen
        is not None
    )


def test_axis_marginal_fallback() -> None:
    marginal = offering("marginal", capability={"code": 2})
    adequate = offering("adequate", capability={"code": 3})
    req = LlmRequest(Tier.SMALL, consequence="low")
    requirement = Requirement(Tier.SMALL, axis="code", min_ordinal=3)
    assert result(marginal, req=req, requirement=requirement).chosen == marginal
    decision = d.decide(req, requirement, ledger(marginal, adequate), "shadow")
    assert decision.chosen == adequate
    assert decision.rejected[0].filter == "capability"


def test_band_precedes_capacity_and_price() -> None:
    items = [
        offering("strong", price_in=100, capability={"code": 5}),
        offering("adequate", price_in=1, capability={"code": 3}),
        offering("unknown", price_in=0),
    ]
    decision = d.decide(
        LlmRequest(Tier.SMALL),
        Requirement(Tier.SMALL, axis="code", min_ordinal=3),
        ledger(*items),
        "shadow",
    )
    assert [r.band for r in decision.ranked] == ["strong", "adequate", "unknown"]


@pytest.mark.parametrize(
    "state,work_class,admitted",
    [
        ("exhausted", "interactive", False),
        ("ahead", "background", False),
        ("ahead", "deliverable", True),
        ("unknown", "background", True),
        ("behind", "background", True),
    ],
)
def test_availability(state: str, work_class: str, admitted: bool) -> None:
    decision = result(
        offering(pool_keys=("p",)),
        req=LlmRequest(Tier.SMALL, work_class=work_class),
        pools={"p": Pacing(state, 0.5, 0.95, None, None)},
    )
    assert (decision.chosen is not None) == admitted
    if not admitted:
        assert decision.rejected[0].filter == "availability"
    assert (
        result(offering(), unavailable_transports=frozenset({"api"})).rejected[0].filter
        == "availability"
    )


def test_capacity_ranking_and_deficit() -> None:
    sub = account()
    items = [
        offering("dollars", price_in=1),
        offering("unknown"),
        offering("expensive", price_in=2),
        offering("ahead", account="sub", billing="subscription", pool_keys=("ahead",)),
        offering(
            "behind", account="sub", billing="subscription", pool_keys=("behind",)
        ),
        offering("local", billing="local", live=True),
        offering("free", billing="free", pool_keys=("behind",)),
        offering("freer", billing="free", pool_keys=("freer",)),
    ]
    pools = {
        key: Pacing(state, fraction, 0.95, None, None)
        for key, state, fraction in [
            ("ahead", "ahead", 0.96),
            ("behind", "behind", 0.5),
            ("freer", "behind", 0.1),
        ]
    }
    decision = d.decide(
        LlmRequest(Tier.SMALL, work_class="interactive"),
        Requirement(Tier.SMALL),
        ledger(*items, accounts={"sub": sub}, pools=pools),
        "shadow",
    )
    assert [r.offering.key for r in decision.ranked] == [
        "freer",
        "free",
        "local",
        "behind",
        "ahead",
        "dollars",
        "expensive",
        "unknown",
    ]


def test_pin_retains_free_ranking_and_empty_fallback() -> None:
    bad = offering("bad", model="pinned", enabled=False)
    good = offering("good", model="good")
    decision = d.decide(
        LlmRequest(Tier.SMALL, model="pinned"),
        Requirement(Tier.SMALL),
        ledger(bad, good),
        "shadow",
    )
    assert decision.pinned and decision.chosen == bad
    assert decision.ranked[0].offering == good
    synthetic = result(good, req=LlmRequest(Tier.SMALL, model="absent"))
    assert synthetic.chosen is not None and synthetic.chosen.key == "absent@pinned/none"
    empty = d.decide(
        d.SliceRequest("code.review"), Requirement(Tier.SMALL), ledger(), "shadow"
    )
    assert empty.chosen is None and empty.fallback_model == resolve_model(Tier.SMALL)


def test_explanation_bounded_and_json_safe() -> None:
    items = [offering(str(i), enabled=i < 8) for i in range(25)]
    decision = d.decide(
        LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), ledger(*items), "shadow"
    )
    explanation = decision.explain()
    assert len(json.dumps(explanation, allow_nan=False)) < 3500
    assert len(explanation["ranked"]) == 5
    assert len(explanation["rejected"]) == 10
    assert explanation["rejected_counts"] == {"entitlement": 17}
    assert set(explanation["ranked"][0]) == {
        "key",
        "model",
        "adapter",
        "account",
        "billing",
        "price_in",
        "price_out",
        "band",
        "capacity_class",
        "pool_state",
        "reason",
    }


@pytest.mark.parametrize(
    "transport,bare,profile,account_name",
    [
        (Transport.CLAUDE_P, False, None, "claude-sub"),
        (Transport.CLAUDE_AGENT, True, None, "anthropic-api"),
        (Transport.CODEX_EXEC, False, None, "codex"),
        (Transport.LOCAL, False, None, "local"),
        (Transport.OPENAI_COMPAT, False, "named", "named"),
        (Transport.OPENAI_TOOLS, False, None, "metered"),
    ],
)
def test_legacy_keys_and_compare(
    transport: Transport, bare: bool, profile: str | None, account_name: str
) -> None:
    rung = Rung(transport, model="m", bare=bare, profile=profile)
    legacy = d.legacy_decision([rung], "fallback", ledger())
    assert legacy.chosen is not None
    assert legacy.chosen.key == f"m@{transport.value}/{account_name}"
    assert d.compare(legacy, legacy)["agree"] is True
    assert d.compare(legacy, legacy)["claude_window"]["legacy"] == (
        account_name == "claude-sub"
    )
    assert d.compare(replace(legacy, chosen=None), legacy)["agree"] is False


def test_compare_agrees_within_adapter_family() -> None:
    def picked(model: str, adapter: str, account: str | None = "acct") -> d.Decision:
        o = d.Offering(
            f"{model}@{adapter}/acct", model, adapter=adapter, account=account
        )
        return d.Decision("x", o, False, (), (), {}, {}, None, Requirement(Tier.SMALL))

    same_model = picked("m", "claude_agent")
    legacy_lane = picked("m", "claude_p")
    assert d.compare(same_model, legacy_lane)["agree"] is True
    assert d.compare(picked("other", "claude_agent"), legacy_lane)["agree"] is False
    assert (
        d.compare(picked("m", "openai_tools"), picked("m", "openai_compat"))["agree"]
        is True
    )
    assert d.compare(picked("m", "codex_exec"), legacy_lane)["agree"] is False


def test_compare_disagrees_across_accounts_on_the_same_model_and_family() -> None:
    """Same model, same adapter family — but a different account (the
    legacy-sub pick vs. a metered pick the chooser made) must not agree:
    agreeing would run the wrong account's dispatch under ``executed:
    decision`` (td575104)."""

    def picked(account: str | None) -> d.Decision:
        o = d.Offering("m@claude_p/acct", "m", adapter="claude_p", account=account)
        return d.Decision("x", o, False, (), (), {}, {}, None, Requirement(Tier.SMALL))

    assert d.compare(picked("claude-sub"), picked("anthropic-api"))["agree"] is False
    assert d.compare(picked("claude-sub"), picked("claude-sub"))["agree"] is True


def test_mode_for_legacy_reads_nothing_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default legacy mode costs no DB read per route() (gr572666)."""
    from precis import settings
    from precis.utils.llm import live_config

    settings.bind_store(cast("Any", object()))
    monkeypatch.delenv("PRECIS_LLM_DECIDE", raising=False)
    counts = {"settings": 0, "live": 0}

    def db_read(*args: Any) -> None:
        counts["settings"] += 1
        return None

    def read_setting(*args: Any) -> None:
        counts["live"] += 1
        return None

    monkeypatch.setattr(settings, "_db_read", db_read)
    monkeypatch.setattr(live_config, "_read_setting", read_setting)
    live_config.bust_cache()
    try:
        assert [d.mode_for("code.review") for _ in range(5)] == ["legacy"] * 5
        assert counts["settings"] == 0
        assert counts["live"] <= 3
    finally:
        settings.bind_store(None)
        live_config.bust_cache()


def test_mode_for_rereads_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each mode key re-reads only once its 15 s cache window has passed."""
    from precis import settings
    from precis.utils.llm import live_config

    settings.bind_store(cast("Any", object()))
    monkeypatch.delenv("PRECIS_LLM_DECIDE", raising=False)
    reads: list[str] = []

    def read(key: str) -> None:
        reads.append(key)
        return None

    monkeypatch.setattr(live_config, "_read_setting", read)
    monkeypatch.setattr(settings, "_db_read", lambda *a: None)
    clock = [1000.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    live_config.bust_cache()
    try:
        assert d.mode_for("code.review") == "legacy"
        assert sorted(reads) == [
            "llm.decide.code",
            "llm.decide.kind.code.review",
            "llm.decide.mode",
        ]
        d.mode_for("code.review")
        assert len(reads) == 3
        clock[0] += 16
        d.mode_for("code.review")
        assert sorted(reads[3:]) == [
            "llm.decide.code",
            "llm.decide.kind.code.review",
            "llm.decide.mode",
        ]
    finally:
        settings.bind_store(None)
        live_config.bust_cache()


@pytest.fixture(autouse=True)
def _reset_mode_cache() -> Iterator[None]:
    d.reset_mode_cache()
    yield
    d.reset_mode_cache()


def test_mode_prefix_and_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.utils.llm import live_config

    seen: list[str] = []

    def override(prefix: str) -> str | None:
        seen.append(prefix)
        return "shadow" if prefix == "code" else None

    monkeypatch.setattr(live_config, "decide_override", override)
    monkeypatch.setattr(live_config, "decide_default", lambda: "on")
    assert d.mode_for("code.review") == "shadow"
    assert d.mode_for("code:review") == "shadow"
    assert d.mode_for(None) == "on"
    # The memo keys on the kind, so "code:review" re-reads the prefix row.
    assert seen == ["code", "code", "unknown"]
    monkeypatch.setattr(live_config, "decide_default", lambda: "bogus")
    assert d.mode_for("other") == "legacy"


def test_loader_real_catalog_and_cache(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import llm_catalog

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE kind = 'llm' AND retired_at IS NULL"
        )
    llm_catalog.upsert_card(
        store,
        model_id="shadow-test",
        text="test",
        offerings=[
            {"transport": "claude_p", "billing": "subscription", "max_input": 1000}
        ],
    )
    llm_catalog.upsert_card(
        store,
        model_id="shadow-free",
        text="test",
        endpoints=[
            {
                "provider": "sample",
                "quant": "fp8",
                "max_input": 2000,
                "price_in": 0,
                "price_out": 0,
            }
        ],
    )
    llm_catalog.upsert_card(
        store,
        model_id="shadow-local",
        text="test",
        offerings=[{"transport": "local", "max_input": 3000}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="shadow-disabled",
        text="test",
        offerings=[{"transport": "kimi_tree", "billing": "subscription"}],
    )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    clock = [10.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    reads: list[int] = []
    original = d.accounts.list_accounts

    def read_accounts(bound: Any) -> list[Account]:
        reads.append(1)
        return original(bound)

    monkeypatch.setattr(d.accounts, "list_accounts", read_accounts)
    first = d.ledger_from_store(store, now=NOW)
    assert first is not None
    o = next(o for o in first.offerings if o.model == "shadow-test")
    assert o.billing == "subscription" and o.account == "claude-sub"
    assert o.window == 1000 and o.pool_keys == (
        "llm:claude-sub:5h",
        "llm:claude-sub:7d",
    )
    free = next(o for o in first.offerings if o.model == "shadow-free")
    assert free.billing == "free" and free.variant == "sample/fp8"
    assert free.window == 2000 and free.account is None
    local = next(o for o in first.offerings if o.model == "shadow-local")
    assert local.billing == "local" and local.live is True and local.window == 3000
    disabled = next(o for o in first.offerings if o.model == "shadow-disabled")
    assert disabled.account == "kimi"
    decision = d.decide(
        LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), first, "shadow"
    )
    assert any(
        r.offering_key == disabled.key and r.filter == "entitlement"
        for r in decision.rejected
    )
    assert d.ledger_from_store(store, now=NOW) is not None
    assert len(reads) == 1
    clock[0] += 16
    assert d.ledger_from_store(store, now=NOW) is not None
    assert len(reads) == 2
    d.admit.reset_cache()
    d._ledger_cache.clear()


def test_loader_caches_offerings_and_heartbeats(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 15 s ledger entry carries built offerings; host heartbeats probe
    only cards with a local operating point, and only on a rebuild."""
    from precis import llm_catalog

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE kind = 'llm' AND retired_at IS NULL"
        )
    llm_catalog.upsert_card(
        store,
        model_id="cache-local",
        text="test",
        offerings=[{"transport": "local", "max_input": 3000}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="cache-cloud",
        text="test",
        offerings=[{"transport": "claude_p", "billing": "subscription"}],
    )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    clock = [10.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    catalogs: list[int] = []
    heartbeats: list[int] = []
    real_catalog = d.admit._catalog

    def catalog(bound: Any) -> Any:
        catalogs.append(1)
        return real_catalog(bound)

    real_hosts = d.policy._live_served_hosts

    def hosts(bound: Any, meta: Any) -> Any:
        heartbeats.append(1)
        return real_hosts(bound, meta)

    monkeypatch.setattr(d.admit, "_catalog", catalog)
    monkeypatch.setattr(d.policy, "_live_served_hosts", hosts)
    assert d.ledger_from_store(store, now=NOW) is not None
    assert d.ledger_from_store(store, now=NOW) is not None
    assert len(catalogs) == 1
    assert len(heartbeats) == 1
    clock[0] += 16
    assert d.ledger_from_store(store, now=NOW) is not None
    assert len(catalogs) == 2
    assert len(heartbeats) == 2
    d.admit.reset_cache()
    d._ledger_cache.clear()


def test_loader_infers_billing_from_adapter(store: Any) -> None:
    """A card with no ``billing`` key prices by transport: the subscription
    adapters bind their enabled account, openai_compat meters, 0/0 is free."""
    from precis import llm_catalog

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE kind = 'llm' AND retired_at IS NULL"
        )
    llm_catalog.upsert_card(
        store,
        model_id="infer-claude-p",
        text="test",
        offerings=[{"transport": "claude_p"}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="infer-claude-agent",
        text="test",
        offerings=[{"transport": "claude_agent"}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="infer-codex",
        text="test",
        offerings=[{"transport": "codex_exec"}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="infer-metered",
        text="test",
        offerings=[{"transport": "openai_compat", "price_in": 1, "price_out": 2}],
    )
    llm_catalog.upsert_card(
        store,
        model_id="infer-free",
        text="test",
        offerings=[{"transport": "openai_compat", "price_in": 0, "price_out": 0}],
    )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    ledger = d.ledger_from_store(store, now=NOW)
    assert ledger is not None
    by_model = {o.model: o for o in ledger.offerings}
    expected = {
        "infer-claude-p": ("subscription", "claude-sub"),
        "infer-claude-agent": ("subscription", "claude-sub"),
        "infer-codex": ("subscription", "codex"),
        "infer-metered": ("metered", None),
        "infer-free": ("free", None),
    }
    for model, want in expected.items():
        o = by_model[model]
        assert (o.billing, o.account) == want, model
    decision = d.decide(
        LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), ledger, "shadow"
    )
    entitled = {r.offering_key for r in decision.rejected if r.filter == "entitlement"}
    assert not [o for o in ledger.offerings if o.key in entitled]
    assert decision.chosen is not None
    d.admit.reset_cache()
    d._ledger_cache.clear()


def test_loader_tools_flag_falls_back_to_facts(store: Any) -> None:
    """A curated offering with no tools data reads the card's
    ``facts_openrouter.supported_parameters``; absent both, tools is None."""
    from precis import llm_catalog

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE kind = 'llm' AND retired_at IS NULL"
        )
    ref_id, _ = llm_catalog.upsert_card(
        store,
        model_id="tools-no",
        text="test",
        offerings=[{"transport": "claude_p", "billing": "subscription"}],
    )
    store.update_ref(
        ref_id,
        meta_patch={"facts_openrouter": {"supported_parameters": ["max_tokens"]}},
    )
    llm_catalog.upsert_card(
        store,
        model_id="tools-unknown",
        text="test",
        offerings=[{"transport": "claude_p", "billing": "subscription"}],
    )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    ledger = d.ledger_from_store(store, now=NOW)
    assert ledger is not None
    by_model = {o.model: o for o in ledger.offerings}
    assert by_model["tools-no"].tools is False
    assert by_model["tools-unknown"].tools is None
    decision = d.decide(
        LlmRequest(Tier.SMALL, tools_needed=True),
        Requirement(Tier.SMALL, needs_tools=True),
        ledger,
        "shadow",
    )
    rejected = {r.offering_key: r.filter for r in decision.rejected}
    assert rejected.get(by_model["tools-no"].key) == "capability"
    assert by_model["tools-unknown"].key not in rejected
    assert decision.chosen == by_model["tools-unknown"]
    d.admit.reset_cache()
    d._ledger_cache.clear()


def test_legacy_rung_resolves_priced_offering() -> None:
    """A legacy rung finds its priced catalog offering by (model, adapter):
    the catalog key carries account-or-none + a variant the rung key never
    has, so a key-equality lookup can never bind the price."""
    rung = Rung(Transport.OPENAI_COMPAT, model="z-ai/glm-4.7-flash")
    priced = offering(
        "z-ai/glm-4.7-flash@openai_compat/none/host",
        model="z-ai/glm-4.7-flash",
        adapter="openai_compat",
        variant="host",
        price_in=0.06,
    )
    legacy = d.legacy_decision([rung], "summarizer", ledger(priced))
    assert legacy.chosen is not None
    assert legacy.chosen.price_in == 0.06


def test_local_offering_prices_at_zero(store: Any) -> None:
    """A local operating point with no card price prices at 0 — the
    catalog-built offering and the synthesized legacy one alike."""
    from precis import llm_catalog

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE kind = 'llm' AND retired_at IS NULL"
        )
    llm_catalog.upsert_card(
        store,
        model_id="local-zero",
        text="test",
        offerings=[{"transport": "local", "max_input": 3000}],
    )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    built = d.ledger_from_store(store, now=NOW)
    assert built is not None
    local = next(o for o in built.offerings if o.model == "local-zero")
    assert local.billing == "local"
    assert local.price_in == 0.0 and local.price_out == 0.0
    d.admit.reset_cache()
    d._ledger_cache.clear()
    legacy = d.legacy_decision([Rung(Transport.LOCAL, model="m")], "m", ledger())
    assert legacy.chosen is not None
    assert legacy.chosen.billing == "local"
    assert legacy.chosen.price_in == 0.0 and legacy.chosen.price_out == 0.0


def test_tier_floor_rejects_smaller_offering() -> None:
    """A demand's tier floor rejects a lower-floor offering as capability."""
    small = offering("small-card", model="small-card", tier_floor="small")
    big = offering("big-card", model="big-card", tier_floor="big")
    decision = d.decide(
        LlmRequest(Tier.BIG), Requirement(Tier.BIG), ledger(small, big), "shadow"
    )
    assert decision.chosen == big
    rejected = {r.offering_key: (r.filter, r.reason) for r in decision.rejected}
    assert rejected["small-card"] == (
        "capability",
        "offering tier below demanded tier floor",
    )


def test_unset_tier_floor_rejected_for_big_demand() -> None:
    """A card carrying no floor value cannot serve a demand above SMALL —
    it can't prove the capability the floor names, so ``on`` would rank
    nothing and fall back (``no-offering``) until the card carries one."""
    no_floor = offering("no-floor", model="no-floor")
    floored = offering("big-card", model="big-card", tier_floor="big")
    decision = d.decide(
        LlmRequest(Tier.BIG),
        Requirement(Tier.BIG),
        ledger(no_floor, floored),
        "shadow",
    )
    assert decision.chosen == floored
    rejected = {r.offering_key: (r.filter, r.reason) for r in decision.rejected}
    assert rejected["no-floor"] == ("capability", "tier floor unset")


def test_unset_tier_floor_passes_small_demand() -> None:
    """SMALL is the floor's own rank — an unset floor still serves it."""
    no_floor = offering("no-floor", model="no-floor")
    decision = d.decide(
        LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), ledger(no_floor), "shadow"
    )
    assert decision.chosen == no_floor


def test_exact_kind_key_beats_prefix_beats_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm.decide.kind.<task_kind>`` outranks the prefix row, which
    outranks ``llm.decide.mode``; an invalid value fails safe to legacy."""
    from precis.utils.llm import live_config

    monkeypatch.setattr(
        live_config,
        "decide_kind_override",
        lambda kind: "on" if kind == "code.review" else None,
    )
    monkeypatch.setattr(
        live_config,
        "decide_override",
        lambda prefix: "shadow" if prefix == "code" else None,
    )
    monkeypatch.setattr(live_config, "decide_default", lambda: "legacy")
    assert d.mode_for("code.review") == "on"
    assert d.mode_for("code.fix") == "shadow"
    assert d.mode_for("other.task") == "legacy"
    monkeypatch.setattr(live_config, "decide_kind_override", lambda kind: "bogus")
    d.reset_mode_cache()
    assert d.mode_for("code.review") == "legacy"


def test_exact_kind_key_does_not_leak_to_sibling_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-kind row binds that kind only — siblings read the lower
    layers as before."""
    from precis.utils.llm import live_config

    monkeypatch.setattr(
        live_config,
        "decide_kind_override",
        lambda kind: "on" if kind == "code.review" else None,
    )
    monkeypatch.setattr(live_config, "decide_override", lambda prefix: None)
    monkeypatch.setattr(live_config, "decide_default", lambda: None)
    monkeypatch.delenv("PRECIS_LLM_DECIDE", raising=False)
    assert d.mode_for("code.review") == "on"
    assert d.mode_for("code.review.deep") == "legacy"
    assert d.mode_for("code.other") == "legacy"
    assert d.mode_for("code") == "legacy"


def test_all_failing_offering_is_ranked_last() -> None:
    """≥20 calls and zero successes in the health window demotes an
    offering behind every healthy one — but never rejects it."""
    bad = offering("bad", model="bad", price_in=1)
    healthy = offering("healthy", model="healthy", price_in=100)
    led = ledger(bad, healthy, health={("bad", "api"): (20, 0)})
    decision = d.decide(LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), led, "on")
    assert [r.offering.key for r in decision.ranked] == ["healthy", "bad"]
    assert "unhealthy: 0 of 20 calls ok in 30 min" in decision.ranked[1].reason
    # The only survivor stays eligible — the apply fallback covers a failure.
    solo = d.decide(
        LlmRequest(Tier.SMALL),
        Requirement(Tier.SMALL),
        ledger(bad, health={("bad", "api"): (20, 0)}),
        "on",
    )
    assert solo.chosen == bad


def test_offering_with_a_success_or_few_calls_is_not_demoted() -> None:
    """One success, or fewer than the window's minimum calls, ranks normally."""
    cheap = offering("cheap", model="m", price_in=1)
    pricey = offering("pricey", model="n", price_in=100)
    for health in ({("m", "api"): (20, 1)}, {("m", "api"): (19, 0)}, {}):
        decision = d.decide(
            LlmRequest(Tier.SMALL),
            Requirement(Tier.SMALL),
            ledger(cheap, pricey, health=health),
            "on",
        )
        assert decision.chosen == cheap


def test_loader_health_from_call_log(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ledger_from_store`` fills ``health`` from ``llm_call_log`` inside
    the 15 s cache entry — ``budget`` rows are excluded, gated ones kept,
    and a second call inside the window does not re-query."""
    with store.pool.connection() as conn:
        for _ in range(20):
            conn.execute(
                "INSERT INTO llm_call_log (source, tier, transport, model, "
                "errored, reason_class) "
                "VALUES ('h','small','openai_compat','sick-model',true,'rate')"
            )
        conn.execute(
            "INSERT INTO llm_call_log (source, tier, transport, model, "
            "errored, reason_class) "
            "VALUES ('h','small','openai_compat','sick-model',true,'budget')"
        )
        conn.execute(
            "INSERT INTO llm_call_log (source, tier, transport, model, "
            "errored, reason_class) "
            "VALUES ('h','big','local','well-model',false,null)"
        )
    d.admit.reset_cache()
    d._ledger_cache.clear()
    clock = [10.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    reads: list[int] = []
    real = d._health_from_store

    def spy(bound: Any) -> Any:
        reads.append(1)
        return real(bound)

    monkeypatch.setattr(d, "_health_from_store", spy)
    led = d.ledger_from_store(store, now=NOW)
    assert led is not None
    # The 'budget' row never counted — a spend-cap pause is no offering fault.
    assert led.health[("sick-model", "openai_compat")] == (20, 0)
    assert led.health[("well-model", "local")] == (1, 1)
    d.ledger_from_store(store, now=NOW)
    assert len(reads) == 1
    clock[0] += 16
    assert d.ledger_from_store(store, now=NOW) is not None
    assert len(reads) == 2
    d.admit.reset_cache()
    d._ledger_cache.clear()


def test_ties_prefer_legacy_lane() -> None:
    """Equal band/capacity/price picks prefer the model the legacy lane ran."""
    earlier = offering(
        "aaa@local/local",
        model="aaa",
        adapter="local",
        billing="local",
        locality="local",
        live=True,
        price_in=0.0,
        price_out=0.0,
    )
    later = offering(
        "zzz@local/local",
        model="zzz",
        adapter="local",
        billing="local",
        locality="local",
        live=True,
        price_in=0.0,
        price_out=0.0,
    )
    led = ledger(earlier, later, legacy_adapter="local", legacy_model="zzz")
    decision = d.decide(LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), led, "shadow")
    assert decision.chosen is not None and decision.chosen.model == "zzz"
    legacy = d.legacy_decision([Rung(Transport.LOCAL, model="zzz")], "zzz", led)
    assert d.compare(decision, legacy)["agree"] is True


def _hosted_payload(**overrides: Any) -> dict[str, Any]:
    """A door-complete hosted-provider account payload (groq by default)."""
    payload: dict[str, Any] = {
        "name": "groq-free",
        "vendor": "groq",
        "plan": "free",
        "adapter": "openai_compat",
        "endpoint": {"base_url": "https://api.groq.com/openai/v1"},
        "api_key_ref": "GROQ_API_KEY",
        "models": ["m-groq"],
        "privacy": {
            "training_use": "no",
            "retention": "zdr",
            "source_url": "https://groq.com/privacy/",
            "verified_at": "2026-11-01T00:00:00Z",
        },
        "entitlement": {
            "models": {
                "m-groq": {
                    "allowed": True,
                    "price_in": 0,
                    "price_out": 0,
                    "source_url": "https://groq.com/pricing/",
                    "verified_at": "2026-11-01T00:00:00Z",
                }
            }
        },
        "limits": [
            {
                "pool": "llm:groq-free:rpm",
                "unit": "calls",
                "limit": 30,
                "reset_rule": "rolling:60",
                "source_url": "https://groq.com/docs/rate-limits/",
                "verified_at": "2026-11-01T00:00:00Z",
            }
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def _hosted_account(store: Any) -> Iterator[None]:
    """Drop the door-created rows and restore the profiles map after."""
    with store.pool.connection() as conn:
        saved = conn.execute(
            "SELECT value, updated_at, updated_by FROM app_settings "
            "WHERE key = 'llm.profiles'"
        ).fetchone()
    yield
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM llm_account WHERE name = 'groq-free'")
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider LIKE 'llm:groq-free:%%'"
        )
        conn.execute("DELETE FROM app_settings WHERE key = 'llm.profiles'")
        if saved is not None:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at, updated_by) "
                "VALUES ('llm.profiles', %s, %s, %s)",
                (saved[0], saved[1], saved[2]),
            )


def _put_account(store: Any, payload: dict[str, Any]) -> None:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    LlmHandler(hub=Hub(store=store)).put(args={"account": payload})


def _hosted_ledger(store: Any) -> d.Ledger:
    d.admit.reset_cache()
    d._ledger_cache.clear()
    led = d.ledger_from_store(store, now=NOW)
    d.admit.reset_cache()
    d._ledger_cache.clear()
    assert led is not None
    return led


def test_account_offerings_empty_for_seed_accounts(store: Any) -> None:
    """The five seed accounts alone add no account offering."""
    d.admit.reset_cache()
    accts = d.accounts.list_accounts(store)
    assert d._account_offerings(accts, d.admit._catalog(store)) == ()


@pytest.mark.usefixtures("_hosted_account")
def test_disabled_account_yields_no_offering(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A door-created account starts disabled: no offering, nothing ranked."""
    monkeypatch.setenv("PRECIS_SOURCE", "cli")
    _put_account(store, _hosted_payload())
    led = _hosted_ledger(store)
    key = d.offering_key("m-groq", "openai_compat", "groq-free")
    assert all(o.key != key for o in led.offerings)
    decision = d.decide(LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), led, "shadow")
    assert all(r.offering.account != "groq-free" for r in decision.ranked)


@pytest.mark.usefixtures("_hosted_account")
def test_enabled_free_account_offerings_priced_zero(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One offering per account model — free-priced, cloud, on the
    account's pools."""
    monkeypatch.setenv("PRECIS_SOURCE", "cli")
    _put_account(store, _hosted_payload())
    _put_account(store, {"name": "groq-free", "enabled": True})
    led = _hosted_ledger(store)
    key = d.offering_key("m-groq", "openai_compat", "groq-free")
    acct = led.accounts["groq-free"]
    mine = [o for o in led.offerings if o.account == "groq-free"]
    assert len(mine) == len(acct.models) == 1
    o = mine[0]
    assert o.key == key
    assert o.billing == "free"
    assert o.price_in == 0 and o.price_out == 0
    assert o.pool_keys == acct.pools
    assert o.locality == "cloud"
    decision = d.decide(LlmRequest(Tier.SMALL), Requirement(Tier.SMALL), led, "shadow")
    assert any(r.offering.key == key for r in decision.ranked)


@pytest.mark.usefixtures("_hosted_account")
def test_hosted_free_offering_rejected_for_local_only(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloud account offering never serves a local-only demand."""
    monkeypatch.setenv("PRECIS_SOURCE", "cli")
    _put_account(store, _hosted_payload())
    _put_account(store, {"name": "groq-free", "enabled": True})
    led = _hosted_ledger(store)
    key = d.offering_key("m-groq", "openai_compat", "groq-free")
    decision = d.decide(
        LlmRequest(Tier.SMALL, privacy_class="local-only"),
        Requirement(Tier.SMALL),
        led,
        "shadow",
    )
    rejected = {r.offering_key: r.filter for r in decision.rejected}
    assert rejected.get(key) == "privacy"


def test_unlisted_price_is_unpriced_never_free() -> None:
    """No entitlement entry and no card feed price: metered, prices
    None. A card feed price of 0/0 without an entitlement entry: free —
    a missing price is never free."""
    acct = replace(
        account(),
        name="groq-free",
        vendor="groq",
        adapter="openai_compat",
        profile="groq-free",
        models=("m-unlisted",),
        pools=("llm:groq-free:rpm",),
    )
    (unpriced,) = d._account_offerings([acct], {})
    assert unpriced.billing == "metered"
    assert unpriced.price_in is None and unpriced.price_out is None
    catalog = {"m-unlisted": {"facts_openrouter": {"price_in": 0, "price_out": 0}}}
    (free,) = d._account_offerings([acct], catalog)
    assert free.billing == "free"
    assert free.price_in == 0 and free.price_out == 0


def test_local_only_rejects_training_account() -> None:
    """A local-only demand refuses an account that may train on prompts —
    or that records no stance. ``training_use='no'`` passes; the default
    privacy class never reads the stance."""

    def run(privacy: dict[str, Any], privacy_class: str) -> d.Decision:
        acct = replace(account(), name="train", privacy=privacy)
        o = offering(
            "m@local/train",
            model="m",
            adapter="local",
            billing="local",
            locality="local",
            live=True,
            account="train",
        )
        return d.decide(
            LlmRequest(Tier.SMALL, privacy_class=privacy_class),
            Requirement(Tier.SMALL),
            ledger(o, accounts={"train": acct}),
            "shadow",
        )

    for stance in ({"training_use": "yes"}, {}):
        decision = run(stance, "local-only")
        assert decision.chosen is None
        assert decision.rejected[0].filter == "privacy"
    assert run({"training_use": "no"}, "local-only").chosen is not None
    assert run({"training_use": "yes"}, "default").chosen is not None
