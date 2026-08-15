"""Resolver-layer tests for :mod:`precis.secrets` — no DB required.

Covers the env-override-wins order, the file fallback, the default, and the
reveal cache, using a fake store so the logic is exercised without Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from precis import secrets as vault


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    vault.bind_store(None)
    vault.invalidate()
    vault._warned.clear()
    yield
    vault.bind_store(None)
    vault.invalidate()


class _FakeConn:
    def __init__(self, value: str | None, counter: list[int]) -> None:
        self._value = value
        self._counter = counter

    def execute(self, _sql: str, _params: Any) -> Any:
        self._counter[0] += 1
        val = self._value
        return type("R", (), {"fetchone": lambda self: (val,)})()


class _FakePool:
    def __init__(self, value: str | None, counter: list[int]) -> None:
        self._value = value
        self._counter = counter

    @contextmanager
    def connection(self) -> Any:
        yield _FakeConn(self._value, self._counter)


class _FakeStore:
    def __init__(self, value: str | None) -> None:
        self.calls = [0]
        self.pool = _FakePool(value, self.calls)


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "from-env")
    store: Any = _FakeStore("from-vault")
    assert vault.get_secret("MY_KEY", store=store) == "from-env"
    assert store.calls[0] == 0  # never touched the vault


def test_vault_reveal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    store: Any = _FakeStore("from-vault")
    assert vault.get_secret("MY_KEY", store=store) == "from-vault"


def test_reveal_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    store: Any = _FakeStore("v")
    assert vault.get_secret("MY_KEY", store=store) == "v"
    assert vault.get_secret("MY_KEY", store=store) == "v"
    assert store.calls[0] == 1  # second call served from cache
    vault.invalidate("MY_KEY")
    assert vault.get_secret("MY_KEY", store=store) == "v"
    assert store.calls[0] == 2


def test_file_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("FILE_KEY", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    (tmp_path / "FILE_KEY").write_text("from-file\n")
    assert vault.get_secret("FILE_KEY") == "from-file"  # no store bound


def test_default_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    assert vault.get_secret("NOPE", default="fallback") == "fallback"


def test_require_secret_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    with pytest.raises(KeyError):
        vault.require_secret("NOPE")


def test_reveal_error_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A vault that raises (schema absent / key unset) degrades to file/default,
    never propagates — so the vault can ship dark."""
    monkeypatch.delenv("BOOM", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))

    class _Boom:
        @contextmanager
        def connection(self) -> Any:
            raise RuntimeError("no schema")
            yield  # pragma: no cover

    store = type("S", (), {"pool": _Boom()})()
    assert vault.get_secret("BOOM", store=store, default="d") == "d"


# ── complete_dsn_password: pgpass completion for cross-boundary DSNs ──────
#
# The worker DSN is password-free by design (§L): libpq fills the password
# from PGPASSFILE at connect time. A DSN handed across an isolation boundary
# (the §13 agent container) lands where no pgpass exists — the 2026-08-15
# plan_tick zombie loop, every in-container call dying fe_sendauth. The
# helper completes the password on the host before the DSN crosses.


@pytest.fixture()
def pgpass(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    path = tmp_path / "pgpass"
    monkeypatch.setenv("PGPASSFILE", str(path))
    return path


def test_complete_dsn_fills_password_from_pgpass(pgpass: Any) -> None:
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:s3cret\n")
    dsn = "postgresql://agent_rw@db.example.com:6432/precis_prod"
    out = vault.complete_dsn_password(dsn)
    assert out == "postgresql://agent_rw:s3cret@db.example.com:6432/precis_prod"


def test_complete_dsn_wildcard_entry_matches(pgpass: Any) -> None:
    pgpass.write_text(
        "# comment\n"
        "\n"
        "other:5432:*:someone:nope\n"
        "db.example.com:6432:*:agent_rw:wildpw\n"
    )
    out = vault.complete_dsn_password(
        "postgresql://agent_rw@db.example.com:6432/precis_prod"
    )
    assert ":wildpw@" in out


def test_complete_dsn_existing_password_unchanged(pgpass: Any) -> None:
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:other\n")
    dsn = "postgresql://agent_rw:mine@db.example.com:6432/precis_prod"
    assert vault.complete_dsn_password(dsn) == dsn


def test_complete_dsn_no_entry_unchanged(pgpass: Any) -> None:
    pgpass.write_text("elsewhere:5432:db:user:pw\n")
    dsn = "postgresql://agent_rw@db.example.com:6432/precis_prod"
    assert vault.complete_dsn_password(dsn) == dsn


def test_complete_dsn_missing_pgpass_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "absent"))
    dsn = "postgresql://agent_rw@db.example.com:6432/precis_prod"
    assert vault.complete_dsn_password(dsn) == dsn


def test_complete_dsn_password_is_url_quoted(pgpass: Any) -> None:
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:p@ss/w:rd\n")
    out = vault.complete_dsn_password(
        "postgresql://agent_rw@db.example.com:6432/precis_prod"
    )
    assert ":p%40ss%2Fw%3Ard@" in out
    # And the round-trip parse must recover the raw password.
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(out)["password"] == "p@ss/w:rd"


def test_complete_dsn_pgpass_escaped_colon(pgpass: Any) -> None:
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:a\\:b\\\\c\n")
    out = vault.complete_dsn_password(
        "postgresql://agent_rw@db.example.com:6432/precis_prod"
    )
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(out)["password"] == "a:b\\c"


def test_complete_dsn_keyword_conninfo(pgpass: Any) -> None:
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:kwpw\n")
    out = vault.complete_dsn_password(
        "host=db.example.com port=6432 dbname=precis_prod user=agent_rw"
    )
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(out)["password"] == "kwpw"


def test_complete_dsn_garbage_unchanged(pgpass: Any) -> None:
    assert vault.complete_dsn_password("not a dsn at all ===") == "not a dsn at all ==="


def test_complete_dsn_empty_password_userinfo_completed_cleanly(pgpass: Any) -> None:
    """``user:@host`` (explicit empty password) parses as password="" and must
    complete without doubling the ``:`` separator (``user::pw@host``)."""
    pgpass.write_text("db.example.com:6432:precis_prod:agent_rw:filled\n")
    out = vault.complete_dsn_password(
        "postgresql://agent_rw:@db.example.com:6432/precis_prod"
    )
    assert out == "postgresql://agent_rw:filled@db.example.com:6432/precis_prod"
