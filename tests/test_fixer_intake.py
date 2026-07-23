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


def test_ready_proposals_model_and_blocked_by_absent_by_default(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\n---\n\n# X\n")
    (item,) = ready_proposals(tmp_path)
    assert item.model is None
    assert item.blocked_by is None


def test_ready_proposals_parses_model_and_blocked_by(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "x.md",
        "---\nstatus: ready\nmodel: opus\nblocked-by: some-earlier-thing\n---\n\n# X\n",
    )
    (item,) = ready_proposals(tmp_path)
    assert item.model == "opus"
    assert item.blocked_by == "some-earlier-thing"


def _item(
    slug: str, *, blocked_by: str | None = None, model: str | None = None
) -> WorkItem:
    return WorkItem(
        kind="proposal",
        slug=slug,
        title=slug,
        branch=f"fix/{slug}",
        spec_text="x",
        blocked_by=blocked_by,
        model=model,
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


def test_pick_next_skips_blocked_while_predecessor_branch_exists() -> None:
    items = [_item("two", blocked_by="one")]
    picked = pick_next(items, lambda b: b == "fix/one")
    assert picked is None


def test_pick_next_picks_blocked_once_predecessor_branch_gone() -> None:
    # Predecessor already shipped and dropped out of `items` entirely —
    # the check is against branch_exists alone, not presence in items.
    items = [_item("two", blocked_by="one")]
    picked = pick_next(items, lambda _b: False)
    assert picked is not None and picked.slug == "two"


def test_pick_next_blocked_by_does_not_affect_unblocked_items() -> None:
    items = [_item("blocked", blocked_by="predecessor"), _item("free")]
    picked = pick_next(items, lambda b: b == "fix/predecessor")
    assert picked is not None and picked.slug == "free"
