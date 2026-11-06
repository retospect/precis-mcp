"""The decision CLI explains the router without running a provider."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.cli import llm as llm_cli
from precis.utils.llm import decide, live_config, operations, recipes, router


def test_decide_cli_json_and_legacy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = argparse.ArgumentParser()
    llm_cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["llm", "decide", "--source", "test", "--tier", "small", "hello"]
    )
    ledger = decide.Ledger((), {}, {}, frozenset(), datetime.now(UTC))
    monkeypatch.setattr(decide, "ledger_from_store", lambda *a, **kw: ledger)
    monkeypatch.setattr(
        recipes,
        "resolve",
        lambda *a: recipes.Resolution("test.task", "low", "default", "caller"),
    )
    monkeypatch.setattr(recipes, "lookup", lambda _s: None)
    monkeypatch.setattr(operations, "resolve_op_chain", lambda *a, **kw: None)
    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *a, **kw: "test-model")
    monkeypatch.setattr(
        router,
        "resolve_chain",
        lambda *a, **kw: [router.Rung(router.Transport.LOCAL, "test-model", "local")],
    )
    llm_cli._cmd_decide(cast("Any", object()), args)
    explanation, legacy = capsys.readouterr().out.rsplit("legacy:", 1)
    assert json.loads(explanation)["chosen"] is None
    assert "test-model@local/local" in legacy


def test_decide_override_shares_negative_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: dict[str, str] = {}
    reads: list[str] = []

    def read(key: str) -> str | None:
        reads.append(key)
        return rows.get(key)

    monkeypatch.setattr(live_config, "_read_setting", read)
    live_config.bust_cache()
    try:
        assert live_config.decide_override("test") is None
        rows["llm.decide.test"] = "shadow"
        assert live_config.decide_override("test") is None
        assert reads == ["llm.decide.test"]
        live_config.bust_cache()
        assert live_config.decide_override("test") == "shadow"
    finally:
        live_config.bust_cache()
