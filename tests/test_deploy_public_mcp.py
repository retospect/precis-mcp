"""Public MCP shares OAuth configuration, not session infrastructure credentials."""

from __future__ import annotations

import os
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parents[1] / "deploy/roles/precis_web"


def _tasks() -> list[dict[str, Any]]:
    return yaml.safe_load((_ROLE / "tasks/public-mcp.yml").read_text(encoding="utf-8"))


def _render(**extra: Any) -> dict[str, Any]:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["combine"] = lambda a, b: {**a, **b}
    context: dict[str, Any] = {
        "precis_shared_env": {},
        "precis_identity_env": {},
        "precis_web_venv": "/opt/mcps/venv",
        "precis_web_port": 8000,
        "precis_web_dsn": "postgresql://precis_ingress@db/precis_prod",
        "precis_web_corpus": "/srv/corpus",
        "precis_web_watch_inbox": "/srv/papers/inbox",
        "precis_web_podcast_dir": "/srv/podcast",
        "precis_web_podcast_base_url": "https://gw.example",
        "precis_capabilities": {},
        "inventory_hostname": "gw",
        **extra,
    }
    source = (_ROLE / "templates/precis-web.plist.j2").read_text(encoding="utf-8")
    return plistlib.loads(env.from_string(source).render(**context).encode())


def test_web_defaults_preserve_command_and_keep_oauth_opt_in() -> None:
    plist = _render(
        precis_mcp_public_oauth_env={"PRECIS_OAUTH_ISSUER": "https://gw.example"}
    )
    assert plist["Label"] == "com.precis.web"
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        "-c",
        "exec /opt/mcps/venv/bin/precis web --host 127.0.0.1 --port 8000",
    ]
    assert plist["StandardErrorPath"] == "/var/log/precis-web.log"
    assert "PRECIS_OAUTH_ISSUER" not in plist["EnvironmentVariables"]


def test_public_mcp_has_same_oauth_but_its_own_credentials_and_guards() -> None:
    task_vars = next(
        t["vars"] for t in _tasks() if t["name"].endswith("LaunchDaemon plist")
    )
    guards = {
        **task_vars["precis_web_daemon_env"],
        "PRECIS_SECRETS_FILE_DIR": "/opt/mcps/mcp-public-secrets",
    }
    oauth = {
        "PRECIS_OAUTH_ISSUER": "https://gw.example",
        "PRECIS_OAUTH_RESOURCE": "https://gw.example:8443/mcp",
        "PRECIS_OAUTH_SCOPES": "mcp:full",
    }
    context = {
        "precis_mcp_public_enabled": True,
        "precis_mcp_public_oauth_env": oauth,
        "precis_shared_env": {
            "PRECIS_MCP_TOKEN": secrets.token_urlsafe(32),
            "PRECIS_AUTO_MIGRATE": "1",
            "PRECIS_SCHEMA_GATE": "off",
            "PRECIS_DATABASE_URL": "postgresql://precis_owner@db/precis_prod",
            "SERVICE_URL": "https://service.example/?a=1&b=2",
        },
    }
    web = _render(**context)
    public = _render(
        **context,
        precis_web_daemon_label=task_vars["precis_web_daemon_label"],
        precis_web_daemon_command="exec /opt/mcps/venv/bin/precis serve --transport streamable-http --host 127.0.0.1 --port 8766",
        precis_web_daemon_process=task_vars["precis_web_daemon_process"],
        precis_web_daemon_dsn="postgresql://agent_rw@db/precis_prod",
        precis_web_daemon_env=guards,
    )
    assert public["Label"] == "com.precis.mcp-public"
    actual = public["EnvironmentVariables"]
    for key, value in oauth.items():
        assert actual[key] == web["EnvironmentVariables"][key] == value
    assert actual["PRECIS_DATABASE_URL"] == "postgresql://agent_rw@db/precis_prod"
    assert actual["PRECIS_MCP_TOKEN"] == ""
    assert actual["PRECIS_AUTO_MIGRATE"] == "0"
    assert actual["PRECIS_SCHEMA_GATE"] == "refuse"
    assert actual["PRECIS_SKIP_SCHEMA_CHECK"] == ""
    assert actual["SERVICE_URL"] == "https://service.example/?a=1&b=2"


def test_token_generation_is_private_and_does_not_rotate_existing_token(
    tmp_path: Path,
) -> None:
    task = next(
        t
        for t in _tasks()
        if t["name"] == "Generate a separate infrastructure token once"
    )
    assert task["no_log"] is True
    script = task["ansible.builtin.command"]["argv"][2]
    path = tmp_path / "PRECIS_MCP_TOKEN"
    command = [sys.executable, "-c", script, str(path)]
    first = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    secret = path.read_text(encoding="utf-8")
    second = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert len(secret.strip()) == 64
    assert path.read_text(encoding="utf-8") == secret
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert first.stdout == second.stdout == first.stderr == second.stderr == ""
    assert list(tmp_path.iterdir()) == [path]
