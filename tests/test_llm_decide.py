"""Shadow decisions keep filtering evidence without executing any provider."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.utils.llm import decide as d
from precis.utils.llm.accounts import Account, Pacing
from precis.utils.llm.policy import Requirement
from precis.utils.llm.router import LlmRequest, Rung, Tier, Transport, resolve_model

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def offering(key: str = "m@api/none", **kwargs: Any) -> d.Offering:
    return d.Offering(key, kwargs.pop("model", "m"), adapter="api", **kwargs)


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
    json.dumps(explanation, allow_nan=False)
    assert len(explanation["ranked"]) == 5
    assert len(explanation["rejected"]) == 10
    assert explanation["rejected_counts"] == {"entitlement": 17}


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


def test_mode_prefix_and_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis import settings
    from precis.utils.llm import live_config

    seen: list[str] = []

    def override(prefix: str) -> str | None:
        seen.append(prefix)
        return "shadow" if prefix == "code" else None

    monkeypatch.setattr(live_config, "decide_override", override)
    monkeypatch.setattr(settings, "get_str", lambda *a, **k: "on")
    assert d.mode_for("code.review") == "shadow"
    assert d.mode_for("code:review") == "shadow"
    assert d.mode_for(None) == "on"
    assert seen == ["code", "code", "unknown"]
    monkeypatch.setattr(settings, "get_str", lambda *a, **k: "bogus")
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
    assert free.window == 2000 and free.account == "metered"
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
