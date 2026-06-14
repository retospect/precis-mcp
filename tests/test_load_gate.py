"""Tests for the 1-minute load-average gate used by heavy passes."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from precis.utils.load_gate import (
    _load_ceiling,
    current_load,
    skip_if_high_load,
)


def test_ceiling_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_LOAD_CEILING", "20.0")
    assert _load_ceiling() == 20.0


def test_ceiling_auto_scales_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_LOAD_CEILING", raising=False)
    expected = (os.cpu_count() or 4) * 1.5
    assert _load_ceiling() == expected


def test_ceiling_falls_back_when_env_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_LOAD_CEILING", "not-a-number")
    expected = (os.cpu_count() or 4) * 1.5
    assert _load_ceiling() == expected


def test_skip_returns_false_under_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_LOAD_CEILING", "100.0")
    # Ceiling well above any plausible load.
    assert skip_if_high_load("test_pass") is False


def test_skip_returns_true_over_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_LOAD_CEILING", "0.0001")
    # Ceiling so small that any nonzero load triggers.
    # Mock current_load to a known nonzero value to keep the test
    # deterministic regardless of the host's actual load.
    with patch(
        "precis.utils.load_gate.current_load", return_value=10.0
    ):
        assert skip_if_high_load("test_pass") is True


def test_skip_returns_false_when_loadavg_unavailable() -> None:
    """Platforms without getloadavg → skip is False (degrade safe)."""
    with patch(
        "precis.utils.load_gate.current_load", return_value=None
    ):
        assert skip_if_high_load("test_pass") is False


def test_current_load_returns_a_float_on_unix() -> None:
    """Smoke: getloadavg actually answers on the test platform."""
    load = current_load()
    # macOS / Linux always have getloadavg; only Windows returns None.
    # The test runs in the precis-dev container (Debian) so we expect
    # a number; bail with a skip-message if not.
    if load is None:
        pytest.skip("os.getloadavg() not available on this platform")
    assert load >= 0.0
