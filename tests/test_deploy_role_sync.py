"""scripts/deploy runs `02-postgres.yml --tags roles` before migrate (gr583700).

r241 died at the migrate-first play on `password authentication failed`:
the db-own roles had no LOGIN/password and pgbouncer's userlist lacked
them, because those only materialise through playbooks/02-postgres.yml —
which redeploy-precis.yml never imports. A FULL 02-postgres run also
carries postgresql.conf drift that notifies `restart postgresql`, so the
controller runs ONLY the `roles`-tagged slice, and only when it matters:

  a) the resolved overlay's hash differs from the `overlay=` hash the
     deploy-state marker recorded at the last green deploy (role edits
     move no git sha — that hash is the only signal), or
  b) the overlay's merged vars declare `precis_db_own_cutover_done: true`
     (read through `ansible-inventory --host <gateway> --yaml`, because
     the flag lives in group_vars/all, which an awk over hosts.yml can
     never see) AND the deploy-state marker carries no `roles_synced=1`
     stamp. The stamp is written by the same success write that records
     `overlay=`, so it means "the roles slice ran green at least once at
     this overlay hash" — carried forward only while the hash does not
     move.

A red sync dies before the migrate play is ever reached; a deploy that
fails after a green sync writes no marker and therefore no stamp — the
next run re-syncs.

Two layers: unit tests drive the extracted `_deploy_role_sync` block in
a ``bash -c`` harness (same pattern as tests/test_deploy_ansible_path.py);
integration tests drive the whole script through the fake-ansible fixture
of tests/test_deploy_render_worktree.py.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_render_worktree import (
    Fixture,
    _git,
    _lib_call,
    _run_deploy,
    build_fixture,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

DEPLOY = Path(__file__).resolve().parent.parent / "scripts" / "deploy"
PGB_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "pgb-host.sh"


# ── unit layer: the extracted _deploy_role_sync block ───────────────────


def _funcs() -> str:
    m = re.search(
        r"# BEGIN _deploy_role_sync.*?\n(.*?)# END _deploy_role_sync",
        DEPLOY.read_text(encoding="utf-8"),
        re.S,
    )
    assert m, "_deploy_role_sync markers missing from scripts/deploy"
    return m.group(1)


_HOSTS = """\
all:
  children:
    gateway:
      hosts:
        node-gw:
          ansible_host: 192.0.2.10
    data:
      hosts:
        node-data:
          ansible_host: 192.0.2.12
"""


def _overlay(tmp_path: Path, gvars: str | None) -> Path:
    """A fake overlay dir: hosts.yml (groups, no flag — the flag must come
    from group_vars, which is the whole point of item 2) plus an optional
    group_vars/all/main.yml serving the merged-var read."""
    inv = tmp_path / "inv"
    (inv / "group_vars" / "all").mkdir(parents=True)
    (inv / "hosts.yml").write_text(_HOSTS, encoding="utf-8")
    if gvars is not None:
        (inv / "group_vars" / "all" / "main.yml").write_text(gvars, encoding="utf-8")
    return inv


_FAKE_APB = (
    "#!/usr/bin/env bash\n"
    'printf "ARGS=%s\\n" "$*" >> "$SYNC_RECORD"\n'
    'exit "${APB_RC:-0}"\n'
)

# ansible-inventory serves the merged host vars: for every `-i <path>` it
# cats <dir>/group_vars/all/main.yml, the way real ansible merges the
# overlay's group_vars into the host's var set. FAKE_INV_FORMAT=json wraps
# the same vars as a JSON object (older ansible-core ignores --yaml for
# --host); FAKE_INV_STDOUT, when SET (even empty), replaces the dump.
_FAKE_ANSIBLE_INVENTORY = (
    "#!/usr/bin/env bash\n"
    'printf "INVENTORY=%s\\n" "$*" >> "$SYNC_RECORD"\n'
    'if [ "${FAKE_INV_RC:-0}" != "0" ]; then exit "$FAKE_INV_RC"; fi\n'
    'if [ "${FAKE_INV_STDOUT+x}" = "x" ]; then\n'
    '    printf "%s\\n" "$FAKE_INV_STDOUT"; exit 0\n'
    "fi\n"
    'prev=""\n'
    'for a in "$@"; do\n'
    '    if [ "$prev" = "-i" ] && [ -f "$(dirname "$a")/group_vars/all/main.yml" ]; then\n'
    '        f="$(dirname "$a")/group_vars/all/main.yml"\n'
    '        if [ "${FAKE_INV_FORMAT:-yaml}" = "json" ]; then\n'
    '            printf "{\\n"\n'
    "            while IFS= read -r line; do\n"
    '                [ -n "$line" ] || continue\n'
    '                printf \'  \\"%s\\":%s,\\n\' "${line%%:*}" "${line#*:}"\n'
    '            done < "$f"\n'
    '            printf "}\\n"\n'
    "        else\n"
    '            cat "$f"\n'
    "        fi\n"
    "    fi\n"
    '    prev="$a"\n'
    "done\n"
    "exit 0\n"
)

_FAKE_ANSIBLE = (
    '#!/usr/bin/env bash\nprintf "ANSIBLE=%s\\n" "$*" >> "$SYNC_RECORD"\nexit 0\n'
)


def _sync_env(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin-sync"
    bindir.mkdir(exist_ok=True)
    for name, body in (
        ("ansible-playbook", _FAKE_APB),
        ("ansible-inventory", _FAKE_ANSIBLE_INVENTORY),
        ("ansible", _FAKE_ANSIBLE),
    ):
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    return bindir


def _run_sync(
    tmp_path: Path,
    inv: Path,
    *,
    overlay_changed: bool = False,
    prev_synced: bool = False,
    apb_rc: int = 0,
    inv_rc: int = 0,
    inv_args: str = "-i /ov/hosts.yml --vault-password-file /ov/.vault-pass",
    inv_format: str | None = None,
    inv_stdout: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bindir = _sync_env(tmp_path)
    record = tmp_path / "sync-record.txt"
    # The block under test is driven with the REAL pgb-host.sh so
    # inventory_group_hosts reads the fixture hosts.yml — only the
    # ansible binaries are stubs.
    script = f"""
set -euo pipefail
say() {{ printf 'say: %s\\n' "$*"; }}
die() {{ printf 'DIE: %s\\n' "$*" >&2; exit 1; }}
. '{PGB_LIB}'
{_funcs()}
INV_ARGS=({inv_args})
_OVERLAY_CHANGED={"1" if overlay_changed else ""}
_PREV_ROLES_SYNCED={"1" if prev_synced else ""}
_deploy_role_sync
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "SYNC_RECORD": str(record),
            "APB_RC": str(apb_rc),
            "FAKE_INV_RC": str(inv_rc),
            "CLUSTER_INVENTORY": str(inv / "hosts.yml"),
            **({"FAKE_INV_FORMAT": inv_format} if inv_format is not None else {}),
            **({"FAKE_INV_STDOUT": inv_stdout} if inv_stdout is not None else {}),
        },
    )


def _record(tmp_path: Path) -> str:
    record = tmp_path / "sync-record.txt"
    return record.read_text(encoding="utf-8") if record.exists() else ""


def test_sync_runs_when_the_overlay_hash_changed(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: false\n")
    result = _run_sync(tmp_path, inv, overlay_changed=True)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: ran 02-postgres --tags roles (a)" in out, out
    assert "playbooks/02-postgres.yml --tags roles" in _record(tmp_path)


def test_sync_runs_when_cutover_flag_set_and_marker_unstamped(
    tmp_path: Path,
) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: ran 02-postgres --tags roles (b)" in out, out


def test_both_reasons_report_as_a_plus_b(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, overlay_changed=True)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "(a+b)" in out, out


def test_no_sync_when_neither_trigger_holds(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: false\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_an_absent_flag_is_quiet_false(tmp_path: Path) -> None:
    # group_vars exists but names no flag: a legitimate read of "not
    # cutover" — no sync AND no "could not read" warning.
    inv = _overlay(tmp_path, "unrelated_var: 1\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)
    assert "could not read" not in result.stderr, out


def test_a_stamped_marker_suppresses_reason_b(tmp_path: Path) -> None:
    # flag true + stamped marker + unchanged hash: roles are already in
    # sync — nothing runs.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, prev_synced=True)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_a_stamped_marker_with_a_changed_hash_still_syncs(tmp_path: Path) -> None:
    # flag true + stamped marker + CHANGED hash: the overlay moved, so the
    # stamp says nothing — reason (a) alone (the stamp suppresses b).
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, overlay_changed=True, prev_synced=True)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: ran 02-postgres --tags roles (a)" in out, out
    assert "a+b" not in out, out


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", '"yes"', "Yes"])
def test_cutover_flag_value_variants_count_as_true(tmp_path: Path, value: str) -> None:
    inv = _overlay(tmp_path, f"precis_db_own_cutover_done: {value}\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "(b)" in out, out


@pytest.mark.parametrize("value", ["false", "False", "no", "0"])
def test_cutover_flag_false_variants_mean_no_sync(tmp_path: Path, value: str) -> None:
    inv = _overlay(tmp_path, f"precis_db_own_cutover_done: {value}\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)
    assert "could not read" not in result.stderr, out


def test_a_json_dump_with_the_flag_true_syncs(tmp_path: Path) -> None:
    # Older ansible-core ignores `--yaml` for `--host` and prints JSON:
    # `"precis_db_own_cutover_done": true,` must read the same as YAML.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, inv_format="json")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: ran 02-postgres --tags roles (b)" in out, out


def test_a_json_dump_with_the_flag_false_means_no_sync(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: false\n")
    result = _run_sync(tmp_path, inv, inv_format="json")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)
    assert "could not read" not in result.stderr, out


def test_empty_inventory_output_dies_loud(tmp_path: Path) -> None:
    # An empty dump must never read as a quiet "flag absent" — the deploy
    # dies where the operator can see why.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, inv_stdout="")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "role-sync: unparseable ansible-inventory --host output" in out, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_garbage_inventory_output_dies_loud(tmp_path: Path) -> None:
    # `ERROR! ...` on stdout with rc 0 is not a var dump either — same die.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(
        tmp_path, inv, inv_stdout="ERROR! unable to parse inventory source"
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "role-sync: unparseable ansible-inventory --host output" in out, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_an_unrecognised_flag_value_warns_and_skips(tmp_path: Path) -> None:
    # A parseable dump whose value is neither true-ish nor false-ish keeps
    # the existing visible-warning path: not cutover, but loudly.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: maybe\n")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (
        "could not read precis_db_own_cutover_done (unexpected value 'maybe')"
        in result.stderr
    ), out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_an_unreadable_flag_warns_and_skips(tmp_path: Path) -> None:
    # ansible-inventory itself fails: cannot read counts as NOT cutover —
    # but loudly, so the silence is visible.
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: true\n")
    result = _run_sync(tmp_path, inv, inv_rc=2)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: could not read precis_db_own_cutover_done" in result.stderr, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_a_missing_gateway_host_warns_and_skips(tmp_path: Path) -> None:
    inv = tmp_path / "inv"
    inv.mkdir()
    (inv / "hosts.yml").write_text("all:\n", encoding="utf-8")
    result = _run_sync(tmp_path, inv)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "role-sync: could not read precis_db_own_cutover_done" in result.stderr, out
    assert "ARGS=" not in _record(tmp_path), _record(tmp_path)


def test_a_failing_sync_dies(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: false\n")
    result = _run_sync(tmp_path, inv, overlay_changed=True, apb_rc=2)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "DIE:" in out, out


def test_the_sync_argv_is_the_tagged_playbook_plus_inv_args(tmp_path: Path) -> None:
    inv = _overlay(tmp_path, "precis_db_own_cutover_done: false\n")
    result = _run_sync(tmp_path, inv, overlay_changed=True)
    assert result.returncode == 0, result.stderr
    lines = [
        line[len("ARGS=") :]
        for line in _record(tmp_path).splitlines()
        if line.startswith("ARGS=")
    ]
    assert lines == [
        "playbooks/02-postgres.yml --tags roles"
        " -i /ov/hosts.yml --vault-password-file /ov/.vault-pass"
    ], lines


# ── integration layer: whole-script runs through the fake-ansible fixture ──

# The flag read is an `ansible-inventory --host` call; the fake serves the
# overlay's group_vars merge like the real merged-vars read.
_FAKE_ANSIBLE = """#!/usr/bin/env bash
printf 'ANSIBLE=%s\\n' "$*" >> "$RENDER_RECORD"
for a in "$@"; do
    if [ "$a" = "--list-hosts" ]; then
        set -- ${FAKE_CRITICAL:-}
        printf '  hosts (%d):\\n' "$#"
        for h in "$@"; do printf '    %s\\n' "$h"; done
        exit 0
    fi
done
rc=0
for h in gateway scheduler data inference serving; do
    case " ${FAKE_PING_DOWN:-} " in
        *" $h "*) printf '%s | UNREACHABLE! => {}\\n' "$h"; rc=1 ;;
        *) printf '%s | SUCCESS => {"ping": "pong"}\\n' "$h" ;;
    esac
done
exit "$rc"
"""

_FAKE_ANSIBLE_INVENTORY = """#!/usr/bin/env bash
printf 'INVENTORY=%s\\n' "$*" >> "$RENDER_RECORD"
if [ "${FAKE_INV_RC:-0}" != "0" ]; then exit "$FAKE_INV_RC"; fi
if [ "${FAKE_INV_STDOUT+x}" = "x" ]; then
    printf "%s\\n" "$FAKE_INV_STDOUT"; exit 0
fi
prev=""
for a in "$@"; do
    if [ "$prev" = "-i" ] && [ -f "$(dirname "$a")/group_vars/all/main.yml" ]; then
        f="$(dirname "$a")/group_vars/all/main.yml"
        if [ "${FAKE_INV_FORMAT:-yaml}" = "json" ]; then
            printf '{\\n'
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                printf '  "%s":%s,\\n' "${line%%:*}" "${line#*:}"
            done < "$f"
            printf '}\\n'
        else
            cat "$f"
        fi
    fi
    prev="$a"
done
exit 0
"""

_FAKE_ANSIBLE_PLAYBOOK = """#!/usr/bin/env bash
{
  printf 'PWD=%s\\n' "$PWD"
  printf 'ARGS=%s\\n' "$*"
} >> "$RENDER_RECORD"
case " $* " in
  *02-postgres*)
    printf 'TASK [postgres : Create database roles] *****************\\nfatal: [data]: FAILED! => {"msg": "auth failed"}\\n'
    exit "${FAKE_ROLE_SYNC_RC:-0}" ;;
esac
if [ -n "${FAKE_PLAYBOOK_DIES:-}" ]; then
  printf 'TASK [bounce daemons] *********************************\\nfatal: [data]: FAILED! => {"msg": "boom"}\\n'
  exit 1
fi
cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0
data                       : ok=8    changed=1    unreachable=0    failed=0
RECAP
exit "${FAKE_PLAYBOOK_RC:-0}"
"""

_FAKE_ANSIBLE_DOC = """#!/usr/bin/env bash
exit 0
"""


def _make_fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ansible").write_text(_FAKE_ANSIBLE, encoding="utf-8")
    (bindir / "ansible-playbook").write_text(_FAKE_ANSIBLE_PLAYBOOK, encoding="utf-8")
    (bindir / "ansible-inventory").write_text(_FAKE_ANSIBLE_INVENTORY, encoding="utf-8")
    (bindir / "ansible-doc").write_text(_FAKE_ANSIBLE_DOC, encoding="utf-8")
    for name in ("ansible", "ansible-playbook", "ansible-inventory", "ansible-doc"):
        (bindir / name).chmod(0o755)
    return bindir


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    fixture = build_fixture(tmp_path)
    # The flag read resolves the overlay through pgb-host.sh; the shared
    # fixture copies only the libs its own tests need.
    (fixture.repo / "scripts" / "lib" / "pgb-host.sh").write_text(
        PGB_LIB.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return fixture


def _set_marker(
    fx: Fixture, sha: str, overlay: str = "", roles_synced: bool = False
) -> None:
    marker = Path(_lib_call("deploy_state_path", str(fx.repo)))
    marker.parent.mkdir(parents=True, exist_ok=True)
    suffix = f" overlay={overlay}" if overlay else ""
    suffix += " roles_synced=1" if roles_synced else ""
    marker.write_text(f"{sha} 1700000000 success{suffix}\n", encoding="utf-8")


def _marker_text(fx: Fixture) -> str:
    return Path(_lib_call("deploy_state_path", str(fx.repo))).read_text(
        encoding="utf-8"
    )


def _marker_bytes(fx: Fixture) -> bytes | None:
    """Marker content as raw bytes; None when the file does not exist."""
    p = Path(_lib_call("deploy_state_path", str(fx.repo)))
    return p.read_bytes() if p.exists() else None


def _commit_inventory(fx: Fixture, hosts_yml: str, gvars: str | None = None) -> str:
    (fx.repo / "deploy" / "inventory" / "hosts.yml").write_text(
        hosts_yml, encoding="utf-8"
    )
    if gvars is not None:
        gvdir = fx.repo / "deploy" / "inventory" / "group_vars" / "all"
        gvdir.mkdir(parents=True, exist_ok=True)
        (gvdir / "main.yml").write_text(gvars, encoding="utf-8")
    _git(fx.repo, "add", "-A")
    _git(fx.repo, "commit", "-q", "-m", "inventory update")
    return _git(fx.repo, "rev-parse", "HEAD").stdout.strip()


_CUTOVER_HOSTS = """\
all:
  children:
    gateway:
      hosts:
        node-gw:
          ansible_host: 192.0.2.10
    data:
      hosts:
        node-data:
          ansible_host: 192.0.2.12
"""

_CUTOVER_GVARS = "precis_db_own_cutover_done: true\n"


def _deploy(
    fx: Fixture,
    tmp_path: Path,
    sha: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {"FAKE_CRITICAL": "gateway data"}
    env.update(extra_env or {})
    return _run_deploy(
        fx,
        _make_fake_bin(tmp_path),
        tmp_path / "record.txt",
        sha,
        *args,
        extra_env=env,
    )


def test_deploy_syncs_roles_when_the_overlay_hash_moved(
    fx: Fixture, tmp_path: Path
) -> None:
    _set_marker(fx, fx.gated, overlay="0" * 64)
    result = _deploy(fx, tmp_path, fx.gated, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    sync = record.index("playbooks/02-postgres.yml --tags roles")
    redeploy = record.index("redeploy-precis.yml")
    assert sync < redeploy, "the role-sync must run before the rollout"
    assert "role-sync: ran 02-postgres --tags roles (a)" in out, out


def test_deploy_syncs_roles_and_stamps_when_cutover_unstamped(
    fx: Fixture, tmp_path: Path
) -> None:
    # cutover flag in group_vars (NOT hosts.yml — that is the point) plus a
    # marker with no roles_synced stamp: reason (b) fires.
    sha = _commit_inventory(fx, _CUTOVER_HOSTS, _CUTOVER_GVARS)
    _set_marker(fx, sha)  # no overlay= field: trigger (a) cannot fire
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "playbooks/02-postgres.yml --tags roles" in record, record
    assert "role-sync: ran 02-postgres --tags roles (b)" in out, out
    # The green deploy's marker write stamps it.
    assert " roles_synced=1" in _marker_text(fx), _marker_text(fx)


def test_deploy_skips_the_sync_when_nothing_triggered_it(
    fx: Fixture, tmp_path: Path
) -> None:
    # First deploy writes the marker with the real overlay hash; the second
    # sees it unchanged and the cutover flag is absent — neither trigger.
    _set_marker(fx, fx.gated)
    first = _deploy(fx, tmp_path, fx.gated, "--pinned")
    assert first.returncode == 0, first.stdout + first.stderr
    assert " overlay=" in _marker_text(fx), "the marker must record the overlay hash"

    (tmp_path / "record.txt").unlink()
    second = _deploy(fx, tmp_path, fx.gated, "--pinned")
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "02-postgres" not in record, record
    assert "role-sync: ran" not in out, out


def test_a_stamped_marker_suppresses_the_sync_and_is_carried(
    fx: Fixture, tmp_path: Path
) -> None:
    # cutover flag + a first deploy that syncs and stamps; the second
    # deploy at the same overlay hash must not re-sync — and the stamp
    # must carry into the new marker.
    sha = _commit_inventory(fx, _CUTOVER_HOSTS, _CUTOVER_GVARS)
    _set_marker(fx, sha)
    first = _deploy(fx, tmp_path, sha, "--pinned")
    assert first.returncode == 0, first.stdout + first.stderr
    assert " roles_synced=1" in _marker_text(fx)

    (tmp_path / "record.txt").unlink()
    second = _deploy(fx, tmp_path, sha, "--pinned")
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "02-postgres" not in record, record
    assert " roles_synced=1" in _marker_text(fx), "the stamp must carry forward"


def test_a_stamped_marker_with_a_moved_overlay_still_syncs(
    fx: Fixture, tmp_path: Path
) -> None:
    # The stamp only ever means "synced at THIS hash": a moved overlay
    # triggers (a) even though the marker is stamped — and the fresh green
    # sync re-stamps at the new hash.
    sha = _commit_inventory(fx, _CUTOVER_HOSTS, _CUTOVER_GVARS)
    _set_marker(fx, sha, overlay="0" * 64, roles_synced=True)
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "playbooks/02-postgres.yml --tags roles" in record, record
    assert "role-sync: ran 02-postgres --tags roles (a)" in out, out
    assert " roles_synced=1" in _marker_text(fx), _marker_text(fx)


def test_a_failing_sync_dies_before_the_migrate_play(
    fx: Fixture, tmp_path: Path
) -> None:
    _set_marker(fx, fx.gated, overlay="0" * 64)
    marker_before = _marker_bytes(fx)
    assert marker_before is not None
    result = _deploy(
        fx,
        tmp_path,
        fx.gated,
        "--pinned",
        extra_env={"FAKE_ROLE_SYNC_RC": "1"},
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "playbooks/02-postgres.yml --tags roles" in record, record
    assert "redeploy-precis.yml" not in record, (
        "the migrate-first play was reached after a red role-sync:\n" + record
    )
    # The marker is BYTE-identical — no stamp, no rewrite of any kind.
    assert _marker_bytes(fx) == marker_before, (
        f"marker changed across a red sync: {marker_before!r} -> {_marker_bytes(fx)!r}"
    )


def test_a_failed_rollout_writes_no_stamp(fx: Fixture, tmp_path: Path) -> None:
    # The sync ran green but the rollout died: the marker write is the
    # only place the stamp can come from, so it stays unstamped and the
    # next run re-syncs.
    sha = _commit_inventory(fx, _CUTOVER_HOSTS, _CUTOVER_GVARS)
    _set_marker(fx, sha)
    marker_before = _marker_bytes(fx)
    assert marker_before is not None
    result = _deploy(
        fx,
        tmp_path,
        sha,
        "--pinned",
        extra_env={"FAKE_PLAYBOOK_DIES": "1"},
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert "playbooks/02-postgres.yml --tags roles" in record, record
    assert _marker_bytes(fx) == marker_before, (
        f"marker changed across a failed rollout: {marker_before!r} -> {_marker_bytes(fx)!r}"
    )
