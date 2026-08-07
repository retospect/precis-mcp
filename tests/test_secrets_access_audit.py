"""``vault.events`` must say WHICH PROCESS asked for a key (migration 0111).

The audit trail itself is not new — ``vault.reveal`` has written a row since
0059. But the only identity it carried was ``session_user``, and every precis
process connects as the same role, so in prod all ~287k reveal rows say
``agent_rw``. The log proved a secret was read and could not say by what.
"""

from __future__ import annotations

import os
from typing import Any, cast

import psycopg
import pytest

from precis import secrets


@pytest.fixture(autouse=True)
def _clear_identity_cache() -> Any:
    secrets._IDENTITY = None
    yield
    secrets._IDENTITY = None


def test_identity_reports_this_process() -> None:
    host, user, pid, ppid, process = secrets._client_identity()
    assert pid == os.getpid()
    assert ppid == os.getppid()
    assert host
    assert user
    assert isinstance(process, str)


def test_identity_is_cached() -> None:
    """A reveal must not pay to recompute what cannot change."""
    assert secrets._client_identity() is secrets._client_identity()


def test_identity_process_field_is_bounded(monkeypatch: Any) -> None:
    """A command line must not be able to smuggle bulk text into the audit log."""
    monkeypatch.setattr(secrets.sys, "argv", ["/usr/bin/python3.13"] + ["x" * 400] * 5)
    _, _, _, _, process = secrets._client_identity()
    assert len(process) <= 200


def test_identity_survives_a_missing_passwd_entry(monkeypatch: Any) -> None:
    """Slim containers have no passwd entry for the uid; the uid still names us."""

    def _boom() -> str:
        raise KeyError("no passwd entry")

    monkeypatch.setattr(secrets.getpass, "getuser", _boom)
    _, user, _, _, _ = secrets._client_identity()
    assert user.startswith("uid:")


def test_scrub_redacts_a_secret_bearing_flag() -> None:
    """0111 is the first thing to persist argv into the DB — don't persist keys."""
    assert secrets._scrub_argv("--api-key=sk-ant-oat01-REAL") == "--api-key=<redacted>"
    assert secrets._scrub_argv("--token=abc123") == "--token=<redacted>"
    assert secrets._scrub_argv("--profile=agent") == "--profile=agent"


def test_scrub_redacts_a_bare_high_entropy_blob() -> None:
    blob = "postgresql://user:hunter2@10.0.0.1:6432/precis_prod?sslmode=require"
    assert secrets._scrub_argv(blob).startswith("<redacted:")
    assert "hunter2" not in secrets._scrub_argv(blob)


def test_scrub_keeps_ordinary_subcommands() -> None:
    for tok in ("worker", "--profile", "agent", "-v"):
        assert secrets._scrub_argv(tok) == tok


class _FakeConn:
    """Records the SQL it is handed; optionally fails the 6-arg form.

    ``six_arg_fails`` raises the *specific* error a pre-0111 DB raises, so the
    test exercises the narrow ``except psycopg.errors.UndefinedFunction`` rather
    than proving only that "some failure" is caught.
    """

    def __init__(
        self, *, six_arg_fails: bool, exc: BaseException | None = None
    ) -> None:
        self.six_arg_fails = six_arg_fails
        self.exc = exc or psycopg.errors.UndefinedFunction("no vault.reveal(text,...)")
        self.sql: list[str] = []
        self.rolled_back = False

    def execute(self, sql: str, params: tuple = ()) -> Any:
        self.sql.append(sql)
        if self.six_arg_fails and sql.count("%s") > 1:
            raise self.exc

        class _Cur:
            @staticmethod
            def fetchone() -> tuple[str]:
                return ("s3cret",)

        return _Cur()

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _FakeStore:
    def __init__(self, conn: _FakeConn) -> None:
        self.pool = type("P", (), {"connection": lambda _s: conn})()


def test_reveal_passes_client_identity() -> None:
    conn = _FakeConn(six_arg_fails=False)
    val = secrets._reveal(cast(Any, _FakeStore(conn)), "CLAUDE_CODE_OAUTH_TOKEN")

    assert val == "s3cret"
    # Six placeholders: the name plus the five identity fields.
    assert conn.sql[0].count("%s") == 6
    assert conn.rolled_back is False


def test_reveal_falls_back_on_a_pre_migration_db() -> None:
    """A rolling deploy runs both schemas; a secret resolving beats a full row.

    The rollback is load-bearing: the failed statement aborts the transaction,
    so without it the 1-arg retry dies with InFailedSqlTransaction and the
    secret fails to resolve at all — turning an audit-detail gap into an outage.
    """
    conn = _FakeConn(six_arg_fails=True)
    val = secrets._reveal(cast(Any, _FakeStore(conn)), "CLAUDE_CODE_OAUTH_TOKEN")

    assert val == "s3cret"
    assert conn.rolled_back is True
    assert conn.sql[-1].count("%s") == 1


def test_reveal_does_not_mask_a_real_error_as_a_pre_migration_db() -> None:
    """Only "that function doesn't exist" may take the silent fallback.

    A blanket catch here would let a genuine bug in the identity path (bad
    param type, a NUL byte psycopg rejects client-side) retry into the 1-arg
    call and succeed — so the process would write NULL-identity rows forever,
    indistinguishable from an un-migrated DB, with no operator-visible signal.
    A non-UndefinedFunction failure must reach the outer warn-once handler
    instead, which returns None so the caller falls through to file/default.
    """
    conn = _FakeConn(six_arg_fails=True, exc=TypeError("NUL byte in parameter"))
    val = secrets._reveal(cast(Any, _FakeStore(conn)), "CLAUDE_CODE_OAUTH_TOKEN")

    assert val is None
    assert conn.rolled_back is False
    # Never silently retried into the identity-less form.
    assert all(s.count("%s") > 1 for s in conn.sql)
