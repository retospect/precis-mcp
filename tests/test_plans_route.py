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
    "scripts/lib/**",
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


@pytest.mark.parametrize(
    "text",
    [
        'scripts/test tests/test_pcb_examples_route.py -m "slow or not slow"',
        "3. `scripts/test --typecheck` clean.",
        "run scripts/test -k quota_refusal and report",
    ],
)
def test_harness_run_as_a_command_never_routes(text: str) -> None:
    """td647606: an acceptance line that RUNS the harness names no write
    target — every slice brief carries one."""
    assert route.coordinator_route("fix the flood in wires.py\n" + text, _DENY) is None


def test_file_set_alone_is_judged() -> None:
    """td647606: a declared write set is the builder's whole target —
    harness mentions elsewhere in the brief don't route, a harness path
    inside it does."""
    brief = (
        "Goal: GND routes.\n\n"
        "File set: src/precis/pcb/topoplace/wires.py, tests/test_wires.py\n\n"
        "Acceptance: scripts/test passes; see scripts/ship for the land.\n"
    )
    assert route.coordinator_route(brief, _DENY) is None
    harness = brief.replace("tests/test_wires.py", "scripts/test")
    why = route.coordinator_route(harness, _DENY)
    assert why is not None and "scripts/test" in why


def test_handle_only_report_is_graph_only() -> None:
    why = route.coordinator_route("gr630346 me123456 td999", _DENY)
    assert why is not None and "graph" in why


@pytest.mark.parametrize(
    "text",
    [
        # the review td645206 example: a graph edit phrased as a sentence
        "remove the stale edge from me123456",
        "retire the duplicate node me123456",
        "relink me123456 part-of me654321",
        "close td123 and mark gr456 answered",
        "update the title on me123456",
        "the edge between me1 and me2 is stale — delete it",
        # a handle's possessive still names the row
        "remove gr630346's stale edge",
    ],
)
def test_graph_edit_in_prose_is_graph_only(text: str) -> None:
    """td645566: the classifier read handles-only reports; a graph edit
    in plain prose still named no repo target and burned a builder run."""
    why = route.coordinator_route(text, _DENY)
    assert why is not None and "graph" in why


@pytest.mark.parametrize(
    "text",
    [
        # generic verbs stay ambiguous — a fix can mean the code that
        # wrote the edge, so it still builds
        "fix gr630346's stale edge",
        # repo words anywhere in the sentence keep it a build
        "remove the stale guard from plan_dispatch",
        "update the edge handling in src/precis/todo/guards.py",
        "delete the flaky test for gr630346",
        "close the issue by editing the retry cap",
        # no handle at all is never graph-only
        "remove the stale edge",
    ],
)
def test_non_graph_prose_is_not_graph_only(text: str) -> None:
    assert route.coordinator_route(text, _DENY) is None


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


@pytest.mark.parametrize(
    ("text", "expected_path"),
    [
        pytest.param(
            "Symptom (2026-11-12 UTC): a CLAUDE.md docs commit deployed, "
            "but release confirmation still reported a stale release.\n\n"
            "Fix: repair release_confirm in src/precis/workers/release_confirm.py.",
            None,
            id="gr645561-evidence-only",
        ),
        pytest.param(
            "The devin brief describes quick land / scripts/ship --quick. "
            "The land_slice job runs scripts/ship --quick in the worktree; "
            "repair the devin brief text so it describes that handoff.",
            None,
            id="gr635349-command-invocation",
        ),
        pytest.param(
            "`scripts/round open` refuses the next cut. "
            "Round open has no step that reruns failed soak legs.",
            "scripts/round",
            id="gr639737-round-target",
        ),
        pytest.param(
            "Goal: enumerate every code path through scripts/ship and its gate "
            "paths; ensure home-made caps are retired.",
            "scripts/ship",
            id="gr647996-harness-target",
        ),
    ],
)
def test_report_change_intent_routes(text: str, expected_path: str | None) -> None:
    why = route.coordinator_route(text, (*_DENY, "scripts/round"))
    if expected_path is None:
        assert why is None
    else:
        assert why is not None and f"`{expected_path}`" in why


@pytest.mark.parametrize("label", ["Goal", "Fix", "Missing", "Want", "Expected"])
def test_intent_paragraph_ignores_surrounding_evidence(label: str) -> None:
    text = (
        "Symptom: CLAUDE.md was the only file in the deployed commit.\n\n"
        f"{label}: repair the release confirmation worker.\n\n"
        "Evidence: scripts/ship reported success."
    )
    assert route.coordinator_route(text, _DENY) is None


@pytest.mark.parametrize(
    "target", ["src/precis/workers/release_confirm.py", "scripts/ship"]
)
def test_markdown_intent_heading_keeps_following_paragraph(target: str) -> None:
    text = (
        "## Symptom\n\nCLAUDE.md was deployed.\n\n"
        f"## Fix\n\nRepair `{target}`.\n"
        "## Evidence\nThe command scripts/test failed."
    )
    why = route.coordinator_route(text, _DENY)
    if target == "scripts/ship":
        assert why is not None and "`scripts/ship`" in why
    else:
        assert why is None


@pytest.mark.parametrize(
    "label", ["Symptom (2026-11-12 UTC)", "Route", "Evidence", "Seen"]
)
def test_evidence_label_ends_intent_without_blank_line(label: str) -> None:
    text = f"Fix: repair the worker.\n{label}: scripts/ship reported success."
    assert route.coordinator_route(text, _DENY) is None


@pytest.mark.parametrize(
    "evidence",
    [
        "> scripts/ship reported success.",
        "```log\nscripts/ship reported success.\n```",
        "~~~console\nscripts/ship reported success.\n~~~",
    ],
)
def test_quoted_log_inside_intent_does_not_route(evidence: str) -> None:
    assert (
        route.coordinator_route(f"Fix: repair the worker.\n{evidence}", _DENY) is None
    )


def test_multiline_and_multiple_intent_paragraphs_keep_deny_targets() -> None:
    text = (
        "Goal: repair the worker.\n\n"
        "Evidence: CLAUDE.md was deployed.\n\n"
        "Fix: adjust the release gate in\n"
        "scripts/ship to verify the worker's confirmation."
    )
    why = route.coordinator_route(text, _DENY)
    assert why is not None and "`scripts/ship`" in why


def test_evidence_without_intent_keeps_whole_text_fallback() -> None:
    why = route.coordinator_route("Symptom: scripts/ship cannot land a branch.", _DENY)
    assert why is not None and "`scripts/ship`" in why


def test_file_set_takes_precedence_over_intent() -> None:
    text = "Goal: repair scripts/ship.\n\nFile set: src/precis/workers/release_confirm.py\n"
    assert route.coordinator_route(text, _DENY) is None


def test_intent_selection_does_not_loosen_graph_only_rule() -> None:
    text = "Symptom: the worker writes stale edges.\n\nGoal: remove the stale edge from me123456."
    assert route.coordinator_route(text, _DENY) is None


@pytest.mark.parametrize(
    "text",
    [
        "Goal: repair scripts/ship\n\nExpected: tests/test_ship.py passes",
        "Fix: repair scripts/ship\n> previous failure log\ntests/test_ship.py covers it",
    ],
)
def test_intent_selection_does_not_create_command_invocations(text: str) -> None:
    why = route.coordinator_route(text, _DENY)
    assert why is not None and "`scripts/ship`" in why


@pytest.mark.parametrize("language", ["", "text", "sh"])
def test_fenced_intent_target_still_routes(language: str) -> None:
    text = f"Fix: update these files:\n```{language}\nscripts/ship\n```"
    why = route.coordinator_route(text, _DENY)
    assert why is not None and "`scripts/ship`" in why


_WORK = "fix the widget flake in src/precis/todo/guards.py"


@pytest.mark.parametrize(
    "mention",
    [
        # gr573439/gr573440: a scope note, not a target
        "no deploy/.github/scripts paths",
        "never edit scripts/ship for this",
        "without touching .github/workflows/check.yml",
        "the fix belongs in guards.py, not scripts/lib/lock-holder.sh",
        "do not touch `scripts/lib/**`",
    ],
)
def test_negated_deny_mention_does_not_route(mention: str) -> None:
    """gr653037: a negated mention names a path the fix must NOT edit."""
    assert route.coordinator_route(f"{_WORK}; {mention}.", _DENY) is None


@pytest.mark.parametrize(
    "note",
    [
        "dispatch: routes to the coordinator — report names deny path "
        "`scripts/lib/lock-holder.sh` (matched `scripts/lib/**`); ask td1. "
        "No chain minted; `hold:no-dispatch` holds the gripe — remove the "
        "tag to release it back to dispatch.",
        "plan_dispatch: routes to the coordinator — names deny path "
        "`.github/workflows/check.yml` (matched `.github/**`); ask td2",
        "Coordinator note — released; the earlier hold quoted "
        "scripts/lib/lock-holder.sh only.",
    ],
)
def test_routing_note_does_not_route(note: str) -> None:
    """gr653037: a routed gripe's own routing comment (or the coordinator's
    note on it) quotes the deny path; it must not re-route the gripe."""
    assert route.coordinator_route(f"{_WORK}\n\n{note}", _DENY) is None
    assert route.routing_note(note)


@pytest.mark.parametrize(
    "mention",
    [
        # td652194: a review brief citing a harness file as checked
        "Cross-checked against scripts/lib/lock-holder.sh: same lock format.",
        "The new lock line matches scripts/lib/lock-holder.sh's format.",
        "Related: scripts/lib/lock-holder.sh",
        "Evidence: scripts/ship reported success.",
        "The lock format (cf. scripts/lib/lock-holder.sh) is unchanged.",
        "`scripts/lib/lock-holder.sh` cross-checked, no drift.",
    ],
)
def test_cited_deny_mention_does_not_route(mention: str) -> None:
    """gr653037: a deny path cited as evidence is not the edit target."""
    assert route.coordinator_route(f"{_WORK}.\n\n{mention}", _DENY) is None


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("the fix is in scripts/lib/lock-holder.sh", "scripts/lib/lock-holder.sh"),
        ("edit .github/workflows/check.yml", ".github/workflows/check.yml"),
        # a negated mention elsewhere hides no requested target
        (
            "no deploy paths; the fix is in scripts/lib/lock-holder.sh",
            "scripts/lib/lock-holder.sh",
        ),
        (
            "The fix is not in guards.py, it's in scripts/lib/lock-holder.sh",
            "scripts/lib/lock-holder.sh",
        ),
    ],
)
def test_requested_deny_edit_still_routes(text: str, path: str) -> None:
    why = route.coordinator_route(text, _DENY)
    assert why is not None and f"`{path}`" in why


@pytest.mark.parametrize(
    ("body", "brief", "routes"),
    [
        ("Goal: repair release confirmation", "Update scripts/ship", True),
        (
            "Goal: repair release confirmation\n\nEvidence: scripts/ship reported success",
            "Update the release confirmation worker",
            False,
        ),
        ("Goal: update scripts/ship", "Repair release confirmation", True),
        # One document's write set cannot hide the other's requested target.
        ("File set: worker.py", "Update scripts/ship", True),
        ("Goal: update scripts/ship", "File set: worker.py", True),
        # Graph-only still means the whole task names no repo work.
        ("remove the stale edge from me123456", "Update worker.py", False),
        ("Update worker.py", "remove the stale edge from me123456", False),
        ("remove the stale edge from me123456", "retire me654321", True),
    ],
)
def test_body_and_brief_keep_separate_deny_path_scopes(
    body: str, brief: str, routes: bool
) -> None:
    # An iterable deny set must remain available for both documents.
    why = route.coordinator_route(body, iter(_DENY), brief=brief)
    assert (why is not None) is routes
