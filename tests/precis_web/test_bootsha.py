"""The web reports the commit it booted on (gr496849)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis_web import bootsha


def test_healthz_carries_boot_sha_header_and_keeps_body(client) -> None:
    resp = client.get("/healthz")
    assert resp.json() == {"status": "ok"}
    assert resp.headers[bootsha.BOOT_SHA_HEADER] == bootsha.BOOT_SHA


class _Dist:
    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self._raw


def _install(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    monkeypatch.setattr(bootsha, "distribution", lambda _name: _Dist(raw))


def test_reads_the_pep610_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    _install(monkeypatch, json.dumps({"vcs_info": {"commit_id": sha}}))
    assert bootsha.read_installed_sha() == sha


@pytest.mark.parametrize(
    "raw",
    [None, "", "{}", json.dumps({"dir_info": {"editable": True}}), "not json"],
)
def test_no_recorded_commit_is_unknown(
    monkeypatch: pytest.MonkeyPatch, raw: Any
) -> None:
    _install(monkeypatch, raw)
    assert bootsha.read_installed_sha() == bootsha.UNKNOWN


def test_missing_distribution_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_name: str) -> Any:
        raise ModuleNotFoundError("precis-mcp")

    monkeypatch.setattr(bootsha, "distribution", boom)
    assert bootsha.read_installed_sha() == bootsha.UNKNOWN
