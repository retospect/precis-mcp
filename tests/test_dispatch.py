"""Tests for ``precis.dispatch`` — the seven-verb registry + boot.

These tests exercise only the registration machinery and the boot
loop's failure semantics; they do not depend on any real handler
being ported yet. See ``docs/seven-verb-surface-migration.md`` D7
for the contract under test.
"""

from __future__ import annotations

import logging

import pytest

from precis.dispatch import (
    DuplicateRegistration,
    Hub,
    InitError,
    _try,
    boot,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response

# ---------------------------------------------------------------------------
# Hub primitives
# ---------------------------------------------------------------------------


def test_register_ability_records_key_and_callable() -> None:
    r = Hub()

    def fn(**kw): return "ok"

    r.register_ability("demo", "get", None, fn)

    assert r.get("demo", "get") is fn
    assert r.get("demo", "get", None) is fn
    assert "demo" in r.kinds


def test_register_ability_with_mode() -> None:
    r = Hub()

    def create(**kw): return "c"
    def replace(**kw): return "r"

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
    assert r.get("good", "get")().body == "good"
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
    """Stateless path (no store) registers only the calc kind.

    This is the phase-1 "no DB" deployment mode, preserved from the
    v1 ``registry.builtins(store=None)`` shape.
    """
    r = boot(store=None)
    assert isinstance(r, Hub)
    assert r.kinds == {"calc"}
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
