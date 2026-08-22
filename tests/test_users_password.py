"""Password KDF unit tests — :mod:`precis.users`, no DB.

The properties worth pinning are the ones a refactor could silently
break without any test going red at the call site: that the salt is
per-user, that the pepper is *required* (loudly) once a row records it,
and that the unknown-user burn costs what a real verify costs.
"""

from __future__ import annotations

import time

import pytest

from precis.users import (
    ALGO_PEPPERED,
    ALGO_PLAIN,
    PasswordRecord,
    PepperUnavailable,
    burn_verify,
    feed_token_digest,
    hash_password,
    mint_feed_token,
    normalize_login,
    verify_password,
)


def test_roundtrip_unpeppered() -> None:
    rec = hash_password("correct horse")
    assert rec.password_algo == ALGO_PLAIN
    assert verify_password("correct horse", rec)
    assert not verify_password("correct horsE", rec)


def test_roundtrip_peppered() -> None:
    rec = hash_password("correct horse", pepper="p3pp3r")
    assert rec.password_algo == ALGO_PEPPERED
    assert verify_password("correct horse", rec, pepper="p3pp3r")
    assert not verify_password("correct horse", rec, pepper="other")


def test_salt_is_per_call() -> None:
    """Two users with the same password must not share a hash — otherwise
    one cracked password cracks every account that reused it."""
    a = hash_password("same")
    b = hash_password("same")
    assert a.password_salt != b.password_salt
    assert a.password_hash != b.password_hash


def test_missing_pepper_raises_rather_than_denying() -> None:
    """A vault outage must look like an outage, not like everyone
    mistyping their password at once."""
    rec = hash_password("pw", pepper="p")
    with pytest.raises(PepperUnavailable):
        verify_password("pw", rec)


def test_unknown_algo_raises() -> None:
    rec = PasswordRecord(password_hash="00", password_salt="00", password_algo="argon2")
    with pytest.raises(PepperUnavailable):
        verify_password("pw", rec)


def test_corrupt_stored_hex_is_a_denial_not_a_crash() -> None:
    rec = PasswordRecord(
        password_hash="nothex", password_salt="alsonothex", password_algo=ALGO_PLAIN
    )
    assert not verify_password("pw", rec)


def test_empty_password_rejected_at_hash_time() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_normalize_login() -> None:
    assert normalize_login("  ReTo  ") == "reto"


def test_burn_verify_costs_about_what_a_verify_costs() -> None:
    """The unknown-login branch spends a comparable scrypt, so the roster
    isn't enumerable by timing. Generous bounds — this asserts the same
    order of magnitude, not a stopwatch-tight equality."""
    rec = hash_password("pw")
    t0 = time.perf_counter()
    verify_password("pw", rec)
    real = time.perf_counter() - t0
    t1 = time.perf_counter()
    burn_verify()
    burn = time.perf_counter() - t1
    assert 0.2 < burn / real < 5.0


def test_feed_token_digest_matches_mint() -> None:
    token, digest = mint_feed_token()
    assert feed_token_digest(token) == digest
    assert len(token) >= 32
    assert mint_feed_token()[0] != token
