"""Who an attesting signature names.

A nanopub is attributed to an ORCID, never to a login, so the identity
has to come from the human driving the sign surface — their
``web_users.orcid`` (set on ``/account``), passed down through
:func:`precis.nanopub.mint.sign` into
:func:`precis.nanopub.keys.load_profile`. These are pure units: the vault
is stood up as env vars (``get_secret`` reads env first), so no DB.

The rule under test is the pairing. The attesting key is registered to
one identity at the nanopub registry; signing under a different iD with
it would publish a claim attributed to a person who never held the key.
So a mismatch is a refusal — which is also what turns the account field
into an authorization check rather than a label.
"""

from __future__ import annotations

import pytest

from precis.nanopub.keys import generate_keypair, load_profile, orcid_uri

pytest.importorskip("nanopub")
pytest.importorskip("Crypto")

#: A real iD (Josiah Carberry's), so the ISO 7064 checksum passes.
CARBERRY = "0000-0002-1825-0097"
OTHER = "0000-0002-0685-6171"


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_ATTESTING_PRIVATE_KEY", priv)
    monkeypatch.setenv("NANOPUB_ATTESTING_ORCID", f"https://orcid.org/{CARBERRY}")


def test_orcid_uri_accepts_any_form_and_rejects_a_bad_checksum() -> None:
    assert orcid_uri(f"https://orcid.org/{CARBERRY}") == f"https://orcid.org/{CARBERRY}"
    assert orcid_uri(CARBERRY.replace("-", "")) == f"https://orcid.org/{CARBERRY}"
    with pytest.raises(ValueError):
        orcid_uri("0000-0002-1825-0098")  # last digit off by one
    with pytest.raises(ValueError):
        orcid_uri("")


def test_the_signers_own_id_is_what_the_profile_carries(vault: None) -> None:
    profile = load_profile(
        None,
        "attesting",
        interactive=True,
        signer_orcid=CARBERRY,
        signer_name="Josiah Carberry",
    )
    assert profile.orcid_id == f"https://orcid.org/{CARBERRY}"
    assert profile.name == "Josiah Carberry"


def test_a_signer_the_key_is_not_registered_to_is_refused(vault: None) -> None:
    with pytest.raises(PermissionError) as exc:
        load_profile(None, "attesting", interactive=True, signer_orcid=OTHER)
    # The message has to name both iDs — "refused" without them leaves the
    # person guessing which of the two is the one to fix.
    assert CARBERRY in str(exc.value) and OTHER in str(exc.value)


def test_a_mistyped_signer_id_is_refused_before_anything_is_signed(
    vault: None,
) -> None:
    with pytest.raises(PermissionError):
        load_profile(
            None, "attesting", interactive=True, signer_orcid="0000-0002-1825-0098"
        )


def test_no_signer_falls_back_to_the_vault_identity(vault: None) -> None:
    # The CLI sign surface has no web session to read an account from.
    profile = load_profile(None, "attesting", interactive=True)
    assert profile.orcid_id == f"https://orcid.org/{CARBERRY}"


def test_the_bot_identity_ignores_a_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.nanopub.vocab import BOT_AGENT

    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    profile = load_profile(None, "bot", signer_orcid=CARBERRY)
    # A bot signature is the machine's, and attributing it to a human
    # would be exactly the claim the non-attesting key exists to avoid.
    assert profile.orcid_id == str(BOT_AGENT)
