"""gr503441: subagent write attribution is an env property — pin it.

A subagent shares the parent's spawned ``precis serve`` and therefore its
environment. Two env vars do the work: ``PRECIS_MCP_DENY_VERBS`` (the
envelope's verb deny, enforced in ``dispatch._refuse_envelope_denied_verb``)
and ``PRECIS_CURRENT_AGENTLOG`` (the run id ``_record_tool_call`` stamps on
every ledger row). Because both ride the shared env, a subagent's refused —
or accepted — write is attributed to the parent run today. No runtime
change; this pins the env → ledger path so a transport change (a
shared-server thread) cannot silently drop it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from precis import agentlog, tool_ledger
from precis.runtime import PrecisRuntime
from precis.utils import claude_agent as ca
from precis.workers.envelope import DENY_VERBS_ENV, Envelope


def test_spawned_serve_env_denies_write_and_attributes_to_parent_run(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end on the env seam: the proc env ``call_claude_agent`` builds
    for a ``write:none`` run carries both vars; a dispatch under that env —
    which is exactly what a subagent's shared serve sees — is refused AND
    attributed to the parent's agentlog id."""
    # ── parent side: the env ``claude -p`` (and its spawned serve) gets ──
    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
    monkeypatch.delenv("PRECIS_MCP_PROFILE", raising=False)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "/nonexistent/claude")
    captured: dict[str, object] = {}

    def _fake_run(argv, **k):
        captured["env"] = k.get("env")
        return SimpleNamespace(stdout="done", stderr="", returncode=0)

    monkeypatch.setattr(ca, "run_claude", _fake_run)
    ca.call_claude_agent(
        "the prompt",
        model="opus",
        envelope=Envelope(write="none"),
        bare=True,
        env_base={},
        env_overlay={agentlog.ENV_VAR: "4242"},
    )
    proc_env = captured["env"]
    assert isinstance(proc_env, dict)
    assert proc_env[DENY_VERBS_ENV] == "put,edit,delete,tag,link"
    assert proc_env[agentlog.ENV_VAR] == "4242"

    # ── serve side: the env a subagent's shared ``precis serve`` runs ──
    monkeypatch.setenv(DENY_VERBS_ENV, proc_env[DENY_VERBS_ENV])
    monkeypatch.setenv(agentlog.ENV_VAR, proc_env[agentlog.ENV_VAR])
    recorded: list[tool_ledger.ToolCallRecord] = []
    monkeypatch.setattr(tool_ledger, "record_call", lambda s, rec: recorded.append(rec))

    body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "todo", "text": "x"}
    )

    assert is_error is True
    assert "Denied" in body
    assert "permission envelope" in body
    assert len(recorded) == 1
    assert recorded[0].agentlog_id == 4242
    assert recorded[0].outcome == "error"
    assert recorded[0].verb == "put"
