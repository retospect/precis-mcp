"""Content contract for the mesh-first survey runbook (gr613096).

A survey or gap analysis used to go straight to a draft: the two runbooks
an agent reaches for (``precis-source-comparison``, ``precis-paper-code-audit``)
opened with gather-then-``put(kind='draft')``, so the write-up cited bare
``[pc…]``/``[pa…]`` handles and its code-level facts had no held source.
The procedure lived only in memory (me613097, me612726). These tests pin
its promotion to a runtime skill: ``precis-gap-analysis`` carries the step
order (findings before the draft), both runbooks open by pointing at it,
and the skill-eval case turns red on a draft that cites a bare chunk.

The phrase "Build the mesh first" is load-bearing: gr663903 probes prod
for it before retiring the two memory nodes.
"""

from __future__ import annotations

import re

from precis.skill_eval import FakeRunner, Transcript, VerbCall, load_cases, run_cases
from tests._skill_roots import BUILTIN_SKILLS_DIR

SKILL = "precis-gap-analysis"
MESH = "Build the mesh first"
RUNBOOKS = ("precis-source-comparison", "precis-paper-code-audit")

_H2 = re.compile(r"^## (.+?)\s*$", re.M)
_FINDING_PUT = re.compile(r"""\bput\(\s*kind=["']finding["']""")
_DRAFT_PUT = re.compile(r"""\bput\(\s*kind=["']draft["']""")


def _body(slug: str) -> str:
    """The skill's markdown body, front matter stripped."""
    path = BUILTIN_SKILLS_DIR / f"{slug}.md"
    assert path.exists(), f"missing skill file: {path}"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{slug}: no front matter"
    return text.split("\n---\n", 1)[1]


def _section(body: str, heading: str) -> str:
    """Text of the ``## <heading>`` section, up to the next ``##``."""
    start = body.index(f"## {heading}\n")
    nxt = _H2.search(body, start + 3)
    return body[start : nxt.start() if nxt else len(body)]


def test_phrase_and_steps() -> None:
    body = _body(SKILL)
    for needle in (MESH, "me608724", "never bare"):
        assert needle in body, f"{SKILL}: missing {needle!r}"
    assert f"## {MESH}\n" in body, f"{SKILL}: {MESH!r} must be an H2 heading"
    assert body.count('rel="part-of"') >= 2, (
        f"{SKILL}: step 4 must show both part-of links (prior-art hub, topical hub)"
    )
    assert "</ul><hr>" in body, f"{SKILL}: Back Extra is an HTML list, then <hr>"
    mesh = body.index(f"## {MESH}\n")
    finding = _FINDING_PUT.search(body)
    draft = _DRAFT_PUT.search(body)
    assert finding, f"{SKILL}: no put(kind='finding') step"
    assert draft, f"{SKILL}: no put(kind='draft') step"
    assert mesh < finding.start() < draft.start(), (
        f"{SKILL}: the finding step must precede the draft step, both under {MESH!r}"
    )


def test_runbooks_open_with_mesh_section() -> None:
    for slug in RUNBOOKS:
        body = _body(slug)
        first = _H2.search(body)
        assert first and first.group(1) == MESH, (
            f"{slug}: first `##` section is {first.group(1) if first else None!r}, "
            f"want {MESH!r}"
        )
        assert f"[[{SKILL}]]" in _section(body, MESH), (
            f"{slug}: the {MESH!r} section must point at [[{SKILL}]]"
        )
    comparison = _section(_body("precis-source-comparison"), MESH)
    assert "[me<id>]" in comparison, "non-paper rows cite a memory node"
    audit = _section(_body("precis-paper-code-audit"), MESH)
    assert "cited_in" in audit, "the mismatch findings keep their cited_in door"


def test_eval_case_turns_red_on_a_draft_citing_a_bare_chunk() -> None:
    cases = load_cases(skills=[SKILL])
    assert cases, f"no skill-eval case for {SKILL}"
    case = cases[0]
    assert run_cases([case], FakeRunner()).status == "ok"
    straight_to_draft = Transcript(
        calls=(
            VerbCall(
                "put",
                "draft",
                {
                    "id": "x-gap-analysis",
                    "text": "Project X reaches 71 % task completion [pc42].",
                },
            ),
        ),
        answer="Wrote the comparison.",
    )
    report = run_cases([case], FakeRunner({case.case_id: straight_to_draft}))
    assert report.red and report.cases[0].status == "fail"
    reasons = report.cases[0].reasons
    assert any(
        r.startswith("expected call not made: put(kind='finding'") for r in reasons
    ), reasons
    assert any(
        r.startswith("forbidden call made: put(kind='draft'") for r in reasons
    ), reasons
