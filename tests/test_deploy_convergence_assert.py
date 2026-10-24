"""The convergence assert must diagnose a concurrent deploy, not deny it.

2026-08-21/22 (docs/backlog/deploy-concurrency.md): every venv failed
"DEPLOY DID NOT CONVERGE" while reporting an installed sha one commit
AHEAD of the pin — a sibling deploy's commit. The old fail_msg asserted
the one thing that was true ("not a main that moved under it") and sent
the operator to re-run, which would have rolled the fleet back.

The fix runs in two places, both pinned here:

  (a) a controller-side task classifies each mismatching installed sha by
      git ancestry — descendant of the pin means a newer deploy landed
      mid-run, ancestor means a genuinely skipped/failed install — and it
      MUST run on localhost with become:false (the remote host has no
      repo; a become:true git fetch as root in the render worktree would
      hit safe.directory and leave root-owned objects);
  (b) the assert's fail_msg branches on that classification: descendant →
      "concurrent deploy, do not blindly re-run"; ancestor → "skipped
      install, re-run". Both still FAIL the run — making the
      uniform-but-ahead case a pass is a separate decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REDEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "redeploy-precis.yml"


def _convergence_play() -> dict[str, Any]:
    plays: list[dict[str, Any]] = yaml.safe_load(REDEPLOY.read_text(encoding="utf-8"))
    for play in plays:
        names = [t.get("name", "") for t in play.get("tasks", [])]
        if any("Assert each managed venv" in n for n in names):
            return play
    raise AssertionError(
        "the 'Verify every managed precis venv' play / its assert is gone"
    )


def _task(play: dict[str, Any], needle: str) -> dict[str, Any]:
    for task in play.get("tasks", []):
        if needle in task.get("name", ""):
            return task
    raise AssertionError(f"no task matching {needle!r} in the convergence play")


def test_the_classify_task_runs_on_the_controller_before_the_assert() -> None:
    play = _convergence_play()
    names = [t.get("name", "") for t in play.get("tasks", [])]
    classify = _task(play, "Classify each mismatching installed sha")
    idx_classify = names.index(classify["name"])
    idx_assert = next(i for i, n in enumerate(names) if "Assert each managed venv" in n)
    assert idx_classify < idx_assert, (
        "the ancestry classification must be registered before the assert reads it"
    )
    assert classify.get("delegate_to") == "localhost", (
        "ancestry needs the controller's checkout — the remote venv host "
        "has no repo to run merge-base in"
    )
    assert classify.get("become") is False, (
        "a become:true git fetch/cat-file in the playbook's checkout runs "
        "as root: safe.directory refuses, and a fetch that does get through "
        "leaves root-owned objects in the shared .git"
    )
    body = classify["ansible.builtin.shell"]
    assert "merge-base --is-ancestor" in body
    assert "descendant" in body and "ancestor" in body
    assert classify.get("register") == "precis_convergence_ancestry", (
        "the assert's fail_msg reads this register name"
    )
    loop = classify.get("loop", "")
    assert "precis_convergence.results" in str(loop)


def test_the_fail_msg_branches_on_ancestry() -> None:
    assert_task = _task(_convergence_play(), "Assert each managed venv")
    msg = assert_task["ansible.builtin.assert"]["fail_msg"]
    # Descendant: a newer deploy landed mid-run — named, and NOT told to
    # re-run (re-running would roll the fleet back).
    assert "_rel == 'descendant'" in msg
    assert "concurrent" in msg and "AHEAD" in msg
    # Ancestor: the install genuinely skipped/failed — this is the branch
    # that wants the re-run.
    assert "_rel == 'ancestor'" in msg
    assert "skipped/failed" in msg and "re-run" in msg
    # The old confident-wrong conclusion is gone: it asserted the one thing
    # that was true under a concurrent deploy.
    assert "not a main that moved under it" not in msg
