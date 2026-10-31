"""``tests._timing.budget`` — the plain-run limit is never weakened; a
coverage tracer (or the coverage-db job's env flag) scales it x3."""

from __future__ import annotations

import sys

import pytest

from tests import _timing


def _no_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_timing.COVERAGE_ENV, raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    monkeypatch.delitem(sys.modules, "coverage", raising=False)


def test_budget_is_unscaled_without_a_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_coverage(monkeypatch)
    assert _timing.budget(20.0) == 20.0
    assert _timing.budget(200) == 200


def test_budget_scales_x3_when_the_coverage_env_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_coverage(monkeypatch)
    monkeypatch.setenv(_timing.COVERAGE_ENV, "1")
    assert _timing.budget(20.0) == 60.0


def test_budget_scales_x3_under_a_live_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_coverage(monkeypatch)
    monkeypatch.setattr(sys, "gettrace", lambda: lambda *a: None)
    assert _timing.budget(20.0) == 60.0
