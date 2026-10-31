"""Per-slice bearer tokens (gr543694): the mint/grant/revoke registry
and the run-context stamps (:mod:`precis.slice_tokens`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import slice_tokens
from precis.utils import inproc_context


def test_registry_dir_env_wins_but_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "slice-tokens"
    monkeypatch.setenv(slice_tokens.ENV_DIR, str(d))
    # Set-but-missing is a misconfig: never created on the writer's host,
    # a dir the container does not mount would 401 every slice call.
    assert slice_tokens.registry_dir() is None
    d.mkdir()
    assert slice_tokens.registry_dir() == d


def test_registry_dir_default_needs_the_ensure_created_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy handshake: no env var, and the default under the
    mcp-http state dir counts only once the ensure script made it."""
    monkeypatch.delenv(slice_tokens.ENV_DIR, raising=False)
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(tmp_path / "state"))
    assert slice_tokens.registry_dir() is None
    want = tmp_path / "state" / "slice-tokens"
    want.mkdir(parents=True)
    assert slice_tokens.registry_dir() == want


def test_mint_grant_revoke_roundtrip(tmp_path: Path) -> None:
    token = slice_tokens.mint(tmp_path, "td42", agentlog_id=777)
    assert token.startswith("td42.") and len(token.rsplit(".", 1)[1]) == 64
    assert (tmp_path / "td42").stat().st_mode & 0o777 == 0o600
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == ("td42", 777)
    # wrong token, shared-token shape, tampered tail, no header, no dir
    assert slice_tokens.grant(tmp_path, "Bearer td42." + "0" * 64) is None
    assert slice_tokens.grant(tmp_path, "Bearer shared-token") is None
    assert slice_tokens.grant(tmp_path, f"Bearer {token[:-1]}0") is None
    assert slice_tokens.grant(tmp_path, None) is None
    assert slice_tokens.grant(None, f"Bearer {token}") is None
    # revoke = delete the file → the token 401s from the next request on
    assert slice_tokens.revoke(tmp_path, "td42") is True
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None
    assert slice_tokens.revoke(tmp_path, "td42") is False


def test_mint_without_an_agentlog(tmp_path: Path) -> None:
    """A failed open_run still mints — attribution rides the slice id."""
    token = slice_tokens.mint(tmp_path, "td7")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == ("td7", None)


def test_mint_rejects_a_non_td_slice_id(tmp_path: Path) -> None:
    """The id is a filename: anything but td<digits> never reaches the fs."""
    with pytest.raises(ValueError, match="td<digits>"):
        slice_tokens.mint(tmp_path, "../escape")


def test_stamp_meta_only_inside_a_slice_context() -> None:
    assert slice_tokens.stamp_meta({"a": 1}) == {"a": 1}
    assert slice_tokens.stamp_meta(None) is None
    ctx = inproc_context.TickContext(slice_id="td42")
    with inproc_context.tick_context(ctx):
        assert slice_tokens.stamp_meta({"a": 1}) == {"a": 1, "slice": "td42"}
        # never overwrites a slice the caller set
        assert slice_tokens.stamp_meta({"slice": "td9"}) == {"slice": "td9"}
        assert slice_tokens.stamp_meta(None) == {"slice": "td42"}
        assert slice_tokens.current_slice_id() == "td42"
    assert slice_tokens.current_slice_id() is None
