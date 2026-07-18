"""13-code: the general agentic container executor's pure core (§13).

Proves the envelope (slice 8) → ``docker run`` knob wiring — tier-2 (write → DB
role env) and tier-3 (egress → ``--network`` + allowlist) — and the dark policy
gate. All pure (no DB / no container), so it runs anywhere.
"""

from __future__ import annotations

import json

import pytest

from precis.workers.envelope import Envelope
from precis.workers.executors import agent_container as ac


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip the knobs so each test asserts against the documented defaults."""
    for k in (
        "PRECIS_AGENT_CONTAINER",
        "PRECIS_AGENT_IMAGE",
        "PRECIS_AGENT_MODE",
        "PRECIS_AGENT_NETWORK",
        "PRECIS_AGENT_LLM_HOST",
        "PRECIS_PODMAN_BIN",
    ):
        monkeypatch.delenv(k, raising=False)


# ── policy gate (the dark switch) ──────────────────────────────────


def test_policy_off_by_default() -> None:
    assert ac.container_agent_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE"])
def test_policy_on_when_set(monkeypatch, val) -> None:
    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", val)
    assert ac.container_agent_enabled() is True


def test_policy_off_for_junk(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "0")
    assert ac.container_agent_enabled() is False


# ── tier-3: egress → network ───────────────────────────────────────


def test_open_egress_has_no_network_restriction() -> None:
    plan = ac.resolve_network(Envelope(egress="open"))
    assert plan.mode == "open"
    assert plan.docker_args == ()
    assert plan.allowlist == ()


def test_egress_none_is_network_none() -> None:
    plan = ac.resolve_network(Envelope(egress="none"))
    assert plan.mode == "none"
    assert plan.docker_args == ("--network", "none")
    assert plan.allowlist == ()


def test_api_only_allowlists_anthropic_plus_pgbouncer() -> None:
    dsn = "postgresql://agent_rw:pw@db.internal:6432/precis_prod"
    plan = ac.resolve_network(Envelope(egress="api-only"), dsn=dsn)
    assert plan.mode == "api-only"
    assert plan.docker_args == ("--network", "bridge")
    assert plan.allowlist == ("api.anthropic.com", "db.internal:6432")


def test_api_only_without_dsn_still_lists_anthropic() -> None:
    plan = ac.resolve_network(Envelope(egress="api-only"))
    assert plan.allowlist == ("api.anthropic.com",)


def test_api_only_honors_local_llm_host_override(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_AGENT_LLM_HOST", "llm.internal:8080")
    plan = ac.resolve_network(Envelope(egress="api-only"))
    assert plan.allowlist == ("llm.internal:8080",)


# ── tier-2: write → DB role env + secret mode ──────────────────────


def test_write_full_is_agent_rw_oauth() -> None:
    cenv = ac.container_env(Envelope(write="full"), model="qwen")
    assert cenv.values["PRECIS_MCP_DB_ROLE"] == "agent_rw"
    assert cenv.values["PRECIS_AGENT_MODE"] == "oauth"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cenv.secret_keys
    assert "PRECIS_DATABASE_URL" in cenv.secret_keys
    assert "ANTHROPIC_API_KEY" not in cenv.secret_keys


def test_write_none_is_agent_ro() -> None:
    cenv = ac.container_env(Envelope(write="none"), model="qwen")
    assert cenv.values["PRECIS_MCP_DB_ROLE"] == "agent_ro"


def test_api_mode_injects_api_key_not_oauth(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_AGENT_MODE", "api")
    cenv = ac.container_env(Envelope(), model="qwen")
    assert "ANTHROPIC_API_KEY" in cenv.secret_keys
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cenv.secret_keys
    assert cenv.values["PRECIS_AGENT_MODE"] == "api"


# ── argv assembly (secret-by-key, network, image-last) ─────────────


def test_argv_passes_secrets_by_key_only() -> None:
    cenv = ac.ContainerEnv(
        secret_keys=("CLAUDE_CODE_OAUTH_TOKEN", "PRECIS_DATABASE_URL"),
        values={"PRECIS_MCP_DB_ROLE": "agent_ro"},
    )
    argv = ac.build_agent_run_argv(
        container_bin="podman",
        name="agent-7",
        image="precis-agent:pinned",
        cenv=cenv,
        net=ac.NetworkPlan(mode="none", docker_args=("--network", "none")),
    )
    # secret KEY only — the value never appears in argv.
    assert "--env" in argv and "CLAUDE_CODE_OAUTH_TOKEN" in argv
    joined = " ".join(argv)
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in joined  # by key, not value
    assert "PRECIS_MCP_DB_ROLE=agent_ro" in argv  # non-secret value inline
    assert "--network" in argv and "none" in argv
    assert argv[-1] == "precis-agent:pinned"  # image last
    assert "--device" not in argv  # never a GPU
    assert argv[:2] == ["podman", "run"] and "--rm" in argv and "agent-7" in argv


def test_build_agent_run_wires_both_tiers_end_to_end() -> None:
    env = Envelope(egress="none", write="none")
    argv = ac.build_agent_run(env, name="agent-9", model="qwen", image="precis-agent:x")
    assert "--network" in argv and "none" in argv  # tier-3
    assert "PRECIS_MCP_DB_ROLE=agent_ro" in argv  # tier-2
    assert argv[-1] == "precis-agent:x"


def test_default_image_env_override(monkeypatch) -> None:
    assert ac.default_agent_image() == "precis-agent:latest"
    monkeypatch.setenv("PRECIS_AGENT_IMAGE", "precis-agent@sha256:abc")
    assert ac.default_agent_image() == "precis-agent@sha256:abc"


# ── the claude -p command (the container's argv tail) ──────────────


def test_claude_command_mirrors_inproc_argv() -> None:
    argv = ac.build_claude_command(model="qwen", prompt="do the thing", mode="oauth")
    assert argv[0:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == "qwen"
    assert "--strict-mcp-config" in argv
    # baked MCP config wires precis serve over stdio
    assert argv[argv.index("--mcp-config") + 1] == "/etc/precis/agent-mcp.json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[-1] == "do the thing"  # prompt last
    assert "--bare" not in argv  # oauth mode uses the token, not the API key


def test_claude_command_api_mode_is_bare() -> None:
    argv = ac.build_claude_command(model="qwen", prompt="p", mode="api")
    assert "--bare" in argv


def test_claude_command_deny_via_settings_not_variadic() -> None:
    argv = ac.build_claude_command(
        model="qwen", prompt="p", mode="oauth", disallowed_tools=("WebFetch", "put")
    )
    # deny list rides --settings JSON (never --disallowed-tools, which eats the
    # prompt), and the prompt stays the final positional.
    assert "--disallowed-tools" not in argv
    settings = argv[argv.index("--settings") + 1]
    assert json.loads(settings) == {"permissions": {"deny": ["WebFetch", "put"]}}
    assert argv[-1] == "p"


def test_mcp_config_env_override(monkeypatch) -> None:
    assert ac.default_agent_mcp_config() == "/etc/precis/agent-mcp.json"
    monkeypatch.setenv("PRECIS_AGENT_MCP_CONFIG", "/custom/mcp.json")
    assert ac.default_agent_mcp_config() == "/custom/mcp.json"


def test_run_argv_appends_command_after_image() -> None:
    cenv = ac.ContainerEnv(secret_keys=("CLAUDE_CODE_OAUTH_TOKEN",), values={})
    argv = ac.build_agent_run_argv(
        container_bin="podman",
        name="agent-1",
        image="precis-agent:x",
        cenv=cenv,
        net=ac.NetworkPlan(mode="open"),
        command=["claude", "-p", "hello"],
    )
    i = argv.index("precis-agent:x")
    assert argv[i + 1 :] == ["claude", "-p", "hello"]  # command trails the image


def test_build_agent_run_with_prompt_appends_claude_command() -> None:
    env = Envelope(write="none", egress="none")
    argv = ac.build_agent_run(
        env, name="agent-9", model="qwen", prompt="summarize", image="precis-agent:x"
    )
    # run knobs still present (tier-2 + tier-3) ...
    assert "PRECIS_MCP_DB_ROLE=agent_ro" in argv
    assert "--network" in argv and "none" in argv
    # ... and the claude -p command now trails the image, prompt last.
    i = argv.index("precis-agent:x")
    assert argv[i + 1 : i + 3] == ["claude", "-p"]
    assert argv[-1] == "summarize"


def test_build_agent_run_without_prompt_is_unchanged() -> None:
    """The 13-code shape: no prompt ⇒ no command, image last (default CMD runs)."""
    env = Envelope()
    argv = ac.build_agent_run(env, name="a", model="qwen", image="precis-agent:x")
    assert argv[-1] == "precis-agent:x"


# ── containerize an already-built host argv (the live-executor seam) ──


def test_containerize_wraps_host_argv_preserving_all_flags() -> None:
    env = Envelope(egress="none", write="none")
    host = [
        "/usr/local/bin/claude",
        "-p",
        "--model",
        "opus",
        "--max-budget-usd",
        "5",
        "--append-system-prompt",
        "sys",
        "the prompt",
    ]
    argv = ac.containerize_claude_argv(
        host, env, name="agent-x", model="opus", image="precis-agent:x"
    )
    # host binary dropped; the image's `claude` (on PATH) runs instead
    assert "/usr/local/bin/claude" not in argv
    # tier-2 + tier-3 knobs applied from the envelope
    assert "PRECIS_MCP_DB_ROLE=agent_ro" in argv
    assert "--network" in argv and "none" in argv
    # synchronous (foreground, stdout captured) — no -d
    assert "-d" not in argv
    # image then the command: `claude` + EVERY original flag after the host binary
    i = argv.index("precis-agent:x")
    assert argv[i + 1 :] == [
        "claude",
        "-p",
        "--model",
        "opus",
        "--max-budget-usd",
        "5",
        "--append-system-prompt",
        "sys",
        "the prompt",
    ]
