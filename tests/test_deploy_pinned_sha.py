"""Tests for the gated-sha pin: `/go` deploys the tree its gate validated.

`scripts/deploy` resolves its default REF, the branch name ``main``, at deploy
time. `/go` gates a tree, CAS-pushes it, and *then* deploys — so a sibling
`/qland` (`scripts/ship --quick`, ungated by design) landing in that window
silently substitutes an untested tree for the tested one, while the ship
reports green. The fix: `scripts/ship` records the exact sha a FULL gate
validated in ``.ship-sha`` and `/go` deploys ``scripts/deploy "$(cat
.ship-sha)" --pinned``.

That makes "target is behind origin/main" the expected steady state rather
than a rollback, which collides with the gr338201 rollback guard. ``--pinned``
therefore drops *only* that leg and keeps the currently-deployed-sha leg —
the one that actually catches the 2026-09-13 incident shape (a stale worktree
deploying an ancestor of what was already live). With no deploy-state marker
that leg has nothing to compare against, so ``--pinned`` fails closed.

Pinning also makes a literal-sha REF the common path, which exposed a
pre-existing hole the gr338201 header already admitted: the freshness guard
compares ``origin/$REF``, and for a sha that ref does not exist, so the guard
no-opped entirely and a sha deploy got no template-skew check at all. Covered
here too.

Cases:
  (a) --pinned, target behind origin/main but not behind the deployed sha →
      ALLOWED, and the marker records the pinned sha (not origin/main).
  (b) --pinned, target behind the DEPLOYED sha → still refused (gr338201).
  (c) --pinned with no deploy-state marker → refused, fail closed.
  (d) no --pinned, target behind origin/main → still refused (non-regression:
      --pinned must not become the ambient behaviour).
  (e) --pinned prints how far behind origin/main the pin is — the ungated
      backlog is a number the operator should see, not infer from silence.
  (f) a literal-sha REF the local checkout does not contain → refused by the
      freshness guard (the closed hole).
  (g) scripts/ship writes the pin only after a FULL gate, and clears any
      stale pin before it can be mistaken for this run's.

Exercised against the REAL scripts, never reimplemented — same technique as
tests/test_deploy_lag_honesty.py: scripts/deploy is copied byte-for-byte into
a throwaway repo at its real relative path (it resolves REPO_ROOT off
``dirname "$0"/..``) and driven with fake ansible binaries on $PATH; the
scripts/ship decision is sliced out of the shipped file's own text.
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


def _commit(path: Path, name: str) -> str:
    (path / name).write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", name)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _lib_call(fn: str, *args: str) -> str:
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


# The fake ping reports a fixed reachable host set; enough to get past the
# reachability check into the marker-writing path, which is all these cases
# need. Copied in shape from tests/test_deploy_lag_honesty.py.
_FAKE_ANSIBLE = """#!/usr/bin/env bash
for h in gateway scheduler data inference serving; do
    printf '%s | SUCCESS => {\n    "changed": false,\n    "ping": "pong"\n}\n' "$h"
done
exit 0
"""

_FAKE_ANSIBLE_PLAYBOOK = """#!/usr/bin/env bash
cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
data                       : ok=8    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
inference                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
serving                    : ok=5    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
RECAP
exit 0
"""


def _make_fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "ansible").write_text(_FAKE_ANSIBLE, encoding="utf-8")
    (bindir / "ansible-playbook").write_text(_FAKE_ANSIBLE_PLAYBOOK, encoding="utf-8")
    (bindir / "ansible").chmod(0o755)
    (bindir / "ansible-playbook").chmod(0o755)
    return bindir


class Fixture:
    """A throwaway repo with a real `origin` remote, so the rollback guard's
    freshly-fetched ``origin/main`` leg is live (the lag-honesty fixture has
    no remote, which leaves that leg inert).

    History, mirroring the shape this change is about:
        base → gated → sibling1 → sibling2
    where `gated` is what a /go's full gate validated and sibling1/sibling2
    are ungated /qland merges that landed during the deploy window.
    """

    def __init__(self, repo: Path, cluster_dir: Path, shas: dict[str, str]) -> None:
        self.repo = repo
        self.cluster_dir = cluster_dir
        self.base = shas["base"]
        self.gated = shas["gated"]
        self.sibling1 = shas["sibling1"]
        self.sibling2 = shas["sibling2"]

    def set_marker(self, sha: str) -> None:
        marker = _marker_path(self.repo)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{sha} 1700000000 success\n", encoding="utf-8")

    def marker_sha(self) -> str | None:
        marker = _marker_path(self.repo)
        if not marker.exists():
            return None
        return marker.read_text(encoding="utf-8").split()[0]

    @property
    def pin(self) -> Path:
        return self.repo / ".ship-sha"

    def set_pin(self, sha: str) -> None:
        self.pin.write_text(f"{sha}\n", encoding="utf-8")


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")

    scripts_dir = repo / "scripts"
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY_SRC, scripts_dir / "deploy")
    shutil.copy2(DEPLOY_STATE_LIB_SRC, lib_dir / "deploy-state.sh")
    (scripts_dir / "deploy").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add deploy scripts")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    gated = _commit(repo, "gated.txt")
    sibling1 = _commit(repo, "sibling1.txt")
    sibling2 = _commit(repo, "sibling2.txt")

    origin = tmp_path / "origin.git"
    _git(repo, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()
    (cluster_dir / "redeploy-precis.yml").write_text("---\n", encoding="utf-8")

    return Fixture(
        repo,
        cluster_dir,
        {"base": base, "gated": gated, "sibling1": sibling1, "sibling2": sibling2},
    )


def _run_deploy(
    fx: Fixture, fakebin: Path, *args: str, allow_stale: str = "1"
) -> subprocess.CompletedProcess[str]:
    env = _test_env(
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_FROM_TREE="",
        PRECIS_CLUSTER_DIR=str(fx.cluster_dir),
        PRECIS_DEPLOY_NO_LOG="1",
    )
    if allow_stale:
        env["PRECIS_DEPLOY_ALLOW_STALE"] = allow_stale
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(fx.repo / "scripts" / "deploy"), *args],
        cwd=str(fx.repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ───────────────────────── the --pinned rollback guard ───────────────────────


def test_pinned_allows_a_target_behind_origin_main(fx: Fixture, tmp_path: Path) -> None:
    """Case (a): the whole point. The gated sha is two ungated sibling merges
    behind origin/main — without --pinned that is indistinguishable from a
    rollback, and the guard refuses. With --pinned it goes out, and the marker
    records the PINNED sha, proving the fleet got the gated tree rather than
    origin/main's."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned")

    assert result.returncode == 0, (
        "--pinned must allow a deliberately-behind-main target\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "REFUSING TO DEPLOY" not in result.stdout + result.stderr
    assert fx.marker_sha() == fx.gated, (
        "the fleet must be recorded as running the GATED sha, not origin/main"
    )


def test_pinned_still_refuses_a_target_behind_the_deployed_sha(
    fx: Fixture, tmp_path: Path
) -> None:
    """Case (b): --pinned drops only the origin/main leg. The deployed-sha leg
    is the one that catches the real gr338201 shape — a stale worktree
    deploying an ancestor of what is already live — and must survive."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.sibling1)

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned")

    assert result.returncode != 0, "--pinned must not disarm the rollback guard"
    combined = result.stdout + result.stderr
    assert "REFUSING TO DEPLOY" in combined
    assert "currently-deployed sha" in combined, (
        f"the refusal must name the deployed-sha leg, got:\n{combined}"
    )
    assert fx.marker_sha() == fx.sibling1, "a refused deploy must not move the marker"


def test_pinned_without_a_marker_fails_closed(fx: Fixture, tmp_path: Path) -> None:
    """Case (c): with no deploy-state marker the surviving leg has nothing to
    compare against, so --pinned would degrade into a guard-free deploy.
    Refuse instead."""
    fakebin = _make_fake_bin(tmp_path)
    assert fx.marker_sha() is None

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned")

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "--pinned needs the deploy-state marker" in combined, (
        f"expected the fail-closed refusal, got:\n{combined}"
    )
    assert fx.marker_sha() is None


def test_pinned_with_nothing_behind_main_still_succeeds(
    fx: Fixture, tmp_path: Path
) -> None:
    """A pin that is not behind origin/main at all: here a local commit that
    has not been pushed, so ``rev-list --count target..origin/main`` is zero
    while target still differs from origin/main — the one combination that
    reaches the behind-count block with nothing to report. It must deploy, and
    say nothing about a backlog that does not exist."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    ahead = _commit(fx.repo, "unpushed.txt")

    result = _run_deploy(fx, fakebin, ahead, "--pinned")

    assert result.returncode == 0, (
        "a pin that happens to equal origin/main must deploy, not abort\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "behind origin/main" not in result.stdout
    assert fx.marker_sha() == ahead


def test_unpinned_still_refuses_a_target_behind_origin_main(
    fx: Fixture, tmp_path: Path
) -> None:
    """Case (d): non-regression. Without --pinned the origin/main leg must
    still fire — the flag has to be an explicit opt-in, not a loosening of the
    default."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)

    result = _run_deploy(fx, fakebin, fx.gated)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING TO DEPLOY" in combined
    assert "origin/main" in combined


def test_pinned_reports_how_far_behind_main_the_pin_is(
    fx: Fixture, tmp_path: Path
) -> None:
    """Case (e): the ungated backlog the pin leaves behind is the operator's
    signal that main needs a gate — print the count rather than succeeding
    silently."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned")

    assert result.returncode == 0
    assert "2 commit(s) behind origin/main" in result.stdout, (
        f"expected the behind-count NOTE, got:\n{result.stdout}"
    )


# ───────────────────── the literal-sha freshness hole (f) ────────────────────


def test_literal_sha_deploy_requires_the_local_checkout_to_contain_it(
    fx: Fixture, tmp_path: Path
) -> None:
    """Case (f): ansible renders deploy/ templates from the local checkout
    while the venvs install the target sha. For a literal sha there is no
    ``origin/<sha>`` ref, so the freshness guard used to no-op completely and
    a sha deploy got no skew check at all. Now the target commit itself must
    be contained in HEAD."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    # A stale worktree: checked out at `gated`, asked to deploy a LATER sha
    # whose tree this checkout has never rendered from.
    _git(fx.repo, "checkout", "-q", fx.gated)

    result = _run_deploy(fx, fakebin, fx.sibling2, allow_stale="")

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "local tree does not contain" in combined, (
        f"expected the freshness refusal, got:\n{combined}"
    )


def test_literal_sha_deploy_notes_a_checkout_ahead_of_the_target(
    fx: Fixture, tmp_path: Path
) -> None:
    """The /go case: HEAD contains the target but has moved past it. Allowed —
    that is what pinning means — but said out loud, because the templates and
    the installed code no longer come from the same tree."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned", allow_stale="")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "rendering deploy/ templates from HEAD" in result.stdout


# ─────────────────── canary path resolves a literal sha (gr346747) ───────────


def test_canary_path_pins_a_local_sha(fx: Fixture, tmp_path: Path) -> None:
    """`git ls-remote <url> <sha>` matches ref names only, so the canary
    phase used to die on exactly the target /go hands it. A sha the local
    checkout contains (and a remote branch reaches) is pinned directly; the
    run then proceeds into phase 1 (fake ansible) and on to the heartbeat
    verify, which fails here for want of prod — past the resolution."""
    fx.set_marker(fx.base)
    env_bin = _make_fake_bin(tmp_path)
    env = _test_env(
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_FROM_TREE="",
        PRECIS_CLUSTER_DIR=str(fx.cluster_dir),
        PRECIS_DEPLOY_NO_LOG="1",
        PRECIS_DEPLOY_ALLOW_STALE="1",
        PRECIS_DEPLOY_CANARY="gateway",
        PRECIS_DEPLOY_CANARY_TIMEOUT_S="0",
    )
    env["PATH"] = f"{env_bin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(fx.repo / "scripts" / "deploy"), fx.gated, "--pinned"],
        cwd=str(fx.repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "could not resolve" not in result.stderr
    assert f"is a commit this checkout contains — pinned {fx.gated[:8]}" in result.stdout
    assert f"phase 1 — gateway only (pinned {fx.gated[:8]})" in result.stdout
    # Past resolution and phase 1; the heartbeat verify has no prod to ask.
    assert result.returncode != 0
    assert "canary verify" in result.stderr


def test_canary_path_refuses_a_sha_no_remote_branch_reaches(
    fx: Fixture, tmp_path: Path
) -> None:
    """The hosts install precis-mcp@<sha> from GitHub — a local-only commit
    would fail on every host, so it is refused up front."""
    fx.set_marker(fx.base)
    local_only = _commit(fx.repo, "unpushed.txt")
    env_bin = _make_fake_bin(tmp_path)
    env = _test_env(
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_FROM_TREE="",
        PRECIS_CLUSTER_DIR=str(fx.cluster_dir),
        PRECIS_DEPLOY_NO_LOG="1",
        PRECIS_DEPLOY_ALLOW_STALE="1",
        PRECIS_DEPLOY_CANARY="gateway",
    )
    env["PATH"] = f"{env_bin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(fx.repo / "scripts" / "deploy"), local_only, "--pinned"],
        cwd=str(fx.repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "not on any remote-tracking branch" in result.stderr
    assert "phase 1" not in result.stdout


# ──────────────────────── scripts/ship writes the pin (g) ────────────────────


def _ship_pin_block() -> str:
    """Slice the pin-writing decision straight out of the shipped
    scripts/ship, so this test can never drift from the real condition."""
    lines = SHIP_SRC.read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith('if [[ "$QUICK" != 1 &&')
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start : end + 1])


@pytest.mark.parametrize(
    ("quick", "impacted", "remote", "docs_only", "expect_pin", "why"),
    [
        ("0", "0", "0", "0", True, "/go: full local gate"),
        ("1", "0", "0", "0", False, "/qland: nothing was gated at all"),
        ("0", "1", "0", "0", False, "/land: testmon-narrowed subset, not a deploy warrant"),
        (
            "0",
            "1",
            "1",
            "0",
            True,
            "--remote --impacted: the impacted run is only a pre-gate ahead of GitHub's full matrix",
        ),
        ("0", "0", "1", "0", True, "--remote: GitHub's full matrix"),
        (
            "0",
            "0",
            "0",
            "1",
            False,
            "docs-only local lane ran ruff + doc pointers, never pytest (gr347014)",
        ),
        ("0", "0", "1", "1", False, "docs-only remote lane ran GitHub's fast set, not the shards"),
        ("1", "0", "0", "1", False, "docs-only --quick: doubly ungated"),
    ],
)
def test_ship_pins_the_gated_sha_only_after_a_full_gate(
    tmp_path: Path,
    quick: str,
    impacted: str,
    remote: str,
    docs_only: str,
    expect_pin: bool,
    why: str,
) -> None:
    """Case (g): the five-input decision. A pin written after a --quick,
    bare --impacted or docs-only-lane ship would let /go deploy a tree the
    full suite never ran."""
    pin = tmp_path / ".ship-sha"
    script = (
        "say() { :; }\n"
        f"QUICK={quick}\nIMPACTED={impacted}\nREMOTE={remote}\nDOCS_ONLY={docs_only}\n"
        'GATED_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"\n'
        f'SHIP_SHA_FILE="{pin}"\n' + _ship_pin_block() + "\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr

    assert pin.exists() is expect_pin, why
    if expect_pin:
        assert pin.read_text(encoding="utf-8").strip() == "deadbeef" * 5


def test_ship_never_pins_an_empty_sha(tmp_path: Path) -> None:
    """A ship that reached the write with no captured sha must leave no pin —
    an empty .ship-sha would make /go's `scripts/deploy "$(cat .ship-sha)"`
    fall back to deploy's default REF, `main`: exactly the substitution the
    pin exists to prevent."""
    pin = tmp_path / ".ship-sha"
    script = (
        "say() { :; }\n"
        'QUICK=0\nIMPACTED=0\nREMOTE=0\nDOCS_ONLY=0\nGATED_SHA=""\n'
        f'SHIP_SHA_FILE="{pin}"\n' + _ship_pin_block() + "\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert not pin.exists()


def test_ship_full_flag_forces_the_local_suite_on_a_docs_only_diff() -> None:
    """/go passes --full: the docs-only classification must be overridable
    so a docs-only /go still ends in a deploy pin, and /go must pass it."""
    text = SHIP_SRC.read_text(encoding="utf-8")
    assert "--full)     FULL=1; shift ;;" in text
    override_at = text.index('if [[ "$FULL" == 1 && "$DOCS_ONLY" == 1 ]]; then')
    pin_at = text.index('if [[ "$QUICK" != 1 && "$DOCS_ONLY" != 1')
    assert override_at < pin_at
    go = (REPO_ROOT / ".claude" / "commands" / "go.md").read_text(encoding="utf-8")
    assert "scripts/ship --mutate --full" in go


def test_ship_clears_a_stale_pin_before_it_can_do_anything_else() -> None:
    """The pin is removed unconditionally at the top, ahead of every step that
    can fail — so a red gate, an aborted merge or a lost CAS race can never
    leave an EARLIER run's sha lying around for /go to deploy."""
    text = SHIP_SRC.read_text(encoding="utf-8")
    rm_at = text.index('rm -f "$SHIP_SHA_FILE"')
    sanity_at = text.index("── 1. sanity")
    assert rm_at < sanity_at, (
        "the stale-pin removal must precede step 1, not sit inside a path a "
        "failure can skip"
    )


# ─────────── the pin as a one-shot token: enforced, then consumed ────────────
# `--pinned` is only load-bearing if the caller types the pinned sha, and /go
# is executed by an agent following prose. These cover the mechanism that does
# not depend on anyone remembering.


def test_an_unconsumed_pin_refuses_a_bare_deploy(fx: Fixture, tmp_path: Path) -> None:
    """The original bug, reproduced at the only place that can still cause it:
    a bare `scripts/deploy` resolves `main` at deploy time (here: two ungated
    sibling merges past the gated sha). With a pin pending that must be a hard
    refusal, not a silent success over untested code."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    fx.set_pin(fx.gated)

    result = _run_deploy(fx, fakebin)

    assert result.returncode != 0, (
        "a bare deploy while a gated pin is unconsumed must refuse\n"
        f"stdout: {result.stdout}"
    )
    assert fx.gated[:8] in result.stderr
    assert "--ignore-pin" in result.stderr, "the refusal must name its escape hatch"
    assert fx.pin.exists(), "a refused deploy must not consume the pin"


def test_deploying_the_pin_consumes_it(fx: Fixture, tmp_path: Path) -> None:
    """Success removes the pin. Without this a spent .ship-sha would block
    every later ordinary deploy from the tree that shipped it."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    fx.set_pin(fx.gated)

    result = _run_deploy(fx, fakebin, fx.gated, "--pinned")

    assert result.returncode == 0, result.stdout + result.stderr
    assert fx.marker_sha() == fx.gated
    assert not fx.pin.exists(), "a deployed pin must be consumed, not left to go stale"


def test_ignore_pin_allows_a_deliberate_off_pin_deploy(
    fx: Fixture, tmp_path: Path
) -> None:
    """Bare `scripts/deploy` stays a documented cluster-admin operation — it
    just has to be deliberate while a pin is pending. The pin survives: it is
    still guarding a sha that never went out."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    fx.set_pin(fx.gated)

    result = _run_deploy(fx, fakebin, "--ignore-pin")

    assert result.returncode == 0, result.stdout + result.stderr
    assert fx.marker_sha() == fx.sibling2
    assert fx.pin.exists(), (
        "an off-pin deploy must leave the pin in place — that sha is still ungone"
    )


def test_force_rollback_is_not_gated_behind_a_second_flag(
    fx: Fixture, tmp_path: Path
) -> None:
    """--force-rollback is the incident hatch. A pending pin must not make an
    operator discover a second flag mid-incident."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.sibling2)
    fx.set_pin(fx.gated)

    result = _run_deploy(fx, fakebin, fx.base, "--force-rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    assert fx.marker_sha() == fx.base


def test_no_pin_leaves_a_bare_deploy_untouched(fx: Fixture, tmp_path: Path) -> None:
    """Non-regression: the gate is inert in any worktree that never shipped."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    assert not fx.pin.exists()

    result = _run_deploy(fx, fakebin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fx.marker_sha() == fx.sibling2


def test_an_unusable_pin_says_what_actually_works(fx: Fixture, tmp_path: Path) -> None:
    """A pin that does not resolve to a commit here (partial write from a
    killed ship, a truncation) can never be satisfied. Refuse — an unreadable
    pin is not evidence that deploying is safe — but print the remedy that
    works, not the generic one, which would replay the same garbage as REF and
    die identically while the operator follows instructions in a loop."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    fx.pin.write_text("not-a-sha\n", encoding="utf-8")

    result = _run_deploy(fx, fakebin, "not-a-sha", "--pinned")

    assert result.returncode != 0
    assert "unusable" in result.stderr
    assert "rm .ship-sha" in result.stderr, "the printed remedy must be one that works"


def test_pinned_with_no_target_refuses_rather_than_defaulting_to_main(
    fx: Fixture, tmp_path: Path
) -> None:
    """`scripts/deploy "$(cat .ship-sha)" --pinned` with the pin already
    consumed expands to a bare `--pinned`. Without this guard REF falls back to
    `main` — re-resolved at deploy time, with --pinned suppressing the
    origin/main rollback leg — which is precisely the ungated-substitution bug
    the pin exists to prevent, arriving through the pin's own retry path."""
    fakebin = _make_fake_bin(tmp_path)
    fx.set_marker(fx.base)
    assert not fx.pin.exists()

    result = _run_deploy(fx, fakebin, "--pinned")

    assert result.returncode != 0, (
        "--pinned with no target must refuse, not silently deploy main\n"
        f"stdout: {result.stdout}"
    )
    assert "no target" in result.stderr
    assert fx.marker_sha() == fx.base, "nothing may have been deployed"
