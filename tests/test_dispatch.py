"""Tests for ``precis.dispatch`` — the seven-verb registry + boot.

These tests exercise only the registration machinery and the boot
loop's failure semantics; they do not depend on any real handler
being ported yet.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from precis.dispatch import (
    PLUGIN_GROUP,
    DuplicateRegistration,
    Hub,
    InitError,
    _try,
    boot,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store

# ---------------------------------------------------------------------------
# Hub primitives
# ---------------------------------------------------------------------------


def test_register_ability_records_key_and_callable() -> None:
    r = Hub()

    def fn(**kw):
        return "ok"

    r.register_ability("demo", "get", None, fn)

    assert r.get("demo", "get") is fn
    assert r.get("demo", "get", None) is fn
    assert "demo" in r.kinds


def test_register_ability_with_mode() -> None:
    r = Hub()

    def create(**kw):
        return "c"

    def replace(**kw):
        return "r"

    r.register_ability("demo", "put", "create", create)
    r.register_ability("demo", "put", "replace", replace)

    assert r.get("demo", "put", "create") is create
    assert r.get("demo", "put", "replace") is replace
    assert r.modes_for("demo", "put") == {"create", "replace"}


def test_register_ability_rejects_duplicate_key() -> None:
    r = Hub()
    r.register_ability("demo", "get", None, lambda **k: None)

    with pytest.raises(DuplicateRegistration, match="duplicate ability"):
        r.register_ability("demo", "get", None, lambda **k: None)


def test_register_skill_rejects_duplicate_slug() -> None:
    r = Hub()
    r.register_skill("precis-demo-help", "first content")

    with pytest.raises(DuplicateRegistration, match="duplicate skill"):
        r.register_skill("precis-demo-help", "second content")


def test_register_overview_allows_overwrite() -> None:
    """Overview is the one place where a later registration silently
    replaces an earlier one — a composite handler can set a blurb
    after its per-kind calls."""
    r = Hub()
    r.register_overview("demo", "first blurb")
    r.register_overview("demo", "second blurb")
    assert r.overview["demo"] == "second blurb"


def test_get_returns_none_on_miss() -> None:
    r = Hub()
    assert r.get("nosuch", "get") is None
    assert r.get("nosuch", "get", "create") is None


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def test_kinds_and_verbs_for_derivations() -> None:
    r = Hub()
    r.register_ability("demo", "get", None, lambda **k: None)
    r.register_ability("demo", "put", "create", lambda **k: None)
    r.register_ability("demo", "tag", None, lambda **k: None)
    r.register_ability("other", "get", None, lambda **k: None)

    assert r.kinds == {"demo", "other"}
    assert r.verbs_for("demo") == {"get", "put", "tag"}
    assert r.verbs_for("other") == {"get"}
    assert r.verbs_for("unknown") == set()


def test_kinds_supporting_verb() -> None:
    r = Hub()
    r.register_ability("a", "tag", None, lambda **k: None)
    r.register_ability("b", "tag", None, lambda **k: None)
    r.register_ability("c", "get", None, lambda **k: None)

    assert r.kinds_supporting("tag") == {"a", "b"}
    assert r.kinds_supporting("get") == {"c"}
    assert r.kinds_supporting("delete") == set()


# ---------------------------------------------------------------------------
# Hub.sibling — replaces the throwaway ``Hub(store=...)`` idiom
# ---------------------------------------------------------------------------


def test_sibling_returns_registered_instance_on_booted_hub(store: Store) -> None:
    from precis.handlers.todo import TodoHandler

    r = boot(store=store)
    inst = r.sibling("todo")
    assert inst is r.handler_for("todo")
    assert isinstance(inst, TodoHandler)


def test_sibling_lazily_constructs_and_caches_on_bare_hub(store: Store) -> None:
    from precis.handlers.job import JobHandler

    r = Hub(store=store)
    assert "job" not in r.handlers
    inst = r.sibling("job")
    assert isinstance(inst, JobHandler)
    # Cached: a second call returns the identical instance, not a fresh one.
    assert r.sibling("job") is inst
    assert r.handlers["job"] is inst


def test_sibling_unknown_kind_raises_key_error(store: Store) -> None:
    r = Hub(store=store)
    with pytest.raises(KeyError, match="no lazy-construction mapping"):
        r.sibling("nosuchkind")


# ---------------------------------------------------------------------------
# _try failure semantics
# ---------------------------------------------------------------------------


_GOOD_SPEC = KindSpec(
    kind="good",
    title="Good test handler",
    description="A handler that constructs fine.",
    supports_get=True,
)


class _Good(Handler):
    """Constructs fine; ``_try`` calls ``_register_with`` for us."""

    spec = _GOOD_SPEC

    def __init__(self, *, hub: Hub) -> None:
        # Smoke-test handler: no deps, but accept ``hub`` since
        # ``_try`` always threads it.
        _ = hub

    def get(self, **kw):
        return Response(body="good")


class _BadConfig(Handler):
    """Raises ``InitError`` before ``_register_with`` is reached."""

    spec = KindSpec(
        kind="badconfig",
        title="Bad config test handler",
        description="Raises InitError to simulate a missing dep.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub
        raise InitError("bad config: PRECIS_FOO missing")


class _BugInInit(Handler):
    """Raises a non-``InitError`` exception. ``_try`` must propagate."""

    spec = KindSpec(
        kind="bug",
        title="Buggy init test handler",
        description="Simulates a programmer error that must not be swallowed.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub
        raise RuntimeError("programmer bug")


def test_try_returns_instance_on_success() -> None:
    r = Hub()
    inst = _try(_Good, hub=r)
    assert isinstance(inst, _Good)
    # Compare with == (not ``is``): Python creates a fresh bound-method
    # object on every attribute access, so identity fails even though
    # both resolve to the same underlying function + instance.
    assert r.get("good", "get") == inst.get
    # The stored callable actually fires on the right instance.
    fn = r.get("good", "get")
    assert fn is not None
    assert fn().body == "good"
    # ``_register_with`` stashed the hub on the handler.
    assert inst.hub is r
    # And registered the handler itself for metadata queries.
    assert r.handler_for("good") is inst


def test_try_returns_none_on_init_error(caplog: pytest.LogCaptureFixture) -> None:
    r = Hub()
    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        inst = _try(_BadConfig, hub=r)
    assert inst is None
    # Registration never happened — the handler raised before
    # ``_try`` could call ``_register_with``.
    assert r.abilities == {}
    assert r.handlers == {}
    # Operator-facing WARN names the class and the reason.
    assert any(
        "_BadConfig init failed" in rec.message and "PRECIS_FOO missing" in rec.message
        for rec in caplog.records
    )


def test_try_propagates_non_init_exceptions() -> None:
    """Programmer bugs must NOT be silently swallowed — they would
    otherwise hide real errors behind "kind missing from surface"
    noise. ``InitError`` / ``ImportError`` / ``ValueError`` are the
    only swallowed exceptions."""
    r = Hub()
    with pytest.raises(RuntimeError, match="programmer bug"):
        _try(_BugInInit, hub=r)


def test_try_swallows_import_error(caplog: pytest.LogCaptureFixture) -> None:
    """Optional-dep handlers (math/sympy, patent/epo_ops) surface
    missing deps as ``ImportError`` from module-level imports inside
    ``__init__``. ``_try`` treats these as missing-dep and logs."""

    class _NeedsMissingModule(Handler):
        spec = KindSpec(
            kind="needsmod",
            title="Needs a missing module",
            description="Simulates an optional-dep import failure.",
            supports_get=True,
        )

        def __init__(self, *, hub: Hub) -> None:
            _ = hub
            raise ImportError("no module named fictional_dep")

        def get(self, **kw):
            return Response(body="never")

    r = Hub()
    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        result = _try(_NeedsMissingModule, hub=r)
    assert result is None
    assert r.abilities == {}


# ---------------------------------------------------------------------------
# boot() smoke tests
# ---------------------------------------------------------------------------


def test_boot_stateless_registers_calc_only() -> None:
    """Stateless path (no store) registers stateless handlers.

    Originally just ``calc``; ``provenance`` was added (also store-
    optional, gated on habanero). Both must show up on the
    no-store boot path.
    """
    r = boot(store=None)
    assert isinstance(r, Hub)
    assert {"calc", "provenance"}.issubset(r.kinds)
    # calc exposes only ``get``.
    assert r.verbs_for("calc") == {"get"}
    # Overview blurb was registered.
    assert "calc" in r.overview
    assert r.overview["calc"]


def test_boot_stateless_registers_handler_instance() -> None:
    """``handler_for`` returns the live ``CalcHandler`` instance so
    runtime metadata reads (``.spec``, ``search_hits``, …) hit the
    same object the dispatch table's bound methods belong to."""
    from precis.handlers.calc import CalcHandler

    r = boot(store=None)
    h = r.handler_for("calc")
    assert isinstance(h, CalcHandler)
    # The ability in the table is the same method on the same instance.
    assert r.get("calc", "get") == h.get


def test_boot_survives_missing_sympy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare-install regression: boot must not crash if sympy (the
    [calc] optional dep) isn't installed. The calc kind silently
    drops off the surface, same way math / youtube / web / patent
    drop when their extras are missing.
    """
    import builtins as _bi

    real_import = _bi.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "sympy" or name.startswith("sympy."):
            raise ImportError("simulated: sympy not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_bi, "__import__", _fake_import)
    import sys as _sys

    monkeypatch.delitem(_sys.modules, "precis.handlers.calc", raising=False)
    monkeypatch.delitem(_sys.modules, "sympy", raising=False)

    r = boot(store=None)
    assert "calc" not in r.kinds


# ---------------------------------------------------------------------------
# Read-only DSN detection — skip boot's non-idempotent writes (gr298050)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_read_only_env_override_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from precis.dispatch import _read_only_env_override

    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", value)
    assert _read_only_env_override() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_read_only_env_override_falsy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from precis.dispatch import _read_only_env_override

    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", value)
    assert _read_only_env_override() is False


def test_read_only_env_override_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.dispatch import _read_only_env_override

    monkeypatch.delenv("PRECIS_MCP_READ_ONLY", raising=False)
    assert _read_only_env_override() is False


def test_probe_read_only_reads_show_transaction_read_only(store: Store) -> None:
    """Sanity check against the real test DB: a normal (writable)
    connection reports ``off``."""
    from precis.dispatch import _probe_read_only

    assert _probe_read_only(store) is False


def test_probe_read_only_false_on_probe_failure() -> None:
    """A probe that itself blows up must not become a new boot failure
    mode — fall back to "assume writable" (today's behaviour).

    Uses a bare stand-in rather than the ``store`` fixture: swapping
    out the real ``store.pool`` would also have to survive the
    fixture's own teardown (``pool.close()``), which is an unrelated
    concern this unit test shouldn't have to manage.
    """
    from precis.dispatch import _probe_read_only

    class _BoomPool:
        def connection(self):  # pragma: no cover - never entered
            raise RuntimeError("simulated pool failure")

    class _FakeStore:
        pool = _BoomPool()

    assert _probe_read_only(_FakeStore()) is False  # type: ignore[arg-type]


def test_boot_skips_kinds_upsert_when_probe_reports_read_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    store: Store,
) -> None:
    """Mock the probe to report a read-only DSN: boot must skip the
    kinds/kind_provider upserts and log once at INFO, not exception
    level."""
    from precis import dispatch as _d

    monkeypatch.setattr(_d, "_probe_read_only", lambda _store: True)

    def _boom(*a: object, **kw: object) -> None:
        raise AssertionError("upsert_kinds must not be called on a read-only DSN")

    monkeypatch.setattr(store, "upsert_kinds", _boom)
    monkeypatch.setattr(store, "upsert_kind_providers", _boom)

    with caplog.at_level(logging.INFO, logger="precis.dispatch"):
        hub = _d.boot(store=store)

    assert isinstance(hub, Hub)
    assert any(
        rec.levelno == logging.INFO
        and "read-only DSN" in rec.message
        and "skipping boot writes" in rec.message
        for rec in caplog.records
    )
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_boot_skips_oracle_sync_thread_when_read_only(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    """The oracle_sync background thread must not even spawn on a
    read-only DSN — it holds an advisory lock and calls INSERT/UPDATE
    on every re-ingest."""
    from precis import dispatch as _d

    monkeypatch.setattr(_d, "_probe_read_only", lambda _store: True)
    monkeypatch.setenv("PRECIS_ORACLE_AUTO_REINGEST", "1")

    started: list[str] = []
    real_thread_start = __import__("threading").Thread.start

    def _spy_start(self: Any, *a: object, **kw: object) -> None:
        if self.name == "precis-oracle-sync":
            started.append(self.name)
        real_thread_start(self, *a, **kw)

    monkeypatch.setattr("threading.Thread.start", _spy_start)

    _d.boot(store=store)

    assert started == []


def test_boot_env_override_skips_writes_without_probing(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    """``PRECIS_MCP_READ_ONLY=1`` skips the probe entirely — boot must
    not even attempt the ``SHOW transaction_read_only`` round-trip."""
    from precis import dispatch as _d

    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", "1")

    def _boom_probe(_store: Store) -> bool:
        raise AssertionError("probe must not run when the env override is set")

    monkeypatch.setattr(_d, "_probe_read_only", _boom_probe)

    def _boom_write(*a: object, **kw: object) -> None:
        raise AssertionError("writes must not run when the env override is set")

    monkeypatch.setattr(store, "upsert_kinds", _boom_write)
    monkeypatch.setattr(store, "upsert_kind_providers", _boom_write)

    hub = _d.boot(store=store)
    assert isinstance(hub, Hub)


def test_boot_falls_back_to_writes_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    store: Store,
) -> None:
    """Probe failure (not read-only detection, the probe *erroring*)
    must fall back to the pre-fix behaviour — attempt the writes."""
    from precis import dispatch as _d

    def _boom_probe(_store: Store) -> bool:
        return False  # what _probe_read_only itself returns on failure

    monkeypatch.setattr(_d, "_probe_read_only", _boom_probe)
    monkeypatch.delenv("PRECIS_MCP_READ_ONLY", raising=False)

    called: list[str] = []
    real_upsert_kinds = store.upsert_kinds

    def _spy_upsert_kinds(specs: list[Any], *, conn: Any = None) -> int:
        called.append("upsert_kinds")
        return real_upsert_kinds(specs, conn=conn)

    monkeypatch.setattr(store, "upsert_kinds", _spy_upsert_kinds)

    with caplog.at_level(logging.INFO, logger="precis.dispatch"):
        _d.boot(store=store)

    assert called == ["upsert_kinds"]
    assert not any(
        "read-only DSN — skipping boot writes" in rec.message
        for rec in caplog.records
    )


def test_duplicate_handler_registration_raises() -> None:
    """Two handlers claiming the same kind is always a programming
    error — caught at boot time so it doesn't silently shadow at
    dispatch time."""
    from precis.handlers.calc import CalcHandler

    r = Hub()
    first = CalcHandler(hub=r)
    first._register_with(r)

    second = CalcHandler(hub=r)
    with pytest.raises(DuplicateRegistration, match="duplicate handler"):
        second._register_with(r)


# ---------------------------------------------------------------------------
# Third-party plugin discovery via entry-points
# ---------------------------------------------------------------------------


class _FakeEP:
    """Stand-in for ``importlib.metadata.EntryPoint`` in tests.

    Exposes only the two attributes ``_load_plugins`` actually reads:
    ``name`` (for log messages) and ``load()`` (returns the class).
    """

    def __init__(self, name: str, loader: object) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> object:
        if callable(self._loader) and not isinstance(self._loader, type):
            # Allow passing a zero-arg factory that raises to simulate
            # import-time failure.
            return self._loader()
        return self._loader


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEP]) -> None:
    """Stub ``precis.dispatch._entry_points`` to return ``eps`` for our group.

    Other groups return an empty list so we don't pollute unrelated
    importlib.metadata consumers.
    """
    from precis import dispatch as _d

    def _fake(*, group: str) -> list[_FakeEP]:
        return list(eps) if group == PLUGIN_GROUP else []

    monkeypatch.setattr(_d, "_entry_points", _fake)


class _PluginGood(Handler):
    """A plugin handler that works. Registers a fresh kind."""

    spec = KindSpec(
        kind="plugin-demo",
        title="Plugin demo",
        description="Test plugin loaded via entry-points.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub

    def get(self, **kw: object) -> Response:
        return Response(body="plugin ok")


class _PluginNeedsDep(Handler):
    """A plugin that raises ``InitError`` (missing dep path)."""

    spec = KindSpec(
        kind="plugin-needsdep",
        title="Plugin needing a dep",
        description="Simulates missing optional dependency.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub
        raise InitError("plugin-needsdep requires 'fictional_lib'")


class _PluginBuggy(Handler):
    """A plugin whose ``__init__`` raises a non-InitError exception.

    For *built-in* handlers this would crash boot (programmer bug).
    For plugins we log and skip — a third-party bug must not brick
    the MCP server.
    """

    spec = KindSpec(
        kind="plugin-buggy",
        title="Buggy plugin",
        description="Simulates a third-party programmer bug.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub
        raise RuntimeError("third-party programmer bug")


class _PluginShadowsCalc(Handler):
    """A plugin that tries to register the ``calc`` kind already
    owned by the built-in ``CalcHandler``."""

    spec = KindSpec(
        kind="calc",
        title="Impostor calc",
        description="Tries to shadow the built-in calc kind.",
        supports_get=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        _ = hub

    def get(self, **kw: object) -> Response:
        return Response(body="impostor")


def test_plugin_entry_point_registers_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin advertised via the ``precis.handlers`` entry-point
    group shows up in the hub alongside built-ins."""
    _patch_entry_points(monkeypatch, [_FakeEP("plugin-demo", _PluginGood)])

    hub = boot(store=None)
    assert "plugin-demo" in hub.kinds
    assert hub.verbs_for("plugin-demo") == {"get"}
    ability = hub.get("plugin-demo", "get")
    assert ability is not None
    assert ability().body == "plugin ok"
    # Built-ins still present — plugins augment, they don't replace.
    assert "calc" in hub.kinds


def test_plugin_init_error_is_logged_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin that raises ``InitError`` during ``__init__`` is
    logged at WARNING level; the hub otherwise boots cleanly."""
    _patch_entry_points(monkeypatch, [_FakeEP("needs-dep", _PluginNeedsDep)])

    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        hub = boot(store=None)

    assert "plugin-needsdep" not in hub.kinds
    assert any(
        "precis plugin 'needs-dep'" in rec.message and "fictional_lib" in rec.message
        for rec in caplog.records
    )


def test_plugin_programmer_bug_does_not_brick_boot(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Third-party plugins get wider failure tolerance than
    built-ins: a stray ``RuntimeError`` in plugin ``__init__`` is
    caught and logged (not propagated), so one bad plugin can't
    brick the whole MCP server.

    Contrast with ``_try`` for in-tree handlers, which propagates
    non-InitError exceptions so programmer bugs get noticed.
    """
    _patch_entry_points(monkeypatch, [_FakeEP("buggy", _PluginBuggy)])

    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        hub = boot(store=None)

    # Server still booted — built-ins are present.
    assert "calc" in hub.kinds
    assert "plugin-buggy" not in hub.kinds
    # Log line names the plugin, the class, and the exception type.
    assert any(
        "precis plugin 'buggy'" in rec.message
        and "RuntimeError" in rec.message
        and "third-party programmer bug" in rec.message
        for rec in caplog.records
    )


def test_plugin_cannot_shadow_builtin_kind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Built-in handlers boot first, so a plugin claiming an
    already-registered kind hits ``DuplicateRegistration`` inside
    ``_register_with`` and is logged. The built-in keeps the kind.
    """
    _patch_entry_points(monkeypatch, [_FakeEP("impostor", _PluginShadowsCalc)])

    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        hub = boot(store=None)

    from precis.handlers.calc import CalcHandler

    # The built-in calc handler is still in place — not the impostor.
    assert isinstance(hub.handler_for("calc"), CalcHandler)
    assert any(
        "precis plugin 'impostor'" in rec.message and "duplicate handler" in rec.message
        for rec in caplog.records
    )


def test_plugin_load_import_error_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry-point whose target module can't be imported (e.g.
    missing optional dep at module scope) is logged at WARNING and
    does not crash boot."""

    def _raises_at_load() -> object:
        raise ImportError("simulated: module not found")

    _patch_entry_points(monkeypatch, [_FakeEP("broken-import", _raises_at_load)])

    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        hub = boot(store=None)

    assert "calc" in hub.kinds  # built-ins intact
    assert any(
        "precis plugin 'broken-import' failed to load" in rec.message
        and "ImportError" in rec.message
        for rec in caplog.records
    )


def test_plugin_empty_entry_points_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no plugins advertised, boot is indistinguishable from
    the pre-plugin behaviour (only built-ins present)."""
    _patch_entry_points(monkeypatch, [])

    hub = boot(store=None)
    assert {"calc", "provenance"}.issubset(hub.kinds)
