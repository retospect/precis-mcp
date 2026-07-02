"""Byte-equivalence guard for the ADR 0038 step-3 reviewer refactor.

The two tree reviewers (``structural``, ``deep_review``) were migrated off
their ``str.format`` templates onto the shared ``utils.prompt`` assembler +
:class:`ClaudeAgentAdapter` (ADR 0038 step 3). The verbatim-duplicated
boilerplate — the "Define your abbreviations" paragraph and the "Do not
address anyone / the only put you may make is a gripe" footer (with its gripe
carve-out) — was deduped into ONE shared module set reused by both reviewers.

This is a *refactor, not a rewrite*: the assembled directive prompt must
reproduce the pre-refactor template output. The reference is a verbatim copy
of the pre-refactor templates (``_LEGACY_STRUCTURAL_TEMPLATE`` /
``_LEGACY_DEEP_TEMPLATE``), captured here as the known-good golden. Each test
renders the new module list against a fixture context and asserts it matches
the golden, DOCUMENTING the only two whitespace-level differences:

1. **Trailing newline (both reviewers).** The source template literals ended
   with a single ``\\n`` (the line break before the closing triple-quote); the
   assembler strips each block's trailing newline (``Block`` text is
   ``.strip("\\n")``-ed), so the assembled prompt omits that one ``\\n``. No
   content moves.
2. **One soft-wrap reflow (deep only).** The shared footer fixes ONE hand
   wrapping of the sentence "Your final stdout IS the digest body." The two
   templates had wrapped it differently *because the interpolated tier tag has
   a different length* (``tier:structural`` vs ``tier:deep`` shifted the fill).
   The shared footer adopts the ``structural`` wrapping, so ``structural`` is
   byte-identical there and ``deep`` differs only by where that one line
   breaks — a whitespace reflow, no word changed. Proven below by collapsing
   all whitespace and asserting the token streams are identical.
"""

from __future__ import annotations

from precis.utils.prompt import (
    AssemblyContext,
    ClaudeAgentAdapter,
    Profile,
    assemble,
)
from precis.workers.deep_review import DEEP_REVIEW
from precis.workers.review import Reviewer
from precis.workers.structural import STRUCTURAL

# --------------------------------------------------------------------------
# golden reference — verbatim copies of the PRE-refactor templates
# --------------------------------------------------------------------------

_LEGACY_STRUCTURAL_TEMPLATE = """STRUCTURAL REVIEW — {today}

You are reviewing the asa todo tree for *structural* problems that
SQL can't detect. Below is a snapshot of the strategic + tactical
layer and the currently-open nursery alerts. If you need to drill in,
use the `precis` MCP tool (`get(kind='todo', id=N, view='tree')` to
read a subtree; `search(kind='todo', view='doable')` for next
actions).

## Strategic + tactical layer (snapshot)

{strategic_layer}

## Open nursery alerts

{nursery_excerpt}

## What to look for

Look for issues that need semantic judgment, not the rules-based
checks the nursery already runs:

1. **Branches missing an outcome line.** A node with children
   should read as "what does done look like" on its first line.
   If the first line reads as an action verb ("Draft the…",
   "Write the…", "Fix the…"), call it out — the branch is a
   project mis-labelled as an action.
2. **Drift between branch outcome and child actions.** The
   children should plausibly ladder to the outcome. If a branch
   says "Submitted to JCP, all figs camera-ready" but its
   children are about "set up the lab notebook", flag the drift.
3. **Sibling contradictions.** Two children whose work undoes
   each other (one renames X, the other depends on the old
   name), or two that compete for the same artifact without one
   blocking the other.
4. **Depth/fanout warnings.** Tactical branches with 8+ direct
   subtasks (probably under-decomposed) or three-level pillars of
   single-child branches (probably over-decomposed). Plan cap is
   depth 10; flag anything approaching it.

## Output format

Write a markdown digest. Start with a one-line summary. Then a
section per problem type (only those with findings). Each finding
references the ref by id. Be specific — name what's wrong and
suggest the next move. If the tree looks clean, say so explicitly
("No structural issues this pass"); we still write the digest so
the audit log shows the review ran.

**Define your abbreviations.** A memory has no glossary, so spell out
each abbreviation on first use — write `AGNR (armchair graphene
nanoribbon)`, not a bare `AGNR`. This covers all-caps acronyms and
hyphenated compounds (`GNR-FET`).

Do not address anyone. Do not use the precis MCP `put` tool to
write a memory directly — the worker will write your output as a
memory tagged `tier:structural` after you finish. Your final stdout
IS the digest body.

Exception: if a precis tool itself errored or returned wrong results
while you were reviewing (tooling friction, not a tree finding), you
may `put(kind='gripe', text=…)` — search existing gripes first. That
is the only `put` you may make; your digest still goes to stdout, not
to a memory.
"""

_LEGACY_DEEP_TEMPLATE = """DEEP REVIEW — {today}

You are reviewing the asa todo tree at the weekly cadence (Allen's
"deep review" tier). Below is the strategic dashboard with 7d
picks accounting and a summary of the last week's nursery +
structural digests. Use the `precis` MCP tool to drill into any
subtree you need to look at (`get(kind='todo', id=N, view='tree')`).

## Strategic dashboard

{strategic_dashboard}

## Recent review summary (last 7 days)

{recent_review_summary}

## What to do

Produce a markdown digest organised into five sections (skip any
section with no recommendations):

1. **Archive candidates** — strategics whose work is functionally
   done. Name the strategic id and the evidence (last done leaf,
   no open descendants, etc.). Suggest the operator close them
   with `tag(id=N, add=['STATUS:done'])`.

2. **Pruning candidates** — branches whose subtree is stale,
   irrelevant, or duplicates work elsewhere. Suggest soft-deletes
   with reasoning.

3. **Decomposition budget warnings** — strategics approaching the
   soft cap of 30 descendants (knob #5 in the plan). Suggest
   which subtrees could be pruned, archived, or split out into
   their own strategic.

4. **Rotation rebalancing** — strategics that have drifted from
   their 1/N share (very few or very many picks vs expected).
   Note whether the imbalance is workload-driven (legitimate) or
   crowding-out (pause / re-PRIO).

5. **Long-running waits** — `waiting-for:*` leaves > 7d that
   probably need the dependency replaced or the wait converted to
   an ask-user leaf.

End with one or two paragraphs of qualitative narrative — what's
the tree telling you about how the week went? Use this for
continuity; asa-bot's preamble surfaces recent memories so a good
narrative gets quoted back in chat.

**Define your abbreviations.** A memory has no glossary, so spell out
each abbreviation on first use — write `AGNR (armchair graphene
nanoribbon)`, not a bare `AGNR`. This covers all-caps acronyms and
hyphenated compounds (`GNR-FET`).

Do not address anyone. Do not use the precis MCP `put` tool to
write a memory directly — the worker will write your output as a
memory tagged `tier:deep` after you finish. Your final stdout IS
the digest body.

Exception: if a precis tool itself errored or returned wrong results
while you were reviewing (tooling friction, not a tree finding), you
may `put(kind='gripe', text=…)` — search existing gripes first. That
is the only `put` you may make; your digest still goes to stdout, not
to a memory.
"""


# --------------------------------------------------------------------------
# helpers — assemble the reviewer's module list without touching the DB
# --------------------------------------------------------------------------


def _assemble(reviewer: Reviewer, **extras: str) -> str:
    """Render ``reviewer.modules`` against a fixture ``extras`` (no store).

    Mirrors :func:`precis.workers.review._build_prompt` but injects the
    context strings directly instead of running the SQL context-builder, so
    the golden comparison is deterministic and offline.
    """
    ctx = AssemblyContext(
        store=None,
        ref_id=0,
        model=reviewer.model,
        profile=Profile.AGENT,
        extras={"tier_tag": reviewer.tier_tag, **extras},
    )
    system, user = ClaudeAgentAdapter.render(assemble(reviewer.modules, ctx))
    # Reviewers are a single flat directive: nothing lands in the cached half.
    assert system == ""
    return user


_FIXTURE_STRUCTURAL = {
    "today": "2026-07-02",
    "strategic_layer": "#1 Main goal\n  └─ #2 Tactic A (2 direct children)",
    "nursery_excerpt": "(no open nursery alerts)",
}

_FIXTURE_DEEP = {
    "today": "2026-07-02",
    "strategic_dashboard": "#1 Main  (3 descendants, 1 picks in 7d)",
    "recent_review_summary": "(no open nursery alerts or structural digests"
    " in the last 7 days)",
}


# --------------------------------------------------------------------------
# structural — byte-identical modulo the single stripped trailing newline
# --------------------------------------------------------------------------


def test_structural_prompt_matches_legacy_template() -> None:
    assembled = _assemble(STRUCTURAL, **_FIXTURE_STRUCTURAL)
    legacy = _LEGACY_STRUCTURAL_TEMPLATE.format(**_FIXTURE_STRUCTURAL)

    # The ONLY difference is the single trailing newline the assembler strips.
    assert legacy.endswith("\n")
    assert not assembled.endswith("\n")
    assert assembled + "\n" == legacy


# --------------------------------------------------------------------------
# deep — identical wording; two documented whitespace diffs
# --------------------------------------------------------------------------


def test_deep_prompt_matches_legacy_wording() -> None:
    assembled = _assemble(DEEP_REVIEW, **_FIXTURE_DEEP)
    legacy = _LEGACY_DEEP_TEMPLATE.format(**_FIXTURE_DEEP)

    # Diff 1 — trailing newline (as above).
    assert legacy.endswith("\n")
    assert not assembled.endswith("\n")

    # Diff 2 — one soft-wrap reflow in the shared footer. The shared footer
    # adopts the `structural` wrapping ("... Your final stdout\nIS the digest
    # body."); the deep template hand-wrapped it one word later ("... Your
    # final stdout IS\nthe digest body.") because `tier:deep` is shorter. No
    # word changes — only where that line breaks.
    assert "Your final stdout\nIS the digest body." in assembled
    assert "Your final stdout IS\nthe digest body." in legacy

    # Wording is byte-identical once all whitespace is collapsed: this proves
    # the two diffs above are the ONLY differences (both whitespace-only).
    assert " ".join(assembled.split()) == " ".join(legacy.split())


# --------------------------------------------------------------------------
# dedup — the shared boilerplate is authored once and reused by both
# --------------------------------------------------------------------------


def test_shared_boilerplate_modules_are_the_same_objects() -> None:
    """The abbreviations + footer modules are the SAME instances in both
    reviewers' module lists — the dedup is structural, not a copy."""
    assert STRUCTURAL.modules[1:] == DEEP_REVIEW.modules[1:]
    # …and there are exactly two shared trailing modules after each body.
    assert [m.id for m in STRUCTURAL.modules] == [
        "structural.body",
        "reviewer.abbreviations",
        "reviewer.footer",
    ]
    assert [m.id for m in DEEP_REVIEW.modules] == [
        "deep_review.body",
        "reviewer.abbreviations",
        "reviewer.footer",
    ]


def test_gripe_carveout_survives_in_shared_footer() -> None:
    """The gripe carve-out (only put you may make) rides the shared footer for
    both reviewers, with each reviewer's own tier tag interpolated."""
    for reviewer, fixture, tier in (
        (STRUCTURAL, _FIXTURE_STRUCTURAL, "tier:structural"),
        (DEEP_REVIEW, _FIXTURE_DEEP, "tier:deep"),
    ):
        prompt = _assemble(reviewer, **fixture)
        assert (
            "may `put(kind='gripe', text=…)` — search existing gripes first." in prompt
        )
        assert "is the only `put` you may make" in prompt
        assert f"memory tagged `{tier}`" in prompt
