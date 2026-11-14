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

from precis.plans import lanes, route, templates

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


# --- gr663138 / gr658931: run invocations, rescue evidence, checkout paths ---
#
# The fixtures are real prod texts, trimmed to the lines that tripped the
# router (the irrelevant middle cut, cluster addresses kept out).

#: td675895: gr665602's rescue report. ``scripts/test`` appears only inside
#: the log tail, as the command the dead builder ran.
_RESCUE_REPORT = """\
rescue class: builder
signature key: ! [rejected] slice/# -> slice/# (non-fast-forward)
first node: td664398
job: jo665497
step: retry cap 5: the lane ladder is spent (5 attempts; rungs tried: None/default → devin/swe-2-high → claude-sub/claude-opus-5-5)
branch: slice/664398
head: e47a116ad

log tail:
run_slice: session for td664398 exited clean but the pre-land defer failed (branch push of slice/664398: FAILED (To https://example.invalid/org/repo
 ! [rejected]            slice/664398 -> slice/664398 (non-fast-forward)
hint: U)); todo back to open, attempt:5
--- log tail ---
- **Tests:** new cases in `tests/test_run_slice.py`. I never ran `scripts/test tests/test_pla…`.
- **Step 14:** `scripts/test` on `tests/test_run_slice.py`, `tests/test_land_slice.py` gave 819 passed, 1 failed.
"""

#: td665622/td666424: a diagnose brief naming the slice's checkout path.
_DIAGNOSE_BRIEF = """\
You are a read-only diagnosis of td664398, rescued under class builder.

## The rescued todo
td664398: Build the fix for gr663909
Fix gr663909 directly: no triage and no brief ran. Touch nothing under the lane's deny paths. The land (land_slice, `scripts/ship --quick`) squash-merges onto main.

## Its newest brief:/handoff: comment
brief: rebase on origin/main and resolve tests/test_land_slice.py, keeping main's side for others' changes — mechanical red on jo665010: ✖ rebase conflict onto origin/main — resolve in /Users/deploy/precis-fix-repo/.claude/worktrees/slice-td664398, then 'git add -A && GIT_EDITOR=true git rebase --continue' and re-run scripts/ship

## The rescue evidence
rescue: builder
job: jo665497
step: retry cap 5: the lane ladder is spent
branch: slice/664398 @ e47a116ad
log tail:
run_slice: session for td664398 exited clean but the pre-land defer failed
--- log tail ---
w goes ahead only on that machine-set `done`.

## The signature gripe
gr665602: [rescue] builder: ! [rejected] slice/# -> slice/# (non-fast-forward)
rescue class: builder
log tail:
run_slice: session for td664398 exited clean

## What you may do
Read through the precis MCP and run read-only git.
"""

#: td663618: gr663138's own text, which names the harness only as examples
#: of commands to run.
_GR663138 = """\
deny-path router trips on the factory's own brief text: any brief that says "run scripts/test" routes to the coordinator
Observed 2026-11-13 (coordinator): td663028 routed td662214 to a coordinator showstopper because its rescue brief says "Run `scripts/test tests/…`" — a command to run, not a path to edit. Same false positive on open asks td659029, td652980, td647766 ("names deny path scripts/test"), and the .claude/worktrees variant gr658931.
Goal: the router matches only paths the brief asks to EDIT (diff targets, "edit/modify/change <path>"), never paths named as commands to run (`scripts/test`, `scripts/ship`) or quoted in logs; a test pins "Run scripts/test tests/x.py" as not routed. Related: router fix 2c191a6ff (td647766), gr658931, gr645163.
"""

#: td674827: gr663864 — the hand pre-land check runs the tree's own harness.
_GR663864 = """\
Skills: re-land-a-slice runbook lives in memory

New skill `reland-slice` (trigger: a land_slice job is red with no assertion):
- Local pre-land check: bundle `origin/main..slice/<id>`, fetch it into a scratch branch, add a worktree, run the branch tests plus the schema-baseline and dispatch ratchet tests through that tree's own `scripts/test`; edit through EnterWorktree(path=…); then quick-ship with the todo handle in the subject.
- A new `args` verb must also be listed in the dispatch passthrough roster test.
"""

#: td652980: gr623589 — a reproduce line that runs the harness on one test.
_GR623589 = """\
Cross-kind fan-out repeats the dispatch query embedding

Observed during OAuth verification. tests/test_cross_kind_fanout_bound.py::test_fan_out_shares_the_dispatch_query_embedding expects one embedding call and gets two.

Reproduce with scripts/test tests/test_cross_kind_fanout_bound.py::test_fan_out_shares_the_dispatch_query_embedding. Expected: one embedding computation shared by the dispatch fan-out. Actual: emb.calls == 2. Root cause is not established; do not relax the assertion merely to make the gate green.
"""

#: td674828: gr663867 asks for a new section IN a factory-governed skill
#: (``governs: factory``) — a genuine deny path, so it must route.
_GR663867 = """\
Skills: gripe-fleet lacks the pre-dispatch overlap check

Add a short section "Before you dispatch" to `gripe-fleet` (src/precis/data/skills/gripe-fleet.md and its .claude mirror):
- Run `scripts/inflight` and read the `.claude/purpose` lines for the gripe id or file.
- `scripts/fleet-wave cites ID…` lists unlanded branch commits naming it.
"""
_GOVERNED = ("src/precis/data/skills/gripe-fleet.md",)
#: the router's real deny set: strict deny plus the governed skills
_ALL_DENY = lanes.STRICT_DENY + _GOVERNED


@pytest.mark.parametrize(
    "text",
    [
        _RESCUE_REPORT,
        _DIAGNOSE_BRIEF,
        _GR663138,
        _GR663864,
        _GR623589,
        # bare run cues and the "on <files>" form
        "Fix: the pooled fixture leaks. Run scripts/test when done.",
        "Goal: stop the flake in guards.py; re-run `scripts/test` on tests/test_x.py",
        "Fix: guards.py flakes; step 14 ran scripts/test on `tests/test_x.py`.",
        "Fix: guards.py flakes (never ran `scripts/ship`).",
        "scripts/round --quick on src/precis/todo/guards.py",
        # a checkout location is not a write target
        "resolve in /Users/deploy/work/repo/.claude/worktrees/slice-td1 and retry",
        "the tree at .claude/worktrees/agent-a1b2c3 is dirty; fix guards.py",
        "`.claude/worktrees` holds the slice checkouts",
    ],
)
def test_run_invocation_evidence_and_checkout_never_route(text: str) -> None:
    assert route.coordinator_route(text, _ALL_DENY) is None


@pytest.mark.parametrize(
    ("text", "path"),
    [
        # a genuine edit request on the harness still routes
        ("Goal: scripts/test accepts --foo", "scripts/test"),
        ("Fix: edit deploy/group_vars/all.yml", "deploy/group_vars/all.yml"),
        ("Goal: scripts/ship should refuse a dirty tree", "scripts/ship"),
        # a run cue does not hide a later edit request in the same text
        ("Run scripts/test first.\n\nFix: edit scripts/test", "scripts/test"),
        ("Fix: make scripts/ship quieter, then run scripts/test", "scripts/ship"),
        # only the repo-relative remainder of a checkout path is judged
        (
            "Fix: edit /Users/deploy/work/repo/.claude/worktrees/slice-td1/scripts/ship",
            "scripts/ship",
        ),
        ("Goal: edit .claude/worktrees/t1/CLAUDE.md", "CLAUDE.md"),
        # a rescue report's non-evidence lines still classify
        (
            _RESCUE_REPORT.replace(
                "head: e47a116ad\n",
                "head: e47a116ad\nFix: edit scripts/ship so the push retries\n",
            ),
            "scripts/ship",
        ),
        # gr663867: the skill file IS the requested edit target
        (_GR663867, "src/precis/data/skills/gripe-fleet.md"),
        # a target ending its sentence keeps its glob match
        ("Fix: please edit scripts/ship.", "scripts/ship"),
        ("Run `scripts/test tests/x.py`, then edit scripts/ship.", "scripts/ship"),
    ],
)
def test_genuine_edit_request_still_routes(text: str, path: str) -> None:
    why = route.coordinator_route(text, _ALL_DENY)
    assert why is not None and f"`{path}`" in why
