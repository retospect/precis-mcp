"""Unit tests for the fixer intake.

The risky small bits: the proposal-ready convention (only
``status: ready`` files, skip TEMPLATE/README), the idempotent pick
(skip items whose branch already exists), and the gripe-intake lane
(promotion filter, WorkItem mapping, merge ordering, dial-off no-DB
behavior, DB-error degradation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.fixer import intake as intake_mod
from precis.fixer.intake import WorkItem, parse_front_matter, pick_next, ready_items


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


def test_ready_items_only_ready(tmp_path: Path) -> None:
    _write(tmp_path, "a-feature.md", "---\nstatus: ready\n---\n\n# A feature\n")
    _write(tmp_path, "b-draft.md", "---\nstatus: draft\n---\n\n# Not yet\n")
    _write(tmp_path, "TEMPLATE.md", "---\nstatus: ready\n---\n\n# template\n")
    _write(tmp_path, "README.md", "---\nstatus: ready\n---\n\n# readme\n")

    items = ready_items(tmp_path)
    slugs = [i.slug for i in items]
    assert slugs == ["a-feature"]
    assert items[0].branch == "fix/a-feature"
    assert items[0].kind == "proposal"


def test_ready_items_title_fallback_to_heading(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\n---\n\n# The Heading Title\n\nbody\n")
    (item,) = ready_items(tmp_path)
    assert item.title == "The Heading Title"


def test_ready_items_title_from_front_matter(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\ntitle: FM Title\n---\n\n# Other\n")
    (item,) = ready_items(tmp_path)
    assert item.title == "FM Title"


def test_ready_items_missing_dir(tmp_path: Path) -> None:
    assert ready_items(tmp_path / "nope") == []


def test_ready_items_model_and_blocked_by_absent_by_default(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\n---\n\n# X\n")
    (item,) = ready_items(tmp_path)
    assert item.model is None
    assert item.blocked_by is None


def test_ready_items_parses_model_and_blocked_by(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "x.md",
        "---\nstatus: ready\nmodel: opus\nblocked-by: some-earlier-thing\n---\n\n# X\n",
    )
    (item,) = ready_items(tmp_path)
    assert item.model == "opus"
    assert item.blocked_by == "some-earlier-thing"


def test_ready_items_prio_defaults_to_normal(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\n---\n\n# X\n")
    (item,) = ready_items(tmp_path)
    assert item.prio == "normal"


def test_ready_items_unknown_prio_falls_to_normal(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nstatus: ready\nprio: urgent\n---\n\n# X\n")
    (item,) = ready_items(tmp_path)
    assert item.prio == "normal"


def test_ready_items_sorted_high_prio_first(tmp_path: Path) -> None:
    # Filenames are deliberately reverse-alphabetical to the desired order,
    # proving priority — not filename — drives the sort.
    _write(tmp_path, "a-low.md", "---\nstatus: ready\nprio: low\n---\n\n# low\n")
    _write(tmp_path, "b-norm.md", "---\nstatus: ready\n---\n\n# norm\n")
    _write(tmp_path, "c-high.md", "---\nstatus: ready\nprio: high\n---\n\n# high\n")

    slugs = [i.slug for i in ready_items(tmp_path)]
    assert slugs == ["c-high", "b-norm", "a-low"]


def test_ready_items_filename_order_within_a_bucket(tmp_path: Path) -> None:
    _write(tmp_path, "z-high.md", "---\nstatus: ready\nprio: high\n---\n\n# z\n")
    _write(tmp_path, "a-high.md", "---\nstatus: ready\nprio: high\n---\n\n# a\n")

    slugs = [i.slug for i in ready_items(tmp_path)]
    assert slugs == ["a-high", "z-high"]


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


# ── gripe intake: pure helpers ───────────────────────────────────────

_TE = intake_mod._TimelineEntry


def test_gripe_prio_bucket_1_to_3_is_high() -> None:
    assert intake_mod._gripe_prio_bucket(1) == "high"
    assert intake_mod._gripe_prio_bucket(3) == "high"


def test_gripe_prio_bucket_4_to_6_is_normal() -> None:
    assert intake_mod._gripe_prio_bucket(4) == "normal"
    assert intake_mod._gripe_prio_bucket(6) == "normal"


def test_gripe_prio_bucket_7_to_10_is_low() -> None:
    assert intake_mod._gripe_prio_bucket(7) == "low"
    assert intake_mod._gripe_prio_bucket(10) == "low"


def test_gripe_prio_bucket_unset_is_low() -> None:
    assert intake_mod._gripe_prio_bucket(None) == "low"


def test_is_diagnosed_true_with_diagnosis_comment() -> None:
    entries = [
        _TE("gripe_body", 0, "It broke."),
        _TE("gripe_comment", 1, "DIAGNOSIS (auto, confidence=high): root cause is X."),
    ]
    assert intake_mod._is_diagnosed(entries) is True


def test_is_diagnosed_false_without_any_comment() -> None:
    entries = [_TE("gripe_body", 0, "It broke.")]
    assert intake_mod._is_diagnosed(entries) is False


def test_is_diagnosed_false_with_non_diagnosis_comment() -> None:
    entries = [
        _TE("gripe_body", 0, "It broke."),
        _TE("gripe_comment", 1, "seen this before, still triaging"),
    ]
    assert intake_mod._is_diagnosed(entries) is False


def test_render_gripe_spec_includes_title_body_and_comments_in_order() -> None:
    entries = [
        _TE("gripe_body", 0, "It broke."),
        _TE("gripe_comment", 1, "DIAGNOSIS (auto): root cause is X."),
    ]
    spec = intake_mod._render_gripe_spec("Something broke", entries)
    assert spec.index("Something broke") < spec.index("It broke.")
    assert spec.index("It broke.") < spec.index("DIAGNOSIS (auto): root cause is X.")


def test_work_item_from_gripe_none_without_diagnosis() -> None:
    entries = [_TE("gripe_body", 0, "It broke.")]
    assert intake_mod._work_item_from_gripe(42, "Broke", 2, entries) is None


def test_work_item_from_gripe_maps_fields() -> None:
    entries = [
        _TE("gripe_body", 0, "It broke."),
        _TE("gripe_comment", 1, "DIAGNOSIS (auto): root cause is X."),
    ]
    item = intake_mod._work_item_from_gripe(42, "Broke", 2, entries)
    assert item is not None
    assert item.kind == "gripe"
    assert item.slug == "gr42"
    assert item.branch == "fix/gr42"
    assert item.title == "Broke"
    assert item.model is None
    assert item.prio == "high"
    assert "It broke." in item.spec_text
    assert "DIAGNOSIS (auto): root cause is X." in item.spec_text


# ── gripe intake: gripe_items (fake store, no real DB) ───────────────


class _FakeChunk:
    def __init__(self, chunk_kind: str, ord_: int, text: str) -> None:
        self.chunk_kind = chunk_kind
        self.ord = ord_
        self.text = text


class _FakeRef:
    def __init__(self, id: int, title: str, prio: int | None) -> None:
        self.id = id
        self.title = title
        self.prio = prio


class _FakeStore:
    chunks = property(
        lambda self: self
    )  # chunks carve: flat fake doubles as its own sub-store

    def __init__(
        self, refs: list[_FakeRef], blocks_by_ref: dict[int, list[_FakeChunk]]
    ) -> None:
        self.refs = refs
        self.blocks_by_ref = blocks_by_ref
        self.closed = False
        self.list_refs_kwargs: dict[str, Any] | None = None

    def list_refs(self, **kwargs: Any) -> list[_FakeRef]:
        self.list_refs_kwargs = kwargs
        return self.refs

    def list_chunks_for_ref(self, ref_id: int) -> list[_FakeChunk]:
        return self.blocks_by_ref.get(ref_id, [])

    def close(self) -> None:
        self.closed = True


def test_gripe_items_promotes_diagnosed_and_skips_undiagnosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnosed = _FakeRef(id=100, title="Bad thing happens", prio=2)
    undiagnosed = _FakeRef(id=200, title="Also bad", prio=5)
    blocks = {
        100: [
            _FakeChunk("gripe_body", 0, "It broke."),
            _FakeChunk("gripe_comment", 1, "DIAGNOSIS (auto): root cause is X."),
        ],
        200: [_FakeChunk("gripe_body", 0, "Not diagnosed yet.")],
    }
    fake = _FakeStore([diagnosed, undiagnosed], blocks)

    import precis.store.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

    items = intake_mod.gripe_items("postgresql://example/db")

    assert [i.slug for i in items] == ["gr100"]
    (item,) = items
    assert item.kind == "gripe"
    assert item.branch == "fix/gr100"
    assert item.title == "Bad thing happens"
    assert item.model is None
    assert item.prio == "high"
    assert "It broke." in item.spec_text
    assert "DIAGNOSIS (auto): root cause is X." in item.spec_text
    assert fake.closed is True
    assert fake.list_refs_kwargs is not None
    assert fake.list_refs_kwargs["kind"] == "gripe"
    assert set(fake.list_refs_kwargs["tags"]) == {"STATUS:open", "auto-fix"}


def test_gripe_items_db_unreachable_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.store.store as store_mod

    def _boom(*a: Any, **k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(store_mod.Store, "connect", _boom)

    assert intake_mod.gripe_items("postgresql://unreachable/db") == []


def test_gripe_items_query_error_degrades_to_empty_and_closes_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingStore:
        def __init__(self) -> None:
            self.closed = False

        def list_refs(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("boom")

        def close(self) -> None:
            self.closed = True

    fake = _FailingStore()
    import precis.store.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

    assert intake_mod.gripe_items("postgresql://example/db") == []
    assert fake.closed is True


# ── gripe intake: all_items merge + dial ─────────────────────────────


def test_all_items_dial_off_never_calls_gripe_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _must_not_be_called(db_url: str) -> list[WorkItem]:
        raise AssertionError("gripe_items must not be called when the dial is off")

    monkeypatch.setattr(intake_mod, "gripe_items", _must_not_be_called)
    _write(tmp_path, "a.md", "---\nstatus: ready\n---\n\n# A\n")

    items = intake_mod.all_items(tmp_path, None)

    assert [i.slug for i in items] == ["a"]


def test_all_items_dial_off_matches_ready_items_exactly(tmp_path: Path) -> None:
    _write(tmp_path, "a-low.md", "---\nstatus: ready\nprio: low\n---\n\n# low\n")
    _write(tmp_path, "b-norm.md", "---\nstatus: ready\n---\n\n# norm\n")
    _write(tmp_path, "c-high.md", "---\nstatus: ready\nprio: high\n---\n\n# high\n")

    assert intake_mod.all_items(tmp_path, None) == ready_items(tmp_path)


def test_all_items_merges_gripes_after_proposals_reordered_by_prio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write(tmp_path, "normal-proposal.md", "---\nstatus: ready\n---\n\n# Normal\n")
    high_gripe = WorkItem(
        kind="gripe",
        slug="gr9",
        title="Urgent bug",
        branch="fix/gr9",
        spec_text="x",
        model=None,
        prio="high",
    )
    monkeypatch.setattr(intake_mod, "gripe_items", lambda db_url: [high_gripe])

    items = intake_mod.all_items(tmp_path, "postgresql://example/db")

    # A high-prio gripe outranks a normal-prio proposal.
    assert [i.slug for i in items] == ["gr9", "normal-proposal"]


def test_all_items_ties_proposals_before_gripes_in_same_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write(tmp_path, "normal-proposal.md", "---\nstatus: ready\n---\n\n# Normal\n")
    normal_gripe = WorkItem(
        kind="gripe",
        slug="gr9",
        title="Also normal",
        branch="fix/gr9",
        spec_text="x",
        model=None,
        prio="normal",
    )
    monkeypatch.setattr(intake_mod, "gripe_items", lambda db_url: [normal_gripe])

    items = intake_mod.all_items(tmp_path, "postgresql://example/db")

    assert [i.slug for i in items] == ["normal-proposal", "gr9"]
