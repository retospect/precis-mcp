"""Spectator-layer feature detection (gr496903): the config key is passed
when the installed engine's ``Config`` takes it and dropped, with a
warning, when it does not."""

from __future__ import annotations

import dataclasses
import logging

import pytest

pytest.importorskip("autocatpath")

from precis_pathway import runner

_CFG = {"name": "t", "spectators": {"species": "NO", "coverage": 0.75}}


def test_spectators_support_matches_the_engine_config_fields() -> None:
    from autocatpath.config import Config

    got = runner.spectators_supported()
    assert isinstance(got, bool)
    assert got == ("spectators" in {f.name for f in dataclasses.fields(Config)})


def test_supported_engine_gets_the_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "spectators_supported", lambda: True)
    assert runner.drop_unsupported_spectators(_CFG) is _CFG


def test_older_engine_drops_the_key_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runner, "spectators_supported", lambda: False)
    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        got = runner.drop_unsupported_spectators(_CFG)
    assert got == {"name": "t"}
    assert "spectators" in _CFG  # the caller's dict is not mutated
    assert "without the spectator layer" in caplog.text


def test_config_without_spectators_is_untouched_on_any_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "spectators_supported", lambda: False)
    plain = {"name": "t"}
    assert runner.drop_unsupported_spectators(plain) is plain
