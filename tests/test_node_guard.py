"""The shared node guard fails under CI and skips locally (gr495017)."""

from __future__ import annotations

from typing import Any

import pytest
from _pytest.outcomes import Failed, Skipped

from tests import _node


def _call(monkeypatch: pytest.MonkeyPatch, *, ci: str | None) -> str:
    monkeypatch.setattr(_node.shutil, "which", lambda _name: None)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    fixture: Any = _node.require_node
    return str(fixture.__wrapped__())


def test_missing_node_fails_under_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Failed, match="setup-node"):
        _call(monkeypatch, ci="true")


def test_missing_node_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Skipped):
        _call(monkeypatch, ci=None)
