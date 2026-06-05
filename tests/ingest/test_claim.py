"""Tests for ``precis.ingest.claim``.

Pure-unit tests against the key derivation function and the context-
manager wiring run unconditionally. The interesting integration tests
(acquire / busy / release / auto-release-on-close) require a live
Postgres reachable via ``PRECIS_DATABASE_URL`` — skipped otherwise.

Auto-release behaviour is exactly the property we want to verify: a
session that holds an advisory lock and then closes (cleanly or not)
must surrender it within Postgres' next attempt. That's the whole
reason we chose advisory locks over a row-based claim.
"""

from __future__ import annotations

import os

import pytest

from precis.ingest.claim import Claim, _key_for

# ---------------------------------------------------------------------------
# Pure key derivation
# ---------------------------------------------------------------------------


class TestKeyDerivation:
    def test_first_16_hex_chars_decode_in_signed_bigint_range(self) -> None:
        # All zeros → 0
        assert _key_for("0" * 64) == 0
        # All Fs in first 16 chars → -1 (after the unsigned→signed map)
        assert _key_for("f" * 16 + "0" * 48) == -1

    def test_collision_resistant_on_close_inputs(self) -> None:
        a = "abcdef0123456789" + "0" * 48
        b = "abcdef012345678a" + "0" * 48
        assert _key_for(a) != _key_for(b)

    def test_only_first_16_chars_matter(self) -> None:
        # Bytes past the first 16 hex chars don't influence the key —
        # documented as intentional (8 bytes of collision resistance
        # is plenty for our corpus sizes).
        a = "abcdef0123456789" + "0" * 48
        b = "abcdef0123456789" + "f" * 48
        assert _key_for(a) == _key_for(b)


# ---------------------------------------------------------------------------
# Integration — requires Postgres
# ---------------------------------------------------------------------------

_DSN = os.environ.get("PRECIS_DATABASE_URL", "")
_pg = pytest.mark.skipif(not _DSN, reason="PRECIS_DATABASE_URL not set")
_SHA = "fedcba9876543210" + "00" * 24  # 64-char hex


@_pg
class TestClaimIntegration:
    def test_acquire_then_release(self) -> None:
        with Claim(_DSN, _SHA) as claim:
            assert claim.acquired is True
        # After exit, a second Claim on the same hash succeeds —
        # confirming the first one released the lock.
        with Claim(_DSN, _SHA) as second:
            assert second.acquired is True

    def test_second_concurrent_claim_busy(self) -> None:
        with Claim(_DSN, _SHA) as first:
            assert first.acquired is True
            # Open a second Claim while the first is still active —
            # should fail to acquire.
            with Claim(_DSN, _SHA) as second:
                assert second.acquired is False

    def test_release_on_exception(self) -> None:
        """If the body raises, the claim is still released on exit."""
        with pytest.raises(RuntimeError):
            with Claim(_DSN, _SHA) as claim:
                assert claim.acquired is True
                raise RuntimeError("forced")
        # Lock is now free.
        with Claim(_DSN, _SHA) as after:
            assert after.acquired is True

    def test_different_hashes_dont_collide(self) -> None:
        other_sha = "0123456789abcdef" + "11" * 24
        with Claim(_DSN, _SHA) as a, Claim(_DSN, other_sha) as b:
            assert a.acquired is True
            assert b.acquired is True
