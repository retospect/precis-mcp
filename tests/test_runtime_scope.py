"""``runtime.scope.current_scope`` — the process-scope signal (gr512233,
``docs/backlog/read-scope-skill-fencing.md`` slice 1) — and
``runtime.scope.current_domain`` — the surface-domain model and
resolution (gr572719).

Each read source yields ``'read'``; the default is ``'write'``;
``write:scoped`` (only ``delete`` denied) stays ``'write'``. The
per-request read token is slice 4 — no request source exists yet.

Domain resolution: the request-bound token domain (the
``request_domain`` seam slice 4 wires to the shared server's token
gate) beats ``PRECIS_DOMAIN``, which beats the dark default ``'all'`` —
unrestricted on every axis, byte-identical to today. The lint half:
a domain naming an unknown kind, family or space raises at
construction, so a bad ``DOMAINS`` entry fails the module's import,
not a serve.
"""

from __future__ import annotations

import pytest

import precis.runtime.scope as scope_mod
from precis.runtime.scope import (
    Domain,
    current_domain,
    current_scope,
    note_boot_read_only,
    request_domain,
)
from precis.utils.skill_common import SKILL_FAMILIES


@pytest.fixture(autouse=True)
def _clean_scope_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize every scope input so each test sets exactly one."""
    for name in (
        scope_mod.SERVE_READ_ONLY_ENV,
        "PRECIS_MCP_DENY_VERBS",
        "PRECIS_MCP_READ_ONLY",
        scope_mod.DOMAIN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


def test_default_is_write() -> None:
    assert current_scope() == "write"


def test_deny_verbs_naming_put_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``write:none`` envelope exports all five write verbs."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert current_scope() == "read"


def test_deny_verbs_delete_only_stays_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``write:scoped`` denies ``delete`` only — still a writing run."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "delete")
    assert current_scope() == "write"


def test_deny_verbs_without_put_stays_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dream's deny list (edit,delete,link) leaves ``put`` open."""
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "edit,delete,link")
    assert current_scope() == "write"


def test_serve_read_only_env_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scope_mod.SERVE_READ_ONLY_ENV, "1")
    assert current_scope() == "read"


def test_precis_mcp_read_only_env_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", "1")
    assert current_scope() == "read"


def test_boot_probe_verdict_is_read() -> None:
    note_boot_read_only(True)
    assert current_scope() == "read"


def test_second_boot_overwrites_stale_verdict() -> None:
    """``note_boot_read_only`` replaces, so a later writable boot in the
    same process (tests boot repeatedly) doesn't inherit a stale read."""
    note_boot_read_only(True)
    assert current_scope() == "read"
    note_boot_read_only(False)
    assert current_scope() == "write"


def test_put_denied_mirrors_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``runtime.dispatch._put_denied`` is now the scope's read half —
    the env deny, serve --read-only and the boot probe all reach the
    gripe handler's capped channel through it (gr512233)."""
    from precis.runtime.dispatch import _put_denied

    assert _put_denied() is False
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert _put_denied() is True


def test_put_denied_true_for_non_env_read_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cutover widens ``_put_denied`` beyond the env deny: an
    ``agent_ro``/read-only-DSN boot and a declared read-only process now
    take the same text-only gripe channel a write:none run gets."""
    from precis.runtime.dispatch import _put_denied

    monkeypatch.setenv("PRECIS_MCP_READ_ONLY", "1")
    assert _put_denied() is True


def test_serve_read_only_marks_scope() -> None:
    """``precis serve --read-only`` flags the scope inside
    ``_restrict_to_read_verbs`` — a module flag now, not the env var,
    so an in-process call no longer leaks ``read`` through
    ``os.environ`` (gr512233 round-2 review). The conftest reset
    clears it after the test."""
    from typing import cast

    from mcp.server.fastmcp import FastMCP

    from precis import server

    class _Tools:
        def list_tools(self) -> list:
            return []

    class _FakeFastMCP:
        _tool_manager = _Tools()

    server._restrict_to_read_verbs(cast(FastMCP, _FakeFastMCP()))
    assert current_scope() == "read"


# ── domains (gr572719) ─────────────────────────────────────────────────


def test_default_domain_is_all() -> None:
    assert current_domain() is scope_mod.DOMAINS["all"]


def test_all_domain_leaves_every_axis_open() -> None:
    """The dark default is byte-identical to today: ``None`` on an axis
    is no fence, and ``'all'`` is ``None`` on all three."""
    d = current_domain()
    assert d.kinds is None and d.families is None and d.spaces is None


def test_env_names_registered_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    research = Domain(name="research", kinds=frozenset({"paper", "finding"}))
    monkeypatch.setitem(scope_mod.DOMAINS, "research", research)
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "research")
    assert current_domain() is research
    assert current_domain().kinds == frozenset({"paper", "finding"})


def test_env_all_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    assert current_domain() is scope_mod.DOMAINS["all"]


def test_env_unknown_domain_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd overlay must fail loud — never silently widen to
    ``'all'``."""
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "no-such-domain")
    with pytest.raises(ValueError, match="no-such-domain"):
        current_domain()


def test_request_binding_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection token's domain outranks the process overlay — the
    resolution order's first rung (fencing slice 4 binds it)."""
    bound = Domain(name="bound")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    with request_domain(bound):
        assert current_domain() is bound
    assert current_domain() is scope_mod.DOMAINS["all"]


def test_request_binding_none_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    with request_domain(None):
        assert current_domain() is scope_mod.DOMAINS["all"]


def test_always_in_families_union() -> None:
    """A declared ``families`` set gains the always-in five; the stored
    value is the effective serve set."""
    d = Domain(name="eng", families=frozenset({"se", "pcb"}))
    assert d.families == scope_mod.ALWAYS_IN_FAMILIES | {"se", "pcb"}


def test_domain_unknown_kind_fails_at_construction() -> None:
    """The lint: ``__post_init__`` raises on an unknown kind, so a bad
    ``DOMAINS`` entry fails the import that builds it — not a serve."""
    with pytest.raises(ValueError, match="unknown kind"):
        Domain(name="bad", kinds=frozenset({"flying-cars"}))


def test_domain_unknown_family_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown skill families"):
        Domain(name="bad", families=frozenset({"alchemy"}))


def test_domain_unknown_space_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown SPACE"):
        Domain(name="bad", spaces=frozenset({"narnia"}))


def test_domain_space_prefix_normalises() -> None:
    d = Domain(name="rd", spaces=frozenset({"SPACE:repo-dev"}))
    assert d.spaces == frozenset({"repo-dev"})


def test_registry_entries_valid_and_always_in_known() -> None:
    """The lint's registry half: every ``DOMAINS`` entry survived the
    construction check above (import would have failed otherwise), is
    keyed under its own name, and the always-in set is itself inside
    the closed family vocabulary so the union can't inject one."""
    assert set(SKILL_FAMILIES) >= scope_mod.ALWAYS_IN_FAMILIES
    for name, d in scope_mod.DOMAINS.items():
        assert d.name == name
