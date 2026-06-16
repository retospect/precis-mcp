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


def _sessions_are_isolated() -> bool:
    """Detect whether the test env gives distinct Postgres backend
    sessions per ``psycopg.connect()`` call.

    The dev container reaches Postgres via ``host.docker.internal``;
    the libpq/OrbStack path multiplexes successive connections from
    one container to a single backend session (sometimes — it's
    flaky, not deterministic). Advisory locks are re-entrant within
    a session, so the "second concurrent claim is busy" invariant
    can't be checked from a single Python process when this is in
    effect. The contract still holds in production (each watcher /
    worker is its own process on its own host), so we skip the busy
    test rather than weaken the assertion to match the dev quirk.
    """
    if not _DSN:
        return False
    import psycopg

    try:
        a = psycopg.connect(_DSN, autocommit=True)
        b = psycopg.connect(_DSN, autocommit=True)
    except Exception:
        return False
    try:
        a_pid = a.execute("SELECT pg_backend_pid()").fetchone()[0]
        b_pid = b.execute("SELECT pg_backend_pid()").fetchone()[0]
    finally:
        a.close()
        b.close()
    return a_pid != b_pid


_sessions_isolated = pytest.mark.skipif(
    not _sessions_are_isolated(),
    reason=(
        "test env multiplexes psycopg connections to one Postgres "
        "backend session — busy-claim invariant not testable here"
    ),
)


def _fresh_sha() -> str:
    """A unique 64-char hex SHA per call.

    Avoids collision with leftover advisory locks from prior runs.
    In dev containers fronted by ``host.docker.internal``, OrbStack's
    proxy can keep a backend session alive after the python process
    exits — its session-scoped lock hangs around until the backend
    is recycled. Picking a fresh key per test sidesteps the carry-
    over without depending on cleanup hooks.
    """
    import secrets

    return secrets.token_hex(32)


@pytest.fixture
def sha() -> str:
    """Per-test fresh 64-char hex so we never collide with leftover
    advisory locks from earlier runs."""
    return _fresh_sha()


@_pg
class TestClaimIntegration:
    def test_acquire_then_release(self, sha: str) -> None:
        with Claim(_DSN, sha) as claim:
            assert claim.acquired is True
        # After exit, a second Claim on the same hash succeeds —
        # confirming the first one released the lock.
        with Claim(_DSN, sha) as second:
            assert second.acquired is True

    @_sessions_isolated
    def test_second_concurrent_claim_busy(self, sha: str) -> None:
        """A second Claim on the same hash from a *separate process*
        must fail to acquire while the first is held.

        The second Claim runs in a subprocess intentionally. The
        contract we ship is cross-process (each watcher, worker, and
        ``_watch_batch_ingest`` runs in its own process), so the
        subprocess shape is the one production exercises every day.

        Practical reason: in-container TCP forwarding to
        ``host.docker.internal`` multiplexes psycopg connections from
        a single process to one Postgres backend session — advisory
        locks are re-entrant within a session, so the simple two-
        ``Claim`` form in one process would spuriously report the
        second as acquired. A child process gets its own backend and
        the busy-state check is honest.
        """
        import subprocess
        import sys

        with Claim(_DSN, sha) as first:
            assert first.acquired is True
            # Child: try to acquire the same hash; print the
            # ``acquired`` outcome. Caller asserts on it.
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from precis.ingest.claim import Claim\n"
                        f"with Claim({_DSN!r}, {sha!r}) as c:\n"
                        "    print(c.acquired)\n"
                    ),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert child.stdout.strip() == "False", (
                f"child unexpectedly acquired the busy lock: "
                f"stdout={child.stdout!r} stderr={child.stderr!r}"
            )

    def test_release_on_exception(self, sha: str) -> None:
        """If the body raises, the claim is still released on exit."""
        with pytest.raises(RuntimeError):
            with Claim(_DSN, sha) as claim:
                assert claim.acquired is True
                raise RuntimeError("forced")
        # Lock is now free.
        with Claim(_DSN, sha) as after:
            assert after.acquired is True

    def test_different_hashes_dont_collide(self, sha: str) -> None:
        other_sha = "0123456789abcdef" + "11" * 24
        with Claim(_DSN, sha) as a, Claim(_DSN, other_sha) as b:
            assert a.acquired is True
            assert b.acquired is True
