"""Unit tests for the idempotency advisory-lock helper.

``_idem_lock_key`` hashes idem strings deterministically into a
``bigint`` so ``pg_advisory_xact_lock`` can serialize concurrent
puts that share an idem key. The end-to-end race-safety check
(two threads racing the same idem put) requires a real Postgres
and lives behind the ``fresh_db`` fixture in the wider suite; here
we cover the deterministic helper.
"""

from __future__ import annotations

from precis.handlers.job import _idem_lock_key


class TestIdemLockKey:
    def test_deterministic(self) -> None:
        """Same input must produce the same lock key on every call.

        Two concurrent workers racing the same idem string must
        compute identical lock keys, otherwise they would not
        serialize on the same advisory lock.
        """
        assert _idem_lock_key("gripe:42") == _idem_lock_key("gripe:42")

    def test_different_inputs_different_keys(self) -> None:
        """Distinct idem strings produce different keys.

        BLAKE2b 8-byte digests give 64 bits of separation; for the
        test strings here collisions are vanishingly improbable.
        """
        keys = {
            _idem_lock_key("gripe:42"),
            _idem_lock_key("gripe:43"),
            _idem_lock_key("plan_tick:7"),
            _idem_lock_key(""),
            _idem_lock_key("gripe:42:extra"),
        }
        assert len(keys) == 5

    def test_fits_postgres_bigint(self) -> None:
        """Returned key must fit in Postgres ``bigint``.

        ``pg_advisory_xact_lock(bigint)`` rejects values outside
        ``[-2**63, 2**63-1]``.
        """
        bigint_min = -(2**63)
        bigint_max = 2**63 - 1
        for s in ["a", "gripe:42", "x" * 1024, "spicy hot"]:
            k = _idem_lock_key(s)
            assert bigint_min <= k <= bigint_max

    def test_unicode_safe(self) -> None:
        """Idem strings can be any UTF-8 sequence (they come from
        agent-supplied ``link=`` values). The hash must not throw."""
        for s in ["cafe", "Omega", "test string", "hidden text"]:
            _idem_lock_key(s)
