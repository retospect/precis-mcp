"""Capability universalization — incidental gates default (factory slice 5).

The patent / edgar data-source kinds used to hard-gate on a raw-cache
directory (and edgar on a User-Agent string). Those are *incidental*
(a cache dir / an id string any host can provide), so they now default
via ``precis.config`` and are dropped from ``KindSpec.requires_env``.
The genuinely-scarce gate stays (patent still needs the EPO credentials,
via ``requires_secret``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import config
from precis.handlers.edgar import EdgarHandler
from precis.kind_gate import gate


def test_cache_root_honours_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdgcache")
    assert config.cache_root("patent-raw") == Path("/tmp/xdgcache/precis/patent-raw")


def test_cache_root_defaults_to_home(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert config.cache_root("edgar-raw") == Path("/home/u/.cache/precis/edgar-raw")


def test_patent_raw_root_env_override_else_default(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_PATENT_RAW_ROOT", "/data/patents")
    assert config.patent_raw_root() == Path("/data/patents")
    monkeypatch.delenv("PRECIS_PATENT_RAW_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/c")
    assert config.patent_raw_root() == Path("/tmp/c/precis/patent-raw")


def test_edgar_raw_root_env_override_else_default(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_EDGAR_RAW_ROOT", "/data/edgar")
    assert config.edgar_raw_root() == Path("/data/edgar")
    monkeypatch.delenv("PRECIS_EDGAR_RAW_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/c")
    assert config.edgar_raw_root() == Path("/tmp/c/precis/edgar-raw")


def test_edgar_user_agent_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_EDGAR_USER_AGENT", raising=False)
    assert config.edgar_user_agent() == config.DEFAULT_EDGAR_USER_AGENT
    assert "precis-mcp" in config.edgar_user_agent()
    monkeypatch.setenv("PRECIS_EDGAR_USER_AGENT", "acme (you@acme.com)")
    assert config.edgar_user_agent() == "acme (you@acme.com)"


def test_edgar_kind_has_no_incidental_env_gate() -> None:
    """edgar declares no requires_env → gate loads it with nothing set."""
    assert EdgarHandler.spec.requires_env == ()
    assert not EdgarHandler.spec.requires_secret
    verdict = gate(EdgarHandler.spec, disabled=frozenset())
    assert verdict.loaded is True


def test_patent_kind_drops_raw_root_but_keeps_creds() -> None:
    """patent no longer gates on the cache dir; EPO creds stay (requires_secret)."""
    patent = pytest.importorskip("precis.handlers.patent")  # needs epo_ops dep
    spec = patent.PatentHandler.spec
    assert spec.requires_env == ()  # PRECIS_PATENT_RAW_ROOT gone
    assert "EPO_OPS_CLIENT_KEY" in spec.requires_secret  # real gate stays
