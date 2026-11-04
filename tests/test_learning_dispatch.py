"""Private learning never enters public ref diagnostics or continuation caches."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, PropertyMock

import pytest

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import Denied
from precis.learning_privacy import is_learning_call
from precis.response import Response
from precis.runtime import PrecisRuntime


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "learning"},
        {"kind": "paper,learning"},
        {"kind": " LEARNING "},
        {"id": "learning:/activity"},
        {"id": ["learning:le1"]},
    ],
)
def test_private_spellings_are_classified(payload: dict[str, Any]) -> None:
    assert is_learning_call(payload)


def test_public_requests_are_not_private() -> None:
    assert not is_learning_call({"kind": "paper", "id": "pa1"})
    assert not is_learning_call({"kind": "memory"})
    assert not is_learning_call({})


def _runtime(monkeypatch: pytest.MonkeyPatch) -> PrecisRuntime:
    runtime = PrecisRuntime(config=PrecisConfig(), hub=Hub())
    monkeypatch.setattr("precis.user_context.require_user", lambda _store: 7)
    return runtime


def _assert_public_refusal(
    monkeypatch: pytest.MonkeyPatch, verb: str, payload: dict[str, Any]
) -> None:
    runtime = _runtime(monkeypatch)
    owner = Mock(side_effect=AssertionError("owner lookup for public capability"))
    store = PropertyMock(side_effect=AssertionError("store read for public capability"))
    resolve = Mock(side_effect=AssertionError("handler resolved for public capability"))
    monkeypatch.setattr("precis.user_context.require_user", owner)
    monkeypatch.setattr(PrecisRuntime, "store", store)
    monkeypatch.setattr(runtime, "_resolve_handler", resolve)
    body, error = runtime.dispatch_with_status(verb, payload)
    assert error and "[error:Unsupported]" in body
    assert "options:" in body and all(verb in body for verb in ("get", "put", "edit"))
    assert "get(kind='learning', id='/activity', args={'days': 14})" in body
    owner.assert_not_called()
    store.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.parametrize("verb", ["search", "delete", "tag", "link"])
@pytest.mark.parametrize(
    "address",
    [{"kind": "learning"}, {"kind": "learning", "id": "x"}, {"id": "learning:le1"}],
)
def test_unsupported_verbs_are_public_capabilities(
    monkeypatch: pytest.MonkeyPatch, verb: str, address: dict[str, Any]
) -> None:
    _assert_public_refusal(monkeypatch, verb, address)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("extent", "fisheye"),
        ("view", "verbatim"),
        ("ring", {"kinds": "memory"}),
        ("q", "private"),
    ],
)
@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize(
    "address", [{"kind": "learning", "id": "/activity"}, {"id": "learning:le1"}]
)
def test_shared_read_paths_are_public_capability_refusals(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: Any,
    extra: bool,
    address: dict[str, Any],
) -> None:
    options = {"__extras__": {key: value}} if extra else {key: value}
    _assert_public_refusal(monkeypatch, "get", {**address, **options})


def test_private_success_bypasses_shared_ledger_and_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    record = Mock(side_effect=AssertionError("private content entered shared ledger"))
    monkeypatch.setattr(runtime, "_record_tool_call", record)
    monkeypatch.setattr(
        "precis.runtime.dispatch.search_feedback.record",
        Mock(side_effect=AssertionError("private content entered search feedback")),
    )
    monkeypatch.setattr(
        "precis._result_budget.fit_result_body",
        Mock(side_effect=AssertionError("private content entered result fitting")),
    )
    monkeypatch.setattr(runtime, "_resolve_handler", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime, "_validate_mode", lambda *_a: None)
    monkeypatch.setattr(
        runtime,
        "_invoke_handler",
        lambda *_a, **_k: Response(body="my private activity"),
    )
    body, error = runtime.dispatch_with_status(
        "get", {"kind": "learning", "id": "/activity"}, fold=True
    )
    assert not error and body == "my private activity"
    record.assert_not_called()


@pytest.mark.parametrize(
    ("verb", "payload"),
    [
        ("get", {"kind": "learning"}),
        ("get", {"kind": "learning", "id": "le99"}),
        ("get", {"id": "learning:/settings"}),
        ("put", {"kind": "learning", "id": "/checkin"}),
        ("edit", {"kind": "learning", "id": "/settings"}),
        ("search", {"kind": "paper,learning", "q": "private"}),
        ("search", {"kind": " LEARNING ", "q": "private"}),
        ("search", {"kind": "paper", "id": "learning:le1"}),
        ("search", {"kind": "learning", "id": ["le1"]}),
        ("search", {"id": ["learning:le1"]}),
        ("get", {"kind": "learning", "id": ["me1"], "extent": "fisheye"}),
    ],
)
def test_private_authentication_precedes_handler_resolution(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    payload: dict[str, Any],
) -> None:
    runtime = _runtime(monkeypatch)

    def refuse(_store: Any) -> int:
        raise Denied("identity required", next="mint a personal MCP token at /account")

    monkeypatch.setattr("precis.user_context.require_user", refuse)
    resolve = Mock(side_effect=AssertionError("handler reached before authentication"))
    monkeypatch.setattr(runtime, "_resolve_handler", resolve)
    body, error = runtime.dispatch_with_status(verb, payload)
    assert error and "[error:Denied]" in body
    resolve.assert_not_called()


@pytest.mark.parametrize("ident", [["me1"], [1], ["learning:me1"], {}, 1.5])
def test_malformed_learning_id_refuses_after_authentication(
    monkeypatch: pytest.MonkeyPatch, ident: Any
) -> None:
    runtime = _runtime(monkeypatch)
    owner = Mock(return_value=7)
    resolve = Mock(side_effect=AssertionError("malformed id reached handler"))
    monkeypatch.setattr("precis.user_context.require_user", owner)
    monkeypatch.setattr(runtime, "_resolve_handler", resolve)

    body, error = runtime.dispatch_with_status(
        "get", {"kind": "learning", "id": ident, "extent": "fisheye"}
    )

    assert error and "[error:BadInput]" in body
    assert "get(kind='learning', id='/activity', args={'days': 14})" in body
    owner.assert_called_once()
    resolve.assert_not_called()


def test_private_failures_do_not_log_exception_contents(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _runtime(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "_resolve_handler",
        Mock(side_effect=RuntimeError("a private learner answer")),
    )
    body, error = runtime.dispatch_with_status("get", {"kind": "learning"})
    assert error and "[error:Upstream]" in body
    assert "private learner answer" not in body
    assert "private learner answer" not in caplog.text


def test_oversized_private_body_has_no_shared_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    monkeypatch.setattr(runtime, "_resolve_handler", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime, "_validate_mode", lambda *_a: None)
    monkeypatch.setattr(
        runtime, "_invoke_handler", lambda *_a, **_k: Response(body="private" * 1000)
    )
    body, error = runtime.dispatch_with_status("get", {"kind": "learning"})
    assert error and "[error:BadInput]" in body
    assert "too large" in body and "privateprivate" not in body


def test_private_path_still_honors_read_only_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    monkeypatch.setattr(
        "precis.runtime.dispatch._denied_verbs_env", lambda: frozenset({"put"})
    )
    body, error = runtime.dispatch_with_status("put", {"kind": "learning"})
    assert error and "[error:Denied]" in body


@pytest.mark.parametrize(
    ("verb", "payload"),
    [
        ("get", {"id": "learning:/activity"}),
        ("get", {"kind": "memory", "id": "learning:/activity"}),
        ("get", {"kind": "learning", "id": ["le1", "me1"]}),
        ("get", {"id": ["learning:le1", "memory:me1"]}),
        ("edit", {"kind": "learning", "id": ["le1", "me1"]}),
        ("edit", {"kind": "memory", "id": ["learning:le1", "me1"]}),
        ("search", {"kind": "memory,learning", "queries": [{"q": "private"}]}),
        ("search", {"kind": " LEARNING ", "q": "private"}),
        ("put", {"kind": "learning", "text": {"$ref": "me1"}}),
    ],
)
def test_learning_classification_never_reaches_public_dispatch(
    monkeypatch: pytest.MonkeyPatch, verb: str, payload: dict[str, Any]
) -> None:
    runtime = _runtime(monkeypatch)
    public = Mock(side_effect=AssertionError("learning reached public dispatch"))
    resolved = Mock(return_value=object())
    monkeypatch.setattr(runtime, "_dispatch_status", public)
    monkeypatch.setattr(runtime, "_resolve_handler", resolved)
    monkeypatch.setattr(runtime, "_validate_mode", lambda *_a: None)
    monkeypatch.setattr(
        runtime, "_invoke_handler", lambda *_a, **_k: Response(body="private route")
    )
    assert is_learning_call(payload)
    runtime.dispatch_with_status(verb, payload)
    public.assert_not_called()
    assert all(call.args[0] == "learning" for call in resolved.call_args_list)
