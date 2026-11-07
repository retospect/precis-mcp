"""Per-slice bearer tokens (gr543694): the mint/grant/revoke registry
and the run-context stamps (:mod:`precis.slice_tokens`)."""

from __future__ import annotations

import hashlib
import os
import time
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
    # The file holds the token's digest, never the token: it is not a
    # credential, so 0644 — a server running as another user (through
    # colima's virtiofs, which reads as the colima user) can read it.
    assert (tmp_path / "td42").stat().st_mode & 0o777 == 0o644
    content = (tmp_path / "td42").read_text(encoding="utf-8")
    assert token not in content and token.rsplit(".", 1)[1] not in content
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == ("td42", 777, None, ())
    # a file holding the raw token (the pre-digest layout) grants nothing
    raw = "td43." + "a" * 64
    (tmp_path / "td43").write_text(f"{raw}\n", encoding="utf-8")
    assert slice_tokens.grant(tmp_path, f"Bearer {raw}") is None
    # wrong token, shared-token shape, tampered tail, no header, no dir
    assert slice_tokens.grant(tmp_path, "Bearer td42." + "0" * 64) is None
    assert slice_tokens.grant(tmp_path, "Bearer shared-token") is None
    # flip the last hex digit (a fixed "0" equals the real tail 1 time in 16)
    tampered = token[:-1] + ("1" if token[-1] == "0" else "0")
    assert slice_tokens.grant(tmp_path, f"Bearer {tampered}") is None
    assert slice_tokens.grant(tmp_path, None) is None
    assert slice_tokens.grant(None, f"Bearer {token}") is None
    # revoke = delete the file → the token 401s from the next request on
    assert slice_tokens.revoke(tmp_path, "td42") is True
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None
    assert slice_tokens.revoke(tmp_path, "td42") is False


def test_mint_without_an_agentlog(tmp_path: Path) -> None:
    """A failed open_run still mints — attribution rides the slice id."""
    token = slice_tokens.mint(tmp_path, "td7")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == ("td7", None, None, ())


def test_mint_rejects_a_non_td_slice_id(tmp_path: Path) -> None:
    """The id is a filename: anything but td<digits>[ja<digits>] never
    reaches the fs."""
    with pytest.raises(ValueError, match="td<digits>"):
        slice_tokens.mint(tmp_path, "../escape")
    with pytest.raises(ValueError, match="td<digits>"):
        slice_tokens.mint(tmp_path, "td42x1")


def test_job_qualified_names_validate_and_stay_strict(tmp_path: Path) -> None:
    """The grant names its run: td<todo>j<job> / td<todo>a<log> — and the
    bearer regex accepts nothing else, so a malformed locator never
    reaches the file lookup."""
    token = slice_tokens.mint(tmp_path, "td42j900")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == (
        "td42j900",
        None,
        None,
        (),
    )
    hexpart = "0" * 64
    for bad in ("td42x1", "td42.j900", "tdj42", "td42j", "j42", "td42j-1", "tD42"):
        assert slice_tokens.grant(tmp_path, f"Bearer {bad}.{hexpart}") is None


def test_grant_rejects_a_file_older_than_the_wall_clock_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTL is the run_slice wall-clock cap judged by mtime: a worker
    that dies before its revoke leaves the file, but it stops granting."""
    monkeypatch.setenv("PRECIS_RUN_SLICE_WALL_S", "3600")
    token = slice_tokens.mint(tmp_path, "td42")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == ("td42", None, None, ())
    stale = time.time() - 3700
    os.utime(tmp_path / "td42", (stale, stale))
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None


def test_grant_ttl_reads_the_wall_clock_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker and server share the one knob; a missing or unparsable
    value falls back to the run_slice default cap."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_WALL_S", raising=False)
    assert slice_tokens.grant_ttl_s() == 4 * 3600
    monkeypatch.setenv("PRECIS_RUN_SLICE_WALL_S", "600")
    assert slice_tokens.grant_ttl_s() == 600.0
    monkeypatch.setenv("PRECIS_RUN_SLICE_WALL_S", "junk")
    assert slice_tokens.grant_ttl_s() == 4 * 3600


def test_mint_sweeps_stale_grants_and_tmp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer-side reaper: each mint removes grant and mint-temp
    files past the TTL — the revoke never ran for them — while fresh
    files and foreign names stay."""
    monkeypatch.setenv("PRECIS_RUN_SLICE_WALL_S", "60")
    stale = time.time() - 3600
    for name in ("td1", "td2j5", ".td3.99.tmp"):
        p = tmp_path / name
        p.write_text("x\n", encoding="utf-8")
        os.utime(p, (stale, stale))
    keep = tmp_path / "unrelated"
    keep.write_text("x\n", encoding="utf-8")
    os.utime(keep, (stale, stale))
    fresh = slice_tokens.mint(tmp_path, "td9")  # mints while sweeping
    assert not (tmp_path / "td1").exists()
    assert not (tmp_path / "td2j5").exists()
    assert not (tmp_path / ".td3.99.tmp").exists()
    assert (tmp_path / "td9").exists() and keep.exists()
    assert slice_tokens.grant(tmp_path, f"Bearer {fresh}") is not None


def test_grant_file_with_non_ascii_content_is_no_grant(tmp_path: Path) -> None:
    """A corrupt grant must 401, never 500: content that does not decode
    or compare cleanly (compare_digest raises TypeError on non-ASCII) is
    simply no grant."""
    token = slice_tokens.mint(tmp_path, "td42")
    (tmp_path / "td42").write_text("tökën\n", encoding="utf-8")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None
    (tmp_path / "td42").write_bytes(b"\xff\xfe binary\n")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None


def test_mint_grant_roundtrip_with_scope_and_refs(tmp_path: Path) -> None:
    """gr574082: a scoped mint writes ``scope=``/``refs=`` k=v fields
    after the agentlog id; ``grant()`` carries them back on the grant."""
    token = slice_tokens.mint(
        tmp_path,
        "td42",
        agentlog_id=777,
        scope="reader",
        refs=("td900", "gr800"),
    )
    content = (tmp_path / "td42").read_text(encoding="utf-8")
    fields = content.split()
    assert fields[2] == "scope=reader" and fields[3] == "refs=td900,gr800"
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == (
        "td42",
        777,
        "reader",
        ("td900", "gr800"),
    )
    triage = slice_tokens.mint(tmp_path, "td9", scope="triage", refs=("td9",))
    assert slice_tokens.grant(tmp_path, f"Bearer {triage}") == (
        "td9",
        None,
        "triage",
        ("td9",),
    )
    # An agentlog id and the scope fields coexist in either minted order —
    # the writer always emits agentlog first, the parser takes either.
    token = "td5." + "b" * 64
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    (tmp_path / "td5").write_text(
        f"{digest} 900 scope=reader refs=td900\n", encoding="utf-8"
    )
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == (
        "td5",
        900,
        "reader",
        ("td900",),
    )


def test_grant_legacy_file_yields_scope_none(tmp_path: Path) -> None:
    """The pre-scope layout (digest + agentlog id only) still grants,
    with no scope — in-flight grants minted before the fields drain."""
    token = "td42." + "a" * 64
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    (tmp_path / "td42").write_text(f"{digest} 777\n", encoding="utf-8")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") == (
        "td42",
        777,
        None,
        (),
    )


@pytest.mark.parametrize(
    "tail",
    [
        "scope=bogus",  # not a scope the server knows
        "scope=reader refs=td900,,gr800",  # empty member
        "scope=reader refs=TD900",  # a ref must be ^[a-z]{2}[0-9]+$
        "scope=reader refs=zz",
        "refs=td900",  # refs without scope
        "scope=reader bogus=1",  # unknown key
        "scope=reader scope=triage",  # duplicate key
        "scope=reader refs=td900 refs=gr800",
        "junk",  # a bare field that is not an int
        "scope=reader 900",  # a bare int outside position 1
        "900 junk",  # bare int, then a bare non-int
    ],
)
def test_grant_file_malformed_scope_fields_grant_nothing(
    tmp_path: Path, tail: str
) -> None:
    """Fail closed: a grant file that parses weirdly grants nothing at
    all — never a scope the writer did not intend."""
    token = "td42." + "c" * 64
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    (tmp_path / "td42").write_text(f"{digest} {tail}\n", encoding="utf-8")
    assert slice_tokens.grant(tmp_path, f"Bearer {token}") is None


def test_mint_rejects_bad_scope_and_refs(tmp_path: Path) -> None:
    """The writer side validates too: a scope outside the known set, a
    ref that is not a handle, and refs with no scope all raise."""
    with pytest.raises(ValueError):
        slice_tokens.mint(tmp_path, "td42", scope="bogus")
    with pytest.raises(ValueError):
        slice_tokens.mint(tmp_path, "td42", scope="reader", refs=("bad",))
    with pytest.raises(ValueError):
        slice_tokens.mint(tmp_path, "td42", refs=("td9",))


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
