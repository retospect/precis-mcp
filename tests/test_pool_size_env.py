"""Pool size is settable from the environment.

One long-lived MCP process serving every session on a machine has to be
sized against the fleet of sessions, and the thing that knows that number
is a wrapper script, not this repo. Previously the sizes were constants
with a kwarg override only, so the only way to change them was a code edit.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import pool as pool_mod


def test_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pool_mod.POOL_MIN_SIZE_ENV, raising=False)
    monkeypatch.delenv(pool_mod.POOL_MAX_SIZE_ENV, raising=False)
    assert pool_mod.resolved_pool_min_size() == pool_mod.DEFAULT_POOL_MIN_SIZE
    assert pool_mod.resolved_pool_max_size() == pool_mod.DEFAULT_POOL_MAX_SIZE


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pool_mod.POOL_MIN_SIZE_ENV, "4")
    monkeypatch.setenv(pool_mod.POOL_MAX_SIZE_ENV, "16")
    assert pool_mod.resolved_pool_min_size() == 4
    assert pool_mod.resolved_pool_max_size() == 16


@pytest.mark.parametrize("bad", ["", "  ", "nope", "0", "-3", "1.5"])
def test_bad_values_fall_back_rather_than_refusing_to_boot(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A typo in a wrapper script must not be the thing that stops the
    server starting — it degrades to the compiled-in default."""
    monkeypatch.setenv(pool_mod.POOL_MAX_SIZE_ENV, bad)
    assert pool_mod.resolved_pool_max_size() == pool_mod.DEFAULT_POOL_MAX_SIZE


def _captured_pool_sizes(
    monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[int, int]:
    """Call ``create_pool`` with ``ConnectionPool`` stubbed; return sizes."""
    seen: dict[str, Any] = {}

    class _FakePool:
        def __init__(self, **init_kwargs: Any) -> None:
            seen.update(init_kwargs)

        def open(self, **_: Any) -> None:
            return None

        @staticmethod
        def check_connection(_conn: Any) -> None:  # pragma: no cover — unused
            return None

    monkeypatch.setattr(pool_mod, "ConnectionPool", _FakePool)
    pool_mod.create_pool("postgresql:///nope", **kwargs)
    return seen["min_size"], seen["max_size"]


def test_create_pool_applies_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pool_mod.POOL_MIN_SIZE_ENV, "4")
    monkeypatch.setenv(pool_mod.POOL_MAX_SIZE_ENV, "16")
    assert _captured_pool_sizes(monkeypatch) == (4, 16)


def test_explicit_arguments_beat_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller sizing a pool for its own reasons (a test, a one-off
    script) must not be silently resized by a machine-wide env var."""
    monkeypatch.setenv(pool_mod.POOL_MIN_SIZE_ENV, "4")
    monkeypatch.setenv(pool_mod.POOL_MAX_SIZE_ENV, "16")
    assert _captured_pool_sizes(monkeypatch, min_size=1, max_size=2) == (1, 2)


def test_store_connect_does_not_swallow_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Store.connect`` used to substitute the module constants for
    ``None`` before calling ``create_pool``, which would defeat the env
    override at that seam while leaving direct callers working."""
    from precis.store import store as store_mod

    seen: dict[str, Any] = {}

    def _fake_create_pool(dsn: str, **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(store_mod, "create_pool", _fake_create_pool)
    monkeypatch.setattr(store_mod.Store, "__init__", lambda self, *a, **k: None)
    store_mod.Store.connect("postgresql:///nope")
    assert seen == {"min_size": None, "max_size": None}
