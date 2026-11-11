"""Ruling-meta call spellings in the skills (gr631567).

``tag``'s ``meta=`` is a todo-only door — on ``kind='memory'`` the
dispatcher refuses it with ``does not accept ['meta']`` — and ``edit``'s
slim MCP schema does not advertise top-level ``meta=`` (it still lands
as a deprecated extra, but the canonical spelling is
``args={'meta': …}``). The ruling docs showed ``tag(meta=…)`` and
``edit(meta=…)``; this pins the working spellings so they cannot drift
back.
"""

from __future__ import annotations

import re

from tests._skill_roots import skill_path

_SKILLS = (
    "precis-spec-help",
    "precis-memory-help",
    "precis-tag-help",
)

#: A ``tag(`` / ``edit(`` call carrying ``meta=`` before its closing
#: paren — the refused (tag) and deprecated (edit) spellings. The
#: ``args={'meta': …}`` form writes meta as a dict key (``'meta':``, no
#: ``=``) so it never matches.
_TOP_LEVEL_META = re.compile(r"\b(?:tag|edit)\([^)]*\bmeta\s*=")


def _body(slug: str) -> str:
    return skill_path(slug).read_text(encoding="utf-8")


def test_no_top_level_meta_on_tag_or_edit() -> None:
    for slug in _SKILLS:
        bad = _TOP_LEVEL_META.findall(_body(slug))
        assert not bad, (
            f"{slug}: meta= on tag( is refused (todo-only door) and on "
            f"edit( is deprecated — spell it args={{'meta': …}}: {bad}"
        )


def test_ruling_docs_show_the_args_meta_spelling() -> None:
    spec = _body("precis-spec-help")
    assert "args={'meta'" in spec, (
        "precis-spec-help ruling node lost the args={'meta': …} spelling"
    )
    mem = _body("precis-memory-help")
    assert 'args={"meta": {"status": "absorbed"}}' in mem, (
        "precis-memory-help rulings example lost the args={meta: …} spelling"
    )
