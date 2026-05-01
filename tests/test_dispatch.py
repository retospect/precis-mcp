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
    InitError,
    Registry,
    _try,
    boot,
)

# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------


def test_register_ability_records_key_and_callable() -> None:
    r = Registry()

    def fn(**kw): return "ok"

    r.register_ability("demo", "get", None, fn)

    assert r.get("demo", "get") is fn
    assert r.get("demo", "get", None) is fn
    assert "demo" in r.kinds


def test_register_ability_with_mode() -> None:
    r = Registry()

    def create(**kw): return "c"
    def replace(**kw): return "r"

    r.register_ability("demo", "put", "create", create)
    r.register_ability("demo", "put", "replace", replace)

    assert r.get("demo", "put", "create") is create
    assert r.get("demo", "put", "replace") is replace
    assert r.modes_for("demo", "put") == {"create", "replace"}


def test_register_ability_rejects_duplicate_key() -> None:
    r = Registry()
    r.register_ability("demo", "get", None, lambda **k: None)

    with pytest.raises(DuplicateRegistration, match="duplicate ability"):
        r.register_ability("demo", "get", None, lambda **k: None)


def test_register_skill_rejects_duplicate_slug() -> None:
    r = Registry()
    r.register_skill("precis-demo-help", "first content")

    with pytest.raises(DuplicateRegistration, match="duplicate skill"):
        r.register_skill("precis-demo-help", "second content")


def test_register_overview_allows_overwrite() -> None:
    """Overview is the one place where a later registration silently
    replaces an earlier one — a composite handler can set a blurb
    after its per-kind calls."""
    r = Registry()
    r.register_overview("demo", "first blurb")
    r.register_overview("demo", "second blurb")
    assert r.overview["demo"] == "second blurb"


def test_get_returns_none_on_miss() -> None:
    r = Registry()
    assert r.get("nosuch", "get") is None
    assert r.get("nosuch", "get", "create") is None


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def test_kinds_and_verbs_for_derivations() -> None:
    r = Registry()
    r.register_ability("demo", "get", None, lambda **k: None)
    r.register_ability("demo", "put", "create", lambda **k: None)
    r.register_ability("demo", "tag", None, lambda **k: None)
    r.register_ability("other", "get", None, lambda **k: None)

    assert r.kinds == {"demo", "other"}
    assert r.verbs_for("demo") == {"get", "put", "tag"}
    assert r.verbs_for("other") == {"get"}
    assert r.verbs_for("unknown") == set()


def test_kinds_supporting_verb() -> None:
    r = Registry()
    r.register_ability("a", "tag", None, lambda **k: None)
    r.register_ability("b", "tag", None, lambda **k: None)
    r.register_ability("c", "get", None, lambda **k: None)

    assert r.kinds_supporting("tag") == {"a", "b"}
    assert r.kinds_supporting("get") == {"c"}
    assert r.kinds_supporting("delete") == set()


# ---------------------------------------------------------------------------
# _try failure semantics
# ---------------------------------------------------------------------------


class _Good:
    """Constructs fine and registers one ability."""

    def __init__(self, r: Registry) -> None:
        r.register_ability("good", "get", None, self.get)

    def get(self, **kw):
        return "good"


class _BadInit(Exception):
    """Unrelated exception type — _try should NOT swallow this."""


class _BadConfig:
    """Raises ``InitError`` before registering anything."""

    def __init__(self, r: Registry) -> None:
        raise InitError("bad config: PRECIS_FOO missing")


class _BugInInit:
    """Raises a non-``InitError`` exception. _try must propagate."""

    def __init__(self, r: Registry) -> None:
        raise RuntimeError("programmer bug")


class _RegistersThenRaises:
    """Violates the contract: registers BEFORE raising ``InitError``.

    The registry will end up with a dangling entry. The test asserts
    this is the operator's problem to notice (via the WARN log), not
    something _try tries to roll back.
    """

    def __init__(self, r: Registry) -> None:
        r.register_ability("broken", "get", None, self.get)
        raise InitError("decided to fail after registering, naughty")

    def get(self, **kw):
        return "broken"


def test_try_returns_instance_on_success() -> None:
    r = Registry()
    inst = _try(_Good, r)
    assert isinstance(inst, _Good)
    # Compare with == (not ``is``): Python creates a fresh bound-method
    # object on every attribute access, so identity fails even though
    # both resolve to the same underlying function + instance.
    assert r.get("good", "get") == inst.get
    # And the stored callable actually fires on the right instance.
    assert r.get("good", "get")() == "good"


def test_try_returns_none_on_init_error(caplog: pytest.LogCaptureFixture) -> None:
    r = Registry()
    with caplog.at_level(logging.WARNING, logger="precis.dispatch"):
        inst = _try(_BadConfig, r)
    assert inst is None
    # No abilities registered — registration was the last block in the
    # well-behaved handler, so an early raise leaves the registry clean.
    assert r.abilities == {}
    # Operator-facing WARN names the class and the reason.
    assert any(
        "_BadConfig init failed" in rec.message and "PRECIS_FOO missing" in rec.message
        for rec in caplog.records
    )


def test_try_propagates_non_init_exceptions() -> None:
    """Programmer bugs must NOT be silently swallowed — they would
    otherwise hide real errors behind "kind missing from surface"
    noise."""
    r = Registry()
    with pytest.raises(RuntimeError, match="programmer bug"):
        _try(_BugInInit, r)


def test_try_does_not_roll_back_partial_registration() -> None:
    """A handler that registers before raising leaves the registry in
    a broken state. This documents current behaviour so a future
    refactor doesn't silently change it — the handler-author contract
    says "register LAST", and violators pay the cost of a broken
    dispatch entry pointing at a half-constructed instance."""
    r = Registry()
    result = _try(_RegistersThenRaises, r)
    assert result is None
    # The errant entry is still there. The dispatch table is corrupt
    # for this key. Operator sees the WARN and must fix the handler.
    assert ("broken", "get", None) in r.abilities


# ---------------------------------------------------------------------------
# boot() smoke tests
# ---------------------------------------------------------------------------


def test_boot_returns_empty_registry_when_no_handlers_wired() -> None:
    """Phase 1 stub boot: no handlers are wired yet, so boot returns
    an empty registry. Once handlers port over, this test gets
    replaced by the per-kind registration asserts."""
    r = boot({})
    assert isinstance(r, Registry)
    assert r.abilities == {}
    assert r.kinds == set()
