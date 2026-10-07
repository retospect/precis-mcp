"""`scripts/round gate` and `scripts/round deploy` — the CI-verdict round gate.

The round no longer gates with a local suite under the ship lock. The
coordinator asks for the newest main sha with a full green CI verdict
(`scripts/last-gated-main-sha`) and deploys exactly that sha. Pinned here,
against the real script and a throwaway repo with a bare `origin`:

  - `gate` reports the candidate and how far main is ahead, split docs-only vs
    code by the same regex check.yml's `plan` job uses;
  - no candidate is a plain message and exit 1, not a traceback;
  - `deploy` refuses a stale or unknown-age verdict, a candidate that left
    main, and a `gated` that is not a fast-forward; it also refuses when
    `gated` did not actually move;
  - `--dry-run` prints the gated move and the deploy argv and runs neither;
  - the happy path runs `scripts/deploy <40-char sha> --pinned`;
  - neither verb takes the ship lock;
  - `cut` (release branch, slice a) pushes `release/r<N>` at the candidate,
    records it, prints late marks, and refuses a second release, a sha off
    main's first-parent line and a new duplicate migration number; `--abandon`
    deletes a release whose head is on main.

Seam: `PRECIS_ROUND_ROOT` points the verbs at the throwaway repo, whose
`scripts/last-gated-main-sha`, `scripts/deploy` are stubs (`LGM_SHA` /
`LGM_AGE` steer the first) and whose `gh` is a PATH stub serving canned
check-run rows from `GH_STUB_DIR/<sha>`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs the shebang'd scripts/round and bash stubs",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "round"

_LGM_STUB = """#!/usr/bin/env bash
case " $* " in
  *" --exact "*)
    [ -n "${LGM_EXACT_HOOK:-}" ] && bash -c "$LGM_EXACT_HOOK"
    printf '%s\\n' "${LGM_EXACT:-}"; exit 0 ;;
esac
case " $* " in
  *" --age-hours "*) printf '%s\\n' "${LGM_AGE:-}" ;;
  *) printf '%s\\n' "${LGM_SHA:-}" ;;
esac
case " $* " in *" --age-hours "*) exit 0 ;; esac
exit "${LGM_RC:-0}"
"""

_DEPLOY_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/../deploy.log"
if [ -n "${DEPLOY_STUB_HOOK:-}" ]; then
  bash -c "$DEPLOY_STUB_HOOK" || exit $?
fi
if [ "${DEPLOY_STUB_VERIFY:-}" = 1 ]; then
  common=$(git rev-parse --path-format=absolute --git-common-dir)
  printf '%s %s success\\n' "$1" "$(date +%s)" > "$common/precis-deploy-state"
  rm -f "$common/precis-deploy-attempt"
  git push -q origin "$1:refs/heads/prod" || exit 1
fi
exit "${DEPLOY_STUB_RC:-0}"
"""

_GH_STUB = """#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    */commits/*/check-runs)
      sha="${a#*/commits/}"; sha="${sha%/check-runs}"
      [ -f "$GH_STUB_DIR/$sha" ] && { cat "$GH_STUB_DIR/$sha"; exit 0; }
      exit 1 ;;
  esac
done
exit 1
"""


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    for k in ("PRECIS_ENV_POINTERS", "PRECIS_ROUND_MAX_CANDIDATE_HOURS"):
        env.pop(k, None)
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    ).stdout.strip()


@dataclass
class Rig:
    root: Path
    origin: Path
    shas: dict[str, str]  # c0 code, c1 docs, c2 README, c3 code
    gh_dir: Path
    env: dict[str, str]

    def round(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )

    def deploy_log(self) -> list[str]:
        log = self.root / "deploy.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def origin_ref(self, name: str) -> str:
        cp = subprocess.run(
            [
                "git",
                "-C",
                str(self.origin),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{name}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return cp.stdout.strip()

    @property
    def lock(self) -> Path:
        return self.root / ".git" / "precis-ship.lock.d"


def _commit(root: Path, rel: str, msg: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    origin = tmp_path / "origin.git"
    root = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "remote", "add", "origin", str(origin))
    scripts = root / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "lib" / "env-pointers.sh", scripts / "lib")
    for name, body in (("last-gated-main-sha", _LGM_STUB), ("deploy", _DEPLOY_STUB)):
        (scripts / name).write_text(body, encoding="utf-8")
        (scripts / name).chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(_GH_STUB, encoding="utf-8")
    (bindir / "gh").chmod(0o755)
    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    # .gitignore keeps the stubs' scratch files out of the commits' diffs.
    (root / ".gitignore").write_text("deploy.log\n", encoding="utf-8")
    shas = {"c0": _commit(root, "src/a.py", "c0 code")}
    shas["c1"] = _commit(root, "docs/x.md", "c1 docs")
    shas["c2"] = _commit(root, "README.md", "c2 readme")
    shas["c3"] = _commit(root, "src/b.py", "c3 code")
    _git(root, "push", "-q", "origin", "main")
    env = _env(
        PRECIS_ROUND_ROOT=str(root),
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        GH_STUB_DIR=str(gh_dir),
    )
    return Rig(root, origin, shas, gh_dir, env)


def _push_branch(rig: Rig, name: str, sha: str) -> None:
    _git(rig.root, "push", "-q", "origin", f"{sha}:refs/heads/{name}")


def _fresh(rig: Rig, key: str = "c2", age: str = "1.5") -> dict[str, str]:
    return {"LGM_SHA": rig.shas[key], "LGM_AGE": age}


# ── gate ────────────────────────────────────────────────────────────────


def test_docs_only_regex_is_the_one_check_yml_uses(tmp_path: Path) -> None:
    # Load a .py copy: testmon fingerprints every module it sees, and a dotless
    # path crashes its get_file (gr450298).
    copy = tmp_path / "round_script.py"
    copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    loader = SourceFileLoader("round_script", str(copy))
    spec = importlib.util.spec_from_loader("round_script", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    workflow = (REPO_ROOT / ".github" / "workflows" / "check.yml").read_text(
        encoding="utf-8"
    )
    assert f"grep -qvE '{mod.DOCS_ONLY_RE}'" in workflow


def test_gate_reports_candidate_and_docs_code_split(rig: Rig) -> None:
    _push_branch(rig, "prod", rig.shas["c0"])
    out = rig.round("gate", **_fresh(rig, "c0", "2.0"))
    assert out.returncode == 0, out.stderr
    assert f"candidate {rig.shas['c0'][:8]} c0 code" in out.stdout
    assert "verdict age 2.0h" in out.stdout
    assert rig.shas["c3"][:8] in out.stdout  # origin/main
    assert "ahead of the candidate by 2 docs-only and 1 code commit(s)" in out.stdout
    assert not rig.lock.exists()


def test_gate_json_has_the_same_facts(rig: Rig) -> None:
    out = rig.round("gate", "--json", **_fresh(rig, "c1", "0.5"))
    facts = json.loads(out.stdout)
    assert facts["candidate"] == rig.shas["c1"]
    assert facts["age_hours"] == 0.5
    assert (facts["ahead_docs"], facts["ahead_code"]) == (1, 1)
    assert facts["origin_main"] == rig.shas["c3"]
    assert facts["origin_gated"] == ""
    assert facts["red"] is None


_NONE_MSG = "no main sha with a green CI verdict in the last 48 h"
_UNREADABLE_MSG = "could not read CI verdicts (gh/auth/API) — no candidate"


@pytest.mark.parametrize(
    ("rc", "msg", "state"),
    [("2", _NONE_MSG, "none_in_window"), ("0", _UNREADABLE_MSG, "unreadable")],
)
def test_gate_with_no_candidate_words_the_two_cases_and_exits_1(
    rig: Rig, rc: str, msg: str, state: str
) -> None:
    out = rig.round("gate", LGM_SHA="", LGM_AGE="", LGM_RC=rc)
    assert out.returncode == 1
    assert msg in out.stdout
    assert "Traceback" not in out.stderr
    js = rig.round("gate", "--json", LGM_SHA="", LGM_AGE="", LGM_RC=rc)
    assert js.returncode == 1
    assert json.loads(js.stdout)["candidate_state"] == state


def test_gate_flags_gated_that_is_not_an_ancestor(rig: Rig) -> None:
    side = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c0']}^{{tree}}",
        "-p",
        rig.shas["c0"],
        "-m",
        "side",
    )
    _push_branch(rig, "gated", side)
    out = rig.round("gate", **_fresh(rig, "c2"))
    assert "origin/gated is not an ancestor of the candidate" in out.stdout


def test_gate_routes_to_the_newest_red_commit_above_the_candidate(rig: Rig) -> None:
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / rig.shas["c3"]).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"test-linux (2)\tcompleted\tfailure\t{url}\n"
        f"test-linux (1)\tin_progress\t\t{url}\n"
        f"test-other\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    # An in-progress shard means the verdict is incomplete: not red yet.
    assert (
        json.loads(rig.round("gate", "--json", **_fresh(rig, "c2")).stdout)["red"]
        is None
    )
    (rig.gh_dir / rig.shas["c3"]).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"test-linux (2)\tcompleted\tfailure\t{url}\n"
        f"test-linux (1)\tcompleted\tsuccess\t{url}\n"
        f"test-linux (3.12, 3)\tcompleted\ttimed_out\t{url}\n"
        f"test-other\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    out = rig.round("gate", **_fresh(rig, "c2"))
    assert f"red above the candidate: {rig.shas['c3'][:8]}" in out.stdout
    assert "failing: test-linux (2), test-linux (3.12, 3)" in out.stdout
    assert "test-other" not in out.stdout
    assert "gh run view 4242 --log-failed" in out.stdout


def test_gate_survives_a_failing_gh(rig: Rig) -> None:
    out = rig.round("gate", **_fresh(rig, "c2"))  # stub gh exits 1: no canned rows
    assert out.returncode == 0
    assert "red above" not in out.stdout


# ── deploy ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"LGM_SHA": "", "LGM_AGE": "", "LGM_RC": "2"}, _NONE_MSG),
        ({"LGM_SHA": "", "LGM_AGE": "", "LGM_RC": "0"}, _UNREADABLE_MSG),
        ({"LGM_AGE": "7.0"}, "7.0h old"),
        ({"LGM_AGE": ""}, "age of candidate"),
        ({"LGM_AGE": "3", "PRECIS_ROUND_MAX_CANDIDATE_HOURS": "2"}, "limit 2h"),
    ],
)
def test_deploy_refuses_on_candidate_or_age(
    rig: Rig, env: dict[str, str], needle: str
) -> None:
    out = rig.round("deploy", **{**_fresh(rig, "c2"), **env})
    assert out.returncode == 1
    assert needle in out.stderr
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == ""


def test_deploy_refuses_a_candidate_that_left_main(rig: Rig) -> None:
    stray = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c0']}^{{tree}}",
        "-p",
        rig.shas["c0"],
        "-m",
        "stray",
    )
    out = rig.round("deploy", LGM_SHA=stray, LGM_AGE="1")
    assert out.returncode == 1
    assert "not an ancestor of origin/main" in out.stderr
    assert rig.deploy_log() == []


def test_deploy_refuses_when_gated_is_not_a_fast_forward(rig: Rig) -> None:
    side = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c0']}^{{tree}}",
        "-p",
        rig.shas["c0"],
        "-m",
        "side",
    )
    _push_branch(rig, "gated", side)
    out = rig.round("deploy", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert "gated moves fast-forward only" in out.stderr
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == side


def test_deploy_refuses_when_gated_did_not_move(rig: Rig) -> None:
    _push_branch(rig, "gated", rig.shas["c0"])
    out = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert "env_pointer_move gated" in out.stderr
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == rig.shas["c0"]


def test_deploy_refuses_a_foreign_ship_pin(rig: Rig) -> None:
    (rig.root / ".ship-sha").write_text(rig.shas["c0"] + "\n", encoding="utf-8")
    out = rig.round("deploy", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert ".ship-sha" in out.stderr
    assert rig.deploy_log() == []


def test_dry_run_prints_the_move_and_argv_and_runs_neither(rig: Rig) -> None:
    out = rig.round("deploy", "--dry-run", **_fresh(rig, "c2"))
    assert out.returncode == 0, out.stderr
    assert f"scripts/deploy {rig.shas['c2']} --pinned" in out.stdout
    assert "move origin/gated" in out.stdout
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == ""
    assert not rig.lock.exists()
    assert not (rig.root / ".ship-sha").exists()


def test_deploy_moves_gated_then_runs_pinned_deploy_with_the_full_sha(rig: Rig) -> None:
    _push_branch(rig, "gated", rig.shas["c0"])
    out = rig.round("deploy", **_fresh(rig, "c2"))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("gated") == rig.shas["c2"]
    log = rig.deploy_log()
    assert log == [f"{rig.shas['c2']} --pinned"]
    assert len(log[0].split()[0]) == 40
    assert not rig.lock.exists()
    assert not (rig.root / ".ship-sha").exists()


def test_deploy_exits_with_the_deploy_scripts_code(rig: Rig) -> None:
    out = rig.round("deploy", DEPLOY_STUB_RC="7", **_fresh(rig, "c2"))
    assert out.returncode == 7
    assert rig.origin_ref("gated") == rig.shas["c2"]


# ── cut (release branch, slice a) ───────────────────────────────────────


def _open_round(rig: Rig) -> None:
    assert rig.round("open").returncode == 0


def _round_json(rig: Rig) -> dict[str, Any]:
    path = rig.root / ".git" / "precis-round" / "round.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _child(rig: Rig, parent: str, msg: str) -> str:
    """A commit on top of `parent` with the same tree (object only, no ref)."""
    return _git(rig.root, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", msg)


def _mig_commit(rig: Rig, *names: str) -> str:
    for name in names:
        _commit(rig.root, f"src/precis/migrations/{name}", name)
    sha = _git(rig.root, "rev-parse", "HEAD")
    _git(rig.root, "push", "-q", "origin", "main")
    return sha


def test_cut_pushes_the_release_at_the_candidate_and_records_it(rig: Rig) -> None:
    _open_round(rig)
    out = rig.round("cut", **_fresh(rig, "c2"))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("release/r1") == rig.shas["c2"]
    assert (
        f"release/r1 cut at {rig.shas['c2'][:9]} — late work lands on main for round 2"
        in out.stdout
    )
    assert 'fleet say -m "release/r1 is cut at' in out.stdout
    rel = _round_json(rig)["release"]
    assert (rel["branch"], rel["base"]) == ("release/r1", rig.shas["c2"])
    assert str(rel["cut_at"]).endswith("Z")
    assert rig.origin_ref("main") == rig.shas["c3"]  # main untouched


def test_cut_sha_overrides_the_candidate(rig: Rig) -> None:
    _open_round(rig)
    out = rig.round("cut", "--sha", rig.shas["c1"][:10], **_fresh(rig, "c3"))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("release/r1") == rig.shas["c1"]
    assert _round_json(rig)["release"]["base"] == rig.shas["c1"]


def test_cut_needs_an_open_round(rig: Rig) -> None:
    out = rig.round("cut", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert "no round is open" in out.stderr
    assert rig.origin_ref("release/r1") == ""


@pytest.mark.parametrize(("rc", "msg"), [("2", _NONE_MSG), ("0", _UNREADABLE_MSG)])
def test_cut_without_a_candidate_refuses_in_gates_words(
    rig: Rig, rc: str, msg: str
) -> None:
    _open_round(rig)
    out = rig.round("cut", LGM_SHA="", LGM_AGE="", LGM_RC=rc)
    assert out.returncode == 1
    assert msg in out.stderr
    assert rig.origin_ref("release/r1") == ""


def test_cut_refuses_while_a_release_branch_exists_on_origin(rig: Rig) -> None:
    _open_round(rig)
    _push_branch(rig, "release/r0", rig.shas["c0"])
    out = rig.round("cut", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert "release/r0" in out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert "release" not in _round_json(rig)


def test_cut_refuses_when_round_json_records_an_open_release(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("cut", **_fresh(rig, "c2")).returncode == 0
    again = rig.round("cut", **_fresh(rig, "c3"))
    assert again.returncode == 1
    assert "already records an open release (release/r1)" in again.stderr
    assert rig.origin_ref("release/r1") == rig.shas["c2"]


def test_cut_refuses_a_sha_off_mains_first_parent_line(rig: Rig) -> None:
    _open_round(rig)
    side = _child(rig, rig.shas["c0"], "side")  # not on main at all
    out = rig.round("cut", "--sha", side, **_fresh(rig, "c3"))
    assert out.returncode == 1
    assert "first-parent" in out.stderr
    # On main, but only as a merge's second parent: still off the line.
    c1_side = _child(rig, rig.shas["c1"], "merged-side")
    merge = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c3']}^{{tree}}",
        "-p",
        rig.shas["c3"],
        "-p",
        c1_side,
        "-m",
        "merge",
    )
    _push_branch(rig, "main", merge)
    out = rig.round("cut", "--sha", c1_side, **_fresh(rig, "c1"))
    assert out.returncode == 1
    assert "first-parent" in out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert rig.round("cut", "--sha", rig.shas["c3"]).returncode == 0


def test_cut_prints_a_late_line_for_each_marked_sha_it_lacks(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("in", rig.shas["c3"], PRECIS_ROUND_PEER="alice").returncode == 0
    assert rig.round("in", rig.shas["c1"], PRECIS_ROUND_PEER="bob").returncode == 0
    ghost = "ab" * 20
    marks = rig.root / ".git" / "precis-round" / "marks"
    (marks / "carol.json").write_text(
        json.dumps({"round": 1, "shas": [ghost], "status": "in"}), encoding="utf-8"
    )
    out = rig.round("cut", **_fresh(rig, "c2"))
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert f"late: alice {rig.shas['c3'][:9]}" in lines
    assert f"late: carol {ghost[:9]} (not fetched)" in lines
    assert not any(ln.startswith("late: bob") for ln in lines)
    # Printed before the success line.
    cut_line = next(i for i, x in enumerate(lines) if x.startswith("release/r1 cut at"))
    assert all(i < cut_line for i, x in enumerate(lines) if x.startswith("late:"))


def test_cut_prints_no_late_line_when_every_marked_sha_is_in(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("in", rig.shas["c1"], PRECIS_ROUND_PEER="bob").returncode == 0
    assert rig.round("none", PRECIS_ROUND_PEER="dan").returncode == 0
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 0, out.stderr
    assert "late:" not in out.stdout


def test_cut_refuses_a_new_duplicate_migration_number(rig: Rig) -> None:
    _open_round(rig)
    tip = _mig_commit(rig, "0001_a.sql", "0001_b.sql")
    out = rig.round("cut", LGM_SHA=tip, LGM_AGE="1")
    assert out.returncode == 1
    assert "0001: 0001_a.sql, 0001_b.sql" in out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert "release" not in _round_json(rig)


def test_cut_accepts_a_duplicate_migration_number_prod_already_carries(
    rig: Rig,
) -> None:
    _open_round(rig)
    tip = _mig_commit(rig, "0001_a.sql", "0001_b.sql", "archive/0002_x.sql")
    _push_branch(rig, "prod", tip)  # the duplicate shipped: accepted history
    nxt = _mig_commit(rig, "0002_c.sql")
    out = rig.round("cut", LGM_SHA=nxt, LGM_AGE="1")
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("release/r1") == nxt


def test_cut_dry_run_checks_everything_and_changes_nothing(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("in", rig.shas["c3"], PRECIS_ROUND_PEER="alice").returncode == 0
    state = rig.root / ".git" / "precis-round" / "round.json"
    before = state.read_bytes()
    out = rig.round("cut", "--dry-run", **_fresh(rig, "c2"))
    assert out.returncode == 0, out.stderr
    assert (
        f"dry-run: would push {rig.shas['c2'][:9]} to origin as release/r1"
        in out.stdout
    )
    assert f"late: alice {rig.shas['c3'][:9]}" in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert state.read_bytes() == before
    # A refusing check still refuses under --dry-run.
    _push_branch(rig, "release/r0", rig.shas["c0"])
    assert rig.round("cut", "--dry-run", **_fresh(rig, "c2")).returncode == 1


def test_status_shows_the_open_release(rig: Rig) -> None:
    _open_round(rig)
    assert "release" not in rig.round("status").stdout
    assert rig.round("cut", **_fresh(rig, "c2")).returncode == 0
    out = rig.round("status")
    assert f"release release/r1 at {rig.shas['c2'][:9]}, cut " in out.stdout
    assert " ago" in out.stdout


def test_abandon_deletes_a_release_whose_head_is_on_main(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("cut", **_fresh(rig, "c2")).returncode == 0
    dry = rig.round("cut", "--abandon", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert rig.origin_ref("release/r1") == rig.shas["c2"]
    out = rig.round("cut", "--abandon")
    assert out.returncode == 0, out.stderr
    assert "abandoned" in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert "release" not in _round_json(rig)
    # The slot is free again.
    assert rig.round("cut", **_fresh(rig, "c3")).returncode == 0


def test_abandon_refuses_and_lists_commits_not_on_main(rig: Rig) -> None:
    _open_round(rig)
    assert rig.round("cut", **_fresh(rig, "c2")).returncode == 0
    fix = _child(rig, rig.shas["c2"], "release fix not forwarded")
    _push_branch(rig, "release/r1", fix)
    out = rig.round("cut", "--abandon")
    assert out.returncode == 1
    assert "release fix not forwarded" in out.stderr
    assert fix[:7] in out.stderr
    assert rig.origin_ref("release/r1") == fix
    assert _round_json(rig)["release"]["branch"] == "release/r1"


def test_abandon_without_a_recorded_release_refuses(rig: Rig) -> None:
    _open_round(rig)
    out = rig.round("cut", "--abandon")
    assert out.returncode == 1
    assert "nothing to abandon" in out.stderr
