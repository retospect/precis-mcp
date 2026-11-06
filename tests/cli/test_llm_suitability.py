"""The suitability CLI rolls up the ledger and reports card writes."""

from __future__ import annotations

import argparse
from typing import Any, cast

import pytest

from precis.cli import llm as llm_cli
from precis.utils.llm import suitability


def _row(**kw: Any) -> suitability.SuitabilityRow:
    base: dict[str, Any] = {
        "model": "m1",
        "adapter": "openai_compat",
        "task_kind": "summarize",
        "band": "any",
        "n": 5,
        "n_eff": 5.0,
        "alpha": 6.0,
        "beta": 1.0,
        "lower90": 0.68,
        "upper90": 0.98,
        "mean": 0.86,
        "label": "marginal",
        "headroom": None,
        "identity": None,
        "drift_at": None,
        "carded": True,
        "written": True,
    }
    base.update(kw)
    return suitability.SuitabilityRow(**base)


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    llm_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["llm", "suitability", *argv])


def test_suitability_cli_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_rollup(store: Any, **kw: Any) -> list[suitability.SuitabilityRow]:
        seen.update(kw)
        return [_row()]

    monkeypatch.setattr(suitability, "rollup", fake_rollup)
    llm_cli._cmd_suitability(
        cast("Any", object()),
        _parse("--dry-run", "--days", "7", "--model", "m1"),
    )
    out = capsys.readouterr().out
    assert seen == {"window_days": 7, "write": False, "model": "m1"}
    assert "m1@openai_compat summarize band=any label=marginal n=5" in out
    assert "identity=requested only" in out
    assert "drift=no" in out
    assert "dry-run — nothing written" in out


def test_suitability_cli_reports_cards_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [_row(model="m1", written=True), _row(model="m2", written=False)]
    seen: dict[str, Any] = {}

    def fake_rollup(store: Any, **kw: Any) -> list[suitability.SuitabilityRow]:
        seen.update(kw)
        return rows

    monkeypatch.setattr(suitability, "rollup", fake_rollup)
    llm_cli._cmd_suitability(cast("Any", object()), _parse())
    out = capsys.readouterr().out
    assert seen == {"window_days": 30, "write": True, "model": None}
    assert "suitability: 2 posteriors, 1 cards written" in out
