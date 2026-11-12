"""gr645163: the mint-time coordinator route's classifier.

Work no builder lane may write — a judged text naming a deny path, or a
report reduced to graph handles alone (prod-graph only) — routes to one
coordinator ask instead of burning a build (Reto 2026-11-12, "escalate
to next model"). These pin the classifier itself: the deny-glob match,
the graph-only shape, and the boilerplate strips that keep the
factory's own instructions — the ``gripe-fix-direct`` head, the slice
brief's deny-list/land-lint sections, the stock land-mechanism phrases
factory bodies carry — from reading as the work's target.
"""

from __future__ import annotations

import pytest

from precis.plans import route, templates

_DENY = (
    "scripts/ship",
    "scripts/test",
    "src/precis/plans/**",
    ".github/**",
    "deploy/inventory*/**",
    "CLAUDE.md",
    "*/CLAUDE.md",
    ".mcp.json",
)


@pytest.mark.parametrize(
    "text",
    [
        "add a --remote flag to scripts/ship",
        "the fix touches `scripts/ship`",
        "edit ./scripts/ship so it takes --remote",
        "the land broke; fix Scripts/Ship",
        "the new rule lives in src/precis/plans/lanes.py",
        "CI needs .github/workflows/test.yml updated",
        "rotate the host in deploy/inventory-prod/hosts.yml",
        "fix CLAUDE.md's stale rule",
        "the slice rules moved to .claude/CLAUDE.md",
        "refresh .mcp.json's server list",
    ],
)
def test_deny_path_mention_routes(text: str) -> None:
    why = route.coordinator_route(text, _DENY)
    assert why is not None and why.startswith("names deny path")


@pytest.mark.parametrize(
    "text",
    [
        # a neighbouring path is not a glob hit
        "fix scripts/shippy's flag parsing",
        "the helper lives in scripts/shipping/label.py",
        # ordinary work text
        "fix the widget in src/precis/todo/guards.py",
        "the digest misreads gr645163's title",
        "plan_dispatch skipped the node",
    ],
)
def test_no_deny_path_means_no_route(text: str) -> None:
    assert route.coordinator_route(text, _DENY) is None


def test_handle_only_report_is_graph_only() -> None:
    why = route.coordinator_route("gr630346 me123456 td999", _DENY)
    assert why is not None and "graph" in why


def test_handles_inside_prose_are_not_graph_only() -> None:
    assert route.coordinator_route("fix gr630346's stale edge", _DENY) is None


@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_empty_text_never_routes(text: str | None) -> None:
    assert route.coordinator_route(text, _DENY) is None


def test_direct_head_never_routes_but_the_report_does() -> None:
    head = "Fix gr1 directly: find the code it names. " + templates.DIRECT_HEAD_END
    assert route.coordinator_route(head + "\nclean the cache", _DENY) is None
    why = route.coordinator_route(head + "\nfix scripts/ship", _DENY)
    assert why is not None and "scripts/ship" in why


def test_brief_boilerplate_sections_never_route() -> None:
    text = (
        "fix the flake in src/precis/todo/guards.py\n\n"
        "## Paths you may not write\n\n- `scripts/ship`\n- `.github/**`\n\n"
        "## When done\n\n1. `uv run pytest -q tests/test_x.py`\n"
    )
    assert route.coordinator_route(text, _DENY) is None


@pytest.mark.parametrize(
    "text",
    [
        # the release-fixer's stock body tail
        "Read the brief: comment on this todo -- the digest of the red "
        "run's failed jobs -- and fix the failures it names; land with "
        "scripts/ship --quick. Fix forward: never skip.",
        # the revert task's stock land instruction
        "commit with a `revert(...)` subject and land it as a normal "
        "slice (scripts/ship).",
        # the gripe-fix chain's build body
        "run_slice on the lane triage picked, in its own worktree on "
        "`slice/<id>`. Then the land (land_slice, `scripts/ship --quick`): "
        "rebase on main, lint (fix if red), squash-merge onto main.",
        # the land node's stock body (a non-run_slice op, kept honest)
        "land_slice lands slice/pr-3 (the PR head) with scripts/ship "
        "--checked, then closes the PR.",
    ],
)
def test_factory_land_mechanics_never_route(text: str) -> None:
    assert route.coordinator_route(text, _DENY) is None
