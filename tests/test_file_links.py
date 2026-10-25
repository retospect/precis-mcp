"""``precis.file_links`` — the HMAC token mint/verify half of the
signed-PDF-link slice (gr517423, spec §Slice 3). Pure unit tests: the
key comes from the env layer of ``get_secret``, so no Postgres needed —
the vault-mint path is exercised by patching ``precis.secrets``
directly in ``test_key_minted_once_and_reused``."""

from __future__ import annotations

from typing import Any

import pytest

from precis import file_links

_SHA = "a" * 64
_SHA_B = "b" * 64


@pytest.fixture(autouse=True)
def _link_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A fixed env-layer key; the secrets-file layer points at an empty
    dir so nothing ambient leaks into the resolution."""
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "test-link-key")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))


def test_round_trip() -> None:
    token = file_links.mint("get", _SHA, 42, 600)
    claims = file_links.verify(token, op="get", sha=_SHA)
    assert (claims.op, claims.sha, claims.ref_id) == ("get", _SHA, 42)
    assert token.count(".") == 5


@pytest.mark.parametrize("field", ["op", "sha", "ref_id", "exp"])
def test_tampered_field_refuses(field: str) -> None:
    parts = file_links.mint("get", _SHA, 42, 600).split(".")
    idx = {"op": 1, "sha": 2, "ref_id": 3, "exp": 4}[field]
    parts[idx] = {"op": "put", "sha": _SHA_B, "ref_id": "43", "exp": "1"}[field]
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(".".join(parts), op="get", sha=_SHA)


def test_wrong_sha_refuses() -> None:
    token = file_links.mint("get", _SHA, 42)
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(token, op="get", sha=_SHA_B)


def test_wrong_op_refuses() -> None:
    token = file_links.mint("get", _SHA, 42)
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(token, op="put", sha=_SHA)


def test_expired_token() -> None:
    token = file_links.mint("get", _SHA, 42, 600, now=1_000_000)
    with pytest.raises(file_links.TokenExpired):
        file_links.verify(token, op="get", sha=_SHA, now=1_000_601)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x",
        "v1.get.sha.1.2",
        "v2.get.a.1.2.sig",
        "v1.get.a.x.2.sig",
        "v1.get.a.1.2.sig.extra",
        # Non-ASCII (the ``?t=…%C3%A9`` probe): ``compare_digest`` would
        # TypeError → unauthenticated 500 — must be a clean 403.
        "v1.get.a.1.2.é",
        f"v1.get.{_SHA}.1.2.é",
        "v1.get.a.٤٢.2.sig",
    ],
)
def test_malformed_refuses(bad: str) -> None:
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(bad, op="get", sha=_SHA)


def test_ttl_clamps_to_3600() -> None:
    claims = file_links.parse(file_links.mint("get", _SHA, 42, 99_999, now=1000))
    assert claims.exp == 1000 + file_links.MAX_TTL_S


def test_ttl_defaults_to_600() -> None:
    claims = file_links.parse(file_links.mint("get", _SHA, 42, now=1000))
    assert claims.exp == 1000 + file_links.DEFAULT_TTL_S


def test_mint_rejects_unknown_op() -> None:
    with pytest.raises(ValueError, match="op"):
        file_links.mint("delete", _SHA, 42)


def test_key_minted_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ensure_file_link_key`` is the ``ensure_pepper`` shape: the first
    call vaults a fresh key, later calls read the same one back — a
    re-mint would silently invalidate every live link."""
    monkeypatch.delenv("PRECIS_FILE_LINK_KEY")
    vault_state: dict[str, str] = {}
    sets: list[str] = []

    def fake_get(name: str, *, store: Any = None, default: Any = None) -> Any:
        return vault_state.get(name, default)

    def fake_set(name: str, value: str, *, store: Any) -> None:
        sets.append(name)
        vault_state[name] = value

    monkeypatch.setattr("precis.secrets.get_secret", fake_get)
    monkeypatch.setattr("precis.secrets.set_secret", fake_set)
    bogus_store: Any = object()
    first = file_links.ensure_file_link_key(store=bogus_store)
    assert sets == [file_links.FILE_LINK_SECRET]
    assert file_links.ensure_file_link_key(store=bogus_store) == first
    assert sets == [file_links.FILE_LINK_SECRET]


def test_racing_mints_converge_on_the_stored_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vault.set_secret`` is ON CONFLICT DO UPDATE: a racer landing
    between our set and re-read overwrites us — the returned key must be
    the vault's survivor, not our minted loser (which would sign dead
    links)."""
    monkeypatch.delenv("PRECIS_FILE_LINK_KEY")
    vault_state: dict[str, str] = {}

    def fake_get(name: str, *, store: Any = None, default: Any = None) -> Any:
        return vault_state.get(name, default)

    def fake_set(name: str, value: str, *, store: Any) -> None:
        vault_state[name] = value
        vault_state[name] = "racer-key"  # a concurrent mint overwrites

    monkeypatch.setattr("precis.secrets.get_secret", fake_get)
    monkeypatch.setattr("precis.secrets.set_secret", fake_set)
    bogus_store: Any = object()
    assert file_links.ensure_file_link_key(store=bogus_store) == "racer-key"


def test_verify_never_mints(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key configured → 403, not a vault write: an unauthenticated
    request must not make precis_web create (or fail to create) the
    signing key."""
    monkeypatch.delenv("PRECIS_FILE_LINK_KEY")
    sets: list[str] = []
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **kw: None)
    monkeypatch.setattr("precis.secrets.set_secret", lambda *a, **kw: sets.append(a))
    bogus_store: Any = object()
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(
            f"v1.get.{_SHA}.1.2.deadbeef", op="get", sha=_SHA, store=bogus_store
        )
    assert sets == []


def test_key_rotation_invalidates_old_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = file_links.mint("get", _SHA, 42)
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "rotated-key")
    with pytest.raises(file_links.TokenInvalid):
        file_links.verify(token, op="get", sha=_SHA)
