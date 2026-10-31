"""claude run user (Reto ruling 2026-10-26): the mac worker's in-proc
``claude -p`` spawns drop to a dedicated non-admin user, never admin
``deploy`` — mirroring the hermes pattern on the gateway.

Mechanism under test (wrapper route): ``PRECIS_CLAUDE_BIN`` on the worker
units points at a ``sudo -n`` re-exec shim rendered by the ``claude_code``
role; the shim re-runs itself as the run user and execs the real claude
binary. deploy's own sudoers file carries the ONE wrapper grant plus a
Cmnd-scoped ``env_keep`` — the allowlist for caller-set vars: sudo's
``env_reset`` (pinned per-command so a global ``!env_reset`` can't widen
the boundary) scrubs everything else, though on macOS the stock
/etc/sudoers env_keep vars (SSH_AUTH_SOCK, EDITOR, VISUAL, TZ, MAIL,
__CF_USER_TEXT_ENCODING — none secret, HOME is re-pinned) also cross.
The run user itself must have no admin membership and no sudo grant
anywhere — which the role's deploy-time asserts enforce.

Pins:

* the role creates the user, renders the wrapper, and FAILS on admin
  membership, on any sudo grant (``sudo -l -U`` exits 0 even for a
  grant-less user — the denial text decides), on a literal sudoers.d
  grant line, and on a host whose worker binds ``PRECIS_MCP_CONFIG``;
* the rendered wrapper adds no env filter of its own — the sudoers
  ``env_keep`` (plus macOS's stock list) is the allowlist and must carry
  claude's auth vars, with ``env_reset`` pinned per-command;
* the sudoers template grants deploy the one wrapper command only, never
  grants the run user anything, lets sudo log the wrapper's argv as normal
  (per-command log suppression was dropped: CI's sudo-rs visudo rejects
  it), and stays byte-identical to today when ``claude_run_user`` is
  unset;
* every claude-spawning darwin unit env (20b collapsed, 20e agentlane, the
  dormant split-unit rollback plist) points ``PRECIS_CLAUDE_BIN`` at the
  wrapper iff the host var is set, and play 29 covers both mac claude
  hosts and is imported before 20b in redeploy-precis.yml.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

from tests._timing import budget

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the bash run-user wrapper"
)

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_CLAUDE_ROLE = _DEPLOY / "roles" / "claude_code"
_WRAPPER_TPL = _CLAUDE_ROLE / "templates" / "claude-run-user.sh.j2"
_SUDOERS_TPL = _DEPLOY / "roles" / "hardening" / "templates" / "sudoers_deploy.j2"

_RUN_USER = "claudebot"
_WRAPPER = "/opt/precis/bin/claude-run-user"

#: The env allowlist — claude's own auth, locale/terminal, the nested-
#: session marker, the MCP-startup wait knob, and the non-secret
#: tick-context vars mcp.json passthroughs expand. Anything else (the
#: worker's PRECIS_DATABASE_URL, PGPASSFILE, shared API keys) must not
#: cross.
_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "TERM",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_MCP_STARTUP_WAIT_MS",
    "PRECIS_NO_AUTOREAP",
    "PRECIS_CURRENT_AGENTLOG",
    "PRECIS_CURRENT_MODEL",
    "PRECIS_ACTING_USER",
    "PRECIS_CURRENT_TODO",
    "PRECIS_WORKSPACE",
    "PRECIS_KINDS_DISABLED",
    "PRECIS_PYTHON_ROOTS",
    "PRECIS_MCP_DB_ROLE",
    "PRECIS_MCP_DENY_VERBS",
    "PRECIS_MCP_CONFIG",
}


def _render(path: Path, **ctx: Any) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env.from_string(path.read_text(encoding="utf-8")).render(
        ansible_managed="managed", **ctx
    )


def _tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load(
        (_CLAUDE_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(tasks, list)
    return tasks


def _flatten(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the task plus anything nested under block:/rescue:/always:."""
    out = [task]
    for key in ("block", "rescue", "always"):
        for sub in task.get(key) or []:
            out.extend(_flatten(sub))
    return out


def _all_tasks() -> list[dict[str, Any]]:
    return [t for task in _tasks() for t in _flatten(task)]


def _render_wrapper() -> str:
    return _render(
        _WRAPPER_TPL,
        claude_run_user=_RUN_USER,
        claude_run_home=f"/Users/{_RUN_USER}",
        claude_bin="/opt/homebrew/bin/claude",
    )


def _render_sudoers(**ctx: Any) -> str:
    return _render(_SUDOERS_TPL, **ctx)


# ── role shape ──────────────────────────────────────────────────────────


def test_role_gates_the_run_user_path_on_darwin_and_a_pinned_uid() -> None:
    text = (_CLAUDE_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert 'os_family != "darwin"' in text
    assert "claude_run_uid" in text


def test_deploy_time_asserts_exist() -> None:
    """The ruling's hard edge: a deploy must FAIL when the run user is in
    the admin group or holds any sudo grant. ``sudo -l -U`` exits 0 even
    for a grant-less user (it prints "is not allowed to run sudo" and
    still returns true), so the check must read the text AND catch probe
    errors — never the bare ``rc == 0`` form."""
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    admin = by_name["claude run user: FAIL if it is in the admin group"]
    assert "id -Gn" in admin["ansible.builtin.command"]
    assert "'admin' in" in admin["failed_when"]

    sudo = by_name["claude run user: FAIL if it has any sudo grant"]
    assert "sudo -l -U" in sudo["ansible.builtin.command"]
    # ONE expression — a YAML list here is ANDed by Ansible, which can
    # never fail on a granted user (rc == 0 falsifies the first item).
    fw = sudo["failed_when"]
    assert isinstance(fw, str), fw
    assert "_claude_run_sudo.rc != 0" in fw
    assert "is not allowed to run sudo" in fw
    assert " or " in fw

    # Belt: a literal grant line for the user under /etc/sudoers.d fails
    # outright — grep rc 1 (no match) is the only pass.
    sudoersd = by_name[
        "claude run user: FAIL if any sudoers.d file names it as a subject"
    ]
    assert "/etc/sudoers.d" in sudoersd["ansible.builtin.command"]
    assert sudoersd["failed_when"] == "_claude_run_sudoersd.rc != 1"


def _eval_sudo_grant_failed_when(rc: int, stdout: str, stderr: str = "") -> bool:
    """Evaluate the task's actual ``failed_when`` jinja expression against a
    fake ``_claude_run_sudo`` register — a substring check alone cannot see
    whether the clause fires (the list-AND bug passed review exactly this
    way)."""
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    expr = by_name["claude run user: FAIL if it has any sudo grant"]["failed_when"]
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    rendered = env.from_string("{{ " + expr + " }}").render(
        _claude_run_sudo={"rc": rc, "stdout": stdout, "stderr": stderr}
    )
    return rendered == "True"


def test_sudo_grant_probe_fails_on_the_case_it_exists_for() -> None:
    """The three shapes ``sudo -l -U`` can return:

    * granted — rc 0 and the "may run the following commands" text → the
      task must FAIL (this is the whole point of the probe);
    * denied — the "is not allowed to run sudo" line (sudo exits 0 even
      here) → PASS;
    * probe error — rc != 0 → FAIL (never silently pass a broken probe).
    """
    assert _eval_sudo_grant_failed_when(
        0, "User claudebot may run the following commands on mac:\n    (ALL) ALL"
    )
    assert not _eval_sudo_grant_failed_when(
        0, "User claudebot is not allowed to run sudo on mac."
    )
    # Denial text may land on stderr instead — either stream counts.
    assert not _eval_sudo_grant_failed_when(
        0, "", "User claudebot is not allowed to run sudo on mac."
    )
    assert _eval_sudo_grant_failed_when(1, "", "sudo: unknown user claudebot")


def test_run_user_refused_on_a_precis_mcp_config_host() -> None:
    """A host whose worker binds PRECIS_MCP_CONFIG (the gateway group)
    would spawn claude's stdio `precis serve` child as the run user — no
    .pgpass, no DB env. The role must refuse the combination."""
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    refuse = by_name[
        "claude run user: refuse a host whose worker binds PRECIS_MCP_CONFIG"
    ]
    assert "'gateway' in group_names" in refuse["when"]
    assert "PRECIS_MCP_CONFIG" in refuse["ansible.builtin.fail"]["msg"]


def test_role_creates_user_home_and_wrapper() -> None:
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    create = by_name["claude run user: create the account"]
    assert "sysadminctl -addUser" in create["ansible.builtin.command"]
    home = by_name["claude run user: ~/.claude exists for the one-time login"]
    assert home["ansible.builtin.file"]["mode"] == "0700"
    render = by_name["claude run user: render the PRECIS_CLAUDE_BIN wrapper"]
    assert render["ansible.builtin.template"]["src"] == "claude-run-user.sh.j2"


def test_run_gid_defaults_to_uid_when_empty() -> None:
    """``claude_run_gid: ""`` is DEFINED, so a bare ``default(uid)`` never
    fires and the command renders ``-GID  -shell``; the boolean form is
    required. Render each command with the real defaults' empty gid."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    ctx = {
        "claude_run_user": _RUN_USER,
        "claude_run_uid": 555,
        "claude_run_gid": "",
        "claude_run_shell": "/bin/zsh",
    }
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    cases = {
        "claude run user: create the pinned group": "-i",
        "claude run user: create the account": "-GID",
    }
    for name, flag in cases.items():
        rendered = env.from_string(by_name[name]["ansible.builtin.command"]).render(
            **ctx
        )
        assert f"{flag} 555" in rendered, rendered
        assert f"{flag}  " not in rendered, rendered


def test_hermes_home_prep_is_gateway_only() -> None:
    """hermes exists only in the gateway group, but play 29 also targets
    scheduler — the default-true prep must not chown a nonexistent user's
    home there on every redeploy."""
    by_name = {t["name"]: t for t in _all_tasks() if "name" in t}
    (prep,) = [
        t for name, t in by_name.items() if name.startswith("Ensure hermes user's")
    ]
    assert "'gateway' in group_names" in prep["when"]


# ── wrapper render ───────────────────────────────────────────────────────


def test_wrapper_reexecs_as_the_run_user_via_sudo_n() -> None:
    out = _render_wrapper()
    assert f'/usr/bin/sudo -n -u "{_RUN_USER}" "$0" "$@"' in out
    assert f'HOME="/Users/{_RUN_USER}"' in out
    # Watchdog, not exec: claude is a background child the wrapper waits
    # on and reaps if its own parent (sudo) dies (ruling 2026-10-26). The
    # <&0 keeps the caller's stdin on the child — an async command's
    # stdin is /dev/null otherwise, which EOFs vision + gated stream-json
    # runs. INT forwards as TERM: an async child spawns with SIGINT
    # ignored and can't un-ignore it across exec.
    assert '"/opt/homebrew/bin/claude" "$@" <&0 &' in out
    assert "trap '_fwd TERM' TERM" in out
    assert "trap '_fwd TERM' INT" in out
    assert "trap '_fwd HUP' HUP" in out
    assert "ps -o ppid= -p $$" in out
    assert 'kill -TERM "$_child"' in out
    assert 'kill -KILL "$_child"' in out
    assert 'wait "$_child"' in out
    assert 'exit "$_st"' in out
    # The ppid watchdog is a background loop — the main path is a bare
    # wait, so a fast-exiting claude is not delayed by a poll tick. Its
    # fds point at /dev/null so orphaned poll `sleep`s never hold the
    # caller's pipes open past the child's death.
    assert ") </dev/null >/dev/null 2>&1 &" in out


def _render_wrapper_as_me(claude_bin: str, tmp_path: Path) -> Path:
    """Render the wrapper with the CURRENT user as the run user — the
    sudo re-exec leg skips itself, no sudo anywhere — into a file under
    ``tmp_path``."""
    wrapper = tmp_path / "claude-run-user"
    wrapper.write_text(
        _render(
            _WRAPPER_TPL,
            claude_run_user=getpass.getuser(),
            claude_run_home=str(tmp_path),
            claude_run_bin_dir="/usr/bin",
            claude_bin=claude_bin,
        ),
        encoding="utf-8",
    )
    return wrapper


def test_wrapper_passes_stdin_to_the_child(tmp_path: Path) -> None:
    """``run_claude`` pipes stdin for vision calls and ``run_claude_gated``
    writes stream-json to it — a backgrounded child would read /dev/null
    without the explicit ``<&0``. Pipe bytes through the rendered wrapper
    and assert the stub reads them."""
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("POSIX-only wrapper run")

    reader = tmp_path / "fake-claude"
    got = tmp_path / "stdin.out"
    reader.write_text(
        f"#!/usr/bin/env bash\ncat > {shlex.quote(str(got))}\n",
        encoding="utf-8",
    )
    reader.chmod(0o755)
    wrapper = _render_wrapper_as_me(str(reader), tmp_path)

    res = subprocess.run(
        ["bash", str(wrapper)],
        input=b"hello-from-stdin",
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert got.read_bytes() == b"hello-from-stdin"


def test_wrapper_returns_promptly_with_the_childs_status(tmp_path: Path) -> None:
    """The parent-death poll runs in a background loop; the main path is a
    bare ``wait`` — a child that exits at once must return in well under a
    2 s poll tick, carrying the child's own exit status."""
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("POSIX-only wrapper run")

    quick = tmp_path / "fake-claude"
    quick.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    quick.chmod(0o755)
    wrapper = _render_wrapper_as_me(str(quick), tmp_path)

    started = time.monotonic()
    res = subprocess.run(
        ["bash", str(wrapper)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 7
    assert time.monotonic() - started < budget(1.0)


def test_wrapper_watchdog_reaps_the_child_when_its_parent_dies(
    tmp_path: Path,
) -> None:
    """Real run of the rendered wrapper under bash — the run user rendered
    as the CURRENT user so the sudo re-exec leg skips, no sudo anywhere.
    A stand-in bash plays the parent (sudo); killing it must make the
    watchdog TERM the orphaned claude-stub child within ~10 s."""
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("POSIX-only watchdog run")

    sleeper = tmp_path / "fake-claude"
    child_pid = tmp_path / "child.pid"
    sleeper.write_text(
        "#!/usr/bin/env bash\n"
        f"echo $$ > {shlex.quote(str(child_pid))}\n"
        "exec sleep 30\n",
        encoding="utf-8",
    )
    sleeper.chmod(0o755)

    wrapper = _render_wrapper_as_me(str(sleeper), tmp_path)

    # Stand-in "sudo": a bash that keeps the wrapper as its child. Killing
    # it reparents the wrapper to launchd (ppid 1) — what a dead sudo
    # looks like to the watchdog.
    parent = subprocess.Popen(
        ["bash", "-c", f"bash {shlex.quote(str(wrapper))} -p hi; exit 0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not child_pid.exists():
        if time.monotonic() > deadline:
            parent.kill()
            pytest.fail("wrapper never spawned the claude stub")
        time.sleep(0.1)

    parent.kill()
    parent.wait(timeout=5)
    pid = int(child_pid.read_text(encoding="utf-8").strip())

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        pytest.fail("watchdog never reaped the orphaned claude stub")


def test_wrapper_has_no_env_filter_of_its_own() -> None:
    """The sudoers ``env_keep`` is the caller-set allowlist — an ``env -i``
    re-filter would only duplicate it and put the OAuth token in env's
    argv, where ``ps`` shows it to every local user. The wrapper pins
    HOME/PATH and execs; no ``${VAR…}`` passthroughs remain."""
    out = _render_wrapper()
    assert "env -i" not in out
    assert "${" not in out
    for forbidden in ("PRECIS_DATABASE_URL", "PGPASSFILE", "PRECIS_SHARED"):
        assert ("$" + forbidden) not in out


# ── sudoers render ───────────────────────────────────────────────────────


def test_sudoers_is_unchanged_when_run_user_unset() -> None:
    out = _render_sudoers()
    assert "NOPASSWD: /opt/precis/bin/claude-run-user" not in out
    assert "env_keep" not in out
    assert "deploy ALL=(ALL) NOPASSWD: ALL" in out


def test_sudoers_grants_deploy_only_the_wrapper_as_the_run_user() -> None:
    out = _render_sudoers(claude_run_user=_RUN_USER)
    assert f"deploy ALL=({_RUN_USER}) NOPASSWD: {_WRAPPER}" in out
    # The run user itself must never be a grantee: it may appear only in
    # the (runas) position of deploy's own rule.
    for line in out.splitlines():
        assert not line.startswith(_RUN_USER), line
        assert f"{_RUN_USER} ALL" not in line, line


def test_sudoers_env_keep_is_the_allowlist() -> None:
    """env_reset makes the Cmnd-scoped env_keep the allowlist for
    caller-set vars — a var sudo strips at the boundary can never reach
    claude (macOS's stock /etc/sudoers env_keep vars also cross, but none
    are secrets and HOME is re-pinned). The list must equal the
    documented set: claude's auth, locale/terminal, the nested-session
    marker, the MCP-startup wait knob, and non-secret tick context."""
    out = _render_sudoers(claude_run_user=_RUN_USER)
    (line,) = [
        line
        for line in out.splitlines()
        if line.startswith("Defaults!") and "env_keep" in line
    ]
    assert line.startswith(f"Defaults!{_WRAPPER}")
    m = re.search(r'env_keep \+= "([^"]+)"', line)
    assert m is not None
    kept = set(m.group(1).split())
    assert kept == _ALLOWLIST
    # claude's auth must be in the allowlist or every call 401s.
    assert "CLAUDE_CODE_OAUTH_TOKEN" in kept


def test_sudoers_pins_env_reset_for_the_wrapper() -> None:
    """The env_keep allowlist only means anything while env_reset holds.
    Pin it per-command: a future global ``!env_reset`` in /etc/sudoers
    must not let the worker's whole env — PRECIS_DATABASE_URL, shared
    keys — across the boundary."""
    out = _render_sudoers(claude_run_user=_RUN_USER)
    assert f"Defaults!{_WRAPPER} env_reset" in out.splitlines()


def test_rendered_sudoers_parses_under_visudo(tmp_path: Path) -> None:
    """The Defaults lines only protect anything if sudo accepts the file —
    run visudo's syntax check on the render when the binary is on the
    host (``-cf`` needs no root; the wrapper-path warning is not a parse
    failure)."""
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not on this host")
    sudoers = tmp_path / "sudoers_deploy"
    sudoers.write_text(_render_sudoers(claude_run_user=_RUN_USER), encoding="utf-8")
    sudoers.chmod(0o440)
    res = subprocess.run(
        [visudo, "-cf", str(sudoers)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_sudoers_has_no_per_command_log_defaults() -> None:
    """sudo logs the wrapper's argv as normal; per-command log suppression
    was dropped because CI's sudo-rs visudo rejects it."""
    out = _render_sudoers(claude_run_user=_RUN_USER)
    assert "!syslog" not in out
    assert "log_allowed" not in out
    defaults = [ln for ln in out.splitlines() if ln.startswith(f"Defaults!{_WRAPPER}")]
    assert all(ln.split()[1] in ("env_reset", "env_keep") for ln in defaults)


# ── unit env wiring ─────────────────────────────────────────────────────


def test_collapsed_unit_env_points_precis_claude_bin_at_the_wrapper() -> None:
    text = (_DEPLOY / "playbooks/20b-precis-worker-collapsed.yml").read_text(
        encoding="utf-8"
    )
    assert "'PRECIS_CLAUDE_BIN'" in text
    assert "claude_run_wrapper | default('/opt/precis/bin/claude-run-user')" in text
    assert "claude_run_user | default('')" in text


def test_agentlane_env_and_rollback_plist_mirror_the_wrapper() -> None:
    lane = (_DEPLOY / "playbooks/20e-precis-worker-agentlane.yml").read_text(
        encoding="utf-8"
    )
    assert "'PRECIS_CLAUDE_BIN'" in lane
    plist = (
        _DEPLOY / "roles/precis_worker/templates/precis-worker.plist.j2"
    ).read_text(encoding="utf-8")
    assert "<key>PRECIS_CLAUDE_BIN</key>" in plist
    assert "claude_run_user | default('')" in plist


def test_play_29_covers_both_mac_claude_hosts() -> None:
    play = yaml.safe_load(
        (_DEPLOY / "playbooks/29-claude-code.yml").read_text(encoding="utf-8")
    )[0]
    assert set(play["hosts"]) == {"gateway", "scheduler"}
    assert "claude_code" in play["roles"]


def test_redeploy_imports_29_before_the_worker_render() -> None:
    """redeploy-precis.yml must provision the wrapper BEFORE 20b renders
    PRECIS_CLAUDE_BIN at it — same silent-drift rule as pgpass/logrotate."""
    entries = yaml.safe_load(
        (_DEPLOY / "redeploy-precis.yml").read_text(encoding="utf-8")
    )
    imports = [
        e["ansible.builtin.import_playbook"]
        for e in entries
        if isinstance(e, dict) and "ansible.builtin.import_playbook" in e
    ]
    assert imports.index("playbooks/29-claude-code.yml") < imports.index(
        "playbooks/20b-precis-worker-collapsed.yml"
    )
    site = (_DEPLOY / "site.yml").read_text(encoding="utf-8")
    assert site.index("29-claude-code.yml") < site.index(
        "20b-precis-worker-collapsed.yml"
    )
