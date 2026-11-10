"""Soul conduct layer — generated-root invariant (gr563975).

``soul.md`` is the root index of the migrated staged soul (memory root
me563907 + 18 children, Reto 2026-11-04); ``soul-*.md`` entries are the
one canonical source and its two entry tables are GENERATED from them —
``scripts/gen-soul-index`` rewrites the marked block. These tests pin:

- the committed ``soul.md`` carries exactly the generated block (the
  index can never drift from the entries it lists);
- the root keeps at most 20 entries;
- every entry carries the contract the generator and the migration need:
  ``parent: soul``, a ``soul-group:`` of ``role`` or ``principle``, a
  ``staged-from:`` me-handle (unique — provenance into the retired
  staging graph), a first-person hook line and a ``## Goals → skills``
  table (the goal-to-skill links the staged cards carried);
- roles and principles use the same ordinary-skill shape — the same
  frontmatter keys and sections, nothing persona-flavoured.

Pure file stats — no DB, no embedder.
"""

from __future__ import annotations

import re
from pathlib import Path

from precis.skill_index import soul_index
from precis.skill_index.chunker import chunk_by_h2
from precis.skill_index.soul_index import SoulIndexError
from precis.utils.skill_common import parse_frontmatter
from tests._skill_roots import BUILTIN_SKILLS_DIR

ROOT_FILE = BUILTIN_SKILLS_DIR / f"{soul_index.ROOT_SLUG}.md"

#: The staged staging-graph membership (root me563907 excluded — it is
#: the index file itself, not an entry).
EXPECTED_STAGED = frozenset(
    {
        "me563910",
        "me563911",
        "me563912",
        "me563913",
        "me563914",
        "me563915",
        "me563916",
        "me563918",
        "me563919",
        "me563920",
        "me563921",
        "me563922",
        "me563923",
        "me563924",
        "me563925",
        "me563926",
        "me563927",
        "me563928",
    }
)

_ENTRY_FILES = sorted(BUILTIN_SKILLS_DIR.glob("soul-*.md"))


def _root_text() -> str:
    return ROOT_FILE.read_text(encoding="utf-8")


def test_root_generated_block_is_current() -> None:
    """The committed table equals a fresh render — drift fails loudly."""
    block = soul_index.render_block(BUILTIN_SKILLS_DIR)
    text = _root_text()
    assert block in text, (
        "soul.md entry tables are stale — run scripts/gen-soul-index "
        "to regenerate the GENERATED soul-index block"
    )
    # Exactly one marked region.
    assert text.count(soul_index.GENERATED_BEGIN) == 1
    assert text.count(soul_index.GENERATED_END) == 1


def test_root_cap_and_expected_membership() -> None:
    entries = soul_index.iter_entries(BUILTIN_SKILLS_DIR)
    assert len(entries) <= soul_index.ENTRY_CAP
    assert len(entries) == len(_ENTRY_FILES)
    # The whole staged set migrated — no entry silently dropped.
    assert {e.staged_from for e in entries} == EXPECTED_STAGED
    # Roles list before principles, each group in staging order.
    groups = [e.group for e in entries]
    first_principle = (
        groups.index(soul_index.GROUP_PRINCIPLE)
        if soul_index.GROUP_PRINCIPLE in groups
        else len(groups)
    )
    assert soul_index.GROUP_ROLE not in groups[first_principle:]
    for g in (soul_index.GROUP_ROLE, soul_index.GROUP_PRINCIPLE):
        nums = [int(e.staged_from[2:]) for e in entries if e.group == g]
        assert nums == sorted(nums)


def test_every_entry_carries_the_contract() -> None:
    assert _ENTRY_FILES, "no soul-*.md entries found"
    for path in _ENTRY_FILES:
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        # The generator's contract — iter_entries validates it too, but
        # a file that slipped the glob must still be shaped correctly.
        assert fm.id == path.stem, path.name
        assert fm.parent == soul_index.ROOT_SLUG, path.name
        assert fm.extra.get("soul-group") in {
            soul_index.GROUP_ROLE,
            soul_index.GROUP_PRINCIPLE,
        }, path.name
        staged = fm.extra.get("staged-from", "")
        assert re.fullmatch(r"me\d{6,}", staged), path.name
        assert fm.flavor == "concept", path.name
        assert fm.family == "core", path.name
        # First-person behavioural hook survives migration.
        head = chunk_by_h2(text)[0].text
        assert re.search(r"^(?:I|My)\s", head, re.M), path.name
        # Goal-to-skill links survive as a real table row set.
        assert "## Goals → skills" in text, path.name
        assert "| Goal | Skill |" in text, path.name


def test_entries_link_back_to_root() -> None:
    for path in _ENTRY_FILES:
        assert "[[soul]]" in path.read_text(encoding="utf-8"), path.name


def test_staged_from_handles_are_unique() -> None:
    handles = [
        parse_frontmatter(p.read_text(encoding="utf-8")).extra.get("staged-from")
        for p in _ENTRY_FILES
    ]
    assert len(handles) == len(set(handles))


def test_generator_rejects_a_bad_entry(tmp_path: Path) -> None:
    bad = tmp_path / "soul-bogus.md"
    bad.write_text("---\nid: soul-bogus\n---\n# x\nno hook here\n", encoding="utf-8")
    try:
        soul_index.iter_entries(tmp_path)
    except SoulIndexError as exc:
        assert "soul-bogus.md" in str(exc)
    else:  # pragma: no cover - contract break must raise
        raise AssertionError("iter_entries accepted a contract-less entry")
