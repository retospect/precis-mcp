"""S1 local-first routing contracts, with no providers or live database."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis import route_log, settings
from precis.utils.llm import live_config, local_first, local_serving, operations, router
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, Transport

_OP = "gripe_triage:tick"
_RECORDED_SPEND = local_first._spent_usd
_SET_CASES = (
    ("triage", "gripe_triage:tick"),
    ("tables", "survey_extract:table_recipe"),
    ("claims", "dtree:claim_check"),
)
_PARK_OPS = ("survey_extract:table_recipe", "dtree:claim_check")


def _answer(**changes: Any) -> LlmResult:
    return replace(
        LlmResult(
            text='{"verdict": "noise"}',
            cost_usd=0.0,
            turns_used=1,
            model="local-model",
            tier=Tier.BIG,
            placement="local",
        ),
        **changes,
    )


def _request() -> LlmRequest:
    return LlmRequest(
        tier=Tier.BIG,
        source=_OP,
        prompt="screen this gripe",
        model="caller-model",
        max_tokens=1024,
        timeout_s=90,
        messages=[{"role": "user", "content": "preserve this message"}],
        features={"caller": {"trace": "kept"}},
    )


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_first, "_state", local_first._Circuit())
    monkeypatch.setattr(
        local_first,
        "_slots",
        {
            op: threading.BoundedSemaphore(config.cap)
            for op, config in operations.LOCAL_FIRST_OPS.items()
        },
    )
    monkeypatch.setattr(local_first, "_spent_usd", lambda op: 0.0)
    monkeypatch.setattr(settings, "get_bool", lambda *args, **kwargs: True)
    monkeypatch.setattr(settings, "get_str", lambda *args, **kwargs: "on")
    monkeypatch.setattr(settings, "get_float", lambda *args, **kwargs: 3.0)


def _stub_route(
    monkeypatch: pytest.MonkeyPatch, *answers: LlmResult
) -> list[LlmRequest]:
    calls: list[LlmRequest] = []
    pending = iter(answers)

    def fake_route(req: LlmRequest) -> LlmResult:
        calls.append(req)
        return next(pending)

    monkeypatch.setattr(router, "route", fake_route)
    return calls


@pytest.mark.parametrize("mode", ["off", "shadow", "invalid"])
def test_disabled_set_mode_preserves_serialized_request(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Shadow without a comparator preserves the original paid path."""
    monkeypatch.setattr(settings, "get_str", lambda *args, **kwargs: mode)
    req = _request()
    before = router._serialize_request(req)
    paid = _answer(text="paid", placement="cloud")
    calls = _stub_route(monkeypatch, paid)

    def unexpected_accept(result: LlmResult) -> None:
        pytest.fail("disabled mode must not invoke local acceptance")

    out = local_first.run(req, op=_OP, accept=unexpected_accept)

    assert out.result is paid
    assert out.local is None
    assert calls == [req]
    assert calls[0] is req
    assert router._serialize_request(calls[0]) == before


@pytest.mark.parametrize(("set_name", "op"), _SET_CASES)
def test_master_false_overrides_enabled_set(
    monkeypatch: pytest.MonkeyPatch, set_name: str, op: str
) -> None:
    monkeypatch.setattr(settings, "get_bool", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        settings,
        "get_str",
        lambda key: "on" if key == f"llm.local_first.set.{set_name}.mode" else "off",
    )
    req = replace(_request(), source=op)
    before = router._serialize_request(req)
    paid = _answer(placement="cloud")
    calls = _stub_route(monkeypatch, paid)

    out = local_first.run(req, op=op, accept=lambda result: "parse")

    assert out.result is paid
    assert calls == [req]
    assert router._serialize_request(calls[0]) == before


@pytest.mark.parametrize(
    ("enabled_set", "op", "local_expected"),
    [
        (enabled_set, op, enabled_set == owner)
        for owner, op in _SET_CASES
        for enabled_set, _ in _SET_CASES
    ],
)
def test_only_a_containing_enabled_set_activates_local_routing(
    monkeypatch: pytest.MonkeyPatch,
    enabled_set: str,
    op: str,
    local_expected: bool,
) -> None:
    monkeypatch.setattr(
        settings,
        "get_str",
        lambda key: "on" if key == f"llm.local_first.set.{enabled_set}.mode" else "off",
    )
    req = replace(_request(), source=op)
    answer = _answer()
    calls = _stub_route(monkeypatch, answer)

    out = local_first.run(req, op=op, accept=lambda result: None)

    assert out.result is answer
    assert len(calls) == 1
    if local_expected:
        assert calls[0].source == f"{op}:local"
        assert out.local is answer
    else:
        assert calls[0] is req
        assert out.local is None


def test_op_without_set_membership_stays_on_original_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(operations.LOCAL_FIRST_SETS, "triage", ())
    req = _request()
    calls = _stub_route(monkeypatch, _answer())

    out = local_first.run(req, op=_OP, accept=lambda result: "parse")

    assert calls == [req]
    assert calls[0] is req
    assert out.local is None


@pytest.mark.parametrize("enabled_set", ["triage", "mixed"])
def test_any_enabled_containing_set_activates_an_op(
    monkeypatch: pytest.MonkeyPatch, enabled_set: str
) -> None:
    monkeypatch.setitem(operations.LOCAL_FIRST_SETS, "mixed", (_OP,))
    monkeypatch.setattr(
        settings,
        "get_str",
        lambda key: "on" if key == f"llm.local_first.set.{enabled_set}.mode" else "off",
    )
    calls = _stub_route(monkeypatch, _answer())

    out = local_first.run(_request(), op=_OP, accept=lambda result: None)

    assert out.local is not None
    assert len(calls) == 1
    assert calls[0].source == f"{_OP}:local"


def test_accepted_local_request_unpins_tokens_and_preserves_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _request()
    before = router._serialize_request(req)
    local = _answer()
    calls = _stub_route(monkeypatch, local)

    out = local_first.run(req, op=_OP, accept=lambda result: None)

    assert out.result is local
    assert out.local is local
    assert out.escalated is None
    assert len(calls) == 1
    assert calls[0].source == f"{_OP}:local"
    assert calls[0].placement == "local"
    assert calls[0].max_tokens is None
    assert calls[0].prompt == req.prompt
    assert calls[0].messages == req.messages
    assert calls[0].features is not None
    assert calls[0].features["local_first"]["mode"] == "on"
    assert calls[0].features["local_first"]["arm"] == "local"
    assert router._serialize_request(req) == before
    assert req.features == {"caller": {"trace": "kept"}}


@pytest.mark.parametrize(
    ("reason", "local", "rejection"),
    [
        ("parse", _answer(), "parse"),
        ("truncated", _answer(stop_reason="length"), None),
        ("truncated", _answer(stop_reason="max_tokens"), None),
        (
            "timeout",
            _answer(error="timed out", timed_out=True, reason_class="transport"),
            None,
        ),
        (
            "timeout",
            _answer(error="timed out", timed_out=False, reason_class="transport"),
            None,
        ),
        ("refusal", _answer(error="refused", reason_class="content"), None),
        ("low_confidence", _answer(), "low_confidence"),
        ("validator", _answer(), "validator"),
        (
            "saturated",
            _answer(
                error="all local serving slots are busy",
                paused=True,
                reason_class="rate",
            ),
            None,
        ),
        (
            "transport",
            _answer(error="connection failed", reason_class="transport"),
            None,
        ),
        (
            "no_local_rung",
            _answer(
                error="placement_refusal: no local operation chain", placement=None
            ),
            None,
        ),
    ],
)
def test_d3_rejection_escalates_original_request_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    local: LlmResult,
    rejection: str | None,
) -> None:
    req = _request()
    paid = _answer(text="paid", placement="cloud", cost_usd=0.1)
    calls = _stub_route(monkeypatch, local, paid)

    def accept(result: LlmResult) -> str | None:
        assert result is local
        return rejection

    out = local_first.run(req, op=_OP, accept=accept)

    assert out.result is paid
    assert out.local is local
    assert out.escalated == reason
    assert len(calls) == 2
    assert calls[0].source == f"{_OP}:local"
    assert calls[0].placement == "local"
    escalated = calls[1]
    assert escalated.source == _OP
    assert escalated.max_tokens == 1024
    assert escalated.model == req.model
    assert escalated.placement == req.placement
    assert escalated.features is not None
    assert escalated.features["caller"] == {"trace": "kept"}
    assert escalated.features["local_first"]["escalated"] == reason
    assert router._route_features(escalated)["local_first"]["escalated"] == reason


@pytest.mark.parametrize("spent", [3.0, 3.5])
def test_exhausted_budget_never_calls_paid(
    monkeypatch: pytest.MonkeyPatch, spent: float
) -> None:
    monkeypatch.setattr(local_first, "_spent_usd", lambda op: spent)
    local = _answer()
    calls = _stub_route(monkeypatch, local)

    out = local_first.run(_request(), op=_OP, accept=lambda result: "validator")

    assert len(calls) == 1
    assert out.result is local
    assert out.escalated == "budget"


@pytest.mark.parametrize("timed_out", [True, False])
def test_five_failures_skip_local_for_ten_minutes_then_retry(
    monkeypatch: pytest.MonkeyPatch, timed_out: bool
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(local_first.time, "monotonic", lambda: clock[0])
    failed = _answer(
        error="local wire failed", reason_class="transport", timed_out=timed_out
    )
    paid = _answer(text="paid", placement="cloud")
    calls = _stub_route(monkeypatch, *([failed, paid] * 5), paid, _answer())

    for _ in range(5):
        local_first.run(_request(), op=_OP, accept=lambda result: None)

    assert len(calls) == 10
    clock[0] += 599
    skipped = local_first.run(_request(), op=_OP, accept=lambda result: None)
    assert len(calls) == 11
    assert calls[-1].source == _OP
    assert skipped.escalated == ("timeout" if timed_out else "transport")

    clock[0] += 1
    recovered = local_first.run(_request(), op=_OP, accept=lambda result: None)
    assert len(calls) == 12
    assert calls[-1].source == f"{_OP}:local"
    assert recovered.escalated is None


def test_success_breaks_consecutive_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _answer(error="timeout", timed_out=True, reason_class="transport")
    paid = _answer(placement="cloud")
    calls = _stub_route(
        monkeypatch, *([failed, paid] * 4), _answer(), failed, paid, _answer()
    )
    for _ in range(7):
        local_first.run(_request(), op=_OP, accept=lambda result: None)
    assert len(calls) == 12
    assert calls[-1].source == f"{_OP}:local"


def test_inflight_cap_skips_local_until_a_slot_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = local_first._slots[_OP]
    for _ in range(operations.LOCAL_FIRST_OPS[_OP].cap):
        assert slots.acquire(blocking=False)
    paid = _answer(placement="cloud")
    calls = _stub_route(monkeypatch, paid, _answer())
    try:
        capped = local_first.run(_request(), op=_OP, accept=lambda result: None)
        assert capped.escalated == "saturated"
        assert [req.source for req in calls] == [_OP]
    finally:
        for _ in range(operations.LOCAL_FIRST_OPS[_OP].cap):
            slots.release()
    resumed = local_first.run(_request(), op=_OP, accept=lambda result: None)
    assert resumed.escalated is None
    assert calls[-1].source == f"{_OP}:local"


@pytest.mark.parametrize("op", _PARK_OPS)
def test_batch_cap_defers_without_paid_call_then_recovers(
    monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    monkeypatch.setattr(
        local_first,
        "_spent_usd",
        lambda source: pytest.fail("a deferred call must not query paid spend"),
    )
    slots = local_first._slots[op]
    for _ in range(operations.LOCAL_FIRST_OPS[op].cap):
        assert slots.acquire(blocking=False)
    req = replace(_request(), source=op)
    local = _answer()
    calls = _stub_route(monkeypatch, local)
    try:
        out = local_first.run(req, op=op, accept=lambda result: None)
        assert calls == []
        assert out.result.paused
        assert out.deferred == "saturated"
        assert out.escalated is None
        assert out.local is None
    finally:
        for _ in range(operations.LOCAL_FIRST_OPS[op].cap):
            slots.release()

    recovered = local_first.run(req, op=op, accept=lambda result: None)
    assert recovered.result is local
    assert recovered.deferred is None
    assert recovered.escalated is None
    assert [call.source for call in calls] == [f"{op}:local"]


@pytest.mark.parametrize("op", _PARK_OPS)
@pytest.mark.parametrize(
    ("reason", "local"),
    [
        (
            "transport",
            _answer(error="connection failed", reason_class="transport"),
        ),
        (
            "timeout",
            _answer(error="timed out", timed_out=True, reason_class="transport"),
        ),
        (
            "no_local_rung",
            _answer(error="placement_refusal: no local operation chain"),
        ),
        (
            "saturated",
            _answer(error="local slots busy", paused=True, reason_class="rate"),
        ),
    ],
)
def test_batch_local_unavailability_defers_without_paid_budget_lookup(
    monkeypatch: pytest.MonkeyPatch, op: str, reason: str, local: LlmResult
) -> None:
    monkeypatch.setattr(
        local_first,
        "_spent_usd",
        lambda source: pytest.fail("a deferred call must not query paid spend"),
    )
    calls = _stub_route(monkeypatch, local)

    out = local_first.run(
        replace(_request(), source=op), op=op, accept=lambda result: None
    )

    assert [call.source for call in calls] == [f"{op}:local"]
    assert out.result.paused
    assert out.local is local
    assert out.deferred == reason
    assert out.escalated is None


@pytest.mark.parametrize("op", _PARK_OPS)
@pytest.mark.parametrize("reason", ["timeout", "transport"])
def test_batch_open_circuit_defers_without_local_or_paid_call(
    monkeypatch: pytest.MonkeyPatch, op: str, reason: str
) -> None:
    monkeypatch.setattr(local_first.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(
        local_first,
        "_state",
        local_first._Circuit(failures=5, skip_until=1600.0, reason=reason),
    )
    monkeypatch.setattr(
        local_first,
        "_spent_usd",
        lambda source: pytest.fail("a deferred call must not query paid spend"),
    )
    calls = _stub_route(monkeypatch)

    out = local_first.run(
        replace(_request(), source=op), op=op, accept=lambda result: None
    )

    assert calls == []
    assert out.result.paused
    assert out.deferred == reason
    assert out.escalated is None
    assert out.local is None


@pytest.mark.parametrize("op", _PARK_OPS)
@pytest.mark.parametrize("reason", ["parse", "validator"])
def test_batch_quality_failure_still_escalates_within_budget(
    monkeypatch: pytest.MonkeyPatch, op: str, reason: str
) -> None:
    spend_queries: list[str] = []

    def spend(source: str) -> float:
        spend_queries.append(source)
        return 0.25

    monkeypatch.setattr(local_first, "_spent_usd", spend)
    local = _answer()
    paid = _answer(text="paid", placement="cloud", cost_usd=0.1)
    calls = _stub_route(monkeypatch, local, paid)

    out = local_first.run(
        replace(_request(), source=op), op=op, accept=lambda result: reason
    )

    assert [call.source for call in calls] == [f"{op}:local", op]
    assert spend_queries == [op]
    assert out.result is paid
    assert out.local is local
    assert out.escalated == reason
    assert out.deferred is None


def test_registry_defaults_are_dark_and_register_local_arms() -> None:
    assert settings.REGISTRY["llm.local_first.enabled"].default is False
    assert settings.REGISTRY["llm.local_first.enabled"].env_var == "PRECIS_LOCAL_FIRST"
    assert operations.LOCAL_FIRST_SETS == {
        "triage": ("gripe_triage:tick",),
        "tables": ("survey_extract:table_recipe",),
        "claims": ("dtree:claim_check",),
    }
    for set_name, _ in _SET_CASES:
        mode = settings.REGISTRY[f"llm.local_first.set.{set_name}.mode"]
        assert mode.default == "off"
        assert mode.env_var == f"PRECIS_LOCAL_FIRST_SET_{set_name.upper()}"
    expected = {
        "gripe_triage:tick": ("triage", 6, 3.0),
        "survey_extract:table_recipe": ("table_recipe", 1, 10.0),
        "dtree:claim_check": ("dtree", 6, 10.0),
    }
    for op, (name, cap, budget) in expected.items():
        config = operations.LOCAL_FIRST_OPS[op]
        assert (config.name, config.op, config.paid_source) == (name, op, op)
        assert (config.cap, config.default_budget) == (cap, budget)
        assert config.park == (
            frozenset({"saturated", "timeout", "transport", "no_local_rung"})
            if op in _PARK_OPS
            else frozenset()
        )
        assert f"{op}:local" in operations.LLM_OPERATIONS
        assert f"llm.local_first.{name}.mode" not in settings.REGISTRY
        assert (
            settings.REGISTRY[f"llm.local_first.{name}.escalate_usd_day"].default
            == budget
        )


def _router_without_external_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *args, **kwargs: "local-model")
    monkeypatch.setattr(router._keyword_route, "match_request", lambda req: None)
    monkeypatch.setattr(
        router, "_drop_unavailable_rungs", lambda ladder, model: (ladder, [])
    )
    monkeypatch.setattr(live_config, "cloud_enabled", lambda: True)
    monkeypatch.setattr("precis.budget.breaker.gate_tier", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "precis.utils.llm.admit.check_dispatch", lambda *args, **kwargs: None
    )


def test_saturated_local_request_is_paused_and_never_lands_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _router_without_external_reads(monkeypatch)
    monkeypatch.setattr(local_first, "_spent_usd", lambda op: 3.0)
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda source: {
            "chain": [
                {
                    "placement": "local",
                    "model": "local-model",
                    "transport": "local",
                }
            ]
        },
    )
    monkeypatch.setattr(
        local_serving,
        "acquire",
        lambda model: local_serving.LocalSlot(
            host="local", resource=f"llm:{model}", reserved=False, paused=True
        ),
    )
    recorded: list[route_log.LlmCallRecord] = []
    monkeypatch.setattr(route_log, "enabled", lambda: True)
    monkeypatch.setattr(
        route_log, "record_call", lambda record: recorded.append(record)
    )

    class NeverProvider:
        def run(self, req: LlmRequest, *, model: str) -> LlmResult:
            pytest.fail("saturated local placement must not invoke a hosted provider")

    monkeypatch.setitem(router._PROVIDERS, Transport.LOCAL, NeverProvider())

    out = local_first.run(_request(), op=_OP, accept=lambda result: None)

    assert out.local is not None
    assert out.local.paused
    assert out.local.placement != "cloud"
    assert len(recorded) == 1
    assert recorded[0].placement_routed == "local"
    assert recorded[0].placement != "cloud"


@pytest.mark.parametrize("op", [op for _, op in _SET_CASES])
def test_missing_local_op_chain_refuses_before_tier_fallback(
    monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    _router_without_external_reads(monkeypatch)
    monkeypatch.setattr(live_config, "op_override", lambda source: None)

    def forbidden_fallback(*args: Any, **kwargs: Any) -> list[router.Rung]:
        pytest.fail("missing local op row must not inherit a tier chain")

    monkeypatch.setattr(router, "resolve_chain", forbidden_fallback)
    req = replace(_request(), source=f"{op}:local", placement="local", log_call=False)
    preview = router.placement_refusal(req)
    out = router.route(req)
    assert out.error is not None
    assert "placement_refusal" in out.error
    assert preview == out.error


@pytest.mark.parametrize(
    "transport", [Transport.CLAUDE_P, Transport.OPENAI_COMPAT, Transport.OPENAI_TOOLS]
)
@pytest.mark.parametrize("op", [op for _, op in _SET_CASES])
def test_local_label_cannot_admit_a_paid_transport(
    monkeypatch: pytest.MonkeyPatch, transport: Transport, op: str
) -> None:
    """An operator label cannot turn a potentially hosted wire into free work."""
    _router_without_external_reads(monkeypatch)
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda source: {
            "chain": [
                {
                    "placement": "local",
                    "model": "configured-model",
                    "transport": transport.value,
                }
            ]
        },
    )

    def forbidden_fallback(*args: Any, **kwargs: Any) -> list[router.Rung]:
        pytest.fail("an unsafe local op chain must not inherit a tier chain")

    class NeverProvider:
        def run(self, req: LlmRequest, *, model: str) -> LlmResult:
            pytest.fail("a local label must not authorize a paid transport")

    monkeypatch.setattr(router, "resolve_chain", forbidden_fallback)
    monkeypatch.setitem(router._PROVIDERS, transport, NeverProvider())
    req = replace(_request(), source=f"{op}:local", placement="local", log_call=False)
    preview = router.placement_refusal(req)
    out = router.route(req)
    assert out.error is not None
    assert "placement_refusal" in out.error
    assert preview == out.error


@pytest.mark.parametrize("op", [op for _, op in _SET_CASES])
def test_explicit_local_chain_is_allowed_by_preview_and_route(
    monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    _router_without_external_reads(monkeypatch)
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda source: {
            "chain": [
                {
                    "placement": "local",
                    "model": "configured-model",
                    "transport": "local",
                }
            ]
        },
    )
    monkeypatch.setattr(local_serving, "acquire", lambda model: None)
    calls: list[LlmRequest] = []

    class LocalProvider:
        def run(self, req: LlmRequest, *, model: str) -> LlmResult:
            calls.append(req)
            return _answer(model=model, tier=req.tier)

    monkeypatch.setitem(router._PROVIDERS, Transport.LOCAL, LocalProvider())
    req = replace(_request(), source=f"{op}:local", placement="local", log_call=False)

    assert router.placement_refusal(req) is None
    assert calls == []
    out = router.route(req)

    assert out.error is None
    assert out.placement == "local"
    assert out.model == "configured-model"
    assert len(calls) == 1
    assert calls[0].source == f"{op}:local"


def test_metadata_is_serializable_and_reaches_route_log_features() -> None:
    req = replace(
        _request(),
        features={"local_first": {"mode": "on", "arm": "paid", "escalated": "parse"}},
    )
    assert router._route_features(req)["local_first"]["escalated"] == "parse"
    assert isinstance(json.loads(router._serialize_request(req)), dict)


def test_spend_query_is_per_op_escalated_cost_in_one_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(2026, 10, 25, 23, 59, 59, tzinfo=UTC)

    class Ledger:
        sql: str = ""
        params: tuple[Any, ...] = ()

        def execute(self, sql: str, params: tuple[Any, ...]) -> Ledger:
            self.sql, self.params = sql, params
            return self

        def fetchone(self) -> tuple[float]:
            return (2.75,)

    ledger = Ledger()

    @contextmanager
    def connection() -> Iterator[Ledger]:
        yield ledger

    store = SimpleNamespace(pool=SimpleNamespace(connection=connection))
    monkeypatch.setattr(local_first.meter, "active_store", lambda: store)
    monkeypatch.setattr(local_first, "datetime", Clock)

    assert _RECORDED_SPEND(_OP) == 2.75
    start = datetime(2026, 10, 25, tzinfo=UTC)
    assert ledger.params == (_OP, start, start + timedelta(days=1))
    assert "sum(cost_usd)" in ledger.sql
    assert "source = %s" in ledger.sql
    assert "ts >= %s" in ledger.sql and "ts < %s" in ledger.sql
    assert "->>'escalated' IS NOT NULL" in ledger.sql
    assert "transport" not in ledger.sql, "quota-priced escalations count too"


def test_missing_spend_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_first.meter, "active_store", lambda: None)
    assert math.isinf(_RECORDED_SPEND(_OP))


def test_spend_query_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def connection() -> None:
        raise RuntimeError("ledger unavailable")

    store = SimpleNamespace(pool=SimpleNamespace(connection=connection))
    monkeypatch.setattr(local_first.meter, "active_store", lambda: store)
    assert math.isinf(_RECORDED_SPEND(_OP))
