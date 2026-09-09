"""Regression tests for gr332009 — scripts/ship's deploy-lag footer fabricating
a growing "N commits not yet deployed" count whenever the newest deploy ended
red on a persistent RESIDUAL task (e.g. an un-provisioned sandbox host's image
rebuild), because scripts/deploy only ever wrote its deploy-state marker as
its LAST step, gated behind every ``|| die`` above it — so a real,
fully-converged rollout left no fresh marker at all and ship's lag report
went stale or wrong.

This landed in two independent commits that collided on `main`:

  - 62d570943 (a sibling session, merged first): the ATTEMPT-STAMP design.
    scripts/deploy writes an attempt stamp (`deploy_attempt_path` →
    `.git/precis-deploy-attempt`, "<sha> <epoch>") the moment it starts
    touching hosts; a fully green deploy replaces it with the success marker
    (`deploy_state_path`) and removes the stamp. `deploy_state_read_path` has
    NO legacy per-worktree fallback any more (an absent marker means "no
    successful deploy on record", not "go dig up an old file"). scripts/ship
    reports "deploy state uncertain" when a stamp exists with no marker at
    least as new.
  - This session's own gr332009 fix (superseded as the base, kept as a graft):
    the ROLLOUT-CONVERGENCE mechanism — `_run_rollout_playbook`/
    `_rollout_converged` in scripts/deploy, which reads the ansible-playbook
    run's own PLAY RECAP and writes the success marker (clearing the attempt
    stamp) as soon as every ROLLOUT host converges, even when the whole
    invocation still exits non-zero because a LATER, unrelated play died on a
    host outside that set (the balthazar sandbox podman-pull residual).
    Without this graft, the attempt-stamp design alone still leaves every
    ship footer saying "uncertain" forever the moment that residual task goes
    red — the stamp never clears because step 3 (the success write) never
    runs on a non-zero exit.

Resolution: the sibling's attempt-stamp/no-legacy-fallback design is the
base (scripts/ship, scripts/lib/deploy-state.sh unchanged from it); this
session's rollout-convergence mechanism is grafted into scripts/deploy on
top, scoped to the single-pass path only (`PRECIS_DEPLOY_CANARY` unset).

Cases covered below:
  (a) converged rollout + failed residual play → success marker written,
      attempt stamp removed, script still exits non-zero (the graft).
  (b) genuine rollout-host failure → no success marker, attempt stamp
      remains (the graft's fail-safe direction).
  (c) full success → success marker written, attempt stamp removed (the
      base design, unchanged — non-regression).
  (d) ship footer: an attempt stamp with no (or no newer) marker → the
      base design's "deploy state uncertain" wording, copied verbatim.
  (e) a current success marker, no pending attempt → the ordinary commit
      count line.
  (f) neither a marker nor a stamp → "no successful deploy on record" (the
      base design's third state — a cheap non-regression pin).

Exercised against the REAL scripts, never reimplemented:
  - scripts/deploy: copied byte-for-byte into a throwaway repo at its real
    relative path (it resolves its own REPO_ROOT off `dirname "$0"/..`),
    driven with fake `ansible`/`ansible-playbook` binaries on $PATH standing
    in for the cluster — never the real thing.
  - scripts/ship: its function-definitions-only PRELUDE (everything before
    "── 1. sanity", which never does anything invasive the moment it's
    sourced/run — no commits, no locks, no gate) plus, for the footer-text
    tests, its actual "── 8." print block — sliced directly out of the
    shipped file's own text, never rewritten.
  - scripts/lib/deploy-state.sh: sourced directly (its functions take the
    repo root as an explicit argument, so no throwaway copy is needed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "scripts" / "deploy"
SHIP_SRC = REPO_ROOT / "scripts" / "ship"
DEPLOY_STATE_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "deploy-state.sh"


def _test_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("root\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")


def _commit(path: Path, name: str) -> str:
    (path / name).write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", name)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _lib_call(fn: str, *args: str) -> str:
    """Invoke a single function out of the REAL deploy-state.sh directly —
    used to compute the exact marker/attempt paths the scripts themselves
    would compute, rather than reimplementing that path arithmetic here."""
    quoted_args = " ".join(f"'{a}'" for a in args)
    result = subprocess.run(
        ["bash", "-c", f"source '{DEPLOY_STATE_LIB_SRC}'; {fn} {quoted_args}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _marker_path(repo: Path) -> Path:
    return Path(_lib_call("deploy_state_path", str(repo)))


def _attempt_path(repo: Path) -> Path:
    return Path(_lib_call("deploy_attempt_path", str(repo)))


# ─────────────────────────── scripts/deploy graft ────────────────────────────

# The fake ping always reports the same 5 rollout hosts as reachable,
# regardless of what group pattern scripts/deploy actually passed — good
# enough for exercising the marker-write seam, which only cares about the
# host LIST, not real ansible ping semantics.
_FAKE_ANSIBLE = """#!/usr/bin/env bash
for h in gateway scheduler data inference serving; do
    printf '%s | SUCCESS => {\n    "changed": false,\n    "ping": "pong"\n}\n' "$h"
done
exit 0
"""

# Three canned redeploy-precis.yml PLAY RECAPs, selected via
# $FAKE_PLAY_SCENARIO:
#   ok            — every host clean, exit 0 (full success, base design).
#   residual_fail — the 5 rollout hosts clean, but an extra host OUTSIDE that
#                   set (a stand-in for the balthazar sandbox residual) shows
#                   failed=1 — the exact gr332009 collision shape. Overall
#                   exit non-zero (ansible-playbook fails the whole run on
#                   ANY host failure), but the rollout itself converged.
#   rollout_fail  — one of the 5 rollout hosts itself shows failed=1 — a
#                   genuine rollout failure that must NOT clear the stamp.
_FAKE_ANSIBLE_PLAYBOOK = """#!/usr/bin/env bash
case "${FAKE_PLAY_SCENARIO:-ok}" in
  residual_fail)
    cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
data                       : ok=8    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
inference                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
serving                    : ok=5    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
balthazar                  : ok=2    changed=0    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
RECAP
    exit 2
    ;;
  rollout_fail)
    cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
data                       : ok=8    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
inference                  : ok=3    changed=1    unreachable=0    failed=1    skipped=1    rescued=0    ignored=0
serving                    : ok=5    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
RECAP
    exit 2
    ;;
  ok)
    cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
data                       : ok=8    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
inference                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
serving                    : ok=5    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
RECAP
    exit 0
    ;;
esac
"""


def _make_fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "ansible").write_text(_FAKE_ANSIBLE, encoding="utf-8")
    (bindir / "ansible-playbook").write_text(_FAKE_ANSIBLE_PLAYBOOK, encoding="utf-8")
    (bindir / "ansible").chmod(0o755)
    (bindir / "ansible-playbook").chmod(0o755)
    return bindir


@pytest.fixture
def deploy_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo carrying the REAL scripts/deploy +
    scripts/lib/deploy-state.sh at their real relative paths (scripts/deploy
    resolves its own REPO_ROOT off `dirname "$0"/..`), plus a separate fake
    `deploy/` (CLUSTER_DIR) tree containing only the empty
    redeploy-precis.yml scripts/deploy checks for. PRECIS_DEPLOY_FROM_TREE is
    set to the empty string in every invocation below (the documented
    "legacy escape") so scripts/deploy uses this CLUSTER_DIR directly instead
    of trying to resolve deploy/'s private overlay, which this throwaway
    fixture never provides.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts_dir = repo / "scripts"
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY_SRC, scripts_dir / "deploy")
    shutil.copy2(DEPLOY_STATE_LIB_SRC, lib_dir / "deploy-state.sh")
    (scripts_dir / "deploy").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add deploy scripts")

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()
    (cluster_dir / "redeploy-precis.yml").write_text("---\n", encoding="utf-8")
    return repo, cluster_dir


def _run_deploy(
    repo: Path, cluster_dir: Path, fakebin: Path, scenario: str
) -> subprocess.CompletedProcess[str]:
    env = _test_env(
        PRECIS_DEPLOY_ALLOW_STALE="1",
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_FROM_TREE="",
        PRECIS_CLUSTER_DIR=str(cluster_dir),
        PRECIS_DEPLOY_NO_LOG="1",
        FAKE_PLAY_SCENARIO=scenario,
    )
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(repo / "scripts" / "deploy")],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_deploy_writes_marker_and_clears_stamp_when_rollout_converges_despite_residual_failure(
    deploy_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """Case (a): the exact collision shape — the rollout itself converged
    (every rollout host clean in the PLAY RECAP) but a later, unrelated play
    on an outside host (balthazar) failed. The graft must write the success
    marker AND clear the attempt stamp before the caller's `|| die` fires, so
    the ship footer doesn't say "uncertain" forever."""
    repo, cluster_dir = deploy_repo
    fakebin = _make_fake_bin(tmp_path)

    result = _run_deploy(repo, cluster_dir, fakebin, "residual_fail")

    assert result.returncode != 0, (
        "the overall deploy must still report red — a residual play DID fail"
    )
    marker = _marker_path(repo)
    stamp = _attempt_path(repo)
    assert marker.exists(), (
        "a converged rollout must get its success marker recorded even though "
        f"an unrelated later play failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    recorded_sha = marker.read_text(encoding="utf-8").split()[0]
    assert recorded_sha == _git(repo, "rev-parse", "main").stdout.strip()
    assert not stamp.exists(), (
        "the attempt stamp must be cleared on a converged rollout"
    )
    assert "recording the deploy-state marker" in result.stdout


def test_deploy_leaves_stamp_and_writes_no_marker_when_rollout_itself_fails(
    deploy_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """Case (b): a genuine rollout-host failure must NOT get a marker, and
    the attempt stamp (written before the playbook ran) must survive — that's
    exactly the signal the base design's "uncertain" footer depends on."""
    repo, cluster_dir = deploy_repo
    fakebin = _make_fake_bin(tmp_path)

    result = _run_deploy(repo, cluster_dir, fakebin, "rollout_fail")

    assert result.returncode != 0
    marker = _marker_path(repo)
    stamp = _attempt_path(repo)
    assert not marker.exists(), (
        "a genuinely failed rollout host must never get a marker written"
        f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert stamp.exists(), "the attempt stamp must survive a genuine rollout failure"
    recorded_sha = stamp.read_text(encoding="utf-8").split()[0]
    assert recorded_sha == _git(repo, "rev-parse", "main").stdout.strip()


def test_deploy_writes_marker_and_clears_stamp_on_full_success(
    deploy_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """Case (c): non-regression on the base (sibling) design's own success
    path — untouched by the graft."""
    repo, cluster_dir = deploy_repo
    fakebin = _make_fake_bin(tmp_path)

    result = _run_deploy(repo, cluster_dir, fakebin, "ok")

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    marker = _marker_path(repo)
    stamp = _attempt_path(repo)
    assert marker.exists()
    recorded_sha = marker.read_text(encoding="utf-8").split()[0]
    assert recorded_sha == _git(repo, "rev-parse", "main").stdout.strip()
    assert not stamp.exists()


# ──────────────────────── scripts/ship footer (base design) ──────────────────


def _write_ship_prelude(repo: Path) -> None:
    """Stage the REAL scripts/ship's function-definitions-only prelude —
    everything up to (not including) "── 1. sanity", the first line that
    actually DOES anything invasive (branch checks, session-lock
    re-assertion, committing WIP, gating, ...). Everything before that point
    is `set -euo pipefail` + `cd`/var-assignment + function defs + one
    guarded `source` — safe to run standalone, and it's what defines
    `_deploy_lag_stats`/`_deploy_attempt_pending`/`_deploy_marker_missing`,
    the functions under test. Never reimplemented: sliced directly out of the
    shipped file's own text.
    """
    text = SHIP_SRC.read_text(encoding="utf-8")
    marker = "# ── 1. sanity"
    idx = text.index(marker)
    dest = repo / "scripts" / "ship"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text[:idx], encoding="utf-8")
    dest.chmod(0o755)


def _footer_block() -> str:
    """The REAL "── 8. deploy-lag summary" print block, sliced verbatim off
    the end of the shipped file (it's the last thing in scripts/ship)."""
    text = SHIP_SRC.read_text(encoding="utf-8")
    marker = "# ── 8. deploy-lag summary"
    idx = text.index(marker)
    return text[idx:]


@pytest.fixture
def ship_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lib_dir = repo / "scripts" / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY_STATE_LIB_SRC, lib_dir / "deploy-state.sh")
    _write_ship_prelude(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add ship prelude + deploy-state lib")
    return repo


def _run_ship_probe(repo: Path, driver: str) -> subprocess.CompletedProcess[str]:
    """Append `driver` to the staged prelude and run it — `$0`'s
    `dirname .. /..` trick in the prelude only resolves `WORKTREE` correctly
    when the file lives at `<repo>/scripts/ship`, which is where the fixture
    put it.
    """
    probe = repo / "scripts" / "ship"
    base = probe.read_text(encoding="utf-8")
    probe.write_text(base + "\n" + driver + "\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(probe)],
        cwd=str(repo),
        env=_test_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_deploy_lag_footer_reports_uncertain_when_a_stamp_has_no_newer_marker(
    ship_repo: Path,
) -> None:
    """Case (d): an attempt stamp with no (or no newer) success marker — the
    base design's own "uncertain" wording, asserted verbatim (not
    paraphrased) so a future edit to that string is a deliberate, visible
    diff here too."""
    repo = ship_repo
    attempt = _attempt_path(repo)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    attempt.write_text(
        f"{sha} {1}\n", encoding="utf-8"
    )  # epoch=1: ancient, still pending

    result = _run_ship_probe(repo, _footer_block())
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "deploy state uncertain — deploy of" in result.stdout, result.stdout
    assert (
        "no success recorded (died red or still running); scripts/deploy to retry."
        in result.stdout
    ), result.stdout
    assert "commit(s) on main not yet deployed" not in result.stdout, result.stdout
    assert "no successful deploy on record" not in result.stdout, result.stdout


def test_deploy_lag_footer_reports_ordinary_count_when_marker_is_current(
    ship_repo: Path,
) -> None:
    """Case (e): a success marker with no pending (or no newer) attempt —
    the ordinary commit-count line, unchanged from before gr332009."""
    repo = ship_repo
    deployed_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "file.txt")
    marker = _marker_path(repo)
    import time

    marker.write_text(f"{deployed_sha} {int(time.time())}\n", encoding="utf-8")

    result = _run_ship_probe(repo, _footer_block())
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "1 commit(s) on main not yet deployed" in result.stdout, result.stdout
    assert "uncertain" not in result.stdout, result.stdout
    assert "no successful deploy on record" not in result.stdout, result.stdout


def test_deploy_lag_footer_reports_no_record_when_neither_marker_nor_stamp_exist(
    ship_repo: Path,
) -> None:
    """Case (f): the base design's third state — a cheap non-regression pin
    on behavior this session did not write."""
    repo = ship_repo

    result = _run_ship_probe(repo, _footer_block())
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "no successful deploy on record" in result.stdout, result.stdout
    assert "uncertain" not in result.stdout, result.stdout
    assert "not yet deployed" not in result.stdout, result.stdout
