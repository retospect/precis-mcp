"""Tests for the version-gated oracle re-ingest module.

These tests exercise the version + sha256 gate machinery against a
fake store. The actual ingest call is mocked — that path is covered
by ``test_ingest_oracles.py``. Here we only care that the gate makes
the right decision in each branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.jobs import oracle_sync
from precis.jobs.oracle_sync import (
    compute_corpus_state,
    maybe_reingest,
    wheel_version_int,
)

# ── version packing ──────────────────────────────────────────────────


def test_wheel_version_int_simple() -> None:
    assert wheel_version_int("6.0.0") == 6 * 10**12
    assert wheel_version_int("6.1.2") == 6 * 10**12 + 1 * 10**6 + 2
    assert wheel_version_int("0.0.1") == 1


def test_wheel_version_int_strips_prerelease() -> None:
    """``6.0.0a0`` -> patch becomes 0; alphas sort just below the release."""
    base = wheel_version_int("6.0.0")
    assert wheel_version_int("6.0.0a0") == base
    assert wheel_version_int("6.0.0rc1") == base
    assert wheel_version_int("6.0.0.dev3") == base


def test_wheel_version_int_handles_garbage() -> None:
    """Bad parses return 0 (failsafe — older than any real version)."""
    assert wheel_version_int("not-a-version") == 0


def test_wheel_version_int_empty_uses_package_default() -> None:
    """Empty input falls back to ``precis.__version__`` so callers
    that omit the arg get the wheel version, not 0."""
    import precis as _precis_pkg

    expected = wheel_version_int(getattr(_precis_pkg, "__version__", "0.0.0"))
    assert wheel_version_int("") == expected
    assert wheel_version_int(None) == expected


def test_wheel_version_int_orders_correctly() -> None:
    """Sanity: lexicographic-vs-numeric collisions don't bite."""
    assert wheel_version_int("6.10.0") > wheel_version_int("6.9.99")
    assert wheel_version_int("7.0.0") > wheel_version_int("6.99.99")


# ── corpus hashing ───────────────────────────────────────────────────


def _write_yaml(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body, encoding="utf-8")


def test_compute_corpus_state_picks_up_yaml(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "stoic.yaml", "slug: stoic\ntitle: Stoic\n")
    _write_yaml(tmp_path, "zen.yaml", "slug: zen\ntitle: Zen\n")
    state = compute_corpus_state(tmp_path)
    assert state is not None
    assert len(state.sha256) == 64
    assert state.version > 0


def test_compute_corpus_state_changes_on_edit(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "stoic.yaml", "slug: stoic\n")
    s1 = compute_corpus_state(tmp_path)
    assert s1 is not None
    _write_yaml(tmp_path, "stoic.yaml", "slug: stoic\ntitle: Stoic\n")
    s2 = compute_corpus_state(tmp_path)
    assert s2 is not None
    assert s1.sha256 != s2.sha256


def test_compute_corpus_state_returns_none_when_empty(tmp_path: Path) -> None:
    assert compute_corpus_state(tmp_path) is None


def test_compute_corpus_state_filename_matters(tmp_path: Path) -> None:
    """Renaming a file invalidates the hash even if content is identical."""
    _write_yaml(tmp_path, "a.yaml", "x: 1\n")
    s1 = compute_corpus_state(tmp_path)
    (tmp_path / "a.yaml").rename(tmp_path / "b.yaml")
    s2 = compute_corpus_state(tmp_path)
    assert s1 is not None and s2 is not None
    assert s1.sha256 != s2.sha256


# ── fake store + maybe_reingest gate ─────────────────────────────────


class FakeStore:
    """Just enough store surface to exercise the gate.

    Tracks ``set_setting`` / ``get_setting`` calls and lets the test
    pre-seed stored state. The advisory lock helpers use ``pool``,
    which we leave ``None`` so the locking degrades gracefully (the
    docstring says that's the intended behaviour for non-Postgres
    fixtures).

    The method names mirror the real ``Store`` API exactly — if
    they drift, ``test_oracle_sync_uses_real_store_api`` below trips
    so the gate can never silently fall back to "reingest every
    boot" again.
    """

    pool = None

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self.ingest_calls = 0

    def get_setting(self, key: str) -> str | None:
        return self._kv.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self._kv[key] = value


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def src_dir(tmp_path: Path) -> Path:
    """Minimal valid YAML directory with one tradition."""
    body = (
        "slug: stoic\n"
        "title: Stoic\n"
        "description: Stoic philosophy\n"
        "tags: [philosophy]\n"
        "entries:\n"
        "  - title: Memento mori\n"
        "    body: Remember you must die.\n"
    )
    (tmp_path / "stoic.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def _stub_ingest_returning(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> dict[str, Any]:
    """Replace ``ingest_directory`` with a counter-bumping stub.

    Returned dict tracks number of invocations so the gate's
    behaviour can be asserted directly.
    """
    calls: dict[str, Any] = {"count": 0}

    def fake_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        store.ingest_calls += 1
        return {
            "files": 1,
            "created": 0,
            "replaced": 1,
            "chunks": 1,
            "skipped": 0,
            "errors": 0,
            "per_file": {},
        }

    monkeypatch.setattr(oracle_sync, "ingest_directory", fake_ingest)
    return calls


def test_no_store_short_circuits() -> None:
    out = maybe_reingest(store=None, embedder=None)
    assert out["status"] == "no_store"


def test_no_data_when_empty_dir(fake_store: FakeStore, tmp_path: Path) -> None:
    out = maybe_reingest(store=fake_store, embedder=None, src_dir=tmp_path)
    assert out["status"] == "no_data"


def test_first_boot_ingests(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty system table → ingest unconditionally."""
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir)
    assert out["status"] == "ingested"
    assert calls["count"] == 1
    # State was persisted.
    assert fake_store._kv["corpus.oracle.version"] != "0"
    assert fake_store._kv["corpus.oracle.sha256"]


def test_up_to_date_skips_ingest(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same version + same sha → no ingest call, status=up_to_date."""
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    state = compute_corpus_state(src_dir)
    assert state is not None
    fake_store.set_setting("corpus.oracle.version", str(state.version))
    fake_store.set_setting("corpus.oracle.sha256", state.sha256)

    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir)
    assert out["status"] == "up_to_date"
    assert calls["count"] == 0


def test_older_local_skips_ingest(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local version < stored → silently skip, no stomp."""
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    state = compute_corpus_state(src_dir)
    assert state is not None
    # Pretend the DB has v9999 (newer than our wheel).
    fake_store.set_setting("corpus.oracle.version", "999999999999999")
    fake_store.set_setting("corpus.oracle.sha256", "different-sha")

    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir)
    assert out["status"] == "older_local"
    assert calls["count"] == 0
    # Stored state untouched.
    assert fake_store._kv["corpus.oracle.version"] == "999999999999999"


def test_same_version_different_sha_ingests(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same version but different sha → re-ingest (operator hand-edit case)."""
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    state = compute_corpus_state(src_dir)
    assert state is not None
    fake_store.set_setting("corpus.oracle.version", str(state.version))
    fake_store.set_setting("corpus.oracle.sha256", "old-sha-from-prior-build")

    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir)
    assert out["status"] == "ingested"
    assert calls["count"] == 1
    assert fake_store._kv["corpus.oracle.sha256"] == state.sha256


def test_force_bypasses_both_gates(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` ingests even when version+sha both match."""
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    state = compute_corpus_state(src_dir)
    assert state is not None
    fake_store.set_setting("corpus.oracle.version", str(state.version))
    fake_store.set_setting("corpus.oracle.sha256", state.sha256)

    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir, force=True)
    assert out["status"] == "ingested"
    assert calls["count"] == 1


def test_force_overrides_older_local(
    fake_store: FakeStore,
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` even ingests over a newer stored state.

    The intentional foot-gun: forcing a downgrade. That's what the
    ``--force`` flag exists for in the first place; the boot-time
    gate is what protects you from it.
    """
    calls = _stub_ingest_returning(monkeypatch, fake_store)
    fake_store.set_setting("corpus.oracle.version", "999999999999999")
    fake_store.set_setting("corpus.oracle.sha256", "different-sha")

    out = maybe_reingest(store=fake_store, embedder=None, src_dir=src_dir, force=True)
    assert out["status"] == "ingested"
    assert calls["count"] == 1


# ── env-var disable ──────────────────────────────────────────────────


def test_disabled_by_env_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_ORACLE_AUTO_REINGEST", raising=False)
    assert oracle_sync.is_disabled_by_env() is False


@pytest.mark.parametrize("val", ["0", "false", "False", "no", "off", ""])
def test_disabled_by_env_true_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("PRECIS_ORACLE_AUTO_REINGEST", val)
    assert oracle_sync.is_disabled_by_env() is True


def test_disabled_by_env_one_means_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_ORACLE_AUTO_REINGEST", "1")
    assert oracle_sync.is_disabled_by_env() is False


# ── API-surface regression ───────────────────────────────────────────


def test_oracle_sync_uses_real_store_api() -> None:
    """Pin the Store method names ``oracle_sync`` depends on.

    Regression for the silent ~10 s every-boot reingest: previously
    the gate called ``store.get_system`` / ``store.set_system``,
    which never existed on the real :class:`precis.store.Store`.
    Every boot the read failed, was treated as "never ingested",
    and the corpus was re-embedded from scratch — burning the
    bge-m3 model load + 9 batches per process start.

    If the Store API is renamed in the future, this test trips
    before the gate can silently fall back to reingest.
    """
    from precis.store import Store

    assert hasattr(Store, "get_setting"), (
        "oracle_sync.maybe_reingest depends on Store.get_setting; "
        "renaming it without updating oracle_sync re-introduces the "
        "every-boot reingest bug."
    )
    assert hasattr(Store, "set_setting"), (
        "oracle_sync.maybe_reingest depends on Store.set_setting."
    )


def test_read_state_reraises_attributeerror_loudly(
    src_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store stub missing ``get_setting`` must raise, not silently reingest."""
    calls = {"count": 0}

    def fake_ingest(*_: Any, **__: Any) -> dict[str, Any]:
        calls["count"] += 1
        return {}

    monkeypatch.setattr(oracle_sync, "ingest_directory", fake_ingest)

    class BrokenStore:
        pool = None

    with pytest.raises(AttributeError):
        maybe_reingest(store=BrokenStore(), embedder=None, src_dir=src_dir)
    assert calls["count"] == 0
