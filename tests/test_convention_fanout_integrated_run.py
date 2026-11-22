"""Fan-out gate: per-agent green is not integrated green (gr692288).

2026-09-28: one change was split across six subagents, each verified its
own slice and reported green. Run together, the same areas failed three
tests in ``tests/test_design_scrubber.py``: one agent had changed how a
leaf renders, another owned the test asserting rendered names, and no run
covered both. Six green reports read like coverage and are not.

The rule lives in ``AGENTS.md`` §Agent sizing, the section a caller reads
before it delegates: slice greens do not replace one integrated run over
the union of the areas the slices touched, the caller owns that run, and
it is the last step before landing (a run started before a later fix
describes a tree that no longer exists). The two remits whose return
carries a green, ``coder`` and ``test-author``, say that green covers
their slice only and point at the caller's run.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._policy_scan import read_text

_ROOT = Path(__file__).resolve().parent.parent

_SECTION = "## Agent sizing"

#: claim -> pattern, matched on lower-cased, whitespace-collapsed text.
_CALLER_CLAIMS: dict[str, re.Pattern[str]] = {
    "slice greens do not replace an integrated run": re.compile(
        r"slice greens? (do|does) not (replace|substitute for) (one|an|the) "
        r"integrated run"
    ),
    "the run covers the union of the touched areas": re.compile(
        r"union of the areas|union of touched areas"
    ),
    "the caller owns the run": re.compile(r"caller owns"),
    "the run is the last step before landing": re.compile(
        r"last (step|thing) before landing"
    ),
    "an earlier run describes a tree that no longer exists": re.compile(
        r"tree that no longer exists"
    ),
}

_REMIT_CLAIMS: dict[str, re.Pattern[str]] = {
    "the green covers this slice only": re.compile(
        r"covers (only )?your slice( only)?"
    ),
    "the caller owns the integrated run": re.compile(r"caller owns the integrated run"),
}

_REMITS = (".claude/agents/coder.md", ".claude/agents/test-author.md")


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def section(text: str, heading: str) -> str:
    """The body under ``heading`` up to the next heading of the same level."""
    level = heading.split(" ", 1)[0]
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip() == heading:
            inside = True
            continue
        if inside and line.startswith(level + " "):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def missing_claims(text: str, claims: dict[str, re.Pattern[str]]) -> list[str]:
    """Names of the ``claims`` whose pattern does not occur in ``text``."""
    flat = _flat(text)
    return [name for name, pattern in claims.items() if not pattern.search(flat)]


def test_agent_sizing_states_the_caller_owns_the_integrated_run() -> None:
    body = section(read_text(_ROOT / "AGENTS.md"), _SECTION)
    assert body.strip(), f"AGENTS.md has no `{_SECTION}` section — vacuous"
    gaps = missing_claims(body, _CALLER_CLAIMS)
    assert not gaps, (
        f"AGENTS.md `{_SECTION}` does not say who verifies a subagent "
        "fan-out as a whole (gr692288); missing:\n  " + "\n  ".join(gaps)
    )


def test_remits_say_their_green_covers_their_slice_only() -> None:
    gaps = [
        f"{rel}: {name}"
        for rel in _REMITS
        for name in missing_claims(read_text(_ROOT / rel), _REMIT_CLAIMS)
    ]
    assert not gaps, (
        "a remit that returns a green does not say the green is for its "
        "slice only and that the caller owns the integrated run "
        "(gr692288):\n  " + "\n  ".join(gaps)
    )


def test_gate_flags_a_section_silent_on_the_integrated_run() -> None:
    """Today's delegation text is flagged; text carrying every claim passes."""
    silent = (
        "## Agent sizing\n\nDelegate down: start each task on the cheapest "
        "agent that fits.\n\n## Next\n\nThe caller owns one integrated run.\n"
    )
    assert missing_claims(section(silent, _SECTION), _CALLER_CLAIMS) == list(
        _CALLER_CLAIMS
    )
    stated = (
        "## Agent sizing\n\nSlice greens do not replace one integrated\nrun "
        "over the union of the areas the slices touched. The caller owns "
        "it, as the last step before landing: an earlier run describes a "
        "tree that no longer exists.\n\n## Next\n"
    )
    assert missing_claims(section(stated, _SECTION), _CALLER_CLAIMS) == []
    assert missing_claims("Run the tests to green.", _REMIT_CLAIMS) == list(
        _REMIT_CLAIMS
    )
    assert (
        missing_claims(
            "That green covers your slice only: the caller owns the integrated run.",
            _REMIT_CLAIMS,
        )
        == []
    )
