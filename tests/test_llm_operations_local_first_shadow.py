"""S2 shadow contracts: preserve paid answers and pair measured local outcomes."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from precis import route_log, settings
from precis.utils.llm import local_first, operations, router
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, Transport

_OP = "gripe_triage:tick"
_ANNOTATE = route_log.annotate_local_first


def _request(**changes: Any) -> LlmRequest:
    return replace(
        LlmRequest(
            tier=Tier.BIG,
            source=_OP,
            prompt="screen this gripe",
            model="caller-model",
            max_tokens=1024,
            messages=[{"role": "user", "content": "caller message"}],
            features={"caller": {"trace": "kept"}},
            log_call=False,
            log_blobs=False,
        ),
        **changes,
    )


def _answer(**changes: Any) -> LlmResult:
    return replace(
        LlmResult(
            text="local answer",
            cost_usd=0.0,
            turns_used=1,
            model="local-model",
            tier=Tier.BIG,
            placement="local",
        ),
        **changes,
    )


@pytest.fixture(autouse=True)
def _isolated_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_first, "_state", local_first._Circuit())
    monkeypatch.setattr(
        local_first,
        "_slots",
        {
            op: threading.BoundedSemaphore(config.cap)
            for op, config in operations.LOCAL_FIRST_OPS.items()
        },
    )
    monkeypatch.setattr(settings, "get_bool", lambda *args, **kwargs: True)
    monkeypatch.setattr(settings, "get_str", lambda *args, **kwargs: "shadow")
    monkeypatch.setattr(
        local_first,
        "_spent_usd",
        lambda op: pytest.fail("shadow must not read escalation spend"),
    )
    monkeypatch.setattr(
        settings,
        "get_float",
        lambda *args, **kwargs: pytest.fail("shadow must not read escalation budget"),
    )
    monkeypatch.setattr(route_log, "annotate_local_first", lambda *args: None)


def _stub_route(
    monkeypatch: pytest.MonkeyPatch, *answers: LlmResult | Exception
) -> list[LlmRequest]:
    calls: list[LlmRequest] = []
    pending = iter(answers)

    def dispatch(req: LlmRequest) -> LlmResult:
        calls.append(req)
        answer = next(pending)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(router, "route", dispatch)
    return calls


def test_shadow_preserves_paid_result_and_stamps_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _request()
    before = router._serialize_request(req)
    paid = _answer(text="paid answer", placement="cloud", request_hash="paid-hash")
    local = _answer()
    calls = _stub_route(monkeypatch, paid, local)
    annotations: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        route_log,
        "annotate_local_first",
        lambda source, token, values: annotations.append((source, token, values)),
    )
    diff = {"label": ["noise", "actionable"], "confidence_delta": 0.1}

    def compare(candidate: LlmResult, baseline: LlmResult) -> tuple[bool, Any]:
        assert candidate is local
        assert baseline is paid
        return False, diff

    out = local_first.run(req, op=_OP, accept=lambda result: None, compare=compare)

    assert out.result is paid
    assert out.local is local
    assert out.escalated is None
    assert len(calls) == 2
    baseline, candidate = calls
    assert baseline.source == req.source
    assert baseline.max_tokens == req.max_tokens
    assert baseline.model == req.model
    assert baseline.placement == req.placement
    assert baseline.log_call and baseline.log_blobs
    assert candidate.source == f"{_OP}:local"
    assert candidate.placement == "local"
    assert candidate.max_tokens is None
    for dispatched in calls:
        assert dispatched.prompt == req.prompt
        assert dispatched.messages == req.messages
        assert dispatched.features is not None
        assert dispatched.features["caller"] == {"trace": "kept"}
        assert dispatched.features["local_first"]["mode"] == "shadow"
        assert dispatched.features["local_first"]["shadow"] is True
    assert baseline.features is not None and candidate.features is not None
    token = baseline.features["local_first"]["call"]
    assert token
    assert candidate.features["local_first"]["call"] == token
    assert baseline.features["local_first"]["arm"] == "paid"
    assert candidate.features["local_first"]["arm"] == "local"
    assert candidate.features["local_first"]["pair"] == paid.request_hash
    assert annotations == [
        (_OP, token, {"pair": paid.request_hash}),
        (
            f"{_OP}:local",
            token,
            {"accepted": True, "rejected": None, "agree": False, "diff": diff},
        ),
    ]
    assert router._serialize_request(req) == before
    assert req.features == {"caller": {"trace": "kept"}}


@pytest.mark.parametrize(
    "failure", ["rejected", "transport", "dispatch_exception", "compare_exception"]
)
def test_shadow_failures_keep_paid_answer_without_extra_paid_call(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    paid = _answer(text="paid", placement="cloud", request_hash="paid-hash")
    local = (
        _answer(error="local unavailable", reason_class="transport")
        if failure == "transport"
        else _answer()
    )
    calls = _stub_route(
        monkeypatch,
        paid,
        RuntimeError("local dispatch failed")
        if failure == "dispatch_exception"
        else local,
    )

    def compare(candidate: LlmResult, baseline: LlmResult) -> tuple[bool, Any]:
        if failure == "compare_exception":
            raise ValueError("comparison failed")
        return False, {"label": "different"}

    out = local_first.run(
        _request(),
        op=_OP,
        accept=lambda result: "validator" if failure == "rejected" else None,
        compare=compare,
    )

    assert out.result is paid
    assert out.escalated is None
    assert [call.source for call in calls] == [_OP, f"{_OP}:local"]
    assert calls[1].max_tokens is None


@pytest.mark.parametrize("diff", [float("nan"), {"not_json": object()}])
def test_shadow_malformed_comparison_keeps_paid_answer(
    monkeypatch: pytest.MonkeyPatch, diff: Any
) -> None:
    paid = _answer(placement="cloud")
    calls = _stub_route(monkeypatch, paid, _answer())

    out = local_first.run(
        _request(),
        op=_OP,
        accept=lambda result: None,
        compare=lambda *args: (True, diff),
    )

    assert out.result is paid
    assert len(calls) == 2


@pytest.mark.parametrize("on_set", ["triage", "overlap"])
def test_on_wins_over_shadow_in_overlapping_named_sets(
    monkeypatch: pytest.MonkeyPatch, on_set: str
) -> None:
    monkeypatch.setitem(operations.LOCAL_FIRST_SETS, "overlap", (_OP,))
    monkeypatch.setattr(
        settings,
        "get_str",
        lambda key: "on" if key == f"llm.local_first.set.{on_set}.mode" else "shadow",
    )
    local = _answer()
    calls = _stub_route(monkeypatch, local)
    annotations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        route_log,
        "annotate_local_first",
        lambda source, token, values: annotations.append(values),
    )

    def unexpected_compare(*args: Any) -> tuple[bool, Any]:
        pytest.fail("on mode must not invoke the shadow comparator")

    out = local_first.run(
        _request(), op=_OP, accept=lambda result: None, compare=unexpected_compare
    )

    assert out.result is local
    assert out.local is local
    assert [call.source for call in calls] == [f"{_OP}:local"]
    assert calls[0].features is not None
    assert calls[0].features["local_first"]["mode"] == "on"
    assert annotations == [{"accepted": True, "rejected": None}]


@pytest.mark.parametrize(
    ("op", "master", "has_comparator"),
    [
        (_OP, False, True),
        (_OP, True, False),
        ("survey_extract:table_recipe", True, True),
        ("dtree:claim_check", True, True),
    ],
    ids=["master-off", "missing-comparator", "offline-tables", "offline-claims"],
)
def test_shadow_without_live_comparison_preserves_original_path(
    monkeypatch: pytest.MonkeyPatch, op: str, master: bool, has_comparator: bool
) -> None:
    monkeypatch.setattr(settings, "get_bool", lambda *args, **kwargs: master)
    req = _request(source=op)
    before = router._serialize_request(req)
    original = _answer()
    calls = _stub_route(monkeypatch, original)

    def unexpected_accept(result: LlmResult) -> str | None:
        pytest.fail("passthrough must not evaluate local acceptance")

    def unexpected_compare(*args: Any) -> tuple[bool, Any]:
        pytest.fail("passthrough must not compare answers")

    out = local_first.run(
        req,
        op=op,
        accept=unexpected_accept,
        compare=unexpected_compare if has_comparator else None,
    )

    assert out.result is original
    assert out.local is None
    assert calls == [req]
    assert calls[0] is req
    assert router._serialize_request(req) == before


@pytest.mark.parametrize("skip", ["saturated", "transport"])
def test_shadow_cap_or_circuit_skips_local_without_extra_paid_call(
    monkeypatch: pytest.MonkeyPatch, skip: str
) -> None:
    slots = local_first._slots[_OP]
    held = 0
    if skip == "saturated":
        for _ in range(operations.LOCAL_FIRST_OPS[_OP].cap):
            assert slots.acquire(blocking=False)
            held += 1
    else:
        monkeypatch.setattr(local_first.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(
            local_first,
            "_state",
            local_first._Circuit(failures=5, skip_until=1600.0, reason=skip),
        )
    paid = _answer(placement="cloud")
    calls = _stub_route(monkeypatch, paid)
    try:
        out = local_first.run(
            _request(),
            op=_OP,
            accept=lambda result: None,
            compare=lambda *args: (True, {}),
        )
    finally:
        for _ in range(held):
            slots.release()

    assert out.result is paid
    assert out.local is None
    assert out.deferred is None
    assert not out.result.paused
    assert [call.source for call in calls] == [_OP]


def test_shadow_logged_rows_pair_to_paid_hash_with_lite_local(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(route_log, "_STORE", store)
    monkeypatch.setattr(route_log, "annotate_local_first", _ANNOTATE)
    tag = uuid4().hex
    req = _request(prompt=f"same prompt {tag}", features={"caller": {"trace": tag}})
    calls: list[LlmRequest] = []

    def logged_dispatch(request: LlmRequest) -> LlmResult:
        calls.append(request)
        local = request.placement == "local"
        answer = _answer(
            text="local" if local else "paid", placement="local" if local else "cloud"
        )
        request_hash = router._record_dispatch(
            request, answer, transport=Transport.LOCAL, duration_ms=1
        )
        return replace(answer, request_hash=request_hash)

    monkeypatch.setattr(router, "route", logged_dispatch)
    results = []
    for agree in [True, False]:

        def compare(
            candidate: LlmResult, baseline: LlmResult, agree: bool = agree
        ) -> tuple[bool, dict[str, bool]]:
            return agree, {"agree": agree}

        results.append(
            local_first.run(
                req,
                op=_OP,
                accept=lambda result: None,
                compare=compare,
            )
        )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT source, request_hash, features FROM llm_call_log "
            "WHERE features->'caller'->>'trace' = %s ORDER BY id",
            (tag,),
        ).fetchall()

    assert len(rows) == 4
    tokens = []
    for index, expected_agreement in enumerate([True, False]):
        paid, local = rows[index * 2 : index * 2 + 2]
        assert (paid[0], local[0]) == (_OP, f"{_OP}:local")
        assert paid[1] == results[index].result.request_hash
        assert isinstance(paid[1], str) and len(paid[1]) == 64
        assert local[1] is None  # metadata-only rows still receive outcomes
        paid_stamp = paid[2]["local_first"]
        local_stamp = local[2]["local_first"]
        assert paid_stamp["pair"] == local_stamp["pair"] == paid[1]
        assert paid_stamp["call"] == local_stamp["call"]
        assert local_stamp["agree"] is expected_agreement
        assert local_stamp["diff"] == {"agree": expected_agreement}
        assert local_stamp["accepted"] is True
        assert local[2]["caller"] == paid[2]["caller"] == {"trace": tag}
        tokens.append(paid_stamp["call"])
    assert tokens[0] != tokens[1]
    assert len(calls) == 4


@pytest.mark.parametrize("store_blobs", [True, False], ids=["full", "lite"])
def test_annotation_uses_call_token_for_identical_prompt_rows(
    monkeypatch: pytest.MonkeyPatch, store: Any, store_blobs: bool
) -> None:
    monkeypatch.setattr(route_log, "_STORE", store)
    tag = uuid4().hex
    source = f"shadow-annotation-{tag}"
    tokens = [uuid4().hex, uuid4().hex]
    for token in tokens:
        route_log.record_call(
            route_log.LlmCallRecord(
                source=source,
                tier="big",
                transport="local",
                model="local-model",
                tools_needed=False,
                request_text=f"identical request {tag}",
                response_text="answer",
                cost_usd=0.0,
                turns_used=1,
                duration_ms=1,
                errored=False,
                error=None,
                data_parsed=None,
                store_blobs=store_blobs,
                features={
                    "caller": {"trace": "kept"},
                    "local_first": {"call": token, "mode": "on", "arm": "local"},
                },
            ),
            store=store,
        )
    _ANNOTATE(source, tokens[0], {"accepted": True, "rejected": None})
    _ANNOTATE(source, tokens[1], {"accepted": False, "rejected": "validator"})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT request_hash, features FROM llm_call_log WHERE source = %s ORDER BY id",
            (source,),
        ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]
    assert (rows[0][0] is not None) is store_blobs
    for index, accepted in enumerate([True, False]):
        features = rows[index][1]
        assert features["caller"] == {"trace": "kept"}
        assert features["local_first"] == {
            "call": tokens[index],
            "mode": "on",
            "arm": "local",
            "accepted": accepted,
            "rejected": None if accepted else "validator",
        }
