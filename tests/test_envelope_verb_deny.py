"""gr254326: the envelope's precis write-verb deny holds under both MCP profiles.

``permissions.deny`` names (``mcp__precis__put`` ...) only resolve under the
``typed`` profile. The profile-independent gate is the verb-level deny in
``runtime/dispatch.py`` driven by ``PRECIS_MCP_DENY_VERBS`` (exported by the
worker from :func:`precis.workers.envelope.denied_verbs`). Both profiles end
in ``dispatch_with_status``, so these tests drive each profile's entry point
against a real store-backed runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import server
from precis.runtime import PrecisRuntime
from precis.runtime import dispatch as dispatch_mod
from precis.tools import core as tools_core
from precis.utils import claude_agent
from precis.workers import envelope as env_mod
from precis.workers.envelope import DEFAULT, Envelope, denied_verbs


@pytest.fixture
def mounted(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PrecisRuntime]:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    yield runtime_with_store


def _text(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


# ── resolver + wiring ─────────────────────────────────────────────


def test_env_name_matches_dispatch_literal() -> None:
    assert env_mod.DENY_VERBS_ENV == dispatch_mod._DENY_VERBS_ENV


def test_write_verb_list_matches_dispatch_write_verbs() -> None:
    assert set(env_mod.PRECIS_WRITE_VERB_NAMES) == set(dispatch_mod._WRITE_VERBS)


def test_overlay_cannot_loosen_denied_verbs() -> None:
    out = claude_agent._prepare_agent_env(
        active_env=Envelope(write="none"),
        bare=True,
        env_overlay={env_mod.DENY_VERBS_ENV: ""},
        env_base={},
    )
    assert out[env_mod.DENY_VERBS_ENV] == "put,edit,delete,tag,link"


def test_denied_verbs_per_axis() -> None:
    assert denied_verbs(Envelope(write="none")) == (
        "put",
        "edit",
        "delete",
        "tag",
        "link",
    )
    assert denied_verbs(Envelope(write="scoped")) == ("delete",)
    assert denied_verbs(Envelope(write="full")) == ()
    assert denied_verbs(DEFAULT) == ()


def test_prepare_env_exports_denied_verbs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "ambient")
    out = claude_agent._prepare_agent_env(
        active_env=Envelope(write="none"), bare=True, env_overlay=None, env_base={}
    )
    assert out[env_mod.DENY_VERBS_ENV] == "put,edit,delete,tag,link"
    full = claude_agent._prepare_agent_env(
        active_env=Envelope(write="full"),
        bare=True,
        env_overlay=None,
        env_base={env_mod.DENY_VERBS_ENV: "stale"},
    )
    assert env_mod.DENY_VERBS_ENV not in full


def test_container_env_carries_denied_verbs() -> None:
    from precis.workers.executors import agent_container as ac

    vals = ac.container_env(Envelope(write="none"), model="m", mode="oauth").values
    assert vals[env_mod.DENY_VERBS_ENV] == "put,edit,delete,tag,link"


# ── enforcement, typed profile (per-verb tools) ───────────────────


def test_typed_profile_write_none_refuses_every_write(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "put,edit,delete,tag,link")
    outs = [
        tools_core.put(kind="todo", text="x"),
        tools_core.edit(kind="todo", id=1, text="y"),
        tools_core.delete(kind="todo", id=1),
        tools_core.tag(kind="todo", id=1, add=["a"]),
        tools_core.link(kind="todo", id=1, target="todo:2"),
    ]
    for out in outs:
        assert "[error:Denied]" in _text(out)


def test_typed_profile_reads_still_work(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "put,edit,delete,tag,link")
    assert "Denied" not in _text(tools_core.search(kind="todo", q="anything"))


def test_typed_profile_scoped_refuses_only_delete(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "delete")
    assert "[error:Denied]" in _text(tools_core.delete(kind="todo", id=1))
    assert "[error:Denied]" not in _text(tools_core.put(kind="todo", text="ok"))


# ── enforcement, command profile (single precis(command=...) tool) ─


@pytest.mark.parametrize(
    "command",
    [
        "put(kind='todo', text='x')",
        "edit(kind='todo', id=1, text='y')",
        "delete(kind='todo', id=1)",
        "tag(kind='todo', id=1, add=['a'])",
        "link(kind='todo', id=1, target='todo:2')",
    ],
)
def test_command_profile_write_none_refuses_every_write(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("PRECIS_MCP_PROFILE", "command")
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "put,edit,delete,tag,link")
    assert "[error:Denied]" in _text(server.precis(command=command))


def test_command_profile_scoped_and_reads(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MCP_PROFILE", "command")
    monkeypatch.setenv(env_mod.DENY_VERBS_ENV, "delete")
    assert "[error:Denied]" in _text(server.precis(command="delete(kind='todo', id=1)"))
    assert "Denied" not in _text(server.precis(command="search(kind='todo', q='x')"))


def test_no_env_var_denies_nothing(
    mounted: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(env_mod.DENY_VERBS_ENV, raising=False)
    assert "[error:Denied]" not in _text(tools_core.put(kind="todo", text="ok"))
