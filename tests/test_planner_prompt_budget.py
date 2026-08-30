"""The planner system prompt is paid on every tick — keep the menu cheap.

Measured against a real prod tick (2026-08-07, `llm_call_log` + `llm_blob`):

    system_prompt   53,610 chars
    prompt (task)      727 chars   ← 1.3% of the payload

…and 22,409 of the system prompt — **42%** — was a single injected block
listing all 141 active skills with their full `summary:` front-matter. Every
tick, on a lane where 88% of todos finish in exactly one tick.

It was redundant twice: the block's own header tells the model to
`search(kind='skill', q=...)`, and `precis-todo-tree-help`'s `## See also` already
names the nine skills a planner needs, ending "if none of the above fit".

Slugs stayed (cheap, self-describing, still advertise the namespace); summaries
went (one `get` away, as the header says). These tests pin that trade so the
menu can't quietly regrow into a per-tick tax again.
"""

from __future__ import annotations

import re

from precis.workers.planner_prompt import _build_skill_index

#: Generous ceiling — ~141 slugs at ~20 chars plus the header. The point is to
#: catch a regression back to per-entry prose (which was 7× this), not to
#: police a few hundred bytes as the skill set grows.
_MENU_BUDGET_CHARS = 6000


def test_skill_menu_stays_within_budget() -> None:
    menu = _build_skill_index()
    assert len(menu) <= _MENU_BUDGET_CHARS, (
        f"planner skill menu is {len(menu)} chars (budget {_MENU_BUDGET_CHARS}). "
        "It was 22,409 — 42% of the system prompt — before summaries were "
        "dropped. If skills genuinely grew, raise the budget deliberately; if a "
        "per-entry description crept back, that is the regression."
    )


def test_skill_menu_carries_no_per_entry_descriptions() -> None:
    """The specific shape that made it expensive: `- slug — summary` lines."""
    menu = _build_skill_index()
    prose_entries = [
        ln for ln in menu.splitlines() if ln.lstrip().startswith("- ") and " — " in ln
    ]
    assert not prose_entries, (
        f"{len(prose_entries)} per-skill description line(s) are back in the "
        "planner menu — that is the 22 KB shape. Summaries belong behind "
        "get(kind='skill', id=...)."
    )


def test_skill_menu_keeps_the_discovery_affordances() -> None:
    """Cutting summaries is only safe because both discovery paths remain."""
    menu = _build_skill_index()
    assert "get(kind='skill'" in menu
    assert "search(kind='skill'" in menu


def test_slugs_are_not_wrapped_mid_name() -> None:
    """`textwrap` breaks on hyphens by default and every slug is hyphenated.

    A wrapped `precis-\\nargument-help` reads as a name that doesn't resolve, so
    a model copying it back gets a miss — the failure mode is silent and the
    fix (`break_on_hyphens=False`) is one keyword.
    """
    menu = _build_skill_index()
    for line in menu.splitlines():
        assert not line.rstrip().endswith("-"), f"slug wrapped mid-name: {line!r}"


def test_every_listed_slug_is_resolvable() -> None:
    """A menu of names is only useful if each name is a real skill id."""
    from precis.handlers.skill import _load_skills_map

    menu = _build_skill_index()
    body = "\n".join(menu.splitlines()[1:])
    slugs = [s.strip() for s in re.split(r",\s*", body) if s.strip()]
    known = set(_load_skills_map())

    assert slugs, "menu listed no skills at all"
    unknown = [s for s in slugs if s not in known]
    assert not unknown, f"menu lists non-existent skill ids: {unknown[:5]}"
