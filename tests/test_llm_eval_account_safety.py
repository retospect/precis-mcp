"""Account dispatch must contain infrastructure failures (td574957)."""

from types import SimpleNamespace
from typing import Any

import pytest

from precis.llm_eval import account_pin
from precis.utils.llm.router import LlmRequest, Tier


@pytest.mark.parametrize(
    "failure", ["clock", "duration", "sleep", "scrub", "record", "warning"]
)
@pytest.mark.parametrize("rate", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_dispatch_failure_boundary(
    monkeypatch: pytest.MonkeyPatch, failure: str, rate: bool, raises: bool
) -> None:
    pin = account_pin.AccountPin("account", "model", "profile", "key", 1.0, ())
    req = LlmRequest(tier=Tier.MEDIUM, prompt="p")
    calls: list[LlmRequest] = []
    ticks = 0

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private credential in internal failure")

    def clock() -> float:
        nonlocal ticks
        ticks += 1
        if failure == "clock" or (failure == "duration" and ticks > 1):
            fail()
        return 0.0

    def run(request: LlmRequest, *, model: str) -> Any:
        calls.append(request)
        error = "HTTP Error 429" if rate else "provider error"
        if raises:
            raise RuntimeError(error)
        return account_pin._error(request, model, error)

    monkeypatch.setattr(
        account_pin, "provider_for", lambda transport: SimpleNamespace(run=run)
    )
    monkeypatch.setattr(account_pin, "_scrubbed_result", lambda pin, result: result)
    monkeypatch.setattr(account_pin, "record_dispatch", lambda *a, **kw: None)
    monkeypatch.setattr(account_pin.log, "warning", lambda *a, **kw: None)
    if failure in {"scrub", "record"}:
        name = "_scrubbed_result" if failure == "scrub" else "record_dispatch"
        monkeypatch.setattr(account_pin, name, fail)
    if failure == "warning":
        monkeypatch.setattr(account_pin.log, "warning", fail)
    dispatch = account_pin.account_pinned_dispatch(
        pin,
        clock=clock,
        sleep=fail if failure == "sleep" else lambda seconds: None,
    )
    first = dispatch(req)
    second = dispatch(req)
    if rate and failure != "clock":
        assert "halted" in (second.error or "")
        assert len(calls) == 1
    else:
        assert second.error == "account dispatch failed"
    assert "private credential" not in (first.error or "")
    assert "private credential" not in (second.error or "")
