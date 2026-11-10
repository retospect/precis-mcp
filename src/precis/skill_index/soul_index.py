"""Soul conduct layer — generated root index (gr563975).

The staged soul memories (root ``me563907`` plus its 18 children,
staged at Reto's request 2026-11-04) migrated into ordinary skills:
``soul.md`` is the root index and ``soul-<name>.md`` the entries —
eight role indexes and ten principles, all in the same file shape.

The two entry tables inside ``soul.md`` are **generated from the
entries themselves** — slug, ``soul-group:`` frontmatter, the
first-person hook line and the ``staged-from:`` provenance handle —
so the index can never drift from the content it lists: the entries
are the one canonical source, capped at :data:`ENTRY_CAP` rows.
``scripts/gen-soul-index`` rewrites the marked block in place;
``tests/test_soul_index.py`` fails on drift, on a missing contract
field, or past the cap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from precis.skill_index.chunker import _strip_front_matter
from precis.utils.skill_common import parse_frontmatter

#: Root skill slug; entries are ``soul-<name>.md`` siblings in the
#: same flat directory.
ROOT_SLUG: Final = "soul"

#: ``soul-group:`` frontmatter values and their emit order — role
#: indexes list before principles.
GROUP_ROLE: Final = "role"
GROUP_PRINCIPLE: Final = "principle"
_GROUPS: Final = (GROUP_ROLE, GROUP_PRINCIPLE)
_GROUP_HEADINGS: Final = {
    GROUP_ROLE: "### Role indexes",
    GROUP_PRINCIPLE: "### Principles",
}

#: The staged set held 18 entries; the root index caps at 20.
ENTRY_CAP: Final = 20

#: Markers wrapping the generated block inside ``soul.md``.
GENERATED_BEGIN: Final = (
    "<!-- GENERATED soul-index — regenerate: scripts/gen-soul-index -->"
)
GENERATED_END: Final = "<!-- /GENERATED soul-index -->"

_ENTRY_GLOB: Final = "soul-*.md"
_HOOK_RE: Final = re.compile(r"^(?:I|My)\s[^.\n]*\.", re.M)
_STAGED_RE: Final = re.compile(r"^me\d{6,}$")


class SoulIndexError(ValueError):
    """An entry file or the root file breaks the generation contract."""


@dataclass(frozen=True)
class SoulEntry:
    """One generated table row, derived from a ``soul-*.md`` file."""

    slug: str
    group: str
    name: str
    hook: str
    staged_from: str


def _entry(path: Path) -> SoulEntry:
    """Derive the row for one entry file; raise on a contract break."""
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    if fm.id and fm.id != path.stem:
        raise SoulIndexError(f"{path.name}: id {fm.id!r} != slug {path.stem!r}")
    group = fm.extra.get("soul-group", "").strip()
    if group not in _GROUPS:
        raise SoulIndexError(
            f"{path.name}: soul-group must be one of {sorted(_GROUPS)}"
        )
    staged_from = fm.extra.get("staged-from", "").strip()
    if not _STAGED_RE.match(staged_from):
        raise SoulIndexError(
            f"{path.name}: staged-from must be a me<digits> provenance handle"
        )
    if (fm.parent or "") != ROOT_SLUG:
        raise SoulIndexError(f"{path.name}: parent must be {ROOT_SLUG!r}")
    hook_match = _HOOK_RE.search(_strip_front_matter(text))
    if hook_match is None:
        raise SoulIndexError(f"{path.name}: no first-person hook line ('I …' / 'My …')")
    name = path.stem[len(ROOT_SLUG) + 1 :].replace("-", " ").capitalize()
    return SoulEntry(
        slug=path.stem,
        group=group,
        name=name,
        hook=hook_match.group(0),
        staged_from=staged_from,
    )


def iter_entries(skills_dir: Path) -> tuple[SoulEntry, ...]:
    """Every ``soul-*.md`` entry, roles first then principles, each in
    staging order (numeric ``staged-from:`` handle)."""
    entries = [_entry(p) for p in sorted(skills_dir.glob(_ENTRY_GLOB))]
    if len(entries) > ENTRY_CAP:
        raise SoulIndexError(
            f"{len(entries)} soul entries exceed the {ENTRY_CAP}-entry root cap"
        )
    rank = {g: i for i, g in enumerate(_GROUPS)}
    return tuple(sorted(entries, key=lambda e: (rank[e.group], int(e.staged_from[2:]))))


def render_block(skills_dir: Path) -> str:
    """The marked generated block for ``soul.md``, markers included."""
    entries = iter_entries(skills_dir)
    lines = [GENERATED_BEGIN, ""]
    for group in _GROUPS:
        members = [e for e in entries if e.group == group]
        lines.append(_GROUP_HEADINGS[group])
        lines.append("")
        lines.append("| Entry | Stance |")
        lines.append("|---|---|")
        for e in members:
            lines.append(f"| [[{e.slug}]] | **{e.name}** — {e.hook} |")
        lines.append("")
    lines.append(GENERATED_END)
    return "\n".join(lines)


def sync_root(skills_dir: Path) -> bool:
    """Rewrite the marked block inside ``soul.md``; True when it changed."""
    path = skills_dir / f"{ROOT_SLUG}.md"
    text = path.read_text(encoding="utf-8")
    begin = text.find(GENERATED_BEGIN)
    end = text.find(GENERATED_END)
    if begin == -1 or end == -1 or end < begin:
        raise SoulIndexError(
            f"{path.name}: no {GENERATED_BEGIN!r} … {GENERATED_END!r} region"
        )
    updated = text[:begin] + render_block(skills_dir) + text[end + len(GENERATED_END) :]
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True
