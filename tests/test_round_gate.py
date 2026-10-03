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
  - neither verb takes the ship lock.

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

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs the shebang'd scripts/round and bash stubs",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "round"

_LGM_STUB = """#!/usr/bin/env bash
case " $* " in
  *" --age-hours "*) printf '%s\\n' "${LGM_AGE:-}" ;;
  *) printf '%s\\n' "${LGM_SHA:-}" ;;
esac
case " $* " in *" --age-hours "*) exit 0 ;; esac
exit "${LGM_RC:-0}"
"""

_DEPLOY_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/../deploy.log"
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


def test_docs_only_regex_is_the_one_check_yml_uses() -> None:
    loader = SourceFileLoader("round_script", str(SCRIPT))
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
