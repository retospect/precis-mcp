"""asa_slack.bot — the dispatch seam.

Only `_dispatch_warm` for now: the runtime warm-up MUST precede `route`,
else `live_config.chain_override` reads dark in the storeless asa-slack
process and every turn falls back to the default (claude) chain.
"""

from __future__ import annotations

import pytest

bot = pytest.importorskip("asa_slack.bot")


def test_dispatch_warm_binds_runtime_before_dispatch(monkeypatch):
    calls: list[str] = []
    sentinel_req = object()
    sentinel_result = object()

    def fake_warm() -> None:
        calls.append("warm")

    def fake_dispatch(req):
        assert req is sentinel_req
        calls.append("dispatch")
        return sentinel_result

    monkeypatch.setattr(bot, "warm_runtime", fake_warm)
    monkeypatch.setattr(bot, "route", fake_dispatch)

    assert bot._dispatch_warm(sentinel_req) is sentinel_result
    assert calls == ["warm", "dispatch"]
