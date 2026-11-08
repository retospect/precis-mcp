"""gr596926 / td574934 A2: the security-boundary registry.

``precis.plans.boundary`` names the path sets and added-line rules whose
diff always wants a security review — the agent envelope, the SSRF guard,
the factory's dispatch-and-land path, the credential modules, and the
registry itself. The registry is read-only today: nothing calls
``boundary_hits`` from a land yet (that is A3). These tests pin the list
to the tracked tree and the one behavioural rule that is live — a
``security-review`` lane pick refuses the vendor CLIs even when their
account rows claim the class.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from precis.plans import boundary, lanes
from tests._policy_scan import (
    ScanIncomplete,
    gitignore_matcher,
    walk_files,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[str]:
    """Every tracked repo-relative file — ``git ls-files`` when the
    checkout answers, else a filesystem walk honouring the root
    ``.gitignore`` (the container gate runs without ``.git``)."""
    if (_REPO_ROOT / ".git").exists():
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "-c",
                "safe.directory=*",
                "ls-files",
                "-z",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            raise ScanIncomplete(
                OSError(proc.returncode, "git ls-files exited"),
                "listing tracked files",
            )
        return [
            rel
            for rel in proc.stdout.split("\0")
            if rel and (_REPO_ROOT / rel).is_file()
        ]
    ignored = gitignore_matcher(_REPO_ROOT)
    return [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in walk_files(
            _REPO_ROOT,
            skip_dirs={".git", ".venv", ".claude", "node_modules", "__pycache__"},
        )
        if not ignored(p.relative_to(_REPO_ROOT).as_posix())
    ]


def _account(**kw: Any) -> Any:
    base = {
        "name": "x",
        "adapter": "claude_p",
        "plan": "subscription",
        "promo_expiry": None,
        "concurrency": 1,
        "task_classes": ("*",),
        "enabled": True,
        "models": (),
        "default_model": None,
        "vendor": "anthropic",
        "lane_rank": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_boundary_glob_matches_a_tracked_file() -> None:
    """A rename that would silently unwatch a boundary file reds here:
    every BOUNDARIES pattern and every CONTENT_RULES path glob names at
    least one tracked file."""
    tracked = _tracked_files()
    for b in boundary.BOUNDARIES:
        for glob in b.patterns:
            assert any(fnmatch.fnmatchcase(p, glob) for p in tracked), (
                f"{b.name}: {glob} matches no tracked file"
            )
    for r in boundary.CONTENT_RULES:
        for glob in r.paths:
            assert any(fnmatch.fnmatchcase(p, glob) for p in tracked), (
                f"{r.name}: {glob} matches no tracked file"
            )


def test_each_boundary_area_hits() -> None:
    """The four named areas plus the registry itself each hit on a
    canonical path."""
    cases = {
        "agent-envelope": "src/precis/workers/job_types/run_slice.py",
        "safe-fetch": "src/precis/utils/safe_fetch.py",
        "factory-dispatch": "src/precis/workers/job_types/plan_dispatch.py",
        "secrets": "src/precis/secrets.py",
        "boundary-registry": "src/precis/plans/lanes.py",
    }
    for name, path in cases.items():
        hits = boundary.boundary_hits([path])
        assert any(h.rule.name == name and path in h.paths for h in hits), name


def test_the_registry_and_its_probes_are_on_the_list() -> None:
    """boundary.py, lanes.py and scripts/probes/** protect themselves by
    being on the list: a boundary edit or a probe edit is a boundary hit."""
    for path in (
        "src/precis/plans/boundary.py",
        "src/precis/plans/lanes.py",
        "scripts/probes/deny-intact",
        "scripts/probes/nested/x.sh",
    ):
        hits = boundary.boundary_hits([path])
        assert any(
            h.rule.name == "boundary-registry" and path in h.paths for h in hits
        ), path


def test_privilege_sql_in_a_migration_hits_a_content_rule() -> None:
    mig = "src/precis/migrations/9999_test.sql"
    line = "GRANT SELECT ON refs TO agent_ro;"
    hits = boundary.boundary_hits([mig], {mig: [line, "-- harmless"]})
    content = [h for h in hits if isinstance(h.rule, boundary.ContentRule)]
    assert len(content) == 1
    assert content[0].rule.name == "migration-privilege-sql"
    assert content[0].paths == (mig,)
    assert content[0].lines == (line,)


def test_privilege_sql_folds_case_like_the_engine() -> None:
    """SQL keywords are case-insensitive — a lowercase ``grant`` must not
    evade the rule."""
    mig = "src/precis/migrations/9999_test.sql"
    hits = boundary.boundary_hits([mig], {mig: ["grant select on refs to r;"]})
    assert any(isinstance(h.rule, boundary.ContentRule) for h in hits)


def test_privilege_sql_in_python_does_not_hit() -> None:
    """The same text outside the rule's path glob does not fire."""
    hits = boundary.boundary_hits(
        ["src/precis/handlers/x.py"],
        {"src/precis/handlers/x.py": ['msg = "GRANT SELECT ON refs TO r"']},
    )
    assert hits == []


def test_security_review_is_an_account_task_class() -> None:
    """An account row may LIST the class; the pick below still refuses
    the vendor CLIs."""
    from precis.handlers import llm

    assert boundary.SECURITY_REVIEW in llm._ACCOUNT_TASK_CLASSES


def test_security_review_pick_refuses_the_vendor_clis() -> None:
    """A ``security-review`` pick launches only on
    ``SECURITY_LANE_ADAPTERS`` — a codex or devin lane reviewing the
    boundary its own builder crossed is no review. The account rows all
    list the class; the pick refuses them anyway."""
    table = lanes.derive_lanes(
        [
            _account(
                name="devin",
                adapter="devin_tree",
                vendor="devin",
                lane_rank=0,
                task_classes=("security-review",),
            ),
            _account(
                name="codex",
                adapter="codex_exec",
                vendor="openai",
                lane_rank=1,
                task_classes=("security-review",),
            ),
            _account(
                name="claude-sub",
                adapter="claude_p",
                vendor="anthropic",
                lane_rank=2,
                task_classes=("security-review",),
            ),
        ]
    )
    assert all(ln.fits("security-review") for ln in table)
    pick = boundary.pick_lane(None, "security-review", lanes=table, counts={})
    assert pick is not None
    assert pick.adapter == "claude_p"


def test_security_review_pick_is_none_without_a_security_adapter_lane() -> None:
    """Only vendor lanes enabled: the pick refuses rather than fall back
    — a security review on the builder's own CLI is waited on, never
    bypassed."""
    table = lanes.derive_lanes(
        [
            _account(
                name="devin",
                adapter="devin_tree",
                vendor="devin",
                task_classes=("security-review",),
            ),
            _account(
                name="codex",
                adapter="codex_exec",
                vendor="openai",
                task_classes=("security-review",),
            ),
        ]
    )
    assert boundary.pick_lane(None, "security-review", lanes=table, counts={}) is None


def test_fit_lanes_leaves_other_task_classes_alone() -> None:
    table = lanes.derive_lanes([_account(adapter="codex_exec")])
    assert boundary.fit_lanes("code", table) == table
    assert boundary.fit_lanes("review", table) == table


def test_unprobed_returns_hits_whose_probe_is_unset_or_absent(
    tmp_path: Path,
) -> None:
    """Every boundary today has ``probe=None``, so every hit is unprobed;
    a named probe counts only while its script exists in the tree."""
    hits = boundary.boundary_hits(["src/precis/plans/lanes.py"])
    assert hits and boundary.unprobed(hits) == hits

    probed = boundary.Boundary("x", ("a/**",), "scripts/probes/x", "why")
    hit = boundary.Hit(rule=probed, paths=("a/f",))
    assert boundary.unprobed([hit]) == [hit]
    (tmp_path / "scripts/probes").mkdir(parents=True)
    (tmp_path / "scripts/probes/x").write_text("#!/bin/sh\n", encoding="utf-8")
    assert boundary.unprobed([hit], root=tmp_path) == []
