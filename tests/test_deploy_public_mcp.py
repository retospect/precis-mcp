"""Public MCP shares OAuth configuration, not session infrastructure credentials."""

from __future__ import annotations

import ast
import json
import os
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


def test_public_mcp_restarts_once_per_deploy_after_the_install() -> None:
    """gr660587: the install watchdog fired mid-install (respawn into a
    half-built venv, ~100 s down) and the bounce play restarted it again.
    The role handler is now its one restart, after both installs."""
    task_vars = next(
        t["vars"] for t in _tasks() if t["name"].endswith("LaunchDaemon plist")
    )
    assert task_vars["precis_web_daemon_env"]["PRECIS_INSTALL_WATCHDOG"] == "0"
    assert task_vars["precis_web_daemon_throttle"] == 5

    handlers = yaml.safe_load((_ROLE / "handlers/main.yml").read_text(encoding="utf-8"))
    handler = next(h for h in handlers if h["name"] == "restart precis-mcp-public")
    assert "precis_deploy_changed" not in str(handler["when"])

    installs = [
        t
        for t in yaml.safe_load(
            (_ROLE / "tasks/install.yml").read_text(encoding="utf-8")
        )
        if t["name"].startswith("Install precis-mcp[")
    ]
    assert len(installs) == 2
    for task in installs:
        assert "restart precis-mcp-public" in task["notify"]

    plays = yaml.safe_load(
        (_ROLE.parents[1] / "redeploy-precis.yml").read_text(encoding="utf-8")
    )
    bounce = next(p for p in plays if p.get("name") == "Bounce all precis daemons")
    find = next(t for t in bounce["tasks"] if "ansible.builtin.find" in t)
    assert "com.precis.mcp-public.plist" in find["ansible.builtin.find"]["excludes"]


def test_web_keeps_default_throttle() -> None:
    plist = _render(precis_mcp_public_oauth_env={})
    assert plist["ThrottleInterval"] == 30


# -- Credential classes (PRECIS_MCP_CREDENTIAL_CLASSES) ----------------------

_VALID_CLASSES = ["install", "slice", "personal", "capability", "oauth"]


def _defaults() -> dict[str, Any]:
    return yaml.safe_load((_ROLE / "defaults/main.yml").read_text(encoding="utf-8"))


def _expression(source: str) -> Any:
    return jinja2.Environment(undefined=jinja2.StrictUndefined).compile_expression(
        source.strip()
    )


def _when(task_name: str, classes: str) -> bool:
    task = next(t for t in _tasks() if t["name"] == task_name)
    return bool(_expression(task["when"])(precis_mcp_public_credential_classes=classes))


def _class_assertions() -> list[str]:
    assertion = next(t for t in _tasks() if t["name"].startswith("Validate public"))
    return [
        c
        for c in assertion["ansible.builtin.assert"]["that"]
        if "precis_mcp_public_credential_classes" in c
    ]


def _classes_pass_assert(value: Any) -> bool:
    return all(
        _expression(cond)(precis_mcp_public_credential_classes=value)
        for cond in _class_assertions()
    )


def _daemon_plists(**context: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    task_vars = next(
        t["vars"] for t in _tasks() if t["name"].endswith("LaunchDaemon plist")
    )
    env = task_vars["precis_web_daemon_env"]
    web = _render(**context)
    public = _render(
        **context,
        precis_web_daemon_label=task_vars["precis_web_daemon_label"],
        precis_web_daemon_command="exec true",
        precis_web_daemon_process=task_vars["precis_web_daemon_process"],
        precis_web_daemon_dsn="postgresql://agent_rw@db/precis_prod",
        precis_web_daemon_env=env,
    )
    return web, public


_OAUTH = {"PRECIS_OAUTH_ISSUER": "https://gw.example"}


def test_public_credential_classes_default_to_oauth() -> None:
    assert _defaults()["precis_mcp_public_credential_classes"] == "oauth"
    assert "precis_mcp_public_install_bearer" not in _defaults()
    assert _classes_pass_assert("oauth")


def test_both_daemons_carry_the_classes_when_the_public_mcp_is_enabled() -> None:
    for value in (_defaults()["precis_mcp_public_credential_classes"], "oauth,slice"):
        web, public = _daemon_plists(
            precis_mcp_public_enabled=True,
            precis_mcp_public_oauth_env=_OAUTH,
            precis_mcp_public_credential_classes=value,
        )
        assert public["Label"] == "com.precis.mcp-public"
        for plist in (web, public):
            assert (
                plist["EnvironmentVariables"]["PRECIS_MCP_CREDENTIAL_CLASSES"] == value
            )
            assert (
                plist["EnvironmentVariables"]["PRECIS_OAUTH_ISSUER"]
                == "https://gw.example"
            )


def test_web_plist_has_no_classes_when_the_public_mcp_is_disabled() -> None:
    web = _render(
        precis_mcp_public_enabled=False,
        precis_mcp_public_oauth_env=_OAUTH,
        precis_mcp_public_credential_classes="oauth",
    )
    assert "PRECIS_MCP_CREDENTIAL_CLASSES" not in web["EnvironmentVariables"]
    assert "PRECIS_OAUTH_ISSUER" not in web["EnvironmentVariables"]
    # The variable is never left undefined: the role default always applies.
    assert "PRECIS_MCP_CREDENTIAL_CLASSES" not in _render()["EnvironmentVariables"]


def test_the_retired_install_bearer_mode_is_gone_from_the_role() -> None:
    for path in (
        "tasks/public-mcp.yml",
        "defaults/main.yml",
        "templates/precis-web.plist.j2",
    ):
        text = (_ROLE / path).read_text(encoding="utf-8")
        assert "INSTALL_BEARER" not in text and "install_bearer" not in text


def test_token_tasks_run_only_when_install_is_listed() -> None:
    names = (
        "Generate a separate infrastructure token once",
        "Enforce dedicated token ownership and mode",
    )
    for name in names:
        assert not _when(name, "oauth")
        assert not _when(name, "oauth,slice,personal,capability")
        assert _when(name, "install")
        assert _when(name, "oauth, Install")
        assert _when(name, "oauth\tinstall")


def test_removal_task_runs_under_the_inverse_condition() -> None:
    name = "Remove the infrastructure token when install is not accepted"
    task = next(t for t in _tasks() if t["name"] == name)
    assert task["ansible.builtin.file"] == {
        "path": "{{ precis_mcp_public_secrets_dir }}/PRECIS_MCP_TOKEN",
        "state": "absent",
    }
    gen = "Generate a separate infrastructure token once"
    for classes in ("oauth", "install", "oauth,install", "slice personal", "INSTALL"):
        assert _when(name, classes) is (not _when(gen, classes))
    # The secrets directory and the blank-token plist entries stay as they were.
    secrets_dir = next(
        t for t in _tasks() if t["name"] == "Ensure the dedicated MCP secret directory"
    )
    assert "when" not in secrets_dir
    plist = next(t for t in _tasks() if t["name"].endswith("LaunchDaemon plist"))
    env = plist["vars"]["precis_web_daemon_env"]
    assert env["PRECIS_MCP_TOKEN"] == ""
    assert env["PRECIS_SECRETS_FILE_DIR"] == "{{ precis_mcp_public_secrets_dir }}"


def test_assert_accepts_every_valid_listing_and_rejects_a_typo() -> None:
    assert _classes_pass_assert("oauth")
    assert _classes_pass_assert("oauth, install\tslice")
    # this role publishes an OAuth endpoint: a list without oauth is refused
    assert not _classes_pass_assert("install")
    assert not _classes_pass_assert("personal capability")
    assert _classes_pass_assert(" ".join(_VALID_CLASSES))
    assert _classes_pass_assert("OAuth,Personal")
    assert not _classes_pass_assert("oauth,instal")
    assert not _classes_pass_assert("oauth;install")
    assert not _classes_pass_assert("bearer")
    # blank would mean "all classes" to the daemon: refused here instead
    assert not _classes_pass_assert("")
    assert not _classes_pass_assert(" , ")
    assert not _classes_pass_assert(["oauth"])


def _main_tasks() -> list[dict[str, Any]]:
    return yaml.safe_load((_ROLE / "tasks/main.yml").read_text(encoding="utf-8"))


def test_classes_are_validated_before_the_web_plist_is_written() -> None:
    """The web plist carries the class list and the web app refuses to start
    on a bad one, so the check cannot wait for the public-MCP tasks."""
    tasks = _main_tasks()
    names = [t.get("name", "") for t in tasks]
    early = names.index("Require OAuth endpoints before changing the web configuration")
    web_plist = next(
        i
        for i, t in enumerate(tasks)
        if t.get("ansible.builtin.template", {}).get("src") == "precis-web.plist.j2"
    )
    assert early < web_plist
    early_conditions = [
        c
        for c in tasks[early]["ansible.builtin.assert"]["that"]
        if "precis_mcp_public_credential_classes" in c
    ]
    assert early_conditions == _class_assertions()


def test_the_token_is_removed_only_after_the_daemon_answers_on_the_new_plist() -> None:
    names = [t["name"] for t in _tasks()]
    removal = names.index(
        "Remove the infrastructure token when install is not accepted"
    )
    assert removal > names.index(
        "Apply pending OAuth and listener configuration before exposure"
    )
    assert removal > names.index(
        "Require OAuth discovery in the unauthenticated challenge"
    )


# -- Tailscale mapping convergence (gr730563) --------------------------------

# Status document of a gateway moved from dedicated port 8443 to 443: the old
# mapping stayed public (hostname replaced).
_MOVED_STATUS: dict[str, Any] = {
    "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
    "Web": {
        "gw.example.ts.net:443": {
            "Handlers": {
                "/": {"Proxy": "http://127.0.0.1:8000"},
                "/.well-known/oauth-protected-resource/mcp": {
                    "Proxy": "http://127.0.0.1:8766/.well-known/oauth-protected-resource/mcp"
                },
                "/mcp": {"Proxy": "http://127.0.0.1:8766/mcp"},
            }
        },
        "gw.example.ts.net:8443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}
        },
    },
    "AllowFunnel": {"gw.example.ts.net:443": True, "gw.example.ts.net:8443": True},
}


def _copy(status: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(status))


def _converge_task() -> dict[str, Any]:
    return next(
        t for t in _tasks() if t["name"] == "Converge the public MCP Tailscale mapping"
    )


def _block_tasks() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _converge_task()["block"]}


def _converge_vars(
    status: dict[str, Any], https_port: int, mcp_port: int = 8766
) -> dict[str, Any]:
    """Evaluate the block vars with plain jinja2, in declaration order.

    The ansible filters the block uses are stubbed. List-shaped strings are
    parsed the way Ansible parses a templated var that looks like a list.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["from_json"] = json.loads
    env.filters["urlsplit"] = lambda url, part: getattr(urlsplit(url), part)
    context: dict[str, Any] = {
        "precis_mcp_public_https_port": https_port,
        "precis_mcp_public_port": mcp_port,
        "precis_mcp_public_oauth_env": {
            "PRECIS_OAUTH_RESOURCE": f"https://gw.example.ts.net:{https_port}/mcp"
        },
        "precis_web_podcast_base_url": "https://gw.example.ts.net",
        "_precis_mcp_public_ts_status": {"stdout": json.dumps(status)},
    }
    for name, source in _converge_task()["vars"].items():
        value: Any = env.from_string(source).render(**context).strip()
        if name in ("_precis_mcp_ts_config", "_precis_mcp_mounts", "_precis_mcp_stale"):
            value = ast.literal_eval(value)
        context[name] = value
    return context


def _stale(status: dict[str, Any], https_port: int) -> list[tuple[str, str]]:
    stale = _converge_vars(status, https_port)["_precis_mcp_stale"]
    return [(s["hostport"], s["mount"]) for s in stale]


def test_stale_set_is_the_retired_dedicated_port_when_moving_to_443() -> None:
    assert _stale(_MOVED_STATUS, 443) == [("gw.example.ts.net:8443", "/")]
    entry = _converge_vars(_MOVED_STATUS, 443)["_precis_mcp_stale"][0]
    assert entry["port"] == "8443"


def test_stale_set_is_empty_when_only_declared_mappings_exist() -> None:
    only_443 = _copy(_MOVED_STATUS)
    del only_443["Web"]["gw.example.ts.net:8443"]
    assert _stale(only_443, 443) == []
    only_8443 = _copy(_MOVED_STATUS)
    del only_8443["Web"]["gw.example.ts.net:443"]["Handlers"]["/mcp"]
    del only_8443["Web"]["gw.example.ts.net:443"]["Handlers"][
        "/.well-known/oauth-protected-resource/mcp"
    ]
    assert _stale(only_8443, 8443) == []
    assert _stale({"Web": {}}, 443) == []
    assert _stale({}, 443) == []


def test_stale_set_is_both_443_mounts_when_moving_to_the_dedicated_port() -> None:
    assert sorted(_stale(_MOVED_STATUS, 8443)) == [
        ("gw.example.ts.net:443", "/.well-known/oauth-protected-resource/mcp"),
        ("gw.example.ts.net:443", "/mcp"),
    ]


def test_stale_set_never_touches_the_web_mapping_or_other_proxies() -> None:
    status = _copy(_MOVED_STATUS)
    status["Web"]["gw.example.ts.net:10000"] = {
        "Handlers": {
            "/": {"Proxy": "http://127.0.0.1:87660"},  # longer port, not ours
            "/x": {"Proxy": "http://127.0.0.1:9999/mcp"},
            "/y": {"Proxy": "https://elsewhere.example"},
            "/z": {"Text": "no proxy"},
        }
    }
    for port in (443, 8443, 10000):
        stale = _stale(status, port)
        assert ("gw.example.ts.net:443", "/") not in stale
        assert ("gw.example.ts.net:10000", "/") not in stale
        assert all(mount not in {"/x", "/y", "/z"} for _, mount in stale)
    # Another daemon port moves the candidate set with it (here: the web one).
    other = _converge_vars(status, 443, mcp_port=8000)["_precis_mcp_stale"]
    assert [(s["hostport"], s["mount"]) for s in other] == [
        ("gw.example.ts.net:443", "/")
    ]


def test_stale_set_catches_a_hand_made_localhost_mapping() -> None:
    status = _copy(_MOVED_STATUS)
    del status["Web"]["gw.example.ts.net:8443"]
    status["Web"]["gw.example.ts.net:10000"] = {
        "Handlers": {
            "/": {"Proxy": "http://localhost:8766"},
            "/other": {"Proxy": "http://localhost:87660"},
        }
    }
    assert _stale(status, 443) == [("gw.example.ts.net:10000", "/")]


def test_cleanup_and_final_assertions_follow_the_mapping_in_order() -> None:
    names = [t["name"] for t in _converge_task()["block"]]
    assert names == [
        "Persist the public MCP HTTPS mapping",
        "Read the persistent Tailscale mappings",
        "Remove stale public MCP mappings from retired ports or mounts",
        "Re-read the persistent Tailscale mappings",
        "Verify no undeclared handler proxies to the MCP listener",
        "Verify the exact MCP mapping and preserve the web mapping",
    ]
    top = [t["name"] for t in _tasks()]
    assert top.index("Converge the public MCP Tailscale mapping") > top.index(
        "Require OAuth discovery in the unauthenticated challenge"
    )


def test_cleanup_command_and_bounds() -> None:
    tasks = _block_tasks()
    cleanup = tasks["Remove stale public MCP mappings from retired ports or mounts"]
    assert cleanup["changed_when"] is True
    assert (cleanup["async"], cleanup["poll"]) == (60, 5)
    assert cleanup["loop"] == "{{ _precis_mcp_stale }}"
    persist = tasks["Persist the public MCP HTTPS mapping"]
    assert (persist["async"], persist["poll"]) == (cleanup["async"], cleanup["poll"])

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool
    item = {"hostport": "gw.example.ts.net:8443", "port": "8443", "mount": "/"}
    for funnel, verb in ((True, "funnel"), (False, "serve")):
        argv = [
            env.from_string(a).render(item=item, precis_mcp_public_funnel=funnel)
            for a in cleanup["ansible.builtin.command"]["argv"]
        ]
        assert argv == ["tailscale", verb, "--https=8443", "--set-path=/", "off"]


def test_final_assertions_read_the_final_status_and_fail_closed() -> None:
    tasks = _block_tasks()
    names = list(tasks)
    reread = names.index("Re-read the persistent Tailscale mappings")
    assert (
        tasks["Re-read the persistent Tailscale mappings"]["register"]
        == tasks["Read the persistent Tailscale mappings"]["register"]
    )
    none_left_name = "Verify no undeclared handler proxies to the MCP listener"
    none_left = tasks[none_left_name]["ansible.builtin.assert"]
    assert names.index(none_left_name) > reread
    assert none_left["that"] == ["_precis_mcp_stale | length == 0"]
    assert " off" in none_left["fail_msg"]
    exact_name = "Verify the exact MCP mapping and preserve the web mapping"
    assert names.index(exact_name) > reread
    that = " ".join(tasks[exact_name]["ansible.builtin.assert"]["that"])
    assert "_precis_web_ts_host" in that
    assert "precis_web_funnel" in that
    assert "precis_mcp_public_funnel" in that
