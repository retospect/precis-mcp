"""Unit tests for the fixer intake (ADR 0048).

The two risky small bits: the proposal-ready convention (only
``status: ready`` files, skip TEMPLATE/README) and the idempotent pick
(skip items whose branch already exists).
"""

from __future__ import annotations

from pathlib import Path

from precis.fixer.intake import WorkItem, parse_front_matter, pick_next, ready_proposals


def test_parse_front_matter_basic() -> None:
    fm = parse_front_matter("---\nstatus: ready\ntitle: Fix the thing\n---\n\n# Body\n")
    assert fm == {"status": "ready", "title": "Fix the thing"}


def test_parse_front_matter_missing_block() -> None:
    assert parse_front_matter("# no front matter\n") == {}


def test_parse_front_matter_skips_comments_and_blanks() -> None:
    fm = parse_front_matter("---\n# a comment\n\nstatus: draft\n---\nbody")
    assert fm == {"status": "draft"}


def _write(dir_: Path, name: str, text: str) -> None:
    (dir_ / name).write_text(text, encoding="utf-8")


def test_ready_proposals_only_ready(tmp_path: Path) -> None:
    _write(tmp_path, "a-feature.md", "---\nstatus: ready\n---\n\n# A feature\n")
    _write(tmp_path, "b-draft.md", "---\nstatus: draft\n---\n\n# Not yet\n")
    _write(tmp_path, "TEMPLATE.md", "---\nstatus: ready\n---\n\n# template\n")
    _write(tmp_path, "README.md", "---\nstatus: ready\n---\n\n# readme\n")

    items = ready_proposals(tmp_path)
    slugs = [i.slug for i in items]
    assert slugs == ["a-feature"]
    assert items[0].branch == "fix/a-feature"
    assert items[0].kind == "proposal"


def test_ready_proposals_title_fallback_to_heading(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\n---\n\n# The Heading Title\n\nbody\n")
    (item,) = ready_proposals(tmp_path)
    assert item.title == "The Heading Title"


def test_ready_proposals_title_from_front_matter(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\ntitle: FM Title\n---\n\n# Other\n")
    (item,) = ready_proposals(tmp_path)
    assert item.title == "FM Title"


def test_ready_proposals_missing_dir(tmp_path: Path) -> None:
    assert ready_proposals(tmp_path / "nope") == []


def _item(slug: str) -> WorkItem:
    return WorkItem(
        kind="proposal", slug=slug, title=slug, branch=f"fix/{slug}", spec_text="x"
    )


def test_pick_next_skips_existing_branch() -> None:
    items = [_item("one"), _item("two"), _item("three")]
    existing = {"fix/one", "fix/two"}
    picked = pick_next(items, lambda b: b in existing)
    assert picked is not None and picked.slug == "three"


def test_pick_next_none_when_all_branched() -> None:
    items = [_item("one")]
    assert pick_next(items, lambda _b: True) is None


def test_pick_next_first_when_none_branched() -> None:
    items = [_item("one"), _item("two")]
    picked = pick_next(items, lambda _b: False)
    assert picked is not None and picked.slug == "one"
