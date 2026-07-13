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
