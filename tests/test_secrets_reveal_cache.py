"""The version-checked reveal cache in :mod:`precis.secrets` — no DB required.

A fake vault answers ``vault.list()`` and ``vault.reveal()`` and counts the
reveals (each one is a decrypt plus a ``vault.events`` row); a fake clock
drives simulated days. The budget under test: a hot ``get_secret`` loop
decrypts at most once per max age per process, while a rotation or delete
made by another process still propagates within the 60 s window.
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis import secrets as vault

FAKE_VALUE = "fake-value-for-tests-only"


class _FakeVault:
    """``name -> (value, updated_at)``, shared like the real table is."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, int]] = {}
        self.reveals = 0
        self.lists = 0
        self.list_fails = False
        self.reveal_delay = 0.0
        self.on_reveal: Any = None

    def set(self, name: str, value: str) -> None:
        old = self.rows.get(name)
        self.rows[name] = (value, (old[1] if old else 0) + 1)


class _Conn:
    def __init__(self, fake: _FakeVault) -> None:
        self._fake = fake

    def execute(self, sql: str, params: Any = ()) -> Any:
        fake = self._fake
        if "vault.list()" in sql:
            fake.lists += 1
            if fake.list_fails:
                raise RuntimeError("list unavailable")
            rows = [(n, v[1]) for n, v in fake.rows.items()]
            return SimpleNamespace(fetchall=lambda: rows)
        if "vault.reveal" in sql:
            fake.reveals += 1
            if fake.reveal_delay:
                time.sleep(fake.reveal_delay)
            row = fake.rows.get(params[0])
            if fake.on_reveal is not None:
                fake.on_reveal()
            return SimpleNamespace(fetchone=lambda: (row[0] if row else None,))
        raise AssertionError(f"unexpected SQL: {sql}")


class _Pool:
    def __init__(self, fake: _FakeVault) -> None:
        self._fake = fake

    @contextmanager
    def connection(self) -> Any:
        yield _Conn(self._fake)


class _Store:
    def __init__(self, fake: _FakeVault) -> None:
        self.pool = _Pool(fake)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("HOT_KEY", raising=False)
    vault.bind_store(None)
    vault.invalidate()
    vault._warned.clear()
    yield
    vault.bind_store(None)
    vault.invalidate()


@pytest.fixture()
def fake() -> _FakeVault:
    f = _FakeVault()
    f.set("HOT_KEY", FAKE_VALUE)
    return f


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(vault, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


def simulate(
    secrets_module: Any,
    fake: _FakeVault,
    clock: _Clock,
    *,
    seconds: int,
    every: float = 1.0,
    name: str = "HOT_KEY",
) -> list[str | None]:
    """One process calling ``get_secret(name)`` every ``every`` seconds."""
    store = cast(Any, _Store(fake))
    seen: list[str | None] = []
    for _ in range(int(seconds / every)):
        seen.append(secrets_module.get_secret(name, store=store))
        clock.t += every
    return seen


DAY = 86_400


def test_hot_loop_reveals_once_per_max_age(fake: _FakeVault, clock: _Clock) -> None:
    seen = simulate(vault, fake, clock, seconds=DAY)
    assert set(seen) == {FAKE_VALUE}
    assert fake.reveals <= DAY / vault._CACHE_MAX_AGE_SECONDS + 1
    # One version check per window, never per call.
    assert fake.lists <= DAY / vault._CACHE_TTL_SECONDS + 1


def test_hot_loop_reveals_at_most_once_per_ttl(fake: _FakeVault, clock: _Clock) -> None:
    """The backlog item's own acceptance line."""
    simulate(vault, fake, clock, seconds=600, every=0.01)
    assert fake.reveals <= 600 / vault._CACHE_TTL_SECONDS


def test_rotation_elsewhere_propagates_within_one_window(
    fake: _FakeVault, clock: _Clock
) -> None:
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    clock.t += 5
    fake.set("HOT_KEY", "rotated-fake-value")  # another process rotates
    clock.t += vault._CACHE_TTL_SECONDS
    assert vault.get_secret("HOT_KEY", store=store) == "rotated-fake-value"
    assert fake.reveals == 2


def test_delete_elsewhere_stops_serving_within_one_window(
    fake: _FakeVault, clock: _Clock
) -> None:
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    del fake.rows["HOT_KEY"]
    clock.t += vault._CACHE_TTL_SECONDS
    assert vault.get_secret("HOT_KEY", store=store, default="d") == "d"
    assert "HOT_KEY" not in vault._cache


def test_rotation_racing_the_decrypt_is_not_served_stale(
    fake: _FakeVault, clock: _Clock
) -> None:
    """The version is read before the decrypt: a rotation in between leaves
    an entry stamped older than its value, which costs one extra reveal at
    the next window — never a value older than its stamp."""
    store = cast(Any, _Store(fake))
    fake.on_reveal = lambda: fake.set("HOT_KEY", "rotated-fake-value")
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    fake.on_reveal = None
    clock.t += vault._CACHE_TTL_SECONDS
    assert vault.get_secret("HOT_KEY", store=store) == "rotated-fake-value"
    clock.t += vault._CACHE_TTL_SECONDS
    assert vault.get_secret("HOT_KEY", store=store) == "rotated-fake-value"
    assert fake.reveals == 2


def test_local_set_secret_is_visible_immediately(
    fake: _FakeVault, clock: _Clock
) -> None:
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    fake.set("HOT_KEY", "rotated-fake-value")
    vault.invalidate("HOT_KEY")  # what set_secret does after its write
    assert vault.get_secret("HOT_KEY", store=store) == "rotated-fake-value"


def test_unlistable_vault_falls_back_to_the_plain_window(
    fake: _FakeVault, clock: _Clock
) -> None:
    fake.list_fails = True
    seen = simulate(vault, fake, clock, seconds=600)
    assert set(seen) == {FAKE_VALUE}
    assert fake.reveals == 600 / vault._CACHE_TTL_SECONDS


def test_concurrent_misses_share_one_decrypt(fake: _FakeVault) -> None:
    store = cast(Any, _Store(fake))
    fake.reveal_delay = 0.05
    start = threading.Barrier(16)
    out: list[str | None] = []

    def worker() -> None:
        start.wait()
        out.append(vault.get_secret("HOT_KEY", store=store))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == [FAKE_VALUE] * 16
    assert fake.reveals == 1


def test_cached_value_stays_out_of_reprs_pickles_and_logs(
    fake: _FakeVault, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="precis.secrets")
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    fake.list_fails = True
    clock.t += vault._CACHE_TTL_SECONDS
    vault.get_secret("HOT_KEY", store=store)
    entry = vault._cache["HOT_KEY"]
    assert FAKE_VALUE not in repr(entry)
    assert FAKE_VALUE not in repr(vault._cache)
    assert FAKE_VALUE not in str(vars(vault).get("_cache"))
    with pytest.raises(TypeError) as err:
        pickle.dumps(entry)
    assert FAKE_VALUE not in str(err.value)
    assert FAKE_VALUE not in caplog.text
    assert not hasattr(entry, "__dict__")


def test_rotation_after_a_shared_snapshot_is_seen_within_one_window(
    fake: _FakeVault, clock: _Clock
) -> None:
    """One ``vault.list()`` snapshot serves every name for a window. Extending
    an entry from *now* instead of from the snapshot's age would stretch a
    rotation's reach to nearly two windows (review of 64e67d992)."""
    fake.set("OTHER_KEY", "other-fake-value")
    store = cast(Any, _Store(fake))
    ttl = vault._CACHE_TTL_SECONDS
    t0 = clock.t
    assert vault.get_secret("OTHER_KEY", store=store) == "other-fake-value"
    clock.t = t0 + ttl / 2
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    clock.t = t0 + ttl  # OTHER_KEY rechecks: a fresh snapshot is taken now
    assert vault.get_secret("OTHER_KEY", store=store) == "other-fake-value"
    rotated_at = t0 + ttl + 1
    clock.t = rotated_at
    fake.set("HOT_KEY", "rotated-fake-value")
    clock.t = t0 + ttl * 1.5  # HOT_KEY rechecks against the pre-rotation snapshot
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    clock.t = rotated_at + ttl
    assert vault.get_secret("HOT_KEY", store=store) == "rotated-fake-value"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only os.fork()")
def test_fork_child_gets_fresh_locks_and_no_inherited_plaintext(
    fake: _FakeVault,
) -> None:
    """A lock held by another thread at fork time stays held in the child;
    the at-fork hook rebinds it and drops the inherited cache."""
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    held = vault._cache_lock
    held.acquire()
    try:
        vault._reset_after_fork()
    finally:
        held.release()
    assert vault._cache_lock is not held
    assert vault._cache == {}
    assert vault._versions_at is None
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    assert fake.reveals == 2


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_forked_child_does_not_deadlock_on_a_held_lock(fake: _FakeVault) -> None:
    store = cast(Any, _Store(fake))
    assert vault.get_secret("HOT_KEY", store=store) == FAKE_VALUE
    with vault._cache_lock:
        pid = os.fork()
        if pid == 0:  # child: the parent's lock is held; ours must not be
            code = 0
            try:
                code = 0 if not vault._cache else 2
                if vault.get_secret("HOT_KEY", store=store) != FAKE_VALUE:
                    code = 3
            except BaseException:
                code = 4
            os._exit(code)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.01)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("forked child deadlocked on the inherited cache lock")
    assert os.waitstatus_to_exitcode(status) == 0


def test_waiters_queued_on_a_miss_share_it(fake: _FakeVault) -> None:
    """A miss is not cached, but callers already queued on the fill that
    missed take its answer rather than re-revealing one after another."""
    store = cast(Any, _Store(fake))
    fake.reveal_delay = 0.05
    start = threading.Barrier(16)
    out: list[str | None] = []

    def worker() -> None:
        start.wait()
        out.append(vault.get_secret("ABSENT_KEY", store=store))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == [None] * 16
    assert fake.reveals == 1
    # A later caller still asks: a freshly-set secret shows at once.
    fake.set("ABSENT_KEY", "new-fake-value")
    assert vault.get_secret("ABSENT_KEY", store=store) == "new-fake-value"


def test_fill_locks_stay_bounded_across_many_names(fake: _FakeVault) -> None:
    store = cast(Any, _Store(fake))
    for i in range(500):
        vault.get_secret(f"NAME_{i}", store=store)
    assert len(vault._fill_locks) == vault._FILL_STRIPES
    assert len(vault._fill_miss) <= vault._MISS_RECORDS_MAX
