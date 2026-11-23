"""Ask chat preflight (gr681394)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import secrets
from precis_web import chat_turns


@pytest.fixture
def present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "chat_mcp.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(chat_turns.MCP_CONFIG_ENV, str(cfg))
    monkeypatch.setattr(chat_turns.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    monkeypatch.setattr(secrets, "_STORE", None)
    return cfg


def test_preflight_all_present(present: Path) -> None:
    assert chat_turns.preflight() == []


def test_preflight_reports_missing_mcp_config(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(chat_turns.MCP_CONFIG_ENV)
    assert any(chat_turns.MCP_CONFIG_ENV in line for line in chat_turns.preflight())
    monkeypatch.setenv(chat_turns.MCP_CONFIG_ENV, str(present.parent / "gone.json"))
    assert any("does not exist" in line for line in chat_turns.preflight())


def test_preflight_reports_missing_api_key(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    lines = chat_turns.preflight()
    assert len(lines) == 1 and "ANTHROPIC_API_KEY" in lines[0]


def test_preflight_reports_invalid_utf8_api_key_file(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    (present.parent / "ANTHROPIC_API_KEY").write_bytes(b"\xff")

    assert chat_turns.preflight() == [
        "ANTHROPIC_API_KEY is not resolvable; store it on the /secrets page"
    ]


def test_preflight_reports_missing_claude(
    present: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_turns.shutil, "which", lambda _n: None)
    assert any("claude" in line for line in chat_turns.preflight())
