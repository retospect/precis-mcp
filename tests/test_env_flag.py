"""Regression for gripe 162065: env gates must treat "0"/"false" as OFF.

The pass-enable gates in ``cli/worker.py`` used raw ``os.environ.get`` in a
boolean context, so a shared-env host-conditional emitting the string ``"0"``
for excluded hosts turned the pass **on** (non-empty string is truthy). The
fix routes those gates through ``precis.utils.env.env_flag``.
"""

from __future__ import annotations

import pytest

from precis.utils.env import env_flag, env_truthy


@pytest.mark.parametrize("raw", ["1", "true", "True", "YES", "on", " on "])
def test_truthy_tokens_enable(raw: str) -> None:
    assert env_truthy(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", "", None])
def test_falsey_tokens_disable(raw: str | None) -> None:
    # The core of gripe 162065: "0" and "false" must NOT enable a gate.
    assert env_truthy(raw) is False


def test_env_flag_reads_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_TEST_GATE", "0")
    assert env_flag("PRECIS_TEST_GATE") is False
    monkeypatch.setenv("PRECIS_TEST_GATE", "1")
    assert env_flag("PRECIS_TEST_GATE") is True
    monkeypatch.delenv("PRECIS_TEST_GATE", raising=False)
    assert env_flag("PRECIS_TEST_GATE") is False


# ── default=True (§F cycle b: default-ON gates, e.g. PRECIS_MATERIALIZE_EMBED) ──


def test_env_flag_default_true_unset_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_TEST_GATE", raising=False)
    assert env_flag("PRECIS_TEST_GATE", default=True) is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off"])
def test_env_flag_default_true_explicit_off_wins(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # The documented opt-out: a default-ON gate's ``default=True`` must
    # NOT override an explicit non-truthy token — only absence falls
    # back to the default.
    monkeypatch.setenv("PRECIS_TEST_GATE", raw)
    assert env_flag("PRECIS_TEST_GATE", default=True) is False


def test_env_flag_default_true_explicit_on_stays_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TEST_GATE", "1")
    assert env_flag("PRECIS_TEST_GATE", default=True) is True
