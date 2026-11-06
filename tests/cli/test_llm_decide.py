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


def test_decide_mode_set_read_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """``llm decide-mode`` writes the app_settings row mode_for reads."""
    from precis.budget import meter
    from precis.budget import settings as budget_settings

    rows: dict[str, str] = {}
    store = cast("Any", object())
    monkeypatch.setattr(meter, "active_store", lambda: store)
    monkeypatch.setattr(budget_settings, "get_setting", lambda _s, key: rows.get(key))
    monkeypatch.setattr(
        budget_settings,
        "set_setting",
        lambda _s, key, value: rows.__setitem__(key, value),
    )
    monkeypatch.setattr(
        budget_settings, "clear_setting", lambda _s, key: rows.pop(key, None)
    )
    monkeypatch.delenv("PRECIS_LLM_DECIDE", raising=False)
    live_config.bust_cache()
    decide.reset_mode_cache()
    try:
        ns = argparse.Namespace(prefix="default", mode="shadow")
        llm_cli._cmd_decide_mode(store, ns)
        assert rows["llm.decide.mode"] == "shadow"
        assert decide.mode_for("code.review") == "shadow"

        llm_cli._cmd_decide_mode(store, argparse.Namespace(prefix="code", mode="on"))
        assert rows["llm.decide.code"] == "on"
        assert decide.mode_for("code.review") == "on"
        assert decide.mode_for("other.task") == "shadow"

        llm_cli._cmd_decide_mode(
            store, argparse.Namespace(prefix="default", mode="clear")
        )
        assert "llm.decide.mode" not in rows
        assert decide.mode_for("other.task") == "legacy"

        with pytest.raises(SystemExit, match="reserved"):
            llm_cli._cmd_decide_mode(
                store, argparse.Namespace(prefix="mode", mode="on")
            )
        with pytest.raises(SystemExit, match="invalid mode"):
            llm_cli._cmd_decide_mode(
                store, argparse.Namespace(prefix="default", mode="bogus")
            )
        assert "llm.decide.mode" not in rows
    finally:
        live_config.bust_cache()
        decide.reset_mode_cache()


def test_decide_mode_kind_writes_and_clears_exact_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--kind`` writes ``llm.decide.kind.<task_kind>`` — read by mode_for
    ahead of the prefix and default rows, and bound to that kind only."""
    from precis.budget import meter
    from precis.budget import settings as budget_settings

    rows: dict[str, str] = {}
    store = cast("Any", object())
    monkeypatch.setattr(meter, "active_store", lambda: store)
    monkeypatch.setattr(budget_settings, "get_setting", lambda _s, key: rows.get(key))
    monkeypatch.setattr(
        budget_settings,
        "set_setting",
        lambda _s, key, value: rows.__setitem__(key, value),
    )
    monkeypatch.setattr(
        budget_settings, "clear_setting", lambda _s, key: rows.pop(key, None)
    )
    monkeypatch.delenv("PRECIS_LLM_DECIDE", raising=False)
    live_config.bust_cache()
    decide.reset_mode_cache()
    try:
        llm_cli._cmd_decide_mode(
            store,
            argparse.Namespace(
                prefix=None, mode="on", kind="chunk.summarize", show=False
            ),
        )
        assert rows["llm.decide.kind.chunk.summarize"] == "on"
        assert decide.mode_for("chunk.summarize") == "on"
        assert decide.mode_for("chunk.other") == "legacy"
        llm_cli._cmd_decide_mode(
            store,
            argparse.Namespace(
                prefix=None, mode="clear", kind="chunk.summarize", show=False
            ),
        )
        assert "llm.decide.kind.chunk.summarize" not in rows
        assert decide.mode_for("chunk.summarize") == "legacy"
    finally:
        live_config.bust_cache()
        decide.reset_mode_cache()


def test_decide_mode_show_lists_rows(
    store: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--show`` prints every ``llm.decide.*`` row, ordered by key."""
    from precis.budget import settings as budget_settings

    budget_settings.set_setting(store, "llm.decide.mode", "shadow")
    budget_settings.set_setting(store, "llm.decide.kind.chunk.summarize", "on")
    try:
        llm_cli._cmd_decide_mode(
            store,
            argparse.Namespace(prefix=None, mode=None, kind=None, show=True),
        )
        out = capsys.readouterr().out
        assert "llm.decide.kind.chunk.summarize = on" in out
        assert "llm.decide.mode = shadow" in out
        assert out.index("llm.decide.kind.") < out.index("llm.decide.mode")
    finally:
        budget_settings.clear_setting(store, "llm.decide.mode")
        budget_settings.clear_setting(store, "llm.decide.kind.chunk.summarize")


def test_decide_mode_misuse_exits_with_usage() -> None:
    """No mode, ``--kind`` beside a prefix, and ``--show`` with arguments
    all die on the one-line usage message; an unknown mode names the
    valid values."""
    store = cast("Any", object())
    with pytest.raises(SystemExit, match="usage"):
        llm_cli._cmd_decide_mode(
            store, argparse.Namespace(prefix=None, mode=None, kind=None, show=False)
        )
    with pytest.raises(SystemExit, match="usage"):
        llm_cli._cmd_decide_mode(
            store, argparse.Namespace(prefix="code", mode=None, kind=None, show=False)
        )
    with pytest.raises(SystemExit, match="usage"):
        llm_cli._cmd_decide_mode(
            store,
            argparse.Namespace(
                prefix="code", mode="on", kind="code.review", show=False
            ),
        )
    with pytest.raises(SystemExit, match="usage"):
        llm_cli._cmd_decide_mode(
            store, argparse.Namespace(prefix="code", mode="on", kind=None, show=True)
        )
    with pytest.raises(SystemExit, match="invalid mode"):
        llm_cli._cmd_decide_mode(
            store,
            argparse.Namespace(prefix="default", mode="bogus", kind=None, show=False),
        )


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
