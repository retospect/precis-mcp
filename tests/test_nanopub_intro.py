"""The attesting key's introduction nanopub (key→ORCID declaration).

Pure units in the :mod:`tests.test_nanopub_signer_identity` style: the
vault is stood up as env vars, no DB, and the registry POST is a
recording stub — a live POST is the point of no return, so nothing here
may ever reach the network.
"""

from __future__ import annotations

import pytest

from precis.nanopub.keys import fingerprint, generate_keypair, public_key_b64

pytest.importorskip("nanopub")
pytest.importorskip("Crypto")

#: A real iD (Josiah Carberry's), so the ISO 7064 checksum passes.
CARBERRY = "0000-0002-1825-0097"


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> str:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_ATTESTING_PRIVATE_KEY", priv)
    monkeypatch.setenv("NANOPUB_ATTESTING_ORCID", f"https://orcid.org/{CARBERRY}")
    return priv


def test_dry_run_signs_locally_and_declares_the_key(vault: str) -> None:
    from precis.nanopub.intro import introduce

    calls: list[tuple[str, bytes]] = []
    result = introduce(
        None,
        name="Josiah Carberry",
        interactive=True,
        post=lambda url, data: calls.append((url, data)),
    )
    assert not calls, "a dry run must never POST"
    assert not result.live
    assert result.orcid == f"https://orcid.org/{CARBERRY}"
    assert result.trusty_uri.startswith("http")
    assert result.key_fingerprint == fingerprint(public_key_b64(vault))
    # The assertion carries the declaration triple and the public key.
    assert "declaredBy" in result.trig
    assert public_key_b64(vault) in result.trig
    assert "Josiah Carberry" in result.trig


def test_live_posts_the_exact_bytes(vault: str) -> None:
    from precis.nanopub.intro import introduce

    calls: list[tuple[str, bytes]] = []
    result = introduce(
        None,
        name="Josiah Carberry",
        interactive=True,
        live=True,
        post=lambda url, data: calls.append((url, data)),
    )
    assert result.live
    [(url, data)] = calls
    assert url == result.registry_url
    assert data == result.trig.encode("utf-8")
    assert len(data) == result.byte_count


def test_non_interactive_is_refused_before_touching_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.nanopub.intro import introduce

    # No vault at all: the interactive gate must fire first.
    monkeypatch.delenv("NANOPUB_ATTESTING_PRIVATE_KEY", raising=False)
    with pytest.raises(PermissionError):
        introduce(None, name="Josiah Carberry")


def test_a_blank_name_is_refused(vault: str) -> None:
    from precis.nanopub.intro import introduce

    with pytest.raises(ValueError):
        introduce(None, name="  ", interactive=True)
