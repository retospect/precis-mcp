"""anki_sync job_type (gr747312) — registration, caller binding, submit gate,
atomic rate limit, backoff, and the dispatch against a fake DispatchContext
(no DB, no AnkiWeb)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.anki.sync import AnkiNotInstalled, AnkiSyncError
from precis.errors import BadInput
from precis.workers.anki_sync import AnkiSyncMisconfigured
from precis.workers.job_types import anki_sync as job
from precis.workers.job_types import get_job_type, known_job_types


class _FakeResult:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConn:
    """Emulates the cooldown and backoff writes and an empty retired-ref list."""

    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    def execute(self, sql: str, params: tuple[Any, ...]) -> _FakeResult:
        settings = self._store.settings
        if "WHERE app_state.value <=" in sql:
            key, value, cutoff = params
            if key not in settings or str(settings[key]) <= str(cutoff):
                settings[key] = str(value)
                return _FakeResult(row=(key,))
        elif "DO NOTHING" in sql:
            settings.setdefault(params[0], "")
        elif "ON CONFLICT" in sql:
            settings[params[0]] = params[1]
        elif "SELECT value FROM app_state" in sql and params[0] in settings:
            return _FakeResult(row=(settings[params[0]],))
        return _FakeResult()

    def transaction(self) -> _FakeConn:
        return self

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    def connection(self) -> _FakeConn:
        return _FakeConn(self._store)


class _FakeStore:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.dsn: str | None = "fake-dsn"
        self.claim_calls: list[tuple[str, str]] = []
        self.pool = _FakePool(self)

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def claim_unowned_refs(self, kind: str, login: str) -> int:
        self.claim_calls.append((kind, login))
        return 0

    def list_refs(self, **kw: Any) -> list[Any]:
        return []


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    chunks: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The submit door stamps the verified owner; default to a bound job.
        user = (self.meta.get("params") or {}).get("user")
        if user and "owner_login" not in self.meta:
            self.meta = {**self.meta, "owner_login": user}

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Credentials configured, anki enabled, caller "reto";
    ``run_anki_sync`` records calls."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("precis.anki.creds.user_anki_configured", lambda s, lg: True)
    monkeypatch.setattr(
        "precis.config.load_config", lambda: SimpleNamespace(anki_enabled=True)
    )
    monkeypatch.setattr("precis.profiles.acting_login", lambda s: "reto")

    def fake_run(store: Any, cfg: Any, **kw: Any) -> str:
        calls.append(kw)
        return "anki-sync[reto]: ok"

    monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", fake_run)
    return calls


def test_registered() -> None:
    spec = get_job_type("anki_sync")
    assert spec is not None
    assert "anki_sync" in known_job_types()
    assert spec.dispatch is not None
    assert spec.conditional_requires is not None
    assert spec.conditional_requires({"user": "reto"}) == {"anki_sync"}


def test_probe_anki_sync_requires_a_configured_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.capability_probe import _probe_anki_sync

    cfg = SimpleNamespace(anki_enabled=True, anki_mirror_dir=None)
    monkeypatch.setattr("precis.config.load_config", lambda: cfg)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert _probe_anki_sync() == 0
    cfg.anki_mirror_dir = str(tmp_path)
    assert _probe_anki_sync() == 1


def test_validate_requires_user_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.profiles.acting_login", lambda s: "reto")
    store: Any = _FakeStore()
    monkeypatch.setattr("precis.anki.creds.user_anki_configured", lambda s, lg: False)
    with pytest.raises(BadInput, match="no AnkiWeb credentials"):
        job.validate(store, {"user": "reto"})


def test_validate_binds_target_to_acting_login(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An identified caller can only ever sync their own account — a
    mismatch is refused, an omission binds to the caller."""
    monkeypatch.setattr("precis.profiles.acting_login", lambda s: "reto")
    store: Any = _FakeStore()
    with pytest.raises(BadInput, match="not your login"):
        job.validate(store, {"user": "ann"})
    params: dict[str, Any] = {}
    job.validate(store, params)
    assert params["user"] == "reto"  # bound target persists into meta.params
    job.validate(store, {"user": "reto"})


def test_validate_refuses_unidentified_caller(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.profiles.acting_login", lambda s: None)
    store: Any = _FakeStore()
    with pytest.raises(BadInput, match="identified caller"):
        job.validate(store, {"user": "ann"})


def test_dispatch_refuses_unbound_job(armed: list[dict[str, Any]]) -> None:
    """A todo-minted job carries params.user but no stamped owner."""
    store: Any = _FakeStore()
    ctx = _FakeCtx(store, {"params": {"user": "ann"}, "owner_login": ""})
    job._dispatch(ctx, None)
    assert "owner binding" in ctx.failures[0]
    assert armed == []
    ctx2 = _FakeCtx(store, {"params": {"user": "ann"}, "owner_login": "reto"})
    job._dispatch(ctx2, None)
    assert "owner binding" in ctx2.failures[0]
    assert armed == []


def test_dispatch_success_leaves_shared_backoff(armed: list[dict[str, Any]]) -> None:
    store: Any = _FakeStore()
    until = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    store.set_setting("anki_sync:next_attempt_at", until)
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert not ctx.failures
    assert store.get_setting("anki_sync:next_attempt_at") == until


def test_validate_rate_limit_and_backoff(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    who = {"login": "reto"}
    monkeypatch.setattr("precis.profiles.acting_login", lambda s: who["login"])
    store: Any = _FakeStore()
    job.validate(store, {"user": "reto"})
    store.set_setting(job._stamp_key("reto"), datetime.now(UTC).isoformat())
    with pytest.raises(BadInput, match="retry in"):
        job.validate(store, {"user": "reto"})
    # Another user and a dry run are unaffected.
    who["login"] = "ann"
    job.validate(store, {"user": "ann"})
    who["login"] = "reto"
    job.validate(store, {"user": "reto", "dry_run": True})
    store.settings[job._stamp_key("reto")] = (
        datetime.now(UTC) - timedelta(seconds=job.MIN_INTERVAL_S + 5)
    ).isoformat()
    job.validate(store, {"user": "reto"})
    store.set_setting(
        "anki_sync:next_attempt_at",
        (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    )
    with pytest.raises(BadInput, match="backing off"):
        job.validate(store, {"user": "reto"})


def test_dispatch_syncs_only_that_login_and_stamps(
    armed: list[dict[str, Any]],
) -> None:
    store: Any = _FakeStore()
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert not ctx.failures
    assert armed == [{"dry_run": False, "login": "reto", "honor_cfg_flags": False}]
    assert ctx.chunks == [("job_summary", "anki-sync[reto]: ok")]
    assert store.get_setting(job._stamp_key("reto"))
    # A second run inside the window is refused without touching AnkiWeb.
    ctx2 = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx2, None)
    assert len(armed) == 1
    assert "retry in" in ctx2.failures[0]


def test_dispatch_lost_cooldown_claim_is_refused(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent runs can both pass the read check; the atomic claim
    is what refuses the loser — it must not reach AnkiWeb."""
    monkeypatch.setattr(job, "_refusal", lambda store, login: None)
    store: Any = _FakeStore()
    store.set_setting(job._stamp_key("reto"), datetime.now(UTC).isoformat())
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert len(armed) == 0
    assert "cooldown" in ctx.failures[0]


def test_dispatch_failure_widens_backoff(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(store: Any, cfg: Any, **kw: Any) -> str:
        raise AnkiSyncError("429")

    monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", boom)
    store: Any = _FakeStore()
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert "sync failed: 429" in ctx.failures[0]
    assert store.get_setting("anki_sync:next_attempt_at")
    assert store.get_setting("anki_sync:fail_count") == "1"


def test_dispatch_unexpected_error_widens_backoff(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare error escaping run_anki_sync entirely still counts against
    the shared backoff — a manual trigger can't hammer a sick AnkiWeb."""

    def boom(store: Any, cfg: Any, **kw: Any) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", boom)
    store: Any = _FakeStore()
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert "sync failed: connection reset" in ctx.failures[0]
    assert store.get_setting("anki_sync:fail_count") == "1"


def test_dispatch_busy_leaves_shared_backoff_alone(
    armed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock-busy means nothing synced: the job fails its own request but
    neither clears nor widens the cadence's shared backoff."""
    from precis.workers.anki_sync import AnkiSyncBusy

    def busy(store: Any, cfg: Any, **kw: Any) -> str:
        raise AnkiSyncBusy("anki-sync[reto]: another sync holds the lock; skipping.")

    monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", busy)
    store: Any = _FakeStore()
    store.set_setting("anki_sync:fail_count", "2")
    until = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    store.set_setting("anki_sync:next_attempt_at", until)
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert "holds the lock" in ctx.failures[0]
    assert store.get_setting("anki_sync:fail_count") == "2"
    assert store.get_setting("anki_sync:next_attempt_at") == until
    # The cooldown stamp was still claimed — a failed run counts too.
    assert store.get_setting(job._stamp_key("reto"))


@pytest.mark.parametrize("local_error", [AnkiSyncMisconfigured, AnkiNotInstalled])
def test_dispatch_local_failure_keeps_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    local_error: type[AnkiSyncError],
) -> None:
    core = _arm_core(monkeypatch, ["reto"])
    monkeypatch.setattr(
        "precis.config.load_config", lambda: _core_cfg(tmp_path, anki_enabled=True)
    )

    def boom(*args: Any, **kwargs: Any) -> str:
        raise local_error("local setup incomplete")

    monkeypatch.setattr(core, "_sync_one_user", boom)
    store: Any = _FakeStore()
    ctx = _FakeCtx(store, {"params": {"user": "reto"}})
    job._dispatch(ctx, None)
    assert "local setup incomplete" in ctx.failures[0]
    assert store.get_setting("anki_sync:fail_count") is None


@pytest.mark.parametrize("dry_run", ["false", 0, 1, None])
def test_rejects_non_boolean_dry_run(
    armed: list[dict[str, Any]], dry_run: object
) -> None:
    store: Any = _FakeStore()
    params = {"user": "reto", "dry_run": dry_run}
    with pytest.raises(BadInput, match="dry_run must be a boolean"):
        job.validate(store, params)
    ctx = _FakeCtx(store, {"params": params})
    job._dispatch(ctx, None)
    assert "dry_run must be a boolean" in ctx.failures[0]
    assert armed == []


def test_dispatch_dry_run_skips_rate_limit(armed: list[dict[str, Any]]) -> None:
    store: Any = _FakeStore()
    store.set_setting(job._stamp_key("reto"), datetime.now(UTC).isoformat())
    ctx = _FakeCtx(store, {"params": {"user": "reto", "dry_run": True}})
    job._dispatch(ctx, None)
    assert not ctx.failures
    assert armed == [{"dry_run": True, "login": "reto", "honor_cfg_flags": False}]


# ── the shared core (workers/anki_sync.py) paths the review asked to be
# covered: the busy signal, the env-flag scope, the dry-run claim, and the
# raw-AnkiWeb-error fold. ────────────────────────────────────────────────


def _core_cfg(tmp_path: Any, **overrides: Any) -> Any:
    return SimpleNamespace(
        anki_mirror_dir=str(tmp_path),
        anki_deck="Precis",
        anki_fix_enabled=True,
        anki_project_enabled=True,
        **overrides,
    )


def _arm_core(monkeypatch: pytest.MonkeyPatch, logins: list[str]) -> Any:
    """Stub the roster + credentials so ``run_anki_sync`` reaches the
    per-user loop; returns the core module."""
    import precis.workers.anki_sync as core

    monkeypatch.setattr("precis.anki.creds.anki_logins", lambda s: list(logins))
    monkeypatch.setattr("precis.anki.creds.user_anki_configured", lambda s, lg: True)
    return core


def test_core_all_busy_raises_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    core = _arm_core(monkeypatch, ["reto"])
    monkeypatch.setattr(
        core,
        "_sync_one_user",
        lambda *a, **kw: (_ for _ in ()).throw(
            core.AnkiSyncBusy("anki-sync[reto]: another sync holds the lock; skipping.")
        ),
    )
    with pytest.raises(core.AnkiSyncBusy, match="holds the lock"):
        core.run_anki_sync(_FakeStore(), _core_cfg(tmp_path), login="reto")
    assert issubclass(core.AnkiSyncBusy, AnkiSyncError)


def test_core_partial_busy_returns_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    core = _arm_core(monkeypatch, ["ann", "reto"])

    def one_busy(store: Any, cfg: Any, login: str, root: Any, **kw: Any) -> str:
        if login == "ann":
            raise core.AnkiSyncBusy(
                "anki-sync[ann]: another sync holds the lock; skipping."
            )
        return f"anki-sync[{login}]: synced: ok"

    monkeypatch.setattr(core, "_sync_one_user", one_busy)
    summary = core.run_anki_sync(_FakeStore(), _core_cfg(tmp_path))
    assert "ann]: another sync holds the lock" in summary
    assert "reto]: synced: ok" in summary


def test_core_raw_ankiweb_error_folds_into_sync_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``sync_login``/``sync_collection`` exceptions are not AnkiSyncError;
    the per-user fold keeps them inside the caller's backoff catch and off
    the other users' fan-out."""
    core = _arm_core(monkeypatch, ["ann", "reto"])

    def boom(store: Any, cfg: Any, login: str, root: Any, **kw: Any) -> str:
        if login == "ann":
            raise RuntimeError("AnkiWeb error: 429 too many requests")
        return f"anki-sync[{login}]: synced: ok"

    monkeypatch.setattr(core, "_sync_one_user", boom)
    with pytest.raises(AnkiSyncError) as ei:
        core.run_anki_sync(_FakeStore(), _core_cfg(tmp_path))
    assert "429" in str(ei.value)
    assert "reto]: synced: ok" in str(ei.value)


def test_core_honor_cfg_flags_scopes_the_manual_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The cadence/CLI inherit env fix+project flags; the on-demand job's
    documented scope does not."""
    core = _arm_core(monkeypatch, ["reto"])
    seen: list[dict[str, Any]] = []

    def capture(*a: Any, **kw: Any) -> str:
        seen.append(kw)
        return "ok"

    monkeypatch.setattr(core, "_sync_one_user", capture)
    cfg = _core_cfg(tmp_path)
    core.run_anki_sync(_FakeStore(), cfg, login="reto")
    assert seen[-1]["fix"] is True and seen[-1]["project"] is True
    core.run_anki_sync(_FakeStore(), cfg, login="reto", honor_cfg_flags=False)
    assert seen[-1]["fix"] is False and seen[-1]["project"] is False


def test_core_dry_run_never_claims_unowned_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A listing pass counts cards; it must not mutate ref ownership."""
    core = _arm_core(monkeypatch, ["reto"])
    store = _FakeStore()
    summary = core.run_anki_sync(store, _core_cfg(tmp_path), login="reto", dry_run=True)
    assert "[DRY-RUN]" in summary
    assert store.claim_calls == []


def test_core_lock_busy_leaves_refs_unclaimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A busy run does nothing at all — including the unowned-refs claim."""
    import contextlib

    core = _arm_core(monkeypatch, ["reto"])

    @contextlib.contextmanager
    def no_lock(*a: Any, **kw: Any):
        yield False

    monkeypatch.setattr(core, "try_xact_advisory_lock", no_lock)
    store = _FakeStore()
    with pytest.raises(core.AnkiSyncBusy):
        core.run_anki_sync(store, _core_cfg(tmp_path), login="reto")
    assert store.claim_calls == []
